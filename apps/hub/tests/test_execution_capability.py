"""Explicit capability binds the new store without rewriting governance history."""

import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal
from cloud_memory_hub.write_admissions import AdmissionJournal
from test_governance_journal import intent, reopen
from test_governance_journal import journal as journal_fixture

journal = journal_fixture


def history(journal):
    with journal._connection() as db:
        return {
            name: [tuple(row) for row in db.execute("SELECT * FROM " + name)]
            for name in ("binding", "heads", "intents")
        }


def test_explicit_capability_preserves_heads_and_is_idempotent(journal):
    value = intent(journal)
    checkpoint = journal.append(value)
    admission = str(uuid4())
    journal.upgrade_v2(admission)
    before = history(journal)
    store_id = str(uuid4())
    assert journal.execution_store_id() is None
    journal.bind_execution_store(store_id)
    assert history(journal) == before
    assert journal.head(value.scope_id) == checkpoint
    assert reopen(journal).execution_store_id() == store_id
    assert journal.admissions_ledger_id() == admission
    journal.bind_execution_store(store_id)
    assert history(journal) == before
    with pytest.raises(GovernanceError, match="capability_mismatch"):
        journal.bind_execution_store(str(uuid4()))


def test_capability_requires_admissions_and_never_implicitly_upgrades(journal):
    before = history(journal)
    with pytest.raises(GovernanceError, match="admissions_binding_required"):
        journal.bind_execution_store(str(uuid4()))
    assert journal.schema_version == 1
    assert history(journal) == before


@pytest.mark.parametrize("damage", ["unknown", "missing_admissions", "bad_signature", "bad_uuid"])
def test_closed_capability_table_rejects_invalid_new_bindings(journal, damage):
    journal.upgrade_v2(str(uuid4()))
    journal.bind_execution_store(str(uuid4()))
    with sqlite3.connect(journal.path) as db:
        if damage == "unknown":
            db.execute("INSERT INTO capabilities VALUES ('unknown',?,?)", (str(uuid4()), "a" * 64))
        elif damage == "missing_admissions":
            db.execute("DELETE FROM capabilities WHERE name='admissions_ledger_id'")
        elif damage == "bad_signature":
            db.execute(
                "UPDATE capabilities SET signature=? WHERE name='execution_store_v2'", ("a" * 64,)
            )
        else:
            db.execute("UPDATE capabilities SET value='invalid' WHERE name='execution_store_v2'")
    with pytest.raises(GovernanceError, match="capability_invalid"):
        reopen(journal)


def test_readonly_journal_rejects_mutators_before_validation_or_callbacks(journal, monkeypatch):
    read = GovernanceJournal(
        journal.path, instance_id=journal.instance_id, key=journal._key, read_only=True
    )
    before = history(journal)
    for function in (read.append, read.upgrade_v2, read.bind_execution_store):
        with pytest.raises(GovernanceError, match="journal_read_only"):
            function(None)
    with read._connection() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM heads")
    assert history(journal) == before


def test_admissions_journal_rejects_main_capabilities(tmp_path):
    admission = AdmissionJournal.initialize(
        tmp_path / "admissions.sqlite3", instance_id=str(uuid4()), key=b"x" * 32
    )
    with sqlite3.connect(admission.path) as db:
        identity = str(uuid4())
        db.execute(
            "INSERT INTO capabilities VALUES ('admissions_ledger_id',?,?)",
            (identity, admission._mac("capability", ["admissions_ledger_id", identity])),
        )
    with pytest.raises(GovernanceError, match="capability_invalid"):
        AdmissionJournal(admission.path, instance_id=admission.instance_id, key=admission._key)


def test_legacy_service_refuses_new_capability_even_when_main_journal_is_empty(tmp_path):
    from cloud_memory_hub.app import create_hub
    from cloud_memory_hub.governance_authority import open_authority
    from test_original_archive import config_for

    config = config_for(tmp_path)
    authority = open_authority(config)
    assert authority.verify() == {}
    authority.journal.bind_execution_store(str(uuid4()))
    with pytest.raises(GovernanceError, match="execution_runtime_unsupported"):
        authority.require_ready()
    with pytest.raises(GovernanceError, match="execution_runtime_unsupported"):
        create_hub(config)


def test_empty_migration_gap_cannot_skip_existing_execution_store_validation(tmp_path):
    from cloud_memory_hub.app import create_hub
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_execution_store import ExecutionStore
    from test_original_archive import config_for

    config = config_for(tmp_path)
    authority = open_authority(config)
    store = ExecutionStore(authority.journal)
    store.initialize_empty()
    assert authority.journal.execution_store_id() is None
    assert authority.verify() == {}
    with pytest.raises(GovernanceError, match="execution_version_unsupported"):
        create_hub(config)
