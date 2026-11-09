"""ADR-0037: deleting content must preserve Chroma's write-log sequence."""

import asyncio
import sqlite3
from uuid import uuid4

import pytest
import test_chat_graph
from chromadb.api.client import SharedSystemClient
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_authority import GovernanceAuthority
from cloud_memory_hub.governance_journal import Checkpoint, GovernanceError
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.data.vector_store_chroma import ChromaVectorStore
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED, RUNNING
from test_forget_recovery import restored_forget
from test_governance_execution import EXECUTE, PREVIEW, inflight_writer, read, sdk_status
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_read import grant_reads

MARK = "Synthetic sequence original "


def chroma_path(config):
    paths = list(config.data_dir.rglob("chroma.sqlite3"))
    assert len(paths) == 1
    return paths[0]


def sequence_state(config):
    with sqlite3.connect(chroma_path(config)) as db:
        head = db.execute("SELECT max(seq_id) FROM embeddings_queue").fetchone()[0]
        watermarks = [row[0] for row in db.execute("SELECT seq_id FROM max_seq_id")]
    assert head is None or type(head) is int
    assert all(type(value) is int for value in watermarks)
    return head or 0, max(watermarks, default=0)


def snapshot(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


async def govern(web, config, event, action, selected):
    admin = {"Authorization": "Bearer " + config.admin_token}
    body = {"scope_id": event["scope_id"], "action": action, "record_ids": [selected]}
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
    assert reply.json()["state"] == "completed"


async def assert_new_write(app, web, config, monkeypatch, state, before):
    jobs, results, headers, event, untouched = state
    outputs = await inflight_writer(app, monkeypatch)
    captured = []

    async def capture(self, **kwargs):
        captured.append(kwargs["history_context"])
        return AgentResult(
            success=True,
            extracted_info={"memory": [{"content": "Synthetic inflight resurrection fact"}]},
        )

    monkeypatch.setattr(MemAgent, "process_add", capture)
    fresh = {
        **event,
        **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
        "content": {"messages": [{"role": "user", "text": "Fresh post-purge weather topic."}]},
    }
    reply = await web.post("/hub/v1/events", headers=headers, json=fresh)
    assert reply.status_code == 202, reply.text
    job = reply.json()["receipt_id"]
    queue = app.core.state.memory_write_queue
    request_id = queue.payload_for(job).idempotency_key
    queue.settle(job, state=RUNNING, hy_invoked=False)
    await asyncio.wait_for(app.core.state.memory_write_drain.execute(job), 15)
    bridge = _sdk_library_bridge(app.core)
    sdk = await bridge._get_client()
    outcome = await _joined_thread(sdk.get_write_outcome, request_id)
    assert outcome is not None and len(outputs) == 1
    raw = await bridge.get_sdk_raw(outcome["result"]["memory_id"])
    memory = await bridge.get_sdk_memory(outputs[0])
    assert raw is not None and memory is not None, "successful SDK add lost its new native IDs"
    assert queue.get(request_id).state == COMPLETED, "output observer did not complete"
    assert len(captured) == 1 and MARK + "2" in captured[0]
    assert MARK + "0" not in captured[0] and MARK + "1" not in captured[0]
    assert await sdk_status(app, results[0]["memory_id"]) == ("archived", False)
    assert await sdk_status(app, results[2]["memory_id"]) == untouched
    for index, expected in ((0, 200), (1, 404), (2, 200)):
        assert (
            await read(web, headers, event["scope_id"], "history", "cmhe:" + jobs[index])
        ).status_code == expected
    assert (
        await read(web, headers, event["scope_id"], "history", results[2]["memory_id"])
    ).status_code == 200
    # The implementation may retain content-free DELETE records, including
    # target IDs. Require monotonic progress rather than a completely empty log.
    assert before[0] >= before[1], "purge reduced queue head below its persisted watermark"
    head, watermark = sequence_state(config)
    assert head >= watermark


@pytest.mark.parametrize("reopen", [False, True])
def test_forget_then_purge_preserves_real_drain_writes_and_history(tmp_path, monkeypatch, reopen):
    config = config_for(tmp_path)
    original_accept = test_chat_graph.accepted
    count = 0

    async def unique(web, headers, event):
        nonlocal count
        event["content"] = {
            "messages": [{"role": "user", "text": f"{MARK}{count} remains distinct and traceable."}]
        }
        count += 1
        return await original_accept(web, headers, event)

    monkeypatch.setattr(test_chat_graph, "accepted", unique)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await test_chat_graph.produce(
                app, web, config, monkeypatch
            )
            await grant_reads(
                web,
                {"Authorization": "Bearer " + config.admin_token},
                event,
                permissions=("events:write", "memory:read", "history:read"),
                revision=3,
            )
            untouched = await sdk_status(app, results[2]["memory_id"])
            await govern(web, config, event, "forget", "cmhe:" + jobs[0])
            await govern(web, config, event, "purge", "cmhe:" + jobs[1])
            state = jobs, results, headers, event, untouched
            before = sequence_state(config)
            if not reopen:
                await assert_new_write(app, web, config, monkeypatch, state, before)
        if reopen:
            # Discard the client cache and open a new Core and SDK from disk.
            SharedSystemClient.clear_system_cache()
            async with fixture(config) as (app, web):
                await assert_new_write(app, web, config, monkeypatch, state, before)

    asyncio.run(scenario())


def test_legacy_regressed_sequence_refuses_startup_before_forget_updates(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        _, _, _, _, event, _, operations, checkpoint = await restored_forget(
            config, tmp_path, monkeypatch
        )
        path = chroma_path(config)
        with sqlite3.connect(path) as db:
            # Reproduce the previous cleanup of the highest queued row. The
            # segment watermark remains committed, exactly as in an old purge.
            db.execute(
                "DELETE FROM embeddings_queue "
                "WHERE seq_id=(SELECT max(seq_id) FROM embeddings_queue)"
            )
        head, watermark = sequence_state(config)
        assert head < watermark, "legacy damage fixture did not regress the log"
        before = snapshot(path)
        archive_path = config.config_dir / "remote-access.sqlite3"
        restored_archive = snapshot(archive_path)
        with sqlite3.connect(archive_path) as db:
            assert db.execute("SELECT * FROM cmh_governance_floors").fetchall() == []
        writes = []
        original_update = ChromaVectorStore.update_payload

        async def observed_update(self, identity, changes):
            writes.append(identity)
            return await original_update(self, identity, changes)

        monkeypatch.setattr(ChromaVectorStore, "update_payload", observed_update)
        reopened = create_hub(config)
        executor = reopened.core.state.cmh_executor
        archive_before = snapshot(archive_path)
        # Opening an old business backup must advance its signed anti-rollback
        # floor. Prove that this is the constructor's only archive DB change.
        prefix = 'INSERT INTO "cmh_governance_floors" '
        assert tuple(row for row in archive_before if not row.startswith(prefix)) == tuple(
            row for row in restored_archive if not row.startswith(prefix)
        )
        with sqlite3.connect(archive_path) as db:
            floors = db.execute("SELECT * FROM cmh_governance_floors").fetchall()
        assert len(floors) == 1
        scope, payload, signature = floors[0]
        assert scope == event["scope_id"]
        assert Checkpoint.model_validate_json(payload) == checkpoint
        assert signature == GovernanceAuthority._signature(
            executor.authority.journal, "floor", [scope, payload]
        )
        with pytest.raises(GovernanceError, match="^governance_chroma_sequence_invalid$"):
            async with reopened.core.router.lifespan_context(reopened.core):
                pytest.fail("a regressed Chroma log must not resume normal service")
        assert not executor.ready and not executor._replayed
        assert writes == [], "startup mutated forgotten nodes before checking sequence integrity"
        assert snapshot(path) == before
        assert snapshot(archive_path) == archive_before
        assert executor.store.operations() == operations
        assert executor.authority.journal.head(event["scope_id"]) == checkpoint

    asyncio.run(scenario())
