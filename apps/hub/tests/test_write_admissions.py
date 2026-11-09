"""ADR-0038: real SQLite birth/permission proofs; SDK descriptions are synthetic."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_authority import GovernanceAuthority
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal, Intent
from cloud_memory_hub.governance_sources import (
    content_policy_target,
    content_targets,
    source_target,
)
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.write_admissions import (
    AdmissionJournal,
    WriteAdmissions,
)


@dataclass(frozen=True)
class SyntheticInput:
    operation: str
    request_id: str
    canonical_input: bytes


def prepare(event, *, operation="add"):
    def build(request_id):
        return SyntheticInput(
            operation,
            request_id,
            json.dumps(
                {"request_id": request_id, "event": event.model_dump(mode="json")},
                ensure_ascii=False,
                sort_keys=True,
            ).encode(),
        )

    return build


def rules(main, event):
    return (
        content_policy_target(main, event.scope_id),
        source_target(main, event),
        *content_targets(main, event),
    )


@pytest.fixture
def legacy(tmp_path):
    access = DeviceAccessStore(tmp_path / "config" / "remote-access.sqlite3")
    main = GovernanceJournal.initialize(
        tmp_path / "governance" / "journal.sqlite3",
        instance_id=access.instance_id,
        key=os.urandom(32),
    )
    GovernanceAuthority.activate(access, main)
    return access, main


@pytest.fixture
def admissions(legacy):
    return WriteAdmissions.activate(*legacy)


def event_for_store(store):
    return EventEnvelope(
        instance_id=store.access.instance_id,
        **{
            key: str(uuid4())
            for key in (
                "event_id",
                "device_id",
                "source_id",
                "scope_id",
                "conversation_id",
                "record_id",
            )
        },
        revision=1,
        kind="conversation_turn",
        content={
            "messages": [{"role": "user", "text": "Synthetic original private admission message"}]
        },
    )


def enroll(store, event, *, operation="add"):
    with store.access._connection(writing=True) as db:
        return store.enroll_in(
            db, event, prepare(event, operation=operation), rules(store.main, event)
        )


def proof(store, binding, **kwargs):
    with store.access._connection(writing=True) as db:
        return store.proof_in(db, binding.request_id, **kwargs)


def reopen(store):
    return WriteAdmissions(
        DeviceAccessStore(store.access.path, require_existing=True),
        GovernanceJournal(store.main.path, instance_id=store.main.instance_id, key=store.main._key),
    )


def snapshot(path):
    backup = sqlite3.connect(":memory:")
    with sqlite3.connect(path) as db:
        db.backup(backup)
    return backup


def restore(backup, path):
    with sqlite3.connect(path) as db:
        backup.backup(db)


def deny_nothing(_binding):
    return None


def test_upgrade_preserves_intents_targets_and_checkpoints_and_binds_empty_log(legacy):
    access, main = legacy
    target = main.target("global", "raw", "synthetic original")
    intent = Intent(
        operation_id=str(uuid4()),
        actor_id=access.instance_id,
        scope_id="global",
        action="forget",
        targets=(target,),
    )
    checkpoint = main.append(intent)
    with sqlite3.connect(main.path) as db:
        before = db.execute("SELECT * FROM intents").fetchall()
    store = WriteAdmissions.activate(access, main)
    assert main.schema_version == 2 and access.schema_version == 3
    assert main.admissions_ledger_id() == store.journal.ledger_id
    assert main.head("global") == checkpoint
    assert main.target("global", "raw", "synthetic original") == target
    with sqlite3.connect(main.path) as db:
        assert db.execute("SELECT * FROM intents").fetchall() == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        binding = db.execute("SELECT * FROM binding").fetchone()
        assert binding[-1] != main._sign(
            main._key, "binding", [access.instance_id, main.ledger_id, 1]
        )
    assert reopen(store).journal.ledger_id == store.journal.ledger_id
    assert WriteAdmissions.activate(access, main).journal.ledger_id == store.journal.ledger_id


@pytest.mark.parametrize("operation", ["add", "add_extracted"])
def test_enrollment_is_stable_private_and_permission_is_once(admissions, operation):
    store = admissions
    event = event_for_store(store)
    first = enroll(store, event, operation=operation)
    for path in store.main.path.parent.glob("*.sqlite3*"):
        for key in ("source_id", "conversation_id", "record_id"):
            assert getattr(event, key).encode() not in path.read_bytes()
    assert enroll(store, event, operation=operation) == first
    actual = prepare(event, operation=operation)(first.request_id)
    assert proof(store, first, event=event, actual=actual).state == "enrolled"
    checked = []
    store.permit_once(actual, require_allowed=lambda binding: checked.append(binding))
    assert checked == [first]
    assert proof(reopen(store), first).state == "possible"
    with pytest.raises(GovernanceError, match="^governance_admission_already_possible$"):
        store.permit_once(actual, require_allowed=deny_nothing)
    assert enroll(store, event, operation=operation) == first
    assert store.journal.head(first.birth_id).sequence == 2
    for path in store.main.path.parent.glob("*.sqlite3*"):
        assert b"Synthetic original" not in path.read_bytes()
        for key in ("source_id", "conversation_id", "record_id"):
            assert getattr(event, key).encode() not in path.read_bytes()
    assert "Synthetic original" not in repr(first)


@pytest.mark.parametrize("changed_field", ["content", "source_id"])
def test_event_and_actual_input_conflicts_never_create_new_identity(admissions, changed_field):
    store = admissions
    event = event_for_store(store)
    binding = enroll(store, event)
    changed = event.model_copy(
        update={
            "content": {"messages": [{"role": "user", "text": "Synthetic changed secret content"}]}
        }
    )
    if changed_field == "source_id":
        changed = event.model_copy(update={"source_id": str(uuid4())})
    with pytest.raises(GovernanceError, match="^governance_admission_event_conflict$"):
        enroll(store, changed)
    actual = prepare(event)(binding.request_id)
    for altered in (
        replace(actual, canonical_input=actual.canonical_input + b" "),
        replace(actual, operation="add_extracted"),
        replace(actual, request_id=binding.request_id.replace(binding.birth_id, str(uuid4()))),
    ):
        with pytest.raises(GovernanceError):
            store.permit_once(altered, require_allowed=deny_nothing)
    assert proof(store, binding).state == "enrolled"
    assert len(store.journal.scopes()) == 1


def test_rule_rejection_and_callback_failure_never_grant_permission(admissions):
    event = event_for_store(admissions)
    binding = enroll(admissions, event)

    def reject(_binding):
        raise GovernanceError("governance_content_suppressed")

    with pytest.raises(GovernanceError, match="^governance_content_suppressed$"):
        admissions.permit_once(prepare(event)(binding.request_id), require_allowed=reject)
    assert proof(admissions, binding).state == "enrolled"


def test_journal_leading_failed_enrollment_transaction_recovers_same_birth(admissions):
    event = event_for_store(admissions)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with admissions.access._connection(writing=True) as db:
            binding = admissions.enroll_in(db, event, prepare(event), rules(admissions.main, event))
            raise RuntimeError("synthetic rollback")
    with admissions.access._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM cmh_admission_witnesses").fetchone()[0] == 0
    store = reopen(admissions)
    assert enroll(store, event) == binding
    assert proof(store, binding).state == "enrolled"


def test_permission_journal_commit_followed_by_witness_failure_is_never_reissued(admissions):
    event = event_for_store(admissions)
    binding = enroll(admissions, event)
    with admissions.access._connection(writing=True) as db:
        db.execute(
            "CREATE TRIGGER reject_witness BEFORE UPDATE ON cmh_admission_witnesses "
            "BEGIN SELECT RAISE(ABORT,'synthetic witness failure'); END"
        )
    with pytest.raises(GovernanceError):
        admissions.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)
    assert admissions.journal.head(binding.birth_id).sequence == 2
    with admissions.access._connection(writing=True) as db:
        db.execute("DROP TRIGGER reject_witness")
    # A refused retry itself repairs the external floor; no restart is required.
    with pytest.raises(GovernanceError, match="^governance_admission_already_possible$"):
        admissions.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)
    with admissions.access._connection() as db:
        saved = db.execute("SELECT checkpoint FROM cmh_admission_witnesses").fetchone()[0]
        assert json.loads(saved)["sequence"] == 2
    store = reopen(admissions)
    assert proof(store, binding).state == "possible"
    with pytest.raises(GovernanceError, match="^governance_admission_already_possible$"):
        store.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)


def test_current_witness_rejects_admission_rollback(admissions):
    event = event_for_store(admissions)
    binding = enroll(admissions, event)
    old_log = snapshot(admissions.journal.path)
    try:
        admissions.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)
        restore(old_log, admissions.journal.path)
        with pytest.raises(GovernanceError, match="^governance_admission_watermark_ahead$"):
            reopen(admissions)
    finally:
        old_log.close()


def test_old_config_recovers_all_births_and_possible_flags_from_independent_log(admissions):
    old_config = snapshot(admissions.access.path)
    try:
        event = event_for_store(admissions)
        binding = enroll(admissions, event)
        admissions.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)
        restore(old_config, admissions.access.path)
        restored = reopen(admissions)
        assert enroll(restored, event) == binding
        assert proof(restored, binding).state == "possible"
    finally:
        old_config.close()


@pytest.mark.parametrize("missing", [True, False])
def test_main_volume_binding_prevents_empty_replacement_after_old_config_restore(legacy, missing):
    access, main = legacy
    old_config = snapshot(access.path)
    try:
        store = WriteAdmissions.activate(access, main)
        original_ledger = store.journal.ledger_id
        store.journal.path.rename(store.journal.path.with_name("saved-admissions.sqlite3"))
        if not missing:
            AdmissionJournal.initialize(
                store.journal.path, instance_id=access.instance_id, key=store.journal._key
            )
        restore(old_config, access.path)
        restored_access = DeviceAccessStore(access.path, require_existing=True)
        with pytest.raises(GovernanceError):
            WriteAdmissions.activate(restored_access, main)
        assert main.admissions_ledger_id() == original_ledger
        if missing:
            assert not store.journal.path.exists()
    finally:
        old_config.close()


def test_deleted_main_capability_cannot_reinitialize_a_missing_log_after_config_restore(legacy):
    access, main = legacy
    old_config = snapshot(access.path)
    try:
        store = WriteAdmissions.activate(access, main)
        store.journal.path.rename(store.journal.path.with_name("saved-admissions.sqlite3"))
        with sqlite3.connect(main.path) as db:
            db.execute("DELETE FROM capabilities")
        restore(old_config, access.path)
        restored_access = DeviceAccessStore(access.path, require_existing=True)
        with pytest.raises(GovernanceError, match="^governance_capability_invalid$"):
            WriteAdmissions.activate(restored_access, main)
        assert not store.journal.path.exists()
        with pytest.raises(GovernanceError, match="^governance_capability_invalid$"):
            GovernanceJournal(main.path, instance_id=main.instance_id, key=main._key)
    finally:
        old_config.close()


def test_concurrent_same_event_and_permission_has_one_birth_and_one_permit(admissions):
    event = event_for_store(admissions)
    with ThreadPoolExecutor(max_workers=4) as pool:
        bindings = list(pool.map(lambda _: enroll(admissions, event), range(8)))
    assert len({binding.birth_id for binding in bindings}) == 1
    binding = bindings[0]

    def permit(_):
        try:
            admissions.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)
            return "allowed"
        except GovernanceError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(permit, range(8)))
    assert results.count("allowed") == 1
    assert results.count("governance_admission_already_possible") == 7


def test_invalid_transitions_are_rejected_before_log_changes(admissions):
    event = event_for_store(admissions)
    binding = enroll(admissions, event)
    changed = binding.model_copy(update={"input_mac": "a" * 64})
    with pytest.raises(GovernanceError):
        admissions.journal.append(admissions._entry(changed, "possible"))
    admissions.journal.append(admissions._entry(binding, "possible"))
    with pytest.raises(GovernanceError, match="^governance_admission_already_possible$"):
        admissions.journal.append(admissions._entry(binding, "possible"))
    assert admissions.journal.head(binding.birth_id).sequence == 2
    other = binding.model_copy(update={"birth_id": str(uuid4()), "event_id": str(uuid4())})
    other = other.model_copy(
        update={"event_key": admissions.journal.event_key(other.device_id, other.event_id)}
    )
    with pytest.raises(GovernanceError, match="^governance_admission_sequence_invalid$"):
        admissions.journal.append(admissions._entry(other, "possible"))
    assert admissions.journal.head(other.birth_id).sequence == 0


def test_proof_checks_only_the_named_two_entry_stream(admissions, monkeypatch):
    first = enroll(admissions, event_for_store(admissions))
    for _ in range(3):
        enroll(admissions, event_for_store(admissions))

    def no_scan(*_args):
        pytest.fail("a point proof must not scan all streams")

    monkeypatch.setattr(admissions.journal, "_verify", no_scan)
    monkeypatch.setattr(admissions.journal, "scopes", no_scan)
    assert proof(admissions, first).state == "enrolled"


@pytest.mark.parametrize(
    "damage", ["missing_birth", "third_entry", "witness_signature", "capability_signature"]
)
def test_damaged_proof_never_becomes_an_empty_log_or_a_negative_result(admissions, damage):
    event = event_for_store(admissions)
    binding = enroll(admissions, event)
    admissions.permit_once(prepare(event)(binding.request_id), require_allowed=deny_nothing)
    if damage == "witness_signature":
        with admissions.access._connection(writing=True) as db:
            db.execute("UPDATE cmh_admission_witnesses SET signature='synthetic-private-damage'")
    elif damage == "capability_signature":
        with sqlite3.connect(admissions.main.path) as db:
            db.execute("UPDATE capabilities SET signature='synthetic-private-damage'")
    else:
        with sqlite3.connect(admissions.journal.path) as db:
            if damage == "missing_birth":
                db.execute("DELETE FROM intents WHERE sequence=1")
            else:
                # A correctly MACed but illegal third entry simulates a broken
                # older writer. Generic hash-chain integrity alone is insufficient.
                entry = admissions._entry(binding, "possible").model_copy(
                    update={"operation_id": str(uuid4())}
                )
                payload = json.dumps(
                    entry.model_dump(mode="json"),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                prior = db.execute(
                    "SELECT digest FROM heads WHERE scope_id=?", (binding.birth_id,)
                ).fetchone()[0]
                digest = admissions.journal._mac("intent", [3, prior, payload])
                db.execute(
                    "INSERT INTO intents VALUES (?,?,?,?,?,?)",
                    (binding.birth_id, 3, entry.operation_id, payload, prior, digest),
                )
                db.execute(
                    "UPDATE heads SET sequence=3,digest=? WHERE scope_id=?",
                    (digest, binding.birth_id),
                )
    with pytest.raises(GovernanceError) as caught:
        reopen(admissions)
    assert "synthetic-private-damage" not in str(caught.value)
    assert admissions.journal.path.exists()


@pytest.mark.parametrize("stage", ["before_main_upgrade", "before_config_commit"])
def test_activation_resumes_same_independent_log_after_interruption(legacy, monkeypatch, stage):
    from contextlib import contextmanager

    access, main = legacy
    with monkeypatch.context() as patch:
        if stage == "before_main_upgrade":

            def stop(_ledger):
                raise GovernanceError("synthetic_upgrade_interrupted")

            patch.setattr(main, "upgrade_v2", stop)
        else:

            @contextmanager
            def stop(**_kwargs):
                raise GovernanceError("synthetic_upgrade_interrupted")
                yield  # pragma: no cover - a failed context manager never yields

            patch.setattr(access, "_connection", stop)
        with pytest.raises(GovernanceError, match="synthetic_upgrade_interrupted"):
            WriteAdmissions.activate(access, main)
    path = main.path.with_name("admissions.sqlite3")
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT ledger_id FROM binding").fetchone()[0]
    assert access.schema_version == 2
    completed = WriteAdmissions.activate(access, main)
    assert completed.journal.ledger_id == original


def test_input_budget_and_invalid_event_fail_before_any_enrollment(admissions, monkeypatch):
    import cloud_memory_hub.write_admissions as module

    event = event_for_store(admissions)
    monkeypatch.setattr(module, "MAX_INPUT_BYTES", 32)
    with pytest.raises(GovernanceError, match="^governance_admission_scan_limit$"):
        enroll(admissions, event)
    monkeypatch.setattr(module, "MAX_INPUT_BYTES", 32 * 1024 * 1024)
    with pytest.raises(GovernanceError, match="^governance_admission_invalid$"):
        enroll(admissions, event.model_copy(update={"revision": 0}))
    with admissions.access._connection(writing=True) as db:
        with pytest.raises(GovernanceError, match="^governance_admission_rules_invalid$"):
            admissions.enroll_in(db, event, prepare(event), ())
    assert admissions.journal.scopes() == ()
