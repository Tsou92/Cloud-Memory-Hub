"""ADR-0038: independent birth records and one-time permission to enter the SDK.

The access writer serializes every cooperating admission writer. A birth has
exactly ENROLLED -> MAY_INVOKE; absence of the latter proves nothing without
the former, its entire two-entry chain, and the externally signed witness.
"""

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from functools import wraps
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_memory_hub.events import EventEnvelope, canonical_uuid
from cloud_memory_hub.governance_journal import (
    Checkpoint,
    GovernanceError,
    GovernanceJournal,
    Target,
    _json,
)
from cloud_memory_hub.governance_sources import (
    content_policy_target,
    content_targets,
    source_target,
)
from cloud_memory_hub.remote_access import AccessError, ScopeGrant

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024
MAX_RULE_TARGETS = 202  # At most 200 messages, one source and one policy marker.
_MAC = r"^[0-9a-f]{64}$"


def _safe_storage(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (sqlite3.Error, OSError):
            raise GovernanceError("governance_admission_store_unavailable") from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise GovernanceError("governance_admission_invalid") from None

    return call


class AdmissionBinding(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    instance_id: str
    birth_id: str
    project_id: str
    device_id: str
    event_id: str
    event_key: str = Field(pattern=_MAC)
    event_mac: str = Field(pattern=_MAC)
    input_mac: str = Field(pattern=_MAC)
    operation: Literal["add", "add_extracted"]
    rule_targets: tuple[Target, ...] = Field(min_length=2, max_length=MAX_RULE_TARGETS)

    @field_validator("instance_id", "birth_id", "device_id", "event_id")
    @classmethod
    def uuid(cls, value):
        return canonical_uuid(value)

    @field_validator("project_id")
    @classmethod
    def project(cls, value):
        return ScopeGrant.valid_scope(value)

    @field_validator("rule_targets")
    @classmethod
    def rules(cls, value):
        if len(set(value)) != len(value) or any(
            item.kind not in {"source_record", "content"} for item in value
        ):
            raise ValueError("invalid_rules")
        return tuple(sorted(value, key=lambda item: (item.kind, item.key)))

    @property
    def request_id(self):
        return f"cmh2:{self.instance_id}:{self.birth_id}:{self.device_id}:{self.event_id}"


class AdmissionEntry(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )
    operation_id: str
    scope_id: str
    phase: Literal["enrolled", "possible"]
    binding: AdmissionBinding

    @field_validator("operation_id", "scope_id")
    @classmethod
    def uuid(cls, value):
        return canonical_uuid(value)

    @model_validator(mode="after")
    def birth_scope(self):
        if self.scope_id != self.binding.birth_id:
            raise ValueError("birth_mismatch")
        return self


@dataclass(frozen=True)
class AdmissionProof:
    binding: AdmissionBinding
    checkpoint: Checkpoint
    state: Literal["enrolled", "possible"]


class AdmissionJournal(GovernanceJournal):
    entry_model = AdmissionEntry
    initial_version = 2
    requires_admissions_binding = False

    def event_key(self, device_id, event_id):
        return self._mac("admission-event", [canonical_uuid(device_id), canonical_uuid(event_id)])

    def operation_id(self, event_key, phase):
        digest = self._mac("admission-operation-" + phase, [event_key])
        return str(UUID(bytes=bytes.fromhex(digest)[:16], version=4))

    def _check_append(self, db, entry, existing):
        binding = entry.binding
        if (
            binding.instance_id != self.instance_id
            or binding.event_key != self.event_key(binding.device_id, binding.event_id)
            or entry.operation_id != self.operation_id(binding.event_key, entry.phase)
        ):
            raise GovernanceError("governance_admission_binding_mismatch")
        if len(_json(entry.model_dump(mode="json")).encode()) > MAX_ENTRY_BYTES:
            raise GovernanceError("governance_admission_scan_limit")
        found = db.execute(
            "SELECT 1 FROM heads WHERE scope_id=? UNION "
            "SELECT 1 FROM intents WHERE scope_id=? LIMIT 1",
            (entry.scope_id, entry.scope_id),
        ).fetchone()
        if found is None:
            if entry.phase != "enrolled" or existing is not None:
                raise GovernanceError("governance_admission_sequence_invalid")
            return
        proof, _ = self._stream(db, entry.scope_id)
        if proof.binding != binding:
            raise GovernanceError("governance_admission_binding_mismatch")
        if existing is not None and entry.phase == "enrolled":
            return  # Stable event replay; this is never an SDK permission.
        if existing is not None or proof.state == "possible":
            raise GovernanceError("governance_admission_already_possible")
        if entry.phase != "possible":
            raise GovernanceError("governance_admission_sequence_invalid")

    def _decode(self, row):
        entry = super()._decode(row)
        expected_phase = {1: "enrolled", 2: "possible"}.get(row["sequence"])
        binding = entry.binding
        if (
            entry.phase != expected_phase
            or binding.instance_id != self.instance_id
            or binding.event_key != self.event_key(binding.device_id, binding.event_id)
            or entry.operation_id != self.operation_id(binding.event_key, entry.phase)
        ):
            raise GovernanceError("governance_admission_sequence_invalid")
        return entry

    def _stream(self, db, birth_id):
        """Read and verify the complete bounded stream, including its head."""
        measured = db.execute(
            "SELECT sequence,length(CAST(payload AS BLOB)) FROM intents "
            "WHERE scope_id=? ORDER BY sequence LIMIT 3",
            (birth_id,),
        ).fetchall()
        if not 1 <= len(measured) <= 2 or any(
            row[1] is None or row[1] > MAX_ENTRY_BYTES for row in measured
        ):
            raise GovernanceError("governance_admission_sequence_invalid")
        prior, binding, checkpoints = self._zero(birth_id), None, {}
        for row in db.execute(
            "SELECT * FROM intents WHERE scope_id=? ORDER BY sequence LIMIT 3", (birth_id,)
        ):
            entry = self._decode(row)
            if (
                row["sequence"] != prior.sequence + 1
                or row["previous_digest"] != prior.digest
                or binding is not None
                and binding != entry.binding
            ):
                raise GovernanceError("governance_admission_sequence_invalid")
            binding = entry.binding
            prior = Checkpoint(
                ledger_id=self.ledger_id, sequence=row["sequence"], digest=row["digest"]
            )
            checkpoints[prior.sequence] = prior
        head = db.execute(
            "SELECT sequence,digest FROM heads WHERE scope_id=?", (birth_id,)
        ).fetchone()
        if head is None or (head[0], head[1]) != (prior.sequence, prior.digest):
            raise GovernanceError("governance_admission_sequence_invalid")
        return AdmissionProof(binding, prior, entry.phase), checkpoints

    def _verify(self, db):
        super()._verify(db)
        if self.schema_version != 2:
            raise GovernanceError("governance_admission_migration_required")
        for row in db.execute("SELECT scope_id FROM heads"):
            self._stream(db, row[0])


class WriteAdmissions:
    def __init__(self, access, main_journal):
        self.access, self.main = access, main_journal
        if access.schema_version != 3 or main_journal.schema_version != 2:
            raise GovernanceError("governance_admission_migration_required")
        self.journal = self._open_journal(main_journal)
        self.verify_all()

    @staticmethod
    def _key(main):
        return hmac.digest(main._key, b"cmh-sdk-admissions/v1", hashlib.sha256)

    @classmethod
    def _open_journal(cls, main):
        expected = main.admissions_ledger_id()
        if expected is None:
            raise GovernanceError("governance_admission_binding_mismatch")
        journal = AdmissionJournal(
            main.path.with_name("admissions.sqlite3"),
            instance_id=main.instance_id,
            key=cls._key(main),
        )
        if journal.ledger_id != expected:
            raise GovernanceError("governance_admission_binding_mismatch")
        return journal

    @classmethod
    def activate(cls, access, main_journal):
        """Explicit stopped-service migration; never replace a bound missing log."""
        if access.instance_id != main_journal.instance_id or access.schema_version not in {2, 3}:
            raise GovernanceError("governance_admission_migration_required")
        if access.schema_version == 3:
            return cls(access, main_journal)
        path = main_journal.path.with_name("admissions.sqlite3")
        bound = main_journal.admissions_ledger_id()
        if bound is not None:
            journal = cls._open_journal(main_journal)
        elif path.exists():
            journal = AdmissionJournal(
                path, instance_id=access.instance_id, key=cls._key(main_journal)
            )
            if journal.scopes():
                raise GovernanceError("governance_admission_unbound_log")
        else:
            journal = AdmissionJournal.initialize(
                path, instance_id=access.instance_id, key=cls._key(main_journal)
            )
        main_journal.upgrade_v2(journal.ledger_id)
        signature = journal._mac("admission-access-binding", [main_journal.ledger_id, 3])
        try:
            with access._connection(writing=True) as db:
                db.execute(
                    "CREATE TABLE cmh_admission_binding (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "ledger_id TEXT NOT NULL, signature TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE cmh_admission_witnesses (event_key TEXT PRIMARY KEY, "
                    "birth_id TEXT NOT NULL UNIQUE, checkpoint TEXT NOT NULL, "
                    "signature TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO cmh_admission_binding VALUES (1,?,?)",
                    (journal.ledger_id, signature),
                )
                db.execute("PRAGMA user_version=3")
        except (AccessError, sqlite3.Error):
            raise GovernanceError("governance_admission_store_unavailable") from None
        access.schema_version = 3
        return cls(access, main_journal)

    def _binding(self, db):
        if (
            not db.in_transaction
            or db.execute("PRAGMA user_version").fetchone()[0] != 3
            or db.execute("SELECT instance_id FROM instance WHERE id=1").fetchone()[0]
            != self.access.instance_id
            or self.access.instance_id != self.main.instance_id
            or self.main.admissions_ledger_id() != self.journal.ledger_id
        ):
            raise GovernanceError("governance_admission_binding_mismatch")
        rows = db.execute("SELECT * FROM cmh_admission_binding").fetchall()
        expected = self.journal._mac("admission-access-binding", [self.main.ledger_id, 3])
        if (
            len(rows) != 1
            or rows[0]["ledger_id"] != self.journal.ledger_id
            or not hmac.compare_digest(rows[0]["signature"], expected)
        ):
            raise GovernanceError("governance_admission_binding_mismatch")

    def _witness(self, db, proof, checkpoints, *, refresh_witness=True):
        binding = proof.binding
        row = db.execute(
            "SELECT * FROM cmh_admission_witnesses WHERE event_key=?", (binding.event_key,)
        ).fetchone()
        if row is None and not refresh_witness:
            raise GovernanceError("governance_admission_witness_missing")
        if row is not None:
            expected = self.journal._mac(
                "admission-witness", [row["event_key"], row["birth_id"], row["checkpoint"]]
            )
            if row["birth_id"] != binding.birth_id or not hmac.compare_digest(
                row["signature"], expected
            ):
                raise GovernanceError("governance_admission_witness_invalid")
            try:
                floor = Checkpoint.model_validate_json(row["checkpoint"])
            except ValueError:
                raise GovernanceError("governance_admission_witness_invalid") from None
            if floor.sequence > proof.checkpoint.sequence:
                raise GovernanceError("governance_admission_watermark_ahead")
            if (
                floor != checkpoints.get(floor.sequence)
                or _json(floor.model_dump()) != row["checkpoint"]
            ):
                raise GovernanceError("governance_admission_witness_invalid")
        if not refresh_witness:
            return
        payload = _json(proof.checkpoint.model_dump())
        signature = self.journal._mac(
            "admission-witness", [binding.event_key, binding.birth_id, payload]
        )
        if row is None or row["checkpoint"] != payload:
            db.execute(
                "INSERT INTO cmh_admission_witnesses VALUES (?,?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET checkpoint=excluded.checkpoint,"
                "signature=excluded.signature",
                (binding.event_key, binding.birth_id, payload, signature),
            )

    def _lookup(self, db, event_key, *, refresh_witness=True):
        with self.journal._connection() as log:
            row = log.execute(
                "SELECT scope_id FROM intents WHERE operation_id=?",
                (self.journal.operation_id(event_key, "enrolled"),),
            ).fetchone()
            if row is None:
                if db.execute(
                    "SELECT 1 FROM cmh_admission_witnesses WHERE event_key=?", (event_key,)
                ).fetchone():
                    raise GovernanceError("governance_admission_watermark_ahead")
                return None
            proof, checkpoints = self.journal._stream(log, row[0])
        if proof.binding.event_key != event_key:
            raise GovernanceError("governance_admission_binding_mismatch")
        self._witness(db, proof, checkpoints, refresh_witness=refresh_witness)
        return proof

    def _event_mac(self, event):
        payload = event.canonical_payload()
        if len(payload.encode()) > MAX_INPUT_BYTES:
            raise GovernanceError("governance_admission_scan_limit")
        return self.journal._mac("admission-filtered-event", [payload])

    def _actual(self, actual, request_id):
        try:
            if (
                actual.request_id != request_id
                or actual.operation not in {"add", "add_extracted"}
                or type(actual.canonical_input) is not bytes
            ):
                raise ValueError
            data = actual.canonical_input
            if not data:
                raise ValueError
            if len(data) > MAX_INPUT_BYTES:
                raise GovernanceError("governance_admission_scan_limit")
            digest = hashlib.sha256(data).hexdigest()
            return actual.operation, self.journal._mac(
                "admission-sdk-input", [actual.operation, digest]
            )
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_admission_input_mismatch") from None

    @_safe_storage
    def enroll_in(self, db, event, prepare, rule_targets):
        """Borrow the caller's access BEGIN IMMEDIATE; no nested write checkout."""
        self._binding(db)
        event = EventEnvelope.model_validate(
            event.model_dump() if isinstance(event, EventEnvelope) else event
        )
        if event.instance_id != self.access.instance_id:
            raise GovernanceError("governance_admission_binding_mismatch")
        event_key = self.journal.event_key(event.device_id, event.event_id)
        event_mac = self._event_mac(event)
        previous = self._lookup(db, event_key)
        birth_id = previous.binding.birth_id if previous is not None else str(uuid4())
        request_id = f"cmh2:{event.instance_id}:{birth_id}:{event.device_id}:{event.event_id}"
        operation, input_mac = self._actual(prepare(request_id), request_id)
        rules = tuple(rule_targets)
        expected_rules = {
            content_policy_target(self.main, event.scope_id),
            source_target(self.main, event),
            *content_targets(self.main, event),
        }
        if set(rules) != expected_rules or len(rules) != len(expected_rules):
            raise GovernanceError("governance_admission_rules_invalid")
        binding = AdmissionBinding(
            instance_id=event.instance_id,
            birth_id=birth_id,
            project_id=event.scope_id,
            device_id=event.device_id,
            event_id=event.event_id,
            event_key=event_key,
            event_mac=event_mac,
            input_mac=input_mac,
            operation=operation,
            rule_targets=rules,
        )
        if previous is not None:
            if previous.binding != binding:
                raise GovernanceError("governance_admission_event_conflict")
            return binding
        entry = self._entry(binding, "enrolled")
        self.journal.append(entry)
        proof = self._lookup(db, event_key)
        if proof is None:
            raise GovernanceError("governance_admission_sequence_invalid")
        return proof.binding

    def _entry(self, binding, phase):
        entry = AdmissionEntry(
            operation_id=self.journal.operation_id(binding.event_key, phase),
            scope_id=binding.birth_id,
            phase=phase,
            binding=binding,
        )
        if len(_json(entry.model_dump(mode="json")).encode()) > MAX_ENTRY_BYTES:
            raise GovernanceError("governance_admission_scan_limit")
        return entry

    @_safe_storage
    def proof_in(self, db, request_id, *, event=None, actual=None, refresh_witness=True):
        """Validate the latest proof; read-only callers require an existing witness."""
        self._binding(db)
        try:
            parts = request_id.split(":")
            if len(parts) != 5 or parts[:2] != ["cmh2", self.access.instance_id]:
                raise ValueError
            birth, device, event_id = (canonical_uuid(part) for part in parts[2:])
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_admission_identity_invalid") from None
        proof = self._lookup(
            db,
            self.journal.event_key(device, event_id),
            refresh_witness=refresh_witness,
        )
        if (
            proof is None
            or proof.binding.birth_id != birth
            or proof.binding.request_id != request_id
        ):
            raise GovernanceError("governance_admission_birth_unproven")
        binding = proof.binding
        if event is not None and self._event_mac(event) != binding.event_mac:
            raise GovernanceError("governance_admission_event_conflict")
        if actual is not None and self._actual(actual, request_id) != (
            binding.operation,
            binding.input_mac,
        ):
            raise GovernanceError("governance_admission_input_mismatch")
        return proof

    @_safe_storage
    def permit_once(self, actual, *, require_allowed):
        """Returning successfully is a new permission, only after both commits."""
        refusal = None
        try:
            with self.access._connection(writing=True) as db:
                proof = self.proof_in(db, actual.request_id)
                try:
                    if self._actual(actual, actual.request_id) != (
                        proof.binding.operation,
                        proof.binding.input_mac,
                    ):
                        raise GovernanceError("governance_admission_input_mismatch")
                    require_allowed(proof.binding)
                    if proof.state != "enrolled":
                        raise GovernanceError("governance_admission_already_possible")
                except (GovernanceError, AccessError) as error:
                    refusal = error
                if refusal is None:
                    self.journal.append(self._entry(proof.binding, "possible"))
                    updated = self._lookup(db, proof.binding.event_key)
                    if updated is None or updated.state != "possible":
                        raise GovernanceError("governance_admission_sequence_invalid")
        except (AccessError, sqlite3.Error, OSError):
            raise GovernanceError("governance_admission_store_unavailable") from None
        if refusal is not None:
            # Verified floor repairs commit even when this invocation is refused.
            raise refusal

    @_safe_storage
    def verify_all(self):
        try:
            with self.access._connection(writing=True) as db:
                self._binding(db)
                for row in db.execute("SELECT event_key FROM cmh_admission_witnesses"):
                    if self._lookup(db, row[0]) is None:
                        raise GovernanceError("governance_admission_witness_invalid")
                with self.journal._connection() as log:
                    self.journal._verify(log)
                    for row in log.execute("SELECT scope_id FROM heads"):
                        proof, checkpoints = self.journal._stream(log, row[0])
                        self._witness(db, proof, checkpoints)
        except (AccessError, sqlite3.Error, OSError):
            raise GovernanceError("governance_admission_store_unavailable") from None
