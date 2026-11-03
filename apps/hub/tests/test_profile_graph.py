import asyncio
import copy
import json
import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import plan_relations
from cloud_memory_hub.profile_evidence import read_profile, stored_profile
from cloud_memory_hub.reconcile_evidence import digest, encode
from cloud_memory_hub.remote_access import AccessError
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.coding.record_origin import record_digest
from hy_memory.pipelines.writer import MemoryWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture, read_memory
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request


async def produce(app, web, config, monkeypatch, *, observe=True):
    admin, headers, _, event = await provision(web, config)
    queue = app.core.state.memory_write_queue
    sdk = await _sdk_library_bridge(app.core)._get_client()
    jobs, requests, results = [], [], []

    async def extract(self, **kwargs):
        return AgentResult(
            success=True,
            extracted_info={
                "basic_info": {"name": "Synthetic", "age": 30 if not results else 31},
                "memory": [],
            },
        )

    monkeypatch.setattr(MemAgent, "process_add", extract)
    for index in range(3):
        source = await web.post(
            "/hub/v1/sources",
            headers=admin,
            json={
                "display_name": f"Synthetic agent {index}",
                "adapter_id": f"synthetic.agent{index}",
                "device_id": event["device_id"],
                "scope_id": event["scope_id"],
            },
        )
        assert source.status_code == 201
        current = {
            **event,
            "source_id": source.json()["source_id"],
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
        assert result["success"] and not result.get("memory_ids")
        if observe:
            await app.core.state.cmh_lineage.observe(job, frozen)
            queue.settle(job, state=COMPLETED)
        jobs.append(job)
        requests.append(request)
        results.append(result)
    await grant_reads(web, admin, event)
    return jobs, requests, results, headers, event


def test_profile_only_writes_cross_agent_ancestry_restart_and_scope(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event = await produce(app, web, config, monkeypatch)
            assert len({request.agent_id for request in requests}) == 3
            assert len({request.user_id for request in requests}) == 1
            with app.core.state.cmh_ingest._database() as db:
                assert (
                    db.execute("SELECT count(*) FROM cmh_event_profile_outputs").fetchone()[0] == 2
                )
                assert db.execute("SELECT count(*) FROM cmh_event_output_links").fetchone()[0] == 3
                assert stored_profile(db, jobs[2], requests[2])["capture"]["output_ids"] == []
            assert app.core.state.memory_write_queue.clear_terminal_payloads() == 3
        async with fixture(config) as (app, web):
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            stages = ["cmhb:" + job for job in jobs]
            first = results[0]["basic_profile_inputs"]["output_ids"][0]
            second = results[1]["basic_profile_inputs"]["output_ids"][0]
            assert nodes[stages[0]].parents == {"cmhe:" + jobs[0], "cmhj:" + jobs[0]}
            assert nodes[stages[1]].parents == {
                "cmhe:" + jobs[1],
                "cmhj:" + jobs[1],
                "cmhe:" + jobs[0],
                stages[0],
            }
            assert {stages[0], stages[1]} <= nodes[stages[2]].parents
            assert nodes[first].parents == {stages[0]}
            assert nodes[second].parents == {stages[1]}
            assert not any(parent.startswith("cmhm:") for parent in nodes[second].parents)
            selected = set(plan_relations(nodes, ("cmhe:" + jobs[0],))["record_ids"])
            assert {first, second, *stages} <= selected
            assert "cmhe:" + jobs[1] not in selected
            reply = await read_memory(web, headers, event, second)
            assert reply.status_code == 200, reply.text
            assert {"cmhe:" + jobs[0], "cmhe:" + jobs[1]} <= set(
                reply.json()["items"][0]["source_raw_ids"]
            )
            denied = await read_memory(web, headers, {**event, "scope_id": "global"}, second)
            assert denied.status_code in (403, 404)

    asyncio.run(scenario())


def test_backfill_preserves_old_output_digest_and_does_not_rewrite(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event = await produce(app, web, config, monkeypatch)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                before = db.execute(
                    "SELECT * FROM cmh_event_outcomes ORDER BY receipt_id"
                ).fetchall()
                db.execute("DELETE FROM cmh_event_profile_outputs")
                db.execute("DELETE FROM cmh_event_profile_inputs")
            assert app.core.state.memory_write_queue.clear_terminal_payloads() == 3

            async def no_write(*args, **kwargs):
                pytest.fail("backfill invoked writer")

            monkeypatch.setattr(MemoryWriter, "write", no_write)
            assert await app.core.state.cmh_lineage.reconcile_completed() == 3
            with app.core.state.cmh_ingest._database() as db:
                after = [
                    tuple(row)
                    for row in db.execute("SELECT * FROM cmh_event_outcomes ORDER BY receipt_id")
                ]
                assert before == after
                for job, request, result in zip(jobs, requests, results, strict=True):
                    assert (
                        stored_profile(db, job, request)["capture"]
                        == result["basic_profile_inputs"]
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage", ["origin", "content", "scope", "self", "output", "index", "unavailable", "live"]
)
def test_profile_proof_damage_fails_closed(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event = await produce(app, web, config, monkeypatch)
            lineage = app.core.state.cmh_lineage
            with lineage.ingest._database() as db:
                value = copy.deepcopy(stored_profile(db, jobs[1], requests[1]))
            capture = value["capture"]
            item = capture["stages"][0]["inputs"][0]
            if damage in {"origin", "self"}:
                item["record"]["origin_request_id"] = (
                    "unknown" if damage == "origin" else requests[1].request_id
                )
                item["record"]["origin_digest"] = record_digest("profile", item["record"])
            elif damage == "content":
                item["record"]["custom"]["basic_info_kv"]["age"] = 99
            elif damage == "scope":
                item["record"]["user_id"] = "other"
                item["record"]["origin_digest"] = record_digest("profile", item["record"])
            elif damage == "output":
                item["id"] = item["record"]["node_id"] = "not-the-producers-output"
                item["record"]["origin_digest"] = record_digest("profile", item["record"])

                rule = json.loads(capture["stages"][0]["rendered"])
                rule["head_ids"] = [item["id"]]
                capture["attempted_supersede_ids"] = capture["superseded_ids"] = [item["id"]]
                capture["stages"][0]["rendered"] = json.dumps(rule)
            elif damage == "unavailable":
                capture.update(status="unavailable", stages=[])
            elif damage == "live":
                sdk = await _sdk_library_bridge(app.core)._get_client()
                identity = results[1]["basic_profile_inputs"]["output_ids"][0]
                assert await _joined_thread(
                    lambda: sdk._loop_thread.run(
                        sdk._vector_store.update_payload(
                            identity, {"custom": {"basic_info_kv": {"age": 99}}}
                        )
                    )
                )
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                if damage == "index":
                    db.execute(
                        "DELETE FROM cmh_event_profile_outputs WHERE receipt_id=?", (jobs[0],)
                    )
                elif damage != "live":
                    encoded = encode(value)
                    db.execute(
                        "UPDATE cmh_event_profile_inputs SET profile_json=?,profile_digest=? "
                        "WHERE receipt_id=?",
                        (encoded, digest(encoded), jobs[1]),
                    )
            with pytest.raises(GovernanceError):
                await observe_inventory(_sdk_library_bridge(app.core), requests[0].user_id, lineage)
            if damage == "live":
                reply = await read_memory(web, headers, event, identity)
                assert reply.status_code == 503

    asyncio.run(scenario())


def test_profile_index_failure_rolls_back_all_evidence_then_recovers_without_writes(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, _, _ = await produce(
                app, web, config, monkeypatch, observe=False
            )
            lineage = app.core.state.cmh_lineage
            queue = app.core.state.memory_write_queue
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "CREATE TRIGGER fail_profile BEFORE INSERT ON cmh_event_profile_outputs "
                    "BEGIN SELECT RAISE(ABORT,'synthetic profile failure'); END"
                )
            with pytest.raises(AccessError, match="access_store_unavailable"):
                await lineage.observe(jobs[0], queue.payload_for(jobs[0]))
            with lineage.ingest._database() as db:
                for table in (
                    "cmh_event_outcomes",
                    "cmh_event_output_links",
                    "cmh_event_contexts",
                    "cmh_event_chat_inputs",
                    "cmh_event_memory_inputs",
                    "cmh_event_profile_inputs",
                    "cmh_event_profile_outputs",
                ):
                    assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute("DROP TRIGGER fail_profile")

            async def no_write(*args, **kwargs):
                pytest.fail("profile recovery invoked writer")

            monkeypatch.setattr(MemoryWriter, "write", no_write)
            await lineage.observe(jobs[0], queue.payload_for(jobs[0]))
            with lineage.ingest._database() as db:
                assert (
                    stored_profile(db, jobs[0], requests[0])["capture"]
                    == results[0]["basic_profile_inputs"]
                )

    asyncio.run(scenario())


def test_profile_bytes_are_preflighted_before_parsing(tmp_path, monkeypatch):
    from cloud_memory_hub import governance_inventory

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, _, _, _ = await produce(app, web, config, monkeypatch)
            monkeypatch.setattr(governance_inventory, "MAX_CONTENT_BYTES", 100_000)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "UPDATE cmh_event_profile_inputs SET profile_json=? WHERE receipt_id=?",
                    ("x" * 100_001, jobs[0]),
                )
            with pytest.raises(GovernanceError, match="scan_limit"):
                app.core.state.cmh_lineage.inventory(requests[0].user_id, {})

    asyncio.run(scenario())


def test_profile_candidate_cycle_is_not_accepted_as_ancestry(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, _, _ = await produce(app, web, config, monkeypatch)
            lineage = app.core.state.cmh_lineage
            identity = results[1]["basic_profile_inputs"]["output_ids"][0]
            record = await read_profile(_sdk_library_bridge(app.core), identity)
            with lineage.ingest._database() as db:
                value = stored_profile(db, jobs[0], requests[0])
            capture = value["capture"]
            stage = capture["stages"][0]
            stage["inputs"] = [{"kind": "profile", "id": identity, "record": record}]
            rule = json.loads(stage["rendered"])
            rule.update(existing_kv=record["custom"]["basic_info_kv"], head_ids=[identity])
            stage["rendered"] = json.dumps(rule)
            capture["attempted_supersede_ids"] = capture["superseded_ids"] = [identity]
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                encoded = encode(value)
                db.execute(
                    "UPDATE cmh_event_profile_inputs SET profile_json=?,profile_digest=? "
                    "WHERE receipt_id=?",
                    (encoded, digest(encoded), jobs[0]),
                )
            with pytest.raises(GovernanceError, match="cycle"):
                await observe_inventory(_sdk_library_bridge(app.core), requests[0].user_id, lineage)

    asyncio.run(scenario())
