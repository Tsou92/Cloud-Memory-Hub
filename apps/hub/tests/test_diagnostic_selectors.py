"""ADR-0042: real metadata proofs without queue, payload, witness or journal writes."""

import asyncio
import json
import sqlite3
from dataclasses import FrozenInstanceError, asdict, fields, replace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_candidates import collect_inflight_evidence
from cloud_memory_hub.governance_inflight import (
    CleanupPreparation,
    CleanupSelector,
    inflight_job_target,
)
from cloud_memory_hub.governance_journal import GovernanceError
from one_memory.memory_server.durable_queue import CANCELLED, COMPLETED, PENDING, UNKNOWN
from test_governance_candidates import confirmed
from test_governance_inflight import coordinator, may_invoke, signed_scope, state, unrelated
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_write_admissions import restore, snapshot


def all_rows(config, planner):
    """Include every user table and both signed journals, not selected columns."""
    paths = {
        config.config_dir / "remote-access.sqlite3",
        config.data_dir / "one-memory.sqlite3",
        *config.governance_dir.glob("*.sqlite3"),
    }
    values = {}
    for path in sorted(paths):
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            values[str(path)] = {
                name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                for (name,) in tables
            }
    for label, journal in (
        ("main_heads", planner.journal),
        ("admission_heads", planner.admissions.journal),
    ):
        values[label] = {scope: journal.head(scope) for scope in journal.scopes()}
    return values


def checked(config, planner, scope, **kwargs):
    before = all_rows(config, planner)
    try:
        return planner.prepare_cleanup_selectors(scope, **kwargs)
    finally:
        assert all_rows(config, planner) == before


def witness_rows(planner):
    with planner.admissions.access._connection() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM cmh_admission_witnesses")]


@pytest.mark.parametrize("initial", [PENDING, UNKNOWN, COMPLETED])
def test_real_selectors_are_read_only_sorted_immutable_and_have_no_body(tmp_path, initial):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            first = await accepted(web, headers, event)
            second = await accepted(web, headers, unrelated(event))
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET state=?,hy_invoked=?",
                    (initial, int(initial == UNKNOWN)),
                )
            scope = confirmed(app, event, [second, first])
            prepared = checked(config, planner, scope)
            assert isinstance(prepared, CleanupPreparation)
            assert [item.job_id for item in prepared.selectors] == sorted([first, second])
            assert {item.disposition for item in prepared.selectors} == {
                "terminal_cleanup" if initial == COMPLETED else "cancel_unpermitted"
            }
            assert prepared.rule_targets == planner.prepare(scope)
            assert tuple(field.name for field in fields(CleanupSelector)) == (
                "job_id",
                "request_id",
                "input_digest",
                "disposition",
            )
            encoded = json.dumps(
                {
                    "selectors": [asdict(item) for item in prepared.selectors],
                    "rule_targets": [target.model_dump() for target in prepared.rule_targets],
                }
            )
            for item in event["content"]["messages"]:
                assert item["text"] not in encoded
            assert "message_texts" not in encoded and "payload" not in encoded
            with pytest.raises(FrozenInstanceError):
                prepared.selectors[0].job_id = str(uuid4())
            with pytest.raises(FrozenInstanceError):
                prepared.selectors = ()
            assert not hasattr(prepared, "__dict__")
            assert not hasattr(prepared.selectors[0], "__dict__")
            assert state(queue, first) == state(queue, second) == initial
            operation = signed_scope(planner, scope, prepared.rule_targets)
            assert checked(config, planner, operation, signed=True) == prepared

    asyncio.run(scenario())


