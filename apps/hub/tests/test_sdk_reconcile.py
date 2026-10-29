import asyncio
import sqlite3
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub import sdk_reconcile
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.remote_ingest import write_request
from cloud_memory_hub.sdk_admission import SdkAdmission
from cloud_memory_hub.sdk_productivity import existing_database, productivity_identities
from cloud_memory_hub.sdk_reconcile import SdkReconciler, proven_receipt
from httpx import ASGITransport, AsyncClient
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, WorkingFact
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.imports.models import EvidenceContentKind
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import (
    COMPLETED,
    UNKNOWN,
    DurableSdkReceipt,
    DurableWriteQueue,
)
from one_memory.memory_tasks import NON_RETRYABLE_POST_WRITE_CODES
from test_remote_ingest import config_for, provision


def request_for(*, extracted=False):
    event = EventEnvelope.model_validate(
        {
            **{
                key: str(uuid4())
                for key in (
                    "instance_id",
                    "device_id",
                    "source_id",
                    "scope_id",
                    "event_id",
                    "conversation_id",
                    "record_id",
                )
            },
            "revision": 1,
            "kind": "conversation_turn",
            "occurred_at": "2026-10-26T08:00:00Z",
            "content": {"messages": [{"role": "user", "text": "Synthetic queue recovery."}]},
        }
    )
    request = write_request(event)
    if extracted:
        request = request.model_copy(
            update={
                "input_records": tuple(
                    row.model_copy(update={"content_kind": EvidenceContentKind.MEMORY_CONTENT})
                    for row in request.input_records
                )
            }
        )
    return request


def unknown(queue, request, *, error="hy_memory_runtime_failed"):
    row = queue.submit(request)
    queue.settle(row.job_id, state=UNKNOWN, hy_invoked=True, error_code=error)
    return next(job for job in queue.unknown_jobs(limit=100) if job.job_id == row.job_id)


def sdk_request(frozen):
    return MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)


def outcome_for(request, *, operation="add", **result):
    return {
        "request_id": request.request_id,
        "user_id": request.user_id,
        "agent_id": request.agent_id,
        "session_id": request.session_id,
        "operation": operation,
        "status": "succeeded",
        "result": {"request_id": request.request_id, "success": True, "scene": "chat", **result},
    }


class InquiryBridge:
    """A controlled failure seam; queue and admission below remain real."""

    def __init__(self, request, result):
        self.request, self.result = request, result
        self.gate = SdkAdmission()
        self.queries = []

    @asynccontextmanager
    async def exclusive_access(self):
        async with self.gate.operation():
            yield

    async def get_sdk_write_outcome(self, identity):
        self.queries.append(identity)
        return self.result

    async def get_sdk_raw(self, identity):
        return SimpleNamespace(
            raw_id=identity,
            user_id=self.request.user_id,
            agent_id=self.request.agent_id,
            session_id=self.request.session_id,
        )

    async def get_sdk_memory(self, identity):
        return SimpleNamespace(
            memory_id=identity,
            user_id=self.request.user_id,
            agent_id=self.request.agent_id,
            layer="l2_fact",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"status": "running"},
        {"status": "unknown"},
        {"status": "failed"},
        {"request_id": "foreign"},
        {"user_id": "foreign"},
        {"agent_id": "foreign"},
        {"session_id": "foreign"},
        {"operation": "add_extracted"},
        {"result": {}},
        {"result.success": 1},
        {"result.request_id": "foreign"},
        {"result.memory_ids": [1]},
        {"result.memory_ids": ["raw"]},
        {"result.memory_id": None},
        {"result.scene": "future"},
        {"result.fact_ids": ["fact"]},
        {"result.memory_ids": ["x"] * 513},
    ],
)
def test_incomplete_or_mismatched_outcomes_never_complete_or_retry(tmp_path, change):
    queue = DurableWriteQueue(tmp_path / "queue.sqlite3", sdk_authority=True)
    frozen = request_for()
    job = unknown(queue, frozen)
    request = sdk_request(frozen)
    result = outcome_for(request, memory_id="raw", memory_ids=["semantic"])
    for key, value in change.items():
        if key.startswith("result."):
            result["result"][key.split(".", 1)[1]] = value
        else:
            result[key] = value
    bridge = InquiryBridge(request, result)
    reconciler = SdkReconciler(
        lambda: queue, lambda: bridge, SimpleNamespace(require_ready=lambda: None)
    )
    assert asyncio.run(reconciler.run()) == 0
    row = queue.get(job.request_id)
    assert row.state == UNKNOWN and row.updated_at == job.updated_at
    assert queue.sdk_receipt(job.job_id) is None
    assert bridge.queries == [job.request_id]


