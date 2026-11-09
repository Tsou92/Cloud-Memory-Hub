"""Installed forget/purge: real Core, SDK, stores and HTTP; synthetic extraction only."""

import asyncio
import json
import os
import shutil
import sqlite3
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

MARK = "CMH_SYNTHETIC_PURGE_MARK"


def leftover(root, needle):
    # data_level0.bin slack is the documented Chroma gap (ADR-0032).
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "data_level0.bin"
        and needle.encode() in path.read_bytes()
    )


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
            scope = event["scope_id"]
            admin = {"Authorization": "Bearer " + config.admin_token}
            device = {"Authorization": "Bearer " + state["credential"]}

            async def get(kind, identity):
                return (
                    await web.post(
                        f"/hub/v1/{kind}/get",
                        headers=device,
                        json={"scope_id": scope, "record_id": identity},
                    )
                ).status_code

            async def execute(action, selected):
                view = await web.post(
                    "/hub/v1/governance/preview",
                    headers=admin,
                    json={"scope_id": scope, "action": action, "record_ids": [selected]},
                )
                assert view.status_code == 200, "preview_failed"
                reply = await web.post(
                    "/hub/v1/governance/execute",
                    headers=admin,
                    json={
                        "scope_id": scope,
                        "action": action,
                        "record_ids": [selected],
                        "expected_view": view.json()["view_digest"],
                    },
                )
                assert reply.status_code == 200, "execute_failed"

            if mode == "seed":
                await web.put(
                    "/hub/v1/devices/" + event["device_id"] + "/grants",
                    headers=admin,
                    json={
                        "expected_revision": 2,
                        "grants": [
                            {
                                "scope_id": scope,
                                "permissions": ["events:write", "history:read", "memory:read"],
                            }
                        ],
                    },
                )
                rows = []

                async def extract(self, **kwargs):
                    return AgentResult(
                        success=True,
                        extracted_info={"memory": [{"content": f"{MARK} fact {len(rows)}"}]},
                    )

                previous = MemAgent.process_add, native_writer._RECONCILE_ENABLED
                MemAgent.process_add, native_writer._RECONCILE_ENABLED = extract, False
                try:
                    for _ in range(2):
                        current = {
                            **event,
                            **{
                                k: str(uuid4())
                                for k in ("event_id", "record_id", "conversation_id")
                            },
                        }
                        reply = await web.post("/hub/v1/events", headers=device, json=current)
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
                        await lineage.observe(receipt, frozen)
                        queue.settle(receipt, state=COMPLETED)
                        rows.append(
                            {
                                "receipt": receipt,
                                "memory": result["memory_ids"][0],
                                "event": current,
                            }
                        )
                    queue.clear_terminal_payloads()
                finally:
                    MemAgent.process_add, native_writer._RECONCILE_ENABLED = previous
                state["rows"] = rows
                state_path.write_text(json.dumps(state))
                return
            if mode == "govern":
                rows = state["rows"]
                # Forget the first turn, purge the second.
                await execute("forget", "cmhe:" + rows[0]["receipt"])
                await execute("purge", "cmhe:" + rows[1]["receipt"])
                return
            first, second = state["rows"]
            assert await get("memories", first["memory"]) == 404
            assert await get("history", "cmhe:" + first["receipt"]) == 200
            assert await get("memories", second["memory"]) == 404
            assert await get("history", "cmhe:" + second["receipt"]) == 404
            for index, row in enumerate(state["rows"]):
                current = row["event"]
                identity = (
                    f"cmh:{current['instance_id']}:{current['device_id']}:{current['event_id']}"
                )
                outcome = await _joined_thread(sdk.get_write_outcome, identity)
                assert (outcome is None) == (index == 1), "purge_result_cache_mismatch"
            # New revisions must not resurrect either forgotten or purged
            # originals, including after restoring the old business volumes.
            for row in state["rows"]:
                revised = {**row["event"], "event_id": str(uuid4()), "revision": 2}
                reply = await web.post("/hub/v1/events", headers=device, json=revised)
                assert reply.status_code == 409, "source_reimport_was_accepted"
                assert reply.json() == {"detail": "governance_source_suppressed"}
                request_id = (
                    f"cmh:{event['instance_id']}:{event['device_id']}:{revised['event_id']}"
                )
                assert queue.get(request_id) is None, "suppressed_source_was_queued"
            app.core.state.cmh_governance.require_writes_ready()


def run(root, mode):
    with (root / f"{mode}.log").open("w") as log:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
            stdout=log,
            stderr=log,
            timeout=180,
        )
    if result.returncode:
        raise RuntimeError("governance_execution_probe_failed_" + mode)


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "govern", "read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-governance-execution-") as temporary:
        root = Path(temporary) / "instance"
        root.mkdir()
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env=environment(root),
            check=True,
            capture_output=True,
        )
        backup = Path(temporary) / "backup"
        run(root, "seed")
        # Business-data backup before governance; the governance dir is separate.
        for name in ("data", "config"):
            shutil.copytree(root / name, backup / name)
        run(root, "govern")
        run(root, "read")
        assert leftover(root, MARK + " fact 1") == [], "purge_residue"
        assert leftover(root, MARK + " fact 0"), "forget_must_keep_content"
        # Restore only the SDK outcome cache. No raw/vector node is restored,
        # so ID existence alone cannot trigger replay (ADR-0035).
        state = json.loads((root / "state.json").read_text())
        event = state["rows"][1]["event"]
        request_id = f"cmh:{event['instance_id']}:{event['device_id']}:{event['event_id']}"
        caches = list((backup / "data").rglob("cache.sqlite3"))
        assert len(caches) == 1, "cache_location_unproven"
        cache = caches[0]
        current_cache = root / "data" / cache.relative_to(backup / "data")
        with sqlite3.connect(cache) as source, sqlite3.connect(current_cache) as destination:
            source.backup(destination)
            assert destination.execute(
                "SELECT 1 FROM write_outcomes WHERE request_id=?", (request_id,)
            ).fetchone(), "cache_restore_fixture_invalid"
        run(root, "read")
        # Restore pre-purge business data: startup must re-apply the purge.
        for name in ("data", "config"):
            shutil.rmtree(root / name)
            shutil.copytree(backup / name, root / name)
        assert leftover(root, MARK + " fact 1"), "restore_fixture_invalid"
        run(root, "read")
        assert leftover(root, MARK + " fact 1") == [], "restore_replay_residue"
    print(
        "Forget, purge, restart, full/cache-only restore and source reimport prevention "
        "passed across processes."
    )


if __name__ == "__main__":
    main()
