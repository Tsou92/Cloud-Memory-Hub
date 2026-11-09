"""ADR-0045 recovery choices come from real journal and Store2 snapshots."""

import os
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    RepairParent,
    cleanup_digest,
)
from cloud_memory_hub.governance_diagnostic_recovery import (
    RecoveryBudget,
    build_recovery_graph,
    resolve_cleanup_authority,
)
from cloud_memory_hub.governance_execution import ExecutionStore as LegacyExecutionStore
from cloud_memory_hub.governance_execution_store import CleanupContributor, ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal
from test_diagnostic_confirmation import authority_for, intent_for
from test_governance_execution_store import bound_store, database_rows, make_intent


@pytest.fixture
def journal(tmp_path):
    return GovernanceJournal.initialize(
        tmp_path / "authority" / "journal.sqlite3", instance_id=str(uuid4()), key=os.urandom(32)
    )


def committed(journal, store, choice, *, scope=None, commit=True):
    intent, records = intent_for(journal, choice, scope=scope)
    store.plan(intent, records, cleanup=choice)
    if commit:
        journal.append(intent)
    return intent


def actual_digest(journal, identity):
    with journal._connection() as db:
        return db.execute(
            "SELECT digest FROM intents WHERE operation_id=?", (identity,)
        ).fetchone()[0]


def repair_choice(journal, parent, choice, *, variant="b"):
    return CleanupAuthority(
        "repair",
        choice.selectors,
        tuple(replace(item, sha256=variant * 64) for item in choice.grants),
        RepairParent(
            parent.operation_id, actual_digest(journal, parent.operation_id), journal.ledger_id
        ),
    )


def migrated_parent(journal):
    """Use the shipped legacy writer, its real committed intent and explicit migration."""
    journal.upgrade_v2(str(uuid4()))
    choice = authority_for(journal)
    legacy_choice = CleanupAuthority("legacy", choice.selectors, ())
    parent, records = intent_for(journal, legacy_choice)
    legacy = LegacyExecutionStore(journal)
    legacy.plan(parent, records)
    journal.append(parent)
    legacy.complete(parent)
    store = ExecutionStore(journal)
    binding = store.migrate_legacy(store.inspect_legacy(), {parent.operation_id: legacy_choice})
    journal.bind_execution_store(binding.store_id)
    return store, parent, choice


def test_migrated_parent_and_multiple_planned_repairs_form_pure_global_graph(journal):
    store, parent, choice = migrated_parent(journal)
    first = committed(journal, store, repair_choice(journal, parent, choice), scope=parent.scope_id)
    second = committed(
        journal, store, repair_choice(journal, parent, choice, variant="c"), scope=parent.scope_id
    )
    other_choice = authority_for(journal)
    other = committed(journal, store, other_choice)
    orphan = committed(
        journal,
        store,
        repair_choice(journal, parent, choice, variant="d"),
        scope=parent.scope_id,
        commit=False,
    )
    forgotten = make_intent(journal)
    store.plan(forgotten, {"synthetic-memory": "memory"})
    journal.append(forgotten)
    store.complete(forgotten)
    unplanned = make_intent(journal)
    journal.append(unplanned)
    before = database_rows(store.path), database_rows(journal.path)
    recovery = store.recovery_snapshot()
    result = build_recovery_graph(recovery)
    assert result.business_operations == tuple(
        sorted((parent.operation_id, other.operation_id, forgotten.operation_id))
    )
    assert result.repair_operations == tuple(sorted((first.operation_id, second.operation_id)))
    families = {item.parent_id: item.repair_ids for item in result.families}
    assert families == {
        parent.operation_id: tuple(sorted((first.operation_id, second.operation_id))),
        other.operation_id: (),
    }
    assert result.orphans == (orphan.operation_id,)
    assert result.unplanned == (unplanned,)
    assert set(result.pending) == {
        parent.operation_id,
        first.operation_id,
        second.operation_id,
        other.operation_id,
        orphan.operation_id,
        unplanned.operation_id,
    }
    assert len(result.authority.requests) == 2
    assert {item.user_id for item in result.authority.requests} == {
        f"cmh:{journal.instance_id}:{parent.scope_id}",
        f"cmh:{journal.instance_id}:{other.scope_id}",
    }
    assert len(result.authority.grants) == 3
    assert {item.operation_id for item in result.authority.contributors} == {
        parent.operation_id,
        first.operation_id,
        second.operation_id,
        other.operation_id,
    }
    assert store.cleanup(parent.operation_id).authority.grants == ()
    assert store.operations()[parent.operation_id][0] == "completed"
    assert store.operations()[first.operation_id][0] == "planned"
    assert (database_rows(store.path), database_rows(journal.path)) == before
    with pytest.raises(FrozenInstanceError):
        result.pending = ()
    with pytest.raises(TypeError):
        recovery.committed_digests[parent.operation_id] = "a" * 64


