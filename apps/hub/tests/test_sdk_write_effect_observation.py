"""FR-10/FR-11, ADR-0039: observations are facts, never deletion permission."""

import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.agent.reconciler import MemoryReconciler, ReconcileOp, ReconcileResult
from hy_memory.core.write_effects import WriteEffectObservation, observe_async, unsupported
from hy_memory.models.memory import MemoryNode
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_original_archive import config_for
from test_output_lineage import fixture


def observation(identity="synthetic-request"):
    return WriteEffectObservation(
        request_id=identity,
        user_id="synthetic-user",
        agent_id="synthetic-agent",
        session_id="synthetic-session",
        operation="add",
    )


def selectors(snapshot, domain="chroma", field="node_id"):
    return {
        item["selector"][field]
        for item in snapshot["attempts"]
        if item["domain"] == domain and field in item["selector"]
    }


def assert_observation(value, identity):
    assert value["schema_version"] == 1
    assert value["observation_only"] is True and value["all_effects_proven"] is False
    assert value["snapshot_sealed"] is True
    assert value["request"]["request_id"] == identity
    assert value["status"] in {"recorded", "partial", "unsupported"}
    assert all(item["state"] in {"returned", "uncertain"} for item in value["attempts"])
    assert len(json.dumps(value, ensure_ascii=False).encode()) < WriteEffectObservation.MAX_BYTES


def models(monkeypatch, operations=None):
    async def extract(self, **kwargs):
        return AgentResult(
            success=True,
            extracted_info={
                "memory": [{"content": "SYNTHETIC_BODY_NOT_IN_EFFECT_OBSERVATION"}],
                "basic_info": {"name": "Synthetic Observer"},
            },
        )

    async def reconcile(self, **kwargs):
        return ReconcileResult(
            ops=operations
            or [ReconcileOp(op="ADD", content="SYNTHETIC_BODY_NOT_IN_EFFECT_OBSERVATION")]
        )

    monkeypatch.setattr(MemAgent, "process_add", extract)
    monkeypatch.setattr(MemoryReconciler, "reconcile", reconcile)


@pytest.mark.parametrize("method", ["add", "add_extracted"])
def test_real_nonempty_writer_observation_survives_reopen_and_replay(tmp_path, monkeypatch, method):
    models(monkeypatch)
    config, identity, results = config_for(tmp_path), str(uuid4()), []

    async def scenario():
        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            result = await _joined_thread(
                getattr(sdk, method),
                "SYNTHETIC_BODY_NOT_IN_EFFECT_OBSERVATION",
                user_id="synthetic-user",
                agent_id="synthetic-agent",
                request_id=identity,
            )
            assert result["success"] and result["memory_ids"]
            value = result["write_effects"]
            assert_observation(value, identity)
            assert set(result["memory_ids"]) <= selectors(value)
            assert "SYNTHETIC_BODY_NOT_IN_EFFECT_OBSERVATION" not in json.dumps(value)
            assert (
                value["domains"]["write_outcome"] == "known_preexisting_claim_and_terminal_envelope"
            )
            assert value["domains"]["tag_index"] == "unsupported_by_backend"
            assert value["domains"]["retrieval_clause_index"] == "unsupported_by_backend"
            assert value["domains"]["diagnostic_files"] == "not_observed"
            assert "background_pipeline_logs_unjoined" in value["reasons"]
            if method == "add":
                assert result["memory_id"] in selectors(value)
                assert result["memory_id"] in selectors(value, "history", "memory_id")
                profile = result["basic_profile_inputs"]["output_ids"]
                assert profile and set(profile) <= selectors(value)
                assert not set(profile) & set(result["memory_ids"])
            else:
                assert not selectors(value, "history", "memory_id")
            assert any(item["domain"] == "cache_episode_state" for item in value["attempts"])
            results.append(copy.deepcopy(value))

        async def forbidden(*args, **kwargs):
            pytest.fail("cached outcome must not invoke the writer")

        monkeypatch.setattr(MemAgent, "process_add", forbidden)
        monkeypatch.setattr(MemoryReconciler, "reconcile", forbidden)
        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            stored = await _joined_thread(sdk.get_write_outcome, identity)
            assert stored["result"]["write_effects"] == results[0]
            replay = await _joined_thread(
                getattr(sdk, method),
                "different replay input is not executed",
                user_id="synthetic-user",
                agent_id="synthetic-agent",
                request_id=identity,
            )
            assert replay["replayed"] and replay["write_effects"] == results[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["shadow", "supersede"])
