"""Non-blocking Claude stdin/stdout adapter; ordinary Agent settings stay untouched."""

import asyncio
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloud_memory_hub.capture_store import CaptureError
from cloud_memory_hub.privacy import PrivacyError, parse_json
from cloud_memory_hub.recall import recall_context
from cloud_memory_hub.source_capture import SourceConfiguration
from cloud_memory_hub.transport import TransportError

MAX_HOOK_BYTES = 64 * 1024


class PromptInput(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    session_id: str = Field(min_length=1, max_length=512)
    cwd: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=MAX_HOOK_BYTES, repr=False)


def output_context(text):
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        },
    }


def failure(code, *, denied=False):
    text = (
        "Cloud Memory Hub access to the configured scopes was denied. "
        "Do not retry with other credentials or broaden access."
        if denied
        else "Cloud Memory Hub recall is unavailable for this turn. "
        "Continue without assuming that remembered facts were retrieved."
    )
    return output_context(text), {"state": "denied" if denied else "unavailable", "error": code}


def run_hook_process(args, configuration_loader):
    try:
        data = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(data) > MAX_HOOK_BYTES:
            raise CaptureError("hook_input_too_large")
        raw = parse_json(data)
        if not isinstance(raw, dict):
            raise CaptureError("hook_input_invalid")
        if raw.get("hook_event_name") != "UserPromptSubmit":
            print(json.dumps({"continue": True, "suppressOutput": True}))
            return 0
        prompt = PromptInput.model_validate(raw)
        source = SourceConfiguration.from_file(args.source_file)
        if prompt.cwd != source.workspace or str(Path.cwd()) != source.workspace:
            raise CaptureError("hook_workspace_mismatch")
        connection, options = configuration_loader(args)
        if connection is None:
            raise TransportError("connection_configuration_required")
        context = asyncio.run(
            recall_context(
                connection,
                source.scope_id,
                prompt.prompt,
                include_global=args.include_global,
                privacy=options["privacy"],
            )
        )
        output = output_context(context.text)
        status = {
            "state": context.state,
            "records": context.records,
            "truncated": context.truncated,
        }
    except TransportError as error:
        denied = error.code in {
            "device_authentication_required",
            "source_permission_required",
            "scope_permission_required",
            "recall_authority_changed",
        }
        output, status = failure(error.code, denied=denied)
    except (PrivacyError, CaptureError) as error:
        output, status = failure(str(error))
    except (ValidationError, OSError, ValueError):
        output, status = failure("hook_configuration_or_input_invalid")
    except Exception:
        # Unexpected upstream errors must not print raw prompts, paths or tokens.
        output, status = failure("hook_unavailable")
    print(json.dumps(output, ensure_ascii=False))
    print(json.dumps({"component": "claude_hook", **status}), file=sys.stderr)
    return 0
