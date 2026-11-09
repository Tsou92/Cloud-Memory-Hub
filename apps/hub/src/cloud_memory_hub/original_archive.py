"""Accepted event integrity and explicit migration from frozen upstream inputs."""

import json

from one_memory.memory_server.contracts import WriteRequest
from one_memory.memory_server.durable_queue import DurableWriteQueue

from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.remote_access import AccessError

ARCHIVE_VERSION = 2
_IGNORED_REQUEST_FIELDS = {"event_at", "time_quality", "priority"}


def validate_original(row, instance_id):
    try:
        event = EventEnvelope.model_validate_json(row["payload_json"])
        if (
            event.instance_id != instance_id
            or event.canonical_payload() != row["payload_json"]
            or event.payload_digest() != row["payload_digest"]
            or row["request_id"] != f"cmh:{instance_id}:{event.device_id}:{event.event_id}"
            or any(
                getattr(event, key) != row[key]
                for key in (
                    "device_id",
                    "event_id",
                    "source_id",
                    "scope_id",
                    "conversation_id",
                    "record_id",
                    "revision",
                )
            )
        ):
            raise ValueError("original_mismatch")
        return event
    except (ValueError, TypeError, KeyError):
        raise AccessError("original_archive_invalid", 503) from None


def create_archive_indexes(db):
    db.execute("CREATE UNIQUE INDEX cmh_receipt_identity ON cmh_event_receipts(receipt_id)")
    db.execute("CREATE INDEX cmh_original_scope ON cmh_event_receipts(scope_id)")


def require_frozen_match(event: EventEnvelope, request: WriteRequest | None) -> None:
    """Prove the entire frozen input, allowing only existing scheduling differences."""
    # Import lazily: the ingest boundary calls validate_original during replay.
    from cloud_memory_hub.remote_ingest import write_request

    try:
        if not isinstance(request, WriteRequest) or not request.input_records:
            raise ValueError("missing_frozen_inputs")
        rebuilt = write_request(event)
        if rebuilt.model_dump(exclude=_IGNORED_REQUEST_FIELDS) != request.model_dump(
            exclude=_IGNORED_REQUEST_FIELDS
        ):
            raise ValueError("frozen_input_mismatch")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise AccessError("original_migration_source_unproven", 409) from None


def _event_from_footer(request):
    try:
        if not isinstance(request, WriteRequest) or not request.input_records:
            raise ValueError("missing_frozen_inputs")
        header, messages, metadata = None, [], None
        for index, record in enumerate(request.input_records):
            text, marker, tail = record.verbatim.rpartition("\n\n<cmh_source_reference>\n")
            footer = "\n</cmh_source_reference>"
            if not marker or not tail.endswith(footer):
                raise ValueError("missing_source_footer")
            provenance = json.loads(tail[: -len(footer)])
            current = provenance["event"]
            if provenance["message_index"] != index or (header is not None and current != header):
                raise ValueError("source_footer_mismatch")
            header = current
            message = {"role": record.role, "text": text}
            if "message_metadata" in provenance:
                message["metadata"] = provenance["message_metadata"]
            if index == 0 and "content_metadata" in provenance:
                metadata = provenance["content_metadata"]
            messages.append(message)
        content = (
            {"messages": messages}
            if header["kind"] == "conversation_turn"
            else {"text": messages[0]["text"]}
        )
        if metadata is not None:
            content["metadata"] = metadata
        event = EventEnvelope.model_validate({**header, "content": content})
        event.canonical_payload()
        return event
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise AccessError("original_migration_source_unproven", 409) from None


def recover_event(request: WriteRequest | None) -> EventEnvelope:
    """Recover queue input identity, never proof that a receipt was accepted."""
    event = _event_from_footer(request)
    require_frozen_match(event, request)
    return event


def _recover_original(request, row, instance_id):
    # Keep real receipt validation before frozen matching, preserving the old
    # archive-invalid error priority when both the receipt and input are damaged.
    event = _event_from_footer(request)
    # Missing optional empty objects cannot be guessed. Only exact equality proves
    # recovery; an unprovable old representation requires an original source replay.
    validate_original({**dict(row), "payload_json": event.canonical_payload()}, instance_id)
    require_frozen_match(event, request)
    return event.canonical_payload()


def migrate_originals(config):
    """Caller must hold all storage leases with writers stopped; no Core startup."""
    authority = open_authority(config)
    authority.require_ready()
    access = authority.access
    queue = DurableWriteQueue(
        config.data_dir / "one-memory.sqlite3", initialize=False, sdk_authority=True
    )
    try:
        with access._connection(writing=True) as db:
            found = db.execute(
                "SELECT name FROM sqlite_master WHERE name='cmh_ingest_schema'"
            ).fetchone()
            if found is None:
                if db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('cmh_sources','cmh_event_receipts')"
                ).fetchone():
                    raise AccessError("ingest_store_unrecognized", 503)
                # No remote intake was initialized; serve will create the new schema.
                return 0
            version = db.execute("SELECT version FROM cmh_ingest_schema WHERE id=1").fetchone()
            if version is None or version[0] not in (1, ARCHIVE_VERSION):
                raise AccessError("ingest_store_version_unsupported", 503)
            if version[0] == ARCHIVE_VERSION:
                for row in db.execute("SELECT * FROM cmh_event_receipts"):
                    validate_original(row, access.instance_id)
                return 0
            db.execute(
                "ALTER TABLE cmh_event_receipts ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''"
            )
            count = 0
            for row in db.execute("SELECT * FROM cmh_event_receipts ORDER BY rowid"):
                request = queue.payload_for(row["receipt_id"])
                payload = _recover_original(request, row, access.instance_id)
                db.execute(
                    "UPDATE cmh_event_receipts SET payload_json=? WHERE receipt_id=?",
                    (payload, row["receipt_id"]),
                )
                count += 1
            create_archive_indexes(db)
            db.execute("UPDATE cmh_ingest_schema SET version=? WHERE id=1", (ARCHIVE_VERSION,))
            return count
    except AccessError:
        raise
    except Exception:
        raise AccessError("original_migration_source_unproven", 409) from None
