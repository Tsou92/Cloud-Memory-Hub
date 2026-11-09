"""ADR-0041: actual installed logging, Core files and native SQLite/JSONL sinks.

Only synthetic values, no model calls, no user configuration. The separate
diagnostic-quiescence probe covers HTTP/SDK business writes and governance.
"""

import argparse
import asyncio
import gc
import importlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import warnings
import weakref
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace
from uuid import uuid4

MARKER = "Synthetic confidential diagnostic orchard must never appear in output"


class Unrenderable:
    calls = 0

    def __str__(self):
        type(self).calls += 1
        raise AssertionError("diagnostic_rendered_untrusted_object")

    __repr__ = __str__


def installed_modules():
    for name in (
        "cloud_memory_hub.diagnostics",
        "hy_memory.utils.diagnostic_policy",
        "hy_memory.data.cache_sqlite",
        "hy_memory.utils.pipeline_log_writer",
        "one_memory.diagnostic_log",
        "one_memory.exception_reporting",
        "one_memory.log_file",
    ):
        assert "site-packages" in Path(importlib.import_module(name).__file__).resolve().parts


def logging_phase(root):
    from cloud_memory_hub.diagnostics import (
        initialize_diagnostics,
        install_process_exception_hooks,
    )
    from hy_memory.utils import log_setup

    caller_path = root / "caller.log"
    handler = logging.FileHandler(caller_path)
    caller_fd = handler.stream.fileno()
    caller = weakref.ref(handler)
    logging.getLogger().addHandler(handler)
    del handler  # The logger is the only owner before Hub takes over.
    log_setup.setup_logging(log_dir=str(root / "sdk"), level=logging.INFO)
    sdk_handlers = tuple(logging.getLogger("hy_memory").handlers)
    assert len(sdk_handlers) == 3
    initialize_diagnostics()
    initialize_diagnostics()
    log_setup.setup_logging(log_dir=str(root / "unused"), level=logging.DEBUG)
    assert tuple(logging.getLogger("hy_memory").handlers) == sdk_handlers
    gc.collect()
    assert caller() is not None and os.fstat(caller_fd)
    assert caller() not in logging.getLogger().handlers
    previous_hooks = sys.excepthook, threading.excepthook, sys.unraisablehook
    install_process_exception_hooks()

    from hy_memory.utils.diagnostic_policy import StrictDiagnosticFormatter
    from one_memory.diagnostic_log import (
        configure_diagnostics,
        flush_diagnostics,
        reset_diagnostics,
    )
    from one_memory.exception_reporting import report_exception
    from one_memory.hy_runtime_config import set_diagnostic_log_level
    from one_memory.log_file import start_logging_to_file

    start_logging_to_file(root)
    os.environ["ONE_MEMORY_DIAGNOSTICS"] = "1"
    configure_diagnostics(root)
    try:
        for level in ("debug", "info", "debug"):
            set_diagnostic_log_level(root, level)
            for name in (
                "hy_memory.client",
                "one_memory",
                "openai",
                "httpx",
                "uvicorn.error",
                MARKER,
            ):
                logging.getLogger(name).error(MARKER, exc_info=RuntimeError(MARKER))
                logging.getLogger(name).error(Unrenderable())
            report_exception("core.loop", "core.loop.task_exception", RuntimeError(MARKER))
        warnings.warn(MARKER, stacklevel=1)
        error = RuntimeError(MARKER)
        sys.excepthook(type(error), error, None)
        arguments = SimpleNamespace(exc_type=type(error), exc_value=error, exc_traceback=None)
        threading.excepthook(arguments)
        sys.unraisablehook(arguments)
        sys.excepthook, threading.excepthook, sys.unraisablehook = previous_hooks

        class BadStream:
            def write(self, value):
                raise OSError(MARKER)

            def flush(self):
                raise OSError(MARKER)

        # Safe formatting alone is insufficient: exercise actual emit failure.
        broken = logging.StreamHandler(BadStream())
        broken.setFormatter(StrictDiagnosticFormatter())
        record = logging.LogRecord("hy_memory", logging.ERROR, __file__, 1, MARKER, (), None)
        broken.emit(record)
        assert flush_diagnostics(), "core_diagnostic_not_flushed"
        for handler in (*sdk_handlers, *logging.getLogger().handlers):
            handler.flush()
        assert caller_path.read_bytes() == b""
        for path in (root / "sdk" / "hy_memory.log", root / "logs" / "core.log"):
            rows = [json.loads(line) for line in path.read_bytes().split(b"\n") if line]
            assert rows and all(row["content_omitted"] is True for row in rows)
            assert any(row.get("error_type") == "RuntimeError" for row in rows)
        records = (root / "logs" / "diagnostic-core.jsonl").read_bytes()
        rows = [json.loads(line) for line in records.split(b"\n") if line]
        assert rows and any(row.get("error_type") == "RuntimeError" for row in rows)
        assert all(row["content_omitted"] is True for row in rows)
        assert Unrenderable.calls == 0
        assert all(
            MARKER.encode() not in path.read_bytes() for path in root.rglob("*") if path.is_file()
        )
    finally:
        sys.excepthook, threading.excepthook, sys.unraisablehook = previous_hooks
        reset_diagnostics()
    return {"phase": "logging", "passed": True, "caller_fd_preserved": True}


