import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.output_lineage import OutputLineage
from cloud_memory_hub.productivity_read import record_id
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.sdk_reconcile import proven_outputs
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, WorkingFact
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HyMemoryIngestResult, HyMemoryOutput, _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED, RUNNING, UNKNOWN
from test_original_archive import config_for, stop_drain
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request, seed_sdk


@asynccontextmanager
async def fixture(config):
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        await stop_drain(app)
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            yield app, web


def db_rows(config, table):
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        return db.execute("SELECT * FROM " + table).fetchall()


async def accepted(web, headers, event):
    reply = await web.post("/hub/v1/events", headers=headers, json=event)
    assert reply.status_code == 202, reply.text
    return reply.json()["receipt_id"]


async def read_memory(web, headers, event, identity):
    return await web.post(
        "/hub/v1/memories/get",
        headers=headers,
        json={"scope_id": event["scope_id"], "record_id": identity},
    )


@pytest.mark.parametrize("scene", ["chat", "working", "coding"])
@pytest.mark.parametrize("fail_commit", [False, True])
def test_output_commit_precedes_completion_and_unknown_recovery_never_rewrites(
    tmp_path, monkeypatch, scene, fail_commit
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            frozen = queue.payload_for(job)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            ids, calls = await seed_sdk(sdk, bridge, sdk_request(frozen), scene, monkeypatch)
            lineage = app.core.state.cmh_lineage
            if fail_commit:
                with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                    db.execute(
                        "CREATE TRIGGER fail_link BEFORE INSERT ON cmh_event_output_links "
                        "BEGIN SELECT RAISE(ABORT,'synthetic link failure'); END"
                    )

            async def process(records, *, request_id, before_hy_invocation):
                await before_hy_invocation()
                assert request_id == frozen.idempotency_key
                return HyMemoryIngestResult(
                    success=True,
                    raw_ids=ids[0],
                    memories=tuple(
                        HyMemoryOutput(memory_id=identity, content="Synthetic output")
                        for identity in ids[1]
                    ),
                )

            queue.settle(job, state=RUNNING)
            monkeypatch.setattr(
                app.core.state.memory_processing_supervisor, "process_sdk_job", process
            )
            await app.core.state.memory_write_drain.execute(job)
            assert queue.get(frozen.idempotency_key).state == (
                UNKNOWN if fail_commit else COMPLETED
            )
            if fail_commit:
                assert db_rows(config, "cmh_event_outcomes") == []
                assert db_rows(config, "cmh_event_output_links") == []
                with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                    db.execute("DROP TRIGGER fail_link")

                def no_write(*args, **kwargs):
                    pytest.fail("recovery re-invoked the SDK writer")

                monkeypatch.setattr(sdk, "add", no_write)
                assert await app.core.state.cmh_reconciler.run() == 1
            assert queue.get(frozen.idempotency_key).state == COMPLETED
            assert len(db_rows(config, "cmh_event_outcomes")) == 1
            assert len(calls) == 1
            await lineage.observe(job, frozen)
            assert queue.clear_terminal_payloads() == 1
            await grant_reads(web, admin, event)
            identities = (
                ids[1]
                if scene == "chat"
                else tuple(
                    record_id("w" if scene == "working" else "c", kind, native)
                    for kind, native in zip(("e", "f"), ids[1], strict=True)
                )
            )
            for identity in identities:
                result = await read_memory(web, headers, event, identity)
                assert result.status_code == 200, result.text
                assert "cmhe:" + job in result.json()["items"][0]["source_raw_ids"]
            assert len(db_rows(config, "cmh_event_output_links")) == sum(map(len, ids))
        async with fixture(config) as (_, web):
            result = await read_memory(web, headers, event, identities[0])
            assert "cmhe:" + job in result.json()["items"][0]["source_raw_ids"]
            raw = await web.post(
                "/hub/v1/history/get",
                headers=headers,
                json={"scope_id": event["scope_id"], "record_id": "cmhe:" + job},
            )
            assert raw.status_code == 200
            assert json.loads(raw.json()["items"][0]["content"])["content"] == event["content"]
            denied = await read_memory(web, headers, {**event, "scope_id": "global"}, identities[0])
            assert denied.status_code in (403, 404)

    asyncio.run(scenario())


def test_reaped_completed_legacy_results_backfill_and_keep_multiple_origins(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            queue = app.core.state.memory_write_queue
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            first = await accepted(web, headers, event)
            ids, _ = await seed_sdk(
                sdk, bridge, sdk_request(queue.payload_for(first)), "chat", monkeypatch
            )
            queue.settle(first, state=COMPLETED, sdk_raw_ids=ids[0], sdk_memory_ids=ids[1])
            second_event = {**event, "event_id": str(uuid4()), "revision": 2}
            second = await accepted(web, headers, second_event)
            request = sdk_request(queue.payload_for(second))

            async def repeated(*args, **kwargs):
                return {
                    "success": True,
                    "scene": "chat",
                    "request_id": request.request_id,
                    "memory_id": ids[0][0],
                    "memory_ids": list(ids[1]),
                }

            monkeypatch.setattr(sdk, "_async_add_impl", repeated)
            await _joined_thread(
                sdk.add,
                "Synthetic update",
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                request_id=request.request_id,
            )
            queue.settle(second, state=COMPLETED, sdk_raw_ids=ids[0], sdk_memory_ids=ids[1])
            assert queue.clear_terminal_payloads() == 2
            assert queue.payload_for(first) is None
            assert await app.core.state.cmh_lineage.reconcile_completed() == 2
            assert await app.core.state.cmh_lineage.reconcile_completed() == 0
            await grant_reads(web, admin, event)
            result = await read_memory(web, headers, event, ids[1][0])
            assert {"cmhe:" + first, "cmhe:" + second} <= set(
                result.json()["items"][0]["source_raw_ids"]
            )
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute("UPDATE cmh_event_outcomes SET links_digest='damaged'")
            assert (await read_memory(web, headers, event, ids[1][0])).status_code == 503

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["working", "coding"])
def test_real_sdk_route_persists_variant_and_disambiguates_equal_native_ids(
    tmp_path, monkeypatch, variant
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            request = sdk_request(app.core.state.memory_write_queue.payload_for(job))
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            experience = fact = str(uuid4())

            async def populate():
                for name in ("working", "coding"):
                    store = CodingMemoryStore(
                        sdk._config,
                        sdk._embed_service,
                        db_path=sdk._config.coding.db_path,
                        variant=name,
                    )
                    await store.initialize()
                    try:
                        identity = dict(
                            user_id=request.user_id,
                            agent_id=request.agent_id,
                            session_id=request.session_id,
                        )
                        await store.insert(
                            CodingMemory(experience_id=experience, task="Synthetic", **identity)
                        )
                        await store.insert_fact(
                            WorkingFact(fact_id=fact, statement="Synthetic", **identity)
                        )
                    finally:
                        await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(populate()))
            selected = []

            async def get_writer(*, variant):
                selected.append(variant)

                async def write(*args, **kwargs):
                    return dict(
                        success=True,
                        scene="productivity",
                        request_id=kwargs["request_id"],
                        experience_ids=[experience],
                        fact_ids=[fact],
                    )

                return SimpleNamespace(write=write)

            monkeypatch.setattr(sdk, "_get_coding_writer", get_writer)
            monkeypatch.setattr(sdk, "_observe_chain", lambda *args, **kwargs: [])
            result = await _joined_thread(
                sdk.add,
                [
                    {"role": "user", "content": "Synthetic task"},
                    {"role": "tool", "content": "Synthetic result", "tool_name": "read"},
                ],
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                request_id=request.request_id,
                scene=variant,
            )
            assert result["productivity_variant"] == variant and selected == [variant]
            outcome = await bridge.get_sdk_write_outcome(request.request_id)
            assert outcome["result"]["productivity_variant"] == variant
            _, links = await proven_outputs(bridge, job, request, "add")
            assert set(links) == {
                ("memory", record_id(variant[0], "e", experience)),
                ("memory", record_id(variant[0], "f", fact)),
            }
            original = bridge.get_sdk_write_outcome

            async def legacy(identity):
                value = await original(identity)
                value["result"].pop("productivity_variant")
                return value

            monkeypatch.setattr(bridge, "get_sdk_write_outcome", legacy)
            with pytest.raises(ValueError, match="ambiguous"):
                await proven_outputs(bridge, job, request, "add")

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["unknown_version", "orphan_table"])
def test_lineage_schema_failures_never_recreate_data(tmp_path, damage):
    config = config_for(tmp_path)
    app = create_hub(config)
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        if damage == "unknown_version":
            db.execute("UPDATE cmh_lineage_schema SET version=999")
        else:
            db.execute("DROP TABLE cmh_lineage_schema")
    with pytest.raises(AccessError, match="lineage_store_"):
        OutputLineage(app.core.state.cmh_ingest, lambda: None, app.core.state.cmh_governance)
    assert db_rows(config, "cmh_event_outcomes") == []


