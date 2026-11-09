"""Installed productivity-forget refusal probe, using private synthetic stores.

SDK storage, output lineage, HTTP execution and restart are real. The reused
seed helper inserts synthetic records and supplies a controlled SDK writer
result; embeddings are also local. This does not exercise native extraction
or verify complete productivity forgetting.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch
from uuid import uuid4

from cloud_memory_hub import governance_execution
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.productivity_read import record_id
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.store import _variant_db_path
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import LocalHyEmbedService
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase
from probe_sdk_reconciliation import seed_output

REFUSAL = "governance_forget_kind_unsupported"
SCENES = ("working", "coding")


class SyntheticEmbedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


@asynccontextmanager
async def runtime(app, config):
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        bridge = _sdk_library_bridge(app.core)
        sdk = await bridge._get_client()
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            yield bridge, sdk, web


def snapshot(config, state):
    """Read exact synthetic rows without starting a writer after failed startup."""
    path = (config.data_dir / state["database"]).resolve()
    assert path.is_relative_to(config.data_dir.resolve()), "probe_database_outside_fixture"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        records = {}
        for name, table, column in (
            ("experience", "coding_memory_meta", "experience_id"),
            ("fact", "working_fact", "fact_id"),
        ):
            row = db.execute(f"SELECT * FROM {table} WHERE {column}=?", (state[name],)).fetchone()
            assert row is not None, "probe_productivity_fixture_missing"
            records[name] = dict(row)
    path = config.config_dir / "remote-access.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM cmh_event_receipts WHERE receipt_id=?", (state["receipt"],)
        ).fetchone()
        assert row is not None, "probe_original_fixture_missing"
        records["original"] = dict(row)
    return records


async def confirmed_request(web, config, state, selected):
    body = {
        "scope_id": state["event"]["scope_id"],
        "action": "forget",
        "record_ids": [selected],
    }
    viewed = await web.post(
        "/hub/v1/governance/preview",
        headers={"Authorization": "Bearer " + config.admin_token},
        json={**body, "limit": 50},
    )
    assert viewed.status_code == 200, "productivity_preview_failed"
    identities = {item["record_id"] for item in viewed.json()["items"]}
    assert {"cmhe:" + state["receipt"], state["fact_record"]} <= identities
    assert any(identity.startswith("cmhv:") for identity in identities)
    return {
        **body,
        "expected_view": viewed.json()["view_digest"],
        "operation_id": str(uuid4()),
    }


async def seed_and_refuse(app, bridge, sdk, web, config, state, scene):
    queue = app.core.state.memory_write_queue
    frozen = queue.payload_for(state["receipt"])
    request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
    outputs = await seed_output(sdk, bridge, request, scene, typed=True)
    await app.core.state.cmh_lineage.observe(state["receipt"], frozen)
    queue.settle(state["receipt"], state=COMPLETED, hy_invoked=True)
    queue.clear_terminal_payloads()
    state["experience"], state["fact"] = outputs["memory"]
    state["fact_record"] = record_id(scene[0], "f", state["fact"])
    database = Path(
        _variant_db_path(sdk._config.coding.db_path, "_cv" if scene == "coding" else "")
    )
    state["database"] = str(database.resolve().relative_to(config.data_dir.resolve()))
    original = snapshot(config, state)
    assert original["fact"]["status"] == "active"
    assert json.loads(original["original"]["payload_json"]) == state["event"]
    executor = app.core.state.cmh_executor
    authority = app.core.state.cmh_governance
    checkpoint = authority.journal.head(state["event"]["scope_id"])
    operations = executor.store.operations()
    assert operations == {}, "probe_instance_has_prior_governance"
    admin = {"Authorization": "Bearer " + config.admin_token}

    # Both a direct fact and its original must inspect the complete closure.
    for selected in (state["fact_record"], "cmhe:" + state["receipt"]):
        confirmed = await confirmed_request(web, config, state, selected)
        reply = await web.post("/hub/v1/governance/execute", headers=admin, json=confirmed)
        assert reply.status_code == 503, "unsupported_forget_was_not_refused"
        assert reply.json() == {"detail": REFUSAL}
        assert executor.store.operations() == operations, "refusal_created_plan"
        assert authority.journal.head(state["event"]["scope_id"]) == checkpoint
        assert snapshot(config, state) == original, "refusal_modified_business_records"
        assert executor.ready is True, "refusal_blocked_unrelated_work"
        authority.require_writes_ready()

    # Reproduce the old implementation's actual signed completed operation.
    # Only its missing type guard is bypassed; storage and execution stay real.
    with patch.object(governance_execution, "require_forget_supported", lambda records: None):
        reply = await web.post("/hub/v1/governance/execute", headers=admin, json=confirmed)
    assert reply.status_code == 200 and reply.json()["state"] == "completed"
    operation_id = reply.json()["operation_id"]
    assert executor.store.operations()[operation_id][0] == "completed"
    assert snapshot(config, state) == original, "legacy_fixture_changed_productivity_rows"
    checkpoint = authority.journal.head(state["event"]["scope_id"])
    # The same-process completed retry must not remain a success shortcut.
    retry = await web.post("/hub/v1/governance/execute", headers=admin, json=confirmed)
    assert retry.status_code == 503 and retry.json() == {"detail": REFUSAL}
    assert authority.journal.head(state["event"]["scope_id"]) == checkpoint
    assert snapshot(config, state) == original
    state["operation_id"] = operation_id
    state["preserved"] = original


async def blocked_restart(config, state):
    authority = open_authority(config)
    operations = ExecutionStore(authority.journal).operations()
    checkpoint = authority.journal.head(state["event"]["scope_id"])
    assert operations[state["operation_id"]][0] == "completed"
    assert snapshot(config, state) == state["preserved"]
    app = create_hub(config)
    try:
        async with runtime(app, config):
            raise AssertionError("unsupported_legacy_forget_started_ready")
    except GovernanceError as error:
        assert str(error) == REFUSAL
    assert app.core.state.cmh_executor.ready is False
    assert app.core.state.cmh_executor._replayed is False
    assert ExecutionStore(authority.journal).operations() == operations
    assert authority.journal.head(state["event"]["scope_id"]) == checkpoint
    assert snapshot(config, state) == state["preserved"], "refused_restart_changed_business_records"


async def phase(root, scene, mode):
    os.environ.update(environment(root))
    with patch.object(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder()):
        if mode == "seed":
            await archive_phase(root, "seed")
        state_path = root / "state.json"
        state = json.loads(state_path.read_text())
        config = HubConfig.from_environment()
        if mode == "read":
            await blocked_restart(config, state)
            return
        app = create_hub(config)
        async with runtime(app, config) as (bridge, sdk, web):
            await seed_and_refuse(app, bridge, sdk, web, config, state, scene)
        state_path.write_text(json.dumps(state))


def child(root, scene, mode):
    with (root / f"{mode}.log").open("w") as log:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), scene, mode, str(root)],
            stdout=log,
            stderr=log,
            timeout=180,
        )
    if result.returncode:
        raise RuntimeError("productivity_forget_probe_failed_" + scene + "_" + mode)


def main():
    os.umask(0o077)
    if len(sys.argv) == 4:
        scene, mode = sys.argv[1:3]
        assert scene in SCENES and mode in {"seed", "read"}
        asyncio.run(phase(Path(sys.argv[3]), scene, mode))
        return
    root = Path(mkdtemp(prefix="cmh-productivity-forget-"))
    try:
        for scene in SCENES:
            case = root / scene
            case.mkdir(mode=0o700)
            subprocess.run(
                [sys.executable, "-m", "cloud_memory_hub", "init"],
                env=environment(case),
                check=True,
                capture_output=True,
                timeout=60,
            )
            child(case, scene, "seed")
            child(case, scene, "read")
    except BaseException:
        print("Synthetic probe failure evidence retained at " + str(root), file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)
    print(
        "Installed working/coding forget guards passed across processes: full-scope HTTP refusal "
        "without new intents or data changes, completed-retry refusal, and preserved legacy "
        "completed operations on blocked restart (real SDK stores; controlled writer results "
        "and synthetic embeddings; complete productivity forgetting remains unimplemented)."
    )


if __name__ == "__main__":
    main()
