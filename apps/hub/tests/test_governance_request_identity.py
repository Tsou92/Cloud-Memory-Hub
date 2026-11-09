"""ADR-0036: signed request identity and truthful governance retries."""

import asyncio
import json
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_execution
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_execution import ExecuteRequest
from cloud_memory_hub.governance_requests import REQUEST_POLICY, request_targets
from cloud_memory_hub.governance_sources import content_policy_target, content_targets
from test_chat_graph import produce
from test_governance_execution import EXECUTE, PREVIEW, sdk_status
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_read import grant_reads


async def prepare(app, web, config, monkeypatch):
    jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
    admin = {"Authorization": "Bearer " + config.admin_token}
    body = {
        "scope_id": event["scope_id"],
        "action": "forget",
        "record_ids": ["cmhe:" + jobs[2]],
    }
    view = await web.post(PREVIEW, headers=admin, json=body)
    assert view.status_code == 200, view.text
    body.update(expected_view=view.json()["view_digest"], operation_id=str(uuid4()))
    return admin, body, jobs, results, headers, event


@pytest.mark.parametrize("field", ["action", "selection", "view", "scope", "actor"])
def test_same_operation_cannot_change_confirmed_request(tmp_path, monkeypatch, field):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, jobs, _, _, _ = await prepare(app, web, config, monkeypatch)
            first = await web.post(EXECUTE, headers=admin, json=body)
            assert first.status_code == 200, first.text
            changes = {
                "action": {"action": "purge"},
                "selection": {"record_ids": ["cmhe:" + jobs[1]]},
                "view": {"expected_view": "0" * 64},
                "scope": {"scope_id": str(uuid4())},
                "actor": {},
            }
            if field == "actor":
                monkeypatch.setattr(app.core.state.cmh_executor, "actor_id", str(uuid4()))
            changed = await web.post(EXECUTE, headers=admin, json={**body, **changes[field]})
            assert changed.status_code == 409, changed.text
            assert changed.json() == {"detail": "governance_operation_conflict"}
            assert app.core.state.cmh_governance.journal.head(body["scope_id"]).sequence == 1

    asyncio.run(scenario())


