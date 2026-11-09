"""ADR-0038: legacy completed purges can reap only proven terminal originals."""

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_purge
from cloud_memory_hub.governance_journal import GovernanceError
from one_memory.memory_server.durable_queue import (
    CANCELLED,
    COMPLETED,
    DEAD,
    FAILED,
    REAPED_PAYLOAD_MARKER,
    UNKNOWN,
)
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


async def jobs_for(app, web, config, count=1):
    admin, headers, _, event = await provision(web, config)
    jobs = []
    for index in range(count):
        value = {
            **event,
            **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
            "content": {
                "messages": [{"role": "user", "text": f"合成原文{index} terminal cleanup"}]
            },
        }
        jobs.append(await accepted(web, headers, value))
    return admin, event, jobs


def stored_rows(queue):
    with sqlite3.connect(queue.database_path) as db:
        db.row_factory = sqlite3.Row
        return {row["job_id"]: dict(row) for row in db.execute("SELECT * FROM memory_write_jobs")}


@pytest.mark.parametrize("terminal", [COMPLETED, CANCELLED, FAILED, DEAD])
def test_only_selected_terminal_original_is_reaped_without_global_cleanup(
    tmp_path, monkeypatch, terminal
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, jobs = await jobs_for(app, web, config, count=4)
            queue = app.core.state.memory_write_queue
            purger = app.core.state.cmh_executor.purger
            queue.settle(jobs[0], state=terminal, hy_invoked=True)
            queue.settle(jobs[1], state=COMPLETED, hy_invoked=True)
            queue.settle(jobs[2], state=UNKNOWN, hy_invoked=True)
            before = stored_rows(queue)

            def no_global_reaper(*args, **kwargs):
                pytest.fail("a legacy purge must never invoke the global payload reaper")

            monkeypatch.setattr(queue, "clear_terminal_payloads", no_global_reaper)
            # A snapshot owner is not authority to clear its entire job; the
            # explicitly selected unknown and pending originals stay untouched.
            records = {"cmhe:" + job: "raw" for job in (jobs[0], jobs[2], jobs[3])}
            records["cmhj:" + jobs[1]] = "raw"
            targets = purger.legacy_terminal_targets(records, scope_id=event["scope_id"])
            assert stored_rows(queue) == before
            assert len(targets) == 1
            target = targets[0]
            expected = sha256(before[jobs[0]]["payload_json"].encode()).hexdigest()
            assert (target.job_id, target.request_id, target.input_digest) == (
                jobs[0],
                before[jobs[0]]["idempotency_key"],
                expected,
            )
            purger._reap_terminal_payloads(targets)
            after = stored_rows(queue)
            assert json.loads(after[jobs[0]]["payload_json"]) == {REAPED_PAYLOAD_MARKER: expected}
            assert after[jobs[0]] == {
                **before[jobs[0]],
                "payload_json": after[jobs[0]]["payload_json"],
            }
            assert all(after[job] == before[job] for job in jobs[1:])
            assert purger.legacy_terminal_targets(records, scope_id=event["scope_id"]) == targets

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["revoked", "purged_original"])
def test_valid_historical_owner_survives_revocation_or_missing_archive(tmp_path, owner):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, event, (job,) = await jobs_for(app, web, config)
            queue = app.core.state.memory_write_queue
            queue.settle(job, state=COMPLETED)
            purger = app.core.state.cmh_executor.purger
            if owner == "revoked":
                response = await web.post(
                    f"/hub/v1/sources/{event['source_id']}/revoke",
                    headers=admin,
                    json={"expected_revision": 1},
                )
                assert response.status_code == 200, response.text
            else:
                purger._hub_rows({job}, set())
            before = stored_rows(queue)
            targets = purger.legacy_terminal_targets(
                {"cmhe:" + job: "raw"}, scope_id=event["scope_id"]
            )
            assert len(targets) == 1 and targets[0].job_id == job
            assert stored_rows(queue) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["cross_scope", "source", "request", "receipt"])
