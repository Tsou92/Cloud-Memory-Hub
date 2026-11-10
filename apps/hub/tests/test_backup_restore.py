"""ADR-0063 / FR-11, FR-13: stopped restore, current authority, failure fences."""

import json
import os
import secrets
import sqlite3
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
from cloud_memory_hub import backup_restore, backup_store
from cloud_memory_hub.backup_restore import RestoreError, import_backup, restore_backup
from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_journal import Intent
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    NamedResource,
    RevisionRequest,
    ScopeGrant,
)
from cloud_memory_hub.remote_ingest import RemoteIngest, SourceRegistration
from cloud_memory_hub.remote_pairing import PairingClaim, PairingRequest, PairingStore
from cloud_memory_hub.storage_lease import (
    LOCK_NAME,
    RESTORE_MARKER,
    StorageLease,
    StorageLeaseError,
    require_restore_complete,
)
from cloud_memory_hub.write_admissions import WriteAdmissions
from one_memory.memory_server.migrations import memory_write_jobs_sqlite_schema
from test_backup_store import layout as base_layout
from test_backup_store import private_file, row
from test_write_admissions import enroll, event_for_store, prepare


@pytest.fixture
def layout(tmp_path):
    return base_layout.__wrapped__(tmp_path)


def saved(layout):
    config, root = layout
    manifest = backup_store.create_backup(config, root, revision="synthetic-old")
    return root / manifest["backup_id"], manifest


def access(config):
    return DeviceAccessStore(config.config_dir / "remote-access.sqlite3", require_existing=True)


def change_data(config):
    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
        db.execute("UPDATE synthetic SET value='Synthetic newer core row'")
    private_file(config.data_dir / "new-private.bin", b"Synthetic later payload")


def roots(config):
    return config.data_dir, config.config_dir, config.governance_dir


def source(config, device=None):
    authority = access(config)
    device = device or authority.create_device(NamedResource(display_name="Synthetic device"))
    ingest = RemoteIngest(authority, lambda: None)
    item = ingest.create_source(
        SourceRegistration(
            device_id=device.device["device_id"],
            scope_id="global",
            display_name="Synthetic source",
            adapter_id="cloud-memory-hub.agent",
        )
    )
    return device, ingest, item


def tables(path, names):
    with sqlite3.connect(path) as db:
        return {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in names}


def test_history_restores_business_and_preserves_current_security_sources_and_key(layout):
    config, root = layout
    device, ingest, original = source(config)
    old_token = device.credential.get_secret_value()
    revoked = access(config).create_device(NamedResource(display_name="Synthetic revoked device"))
    selected, manifest = saved(layout)
    change_data(config)
    authority = access(config)
    rotated = authority.rotate(device.device["device_id"], RevisionRequest(expected_revision=1))
    authority.replace_grants(
        device.device["device_id"],
        GrantRequest(
            expected_revision=2,
            grants=(ScopeGrant(scope_id="global", permissions=("memory:read",)),),
        ),
    )
    authority.revoke(revoked.device["device_id"], RevisionRequest(expected_revision=1))
    ingest.revoke_source(original["source_id"], RevisionRequest(expected_revision=1))
    source(config)
    private_file(config.config_dir / "admin-token", b"n" * 48 + b"\n")
    before = tables(authority.path, backup_restore._SECURITY)
    governance = {path.name: path.read_bytes() for path in config.governance_dir.iterdir()}
    lock_ids = {path: (path / LOCK_NAME).stat().st_ino for path in roots(config)}
    result = restore_backup(config, selected, root)
    assert result["status"] == "files_restored_pending_startup"
    assert result["backup_id"] == manifest["backup_id"]
    assert result["sources_requiring_reconnection"] == 1
    protected = root / result["protective_backup_id"]
    assert row(protected / "data/one-memory.sqlite3") == "Synthetic newer core row"
    assert row(config.data_dir / "one-memory.sqlite3") == "Synthetic core row"
    assert not (config.data_dir / "new-private.bin").exists()
    assert tables(authority.path, backup_restore._SECURITY) == before
    assert read_private_file(config.config_dir / "admin-token").strip() == b"n" * 48
    assert read_private_file(config.config_dir / "encryption-key").strip() == config.encryption_key
    assert {path.name: path.read_bytes() for path in config.governance_dir.iterdir()} == governance
    assert lock_ids == {path: (path / LOCK_NAME).stat().st_ino for path in roots(config)}
    assert all(not (path / RESTORE_MARKER).exists() for path in roots(config))
    with pytest.raises(AccessError):
        access(config).authenticate(old_token)
    with pytest.raises(AccessError):
        access(config).authenticate(revoked.credential.get_secret_value())
    assert access(config).authenticate(rotated.credential.get_secret_value())["revision"] == 3
    assert RemoteIngest(access(config), lambda: None).list_sources()[0]["status"] == "revoked"
    assert str(config.data_dir) not in json.dumps(result)
    assert old_token not in json.dumps(result)


