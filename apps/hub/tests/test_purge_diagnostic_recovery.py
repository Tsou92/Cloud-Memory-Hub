"""FR-11: request-bound diagnostic rows survive neither purge nor cache restore."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hy_memory.client import HyMemoryClient
from hy_memory.data.cache_sqlite import SqliteCache
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_chat_graph import produce
from test_governance_execution import EXECUTE
from test_inflight_execution_recovery import confirmation
from test_original_archive import config_for
from test_output_lineage import fixture

TABLES = ("pipeline_logs_v2", "memory_operations_v2")
TARGET_TEXT = "Synthetic diagnostic-only orchard detail retained independently of source text."
OTHER_TEXT = "Synthetic unrelated diagnostic harbour detail must remain available."


def diagnostic_rows(path, request_id):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} WHERE request_id=?", (request_id,)).fetchall()
            for table in TABLES
        }


def file_has_text(path, text):
    return any(
        text.encode() in candidate.read_bytes()
        for candidate in (path, path.with_name(path.name + "-wal"))
        if candidate.exists()
    )


async def store_diagnostics(cache, request, memory_id, text):
    # Use the native persistence methods, not hand-written INSERT statements.
    # This deliberately represents a cache from a diagnostic-enabled period;
    # it makes no assertion that normal production defaults enable such logs.
    assert await SqliteCache.store_pipeline_log(
        cache,
        request_id=request.request_id,
        user_id=request.user_id,
        agent_id=request.agent_id,
        session_id=request.session_id,
        step="EXTRACT",
        prompt=text,
        response=text,
        parsed='{"diagnostic":"' + text + '"}',
        memory_ids=[memory_id],
    )
    assert await cache.store_memory_operation(
        request_id=request.request_id,
        user_id=request.user_id,
        agent_id=request.agent_id,
        op="ADD",
        memory_id=memory_id,
        content=text,
        layer="l2_semantic",
        reason="synthetic diagnostic fixture",
    )


@pytest.mark.parametrize("restore_only_diagnostics", [False, True])
def test_purge_removes_bound_diagnostics_and_preserves_unrelated_rows(
    tmp_path, monkeypatch, restore_only_diagnostics
):
    config = config_for(tmp_path)
    backup_path = tmp_path / "diagnostic-only-backup.sqlite3"
    generation_calls = []

    def no_generation(*args, **kwargs):
        generation_calls.append(kwargs.get("request_id"))
        raise AssertionError("diagnostic purge recovery must not invoke SDK generation")

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, _, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            sdk = await _sdk_library_bridge(app.core)._get_client()
            paths = app.core.state.cmh_executor.purger._sdk_paths(sdk)
            cache_path = Path(sdk._cache._db_path)
            target = requests[0]
            other = target.model_copy(update={"request_id": str(uuid4())})
            memory_id = results[0]["memory_ids"][0]
            target_cache = (
                SqliteCache(SimpleNamespace(cache=SimpleNamespace(db_path=str(backup_path))))
                if restore_only_diagnostics
                else sdk._cache
            )
            if restore_only_diagnostics:
                await target_cache.initialize()
            with monkeypatch.context() as logging:
                logging.setenv("MEMORY_MEMORY_OPERATIONS_ENABLED", "true")
                await _joined_thread(
                    lambda: sdk._loop_thread.run(
                        store_diagnostics(target_cache, target, memory_id, TARGET_TEXT)
                    )
                )
                await _joined_thread(
                    lambda: sdk._loop_thread.run(
                        store_diagnostics(target_cache, other, str(uuid4()), OTHER_TEXT)
                    )
                )
            selected_path = backup_path if restore_only_diagnostics else cache_path
            expected_other = diagnostic_rows(selected_path, other.request_id)
            assert all(expected_other.values())
            assert all(diagnostic_rows(selected_path, target.request_id).values())
            assert file_has_text(selected_path, TARGET_TEXT)
            if restore_only_diagnostics:
                await target_cache.close()

            monkeypatch.setattr(HyMemoryClient, "add", no_generation)
            monkeypatch.setattr(HyMemoryClient, "add_extracted", no_generation)
            body = await confirmation(web, admin, event, jobs, action="purge")
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            executor = app.core.state.cmh_executor
            operations = executor.store.operations()
            head = executor.authority.journal.head(event["scope_id"])

        if restore_only_diagnostics:
            # Restore only the cache containing diagnostic rows. In particular,
            # no write_outcomes, vector node, Hub receipt, or history is restored.
            with sqlite3.connect(backup_path) as source, sqlite3.connect(cache_path) as destination:
                assert source.execute("SELECT count(*) FROM write_outcomes").fetchone()[0] == 0
                source.backup(destination)
            assert all(diagnostic_rows(cache_path, target.request_id).values())
            assert executor.purger._side_present(paths, set(), {target.request_id})
            async with fixture(config) as (app, _web):
                executor = app.core.state.cmh_executor
                executor.authority.require_writes_ready()
                assert executor.store.operations() == operations
                assert executor.authority.journal.head(event["scope_id"]) == head

        assert diagnostic_rows(cache_path, target.request_id) == {table: [] for table in TABLES}
        assert diagnostic_rows(cache_path, other.request_id) == expected_other
        assert not file_has_text(cache_path, TARGET_TEXT)
        assert file_has_text(cache_path, OTHER_TEXT)
        assert generation_calls == []

    asyncio.run(scenario())


def test_completed_purge_retry_removes_restored_diagnostics_without_new_scope(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)
    generation_calls = []

    def no_generation(*args, **kwargs):
        generation_calls.append(kwargs.get("request_id"))
        raise AssertionError("completed purge retry must not invoke SDK generation")

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, _, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            sdk = await _sdk_library_bridge(app.core)._get_client()
            cache_path = Path(sdk._cache._db_path)
            target = requests[0]
            other = target.model_copy(update={"request_id": str(uuid4())})
            monkeypatch.setattr(HyMemoryClient, "add", no_generation)
            monkeypatch.setattr(HyMemoryClient, "add_extracted", no_generation)
            body = await confirmation(web, admin, event, jobs, action="purge")
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            executor = app.core.state.cmh_executor
            operations = executor.store.operations()
            head = executor.authority.journal.head(event["scope_id"])

            # Restore logical diagnostic rows through the native writer while
            # the cache remains open; do not replace a live SQLite database.
            with monkeypatch.context() as logging:
                logging.setenv("MEMORY_MEMORY_OPERATIONS_ENABLED", "true")
                for request, identity, text in (
                    (target, results[0]["memory_ids"][0], TARGET_TEXT),
                    (other, str(uuid4()), OTHER_TEXT),
                ):
                    await _joined_thread(
                        lambda request=request, identity=identity, text=text: sdk._loop_thread.run(
                            store_diagnostics(sdk._cache, request, identity, text)
                        )
                    )
            expected_other = diagnostic_rows(cache_path, other.request_id)
            assert all(expected_other.values())
            assert all(diagnostic_rows(cache_path, target.request_id).values())
            assert file_has_text(cache_path, TARGET_TEXT)
            assert executor.purger._side_present(
                executor.purger._sdk_paths(sdk), set(), {target.request_id}
            )
            again = await web.post(EXECUTE, headers=admin, json=body)
            assert again.status_code == 200, again.text
            assert again.json()["operation_id"] == reply.json()["operation_id"]
            assert again.json()["state"] == "completed"
            assert executor.store.operations() == operations
            assert executor.authority.journal.head(event["scope_id"]) == head
            assert diagnostic_rows(cache_path, target.request_id) == {table: [] for table in TABLES}
            assert diagnostic_rows(cache_path, other.request_id) == expected_other
        assert not file_has_text(cache_path, TARGET_TEXT)
        assert file_has_text(cache_path, OTHER_TEXT)
        assert generation_calls == []

    asyncio.run(scenario())
