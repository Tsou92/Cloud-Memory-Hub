import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from cloud_memory_hub.__main__ import initialize
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.governance_authority import (
    activate_authority,
    journal_key,
    open_authority,
)
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal, Intent
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    NamedResource,
)
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from one_memory.api import _source_writes_on
from starlette.responses import JSONResponse, StreamingResponse
from test_remote_ingest import config_for, provision


def intent_for(authority, *, scope="global", action="forget"):
    return Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action=action,
        targets=(authority.journal.target(scope, "content", "Synthetic retained content"),),
    )


def test_explicit_migration_preserves_identity_grants_and_credentials(tmp_path):
    config = HubConfig(
        tmp_path / "data",
        tmp_path / "config",
        "http://localhost:8800",
        "a" * 40,
        Fernet.generate_key(),
        governance_dir=tmp_path / "governance",
    )
    old = DeviceAccessStore(config.config_dir / "remote-access.sqlite3")
    issued = old.create_device(NamedResource(display_name="Synthetic device"))
    old.replace_grants(
        issued.device["device_id"],
        GrantRequest(
            expected_revision=1, grants=[{"scope_id": "global", "permissions": ["memory:read"]}]
        ),
    )
    credential = issued.credential.get_secret_value()
    before = old.authenticate(credential)
    with pytest.raises(GovernanceError, match="migration_required"):
        create_hub(config)
    assert not config.data_dir.exists()
    authority = activate_authority(config.config_dir, config.governance_dir, config.encryption_key)
    assert authority.access.instance_id == old.instance_id
    assert authority.access.authenticate(credential) == before
    assert authority.access.schema_version == 3
    assert authority.verify() == {}
    # A process that opened version 1 before migration also refuses new operations.
    with pytest.raises(AccessError, match="access_store_changed"):
        old.authenticate(credential)
    again = activate_authority(config.config_dir, config.governance_dir, config.encryption_key)
    assert again.journal.ledger_id == authority.journal.ledger_id
    assert again.access.authenticate(credential) == before


def test_serve_never_creates_missing_authority_and_rejects_wrong_key(tmp_path):
    config = config_for(tmp_path)
    with pytest.raises(GovernanceError, match="binding_mismatch"):
        create_hub(replace(config, encryption_key=Fernet.generate_key()))
    journal = config.governance_dir / "journal.sqlite3"
    journal.unlink()
    with pytest.raises(GovernanceError, match="store_unavailable"):
        create_hub(config)
    assert not journal.exists()
    identity = config.config_dir / "remote-access.sqlite3"
    identity.unlink()
    with pytest.raises(AccessError, match="access_store_unavailable"):
        create_hub(config)
    assert not identity.exists()


@pytest.mark.parametrize("directory", ["config", "config/child", ".", "data", "data/child"])
def test_governance_must_survive_business_or_config_directory_replacement(tmp_path, directory):
    config = config_for(tmp_path)
    with pytest.raises(GovernanceError, match="directory_not_separate"):
        create_hub(replace(config, governance_dir=tmp_path / directory))


def test_old_copy_and_same_sequence_fork_are_rejected_by_external_witness(tmp_path):
    config = config_for(tmp_path)
    authority = open_authority(config)
    first = authority.append(intent_for(authority))
    backup = tmp_path / "old.sqlite3"
    shutil.copyfile(authority.journal.path, backup)
    second = authority.append(intent_for(authority, action="purge"))
    assert second.sequence == first.sequence + 1
    shutil.copyfile(backup, authority.journal.path)
    with pytest.raises(GovernanceError, match="watermark_ahead"):
        open_authority(config)
    authority.journal.append(intent_for(authority))
    with pytest.raises(GovernanceError, match="watermark_diverged"):
        open_authority(config)


def test_replacing_journal_cannot_reset_binding(tmp_path):
    config = config_for(tmp_path)
    authority = open_authority(config)
    authority.journal.path.unlink()
    GovernanceJournal.initialize(
        authority.journal.path,
        instance_id=authority.access.instance_id,
        key=journal_key(config.encryption_key),
    )
    with pytest.raises(GovernanceError, match="binding_mismatch"):
        activate_authority(config.config_dir, config.governance_dir, config.encryption_key)


def test_partial_initialization_can_resume_only_with_explicit_migration(tmp_path):
    config = config_for(tmp_path)
    path = config.config_dir / "remote-access.sqlite3"
    # Simulate an interrupted initial binding, not a valid initialized runtime.
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE cmh_governance_binding")
        db.execute("DROP TABLE cmh_governance_floors")
        db.execute("DROP TABLE cmh_admission_witnesses")
        db.execute("DROP TABLE cmh_admission_binding")
        db.execute("PRAGMA user_version=1")
    original = (config.governance_dir / "journal.sqlite3").read_bytes()
    with pytest.raises(GovernanceError, match="migration_required"):
        create_hub(config)
    authority = activate_authority(config.config_dir, config.governance_dir, config.encryption_key)
    assert authority.journal.path.read_bytes() == original
    authority.require_ready()


