"""ADR-0044: a runtime fault must not expose restored content through Core."""

import asyncio
import socket

import pytest
from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.governance_authority import open_authority
from httpx import ASGITransport, AsyncClient
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import LocalHyEmbedService, _joined_thread
from starlette.responses import JSONResponse, PlainTextResponse
from test_chat_graph import produce
from test_entity_inventory_guard import SyntheticEmbedder
from test_governance_execution import EXECUTE
from test_inflight_execution_recovery import confirmation
from test_original_archive import config_for
from test_output_lineage import fixture


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    monkeypatch.setattr(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder())
    connect = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple) and address[0] not in {"localhost", "127.0.0.1", "::1"}:
            raise AssertionError("external_network_forbidden")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def test_failed_completed_purge_hides_restored_native_record_but_keeps_repair_reachable(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, device, event, _ = await produce(app, web, config, monkeypatch)
            admin = {"Authorization": "Bearer " + config.admin_token}
            sdk = await _sdk_library_bridge(app.core)._get_client()
            vector = sdk._vector_store
            identity = results[0]["memory_ids"][0]
            snapshot = await _joined_thread(
                vector._collection.get,
                ids=[vector._node_id_to_point_id(identity)],
                include=["metadatas", "documents", "embeddings"],
            )
            assert len(snapshot["ids"]) == 1
            body = await confirmation(web, admin, event, [jobs[0]], action="purge")
            completed = await web.post(EXECUTE, headers=admin, json=body)
            assert completed.status_code == 200 and completed.json()["state"] == "completed"
            path = "/api/v2/memories/" + identity
            assert (await web.get(path, headers=admin)).status_code == 404
            # Exact captured native row restoration, not a fabricated response.
            await _joined_thread(
                vector._collection.upsert,
                ids=snapshot["ids"],
                metadatas=snapshot["metadatas"],
                documents=snapshot["documents"],
                embeddings=snapshot["embeddings"],
            )
            executor = app.core.state.cmh_executor
            original_operations = executor.store.operations()

            async def failed_delete(*args):
                raise OSError("synthetic_native_delete_failure")

            with monkeypatch.context() as failure:
                failure.setattr(executor.purger, "_sdk_delete", failed_delete)
                assert (await web.post(EXECUTE, headers=admin, json=body)).status_code == 503
                assert not executor.ready and executor.authority.runtime_recovery_required
                executor.authority.require_ready()  # Structure remains usable for recovery.
                denied = await web.get(path, headers=admin)
                assert denied.status_code == 503
                assert denied.json() == {"detail": "governance_recovery_required"}
                assert denied.headers["cache-control"] == "no-store"
                assert snapshot["documents"][0] not in denied.text
                remote = await web.post(
                    "/hub/v1/memories/get",
                    headers=device,
                    json={"scope_id": event["scope_id"], "record_id": identity},
                )
                assert remote.status_code == 404
                assert (await web.get("/hub/login")).status_code == 200
                assert (await web.get("/healthz")).status_code == 200

            # The existing governance path is reachable while Core is fenced.
            assert (await web.post(EXECUTE, headers=admin, json=body)).status_code == 200
            assert executor.store.operations() == original_operations
            assert (await web.get(path, headers=admin)).status_code == 503
            await executor.resume()
            assert executor.ready
            assert (await web.get(path, headers=admin)).status_code == 404

    asyncio.run(scenario())


def boundary_for(config, core, *, remote_api=None):
    authority = open_authority(config)
    authority.finish_runtime_recovery()
    return (
        CloudBoundary(
            core, config, "synthetic-internal", governance=authority, remote_api=remote_api
        ),
        authority,
    )


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/v2/raw"), ("PUT", "/api/v2/memories/id"), ("GET", "/future-body")]
)
def test_fault_rejects_core_before_entry_after_authentication(tmp_path, method, path):
    config = config_for(tmp_path)
    calls = []

    async def core(scope, receive, send):
        calls.append(scope["path"])
        await JSONResponse({"content": "synthetic-private-body"})(scope, receive, send)

    app, authority = boundary_for(config, core)

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            admin = {"Authorization": "Bearer " + config.admin_token}
            assert (await web.request(method, path, headers=admin)).status_code == 200
            authority.require_runtime_recovery()
            unauthorized = await web.get("/api/v2/raw")
            assert unauthorized.status_code == 401
            cross_origin = await web.get(
                path, headers={**admin, "Origin": "https://other.example.test"}
            )
            assert cross_origin.status_code == 403
            denied = await web.request(method, path, headers=admin)
            assert denied.status_code == 503
            assert denied.json() == {"detail": "governance_recovery_required"}
            assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("started", [False, True])
