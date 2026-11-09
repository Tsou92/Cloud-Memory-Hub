"""ADR-0047 signed SQLite progress distinguishes SDK and Core file coverage.

Inventories here exercise storage validation, not file-origin or physical-I/O
proof. The integrated file engine separately produces real scan observations.
"""

import json
import sqlite3
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_inventory import DiagnosticInventory, DomainObservation
from cloud_memory_hub.governance_diagnostic_profiles import coverage_satisfies, required_domains
from cloud_memory_hub.governance_diagnostic_recovery import build_recovery_graph
from cloud_memory_hub.governance_execution_store import (
    CleanupManifest,
    CleanupVerification,
    ExecutionStore,
    manifest_from_dict,
    manifest_to_dict,
    stage_name,
)
from cloud_memory_hub.governance_journal import GovernanceError
from test_cleanup_contributors import cleanup_row, family, references
from test_governance_execution_store import (
    absence,
    bound_store,
    database_rows,
    file_plan,
    final_inventory,
    finish_pass,
    planned_purge,
)
from test_governance_execution_store import journal as journal_fixture

journal = journal_fixture
SDK_PROFILE = "sdk-two-domains-v1"
CORE_PROFILE = "sdk-core-four-domains-v1"
DOMAINS = ("core-diagnostic", "core-log", "pipeline", "sdk")


def activate(journal, store):
    journal.bind_file_coverage(store.snapshot().binding.store_id)


def empty_inventory(journal, domains=DOMAINS):
    return DiagnosticInventory(
        tuple(DomainObservation(name, journal.path.parent / name, False) for name in domains),
        (),
        (),
        0,
        0,
        profile=CORE_PROFILE,
    )


def core_manifest(store, intent):
    return CleanupManifest(
        intent.operation_id,
        str(uuid4()),
        (),
        references(store, intent.operation_id),
        profile=CORE_PROFILE,
    )


def begin_core(journal, store, intent):
    activate(journal, store)
    old = store.cleanup(intent.operation_id)
    return store.begin_pass(
        intent.operation_id,
        expected=old,
        manifest=core_manifest(store, intent),
        artifact_absence=absence(old),
    )


def verify_core(journal, store, receipt, *, domains=DOMAINS):
    return store.verify_pass(
        receipt,
        verification=CleanupVerification(
            empty_inventory(journal, domains), absence(receipt), profile=CORE_PROFILE
        ),
    )


