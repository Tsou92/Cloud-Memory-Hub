import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import pytest
import uvicorn
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.transport import (
    RemoteConnection,
    TransportError,
    send_pending,
    watch_delivery,
)
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.responses import JSONResponse
from test_remote_ingest import count_jobs, provision


def connection_data(origin="https://nas.example.test"):
    device = str(uuid4())
    return {
        "schema_version": 1,
        "origin": origin,
        "instance_id": str(uuid4()),
        "device_id": device,
        "credential": f"cmhdev_v1_{device}_{secrets.token_urlsafe(32)}",
    }


def queue_for(tmp_path, connection, raw=None):
    outbox = LocalOutbox.initialize(
        tmp_path / "connector" / "outbox.sqlite3",
        instance_id=connection.instance_id,
        device_id=connection.device_id,
    )
    if raw is None:
        raw = {
            "instance_id": connection.instance_id,
            "device_id": connection.device_id,
            **{
                key: str(uuid4())
                for key in ("source_id", "scope_id", "event_id", "conversation_id", "record_id")
            },
            "kind": "operation",
            "revision": 1,
            "content": {"text": "Retry after network recovery."},
        }
    outbox.enqueue(raw)
    return outbox, raw


@pytest.fixture
def live_hub(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        origin,
        "a" * 40,
        Fernet.generate_key(),
        governance_dir=tmp_path / "governance",
    )
    activate_authority(config.config_dir, config.governance_dir, config.encryption_key, new=True)
    app = create_hub(config)
    app.core.state.transport_test_offline = False
    uploaded = []

    async def capture(scope, receive, send):
        if scope.get("path") == "/hub/v1/session" and app.core.state.transport_test_offline:
            await JSONResponse({"detail": "synthetic_unavailable"}, status_code=503)(
                scope, receive, send
            )
            return

        async def wrapped():
            message = await receive()
            if scope.get("path") == "/hub/v1/events" and message.get("body"):
                uploaded.append(message["body"])
            return message

        await app(scope, wrapped, send)

    server = uvicorn.Server(uvicorn.Config(capture, log_config=None, access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield config, app, uploaded
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


async def bind_live(config):
    async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
        admin, headers, credential, raw = await provision(client, config)
    connection = RemoteConnection(
        origin=config.public_origin,
        instance_id=raw["instance_id"],
        device_id=raw["device_id"],
        credential=credential,
    )
    return connection, admin, headers, raw


def test_actual_tcp_filtered_upload_and_lost_response_replay(live_hub, tmp_path):
    config, _, uploaded = live_hub

    class LoseResponse(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.url.path == "/hub/v1/events":
                await response.aclose()
                raise httpx.ReadError("synthetic response loss; private request withheld")
            return response

        async def aclose(self):
            await self.inner.aclose()

    async def scenario():
        connection, _, _, raw = await bind_live(config)
        secret = secrets.token_urlsafe(32)
        raw["content"]["messages"].append({"role": "tool", "text": "password=" + secret})
        outbox, _ = queue_for(tmp_path, connection, raw)
        with pytest.raises(TransportError, match="transport_unavailable") as caught:
            await send_pending(connection, outbox, transport=LoseResponse())
        assert caught.value.retryable and count_jobs(config) == 1
        assert outbox.status()["queued"] == 1
        reopened = LocalOutbox(
            outbox.path, instance_id=connection.instance_id, device_id=connection.device_id
        )
        assert await send_pending(connection, reopened) == 1
        assert reopened.status()["accepted"] == 1 and count_jobs(config) == 1
        assert await send_pending(connection, reopened) == 0
        assert len(uploaded) == 2 and uploaded[0] == uploaded[1]
        assert secret.encode() not in b"".join(uploaded)
        assert connection.credential.get_secret_value().encode() not in b"".join(uploaded)

    asyncio.run(scenario())


def test_preflight_rejects_revoked_source_and_wrong_project_before_content(live_hub, tmp_path):
    config, _, uploaded = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        outbox, _ = queue_for(tmp_path, connection, {**raw, "scope_id": "global"})
        with pytest.raises(TransportError, match="source_permission_required"):
            await send_pending(connection, outbox)
        assert uploaded == []
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            assert (
                await client.post(
                    f"/hub/v1/sources/{raw['source_id']}/revoke",
                    headers=admin,
                    json={"expected_revision": 1},
                )
            ).status_code == 200
        with pytest.raises(TransportError, match="source_permission_required"):
            await send_pending(connection, outbox)
        assert uploaded == [] and outbox.status()["queued"] == 1 and count_jobs(config) == 0

    asyncio.run(scenario())


def test_wrong_instance_and_device_binding_never_upload_content(live_hub, tmp_path):
    config, _, uploaded = live_hub

    async def scenario():
        connection, _, _, raw = await bind_live(config)
        wrong = connection.model_copy(update={"instance_id": str(uuid4())})
        outbox, _ = queue_for(tmp_path, wrong, {**raw, "instance_id": wrong.instance_id})
        with pytest.raises(TransportError, match="remote_instance_mismatch"):
            await send_pending(wrong, outbox)
        with pytest.raises(TransportError, match="connection_binding_mismatch"):
            await send_pending(connection, outbox)
        assert uploaded == [] and outbox.status()["queued"] == 1

    asyncio.run(scenario())


def test_untrusted_tls_peer_gets_no_http_credentials_or_content(tmp_path):
    import ssl

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "test-key.pem", tmp_path / "test-cert.pem"
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = RemoteConnection.model_validate(
            connection_data(f"https://localhost:{server.server_port}")
        )
        outbox, _ = queue_for(tmp_path, connection)
        with pytest.raises(TransportError, match="transport_unavailable"):
            asyncio.run(send_pending(connection, outbox))
        assert requests == [] and outbox.status()["queued"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "origin",
    [
        "http://nas.example.test",
        f"https://user:{secrets.token_urlsafe(12)}@nas.example.test",
        "https://nas.example.test/path",
        "https://nas.example.test?token=a",
        "https://nas.example.test#fragment",
        "https://nas.example.test:",
        "https://nas.example.test:0",
        "https://nas.example.test:99999",
        "https://nas.example.test\n",
        "https://nas.example.test\\path",
        "file:///private/config",
    ],
)
def test_reject_untrusted_origin_before_network(origin):
    with pytest.raises(ValueError):
        RemoteConnection.model_validate(connection_data(origin))


def test_private_binding_validation_and_secret_repr(tmp_path):
    value = connection_data()
    path = tmp_path / "connection.json"
    path.touch(mode=0o600)
    path.write_text(json.dumps(value))
    connection = RemoteConnection.from_file(path)
    assert value["credential"] not in repr(connection)
    path.chmod(0o644)
    with pytest.raises(TransportError, match="connection_configuration_invalid"):
        RemoteConnection.from_file(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(TransportError):
        RemoteConnection.from_file(link)
    link.unlink()
    fifo = tmp_path / "not-a-file"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(TransportError, match="connection_configuration_invalid"):
        RemoteConnection.from_file(fifo)
    os.link(path, link)
    with pytest.raises(TransportError):
        RemoteConnection.from_file(path)
    link.unlink()
    for change in (
        {"schema_version": True},
        {"schema_version": 2},
        {"device_id": str(uuid4())},
        {"extra": "not permitted"},
    ):
        path.write_text(json.dumps({**value, **change}))
        with pytest.raises(TransportError) as caught:
            RemoteConnection.from_file(path)
        assert value["credential"] not in str(caught.value)


def test_watch_recovers_from_real_http_unavailability(live_hub, tmp_path):
    config, app, uploaded = live_hub

    async def scenario():
        connection, _, _, raw = await bind_live(config)
        outbox, _ = queue_for(tmp_path, connection, raw)
        app.core.state.transport_test_offline = True
        reports = []

        async def attempt():
            return await send_pending(connection, outbox)

        async def sleep(delay):
            if app.core.state.transport_test_offline:
                assert outbox.status()["queued"] == 1 and uploaded == []
                app.core.state.transport_test_offline = False
            else:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await watch_delivery(attempt, reports.append, sleep=sleep)
        assert reports[0] == {"status": "retrying", "error": "remote_temporarily_unavailable"}
        assert reports[1] == {"status": "accepted", "accepted": 1}
        assert outbox.status()["queued"] == 0 and count_jobs(config) == 1

    asyncio.run(scenario())


def test_source_preflight_observes_current_grants_and_device_revocation(live_hub, tmp_path):
    config, _, uploaded = live_hub

    async def scenario():
        connection, admin, headers, raw = await bind_live(config)
        outbox, _ = queue_for(tmp_path, connection, raw)
        access = f"/hub/v1/sources/{raw['source_id']}/access"
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            assert (await client.get(access, headers=headers)).status_code == 200
            _, _, other_headers, _ = await bind_live(config)
            assert (await client.get(access, headers=other_headers)).status_code == 403
            assert (
                await client.put(
                    f"/hub/v1/devices/{connection.device_id}/grants",
                    headers=admin,
                    json={"expected_revision": 2, "grants": []},
                )
            ).status_code == 200
            with pytest.raises(TransportError, match="source_permission_required"):
                await send_pending(connection, outbox)
            assert (
                await client.post(
                    f"/hub/v1/devices/{connection.device_id}/revoke",
                    headers=admin,
                    json={"expected_revision": 3},
                )
            ).status_code == 200
            with pytest.raises(TransportError, match="device_authentication_required"):
                await send_pending(connection, outbox)
        assert uploaded == [] and outbox.status()["queued"] == 1

    asyncio.run(scenario())


class BytesBody(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def json_response(value, status=200):
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        stream=BytesBody(json.dumps(value).encode()),
    )


def mock_server(connection, raw, *, receipt_change=None):
    def handler(request):
        identity = {
            "protocol_version": 1,
            "instance_id": connection.instance_id,
            "device_id": connection.device_id,
        }
        if request.url.path == "/hub/v1/session":
            return json_response(
                {
                    **identity,
                    "device": {"device_id": connection.device_id, "status": "active"},
                    "features": ["device_identity", "event_upload", "source_preflight"],
                }
            )
        if request.url.path.endswith("/access"):
            return json_response(
                {
                    **identity,
                    "source_id": raw["source_id"],
                    "scope_id": raw["scope_id"],
                    "allowed": True,
                }
            )
        from cloud_memory_hub.events import EventEnvelope

        event = EventEnvelope.model_validate_json(request.content)
        return json_response(
            {
                **identity,
                "status": "accepted",
                "event_id": event.event_id,
                "payload_digest": event.payload_digest(),
                "receipt_id": str(uuid4()),
                "processing_state": "pending",
                **(receipt_change or {}),
            },
            202,
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "processed"},
        {"event_id": str(uuid4())},
        {"payload_digest": "wrong"},
        {"device_id": str(uuid4())},
        {"receipt_id": None},
    ],
)
def test_mismatched_receipt_does_not_acknowledge(tmp_path, change):
    connection = RemoteConnection.model_validate(connection_data())
    outbox, raw = queue_for(tmp_path, connection)
    with pytest.raises(TransportError):
        asyncio.run(
            send_pending(
                connection, outbox, transport=mock_server(connection, raw, receipt_change=change)
            )
        )
    assert outbox.status()["queued"] == 1


@pytest.mark.parametrize(
    "status,retryable",
    [
        (302, False),
        (401, False),
        (403, False),
        (409, False),
        (422, False),
        (429, True),
        (503, True),
    ],
)
def test_http_failure_and_redirect_never_follow_or_ack(tmp_path, status, retryable):
    connection = RemoteConnection.model_validate(connection_data())
    outbox, _ = queue_for(tmp_path, connection)
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(status, headers={"location": "https://elsewhere.example.test"})

    with pytest.raises(TransportError) as caught:
        asyncio.run(send_pending(connection, outbox, transport=httpx.MockTransport(handler)))
    assert caught.value.retryable is retryable and len(calls) == 1
    assert calls[0].host == "nas.example.test" and outbox.status()["queued"] == 1


@pytest.mark.parametrize("features", [["device_identity", "event_upload"], [{"bad": "shape"}]])
def test_old_or_malformed_capability_handshake_stops_before_preflight(tmp_path, features):
    connection = RemoteConnection.model_validate(connection_data())
    outbox, _ = queue_for(tmp_path, connection)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return json_response(
            {
                "protocol_version": 1,
                "instance_id": connection.instance_id,
                "device": {"device_id": connection.device_id, "status": "active"},
                "features": features,
            }
        )

    with pytest.raises(TransportError, match="remote_session_invalid"):
        asyncio.run(send_pending(connection, outbox, transport=httpx.MockTransport(handler)))
    assert paths == ["/hub/v1/session"] and outbox.status()["queued"] == 1


@pytest.mark.parametrize(
    "body,encoding",
    [
        (b" " * (128 * 1024 + 1), "identity"),
        (b'{"protocol_version":1,"protocol_version":2}', "identity"),
        (b"compressed", "gzip"),
    ],
)
def test_response_limits_and_invalid_json_keep_queue(tmp_path, body, encoding):
    connection = RemoteConnection.model_validate(connection_data())
    outbox, _ = queue_for(tmp_path, connection)

    def handler(request):
        return httpx.Response(
            200,
            stream=BytesBody(body),
            headers={"content-type": "application/json", "content-encoding": encoding},
        )

    with pytest.raises(TransportError):
        asyncio.run(send_pending(connection, outbox, transport=httpx.MockTransport(handler)))
    assert outbox.status()["queued"] == 1


def test_stalled_stream_has_whole_request_deadline(tmp_path, monkeypatch):
    import cloud_memory_hub.transport as transport_module

    monkeypatch.setattr(transport_module, "REQUEST_DEADLINE", 0.05)
    connection = RemoteConnection.model_validate(connection_data())
    outbox, _ = queue_for(tmp_path, connection)

    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Stalled())

    started = time.monotonic()
    with pytest.raises(TransportError, match="transport_unavailable"):
        asyncio.run(send_pending(connection, outbox, transport=httpx.MockTransport(handler)))
    assert time.monotonic() - started < 2 and outbox.status()["queued"] == 1


def test_watch_retries_with_bounded_backoff_and_cancels_without_drop():
    async def scenario():
        reports, delays = [], []
        attempts = 0

        async def attempt():
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise TransportError("transport_unavailable", retryable=True)
            return 1

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await watch_delivery(attempt, reports.append, sleep=sleep)
        assert 0.8 <= delays[0] <= 1.2 and 1.6 <= delays[1] <= 2.4
        assert delays[2] == 2
        assert reports == [
            {"status": "retrying", "error": "transport_unavailable"},
            {"status": "accepted", "accepted": 1},
        ]

        async def denied():
            raise TransportError("device_authentication_required")

        with pytest.raises(TransportError):
            await watch_delivery(denied, reports.append, sleep=sleep)
        assert len(delays) == 3

    asyncio.run(scenario())


def test_installed_cli_watch_delivers_and_handles_interrupt(live_hub, tmp_path):
    config, _, uploaded = live_hub
    connection, _, _, raw = asyncio.run(bind_live(config))
    path = tmp_path / "connection.json"
    path.touch(mode=0o600)
    path.write_text(
        json.dumps(
            {
                **connection.model_dump(mode="json"),
                "credential": connection.credential.get_secret_value(),
            }
        )
    )
    state = tmp_path / "cli-state"
    cli = str(Path(sys.executable).with_name("cloud-memory-connector"))
    args = ["--state-dir", str(state), "--connection-file", str(path)]

    def run(command, *, input=None):
        result = subprocess.run(
            [cli, command, *args], input=input, text=True, capture_output=True, timeout=15
        )
        assert result.returncode == 0
        assert connection.credential.get_secret_value() not in result.stdout + result.stderr
        return json.loads(result.stdout)

    assert run("init")["queued"] == 0
    assert run("enqueue", input=json.dumps(raw))["status"] == "queued"
    process = subprocess.Popen(
        [cli, "watch", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            if run("status")["accepted"] == 1:
                break
            time.sleep(0.05)
        assert run("status")["accepted"] == 1
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130 and len(uploaded) == 1 and count_jobs(config) == 1
        assert connection.credential.get_secret_value() not in stdout + stderr
        assert "Traceback" not in stderr
        assert run("send") == {"accepted": 0, "queued": 0}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
