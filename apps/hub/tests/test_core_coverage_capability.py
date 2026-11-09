"""ADR-0047: explicit coverage requirements never rewrite history or imply cleanup."""

import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal
from test_execution_capability import history
from test_governance_execution_store import database_rows
from test_governance_journal import intent
from test_governance_journal import journal as journal_fixture

journal = journal_fixture
CAPABILITY = "execution_file_coverage_v2"
SDK = "sdk-two-domains-v1"
CORE = "sdk-core-four-domains-v1"


def bound(journal):
    journal.upgrade_v2(str(uuid4()))
    identity = str(uuid4())
    journal.bind_execution_store(identity)
    return identity


def test_coverage_activation_preserves_actual_history_and_is_idempotent(journal):
    item = intent(journal)
    head = journal.append(item)
    identity = bound(journal)
    original = history(journal)
    assert journal.file_coverage_profile() == SDK
    journal.bind_file_coverage(identity)
    assert journal.file_coverage_profile() == CORE
    assert history(journal) == original
    assert journal.head(item.scope_id) == head
    before = database_rows(journal.path)
    journal.bind_file_coverage(identity)
    assert database_rows(journal.path) == before
    reopened = GovernanceJournal(
        journal.path, instance_id=journal.instance_id, key=journal._key, read_only=True
    )
    assert reopened.file_coverage_profile() == CORE


@pytest.mark.parametrize("upgrade", [False, True])
def test_coverage_requires_existing_execution_binding_without_upgrading(journal, upgrade):
    if upgrade:
        journal.upgrade_v2(str(uuid4()))
    before = database_rows(journal.path)
    with pytest.raises(GovernanceError, match="^governance_execution_binding_required$"):
        journal.bind_file_coverage(str(uuid4()))
    assert database_rows(journal.path) == before
    assert journal.file_coverage_profile() == SDK


def test_wrong_store_and_readonly_activation_change_no_rows(journal):
    identity = bound(journal)
    before = database_rows(journal.path)
    with pytest.raises(GovernanceError, match="^governance_capability_mismatch$"):
        journal.bind_file_coverage(str(uuid4()))
    readonly = GovernanceJournal(
        journal.path, instance_id=journal.instance_id, key=journal._key, read_only=True
    )
    for value in (identity, None):
        with pytest.raises(GovernanceError, match="^governance_journal_read_only$"):
            readonly.bind_file_coverage(value)
    assert database_rows(journal.path) == before


@pytest.mark.parametrize("damage", ["different_store", "missing_store", "signature", "unknown"])
def test_coverage_capability_is_closed_signed_and_bound_to_the_actual_store(journal, damage):
    identity = bound(journal)
    journal.bind_file_coverage(identity)
    with sqlite3.connect(journal.path) as db:
        if damage == "different_store":
            other = str(uuid4())
            db.execute(
                "UPDATE capabilities SET value=?,signature=? WHERE name=?",
                (other, journal._mac("capability", [CAPABILITY, other]), CAPABILITY),
            )
        elif damage == "missing_store":
            db.execute("DELETE FROM capabilities WHERE name='execution_store_v2'")
        elif damage == "signature":
            db.execute("UPDATE capabilities SET signature=? WHERE name=?", ("0" * 64, CAPABILITY))
        else:
            db.execute("INSERT INTO capabilities VALUES ('unknown',?,?)", (identity, "0" * 64))
    before = database_rows(journal.path)
    with pytest.raises(GovernanceError, match="^governance_capability_invalid$"):
        journal.file_coverage_profile()
    assert database_rows(journal.path) == before


def test_missing_coverage_remains_legacy_without_implicitly_restoring_capability(journal):
    identity = bound(journal)
    journal.bind_file_coverage(identity)
    with sqlite3.connect(journal.path) as db:
        db.execute("DELETE FROM capabilities WHERE name=?", (CAPABILITY,))
    before = database_rows(journal.path)
    assert journal.file_coverage_profile() == SDK
    assert database_rows(journal.path) == before
