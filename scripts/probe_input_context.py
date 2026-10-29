"""Installed real-writer history capture; model decisions use synthetic substitutes."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.input_context import stored_context
from cloud_memory_hub.productivity_read import record_id
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, CodingMemoryDraft, ReconcileOp
from hy_memory.coding.writer import CodingWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED, UNKNOWN
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase


async def produce(sdk, request, *, output, variant="working"):
    store = CodingMemoryStore(
        sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
    )
    await _joined_thread(lambda: sdk._loop_thread.run(store.initialize()))
    calls = []

    async def extract(messages, **kwargs):
        calls.append(kwargs["history_messages"])
        return ([CodingMemoryDraft(task="Synthetic draft")] if output else []), []

    async def reconcile(*args, **kwargs):
        identity = str(uuid4())
        await store.insert(
            CodingMemory(
                experience_id=identity,
                task="Synthetic task",
                solution="Synthetic solution",
                user_id=request.user_id,
                agent_id=request.agent_id,
            )
        )
        return [ReconcileOp(action="ADD", draft_idx=0, target_experience_id=identity)]

    writer = CodingWriter(
        store,
        SimpleNamespace(extract=extract),
        SimpleNamespace(reconcile=reconcile),
        SimpleNamespace(),
        sdk._embed_service,
        fact_reconciler=SimpleNamespace(),
    )

    async def get_writer(**kwargs):
        assert kwargs["variant"] == variant
        return writer

    previous = sdk._get_coding_writer
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
        assert result["success"] and result["scene"] == "productivity"
        assert len(calls) == 1 and bool(calls[0]) == bool(result["history_request_ids"])
        return result, store._trace_db_path
    finally:
        sdk._get_coding_writer = previous
        await _joined_thread(lambda: sdk._loop_thread.run(store.close()))


async def verify_chain(app, web, sdk, state, root, mode):
    event = state["event"]
    headers = {"Authorization": "Bearer " + state["credential"]}
    queue = app.core.state.memory_write_queue
    if mode == "chain":
        current = {
            **event,
            "event_id": str(uuid4()),
            "record_id": str(uuid4()),
            "content": {
                "messages": [
                    {"role": "user", "text": "Synthetic code edit"},
                    {
                        "role": "tool",
                        "text": "path='synthetic.py'\nold='''value=1\n'''\nnew='''value=2\n'''",
                    },
                ]
            },
        }
        reply = await web.post("/hub/v1/events", headers=headers, json=current)
        assert reply.status_code == 202
        job = reply.json()["receipt_id"]
        frozen = queue.payload_for(job)
        request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
        result, _ = await produce(sdk, request, output=True, variant="coding")
        assert result["chain_context"]["status"] == "complete"
        assert result["chain_context"]["round_ids"] and not result["history_request_ids"]
        await app.core.state.cmh_lineage.observe(job, frozen)
        queue.settle(job, state=COMPLETED)
        queue.clear_terminal_payloads()
        state["chain_job"], state["chain_rounds"] = job, result["chain_context"]["round_ids"]
        state["chain_memory"] = record_id("c", "e", result["experience_ids"][0])
        (root / "state.json").write_text(json.dumps(state))
    else:
        await app.core.state.cmh_lineage.observe(state["chain_job"], None)
    with app.core.state.cmh_ingest._database() as db:
        context = stored_context(db, state["chain_job"])
        assert list(context.chain_round_ids) == state["chain_rounds"]
    reply = await web.post(
        "/hub/v1/memories/get",
        headers=headers,
        json={"scope_id": event["scope_id"], "record_id": state["chain_memory"]},
    )
    assert reply.status_code == 200
    assert reply.json()["items"][0]["source_raw_ids"] == ["cmhe:" + state["chain_job"]]
    preview = await web.post(
        "/hub/v1/governance/preview",
        headers={"Authorization": "Bearer " + HubConfig.from_environment().admin_token},
        json={
            "scope_id": event["scope_id"],
            "action": "purge",
            "record_ids": [state["chain_memory"]],
        },
    )
    assert preview.status_code == 503
    assert preview.json()["detail"] == "governance_chain_inventory_incomplete"


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
            if mode in {"chain", "chain-read"}:
                await verify_chain(app, web, sdk, state, root, mode)
                return
            event = state["event"]
            headers = {"Authorization": "Bearer " + state["credential"]}
            admin = {"Authorization": "Bearer " + config.admin_token}
            if mode == "seed":
                reply = await web.put(
                    "/hub/v1/devices/" + event["device_id"] + "/grants",
                    headers=admin,
                    json={
                        "expected_revision": 2,
                        "grants": [
                            {
                                "scope_id": event["scope_id"],
                                "permissions": ["events:write", "memory:read", "history:read"],
                            }
                        ],
                    },
                )
                assert reply.status_code == 200
                state["context_jobs"], state["context_requests"] = [], []
                for output in (False, True):
                    current = {
                        **event,
                        "event_id": str(uuid4()),
                        "record_id": str(uuid4()),
                        "content": {
                            "messages": [
                                {"role": "user", "text": "Synthetic task"},
                                {"role": "tool", "text": "Synthetic tool response"},
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
                    result, trace_path = await produce(sdk, request, output=output)
                    assert result["history_request_ids"] == state["context_requests"]
                    state["context_jobs"].append(job)
                    state["context_requests"].append(request.request_id)
                    queue.settle(job, state=UNKNOWN, hy_invoked=True)
                state["context_memory"] = record_id("w", "e", result["experience_ids"][0])
                with sqlite3.connect(trace_path) as db:
                    db.execute("DELETE FROM coding_trace")
                (root / "state.json").write_text(json.dumps(state))
                return

            def no_write(*args, **kwargs):
                raise AssertionError("context_recovery_reinvoked_writer")

            sdk.add = sdk.add_extracted = no_write
            await app.core.state.memory_write_drain.reconcile_unknown()
            for request_id in state["context_requests"]:
                assert queue.get(request_id).state == COMPLETED
            queue.clear_terminal_payloads()
            assert all(queue.payload_for(job) is None for job in state["context_jobs"])
            with app.core.state.cmh_ingest._database() as db:
                for index, job in enumerate(state["context_jobs"]):
                    context = stored_context(db, job)
                    assert context.history_status == "captured"
                    assert context.history_request_ids == tuple(state["context_requests"][:index])
                    assert context.chain_status == "captured" and context.chain_round_ids == ()
            reply = await web.post(
                "/hub/v1/memories/get",
                headers=headers,
                json={"scope_id": event["scope_id"], "record_id": state["context_memory"]},
            )
            assert reply.status_code == 200
            expected = {"cmhe:" + job for job in state["context_jobs"]}
            assert set(reply.json()["items"][0]["source_raw_ids"]) == expected
            preview = await web.post(
                "/hub/v1/governance/preview",
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "forget",
                    "record_ids": [state["context_memory"]],
                    "limit": 50,
                },
            )
            assert preview.status_code == 200, preview.text
            assert not preview.json()["execution_available"]
            assert expected <= {item["record_id"] for item in preview.json()["items"]}


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] in {"seed", "read", "chain", "chain-read"}:
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-input-context-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env=environment(root),
            check=True,
            capture_output=True,
            timeout=30,
        )
        for index, mode in enumerate(("seed", "read", "read", "chain", "chain-read")):
            with (root / f"phase-{index}.log").open("w") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                    stdout=log,
                    stderr=log,
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError("installed_context_probe_failed_" + mode)
    print(
        "Exact history, chain IDs, recovery and incomplete-chain protection "
        "passed across processes."
    )


if __name__ == "__main__":
    main()
