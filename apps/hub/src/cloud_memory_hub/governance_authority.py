"""Bind independent governance storage to durable, monotonic external witnesses.

The access DB serializes cooperating writers. A journal commit may precede its
witness after process death; reopening verifies and advances that witness. No
cross-database atomicity or physical deletion is claimed. See ADR-0011.
"""

import hashlib
import hmac
import json
from pathlib import Path

from cryptography.fernet import Fernet

from cloud_memory_hub.governance_journal import (
    Checkpoint,
    GovernanceError,
    GovernanceJournal,
    Intent,
)
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore


def journal_key(encryption_key: bytes) -> bytes:
    try:
        Fernet(encryption_key)
    except (ValueError, TypeError):
        raise GovernanceError("governance_key_invalid") from None
    return hmac.digest(encryption_key, b"cmh-governance-journal/v1", hashlib.sha256)


def require_separate_directory(directory: Path, *protected: Path) -> None:
    path = directory.resolve()
    for other in protected:
        other = other.resolve()
        if path == other or path in other.parents or other in path.parents:
            raise GovernanceError("governance_directory_not_separate")


class GovernanceAuthority:
    def __init__(self, access: DeviceAccessStore, journal: GovernanceJournal):
        self.access = access
        self.journal = journal
        if access.instance_id != journal.instance_id:
            raise GovernanceError("governance_binding_mismatch")
        self.verify()

    @staticmethod
    def _signature(journal, domain, value):
        return journal._mac("authority-" + domain, [value])

    @classmethod
    def activate(cls, access: DeviceAccessStore, journal: GovernanceJournal):
        """Explicit stopped-service migration; never reset a preexisting binding."""
        if access.instance_id != journal.instance_id:
            raise GovernanceError("governance_binding_mismatch")
        if access.schema_version == 2:
            return cls(access, journal)
        if journal.scopes():
            raise GovernanceError("governance_activation_requires_empty_journal")
        try:
            with access._connection(writing=True) as db:
                db.execute(
                    "CREATE TABLE cmh_governance_binding ("
                    "id INTEGER PRIMARY KEY CHECK(id=1), ledger_id TEXT NOT NULL, "
                    "signature TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE cmh_governance_floors (scope_id TEXT PRIMARY KEY, "
                    "checkpoint TEXT NOT NULL, signature TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO cmh_governance_binding VALUES (1,?,?)",
                    (journal.ledger_id, cls._signature(journal, "binding", 2)),
                )
                db.execute("PRAGMA user_version=2")
        except AccessError:
            raise GovernanceError("governance_authority_unavailable") from None
        access.schema_version = 2
        return cls(access, journal)

    def _refresh(self, db) -> dict[str, Checkpoint]:
        if self.access.schema_version != 2:
            raise GovernanceError("governance_migration_required")
        rows = db.execute("SELECT * FROM cmh_governance_binding").fetchall()
        if (
            len(rows) != 1
            or rows[0]["ledger_id"] != self.journal.ledger_id
            or not hmac.compare_digest(
                rows[0]["signature"], self._signature(self.journal, "binding", 2)
            )
        ):
            raise GovernanceError("governance_binding_mismatch")
        floors = {}
        for row in db.execute("SELECT * FROM cmh_governance_floors"):
            scope = self.journal._scope(row["scope_id"])
            expected = self._signature(self.journal, "floor", [scope, row["checkpoint"]])
            if not hmac.compare_digest(row["signature"], expected):
                raise GovernanceError("governance_witness_invalid")
            floors[scope] = Checkpoint.model_validate_json(row["checkpoint"])
        heads = {}
        for scope in sorted(set(floors) | set(self.journal.scopes())):
            cursor = floors.get(scope)
            while True:
                page = self.journal.changes(scope, after=cursor, limit=1000)
                cursor = page["cursor"]
                if not page["has_more"]:
                    break
            heads[scope] = cursor
            if cursor != floors.get(scope):
                payload = json.dumps(cursor.model_dump(), sort_keys=True, separators=(",", ":"))
                db.execute(
                    "INSERT INTO cmh_governance_floors VALUES (?,?,?) "
                    "ON CONFLICT(scope_id) DO UPDATE SET "
                    "checkpoint=excluded.checkpoint,signature=excluded.signature",
                    (scope, payload, self._signature(self.journal, "floor", [scope, payload])),
                )
        return heads

    def _run(self, intent=None):
        try:
            # Same order for every cooperating process: witness DB, then journal.
            with self.access._connection(writing=True) as db:
                heads = self._refresh(db)
                if intent is None:
                    return heads
                checkpoint = self.journal.append(intent)
                self._refresh(db)
                return checkpoint
        except AccessError:
            raise GovernanceError("governance_authority_unavailable") from None
        except (ValueError, TypeError, KeyError):
            raise GovernanceError("governance_witness_invalid") from None

    def verify(self) -> dict[str, Checkpoint]:
        return self._run()

    def append(self, intent: Intent) -> Checkpoint:
        """Internal primitive; the future user endpoint must authorize the intent."""
        return self._run(intent)

    def _pending(self):
        from cloud_memory_hub.governance_execution import ExecutionStore, pending_operations

        if not self.verify():
            return [], {}
        store = ExecutionStore(self.journal)
        return pending_operations(self.journal, store), store.operations()

    def require_ready(self) -> None:
        # Reads may proceed while a forget/purge is planned: its Hub suppression
        # rows are durable and enforced. An intent without a plan (foreign
        # writer, lost execution store) quarantines the instance (ADR-0032).
        pending, planned = self._pending()
        for intent in pending:
            if intent.operation_id not in planned:
                raise GovernanceError("governance_execution_pending")

    def require_writes_ready(self) -> None:
        """New SDK writes wait until every intent is fully executed."""
        self.require_ready()
        if self._pending()[0]:
            raise GovernanceError("governance_execution_pending")


def open_authority(config) -> GovernanceAuthority:
    require_separate_directory(config.governance_dir, config.config_dir, config.data_dir)
    access = DeviceAccessStore(config.config_dir / "remote-access.sqlite3", require_existing=True)
    if access.schema_version != 2:
        raise GovernanceError("governance_migration_required")
    journal = GovernanceJournal(
        config.governance_dir / "journal.sqlite3",
        instance_id=access.instance_id,
        key=journal_key(config.encryption_key),
    )
    return GovernanceAuthority(access, journal)


def activate_authority(config_dir: Path, governance_dir: Path, encryption_key: bytes, *, new=False):
    """Called only by explicit init/migrate, with all service writers stopped."""
    require_separate_directory(governance_dir, config_dir)
    identity = config_dir / "remote-access.sqlite3"
    if new and (identity.exists() or identity.is_symlink()):
        raise GovernanceError("governance_identity_already_exists")
    access = DeviceAccessStore(identity, require_existing=not new)
    key = journal_key(encryption_key)
    path = governance_dir / "journal.sqlite3"
    if access.schema_version == 1 and not path.exists() and not path.is_symlink():
        journal = GovernanceJournal.initialize(path, instance_id=access.instance_id, key=key)
    else:
        journal = GovernanceJournal(path, instance_id=access.instance_id, key=key)
    return GovernanceAuthority.activate(access, journal)
