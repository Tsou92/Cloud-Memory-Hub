"""ADR-0035: real persisted SDK results across purge faults and split restores."""

import asyncio
import sqlite3

import pytest
import test_chat_graph
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_purge import partition
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_chat_graph import produce
from test_governance_execution import EXECUTE, PREVIEW, read, scan
from test_original_archive import config_for
from test_output_lineage import fixture


async def purge(web, config, event, selected):
    admin = {"Authorization": "Bearer " + config.admin_token}
    body = {"scope_id": event["scope_id"], "action": "purge", "record_ids": [selected]}
    view = await web.post(PREVIEW, headers=admin, json={**body, "limit": 50})
    assert view.status_code == 200, view.text
    return await web.post(
        EXECUTE, headers=admin, json={**body, "expected_view": view.json()["view_digest"]}
    )


@pytest.mark.parametrize("fault", ["legacy_before_side_tables", "after_hub_rows"])
def test_missing_receipt_does_not_lose_cleanup_request_on_restart(tmp_path, monkeypatch, fault):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, _, _, event, _ = await produce(app, web, config, monkeypatch)
            purger = app.core.state.cmh_executor.purger
            sdk = await _sdk_library_bridge(app.core)._get_client()
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id)
            with monkeypatch.context() as injection:
                if fault == "legacy_before_side_tables":
                    # The old order deleted Hub rows before attempting cache cleanup.
                    def crash(*args):
                        purger._hub_rows({jobs[2]}, set())
                        raise RuntimeError("synthetic legacy interruption")

                    injection.setattr(purger, "_sdk_side_tables", crash)
                else:
                    original = purger._hub_rows

                    def crash(*args):
                        original(*args)
                        raise RuntimeError("synthetic post-archive interruption")

                    injection.setattr(purger, "_hub_rows", crash)
                reply = await purge(web, config, event, "cmhe:" + jobs[2])
                assert reply.status_code == 503, reply.text
            with app.core.state.cmh_ingest._database() as db:
                assert not db.execute(
                    "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?", (jobs[2],)
                ).fetchone()
            if fault == "legacy_before_side_tables":
                assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id)

        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            app.core.state.cmh_governance.require_writes_ready()
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is None
            assert await _joined_thread(sdk.get_write_outcome, requests[0].request_id)
        assert scan(tmp_path, "Synthetic fact 3") == []

    asyncio.run(scenario())


def test_purge_removes_cached_input_copies_but_keeps_later_originals_and_raws(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)
    original_accept = test_chat_graph.accepted
    count = 0

    async def unique_original(web, headers, event):
        nonlocal count
        count += 1
        event["content"] = {
            "messages": [{"role": "user", "text": f"Synthetic distinct original {count} for purge"}]
        }
        return await original_accept(web, headers, event)

    monkeypatch.setattr(test_chat_graph, "accepted", unique_original)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            sdk = await _sdk_library_bridge(app.core)._get_client()
            later = await _joined_thread(sdk.get_write_outcome, requests[2].request_id)
            assert (
                "Synthetic distinct original 1 for purge"
                in later["result"]["chat_history"]["rendered"]
            )
            reply = await purge(web, config, event, "cmhe:" + jobs[0])
            assert reply.status_code == 200, reply.text
            for request in requests:
                assert await _joined_thread(sdk.get_write_outcome, request.request_id) is None
            for index in (1, 2):
                assert (
                    await read(web, headers, event["scope_id"], "history", "cmhe:" + jobs[index])
                ).status_code == 200
                assert (
                    await read(
                        web, headers, event["scope_id"], "history", results[index]["memory_id"]
                    )
                ).status_code == 200
                # Facts extracted using the selected history are in its derived
                # closure; their own unselected originals/raw turns are retained.
                assert (
                    await read(
                        web, headers, event["scope_id"], "memories", results[index]["memory_ids"][0]
                    )
                ).status_code == 404
        assert scan(tmp_path, "Synthetic fact 1") == []
        assert scan(tmp_path, "Synthetic distinct original 1 for purge") == []

    asyncio.run(scenario())


def test_restore_only_sdk_cache_replays_completed_purge(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario(backup):
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            sdk = await _sdk_library_bridge(app.core)._get_client()
            cache_path = sdk._cache._db_path
            with sqlite3.connect(cache_path) as cache:
                cache.backup(backup)
            reply = await purge(web, config, event, "cmhe:" + jobs[2])
            assert reply.status_code == 200, reply.text
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is None
        with sqlite3.connect(cache_path) as cache:
            backup.backup(cache)
            assert cache.execute(
                "SELECT 1 FROM write_outcomes WHERE request_id=?", (requests[2].request_id,)
            ).fetchone()
        async with fixture(config) as (app, web):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is None
            assert await _joined_thread(sdk.get_write_outcome, requests[0].request_id)
            assert (
                await read(web, headers, event["scope_id"], "memories", results[2]["memory_ids"][0])
            ).status_code == 404
            app.core.state.cmh_governance.require_writes_ready()

    with sqlite3.connect(":memory:") as backup:
        asyncio.run(scenario(backup))


@pytest.mark.parametrize("damage", ["missing_job", "foreign_scope", "wrong_request"])
def test_unproven_cleanup_identity_never_selects_other_requests(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, _, _, event, _ = await produce(app, web, config, monkeypatch)
            purger = app.core.state.cmh_executor.purger
            _, receipts, snapshots = partition({"cmhe:" + jobs[2]})
            assert purger._requests(receipts, snapshots, event["scope_id"]) == {
                requests[2].request_id
            }
            if damage == "foreign_scope":
                scope = "global"
            else:
                scope = event["scope_id"]
                with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                    if damage == "missing_job":
                        db.execute("DELETE FROM memory_write_jobs WHERE job_id=?", (jobs[2],))
                    else:
                        db.execute(
                            "UPDATE memory_write_jobs SET idempotency_key=? WHERE job_id=?",
                            (requests[2].request_id[:-1] + "z", jobs[2]),
                        )
            with pytest.raises(GovernanceError, match="governance_purge_request_unproven"):
                purger._requests(receipts, snapshots, scope)

    asyncio.run(scenario())


def test_startup_and_background_resume_share_one_replay(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            assert (await purge(web, config, event, "cmhe:" + jobs[2])).status_code == 200
            executor = app.core.state.cmh_executor
            executor._replayed = False
            entered, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def present(*args, **kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return True

            monkeypatch.setattr(executor.purger, "present", present)
            first = asyncio.create_task(executor.resume())
            await asyncio.wait_for(entered.wait(), timeout=5)
            second = asyncio.create_task(executor.resume())
            await asyncio.sleep(0)  # Give the competing caller one scheduling turn.
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
            assert calls == 1
            app.core.state.cmh_governance.require_writes_ready()

    asyncio.run(scenario())
