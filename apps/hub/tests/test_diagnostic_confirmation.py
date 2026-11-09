"""Exact bytes and whole-write certificates are separate from request identity."""

import copy
from dataclasses import replace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    RepairParent,
    cleanup_digest,
    cleanup_from_dict,
    cleanup_policy_targets,
    cleanup_to_dict,
    validate_cleanup_for_intent,
)
from cloud_memory_hub.governance_diagnostic_records import RecordGrant
from cloud_memory_hub.governance_inflight import (
    CleanupSelector,
    inflight_job_target,
    inflight_policy_target,
)
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from test_governance_journal import journal as journal_fixture

journal = journal_fixture


def authority_for(journal):
    selector = CleanupSelector(
        str(uuid4()),
        f"cmh:{journal.instance_id}:{uuid4()}:{uuid4()}",
        "a" * 64,
        "terminal_cleanup",
    )
    grant = RecordGrant("sdk-jsonl-v1", selector.request_id, "b" * 64, 123)
    return CleanupAuthority("records", (selector,), (grant,))


def intent_for(journal, authority, *, scope=None):
    scope = scope or str(uuid4())
    records = {"cmhe:" + item.job_id: "raw" for item in authority.selectors}
    if authority.mode == "repair":
        records = {}
    targets = [journal.target(scope, kind, identity) for identity, kind in records.items()]
    targets.extend(cleanup_policy_targets(journal, scope, authority))
    targets.append(inflight_policy_target(journal, scope))
    targets.extend(
        inflight_job_target(journal, scope, item, disposition=item.disposition)
        for item in authority.selectors
    )
    return Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action="purge",
        targets=tuple(targets),
    ), records


def test_roundtrip_is_canonical_and_preserves_exact_bytes(journal):
    first, second = authority_for(journal), authority_for(journal)
    combined = CleanupAuthority(
        "records", first.selectors + second.selectors, first.grants + second.grants
    )
    reverse = replace(
        combined,
        selectors=tuple(reversed(combined.selectors)),
        grants=tuple(reversed(combined.grants)),
    )
    assert cleanup_to_dict(reverse) == cleanup_to_dict(combined)
    assert cleanup_digest(reverse) == cleanup_digest(combined)
    assert cleanup_from_dict(cleanup_to_dict(combined)) == combined
    intent, records = intent_for(journal, combined)
    validate_cleanup_for_intent(journal, intent, records, combined)
    changed = replace(
        combined, grants=(replace(combined.grants[0], byte_length=124), *combined.grants[1:])
    )
    assert cleanup_digest(changed) != cleanup_digest(combined)
    with pytest.raises(GovernanceError, match="confirmation_invalid"):
        validate_cleanup_for_intent(journal, intent, records, changed)


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "bool_version",
        "missing",
        "tuple_selectors",
        "duplicate_selector",
        "duplicate_request",
        "duplicate_grant",
        "unselected_request",
        "extra_selector",
        "extra_grant",
        "invalid_digest",
        "invalid_job",
        "invalid_disposition",
        "new_policy",
        "legacy_with_grants",
        "records_with_parent",
        "repair_without_parent",
        "oversized_grant",
    ],
)
def test_closed_confirmation_rejects_ambiguous_or_extended_authority(journal, change):
    value = cleanup_to_dict(authority_for(journal))
    if change == "extra":
        value["body"] = "synthetic body"
    elif change == "bool_version":
        value["version"] = True
    elif change == "missing":
        del value["parent"]
    elif change == "tuple_selectors":
        value["selectors"] = tuple(value["selectors"])
    elif change == "duplicate_selector":
        value["selectors"] *= 2
    elif change == "duplicate_request":
        item = copy.deepcopy(value["selectors"][0])
        item["job_id"] = str(uuid4())
        value["selectors"].append(item)
    elif change == "duplicate_grant":
        value["grants"] *= 2
    elif change == "unselected_request":
        value["grants"][0]["request_id"] = authority_for(journal).grants[0].request_id
    elif change == "extra_selector":
        value["selectors"][0]["body"] = "synthetic"
    elif change == "extra_grant":
        value["grants"][0]["path"] = "synthetic"
    elif change == "invalid_digest":
        value["selectors"][0]["input_digest"] = "X" * 64
    elif change == "invalid_job":
        value["selectors"][0]["job_id"] = "not-an-id"
    elif change == "invalid_disposition":
        value["selectors"][0]["disposition"] = "unknown"
    elif change == "new_policy":
        value["policy"] = "anything-goes"
    elif change == "legacy_with_grants":
        value["mode"] = "legacy"
    elif change == "records_with_parent":
        value["parent"] = dict(
            operation_id=str(uuid4()), intent_digest="a" * 64, ledger_id=journal.ledger_id
        )
    elif change == "repair_without_parent":
        value["mode"] = "repair"
    elif change == "oversized_grant":
        value["grants"][0]["byte_length"] = 2 * 1024 * 1024 + 1
    with pytest.raises(GovernanceError, match="confirmation_invalid"):
        cleanup_from_dict(value)


