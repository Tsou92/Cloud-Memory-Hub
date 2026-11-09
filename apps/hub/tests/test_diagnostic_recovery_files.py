"""ADR-0045: real native files and signed families share one recovery pass.

Legacy lines come from the existing native emitter in an isolated subprocess.
Copying exact retained bytes into registered stage names models a restored old
artifact; it never supplies a new grant or invents the body being removed.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_diagnostic_files as engine
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority, RepairParent
from cloud_memory_hub.governance_diagnostic_inventory import (
    InventoryBudget,
    inspect_diagnostic_files,
)
from cloud_memory_hub.governance_diagnostic_records import RecordInspector, WriteRequest
from cloud_memory_hub.governance_execution import ExecutionStore as LegacyExecutionStore
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_inflight import CleanupSelector
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal, Intent
from hy_memory.utils import file_log_lifecycle as native
from hy_memory.utils.diagnostic_policy import StrictDiagnosticFormatter
from test_diagnostic_confirmation import intent_for
from test_diagnostic_file_execution import reopen, retained, stages
from test_diagnostic_inventory import PROBE, contents, pipeline


@pytest.fixture(scope="module")
def native_recovery_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-recovery-families").resolve()
    instance, first_scope, second_scope = str(uuid4()), str(uuid4()), str(uuid4())
    bindings = [
        {
            "request_id": "cmh2:" + ":".join((instance, *(str(uuid4()) for _ in range(3)))),
            "user_id": f"cmh:{instance}:{scope}",
        }
        for scope in (first_scope, first_scope, second_scope)
    ]
    (root / "bindings.json").write_text(json.dumps(bindings))
    result = subprocess.run(
        [sys.executable, str(PROBE), "--phase", "emit-shared", "--root", str(root)],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    return root / "files", tuple(WriteRequest(**item) for item in bindings)


@pytest.fixture
def recovery_case(native_recovery_files, tmp_path):
    source, requests = native_recovery_files
    root = tmp_path.resolve() / "files"
    shutil.copytree(source, root)
    return root, requests


def prepare(case, *, repairs=(0, 1), commit_repairs=True):
    root, requests = case
    options = {"data_root": root / "data", "sdk_log_root": pipeline(root)}
    proposal = inspect_diagnostic_files(**options, requests=requests, mode="propose")
    assert len(proposal.files) == 2 and len(proposal.grants) == 9
    instance, scope = requests[0].user_id.split(":")[1:]
    journal = GovernanceJournal.initialize(
        root.parent / "authority/journal.sqlite3", instance_id=instance, key=os.urandom(32)
    )
    journal.upgrade_v2(str(uuid4()))
    selectors = tuple(
        CleanupSelector(str(uuid4()), request.request_id, "a" * 64, "terminal_cleanup")
        for request in requests
    )
    legacy = CleanupAuthority("legacy", selectors[:2], ())
    parent, records = intent_for(journal, legacy, scope=scope)
    old_store = LegacyExecutionStore(journal)
    old_store.plan(parent, records)
    checkpoint = journal.append(parent)
    old_store.complete(parent)
    store = ExecutionStore(journal)
    binding = store.migrate_legacy(store.inspect_legacy(), {parent.operation_id: legacy})
    journal.bind_execution_store(binding.store_id)
    other_authority = CleanupAuthority(
        "records",
        selectors[2:],
        tuple(item for item in proposal.grants if item.request_id == requests[2].request_id),
    )
    other, records = intent_for(journal, other_authority, scope=requests[2].user_id.split(":")[2])
    store.plan(other, records, cleanup=other_authority)
    journal.append(other)
    family = SimpleNamespace(
        store=store,
        parent=parent,
        other=other,
        parent_digest=checkpoint.digest,
        selectors=selectors,
        requests=requests,
        grants=proposal.grants,
        options=options,
        repairs=[],
    )
    for index in repairs:
        add_repair(family, index, commit=commit_repairs)
    return family


def add_repair(family, index, *, commit=True):
    journal = family.store.journal
    authority = CleanupAuthority(
        "repair",
        family.selectors[:2],
        tuple(
            item for item in family.grants if item.request_id == family.requests[index].request_id
        ),
        RepairParent(family.parent.operation_id, family.parent_digest, journal.ledger_id),
    )
    intent, records = intent_for(journal, authority, scope=family.parent.scope_id)
    assert records == {}
    family.store.plan(intent, records, cleanup=authority)
    if commit:
        journal.append(intent)
    family.repairs.append(intent)
    return intent


def recover(family, *, store=None, **kwargs):
    options = family.options
    with native.diagnostic_file_maintenance(roots=(options["sdk_log_root"],)) as gate:
        return engine.recover_diagnostic_files(
            store or family.store, **options, gate=gate, **kwargs
        )


def journal_heads(journal):
    return tuple((scope, journal.head(scope)) for scope in sorted(journal.scopes()))


def immutable_state(store):
    snapshot = store.recovery_snapshot()
    return {
        identity: (item.state, item.intent, dict(item.records), item.cleanup.authority)
        for identity, item in snapshot.snapshot.operations.items()
    }


def without_request(case, request_id):
    root, requests = case
    inspector = RecordInspector(requests, mode="propose")
    for relative, raw in contents(root).items():
        path = root / relative
        kind = "pipeline" if path.parent.name == "default" else "sdk"
        kept = []
        for line in raw.split(b"\n")[:-1]:
            line += b"\n"
            decision = inspector.inspect(line, kind=kind)
            assert decision.action in {"keep", "candidate"}
            if decision.grant is None or decision.grant.request_id != request_id:
                kept.append(line)
        path.write_bytes(b"".join(kept))


def restore(root, snapshot):
    for relative, raw in snapshot.items():
        (root / relative).write_bytes(raw)


def restored_stages(family, receipts, *, saved=None):
    """Recreate only signed names, using actual verified retained file bytes."""
    result = []
    for receipt in receipts:
        for entry in receipt.manifest.artifacts:
            formal = family.options["sdk_log_root"] / entry.before.key
            raw = (
                formal.read_bytes()
                if saved is None
                else saved[(receipt.operation_id, entry.before.domain, entry.before.key)]
            )
            assert (
                len(raw) == entry.retained_bytes
                and sha256(raw).hexdigest() == entry.retained_sha256
            )
            path = formal.with_name(entry.stage_name)
            path.write_bytes(raw)
            path.chmod(0o600)
            result.append((path, formal))
    return result


def two_owner_stages(case, family, *, restore_artifacts=True, conflicting_prefix=False):
    """Make genuine v2 and v1 passes, then restore their registered stage copies."""
    root, requests = case
    original = contents(root)
    first = recover(family)
    owner = next(item.operation_id for item in first if item.manifest.artifacts)
    saved = {
        (item.operation_id, entry.before.domain, entry.before.key): (
            family.options["sdk_log_root"] / entry.before.key
        ).read_bytes()
        for item in first
        for entry in item.manifest.artifacts
    }
    selected = next(item for item in family.repairs if item.operation_id != owner)
    selected_request = family.store.cleanup(selected.operation_id).authority.grants[0].request_id
    restore(root, original)
    for request in requests:
        if request.request_id != selected_request:
            without_request(case, request.request_id)
    if conflicting_prefix:
        # The extra line is emitted by a real native handler, outside the scanned
        # roots; prepend those exact bytes before creating the second real pass.
        output = root.parent / "native-strict-prefix.log"
        handler = native._create_sdk_file_handler(
            filename=output, when="midnight", backupCount=30, encoding="utf-8"
        )
        handler.setFormatter(StrictDiagnosticFormatter())
        try:
            handler.handle(
                logging.LogRecord("hy_memory", logging.INFO, __file__, 1, "omitted", (), None)
            )
            handler.flush()
        finally:
            handler.close()
        prefix = output.read_bytes()
        assert RecordInspector((), mode="authorized").inspect(prefix, kind="sdk").action == "keep"
        formal = family.options["sdk_log_root"] / "hy_memory.log"
        formal.write_bytes(prefix + formal.read_bytes())
    with native.diagnostic_file_maintenance(roots=(family.options["sdk_log_root"],)) as gate:
        second = engine.execute_diagnostic_files(
            family.store, selected.operation_id, **family.options, gate=gate
        )
    assert second.manifest.contributors is None and len(second.manifest.artifacts) == 2
    saved.update(
        {
            (second.operation_id, entry.before.domain, entry.before.key): (
                family.options["sdk_log_root"] / entry.before.key
            ).read_bytes()
            for entry in second.manifest.artifacts
        }
    )
    receipts = tuple(
        item.cleanup for item in family.store.snapshot().operations.values() if item.cleanup
    )
    assert sum(bool(item.manifest.artifacts) for item in receipts) == 2
    if not restore_artifacts:
        return receipts
    artifacts = restored_stages(family, receipts, saved=saved)
    assert len(artifacts) == 4
    return artifacts


def test_multiple_scopes_and_two_repairs_share_one_native_pass_without_completing(
    recovery_case, monkeypatch
):
    root, _ = recovery_case
    family = prepare(recovery_case)
    before, head = immutable_state(family.store), journal_heads(family.store.journal)
    expected = retained(recovery_case, family.options)
    path = pipeline(root) / "hy_memory.log"
    handler = native._create_sdk_file_handler(
        filename=path, when="midnight", backupCount=30, encoding="utf-8"
    )
    handler.setFormatter(StrictDiagnosticFormatter())
    old_stream, replaced = handler.stream, []
    original = engine.os.replace

    def track(source, destination, *args, **kwargs):
        replaced.append(destination)
        return original(source, destination, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(engine.os, "replace", track)
            receipts = recover(family)
        assert len(receipts) == 4 and all(item.state == "verified" for item in receipts)
        assert {item.operation_id for item in receipts} == set(before)
        assert len(replaced) == len(set(replaced)) == 2
        assert contents(root) == expected and not stages(root)
        assert immutable_state(family.store) == before
        assert journal_heads(family.store.journal) == head
        assert family.store.cleanup(family.parent.operation_id).authority.grants == ()
        assert (
            family.store.has_pending()
        )  # Repairs and the other business parent are still planned.
        assert old_stream.closed and handler.stream is not old_stream
        assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino
        assert sum(bool(item.manifest.artifacts) for item in receipts) == 1
        assert all(len(item.manifest.contributors) == 4 for item in receipts)
    finally:
        handler.close()


def test_uncommitted_repair_never_contributes_or_mutates_files(recovery_case):
    root, _ = recovery_case
    family = prepare(recovery_case, repairs=(0,))
    orphan = add_repair(family, 1, commit=False)
    before = contents(root), family.store.snapshot(), journal_heads(family.store.journal)
    assert orphan.operation_id in family.store.recovery_snapshot().orphans
    with pytest.raises(GovernanceError):
        recover(family)
    assert (contents(root), family.store.snapshot(), journal_heads(family.store.journal)) == before


@pytest.mark.parametrize("kind", ["empty", "forget", "orphan", "unplanned"])
def test_empty_or_unplanned_graph_never_enters_file_scan(tmp_path, monkeypatch, kind):
    journal = GovernanceJournal.initialize(
        tmp_path / "authority/journal.sqlite3", instance_id=str(uuid4()), key=os.urandom(32)
    )
    journal.upgrade_v2(str(uuid4()))
    store = ExecutionStore(journal)
    binding = store.initialize_empty()
    journal.bind_execution_store(binding.store_id)
    if kind == "forget":
        scope = str(uuid4())
        intent = Intent(
            operation_id=str(uuid4()),
            actor_id=journal.instance_id,
            scope_id=scope,
            action="forget",
            targets=(journal.target(scope, "memory", "synthetic-memory"),),
        )
        store.plan(intent, {"synthetic-memory": "memory"})
        journal.append(intent)
    elif kind in {"orphan", "unplanned"}:
        authority = CleanupAuthority("records", (), ())
        intent, records = intent_for(journal, authority)
        if kind == "orphan":
            store.plan(intent, records, cleanup=authority)
        else:
            journal.append(intent)

    def forbidden(*args, **kwargs):
        raise AssertionError("empty/unplanned graph entered physical inventory")

    monkeypatch.setattr(engine, "_inspection", forbidden)
    options = {"data_root": tmp_path / "data", "sdk_log_root": tmp_path / "sdk"}
    family = SimpleNamespace(store=store, options=options)
    before = store.snapshot(), journal_heads(journal)
    if kind == "unplanned":
        with pytest.raises(GovernanceError):
            recover(family)
    else:
        assert recover(family) == ()
    assert (store.snapshot(), journal_heads(journal)) == before
    assert not options["data_root"].exists() and not options["sdk_log_root"].exists()


def test_old_stage_and_new_repair_use_fresh_union_without_changing_old_refs(
    recovery_case, monkeypatch
):
    root, requests = recovery_case
    family = prepare(recovery_case, repairs=(0,))
    original = contents(root)
    expected = retained(recovery_case, family.options)
    without_request(recovery_case, requests[1].request_id)
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("synthetic replacement interruption")

        patch.setattr(engine.os, "replace", fail)
        with pytest.raises(GovernanceError, match="diagnostic_file_io_failed"):
            recover(family)
    old = next(
        item.cleanup
        for item in family.store.snapshot().operations.values()
        if item.cleanup.manifest is not None and item.cleanup.manifest.artifacts
    )
    assert stages(root) and len(old.manifest.contributors) == 3
    restore(root, original)
    new_repair = add_repair(family, 1)
    old_refs = old.manifest.contributors
    observed = []
    original_checkpoint = ExecutionStore.checkpoint

    def check_refs(store, expected, *, manifest):
        if manifest.pass_id == old.pass_id:
            observed.append(manifest.contributors)
        return original_checkpoint(store, expected, manifest=manifest)

    with monkeypatch.context() as patch:
        patch.setattr(ExecutionStore, "checkpoint", check_refs)
        receipts = recover(family, store=reopen(family.store))
    assert observed and all(item == old_refs for item in observed)
    assert new_repair.operation_id in {item.operation_id for item in receipts}
    assert all(item.state == "verified" for item in receipts)
    assert contents(root) == expected and not stages(root)


@pytest.mark.parametrize("damage", ["unknown", "tampered", "only_copy", "second_stage"])
def test_every_old_artifact_is_proved_before_any_retirement(recovery_case, damage):
    root, _ = recovery_case
    family = prepare(recovery_case)
    receipts = recover(family)
    artifacts = restored_stages(family, receipts)
    assert len(artifacts) == 2
    artifacts.sort(key=lambda item: str(item[0]))
    path, formal = artifacts[-1]
    if damage == "unknown":
        path = formal.with_name(".cmh-cleanup-unregistered.tmp")
        path.write_bytes(b"synthetic unknown artifact")
        path.chmod(0o600)
    elif damage in {"tampered", "second_stage"}:
        if damage == "tampered":
            path = artifacts[0][0]
        raw = bytearray(path.read_bytes())
        raw[0] ^= 1
        path.write_bytes(raw)
    else:
        formal.unlink()
    before = contents(root), family.store.snapshot(), journal_heads(family.store.journal)
    with pytest.raises(GovernanceError):
        recover(family, store=reopen(family.store))
    assert (contents(root), family.store.snapshot(), journal_heads(family.store.journal)) == before
    assert all(item.exists() for item, _ in artifacts)


@pytest.mark.parametrize("damage", [False, True])
def test_multiple_old_owners_are_proved_together_including_v1_passes(recovery_case, damage):
    root, _ = recovery_case
    family = prepare(recovery_case)
    artifacts = two_owner_stages(recovery_case, family)
    stage_paths = {str(path.relative_to(root)) for path, _ in artifacts}
    expected = {key: value for key, value in contents(root).items() if key not in stage_paths}
    if damage:
        path = sorted(item for item, _ in artifacts)[-1]
        raw = bytearray(path.read_bytes())
        raw[-2] ^= 1
        path.write_bytes(raw)
    before = contents(root), family.store.snapshot(), journal_heads(family.store.journal)
    if damage:
        with pytest.raises(GovernanceError, match="diagnostic_file_artifact_unproven"):
            recover(family, store=reopen(family.store))
        assert (
            contents(root),
            family.store.snapshot(),
            journal_heads(family.store.journal),
        ) == before
        assert all(item.exists() for item, _ in artifacts)
    else:
        receipts = recover(family, store=reopen(family.store))
        assert all(item.state == "verified" for item in receipts)
        assert contents(root) == expected and not stages(root)


def test_individually_valid_old_passes_with_conflicting_prefixes_preserve_every_artifact(
    recovery_case, monkeypatch
):
    root, _ = recovery_case
    family = prepare(recovery_case)
    artifacts = two_owner_stages(recovery_case, family, conflicting_prefix=True)
    sdk = sorted(
        (path.read_bytes() for path, formal in artifacts if formal.name == "hy_memory.log"),
        key=len,
    )
    assert len(sdk) == 2 and not sdk[1].startswith(sdk[0])
    assert (family.options["sdk_log_root"] / "hy_memory.log").read_bytes() == sdk[1]
    snapshot = family.store.recovery_snapshot()  # Both persisted manifests verify their real MACs.
    for operation in snapshot.snapshot.operations.values():
        for entry in operation.cleanup.manifest.artifacts:
            formal = family.options["sdk_log_root"] / entry.before.key
            raw = formal.with_name(entry.stage_name).read_bytes()
            assert len(raw) == entry.retained_bytes
            assert sha256(raw).hexdigest() == entry.retained_sha256
    before = contents(root), family.store.snapshot(), journal_heads(family.store.journal)

    def forbidden(*args, **kwargs):
        raise AssertionError("one owner retired a stage before all retained prefixes were proved")

    with monkeypatch.context() as patch:
        patch.setattr(engine.os, "unlink", forbidden)
        with pytest.raises(GovernanceError, match="^diagnostic_file_retained_unproven$"):
            recover(family, store=reopen(family.store))
    assert (contents(root), family.store.snapshot(), journal_heads(family.store.journal)) == before
    assert all(path.exists() for path, _ in artifacts)


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_actual_stage_budget_is_shared_across_old_pass_owners(recovery_case, limit):
    root, _ = recovery_case
    family = prepare(recovery_case)
    artifacts = two_owner_stages(recovery_case, family)
    stage_bytes = sum(path.stat().st_size for path, _ in artifacts)
    formal_bytes = sum(path.stat().st_size for path in {formal for _, formal in artifacts})
    assert stage_bytes == 2 * formal_bytes and formal_bytes < stage_bytes - 1
    budget = (
        InventoryBudget(max_files=3)
        if limit == "files"
        else InventoryBudget(max_total_bytes=stage_bytes - 1)
    )
    before = contents(root), family.store.snapshot()
    with pytest.raises(GovernanceError, match="budget_exceeded"):
        recover(family, budget=budget)
    assert (contents(root), family.store.snapshot()) == before
    assert all(item.exists() for item, _ in artifacts)


def test_absent_historical_stage_names_do_not_consume_actual_file_budget(recovery_case):
    root, _ = recovery_case
    family = prepare(recovery_case)
    old = two_owner_stages(recovery_case, family, restore_artifacts=False)
    assert sum(len(item.manifest.artifacts) for item in old) == 4 and not stages(root)
    before = contents(root)
    assert len(before) == 2
    receipts = recover(family, budget=InventoryBudget(max_files=2))
    assert all(item.state == "verified" for item in receipts)
    assert contents(root) == before and not stages(root)


@pytest.mark.parametrize(
    "budget",
    [
        InventoryBudget(max_files=1),
        InventoryBudget(max_total_bytes=1),
        InventoryBudget(max_records=1),
    ],
)
def test_global_inventory_budget_is_not_multiplied_by_contributors(recovery_case, budget):
    root, _ = recovery_case
    family = prepare(recovery_case)
    before = contents(root), family.store.snapshot()
    with pytest.raises(GovernanceError, match="budget_exceeded"):
        recover(family, budget=budget)
    assert (contents(root), family.store.snapshot()) == before and not stages(root)


def test_repeated_restore_native_rollover_and_new_strict_output_reverify_every_receipt(
    recovery_case,
):
    root, _ = recovery_case
    family = prepare(recovery_case)
    original = contents(root)
    first = recover(family)
    expected = contents(root)
    head, immutable = journal_heads(family.store.journal), immutable_state(family.store)
    restore(root, original)
    second = recover(family, store=reopen(family.store))
    assert contents(root) == expected
    assert all(a.pass_id != b.pass_id for a, b in zip(first, second, strict=True))
    path = pipeline(root) / "hy_memory.log"
    handler = native._create_sdk_file_handler(
        filename=path, when="midnight", backupCount=30, encoding="utf-8"
    )
    handler.setFormatter(StrictDiagnosticFormatter())
    try:
        handler.doRollover()
        handler.handle(
            logging.LogRecord("hy_memory", logging.INFO, __file__, 1, "synthetic omitted", (), None)
        )
        handler.flush()
        before = contents(root)
        assert len(before) == 3
        third = recover(family, store=reopen(family.store))
        assert all(item.state == "verified" and not item.manifest.artifacts for item in third)
        assert contents(root) == before and not stages(root)
        assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino
    finally:
        handler.close()
    assert (
        journal_heads(family.store.journal) == head and immutable_state(family.store) == immutable
    )


def test_new_body_with_same_request_requires_new_exact_grant(recovery_case):
    root, _ = recovery_case
    family = prepare(recovery_case)
    original = contents(root)
    recover(family)
    restore(root, original)
    path = pipeline(root) / "hy_memory.log"
    raw = path.read_bytes()
    changed = raw.replace(
        b"Synthetic diagnostic inventory original", b"Different synthetic body", 1
    )
    assert changed != raw
    path.write_bytes(changed)
    before = contents(root), family.store.snapshot()
    with pytest.raises(GovernanceError):
        recover(family)
    assert (contents(root), family.store.snapshot()) == before and not stages(root)


def test_interrupted_per_operation_verification_reopens_and_finishes_remaining_receipts(
    recovery_case, monkeypatch
):
    root, _ = recovery_case
    family = prepare(recovery_case)
    expected = retained(recovery_case, family.options)
    head, immutable = journal_heads(family.store.journal), immutable_state(family.store)
    original = family.store.verify_pass
    calls = 0

    def fail(expected, *, verification):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GovernanceError("synthetic_receipt_interruption")
        return original(expected, verification=verification)

    with monkeypatch.context() as patch:
        patch.setattr(family.store, "verify_pass", fail)
        with pytest.raises(GovernanceError, match="synthetic_receipt_interruption"):
            recover(family)
    states = [item.cleanup.state for item in family.store.snapshot().operations.values()]
    assert states.count("verified") == 1 and "pending" in states
    assert contents(root) == expected
    receipts = recover(family, store=reopen(family.store))
    assert len(receipts) == 4 and all(item.state == "verified" for item in receipts)
    assert contents(root) == expected and not stages(root)
    assert (
        journal_heads(family.store.journal) == head and immutable_state(family.store) == immutable
    )
