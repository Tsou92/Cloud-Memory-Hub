"""ADR-0046: synthetic native Core writer and file-handler maintenance probe.

Every scenario runs in a fresh process. No external models, Docker, real logs,
or daily configuration; the real native queue, writer and file gate are used.
"""

import argparse
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

SCENARIOS = (
    "queue_timeout",
    "error_logging",
    "strict_overflow",
    "reset_retry",
    "configure_retry",
    "handler_rebind",
    "handler_failure",
    "constructor_race",
    "unknown_handler",
    "failed_domain_close",
    "hub_combined",
    "multiple_writers",
    "resume_thread_failure",
    "pause_all_before_start",
)


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("native_probe_wait_timeout")
        time.sleep(0.002)


def write(log, body="synthetic diagnostic body"):
    log.write(
        level="info",
        component="core.lifecycle",
        event="core.started",
        outcome="ok",
        details={"reason": body},
    )


def files(root):
    return tuple(
        (path, path.stat().st_dev, path.stat().st_ino)
        for path in sorted(root.glob("*"))
        if path.is_file()
    )


def verify(lease, root):
    observed = files(root)
    lease.require_bound(observed)
    lease.verify_bound(observed)


def guarded_thread(call):
    errors = []

    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def join(thread, errors):
    thread.join(8)
    assert not thread.is_alive(), "native_probe_thread_stuck"
    if errors:
        raise errors[0]


def replace(lease, path, payload, *, observed=None):
    before = path.stat()
    observed = observed or ((path, before.st_dev, before.st_ino),)
    lease.require_bound(observed)
    requests = lease.suspend(path)
    stage = path.with_name(path.name + ".synthetic-stage")
    stage.write_bytes(payload)
    os.replace(stage, path)
    after = path.stat()
    streams = {}
    try:
        for request in requests:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            streams[request.token] = os.fdopen(
                descriptor, "a", encoding=request.encoding, errors=request.errors
            )
        lease.adopt(path, streams, identity=(after.st_dev, after.st_ino))
    except BaseException:
        for stream in streams.values():
            stream.close()
        raise
    lease.verify_bound(tuple((p, p.stat().st_dev, p.stat().st_ino) for p, *_ in observed))


def queue_timeout(root):
    from one_memory import diagnostic_log as module

    log = module.DiagnosticLog(root)
    entered, release = threading.Event(), threading.Event()
    original = log._append

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    log._append = blocked
    write(log, "before timeout")
    assert entered.wait(5)
    try:
        with module.core_diagnostic_maintenance(roots=(root / "logs",), timeout=0.01):
            raise AssertionError("unfinished_native_writer_accepted")
    except module.DiagnosticMaintenanceError as error:
        assert str(error) == "diagnostic_core_maintenance_timeout"
    assert log._paused and log._writer.is_alive()
    assert log in module._live_diagnostic_logs
    write(log, "MUST_NOT_ENQUEUE_DURING_PAUSE")
    assert not log._queue
    try:
        module.DiagnosticLog(root)
    except module.DiagnosticMaintenanceError:
        pass
    else:
        raise AssertionError("new_writer_bypassed_failed_pause")
    release.set()
    with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
        assert not log._writer.is_alive()
        verify(lease, root / "logs")
    write(log, "after retry")
    assert log.close()
    body = log.path.read_text()
    assert "before timeout" in body and "after retry" in body
    assert "MUST_NOT_ENQUEUE_DURING_PAUSE" not in body


def error_logging(root):
    from one_memory import diagnostic_log as module
    from one_memory.log_file import start_logging_to_file

    destination = start_logging_to_file(root)
    log = module.DiagnosticLog(root)
    entered, release = threading.Event(), threading.Event()
    original = log._write_batch
    attempts = []

    def blocked(batch):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            logging.getLogger("one_memory.synthetic").error("synthetic writer retry")
            raise OSError("synthetic write interruption")
        return original(batch)

    log._write_batch = blocked
    write(log)
    assert entered.wait(5)

    def maintain():
        with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
            verify(lease, root / "logs")

    thread, errors = guarded_thread(maintain)
    wait_until(lambda: log._paused)
    release.set()
    join(thread, errors)
    assert log.close()
    assert len(attempts) == 2
    assert "synthetic writer retry" in destination.read_text()


