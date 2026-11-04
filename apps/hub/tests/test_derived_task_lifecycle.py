import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cloud_memory_hub.sdk_admission import SdkAdmission
from hy_memory.client import HyMemoryClient
from hy_memory.config import CacheConfig, MemoryConfig
from hy_memory.data.cache_sqlite import SqliteCache
from hy_memory.pipelines.base import WriteRequest
from hy_memory.pipelines.writer import MemoryWriter


def writer_for(monkeypatch, summaries, *, reconcile_error=None):
    import hy_memory.agent.episode_summary as episode
    import hy_memory.pipelines.writer as module

    writer = MemoryWriter.__new__(MemoryWriter)
    writer.config = MemoryConfig()
    writer.config.recall.episode_summary_enabled = True
    writer._mem_agent = SimpleNamespace(llm_provider=object())
    writer._embed_service = object()
    writer._cache = None
    writer._graph_store = None
    writer._get_vector_store = AsyncMock(return_value=object())
    writer._emit_write_timeline = AsyncMock()

    async def summarize(**kwargs):
        entered, release, completed = summaries[kwargs["episode_id"]]
        entered.set()
        await release.wait()
        completed.set()
        return "synthetic-summary"

    async def reconcile(*, request, **kwargs):
        writer._schedule_episode_summary(request, request.request_id)
        await summaries[request.request_id][0].wait()
        if reconcile_error:
            raise RuntimeError("synthetic reconcile error")
        return ["synthetic-fact"], None, {"add": 1}, {}

    writer._reconcile_and_store = reconcile
    monkeypatch.setattr(module, "_RECONCILE_ENABLED", True)
    monkeypatch.setattr(episode, "summarize_closed_episode", summarize)
    return writer


def request(identity="synthetic-request", *, valid=True):
    return WriteRequest(
        content="Synthetic fact" if valid else "",
        user_id="synthetic-user",
        agent_id="synthetic-agent",
        session_id="synthetic-session",
        request_id=identity,
    )


def events():
    return asyncio.Event(), asyncio.Event(), asyncio.Event()


