"""Small JSON command surface for the same operations exposed over MCP."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from cloud_memory_hub.agent_config import AgentConfigurationError, AgentSettings
from cloud_memory_hub.events import EventError
from cloud_memory_hub.local_outbox import OutboxError
from cloud_memory_hub.privacy import PrivacyError, parse_json
from cloud_memory_hub.transport import TransportError

COMMANDS = {
    "search": ("search", {"query", "limit"}),
    "recent": ("recent", {"limit", "offset"}),
    "remember": ("remember", {"text", "request_id", "session_id"}),
    "handoff-save": (
        "handoff_save",
        {"goal", "state", "decisions", "next_steps", "blockers", "request_id", "session_id"},
    ),
    "handoff-load": ("handoff_load", {"handoff_id"}),
    "status": ("status", set()),
    "sync": ("status", set()),
}


def failure(error):
    from cloud_memory_hub.agent_access import AgentAccessError

    if isinstance(error, (TransportError, AgentAccessError)):
        return {"error": error.code, "retryable": error.retryable}
    if isinstance(
        error,
        (AgentAccessError, AgentConfigurationError, EventError, OutboxError, PrivacyError),
    ):
        return {"error": str(error), "retryable": False}
    return {"error": "agent_operation_unavailable", "retryable": False}


async def invoke(service, command, payload):
    from cloud_memory_hub.agent_access import AgentAccessError

    name, fields = COMMANDS[command]
    if type(payload) is not dict or payload.keys() - fields:
        raise AgentAccessError("agent_arguments_invalid")
    try:
        if command == "sync":
            return await service.status(sync=True)
        return await getattr(service, name)(**payload)
    except TypeError:
        raise AgentAccessError("agent_arguments_invalid") from None


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="cloud-memory-agent")
    parser.add_argument("--config-file", type=Path, required=True)
    parser.add_argument("command", choices=["init", "upgrade-outbox", "claude-hook", *COMMANDS])
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--backup-file", type=Path)
    args = parser.parse_args()
    if (args.command == "upgrade-outbox") != (args.backup_file is not None):
        parser.error("--backup-file is required only for upgrade-outbox")
    if args.command == "claude-hook":
        from cloud_memory_hub.agent_hook import run_hook_process

        return run_hook_process(args.config_file, args.workspace)
    if args.workspace is not None:
        parser.error("--workspace is only accepted by claude-hook")
    try:
        settings = AgentSettings.from_file(args.config_file)
        if args.command == "upgrade-outbox":
            print(json.dumps(settings.upgrade_outbox(args.backup_file)))
            return 0
        service = settings.open(initialize=args.command == "init")
        if args.command == "init":
            result = {"status": "initialized", **asyncio.run(service.status())}
        else:
            payload = {}
            if args.command not in {"status", "sync"}:
                data = sys.stdin.buffer.read(64 * 1024 + 1)
                if len(data) > 64 * 1024:
                    raise PrivacyError("agent_input_too_large")
                payload = parse_json(data)
            result = asyncio.run(invoke(service, args.command, payload))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        result = failure(error)
        print(json.dumps(result), file=sys.stderr)
        return 3 if result["retryable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
