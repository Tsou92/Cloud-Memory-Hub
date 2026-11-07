"""Physical purge (ADR-0032 slice 2): remove every stored copy, then verify.

Store list from the residue experiment in ADR-0032. Each step is idempotent so
an interrupted purge is simply re-run from the start.
"""

import sqlite3
from pathlib import Path

from one_memory.hy_memory_bridge import _joined_thread

from cloud_memory_hub.chat_graph import PREFIX as CHAT
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.memory_graph import PREFIX as MEMORY
from cloud_memory_hub.profile_evidence import PREFIX as PROFILE
from cloud_memory_hub.reconcile_graph import PREFIX as INPUTS
from cloud_memory_hub.sdk_reconcile import RESERVED_PREFIXES
from cloud_memory_hub.source_history import PREFIX as ORIGINAL

# Snapshot prefix -> (table, json column) whose row is removed for that receipt.
_SNAPSHOTS = {
    CHAT: ("cmh_event_chat_inputs", "chat_json"),
    MEMORY: ("cmh_event_memory_inputs", "memory_json"),
    INPUTS: ("cmh_event_inputs", "inputs_json"),
    PROFILE: ("cmh_event_profile_inputs", "profile_json"),
}
# Child tables first: they reference outcomes, which reference receipts.
_RECEIPT_TABLES = (
    "cmh_event_profile_outputs",
    "cmh_event_profile_inputs",
    "cmh_event_memory_inputs",
    "cmh_event_chat_inputs",
    "cmh_event_inputs",
    "cmh_event_contexts",
    "cmh_event_output_links",
    "cmh_event_outcomes",
    "cmh_event_receipts",
)


def partition(records):
    """Split a purge scope into SDK IDs, whole receipts and individual snapshots."""
    native, receipts, snapshots = set(), set(), set()
    for identity in records:
        if identity.startswith(ORIGINAL):
            receipts.add(identity[len(ORIGINAL) :])
        elif identity.startswith(tuple(_SNAPSHOTS)):
            prefix = next(p for p in _SNAPSHOTS if identity.startswith(p))
            snapshots.add((prefix, identity[len(prefix) :].split(":", 1)[0]))
        elif identity.startswith(RESERVED_PREFIXES):
            # Productivity/chain records live in SDK side databases (cmhp/cmhc/
            # cmht/cmhv). Their physical purge is not implemented; refuse rather
            # than report a partial purge as complete.
            raise GovernanceError("governance_purge_kind_unsupported")
        else:
            native.add(identity)
    return native, receipts, snapshots


def _vacuum(path):
    with sqlite3.connect(path, timeout=30) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def _delete_in(db, table, column, values):
    values = sorted(values)
    for start in range(0, len(values), 500):
        chunk = values[start : start + 500]
        db.execute(f"DELETE FROM {table} WHERE {column} IN ({','.join('?' * len(chunk))})", chunk)


