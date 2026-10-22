"""Explicit remote boundary configuration, with no implicit user-home access."""

from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def read_private_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Credential file must be a private regular file (mode 0600)")
        return stream.read(4097)


@dataclass(frozen=True)
class HubConfig:
    data_dir: Path
    config_dir: Path
    public_origin: str
    admin_token: str = field(repr=False)
    encryption_key: bytes = field(repr=False)
    host: str = "0.0.0.0"
    port: int = 8800

    def __post_init__(self) -> None:
        parts = urlsplit(self.public_origin)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ValueError("CMH_PUBLIC_ORIGIN must be an exact HTTP(S) origin without a path")
        loopback = parts.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            pass
        if parts.scheme == "http" and not loopback:
            raise ValueError("Remote access requires HTTPS; HTTP is limited to localhost")
        if not 32 <= len(self.admin_token) <= 256 or not self.admin_token.isascii():
            raise ValueError("Admin credential must contain 32 to 256 ASCII characters")
        if any(character.isspace() for character in self.admin_token):
            raise ValueError("Admin credential cannot contain whitespace")
        if not 1 <= self.port <= 65535:
            raise ValueError("Invalid listen port")

    @property
    def authority(self) -> str:
        return urlsplit(self.public_origin).netloc.lower()

    @property
    def secure_cookie(self) -> bool:
        return self.public_origin.startswith("https://")

    @classmethod
    def from_environment(cls) -> HubConfig:
        config_dir = Path(os.environ.get("CMH_CONFIG_DIR", "/config"))
        return cls(
            data_dir=Path(os.environ.get("CMH_DATA_DIR", "/data")),
            config_dir=config_dir,
            public_origin=os.environ.get("CMH_PUBLIC_ORIGIN", "http://localhost:8800"),
            admin_token=read_private_file(config_dir / "admin-token").decode("ascii").strip(),
            encryption_key=read_private_file(config_dir / "encryption-key").strip(),
            port=int(os.environ.get("CMH_PORT", "8800")),
        )
