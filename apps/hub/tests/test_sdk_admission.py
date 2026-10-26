import asyncio
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.sdk_admission import SdkAdmission
from httpx import ASGITransport, AsyncClient
from one_memory.api import _sdk_library_bridge, builtin_retrieval_snapshot
from one_memory.hy_memory_bridge import HyMemoryBridge, HyMemoryBridgeError
from test_remote_ingest import config_for


async def operation(gate):
    async with gate.operation():
        return "admitted"


async def exclusive(gate, entered, release):
    async with gate.exclusive():
        entered.set()
        await release.wait()


def test_concurrent_operations_drain_and_task_identity_controls_reentry():
    async def scenario():
        gate = SdkAdmission()
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        maintenance_entered, maintenance_release = asyncio.Event(), asyncio.Event()

        async def work(index):
            async with gate.operation():
                entered[index].set()
                await release[index].wait()
                # An already admitted Task may finish nested work during draining.
                assert await operation(gate) == "admitted"

        workers = [asyncio.create_task(work(i)) for i in range(2)]
        await asyncio.gather(*(event.wait() for event in entered))
        maintenance = asyncio.create_task(exclusive(gate, maintenance_entered, maintenance_release))
        await asyncio.sleep(0)
        with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
            await operation(gate)
        release[0].set()
        await workers[0]
        assert not maintenance_entered.is_set()
        release[1].set()
        await workers[1]
        await maintenance_entered.wait()
        with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
            await operation(gate)
        maintenance_release.set()
        await maintenance
        assert await operation(gate) == "admitted"
        async with gate.exclusive():
            assert await operation(gate) == "admitted"
            with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                await asyncio.create_task(operation(gate))

    asyncio.run(scenario())


def test_upgrade_and_nested_or_competing_maintenance_fail_without_deadlock():
    async def attempt(gate):
        async with gate.exclusive():
            pytest.fail("must not enter")

    async def scenario():
        gate = SdkAdmission()
        async with gate.operation():
            with pytest.raises(HyMemoryBridgeError, match="upgrade_forbidden"):
                await attempt(gate)
        async with gate.exclusive():
            with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                await attempt(gate)
            with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                await asyncio.create_task(attempt(gate))
        assert await operation(gate) == "admitted"

    asyncio.run(scenario())


def test_cancelling_waiting_maintenance_does_not_cancel_or_forget_active_work():
    async def scenario():
        gate = SdkAdmission()
        entered, release = asyncio.Event(), asyncio.Event()

        async def worker():
            async with gate.operation():
                entered.set()
                await release.wait()

        work = asyncio.create_task(worker())
        await entered.wait()
        maintenance = asyncio.create_task(exclusive(gate, asyncio.Event(), asyncio.Event()))
        await asyncio.sleep(0)
        maintenance.cancel()
        with pytest.raises(asyncio.CancelledError):
            await maintenance
        assert not work.done()
        assert await operation(gate) == "admitted"
        entered_again = asyncio.Event()
        maintenance_release = asyncio.Event()
        again = asyncio.create_task(exclusive(gate, entered_again, maintenance_release))
        await asyncio.sleep(0)
        assert not entered_again.is_set()
        release.set()
        await work
        await entered_again.wait()
        maintenance_release.set()
        await again

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_exclusive_body_failure_restores_admission(cancel):
    async def scenario():
        gate = SdkAdmission()
        if cancel:
            entered = asyncio.Event()
            task = asyncio.create_task(exclusive(gate, entered, asyncio.Event()))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ValueError, match="synthetic"):
                async with gate.exclusive():
                    raise ValueError("synthetic")
        assert await operation(gate) == "admitted"

    asyncio.run(scenario())


def test_cross_loop_use_is_explicitly_rejected():
    gate = SdkAdmission()
    assert asyncio.run(operation(gate)) == "admitted"
    with pytest.raises(HyMemoryBridgeError, match="admission_loop_mismatch"):
        asyncio.run(operation(gate))