def test_fault_after_core_entry_prevents_first_or_later_body(tmp_path, started):
    config = config_for(tmp_path)

    async def core(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if started:
            await send({"type": "http.response.body", "body": b"safe-first", "more_body": True})
        authority.require_runtime_recovery()
        await send(
            {"type": "http.response.body", "body": b"synthetic-obsolete-body", "more_body": False}
        )

    app, authority = boundary_for(config, core)

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            reply = await web.get(
                "/api/v2/raw", headers={"Authorization": "Bearer " + config.admin_token}
            )
            if started:
                assert reply.status_code == 200 and reply.text == "safe-first"
            else:
                assert reply.status_code == 503
                assert reply.json() == {"detail": "governance_recovery_required"}
            assert "obsolete" not in reply.text

    asyncio.run(scenario())


def test_fault_during_outer_authority_check_cannot_escape_inner_runtime_check(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def core(scope, receive, send):
        await JSONResponse({"content": "synthetic-obsolete-body"})(scope, receive, send)

    app, authority = boundary_for(config, core)
    original = authority.require_ready
    checks = 0

    def checked():
        nonlocal checks
        checks += 1
        original()
        if checks == 2:
            # The outer guard checks SQLite in a worker thread after the
            # nested Core guard has already checked the runtime event.
            authority.require_runtime_recovery()

    monkeypatch.setattr(authority, "require_ready", checked)

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            reply = await web.get(
                "/api/v2/raw", headers={"Authorization": "Bearer " + config.admin_token}
            )
            assert reply.status_code == 503
            assert reply.json() == {"detail": "governance_recovery_required"}
            assert "obsolete" not in reply.text
            assert checks == 2

    asyncio.run(scenario())


def test_fault_while_headers_are_sent_stops_the_body(tmp_path):
    config = config_for(tmp_path)

    async def core(scope, receive, send):
        await JSONResponse({"content": "synthetic-obsolete-body"})(scope, receive, send)

    app, authority = boundary_for(config, core)
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            authority.require_runtime_recovery()

    asyncio.run(
        app(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v2/raw",
                "headers": [
                    (b"host", config.authority.encode()),
                    (b"authorization", ("Bearer " + config.admin_token).encode()),
                ],
                "query_string": b"",
            },
            receive,
            send,
        )
    )
    assert messages[0]["status"] == 200  # Headers have already left the boundary.
    assert messages[1:] == [{"type": "http.response.body", "body": b"", "more_body": False}]


def test_fault_keeps_login_static_and_remote_maintenance_without_bypassing_csrf(tmp_path):
    config = config_for(tmp_path)
    paths = []

    async def core(scope, receive, send):
        paths.append(scope["path"])
        await PlainTextResponse("synthetic-static")(scope, receive, send)

    async def remote(scope, receive, send):
        assert scope["state"]["cmh_admin"]
        await JSONResponse({"state": "maintenance"})(scope, receive, send)

    app, authority = boundary_for(config, core, remote_api=remote)
    authority.require_runtime_recovery()

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            assert (await web.get("/hub/login")).status_code == 200
            login = await web.post(
                "/hub/login",
                headers={"Origin": config.public_origin},
                json={"credential": config.admin_token},
            )
            assert login.status_code == 303
            for path in ("/", "/index.html", "/assets/synthetic.js", "/favicon.ico"):
                assert (await web.get(path)).status_code == 200
            route = "/hub/v1/governance/execute"
            assert (await web.post(route, json={})).status_code == 403
            allowed = await web.post(route, json={}, headers={"Origin": config.public_origin})
            assert allowed.status_code == 200 and allowed.json()["state"] == "maintenance"
            assert (
                await web.post("/assets/synthetic.js", headers={"Origin": config.public_origin})
            ).status_code == 503
            assert len(paths) == 4

    asyncio.run(scenario())
