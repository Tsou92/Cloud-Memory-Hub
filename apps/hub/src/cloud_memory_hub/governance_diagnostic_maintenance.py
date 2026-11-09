"""Stopped-service diagnostic maintenance, independent of startup replay/SDK.

Inspection and explicit coverage activation remain available while ordinary
recovery refuses to open the business service. Neither entry deletes files.
"""

from dataclasses import dataclass, replace
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
from cloud_memory_hub.governance_diagnostic_profiles import CORE_PROFILE
from cloud_memory_hub.governance_diagnostic_records import WriteRequest
from cloud_memory_hub.governance_diagnostic_recovery import build_recovery_graph
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_inflight import InflightGovernance
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.source_history import PREFIX
from cloud_memory_hub.storage_lease import StorageLease
from cloud_memory_hub.write_admissions import WriteAdmissions


@dataclass(frozen=True, slots=True)
class _StoppedQueueLocation:
    """Only a path for the upstream read-only database pool; no queue lifecycle."""

    database_path: Path


def _coverage_activation_state(config):
    """Pure pre/post validation, including external witnesses and actual plans."""
    authority = open_authority(config, read_only=True)
    with authority.access._connection() as db:
        heads = authority._refresh(db, refresh_witness=False)
        witnesses = tuple(
            tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("cmh_governance_binding", "cmh_governance_floors")
        )
        recovery = ExecutionStore(authority.journal, read_only=True).recovery_snapshot()
        graph = build_recovery_graph(recovery)
    pending = set(graph.pending) | set(graph.orphans)
    pending.update(intent.operation_id for intent in graph.unplanned)
    return authority.journal, heads, witnesses, recovery, len(pending)


def activate_file_coverage(config) -> dict:
    """Explicitly raise a verified existing store's requirements under exclusion.

    The only mutation is the bound main-journal capability. No migration,
    witness refresh, cleanup, service construction or recovery is performed.
    """
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        journal, heads, witnesses, before, _ = _coverage_activation_state(config)
        identity = before.snapshot.binding.store_id
        changed = before.required_profile != CORE_PROFILE
        if changed:
            writer = GovernanceJournal(
                journal.path, instance_id=journal.instance_id, key=journal._key
            )
            writer.bind_file_coverage(identity)
        _, after_heads, after_witnesses, after, pending = _coverage_activation_state(config)
        if (
            heads != after_heads
            or witnesses != after_witnesses
            or replace(before, required_profile=CORE_PROFILE) != after
        ):
            raise GovernanceError("governance_coverage_activation_changed")
        return {
            "version": 1,
            "required_profile": after.required_profile,
            "store_id": identity,
            "pending": pending,
            "changed": changed,
        }


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
