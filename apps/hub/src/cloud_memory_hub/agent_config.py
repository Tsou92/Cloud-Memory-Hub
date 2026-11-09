"""Explicit, private configuration shared by MCP and the small Agent CLI."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError, parse_json
from cloud_memory_hub.remote_access import ScopeGrant
from cloud_memory_hub.transport import RemoteConnection


class AgentConfigurationError(ValueError):
    pass


def _private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError
        data = stream.read(4097)
    if len(data) > 4096:
        raise ValueError
    return parse_json(data)


@dataclass(frozen=True, slots=True)
class AgentSettings:
    connection_file: Path
    state_dir: Path
    scope_id: str
    source_id: str
    known_credentials_file: Path | None = None

    @classmethod
    def from_file(cls, path):
        try:
            raw = _private_json(path)
            required = {"schema_version", "connection_file", "state_dir", "scope_id", "source_id"}
            if (
                type(raw) is not dict
                or not required <= raw.keys()
                or raw.keys() - required - {"known_credentials_file"}
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
            ):
                raise ValueError
            for key in ("connection_file", "state_dir", "known_credentials_file"):
                if key in raw and (
                    type(raw[key]) is not str
                    or not Path(raw[key]).is_absolute()
                    or "\x00" in raw[key]
                ):
                    raise ValueError
            canonical_uuid(raw["source_id"])
            ScopeGrant.valid_scope(raw["scope_id"])
            return cls(
                Path(raw["connection_file"]),
                Path(raw["state_dir"]),
                raw["scope_id"],
                raw["source_id"],
                Path(raw["known_credentials_file"]) if "known_credentials_file" in raw else None,
            )
        except (OSError, ValueError, TypeError, AttributeError, PrivacyError):
            raise AgentConfigurationError("agent_configuration_invalid") from None

    def open(self, *, initialize=False):
        from cloud_memory_hub.agent_access import AgentAccess

        connection = RemoteConnection.from_file(self.connection_file)
        known = ()
        if self.known_credentials_file is not None:
            try:
                known = _private_json(self.known_credentials_file)
                if type(known) is not list or any(type(item) is not str for item in known):
                    raise ValueError
            except (OSError, ValueError, TypeError, PrivacyError):
                raise AgentConfigurationError("known_credentials_invalid") from None
        privacy = CredentialFilter((*known, connection.credential.get_secret_value()))
        factory = LocalOutbox.initialize if initialize else LocalOutbox
        outbox = factory(
            self.state_dir / "outbox.sqlite3",
            instance_id=connection.instance_id,
            device_id=connection.device_id,
            privacy=privacy,
        )
        return AgentAccess(connection, outbox, scope_id=self.scope_id, source_id=self.source_id)