def strict_overflow(root):
    from hy_memory.utils import diagnostic_policy as policy
    from one_memory import diagnostic_log as module

    policy.enable_strict_diagnostics()
    log = module.DiagnosticLog(root)
    module.MAX_QUEUED_EVENTS = 1
    entered, release = threading.Event(), threading.Event()
    original = log._append

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    log._append = blocked
    write(log)
    assert entered.wait(5)
    write(log)
    write(log)
    assert log._dropped == 1
    release.set()
    assert log.flush()
    before = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert any(record["event"] == "diagnostic.events_dropped" for record in before)
    assert all(record.get("diagnostic_policy") == "strict-v1" for record in before)
    with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
        verify(lease, root / "logs")
        write(log, "not queued")
    assert log.close()
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    dropped = [record for record in records if record["event"] == "diagnostic.events_dropped"]
    assert dropped and sum(record["details"]["count"] for record in dropped) == 2
    assert all(record["diagnostic_policy"] == "strict-v1" for record in records)
    assert all(record["content_omitted"] is True for record in records)
    assert "synthetic diagnostic body" not in log.path.read_text()


def close_retry(root, *, configure):
    from one_memory import diagnostic_log as module

    os.environ["ONE_MEMORY_DIAGNOSTICS"] = "1"
    module.configure_diagnostics(root)
    previous = module._active
    entered, release = threading.Event(), threading.Event()
    original_append, original_close = previous._append, previous.close

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original_append(*args)

    previous._append = blocked
    previous.close = lambda: original_close(timeout=0.01)
    write(previous)
    assert entered.wait(5)
    action = (
        (lambda: module.configure_diagnostics(root / "new"))
        if configure
        else module.reset_diagnostics
    )
    try:
        action()
    except module.DiagnosticMaintenanceError as error:
        assert str(error) == "diagnostic_core_close_incomplete"
    else:
        raise AssertionError("incomplete_close_committed")
    assert module._active is previous
    assert previous in module._live_diagnostic_logs and previous._writer.is_alive()
    assert not (root / "new").exists()
    release.set()
    wait_until(lambda: not previous._writer.is_alive())
    action()
    assert module._active is not previous
    assert previous not in module._live_diagnostic_logs
    assert previous.path.is_file()
    module.reset_diagnostics()


def handler_rebind(root, *, fail=False):
    # Construct Core before importing the SDK: legacy standalone use is supported.
    from one_memory import diagnostic_log as module
    from one_memory.log_file import start_logging_to_file

    path = start_logging_to_file(root)
    logger = logging.getLogger("one_memory.synthetic")
    logger.warning("synthetic old body")
    handler = next(
        h for h in logging.getLogger().handlers if getattr(h, "baseFilename", "") == str(path)
    )
    original_inode = path.stat().st_ino
    if fail:
        try:
            with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
                lease.require_bound(files(root / "logs"))
                lease.suspend(path)
                raise OSError("synthetic replacement failure")
        except OSError:
            pass
        logger.warning("MUST_NOT_REOPEN_FAILED_HANDLER")
        assert "MUST_NOT_REOPEN" not in path.read_text()
    with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
        replace(lease, path, b"retained synthetic metadata\n")
    assert handler.stream is not None and not handler.stream.closed
    assert path.stat().st_ino != original_inode
    logger.warning("synthetic after maintenance")
    assert "synthetic old body" not in path.read_text()
    assert "synthetic after maintenance" in path.read_text()
    handler.maxBytes = 100
    logger.warning("x" * 150)
    assert path.with_name("core.log.1").exists()


def constructor_race(root):
    from hy_memory.utils import diagnostic_policy as policy
    from one_memory import diagnostic_log as module

    entered, release = threading.Event(), threading.Event()
    original = module._register_diagnostic_log
    created = []

    def blocked(log):
        assert hasattr(log, "_ready") and hasattr(log, "_writer")
        entered.set()
        assert release.wait(5)
        original(log)

    module._register_diagnostic_log = blocked
    thread, errors = guarded_thread(
        lambda: created.append(module.DiagnosticLog(root, service="old"))
    )
    assert entered.wait(5)
    assert not module._live_diagnostic_logs
    policy.enable_strict_diagnostics()
    release.set()
    join(thread, errors)
    log = created[0]
    assert log.service == "core" and log in module._live_diagnostic_logs
    with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
        for action in (module.reset_diagnostics, lambda: module.configure_diagnostics(root)):
            try:
                action()
            except module.DiagnosticMaintenanceError:
                pass
            else:
                raise AssertionError("configuration_reentered_maintenance")
        lease.require_bound(())
        lease.verify_bound(())
    assert log.close()


