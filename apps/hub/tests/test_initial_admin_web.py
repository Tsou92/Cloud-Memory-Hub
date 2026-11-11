"""First-use password changes are gated before any browser administrator session."""

import asyncio
import base64
import hashlib
import os
import re
from dataclasses import replace

import pytest
from cloud_memory_hub.admin_credentials import ADMIN_SETUP_PREFIX, initial_admin_setup_proof
from cloud_memory_hub.boundary import ADMIN_SETUP_PATH, CloudBoundary
from cloud_memory_hub.config import HubConfig, read_private_file
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse

INITIAL = ADMIN_SETUP_PREFIX + "a" * 64
REPLACEMENT = "synthetic-new-" + "b" * 40


def configuration(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    key = Fernet.generate_key()
    for name, value in (("admin-token", INITIAL.encode()), ("encryption-key", key)):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value + b"\n")
    return HubConfig(tmp_path / "data", directory, "https://memory.example.test", INITIAL, key)


async def core(scope, receive, send):
    await JSONResponse({"accepted": True})(scope, receive, send)


async def remote(scope, receive, send):
    await JSONResponse({"is_admin": scope["state"]["cmh_admin"]})(scope, receive, send)


def browser(app, config):
    return AsyncClient(transport=ASGITransport(app), base_url=config.public_origin)


def setup_payload():
    return {
        "password": "admin",  # pragma: allowlist secret -- public bootstrap password
        "proof": initial_admin_setup_proof(INITIAL),
        "new_credential": REPLACEMENT,
        "confirm_credential": REPLACEMENT,
    }


def test_setup_uses_scoped_proof_and_rotates_before_issuing_session(tmp_path):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal", remote_api=remote)
    headers = {"Origin": config.public_origin}
    payload = setup_payload()

    async def scenario():
        async with browser(app, config) as web:
            login_page = await web.get("/hub/login")
            assert "初始化链接" in login_page.text
            assert "<script>" not in login_page.text
            for password in ("admin", INITIAL, payload["proof"]):
                denied = await web.post(
                    "/hub/login", json={"credential": password}, headers=headers
                )
                assert denied.json() == {"detail": "initial_setup_required"}
                assert "set-cookie" not in denied.headers
            assert (await web.get("/api/example")).status_code == 401

            # The private bootstrap bearer still supports the installer. The public
            # initial password and the scoped proof cannot authenticate the API.
            for credential, expected in (
                ("admin", False),
                (payload["proof"], False),
                (INITIAL, True),
            ):
                api = await web.get(
                    "/hub/v1/projects", headers={"Authorization": f"Bearer {credential}"}
                )
                assert api.json() == {"is_admin": expected}

            page = await web.get(ADMIN_SETUP_PATH)
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert page.headers["referrer-policy"] == "no-referrer"
            csp = page.headers["content-security-policy"]
            script = re.search(r"<script>(.*?)</script>", page.text, re.DOTALL).group(1)
            script_digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"script-src 'sha256-{script_digest}'" in csp
            assert "connect-src 'self'" in csp
            assert "location.hash" in script and "history.replaceState" in script
            assert payload["proof"] not in page.text and INITIAL not in page.text
            assert "set-cookie" not in page.headers

            for invalid in (
                {**payload, "proof": "0" * 64},
                {
                    **payload,
                    "password": "bad",  # pragma: allowlist secret -- invalid fixture
                },
            ):
                denied = await web.post(ADMIN_SETUP_PATH, json=invalid, headers=headers)
                assert denied.status_code == 401
                assert "set-cookie" not in denied.headers
                assert denied.json() == {"detail": "invalid_initial_credential"}
            assert (await web.get("/api/example")).status_code == 401
            assert app.admin_credentials.valid(INITIAL)

            response = await web.post(ADMIN_SETUP_PATH, json=payload, headers=headers)
            assert response.json() == {"state": "initial_setup_completed"}
            cookie = response.headers["set-cookie"]
            assert "cmh_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
            assert REPLACEMENT not in cookie and payload["proof"] not in response.text
            assert (await web.get("/api/example")).status_code == 200
            replay = await web.post(ADMIN_SETUP_PATH, json=payload, headers=headers)
            assert replay.status_code == 409 and "set-cookie" not in replay.headers
            assert not app.admin_credentials.valid(INITIAL)

        persisted = read_private_file(config.config_dir / "admin-token").decode().strip()
        assert persisted == REPLACEMENT
        restarted = CloudBoundary(core, replace(config, admin_token=persisted), "new-instance")
        async with browser(restarted, config) as web:
            assert (await web.get(ADMIN_SETUP_PATH)).status_code == 409
            assert (
                await web.post(ADMIN_SETUP_PATH, json=payload, headers=headers)
            ).status_code == 409
            new_login = await web.post(
                "/hub/login", json={"credential": REPLACEMENT}, headers=headers
            )
            assert new_login.status_code == 303
            assert (await web.get("/api/example")).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("password", ["7", " 短密码🟣 ", "测" * 500])
