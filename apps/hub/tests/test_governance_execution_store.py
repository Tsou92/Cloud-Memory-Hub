"""ADR-0043 real SQLite/journal invariants, including interrupted migration."""

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority, RepairParent
from cloud_memory_hub.governance_diagnostic_inventory import (
    DiagnosticInventory,
    DomainObservation,
    FileObservation,
)
from cloud_memory_hub.governance_execution_store import (
    ArtifactAbsence,
    ArtifactEntry,
    CleanupManifest,
    CleanupVerification,
    ExecutionStore,
    manifest_digest,
    manifest_from_dict,
    manifest_to_dict,
    stage_name,
)
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal, Intent
from test_diagnostic_confirmation import authority_for, intent_for


@pytest.fixture
def journal(tmp_path):
    return GovernanceJournal.initialize(
        tmp_path / "authority" / "journal.sqlite3", instance_id=str(uuid4()), key=os.urandom(32)
    )


def make_intent(journal, *, action="forget", record="synthetic-memory", scope=None):
    scope = scope or str(uuid4())
    return Intent(
        operation_id=str(uuid4()),
        actor_id=journal.instance_id,
        scope_id=scope,
        action=action,
        targets=(journal.target(scope, "memory", record),),
    )


def legacy_store(journal, entries):
    """The shipped v1 schema and its unchanged v1 row-signature contract."""
    path = journal.path.with_name("execution.sqlite3")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE operations (operation_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,"
            "intent TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('planned','completed')),"
            "signature TEXT NOT NULL);"
            "CREATE TABLE suppressed (operation_id TEXT NOT NULL "
            "REFERENCES operations(operation_id),scope_id TEXT NOT NULL,record_id TEXT NOT NULL,"
            "kind TEXT NOT NULL,signature TEXT NOT NULL,"
            "PRIMARY KEY(operation_id,record_id));"
            "CREATE INDEX suppressed_scope ON suppressed(scope_id,record_id);"
            "PRAGMA user_version=1;"
        )
        for intent, state, records in entries:
            values = [intent.operation_id, intent.scope_id, intent.model_dump_json(), state]
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (*values, journal._mac("execution-v1", ["operations", *values])),
            )
            for identity, kind in sorted(records.items()):
                values = [intent.operation_id, intent.scope_id, identity, kind]
                db.execute(
                    "INSERT INTO suppressed VALUES (?,?,?,?,?)",
                    (*values, journal._mac("execution-v1", ["suppressed", *values])),
                )
    return ExecutionStore(journal)


def database_rows(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            "version": db.execute("PRAGMA user_version").fetchone()[0],
            "tables": {
                name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for name in sorted(names)
            },
        }


def test_inspect_legacy_is_pure_complete_and_distinguishes_uncommitted(journal):
    first = make_intent(journal)
    journal.append(first)
    orphan = make_intent(journal, record="orphan")
    store = legacy_store(
        journal,
        [
            (first, "completed", {"synthetic-memory": "memory"}),
            (orphan, "planned", {"orphan": "memory"}),
        ],
    )
    before = database_rows(store.path), database_rows(journal.path)
    result = store.inspect_legacy()
    assert result.committed == (first.operation_id,)
    assert result.orphans == (orphan.operation_id,)
    assert result.operations[first.operation_id].records == {"synthetic-memory": "memory"}
    assert (database_rows(store.path), database_rows(journal.path)) == before
    assert "synthetic-memory" not in json.dumps(
        {"source": result.source_digest, "journal": result.journal_digest}
    )
    with pytest.raises(TypeError):
        result.operations[first.operation_id].records["additional"] = "raw"


@pytest.mark.parametrize("damage", ["record", "missing_record", "missing_operation", "state"])
def test_inspect_legacy_rejects_edit_or_missing_set(journal, damage):
    value = make_intent(journal)
    journal.append(value)
    store = legacy_store(journal, [(value, "completed", {"synthetic-memory": "memory"})])
    with sqlite3.connect(store.path) as db:
        if damage == "record":
            db.execute("UPDATE suppressed SET record_id='replacement'")
        elif damage == "missing_record":
            db.execute("DELETE FROM suppressed")
        elif damage == "missing_operation":
            db.execute("DELETE FROM operations")
        else:
            db.execute("UPDATE operations SET state='planned'")
    with pytest.raises(GovernanceError, match="execution_tampered"):
        store.inspect_legacy()


