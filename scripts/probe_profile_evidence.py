"""Synthetic native profile-only writes, persisted outcomes and no-write replay."""

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
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.coding.record_origin import verified_origin
from hy_memory.pipelines.writer import MemoryWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from probe_original_archive import environment


async def phase(root, mode):
    os.environ.update(environment(root))
    writing = mode == "seed"
    calls, incoming = [], {}
    original = MemAgent.process_add, MemoryWriter.write, MemoryWriter.write_extracted

    async def extract(self, **kwargs):
        assert writing, "replay must not invoke extraction"
        calls.append(True)
        return AgentResult(
            success=True, extracted_info={"basic_info": dict(incoming), "memory": []}
        )

    async def reject_write(*args, **kwargs):
        raise AssertionError("durable replay must not invoke the writer")

    MemAgent.process_add = extract
    if not writing:
        MemoryWriter.write = MemoryWriter.write_extracted = reject_write
    try:
        app = create_hub(HubConfig.from_environment())
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            path = root / "profile-evidence.json"
            if writing:
                state = []
                for index, age in enumerate((30, 31, 31)):
                    incoming.update(name="Synthetic", age=age)
                    request = dict(
                        user_id="synthetic-profile",
                        agent_id=f"agent-{index}",
                        session_id=f"session-{index}",
                        request_id=str(uuid4()),
                    )
                    result = await _joined_thread(sdk.add, "Synthetic profile update", **request)
                    assert result["success"]
                    capture = result["basic_profile_inputs"]
                    assert capture["status"] == "complete" and capture["effects_complete"]
                    assert len(capture["output_ids"]) == int(index < 2)
                    assert len(capture["stages"][0]["inputs"]) == index
                    if index == 1:
                        prior = capture["stages"][0]["inputs"][0]["record"]
                        assert prior["is_latest"] is True
                        assert (
                            verified_origin("profile", prior) == state[0]["request"]["request_id"]
                        )
                        assert capture["superseded_ids"] == state[0]["capture"]["output_ids"]
                    # User-level L0 IDs must not masquerade as ordinary agent memory IDs.
                    assert not set(capture["output_ids"]) & set(result.get("memory_ids", []))
                    state.append(dict(method="add", request=request, capture=capture))
                request = dict(user_id="synthetic-fact", agent_id="a", request_id=str(uuid4()))
                result = await _joined_thread(
                    sdk.add_extracted, "Synthetic independent fact", **request
                )
                assert result["success"] and result["basic_profile_inputs"]["status"] == "not_used"
                state.append(
                    dict(
                        method="add_extracted",
                        request=request,
                        capture=result["basic_profile_inputs"],
                    )
                )
                assert len(calls) == 3
                path.write_text(json.dumps(state))
            else:
                state = json.loads(path.read_text())
                for item in state:
                    saved = await bridge.get_sdk_write_outcome(item["request"]["request_id"])
                    assert saved["result"]["basic_profile_inputs"] == item["capture"]
                    content = (
                        "Synthetic profile update"
                        if item["method"] == "add"
                        else "Synthetic independent fact"
                    )
                    result = await _joined_thread(
                        getattr(sdk, item["method"]), content, **item["request"]
                    )
                    assert result["basic_profile_inputs"] == item["capture"]
                assert calls == []
            for index, item in enumerate(state[:2]):
                identity = item["capture"]["output_ids"][0]
                node = await _joined_thread(
                    lambda identity=identity: sdk._loop_thread.run(
                        sdk._vector_store.get_by_id(identity)
                    )
                )
                assert node.agent_id in {"", None} and node.session_id in {"", None}
                assert node.is_latest is (index == 1)
                if mode != "old-read":
                    assert (
                        verified_origin("profile", node.to_dict()) == item["request"]["request_id"]
                    )
                assert node.custom["basic_info_kv"] == (
                    {"name": "Synthetic", "age": 30} if index == 0 else {"age": 31}
                )
    finally:
        MemAgent.process_add, MemoryWriter.write, MemoryWriter.write_extracted = original


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read", "old-read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-profile-evidence-") as temporary:
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
                raise RuntimeError("profile_evidence_probe_failed_" + mode)
    print("Native profile-only writes, snapshots and no-write replay persisted across processes.")


if __name__ == "__main__":
    main()
