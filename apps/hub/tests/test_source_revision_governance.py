"""ADR-0034: accepted source revisions share a bounded, previewed version scope.

These tests establish governance scope, not cancellation of pending queue work.
"""

import asyncio
import json
from uuid import uuid4

import pytest
import test_chat_graph
from cloud_memory_hub import governance_inventory
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import plan_relations
from one_memory.api import _sdk_library_bridge
from one_memory.memory_server.durable_queue import PENDING
from test_chat_graph import produce
from test_governance_execution import EXECUTE, PREVIEW
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_remote_read import grant_reads


def revision(event, number, **changes):
    return {**event, "event_id": str(uuid4()), "revision": number, **changes}


def namespace(event):
    return f"cmh:{event['instance_id']}:{event['scope_id']}"


async def preview(web, admin, event, selected, *, action="forget"):
    body = {
        "scope_id": event["scope_id"],
        "action": action,
        "record_ids": [selected],
        "limit": 50,
    }
    reply = await web.post(PREVIEW, headers=admin, json=body)
    assert reply.status_code == 200, reply.text
    return body, reply.json()


def test_pending_revisions_link_by_stable_identity_in_revision_order(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            versions = {}
            for number in (4, 1, 9):
                versions[number] = "cmhe:" + await accepted(web, headers, revision(event, number))
            source = await web.post(
                "/hub/v1/sources",
                headers=admin,
                json={
                    "display_name": "Independent synthetic source",
                    "adapter_id": "synthetic.revisions",
                    "device_id": event["device_id"],
                    "scope_id": event["scope_id"],
                },
            )
            assert source.status_code == 201
            unrelated = set()
            for change in (
                {"source_id": source.json()["source_id"]},
                {"conversation_id": str(uuid4())},
                {"record_id": str(uuid4())},
            ):
                unrelated.add("cmhe:" + await accepted(web, headers, revision(event, 1, **change)))
            _, other_headers, _, other = await provision(web, config)
            other.update(
                conversation_id=event["conversation_id"],
                record_id=event["record_id"],
                content=event["content"],
            )
            other_original = "cmhe:" + await accepted(web, other_headers, other)
            nodes = app.core.state.cmh_lineage.inventory(namespace(event), {})
            assert other_original not in nodes
            assert nodes[versions[1]].versions == frozenset()
            assert nodes[versions[4]].versions == {versions[1]}
            assert nodes[versions[9]].versions == {versions[4]}
            assert all(not nodes[identity].versions for identity in unrelated)
            for number, selected in versions.items():
                assert json.loads(nodes[selected].content)["revision"] == number
                assert set(plan_relations(nodes, (selected,))["record_ids"]) == set(
                    versions.values()
                )
                _, view = await preview(web, admin, event, selected)
                assert {item["record_id"] for item in view["items"]} == set(versions.values())
                assert view["total"] == 3 and view["memory_count"] == 0
                assert all(item["layer"] == "accepted_original" for item in view["items"])
            for receipt in versions.values():
                queue = app.core.state.memory_write_queue
                frozen = queue.payload_for(receipt.removeprefix("cmhe:"))
                assert queue.get(frozen.idempotency_key).state == PENDING

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_new_revision_after_preview_refuses_execution_before_saving_intent(tmp_path, action):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            first = "cmhe:" + await accepted(web, headers, event)
            body, before = await preview(web, admin, event, first, action=action)
            second = "cmhe:" + await accepted(web, headers, revision(event, 2))
            reply = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    **body,
                    "expected_view": before["view_digest"],
                    "operation_id": str(uuid4()),
                },
            )
            assert reply.status_code == 409, reply.text
            assert reply.json() == {"detail": "governance_preview_changed"}
            assert app.core.state.cmh_executor.store.operations() == {}
            assert app.core.state.cmh_governance.journal.head(event["scope_id"]).sequence == 0
            _, current = await preview(web, admin, event, first, action=action)
            assert current["view_digest"] != before["view_digest"]
            assert {item["record_id"] for item in current["items"]} == {first, second}

    asyncio.run(scenario())


def test_completed_revision_outputs_and_pending_revision_share_confirmed_scope(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)
    original_accept = test_chat_graph.accepted
    stable = None
    count = 0

    async def accept_revision(web, headers, event):
        nonlocal stable, count
        if stable is None:
            stable = {key: event[key] for key in ("source_id", "conversation_id", "record_id")}
        count += 1
        event.update(stable, revision=count)
        return await original_accept(web, headers, event)

    monkeypatch.setattr(test_chat_graph, "accepted", accept_revision)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            await grant_reads(
                web,
                admin,
                event,
                permissions=("events:write", "memory:read", "history:read"),
                revision=3,
            )
            pending = await accepted(web, headers, revision(event, 4))
            originals = {"cmhe:" + job for job in [*jobs, pending]}
            outputs = {
                identity
                for result in results
                for identity in (result["memory_id"], *result["memory_ids"])
            }
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), namespace(event), app.core.state.cmh_lineage
            )
            selected = "cmhe:" + jobs[1]
            scoped = set(plan_relations(nodes, (selected,))["record_ids"])
            assert originals | outputs <= scoped
            body, view = await preview(web, admin, event, selected)
            assert {item["record_id"] for item in view["items"]} == scoped
            reply = await web.post(
                EXECUTE, headers=admin, json={**body, "expected_view": view["view_digest"]}
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["suppressed_count"] == len(scoped)
            store = app.core.state.cmh_executor.store
            assert set(store.records(reply.json()["operation_id"])) == scoped

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["edges", "records", "content"])
def test_long_revision_chain_is_linear_and_respects_inventory_budgets(
    tmp_path, monkeypatch, budget
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            receipts = []
            # Out-of-order, sparse revisions must still form a connected path.
            for number in reversed(range(1, 97, 2)):
                receipts.append("cmhe:" + await accepted(web, headers, revision(event, number)))
            nodes = app.core.state.cmh_lineage.inventory(namespace(event), {})
            assert len(nodes) == len(receipts)
            assert sum(len(node.versions) for node in nodes.values()) == len(receipts) - 1
            assert set(plan_relations(nodes, (receipts[len(receipts) // 2],))["record_ids"]) == set(
                receipts
            )
            limits = {
                "edges": ("MAX_EDGES", len(receipts) - 2),
                "records": ("MAX_RECORDS", len(receipts) - 1),
                "content": (
                    "MAX_CONTENT_BYTES",
                    sum(len(node.content.encode()) for node in nodes.values()) - 1,
                ),
            }
            setting, value = limits[budget]
            monkeypatch.setattr(governance_inventory, setting, value)
            with pytest.raises(GovernanceError, match="governance_scan_limit"):
                app.core.state.cmh_lineage.inventory(namespace(event), {})

    asyncio.run(scenario())
