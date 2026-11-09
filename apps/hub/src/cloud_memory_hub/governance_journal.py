"""Content-free governance intents; not a receipt of completed physical deletion."""

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore, ScopeGrant

MAX_SEQUENCE = 2**53 - 1
MAX_PAGE_BYTES = 2 * 1024 * 1024
FILE_COVERAGE_CAPABILITY = "execution_file_coverage_v2"
TargetKind = Literal["memory", "raw", "source_record", "content"]


class GovernanceError(Exception):
    """Fixed, non-sensitive reason codes only."""


class Target(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    kind: TargetKind
    key: str = Field(pattern=r"^[0-9a-f]{64}$")


class Intent(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    operation_id: str
    actor_id: str
    scope_id: str
    action: Literal["forget", "purge"]
    targets: tuple[Target, ...] = Field(min_length=1, max_length=10_000)

    @field_validator("operation_id", "actor_id")
    @classmethod
    def identifier(cls, value):
        return canonical_uuid(value)

    @field_validator("scope_id")
    @classmethod
    def scope(cls, value):
        return ScopeGrant.valid_scope(value)

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, value):
        identities = [(target.kind, target.key) for target in value]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate_governance_target")
        return tuple(sorted(value, key=lambda target: (target.kind, target.key)))


class Checkpoint(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    ledger_id: str
    sequence: int = Field(strict=True, ge=0, le=MAX_SEQUENCE)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("ledger_id")
    @classmethod
    def identifier(cls, value):
        return canonical_uuid(value)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class GovernanceJournal:
    entry_model = Intent
    initial_version = 1
    requires_admissions_binding = True

    def __init__(self, path: Path, *, instance_id: str, key: bytes, read_only: bool = False):
        if type(read_only) is not bool:
            raise GovernanceError("governance_mode_invalid")
        self.read_only = read_only
        self.path = path.absolute()
        self.instance_id = self._instance(instance_id)
        if not isinstance(key, bytes) or len(key) != 32:
            raise GovernanceError("governance_key_invalid")
        self._key = key
        self.ledger_id = None
        self.schema_version = None
        with self._connection() as db:
            self._verify(db)

    @staticmethod
    def _instance(value):
        try:
            return canonical_uuid(value)
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_instance_invalid") from None

    @classmethod
    def initialize(cls, path: Path, *, instance_id: str, key: bytes):
        instance_id = cls._instance(instance_id)
        if not isinstance(key, bytes) or len(key) != 32:
            raise GovernanceError("governance_key_invalid")
        path = path.absolute()
        ledger_id = str(uuid4())
        version = cls.initial_version
        if version not in {1, 2}:
            raise GovernanceError("governance_version_unsupported")
        signature = cls._sign(key, "binding", [instance_id, ledger_id, version])
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            DeviceAccessStore._check_private(path.parent, directory=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE binding (
                        id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL,
                        ledger_id TEXT NOT NULL, signature TEXT NOT NULL
                    );
                    CREATE TABLE heads (
                        scope_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL CHECK(sequence>0),
                        digest TEXT NOT NULL
                    );
                    CREATE TABLE intents (
                        scope_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>0),
                        operation_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                        previous_digest TEXT NOT NULL, digest TEXT NOT NULL,
                        PRIMARY KEY(scope_id,sequence)
                    );
                    PRAGMA user_version=1;
                """)
                db.execute(
                    "INSERT INTO binding VALUES (1,?,?,?)", (instance_id, ledger_id, signature)
                )
                if version == 2:
                    cls._create_capabilities(db)
                    db.execute("PRAGMA user_version=2")
                db.commit()
            # Persist the new directory entry, not just SQLite's journal transaction.
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            raise GovernanceError("governance_already_exists") from None
        except AccessError:
            raise GovernanceError("governance_path_unsafe") from None
        except (OSError, sqlite3.Error):
            # Preserve an incomplete initialization for explicit recovery; never erase it.
            raise GovernanceError("governance_initialize_failed") from None
        return cls(path, instance_id=instance_id, key=key)

    @staticmethod
    def _sign(key, domain, values):
        return hmac.new(key, (domain + "\0" + _json(values)).encode(), hashlib.sha256).hexdigest()

    def _mac(self, domain, values):
        return self._sign(self._key, domain, [self.instance_id, self.ledger_id, *values])

    @contextmanager
    def _connection(self, *, writing=False):
        if writing and self.read_only:
            raise GovernanceError("governance_journal_read_only")
        try:
            DeviceAccessStore._check_private(self.path.parent, directory=True)
            DeviceAccessStore._check_private(self.path)
            mode = "ro" if self.read_only else "rw"
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                if self.read_only:
                    db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                row = db.execute("SELECT * FROM binding WHERE id=1").fetchone()
                if version not in {1, 2} or self.schema_version not in (None, version):
                    raise GovernanceError("governance_version_unsupported")
                if (
                    row is None
                    or row["instance_id"] != self.instance_id
                    or (self.ledger_id is not None and row["ledger_id"] != self.ledger_id)
                    or not hmac.compare_digest(
                        row["signature"],
                        self._sign(
                            self._key, "binding", [self.instance_id, row["ledger_id"], version]
                        ),
                    )
                ):
                    raise GovernanceError("governance_binding_mismatch")
                self.ledger_id = canonical_uuid(row["ledger_id"])
                self.schema_version = version
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except AccessError:
            raise GovernanceError("governance_path_unsafe") from None
        except (OSError, sqlite3.Error):
            raise GovernanceError("governance_store_unavailable") from None
        except (ValueError, TypeError, KeyError):
            raise GovernanceError("governance_store_corrupt") from None

    @staticmethod
    def _create_capabilities(db):
        db.execute(
            "CREATE TABLE capabilities (name TEXT PRIMARY KEY, "
            "value TEXT NOT NULL, signature TEXT NOT NULL)"
        )

    def _capabilities(self, db):
        if self.schema_version == 1:
            return {}
        rows = db.execute("SELECT * FROM capabilities").fetchall()
        if not self.requires_admissions_binding:
            if rows:
                raise GovernanceError("governance_capability_invalid")
            return {}
        names = {row["name"] for row in rows}
        if len(names) != len(rows) or names not in (
            {"admissions_ledger_id"},
            {"admissions_ledger_id", "execution_store_v2"},
            {"admissions_ledger_id", "execution_store_v2", FILE_COVERAGE_CAPABILITY},
        ):
            raise GovernanceError("governance_capability_invalid")
        result = {}
        for row in rows:
            try:
                value = canonical_uuid(row["value"])
                valid = hmac.compare_digest(
                    row["signature"], self._mac("capability", [row["name"], value])
                )
            except (ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise GovernanceError("governance_capability_invalid")
            result[row["name"]] = value
        if (
            FILE_COVERAGE_CAPABILITY in result
            and result[FILE_COVERAGE_CAPABILITY] != result["execution_store_v2"]
        ):
            raise GovernanceError("governance_capability_invalid")
        return result

    def _admissions_binding(self, db):
        return self._capabilities(db).get("admissions_ledger_id")

    def admissions_ledger_id(self):
        with self._connection() as db:
            return self._admissions_binding(db)

    def execution_store_id(self):
        with self._connection() as db:
            return self._capabilities(db).get("execution_store_v2")

    def _coverage_profile(self, db):
        from cloud_memory_hub.governance_diagnostic_profiles import CORE_PROFILE, SDK_PROFILE

        capabilities = self._capabilities(db)
        return CORE_PROFILE if FILE_COVERAGE_CAPABILITY in capabilities else SDK_PROFILE

    def file_coverage_profile(self):
        """Read the signed requirement, never infer completed file coverage."""
        with self._connection() as db:
            return self._coverage_profile(db)

    def bind_file_coverage(self, store_id: str) -> None:
        """Raise requirements after explicit stopped-service store validation.

        This marker does not finish cleanup, rewrite old receipts or open the
        ordinary runtime. Repeating the same activation changes no stored data.
        """
        if self.read_only:
            raise GovernanceError("governance_journal_read_only")
        identity = self._instance(store_id)
        with self._connection(writing=True) as db:
            self._verify(db)
            capabilities = self._capabilities(db)
            current = capabilities.get("execution_store_v2")
            if current is None:
                raise GovernanceError("governance_execution_binding_required")
            if current != identity:
                raise GovernanceError("governance_capability_mismatch")
            if FILE_COVERAGE_CAPABILITY in capabilities:
                return
            db.execute(
                "INSERT INTO capabilities VALUES (?,?,?)",
                (
                    FILE_COVERAGE_CAPABILITY,
                    identity,
                    self._mac("capability", [FILE_COVERAGE_CAPABILITY, identity]),
                ),
            )

    def bind_execution_store(self, store_id: str) -> None:
        """Explicit activation after stopped-writer execution-store validation.

        This capability binds metadata, not a claim of completed deletion.
        It does not rewrite any intent, head or existing checkpoint.
        """
        if self.read_only:
            raise GovernanceError("governance_journal_read_only")
        identity = self._instance(store_id)
        with self._connection(writing=True) as db:
            self._verify(db)
            capabilities = self._capabilities(db)
            if not capabilities.get("admissions_ledger_id"):
                raise GovernanceError("governance_admissions_binding_required")
            current = capabilities.get("execution_store_v2")
            if current is not None:
                if current != identity:
                    raise GovernanceError("governance_capability_mismatch")
                return
            db.execute(
                "INSERT INTO capabilities VALUES ('execution_store_v2',?,?)",
                (identity, self._mac("capability", ["execution_store_v2", identity])),
            )

    def upgrade_v2(self, admissions_ledger_id: str) -> None:
        """Explicit stopped-writer upgrade; preserve every old intent and checkpoint."""
        if self.read_only:
            raise GovernanceError("governance_journal_read_only")
        identity = self._instance(admissions_ledger_id)
        with self._connection(writing=True) as db:
            self._verify(db)
            bound = self._admissions_binding(db)
            if bound is not None:
                if bound != identity:
                    raise GovernanceError("governance_capability_mismatch")
                return
            if self.schema_version == 1:
                self._create_capabilities(db)
            db.execute(
                "INSERT INTO capabilities VALUES ('admissions_ledger_id',?,?)",
                (identity, self._mac("capability", ["admissions_ledger_id", identity])),
            )
            db.execute(
                "UPDATE binding SET signature=? WHERE id=1",
                (self._sign(self._key, "binding", [self.instance_id, self.ledger_id, 2]),),
            )
            db.execute("PRAGMA user_version=2")
        self.schema_version = 2

    def target(self, scope_id, kind: TargetKind, identity: str) -> Target:
        scope_id = self._scope(scope_id)
        if not isinstance(identity, str) or not identity or len(identity) > 1_000_000:
            raise GovernanceError("governance_target_invalid")
        if not isinstance(kind, str) or kind not in {"memory", "raw", "source_record", "content"}:
            raise GovernanceError("governance_target_invalid")
        # Stable across journal migrations, but isolated by instance, project and kind.
        digest = self._sign(self._key, "target", [self.instance_id, scope_id, kind, identity])
        return Target(kind=kind, key=digest)

    @staticmethod
    def _scope(value):
        try:
            return ScopeGrant.valid_scope(value)
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_scope_invalid") from None

    def _zero(self, scope):
        return Checkpoint(ledger_id=self.ledger_id, sequence=0, digest=self._mac("zero", [scope]))

    def _decode(self, row):
        intent = self.entry_model.model_validate_json(row["payload"])
        payload = _json(intent.model_dump(mode="json"))
        digest = self._mac("intent", [row["sequence"], row["previous_digest"], payload])
        if (
            row["scope_id"] != intent.scope_id
            or row["operation_id"] != intent.operation_id
            or row["payload"] != payload
            or not hmac.compare_digest(digest, row["digest"])
        ):
            raise GovernanceError("governance_chain_invalid")
        return intent

    def _head(self, db, scope):
        row = db.execute("SELECT * FROM heads WHERE scope_id=?", (scope,)).fetchone()
        if row is None:
            return self._zero(scope)
        tail = db.execute(
            "SELECT * FROM intents WHERE scope_id=? AND sequence=?", (scope, row["sequence"])
        ).fetchone()
        if tail is None or tail["digest"] != row["digest"]:
            raise GovernanceError("governance_head_invalid")
        self._decode(tail)
        return Checkpoint(ledger_id=self.ledger_id, sequence=row["sequence"], digest=row["digest"])

    def _verify(self, db):
        count = 0
        for row in db.execute("SELECT scope_id FROM heads"):
            scope = ScopeGrant.valid_scope(row[0])
            prior = self._zero(scope)
            for entry in db.execute(
                "SELECT * FROM intents WHERE scope_id=? ORDER BY sequence", (scope,)
            ):
                self._decode(entry)
                if (
                    entry["sequence"] != prior.sequence + 1
                    or entry["previous_digest"] != prior.digest
                ):
                    raise GovernanceError("governance_chain_invalid")
                prior = Checkpoint(
                    ledger_id=self.ledger_id, sequence=entry["sequence"], digest=entry["digest"]
                )
                count += 1
            if self._head(db, scope) != prior:
                raise GovernanceError("governance_head_invalid")
        if count != db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]:
            raise GovernanceError("governance_orphan_intent")
        self._admissions_binding(db)

    def head(self, scope_id):
        scope = self._scope(scope_id)
        with self._connection() as db:
            return self._head(db, scope)

    def scopes(self) -> tuple[str, ...]:
        """Include orphan scopes too so a damaged head cannot hide an intent."""
        with self._connection() as db:
            return tuple(
                self._scope(row[0])
                for row in db.execute(
                    "SELECT scope_id FROM heads UNION SELECT DISTINCT scope_id FROM intents"
                )
            )

    def _check_append(self, db, entry, existing):
        """Typed journals may impose additional transitions inside this transaction."""

    def append(self, intent: Intent) -> Checkpoint:
        if self.read_only:
            raise GovernanceError("governance_journal_read_only")
        try:
            intent = self.entry_model.model_validate(intent)
        except ValueError:
            raise GovernanceError("governance_intent_invalid") from None
        payload = _json(intent.model_dump(mode="json"))
        with self._connection(writing=True) as db:
            existing = db.execute(
                "SELECT * FROM intents WHERE operation_id=?", (intent.operation_id,)
            ).fetchone()
            self._check_append(db, intent, existing)
            if existing is not None:
                self._decode(existing)
                if existing["payload"] != payload:
                    raise GovernanceError("governance_operation_conflict")
                return Checkpoint(
                    ledger_id=self.ledger_id,
                    sequence=existing["sequence"],
                    digest=existing["digest"],
                )
            prior = self._head(db, intent.scope_id)
            if prior.sequence == MAX_SEQUENCE:
                raise GovernanceError("governance_sequence_exhausted")
            next_sequence = prior.sequence + 1
            digest = self._mac("intent", [next_sequence, prior.digest, payload])
            # Intent + head either both commit, or neither is visible after a crash.
            db.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?)",
                (
                    intent.scope_id,
                    next_sequence,
                    intent.operation_id,
                    payload,
                    prior.digest,
                    digest,
                ),
            )
            db.execute(
                "INSERT INTO heads VALUES (?,?,?) ON CONFLICT(scope_id) DO UPDATE SET "
                "sequence=excluded.sequence,digest=excluded.digest",
                (intent.scope_id, next_sequence, digest),
            )
            return Checkpoint(ledger_id=self.ledger_id, sequence=next_sequence, digest=digest)

    def changes(self, scope_id, *, after: Checkpoint | None = None, limit=100):
        scope = self._scope(scope_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GovernanceError("governance_page_limit_invalid")
        try:
            after = Checkpoint.model_validate(after) if after is not None else None
        except ValueError:
            raise GovernanceError("governance_checkpoint_invalid") from None
        with self._connection() as db:
            head = self._head(db, scope)
            after = after if after is not None else self._zero(scope)
            if after.ledger_id != self.ledger_id:
                raise GovernanceError("governance_ledger_changed")
            if after.sequence > head.sequence:
                raise GovernanceError("governance_watermark_ahead")
            anchor = self._zero(scope).digest
            if after.sequence:
                row = db.execute(
                    "SELECT * FROM intents WHERE scope_id=? AND sequence=?", (scope, after.sequence)
                ).fetchone()
                if row is None:
                    raise GovernanceError("governance_chain_invalid")
                self._decode(row)
                anchor = row["digest"]
            if not hmac.compare_digest(anchor, after.digest):
                raise GovernanceError("governance_watermark_diverged")
            entries, cursor, used = [], after, 0
            for row in db.execute(
                "SELECT * FROM intents WHERE scope_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (scope, after.sequence, limit),
            ):
                size = len(row["payload"].encode()) + 256
                if used + size > MAX_PAGE_BYTES:
                    if not entries:
                        raise GovernanceError("governance_entry_too_large")
                    break
                intent = self._decode(row)
                if (
                    row["sequence"] != cursor.sequence + 1
                    or row["previous_digest"] != cursor.digest
                ):
                    raise GovernanceError("governance_chain_invalid")
                cursor = Checkpoint(
                    ledger_id=self.ledger_id, sequence=row["sequence"], digest=row["digest"]
                )
                entries.append(
                    {"checkpoint": cursor.model_dump(), "intent": intent.model_dump(mode="json")}
                )
                used += size
            if cursor.sequence > head.sequence or (
                cursor.sequence == head.sequence and cursor != head
            ):
                raise GovernanceError("governance_head_invalid")
            if not entries and cursor.sequence != head.sequence:
                raise GovernanceError("governance_chain_invalid")
            return {
                "head": head,
                "cursor": cursor,
                "has_more": cursor.sequence < head.sequence,
                "entries": entries,
            }
