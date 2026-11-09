"""ADR-0043: real native records, signed progress and bounded physical replacement."""

import errno
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_diagnostic_files as engine
from cloud_memory_hub import governance_diagnostic_inventory as inventory_module
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
from cloud_memory_hub.governance_diagnostic_inventory import (
    InventoryBudget,
    inspect_diagnostic_files,
)
from cloud_memory_hub.governance_diagnostic_records import RecordInspector
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_inflight import CleanupSelector
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal
from hy_memory.utils import file_log_lifecycle as native
from hy_memory.utils.diagnostic_policy import StrictDiagnosticFormatter
from test_diagnostic_confirmation import intent_for
from test_diagnostic_inventory import case as case_fixture
from test_diagnostic_inventory import (
    contents,
    pipeline,
)
from test_diagnostic_inventory import native_files as native_files_fixture
from test_diagnostic_inventory import native_shared_files as native_shared_files_fixture
from test_diagnostic_inventory import shared_case as shared_case_fixture

case = case_fixture
native_files = native_files_fixture
native_shared_files = native_shared_files_fixture
shared_case = shared_case_fixture


def prepare(case, *, shared=False, grants=None, commit=True, capability=True):
    root, requests = case
    options = dict(
        data_root=root / "data",
        sdk_log_root=pipeline(root) if shared else root / "sdk",
        requests=requests,
    )
    proposal = inspect_diagnostic_files(**options, mode="propose")
    instance, scope = requests[0].user_id.split(":")[1:]
    journal = GovernanceJournal.initialize(
        root.parent / "authority/journal.sqlite3", instance_id=instance, key=os.urandom(32)
    )
    journal.upgrade_v2(str(uuid4()))
    store = ExecutionStore(journal)
    binding = store.initialize_empty()
    journal.bind_execution_store(binding.store_id)
    selectors = tuple(
        CleanupSelector(str(uuid4()), request.request_id, "a" * 64, "terminal_cleanup")
        for request in requests
    )
    authority = CleanupAuthority(
        "records", selectors, proposal.grants if grants is None else grants
    )
    intent, records = intent_for(journal, authority, scope=scope)
    store.plan(intent, records, cleanup=authority)
    if commit:
        journal.append(intent)
    if not capability:
        with sqlite3.connect(journal.path) as db:
            db.execute("DELETE FROM capabilities WHERE name='execution_store_v2'")
    return store, intent, options


def execute(store, intent, options, **kwargs):
    roots = (options["data_root"] / "hy-memory/governed-v2/logs", options["sdk_log_root"])
    with native.diagnostic_file_maintenance(roots=roots) as gate:
        return engine.execute_diagnostic_files(
            store,
            intent.operation_id,
            data_root=options["data_root"],
            sdk_log_root=options["sdk_log_root"],
            gate=gate,
            **kwargs,
        )


def retained(case, options):
    root, requests = case
    inspector = RecordInspector(requests, mode="propose")
    result = {}
    for relative, raw in contents(root).items():
        path = root / relative
        kind = "pipeline" if path.parent.name == "default" else "sdk"
        # Native formats use LF framing; Unicode line separators remain content.
        lines = raw.split(b"\n")
        assert not lines[-1]
        result[relative] = b"".join(
            line + b"\n"
            for line in lines[:-1]
            if inspector.inspect(line + b"\n", kind=kind).action == "keep"
        )
    return result


def stages(root):
    return tuple(sorted(root.rglob(".cmh-cleanup-*.tmp")))


def reopen(store):
    journal = GovernanceJournal(
        store.journal.path, instance_id=store.journal.instance_id, key=store.journal._key
    )
    return ExecutionStore(journal)


def test_real_three_formats_remove_exact_bytes_and_keep_strict_without_completing_purge(case):
    root, _ = case
    source = root / "sdk/hy_memory.log"
    shutil.copyfile(source, source.with_name("hy_memory.log.2024-02-29"))
    store, intent, options = prepare(case)
    expected = retained(case, options)
    result = execute(store, intent, options)
    assert result.state == "verified" and result.revision > 0
    assert contents(root) == expected and not stages(root)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.rglob("*.log*"))
    assert store.snapshot().operations[intent.operation_id].state == "planned"
    assert sum(len(value) for value in expected.values()) > 0
    for entry in result.manifest.artifacts:
        assert entry.state == "verified"
        assert entry.retained_bytes < entry.before.byte_length


