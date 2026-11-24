"""Project-bound Antigravity / T3 Code lifecycle hooks for automatic capture and recall."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from cloud_memory_hub.agent_access import MAX_CAPTURE_BYTES, AgentAccessError
from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.agent_hook import MAX_HOOK_BYTES, _identifier, _identity, process_message
from cloud_memory_hub.claude_hook import failure as claude_failure
from cloud_memory_hub.local_outbox import OutboxError
from cloud_memory_hub.privacy import parse_json

ADAPTER_ID = "antigravity"
EVENTS = frozenset({"PreInvocation", "Stop"})
SILENT = {}
_ABORTED_REASONS = frozenset({"USER_CANCELED", "ERROR"})
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


def antigravity_context(text):
    return {
        "injectSteps": [{"systemMessage": {"systemMessage": text}}],
        "terminationBehavior": "",
    }


def antigravity_failure(code, *, denied=False):
    claude_output, status = claude_failure(code, denied=denied)
    message = claude_output["hookSpecificOutput"]["additionalContext"]
    return antigravity_context(message), status


def _safe_transcript_path(value):
    if type(value) is not str or not value.strip() or "\x00" in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    return path


def _read_jsonl_records(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise AgentAccessError("hook_visible_text_invalid") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size > _MAX_TRANSCRIPT_BYTES
        ):
            raise AgentAccessError("hook_visible_text_invalid")
        raw = stream.read(_MAX_TRANSCRIPT_BYTES + 1)
    if len(raw) > _MAX_TRANSCRIPT_BYTES:
        raise AgentAccessError("hook_input_too_large")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise AgentAccessError("hook_visible_text_invalid") from None
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _current_turn(path):
    records = _read_jsonl_records(path)
    if records is None:
        return None, []
    latest_user = None
    planners = []
    for record in records:
        kind = record.get("type")
        if kind == "USER_INPUT":
            latest_user = record
            planners = []
        elif kind == "PLANNER_RESPONSE" and latest_user is not None:
            planners.append(record)
    return latest_user, planners


def _resolve_full_content(transcript_path, record):
    content = record.get("content")
    truncated = record.get("truncated_fields")
    step_index = record.get("step_index")
    kind = record.get("type")
    if (
        isinstance(truncated, list)
        and "content" in truncated
        and type(step_index) is int
        and step_index >= 0
    ):
        full_path = transcript_path.parent / "transcript_full.jsonl"
        full_records = _read_jsonl_records(full_path)
        if full_records:
            for item in reversed(full_records):
                if item.get("type") == kind and item.get("step_index") == step_index:
                    full_content = item.get("content")
                    if isinstance(full_content, str):
                        return full_content
                    break
    return content


def _matches_user_turn(user_record, last_user_input, transcript_path):
    recorded = _resolve_full_content(transcript_path, user_record)
    if type(recorded) is not str:
        return False
    if recorded == last_user_input:
        return True
    truncated = user_record.get("truncated_fields")
    return (
        isinstance(truncated, list)
        and "content" in truncated
        and last_user_input.startswith(recorded)
    )


def _check_workspace(raw, workspace):
    if workspace is None:
        raise AgentAccessError("hook_workspace_required")
    workspace = Path(workspace)
    cwd_resolved = Path.cwd().resolve()
    if (
        not workspace.is_absolute()
        or str(workspace.resolve()) != str(workspace)
        or cwd_resolved not in {workspace, workspace / ".agents"}
        or raw.get("cwd") != str(workspace)
    ):
        raise AgentAccessError("hook_workspace_mismatch")
    workspace_paths = raw.get("workspacePaths")
    if workspace_paths is not None:
        if type(workspace_paths) is not list or any(type(p) is not str for p in workspace_paths):
            raise AgentAccessError("hook_workspace_mismatch")
        if workspace_paths and str(workspace) not in workspace_paths:
            raise AgentAccessError("hook_workspace_mismatch")
    return workspace


def antigravity_message(raw, workspace):
    """Validate Antigravity hook input and extract a bounded visible turn event."""
    if type(raw) is not dict:
        raise AgentAccessError("hook_input_invalid")
    event = raw.get("hookEventName")
    if event not in EVENTS or "cursor_version" in raw:
        return None
    _check_workspace(raw, workspace)
    conversation_id = _identifier(raw.get("conversationId"))
    transcript_raw = raw.get("transcriptPath")
    transcript_path = _safe_transcript_path(transcript_raw) if transcript_raw is not None else None
    if transcript_raw is not None and transcript_path is None:
        raise AgentAccessError("hook_visible_text_invalid")

    from one_memory.live_capture import _plain, _user_text

    if event == "PreInvocation":
        raw_text = raw.get("lastUserInput")
        if type(raw_text) is not str:
            raise AgentAccessError("hook_visible_text_missing")
        if len(raw_text.encode()) > MAX_HOOK_BYTES:
            raise AgentAccessError("hook_input_too_large")
        text, _ = _user_text(ADAPTER_ID, raw_text)
        if text is None:
            return None
        latest_user = None
        initial_steps = raw.get("initialNumSteps")
        if transcript_path is not None:
            latest_user, planners = _current_turn(transcript_path)
            if (
                latest_user is not None
                and planners
                and _matches_user_turn(latest_user, raw_text, transcript_path)
            ):
                last_planner_step = planners[-1].get("step_index")
                if (
                    type(initial_steps) is not int
                    or initial_steps < 0
                    or type(last_planner_step) is not int
                    or last_planner_step >= initial_steps
                ):
                    return None
        if type(initial_steps) is int and initial_steps >= 0:
            turn_id = f"invocation:{initial_steps}"
        elif (
            latest_user is not None
            and type(latest_user.get("step_index")) is int
            and latest_user["step_index"] >= 0
            and _matches_user_turn(latest_user, raw_text, transcript_path)
        ):
            turn_id = f"step:{latest_user['step_index']}"
        else:
            raise AgentAccessError("hook_identity_missing_or_invalid")
        return event, "user", text, conversation_id, turn_id

    if raw.get("terminationReason") in _ABORTED_REASONS:
        return None
    if transcript_path is None:
        raise AgentAccessError("hook_visible_text_missing")
    _, planners = _current_turn(transcript_path)
    final_planner = None
    for record in reversed(planners):
        if record.get("status") not in {None, "DONE"} or record.get("tool_calls"):
            continue
        content = record.get("content")
        if isinstance(content, str) and content.strip():
            final_planner = record
            break
    if final_planner is None:
        return None
    step_index = final_planner.get("step_index")
    if type(step_index) is not int or step_index < 0:
        raise AgentAccessError("hook_identity_missing_or_invalid")
    full_content = _resolve_full_content(transcript_path, final_planner)
    if type(full_content) is not str:
        raise AgentAccessError("hook_visible_text_missing")
    if len(full_content.encode()) > MAX_CAPTURE_BYTES:
        raise AgentAccessError("hook_input_too_large")
    cleaned = _plain(full_content)
    if cleaned is None:
        return None
    return event, "assistant", cleaned, conversation_id, f"step:{step_index}"


async def process_antigravity_message(service, message):
    event, role, text, native_session, native_prompt = message
    if event == "PreInvocation":
        binding = (
            service.connection.instance_id,
            service.connection.device_id,
            service.scope_id,
            service.source_id,
        )
        request_id = _identity(*binding, native_session, native_prompt, role)
        session_id = _identity(*binding, native_session)
        prepared = service.prepare_event(
            {
                "messages": [{"role": role, "text": text}],
                "metadata": {
                    "adapter_id": ADAPTER_ID,
                    "capture_kind": "visible_hook",
                    "capture_version": 1,
                },
            },
            request_id=request_id,
            session_id=session_id,
            kind="conversation_turn",
        )
        try:
            state = service.outbox.event_state(prepared.event_id, prepared.payload_digest())
        except OutboxError as error:
            code = (
                "request_identity_conflict"
                if str(error) == "event_identity_conflict"
                else "agent_outbox_unavailable"
            )
            raise AgentAccessError(code) from None
        if state == "conflict":
            raise AgentAccessError("request_identity_conflict")
        if state == "matching":
            return SILENT, {
                "event": event,
                "capture": "replayed",
                "recall": "not_requested",
                "processing_state": "not_checked",
            }
    return await process_message(
        service,
        message,
        adapter=ADAPTER_ID,
        recall_event="PreInvocation",
        context_output=antigravity_context,
        failure_output=antigravity_failure,
        silent_output=SILENT,
    )


def run_antigravity_hook_process(config_file, workspace):
    event = None
    try:
        data = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(data) > MAX_HOOK_BYTES:
            raise AgentAccessError("hook_input_too_large")
        raw = parse_json(data)
        if type(raw) is dict and raw.get("hookEventName") in EVENTS:
            event = raw["hookEventName"]
        message = antigravity_message(raw, workspace)
        if message is None:
            print(json.dumps(SILENT))
            return 0
        service = AgentSettings.from_file(config_file).open()
        output, status = asyncio.run(process_antigravity_message(service, message))
    except Exception as error:
        code = (
            error.code
            if isinstance(error, AgentAccessError)
            else "hook_configuration_or_input_invalid"
        )
        output = antigravity_failure(code)[0] if event == "PreInvocation" else SILENT
        status = {
            "capture": "unavailable",
            "recall": "unavailable" if event == "PreInvocation" else "not_requested",
            "error": code,
        }
    print(json.dumps(output, ensure_ascii=False))
    print(json.dumps({"component": "agent_hook", **status}), file=sys.stderr)
    return 0
