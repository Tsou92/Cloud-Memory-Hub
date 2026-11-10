"""Real maintenance CLI subprocesses: private archives, cold import and history restore."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest
from cloud_memory_hub.backup_store import create_backup, inspect_backup
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    NamedResource,
    RevisionRequest,
    ScopeGrant,
)
from cloud_memory_hub.remote_ingest import RemoteIngest, SourceRegistration
from cloud_memory_hub.secret_store import EncryptedSecretStore
from cloud_memory_hub.storage_lease import RESTORE_MARKER
from test_backup_store import layout as backup_layout
from test_backup_store import private_file, row


@pytest.fixture
def layout(tmp_path):
    return backup_layout.__wrapped__(tmp_path)


def invoke(layout, *arguments):
    config, root = layout
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cloud_memory_hub.backup_maintenance",
            *arguments,
            "--backup-dir",
            str(root),
        ],
        env={
            **os.environ,
            "CMH_DATA_DIR": str(config.data_dir),
            "CMH_CONFIG_DIR": str(config.config_dir),
            "CMH_GOVERNANCE_DIR": str(config.governance_dir),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert config.admin_token not in result.stdout + result.stderr
    assert config.encryption_key.decode() not in result.stdout + result.stderr
    return result


def test_cli_export_and_inspect_complete_archive_without_content(layout, tmp_path):
    config, root = layout
    manifest = create_backup(config, root, revision="synthetic-cli")
    exported = tmp_path / "exports"
    exported.mkdir(mode=0o700)
    archive = exported / "private.cmh.tar"
    result = invoke(layout, "export", "--version", manifest["backup_id"], "--output", str(archive))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["bytes"] == archive.stat().st_size
    inspected = invoke(layout, "inspect", "--archive", str(archive))
    assert inspected.returncode == 0, inspected.stderr
    value = json.loads(inspected.stdout)
    assert value == {
        "state": "verified",
        **{
            key: manifest[key]
            for key in (
                "backup_id",
                "created_at",
                "revision",
                "file_count",
                "total_bytes",
            )
        },
    }
    assert "files" not in value and "Synthetic core row" not in inspected.stdout


@pytest.mark.parametrize(
    "command,flags,code",
    [
        ("restore", [], "backup_writers_stopped_required"),
        ("import", [], "backup_writers_stopped_required"),
        ("import", ["--writers-stopped"], "backup_source_stopped_required"),
    ],
)
def test_offline_requirements_checked_before_opening_source(layout, command, flags, code):
    result = invoke(layout, command, "--archive", "/missing-private-file", *flags)
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"error": code}
    assert "/missing-private-file" not in result.stdout + result.stderr


def test_version_argument_cannot_escape_backup_root(layout):
    result = invoke(layout, "inspect", "--version", "../../outside")
    assert result.returncode == 2
    assert json.loads(result.stderr) == {"error": "backup_id_invalid"}


def provision(config):
    access = DeviceAccessStore(config.config_dir / "remote-access.sqlite3", require_existing=True)
    project = access.create_project(NamedResource(display_name="Synthetic CLI project"))
    issued = access.create_device(NamedResource(display_name="Synthetic CLI device"))
    access.replace_grants(
        issued.device["device_id"],
        GrantRequest(
            expected_revision=1,
            grants=(
                ScopeGrant(
                    scope_id=project["project_id"], permissions=("events:write", "memory:read")
                ),
            ),
        ),
    )
    ingest = RemoteIngest(access, lambda: None)
    source = ingest.create_source(
        SourceRegistration(
            device_id=issued.device["device_id"],
            scope_id=project["project_id"],
            display_name="Synthetic CLI source",
            adapter_id="cloud-memory-hub.agent",
        )
    )
    return access, issued, ingest, source


def test_cli_archive_import_into_empty_roots_preserves_payload_identity_and_credentials(
    layout, tmp_path
):
    config, root = layout
    access, issued, ingest, source = provision(config)
    manifest = create_backup(config, root, revision="synthetic-cli-import")
    exported = tmp_path / "exports"
    exported.mkdir(mode=0o700)
    archive = exported / "private.cmh.tar"
    result = invoke(layout, "export", "--version", manifest["backup_id"], "--output", str(archive))
    assert result.returncode == 0, result.stderr
    imported_config = replace(
        config,
        data_dir=tmp_path / "import-data",
        config_dir=tmp_path / "import-config",
        governance_dir=tmp_path / "import-governance",
    )
    result = invoke(
        (imported_config, tmp_path / "import-backups"),
        "import",
        "--archive",
        str(archive),
        "--writers-stopped",
        "--source-stopped",
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["status"] == "files_restored_pending_startup"
    assert value["instance_id"] == access.instance_id == manifest["instance_id"]
    assert value["backup_id"] == manifest["backup_id"]
    assert value["protective_backup_id"] is None
    assert row(imported_config.data_dir / "one-memory.sqlite3") == "Synthetic core row"
    assert (
        row(imported_config.data_dir / "hy-memory/governed-v2/cache.sqlite3")
        == "Synthetic SDK outcome"
    )
    imported = DeviceAccessStore(
        imported_config.config_dir / "remote-access.sqlite3", require_existing=True
    )
    assert imported.instance_id == access.instance_id
    credential = issued.credential.get_secret_value()
    assert imported.authenticate(credential) == access.authenticate(credential)
    assert RemoteIngest(imported, lambda: None).list_sources() == ingest.list_sources() == [source]
    for name in ("admin-token", "encryption-key", "synthetic-probe.private.json"):
        assert (imported_config.config_dir / name).read_bytes() == (
            config.config_dir / name
        ).read_bytes()
    secrets = EncryptedSecretStore(
        imported_config.config_dir / "model-credentials.sqlite3", config.encryption_key
    )
    assert secrets.resolve("synthetic-model-profile") == "synthetic-model-credential"
    assert all(
        not (path / RESTORE_MARKER).exists()
        for path in (
            imported_config.data_dir,
            imported_config.config_dir,
            imported_config.governance_dir,
        )
    )
    assert credential not in result.stdout + result.stderr
    assert "Synthetic core row" not in result.stdout + result.stderr


def test_cli_history_restore_preserves_current_security_and_creates_protective_version(layout):
    config, root = layout
    access, issued, ingest, source = provision(config)
    revoked = access.create_device(NamedResource(display_name="Synthetic revoked CLI device"))
    manifest = create_backup(config, root, revision="synthetic-cli-history")
    with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
        db.execute("UPDATE synthetic SET value='Synthetic current CLI payload'")
    private_file(
        config.config_dir / "synthetic-probe.private.json",
        b'{"purpose":"synthetic current CLI config"}',
    )
    current_admin = "n" * 48
    private_file(config.config_dir / "admin-token", current_admin.encode() + b"\n")
    config = replace(config, admin_token=current_admin)
    rotated = access.rotate(issued.device["device_id"], RevisionRequest(expected_revision=2))
    access.replace_grants(
        issued.device["device_id"],
        GrantRequest(
            expected_revision=3,
            grants=(ScopeGrant(scope_id=source["scope_id"], permissions=("history:read",)),),
        ),
    )
    access.revoke(revoked.device["device_id"], RevisionRequest(expected_revision=1))
    ingest.revoke_source(source["source_id"], RevisionRequest(expected_revision=1))
    access.create_project(NamedResource(display_name="Synthetic current CLI project"))
    projects, devices, sources = (
        access.list_projects(),
        access.list_devices(),
        ingest.list_sources(),
    )
    result = invoke(
        (config, root), "restore", "--version", manifest["backup_id"], "--writers-stopped"
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["status"] == "files_restored_pending_startup"
    assert value["instance_id"] == manifest["instance_id"]
    assert value["protective_backup_id"] != manifest["backup_id"]
    protective = root / value["protective_backup_id"]
    assert inspect_backup(protective)["instance_id"] == manifest["instance_id"]
    assert row(protective / "data/one-memory.sqlite3") == "Synthetic current CLI payload"
    assert row(config.data_dir / "one-memory.sqlite3") == "Synthetic core row"
    assert (config.config_dir / "synthetic-probe.private.json").read_bytes() == (
        root / manifest["backup_id"] / "config/synthetic-probe.private.json"
    ).read_bytes()
    assert access.list_projects() == projects
    assert access.list_devices() == devices
    assert ingest.list_sources() == sources
    with pytest.raises(AccessError):
        access.authenticate(issued.credential.get_secret_value())
    with pytest.raises(AccessError):
        access.authenticate(revoked.credential.get_secret_value())
    assert access.authenticate(rotated.credential.get_secret_value())["grants"] == [
        {"scope_id": source["scope_id"], "permissions": ["history:read"]},
    ]
    assert (config.config_dir / "admin-token").read_bytes() == current_admin.encode() + b"\n"
    assert (config.config_dir / "encryption-key").read_bytes().strip() == config.encryption_key
    assert all(
        not (path / RESTORE_MARKER).exists()
        for path in (
            config.data_dir,
            config.config_dir,
            config.governance_dir,
        )
    )
    assert rotated.credential.get_secret_value() not in result.stdout + result.stderr
    assert "Synthetic current CLI payload" not in result.stdout + result.stderr