def test_shared_native_root_is_rewritten_once_and_preserves_all_kept_bytes(shared_case):
    root, _ = shared_case
    store, intent, options = prepare(shared_case, shared=True)
    expected = retained(shared_case, options)
    result = execute(store, intent, options)
    assert result.state == "verified" and len(result.manifest.artifacts) == 2
    assert contents(root) == expected


@pytest.mark.parametrize(
    "damage", ["grant", "unknown_record", "unknown_stage", "no_commit", "capability"]
)
def test_all_domains_are_preflighted_before_any_stage_or_replacement(case, damage):
    root, _ = case
    store, intent, options = prepare(
        case,
        grants=() if damage == "grant" else None,
        commit=damage != "no_commit",
        capability=damage != "capability",
    )
    if damage == "unknown_record":
        with (root / "sdk/hy_memory.log").open("ab") as stream:
            stream.write(b"unattributed synthetic record\n")
    elif damage == "unknown_stage":
        (root / "sdk/.cmh-cleanup-unknown.tmp").write_bytes(b"unattributed synthetic stage")
    before = contents(root), store.cleanup(intent.operation_id)
    with pytest.raises(GovernanceError):
        execute(store, intent, options)
    assert (contents(root), store.cleanup(intent.operation_id)) == before


@pytest.mark.parametrize(
    "failure",
    [
        "stage_create",
        "partial_stage",
        "stage_fsync",
        "stage_checkpoint",
        "first_replace",
        "second_replace",
        "after_replace",
        "directory_fsync",
        "verify",
    ],
)
def test_real_failures_remain_pending_and_restart_reproves_before_retry(case, monkeypatch, failure):
    root, _ = case
    store, intent, options = prepare(case)
    expected = retained(case, options)
    with monkeypatch.context() as patch:
        if failure == "stage_create":
            original = os.open

            def fail(path, *args, **kwargs):
                if isinstance(path, str) and path.startswith(".cmh-cleanup-"):
                    raise OSError("synthetic stage creation failure")
                return original(path, *args, **kwargs)

            patch.setattr(engine.os, "open", fail)
        elif failure == "partial_stage":
            original = os.write
            calls = 0

            def fail(descriptor, data):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original(descriptor, data[:7])
                raise OSError("synthetic stage failure")

            patch.setattr(engine.os, "write", fail)
        elif failure == "stage_fsync":
            original = os.fsync

            def fail(descriptor):
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("synthetic fsync failure")
                return original(descriptor)

            patch.setattr(engine.os, "fsync", fail)
        elif failure == "stage_checkpoint":
            original = store.checkpoint

            def fail(expected, *, manifest):
                if any(item.state == "staged" for item in manifest.artifacts):
                    raise GovernanceError("synthetic_checkpoint_failure")
                return original(expected, manifest=manifest)

            patch.setattr(store, "checkpoint", fail)
        elif failure in {"first_replace", "second_replace", "after_replace"}:
            original = os.replace
            calls = 0

            def fail(*args, **kwargs):
                nonlocal calls
                calls += 1
                if failure == "second_replace" and calls == 1:
                    return original(*args, **kwargs)
                if failure == "after_replace":
                    original(*args, **kwargs)
                raise OSError("synthetic replacement failure")

            patch.setattr(engine.os, "replace", fail)
        elif failure == "directory_fsync":
            original_replace, original_fsync = os.replace, os.fsync
            replaced = False

            def mark(*args, **kwargs):
                nonlocal replaced
                result = original_replace(*args, **kwargs)
                replaced = True
                return result

            def fail(descriptor):
                if replaced and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("synthetic directory fsync failure")
                return original_fsync(descriptor)

            patch.setattr(engine.os, "replace", mark)
            patch.setattr(engine.os, "fsync", fail)
        else:

            def fail(*args, **kwargs):
                raise GovernanceError("synthetic_completion_failure")

            patch.setattr(store, "verify_pass", fail)
        reason = {
            "stage_checkpoint": "synthetic_checkpoint_failure",
            "verify": "synthetic_completion_failure",
        }.get(failure, "diagnostic_file_io_failed")
        with pytest.raises(GovernanceError, match="^" + reason + "$"):
            execute(store, intent, options)
    pending = store.cleanup(intent.operation_id)
    assert pending.state == "pending" and pending.manifest is not None
    for path in stages(root):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert b"Synthetic diagnostic inventory original" not in path.read_bytes()
    if failure == "partial_stage":
        assert stages(root)[0].stat().st_size == 7
    assert execute(reopen(store), intent, options).state == "verified"
    assert contents(root) == expected and not stages(root)


