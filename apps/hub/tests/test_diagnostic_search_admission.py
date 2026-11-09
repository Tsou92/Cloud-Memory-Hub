"""FR-10/11: native search admission joins diagnostics-producing reads."""

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from cloud_memory_hub.sdk_admission import SdkAdmission
from one_memory.api import builtin_retrieval_snapshot
from one_memory.hy_memory_bridge import HyMemoryBridge, HyMemoryBridgeError


async def search(bridge, method, query):
    kwargs = {"user_id": "synthetic-project"}
    if method == "search_memories":
        kwargs["agent_id"] = "synthetic-agent"
    return await getattr(bridge, method)(query, **kwargs)


@pytest.mark.parametrize("method", ["search_memories", "search_sdk_raw"])
def test_native_searches_remain_concurrent_but_join_maintenance(tmp_path, method):
    entered = {name: Event() for name in ("first", "second")}
    release = {name: Event() for name in entered}
    calls = []

    def native_search(query, **kwargs):
        calls.append(query)
        if query in entered:
            entered[query].set()
            assert release[query].wait(5), "test did not release search"
        return {"memories": []}

    async def scenario():
        gate = SdkAdmission()
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            client=SimpleNamespace(search=native_search),
            operation_guard=gate.operation,
        )
        maintenance_entered, maintenance_release = asyncio.Event(), asyncio.Event()

        async def maintain():
            async with gate.exclusive():
                maintenance_entered.set()
                await maintenance_release.wait()
                # The maintenance owner can read without upgrading admission.
                assert await search(bridge, method, "owner") == ()

        workers = [asyncio.create_task(search(bridge, method, name)) for name in entered]
        maintenance = None
        try:
            assert all(
                await asyncio.gather(*(asyncio.to_thread(e.wait, 2) for e in entered.values()))
            )
            maintenance = asyncio.create_task(maintain())
            await asyncio.sleep(0)
            with pytest.raises(HyMemoryBridgeError, match="maintenance_in_progress"):
                await search(bridge, method, "denied")
            assert not maintenance_entered.is_set()
            release["first"].set()
            assert await workers[0] == ()
            assert not maintenance_entered.is_set()
            release["second"].set()
            assert await workers[1] == ()
            await asyncio.wait_for(maintenance_entered.wait(), 2)
            maintenance_release.set()
            await maintenance
            async with gate.operation():
                assert await search(bridge, method, "nested") == ()
            assert set(calls) == {"first", "second", "owner", "nested"}
        finally:
            for event in release.values():
                event.set()
            maintenance_release.set()
            await asyncio.gather(
                *workers, *([maintenance] if maintenance else []), return_exceptions=True
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["search_memories", "search_sdk_raw"])
def test_cancelled_search_keeps_admission_until_native_thread_finishes(tmp_path, method):
    entered, release = Event(), Event()

    def native_search(query, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release search"
        return {"memories": []}

    async def scenario():
        gate = SdkAdmission()
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            client=SimpleNamespace(search=native_search),
            operation_guard=gate.operation,
        )
        maintenance_entered = asyncio.Event()

        async def maintain():
            async with gate.exclusive():
                maintenance_entered.set()

        worker = asyncio.create_task(search(bridge, method, "synthetic"))
        maintenance = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            maintenance = asyncio.create_task(maintain())
            for _ in range(3):
                worker.cancel()
                await asyncio.sleep(0)
                assert not worker.done()
                assert not maintenance_entered.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await worker
            await asyncio.wait_for(maintenance, 2)
            assert maintenance_entered.is_set()
            assert await search(bridge, method, "after") == ()
        finally:
            release.set()
            await asyncio.gather(
                worker, *([maintenance] if maintenance else []), return_exceptions=True
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["search_memories", "search_sdk_raw"])
def test_close_waits_for_search_and_rejects_read_to_close_upgrade(tmp_path, method):
    entered, release, closed = Event(), Event(), Event()

    def native_search(query, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release search"
        assert not closed.is_set(), "client was closed during a native search"
        return {"memories": []}

    async def scenario():
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            client=SimpleNamespace(search=native_search, close=closed.set),
            operation_guard=SdkAdmission().operation,
        )
        async with bridge.operation_access():
            with pytest.raises(HyMemoryBridgeError, match="close_upgrade_forbidden"):
                await bridge.close()
        worker = asyncio.create_task(search(bridge, method, "synthetic"))
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            closing = asyncio.create_task(bridge.close())
            await asyncio.sleep(0)
            assert not closing.done()
            assert not closed.is_set()
            with pytest.raises(HyMemoryBridgeError, match="client_closing"):
                await search(bridge, method, "denied")
            release.set()
            assert await worker == ()
            await asyncio.wait_for(closing, 2)
            assert closed.is_set()
            assert bridge._client is None
        finally:
            release.set()
            await asyncio.gather(worker, *([closing] if closing else []), return_exceptions=True)

    asyncio.run(scenario())


def test_failed_client_close_retains_identity_until_successful_retry(tmp_path):
    calls = []

    def close():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("synthetic close interruption")

    async def scenario():
        client = SimpleNamespace(search=lambda *a, **k: {"memories": []}, close=close)
        bridge = HyMemoryBridge(
            profile=builtin_retrieval_snapshot(),
            api_key=None,
            data_dir=tmp_path,
            client=client,
            operation_guard=SdkAdmission().operation,
        )
        with pytest.raises(RuntimeError, match="synthetic close interruption"):
            await bridge.close()
        assert bridge._client is client
        with pytest.raises(HyMemoryBridgeError, match="client_closing"):
            await search(bridge, "search_memories", "denied")
        with pytest.raises(HyMemoryBridgeError, match="client_closing"):
            await bridge._get_client()
        await bridge.close()
        assert bridge._client is None
        assert len(calls) == 2
        await bridge.close()
        assert len(calls) == 2

    asyncio.run(scenario())


def test_empty_recovery_keeps_sdk_lazy_without_allocating_native_stores(tmp_path, monkeypatch):
    from cloud_memory_hub.app import create_hub
    from test_original_archive import config_for

    def no_client(*args, **kwargs):
        raise AssertionError("empty recovery must not initialize native stores")

    monkeypatch.setattr("one_memory.hy_memory_bridge.create_hy_memory_client", no_client)
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            assert app.core.state.cmh_executor.ready
            assert app.core.state.cmh_executor._replayed
            assert app.core.state.cmh_executor.store.operations() == {}
        assert list(config.data_dir.rglob("chroma.sqlite3")) == []

    asyncio.run(scenario())
