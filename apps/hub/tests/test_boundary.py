import asyncio
import json
from pathlib import Path

from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.config import HubConfig
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse


def configuration(tmp_path: Path) -> HubConfig:
    return HubConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        public_origin="https://memory.example.test",
        admin_token="synthetic-" + "a" * 40,
        encryption_key=Fernet.generate_key(),
    )


def test_boundary_enforces_identity_origin_and_private_core_headers(tmp_path):
    config = configuration(tmp_path)
    observed = []

    async def core(scope, receive, send):
        observed.append(dict(scope["headers"]))
        await JSONResponse({"accepted": True})(scope, receive, send)

    app = CloudBoundary(core, config, "internal-only")

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            assert (await client.get("/api/v1/memories")).status_code == 401
            assert (await client.get("/")).status_code == 303
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            authorization = {"Authorization": "Bearer " + config.admin_token}
            assert (
                await client.get(
                    "/api/v1/memories",
                    headers={
                        **authorization,
                        "Origin": "https://attacker.example",
                    },
                )
            ).status_code == 403
            assert (
                await client.get(
                    "/api/v1/memories",
                    headers={
                        **authorization,
                        "Host": "attacker.example",
                    },
                )
            ).status_code == 403
            for path in ["/internal/graph-snapshot", "/ui/login", "/api/v1/ui/session"]:
                assert (await client.get(path, headers=authorization)).status_code == 404
            response = await client.post(
                "/api/v1/memory-notes",
                json={},
                headers={
                    **authorization,
                    "X-One-Memory-Installation-Id": "forged",
                    "Origin": config.public_origin,
                    "Cookie": "one_memory_session=forged",
                },
            )
            assert response.status_code == 200
            assert len(observed) == 1
            assert observed[0][b"authorization"] == b"Bearer internal-only"
            assert b"origin" not in observed[0]
            assert b"cookie" not in observed[0]
            assert not any(key.startswith(b"x-one-memory") for key in observed[0])

    asyncio.run(scenario())


def test_login_session_csrf_and_logout(tmp_path):
    config = configuration(tmp_path)

    async def core(scope, receive, send):
        await JSONResponse({"accepted": True})(scope, receive, send)

    app = CloudBoundary(core, config, "internal-only")

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            page = await client.get("/hub/login")
            assert "Cloud Memory Hub" in page.text
            # A no-referrer page makes Chrome post the login form with `Origin: null`.
            assert page.headers["referrer-policy"] == "same-origin"
            assert config.admin_token not in page.text
            assert (
                await client.post("/hub/login", json={"credential": config.admin_token})
            ).status_code == 403
            origin = {"Origin": config.public_origin}
            assert (
                await client.post("/hub/login", json={"credential": "wrong"}, headers=origin)
            ).status_code == 401
            login = await client.post(
                "/hub/login", json={"credential": config.admin_token}, headers=origin
            )
            assert login.status_code == 303
            assert "HttpOnly" in login.headers["set-cookie"]
            assert "Secure" in login.headers["set-cookie"]
            assert "SameSite=strict" in login.headers["set-cookie"]
            assert config.admin_token not in login.headers["set-cookie"]
            assert (await client.get("/api/v1/memories")).status_code == 200
            assert (await client.post("/api/v1/memory-notes", json={})).status_code == 403
            assert (
                await client.post("/api/v1/memory-notes", json={}, headers=origin)
            ).status_code == 200
            assert (
                await client.post("/api/v1/ui/session/logout", headers=origin)
            ).status_code == 200
            assert (await client.get("/api/v1/memories")).status_code == 401

    asyncio.run(scenario())


def test_login_limits_and_malformed_payloads(tmp_path):
    config = configuration(tmp_path)
    app = CloudBoundary(None, config, "internal-only")

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            origin = {"Origin": config.public_origin, "Content-Type": "application/json"}
            assert (
                await client.post("/hub/login", content=b"x" * 4097, headers=origin)
            ).status_code == 413
            for body in ["broken", "[]", json.dumps({"credential": [1]})]:
                assert (
                    await client.post("/hub/login", content=body, headers=origin)
                ).status_code == 401
            for _ in range(17):
                assert (await client.post("/hub/login", json={}, headers=origin)).status_code == 401
            assert (await client.post("/hub/login", json={}, headers=origin)).status_code == 429

    asyncio.run(scenario())
