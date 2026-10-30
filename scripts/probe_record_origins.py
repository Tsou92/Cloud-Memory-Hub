"""Installed same-ID writer origins, durable snapshots and old-writer compatibility."""

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
from hy_memory.coding.store import CodingMemoryStore
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from probe_original_archive import environment
from probe_reconcile_inputs import MESSAGES, seed


async def phase(root, mode):
    os.environ.update(environment(root))
    app = create_hub(HubConfig.from_environment())
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        bridge = _sdk_library_bridge(app.core)
        sdk = await bridge._get_client()
        path = root / "record-origins.json"
        if mode == "seed":
            state = []
            for variant in ("working", "coding"):
                rounds = []
                for index in range(3):
                    request = {
                        "scene": variant,
                        "user_id": "scope",
                        "agent_id": "agent",
                        "session_id": "session",
                        "request_id": str(uuid4()),
                    }
                    captured = await seed(
                        sdk,
                        variant,
                        request,
                        create=index == 0,
                        previous_solution=f"version-{index - 1}" if index else "before update",
                        solution=f"version-{index}",
                    )
                    rounds.append({"request": request, "captured": captured})
                state.append({"variant": variant, "rounds": rounds})
            path.write_text(json.dumps(state))
            return

        async def forbidden_writer(**kwargs):
            raise AssertionError("read/replay must not invoke a writer")

        sdk._get_coding_writer = forbidden_writer
        # Old-image phases deliberately do not import the new helper.
        if not mode.startswith("old-"):
            from hy_memory.coding.record_origin import verified_origin

        for entry in json.loads(path.read_text()):
            previous = None
            for item in entry["rounds"]:
                request = item["request"]
                saved = await bridge.get_sdk_write_outcome(request["request_id"])
                assert saved["result"]["reconcile_inputs"] == item["captured"]
                replay = await _joined_thread(sdk.add, MESSAGES, **request)
                assert replay["reconcile_inputs"] == item["captured"]
                if not mode.startswith("old-"):
                    before = item["captured"]["stages"][0]["inputs"][0]["record"]
                    assert verified_origin("experience", before) == previous
                previous = request["request_id"]

            async def inspect(entry=entry, previous=previous):
                store = CodingMemoryStore(
                    sdk._config,
                    sdk._embed_service,
                    db_path=sdk._config.coding.db_path,
                    variant=entry["variant"],
                )
                await store.initialize()
                try:
                    current = await store.get_by_id("old", "scope")
                    if mode == "old-edit":
                        assert current.solution == "version-2"
                        current.solution = "older writer changed the body"
                        await store.update(current)
                    elif mode == "stale-read":
                        assert current.solution == "older writer changed the body"
                        try:
                            verified_origin("experience", current.to_dict())
                        except ValueError as exc:
                            assert str(exc) == "productivity_origin_invalid"
                        else:
                            raise AssertionError("stale origin accepted after old-image edit")
                    else:
                        assert current.solution == "version-2"
                        if mode == "read":
                            assert verified_origin("experience", current.to_dict()) == previous
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(inspect()))


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read", "old-read", "old-edit", "stale-read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-record-origins-") as temporary:
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
                raise RuntimeError("record_origin_probe_failed_" + mode)
    print("Three same-ID writes preserve immutable producer identities across processes.")


if __name__ == "__main__":
    main()
