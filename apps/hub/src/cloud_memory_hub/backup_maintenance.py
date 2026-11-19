"""Explicit offline backup export/import/recovery commands (ADR-0063)."""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from cloud_memory_hub import backup_crypto
from cloud_memory_hub import backup_store as store
from cloud_memory_hub.backup_archive import DEFAULT_MAX_BYTES, export_backup, unpack_backup
from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.governance_authority import require_separate_directory


def _roots():
    return {
        "data_dir": Path(os.environ.get("CMH_DATA_DIR", "/data")),
        "config_dir": Path(os.environ.get("CMH_CONFIG_DIR", "/config")),
        "governance_dir": Path(os.environ.get("CMH_GOVERNANCE_DIR", "/governance")),
    }


def _passphrase(args, *, required=False):
    value = None
    if args.passphrase_file is not None:
        try:
            raw = read_private_file(args.passphrase_file)
        except (OSError, ValueError):
            raise store.BackupError("backup_passphrase_file_invalid") from None
        raw = raw.rstrip(b"\r\n")
        if not raw or len(raw) > backup_crypto.MAX_PASSPHRASE_BYTES:
            raise store.BackupError("backup_passphrase_file_invalid")
        value = raw
    else:
        environment = os.environ.get("CMH_BACKUP_PASSPHRASE", "")
        if environment:
            value = environment.encode("utf-8")
            if len(value) > backup_crypto.MAX_PASSPHRASE_BYTES:
                raise store.BackupError("backup_passphrase_invalid")
    if required and value is None:
        raise store.BackupError("backup_passphrase_required")
    return value


@contextmanager
def _archive_source(path, args, *, max_bytes):
    if backup_crypto.is_encrypted(path):
        with backup_crypto.decrypted_archive(
            path,
            args.backup_dir,
            _passphrase(args, required=True),
            max_bytes=max_bytes,
        ) as archive:
            with unpack_backup(archive, args.backup_dir, max_bytes=max_bytes) as version:
                yield version
    else:
        with unpack_backup(path, args.backup_dir, max_bytes=max_bytes) as version:
            yield version


@contextmanager
def _source(args):
    if args.archive:
        with _archive_source(args.archive, args, max_bytes=args.max_bytes) as path:
            yield path
    else:
        if not store._ID.fullmatch(args.version):
            raise store.BackupError("backup_id_invalid")
        yield args.backup_dir / args.version


def _run(args):
    roots = _roots()
    require_separate_directory(args.backup_dir, *roots.values())
    if args.command in {"restore", "import"} and not args.writers_stopped:
        raise store.BackupError("backup_writers_stopped_required")
    if args.command == "import" and not args.source_stopped:
        raise store.BackupError("backup_source_stopped_required")
    with _source(args) as version:
        if args.command == "export":
            return export_backup(
                version,
                args.output,
                passphrase=_passphrase(args, required=args.encrypt) if args.encrypt else None,
            )
        if args.command == "inspect":
            manifest = store.inspect_backup(version)
            return {
                "state": "verified",
                **{
                    name: manifest[name]
                    for name in (
                        "backup_id",
                        "created_at",
                        "revision",
                        "file_count",
                        "total_bytes",
                    )
                },
            }
        if args.command == "import":
            from cloud_memory_hub.backup_restore import import_backup

            return import_backup(version, **roots, backup_root=args.backup_dir, source_stopped=True)
        from cloud_memory_hub.backup_restore import restore_backup
        from cloud_memory_hub.config import HubConfig

        return restore_backup(HubConfig.from_environment(), version, args.backup_dir)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Cloud Memory Hub 完整备份离线维护")
    parser.add_argument("command", choices=["export", "inspect", "import", "restore"])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--version", help="已完成版本的备份 ID")
    source.add_argument("--archive", type=Path, help="私有 .cmh.tar 完整归档")
    parser.add_argument(
        "--backup-dir", type=Path, default=Path(os.environ.get("CMH_BACKUP_DIR", "/backups"))
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--encrypt",
        action="store_true",
        help="Encrypt the exported archive; requires --passphrase-file or CMH_BACKUP_PASSPHRASE",
    )
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        help="Private 0600 file containing the backup passphrase",
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--writers-stopped", action="store_true", help="确认目标 Hub 已停止；仍将核对存储租约"
    )
    parser.add_argument(
        "--source-stopped", action="store_true", help="空目标导入前确认原实例已停止，避免同身份双主"
    )
    args = parser.parse_args()
    if (args.command == "export") != (args.output is not None):
        parser.error("--output is required only for export")
    if args.command == "export" and args.archive:
        parser.error("export requires --version")
    if args.encrypt and args.command != "export":
        parser.error("--encrypt is only valid for export")
    if args.source_stopped and args.command != "import":
        parser.error("--source-stopped is only valid for import")
    if args.writers_stopped and args.command not in {"restore", "import"}:
        parser.error("--writers-stopped is only valid for import/restore")
    try:
        print(json.dumps(_run(args), ensure_ascii=False, sort_keys=True))
    except Exception as error:
        code = error.code if isinstance(error, store.BackupError) else "backup_maintenance_failed"
        print(json.dumps({"error": code}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
