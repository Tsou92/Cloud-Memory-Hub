"""ADR-0045: frozen pass rights preserve v1 bytes and require real main entries."""

import json
import sqlite3
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_diagnostic_recovery as recovery_module
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    RepairParent,
    cleanup_digest,
)
from cloud_memory_hub.governance_execution_store import (
    CleanupContributor,
    CleanupManifest,
    CleanupVerification,
    ExecutionStore,
    manifest_from_dict,
    manifest_to_dict,
)
from cloud_memory_hub.governance_journal import GovernanceError
from test_diagnostic_confirmation import authority_for, intent_for
from test_governance_execution_store import (
    absence,
    bound_store,
    database_rows,
    file_plan,
    final_inventory,
    finish_pass,
    make_intent,
    planned_purge,
)
from test_governance_execution_store import (
    journal as journal_fixture,
)

journal = journal_fixture


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def contributor(recovery, operation_id):
    return CleanupContributor(
        operation_id,
        recovery.committed_digests[operation_id],
        cleanup_digest(recovery.snapshot.operations[operation_id].cleanup.authority),
    )


def family(journal, *, commit_repair=True):
    store = bound_store(journal)
    source = authority_for(journal)
    parent_authority = replace(source, grants=())
    parent, records = intent_for(journal, parent_authority)
    store.plan(parent, records, cleanup=parent_authority)
    checkpoint = journal.append(parent)
    authority = CleanupAuthority(
        "repair",
        source.selectors,
        source.grants,
        RepairParent(parent.operation_id, checkpoint.digest, journal.ledger_id),
    )
    repair, records = intent_for(journal, authority, scope=parent.scope_id)
    store.plan(repair, records, cleanup=authority)
    if commit_repair:
        journal.append(repair)
    return store, parent, repair


def references(store, *operation_ids):
    snapshot = store.recovery_snapshot()
    return tuple(sorted(contributor(snapshot, identity) for identity in operation_ids))


def rows(journal, store):
    return database_rows(journal.path), database_rows(store.path)


def cleanup_row(store, operation_id):
    with sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return dict(
            db.execute("SELECT * FROM cleanup WHERE operation_id=?", (operation_id,)).fetchone()
        )


def test_committed_digests_are_immutable_and_stay_with_their_snapshot(journal):
    store = bound_store(journal)
    first, _ = planned_purge(journal, store)
    orphan, _ = planned_purge(journal, store, commit=False)
    before = rows(journal, store)
    snapshot = store.recovery_snapshot()
    assert rows(journal, store) == before
    assert dict(snapshot.committed_digests) == {
        first.operation_id: journal.head(first.scope_id).digest
    }
    with pytest.raises(TypeError):
        snapshot.committed_digests[first.operation_id] = "0" * 64
    checkpoint = journal.append(orphan)
    assert orphan.operation_id not in snapshot.committed_digests
    assert store.recovery_snapshot().committed_digests[orphan.operation_id] == checkpoint.digest
    unplanned = make_intent(journal)
    journal.append(unplanned)
    current = store.recovery_snapshot()
    assert current.unplanned == (unplanned,)
    assert unplanned.operation_id not in current.committed_digests


