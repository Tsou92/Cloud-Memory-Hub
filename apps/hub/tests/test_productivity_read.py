import asyncio
import json
import sqlite3
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub import productivity_read
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.productivity_read import ProductivityRead, record_id
from cloud_memory_hub.read_contract import GetRequest, ReadReply
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.store import CodingMemoryStore, _variant_db_path
from hy_memory.coding.types import CodingMemory, WorkingFact
from hy_memory.data.vector_store_chroma import ChromaVectorStore
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_remote_ingest import config_for, provision
from test_remote_read import grant_reads

QUERY = "The project uses a blue design."


async def seed(sdk, namespace, *, shared_id=None, details="", variants=("working", "coding")):
    identity = shared_id or str(uuid4())

    async def populate():
        for variant in variants:
            store = CodingMemoryStore(
                sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
            )
            await store.initialize()
            try:
                await store.insert(
                    CodingMemory(
                        experience_id=identity,
                        user_id=namespace,
                        agent_id="synthetic-agent",
                        source="synthetic-source",
                        session_id="synthetic-session",
                        task=QUERY,
                        solution="Reuse the blue design.",
                        boundary_envs="Desktop and terminal",
                    )
                )
                await store.insert_fact(
                    WorkingFact(
                        fact_id=identity,
                        user_id=namespace,
                        agent_id="synthetic-agent",
                        source="synthetic-source",
                        session_id="synthetic-session",
                        statement=QUERY,
                        details=details,
                    )
                )
            finally:
                await store.close()

    await _joined_thread(lambda: sdk._loop_thread.run(populate()))
    return identity


def change(sdk, sql, parameters=(), suffix=""):
    with sqlite3.connect(_variant_db_path(sdk._config.coding.db_path, suffix)) as db:
        db.execute(sql, parameters)


