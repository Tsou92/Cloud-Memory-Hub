"""Bounded, unverified manifest summaries for the online UI (ADR-0063)."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from cloud_memory_hub import backup_store as store

MAX_CANDIDATES = 365
MAX_TOTAL_BYTES = 64 * 1024 * 1024
READ_BYTES = 1024 * 1024
DEADLINE_SECONDS = 3
_FIELDS = {
    "schema_version",
    "kind",
    "backup_id",
    "created_at",
    "revision",
    "versions",
    "instance_id",
    "ledger_id",
    "admissions_ledger_id",
    "checkpoints",
    "store_versions",
    "directories",
    "files",
    "file_count",
    "total_bytes",
}


class BackupCatalogError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _invalid(*_args):
    raise BackupCatalogError("backup_catalog_invalid")


class _Budget:
    def __init__(self):
        self.deadline = time.monotonic() + DEADLINE_SECONDS
        self.bytes_read = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BackupCatalogError("backup_catalog_timeout")

    def read(self, path):
        self.check()
        store._plain_path(path, directory=False, private=True)
        before = path.lstat()
        if (
            before.st_size > store.MAX_MANIFEST_BYTES
            or self.bytes_read + before.st_size > MAX_TOTAL_BYTES
        ):
            raise BackupCatalogError("backup_catalog_limit")
        with store._read(path, limit=store.MAX_MANIFEST_BYTES) as stream:
            if store._signature(os.fstat(stream.fileno())) != store._signature(before):
                _invalid()
            chunks, remaining = [], before.st_size
            while remaining:
                self.check()
                chunk = stream.read(min(READ_BYTES, remaining))
                self.check()
                if not chunk:
                    _invalid()
                self.bytes_read += len(chunk)
                remaining -= len(chunk)
                chunks.append(chunk)
            if store._signature(os.fstat(stream.fileno())) != store._signature(before):
                _invalid()
        if store._signature(path.lstat()) != store._signature(before):
            _invalid()
        self.check()
        value = json.loads(
            b"".join(chunks), object_pairs_hook=store._unique_object, parse_constant=_invalid
        )
        self.check()
        return value


def _summary(manifest, expected_id):
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _FIELDS
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["kind"] != store.KIND
        or manifest["backup_id"] != expected_id
        or not store._ID.fullmatch(expected_id)
        or not isinstance(manifest["created_at"], str)
        or not isinstance(manifest["revision"], str)
        or not store._REVISION.fullmatch(manifest["revision"])
        or type(manifest["file_count"]) is not int
        or not 0 <= manifest["file_count"] <= store.MAX_FILES
        or type(manifest["total_bytes"]) is not int
        or manifest["total_bytes"] < 0
        or not isinstance(manifest["files"], list)
        or len(manifest["files"]) != manifest["file_count"]
        or not isinstance(manifest["directories"], list)
    ):
        _invalid()
    created = datetime.fromisoformat(manifest["created_at"])
    if (
        created.tzinfo is None
        or created.utcoffset().total_seconds() != 0
        or not manifest["created_at"].endswith("Z")
        or not expected_id.startswith(f"cmh-backup-{created.strftime('%Y%m%dT%H%M%S%fZ')}-")
    ):
        _invalid()
    for key in ("instance_id", "ledger_id", "admissions_ledger_id"):
        value = manifest[key]
        if not isinstance(value, str) or str(store.UUID(value, version=4)) != value:
            _invalid()
    versions = manifest["versions"]
    if (
        not isinstance(versions, dict)
        or set(versions) != {"cloud-memory-hub", "one-memory", "hy-memory"}
        or any(
            not isinstance(value, str) or not value or len(value) > 64
            for value in versions.values()
        )
        or versions["hy-memory"] != store.HY_MEMORY_VERSION
        or not isinstance(manifest["checkpoints"], dict)
        or not isinstance(manifest["store_versions"], dict)
        or set(manifest["store_versions"]) != {"access", "governance", "admissions", "execution"}
        or any(type(value) is not int or value < 1 for value in manifest["store_versions"].values())
    ):
        _invalid()
    return {**store._summary(manifest), "state": "unverified"}


def list_catalog(root: Path):
    """No payload traversal or recovery verification; errors never look like empty lists."""
    budget = _Budget()
    try:
        if not root.exists() and not root.is_symlink():
            budget.check()
            return []
        root = store._root(root)
        budget.check()
        with store.StorageLease(root):
            budget.check()
            candidates = []
            with os.scandir(root) as entries:
                for entry in entries:
                    budget.check()
                    if store._ID.fullmatch(entry.name):
                        candidates.append(Path(entry.path))
                        if len(candidates) > MAX_CANDIDATES:
                            raise BackupCatalogError("backup_catalog_limit")
            summaries = []
            for path in sorted(candidates, reverse=True):
                budget.check()
                store._plain_path(path, directory=True, private=True)
                summaries.append(_summary(budget.read(path / store.MANIFEST), path.name))
                budget.check()
            return summaries
    except store.StorageLeaseError:
        raise BackupCatalogError("backup_storage_in_use") from None
    except BackupCatalogError:
        raise
    except Exception:
        raise BackupCatalogError("backup_catalog_invalid") from None
