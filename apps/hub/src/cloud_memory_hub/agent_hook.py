"""Explicit-workspace Claude visible-turn capture and fresh original recall."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID

from cloud_memory_hub.agent_access import AgentAccessError
from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.claude_hook import failure, output_context
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.recall import render_context

MAX_HOOK_BYTES = 64 * 1024
HOOK_NETWORK_DEADLINE = 8.0
EVENTS = {"UserPromptSubmit": ("prompt", "user"), "Stop": ("last_assistant_message", "assistant")}
SILENT = {"continue": True, "suppressOutput": True}
DENIED = {
    "device_authentication_required",
    "source_permission_required",
    "scope_permission_required",
    "recall_authority_changed",
}


def _identifier(value):
    if type(value) is not str or not value.strip() or len(value) > 512:
        raise AgentAccessError("hook_identity_missing_or_invalid")
    return value


def _identity(*parts):
    value = json.dumps(["cmh-visible-hook-v1", *parts], ensure_ascii=False).encode()
    return str(UUID(bytes=hashlib.sha256(value).digest()[:16], version=4))


def visible_message(raw, workspace):
    """Validate the binding before touching private configuration or native helpers."""
    if type(raw) is not dict:
        raise AgentAccessError("hook_input_invalid")
    event = raw.get("hook_event_name")
    if event not in EVENTS or "cursor_version" in raw:
        return None
    if workspace is None:
        raise AgentAccessError("hook_workspace_required")
    workspace = Path(workspace)
    if (
        not workspace.is_absolute()
        or str(workspace.resolve()) != str(workspace)
        or str(Path.cwd()) != str(workspace)
        or raw.get("cwd") != str(workspace)
    ):
        raise AgentAccessError("hook_workspace_mismatch")
    session = _identifier(raw.get("session_id"))
    prompt = _identifier(raw.get("prompt_id"))
    field, role = EVENTS[event]
    text = raw.get(field)
    if type(text) is not str:
        raise AgentAccessError("hook_visible_text_missing")
    if len(text.encode()) > MAX_HOOK_BYTES:
        raise AgentAccessError("hook_input_too_large")
    from one_memory.live_capture import _messages

    # Do not pass transcript paths, alternate identities, tools or role lists.
    rows = _messages("claude-code", {"hook_event_name": event, field: text, "prompt_id": prompt})
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != role:
        raise AgentAccessError("hook_visible_text_invalid")
    return event, role, rows[0][2], session, prompt


async def process_message(service, message):
    event, role, text, native_session, native_prompt = message
    binding = (
        service.connection.instance_id,
        service.connection.device_id,
        service.scope_id,
        service.source_id,
    )
    arguments = {
        "role": role,
        "text": text,
        "request_id": _identity(*binding, native_session, native_prompt, role),
        "session_id": _identity(*binding, native_session),
    }
    saved = await service.capture_message(**arguments)
    status = {
        "event": event,
        "capture": saved["status"],
        "recall": "not_requested",
        "processing_state": "not_checked",
    }
    output = SILENT
    try:
        async with asyncio.timeout(HOOK_NETWORK_DEADLINE):
            delivery_error = await service._deliver()
            if delivery_error is not None:
                status["capture_error"] = delivery_error.code
            if event == "UserPromptSubmit":
                try:
                    reply = await service._read("history-list", {"limit": 5, "offset": 0})
                    context = render_context([reply], recent=True)
                    output = output_context(context.text)
                    status.update(
                        recall=context.state, records=context.records, truncated=context.truncated
                    )
                except AgentAccessError as error:
                    output, recall_status = failure(error.code, denied=error.code in DENIED)
                    status.update(recall=recall_status["state"], recall_error=error.code)
    except TimeoutError:
        status["delivery_error"] = "hook_network_deadline_exceeded"
        if event == "UserPromptSubmit":
            output, _ = failure("hook_network_deadline_exceeded")
            status.update(recall="unavailable", recall_error="hook_network_deadline_exceeded")
    except Exception:
        status["delivery_error"] = "hook_network_unavailable"
        if event == "UserPromptSubmit":
            output, _ = failure("hook_network_unavailable")
            status.update(recall="unavailable", recall_error="hook_network_unavailable")
    # Local receipt lookup also runs after cancellation; a queued write stays visible.
    saved = await service.capture_message(**arguments)
    status["capture"] = saved["status"]
    return output, status


def run_hook_process(config_file, workspace):
    event = None
    try:
        data = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(data) > MAX_HOOK_BYTES:
            raise AgentAccessError("hook_input_too_large")
        raw = parse_json(data)
        if type(raw) is dict and raw.get("hook_event_name") in EVENTS:
            event = raw["hook_event_name"]
        message = visible_message(raw, workspace)
        if message is None:
            print(json.dumps(SILENT))
            return 0
        service = AgentSettings.from_file(config_file).open()
        output, status = asyncio.run(process_message(service, message))
    except Exception as error:
        code = (
            error.code
            if isinstance(error, AgentAccessError)
            else "hook_configuration_or_input_invalid"
        )
        output = failure(code)[0] if event != "Stop" else SILENT
        status = {
            "capture": "unavailable",
            "recall": "unavailable" if event != "Stop" else "not_requested",
            "error": code,
        }
    print(json.dumps(output, ensure_ascii=False))
    print(json.dumps({"component": "agent_hook", **status}), file=sys.stderr)
    return 0
