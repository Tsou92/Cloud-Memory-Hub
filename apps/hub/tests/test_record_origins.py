import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from hy_memory.coding.input_capture import ReconcileInputCapture
from hy_memory.coding.reconciler import CodingMemoryReconciler
from hy_memory.coding.record_origin import verified_origin
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemoryDraft
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_original_archive import config_for
from test_output_lineage import fixture
from test_reconcile_input_capture import experience, fact


@pytest.mark.parametrize("variant", ["working", "coding"])
def test_atomic_origin_tracks_each_semantic_version_and_direct_edits_clear_it(tmp_path, variant):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def run():
                store = CodingMemoryStore(
                    sdk._config,
                    sdk._embed_service,
                    db_path=sdk._config.coding.db_path,
                    variant=variant,
                )
                await store.initialize()
                try:
                    old = experience(confidence=1)
                    await store.insert(old, origin_request_id="producer-a")
                    saved = await store.get_by_id("old", "scope")
                    assert verified_origin("experience", saved.to_dict()) == "producer-a"
                    capture = ReconcileInputCapture()
                    token = capture.begin("experience_decision", "experience", [saved], "old")
                    capture.end(token, "used")
                    frozen = capture.result()["stages"][0]["inputs"][0]["record"]

                    for request, text in (("producer-b", "middle"), ("producer-c", "latest")):
                        saved.solution = text
                        await store.update(saved, origin_request_id=request)
                        saved = await store.get_by_id("old", "scope")
                        assert verified_origin("experience", saved.to_dict()) == request
                    assert frozen["solution"] == "original solution"
                    assert verified_origin("experience", frozen) == "producer-a"
                    assert saved.origin_digest != frozen["origin_digest"]

                    # A plain caller cannot inherit the origin of the object it loaded.
                    saved.solution = "independent edit"
                    await store.update(saved)
                    saved = await store.get_by_id("old", "scope")
                    assert saved.origin_request_id is None and saved.origin_digest is None
                    assert verified_origin("experience", saved.to_dict()) is None

                    # Upsert keeps the database creation time, even if the caller supplies another.
                    replacement = experience(created_at=datetime(2020, 1, 1))
                    await store.insert(replacement, origin_request_id="replacement")
                    saved = await store.get_by_id("old", "scope")
                    assert saved.created_at != datetime(2020, 1, 1)
                    assert verified_origin("experience", saved.to_dict()) == "replacement"
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(run()))

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["working", "coding"])
def test_fact_graph_mutations_keep_content_origin_but_old_code_body_edits_fail(tmp_path, variant):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def run():
                store = CodingMemoryStore(
                    sdk._config,
                    sdk._embed_service,
                    db_path=sdk._config.coding.db_path,
                    variant=variant,
                )
                await store.initialize()
                try:
                    await store.insert_fact(
                        fact("before", details="old details"), origin_request_id="fact-producer"
                    )
                    await store.insert_fact(fact("after"), origin_request_id="next-producer")
                    await store.supersede_fact("before", "after")
                    old = await store.get_fact("before", "scope")
                    assert old.status == "superseded" and old.fact_link == "after"
                    assert verified_origin("fact", old.to_dict()) == "fact-producer"
                    with sqlite3.connect(store._db_path) as db:
                        db.execute(
                            "UPDATE working_fact SET derived_experience_ids=? WHERE fact_id=?",
                            (json.dumps(["new-experience"]), "before"),
                        )
                    old = await store.get_fact("before", "scope")
                    assert verified_origin("fact", old.to_dict()) == "fact-producer"
                    # Emulate the previous SDK updating content without knowing new columns.
                    with sqlite3.connect(store._db_path) as db:
                        db.execute(
                            "UPDATE working_fact SET statement='old SDK edit' "
                            "WHERE fact_id='before'"
                        )
                    old = await store.get_fact("before", "scope")
                    with pytest.raises(ValueError, match="productivity_origin_invalid"):
                        verified_origin("fact", old.to_dict())
                    await store.insert_fact(old)
                    old = await store.get_fact("before", "scope")
                    assert verified_origin("fact", old.to_dict()) is None
                    replacement = fact(
                        "before",
                        created_at=datetime(2020, 1, 1),
                        subject=None,
                        details=None,
                        fact_chain_summary=None,
                    )
                    await store.insert_fact(replacement, origin_request_id="replacement")
                    saved = await store.get_fact("before", "scope")
                    assert saved.created_at != datetime(2020, 1, 1)
                    assert verified_origin("fact", saved.to_dict()) == "replacement"
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(run()))

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["add", "error", "empty"])
def test_actual_reconciler_add_and_fallback_store_the_producing_request(tmp_path, mode):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def run():
                store = CodingMemoryStore(
                    sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path
                )
                await store.initialize()

                async def complete(**kwargs):
                    if mode == "error":
                        raise RuntimeError("synthetic provider failure")
                    return SimpleNamespace(
                        content=json.dumps(
                            {
                                "ops": []
                                if mode == "empty"
                                else [
                                    {"action": "ADD", "draft_idx": 0},
                                ]
                            }
                        )
                    )

                try:
                    reconciler = CodingMemoryReconciler(
                        store, sdk._embed_service, SimpleNamespace(complete=complete)
                    )
                    ops = await reconciler.reconcile(
                        [
                            CodingMemoryDraft(
                                task="Synthetic task",
                                solution="Synthetic solution",
                                user_id="scope",
                                agent_id="agent",
                            ),
                        ],
                        request_id="actual-producing-request",
                        user_id="scope",
                        agent_id="agent",
                    )
                    assert len(ops) == 1 and ops[0].action == "ADD"
                    saved = await store.get_by_id(ops[0].target_experience_id, "scope")
                    assert (
                        verified_origin("experience", saved.to_dict()) == "actual-producing-request"
                    )
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(run()))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "column,value",
    [
        ("origin_request_id", "different-producer"),
        ("origin_digest", "0" * 64),
        ("origin_digest", "非" * 64),
        ("origin_request_id", None),
        ("solution", "changed"),
        ("agent_id", "foreign"),
    ],
)
def test_origin_integrity_rejects_changed_body_identity_or_marker(column, value):
    from hy_memory.coding.record_origin import stamp_origin

    record = experience()
    stamp_origin(record, "experience", "producer")
    changed = record.to_dict()
    changed[column] = value
    with pytest.raises(ValueError, match="productivity_origin_invalid"):
        verified_origin("experience", changed)


def test_invalid_origin_cannot_replace_previously_committed_body(tmp_path):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def run():
                store = CodingMemoryStore(
                    sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path
                )
                await store.initialize()
                try:
                    await store.insert(experience(), origin_request_id="producer")
                    value = await store.get_by_id("old", "scope")
                    value.solution = "should not commit"
                    with pytest.raises(ValueError, match="productivity_origin_request_invalid"):
                        await store.update(value, origin_request_id="")
                    saved = await store.get_by_id("old", "scope")
                    assert saved.solution == "original solution"
                    assert verified_origin("experience", saved.to_dict()) == "producer"
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(run()))

    asyncio.run(scenario())
