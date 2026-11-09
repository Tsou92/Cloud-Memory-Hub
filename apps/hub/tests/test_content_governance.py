"""FR-10/FR-11: complete HTTP/SDK governance prevents changed-ID reimports."""

import asyncio
import copy
import sqlite3
from uuid import uuid4

import pytest
import test_chat_graph
from cloud_memory_hub import governance_execution
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_sources import (
    capture_sources,
    content_policy_target,
    content_targets,
)
from test_chat_graph import produce
from test_governance_execution import EXECUTE, PREVIEW, read
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_ingest import provision
from test_remote_read import grant_reads


async def produce_long_originals(app, web, config, monkeypatch):
    original_accept = test_chat_graph.accepted
    count = 0

    async def long_original(web, headers, event):
        nonlocal count
        count += 1
        event["content"] = {
            "messages": [
                {
                    "role": "user",
                    "text": f"Synthetic protected original number {count} must not revive.",
                }
            ]
        }
        return await original_accept(web, headers, event)

    monkeypatch.setattr(test_chat_graph, "accepted", long_original)
    result = await produce(app, web, config, monkeypatch)
    event = result[4]
    await grant_reads(
        web,
        {"Authorization": "Bearer " + config.admin_token},
        event,
        permissions=("events:write", "memory:read", "history:read"),
        revision=3,
    )
    return result


