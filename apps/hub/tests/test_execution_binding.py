"""ADR-0046 exact store selection and coherent, non-mutating runtime views."""

import os
import sqlite3
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
from cloud_memory_hub.governance_execution import ExecutionStore as LegacyExecutionStore
from cloud_memory_hub.governance_execution_binding import ExecutionBinding, select_execution_store
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal
from test_diagnostic_confirmation import authority_for, intent_for
from test_governance_execution_store import bound_store, database_rows, make_intent


@pytest.fixture
def journal(tmp_path):
    return GovernanceJournal.initialize(
        tmp_path / "governance" / "journal.sqlite3", instance_id=str(uuid4()), key=os.urandom(32)
    )


def add_legacy(journal, *, committed=True, completed=False, action="forget"):
    store = LegacyExecutionStore(journal)
    intent = make_intent(journal, action=action)
    store.plan(intent, {"synthetic-memory": "memory"})
    if committed:
        journal.append(intent)
    if completed:
        store.complete(intent)
    return store, intent


@pytest.mark.parametrize("read_only", [False, True])
def test_missing_empty_v1_is_lazy_and_selection_creates_nothing(journal, read_only):
    before = tuple(journal.path.parent.iterdir()), database_rows(journal.path)
    binding = select_execution_store(journal, read_only=read_only)
    assert binding.version == 1
    assert isinstance(binding.store, LegacyExecutionStore)
    assert binding.read_only == binding.store.read_only == read_only
    state = binding.read_state()
    assert not state.operations and state.pending == state.business_plans == ()
    assert binding.suppressed(str(uuid4())) == {}
    assert state.recovery is None
    assert not binding.store.path.exists()
    assert (tuple(journal.path.parent.iterdir()), database_rows(journal.path)) == before


def test_missing_v1_keeps_real_unplanned_intents_visible_without_creating_store(journal):
    intent = make_intent(journal)
    journal.append(intent)
    binding = select_execution_store(journal)
    state = binding.read_state()
    assert state.unplanned == (intent,)
    assert state.pending == (intent.operation_id,)
    assert not state.operations and state.business_plans == ()
    assert not binding.store.path.exists()


def test_v1_snapshot_keeps_orphans_and_unplanned_distinct(journal):
    store, committed = add_legacy(journal, completed=True)
    _, orphan = add_legacy(journal, committed=False, action="purge")
    unplanned = make_intent(journal)
    journal.append(unplanned)
    before = database_rows(store.path), database_rows(journal.path)
    binding = select_execution_store(journal)
    state = binding.read_state()
    assert state.committed == (committed.operation_id,)
    assert state.orphans == (orphan.operation_id,)
    assert state.unplanned == (unplanned,)
    assert state.pending == tuple(sorted((orphan.operation_id, unplanned.operation_id)))
    assert set(state.business_plans) == {committed.operation_id, orphan.operation_id}
    assert binding.suppressed(orphan.scope_id) == {"synthetic-memory": "purged"}
    with pytest.raises(GovernanceError, match="governance_execution_pending"):
        ExecutionStore(journal, read_only=True).inspect_legacy()
    assert (database_rows(store.path), database_rows(journal.path)) == before
    with pytest.raises(TypeError):
        state.operations[orphan.operation_id] = state.operations[committed.operation_id]
    with pytest.raises(FrozenInstanceError):
        state.pending = ()


def test_v1_records_and_intents_remain_one_actual_sqlite_snapshot(journal, monkeypatch):
    store, first = add_legacy(journal)
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    binding = select_execution_store(journal)
    second = make_intent(journal, record="later-memory")
    original, calls = ExecutionStore._old_snapshot, []

    def concurrent_plan(self, db):
        before = original(self, db)
        assert db.in_transaction
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        store.plan(second, {"later-memory": "memory"})
        calls.append(True)
        after = original(self, db)
        assert after == before
        return after

    with monkeypatch.context() as patch:
        patch.setattr(ExecutionStore, "_old_snapshot", concurrent_plan)
        state = binding.read_state()
    assert calls == [True]
    assert set(state.operations) == {first.operation_id}
    assert state.operations[first.operation_id].records == {"synthetic-memory": "memory"}
    assert second.operation_id in binding.read_state().orphans


@pytest.mark.parametrize("version", [1, 2])
def test_readonly_binding_store_cannot_mutate(version, journal):
    if version == 1:
        store, _ = add_legacy(journal)
    else:
        store = bound_store(journal)
    readonly_journal = GovernanceJournal(
        journal.path, instance_id=journal.instance_id, key=journal._key, read_only=True
    )
    binding = select_execution_store(readonly_journal)
    before = database_rows(store.path), database_rows(journal.path)
    assert binding.read_only and binding.store.read_only
    with pytest.raises(GovernanceError, match="governance_execution_read_only"):
        binding.store.plan(make_intent(journal), {"synthetic-memory": "memory"})
    assert (database_rows(store.path), database_rows(journal.path)) == before


