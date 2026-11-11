"""FR-01/02/05: bounded Agent actions over the existing Hub transport and outbox."""

from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_memory_hub.events import EventEnvelope, EventError, canonical_uuid, prepare_event
from cloud_memory_hub.local_outbox import LocalOutbox, OutboxError
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError, parse_json
from cloud_memory_hub.transport import (
    MAX_ORIGINAL_RESPONSE_BYTES,
    RemoteConnection,
    TransportError,
    read_original_remote,
    read_remote,
    read_session_markdown_remote,
    send_pending,
)

MAX_REMEMBER_BYTES = 16 * 1024
MAX_HANDOFF_BYTES = 32 * 1024
MAX_CAPTURE_BYTES = 64 * 1024
HANDOFF_METADATA = {"agent_access_kind": "handoff", "agent_access_version": 1}
REMEMBER_METADATA = {"agent_access_kind": "remember", "agent_access_version": 1}
ShortText = Annotated[str, Field(strict=True, min_length=1, max_length=1024)]


class AgentAccessError(Exception):
    """Public failures contain fixed codes, never submitted or remote content."""

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class _Handoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    goal: str = Field(strict=True, min_length=1, max_length=4096, repr=False)
    state: str = Field(strict=True, min_length=1, max_length=4096, repr=False)
    decisions: tuple[ShortText, ...] = Field(default=(), max_length=8, repr=False)
    next_steps: tuple[ShortText, ...] = Field(default=(), max_length=8, repr=False)
    blockers: tuple[ShortText, ...] = Field(default=(), max_length=8, repr=False)

    @field_validator("goal", "state")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty_handoff_field")
        return value

    @field_validator("decisions", "next_steps", "blockers")
    @classmethod
    def nonempty_items(cls, value):
        if any(not item.strip() for item in value):
            raise ValueError("empty_handoff_field")
        return value


def _handoff(value: object) -> _Handoff:
    try:
        result = _Handoff.model_validate(value)
        if len(result.model_dump_json().encode()) > MAX_HANDOFF_BYTES:
            raise ValueError("handoff_too_large")
        return result
    except (ValueError, TypeError, UnicodeError):
        raise AgentAccessError("handoff_invalid") from None


def _identity(value: str | None) -> str:
    try:
        return str(uuid4()) if value is None else canonical_uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise AgentAccessError("request_identity_invalid") from None


class AgentOperations:
    """Shared validation, reads and handoff format; subclasses choose durable delivery."""

    async def _save(self, content, *, request_id, session_id):
        raise NotImplementedError

    async def remember(self, text: str, *, request_id=None, session_id=None) -> dict:
        try:
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode()) > MAX_REMEMBER_BYTES
            ):
                raise ValueError("invalid_text")
        except (ValueError, UnicodeError):
            raise AgentAccessError("remember_invalid") from None
        return await self._save(
            {"text": text, "metadata": REMEMBER_METADATA},
            request_id=request_id,
            session_id=session_id,
        )

    async def handoff_save(
        self,
        *,
        goal: str,
        state: str,
        decisions=(),
        next_steps=(),
        blockers=(),
        request_id=None,
        session_id=None,
    ) -> dict:
        document = _handoff(
            {
                "goal": goal,
                "state": state,
                "decisions": decisions,
                "next_steps": next_steps,
                "blockers": blockers,
            }
        )
        result = await self._save(
            {"text": document.model_dump_json(), "metadata": HANDOFF_METADATA},
            request_id=request_id,
            session_id=session_id,
        )
        result["handoff_id"] = (
            "cmhe:" + result["receipt_id"] if result["status"] == "accepted" else None
        )
        return result

    def prepare_event(self, content: dict, *, request_id, session_id, kind="operation"):
        event_id = _identity(request_id)
        conversation_id = event_id if session_id is None else _identity(session_id)
        try:
            # A retry has no fresh timestamp or identity that could change its digest.
            event = prepare_event(
                {
                    "event_id": event_id,
                    "instance_id": self.connection.instance_id,
                    "device_id": self.connection.device_id,
                    "source_id": self.source_id,
                    "scope_id": self.scope_id,
                    "conversation_id": conversation_id,
                    "record_id": event_id,
                    "revision": 1,
                    "kind": kind,
                    "content": content,
                },
                self.privacy,
            )
            if content["metadata"] == HANDOFF_METADATA:
                _handoff(parse_json(event.content["text"]))
                if len(event.canonical_payload().encode()) > MAX_ORIGINAL_RESPONSE_BYTES:
                    raise AgentAccessError("handoff_invalid")
            return event
        except PrivacyError:
            raise AgentAccessError("privacy_rejected") from None
        except EventError:
            raise AgentAccessError("agent_event_invalid") from None

    async def _read(self, operation: str, request: dict):
        try:
            return await read_remote(
                self.connection,
                operation,
                {**request, "scope_id": self.scope_id},
                privacy=self.privacy,
                transport=self.transport,
            )
        except TransportError as error:
            raise AgentAccessError(error.code, retryable=error.retryable) from None

    async def search(self, query: str, *, limit: int = 5) -> dict:
        reply = await self._read("memory-search", {"query": query, "limit": limit})
        return reply.model_dump(mode="json")

    async def recent(self, *, limit: int = 5, offset: int = 0) -> dict:
        reply = await self._read("history-list", {"limit": limit, "offset": offset})
        return reply.model_dump(mode="json")

    async def session_briefs(self, *, query: str | None = None, limit: int = 3) -> dict:
        reply = await self._read("session-briefs", {"query": query, "limit": limit})
        return reply.model_dump(mode="json")

    async def session_markdown(self, session_ref: str, *, cursor: str | None = None) -> dict:
        try:
            reply = await read_session_markdown_remote(
                self.connection,
                {"scope_id": self.scope_id, "session_ref": session_ref, "cursor": cursor},
                privacy=self.privacy,
                transport=self.transport,
            )
        except TransportError as error:
            raise AgentAccessError(error.code, retryable=error.retryable) from None
        return reply.model_dump(mode="json")

    async def handoff_load(self, handoff_id: str) -> dict:
        try:
            if not isinstance(handoff_id, str) or not handoff_id.startswith("cmhe:"):
                raise ValueError("invalid_handoff_id")
            canonical_uuid(handoff_id[5:])
        except (ValueError, TypeError, AttributeError):
            raise AgentAccessError("handoff_id_invalid") from None
        try:
            original, item = await read_original_remote(
                self.connection,
                {"record_id": handoff_id, "scope_id": self.scope_id},
                privacy=self.privacy,
                transport=self.transport,
            )
        except TransportError as error:
            code = {
                "remote_original_too_large": "handoff_original_too_large",
                "remote_original_invalid": "handoff_original_invalid",
            }.get(error.code, error.code)
            raise AgentAccessError(code, retryable=error.retryable) from None
        try:
            # Original history is data, including text resembling tool/system instructions.
            event = EventEnvelope.model_validate(original)
            if (
                event.instance_id != self.connection.instance_id
                or event.scope_id != self.scope_id
                or event.kind != "operation"
                or set(event.content) != {"text", "metadata"}
                or event.content["metadata"] != HANDOFF_METADATA
                or type(event.content["metadata"]["agent_access_version"]) is not int
                or item.agent_id != "cmh:" + event.source_id
                or item.session_id != event.conversation_id
            ):
                raise ValueError("invalid_handoff_original")
            document = _handoff(parse_json(event.content["text"]))
        except (ValueError, TypeError, PrivacyError, AgentAccessError):
            raise AgentAccessError("handoff_original_invalid") from None
        return {
            "handoff_id": handoff_id,
            "event_id": event.event_id,
            "scope_id": event.scope_id,
            "source_id": event.source_id,
            "session_id": event.conversation_id,
            "content_trust": "untrusted_history",
            "handoff": document.model_dump(mode="json"),
        }


