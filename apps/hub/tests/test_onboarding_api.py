"""FR-02/06: public installers are available without exposing Hub administration."""

import asyncio
import base64
import hashlib
import re

from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.onboarding_api import PUBLIC_CONNECT_PATHS
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse
from test_boundary import configuration


def test_connection_resources_need_origin_but_not_administrator(tmp_path):
    config = configuration(tmp_path)
    observed = []

    async def core(scope, receive, send):
        observed.append(scope)
        await JSONResponse({"private": True})(scope, receive, send)

    app = CloudBoundary(core, config, "internal-test-only")

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            for path in PUBLIC_CONNECT_PATHS:
                reply = await client.get(path)
                assert reply.status_code == 200
                assert reply.headers["cache-control"] == "no-store"
                assert reply.headers["referrer-policy"] == "no-referrer"
                assert config.admin_token not in reply.text
                assert "internal-test-only" not in reply.text
                assert "set-cookie" not in reply.headers
                assert (await client.post(path)).status_code == 405
                assert (
                    await client.get(path, headers={"Origin": "https://foreign.example"})
                ).status_code == 403
                assert (
                    await client.get(path, headers={"Host": "foreign.example"})
                ).status_code == 403
            assert (await client.get("/api/v1/memories")).status_code == 401
            assert (await client.get("/hub/connect/private")).status_code == 401
            assert not observed

    asyncio.run(scenario())


def test_downloads_are_bound_to_served_connector_and_configured_origin(tmp_path):
    config = configuration(tmp_path)

    async def core(scope, receive, send):
        raise AssertionError("must not enter Core")

    async def scenario():
        app = CloudBoundary(core, config, "internal-test-only")
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            connector = await client.get("/hub/connect.py")
            assert connector.status_code == 200
            digest = hashlib.sha256(connector.content).hexdigest()
            for path in ("/hub/install.sh", "/hub/install.ps1"):
                reply = await client.get(path)
                assert reply.status_code == 200
                assert digest in reply.text
                assert config.public_origin in reply.text
                assert "__CMH_" not in reply.text
                assert "0.12.5" in reply.text
                assert "3.12.13" in reply.text
            page = await client.get("/hub/connect")
            script = re.search(r"<script>(.*?)</script>", page.text, re.S).group(1)
            script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            style = re.search(r"<style>(.*?)</style>", page.text, re.S).group(1)
            style_hash = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
            csp = page.headers["content-security-policy"]
            assert f"script-src 'sha256-{script_hash}'" in csp
            assert f"style-src 'sha256-{style_hash}'" in csp
            assert "unsafe-inline" not in csp
            assert "default-src 'none'" in csp
            assert "localStorage" not in script and "sessionStorage" not in script
            assert "history.replaceState" in script
            assert "fetch(" not in script
            assert "__SCRIPT__" not in page.text and "__ORIGIN__" not in page.text

    asyncio.run(scenario())