def test_missing_witness_refuses_without_repair_then_explicit_verification_allows_retry(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner = coordinator(app)
            scope = confirmed(app, event, [job])
            with planner.admissions.access._connection(writing=True) as db:
                db.execute("DELETE FROM cmh_admission_witnesses")
            with pytest.raises(GovernanceError, match="^governance_admission_witness_missing$"):
                checked(config, planner, scope)
            assert witness_rows(planner) == []
            planner.admissions.verify_all()
            assert len(witness_rows(planner)) == 1
            assert checked(config, planner, scope).selectors[0].job_id == job

    asyncio.run(scenario())


def test_lagging_valid_witness_reads_latest_possible_proof_without_advancing_floor(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            saved = witness_rows(planner)[0]
            may_invoke(planner, queue, job)
            with planner.admissions.access._connection(writing=True) as db:
                db.execute("DELETE FROM cmh_admission_witnesses")
                db.execute("INSERT INTO cmh_admission_witnesses VALUES (?,?,?,?)", saved)
            before = all_rows(config, planner)
            with planner.admissions.access._connection(writing=True) as db:
                db.execute("PRAGMA query_only=ON")
                proof = planner.admissions.proof_in(
                    db, queue.payload_for(job).idempotency_key, refresh_witness=False
                )
            assert proof.state == "possible" and proof.checkpoint.sequence == 2
            assert all_rows(config, planner) == before
            with pytest.raises(GovernanceError, match="^governance_inflight_invocation_unproven$"):
                checked(config, planner, scope)
            assert witness_rows(planner) == [saved]
            planner.admissions.verify_all()
            assert json.loads(witness_rows(planner)[0][2])["sequence"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["ahead", "signature", "birth", "fork"])
def test_witness_ahead_fork_or_tampering_never_becomes_readonly_proof(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            if damage == "ahead":
                backup = snapshot(planner.admissions.journal.path)
                try:
                    may_invoke(planner, queue, job)
                    restore(backup, planner.admissions.journal.path)
                finally:
                    backup.close()
                reason = "governance_admission_watermark_ahead"
            else:
                with planner.admissions.access._connection(writing=True) as db:
                    if damage == "signature":
                        db.execute("UPDATE cmh_admission_witnesses SET signature=?", ("0" * 64,))
                    elif damage == "birth":
                        db.execute("UPDATE cmh_admission_witnesses SET birth_id=?", (str(uuid4()),))
                    else:
                        row = dict(db.execute("SELECT * FROM cmh_admission_witnesses").fetchone())
                        checkpoint = json.loads(row["checkpoint"])
                        checkpoint["digest"] = "0" * 64
                        payload = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
                        signature = planner.admissions.journal._mac(
                            "admission-witness", [row["event_key"], row["birth_id"], payload]
                        )
                        db.execute(
                            "UPDATE cmh_admission_witnesses SET checkpoint=?,signature=?",
                            (payload, signature),
                        )
                reason = "governance_admission_witness_invalid"
            with pytest.raises(GovernanceError, match="^" + reason + "$"):
                checked(config, planner, scope)
            assert state(queue, job) == PENDING

    asyncio.run(scenario())


def test_completed_reaped_inputs_keep_original_selector_without_apply_or_queue_mutation(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            prepared = checked(config, planner, scope)
            operation = signed_scope(planner, scope, prepared.rule_targets)
            planner.apply(operation)
            selector = prepared.selectors[0]
            assert (
                queue.clear_terminal_payload_for_governance(
                    job,
                    expected_request_id=selector.request_id,
                    expected_payload_digest=selector.input_digest,
                )
                == "reaped"
            )
            with planner.ingest._database(writing=True) as db:
                db.execute("DELETE FROM cmh_event_receipts WHERE receipt_id=?", (job,))
            inventory, inputs = collect_inflight_evidence(
                planner.ingest, operation, journal=planner.journal, anchored_only=True
            )
            assert (
                inputs == {}
                and inventory.confirmed_original_candidates[0].event_proof == "unavailable"
            )

            def forbidden(*args, **kwargs):
                raise AssertionError("readonly proof invoked a mutator")

            monkeypatch.setattr(planner, "apply", forbidden)
            monkeypatch.setattr(queue, "cancel_unpermitted_for_governance", forbidden)
            monkeypatch.setattr(queue, "clear_terminal_payload_for_governance", forbidden)
            assert checked(config, planner, operation, signed=True, completed=True) == prepared
            assert state(queue, job) == CANCELLED and queue.payload_for(job) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["digest", "request", "disposition", "missing_job"])
def test_signed_selectors_require_complete_exact_whole_request_certificates(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            prepared = checked(config, planner, scope)
            candidate = collect_inflight_evidence(planner.ingest, scope, journal=planner.journal)[
                0
            ].confirmed_original_candidates[0]
            targets = prepared.rule_targets
            if damage != "missing_job":
                original = inflight_job_target(planner.journal, scope.scope_id, candidate)
                changed_candidate = replace(
                    candidate,
                    **{"input_digest": "0" * 64}
                    if damage == "digest"
                    else {
                        "request_id": candidate.request_id.replace(
                            candidate.request_id.split(":")[2], str(uuid4())
                        )
                    }
                    if damage == "request"
                    else {},
                )
                changed_target = inflight_job_target(
                    planner.journal,
                    scope.scope_id,
                    changed_candidate,
                    disposition="terminal_cleanup"
                    if damage == "disposition"
                    else "cancel_unpermitted",
                )
                targets = tuple(
                    changed_target if target == original else target for target in targets
                )
            operation = signed_scope(planner, scope, targets)
            if damage == "missing_job":
                with sqlite3.connect(queue.database_path) as db:
                    db.execute("DELETE FROM memory_write_jobs WHERE job_id=?", (job,))
            with pytest.raises(GovernanceError):
                checked(config, planner, operation, signed=True)

    asyncio.run(scenario())


def test_snapshot_only_and_invalid_completed_mode_cannot_create_whole_selectors(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            with pytest.raises(GovernanceError, match="^governance_inflight_scope_invalid$"):
                checked(config, planner, scope, completed=True)
            queue.settle(job, state=COMPLETED)
            snapshot_scope = replace(confirmed(app, event, []), records={"cmhj:" + job: "raw"})
            operation = signed_scope(planner, snapshot_scope, planner.prepare(snapshot_scope))
            result = checked(config, planner, operation, signed=True, completed=True)
            assert result.selectors == ()

    asyncio.run(scenario())


def test_query_only_is_a_write_firewall_not_a_rollback_strategy(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner = coordinator(app)

            def accidental_mutation(db, *args, **kwargs):
                assert db.execute("PRAGMA query_only").fetchone()[0] == 1
                before = db.total_changes
                try:
                    db.execute("DELETE FROM cmh_admission_witnesses")
                finally:
                    assert db.total_changes == before == 0

            monkeypatch.setattr(planner, "_prove_live", accidental_mutation)
            with pytest.raises(GovernanceError, match="^governance_inflight_store_unavailable$"):
                checked(config, planner, confirmed(app, event, [job]))

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["prepare", "apply"])
def test_existing_entrypoints_preserve_proof_storage_error_contract(tmp_path, monkeypatch, method):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner = coordinator(app)
            scope = confirmed(app, event, [job])
            if method == "apply":
                scope = signed_scope(planner, scope, planner.prepare(scope))
            before = all_rows(config, planner)

            def unavailable(*args, **kwargs):
                raise sqlite3.OperationalError("synthetic unavailable proof store")

            monkeypatch.setattr(planner, "_prove_live", unavailable)
            with pytest.raises(GovernanceError, match="^governance_inflight_store_unavailable$"):
                getattr(planner, method)(scope)
            assert all_rows(config, planner) == before

    asyncio.run(scenario())


def test_apply_rejects_invalid_certificate_before_its_additional_queue_access(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner = coordinator(app)
            scope = confirmed(app, event, [job])
            prepared = checked(config, planner, scope)
            candidate = collect_inflight_evidence(planner.ingest, scope, journal=planner.journal)[
                0
            ].confirmed_original_candidates[0]
            certificate = inflight_job_target(planner.journal, scope.scope_id, candidate)
            operation = signed_scope(
                planner, scope, tuple(item for item in prepared.rule_targets if item != certificate)
            )
            original = planner.ingest._queue
            queue_accesses = []

            def queue_access():
                queue_accesses.append(True)
                assert len(queue_accesses) == 1, "invalid scope reached apply queue access"
                return original()

            monkeypatch.setattr(planner.ingest, "_queue", queue_access)
            before = all_rows(config, planner)
            with pytest.raises(GovernanceError, match="^governance_inflight_binding_mismatch$"):
                planner.apply(operation)
            assert len(queue_accesses) == 1  # The existing read-only evidence collection.
            assert all_rows(config, planner) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_cas", [False, True])
def test_apply_keeps_writer_lock_through_cas_and_existing_queue_error(
    tmp_path, monkeypatch, fail_cas
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            operation = signed_scope(planner, scope, planner.prepare(scope))
            original = queue.cancel_unpermitted_for_governance
            attempts = []

            def cancel(identity, **kwargs):
                with sqlite3.connect(planner.admissions.access.path, timeout=0) as competing:
                    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                        competing.execute("BEGIN IMMEDIATE")
                attempts.append(identity)
                if fail_cas:
                    raise TypeError("synthetic invalid queue CAS")
                return original(identity, **kwargs)

            monkeypatch.setattr(queue, "cancel_unpermitted_for_governance", cancel)
            if fail_cas:
                before = all_rows(config, planner)
                with pytest.raises(GovernanceError, match="^governance_inflight_queue_unproven$"):
                    planner.apply(operation)
                assert all_rows(config, planner) == before
            else:
                assert planner.apply(operation)[0].job_id == job
                assert state(queue, job) == CANCELLED
            assert attempts == [job]

    asyncio.run(scenario())