def test_new_governance_and_external_witnesses_survive_rollback(layout):
    config, root = layout
    selected, _ = saved(layout)
    authority = open_authority(config)
    intent = Intent(
        operation_id=str(uuid4()),
        actor_id=authority.access.instance_id,
        scope_id="global",
        action="forget",
        targets=(authority.journal.target("global", "raw", "synthetic"),),
    )
    execution = ExecutionStore(authority.journal)
    execution.plan(intent, {"synthetic": "raw"})
    authority.append(intent)
    execution.complete(intent)
    heads = authority.verify()
    result = restore_backup(config, selected, root)
    assert result["status"] == "files_restored_pending_startup"
    restored = open_authority(config, read_only=True)
    assert restored.verify() == heads
    assert restored.runtime_recovery_required  # Startup still must perform replay.


def test_different_instance_or_key_rejected_before_live_changes(layout, tmp_path):
    config, root = layout
    selected, _ = saved(layout)
    from cloud_memory_hub.__main__ import initialize

    initialize(tmp_path / "other-config", governance_dir=tmp_path / "other-governance")
    other = replace(
        config,
        config_dir=tmp_path / "other-config",
        governance_dir=tmp_path / "other-governance",
        encryption_key=read_private_file(tmp_path / "other-config/encryption-key").strip(),
    )
    with pytest.raises(RestoreError, match="backup_restore_key_mismatch"):
        restore_backup(other, selected, root)
    assert all(not (path / RESTORE_MARKER).exists() for path in roots(other))


def test_diverged_or_future_governance_prefix_rejected(layout):
    config, root = layout
    old, _ = saved(layout)
    # Copy a genuine complete old governance state, then create a signed newer version.
    authority = open_authority(config)
    intent = Intent(
        operation_id=str(uuid4()),
        actor_id=authority.access.instance_id,
        scope_id="global",
        action="forget",
        targets=(authority.journal.target("global", "raw", "synthetic"),),
    )
    execution = ExecutionStore(authority.journal)
    execution.plan(intent, {"synthetic": "raw"})
    authority.append(intent)
    execution.complete(intent)
    future, _ = saved(layout)
    for name, target in (("config", config.config_dir), ("governance", config.governance_dir)):
        for path in target.iterdir():
            if path.name != LOCK_NAME:
                path.unlink()
        for path in (old / name).iterdir():
            backup_store._copy(path, target / path.name)
    with pytest.raises(RestoreError, match="backup_restore_validation_failed"):
        restore_backup(config, future, root)
    assert not (config.data_dir / RESTORE_MARKER).exists()


def test_source_identity_change_refuses_without_replacing_live_data(layout):
    config, root = layout
    _, _, original = source(config)
    selected, _ = saved(layout)
    change_data(config)
    with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
        db.execute(
            "UPDATE cmh_sources SET adapter_id='synthetic.changed' WHERE source_id=?",
            (original["source_id"],),
        )
    with pytest.raises(RestoreError, match="backup_restore_sources_mismatch"):
        restore_backup(config, selected, root)
    assert row(config.data_dir / "one-memory.sqlite3") == "Synthetic newer core row"


def test_staged_trigger_cannot_undo_current_device_revocation(layout):
    config, root = layout
    authority = access(config)
    device = authority.create_device(NamedResource(display_name="Synthetic protected device"))
    with sqlite3.connect(authority.path) as db:
        db.execute(
            "CREATE TRIGGER synthetic_stale AFTER UPDATE ON devices "
            "BEGIN UPDATE devices SET status='active' WHERE device_id=NEW.device_id; END"
        )
    selected, _ = saved(layout)
    with sqlite3.connect(authority.path) as db:
        db.execute("DROP TRIGGER synthetic_stale")
    authority.revoke(device.device["device_id"], RevisionRequest(expected_revision=1))
    with pytest.raises(RestoreError, match="backup_restore_security_merge_mismatch"):
        restore_backup(config, selected, root)
    with pytest.raises(AccessError):
        access(config).authenticate(device.credential.get_secret_value())
    assert all(not (path / RESTORE_MARKER).exists() for path in roots(config))


