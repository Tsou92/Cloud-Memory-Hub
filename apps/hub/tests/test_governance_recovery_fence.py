"""ADR-0044: real HTTP retries and native permits after failed recovery."""

import asyncio
from contextlib import asynccontextmanager, suppress
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HyMemoryBridgeError, LocalHyEmbedService
from one_memory.memory_server.durable_queue import COMPLETED
from test_entity_inventory_guard import SyntheticEmbedder, seed_entity
from test_governance_execution import EXECUTE, inflight_writer, read
from test_inflight_execution_recovery import confirmation
from test_original_archive import config_for, stop_drain
from test_output_lineage import fixture
from test_remote_read import grant_reads
from test_sdk_generation_admission import generation_request, intake, proof, sdk_seam


@pytest.fixture(autouse=True)
def local_embedding(monkeypatch):
    monkeypatch.setattr(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder())


async def completed_purge(app, web, config):
    admin, _, event, job, _ = await intake(app, web, config)
    body = await confirmation(web, admin, event, [job], action="purge")
    reply = await web.post(EXECUTE, headers=admin, json=body)
    assert reply.status_code == 200 and reply.json()["state"] == "completed", reply.text
    return admin, body


def assert_blocked(app, event):
    executor = app.core.state.cmh_executor
    drain = app.core.state.memory_write_drain
    assert not executor.ready
    with pytest.raises(GovernanceError, match="governance_recovery_required"):
        executor.authority.require_writes_ready()
    assert not drain.source_claim_enabled("cmh:" + event["source_id"])
    assert not drain.source_writes_permitted("cmh:" + event["source_id"])
    # A recovery fault does not discard structural authority or hide safe reads.
    executor.authority.require_ready()


def assert_healthy(app, event):
    executor = app.core.state.cmh_executor
    drain = app.core.state.memory_write_drain
    assert executor.ready
    executor.authority.require_writes_ready()
    assert drain.source_claim_enabled("cmh:" + event["source_id"])
    assert drain.source_writes_permitted("cmh:" + event["source_id"])


