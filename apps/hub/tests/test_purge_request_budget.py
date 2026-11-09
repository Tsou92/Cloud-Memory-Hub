"""FR-10: purge request proofs share bounded reads and one two-store snapshot."""

import asyncio
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_purge
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.remote_access import AccessError
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_purge_legacy_targets import jobs_for, stored_rows
from test_remote_ingest import provision


def observe_reads(monkeypatch, purger, *, on_core_open=None):
    bodies, queries = [], []
    original_ingest = purger.ingest._database
    original_core = governance_purge.main_database
    opens = []

    class ObservedConnection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            normalized = " ".join(sql.upper().split())
            queries.append(normalized)
            if (
                normalized.startswith("SELECT * FROM CMH_EVENT_RECEIPTS")
                or normalized.startswith("SELECT PAYLOAD_JSON FROM")
                or "THEN PAYLOAD_JSON END FROM" in normalized
            ):
                bodies.append(normalized)
            return self.db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def ingest(**kwargs):
        with original_ingest(**kwargs) as db:
            yield ObservedConnection(db)

    class ObservedDatabase:
        def __init__(self, path):
            self.db = original_core(path)

        @contextmanager
        def read(self, **kwargs):
            opens.append(1)
            if on_core_open is not None:
                on_core_open(len(opens))
            with self.db.read(**kwargs) as db:
                yield ObservedConnection(db)

    monkeypatch.setattr(purger.ingest, "_database", ingest)
    monkeypatch.setattr(governance_purge, "main_database", ObservedDatabase)
    return bodies, queries, opens


def lookup(purger, jobs, scope, *, legacy):
    if legacy:
        return purger.legacy_terminal_targets(
            {"cmhe:" + job: "raw" for job in jobs}, scope_id=scope
        )
    return purger._requests(set(jobs), set(), scope)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("budget", ["zero_bytes", "rows"])
def test_refusal_precedes_every_receipt_and_queue_body_fetch(tmp_path, monkeypatch, legacy, budget):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, jobs = await jobs_for(app, web, config, count=2)
            queue, purger = app.core.state.memory_write_queue, app.core.state.cmh_executor.purger
            for job in jobs:
                queue.settle(job, state=COMPLETED)
            before = stored_rows(queue)
            if budget == "zero_bytes":
                monkeypatch.setattr(governance_purge, "MAX_CONTENT_BYTES", 0)
            else:
                monkeypatch.setattr(governance_purge, "MAX_RECORDS", 1)
            bodies, _, _ = observe_reads(monkeypatch, purger)
            with pytest.raises(GovernanceError, match="^governance_scan_limit$"):
                lookup(purger, jobs, event["scope_id"], legacy=legacy)
            assert bodies == []
            assert stored_rows(queue) == before

    asyncio.run(scenario())