def test_old_manifest_and_verification_bytes_survive_new_reader(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    manifest = file_plan(intent.operation_id)
    old_payload = {
        "version": 1,
        "operation_id": manifest.operation_id,
        "pass_id": manifest.pass_id,
        "artifacts": [asdict(item) for item in manifest.artifacts],
    }
    assert manifest_to_dict(manifest) == old_payload
    assert manifest_to_dict(manifest_from_dict(old_payload)) == old_payload
    receipt = finish_pass(journal, store, store.cleanup(intent.operation_id), manifest)
    before = cleanup_row(store, intent.operation_id)
    assert set(json.loads(before["verification_json"])) == {
        "version",
        "inventory_sha256",
        "artifact_absence_sha256",
    }
    assert json.loads(before["verification_json"])["version"] == 1
    assert ExecutionStore(journal).cleanup(intent.operation_id) == receipt
    assert cleanup_row(store, intent.operation_id) == before


def test_old_completed_two_domain_is_pending_after_real_profile_activation(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = finish_pass(journal, store, store.cleanup(intent.operation_id))
    store.complete(intent, expected_cleanup=receipt)
    assert not store.has_pending()
    before = database_rows(store.path)
    activate(journal, store)
    recovery = ExecutionStore(journal).recovery_snapshot()
    assert store.has_pending()
    assert recovery.required_profile == CORE_PROFILE
    assert build_recovery_graph(recovery).pending == (intent.operation_id,)
    with pytest.raises(GovernanceError, match="^governance_cleanup_incomplete$"):
        store.complete(intent, expected_cleanup=receipt)
    assert database_rows(store.path) == before
    current = verify_core(journal, store, begin_core(journal, store, intent))
    store.complete(intent, expected_cleanup=current)
    assert not store.has_pending()
    assert build_recovery_graph(store.recovery_snapshot()).pending == ()
    assert current.authority == receipt.authority


def test_four_domain_manifest_and_evidence_are_versioned_and_reopen(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = begin_core(journal, store, intent)
    payload = manifest_to_dict(receipt.manifest)
    assert payload["version"] == 3 and payload["profile"] == CORE_PROFILE
    assert manifest_from_dict(payload) == receipt.manifest
    verified = verify_core(journal, store, receipt)
    evidence = json.loads(cleanup_row(store, intent.operation_id)["verification_json"])
    assert evidence["version"] == 2
    assert evidence["profile"] == CORE_PROFILE
    assert evidence["domains"] == list(DOMAINS)
    assert ExecutionStore(journal).cleanup(intent.operation_id) == verified
    store.complete(intent, expected_cleanup=verified)
    assert not store.has_pending()


@pytest.mark.parametrize(
    "domains", (DOMAINS[:-1], (*DOMAINS[:-1], "core-log"), (*DOMAINS, "unknown"))
)
def test_four_domain_verification_requires_complete_unique_domain_set(journal, domains):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = begin_core(journal, store, intent)
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="verification_invalid"):
        verify_core(journal, store, receipt, domains=domains)
    assert database_rows(store.path) == before


def test_profile_strings_cannot_activate_or_downgrade_storage(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    before = database_rows(store.path), database_rows(journal.path)
    with pytest.raises(GovernanceError, match="^governance_cleanup_profile_required$"):
        store.begin_pass(
            intent.operation_id,
            expected=store.cleanup(intent.operation_id),
            manifest=core_manifest(store, intent),
        )
    assert (database_rows(store.path), database_rows(journal.path)) == before
    activate(journal, store)
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="^governance_cleanup_profile_required$"):
        store.begin_pass(
            intent.operation_id,
            expected=store.cleanup(intent.operation_id),
            manifest=CleanupManifest(intent.operation_id, str(uuid4()), ()),
        )
    assert database_rows(store.path) == before


def test_started_old_pass_can_retire_but_cannot_complete_new_coverage(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    manifest = file_plan(intent.operation_id)
    receipt = store.begin_pass(
        intent.operation_id, expected=store.cleanup(intent.operation_id), manifest=manifest
    )
    activate(journal, store)
    finished = replace(
        manifest, artifacts=tuple(replace(item, state="verified") for item in manifest.artifacts)
    )
    receipt = store.checkpoint(receipt, manifest=finished)
    old = store.verify_pass(
        receipt,
        verification=CleanupVerification(final_inventory(journal, finished), absence(receipt)),
    )
    assert old.verification.profile == SDK_PROFILE
    with pytest.raises(GovernanceError, match="^governance_cleanup_incomplete$"):
        store.complete(intent, expected_cleanup=old)
    verified = verify_core(journal, store, begin_core(journal, store, intent))
    store.complete(intent, expected_cleanup=verified)
    assert not store.has_pending()


@pytest.mark.parametrize("field", ("manifest", "verification"))
def test_profile_changes_cannot_relabel_existing_pass(journal, field):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = begin_core(journal, store, intent)
    before = database_rows(store.path)
    with pytest.raises(GovernanceError):
        if field == "manifest":
            store.checkpoint(receipt, manifest=replace(receipt.manifest, profile=SDK_PROFILE))
        else:
            store.verify_pass(
                receipt,
                verification=CleanupVerification(empty_inventory(journal), absence(receipt)),
            )
    assert database_rows(store.path) == before


def test_sdk_inventory_cannot_be_relabelled_by_four_domain_verification(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = begin_core(journal, store, intent)
    inventory = replace(empty_inventory(journal), profile=SDK_PROFILE)
    assert tuple(item.domain for item in inventory.domains) == DOMAINS
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="^governance_cleanup_verification_invalid$"):
        store.verify_pass(
            receipt,
            verification=CleanupVerification(inventory, absence(receipt), profile=CORE_PROFILE),
        )
    assert database_rows(store.path) == before


@pytest.mark.parametrize(
    "damage,expected",
    (
        ("missing_domains", "verification_invalid"),
        ("old_evidence", "execution_tampered"),
        ("sdk_profile", "verification_invalid"),
        ("missing_profile", "verification_invalid"),
    ),
)
def test_signed_wrong_domain_evidence_is_not_a_valid_receipt(journal, damage, expected):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = verify_core(journal, store, begin_core(journal, store, intent))
    row = cleanup_row(store, intent.operation_id)
    evidence = json.loads(row["verification_json"])
    if damage == "missing_domains":
        evidence["domains"] = ["pipeline", "sdk"]
    elif damage == "old_evidence":
        evidence = {
            "version": 1,
            "inventory_sha256": evidence["inventory_sha256"],
            "artifact_absence_sha256": evidence["artifact_absence_sha256"],
        }
    elif damage == "sdk_profile":
        evidence["profile"] = SDK_PROFILE
    else:
        evidence.pop("profile")
    payload = json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    values = [row["state"], row["pass_id"], row["manifest_json"], row["revision"], payload]
    signature = journal._mac(
        "execution-cleanup-progress-v2",
        [
            store.snapshot().binding.store_id,
            intent.operation_id,
            row["immutable_signature"],
            *values,
        ],
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE cleanup SET verification_json=?,progress_signature=? WHERE operation_id=?",
            (payload, signature, receipt.operation_id),
        )
    with pytest.raises(GovernanceError, match=expected):
        store.recovery_snapshot()


def test_old_v2_manifest_keeps_exact_canonical_shape(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    manifest = replace(
        file_plan(intent.operation_id), contributors=references(store, intent.operation_id)
    )
    payload = manifest_to_dict(manifest)
    assert payload == {
        "version": 2,
        "operation_id": manifest.operation_id,
        "pass_id": manifest.pass_id,
        "artifacts": [asdict(item) for item in manifest.artifacts],
        "contributors": [asdict(item) for item in manifest.contributors],
    }
    assert manifest_to_dict(manifest_from_dict(payload)) == payload
    verified = finish_pass(journal, store, store.cleanup(intent.operation_id), manifest)
    assert coverage_satisfies(verified, SDK_PROFILE)
    assert not coverage_satisfies(verified, CORE_PROFILE)


@pytest.mark.parametrize("profile", (None, True, "four", ("pipeline", "sdk")))
def test_unknown_profile_cannot_supply_a_custom_domain_set(profile):
    with pytest.raises(GovernanceError, match="^governance_cleanup_profile_invalid$"):
        required_domains(profile)


@pytest.mark.parametrize(
    "domain,key",
    (
        ("core-log", "core.log"),
        ("core-log", "core.log.3"),
        ("core-diagnostic", "diagnostic-core.jsonl"),
        ("core-diagnostic", "diagnostic-core.jsonl.1"),
    ),
)
def test_sdk_rights_never_authorize_a_core_artifact(journal, domain, key):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    activate(journal, store)
    legacy = file_plan(intent.operation_id)
    entry = replace(
        legacy.artifacts[0], before=replace(legacy.artifacts[0].before, domain=domain, key=key)
    )
    with pytest.raises(GovernanceError, match="manifest_invalid"):
        replace(legacy, artifacts=(entry,))
    manifest = replace(
        legacy,
        artifacts=(entry,),
        contributors=references(store, intent.operation_id),
        profile=CORE_PROFILE,
    )
    assert manifest_from_dict(manifest_to_dict(manifest)) == manifest
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="manifest_invalid"):
        store.begin_pass(
            intent.operation_id, expected=store.cleanup(intent.operation_id), manifest=manifest
        )
    assert database_rows(store.path) == before


def test_shared_contributor_cache_cannot_skip_another_manifest_domain_check(journal):
    store, parent, repair = family(journal)
    activate(journal, store)
    refs = references(store, parent.operation_id, repair.operation_id)
    receipts = []
    for identity in sorted((parent.operation_id, repair.operation_id)):
        receipts.append(
            store.begin_pass(
                identity,
                expected=store.cleanup(identity),
                manifest=CleanupManifest(identity, str(uuid4()), (), refs, profile=CORE_PROFILE),
            )
        )
    # The first row is valid and fills the per-snapshot contributor cache.
    # A correctly MACed second row still must undergo its own domain checks.
    second = receipts[1]
    entry = file_plan(second.operation_id).artifacts[0]
    entry = replace(
        entry,
        before=replace(entry.before, domain="core-log", key="core.log"),
        stage_name=stage_name(second.operation_id, second.pass_id, 0),
    )
    manifest = replace(second.manifest, artifacts=(entry,))
    row = cleanup_row(store, second.operation_id)
    payload = json.dumps(
        manifest_to_dict(manifest), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    values = [row["state"], row["pass_id"], payload, row["revision"], row["verification_json"]]
    signature = store._progress_signature(
        store.snapshot().binding, second.operation_id, row["immutable_signature"], values
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE cleanup SET manifest_json=?,progress_signature=? WHERE operation_id=?",
            (payload, signature, second.operation_id),
        )
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="^governance_cleanup_manifest_invalid$"):
        store.recovery_snapshot()
    assert database_rows(store.path) == before


@pytest.mark.parametrize(
    "key", ("core.log.0", "core.log.4", "core.log.01", "../core.log", "core.log/child")
)
def test_core_file_family_is_fixed(journal, key):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    entry = file_plan(intent.operation_id).artifacts[0]
    with pytest.raises(GovernanceError, match="manifest_invalid"):
        replace(entry, before=replace(entry.before, domain="core-log", key=key))


def test_profile_is_from_the_same_main_journal_snapshot(journal, monkeypatch):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = finish_pass(journal, store, store.cleanup(intent.operation_id))
    store.complete(intent, expected_cleanup=receipt)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    original = store._snapshot
    activated = False

    def snapshot(db):
        nonlocal activated
        result = original(db)
        if not activated:
            activated = True
            journal.bind_file_coverage(result.binding.store_id)
        return result

    monkeypatch.setattr(store, "_snapshot", snapshot)
    first = store.recovery_snapshot()
    assert activated and first.required_profile == SDK_PROFILE
    assert build_recovery_graph(first).pending == ()
    second = store.recovery_snapshot()
    assert second.required_profile == CORE_PROFILE
    assert build_recovery_graph(second).pending == (intent.operation_id,)


def test_four_domain_evidence_satisfies_sdk_without_relabelling(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    verified = verify_core(journal, store, begin_core(journal, store, intent))
    assert coverage_satisfies(verified, SDK_PROFILE)
    assert coverage_satisfies(verified, CORE_PROFILE)
    assert verified.verification.profile == CORE_PROFILE


@pytest.mark.parametrize("problem", ("exists", "missing_identity", "absent_identity"))
def test_domain_observation_metadata_is_not_only_a_label(journal, problem):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = begin_core(journal, store, intent)
    inventory = empty_inventory(journal)
    first = inventory.domains[0]
    changes = (
        {"exists": 1}
        if problem == "exists"
        else {"exists": True}
        if problem == "missing_identity"
        else {"inode": 1}
    )
    inventory = replace(inventory, domains=(replace(first, **changes), *inventory.domains[1:]))
    before = database_rows(store.path)
    with pytest.raises(GovernanceError, match="verification_invalid"):
        store.verify_pass(
            receipt,
            verification=CleanupVerification(inventory, absence(receipt), profile=CORE_PROFILE),
        )
    assert database_rows(store.path) == before
