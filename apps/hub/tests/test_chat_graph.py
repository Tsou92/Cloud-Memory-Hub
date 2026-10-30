import asyncio
import copy
import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.chat_evidence import capture_chat, stored_chat
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import plan_relations
from cloud_memory_hub.reconcile_evidence import digest, encode
from cloud_memory_hub.remote_access import AccessError
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture, read_memory
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request


async def produce(app, web, config, monkeypatch, *, cross_source=False):
    admin, headers, _, event = await provision(web, config)
    queue = app.core.state.memory_write_queue
    sdk = await _sdk_library_bridge(app.core)._get_client()
    jobs, requests, results, calls = [], [], [], []

    async def agent(self, **kwargs):
        calls.append(kwargs["history_context"])
        return AgentResult(
            success=True,
            extracted_info={"memory": [{"content": f"Synthetic fact {len(calls)}"}]},
        )

    monkeypatch.setattr(MemAgent, "process_add", agent)
    # Exercise the native direct-store option; never call a paid reconciliation model.
    monkeypatch.setattr("hy_memory.pipelines.writer._RECONCILE_ENABLED", False)
    for index in range(3):
        if index == 1 and cross_source:
            reply = await web.post(
                "/hub/v1/sources",
                headers=admin,
                json={
                    "display_name": "Second agent",
                    "adapter_id": "future-agent.custom",
                    "device_id": event["device_id"],
                    "scope_id": event["scope_id"],
                },
            )
            assert reply.status_code == 201, reply.text
            event = {**event, "source_id": reply.json()["source_id"]}
        event = {
            **event,
            **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
        }
        job = await accepted(web, headers, event)
        request = sdk_request(queue.payload_for(job))
        result = await _joined_thread(
            sdk.add,
            [turn.model_dump() for turn in request.turns],
            user_id=request.user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            source=request.source_id,
            request_id=request.request_id,
            scene="chat",
        )
        assert result["success"] and len(result["memory_ids"]) == 1
        await app.core.state.cmh_lineage.observe(job, queue.payload_for(job))
        queue.settle(job, state=COMPLETED)
        jobs.append(job)
        requests.append(request)
        results.append(result)
    await grant_reads(web, admin, event)
    return jobs, requests, results, headers, event, calls


