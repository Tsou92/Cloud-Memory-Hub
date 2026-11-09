"""Hub routing ownership and cancellation encompass real file-handler repair."""

import asyncio
import logging
import os
import threading

import pytest
from cloud_memory_hub import diagnostics
from hy_memory.utils.diagnostic_policy import DiagnosticPolicyError
from hy_memory.utils.file_log_lifecycle import _create_sdk_file_handler
from one_memory.hy_memory_bridge import _joined_thread


def test_hub_routing_busy_and_same_thread_reentry_refuse_without_mutation(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def held():
        with diagnostics._LOCK:
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=held)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(DiagnosticPolicyError, match="logging_busy"):
            with diagnostics.diagnostic_file_maintenance(roots=(tmp_path,)):
                pytest.fail("busy routing lock was acquired")
    finally:
        release.set()
        thread.join(5)
    before = tuple(diagnostics._detached_handlers)
    with diagnostics.diagnostic_file_maintenance(roots=(tmp_path,)):
        with pytest.raises(DiagnosticPolicyError, match="logging_busy"):
            diagnostics.initialize_diagnostics()
        with pytest.raises(DiagnosticPolicyError, match="logging_busy"):
            with diagnostics.diagnostic_file_maintenance(roots=(tmp_path,)):
                pytest.fail("maintenance reentered")
    assert tuple(diagnostics._detached_handlers) == before


def test_hub_retained_caller_descriptor_is_refused_without_closing(tmp_path):
    path = tmp_path / "hy_memory.log"
    handler = logging.FileHandler(path)
    diagnostics._detached_handlers.add(handler)
    try:
        with pytest.raises(DiagnosticPolicyError, match="unsupported"):
            with diagnostics.diagnostic_file_maintenance(roots=(tmp_path,)):
                pytest.fail("caller file handler was silently adopted")
        assert not handler.stream.closed
        handler.emit(
            logging.LogRecord("synthetic", logging.INFO, __file__, 1, "retained", (), None)
        )
        assert path.read_text() == "retained\n"
    finally:
        diagnostics._detached_handlers.discard(handler)
        handler.close()


def test_cancelled_joined_thread_keeps_routing_and_native_io_exclusion_until_rebind(tmp_path):
    path = tmp_path / "hy_memory.log"
    path.write_bytes(b"synthetic old\n")
    replacement = tmp_path / "retained.tmp"
    replacement.write_bytes(b"synthetic retained\n")
    handler = _create_sdk_file_handler(filename=str(path), when="midnight", encoding="utf-8")
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    before = path.stat()

    def rewrite():
        with diagnostics.diagnostic_file_maintenance(roots=(tmp_path,)) as gate:
            gate.require_bound(((path, before.st_dev, before.st_ino),))
            streams = gate.suspend(path)
            entered.set()
            assert release.wait(5)
            os.replace(replacement, path)
            info = path.stat()
            supplied = {
                item.token: path.open(item.mode, encoding=item.encoding, errors=item.errors)
                for item in streams
            }
            gate.adopt(path, supplied, identity=(info.st_dev, info.st_ino))
            gate.verify_bound(((path, info.st_dev, info.st_ino),))
        completed.set()

    async def scenario():
        task = asyncio.create_task(_joined_thread(rewrite))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not completed.is_set()
        with pytest.raises(DiagnosticPolicyError, match="logging_busy"):
            with diagnostics.diagnostic_file_maintenance(roots=(tmp_path,)):
                pytest.fail("cancel released the routing boundary early")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()

    try:
        asyncio.run(scenario())
        assert os.fstat(handler.stream.fileno()).st_ino == path.stat().st_ino
        handler.emit(logging.LogRecord("synthetic", logging.INFO, __file__, 1, "after", (), None))
        assert path.read_bytes() == b"synthetic retained\nafter\n"
    finally:
        release.set()
        handler.close()
