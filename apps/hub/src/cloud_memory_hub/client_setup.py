"""FR-02/03/07/12: import a downloaded pairing into a private, independent client."""

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.client_processing import read_processing_sample
from cloud_memory_hub.client_service import ClientServiceError, service_install, service_start
from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.onboarding.connect import (
    ConnectError,
    Installation,
    atomic_write,
    automation_workspace,
    checked_path,
    ensure_dir,
    exclusive_lock,
    json_bytes,
    json_config,
    read_file,
    stable_automation_python,
)
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
    ):
        raise ClientSetupError("existing_client_binding_conflict")
    existing = RemoteConnection.from_file(settings.connection_file)
    connection = bundle.connection()
    if existing.model_copy(update={"network_mode": connection.network_mode}) != connection:
        raise ClientSetupError("existing_client_binding_conflict")
    settings.open()  # Validate the retained queue; never reinitialize it.
    if (
        "network_mode" not in bundle.model_fields_set
        or existing.network_mode == connection.network_mode
    ):
        return False
    try:
        with exclusive_lock(target.parent):
            raw = read_file(settings.connection_file, private=True)
            if raw is None or RemoteConnection.model_validate(parse_json(raw)) != existing:
                raise ClientSetupError("existing_client_binding_conflict")
            updated = parse_json(raw)
            updated["network_mode"] = connection.network_mode
            if read_file(settings.connection_file, private=True) != raw:
                raise ClientSetupError("existing_client_binding_conflict")
            atomic_write(settings.connection_file, json_bytes(updated))
    except ConnectError as error:
        raise ClientSetupError(str(error)) from None
    return True


async def import_pairing(path, *, client_root=None, transport=None):
    bundle = PairingBundle.from_download(path)
    return await import_bundle(bundle, client_root=client_root, transport=transport)


async def import_bundle(bundle, *, client_root=None, transport=None):
    connection = bundle.connection()
    root = (client_root or default_client_root()).absolute()
    target = root / f"{bundle.instance_id}-{bundle.device_id}"
    await verify_connection(
        connection, scope_id=bundle.scope_id, source_id=bundle.source_id, transport=transport
    )
    _private_directory(root)
    if target.exists() or target.is_symlink():
        changed = _existing_client(target, bundle)
        result = {"status": "already_installed", "config_file": str(target / "agent.json")}
        if changed:
            result["connection_updated"] = True
        return result
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


_HOOK_TARGETS = {"claude": ".claude/settings.local.json", "codex": ".codex/hooks.json"}
# DeepSeek Harness has no shell hooks. packages/dsh-plugin reads this home-relative
# binding list and forwards its turn events to the same `dsh-hook` command.
DSH_BINDINGS = ".cloud-memory-hub/dsh-capture.json"


def _managed_hook_command(value):
    if type(value) is not str:
        return False
    try:
        parts = shlex.split(value)
    except ValueError:
        return False
    return bool(
        parts
        and Path(parts[0]).name == "cloud-memory-agent"
        and "--config-file" in parts
        and ("claude-hook" in parts or "codex-hook" in parts)
    )


def _merge_automation_hooks(raw, command):
    document = json_config(raw)
    hooks = document.setdefault("hooks", {})
    if type(hooks) is not dict:
        raise ClientSetupError("automation_configuration_conflict")
    changed = False
    for event in ("UserPromptSubmit", "Stop"):
        entries = hooks.setdefault(event, [])
        if type(entries) is not list:
            raise ClientSetupError("automation_configuration_conflict")
        matching = 0
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise ClientSetupError("automation_configuration_conflict")
            for hook in entry["hooks"]:
                value = hook.get("command") if isinstance(hook, dict) else None
                if value == command and hook.get("type") == "command":
                    matching += 1
                elif _managed_hook_command(value):
                    raise ClientSetupError("automation_configuration_conflict")
        if matching > 1:
            raise ClientSetupError("automation_configuration_conflict")
        if not matching:
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 12}]})
            changed = True
    return json_bytes(document) if changed else raw