def queue_job(config, *, state="pending", invoked=0):
    authority = open_authority(config)
    admissions = WriteAdmissions(authority.access, authority.journal)
    event = event_for_store(admissions)
    binding = enroll(admissions, event)
    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
        for statement in memory_write_jobs_sqlite_schema():
            db.execute(statement)
        db.execute(
            "INSERT INTO memory_write_jobs(job_id,session_key,batch_id,"
            "idempotency_key,payload_json,"
            "state,hy_invoked,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                "synthetic",
                "synthetic",
                binding.request_id,
                "{}",
                state,
                invoked,
                "synthetic",
                "synthetic",
            ),
        )
    return admissions, event, binding


def test_old_pending_current_possible_is_not_replayed(layout):
    config, root = layout
    admissions, event, binding = queue_job(config)
    selected, _ = saved(layout)
    admissions.permit_once(
        prepare(event)(binding.request_id), require_allowed=lambda _binding: None
    )
    with pytest.raises(RestoreError, match="backup_restore_write_uncertain"):
        restore_backup(config, selected, root)
    assert not (config.data_dir / RESTORE_MARKER).exists()


def test_enrolled_pending_preserves_queue_without_replaying(layout):
    config, root = layout
    _, _, binding = queue_job(config)
    selected, _ = saved(layout)
    result = restore_backup(config, selected, root)
    assert result["status"] == "files_restored_pending_startup"
    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
        assert db.execute(
            "SELECT state,hy_invoked FROM memory_write_jobs WHERE idempotency_key=?",
            (binding.request_id,),
        ).fetchone() == ("pending", 0)


@pytest.mark.parametrize("state,invoked", [("unknown", 0), ("pending", 1)])
def test_unknown_queue_refuses_import(layout, tmp_path, state, invoked):
    config, _ = layout
    queue_job(config, state=state, invoked=invoked)
    selected, _ = saved(layout)
    targets = [tmp_path / ("import-" + name) for name in ("data", "config", "governance")]
    with pytest.raises(RestoreError, match="backup_restore_write_uncertain"):
        import_backup(
            selected,
            data_dir=targets[0],
            config_dir=targets[1],
            governance_dir=targets[2],
            backup_root=tmp_path / "import-versions",
            source_stopped=True,
        )
    assert all({path.name for path in root.iterdir()} == {LOCK_NAME} for root in targets)


def test_empty_import_keeps_all_identity_and_credentials(layout, tmp_path):
    config, _ = layout
    selected, manifest = saved(layout)
    targets = [tmp_path / ("import-" + name) for name in ("data", "config", "governance")]
    result = import_backup(
        selected,
        data_dir=targets[0],
        config_dir=targets[1],
        governance_dir=targets[2],
        backup_root=tmp_path / "import-versions",
        source_stopped=True,
    )
    assert result["protective_backup_id"] is None
    assert result["instance_id"] == manifest["instance_id"]
    assert row(targets[0] / "one-memory.sqlite3") == row(config.data_dir / "one-memory.sqlite3")
    assert (targets[1] / "admin-token").read_bytes() == (
        selected / "config/admin-token"
    ).read_bytes()
    assert (targets[1] / "encryption-key").read_bytes() == (
        selected / "config/encryption-key"
    ).read_bytes()
    assert access(replace(config, config_dir=targets[1])).instance_id == manifest["instance_id"]
    assert all(not (path / RESTORE_MARKER).exists() for path in targets)


