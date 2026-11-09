"""Real Claude MCP calls with private synthetic state and a scripted local model.

This proves host/tool integration, not autonomous tool selection or model quality.
No daily Agent configuration, real history, or external model is used.
"""

import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from probe_agent_access import call, client, device_config
from probe_claude_native import ModelHandler, ModelStub, NativeSession, stream_reply
from probe_claude_recall import LiveHub

PREFIX = "mcp__cmh__"
TOOLS = {"memory_remember", "memory_recent", "handoff_save", "handoff_load"}
FIRST = "CMH_CLAUDE_MCP_V1: Use the blue navigation."
SECOND = "CMH_CLAUDE_MCP_V2: Another device selected green navigation."
HANDOFF = {
    "goal": "Continue the synthetic navigation task",
    "state": "The first design decision is saved",
    "decisions": ["Use shared project memory"],
    "next_steps": ["Read the other device's latest update"],
    "blockers": [],
}


def tool_reply(model, name, arguments, number):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": f"msg_cmh_{number}",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": f"tool_cmh_{number}",
                "name": PREFIX + name,
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def tool_result(body, number):
    matches = [
        block
        for block in body["messages"][-1]["content"]
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("tool_use_id") == f"tool_cmh_{number}"
    ]
    assert len(matches) == 1 and not matches[0].get("is_error"), "claude_tool_result_failed"
    content = matches[0]["content"]
    if isinstance(content, list):
        content = "".join(block["text"] for block in content if block.get("type") == "text")
    return json.loads(content)


class ToolModel(ModelStub):
    def __init__(self):
        super().__init__("synthetic-" + uuid4().hex)
        self.RequestHandlerClass = ToolHandler
        self.results = {}
        self.failure = False

    def reply(self, body):
        self.requests.append(body)
        number = len(self.requests)
        advertised = {tool["name"] for tool in body.get("tools", [])}
        assert {PREFIX + name for name in TOOLS} <= advertised, "claude_mcp_tools_missing"
        if number in {2, 3, 4, 5, 7}:
            self.results[number - 1] = tool_result(body, number - 1)
        if number == 1:
            name, arguments = "memory_remember", {"text": FIRST, "request_id": str(uuid4())}
        elif number == 2:
            assert self.results[1]["status"] == "accepted", "claude_remember_not_accepted"
            name, arguments = "memory_recent", {}
        elif number == 3:
            assert any(FIRST in row["content"] for row in self.results[2]["items"]), (
                "claude_recent_missing_first_update"
            )
            name, arguments = "handoff_save", {**HANDOFF, "request_id": str(uuid4())}
        elif number == 4:
            assert self.results[3]["status"] == "accepted", "claude_handoff_not_accepted"
            name, arguments = "handoff_load", {"handoff_id": self.results[3]["handoff_id"]}
        elif number == 5:
            assert self.results[4]["handoff"] == HANDOFF, "claude_handoff_changed"
            assert self.results[4]["content_trust"] == "untrusted_history"
            return stream_reply(body["model"], "CMH_SYNTHETIC_MCP_FIRST_COMPLETE", number)
        elif number == 6:
            name, arguments = "memory_recent", {}
        elif number == 7:
            assert any(SECOND in row["content"] for row in self.results[6]["items"]), (
                "claude_next_turn_missing_other_device_update"
            )
            return stream_reply(body["model"], "CMH_SYNTHETIC_MCP_SECOND_COMPLETE", number)
        else:
            raise AssertionError("unexpected_claude_model_request")
        return tool_reply(body["model"], name, arguments, number)