def test_inspect_legacy_rejects_signed_completed_without_real_intent(journal):
    value = make_intent(journal)
    store = legacy_store(journal, [(value, "completed", {"synthetic-memory": "memory"})])
    with pytest.raises(GovernanceError, match="execution_tampered"):
        store.inspect_legacy()


def test_inspect_legacy_refuses_main_intent_without_any_plan(journal):
    journal.append(make_intent(journal))
    store = legacy_store(journal, [])
    with pytest.raises(GovernanceError, match="execution_pending"):
        store.inspect_legacy()


def test_read_only_never_initializes_missing_execution_database(journal):
    store = ExecutionStore(journal, read_only=True)
    with pytest.raises(GovernanceError, match="execution_unavailable"):
        store.inspect_legacy()
    with pytest.raises(GovernanceError, match="execution_read_only"):
        store.initialize_empty()
    assert not store.path.exists()


def bound_store(journal):
    journal.upgrade_v2(str(uuid4()))
    store = ExecutionStore(journal)
    binding = store.initialize_empty()
    assert journal.execution_store_id() is None
    assert store.activation_binding() == binding
    journal.bind_execution_store(binding.store_id)
    assert store.require_capability() == binding
    return store


def planned_purge(journal, store, *, commit=True):
    authority = authority_for(journal)
    intent, records = intent_for(journal, authority)
    store.plan(intent, records, cleanup=authority)
    if commit:
        journal.append(intent)
    return intent, authority


def file_plan(operation_id):
    # Synthetic metadata tests the storage protocol only; native file rewriting
    # and its retained-byte observations are exercised by the engine tests.
    before = FileObservation(
        "sdk",
        "hy_memory.log",
        1,
        2,
        12,
        100,
        100,
        sha256(b"target\nkeep\n").hexdigest(),
        2,
        1,
        0,
        1,
    )
    pass_id = str(uuid4())
    entry = ArtifactEntry(
        before, sha256(b"keep\n").hexdigest(), 5, stage_name(operation_id, pass_id, 0)
    )
    return CleanupManifest(operation_id, pass_id, (entry,))


def absence(receipt):
    names = (
        ()
        if receipt.manifest is None
        else tuple(
            sorted((entry.before.domain, entry.stage_name) for entry in receipt.manifest.artifacts)
        )
    )
    return ArtifactAbsence(
        receipt.operation_id, receipt.pass_id, manifest_digest(receipt.manifest), names
    )


def final_inventory(journal, manifest):
    files = tuple(
        replace(
            item.before,
            sha256=item.retained_sha256,
            byte_length=item.retained_bytes,
            record_count=1,
            kept_count=1,
            candidate_count=0,
            removed_count=0,
        )
        for item in manifest.artifacts
    )
    return DiagnosticInventory(
        (
            DomainObservation("pipeline", journal.path.parent, True, 1, 1),
            DomainObservation("sdk", journal.path.parent, True, 1, 1),
        ),
        files,
        (),
        sum(item.byte_length for item in files),
        sum(item.record_count for item in files),
    )


def finish_pass(journal, store, receipt, manifest=None):
    manifest = manifest or CleanupManifest(receipt.operation_id, str(uuid4()), ())
    receipt = store.begin_pass(receipt.operation_id, expected=receipt, manifest=manifest)
    finished = replace(
        manifest, artifacts=tuple(replace(entry, state="verified") for entry in manifest.artifacts)
    )
    receipt = store.checkpoint(receipt, manifest=finished)
    return store.verify_pass(
        receipt,
        verification=CleanupVerification(final_inventory(journal, finished), absence(receipt)),
    )


def test_explicit_initialize_capability_gap_and_missing_store_are_not_repaired(journal):
    journal.upgrade_v2(str(uuid4()))
    store = ExecutionStore(journal)
    binding = store.initialize_empty()
    value = make_intent(journal)
    before = database_rows(store.path), database_rows(journal.path)
    with pytest.raises(GovernanceError, match="binding_mismatch"):
        store.plan(value, {"synthetic-memory": "memory"})
    with pytest.raises(GovernanceError, match="binding_mismatch"):
        store.require_capability()
    assert (database_rows(store.path), database_rows(journal.path)) == before
    assert ExecutionStore(journal, read_only=True).activation_binding() == binding
    journal.bind_execution_store(binding.store_id)
    store.path.unlink()
    with pytest.raises(GovernanceError):
        store.require_capability()
    with pytest.raises(GovernanceError, match="binding_exists"):
        store.initialize_empty()
    assert not store.path.exists()


