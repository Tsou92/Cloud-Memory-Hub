"""FR-10/FR-11: candidate observation preserves privacy and recovery evidence."""

import asyncio
import re
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_candidates import (
    CandidateBudget,
    ConfirmedScope,
    collect_inflight_candidates,
)
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_sources import (
    content_policy_target,
    content_targets,
    source_target,
)
from cloud_memory_hub.remote_ingest import write_request
from one_memory.database_access import main_database
from one_memory.memory_server.durable_queue import COMPLETED, PENDING
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision

TEXT = "Synthetic candidate boundary sentence must remain private."


def envelope(raw, **updates):
    return EventEnvelope.model_validate({**raw, **updates})


def scope_for(journal, event, records=(), *, rules=None, basis="preview"):
    if rules is None:
        rules = (content_policy_target(journal, event.scope_id),)
        if records:
            rules += (source_target(journal, event), *content_targets(journal, event))
    return ConfirmedScope(
        scope_id=event.scope_id,
        action="purge",
        records={"cmhe:" + identity: "raw" for identity in records},
        rule_targets=tuple(rules),
        basis=basis,
        view_digest="a" * 64 if basis == "preview" else None,
        operation_id=str(uuid4()) if basis == "operation" else None,
    )


def observe(app, scope, **kwargs):
    return collect_inflight_candidates(
        app.core.state.cmh_ingest,
        scope,
        journal=app.core.state.cmh_governance.journal,
        **kwargs,
    )


@contextmanager
def watch_sql(monkeypatch, app, *, forbid_payload=False, scope_id=None, before_queue_query=None):
    """Guard real SQLite readers, after schema validation and before observation."""
    ingest = app.core.state.cmh_ingest
    queue = app.core.state.memory_write_queue
    database = main_database(queue.database_path)
    ingest_database, queue_read = ingest._database, database.read
    seen = SimpleNamespace(reads=[], body_reads=[], denied=[], statements=[])
    active = {"sql": "", "parameters": ()}
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            active.update(sql=sql, parameters=parameters)
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def scoped_receipt_length_only(table):
        sql = active["sql"].lower()
        remainder, lengths = re.subn(
            r"length\s*\(\s*cast\s*\(\s*payload_json\s+as\s+blob\s*\)\s*\)", "", sql
        )
        compact = "".join(sql.split())
        return (
            table == "cmh_event_receipts"
            and lengths == 1
            and "payload_json" not in remainder
            and "wherescope_id=?" in compact
            and active["parameters"][0] == scope_id
        )

    def authorize(action, table, column, _database, _trigger):
        if action in writes:
            seen.denied.append(("write", table, column))
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            seen.reads.append((table, column))
            if column == "payload_json" and not scoped_receipt_length_only(table):
                seen.body_reads.append((table, column))
            if (
                forbid_payload
                and column == "payload_json"
                and not scoped_receipt_length_only(table)
            ):
                seen.denied.append(("payload", table, column))
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def trace(statement):
        seen.statements.append(statement)
        if before_queue_query is not None:
            before_queue_query(statement)

    @contextmanager
    def access_reader(*, writing=False):
        assert not writing, "candidate observation requested an intake writer"
        with ingest_database() as db:
            db.set_authorizer(authorize)
            try:
                yield ObservedConnection(db)
            finally:
                db.set_authorizer(None)

    @contextmanager
    def core_reader(**kwargs):
        with queue_read(**kwargs) as db:
            db.set_authorizer(authorize)
            db.set_trace_callback(trace)
            try:
                yield ObservedConnection(db)
            finally:
                db.set_authorizer(None)
                db.set_trace_callback(None)

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_database", access_reader)
        patch.setattr(database, "read", core_reader)
        yield seen


def stored_rows(config):
    result = []
    for path, tables in (
        (config.config_dir / "remote-access.sqlite3", ("cmh_sources", "cmh_event_receipts")),
        (config.data_dir / "one-memory.sqlite3", ("memory_write_jobs",)),
    ):
        with sqlite3.connect(path) as db:
            result.extend(tuple(db.execute("SELECT * FROM " + table)) for table in tables)
    return result


