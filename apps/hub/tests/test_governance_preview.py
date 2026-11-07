import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_inventory
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import PreviewRequest, RelationNode, plan_relations
from cloud_memory_hub.remote_access import AccessError
from httpx import ASGITransport, AsyncClient
from one_memory.api import _sdk_library_bridge
from pydantic import ValidationError
from test_remote_ingest import config_for, provision
from test_remote_read import grant_reads

ENDPOINT = "/hub/v1/governance/preview"


def node(identity, *, raw=False, parents=(), versions=(), status="active"):
    return RelationNode(
        identity,
        "raw" if raw else "memory",
        "Synthetic " + identity,
        "l1_raw" if raw else "l2_fact",
        status,
        frozenset(parents),
        frozenset(versions),
    )


def test_directional_closure_preserves_independent_sources_and_handles_cycles():
    nodes = {
        item.record_id: item
        for item in (
            node("A", raw=True),
            node("B", raw=True),
            node("X", parents=("A",), versions=("old",)),
            node("old", versions=("X",), status="superseded"),
            node("Z", parents=("B",)),
            node("Y", parents=("X", "Z"), status="archived"),
        )
    }
    plan = plan_relations(nodes, ("X",))
    assert set(plan["record_ids"]) == {"A", "X", "old", "Y"}
    assert set(plan["roots"]) == {"A", "X", "old"}
    assert "source" in plan["reasons"]["A"]
    assert "derived" in plan["reasons"]["Y"]
    assert set(plan_relations(nodes, ("Y",))["record_ids"]) == set(nodes)
    assert set(plan_relations(nodes, ("old",))["record_ids"]) == {"A", "X", "old", "Y"}
    nodes["A"] = replace(nodes["A"], parents=frozenset({"X"}))
    assert plan_relations(nodes, ("X",))["record_ids"] == plan["record_ids"]


@pytest.mark.parametrize("relation", ["parents", "versions"])
def test_missing_or_foreign_reference_is_not_resolved_outside_inventory(relation):
    selected = replace(node("X"), **{relation: frozenset({"outside"})})
    with pytest.raises(GovernanceError, match="^governance_reference_unresolved$"):
        plan_relations({"X": selected}, ("X",))
    with pytest.raises(GovernanceError, match="^governance_selection_not_found$"):
        plan_relations({"X": selected}, ("outside",))


def test_cross_kind_versions_are_not_silently_merged():
    with pytest.raises(GovernanceError, match="^governance_version_kind_conflict$"):
        plan_relations({"A": node("A", raw=True), "X": node("X", versions=("A",))}, ("X",))


@pytest.mark.parametrize(
    "change",
    [
        {"record_ids": []},
        {"record_ids": ["X", "X"]},
        {"record_ids": ["../X"]},
        {"limit": 51},
        {"offset": True},
        {"actor_id": "administrator"},
        {"action": "execute"},
    ],
)
def test_preview_request_rejects_ambiguous_selection_or_execution(change):
    with pytest.raises(ValidationError):
        PreviewRequest.model_validate(
            {
                "scope_id": "global",
                "action": "forget",
                "record_ids": ["X"],
                **change,
            }
        )


async def seed(bridge, namespace, ids):
    sdk = await bridge._get_client()
    for key in ("raw", "independent"):
        await bridge.import_sdk_raw_exact(
            content="Synthetic conversation " + key,
            raw_id=ids[key],
            user_id=namespace,
            agent_id="synthetic-agent",
            session_id="synthetic-session",
            source="synthetic",
            memory_at=None,
            metadata={},
            request_id=str(uuid4()),
        )
    for key, options in (
        ("old", {"status": "superseded", "is_latest": False, "superseded_by": [ids["head"]]}),
        ("head", {"supersedes": [ids["old"]], "metadata": {"source_raw_memory_ids": [ids["raw"]]}}),
        ("other", {"metadata": {"evidence_raw_ids": [ids["independent"]]}}),
        (
            "summary",
            {"status": "archived", "metadata": {"source_memory_ids": [ids["head"], ids["other"]]}},
        ),
        ("anchor", {"layer": "l3_summary"}),
        ("foreign", {"user_id": "cmh:synthetic:foreign"}),
    ):
        result = await asyncio.to_thread(
            sdk.import_memory_exact,
            "Synthetic " + key,
            memory_id=ids[key],
            **{"user_id": namespace, "agent_id": "synthetic-agent", **options},
        )
        assert result["success"]

    async def links():
        assert await sdk._vector_store.update_payload(
            ids["anchor"], {"source_raw_memory_id": ids["raw"]}
        )

    await asyncio.to_thread(lambda: sdk._loop_thread.run(links()))
    return sdk


