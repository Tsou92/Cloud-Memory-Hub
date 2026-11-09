"""Signed repair request identities; exact byte authorization is separate from progress."""

import re
from dataclasses import dataclass

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    cleanup_digest,
    cleanup_policy_targets,
    cleanup_to_dict,
)
from cloud_memory_hub.governance_journal import GovernanceError, Intent, _json
from cloud_memory_hub.remote_access import ScopeGrant

REQUEST_POLICY = "diagnostic-repair-request-v1"
CONFIRMATION_POLICY = "whole-record-selected-parent-and-exact-copies-v1"


@dataclass(frozen=True, slots=True)
class RepairRequest:
    operation_id: str
    scope_id: str
    selection: CleanupAuthority
    review_digest: str

    def __post_init__(self):
        try:
            canonical_uuid(self.operation_id)
            ScopeGrant.valid_scope(self.scope_id)
            cleanup_to_dict(self.selection)
            if self.selection.mode != "repair" or (
                type(self.review_digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.review_digest) is None
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("diagnostic_repair_request_invalid") from None


def repair_request_targets(journal, request: RepairRequest, actor_id: str):
    if type(request) is not RepairRequest:
        raise GovernanceError("diagnostic_repair_request_invalid")
    request.__post_init__()
    values = (
        [1, "diagnostic-repair-request-policy", REQUEST_POLICY],
        [
            1,
            "diagnostic-repair-request",
            REQUEST_POLICY,
            CONFIRMATION_POLICY,
            actor_id,
            request.operation_id,
            request.scope_id,
            cleanup_digest(request.selection),
            request.review_digest,
        ],
    )
    return tuple(journal.target(request.scope_id, "content", _json(value)) for value in values)


def repair_intent(journal, request: RepairRequest, actor_id: str) -> Intent:
    return Intent(
        operation_id=request.operation_id,
        actor_id=actor_id,
        scope_id=request.scope_id,
        action="purge",
        targets=(
            *cleanup_policy_targets(journal, request.scope_id, request.selection),
            *repair_request_targets(journal, request, actor_id),
        ),
    )


def require_same_repair_request(journal, operation, request: RepairRequest, actor_id: str):
    """Verify the original submission, even after files no longer match its review."""
    expected = repair_intent(journal, request, actor_id)
    if (
        operation.intent != expected
        or operation.records
        or operation.cleanup is None
        or operation.cleanup.authority != request.selection
        or operation.cleanup.orphan_only
    ):
        raise GovernanceError("governance_operation_conflict")
