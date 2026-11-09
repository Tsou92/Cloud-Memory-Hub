"""ADR-0038: real queue CAS and signed admission proofs, no model calls.

Completed output coverage and arbitrary native-only deletion remain separate
proof obligations; these tests do not declare either one solved.
"""

import asyncio
import hashlib
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_candidates import collect_inflight_evidence
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_inflight import (
    InflightGovernance,
    inflight_job_target,
    inflight_policy_target,
)
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from one_memory.memory_processing import sdk_generation_input_for
from one_memory.memory_server.durable_queue import CANCELLED, COMPLETED, PENDING, UNKNOWN
from test_governance_candidates import changed, confirmed, observe, tables
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


def coordinator(app):
    return InflightGovernance(
        app.core.state.cmh_ingest,
        app.core.state.cmh_generation.admissions,
        app.core.state.cmh_governance.journal,
    )


def signed_scope(planner, scope, targets):
    intent = Intent(
        operation_id=str(uuid4()),
        actor_id=planner.ingest.access.instance_id,
        scope_id=scope.scope_id,
        action=scope.action,
        targets=tuple(
            set(scope.rule_targets)
            | set(targets)
            | {
                planner.journal.target(scope.scope_id, kind, identity)
                for identity, kind in scope.records.items()
            }
        ),
    )
    ExecutionStore(planner.journal).plan(intent, scope.records)
    planner.journal.append(intent)
    return replace(
        scope,
        basis="operation",
        view_digest=None,
        operation_id=intent.operation_id,
        rule_targets=intent.targets,
    )


def unrelated(event):
    value = changed(event)
    value["content"] = {
        "messages": [{"role": "user", "text": "Another independent synthetic pending topic."}]
    }
    return value


def may_invoke(planner, queue, job):
    planner.admissions.permit_once(
        sdk_generation_input_for(queue.payload_for(job)), require_allowed=lambda _binding: None
    )


def state(queue, job):
    with sqlite3.connect(queue.database_path) as db:
        return db.execute("SELECT state FROM memory_write_jobs WHERE job_id=?", (job,)).fetchone()[
            0
        ]


