"""ADR-0042: classify one physical diagnostic record without I/O or deletion.

WriteRequest is a candidate selector, not a proof of provenance or authority.
Callers must verify the complete whole-write selection and signed RecordGrant
set. A local decision cannot establish file/domain coverage or finish a purge.
Kept records must be copied byte-for-byte by the caller, never reserialized.
"""

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from hy_memory.utils import diagnostic_policy as policy

from cloud_memory_hub.events import canonical_uuid, split_request_identity

MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_SELECTION = 10_000
RecordFormat = Literal["pipeline-jsonl-v1", "sdk-text-v1", "sdk-jsonl-v1"]
RecordKind = Literal["pipeline", "sdk"]
_FORMATS = frozenset(("pipeline-jsonl-v1", "sdk-text-v1", "sdk-jsonl-v1"))
_LEVELS = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
_READ_STEPS = frozenset(("GET", "LIST", "GET_USER_PROFILE", "GET_BASIC_INFO"))
_PIPELINE_KEYS = frozenset(
    "request_id namespace user_id agent_id session_id step prompt content parsed "
    "tokens elapsed_ms timestamp".split()
)
_TEXT = re.compile(
    r"(?P<ts>[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) "
    r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\] "
    r"\[(?P<request>[^\]\r\n]{1,256})\] "
    r"(?P<logger>hy_memory(?:\.[A-Za-z_][A-Za-z_0-9]*)*): (?P<message>[^\r\n]*)"
)
_LOGGER = re.compile(r"hy_memory(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
_LOCAL_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}")
_UTC_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?\+00:00"
)


def _selection_invalid():
    return ValueError("diagnostic_record_selection_invalid")


@dataclass(frozen=True, slots=True)
class WriteRequest:
    request_id: str
    user_id: str

    def __post_init__(self):
        try:
            if type(self.request_id) is not str or type(self.user_id) is not str:
                raise ValueError
            instance, *_ = split_request_identity(self.request_id)
            prefix, actual_instance, scope = self.user_id.split(":")
            if prefix != "cmh" or actual_instance != instance:
                raise ValueError
            if scope != "global":
                canonical_uuid(scope)
        except (ValueError, TypeError):
            raise _selection_invalid() from None