def test_cross_session_raw_evidence_and_direction_survive_restart(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, calls = await produce(
                app, web, config, monkeypatch
            )
            assert requests[0].source_id == requests[1].source_id
            assert len({item.session_id for item in requests}) == 3
            assert calls[1] == results[1]["chat_history"]["rendered"]
            with app.core.state.cmh_ingest._database() as db:
                assert (
                    stored_chat(db, jobs[1], requests[1])["capture"] == results[1]["chat_history"]
                )
            queue = app.core.state.memory_write_queue
            assert queue.clear_terminal_payloads() == 3
        async with fixture(config) as (app, web):
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            first, second, third = ("cmhe:" + job for job in jobs)
            assert nodes["cmhj:" + jobs[1]].parents == {first, second}
            assert nodes["cmhj:" + jobs[2]].parents == {first, second, third}
            assert nodes[results[1]["memory_id"]].parents == {second}
            selected = plan_relations(nodes, (first,))["record_ids"]
            assert "cmhj:" + jobs[1] in selected and results[1]["memory_ids"][0] in selected
            assert second not in selected and third not in selected
            assert results[1]["memory_id"] not in selected
            preview = await web.post(
                "/hub/v1/governance/preview",
                headers={"Authorization": "Bearer " + config.admin_token},
                json={
                    "scope_id": event["scope_id"],
                    "record_ids": [first],
                    "action": "purge",
                    "limit": 50,
                },
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["execution_available"] is False
            assert {item["record_id"] for item in preview.json()["items"]} == set(selected)
            reply = await read_memory(web, headers, event, results[2]["memory_ids"][0])
            assert reply.status_code == 200, reply.text
            assert set(reply.json()["items"][0]["source_raw_ids"]) >= {first, second, third}

    asyncio.run(scenario())


def test_native_source_partition_and_project_grants_are_preserved(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch, cross_source=True
            )
            assert requests[0].agent_id != requests[1].agent_id
            assert results[1]["chat_history"]["records"] == []
            assert [row["node_id"] for row in results[2]["chat_history"]["records"]] == [
                results[1]["memory_id"]
            ]
            reply = await read_memory(web, headers, event, results[2]["memory_ids"][0])
            assert reply.status_code == 200, reply.text
            references = reply.json()["items"][0]["source_raw_ids"]
            assert "cmhe:" + jobs[1] in references and "cmhe:" + jobs[0] not in references
            _, foreign, _, other = await provision(web, config)
            reply = await read_memory(web, foreign, event, results[2]["memory_ids"][0])
            assert reply.status_code == 403 and "items" not in reply.json()
            reply = await read_memory(web, headers, other, results[2]["memory_ids"][0])
            assert reply.status_code == 403 and "items" not in reply.json()

    asyncio.run(scenario())


def test_legacy_absence_is_unobserved_and_missing_archive_is_backfilled(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            legacy = dict(results[1])
            legacy.pop("chat_history")
            value = capture_chat(legacy, requests[1])
            assert value == {"scene": "chat", "capture": None}
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                encoded = encode(value)
                db.execute(
                    "UPDATE cmh_event_chat_inputs SET chat_json=?,chat_digest=? WHERE receipt_id=?",
                    (encoded, digest(encoded), jobs[1]),
                )
                db.execute("DELETE FROM cmh_event_chat_inputs WHERE receipt_id=?", (jobs[2],))
            sdk = await _sdk_library_bridge(app.core)._get_client()
            monkeypatch.setattr(sdk, "add", lambda *a, **k: pytest.fail("backfill wrote again"))
            assert await app.core.state.cmh_lineage.reconcile_completed() == 1
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            assert "cmhj:" + jobs[1] not in nodes
            assert "cmhj:" + jobs[2] in nodes
            reply = await read_memory(web, headers, event, results[1]["memory_ids"][0])
            assert reply.status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage",
    [
        "content",
        "source",
        "session",
        "user",
        "request",
        "missing",
        "ambiguous",
        "digest",
        "self",
        "unavailable",
        "bytes",
    ],
)
def test_unproven_history_fails_closed_on_reads_and_inventory(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            value = capture_chat(results[1], requests[1])
            item = value["capture"]["records"][0]
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                if damage in {"content", "source", "session", "user", "request"}:
                    key = {"session": "session_id", "user": "user_id", "request": "request_id"}.get(
                        damage, damage
                    )
                    item[key] = "synthetic mismatch"
                elif damage == "missing":
                    item["node_id"] = str(uuid4())
                elif damage == "self":
                    item["node_id"] = results[1]["memory_id"]
                elif damage == "unavailable":
                    value["capture"] = {
                        "schema_version": 1,
                        "status": "unavailable",
                        "records": [],
                        "rendered": "",
                    }
                elif damage == "bytes":
                    item["content"] = "界" * 1024 * 1024
                elif damage == "ambiguous":
                    db.execute(
                        "INSERT INTO cmh_event_output_links VALUES (?,'raw',?)",
                        (jobs[2], item["node_id"]),
                    )
                encoded = encode(value)
                db.execute(
                    "UPDATE cmh_event_chat_inputs SET chat_json=?,chat_digest=? WHERE receipt_id=?",
                    (encoded, "invalid" if damage == "digest" else digest(encoded), jobs[1]),
                )
            reply = await read_memory(web, headers, event, results[1]["memory_ids"][0])
            assert reply.status_code == 503, reply.text
            assert "items" not in reply.json()
            with pytest.raises((GovernanceError, ValueError)):
                await observe_inventory(
                    _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
                )

    asyncio.run(scenario())


def test_chat_archive_rolls_back_and_completed_recovery_never_rewrites(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, _, _, _ = await produce(app, web, config, monkeypatch)
            lineage = app.core.state.cmh_lineage
            tables = (
                "cmh_event_chat_inputs",
                "cmh_event_inputs",
                "cmh_event_contexts",
                "cmh_event_output_links",
                "cmh_event_outcomes",
            )
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                for table in tables:
                    db.execute(f"DELETE FROM {table} WHERE receipt_id=?", (jobs[-1],))
                db.execute(
                    "CREATE TRIGGER reject_chat BEFORE INSERT ON cmh_event_chat_inputs "
                    "BEGIN SELECT RAISE(ABORT,'synthetic archive failure'); END"
                )
            with pytest.raises(AccessError, match="access_store_unavailable"):
                await lineage.observe(jobs[-1], None)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                for table in tables:
                    assert (
                        db.execute(
                            f"SELECT count(*) FROM {table} WHERE receipt_id=?", (jobs[-1],)
                        ).fetchone()[0]
                        == 0
                    )
                db.execute("DROP TRIGGER reject_chat")
            sdk = await _sdk_library_bridge(app.core)._get_client()
            monkeypatch.setattr(sdk, "add", lambda *a, **k: pytest.fail("recovery wrote again"))
            assert await lineage.reconcile_completed() == 1
            assert await lineage.reconcile_completed() == 0
            changed = copy.deepcopy(results[-1])
            changed["chat_history"]["rendered"] += " changed"
            from cloud_memory_hub.chat_evidence import commit_chat

            with app.core.state.cmh_ingest._database(writing=True) as db:
                assert (
                    stored_chat(db, jobs[-1], requests[-1])["capture"]
                    == results[-1]["chat_history"]
                )
                with pytest.raises(ValueError, match="lineage_chat_changed"):
                    commit_chat(db, jobs[-1], capture_chat(changed, requests[-1]), requests[-1])

    asyncio.run(scenario())
