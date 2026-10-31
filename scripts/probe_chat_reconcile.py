"""Installed native reconciliation capture with synthetic extraction/model replies."""

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
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from probe_original_archive import environment

CONTENT = "Synthetic reconciliation fact"


async def phase(root, mode):
    os.environ.update(environment(root))
    app = create_hub(HubConfig.from_environment())
    original_get_llm, original_extract = MemoryReconciler._get_llm, MemAgent.process_add
    prompts, extractions = [], []
    writing = mode in {"seed", "legacy-seed"}

    async def complete(**kwargs):
        assert writing, "replay must not invoke a model"
        prompts.append(kwargs["prompt"])
        return LLMResponse(
            content=json.dumps([{"op": "ADD", "content": CONTENT}]),
            tokens_used=1,
            prompt_tokens=1,
            completion_tokens=0,
        )

    async def extract(self, **kwargs):
        assert writing, "replay must not invoke extraction"
        extractions.append(True)
        return AgentResult(success=True, extracted_info={"memory": [{"content": CONTENT}]})

    MemoryReconciler._get_llm = lambda self: SimpleNamespace(complete=complete)
    MemAgent.process_add = extract
    try:
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            path = root / "chat-reconcile.json"
            if writing:
                state = []
                for index, method in enumerate(("add_extracted", "add", "add_extracted")):
                    request = dict(user_id="scope", agent_id="agent", request_id=str(uuid4()))
                    if method == "add":
                        request["scene"] = "chat"
                    result = await _joined_thread(getattr(sdk, method), CONTENT, **request)
                    assert result["success"]
                    capture = result.get("chat_reconcile_inputs")
                    if mode == "legacy-seed":
                        assert "chat_reconcile_inputs" not in result
                    else:
                        assert capture["status"] == "complete"
                        stage = capture["stages"][0]
                        if index == 0:
                            assert stage["status"] == "skipped" and stage["inputs"] == []
                        else:
                            assert stage["status"] == "used" and stage["inputs"]
                            assert stage["rendered"] == prompts[-1]
                            for item in stage["inputs"]:
                                assert item["kind"] == "memory"
                                assert item["id"] == item["record"]["node_id"]
                                assert item["record"]["content"] == CONTENT
                                assert "embedding" not in item["record"]
                    state.append(dict(method=method, request=request, captured=capture))
                assert len(prompts) == 2 and len(extractions) == 1
                path.write_text(json.dumps(state))
                return
            for item in json.loads(path.read_text()):
                saved = await bridge.get_sdk_write_outcome(item["request"]["request_id"])
                assert saved["result"].get("chat_reconcile_inputs") == item["captured"]
                replay = await _joined_thread(
                    getattr(sdk, item["method"]), CONTENT, **item["request"]
                )
                assert replay.get("chat_reconcile_inputs") == item["captured"]
            assert prompts == extractions == []
    finally:
        MemoryReconciler._get_llm, MemAgent.process_add = original_get_llm, original_extract


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "legacy-seed", "read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-chat-reconcile-") as temporary:
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
                raise RuntimeError("chat_reconcile_probe_failed_" + mode)
    print("Native reconciliation snapshots and exact replay passed across processes.")


if __name__ == "__main__":
    main()