@pytest.mark.parametrize(
    "change",
    ["omit_raw", "more_raw", "wrong_kind", "certificate", "disposition", "instance", "forget"],
)
def test_whole_cmhe_selection_and_certificate_are_mandatory(journal, change):
    authority = authority_for(journal)
    intent, records = intent_for(journal, authority)
    selector = authority.selectors[0]
    if change == "omit_raw":
        records = {}
    elif change == "more_raw":
        records["cmhe:" + str(uuid4())] = "raw"
    elif change == "wrong_kind":
        records["cmhe:" + selector.job_id] = "memory"
    elif change == "certificate":
        target = inflight_job_target(
            journal, intent.scope_id, selector, disposition=selector.disposition
        )
        intent = intent.model_copy(
            update={"targets": tuple(t for t in intent.targets if t != target)}
        )
    elif change == "disposition":
        authority = replace(
            authority, selectors=(replace(selector, disposition="cancel_unpermitted"),)
        )
    elif change == "instance":
        request = f"cmh:{uuid4()}:{uuid4()}:{uuid4()}"
        authority = replace(
            authority,
            selectors=(replace(selector, request_id=request),),
            grants=(replace(authority.grants[0], request_id=request),),
        )
        intent, records = intent_for(journal, authority)
    elif change == "forget":
        intent = intent.model_copy(update={"action": "forget"})
    with pytest.raises(GovernanceError, match="confirmation_invalid"):
        validate_cleanup_for_intent(journal, intent, records, authority)


def test_legacy_has_no_new_authority_and_requires_existing_whole_write_proof(journal):
    authority = replace(authority_for(journal), mode="legacy", grants=())
    assert cleanup_policy_targets(journal, str(uuid4()), authority) == ()
    intent, records = intent_for(journal, authority)
    validate_cleanup_for_intent(journal, intent, records, authority)
    assert not cleanup_to_dict(authority)["grants"]


def test_repair_commits_parent_but_never_selects_business_records(journal):
    parent = RepairParent(str(uuid4()), "c" * 64, journal.ledger_id)
    authority = replace(authority_for(journal), mode="repair", parent=parent)
    intent, records = intent_for(journal, authority)
    validate_cleanup_for_intent(journal, intent, records, authority)
    with pytest.raises(GovernanceError, match="confirmation_invalid"):
        validate_cleanup_for_intent(journal, intent, {"cmhe:" + str(uuid4()): "raw"}, authority)
    for changed in (
        replace(parent, ledger_id=str(uuid4())),
        replace(parent, operation_id=intent.operation_id),
        replace(parent, intent_digest="d" * 64),
    ):
        with pytest.raises(GovernanceError, match="confirmation_invalid"):
            validate_cleanup_for_intent(
                journal, intent, records, replace(authority, parent=changed)
            )


def test_empty_grants_are_signed_but_do_not_assert_absent_files(journal):
    authority = CleanupAuthority("records", (), ())
    intent, records = intent_for(journal, authority)
    validate_cleanup_for_intent(journal, intent, records, authority)
    assert len(cleanup_policy_targets(journal, intent.scope_id, authority)) == 2
