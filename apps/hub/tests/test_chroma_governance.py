"""ADR-0037 contracts against real Chroma SQLite logs and native ID mapping."""

import asyncio
import sqlite3
from types import SimpleNamespace

import chromadb
import pytest
from cloud_memory_hub.chroma_governance import (
    MAX_SEGMENTS,
    append_target_deletes,
    payload_rows_present,
    point_ids,
    purge_payload_rows,
    queue_topic,
    require_sequence,
)
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_purge import Purger

ERROR = "^governance_chroma_sequence_invalid$"


def stored(tmp_path, native="synthetic:non-uuid-target"):
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    collection = client.create_collection("synthetic-governance-contract")
    identity = point_ids([native])[0]
    collection.add(ids=[identity], embeddings=[[1.0, 0.0]], documents=["Synthetic old text"])
    vector = SimpleNamespace(_client=client, _collection=collection)
    return vector, tmp_path / "chroma" / "chroma.sqlite3", queue_topic(vector), identity


def dump(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


def test_real_non_uuid_purge_maps_only_queue_ids_and_preserves_other_collection(tmp_path):
    native, request = "synthetic:non-uuid-target", "synthetic-request"
    vector, path, topic, identity = stored(tmp_path, native)
    assert native != identity
    other = vector._client.create_collection("synthetic-other-collection")
    other.add(ids=[identity], embeddings=[[0.0, 1.0]], documents=["Unrelated synthetic text"])
    history, cache = tmp_path / "history.sqlite3", tmp_path / "cache.sqlite3"
    with sqlite3.connect(history) as db:
        db.execute("CREATE TABLE memory_history(memory_id TEXT, content TEXT)")
        db.executemany(
            "INSERT INTO memory_history VALUES (?,?)", [(native, "target"), (identity, "other")]
        )
    with sqlite3.connect(cache) as db:
        db.execute("CREATE TABLE write_outcomes(request_id TEXT, content TEXT)")
        # Minimal side-table fixtures for this Chroma ID-mapping contract;
        # native diagnostic persistence/recovery has separate integration tests.
        for table in ("pipeline_logs_v2", "memory_operations_v2"):
            db.execute(f"CREATE TABLE {table}(request_id TEXT, content TEXT)")
        db.executemany(
            "INSERT INTO write_outcomes VALUES (?,?)", [(request, "target"), (native, "other")]
        )
    paths = {"chroma": path, "chroma_topic": topic, "history": history, "cache": cache}
    purger = Purger(None, None, tmp_path)
    assert purger._side_present(paths, {native}, {request})
    asyncio.run(append_target_deletes(vector, {native}))
    purger._sdk_side_tables(paths, {native}, {request})
    require_sequence(path)
    assert not purger._side_present(paths, {native}, {request})
    assert vector._collection.get(ids=[identity])["ids"] == []
    assert other.get(ids=[identity])["documents"] == ["Unrelated synthetic text"]
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT operation,vector,encoding,metadata FROM embeddings_queue "
            "WHERE topic=? AND id=?",
            (topic, identity),
        ).fetchall()
        assert rows and all(row == (3, None, None, "{}") for row in rows)
        assert db.execute(
            "SELECT 1 FROM embeddings_queue WHERE topic!=? AND id=?", (topic, identity)
        ).fetchone()
    with sqlite3.connect(history) as db:
        assert db.execute("SELECT memory_id FROM memory_history").fetchall() == [(identity,)]
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT request_id FROM write_outcomes").fetchall() == [(native,)]
    # Retried absent IDs still get a real deletion marker, never a random anchor.
    asyncio.run(append_target_deletes(vector, {native}))
    purger._sdk_side_tables(paths, {native}, {request})
    assert not purger._side_present(paths, {native}, {request})


