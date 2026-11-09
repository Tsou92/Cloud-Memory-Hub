"""ADR-0043 execution metadata; the actual journal remains deletion authority.

Every read validates the complete operation/suppression/cleanup relationship in
one SQLite snapshot. Cleanup progress is a retry cache, never a substitute for
checking the committed intent or rescanning the files after process startup.
"""

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from types import MappingProxyType
from uuid import uuid4

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    cleanup_digest,
    cleanup_from_dict,
    cleanup_to_dict,
    validate_cleanup_for_intent,
)
from cloud_memory_hub.governance_diagnostic_inventory import (
    DiagnosticInventory,
    DomainObservation,
    FileObservation,
)
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from cloud_memory_hub.remote_access import DeviceAccessStore

SCHEMA = 2
MAX_REVISION = 2**53 - 1
MAX_CONTRIBUTORS = 10_000
_OLD_COLUMNS = {
    "operations": ("operation_id", "scope_id", "intent", "state", "signature"),
    "suppressed": ("operation_id", "scope_id", "record_id", "kind", "signature"),
}


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _tampered():
    return GovernanceError("governance_execution_tampered")


def _hex(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _file_key(domain, key):
    if type(key) is not str:
        return False
    if domain == "pipeline" and key.startswith("default/") and key.endswith(".log"):
        suffix = key[len("default/") : -len(".log")]
    elif domain == "sdk" and key == "hy_memory.log":
        return True
    elif domain == "sdk" and key.startswith("hy_memory.log."):
        suffix = key[len("hy_memory.log.") :]
    else:
        return False
    try:
        return date.fromisoformat(suffix).isoformat() == suffix
    except ValueError:
        return False


def _file_observation(value):
    if type(value) is not FileObservation or not _file_key(value.domain, value.key):
        raise GovernanceError("governance_cleanup_manifest_invalid")
    integers = (
        value.device,
        value.inode,
        value.byte_length,
        value.mtime_ns,
        value.ctime_ns,
        value.record_count,
        value.kept_count,
        value.candidate_count,
        value.removed_count,
    )
    if (
        any(type(number) is not int or not 0 <= number <= 2**64 - 1 for number in integers)
        or value.byte_length > 32 * 1024 * 1024
        or value.record_count > 100_000
        or value.record_count != value.kept_count + value.candidate_count + value.removed_count
        or not _hex(value.sha256)
    ):
        raise GovernanceError("governance_cleanup_manifest_invalid")


def stage_name(operation_id, pass_id, index):
    canonical_uuid(operation_id)
    canonical_uuid(pass_id)
    if type(index) is not int or not 0 <= index < 256:
        raise GovernanceError("governance_cleanup_manifest_invalid")
    return f".cmh-cleanup-{operation_id}-{pass_id}-{index:03d}.tmp"


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    before: FileObservation
    retained_sha256: str
    retained_bytes: int
    stage_name: str
    state: str = "registered"

    def __post_init__(self):
        _file_observation(self.before)
        if (
            not _hex(self.retained_sha256)
            or type(self.retained_bytes) is not int
            or not 0 <= self.retained_bytes <= self.before.byte_length
            or self.retained_bytes == 0
            and self.retained_sha256 != hashlib.sha256(b"").hexdigest()
            or type(self.stage_name) is not str
            or type(self.state) is not str
            or self.state not in {"registered", "staged", "replaced", "verified", "abandoned"}
        ):
            raise GovernanceError("governance_cleanup_manifest_invalid")


@dataclass(frozen=True, slots=True, order=True)
class CleanupContributor:
    operation_id: str
    intent_digest: str
    cleanup_digest: str

    def __post_init__(self):
        try:
            canonical_uuid(self.operation_id)
            if not _hex(self.intent_digest) or not _hex(self.cleanup_digest):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_cleanup_contributor_invalid") from None


@dataclass(frozen=True, slots=True)
class CleanupManifest:
    operation_id: str
    pass_id: str
    artifacts: tuple[ArtifactEntry, ...]
    contributors: tuple[CleanupContributor, ...] | None = None

    def __post_init__(self):
        try:
            canonical_uuid(self.operation_id)
            canonical_uuid(self.pass_id)
            if self.contributors is not None:
                if (
                    type(self.contributors) is not tuple
                    or not 0 < len(self.contributors) <= MAX_CONTRIBUTORS
                    or any(type(item) is not CleanupContributor for item in self.contributors)
                ):
                    raise ValueError
                for item in self.contributors:
                    item.__post_init__()
                if (
                    self.contributors != tuple(sorted(set(self.contributors)))
                    or len({item.operation_id for item in self.contributors})
                    != len(self.contributors)
                    or self.operation_id not in {item.operation_id for item in self.contributors}
                ):
                    raise ValueError
            if type(self.artifacts) is not tuple or len(self.artifacts) > 256:
                raise ValueError
            keys = []
            for index, entry in enumerate(self.artifacts):
                if not isinstance(entry, ArtifactEntry):
                    raise ValueError
                entry.__post_init__()
                if entry.stage_name != stage_name(self.operation_id, self.pass_id, index):
                    raise ValueError
                keys.append((entry.before.domain, entry.before.key))
            if (
                keys != sorted(set(keys))
                or sum(entry.before.byte_length for entry in self.artifacts) > 128 * 1024 * 1024
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_cleanup_manifest_invalid") from None


def manifest_to_dict(value):
    if value is None:
        return None
    if not isinstance(value, CleanupManifest):
        raise GovernanceError("governance_cleanup_manifest_invalid")
    value.__post_init__()
    result = {
        "version": 1 if value.contributors is None else 2,
        "operation_id": value.operation_id,
        "pass_id": value.pass_id,
        "artifacts": [asdict(item) for item in value.artifacts],
    }
    if value.contributors is not None:
        result["contributors"] = [asdict(item) for item in value.contributors]
    return result


def manifest_from_dict(value):
    if value is None:
        return None
    try:
        if (
            type(value) is not dict
            or type(value["version"]) is not int
            or value["version"] not in {1, 2}
            or set(value)
            != {"version", "operation_id", "pass_id", "artifacts"}
            | ({"contributors"} if value["version"] == 2 else set())
            or type(value["artifacts"]) is not list
            or len(value["artifacts"]) > 256
        ):
            raise ValueError
        artifacts = []
        for item in value["artifacts"]:
            if type(item) is not dict or set(item) != {
                "before",
                "retained_sha256",
                "retained_bytes",
                "stage_name",
                "state",
            }:
                raise ValueError
            artifacts.append(ArtifactEntry(**{**item, "before": FileObservation(**item["before"])}))
        contributors = None
        if value["version"] == 2:
            raw = value["contributors"]
            if type(raw) is not list or not 0 < len(raw) <= MAX_CONTRIBUTORS:
                raise ValueError
            contributors = tuple(CleanupContributor(**item) for item in raw)
        return CleanupManifest(
            value["operation_id"], value["pass_id"], tuple(artifacts), contributors
        )
    except (ValueError, TypeError, KeyError):
        raise GovernanceError("governance_cleanup_manifest_invalid") from None


def manifest_digest(value):
    return _digest(manifest_to_dict(value))


@dataclass(frozen=True, slots=True)
class ArtifactAbsence:
    """Engine observation under exclusion, not a caller-supplied authorization."""

    operation_id: str
    pass_id: str | None
    manifest_digest: str
    absent: tuple[tuple[str, str], ...]

    def __post_init__(self):
        try:
            canonical_uuid(self.operation_id)
            if self.pass_id is not None:
                canonical_uuid(self.pass_id)
            if (
                not _hex(self.manifest_digest)
                or type(self.absent) is not tuple
                or len(self.absent) > 256
                or any(
                    type(item) is not tuple
                    or len(item) != 2
                    or item[0] not in {"pipeline", "sdk"}
                    or type(item[1]) is not str
                    or "/" in item[1]
                    or len(item[1]) > 128
                    for item in self.absent
                )
                or self.absent != tuple(sorted(set(self.absent)))
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise GovernanceError("governance_cleanup_verification_invalid") from None


@dataclass(frozen=True, slots=True)
class CleanupVerification:
    inventory: DiagnosticInventory
    artifacts: ArtifactAbsence


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """Digest of engine observations, not an anti-rollback completion witness."""

    inventory_sha256: str
    artifact_absence_sha256: str

    def __post_init__(self):
        if not _hex(self.inventory_sha256) or not _hex(self.artifact_absence_sha256):
            raise GovernanceError("governance_cleanup_verification_invalid")


def _evidence_to_dict(value):
    if value is None:
        return None
    if type(value) is not VerificationEvidence:
        raise GovernanceError("governance_cleanup_verification_invalid")
    value.__post_init__()
    return {"version": 1, **asdict(value)}


def _evidence_from_dict(value):
    if value is None:
        return None
    if (
        type(value) is not dict
        or set(value) != {"version", "inventory_sha256", "artifact_absence_sha256"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise GovernanceError("governance_cleanup_verification_invalid")
    return VerificationEvidence(value["inventory_sha256"], value["artifact_absence_sha256"])


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    operation_id: str
    scope_id: str
    authority: CleanupAuthority
    state: str
    pass_id: str | None
    manifest: CleanupManifest | None
    revision: int
    signature: str
    orphan_only: bool = False
    verification: VerificationEvidence | None = None


@dataclass(frozen=True, slots=True)
class StoreBinding:
    store_id: str
    instance_id: str
    ledger_id: str
    baseline_root: str


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    state: str
    intent: Intent
    records: Mapping[str, str]
    cleanup: CleanupReceipt | None = None

    def __post_init__(self):
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    binding: StoreBinding
    operations: Mapping[str, OperationSnapshot]

    def __post_init__(self):
        object.__setattr__(self, "operations", MappingProxyType(dict(self.operations)))


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    snapshot: ExecutionSnapshot
    committed: tuple[str, ...]
    orphans: tuple[str, ...]
    unplanned: tuple[Intent, ...]
    committed_digests: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(
            self, "committed_digests", MappingProxyType(dict(self.committed_digests))
        )

    @property
    def purges_to_rescan(self):
        """All committed purges, including completed operations with verified cache."""
        return tuple(
            key for key in self.committed if self.snapshot.operations[key].intent.action == "purge"
        )


@dataclass(frozen=True, slots=True)
class LegacySnapshot:
    source_digest: str
    operations: Mapping[str, OperationSnapshot]
    committed: tuple[str, ...]
    orphans: tuple[str, ...]
    journal_digest: str
    unplanned: tuple[Intent, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "operations", MappingProxyType(dict(self.operations)))


class ExecutionStore:
    """Explicit schema-2 storage; construction neither creates nor migrates."""

    def __init__(self, journal, *, read_only=False):
        if type(read_only) is not bool:
            raise GovernanceError("governance_execution_mode_invalid")
        self.journal = journal
        self.path = journal.path.with_name("execution.sqlite3")
        self.read_only = read_only

    def _writable(self):
        if self.read_only:
            raise GovernanceError("governance_execution_read_only")

    def _sign(self, values):
        # Retain the original operations/suppressed MAC byte-for-byte.
        return self.journal._mac("execution-v1", values)

    @contextmanager
    def _connection(self, *, writing=False, version=SCHEMA):
        if writing:
            self._writable()
        try:
            DeviceAccessStore._check_private(self.path.parent, directory=True)
            DeviceAccessStore._check_private(self.path)
            mode = "rw" if writing else "ro"
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                if writing:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("PRAGMA foreign_keys=ON")
                else:
                    db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
                if db.execute("PRAGMA user_version").fetchone()[0] != version:
                    raise GovernanceError("governance_execution_version_unsupported")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except GovernanceError:
            raise
        except Exception:
            raise GovernanceError("governance_execution_unavailable") from None

    def _check_old(self, row, table):
        columns = _OLD_COLUMNS[table]
        if tuple(row.keys()) != columns:
            raise _tampered()
        values = [row[key] for key in columns[:-1]]
        if not isinstance(row["signature"], str) or not hmac.compare_digest(
            row["signature"], self._sign([table, *values])
        ):
            raise _tampered()

    def _old_snapshot(self, db):
        operation_rows = db.execute("SELECT * FROM operations ORDER BY operation_id").fetchall()
        record_rows = db.execute(
            "SELECT * FROM suppressed ORDER BY operation_id,record_id"
        ).fetchall()
        result, records = {}, {}
        try:
            for row in operation_rows:
                self._check_old(row, "operations")
                intent = Intent.model_validate_json(row["intent"])
                if (
                    row["operation_id"] != intent.operation_id
                    or row["scope_id"] != intent.scope_id
                    or row["intent"] != intent.model_dump_json()
                    or row["state"] not in {"planned", "completed"}
                    or intent.operation_id in result
                ):
                    raise _tampered()
                result[intent.operation_id] = (row["state"], intent)
                records[intent.operation_id] = {}
            for row in record_rows:
                self._check_old(row, "suppressed")
                known = result.get(row["operation_id"])
                if (
                    known is None
                    or row["scope_id"] != known[1].scope_id
                    or row["kind"] not in {"raw", "memory"}
                    or not isinstance(row["record_id"], str)
                    or not row["record_id"]
                    or row["record_id"] in records[row["operation_id"]]
                ):
                    raise _tampered()
                records[row["operation_id"]][row["record_id"]] = row["kind"]
            for operation_id, (_, intent) in result.items():
                actual = {
                    self.journal.target(intent.scope_id, kind, identity)
                    for identity, kind in records[operation_id].items()
                }
                expected = {target for target in intent.targets if target.kind in {"raw", "memory"}}
                if actual != expected:
                    raise _tampered()
        except (ValueError, TypeError, KeyError):
            raise _tampered() from None
        operations = {
            operation_id: OperationSnapshot(state, intent, records[operation_id])
            for operation_id, (state, intent) in result.items()
        }
        rows = [[list(row) for row in operation_rows], [list(row) for row in record_rows]]
        return operations, rows

    def _journal_snapshot(self, db):
        self.journal._verify(db)
        entries = {}
        for row in db.execute("SELECT * FROM intents ORDER BY scope_id,sequence"):
            intent = self.journal._decode(row)
            entries[intent.operation_id] = (intent, row["digest"])
        return entries

    @staticmethod
    def _classify(operations, entries):
        committed, orphans, unplanned = [], [], []
        for operation_id, operation in operations.items():
            actual = entries.get(operation_id)
            if actual is None:
                if operation.state != "planned":
                    raise _tampered()
                orphans.append(operation_id)
            elif actual[0] != operation.intent:
                raise _tampered()
            else:
                committed.append(operation_id)
        for operation_id, (intent, _) in entries.items():
            if operation_id not in operations:
                unplanned.append(intent)
        return tuple(sorted(committed)), tuple(sorted(orphans)), tuple(unplanned)

    def legacy_recovery_snapshot(self, *, allow_missing=False):
        """Read v1 and its actual chain together, including unplanned intents.

        A missing store is only an observation, never initialization. Migration
        still uses inspect_legacy, which requires a present, complete old store.
        """
        if type(allow_missing) is not bool:
            raise GovernanceError("governance_execution_mode_invalid")
        # This order also governs all mutations: journal, then execution.
        with self.journal._connection() as journal_db:
            entries = self._journal_snapshot(journal_db)
            if self.journal._capabilities(journal_db).get("execution_store_v2") is not None:
                raise GovernanceError("governance_execution_binding_mismatch")
            if allow_missing and not self.path.exists() and not self.path.is_symlink():
                operations, rows = {}, [[], []]
            else:
                with self._connection(version=1) as db:
                    operations, rows = self._old_snapshot(db)
            committed, orphans, unplanned = self._classify(operations, entries)
            return LegacySnapshot(
                _digest(rows),
                operations,
                committed,
                orphans,
                _digest([[key, value[0].model_dump(), value[1]] for key, value in entries.items()]),
                unplanned,
            )

    def inspect_legacy(self):
        """Pure migration inspection; unplanned real intents cannot be migrated."""
        snapshot = self.legacy_recovery_snapshot()
        if snapshot.unplanned:
            raise GovernanceError("governance_execution_pending")
        return snapshot

    @staticmethod
    def _entries_digest(entries):
        return _digest([[key, value[0].model_dump(), value[1]] for key, value in entries.items()])

    @staticmethod
    def _baseline(source_digest, journal_digest, cleanups, orphans):
        return _digest(
            {
                "version": 1,
                "source_digest": source_digest,
                "journal_digest": journal_digest,
                "cleanup": [
                    [key, cleanup_to_dict(value), key in orphans]
                    for key, value in sorted(cleanups.items())
                ],
            }
        )

    def _validate_migration(self, operations, cleanups, orphans):
        purges = {key for key, value in operations.items() if value.intent.action == "purge"}
        if not isinstance(cleanups, Mapping) or set(cleanups) != purges:
            raise GovernanceError("governance_execution_migration_invalid")
        for key in purges:
            authority = cleanups[key]
            cleanup_to_dict(authority)
            if authority.mode != "legacy" or authority.grants:
                raise GovernanceError("governance_execution_migration_invalid")
            if key in orphans:
                if authority.selectors:
                    raise GovernanceError("governance_execution_migration_invalid")
            else:
                operation = operations[key]
                validate_cleanup_for_intent(
                    self.journal, operation.intent, operation.records, authority
                )

    def migrate_legacy(self, snapshot, cleanups):
        """Explicit stopped-writer migration; all proofs precede schema mutation.

        The caller proves original selectors without apply/cancellation and
        checks external witnesses. This method rechecks the exact old rows and
        actual journal, commits execution alone, and never activates capability.
        """
        self._writable()
        if not isinstance(snapshot, LegacySnapshot):
            raise GovernanceError("governance_execution_migration_invalid")
        self._validate_migration(snapshot.operations, cleanups, snapshot.orphans)
        with (
            self.journal._connection() as journal_db,
            self._connection(writing=True, version=1) as db,
        ):
            entries = self._journal_snapshot(journal_db)
            if self.journal._capabilities(journal_db).get("execution_store_v2") is not None:
                raise GovernanceError("governance_execution_binding_exists")
            operations, rows = self._old_snapshot(db)
            committed, orphans, unplanned = self._classify(operations, entries)
            source_digest, journal_digest = _digest(rows), self._entries_digest(entries)
            if (
                unplanned
                or source_digest != snapshot.source_digest
                or journal_digest != snapshot.journal_digest
                or committed != snapshot.committed
                or orphans != snapshot.orphans
                or operations != snapshot.operations
            ):
                raise GovernanceError("governance_execution_migration_changed")
            self._validate_migration(operations, cleanups, orphans)
            binding = StoreBinding(
                str(uuid4()),
                self.journal.instance_id,
                self.journal.ledger_id,
                self._baseline(source_digest, journal_digest, cleanups, orphans),
            )
            self._create_metadata(db, binding)
            for key, authority in sorted(cleanups.items()):
                self._insert_cleanup(
                    db, binding, operations[key].intent, authority, orphan_only=key in orphans
                )
            # Read back the complete schema before the only commit boundary.
            migrated = self._snapshot(db)
            self._recovery(migrated, entries)
            db.execute("PRAGMA user_version=2")
        return binding

    def activation_binding(self):
        """Pure verification for retrying the execution-commit/capability gap."""
        with self.journal._connection() as journal_db, self._connection() as db:
            entries = self._journal_snapshot(journal_db)
            snapshot = self._snapshot(db)
            recovery = self._recovery(snapshot, entries)
            current = self.journal._capabilities(journal_db).get("execution_store_v2")
            if current is not None:
                if current != snapshot.binding.store_id:
                    raise GovernanceError("governance_execution_binding_mismatch")
                return snapshot.binding
            if recovery.unplanned:
                raise GovernanceError("governance_execution_pending")
            if (
                not snapshot.operations
                and not entries
                and snapshot.binding.baseline_root == _digest([])
            ):
                return snapshot.binding
            cleanups = {}
            for key, operation in snapshot.operations.items():
                cleanup = operation.cleanup
                if cleanup is not None:
                    if (
                        cleanup.authority.mode != "legacy"
                        or cleanup.state != "pending"
                        or cleanup.pass_id is not None
                        or cleanup.revision != 0
                        or cleanup.orphan_only != (key in recovery.orphans)
                    ):
                        raise GovernanceError("governance_execution_migration_changed")
                    cleanups[key] = cleanup.authority
            _, rows = self._old_snapshot(db)
            expected = self._baseline(
                _digest(rows), self._entries_digest(entries), cleanups, recovery.orphans
            )
            if expected != snapshot.binding.baseline_root:
                raise GovernanceError("governance_execution_migration_changed")
            return snapshot.binding

    def _binding(self, db):
        rows = db.execute("SELECT * FROM binding").fetchall()
        if len(rows) != 1:
            raise _tampered()
        row = rows[0]
        expected_columns = (
            "id",
            "store_id",
            "instance_id",
            "ledger_id",
            "schema_version",
            "baseline_root",
            "signature",
        )
        if tuple(row.keys()) != expected_columns:
            raise _tampered()
        values = [row[key] for key in expected_columns[:-1]]
        try:
            if (
                row["id"] != 1
                or row["schema_version"] != SCHEMA
                or row["instance_id"] != self.journal.instance_id
                or row["ledger_id"] != self.journal.ledger_id
                or canonical_uuid(row["store_id"]) != row["store_id"]
                or len(row["baseline_root"]) != 64
                or any(c not in "0123456789abcdef" for c in row["baseline_root"])
                or not hmac.compare_digest(
                    row["signature"], self.journal._mac("execution-binding-v2", values)
                )
            ):
                raise _tampered()
        except (ValueError, TypeError):
            raise _tampered() from None
        return StoreBinding(
            row["store_id"], row["instance_id"], row["ledger_id"], row["baseline_root"]
        )

    def _create_metadata(self, db, binding):
        db.execute(
            "CREATE TABLE binding (id INTEGER PRIMARY KEY CHECK(id=1), "
            "store_id TEXT NOT NULL, instance_id TEXT NOT NULL, ledger_id TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL CHECK(schema_version=2), "
            "baseline_root TEXT NOT NULL, signature TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE cleanup (operation_id TEXT PRIMARY KEY "
            "REFERENCES operations(operation_id), scope_id TEXT NOT NULL, "
            "immutable_json TEXT NOT NULL, immutable_signature TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN ('pending','verified')), pass_id TEXT, "
            "manifest_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0), "
            "verification_json TEXT NOT NULL, progress_signature TEXT NOT NULL)"
        )
        values = [
            1,
            binding.store_id,
            binding.instance_id,
            binding.ledger_id,
            SCHEMA,
            binding.baseline_root,
        ]
        db.execute(
            "INSERT INTO binding VALUES (?,?,?,?,?,?,?)",
            (*values, self.journal._mac("execution-binding-v2", values)),
        )

    def initialize_empty(self):
        """Explicit initialization before the separately committed capability."""
        self._writable()
        if self.journal.execution_store_id() is not None:
            raise GovernanceError("governance_execution_binding_exists")
        with self.journal._connection() as journal_db:
            if self._journal_snapshot(journal_db):
                raise GovernanceError("governance_execution_migration_required")
            binding = StoreBinding(
                str(uuid4()), self.journal.instance_id, self.journal.ledger_id, _digest([])
            )
            try:
                DeviceAccessStore._check_private(self.path.parent, directory=True)
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
                )
                os.close(descriptor)
                with self._connection(writing=True, version=0) as db:
                    db.execute(
                        "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, "
                        "scope_id TEXT NOT NULL, intent TEXT NOT NULL, "
                        "state TEXT NOT NULL CHECK(state IN ('planned','completed')), "
                        "signature TEXT NOT NULL)"
                    )
                    db.execute(
                        "CREATE TABLE suppressed (operation_id TEXT NOT NULL "
                        "REFERENCES operations(operation_id), scope_id TEXT NOT NULL, "
                        "record_id TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL, "
                        "PRIMARY KEY(operation_id,record_id))"
                    )
                    db.execute("CREATE INDEX suppressed_scope ON suppressed(scope_id,record_id)")
                    self._create_metadata(db, binding)
                    db.execute("PRAGMA user_version=2")
                descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except FileExistsError:
                raise GovernanceError("governance_execution_already_exists") from None
            except GovernanceError:
                raise
            except Exception:
                raise GovernanceError("governance_execution_unavailable") from None
        return binding

    def require_capability(self):
        binding = self.snapshot().binding
        if self.journal.execution_store_id() != binding.store_id:
            raise GovernanceError("governance_execution_binding_mismatch")
        return binding

    def _immutable(self, binding, intent, authority, *, orphan_only=False):
        payload = _canonical(
            {"authority": cleanup_to_dict(authority), "orphan_only": orphan_only, "version": 1}
        )
        signature = self.journal._mac(
            "execution-cleanup-authority-v2",
            [
                binding.store_id,
                intent.operation_id,
                intent.scope_id,
                intent.model_dump_json(),
                payload,
            ],
        )
        return payload, signature

    def _progress_signature(self, binding, operation_id, immutable_signature, values):
        return self.journal._mac(
            "execution-cleanup-progress-v2",
            [binding.store_id, operation_id, immutable_signature, *values],
        )

    @staticmethod
    def _json(payload):
        try:
            if type(payload) is not str or len(payload.encode()) > 16 * 1024 * 1024:
                raise ValueError
            value = json.loads(payload)
            # This also rejects duplicate keys, NaN, alternate encodings and whitespace.
            if _canonical(value) != payload:
                raise ValueError
            return value
        except (ValueError, TypeError, RecursionError):
            raise _tampered() from None

    def _read_cleanup(self, binding, operation, row):
        expected_columns = (
            "operation_id",
            "scope_id",
            "immutable_json",
            "immutable_signature",
            "state",
            "pass_id",
            "manifest_json",
            "revision",
            "verification_json",
            "progress_signature",
        )
        if tuple(row.keys()) != expected_columns:
            raise _tampered()
        intent = operation.intent
        immutable = self._json(row["immutable_json"])
        if (
            type(immutable) is not dict
            or set(immutable) != {"authority", "orphan_only", "version"}
            or type(immutable["version"]) is not int
            or immutable["version"] != 1
            or type(immutable["orphan_only"]) is not bool
            or row["operation_id"] != intent.operation_id
            or row["scope_id"] != intent.scope_id
        ):
            raise _tampered()
        authority = cleanup_from_dict(immutable["authority"])
        orphan_only = immutable["orphan_only"]
        if orphan_only:
            if authority.mode != "legacy" or authority.selectors or authority.grants:
                raise _tampered()
        else:
            validate_cleanup_for_intent(self.journal, intent, operation.records, authority)
        payload, signature = self._immutable(binding, intent, authority, orphan_only=orphan_only)
        if payload != row["immutable_json"] or not hmac.compare_digest(
            signature, row["immutable_signature"]
        ):
            raise _tampered()
        manifest = manifest_from_dict(self._json(row["manifest_json"]))
        verification = _evidence_from_dict(self._json(row["verification_json"]))
        revision = row["revision"]
        if (
            type(revision) is not int
            or not 0 <= revision <= MAX_REVISION
            or row["state"] not in {"pending", "verified"}
            or row["pass_id"] is None
            and (manifest is not None or revision != 0 or row["state"] != "pending")
            or row["pass_id"] is not None
            and (
                manifest is None
                or manifest.operation_id != intent.operation_id
                or manifest.pass_id != row["pass_id"]
                or revision == 0
            )
            or orphan_only
            and row["pass_id"] is not None
            or row["state"] == "pending"
            and verification is not None
            or row["state"] == "verified"
            and verification is None
            or row["state"] == "verified"
            and any(item.state != "verified" for item in manifest.artifacts)
        ):
            raise _tampered()
        values = [
            row["state"],
            row["pass_id"],
            row["manifest_json"],
            revision,
            row["verification_json"],
        ]
        expected = self._progress_signature(binding, intent.operation_id, signature, values)
        if not hmac.compare_digest(row["progress_signature"], expected):
            raise _tampered()
        return CleanupReceipt(
            intent.operation_id,
            intent.scope_id,
            authority,
            row["state"],
            row["pass_id"],
            manifest,
            revision,
            row["progress_signature"],
            orphan_only,
            verification,
        )

    def _snapshot(self, db):
        binding = self._binding(db)
        operations, _ = self._old_snapshot(db)
        cleanups = {}
        for row in db.execute("SELECT * FROM cleanup ORDER BY operation_id"):
            operation = operations.get(row["operation_id"])
            if (
                operation is None
                or operation.intent.action != "purge"
                or row["operation_id"] in cleanups
            ):
                raise _tampered()
            cleanups[row["operation_id"]] = self._read_cleanup(binding, operation, row)
        expected = {key for key, op in operations.items() if op.intent.action == "purge"}
        if set(cleanups) != expected:
            raise _tampered()
        result = {
            key: OperationSnapshot(op.state, op.intent, op.records, cleanups.get(key))
            for key, op in operations.items()
        }
        for operation in result.values():
            cleanup = operation.cleanup
            if cleanup is None or cleanup.authority.mode != "repair":
                continue
            parent = result.get(cleanup.authority.parent.operation_id)
            if (
                parent is None
                or parent.intent.action != "purge"
                or parent.intent.scope_id != operation.intent.scope_id
                or parent.cleanup.authority.mode == "repair"
                or parent.cleanup.orphan_only
                or cleanup.authority.selectors != parent.cleanup.authority.selectors
            ):
                raise GovernanceError("governance_cleanup_parent_invalid")
        return ExecutionSnapshot(binding, result)

    def snapshot(self):
        with self._connection() as db:
            return self._snapshot(db)

    def _recovery(self, snapshot, entries):
        committed, orphans, unplanned = self._classify(snapshot.operations, entries)
        for key in committed:
            cleanup = snapshot.operations[key].cleanup
            if cleanup is not None and cleanup.orphan_only:
                raise _tampered()
        for operation in snapshot.operations.values():
            cleanup = operation.cleanup
            if cleanup is None:
                continue
            if operation.intent.operation_id in orphans and cleanup.pass_id is not None:
                raise _tampered()
            if cleanup.authority.mode == "repair":
                parent = cleanup.authority.parent
                entry = entries.get(parent.operation_id)
                if (
                    entry is None
                    or entry[1] != parent.intent_digest
                    or entry[0] != snapshot.operations[parent.operation_id].intent
                    or entry[0].action != "purge"
                    or entry[0].scope_id != operation.intent.scope_id
                    or parent.ledger_id != self.journal.ledger_id
                ):
                    raise GovernanceError("governance_cleanup_parent_invalid")
        recovery = RecoverySnapshot(
            snapshot,
            committed,
            orphans,
            unplanned,
            {operation_id: entries[operation_id][1] for operation_id in committed},
        )
        # A batch stores the same frozen references on several receipts. Reuse
        # validation only within this real journal/execution snapshot; nothing
        # survives this call or skips a later main-chain check.
        resolved_owners = {}
        for operation in snapshot.operations.values():
            if operation.cleanup is None or operation.cleanup.manifest is None:
                continue
            manifest = operation.cleanup.manifest
            references = manifest.contributors
            if references is None:
                self._resolve_manifest(recovery, manifest)
                continue
            if references not in resolved_owners:
                resolved = self._resolve_manifest(recovery, manifest)
                resolved_owners[references] = frozenset(
                    reference.operation_id for reference in resolved.contributors
                )
            if manifest.operation_id not in resolved_owners[references]:
                raise GovernanceError("governance_cleanup_contributor_invalid")
        return recovery

    @staticmethod
    def _resolve_manifest(recovery, manifest):
        # The pure resolver imports these immutable storage types; delay this
        # import so both layers reuse the same authority checks without a cycle.
        from cloud_memory_hub.governance_diagnostic_recovery import resolve_cleanup_authority

        contributors = manifest.contributors
        if contributors is None:
            operation = recovery.snapshot.operations[manifest.operation_id]
            digest = recovery.committed_digests.get(manifest.operation_id)
            if digest is None:
                raise GovernanceError("governance_operation_uncommitted")
            contributors = (
                CleanupContributor(
                    manifest.operation_id, digest, cleanup_digest(operation.cleanup.authority)
                ),
            )
        return resolve_cleanup_authority(
            recovery, contributors, owner_operation_id=manifest.operation_id
        )

    def recovery_snapshot(self):
        """Bind stored plans to real entries; external floors are checked by authority."""
        with self.journal._connection() as journal_db, self._connection() as db:
            entries = self._journal_snapshot(journal_db)
            snapshot = self._snapshot(db)
            if self.journal._capabilities(journal_db).get("execution_store_v2") != (
                snapshot.binding.store_id
            ):
                raise GovernanceError("governance_execution_binding_mismatch")
            return self._recovery(snapshot, entries)

    @contextmanager
    def _mutation(self):
        self._writable()
        with self.journal._connection() as journal_db, self._connection(writing=True) as db:
            entries = self._journal_snapshot(journal_db)
            snapshot = self._snapshot(db)
            if self.journal._capabilities(journal_db).get("execution_store_v2") != (
                snapshot.binding.store_id
            ):
                raise GovernanceError("governance_execution_binding_mismatch")
            recovery = self._recovery(snapshot, entries)
            yield db, recovery, entries

    def _insert_cleanup(self, db, binding, intent, authority, *, orphan_only=False):
        payload, immutable_signature = self._immutable(
            binding, intent, authority, orphan_only=orphan_only
        )
        values = ["pending", None, "null", 0, "null"]
        signature = self._progress_signature(
            binding, intent.operation_id, immutable_signature, values
        )
        db.execute(
            "INSERT INTO cleanup VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                intent.operation_id,
                intent.scope_id,
                payload,
                immutable_signature,
                *values,
                signature,
            ),
        )

    def plan(self, intent, records, *, cleanup=None):
        intent = Intent.model_validate(intent)
        if not isinstance(records, Mapping):
            raise GovernanceError("governance_operation_conflict")
        records = dict(records)
        if intent.action == "purge":
            if not isinstance(cleanup, CleanupAuthority) or cleanup.mode == "legacy":
                raise GovernanceError("governance_cleanup_confirmation_required")
            validate_cleanup_for_intent(self.journal, intent, records, cleanup)
        elif cleanup is not None:
            raise GovernanceError("governance_cleanup_confirmation_invalid")
        with self._mutation() as (db, recovery, entries):
            snapshot = recovery.snapshot
            existing = snapshot.operations.get(intent.operation_id)
            if existing is not None:
                if (
                    existing.intent != intent
                    or dict(existing.records) != records
                    or (existing.cleanup.authority if existing.cleanup is not None else None)
                    != cleanup
                ):
                    raise GovernanceError("governance_operation_conflict")
                return existing
            if intent.operation_id in entries:
                raise GovernanceError("governance_operation_unplanned")
            values = [intent.operation_id, intent.scope_id, intent.model_dump_json(), "planned"]
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (*values, self._sign(["operations", *values])),
            )
            for identity, kind in sorted(records.items()):
                values = [intent.operation_id, intent.scope_id, identity, kind]
                db.execute(
                    "INSERT INTO suppressed VALUES (?,?,?,?,?)",
                    (*values, self._sign(["suppressed", *values])),
                )
            if cleanup is not None:
                self._insert_cleanup(db, snapshot.binding, intent, cleanup)
            updated = self._snapshot(db)
            self._recovery(updated, entries)
            return updated.operations[intent.operation_id]

    def cleanup(self, operation_id):
        operation = self.snapshot().operations.get(operation_id)
        if operation is None or operation.cleanup is None:
            raise GovernanceError("governance_cleanup_missing")
        return operation.cleanup

    @staticmethod
    def _current(recovery, expected):
        if not isinstance(expected, CleanupReceipt):
            raise GovernanceError("governance_cleanup_conflict")
        operation = recovery.snapshot.operations.get(expected.operation_id)
        if operation is None or operation.cleanup != expected:
            raise GovernanceError("governance_cleanup_conflict")
        if expected.operation_id not in recovery.committed or expected.orphan_only:
            raise GovernanceError("governance_operation_uncommitted")
        return operation

    @staticmethod
    def _absence(expected, observed):
        if not isinstance(observed, ArtifactAbsence):
            raise GovernanceError("governance_cleanup_artifact_unproven")
        observed.__post_init__()
        names = (
            ()
            if expected.manifest is None
            else tuple(
                sorted(
                    (item.before.domain, item.stage_name) for item in expected.manifest.artifacts
                )
            )
        )
        if (
            observed.operation_id != expected.operation_id
            or observed.pass_id != expected.pass_id
            or observed.manifest_digest != manifest_digest(expected.manifest)
            or observed.absent != names
        ):
            raise GovernanceError("governance_cleanup_artifact_unproven")

    def _save_progress(self, db, snapshot, expected, *, state, manifest, verification=None):
        if expected.revision == MAX_REVISION:
            raise GovernanceError("governance_cleanup_revision_exhausted")
        manifest_json = _canonical(manifest_to_dict(manifest))
        intent = snapshot.operations[expected.operation_id].intent
        _, immutable_signature = self._immutable(
            snapshot.binding, intent, expected.authority, orphan_only=expected.orphan_only
        )
        values = [
            state,
            manifest.pass_id,
            manifest_json,
            expected.revision + 1,
            _canonical(_evidence_to_dict(verification)),
        ]
        signature = self._progress_signature(
            snapshot.binding, expected.operation_id, immutable_signature, values
        )
        changed = db.execute(
            "UPDATE cleanup SET state=?,pass_id=?,manifest_json=?,revision=?,"
            "verification_json=?,progress_signature=? "
            "WHERE operation_id=? AND revision=? AND progress_signature=?",
            (*values, signature, expected.operation_id, expected.revision, expected.signature),
        ).rowcount
        if changed != 1:
            raise GovernanceError("governance_cleanup_conflict")
        return self._snapshot(db).operations[expected.operation_id].cleanup

    def begin_pass(self, operation_id, *, expected, manifest, artifact_absence=None):
        manifest_to_dict(manifest)
        if manifest is None or manifest.operation_id != operation_id:
            raise GovernanceError("governance_cleanup_manifest_invalid")
        with self._mutation() as (db, recovery, _):
            self._current(recovery, expected)
            if operation_id != expected.operation_id:
                raise GovernanceError("governance_cleanup_manifest_invalid")
            resolved = self._resolve_manifest(recovery, manifest)
            if (
                expected.pass_id == manifest.pass_id
                or any(item.state != "registered" for item in manifest.artifacts)
                or not resolved.grants
                and manifest.artifacts
                or any(
                    item.retained_bytes >= item.before.byte_length
                    or item.before.candidate_count + item.before.removed_count == 0
                    for item in manifest.artifacts
                )
            ):
                raise GovernanceError("governance_cleanup_manifest_invalid")
            if expected.manifest is not None and expected.manifest.artifacts:
                if any(
                    item.state not in {"verified", "abandoned"}
                    for item in expected.manifest.artifacts
                ):
                    raise GovernanceError("governance_cleanup_artifact_pending")
                self._absence(expected, artifact_absence)
            return self._save_progress(
                db, recovery.snapshot, expected, state="pending", manifest=manifest
            )

    def checkpoint(self, expected, *, manifest):
        manifest_to_dict(manifest)
        with self._mutation() as (db, recovery, _):
            self._current(recovery, expected)
            old = expected.manifest
            if (
                old is None
                or manifest is None
                or expected.state != "pending"
                or manifest.operation_id != old.operation_id
                or manifest.pass_id != old.pass_id
                or manifest.contributors != old.contributors
                or len(manifest.artifacts) != len(old.artifacts)
            ):
                raise GovernanceError("governance_cleanup_manifest_invalid")
            order = {"registered": 0, "staged": 1, "replaced": 2, "verified": 3}
            for before, after in zip(old.artifacts, manifest.artifacts, strict=True):
                left, right = asdict(before), asdict(after)
                left.pop("state")
                right.pop("state")
                if (
                    left != right
                    or before.state in {"verified", "abandoned"}
                    and before.state != after.state
                    or after.state != "abandoned"
                    and order.get(after.state, -1) < order.get(before.state, 4)
                ):
                    raise GovernanceError("governance_cleanup_manifest_invalid")
            return self._save_progress(
                db, recovery.snapshot, expected, state="pending", manifest=manifest
            )

    def verify_pass(self, expected, *, verification):
        with self._mutation() as (db, recovery, _):
            self._current(recovery, expected)
            if (
                not isinstance(verification, CleanupVerification)
                or not isinstance(verification.inventory, DiagnosticInventory)
                or expected.manifest is None
                or any(item.state != "verified" for item in expected.manifest.artifacts)
            ):
                raise GovernanceError("governance_cleanup_verification_invalid")
            self._absence(expected, verification.artifacts)
            inventory = verification.inventory
            if (
                type(inventory.files) is not tuple
                or len(inventory.files) > 256
                or type(inventory.domains) is not tuple
                or len(inventory.domains) != 2
                or any(type(domain) is not DomainObservation for domain in inventory.domains)
                or type(inventory.grants) is not tuple
                or type(inventory.byte_length) is not int
                or not 0 <= inventory.byte_length <= 128 * 1024 * 1024
                or type(inventory.record_count) is not int
                or not 0 <= inventory.record_count <= 100_000
            ):
                raise GovernanceError("governance_cleanup_verification_invalid")
            files = {}
            for item in inventory.files:
                _file_observation(item)
                if item.candidate_count or item.removed_count:
                    raise GovernanceError("governance_cleanup_incomplete")
                key = (item.domain, item.key)
                if key in files:
                    raise GovernanceError("governance_cleanup_verification_invalid")
                files[key] = item
            if (
                inventory.grants
                or tuple(sorted(item.domain for item in inventory.domains)) != ("pipeline", "sdk")
                or inventory.byte_length != sum(item.byte_length for item in inventory.files)
                or inventory.record_count != sum(item.record_count for item in inventory.files)
            ):
                raise GovernanceError("governance_cleanup_verification_invalid")
            for entry in expected.manifest.artifacts:
                found = files.get((entry.before.domain, entry.before.key))
                if (
                    found is None
                    or found.sha256 != entry.retained_sha256
                    or found.byte_length != entry.retained_bytes
                ):
                    raise GovernanceError("governance_cleanup_retained_mismatch")
            return self._save_progress(
                db,
                recovery.snapshot,
                expected,
                state="verified",
                manifest=expected.manifest,
                verification=VerificationEvidence(
                    _digest(
                        {
                            "domains": [
                                {
                                    "domain": item.domain,
                                    "exists": item.exists,
                                    "device": item.device,
                                    "inode": item.inode,
                                }
                                for item in inventory.domains
                            ],
                            "files": [asdict(item) for item in inventory.files],
                        }
                    ),
                    _digest(asdict(verification.artifacts)),
                ),
            )

    def complete(self, intent, *, expected_cleanup=None):
        with self._mutation() as (db, recovery, _):
            operation = recovery.snapshot.operations.get(intent.operation_id)
            if operation is None or operation.intent != intent:
                raise GovernanceError("governance_operation_unplanned")
            if intent.operation_id not in recovery.committed:
                raise GovernanceError("governance_operation_uncommitted")
            if intent.action == "purge":
                self._current(recovery, expected_cleanup)
                if expected_cleanup.operation_id != intent.operation_id or (
                    expected_cleanup.state != "verified"
                ):
                    raise GovernanceError("governance_cleanup_incomplete")
            elif expected_cleanup is not None:
                raise GovernanceError("governance_cleanup_conflict")
            values = [intent.operation_id, intent.scope_id, intent.model_dump_json(), "completed"]
            db.execute(
                "UPDATE operations SET state='completed',signature=? WHERE operation_id=?",
                (self._sign(["operations", *values]), intent.operation_id),
            )

    def has_pending(self):
        return any(
            operation.state != "completed"
            or operation.cleanup is not None
            and operation.cleanup.state != "verified"
            for operation in self.snapshot().operations.values()
        )

    def discard_uncommitted(self, operation_id, *, expected_cleanup=None, artifact_absence=None):
        with self._mutation() as (db, recovery, _):
            operation = recovery.snapshot.operations.get(operation_id)
            if operation is None:
                return
            if operation_id not in recovery.orphans or operation.state != "planned":
                raise GovernanceError("governance_operation_committed")
            if operation.cleanup is not None:
                if operation.cleanup != expected_cleanup or expected_cleanup.pass_id is not None:
                    raise GovernanceError("governance_cleanup_conflict")
                self._absence(expected_cleanup, artifact_absence)
            elif expected_cleanup is not None:
                raise GovernanceError("governance_cleanup_conflict")
            else:
                if not isinstance(artifact_absence, ArtifactAbsence):
                    raise GovernanceError("governance_cleanup_artifact_unproven")
                artifact_absence.__post_init__()
                if artifact_absence != ArtifactAbsence(
                    operation_id, None, manifest_digest(None), ()
                ):
                    raise GovernanceError("governance_cleanup_artifact_unproven")
            db.execute("DELETE FROM cleanup WHERE operation_id=?", (operation_id,))
            db.execute("DELETE FROM suppressed WHERE operation_id=?", (operation_id,))
            db.execute("DELETE FROM operations WHERE operation_id=?", (operation_id,))

    def operations(self):
        return {
            identity: (operation.state, operation.intent)
            for identity, operation in self.snapshot().operations.items()
        }

    def records(self, operation_id):
        operation = self.snapshot().operations.get(operation_id)
        return sorted(operation.records) if operation is not None else []

    def record_kinds(self, operation_id):
        operation = self.snapshot().operations.get(operation_id)
        return dict(operation.records) if operation is not None else {}

    def suppressed(self, scope_id):
        result = {}
        for operation in self.snapshot().operations.values():
            if operation.intent.scope_id != scope_id:
                continue
            for identity, kind in operation.records.items():
                if operation.intent.action == "purge":
                    result[identity] = "purged"
                else:
                    result.setdefault(identity, kind)
        return result