def test_reference_order_is_stable_and_exact_duplicates_are_deduplicated(journal):
    store = bound_store(journal)
    choice = authority_for(journal)
    parent = committed(journal, store, choice)
    repair = committed(
        journal, store, repair_choice(journal, parent, choice), scope=parent.scope_id
    )
    recovery = store.recovery_snapshot()
    result = build_recovery_graph(recovery)
    assert len(result.authority.grants) == len(result.authority.requests) == 1
    assert (
        resolve_cleanup_authority(
            recovery,
            tuple(reversed(result.authority.contributors)),
            owner_operation_id=repair.operation_id,
        )
        == result.authority
    )
    reordered = replace(
        recovery,
        committed=tuple(reversed(recovery.committed)),
        snapshot=replace(
            recovery.snapshot,
            operations=dict(reversed(tuple(recovery.snapshot.operations.items()))),
        ),
    )
    assert build_recovery_graph(reordered) == result


def test_v1_repair_implicit_owner_does_not_need_parent_in_refs_or_verified_state(journal):
    store, parent, choice = migrated_parent(journal)
    repair_authority = repair_choice(journal, parent, choice)
    repair = committed(journal, store, repair_authority, scope=parent.scope_id)
    unplanned = make_intent(journal)
    journal.append(unplanned)
    recovery = store.recovery_snapshot()
    reference = CleanupContributor(
        repair.operation_id,
        recovery.committed_digests[repair.operation_id],
        cleanup_digest(repair_authority),
    )
    result = resolve_cleanup_authority(
        recovery, (reference,), owner_operation_id=repair.operation_id
    )
    assert result.contributors == (reference,)
    assert result.grants == repair_authority.grants
    assert recovery.snapshot.operations[repair.operation_id].cleanup.state == "pending"
    assert build_recovery_graph(recovery).unplanned == (unplanned,)


def test_actual_read_only_open_resolves_current_commits_without_progress_or_head_writes(journal):
    store, parent, choice = migrated_parent(journal)
    repair = committed(
        journal, store, repair_choice(journal, parent, choice), scope=parent.scope_id
    )
    before = database_rows(store.path), database_rows(journal.path)
    read_journal = GovernanceJournal(
        journal.path, instance_id=journal.instance_id, key=journal._key, read_only=True
    )
    read_store = ExecutionStore(read_journal, read_only=True)
    read_store.require_capability()
    recovery = read_store.recovery_snapshot()
    graph = build_recovery_graph(recovery)
    assert graph.repair_operations == (repair.operation_id,)
    assert recovery.committed_digests[parent.operation_id] == actual_digest(
        journal, parent.operation_id
    )
    assert (
        resolve_cleanup_authority(
            recovery, graph.authority.contributors, owner_operation_id=parent.operation_id
        )
        == graph.authority
    )
    assert (database_rows(store.path), database_rows(journal.path)) == before


@pytest.mark.parametrize(
    "damage", ["intent_digest", "cleanup_digest", "orphan", "forget", "owner", "duplicate"]
)
def test_bad_frozen_contributors_never_create_authority(journal, damage):
    store = bound_store(journal)
    choice = authority_for(journal)
    intent = committed(journal, store, choice, commit=damage != "orphan")
    if damage == "forget":
        intent = make_intent(journal)
        store.plan(intent, {"synthetic-memory": "memory"})
        journal.append(intent)
    recovery = store.recovery_snapshot()
    reference = CleanupContributor(
        intent.operation_id,
        recovery.committed_digests.get(intent.operation_id, "a" * 64),
        cleanup_digest(choice),
    )
    if damage in {"intent_digest", "cleanup_digest"}:
        reference = replace(reference, **{damage: "f" * 64})
    references = (reference, reference) if damage == "duplicate" else (reference,)
    owner = str(uuid4()) if damage == "owner" else intent.operation_id
    with pytest.raises(GovernanceError, match="governance_cleanup_contributor_invalid"):
        resolve_cleanup_authority(recovery, references, owner_operation_id=owner)


