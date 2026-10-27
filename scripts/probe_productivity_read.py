"""Exercise installed unified reads across processes with real synthetic SDK stores."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.read_contract import ReadReply
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, WorkingFact
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread

QUERY = "The project uses a blue design."


async def populate(sdk, namespace, identity):
    for variant in ("working", "coding"):
        store = CodingMemoryStore(
            sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
        )
        await store.initialize()
        try:
            await store.insert(
                CodingMemory(
                    experience_id=identity, user_id=namespace, task=QUERY, solution="Reuse blue."
                )
            )
            await store.insert_fact(
                WorkingFact(fact_id=identity, user_id=namespace, statement=QUERY)
            )
        finally:
            await store.close()


async def phase(root, mode):
    state_path = root / "state.json"
    state = json.loads(state_path.read_text())
    config = HubConfig(
        root / "data",
        root / "config",
        "http://localhost:8800",
        state["admin"],
        state["key"].encode(),
        governance_dir=root / "governance",
    )
    if mode == "seed":
        activate_authority(
            config.config_dir, config.governance_dir, config.encryption_key, new=True
        )
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            if mode == "seed":
                admin = {"Authorization": "Bearer " + config.admin_token}
                project = await web.post(
                    "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic read probe"}
                )
                state["scope"] = project.json()["project_id"]
                device = await web.post(
                    "/hub/v1/devices", headers=admin, json={"display_name": "Synthetic reader"}
                )
                state["credential"] = device.json()["credential"]
                result = await web.put(
                    "/hub/v1/devices/" + device.json()["device"]["device_id"] + "/grants",
                    headers=admin,
                    json={
                        "expected_revision": 1,
                        "grants": [{"scope_id": state["scope"], "permissions": ["memory:read"]}],
                    },
                )
                assert result.status_code == 200
                instance = app.core.state.cmh_reads.access.instance_id
                namespace = f"cmh:{instance}:{state['scope']}"
                state["native"] = str(uuid4())
                await _joined_thread(
                    lambda: sdk._loop_thread.run(populate(sdk, namespace, state["native"]))
                )
                result = await _joined_thread(
                    sdk.import_memory_exact,
                    QUERY,
                    memory_id=state["native"],
                    user_id=namespace,
                    agent_id="synthetic-agent",
                    layer="l2_fact",
                    request_id=str(uuid4()),
                )
                assert result["success"]
                state_path.write_text(json.dumps(state))
                return

            def forbidden(*args, **kwargs):
                raise AssertionError("unexpected_productivity_initialization")

            sdk._get_coding_writer = forbidden
            CodingMemoryStore.initialize = forbidden
            headers = {"Authorization": "Bearer " + state["credential"]}

            async def read(operation, **body):
                response = await web.post(
                    "/hub/v1/memories/" + operation,
                    headers=headers,
                    json={"scope_id": state["scope"], **body},
                )
                assert response.status_code == 200, "unified_read_failed"
                return ReadReply.model_validate(response.json())

            expected = [state["native"]] + [
                f"cmhp:{v}:{k}:{state['native']}" for v in ("w", "c") for k in ("e", "f")
            ]
            ids, offset = [], 0
            while offset is not None:
                reply = await read("list", limit=2, offset=offset)
                ids.extend(item.record_id for item in reply.items)
                offset = reply.next_offset
            assert ids == expected
            reply = await read("search", query=QUERY, limit=10)
            assert {item.record_id for item in reply.items} == set(expected)
            for identity in expected:
                reply = await read("get", record_id=identity)
                assert reply.items[0].record_id == identity
                assert QUERY in reply.items[0].content
            assert not sdk._coding_stores


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] in {"seed", "read"}:
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    if "--installed" in sys.argv:
        from cloud_memory_hub import productivity_read

        assert "/site-packages/" in productivity_read.__file__
    with TemporaryDirectory(prefix="cmh-productivity-read-") as temporary:
        root = Path(temporary)
        (root / "state.json").write_text(
            json.dumps({"admin": secrets.token_urlsafe(40), "key": Fernet.generate_key().decode()})
        )
        for mode in ("seed", "read"):
            with (root / (mode + ".log")).open("w") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError("productivity_read_probe_failed_" + mode)
    print(
        "Unified list/search/get passed across processes for ordinary, working and coding stores."
    )


if __name__ == "__main__":
    main()
