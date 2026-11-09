"""ADR-0046: actual composition shares a format and observes all pending work."""

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
from cloud_memory_hub.governance_execution import ExecutionStore as LegacyStore
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError
from test_governance_execution_store import database_rows, make_intent
from test_original_archive import config_for


def test_actual_application_shares_one_binding_without_creating_execution_storage(tmp_path):
    app = create_hub(config_for(tmp_path))
    state = app.core.state
    binding = state.cmh_governance.execution
    assert state.cmh_executor.execution is binding
    assert state.cmh_executor.store is binding.store
    assert state.cmh_generation.suppressions.execution is binding
    assert state.cmh_generation.suppressions.store is binding.store
    assert state.cmh_ingest.suppressed.__self__ is binding
    assert binding.version == 1
    assert not binding.store.path.exists()
    assert binding.read_state().operations == {}
    assert not binding.store.path.exists()
    assert state.cmh_governance.runtime_recovery_required


def test_authority_reports_orphan_and_missing_plan_as_pending(tmp_path):
    authority = open_authority(config_for(tmp_path))
    store = LegacyStore(authority.journal)
    orphan = make_intent(authority.journal)
    store.plan(orphan, {"synthetic-memory": "memory"})
    pending, planned = authority._pending()
    assert pending == [orphan]
    assert planned[orphan.operation_id] == ("planned", orphan)
    # A never-committed plan suppresses conservatively, but grants no deletion.
    assert authority.execution.suppressed(orphan.scope_id) == {"synthetic-memory": "memory"}
    missing = make_intent(authority.journal, record="unplanned-memory")
    authority.append(missing)
    pending, planned = authority._pending()
    assert {item.operation_id for item in pending} == {orphan.operation_id, missing.operation_id}
    assert missing.operation_id not in planned
    with pytest.raises(GovernanceError, match="governance_execution_pending"):
        authority.require_ready()


def test_authority_sees_pending_files_below_completed_legacy_parent(tmp_path):
    config = config_for(tmp_path)
    authority = open_authority(config)
    journal = authority.journal
    legacy = LegacyStore(journal)
    intent = make_intent(journal, action="purge")
    legacy.plan(intent, {"synthetic-memory": "memory"})
    authority.append(intent)
    legacy.complete(intent)
    store = ExecutionStore(journal)
    binding = store.migrate_legacy(
        store.inspect_legacy(), {intent.operation_id: CleanupAuthority("legacy", (), ())}
    )
    journal.bind_execution_store(binding.store_id)
    # A new stopped-service authority sees the actual version. No runtime
    # feature flag or fabricated complete receipt is used by this observation.
    authority = open_authority(config)
    before = database_rows(store.path), database_rows(journal.path)
    pending, planned = authority._pending()
    assert authority.execution.version == 2
    assert pending == [intent]
    assert planned[intent.operation_id] == ("completed", intent)
    assert (database_rows(store.path), database_rows(journal.path)) == before
    # Full output coverage is not enabled by merely choosing the new store.
    with pytest.raises(GovernanceError, match="governance_execution_runtime_unsupported"):
        authority.require_ready()


@pytest.mark.parametrize("existing", [False, True])
def test_readonly_legacy_store_rejects_write_before_open_or_creation(tmp_path, existing):
    authority = open_authority(config_for(tmp_path))
    intent = make_intent(authority.journal)
    ordinary = LegacyStore(authority.journal)
    if existing:
        ordinary.plan(intent, {"synthetic-memory": "memory"})
    before = database_rows(ordinary.path) if existing else None
    readonly = LegacyStore(authority.journal, read_only=True)
    for write in (
        lambda: readonly.plan(intent, {"synthetic-memory": "memory"}),
        lambda: readonly.complete(intent),
        lambda: readonly.discard(intent.operation_id),
    ):
        with pytest.raises(GovernanceError, match="governance_execution_read_only"):
            write()
    if existing:
        assert database_rows(ordinary.path) == before
        assert readonly.operations() == ordinary.operations()
    else:
        assert readonly.operations() == {}
        assert not ordinary.path.exists()
