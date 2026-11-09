"""ADR-0038: prove unpermitted inputs before targeted governance cancellation.

Callers hold governance, full-worker quiescence and SDK/intake exclusion. These
proofs cover generator admission, not completed SDK output coverage. In
particular, unrelated live work cannot certify a native-only deletion scope.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from one_memory.memory_processing import sdk_generation_input_for
from one_memory.memory_server.durable_queue import _TERMINAL_WRITE_STATES, UNKNOWN

from cloud_memory_hub.governance_candidates import ConfirmedScope, collect_inflight_evidence
from cloud_memory_hub.governance_journal import GovernanceError, _json
from cloud_memory_hub.governance_sources import MAX_TARGETS
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import Conversation, Operation
from cloud_memory_hub.source_history import PREFIX


@dataclass(frozen=True)
class PayloadTarget:
    job_id: str
    request_id: str
    input_digest: str
    disposition: Literal["cancel_unpermitted", "terminal_cleanup"] = "terminal_cleanup"
    message_texts: tuple[str, ...] = field(default=(), repr=False, compare=False)


def _message_texts(inputs, candidate):
    pair = inputs.get(candidate.job_id)
    if pair is None:
        return ()
    event, _request = pair
    texts = (
        (message.text for message in Conversation.model_validate(event.content).messages)
        if event.kind == "conversation_turn"
        else (Operation.model_validate(event.content).text,)
    )
    return tuple(dict.fromkeys(text for text in texts if len(text) >= 16))


def inflight_policy_target(journal, scope_id):
    return journal.target(scope_id, "content", _json([1, "inflight-policy", "birth-input-v1"]))


def inflight_job_target(journal, scope_id, candidate, *, disposition=None):
    if disposition is None:
        disposition = (
            "terminal_cleanup"
            if candidate.state in _TERMINAL_WRITE_STATES
            else "cancel_unpermitted"
        )
    if disposition not in {"cancel_unpermitted", "terminal_cleanup"}:
        raise GovernanceError("governance_inflight_disposition_invalid")
    return journal.target(
        scope_id,
        "content",
        _json(
            [
                1,
                "inflight-job",
                candidate.job_id,
                candidate.request_id,
                candidate.input_digest,
                disposition,
            ]
        ),
    )


class InflightGovernance:
    def __init__(self, ingest, admissions, journal):
        self.ingest, self.admissions, self.journal = ingest, admissions, journal

    def _scope_marker(self, scope, *, signed):
        if not isinstance(scope, ConfirmedScope) or scope.basis != (
            "operation" if signed else "preview"
        ):
            raise GovernanceError("governance_inflight_scope_invalid")
        marker = inflight_policy_target(self.journal, scope.scope_id)
        if signed and marker not in scope.rule_targets:
            raise GovernanceError("governance_inflight_legacy_unavailable")
        return marker

    def _inspect(self, scope, *, signed, anchored_only=False):
        marker = self._scope_marker(scope, signed=signed)
        inventory, inputs = collect_inflight_evidence(
            self.ingest, scope, journal=self.journal, anchored_only=anchored_only
        )
        candidates = (
            *inventory.confirmed_original_candidates,
            *inventory.rule_only_candidates,
            *inventory.unmatched_nonterminal_candidates,
        )
        unknown = {candidate.job_id for candidate in candidates if candidate.state == UNKNOWN}
        for blocker in inventory.blockers:
            if not (
                blocker.reason == "governance_candidate_state_unproven"
                and blocker.job_id in unknown
            ):
                raise GovernanceError(blocker.reason)
        if inventory.ownership_coverage != "complete":
            raise GovernanceError("governance_candidate_owner_unproven")
        expected = {
            identity[len(PREFIX) :] for identity in scope.records if identity.startswith(PREFIX)
        }
        confirmed = inventory.confirmed_original_candidates
        if expected != {candidate.job_id for candidate in confirmed}:
            raise GovernanceError("governance_inflight_job_missing")
        if inventory.rule_only_candidates:
            raise GovernanceError("governance_inflight_preview_changed")
        if inventory.unmatched_nonterminal_candidates and any(
            not identity.startswith(PREFIX) for identity in scope.records
        ):
            raise GovernanceError("governance_inflight_scope_unproven")
        targets, dispositions = {marker}, {}
        for candidate in confirmed:
            if signed:
                matches = {
                    disposition: inflight_job_target(
                        self.journal, scope.scope_id, candidate, disposition=disposition
                    )
                    for disposition in ("cancel_unpermitted", "terminal_cleanup")
                }
                matches = {
                    key: target for key, target in matches.items() if target in scope.rule_targets
                }
                if len(matches) != 1:
                    raise GovernanceError("governance_inflight_binding_mismatch")
                disposition, target = next(iter(matches.items()))
            else:
                disposition = (
                    "terminal_cleanup"
                    if candidate.state in _TERMINAL_WRITE_STATES
                    else "cancel_unpermitted"
                )
                target = inflight_job_target(
                    self.journal, scope.scope_id, candidate, disposition=disposition
                )
            targets.add(target)
            dispositions[candidate.job_id] = disposition
        rule_count = sum(
            target.kind not in {"raw", "memory"} for target in set(scope.rule_targets) | targets
        )
        if len(scope.records) + rule_count > MAX_TARGETS:
            raise GovernanceError("governance_scan_limit")
        return (
            inventory,
            inputs,
            tuple(sorted(targets, key=lambda target: (target.kind, target.key))),
            dispositions,
        )

    def _prove_live(self, db, inventory, inputs, dispositions):
        candidates = (
            *inventory.confirmed_original_candidates,
            *inventory.unmatched_nonterminal_candidates,
        )
        live = [
            candidate for candidate in candidates if candidate.state not in _TERMINAL_WRITE_STATES
        ]
        if {candidate.job_id for candidate in live} != set(inventory.scope_nonterminal_job_ids):
            raise GovernanceError("governance_inflight_candidate_unproven")
        for candidate in live:
            if dispositions.get(candidate.job_id) == "terminal_cleanup":
                raise GovernanceError("governance_inflight_disposition_unproven")
            if not candidate.request_id.startswith("cmh2:"):
                raise GovernanceError("governance_inflight_invocation_unproven")
            if candidate.job_id not in inputs:
                raise GovernanceError("governance_candidate_payload_unproven")
            event, request = inputs[candidate.job_id]
            if request is None:
                raise GovernanceError("governance_candidate_payload_unproven")
            try:
                actual = sdk_generation_input_for(request)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise GovernanceError("governance_inflight_input_unproven") from None
            proof = self.admissions.proof_in(db, candidate.request_id, event=event, actual=actual)
            if proof.binding.project_id != candidate.scope_id:
                raise GovernanceError("governance_inflight_scope_unproven")
            if proof.state != "enrolled":
                raise GovernanceError("governance_inflight_invocation_unproven")
        # UNKNOWN's observation blocker is waived only after every live request
        # above has a complete, independently witnessed ENROLLED proof.

    def prepare(self, scope):
        inventory, inputs, targets, dispositions = self._inspect(scope, signed=False)
        try:
            with self.admissions.access._connection(writing=True) as db:
                self._prove_live(db, inventory, inputs, dispositions)
        except (AccessError, sqlite3.Error, OSError):
            raise GovernanceError("governance_inflight_store_unavailable") from None
        return targets

    def apply(self, scope, *, completed=False):
        self._scope_marker(scope, signed=True)
        if completed and not any(identity.startswith(PREFIX) for identity in scope.records):
            return ()
        inventory, inputs, _, dispositions = self._inspect(
            scope, signed=True, anchored_only=completed
        )
        queue = self.ingest._queue()
        try:
            # Keep this same writer lock through all CAS operations. A competing
            # SDK permit cannot commit between the negative proof and cancellation.
            with self.admissions.access._connection(writing=True) as db:
                self._prove_live(db, inventory, inputs, dispositions)
                for candidate in inventory.confirmed_original_candidates:
                    if candidate.state in _TERMINAL_WRITE_STATES:
                        continue
                    result = queue.cancel_unpermitted_for_governance(
                        candidate.job_id,
                        expected_request_id=candidate.request_id,
                        expected_payload_digest=candidate.input_digest,
                        expected_updated_at=candidate.updated_at,
                        expected_state=candidate.state,
                        expected_hy_invoked=int(candidate.hy_invoked),
                    )
                    if result != "cancelled":
                        raise GovernanceError("governance_inflight_queue_conflict")
        except (AccessError, sqlite3.Error, OSError):
            raise GovernanceError("governance_inflight_store_unavailable") from None
        except (ValueError, TypeError):
            raise GovernanceError("governance_inflight_queue_unproven") from None
        return tuple(
            PayloadTarget(
                candidate.job_id,
                candidate.request_id,
                candidate.input_digest,
                dispositions[candidate.job_id],
                _message_texts(inputs, candidate),
            )
            for candidate in inventory.confirmed_original_candidates
        )
