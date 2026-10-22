"""Regression: QA-001 — a failed form login must remain retryable.

Found by QA on 2026-10-22. Evidence: docs/reviews/2026-10-22-runtime.md.
"""

import asyncio

from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.config import HubConfig
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient


def test_invalid_form_keeps_login_form_without_echoing_credentials(tmp_path):
    config = HubConfig(tmp_path, tmp_path, "http://localhost:8800", "a" * 40, Fernet.generate_key())
    app = CloudBoundary(None, config, "internal")

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as c:
            headers = {"Origin": config.public_origin}
            response = await c.post(
                "/hub/login", data={"credential": "synthetic-wrong-input"}, headers=headers
            )
            assert response.status_code == 401
            assert response.headers["content-type"].startswith("text/html")
            assert 'role="alert"' in response.text
            assert "凭证不正确，请重新输入。" in response.text
            assert '<form method="post" action="/hub/login">' in response.text
            assert "synthetic-wrong-input" not in response.text
            assert response.headers["cache-control"] == "no-store"
            success = await c.post(
                "/hub/login", data={"credential": config.admin_token}, headers=headers
            )
            assert success.status_code == 303
            assert "cmh_session=" in success.headers["set-cookie"]
            for _ in range(19):
                await c.post("/hub/login", data={"credential": "wrong"}, headers=headers)
            limited = await c.post("/hub/login", data={"credential": "wrong"}, headers=headers)
            assert limited.status_code == 429
            assert "请稍后再试" in limited.text

    asyncio.run(scenario())