def test_new_forget_reuses_signatures_and_requires_actual_commit(journal):
    store = bound_store(journal)
    value = make_intent(journal)
    operation = store.plan(value, {"synthetic-memory": "memory"})
    assert operation.cleanup is None
    assert store.suppressed(value.scope_id) == {"synthetic-memory": "memory"}
    assert store.recovery_snapshot().orphans == (value.operation_id,)
    with pytest.raises(GovernanceError, match="uncommitted"):
        store.complete(value)
    journal.append(value)
    store.complete(value)
    assert not store.has_pending()
    assert store.records(value.operation_id) == ["synthetic-memory"]
    assert store.record_kinds(value.operation_id) == {"synthetic-memory": "memory"}
    assert store.operations()[value.operation_id] == ("completed", value)


def test_new_purge_full_cas_completion_and_replay_pending(journal):
    store = bound_store(journal)
    value, authority = planned_purge(journal, store)
    initial = store.cleanup(value.operation_id)
    assert initial.authority == authority and initial.revision == 0
    with pytest.raises(GovernanceError, match="cleanup_conflict"):
        store.complete(value)
    with pytest.raises(GovernanceError, match="cleanup_incomplete"):
        store.complete(value, expected_cleanup=initial)
    verified = finish_pass(journal, store, initial, file_plan(value.operation_id))
    assert verified.state == "verified" and verified.revision == 3
    store.complete(value, expected_cleanup=verified)
    assert not store.has_pending()
    assert store.suppressed(value.scope_id) == {
        "cmhe:" + selector.job_id: "purged" for selector in authority.selectors
    }
    # An old verified row is not an exemption from a new process's file scan.
    reopened = ExecutionStore(journal)
    replay = CleanupManifest(value.operation_id, str(uuid4()), ())
    with pytest.raises(GovernanceError, match="artifact_unproven"):
        reopened.begin_pass(value.operation_id, expected=verified, manifest=replay)
    receipt = reopened.begin_pass(
        value.operation_id, expected=verified, manifest=replay, artifact_absence=absence(verified)
    )
    assert receipt.state == "pending" and reopened.has_pending()
    assert reopened.operations()[value.operation_id][0] == "completed"
    with pytest.raises(GovernanceError, match="cleanup_conflict"):
        reopened.complete(value, expected_cleanup=verified)


def test_plan_exact_idempotency_does_not_accept_different_record_map(journal):
    store = bound_store(journal)
    value = make_intent(journal)
    first = store.plan(value, {"synthetic-memory": "memory"})
    assert store.plan(value, {"synthetic-memory": "memory"}) == first
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="operation_conflict"):
        store.plan(value, {"different": "memory"})
    assert database_rows(store.path) == before


def test_signed_plan_without_main_intent_cannot_register_artifacts(journal):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store, commit=False)
    receipt = store.cleanup(value.operation_id)
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="uncommitted"):
        store.begin_pass(
            value.operation_id, expected=receipt, manifest=file_plan(value.operation_id)
        )
    assert database_rows(store.path) == before


def test_same_value_progress_updates_invalidate_previous_receipts(journal):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    manifest = file_plan(value.operation_id)
    current = store.begin_pass(
        value.operation_id, expected=store.cleanup(value.operation_id), manifest=manifest
    )
    next_receipt = store.checkpoint(current, manifest=manifest)
    assert current.manifest == next_receipt.manifest
    assert current.signature != next_receipt.signature
    assert current.revision + 1 == next_receipt.revision
    with pytest.raises(GovernanceError, match="cleanup_conflict"):
        store.checkpoint(current, manifest=manifest)


@pytest.mark.parametrize("damage", ["retained", "stage", "before", "new_file", "backward"])
def test_checkpoint_cannot_change_registered_plan(journal, damage):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    manifest = file_plan(value.operation_id)
    current = store.begin_pass(
        value.operation_id, expected=store.cleanup(value.operation_id), manifest=manifest
    )
    manifest = replace(manifest, artifacts=(replace(manifest.artifacts[0], state="staged"),))
    current = store.checkpoint(current, manifest=manifest)
    item = manifest.artifacts[0]
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="manifest_invalid"):
        if damage == "retained":
            changed = replace(item, retained_sha256="a" * 64)
        elif damage == "stage":
            changed = replace(item, stage_name="../foreign-file")
        elif damage == "before":
            changed = replace(item, before=replace(item.before, inode=999))
        elif damage == "backward":
            changed = replace(item, state="registered")
        else:
            changed = replace(item, before=replace(item.before, key="hy_memory.log.2026-11-09"))
        store.checkpoint(current, manifest=replace(manifest, artifacts=(changed,)))
    assert database_rows(store.path) == before


