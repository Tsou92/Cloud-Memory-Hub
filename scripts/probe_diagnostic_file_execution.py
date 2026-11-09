"""Installed ADR-0043 file mechanism, using synthetic HTTP events and real I/O.

Sibling dependencies: probe_diagnostic_inventory (native legacy emitter and
logical DB snapshots), probe_inflight_governance (HTTP seed), and
probe_original_archive (isolated environment). No tests/source imports, model
calls or whole-purge completion claims. Each recovery runs in a fresh process.
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch
from uuid import uuid4


def state_path(root):
    return root / "file-execution.json"


def load_state(root):
    return json.loads(state_path(root).read_text())


def save_state(root, value):
    state_path(root).write_text(json.dumps(value, sort_keys=True))


def logs(root):
    return root / "data/hy-memory/governed-v2/logs"


def files_at(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def put_files(directory, values):
    for name, raw in values.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def forbid_network():
    def forbidden(*args, **kwargs):
        raise AssertionError("probe_external_network_forbidden")

    socket.create_connection = forbidden
    socket.socket.connect = forbidden


def require_installed_sources():
    for name in (
        "cloud_memory_hub.governance_execution_store",
        "cloud_memory_hub.governance_diagnostic_files",
        "cloud_memory_hub.governance_diagnostic_confirmation",
        "cloud_memory_hub.governance_diagnostic_recovery",
        "cloud_memory_hub.governance_diagnostic_maintenance",
        "cloud_memory_hub.governance_diagnostic_inventory",
        "hy_memory.client",
        "hy_memory.utils.file_log_lifecycle",
        "one_memory.api",
        "one_memory.hy_memory_bridge",
        "one_memory.memory_server.durable_queue",
    ):
        module = importlib.import_module(name)
        assert "site-packages" in Path(module.__file__).resolve().parts, "probe_source_imported"


async def seed_phase(root):
    from cloud_memory_hub.app import create_hub
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.governance_candidates import ConfirmedScope
    from cloud_memory_hub.governance_sources import capture_rules
    from httpx import ASGITransport, AsyncClient
    from hy_memory.client import HyMemoryClient
    from one_memory.hy_memory_bridge import LocalHyEmbedService
    from probe_inflight_governance import SyntheticEmbedder, seed

    config = HubConfig.from_environment()
    config.data_dir.mkdir(mode=0o700, exist_ok=True)
    calls = []

    def no_generation(*args, **kwargs):
        calls.append(True)
        raise AssertionError("probe_unexpected_generation")

    with (
        patch.object(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder()),
        patch.object(HyMemoryClient, "add", no_generation),
        patch.object(HyMemoryClient, "add_extracted", no_generation),
    ):
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                seeded = await seed(app, web, config)
                executor = app.core.state.cmh_executor
                records = {"cmhe:" + item["job"]: "raw" for item in seeded["entries"].values()}
                async with executor._resume_lock, executor._exclusive():
                    scope = ConfirmedScope(
                        seeded["scope"],
                        "purge",
                        records,
                        capture_rules(
                            executor.authority.journal,
                            executor.lineage.ingest,
                            seeded["scope"],
                            records,
                        ),
                        "preview",
                        view_digest="a" * 64,
                    )
                    selected = executor.inflight.prepare_cleanup_selectors(scope)
                admin = {"Authorization": "Bearer " + config.admin_token}
                request = {
                    "scope_id": seeded["scope"],
                    "action": "purge",
                    "record_ids": sorted(records),
                }
                preview = await web.post("/hub/v1/governance/preview", headers=admin, json=request)
                assert preview.status_code == 200, "probe_business_preview_failed"
                operation_id = str(uuid4())
                result = await web.post(
                    "/hub/v1/governance/execute",
                    headers=admin,
                    json={
                        **request,
                        "operation_id": operation_id,
                        "expected_view": preview.json()["view_digest"],
                    },
                )
                assert result.status_code == 200, "probe_legacy_business_purge_failed"
                assert result.json()["state"] == "completed", "probe_parent_not_completed"
                journal = executor.authority.journal
                parent = executor.store.operations()[operation_id][1]
                bindings = [
                    {
                        "request_id": item.request_id,
                        "user_id": f"cmh:{journal.instance_id}:{seeded['scope']}",
                    }
                    for item in selected.selectors
                ]
                (root / "bindings.json").write_text(json.dumps(bindings))
                save_state(
                    root,
                    {
                        "parent": operation_id,
                        "scope": seeded["scope"],
                        "parent_intent": parent.model_dump(mode="json"),
                        "selectors": [asdict(item) for item in selected.selectors],
                    },
                )
        assert not calls, "probe_model_was_called"


def emit_phase(root):
    # No app/environment import may activate strict policy before legacy emission.
    from probe_diagnostic_inventory import emit_native

    emit_native(root, shared=True)
    source = root / "files/data/hy-memory/governed-v2/logs"
    for name, raw in files_at(source).items():
        target = logs(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab") as stream:
            stream.write(raw)
    put_files(root / "legacy-fixture", files_at(logs(root)))

    from cloud_memory_hub.governance_diagnostic_inventory import inspect_diagnostic_files
    from cloud_memory_hub.governance_diagnostic_records import RecordInspector, WriteRequest

    requests = tuple(
        WriteRequest(**item) for item in json.loads((root / "bindings.json").read_text())
    )
    inventory = inspect_diagnostic_files(
        data_root=root / "data", sdk_log_root=logs(root), requests=requests, mode="propose"
    )
    assert len(inventory.grants) == 6, "probe_native_three_formats_missing"
    inspector = RecordInspector(requests, mode="propose")
    expected = {}
    for item in inventory.files:
        kept = []
        with (logs(root) / item.key).open("rb") as source:
            for line in source:
                decision = inspector.inspect(line, kind=item.domain)
                assert decision.action in {"keep", "candidate"}, "probe_fixture_record_invalid"
                if decision.action == "keep":
                    kept.append(line)
        expected[item.key] = b"".join(kept)
    put_files(root / "retained-fixture", expected)


def migrate_phase(root):
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_candidates import ConfirmedScope
    from cloud_memory_hub.governance_diagnostic_confirmation import CleanupAuthority
    from cloud_memory_hub.governance_diagnostic_maintenance import _StoppedQueueLocation
    from cloud_memory_hub.governance_execution_store import ExecutionStore
    from cloud_memory_hub.governance_inflight import InflightGovernance
    from cloud_memory_hub.remote_ingest import RemoteIngest
    from cloud_memory_hub.storage_lease import StorageLease
    from cloud_memory_hub.write_admissions import WriteAdmissions

    config, state = HubConfig.from_environment(), load_state(root)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        readonly = open_authority(config, read_only=True)
        observation = ExecutionStore(readonly.journal, read_only=True).inspect_legacy()
        parent = observation.operations[state["parent"]]
        assert parent.state == "completed", "probe_old_completed_missing"
        ingest = RemoteIngest(
            readonly.access,
            lambda: _StoppedQueueLocation(config.data_dir / "one-memory.sqlite3"),
            read_only=True,
        )
        planner = InflightGovernance(
            ingest,
            WriteAdmissions(readonly.access, readonly.journal, read_only=True),
            readonly.journal,
        )
        scope = ConfirmedScope(
            parent.intent.scope_id,
            "purge",
            parent.records,
            parent.intent.targets,
            "operation",
            operation_id=parent.intent.operation_id,
        )
        recovered = planner.prepare_cleanup_selectors(scope, signed=True, completed=True)
        assert [asdict(item) for item in recovered.selectors] == state["selectors"], (
            "probe_anchored_selectors_changed"
        )
        authority = open_authority(config)
        store = ExecutionStore(authority.journal)
        before = authority.journal.head(state["scope"])
        binding = store.migrate_legacy(
            observation, {state["parent"]: CleanupAuthority("legacy", recovered.selectors, ())}
        )
        assert authority.journal.head(state["scope"]) == before, "probe_migration_resigned_intent"
        assert authority.journal.execution_store_id() is None, "probe_capability_autoactivated"
        assert store.has_pending(), "probe_completed_masked_cleanup_pending"
        state["store_id"] = binding.store_id
        save_state(root, state)


def repair_phase(root):
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_diagnostic_confirmation import (
        CleanupAuthority,
        RepairParent,
        cleanup_policy_targets,
    )
    from cloud_memory_hub.governance_diagnostic_inventory import inspect_diagnostic_files
    from cloud_memory_hub.governance_diagnostic_records import WriteRequest
    from cloud_memory_hub.governance_execution_store import ExecutionStore
    from cloud_memory_hub.governance_journal import Intent
    from cloud_memory_hub.storage_lease import StorageLease

    config, state = HubConfig.from_environment(), load_state(root)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        authority = open_authority(config)
        journal, store = authority.journal, ExecutionStore(authority.journal)
        binding = store.activation_binding()
        assert binding.store_id == state["store_id"], "probe_migration_binding_changed"
        journal.bind_execution_store(binding.store_id)
        parent = store.recovery_snapshot().snapshot.operations[state["parent"]]
        with journal._connection() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE operation_id=?", (state["parent"],)
            ).fetchone()
            assert journal._decode(row) == parent.intent, "probe_parent_chain_changed"
            parent_digest = row["digest"]
        requests = tuple(
            WriteRequest(item.request_id, f"cmh:{journal.instance_id}:{state['scope']}")
            for item in parent.cleanup.authority.selectors
        )
        inventory = inspect_diagnostic_files(
            data_root=config.data_dir, sdk_log_root=logs(root), requests=requests, mode="propose"
        )
        selection = CleanupAuthority(
            "repair",
            parent.cleanup.authority.selectors,
            inventory.grants,
            RepairParent(state["parent"], parent_digest, journal.ledger_id),
        )
        repair = Intent(
            operation_id=str(uuid4()),
            actor_id=journal.instance_id,
            scope_id=state["scope"],
            action="purge",
            targets=cleanup_policy_targets(journal, state["scope"], selection),
        )
        store.plan(repair, {}, cleanup=selection)
        authority.append(repair)
        state.update(
            repair=repair.operation_id, repair_head=journal.head(state["scope"]).model_dump()
        )
        save_state(root, state)
    execute_phase(root, failure=True)


def batch_repair_phase(root):
    """An additional real signed repair covers only the newly changed bytes."""
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_diagnostic_confirmation import (
        CleanupAuthority,
        RepairParent,
        cleanup_policy_targets,
        cleanup_to_dict,
    )
    from cloud_memory_hub.governance_diagnostic_inventory import inspect_diagnostic_files
    from cloud_memory_hub.governance_diagnostic_records import WriteRequest
    from cloud_memory_hub.governance_execution_store import ExecutionStore
    from cloud_memory_hub.governance_journal import Intent
    from cloud_memory_hub.storage_lease import StorageLease

    config, state = HubConfig.from_environment(), load_state(root)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        authority = open_authority(config)
        store, journal = ExecutionStore(authority.journal), authority.journal
        recovery = store.recovery_snapshot()
        parent = recovery.snapshot.operations[state["parent"]]
        requests = tuple(
            WriteRequest(item.request_id, f"cmh:{journal.instance_id}:{parent.intent.scope_id}")
            for item in parent.cleanup.authority.selectors
        )
        proposal = inspect_diagnostic_files(
            data_root=config.data_dir, sdk_log_root=logs(root), requests=requests, mode="propose"
        )
        previous = recovery.snapshot.operations[state["repair"]].cleanup.authority.grants
        added = tuple(sorted(set(proposal.grants) - set(previous)))
        assert len(added) == 1, "probe_new_exact_variant_missing"
        selected = CleanupAuthority(
            "repair",
            parent.cleanup.authority.selectors,
            added,
            RepairParent(
                state["parent"], recovery.committed_digests[state["parent"]], journal.ledger_id
            ),
        )
        intent = Intent(
            operation_id=str(uuid4()),
            actor_id=journal.instance_id,
            scope_id=parent.intent.scope_id,
            action="purge",
            targets=cleanup_policy_targets(journal, parent.intent.scope_id, selected),
        )
        store.plan(intent, {}, cleanup=selected)
        authority.append(intent)
        actual = store.recovery_snapshot().snapshot.operations
        assert len(actual) == 3, "probe_batch_members_changed"
        state.update(
            batch_head=journal.head(state["scope"]).model_dump(),
            batch_states={key: value.state for key, value in actual.items()},
            batch_authorities={
                key: cleanup_to_dict(value.cleanup.authority) for key, value in actual.items()
            },
        )
        save_state(root, state)
        put_files(root / "batch-fixture", files_at(logs(root)))
    execute_phase(root, failure=True, batch=True)


def execute_phase(root, *, failure=False, restored=False, batch=False):
    from cloud_memory_hub import governance_diagnostic_files as engine
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.diagnostics import diagnostic_file_maintenance
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_diagnostic_confirmation import cleanup_to_dict
    from cloud_memory_hub.governance_execution_store import ExecutionStore
    from cloud_memory_hub.governance_journal import GovernanceError
    from cloud_memory_hub.storage_lease import StorageLease
    from hy_memory.utils.diagnostic_policy import StrictDiagnosticFormatter
    from hy_memory.utils.file_log_lifecycle import _create_sdk_file_handler

    config, state = HubConfig.from_environment(), load_state(root)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        authority = open_authority(config)
        store = ExecutionStore(authority.journal)
        path = logs(root) / "hy_memory.log"
        handler = _create_sdk_file_handler(
            filename=path, when="midnight", backupCount=30, encoding="utf-8"
        )
        handler.setFormatter(StrictDiagnosticFormatter())
        old_stream, rollover = handler.stream, handler.rolloverAt
        try:

            def run():
                authority.verify()
                with diagnostic_file_maintenance(roots=(logs(root),)) as gate:
                    if batch:
                        return engine.recover_diagnostic_files(
                            store, data_root=config.data_dir, sdk_log_root=logs(root), gate=gate
                        )
                    return engine.execute_diagnostic_files(
                        store,
                        state["repair"],
                        data_root=config.data_dir,
                        sdk_log_root=logs(root),
                        gate=gate,
                    )

            if failure:
                calls, original = [], engine.os.replace

                def interrupted(*args, **kwargs):
                    calls.append(True)
                    if len(calls) == 2:
                        raise OSError("synthetic second replacement interruption")
                    return original(*args, **kwargs)

                with patch.object(engine.os, "replace", interrupted):
                    try:
                        run()
                    except GovernanceError as error:
                        assert str(error) == "diagnostic_file_io_failed", "probe_unexpected_failure"
                    else:
                        raise AssertionError("probe_failure_not_reached")
                assert len(calls) == 2, "probe_replacement_boundary_missing"
                owner = min(state["batch_states"]) if batch else state["repair"]
                assert store.cleanup(owner).state == "pending", "probe_failure_not_pending"
                assert any(name.endswith(".tmp") for name in files_at(logs(root))), (
                    "probe_stage_missing"
                )
                return
            result = run()
            if batch:
                assert len(result) == 3
                assert all(item.state == "verified" and item.verification for item in result)
                assert all(item.manifest.contributors is not None for item in result)
                actual = store.recovery_snapshot().snapshot.operations
                assert {key: item.state for key, item in actual.items()} == state["batch_states"]
                assert {
                    key: cleanup_to_dict(item.cleanup.authority) for key, item in actual.items()
                } == state["batch_authorities"], "probe_batch_rewrote_original_authority"
            else:
                assert result.state == "verified" and result.verification is not None
            assert store.operations()[state["repair"]][0] == "planned", "probe_claimed_whole_purge"
            assert store.cleanup(state["parent"]).authority.grants == (), (
                "probe_resigned_old_grants"
            )
            assert (
                store.operations()[state["parent"]][1].model_dump(mode="json")
                == state["parent_intent"]
            )
            expected_head = state["batch_head"] if batch else state["repair_head"]
            assert authority.journal.head(state["scope"]).model_dump() == expected_head
            expected = files_at(root / "retained-fixture")
            if restored:
                expected["hy_memory.log"] += (root / "safe-tail.bin").read_bytes()
            assert files_at(logs(root)) == expected, "probe_retained_bytes_changed"
            assert old_stream.closed and handler.stream is not old_stream, (
                "probe_stream_not_reopened"
            )
            assert handler.rolloverAt == rollover, "probe_rollover_changed"
            assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino, (
                "probe_old_inode_open"
            )
            previous = path.read_bytes()
            handler.handle(
                logging.LogRecord(
                    "hy_memory.client",
                    logging.INFO,
                    __file__,
                    1,
                    "synthetic post-cleanup output",
                    (),
                    None,
                )
            )
            handler.flush()
            assert path.read_bytes().startswith(previous) and len(path.read_bytes()) > len(previous)
            if not restored:
                (root / "safe-tail.bin").write_bytes(path.read_bytes()[len(previous) :])
        finally:
            handler.close()


def restore_phase(root, *, batch=False):
    put_files(logs(root), files_at(root / ("batch-fixture" if batch else "legacy-fixture")))
    with (logs(root) / "hy_memory.log").open("ab") as stream:
        stream.write((root / "safe-tail.bin").read_bytes())


def changed_record_phase(root):
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.diagnostics import diagnostic_file_maintenance
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_diagnostic_files import execute_diagnostic_files
    from cloud_memory_hub.governance_execution_store import ExecutionStore
    from cloud_memory_hub.governance_journal import GovernanceError
    from cloud_memory_hub.storage_lease import StorageLease

    put_files(logs(root), files_at(root / "legacy-fixture"))
    path = logs(root) / "hy_memory.log"
    raw = path.read_bytes()
    assert b"Synthetic" in raw, "probe_native_body_missing"
    path.write_bytes(raw.replace(b"Synthetic", b"Modified", 1))
    config, state = HubConfig.from_environment(), load_state(root)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        authority = open_authority(config)
        store = ExecutionStore(authority.journal)
        before = files_at(logs(root)), store.cleanup(state["repair"])
        try:
            with diagnostic_file_maintenance(roots=(logs(root),)) as gate:
                execute_diagnostic_files(
                    store,
                    state["repair"],
                    data_root=config.data_dir,
                    sdk_log_root=logs(root),
                    gate=gate,
                )
        except GovernanceError as error:
            assert str(error) == "diagnostic_record_grant_missing", (
                "probe_wrong_changed_record_failure"
            )
        else:
            raise AssertionError("probe_changed_record_was_erased")
        assert (files_at(logs(root)), store.cleanup(state["repair"])) == before


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed", action="store_true")
    parser.add_argument(
        "--phase",
        choices=(
            "seed",
            "emit",
            "migrate",
            "repair-fault",
            "recover",
            "restore",
            "recover-restored",
            "changed-record",
            "batch-repair-fault",
            "batch-recover",
            "batch-restore",
            "batch-recover-restored",
        ),
    )
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.phase:
        assert args.root is not None
        forbid_network()
        if args.phase == "emit":
            emit_phase(args.root)
        else:
            from cloud_memory_hub.diagnostics import initialize_diagnostics
            from probe_original_archive import environment

            os.environ.update(environment(args.root))
            initialize_diagnostics()
            actions = {
                "seed": lambda: asyncio.run(seed_phase(args.root)),
                "migrate": lambda: migrate_phase(args.root),
                "repair-fault": lambda: repair_phase(args.root),
                "recover": lambda: execute_phase(args.root),
                "restore": lambda: restore_phase(args.root),
                "recover-restored": lambda: execute_phase(args.root, restored=True),
                "changed-record": lambda: changed_record_phase(args.root),
                "batch-repair-fault": lambda: batch_repair_phase(args.root),
                "batch-recover": lambda: execute_phase(args.root, batch=True),
                "batch-restore": lambda: restore_phase(args.root, batch=True),
                "batch-recover-restored": lambda: execute_phase(
                    args.root, restored=True, batch=True
                ),
            }
            actions[args.phase]()
        if args.installed:
            require_installed_sources()
        print(json.dumps({"phase": args.phase, "passed": True}, sort_keys=True))
        return
    if args.installed:
        require_installed_sources()
    from probe_diagnostic_inventory import logical_snapshot
    from probe_original_archive import environment

    root = Path(mkdtemp(prefix="cmh-diagnostic-file-execution-")).resolve()
    try:

        def invoke(name, command):
            with (root / (name + ".log")).open("w") as stream:
                result = subprocess.run(
                    command, env=environment(root), stdout=stream, stderr=stream, timeout=120
                )
            assert result.returncode == 0, "probe_subprocess_failed_" + name

        def phase(name):
            invoke(
                name,
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--phase",
                    name,
                    "--root",
                    str(root),
                    *(["--installed"] if args.installed else []),
                ],
            )

        def preview(name):
            before = logical_snapshot(root)
            invoke(
                name,
                [
                    sys.executable,
                    "-m",
                    "cloud_memory_hub",
                    "diagnostic-preview",
                    "--writers-stopped",
                    "--operation-id",
                    load_state(root)["parent"],
                ],
            )
            result = json.loads((root / (name + ".log")).read_text().splitlines()[-1])
            assert result["execution_available"] is False and result["origin_proven"] is False
            assert len(result["selection"]["grants"]) == 6, "probe_cli_candidates_missing"
            assert logical_snapshot(root) == before, "probe_readonly_cli_changed_database"

        invoke("init", [sys.executable, "-m", "cloud_memory_hub", "init"])
        phase("seed")
        phase("emit")
        preview("preview-v1")
        phase("migrate")
        preview("preview-capability-gap")
        for name in (
            "repair-fault",
            "recover",
            "restore",
            "recover-restored",
            "changed-record",
            "batch-repair-fault",
            "batch-recover",
            "batch-restore",
            "batch-recover-restored",
        ):
            phase(name)
        print(
            json.dumps(
                {
                    "passed": True,
                    "http_requests": 2,
                    "native_legacy_records": 6,
                    "readonly_cli": True,
                    "migration_gap_recovered": True,
                    "repair_intent": True,
                    "interrupted_io_pending": True,
                    "fresh_process_recovery": True,
                    "handler_reopened": True,
                    "exact_file_restore_recleaned": True,
                    "strict_tail_preserved": True,
                    "changed_record_blocked": True,
                    "signed_multi_repair_recovery": True,
                    "legacy_parent_authority_unchanged": True,
                    "batch_interruption_and_restore": True,
                    "whole_purge_completed": False,
                    "external_models": False,
                },
                sort_keys=True,
            )
        )
    except BaseException:
        print("Synthetic probe evidence retained at " + str(root), file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
