"""ADR-0063 cold backup boundaries, using synthetic files and real SQLite/SDK snapshots."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from cloud_memory_hub import backup_store
from cloud_memory_hub.__main__ import initialize
from cloud_memory_hub.backup_store import (
    BackupError,
    create_backup,
    inspect_backup,
    list_backups,
    prune_backups,
)
from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.remote_access import DeviceAccessStore, NamedResource
from cloud_memory_hub.secret_store import EncryptedSecretStore
from cloud_memory_hub.storage_lease import LOCK_NAME, StorageLease


def private_file(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(0o600)


def database(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE synthetic (value TEXT)")
        connection.execute("INSERT INTO synthetic VALUES (?)", (value,))
    path.chmod(0o600)


@pytest.fixture
def layout(tmp_path):
    data, config, governance = (tmp_path / name for name in ("data", "config", "governance"))
    data.mkdir(mode=0o700)
    initialize(config, governance_dir=governance)
    hub = HubConfig(
        data,
        config,
        "http://localhost:8800",
        read_private_file(config / "admin-token").decode().strip(),
        read_private_file(config / "encryption-key").strip(),
        governance_dir=governance,
    )
    database(data / "one-memory.sqlite3", "Synthetic core row")
    database(data / "hy-memory/governed-v2/cache.sqlite3", "Synthetic SDK outcome")
    private_file(data / "hy-memory/governed-v2/vector/segment.bin", b"Synthetic vector segment")
    private_file(config / "synthetic-probe.private.json", b'{"purpose":"synthetic backup fixture"}')
    credentials = EncryptedSecretStore(config / "model-credentials.sqlite3", hub.encryption_key)
    credentials.put("synthetic-model-profile", "synthetic-model-credential")
    device = DeviceAccessStore(config / "remote-access.sqlite3", require_existing=True)
    device.create_device(NamedResource(display_name="Synthetic backup device"))
    return hub, tmp_path / "versions"


def create(layout):
    config, root = layout
    manifest = create_backup(config, root, revision="synthetic-revision-1")
    return root / manifest["backup_id"], manifest


def row(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM synthetic").fetchone()[0]


def test_complete_snapshot_reuses_core_and_sdk_and_preserves_private_config(layout, monkeypatch):
    config, root = layout
    snapshot, database_backup = (
        backup_store.snapshot_local_data_plane,
        backup_store.MainDatabase.back_up,
    )
    calls = []

    def sdk(**kwargs):
        calls.append("sdk")
        return snapshot(**kwargs)

    def sql(self, target):
        calls.append("sqlite")
        return database_backup(self, target)

    monkeypatch.setattr(backup_store, "snapshot_local_data_plane", sdk)
    monkeypatch.setattr(backup_store.MainDatabase, "back_up", sql)
    path, manifest = create(layout)
    assert inspect_backup(path) == manifest
    assert calls.count("sdk") == 1
    assert calls.count("sqlite") >= 5
    assert row(path / "data/one-memory.sqlite3") == "Synthetic core row"
    assert row(path / "data/hy-memory/governed-v2/cache.sqlite3") == "Synthetic SDK outcome"
    for name in ("admin-token", "encryption-key", "synthetic-probe.private.json"):
        assert (path / "config" / name).read_bytes() == (config.config_dir / name).read_bytes()
    assert list_backups(root) == [
        {
            "state": "complete",
            **{
                key: manifest[key]
                for key in ("backup_id", "created_at", "revision", "file_count", "total_bytes")
            },
        }
    ]
    assert "files" not in list_backups(root)[0]
    assert all(p.stat().st_mode & 0o077 == 0 for p in path.rglob("*"))
    assert str(config.data_dir) not in json.dumps(manifest)
    assert config.admin_token not in json.dumps(manifest)
    assert config.encryption_key.decode() not in json.dumps(manifest)
    assert not any(p.name.endswith(("-wal", "-shm")) for p in path.rglob("*"))


def test_cold_snapshot_preserves_committed_wal_without_touching_original(layout):
    config, _ = layout
    path = config.data_dir / "one-memory.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute("UPDATE synthetic SET value='Synthetic committed WAL row'")
connection.commit()
os._exit(0)
""",
            str(path),
        ],
        check=True,
    )
    wal = path.with_name(path.name + "-wal")
    before = wal.read_bytes()
    saved, _ = create(layout)
    assert row(saved / "data/one-memory.sqlite3") == "Synthetic committed WAL row"
    assert wal.read_bytes() == before
    assert not (saved / "data/one-memory.sqlite3-wal").exists()


