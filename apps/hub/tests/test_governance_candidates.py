"""FR-10/FR-11: real two-store candidate observations, without execution wiring."""

import asyncio
import copy
import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_candidates import (
    ConfirmedScope,
    collect_inflight_candidates,
)
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_sources import capture_rules
from cloud_memory_hub.remote_ingest import write_request
from one_memory.memory_server.durable_queue import _TERMINAL_WRITE_STATES
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


def confirmed(app, event, jobs):
    journal = app.core.state.cmh_governance.journal
    records = {"cmhe:" + job: "raw" for job in jobs}
    ingest = app.core.state.cmh_ingest
    return ConfirmedScope(
        event["scope_id"],
        "purge",
        records,
        capture_rules(journal, ingest, event["scope_id"], records),
        "preview",
        view_digest="a" * 64,
    )


def observe(app, scope, **kwargs):
    return collect_inflight_candidates(
        app.core.state.cmh_ingest,
        scope,
        journal=app.core.state.cmh_governance.journal,
        **kwargs,
    )


def changed(event, *, revision=None):
    value = copy.deepcopy(event)
    value["event_id"] = str(uuid4())
    if revision is not None:
        value["revision"] = revision
    else:
        value.update({key: str(uuid4()) for key in ("conversation_id", "record_id")})
    return value


def tables(config):
    values = []
    for path in (
        config.config_dir / "remote-access.sqlite3",
        config.data_dir / "one-memory.sqlite3",
    ):
        with sqlite3.connect(path) as db:
            names = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            values.append(
                {name: db.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}
            )
    return values


def test_scope_copies_its_internal_input_and_rejects_client_shaped_data():
    job = str(uuid4())
    records = {"cmhe:" + job: "raw"}
    scope = ConfirmedScope("global", "forget", records, [], "preview", view_digest="a" * 64)
    records.clear()
    assert dict(scope.records) == {"cmhe:" + job: "raw"}
    assert scope.rule_targets == ()
    with pytest.raises(TypeError):
        scope.records["extra"] = "raw"
    with pytest.raises(GovernanceError, match="governance_candidate_scope_invalid"):
        ConfirmedScope("global", "forget", {}, (), "preview")
    with pytest.raises(GovernanceError, match="governance_candidate_scope_invalid"):
        collect_inflight_candidates(None, {"scope_id": "global"}, journal=None)


