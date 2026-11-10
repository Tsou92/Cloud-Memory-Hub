"""FR-06/07: owned LaunchAgent lifecycle without touching daily Agent settings."""

import json
import os
import plistlib
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub import client_service as service
from cloud_memory_hub.agent_config import AgentSettings
from test_agent_mcp import config_file
from test_transport import connection_data


class Launchd:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.fail = {}
        self.pid = 60000

    def __call__(self, *arguments, capture=True):
        self.calls.append(arguments)
        action = arguments[0]
        stdout = ""
        key = action
        if action == "print":
            key = "print-domain" if arguments[1].count("/") == 1 else "print-job"
        if key in self.fail:
            return subprocess.CompletedProcess(arguments, self.fail[key], "")
        if key == "print-job":
            if arguments[1] not in self.jobs:
                return subprocess.CompletedProcess(arguments, 113, "")
            job = self.jobs[arguments[1]]
            argv = job["definition"]["ProgramArguments"]
            lines = [
                f"path = {job['path']}",
                f"program = {argv[0]}",
                "arguments = {",
                *("\t" + argument for argument in argv),
                "}",
                "state = " + ("running" if job["pid"] else "not running"),
            ]
            if job["pid"]:
                lines.append(f"pid = {job['pid']}")
            if job["exit"] is not None:
                lines.append(f"last exit code = {job['exit']}")
            stdout = "\n".join(lines)
        elif action == "bootstrap":
            path = Path(arguments[2])
            definition = plistlib.loads(path.read_bytes())
            target = arguments[1] + "/" + definition["Label"]
            self.pid += 1
            self.jobs[target] = {
                "path": str(path),
                "definition": definition,
                "pid": self.pid,
                "exit": None,
            }
        elif action == "kickstart":
            self.pid += 1
            self.jobs[arguments[1]].update(pid=self.pid, exit=None)
        elif action == "bootout":
            self.jobs.pop(arguments[1])
        return subprocess.CompletedProcess(arguments, 0, stdout)


@pytest.fixture
def binding(tmp_path):
    connection = connection_data()
    config = config_file(tmp_path / "private binding", connection, str(uuid4()), str(uuid4()))
    home = tmp_path / "isolated home"
    home.mkdir(mode=0o700)
    return SimpleNamespace(config=config, connection=connection, home=home)


@pytest.fixture
def launchd(binding, monkeypatch, tmp_path):
    command = tmp_path / "runtime" / "bin" / "cloud-memory-connector"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\nexit 2\n")
    command.chmod(0o700)
    fake = Launchd()
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_connector_path", lambda: command)
    monkeypatch.setattr(service, "_launchctl", fake)
    return fake


def invoke(action, binding):
    return getattr(service, "service_" + action)(binding.config, home=binding.home)


def test_install_is_private_idempotent_and_does_not_start_or_capture(binding, launchd):
    library = binding.home / "Library"
    library.mkdir(mode=0o755)
    agents = library / "LaunchAgents"
    agents.mkdir(mode=0o755)
    result = invoke("install", binding)
    assert result["installed"] and not result["loaded"] and not result["running"]
    assert result["state"] == "stopped" and result["online"] == "not_checked"
    assert result["mode"] == "queued_delivery_only"
    path = Path(result["plist_path"])
    raw, inode = path.read_bytes(), path.stat().st_ino
    assert path.stat().st_mode & 0o777 == 0o600
    assert library.stat().st_mode & 0o777 == agents.stat().st_mode & 0o777 == 0o755
    definition = plistlib.loads(raw)
    assert definition["ProgramArguments"][1] == "watch"
    assert definition["RunAtLoad"] is True and definition["KeepAlive"] is False
    assert definition["StandardOutPath"] == definition["StandardErrorPath"] == "/dev/null"
    assert binding.connection["credential"].encode() not in raw
    assert binding.connection["credential"] not in json.dumps(result)
    assert binding.connection["instance_id"] in result["label"]
    assert binding.connection["device_id"] in result["label"]
    assert invoke("install", binding) == result
    assert (path.read_bytes(), path.stat().st_ino) == (raw, inode)
    assert all(call[0] == "print" for call in launchd.calls)


def test_start_status_stop_uninstall_preserve_queue_and_credentials(binding, launchd):
    settings = AgentSettings.from_file(binding.config)
    queue = settings.state_dir / "outbox.sqlite3"
    original = {
        path: path.read_bytes() for path in (binding.config, settings.connection_file, queue)
    }
    initial = invoke("install", binding)
    started = invoke("start", binding)
    assert started["installed"] and started["loaded"] and started["running"]
    assert started["pid"] and started["online"] == "not_checked"
    assert invoke("start", binding)["pid"] == started["pid"]
    assert sum(call[0] == "bootstrap" for call in launchd.calls) == 1
    stopped = invoke("stop", binding)
    assert stopped["installed"] and not stopped["loaded"] and not stopped["running"]
    assert Path(initial["plist_path"]).exists()
    assert invoke("start", binding)["pid"] != started["pid"]
    removed = invoke("uninstall", binding)
    assert not removed["installed"] and not removed["loaded"] and not removed["running"]
    assert removed["state"] == "not_installed" and not Path(initial["plist_path"]).exists()
    assert invoke("uninstall", binding) == removed
    assert all(path.read_bytes() == value for path, value in original.items())


