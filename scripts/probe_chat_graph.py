"""Installed Hub history provenance; synthetic extraction, real storage and HTTP."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import hy_memory.pipelines.writer as native_writer
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from httpx import ASGITransport, AsyncClient
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase


async def phase(root, mode):
    os.environ.update(environment(root))
    if mode == "seed":
        await archive_phase(root, "seed")
    state_path = root / "state.json"
    state = json.loads(state_path.read_text())
    config = HubConfig.from_environment()
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        sdk = await _sdk_library_bridge(app.core)._get_client()
        queue, lineage = app.core.state.memory_write_queue, app.core.state.cmh_lineage
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            event = state["event"]
            admin = {"Authorization": "Bearer " + config.admin_token}
            headers = {"Authorization": "Bearer " + state["credential"]}
            if mode == "seed":
                reply = await web.put(
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
                assert reply.status_code == 200
                state["chat"] = []

                async def extract(self, **kwargs):
                    return AgentResult(
                        success=True,
                        extracted_info={
                            "memory": [{"content": "Synthetic fact " + str(len(state["chat"]))}]
                        },
                    )

                previous, reconcile = MemAgent.process_add, native_writer._RECONCILE_ENABLED
                MemAgent.process_add, native_writer._RECONCILE_ENABLED = extract, False
                try:
                    for index in range(3):
                        current = (
                            event
                            if index == 0
                            else {
                                **event,
                                **{
                                    key: str(uuid4())
                                    for key in ("event_id", "record_id", "conversation_id")
                                },
                            }
                        )
                        reply = await web.post("/hub/v1/events", headers=headers, json=current)
                        assert reply.status_code == 202
                        receipt = reply.json()["receipt_id"]
                        frozen = queue.payload_for(receipt)
                        request = MemoryTaskRunner.request_for(
                            frozen.idempotency_key, frozen.input_records
                        )
                        result = await _joined_thread(
                            sdk.add,
                            [turn.model_dump() for turn in request.turns],
                            scene="chat",
                            user_id=request.user_id,
                            agent_id=request.agent_id,
                            session_id=request.session_id,
                            source=request.source_id,
                            request_id=request.request_id,
                        )
                        assert result["success"] and len(result["memory_ids"]) == 1
                        assert len(result["chat_history"]["records"]) == index
                        await lineage.observe(receipt, frozen)
                        queue.settle(receipt, state=COMPLETED)
                        state["chat"].append(
                            {
                                "receipt": receipt,
                                "raw": result["memory_id"],
                                "memory": result["memory_ids"][0],
                            }
                        )
                    assert queue.clear_terminal_payloads() == 3
                    state_path.write_text(json.dumps(state))
                finally:
                    MemAgent.process_add, native_writer._RECONCILE_ENABLED = previous, reconcile
                return

            def forbidden(*args, **kwargs):
                raise AssertionError("read or recovery must not invoke writer")

            sdk.add = forbidden
            if mode == "read":
                # New readers can recover an older Hub's already-completed outcomes.
                await lineage.reconcile_completed()
            first, second, third = state["chat"]
            for item in state["chat"]:
                reply = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": event["scope_id"], "record_id": item["memory"]},
                )
                assert reply.status_code == 200, "chat_graph_read_failed"
                references = reply.json()["items"][0]["source_raw_ids"]
                assert "cmhe:" + item["receipt"] in references
                if mode == "read":
                    assert "cmhe:" + first["receipt"] in references
            reply = await web.post(
                "/hub/v1/governance/preview",
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "purge",
                    "limit": 50,
                    "record_ids": ["cmhe:" + first["receipt"]],
                },
            )
            assert reply.status_code == 200, "chat_graph_preview_failed"
            assert reply.json()["execution_available"] is False
            selected = {item["record_id"] for item in reply.json()["items"]}
            for item in (second, third):
                assert "cmhe:" + item["receipt"] not in selected and item["raw"] not in selected
                if mode == "read":
                    assert "cmhj:" + item["receipt"] in selected and item["memory"] in selected


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read", "old-read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-chat-graph-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env=environment(root),
            check=True,
            capture_output=True,
        )
        for index, mode in enumerate(("seed", "read", "read")):
            with (root / f"phase-{index}.log").open("w") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                    stdout=log,
                    stderr=log,
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError("chat_graph_probe_failed_" + mode)
    print("Chat history source proof, scoped reads and preview direction passed across processes.")


if __name__ == "__main__":
    main()
