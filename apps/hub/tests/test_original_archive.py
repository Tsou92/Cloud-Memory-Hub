import asyncio
import json
import sqlite3
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.original_archive import migrate_originals
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.source_history import SourceHistory
from cloud_memory_hub.storage_lease import StorageLease
from httpx import ASGITransport, AsyncClient
from one_memory.api import _sdk_library_bridge
from one_memory.memory_server.durable_queue import COMPLETED, DEAD
from test_remote_ingest import config_for as base_config
from test_remote_ingest import provision
from test_remote_read import grant_reads


def config_for(tmp_path):
    config = base_config(tmp_path)
    # Match the real CLI, which creates the data root privately before Core.
    config.data_dir.mkdir(mode=0o700)
    return config


def receipt_rows(config):
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM cmh_event_receipts")]


def legacy_schema(config):
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        db.execute("DROP INDEX cmh_receipt_identity")
        db.execute("DROP INDEX cmh_original_scope")
        db.execute("ALTER TABLE cmh_event_receipts DROP COLUMN payload_json")
        db.execute("UPDATE cmh_ingest_schema SET version=1")


async def stop_drain(app):
    await asyncio.sleep(0)
    app.core.state.memory_write_drain.stop()
    await app.core.state.memory_write_drain_task


@pytest.mark.parametrize("terminal", [COMPLETED, DEAD])
def test_originals_survive_queue_reaping_source_revocation_and_restart(tmp_path, terminal):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app)
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, _, raw = await provision(web, config)
                raw["content"]["messages"][0]["text"] = "原文" * 3000
                accepted = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert accepted.status_code == 202, accepted.text
                receipt = accepted.json()["receipt_id"]
                original_id = "cmhe:" + receipt
                expected = EventEnvelope.model_validate(raw).canonical_payload()
                assert receipt_rows(config)[0]["payload_json"] == expected
                queue = app.core.state.memory_write_queue
                queue.settle(receipt, state=terminal, hy_invoked=True)
                assert queue.clear_terminal_payloads() == 1
                assert queue.payload_for(receipt) is None
                duplicate = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert duplicate.status_code == 202 and duplicate.json()["receipt_id"] == receipt
                assert len(receipt_rows(config)) == 1
                status = await web.get("/hub/v1/events/" + raw["event_id"], headers=headers)
                assert status.json()["processing_state"] == terminal
                await grant_reads(web, admin, raw)
                native_id = str(uuid4())
                await _sdk_library_bridge(app.core).import_sdk_raw_exact(
                    content="SDK native history",
                    raw_id=native_id,
                    user_id=f"cmh:{raw['instance_id']}:{raw['scope_id']}",
                    agent_id="synthetic",
                    session_id="synthetic",
                    source="synthetic",
                    memory_at=None,
                    metadata={},
                    request_id=str(uuid4()),
                )
                ids, offset = [], 0
                while offset is not None:
                    page = await web.post(
                        "/hub/v1/history/list",
                        headers=headers,
                        json={"scope_id": raw["scope_id"], "limit": 1, "offset": offset},
                    )
                    assert page.status_code == 200, page.text
                    ids.extend(item["record_id"] for item in page.json()["items"])
                    offset = page.json()["next_offset"]
                assert ids == [original_id, native_id]
                response = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": raw["scope_id"], "record_id": original_id},
                )
                assert response.status_code == 404
                response = await web.post(
                    "/hub/v1/history/get",
                    headers=headers,
                    json={"scope_id": "global", "record_id": original_id},
                )
                assert response.status_code == 404
                revoked = await web.post(
                    f"/hub/v1/sources/{raw['source_id']}/revoke",
                    headers=admin,
                    json={"expected_revision": 1},
                )
                assert revoked.status_code == 200
                text, offset = "", 0
                while offset is not None:
                    reply = await web.post(
                        "/hub/v1/history/get",
                        headers=headers,
                        json={
                            "scope_id": raw["scope_id"],
                            "record_id": original_id,
                            "content_offset": offset,
                        },
                    )
                    assert reply.status_code == 200, reply.text
                    item = reply.json()["items"][0]
                    assert item["source"] == "future-agent.custom"
                    text += item["content"]
                    offset = item["next_content_offset"]
                assert json.loads(text) == json.loads(expected)
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                reply = await web.post(
                    "/hub/v1/history/get",
                    headers=headers,
                    json={"scope_id": raw["scope_id"], "record_id": original_id},
                )
                assert reply.status_code == 200
                assert reply.json()["items"][0]["content"] == text[:4096]
                await grant_reads(web, admin, raw, permissions=("memory:read",), revision=3)
                reply = await web.post(
                    "/hub/v1/history/get",
                    headers=headers,
                    json={"scope_id": raw["scope_id"], "record_id": original_id},
                )
                assert reply.status_code == 403

    asyncio.run(scenario())


