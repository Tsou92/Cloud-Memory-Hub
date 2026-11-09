"""FR-11: request-bound diagnostic rows survive neither purge nor cache restore."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from hy_memory.client import HyMemoryClient
from hy_memory.utils.diagnostic_policy import strict_diagnostics_enabled
from one_memory.api import _sdk_library_bridge
from test_chat_graph import produce
from test_governance_execution import EXECUTE
from test_inflight_execution_recovery import confirmation
from test_original_archive import config_for
from test_output_lineage import fixture

TABLES = ("pipeline_logs_v2", "memory_operations_v2")
TARGET_TEXT = "Synthetic diagnostic-only orchard detail retained independently of source text."
OTHER_TEXT = "Synthetic unrelated diagnostic harbour detail must remain available."

# A separate process uses the SDK's supported legacy default. Never turn off
# the irreversible strict policy in the running Hub to manufacture old bodies.
LEGACY_WRITER = r"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from hy_memory.data.cache_sqlite import SqliteCache
from hy_memory.utils.diagnostic_policy import strict_diagnostics_enabled

async def main():
    assert not strict_diagnostics_enabled()
    payload = json.load(sys.stdin)
    path = Path(payload["path"])
    assert not path.exists()
    os.environ["MEMORY_MEMORY_OPERATIONS_ENABLED"] = "true"
    os.environ["MEMORY_LOG_LLM_CONTENT"] = "true"
    cache = SqliteCache(SimpleNamespace(cache=SimpleNamespace(db_path=str(path))))
    await cache.initialize()
    try:
        for row in payload["rows"]:
            identity = {key: row[key] for key in ("request_id", "user_id", "agent_id")}
            assert await cache.store_pipeline_log(
                **identity, session_id=row["session_id"], step="EXTRACT",
                prompt=row["text"], response=row["text"],
                parsed=json.dumps({"diagnostic": row["text"]}),
                memory_ids=[row["memory_id"]],
            )
            assert await cache.store_memory_operation(
                **identity, op="ADD", memory_id=row["memory_id"],
                content=row["text"], layer="l2_semantic",
                reason="synthetic legacy diagnostic fixture",
            )
    finally:
        await cache.close()
    assert not strict_diagnostics_enabled()

asyncio.run(main())
"""


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


def legacy_diagnostics(path, rows):
    payload = [
        {
            **{
                key: getattr(request, key)
                for key in ("request_id", "user_id", "agent_id", "session_id")
            },
            "memory_id": memory_id,
            "text": text,
        }
        for request, memory_id, text in rows
    ]
    reply = subprocess.run(
        [sys.executable, "-c", LEGACY_WRITER],
        input=json.dumps({"path": str(path), "rows": payload}),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert reply.returncode == 0, reply.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM write_outcomes").fetchone()[0] == 0
        for row in payload:
            assert db.execute(
                "SELECT prompt,response,parsed FROM pipeline_logs_v2 WHERE request_id=?",
                (row["request_id"],),
            ).fetchall() == [(row["text"], row["text"], json.dumps({"diagnostic": row["text"]}))]
            assert db.execute(
                "SELECT content FROM memory_operations_v2 WHERE request_id=?",
                (row["request_id"],),
            ).fetchall() == [(row["text"],)]
            assert file_has_text(path, row["text"])


def _other_tables(db):
    names = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {
        name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
        for (name,) in names
        if name not in TABLES
    }


def restore_diagnostics(legacy_path, cache_path):
    # Copy actual native rows, never synthesize diagnostic fields in SQL. The
    # destination allocates local row IDs; every other field is copied exactly.
    with sqlite3.connect(legacy_path) as source, sqlite3.connect(cache_path) as destination:
        destination.execute("BEGIN IMMEDIATE")
        before = _other_tables(destination)
        for table in TABLES:
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
            assert columns == [row[1] for row in destination.execute(f"PRAGMA table_info({table})")]
            columns.remove("id")
            names = ",".join(columns)
            rows = source.execute(f"SELECT {names} FROM {table}").fetchall()
            destination.executemany(
                f"INSERT INTO {table} ({names}) VALUES ({','.join('?' for _ in columns)})", rows
            )
        assert _other_tables(destination) == before
    assert strict_diagnostics_enabled(), "restoring old rows must not downgrade the Hub policy"


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
            assert strict_diagnostics_enabled()
            jobs, requests, results, _, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            sdk = await _sdk_library_bridge(app.core)._get_client()
            paths = app.core.state.cmh_executor.purger._sdk_paths(sdk)
            cache_path = Path(sdk._cache._db_path)
            target = requests[0]
            other = target.model_copy(update={"request_id": str(uuid4())})
            memory_id = results[0]["memory_ids"][0]
            await asyncio.to_thread(
                legacy_diagnostics,
                backup_path,
                ((target, memory_id, TARGET_TEXT), (other, str(uuid4()), OTHER_TEXT)),
            )
            if not restore_only_diagnostics:
                await asyncio.to_thread(restore_diagnostics, backup_path, cache_path)
            selected_path = backup_path if restore_only_diagnostics else cache_path
            expected_other = diagnostic_rows(selected_path, other.request_id)
            assert all(expected_other.values())
            assert all(diagnostic_rows(selected_path, target.request_id).values())
            assert file_has_text(selected_path, TARGET_TEXT)

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
            # Restore only old diagnostic tables, preserving every other cache
            # table. No outcome, vector node, Hub receipt or history is restored.
            await asyncio.to_thread(restore_diagnostics, backup_path, cache_path)
            restored_other = diagnostic_rows(cache_path, other.request_id)
            assert {table: [row[1:] for row in rows] for table, rows in restored_other.items()} == {
                table: [row[1:] for row in rows] for table, rows in expected_other.items()
            }
            expected_other = restored_other  # Destination-owned autoincrement IDs.
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
            assert strict_diagnostics_enabled()
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

            # Import legacy native bodies into the live cache. Strict remains
            # enabled and no live SQLite file is replaced beneath its owner.
            legacy_path = tmp_path / "retry-legacy-diagnostics.sqlite3"
            await asyncio.to_thread(
                legacy_diagnostics,
                legacy_path,
                (
                    (target, results[0]["memory_ids"][0], TARGET_TEXT),
                    (other, str(uuid4()), OTHER_TEXT),
                ),
            )
            await asyncio.to_thread(restore_diagnostics, legacy_path, cache_path)
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