@pytest.mark.parametrize("race", ["timestamp", "payload", "state", "policy"])
def test_queue_compare_and_commit_never_overwrites_changed_intent(tmp_path, race):
    path = tmp_path / "queue.sqlite3"
    queue = DurableWriteQueue(path, sdk_authority=True)
    frozen = request_for()
    job = unknown(queue, frozen)
    with sqlite3.connect(path) as db:
        column, value = {
            "timestamp": ("updated_at", "changed"),
            "payload": ("payload_json", "{}"),
            "state": ("state", "pending"),
            "policy": ("last_error", NON_RETRYABLE_POST_WRITE_CODES[0]),
        }[race]
        db.execute(f"UPDATE memory_write_jobs SET {column}=? WHERE job_id=?", (value, job.job_id))
    assert (
        queue.resolve_unknown(
            job.job_id,
            found=True,
            sdk_receipt=DurableSdkReceipt(job.job_id, ("raw",), ("memory",)),
            expected_request=frozen,
            expected_updated_at=job.updated_at,
        )
        is None
    )
    assert queue.sdk_receipt(job.job_id) is None


def test_receipt_atomic_restart_idempotency_and_late_worker(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = DurableWriteQueue(path, sdk_authority=True)
    frozen = request_for()
    job = unknown(queue, frozen)
    receipt = DurableSdkReceipt(job.job_id, ("raw",), ("memory",))
    kwargs = dict(
        found=True, sdk_receipt=receipt, expected_request=frozen, expected_updated_at=job.updated_at
    )
    # A failure between UPDATE and COMMIT cannot publish just the terminal state.
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER reject_receipt BEFORE UPDATE ON memory_write_jobs "
            "WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        queue.resolve_unknown(job.job_id, **kwargs)
    assert queue.get(job.request_id).state == UNKNOWN
    assert queue.sdk_receipt(job.job_id) is None
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER reject_receipt")
    assert queue.resolve_unknown(job.job_id, **kwargs) == COMPLETED
    restarted = DurableWriteQueue(path, sdk_authority=True)
    assert restarted.sdk_receipt(job.job_id) == receipt
    assert restarted.resolve_unknown(job.job_id, **kwargs) is None
    restarted.settle(job.job_id, state=UNKNOWN, hy_invoked=True)
    assert restarted.get(job.request_id).state == COMPLETED
    assert restarted.sdk_receipt(job.job_id) == receipt


async def stop_drain(core):
    await asyncio.sleep(0)
    core.state.memory_write_drain.stop()
    await core.state.memory_write_drain_task


async def seed_sdk(sdk, bridge, request, scene, monkeypatch):
    """Real SDK stores/outcome wrapper; extraction response is an explicit test stub."""
    raw_id, memory_id, fact_id = (str(uuid4()) for _ in range(3))
    identity = dict(
        user_id=request.user_id, agent_id=request.agent_id, session_id=request.session_id
    )
    if scene in {"working", "coding"}:

        async def populate():
            store = CodingMemoryStore(
                sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=scene
            )
            await store.initialize()
            try:
                await store.insert(
                    CodingMemory(
                        experience_id=memory_id,
                        task="Synthetic task",
                        solution="Synthetic solution",
                        source_fact_ids=[fact_id],
                        **identity,
                    )
                )
                await store.insert_fact(
                    WorkingFact(
                        fact_id=fact_id,
                        statement="Synthetic fact",
                        derived_experience_ids=[memory_id],
                        **identity,
                    )
                )
            finally:
                await store.close()

        await _joined_thread(lambda: sdk._loop_thread.run(populate()))
        # This is the live gap: main public get cannot hydrate productivity output.
        assert await bridge.get_sdk_memory(memory_id) is None
        result = dict(
            scene="productivity",
            memory_id=memory_id,
            experience_ids=[memory_id],
            fact_ids=[fact_id],
        )
        expected = ((), (memory_id, fact_id))
    else:
        inserted = await _joined_thread(
            sdk.import_memory_exact,
            "Synthetic semantic memory",
            memory_id=memory_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
        )
        assert inserted["success"]
        if scene == "chat":
            await bridge.import_sdk_raw_exact(
                content="Synthetic original conversation",
                raw_id=raw_id,
                **identity,
                source="synthetic",
                memory_at=None,
                metadata={},
                request_id=str(uuid4()),
            )
            expected = ((raw_id,), (memory_id,))
        else:
            expected = ((), (memory_id,))
        result = dict(
            scene="chat", memory_id=raw_id if scene == "chat" else memory_id, memory_ids=[memory_id]
        )
    calls = []

    async def extraction_stub(*args, **kwargs):
        calls.append(kwargs["request_id"])
        return dict(success=True, request_id=request.request_id, **result)

    operation = "add_extracted" if scene == "extracted" else "add"
    monkeypatch.setattr(sdk, "_async_" + operation + "_impl", extraction_stub)
    response = await _joined_thread(
        getattr(sdk, operation), "Synthetic input", **identity, request_id=request.request_id
    )
    assert response["success"] and calls == [request.request_id]
    return expected, calls


def test_real_sdk_all_backends_reopen_and_http_status_recovery(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    expected, invocations = {}, []

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app.core)
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                _, headers, _, envelope = await provision(web, config)
                posted = await web.post("/hub/v1/events", headers=headers, json=envelope)
                assert posted.status_code == 202
                queue = app.core.state.memory_write_queue
                remote_id = posted.json()["receipt_id"]
                remote_frozen = queue.payload_for(remote_id)
                queue.settle(remote_id, state=UNKNOWN, hy_invoked=True)
                bridge = _sdk_library_bridge(app.core)
                sdk = await bridge._get_client()
                for scene in ("chat", "extracted", "working", "coding"):
                    frozen = (
                        remote_frozen
                        if scene == "chat"
                        else request_for(extracted=scene == "extracted")
                    )
                    job_id = remote_id if scene == "chat" else unknown(queue, frozen).job_id
                    request = sdk_request(frozen)
                    ids, calls = await seed_sdk(sdk, bridge, request, scene, monkeypatch)
                    expected[job_id] = DurableSdkReceipt(job_id, *ids)
                    invocations.append(calls)
                    if scene in {"working", "coding"}:
                        from cloud_memory_hub.productivity_read import record_id

                        inventory = await observe_inventory(bridge, request.user_id)
                        experience = record_id(scene[0], "e", ids[1][0])
                        fact = record_id(scene[0], "f", ids[1][1])
                        assert inventory[experience].parents == frozenset({fact})
                        assert inventory[fact].kind == "memory"
                    else:
                        assert await observe_inventory(bridge, request.user_id)
                assert all(queue.sdk_receipt(job_id) is None for job_id in expected)
        restarted = create_hub(config)
        async with restarted.core.router.lifespan_context(restarted.core):
            await stop_drain(restarted.core)
            bridge = _sdk_library_bridge(restarted.core)
            sdk = await bridge._get_client()

            def no_write(*args, **kwargs):
                pytest.fail("reconciliation must never invoke the writer")

            monkeypatch.setattr(sdk, "add", no_write)
            monkeypatch.setattr(sdk, "add_extracted", no_write)
            assert not sdk._coding_stores
            # Exercise the same callback wired into the production queue timer.
            await restarted.core.state.memory_write_drain.reconcile_unknown()
            queue = restarted.core.state.memory_write_queue
            assert {identity: queue.sdk_receipt(identity) for identity in expected} == expected
            assert not sdk._coding_stores, "inquiry must not initialize any coding writer"
            assert await restarted.core.state.cmh_reconciler.run() == 0
            async with AsyncClient(
                transport=ASGITransport(restarted), base_url=config.public_origin
            ) as web:
                response = await web.get("/hub/v1/events/" + envelope["event_id"], headers=headers)
                assert response.status_code == 200
                assert response.json()["processing_state"] == COMPLETED
            assert all(len(calls) == 1 for calls in invocations)

    asyncio.run(scenario())


def test_cancelled_commit_is_joined_before_sdk_admission_releases(tmp_path, monkeypatch):
    queue = DurableWriteQueue(tmp_path / "queue.sqlite3", sdk_authority=True)
    frozen = request_for()
    job = unknown(queue, frozen)
    request = sdk_request(frozen)
    bridge = InquiryBridge(request, outcome_for(request, memory_id="raw", memory_ids=[]))
    entered, release = Event(), Event()
    original = queue.resolve_unknown

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, "resolve_unknown", held)
    reconciler = SdkReconciler(
        lambda: queue, lambda: bridge, SimpleNamespace(require_ready=lambda: None)
    )

    async def scenario():
        task = asyncio.create_task(reconciler.run())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        barrier_entered = asyncio.Event()

        async def maintenance():
            async with bridge.gate.exclusive():
                barrier_entered.set()

        barrier = asyncio.create_task(maintenance())
        await asyncio.sleep(0)
        try:
            assert not task.done() and not barrier_entered.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await barrier
        assert barrier_entered.is_set() and queue.sdk_receipt(job.job_id).raw_ids == ("raw",)

    asyncio.run(scenario())


