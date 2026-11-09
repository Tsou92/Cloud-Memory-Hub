"""Actual stopped CLI raises file requirements without starting or repairing data."""

import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
from cloud_memory_hub.governance_execution import ExecutionStore as LegacyStore
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.storage_lease import StorageLease, StorageLeaseError
from test_diagnostic_confirmation import intent_for
from test_governance_execution_store import database_rows, finish_pass, make_intent
from test_original_archive import config_for

CORE_PROFILE = "sdk-core-four-domains-v1"


def seeded(tmp_path, *, schema="bound", completed=False, operation=True):
    config = config_for(tmp_path)
    authority = open_authority(config)
    store = ExecutionStore(authority.journal)
    if schema in {"bound", "gap"}:
        binding = store.initialize_empty()
        if schema == "bound":
            authority.journal.bind_execution_store(binding.store_id)
        if schema == "bound" and operation:
            selection = CleanupAuthority("records", (), ())
            intent, records = intent_for(authority.journal, selection)
            store.plan(intent, records, cleanup=selection)
            authority.append(intent)
            if completed:
                receipt = finish_pass(authority.journal, store, store.cleanup(intent.operation_id))
                store.complete(intent, expected_cleanup=receipt)
    elif schema == "legacy":
        intent = make_intent(authority.journal)
        LegacyStore(authority.journal).plan(intent, {"synthetic-memory": "memory"})
        authority.append(intent)
    else:
        assert schema == "missing"
    logs = config.data_dir / "logs"
    logs.mkdir(mode=0o700)
    (logs / "core.log").write_bytes(b"Synthetic unreviewed diagnostic body remains untouched.\n")
    for name, value in (
        ("admin-token", config.admin_token.encode()),
        ("encryption-key", config.encryption_key),
    ):
        path = config.config_dir / name
        with path.open("xb") as stream:
            stream.write(value + b"\n")
        path.chmod(0o600)
    # Stable coordination files are established before zero-change assertions.
    # No business file is excluded from the before/after byte comparison.
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        pass
    return config, authority, store


