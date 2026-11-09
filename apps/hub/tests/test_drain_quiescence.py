"""ADR-0034: real SQLite claim/handle lifetimes, with synthetic SDK work only."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from cloud_memory_hub.sdk_admission import SdkAdmission
from one_memory.memory_server.contracts import BatchIdentity, WriteRequest
from one_memory.memory_server.durable_queue import (
    COMPLETED,
    PENDING,
    DurableWriteDrain,
    DurableWriteQueue,
    execute_job,
)


def submit(queue, key="first"):
    return queue.submit(
        WriteRequest(
            internal_session_id="synthetic-session",
            source_id="synthetic-source",
            batch_identity=BatchIdentity(
                internal_session_id="synthetic-session", first_round=1, last_round=1
            ),
            evidence_ids=["synthetic-evidence"],
            idempotency_key=key,
        )
    )


async def wait(event):
    await asyncio.wait_for(event.wait(), 3)


async def turn():
    # Scheduling barriers, not elapsed-time guesses about completion.
    for _ in range(3):
        await asyncio.sleep(0)


def run(drain, mode):
    return asyncio.create_task(drain.run_one() if mode == "one" else drain.run_forever())


async def finish(drain, task):
    drain.stop()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), 3)


@pytest.mark.parametrize("mode", ["one", "forever"])
@pytest.mark.parametrize("phase", ["prepare", "write", "observe"])
def test_quiesce_waits_for_full_native_job_before_sdk_exclusive(tmp_path, mode, phase):
    async def scenario():
        queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
        first = submit(queue)
        gate = SdkAdmission()
        blocked, release = asyncio.Event(), asyncio.Event()
        governed, release_governance = asyncio.Event(), asyncio.Event()
        observed = []

        async def at_phase(name):
            if phase == name:
                blocked.set()
                await release.wait()
            async with gate.operation():
                return

        class Supervisor:
            sdk_authority = staticmethod(lambda: True)

            async def process_sdk_job(self, records, *, request_id, before_hy_invocation):
                await at_phase("prepare")
                await before_hy_invocation()
                await at_phase("write")
                return SimpleNamespace(raw_ids=("synthetic-raw",), memories=())

        async def observe(job_id, request):
            await at_phase("observe")
            observed.append(job_id)

        evidence = SimpleNamespace(get=lambda identity: SimpleNamespace(evidence_id=identity))

        async def execute(job_id, owner):
            await execute_job(
                job_id,
                queue=queue,
                supervisor=Supervisor(),
                evidence=evidence,
                owner=owner,
                observe_sdk_outputs=observe,
            )

        drain = DurableWriteDrain(
            queue,
            execute=lambda job_id: execute(job_id, None),
            execute_owned=execute,
            max_concurrent_sources=1,
        )
        runner = run(drain, mode)

        async def govern():
            async with drain.quiesce(), gate.exclusive():
                governed.set()
                await release_governance.wait()

        maintenance = None
        next_runner = None
        try:
            await wait(blocked)
            maintenance = asyncio.create_task(govern())
            await turn()
            assert not governed.is_set()
            # Governance must not close the SDK gate while a claimed job still
            # needs a later SDK operation (including output observation).
            async with gate.operation():
                pass
            second = submit(queue, "second")
            release.set()
            await wait(governed)
            assert observed == [first.job_id]
            assert queue.get(first.request_id).state == COMPLETED
            assert queue.get(second.request_id).state == PENDING
            if mode == "one":
                next_runner = run(drain, mode)
                await turn()
                assert not next_runner.done()
                assert queue.get(second.request_id).state == PENDING
        finally:
            release.set()
            release_governance.set()
            if maintenance:
                await maintenance
            await finish(drain, runner)
            if next_runner:
                await finish(drain, next_runner)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["one", "forever"])
@pytest.mark.parametrize("cancel_claimant", [False, True])
def test_claim_commit_and_handle_registration_are_one_boundary(
    tmp_path, monkeypatch, mode, cancel_claimant
):
    async def scenario():
        queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
        job = submit(queue)
        claim_committed, return_claim = Event(), Event()
        worker_started, release_worker = asyncio.Event(), asyncio.Event()
        governed = asyncio.Event()

        async def execute(job_id):
            worker_started.set()
            await release_worker.wait()
            queue.settle(job_id, state=COMPLETED)

        drain = DurableWriteDrain(queue, execute=execute)
        original_claim = drain.claim

        def blocked_claim(**kwargs):
            ids = original_claim(**kwargs)
            if ids:
                claim_committed.set()
                assert return_claim.wait(3)
            return ids

        monkeypatch.setattr(drain, "claim", blocked_claim)
        runner = run(drain, mode)

        async def govern():
            async with drain.quiesce():
                governed.set()

        maintenance = None
        try:
            assert await asyncio.to_thread(claim_committed.wait, 3)
            maintenance = asyncio.create_task(govern())
            await turn()
            assert not governed.is_set()
            if cancel_claimant:
                for _ in range(3):
                    runner.cancel()
                    await turn()
                    assert not runner.done()
                    assert not governed.is_set()
            return_claim.set()
            await wait(worker_started)
            assert not governed.is_set()
            assert queue.get(job.request_id).state == "running"
            release_worker.set()
            await wait(governed)
            assert queue.get(job.request_id).state == COMPLETED
        finally:
            return_claim.set()
            release_worker.set()
            await finish(drain, runner)
            if maintenance:
                await maintenance

    asyncio.run(scenario())


def test_cancelled_quiesce_waiter_never_cancels_the_worker(tmp_path):
    async def scenario():
        queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
        job = submit(queue)
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def execute(job_id):
            started.set()
            await release.wait()
            queue.settle(job_id, state=COMPLETED)
            completed.set()

        drain = DurableWriteDrain(queue, execute=execute)
        runner = run(drain, "forever")

        async def govern():
            async with drain.quiesce():
                assert completed.is_set()

        try:
            await wait(started)
            maintenance = asyncio.create_task(govern())
            await turn()
            maintenance.cancel()
            with pytest.raises(asyncio.CancelledError):
                await maintenance
            assert not completed.is_set()
            assert not runner.done()
            release.set()
            await wait(completed)
            async with drain.quiesce():
                assert queue.get(job.request_id).state == COMPLETED
        finally:
            release.set()
            await finish(drain, runner)

    asyncio.run(scenario())


def test_startup_and_loop_maintenance_can_both_quiesce_without_self_await(tmp_path):
    async def scenario():
        queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
        submit(queue)
        resume_lock = asyncio.Lock()
        startup_locked, callback_entered = asyncio.Event(), asyncio.Event()
        worker_started, release_worker = asyncio.Event(), asyncio.Event()
        startup_finished, callback_finished = asyncio.Event(), asyncio.Event()

        async def execute(job_id):
            worker_started.set()
            await release_worker.wait()
            queue.settle(job_id, state=COMPLETED)

        async def callback():
            callback_entered.set()
            async with resume_lock, drain.quiesce():
                assert startup_finished.is_set()
                callback_finished.set()
                drain.stop()

        drain = DurableWriteDrain(
            queue, execute=execute, reconcile_unknown=callback, reap_interval=timedelta(0)
        )

        async def startup():
            async with resume_lock:
                startup_locked.set()
                await callback_entered.wait()
                async with drain.quiesce():
                    startup_finished.set()

        starting = asyncio.create_task(startup())
        await wait(startup_locked)
        runner = run(drain, "forever")
        try:
            await wait(worker_started)
            await wait(callback_entered)
            await turn()
            assert not startup_finished.is_set()
            assert not callback_finished.is_set()
            release_worker.set()
            await wait(callback_finished)
            await starting
        finally:
            release_worker.set()
            await finish(drain, runner)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["one", "forever"])
def test_cancelled_runner_joins_an_active_handle_after_repeated_cancellation(tmp_path, mode):
    async def scenario():
        queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
        job = submit(queue)
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def execute(job_id):
            started.set()
            await release.wait()
            queue.settle(job_id, state=COMPLETED)
            completed.set()

        drain = DurableWriteDrain(queue, execute=execute)
        runner = run(drain, mode)
        try:
            await wait(started)
            for _ in range(3):
                runner.cancel()
                await turn()
                assert not runner.done()
                assert not completed.is_set()
            release.set()
            await finish(drain, runner)
            assert completed.is_set()
            assert queue.get(job.request_id).state == COMPLETED
        finally:
            release.set()
            await finish(drain, runner)

    asyncio.run(scenario())


def test_releasing_quiescence_resumes_the_same_running_drain(tmp_path):
    async def scenario():
        queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
        completed = asyncio.Event()

        async def execute(job_id):
            queue.settle(job_id, state=COMPLETED)
            completed.set()

        drain = DurableWriteDrain(queue, execute=execute)
        async with drain.quiesce():
            job = submit(queue)
            runner = run(drain, "forever")
            await turn()
            assert drain.running
            assert queue.get(job.request_id).state == PENDING
        try:
            await wait(completed)
            assert queue.get(job.request_id).state == COMPLETED
        finally:
            await finish(drain, runner)

    asyncio.run(scenario())


def test_core_readiness_hook_only_filters_claim_and_preserves_source_authorization(
    tmp_path, monkeypatch
):
    from one_memory import api
    from one_memory.config import Settings

    ready = Event()
    core = api.create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            source_home=tmp_path / "sources",
            imports_enabled=False,
            live_sync_enabled=False,
        ),
        manage_runtime=True,
        source_write_authorizer=lambda source: True,
        sdk_claim_ready=ready.is_set,
    )

    async def scenario():
        async with core.router.lifespan_context(core):
            drain = core.state.memory_write_drain
            await asyncio.sleep(0)
            drain.stop()
            await core.state.memory_write_drain_task
            monkeypatch.setattr(api, "_sedimentation_claim_enabled", lambda app, source: True)
            job = submit(core.state.memory_write_queue)
            assert drain.claim() == ()
            assert core.state.memory_write_queue.get(job.request_id).state == PENDING
            assert core.state.memory_processing_supervisor.source_writes_permitted(
                "synthetic-source"
            )
            ready.set()
            assert drain.claim() == (job.job_id,)

    asyncio.run(scenario())
