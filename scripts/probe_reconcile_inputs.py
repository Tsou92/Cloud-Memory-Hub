"""Real installed writer/outcome persistence with synthetic model decisions only."""

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
from hy_memory.coding.reconciler import CodingMemoryReconciler
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, CodingMemoryDraft
from hy_memory.coding.writer import CodingWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from probe_original_archive import environment

MESSAGES = [
    {"role": "user", "content": "Synthetic build fix"},
    {"role": "tool", "content": "Synthetic build result"},
]


async def seed(sdk, variant, request):
    store = CodingMemoryStore(
        sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
    )

    async def initialize():
        await store.initialize()
        await store.insert(
            CodingMemory(
                experience_id="old",
                task="Synthetic build fix",
                solution="before update",
                user_id="scope",
                agent_id="agent",
                session_id="session",
            )
        )

    async def complete(**kwargs):
        assert kwargs["audit_stage"] == "coding_reconcile"
        assert "before update" in kwargs["prompt"]
        return SimpleNamespace(
            content=json.dumps(
                {"ops": [{"action": "UPDATE", "draft_idx": 0, "target_experience_id": "old"}]}
            )
        )

    async def extract(messages, **kwargs):
        return [
            CodingMemoryDraft(
                task="Synthetic build fix",
                solution="after update",
                user_id="scope",
                agent_id="agent",
                session_id="session",
            )
        ], []

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

    previous = sdk._get_coding_writer
    sdk._get_coding_writer = get_writer
    await _joined_thread(lambda: sdk._loop_thread.run(initialize()))
    try:
        result = await _joined_thread(sdk.add, MESSAGES, **request)
        captured = result["reconcile_inputs"]
        assert captured["status"] == "complete" and len(captured["stages"]) == 1
        stage = captured["stages"][0]
        assert stage["status"] == "used" and stage["inputs"][0]["id"] == "old"
        assert stage["inputs"][0]["record"]["solution"] == "before update"
        current = await _joined_thread(
            lambda: sdk._loop_thread.run(store.get_by_id("old", "scope"))
        )
        assert current.solution == "after update"
        return captured
    finally:
        sdk._get_coding_writer = previous
        await _joined_thread(lambda: sdk._loop_thread.run(store.close()))


async def phase(root, mode):
    os.environ.update(environment(root))
    app = create_hub(HubConfig.from_environment())
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        bridge = _sdk_library_bridge(app.core)
        sdk = await bridge._get_client()
        path = root / "reconcile-inputs.json"
        if mode == "seed":
            state = []
            for variant in ("working", "coding"):
                request = {
                    "scene": variant,
                    "user_id": "scope",
                    "agent_id": "agent",
                    "session_id": "session",
                    "request_id": str(uuid4()),
                }
                captured = await seed(sdk, variant, request)
                state.append({"request": request, "captured": captured})
            path.write_text(json.dumps(state))
            return

        async def forbidden_writer(**kwargs):
            raise AssertionError("replay attempted new extraction")

        sdk._get_coding_writer = forbidden_writer
        for item in json.loads(path.read_text()):
            saved = await bridge.get_sdk_write_outcome(item["request"]["request_id"])
            assert saved["result"]["reconcile_inputs"] == item["captured"]
            replay = await _joined_thread(sdk.add, MESSAGES, **item["request"])
            assert replay["reconcile_inputs"] == item["captured"]


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-reconcile-inputs-") as temporary:
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
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError("reconcile_input_probe_failed_" + mode)
    print("Immutable reconcile inputs and exact request replay passed across processes.")


if __name__ == "__main__":
    main()
