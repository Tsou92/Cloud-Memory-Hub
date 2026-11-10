"""FR-11/13: actual child shutdown ordering, durable plans, and bounded failures."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cloud_memory_hub import backup_runtime as runtime


def wait_for(predicate):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("supervisor did not reach expected state")


def configuration(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        governance_dir=tmp_path / "governance",
    )


def worker(tmp_path):
    script = tmp_path / "synthetic_worker.py"
    log = tmp_path / "process-events.txt"
    script.write_text(
        "import signal,sys,time\n"
        "from pathlib import Path\n"
        "p=Path(sys.argv[1])\n"
        "def record(value):\n"
        "    with p.open('a') as f: f.write(value+'\\n'); f.flush()\n"
        "def stop(*args):\n"
        "    record('stop'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "record('start')\n"
        "while True: time.sleep(.02)\n"
    )
    return [sys.executable, str(script), str(log)], log


def start(tmp_path, monkeypatch, backup):
    from cloud_memory_hub import backup_store

    monkeypatch.setattr(backup_store, "prune_backups", lambda *a, **kw: {})
    command, log = worker(tmp_path)
    stopped = threading.Event()
    failures = []

    def run():
        try:
            runtime.supervise(
                configuration(tmp_path),
                tmp_path / "backups",
                child_command=command,
                stop_event=stopped,
                poll_seconds=0.02,
                backup=backup,
            )
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    wait_for(lambda: log.exists() and log.read_text().count("start") == 1)
    return stopped, thread, failures, log


def finish(stopped, thread, failures):
    stopped.set()
    thread.join(10)
    assert not thread.is_alive()
    assert not failures


def test_schedule_persists_and_disabled_is_not_claimed_running(tmp_path):
    root = tmp_path / "backups"
    assert runtime.status(root)["schedule"]["enabled"] is False
    plan = runtime.configure(root, interval_minutes=1440, keep=4)
    result = runtime.status(root)
    assert result["schedule"] == plan
    assert result["supervisor_running"] == "not_checked"
    assert runtime._timestamp(result["next_due_at"]) == (
        runtime._timestamp(plan["configured_at"]) + timedelta(days=1)
    )
    assert (root / "schedule.json").stat().st_mode & 0o777 == 0o600
    runtime.configure(root, enabled=False)
    assert runtime.status(root)["next_due_at"] is None


@pytest.mark.parametrize("kwargs", [{"interval_minutes": 0}, {"keep": 0}, {"keep": True}])
def test_invalid_plan_never_replaces_previous(tmp_path, kwargs):
    root = tmp_path / "backups"
    before = runtime.configure(root)
    with pytest.raises(runtime.BackupRuntimeError, match="schedule_invalid"):
        runtime.configure(root, **kwargs)
    assert runtime.read_plan(root) == before


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "oversize", "unknown"])
def test_control_file_safety(tmp_path, unsafe):
    root = tmp_path / "backups"
    runtime.configure(root)
    path = root / "schedule.json"
    if unsafe == "symlink":
        original = tmp_path / "elsewhere"
        path.rename(original)
        path.symlink_to(original)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "linked")
    elif unsafe == "public":
        path.chmod(0o644)
    elif unsafe == "oversize":
        path.write_text("x" * 65537)
    else:
        value = json.loads(path.read_text())
        value["credential"] = "synthetic-do-not-echo"
        path.write_text(json.dumps(value))
    with pytest.raises(runtime.BackupRuntimeError):
        runtime.status(root)


def test_manual_request_is_not_overwritten_while_pending(tmp_path):
    root = tmp_path / "backups"
    first = runtime.request_backup(root)
    with pytest.raises(runtime.BackupRuntimeError, match="request_pending"):
        runtime.request_backup(root)
    assert runtime.status(root)["pending_request"] == first["request_id"]


def test_competing_control_writer_fails_without_waiting(tmp_path):
    root = tmp_path / "backups"
    with runtime._control(root):
        started = time.monotonic()
        with pytest.raises(runtime.BackupRuntimeError, match="control_busy"):
            runtime.configure(root)
        assert time.monotonic() - started < 1
    assert runtime.status(root)["schedule"]["enabled"] is False


def test_two_due_backups_stop_child_first_then_restart_and_keep_plan(tmp_path, monkeypatch):
    root = tmp_path / "backups"
    instant = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(runtime, "_now", lambda: instant[0])
    plan = runtime.configure(root, interval_minutes=1, keep=2)
    calls = []
    log = tmp_path / "process-events.txt"

    def backup(*args, **kwargs):
        assert log.read_text().splitlines()[-1] == "stop"
        calls.append(kwargs)
        return {"backup_id": "synthetic-" + str(len(calls)), "file_count": 3, "total_bytes": 12}

    stopped, thread, failures, _ = start(tmp_path, monkeypatch, backup)
    try:
        instant[0] += timedelta(seconds=61)
        wait_for(lambda: log.read_text().count("start") == 2)
        assert len(calls) == 1
        instant[0] += timedelta(seconds=61)
        wait_for(lambda: log.read_text().count("start") == 3)
        assert len(calls) == 2
        result = runtime.status(root)
        assert result["execution"]["state"] == "completed"
        assert result["execution"]["last_backup_id"] == "synthetic-2"
        assert result["schedule"] == plan
    finally:
        finish(stopped, thread, failures)
    assert log.read_text().splitlines() == ["start", "stop"] * 3


def test_backup_failure_restarts_service_without_busy_retry_or_error_leak(tmp_path, monkeypatch):
    root = tmp_path / "backups"
    calls = []

    def backup(*args, **kwargs):
        calls.append(True)
        raise OSError("synthetic-private-content")

    stopped, thread, failures, log = start(tmp_path, monkeypatch, backup)
    try:
        runtime.request_backup(root)
        wait_for(lambda: log.read_text().count("start") == 2)
        time.sleep(0.12)
        assert len(calls) == 1
        result = runtime.status(root)
        assert result["execution"]["state"] == "failed"
        assert result["execution"]["error"] == "backup_create_failed"
        assert "synthetic-private-content" not in json.dumps(result)
        assert result["pending_request"] is None
    finally:
        finish(stopped, thread, failures)


def test_interrupted_state_survives_restart_as_explicit_failure(tmp_path, monkeypatch):
    root = tmp_path / "backups"
    runtime.prepare_root(root)
    runtime._write(
        root,
        "status.json",
        {
            "schema_version": 1,
            "state": "running",
            "last_attempt_at": runtime._now().isoformat(),
        },
    )
    stopped, thread, failures, _ = start(tmp_path, monkeypatch, lambda *a, **kw: pytest.fail())
    try:
        result = runtime.status(root)
        assert result["execution"]["state"] == "interrupted"
        assert result["execution"]["error"] == "backup_interrupted"
        assert "last_success_at" not in result["execution"]
    finally:
        finish(stopped, thread, failures)


def test_unexpected_child_exit_does_not_loop(tmp_path):
    with pytest.raises(runtime.BackupRuntimeError, match="service_exited"):
        runtime.supervise(
            configuration(tmp_path),
            tmp_path / "backups",
            child_command=[sys.executable, "-c", "raise SystemExit(7)"],
            poll_seconds=0.02,
            backup=lambda *a, **kw: pytest.fail(),
        )


def test_forced_stop_is_never_successful():
    class Child:
        def __init__(self):
            self.actions = []

        def poll(self):
            return None

        def terminate(self):
            self.actions.append("terminate")

        def wait(self, timeout):
            if self.actions[-1] != "kill":
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return -9

        def kill(self):
            self.actions.append("kill")

    child = Child()
    assert runtime._stop_child(child, timeout=0.1) is False
    assert child.actions == ["terminate", "kill"]


def test_status_rejects_untrusted_error_text(tmp_path):
    root = tmp_path / "backups"
    runtime.prepare_root(root)
    runtime._write(
        root,
        "status.json",
        {
            "schema_version": 1,
            "state": "failed",
            "error": "synthetic-private-content",
        },
    )
    with pytest.raises(runtime.BackupRuntimeError, match="status_invalid"):
        runtime.status(root)


@pytest.mark.parametrize("phase", ["before_snapshot", "after_snapshot", "restart"])
def test_unwritable_control_state_keeps_service_available_without_restart_loop(
    tmp_path,
    monkeypatch,
    phase,
    capsys,
):
    root = tmp_path / "backups"
    runtime.prepare_root(root)
    if phase == "restart":
        runtime._write(
            root,
            "status.json",
            {
                "schema_version": 1,
                "state": "running",
                "last_attempt_at": runtime._now().isoformat(),
            },
        )
    original = runtime._write
    failures_written = []
    copies = []

    def write(directory, name, value):
        if (
            name == "status.json"
            and value["state"]
            == {
                "before_snapshot": "running",
                "after_snapshot": "completed",
                "restart": "interrupted",
            }[phase]
        ):
            failures_written.append(True)
            raise OSError("synthetic-full-disk-private-path")
        return original(directory, name, value)

    def backup(*args, **kwargs):
        copies.append(True)
        return {"backup_id": "synthetic-success", "file_count": 1, "total_bytes": 4}

    monkeypatch.setattr(runtime, "_write", write)
    stopped, thread, failures, log = start(tmp_path, monkeypatch, backup)
    try:
        if phase != "restart":
            runtime.request_backup(root)
            wait_for(lambda: log.read_text().count("start") == 2)
        time.sleep(0.15)
        assert len(failures_written) == 1
        assert log.read_text().splitlines()[-1] == "start"
        assert thread.is_alive()
        assert len(copies) == (1 if phase == "after_snapshot" else 0)
    finally:
        finish(stopped, thread, failures)
    errors = capsys.readouterr().err
    assert "backup_control_unavailable_retry_delayed" in errors
    assert "synthetic-full-disk-private-path" not in errors


@pytest.mark.parametrize("phase", ["before_snapshot", "after_snapshot", "restart"])
def test_control_write_recovery_publishes_terminal_state(tmp_path, monkeypatch, phase):
    root = tmp_path / "backups"
    runtime.prepare_root(root)
    if phase == "restart":
        runtime._write(
            root,
            "status.json",
            {
                "schema_version": 1,
                "state": "running",
                "last_attempt_at": runtime._now().isoformat(),
            },
        )
    original = runtime._write
    failed_once = []

    def write(directory, name, value):
        wanted = {
            "before_snapshot": "running",
            "after_snapshot": "completed",
            "restart": "interrupted",
        }[phase]
        if name == "status.json" and value["state"] == wanted and not failed_once:
            failed_once.append(True)
            raise OSError("synthetic-transient-write-error")
        return original(directory, name, value)

    monkeypatch.setattr(runtime, "_write", write)
    monkeypatch.setattr(runtime, "CONTROL_RETRY_SECONDS", 0.12)
    stopped, thread, failures, log = start(
        tmp_path,
        monkeypatch,
        lambda *a, **kw: {
            "backup_id": "synthetic-complete",
            "file_count": 1,
            "total_bytes": 4,
        },
    )
    try:
        if phase != "restart":
            runtime.request_backup(root)
            wait_for(lambda: log.read_text().count("start") == 2)
        expected = "completed" if phase == "after_snapshot" else "interrupted"
        wait_for(lambda: runtime.status(root)["execution"]["state"] == expected)
        assert len(failed_once) == 1
        assert log.read_text().splitlines()[-1] == "start"
    finally:
        finish(stopped, thread, failures)


def test_real_uvicorn_shutdown_finishes_lifespan_before_zero_exit(tmp_path):
    script = tmp_path / "real_uvicorn.py"
    log = tmp_path / "lifespan.txt"
    script.write_text(
        "import sys,uvicorn\n"
        "from pathlib import Path\n"
        "from contextlib import asynccontextmanager\n"
        "from starlette.applications import Starlette\n"
        "from cloud_memory_hub.__main__ import graceful_sigterm\n"
        "p=Path(sys.argv[1])\n"
        "@asynccontextmanager\n"
        "async def lifespan(app):\n"
        "    p.write_text('ready')\n"
        "    yield\n"
        "    p.write_text('closed')\n"
        "with graceful_sigterm():\n"
        "    uvicorn.run(Starlette(lifespan=lifespan),host='127.0.0.1',port=0,log_config=None)\n"
    )
    child = subprocess.Popen([sys.executable, str(script), str(log)])
    try:
        wait_for(lambda: log.exists() and log.read_text() == "ready")
        assert runtime._stop_child(child)
        assert child.returncode == 0
        assert log.read_text() == "closed"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_retention_failure_does_not_hide_successful_snapshot(tmp_path, monkeypatch):
    from cloud_memory_hub import backup_store

    root = tmp_path / "backups"
    runtime.prepare_root(root)
    monkeypatch.setattr(backup_store, "prune_backups", lambda *a, **kw: {"failures": ["synthetic"]})
    runtime._perform(
        configuration(tmp_path),
        root,
        {},
        {},
        None,
        lambda *a, **kw: {
            "backup_id": "synthetic-version",
            "file_count": 1,
            "total_bytes": 2,
        },
    )
    state = runtime.status(root)["execution"]
    assert state["state"] == "completed"
    assert state["retention_error"] == "backup_retention_failed"


@pytest.mark.parametrize("trigger", ["request", "overdue"])
def test_restart_runs_persisted_work_while_cold_before_starting_child(
    tmp_path, monkeypatch, trigger
):
    root = tmp_path / "backups"
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(runtime, "_now", lambda: clock[0])
    if trigger == "request":
        runtime.request_backup(root)
    else:
        runtime.configure(root, interval_minutes=1)
        clock[0] += timedelta(minutes=2)
    log = tmp_path / "process-events.txt"
    calls = []

    def backup(*args, **kwargs):
        assert not log.exists(), "backup must run before any child starts"
        calls.append(True)
        return {"backup_id": "synthetic-cold", "file_count": 1, "total_bytes": 1}

    stopped, thread, failures, _ = start(tmp_path, monkeypatch, backup)
    try:
        assert len(calls) == 1
        assert runtime.status(root)["execution"]["state"] == "completed"
        assert log.read_text().splitlines() == ["start"]
    finally:
        finish(stopped, thread, failures)
