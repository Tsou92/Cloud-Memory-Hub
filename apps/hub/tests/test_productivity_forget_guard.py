"""Reject unsupported productivity forgets; this is not a working forget implementation."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_execution
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_execution import ExecuteRequest
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError, Intent
from cloud_memory_hub.governance_plan import plan_relations
from cloud_memory_hub.governance_requests import request_targets
from cloud_memory_hub.governance_sources import capture_rules
from cloud_memory_hub.productivity_read import record_id
from hy_memory.coding.fact_reconciler import WorkingFactReconciler
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import WorkingFactDraft
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_governance_execution import EXECUTE, PREVIEW, read
from test_original_archive import config_for
from test_output_lineage import fixture
from test_productivity_inventory import seed
from test_remote_ingest import provision
from test_remote_read import grant_reads

ERROR = "governance_forget_kind_unsupported"
UNSUPPORTED = ("cmhp:", "cmht:", "cmhc:", "cmhv:")


async def prepare(app, web, config, scene, monkeypatch):
    admin, headers, _, event = await provision(web, config)
    await grant_reads(
        web, admin, event, permissions=("events:write", "memory:read", "history:read")
    )
    sdk, request, (_, (_, fact)), original = await seed(
        app, web, headers, event, scene, monkeypatch
    )
    body = {"scope_id": event["scope_id"], "action": "forget", "record_ids": [original]}
    view = await web.post(PREVIEW, headers=admin, json=body)
    assert view.status_code == 200, view.text
    identities = {item["record_id"] for item in view.json()["items"]}
    assert original in identities
    assert record_id(scene[0], "f", fact) in identities
    assert any(identity.startswith("cmhv:") for identity in identities)
    body.update(expected_view=view.json()["view_digest"], operation_id=str(uuid4()))
    return admin, headers, body, sdk, request, fact, original


async def inventory(app, request):
    return await observe_inventory(
        _sdk_library_bridge(app.core), request.user_id, app.core.state.cmh_lineage
    )


async def internal_fact_input(sdk, request, scene, fact):
    """Real native candidate selection and prompt; only the model answer is synthetic."""

    async def inspect():
        store = CodingMemoryStore(
            sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=scene
        )
        await store.initialize()
        try:
            stored = await store.get_fact(fact, user_id=request.user_id)
            prompts = []

            async def complete(**kwargs):
                prompts.append(kwargs["prompt"])
                return SimpleNamespace(content='{"ops":[]}')

            reconciler = WorkingFactReconciler(
                store, sdk._embed_service, SimpleNamespace(complete=complete), temperature=0.1
            )
            drafts = [WorkingFactDraft(statement="Synthetic fact")]
            candidates = await reconciler._retrieve_candidates(
                drafts,
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                workspace_id=None,
                branch=None,
                source="default",
            )
            await reconciler._llm_decide(drafts, candidates)
            return (
                stored.status,
                any(item.fact_id == fact for group in candidates for item in group),
                any(fact in prompt and "Synthetic fact" in prompt for prompt in prompts),
            )
        finally:
            await store.close()

    return await _joined_thread(lambda: sdk._loop_thread.run(inspect()))


async def historical_plan(app, body, request, *, committed, completed=False):
    """Persist a real signed old-operation fixture, without bypassing the new HTTP guard."""
    executor = app.core.state.cmh_executor
    nodes = await inventory(app, request)
    selected = plan_relations(nodes, body["record_ids"])["record_ids"]
    records = {identity: nodes[identity].kind for identity in selected}
    journal = executor.authority.journal
    targets = {
        journal.target(body["scope_id"], kind, identity) for identity, kind in records.items()
    }
    targets.update(capture_rules(journal, executor.lineage.ingest, body["scope_id"], records))
    targets.update(request_targets(journal, ExecuteRequest.model_validate(body), executor.actor_id))
    intent = Intent(
        operation_id=body["operation_id"],
        actor_id=executor.actor_id,
        scope_id=body["scope_id"],
        action="forget",
        targets=tuple(targets),
    )
    executor.store.plan(intent, records)
    if committed:
        executor.authority.append(intent)
    if completed:
        assert committed
        executor.store.complete(intent)
    return intent, records


@pytest.mark.parametrize("scene", ["working", "coding"])
def test_new_productivity_forget_refuses_complete_closure_before_plan(tmp_path, monkeypatch, scene):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, body, sdk, request, fact, original = await prepare(
                app, web, config, scene, monkeypatch
            )
            executor = app.core.state.cmh_executor
            before = await inventory(app, request)
            raw = await read(web, headers, body["scope_id"], "history", original)
            assert raw.status_code == 200, raw.text
            before_input = await internal_fact_input(sdk, request, scene, fact)
            assert before_input == ("active", True, True)

            response = await web.post(EXECUTE, headers=admin, json=body)
            assert response.status_code == 503, response.text
            assert response.json() == {"detail": ERROR}
            assert executor.store.operations() == {}
            assert executor.authority.journal.head(body["scope_id"]).sequence == 0
            assert executor.ready is True
            executor.authority.require_writes_ready()
            assert await inventory(app, request) == before
            after = await read(web, headers, body["scope_id"], "history", original)
            assert after.status_code == 200 and after.json() == raw.json()
            assert (
                await read(
                    web, headers, body["scope_id"], "memories", record_id(scene[0], "f", fact)
                )
            ).status_code == 200
            # Refusal preserves the original data; it does not implement native forgetting.
            assert await internal_fact_input(sdk, request, scene, fact) == before_input

    asyncio.run(scenario())


@pytest.mark.parametrize("scene", ["working", "coding"])
@pytest.mark.parametrize("completed", [False, True], ids=["planned", "completed"])
def test_signed_committed_productivity_forget_refuses_retry_finish_and_restart(
    tmp_path, monkeypatch, scene, completed
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, _, body, sdk, request, fact, _ = await prepare(
                app, web, config, scene, monkeypatch
            )
            before = await internal_fact_input(sdk, request, scene, fact)
            intent, records = await historical_plan(
                app, body, request, committed=True, completed=completed
            )
            executor = app.core.state.cmh_executor
            response = await web.post(EXECUTE, headers=admin, json=body)
            assert response.status_code == 503, response.text
            assert response.json() == {"detail": ERROR}
            async with executor._exclusive():
                with pytest.raises(GovernanceError, match="^" + ERROR + "$"):
                    await executor._finish(intent, records)
            state = "completed" if completed else "planned"
            assert executor.store.operations()[intent.operation_id] == (state, intent)
            assert executor.authority.journal.head(body["scope_id"]).sequence == 1
            assert await internal_fact_input(sdk, request, scene, fact) == before

        restarted = create_hub(config)
        with pytest.raises(GovernanceError, match="^" + ERROR + "$"):
            async with restarted.core.router.lifespan_context(restarted.core):
                pytest.fail("unsupported committed forget must not finish startup")
        executor = restarted.core.state.cmh_executor
        assert executor.ready is False
        assert executor.store.operations()[intent.operation_id] == (state, intent)
        assert executor.authority.journal.head(body["scope_id"]).sequence == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("scene", ["working", "coding"])
@pytest.mark.parametrize("recovery", ["retry", "restart"])
def test_uncommitted_productivity_plan_is_discarded_without_permanent_write_block(
    tmp_path, monkeypatch, scene, recovery
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, _, body, _, request, _, _ = await prepare(app, web, config, scene, monkeypatch)
            await historical_plan(app, body, request, committed=False)
            if recovery == "retry":
                response = await web.post(EXECUTE, headers=admin, json=body)
                assert response.status_code == 503 and response.json() == {"detail": ERROR}
                assert app.core.state.cmh_executor.store.operations() == {}
                assert app.core.state.cmh_executor.ready is True
                app.core.state.cmh_governance.require_writes_ready()

        async with fixture(config) as (app, web):
            executor = app.core.state.cmh_executor
            assert executor.ready is True
            assert executor.store.operations() == {}
            assert executor.authority.journal.head(body["scope_id"]).sequence == 0
            executor.authority.require_writes_ready()
            response = await web.post(EXECUTE, headers=admin, json=body)
            assert response.status_code == 503 and response.json() == {"detail": ERROR}
            assert executor.store.operations() == {}
            assert executor.ready is True

    asyncio.run(scenario())


@pytest.mark.parametrize("prefix", UNSUPPORTED)
def test_forget_guard_rejects_each_productivity_storage_prefix(prefix):
    with pytest.raises(GovernanceError, match="^" + ERROR + "$"):
        governance_execution.require_forget_supported(
            {"cmhe:original": "raw", prefix + "id": "raw"}
        )


@pytest.mark.parametrize("prefix", ["cmhe:", "cmhi:", "cmhj:", "cmhm:", "cmhb:", ""])
def test_forget_guard_preserves_supported_original_snapshot_and_native_ids(prefix):
    governance_execution.require_forget_supported([prefix + str(uuid4())])