def test_bridge_cancelled_worker_and_local_lock_waiter_remain_accounted(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def get(identity):
        calls.append(identity)
        entered.set()
        assert release.wait(5), "test did not release worker"
        return None

    async def scenario():
        gate = SdkAdmission()
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            client=SimpleNamespace(get=get),
            operation_guard=gate.operation,
        )
        first = asyncio.create_task(bridge.get_sdk_memory("first"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(bridge.get_sdk_memory("waiting"))
            await asyncio.sleep(0)
            maintenance_entered, maintenance_release = asyncio.Event(), asyncio.Event()
            maintenance = asyncio.create_task(
                exclusive(gate, maintenance_entered, maintenance_release)
            )
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            for _ in range(3):
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done()
                assert not maintenance_entered.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await maintenance_entered.wait()
            assert calls == ["first"]
            with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                await bridge.get_sdk_memory("denied")
            maintenance_release.set()
            await maintenance
            assert await bridge.get_sdk_memory("after") is None
            assert calls == ["first", "after"]
        finally:
            release.set()

    asyncio.run(scenario())


def test_initialization_thread_is_included_in_drain_after_cancellation(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def initialize(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("one_memory.hy_memory_bridge.create_hy_memory_client", initialize)

    async def scenario():
        gate = SdkAdmission()
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            operation_guard=gate.operation,
        )
        prepare = asyncio.create_task(bridge.prepare())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            maintenance_entered, maintenance_release = asyncio.Event(), asyncio.Event()
            maintenance = asyncio.create_task(
                exclusive(gate, maintenance_entered, maintenance_release)
            )
            for _ in range(2):
                prepare.cancel()
                await asyncio.sleep(0)
                assert not prepare.done()
                assert not maintenance_entered.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await prepare
            await maintenance_entered.wait()
            maintenance_release.set()
            await maintenance
            await bridge.close()
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("prepare", ()),
        ("warm", ()),
        ("get_sdk_memory", ("x",)),
        ("update", ("x", "synthetic")),
        ("delete", ("x",)),
        ("close", ()),
    ],
)
def test_all_bridge_entry_kinds_reject_before_sdk_access(tmp_path, method, args):
    async def scenario():
        gate = SdkAdmission()
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            # No methods: reaching the client would fail with a different error.
            client=SimpleNamespace(),
            operation_guard=gate.operation,
        )
        async with gate.exclusive():
            with pytest.raises(HyMemoryBridgeError, match="^hy_memory_maintenance_in_progress$"):
                await asyncio.create_task(getattr(bridge, method)(*args))

    asyncio.run(scenario())


def test_actual_core_factories_share_admission_and_real_sdk_stays_usable(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            gate = app.core.state.cmh_sdk_admission
            reader = _sdk_library_bridge(app.core)
            supervisor = app.core.state.memory_processing_supervisor
            writer = supervisor._bridge(builtin_retrieval_snapshot(), None, config.data_dir)
            scoped_reader = supervisor._read_bridge(
                builtin_retrieval_snapshot(), None, config.data_dir
            )
            assert reader is not writer and scoped_reader is not reader
            # Factories must gate even initialization of a new client.
            async with gate.exclusive():
                for bridge in (reader, writer, scoped_reader):
                    with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                        await asyncio.create_task(bridge.prepare())
            identity = str(uuid4())
            result = await writer.import_sdk_memory_exact(
                content="Synthetic admission integration memory",
                memory_id=identity,
                user_id="local",
                agent_id="synthetic",
                session_id="synthetic",
                layer="l2_fact",
                status="active",
                owner=None,
                tags=(),
                supersedes=(),
                superseded_by=(),
                is_latest=True,
                request_id=str(uuid4()),
            )
            assert result["success"] is True
            assert (await reader.get_sdk_memory(identity)).content.startswith("Synthetic")
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                auth = {"Authorization": "Bearer " + config.admin_token}
                path = f"/api/v2/memories/{identity}"
                assert (await web.get(path, headers=auth)).status_code == 200
                async with gate.exclusive():
                    # The maintenance Task can inspect; child requests cannot borrow ownership.
                    assert (await reader.get_sdk_memory(identity)).memory_id == identity
                    for bridge in (reader, writer, scoped_reader):
                        with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                            await asyncio.create_task(bridge.get_sdk_memory(identity))
                    denied = await asyncio.create_task(web.get(path, headers=auth))
                    assert denied.status_code == 503
                    assert denied.json() == {"detail": "hy_memory_maintenance_in_progress"}
                    assert "Synthetic" not in denied.text
                assert (await web.get(path, headers=auth)).status_code == 200
            assert (await scoped_reader.get_sdk_memory(identity)).memory_id == identity
        # Actual lifespan closed all bridges after the barrier had been released.

    asyncio.run(scenario())
