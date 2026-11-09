"""Old memory-only purges need no invented whole-event request certificate."""

import asyncio
import json
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CleanupAuthority,
    cleanup_from_dict,
    cleanup_policy_targets,
)
from cloud_memory_hub.governance_diagnostic_maintenance import preview_diagnostic_repair
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_execution_store import ExecutionStore as ExecutionStoreV2
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from test_diagnostic_maintenance import cli, diagnostics
from test_diagnostic_records import native_records as native_records_fixture
from test_diagnostic_selectors import all_rows
from test_governance_inflight import coordinator
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision

native_records = native_records_fixture


def old_parent(tmp_path, native, *, whole_event=False, completed=True):
    config = config_for(tmp_path)

    async def prepare():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            planner = coordinator(app)
            records = {"synthetic-legacy-memory": "memory"}
            if whole_event:
                records["cmhe:" + await accepted(web, headers, event)] = "raw"
            intent = Intent(
                operation_id=str(uuid4()),
                actor_id=str(uuid4()),
                scope_id=event["scope_id"],
                action="purge",
                targets=tuple(
                    planner.journal.target(event["scope_id"], kind, identity)
                    for identity, kind in sorted(records.items())
                ),
            )
            store = ExecutionStore(planner.journal)
            store.plan(intent, records)
            app.governance.append(intent)
            if completed:
                # A historical receipt, not proof that this fixture ran a purge.
                store.complete(intent)
            return planner, intent

    planner, intent = asyncio.run(prepare())
    for name, value in (
        ("admin-token", config.admin_token.encode()),
        ("encryption-key", config.encryption_key),
    ):
        path = config.config_dir / name
        path.write_bytes(value + b"\n")
        path.chmod(0o600)
    root = config.data_dir / "hy-memory/governed-v2/logs"
    (root / "default").mkdir(parents=True)
    (root / "hy_memory.log").write_bytes(native["strict_sdk"])
    (root / "default/2026-11-10.log").write_bytes(native["strict_pipeline"])
    return config, planner, intent


@pytest.mark.parametrize("completed", [False, True])
def test_real_stopped_cli_accepts_old_memory_only_parent_with_strict_logs(
    tmp_path, native_records, completed
):
    config, planner, intent = old_parent(tmp_path, native_records, completed=completed)
    before, files = all_rows(config, planner), diagnostics(config)
    result = cli(config, intent.operation_id)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["operation_state"] == ("completed" if completed else "planned")
    assert value["selection"]["selectors"] == value["selection"]["grants"] == []
    assert value["records"] == value["files"] == 2
    assert value["candidate_copies"] == 0
    assert value["origin_proven"] is value["body_reviewed"] is value["execution_available"] is False
    assert preview_diagnostic_repair(config, intent.operation_id) == value
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files


@pytest.mark.parametrize("body", ["unknown", "native_legacy"])
def test_empty_original_selection_still_scans_and_refuses_body_without_writes(
    tmp_path, native_records, body
):
    config, planner, intent = old_parent(tmp_path, native_records)
    path = config.data_dir / "hy-memory/governed-v2/logs/hy_memory.log"
    with path.open("ab") as stream:
        stream.write(
            native_records["text"] if body == "native_legacy" else b"unknown synthetic body\n"
        )
    before, files = all_rows(config, planner), diagnostics(config)
    with pytest.raises(GovernanceError, match="diagnostic_record_"):
        preview_diagnostic_repair(config, intent.operation_id)
    result = cli(config, intent.operation_id)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "diagnostic_preview_unavailable"}
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files


def test_real_accepted_original_without_old_certificate_stays_rejected(tmp_path, native_records):
    config, planner, intent = old_parent(tmp_path, native_records, whole_event=True)
    before, files = all_rows(config, planner), diagnostics(config)
    with pytest.raises(GovernanceError, match="governance_inflight_legacy_unavailable"):
        preview_diagnostic_repair(config, intent.operation_id)
    result = cli(config, intent.operation_id)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "diagnostic_preview_unavailable"}
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files


def test_empty_repair_is_not_promoted_to_a_new_business_parent(tmp_path, native_records):
    config, planner, parent = old_parent(tmp_path, native_records)
    authority = open_authority(config)
    store = ExecutionStoreV2(authority.journal)
    binding = store.migrate_legacy(
        store.inspect_legacy(), {parent.operation_id: CleanupAuthority("legacy", (), ())}
    )
    authority.journal.bind_execution_store(binding.store_id)
    choice = cleanup_from_dict(preview_diagnostic_repair(config, parent.operation_id)["selection"])
    repair = Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=parent.scope_id,
        action="purge",
        targets=cleanup_policy_targets(authority.journal, parent.scope_id, choice),
    )
    store.plan(repair, {}, cleanup=choice)
    authority.append(repair)
    before, files = all_rows(config, planner), diagnostics(config)
    with pytest.raises(GovernanceError, match="governance_diagnostic_parent_unproven"):
        preview_diagnostic_repair(config, repair.operation_id)
    result = cli(config, repair.operation_id)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "diagnostic_preview_unavailable"}
    assert cli(config, parent.operation_id).returncode == 0
    assert all_rows(config, planner) == before
    assert diagnostics(config) == files