def test_old_stage_never_overwrites_new_strict_tail_and_restored_files_are_recleaned(case):
    root, _ = case
    store, intent, options = prepare(case)
    original = contents(root)
    expected = retained(case, options)
    verified = execute(store, intent, options)
    sdk = root / "sdk/hy_memory.log"
    entry = next(item for item in verified.manifest.artifacts if item.before.domain == "sdk")
    old_kept = sdk.read_bytes()
    artifact = sdk.with_name(entry.stage_name)
    artifact.write_bytes(old_kept)
    artifact.chmod(0o600)
    sdk.write_bytes(old_kept + old_kept)
    expected[str(sdk.relative_to(root))] = old_kept + old_kept
    assert execute(reopen(store), intent, options).state == "verified"
    assert contents(root) == expected and not artifact.exists()
    # Restore a real historical byte-identical file and its duplicate rotated copy.
    sdk.write_bytes(original[str(sdk.relative_to(root))])
    sdk.with_name("hy_memory.log.2024-02-29").write_bytes(sdk.read_bytes())
    result = execute(reopen(store), intent, options)
    assert result.state == "verified"
    assert sdk.read_bytes() == old_kept
    assert sdk.with_name("hy_memory.log.2024-02-29").read_bytes() == old_kept


@pytest.mark.parametrize(
    "damage", ["missing_formal", "lost_kept_prefix", "stage_body", "stage_link", "stage_mode"]
)
def test_unproven_or_only_surviving_stage_is_preserved(case, monkeypatch, damage):
    root, _ = case
    store, intent, options = prepare(case)
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("synthetic replacement stop")

        patch.setattr(engine.os, "replace", fail)
        with pytest.raises(GovernanceError):
            execute(store, intent, options)
    receipt = store.cleanup(intent.operation_id)
    entry = receipt.manifest.artifacts[0]
    formal = (
        pipeline(root) if entry.before.domain == "pipeline" else root / "sdk"
    ) / entry.before.key
    artifact = formal.with_name(entry.stage_name)
    assert artifact.exists()
    if damage == "missing_formal":
        formal.unlink()
    elif damage == "lost_kept_prefix":
        formal.write_bytes(b"")
    elif damage == "stage_body":
        raw = bytearray(artifact.read_bytes())
        raw[0] ^= 1
        artifact.write_bytes(raw)
    elif damage == "stage_link":
        original = artifact.read_bytes()
        artifact.unlink()
        outside = root.parent / "outside-stage"
        outside.write_bytes(original)
        artifact.symlink_to(outside)
    else:
        artifact.chmod(0o644)
    before = contents(root), store.cleanup(intent.operation_id)
    with pytest.raises(GovernanceError):
        execute(reopen(store), intent, options)
    assert (contents(root), store.cleanup(intent.operation_id)) == before
    assert artifact.exists()


