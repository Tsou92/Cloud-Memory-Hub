import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.events import EventEnvelope, prepare_event
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.remote_access import AccessError, GrantRequest, RevisionRequest
from cloud_memory_hub.remote_ingest import RemoteIngest, write_request
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.preproc import has_any_tool_message, strip_tool_messages
from hy_memory.pipelines.base import ChatMessage
from one_memory.api import _source_writes_on
from one_memory.imports.models import WrittenBy
from one_memory.memory_processing import MemoryTaskRunner


def config_for(tmp_path):
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        "http://localhost:8800",
        "a" * 40,
        Fernet.generate_key(),
        governance_dir=tmp_path / "governance",
    )
    activate_authority(config.config_dir, config.governance_dir, config.encryption_key, new=True)
    return config


async def provision(client, config):
    admin = {"Authorization": "Bearer " + config.admin_token}
    project = (
        await client.post(
            "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic project"}
        )
    ).json()["project_id"]
    issued = (
        await client.post(
            "/hub/v1/devices", headers=admin, json={"display_name": "Synthetic connector"}
        )
    ).json()
    device_id = issued["device"]["device_id"]
    token = issued["credential"]
    headers = {"Authorization": "Bearer " + token}
    await client.put(
        f"/hub/v1/devices/{device_id}/grants",
        headers=admin,
        json={
            "expected_revision": 1,
            "grants": [{"scope_id": project, "permissions": ["events:write"]}],
        },
    )
    source_reply = await client.post(
        "/hub/v1/sources",
        headers=admin,
        json={
            "display_name": "Future agent",
            "adapter_id": "future-agent.custom",
            "device_id": device_id,
            "scope_id": project,
        },
    )
    assert source_reply.status_code == 201
    source_id = source_reply.json()["source_id"]
    handshake = (await client.get("/hub/v1/session", headers=headers)).json()
    envelope = {
        "instance_id": handshake["instance_id"],
        "device_id": device_id,
        "source_id": source_id,
        "scope_id": project,
        **{key: str(uuid4()) for key in ("event_id", "conversation_id", "record_id")},
        "revision": 1,
        "kind": "conversation_turn",
        "occurred_at": "2026-10-23T08:00:00+00:00",
        "content": {
            "messages": [
                {"role": "user", "text": "决定复用上游队列。"},
                {"role": "assistant", "text": "下一步验证断网补传。"},
            ],
            "metadata": {"milestone": "remote-intake"},
        },
    }
    return admin, headers, token, envelope


def count_jobs(config):
    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
        return db.execute("SELECT COUNT(*) FROM memory_write_jobs").fetchone()[0]