def unknown_handler(root):
    from logging.handlers import RotatingFileHandler

    from one_memory.diagnostic_log import core_diagnostic_maintenance

    path = root / "core.log"
    handler = RotatingFileHandler(path)
    handler._one_memory_diagnostic_handler = True
    logging.getLogger().addHandler(handler)
    try:
        with core_diagnostic_maintenance(roots=(root,)):
            raise AssertionError("unknown_handler_accepted")
    except RuntimeError as error:
        assert str(error) == "diagnostic_file_logging_unsupported"


def failed_domain_close(root):
    from one_memory import diagnostic_log as module
    from one_memory.log_file import start_logging_to_file

    os.environ["ONE_MEMORY_DIAGNOSTICS"] = "1"
    module.configure_diagnostics(root)
    log = module._active
    write(log)
    assert log.flush()
    try:
        with module.core_diagnostic_maintenance(roots=(root / "logs",)):
            raise OSError("synthetic maintenance interruption")
    except OSError:
        pass
    assert log._paused and log.close()
    assert log not in module._live_diagnostic_logs
    before = log.path.read_bytes()
    for action in (
        lambda: module.configure_diagnostics(root),
        lambda: module.DiagnosticLog(root),
        lambda: start_logging_to_file(root),
    ):
        try:
            action()
        except module.DiagnosticMaintenanceError:
            pass
        else:
            raise AssertionError("closed_writer_erased_failed_domain_fence")
    with module.core_diagnostic_maintenance(roots=(root / "other",)) as lease:
        lease.require_bound(())
        lease.verify_bound(())
    assert module._pending_maintenance_roots == {root / "logs"}
    assert log.path.read_bytes() == before
    with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
        verify(lease, root / "logs")
    assert not module._pending_maintenance_roots
    module.configure_diagnostics(root)
    write(module._active, "synthetic after verified recovery")
    module.reset_diagnostics()
    assert "synthetic after verified recovery" in log.path.read_text()


def hub_combined(root):
    from cloud_memory_hub.diagnostics import diagnostic_file_maintenance, initialize_diagnostics
    from hy_memory.utils.log_setup import setup_logging
    from one_memory import diagnostic_log as module
    from one_memory.log_file import start_logging_to_file

    initialize_diagnostics()
    setup_logging(log_dir=str(root / "sdk"), level=logging.INFO)
    core_path = start_logging_to_file(root / "core")
    sdk_path = root / "sdk" / "hy_memory.log"
    logging.getLogger("hy_memory.synthetic").warning("synthetic sdk body")
    logging.getLogger("one_memory.synthetic").warning("synthetic core body")
    log = module.DiagnosticLog(root / "core")
    entered, release, yielded = threading.Event(), threading.Event(), threading.Event()
    original = log._append

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    log._append = blocked
    write(log)
    assert entered.wait(5)
    roots = (root / "core" / "logs", root / "sdk")
    before = (core_path.stat().st_ino, sdk_path.stat().st_ino)

    def maintain():
        with diagnostic_file_maintenance(roots=roots) as lease:
            yielded.set()
            assert not log._writer.is_alive()
            for path in (core_path, sdk_path):
                observed = files(roots[0]) + files(roots[1])
                replace(lease, path, path.read_bytes(), observed=observed)

    thread, errors = guarded_thread(maintain)
    wait_until(lambda: log._paused or yielded.is_set() or bool(errors))
    assert log._paused and not yielded.is_set(), "hub_wrapper_did_not_join_core_writer"
    release.set()
    join(thread, errors)
    assert before != (core_path.stat().st_ino, sdk_path.stat().st_ino)
    sizes = (core_path.stat().st_size, sdk_path.stat().st_size)
    logging.getLogger("hy_memory.synthetic").warning("synthetic after sdk")
    logging.getLogger("one_memory.synthetic").warning("synthetic after core")
    write(log)
    assert log.close()
    assert core_path.stat().st_size > sizes[0] and sdk_path.stat().st_size > sizes[1]
    for path in (*roots[0].glob("*"), *roots[1].glob("*")):
        if path.is_file():
            assert "synthetic" not in path.read_text()


