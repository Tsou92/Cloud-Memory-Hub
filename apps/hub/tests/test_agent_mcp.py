"""Real MCP child processes communicate with a live, separately authorized Hub."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cloud_memory_hub.agent_config import AgentConfigurationError, AgentSettings
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_agent_access import preserve_pytest_log_handlers as preserve_pytest_log_handlers
from test_remote_ingest import provision
from test_transport import connection_data
from test_transport import live_hub as live_hub_fixture

live_hub = live_hub_fixture


def config_file(root, connection, scope, source):
    root.mkdir(mode=0o700)
    remote = root / "connection.json"
    remote.write_text(json.dumps(connection))
    remote.chmod(0o600)
    config = root / "agent.json"
    config.write_text(
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
    config.chmod(0o600)
    AgentSettings.from_file(config).open(initialize=True)
    return config


@asynccontextmanager
async def mcp_session(config):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cloud_memory_hub.agent_mcp", "--config-file", str(config)],
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.defpath},
    )
    with (config.parent / "mcp.stderr").open("w") as errors:
        async with stdio_client(parameters, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=30) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "Cloud Memory Hub"
                yield session


def result_data(result):
    assert not result.is_error
    if result.structured_content is not None:
        return result.structured_content
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def provision_second(client, admin, raw, *, scope):
    created = await client.post(
        "/hub/v1/devices", headers=admin, json={"display_name": "Synthetic second Agent"}
    )
    assert created.status_code == 201
    device = created.json()
    device_id = device["device"]["device_id"]
    grants = await client.put(
        f"/hub/v1/devices/{device_id}/grants",
        headers=admin,
        json={
            "expected_revision": 1,
            "grants": [
                {"scope_id": scope, "permissions": ["events:write", "memory:read", "history:read"]}
            ],
        },
    )
    assert grants.status_code == 200
    source = await client.post(
        "/hub/v1/sources",
        headers=admin,
        json={
            "display_name": "Synthetic MCP",
            "device_id": device_id,
            "scope_id": scope,
            "adapter_id": "mcp",
        },
    )
    assert source.status_code == 201
    return {
        "schema_version": 1,
        "origin": str(client.base_url).rstrip("/"),
        "instance_id": raw["instance_id"],
        "device_id": device_id,
        "credential": device["credential"],
    }, source.json()["source_id"]


def test_real_mcp_two_devices_share_updates_and_complete_handoff(live_hub, tmp_path):
    config, _app, uploaded = live_hub

    async def scenario():
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as web:
            admin, _headers, credential, raw = await provision(web, config)
            granted = await web.put(
                f"/hub/v1/devices/{raw['device_id']}/grants",
                headers=admin,
                json={
                    "expected_revision": 2,
                    "grants": [
                        {
                            "scope_id": raw["scope_id"],
                            "permissions": ["events:write", "memory:read", "history:read"],
                        }
                    ],
                },
            )
            assert granted.status_code == 200
            first = config_file(
                tmp_path / "first",
                {
                    "schema_version": 1,
                    "origin": config.public_origin,
                    "instance_id": raw["instance_id"],
                    "device_id": raw["device_id"],
                    "credential": credential,
                },
                raw["scope_id"],
                raw["source_id"],
            )
            second_connection, source_id = await provision_second(
                web, admin, raw, scope=raw["scope_id"]
            )
            second = config_file(tmp_path / "second", second_connection, raw["scope_id"], source_id)
            async with mcp_session(first) as writer, mcp_session(second) as reader:
                tools = await writer.list_tools()
                assert {item.name for item in tools.tools} == {
                    "memory_search",
                    "memory_recent",
                    "session_briefs",
                    "session_markdown",
                    "memory_remember",
                    "handoff_save",
                    "handoff_load",
                    "memory_status",
                    "memory_sync",
                }
                request_id = str(uuid4())
                remembered = result_data(
                    await writer.call_tool(
                        "memory_remember",
                        {"text": "Synthetic plan: use green navigation.", "request_id": request_id},
                    )
                )
                assert remembered["status"] == "accepted"
                recent = result_data(await reader.call_tool("memory_recent", {}))
                assert any("green navigation" in item["content"] for item in recent["items"])
                saved = result_data(
                    await writer.call_tool(
                        "handoff_save",
                        {
                            "goal": "Finish the synthetic shared memory demo",
                            "state": "A completed the green navigation decision. " * 85,
                            "decisions": ["Use green navigation"],
                            "next_steps": ["B should implement the navigation"],
                            "blockers": [],
                            "request_id": str(uuid4()),
                        },
                    )
                )
                assert saved["status"] == "accepted"
                loaded = result_data(
                    await reader.call_tool("handoff_load", {"handoff_id": saved["handoff_id"]})
                )
                assert "B should implement the navigation" in json.dumps(loaded)
                assert loaded["content_trust"] == "untrusted_history"
                repeated = result_data(
                    await writer.call_tool(
                        "memory_remember",
                        {"text": "Synthetic plan: use green navigation.", "request_id": request_id},
                    )
                )
                assert repeated["receipt_id"] == remembered["receipt_id"]
                invalid = await reader.call_tool("handoff_load", {"handoff_id": "not-an-id"})
                assert invalid.is_error
                assert credential not in str(invalid)
                invalid_input = await reader.call_tool(
                    "memory_remember",
                    {
                        "text": {
                            "password": "CMH_FAKE_SECRET"  # pragma: allowlist secret
                        }
                    },
                )
                assert invalid_input.is_error
                assert "CMH_FAKE_SECRET" not in str(invalid_input)
                _app.core.state.transport_test_offline = True
                unavailable = await reader.call_tool("memory_recent", {})
                assert unavailable.is_error
                error = json.loads(unavailable.content[0].text)
                assert error["retryable"] is True
                cli = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "cloud_memory_hub.agent_cli",
                        "--config-file",
                        str(second),
                        "search",
                    ],
                    input='{"query":"synthetic next step"}',
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert cli.returncode == 3
                assert json.loads(cli.stderr)["retryable"] is True
                _app.core.state.transport_test_offline = False
            assert "CMH_SYNTHETIC_REJECTED_SECRET" not in (second.parent / "mcp.stderr").read_text()
        assert len(uploaded) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["public", "relative", "unknown", "symlink"])
def test_agent_configuration_rejects_unsafe_or_ambiguous_files(tmp_path, mutation):
    config = config_file(tmp_path / "local", connection_data(), str(uuid4()), str(uuid4()))
    if mutation == "public":
        config.chmod(0o644)
    elif mutation == "symlink":
        link = tmp_path / "alias.json"
        link.symlink_to(config)
        config = link
    else:
        data = json.loads(config.read_text())
        if mutation == "relative":
            data["connection_file"] = "connection.json"
        else:
            data["admin_token"] = "synthetic_not_allowed"
        config.write_text(json.dumps(data))
    with pytest.raises(AgentConfigurationError, match="^agent_configuration_invalid$"):
        AgentSettings.from_file(config)


def test_cli_missing_state_is_not_recreated_and_stderr_is_safe(tmp_path):
    config = config_file(tmp_path / "local", connection_data(), str(uuid4()), str(uuid4()))
    data = json.loads(config.read_text())
    data["state_dir"] = str(tmp_path / "absent")
    config.write_text(json.dumps(data))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cloud_memory_hub.agent_cli",
            "--config-file",
            str(config),
            "status",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert not Path(data["state_dir"]).exists()
    assert str(tmp_path) not in result.stderr
    assert json.loads(result.stderr)["retryable"] is False
