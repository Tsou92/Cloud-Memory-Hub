import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.input_context import InputContext, capture_context, stored_context
from cloud_memory_hub.output_lineage import OutputLineage
from cloud_memory_hub.productivity_read import record_id
from cloud_memory_hub.remote_access import AccessError
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, CodingMemoryDraft, ReconcileOp
from hy_memory.coding.writer import CodingWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, db_rows, fixture, read_memory
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request, seed_sdk


def test_actual_history_survives_trace_queue_cleanup_and_process_reopen(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            event["content"]["messages"].append({"role": "tool", "text": "Synthetic tool result"})
            queue = app.core.state.memory_write_queue
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            store = CodingMemoryStore(
                sdk._config,
                sdk._embed_service,
                db_path=sdk._config.coding.db_path,
                variant="working",
            )
            await _joined_thread(lambda: sdk._loop_thread.run(store.initialize()))
            calls, jobs, requests, results = [], [], [], []

            async def extract(messages, **kwargs):
                calls.append(kwargs["history_messages"])
                return ([CodingMemoryDraft(task="Synthetic")] if len(calls) == 2 else []), []

            async def reconcile(*args, **kwargs):
                request = requests[-1]
                identity = str(uuid4())
                await store.insert(
                    CodingMemory(
                        experience_id=identity,
                        task="Synthetic",
                        solution="Synthetic solution",
                        user_id=request.user_id,
                        agent_id=request.agent_id,
                    )
                )
                return [ReconcileOp(action="ADD", draft_idx=0, target_experience_id=identity)]

            writer = CodingWriter(
                store,
                SimpleNamespace(extract=extract),
                SimpleNamespace(reconcile=reconcile),
                SimpleNamespace(),
                sdk._embed_service,
                fact_reconciler=SimpleNamespace(),
            )

            async def get_writer(**kwargs):
                return writer

            monkeypatch.setattr(sdk, "_get_coding_writer", get_writer)
            try:
                for _ in range(2):
                    current = {**event, "event_id": str(uuid4()), "record_id": str(uuid4())}
                    job = await accepted(web, headers, current)
                    frozen = queue.payload_for(job)
                    request = sdk_request(frozen)
                    jobs.append(job)
                    requests.append(request)
                    result = await _joined_thread(
                        sdk.add,
                        [turn.model_dump() for turn in request.turns],
                        scene="working",
                        user_id=request.user_id,
                        agent_id=request.agent_id,
                        session_id=request.session_id,
                        source=request.source_id,
                        request_id=request.request_id,
                    )
                    results.append(result)
                    assert result["success"] and result["scene"] == "productivity"
                    await app.core.state.cmh_lineage.observe(job, frozen)
                    queue.settle(job, state=COMPLETED)
                assert calls[0] == [] and calls[1]
                assert results[1]["history_request_ids"] == [requests[0].request_id]
                assert queue.clear_terminal_payloads() == 2
                await grant_reads(web, admin, event)
                with sqlite3.connect(store._trace_db_path) as db:
                    db.execute("DELETE FROM coding_trace")
                identity = record_id("w", "e", results[1]["experience_ids"][0])
                with app.core.state.cmh_ingest._database() as db:
                    first = stored_context(db, jobs[0])
                    assert first.history_status == "captured" and not first.history_request_ids
                    second = stored_context(db, jobs[1])
                    assert second.history_request_ids == (requests[0].request_id,)
            finally:
                await _joined_thread(lambda: sdk._loop_thread.run(store.close()))
        async with fixture(config) as (app, web):
            reply = await read_memory(web, headers, event, identity)
            assert reply.status_code == 200, reply.text
            assert set(reply.json()["items"][0]["source_raw_ids"]) == {
                "cmhe:" + job for job in jobs
            }
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            assert nodes[identity].parents == {"cmhe:" + job for job in jobs}
            assert all(not nodes["cmhe:" + job].parents for job in jobs)
            assert len(db_rows(config, "cmh_event_output_links")) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing", "foreign", "self", "atomic", "changed", "digest"])
def test_context_failure_never_commits_false_or_partial_output_proof(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            frozen = queue.payload_for(job)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            request = sdk_request(frozen)
            await seed_sdk(sdk, bridge, request, "working", monkeypatch)
            lineage = app.core.state.cmh_lineage
            original = bridge.get_sdk_write_outcome
            history = []
            if damage == "missing":
                history = ["unknown"]
            elif damage == "self":
                history = [request.request_id]
            elif damage == "foreign":
                _, other_headers, _, other_event = await provision(web, config)
                other_job = await accepted(web, other_headers, other_event)
                history = [queue.payload_for(other_job).idempotency_key]

            async def contextual(identity):
                result = await original(identity)
                result["result"].update(productivity_variant="working", history_request_ids=history)
                return result

            monkeypatch.setattr(bridge, "get_sdk_write_outcome", contextual)
            if damage in {"changed", "digest"}:
                await lineage.observe(job, frozen)
                if damage == "changed":
                    monkeypatch.setattr(bridge, "get_sdk_write_outcome", original)
                else:
                    with app.core.state.cmh_ingest._database(writing=True) as db:
                        db.execute("UPDATE cmh_event_contexts SET context_digest='broken'")
            elif damage == "atomic":
                with app.core.state.cmh_ingest._database(writing=True) as db:
                    db.execute(
                        "CREATE TRIGGER fail_context BEFORE INSERT ON cmh_event_contexts "
                        "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
                    )
            with pytest.raises((ValueError, AccessError)):
                await lineage.observe(job, frozen)
            if damage not in {"changed", "digest"}:
                assert not db_rows(config, "cmh_event_outcomes")
                assert not db_rows(config, "cmh_event_output_links")
                assert not db_rows(config, "cmh_event_contexts")

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["unknown", "marker_missing", "table_missing", "index_missing"])
def test_context_schema_refuses_ambiguous_or_partial_store(tmp_path, damage):
    config = config_for(tmp_path)
    from cloud_memory_hub.app import create_hub

    app = create_hub(config)
    with app.core.state.cmh_ingest._database(writing=True) as db:
        if damage == "unknown":
            db.execute("UPDATE cmh_context_schema SET version=999")
        elif damage == "marker_missing":
            db.execute("DROP TABLE cmh_context_schema")
        elif damage == "table_missing":
            db.execute("DROP TABLE cmh_event_contexts")
        else:
            db.execute("DROP INDEX cmh_context_requests")
    with pytest.raises((AccessError, sqlite3.OperationalError)):
        OutputLineage(app.core.state.cmh_ingest, lambda: None, app.core.state.cmh_governance)


def test_chain_scope_and_capture_status_are_not_inferred_from_counts(tmp_path):
    from hy_memory.coding.chain_store import HyCodeMem
    from test_sdk_context_capture import edit_messages

    base = tmp_path / "coding.db"
    path = Path(base).parent / "hy_code_chain" / "hy_code.db"
    path.parent.mkdir()
    store = HyCodeMem(str(path.parent), owner="scope|")
    from hy_memory.client import HyMemoryClient

    ids = []
    store.observe(HyMemoryClient._chain_text(edit_messages()), observed_round_ids=ids)
    store.db.close()
    assert ids
    result = {
        "scene": "productivity",
        "productivity_variant": "coding",
        "history_request_ids": [],
        "chain_rounds": len(ids),
        "chain_context": {"status": "complete", "round_ids": ids},
    }
    request = SimpleNamespace(user_id="scope")
    context = capture_context(result, base, request)
    assert context.chain_status == "captured" and context.chain_round_ids == tuple(ids)
    with pytest.raises(ValueError, match="unproven"):
        capture_context(result, base, SimpleNamespace(user_id="other"))
    for bad in ([True], [0], [ids[0], ids[0]], [2**63]):
        result["chain_context"]["round_ids"] = bad
        with pytest.raises(ValueError):
            capture_context(result, base, request)
    result["chain_context"] = {"status": "unavailable", "round_ids": []}
    assert capture_context(result, base, request).chain_status == "unavailable"
    del result["chain_context"]
    assert capture_context(result, base, request).chain_status == "unobserved"
    canonical = replace(context, chain_round_ids=()).encode()
    assert InputContext.decode(canonical).chain_status == "captured"
    with pytest.raises(ValueError):
        InputContext.decode(json.dumps(json.loads(canonical), indent=2))
