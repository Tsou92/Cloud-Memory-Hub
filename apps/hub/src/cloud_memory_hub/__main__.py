"""Headless deployment entry, including explicit non-overwriting initialization."""

import argparse
import os
import secrets
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet

from cloud_memory_hub.config import HubConfig


def initialize(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {
        "admin-token": secrets.token_urlsafe(48).encode(),
        "encryption-key": Fernet.generate_key(),
    }
    if any((directory / name).exists() or (directory / name).is_symlink() for name in values):
        raise ValueError("Configuration already exists; refusing to replace credentials")
    for name, value in values.items():
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="cloud-memory-hub")
    parser.add_argument("command", choices=["serve", "init"], nargs="?", default="serve")
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.config_dir or Path(os.environ.get("CMH_CONFIG_DIR", "/config")))
        print("Private configuration created. Existing credentials were not changed.")
        return
    from cloud_memory_hub.app import create_hub

    config = HubConfig.from_environment()
    uvicorn.run(
        create_hub(config),
        host=config.host,
        port=config.port,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