def test_v1_canonical_progress_bytes_and_mac_survive_reopen_unchanged(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    manifest = file_plan(intent.operation_id)
    old_payload = {
        "version": 1,
        "operation_id": manifest.operation_id,
        "pass_id": manifest.pass_id,
        "artifacts": [asdict(entry) for entry in manifest.artifacts],
    }
    assert manifest_to_dict(manifest) == old_payload
    assert manifest_from_dict(old_payload).contributors is None
    receipt = store.begin_pass(
        intent.operation_id, expected=store.cleanup(intent.operation_id), manifest=manifest
    )
    row = cleanup_row(store, intent.operation_id)
    assert row["manifest_json"] == canonical(old_payload)
    values = [
        row["state"],
        row["pass_id"],
        row["manifest_json"],
        row["revision"],
        row["verification_json"],
    ]
    binding = store.snapshot().binding
    assert row["progress_signature"] == journal._mac(
        "execution-cleanup-progress-v2",
        [binding.store_id, intent.operation_id, row["immutable_signature"], *values],
    )
    before = rows(journal, store)
    recovered = ExecutionStore(journal).recovery_snapshot()
    assert recovered.snapshot.operations[intent.operation_id].cleanup == receipt
    assert rows(journal, store) == before


def test_v2_repair_contributes_without_changing_empty_parent_authority(journal):
    store, parent, repair = family(journal)
    refs = references(store, parent.operation_id, repair.operation_id)
    manifest = replace(file_plan(parent.operation_id), contributors=refs)
    assert manifest_from_dict(manifest_to_dict(manifest)) == manifest
    before = cleanup_row(store, parent.operation_id)
    initial = store.cleanup(parent.operation_id)
    result = finish_pass(journal, store, initial, manifest)
    assert result.state == "verified"
    assert result.manifest.contributors == refs
    assert result.authority == initial.authority and result.authority.grants == ()
    after = cleanup_row(store, parent.operation_id)
    assert after["immutable_json"] == before["immutable_json"]
    assert after["immutable_signature"] == before["immutable_signature"]
    assert store.operations()[parent.operation_id][0] == "planned"
    assert store.operations()[repair.operation_id][0] == "planned"
    assert store.cleanup(repair.operation_id).state == "pending"
    assert (
        ExecutionStore(journal).recovery_snapshot().snapshot.operations[parent.operation_id].cleanup
        == result
    )


def test_v2_can_bind_independent_scopes_and_keep_unplanned_intent_diagnosable(journal):
    store = bound_store(journal)
    first, _ = planned_purge(journal, store)
    second, _ = planned_purge(journal, store)
    assert first.scope_id != second.scope_id
    refs = references(store, first.operation_id, second.operation_id)
    manifest = replace(file_plan(first.operation_id), contributors=refs)
    verified = finish_pass(journal, store, store.cleanup(first.operation_id), manifest)
    unplanned = make_intent(journal)
    journal.append(unplanned)
    before = rows(journal, store)
    recovered = ExecutionStore(journal).recovery_snapshot()
    assert recovered.unplanned == (unplanned,)
    assert recovered.snapshot.operations[first.operation_id].cleanup == verified
    assert recovered.snapshot.operations[second.operation_id].cleanup.state == "pending"
    assert rows(journal, store) == before


def test_union_without_any_signed_grants_cannot_register_physical_artifacts(journal):
    store = bound_store(journal)
    authority = replace(authority_for(journal), grants=())
    intent, records = intent_for(journal, authority)
    store.plan(intent, records, cleanup=authority)
    journal.append(intent)
    manifest = replace(
        file_plan(intent.operation_id), contributors=references(store, intent.operation_id)
    )
    before = rows(journal, store)
    with pytest.raises(GovernanceError, match="^governance_cleanup_manifest_invalid$"):
        store.begin_pass(
            intent.operation_id, expected=store.cleanup(intent.operation_id), manifest=manifest
        )
    assert rows(journal, store) == before
    assert finish_pass(journal, store, store.cleanup(intent.operation_id)).state == "verified"


def test_begin_owner_mismatch_preserves_fixed_error_before_contributor_resolution(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    unknown = str(uuid4())
    manifest = CleanupManifest(unknown, str(uuid4()), ())
    before = rows(journal, store)
    with pytest.raises(GovernanceError, match="^governance_cleanup_manifest_invalid$"):
        store.begin_pass(unknown, expected=store.cleanup(intent.operation_id), manifest=manifest)
    assert rows(journal, store) == before


@pytest.mark.parametrize("damage", ["intent_digest", "cleanup_digest", "uncommitted", "unknown"])
def test_begin_rejects_unproven_contributor_without_any_row_change(journal, damage):
    store, parent, repair = family(journal, commit_repair=damage != "uncommitted")
    snapshot = store.recovery_snapshot()
    parent_ref = contributor(snapshot, parent.operation_id)
    operation = snapshot.snapshot.operations[repair.operation_id]
    repair_ref = CleanupContributor(
        repair.operation_id,
        snapshot.committed_digests.get(repair.operation_id, "b" * 64),
        cleanup_digest(operation.cleanup.authority),
    )
    if damage in {"intent_digest", "cleanup_digest"}:
        repair_ref = replace(repair_ref, **{damage: "0" * 64})
    if damage == "unknown":
        repair_ref = replace(repair_ref, operation_id=str(uuid4()))
    manifest = replace(
        file_plan(parent.operation_id), contributors=tuple(sorted((parent_ref, repair_ref)))
    )
    before = rows(journal, store)
    with pytest.raises(GovernanceError, match="^governance_cleanup_contributor_invalid$"):
        store.begin_pass(
            parent.operation_id, expected=store.cleanup(parent.operation_id), manifest=manifest
        )
    assert rows(journal, store) == before


def test_checkpoint_cannot_add_drop_or_downgrade_frozen_contributors(journal):
    store, parent, repair = family(journal)
    refs = references(store, parent.operation_id, repair.operation_id)
    old = replace(file_plan(parent.operation_id), contributors=refs)
    receipt = store.begin_pass(
        parent.operation_id, expected=store.cleanup(parent.operation_id), manifest=old
    )
    other, _ = planned_purge(journal, store)
    additions = references(store, parent.operation_id, repair.operation_id, other.operation_id)
    before = rows(journal, store)
    for changed in (None, references(store, parent.operation_id), additions):
        with pytest.raises(GovernanceError, match="^governance_cleanup_manifest_invalid$"):
            store.checkpoint(receipt, manifest=replace(old, contributors=changed))
        assert rows(journal, store) == before


def test_new_pass_can_add_signed_rights_only_after_old_artifacts_are_absent(journal):
    store = bound_store(journal)
    parent, _ = planned_purge(journal, store)
    original = replace(
        file_plan(parent.operation_id), contributors=references(store, parent.operation_id)
    )
    verified = finish_pass(journal, store, store.cleanup(parent.operation_id), original)
    other, _ = planned_purge(journal, store)
    next_pass = CleanupManifest(
        parent.operation_id,
        str(uuid4()),
        (),
        references(store, parent.operation_id, other.operation_id),
    )
    before = rows(journal, store)
    with pytest.raises(GovernanceError, match="^governance_cleanup_artifact_unproven$"):
        store.begin_pass(parent.operation_id, expected=verified, manifest=next_pass)
    assert rows(journal, store) == before
    newer = store.begin_pass(
        parent.operation_id,
        expected=verified,
        manifest=next_pass,
        artifact_absence=absence(verified),
    )
    assert newer.manifest == next_pass and newer.state == "pending"
    with pytest.raises(GovernanceError, match="^governance_cleanup_conflict$"):
        store.verify_pass(
            verified,
            verification=CleanupVerification(final_inventory(journal, original), absence(verified)),
        )


@pytest.mark.parametrize("damage", ["intent_digest", "cleanup_digest"])
def test_valid_progress_mac_cannot_substitute_for_contributor_main_authority(journal, damage):
    store, parent, repair = family(journal)
    manifest = replace(
        file_plan(parent.operation_id),
        contributors=references(store, parent.operation_id, repair.operation_id),
    )
    receipt = store.begin_pass(
        parent.operation_id, expected=store.cleanup(parent.operation_id), manifest=manifest
    )
    row = cleanup_row(store, parent.operation_id)
    payload = json.loads(row["manifest_json"])
    next(item for item in payload["contributors"] if item["operation_id"] == repair.operation_id)[
        damage
    ] = "0" * 64
    manifest_json = canonical(payload)
    values = [
        row["state"],
        row["pass_id"],
        manifest_json,
        row["revision"],
        row["verification_json"],
    ]
    signature = journal._mac(
        "execution-cleanup-progress-v2",
        [
            store.snapshot().binding.store_id,
            parent.operation_id,
            row["immutable_signature"],
            *values,
        ],
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE cleanup SET manifest_json=?,progress_signature=? WHERE operation_id=?",
            (manifest_json, signature, parent.operation_id),
        )
    before = rows(journal, store)
    with pytest.raises(GovernanceError, match="^governance_cleanup_contributor_invalid$"):
        ExecutionStore(journal).recovery_snapshot()
    with pytest.raises(GovernanceError, match="^governance_cleanup_contributor_invalid$"):
        store.checkpoint(receipt, manifest=manifest)
    with pytest.raises(GovernanceError, match="^governance_cleanup_contributor_invalid$"):
        store.verify_pass(
            receipt,
            verification=CleanupVerification(final_inventory(journal, manifest), absence(receipt)),
        )
    assert rows(journal, store) == before


@pytest.mark.parametrize(
    "damage", ["empty", "list", "duplicate", "order", "owner", "extra", "v1_extra", "version"]
)
def test_manifest_contributors_have_closed_canonical_shape(journal, damage):
    ids = sorted((str(uuid4()), str(uuid4())))
    refs = tuple(CleanupContributor(identity, "a" * 64, "b" * 64) for identity in ids)
    payload = manifest_to_dict(CleanupManifest(ids[0], str(uuid4()), (), refs))
    if damage == "empty":
        payload["contributors"] = []
    elif damage == "list":
        payload["contributors"] = tuple(payload["contributors"])
    elif damage == "duplicate":
        payload["contributors"].append(payload["contributors"][0])
    elif damage == "order":
        payload["contributors"].reverse()
    elif damage == "owner":
        payload["contributors"] = payload["contributors"][1:]
    elif damage == "extra":
        payload["contributors"][0]["body"] = "synthetic-not-metadata"
    elif damage == "v1_extra":
        payload["version"] = 1
    else:
        payload["version"] = True
    with pytest.raises(GovernanceError, match="^governance_cleanup_manifest_invalid$"):
        manifest_from_dict(payload)


def test_manifest_rejects_oversized_contributor_set_before_resolving(journal):
    reference = CleanupContributor(str(uuid4()), "a" * 64, "b" * 64)
    payload = {
        "version": 2,
        "operation_id": reference.operation_id,
        "pass_id": str(uuid4()),
        "artifacts": [],
        "contributors": [asdict(reference)] * 10_001,
    }
    with pytest.raises(GovernanceError, match="^governance_cleanup_manifest_invalid$"):
        manifest_from_dict(payload)


def test_identical_refs_resolve_once_per_real_snapshot_without_cross_snapshot_cache(
    journal, monkeypatch
):
    store = bound_store(journal)
    intents = [planned_purge(journal, store)[0] for _ in range(4)]
    refs = references(store, *(intent.operation_id for intent in intents))
    for intent in intents:
        finish_pass(
            journal,
            store,
            store.cleanup(intent.operation_id),
            CleanupManifest(intent.operation_id, str(uuid4()), (), refs),
        )
    calls = []
    original = recovery_module.resolve_cleanup_authority

    def track(*args, **kwargs):
        calls.append((args[0], args[1], kwargs["owner_operation_id"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "resolve_cleanup_authority", track)
    first = store.recovery_snapshot()
    assert len(calls) == 1 and calls[0][0] is first and calls[0][1] == refs
    calls.clear()
    second = ExecutionStore(journal).recovery_snapshot()
    assert second is not first and len(calls) == 1 and calls[0][0] is second
    # A separately committed v1 pass still resolves its own implicit authority.
    other, _ = planned_purge(journal, store)
    finish_pass(journal, store, store.cleanup(other.operation_id))
    calls.clear()
    mixed = store.recovery_snapshot()
    assert len(calls) == 2 and all(call[0] is mixed for call in calls)
    assert {len(call[1]) for call in calls} == {1, 4}
