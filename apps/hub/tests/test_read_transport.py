import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_transport
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import ReadReply
from cloud_memory_hub.transport import RemoteConnection, TransportError, read_remote
from one_memory.hy_memory_bridge import HySdkMemoryRecord
from test_remote_read import grant_reads
from test_transport import BytesBody, bind_live, connection_data

live_hub = test_transport.live_hub


def json_response(value):
    return httpx.Response(
        200,
        stream=BytesBody(json.dumps(value).encode()),
        headers={"content-type": "application/json"},
    )


def test_installed_read_cli_uses_tcp_and_does_not_need_outbox(live_hub, tmp_path):
    config, app, _ = live_hub
    connection, admin, headers, raw = asyncio.run(bind_live(config))
    secret = uuid4().hex
    record = HySdkMemoryRecord(
        memory_id=str(uuid4()),
        content="合成记忆",
        layer="l2_fact",
        status="active",
        is_latest=True,
        user_id=f"cmh:{raw['instance_id']}:{raw['scope_id']}",
        agent_id="synthetic",
    )
    queries = []

    async def search(query, **kwargs):
        queries.append(query)
        return (SimpleNamespace(memory_id=record.memory_id, member_ids=()),)

    async def get(_):
        return record

    app.core.state.cmh_reads.bridge = lambda: SimpleNamespace(
        search_memories=search, get_sdk_memory=get
    )

    async def authorize():
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await grant_reads(client, admin, raw)

    asyncio.run(authorize())
    connection_file = tmp_path / "connection.json"
    connection_file.touch(mode=0o600)
    connection_file.write_text(
        json.dumps(
            {
                **connection.model_dump(mode="json"),
                "credential": connection.credential.get_secret_value(),
            }
        )
    )
    known = tmp_path / "known.json"
    known.touch(mode=0o600)
    known.write_text(json.dumps([secret]))
    cli = str(Path(sys.executable).with_name("cloud-memory-connector"))
    command = [
        cli,
        "memory-search",
        "--connection-file",
        str(connection_file),
        "--known-credentials-file",
        str(known),
    ]
    body = json.dumps({"scope_id": raw["scope_id"], "query": "任务接续 " + secret})
    result = subprocess.run(command, input=body, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    reply = ReadReply.model_validate_json(result.stdout)
    assert reply.items[0].record_id == record.memory_id
    assert queries and secret not in queries[0] and secret not in result.stdout + result.stderr
    assert not (tmp_path / "outbox.sqlite3").exists()

    async def revoke():
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            response = await client.put(
                f"/hub/v1/devices/{raw['device_id']}/grants",
                headers=admin,
                json={"expected_revision": 3, "grants": []},
            )
            assert response.status_code == 200

    asyncio.run(revoke())
    rejected = subprocess.run(command, input=body, text=True, capture_output=True, timeout=20)
    assert rejected.returncode == 2
    assert json.loads(rejected.stderr)["error"] == "scope_permission_required"
    assert len(queries) == 1 and secret not in rejected.stderr


@pytest.mark.parametrize("fault", ["no_feature", "no_grant", "wrong_device", "wrong_instance"])
def test_failed_read_preflight_sends_no_query(fault):
    connection = RemoteConnection.model_validate(connection_data())
    scope, calls = str(uuid4()), []

    def handler(request):
        calls.append(request)
        value = {
            "protocol_version": 1,
            "instance_id": connection.instance_id,
            "features": ["scoped_reads"],
            "device": {
                "device_id": connection.device_id,
                "revision": 3,
                "status": "active",
                "grants": [{"scope_id": scope, "permissions": ["memory:read"]}],
            },
        }
        if fault == "no_feature":
            value["features"] = []
        elif fault == "no_grant":
            value["device"]["grants"] = []
        elif fault == "wrong_device":
            value["device"]["device_id"] = str(uuid4())
        else:
            value["instance_id"] = str(uuid4())
        return json_response(value)

    with pytest.raises(TransportError):
        asyncio.run(
            read_remote(
                connection,
                "memory-search",
                {"scope_id": scope, "query": "保密问题"},
                transport=httpx.MockTransport(handler),
            )
        )
    assert len(calls) == 1 and calls[0].url.path == "/hub/v1/session"
    assert not calls[0].content


@pytest.mark.parametrize("fault", ["scope", "device", "revision", "operation", "trust", "version"])
def test_read_reply_identity_is_verified(fault):
    connection = RemoteConnection.model_validate(connection_data())
    scope = str(uuid4())
    secret = uuid4().hex
    bodies = []

    def handler(request):
        if request.url.path == "/hub/v1/session":
            return json_response(
                {
                    "protocol_version": 1,
                    "instance_id": connection.instance_id,
                    "features": ["scoped_reads"],
                    "device": {
                        "device_id": connection.device_id,
                        "revision": 3,
                        "status": "active",
                        "grants": [{"scope_id": scope, "permissions": ["memory:read"]}],
                    },
                }
            )
        bodies.append(request.content)
        result = ReadReply(
            scope_id=scope,
            instance_id=connection.instance_id,
            device_id=connection.device_id,
            device_revision=3,
            operation="memory-search",
            items=(),
        ).model_dump(mode="json")
        key, value = {
            "scope": ("scope_id", str(uuid4())),
            "device": ("device_id", str(uuid4())),
            "revision": ("device_revision", 4),
            "operation": ("operation", "history-list"),
            "trust": ("content_trust", "system_instructions"),
            "version": ("protocol_version", True),
        }[fault]
        result[key] = value
        return json_response(result)

    with pytest.raises(TransportError):
        asyncio.run(
            read_remote(
                connection,
                "memory-search",
                {"scope_id": scope, "query": secret},
                privacy=CredentialFilter((secret,)),
                transport=httpx.MockTransport(handler),
            )
        )
    assert len(bodies) == 1 and secret.encode() not in bodies[0]
