import asyncio

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import activate_authority
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient


def test_real_memhub_core_boots_behind_authenticated_boundary(tmp_path):
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        "http://localhost:8800",
        "a" * 40,
        Fernet.generate_key(),
        governance_dir=tmp_path / "governance",
    )
    activate_authority(config.config_dir, config.governance_dir, config.encryption_key, new=True)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as client:
                assert (await client.get("/api/v1/health")).status_code == 401
                headers = {"Authorization": "Bearer " + config.admin_token}
                health = await client.get("/api/v1/health", headers=headers)
                assert health.status_code == 200
                assert health.json()["service"] == "one-memory-core"
                profiles = await client.get("/api/v1/model-profiles", headers=headers)
                assert profiles.status_code == 200
                assert (await client.get("/api/v1/onboarding", headers=headers)).status_code == 200
                authority = await client.get("/api/v2/memory-authority", headers=headers)
                assert authority.status_code == 200
                assert authority.json() == {"authority": "sdk", "migration_id": None}
                library = await client.get("/api/v2/memory-library", headers=headers)
                assert library.status_code == 200
                assert library.json()["memories"] == []
                assert library.json()["total"] == 0
                summary = await client.get("/api/v2/home-summary", headers=headers)
                assert summary.status_code == 200
                assert summary.json()["memory_count"] == 0
                assert (config.data_dir / "one-memory.sqlite3").is_file()

    asyncio.run(scenario())