@pytest.mark.parametrize("invalid", [False, True])
def test_explicit_zero_output_is_distinct_from_unproven_result(tmp_path, monkeypatch, invalid):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            frozen = queue.payload_for(job)
            request = sdk_request(frozen)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()

            async def empty(*args, **kwargs):
                return dict(
                    success=True,
                    scene="productivity",
                    request_id=request.request_id,
                    experience_ids=[],
                    fact_ids=[],
                    memory_id=None,
                )

            monkeypatch.setattr(sdk, "_async_add_impl", empty)
            await _joined_thread(
                sdk.add,
                "Synthetic zero result",
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                request_id=request.request_id,
            )
            if invalid:
                original = bridge.get_sdk_write_outcome

                async def damaged(identity):
                    value = await original(identity)
                    value["result"].pop("fact_ids")
                    return value

                monkeypatch.setattr(bridge, "get_sdk_write_outcome", damaged)
                with pytest.raises(ValueError):
                    await app.core.state.cmh_lineage.observe(job, frozen)
                assert not db_rows(config, "cmh_event_outcomes")
            else:
                await app.core.state.cmh_lineage.observe(job, frozen)
                await app.core.state.cmh_lineage.observe(job, frozen)
                assert len(db_rows(config, "cmh_event_outcomes")) == 1
            assert not db_rows(config, "cmh_event_output_links")

    asyncio.run(scenario())


def test_backfill_rotates_past_pending_events_and_rejects_mismatched_inputs(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            for _ in range(32):
                await accepted(
                    web, headers, {**event, "event_id": str(uuid4()), "record_id": str(uuid4())}
                )
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            frozen = queue.payload_for(job)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            ids, _ = await seed_sdk(sdk, bridge, sdk_request(frozen), "chat", monkeypatch)
            wrong = frozen.model_copy(update={"internal_session_id": "synthetic-wrong-session"})
            with pytest.raises(ValueError, match="lineage_input_mismatch"):
                await app.core.state.cmh_lineage.observe(job, wrong)
            assert not db_rows(config, "cmh_event_outcomes")
            queue.settle(job, state=COMPLETED, sdk_raw_ids=ids[0], sdk_memory_ids=ids[1])
            assert await app.core.state.cmh_lineage.reconcile_completed() == 0
            assert await app.core.state.cmh_lineage.reconcile_completed() == 1
            assert len(db_rows(config, "cmh_event_outcomes")) == 1

    asyncio.run(scenario())
