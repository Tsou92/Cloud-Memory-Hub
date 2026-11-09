"""ADR-0042 read-only proof/inventory probe using actual installed emitters.

Real HTTP admission, signed stores, Core queue and native legacy/strict formats;
no model calls or file cleanup. Sibling dependencies: probe_inflight_governance
(synthetic HTTP seed) and probe_original_archive (isolated environment).
"""

import argparse
import asyncio
import hashlib
import importlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch
from uuid import uuid4

MARKER = "Synthetic diagnostic inventory original with Unicode\u2028and newline\nkept intact."


def emit_native(root, *, shared=False):
    """Separate legacy process; never downgrade the running Hub policy."""
    from hy_memory.utils.diagnostic_policy import (
        StrictDiagnosticFormatter,
        enable_strict_diagnostics,
    )
    from hy_memory.utils.log_setup import (
        _DATE_FORMAT,
        _LOG_FORMAT,
        JsonLineFormatter,
        SingleLineFormatter,
    )
    from hy_memory.utils.pipeline_log_writer import PipelineLogWriter

    os.environ["MEMORY_LOG_LLM_CONTENT"] = "true"
    bindings = json.loads((root / "bindings.json").read_text())
    pipeline = root / "files" / "data" / "hy-memory" / "governed-v2" / "logs"
    service = pipeline if shared else root / "files" / "sdk"
    service.mkdir(parents=True, exist_ok=True)
    writer = PipelineLogWriter(str(pipeline))
    text = SingleLineFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    structured = JsonLineFormatter()
    with (service / "hy_memory.log").open("wb") as stream:
        for binding in bindings:
            writer.write_step(
                request_id=binding["request_id"],
                user_id=binding["user_id"],
                step="EXTRACT",
                prompt=MARKER,
                response=MARKER,
                parsed=json.dumps({"result": MARKER}),
                event_at_ms=1794240000000,
            )
            record = logging.LogRecord(
                "hy_memory.client", logging.INFO, __file__, 1, MARKER, (), None
            )
            record.request_id = binding["request_id"]
            for formatter in (text, structured):
                stream.write((formatter.format(record) + "\n").encode())
    writer.close()
    enable_strict_diagnostics()
    writer = PipelineLogWriter(str(pipeline))
    writer.write_step(
        request_id=str(uuid4()),
        step="EXTRACT",
        prompt=MARKER,
        response=MARKER,
        event_at_ms=1794240000000,
    )
    writer.close()
    record = logging.LogRecord("hy_memory.client", logging.INFO, __file__, 1, MARKER, (), None)
    with (service / "hy_memory.log").open("ab") as stream:
        stream.write((StrictDiagnosticFormatter().format(record) + "\n").encode())


def logical_snapshot(root):
    result = {}
    for directory in ("config", "governance", "data"):
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file():
                continue
            with path.open("rb") as source:
                if source.read(16) != b"SQLite format 3\x00":
                    continue
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                result[str(path.relative_to(root))] = hashlib.sha256(
                    "\n".join(db.iterdump()).encode()
                ).hexdigest()
    return result


