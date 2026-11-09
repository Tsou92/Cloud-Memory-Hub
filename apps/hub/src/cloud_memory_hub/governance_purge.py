"""Physical purge (ADR-0032 slice 2): remove every stored copy, then verify.

Store list from the residue experiment in ADR-0032. Each step is idempotent so
an interrupted purge is simply re-run from the start.
"""

import sqlite3
from pathlib import Path

from hy_memory.data.cache_sqlite import SqliteCache
from one_memory.database_access import main_database
from one_memory.hy_memory_bridge import _joined_thread

from cloud_memory_hub.chat_graph import PREFIX as CHAT
from cloud_memory_hub.chroma_governance import (
    append_target_deletes,
    payload_rows_present,
    point_ids,
    purge_payload_rows,
    queue_topic,
    require_sequence,
)
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_request_owner import verify_request_owner
from cloud_memory_hub.memory_graph import PREFIX as MEMORY
from cloud_memory_hub.original_archive import validate_original
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


def _present_in(path, table, column, values):
    values = sorted(values)
    if not values or not path.exists():
        return False
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as db:
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            if db.execute(
                f"SELECT 1 FROM {table} WHERE {column} IN ({','.join('?' * len(chunk))}) LIMIT 1",
                chunk,
            ).fetchone():
                return True
    return False


class Purger:
    def __init__(self, ingest, bridge, data_dir):
        self.ingest, self.bridge = ingest, bridge
        self.data_dir = Path(data_dir)

    def _sdk_paths(self, sdk):
        if not isinstance(sdk._cache, SqliteCache):
            raise GovernanceError("governance_purge_cache_unsupported")
        return {
            "chroma": Path(sdk._config.vector_store.persist_directory) / "chroma.sqlite3",
            "chroma_topic": queue_topic(sdk._vector_store),
            "history": Path(sdk._config.history.db_path),
            "cache": Path(sdk._cache._db_path),
        }

    def _requests(self, receipts, snapshots, scope_id):
        """Core keeps job identities after payload reaping and Hub receipt purge."""
        requests = set()
        owners = receipts | {receipt for _, receipt in snapshots}
        queue = self.ingest._queue()
        with self.ingest._database() as db:
            with main_database(queue.database_path).read(row_factory=sqlite3.Row) as jobs:
                for receipt in sorted(owners):
                    job = jobs.execute(
                        "SELECT job_id,idempotency_key,source_key "
                        "FROM memory_write_jobs WHERE job_id=?",
                        (receipt,),
                    ).fetchone()
                    if job is None or not isinstance(job["source_key"], str):
                        raise GovernanceError("governance_purge_request_unproven")
                    source_id = job["source_key"][4:]
                    source = db.execute(
                        "SELECT source_id,device_id,scope_id FROM cmh_sources WHERE source_id=?",
                        (source_id,),
                    ).fetchone()
                    try:
                        owner = verify_request_owner(
                            job,
                            source,
                            instance_id=self.ingest.access.instance_id,
                            scope_id=scope_id,
                        )
                    except GovernanceError:
                        raise GovernanceError("governance_purge_request_unproven") from None
                    row = db.execute(
                        "SELECT * FROM cmh_event_receipts WHERE receipt_id=?", (receipt,)
                    ).fetchone()
                    if row is not None:
                        event = validate_original(row, self.ingest.access.instance_id)
                        if (
                            event.scope_id != owner.scope_id
                            or event.source_id != owner.source_id
                            or event.device_id != owner.device_id
                            or event.event_id != owner.event_id
                            or row["receipt_id"] != owner.job_id
                            or row["request_id"] != owner.request_id
                        ):
                            raise GovernanceError("governance_purge_request_unproven")
                    requests.add(owner.request_id)
        return requests

    async def _sdk_delete(self, sdk, native):
        for identity in sorted(native):
            result = await _joined_thread(sdk.delete, identity)
            if result.get("error_code") or result.get("partial"):
                raise GovernanceError("governance_purge_incomplete")
            # deleted_count 0 without error: already absent from a retried run.
        await _joined_thread(
            lambda: sdk._loop_thread.run(append_target_deletes(sdk._vector_store, native))
        )

    def _sdk_side_tables(self, paths, native, requests):
        purge_payload_rows(paths["chroma"], paths["chroma_topic"], point_ids(native))
        with sqlite3.connect(paths["history"], timeout=30) as db:
            _delete_in(db, "memory_history", "memory_id", native)
        if paths["cache"].exists():
            with sqlite3.connect(paths["cache"], timeout=30) as db:
                _delete_in(db, "write_outcomes", "request_id", requests)

    def _hub_rows(self, receipts, snapshots):
        """Whole receipts in scope; only the in-scope snapshots of other receipts."""
        with self.ingest._database(writing=True) as db:
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

    def _side_present(self, paths, native, requests):
        return any(
            _present_in(paths[store], table, column, values)
            for store, table, column, values in (
                ("cache", "write_outcomes", "request_id", requests),
                ("history", "memory_history", "memory_id", native),
            )
        ) or payload_rows_present(paths["chroma"], paths["chroma_topic"], point_ids(native))

    def _reap_terminal_payloads(self):
        # Reuse Core's existing terminal-only retention rule. This does not
        # cancel or resolve pending/unknown work (handled separately in ADR-0034).
        while self.ingest._queue().clear_terminal_payloads():
            pass

    async def present(self, records, *, scope_id):
        """Whether any purged record exists again, e.g. after a data restore."""
        native, receipts, snapshots = partition(records)
        if await _joined_thread(self._hub_present, receipts, snapshots):
            return True
        sdk = await self.bridge()._get_client()
        requests = await _joined_thread(self._requests, receipts, snapshots, scope_id)
        if await _joined_thread(self._side_present, self._sdk_paths(sdk), native, requests):
            return True
        vector = sdk._vector_store

        async def lookup():
            for identity in sorted(native):
                if await vector.get_by_id(identity) is not None:
                    return True
            return False

        return await _joined_thread(lambda: sdk._loop_thread.run(lookup()))

    async def run(self, records, contents, *, scope_id):
        native, receipts, snapshots = partition(records)
        sdk = await self.bridge()._get_client()
        paths = self._sdk_paths(sdk)
        await _joined_thread(require_sequence, paths["chroma"])
        requests = await _joined_thread(self._requests, receipts, snapshots, scope_id)
        await self._sdk_delete(sdk, native)
        await _joined_thread(self._sdk_side_tables, paths, native, requests)
        await _joined_thread(self._hub_rows, receipts, snapshots)
        await _joined_thread(self._reap_terminal_payloads)
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
