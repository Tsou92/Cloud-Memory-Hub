"""Transport identity and filtered event contract; not a memory model."""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from cloud_memory_hub.privacy import POLICY_VERSION, CredentialFilter, PrivacyError


class EventError(Exception):
    pass


def canonical_uuid(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid_identifier")
    return value


def split_request_identity(value: str) -> tuple[str, str, str, str | None]:
    """Transport identity only; a cmh2 birth still requires its signed admission."""
    if not isinstance(value, str):
        raise ValueError("invalid_request_identity")
    parts = value.split(":")
    if len(parts) == 4 and parts[0] == "cmh":
        instance, device, event = (canonical_uuid(item) for item in parts[1:])
        return instance, device, event, None
    if len(parts) == 5 and parts[0] == "cmh2":
        instance, birth, device, event = (canonical_uuid(item) for item in parts[1:])
        return instance, device, event, birth
    raise ValueError("invalid_request_identity")


def require_event_request_identity(value: str, event: "EventEnvelope") -> str:
    instance, device, event_id, _ = split_request_identity(value)
    if (instance, device, event_id) != (event.instance_id, event.device_id, event.event_id):
        raise ValueError("request_event_mismatch")
    return value


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    protocol_version: Literal[1] = 1
    event_id: str
    instance_id: str
    device_id: str
    source_id: str
    scope_id: str
    conversation_id: str
    record_id: str
    revision: int = Field(strict=True, ge=1, le=2**53 - 1)
    kind: Literal["conversation_turn", "operation"]
    occurred_at: datetime | None = None
    content: dict[str, JsonValue] = Field(repr=False, min_length=1)
    privacy_policy: Literal[POLICY_VERSION] = POLICY_VERSION

    @field_validator("protocol_version", mode="before")
    @classmethod
    def strict_protocol_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported_protocol_version")
        return value

    @field_validator(
        "event_id", "instance_id", "device_id", "source_id", "conversation_id", "record_id"
    )
    @classmethod
    def valid_uuid(cls, value):
        return canonical_uuid(value)

    @field_validator("scope_id")
    @classmethod
    def valid_scope(cls, value):
        return value if value == "global" else canonical_uuid(value)

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value

    def canonical_payload(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def payload_digest(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode()).hexdigest()


def prepare_event(raw: object, privacy: CredentialFilter) -> EventEnvelope:
    if not isinstance(raw, dict) or "content" not in raw:
        raise EventError("invalid_event")
    try:
        filtered = privacy.protect(raw["content"])
        return EventEnvelope.model_validate({**raw, "content": filtered.value})
    except (ValidationError, ValueError, TypeError):
        raise EventError("invalid_event") from None


def require_current_filter(event: EventEnvelope, privacy: CredentialFilter) -> None:
    try:
        filtered = privacy.protect(event.content)
        if filtered.value != event.content:
            raise EventError("event_requires_refiltering")
    except PrivacyError:
        raise EventError("event_requires_refiltering") from None
