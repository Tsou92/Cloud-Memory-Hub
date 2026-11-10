"""Cold-backup scheduling without host privileges or a Docker socket (ADR-0063)."""

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from cloud_memory_hub.governance_authority import require_separate_directory
from cloud_memory_hub.storage_lease import StorageLease

CONTROL_RETRY_SECONDS = 60


class BackupRuntimeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _PendingStatus(BackupRuntimeError):
    def __init__(self, state):
        super().__init__("backup_control_unavailable")
        self.state = state


def _store_status(root, state):
    try:
        _write(root, "status.json", state)
    except Exception:
        if state["state"] == "running":
            state = {
                **state,
                "state": "interrupted",
                "error": "backup_interrupted",
                "finished_at": _now().isoformat(),
            }
        raise _PendingStatus(state) from None


def _now():
    return datetime.now(UTC)


def _timestamp(value):
    if not isinstance(value, str):
        raise BackupRuntimeError("backup_control_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise BackupRuntimeError("backup_control_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BackupRuntimeError("backup_control_invalid")
    return parsed


def _private(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise BackupRuntimeError("backup_control_unsafe")


def prepare_root(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(root, directory=True)


def _read(root, name):
    path = root / name
    if not path.exists() and not path.is_symlink():
        return None
    _private(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise BackupRuntimeError("backup_control_unsafe")
        content = stream.read(65537)
    if len(content) > 65536:
        raise BackupRuntimeError("backup_control_invalid")
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise BackupRuntimeError("backup_control_invalid") from None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise BackupRuntimeError("backup_control_invalid")
    return value


def _write(root, name, value):
    temporary = root / ("." + name + "." + uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _control(root):
    import fcntl

    prepare_root(root)
    path = root / ".backup-control.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _private(path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupRuntimeError("backup_control_busy") from None
        yield
    finally:
        os.close(descriptor)


def read_plan(root):
    value = _read(root, "schedule.json")
    if value is None:
        return {"schema_version": 1, "enabled": False}
    if (
        set(value) != {"schema_version", "enabled", "interval_minutes", "keep", "configured_at"}
        or type(value["enabled"]) is not bool
        or type(value["interval_minutes"]) is not int
        or not 1 <= value["interval_minutes"] <= 525600
        or type(value["keep"]) is not int
        or not 1 <= value["keep"] <= 365
    ):
        raise BackupRuntimeError("backup_schedule_invalid")
    _timestamp(value["configured_at"])
    return value


def configure(root, *, interval_minutes=1440, keep=7, enabled=True):
    if (
        type(interval_minutes) is not int
        or not 1 <= interval_minutes <= 525600
        or type(keep) is not int
        or not 1 <= keep <= 365
        or type(enabled) is not bool
    ):
        raise BackupRuntimeError("backup_schedule_invalid")
    value = {
        "schema_version": 1,
        "enabled": enabled,
        "interval_minutes": interval_minutes,
        "keep": keep,
        "configured_at": _now().isoformat(),
    }
    with _control(root):
        _write(root, "schedule.json", value)
    return value


def request_backup(root):
    with _control(root):
        current = _read(root, "request.json")
        state = _read(root, "status.json") or {}
        if current and current.get("request_id") != state.get("last_request_id"):
            raise BackupRuntimeError("backup_request_pending")
        value = {
            "schema_version": 1,
            "request_id": str(uuid4()),
            "requested_at": _now().isoformat(),
        }
        _write(root, "request.json", value)
        return value


def _request(root, state):
    request = _read(root, "request.json")
    if not request:
        return None
    try:
        valid = str(UUID(request["request_id"])) == request["request_id"]
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid or set(request) != {"schema_version", "request_id", "requested_at"}:
        raise BackupRuntimeError("backup_request_invalid")
    _timestamp(request["requested_at"])
    return request["request_id"] if request["request_id"] != state.get("last_request_id") else None


def read_state(root):
    state = _read(root, "status.json") or {"schema_version": 1, "state": "not_run"}
    fields = {
        "schema_version",
        "state",
        "last_attempt_at",
        "finished_at",
        "last_success_at",
        "last_backup_id",
        "last_file_count",
        "last_total_bytes",
        "last_request_id",
        "error",
        "retention_error",
    }
    if not set(state) <= fields or state.get("state") not in {
        "not_run",
        "running",
        "completed",
        "failed",
        "interrupted",
    }:
        raise BackupRuntimeError("backup_status_invalid")
    for name in ("last_attempt_at", "finished_at", "last_success_at"):
        if name in state:
            _timestamp(state[name])
    for name in ("last_file_count", "last_total_bytes"):
        if name in state and (type(state[name]) is not int or state[name] < 0):
            raise BackupRuntimeError("backup_status_invalid")
    if "last_backup_id" in state and (
        not isinstance(state["last_backup_id"], str)
        or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", state["last_backup_id"])
    ):
        raise BackupRuntimeError("backup_status_invalid")
    if state.get("error") not in {
        None,
        "backup_create_failed",
        "backup_service_stop_failed",
        "backup_interrupted",
    } or state.get("retention_error") not in {None, "backup_retention_failed"}:
        raise BackupRuntimeError("backup_status_invalid")
    if "last_request_id" in state:
        try:
            valid = str(UUID(state["last_request_id"])) == state["last_request_id"]
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise BackupRuntimeError("backup_status_invalid")
    return state


def next_due(plan, state):
    if not plan["enabled"]:
        return None
    start = _timestamp(plan["configured_at"])
    if state.get("last_attempt_at"):
        start = max(start, _timestamp(state["last_attempt_at"]))
    return start + timedelta(minutes=plan["interval_minutes"])


def status(root):
    prepare_root(root)
    plan = read_plan(root)
    state = read_state(root)
    due = next_due(plan, state)
    return {
        "schedule": plan,
        "execution": state,
        "next_due_at": due.isoformat() if due else None,
        "pending_request": _request(root, state),
        "supervisor_running": "not_checked",
    }


def _stop_child(child, *, timeout=35):
    if child.poll() is not None:
        return child.returncode == 0
    child.terminate()
    try:
        return child.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
        return False


def _perform(config, root, plan, state, request_id, backup):
    state = {
        **state,
        "schema_version": 1,
        "state": "running",
        "last_attempt_at": _now().isoformat(),
        "error": None,
        "retention_error": None,
    }
    if request_id:
        state["last_request_id"] = request_id
    _store_status(root, state)
    try:
        result = backup(config, root, revision=os.environ.get("CMH_REVISION", "development"))
        from cloud_memory_hub.backup_store import prune_backups

        state.update(
            state="completed",
            last_success_at=_now().isoformat(),
            last_backup_id=result["backup_id"],
            last_file_count=result["file_count"],
            last_total_bytes=result["total_bytes"],
        )
        try:
            retention = prune_backups(root, keep=plan.get("keep", 7))
            if retention.get("failures"):
                state["retention_error"] = "backup_retention_failed"
        except Exception:
            state["retention_error"] = "backup_retention_failed"
    except Exception:
        # Unexpected SDK/filesystem exceptions can contain original text or paths.
        state.update(state="failed", error="backup_create_failed")
    state["finished_at"] = _now().isoformat()
    _store_status(root, state)


def supervise(config, root, *, child_command=None, stop_event=None, poll_seconds=1, backup=None):
    require_separate_directory(root, config.data_dir, config.config_dir, config.governance_dir)
    prepare_root(root)
    stopped = stop_event or threading.Event()
    command = child_command or [sys.executable, "-m", "cloud_memory_hub", "serve"]
    if backup is None:
        from cloud_memory_hub.backup_store import create_backup

        backup = create_backup
    child = None
    control_retry_at = 0.0
    pending_status = None

    def pause_control():
        # The backup destination may be full/read-only, so a durable error cannot
        # be assumed writable. Keep serve available, retain evidence, and bound retries.
        print("cloud-memory-hub: backup_control_unavailable_retry_delayed", file=sys.stderr)
        return time.monotonic() + CONTROL_RETRY_SECONDS

    # Keep process ownership separate from create/prune's backup-operation lease.
    # flock is not reentrant for separately opened descriptors, even in one process.
    supervisor_lock = root / ".supervisor"
    supervisor_lock.mkdir(mode=0o700, exist_ok=True)
    with StorageLease(supervisor_lock):
        try:
            try:
                previous_state = read_state(root)
                if previous_state.get("state") == "running":
                    _store_status(
                        root,
                        {
                            **previous_state,
                            "state": "interrupted",
                            "error": "backup_interrupted",
                            "finished_at": _now().isoformat(),
                        },
                    )
            except Exception as error:
                if isinstance(error, _PendingStatus):
                    pending_status = error.state
                control_retry_at = pause_control()
            while not stopped.is_set():
                if child is not None and child.poll() is not None:
                    raise BackupRuntimeError("backup_service_exited")
                if time.monotonic() >= control_retry_at:
                    try:
                        if pending_status is not None:
                            _store_status(root, pending_status)
                            pending_status = None
                        plan = read_plan(root)
                        state = read_state(root)
                        requested = _request(root, state)
                        due = next_due(plan, state)
                        if requested or (due is not None and _now() >= due):
                            # On container restart an overdue job can run while
                            # already cold. Do not start and immediately TERM a
                            # Python child before Uvicorn installs its handlers.
                            graceful = child is None or _stop_child(child)
                            child = None
                            if stopped.is_set():
                                break
                            if graceful:
                                _perform(config, root, plan, state, requested, backup)
                            else:
                                failed = {
                                    **state,
                                    "schema_version": 1,
                                    "state": "failed",
                                    "last_attempt_at": _now().isoformat(),
                                    "finished_at": _now().isoformat(),
                                    "error": "backup_service_stop_failed",
                                }
                                if requested:
                                    failed["last_request_id"] = requested
                                _store_status(root, failed)
                    except Exception as error:
                        if isinstance(error, _PendingStatus):
                            pending_status = error.state
                        control_retry_at = pause_control()
                if child is None and not stopped.is_set():
                    child = subprocess.Popen(command)
                stopped.wait(poll_seconds)
        finally:
            if child is not None:
                _stop_child(child)


def serve_supervised(config, root):
    stopped = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stopped.set())
    try:
        supervise(config, root, stop_event=stopped)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Cloud Memory Hub 备份计划与维护状态")
    parser.add_argument("command", choices=["schedule", "disable", "request", "status", "list"])
    parser.add_argument(
        "--backup-dir", type=Path, default=Path(os.environ.get("CMH_BACKUP_DIR", "/backups"))
    )
    parser.add_argument("--every-minutes", type=int, default=1440)
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    try:
        if args.command in {"schedule", "disable"}:
            result = configure(
                args.backup_dir,
                interval_minutes=args.every_minutes,
                keep=args.keep,
                enabled=args.command == "schedule",
            )
        elif args.command == "request":
            result = request_backup(args.backup_dir)
        elif args.command == "list":
            from cloud_memory_hub.backup_store import list_backups

            result = list_backups(args.backup_dir)
        else:
            result = status(args.backup_dir)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        code = error.code if isinstance(error, BackupRuntimeError) else "backup_control_unavailable"
        print(json.dumps({"error": code}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
