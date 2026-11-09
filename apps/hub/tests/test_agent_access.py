"""Synthetic real-HTTP acceptance/replay and scope checks for FR-01/02/05."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_transport
from cloud_memory_hub.agent_access import HANDOFF_METADATA, AgentAccess, AgentAccessError
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import ReadItem, ReadReply
from cloud_memory_hub.transport import RemoteConnection
from one_memory.hy_memory_bridge import HySdkMemoryRecord
from test_read_transport import json_response
from test_remote_ingest import count_jobs
from test_transport import bind_live, connection_data

live_hub = test_transport.live_hub


@pytest.fixture(autouse=True)
def preserve_pytest_log_handlers(request):
    # Repeated live Hub startup can apply the native formatter to pytest's
    # shared capture handlers. Restore them only after live_hub has stopped;
    # the running Hub still uses its unchanged strict diagnostic policy.
    plugin = request.config.pluginmanager.get_plugin("logging-plugin")
    with pytest.MonkeyPatch.context() as patch:
        if plugin is not None:
            for handler in (plugin.caplog_handler, plugin.report_handler):
                patch.setattr(handler, "formatter", handler.formatter)
                patch.setattr(handler, "handleError", handler.handleError)
        yield


def access_for(path, connection, raw, *, privacy=None, transport=None):
    outbox = LocalOutbox.initialize(
        path / "outbox.sqlite3",
        instance_id=connection.instance_id,
        device_id=connection.device_id,
        privacy=privacy,
    )
    return AgentAccess(
        connection,
        outbox,
        scope_id=raw["scope_id"],
        source_id=raw["source_id"],
        transport=transport,
    )


async def read_grants(client, admin, device_id, scope_id, *, revision=2):
    response = await client.put(
        f"/hub/v1/devices/{device_id}/grants",
        headers=admin,
        json={
            "expected_revision": revision,
            "grants": [
                {
                    "scope_id": scope_id,
                    "permissions": ["events:write", "history:read", "memory:read"],
                }
            ],
        },
    )
    assert response.status_code == 200


async def second_device(client, admin, connection, scope_id):
    issued = (
        await client.post(
            "/hub/v1/devices", headers=admin, json={"display_name": "Synthetic second Agent"}
        )
    ).json()
    device_id = issued["device"]["device_id"]
    await read_grants(client, admin, device_id, scope_id, revision=1)
    source = (
        await client.post(
            "/hub/v1/sources",
            headers=admin,
            json={
                "display_name": "Synthetic second source",
                "adapter_id": "future-agent.custom",
                "device_id": device_id,
                "scope_id": scope_id,
            },
        )
    ).json()
    return RemoteConnection(
        origin=connection.origin,
        instance_id=connection.instance_id,
        device_id=device_id,
        credential=issued["credential"],
    ), source


def test_tcp_handoff_pages_immediately_readable_on_second_device(live_hub, tmp_path):
    config, _, uploaded = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        first = access_for(tmp_path / "first", connection, raw)
        remembered = await first.remember("合成记忆: 保存后可在另一端原文列表读取。")
        document = {
            "goal": "合成目标" * 700,
            "state": "已完成本地合成检查" * 200,
            "decisions": ["保留原文,不依赖后台提炼。"],
            "next_steps": ["下一端读取这条交接。"],
            "blockers": [],
        }
        saved = await first.handoff_save(**document)
        assert saved["status"] == "accepted"
        assert saved["handoff_id"] == "cmhe:" + saved["receipt_id"]
        assert saved["processing_state"] == "not_checked"
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            other, source = await second_device(client, admin, connection, raw["scope_id"])
        second = access_for(tmp_path / "second", other, source)
        loaded = await second.handoff_load(saved["handoff_id"])
        assert loaded["handoff"] == document
        assert loaded["source_id"] == raw["source_id"]
        assert loaded["content_trust"] == "untrusted_history"
        recent = await second.recent(limit=2)
        assert recent["items"][0]["record_id"] == saved["handoff_id"]
        assert recent["items"][0]["next_content_offset"] == 4096
        assert recent["items"][1]["record_id"] == "cmhe:" + remembered["receipt_id"]
        assert json.loads(recent["items"][1]["content"])["content"]["text"].startswith("合成记忆")
        assert len(uploaded) == 2 and count_jobs(config) == 2

    asyncio.run(scenario())


def test_tcp_cross_project_and_revoked_read_permissions_fail(live_hub, tmp_path):
    config, _, _ = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        first = access_for(tmp_path / "first", connection, raw)
        saved = await first.handoff_save(goal="合成目标", state="合成状态")
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            project = (
                await client.post(
                    "/hub/v1/projects", headers=admin, json={"display_name": "Other synthetic"}
                )
            ).json()["project_id"]
            other, source = await second_device(client, admin, connection, project)
            second = access_for(tmp_path / "second", other, source)
            with pytest.raises(AgentAccessError, match="remote_endpoint_unavailable"):
                await second.handoff_load(saved["handoff_id"])
            await read_grants(client, admin, other.device_id, raw["scope_id"])
            # The method still cannot select the newly authorized other scope.
            with pytest.raises(AgentAccessError, match="scope_permission_required"):
                await second.handoff_load(saved["handoff_id"])
            same = AgentAccess(
                other, second.outbox, scope_id=raw["scope_id"], source_id=source["source_id"]
            )
            assert (await same.handoff_load(saved["handoff_id"]))["handoff"]["goal"] == "合成目标"
            assert (
                await client.put(
                    f"/hub/v1/devices/{other.device_id}/grants",
                    headers=admin,
                    json={"expected_revision": 3, "grants": []},
                )
            ).status_code == 200
            with pytest.raises(AgentAccessError, match="scope_permission_required"):
                await same.handoff_load(saved["handoff_id"])

    asyncio.run(scenario())


def test_tcp_offline_filtered_outbox_reopens_and_replays_once(live_hub, tmp_path):
    config, app, uploaded = live_hub

    async def scenario():
        connection, _, _, raw = await bind_live(config)
        secret = uuid4().hex
        privacy = CredentialFilter((secret,))
        first = access_for(tmp_path / "first", connection, raw, privacy=privacy)
        request_id = str(uuid4())
        text = "合成记忆 " + secret + " " + connection.credential.get_secret_value()
        app.core.state.transport_test_offline = True
        saved = await first.remember(text, request_id=request_id)
        assert saved["status"] == "queued" and saved["receipt_id"] is None
        assert saved["error"] == "remote_temporarily_unavailable" and saved["retryable"]
        assert secret.encode() not in first.outbox.path.read_bytes()
        assert (
            connection.credential.get_secret_value().encode() not in first.outbox.path.read_bytes()
        )
        assert (await first.status())["delivery"] == "not_attempted"
        assert count_jobs(config) == 0 and uploaded == []
        reopened = LocalOutbox(
            first.outbox.path,
            instance_id=connection.instance_id,
            device_id=connection.device_id,
            privacy=privacy,
        )
        second = AgentAccess(
            connection, reopened, scope_id=raw["scope_id"], source_id=raw["source_id"]
        )
        app.core.state.transport_test_offline = False
        status = await second.status(sync=True)
        assert status["queued"] == 0 and status["accepted"] == 1
        retried = await second.remember(text, request_id=request_id)
        assert retried["status"] == "accepted" and retried["event_id"] == request_id
        assert count_jobs(config) == 1 and len(uploaded) == 1
        with pytest.raises(AgentAccessError, match="request_identity_conflict"):
            await second.remember("另一段正文", request_id=request_id)
        assert count_jobs(config) == 1

    asyncio.run(scenario())


def test_tcp_memory_search_hydrates_scope_and_filters_query(live_hub, tmp_path):
    config, app, _ = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        secret = uuid4().hex
        service = access_for(
            tmp_path / "first", connection, raw, privacy=CredentialFilter((secret,))
        )
        record = HySdkMemoryRecord(
            memory_id=str(uuid4()),
            content="合成已提炼记忆",
            layer="l2_fact",
            status="active",
            is_latest=True,
            user_id=f"cmh:{connection.instance_id}:{raw['scope_id']}",
            agent_id="synthetic",
        )
        queries = []

        async def search(query, **kwargs):
            queries.append((query, kwargs))
            return (SimpleNamespace(memory_id=record.memory_id, member_ids=()),)

        async def get(_):
            return record

        app.core.state.cmh_reads.bridge = lambda: SimpleNamespace(
            search_memories=search, get_sdk_memory=get
        )
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, raw["scope_id"])
        result = await service.search("合成任务 " + secret)
        assert result["items"][0]["record_id"] == record.memory_id
        assert result["content_trust"] == "untrusted_history"
        assert secret not in queries[0][0]
        record = record.model_copy(update={"user_id": f"cmh:{connection.instance_id}:global"})
        assert (await service.search("合成任务"))["items"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,arguments,code",
    [
        ("remember", {"text": " "}, "remember_invalid"),
        ("remember", {"text": "a" * 16385}, "remember_invalid"),
        ("remember", {"text": "合成", "request_id": "not-a-uuid"}, "request_identity_invalid"),
        (
            "handoff_save",
            {"goal": "目标", "state": "状态", "decisions": ["a"] * 9},
            "handoff_invalid",
        ),
        ("handoff_save", {"goal": "目标", "state": "状态", "blockers": [True]}, "handoff_invalid"),
        ("handoff_load", {"handoff_id": str(uuid4())}, "handoff_id_invalid"),
        ("status", {"sync": 1}, "status_request_invalid"),
    ],
)
def test_invalid_action_has_no_local_or_network_write(tmp_path, method, arguments, code):
    connection = RemoteConnection.model_validate(connection_data())
    calls = []

    def unexpected(request):
        calls.append(request)
        raise AssertionError("Invalid action must not call HTTP")

    service = access_for(
        tmp_path / "first",
        connection,
        {"source_id": str(uuid4()), "scope_id": str(uuid4())},
        transport=httpx.MockTransport(unexpected),
    )
    with pytest.raises(AgentAccessError, match=code):
        asyncio.run(getattr(service, method)(**arguments))
    assert service.outbox.status() == {"queued": 0, "accepted": 0, "rejected": {}}
    assert calls == []


def test_tcp_remember_json_is_not_a_handoff(live_hub, tmp_path):
    config, _, _ = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        service = access_for(tmp_path / "first", connection, raw)
        saved = await service.remember(json.dumps({"goal": "目标", "state": "状态"}))
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, raw["scope_id"])
        with pytest.raises(AgentAccessError, match="handoff_original_invalid"):
            await service.handoff_load("cmhe:" + saved["receipt_id"])

    asyncio.run(scenario())


def test_tcp_lost_response_handoff_replay_keeps_receipt(live_hub, tmp_path):
    config, _, uploaded = live_hub

    class LostResponse(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.url.path == "/hub/v1/events":
                await response.aclose()
                raise httpx.ReadError("synthetic lost response")
            return response

        async def aclose(self):
            await self.inner.aclose()

    async def scenario():
        connection, _, _, raw = await bind_live(config)
        first = access_for(tmp_path / "first", connection, raw, transport=LostResponse())
        request_id, session_id = str(uuid4()), str(uuid4())
        arguments = {
            "goal": "合成交接",
            "state": "服务端接收后连接丢失",
            "request_id": request_id,
            "session_id": session_id,
        }
        queued = await first.handoff_save(**arguments)
        assert queued["status"] == "queued" and queued["handoff_id"] is None
        assert queued["error"] == "transport_unavailable" and count_jobs(config) == 1
        reopened = LocalOutbox(
            first.outbox.path, instance_id=connection.instance_id, device_id=connection.device_id
        )
        second = AgentAccess(
            connection, reopened, scope_id=raw["scope_id"], source_id=raw["source_id"]
        )
        accepted = await second.handoff_save(**arguments)
        assert accepted["status"] == "accepted" and accepted["handoff_id"] is not None
        assert count_jobs(config) == 1 and len(uploaded) == 2 and uploaded[0] == uploaded[1]
        assert (await second.handoff_save(**arguments)) == accepted
        assert len(uploaded) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("label", ["password=", "密码: ", "api_key="])
def test_tcp_paged_handoff_preserves_filtered_json_with_credential_labels(
    live_hub, tmp_path, label
):
    config, _, uploaded = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        service = access_for(tmp_path / "first", connection, raw)
        document = {"goal": "g" * 4096, "state": label + "synthetic_private_value"}
        saved = await service.handoff_save(**document)
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, raw["scope_id"])
        loaded = await service.handoff_load(saved["handoff_id"])
        assert loaded["handoff"]["goal"] == document["goal"]
        assert loaded["handoff"]["state"] == service.privacy.protect(document).value["state"]
        assert "synthetic_private_value" not in json.dumps(loaded)
        assert b"synthetic_private_value" not in uploaded[0]

    asyncio.run(scenario())


def original_transport(connection, event, text, offsets, *, changed_page=False):
    def handler(request):
        if request.url.path == "/hub/v1/session":
            return json_response(
                {
                    "protocol_version": 1,
                    "instance_id": connection.instance_id,
                    "features": ["scoped_reads"],
                    "device": {
                        "device_id": connection.device_id,
                        "revision": 1,
                        "status": "active",
                        "grants": [{"scope_id": event.scope_id, "permissions": ["history:read"]}],
                    },
                }
            )
        assert request.url.path == "/hub/v1/history/get"
        body = json.loads(request.content)
        offset = body["content_offset"]
        offsets.append(offset)
        return json_response(
            ReadReply(
                instance_id=connection.instance_id,
                device_id=connection.device_id,
                device_revision=1,
                scope_id=event.scope_id,
                operation="history-get",
                items=(
                    ReadItem(
                        record_id=body["record_id"],
                        kind="history",
                        content=text[offset : offset + 4096],
                        content_offset=offset,
                        next_content_offset=offset + 4096 if len(text) > offset + 4096 else None,
                        agent_id="cmh:" + event.source_id,
                        session_id=(
                            str(uuid4()) if changed_page and offset else event.conversation_id
                        ),
                    ),
                ),
            ).model_dump(mode="json")
        )

    return httpx.MockTransport(handler)


def synthetic_original(connection, content):
    return EventEnvelope(
        instance_id=connection.instance_id,
        device_id=connection.device_id,
        **{
            key: str(uuid4())
            for key in ("event_id", "conversation_id", "record_id", "scope_id", "source_id")
        },
        revision=1,
        kind="operation",
        content=content,
    )


@pytest.mark.parametrize(
    "fault", ["boolean_version", "extra_field", "wrong_scope", "duplicate_key"]
)
def test_original_structure_and_metadata_are_not_inferred(tmp_path, fault):
    connection = RemoteConnection.model_validate(connection_data())
    content = {
        "text": json.dumps({"goal": "synthetic", "state": "paused"}),
        "metadata": HANDOFF_METADATA,
    }
    if fault == "boolean_version":
        content["metadata"] = {**HANDOFF_METADATA, "agent_access_version": True}
    elif fault == "extra_field":
        content["text"] = json.dumps(
            {"goal": "synthetic", "state": "paused", "execute": "untrusted"}
        )
    elif fault == "duplicate_key":
        content["text"] = '{"goal":"one","goal":"two","state":"paused"}'
    event = synthetic_original(connection, content)
    text = event.canonical_payload()
    if fault == "wrong_scope":
        text = event.model_copy(update={"scope_id": str(uuid4())}).canonical_payload()
    offsets = []
    service = access_for(
        tmp_path / "first",
        connection,
        event.model_dump(),
        transport=original_transport(connection, event, text, offsets),
    )
    with pytest.raises(AgentAccessError, match="handoff_original_invalid|remote_read_invalid"):
        asyncio.run(service.handoff_load("cmhe:" + str(uuid4())))
    assert offsets == [0]


@pytest.mark.parametrize("fault", ["oversized", "changed_page"])
def test_handoff_assembly_is_bounded_and_rejects_changed_identity(tmp_path, fault):
    connection = RemoteConnection.model_validate(connection_data())
    event = synthetic_original(
        connection,
        {
            "text": json.dumps(
                {"goal": "a" * (70_000 if fault == "oversized" else 5000), "state": "s"}
            ),
            "metadata": HANDOFF_METADATA,
        },
    )
    offsets = []
    service = access_for(
        tmp_path / "first",
        connection,
        event.model_dump(),
        transport=original_transport(
            connection,
            event,
            event.canonical_payload(),
            offsets,
            changed_page=fault == "changed_page",
        ),
    )
    code = "handoff_original_too_large" if fault == "oversized" else "handoff_original_invalid"
    with pytest.raises(AgentAccessError, match=code):
        asyncio.run(service.handoff_load("cmhe:" + str(uuid4())))
    assert len(offsets) == (16 if fault == "oversized" else 2)


def test_complete_original_filters_local_credential_spanning_pages(tmp_path):
    connection = RemoteConnection.model_validate(connection_data())
    secret = "synthetic-only-second-device-known-value"  # pragma: allowlist secret
    content = {"text": json.dumps({"goal": "g", "state": secret}), "metadata": HANDOFF_METADATA}
    event = synthetic_original(connection, content)
    start = event.canonical_payload().index(secret)
    document = {"goal": "g" * (4096 - start - len(secret) // 2 + 1), "state": secret}
    event = event.model_copy(update={"content": {**content, "text": json.dumps(document)}})
    text = event.canonical_payload()
    start = text.index(secret)
    assert start < 4096 < start + len(secret)
    offsets = []
    service = access_for(
        tmp_path / "first",
        connection,
        event.model_dump(),
        privacy=CredentialFilter((secret,)),
        transport=original_transport(connection, event, text, offsets),
    )
    loaded = asyncio.run(service.handoff_load("cmhe:" + str(uuid4())))
    assert loaded["handoff"]["state"] == "<redacted-secret>"
    assert len(offsets) == 2
