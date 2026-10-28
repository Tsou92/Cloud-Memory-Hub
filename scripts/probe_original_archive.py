"""Installed, offline archive/migration probe using only synthetic records."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.events import EventEnvelope
from httpx import ASGITransport, AsyncClient
from one_memory.memory_server.durable_queue import COMPLETED


def environment(root):
    return {
        **os.environ,
        "CMH_CONFIG_DIR": str(root / "config"),
        "CMH_DATA_DIR": str(root / "data"),
        "CMH_GOVERNANCE_DIR": str(root / "governance"),
        "CMH_PUBLIC_ORIGIN": "http://localhost:8800",
    }


async def phase(root, mode):
    os.environ.update(environment(root))
    config = HubConfig.from_environment()
    config.data_dir.mkdir(exist_ok=True, mode=0o700)
    state_path = root / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            admin = {"Authorization": "Bearer " + config.admin_token}
            if mode == "seed":
                project = await web.post(
                    "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic archive"}
                )
                assert project.status_code == 201
                scope = project.json()["project_id"]
                issued = await web.post(
                    "/hub/v1/devices", headers=admin, json={"display_name": "Synthetic probe"}
                )
                assert issued.status_code == 201
                device = issued.json()
                state["credential"] = device["credential"]
                device_id = device["device"]["device_id"]
                granted = await web.put(
                    f"/hub/v1/devices/{device_id}/grants",
                    headers=admin,
                    json={
                        "expected_revision": 1,
                        "grants": [
                            {"scope_id": scope, "permissions": ["events:write", "history:read"]}
                        ],
                    },
                )
                assert granted.status_code == 200
                source = await web.post(
                    "/hub/v1/sources",
                    headers=admin,
                    json={
                        "display_name": "Synthetic source",
                        "adapter_id": "synthetic.archive",
                        "device_id": device_id,
                        "scope_id": scope,
                    },
                )
                assert source.status_code == 201
                session = await web.get(
                    "/hub/v1/session", headers={"Authorization": "Bearer " + state["credential"]}
                )
                event = {
                    "instance_id": session.json()["instance_id"],
                    "device_id": device_id,
                    "source_id": source.json()["source_id"],
                    "scope_id": scope,
                    **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
                    "revision": 1,
                    "kind": "conversation_turn",
                    "content": {
                        "messages": [{"role": "user", "text": "合成原始记录。" * 2500}],
                        "metadata": {"synthetic": True},
                    },
                }
                state["event"] = json.loads(EventEnvelope.model_validate(event).canonical_payload())
            headers = {"Authorization": "Bearer " + state["credential"]}
            event = state["event"]
            if mode == "reap":
                queue = app.core.state.memory_write_queue
                queue.settle(state["receipt"], state=COMPLETED, hy_invoked=True)
                assert queue.clear_terminal_payloads() == 1
                assert queue.payload_for(state["receipt"]) is None
                return
            reply = await web.post("/hub/v1/events", headers=headers, json=event)
            assert reply.status_code == 202, "archive_accept_failed"
            if mode == "seed":
                state["receipt"] = reply.json()["receipt_id"]
                state_path.write_text(json.dumps(state))
                return
            assert reply.json()["receipt_id"] == state["receipt"]
            raw_id = "cmhe:" + state["receipt"]
            listed = await web.post(
                "/hub/v1/history/list", headers=headers, json={"scope_id": event["scope_id"]}
            )
            assert listed.status_code == 200
            assert [item["record_id"] for item in listed.json()["items"]] == [raw_id]
            content, offset = "", 0
            while offset is not None:
                result = await web.post(
                    "/hub/v1/history/get",
                    headers=headers,
                    json={
                        "scope_id": event["scope_id"],
                        "record_id": raw_id,
                        "content_offset": offset,
                    },
                )
                assert result.status_code == 200, "archive_read_failed"
                item = result.json()["items"][0]
                content += item["content"]
                offset = item["next_content_offset"]
            assert json.loads(content) == event


def legacy_schema(root):
    # Synthetic-only fixture: no supported production downgrade is provided.
    with sqlite3.connect(root / "config" / "remote-access.sqlite3") as db:
        for table in ("cmh_event_output_links", "cmh_event_outcomes", "cmh_lineage_schema"):
            db.execute("DROP TABLE IF EXISTS " + table)
        db.execute("DROP INDEX cmh_receipt_identity")
        db.execute("DROP INDEX cmh_original_scope")
        db.execute("ALTER TABLE cmh_event_receipts DROP COLUMN payload_json")
        db.execute("UPDATE cmh_ingest_schema SET version=1")


def run(root):
    root.mkdir(mode=0o700, exist_ok=True)
    command = [sys.executable, "-m", "cloud_memory_hub"]

    def invoke(args, *, expected=0, message=None):
        result = subprocess.run(
            args, env=environment(root), capture_output=True, text=True, timeout=120
        )
        # Logs contain only generated probe data and remain within the private root.
        (root / "last-phase.log").write_text(result.stdout + result.stderr)
        assert result.returncode == expected, "archive_subprocess_failed"
        if message:
            assert message in result.stdout + result.stderr, "archive_subprocess_result_mismatch"

    def child(mode):
        invoke([sys.executable, str(Path(__file__).resolve()), mode, str(root)])

    invoke([*command, "init"])
    child("seed")
    legacy_schema(root)
    invoke([*command, "serve"], expected=2, message="original_retention_migration_required")
    invoke([*command, "migrate-originals"], expected=2, message="--writers-stopped")
    invoke(
        [*command, "migrate-originals", "--writers-stopped"], message="recovered 1 accepted events"
    )
    invoke(
        [*command, "migrate-originals", "--writers-stopped"], message="recovered 0 accepted events"
    )
    child("read")
    child("reap")
    child("read")
    print("Archive migration, restart, queue cleanup and complete scoped reads passed.")


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] in {"seed", "read", "reap"}:
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    if "--installed" in sys.argv:
        from cloud_memory_hub import original_archive

        assert "/site-packages/" in original_archive.__file__
    if len(sys.argv) == 3 and sys.argv[1] == "prepare":
        run(Path(sys.argv[2]))
        return
    with TemporaryDirectory(prefix="cmh-original-archive-") as temporary:
        run(Path(temporary))


if __name__ == "__main__":
    main()
