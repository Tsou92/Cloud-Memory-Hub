"""Headless deployment entry, including explicit non-overwriting initialization."""

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.diagnostics import initialize_diagnostics, install_process_exception_hooks
from cloud_memory_hub.governance_authority import activate_authority, require_separate_directory
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.storage_lease import StorageLease, StorageLeaseError


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
        choices=["serve", "init", "migrate-governance", "migrate-originals"],
        nargs="?",
        default="serve",
    )
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--governance-dir", type=Path)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    config_dir = args.config_dir or Path(os.environ.get("CMH_CONFIG_DIR", "/config"))
    governance_dir = args.governance_dir or Path(
        os.environ.get("CMH_GOVERNANCE_DIR", "/governance")
    )
    require_separate_directory(
        governance_dir, config_dir, Path(os.environ.get("CMH_DATA_DIR", "/data"))
    )
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
    config = HubConfig.from_environment()
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        import uvicorn

        from cloud_memory_hub.app import create_hub

        uvicorn.run(
            create_hub(config),
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
        if error.args == ("storage_in_use",):
            print("cloud-memory-hub: storage_in_use", file=sys.stderr)
        else:
            logging.getLogger(__name__).error("hub_storage_unavailable", exc_info=True)
        raise SystemExit(2) from None
    except Exception:
        logging.getLogger(__name__).error("hub_startup_failed", exc_info=True)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
