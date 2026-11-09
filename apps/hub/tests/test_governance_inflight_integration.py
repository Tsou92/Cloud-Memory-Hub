"""ADR-0038 / FR-10, FR-11: pending cancellation with unrelated native memory."""

import asyncio
import sqlite3
from uuid import uuid4

import pytest
from one_memory.api import _sdk_library_bridge
from one_memory.memory_server.durable_queue import COMPLETED
from test_chat_graph import produce
from test_governance_execution import (
    EXECUTE,
    PREVIEW,
    assert_inflight_is_not_extracted,
    read,
    t0342b_nodes,
)
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_read import grant_reads


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_uninvoked_target_can_complete_with_unrelated_native_records(tmp_path, monkeypatch, action):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            # Real SDK storage, result cache and lineage; produce substitutes
            # model output and invokes private SDK add to seed this fixture.
            # This is evidence about cancellation, not full supervisor coverage.
            jobs, requests, results, headers, event, _ = await produce(
                app, web, config, monkeypatch
            )
            admin = {"Authorization": "Bearer " + config.admin_token}
            await grant_reads(
                web,
                admin,
                event,
                permissions=("events:write", "memory:read", "history:read"),
                revision=3,
            )
            new_source = await web.post(
                "/hub/v1/sources",
                headers=admin,
                json={
                    "display_name": "Independent pending source",
                    "adapter_id": "synthetic.pending",
                    "device_id": event["device_id"],
                    "scope_id": event["scope_id"],
                },
            )
            assert new_source.status_code == 201, new_source.text
            target = {
                **event,
                **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
                "source_id": new_source.json()["source_id"],
                "content": {
                    "messages": [
                        {
                            "role": "user",
                            "text": "Synthetic pending decision about a separate warehouse map.",
                        }
                    ]
                },
            }
            pending = await accepted(web, headers, target)
            queue = app.core.state.memory_write_queue
            with sqlite3.connect(queue.database_path) as db:
                original_payload = db.execute(
                    "SELECT payload_json FROM memory_write_jobs WHERE job_id=?", (pending,)
                ).fetchone()[0]
                unrelated_rows = db.execute(
                    "SELECT * FROM memory_write_jobs WHERE job_id != ? ORDER BY job_id",
                    (pending,),
                ).fetchall()
            assert all(queue.get(request.request_id).state == COMPLETED for request in requests)

            sdk = await _sdk_library_bridge(app.core)._get_client()
            namespace = f"cmh:{event['instance_id']}:{event['scope_id']}"

            async def native_snapshot():
                return {
                    node.node_id: (node.content, node.status.value, node.is_latest)
                    for node in await t0342b_nodes(sdk, namespace)
                }

            native_before = await native_snapshot()
            native_ids = {
                identity
                for result in results
                for identity in (result["memory_id"], *result["memory_ids"])
            }
            assert native_ids and native_ids <= native_before.keys()
            originals = ["cmhe:" + job for job in jobs]
            memories = [identity for result in results for identity in result["memory_ids"]]
            assert all(native_before[identity][1:] == ("active", True) for identity in memories)
            readable_before = {}
            for kind, identities in (("history", originals), ("memories", memories)):
                for identity in identities:
                    response = await read(web, headers, event["scope_id"], kind, identity)
                    assert response.status_code == 200, response.text
                    readable_before[kind, identity] = response.json()["items"]

            body = {
                "scope_id": event["scope_id"],
                "action": action,
                "record_ids": ["cmhe:" + pending],
            }
            view = await web.post(PREVIEW, headers=admin, json=body)
            assert view.status_code == 200, view.text
            assert {item["record_id"] for item in view.json()["items"]} == {"cmhe:" + pending}
            reply = await web.post(
                EXECUTE, headers=admin, json={**body, "expected_view": view.json()["view_digest"]}
            )
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "completed"
            await assert_inflight_is_not_extracted(
                app,
                monkeypatch,
                pending,
                action=action,
                original_payload=original_payload,
                web=web,
                headers=headers,
                event=target,
            )
            assert await native_snapshot() == native_before
            for (kind, identity), items in readable_before.items():
                response = await read(web, headers, event["scope_id"], kind, identity)
                assert response.status_code == 200, response.text
                assert response.json()["items"] == items
            with sqlite3.connect(queue.database_path) as db:
                assert (
                    db.execute(
                        "SELECT * FROM memory_write_jobs WHERE job_id != ? ORDER BY job_id",
                        (pending,),
                    ).fetchall()
                    == unrelated_rows
                )

    asyncio.run(scenario())
