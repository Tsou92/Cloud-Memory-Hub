"""Synthetic checks against two already-paired MCP clients and their HTTPS Hub.

Run with the installed Hub Python and --config-file pointing to private JSON:
{"schema_version":1,"hub_origin":"https://memory.example.test",
 "local":{"python":"/absolute/python","agent_config":"/private/agent.json"},
 "remote":{"ssh":["ssh","host-alias"],"python":"/absolute/python",
           "agent_config":"/private/agent.json"}}

The remote command uses ordinary SSH stdio, never a forwarded Hub port. Physical
host identity is verified separately by the operator, not inferred by this probe.
No Hub, device, project, or Agent configuration is created or changed here.
"""

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from cloud_memory_hub.agent_config import _private_json
from cloud_memory_hub.onboarding.connect import TOOL_NAMES
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from probe_agent_access import TOOLS, call

PREFLIGHT = """import json,sys
from pathlib import Path
from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.transport import RemoteConnection
settings=AgentSettings.from_file(Path(sys.argv[1]))
connection=RemoteConnection.from_file(settings.connection_file)
assert connection.origin == sys.argv[2] and connection.origin.startswith('https://')
settings.open()
print(json.dumps({'instance_id':connection.instance_id,'device_id':connection.device_id,
 'scope_id':settings.scope_id,'source_id':settings.source_id}))
"""


def configuration(path):
    value = _private_json(path)
    assert type(value) is dict and set(value) == {
        "schema_version",
        "hub_origin",
        "local",
        "remote",
    }, "probe_configuration_invalid"
    assert type(value["schema_version"]) is int and value["schema_version"] == 1
    origin = urlsplit(value["hub_origin"])
    assert (
        origin.scheme == "https"
        and origin.hostname
        and not origin.path
        and not origin.username
        and not origin.password
        and not origin.query
        and not origin.fragment
    ), "probe_https_required"
    for name in ("local", "remote"):
        endpoint = value[name]
        assert type(endpoint) is dict and set(endpoint) == (
            {"python", "agent_config", "ssh"} if name == "remote" else {"python", "agent_config"}
        ), "probe_endpoint_invalid"
        for field in ("python", "agent_config"):
            assert type(endpoint[field]) is str and Path(endpoint[field]).is_absolute()
            assert "\x00" not in endpoint[field]
    ssh = value["remote"]["ssh"]
    assert (
        type(ssh) is list
        and len(ssh) >= 2
        and all(type(part) is str and part and "\x00" not in part for part in ssh)
        and Path(ssh[0]).name == "ssh"
    ), "probe_ssh_command_invalid"
    return value


def command(endpoint, *, origin=None):
    arguments = [endpoint["python"]]
    if origin is None:
        arguments += ["-m", "cloud_memory_hub.agent_mcp", "--config-file", endpoint["agent_config"]]
    else:
        arguments += ["-c", PREFLIGHT, endpoint["agent_config"], origin]
    if "ssh" in endpoint:
        ssh = endpoint["ssh"]
        return [
            ssh[0],
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            *ssh[1:],
            shlex.join(arguments),
        ]
    return arguments


def preflight(endpoint, origin):
    result = subprocess.run(
        command(endpoint, origin=origin),
        capture_output=True,
        text=True,
        timeout=30,
        env=launch_environment(),
    )
    assert result.returncode == 0, "paired_connection_preflight_failed"
    return json.loads(result.stdout)


def launch_environment():
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("HOME", "SSH_AUTH_SOCK"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


@asynccontextmanager
async def connect(arguments):
    parameters = StdioServerParameters(
        command=arguments[0],
        args=arguments[1:],
        env=launch_environment(),
    )
    with open(os.devnull, "w") as errors:
        async with stdio_client(parameters, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=45) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "Cloud Memory Hub", "mcp_identity_invalid"
                advertised = await session.list_tools()
                names = {tool.name for tool in advertised.tools}
                assert names in (TOOL_NAMES, TOOLS), "mcp_tools_incomplete"
                yield session


def same_project(first, second):
    assert first["instance_id"] == second["instance_id"], "hub_instance_mismatch"
    assert first["scope_id"] == second["scope_id"], "project_mismatch"
    assert first["device_id"] != second["device_id"], "independent_devices_required"
    assert first["source_id"] != second["source_id"], "independent_sources_required"


async def exercise(first, second):
    """Shared behavioral checks; local fixture runs do not certify two hosts/TLS."""
    checks = ["both_installed_mcp_servers_and_tools_verified"]
    nonce = uuid4().hex
    async with connect(first) as a, connect(second) as b:
        same_project(await call(a, "memory_status"), await call(b, "memory_status"))
        checks.append("same_hub_project_distinct_devices_and_sources")
        await call(a, "memory_recent", {"limit": 10})
        await call(b, "memory_recent", {"limit": 10})

        async def remember(writer, reader, text):
            request = {"text": text, "request_id": str(uuid4())}
            saved = await call(writer, "memory_remember", request)
            assert saved["status"] == "accepted", "synthetic_write_not_accepted"
            recent = await call(reader, "memory_recent", {"limit": 10})
            assert any(text in row["content"] for row in recent["items"]), "shared_update_missing"
            return request, saved

        request, saved = await remember(a, b, f"Synthetic two-host A decision {nonce}: blue.")
        checks.append("a_write_visible_to_b")
        await remember(b, a, f"Synthetic two-host B decision {nonce}: green.")
        checks.append("b_write_visible_to_a")
        await remember(a, b, f"Synthetic next update {nonce}: continue the green plan.")
        checks.append("same_open_mcp_connection_reads_new_update")
        replay = await call(a, "memory_remember", request)
        assert replay["status"] == "accepted"
        assert (
            replay["event_id"] == saved["event_id"] and replay["receipt_id"] == saved["receipt_id"]
        )
        checks.append("same_request_reuses_event_and_receipt")

        handoff = {
            "goal": "Synthetic complete goal " + "G" * 4000,
            "state": "Synthetic complete state " + "S" * 4000,
            "decisions": [f"Keep the synthetic shared decision {nonce}"],
            "next_steps": ["Continue from the final handoff section"],
            "blockers": [],
        }
        saved_handoff = await call(a, "handoff_save", {**handoff, "request_id": str(uuid4())})
        assert saved_handoff["status"] == "accepted", "handoff_not_accepted"
        loaded = await call(b, "handoff_load", {"handoff_id": saved_handoff["handoff_id"]})
        assert loaded["handoff"] == handoff, "long_handoff_incomplete"
        assert loaded["content_trust"] == "untrusted_history", "handoff_trust_invalid"
        checks.append("complete_long_handoff_across_mcp_clients")
    return checks


def probe(path):
    started = time.monotonic()
    value = configuration(path)
    first = preflight(value["local"], value["hub_origin"])
    second = preflight(value["remote"], value["hub_origin"])
    same_project(first, second)
    checks = asyncio.run(exercise(command(value["local"]), command(value["remote"])))
    return {
        "probe": "two-host-mcp-v1",
        "status": "passed",
        "checks": checks,
        "checks_passed": len(checks),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "both_client_connections_use_configured_https": True,
        "execution": "local_mcp_and_remote_mcp_over_ssh",
        "physical_host_identity_verification": "external_to_probe",
        "real_agents_autonomous_calls_verified": False,
        "agent_model_called_by_probe": False,
        "automatic_extraction_verified": False,
    }


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, required=True)
    options = parser.parse_args()
    try:
        print(json.dumps(probe(options.config_file), sort_keys=True))
    except Exception:
        print(json.dumps({"probe": "two-host-mcp-v1", "status": "failed"}))
        sys.exit(1)