class Purger:
    def __init__(self, ingest, bridge, data_dir):
        self.ingest, self.bridge = ingest, bridge
        self.data_dir = Path(data_dir)

    def _sdk_paths(self, sdk):
        root = Path(sdk._config.vector_store.persist_directory).parent
        return {
            "chroma": Path(sdk._config.vector_store.persist_directory) / "chroma.sqlite3",
            "history": Path(sdk._config.history.db_path),
            "cache": root / "cache.sqlite3",
        }

    async def _sdk_delete(self, sdk, native):
        for identity in sorted(native):
            result = await _joined_thread(sdk.delete, identity)
            if result.get("error_code") or result.get("partial"):
                raise GovernanceError("governance_purge_incomplete")
            # deleted_count 0 without error: already absent from a retried run.

    def _sdk_side_tables(self, paths, native, requests):
        with sqlite3.connect(paths["chroma"], timeout=30) as db:
            # Chroma's write-ahead queue keeps full metadata until purge_log;
            # rows at or below max_seq_id are already applied to segments.
            _delete_in(db, "embeddings_queue", "id", native)
        with sqlite3.connect(paths["history"], timeout=30) as db:
            _delete_in(db, "memory_history", "memory_id", native)
        if paths["cache"].exists():
            with sqlite3.connect(paths["cache"], timeout=30) as db:
                _delete_in(db, "write_outcomes", "request_id", requests)

    def _hub_rows(self, receipts, snapshots):
        """Whole receipts in scope; only the in-scope snapshots of other receipts."""
        with self.ingest._database(writing=True) as db:
            requests = set()
            for receipt in sorted(receipts):
                row = db.execute(
                    "SELECT request_id FROM cmh_event_receipts WHERE receipt_id=?", (receipt,)
                ).fetchone()
                if row is not None:
                    requests.add(row[0])
            for table in _RECEIPT_TABLES:
                _delete_in(db, table, "receipt_id", receipts)
            for prefix, receipt in sorted(snapshots):
                if receipt in receipts:
                    continue
                table, _ = _SNAPSHOTS[prefix]
                if prefix == PROFILE:
                    _delete_in(db, "cmh_event_profile_outputs", "receipt_id", {receipt})
                _delete_in(db, table, "receipt_id", {receipt})
                db.execute(
                    "INSERT OR IGNORE INTO cmh_purged_snapshots VALUES (?,?)", (prefix, receipt)
                )
            for receipt in receipts:
                db.execute("INSERT OR IGNORE INTO cmh_purged_receipts VALUES (?)", (receipt,))
            return requests

    @staticmethod
    def initialize(db):
        db.execute("CREATE TABLE IF NOT EXISTS cmh_purged_receipts (receipt_id TEXT PRIMARY KEY)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS cmh_purged_snapshots (prefix TEXT NOT NULL, "
            "receipt_id TEXT NOT NULL, PRIMARY KEY(prefix, receipt_id))"
        )

    def _verify(self, files, needles):
        for path in files:
            for candidate in (path, path.with_name(path.name + "-wal")):
                if not candidate.exists():
                    continue
                data = candidate.read_bytes()
                if any(needle in data for needle in needles):
                    raise GovernanceError("governance_purge_residue")

    def _hub_present(self, receipts, snapshots):
        with self.ingest._database() as db:
            for receipt in receipts:
                if db.execute(
                    "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?", (receipt,)
                ).fetchone():
                    return True
            for prefix, receipt in snapshots:
                table, _ = _SNAPSHOTS[prefix]
                if db.execute(f"SELECT 1 FROM {table} WHERE receipt_id=?", (receipt,)).fetchone():
                    return True
        return False

    async def present(self, records):
        """Whether any purged record exists again, e.g. after a data restore."""
        native, receipts, snapshots = partition(records)
        if await _joined_thread(self._hub_present, receipts, snapshots):
            return True
        sdk = await self.bridge()._get_client()
        vector = sdk._vector_store

        async def lookup():
            for identity in sorted(native):
                if await vector.get_by_id(identity) is not None:
                    return True
            return False

        return await _joined_thread(lambda: sdk._loop_thread.run(lookup()))

    async def run(self, records, contents, requests_hint=()):
        native, receipts, snapshots = partition(records)
        sdk = await self.bridge()._get_client()
        paths = self._sdk_paths(sdk)
        await self._sdk_delete(sdk, native)
        requests = await _joined_thread(self._hub_rows, receipts, snapshots)
        requests |= set(requests_hint)
        await _joined_thread(self._sdk_side_tables, paths, native, requests)
        files = [
            paths["chroma"],
            paths["history"],
            paths["cache"],
            self.ingest.access.path,
            self.data_dir / "one-memory.sqlite3",
        ]
        for path in files:
            if path.exists():
                await _joined_thread(_vacuum, path)
        # Verify by content, not by ID: copies are what purge must remove.
        needles = [text.encode() for text in contents if len(text) >= 16]
        await _joined_thread(self._verify, [p for p in files if p.exists()], needles)
        return {"native": len(native), "receipts": len(receipts), "snapshots": len(snapshots)}
