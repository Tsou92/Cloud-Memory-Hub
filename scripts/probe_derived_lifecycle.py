"""Real SDK outcomes and Hub admission, with synthetic extraction/derived writes."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import hy_memory.agent.episode_summary as episode
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.models.memory import MemoryLayer, MemoryNode
from hy_memory.pipelines.writer import MemoryWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HyMemoryIngestRequest, HyMemoryTurn, _joined_thread
from probe_original_archive import environment

CONTENT = "Synthetic lifecycle fact"


async def phase(root, mode):
    os.environ.update(environment(root))
    original = MemAgent.process_add, episode.summarize_closed_episode
    controls, rows = {}, []
    path = root / "derived-lifecycle.json"

    async def extract(self, **kwargs):
        assert mode == "seed", "replay must not invoke extraction"
        return AgentResult(success=True, extracted_info={"memory": [{"content": CONTENT}]})

    async def summarize(**kwargs):
        assert mode == "seed", "replay must not schedule derived writes"
        entered, release, completed, node_id = controls[kwargs["episode_id"]]
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        vector = await kwargs["embed_service"].embed_queued("Synthetic derived summary")
        saved = await kwargs["vector_store"].upsert(
            MemoryNode(
                node_id=node_id,
                user_id=kwargs["user_id"],
                agent_id=kwargs["agent_id"],
                session_id=kwargs["session_id"],
                layer=MemoryLayer.L3_SUMMARY,
                content="Synthetic derived summary",
                embedding=vector,
                episode_id=kwargs["episode_id"],
            )
        )
        assert saved == node_id
        completed.set()
        return saved

    MemAgent.process_add = extract
    episode.summarize_closed_episode = summarize
    try:
        app = create_hub(HubConfig.from_environment())
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            assert hasattr(MemoryWriter, "_await_derived_tasks")
            gate = app.core.state.cmh_sdk_admission
            if mode == "seed":
                for method, cancelled in (("add", False), ("add_extracted", True)):
                    item = HyMemoryIngestRequest(
                        request_id=str(uuid4()),
                        user_id="synthetic-" + method,
                        agent_id="synthetic-agent",
                        session_id="synthetic-session",
                        source_id="synthetic-source",
                        turns=(HyMemoryTurn(role="user", content=CONTENT),),
                        evidence_ids=("synthetic-evidence",),
                    )
                    episode_id = "ep_synthetic_" + method
                    entered, release, completed = (threading.Event() for _ in range(3))
                    node_id = str(uuid4())
                    controls[episode_id] = entered, release, completed, node_id

                    async def prepare(item, episode_id):
                        sdk._config.recall.episode_summary_enabled = True
                        await sdk._cache.upsert_episode_state(
                            "::".join((item.user_id, item.agent_id, item.session_id)),
                            user_id=item.user_id,
                            agent_id=item.agent_id,
                            session_id=item.session_id,
                            episode_id=episode_id,
                            last_write_ts=1,
                            memory_count=1000,
                        )

                    await _joined_thread(sdk._loop_thread.run, prepare(item, episode_id))
                    job = asyncio.create_task(
                        bridge.ingest(item)
                        if method == "add"
                        else bridge.ingest_extracted(item, [CONTENT])
                    )
                    maintenance_entered = asyncio.Event()

                    async def maintenance(completed, maintenance_entered):
                        async with gate.exclusive():
                            assert completed.is_set(), "maintenance preceded derived completion"
                            maintenance_entered.set()

                    waiting = None
                    try:
                        assert await asyncio.to_thread(entered.wait, 30), "summary not started"
                        waiting = asyncio.create_task(maintenance(completed, maintenance_entered))
                        for _ in range(3):
                            if cancelled:
                                job.cancel()
                            await asyncio.sleep(0.02)
                            assert not job.done() and not maintenance_entered.is_set()
                        outcome = await _joined_thread(sdk.get_write_outcome, item.request_id)
                        assert outcome["status"] == "running", "outcome completed before summary"
                    finally:
                        release.set()
                        if cancelled:
                            try:
                                await job
                            except asyncio.CancelledError:
                                pass
                            else:
                                raise AssertionError("cancelled bridge returned success")
                        else:
                            assert (await job).success
                        if waiting is not None:
                            await waiting
                    assert completed.is_set() and maintenance_entered.is_set()
                    saved = await bridge.get_sdk_write_outcome(item.request_id)
                    # Bridge cancellation waits for the synchronous SDK call, which
                    # finishes normally; direct native cancellation is tested separately.
                    assert saved["status"] == "succeeded"
                    rows.append(dict(request=item.model_dump(mode="json"), node_id=node_id))
                path.write_text(json.dumps(rows))
            else:
                rows = json.loads(path.read_text())
                for row in rows:
                    item = HyMemoryIngestRequest.model_validate(row["request"])
                    saved = await bridge.get_sdk_write_outcome(item.request_id)
                    assert saved["status"] == "succeeded"
                    node = await bridge.get_sdk_memory(row["node_id"])
                    assert node is not None and node.content == "Synthetic derived summary"
                    replay = await _joined_thread(
                        sdk.add if item.user_id.endswith("-add") else sdk.add_extracted,
                        CONTENT,
                        user_id=item.user_id,
                        agent_id=item.agent_id,
                        session_id=item.session_id,
                        request_id=item.request_id,
                    )
                    assert replay["success"] and replay["replayed"]
    finally:
        MemAgent.process_add, episode.summarize_closed_episode = original


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-derived-lifecycle-") as temporary:
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
                raise RuntimeError("derived_lifecycle_probe_failed_" + mode)
    print("Derived writes precede SDK completion and maintenance; restart and replay passed.")


if __name__ == "__main__":
    main()
