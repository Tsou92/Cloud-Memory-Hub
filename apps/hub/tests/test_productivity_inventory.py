import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.productivity_inventory import trace_id
from cloud_memory_hub.productivity_read import record_id
from hy_memory.coding.store import CodingMemoryStore, _variant_db_path
from hy_memory.coding.types import CodingMemory, WorkingFact
from hy_memory.data.vector_store_chroma import ChromaVectorStore
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_sdk_reconcile import sdk_request, seed_sdk

ENDPOINT = "/hub/v1/governance/preview"


async def seed(app, web, headers, event, scene, monkeypatch):
    job = await accepted(web, headers, event)
    queue = app.core.state.memory_write_queue
    frozen = queue.payload_for(job)
    bridge = _sdk_library_bridge(app.core)
    sdk = await bridge._get_client()
    request = sdk_request(frozen)
    ids, _ = await seed_sdk(sdk, bridge, request, scene, monkeypatch)
    await app.core.state.cmh_lineage.observe(job, frozen)
    queue.settle(job, state=COMPLETED, sdk_raw_ids=ids[0], sdk_memory_ids=ids[1])
    queue.clear_terminal_payloads()
    return sdk, request, ids, "cmhe:" + job


@pytest.mark.parametrize("scene", ["working", "coding"])
def test_actual_productivity_relations_originals_trace_index_copies_and_restart(
    tmp_path, monkeypatch, scene
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            sdk, request, (_, (experience, fact)), original = await seed(
                app, web, headers, event, scene, monkeypatch
            )
            second_event = {**event, "event_id": str(uuid4()), "record_id": str(uuid4())}
            _, _, (_, (independent_exp, independent_fact)), independent_original = await seed(
                app, web, headers, second_event, scene, monkeypatch
            )
            shared, old = str(uuid4()), str(uuid4())

            async def populate():
                store = CodingMemoryStore(
                    sdk._config,
                    sdk._embed_service,
                    db_path=sdk._config.coding.db_path,
                    variant=scene,
                )
                await store.initialize()
                try:
                    await store.insert(
                        CodingMemory(
                            experience_id=shared,
                            user_id=request.user_id,
                            task="Shared synthetic task",
                            solution="Shared synthetic solution",
                            source_fact_ids=[fact, independent_fact],
                        )
                    )
                    await store.insert_fact(
                        WorkingFact(
                            fact_id=old,
                            user_id=request.user_id,
                            statement="Previous fact",
                        )
                    )
                    await store.supersede_fact(old, fact)
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(populate()))
            trace = _variant_db_path(sdk._config.coding.db_path, "_trace")
            with sqlite3.connect(trace) as db:
                db.execute(
                    "INSERT INTO coding_trace(request_id,user_id,messages_json,created_at) "
                    "VALUES (?,?,?,?)",
                    (
                        request.request_id,
                        request.user_id,
                        '[{"role":"user","content":"Synthetic trace"}]',
                        "2026-01-01T00:00:00",
                    ),
                )
            selected = record_id(scene[0], "f", fact)
            body = {
                "scope_id": event["scope_id"],
                "action": "purge",
                "record_ids": [selected],
                "limit": 50,
            }
            result = await web.post(ENDPOINT, headers=admin, json=body)
            assert result.status_code == 200, result.text
            first = result.json()
            identities = {item["record_id"] for item in first["items"]}
            expected = {original, trace_id(request.request_id)} | {
                record_id(scene[0], kind, native)
                for kind, native in (("f", fact), ("f", old), ("e", experience), ("e", shared))
            }
            assert expected <= identities
            assert independent_original not in identities
            assert record_id(scene[0], "f", independent_fact) not in identities
            assert record_id(scene[0], "e", independent_exp) not in identities
            copies = [item for item in first["items"] if item["record_id"].startswith("cmhv:")]
            assert len(copies) == 3  # selected fact plus two dependent experiences
            assert all("derived" in item["reasons"] for item in copies)
            assert first["execution_available"] is False
            assert (await web.post(ENDPOINT, headers=headers, json=body)).status_code == 403
            database = _variant_db_path(
                sdk._config.coding.db_path, "_cv" if scene == "coding" else ""
            )
            with sqlite3.connect(database) as db:
                db.execute(
                    "UPDATE working_fact SET details='Changed independent fact' WHERE fact_id=?",
                    (independent_fact,),
                )
            changed = await web.post(
                ENDPOINT, headers=admin, json={**body, "expected_view": first["view_digest"]}
            )
            assert changed.status_code == 409
        async with fixture(config) as (app, web):
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            assert not sdk._coding_stores
            result = await web.post(ENDPOINT, headers=admin, json=body)
            assert result.status_code == 200, result.text
            assert {item["record_id"] for item in result.json()["items"]} == identities
            assert not sdk._coding_stores and sdk._chain_store is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage", ["all_links", "digest", "kind", "reference", "json", "state", "index", "chain"]
)
def test_corrupt_or_unmapped_stores_cannot_look_like_complete_scope(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            sdk, request, (_, (experience, fact)), original = await seed(
                app, web, headers, event, "working", monkeypatch
            )
            if damage in {"all_links", "digest", "kind"}:
                with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                    if damage == "all_links":
                        db.execute("DELETE FROM cmh_event_output_links")
                    elif damage == "digest":
                        db.execute("UPDATE cmh_event_outcomes SET links_digest='changed'")
                    else:
                        db.execute("UPDATE cmh_event_output_links SET kind='raw'")
                        from cloud_memory_hub.output_lineage import _digest

                        links = db.execute(
                            "SELECT kind,output_id FROM cmh_event_output_links"
                        ).fetchall()
                        db.execute(
                            "UPDATE cmh_event_outcomes SET links_digest=?", (_digest(links),)
                        )
            elif damage in {"reference", "json", "state"}:
                with sqlite3.connect(sdk._config.coding.db_path) as db:
                    if damage == "state":
                        db.execute(
                            "UPDATE working_fact SET status='unknown' WHERE fact_id=?", (fact,)
                        )
                    else:
                        value = '["foreign-fact"]' if damage == "reference" else "[bad"
                        db.execute(
                            "UPDATE coding_memory_meta SET source_fact_ids=? WHERE experience_id=?",
                            (value, experience),
                        )
            elif damage == "index":
                store = CodingMemoryStore(sdk._config, sdk._embed_service)
                await _joined_thread(
                    sdk._vector_store._client.delete_collection,
                    ChromaVectorStore(store._coding_config)._collection_name,
                )
            else:
                path = Path(sdk._config.coding.db_path).parent / "hy_code_chain"
                path.mkdir()
                with sqlite3.connect(path / "hy_code.db") as db:
                    db.execute("CREATE TABLE thread(owner TEXT)")
                    db.execute("INSERT INTO thread VALUES (?)", (request.user_id + "|workspace",))
            response = await web.post(
                ENDPOINT,
                headers=admin,
                json={"scope_id": event["scope_id"], "action": "purge", "record_ids": [original]},
            )
            assert response.status_code in (409, 503), response.text
            assert "items" not in response.json()
            assert app.core.state.cmh_governance.verify() == {}

    asyncio.run(scenario())


def test_combined_inventory_budget_includes_accepted_originals(tmp_path, monkeypatch):
    from cloud_memory_hub import governance_inventory

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, web):
            config = app.config
            _, headers, _, event = await provision(web, config)
            sdk, request, _, _ = await seed(app, web, headers, event, "chat", monkeypatch)
            monkeypatch.setattr(governance_inventory, "MAX_RECORDS", 2)
            with pytest.raises(GovernanceError, match="governance_scan_limit"):
                await observe_inventory(
                    _sdk_library_bridge(app.core), request.user_id, app.core.state.cmh_lineage
                )

    asyncio.run(scenario())


