"""FR-06/07: explicit macOS background delivery over the existing connector watch.

This manages one owned LaunchAgent per Hub/device. It neither captures history
nor treats a running process or an empty queue as proof of remote connectivity.
"""

import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.transport import RemoteConnection


class ClientServiceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Service:
    label: str
    path: Path
    home: Path
    definition: dict

    @property
    def domain(self):
        return f"gui/{os.getuid()}"

    @property
    def target(self):
        return f"{self.domain}/{self.label}"


def _connector_path():
    return Path(sys.executable).absolute().parent / "cloud-memory-connector"


def _service(agent_config, home, *, validate_outbox=False):
    if sys.platform != "darwin":
        raise ClientServiceError("service_platform_unsupported")
    try:
        settings = AgentSettings.from_file(Path(agent_config).absolute())
        connection = RemoteConnection.from_file(settings.connection_file)
        if validate_outbox:
            settings.open()  # Never initialize or replace a queue while managing the service.
    except Exception:
        raise ClientServiceError("service_configuration_invalid") from None
    home = Path(home or Path.home()).absolute()
    label = f"com.cloudmemoryhub.sync.{connection.instance_id}.{connection.device_id}"
    arguments = [
        str(_connector_path()),
        "watch",
        "--state-dir",
        str(settings.state_dir),
        "--connection-file",
        str(settings.connection_file),
    ]
    if settings.known_credentials_file is not None:
        arguments.extend(("--known-credentials-file", str(settings.known_credentials_file)))
    if any(any(ord(char) < 32 or ord(char) == 127 for char in argument) for argument in arguments):
        raise ClientServiceError("service_configuration_invalid")
    definition = {
        "Label": label,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "WorkingDirectory": str(settings.state_dir),
        "Umask": 0o077,
        # The watch emits only metadata, but do not accumulate unbounded logs.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    }
    return _Service(label, home / "Library" / "LaunchAgents" / f"{label}.plist", home, definition)


def _check_directories(service, *, create=False):
    for directory in (service.home, service.home / "Library", service.path.parent):
        if create and directory != service.home:
            directory.mkdir(mode=0o700, exist_ok=True)
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if not create:
                return
            raise
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ClientServiceError("service_directory_unsafe")
        # Existing Library/LaunchAgents may legitimately be 0755. Do not chmod it.


def _installed(service):
    _check_directories(service)
    try:
        descriptor = os.open(service.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        raise ClientServiceError("service_definition_conflict") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ClientServiceError("service_definition_conflict")
        raw = stream.read(65537)
    try:
        same = len(raw) <= 65536 and plistlib.loads(raw) == service.definition
    except Exception:
        same = False
    if not same:
        raise ClientServiceError("service_definition_conflict")
    return True


def _require_connector(service):
    command = Path(service.definition["ProgramArguments"][0])
    try:
        info = command.lstat()
    except OSError:
        raise ClientServiceError("service_connector_unavailable") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {os.getuid(), 0}
        or info.st_mode & 0o022
        or not os.access(command, os.X_OK)
    ):
        raise ClientServiceError("service_connector_unavailable")


def _launchctl(*arguments, capture=True):
    try:
        result = subprocess.run(
            ["/bin/launchctl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ClientServiceError("service_controller_unavailable") from None
    if capture and len(result.stdout) > 65536:
        raise ClientServiceError("service_controller_response_invalid")
    return result


def _job(service):
    if _launchctl("print", service.domain, capture=False).returncode != 0:
        raise ClientServiceError("service_session_unavailable")
    result = _launchctl("print", service.target)
    if result.returncode == 113:  # The GUI domain exists, but this service does not.
        return None
    if result.returncode != 0:
        raise ClientServiceError("service_status_unavailable")
    path = re.search(r"^[ \t]*path = (.+)$", result.stdout, re.MULTILINE)
    program = re.search(r"^[ \t]*program = (.+)$", result.stdout, re.MULTILINE)
    arguments = re.search(
        r"^[ \t]*arguments = \{\n(.*?)^[ \t]*\}", result.stdout, re.MULTILINE | re.DOTALL
    )
    if (
        path is None
        or path[1].strip() != str(service.path)
        or program is None
        or program[1].strip() != service.definition["ProgramArguments"][0]
        or arguments is None
        or [line.strip() for line in arguments[1].splitlines()]
        != service.definition["ProgramArguments"]
    ):
        raise ClientServiceError("service_loaded_definition_conflict")
    pid = re.search(r"^\s*pid = ([1-9][0-9]*)\s*$", result.stdout, re.MULTILINE)
    exited = re.search(r"^\s*last exit code = (-?[0-9]+)\s*$", result.stdout, re.MULTILINE)
    running = re.search(r"^\s*state = running\s*$", result.stdout, re.MULTILINE) is not None
    return {
        "running": running and pid is not None,
        "pid": int(pid[1]) if running and pid else None,
        "last_exit_code": int(exited[1]) if exited else None,
    }


def _status(service):
    installed, job = _installed(service), _job(service)
    if job is not None and not installed:
        raise ClientServiceError("service_definition_missing")
    running = job is not None and job["running"]
    exited = job["last_exit_code"] if job else None
    state = (
        "not_installed"
        if not installed
        else "stopped"
        if job is None
        else "running"
        if running
        else "needs_attention"
        if exited is not None and exited != 0
        else "waiting"
    )
    return {
        "schema_version": 1,
        "label": service.label,
        "plist_path": str(service.path),
        "installed": installed,
        "loaded": job is not None,
        "running": running,
        "state": state,
        "pid": job["pid"] if job else None,
        "last_exit_code": exited,
        "online": "not_checked",
        "mode": "queued_delivery_only",
    }


def _write(service):
    _check_directories(service, create=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".cmh-", dir=service.path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            plistlib.dump(service.definition, file)
            file.flush()
            os.fsync(file.fileno())
        # An exclusive link publishes the complete file without replacing a rival.
        os.link(temporary, service.path, follow_symlinks=False)
    except FileExistsError:
        if not _installed(service):
            raise ClientServiceError("service_definition_conflict") from None
    finally:
        if temporary is not None:
            temporary.unlink()
    directory = os.open(service.path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _manage(action, agent_config, *, home=None):
    try:
        service = _service(agent_config, home, validate_outbox=action in {"install", "start"})
        status = _status(service)
        if action == "status":
            return status
        if action == "install":
            _require_connector(service)
            if not status["installed"]:
                _write(service)
        elif action == "start":
            if not status["installed"]:
                raise ClientServiceError("service_not_installed")
            _require_connector(service)
            if not status["running"]:
                arguments = (
                    ("kickstart", service.target)
                    if status["loaded"]
                    else ("bootstrap", service.domain, str(service.path))
                )
                if _launchctl(*arguments, capture=False).returncode != 0:
                    raise ClientServiceError("service_start_failed")
        elif action in {"stop", "uninstall"}:
            if status["loaded"]:
                if _launchctl("bootout", service.target, capture=False).returncode != 0:
                    raise ClientServiceError("service_stop_failed")
            if action == "uninstall" and status["installed"]:
                # Revalidate after launchctl yields; never unlink an unknown replacement.
                if not _installed(service):
                    raise ClientServiceError("service_definition_changed")
                service.path.unlink()
        return _status(service)
    except ClientServiceError:
        raise
    except Exception:
        raise ClientServiceError("service_operation_unavailable") from None


def service_install(agent_config: Path, *, home: Path | None = None):
    """Install for the next login; starting in the current login is a separate action."""
    return _manage("install", agent_config, home=home)


def service_start(agent_config: Path, *, home: Path | None = None):
    return _manage("start", agent_config, home=home)


def service_status(agent_config: Path, *, home: Path | None = None):
    return _manage("status", agent_config, home=home)


def service_stop(agent_config: Path, *, home: Path | None = None):
    return _manage("stop", agent_config, home=home)


def service_uninstall(agent_config: Path, *, home: Path | None = None):
    """Remove only the owned LaunchAgent. Credentials and queued events are preserved."""
    return _manage("uninstall", agent_config, home=home)
