"""ADR-0031: do not publish a scope that omits the native entity collection."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from chromadb.errors import NotFoundError
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_inventory import (
    observe_inventory,
    require_modeled_derivations,
)
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_purge import Purger
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.pipelines._retrieval import entities
from hy_memory.pipelines._retrieval.config import entity_collection_name
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import LocalHyEmbedService, _joined_thread
from test_governance_execution import EXECUTE, PREVIEW
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_sdk_reconcile import sdk_request

ENTITY = "Synthetic entity inventory canary"


class SyntheticEmbedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


async def native(sdk, operation):
    return await _joined_thread(lambda: sdk._loop_thread.run(operation))


async def seed_entity(sdk, namespace):
    await native(
        sdk,
        sdk._vector_store.upsert_entity(
            entity_text=ENTITY,
            entity_type="PROPER",
            embedding=[1.0] + [0.0] * 511,
            memory_id=str(uuid4()),
            user_id=namespace,
        ),
    )
    # A reopened SDK has no cached handle to the persisted companion collection.
    sdk._vector_store._entity_collection = None


async def confirmed_operation(web, config, action):
    admin, headers, _, event = await provision(web, config)
    job = await accepted(web, headers, event)
    body = {
        "scope_id": event["scope_id"],
        "action": action,
        "record_ids": ["cmhe:" + job],
    }
    preview = await web.post(PREVIEW, headers=admin, json=body)
    assert preview.status_code == 200, preview.text
    return (
        admin,
        {
            **body,
            "expected_view": preview.json()["view_digest"],
            "operation_id": str(uuid4()),
        },
        f"cmh:{event['instance_id']}:{event['scope_id']}",
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_real_sdk_entity_output_refuses_scope_even_after_disabling_writer(
    tmp_path, monkeypatch, enabled
):
    monkeypatch.setenv("MEMORY_ENTITY_STORE_ENABLED", "1")
    monkeypatch.setattr(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder())
    monkeypatch.setattr("hy_memory.pipelines.writer._RECONCILE_ENABLED", False)
    monkeypatch.setattr(entities, "extract_entities", lambda text: [("PROPER", ENTITY)])

    async def extract(self, **kwargs):
        return AgentResult(
            success=True,
            extracted_info={"memory": [{"content": ENTITY + " preference"}]},
        )

    monkeypatch.setattr(MemAgent, "process_add", extract)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            frozen = app.core.state.memory_write_queue.payload_for(job)
            request = sdk_request(frozen)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            assert sdk._config.recall.entity_store_enabled is True
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
            assert result["success"] is True
            vector = sdk._vector_store
            entity = await _joined_thread(
                vector._entity_collection.get,
                where={"user_id": {"$eq": request.user_id}},
                include=["metadatas", "documents"],
            )
            assert len(entity["ids"]) == 1
            assert entity["documents"] == [ENTITY]
            assert json.loads(entity["metadatas"][0]["linked_memory_ids"]) == result["memory_ids"]
            sdk._config.recall.entity_store_enabled = enabled
            # Restored storage can exist without an initialized entity handle.
            vector._entity_collection = None
            if enabled:
                with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
                    require_modeled_derivations(sdk._config)
            else:
                require_modeled_derivations(sdk._config)
            with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
                await observe_inventory(bridge, request.user_id, app.core.state.cmh_lineage)
            reply = await web.post(
                "/hub/v1/governance/preview",
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "purge",
                    "record_ids": ["cmhe:" + job],
                },
            )
            assert reply.status_code == 503
            assert reply.json() == {"detail": "governance_derivation_unmodeled"}
            assert ENTITY not in reply.text
        if not enabled:
            monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED")
            async with fixture(config) as (reopened, _):
                bridge = _sdk_library_bridge(reopened.core)
                sdk = await bridge._get_client()
                assert sdk._config.recall.entity_store_enabled is False
                assert sdk._vector_store._entity_collection is None
                with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
                    await observe_inventory(
                        bridge, request.user_id, reopened.core.state.cmh_lineage
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("storage", ["absent", "empty", "foreign"])
def test_empty_or_foreign_entity_storage_is_scoped_read_only(tmp_path, monkeypatch, storage):
    monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED", raising=False)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, event = await provision(web, config)
            namespace = f"cmh:{event['instance_id']}:{event['scope_id']}"
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            vector = sdk._vector_store
            assert sdk._config.recall.entity_store_enabled is False
            name = entity_collection_name(vector._collection_name)
            if storage == "empty":
                await native(sdk, vector._get_entity_collection())
            elif storage == "foreign":
                await native(
                    sdk,
                    vector.upsert_entity(
                        entity_text=ENTITY,
                        entity_type="PROPER",
                        embedding=[1.0] + [0.0] * 511,
                        memory_id=str(uuid4()),
                        user_id=f"cmh:{event['instance_id']}:{uuid4()}",
                    ),
                )
            vector._entity_collection = None
            before = {
                collection.name
                for collection in await _joined_thread(vector._client.list_collections)
            }
            assert (name in before) == (storage != "absent")

            def never_create(*args, **kwargs):
                pytest.fail("governance observation must not create a collection")

            original_get = vector._client.get_collection
            queries = []

            def inspect_get(*, name, **kwargs):
                collection = original_get(name=name, **kwargs)
                get = collection.get

                def inspect_query(**options):
                    queries.append(options)
                    return get(**options)

                return SimpleNamespace(get=inspect_query)

            monkeypatch.setattr(vector._client, "get_or_create_collection", never_create)
            monkeypatch.setattr(vector._client, "get_collection", inspect_get)
            assert await observe_inventory(bridge, namespace, app.core.state.cmh_lineage) == {}
            assert queries == (
                []
                if storage == "absent"
                else [{"where": {"user_id": {"$eq": namespace}}, "include": [], "limit": 1}]
            )
            assert vector._entity_collection is None
            after = {
                collection.name
                for collection in await _joined_thread(vector._client.list_collections)
            }
            assert after == before

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["lookup", "read", "read_not_found"])
def test_entity_lookup_errors_are_not_treated_as_absent(tmp_path, monkeypatch, failure):
    monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED", raising=False)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, event = await provision(web, config)
            namespace = f"cmh:{event['instance_id']}:{event['scope_id']}"
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()

            def unreadable(*args, **kwargs):
                if failure == "read_not_found":
                    raise NotFoundError(ENTITY)
                raise RuntimeError(ENTITY)

            monkeypatch.setattr(
                sdk._vector_store._client,
                "get_collection",
                unreadable
                if failure == "lookup"
                else lambda **kwargs: SimpleNamespace(get=unreadable),
            )
            with pytest.raises(
                GovernanceError, match="^governance_inventory_unavailable$"
            ) as error:
                await observe_inventory(bridge, namespace, app.core.state.cmh_lineage)
            assert ENTITY not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"ids": None},
        {"ids": "not-a-list"},
        {"ids": [None]},
        {"ids": [""]},
        {"ids": ["one", "two"]},
    ],
)
def test_entity_lookup_malformed_result_fails_closed(tmp_path, monkeypatch, result):
    monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED", raising=False)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, event = await provision(web, config)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            monkeypatch.setattr(
                sdk._vector_store._client,
                "get_collection",
                lambda **kwargs: SimpleNamespace(get=lambda **options: result),
            )
            with pytest.raises(GovernanceError, match="^governance_store_shape_unknown$"):
                await observe_inventory(
                    bridge,
                    f"cmh:{event['instance_id']}:{event['scope_id']}",
                    app.core.state.cmh_lineage,
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
@pytest.mark.parametrize("recovery", ["same_process", "restart"])
@pytest.mark.parametrize("obstruction", ["entity", "enabled_writer"])
def test_legacy_plan_cannot_complete_with_unmodeled_entities(
    tmp_path, monkeypatch, action, recovery, obstruction
):
    monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED", raising=False)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, namespace = await confirmed_operation(web, config, action)
            executor = app.core.state.cmh_executor
            sdk = await _sdk_library_bridge(app.core)._get_client()
            if obstruction == "entity":
                await seed_entity(sdk, namespace)

            async def old_entity_guard(*args):
                pass

            async def crash_before_finish(*args):
                raise RuntimeError("synthetic legacy interruption after intent")

            # Recreate an older version that omitted entities, then crashed
            # after the real signed intent was appended and before finishing.
            with monkeypatch.context() as legacy:
                legacy.setattr(
                    "cloud_memory_hub.governance_inventory._require_no_entity_copies",
                    old_entity_guard,
                )
                legacy.setattr(executor, "_finish", crash_before_finish)
                reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 503, reply.text
            assert executor.store.operations()[body["operation_id"]][0] == "planned"
            if obstruction == "enabled_writer":
                sdk._config.recall.entity_store_enabled = True

            if recovery == "same_process":
                assert executor._replayed is False
                assert executor.ready is False
                assert executor.authority.runtime_recovery_required
                with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
                    await asyncio.wait_for(executor.resume(), 8)
                assert executor.store.operations()[body["operation_id"]][0] == "planned"
                with app.core.state.cmh_ingest._database() as db:
                    assert db.execute(
                        "SELECT 1 FROM cmh_event_receipts WHERE receipt_id=?",
                        (body["record_ids"][0].removeprefix("cmhe:"),),
                    ).fetchone()

        if recovery == "restart":
            if obstruction == "enabled_writer":
                monkeypatch.setenv("MEMORY_ENTITY_STORE_ENABLED", "1")
            reopened = create_hub(config)
            with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
                async with reopened.core.router.lifespan_context(reopened.core):
                    pytest.fail("startup must refuse the incomplete legacy plan")
            executor = reopened.core.state.cmh_executor
            assert executor._replayed is False
            assert executor.ready is False
        store = ExecutionStore(open_authority(config).journal)
        assert store.operations()[body["operation_id"]][0] == "planned"

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
@pytest.mark.parametrize("storage", ["target", "foreign", "absent"])
def test_completed_operations_check_entity_only_restore_before_purge_presence(
    tmp_path, monkeypatch, action, storage
):
    monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED", raising=False)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, body, namespace = await confirmed_operation(web, config, action)
            reply = await web.post(EXECUTE, headers=admin, json=body)
            assert reply.status_code == 200, reply.text
            executor = app.core.state.cmh_executor
            if action == "purge":
                assert not await executor.purger.present(
                    body["record_ids"], scope_id=body["scope_id"]
                )
            if storage != "absent":
                sdk = await _sdk_library_bridge(app.core)._get_client()
                await seed_entity(sdk, namespace if storage == "target" else namespace + "-other")

        if storage == "target":

            async def never_skip_or_run(*args, **kwargs):
                pytest.fail("entity preflight must run before purge presence or mutation")

            monkeypatch.setattr(Purger, "present", never_skip_or_run)
            monkeypatch.setattr(Purger, "run", never_skip_or_run)
            reopened = create_hub(config)
            with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
                async with reopened.core.router.lifespan_context(reopened.core):
                    pytest.fail("startup must refuse a restored entity-only copy")
            assert reopened.core.state.cmh_executor._replayed is False
            assert reopened.core.state.cmh_executor.ready is False
        else:
            async with fixture(config) as (reopened, _):
                assert reopened.core.state.cmh_executor._replayed is True
                assert reopened.core.state.cmh_executor.ready is True
                reopened.core.state.cmh_governance.require_writes_ready()
                if storage == "foreign":
                    sdk = await _sdk_library_bridge(reopened.core)._get_client()
                    collection = await _joined_thread(
                        sdk._vector_store._client.get_collection,
                        name=entity_collection_name(sdk._vector_store._collection_name),
                    )
                    assert await _joined_thread(collection.count) == 1
        store = ExecutionStore(open_authority(config).journal)
        assert store.operations()[body["operation_id"]][0] == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("purger_available", [True, False])
def test_replay_checks_all_operation_scopes_before_any_purge(
    tmp_path, monkeypatch, purger_available
):
    monkeypatch.delenv("MEMORY_ENTITY_STORE_ENABLED", raising=False)
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            # A later forget in another project must be checked before even
            # testing presence for the first completed purge.
            for action in ("purge", "forget"):
                admin, body, namespace = await confirmed_operation(web, config, action)
                reply = await web.post(EXECUTE, headers=admin, json=body)
                assert reply.status_code == 200, reply.text
            sdk = await _sdk_library_bridge(app.core)._get_client()
            await seed_entity(sdk, namespace)

        async def never_present(*args, **kwargs):
            pytest.fail("all recorded scopes must pass before any purge presence check")

        monkeypatch.setattr(Purger, "present", never_present)
        reopened = create_hub(config)
        executor = reopened.core.state.cmh_executor
        if not purger_available:
            executor.purger = None
        with pytest.raises(GovernanceError, match="^governance_derivation_unmodeled$"):
            async with reopened.core.router.lifespan_context(reopened.core):
                pytest.fail("every recorded project needs the entity preflight")
        assert executor._replayed is False
        assert executor.ready is False
        assert all(state == "completed" for state, _ in executor.store.operations().values())

    asyncio.run(scenario())
