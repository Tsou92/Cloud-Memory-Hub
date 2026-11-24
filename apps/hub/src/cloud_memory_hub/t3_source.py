"""Unified historical and incremental capture from T3 Code statev2 SQLite."""

import asyncio
import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC
from functools import partial

from one_memory.derived_echo import strip_injected_memory
from one_memory.imports.access import (
    AuthorizedSourceFile,
    AuthorizedSourceRoot,
    SourceAccessError,
)
from one_memory.imports.adapters import (
    ScanCursorTouch,
    ScanEvent,
    ScanFailure,
    ScanIssueCode,
    ScannedEvidence,
    ScanOptions,
    ScanPhase,
    ScanProgressEvent,
    ScanSkip,
    SourceCapabilities,
    SourceCursor,
    SourceDescriptor,
    StorageKind,
)
from one_memory.imports.async_bridge import iterate_in_worker
from one_memory.imports.discovery import (
    SourceFileDiscoveryFailure,
    discover_source_files,
    discovered_off_loop,
)
from one_memory.imports.models import (
    EvidenceInput,
    EvidenceOrigin,
    OriginMetadata,
    WrittenBy,
)
from one_memory.imports.sources.claude_code import subagent_trace_enabled
from one_memory.imports.sources.common import (
    STOP_SCAN_ACCESS_CODES,
    discovery_failure_code,
    resume_checkpoint,
    source_cursor,
    touched_cursor,
)
from one_memory.imports.sources.parsing import (
    as_record,
    first_string,
    is_host_generated_user_turn,
    parse_timestamp,
    strip_host_wrappers,
)
from one_memory.imports.sqlite_access import open_sqlite_readonly

from cloud_memory_hub.capture_store import CaptureError
from cloud_memory_hub.media_text import MAX_MEDIA_BYTES, MediaTextError, extract_raw_document
from cloud_memory_hub.privacy import MAX_CONTENT_BYTES

T3_CODE_DESCRIPTOR = SourceDescriptor(
    source_id="t3-code",
    display_name="T3 Code",
    storage_kinds=frozenset({StorageKind.SQLITE}),
    capabilities=SourceCapabilities(
        can_append=True,
        can_observe_edits=True,
        can_observe_deletes=False,
        can_reconcile_complete_set=False,
    ),
)

_REQUIRED_SCHEMA = (
    ("projection_projects", {"project_id", "title", "workspace_root", "deleted_at"}),
    (
        "orchestration_v2_projection_threads",
        {
            "thread_id",
            "project_id",
            "title",
            "default_provider",
            "runtime_mode",
            "created_at",
            "updated_at",
            "deleted_at",
            "payload_json",
        },
    ),
    (
        "orchestration_v2_projection_messages",
        {
            "message_id",
            "thread_id",
            "role",
            "streaming",
            "created_at",
            "updated_at",
            "payload_json",
        },
    ),
)
_MEDIA_CONTEXT_KINDS = frozenset(
    {
        "image",
        "file",
        "document",
        "audio",
        "video",
        "input_image",
        "input_file",
        "input_audio",
        "input_video",
    }
)
_SUPPORTED_DOC_MIMES = frozenset(
    {"application/pdf", "text/plain", "text/markdown", "text/x-markdown"}
)
_SUPPORTED_DOC_SUFFIXES = (".pdf", ".txt", ".md", ".markdown")
_SAFE_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CHUNK_SIZE = 200


@dataclass(frozen=True)
class WorkspaceMismatchSkip:
    """Indicates a multi-project DB row outside the bound workspace."""

    cursor: SourceCursor
    conversation_id: str | None = None
    reason_code: str = "workspace_mismatch"


class _StreamState:
    def __init__(self) -> None:
        self.active_streaming = False


def create_t3_adapter(
    grant: AuthorizedSourceRoot,
    *,
    source_version: str | None = None,
    media_text_extraction: bool = False,
    workspace: str | None = None,
) -> "T3CodeSourceAdapter":
    return T3CodeSourceAdapter(
        grant=grant,
        source_version=source_version,
        media_text_extraction=media_text_extraction,
        workspace=workspace,
    )


