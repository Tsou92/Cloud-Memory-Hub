"""FR-02/03/07/12: import a downloaded pairing into a private, independent client."""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError, parse_json
from cloud_memory_hub.remote_access import ScopeGrant
from cloud_memory_hub.transport import RemoteConnection, TransportError, verify_connection


class ClientSetupError(ValueError):
    """Fixed, safe error codes for local installation failures."""


class PairingBundle(RemoteConnection):
    schema_version: Literal[1]
    kind: Literal["cloud-memory-hub-pairing"]
    source_id: str = Field(strict=True)
    scope_id: str = Field(strict=True)

    @model_validator(mode="after")
    def binding(self):
        canonical_uuid(self.source_id)
        ScopeGrant.valid_scope(self.scope_id)
        return self

    def connection(self):
        return RemoteConnection.model_validate(
            self.model_dump(exclude={"kind", "source_id", "scope_id"})
        )

    @classmethod
    def from_download(cls, path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_size > 4096
                ):
                    raise ValueError
                # Browser downloads may start at 0644; tighten this verified inode only.
                os.fchmod(stream.fileno(), 0o600)
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError
            return cls.model_validate(parse_json(raw))
        except (OSError, ValueError, TypeError, PrivacyError):
            raise ClientSetupError("pairing_file_invalid") from None


def default_client_root():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CloudMemoryHub" / "clients"
    return Path.home() / ".local" / "share" / "cloud-memory-hub" / "clients"


def _private_directory(path):
    # Reject redirection through existing symlinks, including an optional caller root.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ClientSetupError("client_directory_unsafe")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ClientSetupError("client_directory_unsafe")


def _write_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _existing_client(target, bundle):
    _private_directory(target)
    settings = AgentSettings.from_file(target / "agent.json")
    if (
        settings.connection_file != target / "connection.json"
        or settings.state_dir != target / "state"
        or settings.scope_id != bundle.scope_id
        or settings.source_id != bundle.source_id
        or RemoteConnection.from_file(settings.connection_file) != bundle.connection()
    ):
        raise ClientSetupError("existing_client_binding_conflict")
    settings.open()  # Validate the retained queue; never reinitialize it.


async def import_pairing(path, *, client_root=None, transport=None):
    bundle = PairingBundle.from_download(path)
    connection = bundle.connection()
    root = (client_root or default_client_root()).absolute()
    target = root / f"{bundle.instance_id}-{bundle.device_id}"
    await verify_connection(
        connection, scope_id=bundle.scope_id, source_id=bundle.source_id, transport=transport
    )
    _private_directory(root)
    if target.exists() or target.is_symlink():
        _existing_client(target, bundle)
        return {"status": "already_installed", "config_file": str(target / "agent.json")}
    stage = Path(tempfile.mkdtemp(prefix=".pairing-", dir=root))
    try:
        raw_connection = connection.model_dump(mode="json")
        raw_connection["credential"] = connection.credential.get_secret_value()
        _write_json(stage / "connection.json", raw_connection)
        _write_json(
            stage / "agent.json",
            {
                "schema_version": 1,
                "connection_file": str(target / "connection.json"),
                "state_dir": str(target / "state"),
                "scope_id": bundle.scope_id,
                "source_id": bundle.source_id,
            },
        )
        LocalOutbox.initialize(
            stage / "state" / "outbox.sqlite3",
            instance_id=bundle.instance_id,
            device_id=bundle.device_id,
            privacy=CredentialFilter((connection.credential.get_secret_value(),)),
        )
        with (stage / "state" / "outbox.sqlite3").open("rb") as stream:
            os.fsync(stream.fileno())
        _sync_directory(stage / "state")
        _sync_directory(stage)
        # No replacement of an existing installation, including a concurrent import.
        if target.exists() or target.is_symlink():
            raise ClientSetupError("existing_client_binding_conflict")
        stage.rename(target)
        _sync_directory(root)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"status": "installed", "config_file": str(target / "agent.json")}


async def client_status(config_file, *, check_remote=False, transport=None):
    settings = AgentSettings.from_file(config_file)
    service = settings.open()
    result = {
        "queue": service.outbox.status(),
        "remote": {"status": "not_checked"},
        "scope_id": settings.scope_id,
        "source_id": settings.source_id,
    }
    if check_remote:
        try:
            result["remote"] = await verify_connection(
                service.connection,
                scope_id=settings.scope_id,
                source_id=settings.source_id,
                transport=transport,
            )
        except TransportError as error:
            result["remote"] = {
                "status": "unavailable",
                "error": error.code,
                "retryable": error.retryable,
            }
    return result


def mcp_configuration(config_file):
    AgentSettings.from_file(config_file).open()
    command = Path(sys.executable).with_name("cloud-memory-mcp")
    if not command.is_file() or not os.access(command, os.X_OK):
        raise ClientSetupError("client_executable_unavailable")
    return {
        "mcpServers": {
            "cloud-memory-hub": {
                "command": str(command),
                "args": ["--config-file", str(config_file.absolute())],
            }
        }
    }
