"""Real stopped CLI reaches a bounded preview without service startup or writes."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
from cloud_memory_hub.governance_diagnostic_maintenance import preview_diagnostic_repair
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_execution_store import ExecutionStore as ExecutionStoreV2
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.storage_lease import StorageLease
from test_diagnostic_selectors import all_rows
from test_governance_candidates import confirmed
from test_governance_inflight import coordinator, signed_scope
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision

PROBE = Path(__file__).resolve().parents[3] / "scripts/probe_diagnostic_inventory.py"


def seed(tmp_path, *, completed=False):
    config = config_for(tmp_path)

    async def prepare():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner = coordinator(app)
            scope = confirmed(app, event, [job])
            selection = planner.prepare_cleanup_selectors(scope)
            operation = signed_scope(planner, scope, selection.rule_targets)
            app.governance.verify()
            if completed:
                # Model a historical completed receipt with separately restored logs.
                # This is synthetic fixture construction, not a successful file purge.
                store = ExecutionStore(planner.journal)
                intent = store.operations()[operation.operation_id][1]
                store.complete(intent)
            return planner, operation, selection

    planner, operation, selection = asyncio.run(prepare())
    # Embedded fixtures use an in-memory config; the actual stopped CLI reads
    # explicit private credential files just like an initialized installation.
    for name, value in (
        ("admin-token", config.admin_token.encode()),
        ("encryption-key", config.encryption_key),
    ):
        path = config.config_dir / name
        with path.open("xb") as stream:
            stream.write(value + b"\n")
        path.chmod(0o600)
    emitter = tmp_path / "emitter"
    emitter.mkdir()
    (emitter / "bindings.json").write_text(
        json.dumps(
            [
                {
                    "request_id": item.request_id,
                    "user_id": f"cmh:{planner.journal.instance_id}:{operation.scope_id}",
                }
                for item in selection.selectors
            ]
        )
    )
    process = subprocess.run(
        [sys.executable, str(PROBE), "--phase", "emit-shared", "--root", str(emitter)],
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr.decode()
    source = emitter / "files/data/hy-memory/governed-v2/logs"
    destination = config.data_dir / "hy-memory/governed-v2/logs"
    assert not destination.exists()
    shutil.copytree(source, destination)
    return config, planner, operation


def diagnostics(config):
    root = config.data_dir / "hy-memory/governed-v2/logs"
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def cli(config, operation_id, *extra):
    # Execute the actual CLI with an import guard: creating the application or
    # starting Uvicorn cannot accidentally be the way the preview succeeds.
    launcher = """
import importlib.abc,sys
class DenyService(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in {'cloud_memory_hub.app','uvicorn'}:
            raise AssertionError('maintenance attempted service startup')
sys.meta_path.insert(0,DenyService())
from cloud_memory_hub.__main__ import main
main()
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            "diagnostic-preview",
            "--writers-stopped",
            "--config-dir",
            str(config.config_dir),
            "--governance-dir",
            str(config.governance_dir),
            "--operation-id",
            operation_id,
            *extra,
        ],
        env={**os.environ, "CMH_DATA_DIR": str(config.data_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("completed", [False, True])
def test_actual_stopped_cli_preview_leaves_every_store_and_file_unchanged(tmp_path, completed):
    config, planner, operation = seed(tmp_path, completed=completed)
    if not completed:
        with pytest.raises(GovernanceError, match="execution_pending"):
            open_authority(config, read_only=True).require_writes_ready()
    before, files = all_rows(config, planner), diagnostics(config)
    result = cli(config, operation.operation_id)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["operation_state"] == ("completed" if completed else "planned")
    assert value["candidate_copies"] == len(value["selection"]["grants"]) == 3
    assert value["execution_available"] is False
    assert value["origin_proven"] is False and value["body_reviewed"] is False
    assert "Synthetic diagnostic inventory original" not in result.stdout
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files
    assert preview_diagnostic_repair(config, operation.operation_id) == value


@pytest.mark.parametrize(
    "problem", ["busy", "unknown_operation", "unknown_record", "missing_floor"]
)
def test_stopped_cli_refuses_without_partial_metadata_or_writes(tmp_path, problem):
    config, planner, operation = seed(tmp_path)
    operation_id = str(uuid4()) if problem == "unknown_operation" else operation.operation_id
    if problem == "unknown_record":
        with (config.data_dir / "hy-memory/governed-v2/logs/hy_memory.log").open("ab") as stream:
            stream.write(b"unrecognized synthetic tail\n")
    if problem == "missing_floor":
        with planner.admissions.access._connection(writing=True) as db:
            db.execute("DELETE FROM cmh_governance_floors")
    before, files = all_rows(config, planner), diagnostics(config)
    if problem == "busy":
        with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
            result = cli(config, operation_id)
        assert "storage_in_use" in result.stderr
        assert not result.stdout
    else:
        result = cli(config, operation_id)
        assert json.loads(result.stdout) == {"error": "diagnostic_preview_unavailable"}
    assert result.returncode == 2
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files


@pytest.mark.parametrize("activation", ["gap", "bound", "changed_gap"])
def test_preview_survives_explicit_migration_gap_without_activating_runtime(tmp_path, activation):
    config, planner, operation = seed(tmp_path)
    prepared = planner.prepare_cleanup_selectors(operation, signed=True)
    store = ExecutionStoreV2(planner.journal)
    old = store.inspect_legacy()
    binding = store.migrate_legacy(
        old, {operation.operation_id: CleanupAuthority("legacy", prepared.selectors, ())}
    )
    if activation == "bound":
        planner.journal.bind_execution_store(binding.store_id)
    if activation == "changed_gap":
        from test_governance_journal import intent

        # An unrelated new journal entry means this is no longer the frozen
        # stopped migration baseline; inspection must not silently bless it.
        open_authority(config).append(intent(planner.journal))
    before, files = all_rows(config, planner), diagnostics(config)
    result = cli(config, operation.operation_id)
    value = json.loads(result.stdout)
    if activation == "changed_gap":
        assert result.returncode == 2
        assert value == {"error": "diagnostic_preview_unavailable"}
    else:
        assert result.returncode == 0, result.stderr
        assert value["candidate_copies"] == 3
        assert value["execution_available"] is False
    assert planner.journal.execution_store_id() == (
        binding.store_id if activation == "bound" else None
    )
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files
