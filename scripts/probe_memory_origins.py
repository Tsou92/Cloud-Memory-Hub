"""Installed semantic origin persistence with synthetic extraction/model responses."""

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
from hy_memory.agent.llm_provider import LLMResponse
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.agent.reconciler import MemoryReconciler
from hy_memory.coding.record_origin import verified_origin
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from probe_original_archive import environment

CONTENT = "Synthetic fact"


async def phase(root, mode):
    os.environ.update(environment(root))
    writing = mode in {"seed", "legacy-seed"}
    calls, extracted, target = [], [], {}
    original = MemoryReconciler._get_llm, MemAgent.process_add

    async def complete(**kwargs):
        assert writing, "replay must not invoke the model"
        calls.append(True)
        return LLMResponse(
            content=json.dumps(
                [{"op": "UPDATE", "memory_id": target["id"], "content": "Synthetic revised fact"}]
            ),
            tokens_used=1,
            prompt_tokens=1,
            completion_tokens=0,
        )

    async def extract(self, **kwargs):
        assert writing, "replay must not invoke extraction"
        extracted.append(True)
        return AgentResult(success=True, extracted_info={"memory": [{"content": CONTENT}]})

    MemoryReconciler._get_llm = lambda self: SimpleNamespace(complete=complete)
    MemAgent.process_add = extract
    try:
        app = create_hub(HubConfig.from_environment())
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            path = root / "memory-origins.json"
            if writing:
                state = []
                for method in ("add", "add_extracted"):
                    request = dict(
                        user_id="synthetic-" + method, agent_id="a", request_id=str(uuid4())
                    )
                    first = await _joined_thread(sdk.add_extracted, CONTENT, **request)
                    assert first["success"]
                    target["id"] = first["memory_id"]
                    first_request = request["request_id"]
                    request["request_id"] = str(uuid4())
                    result = await _joined_thread(getattr(sdk, method), CONTENT, **request)
                    assert result["success"]
                    snapshot = result["chat_reconcile_inputs"]
                    record = snapshot["stages"][0]["inputs"][0]["record"]
                    assert record["content"] == CONTENT
                    expected = None if mode == "legacy-seed" else first_request
                    assert verified_origin("memory", record) == expected
                    state.append(
                        dict(
                            method=method,
                            request=request,
                            node_id=target["id"],
                            capture=snapshot,
                            producer=None if mode == "legacy-seed" else request["request_id"],
                        )
                    )
                assert len(calls) == 2 and len(extracted) == 1
                path.write_text(json.dumps(state))
            else:
                state = json.loads(path.read_text())
                for item in state:
                    saved = await bridge.get_sdk_write_outcome(item["request"]["request_id"])
                    assert saved["result"]["chat_reconcile_inputs"] == item["capture"]
                    replay = await _joined_thread(
                        getattr(sdk, item["method"]), CONTENT, **item["request"]
                    )
                    assert replay["chat_reconcile_inputs"] == item["capture"]
                assert calls == extracted == []
            if mode != "old-read":
                for item in state:
                    current = await _joined_thread(
                        lambda identity=item["node_id"]: sdk._loop_thread.run(
                            sdk._vector_store.get_by_id(identity)
                        )
                    )
                    assert current.content == "Synthetic revised fact"
                    assert verified_origin("memory", current.to_dict()) == item["producer"]
    finally:
        MemoryReconciler._get_llm, MemAgent.process_add = original


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "legacy-seed", "read", "old-read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-memory-origins-") as temporary:
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
                raise RuntimeError("memory_origin_probe_failed_" + mode)
    print("Native memory versions, pre-update snapshots and replay persisted across processes.")


if __name__ == "__main__":
    main()