@pytest.mark.parametrize("mode", ["history", "empty_import"])
def test_restore_cancels_pending_and_claimed_pairings_without_revoking_devices(
    layout, tmp_path, mode
):
    config, root = layout
    authority = access(config)
    pairings = PairingStore(RemoteIngest(authority, lambda: None), config.public_origin)
    project = authority.create_project(NamedResource(display_name="Synthetic pairing project"))
    requests = []
    for name in ("pending", "claimed", "cancelled"):
        invitation = pairings.create(
            PairingRequest(scope_id=project["project_id"], display_name=f"Synthetic {name}")
        )
        requests.append(
            PairingClaim(
                pairing_id=invitation.pairing["pairing_id"],
                invitation_secret=invitation.invitation_secret.get_secret_value(),
                claimant_secret=secrets.token_urlsafe(32),
            )
        )
    claimed = pairings.claim(requests[1])
    token = f"cmhdev_v1_{claimed['device_id']}_{requests[1].claimant_secret.get_secret_value()}"
    pairings.revoke(requests[2].pairing_id)
    selected, _ = saved(layout)
    # The selected version still contains an unconsumed invitation at this point.
    pairings.claim(requests[0])
    if mode == "history":
        restore_backup(config, selected, root)
        restored = config
    else:
        targets = [
            tmp_path / ("pairing-import-" + name) for name in ("data", "config", "governance")
        ]
        import_backup(
            selected,
            data_dir=targets[0],
            config_dir=targets[1],
            governance_dir=targets[2],
            backup_root=tmp_path / "pairing-import-versions",
            source_stopped=True,
        )
        restored = replace(
            config, data_dir=targets[0], config_dir=targets[1], governance_dir=targets[2]
        )
    current = PairingStore(RemoteIngest(access(restored), lambda: None), restored.public_origin)
    for request in requests:
        assert current.get(request.pairing_id)["state"] == "cancelled"
        with pytest.raises(AccessError, match="pairing_invalid_or_expired"):
            current.claim(request)
    assert current.access.authenticate(token)["device_id"] == claimed["device_id"]
    assert (
        current.ingest.source_access(token, claimed["source_id"])["scope_id"] == claimed["scope_id"]
    )
    # Cancelling restored invitations does not disable fresh enrollment.
    fresh = current.create(
        PairingRequest(scope_id=project["project_id"], display_name="Synthetic new")
    )
    assert (
        current.claim(
            PairingClaim(
                pairing_id=fresh.pairing["pairing_id"],
                invitation_secret=fresh.invitation_secret.get_secret_value(),
                claimant_secret=secrets.token_urlsafe(32),
            )
        )["device_id"]
        != claimed["device_id"]
    )


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_import_rejects_nonempty_or_unsafe_target(layout, tmp_path, kind):
    selected, _ = saved(layout)
    targets = [tmp_path / ("import-" + name) for name in ("data", "config", "governance")]
    for path in targets:
        path.mkdir(mode=0o700)
    if kind == "directory":
        (targets[1] / "private").mkdir(mode=0o700)
    elif kind == "symlink":
        (targets[1] / "private").symlink_to(tmp_path / "missing")
    else:
        private_file(targets[1] / "private", b"Synthetic data")
    with pytest.raises(RestoreError, match="backup_import_target_not_empty"):
        import_backup(
            selected,
            data_dir=targets[0],
            config_dir=targets[1],
            governance_dir=targets[2],
            backup_root=tmp_path / "import-versions",
            source_stopped=True,
        )
    assert (targets[1] / "private").exists() or (targets[1] / "private").is_symlink()
    assert all(not (path / RESTORE_MARKER).exists() for path in targets)


def test_import_requires_explicit_source_stop_and_separate_roots(layout, tmp_path):
    config, root = layout
    selected, _ = saved(layout)
    with pytest.raises(RestoreError, match="backup_import_source_must_be_stopped"):
        import_backup(
            selected,
            data_dir=config.data_dir,
            config_dir=config.config_dir,
            governance_dir=config.governance_dir,
            backup_root=root,
        )
    with pytest.raises(RestoreError, match="backup_roots_overlap"):
        import_backup(
            selected,
            data_dir=tmp_path / "new",
            config_dir=tmp_path / "new/config",
            governance_dir=tmp_path / "new-governance",
            backup_root=root,
            source_stopped=True,
        )


def test_alias_roots_rejected_before_empty_import_changes(layout, tmp_path):
    selected, _ = saved(layout)
    alias = tmp_path / "alias"
    alias.mkdir(mode=0o700)
    target = tmp_path / "target"
    with pytest.raises(RestoreError, match="backup_roots_overlap"):
        import_backup(
            selected,
            data_dir=alias / ".." / "target",
            config_dir=target,
            governance_dir=tmp_path / "new-governance",
            backup_root=tmp_path / "new-backups",
            source_stopped=True,
        )
    assert not target.exists()
    assert not (tmp_path / "new-backups").exists()


def test_writer_change_between_protection_and_second_lease_refuses(layout, monkeypatch):
    config, root = layout
    selected, _ = saved(layout)
    original = backup_store.create_backup

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        change_data(config)
        return result

    monkeypatch.setattr(backup_store, "create_backup", changed)
    with pytest.raises(RestoreError, match="backup_restore_source_changed") as error:
        restore_backup(config, selected, root)
    assert error.value.protective_backup_id
    assert row(config.data_dir / "one-memory.sqlite3") == "Synthetic newer core row"