def test_exclusions_do_not_drop_sdk_cache_or_other_private_files(layout):
    config, _ = layout
    for name in (
        "runtime-token",
        "runtime.json",
        "core.lock",
        "backups/old.bin",
        "__pycache__/module.pyc",
    ):
        private_file(config.data_dir / name, b"Synthetic excluded runtime file")
    path, manifest = create(layout)
    names = {row["path"] for row in manifest["files"]}
    assert "data/hy-memory/governed-v2/cache.sqlite3" in names
    assert "config/synthetic-probe.private.json" in names
    assert not any(name.endswith(LOCK_NAME) for name in names)
    assert not (path / "data/backups").exists()
    assert not (path / "data/runtime-token").exists()


@pytest.mark.parametrize(
    "damage", ["symlink", "hardlink", "fifo", "directory_link", "excluded_link"]
)
def test_unsafe_source_is_rejected_before_sdk_or_copy(layout, tmp_path, monkeypatch, damage):
    config, root = layout
    other = tmp_path / "unrelated"
    private_file(other, b"Synthetic unrelated content")
    target = config.data_dir / "unsafe"
    if damage in {"symlink", "excluded_link"}:
        if damage == "excluded_link":
            target = config.data_dir / "backups"
        target.symlink_to(other)
    elif damage == "hardlink":
        os.link(other, target)
    elif damage == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(
        backup_store, "_local_sdk", lambda _root: pytest.fail("unsafe input reached SDK probe")
    )
    with pytest.raises(BackupError, match="^backup_unsafe_path$"):
        create(layout)
    assert list_backups(root) == []
    assert other.read_bytes() == b"Synthetic unrelated content"


def test_live_runtime_lease_blocks_backup_without_publishing(layout):
    config, root = layout
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        with pytest.raises(BackupError, match="^backup_storage_in_use$"):
            create(layout)
    assert list_backups(root) == []


def test_overlap_and_linked_destination_are_rejected(layout, tmp_path):
    config, root = layout
    nested = config.data_dir / "versions"
    with pytest.raises(BackupError, match="^backup_roots_overlap$"):
        create_backup(config, nested, revision="synthetic")
    assert not nested.exists()
    root.symlink_to(config.data_dir, target_is_directory=True)
    with pytest.raises(BackupError, match="^backup_unsafe_path$"):
        create(layout)


def test_remote_sdk_storage_is_rejected(layout, monkeypatch):
    builder = backup_store.build_hy_memory_config

    def remote(*args, **kwargs):
        config = builder(*args, **kwargs)
        config.vector_store.host = "synthetic.example.invalid"
        return config

    monkeypatch.setattr(backup_store, "build_hy_memory_config", remote)
    with pytest.raises(BackupError, match="^backup_sdk_unsupported$"):
        create(layout)
    assert list_backups(layout[1]) == []


@pytest.mark.parametrize("damage", ["corrupt_core", "wrong_key"])
def test_invalid_source_database_or_key_never_publishes(layout, damage):
    config, root = layout
    if damage == "corrupt_core":
        private_file(config.data_dir / "one-memory.sqlite3", b"Synthetic invalid database")
    else:
        from cryptography.fernet import Fernet

        config = replace(config, encryption_key=Fernet.generate_key())
    with pytest.raises(BackupError):
        create_backup(config, root, revision="synthetic")
    assert list_backups(root) == []


@pytest.mark.parametrize("failure", ["copy", "publish", "published_sync"])
def test_failure_preserves_prior_version_and_does_not_offer_false_success(
    layout, monkeypatch, failure
):
    old_path, old_manifest = create(layout)
    if failure == "copy":

        def fail_copy(*_args):
            raise OSError("Synthetic no space")

        monkeypatch.setattr(backup_store, "_copy", fail_copy)
    elif failure == "publish":
        rename = os.rename

        def fail_publish(source, target):
            if Path(source).name.endswith(".partial"):
                raise OSError("Synthetic rename failure")
            return rename(source, target)

        monkeypatch.setattr(backup_store.os, "rename", fail_publish)
    else:
        sync = backup_store._fsync_directory

        def fail_sync(path):
            if path == layout[1]:
                raise OSError("Synthetic directory sync failure")
            return sync(path)

        monkeypatch.setattr(backup_store, "_fsync_directory", fail_sync)
    with pytest.raises(BackupError, match="^backup_create_failed$"):
        create(layout)
    assert inspect_backup(old_path) == old_manifest
    assert [row["backup_id"] for row in list_backups(layout[1])] == [old_manifest["backup_id"]]


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "extra",
        "extra_dir",
        "digest",
        "symlink",
        "hardlink",
        "fifo",
        "duplicate",
        "traversal",
        "unknown_version",
        "identity",
    ],
)
def test_inspection_rejects_damaged_inventory_and_never_exposes_file_contents(layout, damage):
    path, manifest = create(layout)
    file = path / "config/synthetic-probe.private.json"
    if damage == "missing":
        file.unlink()
    elif damage == "extra":
        private_file(path / "data/extra.bin", b"Synthetic extra")
    elif damage == "extra_dir":
        (path / "data/extra").mkdir(mode=0o700)
    elif damage == "digest":
        file.write_bytes(b"Synthetic modified content")
    elif damage == "symlink":
        file.unlink()
        file.symlink_to(layout[0].config_dir / "synthetic-probe.private.json")
    elif damage == "hardlink":
        os.link(file, path / "config/linked")
    elif damage == "fifo":
        os.mkfifo(path / "config/pipe", 0o600)
    else:
        if damage == "duplicate":
            manifest["files"].append(manifest["files"][0])
            manifest["file_count"] += 1
        elif damage == "traversal":
            manifest["files"][0]["path"] = "../outside"
        elif damage == "unknown_version":
            manifest["schema_version"] = 999
        else:
            manifest["instance_id"] = "00000000-0000-4000-8000-000000000001"
        (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError) as result:
        inspect_backup(path)
    assert str(result.value).startswith("backup_")
    assert "Synthetic" not in str(result.value)
    assert list_backups(layout[1])[0]["state"] == "invalid"