def test_real_owned_handler_reopens_same_object_and_future_output_uses_new_inode(case):
    root, _ = case
    store, intent, options = prepare(case)
    expected = retained(case, options)
    path = root / "sdk/hy_memory.log"
    handler = native._create_sdk_file_handler(
        filename=path, when="midnight", backupCount=30, encoding="utf-8"
    )
    old_stream, old_inode = handler.stream, path.stat().st_ino
    rollover = handler.rolloverAt
    safe_line = expected[str(path.relative_to(root))].decode().rstrip("\n")
    try:
        handler.setFormatter(logging.Formatter(safe_line, validate=False))
        assert execute(store, intent, options).state == "verified"
        assert old_stream.closed and handler.stream is not old_stream
        assert path.stat().st_ino != old_inode and handler.rolloverAt == rollover
        assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino
        handler.handle(
            logging.LogRecord("synthetic", logging.INFO, __file__, 1, "ignored", (), None)
        )
        handler.flush()
        assert path.read_bytes() == expected[str(path.relative_to(root))] * 2
    finally:
        handler.close()


def test_actual_reopen_failure_seals_handler_and_retry_rebinds_without_truncation(
    case, monkeypatch
):
    root, _ = case
    store, intent, options = prepare(case)
    expected = retained(case, options)
    path = root / "sdk/hy_memory.log"
    handler = native._create_sdk_file_handler(
        filename=path, when="midnight", backupCount=30, encoding="utf-8"
    )
    try:
        with monkeypatch.context() as patch:
            original = os.fdopen

            def fail(descriptor, mode="r", *args, **kwargs):
                if mode == "a":
                    raise OSError("synthetic reopen failure")
                return original(descriptor, mode, *args, **kwargs)

            patch.setattr(engine.os, "fdopen", fail)
            with pytest.raises(GovernanceError):
                execute(store, intent, options)
        assert store.cleanup(intent.operation_id).state == "pending"
        assert handler.stream is None
        assert execute(reopen(store), intent, options).state == "verified"
        assert contents(root) == expected
        assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino
    finally:
        handler.close()


def test_verified_no_stage_replay_after_real_native_rotation_uses_fresh_inventory(case):
    root, _ = case
    store, intent, options = prepare(case)
    path = root / "sdk/hy_memory.log"
    handler = native._create_sdk_file_handler(
        filename=path, when="midnight", backupCount=30, encoding="utf-8"
    )
    handler.setFormatter(StrictDiagnosticFormatter())
    try:
        previous = execute(store, intent, options)
        assert previous.state == "verified" and not stages(root)
        old_kept = path.read_bytes()
        handler.doRollover()
        handler.handle(
            logging.LogRecord("hy_memory", logging.INFO, __file__, 123, "omitted", (), None)
        )
        handler.flush()
        assert path.read_bytes() != old_kept
        assert any(item.read_bytes() == old_kept for item in path.parent.glob("hy_memory.log.*"))
        inventory = inspect_diagnostic_files(
            **options, mode="authorized", grants=previous.authority.grants
        )
        assert len(inventory.files) == 3
        assert all(not item.candidate_count and not item.removed_count for item in inventory.files)
        before = contents(root)
        current = execute(reopen(store), intent, options)
        assert current.state == "verified" and current.pass_id != previous.pass_id
        assert current.manifest.artifacts == ()
        assert contents(root) == before and not stages(root)
        assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino
    finally:
        handler.close()


def test_budget_failure_has_no_partial_artifacts(case):
    root, _ = case
    store, intent, options = prepare(case)
    before = contents(root), store.cleanup(intent.operation_id)
    with pytest.raises(GovernanceError, match="budget_exceeded"):
        execute(store, intent, options, budget=InventoryBudget(max_total_bytes=1))
    assert (contents(root), store.cleanup(intent.operation_id)) == before


