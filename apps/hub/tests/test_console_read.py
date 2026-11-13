"""FR-01/03/10/12: administrator console reads the real scoped Hub authority."""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

from cloud_memory_hub.remote_access import AccessError, NamedResource
from one_memory.hy_memory_bridge import HySdkMemoryRecord
from test_chat_graph import produce
from test_governance_execution import EXECUTE, preview
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_ingest import provision
from test_remote_read import grant_reads

BOOTSTRAP = "/hub/v1/console/bootstrap"
SUMMARY = "/hub/v1/console/summary"
READ = "/hub/v1/console/read/"


async def read(web, admin, scope, operation="memory-list", **fields):
    return await web.post(READ + operation, headers=admin, json={"scope_id": scope, **fields})


def ids(response):
    assert response.status_code == 200, response.text
    return {item["record_id"] for item in response.json()["items"]}


def test_real_two_project_reads_match_device_content_and_show_registered_sources(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            first = await produce(app, web, config, monkeypatch)
            second = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scopes = {group[4]["scope_id"] for group in (first, second)}
            pages = []
            for offset in (0, 1):
                reply = await web.get(
                    BOOTSTRAP, headers=admin, params={"limit": 1, "offset": offset}
                )
                assert reply.status_code == 200
                value = reply.json()
                assert value["product"] == "Cloud Memory Hub" and value["mode"] == "hub"
                assert value["instance_id"] == first[4]["instance_id"]
                assert value["global_scope"]["scope_id"] == "global"
                assert value["build_revision"] is None
                assert value["features"] == ["scoped_console_reads", "scoped_console_ask"]
                assert value["next_offset"] == (1 if offset == 0 else None)
                pages.extend(value["projects"])
            assert {item["scope_id"] for item in pages} == scopes
            assert config.admin_token not in reply.text

            for jobs, _, results, device, event, _ in (first, second):
                scope = event["scope_id"]
                operations = (
                    ("memory-list", "memories/list", {}),
                    ("memory-search", "memories/search", {"query": "Synthetic fact"}),
                    ("memory-get", "memories/get", {"record_id": results[-1]["memory_ids"][0]}),
                    ("history-list", "history/list", {}),
                    ("history-get", "history/get", {"record_id": "cmhe:" + jobs[-1]}),
                )
                for operation, device_path, fields in operations:
                    console_reply = await read(web, admin, scope, operation, **fields)
                    device_reply = await web.post(
                        "/hub/v1/" + device_path,
                        headers=device,
                        json={"scope_id": scope, **fields},
                    )
                    assert console_reply.status_code == device_reply.status_code == 200
                    value = console_reply.json()
                    assert value["items"] == device_reply.json()["items"]
                    assert value["returned_count"] == len(value["items"])
                    assert value["authority"] == "administrator"
                    assert value["content_trust"] == "untrusted_history"
                    assert "device_id" not in value and "device_revision" not in value
                    assert "total" not in value
                    assert console_reply.headers["cache-control"] == "no-store"
                assert ids(await read(web, admin, scope)) == {
                    result["memory_ids"][0] for result in results
                }
                assert ids(await read(web, admin, scope, "history-list")) == {
                    "cmhe:" + job for job in jobs
                }
                summary = await web.post(SUMMARY, headers=admin, json={"scope_id": scope})
                assert summary.status_code == 200
                state = summary.json()
                assert state["scope"]["scope_id"] == scope
                assert state["sources_total"] == 1 and state["next_offset"] is None
                assert [row["source_id"] for row in state["sources"]] == [event["source_id"]]
                assert state["sources"][0]["device_display_name"] == "Synthetic connector"
                assert state["sources"][0]["status"] == "active"
                assert state["sources"][0]["device_status"] == "active"
                assert state["visible_memory_total"] is state["visible_history_total"] is None
                assert state["count_state"] == "not_computed"
                # Last accepted upload is derived from receipts plus queue time;
                # a missing value must stay absent rather than become zero.
                accepted = state["sources"][0]["last_accepted_at"]
                assert accepted is None or (
                    isinstance(accepted, int)
                    and 0 < accepted <= int(time.time())
                )
                assert "credential" not in summary.text
            assert ids(await read(web, admin, "global")) == set()
            wrong_scope = await read(
                web, admin, second[4]["scope_id"], "history-get", record_id="cmhe:" + first[0][-1]
            )
            assert wrong_scope.status_code == 404
            assert wrong_scope.json() == {"detail": "record_not_found"}

    asyncio.run(scenario())


def test_console_requires_administrator_and_browser_origin_before_reading(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, device, _, event = await provision(web, config)
            for headers in ({}, device, {"X-Cmh-Admin": "true"}):
                for path in (SUMMARY, READ + "history-list"):
                    rejected = await web.post(
                        path, headers=headers, json={"scope_id": event["scope_id"]}
                    )
                    assert rejected.status_code == 403
                    assert rejected.json() == {"detail": "administrator_required"}
                assert (await web.get(BOOTSTRAP, headers=headers)).status_code == 403
            code, _ = app.sessions.issue_exchange()
            web.cookies.set("cmh_session", app.sessions.consume_exchange(code))
            assert (await web.get(BOOTSTRAP)).status_code == 200
            assert (await read(web, {}, event["scope_id"])).json() == {"detail": "origin_required"}
            assert (
                await read(web, {"Origin": config.public_origin}, event["scope_id"])
            ).status_code == 200
            assert (
                await read(web, {"Origin": "https://foreign.invalid"}, event["scope_id"])
            ).status_code == 403
            assert (await read(web, admin, event["scope_id"])).status_code == 200

    asyncio.run(scenario())


def test_console_rejects_unknown_scope_bad_requests_and_preserves_empty_global(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, _, _, event = await provision(web, config)
            scope = event["scope_id"]
            for path, body, code in (
                (SUMMARY, {"scope_id": str(uuid4())}, "console_scope_not_found"),
                (READ + "memory-list", {"scope_id": str(uuid4())}, "console_scope_not_found"),
                (READ + "memory-list", {}, "invalid_request"),
                (READ + "memory-list", {"scope_id": "local"}, "invalid_request"),
                (READ + "memory-list", {"scope_id": scope, "user_id": "local"}, "invalid_request"),
                (READ + "history-list", {"scope_id": scope, "limit": 11}, "invalid_request"),
                (READ + "memory-search", {"scope_id": scope, "query": " "}, "invalid_request"),
                (
                    READ + "history-get",
                    {"scope_id": scope, "record_id": "../../x"},
                    "invalid_request",
                ),
            ):
                reply = await web.post(path, headers=admin, json=body)
                assert reply.status_code == (404 if code.endswith("not_found") else 422)
                assert reply.json() == {"detail": code}
            for query in ("limit=51", "offset=-1", "extra=1", "limit=invalid"):
                assert (await web.get(BOOTSTRAP + "?" + query, headers=admin)).json() == {
                    "detail": "invalid_request"
                }
            assert (await web.post(READ + "unknown", headers=admin, json={})).status_code == 404
            assert (await web.get(READ + "memory-list", headers=admin)).status_code == 405
            assert (await web.post(SUMMARY, headers=admin, content="{}")).json() == {
                "detail": "json_required"
            }
            oversized = await web.post(
                SUMMARY, headers={**admin, "Content-Type": "application/json"}, content="x" * 65537
            )
            assert oversized.status_code == 413
            assert (await web.post(SUMMARY, headers=admin, json={"scope_id": "global"})).json()[
                "sources_total"
            ] == 0

    asyncio.run(scenario())


def test_real_forget_is_shared_by_console_and_device_without_hiding_explicit_history(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, device, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope, original = event["scope_id"], "cmhe:" + jobs[-1]
            before = await read(web, admin, scope, "history-get", record_id=original)
            view = await preview(web, admin, scope, [original])
            executed = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "forget",
                    "record_ids": [original],
                    "expected_view": view["view_digest"],
                    "operation_id": str(uuid4()),
                },
            )
            assert executed.status_code == 200 and executed.json()["state"] == "completed"
            listed = await read(web, admin, scope, "history-list")
            device_list = await web.post(
                "/hub/v1/history/list", headers=device, json={"scope_id": scope}
            )
            assert original not in ids(listed)
            assert listed.json()["items"] == device_list.json()["items"]
            assert (
                await read(web, admin, scope, "history-get", record_id=original)
            ).json() == before.json()
            assert results[-1]["memory_ids"][0] not in ids(
                await read(web, admin, scope, "memory-search", query="Synthetic fact")
            )
            # Controlled stale authority row models a purge in progress; no body may escape.
            app.core.state.cmh_reads.suppressed = lambda selected: {original: "purged"}
            assert original not in ids(await read(web, admin, scope, "history-list"))
            assert (
                await read(web, admin, scope, "history-get", record_id=original)
            ).status_code == 404

    asyncio.run(scenario())


def test_console_reuses_hydration_privacy_budget_and_safe_sdk_errors(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, _, _, event = await provision(web, config)
            scope = event["scope_id"]
            code, _ = app.sessions.issue_exchange()
            cookie = app.sessions.consume_exchange(code)
            web.cookies.set("cmh_session", cookie)
            current = HySdkMemoryRecord(
                memory_id=str(uuid4()),
                content="Synthetic " + config.admin_token + " " + cookie,
                user_id=f"cmh:{event['instance_id']}:{scope}",
                agent_id="synthetic",
                layer="l2_fact",
                status="active",
                is_latest=True,
            )
            stale = current.model_copy(update={"memory_id": str(uuid4()), "is_latest": False})
            foreign = current.model_copy(update={"memory_id": str(uuid4()), "user_id": "local"})
            records = {row.memory_id: row for row in (current, stale, foreign)}

            async def listing(**kwargs):
                return SimpleNamespace(memories=tuple(records.values()), total=100)

            async def get(identity):
                return records[identity]

            reads = app.core.state.cmh_reads
            reads.bridge = lambda: SimpleNamespace(list_sdk_memories=listing, get_sdk_memory=get)
            reply = await read(web, admin, scope)
            assert ids(reply) == {current.memory_id}
            assert reply.json()["returned_count"] == 1 and reply.json()["next_offset"] == 10
            assert "total" not in reply.json()
            assert config.admin_token not in reply.text and cookie not in reply.text
            query = await read(web, admin, scope, "memory-search", query=config.admin_token)
            assert query.json() == {"detail": "query_requires_refiltering"}
            records.clear()
            records[config.admin_token] = current.model_copy(
                update={"memory_id": config.admin_token}
            )
            assert (await read(web, admin, scope)).json() == {
                "detail": "read_metadata_requires_review"
            }
            records.clear()
            for _ in range(10):
                item = current.model_copy(
                    update={"memory_id": str(uuid4()), "content": "合" * 4096}
                )
                records[item.memory_id] = item
            assert (await read(web, admin, scope)).json() == {"detail": "read_response_too_large"}

            async def broken(**kwargs):
                raise ValueError("private model configuration " + config.admin_token)

            reads.bridge = lambda: SimpleNamespace(list_sdk_memories=broken)
            failure = await read(web, admin, scope)
            assert failure.status_code == 503 and failure.json() == {"detail": "read_unavailable"}
            assert config.admin_token not in failure.text

    asyncio.run(scenario())


def test_console_and_device_share_bounded_read_slots(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, device, _, event = await provision(web, config)
            await grant_reads(web, admin, event)
            entered, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def listing(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    entered.set()
                await release.wait()
                return SimpleNamespace(memories=(), total=0)

            app.core.state.cmh_reads.bridge = lambda: SimpleNamespace(list_sdk_memories=listing)
            first = asyncio.create_task(read(web, admin, event["scope_id"]))
            second = asyncio.create_task(
                web.post(
                    "/hub/v1/memories/list", headers=device, json={"scope_id": event["scope_id"]}
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                busy = await read(web, admin, event["scope_id"])
                assert busy.status_code == 503 and busy.json() == {"detail": "read_busy"}
            finally:
                release.set()
                replies = await asyncio.gather(first, second)
            assert all(reply.status_code == 200 for reply in replies)
            assert (await read(web, admin, event["scope_id"])).status_code == 200

    asyncio.run(scenario())


def test_console_metadata_failures_are_not_empty_success_and_sources_paginate(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, _, _, event = await provision(web, config)
            scope = event["scope_id"]
            added = await web.post(
                "/hub/v1/sources",
                headers=admin,
                json={
                    "display_name": "Second source",
                    "adapter_id": "mcp",
                    "device_id": event["device_id"],
                    "scope_id": scope,
                },
            )
            assert added.status_code == 201
            source_ids = set()
            for offset in (0, 1):
                reply = await web.post(
                    SUMMARY, headers=admin, json={"scope_id": scope, "limit": 1, "offset": offset}
                )
                assert reply.status_code == 200
                assert reply.json()["sources_total"] == 2
                assert reply.json()["next_offset"] == (1 if offset == 0 else None)
                source_ids.update(row["source_id"] for row in reply.json()["sources"])
            assert source_ids == {event["source_id"], added.json()["source_id"]}
            app.governance.access.create_project(NamedResource(display_name=config.admin_token))
            hidden = await web.get(BOOTSTRAP, headers=admin)
            assert hidden.status_code == 503
            assert hidden.json() == {"detail": "console_metadata_requires_review"}
            assert config.admin_token not in hidden.text

            def unavailable(*args):
                raise AccessError("access_store_unavailable", 503)

            monkeypatch.setattr(app.core.state.cmh_console, "_summary", unavailable)
            failed = await web.post(SUMMARY, headers=admin, json={"scope_id": scope})
            assert failed.status_code == 503 and failed.json() == {
                "detail": "access_store_unavailable"
            }

    asyncio.run(scenario())
