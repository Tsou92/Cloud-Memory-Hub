"""ADR-0037: retain DELETEs not yet consumed by the persisted vector segment.

Use real Rust Chroma, explicit 3D vectors and independent processes. The Hub
cleanup entry point is real; no model or fabricated watermark participates.
"""

import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings
from chromadb.ingest.impl.utils import create_topic_name
from cloud_memory_hub.chroma_governance import purge_payload_rows

COLLECTION = "synthetic_delete_replay"
TARGET = "synthetic-target"
OLD = "Synthetic original target document before physical purge."
FRESH = "Synthetic replacement document after deleting the same ID."
SIZE = 1000


def log_state(root):
    with sqlite3.connect(root / "chroma.sqlite3") as db:
        rows = db.execute(
            "SELECT seq_id,operation,id,vector IS NULL,encoding,metadata "
            "FROM embeddings_queue WHERE id=? ORDER BY seq_id",
            (TARGET,),
        ).fetchall()
        segments = db.execute(
            "SELECT segments.type,max_seq_id.seq_id FROM segments "
            "JOIN max_seq_id ON segments.id=max_seq_id.segment_id"
        ).fetchall()
        head = db.execute("SELECT max(seq_id) FROM embeddings_queue").fetchone()[0]
    assert all(type(value) is int for _, value in segments)
    watermarks = {"vector" if "/vector/" in kind else "metadata": value for kind, value in segments}
    return {"targets": rows, "watermarks": watermarks, "head": head}


def assert_others_preserved(collection):
    kept = collection.get(where={"match": "keep"}, include=["documents"])
    assert dict(zip(kept["ids"], kept["documents"], strict=True)) == {
        f"keep-{index}": f"Synthetic unrelated document {index}" for index in range(1, SIZE)
    }


def phase(root, mode):
    client = chromadb.PersistentClient(
        path=str(root), settings=Settings(anonymized_telemetry=False)
    )
    if mode == "seed":
        collection = client.create_collection(
            COLLECTION,
            metadata={
                "hnsw:space": "l2",
                "hnsw:sync_threshold": SIZE,
                "hnsw:batch_size": SIZE,
                "hnsw:search_ef": SIZE,
            },
        )
        collection.add(
            ids=[TARGET] + [f"keep-{index}" for index in range(1, SIZE)],
            embeddings=[[1.0, 0.0, 0.0]]
            + [
                [math.cos(index * 2 * math.pi / SIZE), math.sin(index * 2 * math.pi / SIZE), 0.0]
                for index in range(1, SIZE)
            ],
            documents=[OLD] + [f"Synthetic unrelated document {i}" for i in range(1, SIZE)],
            metadatas=[{"match": "target"}] + [{"match": "keep"}] * (SIZE - 1),
        )
        queried = collection.query(
            query_embeddings=[[1.0, 0.0, 0.0]], n_results=1, where={"match": "target"}
        )
        assert queried["ids"] == [[TARGET]]
        assert collection.get(ids=[TARGET], include=["documents"])["documents"] == [OLD]
        state = log_state(root)
        assert state["watermarks"]["vector"] == SIZE
        assert state["watermarks"]["metadata"] == SIZE
        return state
    collection = client.get_collection(COLLECTION)
    if mode == "cleanup":
        collection.delete(ids=[TARGET])
        fence = "synthetic-nonexistent-fence"
        assert collection.get(ids=[fence], include=[])["ids"] == []
        collection.delete(ids=[fence])
        before = log_state(root)
        deletes = [row[0] for row in before["targets"] if row[1] == 3]
        assert len(deletes) == 1
        assert before["watermarks"]["vector"] < deletes[0]
        assert before["watermarks"]["metadata"] >= deletes[0]
        # A native high-sequence fence isolates delayed DELETE consumption from
        # the independently covered queue-head regression.
        settings = client.get_settings()
        purge_payload_rows(
            root / "chroma.sqlite3",
            create_topic_name(settings.tenant_id, settings.topic_namespace, collection.id),
            {TARGET},
        )
        assert collection.get(ids=[TARGET], include=[])["ids"] == []
        assert_others_preserved(collection)
        return {"before": before, "after": log_state(root)}
    if mode == "read":
        assert collection.get(ids=[TARGET], include=[])["ids"] == []
        assert collection.query(
            query_embeddings=[[1.0, 0.0, 0.0]],
            n_results=1,
            where={"match": "target"},
        )["ids"] == [[]]
        assert_others_preserved(collection)
        return {"count": collection.count()}
    assert mode == "readd"
    collection.add(
        ids=[TARGET],
        embeddings=[[0.0, 0.0, 1.0]],
        documents=[FRESH],
        metadatas=[{"match": "target"}],
    )
    assert_others_preserved(collection)
    return {
        "get": collection.get(ids=[TARGET], include=["documents"]),
        # Distinct neighbors plus exact filtering avoid a degenerate graph of
        # identical vectors masquerading as a DELETE replay defect.
        "fresh_query": collection.query(
            query_embeddings=[[0.0, 0.0, 1.0]],
            n_results=1,
            where={"match": "target"},
            include=["documents", "distances"],
        ),
        "old_query": collection.query(
            query_embeddings=[[1.0, 0.0, 0.0]],
            n_results=1,
            where={"match": "target"},
            include=["documents", "distances"],
        ),
        "count": collection.count(),
    }


def run_phase(root, mode):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(root), mode],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{mode} failed: {result.stderr[-3000:]}"
    return json.loads((root / (mode + ".json")).read_text())


def test_product_cleanup_preserves_unconsumed_delete_across_processes(tmp_path):
    run_phase(tmp_path, "seed")
    cleanup = run_phase(tmp_path, "cleanup")
    assert run_phase(tmp_path, "read")["count"] == SIZE - 1
    result = run_phase(tmp_path, "readd")
    assert result["get"]["ids"] == [TARGET] and result["get"]["documents"] == [FRESH]
    assert result["fresh_query"]["ids"] == [[TARGET]], "get/query disagree after same-ID re-add"
    assert result["fresh_query"]["documents"] == [[FRESH]]
    assert result["fresh_query"]["distances"][0] == pytest.approx([0.0]), (
        "same-ID re-add retained its deleted vector"
    )
    assert result["old_query"]["ids"] == [[TARGET]]
    assert result["old_query"]["distances"][0] == pytest.approx([2.0])
    assert result["count"] == SIZE
    remaining = cleanup["after"]["targets"]
    assert remaining, "unconsumed target DELETE was removed"
    for _, operation, identity, vector_is_null, encoding, metadata in remaining:
        assert (operation, identity, vector_is_null, encoding) == (3, TARGET, 1, None)
        assert json.loads(metadata) == {}, "retained DELETE contains user content"
    assert cleanup["after"]["head"] >= max(cleanup["after"]["watermarks"].values())


if __name__ == "__main__":
    root, mode = Path(sys.argv[1]), sys.argv[2]
    (root / (mode + ".json")).write_text(json.dumps(phase(root, mode)))
