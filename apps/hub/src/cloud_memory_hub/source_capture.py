"""Explicit project capture, composing upstream source adapters and local delivery."""

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from one_memory.imports.access import AuthorizedSourceRoot, SourceAccessError, opaque_workspace_id
from one_memory.imports.adapters import ScanFailure, ScannedEvidence, ScanOptions, ScanSkip
from one_memory.imports.discovery import SourceFileDiscoveryFailure, discover_source_files
from one_memory.imports.sources.claude_code import create_claude_code_adapter
from one_memory.imports.sources.common import discovery_failure_code, is_unchanged, newest_first
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cloud_memory_hub.capture_store import (
    CaptureError,
    CaptureStore,
    capture_lock,
    read_capture_file,
)
from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.privacy import MAX_CONTENT_BYTES, parse_json


class SourceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    adapter_id: Literal["claude-code"]
    source_id: str
    scope_id: str
    source_root: str = Field(min_length=1, max_length=4096, repr=False)
    workspace: str = Field(min_length=1, max_length=4096, repr=False)
    source_version: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("schema_version", mode="before")
    @classmethod
    def version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported_source_version")
        return value

    @field_validator("source_id", "scope_id")
    @classmethod
    def identifier(cls, value):
        return canonical_uuid(value)

    @field_validator("source_root", "workspace")
    @classmethod
    def absolute_path(cls, value):
        if not Path(value).is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid_source_path")
        return value

    @classmethod
    def from_file(cls, path):
        if path is None:
            raise CaptureError("source_configuration_required")
        try:
            return cls.model_validate(parse_json(read_capture_file(path, limit=16 * 1024)))
        except (ValidationError, ValueError):
            raise CaptureError("source_configuration_invalid") from None

    def binding_digest(self):
        # A version upgrade is metadata, not authorization to change roots/scopes.
        value = self.model_dump(exclude={"source_version"})
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


_MEDIA_BLOCK_TYPES = frozenset({"image", "document", "audio", "video", "file"})
# A schema may declare a property *named* attachments; only real entries are files.
_SCHEMA_KEYS = frozenset({"properties", "input_schema", "json_schema", "parameters"})


def _is_media_block(value):
    """True only for a real content block, not any dict carrying a 'type' key.

    Tool results embed JSON Schema fragments where 'type' is a list such as
    ["object", "null"], and schemas can declare a property called
    'attachments'. Treating those as media aborted a whole capture batch.
    """
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if isinstance(kind, str) and kind in _MEDIA_BLOCK_TYPES:
        return True
    attachments = value.get("attachments")
    if isinstance(attachments, list) and attachments:
        return not _SCHEMA_KEYS.intersection(value)
    return False


