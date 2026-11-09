"""Actual signed repair submission and retries never substitute a new authority."""

from dataclasses import replace

import pytest
from cloud_memory_hub.governance_authority import GovernanceAuthority
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
from cloud_memory_hub.governance_diagnostic_review import review_diagnostic_repairs
from cloud_memory_hub.governance_journal import GovernanceError
from test_diagnostic_confirmation import intent_for
from test_diagnostic_review import approve, seed, state, submit


def test_same_committed_request_retries_after_file_changes_without_new_intents(tmp_path):
    case = seed(tmp_path)
    approval, _ = approve(review_diagnostic_repairs(case.config, case.parents))
    first = submit(case, approval.requests, approval)
    path = case.root / "hy_memory.log"
    path.write_bytes(path.read_bytes() + b"unknown later body awaiting separate review\n")
    before = state(case)
    assert submit(case, approval.requests) == first
    assert state(case) == before


@pytest.mark.parametrize("change", ["review", "selection", "scope"])
def test_same_operation_id_with_changed_request_conflicts_before_mutation(tmp_path, change):
    case = seed(tmp_path)
    approval, _ = approve(review_diagnostic_repairs(case.config, case.parents))
    submit(case, approval.requests, approval)
    request = approval.requests[0]
    if change == "review":
        request = replace(request, review_digest="0" * 64)
    elif change == "selection":
        request = replace(request, selection=replace(request.selection, grants=()))
    else:
        request = replace(request, scope_id="global")
    before = state(case)
    with pytest.raises(GovernanceError, match="governance_operation_conflict"):
        submit(case, (request, *approval.requests[1:]))
    assert state(case) == before


def test_interrupted_second_append_retains_orphan_and_retry_uses_original_requests(
    tmp_path, monkeypatch
):
    case = seed(tmp_path)
    approval, _ = approve(review_diagnostic_repairs(case.config, case.parents))
    original = GovernanceAuthority.append
    calls = 0

    def fail(authority, intent):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GovernanceError("synthetic_append_failure")
        return original(authority, intent)

    with monkeypatch.context() as patch:
        patch.setattr(GovernanceAuthority, "append", fail)
        with pytest.raises(GovernanceError, match="synthetic_append_failure"):
            submit(case, approval.requests, approval)
    recovery = case.store.recovery_snapshot()
    assert approval.requests[0].operation_id in recovery.committed
    assert approval.requests[1].operation_id in recovery.orphans
    first_head = case.store.journal.head(approval.requests[0].scope_id)
    result = submit(case, approval.requests, approval)
    assert len(result) == 2 and case.store.journal.head(approval.requests[0].scope_id) == first_head
    assert not case.store.recovery_snapshot().orphans
    assert all(case.store.operations()[item.operation_id][0] == "planned" for item in result)


def test_signed_receipt_retry_remains_read_only_with_an_unrelated_unplanned_intent(tmp_path):
    case = seed(tmp_path)
    approval, _ = approve(review_diagnostic_repairs(case.config, case.parents))
    first = submit(case, approval.requests, approval)
    other, _ = intent_for(case.authority.journal, CleanupAuthority("records", (), ()))
    case.authority.append(other)
    assert case.store.recovery_snapshot().unplanned == (other,)
    before = state(case)
    assert submit(case, approval.requests) == first
    assert state(case) == before
    with pytest.raises(GovernanceError, match="governance_execution_pending"):
        review_diagnostic_repairs(case.config, case.parents)
