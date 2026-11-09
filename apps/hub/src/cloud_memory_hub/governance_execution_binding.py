"""ADR-0046: one format selection and fresh signed business/recovery views.

This layer never creates, migrates, activates or completes an execution store.
It does not enable the schema2 runtime or replace Authority's external witnesses.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from cloud_memory_hub.governance_diagnostic_profiles import SDK_PROFILE, coverage_satisfies
from cloud_memory_hub.governance_execution_store import (
    ExecutionStore as ExecutionStoreV2,
)
from cloud_memory_hub.governance_execution_store import (
    OperationSnapshot,
    RecoverySnapshot,
)
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal, Intent

if TYPE_CHECKING:
    from cloud_memory_hub.governance_execution import ExecutionStore as LegacyExecutionStore


@dataclass(frozen=True, slots=True)
class ExecutionState:
    operations: Mapping[str, OperationSnapshot]
    business_plans: tuple[str, ...]
    committed: tuple[str, ...]
    orphans: tuple[str, ...]
    unplanned: tuple[Intent, ...]
    pending: tuple[str, ...]
    recovery: RecoverySnapshot | None
    required_profile: str = SDK_PROFILE

    def __post_init__(self):
        object.__setattr__(self, "operations", MappingProxyType(dict(self.operations)))


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    journal: GovernanceJournal
    store: "LegacyExecutionStore | ExecutionStoreV2"
    version: Literal[1, 2]
    read_only: bool
    _store_id: str | None = None

    def __post_init__(self):
        from cloud_memory_hub.governance_execution import ExecutionStore as LegacyExecutionStore

        if (
            type(self.version) is not int
            or self.version not in (1, 2)
            or type(self.read_only) is not bool
            or self.journal.read_only
            and not self.read_only
            or not isinstance(
                self.store, LegacyExecutionStore if self.version == 1 else ExecutionStoreV2
            )
            or self.store.journal is not self.journal
            or self.store.read_only != self.read_only
            or (self.version == 1) != (self._store_id is None)
        ):
            raise GovernanceError("governance_execution_binding_mismatch")

    def read_state(self) -> ExecutionState:
        """Fresh classification; pending includes orphan and unplanned IDs.

        Business plans retain ordinary orphans for conservative suppression.
        They are not the committed-only set that authorizes recovery mutations.
        """
        if self.version == 2:
            recovery = self.store.recovery_snapshot()
            if recovery.snapshot.binding.store_id != self._store_id:
                raise GovernanceError("governance_execution_binding_mismatch")
            operations = recovery.snapshot.operations
            committed, orphans, unplanned = (
                recovery.committed,
                recovery.orphans,
                recovery.unplanned,
            )
            required_profile = recovery.required_profile
        else:
            recovery = None
            required_profile = SDK_PROFILE
            snapshot = ExecutionStoreV2(self.journal, read_only=True).legacy_recovery_snapshot(
                allow_missing=True
            )
            operations = snapshot.operations
            committed, orphans, unplanned = (
                snapshot.committed,
                snapshot.orphans,
                snapshot.unplanned,
            )
        business = tuple(
            identity
            for identity, operation in sorted(operations.items())
            if operation.cleanup is None or operation.cleanup.authority.mode != "repair"
        )
        pending = set(orphans) | {intent.operation_id for intent in unplanned}
        pending.update(
            identity
            for identity, operation in operations.items()
            if operation.state != "completed"
            or operation.cleanup is not None
            and not coverage_satisfies(operation.cleanup, required_profile)
        )
        return ExecutionState(
            operations,
            business,
            tuple(sorted(committed)),
            tuple(sorted(orphans)),
            tuple(sorted(unplanned, key=lambda intent: intent.operation_id)),
            tuple(sorted(pending)),
            recovery,
            required_profile,
        )

    def suppressed(self, scope_id: str) -> dict[str, str]:
        state = self.read_state()
        result = {}
        for identity in state.business_plans:
            operation = state.operations[identity]
            if operation.intent.scope_id != scope_id:
                continue
            for record, kind in operation.records.items():
                if operation.intent.action == "purge":
                    result[record] = "purged"
                else:
                    result.setdefault(record, kind)
        return result


def select_execution_store(
    journal: GovernanceJournal, *, read_only: bool = False
) -> ExecutionBinding:
    """Select exactly one format; existing bindings never silently change format."""
    if type(read_only) is not bool:
        raise GovernanceError("governance_execution_mode_invalid")
    read_only = read_only or journal.read_only
    store_id = journal.execution_store_id()
    if store_id is not None:
        store = ExecutionStoreV2(journal, read_only=read_only)
        binding = ExecutionBinding(journal, store, 2, read_only, store_id)
        binding.read_state()
        return binding
    # Runtime import avoids SourceSuppression -> binding -> legacy execution
    # importing SourceSuppression again while that module is being initialized.
    from cloud_memory_hub.governance_execution import ExecutionStore as LegacyExecutionStore

    store = LegacyExecutionStore(journal, read_only=read_only)
    binding = ExecutionBinding(journal, store, 1, read_only)
    try:
        binding.read_state()
    except GovernanceError as error:
        if error.args != ("governance_execution_version_unsupported",):
            raise
        # Only a verified schema2 commit/capability gap gets the actionable
        # migration error. Unknown schemas or damaged gaps retain their failure.
        ExecutionStoreV2(journal, read_only=True).activation_binding()
        raise GovernanceError("governance_execution_migration_required") from None
    return binding
