"""Opt-in native CLI contract probe. Model responses are a local test double.

No daily Agent config, private history, real credential or external model is used.
The synthetic hook verifies host behavior; it is not the product connector.
"""

import asyncio
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.source_capture import SourceConfiguration
from one_memory.imports.access import AuthorizedSourceRoot
from one_memory.imports.adapters import ScannedEvidence, ScanOptions
from one_memory.imports.sources.claude_code import create_claude_code_adapter

HOOK = """import json, sys
from pathlib import Path
root = Path(__file__).parent
value = json.load(sys.stdin)
with (root / "hooks.jsonl").open("a") as output:
    output.write(json.dumps(value) + "\\n")
if value.get("hook_event_name") == "UserPromptSubmit":
    marker = (root / "context.txt").read_text()
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": marker,
    }}))
else:
    print(json.dumps({"continue": True, "suppressOutput": True}))
"""


def stream_reply(model, text, number):
    message = {
        "id": f"msg_synthetic_{number}",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": model,
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    events = [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


class ModelStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, credential):
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.credential = credential
        self.requests = []


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
            self.server.requests.append(body)
            number = len(self.server.requests)
            data = stream_reply(body["model"], f"CMH_SYNTHETIC_REPLY_{number}", number)
            content_type = "text/event-stream"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class NativeSession:
    def __init__(self, root, executable, server):
        # HOME retains its actual value. Auth and ordinary settings come only from
        # the new config directory and explicit synthetic provider environment.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "USER",
                "LOGNAME",
                "SHELL",
                "TMPDIR",
                "LANG",
            }
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
        self.stderr = (root / "stderr.log").open("w")
        self.output = (root / "stdout.jsonl").open("w")
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [
                executable,
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
                '{"mcpServers":{}}',
                "--strict-mcp-config",
                "--tools",
                "",
                "--disable-slash-commands",
                "--system-prompt",
                "You are a synthetic integration test.",
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

    def _drain(self):
        for line in self.process.stdout:
            self.output.write(line)
            self.output.flush()
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue

    def turn(self, text):
        self.process.stdin.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": text},
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                value = self.messages.get(timeout=1)
            except queue.Empty:
                if self.process.poll() is not None:
                    break
                continue
            if value.get("type") == "result":
                if value.get("subtype") != "success":
                    raise RuntimeError("native_turn_failed")
                return value
        raise RuntimeError("native_result_not_received")

    def finish(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        assert self.process.returncode == 0, "native_exit_failed"

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.stderr.close()
        self.output.close()


class ProductCapture:
    """Run the installed Python entry point as a separate continuous process."""

    def __init__(self, root, version, *, connection=None, source_id=None, scope_id=None):
        self.state = root / "connector"
        self.instance_id, self.device_id = str(uuid4()), str(uuid4())
        if connection is not None:
            self.instance_id, self.device_id = connection.instance_id, connection.device_id
        source = SourceConfiguration(
            adapter_id="claude-code",
            source_id=source_id or str(uuid4()),
            scope_id=scope_id or str(uuid4()),
            source_root=str(root / "config"),
            workspace=str(root / "project"),
            source_version=version,
        )
        config_file = root / "source.json"
        config_file.write_text(source.model_dump_json())
        self.options = [
            "--state-dir",
            str(self.state),
            "--instance-id",
            self.instance_id,
            "--device-id",
            self.device_id,
            "--source-file",
            str(config_file),
        ]
        if connection is not None:
            connection_file = root / "connection.json"
            connection_file.write_text(
                json.dumps(
                    {
                        **connection.model_dump(mode="json"),
                        "credential": connection.credential.get_secret_value(),
                    }
                )
            )
            self.options.extend(["--connection-file", str(connection_file)])
        self.command("init")
        self.command("capture-init")
        self.stdout = (root / "capture.jsonl").open("w")
        self.stderr = (root / "capture-stderr.log").open("w")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "cloud_memory_hub.connector", "collect-watch", *self.options],
            stdout=self.stdout,
            stderr=self.stderr,
        )

    def command(self, name):
        result = subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub.connector", name, *self.options],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError("product_capture_command_failed")
        return json.loads(result.stdout)

    def outbox(self):
        return LocalOutbox(
            self.state / "outbox.sqlite3", instance_id=self.instance_id, device_id=self.device_id
        )

    def wait_for_records(self, count):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("product_capture_watch_failed")
            status = self.outbox().status()
            if status["queued"] + status["accepted"] == count:
                return
            time.sleep(0.1)
        raise RuntimeError("product_capture_records_missing")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.stdout.close()
        self.stderr.close()


async def scan(adapter, cursors):
    records = []
    async for event in adapter.scan(ScanOptions(cursors=cursors, max_messages=100)):
        if getattr(event, "kind", None) == "error":
            raise RuntimeError("native_transcript_scan_failed")
        if isinstance(event, ScannedEvidence):
            records.append(event.evidence)
        if getattr(event, "cursor", None) is not None:
            cursors[event.cursor.file_key] = event.cursor
    return records