@pytest.mark.parametrize("initial_state,hy_invoked", [(PENDING, 0), (UNKNOWN, 1)])
def test_confirmed_enrolled_is_cancelled_and_unrelated_enrolled_is_preserved(
    tmp_path, initial_state, hy_invoked
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            other = await accepted(web, headers, unrelated(event))
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET state=?,hy_invoked=? WHERE job_id=?",
                    (initial_state, hy_invoked, job),
                )
            scope = confirmed(app, event, [job])
            before = tables(config)
            inventory = observe(app, scope)
            assert [item.job_id for item in inventory.unmatched_nonterminal_candidates] == [other]
            assert inventory.unmatched_nonterminal_candidates[0].reasons == ()
            targets = planner.prepare(scope)
            assert tables(config) == before  # Preflight never changes queue state or body.
            operation = signed_scope(planner, scope, targets)
            payloads = planner.apply(operation)
            assert len(payloads) == 1 and payloads[0].job_id == job
            assert state(queue, job) == CANCELLED
            assert state(queue, other) == PENDING
            assert queue.payload_for(job) is not None and queue.payload_for(other) is not None
            with sqlite3.connect(queue.database_path) as db:
                row = db.execute(
                    "SELECT hy_invoked,payload_json FROM memory_write_jobs WHERE job_id=?", (job,)
                ).fetchone()
                assert row[0] == hy_invoked
                assert hashlib.sha256(row[1].encode()).hexdigest() == payloads[0].input_digest
            assert planner.apply(operation) == payloads
            assert planner.apply(operation, completed=True) == payloads

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed_job", [False, True])
@pytest.mark.parametrize("evidence", ["possible", "legacy"])
def test_possible_or_legacy_live_request_never_becomes_negative_proof(
    tmp_path, monkeypatch, confirmed_job, evidence
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            if evidence == "legacy":
                monkeypatch.setattr(planner.ingest, "enroll_request", None)
            job = await accepted(web, headers, event)
            scope = confirmed(app, event, [job] if confirmed_job else [])
            if evidence == "possible":
                may_invoke(planner, queue, job)
            before = tables(config)
            with pytest.raises(GovernanceError, match="^governance_inflight_invocation_unproven$"):
                planner.prepare(scope)
            assert tables(config) == before
            assert state(queue, job) == PENDING

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", [False, True])
def test_rule_only_requires_new_preview_even_when_terminal(tmp_path, terminal):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            event["content"] = {
                "messages": [
                    {
                        "role": "user",
                        "text": "A synthetic copied message exceeding the content rule minimum.",
                    }
                ]
            }
            job = await accepted(web, headers, event)
            scope = confirmed(app, event, [job])
            copy = await accepted(web, headers, changed(event))
            queue = app.core.state.memory_write_queue
            if terminal:
                queue.settle(copy, state=COMPLETED)
            before = tables(config)
            with pytest.raises(GovernanceError, match="^governance_inflight_preview_changed$"):
                coordinator(app).prepare(scope)
            assert tables(config) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("record", ["synthetic-native", "cmhj:"])
def test_unrelated_enrollment_does_not_certify_future_native_or_snapshot_inputs(tmp_path, record):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            scope = confirmed(app, event, [])
            scope = replace(scope, records={record + job: "raw"})
            with pytest.raises(GovernanceError, match="^governance_inflight_scope_unproven$"):
                coordinator(app).prepare(scope)
            assert state(app.core.state.memory_write_queue, job) == PENDING

    asyncio.run(scenario())


def test_all_proofs_precede_first_cancellation_and_signed_digest_is_exact(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            first = await accepted(web, headers, event)
            second = await accepted(web, headers, unrelated(event))
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [first, second])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            with sqlite3.connect(queue.database_path) as db:
                # JSON still decodes to exactly the same frozen input. The signed
                # binding nevertheless covers the original stored bytes.
                db.execute(
                    "UPDATE memory_write_jobs SET payload_json=payload_json || ' ' WHERE job_id=?",
                    (second,),
                )
            with pytest.raises(GovernanceError, match="^governance_inflight_binding_mismatch$"):
                planner.apply(operation)
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs "
                    "SET payload_json=substr(payload_json,1,length(payload_json)-1) "
                    "WHERE job_id=?",
                    (second,),
                )
            may_invoke(planner, queue, second)
            with pytest.raises(GovernanceError, match="^governance_inflight_invocation_unproven$"):
                planner.apply(operation)
            assert state(queue, first) == state(queue, second) == PENDING

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["updated_at", "state", "payload", "missing"])
def test_cas_conflict_preserves_queue_and_never_claims_success(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            original = queue.cancel_unpermitted_for_governance

            def race(identity, **kwargs):
                with sqlite3.connect(queue.database_path) as db:
                    if damage == "updated_at":
                        db.execute(
                            "UPDATE memory_write_jobs SET updated_at='later' WHERE job_id=?", (job,)
                        )
                    elif damage == "state":
                        db.execute(
                            "UPDATE memory_write_jobs SET state='unknown' WHERE job_id=?", (job,)
                        )
                    elif damage == "payload":
                        db.execute(
                            "UPDATE memory_write_jobs SET payload_json=payload_json || ' ' "
                            "WHERE job_id=?",
                            (job,),
                        )
                    else:
                        db.execute("DELETE FROM memory_write_jobs WHERE job_id=?", (job,))
                return original(identity, **kwargs)

            monkeypatch.setattr(queue, "cancel_unpermitted_for_governance", race)
            with pytest.raises(GovernanceError, match="^governance_inflight_queue_conflict$"):
                planner.apply(operation)
            with sqlite3.connect(queue.database_path) as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM memory_write_jobs WHERE state='cancelled'"
                    ).fetchone()[0]
                    == 0
                )

    asyncio.run(scenario())