def files(config):
    return {
        (label, str(path.relative_to(root))): path.read_bytes()
        for label, root in (
            ("data", config.data_dir),
            ("config", config.config_dir),
            ("governance", config.governance_dir),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def cli(config, *, stopped=True):
    launcher = """
import importlib.abc,sys
class DenyService(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in {'cloud_memory_hub.app','one_memory.api','uvicorn'}:
            raise AssertionError('activation attempted service startup')
sys.meta_path.insert(0,DenyService())
from cloud_memory_hub.__main__ import main
main()
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            "activate-file-coverage",
            "--config-dir",
            str(config.config_dir),
            "--governance-dir",
            str(config.governance_dir),
            *(["--writers-stopped"] if stopped else []),
        ],
        env={**os.environ, "CMH_DATA_DIR": str(config.data_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("completed", (False, True))
def test_actual_cli_activates_pending_or_old_completed_store_and_is_idempotent(tmp_path, completed):
    config, authority, store = seeded(tmp_path, completed=completed)
    original = files(config)
    before = database_rows(authority.journal.path)
    result = cli(config)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value == {
        "version": 1,
        "store_id": store.snapshot().binding.store_id,
        "required_profile": CORE_PROFILE,
        "pending": 1,
        "changed": True,
    }
    after = database_rows(authority.journal.path)
    old_caps = before["tables"].pop("capabilities")
    new_caps = after["tables"].pop("capabilities")
    assert after == before
    assert set(map(tuple, new_caps)) - set(map(tuple, old_caps)) == {
        (
            "execution_file_coverage_v2",
            value["store_id"],
            authority.journal._mac("capability", ["execution_file_coverage_v2", value["store_id"]]),
        )
    }
    current = files(config)
    journal_key = ("governance", "journal.sqlite3")
    assert {key: data for key, data in current.items() if key != journal_key} == {
        key: data for key, data in original.items() if key != journal_key
    }
    again = cli(config)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == {**value, "changed": False}
    assert files(config) == current
    with pytest.raises(GovernanceError, match="execution_runtime_unsupported"):
        open_authority(config, read_only=True).require_ready()
    assert (
        config.admin_token not in result.stdout
        and config.encryption_key.decode() not in result.stdout
    )
    assert "Synthetic unreviewed" not in result.stdout


@pytest.mark.parametrize("schema", ("legacy", "gap", "missing"))
def test_actual_cli_never_creates_migrates_or_binds_an_old_execution_store(tmp_path, schema):
    config, authority, _ = seeded(tmp_path, schema=schema)
    before = files(config)
    result = cli(config)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "file_coverage_activation_unavailable"}
    assert authority.journal.execution_store_id() is None
    assert files(config) == before


@pytest.mark.parametrize(
    "damage", ("store_signature", "missing_witness", "bad_witness", "wrong_store")
)
def test_readonly_preflight_failure_keeps_all_rows_files_and_witnesses(tmp_path, damage):
    config, authority, store = seeded(tmp_path)
    if damage == "store_signature":
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE binding SET baseline_root=?", ("a" * 64,))
    elif damage == "wrong_store":
        value = str(uuid4())
        with sqlite3.connect(authority.journal.path) as db:
            db.execute(
                "UPDATE capabilities SET value=?,signature=? WHERE name='execution_store_v2'",
                (value, authority.journal._mac("capability", ["execution_store_v2", value])),
            )
    else:
        with sqlite3.connect(authority.access.path) as db:
            if damage == "missing_witness":
                db.execute("DELETE FROM cmh_governance_floors")
            else:
                db.execute("UPDATE cmh_governance_floors SET signature=?", ("a" * 64,))
    before = files(config)
    result = cli(config)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "file_coverage_activation_unavailable"}
    assert files(config) == before


@pytest.mark.parametrize("root", ("data_dir", "config_dir", "governance_dir"))
def test_real_process_lock_on_any_root_prevents_activation(tmp_path, root):
    config, _, _ = seeded(tmp_path)
    before = files(config)
    with StorageLease(getattr(config, root)):
        result = cli(config)
    assert result.returncode == 2 and "storage_in_use" in result.stderr
    assert not result.stdout
    assert files(config) == before


def test_cli_requires_explicit_stopped_writers_flag(tmp_path):
    config, _, _ = seeded(tmp_path)
    before = files(config)
    result = cli(config, stopped=False)
    assert result.returncode == 2 and "--writers-stopped" in result.stderr
    assert not result.stdout and files(config) == before


def test_helper_counts_unplanned_and_orphans_without_discarding_them(tmp_path):
    from cloud_memory_hub.governance_diagnostic_maintenance import activate_file_coverage

    config, authority, store = seeded(tmp_path)
    unplanned, orphan = make_intent(authority.journal), make_intent(authority.journal)
    authority.append(unplanned)
    store.plan(orphan, {"synthetic-memory": "memory"})
    before = database_rows(store.path), database_rows(authority.access.path)
    result = activate_file_coverage(config)
    assert result["pending"] == 3 and result["changed"] is True
    assert (database_rows(store.path), database_rows(authority.access.path)) == before


def test_empty_bound_store_can_raise_requirement_without_claiming_file_cleanliness(tmp_path):
    config, authority, store = seeded(tmp_path, operation=False)
    before = database_rows(store.path), database_rows(authority.access.path)
    body = (config.data_dir / "logs/core.log").read_bytes()
    result = cli(config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "version": 1,
        "required_profile": CORE_PROFILE,
        "store_id": store.snapshot().binding.store_id,
        "pending": 0,
        "changed": True,
    }
    assert (database_rows(store.path), database_rows(authority.access.path)) == before
    assert (config.data_dir / "logs/core.log").read_bytes() == body
    with pytest.raises(GovernanceError, match="execution_runtime_unsupported"):
        open_authority(config, read_only=True).require_ready()


def test_post_verification_failure_retains_raised_requirement_and_retries_under_lease(
    tmp_path, monkeypatch
):
    from cloud_memory_hub import governance_diagnostic_maintenance as maintenance

    config, authority, store = seeded(tmp_path)
    before = database_rows(store.path), database_rows(authority.access.path)
    original = maintenance._coverage_activation_state
    calls = []

    def observed(config):
        for root in (config.data_dir, config.config_dir, config.governance_dir):
            with pytest.raises(StorageLeaseError, match="^storage_in_use$"):
                with StorageLease(root):
                    pytest.fail("activation released process exclusion before verification")
        state = original(config)
        calls.append(state[3].required_profile)
        if len(calls) == 2:
            raise GovernanceError("synthetic_post_verification_failure")
        return state

    monkeypatch.setattr(maintenance, "_coverage_activation_state", observed)
    with pytest.raises(GovernanceError, match="^synthetic_post_verification_failure$"):
        maintenance.activate_file_coverage(config)
    assert calls == ["sdk-two-domains-v1", CORE_PROFILE]
    assert authority.journal.file_coverage_profile() == CORE_PROFILE
    assert (database_rows(store.path), database_rows(authority.access.path)) == before
    monkeypatch.setattr(maintenance, "_coverage_activation_state", original)
    retry = maintenance.activate_file_coverage(config)
    assert retry["changed"] is False and retry["pending"] == 1
    assert (database_rows(store.path), database_rows(authority.access.path)) == before