def test_receipt_transaction_failure_replay_recovers_the_same_job_and_original(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app)
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                _, headers, _, raw = await provision(web, config)
                with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                    db.execute(
                        "CREATE TRIGGER refuse_original BEFORE INSERT ON cmh_event_receipts "
                        "BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END"
                    )
                failed = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert failed.status_code == 503
                assert receipt_rows(config) == []
                queue = app.core.state.memory_write_queue
                request_id = f"cmh:{raw['instance_id']}:{raw['device_id']}:{raw['event_id']}"
                job = queue.get(request_id)
                assert job is not None
                # Even if Core has already finished and reaped its input, the client
                # still owns the unacknowledged original and can complete acceptance.
                queue.settle(job.job_id, state=COMPLETED, hy_invoked=True)
                assert queue.clear_terminal_payloads() == 1
                with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                    db.execute("DROP TRIGGER refuse_original")
                accepted = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert accepted.status_code == 202
                assert accepted.json()["receipt_id"] == job.job_id
                assert (
                    receipt_rows(config)[0]["payload_json"]
                    == EventEnvelope.model_validate(raw).canonical_payload()
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["body", "digest", "scope"])
def test_corrupt_archive_never_confirms_success_or_publishes_content(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, _, raw = await provision(web, config)
                accepted = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert accepted.status_code == 202
                column, value = {
                    "body": ("payload_json", "{}"),
                    "digest": ("payload_digest", "bad"),
                    "scope": ("conversation_id", str(uuid4())),
                }[damage]
                with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                    db.execute(f"UPDATE cmh_event_receipts SET {column}=?", (value,))
                replay = await web.post("/hub/v1/events", headers=headers, json=raw)
                status = await web.get("/hub/v1/events/" + raw["event_id"], headers=headers)
                assert replay.status_code == status.status_code == 503
                assert replay.json() == status.json() == {"detail": "original_archive_invalid"}
                await grant_reads(web, admin, raw)
                result = await web.post(
                    "/hub/v1/history/list", headers=headers, json={"scope_id": raw["scope_id"]}
                )
                assert result.status_code == 503
                assert "items" not in result.json()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["conversation_turn", "operation"])
def test_explicit_migration_proves_originals_and_preserves_identity(tmp_path, kind):
    config = config_for(tmp_path)

    async def populate():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app)
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                _, headers, _, raw = await provision(web, config)
                raw["kind"] = kind
                if kind == "operation":
                    raw["content"] = {
                        "text": "Synthetic operation",
                        "metadata": {"command": "status"},
                    }
                else:
                    # A marker inside user content is data, not the final wrapper.
                    raw["content"]["messages"][0]["text"] += (
                        "\n\n<cmh_source_reference>\n{}\n</cmh_source_reference>"
                    )
                    raw["content"]["messages"][1]["metadata"] = {"synthetic": True}
                result = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert result.status_code == 202
        return raw

    raw = asyncio.run(populate())
    before = receipt_rows(config)
    legacy_schema(config)
    with pytest.raises(AccessError, match="original_retention_migration_required"):
        create_hub(config)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        assert migrate_originals(config) == 1
        assert migrate_originals(config) == 0
    assert receipt_rows(config) == before
    assert (
        receipt_rows(config)[0]["payload_json"]
        == EventEnvelope.model_validate(raw).canonical_payload()
    )
    create_hub(config)


@pytest.mark.parametrize("damage", ["reaped", "changed", "missing", "optional_empty"])
def test_unprovable_legacy_migration_rolls_back_all_rows_and_schema(tmp_path, damage):
    config = config_for(tmp_path)

    async def populate():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app)
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                _, headers, _, raw = await provision(web, config)
                assert (
                    await web.post("/hub/v1/events", headers=headers, json=raw)
                ).status_code == 202
                raw = {**raw, "event_id": str(uuid4()), "record_id": str(uuid4())}
                if damage == "optional_empty":
                    raw["content"]["messages"][0]["metadata"] = {}
                response = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert response.status_code == 202
                job = response.json()["receipt_id"]
                queue = app.core.state.memory_write_queue
                if damage == "reaped":
                    queue.settle(job, state=COMPLETED, hy_invoked=True)
                    queue.clear_terminal_payloads()
                elif damage in ("missing", "changed"):
                    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                        if damage == "missing":
                            db.execute("DELETE FROM memory_write_jobs WHERE job_id=?", (job,))
                        else:
                            db.execute(
                                "UPDATE memory_write_jobs SET payload_json='{}' WHERE job_id=?",
                                (job,),
                            )

    asyncio.run(populate())
    legacy_schema(config)
    before = receipt_rows(config)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        with pytest.raises(AccessError):
            migrate_originals(config)
    assert receipt_rows(config) == before
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        assert db.execute("SELECT version FROM cmh_ingest_schema").fetchone()[0] == 1


def test_original_read_rechecks_authorization_and_filters_upload_before_storage(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)
    entered, release = Event(), Event()
    original = SourceHistory._get

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
                bad = {
                    **raw,
                    "content": {"messages": [{"role": "user", "text": "password=" + uuid4().hex}]},
                }
                assert (
                    await web.post("/hub/v1/events", headers=headers, json=bad)
                ).status_code == 422
                assert receipt_rows(config) == []
                accepted = await web.post("/hub/v1/events", headers=headers, json=raw)
                assert accepted.status_code == 202
                await grant_reads(web, admin, raw)
                monkeypatch.setattr(SourceHistory, "_get", held)
                pending = asyncio.create_task(
                    web.post(
                        "/hub/v1/history/get",
                        headers=headers,
                        json={
                            "scope_id": raw["scope_id"],
                            "record_id": "cmhe:" + accepted.json()["receipt_id"],
                        },
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