def test_concurrent_writers_advance_witnesses_without_downgrading(tmp_path):
    config = config_for(tmp_path)
    authority = open_authority(config)
    repeated = intent_for(authority)
    intents = [repeated] * 6 + [intent_for(authority) for _ in range(6)]

    def append(intent):
        return open_authority(config).append(intent)

    with ThreadPoolExecutor(max_workers=6) as pool:
        checkpoints = list(pool.map(append, intents))
    assert sorted({item.sequence for item in checkpoints}) == list(range(1, 8))
    assert open_authority(config).verify()["global"].sequence == 7


def test_process_death_between_journal_and_witness_recovers_without_losing_intent(tmp_path):
    config_dir, governance_dir = tmp_path / "config", tmp_path / "governance"
    initialize(config_dir, governance_dir=governance_dir)
    key = read_private_file(config_dir / "encryption-key").strip()
    script = """
import os,sys
from pathlib import Path
from uuid import uuid4
from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.governance_journal import Intent
config, governance = map(Path, sys.argv[1:])
a = activate_authority(config, governance, read_private_file(config/'encryption-key').strip())
original = a.journal.append
def crash(intent):
    original(intent)
    os._exit(23)
a.journal.append = crash
a.append(Intent(operation_id=str(uuid4()), actor_id=str(uuid4()), scope_id='global',
    action='purge', targets=(a.journal.target('global','raw','Synthetic raw record'),)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(config_dir), str(governance_dir)],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 23, result.stderr.decode()
    with sqlite3.connect(config_dir / "remote-access.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM cmh_governance_floors").fetchone()[0] == 0
    recovered = activate_authority(config_dir, governance_dir, key)
    assert recovered.verify()["global"].sequence == 1
    assert recovered.journal.changes("global")["entries"][0]["intent"]["action"] == "purge"
    with pytest.raises(GovernanceError, match="execution_pending"):
        recovered.require_ready()


@pytest.mark.parametrize("damage", ["missing", "permissions", "intent", "identity", "binding"])
def test_runtime_fault_blocks_core_remote_health_and_background_claims(tmp_path, damage):
    config = config_for(tmp_path)
    app = create_hub(config)
    authority = app.governance

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, device, _, event = await provision(web, config)
                source = "cmh:" + event["source_id"]
                assert _source_writes_on(app.core, source)
                if damage == "missing":
                    authority.journal.path.unlink()
                elif damage == "permissions":
                    os.chmod(authority.journal.path, 0o644)
                elif damage == "intent":
                    authority.append(intent_for(authority))
                elif damage == "identity":
                    authority.access.path.unlink()
                else:
                    with sqlite3.connect(authority.access.path) as db:
                        db.execute("UPDATE cmh_governance_binding SET ledger_id=?", (str(uuid4()),))
                for path, headers in [
                    ("/healthz", {}),
                    ("/api/v2/memory-library", admin),
                    ("/hub/v1/session", device),
                    ("/hub/login", {}),
                ]:
                    result = await web.get(path, headers=headers)
                    assert result.status_code == 503
                    assert result.json() == {"detail": "memory_authority_unavailable"}
                assert (
                    await web.post("/hub/v1/events", headers=device, json=event)
                ).status_code == 503
                assert not _source_writes_on(app.core, source)
                assert not _source_writes_on(app.core, "one-memory-manual")
                assert not _source_writes_on(app.core, "synthetic-other-source")

    asyncio.run(scenario())


def test_response_prepared_before_intent_is_not_published_after_it(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def read_then_govern():
        app.governance.append(intent_for(app.governance))
        return JSONResponse({"content": "Synthetic obsolete memory"})

    app.core.add_api_route("/api/test-governance", read_then_govern)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                reply = await web.get(
                    "/api/test-governance",
                    headers={"Authorization": "Bearer " + config.admin_token},
                )
                assert reply.status_code == 503
                assert "obsolete" not in reply.text

    asyncio.run(scenario())


def test_stream_ends_before_obsolete_chunk_when_authority_changes(tmp_path):
    config = config_for(tmp_path)
    authority = open_authority(config)

    async def stream(scope, receive, send):
        async def chunks():
            yield b"data: synthetic-first\n\n"
            authority.append(intent_for(authority))
            yield b"data: synthetic-obsolete\n\n"

        await StreamingResponse(chunks(), media_type="text/event-stream")(scope, receive, send)

    app = CloudBoundary(stream, config, "synthetic-internal", governance=authority)

    async def scenario():
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            reply = await web.get(
                "/api/test-stream", headers={"Authorization": "Bearer " + config.admin_token}
            )
            assert reply.status_code == 200
            assert reply.text == "data: synthetic-first\n\n"

    asyncio.run(scenario())


def test_lifespan_rechecks_before_starting_core_workers(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)
    app.governance.journal.path.unlink()
    messages = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        messages.append(message)

    asyncio.run(app({"type": "lifespan"}, receive, send))
    assert messages == [
        {"type": "lifespan.startup.failed", "message": "memory_authority_unavailable"}
    ]
    assert not (config.data_dir / "one-memory.sqlite3").exists()


def test_upstream_delete_is_not_exposed_as_durable_purge(tmp_path):
    config = config_for(tmp_path)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                reply = await web.delete(
                    "/api/v2/memories/synthetic",
                    headers={"Authorization": "Bearer " + config.admin_token},
                )
                assert reply.status_code == 409
                assert reply.json() == {"detail": "governance_workflow_required"}
                assert app.governance.verify() == {}

    asyncio.run(scenario())
