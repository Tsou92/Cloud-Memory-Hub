"""Installed scoped profile provenance; synthetic extractor, actual SDK/Core stores."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from httpx import ASGITransport, AsyncClient
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.pipelines.writer import MemoryWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase


async def phase(root, mode):
    os.environ.update(environment(root))
    writing = mode in {"seed", "legacy-seed"}
    if writing:
        await archive_phase(root, "seed")
    path = root / "state.json"
    state = json.loads(path.read_text())
    config = HubConfig.from_environment()
    original = MemAgent.process_add, MemoryWriter.write

    async def extract(self, **kwargs):
        assert writing, "replay must not invoke extraction"
        return AgentResult(
            success=True,
            extracted_info={
                "basic_info": {"name": "Synthetic", "age": 30 if not state["profiles"] else 31},
                "memory": [],
            },
        )

    async def no_write(*args, **kwargs):
        raise AssertionError("profile backfill must not invoke writer")

    MemAgent.process_add = extract
    if not writing:
        MemoryWriter.write = no_write
    try:
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            sdk = await _sdk_library_bridge(app.core)._get_client()
            queue, lineage = app.core.state.memory_write_queue, app.core.state.cmh_lineage
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                event = state["event"]
                admin = {"Authorization": "Bearer " + config.admin_token}
                headers = {"Authorization": "Bearer " + state["credential"]}
                if writing:
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
                    state["profiles"] = []
                    for index in range(3):
                        source = await web.post(
                            "/hub/v1/sources",
                            headers=admin,
                            json={
                                "display_name": f"Synthetic profile agent {index}",
                                "adapter_id": f"synthetic.profile{index}",
                                "device_id": event["device_id"],
                                "scope_id": event["scope_id"],
                            },
                        )
                        assert source.status_code == 201
                        current = {
                            **event,
                            "source_id": source.json()["source_id"],
                            **{
                                key: str(uuid4())
                                for key in ("event_id", "record_id", "conversation_id")
                            },
                        }
                        response = await web.post("/hub/v1/events", headers=headers, json=current)
                        assert response.status_code == 202
                        receipt = response.json()["receipt_id"]
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
                        assert result["success"] and not result.get("memory_ids")
                        await lineage.observe(receipt, frozen)
                        queue.settle(receipt, state=COMPLETED)
                        state["profiles"].append(
                            {
                                "receipt": receipt,
                                "request_id": request.request_id,
                                "user_id": request.user_id,
                                "capture": result["basic_profile_inputs"],
                            }
                        )
                    queue.clear_terminal_payloads()
                    path.write_text(json.dumps(state))
                else:
                    await lineage.reconcile_completed()
                if mode in {"seed", "read"}:
                    from cloud_memory_hub.profile_evidence import stored_profile

                    with app.core.state.cmh_ingest._database() as db:
                        for item in state["profiles"]:
                            assert (
                                stored_profile(db, item["receipt"], None)["capture"]
                                == item["capture"]
                            )
                        assert (
                            db.execute("SELECT count(*) FROM cmh_event_profile_outputs").fetchone()[
                                0
                            ]
                            == 2
                        )
                first, second, third = state["profiles"]
                target = second["capture"]["output_ids"][0]
                reply = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": event["scope_id"], "record_id": target},
                )
                assert reply.status_code == 200, "profile_scoped_read_failed"
                if mode in {"seed", "read"}:
                    refs = reply.json()["items"][0]["source_raw_ids"]
                    assert {"cmhe:" + first["receipt"], "cmhe:" + second["receipt"]} <= set(refs)
                    assert "cmhe:" + third["receipt"] not in refs
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
                    assert reply.status_code == 200, "profile_governance_preview_failed"
                    assert reply.json()["execution_available"] is False
                    selected = {item["record_id"] for item in reply.json()["items"]}
                    assert {"cmhb:" + item["receipt"] for item in state["profiles"]} <= selected
                    assert {target, first["capture"]["output_ids"][0]} <= selected
                    assert "cmhe:" + second["receipt"] not in selected
                denied = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": "global", "record_id": target},
                )
                assert denied.status_code in (403, 404)
    finally:
        MemAgent.process_add, MemoryWriter.write = original


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read", "old-read", "legacy-seed"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-profile-graph-") as temporary:
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
                raise RuntimeError("profile_graph_probe_failed_" + mode)
    print("Scoped profile ancestry and durable backfill persisted across processes.")


if __name__ == "__main__":
    main()
