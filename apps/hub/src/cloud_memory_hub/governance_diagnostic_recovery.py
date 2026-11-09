"""ADR-0045: resolve existing signed cleanup choices without granting new rights.

Inputs must come from Store2.recovery_snapshot(), whose single journal/execution
snapshot verifies signatures, binding and parent relations. These pure views do
not replace that verification, inspect files, update progress or complete a purge.
The file executor must reject graph.unplanned before any file or progress I/O.
"""

from dataclasses import dataclass

from cloud_memory_hub.governance_diagnostic_confirmation import cleanup_digest
from cloud_memory_hub.governance_diagnostic_profiles import coverage_satisfies
from cloud_memory_hub.governance_diagnostic_records import (
    MAX_SELECTION,
    RecordGrant,
    WriteRequest,
)
from cloud_memory_hub.governance_execution_store import CleanupContributor, RecoverySnapshot
from cloud_memory_hub.governance_journal import GovernanceError, Intent


@dataclass(frozen=True, slots=True)
class RecoveryBudget:
    max_operations: int = MAX_SELECTION
    max_requests: int = MAX_SELECTION
    max_grants: int = MAX_SELECTION
    max_contributors: int = MAX_SELECTION

    def __post_init__(self):
        if any(
            type(value) is not int or not 0 < value <= MAX_SELECTION
            for value in (
                self.max_operations,
                self.max_requests,
                self.max_grants,
                self.max_contributors,
            )
        ):
            raise GovernanceError("diagnostic_recovery_budget_invalid")


