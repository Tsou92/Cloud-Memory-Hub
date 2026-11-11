"""Installed MCP/HTTP smoke: synthetic identities, no external model or daily Agent.

Run with the installed Hub Python. Keep this file beside probe_claude_recall.py
and probe_claude_native.py to reuse the existing isolated LiveHub fixture.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from cloud_memory_hub.onboarding.connect import SESSION_TOOL_NAMES, TOOL_NAMES
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from probe_claude_recall import LiveHub

TOOLS = TOOL_NAMES | SESSION_TOOL_NAMES
SECRET = "CMH_SYNTHETIC_PASSWORD_ONLY"  # pragma: allowlist secret -- synthetic filtering fixture


def device_config(hub, root, scope, name):
    root.mkdir(mode=0o700)
    issued = hub.call("/hub/v1/devices", method="POST", status=201, body={"display_name": name})
    device = issued["device"]["device_id"]
    credential = issued["credential"]
    hub.call(
        f"/hub/v1/devices/{device}/grants",
        method="PUT",
        body={
            "expected_revision": 1,
            "grants": [
                {"scope_id": scope, "permissions": ["events:write", "memory:read", "history:read"]}
            ],
        },
    )
    source = hub.call(
        "/hub/v1/sources",
        method="POST",
        status=201,
        body={
            "display_name": "Synthetic MCP source",
            "adapter_id": "mcp",
            "device_id": device,
            "scope_id": scope,
        },
    )["source_id"]
    instance = hub.call("/hub/v1/session", credential=credential)["instance_id"]
    remote = root / "connection.json"
    remote.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "origin": hub.origin,
                "instance_id": instance,
                "device_id": device,
                "credential": credential,
            }
        )
    )
    settings = root / "agent.json"
    settings.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connection_file": str(remote),
                "state_dir": str(root / "state"),
                "scope_id": scope,
                "source_id": source,
            }
        )
    )
    remote.chmod(0o600)
    settings.chmod(0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cloud_memory_hub.agent_cli",
            "--config-file",
            str(settings),
            "init",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, "installed_cli_initialization_failed"
    assert json.loads(result.stdout)["status"] == "initialized", "installed_cli_reply_invalid"
    return settings


@asynccontextmanager
async def client(settings):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cloud_memory_hub.agent_mcp", "--config-file", str(settings)],
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.defpath},
    )
    with (settings.parent / "mcp.stderr").open("w") as errors:
        async with stdio_client(parameters, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=30) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "Cloud Memory Hub", "mcp_identity_invalid"
                tools = await session.list_tools()
                assert {item.name for item in tools.tools} == TOOLS, "mcp_tools_incomplete"
                yield session


async def call(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    assert not result.is_error, "mcp_action_failed_" + name
    if result.structured_content is not None:
        return result.structured_content
    assert len(result.content) == 1 and result.content[0].type == "text", "mcp_result_invalid"
    return json.loads(result.content[0].text)


async def exercise(hub, first, second, foreign):
    checks = []
    async with client(first) as writer, client(second) as reader:
        checks.append("installed_stdio_initialization_and_tools")
        baseline = await call(reader, "memory_recent")
        assert baseline["items"] == [], "new_project_not_empty"
        request = {
            "text": "Synthetic shared plan: use green navigation.",
            "request_id": str(uuid4()),
        }
        started = time.monotonic()
        saved = await call(writer, "memory_remember", request)
        assert saved["status"] == "accepted", "remember_not_accepted"
        recent = await call(reader, "memory_recent")
        assert any("green navigation" in row["content"] for row in recent["items"]), (
            "update_missing"
        )
        elapsed = round(time.monotonic() - started, 3)
        checks.append("same_open_reader_sees_new_accepted_update")
        repeated = await call(writer, "memory_remember", request)
        assert repeated["receipt_id"] == saved["receipt_id"], "remember_retry_duplicated"
        checks.append("stable_request_receipt_replay")

        goal = "G" * 4096
        state = "Synthetic handoff credential password=" + SECRET
        handoff_request = {
            "goal": goal,
            "state": state,
            "decisions": ["Keep the green navigation decision"],
            "next_steps": ["Continue implementing the synthetic navigation"],
            "blockers": [],
            "request_id": str(uuid4()),
        }
        handoff = await call(writer, "handoff_save", handoff_request)
        assert handoff["status"] == "accepted", "handoff_not_accepted"
        loaded = await call(reader, "handoff_load", {"handoff_id": handoff["handoff_id"]})
        assert loaded["handoff"]["goal"] == goal, "handoff_truncated"
        assert loaded["handoff"]["next_steps"] == handoff_request["next_steps"], "handoff_changed"
        assert SECRET not in json.dumps(loaded), "handoff_secret_visible"
        assert "password=" in loaded["handoff"]["state"], "handoff_unrelated_text_lost"
        assert loaded["content_trust"] == "untrusted_history", "handoff_trust_invalid"
        assert SECRET.encode() not in (first.parent / "state/outbox.sqlite3").read_bytes(), (
            "local_outbox_secret_visible"
        )
        checks.append("complete_multipage_handoff_with_local_credential_filter")
        async with client(foreign) as outsider:
            denied = await outsider.call_tool("handoff_load", {"handoff_id": handoff["handoff_id"]})
            assert denied.is_error, "foreign_project_read_allowed"
            assert goal not in str(denied), "foreign_project_content_visible"
        checks.append("foreign_project_handoff_denied")

        pending_request = {
            "text": "Synthetic offline progress to replay.",
            "request_id": str(uuid4()),
        }
        hub.offline = True
        pending = await call(writer, "memory_remember", pending_request)
        assert pending["status"] == "queued" and pending["retryable"], "offline_not_queued"
        assert pending["receipt_id"] is None, "offline_false_receipt"
        hub.offline = False
        synced = await call(writer, "memory_sync")
        assert synced["queued"] == 0 and synced["delivery"] == "attempted", "sync_incomplete"
        accepted = await call(writer, "memory_remember", pending_request)
        assert accepted["event_id"] == pending["event_id"], "retry_identity_changed"
        assert accepted["status"] == "accepted", "replayed_event_not_accepted"
        latest = await call(reader, "memory_recent")
        assert any("offline progress" in row["content"] for row in latest["items"]), (
            "replayed_update_missing"
        )
        checks.append("offline_queue_and_same_identity_sync")
    return checks, elapsed


def probe():
    os.umask(0o077)
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="cmh-agent-access-")).resolve()
    hub = None
    succeeded = False
    try:
        hub = LiveHub(root)
        scope = hub.call(
            "/hub/v1/projects", method="POST", status=201, body={"display_name": "Synthetic shared"}
        )["project_id"]
        other = hub.call(
            "/hub/v1/projects",
            method="POST",
            status=201,
            body={"display_name": "Synthetic foreign"},
        )["project_id"]
        first = device_config(hub, root / "first", scope, "Synthetic Agent A")
        second = device_config(hub, root / "second", scope, "Synthetic Agent B")
        foreign = device_config(hub, root / "foreign", other, "Synthetic foreign Agent")
        checks, update_seconds = asyncio.run(exercise(hub, first, second, foreign))
        hub.close()
        hub = None
        succeeded = True
        return {
            "probe": "agent-access-installed-v1",
            "status": "passed",
            "checks": checks,
            "checks_passed": len(checks),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "synthetic_update_seconds": update_seconds,
            "external_model_used": False,
            "two_physical_macs_verified": False,
            "automatic_extraction_verified": False,
        }
    finally:
        if hub is not None:
            hub.close()
        if succeeded:
            shutil.rmtree(root)


if __name__ == "__main__":
    try:
        print(json.dumps(probe(), sort_keys=True))
    except Exception:
        print(json.dumps({"probe": "agent-access-installed-v1", "status": "failed"}))
        raise SystemExit(1) from None
