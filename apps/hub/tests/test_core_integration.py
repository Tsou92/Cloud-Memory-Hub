import asyncio

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient


def test_real_memhub_core_boots_behind_authenticated_boundary(tmp_path):
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        "http://localhost:8800",
        "a" * 40,
        Fernet.generate_key(),
    )
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
                assert (config.data_dir / "one-memory.sqlite3").is_file()

    asyncio.run(scenario())