def test_real_writer_observes_old_nodes_omitted_by_business_result(
    tmp_path, monkeypatch, operation
):
    models(monkeypatch)

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            old = []
            for _index in range(2):
                result = await _joined_thread(
                    sdk.add_extracted, "Synthetic old body", user_id="u", request_id=str(uuid4())
                )
                old.append(result["memory_id"])
            op = (
                ReconcileOp(op="UPDATE", memory_id=old[0], content="")
                if operation == "shadow"
                else ReconcileOp(
                    op="SUPERSEDE", memory_id=old[1], memory_ids=old, content="Synthetic successor"
                )
            )
            models(monkeypatch, [op])
            result = await _joined_thread(
                sdk.add_extracted, "Synthetic update", user_id="u", request_id=str(uuid4())
            )
            assert result["success"]
            expected = set(old[:1] if operation == "shadow" else old)
            assert expected <= selectors(result["write_effects"])
            assert not expected & set(result.get("memory_ids", []))

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_native_commit_then_failure_keeps_attempt_and_original_exception(
    tmp_path, monkeypatch, failure
):
    from hy_memory.data import vector_store_chroma as module

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def perform():
                capture = observation()
                token = capture.activate()
                node = MemoryNode(
                    user_id="u", content="Synthetic committed body", embedding=[1.0, 0.0]
                )
                original = module._run_in_vdb_pool

                async def fail_after_commit(function, *args, **kwargs):
                    await original(function, *args, **kwargs)
                    if failure == "cancel":
                        raise asyncio.CancelledError()
                    raise RuntimeError("synthetic after-commit failure")

                try:
                    monkeypatch.setattr(module, "_run_in_vdb_pool", fail_after_commit)
                    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
                    with pytest.raises(expected):
                        await sdk._vector_store.upsert(node)
                finally:
                    monkeypatch.setattr(module, "_run_in_vdb_pool", original)
                    capture.reset(token)
                value = capture.seal()
                assert value["attempts"][0]["state"] == "uncertain"
                assert node.node_id in selectors(value)
                assert (await sdk._vector_store.get_by_id(node.node_id)).content == node.content

            await _joined_thread(lambda: sdk._loop_thread.run(perform()))

    asyncio.run(scenario())


def test_five_native_mutators_and_history_cache_share_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_MEMORY_OPERATIONS_ENABLED", "true")

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def perform():
                capture = observation()
                token = capture.activate()
                first = MemoryNode(user_id="u", content="Synthetic first", embedding=[1.0, 0.0])
                second = MemoryNode(user_id="u", content="Synthetic second", embedding=[0.0, 1.0])
                try:
                    await sdk._vector_store.upsert(first)
                    await sdk._vector_store.upsert_batch(iter([second]))
                    await sdk._vector_store.update_payload(
                        first.node_id, {"content": "Synthetic edited", "embedding": [1.0, 0.0]}
                    )
                    await sdk._vector_store.update_embedding(second.node_id, [1.0, 0.0])
                    await sdk._vector_store.delete(first.node_id)
                    await sdk._history_store.record_add(
                        first.node_id, "Synthetic history", "l1_raw", "u::a"
                    )
                    await sdk._cache.store_memory_operation(
                        request_id="synthetic-request",
                        user_id="u",
                        agent_id="a",
                        op="ADD",
                        memory_id=second.node_id,
                        content="Synthetic audit",
                    )
                    await sdk._cache.upsert_episode_state(
                        "u::a::s",
                        user_id="u",
                        agent_id="a",
                        session_id="s",
                        episode_id="ep_1",
                        last_write_ts=1.0,
                        memory_count=1,
                    )
                finally:
                    capture.reset(token)
                value = capture.seal()
                assert {
                    item["operation"] for item in value["attempts"] if item["domain"] == "chroma"
                } == {
                    "upsert",
                    "upsert_batch",
                    "update_payload",
                    "update_embedding",
                    "delete",
                }
                assert selectors(value, "history", "memory_id") == {first.node_id}
                assert selectors(value, "cache_request_rows", "request_id") == {"synthetic-request"}
                assert all(item["state"] == "returned" for item in value["attempts"])

            await _joined_thread(lambda: sdk._loop_thread.run(perform()))

    asyncio.run(scenario())