def test_permanent_exit_is_visible_and_only_explicit_start_restarts(binding, launchd):
    invoke("install", binding)
    initial = invoke("start", binding)
    job = next(iter(launchd.jobs.values()))
    job.update(pid=None, exit=2)
    count = len(launchd.calls)
    stopped = invoke("status", binding)
    assert stopped["loaded"] and not stopped["running"] and stopped["last_exit_code"] == 2
    assert stopped["state"] == "needs_attention"
    assert all(call[0] == "print" for call in launchd.calls[count:])
    restarted = invoke("start", binding)
    assert restarted["running"] and restarted["pid"] != initial["pid"]
    assert sum(call[0] == "kickstart" for call in launchd.calls) == 1


@pytest.mark.parametrize("kind", ["unknown", "public", "symlink", "hardlink"])
def test_unknown_or_unsafe_plist_is_preserved(binding, launchd, tmp_path, kind):
    result = invoke("install", binding)
    path = Path(result["plist_path"])
    if kind == "unknown":
        value = plistlib.loads(path.read_bytes())
        value["ProgramArguments"] = ["/bin/false"]
        path.write_bytes(plistlib.dumps(value))
    elif kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        original = tmp_path / "original.plist"
        path.rename(original)
        path.symlink_to(original)
    else:
        os.link(path, tmp_path / "other-link.plist")
    raw, info = path.read_bytes(), path.lstat()
    before = len(launchd.calls)
    for action in ("install", "start", "stop", "uninstall", "status"):
        with pytest.raises(service.ClientServiceError, match="^service_definition_conflict$"):
            invoke(action, binding)
    assert path.read_bytes() == raw and path.lstat().st_ino == info.st_ino
    assert len(launchd.calls) == before


@pytest.mark.parametrize("kind", ["symlink", "writable"])
def test_unsafe_shared_directory_is_not_repaired(binding, launchd, tmp_path, kind):
    library = binding.home / "Library"
    if kind == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir()
        library.symlink_to(target)
    else:
        library.mkdir()
        library.chmod(0o777)
    with pytest.raises(service.ClientServiceError, match="^service_directory_unsafe$"):
        invoke("install", binding)
    assert library.is_symlink() if kind == "symlink" else library.stat().st_mode & 0o777 == 0o777


def test_loaded_definition_conflict_is_not_stopped_or_replaced(binding, launchd):
    invoke("install", binding)
    invoke("start", binding)
    job = next(iter(launchd.jobs.values()))
    job["definition"]["ProgramArguments"][-1] = "/another-connection.json"
    before = len([call for call in launchd.calls if call[0] != "print"])
    for action in ("install", "start", "stop", "uninstall", "status"):
        with pytest.raises(
            service.ClientServiceError, match="^service_loaded_definition_conflict$"
        ):
            invoke(action, binding)
    assert len([call for call in launchd.calls if call[0] != "print"]) == before


def test_missing_loaded_definition_requires_review(binding, launchd):
    installed = invoke("install", binding)
    invoke("start", binding)
    Path(installed["plist_path"]).unlink()
    with pytest.raises(service.ClientServiceError, match="^service_definition_missing$"):
        invoke("uninstall", binding)
    assert len(launchd.jobs) == 1


@pytest.mark.parametrize("kind", ["missing", "symlink", "writable", "not_executable"])
def test_connector_must_be_current_runtime_executable(
    binding, launchd, monkeypatch, tmp_path, kind
):
    path = service._connector_path()
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        original = tmp_path / "connector-original"
        path.rename(original)
        path.symlink_to(original)
    elif kind == "writable":
        path.chmod(0o777)
    else:
        path.chmod(0o600)
    with pytest.raises(service.ClientServiceError, match="^service_connector_unavailable$"):
        invoke("install", binding)
    assert not (binding.home / "Library").exists()


@pytest.mark.parametrize(
    "failure,action,reason",
    [
        ("print-domain", "status", "service_session_unavailable"),
        ("print-job", "status", "service_status_unavailable"),
        ("bootstrap", "start", "service_start_failed"),
        ("bootout", "uninstall", "service_stop_failed"),
    ],
)
def test_controller_failures_are_safe_and_do_not_delete_state(
    binding, launchd, failure, action, reason
):
    installed = invoke("install", binding)
    if failure == "bootout":
        invoke("start", binding)
    launchd.fail[failure] = 5
    with pytest.raises(service.ClientServiceError, match="^" + reason + "$") as error:
        invoke(action, binding)
    assert error.value.code == reason
    assert binding.connection["credential"] not in str(error.value)
    assert Path(installed["plist_path"]).exists()