def test_only_strict_empty_delete_rows_are_exempt_from_presence_and_cleanup(tmp_path):
    vector, path, topic, identity = stored(tmp_path)
    asyncio.run(append_target_deletes(vector, {"synthetic:non-uuid-target"}))
    shapes = (
        (3, b"", None, None),
        (3, None, "", None),
        (3, None, None, '{"content":"Synthetic residue"}'),
        (3, None, None, "{ }"),
        (3, None, None, b"{}"),
        (2, None, None, None),
        ("3-invalid", None, None, "{}"),
        (3, None, None, None),
        (3, None, None, "{}"),
    )
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO embeddings_queue(operation,vector,encoding,metadata,topic,id) "
            "VALUES (?,?,?,?,?,?)",
            [(*shape, topic, identity) for shape in shapes],
        )
    assert payload_rows_present(path, topic, [identity])
    purge_payload_rows(path, topic, [identity])
    assert not payload_rows_present(path, topic, [identity])
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT operation,vector,encoding,metadata FROM embeddings_queue "
            "WHERE topic=? AND id=?",
            (topic, identity),
        ).fetchall()
    assert rows == [(3, None, None, "{}"), (3, None, None, None), (3, None, None, "{}")]


@pytest.mark.parametrize(
    "damage",
    (
        "negative_queue",
        "negative_watermark",
        "text_watermark",
        "null_watermark",
        "regressed_queue",
        "missing_table",
    ),
)
def test_invalid_sequence_is_read_only_and_cleanup_does_not_repair_it(tmp_path, damage):
    _, path, topic, identity = stored(tmp_path)
    with sqlite3.connect(path) as db:
        if damage == "negative_queue":
            db.execute("UPDATE embeddings_queue SET seq_id=-1")
        elif damage == "regressed_queue":
            db.execute("DELETE FROM embeddings_queue")
        elif damage == "missing_table":
            db.execute("DROP TABLE max_seq_id")
        else:
            value = {"negative_watermark": -1, "text_watermark": "invalid", "null_watermark": None}[
                damage
            ]
            db.execute("UPDATE max_seq_id SET seq_id=?", (value,))
    before = dump(path)
    for check in (
        lambda: require_sequence(path),
        lambda: payload_rows_present(path, topic, [identity]),
        lambda: purge_payload_rows(path, topic, [identity]),
    ):
        with pytest.raises(GovernanceError, match=ERROR):
            check()
        assert dump(path) == before


def test_empty_real_store_is_valid_but_missing_store_is_not_created(tmp_path):
    client = chromadb.PersistentClient(path=str(tmp_path / "empty"))
    client.create_collection("synthetic-empty-collection")
    require_sequence(tmp_path / "empty" / "chroma.sqlite3")
    missing = tmp_path / "absent.sqlite3"
    with pytest.raises(GovernanceError, match=ERROR):
        require_sequence(missing)
    assert not missing.exists()


def test_cleanup_rolls_back_when_it_would_regress_the_queue_head(tmp_path):
    _, path, topic, identity = stored(tmp_path)
    before = dump(path)
    # A caller that skipped the mandatory native DELETE must not commit damage.
    with pytest.raises(GovernanceError, match=ERROR):
        purge_payload_rows(path, topic, [identity])
    assert dump(path) == before


def test_non_rowid_primary_key_cannot_hide_invalid_sequence_between_integer_endpoints(tmp_path):
    path = tmp_path / "wrong-schema.sqlite3"
    with sqlite3.connect(path) as db:
        # SQLite's DESC primary key is not a rowid alias and accepts a real
        # number in the middle, even though both numeric endpoints are integers.
        db.execute("CREATE TABLE embeddings_queue(seq_id INTEGER PRIMARY KEY DESC)")
        db.execute("CREATE TABLE max_seq_id(segment_id TEXT PRIMARY KEY, seq_id INTEGER)")
        db.executemany("INSERT INTO embeddings_queue VALUES (?)", [(1,), (1.5,), (2,)])
        assert db.execute("SELECT min(seq_id),max(seq_id) FROM embeddings_queue").fetchone() == (
            1,
            2,
        )
    before = dump(path)
    with pytest.raises(GovernanceError, match=ERROR):
        require_sequence(path)
    assert dump(path) == before


def test_segment_budget_rejects_instead_of_certifying_truncated_watermarks(tmp_path):
    _, path, _, _ = stored(tmp_path)
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO max_seq_id(segment_id,seq_id) VALUES (?,?)",
            [(f"synthetic-{index}", 0) for index in range(MAX_SEGMENTS)],
        )
    before = dump(path)
    with pytest.raises(GovernanceError, match=ERROR):
        require_sequence(path)
    assert dump(path) == before
