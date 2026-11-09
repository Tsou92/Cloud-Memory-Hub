"""FR-10/FR-11: signed cancellation, uncertain effects and operation-local replay."""

import asyncio
import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_execution
from cloud_memory_hub.governance_execution import pending_operations
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from one_memory.memory_processing import sdk_generation_input_for
from one_memory.memory_server.durable_queue import CANCELLED, UNKNOWN
from test_governance_execution import EXECUTE, PREVIEW
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


def test_uncommitted_orphan_is_discarded_before_sdk_scope_observation(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, event = await provision(web, config)
            executor = app.core.state.cmh_executor
            identity = "cmhp:synthetic-uncommitted-productivity"
            intent = Intent(
                operation_id=str(uuid4()),
                actor_id=str(uuid4()),
                scope_id=event["scope_id"],
                action="forget",
                targets=(executor.authority.journal.target(event["scope_id"], "memory", identity),),
            )
            executor.store.plan(intent, {identity: "memory"})
            executor._replayed = False

            async def unsupported_scope(*args):
                pytest.fail("uncommitted orphan must not request SDK observation")

            monkeypatch.setattr(governance_execution, "require_modeled_scope", unsupported_scope)
            await executor.resume()
            assert executor.store.operations() == {}
            assert executor.store.suppressed(event["scope_id"]) == {}
            assert executor.authority.journal.head(event["scope_id"]).sequence == 0
            executor.authority.require_writes_ready()

    asyncio.run(scenario())


@pytest.mark.parametrize("completed", [False, True])
def test_signed_execution_intent_must_equal_the_committed_journal_intent(tmp_path, completed):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            executor = app.core.state.cmh_executor
            identity = "cmhe:" + job
            intent = Intent(
                operation_id=str(uuid4()),
                actor_id=str(uuid4()),
                scope_id=event["scope_id"],
                action="forget",
                targets=(executor.authority.journal.target(event["scope_id"], "raw", identity),),
            )
            executor.store.plan(intent, {identity: "raw"})
            if completed:
                executor.store.complete(intent)
            # Two individually signed stores are not evidence of the same
            # operation when their intents differ (e.g. mixed restore).
            executor.authority.append(intent.model_copy(update={"action": "purge"}))
            with pytest.raises(GovernanceError, match="governance_execution_tampered"):
                pending_operations(executor.authority.journal, executor.store)
            assert row(app.core.state.memory_write_queue, job)["state"] != CANCELLED

    asyncio.run(scenario())


def row(queue, job):
    with sqlite3.connect(queue.database_path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM memory_write_jobs WHERE job_id=?", (job,)).fetchone())


async def confirmation(web, admin, event, jobs, *, action="forget"):
    body = {
        "scope_id": event["scope_id"],
        "action": action,
        "record_ids": ["cmhe:" + job for job in jobs],
    }
    view = await web.post(PREVIEW, headers=admin, json=body)
    assert view.status_code == 200, view.text
    return {**body, "expected_view": view.json()["view_digest"], "operation_id": str(uuid4())}


async def mark_possible(app, queue, job):
    async with app.core.state.cmh_sdk_admission.operation():
        await app.core.state.cmh_generation.permit(sdk_generation_input_for(queue.payload_for(job)))


@pytest.mark.parametrize("possible", [False, True])
def test_unknown_is_cancelled_only_with_independent_enrolled_proof(tmp_path, possible):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            if possible:
                await mark_possible(app, queue, job)
            queue.settle(job, state=UNKNOWN, hy_invoked=True)
            before = row(queue, job)
            body = await confirmation(web, admin, event, [job])
            reply = await web.post(EXECUTE, headers=admin, json=body)
            if possible:
                assert reply.status_code == 503, reply.text
                assert row(queue, job) == before
                assert app.core.state.cmh_executor.store.operations() == {}
            else:
                assert reply.status_code == 200, reply.text
                after = row(queue, job)
                assert after["state"] == CANCELLED
                assert after["hy_invoked"] == 1
                assert after["payload_json"] == before["payload_json"]

    asyncio.run(scenario())


def test_later_unrelated_possible_task_does_not_block_completed_replay(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            body = await confirmation(web, admin, event, [job])
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            later = {
                **event,
                **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
            }
            later_job = await accepted(web, headers, later)
            await mark_possible(app, queue, later_job)
            queue.settle(later_job, state=UNKNOWN, hy_invoked=True)
            before = row(queue, later_job)
            checkpoint = app.core.state.cmh_governance.journal.head(event["scope_id"])
        async with fixture(config) as (app, web):
            assert app.core.state.cmh_executor.ready
            assert row(app.core.state.memory_write_queue, later_job) == before
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            assert row(app.core.state.memory_write_queue, later_job) == before
            assert app.core.state.cmh_governance.journal.head(event["scope_id"]) == checkpoint

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_interrupted_cancellation_finishes_same_signed_plan_after_restart(
    tmp_path, monkeypatch, action
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            jobs = [await accepted(web, headers, event)]
            jobs.append(
                await accepted(web, headers, {**event, "event_id": str(uuid4()), "revision": 2})
            )
            queue = app.core.state.memory_write_queue
            original = queue.cancel_unpermitted_for_governance
            calls = []

            def interrupted(job, **kwargs):
                calls.append(job)
                if len(calls) == 2:
                    raise RuntimeError("synthetic cancellation interruption")
                return original(job, **kwargs)

            body = await confirmation(web, admin, event, jobs, action=action)
            with monkeypatch.context() as interruption:
                interruption.setattr(queue, "cancel_unpermitted_for_governance", interrupted)
                reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 503, reply.text
            assert len(calls) == 2
            assert row(queue, calls[0])["state"] == CANCELLED
            assert row(queue, calls[1])["state"] != CANCELLED
            assert (
                app.core.state.cmh_executor.store.operations()[body["operation_id"]][0] == "planned"
            )
            checkpoint = app.core.state.cmh_governance.journal.head(event["scope_id"])
        async with fixture(config) as (app, web):
            for job in jobs:
                assert row(app.core.state.memory_write_queue, job)["state"] == CANCELLED
            assert (
                app.core.state.cmh_executor.store.operations()[body["operation_id"]][0]
                == "completed"
            )
            assert app.core.state.cmh_governance.journal.head(event["scope_id"]) == checkpoint

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_completed_retry_rechecks_restored_target_without_new_intent(tmp_path, action):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            before = row(queue, job)
            body = await confirmation(web, admin, event, [job], action=action)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            checkpoint = app.core.state.cmh_governance.journal.head(event["scope_id"])
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET state=?,payload_json=?,hy_invoked=? "
                    "WHERE job_id=?",
                    (before["state"], before["payload_json"], before["hy_invoked"], job),
                )
            assert app.core.state.cmh_executor._replayed
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            assert row(queue, job)["state"] == CANCELLED
            assert app.core.state.cmh_governance.journal.head(event["scope_id"]) == checkpoint

    asyncio.run(scenario())