def test_real_http_reads_both_variants_pagination_scope_privacy_and_restart(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw, permissions=("memory:read",))
                scope = raw["scope_id"]
                namespace = f"cmh:{raw['instance_id']}:{scope}"
                bridge = _sdk_library_bridge(app.core)
                sdk = await bridge._get_client()
                shared = await seed(sdk, namespace, details="x" * 4100 + " password=" + token)
                foreign = await seed(sdk, "outside-this-project")
                global_id = await seed(sdk, f"cmh:{raw['instance_id']}:global")
                obsolete = await seed(sdk, namespace, variants=("working",))
                change(
                    sdk, "UPDATE working_fact SET status='superseded' WHERE fact_id=?", (obsolete,)
                )
                change(sdk, "DELETE FROM coding_memory_meta WHERE experience_id=?", (obsolete,))
                # Same native ID also exists in the ordinary store: no address collision.
                result = await _joined_thread(
                    sdk.import_memory_exact,
                    QUERY,
                    memory_id=shared,
                    user_id=namespace,
                    agent_id="synthetic-agent",
                    layer="l2_fact",
                    request_id=str(uuid4()),
                )
                assert result["success"]
                expected = [shared] + [
                    record_id(v, k, shared) for v in ("w", "c") for k in ("e", "f")
                ]

                def forbidden(*args, **kwargs):
                    pytest.fail("reads must not initialize a productivity writer or collection")

                with monkeypatch.context() as patch:
                    patch.setattr(sdk, "_get_coding_writer", forbidden)
                    patch.setattr(CodingMemoryStore, "initialize", forbidden)
                    patch.setattr(sdk._vector_store._client, "get_or_create_collection", forbidden)

                    async def read(path, **body):
                        return await web.post(
                            "/hub/v1/" + path,
                            headers=headers,
                            json={"scope_id": scope, **body},
                        )

                    for limit in (1, 2, 3, 5, 10):
                        ids, offset = [], 0
                        while offset is not None:
                            response = await read("memories/list", limit=limit, offset=offset)
                            assert response.status_code == 200, response.text
                            page = ReadReply.model_validate(response.json())
                            ids.extend(item.record_id for item in page.items)
                            offset = page.next_offset
                        assert ids == expected
                    assert (await read("memories/list", offset=100)).json()["items"] == []
                    groups = await ProductivityRead(bridge).search(namespace, QUERY, 10)
                    assert {row.memory_id for group in groups for row in group} == set(expected[1:])
                    response = await read("memories/search", query=QUERY, limit=10)
                    assert response.status_code == 200, response.text
                    assert {item["record_id"] for item in response.json()["items"]} == set(expected)
                    # With three slots each storage family gets one result.
                    response = await read("memories/search", query=QUERY, limit=3)
                    ids = [item["record_id"] for item in response.json()["items"]]
                    assert ids == [shared, record_id("w", "f", shared), record_id("c", "f", shared)]
                    for variant in ("w", "c"):
                        for kind in ("e", "f"):
                            for hidden in (foreign, global_id, obsolete, str(uuid4())):
                                response = await read(
                                    "memories/get", record_id=record_id(variant, kind, hidden)
                                )
                                assert response.status_code == 404
                            identity = record_id(variant, kind, shared)
                            content, offset = "", 0
                            while offset is not None:
                                response = await read(
                                    "memories/get", record_id=identity, content_offset=offset
                                )
                                assert response.status_code == 200, response.text
                                item = response.json()["items"][0]
                                assert item["source"] == "synthetic-source"
                                assert item["source_raw_ids"] == []
                                assert item["layer"].endswith(
                                    "experience" if kind == "e" else "fact"
                                )
                                content += item["content"]
                                offset = item["next_content_offset"]
                            assert token not in content
                            assert ("statement" if kind == "f" else "task") in json.loads(content)
                            response = await read("history/get", record_id=identity)
                            assert response.status_code == 403
                    response = await read("memories/list", scope_id="global")
                    assert len(response.json()["items"]) == 4
                    assert all(global_id in item["record_id"] for item in response.json()["items"])
                    for malformed in ("cmhp:w:e:", "cmhp:x:e:abc", "cmhp:w:q:abc"):
                        assert (await read("memories/get", record_id=malformed)).status_code == 404
                assert not sdk._coding_stores

        restarted = create_hub(config)
        async with restarted.core.router.lifespan_context(restarted.core):
            async with AsyncClient(
                transport=ASGITransport(restarted), base_url=config.public_origin
            ) as web:
                response = await web.post(
                    "/hub/v1/memories/list", headers=headers, json={"scope_id": scope}
                )
                assert response.status_code == 200, response.text
                assert [row["record_id"] for row in response.json()["items"]] == expected
                sdk = await _sdk_library_bridge(restarted.core)._get_client()
                assert not sdk._coding_stores

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["corrupt", "schema", "collection", "identity", "metadata"])
def test_broken_existing_store_fails_closed_without_echo(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, _, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                sdk = await _sdk_library_bridge(app.core)._get_client()
                namespace = f"cmh:{raw['instance_id']}:{raw['scope_id']}"
                identity = await seed(sdk, namespace, variants=("working",))
                path = Path(sdk._config.coding.db_path)
                operation = "list"
                if damage == "corrupt":
                    path.write_bytes(b"synthetic corrupt database")
                elif damage == "schema":
                    change(sdk, "DROP TABLE working_fact")
                elif damage == "identity":
                    change(sdk, "UPDATE coding_memory_meta SET experience_id=?", ("a" * 192,))
                elif damage == "metadata":
                    change(sdk, "UPDATE coding_memory_meta SET search_keys=?", ("not-json",))
                else:
                    store = CodingMemoryStore(sdk._config, sdk._embed_service)
                    await _joined_thread(
                        sdk._vector_store._client.delete_collection,
                        name=ChromaVectorStore(store._coding_config)._collection_name,
                    )
                    operation = "search"
                body = {"scope_id": raw["scope_id"]}
                if operation == "search":
                    body["query"] = QUERY
                response = await web.post(
                    "/hub/v1/memories/" + operation, headers=headers, json=body
                )
                assert response.status_code == 503
                assert response.json() == {"detail": "read_unavailable"}
                assert identity not in response.text and QUERY not in response.text

    asyncio.run(scenario())


def test_absent_databases_stay_absent_and_get_is_readonly(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            reader = ProductivityRead(bridge)
            paths = [
                Path(_variant_db_path(sdk._config.coding.db_path, s)) for s in ("", "_cv", "_trace")
            ]
            assert not any(path.exists() for path in paths)
            assert await reader.page("synthetic", 10, 0) == ([], 0)
            assert await reader.get(record_id("w", "f", str(uuid4()))) is None
            assert await reader.search("synthetic", QUERY, 5) == [[], []]
            assert not any(path.exists() for path in paths)
            shared = await seed(sdk, "synthetic", variants=("working",))
            before = paths[0].read_bytes()
            await reader.page("synthetic", 10, 0)
            assert (await reader.get(record_id("w", "f", shared))).source == "synthetic-source"
            assert paths[0].read_bytes() == before
            assert not sdk._coding_stores

    asyncio.run(scenario())


def test_cancel_waits_for_real_sqlite_operation_and_shared_admission(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    entered, release = Event(), Event()
    original = productivity_read._page

    def held(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(productivity_read, "_page", held)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            reader = ProductivityRead(_sdk_library_bridge(app.core))
            task = asyncio.create_task(reader.page("synthetic", 10, 0))
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            maintenance_entered = asyncio.Event()

            async def maintenance():
                async with app.core.state.cmh_sdk_admission.exclusive():
                    maintenance_entered.set()

            barrier = asyncio.create_task(maintenance())
            await asyncio.sleep(0)
            try:
                assert not task.done() and not maintenance_entered.is_set()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await barrier
            assert maintenance_entered.is_set()

    asyncio.run(scenario())


def test_authorization_revision_is_rechecked_after_productivity_read(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    entered, release = Event(), Event()
    original = productivity_read._page

    def held(*args):
        result = original(*args)
        entered.set()
        assert release.wait(10)
        return result

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, _, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                sdk = await _sdk_library_bridge(app.core)._get_client()
                await seed(sdk, f"cmh:{raw['instance_id']}:{raw['scope_id']}")
                monkeypatch.setattr(productivity_read, "_page", held)
                pending = asyncio.create_task(
                    web.post(
                        "/hub/v1/memories/list", headers=headers, json={"scope_id": raw["scope_id"]}
                    )
                )
                assert await asyncio.to_thread(entered.wait, 5)
                try:
                    await grant_reads(web, admin, raw, revision=3)
                finally:
                    release.set()
                response = await pending
                assert response.status_code == 409
                assert response.json() == {"detail": "read_authorization_changed"}

    asyncio.run(scenario())


def test_stale_index_scope_cannot_override_sqlite_and_reserved_ids_fail(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, _, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                bridge = _sdk_library_bridge(app.core)
                sdk = await bridge._get_client()
                namespace = f"cmh:{raw['instance_id']}:{raw['scope_id']}"
                native = await seed(sdk, namespace, variants=("working",))
                change(sdk, "UPDATE coding_memory_meta SET user_id='foreign'")
                rows = await ProductivityRead(bridge).search(namespace, QUERY, 10)
                assert [row.memory_id for group in rows for row in group] == [
                    record_id("w", "f", native)
                ]
                response = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": raw["scope_id"], "record_id": record_id("w", "e", native)},
                )
                assert response.status_code == 404
                reserved = record_id("w", "f", native)
                result = await _joined_thread(
                    sdk.import_memory_exact,
                    QUERY,
                    memory_id=reserved,
                    user_id=namespace,
                    agent_id="synthetic-agent",
                    layer="l2_fact",
                    request_id=str(uuid4()),
                )
                assert result["success"]
                for operation, extra in (
                    ("get", {"record_id": reserved}),
                    ("list", {}),
                    ("search", {"query": QUERY}),
                ):
                    response = await web.post(
                        "/hub/v1/memories/" + operation,
                        headers=headers,
                        json={"scope_id": raw["scope_id"], **extra},
                    )
                    assert response.status_code == 503
                    assert response.json() == {"detail": "read_unavailable"}

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["w", "c"])
@pytest.mark.parametrize("kind", ["e", "f"])
def test_identifiers_fit_existing_v1_contract_without_truncation(variant, kind):
    identity = record_id(variant, kind, "a" * 191)
    assert len(identity) == 200
    assert GetRequest(scope_id="global", record_id=identity).record_id == identity
    with pytest.raises(ValueError):
        record_id(variant, kind, "a" * 192)