@dataclass(frozen=True, slots=True, order=True)
class RecordGrant:
    format: RecordFormat
    request_id: str
    sha256: str
    byte_length: int

    def __post_init__(self):
        try:
            if type(self.format) is not str or self.format not in _FORMATS:
                raise ValueError
            if type(self.request_id) is not str:
                raise ValueError
            split_request_identity(self.request_id)
            if type(self.sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
                raise ValueError
            if type(self.byte_length) is not int or not 0 < self.byte_length <= MAX_RECORD_BYTES:
                raise ValueError
        except (ValueError, TypeError):
            raise _selection_invalid() from None


@dataclass(frozen=True, slots=True)
class RecordDecision:
    action: Literal["candidate", "remove", "keep", "blocked"]
    grant: RecordGrant | None = None
    reason: str | None = None


def _blocked(reason="invalid"):
    return RecordDecision("blocked", reason="diagnostic_record_" + reason)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def _json(value):
    parsed = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
    pending, count = [(parsed, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 20_000:
            raise ValueError
        if type(item) is float and not math.isfinite(item):
            raise ValueError
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    return parsed


def _same(left, right):
    # Canonical JSON comparison distinguishes bool/int and checks every key.
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value)


def _time(value, pattern=None, *, text=False):
    if type(value) is not str or (pattern and not pattern.fullmatch(value)):
        return False
    try:
        if text:
            return (
                datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S") == value
            )
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _identity(value, *, request=False):
    return type(value) is str and (
        value == "" or policy.safe_identifier(value, request=request) == value
    )


def _pipeline_shape(value):
    if type(value) is not dict or set(value) - {"memory_ids"} != _PIPELINE_KEYS:
        return False
    strings = _PIPELINE_KEYS - {"tokens", "elapsed_ms"}
    if any(type(value[key]) is not str for key in strings):
        return False
    if not _number(value["tokens"]) or not _number(value["elapsed_ms"]):
        return False
    if not _time(value["timestamp"], _LOCAL_TIME):
        return False
    ids = value.get("memory_ids", [])
    return type(ids) is list and len(ids) <= 10_000 and all(type(item) is str for item in ids)


def _strict_pipeline(value, parsed):
    if value["namespace"] or value["prompt"] or value["content"]:
        return False
    if not _identity(value["request_id"], request=True):
        return False
    if any(not _identity(value[key]) for key in ("user_id", "agent_id", "session_id")):
        return False
    if type(value["tokens"]) is not int:
        return False
    safe = policy.project_pipeline_diagnostic(
        step=value["step"], memory_ids=value.get("memory_ids", [])
    )
    return (
        safe["step"] == value["step"]
        and safe["memory_ids"] == value.get("memory_ids", [])
        and _same(parsed, _json(policy.project_parsed(value["parsed"])))
    )


def _strict_sdk(value):
    if value == {
        "diagnostic_policy": "strict-v1",
        "content_omitted": True,
        "msg": "diagnostic_format_failed",
    }:
        return type(value["content_omitted"]) is bool
    required = {
        "diagnostic_policy",
        "content_omitted",
        "ts",
        "level",
        "logger",
        "msg",
        "request_id",
        "line",
    }
    optional = {"source", "error_type", "safe_stack"}
    if set(value) - optional != required:
        return False
    if value["diagnostic_policy"] != "strict-v1" or value["content_omitted"] is not True:
        return False
    if value["msg"] != "diagnostic_event" or not _time(value["ts"], _UTC_TIME):
        return False
    if type(value["level"]) is not str or value["level"] not in _LEVELS | {"UNKNOWN"}:
        return False
    if type(value["logger"]) is not str or policy._category(value["logger"]) != value["logger"]:
        return False
    if not _identity(value["request_id"], request=True):
        return False
    if type(value["line"]) is not int or not _number(value["line"]):
        return False
    # Native strict activation builds this trusted code inventory. If it is
    # unavailable, do not guess that an arbitrary relative path is metadata.
    if "source" in value and (
        type(value["source"]) is not str or value["source"] not in policy._KNOWN_SOURCES.values()
    ):
        return False
    if "error_type" in value and (
        type(value["error_type"]) is not str or value["error_type"] not in policy._ERROR_TYPES
    ):
        return False
    return "safe_stack" not in value or _same(
        value["safe_stack"], policy._safe_stack(value["safe_stack"])
    )


class RecordInspector:
    """Reuse one validated selection for streaming; no files or models are opened."""

    def __init__(self, requests, *, mode, grants=(), max_record_bytes=MAX_RECORD_BYTES):
        if (
            type(requests) is not tuple
            or len(requests) > MAX_SELECTION
            or any(type(item) is not WriteRequest for item in requests)
            or type(grants) is not tuple
            or len(grants) > MAX_SELECTION
            or any(type(item) is not RecordGrant for item in grants)
            or type(mode) is not str
            or mode not in {"propose", "authorized"}
            or (mode == "propose" and grants)
            or type(max_record_bytes) is not int
            or not 0 < max_record_bytes <= MAX_RECORD_BYTES
        ):
            raise _selection_invalid()
        self.requests = {item.request_id: item.user_id for item in requests}
        if len(self.requests) != len(requests) or any(
            item.request_id not in self.requests for item in grants
        ):
            raise _selection_invalid()
        self.mode, self.grants, self.max_record_bytes = mode, frozenset(grants), max_record_bytes

    def _legacy(self, raw, request, format):
        if request not in self.requests:
            return _blocked("coverage_unproven")
        grant = RecordGrant(format, request, sha256(raw).hexdigest(), len(raw))
        if self.mode == "propose":
            return RecordDecision("candidate", grant)
        return (
            RecordDecision("remove", grant) if grant in self.grants else _blocked("grant_missing")
        )

    def _pipeline(self, raw, value):
        if not _pipeline_shape(value):
            return _blocked()
        try:
            parsed = _json(value["parsed"])
        except (ValueError, TypeError, RecursionError):
            if value["parsed"].lstrip().startswith(("{", "[")):
                return _blocked()
            parsed = None  # Legacy parsed was allowed to be an opaque string.
        if type(parsed) is dict and ({"diagnostic_policy", "content_omitted"} & parsed.keys()):
            return RecordDecision("keep") if _strict_pipeline(value, parsed) else _blocked()
        if (
            value["step"] not in policy._STEPS
            or value["step"].startswith("READ_")
            or value["step"] in _READ_STEPS
        ):
            return _blocked("coverage_unproven")
        request = value["request_id"]
        if request in self.requests and (
            value["namespace"] != "" or value["user_id"] != self.requests[request]
        ):
            return _blocked("scope_conflict")
        return self._legacy(raw, request, "pipeline-jsonl-v1")

    def _sdk(self, raw, text):
        if text.startswith("{"):
            value = _json(text)
            if type(value) is not dict:
                return _blocked()
            if {"diagnostic_policy", "content_omitted"} & value.keys():
                return RecordDecision("keep") if _strict_sdk(value) else _blocked()
            required = {"ts", "level", "request_id", "logger", "msg"}
            if set(value) - {"exc"} != required or any(
                type(item) is not str for item in value.values()
            ):
                return _blocked()
            if (
                value["level"] not in _LEVELS
                or not _LOGGER.fullmatch(value["logger"])
                or not _time(value["ts"], text=True)
            ):
                return _blocked()
            return self._legacy(raw, value["request_id"], "sdk-jsonl-v1")
        match = _TEXT.fullmatch(text)
        if match is None or not _time(match["ts"], text=True):
            return _blocked()
        return self._legacy(raw, match["request"], "sdk-text-v1")

    def inspect(self, raw: bytes, *, kind: RecordKind) -> RecordDecision:
        if type(kind) is not str or kind not in {"pipeline", "sdk"}:
            raise _selection_invalid()
        if type(raw) is not bytes:
            return _blocked()
        if len(raw) > self.max_record_bytes:
            return _blocked("budget_exceeded")
        if not raw.endswith(b"\n") or b"\n" in raw[:-1] or b"\r" in raw:
            return _blocked()
        try:
            text = raw[:-1].decode("utf-8", errors="strict")
            return self._pipeline(raw, _json(text)) if kind == "pipeline" else self._sdk(raw, text)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return _blocked()


def inspect_record(
    raw: bytes,
    *,
    kind: RecordKind,
    requests: tuple[WriteRequest, ...],
    mode: Literal["propose", "authorized"],
    grants: tuple[RecordGrant, ...] = (),
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> RecordDecision:
    return RecordInspector(
        requests,
        mode=mode,
        grants=grants,
        max_record_bytes=max_record_bytes,
    ).inspect(raw, kind=kind)