def test_identical_retry_normalizes_selection_order_and_keeps_one_intent(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, jobs, _, _, _ = await prepare(app, web, config, monkeypatch)
            selection = {
                "scope_id": body["scope_id"],
                "action": "forget",
                "record_ids": ["cmhe:" + jobs[2], "cmhe:" + jobs[1]],
            }
            view = await web.post(PREVIEW, headers=admin, json=selection)
            assert view.status_code == 200
            body.update(**selection, expected_view=view.json()["view_digest"])
            first = await web.post(EXECUTE, headers=admin, json=body)
            assert first.status_code == 200, first.text
            retry = await web.post(
                EXECUTE,
                headers=admin,
                json={**body, "record_ids": list(reversed(body["record_ids"])), "limit": 1},
            )
            assert retry.status_code == 200, retry.text
            assert retry.json()["state"] == "completed"
            assert retry.json()["suppressed_count"] == first.json()["suppressed_count"]
            journal = app.core.state.cmh_governance.journal
            assert journal.head(body["scope_id"]).sequence == 1
            assert len(app.core.state.cmh_executor.store.operations()) == 1

    asyncio.run(scenario())


def test_append_failure_retry_reexecutes_discarded_plan_instead_of_claiming_success(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor

            def fail_append(intent):
                raise RuntimeError("synthetic failure before journal append")

            with monkeypatch.context() as interruption:
                interruption.setattr(executor.authority, "append", fail_append)
                failed = await web.post(EXECUTE, headers=admin, json=body)
                assert failed.status_code == 503, failed.text
            assert executor.store.operations()[body["operation_id"]][0] == "planned"
            assert executor.authority.journal.head(body["scope_id"]).sequence == 0
            retried = await web.post(EXECUTE, headers=admin, json=body)
            assert retried.status_code == 200, retried.text
            assert await sdk_status(app, results[2]["memory_ids"][0]) == ("archived", False)
            assert retried.json()["suppressed_count"] > 0
            assert executor.store.operations()[body["operation_id"]][0] == "completed"
            assert executor.authority.journal.head(body["scope_id"]).sequence == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("planned", [False, True])
def test_legacy_request_is_unproven_but_automatic_resume_still_works(
    tmp_path, monkeypatch, planned
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor

            async def crash(*args, **kwargs):
                raise RuntimeError("synthetic legacy interruption")

            with monkeypatch.context() as legacy:
                legacy.setattr(governance_execution, "request_targets", lambda *args: ())
                if planned:
                    legacy.setattr(executor, "_finish", crash)
                first = await web.post(EXECUTE, headers=admin, json=body)
                assert first.status_code == (503 if planned else 200), first.text
            old = executor.store.operations()[body["operation_id"]][1]
            checkpoint = executor.authority.journal.head(body["scope_id"])
            retry = await web.post(EXECUTE, headers=admin, json=body)
            assert retry.status_code == 409, retry.text
            assert retry.json() == {"detail": "governance_operation_request_unproven"}
            for changed in ({"action": "purge"}, {"scope_id": str(uuid4())}):
                conflict = await web.post(EXECUTE, headers=admin, json={**body, **changed})
                assert conflict.status_code == 409
                assert conflict.json() == {"detail": "governance_operation_conflict"}
            await asyncio.wait_for(executor.resume(), 8)
            assert executor.store.operations()[body["operation_id"]] == ("completed", old)
            assert await sdk_status(app, results[2]["memory_ids"][0]) == ("archived", False)
            assert executor.authority.journal.head(body["scope_id"]) == checkpoint
            retry = await web.post(EXECUTE, headers=admin, json=body)
            assert retry.status_code == 409

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["append_committed", "finish", "recovery"])
def test_committed_intent_recovers_without_a_second_append(tmp_path, monkeypatch, failure):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor
            append = executor.authority.append

            def append_then_fail(intent):
                append(intent)
                raise RuntimeError("synthetic lost append response")

            async def finish_failure(*args, **kwargs):
                raise RuntimeError("synthetic interrupted SDK suppression")

            with monkeypatch.context() as fault:
                if failure == "append_committed":
                    fault.setattr(executor.authority, "append", append_then_fail)
                else:
                    fault.setattr(executor, "_finish", finish_failure)
                first = await web.post(EXECUTE, headers=admin, json=body)
                assert first.status_code == 503
                assert executor.store.operations()[body["operation_id"]][0] == "planned"
                assert executor.authority.journal.head(body["scope_id"]).sequence == 1
                if failure == "recovery":
                    failed_retry = await asyncio.wait_for(
                        web.post(EXECUTE, headers=admin, json=body), 8
                    )
                    assert failed_retry.status_code == 503
                    assert executor.store.operations()[body["operation_id"]][0] == "planned"
                    assert await sdk_status(app, results[2]["memory_ids"][0]) == ("active", True)
            retry = await asyncio.wait_for(web.post(EXECUTE, headers=admin, json=body), 8)
            assert retry.status_code == 200, retry.text
            assert executor.store.operations()[body["operation_id"]][0] == "completed"
            assert executor.authority.journal.head(body["scope_id"]).sequence == 1
            assert await sdk_status(app, results[2]["memory_ids"][0]) == ("archived", False)

    asyncio.run(scenario())


def test_incomplete_recovery_cannot_report_completed(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, _, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor

            async def crash(*args, **kwargs):
                raise RuntimeError("synthetic interruption")

            async def incomplete():
                return

            with monkeypatch.context() as first_fault:
                first_fault.setattr(executor, "_finish", crash)
                assert (await web.post(EXECUTE, headers=admin, json=body)).status_code == 503
            with monkeypatch.context() as recovery_fault:
                recovery_fault.setattr(executor, "_resume", incomplete)
                reply = await web.post(EXECUTE, headers=admin, json=body)
                assert reply.status_code == 503
                assert reply.json() == {"detail": "governance_execution_pending"}
            assert executor.store.operations()[body["operation_id"]][0] == "planned"

    asyncio.run(scenario())


def test_discarded_orphan_rechecks_view_and_reports_actual_conflict(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, headers, event = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor

            def crash(*args):
                raise RuntimeError("synthetic append failure")

            with monkeypatch.context() as fault:
                fault.setattr(executor.authority, "append", crash)
                assert (await web.post(EXECUTE, headers=admin, json=body)).status_code == 503
            await grant_reads(web, admin, event, permissions=("events:write",), revision=3)
            incoming = {
                **event,
                **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
            }
            assert (
                await web.post("/hub/v1/events", headers=headers, json=incoming)
            ).status_code == 202
            retry = await web.post(EXECUTE, headers=admin, json=body)
            assert retry.status_code == 409, retry.text
            assert retry.json() == {"detail": "governance_preview_changed"}
            assert executor.store.operations() == {}
            assert executor.authority.journal.head(body["scope_id"]).sequence == 0
            assert await sdk_status(app, results[2]["memory_ids"][0]) == ("active", True)

    asyncio.run(scenario())


def test_concurrent_same_request_and_resume_share_one_real_completion(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor
            entered, release = asyncio.Event(), asyncio.Event()
            finish = executor._finish
            calls = []

            async def held_finish(*args, **kwargs):
                calls.append(True)
                entered.set()
                await release.wait()
                await finish(*args, **kwargs)

            monkeypatch.setattr(executor, "_finish", held_finish)
            first = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
            second = resuming = None
            try:
                await asyncio.wait_for(entered.wait(), 8)
                second = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
                resuming = asyncio.create_task(executor.resume())
                for _ in range(3):
                    await asyncio.sleep(0)
                assert not second.done() and not resuming.done()
                assert executor.authority.journal.head(body["scope_id"]).sequence == 1
                release.set()
                replies = await asyncio.wait_for(asyncio.gather(first, second), 8)
                await asyncio.wait_for(resuming, 8)
                assert all(reply.status_code == 200 for reply in replies)
                assert all(reply.json()["state"] == "completed" for reply in replies)
                assert (
                    replies[0].json()["suppressed_count"] == replies[1].json()["suppressed_count"]
                )
                assert calls == [True]
                assert executor.authority.journal.head(body["scope_id"]).sequence == 1
                assert await sdk_status(app, results[2]["memory_ids"][0]) == ("archived", False)
            finally:
                release.set()
                await asyncio.wait_for(
                    asyncio.gather(
                        *(task for task in (first, second, resuming) if task is not None)
                    ),
                    8,
                )

    asyncio.run(scenario())


def test_repeated_cancelled_plan_is_joined_before_retry_reexecutes(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor
            entered, release = Event(), Event()
            original_plan = executor.store.plan

            def held_plan(*args):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("synthetic plan gate was not released")
                return original_plan(*args)

            monkeypatch.setattr(executor.store, "plan", held_plan)
            first = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
            retry = None
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for _ in range(3):
                    first.cancel()
                    await asyncio.sleep(0)
                retry = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
                await asyncio.sleep(0)
                assert not first.done() and not retry.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(first), 8)
                result = await asyncio.wait_for(asyncio.shield(retry), 8)
                assert result.status_code == 200, result.text
                assert executor.authority.journal.head(body["scope_id"]).sequence == 1
                assert executor.store.operations()[body["operation_id"]][0] == "completed"
                assert await sdk_status(app, results[2]["memory_ids"][0]) == ("archived", False)
            finally:
                release.set()
                await asyncio.wait_for(
                    asyncio.gather(
                        *(task for task in (first, retry) if task is not None),
                        return_exceptions=True,
                    ),
                    8,
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("before_request_count", [9998, 9999])
def test_two_request_targets_count_toward_the_existing_total_budget(
    tmp_path, monkeypatch, before_request_count
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, results, _, _ = await prepare(app, web, config, monkeypatch)
            executor = app.core.state.cmh_executor
            prepare_inflight = executor.inflight.prepare

            def near_limit(scope):
                targets = prepare_inflight(scope)
                # Reserve the two request identity targets while counting the
                # real in-flight certificates in the same total budget.
                non_request_count = len(scope.records) + len(scope.rule_targets) + len(targets) - 2
                return targets + tuple(
                    executor.authority.journal.target(
                        scope.scope_id, "source_record", f"synthetic-budget-{index}"
                    )
                    for index in range(before_request_count - non_request_count)
                )

            monkeypatch.setattr(executor.inflight, "prepare", near_limit)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            if before_request_count == 9998:
                assert reply.status_code == 200, reply.text
                assert len(executor.store.operations()[body["operation_id"]][1].targets) == 10_000
            else:
                assert reply.status_code == 409, reply.text
                assert reply.json() == {"detail": "governance_scan_limit"}
                assert executor.store.operations() == {}
                assert executor.authority.journal.head(body["scope_id"]).sequence == 0
                assert await sdk_status(app, results[2]["memory_ids"][0]) == ("active", True)

    asyncio.run(scenario())


def test_request_targets_do_not_match_messages_or_persist_plain_request(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, _, _, headers, event = await prepare(app, web, config, monkeypatch)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200
            executor = app.core.state.cmh_executor
            journal = executor.authority.journal
            request = ExecuteRequest.model_validate(body)
            targets = set(request_targets(journal, request, executor.actor_id))
            intent = executor.store.operations()[body["operation_id"]][1]
            assert targets <= set(intent.targets)
            assert content_policy_target(journal, body["scope_id"]) not in targets
            text = json.dumps(
                [
                    1,
                    "governance-request",
                    executor.actor_id,
                    body["scope_id"],
                    body["action"],
                    sorted(body["record_ids"]),
                    body["expected_view"],
                ],
                separators=(",", ":"),
            )
            candidate = {
                **event,
                **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
                "content": {
                    "messages": [
                        {"role": "user", "text": text},
                        {"role": "user", "text": REQUEST_POLICY},
                    ]
                },
            }
            assert not content_targets(journal, EventEnvelope.model_validate(candidate)) & targets
            await grant_reads(web, admin, event, permissions=("events:write",), revision=3)
            posted = await web.post("/hub/v1/events", headers=headers, json=candidate)
            assert posted.status_code == 202, posted.text
            for path in config.governance_dir.iterdir():
                if path.is_file():
                    persisted = path.read_bytes()
                    assert body["expected_view"].encode() not in persisted
                    assert b'"expected_view"' not in persisted and b'"record_ids"' not in persisted
                    assert text.encode() not in persisted

    asyncio.run(scenario())
