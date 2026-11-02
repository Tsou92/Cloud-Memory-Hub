import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hy_memory.agent.extractor import Extractor, GlobalLLMConfig
from hy_memory.agent.llm_provider import LLMResponse
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.agent.reconciler import MemoryReconciler
from hy_memory.coding.input_capture import ReconcileInputCapture
from hy_memory.coding.record_origin import verified_origin
from hy_memory.config import MemoryConfig
from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus
from hy_memory.pipelines._retrieval.reconcile_retrieval import ReconcileHybridRetriever
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_original_archive import config_for
from test_output_lineage import fixture


def node(identity, **kwargs):
    return MemoryNode(
        node_id=identity,
        user_id="u",
        agent_id="a",
        session_id="s",
        content="Synthetic memory " + identity,
        layer=MemoryLayer.L2_FACT,
        embedding=[1.0, 0.0],
        **kwargs,
    )


def reconciler(monkeypatch, nodes, *, hits=None, response="[]", fail=False, remove=None):
    seen = []
    config = MemoryConfig.from_dict({"llm": {"structure_metadata_enabled": True}})
    writer = MemoryReconciler(config)
    writer._enable_search_query = False

    async def search(self, queries, embeddings, **kwargs):
        await asyncio.sleep(0)
        return [
            [{"node": item, "score": 0.9} for item in (nodes if hits is None else hits)]
            for _ in queries
        ]

    async def get(ids):
        return [item for item in nodes if item.node_id in ids]

    async def embeddings(ids):
        return {identity: [1.0, 0.0] for identity in ids} if remove else {}

    async def embed(texts):
        return [[1.0, 0.0] for _ in texts]

    async def complete(**kwargs):
        seen.append(kwargs["prompt"])
        if fail:
            raise OSError("synthetic provider unavailable")
        text = response.pop(0) if isinstance(response, list) else response
        return LLMResponse(content=text, tokens_used=1, prompt_tokens=1, completion_tokens=0)

    if remove:

        async def dedup(*args, **kwargs):
            return {"delete_ids": [remove]}

        monkeypatch.setattr("hy_memory.pipelines._retrieval.dedup.execute_dedup", dedup)
    monkeypatch.setattr(ReconcileHybridRetriever, "search_candidates_batch", search)
    monkeypatch.setattr(writer, "_get_llm", lambda: SimpleNamespace(complete=complete))
    store = SimpleNamespace(get_by_ids=get, get_embeddings=embeddings)
    embedder = SimpleNamespace(embed_batch=embed)
    return writer, store, embedder, seen


async def invoke(writer, store, embedder, contents=None):
    return await writer.reconcile(
        new_memories=["Synthetic new fact"] if contents is None else contents,
        user_id="u",
        agent_id="a",
        vector_store=store,
        embed_service=embedder,
    )


def test_final_chain_heads_and_dedup_survivors_match_exact_prompt(monkeypatch):
    old = node("old", superseded_by=["head"], is_latest=False, status=MemoryStatus.SUPERSEDED)
    head = node(
        "head",
        supersedes=["old"],
        tags=["captured-tag"],
        custom={"topic_paths": ["synthetic/topic"]},
    )
    removed = node("removed")
    writer, store, embedder, seen = reconciler(
        monkeypatch, [old, head, removed], hits=[old, removed], remove="removed"
    )
    result = asyncio.run(invoke(writer, store, embedder))
    assert result.success and len(seen) == 1
    capture = result.input_capture
    assert capture["status"] == "complete"
    stage = capture["stages"][0]
    assert stage["status"] == "used" and stage["rendered"] == seen[0]
    assert [item["id"] for item in stage["inputs"]] == ["head"]
    assert "captured-tag" in seen[0] and "synthetic/topic" in seen[0]
    assert old.content not in seen[0] and removed.content not in seen[0]
    frozen = copy.deepcopy(stage)
    head.content = "changed later"
    head.tags.append("later-tag")
    head.custom.clear()
    assert stage == frozen and "embedding" not in stage["inputs"][0]["record"]