def test_real_sdk_http_preview_paging_scope_privacy_and_no_mutation(tmp_path):
    config = config_for(tmp_path)
    ids = {
        key: str(uuid4())
        for key in ("raw", "independent", "old", "head", "other", "summary", "anchor", "foreign")
    }
    ids["anchor"] = "synthetic:anchor"

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, device, _, envelope = await provision(web, config)
                await grant_reads(web, admin, envelope)
                bridge = _sdk_library_bridge(app.core)
                scope = envelope["scope_id"]
                namespace = f"cmh:{envelope['instance_id']}:{scope}"
                sdk = await seed(bridge, namespace, ids)
                body = {"scope_id": scope, "action": "forget", "record_ids": [ids["head"]]}
                assert (await web.post(ENDPOINT, headers=device, json=body)).status_code == 403
                assert (await web.post(ENDPOINT, json=body)).status_code == 403
                assert (
                    await web.post(
                        ENDPOINT, headers=admin, json={**body, "record_ids": [ids["foreign"]]}
                    )
                ).status_code == 404
                assert (
                    await web.post(ENDPOINT, headers=admin, json={**body, "scope_id": str(uuid4())})
                ).status_code == 404
                first = await web.post(ENDPOINT, headers=admin, json={**body, "limit": 2})
                assert first.status_code == 200, first.text
                assert first.headers["cache-control"] == "no-store"
                page = first.json()
                assert page["consistency"] == "observational"
                assert page["execution_available"] is False
                assert (page["total"], page["memory_count"], page["raw_count"]) == (5, 4, 1)
                items = page["items"]
                while page["next_offset"] is not None:
                    response = await web.post(
                        ENDPOINT,
                        headers=admin,
                        json={
                            **body,
                            "offset": page["next_offset"],
                            "limit": 2,
                            "expected_view": page["view_digest"],
                        },
                    )
                    assert response.status_code == 200, response.text
                    page = response.json()
                    items += page["items"]
                assert {item["record_id"] for item in items} == {
                    ids[key] for key in ("raw", "old", "head", "summary", "anchor")
                }
                assert len(items) == 5
                assert (
                    next(i for i in items if i["kind"] == "raw")["proposed_effect"]
                    == "retain_for_explicit_read"
                )
                purge = (
                    await web.post(ENDPOINT, headers=admin, json={**body, "action": "purge"})
                ).json()
                assert purge["total"] == 5
                assert all(i["proposed_effect"] == "remove_hub_content" for i in purge["items"])
                assert purge["view_digest"] != page["view_digest"]
                assert app.core.state.cmh_governance.verify() == {}
                assert (await bridge.get_sdk_memory(ids["summary"])).status == "archived"
                assert (
                    await bridge.get_sdk_raw(ids["raw"])
                ).content == "Synthetic conversation raw"

                added = await asyncio.to_thread(
                    sdk.import_memory_exact,
                    "Synthetic new descendant",
                    memory_id=str(uuid4()),
                    user_id=namespace,
                    agent_id="synthetic-agent",
                    metadata={"source_memory_ids": [ids["head"]]},
                )
                assert added["success"]
                assert (
                    await web.post(
                        ENDPOINT,
                        headers=admin,
                        json={
                            **body,
                            "expected_view": page["view_digest"],
                        },
                    )
                ).status_code == 409
                newer = (await web.post(ENDPOINT, headers=admin, json=body)).json()
                assert newer["total"] == 6
                page = newer

                async def mutate():
                    original = await sdk._vector_store.get_by_id(ids["head"])
                    assert await sdk._vector_store.update_payload(
                        ids["head"],
                        {
                            "content": "x" * 490
                            + " password=synthetic-credential-value\n"
                            + config.admin_token,
                            # Synthetic storage mutation only. Supplying the stored
                            # vector avoids Chroma's unrelated default embedder.
                            "embedding": original.embedding,
                        },
                    )

                await asyncio.to_thread(lambda: sdk._loop_thread.run(mutate()))
                stale = await web.post(
                    ENDPOINT, headers=admin, json={**body, "expected_view": page["view_digest"]}
                )
                assert stale.status_code == 409
                assert stale.json() == {"detail": "governance_preview_changed"}
                updated = await web.post(ENDPOINT, headers=admin, json=body)
                assert updated.status_code == 200, updated.text
                assert (
                    "synthetic-credential" not in updated.text
                    and config.admin_token not in updated.text
                )

                code, _ = app.sessions.issue_exchange()
                web.cookies.set("cmh_session", app.sessions.consume_exchange(code))
                assert (await web.post(ENDPOINT, json=body)).json() == {"detail": "origin_required"}
                assert (
                    await web.post(ENDPOINT, headers={"Origin": config.public_origin}, json=body)
                ).status_code == 200
                assert (
                    await web.post(
                        ENDPOINT, headers={"Origin": "https://foreign.invalid"}, json=body
                    )
                ).status_code == 403

    asyncio.run(scenario())


