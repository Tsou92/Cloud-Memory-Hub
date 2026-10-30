"""Installed Hub input graph across actual SDK updates and process restarts."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.productivity_read import record_id
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.reconciler import CodingMemoryReconciler
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemoryDraft
from hy_memory.coding.writer import CodingWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase


async def produce(sdk, request, variant, previous):
    store = CodingMemoryStore(
        sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
    )
    await _joined_thread(lambda: sdk._loop_thread.run(store.initialize()))

    async def extract(messages, **kwargs):
        return [
            CodingMemoryDraft(
                task="Synthetic graph task",
                solution="Synthetic version " + request.request_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
            )
        ], []

    async def complete(**kwargs):
        assert kwargs["audit_stage"] == "coding_reconcile"
        op = {"draft_idx": 0, "action": "UPDATE" if previous else "ADD"}
        if previous:
            op["target_experience_id"] = previous
        return SimpleNamespace(content=json.dumps({"ops": [op]}))

    provider = SimpleNamespace(complete=complete)
    writer = CodingWriter(
        store,
        SimpleNamespace(extract=extract),
        CodingMemoryReconciler(store, sdk._embed_service, provider),
        provider,
        sdk._embed_service,
    )

    async def get_writer(**kwargs):
        return writer

    original = sdk._get_coding_writer
    sdk._get_coding_writer = get_writer
    try:
        result = await _joined_thread(
            sdk.add,
            [turn.model_dump() for turn in request.turns],
            user_id=request.user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            source=request.source_id,
            request_id=request.request_id,
            scene=variant,
        )
        assert result["success"] and result["productivity_variant"] == variant
        assert len(result["experience_ids"]) == 1 and not result["fact_ids"]
        if previous:
            assert result["experience_ids"] == [previous]
        return result
    finally:
        sdk._get_coding_writer = original
        await _joined_thread(lambda: sdk._loop_thread.run(store.close()))


async def phase(root, mode):
    os.environ.update(environment(root))
    if mode == "seed":
        await archive_phase(root, "seed")
    state = json.loads((root / "state.json").read_text())
    config = HubConfig.from_environment()
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        queue = app.core.state.memory_write_queue
        bridge = _sdk_library_bridge(app.core)
        sdk = await bridge._get_client()
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            headers = {"Authorization": "Bearer " + state["credential"]}
            admin = {"Authorization": "Bearer " + config.admin_token}
            event = state["event"]
            if mode == "seed":
                granted = await web.put(
                    "/hub/v1/devices/" + event["device_id"] + "/grants",
                    headers=admin,
                    json={
                        "expected_revision": 2,
                        "grants": [
                            {
                                "scope_id": event["scope_id"],
                                "permissions": ["events:write", "history:read", "memory:read"],
                            }
                        ],
                    },
                )
                assert granted.status_code == 200
                state["graph"] = []
                for variant in ("working", "coding"):
                    previous, rounds = None, []
                    for _ in range(3):
                        current = {
                            **event,
                            "event_id": str(uuid4()),
                            "record_id": str(uuid4()),
                            "content": {
                                "messages": [
                                    {"role": "user", "text": "Synthetic graph task"},
                                    {"role": "tool", "text": "Synthetic edit result"},
                                ]
                            },
                        }
                        reply = await web.post("/hub/v1/events", headers=headers, json=current)
                        assert reply.status_code == 202
                        job = reply.json()["receipt_id"]
                        frozen = queue.payload_for(job)
                        request = MemoryTaskRunner.request_for(
                            frozen.idempotency_key, frozen.input_records
                        )
                        result = await produce(sdk, request, variant, previous)
                        previous = result["experience_ids"][0]
                        await app.core.state.cmh_lineage.observe(job, frozen)
                        queue.settle(job, state=COMPLETED)
                        rounds.append(
                            {
                                "job": job,
                                "request": request.request_id,
                                "capture": result["reconcile_inputs"],
                            }
                        )
                    state["graph"].append(
                        {"memory": record_id(variant[0], "e", previous), "rounds": rounds}
                    )
                queue.clear_terminal_payloads()
                (root / "state.json").write_text(json.dumps(state))
                return

            def forbidden(*args, **kwargs):
                raise AssertionError("graph read/recovery must not invoke SDK writes")

            sdk.add = sdk.add_extracted = forbidden
            if mode != "old-read":
                # Allows completed old receipts to gain the additive archive without rewriting.
                await app.core.state.cmh_lineage.reconcile_completed()
            for entry in state["graph"]:
                for turn in entry["rounds"]:
                    saved = await bridge.get_sdk_write_outcome(turn["request"])
                    assert saved["result"]["reconcile_inputs"] == turn["capture"]
                    assert queue.payload_for(turn["job"]) is None
                    if mode != "old-read":
                        from cloud_memory_hub.reconcile_evidence import stored_inputs

                        lineage = app.core.state.cmh_lineage
                        archived, frozen = lineage._request(turn["job"], None)
                        request = MemoryTaskRunner.request_for(
                            frozen.idempotency_key, frozen.input_records
                        )
                        with app.core.state.cmh_ingest._database() as db:
                            assert (
                                stored_inputs(db, turn["job"], request)["capture"]
                                == turn["capture"]
                            )
                reply = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": event["scope_id"], "record_id": entry["memory"]},
                )
                assert reply.status_code == 200, reply.text
                assert set(reply.json()["items"][0]["source_raw_ids"]) >= {
                    "cmhe:" + turn["job"] for turn in entry["rounds"]
                }
                if mode == "old-read":
                    continue
                first, second, third = (turn["job"] for turn in entry["rounds"])
                selected = "cmhi:" + second + ":0"
                reply = await web.post(
                    "/hub/v1/governance/preview",
                    headers=admin,
                    json={
                        "scope_id": event["scope_id"],
                        "action": "purge",
                        "record_ids": [selected],
                        "limit": 50,
                    },
                )
                assert reply.status_code == 200, reply.text
                result = reply.json()
                ids = {item["record_id"] for item in result["items"]}
                assert not result["execution_available"]
                assert {
                    "cmhe:" + first,
                    "cmhe:" + second,
                    "cmhi:" + third + ":0",
                    entry["memory"],
                } <= ids
                assert "cmhe:" + third not in ids


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read", "old-read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-reconcile-graph-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env={**os.environ, **environment(root)},
            check=True,
            capture_output=True,
        )
        for index, mode in enumerate(("seed", "read", "read")):
            with (root / f"phase-{index}.log").open("w") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                    stdout=log,
                    stderr=log,
                    timeout=180,
                )
            if result.returncode:
                raise RuntimeError("reconcile_graph_probe_failed_" + mode)
    print("Hub input graph, immutable producer ancestry and HTTP preview passed across processes.")


if __name__ == "__main__":
    main()
