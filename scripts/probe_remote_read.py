"""Real SDK + installed read CLI in disposable containers; synthetic content only."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from one_memory.hy_memory_bridge import HyMemoryBridge, builtin_retrieval_snapshot

BASE = "http://localhost:8800"
STATE = Path("/data/read-smoke.json")
CONNECTION = Path("/data/read-connection.json")


def request(path, token, method="GET", body=None):
    try:
        with urlopen(
            Request(
                BASE + path,
                method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            ),
            timeout=30,
        ) as reply:
            assert reply.headers.get("Cache-Control") == "no-store"
            return reply.status, json.load(reply)
    except HTTPError as error:
        return error.code, None


def cli(operation, body):
    result = subprocess.run(
        [
            "/opt/cmh/venv/bin/cloud-memory-connector",
            operation,
            "--connection-file",
            str(CONNECTION),
        ],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, json.loads(result.stdout if result.returncode == 0 else result.stderr)


async def seed(state):
    bridge = HyMemoryBridge(
        profile=builtin_retrieval_snapshot(), api_key=None, data_dir=Path("/data")
    )
    try:
        sdk = await bridge._get_client()
        for key, scope in (("memory_id", state["scope_id"]), ("foreign_id", "global")):
            result = await asyncio.to_thread(
                sdk.import_memory_exact,
                "合成验收：决定使用蓝色设计方案。",
                memory_id=state[key],
                user_id=f"cmh:{state['instance_id']}:{scope}",
                agent_id="synthetic-read-agent",
                session_id=state["conversation_id"],
                source="cmh:" + state["source_id"],
                request_id=str(uuid4()),
                metadata={"source_raw_memory_ids": [state["raw_id"]]},
            )
            assert result["success"] is True
        await bridge.import_sdk_raw_exact(
            content="合成对话：设计使用蓝色，下步检查布局。",
            raw_id=state["raw_id"],
            user_id=f"cmh:{state['instance_id']}:{state['scope_id']}",
            agent_id="synthetic-read-agent",
            session_id=state["conversation_id"],
            source="cmh:" + state["source_id"],
            memory_at=None,
            metadata={},
            request_id=str(uuid4()),
        )
    finally:
        await bridge.close()


def probe(phase):
    if phase == "seed":
        asyncio.run(seed(json.loads(STATE.read_text())))
        print("Synthetic SDK records seeded while the Hub was stopped")
        return
    admin = Path("/config/admin-token").read_text().strip()
    if phase == "prepare":
        status, project = request(
            "/hub/v1/projects", admin, "POST", {"display_name": "Synthetic read project"}
        )
        assert status == 201
        status, issued = request(
            "/hub/v1/devices", admin, "POST", {"display_name": "Synthetic read connector"}
        )
        assert status == 201
        token, device = issued["credential"], issued["device"]["device_id"]
        status, session = request("/hub/v1/session", token)
        assert status == 200 and "scoped_reads" in session["features"]
        scope = project["project_id"]
        assert (
            request(
                f"/hub/v1/devices/{device}/grants",
                admin,
                "PUT",
                {
                    "expected_revision": 1,
                    "grants": [{"scope_id": scope, "permissions": ["memory:read", "history:read"]}],
                },
            )[0]
            == 200
        )
        state = {
            "scope_id": scope,
            "device_id": device,
            "instance_id": session["instance_id"],
            **{
                key: str(uuid4())
                for key in ("memory_id", "foreign_id", "raw_id", "source_id", "conversation_id")
            },
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
                    "device_id": device,
                    "credential": token,
                }
            )
        )
        return
    else:
        state = json.loads(STATE.read_text())
        token = json.loads(CONNECTION.read_text())["credential"]
    scope = {"scope_id": state["scope_id"]}
    if phase == "revoked":
        assert cli("memory-list", scope) == (
            2,
            {"error": "scope_permission_required", "retryable": False},
        )
        assert (
            request("/hub/v1/history/get", token, "POST", {**scope, "record_id": state["raw_id"]})[
                0
            ]
            == 403
        )
        print("Revoked read grant remains denied after restart")
        return
    code, result = cli("memory-search", {**scope, "query": "决定使用蓝色设计方案"})
    assert code == 0 and [i["record_id"] for i in result["items"]] == [state["memory_id"]]
    assert result["content_trust"] == "untrusted_history"
    assert result["items"][0]["source_raw_ids"] == [state["raw_id"]]
    code, history = cli("history-get", {**scope, "record_id": state["raw_id"]})
    assert code == 0 and history["items"][0]["source"] == "cmh:" + state["source_id"]
    assert history["items"][0]["session_id"] == state["conversation_id"]
    assert (
        request("/hub/v1/memories/get", token, "POST", {**scope, "record_id": state["foreign_id"]})[
            0
        ]
        == 404
    )
    if phase == "after":
        assert (
            request(
                f"/hub/v1/devices/{state['device_id']}/grants",
                admin,
                "PUT",
                {
                    "expected_revision": 2,
                    "grants": [],
                },
            )[0]
            == 200
        )
        assert cli("history-list", scope)[0] == 2
    print(f"Real SDK scoped search, provenance and installed read CLI passed ({phase})")


if __name__ == "__main__":
    probe(sys.argv[1])
