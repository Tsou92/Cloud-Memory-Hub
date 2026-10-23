"""Installed connector -> real HTTP -> Core queue, using disposable test data."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from cloud_memory_hub.local_outbox import LocalOutbox

BASE = "http://localhost:8800"
STATE = Path("/data/remote-ingest-smoke.json")
OUTBOX = Path("/data/ingest-connector/outbox.sqlite3")
CONNECTION = Path("/data/ingest-connector/connection.json")


def send_from_cli():
    result = subprocess.run(
        [
            "/opt/cmh/venv/bin/cloud-memory-connector",
            "send",
            "--state-dir",
            str(OUTBOX.parent),
            "--connection-file",
            str(CONNECTION),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, json.loads(result.stdout if result.returncode == 0 else result.stderr)


def request(path, credential, method="GET", body=None):
    try:
        with urlopen(
            Request(
                BASE + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Authorization": "Bearer " + credential,
                    "Content-Type": "application/json",
                },
                method=method,
            ),
            timeout=20,
        ) as reply:
            assert reply.headers.get("Cache-Control") == "no-store"
            return reply.status, json.load(reply)
    except HTTPError as error:
        return error.code, None


def job_count(source_id):
    with sqlite3.connect("file:/data/one-memory.sqlite3?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT COUNT(*) FROM memory_write_jobs WHERE source_key=?", ("cmh:" + source_id,)
        ).fetchone()[0]


def probe(phase):
    admin = Path("/config/admin-token").read_text().strip()
    if phase == "before":
        status, project = request(
            "/hub/v1/projects", admin, "POST", {"display_name": "Synthetic ingest project"}
        )
        assert status == 201
        status, issued = request(
            "/hub/v1/devices", admin, "POST", {"display_name": "Synthetic ingest device"}
        )
        assert status == 201
        credential = issued["credential"]
        device_id = issued["device"]["device_id"]
        status, session = request("/hub/v1/session", credential)
        assert status == 200 and "event_upload" in session["features"]
        scope = project["project_id"]
        assert (
            request(
                f"/hub/v1/devices/{device_id}/grants",
                admin,
                "PUT",
                {
                    "expected_revision": 1,
                    "grants": [{"scope_id": scope, "permissions": ["events:write"]}],
                },
            )[0]
            == 200
        )
        status, source = request(
            "/hub/v1/sources",
            admin,
            "POST",
            {
                "display_name": "Synthetic extra adapter",
                "adapter_id": "reference.extra-agent",
                "device_id": device_id,
                "scope_id": scope,
            },
        )
        assert status == 201
        secret = uuid4().hex
        raw = {
            "instance_id": session["instance_id"],
            "device_id": device_id,
            "source_id": source["source_id"],
            "scope_id": scope,
            **{key: str(uuid4()) for key in ("event_id", "conversation_id", "record_id")},
            "revision": 1,
            "kind": "conversation_turn",
            "content": {
                "messages": [
                    {"role": "user", "text": "Reuse the existing durable queue."},
                    {"role": "assistant", "text": "Next: test restart and replay."},
                    {"role": "tool", "text": "password=" + secret},
                ],
                "metadata": {"milestone": "remote-intake", "cookie": secret},
            },
        }
        assert request("/hub/v1/events", credential, "POST", raw)[0] == 422
        assert job_count(source["source_id"]) == 0
        outbox = LocalOutbox.initialize(OUTBOX, instance_id=raw["instance_id"], device_id=device_id)
        outbox.enqueue(raw)
        outgoing = outbox.pending()[0]
        assert secret not in outgoing.payload and secret.encode() not in OUTBOX.read_bytes()
        filtered = json.loads(outgoing.payload)
        assert (
            request("/hub/v1/events", credential, "POST", {**filtered, "scope_id": "global"})[0]
            == 403
        )
        status, receipt = request("/hub/v1/events", credential, "POST", filtered)
        assert status == 202 and receipt["status"] == "accepted"
        assert receipt["processing_state"] == "pending"
        assert receipt["payload_digest"] == outgoing.payload_digest
        assert job_count(source["source_id"]) == 1
        for path in Path("/data").glob("one-memory.sqlite3*"):
            assert secret.encode() not in path.read_bytes()
        state = {
            "instance_id": raw["instance_id"],
            "device_id": device_id,
            "source_id": source["source_id"],
            "credential": credential,
            "receipt": receipt,
        }
        STATE.touch(mode=0o600, exist_ok=False)
        STATE.write_text(json.dumps(state))
        CONNECTION.touch(mode=0o600, exist_ok=False)
        CONNECTION.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "origin": BASE,
                    "instance_id": state["instance_id"],
                    "device_id": device_id,
                    "credential": credential,
                }
            )
        )
        # Deliberately lose the first acknowledgement; resend after process restart.
        assert outbox.status()["queued"] == 1
    else:
        state = json.loads(STATE.read_text())
        credential, receipt = state["credential"], state["receipt"]
        status_path = "/hub/v1/events/" + receipt["event_id"]
        outbox = LocalOutbox(OUTBOX, instance_id=state["instance_id"], device_id=state["device_id"])
        if phase == "after":
            outgoing = outbox.pending()[0]
            status, persisted = request(status_path, credential)
            assert status == 200 and persisted == receipt
            code, report = send_from_cli()
            assert code == 0 and report == {"accepted": 1, "queued": 0}
            assert request(status_path, credential) == (200, receipt)
            assert outbox.status()["accepted"] == 1 and not outbox.pending()
            assert (
                request(
                    f"/hub/v1/sources/{state['source_id']}/revoke",
                    admin,
                    "POST",
                    {"expected_revision": 1},
                )[0]
                == 200
            )
            assert (
                request("/hub/v1/events", credential, "POST", json.loads(outgoing.payload))[0]
                == 403
            )
            outbox.enqueue(
                {**json.loads(outgoing.payload), "event_id": str(uuid4()), "revision": 2}
            )
            code, report = send_from_cli()
            assert code == 2 and report == {
                "error": "source_permission_required",
                "retryable": False,
            }
        assert request(status_path, credential)[0] == 403
        status, listing = request("/hub/v1/sources", admin)
        assert status == 200
        source = next(
            item for item in listing["sources"] if item["source_id"] == state["source_id"]
        )
        assert source["status"] == "revoked" and source["revision"] == 2
        assert job_count(state["source_id"]) == 1
        assert outbox.status()["queued"] == 1 and outbox.status()["accepted"] == 1
    print(f"Filtered HTTP intake, durable replay and source revocation passed ({phase})")


if __name__ == "__main__":
    if sys.argv[1] not in {"before", "after", "revoked"}:
        raise ValueError("Unknown test phase")
    probe(sys.argv[1])