def test_flexible_password_works_in_setup_login_restart_and_settings(tmp_path, password):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal", remote_api=remote)
    headers = {"Origin": config.public_origin}
    payload = {**setup_payload(), "new_credential": password, "confirm_credential": password}

    async def scenario():
        async with browser(app, config) as web:
            setup_page = await web.get(ADMIN_SETUP_PATH)
            assert "minlength" not in setup_page.text and "maxlength" not in setup_page.text
            response = await web.post(ADMIN_SETUP_PATH, json=payload, headers=headers)
            assert response.status_code == 200
            assert app.admin_credentials.valid_password(password)
            assert not app.admin_credentials.valid(password)
        stored = read_private_file(config.config_dir / "admin-token").decode().strip()
        assert stored != password and initial_admin_setup_proof(stored) is None
        restarted = CloudBoundary(
            core, replace(config, admin_token=stored), "synthetic-restart", remote_api=remote
        )
        async with browser(restarted, config) as web:
            page = await web.get("/hub/login")
            assert "maxlength" not in page.text
            assert (
                await web.get("/hub/v1/projects", headers={"Authorization": f"Bearer {stored}"})
            ).json() == {"is_admin": True}
            response = await web.post("/hub/login", data={"credential": password}, headers=headers)
            assert response.status_code == 303
            response = await web.post(
                "/hub/v1/admin/credential",
                json={"current_credential": password, "new_credential": "新"},
                headers=headers,
            )
            assert response.status_code == 200
            assert not restarted.admin_credentials.valid_password(password)
            assert restarted.admin_credentials.valid_password("新")

    asyncio.run(scenario())


def test_setup_checks_origin_payload_and_attempt_limit(tmp_path):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")
    payload = setup_payload()
    headers = {"Origin": config.public_origin}

    async def scenario():
        async with browser(app, config) as web:
            assert (await web.post(ADMIN_SETUP_PATH, json=payload)).status_code == 403
            wrong_origin = await web.post(
                ADMIN_SETUP_PATH, json=payload, headers={"Origin": "https://other.example.test"}
            )
            assert wrong_origin.status_code == 403
            oversized = await web.post(
                ADMIN_SETUP_PATH,
                content=b"x" * 4097,
                headers={**headers, "Content-Type": "application/json"},
            )
            assert oversized.status_code == 413
            malformed = await web.post(
                ADMIN_SETUP_PATH,
                json={"password": "admin"},  # pragma: allowlist secret -- public bootstrap password
                headers=headers,
            )
            assert malformed.status_code == 400
            mismatch = await web.post(
                ADMIN_SETUP_PATH,
                json={**payload, "confirm_credential": "mismatch"},
                headers=headers,
            )
            assert mismatch.status_code == 422
            for _ in range(20):
                wrong_proof = await web.post(
                    ADMIN_SETUP_PATH, json={**payload, "proof": "1" * 64}, headers=headers
                )
                assert wrong_proof.status_code == 401
            limited = await web.post(ADMIN_SETUP_PATH, json=payload, headers=headers)
            assert limited.status_code == 429 and "set-cookie" not in limited.headers
            assert app.admin_credentials.valid(INITIAL)

    asyncio.run(scenario())
