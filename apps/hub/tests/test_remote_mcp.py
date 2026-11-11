"""ADR-0065: official HTTP MCP client against a real local Hub, synthetic identities only."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from one_memory.hy_memory_bridge import HySdkMemoryRecord
from starlette.responses import JSONResponse
from test_agent_access import preserve_pytest_log_handlers as preserve_pytest_log_handlers
from test_agent_access import read_grants
from test_agent_mcp import provision_second, result_data
from test_remote_ingest import count_jobs, provision
from test_transport import live_hub as live_hub_fixture

live_hub = live_hub_fixture


def endpoint(config, source_id):
    return f"{config.public_origin}/hub/v1/mcp/{source_id}"


@asynccontextmanager
async def remote_session(config, source_id, credential):
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + credential}, trust_env=False
    ) as client:
        async with streamable_http_client(endpoint(config, source_id), http_client=client) as (
            reader,
            writer,
        ):
            async with ClientSession(reader, writer, read_timeout_seconds=15) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "Cloud Memory Hub"
                assert "no client-side offline queue" in initialized.instructions
                yield session


async def bindings(config, web):
    admin, headers, credential, raw = await provision(web, config)
    await read_grants(web, admin, raw["device_id"], raw["scope_id"])
    second, source = await provision_second(web, admin, raw, scope=raw["scope_id"])
    return admin, headers, credential, raw, second, source


def test_remote_mcp_seven_tools_two_concurrent_devices_and_idempotent_handoff(live_hub):
    config, app, _ = live_hub

    async def scenario():
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as web:
            admin, _, credential, raw, second, source = await bindings(config, web)
            async with (
                remote_session(config, raw["source_id"], credential) as first,
                remote_session(config, source, second["credential"]) as other,
            ):
                tools = (await first.list_tools()).tools
                assert {tool.name for tool in tools} == {
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
                for tool in tools:
                    properties = tool.input_schema.get("properties", {})
                    assert not {"ctx", "scope_id", "source_id", "device_id"} & properties.keys()
                request_id = str(uuid4())
                results = await asyncio.gather(
                    first.call_tool(
                        "memory_remember",
                        {
                            "text": "Synthetic first device decision",
                            "request_id": request_id,
                        },
                    ),
                    other.call_tool(
                        "memory_remember",
                        {
                            "text": "Synthetic second device decision",
                            "request_id": request_id,
                        },
                    ),
                )
                receipts = [result_data(result) for result in results]
                assert [r["device_id"] for r in receipts] == [raw["device_id"], second["device_id"]]
                assert receipts[0]["receipt_id"] != receipts[1]["receipt_id"]
                assert all(
                    r["status"] == "accepted" and not r["client_offline_queue"] for r in receipts
                )
                duplicate = result_data(
                    await first.call_tool(
                        "memory_remember",
                        {
                            "text": "Synthetic first device decision",
                            "request_id": request_id,
                        },
                    )
                )
                assert duplicate["receipt_id"] == receipts[0]["receipt_id"]
                conflict = await first.call_tool(
                    "memory_remember",
                    {
                        "text": "Changed synthetic decision",
                        "request_id": request_id,
                    },
                )
                assert conflict.is_error
                assert json.loads(conflict.content[0].text)["error"] == "remote_identity_conflict"
                document = {
                    "goal": "Synthetic goal",
                    "state": "Synthetic state " * 240,
                    "decisions": ["Keep the existing shared memory"],
                    "next_steps": ["Continue on the second device"],
                    "blockers": [],
                }
                saved = result_data(await first.call_tool("handoff_save", document))
                loaded = result_data(
                    await other.call_tool(
                        "handoff_load",
                        {
                            "handoff_id": saved["handoff_id"],
                        },
                    )
                )
                assert loaded["handoff"] == document
                assert loaded["source_id"] == raw["source_id"]
                assert loaded["content_trust"] == "untrusted_history"
                recent = result_data(await other.call_tool("memory_recent", {}))
                assert len(recent["items"]) == 3
                assert recent["scope_id"] == raw["scope_id"]
                status_results = await asyncio.gather(
                    first.call_tool("memory_status", {}),
                    other.call_tool("memory_sync", {}),
                )
                for result, device in zip(
                    status_results, [raw["device_id"], second["device_id"]], strict=True
                ):
                    status = result_data(result)
                    assert status["device_id"] == device and status["status"] == "connected"
                    assert status["client_offline_queue"] is False
                    assert status["local_history_upload"] is False
                    assert "queued" not in status
                # Search uses a synthetic processed record; the writes/history/handoff
                # above exercised the real SQLite and original archive path.
                record = HySdkMemoryRecord(
                    memory_id=str(uuid4()),
                    content="Synthetic processed memory",
                    layer="l2_fact",
                    status="active",
                    is_latest=True,
                    user_id=f"cmh:{raw['instance_id']}:{raw['scope_id']}",
                    agent_id="synthetic",
                )

                async def search(*args, **kwargs):
                    return (SimpleNamespace(memory_id=record.memory_id, member_ids=()),)

                async def get(_):
                    return record

                app.core.state.cmh_reads.bridge = lambda: SimpleNamespace(
                    search_memories=search,
                    get_sdk_memory=get,
                )
                found = result_data(await other.call_tool("memory_search", {"query": "synthetic"}))
                assert found["items"][0]["record_id"] == record.memory_id
                assert count_jobs(config) == 3
                assert not list(config.data_dir.rglob("*outbox*"))

    asyncio.run(scenario())


def request_payload():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "memory_status",
            "arguments": {},
        },
    }


def wire_headers(credential):
    return {
        "Authorization": "Bearer " + credential,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }


def test_remote_mcp_cross_scope_and_current_device_source_authorization(live_hub):
    config, app, _ = live_hub

    async def scenario():
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as web:
            admin, _, credential, raw, second, source = await bindings(config, web)
            project = (
                await web.post(
                    "/hub/v1/projects",
                    headers=admin,
                    json={
                        "display_name": "Other synthetic project",
                    },
                )
            ).json()["project_id"]
            stranger, alien_source = await provision_second(web, admin, raw, scope=project)
            async with (
                remote_session(config, raw["source_id"], credential) as first,
                remote_session(config, alien_source, stranger["credential"]) as alien,
            ):
                saved = result_data(
                    await first.call_tool(
                        "handoff_save",
                        {
                            "goal": "Private synthetic goal",
                            "state": "Current synthetic state",
                        },
                    )
                )
                forbidden = await alien.call_tool(
                    "handoff_load", {"handoff_id": saved["handoff_id"]}
                )
                assert forbidden.is_error and "Private synthetic goal" not in str(forbidden)
                override = await alien.call_tool("memory_recent", {"scope_id": raw["scope_id"]})
                assert override.is_error
                reduced = await web.put(
                    f"/hub/v1/devices/{raw['device_id']}/grants",
                    headers=admin,
                    json={
                        "expected_revision": 3,
                        "grants": [{"scope_id": raw["scope_id"], "permissions": ["events:write"]}],
                    },
                )
                assert reduced.status_code == 200
                # Same already-initialized connection must observe current read grants.
                denied_read = await first.call_tool("memory_recent", {})
                assert denied_read.is_error
                assert (
                    json.loads(denied_read.content[0].text)["error"] == "scope_permission_required"
                )
            wrong_source = await web.post(
                endpoint(config, source), headers=wire_headers(credential), json=request_payload()
            )
            assert wrong_source.status_code == 403
            assert credential not in wrong_source.text
            # An administrator cookie alone, and even the admin Bearer, never
            # substitutes for a separately granted device identity.
            code, _ = app.sessions.issue_exchange()
            cookie = app.sessions.consume_exchange(code)
            for headers in (
                {"Cookie": "cmh_session=" + cookie, "Origin": config.public_origin},
                admin,
            ):
                denied = await web.post(
                    endpoint(config, source), headers=headers, json=request_payload()
                )
                assert denied.status_code == 401
            rotated = (
                await web.post(
                    f"/hub/v1/devices/{second['device_id']}/rotate",
                    headers=admin,
                    json={"expected_revision": 2},
                )
            ).json()
            old = await web.post(
                endpoint(config, source),
                headers=wire_headers(second["credential"]),
                json=request_payload(),
            )
            assert old.status_code == 401
            fresh = rotated["credential"]
            async with remote_session(config, source, fresh) as renewed:
                assert (
                    result_data(await renewed.call_tool("memory_status", {}))["status"]
                    == "connected"
                )
                revoked = await web.post(
                    f"/hub/v1/devices/{second['device_id']}/revoke",
                    headers=admin,
                    json={"expected_revision": 3},
                )
                assert revoked.status_code == 200
                denied = await web.post(
                    endpoint(config, source), headers=wire_headers(fresh), json=request_payload()
                )
                assert denied.status_code == 401
            revoked_source = await web.post(
                f"/hub/v1/sources/{raw['source_id']}/revoke",
                headers=admin,
                json={"expected_revision": 1},
            )
            assert revoked_source.status_code == 200
            denied = await web.post(
                endpoint(config, raw["source_id"]),
                headers=wire_headers(credential),
                json=request_payload(),
            )
            assert denied.status_code == 403

    asyncio.run(scenario())


def test_remote_mcp_fixed_errors_no_queue_and_http_security(live_hub, monkeypatch):
    config, app, _ = live_hub

    async def scenario():
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as web:
            _, _, credential, raw, _, _ = await bindings(config, web)
            async with remote_session(config, raw["source_id"], credential) as session:
                invalid = await session.call_tool(
                    "memory_remember",
                    {
                        "text": {
                            "password": "CMH_SYNTHETIC_REJECTED_SECRET",  # pragma: allowlist secret
                        }
                    },
                )
                assert invalid.is_error
                assert "CMH_SYNTHETIC_REJECTED_SECRET" not in str(invalid)
                assert json.loads(invalid.content[0].text)["error"] == "agent_arguments_invalid"
                saved = result_data(
                    await session.call_tool("memory_remember", {"text": "Synthetic " + credential})
                )
                assert saved["status"] == "accepted"
                recent = result_data(await session.call_tool("memory_recent", {}))
                assert credential not in json.dumps(recent)
                for path in config.data_dir.glob("*.sqlite3*"):
                    assert credential.encode() not in path.read_bytes()
                route = next(r for r in app.remote_api.routes if r.path == "/hub/v1/events")
                monkeypatch.setattr(
                    route, "app", JSONResponse({"detail": "synthetic_unavailable"}, 503)
                )
                unavailable = await session.call_tool(
                    "memory_remember", {"text": "Synthetic failed update"}
                )
                assert unavailable.is_error
                assert json.loads(unavailable.content[0].text) == {
                    "error": "remote_temporarily_unavailable",
                    "retryable": True,
                }
                assert "queued" not in str(unavailable) and count_jobs(config) == 1
            for headers in (
                {"Host": "evil.example.invalid"},
                {"Origin": "https://evil.example.invalid"},
            ):
                denied = await web.post(
                    endpoint(config, raw["source_id"]),
                    headers={**wire_headers(credential), **headers},
                    json=request_payload(),
                )
                assert denied.status_code == 403
            oversized = await web.post(
                endpoint(config, raw["source_id"]),
                headers={**wire_headers(credential), "Content-Type": "application/json"},
                content=b" " * (4 * 1024 * 1024 + 1),
            )
            assert oversized.status_code == 413
            assert oversized.headers["cache-control"] == "no-store"
            assert not list(config.data_dir.rglob("*outbox*"))

    asyncio.run(scenario())
