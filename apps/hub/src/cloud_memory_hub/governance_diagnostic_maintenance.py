"""Stopped-service diagnostic inspection, independent of startup replay/SDK.

This entry produces bounded metadata candidates, not deletion confirmation.
It can run while ordinary recovery refuses to open the business service.
"""

from dataclasses import dataclass
from pathlib import Path

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_candidates import ConfirmedScope
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    RepairParent,
    cleanup_digest,
    cleanup_to_dict,
)
from cloud_memory_hub.governance_diagnostic_inventory import inspect_diagnostic_files
from cloud_memory_hub.governance_diagnostic_records import WriteRequest
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_inflight import InflightGovernance
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.source_history import PREFIX
from cloud_memory_hub.storage_lease import StorageLease
from cloud_memory_hub.write_admissions import WriteAdmissions


@dataclass(frozen=True, slots=True)
class _StoppedQueueLocation:
    """Only a path for the upstream read-only database pool; no queue lifecycle."""

    database_path: Path


def preview_diagnostic_repair(config, operation_id: str) -> dict:
    """Acquire real process exclusion and inspect an existing committed purge.

    Neither the business application nor its automatic resume path is created.
    Old execution data is validated in its original format, never migrated here.
    """
    try:
        canonical_uuid(operation_id)
    except (ValueError, TypeError, AttributeError):
        raise GovernanceError("governance_operation_invalid") from None
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        return _preview_locked(config, operation_id)


def _preview_locked(config, operation_id):
    authority = open_authority(config, read_only=True)
    journal = authority.journal
    store = ExecutionStore(journal, read_only=True)
    if journal.execution_store_id() is None:
        try:
            # The execution migration may have committed before the main
            # capability. Verify its exact baseline; inspection must not repair
            # that gap or mistake it for a schema-1 store.
            store.activation_binding()
        except GovernanceError as error:
            if error.args != ("governance_execution_version_unsupported",):
                raise
            snapshot = store.inspect_legacy()
            operations, committed = snapshot.operations, snapshot.committed
        else:
            operations, committed = store.snapshot().operations, None
    else:
        store.require_capability()
        snapshot = store.recovery_snapshot()
        operations, committed = snapshot.snapshot.operations, snapshot.committed
    operation = operations.get(operation_id)
    if operation is None or committed is not None and operation_id not in committed:
        raise GovernanceError("governance_diagnostic_parent_unproven")
    intent = operation.intent
    if intent.action != "purge" or (
        operation.cleanup is not None and operation.cleanup.authority.mode == "repair"
    ):
        raise GovernanceError("governance_diagnostic_parent_unproven")
    # Fetch the actual chain digest, not a local serialization digest or cache flag.
    with journal._connection() as db:
        journal._verify(db)
        row = db.execute("SELECT * FROM intents WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or journal._decode(row) != intent:
            raise GovernanceError("governance_diagnostic_parent_unproven")
        parent = RepairParent(operation_id, row["digest"], journal.ledger_id)
    selectors = ()
    if any(identity.startswith(PREFIX) for identity in operation.records):
        # Only whole accepted originals need the original request certificates.
        # An older memory-only intent has no such selection to reconstruct;
        # its empty selection still must pass the actual full file scan below.
        main_path = config.data_dir / "one-memory.sqlite3"
        DeviceAccessStore._check_private(main_path)
        location = _StoppedQueueLocation(main_path)
        ingest = RemoteIngest(authority.access, lambda: location, read_only=True)
        admissions = WriteAdmissions(authority.access, journal, read_only=True)
        planner = InflightGovernance(ingest, admissions, journal)
        scope = ConfirmedScope(
            scope_id=intent.scope_id,
            action="purge",
            records=operation.records,
            rule_targets=intent.targets,
            basis="operation",
            operation_id=operation_id,
        )
        selectors = planner.prepare_cleanup_selectors(
            scope, signed=True, completed=operation.state == "completed"
        ).selectors
    requests = tuple(
        WriteRequest(selector.request_id, f"cmh:{journal.instance_id}:{intent.scope_id}")
        for selector in selectors
    )
    inventory = inspect_diagnostic_files(
        data_root=config.data_dir.absolute(),
        sdk_log_root=config.data_dir.absolute() / "hy-memory" / "governed-v2" / "logs",
        requests=requests,
        mode="propose",
    )
    selection = CleanupAuthority("repair", selectors, inventory.grants, parent)
    return {
        "version": 1,
        "operation_id": operation_id,
        "scope_id": intent.scope_id,
        "operation_state": operation.state,
        "candidate_digest": cleanup_digest(selection),
        "selection": cleanup_to_dict(selection),
        "domains": [{"domain": item.domain, "exists": item.exists} for item in inventory.domains],
        "files": len(inventory.files),
        "records": inventory.record_count,
        "candidate_copies": sum(item.candidate_count for item in inventory.files),
        "origin_proven": False,
        "body_reviewed": False,
        "execution_available": False,
    }