async def execute(web, config, event, identity, action):
    admin = {"Authorization": "Bearer " + config.admin_token}
    body = {"scope_id": event["scope_id"], "action": action, "record_ids": [identity]}
    view = await web.post(PREVIEW, headers=admin, json=body)
    assert view.status_code == 200, view.text
    result = await web.post(
        EXECUTE,
        headers=admin,
        json={**body, "expected_view": view.json()["view_digest"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "completed"
    return result.json()


async def register_source(web, config, event):
    reply = await web.post(
        "/hub/v1/sources",
        headers={"Authorization": "Bearer " + config.admin_token},
        json={
            "display_name": "Reinstalled synthetic agent",
            "adapter_id": "synthetic.reimport",
            "device_id": event["device_id"],
            "scope_id": event["scope_id"],
        },
    )
    assert reply.status_code == 201, reply.text
    return reply.json()["source_id"]


def reimport(event, source, *, mixed=False, text=None):
    candidate = copy.deepcopy(event)
    candidate.update({key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")})
    candidate["source_id"] = source
    if text is not None:
        candidate["content"] = {"messages": [{"role": "user", "text": text}]}
    if mixed:
        candidate["content"]["messages"].insert(
            0, {"role": "user", "text": "A newly written sentence must not disguise old content."}
        )
    return candidate


def queue_count(config):
    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
        return db.execute("SELECT COUNT(*) FROM memory_write_jobs").fetchone()[0]


async def rejected_without_enqueue(app, web, config, headers, event, detail):
    before = queue_count(config)
    ingest = app.core.state.cmh_ingest
    with ingest._database() as db:
        original_count = db.execute("SELECT COUNT(*) FROM cmh_event_receipts").fetchone()[0]
    reply = await web.post("/hub/v1/events", headers=headers, json=event)
    assert reply.status_code == 409, reply.text
    assert reply.json() == {"detail": detail}
    request_id = f"cmh:{event['instance_id']}:{event['device_id']}:{event['event_id']}"
    assert app.core.state.memory_write_queue.get(request_id) is None
    assert queue_count(config) == before
    with ingest._database() as db:
        assert db.execute("SELECT COUNT(*) FROM cmh_event_receipts").fetchone()[0] == original_count


async def unrelated_uploads_remain_allowed(app, web, config, headers, event, source):
    unrelated = reimport(event, source, text="Unrelated synthetic project work remains allowed.")
    reply = await web.post("/hub/v1/events", headers=headers, json=unrelated)
    assert reply.status_code == 202, reply.text
    request_id = f"cmh:{unrelated['instance_id']}:{unrelated['device_id']}:{unrelated['event_id']}"
    assert app.core.state.memory_write_queue.get(request_id) is not None
    _, other_headers, _, other = await provision(web, config)
    other["content"] = copy.deepcopy(event["content"])
    reply = await web.post("/hub/v1/events", headers=other_headers, json=other)
    assert reply.status_code == 202, reply.text


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_completed_governance_blocks_changed_ids_and_mixed_content_after_restart(
    tmp_path, monkeypatch, action
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce_long_originals(
                app, web, config, monkeypatch
            )
            selected = "cmhe:" + jobs[2]
            completed = await execute(web, config, event, selected, action)
            journal = app.core.state.cmh_governance.journal
            state, intent = ExecutionStore(journal).operations()[completed["operation_id"]]
            assert state == "completed"
            assert content_policy_target(journal, event["scope_id"]) in intent.targets
            messages = content_targets(journal, EventEnvelope.model_validate(event))
            assert len(messages) == 1 and messages <= set(intent.targets)
            source = await register_source(web, config, event)
            for mixed in (False, True):
                await rejected_without_enqueue(
                    app,
                    web,
                    config,
                    headers,
                    reimport(event, source, mixed=mixed),
                    "governance_content_suppressed",
                )
            await rejected_without_enqueue(
                app,
                web,
                config,
                headers,
                {**event, "event_id": str(uuid4()), "revision": 2},
                "governance_source_suppressed",
            )
            original = await read(web, headers, event["scope_id"], "history", selected)
            assert original.status_code == (200 if action == "forget" else 404)
            memory = await read(
                web, headers, event["scope_id"], "memories", results[2]["memory_ids"][0]
            )
            assert memory.status_code == 404

        async with fixture(config) as (app, web):
            app.core.state.cmh_governance.require_writes_ready()
            await rejected_without_enqueue(
                app,
                web,
                config,
                headers,
                reimport(event, source, mixed=True),
                "governance_content_suppressed",
            )
            original = await read(web, headers, event["scope_id"], "history", selected)
            assert original.status_code == (200 if action == "forget" else 404)
            await unrelated_uploads_remain_allowed(app, web, config, headers, event, source)

    asyncio.run(scenario())


def test_legacy_forget_then_current_purge_keeps_scope_available_after_restart(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, headers, event, _ = await produce_long_originals(
                app, web, config, monkeypatch
            )
            selected = "cmhe:" + jobs[2]
            # Exercise the old source-only writer through the real executor;
            # keep its signed intent unchanged when the new purge is appended.
            with monkeypatch.context() as legacy_writer:
                legacy_writer.setattr(governance_execution, "capture_sources", capture_sources)
                forgotten = await execute(web, config, event, selected, "forget")
            journal = app.core.state.cmh_governance.journal
            old = ExecutionStore(journal).operations()[forgotten["operation_id"]][1]
            assert any(target.kind == "source_record" for target in old.targets)
            assert content_policy_target(journal, event["scope_id"]) not in old.targets
            assert not content_targets(journal, EventEnvelope.model_validate(event)) & set(
                old.targets
            )
            original = await read(web, headers, event["scope_id"], "history", selected)
            assert original.status_code == 200
            purged = await execute(web, config, event, selected, "purge")
            operations = ExecutionStore(journal).operations()
            assert operations[forgotten["operation_id"]] == ("completed", old)
            assert (
                content_policy_target(journal, event["scope_id"])
                in operations[purged["operation_id"]][1].targets
            )
            original = await read(web, headers, event["scope_id"], "history", selected)
            assert original.status_code == 404
            source = await register_source(web, config, event)
            await rejected_without_enqueue(
                app,
                web,
                config,
                headers,
                reimport(event, source),
                "governance_content_suppressed",
            )

        async with fixture(config) as (app, web):
            app.core.state.cmh_governance.require_writes_ready()
            journal = app.core.state.cmh_governance.journal
            assert ExecutionStore(journal).operations()[forgotten["operation_id"]][1] == old
            await rejected_without_enqueue(
                app,
                web,
                config,
                headers,
                reimport(event, source, mixed=True),
                "governance_content_suppressed",
            )
            await unrelated_uploads_remain_allowed(app, web, config, headers, event, source)

    asyncio.run(scenario())