@pytest.mark.parametrize("damage", ["retained_hash", "retained_bytes", "stage", "target_present"])
def test_verify_requires_retained_bytes_and_precise_artifact_absence(journal, damage):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    manifest = file_plan(value.operation_id)
    current = store.begin_pass(
        value.operation_id, expected=store.cleanup(value.operation_id), manifest=manifest
    )
    manifest = replace(manifest, artifacts=(replace(manifest.artifacts[0], state="verified"),))
    current = store.checkpoint(current, manifest=manifest)
    inventory, absent = final_inventory(journal, manifest), absence(current)
    if damage == "retained_hash":
        inventory = replace(inventory, files=(replace(inventory.files[0], sha256="a" * 64),))
    elif damage == "retained_bytes":
        inventory = replace(
            inventory, files=(replace(inventory.files[0], byte_length=4),), byte_length=4
        )
    elif damage == "stage":
        absent = replace(absent, absent=())
    else:
        inventory = replace(
            inventory, files=(replace(inventory.files[0], kept_count=0, removed_count=1),)
        )
    with pytest.raises(GovernanceError):
        store.verify_pass(current, verification=CleanupVerification(inventory, absent))
    assert store.cleanup(value.operation_id) == current


def test_legacy_migration_preserves_old_authority_and_resumes_capability_gap(journal):
    journal.upgrade_v2(str(uuid4()))
    source = authority_for(journal)
    legacy = CleanupAuthority("legacy", source.selectors, ())
    value, records = intent_for(journal, legacy)
    journal.append(value)
    store = legacy_store(journal, [(value, "completed", records)])
    before_execution, before_journal = database_rows(store.path), database_rows(journal.path)
    observation = store.inspect_legacy()
    binding = store.migrate_legacy(observation, {value.operation_id: legacy})
    after = database_rows(store.path)
    assert after["version"] == 2
    assert after["tables"]["operations"] == before_execution["tables"]["operations"]
    assert after["tables"]["suppressed"] == before_execution["tables"]["suppressed"]
    assert database_rows(journal.path) == before_journal
    assert journal.execution_store_id() is None
    assert store.cleanup(value.operation_id).authority.grants == ()
    assert store.operations()[value.operation_id][0] == "completed"
    assert store.has_pending()  # The completed business row cannot mask cleanup.
    with pytest.raises(GovernanceError, match="binding_mismatch"):
        store.require_capability()
    reopened = ExecutionStore(journal, read_only=True)
    untouched = database_rows(store.path), database_rows(journal.path)
    assert reopened.activation_binding() == binding
    assert (database_rows(store.path), database_rows(journal.path)) == untouched
    journal.bind_execution_store(binding.store_id)
    assert store.require_capability() == binding
    assert store.activation_binding() == binding
    assert store.recovery_snapshot().committed == (value.operation_id,)
    verified = finish_pass(journal, store, store.cleanup(value.operation_id))
    store.complete(value, expected_cleanup=verified)
    assert not store.has_pending()
    assert database_rows(journal.path)["tables"]["intents"] == before_journal["tables"]["intents"]


def test_migration_requires_every_committed_selector_before_any_schema_change(journal):
    source = authority_for(journal)
    valid = CleanupAuthority("legacy", source.selectors, ())
    value, records = intent_for(journal, valid)
    journal.append(value)
    store = legacy_store(journal, [(value, "completed", records)])
    observation, before = store.inspect_legacy(), database_rows(store.path)
    with pytest.raises(GovernanceError, match="confirmation_invalid"):
        store.migrate_legacy(observation, {value.operation_id: CleanupAuthority("legacy", (), ())})
    with pytest.raises(GovernanceError, match="migration_invalid"):
        store.migrate_legacy(observation, {})
    with pytest.raises(GovernanceError, match="migration_invalid"):
        store.migrate_legacy(observation, {value.operation_id: source})
    assert database_rows(store.path) == before