class ToolHandler(ModelHandler):
    def do_POST(self):
        if self.headers.get("x-api-key") != self.server.credential:
            self.send_error(401)
            return
        size = int(self.headers.get("content-length", "0"))
        if not 0 < size < 2 * 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        if self.path.startswith("/v1/messages/count_tokens"):
            data, content_type = b'{"input_tokens":100}', "application/json"
        elif self.path.split("?")[0] == "/v1/messages" and body.get("stream") is True:
            try:
                data = self.server.reply(body)
            except (AssertionError, KeyError, TypeError, ValueError):
                self.server.failure = True
                data = stream_reply(body["model"], "CMH_SYNTHETIC_PROBE_FAILED", 0)
            content_type = "text/event-stream"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class McpSession(NativeSession):
    """Reuse the existing stream reader and bounded turn/cleanup lifecycle."""

    def __init__(self, root, executable, server, settings):
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG"}
        }
        environment.update(
            {
                "CLAUDE_CONFIG_DIR": str(root / "config"),
                "ANTHROPIC_API_KEY": server.credential,
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            }
        )
        mcp = {
            "mcpServers": {
                "cmh": {
                    "command": sys.executable,
                    "args": ["-m", "cloud_memory_hub.agent_mcp", "--config-file", str(settings)],
                }
            }
        }
        self.stderr = (root / "stderr.log").open("w")
        self.output = (root / "stdout.jsonl").open("w")
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [
                executable,
                "--bare",
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--setting-sources",
                "",
                "--settings",
                str(root / "settings.json"),
                "--mcp-config",
                json.dumps(mcp),
                "--strict-mcp-config",
                "--tools",
                "",
                "--allowedTools",
                ",".join(PREFIX + name for name in sorted(TOOLS)),
                "--disable-slash-commands",
                "--no-session-persistence",
                "--system-prompt",
                "You are a synthetic MCP integration test.",
                "--permission-mode",
                "dontAsk",
            ],
            cwd=root / "project",
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
        )
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()


async def other_device_update(settings):
    async with client(settings) as writer:
        saved = await call(writer, "memory_remember", {"text": SECOND, "request_id": str(uuid4())})
        assert saved["status"] == "accepted", "other_device_update_not_accepted"


def probe():
    os.umask(0o077)
    started = time.monotonic()
    executable = shutil.which("claude")
    assert executable is not None, "claude_cli_not_installed"
    help_text = subprocess.check_output([executable, "--help"], text=True, timeout=10)
    assert "--bare" in help_text and "--strict-mcp-config" in help_text, "cli_isolation_unavailable"
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
    root = Path(tempfile.mkdtemp(prefix="cmh-claude-mcp-")).resolve()
    (root / "config").mkdir()
    (root / "project").mkdir()
    (root / "settings.json").write_text("{}")
    hub = model = session = None
    try:
        hub = LiveHub(root)
        scope = hub.call(
            "/hub/v1/projects",
            method="POST",
            status=201,
            body={"display_name": "Synthetic Claude MCP"},
        )["project_id"]
        first = device_config(hub, root / "claude", scope, "Synthetic Claude")
        second = device_config(hub, root / "other", scope, "Synthetic other device")
        model = ToolModel()
        thread = threading.Thread(target=model.serve_forever, daemon=True)
        thread.start()
        session = McpSession(root, executable, model, first)
        first_turn = session.turn(
            "Save the synthetic decision, read recent memory, then save and load a handoff."
        )
        assert not model.failure and len(model.requests) == 5, "claude_first_turn_failed"
        asyncio.run(other_device_update(second))
        update_started = time.monotonic()
        second_turn = session.turn("Read the latest shared project update again.")
        assert not model.failure and len(model.requests) == 7, "claude_second_turn_failed"
        assert first_turn["session_id"] == second_turn["session_id"], "claude_session_changed"
        session.finish()
        report = {
            "probe": "claude-mcp-isolated-v1",
            "status": "passed",
            "cli_version": version,
            "tools_called": sorted(TOOLS),
            "same_process_and_session_next_turn": True,
            "other_device_update_visible": True,
            "second_turn_seconds": round(time.monotonic() - update_started, 3),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "model": "scripted_local_test_double",
            "autonomous_tool_selection_verified": False,
            "automatic_extraction_verified": False,
            "external_model_used": False,
            "two_physical_macs_verified": False,
            "nas_network_verified": False,
        }
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, sort_keys=True))
        print(f"Private synthetic evidence: {root}", file=sys.stderr)
    finally:
        if session is not None:
            session.close()
        if model is not None:
            model.shutdown()
            model.server_close()
            thread.join(timeout=5)
        if hub is not None:
            hub.close()


if __name__ == "__main__":
    probe()