def test_real_store_faults_limits_and_dangling_links_fail_explicitly(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    ids = {
        key: str(uuid4())
        for key in ("raw", "independent", "old", "head", "other", "summary", "anchor", "foreign")
    }

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            bridge = _sdk_library_bridge(app.core)
            namespace = f"cmh:{app.core.state.cmh_governance.access.instance_id}:global"
            sdk = await seed(bridge, namespace, ids)
            service = app.core.state.cmh_previews
            request = PreviewRequest(scope_id="global", action="forget", record_ids=(ids["head"],))
            with monkeypatch.context() as patch:
                patch.setattr(governance_inventory, "MAX_RECORDS", 2)
                with pytest.raises(AccessError, match="governance_scan_limit"):
                    await service.preview(request)
            path = Path(sdk._config.graph_store.db_path).with_name("graph")
            path.mkdir()
            with pytest.raises(AccessError, match="governance_graph_unavailable"):
                await service.preview(request)
            path.rmdir()

            async def update(values):
                assert await sdk._vector_store.update_payload(ids["head"], values)

            for values, error in (
                (
                    {"custom": {"source_raw_memory_ids": [ids["foreign"]]}},
                    "governance_reference_unresolved",
                ),
                (
                    {"custom": {"source_raw_memory_ids": "not-a-list"}},
                    "governance_reference_invalid",
                ),
                ({"custom": "invalid-json"}, "governance_reference_invalid"),
                (
                    {"custom": '{"source_memory_ids":["missing"],"source_memory_ids":[]}'},
                    "governance_reference_invalid",
                ),
                ({"node_id": str(uuid4())}, "governance_record_identity_mismatch"),
            ):
                # Keep every fault independent of the previous damaged metadata.
                await asyncio.to_thread(
                    lambda values=values: sdk._loop_thread.run(
                        update(
                            {
                                "node_id": ids["head"],
                                "custom": {"source_raw_memory_ids": [ids["raw"]]},
                                **values,
                            }
                        )
                    )
                )
                with pytest.raises(AccessError, match=error):
                    await service.preview(request)

    asyncio.run(scenario())


def test_preview_slot_remains_owned_until_read_finishes(tmp_path, monkeypatch):
    from cloud_memory_hub import governance_preview

    async def scenario():
        app = create_hub(config_for(tmp_path))
        service = app.core.state.cmh_previews
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            started.set()
            await release.wait()
            return {"X": node("X")}

        monkeypatch.setattr(governance_preview, "observe_inventory", delayed)
        service.bridge = lambda: None
        request = PreviewRequest(scope_id="global", action="forget", record_ids=("X",))
        first = asyncio.create_task(service.preview(request))
        await started.wait()
        try:
            with pytest.raises(AccessError, match="governance_preview_busy"):
                await service.preview(request)
        finally:
            release.set()
            await first
        assert (await service.preview(request))["unresolved_origin_count"] == 1

    asyncio.run(scenario())


def test_cancelled_preview_joins_formatter_before_releasing_its_budget(tmp_path, monkeypatch):
    from cloud_memory_hub import governance_preview

    async def scenario():
        app = create_hub(config_for(tmp_path))
        service = app.core.state.cmh_previews
        started, release = Event(), Event()
        original = service._format

        async def inventory(*args):
            return {"X": node("X")}

        def slow_format(request, nodes):
            started.set()
            assert release.wait(5)
            return original(request, nodes)

        monkeypatch.setattr(governance_preview, "observe_inventory", inventory)
        monkeypatch.setattr(service, "_format", slow_format)
        service.bridge = lambda: None
        request = PreviewRequest(scope_id="global", action="forget", record_ids=("X",))
        first = asyncio.create_task(service.preview(request))
        assert await asyncio.to_thread(started.wait, 5)
        first.cancel()
        try:
            with pytest.raises(AccessError, match="governance_preview_busy"):
                await service.preview(request)
            assert not first.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
        assert (await service.preview(request))["total"] == 1

    asyncio.run(scenario())


DERIVATION_FLAGS = (
    ("mode", "ultra"),
    ("llm.enable_intention", True),
    ("recall.episode_summary_enabled", True),
    ("recall.session_episode_enabled", True),
    ("recall.series_episode_enabled", True),
    ("graph_store.topic_summary_enabled", True),
)


def test_unmodeled_derived_writers_are_off_and_refuse_preview(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            # The real runtime configuration passes; nothing below is a stand-in.
            governance_inventory.require_modeled_derivations(sdk._config)
            service = app.core.state.cmh_previews
            request = PreviewRequest(scope_id="global", action="forget", record_ids=("missing",))
            for path, value in DERIVATION_FLAGS:
                owner, _, name = path.rpartition(".")
                target = sdk._config if not owner else getattr(sdk._config, owner)
                previous = getattr(target, name)
                setattr(target, name, value)
                try:
                    with pytest.raises(AccessError, match="governance_derivation_unmodeled"):
                        await service.preview(request)
                finally:
                    setattr(target, name, previous)

    asyncio.run(scenario())
