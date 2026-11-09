"""Signed request fingerprints without a second request store or plaintext body."""

import json

from cloud_memory_hub.governance_journal import GovernanceError

REQUEST_POLICY = "actor-scope-action-selection-view-v1"


def request_targets(journal, request, actor_id):
    identities = (
        [1, "governance-request-policy", REQUEST_POLICY],
        [
            1,
            "governance-request",
            actor_id,
            request.scope_id,
            request.action,
            sorted(request.record_ids),
            request.expected_view,
        ],
    )
    return tuple(
        journal.target(
            request.scope_id,
            "content",
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
        )
        for identity in identities
    )


def require_same_request(journal, intent, request, actor_id):
    if intent.scope_id != request.scope_id or intent.action != request.action:
        raise GovernanceError("governance_operation_conflict")
    marker, target = request_targets(journal, request, actor_id)
    if marker not in intent.targets:
        raise GovernanceError("governance_operation_request_unproven")
    if intent.actor_id != actor_id or target not in intent.targets:
        raise GovernanceError("governance_operation_conflict")