@pytest.mark.parametrize(
    "response,fail,status,count",
    [
        ("[]", False, "used", 1),
        (["unparseable", "[]"], False, "used", 2),
        ("unparseable", False, "fallback", 2),
        ("[]", True, "fallback", 1),
    ],
)
def test_retry_and_fallback_keep_one_frozen_input(monkeypatch, response, fail, status, count):
    writer, store, embedder, seen = reconciler(
        monkeypatch, [node("a")], response=response, fail=fail
    )
    result = asyncio.run(invoke(writer, store, embedder))
    assert result.success and result.degraded == (status == "fallback")
    assert len(seen) == count and len(set(seen)) == 1
    assert len(result.input_capture["stages"]) == 1
    stage = result.input_capture["stages"][0]
    assert stage["status"] == status and stage["rendered"] == seen[0]


@pytest.mark.parametrize("empty_new", [False, True])
def test_no_candidate_or_new_memory_is_explicitly_skipped(monkeypatch, empty_new):
    writer, store, embedder, seen = reconciler(monkeypatch, [])
    result = asyncio.run(invoke(writer, store, embedder, [] if empty_new else None))
    assert result.success and seen == []
    assert result.input_capture == {
        "schema_version": 1,
        "status": "complete",
        "stages": [
            {
                "stage": "memory_decision",
                "status": "skipped",
                "inputs": [],
                "rendered": "",
                "output_id": None,
            }
        ],
    }


@pytest.mark.parametrize("damage", ["bytes", "records", "metadata", "embedding"])
def test_capture_failure_preserves_native_result_without_partial_evidence(monkeypatch, damage):
    current = node("a")
    if damage == "bytes":
        monkeypatch.setattr(ReconcileInputCapture, "MAX_BYTES", 64)
    elif damage == "records":
        monkeypatch.setattr(ReconcileInputCapture, "MAX_RECORDS", 0)
    elif damage == "metadata":
        current.custom["synthetic_invalid"] = object()
    writer, store, embedder, seen = reconciler(monkeypatch, [current])
    if damage == "embedding":

        async def broken(*args, **kwargs):
            raise OSError("synthetic embedding failure")

        monkeypatch.setattr(embedder, "embed_batch", broken)
    result = asyncio.run(invoke(writer, store, embedder))
    assert result.success and result.degraded == (damage == "embedding")
    assert bool(seen) == (damage != "embedding")
    assert result.input_capture == {"schema_version": 1, "status": "unavailable", "stages": []}


def test_handled_empty_search_does_not_invent_a_model_call(monkeypatch):
    writer, store, embedder, seen = reconciler(monkeypatch, [node("a")])

    async def broken(*args, **kwargs):
        raise OSError("synthetic search failure")

    monkeypatch.setattr(ReconcileHybridRetriever, "search_candidates_batch", broken)
    result = asyncio.run(invoke(writer, store, embedder))
    # Upstream handles failed hybrid retrieval as an empty result. The model
    # was skipped; this does not assert that candidate discovery succeeded.
    assert result.success and seen == []
    assert result.input_capture["stages"][0]["status"] == "skipped"


def test_one_reconciler_keeps_concurrent_capture_state_separate(monkeypatch):
    records = [node(str(index)) for index in range(8)]
    writer, store, embedder, seen = reconciler(monkeypatch, records)

    async def search(self, queries, embeddings, **kwargs):
        await asyncio.sleep(0)
        return [[{"node": records[int(query)], "score": 0.9}] for query in queries]

    monkeypatch.setattr(ReconcileHybridRetriever, "search_candidates_batch", search)

    async def scenario():
        return await asyncio.gather(
            *[invoke(writer, store, embedder, [str(index)]) for index in range(8)]
        )

    results = asyncio.run(scenario())
    assert len(seen) == 8
    assert [result.input_capture["stages"][0]["inputs"][0]["id"] for result in results] == [
        str(index) for index in range(8)
    ]


@pytest.mark.parametrize("content", ["合成当前对话", "Synthetic current chat"])
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("intention", [False, True])
def test_unused_legacy_arguments_do_not_enter_actual_extract_prompt(content, custom, intention):
    seen = []

    async def complete(**kwargs):
        seen.append(copy.deepcopy(kwargs["messages"]))
        return LLMResponse(
            content='{"memory": [], "basic_info": {}, "intentions": []}',
            tokens_used=1,
            prompt_tokens=1,
            completion_tokens=0,
        )

    config = GlobalLLMConfig(
        enable_intention=intention, few_shot_enabled=True, structure_metadata_enabled=True
    )
    if custom:
        config.extract_system_prompt_en = config.extract_system_prompt_zh = "Synthetic system"
    extractor = Extractor(SimpleNamespace(complete_messages=complete), config)

    async def scenario():
        for tags, fields in [
            ([], {}),
            (["UNUSED_TAG_SENTINEL"], {"UNUSED_FIELD": "UNUSED_SCHEMA"}),
        ]:
            result = await extractor.extract(
                content=content,
                existing_tags=tags,
                basic_profile_fields=fields,
                history_context="ACTUAL_HISTORY_SENTINEL",
            )
            assert result.success

    asyncio.run(scenario())
    assert seen[0] == seen[1]
    assert "ACTUAL_HISTORY_SENTINEL" in seen[0][1]["content"]
    assert "UNUSED_" not in str(seen)