def test_same_native_ids_stay_separate_and_foreign_chain_is_not_read(tmp_path):
    async def scenario():
        config = config_for(tmp_path)
        async with fixture(config) as (app, web):
            _, _, _, event = await provision(web, config)
            namespace = f"cmh:{event['instance_id']}:{event['scope_id']}"
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            native = str(uuid4())

            async def populate():
                for variant in ("working", "coding"):
                    store = CodingMemoryStore(
                        sdk._config,
                        sdk._embed_service,
                        db_path=sdk._config.coding.db_path,
                        variant=variant,
                    )
                    await store.initialize()
                    try:
                        await store.insert(
                            CodingMemory(
                                experience_id=native,
                                user_id=namespace,
                                task="Synthetic shared ID",
                                solution="Distinct store",
                                source_fact_ids=[native],
                            )
                        )
                        await store.insert_fact(
                            WorkingFact(
                                fact_id=native,
                                user_id=namespace,
                                statement="Synthetic fact",
                                derived_experience_ids=[native],
                            )
                        )
                    finally:
                        await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(populate()))
            for suffix in ("", "_cv"):
                with sqlite3.connect(_variant_db_path(sdk._config.coding.db_path, suffix)) as db:
                    db.execute("UPDATE coding_memory_meta SET source_fact_ids='[]'")
            chain = Path(sdk._config.coding.db_path).parent / "hy_code_chain"
            chain.mkdir()
            with sqlite3.connect(chain / "hy_code.db") as db:
                db.execute("CREATE TABLE thread(owner TEXT, body TEXT)")
                db.execute(
                    "INSERT INTO thread VALUES (?,?)",
                    (namespace + "-foreign|work", "FOREIGN_SENTINEL"),
                )
            inventory = await observe_inventory(bridge, namespace)
            assert len(inventory) == 8  # four logical records, four physical index copies
            for variant in ("w", "c"):
                experience, fact = (record_id(variant, kind, native) for kind in ("e", "f"))
                assert inventory[experience].parents == frozenset({fact})
            assert all("FOREIGN_SENTINEL" not in node.content for node in inventory.values())
            with sqlite3.connect(sdk._config.coding.db_path) as db:
                db.execute("DELETE FROM working_fact WHERE fact_id=?", (native,))
            with pytest.raises(GovernanceError, match="governance_reference_unresolved"):
                await observe_inventory(bridge, namespace)

    asyncio.run(scenario())
