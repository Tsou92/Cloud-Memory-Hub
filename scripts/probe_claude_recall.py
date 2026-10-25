"""Real Claude hooks + real Hub HTTP/SDK, isolated data and a local model double."""

import asyncio
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.transport import RemoteConnection
from cryptography.fernet import Fernet
from one_memory.api import _sdk_library_bridge
from probe_claude_native import ModelStub, NativeSession, ProductCapture
from starlette.responses import JSONResponse

PROMPT = "Continue the current project plan and follow my preferences."
V1 = "CMH_RECALL_V1: The current project plan uses the blue design."
V2 = "CMH_RECALL_V2: The current project plan now uses the green design."
GLOBAL = "CMH_GLOBAL_PREF: Prefer concise Chinese explanations."
FOREIGN = "CMH_FOREIGN_PRIVATE: This different project's current plan uses blue."
OBSERVER = """import json,sys
from pathlib import Path
value=json.load(sys.stdin)
with (Path(__file__).parent/'hook-events.jsonl').open('a') as out:
    out.write(json.dumps({'event':value.get('hook_event_name'),'session':value.get('session_id')})+'\\n')
print(json.dumps({'continue':True,'suppressOutput':True}))
"""


class LiveHub:
    def __init__(self, root):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.origin = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.config = HubConfig(
            root / "hub-data",
            root / "hub-config",
            self.origin,
            uuid4().hex + uuid4().hex,
            Fernet.generate_key(),
            governance_dir=root / "hub-governance",
        )
        activate_authority(
            self.config.config_dir, self.config.governance_dir, self.config.encryption_key, new=True
        )
        self.app = create_hub(self.config)
        self.offline = False
        self.searches = 0

        async def boundary(scope, receive, send):
            if scope.get("path") == "/hub/v1/session" and self.offline:
                await JSONResponse({"detail": "synthetic_unavailable"}, status_code=503)(
                    scope, receive, send
                )
                return
            if scope.get("path") == "/hub/v1/memories/search":
                self.searches += 1
            await self.app(scope, receive, send)

        self.server = uvicorn.Server(uvicorn.Config(boundary, log_config=None, access_log=False))

        async def serve():
            self.loop = asyncio.get_running_loop()
            await self.server.serve(sockets=[self.socket])

        self.thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.server.started and self.thread.is_alive():
            time.sleep(0.05)
        assert self.server.started, "probe_hub_not_ready"

    def call(self, path, *, credential=None, method="GET", body=None, status=200):
        with httpx.Client(trust_env=False, timeout=15) as client:
            result = client.request(
                method,
                self.origin + path,
                json=body,
                headers={"Authorization": "Bearer " + (credential or self.config.admin_token)},
            )
        assert result.status_code == status, "probe_api_status_failed"
        return result.json()

    def run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=45)

    def provision(self):
        self.scope = self.call(
            "/hub/v1/projects",
            method="POST",
            status=201,
            body={"display_name": "Synthetic native recall"},
        )["project_id"]
        issued = self.call(
            "/hub/v1/devices",
            method="POST",
            status=201,
            body={"display_name": "Synthetic native CLI"},
        )
        self.device = issued["device"]["device_id"]
        self.token = issued["credential"]
        self.call(
            f"/hub/v1/devices/{self.device}/grants",
            method="PUT",
            body={
                "expected_revision": 1,
                "grants": [
                    {"scope_id": self.scope, "permissions": ["events:write", "memory:read"]},
                    {"scope_id": "global", "permissions": ["memory:read"]},
                ],
            },
        )
        self.source = self.call(
            "/hub/v1/sources",
            method="POST",
            status=201,
            body={
                "display_name": "Synthetic Claude source",
                "adapter_id": "claude-code",
                "device_id": self.device,
                "scope_id": self.scope,
            },
        )["source_id"]
        self.instance = self.call("/hub/v1/session", credential=self.token)["instance_id"]
        return RemoteConnection(
            origin=self.origin,
            instance_id=self.instance,
            device_id=self.device,
            credential=self.token,
        )

    async def seed(self):
        self.bridge = _sdk_library_bridge(self.app.core)
        sdk = await self.bridge._get_client()
        self.memory_id = str(uuid4())
        for scope, memory_id, content in (
            (self.scope, self.memory_id, V1),
            ("global", str(uuid4()), GLOBAL),
            (str(uuid4()), str(uuid4()), FOREIGN),
        ):
            raw_id = str(uuid4())
            owner = f"cmh:{self.instance}:{scope}"
            result = await asyncio.to_thread(
                sdk.import_memory_exact,
                content,
                memory_id=memory_id,
                user_id=owner,
                agent_id="synthetic-other-agent",
                session_id="synthetic-memory-fixture",
                source="synthetic-fixture",
                request_id=str(uuid4()),
                metadata={"source_raw_memory_ids": [raw_id]},
            )
            assert result["success"] is True
            await self.bridge.import_sdk_raw_exact(
                content=(
                    "Synthetic fixture covers a blue V1 followed by green V2 and concise replies."
                ),
                raw_id=raw_id,
                user_id=owner,
                agent_id="synthetic-other-agent",
                session_id="synthetic-memory-fixture",
                source="synthetic-fixture",
                memory_at=None,
                metadata={},
                request_id=str(uuid4()),
            )

    def close(self):
        self.server.should_exit = True
        self.thread.join(15)
        self.socket.close()
        assert not self.thread.is_alive(), "probe_hub_did_not_stop"


def wait_accepted(capture, sender, count):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        assert sender.poll() is None, "probe_sender_exited_early"
        if capture.outbox().status()["accepted"] == count:
            return
        time.sleep(0.1)
    raise RuntimeError("native_events_not_accepted")