def test_reaped_terminal_and_restored_queue_use_original_signed_binding(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            backup = sqlite3.connect(":memory:")
            try:
                with sqlite3.connect(queue.database_path) as db:
                    db.backup(backup)
                targets = planner.apply(operation)
                target = targets[0]
                assert (
                    queue.clear_terminal_payload_for_governance(
                        job,
                        expected_request_id=target.request_id,
                        expected_payload_digest=target.input_digest,
                    )
                    == "reaped"
                )
                with planner.ingest._database(writing=True) as db:
                    db.execute("DELETE FROM cmh_event_receipts WHERE receipt_id=?", (job,))
                inventory, evidence = collect_inflight_evidence(
                    planner.ingest, operation, journal=planner.journal, anchored_only=True
                )
                assert evidence == {}
                assert inventory.confirmed_original_candidates[0].event_proof == "unavailable"
                assert planner.apply(operation, completed=True) == targets
                with sqlite3.connect(queue.database_path) as db:
                    backup.backup(db)
                assert state(queue, job) == PENDING
                assert planner.apply(operation, completed=True) == targets
                assert state(queue, job) == CANCELLED
            finally:
                backup.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("later", ["possible", "unknown", "broken_payload", "missing_owner"])
def test_completed_replay_ignores_later_unrelated_activity_but_planned_rechecks(tmp_path, later):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            targets = planner.apply(operation)
            store = ExecutionStore(planner.journal)
            store.complete(store.operations()[operation.operation_id][1])
            other = await accepted(web, headers, unrelated(event))
            if later == "possible":
                may_invoke(planner, queue, other)
            else:
                with sqlite3.connect(queue.database_path) as db:
                    if later == "unknown":
                        db.execute(
                            "UPDATE memory_write_jobs SET state='unknown',payload_json='{}' "
                            "WHERE job_id=?",
                            (other,),
                        )
                    elif later == "broken_payload":
                        db.execute(
                            "UPDATE memory_write_jobs SET payload_json='{}' WHERE job_id=?",
                            (other,),
                        )
                    else:
                        db.execute(
                            "UPDATE memory_write_jobs SET source_key=? WHERE job_id=?",
                            ("cmh:" + str(uuid4()), other),
                        )
            before = tables(config)
            assert planner.apply(operation, completed=True) == targets
            assert tables(config) == before
            with pytest.raises(GovernanceError):
                planner.apply(operation)
            assert tables(config) == before

    asyncio.run(scenario())


def test_missing_job_marker_binding_and_budget_are_never_treated_as_empty_success(
    tmp_path, monkeypatch
):
    import cloud_memory_hub.governance_inflight as module

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            targets = planner.prepare(scope)
            candidate = observe(app, scope).confirmed_original_candidates[0]
            assert set(targets) == {
                inflight_policy_target(planner.journal, scope.scope_id),
                inflight_job_target(planner.journal, scope.scope_id, candidate),
            }
            operation = signed_scope(planner, scope, targets)
            legacy = replace(operation, rule_targets=scope.rule_targets)
            with pytest.raises(GovernanceError, match="^governance_inflight_legacy_unavailable$"):
                planner.apply(legacy, completed=True)
            with monkeypatch.context() as patch:
                patch.setattr(
                    module,
                    "MAX_TARGETS",
                    len(scope.records) + len(scope.rule_targets) + len(targets) - 1,
                )
                with pytest.raises(GovernanceError, match="^governance_scan_limit$"):
                    planner.prepare(scope)
            with sqlite3.connect(queue.database_path) as db:
                db.execute("DELETE FROM memory_write_jobs WHERE job_id=?", (job,))
            for completed in (False, True):
                with pytest.raises(GovernanceError, match="^governance_inflight_job_missing$"):
                    planner.apply(operation, completed=completed)
            empty = replace(operation, records={})
            monkeypatch.setattr(
                module,
                "collect_inflight_evidence",
                lambda *_a, **_kw: pytest.fail("completed empty scope scanned queue"),
            )
            assert planner.apply(empty, completed=True) == ()

    asyncio.run(scenario())


def test_terminal_cleanup_certificate_cannot_authorize_later_nonterminal_cancellation(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            queue.settle(job, state=COMPLETED)
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            assert planner.apply(operation)[0].disposition == "terminal_cleanup"
            with sqlite3.connect(queue.database_path) as db:
                db.execute("UPDATE memory_write_jobs SET state='pending' WHERE job_id=?", (job,))
            before = tables(config)
            for completed in (False, True):
                with pytest.raises(
                    GovernanceError, match="^governance_inflight_disposition_unproven$"
                ):
                    planner.apply(operation, completed=completed)
            assert tables(config) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["conversation_turn", "operation"])
def test_residue_text_is_transient_original_message_only_and_never_in_repr(tmp_path, kind):
    config = config_for(tmp_path)
    text = "Synthetic preserved original text for residual-byte verification."
    metadata = "Synthetic metadata is not a residual message selection."

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            event["kind"] = kind
            event["content"] = (
                {"messages": [{"role": "user", "text": text}, {"role": "tool", "text": "short"}]}
                if kind == "conversation_turn"
                else {"text": text}
            )
            event["content"]["metadata"] = {"note": metadata}
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            targets = planner.apply(operation)
            target = targets[0]
            assert target.message_texts == (text,)
            assert text not in repr(target) and metadata not in repr(target)
            assert target.disposition == "cancel_unpermitted"
            for path in planner.journal.path.parent.glob("*.sqlite3*"):
                assert text.encode() not in path.read_bytes()
            assert (
                queue.clear_terminal_payload_for_governance(
                    job,
                    expected_request_id=target.request_id,
                    expected_payload_digest=target.input_digest,
                )
                == "reaped"
            )
            # The retained, validated receipt still supplies physical-residue
            # text, but never stands in for a missing frozen-input permission.
            assert planner.apply(operation, completed=True)[0].message_texts == (text,)
            with planner.ingest._database(writing=True) as db:
                db.execute("DELETE FROM cmh_event_receipts WHERE receipt_id=?", (job,))
            replay = planner.apply(operation, completed=True)
            assert replay == targets and replay[0].message_texts == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["payload", "owner", "hy_invoked", "updated_at", "admission"])