@pytest.mark.parametrize("orphan", [False, True])
def test_foreign_and_unowned_queue_rows_never_read_payload(tmp_path, monkeypatch, orphan):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, raw = await provision(web, config)
            own = envelope(raw)
            if orphan:
                foreign = envelope(raw, source_id=str(uuid4()), event_id=str(uuid4()))
                app.core.state.memory_write_queue.submit(write_request(foreign))
            else:
                _, headers, _, foreign = await provision(web, config)
                foreign["content"] = {"messages": [{"role": "user", "text": TEXT}]}
                await accepted(web, headers, foreign)
            before = stored_rows(config)
            journal = app.core.state.cmh_governance.journal
            with watch_sql(monkeypatch, app, forbid_payload=True, scope_id=own.scope_id) as watched:
                inventory = observe(app, scope_for(journal, own))
            assert watched.denied == []
            assert watched.body_reads == []
            assert inventory.confirmed_original_candidates == ()
            assert inventory.rule_only_candidates == ()
            assert bool(inventory.blockers) is orphan
            assert inventory.ownership_coverage == ("incomplete" if orphan else "complete")
            assert stored_rows(config) == before
            assert TEXT not in repr(inventory)

    asyncio.run(scenario())


def test_queue_body_uses_the_same_real_read_snapshot_as_metadata(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            raw["content"] = {"messages": [{"role": "user", "text": TEXT}]}
            job = await accepted(web, headers, raw)
            event = envelope(raw)
            queue = app.core.state.memory_write_queue
            replacement = write_request(
                envelope(raw, content={"messages": [{"role": "user", "text": TEXT + " changed"}]})
            ).model_dump_json()
            changed = []

            def replace_between_queries(statement):
                sql = statement.lower()
                if (
                    not changed
                    and sql.lstrip().startswith("select")
                    and "memory_write_jobs" in sql
                    and (
                        "then payload_json" in sql
                        or sql.lstrip().startswith("select payload_json")
                        or sql.lstrip().startswith("select *")
                    )
                    and "limit 0" not in sql
                ):
                    with sqlite3.connect(queue.database_path) as other:
                        other.execute(
                            "UPDATE memory_write_jobs SET payload_json=? WHERE job_id=?",
                            (replacement, job),
                        )
                    changed.append(True)

            journal = app.core.state.cmh_governance.journal
            with watch_sql(monkeypatch, app, before_queue_query=replace_between_queries) as watched:
                inventory = observe(app, scope_for(journal, event, (job,)))
            assert changed == [True], "the second connection must really change the stored body"
            assert watched.denied == []
            assert inventory.blockers == ()
            assert inventory.ownership_coverage == "complete"
            assert [candidate.job_id for candidate in inventory.confirmed_original_candidates] == [
                job
            ]
            assert inventory.confirmed_original_candidates[0].event_proof == "accepted_original"
            with sqlite3.connect(queue.database_path) as db:
                assert (
                    db.execute(
                        "SELECT payload_json FROM memory_write_jobs WHERE job_id=?", (job,)
                    ).fetchone()[0]
                    == replacement
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("restored_pending", [False, True])
def test_native_reaped_marker_is_terminal_identity_only(tmp_path, restored_pending):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            job = await accepted(web, headers, raw)
            event = envelope(raw)
            queue = app.core.state.memory_write_queue
            queue.settle(job, state=COMPLETED, hy_invoked=True)
            assert queue.clear_terminal_payloads() == 1
            with app.core.state.cmh_ingest._database(writing=True) as db:
                db.execute("DELETE FROM cmh_event_receipts WHERE receipt_id=?", (job,))
            if restored_pending:
                with sqlite3.connect(queue.database_path) as db:
                    db.execute(
                        "UPDATE memory_write_jobs SET state=? WHERE job_id=?", (PENDING, job)
                    )
            before = stored_rows(config)
            journal = app.core.state.cmh_governance.journal
            inventory = observe(app, scope_for(journal, event, (job,), basis="operation"))
            assert stored_rows(config) == before
            if restored_pending:
                assert any(
                    blocker.job_id == job
                    and blocker.reason == "governance_candidate_payload_unproven"
                    for blocker in inventory.blockers
                )
            else:
                assert inventory.blockers == ()
                assert inventory.ownership_coverage == "complete"
                assert len(inventory.confirmed_original_candidates) == 1
                candidate = inventory.confirmed_original_candidates[0]
                assert candidate.job_id == job
                assert candidate.receipt_presence == "absent"
                assert candidate.event_proof == "unavailable"
                assert candidate.sdk_effect == "not_checked"
                assert candidate.conversation_id is None
                assert candidate.record_id is None
                assert candidate.revision is None

    asyncio.run(scenario())


@pytest.mark.parametrize("revision", [1, 2])
def test_source_or_content_match_never_promotes_an_unconfirmed_job(tmp_path, revision):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            raw["content"] = {"messages": [{"role": "user", "text": TEXT}]}
            original = envelope(raw)
            revised = {**raw, "event_id": str(uuid4()), "revision": revision}
            source_job = await accepted(web, headers, revised)
            same_content = {
                **raw,
                **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
            }
            content_job = await accepted(web, headers, same_content)
            journal = app.core.state.cmh_governance.journal
            rules = (
                content_policy_target(journal, original.scope_id),
                source_target(journal, original),
                *content_targets(journal, original),
            )
            inventory = observe(app, scope_for(journal, original, rules=rules))
            assert inventory.blockers == ()
            assert inventory.confirmed_original_candidates == ()
            candidates = {
                candidate.job_id: candidate for candidate in inventory.rule_only_candidates
            }
            assert set(candidates) == {source_job, content_job}
            assert "source_record" in candidates[source_job].reasons
            assert candidates[source_job].revision == revision
            assert "content" in candidates[content_job].reasons
            assert "source_record" not in candidates[content_job].reasons
            assert all(candidate.sdk_effect == "not_checked" for candidate in candidates.values())

    asyncio.run(scenario())


def test_legacy_rules_without_current_policy_cannot_claim_complete_matching(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            event = envelope(raw)
            job = await accepted(web, headers, raw)
            journal = app.core.state.cmh_governance.journal
            before = stored_rows(config)
            inventory = observe(
                app,
                scope_for(
                    journal,
                    event,
                    (job,),
                    rules=(source_target(journal, event),),
                    basis="operation",
                ),
            )
            assert any(
                blocker.reason == "governance_candidate_rules_unavailable"
                for blocker in inventory.blockers
            )
            assert stored_rows(config) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", [CandidateBudget(max_records=1), CandidateBudget(max_bytes=128)])
def test_exhausted_budget_raises_instead_of_returning_partial_inventory(tmp_path, budget):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            job = await accepted(web, headers, raw)
            await accepted(web, headers, {**raw, "event_id": str(uuid4()), "revision": 2})
            journal = app.core.state.cmh_governance.journal
            before = stored_rows(config)
            with pytest.raises(GovernanceError, match="^governance_candidate_budget_exceeded$"):
                observe(app, scope_for(journal, envelope(raw), (job,)), budget=budget)
            assert stored_rows(config) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("exhaust", ["source_rows", "source_bytes"])
def test_foreign_source_lookup_budget_error_is_never_an_ownership_blocker(
    tmp_path, monkeypatch, exhaust
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, raw = await provision(web, config)
            _, foreign_headers, _, foreign = await provision(web, config)
            await accepted(web, foreign_headers, foreign)
            if exhaust == "source_bytes":
                with app.core.state.cmh_ingest._database(writing=True) as db:
                    db.execute(
                        "UPDATE cmh_sources SET adapter_id=? WHERE source_id=?",
                        ("synthetic-oversized-metadata" * 400, foreign["source_id"]),
                    )
            budget = (
                CandidateBudget(max_records=1)
                if exhaust == "source_rows"
                else CandidateBudget(max_bytes=4096)
            )
            journal = app.core.state.cmh_governance.journal
            own = envelope(raw)
            before = stored_rows(config)
            with watch_sql(monkeypatch, app, forbid_payload=True, scope_id=own.scope_id) as watched:
                with pytest.raises(GovernanceError, match="^governance_candidate_budget_exceeded$"):
                    observe(app, scope_for(journal, own), budget=budget)
            assert watched.denied == []
            assert watched.body_reads == []
            assert stored_rows(config) == before

    asyncio.run(scenario())