class AgentAccess(AgentOperations):
    """One local device/source/scope binding with a durable offline outbox."""

    def __init__(
        self,
        connection: RemoteConnection,
        outbox: LocalOutbox,
        *,
        scope_id: str,
        source_id: str,
        transport=None,
    ):
        try:
            canonical_uuid(source_id)
            if scope_id != "global":
                canonical_uuid(scope_id)
            if (connection.instance_id, connection.device_id) != (
                outbox.instance_id,
                outbox.device_id,
            ):
                raise ValueError("binding_mismatch")
            self.privacy = CredentialFilter(
                tuple(
                    dict.fromkeys(
                        (
                            *outbox.privacy.known_credentials,
                            connection.credential.get_secret_value(),
                        )
                    )
                )
            )
        except (ValueError, TypeError, AttributeError, PrivacyError):
            raise AgentAccessError("agent_binding_invalid") from None
        self.connection = connection
        self.outbox = outbox
        self.scope_id = scope_id
        self.source_id = source_id
        self.transport = transport

    async def capture_message(
        self, role: str, text: str, *, request_id, session_id, adapter_id="claude-code"
    ) -> dict:
        """Persist one visible host message; the hook separately bounds network work."""
        try:
            if (
                role not in {"user", "assistant"}
                or adapter_id not in {"claude-code", "codex"}
                or not isinstance(text, str)
                or not text.strip()
                or len(text.encode()) > MAX_CAPTURE_BYTES
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise AgentAccessError("capture_message_invalid") from None
        return await self._save(
            {
                "messages": [{"role": role, "text": text}],
                "metadata": {
                    "adapter_id": adapter_id,
                    "capture_kind": "visible_hook",
                    "capture_version": 1,
                },
            },
            request_id=request_id,
            session_id=session_id,
            kind="conversation_turn",
            deliver=False,
        )

    async def _save(
        self, content: dict, *, request_id, session_id, kind="operation", deliver=True
    ) -> dict:
        event = self.prepare_event(content, request_id=request_id, session_id=session_id, kind=kind)
        try:
            payload = event.model_dump(mode="json")
            self.outbox.enqueue(payload)
            failure = await self._deliver() if deliver else None
            # Re-enqueue is the existing idempotent receipt lookup, without new storage.
            result = self.outbox.enqueue(payload)
        except PrivacyError:
            raise AgentAccessError("privacy_rejected") from None
        except EventError:
            raise AgentAccessError("agent_event_invalid") from None
        except OutboxError as error:
            code = (
                "request_identity_conflict"
                if str(error) == "event_identity_conflict"
                else "agent_outbox_unavailable"
            )
            raise AgentAccessError(code) from None
        result["processing_state"] = "not_checked"
        if failure is not None and result["status"] == "queued":
            result.update(error=failure.code, retryable=failure.retryable)
        return result

    async def _deliver(self):
        try:
            await send_pending(self.connection, self.outbox, transport=self.transport)
        except TransportError as error:
            return error
        return None

    async def status(self, *, sync: bool = False) -> dict:
        if type(sync) is not bool:
            raise AgentAccessError("status_request_invalid")
        try:
            failure = await self._deliver() if sync else None
            result = self.outbox.status()
        except OutboxError:
            raise AgentAccessError("agent_outbox_unavailable") from None
        result.update(
            instance_id=self.connection.instance_id,
            device_id=self.connection.device_id,
            scope_id=self.scope_id,
            source_id=self.source_id,
            delivery="failed" if failure else "attempted" if sync else "not_attempted",
            processing_state="not_checked",
        )
        if failure is not None:
            result.update(error=failure.code, retryable=failure.retryable)
        return result