def test_bounded_cursor_makes_progress_and_protected_jobs_are_never_queried(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk_reconcile, "BATCH_SIZE", 2)
    queue = DurableWriteQueue(tmp_path / "queue.sqlite3", sdk_authority=True)
    frozen = [request_for() for _ in range(5)]
    jobs = [unknown(queue, request) for request in frozen[:4]]
    protected = unknown(queue, frozen[4], error=NON_RETRYABLE_POST_WRITE_CODES[0])
    requests = {item.idempotency_key: sdk_request(item) for item in frozen}
    bridge = InquiryBridge(requests[jobs[0].request_id], None)

    async def inquire(identity):
        bridge.queries.append(identity)
        bridge.request = requests[identity]
        if identity == jobs[0].request_id:
            raise RuntimeError("SYNTHETIC_PRIVATE_CONTENT")
        if identity == jobs[1].request_id:
            return None
        return outcome_for(bridge.request, memory_id="raw", memory_ids=[])

    monkeypatch.setattr(bridge, "get_sdk_write_outcome", inquire)
    reconciler = SdkReconciler(
        lambda: queue, lambda: bridge, SimpleNamespace(require_ready=lambda: None)
    )

    async def scenario():
        assert await reconciler.run() == 0
        assert bridge.queries == [job.request_id for job in jobs[:2]]
        assert await reconciler.run() == 2
        assert bridge.queries == [job.request_id for job in jobs]
        assert await reconciler.run() == 0  # exhausted cursor resets, no unbounded rescan
        assert await reconciler.run() == 0
        assert bridge.queries[-2:] == [job.request_id for job in jobs[:2]]
        assert protected.request_id not in bridge.queries

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target,change",
    [
        ("raw", None),
        ("raw", {"raw_id": "different"}),
        ("raw", {"session_id": "different"}),
        ("raw", {"user_id": "different"}),
        ("memory", None),
        ("memory", {"user_id": "different"}),
        ("memory", {"agent_id": "different"}),
        ("memory", {"memory_id": "different"}),
        ("memory", {"layer": "l1_raw"}),
    ],
)
def test_output_references_require_existence_and_exact_ownership(
    tmp_path, monkeypatch, target, change
):
    queue = DurableWriteQueue(tmp_path / "queue.sqlite3", sdk_authority=True)
    frozen = request_for()
    job = unknown(queue, frozen)
    request = sdk_request(frozen)
    bridge = InquiryBridge(request, outcome_for(request, memory_id="raw", memory_ids=["memory"]))
    original = getattr(bridge, "get_sdk_" + target)

    async def changed(identity):
        if change is None:
            return None
        record = await original(identity)
        record.__dict__.update(change)
        return record

    monkeypatch.setattr(bridge, "get_sdk_" + target, changed)
    reconciler = SdkReconciler(
        lambda: queue, lambda: bridge, SimpleNamespace(require_ready=lambda: None)
    )
    assert asyncio.run(reconciler.run()) == 0
    assert queue.get(job.request_id).state == UNKNOWN


