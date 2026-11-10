"""ADR-0065: transport cleanup preserves the existing Hub lifecycle contract."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.remote_mcp import RemoteMCP
from starlette.applications import Starlette
from test_original_archive import config_for


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    # Control Core scheduling, but use the actual MCP task group and composition.
    events, managers = [], []
    initialize = RemoteMCP.__init__

    def capture_manager(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        managers.append(self.session_manager)

    @asynccontextmanager
    async def core_lifespan(app):
        events.append("core_enter")
        try:
            yield {"synthetic": True}
        finally:
            assert managers[0]._task_group is None
            events.append("core_exit")

    monkeypatch.setattr(RemoteMCP, "__init__", capture_manager)
    monkeypatch.setattr(
        "one_memory.api.create_app", lambda *args, **kwargs: Starlette(lifespan=core_lifespan)
    )
    app = create_hub(config_for(tmp_path))
    executor = app.core.state.cmh_executor

    async def resume():
        assert managers[0]._task_group is not None
        events.append("resume")
        await asyncio.sleep(0)
        executor.ready = True

    monkeypatch.setattr(executor, "resume", resume)
    return SimpleNamespace(app=app, manager=managers[0], executor=executor, events=events)


def test_transport_starts_before_final_recovery_and_closes_before_core(lifecycle):
    async def scenario():
        core = lifecycle.app.core
        async with core.router.lifespan_context(core) as state:
            assert lifecycle.executor.ready
            assert state == {"synthetic": True}
            lifecycle.events.append("serving")
        assert lifecycle.events == ["core_enter", "resume", "serving", "core_exit"]

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["startup", "body"])
@pytest.mark.parametrize("kind", ["governance", "group", "cancel"])
def test_hub_errors_keep_their_original_identity(lifecycle, monkeypatch, phase, kind):
    failure = {
        "governance": GovernanceError("synthetic_recovery_failure"),
        "group": ExceptionGroup("synthetic_existing_group", [ValueError("a"), OSError("b")]),
        "cancel": asyncio.CancelledError("synthetic_cancel"),
    }[kind]

    async def fail_resume():
        assert lifecycle.manager._task_group is not None
        raise failure

    if phase == "startup":
        monkeypatch.setattr(lifecycle.executor, "resume", fail_resume)

    async def scenario():
        core = lifecycle.app.core
        with pytest.raises(type(failure)) as observed:
            async with core.router.lifespan_context(core):
                assert phase == "body", "failed startup must not serve"
                raise failure
        assert observed.value is failure
        assert lifecycle.manager._task_group is None
        assert lifecycle.events[-1] == "core_exit"

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["startup", "body"])
def test_task_cancellation_propagates_after_transport_cleanup(lifecycle, monkeypatch, phase):
    async def scenario():
        entered = asyncio.Event()

        async def wait_for_cancel():
            entered.set()
            await asyncio.Event().wait()

        if phase == "startup":
            monkeypatch.setattr(lifecycle.executor, "resume", wait_for_cancel)

        async def run():
            core = lifecycle.app.core
            async with core.router.lifespan_context(core):
                assert phase == "body"
                await wait_for_cancel()

        pending = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert lifecycle.manager._task_group is None
        assert lifecycle.events[-1] == "core_exit"

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("body_fails", [False, True])
def test_transport_failure_groups_are_preserved(lifecycle, count, body_fails):
    failures = [RuntimeError(f"synthetic_transport_{index}") for index in range(count)]
    body_failure = GovernanceError("synthetic_body_failure")

    async def worker(failure, *, task_status=anyio.TASK_STATUS_IGNORED):
        try:
            task_status.started()
            await anyio.sleep_forever()
        finally:
            raise failure

    async def scenario():
        core = lifecycle.app.core
        with pytest.raises(ExceptionGroup) as observed:
            async with core.router.lifespan_context(core):
                for failure in failures:
                    await lifecycle.manager._task_group.start(worker, failure)
                if body_fails:
                    raise body_failure
        assert set(observed.value.exceptions) == set(failures)
        if body_fails:
            assert observed.value.__context__ is body_failure
        assert lifecycle.manager._task_group is None
        assert lifecycle.events[-1] == "core_exit"

    asyncio.run(scenario())
