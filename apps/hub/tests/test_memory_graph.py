import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import plan_relations
from cloud_memory_hub.memory_evidence import capture_memory, stored_memory
from cloud_memory_hub.reconcile_evidence import digest, encode
from cloud_memory_hub.remote_access import AccessError
from hy_memory.agent.llm_provider import LLMResponse
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.agent.reconciler import MemoryReconciler
from hy_memory.coding.record_origin import record_digest
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture, read_memory
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request


async def produce(app, web, config, monkeypatch, *, fallback=False):
    admin, headers, _, event = await provision(web, config)
    queue = app.core.state.memory_write_queue
    sdk = await _sdk_library_bridge(app.core)._get_client()
    jobs, requests, results = [], [], []

    async def extract(self, **kwargs):
        return AgentResult(success=True, extracted_info={"memory": [{"content": "Synthetic fact"}]})

    async def complete(**kwargs):
        if fallback:
            raise RuntimeError("synthetic model failure")
        return LLMResponse(
            content=json.dumps(
                [
                    {
                        "op": "UPDATE",
                        "memory_id": results[0]["memory_ids"][0],
                        "content": "Synthetic fact " + str(len(results)),
                    }
                ]
            ),
            tokens_used=1,
            prompt_tokens=1,
            completion_tokens=0,
        )

    monkeypatch.setattr(MemAgent, "process_add", extract)
    monkeypatch.setattr(
        MemoryReconciler, "_get_llm", lambda self: SimpleNamespace(complete=complete)
    )
    for _ in range(3):
        current = {
            **event,
            **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
        }
        job = await accepted(web, headers, current)
        frozen = queue.payload_for(job)
        request = sdk_request(frozen)
        result = await _joined_thread(
            sdk.add,
            [turn.model_dump() for turn in request.turns],
            scene="chat",
            user_id=request.user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            source=request.source_id,
            request_id=request.request_id,
        )
        assert result["success"] and len(result["memory_ids"]) == 1
        await app.core.state.cmh_lineage.observe(job, frozen)
        queue.settle(job, state=COMPLETED)
        jobs.append(job)
        requests.append(request)
        results.append(result)
    await grant_reads(web, admin, event)
    return jobs, requests, results, headers, event