@pytest.mark.parametrize("failure", [False, True])
def test_extracted_writer_does_not_return_while_summary_can_still_write(monkeypatch, failure):
    async def scenario():
        entered, release, completed = events()
        writer = writer_for(
            monkeypatch,
            {"synthetic-request": (entered, release, completed)},
            reconcile_error=failure,
        )
        job = asyncio.create_task(writer.write_extracted(request()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(0)
            assert not job.done(), "writer returned before its derived write completed"
        finally:
            release.set()
            result = await job
        assert completed.is_set() and result.success is not failure

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["write", "write_extracted"])
def test_early_return_also_joins_previously_scheduled_work(monkeypatch, method):
    async def scenario():
        entered, release, completed = events()
        writer = writer_for(monkeypatch, {"synthetic-request": (entered, release, completed)})
        item = request(valid=False)
        writer._schedule_episode_summary(item, item.request_id)
        await entered.wait()
        job = asyncio.create_task(getattr(writer, method)(item))
        try:
            await asyncio.sleep(0)
            assert not job.done(), "early return escaped derived task lifetime"
        finally:
            release.set()
            result = await job
        assert completed.is_set() and result.error_code == 400

    asyncio.run(scenario())


def test_repeated_cancellation_keeps_maintenance_waiting_for_actual_completion(monkeypatch):
    async def scenario():
        entered, release, completed = events()
        writer = writer_for(monkeypatch, {"synthetic-request": (entered, release, completed)})
        gate, maintenance_entered = SdkAdmission(), asyncio.Event()
        item = request()

        async def write():
            async with gate.operation():
                return await writer.write_extracted(item)

        async def maintenance():
            async with gate.exclusive():
                maintenance_entered.set()
                assert completed.is_set()

        job = asyncio.create_task(write())
        await entered.wait()
        waiting = asyncio.create_task(maintenance())
        try:
            for _ in range(3):
                await asyncio.sleep(0)
                job.cancel()
                await asyncio.sleep(0)
                assert not job.done() and not completed.is_set()
                assert not maintenance_entered.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await job
            await waiting
        assert completed.is_set() and maintenance_entered.is_set()
        assert not hasattr(item, "_episode_summary_tasks")
        await writer._await_derived_tasks(item)

    asyncio.run(scenario())


@pytest.mark.parametrize("graph_failure", ["error", "cancel"])
def test_failed_graph_does_not_abandon_the_other_derived_write(monkeypatch, graph_failure):
    async def scenario():
        entered, release, completed = events()
        graph_entered, graph_release, _ = events()
        writer = writer_for(monkeypatch, {"synthetic-request": (entered, release, completed)})
        item = request()

        async def graph():
            graph_entered.set()
            await graph_release.wait()
            raise RuntimeError("synthetic graph failure")

        item._graph_projection_task = asyncio.create_task(graph())
        graph_job = item._graph_projection_task
        job = asyncio.create_task(writer.write_extracted(item))
        await entered.wait()
        await graph_entered.wait()
        try:
            if graph_failure == "cancel":
                graph_job.cancel()
            else:
                graph_release.set()
            for _ in range(3):
                await asyncio.sleep(0)
            assert graph_job.done() and not job.done() and not completed.is_set()
        finally:
            release.set()
            if graph_failure == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await job
            else:
                assert (await job).success
        assert completed.is_set()
        assert not hasattr(item, "_graph_projection_task")
        assert not hasattr(item, "_episode_summary_tasks")

    asyncio.run(scenario())


def test_multiple_summaries_remain_owned_by_one_request(monkeypatch):
    async def scenario():
        summaries = {identity: events() for identity in ("one", "two")}
        writer = writer_for(monkeypatch, summaries)
        item = request(valid=False)
        for identity in summaries:
            writer._schedule_episode_summary(item, identity)
        await asyncio.gather(*(entry[0].wait() for entry in summaries.values()))
        job = asyncio.create_task(writer.write(item))
        try:
            summaries["one"][1].set()
            await summaries["one"][2].wait()
            await asyncio.sleep(0)
            assert not job.done()
        finally:
            summaries["two"][1].set()
            assert (await job).error_code == 400
        assert all(entry[2].is_set() for entry in summaries.values())

    asyncio.run(scenario())


def test_same_writer_does_not_wait_for_another_requests_summary(monkeypatch):
    async def scenario():
        summaries = {identity: events() for identity in ("one", "two")}
        writer = writer_for(monkeypatch, summaries)
        jobs = {
            identity: asyncio.create_task(writer.write_extracted(request(identity)))
            for identity in summaries
        }
        await asyncio.gather(*(entry[0].wait() for entry in summaries.values()))
        try:
            summaries["one"][1].set()
            assert (await asyncio.wait_for(jobs["one"], 2)).success
            assert not jobs["two"].done() and not summaries["two"][2].is_set()
        finally:
            for entry in summaries.values():
                entry[1].set()
            await asyncio.gather(*jobs.values())

    asyncio.run(scenario())


def test_disabled_summary_does_not_schedule_or_change_the_request(monkeypatch):
    async def scenario():
        writer = writer_for(monkeypatch, {})
        writer.config.recall.episode_summary_enabled = False
        item = request(valid=False)
        writer._schedule_episode_summary(item, "disabled")
        assert not hasattr(item, "_episode_summary_tasks")
        assert (await writer.write(item)).error_code == 400

    asyncio.run(scenario())


def test_early_graph_join_defers_repeated_cancellation_and_can_be_joined_again():
    async def scenario():
        entered, release, completed = events()
        item = request()

        async def project():
            entered.set()
            await release.wait()
            completed.set()

        item._graph_projection_task = asyncio.create_task(project())
        graph = item._graph_projection_task
        await entered.wait()
        join = asyncio.create_task(MemoryWriter._await_graph_projection(item))
        try:
            await asyncio.sleep(0)
            for _ in range(3):
                join.cancel()
                await asyncio.sleep(0)
                assert not join.done() and not graph.done()
                assert item._graph_projection_task is graph
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await join
        assert completed.is_set() and not graph.cancelled()
        assert not hasattr(item, "_graph_projection_task")
        await MemoryWriter._await_graph_projection(item)
        await MemoryWriter._await_graph_projection(None)

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["add", "add_extracted"])
def test_native_cancellation_defers_unknown_until_tasks_finish_and_prevents_replay(
    monkeypatch, tmp_path, method
):
    async def scenario():
        entered, release, completed = events()
        writer = writer_for(monkeypatch, {"synthetic-request": (entered, release, completed)})
        config = MemoryConfig()
        config.cache = CacheConfig(backend="sqlite", db_path=str(tmp_path / "cache.db"))
        cache = SqliteCache(config)
        await cache.initialize()
        client = HyMemoryClient.__new__(HyMemoryClient)
        client._cache = cache
        calls = []

        async def write(*args, **kwargs):
            calls.append(True)
            response = await writer.write_extracted(request(kwargs["request_id"]))
            return {"success": response.success, "request_id": kwargs["request_id"]}

        setattr(client, "_async_" + method + "_impl", write)
        native = getattr(HyMemoryClient, "async_" + method).__wrapped__
        args = dict(user_id="synthetic-user", request_id="synthetic-request")
        job = asyncio.create_task(native(client, "Synthetic fact", **args))
        try:
            await entered.wait()
            for _ in range(3):
                job.cancel()
                await asyncio.sleep(0)
                assert not job.done()
                assert (await cache.get_write_outcome(args["request_id"]))["status"] == "running"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await job
        assert completed.is_set()
        assert (await cache.get_write_outcome(args["request_id"]))["status"] == "unknown"
        replay = await native(client, "Synthetic fact", **args)
        assert replay["replayed"] and replay["outcome_status"] == "unknown"
        assert calls == [True]
        await cache.close()

    asyncio.run(scenario())