@pytest.mark.parametrize(
    "failure",
    [OSError(errno.EIO, "synthetic"), OSError(errno.ELOOP, "synthetic"), ValueError("synthetic")],
)
def test_private_scan_session_preserves_consumer_errors_and_closes_handles(case, failure):
    root, requests = case
    before = contents(root)
    with pytest.raises(type(failure)) as caught:
        with inventory_module._inspection(
            data_root=root / "data", sdk_log_root=root / "sdk", requests=requests, mode="propose"
        ) as (_, scan):
            directories = tuple(item[0] for item in scan.directories)
            raise failure
    assert caught.value is failure
    for descriptor in directories:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert contents(root) == before


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (OSError(errno.EIO, "synthetic"), "diagnostic_inventory_unavailable"),
        (OSError(errno.ELOOP, "synthetic"), "diagnostic_inventory_path_unsafe"),
        (OSError(errno.ENOENT, "synthetic"), "diagnostic_inventory_changed"),
        (ValueError("synthetic"), "diagnostic_inventory_unavailable"),
    ],
)
def test_scan_itself_retains_fixed_inventory_error_mapping(case, monkeypatch, failure, reason):
    root, requests = case
    before = contents(root)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(inventory_module._Scan, "scan_file", fail)
    with pytest.raises(GovernanceError, match="^" + reason + "$"):
        inspect_diagnostic_files(
            data_root=root / "data", sdk_log_root=root / "sdk", requests=requests, mode="propose"
        )
    assert contents(root) == before


def test_successful_same_length_corrupt_stage_write_is_rejected_before_replace(case, monkeypatch):
    root, _ = case
    store, intent, options = prepare(case)
    before = contents(root)
    original_write = os.write

    def corrupt(descriptor, raw):
        value = bytearray(raw)
        value[0] ^= 1
        return original_write(descriptor, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("corrupted stage reached formal replacement")

    with monkeypatch.context() as patch:
        patch.setattr(engine.os, "write", corrupt)
        patch.setattr(engine.os, "replace", forbidden)
        with pytest.raises(GovernanceError, match="^diagnostic_file_artifact_unproven$"):
            execute(store, intent, options)
    assert {name: (root / name).read_bytes() for name in before} == before
    pending = store.cleanup(intent.operation_id)
    assert pending.state == "pending"
    assert all(entry.state == "registered" for entry in pending.manifest.artifacts)
    artifact = stages(root)[0]
    assert artifact.stat().st_size == pending.manifest.artifacts[0].retained_bytes
    old_stage = artifact.read_bytes()
    with pytest.raises(GovernanceError, match="^diagnostic_file_artifact_unproven$"):
        execute(reopen(store), intent, options)
    assert artifact.read_bytes() == old_stage
    assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("point", ["partial_stage", "after_replace"])
def test_process_death_recovers_from_only_persisted_progress(case, point):
    root, _ = case
    store, intent, options = prepare(case)
    expected = retained(case, options)
    payload = {
        "journal": str(store.journal.path),
        "instance": store.journal.instance_id,
        "key_hex": store.journal._key.hex(),
        "operation": intent.operation_id,
        "data": str(options["data_root"]),
        "sdk": str(options["sdk_log_root"]),
        "point": point,
    }
    script = """
import json, os, sys
from pathlib import Path
from cloud_memory_hub import governance_diagnostic_files as engine
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceJournal
from hy_memory.utils.file_log_lifecycle import diagnostic_file_maintenance
p = json.load(sys.stdin)
journal = GovernanceJournal(
    Path(p['journal']), instance_id=p['instance'], key=bytes.fromhex(p['key_hex'])
)
store = ExecutionStore(journal)
data, sdk = Path(p['data']), Path(p['sdk'])
if p['point'] == 'partial_stage':
    original = os.write
    def interrupted(fd, value):
        original(fd, value[:7])
        os._exit(71)
    engine.os.write = interrupted
else:
    original = os.replace
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(71)
    engine.os.replace = interrupted
with diagnostic_file_maintenance(roots=(data / 'hy-memory/governed-v2/logs', sdk)) as gate:
    engine.execute_diagnostic_files(
        store, p['operation'], data_root=data, sdk_log_root=sdk, gate=gate
    )
raise AssertionError('failure boundary was not reached')
"""
    process = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 71, process.stderr
    pending = reopen(store).cleanup(intent.operation_id)
    assert pending.state == "pending" and pending.manifest is not None
    assert execute(reopen(store), intent, options).state == "verified"
    assert contents(root) == expected and not stages(root)
