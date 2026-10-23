import asyncio

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient


def configured(tmp_path):
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        "https://memory.example.test",
        "synthetic-admin-" + "a" * 40,
        Fernet.generate_key(),
    )
    return config, create_hub(config)


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_device_lifecycle_through_real_core_boundary(tmp_path):
    config, app = configured(tmp_path)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                admin = bearer(config.admin_token)
                assert (await client.get("/hub/v1/devices")).status_code == 403
                assert (await client.get("/hub/v1/session", headers=admin)).status_code == 401
                project = await client.post(
                    "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic project"}
                )
                assert project.status_code == 201
                listed = await client.get("/hub/v1/projects", headers=admin)
                assert listed.json()["projects"] == [project.json()]
                created = await client.post(
                    "/hub/v1/devices", headers=admin, json={"display_name": "Isolated connector"}
                )
                assert created.status_code == 201
                assert created.headers["cache-control"] == "no-store"
                identity = created.json()
                token = identity["credential"]
                device = identity["device"]["device_id"]
                credentials = bearer(token)
                session = await client.get("/hub/v1/session", headers=credentials)
                assert session.status_code == 200
                assert session.json()["device"]["grants"] == []
                assert session.json()["features"] == ["device_identity"]
                assert token not in session.text
                instance_id = session.json()["instance_id"]
                for path in ["/api/v1/model-profiles", "/api/v2/memory-library"]:
                    assert (await client.get(path, headers=credentials)).status_code == 401
                internal = await client.get("/internal/test", headers=credentials)
                assert internal.status_code == 303
                assert internal.headers["location"] == "/hub/login"
                assert (
                    await client.get(
                        "/hub/v1/devices", headers={**credentials, "X-Cmh-Admin": "true"}
                    )
                ).status_code == 403
                assert (await client.get("/hub/v1/unknown", headers=credentials)).status_code == 404
                assert (await client.get("/hub/v1", headers=credentials)).status_code == 404
                changed = await client.put(
                    f"/hub/v1/devices/{device}/grants",
                    headers=admin,
                    json={
                        "expected_revision": 1,
                        "grants": [
                            {
                                "scope_id": project.json()["project_id"],
                                "permissions": ["memory:read"],
                            }
                        ],
                    },
                )
                assert changed.status_code == 200 and changed.json()["revision"] == 2
                assert (await client.get("/hub/v1/session", headers=credentials)).json()[
                    "device"
                ] == changed.json()
                stale = await client.post(
                    f"/hub/v1/devices/{device}/rotate", headers=admin, json={"expected_revision": 1}
                )
                assert stale.status_code == 409
                rotated = await client.post(
                    f"/hub/v1/devices/{device}/rotate", headers=admin, json={"expected_revision": 2}
                )
                assert rotated.status_code == 200
                assert (await client.get("/hub/v1/session", headers=credentials)).status_code == 401
                replacement = bearer(rotated.json()["credential"])
                assert (await client.get("/hub/v1/session", headers=replacement)).json()[
                    "instance_id"
                ] == instance_id
                revoked = await client.post(
                    f"/hub/v1/devices/{device}/revoke", headers=admin, json={"expected_revision": 3}
                )
                assert revoked.status_code == 200
                assert (await client.get("/hub/v1/session", headers=replacement)).status_code == 401
                devices = await client.get("/hub/v1/devices", headers=admin)
                assert devices.json()["devices"][0]["status"] == "revoked"
                assert "credential" not in devices.text
                assert (await client.get("/api/v1/health", headers=admin)).status_code == 200

    asyncio.run(scenario())


def test_remote_browser_csrf_input_limits_and_non_reflecting_errors(tmp_path):
    config, app = configured(tmp_path)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                origin = {"Origin": config.public_origin}
                await client.post(
                    "/hub/login", headers=origin, json={"credential": config.admin_token}
                )
                assert (await client.get("/hub/v1/devices")).status_code == 200
                assert (await client.head("/hub/v1/devices")).status_code == 200
                assert (await client.head("/hub/v1/projects")).status_code == 200
                assert (
                    await client.post("/hub/v1/devices", json={"display_name": "Mac"})
                ).status_code == 403
                assert (
                    await client.post(
                        "/hub/v1/devices",
                        json={"display_name": "Mac"},
                        headers={"Origin": "https://invalid.test"},
                    )
                ).status_code == 403
                invalid = await client.post(
                    "/hub/v1/devices",
                    headers=origin,
                    json={"display_name": "Mac", "credential": "synthetic-do-not-echo"},
                )
                assert invalid.status_code == 422
                assert invalid.json() == {"detail": "invalid_request"}
                assert "synthetic-do-not-echo" not in invalid.text
                assert (
                    await client.post(
                        "/hub/v1/devices",
                        headers={**origin, "Content-Type": "application/json"},
                        content=b"x" * (64 * 1024 + 1),
                    )
                ).status_code == 413
                for content in [b"broken", b"[]", b'{"display_name":" "}']:
                    assert (
                        await client.post(
                            "/hub/v1/devices",
                            headers={**origin, "Content-Type": "application/json"},
                            content=content,
                        )
                    ).status_code == 422
                assert (
                    await client.post("/hub/v1/devices", headers=origin, content="not json")
                ).status_code == 422
                assert (await client.delete("/hub/v1/devices", headers=origin)).status_code == 405
                assert (
                    await client.post(
                        "/hub/v1/devices", headers=origin, json={"display_name": "Mac"}
                    )
                ).status_code == 201

    asyncio.run(scenario())
