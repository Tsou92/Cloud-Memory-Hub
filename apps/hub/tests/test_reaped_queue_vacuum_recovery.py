"""ADR-0038: completed purge recovery must compact an already-reaped Core queue."""

import asyncio
import json
import sqlite3
from hashlib import sha256

from cloud_memory_hub import governance_purge
from hy_memory.client import HyMemoryClient
from one_memory.memory_server.durable_queue import CANCELLED, REAPED_PAYLOAD_MARKER
from test_governance_execution import EXECUTE, read
from test_inflight_execution_recovery import confirmation, row
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_remote_read import grant_reads


def core_has_text(path, text):
    return any(
        text.encode() in candidate.read_bytes()
        for candidate in (path, path.with_name(path.name + "-wal"))
        if candidate.exists()
    )


def test_completed_purge_vacuums_restored_reaped_queue_without_live_records(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    path = config.data_dir / "one-memory.sqlite3"
    text = "Synthetic restored reaped queue retains this orchard planning sentence in old pages."
    # A real accepted message spans SQLite overflow pages, within normal intake
    # limits. The restored database contains no manually inserted residue bytes.
    message = (text + " ") * 256
    calls = []

    def no_generation(*args, **kwargs):
        calls.append(kwargs.get("request_id"))
        raise AssertionError("reaped purge recovery must not invoke SDK generation")

    monkeypatch.setattr(HyMemoryClient, "add", no_generation)
    monkeypatch.setattr(HyMemoryClient, "add_extracted", no_generation)

    async def scenario(backup):
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            event["content"] = {"messages": [{"role": "user", "text": message}]}
            await grant_reads(web, admin, event, permissions=("events:write", "history:read"))
            job = await accepted(web, headers, event)
            identity = "cmhe:" + job
            queue = app.core.state.memory_write_queue
            original = row(queue, job)
            digest = sha256(original["payload_json"].encode()).hexdigest()
            with sqlite3.connect(path) as source:
                source.backup(backup)
            body = await confirmation(web, admin, event, [job], action="purge")
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            executor = app.core.state.cmh_executor
            operations = executor.store.operations()
            head = executor.authority.journal.head(event["scope_id"])
            assert len(operations) == head.sequence == 1
            completed = row(queue, job)
            assert completed["state"] == CANCELLED
            assert completed["hy_invoked"] == original["hy_invoked"] == 0
            assert json.loads(completed["payload_json"], object_pairs_hook=list) == [
                (REAPED_PAYLOAD_MARKER, digest)
            ]
            assert queue.payload_for(job) is None
            assert not await executor.purger.present({identity}, scope_id=event["scope_id"])
            assert (
                await read(web, headers, event["scope_id"], "history", identity)
            ).status_code == 404
        assert not core_has_text(path, text)

        # Restore only Core. Recreate the exact row produced by the successful
        # native cancellation/reaper, on a separate connection with secure delete
        # off: this is the logical post-reap/pre-VACUUM restore point. Config,
        # governance, admissions and SDK stores retain their completed state.
        with sqlite3.connect(path) as restored:
            backup.backup(restored)
            restored.execute("PRAGMA secure_delete=OFF")
            assert restored.execute("PRAGMA secure_delete").fetchone()[0] == 0
            columns = [column for column in completed if column != "job_id"]
            restored.execute(
                "UPDATE memory_write_jobs SET "
                + ",".join(column + "=?" for column in columns)
                + " WHERE job_id=?",
                (*[completed[column] for column in columns], job),
            )
        assert row(queue, job) == completed
        assert queue.payload_for(job) is None
        assert core_has_text(path, text), "fixture must retain genuine old-page plaintext"

        recovered, vacuumed = [], []
        real_run, real_vacuum = governance_purge.Purger.run, governance_purge._vacuum

        async def observe_run(purger, records, contents, *, scope_id, payload_targets=()):
            assert set(records) == {identity} and scope_id == event["scope_id"]
            assert not await purger.present(records, scope_id=scope_id)
            current_queue = purger.ingest._queue()
            assert row(current_queue, job) == completed
            assert current_queue.payload_for(job) is None
            assert core_has_text(path, text)
            assert len(payload_targets) == 1
            target = payload_targets[0]
            assert (target.job_id, target.request_id, target.input_digest) == (
                job,
                original["idempotency_key"],
                digest,
            )
            recovered.append(job)
            return await real_run(
                purger, records, contents, scope_id=scope_id, payload_targets=payload_targets
            )

        def observe_vacuum(target):
            if target == path:
                vacuumed.append(target)
            return real_vacuum(target)

        monkeypatch.setattr(governance_purge.Purger, "run", observe_run)
        monkeypatch.setattr(governance_purge, "_vacuum", observe_vacuum)
        # Actual production lifespan runs replay before exposing reads/claims;
        # no manual resume, cancellation or reaping supplies the repair.
        async with fixture(config) as (app, web):
            executor = app.core.state.cmh_executor
            executor.authority.require_writes_ready()
            assert recovered == [job] and vacuumed == [path]
            assert executor.store.operations() == operations
            assert executor.authority.journal.head(event["scope_id"]) == head
            assert row(app.core.state.memory_write_queue, job) == completed
            assert app.core.state.memory_write_queue.payload_for(job) is None
            assert not await executor.purger.present({identity}, scope_id=event["scope_id"])
            assert (
                await read(web, headers, event["scope_id"], "history", identity)
            ).status_code == 404
        assert not core_has_text(path, text)
        assert calls == []

    with sqlite3.connect(":memory:") as backup:
        asyncio.run(scenario(backup))
