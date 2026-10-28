"""Remote source authority and receipts around the existing Core write queue."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from one_memory.imports.models import (
    EvidenceInput,
    EvidenceOrigin,
    OriginMetadata,
    WrittenBy,
    materialize_write_input,
)
from one_memory.memory_server.contracts import BatchIdentity, WriteRequest, internal_session_id_for
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from cloud_memory_hub.events import (
    EventEnvelope,
    EventError,
    canonical_uuid,
    require_current_filter,
)
from cloud_memory_hub.original_archive import (
    ARCHIVE_VERSION,
    create_archive_indexes,
    validate_original,
)
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    NamedResource,
    RevisionRequest,
    ScopeGrant,
)

SOURCE_PREFIX = "cmh:"


class SourceRegistration(NamedResource):
    device_id: str
    scope_id: str
    adapter_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")

    @field_validator("device_id")
    @classmethod
    def valid_device(cls, value):
        return canonical_uuid(value)

    @field_validator("scope_id")
    @classmethod
    def valid_scope(cls, value):
        return ScopeGrant.valid_scope(value)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    role: Literal["user", "assistant", "tool"]
    text: str = Field(min_length=1, max_length=1_000_000, strict=True, repr=False)
    metadata: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    messages: tuple[Message, ...] = Field(min_length=1, max_length=200, repr=False)
    metadata: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    text: str = Field(min_length=1, max_length=1_000_000, strict=True, repr=False)
    metadata: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


def write_request(event: EventEnvelope) -> WriteRequest:
    """Deterministic SDK inputs: retry timestamps must never change the job."""
    if event.kind == "conversation_turn":
        content = Conversation.model_validate(event.content)
        messages, metadata = content.messages, content.metadata
    else:
        operation = Operation.model_validate(event.content)
        messages = (Message(role="tool", text=operation.text),)
        metadata = operation.metadata
    observed = event.occurred_at or datetime(1970, 1, 1, tzinfo=UTC)
    source = SOURCE_PREFIX + event.source_id
    namespace = f"cmh:{event.instance_id}:{event.scope_id}"
    request_id = f"cmh:{event.instance_id}:{event.device_id}:{event.event_id}"
    identity = event.model_dump(mode="json", exclude={"content"})
    records = []
    for index, message in enumerate(messages):
        provenance = {"event": identity, "message_index": index}
        if message.metadata:
            provenance["message_metadata"] = message.metadata
        if index == 0 and metadata:
            provenance["content_metadata"] = metadata
        text = (
            message.text
            + "\n\n<cmh_source_reference>\n"
            + json.dumps(provenance, ensure_ascii=False, sort_keys=True)
            + "\n</cmh_source_reference>"
        )
        record = materialize_write_input(
            EvidenceInput(
                user_id=namespace,
                source_id=source,
                workspace=event.scope_id,
                project=event.scope_id,
                conversation_id=event.conversation_id,
                message_id=f"{event.record_id}:{event.revision}:{index}",
                role=message.role,
                written_by={
                    "user": WrittenBy.PERSON,
                    "assistant": WrittenBy.AGENT,
                    "tool": WrittenBy.TOOL,
                }[message.role],
                created_at=event.occurred_at,
                verbatim=text,
                origin_meta={
                    EvidenceOrigin.PLUGIN: OriginMetadata(
                        raw_meta={"event_id": event.event_id, "message_index": index}
                    )
                },
            ),
            observed_at=observed,
        ).model_copy(update={"revision": event.revision})
        records.append(record)
    session_id = internal_session_id_for(event.conversation_id, source)
    return WriteRequest(
        internal_session_id=session_id,
        source_id=source,
        batch_identity=BatchIdentity(
            internal_session_id=session_id, first_round=1, last_round=len(records)
        ),
        evidence_ids=[record.evidence_id for record in records],
        input_records=tuple(records),
        idempotency_key=request_id,
        priority="live",
        event_at=event.occurred_at,
        time_quality="exact" if event.occurred_at else "approximate",
    )


class RemoteIngest:
    def __init__(self, access: DeviceAccessStore, queue_provider):
        self.access = access
        self.queue_provider = queue_provider
        self.privacy = CredentialFilter()
        with access._connection(writing=True) as db:
            extension = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='cmh_ingest_schema'"
            ).fetchone()
            if extension is None:
                if db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('cmh_sources','cmh_event_receipts')"
                ).fetchone():
                    raise AccessError("ingest_store_unrecognized", 503)
                db.execute(
                    "CREATE TABLE cmh_ingest_schema (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "version INTEGER NOT NULL)"
                )
                db.execute("""CREATE TABLE cmh_sources (
                    source_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id),
                    scope_id TEXT NOT NULL REFERENCES projects(project_id),
                    adapter_id TEXT NOT NULL, display_name TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked'))
                )""")
                db.execute("""CREATE TABLE cmh_event_receipts (
                    device_id TEXT NOT NULL REFERENCES devices(device_id), event_id TEXT NOT NULL,
                    source_id TEXT NOT NULL REFERENCES cmh_sources(source_id),
                    scope_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, record_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL, receipt_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(device_id,event_id),
                    UNIQUE(source_id,conversation_id,record_id,revision)
                )""")
                create_archive_indexes(db)
                db.execute("INSERT INTO cmh_ingest_schema VALUES (1,?)", (ARCHIVE_VERSION,))
            self._check_schema(db)

    @staticmethod
    def _check_schema(db):
        version = db.execute("SELECT version FROM cmh_ingest_schema WHERE id=1").fetchone()
        if version is not None and version[0] == 1:
            raise AccessError("original_retention_migration_required", 503)
        if version is None or version[0] != ARCHIVE_VERSION:
            raise AccessError("ingest_store_version_unsupported", 503)
        db.execute(
            "SELECT source_id, device_id, scope_id, adapter_id, display_name, revision, status "
            "FROM cmh_sources LIMIT 0"
        )
        db.execute(
            "SELECT device_id, event_id, source_id, scope_id, conversation_id, record_id, "
            "revision, payload_digest, receipt_id, request_id, payload_json "
            "FROM cmh_event_receipts LIMIT 0"
        )

    @contextmanager
    def _database(self, *, writing=False):
        with self.access._connection(writing=writing) as db:
            self._check_schema(db)
            yield db

    def create_source(self, registration: SourceRegistration) -> dict:
        with self._database(writing=True) as db:
            device = db.execute(
                "SELECT status FROM devices WHERE device_id=?", (registration.device_id,)
            ).fetchone()
            if device is None or device[0] != "active":
                raise AccessError("device_not_active", 409)
            if not db.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (registration.scope_id,)
            ).fetchone():
                raise AccessError("unknown_scope", 422)
            source = {
                "source_id": str(uuid4()),
                **registration.model_dump(),
                "revision": 1,
                "status": "active",
            }
            db.execute(
                "INSERT INTO cmh_sources VALUES "
                "(:source_id,:device_id,:scope_id,:adapter_id,:display_name,:revision,:status)",
                source,
            )
            return source

    def list_sources(self) -> list[dict]:
        with self._database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM cmh_sources ORDER BY source_id")]

    def revoke_source(self, source_id: str, request: RevisionRequest) -> dict:
        with self._database(writing=True) as db:
            row = db.execute("SELECT * FROM cmh_sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None:
                raise AccessError("source_not_found", 404)
            if row["status"] != "active" or row["revision"] != request.expected_revision:
                raise AccessError("revision_conflict", 409)
            db.execute(
                "UPDATE cmh_sources SET status='revoked', revision=revision+1 WHERE source_id=?",
                (source_id,),
            )
            return {**dict(row), "status": "revoked", "revision": row["revision"] + 1}

    @staticmethod
    def _source_allowed(db, device, source_id: str, scope_id: str) -> bool:
        source = db.execute(
            "SELECT device_id,scope_id,status FROM cmh_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        return bool(
            source
            and tuple(source) == (device["device_id"], scope_id, "active")
            and device["status"] == "active"
            and any(
                grant["scope_id"] == scope_id and "events:write" in grant["permissions"]
                for grant in device["grants"]
            )
        )

    def source_write_authorizer(self, source_key: str) -> bool | None:
        if not source_key.startswith(SOURCE_PREFIX):
            return None
        try:
            with self._database() as db:
                source_id = source_key[len(SOURCE_PREFIX) :]
                row = db.execute(
                    "SELECT device_id,scope_id FROM cmh_sources WHERE source_id=?", (source_id,)
                ).fetchone()
                if row is None:
                    return False
                return self._source_allowed(db, self.access._receipt(db, row[0]), source_id, row[1])
        except AccessError:
            return False

    def source_access(self, credential: str, source_id: str) -> dict:
        """Current permission receipt, containing no content or other devices' sources."""
        with self._database() as db:
            device = self.access._authenticate(db, credential)
            source = db.execute(
                "SELECT * FROM cmh_sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if source is None or not self._source_allowed(
                db, device, source_id, source["scope_id"]
            ):
                raise AccessError("source_not_allowed", 403)
            return {
                "protocol_version": 1,
                "instance_id": self.access.instance_id,
                "device_id": device["device_id"],
                "device_revision": device["revision"],
                "source_id": source_id,
                "source_revision": source["revision"],
                "scope_id": source["scope_id"],
                "allowed": True,
            }

    def _queue(self):
        try:
            return self.queue_provider()
        except AttributeError:
            raise AccessError("runtime_not_ready", 503) from None

    def accept(self, credential: str, event: EventEnvelope) -> dict:
        if event.instance_id != self.access.instance_id:
            raise AccessError("instance_mismatch", 409)
        try:
            require_current_filter(event, self.privacy)
            request = write_request(event)
        except (EventError, ValidationError, ValueError):
            raise AccessError("event_content_not_accepted", 422) from None
        with self._database(writing=True) as db:
            device = self.access._authenticate(db, credential)
            if device["device_id"] != event.device_id or not self._source_allowed(
                db, device, event.source_id, event.scope_id
            ):
                raise AccessError("source_not_allowed", 403)
            row = db.execute(
                "SELECT * FROM cmh_event_receipts WHERE device_id=? AND event_id=?",
                (event.device_id, event.event_id),
            ).fetchone()
            digest = event.payload_digest()
            queue = self._queue()
            if row is not None:
                validate_original(row, self.access.instance_id)
                if row["payload_digest"] != digest:
                    raise AccessError("event_identity_conflict", 409)
                job = queue.get(row["request_id"])
                if job is None or job.job_id != row["receipt_id"]:
                    raise AccessError("receipt_unavailable", 503)
                return self._response(dict(row), job)
            if db.execute(
                "SELECT 1 FROM cmh_event_receipts WHERE source_id=? AND conversation_id=? "
                "AND record_id=? AND revision=?",
                (event.source_id, event.conversation_id, event.record_id, event.revision),
            ).fetchone():
                raise AccessError("record_revision_conflict", 409)
            try:
                job = queue.submit(request)
            except ValueError:
                raise AccessError("event_identity_conflict", 409) from None
            receipt = {
                "device_id": event.device_id,
                "event_id": event.event_id,
                "source_id": event.source_id,
                "scope_id": event.scope_id,
                "conversation_id": event.conversation_id,
                "record_id": event.record_id,
                "revision": event.revision,
                "payload_digest": digest,
                "receipt_id": job.job_id,
                "request_id": job.request_id,
                "payload_json": event.canonical_payload(),
            }
            db.execute(
                "INSERT INTO cmh_event_receipts VALUES "
                "(:device_id,:event_id,:source_id,:scope_id,:conversation_id,:record_id,"
                ":revision,:payload_digest,:receipt_id,:request_id,:payload_json)",
                receipt,
            )
            return self._response(receipt, job)

    def event_status(self, credential: str, event_id: str) -> dict:
        with self._database() as db:
            device = self.access._authenticate(db, credential)
            row = db.execute(
                "SELECT * FROM cmh_event_receipts WHERE device_id=? AND event_id=?",
                (device["device_id"], event_id),
            ).fetchone()
            if row is None:
                raise AccessError("event_not_found", 404)
            if not self._source_allowed(db, device, row["source_id"], row["scope_id"]):
                raise AccessError("source_not_allowed", 403)
            validate_original(row, self.access.instance_id)
            job = self._queue().get(row["request_id"])
            if job is None or job.job_id != row["receipt_id"]:
                raise AccessError("receipt_unavailable", 503)
            return self._response(dict(row), job)

    def _response(self, receipt, job) -> dict:
        return {
            "protocol_version": 1,
            "instance_id": self.access.instance_id,
            **{
                key: receipt[key]
                for key in ("device_id", "event_id", "payload_digest", "receipt_id")
            },
            "status": "accepted",
            "processing_state": job.state,
        }