def test_v2_uses_actual_binding_and_completed_parent_still_has_pending_cleanup(journal):
    journal.upgrade_v2(str(uuid4()))
    old, parent = add_legacy(journal, action="purge", completed=True)
    store = ExecutionStore(journal)
    identity = store.migrate_legacy(
        store.inspect_legacy(), {parent.operation_id: CleanupAuthority("legacy", (), ())}
    )
    journal.bind_execution_store(identity.store_id)
    binding = select_execution_store(journal)
    state = binding.read_state()
    assert binding.version == 2 and isinstance(binding.store, ExecutionStore)
    assert state.operations[parent.operation_id].state == "completed"
    assert state.operations[parent.operation_id].cleanup.state == "pending"
    assert state.pending == state.committed == state.business_plans == (parent.operation_id,)
    assert state.recovery.snapshot.binding == identity
    assert database_rows(old.path)["version"] == 2


@pytest.mark.parametrize("initial", ["empty", "legacy"])
def test_valid_capability_gap_requires_explicit_migration_without_activation(journal, initial):
    journal.upgrade_v2(str(uuid4()))
    store = ExecutionStore(journal)
    if initial == "empty":
        store.initialize_empty()
    else:
        _, intent = add_legacy(journal, action="purge", completed=True)
        store.migrate_legacy(
            store.inspect_legacy(), {intent.operation_id: CleanupAuthority("legacy", (), ())}
        )
    before = database_rows(store.path), database_rows(journal.path)
    with pytest.raises(GovernanceError, match="governance_execution_migration_required"):
        select_execution_store(journal)
    assert journal.execution_store_id() is None
    assert (database_rows(store.path), database_rows(journal.path)) == before


@pytest.mark.parametrize(
    "problem", ["missing", "schema1", "wrong_uuid", "damaged_gap", "unknown_schema", "symlink"]
)
def test_selection_never_downgrades_mismatched_or_unverified_state(journal, problem):
    journal.upgrade_v2(str(uuid4()))
    store = ExecutionStore(journal)
    if problem == "schema1":
        add_legacy(journal)
        journal.bind_execution_store(str(uuid4()))
    elif problem == "missing":
        journal.bind_execution_store(str(uuid4()))
    elif problem == "unknown_schema":
        add_legacy(journal)
        with sqlite3.connect(store.path) as db:
            db.execute("PRAGMA user_version=3")
    elif problem == "symlink":
        store.path.symlink_to(store.path.with_name("missing-target.sqlite3"))
    else:
        identity = store.initialize_empty()
        if problem == "wrong_uuid":
            journal.bind_execution_store(str(uuid4()))
        else:
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE binding SET baseline_root=?", ("a" * 64,))
        assert identity.store_id
    before = journal.path.read_bytes()
    with pytest.raises(GovernanceError):
        select_execution_store(journal)
    assert journal.path.read_bytes() == before
    if problem == "missing":
        assert not store.path.exists()
    if problem == "symlink":
        assert store.path.is_symlink() and not store.path.resolve().exists()


def test_binding_does_not_silently_switch_after_an_explicit_upgrade(journal):
    journal.upgrade_v2(str(uuid4()))
    legacy, intent = add_legacy(journal)
    binding = select_execution_store(journal)
    store = ExecutionStore(journal)
    identity = store.migrate_legacy(store.inspect_legacy(), {})
    journal.bind_execution_store(identity.store_id)
    with pytest.raises(GovernanceError, match="governance_execution_binding_mismatch"):
        binding.read_state()
    assert binding.version == 1 and binding.store.path == legacy.path
    assert select_execution_store(journal).read_state().committed == (intent.operation_id,)


def test_v2_each_read_rechecks_real_rows_and_main_chain(journal):
    store = bound_store(journal)
    binding = select_execution_store(journal)
    assert binding.read_state().pending == ()
    choice = authority_for(journal)
    intent, records = intent_for(journal, choice)
    store.plan(intent, records, cleanup=choice)
    assert binding.read_state().orphans == (intent.operation_id,)
    journal.append(intent)
    state = binding.read_state()
    assert state.committed == state.pending == (intent.operation_id,)
    assert state.orphans == ()
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM suppressed")
    with pytest.raises(GovernanceError, match="governance_execution_tampered"):
        binding.read_state()


def test_binding_refuses_wrong_journal_injection(journal, tmp_path):
    other = GovernanceJournal.initialize(
        tmp_path / "other" / "journal.sqlite3", instance_id=str(uuid4()), key=os.urandom(32)
    )
    with pytest.raises(GovernanceError, match="governance_execution_binding_mismatch"):
        ExecutionBinding(other, LegacyExecutionStore(journal), 1, False)