def test_receipt_and_terminal_queue_bytes_share_one_preflight_budget(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            jobs = []
            for index in range(2):
                current = {
                    **event,
                    "event_id": str(uuid4()),
                    "record_id": str(uuid4()),
                    "content": {
                        "messages": [
                            {
                                "role": "user",
                                "text": ("Synthetic bounded text " * 1000) + str(index),
                            }
                        ]
                    },
                }
                jobs.append(await accepted(web, headers, current))
            queue, purger = app.core.state.memory_write_queue, app.core.state.cmh_executor.purger
            for job in jobs:
                queue.settle(job, state=COMPLETED)
            with purger.ingest._database() as db:
                receipts = db.execute(
                    "SELECT sum(length(CAST(payload_json AS BLOB))) FROM cmh_event_receipts"
                ).fetchone()[0]
            before = stored_rows(queue)
            queue_bytes = sum(len(row["payload_json"].encode()) for row in before.values())
            limit = max(receipts, queue_bytes) + 8_000
            assert receipts < limit and queue_bytes < limit < receipts + queue_bytes
            monkeypatch.setattr(governance_purge, "MAX_CONTENT_BYTES", limit)
            bodies, _, _ = observe_reads(monkeypatch, purger)
            with pytest.raises(GovernanceError, match="^governance_scan_limit$"):
                lookup(purger, jobs, event["scope_id"], legacy=True)
            assert bodies == []
            assert stored_rows(queue) == before

    asyncio.run(scenario())


def test_job_request_mapping_cannot_be_swapped_between_proof_and_payload_read(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, jobs = await jobs_for(app, web, config, count=2)
            queue, purger = app.core.state.memory_write_queue, app.core.state.cmh_executor.purger
            for job in jobs:
                queue.settle(job, state=COMPLETED)
            before = stored_rows(queue)

            def swap_if_second_snapshot(count):
                if count != 2:
                    return
                with sqlite3.connect(queue.database_path) as db:
                    db.execute(
                        "UPDATE memory_write_jobs SET idempotency_key=? WHERE job_id=?",
                        ("temporary", jobs[0]),
                    )
                    db.execute(
                        "UPDATE memory_write_jobs SET idempotency_key=? WHERE job_id=?",
                        (before[jobs[0]]["idempotency_key"], jobs[1]),
                    )
                    db.execute(
                        "UPDATE memory_write_jobs SET idempotency_key=? WHERE job_id=?",
                        (before[jobs[1]]["idempotency_key"], jobs[0]),
                    )

            _, _, opens = observe_reads(monkeypatch, purger, on_core_open=swap_if_second_snapshot)
            targets = lookup(purger, jobs, event["scope_id"], legacy=True)
            assert {target.job_id: target.request_id for target in targets} == {
                job: before[job]["idempotency_key"] for job in jobs
            }
            assert len(opens) == 1
            assert stored_rows(queue) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("damaged", ["job", "source", "receipt"])
def test_oversized_metadata_is_rejected_before_its_value_projection(tmp_path, monkeypatch, damaged):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, (job,) = await jobs_for(app, web, config)
            queue, purger = app.core.state.memory_write_queue, app.core.state.cmh_executor.purger
            queue.settle(job, state=COMPLETED)
            huge = "synthetic oversized metadata " * 1000
            if damaged == "job":
                with sqlite3.connect(queue.database_path) as db:
                    db.execute("UPDATE memory_write_jobs SET idempotency_key=?", (huge,))
                projection = "SELECT JOB_ID,IDEMPOTENCY_KEY,SOURCE_KEY,STATE FROM"
            else:
                # Simulate an externally damaged database, outside its normal
                # foreign-key-enforced writer; the reader must still bound it.
                with sqlite3.connect(purger.ingest.access.path) as db:
                    if damaged == "source":
                        db.execute("UPDATE cmh_sources SET device_id=?", (huge,))
                        projection = "SELECT SOURCE_ID,DEVICE_ID,SCOPE_ID FROM"
                    else:
                        db.execute("UPDATE cmh_event_receipts SET payload_digest=?", (huge,))
                        projection = "SELECT RECEIPT_ID,REQUEST_ID,DEVICE_ID,EVENT_ID,"
            before = stored_rows(queue)
            monkeypatch.setattr(governance_purge, "MAX_CONTENT_BYTES", 4096)
            bodies, queries, _ = observe_reads(monkeypatch, purger)
            with pytest.raises(GovernanceError, match="^governance_scan_limit$"):
                lookup(purger, [job], event["scope_id"], legacy=True)
            assert bodies == []
            assert not any(query.startswith(projection) for query in queries)
            assert stored_rows(queue) == before

    asyncio.run(scenario())


def test_nonterminal_body_is_not_measured_or_fetched_and_snapshots_never_select_whole_job(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, (job,) = await jobs_for(app, web, config)
            queue, purger = app.core.state.memory_write_queue, app.core.state.cmh_executor.purger
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET state='unknown',payload_json=?", ("x" * 20_000,)
                )
            monkeypatch.setattr(governance_purge, "MAX_CONTENT_BYTES", 4096)
            bodies, queries, opens = observe_reads(monkeypatch, purger)
            assert lookup(purger, [job], event["scope_id"], legacy=True) == ()
            assert not any(
                "PAYLOAD_JSON" in query and "MEMORY_WRITE_JOBS" in query for query in queries
            )
            assert all("CMH_EVENT_RECEIPTS" in query for query in bodies)
            prior = len(opens)
            assert (
                purger.legacy_terminal_targets({"cmhj:" + job: "raw"}, scope_id=event["scope_id"])
                == ()
            )
            assert len(opens) == prior

    asyncio.run(scenario())


def test_owner_failure_precedes_body_read_and_original_failure_keeps_public_contract(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, event, (job,) = await jobs_for(app, web, config)
            queue, purger = app.core.state.memory_write_queue, app.core.state.cmh_executor.purger
            with purger.ingest._database(writing=True) as db:
                db.execute("UPDATE cmh_event_receipts SET payload_json='{}'")
            with sqlite3.connect(queue.database_path) as db:
                db.execute("UPDATE memory_write_jobs SET source_key='broken'")
            bodies, _, _ = observe_reads(monkeypatch, purger)
            with pytest.raises(GovernanceError, match="^governance_purge_request_unproven$"):
                lookup(purger, [job], event["scope_id"], legacy=False)
            assert bodies == []
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET source_key=?", ("cmh:" + event["source_id"],)
                )
            with pytest.raises(AccessError, match="^original_archive_invalid$"):
                lookup(purger, [job], event["scope_id"], legacy=False)
            with pytest.raises(GovernanceError, match="^governance_purge_request_unproven$"):
                lookup(purger, [job], event["scope_id"], legacy=True)

    asyncio.run(scenario())
