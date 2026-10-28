"""Installed provenance/recovery probe; SDK storage is real, extraction is synthetic."""

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
from cloud_memory_hub.productivity_read import record_id
from httpx import ASGITransport, AsyncClient
from one_memory.api import _sdk_library_bridge
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED, UNKNOWN
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase
from probe_sdk_reconciliation import seed_output


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
            event = state["event"]
            if mode == "seed":
                granted = await web.put(
                    "/hub/v1/devices/" + event["device_id"] + "/grants",
                    headers={"Authorization": "Bearer " + config.admin_token},
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
                state["origins"], state["jobs"] = {}, []
                for scene in ("chat", "working", "coding"):
                    current = (
                        event
                        if scene == "chat"
                        else {**event, "event_id": str(uuid4()), "record_id": str(uuid4())}
                    )
                    reply = await web.post("/hub/v1/events", headers=headers, json=current)
                    assert reply.status_code == 202
                    job = reply.json()["receipt_id"]
                    frozen = queue.payload_for(job)
                    request = MemoryTaskRunner.request_for(
                        frozen.idempotency_key, frozen.input_records
                    )
                    outputs = await seed_output(sdk, bridge, request, scene, typed=True)
                    queue.settle(job, state=UNKNOWN, hy_invoked=True)
                    state["jobs"].append((job, request.request_id))
                    for kind, native in zip(("e", "f"), outputs["memory"], strict=scene != "chat"):
                        identity = native if scene == "chat" else record_id(scene[0], kind, native)
                        state["origins"][identity] = "cmhe:" + job
                (root / "state.json").write_text(json.dumps(state))
                return

            def no_write(*args, **kwargs):
                raise AssertionError("lineage_recovery_invoked_writer")

            sdk.add = sdk.add_extracted = no_write
            await app.core.state.memory_write_drain.reconcile_unknown()
            for _job, request in state["jobs"]:
                assert queue.get(request).state == COMPLETED
            queue.clear_terminal_payloads()
            assert all(queue.payload_for(job) is None for job, _ in state["jobs"])
            assert not sdk._coding_stores
            listed = await web.post(
                "/hub/v1/memories/list",
                headers=headers,
                json={"scope_id": event["scope_id"], "limit": 10},
            )
            assert listed.status_code == 200
            assert {item["record_id"] for item in listed.json()["items"]} == set(state["origins"])
            for identity, original in state["origins"].items():
                response = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": event["scope_id"], "record_id": identity},
                )
                assert response.status_code == 200
                assert original in response.json()["items"][0]["source_raw_ids"]
                content, offset = "", 0
                while offset is not None:
                    response = await web.post(
                        "/hub/v1/history/get",
                        headers=headers,
                        json={
                            "scope_id": event["scope_id"],
                            "record_id": original,
                            "content_offset": offset,
                        },
                    )
                    assert response.status_code == 200
                    item = response.json()["items"][0]
                    content += item["content"]
                    offset = item["next_content_offset"]
                assert json.loads(content)["content"] == event["content"]


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] in {"seed", "read"}:
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    if "--installed" in sys.argv:
        import cloud_memory_hub.output_lineage
        import hy_memory.client

        for module in (cloud_memory_hub.output_lineage, hy_memory.client):
            assert "/site-packages/" in module.__file__
    with TemporaryDirectory(prefix="cmh-output-lineage-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env=environment(root),
            check=True,
            capture_output=True,
            timeout=30,
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
                raise RuntimeError("installed_lineage_probe_failed_" + mode)
    print("Typed provenance, unknown recovery, cleanup and full originals passed across processes.")


if __name__ == "__main__":
    main()