def multiple_writers(root):
    from one_memory import diagnostic_log as module

    logs = [module.DiagnosticLog(root / name) for name in ("first", "second")]
    release = threading.Event()
    starts = [threading.Event(), threading.Event()]
    for log, started in zip(logs, starts, strict=True):
        original = log._append

        def blocked(*args, started=started, original=original):
            started.set()
            assert release.wait(5)
            return original(*args)

        log._append = blocked
        write(log)
    assert all(started.wait(5) for started in starts)
    try:
        with module.core_diagnostic_maintenance(roots=(root,), timeout=0.01):
            raise AssertionError("live_writer_accepted")
    except module.DiagnosticMaintenanceError as error:
        assert str(error) == "diagnostic_core_maintenance_timeout"
    assert all(log._paused and log._writer.is_alive() for log in logs)
    release.set()
    with module.core_diagnostic_maintenance(roots=(root,)) as lease:
        assert all(not log._writer.is_alive() for log in logs)
        observed = files(logs[0].path.parent) + files(logs[1].path.parent)
        lease.require_bound(observed)
        lease.verify_bound(observed)
    assert all(not log._paused and log.close() for log in logs)


def resume_thread_failure(root):
    from hy_memory.utils import diagnostic_policy as policy
    from one_memory import diagnostic_log as module

    policy.enable_strict_diagnostics()
    log = module.DiagnosticLog(root)
    write(log)
    assert log.flush()
    original = log._start_writer
    attempts = []

    def unavailable():
        attempts.append(1)
        if len(attempts) <= 2:
            raise RuntimeError("synthetic thread unavailable")
        return original()

    with module.core_diagnostic_maintenance(roots=(root / "logs",)) as lease:
        verify(lease, root / "logs")
        write(log)
        log._start_writer = unavailable
    assert not log._paused and log._writer is None and log._dropped == 1
    assert not log.close()
    assert log in module._live_diagnostic_logs and log._dropped == 1
    assert log.close()
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert sum(item.get("details", {}).get("count", 0) for item in records) == 1


def pause_all_before_start(root):
    from one_memory import diagnostic_log as module

    first = module.DiagnosticLog(root / "first")
    second = module.DiagnosticLog(root / "second")
    original = first._start_writer

    def unavailable():
        raise RuntimeError("synthetic thread unavailable")

    first._start_writer = unavailable
    write(first)
    assert first._dropped == 1 and first._writer is None
    write(second)
    assert second.flush()
    before = second.path.read_bytes()
    try:
        with module.core_diagnostic_maintenance(roots=(root,)):
            raise AssertionError("failed_start_accepted")
    except module.DiagnosticMaintenanceError as error:
        assert str(error) == "diagnostic_core_maintenance_failed"
    assert first._paused and second._paused
    write(second, "MUST_NOT_WRITE_AFTER_OTHER_WRITER_FAILED")
    assert second.flush()
    assert second.path.read_bytes() == before
    first._start_writer = original
    with module.core_diagnostic_maintenance(roots=(root,)) as lease:
        observed = files(first.path.parent) + files(second.path.parent)
        lease.require_bound(observed)
        lease.verify_bound(observed)
    assert first.close() and second.close()
    assert "MUST_NOT_WRITE" not in second.path.read_text()


def run(name, root):
    if name == "reset_retry":
        return close_retry(root, configure=False)
    if name == "configure_retry":
        return close_retry(root, configure=True)
    if name == "handler_failure":
        return handler_rebind(root, fail=True)
    return globals()[name](root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    if args.installed:
        for name in (
            "cloud_memory_hub.diagnostics",
            "one_memory.diagnostic_log",
            "one_memory.log_file",
            "hy_memory.utils.file_log_lifecycle",
        ):
            assert "site-packages" in Path(importlib.import_module(name).__file__).resolve().parts
    if args.scenario:
        with TemporaryDirectory(prefix="cmh-core-maintenance-") as directory:
            run(args.scenario, Path(directory).resolve())
        print(json.dumps({"scenario": args.scenario, "status": "passed"}))
        return
    for name in SCENARIOS:
        command = [sys.executable, str(Path(__file__).resolve()), "--scenario", name]
        if args.installed:
            command.append("--installed")
        subprocess.run(command, check=True, timeout=40)


if __name__ == "__main__":
    main()