def test_local_outbox_to_real_core_queue_replay_restart_and_scoped_inputs(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                admin, headers, token, raw = await provision(client, config)
                sample = uuid4().hex
                raw["content"]["messages"].append(
                    {"role": "tool", "text": "password=" + sample, "metadata": {"cookie": sample}}
                )
                outbox = LocalOutbox.initialize(
                    tmp_path / "connector" / "queue.sqlite3",
                    instance_id=raw["instance_id"],
                    device_id=raw["device_id"],
                )
                outbox.enqueue(raw)
                outgoing = outbox.pending()[0]
                assert sample not in outgoing.payload
                posted = await client.post(
                    "/hub/v1/events",
                    headers={**headers, "Content-Type": "application/json"},
                    content=outgoing.payload,
                )
                assert posted.status_code == 202, posted.text
                receipt = posted.json()
                assert receipt["status"] == "accepted" and receipt["processing_state"] == "pending"
                assert receipt["payload_digest"] == outgoing.payload_digest
                assert sample not in posted.text and token not in posted.text
                again = await client.post(
                    "/hub/v1/events",
                    headers={**headers, "Content-Type": "application/json"},
                    content=outgoing.payload,
                )
                assert again.json() == receipt and count_jobs(config) == 1
                outbox.acknowledge(
                    **{
                        key: receipt[key]
                        for key in (
                            "instance_id",
                            "device_id",
                            "event_id",
                            "payload_digest",
                            "receipt_id",
                        )
                    }
                )
                assert outbox.status()["accepted"] == 1 and outbox.pending() == ()
                status = await client.get("/hub/v1/events/" + raw["event_id"], headers=headers)
                assert status.json() == receipt
                request = app.core.state.memory_write_queue.payload_for(receipt["receipt_id"])
                assert request is not None and len(request.input_records) == 3
                assert {record.user_id for record in request.input_records} == {
                    f"cmh:{raw['instance_id']}:{raw['scope_id']}"
                }
                assert {record.workspace for record in request.input_records} == {raw["scope_id"]}
                assert "remote-intake" in request.input_records[0].verbatim
                assert raw["event_id"] in request.input_records[0].verbatim
                assert _source_writes_on(app.core, "cmh:" + raw["source_id"])
                for path in config.data_dir.glob("one-memory.sqlite3*"):
                    assert sample.encode() not in path.read_bytes()
                assert (await client.get("/hub/v1/sources", headers=headers)).status_code == 403
                assert (
                    len((await client.get("/hub/v1/sources", headers=admin)).json()["sources"]) == 1
                )
        restarted = create_hub(config)
        async with restarted.core.router.lifespan_context(restarted.core):
            async with AsyncClient(
                transport=ASGITransport(restarted), base_url=config.public_origin
            ) as client:
                status = await client.get("/hub/v1/events/" + raw["event_id"], headers=headers)
                assert (
                    status.status_code == 200
                    and status.json()["receipt_id"] == receipt["receipt_id"]
                )
                repeated = await client.post(
                    "/hub/v1/events",
                    headers={**headers, "Content-Type": "application/json"},
                    content=outgoing.payload,
                )
                assert repeated.status_code == 202 and count_jobs(config) == 1

    asyncio.run(scenario())


def test_cross_source_scope_plaintext_conflicts_and_revocation_are_rejected(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                admin, headers, token, raw = await provision(client, config)
                _, other_headers, _, other = await provision(client, config)
                for change in (
                    {"source_id": other["source_id"]},
                    {"scope_id": other["scope_id"]},
                    {"scope_id": "global"},
                    {"device_id": other["device_id"]},
                ):
                    assert (
                        await client.post("/hub/v1/events", headers=headers, json={**raw, **change})
                    ).status_code == 403
                assert (
                    await client.post(
                        "/hub/v1/events", headers=headers, json={**raw, "instance_id": str(uuid4())}
                    )
                ).status_code == 409
                dirty = {
                    **raw,
                    "content": {"messages": [{"role": "user", "text": "password=" + token}]},
                }
                refused = await client.post("/hub/v1/events", headers=headers, json=dirty)
                assert refused.status_code == 422 and token not in refused.text
                assert count_jobs(config) == 0
                assert (
                    await client.post("/hub/v1/events", headers=headers, json=raw)
                ).status_code == 202
                different = {
                    **raw,
                    "content": {"messages": [{"role": "user", "text": "Changed intent"}]},
                }
                assert (
                    await client.post("/hub/v1/events", headers=headers, json=different)
                ).status_code == 409
                assert (
                    await client.post(
                        "/hub/v1/events", headers=headers, json={**raw, "event_id": str(uuid4())}
                    )
                ).status_code == 409
                assert count_jobs(config) == 1
                assert (
                    await client.get("/hub/v1/events/" + raw["event_id"], headers=other_headers)
                ).status_code == 404
                revoked = await client.post(
                    f"/hub/v1/sources/{raw['source_id']}/revoke",
                    headers=admin,
                    json={"expected_revision": 1},
                )
                assert revoked.status_code == 200
                assert not _source_writes_on(app.core, "cmh:" + raw["source_id"])
                assert (
                    await client.post("/hub/v1/events", headers=headers, json=raw)
                ).status_code == 403
                assert (
                    await client.get("/hub/v1/events/" + raw["event_id"], headers=headers)
                ).status_code == 403
                assert (
                    await client.post(
                        f"/hub/v1/sources/{raw['source_id']}/revoke",
                        headers=admin,
                        json={"expected_revision": 1},
                    )
                ).status_code == 409

    asyncio.run(scenario())


def test_core_commit_then_receipt_failure_recovers_same_job(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                _, _, token, raw = await provision(client, config)
                ingest = app.core.state.cmh_ingest
                original = ingest._database

                @contextmanager
                def interrupted(*, writing=False):
                    with original(writing=writing) as db:
                        yield db
                        if writing:
                            raise RuntimeError("synthetic_receipt_commit_failure")

                with monkeypatch.context() as patch:
                    patch.setattr(ingest, "_database", interrupted)
                    with pytest.raises(RuntimeError, match="synthetic_receipt_commit_failure"):
                        ingest.accept(token, EventEnvelope.model_validate(raw))
                assert count_jobs(config) == 1
                with original() as db:
                    assert db.execute("SELECT COUNT(*) FROM cmh_event_receipts").fetchone()[0] == 0
                receipt = ingest.accept(token, EventEnvelope.model_validate(raw))
                assert receipt["status"] == "accepted" and count_jobs(config) == 1
                assert ingest.accept(token, EventEnvelope.model_validate(raw)) == receipt

    asyncio.run(scenario())


def test_device_revoke_serializes_with_in_progress_acceptance(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                _, _, token, raw = await provision(client, config)
                ingest, queue = app.core.state.cmh_ingest, app.core.state.memory_write_queue
                entered, release, revoking = Event(), Event(), Event()
                submit = queue.submit

                def held(request):
                    entered.set()
                    assert release.wait(5)
                    return submit(request)

                def revoke():
                    revoking.set()
                    return ingest.access.revoke(
                        raw["device_id"], RevisionRequest(expected_revision=2)
                    )

                with monkeypatch.context() as patch:
                    patch.setattr(queue, "submit", held)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        acceptance = pool.submit(
                            ingest.accept, token, EventEnvelope.model_validate(raw)
                        )
                        assert entered.wait(5)
                        revocation = pool.submit(revoke)
                        assert revoking.wait(5) and not revocation.done()
                        release.set()
                        assert acceptance.result(5)["status"] == "accepted"
                        assert revocation.result(5)["status"] == "revoked"
                assert not _source_writes_on(app.core, "cmh:" + raw["source_id"])
                with pytest.raises(AccessError, match="invalid_device_credential"):
                    ingest.accept(token, EventEnvelope.model_validate(raw))
                assert count_jobs(config) == 1

    asyncio.run(scenario())


def test_terminal_payload_reaping_keeps_receipt_and_does_not_requeue(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                _, headers, token, raw = await provision(client, config)
                ingest, queue = app.core.state.cmh_ingest, app.core.state.memory_write_queue
                event = EventEnvelope.model_validate(raw)
                receipt = ingest.accept(token, event)
                # Inject a terminal outcome to exercise retention, not model extraction.
                queue.settle(receipt["receipt_id"], state="completed", hy_invoked=True)
                assert queue.clear_terminal_payloads() == 1
                assert queue.payload_for(receipt["receipt_id"]) is None
                replay = await client.post("/hub/v1/events", headers=headers, json=raw)
                assert replay.status_code == 202
                assert replay.json()["receipt_id"] == receipt["receipt_id"]
                assert replay.json()["processing_state"] == "completed"
                # The same recovery also works if only the Hub receipt was lost.
                with ingest._database(writing=True) as db:
                    db.execute("DELETE FROM cmh_event_receipts")
                assert ingest.accept(token, event) == replay.json()
                assert count_jobs(config) == 1

    asyncio.run(scenario())


def test_intake_validates_json_contract_and_authenticates_before_body(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                _, headers, _, raw = await provision(client, config)
                for change in (
                    {"protocol_version": True},
                    {"revision": "1"},
                    {"content": {"messages": [{"role": "system", "text": "invalid"}]}},
                    {"content": {"messages": [], "extra": "unexpected"}},
                    {"content": {"messages": [{"role": "user", "text": "a", "extra": "b"}]}},
                ):
                    response = await client.post(
                        "/hub/v1/events", headers=headers, json={**raw, **change}
                    )
                    assert response.status_code == 422
                duplicate = await client.post(
                    "/hub/v1/events",
                    headers={**headers, "Content-Type": "application/json"},
                    content='{"revision":1,"revision":2}',
                )
                assert duplicate.status_code == 422
                oversized = b" " * (1024 * 1024 + 4097)
                assert (
                    await client.post(
                        "/hub/v1/events",
                        headers={**headers, "Content-Type": "application/json"},
                        content=oversized,
                    )
                ).status_code == 413
                assert (
                    await client.post(
                        "/hub/v1/events",
                        content=oversized,
                        headers={
                            "Authorization": "Bearer invalid",
                            "Content-Type": "application/json",
                        },
                    )
                ).status_code == 401
                assert count_jobs(config) == 0

    asyncio.run(scenario())


def test_project_grant_removal_controls_existing_source_and_extension_fails_closed(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                _, _, _, raw = await provision(client, config)
                ingest = app.core.state.cmh_ingest
                ingest.access.replace_grants(
                    raw["device_id"], GrantRequest(expected_revision=2, grants=[])
                )
                assert not ingest.source_write_authorizer("cmh:" + raw["source_id"])
                assert ingest.source_write_authorizer("claude-code") is None
                with ingest.access._connection(writing=True) as db:
                    db.execute("UPDATE cmh_ingest_schema SET version=99")
                assert ingest.source_write_authorizer("cmh:" + raw["source_id"]) is False
                with pytest.raises(AccessError, match="version_unsupported"):
                    RemoteIngest(ingest.access, lambda: None)

    asyncio.run(scenario())


def test_projection_is_deterministic_and_preserves_operation_metadata():
    raw = {
        key: str(uuid4())
        for key in (
            "event_id",
            "instance_id",
            "device_id",
            "source_id",
            "scope_id",
            "conversation_id",
            "record_id",
        )
    }
    raw.update(
        revision=3,
        kind="operation",
        content={
            "text": "Build failed; retry after dependency fix.",
            "metadata": {"command": "make check", "exit_code": 2},
        },
    )
    event = prepare_event(raw, CredentialFilter())
    first, second = write_request(event), write_request(event)
    assert first == second
    assert first.input_records[0].revision == 3
    assert "make check" in first.input_records[0].verbatim
    assert first.input_records[0].role == "tool"
    global_event = event.model_copy(update={"scope_id": "global"})
    assert write_request(global_event).input_records[0].user_id != first.input_records[0].user_id


def _remember_event(metadata):
    raw = {
        key: str(uuid4())
        for key in (
            "event_id",
            "instance_id",
            "device_id",
            "source_id",
            "scope_id",
            "conversation_id",
            "record_id",
        )
    }
    return prepare_event(
        {
            **raw,
            "revision": 1,
            "kind": "operation",
            "content": {"text": "合成项目的发布代号固定为银杉。", "metadata": metadata},
        },
        CredentialFilter(),
    )


def test_explicit_remember_preserves_agent_content_through_real_sdk_preprocessing():
    metadata = {
        "agent_access_kind": "remember",
        "agent_access_version": 1,
        "synthetic_context": "a confirmed project decision",
    }
    event = _remember_event(metadata)
    original_payload = event.canonical_payload()
    projected = write_request(event)
    request = MemoryTaskRunner.request_for(projected.request_key, projected.input_records)
    messages = [ChatMessage.from_dict(turn.model_dump()) for turn in request.turns]
    # Use the SDK's actual routing predicate and filter: no judge or model mock
    # can hide a tool-only input becoming an empty writer request.
    assert not has_any_tool_message(messages)
    retained = strip_tool_messages(messages)
    assert len(retained) == 1 and event.content["text"] in retained[0].content
    record = projected.input_records[0]
    assert record.role == "assistant" and record.written_by is WrittenBy.AGENT
    assert record.workspace == event.scope_id and record.project == event.scope_id
    assert record.source_id == "cmh:" + event.source_id
    text, _, footer = record.verbatim.partition("\n\n<cmh_source_reference>\n")
    provenance = json.loads(footer.removesuffix("\n</cmh_source_reference>"))
    assert text == event.content["text"]
    assert provenance["content_metadata"] == metadata
    assert provenance["event"]["event_id"] == event.event_id
    assert event.canonical_payload() == original_payload
    assert write_request(event) == projected


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"agent_access_kind": "remember"},
        {"agent_access_kind": "remember", "agent_access_version": True},
        {"agent_access_kind": "remember", "agent_access_version": 1.0},
        {"agent_access_kind": "remember", "agent_access_version": "1"},
        {"agent_access_kind": "remember", "agent_access_version": 2},
        {"agent_access_kind": "remember", "agent_access_version": None},
        {"agent_access_kind": "handoff", "agent_access_version": 1},
        {"agent_access_kind": "Remember", "agent_access_version": 1},
    ],
)
def test_other_operation_markers_remain_tool_output(metadata):
    record = write_request(_remember_event(metadata)).input_records[0]
    assert record.role == "tool" and record.written_by is WrittenBy.TOOL


def test_remember_projection_does_not_rewrite_accepted_frozen_jobs(tmp_path, monkeypatch):
    import cloud_memory_hub.remote_ingest as ingest_module
    import cloud_memory_hub.sdk_generation as generation_module

    config = config_for(tmp_path)
    current_projection = write_request

    def previous_projection(event, *, request_id=None):
        request = current_projection(event, request_id=request_id)
        return request.model_copy(
            update={
                "input_records": tuple(
                    record.model_copy(update={"role": "tool", "written_by": WrittenBy.TOOL})
                    for record in request.input_records
                )
            }
        )

    async def scenario():
        app = create_hub(config)
        async with (
            app.core.router.lifespan_context(app.core),
            AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as client,
        ):
            _, headers, _, raw = await provision(client, config)
            raw.update(
                kind="operation",
                content={
                    "text": "合成旧记忆：发布代号为银杉。",
                    "metadata": {"agent_access_kind": "remember", "agent_access_version": 1},
                },
            )
            event = prepare_event(raw, CredentialFilter())
            with monkeypatch.context() as legacy:
                legacy.setattr(ingest_module, "write_request", previous_projection)
                legacy.setattr(generation_module, "write_request", previous_projection)
                accepted = await client.post(
                    "/hub/v1/events", headers=headers, json=event.model_dump(mode="json")
                )
            assert accepted.status_code == 202, accepted.text
            receipt = accepted.json()
            queue = app.core.state.memory_write_queue
            before = queue.payload_for(receipt["receipt_id"])
            assert before.input_records[0].role == "tool"
            assert write_request(event).input_records[0].role == "assistant"
            replayed = await client.post(
                "/hub/v1/events", headers=headers, json=event.model_dump(mode="json")
            )
            assert replayed.status_code == 202 and replayed.json() == receipt
            assert count_jobs(config) == 1
            assert queue.payload_for(receipt["receipt_id"]) == before

    asyncio.run(scenario())


def test_upstream_callback_preserves_local_policy_and_callback_errors_deny(tmp_path, monkeypatch):
    import one_memory.api as upstream
    from one_memory.config import Settings

    monkeypatch.setattr(
        upstream, "_source_processing_enabled", lambda app, source: source == "local-source"
    )
    settings = Settings(data_dir=tmp_path)
    app = upstream.create_app(
        settings, source_write_authorizer=lambda source: True if source == "cmh:owned" else None
    )
    assert _source_writes_on(app, "cmh:owned") and _source_writes_on(app, "local-source")
    assert not _source_writes_on(app, "unknown")

    def broken(source):
        raise RuntimeError("synthetic_authorizer_failure")

    app.state.source_write_authorizer = broken
    assert not _source_writes_on(app, "cmh:owned")
