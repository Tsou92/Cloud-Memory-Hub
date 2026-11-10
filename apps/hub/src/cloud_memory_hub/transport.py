"""Bounded, authenticated delivery of the already-filtered local outbox."""

import asyncio
import ipaddress
import json
import os
import random
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from one_memory.http_client import create_async_client
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from cloud_memory_hub.events import (
    EventEnvelope,
    EventError,
    canonical_uuid,
    require_current_filter,
)
from cloud_memory_hub.local_outbox import TERMINAL_REJECTION_CODE, LocalOutbox, OutboxError
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError, parse_json
from cloud_memory_hub.read_contract import (
    READ_OPERATIONS,
    TEXT_BLOCK,
    GetRequest,
    ListRequest,
    ReadItem,
    ReadReply,
)
from cloud_memory_hub.remote_access import TOKEN_PATTERN, ScopeGrant

MAX_RESPONSE_BYTES = 128 * 1024
MAX_ORIGINAL_RESPONSE_BYTES = 64 * 1024
REQUEST_DEADLINE = 20.0
MAX_REJECTION_RESPONSE_BYTES = 4096


class TransportError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class RemoteConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    origin: str = Field(strict=True)
    instance_id: str
    device_id: str
    credential: SecretStr = Field(repr=False)

    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and type(value.get("schema_version", 1)) is not int:
            raise ValueError("invalid_connection_version")
        return value

    @model_validator(mode="after")
    def validated(self):
        canonical_uuid(self.instance_id)
        canonical_uuid(self.device_id)
        parts = urlsplit(self.origin)
        if (
            not self.origin.isascii()
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in self.origin
            )
            or parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or "?" in self.origin
            or "#" in self.origin
            or "\\" in self.origin
            or parts.netloc.endswith(":")
            or (parts.port is not None and not 1 <= parts.port <= 65535)
        ):
            raise ValueError("invalid_connection_origin")
        loopback = parts.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            pass
        if parts.scheme == "http" and not loopback:
            raise ValueError("https_required")
        # HTTPX must accept the exact origin before any credential is attached.
        url = httpx.URL(self.origin)
        if url.userinfo or url.query or url.fragment or url.path != "/":
            raise ValueError("invalid_connection_origin")
        match = TOKEN_PATTERN.fullmatch(self.credential.get_secret_value())
        if match is None or match[1] != self.device_id:
            raise ValueError("invalid_device_credential")
        return self

    @classmethod
    def from_file(cls, path: Path):
        try:
            # Non-blocking open lets fstat reject FIFOs before a writer can hang the watcher.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                ):
                    raise ValueError("unsafe_connection_file")
                raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError("connection_file_too_large")
            return cls.model_validate(parse_json(raw))
        except (OSError, ValueError, PrivacyError, httpx.InvalidURL):
            raise TransportError("connection_configuration_invalid") from None


def _http_failure(status: int) -> TransportError:
    if status in {408, 425, 429} or 500 <= status <= 599:
        return TransportError("remote_temporarily_unavailable", retryable=True)
    return TransportError(
        {
            401: "device_authentication_required",
            403: "source_permission_required",
            404: "remote_endpoint_unavailable",
            409: "remote_identity_conflict",
            413: "remote_payload_too_large",
            422: "remote_payload_rejected",
        }.get(status, "remote_protocol_invalid")
    )


async def _terminal_upload_rejection(response, path, payload, expected):
    if not (
        path == "/hub/v1/events"
        and payload is not None
        and expected == 202
        and response.status_code == 409
        and response.headers.get("content-type", "").split(";")[0].lower() == "application/json"
        and response.headers.get("content-encoding", "identity").lower() == "identity"
    ):
        return False
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > MAX_REJECTION_RESPONSE_BYTES:
            return False
        body.extend(chunk)
    try:
        return parse_json(body) == {"detail": TERMINAL_REJECTION_CODE}
    except PrivacyError:
        return False


