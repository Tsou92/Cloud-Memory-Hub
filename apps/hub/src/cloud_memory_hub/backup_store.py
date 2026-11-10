"""ADR-0063: private, offline three-volume backups over upstream snapshot primitives.

Only a verified directory is a version. This module never stops a runtime,
restores files, migrates a store, or retries an uncertain write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from hy_memory.snapshot import snapshot_local_data_plane
from one_memory.database_access import MainDatabase
from one_memory.hy_memory_bridge import build_hy_memory_config, builtin_retrieval_snapshot
from one_memory.hy_runtime_config import HY_MEMORY_NAMESPACE, HY_MEMORY_VERSION

from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_journal import Checkpoint
from cloud_memory_hub.storage_lease import (
    LOCK_NAME,
    StorageLease,
    StorageLeaseError,
    require_restore_complete,
)
from cloud_memory_hub.write_admissions import WriteAdmissions

KIND = "cloud-memory-hub-backup"
MANIFEST = "manifest.json"
MAX_FILES = 100_000
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_ID = re.compile(r"cmh-backup-\d{8}T\d{12}Z-[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_ROOTS = ("data", "config", "governance")
# Exact known product caches only: SDK cache.sqlite3 contains durable outcomes.
_EXCLUDED_DATA_DIRS = {"backups", "__pycache__", ".pytest_cache"}
_EXCLUDED_DATA_FILES = {"core.lock", "runtime.json", "runtime-token"}
_SQLITE_HEADER = b"SQLite format 3\x00"
_REQUIRED_FILES = {
    "data/one-memory.sqlite3",
    "config/remote-access.sqlite3",
    "config/admin-token",
    "config/encryption-key",
    "governance/journal.sqlite3",
    "governance/admissions.sqlite3",
}


class BackupError(RuntimeError):
    """Public fixed code; paths, credentials and source contents stay private."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _reject(code="backup_invalid"):
    raise BackupError(code)


def _plain_path(path: Path, *, directory: bool, private=False) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        _reject("backup_unsafe_path")
    for ancestor in reversed((path, *path.parents)):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode):
            _reject("backup_unsafe_path")
    info = path.lstat()
    if directory:
        valid = stat.S_ISDIR(info.st_mode)
    else:
        valid = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not valid or private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        _reject("backup_unsafe_path")
    return path


def _root(path: Path, *, create=False) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        _reject("backup_unsafe_path")
    if create:
        # Reject links in existing ancestors before creating any directories.
        for parent in reversed((path, *path.parents)):
            if parent.exists() or parent.is_symlink():
                _plain_path(parent, directory=True)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _plain_path(path, directory=True, private=True)


def _signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _relative(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _reject()
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in {".", ".."} for p in path.parts):
        _reject()
    return path