def test_actual_sdk_update_versions_survive_reaping_restart_and_scoped_reads(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event = await produce(app, web, config, monkeypatch)
            assert len({request.session_id for request in requests}) == 3
            assert len({result["memory_ids"][0] for result in results}) == 1
            with app.core.state.cmh_ingest._database() as db:
                value = stored_memory(db, jobs[2], requests[2])
                record = value["capture"]["stages"][0]["inputs"][0]["record"]
                assert record["origin_request_id"] == requests[1].request_id
                assert record["session_id"] == requests[0].session_id
                assert record["content"] == "Synthetic fact 1"
            assert app.core.state.memory_write_queue.clear_terminal_payloads() == 3
        async with fixture(config) as (app, web):
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            first, second, third = ("cmhm:" + job for job in jobs)
            target = results[-1]["memory_ids"][0]
            assert nodes[first].parents == {"cmhe:" + jobs[0]}
            assert nodes[second].parents == {
                "cmhe:" + jobs[1],
                "cmhe:" + jobs[0],
                "cmhj:" + jobs[0],
                "cmhj:" + jobs[1],
            }
            assert nodes[third].parents == {
                "cmhe:" + jobs[2],
                "cmhe:" + jobs[1],
                "cmhj:" + jobs[1],
                "cmhj:" + jobs[2],
                second,
            }
            assert target not in nodes[second].parents
            assert first not in nodes[target].parents  # skipped model stage
            assert {second, third} <= nodes[target].parents
            selected = plan_relations(nodes, (second,))["record_ids"]
            assert {third, target} <= set(selected)
            assert "cmhe:" + jobs[2] not in selected
            # The prompt snapshot itself contains text extracted using this history.
            # Selecting that history must include its captured downstream prompt too.
            selected_history = plan_relations(nodes, ("cmhj:" + jobs[1],))["record_ids"]
            assert second in selected_history
            reply = await read_memory(web, headers, event, target)
            assert reply.status_code == 200, reply.text
            assert {"cmhe:" + job for job in jobs} <= set(
                reply.json()["items"][0]["source_raw_ids"]
            )
            denied = await read_memory(web, headers, {**event, "scope_id": "global"}, target)
            assert denied.status_code in (403, 404)

    asyncio.run(scenario())


def test_legacy_capture_absence_and_missing_archive_recovery(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event = await produce(app, web, config, monkeypatch)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute("DELETE FROM cmh_event_memory_inputs WHERE receipt_id=?", (jobs[-1],))
            sdk = await _sdk_library_bridge(app.core)._get_client()
            monkeypatch.setattr(sdk, "add", lambda *args, **kwargs: pytest.fail("backfill wrote"))
            assert await app.core.state.cmh_lineage.reconcile_completed() == 1
            with app.core.state.cmh_ingest._database() as db:
                assert (
                    stored_memory(db, jobs[-1], requests[-1])["capture"]
                    == results[-1]["chat_reconcile_inputs"]
                )
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                for result, request, job in zip(results, requests, jobs, strict=True):
                    old = dict(result)
                    old.pop("chat_reconcile_inputs")
                    value = capture_memory(old, request)
                    assert value == {"scene": "chat", "capture": None}
                    encoded = encode(value)
                    db.execute(
                        "UPDATE cmh_event_memory_inputs SET memory_json=?,memory_digest=? "
                        "WHERE receipt_id=?",
                        (encoded, digest(encoded), job),
                    )
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            assert not any(identity.startswith("cmhm:") for identity in nodes)
            reply = await read_memory(web, headers, event, results[-1]["memory_ids"][0])
            assert reply.status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage",
    [
        "content",
        "origin",
        "agent",
        "source",
        "missing",
        "self",
        "cycle",
        "output",
        "digest",
        "unavailable",
        "bytes",
        "duplicate",
        "cross_scope",
    ],
)
def test_invalid_version_sources_fail_closed(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event = await produce(app, web, config, monkeypatch)
            value = capture_memory(results[1], requests[1])
            item = value["capture"]["stages"][0]["inputs"][0]
            record = item["record"]
            target = jobs[1]
            if damage in {"content", "agent", "source"}:
                record[{"agent": "agent_id"}.get(damage, damage)] = "synthetic mismatch"
                if damage != "content":
                    record["origin_digest"] = record_digest("memory", record)
            elif damage == "origin":
                record["origin_request_id"] = record["origin_digest"] = None
            elif damage in {"missing", "self", "cycle", "cross_scope"}:
                producer = "unknown-request"
                if damage in {"self", "cycle"}:
                    producer = requests[1].request_id
                    if damage == "cycle":
                        target = jobs[0]
                if damage == "cross_scope":
                    _, other_headers, _, other = await provision(web, config)
                    other_job = await accepted(web, other_headers, other)
                    producer = sdk_request(
                        app.core.state.memory_write_queue.payload_for(other_job)
                    ).request_id
                record["origin_request_id"] = producer
                record["origin_digest"] = record_digest("memory", record)
            elif damage == "output":
                item["id"] = record["node_id"] = str(uuid4())
                record["origin_digest"] = record_digest("memory", record)
            elif damage == "unavailable":
                value["capture"] = {"schema_version": 1, "status": "unavailable", "stages": []}
            elif damage == "bytes":
                value["capture"]["stages"][0]["rendered"] = "界" * 1024 * 1024
            elif damage == "duplicate":
                value["capture"]["stages"][0]["inputs"].append(copy.deepcopy(item))
            encoded = encode(value)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "UPDATE cmh_event_memory_inputs SET memory_json=?,memory_digest=? "
                    "WHERE receipt_id=?",
                    (encoded, "invalid" if damage == "digest" else digest(encoded), target),
                )
            reply = await read_memory(web, headers, event, results[-1]["memory_ids"][0])
            assert reply.status_code == 503, reply.text
            assert "items" not in reply.json()
            with pytest.raises((GovernanceError, ValueError)):
                await observe_inventory(
                    _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
                )

    asyncio.run(scenario())


def test_fallback_is_archived_without_semantic_output_edge(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, *_ = await produce(
                app, web, config, monkeypatch, fallback=True
            )
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            for index in (1, 2):
                assert results[index]["chat_reconcile_inputs"]["stages"][0]["status"] == "fallback"
                assert "cmhm:" + jobs[index] in nodes
                assert "cmhm:" + jobs[index] not in nodes[results[index]["memory_ids"][0]].parents

    asyncio.run(scenario())


def test_memory_archive_bytes_are_counted_before_materializing_originals(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, requests, _, _, event = await produce(app, web, config, monkeypatch)
            with app.core.state.cmh_ingest._database() as db:
                old_size, added = db.execute(
                    "SELECT sum(length(CAST(payload_json AS BLOB)) + "
                    "length(CAST(context_json AS BLOB)) + length(CAST(inputs_json AS BLOB)) + "
                    "length(CAST(chat_json AS BLOB))), sum(length(CAST(memory_json AS BLOB))) "
                    "FROM cmh_event_receipts JOIN cmh_event_contexts USING(receipt_id) "
                    "JOIN cmh_event_inputs USING(receipt_id) "
                    "JOIN cmh_event_chat_inputs USING(receipt_id) "
                    "JOIN cmh_event_memory_inputs USING(receipt_id) WHERE scope_id=?",
                    (event["scope_id"],),
                ).fetchone()
            assert old_size > 0 and added > 0
            monkeypatch.setattr("cloud_memory_hub.governance_inventory.MAX_CONTENT_BYTES", old_size)
            monkeypatch.setattr(
                "cloud_memory_hub.output_lineage.validate_original",
                lambda *args: pytest.fail("oversized archive was materialized"),
            )
            with pytest.raises(GovernanceError, match="governance_scan_limit"):
                app.core.state.cmh_lineage.inventory(requests[0].user_id, {})

    asyncio.run(scenario())


def test_archive_failure_is_atomic_and_completed_recovery_never_writes(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, *_ = await produce(app, web, config, monkeypatch)
            tables = (
                "cmh_event_memory_inputs",
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
                    "CREATE TRIGGER reject_memory BEFORE INSERT ON cmh_event_memory_inputs "
                    "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
                )
            lineage = app.core.state.cmh_lineage
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
                db.execute("DROP TRIGGER reject_memory")
            sdk = await _sdk_library_bridge(app.core)._get_client()
            monkeypatch.setattr(sdk, "add", lambda *args, **kwargs: pytest.fail("recovery wrote"))
            assert await lineage.reconcile_completed() == 1
            assert await lineage.reconcile_completed() == 0
            with app.core.state.cmh_ingest._database() as db:
                assert (
                    stored_memory(db, jobs[-1], requests[-1])["capture"]
                    == results[-1]["chat_reconcile_inputs"]
                )

    asyncio.run(scenario())