async def _request(client, connection, path, *, payload=None, expected=200):
    try:
        async with asyncio.timeout(REQUEST_DEADLINE):
            async with client.stream(
                "GET" if payload is None else "POST",
                connection.origin + path,
                content=payload.encode() if payload is not None else None,
                headers={
                    "Authorization": "Bearer " + connection.credential.get_secret_value(),
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code != expected:
                    if await _terminal_upload_rejection(response, path, payload, expected):
                        raise TransportError(TERMINAL_REJECTION_CODE)
                    raise _http_failure(response.status_code)
                if (
                    response.headers.get("content-type", "").split(";")[0].lower()
                    != "application/json"
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise TransportError("remote_protocol_invalid")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise TransportError("remote_response_too_large")
                    body.extend(chunk)
                value = parse_json(body)
                if not isinstance(value, dict):
                    raise TransportError("remote_protocol_invalid")
                return value
    except (httpx.HTTPError, OSError, TimeoutError):
        raise TransportError("transport_unavailable", retryable=True) from None
    except (PrivacyError, ValueError):
        raise TransportError("remote_protocol_invalid") from None


def _identity(value, connection):
    if (
        type(value.get("protocol_version")) is not int
        or value["protocol_version"] != 1
        or value.get("instance_id") != connection.instance_id
    ):
        raise TransportError("remote_instance_mismatch")


async def verify_connection(connection, *, scope_id, source_id, transport=None):
    """Read-only pairing probe; an empty delivery queue is not connectivity evidence."""
    try:
        ScopeGrant.valid_scope(scope_id)
        canonical_uuid(source_id)
    except (ValueError, TypeError, AttributeError):
        raise TransportError("connection_binding_mismatch") from None
    async with create_async_client(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10, connect=5),
        transport=transport,
    ) as client:
        session = await _request(client, connection, "/hub/v1/session")
        _identity(session, connection)
        device, features = session.get("device"), session.get("features")
        if (
            not isinstance(device, dict)
            or device.get("device_id") != connection.device_id
            or device.get("status") != "active"
            or not isinstance(features, list)
            or not all(isinstance(item, str) for item in features)
            or not {"device_identity", "event_upload", "source_preflight"}.issubset(features)
            or not isinstance(device.get("grants"), list)
        ):
            raise TransportError("remote_session_invalid")
        try:
            grants = [ScopeGrant.model_validate(item) for item in device["grants"]]
        except (ValueError, TypeError):
            raise TransportError("remote_session_invalid") from None
        if not any(
            grant.scope_id == scope_id
            and {"events:write", "memory:read", "history:read"}.issubset(grant.permissions)
            for grant in grants
        ):
            raise TransportError("source_permission_required")
        permission = await _request(client, connection, f"/hub/v1/sources/{source_id}/access")
        _identity(permission, connection)
        if (
            permission.get("device_id") != connection.device_id
            or permission.get("source_id") != source_id
            or permission.get("scope_id") != scope_id
            or permission.get("allowed") is not True
        ):
            raise TransportError("source_permission_required")
    return {"status": "authorized", "checked": "session_and_source", "scope_id": scope_id}


async def send_pending(connection: RemoteConnection, outbox: LocalOutbox, *, transport=None) -> int:
    """One bounded batch. Only matching durable receipts acknowledge local records."""
    if (connection.instance_id, connection.device_id) != (outbox.instance_id, outbox.device_id):
        raise TransportError("connection_binding_mismatch")
    pending = outbox.pending()
    if not pending:
        return 0
    privacy = CredentialFilter(
        tuple(
            dict.fromkeys(
                (*outbox.privacy.known_credentials, connection.credential.get_secret_value())
            )
        )
    )
    sent = 0
    async with create_async_client(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10, connect=5),
        transport=transport,
    ) as client:
        session = await _request(client, connection, "/hub/v1/session")
        _identity(session, connection)
        device, features = session.get("device"), session.get("features")
        if (
            not isinstance(device, dict)
            or device.get("device_id") != connection.device_id
            or device.get("status") != "active"
            or not isinstance(features, list)
            or not all(isinstance(feature, str) for feature in features)
            or not {"device_identity", "event_upload", "source_preflight"}.issubset(features)
        ):
            raise TransportError("remote_session_invalid")
        for item in pending:
            event = EventEnvelope.model_validate_json(item.payload)
            try:
                require_current_filter(event, privacy)
            except EventError:
                raise TransportError("outbox_payload_requires_review") from None
            permission = await _request(
                client, connection, f"/hub/v1/sources/{event.source_id}/access"
            )
            _identity(permission, connection)
            if (
                permission.get("device_id") != connection.device_id
                or permission.get("source_id") != event.source_id
                or permission.get("scope_id") != event.scope_id
                or permission.get("allowed") is not True
            ):
                raise TransportError("source_permission_required")
            try:
                receipt = await _request(
                    client, connection, "/hub/v1/events", payload=item.payload, expected=202
                )
            except TransportError as error:
                if error.code != TERMINAL_REJECTION_CODE:
                    raise
                try:
                    outbox.reject_delivery(
                        instance_id=connection.instance_id,
                        device_id=connection.device_id,
                        event_id=item.event_id,
                        payload_digest=item.payload_digest,
                        code=error.code,
                    )
                except OutboxError:
                    raise TransportError("remote_rejection_not_saved") from None
                continue
            _identity(receipt, connection)
            if (
                receipt.get("status") != "accepted"
                or receipt.get("device_id") != connection.device_id
                or receipt.get("event_id") != item.event_id
                or receipt.get("payload_digest") != item.payload_digest
            ):
                raise TransportError("remote_receipt_mismatch")
            try:
                outbox.acknowledge(
                    instance_id=connection.instance_id,
                    device_id=connection.device_id,
                    event_id=item.event_id,
                    payload_digest=item.payload_digest,
                    receipt_id=receipt.get("receipt_id"),
                )
            except OutboxError:
                raise TransportError("remote_receipt_not_saved") from None
            sent += 1
    return sent


async def _read_remote_verified(
    connection: RemoteConnection,
    operation: str,
    raw: object,
    *,
    privacy: CredentialFilter | None = None,
    transport=None,
) -> tuple[ReadReply, CredentialFilter]:
    """Verify a page; only public wrappers may return its filtered content."""
    if operation not in READ_OPERATIONS:
        raise TransportError("read_operation_invalid")
    path, model = READ_OPERATIONS[operation]
    privacy = CredentialFilter(
        tuple(
            dict.fromkeys(
                (
                    *(privacy.known_credentials if privacy else ()),
                    connection.credential.get_secret_value(),
                )
            )
        )
    )
    try:
        request = model.model_validate(raw)
        request = model.model_validate(privacy.protect(request.model_dump(mode="json")).value)
    except (ValueError, PrivacyError):
        raise TransportError("read_request_invalid") from None
    permission = "history:read" if operation.startswith("history-") else "memory:read"
    async with create_async_client(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10, connect=5),
        transport=transport,
    ) as client:
        session = await _request(client, connection, "/hub/v1/session")
        _identity(session, connection)
        device, features = session.get("device"), session.get("features")
        if (
            not isinstance(device, dict)
            or device.get("device_id") != connection.device_id
            or device.get("status") != "active"
            or type(device.get("revision")) is not int
            or not isinstance(features, list)
            or "scoped_reads" not in features
        ):
            raise TransportError("remote_session_invalid")
        grants = device.get("grants")
        if not isinstance(grants, list) or not any(
            isinstance(grant, dict)
            and grant.get("scope_id") == request.scope_id
            and isinstance(grant.get("permissions"), list)
            and permission in grant["permissions"]
            for grant in grants
        ):
            raise TransportError("scope_permission_required")
        value = await _request(client, connection, path, payload=request.model_dump_json())
    _identity(value, connection)
    try:
        result = ReadReply.model_validate(value)
    except ValueError:
        raise TransportError("remote_read_invalid") from None
    if (
        result.device_id != connection.device_id
        or result.scope_id != request.scope_id
        or result.operation != operation
        or result.device_revision != device["revision"]
    ):
        raise TransportError("remote_read_mismatch")
    expected_kind = "history" if operation.startswith("history-") else "memory"
    expected_offset = request.content_offset if isinstance(request, GetRequest) else 0
    if any(
        item.kind != expected_kind
        or item.content_offset != expected_offset
        or (
            item.next_content_offset is not None
            and item.next_content_offset != item.content_offset + len(item.content)
        )
        or item.next_content_offset == item.content_offset
        for item in result.items
    ):
        raise TransportError("remote_read_mismatch")
    if isinstance(request, GetRequest):
        if len(result.items) != 1 or result.items[0].record_id != request.record_id:
            raise TransportError("remote_read_mismatch")
    elif len(result.items) > request.limit:
        raise TransportError("remote_read_mismatch")
    expected_next = request.offset + request.limit if isinstance(request, ListRequest) else None
    if result.next_offset is not None and result.next_offset != expected_next:
        raise TransportError("remote_read_mismatch")
    # This private helper returns unmodified fragments only to the filtering wrappers.
    value = result.model_dump(mode="json")
    for item in value["items"]:
        item.pop("content")
    try:
        if privacy.protect(value).value != value:
            raise TransportError("remote_read_invalid")
    except (ValueError, PrivacyError):
        raise TransportError("remote_read_invalid") from None
    return result, privacy


