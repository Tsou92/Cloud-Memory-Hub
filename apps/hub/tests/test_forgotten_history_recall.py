"""FR-03/05/10/11: forgotten originals stay explicit-only, never automatic context."""

import asyncio
import json
import socket
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.agent_hook import _recall
from cloud_memory_hub.transport import RemoteConnection
from httpx import ASGITransport
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HySdkRawRecord
from test_agent_access import access_for
from test_chat_graph import produce
from test_governance_execution import EXECUTE, preview, read
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_remote_read import grant_reads


async def history_page(web, headers, scope, *, limit=10, offset=0):
    reply = await web.post(
        "/hub/v1/history/list",
        headers=headers,
        json={"scope_id": scope, "limit": limit, "offset": offset},
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


def context_ids(context):
    return {
        json.loads(line)["memory_id"] for line in context.text.splitlines() if line.startswith("{")
    }


def test_real_forget_excludes_automatic_history_but_keeps_explicit_get(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("external_network_not_allowed")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope = event["scope_id"]
            original = "cmhe:" + jobs[-1]
            raw, retained_raw = str(uuid4()), str(uuid4())
            bridge = _sdk_library_bridge(app.core)
            for identity in (raw, retained_raw):
                await bridge.import_sdk_raw_exact(
                    content="Synthetic explicitly imported original " + identity,
                    raw_id=identity,
                    user_id=f"cmh:{event['instance_id']}:{scope}",
                    agent_id="synthetic",
                    session_id="synthetic",
                    source="synthetic",
                    memory_at=None,
                    metadata={},
                    request_id=str(uuid4()),
                )
            targets = {original, raw}
            unchanged = {"cmhe:" + jobs[0], "cmhe:" + jobs[1], retained_raw}
            originals = {
                identity: (await read(web, headers, scope, "history", identity)).json()
                for identity in targets
            }
            before = await history_page(web, headers, scope)
            before_ids = {item["record_id"] for item in before["items"]}
            assert targets | unchanged <= before_ids
            connection = RemoteConnection(
                origin=config.public_origin,
                instance_id=event["instance_id"],
                device_id=event["device_id"],
                credential=headers["Authorization"].removeprefix("Bearer "),
            )
            service = access_for(
                tmp_path / "agent", connection, event, transport=ASGITransport(app)
            )
            assert original in context_ids(await _recall(service, "Synthetic fact"))

            # Another authorized project's original must be unaffected by this forget.
            _, other_headers, _, other_event = await provision(web, config)
            other_original = "cmhe:" + await accepted(web, other_headers, other_event)
            await grant_reads(web, admin, other_event)
            other_before = await history_page(web, other_headers, other_event["scope_id"])
            assert [item["record_id"] for item in other_before["items"]] == [other_original]

            view = await preview(web, admin, scope, [original, raw])
            selected = {item["record_id"] for item in view["items"]}
            assert targets <= selected and not unchanged & selected
            result = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "forget",
                    "record_ids": [original, raw],
                    "expected_view": view["view_digest"],
                    "operation_id": str(uuid4()),
                },
            )
            assert result.status_code == 200, result.text
            assert result.json()["state"] == "completed"

            after = await history_page(web, headers, scope)
            after_ids = {item["record_id"] for item in after["items"]}
            assert after_ids == before_ids - targets
            for identity in targets:
                explicit = await read(web, headers, scope, "history", identity)
                assert explicit.status_code == 200
                assert explicit.json() == originals[identity]
            recalled = context_ids(await _recall(service, "Synthetic fact"))
            assert not recalled & selected
            assert recalled & unchanged
            assert await history_page(web, other_headers, other_event["scope_id"]) == other_before
            assert (
                await read(web, other_headers, other_event["scope_id"], "history", original)
            ).status_code == 404

            # Pagination still counts underlying slots, including filtered empty pages.
            offset, paged, empty_page = 0, set(), False
            while offset is not None:
                page = await history_page(web, headers, scope, limit=1, offset=offset)
                paged.update(item["record_id"] for item in page["items"])
                assert page["next_offset"] in (None, offset + 1)
                empty_page |= not page["items"] and page["next_offset"] is not None
                offset = page["next_offset"]
            assert paged == after_ids and empty_page

    asyncio.run(scenario())


@pytest.mark.parametrize("suppression", ["raw", "purged"])
def test_history_list_never_exposes_suppressed_raw_and_get_preserves_purge(tmp_path, suppression):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            await grant_reads(web, admin, event)
            scope = event["scope_id"]
            record = HySdkRawRecord(
                raw_id=str(uuid4()),
                content="Synthetic retained raw authority row.",
                user_id=f"cmh:{event['instance_id']}:{scope}",
                agent_id="synthetic",
                session_id="synthetic",
                source="synthetic",
            )

            async def listing(**kwargs):
                return [record], 1

            async def get(identity):
                return record if identity == record.raw_id else None

            reads = app.core.state.cmh_reads
            reads.bridge = lambda: SimpleNamespace(list_sdk_raw=listing, get_sdk_raw=get)
            reads.suppressed = lambda selected_scope: {record.raw_id: suppression}
            assert not (await history_page(web, headers, scope))["items"]
            explicit = await read(web, headers, scope, "history", record.raw_id)
            assert explicit.status_code == (404 if suppression == "purged" else 200)
            if suppression == "purged":
                assert explicit.json() == {"detail": "record_not_found"}
            else:
                assert explicit.json()["items"][0]["content"] == record.content

    asyncio.run(scenario())
