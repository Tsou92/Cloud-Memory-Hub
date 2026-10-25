import json
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_journal import (
    Checkpoint,
    GovernanceError,
    GovernanceJournal,
    Intent,
)
from pydantic import ValidationError


@pytest.fixture
def journal(tmp_path):
    return GovernanceJournal.initialize(
        tmp_path / "authority" / "journal.sqlite3", instance_id=str(uuid4()), key=os.urandom(32)
    )


def intent(journal, scope=None, action="forget", identity="synthetic raw record"):
    scope = scope or str(uuid4())
    return Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action=action,
        targets=(journal.target(scope, "raw", identity),),
    )


def reopen(journal):
    return GovernanceJournal(journal.path, instance_id=journal.instance_id, key=journal._key)


def test_private_explicit_initialization_and_binding(journal):
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert journal.path.parent.stat().st_mode & 0o777 == 0o700
    assert reopen(journal).ledger_id == journal.ledger_id
    with pytest.raises(GovernanceError, match="governance_already_exists"):
        GovernanceJournal.initialize(
            journal.path, instance_id=journal.instance_id, key=journal._key
        )
    for instance, key in ((str(uuid4()), journal._key), (journal.instance_id, os.urandom(32))):
        with pytest.raises(GovernanceError, match="governance_binding_mismatch"):
            GovernanceJournal(journal.path, instance_id=instance, key=key)


def test_missing_file_is_never_recreated(journal):
    journal.path.unlink()
    with pytest.raises(GovernanceError, match="governance_store_unavailable"):
        reopen(journal)
    assert not journal.path.exists()


@pytest.mark.parametrize("unsafe", ["permissions", "directory", "symlink", "hardlink"])
def test_unsafe_paths_are_rejected(journal, unsafe):
    if unsafe == "permissions":
        journal.path.chmod(0o644)
    elif unsafe == "directory":
        journal.path.parent.chmod(0o755)
    elif unsafe == "symlink":
        moved = journal.path.with_name("moved.sqlite3")
        journal.path.rename(moved)
        journal.path.symlink_to(moved)
    else:
        os.link(journal.path, journal.path.with_name("alias.sqlite3"))
    with pytest.raises(GovernanceError, match="governance_path_unsafe"):
        reopen(journal)


def test_targets_do_not_persist_content_and_have_distinct_domains(journal):
    content = "synthetic private value " + uuid4().hex
    first = intent(journal, identity=content)
    target = first.targets[0]
    assert target == journal.target(first.scope_id, "raw", content)
    assert target != journal.target("global", "raw", content)
    assert target != journal.target(first.scope_id, "content", content)
    checkpoint = journal.append(first)
    page = journal.changes(first.scope_id)
    assert checkpoint == page["cursor"] == page["head"]
    assert content.encode() not in journal.path.read_bytes()
    assert content not in json.dumps(page["entries"])


def test_retry_order_and_operation_conflict(journal):
    first = intent(journal)
    extra = journal.target(first.scope_id, "memory", str(uuid4()))
    first = Intent.model_validate({**first.model_dump(), "targets": [extra, first.targets[0]]})
    checkpoint = journal.append(first)
    reversed_targets = Intent.model_validate(
        {**first.model_dump(), "targets": list(reversed(first.targets))}
    )
    assert journal.append(reversed_targets) == checkpoint
    assert journal.head(first.scope_id).sequence == 1
    conflicting = first.model_copy(update={"action": "purge"})
    with pytest.raises(GovernanceError, match="governance_operation_conflict"):
        journal.append(conflicting)
    assert journal.head(first.scope_id) == checkpoint


def test_paginated_history_retains_forget_and_purge_without_erasure(journal):
    first = intent(journal)
    checkpoints = []
    for action in ("forget", "purge", "forget"):
        value = first.model_copy(update={"operation_id": str(uuid4()), "action": action})
        checkpoints.append(journal.append(value))
    first_page = journal.changes(first.scope_id, limit=2)
    assert first_page["has_more"] and first_page["cursor"] == checkpoints[1]
    second_page = journal.changes(first.scope_id, after=first_page["cursor"], limit=2)
    assert not second_page["has_more"] and second_page["cursor"] == checkpoints[2]
    actions = [row["intent"]["action"] for row in first_page["entries"] + second_page["entries"]]
    assert actions == ["forget", "purge", "forget"]
    assert journal.changes(first.scope_id, after=checkpoints[2])["entries"] == []
    # A later forget never removes the earlier purge intent; enforcement consumes both.
    assert (
        reopen(journal).changes(first.scope_id)["entries"]
        == journal.changes(first.scope_id)["entries"]
    )


def test_scope_isolation_and_cross_scope_cursor(journal):
    first, other = intent(journal), intent(journal, scope="global")
    checkpoint = journal.append(first)
    journal.append(other)
    assert [
        row["intent"]["operation_id"] for row in journal.changes(first.scope_id)["entries"]
    ] == [first.operation_id]
    with pytest.raises(GovernanceError, match="governance_watermark_diverged"):
        journal.changes("global", after=checkpoint)
    assert journal.head(str(uuid4())).sequence == 0


