"""ADR-0034 Hub barriers, without claiming pending-job suppression is complete.

Use the actual Core drain, SDK, observer, HTTP and signed execution store. Only
the model/profile-dependent supervisor is replaced by the existing synthetic
SDK writer. Each asynchronous wait has a bounded failure path.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager, suppress
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub.output_lineage import OutputLineage
from one_memory.memory_server.durable_queue import COMPLETED, PENDING, RUNNING
from test_chat_graph import produce
from test_governance_execution import EXECUTE, PREVIEW, inflight_writer, sdk_status
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


async def wait(event):
    await asyncio.wait_for(event.wait(), 5)


async def turn():
    for _ in range(3):
        await asyncio.sleep(0)


async def wait_state(queue, request_id, state):
    async with asyncio.timeout(8):
        while queue.get(request_id).state != state:
            await asyncio.sleep(0.01)


async def finish(task):
    if task is not None:
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 8)


async def stop(drain, runner):
    drain.stop()
    try:
        await finish(runner)
    except TimeoutError:
        runner.cancel()
        await finish(runner)
        raise


def fresh(event):
    return {
        **event,
        **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
    }


def request_id(queue, event):
    with sqlite3.connect(queue.database_path) as db:
        rows = db.execute(
            "SELECT idempotency_key FROM memory_write_jobs WHERE idempotency_key LIKE ?",
            ("%:" + event["device_id"] + ":" + event["event_id"],),
        ).fetchall()
    assert len(rows) <= 1
    return rows[0][0] if rows else None


async def confirmed_request(web, config, event, receipt):
    admin = {"Authorization": "Bearer " + config.admin_token}
    body = {
        "scope_id": event["scope_id"],
        "action": "forget",
        "record_ids": ["cmhe:" + receipt],
        "operation_id": str(uuid4()),
    }
    view = await web.post(
        PREVIEW,
        headers=admin,
        json={key: value for key, value in body.items() if key != "operation_id"},
    )
    assert view.status_code == 200, view.text
    return admin, {**body, "expected_view": view.json()["view_digest"]}


@pytest.mark.parametrize("phase", ["prepare", "observer"])
def test_governance_waits_for_whole_worker_then_pending_work_resumes(tmp_path, monkeypatch, phase):
    config = config_for(tmp_path)

    async def scenario():
        blocked, release = asyncio.Event(), asyncio.Event()
        quiesce_requested, governing = asyncio.Event(), asyncio.Event()
        release_governance = asyncio.Event()
        worker_job = None
        original_observe = OutputLineage.observe

        async def observe(self, job_id, frozen):
            if phase == "observer" and job_id == worker_job:
                blocked.set()
                await release.wait()
            await original_observe(self, job_id, frozen)

        monkeypatch.setattr(OutputLineage, "observe", observe)
        async with fixture(config) as (app, web):
            jobs, _, _, _, target, _ = await produce(app, web, config, monkeypatch)
            admin, body = await confirmed_request(web, config, target, jobs[2])
            # The concurrent worker belongs to another project, so its output
            # does not invalidate the administrator's confirmed target view.
            _, headers, _, event = await provision(web, config)
            worker_job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            drain = app.core.state.memory_write_drain
            executor = app.core.state.cmh_executor
            gate = app.core.state.cmh_sdk_admission
            await inflight_writer(app, monkeypatch)
            # No paid model is configured in this isolated fixture. Supply
            # only its readiness identity; the synthetic supervisor below
            # performs real SDK writes while source/grant/Hub gates stay live.
            profiles = app.core.state.model_profile_store
            monkeypatch.setattr(profiles, "active_identity", lambda: ("synthetic-profile", 1))
            monkeypatch.setattr(profiles, "active", lambda secrets: None)
            assert drain.source_writes_permitted("cmh:" + event["source_id"])
            assert drain.source_claim_enabled("cmh:" + event["source_id"])
            supervisor = app.core.state.memory_processing_supervisor
            process = supervisor.process_sdk_job

            async def preparing(records, **kwargs):
                if phase == "prepare" and kwargs["request_id"] == request_id(queue, event):
                    blocked.set()
                    await release.wait()
                return await process(records, **kwargs)

            monkeypatch.setattr(supervisor, "process_sdk_job", preparing)
            original_quiesce, original_finish = drain.quiesce, executor._finish

            @asynccontextmanager
            async def requested_quiesce():
                quiesce_requested.set()
                async with original_quiesce():
                    yield

            async def paused_finish(*args, **kwargs):
                governing.set()
                await release_governance.wait()
                await original_finish(*args, **kwargs)

            monkeypatch.setattr(drain, "quiesce", requested_quiesce)
            monkeypatch.setattr(executor, "_finish", paused_finish)
            runner = asyncio.create_task(drain.run_forever())
            governance = None
            try:
                await wait(blocked)
                assert queue.get(request_id(queue, event)).state == RUNNING
                following = fresh(event)
                await accepted(web, headers, following)
                governance = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
                await wait(quiesce_requested)
                assert not governance.done() and not governing.is_set()
                assert executor.store.operations() == {}
                # The worker still needs SDK admission at either prepare or
                # observation. Governance must not close that gate prematurely.
                async with gate.operation():
                    pass
                release.set()
                await wait(governing)
                assert queue.get(request_id(queue, event)).state == COMPLETED
                assert queue.get(request_id(queue, following)).state == PENDING
                with app.core.state.cmh_ingest._database() as db:
                    assert db.execute(
                        "SELECT 1 FROM cmh_event_outcomes WHERE receipt_id=?", (worker_job,)
                    ).fetchone()
                release_governance.set()
                reply = await asyncio.wait_for(asyncio.shield(governance), 8)
                assert reply.status_code == 200, reply.text
                await wait_state(queue, request_id(queue, following), COMPLETED)
                assert executor.store.operations()[body["operation_id"]][0] == "completed"
            finally:
                release.set()
                release_governance.set()
                await finish(governance)
                await stop(drain, runner)

    asyncio.run(scenario())


def test_core_maintenance_callback_resumes_planned_governance_without_self_deadlock(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, _, event, _ = await produce(app, web, config, monkeypatch)
            admin, body = await confirmed_request(web, config, event, jobs[2])
            executor = app.core.state.cmh_executor

            async def interrupted(*args, **kwargs):
                raise RuntimeError("synthetic interruption after durable intent")

            with monkeypatch.context() as interruption:
                interruption.setattr(executor, "_finish", interrupted)
                reply = await web.post(EXECUTE, headers=admin, json=body)
                assert reply.status_code == 503, reply.text
            assert executor.store.operations()[body["operation_id"]][0] == "planned"
            resumed = asyncio.Event()
            original_resume = executor.resume
            runner = None

            async def tracked_resume():
                assert asyncio.current_task() is runner
                await original_resume()
                resumed.set()

            monkeypatch.setattr(executor, "resume", tracked_resume)
            drain = app.core.state.memory_write_drain
            drain._last_unknown_reconcile = None  # Force its actual maintenance callback this tick.
            runner = asyncio.create_task(drain.run_forever())
            try:
                await wait(resumed)
                assert executor.store.operations()[body["operation_id"]][0] == "completed"
                assert await sdk_status(app, results[2]["memory_ids"][0]) == ("archived", False)
                app.core.state.cmh_governance.require_writes_ready()
            finally:
                await stop(drain, runner)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancellations", [1, 3])
def test_cancelled_upload_joins_intake_before_governance_and_new_uploads_are_refused(
    tmp_path, monkeypatch, cancellations
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, _, target, _ = await produce(app, web, config, monkeypatch)
            admin, body = await confirmed_request(web, config, target, jobs[2])
            _, headers, _, event = await provision(web, config)
            ingest = app.core.state.cmh_ingest
            executor = app.core.state.cmh_executor
            queue = app.core.state.memory_write_queue
            gate = app.core.state.cmh_sdk_admission
            entered, release = Event(), Event()
            exclusive_requested = asyncio.Event()
            original_accept, original_plan = ingest.accept, executor.store.plan
            original_exclusive = gate.exclusive
            plans = []

            def accepting(credential, incoming):
                if incoming.event_id == event["event_id"]:
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError("synthetic intake barrier was not released")
                return original_accept(credential, incoming)

            def plan_after_intake(intent, records):
                assert queue.get(request_id(queue, event)) is not None
                plans.append(intent.operation_id)
                return original_plan(intent, records)

            @asynccontextmanager
            async def requested_exclusive():
                exclusive_requested.set()
                async with original_exclusive():
                    yield

            monkeypatch.setattr(ingest, "accept", accepting)
            monkeypatch.setattr(executor.store, "plan", plan_after_intake)
            monkeypatch.setattr(gate, "exclusive", requested_exclusive)
            upload = asyncio.create_task(web.post("/hub/v1/events", headers=headers, json=event))
            governance = None
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for _ in range(cancellations):
                    upload.cancel()
                    await turn()
                assert not upload.done()
                governance = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
                await wait(exclusive_requested)
                assert not governance.done() and not plans
                later = fresh(event)
                refused = await web.post("/hub/v1/events", headers=headers, json=later)
                assert refused.status_code == 503, refused.text
                assert refused.json() == {"detail": "governance_maintenance_in_progress"}
                assert queue.get(request_id(queue, later)) is None
                assert not plans
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(upload), 8)
                result = await asyncio.wait_for(asyncio.shield(governance), 8)
                assert result.status_code == 200, result.text
                assert plans == [body["operation_id"]]
                assert queue.get(request_id(queue, event)).state == PENDING
                with ingest._database() as db:
                    assert db.execute(
                        "SELECT 1 FROM cmh_event_receipts WHERE event_id=?", (event["event_id"],)
                    ).fetchone()
                # Maintenance has released the admission gate after completion.
                allowed = await web.post("/hub/v1/events", headers=headers, json=later)
                assert allowed.status_code == 202, allowed.text
            finally:
                release.set()
                await finish(upload)
                await finish(governance)

    asyncio.run(scenario())
