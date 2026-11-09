"""Installed ADR-0038 cancellation and queue-only restore, with synthetic events.

Uses real Hub lifespans, HTTP handlers, signed stores, Core queue and SDK stores
in separate processes. No test modules, user history or external models are
used. Only the local embedder and Drain claim-readiness predicates are synthetic;
the latter checks cancellation prevents claim, not the complete supervisor path.
Sibling dependency: probe_original_archive.py (environment only).
"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.events import EventEnvelope
from httpx import ASGITransport, AsyncClient
from hy_memory.client import HyMemoryClient
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import LocalHyEmbedService, _joined_thread
from one_memory.memory_server.durable_queue import (
    CANCELLED,
    PENDING,
    REAPED_PAYLOAD_MARKER,
    DurableWriteDrain,
)
from probe_original_archive import environment

TEXTS = {
    "forget": (
        "Synthetic orchard map decision remains available only through explicit history reads."
    ),
    "purge": (
        "Synthetic warehouse route decision must disappear from restored queue storage forever."
    ),
}
PHASES = ("seed", "govern", "recover", "read")


class SyntheticEmbedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


def queue_rows(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return {row["job_id"]: dict(row) for row in db.execute("SELECT * FROM memory_write_jobs")}


def admission_snapshot(app, entries):
    admissions = app.core.state.cmh_generation.admissions
    values = {}
    with admissions.access._connection(writing=True) as db:
        for action, entry in entries.items():
            proof = admissions.proof_in(db, entry["request_id"])
            assert proof.state == "enrolled" and proof.checkpoint.sequence == 1, (
                "probe_birth_was_permitted"
            )
            values[action] = {
                "binding": proof.binding.model_dump(mode="json"),
                "checkpoint": proof.checkpoint.model_dump(mode="json"),
                "state": proof.state,
            }
    assert len(admissions.journal.scopes()) == 2, "probe_birth_count_changed"
    return values


def governance_snapshot(app, scope):
    authority = app.core.state.cmh_governance
    operations = app.core.state.cmh_executor.store.operations()
    return {
        "head": authority.journal.head(scope).model_dump(mode="json"),
        "operations": {
            identity: {"state": state, "intent": intent.model_dump(mode="json")}
            for identity, (state, intent) in operations.items()
        },
    }


async def seed(app, web, config):
    admin = {"Authorization": "Bearer " + config.admin_token}
    project = await web.post(
        "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic inflight probe"}
    )
    assert project.status_code == 201, "probe_project_failed"
    scope = project.json()["project_id"]
    issued = await web.post(
        "/hub/v1/devices", headers=admin, json={"display_name": "Synthetic probe device"}
    )
    assert issued.status_code == 201, "probe_device_failed"
    credential = issued.json()["credential"]
    device_id = issued.json()["device"]["device_id"]
    headers = {"Authorization": "Bearer " + credential}
    granted = await web.put(
        f"/hub/v1/devices/{device_id}/grants",
        headers=admin,
        json={
            "expected_revision": 1,
            "grants": [{"scope_id": scope, "permissions": ["events:write", "history:read"]}],
        },
    )
    assert granted.status_code == 200, "probe_grants_failed"
    source = await web.post(
        "/hub/v1/sources",
        headers=admin,
        json={
            "display_name": "Synthetic inflight source",
            "adapter_id": "synthetic.inflight",
            "device_id": device_id,
            "scope_id": scope,
        },
    )
    assert source.status_code == 201, "probe_source_failed"
    session = await web.get("/hub/v1/session", headers=headers)
    assert session.status_code == 200, "probe_session_failed"
    state = {"credential": credential, "scope": scope, "entries": {}, "processes": {}}
    for action, text in TEXTS.items():
        event = EventEnvelope.model_validate(
            {
                "instance_id": session.json()["instance_id"],
                "device_id": device_id,
                "source_id": source.json()["source_id"],
                "scope_id": scope,
                **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
                "revision": 1,
                "kind": "conversation_turn",
                "content": {"messages": [{"role": "user", "text": text}]},
            }
        )
        value = json.loads(event.canonical_payload())
        response = await web.post("/hub/v1/events", headers=headers, json=value)
        assert response.status_code == 202, "probe_intake_failed"
        job = response.json()["receipt_id"]
        row = queue_rows(config.data_dir / "one-memory.sqlite3")[job]
        assert (row["state"], row["hy_invoked"]) == (PENDING, 0), "probe_job_not_uninvoked"
        assert row["idempotency_key"].startswith("cmh2:"), "probe_missing_birth"
        state["entries"][action] = {
            "event": value,
            "job": job,
            "request_id": row["idempotency_key"],
            "input_digest": sha256(row["payload_json"].encode()).hexdigest(),
            "hy_invoked": row["hy_invoked"],
        }
    assert len(queue_rows(config.data_dir / "one-memory.sqlite3")) == 2
    state["admissions"] = admission_snapshot(app, state["entries"])
    return state


async def govern(app, web, config, state):
    admin = {"Authorization": "Bearer " + config.admin_token}
    for action, entry in state["entries"].items():
        body = {
            "scope_id": state["scope"],
            "action": action,
            "record_ids": ["cmhe:" + entry["job"]],
        }
        view = await web.post("/hub/v1/governance/preview", headers=admin, json=body)
        assert view.status_code == 200, "probe_preview_failed"
        assert {item["record_id"] for item in view.json()["items"]} == set(body["record_ids"])
        operation_id = str(uuid4())
        response = await web.post(
            "/hub/v1/governance/execute",
            headers=admin,
            json={
                **body,
                "expected_view": view.json()["view_digest"],
                "operation_id": operation_id,
            },
        )
        assert response.status_code == 200, "probe_governance_not_successful"
        assert response.json()["state"] == "completed", "probe_governance_not_completed"
        assert response.json()["operation_id"] == operation_id
        entry["operation_id"] = operation_id
        if action == "forget":
            other = state["entries"]["purge"]
            row = queue_rows(config.data_dir / "one-memory.sqlite3")[other["job"]]
            assert row["state"] == PENDING, "probe_unselected_job_was_cancelled"
            assert sha256(row["payload_json"].encode()).hexdigest() == other["input_digest"]
    state["governance"] = governance_snapshot(app, state["scope"])
    assert state["governance"]["head"]["sequence"] == 2
    assert len(state["governance"]["operations"]) == 2


async def verify(app, sdk, web, config, state):
    rows = queue_rows(config.data_dir / "one-memory.sqlite3")
    assert set(rows) == {entry["job"] for entry in state["entries"].values()}
    headers = {"Authorization": "Bearer " + state["credential"]}
    for action, entry in state["entries"].items():
        row = rows[entry["job"]]
        assert row["idempotency_key"] == entry["request_id"]
        assert (row["state"], row["hy_invoked"]) == (CANCELLED, entry["hy_invoked"]), (
            "probe_target_not_cancelled_without_resetting_marker"
        )
        if action == "purge":
            assert json.loads(row["payload_json"], object_pairs_hook=list) == [
                (REAPED_PAYLOAD_MARKER, entry["input_digest"])
            ], "probe_payload_not_strictly_reaped"
            assert app.core.state.memory_write_queue.payload_for(entry["job"]) is None
        content, offset = "", 0
        while offset is not None:
            response = await web.post(
                "/hub/v1/history/get",
                headers=headers,
                json={
                    "scope_id": state["scope"],
                    "record_id": "cmhe:" + entry["job"],
                    "content_offset": offset,
                },
            )
            if action == "purge":
                assert response.status_code == 404, "probe_purged_original_readable"
                break
            assert response.status_code == 200, "probe_forgotten_original_unreadable"
            items = response.json()["items"]
            assert len(items) == 1
            content += items[0]["content"]
            next_offset = items[0]["next_content_offset"]
            assert next_offset is None or next_offset > offset, "probe_read_cursor_did_not_advance"
            offset = next_offset
        if action == "forget":
            assert json.loads(content) == entry["event"], "probe_original_changed"
        assert await _joined_thread(sdk.get_write_outcome, entry["request_id"]) is None
    assert admission_snapshot(app, state["entries"]) == state["admissions"]
    assert governance_snapshot(app, state["scope"]) == state["governance"], (
        "probe_recovery_changed_signed_operations"
    )
    assert all(item["state"] == "completed" for item in state["governance"]["operations"].values())
    # This is the real Core claim/lease/execute loop. Synthetic readiness only
    # ensures missing model profiles cannot make a pending-row regression pass.
    drain = DurableWriteDrain(
        app.core.state.memory_write_queue,
        execute=app.core.state.memory_write_drain.execute,
        source_writes_permitted=lambda source: True,
        source_claim_enabled=lambda source: True,
    )
    assert await drain.run_one(limit=8) == 0, "probe_cancelled_queue_was_claimed"


async def phase(root, mode):
    os.environ.update(environment(root))
    config = HubConfig.from_environment()
    config.data_dir.mkdir(mode=0o700, exist_ok=True)
    state_path = root / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    calls = []

    def no_generate(*args, **kwargs):
        calls.append(kwargs.get("request_id"))
        raise AssertionError("probe_unexpected_sdk_generation")

    # Installed before lifespan: catches startup calls as well as explicit Drain
    # probes and guarantees a regression cannot spend external model credits.
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
            sdk = await _sdk_library_bridge(app.core)._get_client()
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                if mode == "seed":
                    state = await seed(app, web, config)
                else:
                    if mode == "govern":
                        await govern(app, web, config, state)
                    # recover/read rely on production lifespan replay, never a
                    # manual executor.resume or test-side queue cancellation.
                    await verify(app, sdk, web, config, state)
            event = state["entries"]["forget"]["event"]
            nodes = await _joined_thread(
                lambda: sdk._loop_thread.run(
                    sdk._vector_store.list_by_user(
                        user_id=f"cmh:{event['instance_id']}:{state['scope']}",
                        limit=10,
                        with_vectors=False,
                        allow_raw_layer=True,
                    )
                )
            )
            assert nodes == [], "probe_unexpected_native_sdk_records"
            assert not sdk._coding_stores, "probe_unexpected_productivity_writer"
        assert calls == [], "probe_sdk_generation_was_attempted"
    state["processes"][mode] = os.getpid()
    state_path.write_text(json.dumps(state))


def physical_check(root, *, restored):
    path = root / "data" / "one-memory.sqlite3"
    files = (path, path.with_name(path.name + "-wal"))
    found = any(TEXTS["purge"].encode() in item.read_bytes() for item in files if item.exists())
    assert found == restored, "probe_core_purge_plaintext_residue"


def retained_files(root):
    return {
        str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
        for directory in (root / "config", root / "governance")
        for path in directory.rglob("*")
        if path.is_file()
    }


def invoke(root, mode, args):
    with (root / f"{mode}.log").open("w") as log:
        result = subprocess.run(args, env=environment(root), stdout=log, stderr=log, timeout=120)
    assert result.returncode == 0, "inflight_probe_subprocess_failed_" + mode


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] in PHASES:
        asyncio.run(phase(Path(sys.argv[2]).resolve(), sys.argv[1]))
        return
    assert sys.argv[1:] in ([], ["--installed"]), "probe_arguments_invalid"
    installed = "--installed" in sys.argv
    if installed:
        from cloud_memory_hub import governance_inflight
        from one_memory.memory_server import durable_queue

        for module in (governance_inflight, durable_queue, sys.modules[HyMemoryClient.__module__]):
            assert "/site-packages/" in module.__file__, "probe_requires_installed_packages"
    root = Path(mkdtemp(prefix="cmh-inflight-governance-")).resolve()
    try:
        invoke(root, "init", [sys.executable, "-m", "cloud_memory_hub", "init"])
        script = str(Path(__file__).resolve())
        invoke(root, "seed", [sys.executable, script, "seed", str(root)])
        queue_path = root / "data" / "one-memory.sqlite3"
        backup_path = root / "old-core.sqlite3"
        with sqlite3.connect(queue_path) as source, sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        invoke(root, "govern", [sys.executable, script, "govern", str(root)])
        physical_check(root, restored=False)
        retained = retained_files(root)
        with sqlite3.connect(backup_path) as backup, sqlite3.connect(queue_path) as target:
            backup.backup(target)
        assert retained_files(root) == retained, "probe_restore_modified_retained_authority"
        state = json.loads((root / "state.json").read_text())
        restored_rows = queue_rows(queue_path)
        for entry in state["entries"].values():
            row = restored_rows[entry["job"]]
            assert (row["state"], row["hy_invoked"]) == (PENDING, entry["hy_invoked"])
            assert sha256(row["payload_json"].encode()).hexdigest() == entry["input_digest"]
        physical_check(root, restored=True)
        for mode in ("recover", "read"):
            invoke(root, mode, [sys.executable, script, mode, str(root)])
            physical_check(root, restored=False)
        state = json.loads((root / "state.json").read_text())
        assert set(state["processes"]) == set(PHASES)
        assert len(set(state["processes"].values())) == len(PHASES)
    except BaseException:
        print("Synthetic probe failure evidence retained at " + str(root), file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)
    print(
        json.dumps(
            {
                "installed_packages": installed,
                "independent_processes": len(PHASES),
                "http_governance": "forget_and_purge_completed",
                "queue_only_restore": "cancelled_before_claim",
                "signed_operations_and_births": "unchanged",
                "purge_core_plaintext": "absent_after_shutdown",
                "forget_original": "fully_readable",
                "sdk_generation_calls": 0,
                "external_models": False,
                "synthetic_seams": ["embedding", "drain_claim_readiness"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