@pytest.mark.parametrize("method", ["add", "add_extracted"])
@pytest.mark.parametrize("operation", ["ADD", "UPDATE"])
def test_real_writer_durable_result_and_replay(tmp_path, monkeypatch, method, operation):
    config = config_for(tmp_path)
    requests = [str(uuid4()), str(uuid4())]
    calls, results = [], []
    target = {}

    async def complete(**kwargs):
        calls.append(kwargs["prompt"])
        op = {"op": operation, "content": "Synthetic fact"}
        if operation == "UPDATE":
            op.update(memory_id=target["id"], content="Synthetic revised fact")
        return LLMResponse(
            content=json.dumps([op]),
            tokens_used=1,
            prompt_tokens=1,
            completion_tokens=0,
        )

    async def extract(self, **kwargs):
        return AgentResult(success=True, extracted_info={"memory": [{"content": "Synthetic fact"}]})

    monkeypatch.setattr(
        MemoryReconciler, "_get_llm", lambda self: SimpleNamespace(complete=complete)
    )
    monkeypatch.setattr(MemAgent, "process_add", extract)

    async def scenario():
        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            first = await _joined_thread(
                sdk.add_extracted,
                "Synthetic fact",
                user_id="u",
                agent_id="a",
                request_id=requests[0],
            )
            assert first["success"]
            target["id"] = first["memory_id"]
            assert first["chat_reconcile_inputs"]["stages"][0]["status"] == "skipped"
            result = await _joined_thread(
                getattr(sdk, method),
                "Synthetic fact",
                user_id="u",
                agent_id="a",
                request_id=requests[1],
            )
            assert result["success"] and len(calls) == 1
            capture = result["chat_reconcile_inputs"]
            assert capture["status"] == "complete"
            assert capture["stages"][0]["rendered"] == calls[0]
            assert capture["stages"][0]["inputs"][0]["id"] == first["memory_id"]
            assert capture["stages"][0]["inputs"][0]["record"]["content"] == "Synthetic fact"
            assert (
                verified_origin("memory", capture["stages"][0]["inputs"][0]["record"])
                == requests[0]
            )
            if operation == "UPDATE":
                current = await _joined_thread(
                    lambda: sdk._loop_thread.run(sdk._vector_store.get_by_id(target["id"]))
                )
                assert current.content == "Synthetic revised fact"
                assert current.node_id == target["id"]
                assert verified_origin("memory", current.to_dict()) == requests[1]
            results.append(result)

        def forbidden(*args, **kwargs):
            pytest.fail("replay called extraction or reconciliation")

        monkeypatch.setattr(MemoryReconciler, "reconcile", forbidden)
        monkeypatch.setattr(MemAgent, "process_add", forbidden)
        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            replay = await _joined_thread(
                getattr(sdk, method),
                "Synthetic fact",
                user_id="u",
                agent_id="a",
                request_id=requests[1],
            )
            assert replay["chat_reconcile_inputs"] == results[0]["chat_reconcile_inputs"]
            assert len(calls) == 1

    asyncio.run(scenario())


def test_direct_store_reports_no_reconciliation(tmp_path, monkeypatch):
    from hy_memory.pipelines import writer as writer_module

    monkeypatch.setattr(writer_module, "_RECONCILE_ENABLED", False)

    async def forbidden(*args, **kwargs):
        pytest.fail("direct storage called reconciliation")

    monkeypatch.setattr(MemoryReconciler, "reconcile", forbidden)

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            result = await _joined_thread(
                sdk.add_extracted,
                "Synthetic direct storage",
                user_id="u",
                request_id=str(uuid4()),
            )
            assert result["success"]
            assert result["chat_reconcile_inputs"] == {
                "schema_version": 1,
                "status": "not_used",
                "stages": [],
            }

    asyncio.run(scenario())
