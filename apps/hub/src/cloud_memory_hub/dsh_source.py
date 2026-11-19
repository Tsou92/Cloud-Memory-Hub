"""Historical import for DeepSeek Harness session.v3 JSONL files.

DSH stores each session as a zstd-compressed JSONL stream. The upstream reader
already supports zstd record checkpoints; this module only maps DSH's event
schema onto the shared ParsedMessage contract.
"""

import unicodedata

from one_memory.imports.access import AuthorizedSourceRoot
from one_memory.imports.adapters import (
    ScanIssueCode,
    SourceCapabilities,
    SourceDescriptor,
    StorageKind,
)
from one_memory.imports.models import WrittenBy
from one_memory.imports.sources.common import (
    JsonlSourceAdapter,
    ParsedMessage,
    ParsedSkip,
    SourceRecordContext,
    followed_by_tool,
)
from one_memory.imports.sources.parsing import (
    as_record,
    conversation_from_relative_path,
    first_string,
    parse_timestamp,
    strip_host_wrappers,
    text_content,
    tool_blocks_evidence,
    tool_evidence_text,
)

DSH_DESCRIPTOR = SourceDescriptor(
    source_id="deepseek-harness",
    display_name="DeepSeek Harness",
    storage_kinds=frozenset({StorageKind.ZSTD}),
    capabilities=SourceCapabilities(
        can_append=True,
        can_observe_edits=True,
        can_observe_deletes=False,
        can_reconcile_complete_set=False,
    ),
)


def _is_session_header(record):
    return (
        record.get("type") == "session"
        and record.get("version") == 3
        and isinstance(record.get("id"), str)
        and isinstance(record.get("cwd"), str)
    )


def create_dsh_adapter(grant: AuthorizedSourceRoot, *, source_version: str | None = None):
    return JsonlSourceAdapter(
        descriptor=DSH_DESCRIPTOR,
        grant=grant,
        parser=parse_dsh_record,
        source_version=source_version,
        suffixes=frozenset({".zstd"}),
        file_predicate=lambda source_file: source_file.relative_path.name.endswith(
            "session.v3.jsonl.zstd"
        ),
        header_selector=_is_session_header,
    )


def _session(context):
    header = context.header
    return header if isinstance(header, dict) and _is_session_header(header) else {}


def _identity(context, record):
    session = _session(context)
    return (
        first_string(session.get("id")) or conversation_from_relative_path(context.relative_path),
        first_string(session.get("cwd")),
    )


def _has_block(message, block_type):
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == block_type for block in content)


def _plain_tool_text(text):
    """Drop control/format bytes from tool evidence; keep line structure."""
    value = "".join(
        ch for ch in text if ch in "\n\r\t" or unicodedata.category(ch) not in {"Cf", "Cc", "Cs"}
    )
    return value or None


def parse_dsh_record(record: dict, context: SourceRecordContext) -> ParsedMessage | ParsedSkip:
    kind = record.get("type")
    conversation_id, workspace = _identity(context, record)
    created_at = parse_timestamp(record.get("time"))
    data = as_record(record.get("data"))

    if kind == "user/message":
        source = as_record(data.get("source")) if data else None
        if not source or first_string(source.get("kind")) != "user":
            return ParsedSkip(ScanIssueCode.NOT_WRITTEN_BY_PERSON)
        content = text_content(data.get("content"), allowed_block_types=frozenset({"text"}))
        if not content:
            return ParsedSkip(ScanIssueCode.NO_TEXT)
        content = strip_host_wrappers(content)
        if not content:
            return ParsedSkip(ScanIssueCode.INTERNAL_REASONING_EXCLUDED)
        return ParsedMessage(
            conversation_id=conversation_id,
            message_id=first_string(data.get("id")),
            role="user",
            written_by=WrittenBy.PERSON,
            created_at=created_at,
            verbatim=content,
            workspace=workspace,
            record_type="user/message",
        )

    if kind == "assistant/message":
        message = as_record(data.get("message")) if data else None
        if message is None:
            return ParsedSkip(ScanIssueCode.UNKNOWN_RECORD)
        content = text_content(message.get("content"), allowed_block_types=frozenset({"text"}))
        tool_text = tool_blocks_evidence(
            message.get("content"), block_types=frozenset({"tool-call"})
        )
        if tool_text is not None:
            tool_text = _plain_tool_text(tool_text)
        if not content:
            if tool_text is not None:
                return ParsedMessage(
                    conversation_id=conversation_id,
                    message_id=first_string(message.get("id")),
                    role="tool",
                    written_by=WrittenBy.TOOL,
                    created_at=created_at,
                    verbatim=tool_text,
                    workspace=workspace,
                    record_type="assistant/message.tool",
                )
            reason = (
                ScanIssueCode.INTERNAL_REASONING_EXCLUDED
                if _has_block(message, "reasoning")
                else ScanIssueCode.NO_TEXT
            )
            return ParsedSkip(reason)
        return followed_by_tool(
            ParsedMessage(
                conversation_id=conversation_id,
                message_id=first_string(message.get("id")),
                role="assistant",
                written_by=WrittenBy.AGENT,
                created_at=created_at,
                verbatim=content,
                workspace=workspace,
                record_type="assistant/message",
            ),
            tool_text,
        )

    if kind == "tool/result":
        message = as_record(data.get("message")) if data else None
        content = message.get("content") if message else None
        if not isinstance(content, list):
            return ParsedSkip(ScanIssueCode.TOOL_CONTENT_EXCLUDED)
        parts = []
        call_id = None
        for block in content:
            block = as_record(block)
            if block is None or block.get("type") != "tool-result":
                continue
            call_id = first_string(block.get("toolCallId")) or call_id
            text = text_content(block.get("content"), allowed_block_types=frozenset({"text"}))
            if text:
                parts.append(text)
        if not parts:
            return ParsedSkip(ScanIssueCode.TOOL_CONTENT_EXCLUDED)
        text = tool_evidence_text({"name": "tool-result", "output": "\n".join(parts)})
        if text is not None:
            text = _plain_tool_text(text)
        if text is None:
            return ParsedSkip(ScanIssueCode.TOOL_CONTENT_EXCLUDED)
        return ParsedMessage(
            conversation_id=conversation_id,
            message_id=call_id,
            role="tool",
            written_by=WrittenBy.TOOL,
            created_at=created_at,
            verbatim=text,
            workspace=workspace,
            record_type="tool/result",
        )

    if kind == "system/message":
        return ParsedSkip(ScanIssueCode.INTERNAL_REASONING_EXCLUDED)
    return ParsedSkip(ScanIssueCode.UNKNOWN_RECORD)
