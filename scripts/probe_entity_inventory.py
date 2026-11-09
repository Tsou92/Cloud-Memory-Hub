"""Installed entity guard: real SDK/Chroma and HTTP, synthetic model/embedder.

All instances are private synthetic fixtures. Native upserts model entity-only
restores; neither this probe nor the guard implements entity deletion.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import hy_memory.pipelines.writer as native_writer
from chromadb.errors import NotFoundError
from cloud_memory_hub import governance_inventory
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError
from httpx import ASGITransport, AsyncClient
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.pipelines._retrieval import entities
from hy_memory.pipelines._retrieval.config import entity_collection_name
from one_memory.api import _sdk_library_bridge, builtin_retrieval_snapshot
from one_memory.hy_memory_bridge import HyMemoryBridge, LocalHyEmbedService, _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase

ENTITY = "Synthetic installed entity inventory canary"
SCENARIOS = (
    "native",
    "planned-forget",
    "planned-purge",
    "purge-target",
    "purge-absent",
    "purge-foreign",
)


class SyntheticEmbedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


@asynccontextmanager
async def runtime(app, config):
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        sdk = await _sdk_library_bridge(app.core)._get_client()
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            yield sdk, web


async def entity_rows(sdk, namespace):
    vector = sdk._vector_store
    try:
        collection = await _joined_thread(
            vector._client.get_collection, name=entity_collection_name(vector._collection_name)
        )
    except NotFoundError:
        return {"ids": [], "documents": [], "metadatas": []}
    return await _joined_thread(
        collection.get,
        where={"user_id": {"$eq": namespace}},
        include=["metadatas", "documents"],
    )


async def seed_entity_copy(sdk, namespace):
    await _joined_thread(
        lambda: sdk._loop_thread.run(
            sdk._vector_store.upsert_entity(
                entity_text=ENTITY,
                entity_type="PROPER",
                embedding=[1.0] + [0.0] * 511,
                memory_id=str(uuid4()),
                user_id=namespace,
            )
        )
    )
    sdk._vector_store._entity_collection = None
    assert len((await entity_rows(sdk, namespace))["ids"]) == 1, "entity_copy_fixture_failed"


async def preview(web, config, state, action="purge"):
    return await web.post(
        "/hub/v1/governance/preview",
        headers={"Authorization": "Bearer " + config.admin_token},
        json={
            "scope_id": state["event"]["scope_id"],
            "action": action,
            "record_ids": ["cmhe:" + state["receipt"]],
        },
    )


def require_refusal(reply):
    assert reply.status_code == 503, "entity_scope_was_not_refused"
    assert reply.json() == {"detail": "governance_derivation_unmodeled"}
    assert ENTITY not in reply.text, "entity_text_leaked_in_error"


def archive_present(config, state):
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        return (
            db.execute(
                "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?", (state["receipt"],)
            ).fetchone()
            is not None
        )


async def native_add(app, sdk, web, config, state):
    async def extract(self, **kwargs):
        return AgentResult(
            success=True, extracted_info={"memory": [{"content": ENTITY + " preference"}]}
        )

    queue = app.core.state.memory_write_queue
    frozen = queue.payload_for(state["receipt"])
    request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
    assert sdk._config.recall.entity_store_enabled is True, "entity_writer_not_enabled"
    with (
        patch.object(MemAgent, "process_add", extract),
        patch.object(native_writer, "_RECONCILE_ENABLED", False),
        patch.object(entities, "extract_entities", lambda text: [("PROPER", ENTITY)]),
    ):
        result = await _joined_thread(
            sdk.add,
            [turn.model_dump() for turn in request.turns],
            user_id=request.user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            source=request.source_id,
            request_id=request.request_id,
            scene="chat",
        )
    assert result["success"] is True, "native_entity_add_failed"
    await app.core.state.cmh_lineage.observe(state["receipt"], frozen)
    queue.settle(state["receipt"], state=COMPLETED)
    stored = await entity_rows(sdk, state["namespace"])
    assert stored["documents"] == [ENTITY], "native_entity_document_missing"
    assert json.loads(stored["metadatas"][0]["linked_memory_ids"]) == result["memory_ids"]
    state["memory_ids"] = result["memory_ids"]
    sdk._config.recall.entity_store_enabled = False
    sdk._vector_store._entity_collection = None
    require_refusal(await preview(web, config, state))


async def seed_operation(app, sdk, web, config, state, scenario):
    action = scenario.removeprefix("planned-") if scenario.startswith("planned-") else "purge"
    viewed = await preview(web, config, state, action)
    assert viewed.status_code == 200, "clean_scope_preview_failed"
    request = {
        "scope_id": state["event"]["scope_id"],
        "action": action,
        "record_ids": ["cmhe:" + state["receipt"]],
        "expected_view": viewed.json()["view_digest"],
        "operation_id": str(uuid4()),
    }
    state["request"] = request
    executor = app.core.state.cmh_executor
    admin = {"Authorization": "Bearer " + config.admin_token}
    if scenario.startswith("planned-"):
        await seed_entity_copy(sdk, state["namespace"])

        async def old_inventory_guard(*args):
            pass

        async def interrupted(*args, **kwargs):
            raise RuntimeError("synthetic old executor interrupted after its signed intent")

        # Emulate the previous inventory that omitted companion entities, then
        # interrupt after the actual signed plan and journal were persisted.
        with (
            patch.object(governance_inventory, "_require_no_entity_copies", old_inventory_guard),
            patch.object(executor, "_finish", interrupted),
        ):
            reply = await web.post("/hub/v1/governance/execute", headers=admin, json=request)
        assert reply.status_code == 503, "legacy_plan_fixture_did_not_interrupt"
        try:
            await asyncio.wait_for(executor.resume(), 15)
        except GovernanceError as error:
            assert str(error) == "governance_derivation_unmodeled"
        else:
            raise AssertionError("legacy_plan_resumed_over_entity")
        assert executor.store.operations()[request["operation_id"]][0] == "planned"
        assert archive_present(config, state), "refused_recovery_removed_original"
        assert len((await entity_rows(sdk, state["namespace"]))["ids"]) == 1
    else:
        reply = await web.post("/hub/v1/governance/execute", headers=admin, json=request)
        assert reply.status_code == 200, "clean_purge_failed"
        assert reply.json()["state"] == "completed"
        assert not archive_present(config, state), "completed_purge_retained_original"
        assert not await executor.purger.present(
            request["record_ids"], scope_id=request["scope_id"]
        )
        if scenario == "purge-target":
            await seed_entity_copy(sdk, state["namespace"])
        elif scenario == "purge-foreign":
            project = await web.post(
                "/hub/v1/projects", headers=admin, json={"display_name": "Other synthetic project"}
            )
            assert project.status_code == 201
            state["foreign_namespace"] = (
                f"cmh:{state['event']['instance_id']}:{project.json()['project_id']}"
            )
            await seed_entity_copy(sdk, state["foreign_namespace"])
        # Only the companion entity was restored: modeled purge targets remain absent.
        assert not await executor.purger.present(
            request["record_ids"], scope_id=request["scope_id"]
        )


async def blocked_startup(config, state, scenario):
    authority = open_authority(config)
    checkpoint = authority.journal.head(state["event"]["scope_id"])
    before = ExecutionStore(authority.journal).operations()
    app = create_hub(config)
    try:
        async with runtime(app, config):
            raise AssertionError("entity_obstruction_was_ignored_at_startup")
    except GovernanceError as error:
        assert str(error) == "governance_derivation_unmodeled"
        assert ENTITY not in str(error)
    assert app.core.state.cmh_executor.ready is False
    assert app.core.state.cmh_executor._replayed is False
    assert ExecutionStore(authority.journal).operations() == before, "signed_plan_was_changed"
    assert authority.journal.head(state["event"]["scope_id"]) == checkpoint
    assert archive_present(config, state) == scenario.startswith("planned-")
    # Open a fresh read-only SDK handle after the failed Core startup to verify
    # the authoritative entity survived the refusal unchanged.
    bridge = HyMemoryBridge(
        profile=builtin_retrieval_snapshot(), api_key=None, data_dir=config.data_dir
    )
    try:
        sdk = await bridge._get_client()
        assert (await entity_rows(sdk, state["namespace"]))["documents"] == [ENTITY]
    finally:
        await bridge.close()


async def phase(root, scenario, mode):
    os.environ.update(environment(root))
    os.environ.pop("MEMORY_ENTITY_STORE_ENABLED", None)
    if mode == "seed":
        await archive_phase(root, "seed")
        if scenario == "native":
            os.environ["MEMORY_ENTITY_STORE_ENABLED"] = "1"
    state_path = root / "state.json"
    state = json.loads(state_path.read_text())
    state["namespace"] = f"cmh:{state['event']['instance_id']}:{state['event']['scope_id']}"
    config = HubConfig.from_environment()
    if mode == "read" and (scenario.startswith("planned-") or scenario == "purge-target"):
        await blocked_startup(config, state, scenario)
        return
    app = create_hub(config)
    with patch.object(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder()):
        async with runtime(app, config) as (sdk, web):
            if mode == "seed":
                if scenario == "native":
                    await native_add(app, sdk, web, config, state)
                else:
                    await seed_operation(app, sdk, web, config, state, scenario)
                state_path.write_text(json.dumps(state))
                return
            assert sdk._config.recall.entity_store_enabled is False
            assert sdk._vector_store._entity_collection is None
            if scenario == "native":
                require_refusal(await preview(web, config, state))
                stored = await entity_rows(sdk, state["namespace"])
                assert stored["documents"] == [ENTITY]
                assert (
                    json.loads(stored["metadatas"][0]["linked_memory_ids"]) == state["memory_ids"]
                )
            else:
                app.core.state.cmh_governance.require_writes_ready()
                assert app.core.state.cmh_executor.ready is True
                assert app.core.state.cmh_executor._replayed is True
                assert not archive_present(config, state)
                assert (await entity_rows(sdk, state["namespace"]))["ids"] == []
                operation = app.core.state.cmh_executor.store.operations()[
                    state["request"]["operation_id"]
                ]
                assert operation[0] == "completed"
                if scenario == "purge-foreign":
                    assert (await entity_rows(sdk, state["foreign_namespace"]))["documents"] == [
                        ENTITY
                    ]


def run(root, scenario, mode):
    with (root / f"{mode}.log").open("w") as log:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), scenario, mode, str(root)],
            stdout=log,
            stderr=log,
            timeout=180,
        )
    if result.returncode:
        raise RuntimeError("entity_inventory_probe_failed_" + scenario + "_" + mode)


def main():
    if len(sys.argv) == 4:
        scenario, mode = sys.argv[1:3]
        assert scenario in SCENARIOS and mode in {"seed", "read"}
        asyncio.run(phase(Path(sys.argv[3]), scenario, mode))
        return
    with TemporaryDirectory(prefix="cmh-entity-inventory-") as temporary:
        for scenario in SCENARIOS:
            root = Path(temporary) / scenario
            root.mkdir(mode=0o700)
            subprocess.run(
                [sys.executable, "-m", "cloud_memory_hub", "init"],
                env=environment(root),
                check=True,
                capture_output=True,
                timeout=60,
            )
            run(root, scenario, "seed")
            run(root, scenario, "read")
    print(
        "Installed entity guard passed across processes: native SDK entity writes, disabled-writer "
        "preview refusal, preserved legacy plans, entity-only purge restore refusal, and "
        "clean/foreign scope recovery (real Chroma; synthetic extraction and embeddings; "
        "no model invocation)."
    )


if __name__ == "__main__":
    main()
