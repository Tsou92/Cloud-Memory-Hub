"""FR-10/FR-11, ADR-0039: replay a purge after restoring only native SDK history."""

import asyncio
import sqlite3
from hashlib import sha256
from pathlib import Path

import test_chat_graph
from cloud_memory_hub import governance_purge
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.client import HyMemoryClient
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_chat_graph import produce
from test_governance_execution import read
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_purge_recovery import purge
from test_remote_ingest import provision
from test_sdk_reconcile import sdk_request


def history_rows(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM memory_history ORDER BY id")]


def history_has_text(path, text):
    return any(
        text.encode() in candidate.read_bytes()
        for candidate in (path, path.with_name(path.name + "-wal"))
        if candidate.exists()
    )


def other_files(config, history):
    excluded = {Path(str(history) + suffix) for suffix in ("", "-wal", "-shm")}
    return {
        path: sha256(path.read_bytes()).hexdigest()
        for root in (config.data_dir, config.config_dir, config.governance_dir)
        for path in root.rglob("*")
        if path.is_file() and path not in excluded
    }


def test_completed_purge_replays_only_restored_history_and_preserves_controls(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)
    marker = "Synthetic history-only restoration must erase this distinct orchard planning text."
    original_accept = test_chat_graph.accepted
    accepted_count = 0

    async def distinct_original(web, headers, event):
        nonlocal accepted_count
        accepted_count += 1
        text = (
            (marker + " ") * 96
            if accepted_count == 3
            else f"Synthetic retained same-project history original number {accepted_count}."
        )
        event["content"] = {"messages": [{"role": "user", "text": text}]}
        return await original_accept(web, headers, event)

    monkeypatch.setattr(test_chat_graph, "accepted", distinct_original)
    generation_calls = []

    def no_generation(*args, **kwargs):
        generation_calls.append(kwargs.get("request_id"))
        raise AssertionError("history-only purge replay must not generate SDK memories")

    async def scenario(backup):
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            sdk = await _sdk_library_bridge(app.core)._get_client()
            history = Path(sdk._config.history.db_path)

            # Another real Hub project and SDK write supply a foreign control.
            # No history rows are inserted by the fixture itself.
            _, foreign_headers, _, foreign_event = await provision(web, config)
            foreign_event["content"] = {
                "messages": [{"role": "user", "text": "Synthetic foreign history stays intact."}]
            }
            foreign_job = await accepted(web, foreign_headers, foreign_event)
            queue = app.core.state.memory_write_queue
            foreign_frozen = queue.payload_for(foreign_job)
            foreign_request = sdk_request(foreign_frozen)

            async def foreign_agent(self, **kwargs):
                return AgentResult(
                    success=True,
                    extracted_info={"memory": [{"content": "Synthetic foreign retained fact."}]},
                )

            monkeypatch.setattr(MemAgent, "process_add", foreign_agent)
            foreign_result = await _joined_thread(
                sdk.add,
                [turn.model_dump() for turn in foreign_request.turns],
                user_id=foreign_request.user_id,
                agent_id=foreign_request.agent_id,
                session_id=foreign_request.session_id,
                source=foreign_request.source_id,
                request_id=foreign_request.request_id,
                scene="chat",
            )
            assert foreign_result["success"] and len(foreign_result["memory_ids"]) == 1
            await app.core.state.cmh_lineage.observe(foreign_job, foreign_frozen)
            queue.settle(foreign_job, state=COMPLETED)

            baseline = history_rows(history)
            target_ids = {results[2]["memory_id"], *results[2]["memory_ids"]}
            target_rows = [row for row in baseline if row["memory_id"] in target_ids]
            controls = [row for row in baseline if row["memory_id"] not in target_ids]
            assert len(target_rows) == 1 and marker in target_rows[0]["new_memory"]
            assert {row["memory_id"] for row in controls} == {
                results[0]["memory_id"],
                results[1]["memory_id"],
                foreign_result["memory_id"],
            }
            assert requests[0].user_id != foreign_request.user_id
            assert len({row["isolation_key"] for row in controls}) == 2
            with sqlite3.connect(history) as source:
                source.backup(backup)

            monkeypatch.setattr(HyMemoryClient, "add", no_generation)
            monkeypatch.setattr(HyMemoryClient, "add_extracted", no_generation)
            reply = await purge(web, config, event, "cmhe:" + jobs[2])
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            executor = app.core.state.cmh_executor
            operations = executor.store.operations()
            signed_records = set(executor.store.records(reply.json()["operation_id"]))
            head = executor.authority.journal.head(event["scope_id"])
            foreign_head = executor.authority.journal.head(foreign_event["scope_id"])
            assert set(operations) == {reply.json()["operation_id"]}
            assert head.sequence == 1 and foreign_head.sequence == 0
            assert history_rows(history) == controls
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is None
        assert not history_has_text(history, marker)

        # The service is stopped. Restore the original history database through
        # SQLite backup, leaving every other persisted file byte-for-byte current.
        untouched = other_files(config, history)
        with sqlite3.connect(history) as restored:
            backup.backup(restored)
        assert other_files(config, history) == untouched
        assert history_rows(history) == baseline
        assert history_has_text(history, marker)

        replayed, vacuumed = [], []
        real_run, real_vacuum = governance_purge.Purger.run, governance_purge._vacuum

        async def observe_run(purger, records, contents, *, scope_id, payload_targets=()):
            assert scope_id == event["scope_id"]
            assert set(records) == signed_records and target_ids <= signed_records
            assert history_rows(history) == baseline
            sdk = await purger.bridge()._get_client()
            assert await _joined_thread(sdk.get_write_outcome, requests[2].request_id) is None
            for identity in target_ids:
                assert (
                    await _joined_thread(
                        lambda identity=identity: sdk._loop_thread.run(
                            sdk._vector_store.get_by_id(identity)
                        )
                    )
                ) is None
            assert await purger.present(records, scope_id=scope_id)
            replayed.append(scope_id)
            return await real_run(
                purger, records, contents, scope_id=scope_id, payload_targets=payload_targets
            )

        def observe_vacuum(path):
            if path == history:
                vacuumed.append(path)
            return real_vacuum(path)

        monkeypatch.setattr(governance_purge.Purger, "run", observe_run)
        monkeypatch.setattr(governance_purge, "_vacuum", observe_vacuum)
        # Only the production lifespan performs repair; no manual resume call.
        async with fixture(config) as (app, web):
            executor = app.core.state.cmh_executor
            executor.authority.require_writes_ready()
            assert replayed == [event["scope_id"]] and vacuumed == [history]
            assert executor.store.operations() == operations
            assert executor.authority.journal.head(event["scope_id"]) == head
            assert executor.authority.journal.head(foreign_event["scope_id"]) == foreign_head
            assert history_rows(history) == controls
            assert not history_has_text(history, marker)
            assert (
                await read(web, headers, event["scope_id"], "history", "cmhe:" + jobs[2])
            ).status_code == 404
            assert (
                await read(web, headers, event["scope_id"], "memories", results[2]["memory_ids"][0])
            ).status_code == 404
        assert history_rows(history) == controls
        assert not history_has_text(history, marker)
        assert generation_calls == []

    with sqlite3.connect(":memory:") as backup:
        asyncio.run(scenario(backup))