def _tree(root: Path):
    """Preflight every entry, including entries excluded from exported data."""
    _plain_path(root, directory=True)
    files, directories = {}, {}

    def unreadable(_error):
        _reject("backup_unreadable")

    for current, child_dirs, child_files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted(child_dirs + child_files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                directories[relative] = _signature(info)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                files[relative] = _signature(info)
            else:
                _reject("backup_unsafe_path")
            if len(files) + len(directories) > MAX_FILES:
                _reject("backup_inventory_limit")
    return files, directories


def _excluded(root_name, relative):
    parts = PurePosixPath(relative).parts
    return parts[-1] == LOCK_NAME or (
        root_name == "data"
        and (parts[0] in _EXCLUDED_DATA_DIRS or relative in _EXCLUDED_DATA_FILES)
    )


def _read(path: Path, *, limit: int | None = None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    stream = os.fdopen(descriptor, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or limit and info.st_size > limit:
        stream.close()
        _reject("backup_unsafe_path")
    return stream


def _hash(path: Path):
    digest = hashlib.sha256()
    with _read(path) as stream:
        before = _signature(os.fstat(stream.fileno()))
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        if _signature(os.fstat(stream.fileno())) != before:
            _reject("backup_changed")
    return digest.hexdigest()


def _copy(source: Path, target: Path):
    with _read(source) as incoming:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(incoming, output, 1024 * 1024)


def _local_sdk(backup_root):
    """Check the pinned bridge's actual storage policy without opening a client."""
    if version("hy-memory") != HY_MEMORY_VERSION:
        _reject("backup_sdk_unsupported")
    previous = os.environ.get("MEMORY_ENABLE_GRAPH")
    try:
        with tempfile.TemporaryDirectory(prefix=".sdk-check-", dir=backup_root) as temporary:
            root = Path(temporary)
            config = build_hy_memory_config(
                builtin_retrieval_snapshot(), api_key=None, data_dir=root
            )
            sdk_root = root / "hy-memory" / HY_MEMORY_NAMESPACE
            if (
                config.vector_store.provider != "chroma"
                or config.vector_store.host
                or config.vector_store.port
                or config.graph_store.provider != "kuzu"
                or config.graph_store.url
                or config.cache.backend != "sqlite"
            ):
                _reject("backup_sdk_unsupported")
            for value in (
                config.vector_store.persist_directory,
                config.graph_store.db_path,
                config.cache.db_path,
                config.history.db_path,
                config.coding.db_path,
            ):
                if not Path(value).is_relative_to(sdk_root):
                    _reject("backup_sdk_unsupported")
    finally:
        if previous is None:
            os.environ.pop("MEMORY_ENABLE_GRAPH", None)
        else:
            os.environ["MEMORY_ENABLE_GRAPH"] = previous


def _copy_volumes(config, stage):
    source_roots = dict(
        zip(_ROOTS, (config.data_dir, config.config_dir, config.governance_dir), strict=True)
    )
    before = {name: _tree(path) for name, path in source_roots.items()}
    sdk = f"hy-memory/{HY_MEMORY_NAMESPACE}"
    for name, root in source_roots.items():
        destination = stage / name
        destination.mkdir(mode=0o700)
        files, directories = before[name]
        for relative in sorted(directories, key=lambda path: (path.count("/"), path)):
            if (
                _excluded(name, relative)
                or name == "data"
                and (relative == sdk or relative.startswith(sdk + "/"))
            ):
                continue
            (destination / relative).mkdir(mode=0o700)
        for relative in sorted(files):
            if _excluded(name, relative) or name == "data" and relative.startswith(sdk + "/"):
                continue
            _copy(root / relative, destination / relative)
        if name == "data" and sdk in directories:
            snapshot_local_data_plane(
                source_root=root / sdk, target_root=destination / sdk, writers_stopped=True
            )
    if any(_tree(root) != before[name] for name, root in source_roots.items()):
        _reject("backup_changed")


def _normalize_databases(stage):
    """Use upstream SQLite backup on private copies, retaining committed WAL rows.

    Only staged databases change. Standalone DELETE-mode copies make inspection
    read-only and remove any need to restore process-specific WAL/SHM files.
    """
    files, _ = _tree(stage)
    if not _REQUIRED_FILES <= files.keys():
        _reject("backup_required_file_missing")
    for relative in sorted(files):
        path = stage / relative
        if not path.exists():  # A previously normalized database owned this sidecar.
            continue
        with _read(path) as stream:
            sqlite = stream.read(16) == _SQLITE_HEADER
        if not sqlite:
            if path.suffix in {".sqlite", ".sqlite3"}:
                _reject("backup_database_invalid")
            continue
        target = path.with_name(f".{path.name}.{uuid4().hex}.snapshot")
        database = MainDatabase(path)
        try:
            database.back_up(target)
        finally:
            database.close()
        with closing(sqlite3.connect(target)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                _reject("backup_database_invalid")
        os.replace(target, path)
        for suffix in ("-wal", "-shm", "-journal"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _authority_metadata(path):
    config = HubConfig(
        path / "data",
        path / "config",
        "http://localhost:8800",
        read_private_file(path / "config/admin-token").decode("ascii").strip(),
        read_private_file(path / "config/encryption-key").strip(),
        governance_dir=path / "governance",
    )
    authority = open_authority(config, read_only=True)
    authority.require_ready()
    admissions = WriteAdmissions(authority.access, authority.journal, read_only=True)
    admissions.verify_all()
    return {
        "instance_id": authority.access.instance_id,
        "ledger_id": authority.journal.ledger_id,
        "admissions_ledger_id": admissions.journal.ledger_id,
        "checkpoints": {
            key: value.model_dump() for key, value in sorted(authority.verify().items())
        },
        "store_versions": {
            "access": authority.access.schema_version,
            "governance": authority.journal.schema_version,
            "admissions": admissions.journal.schema_version,
            "execution": authority.execution.version,
        },
    }


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_sync(root):
    files, directories = _tree(root)
    for relative in files:
        path = root / relative
        path.chmod(0o600)
        with _read(path) as stream:
            os.fsync(stream.fileno())
    for relative in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        path = root / relative
        path.chmod(0o700)
        _fsync_directory(path)
    root.chmod(0o700)
    _fsync_directory(root)


def _manifest(stage, backup_id, created_at, revision):
    files, directories = _tree(stage)
    entries = [
        {"path": name, "bytes": (stage / name).stat().st_size, "sha256": _hash(stage / name)}
        for name in sorted(files)
    ]
    return {
        "schema_version": 1,
        "kind": KIND,
        "backup_id": backup_id,
        "created_at": created_at,
        "revision": revision,
        "versions": {
            name: version(name) for name in ("cloud-memory-hub", "one-memory", "hy-memory")
        },
        **_authority_metadata(stage),
        "directories": sorted(directories),
        "files": entries,
        "file_count": len(entries),
        "total_bytes": sum(row["bytes"] for row in entries),
    }


def create_backup(config: HubConfig, backup_root: Path, *, revision: str) -> dict:
    """Create one cold version; the caller must have already stopped serve."""
    stage = published = None
    try:
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            _reject("backup_revision_invalid")
        root = backup_root.absolute()
        roots = [
            config.data_dir.absolute(),
            config.config_dir.absolute(),
            config.governance_dir.absolute(),
            root,
        ]
        if any(
            a == b or a in b.parents or b in a.parents
            for i, a in enumerate(roots)
            for b in roots[i + 1 :]
        ):
            _reject("backup_roots_overlap")
        root = _root(root, create=True)
        with StorageLease(*roots):
            require_restore_complete(*roots[:3])
            # Check all sources before probing SDK configuration or copying bytes.
            for path in roots[:3]:
                _plain_path(path, directory=True, private=True)
                _tree(path)
            _local_sdk(root)
            now = datetime.now(UTC)
            backup_id = f"cmh-backup-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}"
            stage = root / f".{backup_id}.partial"
            stage.mkdir(mode=0o700)
            _copy_volumes(config, stage)
            _normalize_databases(stage)
            _private_sync(stage)
            if not hmac.compare_digest(
                read_private_file(stage / "config/encryption-key").strip(), config.encryption_key
            ):
                _reject("backup_identity_mismatch")
            manifest = _manifest(stage, backup_id, now.isoformat().replace("+00:00", "Z"), revision)
            encoded = (
                json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
            if len(encoded) > MAX_MANIFEST_BYTES:
                _reject("backup_inventory_limit")
            descriptor = os.open(stage / MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _inspect(stage, backup_id)
            _fsync_directory(stage)
            target = root / backup_id
            try:
                os.rename(stage, target)
                published = target
                _fsync_directory(root)
            except BaseException:
                # Keep the operation lease until an uncertain publication is hidden.
                if published is not None:
                    os.rename(published, root / f".failed-{backup_id}")
                    published = None
                raise
            stage = published = None
            return manifest
    except StorageLeaseError as error:
        if error.args == ("restore_incomplete",):
            raise BackupError("backup_restore_incomplete") from None
        raise BackupError(
            "backup_storage_in_use"
            if error.args == ("storage_in_use",)
            else "backup_storage_unsafe"
        ) from None
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_create_failed") from None
    finally:
        if published is not None:
            # A failed directory sync must not leave a selectable success.
            try:
                os.rename(published, published.parent / f".failed-{published.name}")
            except OSError:
                (published / MANIFEST).unlink(missing_ok=True)
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _inspect(path, expected_id):
    _plain_path(path, directory=True, private=True)
    files, directories = _tree(path)
    if not _REQUIRED_FILES <= files.keys():
        _reject("backup_required_file_missing")
    _plain_path(path / MANIFEST, directory=False, private=True)
    with _read(path / MANIFEST, limit=MAX_MANIFEST_BYTES) as stream:
        manifest = json.load(stream, object_pairs_hook=_unique_object)
    fields = {
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
    if (
        not isinstance(manifest, dict)
        or set(manifest) != fields
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["kind"] != KIND
        or manifest["backup_id"] != expected_id
        or not _ID.fullmatch(expected_id)
    ):
        _reject()
    created = datetime.fromisoformat(manifest["created_at"])
    if (
        created.tzinfo is None
        or created.utcoffset().total_seconds() != 0
        or not manifest["created_at"].endswith("Z")
        or not expected_id.startswith(f"cmh-backup-{created.strftime('%Y%m%dT%H%M%S%fZ')}-")
    ):
        _reject()
    if not isinstance(manifest["revision"], str) or not _REVISION.fullmatch(manifest["revision"]):
        _reject()
    for key in ("instance_id", "ledger_id", "admissions_ledger_id"):
        value = manifest[key]
        if not isinstance(value, str) or str(UUID(value, version=4)) != value:
            _reject()
    versions = manifest["versions"]
    if (
        not isinstance(versions, dict)
        or set(versions) != {"cloud-memory-hub", "one-memory", "hy-memory"}
        or any(
            not isinstance(value, str) or not value or len(value) > 64
            for value in versions.values()
        )
        or versions["hy-memory"] != HY_MEMORY_VERSION
    ):
        _reject("backup_version_unsupported")
    stores = manifest["store_versions"]
    if (
        not isinstance(stores, dict)
        or set(stores) != {"access", "governance", "admissions", "execution"}
        or any(type(value) is not int or value < 1 for value in stores.values())
    ):
        _reject("backup_version_unsupported")
    declared_dirs = manifest["directories"]
    if (
        not isinstance(declared_dirs, list)
        or not all(isinstance(item, str) for item in declared_dirs)
        or declared_dirs != sorted(set(declared_dirs))
        or set(declared_dirs) != set(directories)
        or not set(_ROOTS) <= set(declared_dirs)
    ):
        _reject("backup_inventory_mismatch")
    for name in declared_dirs:
        if _relative(name).parts[0] not in _ROOTS:
            _reject()
        _plain_path(path / name, directory=True, private=True)
    entries = manifest["files"]
    if (
        not isinstance(entries, list)
        or len(entries) > MAX_FILES
        or type(manifest["file_count"]) is not int
        or manifest["file_count"] != len(entries)
        or type(manifest["total_bytes"]) is not int
    ):
        _reject()
    expected, total = [], 0
    for row in entries:
        if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
            _reject()
        name = row["path"]
        relative = _relative(name)
        if (
            relative.parts[0] not in _ROOTS
            or len(relative.parts) < 2
            or type(row["bytes"]) is not int
            or row["bytes"] < 0
            or not isinstance(row["sha256"], str)
            or not _DIGEST.fullmatch(row["sha256"])
        ):
            _reject()
        target = _plain_path(path / name, directory=False, private=True)
        if target.stat().st_size != row["bytes"] or _hash(target) != row["sha256"]:
            _reject("backup_digest_mismatch")
        with _read(target) as stream:
            sqlite = stream.read(16) == _SQLITE_HEADER
        if not sqlite and target.suffix in {".sqlite", ".sqlite3"}:
            _reject("backup_database_invalid")
        if sqlite:
            with closing(
                sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as connection:
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    _reject("backup_database_invalid")
        expected.append(name)
        total += row["bytes"]
    if (
        expected != sorted(set(expected))
        or set(files) != {*expected, MANIFEST}
        or total != manifest["total_bytes"]
    ):
        _reject("backup_inventory_mismatch")
    for key, value in _authority_metadata(path).items():
        if manifest[key] != value:
            _reject("backup_identity_mismatch")
    for value in manifest["checkpoints"].values():
        Checkpoint.model_validate(value)
    if _tree(path) != (files, directories):
        _reject("backup_changed")
    return manifest


def inspect_backup(path: Path) -> dict:
    """Verify inventory, SQLite integrity and existing signed authority offline."""
    try:
        return _inspect(path.absolute(), path.name)
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_invalid") from None


def _summary(manifest):
    return {
        "state": "complete",
        **{
            key: manifest[key]
            for key in ("backup_id", "created_at", "revision", "file_count", "total_bytes")
        },
    }


def list_backups(root: Path) -> list[dict]:
    """Unknown files and incomplete stages are never advertised as versions."""
    try:
        if not root.exists() and not root.is_symlink():
            return []
        root = _root(root)
        with StorageLease(root):
            return _list(root)
    except StorageLeaseError:
        raise BackupError("backup_storage_in_use") from None
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_list_failed") from None


def _list(root):
    results = []
    for path in sorted(root.iterdir(), reverse=True):
        if not _ID.fullmatch(path.name):
            continue
        try:
            results.append(_summary(inspect_backup(path)))
        except BackupError as error:
            results.append({"backup_id": path.name, "state": "invalid", "error_code": error.code})
    return results


def prune_backups(root: Path, keep: int) -> dict:
    """Delete only old, freshly verified versions created by this module."""
    try:
        if type(keep) is not int or not 1 <= keep <= 1000:
            _reject("backup_retention_invalid")
        if not root.exists() and not root.is_symlink():
            return {"removed": [], "kept": 0, "invalid": 0, "failures": []}
        root = _root(root)
        with StorageLease(root):
            rows = _list(root)
            valid = sorted(
                (row for row in rows if row["state"] == "complete"),
                key=lambda row: (row["created_at"], row["backup_id"]),
                reverse=True,
            )
            result = {
                "removed": [],
                "kept": min(keep, len(valid)),
                "invalid": len(rows) - len(valid),
                "failures": [],
            }
            for row in valid[keep:]:
                identity = row["backup_id"]
                try:
                    target = root / identity
                    inspect_backup(target)
                    trash = root / f".prune-{identity}-{uuid4().hex}"
                    os.rename(target, trash)
                    _fsync_directory(root)
                    shutil.rmtree(trash)
                    _fsync_directory(root)
                    result["removed"].append(identity)
                except Exception:
                    result["failures"].append(
                        {"backup_id": identity, "error_code": "backup_prune_failed"}
                    )
            return result
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_prune_failed") from None
