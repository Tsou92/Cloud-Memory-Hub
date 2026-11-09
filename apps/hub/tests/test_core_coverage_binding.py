"""The live binding cannot mistake cached SDK progress for Core coverage."""

from cloud_memory_hub.governance_execution_binding import select_execution_store
from test_governance_execution_store import (
    bound_store,
    database_rows,
    finish_pass,
    planned_purge,
)
from test_governance_execution_store import journal as journal_fixture

journal = journal_fixture


def test_existing_binding_observes_raised_profile_and_preserves_business_rows(journal):
    store = bound_store(journal)
    intent, _ = planned_purge(journal, store)
    receipt = finish_pass(journal, store, store.cleanup(intent.operation_id))
    store.complete(intent, expected_cleanup=receipt)
    binding = select_execution_store(journal)
    assert binding.read_state().pending == ()
    before = database_rows(store.path)
    journal.bind_file_coverage(store.snapshot().binding.store_id)
    current = binding.read_state()
    assert current.pending == (intent.operation_id,)
    assert current.required_profile == "sdk-core-four-domains-v1"
    assert current.recovery.required_profile == current.required_profile
    assert current.business_plans == (intent.operation_id,)
    assert current.operations[intent.operation_id].state == "completed"
    assert current.operations[intent.operation_id].cleanup.authority == receipt.authority
    assert database_rows(store.path) == before


def test_legacy_binding_exposes_only_its_actual_old_profile_without_creating_files(journal):
    before = database_rows(journal.path), tuple(journal.path.parent.iterdir())
    binding = select_execution_store(journal, read_only=True)
    assert binding.version == 1
    assert binding.read_state().required_profile == "sdk-two-domains-v1"
    assert not binding.store.path.exists()
    assert (database_rows(journal.path), tuple(journal.path.parent.iterdir())) == before
