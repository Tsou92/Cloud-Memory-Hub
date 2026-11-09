"""Forget execution (ADR-0032 slice 1) on the real SDK, Hub stores and HTTP."""

import asyncio
import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore, pending_operations
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_chat_graph import produce
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_read import grant_reads

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
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
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
