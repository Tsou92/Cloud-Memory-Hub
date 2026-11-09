"""Bind independent governance storage to durable, monotonic external witnesses.

The access DB serializes cooperating writers. A journal commit may precede its
witness after process death; reopening verifies and advances that witness. No
cross-database atomicity or physical deletion is claimed. See ADR-0011.
"""

import hashlib
import hmac
import json
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock

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
    def __init__(
        self, access: DeviceAccessStore, journal: GovernanceJournal, *, read_only: bool = False
    ):
        self.access = access
        self.journal = journal
        self.read_only = read_only
        # Runtime validation belongs to this process, not a restored completed
        # row. The Executor clears this only after its complete recovery pass.
        self._runtime_recovery = Event()
        self._runtime_recovery.set()
        self._runtime_maintenance = 0
        self._runtime_lock = Lock()
        self._execution_binding = None
        if access.instance_id != journal.instance_id:
            raise GovernanceError("governance_binding_mismatch")
        self.verify()

    @staticmethod
    def _signature(journal, domain, value):
        return journal._mac("authority-" + domain, [value])

    @classmethod
    def activate(cls, access: DeviceAccessStore, journal: GovernanceJournal):
        """Explicit stopped-service migration; never reset a preexisting binding."""
        if access.read_only or journal.read_only:
            raise GovernanceError("governance_authority_read_only")
        if access.instance_id != journal.instance_id:
            raise GovernanceError("governance_binding_mismatch")
        if access.schema_version in {2, 3}:
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

    def _refresh(self, db, *, refresh_witness=True) -> dict[str, Checkpoint]:
        refresh_witness = refresh_witness and not self.read_only
        if self.access.schema_version not in {2, 3}:
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
        scopes = set(self.journal.scopes())
        if not refresh_witness and scopes - floors.keys():
            raise GovernanceError("governance_witness_missing")
        for scope in sorted(set(floors) | scopes):
            cursor = floors.get(scope)
            while True:
                page = self.journal.changes(scope, after=cursor, limit=1000)
                cursor = page["cursor"]
                if not page["has_more"]:
                    break
            heads[scope] = cursor
            if refresh_witness and cursor != floors.get(scope):
                payload = json.dumps(cursor.model_dump(), sort_keys=True, separators=(",", ":"))
                db.execute(
                    "INSERT INTO cmh_governance_floors VALUES (?,?,?) "
                    "ON CONFLICT(scope_id) DO UPDATE SET "
                    "checkpoint=excluded.checkpoint,signature=excluded.signature",
                    (scope, payload, self._signature(self.journal, "floor", [scope, payload])),
                )
        return heads

    def _run(self, intent=None):
        if intent is not None and self.read_only:
            raise GovernanceError("governance_authority_read_only")
        try:
            # Same order for every cooperating process: witness DB, then journal.
            with self.access._connection(writing=not self.read_only) as db:
                if self.read_only:
                    db.execute("PRAGMA query_only=ON")
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
        if self.read_only:
            raise GovernanceError("governance_authority_read_only")
        return self._run(intent)

    @property
    def execution(self):
        """One selected format; every state read still verifies its live binding."""
        from cloud_memory_hub.governance_execution_binding import select_execution_store

        # Selection does not initialize or migrate either store. Cooperating
        # format migration requires the stopped-service StorageLease, so a
        # changed capability in this process is a mismatch, not an upgrade.
        with self._runtime_lock:
            if self._execution_binding is None:
                self._execution_binding = select_execution_store(
                    self.journal, read_only=self.read_only
                )
            return self._execution_binding

    def _pending(self):
        self.verify()
        state = self.execution.read_state()
        planned = {
            identity: (operation.state, operation.intent)
            for identity, operation in state.operations.items()
        }
        pending = [
            state.operations[identity].intent
            for identity in state.pending
            if identity in state.operations
        ]
        pending.extend(state.unplanned)
        return pending, planned

    def require_ready(self) -> None:
        # Schema-2 file execution is still an independently tested component.
        # Do not let the legacy runtime accept its capability, including on an
        # empty main journal that would otherwise skip execution-store checks.
        if self.journal.execution_store_id() is not None:
            raise GovernanceError("governance_execution_runtime_unsupported")
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
        with self._runtime_lock:
            blocked = self.runtime_recovery_required or self._runtime_maintenance > 0
        if blocked:
            raise GovernanceError("governance_recovery_required")

    @property
    def runtime_recovery_required(self) -> bool:
        return self._runtime_recovery.is_set()

    def require_runtime_recovery(self) -> None:
        """Fence new SDK permits without changing stored authorization or reads."""
        self._runtime_recovery.set()

    @contextmanager
    def runtime_maintenance(self):
        """Entered only after workers drain; retained through all resource exits."""
        with self._runtime_lock:
            self._runtime_maintenance += 1
        try:
            yield
        finally:
            with self._runtime_lock:
                self._runtime_maintenance -= 1

    def finish_runtime_recovery(self) -> None:
        """Executor-only final check, after every recovery resource has exited."""
        self.require_ready()
        if self._pending()[0]:
            raise GovernanceError("governance_execution_pending")
        self._runtime_recovery.clear()


def open_authority(config, *, read_only: bool = False) -> GovernanceAuthority:
    require_separate_directory(config.governance_dir, config.config_dir, config.data_dir)
    access = DeviceAccessStore(
        config.config_dir / "remote-access.sqlite3", require_existing=True, read_only=read_only
    )
    if access.schema_version not in {2, 3}:
        raise GovernanceError("governance_migration_required")
    journal = GovernanceJournal(
        config.governance_dir / "journal.sqlite3",
        instance_id=access.instance_id,
        key=journal_key(config.encryption_key),
        read_only=read_only,
    )
    return GovernanceAuthority(access, journal, read_only=read_only)


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
    authority = GovernanceAuthority.activate(access, journal)
    from cloud_memory_hub.write_admissions import WriteAdmissions

    WriteAdmissions.activate(access, journal)
    return authority