def test_migration_source_change_or_failure_rolls_back_all_ddl(journal, monkeypatch):
    legacy = CleanupAuthority("legacy", (), ())
    first, records = intent_for(journal, legacy)
    second, other = intent_for(journal, legacy)
    journal.append(first)
    journal.append(second)
    store = legacy_store(journal, [(first, "completed", records), (second, "planned", other)])
    snapshot, before = store.inspect_legacy(), database_rows(store.path)
    original = store._insert_cleanup
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("synthetic transaction interruption")

    monkeypatch.setattr(store, "_insert_cleanup", interrupted)
    with pytest.raises(GovernanceError, match="execution_unavailable"):
        store.migrate_legacy(snapshot, {first.operation_id: legacy, second.operation_id: legacy})
    assert calls == 2 and database_rows(store.path) == before
    monkeypatch.setattr(store, "_insert_cleanup", original)
    journal.append(make_intent(journal))
    with pytest.raises(GovernanceError, match="migration_changed"):
        store.migrate_legacy(snapshot, {first.operation_id: legacy, second.operation_id: legacy})
    assert database_rows(store.path) == before


def test_uncommitted_purge_migration_does_not_require_lost_business_body(journal):
    journal.upgrade_v2(str(uuid4()))
    job_id, scope = str(uuid4()), str(uuid4())
    value = Intent(
        operation_id=str(uuid4()),
        actor_id=journal.instance_id,
        scope_id=scope,
        action="purge",
        targets=(journal.target(scope, "raw", "cmhe:" + job_id),),
    )
    # This old orphan predates certificates and has no committed deletion right.
    store = legacy_store(journal, [(value, "planned", {"cmhe:" + job_id: "raw"})])
    binding = store.migrate_legacy(
        store.inspect_legacy(), {value.operation_id: CleanupAuthority("legacy", (), ())}
    )
    journal.bind_execution_store(binding.store_id)
    receipt = store.cleanup(value.operation_id)
    assert receipt.orphan_only and receipt.authority.selectors == ()
    with pytest.raises(GovernanceError, match="uncommitted"):
        store.begin_pass(
            value.operation_id,
            expected=receipt,
            manifest=CleanupManifest(value.operation_id, str(uuid4()), ()),
        )
    with pytest.raises(GovernanceError, match="artifact_unproven"):
        store.discard_uncommitted(value.operation_id, expected_cleanup=receipt)
    store.discard_uncommitted(
        value.operation_id, expected_cleanup=receipt, artifact_absence=absence(receipt)
    )
    assert store.operations() == {} and journal.scopes() == ()


def test_migrated_orphan_never_upgrades_into_authority(journal):
    journal.upgrade_v2(str(uuid4()))
    value = make_intent(journal, action="purge")
    store = legacy_store(journal, [(value, "planned", {"synthetic-memory": "memory"})])
    binding = store.migrate_legacy(
        store.inspect_legacy(), {value.operation_id: CleanupAuthority("legacy", (), ())}
    )
    journal.bind_execution_store(binding.store_id)
    receipt = store.cleanup(value.operation_id)
    journal.append(value)
    with pytest.raises(GovernanceError, match="execution_tampered"):
        store.recovery_snapshot()
    with pytest.raises(GovernanceError, match="execution_tampered"):
        store.discard_uncommitted(
            value.operation_id, expected_cleanup=receipt, artifact_absence=absence(receipt)
        )


def test_repair_has_new_intent_and_preserves_parent_grants(journal):
    journal.upgrade_v2(str(uuid4()))
    source = authority_for(journal)
    legacy = CleanupAuthority("legacy", source.selectors, ())
    parent, records = intent_for(journal, legacy)
    parent_checkpoint = journal.append(parent)
    store = legacy_store(journal, [(parent, "completed", records)])
    binding = store.migrate_legacy(store.inspect_legacy(), {parent.operation_id: legacy})
    journal.bind_execution_store(binding.store_id)
    parent_before = store.cleanup(parent.operation_id)
    repair = CleanupAuthority(
        "repair",
        source.selectors,
        source.grants,
        RepairParent(parent.operation_id, parent_checkpoint.digest, journal.ledger_id),
    )
    value, repair_records = intent_for(journal, repair, scope=parent.scope_id)
    assert repair_records == {}
    store.plan(value, repair_records, cleanup=repair)
    with pytest.raises(GovernanceError, match="uncommitted"):
        store.begin_pass(
            value.operation_id,
            expected=store.cleanup(value.operation_id),
            manifest=CleanupManifest(value.operation_id, str(uuid4()), ()),
        )
    journal.append(value)
    verified = finish_pass(
        journal, store, store.cleanup(value.operation_id), file_plan(value.operation_id)
    )
    store.complete(value, expected_cleanup=verified)
    assert store.cleanup(parent.operation_id) == parent_before
    assert store.operations()[parent.operation_id] == ("completed", parent)
    assert store.record_kinds(value.operation_id) == {}
    assert store.has_pending()  # Parent still needs an actual fresh coverage pass.
    recovery = store.recovery_snapshot()
    assert set(recovery.committed) == {parent.operation_id, value.operation_id}