def test_running_lease_refuses_before_protective_version(layout):
    config, root = layout
    selected, _ = saved(layout)
    with StorageLease(*roots(config)):
        with pytest.raises(RestoreError, match="backup_storage_in_use"):
            restore_backup(config, selected, root)
    assert len(backup_store.list_backups(root)) == 1


def test_interrupted_cross_volume_restore_keeps_marker_stage_protection_and_lock(
    layout, monkeypatch
):
    config, root = layout
    selected, _ = saved(layout)
    change_data(config)
    lock_ids = {path: (path / LOCK_NAME).stat().st_ino for path in roots(config)}
    original = backup_restore._replace_contents

    def failed(source, target):
        if target == config.config_dir:
            raise OSError("Synthetic write failure")
        original(source, target)

    monkeypatch.setattr(backup_restore, "_replace_contents", failed)
    with pytest.raises(RestoreError, match="backup_restore_incomplete") as error:
        restore_backup(config, selected, root)
    assert error.value.protective_backup_id
    assert (root / (".restore-" + error.value.operation_id) / "payload").is_dir()
    assert all((path / RESTORE_MARKER).is_file() for path in roots(config))
    assert lock_ids == {path: (path / LOCK_NAME).stat().st_ino for path in roots(config)}
    with pytest.raises(StorageLeaseError, match="restore_incomplete"):
        require_restore_complete(*roots(config))
    with pytest.raises(backup_store.BackupError, match="backup_restore_incomplete"):
        backup_store.create_backup(config, root, revision="must-not-snapshot-partial")
    with pytest.raises(RestoreError, match="backup_restore_incomplete"):
        restore_backup(config, selected, root)
    assert len(backup_store.list_backups(root)) == 2