def test_unproven_owner_is_refused_without_mutation(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, (job,) = await jobs_for(app, web, config)
            queue = app.core.state.memory_write_queue
            queue.settle(job, state=COMPLETED)
            if damage in {"source", "request"}:
                column = "source_key" if damage == "source" else "idempotency_key"
                with sqlite3.connect(queue.database_path) as db:
                    db.execute(
                        f"UPDATE memory_write_jobs SET {column}=? WHERE job_id=?",
                        ("cmh:" + str(uuid4()), job),
                    )
            elif damage == "receipt":
                with app.core.state.cmh_ingest._database(writing=True) as db:
                    db.execute(
                        "UPDATE cmh_event_receipts SET scope_id='global' WHERE receipt_id=?", (job,)
                    )
            before = stored_rows(queue)
            with pytest.raises(GovernanceError, match="^governance_purge_request_unproven$"):
                app.core.state.cmh_executor.purger.legacy_terminal_targets(
                    {"cmhe:" + job: "raw"},
                    scope_id="global" if damage == "cross_scope" else event["scope_id"],
                )
            assert stored_rows(queue) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("shape", ["strict", "duplicate", "extra", "uppercase", "nested", "plain"])
def test_only_strict_native_marker_returns_its_embedded_digest(tmp_path, shape):
    config = config_for(tmp_path)
    digest = "a" * 64
    marker = json.dumps({REAPED_PAYLOAD_MARKER: digest})
    payload = {
        "strict": marker,
        "duplicate": marker[:-1] + "," + marker[1:],
        "extra": json.dumps({REAPED_PAYLOAD_MARKER: digest, "body": "must still be cleaned"}),
        "uppercase": json.dumps({REAPED_PAYLOAD_MARKER: digest.upper()}),
        "nested": json.dumps({"nested": {REAPED_PAYLOAD_MARKER: digest}}),
        "plain": "合成的旧终态正文，不是 JSON",
    }[shape]

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, (job,) = await jobs_for(app, web, config)
            queue = app.core.state.memory_write_queue
            queue.settle(job, state=COMPLETED)
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET payload_json=? WHERE job_id=?", (payload, job)
                )
            before = stored_rows(queue)
            (target,) = app.core.state.cmh_executor.purger.legacy_terminal_targets(
                {"cmhe:" + job: "raw"}, scope_id=event["scope_id"]
            )
            assert target.input_digest == (
                digest if shape == "strict" else sha256(payload.encode()).hexdigest()
            )
            assert stored_rows(queue) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_budget_refusal_precedes_fetching_any_queue_body(tmp_path, monkeypatch, budget):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, jobs = await jobs_for(app, web, config, count=2)
            queue = app.core.state.memory_write_queue
            for job in jobs:
                queue.settle(job, state=COMPLETED)
            before = stored_rows(queue)
            if budget == "rows":
                monkeypatch.setattr(governance_purge, "MAX_RECORDS", 1)
            else:
                total = sum(len(row["payload_json"].encode()) for row in before.values())
                monkeypatch.setattr(governance_purge, "MAX_CONTENT_BYTES", total - 1)
            database = governance_purge.main_database
            body_reads = []

            class ObservedConnection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, sql, *args):
                    if sql.upper().startswith("SELECT PAYLOAD_JSON FROM MEMORY_WRITE_JOBS"):
                        body_reads.append(sql)
                    return self.connection.execute(sql, *args)

                def rollback(self):
                    return self.connection.rollback()

            class ObservedDatabase:
                def __init__(self, path):
                    self.database = database(path)

                @contextmanager
                def read(self, **kwargs):
                    with self.database.read(**kwargs) as connection:
                        yield ObservedConnection(connection)

            monkeypatch.setattr(governance_purge, "main_database", ObservedDatabase)
            with pytest.raises(GovernanceError, match="^governance_scan_limit$"):
                app.core.state.cmh_executor.purger.legacy_terminal_targets(
                    {"cmhe:" + job: "raw" for job in jobs}, scope_id=event["scope_id"]
                )
            assert body_reads == []
            assert stored_rows(queue) == before

    asyncio.run(scenario())