@pytest.mark.parametrize("damage", ["digest", "scope", "selectors", "uncommitted", "forget"])
def test_repair_cannot_infer_authority_from_a_locally_signed_plan(journal, damage):
    store = bound_store(journal)
    parent, source = planned_purge(journal, store, commit=damage != "uncommitted")
    parent_digest = (
        "0" * 64 if damage in {"digest", "uncommitted"} else (journal.head(parent.scope_id).digest)
    )
    if damage == "forget":
        parent = make_intent(journal)
        store.plan(parent, {"synthetic-memory": "memory"})
        parent_digest = journal.append(parent).digest
    repair = CleanupAuthority(
        "repair",
        source.selectors,
        source.grants,
        RepairParent(parent.operation_id, parent_digest, journal.ledger_id),
    )
    if damage == "selectors":
        repair = replace(repair, selectors=(replace(repair.selectors[0], input_digest="e" * 64),))
    value, records = intent_for(
        journal, repair, scope=str(uuid4()) if damage == "scope" else parent.scope_id
    )
    before = database_rows(store.path), database_rows(journal.path)
    with pytest.raises(GovernanceError, match="parent_invalid"):
        store.plan(value, records, cleanup=repair)
    assert (database_rows(store.path), database_rows(journal.path)) == before


@pytest.mark.parametrize(
    "damage", ["cleanup_missing", "operation_missing", "binding", "progress", "grant"]
)
def test_schema2_detects_missing_or_edited_rows_in_every_read(journal, damage):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    with sqlite3.connect(store.path) as db:
        if damage == "cleanup_missing":
            db.execute("DELETE FROM cleanup")
        elif damage == "operation_missing":
            db.execute("DELETE FROM operations")
        elif damage == "binding":
            db.execute("UPDATE binding SET store_id=?", (str(uuid4()),))
        elif damage == "progress":
            db.execute("UPDATE cleanup SET state='verified'")
        else:
            payload = json.loads(db.execute("SELECT immutable_json FROM cleanup").fetchone()[0])
            payload["authority"]["grants"][0]["sha256"] = "e" * 64
            db.execute("UPDATE cleanup SET immutable_json=?", (json.dumps(payload),))
    for read in (
        store.snapshot,
        store.operations,
        lambda: store.records(value.operation_id),
        lambda: store.record_kinds(value.operation_id),
        lambda: store.suppressed(value.scope_id),
        store.has_pending,
        store.require_capability,
    ):
        with pytest.raises(GovernanceError):
            read()


def test_discard_requires_actual_uncommitted_intent_and_no_artifact(journal):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store, commit=False)
    receipt = store.cleanup(value.operation_id)
    with pytest.raises(GovernanceError, match="artifact_unproven"):
        store.discard_uncommitted(value.operation_id, expected_cleanup=receipt)
    journal.append(value)
    with pytest.raises(GovernanceError, match="operation_committed"):
        store.discard_uncommitted(
            value.operation_id, expected_cleanup=receipt, artifact_absence=absence(receipt)
        )


def test_all_mutators_reject_read_only_before_starting_transactions(journal):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    receipt = store.cleanup(value.operation_id)
    readonly = ExecutionStore(journal, read_only=True)
    before = database_rows(store.path), database_rows(journal.path)
    manifest = CleanupManifest(value.operation_id, str(uuid4()), ())
    calls = (
        lambda: readonly.initialize_empty(),
        lambda: readonly.plan(make_intent(journal), {"synthetic-memory": "memory"}),
        lambda: readonly.begin_pass(value.operation_id, expected=receipt, manifest=manifest),
        lambda: readonly.checkpoint(receipt, manifest=manifest),
        lambda: readonly.verify_pass(receipt, verification=None),
        lambda: readonly.complete(value, expected_cleanup=receipt),
        lambda: readonly.discard_uncommitted(value.operation_id),
        lambda: readonly.migrate_legacy(None, {}),
    )
    for call in calls:
        with pytest.raises(GovernanceError, match="execution_read_only"):
            call()
    assert (database_rows(store.path), database_rows(journal.path)) == before


