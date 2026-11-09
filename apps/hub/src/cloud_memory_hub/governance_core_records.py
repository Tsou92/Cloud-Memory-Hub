"""ADR-0047: pure Core byte classification and bounded physical framing.

A strict fragment proves only a closed metadata shape. Raw fragments are not
logical log events, source attribution, or deletion grants. Callers must finish
the complete bounded scan before using any frames yielded by an earlier feed.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from hy_memory.utils import diagnostic_policy as policy

from cloud_memory_hub.governance_diagnostic_records import (
    MAX_RECORD_BYTES,
    _json,
    _same,
    _strict_sdk,
)

MAX_RAW_BLOCK_BYTES = 32 * 1024 * 1024
CoreDomain = Literal["core-log", "core-diagnostic"]
FrameKind = Literal["strict", "raw"]
_DOMAINS = frozenset(("core-log", "core-diagnostic"))
_CORE_REQUIRED = frozenset(
    "schema_version diagnostic_policy content_omitted timestamp level service "
    "component event outcome".split()
)
_CORE_OPTIONAL = frozenset(
    "session_id trace_id incident_id instance_id operation_id request_id job_id task_id "
    "duration_ms error_type safe_stack details".split()
)


class CoreRecordError(ValueError):
    """Fixed errors without any raw diagnostic content."""


def _error(code):
    return CoreRecordError("diagnostic_core_" + code)


def _require_domain(domain):
    if type(domain) is not str or domain not in _DOMAINS:
        raise _error("invalid")


def _strict_core(value):
    if set(value) - _CORE_OPTIONAL != _CORE_REQUIRED:
        return False
    timestamp = value["timestamp"]
    if type(timestamp) is not str or len(timestamp) > 40:
        return False
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None or parsed.isoformat() != timestamp:
        return False
    # A canonical aware timestamp guarantees the native projector cannot take
    # its current-time fallback. Exact comparison checks every field and type,
    # including bool/int differences, rather than trusting the strict marker.
    return _same(value, policy.project_core_diagnostic(value))


def classify_core_line(raw: bytes, *, domain: CoreDomain) -> FrameKind:
    """Recognize only complete LF metadata lines; preserve everything else raw."""
    _require_domain(domain)
    if type(raw) is not bytes:
        raise _error("invalid")
    if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n") or b"\n" in raw[:-1] or b"\r" in raw:
        return "raw"
    try:
        value = _json(raw[:-1].decode("utf-8", errors="strict"))
        if type(value) is not dict:
            return "raw"
        strict = _strict_sdk(value) if domain == "core-log" else _strict_core(value)
        return "strict" if strict else "raw"
    except (ValueError, TypeError, RecursionError, OverflowError):
        return "raw"


@dataclass(frozen=True, slots=True)
class CoreFrame:
    domain: CoreDomain
    offset: int
    raw: bytes = field(repr=False)
    kind: FrameKind


class CoreFramer:
    """Split only at strict LF lines, retaining maximal intervening raw blocks.

    Lines exceeding max_line_bytes become raw without being JSON-decoded.
    Raw blocks have a separate bound, so native 4 MiB legacy files are usable.
    A failed call returns no frames, poisons the instance, and cannot be retried.
    The caller separately bounds whole-input bytes, file count and frame count.
    """

    def __init__(
        self,
        domain: CoreDomain,
        *,
        max_line_bytes: int = MAX_RECORD_BYTES,
        max_raw_block_bytes: int = MAX_RAW_BLOCK_BYTES,
    ):
        _require_domain(domain)
        if (
            type(max_line_bytes) is not int
            or not 0 < max_line_bytes <= MAX_RECORD_BYTES
            or type(max_raw_block_bytes) is not int
            or not 0 < max_raw_block_bytes <= MAX_RAW_BLOCK_BYTES
        ):
            raise _error("invalid")
        self.domain = domain
        self.max_line_bytes = max_line_bytes
        self.max_raw_block_bytes = max_raw_block_bytes
        self._line = bytearray()
        self._raw = bytearray()
        self._long_line = False
        self._offset = 0
        self._state = "open"

    def _require_open(self):
        if self._state != "open":
            raise _error("framer_" + self._state)

    def _fail(self):
        self._state = "failed"
        self._line.clear()
        self._raw.clear()

    def _append_raw(self, value):
        if len(self._raw) + len(value) > self.max_raw_block_bytes:
            raise _error("budget_exceeded")
        self._raw.extend(value)

    def _emit_raw(self, frames):
        if self._raw:
            frames.append(CoreFrame(self.domain, self._offset, bytes(self._raw), "raw"))
            self._offset += len(self._raw)
            self._raw.clear()

    def _end_line(self, frames):
        if not self._long_line:
            line = bytes(self._line)
            if classify_core_line(line, domain=self.domain) == "strict":
                self._emit_raw(frames)
                frames.append(CoreFrame(self.domain, self._offset, line, "strict"))
                self._offset += len(line)
            else:
                self._append_raw(self._line)
            self._line.clear()
        self._long_line = False

    def feed(self, chunk: bytes) -> tuple[CoreFrame, ...]:
        self._require_open()
        try:
            if type(chunk) is not bytes:
                raise _error("invalid")
            frames = []
            position = 0
            view = memoryview(chunk)
            while position < len(chunk):
                newline = chunk.find(b"\n", position)
                end = len(chunk) if newline < 0 else newline + 1
                piece = view[position:end]
                if not self._long_line and len(self._line) + len(piece) > self.max_line_bytes:
                    self._append_raw(self._line)
                    self._line.clear()
                    self._long_line = True
                if self._long_line:
                    self._append_raw(piece)
                else:
                    self._line.extend(piece)
                if newline >= 0:
                    self._end_line(frames)
                position = end
            return tuple(frames)
        except BaseException:
            self._fail()
            raise

    def finish(self) -> tuple[CoreFrame, ...]:
        self._require_open()
        try:
            # Without LF, even an otherwise strict-looking suffix is raw.
            self._append_raw(self._line)
            self._line.clear()
            frames = []
            self._emit_raw(frames)
            self._state = "closed"
            return tuple(frames)
        except BaseException:
            self._fail()
            raise
