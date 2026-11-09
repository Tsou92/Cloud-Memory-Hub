"""Opt-in, synthetic Responses endpoint for the real desktop host probe.

This prepares a fresh temporary Codex profile and hooks. It never reads daily
Agent settings, credentials or histories, and does not launch an application.
The operator launches only that profile and drives its actual desktop UI.
This is host-contract evidence, not a product connector or model-quality test.
"""

import argparse
import json
import re
import shlex
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

MARKERS = ("CMH_DESKTOP_MEMORY_BLUE", "CMH_DESKTOP_MEMORY_GREEN")
HOOK = r"""import json, sys
from pathlib import Path
root = Path(__file__).parent
value = json.load(sys.stdin)
event = value.get("hook_event_name")
with (root / "hook-events.jsonl").open("a") as output:
    output.write(json.dumps({
        "event": event, "session": value.get("session_id"),
        "prompt": value.get("prompt"),
        "last_assistant_message": value.get("last_assistant_message"),
    }) + "\n")
if event == "UserPromptSubmit":
    marker = (root / "context.txt").read_text().strip()
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": marker,
    }}))
else:
    print(json.dumps({"continue": True, "suppressOutput": True}))
"""


def reply_events(text, number):
    identity = f"resp_cmh_{number}"
    message = {
        "id": f"msg_cmh_{number}",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    response = {
        "id": identity,
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "cmh-probe",
        "output": [message],
        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    }
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**message, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": response},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps(
                {"object": "list", "data": [{"id": "cmh-probe", "object": "model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2 * 1024 * 1024:
                raise ValueError
            request = json.loads(self.rfile.read(size))
            if type(request) is not dict or request.get("stream") is not True:
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        # A response is derived only from the actual model input. Changing the
        # context file alone cannot make the endpoint return the new marker.
        observed = re.findall("|".join(MARKERS), json.dumps(request.get("input", [])))
        text = observed[-1] if observed else "CMH_DESKTOP_NO_MEMORY"
        with self.server.lock:
            self.server.number += 1
            number = self.server.number
            with (self.server.root / "model-observations.jsonl").open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "request": number,
                            "model": request.get("model"),
                            "markers": observed,
                            "reply": text,
                        }
                    )
                    + "\n"
                )
        body = reply_events(text, number)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def prepare(root, port):
    for name in ("codex", "electron", "project", "home", "tmp"):
        (root / name).mkdir(mode=0o700)
    (root / "context.txt").write_text(MARKERS[0])
    (root / "hook.py").write_text(HOOK)
    command = shlex.join(["/usr/bin/python3", str(root / "hook.py")])
    (root / "codex/hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [{"hooks": [{"type": "command", "command": command}]}]
                    for event in ("UserPromptSubmit", "Stop")
                }
            }
        )
    )
    (root / "codex/config.toml").write_text(
        f"""model = "cmh-probe"
model_provider = "cmh-probe"
cli_auth_credentials_store = "file"
check_for_update_on_startup = false

[model_providers.cmh-probe]
name = "CMH local synthetic probe"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false

[features]
hooks = true
enable_request_compression = false

[analytics]
enabled = false
[feedback]
enabled = false
"""
    )
    (root / "project/README.md").write_text(
        "# CMH synthetic desktop probe\nOnly synthetic conversation markers are used.\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = args.root or Path("/private/tmp") / ("cmh-desktop-" + uuid4().hex)
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.root, server.number, server.lock = root, 0, threading.Lock()
    prepare(root, server.server_port)
    (root / "endpoint.json").write_text(json.dumps({"port": server.server_port}))
    print(json.dumps({"root": str(root), "port": server.server_port}), flush=True)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stopped.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    main()