def probe():
    os.umask(0o077)
    executable = shutil.which("claude")
    assert executable is not None, "claude_cli_not_installed"
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
    root = Path(tempfile.mkdtemp(prefix="cmh-claude-recall-")).resolve()
    (root / "config").mkdir()
    (root / "project").mkdir()
    (root / "observer.py").write_text(OBSERVER)
    hub = capture = session = model = sender = None
    model_thread = None
    handles = []
    try:
        hub = LiveHub(root)
        connection = hub.provision()
        hub.run(hub.seed())
        capture = ProductCapture(
            root, version, connection=connection, source_id=hub.source, scope_id=hub.scope
        )
        observer = shlex.join([sys.executable, str(root / "observer.py")])
        hook = shlex.join(
            [
                sys.executable,
                "-m",
                "cloud_memory_hub.connector",
                "claude-hook",
                "--connection-file",
                str(root / "connection.json"),
                "--source-file",
                str(root / "source.json"),
                "--include-global",
            ]
        )
        (root / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {
                                "hooks": [
                                    {"type": "command", "command": observer, "timeout": 10},
                                    *(
                                        [{"type": "command", "command": hook, "timeout": 12}]
                                        if event == "UserPromptSubmit"
                                        else []
                                    ),
                                ]
                            }
                        ]
                        for event in ("SessionStart", "UserPromptSubmit", "Stop")
                    }
                }
            )
        )
        handles = [(root / "delivery.jsonl").open("w"), (root / "delivery-stderr.log").open("w")]
        sender = subprocess.Popen(
            [sys.executable, "-m", "cloud_memory_hub.connector", "watch", *capture.options],
            stdout=handles[0],
            stderr=handles[1],
        )
        model = ModelStub("synthetic-" + uuid4().hex)
        model_thread = threading.Thread(target=model.serve_forever, daemon=True)
        model_thread.start()
        session = NativeSession(root, executable, model)
        turns = [session.turn(PROMPT)]
        capture.wait_for_records(2)
        wait_accepted(capture, sender, 2)
        hub.run(hub.bridge.update(hub.memory_id, V2))
        turns.append(session.turn(PROMPT))
        capture.wait_for_records(4)
        wait_accepted(capture, sender, 4)
        hub.call(
            f"/hub/v1/devices/{hub.device}/grants",
            method="PUT",
            body={"expected_revision": 2, "grants": []},
        )
        turns.append(session.turn(PROMPT))
        capture.wait_for_records(6)
        sender.wait(timeout=12)
        assert sender.returncode == 2, "revoked_sender_not_stopped"
        hub.offline = True
        turns.append(session.turn(PROMPT))
        capture.wait_for_records(8)
        session.finish()
        assert len(model.requests) == 4 and len({turn["session_id"] for turn in turns}) == 1
        latest = [json.dumps(request["messages"][-1]) for request in model.requests]
        assert "CMH_RECALL_V1" in latest[0] and "CMH_RECALL_V2" not in latest[0]
        assert "CMH_RECALL_V2" in latest[1] and "CMH_RECALL_V1" not in latest[1]
        assert all("CMH_GLOBAL_PREF" in value for value in latest[:2])
        assert "was denied" in latest[2] and "unavailable for this turn" in latest[3]
        assert all(
            "CMH_RECALL_V" not in value and "CMH_GLOBAL_PREF" not in value for value in latest[2:]
        )
        assert all("CMH_FOREIGN_PRIVATE" not in json.dumps(request) for request in model.requests)
        assert hub.searches == 4
        with sqlite3.connect(capture.outbox().path.as_uri() + "?mode=ro", uri=True) as db:
            events = [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM events ORDER BY sequence")
            ]
        assert [event["content"]["messages"][0]["text"] for event in events] == [
            text for number in range(1, 5) for text in (PROMPT, f"CMH_SYNTHETIC_REPLY_{number}")
        ], "injected_memory_was_recaptured"
        assert capture.outbox().status() == {"queued": 4, "accepted": 4, "rejected": {}}
        with sqlite3.connect(
            (hub.config.data_dir / "one-memory.sqlite3").as_uri() + "?mode=ro", uri=True
        ) as db:
            jobs = db.execute(
                "SELECT COUNT(*) FROM memory_write_jobs WHERE source_key=?", ("cmh:" + hub.source,)
            ).fetchone()[0]
        assert jobs == 4
        report = {
            "status": "passed",
            "cli_version": version,
            "turns": 4,
            "same_process": True,
            "same_prompt_refreshed": True,
            "real_sdk_and_http": True,
            "automatic_capture_and_delivery": True,
            "accepted_events": 4,
            "revoked_events_kept_local": 4,
            "fresh_context_denied_after_revocation": True,
            "unavailable_does_not_reinject_old_memory": True,
            "foreign_scope_excluded": True,
            "injected_context_recaptured": False,
            "model": "local_test_double",
            "memory_input": "seeded_real_sdk",
            "extraction_validated": False,
            "nas_network_validated": False,
        }
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        print(f"Private synthetic evidence: {root}")
    except BaseException:
        print(f"Native recall probe failed; private synthetic evidence: {root}", file=sys.stderr)
        raise
    finally:
        if session is not None:
            session.close()
        if capture is not None:
            capture.close()
        if sender is not None and sender.poll() is None:
            sender.terminate()
            try:
                sender.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sender.kill()
                sender.wait(timeout=5)
        for handle in handles:
            handle.close()
        if model is not None:
            model.shutdown()
            model.server_close()
            model_thread.join(timeout=5)
        if hub is not None:
            hub.close()


if __name__ == "__main__":
    probe()