@dataclass(frozen=True, slots=True)
class PurgeFamily:
    parent_id: str
    repair_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedCleanup:
    contributors: tuple[CleanupContributor, ...]
    requests: tuple[WriteRequest, ...]
    grants: tuple[RecordGrant, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticRecoveryGraph:
    business_operations: tuple[str, ...]
    repair_operations: tuple[str, ...]
    families: tuple[PurgeFamily, ...]
    orphans: tuple[str, ...]
    unplanned: tuple[Intent, ...]
    pending: tuple[str, ...]
    authority: ResolvedCleanup


def _budget(recovery, budget):
    budget = RecoveryBudget() if budget is None else budget
    if type(budget) is not RecoveryBudget:
        raise GovernanceError("diagnostic_recovery_budget_invalid")
    budget.__post_init__()
    if type(recovery) is not RecoverySnapshot:
        raise GovernanceError("governance_diagnostic_recovery_invalid")
    if len(recovery.snapshot.operations) + len(recovery.unplanned) > budget.max_operations:
        raise GovernanceError("diagnostic_recovery_budget_exceeded")
    return budget


def _parent(recovery, operation):
    cleanup = operation.cleanup
    if cleanup.authority.mode != "repair":
        return None
    reference = cleanup.authority.parent
    parent = recovery.snapshot.operations.get(reference.operation_id)
    if (
        reference.operation_id == operation.intent.operation_id
        or parent is None
        or parent.intent.action != "purge"
        or parent.cleanup is None
        or parent.cleanup.orphan_only
        or parent.cleanup.authority.mode == "repair"
        or parent.intent.scope_id != operation.intent.scope_id
        or parent.cleanup.authority.selectors != cleanup.authority.selectors
        or reference.ledger_id != recovery.snapshot.binding.ledger_id
        or recovery.committed_digests.get(reference.operation_id) != reference.intent_digest
    ):
        raise GovernanceError("governance_cleanup_parent_invalid")
    return reference.operation_id


def _resolve(recovery, contributors, budget):
    if type(contributors) is not tuple:
        raise GovernanceError("governance_cleanup_contributor_invalid")
    if len(contributors) > budget.max_contributors:
        raise GovernanceError("diagnostic_recovery_budget_exceeded")
    by_operation, requests, jobs, grants = {}, {}, {}, set()
    for reference in contributors:
        if type(reference) is not CleanupContributor:
            raise GovernanceError("governance_cleanup_contributor_invalid")
        reference.__post_init__()
        if reference.operation_id in by_operation:
            raise GovernanceError("governance_cleanup_contributor_invalid")
        operation = recovery.snapshot.operations.get(reference.operation_id)
        if (
            operation is None
            or operation.intent.action != "purge"
            or operation.cleanup is None
            or operation.cleanup.orphan_only
            or recovery.committed_digests.get(reference.operation_id) != reference.intent_digest
            or cleanup_digest(operation.cleanup.authority) != reference.cleanup_digest
        ):
            raise GovernanceError("governance_cleanup_contributor_invalid")
        _parent(recovery, operation)
        by_operation[reference.operation_id] = reference
        namespace = f"cmh:{recovery.snapshot.binding.instance_id}:{operation.intent.scope_id}"
        for selector in operation.cleanup.authority.selectors:
            birth = (
                namespace,
                selector.job_id,
                selector.request_id,
                selector.input_digest,
                selector.disposition,
            )
            if (
                selector.request_id in requests
                and requests[selector.request_id] != birth
                or selector.job_id in jobs
                and jobs[selector.job_id] != birth
            ):
                raise GovernanceError("governance_diagnostic_request_conflict")
            requests[selector.request_id] = birth
            jobs[selector.job_id] = birth
            if len(requests) > budget.max_requests:
                raise GovernanceError("diagnostic_recovery_budget_exceeded")
        for grant in operation.cleanup.authority.grants:
            grants.add(grant)
            if len(grants) > budget.max_grants:
                raise GovernanceError("diagnostic_recovery_budget_exceeded")
    return ResolvedCleanup(
        tuple(sorted(by_operation.values())),
        tuple(WriteRequest(identity, requests[identity][0]) for identity in sorted(requests)),
        tuple(sorted(grants)),
    )


def resolve_cleanup_authority(
    recovery: RecoverySnapshot,
    contributors: tuple[CleanupContributor, ...],
    *,
    owner_operation_id: str,
    budget: RecoveryBudget | None = None,
) -> ResolvedCleanup:
    """Resolve frozen pass references; unrelated unplanned intents stay diagnosable.

    Neither completed operations nor verified progress are prerequisites for rights.
    A v1 pass may name only its repair owner; the actual parent is still verified.
    """
    budget = _budget(recovery, budget)
    result = _resolve(recovery, contributors, budget)
    if not any(item.operation_id == owner_operation_id for item in result.contributors):
        raise GovernanceError("governance_cleanup_contributor_invalid")
    return result


def build_recovery_graph(
    recovery: RecoverySnapshot, *, budget: RecoveryBudget | None = None
) -> DiagnosticRecoveryGraph:
    """Classify the complete snapshot and union only actual committed choices."""
    budget = _budget(recovery, budget)
    operations = recovery.snapshot.operations
    committed, orphans = set(recovery.committed), set(recovery.orphans)
    unplanned_ids = {intent.operation_id for intent in recovery.unplanned}
    if (
        len(committed) != len(recovery.committed)
        or len(orphans) != len(recovery.orphans)
        or len(unplanned_ids) != len(recovery.unplanned)
        or committed & orphans
        or committed | orphans != set(operations)
        or unplanned_ids & set(operations)
        or set(recovery.committed_digests) != committed
    ):
        raise GovernanceError("governance_diagnostic_recovery_invalid")
    business, repairs, families, contributors = [], [], {}, []
    for identity in sorted(committed):
        operation = operations[identity]
        if operation.intent.action != "purge":
            business.append(identity)
            continue
        if operation.cleanup is None or operation.cleanup.orphan_only:
            raise GovernanceError("governance_diagnostic_recovery_invalid")
        parent = _parent(recovery, operation)
        if parent is None:
            business.append(identity)
            families.setdefault(identity, [])
        else:
            repairs.append(identity)
            families.setdefault(parent, []).append(identity)
        contributors.append(
            CleanupContributor(
                identity,
                recovery.committed_digests[identity],
                cleanup_digest(operation.cleanup.authority),
            )
        )
    authority = _resolve(recovery, tuple(contributors), budget)
    pending = unplanned_ids | {
        identity
        for identity, operation in operations.items()
        if operation.state != "completed"
        or operation.cleanup is not None
        and not coverage_satisfies(operation.cleanup, recovery.required_profile)
    }
    return DiagnosticRecoveryGraph(
        tuple(business),
        tuple(repairs),
        tuple(
            PurgeFamily(parent, tuple(sorted(children)))
            for parent, children in sorted(families.items())
        ),
        tuple(sorted(orphans)),
        tuple(sorted(recovery.unplanned, key=lambda intent: intent.operation_id)),
        tuple(sorted(pending)),
        authority,
    )
