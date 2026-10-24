"""Installed capture/send -> real HTTP/Core. Only disposable synthetic sources."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from cloud_memory_hub.local_outbox import LocalOutbox

ROOT = Path("/data/source-capture-smoke")
STATE = ROOT / "state.json"
SOURCE = ROOT / "source.json"
CONNECTION = ROOT / "connection.json"
TRANSCRIPT = ROOT / "native" / "synthetic.jsonl"
BASE = "http://localhost:8800"


def request(path, credential, *, method="GET", body=None, status=200):
    with httpx.Client(trust_env=False, timeout=20) as client:
        reply = client.request(
            method, BASE + path, headers={"Authorization": "Bearer " + credential}, json=body
        )
    assert reply.status_code == status, "capture_http_status_mismatch"
    return reply.json()


def cli(command):
    result = subprocess.run(
        [
            "/opt/cmh/venv/bin/cloud-memory-connector",
            command,
            "--state-dir",
            str(ROOT / "queue"),
            "--connection-file",
            str(CONNECTION),
            "--source-file",
            str(SOURCE),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "installed_capture_cli_failed"
    return json.loads(result.stdout)


def append(text, role="user"):
    with TRANSCRIPT.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": role,
                    "uuid": str(uuid4()),
                    "sessionId": "synthetic-container-session",
                    "cwd": "/synthetic-project",
                    "timestamp": "2026-10-24T00:00:00Z",
                    "message": {"role": role, "content": text},
                }
            )
            + "\n"
        )


def probe(phase):
    os.umask(0o077)
    if phase == "before":
        ROOT.mkdir(mode=0o700)
        TRANSCRIPT.parent.mkdir(mode=0o700)
        admin = Path("/config/admin-token").read_text().strip()
        scope = request(
            "/hub/v1/projects",
            admin,
            method="POST",
            status=201,
            body={"display_name": "Synthetic capture project"},
        )["project_id"]
        issued = request(
            "/hub/v1/devices",
            admin,
            method="POST",
            status=201,
            body={"display_name": "Synthetic capture device"},
        )
        device, credential = issued["device"]["device_id"], issued["credential"]
        session = request("/hub/v1/session", credential)
        request(
            f"/hub/v1/devices/{device}/grants",
            admin,
            method="PUT",
            body={
                "expected_revision": 1,
                "grants": [{"scope_id": scope, "permissions": ["events:write"]}],
            },
        )
        source = request(
            "/hub/v1/sources",
            admin,
            method="POST",
            status=201,
            body={
                "display_name": "Synthetic Claude format",
                "adapter_id": "claude-code",
                "device_id": device,
                "scope_id": scope,
            },
        )["source_id"]
        CONNECTION.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "origin": BASE,
                    "instance_id": session["instance_id"],
                    "device_id": device,
                    "credential": credential,
                }
            )
        )
        SOURCE.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": "claude-code",
                    "source_id": source,
                    "scope_id": scope,
                    "source_root": str(TRANSCRIPT.parent),
                    "workspace": "/synthetic-project",
                }
            )
        )
        cli("init")
        assert cli("capture-init")["scanned"] is False
        secret = uuid4().hex
        append("Capture the synthetic decision. password=" + secret)
        append("Next: verify durable acceptance.", "assistant")
        assert cli("collect")["queued"] == 2
        connection = json.loads(CONNECTION.read_text())
        outbox = LocalOutbox(
            ROOT / "queue" / "outbox.sqlite3",
            instance_id=connection["instance_id"],
            device_id=device,
        )
        events = [item.event_id for item in outbox.pending()]
        assert secret.encode() not in outbox.path.read_bytes()
        assert cli("send") == {"accepted": 2, "queued": 0}
        STATE.write_text(json.dumps({"source_id": source, "events": events}))
        for file in Path("/data").glob("one-memory.sqlite3*"):
            assert secret.encode() not in file.read_bytes()
    connection, state = json.loads(CONNECTION.read_text()), json.loads(STATE.read_text())
    expected = 2
    if phase == "after":
        assert cli("collect")["queued"] == 0
        append("A late assistant reply after restart.", "assistant")
        assert cli("collect")["queued"] == 1
        assert cli("send") == {"accepted": 1, "queued": 0}
        expected = 3
    elif phase == "revoked":
        expected = 3
    for event in state["events"]:
        receipt = request("/hub/v1/events/" + event, connection["credential"])
        assert receipt["status"] == "accepted" and receipt["processing_state"] == "pending"
    with sqlite3.connect("file:/data/one-memory.sqlite3?mode=ro", uri=True) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM memory_write_jobs WHERE source_key=?",
            ("cmh:" + state["source_id"],),
        ).fetchone()[0]
        assert count == expected
    assert cli("status")["accepted"] == expected
    print(f"Installed native-format capture, filtered HTTP delivery and restart passed ({phase})")


if __name__ == "__main__":
    if sys.argv[1] not in {"before", "after", "revoked"}:
        raise ValueError("Unknown test phase")
    probe(sys.argv[1])
