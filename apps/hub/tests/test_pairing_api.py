"""ADR-0065: actual HTTP boundary, SQLite authority and installed-client contract."""

import asyncio
import json
import secrets
from uuid import uuid4

from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.client_setup import PairingBundle, import_pairing
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.remote_api import create_remote_api
from cloud_memory_hub.remote_ingest import RemoteIngest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette


def configured(tmp_path, *, origin=True):
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        "https://memory.example.test",
        "synthetic-admin-" + "a" * 40,
        Fernet.generate_key(),
    )
    access = DeviceAccessStore(config.config_dir / "remote-access.sqlite3")
    ingest = RemoteIngest(access, lambda: None)
    api = create_remote_api(
        access,
        ingest,
        None,
        None,
        public_origin=config.public_origin if origin else None,
    )
    boundary = CloudBoundary(Starlette(), config, "synthetic-internal-" + "b" * 40, remote_api=api)
    return config, boundary


def test_invite_claim_private_import_and_recovery_over_http(tmp_path):
    config, app = configured(tmp_path)

    async def scenario():
        transport = ASGITransport(app)
        async with AsyncClient(transport=transport, base_url=config.public_origin) as client:
            admin = {"Authorization": "Bearer " + config.admin_token}
            project = (
                await client.post(
                    "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic project"}
                )
            ).json()
            created = await client.post(
                "/hub/v1/pairings",
                headers=admin,
                json={"scope_id": project["project_id"], "display_name": "Synthetic Mac"},
            )
            assert created.status_code == 201
            assert created.headers["cache-control"] == "no-store"
            invitation = created.json()
            assert invitation["origin"] == config.public_origin
            path = f"/hub/v1/pairings/{invitation['pairing_id']}"
            assert (await client.get(path)).status_code == 403
            pending = await client.get(path, headers=admin)
            assert pending.json()["state"] == "pending"
            assert "secret" not in pending.text and "hash" not in pending.text
            claimant = secrets.token_urlsafe(32)
            body = {
                "pairing_id": invitation["pairing_id"],
                "invitation_secret": invitation["invitation_secret"],
                "claimant_secret": claimant,
            }
            first = await client.post("/hub/v1/pairings/claim", json=body)
            assert first.status_code == 200
            assert first.headers["cache-control"] == "no-store"
            result = first.json()
            assert claimant not in first.text and invitation["invitation_secret"] not in first.text
            assert "credential" not in result
            token = f"cmhdev_v1_{result['device_id']}_{claimant}"
            bundle = PairingBundle.model_validate({**result, "credential": token})
            assert bundle.scope_id == project["project_id"]
            download = tmp_path / "synthetic-pairing.json"
            download.write_text(json.dumps({**result, "credential": token}))
            installed = await import_pairing(
                download, client_root=tmp_path / "client", transport=transport
            )
            assert installed["status"] == "installed"
            assert (
                await import_pairing(download, client_root=tmp_path / "client", transport=transport)
            )["status"] == "already_installed"
            assert (await client.post("/hub/v1/pairings/claim", json=body)).json() == result
            assert (
                await client.get("/hub/v1/devices", headers={"Authorization": "Bearer " + token})
            ).status_code == 403
            assert (
                await client.get(
                    "/api/v1/model-profiles", headers={"Authorization": "Bearer " + token}
                )
            ).status_code == 401
            assert (await client.get(path, headers=admin)).json()["state"] == "claimed"
            revoke = await client.post(path + "/revoke", headers=admin, json={})
            assert revoke.json()["state"] == "cancelled"
            assert (await client.post("/hub/v1/pairings/claim", json=body)).json() == {
                "detail": "pairing_invalid_or_expired"
            }
            # Cancelling recovery does not silently revoke an installed device.
            assert (
                await client.get("/hub/v1/session", headers={"Authorization": "Bearer " + token})
            ).status_code == 200

    asyncio.run(scenario())


def test_pairing_respects_admin_csrf_host_origin_and_strict_safe_errors(tmp_path):
    config, app = configured(tmp_path)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            payload = {"scope_id": str(uuid4()), "display_name": "Synthetic"}
            assert (await client.post("/hub/v1/pairings", json=payload)).status_code == 403
            origin = {"Origin": config.public_origin}
            await client.post("/hub/login", headers=origin, json={"credential": config.admin_token})
            assert (await client.post("/hub/v1/pairings", json=payload)).json() == {
                "detail": "origin_required"
            }
            for headers in (
                {"Origin": "https://untrusted.test"},
                {**origin, "Host": "untrusted.test"},
            ):
                assert (
                    await client.post("/hub/v1/pairings", headers=headers, json=payload)
                ).status_code == 403
            for extra in ({"scope_id": "global"}, {"permissions": ["admin"]}, {"display_name": ""}):
                rejected = await client.post(
                    "/hub/v1/pairings", headers=origin, json={**payload, **extra}
                )
                assert rejected.status_code == 422 and rejected.json() == {
                    "detail": "invalid_request"
                }
            assert (await client.post("/hub/v1/pairings", headers=origin, json=payload)).json() == {
                "detail": "unknown_scope"
            }
            body = {
                "pairing_id": str(uuid4()),
                "invitation_secret": secrets.token_urlsafe(32),
                "claimant_secret": secrets.token_urlsafe(32),
            }
            invalid = await client.post(
                "/hub/v1/pairings/claim", headers=origin, json={**body, "scope_id": str(uuid4())}
            )
            assert invalid.status_code == 422 and invalid.json() == {"detail": "invalid_request"}
            assert body["claimant_secret"] not in invalid.text
            missing = await client.post("/hub/v1/pairings/claim", headers=origin, json=body)
            assert missing.status_code == 401 and missing.json() == {
                "detail": "pairing_invalid_or_expired"
            }
            assert (
                await client.post(
                    "/hub/v1/pairings/claim",
                    headers={**origin, "Content-Type": "application/json"},
                    content=b"x" * 65537,
                )
            ).status_code == 413
            assert (
                await client.post("/hub/v1/pairings/claim", headers=origin, content="plain")
            ).status_code == 422

    asyncio.run(scenario())


def test_pairing_requires_explicit_public_origin_but_old_api_still_works(tmp_path):
    config, app = configured(tmp_path, origin=False)

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app), base_url=config.public_origin
        ) as client:
            admin = {"Authorization": "Bearer " + config.admin_token}
            assert (
                await client.post(
                    "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic"}
                )
            ).status_code == 201
            result = await client.post("/hub/v1/pairings", headers=admin, json={})
            assert result.status_code == 503 and result.json() == {"detail": "pairing_unavailable"}
            assert (await client.post("/hub/v1/pairings/claim", json={})).status_code == 503

    asyncio.run(scenario())
