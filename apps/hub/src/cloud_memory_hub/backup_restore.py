"""ADR-0063 stopped-service restore of inspected three-volume versions.

The backup store owns snapshotting and path/inventory validation. This module
adds only the security merge and interrupted-restore fence; normal Hub startup
still owns governance replay. It never unpacks archives or starts a writer.
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from cloud_memory_hub import backup_store as store
from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.storage_lease import (
    LOCK_NAME,
    RESTORE_MARKER,
    StorageLease,
    StorageLeaseError,
    require_restore_complete,
)
from cloud_memory_hub.write_admissions import WriteAdmissions

_NAMES = ("data", "config", "governance")
_WITNESSES = (
    "cmh_governance_binding",
    "cmh_governance_floors",
    "cmh_admission_binding",
    "cmh_admission_witnesses",
)
_SECURITY = ("projects", "devices", "grants", *_WITNESSES)
_TERMINAL = {"completed", "cancelled", "dead"}


class RestoreError(store.BackupError):
    """Fixed error and safe recovery identities; never carries source contents."""

    def __init__(self, code, *, operation_id=None, protective_backup_id=None):
        super().__init__(code)
        self.operation_id = operation_id
        self.protective_backup_id = protective_backup_id


def _refuse(code):
    raise RestoreError(code)


def _config(data, config, governance):
    return HubConfig(
        data,
        config,
        "http://localhost:8800",
        read_private_file(config / "admin-token").decode("ascii").strip(),
        read_private_file(config / "encryption-key").strip(),
        governance_dir=governance,
    )


def _from_stage(stage):
    return _config(*(stage / name for name in _NAMES))


def _roots(config):
    return tuple(
        path.absolute() for path in (config.data_dir, config.config_dir, config.governance_dir)
    )


def _locations(roots, backup_root, backup_path):
    # Resolve only for comparisons. Operations retain the supplied paths so the
    # store's explicit symlink/parent-traversal rejection still applies.
    paths = tuple(path.resolve(strict=False) for path in (*roots, backup_root))
    selected = backup_path.resolve(strict=False)
    for i, first in enumerate(paths):
        for second in paths[i + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                _refuse("backup_roots_overlap")
    for root in paths[:3]:
        if selected == root or root in selected.parents or selected in root.parents:
            _refuse("backup_roots_overlap")
    return store._root(backup_root, create=True)


def _fingerprint(roots):
    return tuple(store._tree(root) for root in roots)


def _copy_tree(source, target):
    files, directories = store._tree(source)
    target.mkdir(mode=0o700)
    for relative in sorted(directories, key=lambda value: (value.count("/"), value)):
        (target / relative).mkdir(mode=0o700)
    for relative in files:
        if Path(relative).name in {LOCK_NAME, RESTORE_MARKER}:
            _refuse("backup_restore_unsafe_payload")
        store._copy(source / relative, target / relative)


def _stage(root, selected, manifest, operation_id):
    stage = root / (".restore-" + operation_id)
    stage.mkdir(mode=0o700)
    # Retain the prepared files independently of the archive caller's temporary
    # extraction. Avoid a second full copy of a potentially large SDK directory.
    payload = stage / "payload"
    _copy_tree(selected, payload)
    if store._inspect(payload, manifest["backup_id"]) != manifest:
        _refuse("backup_changed")
    return stage, payload


def _authority(config):
    authority = open_authority(config, read_only=True)
    authority.require_ready()
    admissions = WriteAdmissions(authority.access, authority.journal, read_only=True)
    return authority, admissions


def _prefix(old_config, current_config):
    if not hmac.compare_digest(old_config.encryption_key, current_config.encryption_key):
        _refuse("backup_restore_key_mismatch")
    old, old_admissions = _authority(old_config)
    current, current_admissions = _authority(current_config)
    if (
        old.access.instance_id != current.access.instance_id
        or old.journal.ledger_id != current.journal.ledger_id
        or old_admissions.journal.ledger_id != current_admissions.journal.ledger_id
    ):
        _refuse("backup_restore_identity_mismatch")
    for scope, checkpoint in old.verify().items():
        current.journal.changes(scope, after=checkpoint, limit=1)
    # Admission births are separate signed streams, not main-journal scopes.
    with old_admissions.journal._connection() as db:
        for row in db.execute("SELECT scope_id FROM heads"):
            proof, _ = old_admissions.journal._stream(db, row[0])
            current_admissions.journal.changes(row[0], after=proof.checkpoint, limit=1)
    return current, current_admissions


def _table(db, name):
    # Names here are fixed module constants, never supplied by a backup payload.
    return [tuple(row) for row in db.execute(f"PRAGMA table_info({name})")]


def _merge_security(payload, current):
    staged = DeviceAccessStore(payload / "config/remote-access.sqlite3", require_existing=True)
    with current.access._connection() as source, staged._connection(writing=True) as target:
        for name in _SECURITY:
            if not _table(source, name) or _table(source, name) != _table(target, name):
                _refuse("backup_restore_security_schema_mismatch")
            if [tuple(row) for row in source.execute(f"PRAGMA foreign_key_list({name})")] != [
                tuple(row) for row in target.execute(f"PRAGMA foreign_key_list({name})")
            ]:
                _refuse("backup_restore_security_schema_mismatch")
        # Existing devices/projects cannot disappear through a supported API.
        # Keep FK references intact by upserting parents before replacing grants.
        for name, key in (("projects", "project_id"), ("devices", "device_id")):
            rows = source.execute(f"SELECT * FROM {name}").fetchall()
            current_rows = {row[key]: row for row in rows}
            for old in target.execute(f"SELECT * FROM {name}"):
                newer = current_rows.get(old[key])
                if newer is None or old["created_at"] != newer["created_at"]:
                    _refuse("backup_restore_security_identity_missing")
                if name == "devices" and (
                    old["revision"] > newer["revision"]
                    or old["status"] == "revoked"
                    and newer["status"] != "revoked"
                    or old["revision"] == newer["revision"]
                    and any(old[field] != newer[field] for field in ("credential_hash", "status"))
                ):
                    _refuse("backup_restore_security_revision_mismatch")
            columns = [row[1] for row in _table(source, name)]
            assignments = ",".join(
                f"{column}=excluded.{column}" for column in columns if column != key
            )
            placeholders = ",".join("?" for _ in columns)
            target.executemany(
                f"INSERT INTO {name} VALUES ({placeholders}) "
                f"ON CONFLICT({key}) DO UPDATE SET {assignments}",
                [tuple(row) for row in rows],
            )
        for name in ("grants", *_WITNESSES):
            rows = [tuple(row) for row in source.execute(f"SELECT * FROM {name}")]
            target.execute(f"DELETE FROM {name}")
            placeholders = ",".join("?" for _ in _table(source, name))
            target.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)
        old_has_sources = bool(_table(target, "cmh_sources"))
        current_has_sources = bool(_table(source, "cmh_sources"))
        missing_sources = 0
        if old_has_sources:
            if not current_has_sources or _table(target, "cmh_sources") != _table(
                source, "cmh_sources"
            ):
                _refuse("backup_restore_sources_mismatch")
            RemoteIngest._check_schema(source)
            RemoteIngest._check_schema(target)
            rows = {row["source_id"]: row for row in source.execute("SELECT * FROM cmh_sources")}
            old_rows = target.execute("SELECT * FROM cmh_sources").fetchall()
            for old in old_rows:
                newer = rows.get(old["source_id"])
                if (
                    newer is None
                    or any(
                        old[key] != newer[key] for key in ("device_id", "scope_id", "adapter_id")
                    )
                    or old["revision"] > newer["revision"]
                    or old["revision"] == newer["revision"]
                    and old["status"] != newer["status"]
                    or (old["status"] == "revoked" and newer["status"] != "revoked")
                ):
                    _refuse("backup_restore_sources_mismatch")
                target.execute(
                    "UPDATE cmh_sources SET status=?,revision=? WHERE source_id=?",
                    (newer["status"], newer["revision"], old["source_id"]),
                )
            fields = ("source_id", "device_id", "scope_id", "adapter_id", "revision", "status")
            expected_sources = {
                tuple(rows[old["source_id"]][field] for field in fields) for old in old_rows
            }
            if expected_sources != {
                tuple(row[field] for field in fields)
                for row in target.execute("SELECT * FROM cmh_sources")
            }:
                _refuse("backup_restore_sources_mismatch")
            missing_sources = len(rows) - len(old_rows)
        elif current_has_sources:
            RemoteIngest._check_schema(source)
            missing_sources = source.execute("SELECT count(*) FROM cmh_sources").fetchone()[0]
        for name in _SECURITY:
            if {tuple(row) for row in target.execute(f"SELECT * FROM {name}")} != {
                tuple(row) for row in source.execute(f"SELECT * FROM {name}")
            }:
                _refuse("backup_restore_security_merge_mismatch")
        if target.execute("PRAGMA foreign_key_check").fetchone():
            _refuse("backup_restore_security_foreign_key")
    for name in ("admin-token", "encryption-key"):
        (payload / "config" / name).unlink()
        store._copy(current.access.path.parent / name, payload / "config" / name)
    return missing_sources


def _queue_safe(payload, admissions):
    path = payload / "data/one-memory.sqlite3"
    with (
        closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as core,
        admissions.access._connection() as access,
    ):
        has_queue = bool(_table(core, "memory_write_jobs"))
        has_receipts = bool(_table(access, "cmh_event_receipts"))
        if has_receipts:
            for row in access.execute("SELECT request_id,receipt_id FROM cmh_event_receipts"):
                if (
                    not has_queue
                    or core.execute(
                        "SELECT 1 FROM memory_write_jobs WHERE idempotency_key=? AND job_id=?",
                        tuple(row),
                    ).fetchone()
                    is None
                ):
                    _refuse("backup_restore_receipt_queue_mismatch")
        if not has_queue:
            return
        for request_id, state, invoked in core.execute(
            "SELECT idempotency_key,state,hy_invoked FROM memory_write_jobs"
        ):
            if state in _TERMINAL:
                continue
            if state == "unknown" or invoked:
                _refuse("backup_restore_write_uncertain")
            if not isinstance(request_id, str) or not request_id.startswith("cmh2:"):
                _refuse("backup_restore_queue_unproven")
            proof = admissions.proof_in(access, request_id, refresh_witness=False)
            if proof.state == "possible":
                _refuse("backup_restore_write_uncertain")


def _inventory(root):
    files, directories = store._tree(root)
    return (
        {
            name: store._hash(root / name)
            for name in files
            if name not in {LOCK_NAME, RESTORE_MARKER}
        },
        set(directories),
    )


def _markers(roots, value):
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    for root in roots:
        descriptor = os.open(
            root / RESTORE_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        store._fsync_directory(root)


def _replace_contents(source, target):
    """Keep mount roots, held lock inodes and the startup fence in place."""
    store._tree(target)
    for path in target.iterdir():
        if path.name in {LOCK_NAME, RESTORE_MARKER}:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    files, directories = store._tree(source)
    for relative in sorted(directories, key=lambda value: (value.count("/"), value)):
        (target / relative).mkdir(mode=0o700)
    for relative in files:
        store._copy(source / relative, target / relative)
    store._private_sync(target)


def _install(payload, roots, *, names, marker):
    expected = {name: _inventory(payload / name) for name in names}
    _markers(roots, marker)
    for name, root in zip(_NAMES, roots, strict=True):
        if name in names:
            _replace_contents(payload / name, root)
    for name, root in zip(_NAMES, roots, strict=True):
        if name in names and _inventory(root) != expected[name]:
            _refuse("backup_restore_installed_mismatch")
    _authority(_config(*roots))
    # All roots are now validated. Even a failure clearing markers must keep at
    # least one fence: re-establish removed markers before reporting failure.
    try:
        for root in roots:
            (root / RESTORE_MARKER).unlink()
            store._fsync_directory(root)
    except BaseException:
        for root in roots:
            if not (root / RESTORE_MARKER).exists():
                try:
                    _markers((root,), marker)
                except OSError:
                    pass
        raise


def _failure(error, operation_id, protective_id, roots):
    try:
        require_restore_complete(*roots)
        incomplete = False
    except StorageLeaseError:
        incomplete = True
    if incomplete:
        code = "backup_restore_incomplete"
    elif isinstance(error, store.BackupError):
        code = error.code
    elif isinstance(error, StorageLeaseError):
        code = (
            "backup_storage_in_use"
            if error.args == ("storage_in_use",)
            else "backup_restore_unavailable"
        )
    else:
        code = "backup_restore_validation_failed"
    return RestoreError(code, operation_id=operation_id, protective_backup_id=protective_id)


def _result(manifest, operation_id, protective_id, missing_sources):
    return {
        "status": "files_restored_pending_startup",
        "operation_id": operation_id,
        "backup_id": manifest["backup_id"],
        "protective_backup_id": protective_id,
        "instance_id": manifest["instance_id"],
        "sources_requiring_reconnection": missing_sources,
    }


def _cleanup(stage, root):
    # Removal of a completed staging copy is housekeeping, not a failed restore.
    # A retained hidden stage is never offered as a complete backup version.
    try:
        shutil.rmtree(stage)
        store._fsync_directory(root)
        return False
    except OSError:
        return True


def restore_backup(config: HubConfig, backup_path: Path, backup_root: Path) -> dict:
    """Restore old business state with current security, after a protective version."""
    roots, selected = _roots(config), backup_path.absolute()
    operation_id, protective_id = str(uuid4()), None
    try:
        root = _locations(roots, backup_root, selected)
        manifest = store.inspect_backup(selected)
        with StorageLease(*roots):
            require_restore_complete(*roots)
            before = _fingerprint(roots)
        protective = store.create_backup(
            config, root, revision=os.environ.get("CMH_REVISION", "development")
        )
        protective_id = protective["backup_id"]
        # create_backup owns its own operation lease. Reacquire and prove that
        # no writer changed any source file in the intervening lock gap.
        with StorageLease(*roots, root):
            require_restore_complete(*roots)
            if _fingerprint(roots) != before:
                _refuse("backup_restore_source_changed")
            stage, payload = _stage(root, selected, manifest, operation_id)
            protected = root / protective_id
            if store.inspect_backup(protected) != protective:
                _refuse("backup_changed")
            current_config = _from_stage(protected)
            current, _ = _prefix(_from_stage(payload), current_config)
            missing_sources = _merge_security(payload, current)
            shutil.rmtree(payload / "governance")
            _copy_tree(protected / "governance", payload / "governance")
            store._normalize_databases(payload)
            store._private_sync(payload)
            _, admissions = _authority(_from_stage(payload))
            _queue_safe(payload, admissions)
            store._fsync_directory(stage)
            store._fsync_directory(root)
            marker = _result(manifest, operation_id, protective_id, missing_sources)
            marker["status"] = "restore_incomplete"
            _install(payload, roots, names=("data", "config"), marker=marker)
            # No running governance file is replaced by historical state.
            result = _result(manifest, operation_id, protective_id, missing_sources)
            result["staging_cleanup_pending"] = _cleanup(stage, root)
            return result
    except Exception as error:
        raise _failure(error, operation_id, protective_id, roots) from None


def import_backup(
    backup_path: Path,
    *,
    data_dir: Path,
    config_dir: Path,
    governance_dir: Path,
    backup_root: Path,
    source_stopped: bool = False,
) -> dict:
    """Import into empty roots; caller must explicitly stop the original instance."""
    roots = tuple(path.absolute() for path in (data_dir, config_dir, governance_dir))
    selected, operation_id = backup_path.absolute(), str(uuid4())
    try:
        if source_stopped is not True:
            _refuse("backup_import_source_must_be_stopped")
        root = _locations(roots, backup_root, selected)
        manifest = store.inspect_backup(selected)
        for path in roots:
            store._root(path, create=True)
        with StorageLease(*roots, root):
            require_restore_complete(*roots)
            for path in roots:
                if any(child.name != LOCK_NAME for child in path.iterdir()):
                    _refuse("backup_import_target_not_empty")
            stage, payload = _stage(root, selected, manifest, operation_id)
            _, admissions = _authority(_from_stage(payload))
            _queue_safe(payload, admissions)
            marker = _result(manifest, operation_id, None, 0)
            marker["status"] = "restore_incomplete"
            store._private_sync(stage)
            store._fsync_directory(root)
            _install(payload, roots, names=_NAMES, marker=marker)
            result = _result(manifest, operation_id, None, 0)
            result["staging_cleanup_pending"] = _cleanup(stage, root)
            return result
    except Exception as error:
        raise _failure(error, operation_id, None, roots) from None
