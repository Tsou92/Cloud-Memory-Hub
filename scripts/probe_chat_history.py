"""Installed chat history capture and replay with a synthetic extraction result."""

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
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from probe_original_archive import environment

MESSAGES = [{"role": "user", "content": "Synthetic history input"}]


async def phase(root, mode):
    os.environ.update(environment(root))
    app = create_hub(HubConfig.from_environment())
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        bridge = _sdk_library_bridge(app.core)
        sdk = await bridge._get_client()
        path = root / "chat-history.json"
        previous = MemAgent.process_add
        seen = []

        async def extract(self, **kwargs):
            assert mode == "seed", "replay must not invoke extraction"
            seen.append(kwargs["history_context"])
            return AgentResult(success=True, extracted_info={"memory": [], "entities": []})

        MemAgent.process_add = extract
        try:
            if mode == "seed":
                state = []
                for index in range(3):
                    request = dict(
                        scene="chat", user_id="scope", agent_id="agent", request_id=str(uuid4())
                    )
                    result = await _joined_thread(sdk.add, MESSAGES, **request)
                    assert result["success"] is True
                    capture = result["chat_history"]
                    assert capture["status"] == "complete" and capture["rendered"] == seen[-1]
                    assert {item["node_id"] for item in capture["records"]} == {
                        item["raw_id"] for item in state
                    }
                    assert len(capture["records"]) == index
                    state.append(
                        dict(request=request, captured=capture, raw_id=result["memory_id"])
                    )
                path.write_text(json.dumps(state))
                return
            for item in json.loads(path.read_text()):
                saved = await bridge.get_sdk_write_outcome(item["request"]["request_id"])
                assert saved["result"]["chat_history"] == item["captured"]
                replay = await _joined_thread(sdk.add, MESSAGES, **item["request"])
                assert replay["chat_history"] == item["captured"]
            assert seen == []
        finally:
            MemAgent.process_add = previous


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-chat-history-") as temporary:
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
                raise RuntimeError("chat_history_probe_failed_" + mode)
    print("Chat history snapshots and exact request replay passed across processes.")


if __name__ == "__main__":
    main()