def test_batch_records_all_targets_before_first_native_io_and_partial_write(tmp_path, monkeypatch):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def perform():
                capture = observation()
                token = capture.activate()
                nodes = [
                    MemoryNode(user_id="u", content="Synthetic batch", embedding=[1.0, 0.0])
                    for _ in range(3)
                ]
                collection = sdk._vector_store._collection
                original = collection.upsert

                def partial_write(**kwargs):
                    assert selectors(capture.snapshot()) == {node.node_id for node in nodes}
                    original(**{key: value[:1] for key, value in kwargs.items()})
                    raise RuntimeError("synthetic partial batch")

                try:
                    monkeypatch.setattr(collection, "upsert", partial_write)
                    with pytest.raises(RuntimeError, match="partial batch"):
                        await sdk._vector_store.upsert_batch(nodes)
                finally:
                    monkeypatch.setattr(collection, "upsert", original)
                    capture.reset(token)
                value = capture.seal()
                assert len(value["attempts"]) == 3
                assert all(item["state"] == "uncertain" for item in value["attempts"])
                assert await sdk._vector_store.get_by_id(nodes[0].node_id) is not None
                assert await sdk._vector_store.get_by_id(nodes[1].node_id) is None

            await _joined_thread(lambda: sdk._loop_thread.run(perform()))

    asyncio.run(scenario())


