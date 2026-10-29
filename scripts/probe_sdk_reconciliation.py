"""Verify installed Core/SDK outcome recovery across processes using synthetic stores.

The extraction response is a local stub; this is not a model quality test.
Run with the Hub environment, or mount this file in a network-disabled container.
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.remote_ingest import write_request
from cryptography.fernet import Fernet
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, WorkingFact
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.imports.models import EvidenceContentKind
from one_memory.memory_processing import MemoryTaskRunner


def frozen_request(extracted):
    request = write_request(
        EventEnvelope.model_validate(
            {
                **{
                    key: str(uuid4())
                    for key in (
                        "instance_id",
                        "device_id",
                        "source_id",
                        "scope_id",
                        "event_id",
                        "conversation_id",
                        "record_id",
                    )
                },
                "revision": 1,
                "kind": "conversation_turn",
                "content": {"messages": [{"role": "user", "text": "Synthetic recovery probe."}]},
            }
        )
    )
    if extracted:
        request = request.model_copy(
            update={
                "input_records": tuple(
                    row.model_copy(update={"content_kind": EvidenceContentKind.MEMORY_CONTENT})
                    for row in request.input_records
                )
            }
        )
    return request


async def seed_output(sdk, bridge, request, scene, *, typed=False):
    raw_id, memory_id, fact_id = (str(uuid4()) for _ in range(3))
    identity = dict(
        user_id=request.user_id, agent_id=request.agent_id, session_id=request.session_id
    )
    if scene in {"working", "coding"}:

        async def populate():
            store = CodingMemoryStore(
                sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=scene
            )
            await store.initialize()
            try:
                await store.insert(
                    CodingMemory(
                        experience_id=memory_id,
                        task="Synthetic task",
                        solution="Synthetic solution",
                        source_fact_ids=[fact_id],
                        **identity,
                    )
                )
                await store.insert_fact(
                    WorkingFact(
                        fact_id=fact_id,
                        statement="Synthetic fact",
                        derived_experience_ids=[memory_id],
                        **identity,
                    )
                )
            finally:
                await store.close()

        await _joined_thread(lambda: sdk._loop_thread.run(populate()))
        result = dict(
            scene="productivity",
            memory_id=memory_id,
            experience_ids=[memory_id],
            fact_ids=[fact_id],
        )
        expected = {"raw": [], "memory": [memory_id, fact_id]}
    else:
        response = await _joined_thread(
            sdk.import_memory_exact,
            "Synthetic semantic memory",
            memory_id=memory_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
        )
        assert response["success"]
        if scene == "chat":
            await bridge.import_sdk_raw_exact(
                content="Synthetic original conversation",
                raw_id=raw_id,
                **identity,
                source="synthetic",
                memory_at=None,
                metadata={},
                request_id=str(uuid4()),
            )
        result = dict(
            scene="chat", memory_id=raw_id if scene == "chat" else memory_id, memory_ids=[memory_id]
        )
        expected = {"raw": [raw_id] if scene == "chat" else [], "memory": [memory_id]}
    calls = []

    async def extraction_stub(*args, **kwargs):
        calls.append(kwargs["request_id"])
        return dict(success=True, request_id=request.request_id, **result)

    operation = "add_extracted" if scene == "extracted" else "add"
    original = getattr(sdk, "_async_" + operation + "_impl")
    writer, chain = sdk._get_coding_writer, sdk._observe_chain
    try:
        if typed and scene in {"working", "coding"}:
            from types import SimpleNamespace

            async def controlled_writer(*, variant):
                assert variant == scene
                return SimpleNamespace(write=extraction_stub)

            sdk._get_coding_writer = controlled_writer
            response = await _joined_thread(
                sdk.add,
                [
                    {"role": "user", "content": "Synthetic task"},
                    {"role": "tool", "content": "Synthetic result", "tool_name": "read"},
                ],
                **identity,
                request_id=request.request_id,
                scene=scene,
            )
            assert response["productivity_variant"] == scene
        else:
            setattr(sdk, "_async_" + operation + "_impl", extraction_stub)
            response = await _joined_thread(
                getattr(sdk, operation),
                "Synthetic input",
                **identity,
                request_id=request.request_id,
            )
    finally:
        setattr(sdk, "_async_" + operation + "_impl", original)
        sdk._get_coding_writer, sdk._observe_chain = writer, chain
    assert response["success"] and calls == [request.request_id]
    return expected


async def phase(root, mode):
    state_path = root / "state.json"
    state = json.loads(state_path.read_text())
    config = HubConfig(
        root / "data",
        root / "config",
        "http://localhost:8800",
        state["admin"],
        state["key"].encode(),
        governance_dir=root / "governance",
    )
    if mode == "seed":
        activate_authority(
            config.config_dir, config.governance_dir, config.encryption_key, new=True
        )
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        queue = app.core.state.memory_write_queue
        bridge = _sdk_library_bridge(app.core)
        sdk = await bridge._get_client()
        if mode == "seed":
            for scene in ("chat", "extracted", "working", "coding"):
                frozen = frozen_request(scene == "extracted")
                job = queue.submit(frozen)
                request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
                expected = await seed_output(sdk, bridge, request, scene)
                queue.settle(job.job_id, state="unknown", hy_invoked=True)
                assert queue.sdk_receipt(job.job_id) is None
                state["expected"][job.job_id] = expected
            state_path.write_text(json.dumps(state))
        else:

            def no_write(*args, **kwargs):
                raise AssertionError("unexpected_write_during_inquiry")

            sdk.add = no_write
            sdk.add_extracted = no_write
            assert not sdk._coding_stores
            await app.core.state.memory_write_drain.reconcile_unknown()
            for job_id, expected in state["expected"].items():
                receipt = queue.sdk_receipt(job_id)
                assert receipt is not None
                assert list(receipt.raw_ids) == expected["raw"]
                assert list(receipt.memory_ids) == expected["memory"]
            assert await app.core.state.cmh_reconciler.run() == 0
            assert not sdk._coding_stores
            assert len(state["expected"]) == 4


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] in {"seed", "recover"}:
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    if "--installed" in sys.argv:
        from cloud_memory_hub import sdk_reconcile

        assert "/site-packages/" in sdk_reconcile.__file__
    with TemporaryDirectory(prefix="cmh-sdk-recovery-") as temporary:
        root = Path(temporary)
        (root / "state.json").write_text(
            json.dumps(
                {
                    "admin": secrets.token_urlsafe(40),
                    "key": Fernet.generate_key().decode(),
                    "expected": {},
                }
            )
        )
        for mode in ("seed", "recover"):
            with (root / (mode + ".log")).open("w") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError("sdk_recovery_probe_failed_" + mode)
    print(
        "Four SDK outcome kinds recovered across processes; "
        "no writer retry or coding writer initialization."
    )


if __name__ == "__main__":
    main()
