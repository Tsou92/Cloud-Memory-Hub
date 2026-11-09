"""Pinned Chroma purge-log invariants; callers hold the existing writer barrier."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from chromadb.ingest.impl.utils import create_topic_name
from hy_memory.data.vector_store_chroma import ChromaVectorStore, _run_in_vdb_pool

from cloud_memory_hub.governance_journal import GovernanceError

BATCH_SIZE = 500
MAX_SEGMENTS = 10_000
MAX_SCHEMA_ITEMS = 32
# IS/typeof keep this predicate two-valued, including malformed NULL operations.
# A DELETE with any vector, encoding or nonempty metadata still contains residue.
_EMPTY_DELETE = (
    "typeof(operation)='integer' AND operation=3 "
    "AND vector IS NULL AND encoding IS NULL "
    "AND (metadata IS NULL OR (typeof(metadata)='text' AND metadata='{}'))"
)


def queue_topic(vector):
    settings = vector._client.get_settings()
    return create_topic_name(settings.tenant_id, settings.topic_namespace, vector._collection.id)


def point_ids(native):
    return sorted({ChromaVectorStore._node_id_to_point_id(identity) for identity in native})


def _batches(identities):
    values = sorted(set(identities))
    for start in range(0, len(values), BATCH_SIZE):
        yield values[start : start + BATCH_SIZE]


def _queue_head(db):
    # The pinned INTEGER PRIMARY KEY aliases rowid, so every value is an integer
    # and independent MIN/MAX queries seek the two B-tree endpoints. Do not scan
    # historical payload rows merely to check a startup sequence invariant.
    columns = db.execute(
        "SELECT name,type,pk FROM pragma_table_info('embeddings_queue') LIMIT ?",
        (MAX_SCHEMA_ITEMS + 1,),
    ).fetchall()
    indexes = db.execute(
        "SELECT origin FROM pragma_index_list('embeddings_queue') LIMIT ?",
        (MAX_SCHEMA_ITEMS + 1,),
    ).fetchall()
    tables = db.execute(
        "SELECT type,wr FROM pragma_table_list WHERE schema='main' AND name='embeddings_queue'"
    ).fetchall()
    if (
        len(columns) > MAX_SCHEMA_ITEMS
        or len(indexes) > MAX_SCHEMA_ITEMS
        or tables != [("table", 0)]
        or [(name, kind.upper(), pk) for name, kind, pk in columns if pk]
        != [("seq_id", "INTEGER", 1)]
        # INTEGER PRIMARY KEY DESC has a separate primary-key index and is not
        # a rowid alias; endpoint types alone would miss an invalid middle row.
        or any(origin == "pk" for (origin,) in indexes)
    ):
        raise GovernanceError("governance_chroma_sequence_invalid")
    minimum = db.execute("SELECT min(seq_id) FROM embeddings_queue").fetchone()[0]
    maximum = db.execute("SELECT max(seq_id) FROM embeddings_queue").fetchone()[0]
    if minimum is None and maximum is None:
        return 0
    if type(minimum) is not int or type(maximum) is not int or minimum < 0 or maximum < minimum:
        raise GovernanceError("governance_chroma_sequence_invalid")
    return maximum


def _require_sequence(db):
    head = _queue_head(db)
    rows = db.execute("SELECT seq_id FROM max_seq_id LIMIT ?", (MAX_SEGMENTS + 1,)).fetchall()
    if len(rows) > MAX_SEGMENTS or any(type(value) is not int or value < 0 for (value,) in rows):
        raise GovernanceError("governance_chroma_sequence_invalid")
    if head < max((value for (value,) in rows), default=0):
        raise GovernanceError("governance_chroma_sequence_invalid")


@contextmanager
def _connection(path, *, writing=False):
    try:
        mode = "rw" if writing else "ro"
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=" + mode, uri=True, timeout=30)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
                yield db
        finally:
            db.close()
    except GovernanceError:
        raise
    except Exception:
        raise GovernanceError("governance_chroma_sequence_invalid") from None


def require_sequence(path):
    """Read one SQLite snapshot; an absent or damaged store is not an empty log."""
    with _connection(path) as db:
        _require_sequence(db)


async def append_target_deletes(vector, native):
    """Even an already-absent target needs a replayable native DELETE record."""
    for batch in _batches(point_ids(native)):
        await _run_in_vdb_pool(vector._collection.delete, ids=batch)


def purge_payload_rows(path, topic, identities):
    """Remove target content while retaining exact, content-free DELETE records."""
    with _connection(path, writing=True) as db:
        _require_sequence(db)
        for batch in _batches(identities):
            placeholders = ",".join("?" for _ in batch)
            db.execute(
                "DELETE FROM embeddings_queue WHERE topic=? "
                f"AND id IN ({placeholders}) AND NOT ({_EMPTY_DELETE})",
                (topic, *batch),
            )
        _require_sequence(db)


def payload_rows_present(path, topic, identities):
    """A strict empty DELETE is deletion state; every other target row is residue."""
    with _connection(path) as db:
        _require_sequence(db)
        for batch in _batches(identities):
            placeholders = ",".join("?" for _ in batch)
            if db.execute(
                "SELECT 1 FROM embeddings_queue WHERE topic=? "
                f"AND id IN ({placeholders}) AND NOT ({_EMPTY_DELETE}) LIMIT 1",
                (topic, *batch),
            ).fetchone():
                return True
    return False