def _reject_unsupported_media(record):
    # The upstream text parser intentionally omits media blocks; do not let a
    # mixed text+image record silently pass as a complete import.
    pending = [(record, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 20_000 or depth > 24:
            raise CaptureError("source_record_too_complex")
        if isinstance(value, dict):
            if _is_media_block(value):
                raise CaptureError("source_media_not_supported")
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def _claude_adapter(grant, source_version):
    adapter = create_claude_code_adapter(grant, source_version=source_version)
    original_parser = adapter.parser

    def checked(record, context):
        _reject_unsupported_media(record)
        result = original_parser(record, context)
        if hasattr(result, "rows"):
            for row in result.rows():
                if len(row.verbatim.encode()) > MAX_CONTENT_BYTES:
                    raise CaptureError("source_record_too_large")
        return result

    adapter.parser = checked
    return adapter


ADAPTERS = {"claude-code": _claude_adapter}


def _open_source(outbox, config, *, initialize=False):
    try:
        grant = AuthorizedSourceRoot.grant(
            source_id=config.adapter_id,
            root=Path(config.source_root),
            user_confirmed=True,
            home=Path.home(),
        )
    except (SourceAccessError, OSError, ValueError):
        raise CaptureError("source_access_denied") from None
    store = CaptureStore(
        outbox,
        config.source_id,
        config.binding_digest(),
        grant.root_identity,
        initialize=initialize,
    )
    return store, ADAPTERS[config.adapter_id](grant, config.source_version)


def initialize_capture(outbox, config):
    with capture_lock(outbox):
        _open_source(outbox, config, initialize=True)
    return {"source_id": config.source_id, "status": "registered", "scanned": False}


def _content(evidence, config):
    return {
        "messages": [{"role": evidence.role, "text": evidence.verbatim}],
        "metadata": {
            "adapter_id": config.adapter_id,
            "native_conversation_id": evidence.conversation_id,
            "native_message_id": evidence.message_id,
            "native_correlation_id": evidence.correlation_id,
            "native_source_ordinal": (
                evidence.source_ordinal
                if not (evidence.message_id or evidence.correlation_id)
                else None
            ),
            "origin": {
                str(origin): metadata.model_dump(mode="json")
                for origin, metadata in evidence.origin_meta.items()
            },
        },
    }


def _capture_listing(adapter, store, cursors):
    changed = []
    for item in discover_source_files(adapter.grant, suffixes=adapter.suffixes, max_targets=10_000):
        if isinstance(item, SourceFileDiscoveryFailure):
            raise CaptureError("source_scan_" + discovery_failure_code(item).value)
        if not adapter.file_predicate(item):
            continue
        try:
            changed_ns = item.path.stat(follow_symlinks=False).st_ctime_ns
        except OSError:
            raise CaptureError("source_access_denied") from None
        previous = cursors.get(item.safe_id)
        previous_change = store.changed_times.get(item.safe_id)
        # Upstream's item.changed_ns is birth time, not ctime. It detects inode
        # replacement and must not be mistaken for an in-place edit timestamp.
        store.changed_times[item.safe_id] = changed_ns
        if previous is not None and previous_change != changed_ns:
            if previous.observed_size == item.size:
                # An in-place rewrite may preserve mtime and size. Re-read it,
                # including earlier lines, with existing identity deduplication.
                cursors.pop(item.safe_id)
            else:
                # Let the upstream anchored reader validate append/truncation.
                cursors[item.safe_id] = previous.model_copy(update={"read_to_end": False})
        if not is_unchanged(item, cursors):
            changed.append(item)
    return newest_first(changed)


async def collect_source(outbox, config, *, max_messages=1000):
    if type(max_messages) is not int or not 1 <= max_messages <= 1000:
        raise CaptureError("invalid_capture_batch")
    with capture_lock(outbox):
        store, adapter = _open_source(outbox, config)
        expected_workspace = opaque_workspace_id(config.adapter_id, config.workspace)
        counts = Counter()
        skipped = Counter()
        try:
            cursors = store.cursors()
            files = await asyncio.to_thread(_capture_listing, adapter, store, cursors)
            async for event in adapter.read_listed(
                files,
                ScanOptions(
                    cursors=cursors,
                    max_messages=max_messages,
                    max_scan_targets=10_000,
                    max_source_file_bytes=8 * 1024 * 1024,
                ),
            ):
                if isinstance(event, ScanFailure):
                    # Never advance past a failed/oversized/malformed record.
                    raise CaptureError("source_scan_" + event.reason_code.value)
                if isinstance(event, ScannedEvidence):
                    if event.evidence.workspace != expected_workspace:
                        skipped["workspace_mismatch"] += 1
                        store.advance(event.cursor)
                        continue
                    changed = store.record(
                        event.evidence,
                        event.cursor,
                        scope_id=config.scope_id,
                        content=_content(event.evidence, config),
                    )
                    counts["queued" if changed else "duplicates"] += 1
                elif isinstance(event, ScanSkip):
                    skipped[event.reason_code.value] += 1
                    store.advance(event.cursor)
                elif getattr(event, "cursor", None) is not None:
                    store.advance(event.cursor)
        except SourceAccessError:
            raise CaptureError("source_access_denied") from None
        except (ValidationError, ValueError, TypeError):
            raise CaptureError("source_record_invalid") from None
    return {
        "source_id": config.source_id,
        "queued": counts["queued"],
        "duplicates": counts["duplicates"],
        "skipped": dict(skipped),
        "processed": False,
    }