def test_list_and_prune_touch_only_valid_complete_owned_versions(layout):
    root = layout[1]
    first, old = create(layout)
    invalid, _ = create(layout)
    (invalid / "config/admin-token").unlink()
    latest, current = create(layout)
    unrelated = root / "manual-export"
    unrelated.mkdir(mode=0o700)
    private_file(unrelated / "retain.bin", b"Synthetic unrelated export")
    partial = root / ".incomplete.partial"
    partial.mkdir(mode=0o700)
    private_file(partial / "retain.bin", b"Synthetic incomplete artifact")
    result = prune_backups(root, keep=1)
    assert result == {"removed": [old["backup_id"]], "kept": 1, "invalid": 1, "failures": []}
    assert not first.exists()
    assert inspect_backup(latest) == current
    assert invalid.exists() and unrelated.exists() and partial.exists()
    with pytest.raises(BackupError, match="^backup_retention_invalid$"):
        prune_backups(root, keep=0)
    with pytest.raises(BackupError, match="^backup_retention_invalid$"):
        prune_backups(root, keep=True)


def test_inspection_is_read_only_and_listing_waits_for_publication_lease(layout):
    path, manifest = create(layout)
    before = {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    assert inspect_backup(path) == manifest
    after = {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    assert before == after
    with StorageLease(layout[1]):
        with pytest.raises(BackupError, match="^backup_storage_in_use$"):
            list_backups(layout[1])


@pytest.mark.parametrize("failure", ["changed", "interrupted"])
def test_source_change_or_interruption_does_not_publish(layout, monkeypatch, failure):
    original = backup_store._copy
    config, root = layout

    def copy(source, target):
        if failure == "interrupted":
            raise KeyboardInterrupt()
        original(source, target)
        if source.name == "synthetic-probe.private.json":
            source.write_bytes(b"Synthetic concurrent mutation")

    monkeypatch.setattr(backup_store, "_copy", copy)
    with pytest.raises(KeyboardInterrupt if failure == "interrupted" else BackupError):
        create(layout)
    assert list_backups(root) == []
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        pass


def test_prune_reports_failure_and_retains_newest_version(layout, monkeypatch):
    old, _ = create(layout)
    newest, manifest = create(layout)
    rename = backup_store.os.rename

    def fail(source, target):
        if source == old:
            raise OSError("Synthetic retention failure")
        return rename(source, target)

    monkeypatch.setattr(backup_store.os, "rename", fail)
    result = prune_backups(layout[1], keep=1)
    assert result["removed"] == []
    assert result["failures"] == [{"backup_id": old.name, "error_code": "backup_prune_failed"}]
    assert inspect_backup(newest) == manifest
    assert old.exists()


def test_real_supervisor_manual_request_creates_a_verified_backup_then_restarts(layout, tmp_path):
    from cloud_memory_hub import backup_runtime as runtime
    from test_backup_runtime import wait_for, worker

    config, root = layout
    command, log = worker(tmp_path)
    stopped, failures = threading.Event(), []

    def supervise():
        try:
            runtime.supervise(
                config, root, child_command=command, stop_event=stopped, poll_seconds=0.02
            )
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=supervise)
    thread.start()
    try:
        wait_for(lambda: failures or log.exists() and log.read_text().count("start") == 1)
        assert not failures
        runtime.request_backup(root)
        wait_for(lambda: failures or log.read_text().count("start") == 2)
        assert not failures
        execution = runtime.status(root)["execution"]
        assert execution["state"] == "completed"
        assert execution["retention_error"] is None
        manifest = inspect_backup(root / execution["last_backup_id"])
        assert manifest["file_count"] == execution["last_file_count"]
        assert manifest["total_bytes"] == execution["last_total_bytes"]
        assert log.read_text().splitlines() == ["start", "stop", "start"]
        assert len(list_backups(root)) == 1
    finally:
        stopped.set()
        thread.join(10)
        assert not thread.is_alive()
        assert not failures
