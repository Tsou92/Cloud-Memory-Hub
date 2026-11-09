"""ADR-0040 installed/native diagnostic quiescence probe; no external model.

Real HTTP, SDK, storage, pipeline logger and purge; synthetic extraction/embedding
and explicit queue settlement. Existing JSONL body cleanup remains out of scope.
Sibling dependency: probe_original_archive.py (environment and identity setup).
The scenario is also called directly by Hub pytest; it never imports test modules.
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import mkdtemp
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from httpx import ASGITransport, AsyncClient
from hy_memory.agent.llm_provider import LLMProvider
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.data.cache_sqlite import SqliteCache
from hy_memory.pipelines import writer as native_writer
from hy_memory.utils import log_setup as native_logging
from hy_memory.utils.pipeline_log_writer import PipelineLogWriter
from hy_memory.utils.pipeline_observability import drain_pipeline_log_tasks
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import LocalHyEmbedService, _joined_thread
from one_memory.hy_runtime_config import set_diagnostic_log_level
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED

EXECUTE = "/hub/v1/governance/execute"


class SyntheticEmbedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


class DelayedDiagnostic:
    """Delay an original callback, without replacing its final persistence."""

    def __init__(self, sink):
        self.sink = sink
        self.marker = "Synthetic diagnostic selected orchard " + str(uuid4())
        self.entered = Event()
        self.release = Event()
        self.finished = Event()
        self.request_id = None

    def selected(self, kwargs):
        return kwargs.get("step") == "EXTRACT" and self.marker in kwargs.get("response", "")

    def wait(self, kwargs):
        self.request_id = kwargs["request_id"]
        self.entered.set()
        assert self.release.wait(15), "test did not release native diagnostic I/O"


def diagnostic_rows(sdk, request_id):
    with sqlite3.connect(sdk._cache._db_path) as db:
        return db.execute(
            "SELECT * FROM pipeline_logs_v2 WHERE request_id=? ORDER BY id", (request_id,)
        ).fetchall()


def jsonl_rows(config, request_id):
    rows = []
    root = config.data_dir / "hy-memory" / "governed-v2" / "logs"
    for path in root.glob("*/*.log"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get("request_id") == request_id:
                rows.append(row)
    return rows


async def settle_logs(sdk):
    await _joined_thread(lambda: sdk._loop_thread.run(drain_pipeline_log_tasks()))


async def write(app, web, headers, template, marker):
    event = {
        **template,
        **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
        "content": {"messages": [{"role": "user", "text": "Synthetic original " + marker}]},
    }
    reply = await web.post("/hub/v1/events", headers=headers, json=event)
    assert reply.status_code == 202, "diagnostic_event_rejected"
    job = reply.json()["receipt_id"]
    queue = app.core.state.memory_write_queue
    frozen = queue.payload_for(job)
    request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
    sdk = await _sdk_library_bridge(app.core)._get_client()

    async def extract(self, **kwargs):
        return AgentResult(
            success=True,
            extracted_info={"memory": [{"content": "Synthetic extracted fact " + marker}]},
            extract_raw_response=marker,
        )

    with patch.object(MemAgent, "process_add", extract):
        result = await _joined_thread(
            sdk.add,
            [turn.model_dump() for turn in request.turns],
            user_id=request.user_id,
            agent_id=request.agent_id,
            session_id=request.session_id,
            source=request.source_id,
            request_id=request.request_id,
            scene="chat",
        )
    assert result["success"] and result["memory_ids"]
    await app.core.state.cmh_lineage.observe(job, frozen)
    queue.settle(job, state=COMPLETED)
    return job, request, result, event


def _loggers():
    return [
        logging.getLogger(),
        *[
            item
            for item in tuple(logging.Logger.manager.loggerDict.values())
            if isinstance(item, logging.Logger)
        ],
    ]


@contextmanager
def isolated_logging():
    """Restore caller objects only after the enclosing SDK lifespan has ended."""
    before = {
        logger: (
            logger.level,
            tuple(logger.handlers),
            logger.propagate,
            logger.disabled,
            tuple(logger.filters),
        )
        for logger in _loggers()
    }
    # Include unattached caller handlers: attaching one later does not make it
    # ours to close. The native setup flag is separate from logger state.
    caller_handlers = {ref() for ref in tuple(logging._handlerList) if ref() is not None}
    configured = native_logging._logging_configured
    for logger in before:
        if logger.name == "hy_memory" or logger.name.startswith("hy_memory."):
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
    native_logging._logging_configured = False
    try:
        yield
    finally:
        created = {ref() for ref in tuple(logging._handlerList) if ref() is not None}
        for logger in _loggers():
            level, handlers, propagate, disabled, filters = before.get(
                logger, (logging.NOTSET, (), True, False, ())
            )
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
            for handler in handlers:
                logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
            logger.filters[:] = filters
        native_logging._logging_configured = configured
        for handler in created - caller_handlers:
            handler.close()


@contextmanager
def instrumentation(config, delay):
    original_jsonl = PipelineLogWriter.write_step
    original_trace = SqliteCache.store_pipeline_log
    model_attempts = []

    async def no_model(*args, **kwargs):
        model_attempts.append(True)
        raise AssertionError("diagnostic tests must not call an external model")

    def delayed_jsonl(self, **kwargs):
        selected = delay.sink == "jsonl" and delay.selected(kwargs)
        if selected:
            delay.wait(kwargs)
        try:
            return original_jsonl(self, **kwargs)
        finally:
            if selected:
                delay.finished.set()

    async def delayed_trace(self, *args, **kwargs):
        selected = delay.sink == "trace" and delay.selected(kwargs)
        if selected:
            await asyncio.to_thread(delay.wait, kwargs)
        try:
            return await original_trace(self, *args, **kwargs)
        finally:
            if selected:
                delay.finished.set()

    with ExitStack() as patches:
        # Track the original environment before the real configuration setter
        # changes it. The debug settings file belongs only to this test instance.
        patches.enter_context(patch.dict(os.environ))
        patches.enter_context(isolated_logging())
        os.environ["MEMORY_PIPELINE_TRACE_TIMEOUT_SECONDS"] = "15"
        set_diagnostic_log_level(config.data_dir, "debug")
        native_logging.setup_logging(
            log_dir=str(config.data_dir / "hy-memory" / "governed-v2" / "logs"),
            level=logging.DEBUG,
        )
        patches.enter_context(
            patch.object(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder())
        )
        patches.enter_context(patch.object(native_writer, "_RECONCILE_ENABLED", False))
        patches.enter_context(patch.object(LLMProvider, "complete", no_model))
        patches.enter_context(patch.object(LLMProvider, "complete_messages", no_model))
        patches.enter_context(patch.object(PipelineLogWriter, "write_step", delayed_jsonl))
        patches.enter_context(patch.object(SqliteCache, "store_pipeline_log", delayed_trace))
        try:
            yield
        finally:
            delay.release.set()
    assert model_attempts == []


async def setup_records(app, web, config, admin, headers, template, delay):
    sdk = await _sdk_library_bridge(app.core)._get_client()
    other = await write(app, web, headers, template, "unrelated retained harbour")
    await settle_logs(sdk)
    other_rows = diagnostic_rows(sdk, other[1].request_id)
    other_json = jsonl_rows(config, other[1].request_id)
    assert other_rows and other_json
    target = await write(app, web, headers, template, delay.marker)
    assert await asyncio.to_thread(delay.entered.wait, 5), "native diagnostic did not enqueue"
    assert delay.request_id == target[1].request_id
    body = {
        "scope_id": template["scope_id"],
        "action": "purge",
        "record_ids": ["cmhe:" + target[0]],
    }
    view = await web.post("/hub/v1/governance/preview", headers=admin, json={**body, "limit": 50})
    assert view.status_code == 200, "diagnostic_preview_failed"
    body.update(expected_view=view.json()["view_digest"], operation_id=str(uuid4()))
    return sdk, admin, other, other_rows, other_json, target, body


async def assert_purge_waits(app, task):
    async def admitted():
        while app.core.state.cmh_sdk_admission._owner is None and not task.done():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(admitted(), 5)
    done, _ = await asyncio.wait({task}, timeout=0.15)
    assert not done, "purge finished while native diagnostic I/O was still blocked"


async def verify_case(app, web, config, admin, headers, template, delay, *, interruption="none"):
    sdk, admin, other, other_rows, other_json, target, body = await setup_records(
        app, web, config, admin, headers, template, delay
    )
    executor = app.core.state.cmh_executor
    operations_before = executor.store.operations()
    head_before = executor.authority.journal.head(target[3]["scope_id"])
    if interruption == "timeout":
        from hy_memory.utils.pipeline_log_lifecycle import PipelineLogDrainTimeout

        # Invoke the actual strict API with a short deadline, without replacing
        # the HTTP barrier or its implementation with a simulated error.
        async with app.core.state.cmh_sdk_admission.exclusive():
            try:
                async with _sdk_library_bridge(app.core).quiesce_diagnostics(timeout=0.03):
                    raise AssertionError("strict_drain_entered_with_blocked_native_io")
            except PipelineLogDrainTimeout as error:
                assert str(error) == "pipeline_log_drain_timeout"
        assert not delay.finished.is_set()
        assert executor.store.operations() == operations_before
        assert executor.authority.journal.head(target[3]["scope_id"]) == head_before
        assert await _joined_thread(sdk.get_write_outcome, target[1].request_id)
    task = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
    try:
        await assert_purge_waits(app, task)
        assert not delay.finished.is_set()
        assert executor.store.operations() == operations_before
        assert executor.authority.journal.head(target[3]["scope_id"]) == head_before
        assert await _joined_thread(sdk.get_write_outcome, target[1].request_id)
        if interruption == "cancel":
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled_governance_request_returned_success")
            assert not delay.finished.is_set()
            assert executor.store.operations() == operations_before
            assert executor.authority.journal.head(target[3]["scope_id"]) == head_before
            # Cancellation restores admission but must retain real pending I/O.
            task = asyncio.create_task(web.post(EXECUTE, headers=admin, json=body))
            await assert_purge_waits(app, task)
        delay.release.set()
        reply = await asyncio.wait_for(task, 5)
        assert reply.status_code == 200, reply.text
        assert reply.json()["state"] == "completed"
        assert delay.finished.is_set()
        await settle_logs(sdk)
        assert diagnostic_rows(sdk, target[1].request_id) == []
        assert diagnostic_rows(sdk, other[1].request_id) == other_rows
        assert jsonl_rows(config, other[1].request_id) == other_json
        # ADR-0040 drains writes; it does not yet purge existing log files.
        assert any(delay.marker in str(row) for row in jsonl_rows(config, target[1].request_id))
        operations = executor.store.operations()
        head = executor.authority.journal.head(target[3]["scope_id"])
        assert len(operations) == len(operations_before) + 1
        again = await web.post(EXECUTE, headers=admin, json=body)
        assert again.status_code == 200, again.text
        assert again.json()["operation_id"] == reply.json()["operation_id"]
        assert executor.store.operations() == operations
        assert executor.authority.journal.head(target[3]["scope_id"]) == head
        assert diagnostic_rows(sdk, target[1].request_id) == []
    finally:
        delay.release.set()
        await asyncio.gather(task, return_exceptions=True)


def installed_modules():
    for name in (
        "hy_memory.client",
        "hy_memory.data.cache_sqlite",
        "hy_memory.utils.pipeline_observability",
        "hy_memory.utils.pipeline_log_lifecycle",
        "one_memory.hy_memory_bridge",
        "cloud_memory_hub.governance_execution",
    ):
        path = Path(importlib.import_module(name).__file__).resolve()
        assert "site-packages" in path.parts, "probe_requires_installed_packages"


async def phase(root, sink, *, interruption="none"):
    from probe_original_archive import environment
    from probe_original_archive import phase as archive_phase

    os.environ.update(environment(root))
    config = HubConfig.from_environment()
    delay = DelayedDiagnostic(sink)
    with instrumentation(config, delay):
        await archive_phase(root, "seed", accept_event=False)
        state = json.loads((root / "state.json").read_text())
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            try:
                async with AsyncClient(
                    transport=ASGITransport(app), base_url=config.public_origin
                ) as web:
                    await verify_case(
                        app,
                        web,
                        config,
                        {"Authorization": "Bearer " + config.admin_token},
                        {"Authorization": "Bearer " + state["credential"]},
                        state["event"],
                        delay,
                        interruption=interruption,
                    )
            finally:
                delay.release.set()
    print(json.dumps({"sink": sink, "interruption": interruption, "passed": True}))


def main():
    from probe_original_archive import environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--phase", choices=("jsonl", "trace"))
    parser.add_argument("--interruption", choices=("none", "cancel", "timeout"), default="none")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.installed:
        installed_modules()
    if args.phase:
        if args.root is None:
            parser.error("--phase requires --root")
        asyncio.run(phase(args.root, args.phase, interruption=args.interruption))
        return
    if args.root or args.interruption != "none":
        parser.error("--root and --interruption require --phase")
    root = Path(mkdtemp(prefix="cmh-diagnostic-quiescence-"))
    try:
        for sink in ("jsonl", "trace"):
            for interruption in ("none", "cancel", "timeout"):
                instance = root / f"{sink}-{interruption}"
                instance.mkdir(mode=0o700)
                with (instance / "init.log").open("w") as log:
                    subprocess.run(
                        [sys.executable, "-m", "cloud_memory_hub", "init"],
                        env=environment(instance),
                        stdout=log,
                        stderr=log,
                        check=True,
                        timeout=60,
                    )
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--phase",
                    sink,
                    "--root",
                    str(instance),
                    "--interruption",
                    interruption,
                ]
                if args.installed:
                    command.append("--installed")
                with (instance / "phase.log").open("w") as log:
                    subprocess.run(command, stdout=log, stderr=log, timeout=90, check=True)
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError(
            f"diagnostic_quiescence_probe_failed; private evidence retained at {root}"
        ) from None
    else:
        shutil.rmtree(root)
    print(
        "Native JSONL/trace wait, cancellation/timeout retry and purge passed; "
        "file cleanup excluded."
    )


if __name__ == "__main__":
    main()
