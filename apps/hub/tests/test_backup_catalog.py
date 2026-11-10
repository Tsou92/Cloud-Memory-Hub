"""Online metadata budgets do not replace offline complete inspection (ADR-0063)."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_backup_store
from cloud_memory_hub import backup_catalog as catalog
from cloud_memory_hub import backup_store as store
from cloud_memory_hub.storage_lease import StorageLease

layout = test_backup_store.layout


@pytest.fixture
def version(layout):
    config, root = layout
    manifest = store.create_backup(config, root, revision="synthetic-catalog")
    return root, root / manifest["backup_id"], manifest


def rewrite(path, manifest):
    (path / store.MANIFEST).write_text(json.dumps(manifest))


def test_reads_only_manifests_even_with_large_payload_and_keeps_complete_inspection(
    version, monkeypatch
):
    root, path, manifest = version
    payload = path / "data/large-unverified.bin"
    with payload.open("wb") as stream:
        stream.truncate(1024 * 1024 * 1024)
    payload.chmod(0o600)
    actual_read = store._read
    observed = []

    def only_manifest(target, **kwargs):
        assert target == path / store.MANIFEST
        observed.append(target)
        return actual_read(target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_read", only_manifest)
        patch.setattr(store, "_hash", lambda *_: pytest.fail("catalog must not hash payloads"))
        result = catalog.list_catalog(root)
    assert result == [{**store._summary(manifest), "state": "unverified"}]
    assert len(observed) == 1
    assert "files" not in result[0]
    assert str(root) not in json.dumps(result)
    with pytest.raises(store.BackupError):
        store.inspect_backup(path)
    with StorageLease(root):
        pass


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"kind": "unknown"},
        {"backup_id": "/synthetic/private"},
        {"created_at": "2026-11-10"},
        {"created_at": "2026-11-10T00:00:00+05:00"},
        {"revision": "secret/path"},
        {"file_count": True},
        {"file_count": -1},
        {"total_bytes": -1},
        {"total_bytes": True},
        {"unknown": "synthetic secret"},
        {"files": {}},
        {"instance_id": "unknown"},
        {"versions": {}},
        {"store_versions": {}},
    ],
)
def test_unknown_or_invalid_manifest_metadata_is_rejected_without_content(version, change):
    root, path, manifest = version
    rewrite(path, {**manifest, **change})
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_invalid$"):
        catalog.list_catalog(root)
    with StorageLease(root):
        pass


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON secret",
        b"[]",
        b"null",
        b'{"schema_version":1,"schema_version":1}',
        b'{"unknown":NaN}',
    ],
)
def test_bad_json_and_duplicate_keys_reject_the_whole_catalog(version, raw):
    root, path, _ = version
    (path / store.MANIFEST).write_bytes(raw)
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_invalid$"):
        catalog.list_catalog(root)


@pytest.mark.parametrize("limit", ["candidate", "total", "single"])
def test_each_budget_refuses_the_whole_result_and_releases_lease(version, monkeypatch, limit):
    root, path, _ = version
    if limit == "candidate":
        monkeypatch.setattr(catalog, "MAX_CANDIDATES", 1)
        (root / (path.name.rsplit("-", 1)[0] + "-" + uuid4().hex)).mkdir(mode=0o700)
    elif limit == "total":
        monkeypatch.setattr(catalog, "MAX_TOTAL_BYTES", (path / store.MANIFEST).stat().st_size - 1)
    else:
        monkeypatch.setattr(store, "MAX_MANIFEST_BYTES", (path / store.MANIFEST).stat().st_size - 1)
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_limit$"):
        catalog.list_catalog(root)
    with StorageLease(root):
        pass


def test_deadline_checks_every_mib_and_releases_lease_before_next_chunk(version, monkeypatch):
    root, path, manifest = version
    (path / store.MANIFEST).write_bytes(
        b" " * (catalog.READ_BYTES * 2) + json.dumps(manifest).encode()
    )
    elapsed, read_sizes = [0.0], []
    monkeypatch.setattr(catalog, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    actual_read = store._read

    @contextmanager
    def slow_read(target, **kwargs):
        with actual_read(target, **kwargs) as stream:

            def read(size):
                value = stream.read(size)
                read_sizes.append(len(value))
                elapsed[0] += catalog.DEADLINE_SECONDS + 0.01
                return value

            yield SimpleNamespace(read=read, fileno=stream.fileno)

    monkeypatch.setattr(store, "_read", slow_read)
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_timeout$"):
        catalog.list_catalog(root)
    assert read_sizes == [1024 * 1024]
    with StorageLease(root):
        pass


def test_total_budget_counts_prior_manifests_and_never_returns_a_partial_list(version, monkeypatch):
    root, path, manifest = version
    second = root / (path.name.rsplit("-", 1)[0] + "-" + uuid4().hex)
    second.mkdir(mode=0o700)
    rewrite(second, {**manifest, "backup_id": second.name})
    (second / store.MANIFEST).chmod(0o600)
    total = sum((folder / store.MANIFEST).stat().st_size for folder in (path, second))
    monkeypatch.setattr(catalog, "MAX_TOTAL_BYTES", total - 1)
    assert all((folder / store.MANIFEST).stat().st_size < total - 1 for folder in (path, second))
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_limit$"):
        catalog.list_catalog(root)
    with StorageLease(root):
        pass


def test_deadline_is_checked_after_bounded_json_parse(version, monkeypatch):
    root, _, _ = version
    elapsed = [0.0]
    monkeypatch.setattr(catalog, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    actual_loads = json.loads

    def delayed(*args, **kwargs):
        result = actual_loads(*args, **kwargs)
        elapsed[0] += catalog.DEADLINE_SECONDS + 0.01
        return result

    monkeypatch.setattr(catalog, "json", SimpleNamespace(loads=delayed))
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_timeout$"):
        catalog.list_catalog(root)
    with StorageLease(root):
        pass


def test_real_limits_and_missing_directory_are_explicit(tmp_path):
    assert catalog.MAX_CANDIDATES == 365
    assert catalog.MAX_TOTAL_BYTES == 64 * 1024 * 1024
    assert catalog.DEADLINE_SECONDS == 3
    assert catalog.list_catalog(tmp_path / "absent") == []


def test_symlink_manifest_and_busy_storage_are_rejected(version):
    root, path, _ = version
    with StorageLease(root):
        with pytest.raises(catalog.BackupCatalogError, match="^backup_storage_in_use$"):
            catalog.list_catalog(root)
    target = path / store.MANIFEST
    target.rename(path / "original.json")
    target.symlink_to(path / "original.json")
    with pytest.raises(catalog.BackupCatalogError, match="^backup_catalog_invalid$"):
        catalog.list_catalog(root)
