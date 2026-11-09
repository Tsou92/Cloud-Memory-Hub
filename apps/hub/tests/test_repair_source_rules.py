"""ADR-0046: diagnostic repair cannot become a business suppression rule."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    RepairParent,
    cleanup_policy_targets,
)
from cloud_memory_hub.governance_execution_binding import ExecutionBinding, select_execution_store
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from cloud_memory_hub.governance_sources import (
    SourceSuppression,
    content_policy_target,
    content_targets,
)
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.sdk_generation import SdkGeneration
from test_content_suppression import TEXT, changed
from test_content_suppression import storage as storage_fixture
from test_governance_execution_store import bound_store, database_rows

storage = storage_fixture


def signed_family(storage, *, parent_policy=True, repair_committed=True):
    journal = storage.journal
    store = bound_store(journal)
    scope = storage.event.scope_id
    choice = CleanupAuthority("records", (), ())
    rules = (
        (content_policy_target(journal, scope), *content_targets(journal, storage.event))
        if parent_policy
        else ()
    )
    parent = Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action="purge",
        targets=(
            journal.target(scope, "memory", "synthetic-memory"),
            *rules,
            *cleanup_policy_targets(journal, scope, choice),
        ),
    )
    store.plan(parent, {"synthetic-memory": "memory"}, cleanup=choice)
    journal.append(parent)
    before_repair = SourceSuppression(store)
    if parent_policy:
        before_repair.require_allowed(
            changed(storage.event, ["Unrelated synthetic project detail."])
        )
    with journal._connection() as db:
        digest = db.execute(
            "SELECT digest FROM intents WHERE operation_id=?", (parent.operation_id,)
        ).fetchone()[0]
    repair_choice = CleanupAuthority(
        "repair", (), (), RepairParent(parent.operation_id, digest, journal.ledger_id)
    )
    repair = Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action="purge",
        targets=cleanup_policy_targets(journal, scope, repair_choice),
    )
    store.plan(repair, {}, cleanup=repair_choice)
    if repair_committed:
        journal.append(repair)
    return store, parent, repair


@pytest.mark.parametrize("repair_committed", [False, True])
@pytest.mark.parametrize("binding_input", [False, True])
def test_signed_repair_does_not_poison_business_source_rules(
    storage, repair_committed, binding_input
):
    store, parent, repair = signed_family(storage, repair_committed=repair_committed)
    before = database_rows(store.path), database_rows(storage.journal.path)
    supplied = select_execution_store(storage.journal) if binding_input else store
    suppression = SourceSuppression(supplied)
    if binding_input:
        assert suppression.execution is supplied
        assert suppression.store is supplied.store
    suppression.require_allowed(changed(storage.event, ["Unrelated synthetic project detail."]))
    with pytest.raises(AccessError) as error:
        suppression.require_allowed(changed(storage.event, [TEXT]))
    assert error.value.code == "governance_content_suppressed"
    targets = suppression.targets(storage.event.scope_id)
    # The shared diagnostic policy marker already belongs to the ordinary
    # parent; only the repair's additional selection must stay out of rules.
    assert targets == {
        target for target in parent.targets if target.kind in {"content", "source_record"}
    }
    assert (database_rows(store.path), database_rows(storage.journal.path)) == before


def test_repair_never_excuses_original_business_proof_missing(storage):
    store, _, _ = signed_family(storage, parent_policy=False)
    with pytest.raises(AccessError) as error:
        SourceSuppression(store).require_allowed(
            changed(storage.event, ["Unrelated synthetic detail."])
        )
    assert (error.value.code, error.value.status) == ("governance_content_rules_unavailable", 503)


def test_ordinary_orphan_keeps_conservative_rules_and_record_suppression(storage):
    journal = storage.journal
    store = bound_store(journal)
    scope = storage.event.scope_id
    choice = CleanupAuthority("records", (), ())
    orphan = Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action="purge",
        targets=(
            journal.target(scope, "memory", "orphan-memory"),
            content_policy_target(journal, scope),
            *content_targets(journal, storage.event),
            *cleanup_policy_targets(journal, scope, choice),
        ),
    )
    store.plan(orphan, {"orphan-memory": "memory"}, cleanup=choice)
    binding = select_execution_store(journal)
    assert binding.read_state().committed == ()
    assert (
        binding.read_state().business_plans
        == binding.read_state().orphans
        == (orphan.operation_id,)
    )
    assert binding.suppressed(scope) == {"orphan-memory": "purged"}
    suppression = SourceSuppression(binding)
    with pytest.raises(AccessError) as error:
        suppression.require_allowed(changed(storage.event, [TEXT]))
    assert error.value.code == "governance_content_suppressed"
    suppression.require_allowed(changed(storage.event, ["An unrelated synthetic message."]))


def test_one_rule_check_uses_one_verified_snapshot_not_per_record_reads(storage, monkeypatch):
    signed_family(storage)
    binding = select_execution_store(storage.journal)
    suppression = SourceSuppression(binding)
    original, calls = ExecutionBinding.read_state, []

    def observe(self):
        calls.append(self)
        return original(self)

    def refuse_piecemeal(*args, **kwargs):
        pytest.fail("rules mixed separate operation/record snapshots")

    monkeypatch.setattr(ExecutionBinding, "read_state", observe)
    monkeypatch.setattr(binding.store, "operations", refuse_piecemeal)
    monkeypatch.setattr(binding.store, "records", refuse_piecemeal)
    suppression.require_allowed(changed(storage.event, ["An unrelated synthetic message."]))
    assert calls == [binding]


def test_legacy_sdk_scope_check_uses_business_view(storage, monkeypatch):
    _, parent, repair = signed_family(storage)
    suppression = SourceSuppression(select_execution_store(storage.journal))
    assert [item.intent.operation_id for item in suppression.business_plans(parent.scope_id)] == [
        parent.operation_id
    ]
    assert repair.operation_id != parent.operation_id
    # Exercise the existing legacy decision with real signed storage. Enrollment
    # and one-time permit consumption are covered by the admission integration tests.
    generation = object.__new__(SdkGeneration)
    generation.authority = SimpleNamespace(access=storage.ingest.access)
    generation.suppressions = suppression

    def forbidden():
        pytest.fail("legacy permission inspected all operations including repair")

    monkeypatch.setattr(suppression.store, "operations", forbidden)

    def request(scope):
        return SimpleNamespace(
            canonical_input=json.dumps(
                {"kwargs": {"user_id": f"cmh:{storage.journal.instance_id}:{scope}"}}
            )
        )

    with pytest.raises(GovernanceError, match="governance_admission_legacy_scope_unproven"):
        generation._require_legacy_allowed(request(parent.scope_id))
    generation._require_legacy_allowed(request(str(uuid4())))
