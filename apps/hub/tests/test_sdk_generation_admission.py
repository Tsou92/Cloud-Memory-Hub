"""FR-10/FR-11: real intake, bridge gate and durable log; synthetic SDK seam."""

import asyncio
import sqlite3
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from cloud_memory_hub.governance_sources import capture_rules
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HyMemoryBridgeError
from one_memory.memory_processing import MemoryTaskRunner, sdk_generation_input_for
from one_memory.memory_server.durable_queue import RUNNING, UNKNOWN
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


def generation_request(frozen):
    return MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records).model_copy(
        update={"source_context": None}
    )


def proof(app, frozen):
    generation = app.core.state.cmh_generation
    with generation.admissions.access._connection(writing=True) as db:
        return generation.admissions.proof_in(
            db, frozen.idempotency_key, actual=sdk_generation_input_for(frozen)
        )


async def intake(app, web, config):
    admin, headers, _, event = await provision(web, config)
    job = await accepted(web, headers, event)
    frozen = app.core.state.memory_write_queue.payload_for(job)
    assert frozen.idempotency_key.startswith("cmh2:")
    assert proof(app, frozen).state == "enrolled"
    return admin, headers, event, job, frozen


async def sdk_seam(app, monkeypatch):
    bridge = _sdk_library_bridge(app.core)
    sdk = await bridge._get_client()
    calls = []

    def add(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("synthetic SDK interruption")

    monkeypatch.setattr(sdk, "add", add)
    return bridge, calls


def test_real_bridge_consumes_one_permission_before_actual_sdk_call(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, event, job, frozen = await intake(app, web, config)
            bridge, calls = await sdk_seam(app, monkeypatch)
            request = generation_request(frozen)
            results = await asyncio.gather(
                bridge.ingest(request), bridge.ingest(request), return_exceptions=True
            )
            assert all(isinstance(item, HyMemoryBridgeError) for item in results)
            assert len(calls) == 1
            assert calls[0][1]["request_id"] == frozen.idempotency_key
            assert proof(app, frozen).state == "possible"
            # HTTP replay stays idempotent, without granting another SDK call.
            assert await accepted(web, headers, event) == job
            with pytest.raises(HyMemoryBridgeError):
                await bridge.ingest(request)
            assert len(calls) == 1
        async with fixture(config) as (restarted, _):
            assert proof(restarted, frozen).state == "possible"

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["turns", "source_context"])
def test_actual_input_mismatch_never_consumes_or_invokes(tmp_path, monkeypatch, field):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, frozen = await intake(app, web, config)
            bridge, calls = await sdk_seam(app, monkeypatch)
            request = generation_request(frozen)
            if field == "turns":
                turns = list(request.turns)
                turns[0] = turns[0].model_copy(update={"content": "Synthetic changed turn"})
                request = request.model_copy(update={"turns": tuple(turns)})
            else:
                request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
                assert request.source_context is not None
            with pytest.raises(HyMemoryBridgeError):
                await bridge.ingest(request)
            assert calls == []
            assert proof(app, frozen).state == "enrolled"

    asyncio.run(scenario())


def test_cancel_during_commit_joins_log_and_never_calls_sdk(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, frozen = await intake(app, web, config)
            bridge, calls = await sdk_seam(app, monkeypatch)
            journal = app.core.state.cmh_generation.admissions.journal
            append = journal.append
            entered, release = Event(), Event()

            def delayed(entry):
                if entry.phase == "possible":
                    entered.set()
                    assert release.wait(10)
                return append(entry)

            monkeypatch.setattr(journal, "append", delayed)
            task = asyncio.create_task(bridge.ingest(generation_request(frozen)))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert calls == []
            assert proof(app, frozen).state == "possible"
            with pytest.raises(HyMemoryBridgeError):
                await bridge.ingest(generation_request(frozen))
            assert calls == []

    asyncio.run(scenario())


def test_signed_deletion_blocks_first_permission_after_queue_restore(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, event, job, frozen = await intake(app, web, config)
            authority = app.core.state.cmh_governance
            store = ExecutionStore(authority.journal)
            records = {"cmhe:" + job: "raw"}
            rules = capture_rules(
                authority.journal, app.core.state.cmh_ingest, event["scope_id"], records
            )
            intent = Intent(
                operation_id=str(uuid4()),
                actor_id=authority.access.instance_id,
                scope_id=event["scope_id"],
                action="forget",
                targets=(authority.journal.target(event["scope_id"], "raw", "cmhe:" + job), *rules),
            )
            store.plan(intent, records)
            authority.append(intent)
            store.complete(intent)
            bridge, calls = await sdk_seam(app, monkeypatch)
            with pytest.raises(HyMemoryBridgeError):
                await bridge.ingest(generation_request(frozen))
            assert calls == []
            assert proof(app, frozen).state == "enrolled"

    asyncio.run(scenario())


def test_partial_acceptance_reuses_log_birth_after_config_rollback(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "CREATE TRIGGER fail_receipt BEFORE INSERT ON cmh_event_receipts "
                    "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
                )
            failed = await web.post("/hub/v1/events", headers=headers, json=event)
            assert failed.status_code == 503
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                before = db.execute(
                    "SELECT job_id,idempotency_key FROM memory_write_jobs"
                ).fetchone()
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                assert db.execute("SELECT count(*) FROM cmh_admission_witnesses").fetchone()[0] == 0
                db.execute("DROP TRIGGER fail_receipt")
            assert await accepted(web, headers, event) == before[0]
            frozen = app.core.state.memory_write_queue.payload_for(before[0])
            assert frozen.idempotency_key == before[1]
            assert proof(app, frozen).checkpoint.sequence == 1
            changed = {
                **event,
                "content": {"messages": [{"role": "user", "text": "Synthetic conflict"}]},
            }
            assert (
                await web.post("/hub/v1/events", headers=headers, json=changed)
            ).status_code == 409
            assert len(app.core.state.cmh_generation.admissions.journal.scopes()) == 1

    asyncio.run(scenario())


def test_unregistered_new_identity_never_invokes_sdk(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, frozen = await intake(app, web, config)
            bridge, calls = await sdk_seam(app, monkeypatch)
            request = generation_request(frozen)
            parts = request.request_id.split(":")
            parts[2] = str(uuid4())
            with pytest.raises(HyMemoryBridgeError):
                await bridge.ingest(request.model_copy(update={"request_id": ":".join(parts)}))
            assert calls == []
            assert proof(app, frozen).state == "enrolled"
            with pytest.raises(GovernanceError):
                app.core.state.cmh_generation.admissions.permit_once(
                    sdk_generation_input_for(
                        frozen.model_copy(update={"idempotency_key": ":".join(parts)})
                    ),
                    require_allowed=lambda _: None,
                )

    asyncio.run(scenario())


def test_actual_supervisor_and_drain_bind_intake_before_sdk_and_refuse_restored_queue(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, job, frozen = await intake(app, web, config)
            bridge, calls = await sdk_seam(app, monkeypatch)
            supervisor = app.core.state.memory_processing_supervisor

            async def credentials():
                return object(), None

            # Model readiness and client acquisition are the only supervisor
            # seams. Preserve its real routing, input transformation, prepare,
            # invocation mark, bridge gate and the Drain's result handling.
            monkeypatch.setattr(supervisor, "_active_credentials", credentials)
            monkeypatch.setattr(supervisor, "_sedimentation_stopped", lambda: False)
            monkeypatch.setattr(supervisor, "_bridge", lambda *a, **k: bridge)
            queue = app.core.state.memory_write_queue
            queue.settle(job, state=RUNNING)
            await app.core.state.memory_write_drain.execute(job)
            assert len(calls) == 1
            assert queue.get(frozen.idempotency_key).state == UNKNOWN
            assert proof(app, frozen).state == "possible"
            # Restore only the business queue's earlier state and invocation
            # flag. Independent permission evidence must survive unchanged.
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET state='pending',hy_invoked=0 WHERE job_id=?",
                    (job,),
                )
            queue.settle(job, state=RUNNING)
            await app.core.state.memory_write_drain.execute(job)
            assert len(calls) == 1
            assert queue.get(frozen.idempotency_key).state == UNKNOWN
            assert proof(app, frozen).state == "possible"

    asyncio.run(scenario())