def test_concurrent_writers_and_identical_retries(journal):
    initial = intent(journal)
    writers = [reopen(journal) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        same = list(pool.map(lambda writer: writer.append(initial), writers))
        assert len({row.digest for row in same}) == 1
        unique = list(
            pool.map(lambda writer: writer.append(intent(writer, initial.scope_id)), writers)
        )
    assert sorted(row.sequence for row in unique) == list(range(2, 8))
    assert reopen(journal).head(initial.scope_id).sequence == 7


def test_restore_old_prefix_and_same_height_fork_are_detected(journal, tmp_path):
    first = intent(journal)
    journal.append(first)
    backup = tmp_path / "old.sqlite3"
    shutil.copy2(journal.path, backup)
    seen = journal.append(intent(journal, first.scope_id, "purge"))
    shutil.copy2(backup, journal.path)
    old = reopen(journal)
    with pytest.raises(GovernanceError, match="governance_watermark_ahead"):
        old.changes(first.scope_id, after=seen)
    fork = old.append(intent(old, first.scope_id, "forget", "different target"))
    assert fork.sequence == seen.sequence and fork.digest != seen.digest
    with pytest.raises(GovernanceError, match="governance_watermark_diverged"):
        old.changes(first.scope_id, after=seen)


def test_replacement_ledger_is_not_silently_accepted(journal, tmp_path):
    first = intent(journal)
    seen = journal.append(first)
    replacement = GovernanceJournal.initialize(
        tmp_path / "replacement" / "journal.sqlite3",
        instance_id=journal.instance_id,
        key=journal._key,
    )
    with pytest.raises(GovernanceError, match="governance_ledger_changed"):
        replacement.changes(first.scope_id, after=seen)


@pytest.mark.parametrize("corruption", ["payload", "missing_entry", "head", "version"])
def test_corruption_is_not_reported_as_empty_rules(journal, corruption):
    first = intent(journal)
    journal.append(first)
    journal.append(intent(journal, first.scope_id))
    with sqlite3.connect(journal.path) as db:
        if corruption == "payload":
            db.execute(
                "UPDATE intents SET payload=replace(payload,'forget','purge') WHERE sequence=1"
            )
        elif corruption == "missing_entry":
            db.execute("DELETE FROM intents WHERE sequence=1")
        elif corruption == "head":
            db.execute("UPDATE heads SET sequence=1")
        else:
            db.execute("PRAGMA user_version=99")
    with pytest.raises(GovernanceError):
        reopen(journal)


def test_process_death_between_intent_and_head_rolls_back(journal, tmp_path):
    first = intent(journal)
    args = tmp_path / "private-crash-input.json"
    args.write_text(
        json.dumps(
            {
                "instance_id": journal.instance_id,
                "key": journal._key.hex(),
                "intent": first.model_dump(mode="json"),
            }
        )
    )
    args.chmod(0o600)
    script = """
import json,os,sys
from pathlib import Path
from contextlib import contextmanager
from cloud_memory_hub.governance_journal import GovernanceJournal,Intent
data=json.loads(Path(sys.argv[2]).read_text())
j=GovernanceJournal(Path(sys.argv[1]),instance_id=data['instance_id'],key=bytes.fromhex(data['key']))
original=j._connection
@contextmanager
def crashing(**kwargs):
    with original(**kwargs) as db:
        def trace(statement):
            if statement.startswith('INSERT INTO heads'):
                os._exit(23)
        db.set_trace_callback(trace)
        yield db
j._connection=crashing
j.append(Intent.model_validate(data['intent']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(journal.path), str(args)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 23
    restored = reopen(journal)
    assert restored.head(first.scope_id).sequence == 0
    assert restored.changes(first.scope_id)["entries"] == []
    assert restored.append(first).sequence == 1


def test_invalid_targets_checkpoints_and_page_limits(journal):
    first = intent(journal)
    with pytest.raises(ValidationError):
        Intent.model_validate({**first.model_dump(), "targets": [first.targets[0]] * 2})
    with pytest.raises(ValidationError):
        Checkpoint(ledger_id=journal.ledger_id, sequence=True, digest="0" * 64)
    for limit in (0, 1001, True, "5"):
        with pytest.raises(GovernanceError, match="governance_page_limit_invalid"):
            journal.changes(first.scope_id, limit=limit)
    with pytest.raises(GovernanceError, match="governance_checkpoint_invalid"):
        journal.changes(first.scope_id, after={"sequence": "invalid"})
    with pytest.raises(GovernanceError, match="governance_target_invalid"):
        journal.target(first.scope_id, [], "synthetic")
    assert reopen(journal).head(first.scope_id).sequence == 0


def test_constructed_invalid_model_cannot_corrupt_journal(journal):
    first = intent(journal)
    invalid = first.model_copy(update={"action": "invalid"})
    with pytest.raises(GovernanceError, match="governance_intent_invalid"):
        journal.append(invalid)
    assert reopen(journal).head(first.scope_id).sequence == 0


def test_change_page_respects_byte_budget_without_skipping_entries(journal, monkeypatch):
    first = intent(journal)
    checkpoints = [journal.append(first)]
    checkpoints.append(journal.append(intent(journal, first.scope_id)))
    # One intent plus its checkpoint fits; two cannot fit together.
    monkeypatch.setattr("cloud_memory_hub.governance_journal.MAX_PAGE_BYTES", 850)
    page = journal.changes(first.scope_id, limit=1000)
    assert page["cursor"] == checkpoints[0] and page["has_more"]
    rest = journal.changes(first.scope_id, after=page["cursor"])
    assert rest["cursor"] == checkpoints[1] and not rest["has_more"]
    monkeypatch.setattr("cloud_memory_hub.governance_journal.MAX_PAGE_BYTES", 100)
    with pytest.raises(GovernanceError, match="governance_entry_too_large"):
        journal.changes(first.scope_id)