def test_governance_change_before_commit_and_recovery_error_do_not_leak(tmp_path, caplog):
    queue = DurableWriteQueue(tmp_path / "queue.sqlite3", sdk_authority=True)
    frozen = request_for()
    job = unknown(queue, frozen)
    request = sdk_request(frozen)
    bridge = InquiryBridge(request, outcome_for(request, memory_id="raw", memory_ids=[]))
    checks = []

    def ready():
        checks.append(True)
        if len(checks) == 3:
            raise GovernanceError("SYNTHETIC_PRIVATE_CONTENT")

    reconciler = SdkReconciler(lambda: queue, lambda: bridge, SimpleNamespace(require_ready=ready))
    with caplog.at_level("DEBUG", logger="cloud_memory_hub.sdk_reconcile"):
        assert asyncio.run(reconciler.run()) == 0
    assert len(checks) == 3 and queue.get(job.request_id).state == UNKNOWN
    assert "SYNTHETIC_PRIVATE_CONTENT" not in caplog.text
    assert "sdk_reconcile_deferred" in caplog.text


def test_normal_extracted_bridge_receipt_contains_no_raw_ids():
    from one_memory.hy_memory_bridge import HyMemoryBridge

    frozen = request_for(extracted=True)
    request = sdk_request(frozen)

    class Client:
        def get(self, identity):
            return dict(
                memory_id=identity,
                user_id=request.user_id,
                agent_id=request.agent_id,
                content="Synthetic imported memory",
                layer="l2_fact",
            )

    result = asyncio.run(
        HyMemoryBridge._result(
            Client(),
            request,
            dict(success=True, memory_id="memory", memory_ids=["memory"]),
            expected_contents=frozenset({"Synthetic imported memory"}),
        )
    )
    assert result.success and result.raw_ids == ()
    assert tuple(item.memory_id for item in result.memories) == ("memory",)