class T3CodeSourceAdapter:
    """Read-only incremental SQLite adapter over T3 Code's statev2 database."""

    suffixes = frozenset({".sqlite"})

    def __init__(
        self,
        *,
        grant: AuthorizedSourceRoot,
        source_version: str | None = None,
        media_text_extraction: bool = False,
        workspace: str | None = None,
    ) -> None:
        if grant.source_id != T3_CODE_DESCRIPTOR.source_id:
            raise ValueError("T3 Code grant must use the t3-code source ID")
        self.grant = grant
        self.source_version = source_version
        self.media_text_extraction = media_text_extraction
        self.workspace = workspace

    @staticmethod
    def file_predicate(source_file: AuthorizedSourceFile) -> bool:
        return source_file.relative_path.name == "statev2.sqlite"

    @property
    def descriptor(self) -> SourceDescriptor:
        return T3_CODE_DESCRIPTOR

    async def detect(self) -> bool:
        async for item in discovered_off_loop(
            self.grant, suffixes=self.suffixes, max_targets=10_000
        ):
            if isinstance(item, AuthorizedSourceFile) and self.file_predicate(item):
                return True
        return False

    async def scan(self, options: ScanOptions) -> AsyncIterator[ScanEvent]:
        emitted = 0
        async for discovered in discovered_off_loop(
            self.grant,
            suffixes=self.suffixes,
            max_targets=options.max_scan_targets or 10_000,
        ):
            yield ScanProgressEvent(phase=ScanPhase.DISCOVER, discovered_increment=1)
            if isinstance(discovered, SourceFileDiscoveryFailure):
                yield ScanFailure(reason_code=discovery_failure_code(discovered))
                continue
            if not self.file_predicate(discovered):
                continue
            async for event in self.read_listed([discovered], options):
                yield event
                if isinstance(event, ScannedEvidence):
                    emitted += 1
                    if options.max_messages is not None and emitted >= options.max_messages:
                        return
        yield ScanProgressEvent(phase=ScanPhase.DONE)

    async def read_listed(
        self, files: list[AuthorizedSourceFile], options: ScanOptions
    ) -> AsyncIterator[ScanEvent | WorkspaceMismatchSkip]:
        emitted = 0
        for listed in files:
            try:
                discovered = await asyncio.to_thread(listed.admit)
            except SourceAccessError as error:
                if error.code in STOP_SCAN_ACCESS_CODES:
                    raise
                continue
            previous = options.cursors.get(discovered.safe_id)
            checkpoint = resume_checkpoint(discovered, options)
            last_cursor: SourceCursor | None = None
            stream_state = _StreamState()
            try:
                async for event in iterate_in_worker(
                    partial(
                        self._read_file,
                        discovered,
                        checkpoint=checkpoint,
                        checkpoint_anchor=(
                            previous.checkpoint_anchor if previous is not None else None
                        ),
                        max_messages=(
                            None
                            if options.max_messages is None
                            else max(0, options.max_messages - emitted)
                        ),
                        stream_state=stream_state,
                    ),
                    scope_key=self.descriptor.source_id,
                ):
                    if event.cursor is not None:
                        last_cursor = event.cursor
                    yield event
                    if isinstance(event, ScannedEvidence):
                        emitted += 1
                        if options.max_messages is not None and emitted >= options.max_messages:
                            return
            except sqlite3.Error:
                yield ScanFailure(cursor=last_cursor, reason_code=ScanIssueCode.SOURCE_BUSY)
                continue
            except SourceAccessError as error:
                if error.code in STOP_SCAN_ACCESS_CODES:
                    raise
                continue
            touched = touched_cursor(
                discovered, options, last=last_cursor, empty_checkpoint="t3v2:0:"
            )
            if stream_state.active_streaming:
                touched = touched.model_copy(update={"read_to_end": False})
            yield ScanCursorTouch(cursor=touched)

    def seed_from_now(self) -> list[tuple[AuthorizedSourceFile, SourceCursor]]:
        """Return end-of-log watermarks for statev2 SQLite files without scanning bodies."""
        seeded: list[tuple[AuthorizedSourceFile, SourceCursor]] = []
        for item in discover_source_files(self.grant, suffixes=self.suffixes, max_targets=10_000):
            if isinstance(item, SourceFileDiscoveryFailure):
                raise CaptureError("source_scan_" + discovery_failure_code(item).value)
            if not self.file_predicate(item):
                continue
            try:
                discovered = item.admit()
            except SourceAccessError as error:
                if error.code in STOP_SCAN_ACCESS_CODES:
                    raise
                continue
            try:
                with open_sqlite_readonly(discovered, timeout=0.5) as connection:
                    connection.execute("PRAGMA busy_timeout = 200")
                    if not all(
                        _has_columns(connection, tbl, cols) for tbl, cols in _REQUIRED_SCHEMA
                    ):
                        raise CaptureError("source_scan_unsupported_schema")
                    active_streaming = bool(
                        connection.execute(
                            "SELECT EXISTS(SELECT 1 FROM "
                            "orchestration_v2_projection_messages WHERE streaming != 0)"
                        ).fetchone()[0]
                    )
                    latest: tuple[tuple[tuple[int, int, str], int], str, str] | None = None
                    for row in connection.execute(
                        "SELECT rowid AS source_rowid, message_id, updated_at "
                        "FROM orchestration_v2_projection_messages WHERE streaming = 0"
                    ):
                        source_rowid = int(row["source_rowid"])
                        updated_at = str(row["updated_at"])
                        message_id = str(row["message_id"])
                        key = (_timestamp_sort_key(updated_at), source_rowid)
                        if latest is None or key > latest[0]:
                            latest = (key, message_id, updated_at)
            except sqlite3.Error:
                raise CaptureError("source_scan_source_busy") from None
            if latest is None:
                cursor = source_cursor(discovered, "t3v2:0:", checkpoint_anchor=None)
            else:
                (_, max_rowid), max_message_id, max_updated_at = latest
                cursor = source_cursor(
                    discovered,
                    f"t3v2:{max_rowid}:{max_updated_at}",
                    checkpoint_anchor=_checkpoint_anchor(max_message_id, max_updated_at),
                )
            if active_streaming:
                cursor = cursor.model_copy(update={"read_to_end": False})
            seeded.append((discovered, cursor))
        return seeded

    def _read_file(
        self,
        discovered: AuthorizedSourceFile,
        *,
        checkpoint: str | None,
        checkpoint_anchor: str | None,
        max_messages: int | None,
        stream_state: _StreamState,
    ) -> Iterator[ScanEvent | WorkspaceMismatchSkip]:
        if max_messages == 0:
            return
        with open_sqlite_readonly(discovered, timeout=0.5) as connection:
            connection.execute("PRAGMA busy_timeout = 200")
            if not all(_has_columns(connection, tbl, cols) for tbl, cols in _REQUIRED_SCHEMA):
                yield ScanFailure(cursor=None, reason_code=ScanIssueCode.UNSUPPORTED_SCHEMA)
                return
            stream_state.active_streaming = bool(
                connection.execute(
                    "SELECT EXISTS("
                    "SELECT 1 FROM orchestration_v2_projection_messages WHERE streaming != 0"
                    ")"
                ).fetchone()[0]
            )
            last_rowid, last_ts_key = _validated_watermark(
                connection, checkpoint=checkpoint, checkpoint_anchor=checkpoint_anchor
            )
            candidates = []
            for row in connection.execute(
                "SELECT rowid AS source_rowid, updated_at "
                "FROM orchestration_v2_projection_messages WHERE streaming = 0"
            ):
                source_rowid = int(row["source_rowid"])
                updated_at = str(row["updated_at"])
                key = (_timestamp_sort_key(updated_at), source_rowid)
                if last_ts_key is None or key > (last_ts_key, last_rowid):
                    candidates.append((key, source_rowid))
            candidates.sort(key=lambda item: item[0])
            emitted = 0
            for start in range(0, len(candidates), _CHUNK_SIZE):
                chunk_keys = candidates[start : start + _CHUNK_SIZE]
                rowids = [rowid for _, rowid in chunk_keys]
                by_rowid = _fetch_rows(connection, rowids)
                for _, source_rowid in chunk_keys:
                    row = by_rowid.get(source_rowid)
                    if row is None:
                        continue
                    event = self._map_row(discovered, row)
                    yield event
                    if isinstance(event, ScannedEvidence):
                        emitted += 1
                        if max_messages is not None and emitted >= max_messages:
                            return

    def _map_row(
        self, discovered: AuthorizedSourceFile, row: sqlite3.Row
    ) -> ScanEvent | WorkspaceMismatchSkip:
        source_rowid = int(row["source_rowid"])
        message_id = str(row["message_id"])
        updated_at = str(row["updated_at"])
        thread_id = str(row["thread_id"])
        cursor = source_cursor(
            discovered,
            f"t3v2:{source_rowid}:{updated_at}",
            checkpoint_anchor=_checkpoint_anchor(message_id, updated_at),
        )
        workspace = _resolved_workspace(row)
        # Filter unrelated projects before parsing attachments so an image in
        # another workspace or T3 Scratch never blocks this bound workspace.
        if self.workspace is not None and workspace != self.workspace:
            return WorkspaceMismatchSkip(cursor=cursor, conversation_id=thread_id)

        payload = _parse_json_dict(row["payload_json"])
        if payload is None:
            return ScanFailure(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.INVALID_JSON,
            )
        thread_payload = _parse_json_dict(row["thread_payload_json"]) or {}
        lineage = as_record(thread_payload.get("lineage")) or {}
        parent_thread_id = first_string(lineage.get("parentThreadId"))
        if parent_thread_id is not None and not subagent_trace_enabled():
            return ScanSkip(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.SUBAGENT_TRACE_DISABLED,
            )

        role = str(row["role"])
        payload_role = payload.get("role")
        if payload_role is not None and payload_role != role:
            return ScanSkip(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.UNKNOWN_RECORD,
            )
        if role not in {"user", "assistant"}:
            return ScanSkip(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.UNKNOWN_RECORD,
            )

        created_by = payload.get("createdBy")
        if role == "user" and (
            payload.get("notification") is not None
            or (created_by is not None and created_by != "user")
        ):
            return ScanSkip(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.NOT_WRITTEN_BY_PERSON,
            )

        extracted_blocks = self._process_attachments(payload)
        raw_text = payload.get("text")
        cleaned_text = _clean_text(raw_text) if isinstance(raw_text, str) else ""
        had_raw_text = isinstance(raw_text, str) and bool(raw_text.strip())
        if had_raw_text and cleaned_text is None and not extracted_blocks:
            return ScanSkip(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.INTERNAL_REASONING_EXCLUDED,
            )
        parts = [cleaned_text] if cleaned_text else []
        parts.extend(extracted_blocks)
        final_text = "\n\n".join(parts).strip()
        if not final_text:
            return ScanSkip(
                cursor=cursor,
                conversation_id=thread_id,
                reason_code=ScanIssueCode.NO_TEXT,
            )
        if len(final_text.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise CaptureError("source_record_too_large")

        written_by = (
            WrittenBy.AGENT
            if (
                role == "assistant"
                or parent_thread_id is not None
                or is_host_generated_user_turn(final_text)
            )
            else WrittenBy.PERSON
        )
        raw_meta = {
            "file_key": discovered.safe_id,
            "channel": "t3-statev2",
            "record_type": f"projection_message.{role}",
        }
        provider = first_string(row["provider_instance_id"], row["default_provider"])
        if provider is not None and _SAFE_PROVIDER.fullmatch(provider):
            raw_meta["provider"] = provider
        if first_string(thread_payload.get("worktreePath")) is not None:
            raw_meta["worktree"] = True

        evidence = EvidenceInput(
            source_id=T3_CODE_DESCRIPTOR.source_id,
            workspace=opaque_workspace_id_for(workspace),
            project=_project_label(row, workspace),
            conversation_id=thread_id,
            message_id=message_id,
            source_ordinal=f"{discovered.safe_id}:t3v2:{updated_at}:{source_rowid}",
            role=role,
            written_by=written_by,
            created_at=parse_timestamp(row["created_at"]),
            verbatim=final_text,
            origin_meta={
                EvidenceOrigin.FILE: OriginMetadata(
                    source_version=self.source_version,
                    raw_meta=raw_meta,
                )
            },
        )
        return ScannedEvidence(cursor=cursor, evidence=evidence)

    def _process_attachments(self, payload: dict) -> list[str]:
        refs = _collect_media_refs(payload)
        if not refs:
            return []
        if not self.media_text_extraction:
            raise CaptureError("source_media_not_supported")
        extracted = []
        for ref in refs:
            if not _is_supported_document_ref(ref):
                raise CaptureError("source_media_not_supported")
            raw, name, media_type = self._read_attachment(ref)
            try:
                extracted.append(extract_raw_document(raw, name=name, media_type=media_type))
            except MediaTextError as error:
                raise CaptureError(error.code) from None
        return extracted

    def _read_attachment(self, ref: dict) -> tuple[bytes, str | None, str | None]:
        att_id = first_string(ref.get("id"), ref.get("attachmentId"))
        if att_id is None or not _SAFE_ATTACHMENT_ID.fullmatch(att_id) or att_id in {".", ".."}:
            raise CaptureError("media_source_invalid")
        att_dir = self.grant.root / "attachments"
        try:
            dir_info = os.lstat(att_dir)
        except OSError:
            raise CaptureError("media_source_unavailable") from None
        if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
            raise CaptureError("source_access_denied")
        matches = []
        try:
            with os.scandir(att_dir) as entries:
                for entry in entries:
                    if entry.name == att_id or entry.name.startswith(att_id + "."):
                        matches.append(att_dir / entry.name)
                        if len(matches) > 1:
                            raise CaptureError("media_source_invalid")
        except OSError:
            raise CaptureError("media_source_unavailable") from None
        if len(matches) != 1:
            raise CaptureError("media_source_unavailable")
        try:
            authorized = self.grant.authorize_file(matches[0])
            if authorized.size > MAX_MEDIA_BYTES:
                raise CaptureError("media_too_large")
            with authorized.open_binary() as stream:
                raw = stream.read(MAX_MEDIA_BYTES + 1)
        except SourceAccessError:
            raise CaptureError("source_access_denied") from None
        if len(raw) > MAX_MEDIA_BYTES:
            raise CaptureError("media_too_large")
        return (
            raw,
            first_string(ref.get("name"), authorized.path.name),
            first_string(ref.get("mimeType")),
        )


def opaque_workspace_id_for(workspace: str | None) -> str | None:
    from one_memory.imports.access import opaque_workspace_id

    return opaque_workspace_id(T3_CODE_DESCRIPTOR.source_id, workspace)


def _project_label(row: sqlite3.Row, workspace: str | None) -> str | None:
    from one_memory.imports.access import safe_project_label

    title = first_string(row["project_title"])
    return safe_project_label(workspace) or title


def _resolved_workspace(row: sqlite3.Row) -> str | None:
    if row["thread_deleted_at"] is not None or row["project_deleted_at"] is not None:
        return None
    root = row["project_workspace_root"]
    if not isinstance(root, str) or not root.strip():
        return None
    return root


def _has_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return False
    actual = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
    return required.issubset(actual)


def _timestamp_sort_key(value: str) -> tuple[int, int, str]:
    parsed = parse_timestamp(value)
    if parsed is None:
        return (1, 0, value)
    epoch_us = int(parsed.astimezone(UTC).timestamp() * 1_000_000)
    return (0, epoch_us, "")


def _checkpoint_anchor(message_id: str, updated_at: str) -> str:
    return hashlib.sha256(f"{message_id}\0{updated_at}".encode()).hexdigest()


def _validated_watermark(
    connection: sqlite3.Connection,
    *,
    checkpoint: str | None,
    checkpoint_anchor: str | None,
) -> tuple[int, tuple[int, int, str] | None]:
    if checkpoint is None or checkpoint_anchor is None or not checkpoint.startswith("t3v2:"):
        return 0, None
    parts = checkpoint.split(":", 2)
    if len(parts) != 3:
        return 0, None
    try:
        last_rowid = int(parts[1])
    except ValueError:
        return 0, None
    last_updated_at = parts[2]
    if last_rowid <= 0 or not last_updated_at:
        return 0, None
    row = connection.execute(
        "SELECT message_id, updated_at FROM orchestration_v2_projection_messages WHERE rowid = ?",
        (last_rowid,),
    ).fetchone()
    if (
        row is None
        or _checkpoint_anchor(str(row["message_id"]), str(row["updated_at"])) != checkpoint_anchor
    ):
        return 0, None
    return last_rowid, _timestamp_sort_key(last_updated_at)


def _fetch_rows(connection: sqlite3.Connection, rowids: list[int]) -> dict[int, sqlite3.Row]:
    if not rowids:
        return {}
    placeholders = ",".join("?" for _ in rowids)
    cursor = connection.execute(
        f"""
        SELECT m.rowid AS source_rowid,
               m.message_id,
               m.thread_id,
               m.role,
               m.created_at,
               m.updated_at,
               m.payload_json,
               t.default_provider,
               t.provider_instance_id,
               t.deleted_at AS thread_deleted_at,
               t.payload_json AS thread_payload_json,
               p.title AS project_title,
               p.workspace_root AS project_workspace_root,
               p.deleted_at AS project_deleted_at
        FROM orchestration_v2_projection_messages AS m
        LEFT JOIN orchestration_v2_projection_threads AS t ON t.thread_id = m.thread_id
        LEFT JOIN projection_projects AS p ON p.project_id = t.project_id
        WHERE m.rowid IN ({placeholders}) AND m.streaming = 0
        """,
        rowids,
    )
    return {int(row["source_rowid"]): row for row in cursor}


def _parse_json_dict(value: object) -> dict | None:
    if not isinstance(value, str):
        return None
    import json

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_text(value: str) -> str | None:
    stripped = strip_host_wrappers(value)
    if stripped is None:
        return None
    cleaned = strip_injected_memory(stripped).strip()
    return cleaned or None


def _collect_media_refs(payload: dict) -> list[dict]:
    refs: list[dict] = []
    seen_ids: set[str] = set()
    attachments = payload.get("attachments")
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            item_id = first_string(item.get("id"), item.get("attachmentId"))
            if item_id is not None:
                seen_ids.add(item_id)
            refs.append(item)
    context = payload.get("context")
    if isinstance(context, dict) and isinstance(context.get("records"), list):
        for record in context["records"]:
            if not isinstance(record, dict):
                continue
            item_id = first_string(record.get("attachmentId"), record.get("id"))
            if item_id is not None and item_id in seen_ids:
                continue
            kind = str(record.get("kind") or "").strip().lower()
            mime = str(record.get("mimeType") or "").split(";", 1)[0].strip().lower()
            is_media = (
                kind in _MEDIA_CONTEXT_KINDS
                or item_id is not None
                or mime.startswith(("image/", "audio/", "video/"))
                or mime in _SUPPORTED_DOC_MIMES
            )
            if is_media:
                if item_id is not None:
                    seen_ids.add(item_id)
                refs.append(record)
    return refs


def _is_supported_document_ref(ref: dict) -> bool:
    kind = str(ref.get("kind") or ref.get("type") or "").strip().lower()
    if kind in {"image", "audio", "video", "input_image", "input_audio", "input_video"}:
        return False
    mime = str(ref.get("mimeType") or "").split(";", 1)[0].strip().lower()
    if mime.startswith(("image/", "audio/", "video/")):
        return False
    if mime in _SUPPORTED_DOC_MIMES:
        return True
    if mime in {"", "application/octet-stream"}:
        name = str(ref.get("name") or "").strip().lower()
        return name.endswith(_SUPPORTED_DOC_SUFFIXES)
    return False
