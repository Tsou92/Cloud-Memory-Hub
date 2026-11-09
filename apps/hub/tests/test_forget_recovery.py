"""ADR-0032: restored completed forgets stay out of actual SDK model inputs."""

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import test_chat_graph
from chromadb.api.client import SharedSystemClient
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_journal import GovernanceError
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.data.vector_store_chroma import ChromaVectorStore
from one_memory.api import builtin_retrieval_snapshot
from one_memory.hy_memory_bridge import HyMemoryBridge, _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED, RUNNING
from test_governance_execution import EXECUTE, PREVIEW, inflight_writer, read, sdk_status
from test_original_archive import config_for, stop_drain
from test_output_lineage import fixture
from test_remote_read import grant_reads

MARK = "SYNTHETIC_FORGET_RESTORE_"


@asynccontextmanager
async def sdk_only(config):
    bridge = HyMemoryBridge(
        profile=builtin_retrieval_snapshot(), api_key=None, data_dir=config.data_dir
    )
    try:
        yield await bridge._get_client()
    finally:
        await bridge.close()


async def node_for(sdk, identity):
    return await _joined_thread(lambda: sdk._loop_thread.run(sdk._vector_store.get_by_id(identity)))


async def restored_forget(config, tmp_path, monkeypatch, *, restore=True):
    original_accept = test_chat_graph.accepted
    events = []

    async def unique(web, headers, event):
        event["content"] = {
            "messages": [
                {
                    "role": "user",
                    "text": f"{MARK}{len(events)} original retained for explicit read.",
                }
            ]
        }
        events.append(json.loads(json.dumps(event)))
        return await original_accept(web, headers, event)

    monkeypatch.setattr(test_chat_graph, "accepted", unique)
    async with fixture(config) as (app, web):
        jobs, requests, results, headers, event, _ = await test_chat_graph.produce(
            app, web, config, monkeypatch
        )
        admin = {"Authorization": "Bearer " + config.admin_token}
        await grant_reads(
            web,
            admin,
            event,
            permissions=("events:write", "memory:read", "history:read"),
            revision=3,
        )
        untouched = {
            row["memory_id"]: await sdk_status(app, row["memory_id"]) for row in results[1:]
        }
    backup = tmp_path / "before-forget"
    for name, path in (("data", config.data_dir), ("config", config.config_dir)):
        shutil.copytree(path, backup / name)
    async with fixture(config) as (app, web):
        body = {
            "scope_id": event["scope_id"],
            "action": "forget",
            "record_ids": ["cmhe:" + jobs[0]],
        }
        viewed = await web.post(PREVIEW, headers=admin, json=body)
        assert viewed.status_code == 200, viewed.text
        reply = await web.post(
            EXECUTE,
            headers=admin,
            json={
                **body,
                "operation_id": str(uuid4()),
                "expected_view": viewed.json()["view_digest"],
            },
        )
        assert reply.status_code == 200, reply.text
        executor = app.core.state.cmh_executor
        operations = executor.store.operations()
        checkpoint = executor.authority.journal.head(event["scope_id"])
        assert await sdk_status(app, results[0]["memory_id"]) == ("archived", False)
        assert await sdk_status(app, results[0]["memory_ids"][0]) == ("archived", False)
    if restore:
        for name, path in (("data", config.data_dir), ("config", config.config_dir)):
            shutil.rmtree(path)
            shutil.copytree(backup / name, path)
        SharedSystemClient.clear_system_cache()
    return jobs, requests, results, headers, event, untouched, operations, checkpoint