def diagnostic_snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def inspect_phase(root):
    from cloud_memory_hub.app import create_hub
    from cloud_memory_hub.config import HubConfig
    from cloud_memory_hub.governance_candidates import ConfirmedScope
    from cloud_memory_hub.governance_diagnostic_inventory import inspect_diagnostic_files
    from cloud_memory_hub.governance_diagnostic_records import WriteRequest
    from cloud_memory_hub.governance_journal import GovernanceError
    from cloud_memory_hub.governance_sources import capture_rules
    from httpx import ASGITransport, AsyncClient
    from hy_memory.client import HyMemoryClient
    from one_memory.hy_memory_bridge import LocalHyEmbedService
    from probe_inflight_governance import SyntheticEmbedder, seed
    from probe_original_archive import environment

    os.environ.update(environment(root))
    config = HubConfig.from_environment()
    config.data_dir.mkdir(mode=0o700, exist_ok=True)
    calls = []

    def no_generate(*args, **kwargs):
        calls.append(True)
        raise AssertionError("probe_unexpected_generation")

    with (
        patch.object(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder()),
        patch.object(HyMemoryClient, "add", no_generate),
        patch.object(HyMemoryClient, "add_extracted", no_generate),
    ):
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                state = await seed(app, web, config)
            executor = app.core.state.cmh_executor
            async with executor._exclusive():
                records = {"cmhe:" + item["job"]: "raw" for item in state["entries"].values()}
                scope = ConfirmedScope(
                    state["scope"],
                    "purge",
                    records,
                    capture_rules(
                        executor.authority.journal, executor.lineage.ingest, state["scope"], records
                    ),
                    "preview",
                    view_digest="a" * 64,
                )
                before = logical_snapshot(root)
                sdk = await executor.bridge()._get_client()
                required_databases = (
                    config.data_dir / "one-memory.sqlite3",
                    executor.authority.access.path,
                    executor.authority.journal.path,
                    executor.inflight.admissions.journal.path,
                    Path(sdk._cache._db_path),
                    Path(sdk._config.vector_store.persist_directory) / "chroma.sqlite3",
                )
                assert all(str(path.relative_to(root)) in before for path in required_databases), (
                    "probe_required_database_missing_from_snapshot"
                )
                prepared = executor.inflight.prepare_cleanup_selectors(scope)
                assert logical_snapshot(root) == before, "probe_selector_changed_database"
                assert len(prepared.selectors) == 2, "probe_selector_incomplete"
                bindings = [
                    {
                        "request_id": item.request_id,
                        "user_id": f"cmh:{executor.authority.access.instance_id}:{scope.scope_id}",
                    }
                    for item in prepared.selectors
                ]
                (root / "bindings.json").write_text(json.dumps(bindings))
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--phase",
                        "emit-shared",
                        "--root",
                        str(root),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                assert result.returncode == 0, "probe_native_emitter_failed"
                requests = tuple(WriteRequest(**binding) for binding in bindings)
                runtime_log_root = Path(os.environ["MEMORY_LOG_DIR"])
                assert runtime_log_root == config.data_dir / "hy-memory" / "governed-v2" / "logs", (
                    "probe_runtime_layout_changed"
                )
                kwargs = {
                    "data_root": root / "files" / "data",
                    "sdk_log_root": root
                    / "files"
                    / "data"
                    / runtime_log_root.relative_to(config.data_dir),
                    "requests": requests,
                }
                files_before = diagnostic_snapshot(root / "files")
                proposed = inspect_diagnostic_files(**kwargs, mode="propose")
                assert len(proposed.grants) == 6, "probe_legacy_records_not_discovered"
                checked = inspect_diagnostic_files(
                    **kwargs, mode="authorized", grants=proposed.grants
                )
                assert checked.grants == proposed.grants, "probe_exact_grants_not_preserved"
                try:
                    inspect_diagnostic_files(**{**kwargs, "requests": requests[:1]}, mode="propose")
                except GovernanceError:
                    pass
                else:
                    raise AssertionError("probe_unselected_legacy_was_not_blocked")
                try:
                    inspect_diagnostic_files(
                        **kwargs, mode="authorized", grants=proposed.grants[:-1]
                    )
                except GovernanceError:
                    pass
                else:
                    raise AssertionError("probe_missing_record_grant_was_not_blocked")
                assert diagnostic_snapshot(root / "files") == files_before, (
                    "probe_inventory_changed_files"
                )
                assert logical_snapshot(root) == before, "probe_inventory_changed_database"
                assert not calls, "probe_model_was_called"
    print(
        json.dumps(
            {
                "phase": "inspect",
                "passed": True,
                "selected_requests": 2,
                "legacy_records": 6,
                "database_and_files_unchanged": True,
                "unselected_legacy_blocked": True,
                "missing_grant_blocked": True,
                "physical_cleanup_enabled": False,
                "external_models": False,
            },
            sort_keys=True,
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("emit", "emit-shared", "inspect"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.phase:
        assert args.root is not None
        if args.phase in {"emit", "emit-shared"}:
            emit_native(args.root, shared=args.phase == "emit-shared")
        else:
            asyncio.run(inspect_phase(args.root))
        return
    if args.installed:
        for name in (
            "governance_inflight",
            "write_admissions",
            "governance_diagnostic_records",
            "governance_diagnostic_inventory",
        ):
            module = importlib.import_module("cloud_memory_hub." + name)
            assert "site-packages" in Path(module.__file__).resolve().parts
    from probe_original_archive import environment

    root = Path(mkdtemp(prefix="cmh-diagnostic-inventory-")).resolve()
    try:
        for phase, command in (
            ("init", [sys.executable, "-m", "cloud_memory_hub", "init"]),
            (
                "inspect",
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--phase",
                    "inspect",
                    "--root",
                    str(root),
                ],
            ),
        ):
            with (root / (phase + ".log")).open("w") as log:
                result = subprocess.run(
                    command, env=environment(root), stdout=log, stderr=log, timeout=120
                )
            assert result.returncode == 0, "probe_subprocess_failed_" + phase
        print((root / "inspect.log").read_text().splitlines()[-1])
    except BaseException:
        print("Synthetic probe evidence retained at " + str(root), file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