async def read_remote(
    connection: RemoteConnection,
    operation: str,
    raw: object,
    *,
    privacy: CredentialFilter | None = None,
    transport=None,
) -> ReadReply:
    """Filter locally, prove scope access before sending a query, then verify the reply."""
    result, privacy = await _read_remote_verified(
        connection, operation, raw, privacy=privacy, transport=transport
    )
    # This output is data. Callers must not treat stored history as tool/system instructions.
    try:
        value = result.model_dump(mode="json")
        contents = [item.pop("content") for item in value["items"]]
        for item, content in zip(value["items"], contents, strict=True):
            paged = item["content_offset"] != 0 or item["next_content_offset"] is not None
            item["content"] = (
                privacy.protect_text_fragment(content) if paged else privacy.protect(content).value
            )
        return ReadReply.model_validate(value)
    except (ValueError, PrivacyError):
        raise TransportError("remote_read_invalid") from None


async def read_original_remote(
    connection: RemoteConnection,
    raw: object,
    *,
    privacy: CredentialFilter | None = None,
    transport=None,
) -> tuple[dict, ReadItem]:
    """Read one bounded JSON original; fragments never escape before complete filtering."""
    try:
        request = GetRequest.model_validate(raw)
        if request.content_offset != 0:
            raise ValueError("original_requires_start")
    except ValueError:
        raise TransportError("read_request_invalid") from None
    parts, offset, total_bytes, metadata = [], 0, 0, None
    for _ in range(MAX_ORIGINAL_RESPONSE_BYTES // TEXT_BLOCK):
        reply, complete_privacy = await _read_remote_verified(
            connection,
            "history-get",
            {**request.model_dump(), "content_offset": offset},
            privacy=privacy,
            transport=transport,
        )
        item = reply.items[0]
        current = item.model_dump(exclude={"content", "content_offset", "next_content_offset"})
        if metadata is not None and metadata != current:
            raise TransportError("remote_original_invalid")
        metadata = current
        try:
            total_bytes += len(item.content.encode())
        except UnicodeError:
            raise TransportError("remote_original_invalid") from None
        if total_bytes > MAX_ORIGINAL_RESPONSE_BYTES:
            raise TransportError("remote_original_too_large")
        parts.append(item.content)
        if item.next_content_offset is None:
            break
        offset = item.next_content_offset
    else:
        raise TransportError("remote_original_too_large")
    try:
        original = parse_json("".join(parts))
        if not isinstance(original, dict):
            raise ValueError("original_not_object")
        original = complete_privacy.protect(original).value
        if len(json.dumps(original, ensure_ascii=False).encode()) > MAX_ORIGINAL_RESPONSE_BYTES:
            raise TransportError("remote_original_too_large")
    except (ValueError, PrivacyError):
        raise TransportError("remote_original_invalid") from None
    # Return metadata without retaining an unfiltered final content fragment.
    return original, item.model_copy(update={"content": ""})


async def watch_delivery(attempt, report, *, sleep=asyncio.sleep):
    """Retry transient failures; cancellation leaves unacknowledged events queued."""
    failures, previous = 0, None
    while True:
        try:
            accepted = await attempt()
            failures = 0
            state = {"status": "accepted" if accepted else "idle", "accepted": accepted}
            delay = 0 if accepted == 32 else 2.0
        except TransportError as error:
            if not error.retryable:
                raise
            failures = min(failures + 1, 6)
            delay = min(30.0, 2 ** (failures - 1) * random.uniform(0.8, 1.2))
            state = {"status": "retrying", "error": error.code}
        if state != previous or state.get("accepted", 0):
            report(state)
            previous = state
        await sleep(delay)