def read_new_messages(adapter, cursors, expected):
    records, deadline = [], time.monotonic() + 6
    while time.monotonic() < deadline and len(records) < expected:
        records.extend(asyncio.run(scan(adapter, cursors)))
        if len(records) < expected:
            time.sleep(0.1)
    assert len(records) == expected, "native_transcript_count_mismatch"
    return records


def probe():
    os.umask(0o077)
    executable = shutil.which("claude")
    if executable is None:
        raise RuntimeError("claude_cli_not_installed")
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
    root = Path(tempfile.mkdtemp(prefix="cmh-claude-native-")).resolve()
    (root / "config").mkdir()
    (root / "project").mkdir()
    (root / "hook.py").write_text(HOOK)
    (root / "context.txt").write_text("CMH_SYNTHETIC_CONTEXT_V1")
    command = shlex.join([sys.executable, str(root / "hook.py")])
    (root / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
                    for event in ("SessionStart", "UserPromptSubmit", "Stop")
                }
            }
        )
    )
    server = ModelStub("synthetic-" + uuid4().hex)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = capture = None
    try:
        adapter = create_claude_code_adapter(
            AuthorizedSourceRoot.grant(
                source_id="claude-code",
                root=root / "config",
                user_confirmed=True,
                home=Path.home(),
            ),
            source_version=version,
        )
        cursors = {}
        capture = ProductCapture(root, version)
        session = NativeSession(root, executable, server)
        first = session.turn("Synthetic first turn.")
        records = read_new_messages(adapter, cursors, 2)
        capture.wait_for_records(2)
        (root / "context.txt").write_text("CMH_SYNTHETIC_CONTEXT_V2")
        second = session.turn("Synthetic second turn.")
        records.extend(read_new_messages(adapter, cursors, 2))
        capture.wait_for_records(4)
        capture.close()
        queued = [json.loads(item.payload) for item in capture.outbox().pending()]
        assert [p["content"]["messages"][0]["text"] for p in queued] == [
            r.verbatim for r in records
        ]
        assert len({p["record_id"] for p in queued}) == 4
        assert capture.command("collect")["queued"] == 0
        session.finish()
        assert first["session_id"] == second["session_id"], "native_session_changed"
        assert len(server.requests) == 2, "unexpected_model_requests"
        assert "CMH_SYNTHETIC_CONTEXT_V1" in json.dumps(server.requests[0])
        assert "CMH_SYNTHETIC_CONTEXT_V2" not in json.dumps(server.requests[0])
        assert "CMH_SYNTHETIC_CONTEXT_V2" in json.dumps(server.requests[1]["messages"][-1])
        assert [r.role for r in records] == ["user", "assistant", "user", "assistant"]
        assert [r.verbatim for r in records] == [
            "Synthetic first turn.",
            "CMH_SYNTHETIC_REPLY_1",
            "Synthetic second turn.",
            "CMH_SYNTHETIC_REPLY_2",
        ], "injected_context_recaptured_or_source_text_changed"
        assert all(r.message_id for r in records), "native_message_id_missing"
        assert len({r.idempotency_key for r in records}) == 4
        assert not asyncio.run(scan(adapter, cursors)), "unchanged_scan_duplicated"
        replay = asyncio.run(scan(adapter, {}))
        assert {r.idempotency_key for r in replay} == {r.idempotency_key for r in records}
        hooks = [json.loads(line) for line in (root / "hooks.jsonl").read_text().splitlines()]
        assert [h["hook_event_name"] for h in hooks] == [
            "SessionStart",
            "UserPromptSubmit",
            "Stop",
            "UserPromptSubmit",
            "Stop",
        ]
        prompts = [h["prompt_id"] for h in hooks if h["hook_event_name"] == "UserPromptSubmit"]
        stops = [h for h in hooks if h["hook_event_name"] == "Stop"]
        assert [h["prompt_id"] for h in stops] == prompts and len(set(prompts)) == 2
        assert [h["last_assistant_message"] for h in stops] == [
            "CMH_SYNTHETIC_REPLY_1",
            "CMH_SYNTHETIC_REPLY_2",
        ]
        report = {
            "status": "passed",
            "cli_version": version,
            "turns": 2,
            "same_process": True,
            "fresh_context_in_next_request": True,
            "upstream_native_records": 4,
            "unchanged_rescan_new_records": 0,
            "full_rescan_identity_stable": True,
            "injected_context_recaptured": False,
            "model": "local_test_double",
            "product_capture_validated": True,
            "product_capture_native_records": len(queued),
            "product_recall_validated": False,
        }
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        print(f"Private synthetic evidence: {root}")
    except BaseException:
        print(f"Native probe failed; private synthetic evidence: {root}", file=sys.stderr)
        raise
    finally:
        if session is not None:
            session.close()
        if capture is not None:
            capture.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    probe()