def test_completed_retry_io_fault_blocks_claim_source_and_native_permit_until_full_resume(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body = await completed_purge(app, web, config)
            reader_admin, headers, event, retained, frozen = await intake(app, web, config)
            await grant_reads(
                web,
                reader_admin,
                event,
                permissions=("events:write", "memory:read", "history:read"),
            )
            profiles = app.core.state.model_profile_store
            monkeypatch.setattr(profiles, "active_identity", lambda: ("synthetic-profile", 1))
            monkeypatch.setattr(profiles, "active", lambda secrets: None)
            bridge, sdk_calls = await sdk_seam(app, monkeypatch)
            executor = app.core.state.cmh_executor
            assert_healthy(app, event)
            original = executor.purger._sdk_side_tables
            reached = []

            def broken(*args):
                reached.append(True)
                raise OSError("synthetic side-store failure")

            with monkeypatch.context() as failure:
                failure.setattr(executor.purger, "_sdk_side_tables", broken)
                reply = await web.post(EXECUTE, headers=admin, json=body)
                assert reply.status_code == 503
                assert len(reached) == 1
                assert executor.store.operations()[body["operation_id"]][0] == "completed"
                assert_blocked(app, event)
                assert (
                    await read(web, headers, event["scope_id"], "history", "cmhe:" + retained)
                ).status_code == 200
                before = proof(app, frozen)
                with pytest.raises(HyMemoryBridgeError):
                    await bridge.ingest(generation_request(frozen))
                assert not sdk_calls and proof(app, frozen) == before
                # A full retry must invalidate the old once-per-process replay cache.
                for _ in range(2):
                    with pytest.raises(OSError):
                        await executor.resume()
                    assert_blocked(app, event)
                assert len(reached) == 3

            # A local retry succeeding cannot certify every historical operation.
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200
            assert_blocked(app, event)
            await executor.resume()
            assert_healthy(app, event)
            with pytest.raises(HyMemoryBridgeError):
                await bridge.ingest(generation_request(frozen))
            assert len(sdk_calls) == 1 and proof(app, frozen).state == "possible"

            monkeypatch.setattr(executor.purger, "_sdk_side_tables", broken)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 503
            assert_blocked(app, event)
            monkeypatch.setattr(executor.purger, "_sdk_side_tables", original)
            await executor.resume()
            assert_healthy(app, event)

    asyncio.run(scenario())


def test_startup_is_unverified_until_complete_resume(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        executor = app.core.state.cmh_executor
        assert not executor.ready
        with pytest.raises(GovernanceError, match="governance_recovery_required"):
            executor.authority.require_writes_ready()
        executor.authority.require_ready()
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app)
            assert executor.ready
            executor.authority.require_writes_ready()

    asyncio.run(scenario())


def test_empty_execution_store_cannot_hide_unplanned_main_intent(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, event, _, _ = await intake(app, web, config)
            executor = app.core.state.cmh_executor
            journal = executor.authority.journal
            executor.authority.append(
                Intent(
                    operation_id=str(uuid4()),
                    actor_id=journal.instance_id,
                    scope_id=event["scope_id"],
                    action="forget",
                    targets=(journal.target(event["scope_id"], "memory", "synthetic-unplanned"),),
                )
            )
            assert executor.store.operations() == {}
            with pytest.raises(GovernanceError, match="governance_execution_pending"):
                await executor.resume()
            assert not executor.ready and not executor._replayed
            assert executor.authority.runtime_recovery_required

    asyncio.run(scenario())


@pytest.mark.parametrize("rejection", ["malformed", "stale", "conflict", "unsupported"])
def test_precommit_request_rejections_preserve_healthy_runtime(tmp_path, monkeypatch, rejection):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, done = await completed_purge(app, web, config)
            next_admin, _, event, job, _ = await intake(app, web, config)
            profiles = app.core.state.model_profile_store
            monkeypatch.setattr(profiles, "active_identity", lambda: ("synthetic-profile", 1))
            monkeypatch.setattr(profiles, "active", lambda secrets: None)
            body = await confirmation(web, next_admin, event, [job])
            before = app.core.state.cmh_executor.store.operations()
            if rejection == "malformed":
                body["operation_id"] = "invalid"
            elif rejection == "stale":
                body["expected_view"] = "0" * 64
            elif rejection == "conflict":
                body["operation_id"] = done["operation_id"]
            else:
                from cloud_memory_hub import governance_execution

                def unsupported(records):
                    raise GovernanceError("governance_forget_kind_unsupported")

                monkeypatch.setattr(governance_execution, "require_forget_supported", unsupported)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code in {409, 422, 503}
            assert app.core.state.cmh_executor.store.operations() == before
            assert_healthy(app, event)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["drain_enter", "diagnostic_exit", "exit_after_rejection"])
def test_resource_failures_fence_even_when_unwinding_a_rejected_request(
    tmp_path, monkeypatch, failure
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body = await completed_purge(app, web, config)
            _, _, event, _, _ = await intake(app, web, config)
            executor = app.core.state.cmh_executor
            if failure == "drain_enter":
                drain = app.core.state.memory_write_drain

                @asynccontextmanager
                async def broken():
                    raise OSError("synthetic drain failure")
                    yield

                monkeypatch.setattr(drain, "quiesce", broken)
            else:
                bridge = _sdk_library_bridge(app.core)
                original = bridge.quiesce_diagnostics

                @asynccontextmanager
                async def broken():
                    async with original():
                        try:
                            yield
                        finally:
                            raise OSError("synthetic diagnostic release failure")

                monkeypatch.setattr(bridge, "quiesce_diagnostics", broken)
                if failure == "exit_after_rejection":
                    next_admin, _, current, job, _ = await intake(app, web, config)
                    body = await confirmation(web, next_admin, current, [job])
                    body["expected_view"] = "0" * 64
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 503
            assert_blocked(app, event)
            assert not executor._replayed

    asyncio.run(scenario())


def test_cancelled_native_replay_joins_before_unlock_and_fences_before_next_permit(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body = await completed_purge(app, web, config)
            _, _, event, _, frozen = await intake(app, web, config)
            executor = app.core.state.cmh_executor
            gate = app.core.state.cmh_sdk_admission
            bridge, sdk_calls = await sdk_seam(app, monkeypatch)
            entered, release = Event(), Event()
            original = executor.purger._sdk_side_tables
            exits = []
            admission = gate.exclusive

            def delayed(*args):
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("synthetic replay was not released")
                return original(*args)

            @asynccontextmanager
            async def observed_admission():
                async with admission():
                    try:
                        yield
                    finally:
                        exits.append(executor.authority.runtime_recovery_required)

            monkeypatch.setattr(executor.purger, "_sdk_side_tables", delayed)
            monkeypatch.setattr(gate, "exclusive", observed_admission)
            task = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                assert not task.done() and not exits
                with pytest.raises(HyMemoryBridgeError):
                    await bridge.ingest(generation_request(frozen))
                assert not sdk_calls and proof(app, frozen).state == "enrolled"
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 5)
                assert exits == [True]
                assert_blocked(app, event)
                monkeypatch.setattr(executor.purger, "_sdk_side_tables", original)
                await executor.resume()
                assert executor.ready
                executor.authority.require_writes_ready()
            finally:
                release.set()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_fails", [False, True])
def test_permits_stay_closed_when_admission_unlocks_before_its_exit_finishes(
    tmp_path, monkeypatch, exit_fails
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body = await completed_purge(app, web, config)
            _, _, event, _, frozen = await intake(app, web, config)
            profiles = app.core.state.model_profile_store
            monkeypatch.setattr(profiles, "active_identity", lambda: ("synthetic-profile", 1))
            monkeypatch.setattr(profiles, "active", lambda secrets: None)
            executor, gate = app.core.state.cmh_executor, app.core.state.cmh_sdk_admission
            bridge, sdk_calls = await sdk_seam(app, monkeypatch)
            unlocked, release = asyncio.Event(), asyncio.Event()
            original = gate.exclusive

            @asynccontextmanager
            async def delayed_exit():
                async with original():
                    yield
                unlocked.set()
                await release.wait()
                if exit_fails:
                    raise OSError("synthetic failure after admission unlock")

            monkeypatch.setattr(gate, "exclusive", delayed_exit)
            task = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
            try:
                await asyncio.wait_for(unlocked.wait(), 5)
                assert not task.done() and gate._owner is None
                assert not executor.authority.runtime_recovery_required
                with pytest.raises(GovernanceError, match="governance_recovery_required"):
                    executor.authority.require_writes_ready()
                # The actual SDK admission is open; the shared permit fence
                # must still stop this direct bridge call before consuming it.
                before = proof(app, frozen)
                with pytest.raises(HyMemoryBridgeError):
                    await bridge.ingest(generation_request(frozen))
                assert not sdk_calls and proof(app, frozen) == before
                release.set()
                reply = await asyncio.wait_for(asyncio.shield(task), 5)
                assert reply.status_code == (503 if exit_fails else 200)
                if exit_fails:
                    assert_blocked(app, event)
                    monkeypatch.setattr(gate, "exclusive", original)
                    await executor.resume()
                assert_healthy(app, event)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.shield(task), 5)

    asyncio.run(scenario())


def test_cancellation_waiting_for_another_resume_does_not_fault_healthy_runtime(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, _):
            executor = app.core.state.cmh_executor
            async with executor._resume_lock:
                task = asyncio.create_task(executor.resume())
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert executor.ready
                executor.authority.require_writes_ready()

    asyncio.run(scenario())


@pytest.mark.parametrize("already_committed", [False, True])
def test_native_unmodeled_entity_is_a_request_rejection_or_recovery_fault_by_phase(
    tmp_path, already_committed
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            if already_committed:
                admin, body = await completed_purge(app, web, config)
            else:
                admin, _, event, job, _ = await intake(app, web, config)
                body = await confirmation(web, admin, event, [job], action="purge")
            executor = app.core.state.cmh_executor
            before = executor.store.operations()
            sdk = await _sdk_library_bridge(app.core)._get_client()
            namespace = f"cmh:{executor.authority.access.instance_id}:{body['scope_id']}"
            await seed_entity(sdk, namespace)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 503
            assert reply.json()["detail"] == "governance_derivation_unmodeled"
            assert executor.store.operations() == before
            assert executor.ready is not already_committed
            assert executor.authority.runtime_recovery_required is already_committed
            if already_committed:
                with pytest.raises(GovernanceError, match="governance_recovery_required"):
                    executor.authority.require_writes_ready()
                assert not executor._replayed
            else:
                executor.authority.require_writes_ready()

    asyncio.run(scenario())


def test_cancelled_final_verification_cannot_open_permits_before_thread_returns(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body = await completed_purge(app, web, config)
            _, _, event, _, frozen = await intake(app, web, config)
            executor, authority = app.core.state.cmh_executor, app.core.state.cmh_governance
            bridge, sdk_calls = await sdk_seam(app, monkeypatch)

            def broken(*args):
                raise OSError("synthetic recovery required before final verification")

            with monkeypatch.context() as failure:
                failure.setattr(executor.purger, "_sdk_side_tables", broken)
                reply = await web.post(EXECUTE, headers=admin, json=body)
                assert reply.status_code == 503
            assert_blocked(app, event)
            cleared, release = Event(), Event()
            original = authority.finish_runtime_recovery

            def delayed_return():
                original()
                cleared.set()
                if not release.wait(8):
                    raise TimeoutError("synthetic final verification was not released")

            monkeypatch.setattr(authority, "finish_runtime_recovery", delayed_return)
            task = asyncio.create_task(executor.resume())
            try:
                assert await asyncio.to_thread(cleared.wait, 5)
                assert not authority.runtime_recovery_required
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                assert not task.done()
                before = proof(app, frozen)
                with pytest.raises(HyMemoryBridgeError):
                    await bridge.ingest(generation_request(frozen))
                assert not sdk_calls and proof(app, frozen) == before
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 5)
                assert_blocked(app, event)
                monkeypatch.setattr(authority, "finish_runtime_recovery", original)
                await executor.resume()
                assert executor.ready
                authority.require_writes_ready()
            finally:
                release.set()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 5)

    asyncio.run(scenario())


def test_healthy_background_resume_preserves_active_worker_and_shared_sdk_reads(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, event, _, frozen = await intake(app, web, config)
            profiles = app.core.state.model_profile_store
            monkeypatch.setattr(profiles, "active_identity", lambda: ("synthetic-profile", 1))
            monkeypatch.setattr(profiles, "active", lambda secrets: None)
            executor, drain = app.core.state.cmh_executor, app.core.state.memory_write_drain
            assert executor.ready and executor._replayed and not executor.store.operations()
            outputs = await inflight_writer(app, monkeypatch)
            supervisor = app.core.state.memory_processing_supervisor
            process, reconcile = supervisor.process_sdk_job, drain.reconcile_unknown
            quiesce = drain.quiesce
            worker_entered, worker_done = asyncio.Event(), asyncio.Event()
            barrier_requested, reconciled = asyncio.Event(), asyncio.Event()
            observed = []

            async def delayed_process(*args, **kwargs):
                worker_entered.set()
                waiters = [
                    asyncio.create_task(barrier_requested.wait()),
                    asyncio.create_task(reconciled.wait()),
                ]
                try:
                    await asyncio.wait_for(
                        asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED), 5
                    )
                    observed.append(
                        (
                            reconciled.is_set(),
                            drain.source_writes_permitted("cmh:" + event["source_id"]),
                        )
                    )
                    return await process(*args, **kwargs)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                    await asyncio.gather(*waiters, return_exceptions=True)
                    worker_done.set()

            async def reconcile_after_claim():
                await asyncio.wait_for(worker_entered.wait(), 5)
                await reconcile()
                reconciled.set()

            @asynccontextmanager
            async def observed_quiesce():
                barrier_requested.set()
                async with quiesce():
                    yield

            def must_not_clear():
                raise AssertionError("a healthy background check must not clear a fault")

            monkeypatch.setattr(supervisor, "process_sdk_job", delayed_process)
            monkeypatch.setattr(drain, "reconcile_unknown", reconcile_after_claim)
            monkeypatch.setattr(drain, "quiesce", observed_quiesce)
            monkeypatch.setattr(executor.authority, "finish_runtime_recovery", must_not_clear)
            drain._last_unknown_reconcile = None
            runner = asyncio.create_task(drain.run_forever())
            try:
                await asyncio.wait_for(worker_done.wait(), 6)
                await asyncio.wait_for(reconciled.wait(), 5)
                assert observed == [(True, True)]
                assert len(outputs) == 1
                bridge = _sdk_library_bridge(app.core)
                assert await bridge.get_sdk_memory(str(uuid4())) is None
                assert (
                    app.core.state.memory_write_queue.get(frozen.idempotency_key).state == COMPLETED
                )
            finally:
                drain.stop()
                await asyncio.wait_for(runner, 8)
            executor.authority.require_writes_ready()
            assert executor.ready

    asyncio.run(scenario())