@pytest.mark.parametrize("command", ["serve", "supervise"])
def test_real_startup_blocks_marker_before_reading_missing_credentials(layout, command):
    config, _ = layout
    private_file(config.data_dir / RESTORE_MARKER, b"Synthetic interrupted restore")
    (config.config_dir / "admin-token").unlink()
    env = {
        **os.environ,
        "CMH_DATA_DIR": str(config.data_dir),
        "CMH_CONFIG_DIR": str(config.config_dir),
        "CMH_GOVERNANCE_DIR": str(config.governance_dir),
    }
    result = subprocess.run(
        [sys.executable, "-m", "cloud_memory_hub", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "cloud-memory-hub: restore_incomplete" in result.stderr


def test_dangling_marker_blocks_shared_gate(layout):
    config, _ = layout
    (config.config_dir / RESTORE_MARKER).symlink_to(config.config_dir / "missing")
    with pytest.raises(StorageLeaseError, match="restore_incomplete"):
        require_restore_complete(*roots(config))


def test_different_instance_even_with_same_key_is_rejected(layout, tmp_path):
    from cloud_memory_hub.governance_authority import activate_authority

    config, root = layout
    selected, _ = saved(layout)
    other_config, other_governance = tmp_path / "other-config", tmp_path / "other-governance"
    activate_authority(other_config, other_governance, config.encryption_key, new=True)
    private_file(other_config / "admin-token", b"s" * 48 + b"\n")
    private_file(other_config / "encryption-key", config.encryption_key + b"\n")
    other = replace(config, config_dir=other_config, governance_dir=other_governance)
    with pytest.raises(RestoreError, match="backup_restore_identity_mismatch"):
        restore_backup(other, selected, root)
    assert not (other_config / RESTORE_MARKER).exists()


def test_protective_backup_failure_keeps_current_files_and_creates_no_marker(layout, monkeypatch):
    config, root = layout
    selected, _ = saved(layout)
    change_data(config)
    before = backup_restore._fingerprint(roots(config))

    def fail(*_args, **_kwargs):
        raise backup_store.BackupError("backup_create_failed")

    monkeypatch.setattr(backup_store, "create_backup", fail)
    with pytest.raises(RestoreError, match="backup_create_failed"):
        restore_backup(config, selected, root)
    assert backup_restore._fingerprint(roots(config)) == before
    assert all(not (path / RESTORE_MARKER).exists() for path in roots(config))


def test_import_interruption_retains_independent_staging_and_blocks_start(
    layout, tmp_path, monkeypatch
):
    selected, _ = saved(layout)
    targets = [tmp_path / ("import-" + name) for name in ("data", "config", "governance")]
    root = tmp_path / "import-versions"
    original = backup_restore._replace_contents

    def fail(source, target):
        if target == targets[1]:
            raise OSError("Synthetic import failure")
        original(source, target)

    monkeypatch.setattr(backup_restore, "_replace_contents", fail)
    with pytest.raises(RestoreError, match="backup_restore_incomplete") as error:
        import_backup(
            selected,
            data_dir=targets[0],
            config_dir=targets[1],
            governance_dir=targets[2],
            backup_root=root,
            source_stopped=True,
        )
    prepared = root / (".restore-" + error.value.operation_id) / "payload"
    assert (prepared / "config/admin-token").is_file()
    assert (prepared / "governance/journal.sqlite3").is_file()
    assert all((target / RESTORE_MARKER).is_file() for target in targets)
    assert backup_store.list_backups(root) == []


def test_marker_clear_failure_reestablishes_fence_after_full_copy(layout, monkeypatch):
    config, root = layout
    selected, _ = saved(layout)
    change_data(config)
    original = backup_store._fsync_directory
    failed = False

    def interrupted(path):
        nonlocal failed
        if (
            not failed
            and path == config.governance_dir
            and not (path / RESTORE_MARKER).exists()
            and (config.data_dir / RESTORE_MARKER).exists() is False
            and row(config.data_dir / "one-memory.sqlite3") == "Synthetic core row"
        ):
            failed = True
            raise OSError("Synthetic directory sync failure")
        original(path)

    monkeypatch.setattr(backup_store, "_fsync_directory", interrupted)
    with pytest.raises(RestoreError, match="backup_restore_incomplete"):
        restore_backup(config, selected, root)
    assert failed
    assert all((path / RESTORE_MARKER).is_file() for path in roots(config))


def test_post_install_corruption_keeps_fence(layout, monkeypatch):
    config, root = layout
    selected, _ = saved(layout)
    original = backup_restore._replace_contents

    def corrupt(source, target):
        original(source, target)
        if target == config.data_dir:
            private_file(target / "unexpected.bin", b"Synthetic unexpected file")

    monkeypatch.setattr(backup_restore, "_replace_contents", corrupt)
    with pytest.raises(RestoreError, match="backup_restore_incomplete"):
        restore_backup(config, selected, root)
    assert all((path / RESTORE_MARKER).is_file() for path in roots(config))


def test_real_hub_startup_replays_newer_forget_and_keeps_rotated_credential(tmp_path, monkeypatch):
    """Real Core/SDK files and HTTP; only model output is synthetic."""
    import asyncio

    from chromadb.api.client import SharedSystemClient
    from test_chat_graph import produce
    from test_governance_execution import EXECUTE, preview, read
    from test_original_archive import config_for
    from test_output_lineage import fixture

    config = config_for(tmp_path)
    private_file(config.config_dir / "admin-token", config.admin_token.encode() + b"\n")
    private_file(config.config_dir / "encryption-key", config.encryption_key + b"\n")
    root = tmp_path / "versions"

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, headers, event, _ = await produce(app, web, config, monkeypatch)
            scope = event["scope_id"]
            record = "cmhe:" + jobs[0]
            memory = results[0]["memory_ids"][0]
        manifest = backup_store.create_backup(config, root, revision="synthetic-http-old")
        async with fixture(config) as (app, web):
            admin = {"Authorization": "Bearer " + config.admin_token}
            view = await preview(web, admin, scope, [record])
            response = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "forget",
                    "record_ids": [record],
                    "expected_view": view["view_digest"],
                    "operation_id": str(uuid4()),
                },
            )
            assert response.status_code == 200
            authority = access(config)
            revision = next(
                item["revision"]
                for item in authority.list_devices()
                if item["device_id"] == event["device_id"]
            )
            issued = authority.rotate(
                event["device_id"], RevisionRequest(expected_revision=revision)
            )
        restored = restore_backup(config, root / manifest["backup_id"], root)
        assert restored["status"] == "files_restored_pending_startup"
        SharedSystemClient.clear_system_cache()
        async with fixture(config) as (app, web):
            new_headers = {"Authorization": "Bearer " + issued.credential.get_secret_value()}
            assert (await read(web, headers, scope, "memories", memory)).status_code == 401
            assert (await read(web, new_headers, scope, "memories", memory)).status_code == 404
            app.core.state.cmh_governance.require_writes_ready()

    asyncio.run(scenario())
