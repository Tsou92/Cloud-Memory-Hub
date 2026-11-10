"""Headless deployment entry, including explicit non-overwriting initialization."""

import argparse
import json
import logging
import os
import secrets
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.diagnostics import initialize_diagnostics, install_process_exception_hooks
from cloud_memory_hub.governance_authority import activate_authority, require_separate_directory
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.storage_lease import (
    StorageLease,
    StorageLeaseError,
    require_restore_complete,
)


@contextmanager
def graceful_sigterm():
    """Let Uvicorn re-raise SIGTERM after cleanup without an ambiguous -15 exit.

    Uvicorn owns the signal during its lifespan. Its final re-raise invokes this
    saved handler only after shutdown, so the supervisor can require exit zero.
    A TERM before this context is installed remains an unsuccessful stop.
    """
    previous = signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def initialize(directory: Path, *, governance_dir: Path) -> None:
    require_separate_directory(governance_dir, directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    governance_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with StorageLease(directory, governance_dir):
        _initialize_locked(directory, governance_dir)


def _initialize_locked(directory, governance_dir):
    DeviceAccessStore._check_private(directory, directory=True)
    values = {
        "admin-token": secrets.token_urlsafe(48).encode(),
        "encryption-key": Fernet.generate_key(),
    }
    if any((directory / name).exists() or (directory / name).is_symlink() for name in values):
        raise ValueError("Configuration already exists; refusing to replace credentials")
    if any(
        path.exists() or path.is_symlink()
        for path in (directory / "remote-access.sqlite3", governance_dir / "journal.sqlite3")
    ):
        raise ValueError("Authority already exists; refusing to initialize over existing state")
    for name, value in values.items():
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    activate_authority(directory, governance_dir, values["encryption-key"], new=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="cloud-memory-hub")
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "supervise",
            "init",
            "migrate-governance",
            "migrate-originals",
            "diagnostic-preview",
            "activate-file-coverage",
        ],
        nargs="?",
        default="serve",
    )
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--governance-dir", type=Path)
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    config_dir = args.config_dir or Path(os.environ.get("CMH_CONFIG_DIR", "/config"))
    governance_dir = args.governance_dir or Path(
        os.environ.get("CMH_GOVERNANCE_DIR", "/governance")
    )
    require_separate_directory(
        governance_dir, config_dir, Path(os.environ.get("CMH_DATA_DIR", "/data"))
    )
    if args.operation_id and args.command != "diagnostic-preview":
        parser.error("--operation-id is only valid for diagnostic-preview")
    if args.command == "activate-file-coverage":
        if not args.writers_stopped:
            parser.error("activate-file-coverage requires --writers-stopped")
        from cloud_memory_hub.governance_diagnostic_maintenance import activate_file_coverage
        from cloud_memory_hub.governance_journal import GovernanceError
        from cloud_memory_hub.remote_access import AccessError

        config = HubConfig(
            Path(os.environ.get("CMH_DATA_DIR", "/data")),
            config_dir,
            "http://localhost:8800",
            read_private_file(config_dir / "admin-token").decode("ascii").strip(),
            read_private_file(config_dir / "encryption-key").strip(),
            governance_dir=governance_dir,
        )
        try:
            result = activate_file_coverage(config)
        except (GovernanceError, AccessError):
            print(json.dumps({"error": "file_coverage_activation_unavailable"}))
            raise SystemExit(2) from None
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    if args.command == "diagnostic-preview":
        if not args.writers_stopped or not args.operation_id:
            parser.error("diagnostic-preview requires --writers-stopped and --operation-id")
        from cloud_memory_hub.governance_diagnostic_maintenance import preview_diagnostic_repair
        from cloud_memory_hub.governance_journal import GovernanceError
        from cloud_memory_hub.remote_access import AccessError

        config = HubConfig(
            Path(os.environ.get("CMH_DATA_DIR", "/data")),
            config_dir,
            "http://localhost:8800",
            read_private_file(config_dir / "admin-token").decode("ascii").strip(),
            read_private_file(config_dir / "encryption-key").strip(),
            governance_dir=governance_dir,
        )
        try:
            result = preview_diagnostic_repair(config, args.operation_id)
        except (GovernanceError, AccessError):
            # Never serialize an unexpected exception or partially observed grant set.
            print(json.dumps({"error": "diagnostic_preview_unavailable"}))
            raise SystemExit(2) from None
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    if args.command == "init":
        initialize(config_dir, governance_dir=governance_dir)
        print("Private configuration and governance authority initialized.")
        return
    if args.command == "migrate-originals":
        from cloud_memory_hub.original_archive import migrate_originals

        if not args.writers_stopped:
            parser.error("migrate-originals requires stopped writers and --writers-stopped")
        data_dir = Path(os.environ.get("CMH_DATA_DIR", "/data"))
        config = HubConfig(
            data_dir,
            config_dir,
            "http://localhost:8800",
            read_private_file(config_dir / "admin-token").decode("ascii").strip(),
            read_private_file(config_dir / "encryption-key").strip(),
            governance_dir=governance_dir,
        )
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with StorageLease(data_dir, config_dir, governance_dir):
            count = migrate_originals(config)
        print(f"Original archive verified; recovered {count} accepted events.")
        return
    if args.command == "migrate-governance":
        if not args.writers_stopped:
            parser.error("migrate-governance requires stopped writers and --writers-stopped")
        key = read_private_file(config_dir / "encryption-key").strip()
        data_dir = Path(os.environ.get("CMH_DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        governance_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with StorageLease(data_dir, config_dir, governance_dir):
            activate_authority(config_dir, governance_dir, key)
        print("Governance binding verified. Device identities and credentials preserved.")
        return
    if args.config_dir or args.governance_dir or args.writers_stopped:
        parser.error("serve uses CMH_CONFIG_DIR / CMH_GOVERNANCE_DIR environment settings")
    require_restore_complete(
        Path(os.environ.get("CMH_DATA_DIR", "/data")), config_dir, governance_dir
    )
    config = HubConfig.from_environment()
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "supervise":
        from cloud_memory_hub.backup_runtime import serve_supervised

        serve_supervised(config, Path(os.environ.get("CMH_BACKUP_DIR", "/backups")))
        return
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        require_restore_complete(config.data_dir, config.config_dir, config.governance_dir)
        import uvicorn

        from cloud_memory_hub.app import create_hub

        application = create_hub(config)
        with graceful_sigterm():
            uvicorn.run(
                application,
                host=config.host,
                port=config.port,
                access_log=False,
                proxy_headers=False,
                log_config=None,
            )


def main() -> None:
    try:
        initialize_diagnostics()
        install_process_exception_hooks()
        _main()
    except StorageLeaseError as error:
        # Preserve the public lease-conflict recovery code without formatting
        # arbitrary exceptions or copying their message into diagnostic output.
        if error.args in {("storage_in_use",), ("restore_incomplete",)}:
            print("cloud-memory-hub: " + error.args[0], file=sys.stderr)
        else:
            logging.getLogger(__name__).error("hub_storage_unavailable", exc_info=True)
        raise SystemExit(2) from None
    except Exception:
        logging.getLogger(__name__).error("hub_startup_failed", exc_info=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