def test_productivity_missing_wrong_owner_and_variant_collision_remain_unproven(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            await stop_drain(app.core)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            request = sdk_request(request_for())
            ids, _ = await seed_sdk(sdk, bridge, request, "working", monkeypatch)
            memory_id, fact_id = ids[1]
            base = sdk._config.coding.db_path
            assert (await proven_receipt(bridge, "job", request, "add")).memory_ids == ids[1]
            with sqlite3.connect(base) as db:
                for column, expected in (
                    ("user_id", request.user_id),
                    ("agent_id", request.agent_id),
                    ("session_id", request.session_id),
                ):
                    db.execute(
                        f"UPDATE working_fact SET {column}=? WHERE fact_id=?", ("foreign", fact_id)
                    )
                    db.commit()
                    with pytest.raises(ValueError, match="mismatch"):
                        await proven_receipt(bridge, "job", request, "add")
                    db.execute(
                        f"UPDATE working_fact SET {column}=? WHERE fact_id=?", (expected, fact_id)
                    )
                    db.commit()
                db.execute(
                    "UPDATE working_fact SET fact_id=? WHERE fact_id=?", ("unrelated", fact_id)
                )
                db.commit()
                with pytest.raises(ValueError, match="reference_missing"):
                    await proven_receipt(bridge, "job", request, "add")
                db.execute(
                    "UPDATE working_fact SET fact_id=? WHERE fact_id=?", (fact_id, "unrelated")
                )
                db.commit()

            async def collision():
                store = CodingMemoryStore(
                    sdk._config, sdk._embed_service, db_path=base, variant="coding"
                )
                await store.initialize()
                try:
                    await store.insert(
                        CodingMemory(
                            experience_id=memory_id,
                            user_id=request.user_id,
                            agent_id=request.agent_id,
                            task="Synthetic collision",
                            solution="Different variant",
                        )
                    )
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(collision()))
            with pytest.raises(ValueError, match="identity_ambiguous"):
                await proven_receipt(bridge, "job", request, "add")
            with existing_database(base) as db:
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    db.execute("DELETE FROM working_fact")

    asyncio.run(scenario())
    missing = tmp_path / "absent.sqlite3"
    assert productivity_identities(missing, (), ()) == {}
    assert not missing.exists() and not missing.with_stem("absent_cv").exists()


def test_productivity_empty_result_requires_explicit_lists(tmp_path, monkeypatch):
    frozen = request_for()
    request = sdk_request(frozen)
    bridge = InquiryBridge(
        request,
        outcome_for(request, scene="productivity", memory_id=None, experience_ids=[], fact_ids=[]),
    )

    async def client():
        return SimpleNamespace(
            _config=SimpleNamespace(coding=SimpleNamespace(db_path=tmp_path / "absent.sqlite3"))
        )

    bridge._get_client = client

    async def scenario():
        receipt = await proven_receipt(bridge, "job", request, "add")
        assert receipt == DurableSdkReceipt("job", (), ())
        bridge.result["result"].pop("fact_ids")
        with pytest.raises(ValueError, match="references_invalid"):
            await proven_receipt(bridge, "job", request, "add")

    asyncio.run(scenario())