def test_manifest_closed_shape_rejects_paths_and_unexpected_fields(journal):
    value = file_plan(str(uuid4()))
    assert manifest_from_dict(manifest_to_dict(value)) == value
    payload = manifest_to_dict(value)
    payload["artifacts"][0]["before"]["key"] = "../foreign.log"
    with pytest.raises(GovernanceError, match="manifest_invalid"):
        manifest_from_dict(payload)
    payload = manifest_to_dict(value)
    payload["artifacts"][0]["body"] = "must never enter progress"
    with pytest.raises(GovernanceError, match="manifest_invalid"):
        manifest_from_dict(payload)


def test_real_concurrent_cas_has_exactly_one_winner(journal):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    manifest = file_plan(value.operation_id)
    receipt = store.begin_pass(
        value.operation_id, expected=store.cleanup(value.operation_id), manifest=manifest
    )
    barrier = threading.Barrier(2)

    def advance():
        barrier.wait(timeout=5)
        try:
            return ExecutionStore(journal).checkpoint(receipt, manifest=manifest).revision
        except GovernanceError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: advance(), range(2)))
    assert sorted(results, key=str) == [2, "governance_cleanup_conflict"]
    assert store.cleanup(value.operation_id).revision == 2


def test_verified_cache_restore_is_still_in_every_startup_rescan_set(journal):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    verified = finish_pass(journal, store, store.cleanup(value.operation_id))
    assert verified.verification is not None
    store.complete(value, expected_cleanup=verified)
    with sqlite3.connect(store.path) as db:
        saved = db.execute(
            "SELECT * FROM cleanup WHERE operation_id=?", (value.operation_id,)
        ).fetchone()
    pending = store.begin_pass(
        value.operation_id,
        expected=verified,
        manifest=CleanupManifest(value.operation_id, str(uuid4()), ()),
    )
    assert pending.verification is None and store.has_pending()
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM cleanup WHERE operation_id=?", (value.operation_id,))
        db.execute("INSERT INTO cleanup VALUES (?,?,?,?,?,?,?,?,?,?)", saved)
    reopened = ExecutionStore(journal)
    assert reopened.cleanup(value.operation_id) == verified
    # A coherent, valid old progress row cannot be detected by its own MAC.
    assert not reopened.has_pending()
    assert reopened.recovery_snapshot().purges_to_rescan == (value.operation_id,)


@pytest.mark.parametrize("damage", ["missing", "extra", "edited", "pending_has_evidence"])
def test_verification_summary_is_closed_and_signed(journal, damage):
    store = bound_store(journal)
    value, _ = planned_purge(journal, store)
    finish_pass(journal, store, store.cleanup(value.operation_id))
    with sqlite3.connect(store.path) as db:
        evidence = json.loads(db.execute("SELECT verification_json FROM cleanup").fetchone()[0])
        assert set(evidence) == {"version", "inventory_sha256", "artifact_absence_sha256"}
        assert str(journal.path.parent) not in json.dumps(evidence)
        if damage == "missing":
            db.execute("UPDATE cleanup SET verification_json='null'")
        elif damage == "extra":
            evidence["body"] = "synthetic forbidden field"
            db.execute("UPDATE cleanup SET verification_json=?", (json.dumps(evidence),))
        elif damage == "edited":
            evidence["inventory_sha256"] = "e" * 64
            db.execute("UPDATE cleanup SET verification_json=?", (json.dumps(evidence),))
        else:
            db.execute("UPDATE cleanup SET state='pending'")
    with pytest.raises(GovernanceError):
        store.snapshot()


def test_forget_orphan_discard_also_requires_actual_artifact_absence(journal):
    store = bound_store(journal)
    value = make_intent(journal)
    store.plan(value, {"synthetic-memory": "memory"})
    with pytest.raises(GovernanceError, match="artifact_unproven"):
        store.discard_uncommitted(value.operation_id)
    store.discard_uncommitted(
        value.operation_id,
        artifact_absence=ArtifactAbsence(value.operation_id, None, manifest_digest(None), ()),
    )
    assert store.operations() == {}