@pytest.mark.parametrize("conflict", ["scope", "job", "request", "input_digest", "disposition"])
def test_independently_signed_choices_cannot_alias_request_birth(journal, conflict):
    store = bound_store(journal)
    first_choice = authority_for(journal)
    first = committed(journal, store, first_choice)
    selector = first_choice.selectors[0]
    if conflict == "job":
        selector = replace(selector, job_id=str(uuid4()))
    elif conflict == "request":
        selector = replace(selector, request_id=authority_for(journal).selectors[0].request_id)
    elif conflict == "input_digest":
        selector = replace(selector, input_digest="c" * 64)
    elif conflict == "disposition":
        selector = replace(selector, disposition="cancel_unpermitted")
    choice = CleanupAuthority(
        "records",
        (selector,),
        (replace(first_choice.grants[0], request_id=selector.request_id),),
    )
    committed(journal, store, choice, scope=None if conflict == "scope" else first.scope_id)
    recovery = store.recovery_snapshot()
    with pytest.raises(GovernanceError, match="governance_diagnostic_request_conflict"):
        build_recovery_graph(recovery)


@pytest.mark.parametrize(
    "field", ["max_operations", "max_requests", "max_grants", "max_contributors"]
)
def test_global_budget_is_not_multiplied_by_operations(journal, field):
    store = bound_store(journal)
    for _ in range(2):
        committed(journal, store, authority_for(journal))
    recovery = store.recovery_snapshot()
    before = database_rows(store.path), database_rows(journal.path)
    with pytest.raises(GovernanceError, match="diagnostic_recovery_budget_exceeded"):
        build_recovery_graph(recovery, budget=RecoveryBudget(**{field: 1}))
    assert (database_rows(store.path), database_rows(journal.path)) == before


def test_operation_budget_includes_unplanned_and_orphans(journal):
    store = bound_store(journal)
    committed(journal, store, authority_for(journal), commit=False)
    journal.append(make_intent(journal))
    with pytest.raises(GovernanceError, match="diagnostic_recovery_budget_exceeded"):
        build_recovery_graph(store.recovery_snapshot(), budget=RecoveryBudget(max_operations=1))


@pytest.mark.parametrize("value", [0, -1, True, 10_001, "1"])
def test_budget_rejects_invalid_or_unbounded_inputs(value):
    with pytest.raises(GovernanceError, match="diagnostic_recovery_budget_invalid"):
        RecoveryBudget(max_operations=value)


def test_empty_graph_is_supported_without_fabricating_a_pass_owner(journal):
    store = bound_store(journal)
    result = build_recovery_graph(store.recovery_snapshot())
    assert result.business_operations == result.pending == result.families == ()
    assert (
        result.authority.contributors == result.authority.requests == result.authority.grants == ()
    )
    with pytest.raises(GovernanceError, match="governance_cleanup_contributor_invalid"):
        resolve_cleanup_authority(store.recovery_snapshot(), (), owner_operation_id=str(uuid4()))


@pytest.mark.parametrize(
    "damage", ["parent_digest", "parent_ledger", "parent_scope", "parent_selection"]
)
def test_parent_reference_is_rechecked_against_snapshot(journal, damage):
    store, parent, choice = migrated_parent(journal)
    authority = repair_choice(journal, parent, choice)
    repair = committed(journal, store, authority, scope=parent.scope_id)
    recovery = store.recovery_snapshot()
    operation = recovery.snapshot.operations[repair.operation_id]
    if damage == "parent_digest":
        authority = replace(authority, parent=replace(authority.parent, intent_digest="f" * 64))
    elif damage == "parent_ledger":
        authority = replace(authority, parent=replace(authority.parent, ledger_id=str(uuid4())))
    elif damage == "parent_selection":
        authority = replace(
            authority,
            selectors=(replace(authority.selectors[0], input_digest="f" * 64),),
        )
    else:
        operation = replace(
            operation, intent=operation.intent.model_copy(update={"scope_id": str(uuid4())})
        )
    altered = replace(operation, cleanup=replace(operation.cleanup, authority=authority))
    # A defensive pure-resolver test, not a substitute for Store2's real MAC checks.
    damaged = replace(
        recovery,
        snapshot=replace(
            recovery.snapshot,
            operations={**recovery.snapshot.operations, repair.operation_id: altered},
        ),
    )
    reference = CleanupContributor(
        repair.operation_id,
        recovery.committed_digests[repair.operation_id],
        cleanup_digest(authority),
    )
    with pytest.raises(GovernanceError, match="governance_cleanup_parent_invalid"):
        resolve_cleanup_authority(damaged, (reference,), owner_operation_id=repair.operation_id)