def _merge_dsh_binding(raw, workspace, command):
    document = json_config(raw) or {"schema_version": 1, "bindings": []}
    bindings = document.get("bindings")
    if document.get("schema_version") != 1 or type(bindings) is not list:
        raise ClientSetupError("automation_configuration_conflict")
    entry = {"workspace": str(workspace), "command": command}
    for item in bindings:
        if type(item) is not dict:
            raise ClientSetupError("automation_configuration_conflict")
        if item.get("workspace") == entry["workspace"]:
            if item == entry:
                return raw
            raise ClientSetupError("automation_configuration_conflict")
    bindings.append(entry)
    return json_bytes(document)


def _automation_agent_executable():
    command = Path(sys.executable).absolute().with_name("cloud-memory-agent")
    release = command.parent.parent.parent
    stable_automation_python()
    temporary = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    if (
        command.parent != release / "venv/bin"
        or any(release.resolve().is_relative_to(root) for root in temporary)
        or read_file(release / "installation.json", private=True) is None
    ):
        raise ClientSetupError("automation_runtime_unavailable")
    return command


def install_automation(config_file, *, agent, workspace, home=None, executable=None):
    """Install the selected project's native hooks using its existing local queue."""
    if agent not in _HOOK_TARGETS and agent != "dsh":
        raise ClientSetupError("automation_agent_unsupported")
    home = Path(home or Path.home()).absolute()
    try:
        project = automation_workspace(workspace, home)
        config_file = checked_path(config_file)
        _private_directory(config_file.parent)
        AgentSettings.from_file(config_file).open()
        command = checked_path(executable or _automation_agent_executable())
        info = command.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {os.getuid(), 0}
            or info.st_mode & 0o022
            or not os.access(command, os.X_OK)
            or any(ord(char) < 32 or ord(char) == 127 for char in str(command) + str(config_file))
        ):
            raise ClientSetupError("automation_runtime_unavailable")
        argv = [
            str(command),
            "--config-file",
            str(config_file),
            f"{agent}-hook",
            "--workspace",
            str(project),
        ]
        invocation = shlex.join(argv)
        target = home / DSH_BINDINGS if agent == "dsh" else project / _HOOK_TARGETS[agent]
        directory = ensure_dir(config_file.parent / "automation", private=True)
        binding = {
            "schema_version": 1,
            "workspace": str(project),
            "agent": agent,
            "command": invocation,
        }
        with exclusive_lock(config_file.parent.parent):
            saved = read_file(directory / "pending.json", private=True)
            state = parse_json(saved) if saved is not None else {**binding, "plans": {}}
            if (
                type(state) is not dict
                or any(state.get(key) != value for key, value in binding.items())
                or type(state.get("plans")) is not dict
            ):
                raise ClientSetupError("automation_configuration_conflict")
            installation = Installation(directory, state)
            if agent not in state["plans"]:
                original = read_file(target)
                proposed = (
                    _merge_dsh_binding(original, project, argv)
                    if agent == "dsh"
                    else _merge_automation_hooks(original, invocation)
                )
                installation.plan(agent, [(target, proposed, original)])
            installation.apply(agent, {target})
    except ConnectError as error:
        raise ClientSetupError(str(error)) from None

    background = "unavailable"
    if sys.platform == "darwin":
        try:
            service_install(config_file, home=home)
            status = service_start(config_file, home=home)
            if status["loaded"] and status["state"] != "needs_attention":
                background = "running" if status["running"] else "waiting"
        except ClientServiceError:
            pass
    return {"status": "configured", "background_delivery": background}


async def client_status(config_file, *, check_remote=False, transport=None):
    settings = AgentSettings.from_file(config_file)
    service = settings.open()
    result = {
        "queue": service.outbox.status(),
        "remote": {"status": "not_checked"},
        "processing": {"status": "not_checked"},
        "scope_id": settings.scope_id,
        "source_id": settings.source_id,
    }
    if check_remote:
        events = service.outbox.recent_metadata(
            scope_id=settings.scope_id, source_id=settings.source_id
        )
        try:
            result["remote"] = await verify_connection(
                service.connection,
                scope_id=settings.scope_id,
                source_id=settings.source_id,
                transport=transport,
            )
            result["processing"] = await read_processing_sample(
                service.connection, events, transport=transport
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