@pytest.mark.parametrize("restore", [False, True])
def test_completed_forget_replays_before_real_drain_history_and_is_idempotent(
    tmp_path, monkeypatch, restore
):
    config = config_for(tmp_path)

    async def scenario():
        jobs, _, results, headers, event, untouched, operations, checkpoint = await restored_forget(
            config, tmp_path, monkeypatch, restore=restore
        )
        for _ in range(2):
            async with fixture(config) as (app, web):
                outputs = await inflight_writer(app, monkeypatch)
                captured = []

                async def capture(self, _captured=captured, **kwargs):
                    _captured.append(kwargs["history_context"])
                    return AgentResult(
                        success=True,
                        extracted_info={"memory": [{"content": "Synthetic fresh unrelated fact"}]},
                    )

                monkeypatch.setattr(MemAgent, "process_add", capture)
                fresh = {
                    **event,
                    **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
                    "content": {
                        "messages": [{"role": "user", "text": "Fresh weather discussion."}]
                    },
                }
                reply = await web.post("/hub/v1/events", headers=headers, json=fresh)
                assert reply.status_code == 202, reply.text
                job = reply.json()["receipt_id"]
                queue = app.core.state.memory_write_queue
                request_id = queue.payload_for(job).idempotency_key
                queue.settle(job, state=RUNNING, hy_invoked=False)
                await asyncio.wait_for(app.core.state.memory_write_drain.execute(job), 15)
                assert queue.get(request_id).state == COMPLETED and len(outputs) == 1
                assert len(captured) == 1
                assert MARK + "0" not in captured[0], "forgotten source re-entered model history"
                assert MARK + "1" in captured[0] and MARK + "2" in captured[0]
                for identity in (results[0]["memory_id"], results[0]["memory_ids"][0]):
                    assert await sdk_status(app, identity) == ("archived", False)
                for identity, expected in untouched.items():
                    assert await sdk_status(app, identity) == expected
                assert (
                    await read(web, headers, event["scope_id"], "history", "cmhe:" + jobs[0])
                ).status_code == 200
                assert (
                    await read(web, headers, event["scope_id"], "history", results[0]["memory_id"])
                ).status_code == 200
                assert (
                    await read(
                        web, headers, event["scope_id"], "memories", results[0]["memory_ids"][0]
                    )
                ).status_code == 404
                executor = app.core.state.cmh_executor
                assert executor.ready and executor._replayed
                assert executor.store.operations() == operations
                assert executor.authority.journal.head(event["scope_id"]) == checkpoint

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["return_false", "raise"])
def test_failed_restore_suppression_keeps_service_unready(tmp_path, monkeypatch, fault):
    config = config_for(tmp_path)

    async def scenario():
        jobs, _, results, _, event, _, operations, checkpoint = await restored_forget(
            config, tmp_path, monkeypatch
        )
        target = results[0]["memory_id"]
        original_update = ChromaVectorStore.update_payload

        async def fail(self, identity, values):
            if identity == target:
                if fault == "raise":
                    raise RuntimeError("synthetic private SDK payload failure")
                return False
            return await original_update(self, identity, values)

        with monkeypatch.context() as injection:
            injection.setattr(ChromaVectorStore, "update_payload", fail)
            reopened = create_hub(config)
            with pytest.raises(GovernanceError, match="^governance_suppression_incomplete$"):
                async with reopened.core.router.lifespan_context(reopened.core):
                    pytest.fail("suppression failure must block startup")
        executor = reopened.core.state.cmh_executor
        assert not executor.ready and not executor._replayed
        assert executor.store.operations() == operations
        assert executor.authority.journal.head(event["scope_id"]) == checkpoint
        with reopened.core.state.cmh_ingest._database() as db:
            assert db.execute(
                "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?", (jobs[0],)
            ).fetchone()
        async with sdk_only(config) as sdk:
            node = await node_for(sdk, target)
            assert (node.status.value, node.is_latest) == ("shadow", True)
        # Removing the transient fault permits the same signed operation to recover.
        async with fixture(config) as (app, _):
            assert app.core.state.cmh_executor.ready
            assert await sdk_status(app, target) == ("archived", False)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,error",
    [
        ("user_id", "governance_scope_mismatch"),
        ("node_id", "governance_record_identity_mismatch"),
        ("unreadable", "governance_suppression_incomplete"),
    ],
)
def test_restore_never_mutates_foreign_or_damaged_target(tmp_path, monkeypatch, field, error):
    config = config_for(tmp_path)

    async def scenario():
        _, _, results, _, event, _, operations, _ = await restored_forget(
            config, tmp_path, monkeypatch
        )
        target = results[0]["memory_id"]
        changed = {
            "user_id": f"cmh:{event['instance_id']}:{uuid4()}",
            "node_id": str(uuid4()),
            "unreadable": None,
        }[field]
        async with sdk_only(config) as sdk:
            if field != "unreadable":
                assert await _joined_thread(
                    lambda: sdk._loop_thread.run(
                        sdk._vector_store.update_payload(target, {field: changed})
                    )
                )
            vector = sdk._vector_store
            before = await _joined_thread(
                vector._collection.get,
                ids=[vector._node_id_to_point_id(target)],
                include=["metadatas", "documents"],
            )
        with monkeypatch.context() as injection:
            if field == "unreadable":
                original_get = ChromaVectorStore.get_by_id

                async def swallowed_error(self, identity):
                    # The native SDK returns None on storage/decode failure.
                    return None if identity == target else await original_get(self, identity)

                injection.setattr(ChromaVectorStore, "get_by_id", swallowed_error)
            reopened = create_hub(config)
            with pytest.raises(GovernanceError, match="^" + error + "$"):
                async with reopened.core.router.lifespan_context(reopened.core):
                    pytest.fail("unproven target identity must block startup")
        assert not reopened.core.state.cmh_executor.ready
        assert not reopened.core.state.cmh_executor._replayed
        assert reopened.core.state.cmh_executor.store.operations() == operations
        async with sdk_only(config) as sdk:
            vector = sdk._vector_store
            after = await _joined_thread(
                vector._collection.get,
                ids=[vector._node_id_to_point_id(target)],
                include=["metadatas", "documents"],
            )
            assert after == before, "foreign or damaged node was modified"

    asyncio.run(scenario())


@pytest.mark.parametrize("purger_available", [True, False])
def test_missing_target_is_skipped_and_forget_replay_does_not_require_purger(
    tmp_path, monkeypatch, purger_available
):
    config = config_for(tmp_path)

    async def scenario():
        _, _, results, _, _, _, operations, _ = await restored_forget(config, tmp_path, monkeypatch)
        missing = results[0]["memory_ids"][0]
        async with sdk_only(config) as sdk:
            assert await _joined_thread(
                lambda: sdk._loop_thread.run(sdk._vector_store.delete(missing))
            )
        reopened = create_hub(config)
        executor = reopened.core.state.cmh_executor
        if not purger_available:
            executor.purger = None
        async with reopened.core.router.lifespan_context(reopened.core):
            await stop_drain(reopened)
            assert executor.ready
            assert executor._replayed is purger_available
            assert await sdk_status(reopened, results[0]["memory_id"]) == ("archived", False)
            assert executor.store.operations() == operations
        async with sdk_only(config) as sdk:
            assert await node_for(sdk, missing) is None

    asyncio.run(scenario())