def test_uninstall_rechecks_changed_file_after_bootout(binding, launchd, monkeypatch):
    installed = invoke("install", binding)
    invoke("start", binding)
    path = Path(installed["plist_path"])

    def replace(*arguments, **kwargs):
        result = launchd(*arguments, **kwargs)
        if arguments[0] == "bootout":
            path.write_bytes(b"unknown new owner")
        return result

    monkeypatch.setattr(service, "_launchctl", replace)
    with pytest.raises(service.ClientServiceError, match="^service_definition_conflict$"):
        invoke("uninstall", binding)
    assert path.read_bytes() == b"unknown new owner"


def test_atomic_publish_failure_keeps_unknown_file_and_cleans_own_temp(
    binding, launchd, monkeypatch
):
    def race(source, destination, **kwargs):
        Path(destination).write_bytes(b"another installer")
        raise FileExistsError

    monkeypatch.setattr(service.os, "link", race)
    with pytest.raises(service.ClientServiceError, match="^service_definition_conflict$"):
        invoke("install", binding)
    agents = binding.home / "Library" / "LaunchAgents"
    assert not list(agents.glob(".cmh-*"))
    assert next(agents.glob("*.plist")).read_bytes() == b"another installer"


def test_invalid_binding_or_missing_outbox_is_not_recreated(binding, launchd):
    settings = AgentSettings.from_file(binding.config)
    queue = settings.state_dir / "outbox.sqlite3"
    queue.unlink()
    with pytest.raises(service.ClientServiceError, match="^service_configuration_invalid$"):
        invoke("install", binding)
    assert not queue.exists() and not (binding.home / "Library").exists()


def test_stop_and_uninstall_work_when_queue_is_missing_without_recreating_it(binding, launchd):
    invoke("install", binding)
    invoke("start", binding)
    queue = AgentSettings.from_file(binding.config).state_dir / "outbox.sqlite3"
    queue.unlink()
    assert invoke("status", binding)["running"]
    assert not invoke("stop", binding)["loaded"]
    assert not invoke("uninstall", binding)["installed"]
    assert not queue.exists()


def test_unsupported_platform_and_start_before_install(binding, launchd, monkeypatch):
    with pytest.raises(service.ClientServiceError, match="^service_not_installed$"):
        invoke("start", binding)
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(service.ClientServiceError, match="^service_platform_unsupported$"):
        invoke("status", binding)


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("CMH_TEST_LAUNCHD") != "1",
    reason="explicit isolated macOS LaunchAgent integration run only",
)
def test_real_launchagent_delivery_exit_and_uninstall(tmp_path):
    """Opt-in: a unique job and temporary home, local synthetic server, no daily config."""
    requests = []

    class Denied(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b'{"detail":"invalid_device_credential"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Denied)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "isolated home"
    home.mkdir(mode=0o700)
    connection = connection_data(f"http://127.0.0.1:{server.server_port}")
    config = config_file(tmp_path / "private binding", connection, str(uuid4()), str(uuid4()))
    binding = SimpleNamespace(home=home, config=config)
    settings = AgentSettings.from_file(config)
    before = (settings.state_dir / "outbox.sqlite3").read_bytes()
    installed = False
    try:
        installed = invoke("install", binding)["installed"]
        assert installed
        invoke("start", binding)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not invoke("status", binding)["running"]:
            time.sleep(0.05)
        assert invoke("status", binding)["running"] and not requests
        invoke("stop", binding)
        assert not invoke("status", binding)["loaded"]
        assert (settings.state_dir / "outbox.sqlite3").read_bytes() == before
        outbox = settings.open().outbox
        outbox.enqueue(
            {
                "instance_id": connection["instance_id"],
                "device_id": connection["device_id"],
                "source_id": settings.source_id,
                "scope_id": settings.scope_id,
                **{key: str(uuid4()) for key in ("event_id", "conversation_id", "record_id")},
                "revision": 1,
                "kind": "operation",
                "content": {"text": "Synthetic local LaunchAgent delivery probe."},
            }
        )
        invoke("start", binding)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and invoke("status", binding)["last_exit_code"] != 2:
            time.sleep(0.05)
        state = invoke("status", binding)
        assert state["loaded"] and not state["running"] and state["last_exit_code"] == 2
        assert requests == ["/hub/v1/session"]
        time.sleep(1.2)
        assert invoke("status", binding)["last_exit_code"] == 2
        assert requests == [
            "/hub/v1/session"
        ]  # No KeepAlive restart storm after authorization fails.
        assert outbox.status()["queued"] == 1
        removed = invoke("uninstall", binding)
        installed = False
        assert not removed["installed"] and not removed["loaded"]
        assert outbox.status()["queued"] == 1 and settings.connection_file.exists()
    finally:
        if installed:
            invoke("uninstall", binding)
        server.shutdown()
        thread.join(5)
        server.server_close()