def test_confirmed_revisions_are_deduplicated_and_unconfirmed_rule_matches_remain_separate(
    tmp_path,
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            event["content"] = {
                "messages": [
                    {
                        "role": "user",
                        "text": "A complete synthetic sentence that must never revive.",
                    }
                ]
            }
            first = await accepted(web, headers, event)
            second = await accepted(web, headers, changed(event, revision=2))
            scope = confirmed(app, event, [first, second])
            third = await accepted(web, headers, changed(event, revision=3))
            copied = await accepted(web, headers, changed(event))
            before = tables(config)
            result = observe(app, scope)
            assert tables(config) == before
            assert result.blockers == () and result.ownership_coverage == "complete"
            assert {item.job_id for item in result.confirmed_original_candidates} == {first, second}
            matches = {item.job_id: item for item in result.rule_only_candidates}
            assert set(matches) == {third, copied}
            assert matches[third].reasons == ("content", "source_record")
            assert matches[copied].reasons == ("content",)
            assert result.usage.jobs == result.usage.receipts == 4
            for item in (*result.confirmed_original_candidates, *result.rule_only_candidates):
                assert item.event_proof == "accepted_original"
                assert item.sdk_effect == "not_checked"
                assert item.receipt_presence == "present"
            assert "must never revive" not in repr(result)
            assert result.usage.payload_bytes > 0 and result.usage.metadata_bytes > 0

    asyncio.run(scenario())


def test_missing_receipt_roundtrip_and_revoked_source_still_prove_historical_ownership(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            original = await accepted(web, headers, event)
            scope = confirmed(app, event, [original])
            queue = app.core.state.memory_write_queue
            next_event = EventEnvelope.model_validate(changed(event, revision=2))
            unreceipted = queue.submit(write_request(next_event)).job_id
            with app.core.state.cmh_ingest._database(writing=True) as db:
                db.execute("DELETE FROM cmh_event_receipts WHERE receipt_id=?", (original,))
                db.execute(
                    "UPDATE cmh_sources SET status='revoked' WHERE source_id=?",
                    (event["source_id"],),
                )
            before = tables(config)
            result = observe(app, scope)
            assert result.blockers == () and result.ownership_coverage == "complete"
            assert tables(config) == before
            assert [item.job_id for item in result.confirmed_original_candidates] == [original]
            assert [item.job_id for item in result.rule_only_candidates] == [unreceipted]
            for item in (*result.confirmed_original_candidates, *result.rule_only_candidates):
                assert item.event_proof == "queue_roundtrip"
                assert item.receipt_presence == "absent"
                assert item.source_status == "revoked" and item.sdk_effect == "not_checked"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("instance", "governance_candidate_owner_unproven"),
        ("receipt", "governance_candidate_original_unproven"),
        ("frozen", "governance_candidate_payload_unproven"),
        ("future_state", "governance_candidate_state_unproven"),
    ],
)
def test_ownership_original_and_frozen_damage_are_blockers_without_changes(
    tmp_path, damage, reason
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            scope = confirmed(app, event, [job])
            queue = app.core.state.memory_write_queue
            if damage == "receipt":
                with app.core.state.cmh_ingest._database(writing=True) as db:
                    db.execute("UPDATE cmh_event_receipts SET payload_digest=?", ("0" * 64,))
            else:
                with sqlite3.connect(queue.database_path) as db:
                    if damage == "instance":
                        request_id = f"cmh:{uuid4()}:{event['device_id']}:{event['event_id']}"
                        db.execute("UPDATE memory_write_jobs SET idempotency_key=?", (request_id,))
                    elif damage == "frozen":
                        db.execute("UPDATE memory_write_jobs SET payload_json='{}'")
                    else:
                        db.execute("UPDATE memory_write_jobs SET state='future-state'")
            before = tables(config)
            result = observe(app, scope)
            assert reason in {block.reason for block in result.blockers}
            assert result.confirmed_original_candidates == ()
            assert tables(config) == before
            if damage == "instance":
                assert result.ownership_coverage == "incomplete"
                assert result.scope_nonterminal_job_ids == ()
            else:
                assert result.scope_nonterminal_job_ids == (job,)

    asyncio.run(scenario())


def native_scope(app, event):
    records = {str(uuid4()): "memory"}
    return ConfirmedScope(
        event["scope_id"],
        "purge",
        records,
        capture_rules(
            app.core.state.cmh_governance.journal,
            app.core.state.cmh_ingest,
            event["scope_id"],
            records,
        ),
        "preview",
        view_digest="a" * 64,
    )


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "running",
        "waiting_for_permission",
        "waiting_for_model",
        "unknown",
        "broken-state",
    ],
)
def test_scope_nonterminal_ids_include_unmatched_http_jobs_but_exclude_foreign_jobs(
    tmp_path, state
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            first = await accepted(web, headers, event)
            second = await accepted(web, headers, changed(event))
            _, foreign_headers, _, foreign_event = await provision(web, config)
            foreign_event["content"] = copy.deepcopy(event["content"])
            foreign = await accepted(web, foreign_headers, foreign_event)
            with sqlite3.connect(app.core.state.memory_write_queue.database_path) as db:
                db.execute("UPDATE memory_write_jobs SET state=? WHERE job_id=?", (state, first))
            before = tables(config)
            result = observe(app, native_scope(app, event))
            assert result.scope_nonterminal_job_ids == tuple(sorted((first, second)))
            assert foreign not in result.scope_nonterminal_job_ids
            assert result.confirmed_original_candidates == result.rule_only_candidates == ()
            assert result.ownership_coverage == "complete"
            if state in {"unknown", "broken-state"}:
                assert {item.reason for item in result.blockers} == {
                    "governance_candidate_state_unproven",
                }
            else:
                assert result.blockers == ()
            assert tables(config) == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("payload", "governance_candidate_payload_unproven"),
        ("receipt", "governance_candidate_original_unproven"),
    ],
)
def test_scope_nonterminal_identity_is_kept_before_body_validation(tmp_path, damage, reason):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            if damage == "payload":
                with sqlite3.connect(app.core.state.memory_write_queue.database_path) as db:
                    db.execute(
                        "UPDATE memory_write_jobs SET payload_json='{}' WHERE job_id=?", (job,)
                    )
            else:
                with app.core.state.cmh_ingest._database(writing=True) as db:
                    db.execute(
                        "UPDATE cmh_event_receipts SET payload_json='{}' WHERE receipt_id=?", (job,)
                    )
            before = tables(config)
            result = observe(app, native_scope(app, event))
            assert result.scope_nonterminal_job_ids == (job,)
            assert result.confirmed_original_candidates == result.rule_only_candidates == ()
            assert reason in {item.reason for item in result.blockers}
            assert result.ownership_coverage == "complete"
            assert tables(config) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("state", sorted(_TERMINAL_WRITE_STATES))
def test_scope_nonterminal_identity_excludes_all_upstream_terminal_states(tmp_path, state):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            app.core.state.memory_write_queue.settle(job, state=state, hy_invoked=True)
            result = observe(app, native_scope(app, event))
            assert result.scope_nonterminal_job_ids == ()
            assert result.confirmed_original_candidates == result.rule_only_candidates == ()
            assert result.blockers == ()

    asyncio.run(scenario())