def test_concurrent_nested_contexts_and_late_tasks_do_not_mutate_returned_snapshot():
    @observe_async("history", "insert", lambda a: ({"memory_id": a["identity"]},))
    async def write(identity):
        await asyncio.sleep(0)

    async def scenario():
        async def one(identity):
            capture = observation(identity)
            token = capture.activate()
            release = asyncio.Event()

            async def late():
                await release.wait()
                await write(identity + "-late")

            try:
                task = asyncio.create_task(late())
                await write(identity)
                nested = observation(identity + "-nested")
                inner = nested.activate()
                try:
                    await write(identity + "-nested")
                finally:
                    nested.reset(inner)
                await write(identity + "-restored")
                frozen = capture.seal()
            finally:
                capture.reset(token)
            unchanged = copy.deepcopy(frozen)
            release.set()
            await task
            assert frozen == unchanged
            assert selectors(frozen, "history", "memory_id") == {identity, identity + "-restored"}
            assert "late_mutation" in capture.snapshot()["reasons"]
            assert capture.snapshot()["status"] == "partial"
            assert selectors(nested.seal(), "history", "memory_id") == {identity + "-nested"}
            return frozen

        first, second = await asyncio.gather(one("one"), one("two"))
        assert not selectors(first, "history", "memory_id") & selectors(
            second, "history", "memory_id"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", ["attempts", "strings", "bytes"])
def test_budget_retains_explicit_partial_status_without_body_or_unbounded_snapshot(
    monkeypatch, budget
):
    capture = observation()
    if budget == "attempts":
        monkeypatch.setattr(capture, "MAX_ATTEMPTS", 2)
        items = ({"memory_id": str(index)} for index in range(10))
        expected = "attempt_limit"
    elif budget == "strings":
        items = [{"memory_id": "secret-body" * 100}]
        expected = "selector_invalid"
    else:
        monkeypatch.setattr(capture, "MAX_BYTES", 3000)
        items = ({"memory_id": "x" * 500} for _ in range(10))
        expected = "byte_limit"
    tokens = capture.begin("history", "insert", items)
    capture.finish(tokens, returned=True)
    result = capture.seal()
    assert result["status"] == "partial" and expected in result["reasons"]
    assert len(json.dumps(result).encode()) < capture.MAX_BYTES
    assert "secret-body" not in json.dumps(result)


def test_unknown_backends_productivity_and_claim_are_explicit():
    capture = WriteEffectObservation.for_client(
        SimpleNamespace(
            _vector_store=object(), _cache=object(), _history_store=object(), _graph_store=object()
        ),
        request_id="r",
        user_id="u",
        agent_id="a",
        session_id="s",
        operation="add",
    )
    token = capture.activate()
    try:
        unsupported("productivity_unobserved")
    finally:
        capture.reset(token)
    result = capture.seal()
    assert result["status"] == "unsupported"
    assert result["domains"]["productivity"] == result["domains"]["graph"] == "not_observed"
    assert result["domains"]["write_outcome"] == "known_preexisting_claim_and_terminal_envelope"
    assert result["all_effects_proven"] is False


def test_old_cached_result_replays_without_inventing_observation(tmp_path):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def seed():
                await sdk._cache.claim_write_outcome("legacy", "u", "a", "s", "add")
                await sdk._cache.finish_write_outcome(
                    "legacy",
                    "succeeded",
                    {
                        "success": True,
                        "request_id": "legacy",
                        "memory_id": "legacy-id",
                    },
                )

            await _joined_thread(lambda: sdk._loop_thread.run(seed()))
            result = await _joined_thread(
                sdk.add,
                "Synthetic replay",
                user_id="u",
                agent_id="a",
                session_id="s",
                request_id="legacy",
            )
            assert result["replayed"] and "write_effects" not in result

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_sdk_unknown_persists_observed_targets_and_replay_cannot_write(
    tmp_path, monkeypatch, failure
):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            identity = str(uuid4())
            seen = []

            async def interrupted(*args, **kwargs):
                node = MemoryNode(
                    user_id="u", content="Synthetic pre-error body", embedding=[1.0, 0.0]
                )
                await sdk._vector_store.upsert(node)
                seen.append(node.node_id)
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("synthetic write failure")

            monkeypatch.setattr(sdk, "_async_add_impl", interrupted)
            expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
            with pytest.raises(expected):
                await _joined_thread(sdk.add, "Synthetic input", user_id="u", request_id=identity)
            stored = await _joined_thread(sdk.get_write_outcome, identity)
            assert stored["status"] == "unknown"
            value = stored["result"]["write_effects"]
            assert_observation(value, identity)
            assert selectors(value) == set(seen)
            replay = await _joined_thread(
                sdk.add, "Synthetic replay", user_id="u", request_id=identity
            )
            assert replay["replayed"] and replay["outcome_status"] == "unknown"
            assert len(seen) == 1

    asyncio.run(scenario())


def test_pipeline_enqueue_records_selector_before_unjoined_background_write(tmp_path):
    from hy_memory.utils.pipeline_observability import wrap_store_pipeline_log

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        rows = []

        async def persist(**kwargs):
            entered.set()
            await release.wait()
            rows.append(kwargs["request_id"])

        wrapped = wrap_store_pipeline_log(
            persist,
            log_writer=SimpleNamespace(write_step=lambda **kwargs: None),
            trace_enabled=True,
        )
        capture = observation()
        token = capture.activate()
        try:
            await wrapped(request_id="synthetic-request", prompt="Synthetic unrecorded body")
            value = capture.seal()
        finally:
            capture.reset(token)
        frozen = copy.deepcopy(value)
        await entered.wait()
        assert not rows
        assert "background_pipeline_logs_unjoined" in value["reasons"]
        assert value["status"] == "partial"
        assert selectors(value, "cache_request_rows", "request_id") == {"synthetic-request"}
        assert "Synthetic unrecorded body" not in json.dumps(value)
        release.set()
        from hy_memory.utils.pipeline_observability import drain_pipeline_log_tasks

        await drain_pipeline_log_tasks()
        assert rows == ["synthetic-request"] and value == frozen

    asyncio.run(scenario())


def test_identity_and_domain_bounds_and_invalid_selector_iteration_are_bounded(monkeypatch):
    capture = WriteEffectObservation(
        request_id="synthetic" * 100,
        user_id="u",
        agent_id="a",
        session_id="s",
        operation="add",
        domains={"not-a-domain": "private body", "chroma": "private body"},
    )
    monkeypatch.setattr(capture, "MAX_ATTEMPTS", 2)
    seen = []

    def invalids():
        for index in range(100):
            seen.append(index)
            yield {"body": "must never enter observation"}

    assert capture.begin("history", "insert", invalids()) == ()
    value = capture.seal()
    assert len(seen) == 3
    assert value["request"]["request_id"] is None
    assert value["status"] == "partial"
    assert {"identity_limit", "unknown_domain", "selector_invalid", "attempt_limit"} <= set(
        value["reasons"]
    )
    assert "private body" not in json.dumps(value) and "must never" not in json.dumps(value)


def test_actual_concurrent_sdk_writes_keep_native_selectors_and_requests_separate(
    tmp_path, monkeypatch
):
    models(monkeypatch)

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            identities = [str(uuid4()), str(uuid4())]
            first, second = await asyncio.gather(
                *(
                    _joined_thread(
                        sdk.add_extracted,
                        "Synthetic concurrent body",
                        user_id=f"user-{index}",
                        request_id=identity,
                    )
                    for index, identity in enumerate(identities)
                )
            )
            for index, result in enumerate((first, second)):
                assert result["success"]
                assert_observation(result["write_effects"], identities[index])
                assert selectors(result["write_effects"], "cache_request_rows", "request_id") <= {
                    identities[index]
                }
            assert not selectors(first["write_effects"]) & selectors(second["write_effects"])

    asyncio.run(scenario())


@pytest.mark.parametrize("character", ["\x00", "\\", "汉", "😀"])
def test_json_escaped_identity_and_selector_total_budget(character):
    identity = character * (512 // len(character.encode()))
    capture = WriteEffectObservation(
        request_id=identity,
        user_id=identity,
        agent_id=identity,
        session_id=identity,
        operation="add",
    )
    tokens = capture.begin("history", "insert", ({"memory_id": "x" * 250} for _ in range(1024)))
    capture.finish(tokens, returned=True)
    value = capture.seal()
    assert value["status"] == "partial" and "byte_limit" in value["reasons"]
    assert len(json.dumps(value, ensure_ascii=False).encode()) <= capture.MAX_BYTES
    assert value["attempts"]


def test_sdk_resets_outer_context_when_observation_seal_raises(tmp_path, monkeypatch):
    from hy_memory.core import write_effects

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def native(*args, **kwargs):
                return {"success": True, "request_id": kwargs["request_id"]}

            async def perform():
                outer = observation("outer")
                token = outer.activate()
                original = WriteEffectObservation.seal

                def fail(self):
                    raise RuntimeError("synthetic observation failure")

                try:
                    monkeypatch.setattr(sdk, "_async_add_impl", native)
                    monkeypatch.setattr(WriteEffectObservation, "seal", fail)
                    with pytest.raises(RuntimeError, match="observation failure"):
                        await sdk.async_add("Synthetic", user_id="u", request_id=str(uuid4()))
                    assert write_effects._CURRENT.get() is outer
                finally:
                    monkeypatch.setattr(WriteEffectObservation, "seal", original)
                    outer.reset(token)

            await _joined_thread(lambda: sdk._loop_thread.run(perform()))

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_entity_index_boundary_is_independent_of_graph(tmp_path, enabled):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            assert sdk._graph_store is None
            previous = sdk._config.recall.entity_store_enabled
            try:
                sdk._config.recall.entity_store_enabled = enabled
                capture = WriteEffectObservation.for_client(
                    sdk,
                    request_id="r",
                    user_id="u",
                    agent_id="a",
                    session_id="s",
                    operation="add",
                )
                value = capture.seal()
                assert value["domains"]["entity_index"] == (
                    "not_observed" if enabled else "disabled"
                )
                assert ("entity_index_unobserved" in value["reasons"]) is enabled
                if enabled:
                    assert value["status"] == "unsupported"
            finally:
                sdk._config.recall.entity_store_enabled = previous

    asyncio.run(scenario())