def test_unknown_never_waives_an_independent_damage_blocker(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            with sqlite3.connect(queue.database_path) as db:
                db.execute("UPDATE memory_write_jobs SET state='unknown' WHERE job_id=?", (job,))
                if damage == "payload":
                    db.execute(
                        "UPDATE memory_write_jobs SET payload_json='{}' WHERE job_id=?", (job,)
                    )
                elif damage == "owner":
                    db.execute(
                        "UPDATE memory_write_jobs SET source_key=? WHERE job_id=?",
                        ("cmh:" + str(uuid4()), job),
                    )
                elif damage == "hy_invoked":
                    db.execute("UPDATE memory_write_jobs SET hy_invoked=2 WHERE job_id=?", (job,))
                elif damage == "updated_at":
                    db.execute("UPDATE memory_write_jobs SET updated_at='' WHERE job_id=?", (job,))
            if damage == "admission":
                with sqlite3.connect(planner.admissions.journal.path) as db:
                    db.execute("DELETE FROM intents")
                    db.execute("DELETE FROM heads")
            before = tables(config)
            with pytest.raises(GovernanceError):
                planner.apply(operation)
            assert tables(config) == before
            assert state(queue, job) == UNKNOWN

    asyncio.run(scenario())


def test_completed_replay_has_target_local_budget_and_never_reads_other_bodies(
    tmp_path, monkeypatch
):
    import cloud_memory_hub.governance_candidates as candidates
    import cloud_memory_hub.governance_inflight as module

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            other = await accepted(web, headers, unrelated(event))
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            targets = planner.apply(operation)
            may_invoke(planner, queue, other)
            with sqlite3.connect(queue.database_path) as db:
                job_row = db.execute(
                    "SELECT rowid FROM memory_write_jobs WHERE job_id=?", (job,)
                ).fetchone()[0]
            with planner.ingest._database() as db:
                receipt_row = db.execute(
                    "SELECT rowid FROM cmh_event_receipts WHERE receipt_id=?", (job,)
                ).fetchone()[0]
            original_payload = candidates._payload
            original_collect = module.collect_inflight_evidence

            def bounded(*args, **kwargs):
                return original_collect(
                    *args, budget=candidates.CandidateBudget(max_records=1), **kwargs
                )

            def selected_only(db, table, rowid, budget):
                assert rowid == (job_row if table == "memory_write_jobs" else receipt_row)
                return original_payload(db, table, rowid, budget)

            monkeypatch.setattr(module, "collect_inflight_evidence", bounded)
            monkeypatch.setattr(candidates, "_payload", selected_only)
            assert planner.apply(operation, completed=True) == targets
            with pytest.raises(GovernanceError, match="^governance_candidate_budget_exceeded$"):
                planner.apply(operation)

    asyncio.run(scenario())
