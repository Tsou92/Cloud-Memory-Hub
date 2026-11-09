"""Forget execution (ADR-0032 slice 1) on the real SDK, Hub stores and HTTP."""

import asyncio
import json
import sqlite3
from hashlib import sha256
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore, pending_operations
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HyMemoryIngestResult, HyMemoryOutput, _joined_thread
from one_memory.memory_server.durable_queue import (
    CANCELLED,
    COMPLETED,
    PENDING,
    REAPED_PAYLOAD_MARKER,
    RUNNING,
    DurableWriteDrain,
)
from test_chat_graph import produce
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request

PREVIEW, EXECUTE = "/hub/v1/governance/preview", "/hub/v1/governance/execute"


async def preview(web, admin, scope, selected):
    reply = await web.post(
        PREVIEW,
        headers=admin,
        json={"scope_id": scope, "action": "forget", "record_ids": selected, "limit": 50},
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


async def read(web, headers, scope, kind, identity):
    return await web.post(
        f"/hub/v1/{kind}/get",
        headers=headers,
        json={"scope_id": scope, "record_id": identity},
    )


async def sdk_status(app, identity):
    sdk = await _sdk_library_bridge(app.core)._get_client()
    node = await _joined_thread(lambda: sdk._loop_thread.run(sdk._vector_store.get_by_id(identity)))
    return node.status.value, node.is_latest


def test_forget_suppresses_scope_keeps_originals_and_blocks_reextraction(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope = event["scope_id"]
            first = "cmhe:" + jobs[0]
            view = await preview(web, admin, scope, [first])
            scoped = {item["record_id"] for item in view["items"]}
            memories = [r["memory_ids"][0] for r in results]
            assert set(memories) <= scoped
            body = {
                "scope_id": scope,
                "action": "forget",
                "record_ids": [first],
                "expected_view": view["view_digest"],
            }
            # Device credentials and stale views cannot execute.
            assert (await web.post(EXECUTE, headers=headers, json=body)).status_code == 403
            stale = {**body, "expected_view": "0" * 64}
            reply = await web.post(EXECUTE, headers=admin, json=stale)
            assert reply.status_code == 409
            assert reply.json() == {"detail": "governance_preview_changed"}
            journal = app.core.state.cmh_governance.journal
            assert not pending_operations(journal, ExecutionStore(journal))
            operation = str(uuid4())
            reply = await web.post(EXECUTE, headers=admin, json={**body, "operation_id": operation})
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            assert reply.json()["suppressed_count"] == len(scoped)
            # A retried request after a lost response is idempotent.
            again = await web.post(EXECUTE, headers=admin, json={**body, "operation_id": operation})
            assert again.status_code == 200 and again.json()["operation_id"] == operation
            assert again.json()["suppressed_count"] == len(scoped)

            for memory in memories:
                assert (await read(web, headers, scope, "memories", memory)).status_code == 404
                assert await sdk_status(app, memory) == ("archived", False)
            listed = await web.post(
                "/hub/v1/memories/list", headers=headers, json={"scope_id": scope}
            )
            assert listed.status_code == 200
            assert not {i["record_id"] for i in listed.json()["items"]} & set(memories)
            found = await web.post(
                "/hub/v1/memories/search",
                headers=headers,
                json={"scope_id": scope, "query": "Synthetic fact"},
            )
            assert found.status_code == 200
            assert not {i["record_id"] for i in found.json()["items"]} & set(memories)
            # Forget keeps the original for explicit reading.
            original = await read(web, headers, scope, "history", first)
            assert original.status_code == 200, original.text
            # Lineage proofs survive the status change: preview still resolves.
            assert (await preview(web, admin, scope, [first]))["total"] == len(scoped)

            # A new revision of a forgotten source record is not re-extracted.
            await grant_reads(
                web,
                admin,
                event,
                permissions=("events:write", "memory:read", "history:read"),
                revision=3,
            )
            revised = {**event, "event_id": str(uuid4()), "revision": 2}
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                row = db.execute(
                    "SELECT source_id,conversation_id,record_id FROM cmh_event_receipts "
                    "WHERE receipt_id=?",
                    (jobs[0],),
                ).fetchone()
            revised.update(source_id=row[0], conversation_id=row[1], record_id=row[2])
            reply = await web.post("/hub/v1/events", headers=headers, json=revised)
            assert reply.status_code == 409, reply.text
            assert reply.json() == {"detail": "governance_source_suppressed"}

        # Restart: suppression is durable and the instance is not quarantined.
        async with fixture(config) as (app, web):
            for memory in memories:
                assert (await read(web, headers, scope, "memories", memory)).status_code == 404
            assert (await read(web, headers, scope, "history", first)).status_code == 200

    asyncio.run(scenario())


def test_new_writes_cannot_reactivate_archived_memories(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            admin = {"Authorization": "Bearer " + config.admin_token}
            first = "cmhe:" + jobs[0]
            view = await preview(web, admin, event["scope_id"], [first])
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "forget",
                    "record_ids": [first],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 200, reply.text
            memories = {r["memory_ids"][0] for r in results}

            # Real reconcile against existing memories: archived ones are not
            # candidates, so the same fact is stored fresh and none revive.
            async def agent(self, **kwargs):
                return AgentResult(
                    success=True, extracted_info={"memory": [{"content": "Synthetic fact 1"}]}
                )

            monkeypatch.setattr(MemAgent, "process_add", agent)
            monkeypatch.setattr("hy_memory.pipelines.writer._RECONCILE_ENABLED", True)
            sdk = await _sdk_library_bridge(app.core)._get_client()
            request = requests[1]
            result = await _joined_thread(
                sdk.add,
                [turn.model_dump() for turn in request.turns],
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                source=request.source_id,
                request_id=str(uuid4()),
                scene="chat",
            )
            assert result["success"]
            for memory in memories:
                assert await sdk_status(app, memory) == ("archived", False)
                reply = await read(web, headers, event["scope_id"], "memories", memory)
                assert reply.status_code == 404

    asyncio.run(scenario())


def test_crash_between_plan_and_completion_resumes_and_orphan_plan_is_discarded(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            first = "cmhe:" + jobs[0]
            view = await preview(web, admin, event["scope_id"], [first])
            executor = app.core.state.cmh_executor

            async def crash(*args):
                raise RuntimeError("synthetic crash after intent")

            monkeypatch.setattr(executor, "_finish", crash)
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "forget",
                    "record_ids": [first],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 503
            memory = results[0]["memory_ids"][0]
            # Planned rows already suppress Hub reads; the SDK is still active.
            hidden = await read(web, headers, event["scope_id"], "memories", memory)
            assert hidden.status_code == 404
            assert await sdk_status(app, memory) == ("active", True)
            # New SDK writes wait for completion.
            with pytest.raises(GovernanceError, match="execution_pending"):
                app.core.state.cmh_governance.require_writes_ready()

        # An orphan plan whose intent never reached the journal is discarded.
        authority = open_authority(config)
        store = ExecutionStore(authority.journal)
        orphan = Intent(
            operation_id=str(uuid4()),
            actor_id=str(uuid4()),
            scope_id=event["scope_id"],
            action="forget",
            targets=(authority.journal.target(event["scope_id"], "memory", "orphan"),),
        )
        store.plan(orphan, {"orphan": "memory"})
        assert "orphan" in store.suppressed(event["scope_id"])

        async with fixture(config) as (app, web):
            await app.core.state.cmh_executor.resume()
            assert await sdk_status(app, memory) == ("archived", False)
            hidden = await read(web, headers, event["scope_id"], "memories", memory)
            assert hidden.status_code == 404
            assert "orphan" not in store.suppressed(event["scope_id"])
            app.core.state.cmh_governance.require_writes_ready()

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["signature", "version", "missing_row", "missing_set"])
def test_tampered_execution_state_quarantines(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            first = "cmhe:" + jobs[0]
            scope_id = event["scope_id"]
            view = await preview(web, admin, scope_id, [first])
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope_id,
                    "action": "forget",
                    "record_ids": [first],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 200, reply.text

    asyncio.run(scenario())
    path = config.governance_dir / "execution.sqlite3"
    with sqlite3.connect(path) as db:
        if damage == "signature":
            db.execute("UPDATE suppressed SET record_id='swapped' WHERE rowid=1")
        elif damage == "missing_row":
            db.execute("DELETE FROM suppressed WHERE rowid=1")
        elif damage == "missing_set":
            db.execute("DELETE FROM suppressed")
        else:
            db.execute("PRAGMA user_version=99")
    # Tampering cannot silently empty the suppression set.
    with pytest.raises(GovernanceError):
        create_hub(config)


def hnsw_slack(path):
    # Known gap (ADR-0032): Chroma's Rust HNSW writer preallocates
    # data_level0.bin and leaves unused slots uninitialised, so they can hold
    # fragments of recently freed process memory. Measured by
    # test_hnsw_slack_is_the_only_residue_and_is_not_a_stored_record.
    return path.name == "data_level0.bin"


def scan(root, needle):
    """Every file under the instance that still contains the bytes."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not hnsw_slack(path) and needle.encode() in path.read_bytes()
    )


def test_purge_removes_every_stored_copy_and_keeps_unrelated_data(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    root = tmp_path

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope = event["scope_id"]
            # Purge the last original: its fact and snapshot, not earlier turns.
            last = "cmhe:" + jobs[2]
            reply = await web.post(
                PREVIEW,
                headers=admin,
                json={"scope_id": scope, "action": "purge", "record_ids": [last], "limit": 50},
            )
            assert reply.status_code == 200, reply.text
            view = reply.json()
            scoped = {item["record_id"] for item in view["items"]}
            purged_fact = results[2]["memory_ids"][0]
            kept_fact = results[0]["memory_ids"][0]
            assert purged_fact in scoped and kept_fact not in scoped
            assert scan(root, "Synthetic fact 3")
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "purge",
                    "record_ids": [last],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["action"] == "purge"
            assert reply.json()["raw_retained_for_explicit_read"] == 0
            for kind, identity in (("memories", purged_fact), ("history", last)):
                assert (await read(web, headers, scope, kind, identity)).status_code == 404
            # Unrelated records remain readable.
            assert (await read(web, headers, scope, "memories", kept_fact)).status_code == 200
            assert (
                await read(web, headers, scope, "history", "cmhe:" + jobs[0])
            ).status_code == 200
            # Lineage over the remaining records is still complete.
            remaining = await preview(web, admin, scope, ["cmhe:" + jobs[0]])
            assert purged_fact not in {item["record_id"] for item in remaining["items"]}
            await app.core.state.cmh_lineage.reconcile_completed()

        # After the process exits, no byte of the purged text remains anywhere
        # (SDK vector/FTS/WAL queue, history, cache, Hub archive, queue DB, logs).
        assert scan(root, "Synthetic fact 3") == []
        assert scan(root, "Synthetic fact 1")
        with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
            assert not db.execute(
                "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?", (jobs[2],)
            ).fetchone()

        # Restart: no quarantine, purged records stay gone, backfill does not recapture.
        async with fixture(config) as (app, web):
            await app.core.state.cmh_lineage.reconcile_completed()
            assert (await read(web, headers, scope, "memories", purged_fact)).status_code == 404
            assert (await read(web, headers, scope, "memories", kept_fact)).status_code == 200
        assert scan(root, "Synthetic fact 3") == []

    asyncio.run(scenario())


def test_purge_of_unsupported_kind_refuses_before_any_intent(tmp_path, monkeypatch):
    from cloud_memory_hub.governance_purge import partition

    with pytest.raises(GovernanceError, match="purge_kind_unsupported"):
        partition({"cmhp:coding:f:1": "memory"})
    native, receipts, snapshots = partition(
        {"a": "memory", "cmhe:r1": "raw", "cmhj:r2": "raw", "cmhi:r3:0": "raw"}
    )
    assert (native, receipts) == ({"a"}, {"r1"})
    assert snapshots == {("cmhj:", "r2"), ("cmhi:", "r3")}


def test_purge_blocks_source_reimport_after_receipt_removal_and_restart(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            await grant_reads(
                web,
                admin,
                event,
                permissions=("events:write", "memory:read", "history:read"),
                revision=3,
            )
            body = {
                "scope_id": event["scope_id"],
                "action": "purge",
                "record_ids": ["cmhe:" + jobs[2]],
            }
            view = (await web.post(PREVIEW, headers=admin, json=body)).json()
            reply = await web.post(
                EXECUTE, headers=admin, json={**body, "expected_view": view["view_digest"]}
            )
            assert reply.status_code == 200, reply.text
            with app.core.state.cmh_ingest._database() as db:
                assert not db.execute(
                    "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?", (jobs[2],)
                ).fetchone()

        async with fixture(config) as (app, web):
            queue = app.core.state.memory_write_queue
            revised = {**event, "event_id": str(uuid4()), "revision": 2}
            reply = await web.post("/hub/v1/events", headers=headers, json=revised)
            assert reply.status_code == 409, reply.text
            assert reply.json() == {"detail": "governance_source_suppressed"}
            assert (
                queue.get(f"cmh:{event['instance_id']}:{event['device_id']}:{revised['event_id']}")
                is None
            )
            # Source identities are represented only by keyed digests.
            for path in config.governance_dir.iterdir():
                if path.is_file():
                    for key in ("source_id", "conversation_id", "record_id"):
                        assert event[key].encode() not in path.read_bytes()
            # A different record from the same source remains eligible.
            unrelated = {**revised, "event_id": str(uuid4()), "record_id": str(uuid4())}
            assert (
                await web.post("/hub/v1/events", headers=headers, json=unrelated)
            ).status_code == 202

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_legacy_source_rules_fail_closed_without_rewriting_intents(tmp_path, monkeypatch, action):
    from cloud_memory_hub import governance_execution
    from test_remote_ingest import provision

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            await grant_reads(
                web,
                admin,
                event,
                permissions=("events:write", "memory:read", "history:read"),
                revision=3,
            )
            body = {
                "scope_id": event["scope_id"],
                "action": action,
                "record_ids": ["cmhe:" + jobs[2]],
            }
            view = (await web.post(PREVIEW, headers=admin, json=body)).json()
            # Model a pre-ADR-0033 writer using the same persisted schema.
            with monkeypatch.context() as legacy:
                legacy.setattr(governance_execution, "capture_sources", lambda *args: ())
                legacy.setattr(app.core.state.cmh_executor.inflight, "prepare", lambda scope: ())
                reply = await web.post(
                    EXECUTE, headers=admin, json={**body, "expected_view": view["view_digest"]}
                )
                assert reply.status_code == 200, reply.text
            journal = app.core.state.cmh_governance.journal
            checkpoint = journal.head(event["scope_id"])

        async with fixture(config) as (app, web):
            revised = {**event, "event_id": str(uuid4()), "revision": 2}
            reply = await web.post("/hub/v1/events", headers=headers, json=revised)
            expected = (
                (409, "governance_source_suppressed")
                if action == "forget"
                else (503, "governance_source_rules_unavailable")
            )
            assert (reply.status_code, reply.json()["detail"]) == expected
            if action == "purge":
                assert not app.core.state.cmh_ingest.source_write_authorizer(
                    "cmh:" + event["source_id"]
                )
            assert app.core.state.cmh_governance.journal.head(event["scope_id"]) == checkpoint
            # Unknown legacy sources block only that project, not the instance.
            _, other_headers, _, other_event = await provision(web, config)
            reply = await web.post("/hub/v1/events", headers=other_headers, json=other_event)
            assert reply.status_code == 202, reply.text

    asyncio.run(scenario())


def test_interrupted_purge_hides_content_blocks_writes_and_resumes(tmp_path, monkeypatch):
    from cloud_memory_hub import governance_purge

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            sdk = await _sdk_library_bridge(app.core)._get_client()
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is not None
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope, last = event["scope_id"], "cmhe:" + jobs[2]
            view = (
                await web.post(
                    PREVIEW,
                    headers=admin,
                    json={"scope_id": scope, "action": "purge", "record_ids": [last]},
                )
            ).json()
            original = governance_purge.Purger._sdk_side_tables

            def crash(*args):
                raise RuntimeError("synthetic crash mid-purge")

            monkeypatch.setattr(governance_purge.Purger, "_sdk_side_tables", crash)
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "purge",
                    "record_ids": [last],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 503
            # Hidden from every read while unfinished, and new writes wait.
            fact = results[2]["memory_ids"][0]
            assert (await read(web, headers, scope, "memories", fact)).status_code == 404
            assert (await read(web, headers, scope, "history", last)).status_code == 404
            with pytest.raises(GovernanceError, match="execution_pending"):
                app.core.state.cmh_governance.require_writes_ready()
            monkeypatch.setattr(governance_purge.Purger, "_sdk_side_tables", original)
            await app.core.state.cmh_executor.resume()
            app.core.state.cmh_governance.require_writes_ready()
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is None
            assert await _joined_thread(sdk.get_write_outcome, requests[0].request_id) is not None
        assert scan(tmp_path, "Synthetic fact 3") == []

    asyncio.run(scenario())


def test_restored_old_data_replays_completed_purge_before_serving(tmp_path, monkeypatch):
    import shutil

    config = config_for(tmp_path)
    backup = tmp_path.parent / (tmp_path.name + "-backup")

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope, last = event["scope_id"], "cmhe:" + jobs[2]
        # Business-data backup from before the purge (governance dir excluded).
        shutil.copytree(config.data_dir, backup / "data")
        shutil.copytree(config.config_dir, backup / "config")
        async with fixture(config) as (app, web):
            view = (
                await web.post(
                    PREVIEW,
                    headers=admin,
                    json={"scope_id": scope, "action": "purge", "record_ids": [last]},
                )
            ).json()
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "purge",
                    "record_ids": [last],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 200, reply.text
        # Restore the old business data; governance keeps its newer ledger.
        for name, target in (("data", config.data_dir), ("config", config.config_dir)):
            shutil.rmtree(target)
            shutil.copytree(backup / name, target)
        assert scan(tmp_path, "Synthetic fact 3")
        # A real restore stops the service; a new process opens the restored
        # files. Chroma caches clients per path in-process, so drop that cache.
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
        async with fixture(config) as (app, web):
            fact = results[2]["memory_ids"][0]
            assert (await read(web, headers, scope, "memories", fact)).status_code == 404
            assert (await read(web, headers, scope, "history", last)).status_code == 404
        assert scan(tmp_path, "Synthetic fact 3") == []

    asyncio.run(scenario())


async def inflight_writer(app, monkeypatch):
    """Real SDK/queue/observer; replace model and profile-dependent supervisor only.

    The drain's production source authorization and durable invocation marker
    still run. This is not a paid-model or full supervisor integration test.
    """
    queue = app.core.state.memory_write_queue
    bridge = _sdk_library_bridge(app.core)
    sdk = await bridge._get_client()
    outputs = []

    async def agent(self, **kwargs):
        return AgentResult(
            success=True,
            extracted_info={"memory": [{"content": "Synthetic inflight resurrection fact"}]},
        )

    async def process(records, *, request_id, before_hy_invocation):
        async with bridge.exclusive_access():
            await before_hy_invocation()
            job = queue.get(request_id)
            request = sdk_request(queue.payload_for(job.job_id))
            result = await _joined_thread(
                sdk.add,
                [turn.model_dump() for turn in request.turns],
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                source=request.source_id,
                request_id=request.request_id,
                scene="chat",
            )
            assert result["success"] and len(result["memory_ids"]) == 1
            outputs.extend(result["memory_ids"])
            return HyMemoryIngestResult(
                success=True,
                raw_ids=(result["memory_id"],),
                memories=tuple(
                    HyMemoryOutput(
                        memory_id=identity, content="Synthetic inflight resurrection fact"
                    )
                    for identity in result["memory_ids"]
                ),
            )

    monkeypatch.setattr(MemAgent, "process_add", agent)
    monkeypatch.setattr("hy_memory.pipelines.writer._RECONCILE_ENABLED", False)
    monkeypatch.setattr(app.core.state.memory_processing_supervisor, "process_sdk_job", process)
    return outputs


async def assert_inflight_is_not_extracted(
    app, monkeypatch, job, *, action, original_payload, web, headers, event
):
    """Check real cancellation, retention and Drain admission, not SDK gate coverage.

    The synthetic supervisor below calls private SDK add directly. Zero calls
    proves the native Drain never starts that writer; it does not prove every
    supervisor or SDK generation entry is protected by the production gate.
    """
    queue = app.core.state.memory_write_queue

    def row():
        with sqlite3.connect(queue.database_path) as db:
            return db.execute(
                "SELECT idempotency_key,state,hy_invoked,payload_json,attempts,updated_at "
                "FROM memory_write_jobs WHERE job_id=?",
                (job,),
            ).fetchone()

    before = row()
    assert before is not None and before[1:3] == (CANCELLED, 0)
    original = await read(web, headers, event["scope_id"], "history", "cmhe:" + job)
    if action == "purge":
        # A sole native marker with the exact former payload digest, including
        # duplicate-key rejection; a marker alongside retained text is not reaped.
        assert json.loads(before[3], object_pairs_hook=list) == [
            (REAPED_PAYLOAD_MARKER, sha256(original_payload.encode()).hexdigest())
        ]
        assert queue.payload_for(job) is None
        assert original.status_code == 404, original.text
    else:
        assert original.status_code == 200, original.text
        items = original.json()["items"]
        assert len(items) == 1 and items[0]["next_content_offset"] is None
        assert json.loads(items[0]["content"]) == json.loads(
            EventEnvelope.model_validate(event).canonical_payload()
        )

    outputs = await inflight_writer(app, monkeypatch)
    sdk = await _sdk_library_bridge(app.core)._get_client()
    add, calls = sdk.add, []

    def observed_add(*args, **kwargs):
        calls.append(kwargs.get("request_id"))
        return add(*args, **kwargs)

    monkeypatch.setattr(sdk, "add", observed_add)
    # Open only the profile/source claim predicates: terminal state itself must
    # keep these tasks out of the real queue worker, even if a model is ready.
    drain = DurableWriteDrain(
        queue,
        execute=app.core.state.memory_write_drain.execute,
        source_writes_permitted=lambda source: True,
        source_claim_enabled=lambda source: True,
    )
    assert await drain.run_one(limit=32) == 0
    assert calls == [] and outputs == []
    assert row() == before


@pytest.mark.parametrize("action", ["forget", "purge"])
@pytest.mark.parametrize("revision", [1, 2])
def test_inflight_pending_jobs_are_cancelled_by_completed_governance(
    tmp_path, monkeypatch, action, revision
):
    """ADR-0038 G-A: every confirmed revision is cancelled, with an empty SDK."""
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            await grant_reads(
                web, admin, event, permissions=("events:write", "memory:read", "history:read")
            )
            first = await accepted(web, headers, event)
            events = {first: event}
            if revision == 2:
                revised = {**event, "event_id": str(uuid4()), "revision": 2}
                job = await accepted(web, headers, revised)
                events[job] = revised
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                payloads = dict(db.execute("SELECT job_id,payload_json FROM memory_write_jobs"))
            sdk = await _sdk_library_bridge(app.core)._get_client()
            namespace = f"cmh:{event['instance_id']}:{event['scope_id']}"
            assert await t0342b_nodes(sdk, namespace) == []
            body = {
                "scope_id": event["scope_id"],
                "action": action,
                "record_ids": ["cmhe:" + first],
            }
            view = await web.post(PREVIEW, headers=admin, json=body)
            assert view.status_code == 200, view.text
            assert {"cmhe:" + job for job in events} == {
                item["record_id"] for item in view.json()["items"]
            }
            reply = await web.post(
                EXECUTE, headers=admin, json={**body, "expected_view": view.json()["view_digest"]}
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            for job, original_event in events.items():
                await assert_inflight_is_not_extracted(
                    app,
                    monkeypatch,
                    job,
                    action=action,
                    original_payload=payloads[job],
                    web=web,
                    headers=headers,
                    event=original_event,
                )
            assert await t0342b_nodes(sdk, namespace) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_inflight_acceptance_between_source_capture_and_plan(tmp_path, monkeypatch, action):
    """V2/G-B: new HTTP intake cannot race through governance's shared barrier."""
    from cloud_memory_hub import governance_execution

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            first = await accepted(web, headers, event)
            body = {
                "scope_id": event["scope_id"],
                "action": action,
                "record_ids": ["cmhe:" + first],
            }
            view = await web.post(PREVIEW, headers=admin, json=body)
            assert view.status_code == 200, view.text
            loop = asyncio.get_running_loop()
            capture = governance_execution.capture_sources
            attempted = []
            queue_path = config.data_dir / "one-memory.sqlite3"
            with sqlite3.connect(queue_path) as db:
                before = db.execute(
                    "SELECT job_id FROM memory_write_jobs ORDER BY job_id"
                ).fetchall()

            def capture_then_accept(*args):
                targets = capture(*args)
                revised = {**event, "event_id": str(uuid4()), "revision": 2}
                # capture_sources runs in a thread; HTTP runs on the owning loop.
                attempted.append(
                    asyncio.run_coroutine_threadsafe(
                        web.post("/hub/v1/events", headers=headers, json=revised), loop
                    ).result(10)
                )
                return targets

            monkeypatch.setattr(governance_execution, "capture_sources", capture_then_accept)
            reply = await web.post(
                EXECUTE, headers=admin, json={**body, "expected_view": view.json()["view_digest"]}
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed" and len(attempted) == 1
            assert (attempted[0].status_code, attempted[0].json()["detail"]) == (
                503,
                "governance_maintenance_in_progress",
            )
            with sqlite3.connect(queue_path) as db:
                assert (
                    db.execute("SELECT job_id FROM memory_write_jobs ORDER BY job_id").fetchall()
                    == before
                )
            # Control: an admission after the plan is rejected, not queued.
            denied = await web.post(
                "/hub/v1/events",
                headers=headers,
                json={**event, "event_id": str(uuid4()), "revision": 3},
            )
            assert (denied.status_code, denied.json()["detail"]) == (
                409,
                "governance_source_suppressed",
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_inflight_restored_queue_is_cancelled_before_startup_claim(tmp_path, monkeypatch, action):
    """ADR-0038 G-C: replay cancellation after restoring only the old queue."""
    config = config_for(tmp_path)
    purge_text = "Synthetic restored queue payload must be physically erased after completed purge."
    queue_path = config.data_dir / "one-memory.sqlite3"
    queue_files = (queue_path, queue_path.with_name(queue_path.name + "-wal"))

    async def scenario(backup):
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            if action == "purge":
                event = {
                    **event,
                    "content": {"messages": [{"role": "user", "text": purge_text}]},
                }
            await grant_reads(
                web, admin, event, permissions=("events:write", "memory:read", "history:read")
            )
            first = await accepted(web, headers, event)
            body = {
                "scope_id": event["scope_id"],
                "action": action,
                "record_ids": ["cmhe:" + first],
            }
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as source:
                original_payload = source.execute(
                    "SELECT payload_json FROM memory_write_jobs WHERE job_id=?", (first,)
                ).fetchone()[0]
                source.backup(backup)
            sdk = await _sdk_library_bridge(app.core)._get_client()
            namespace = f"cmh:{event['instance_id']}:{event['scope_id']}"
            assert await t0342b_nodes(sdk, namespace) == []
            view = await web.post(PREVIEW, headers=admin, json=body)
            assert view.status_code == 200, view.text
            reply = await web.post(
                EXECUTE, headers=admin, json={**body, "expected_view": view.json()["view_digest"]}
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            operation = reply.json()["operation_id"]
            await assert_inflight_is_not_extracted(
                app,
                monkeypatch,
                first,
                action=action,
                original_payload=original_payload,
                web=web,
                headers=headers,
                event=event,
            )
        # Closed service; no file deletion, WAL copying or Chroma cache reset.
        with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as restored:
            backup.backup(restored)
            assert restored.execute(
                "SELECT state,payload_json FROM memory_write_jobs WHERE job_id=?", (first,)
            ).fetchone() == (PENDING, original_payload)
        if action == "purge":
            assert any(
                purge_text.encode() in path.read_bytes() for path in queue_files if path.exists()
            )
        startup_calls = []

        def no_startup_add(*args, **kwargs):
            startup_calls.append(kwargs.get("request_id"))
            raise AssertionError("restored cancelled intent must never enter SDK")

        monkeypatch.setattr(type(sdk), "add", no_startup_add)
        async with fixture(config) as (app, web):
            # The production lifespan must replay before claim; no explicit
            # resume call or test-side queue mutation supplies the cancellation.
            store = app.core.state.cmh_executor.store
            assert store.operations()[operation][0] == "completed"
            await assert_inflight_is_not_extracted(
                app,
                monkeypatch,
                first,
                action=action,
                original_payload=original_payload,
                web=web,
                headers=headers,
                event=event,
            )
            assert startup_calls == []
            assert (
                await t0342b_nodes(await _sdk_library_bridge(app.core)._get_client(), namespace)
                == []
            )
        if action == "purge":
            # A reaped logical row can still leave its former body in SQLite's
            # free pages or WAL. Completed recovery must vacuum/verify those too.
            assert all(
                purge_text.encode() not in path.read_bytes()
                for path in queue_files
                if path.exists()
            )

    with sqlite3.connect(":memory:") as backup:
        asyncio.run(scenario(backup))


@pytest.mark.parametrize("state", [PENDING, "waiting_for_permission", "waiting_for_model", RUNNING])
def test_inflight_upstream_ownerless_settle_cancels_uninvoked_jobs(tmp_path, state):
    """ADR-0034 upstream contract, including a genuinely leased running row."""
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            request_id = queue.payload_for(job).idempotency_key
            # Contract test isolates claim/lease SQL from model-profile readiness.
            drain = DurableWriteDrain(
                queue,
                execute=app.core.state.memory_write_drain.execute,
                source_writes_permitted=lambda source: True,
                source_claim_enabled=lambda source: True,
            )
            owner = drain.owner
            if state == RUNNING:
                claimed = drain.claim(limit=1)
                assert claimed == (job,)
            else:
                queue.settle(job, state=state, hy_invoked=False)
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                before = db.execute(
                    "SELECT state,hy_invoked,lease_owner FROM memory_write_jobs WHERE job_id=?",
                    (job,),
                ).fetchone()
            assert before[:2] == (state, 0)
            if state == RUNNING:
                assert before[2] == owner
            queue.settle(
                job, state=CANCELLED, error_code="governance_source_suppressed", hy_invoked=False
            )
            assert queue.get(request_id).state == CANCELLED
            # Stale worker cannot overwrite cancellation or cross invocation marker.
            assert not queue.mark_invoked(job, owner=owner)
            queue.settle(job, state=COMPLETED, owner=owner)
            assert queue.get(request_id).state == CANCELLED
            assert queue.clear_terminal_payloads() == 1
            assert queue.payload_for(job) is None
            print(f"ADR-0034 settle: {before} -> cancelled; payload cleared")

    asyncio.run(scenario())


def test_inflight_worker_during_sdk_exclusive_records_actual_outcome(tmp_path, monkeypatch):
    """Pin observed behavior, not the ADR's unverified pending assumption."""
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            request_id = queue.payload_for(job).idempotency_key
            supervisor = app.core.state.memory_processing_supervisor
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            calls = []

            def no_model(*args, **kwargs):
                calls.append(True)
                pytest.fail("maintenance must stop before any SDK write")

            async def credentials():
                return (None, None)

            # Keep the native supervisor, source checks, prepare and bridge guard;
            # only bypass profile lookup, with the already-open local bridge.
            monkeypatch.setattr(supervisor, "_active_credentials", credentials)
            monkeypatch.setattr(supervisor, "_bridge", lambda *a, **k: bridge)
            monkeypatch.setattr(sdk, "add", no_model)
            queue.settle(job, state=RUNNING, hy_invoked=False)
            async with app.core.state.cmh_sdk_admission.exclusive():
                # Separate task: exclusive owner itself is intentionally reentrant.
                await asyncio.create_task(app.core.state.memory_write_drain.execute(job))
            actual = queue.get(request_id)
            assert calls == []
            assert actual.state == "unknown"
            assert actual.detail == "hy_memory_maintenance_in_progress"
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                assert db.execute(
                    "SELECT hy_invoked FROM memory_write_jobs WHERE job_id=?", (job,)
                ).fetchone() == (1,)
            print(
                f"ADR-0034 maintenance: state={actual.state}, detail={actual.detail}, hy_invoked=1"
            )
            assert await bridge.get_sdk_write_outcome(request_id) is None
            assert await app.core.state.cmh_reconciler.run() == 0
            assert queue.get(request_id).state == "unknown"
            print("ADR-0034 reconcile: absent SDK outcome -> unknown (not requeued)")

    asyncio.run(scenario())


@pytest.fixture
def t0342a_config():
    """This card owns and cleans all its synthetic instance files."""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="memory-hub-t0342a-") as root:
        yield config_for(Path(root))


@pytest.mark.parametrize("step", [3, 4])
def test_t0342a_native_supervisor_prewrite_refusals(t0342a_config, monkeypatch, step):
    config = t0342a_config

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            request_id = queue.payload_for(job).idempotency_key
            supervisor = app.core.state.memory_processing_supervisor
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            calls = []

            def no_write(*args, **kwargs):
                calls.append(True)
                pytest.fail("prewrite refusal must not invoke SDK add")

            async def credentials():
                return (None, None)

            monkeypatch.setattr(sdk, "add", no_write)
            monkeypatch.setattr(supervisor, "_active_credentials", credentials)
            monkeypatch.setattr(supervisor, "_bridge", lambda *a, **k: bridge)
            if step == 3:
                # A job has already passed claim when readiness changes.
                monkeypatch.setattr(supervisor, "source_writes_permitted", lambda source: False)
            queue.settle(job, state=RUNNING, hy_invoked=False)
            if step == 4:
                async with app.core.state.cmh_sdk_admission.exclusive():
                    await asyncio.create_task(app.core.state.memory_write_drain.execute(job))
            else:
                await app.core.state.memory_write_drain.execute(job)
            actual = queue.get(request_id)
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                marker = db.execute(
                    "SELECT hy_invoked FROM memory_write_jobs WHERE job_id=?", (job,)
                ).fetchone()[0]
            expected = (
                "hy_memory_source_disabled" if step == 3 else "hy_memory_maintenance_in_progress"
            )
            assert (actual.state, actual.detail, marker, calls) == ("unknown", expected, 1, [])
            assert await bridge.get_sdk_write_outcome(request_id) is None
            print(
                f"T-034-2a step={step}: state={actual.state}, hy_invoked={marker}, "
                f"error={actual.detail}, sdk_written=False"
            )

    asyncio.run(scenario())


def test_t0342a_postwrite_observer_maintenance(t0342a_config, monkeypatch):
    from one_memory.memory_server.durable_queue import execute_job

    config = t0342a_config

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            queue = app.core.state.memory_write_queue
            frozen = queue.payload_for(job)
            outputs = await inflight_writer(app, monkeypatch)
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold_maintenance():
                async with app.core.state.cmh_sdk_admission.exclusive():
                    entered.set()
                    await release.wait()

            async def observe_after_write(identity, request):
                assert outputs
                holder = asyncio.create_task(hold_maintenance())
                await entered.wait()
                try:
                    # Real observer, not an injected exception: its bridge guard refuses.
                    await app.core.state.cmh_lineage.observe(identity, request)
                finally:
                    release.set()
                    await holder

            queue.settle(job, state=RUNNING, hy_invoked=False)
            await execute_job(
                job,
                queue=queue,
                supervisor=app.core.state.memory_processing_supervisor,
                evidence=app.core.state.memory_write_evidence_store,
                source_writes_permitted=app.core.state.cmh_ingest.source_write_authorizer,
                observe_sdk_outputs=observe_after_write,
            )
            actual = queue.get(frozen.idempotency_key)
            assert (actual.state, actual.detail) == ("unknown", "hy_memory_maintenance_in_progress")
            assert len(outputs) == 1 and await sdk_status(app, outputs[0]) == ("active", True)
            bridge = _sdk_library_bridge(app.core)
            outcome = await bridge.get_sdk_write_outcome(frozen.idempotency_key)
            assert outcome["status"] == "succeeded"
            raw = await bridge.get_sdk_raw(outcome["result"]["memory_id"])
            assert raw is not None and event["content"]["messages"][0]["text"] in raw.content
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                assert db.execute(
                    "SELECT hy_invoked FROM memory_write_jobs WHERE job_id=?", (job,)
                ).fetchone() == (1,)
            print(
                "T-034-2a step=7: state=unknown, hy_invoked=1, "
                "error=hy_memory_maintenance_in_progress, sdk_written=True, raw_text=True"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt_finish", [False, True])
def test_t0342a_cache_persistence_and_random_raw_identity(
    t0342a_config, monkeypatch, interrupt_finish
):
    """Separate SQLite reader sees commits; simulate stop after nodes, before outcome finish."""
    from uuid import UUID

    from hy_memory.models.memory import MemoryNode

    config = t0342a_config

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            frozen = app.core.state.memory_write_queue.payload_for(job)
            request = sdk_request(frozen)
            sdk = await _sdk_library_bridge(app.core)._get_client()
            original_post_init = MemoryNode.__post_init__
            generated = []

            def capture_random_id(self):
                before = self.node_id
                original_post_init(self)
                if self.layer.value == "l1_raw":
                    generated.append((before, self.node_id))

            async def agent(self, **kwargs):
                return AgentResult(
                    success=True,
                    extracted_info={"memory": [{"content": "Synthetic cache durability fact"}]},
                )

            monkeypatch.setattr(MemoryNode, "__post_init__", capture_random_id)
            monkeypatch.setattr(MemAgent, "process_add", agent)
            monkeypatch.setattr("hy_memory.pipelines.writer._RECONCILE_ENABLED", False)
            finish = sdk._finish_write_outcome
            seen = []

            def disk_row():
                with sqlite3.connect(sdk._cache._db_path) as db:
                    return db.execute(
                        "SELECT status,result_json FROM write_outcomes WHERE request_id=?",
                        (request.request_id,),
                    ).fetchone()

            async def inspect_before_finish(request_id, status, result):
                assert status == "succeeded"
                raw_id = result["memory_id"]
                node = await sdk._vector_store.get_by_id(raw_id)
                assert node is not None and event["content"]["messages"][0]["text"] in node.content
                assert disk_row() == ("running", "{}")
                seen.append(raw_id)
                if interrupt_finish:
                    raise RuntimeError("synthetic_stop_before_outcome_finish")
                await finish(request_id, status, result)

            monkeypatch.setattr(sdk, "_finish_write_outcome", inspect_before_finish)

            async def write():
                return await _joined_thread(
                    sdk.add,
                    [turn.model_dump() for turn in request.turns],
                    user_id=request.user_id,
                    agent_id=request.agent_id,
                    session_id=request.session_id,
                    source=request.source_id,
                    request_id=request.request_id,
                    scene="chat",
                )

            if interrupt_finish:
                with pytest.raises(RuntimeError, match="synthetic_stop_before_outcome_finish"):
                    await write()
            else:
                assert (await write())["success"]
            assert len(seen) == 1
            raw_id = seen[0]
            assert ("", raw_id) in generated
            assert UUID(raw_id).version == 4
            assert raw_id not in frozen.evidence_ids
            status, serialized = disk_row()
            assert status == ("running" if interrupt_finish else "succeeded")
            if not interrupt_finish:
                assert json.loads(serialized)["memory_id"] == raw_id
            # Independent reader after SDK shutdown verifies this is not just in-memory cache.
            cache_path = sdk._cache._db_path
        with sqlite3.connect(cache_path) as db:
            assert db.execute(
                "SELECT status FROM write_outcomes WHERE request_id=?", (request.request_id,)
            ).fetchone() == (status,)
        print(
            f"T-034-2a cache: interrupt_finish={interrupt_finish}, disk_status={status}, "
            "raw_already_stored=True, raw_id_random_uuid4=True, evidence_derivable=False"
        )

    asyncio.run(scenario())


async def t0342b_write(sdk, request, monkeypatch, operation="add"):
    """Native SDK write/cache, synthetic extraction, never a paid model."""

    async def agent(self, **kwargs):
        return AgentResult(
            success=True, extracted_info={"memory": [{"content": "Synthetic split restore fact"}]}
        )

    monkeypatch.setattr(MemAgent, "process_add", agent)
    monkeypatch.setattr("hy_memory.pipelines.writer._RECONCILE_ENABLED", False)
    data = (
        [turn.model_dump() for turn in request.turns]
        if operation == "add"
        else ["Synthetic extracted content for ordering test"]
    )
    return await _joined_thread(
        getattr(sdk, operation),
        data,
        user_id=request.user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        source=request.source_id,
        request_id=request.request_id,
        **({"scene": "chat"} if operation == "add" else {}),
    )


async def t0342b_nodes(sdk, namespace, *, raw_only=False):
    from hy_memory.models.memory import MemoryLayer

    return await _joined_thread(
        lambda: sdk._loop_thread.run(
            sdk._vector_store.list_by_user(
                user_id=namespace,
                limit=10001,
                with_vectors=False,
                allow_raw_layer=True,
                **({"layers": [MemoryLayer.L1_RAW]} if raw_only else {}),
            )
        )
    )


def t0342b_event_matches(nodes, frozen):
    """Candidate scanner, test-only: parse identity, not substring similarity."""
    import re

    wanted = [
        json.loads(
            record.verbatim.split("<cmh_source_reference>\n")[1].split("\n</cmh_source_reference>")[
                0
            ]
        )
        for record in frozen.input_records
    ]
    matches = []
    for node in nodes:
        refs = [
            json.loads(value)
            for value in re.findall(
                r"<cmh_source_reference>\s*(.*?)\s*</cmh_source_reference>", node.content, re.S
            )
        ]
        if any(ref in refs for ref in wanted):
            matches.append(node.node_id)
    return matches


@pytest.mark.parametrize("operation", ["add", "add_extracted"])
def test_t0342b_absent_outcome_and_commit_before_nodes(t0342a_config, monkeypatch, operation):
    config = t0342a_config

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            frozen = app.core.state.memory_write_queue.payload_for(job)
            request = sdk_request(frozen)
            sdk = await _sdk_library_bridge(app.core)._get_client()
            claim = sdk._claim_write_outcome

            async def stop_before_claim(**kwargs):
                assert await sdk.async_get_write_outcome(request.request_id) is None
                raise RuntimeError("synthetic_stop_before_claim")

            # No row is committed and no node-writing implementation is entered.
            with monkeypatch.context() as before:
                before.setattr(sdk, "_claim_write_outcome", stop_before_claim)
                with pytest.raises(RuntimeError, match="synthetic_stop_before_claim"):
                    await t0342b_write(sdk, request, monkeypatch, operation)
            assert (
                await _sdk_library_bridge(app.core).get_sdk_write_outcome(request.request_id)
                is None
            )
            assert await t0342b_nodes(sdk, request.user_id) == []
            assert sdk._claim_write_outcome == claim
            impl_name = "_async_add_impl" if operation == "add" else "_async_add_extracted_impl"
            impl = getattr(sdk, impl_name)
            inspected = []

            async def check_committed_then_write(*args, **kwargs):
                with sqlite3.connect(sdk._cache._db_path) as db:
                    row = db.execute(
                        "SELECT status,result_json FROM write_outcomes WHERE request_id=?",
                        (request.request_id,),
                    ).fetchone()
                assert row == ("running", "{}")
                assert (
                    await sdk._vector_store.list_by_user(
                        user_id=request.user_id, allow_raw_layer=True, with_vectors=False
                    )
                    == []
                )
                inspected.append(True)
                return await impl(*args, **kwargs)

            monkeypatch.setattr(sdk, impl_name, check_committed_then_write)
            result = await t0342b_write(sdk, request, monkeypatch, operation)
            assert result["success"] and inspected == [True]
            assert await t0342b_nodes(sdk, request.user_id)
            print(
                f"T-034-2b order: operation={operation}, absent_row_nodes=0, "
                "before_impl_disk_row=running/{}, before_impl_nodes=0, after_write_nodes=True"
            )

    asyncio.run(scenario())


def test_t0342b_old_cache_new_vectors_not_detected_on_restart(t0342a_config, monkeypatch):
    config = t0342a_config

    async def scenario(backup):
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            queue = app.core.state.memory_write_queue
            job = await accepted(web, headers, event)
            frozen = queue.payload_for(job)
            request = sdk_request(frozen)
            sdk = await _sdk_library_bridge(app.core)._get_client()
            cache_path = sdk._cache._db_path
            with sqlite3.connect(cache_path) as source:
                source.backup(backup)
            result = await t0342b_write(sdk, request, monkeypatch)
            assert result["success"]
            raw_id = result["memory_id"]
            queue.settle(job, state="unknown", hy_invoked=True)
        # Restore ONLY old cache, not Chroma or queue; no manual row deletion.
        with sqlite3.connect(cache_path) as target:
            backup.backup(target)
        async with fixture(config) as (app, web):
            app.core.state.cmh_governance.require_writes_ready()
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            assert await bridge.get_sdk_write_outcome(request.request_id) is None
            raw = await bridge.get_sdk_raw(raw_id)
            assert raw is not None and event["content"]["messages"][0]["text"] in raw.content
            assert await app.core.state.cmh_reconciler.run() == 0
            assert app.core.state.memory_write_queue.get(request.request_id).state == "unknown"
            matches = t0342b_event_matches(
                await t0342b_nodes(sdk, request.user_id, raw_only=True), frozen
            )
            assert matches == [raw_id]
            print(
                "T-034-2b split_restore: startup_allowed=True, outcome=None, raw_text=True, "
                "reconciled=0, state=unknown, automatic_mismatch_detection=False, "
                "manual_scan_found=1"
            )

    with sqlite3.connect(":memory:") as backup:
        asyncio.run(scenario(backup))


def test_t0342b_scan_running_outcome_raw_among_1000(t0342a_config, monkeypatch):
    from dataclasses import replace
    from time import perf_counter

    config = t0342a_config

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            frozen = app.core.state.memory_write_queue.payload_for(job)
            request = sdk_request(frozen)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            observed = []

            async def stop_finish(request_id, status, result):
                observed.append(result["memory_id"])
                raise RuntimeError("synthetic_stop_before_success_cache")

            monkeypatch.setattr(sdk, "_finish_write_outcome", stop_finish)
            with pytest.raises(RuntimeError, match="synthetic_stop_before_success_cache"):
                await t0342b_write(sdk, request, monkeypatch)
            outcome = await bridge.get_sdk_write_outcome(request.request_id)
            assert (outcome["status"], outcome["result"]) == ("running", {})
            assert observed
            raw_id = observed[0]
            raw = await _joined_thread(
                lambda: sdk._loop_thread.run(sdk._vector_store.get_by_id(raw_id))
            )
            assert raw is not None
            # Synthetic distracting raws retain all other event fields, but not event_id.
            others = []
            for _ in range(999):
                content = raw.content.replace(event["event_id"], str(uuid4()))
                others.append(replace(raw, node_id=str(uuid4()), content=content))
            await _joined_thread(
                lambda: sdk._loop_thread.run(sdk._vector_store.upsert_batch(others))
            )
            # Foreign namespace with the exact same source reference must not match.
            foreign = replace(raw, node_id=str(uuid4()), user_id="synthetic-foreign-scope")
            await _joined_thread(lambda: sdk._loop_thread.run(sdk._vector_store.upsert(foreign)))
            samples = []
            for _ in range(3):
                start = perf_counter()
                nodes = await t0342b_nodes(sdk, request.user_id, raw_only=True)
                matches = t0342b_event_matches(nodes, frozen)
                samples.append((perf_counter() - start) * 1000)
                assert len(nodes) == 1000 and matches == [raw_id]
                assert foreign.node_id not in matches
            print(
                "T-034-2b scan: outcome=running/{}, result_raw_id=None, "
                f"raws=1000, matches=1, foreign_excluded=True, elapsed_ms={samples}"
            )

    asyncio.run(scenario())


def test_hnsw_slack_is_the_only_residue_and_is_not_a_stored_record(tmp_path, monkeypatch):
    """Pin the known gap: residue, if any, is only in HNSW slack, never in a slot
    the index counts as an element; everything else is clean."""
    import struct

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            last = "cmhe:" + jobs[2]
            view = (
                await web.post(
                    PREVIEW,
                    headers=admin,
                    json={"scope_id": event["scope_id"], "action": "purge", "record_ids": [last]},
                )
            ).json()
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "purge",
                    "record_ids": [last],
                    "expected_view": view["view_digest"],
                },
            )
            assert reply.status_code == 200, reply.text

    asyncio.run(scenario())
    for level0 in tmp_path.rglob("data_level0.bin"):
        header = (level0.parent / "header.bin").read_bytes()
        _, _, _, count, size = struct.unpack_from("<IQQQQ", header, 0)
        used = level0.read_bytes()[: count * size]
        assert b"Synthetic fact 3" not in used