async def persistence_phase(root):
    from cloud_memory_hub.diagnostics import initialize_diagnostics

    initialize_diagnostics()
    from hy_memory.data.cache_sqlite import SqliteCache
    from hy_memory.utils.pipeline_log_writer import PipelineLogWriter
    from one_memory.hy_runtime_config import set_diagnostic_log_level

    path = root / "cache.sqlite3"
    cache = SqliteCache(SimpleNamespace(cache=SimpleNamespace(db_path=str(path))))
    await cache.initialize()
    writer = PipelineLogWriter(str(root / "pipeline"))
    request_id = "cmh2:" + ":".join(str(uuid4()) for _ in range(4))
    fields = {
        "request_id": request_id,
        "user_id": MARKER,
        "agent_id": MARKER,
        "session_id": MARKER,
        "step": "EXTRACT",
        "prompt": MARKER,
        "response": "<redacted len=" + MARKER + ">",
        "parsed": json.dumps({MARKER: MARKER, "reason": MARKER, "owner_id": MARKER, "count": 2}),
        "memory_ids": [str(uuid4()), MARKER],
        "elapsed_ms": 12.5,
        "prompt_tokens": 5,
        "completion_tokens": 2,
    }
    try:
        for level in ("debug", "info", "debug"):
            set_diagnostic_log_level(root, level)
            # Even a subsequent environment change cannot downgrade the policy.
            os.environ["MEMORY_LOG_LLM_CONTENT"] = "true"
            os.environ["MEMORY_MEMORY_OPERATIONS_ENABLED"] = "true"
            assert await cache.store_pipeline_log(**fields)
            writer.write_step(**fields)
            assert await cache.store_memory_operation(
                request_id=request_id,
                user_id=MARKER,
                agent_id=MARKER,
                op="ADD",
                memory_id=str(uuid4()),
                content=MARKER,
                layer="l2_semantic",
                reason=MARKER,
            )
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT request_id,step,prompt,response,parsed,elapsed_ms,"
                "prompt_tokens,completion_tokens "
                "FROM pipeline_logs_v2"
            ).fetchall()
            assert len(rows) == 3
            for row in rows:
                assert row[:4] == (request_id, "EXTRACT", "", "")
                assert json.loads(row[4])["count"] == 2
                assert json.loads(row[4])["content_omitted"] is True
                assert row[5:] == (12.5, 5, 2)
            operations = db.execute("SELECT * FROM memory_operations_v2").fetchall()
            assert len(operations) == 3
            assert MARKER not in json.dumps(operations)
        files = list((root / "pipeline").glob("*/*.log"))
        assert len(files) == 1
        rows = [json.loads(line) for line in files[0].read_bytes().split(b"\n") if line]
        assert len(rows) == 3
        assert all(row["request_id"] == request_id and row["step"] == "EXTRACT" for row in rows)
        assert all(row["tokens"] == 7 and row["elapsed_ms"] == 12.5 for row in rows)
        assert all(not row["prompt"] and not row["content"] for row in rows)
    finally:
        writer.close()
        await cache.close()
    assert all(
        MARKER.encode() not in item.read_bytes() for item in root.rglob("*") if item.is_file()
    )
    return {"phase": "persistence", "passed": True, "request_id_preserved": True}


async def server_phase(root):
    from cloud_memory_hub.diagnostics import initialize_diagnostics

    initialize_diagnostics()
    import httpx
    import uvicorn

    async def failing_application(scope, receive, send):
        raise RuntimeError(MARKER)

    server = uvicorn.Server(
        uvicorn.Config(failing_application, log_config=None, access_log=False, lifespan="off")
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        port = sock.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[sock]))

        async def started():
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("diagnostic_server_exited_before_start")
                await asyncio.sleep(0.005)

        try:
            await asyncio.wait_for(started(), 5)
            async with httpx.AsyncClient(trust_env=False) as client:
                reply = await client.get(f"http://127.0.0.1:{port}/synthetic", timeout=5)
            assert reply.status_code == 500
            assert MARKER not in reply.text
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
    return {"phase": "server", "passed": True, "http_500": True}


def run_phase(root, phase, *, installed=False):
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    os.environ["MEMORY_DATA_DIR"] = str(root / "native")
    os.environ["MEMORY_LOG_DIR"] = str(root / "native" / "logs")
    if installed:
        installed_modules()
    if phase == "logging":
        return logging_phase(root)
    if phase == "server":
        return asyncio.run(server_phase(root))
    return asyncio.run(persistence_phase(root))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--phase", choices=("logging", "persistence", "server"))
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.phase:
        if args.root is None:
            parser.error("--phase requires --root")
        print(json.dumps(run_phase(args.root, args.phase, installed=args.installed)))
        return
    if args.root:
        parser.error("--root requires --phase")
    root = Path(mkdtemp(prefix="cmh-diagnostic-content-"))
    try:
        for phase in ("logging", "persistence", "server"):
            destination = root / phase
            destination.mkdir(mode=0o700)
            command = [sys.executable, __file__, "--phase", phase, "--root", str(destination)]
            if args.installed:
                command.append("--installed")
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
            assert MARKER not in result.stdout + result.stderr
            summary = json.loads(result.stdout.splitlines()[-1])
            assert summary["passed"] is True
            print(json.dumps(summary))
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
