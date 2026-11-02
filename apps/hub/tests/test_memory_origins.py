import asyncio
import copy
from datetime import datetime
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hy_memory.agent.reconciler import ReconcileOp, ReconcileResult
from hy_memory.coding.record_origin import (
    invalidate_memory_origin,
    memory_update_origin,
    stamp_memory_origin,
    verified_origin,
)
from hy_memory.config import MemoryConfig
from hy_memory.data.vector_store import create_vector_store
from hy_memory.data.vector_store_base import VectorStoreBase
from hy_memory.models.memory import MemoryLayer, MemoryNode
from hy_memory.pipelines.base import WriteRequest
from hy_memory.pipelines.writer import MemoryWriter


def memory():
    return MemoryNode(
        node_id=str(uuid4()),
        user_id="u",
        agent_id="a",
        session_id="s",
        layer=MemoryLayer.L2_FACT,
        content="Synthetic fact",
        gmt_created=datetime(2025, 1, 1),
        memory_at=datetime(2025, 1, 1),
        tags=["synthetic"],
        custom={"topic_paths": ["synthetic/topic"]},
        embedding=[1.0, 0.0],
    )


def test_semantic_origin_survives_node_and_store_payload_roundtrips():
    node = stamp_memory_origin(memory(), "request-one")
    restored = VectorStoreBase._payload_to_node(VectorStoreBase._node_to_payload(node))
    assert verified_origin("memory", restored.to_dict()) == "request-one"
    second = MemoryNode.from_dict(node.to_dict())
    assert verified_origin("memory", second.to_dict()) == "request-one"
    legacy = node.to_dict()
    legacy.pop("origin_request_id")
    legacy.pop("origin_digest")
    assert verified_origin("memory", MemoryNode.from_dict(legacy).to_dict()) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_id", "another-id"),
        ("user_id", "another-user"),
        ("agent_id", "another-agent"),
        ("session_id", "another-session"),
        ("source", "another-source"),
        ("owner", "agent"),
        ("layer", "L3_SUMMARY"),
        ("content", "Changed meaning"),
        ("tags", ["changed"]),
        ("memory_at", 123),
        ("gmt_created", 123),
        ("custom", {"event_facet": {"what": "changed"}}),
    ],
)
def test_changed_semantics_cannot_keep_a_previous_producer(field, value):
    record = stamp_memory_origin(memory(), "request-one").to_dict()
    record[field] = value
    with pytest.raises(ValueError):
        verified_origin("memory", record)


def test_auxiliary_mutation_does_not_change_semantic_producer():
    record = stamp_memory_origin(memory(), "request-one").to_dict()
    record.update(access_count=5, gmt_modified=123, is_latest=False, status="superseded")
    record["embedding"] = [0.0, 1.0]
    record["custom"]["auxiliary_counter"] = 8
    assert verified_origin("memory", record) == "request-one"


@pytest.mark.parametrize("invalid", [None, "", "x" * 513, object()])
def test_invalid_request_does_not_fabricate_origin_or_fail_the_write(invalid):
    node = stamp_memory_origin(memory(), invalid)
    assert node.content == "Synthetic fact"
    assert verified_origin("memory", node.to_dict()) is None


def test_invalid_structure_and_missing_update_state_remain_unproven():
    node = memory()
    node.custom["topic_paths"] = object()
    stamp_memory_origin(node, "request")
    assert node.origin_request_id is node.origin_digest is None
    updates = {"content": "New fact", "tags": ["new"]}
    frozen = copy.deepcopy(updates)
    result = memory_update_origin(None, updates, "request")
    assert updates == frozen
    assert result["origin_request_id"] is result["origin_digest"] is None
    assert result["content"] == updates["content"]


def test_payload_change_invalidates_partial_and_missing_origin_pairs():
    for updates in (
        {"content": "Changed"},
        {"tags": ["new"]},
        {"custom": {"counter": 1}},
        {"origin_request_id": "not-a-proof"},
    ):
        frozen = copy.deepcopy(updates)
        result = invalidate_memory_origin(updates)
        assert result["origin_request_id"] is result["origin_digest"] is None
        assert updates == frozen
    assert invalidate_memory_origin({"access_count": 1}) == {"access_count": 1}


def backend_config(root, provider):
    package = {"faiss": "faiss", "qdrant": "qdrant_client"}.get(provider)
    if package and find_spec(package) is None:
        pytest.skip("optional backend tested by check-memory-origins")
    return MemoryConfig.from_dict(
        {
            "vector_store": {
                "provider": provider,
                "persist_directory": str(root / provider),
                "collection_name": "synthetic_origins",
                "embedding_dims": 2,
            },
            "llm": {"structure_metadata_enabled": True},
            "embedder": {"embedding_dims": 2},
        }
    )


@pytest.mark.parametrize("provider", ["chroma", "faiss", "qdrant"])
def test_backend_persistence_and_untracked_update_invalidation(tmp_path, provider):
    config = backend_config(tmp_path, provider)
    first = stamp_memory_origin(memory(), "request-one")

    async def scenario():
        store = create_vector_store(config)
        await store.initialize()
        try:
            await store.upsert(first)
            assert await store.update_payload(first.node_id, {"access_count": 1})
            node = await store.get_by_id(first.node_id)
            assert verified_origin("memory", node.to_dict()) == "request-one"
            changes = memory_update_origin(
                node, {"content": "Updated fact", "embedding": [0.0, 1.0]}, "request-two"
            )
            assert await store.update_payload(first.node_id, changes)
        finally:
            await store.close()
        store = create_vector_store(config)
        await store.initialize()
        try:
            node = await store.get_by_id(first.node_id)
            assert node.content == "Updated fact"
            assert verified_origin("memory", node.to_dict()) == "request-two"
            assert await store.update_payload(
                first.node_id, {"content": "Untracked fact", "embedding": [1.0, 0.0]}
            )
            node = await store.get_by_id(first.node_id)
            assert node.content == "Untracked fact"
            assert verified_origin("memory", node.to_dict()) is None
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["chroma", "faiss", "qdrant"])
def test_native_writer_add_update_supersede_and_direct_storage(tmp_path, provider):
    config = backend_config(tmp_path, provider)
    embedder = SimpleNamespace(
        embed_batch=AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts]),
        embed_queued=AsyncMock(return_value=[1.0, 0.0]),
    )
    writer = MemoryWriter(config, embed_service=embedder)
    writer._reconciler = SimpleNamespace(reconcile=AsyncMock())
    request = WriteRequest(content="Synthetic request", user_id="u", agent_id="a", session_id="s")

    async def apply(store, operations, request_id):
        writer._reconciler.reconcile.return_value = ReconcileResult(ops=operations, success=True)
        result = await writer._reconcile_and_store(
            new_memory_texts=[op.content for op in operations],
            new_memories_meta=[{"content": op.content} for op in operations],
            request=request,
            vector_store=store,
            req_id=request_id,
        )
        assert result[1] is None
        nodes = await store.get_by_ids(result[0])
        assert len(nodes) == len(operations)
        assert all(verified_origin("memory", node.to_dict()) == request_id for node in nodes)
        return nodes

    async def scenario():
        store = create_vector_store(config)
        await store.initialize()
        try:
            nodes = await apply(
                store,
                [
                    ReconcileOp(op="ADD", content="First fact"),
                    ReconcileOp(op="ADD", content="Second fact"),
                ],
                "request-add",
            )
            nodes = await apply(
                store,
                [
                    ReconcileOp(
                        op="UPDATE", content="Updated " + node.content, memory_id=node.node_id
                    )
                    for node in nodes
                ],
                "request-update",
            )
            prior = nodes[0]
            heads = await apply(
                store,
                [ReconcileOp(op="SUPERSEDE", content="Replacement fact", memory_id=prior.node_id)],
                "request-supersede",
            )
            assert heads[0].node_id != prior.node_id
            old = await store.get_by_id(prior.node_id)
            assert verified_origin("memory", old.to_dict()) == "request-update"
            direct = await writer._direct_store(
                [{"content": "Direct fact"}], request, store, "request-direct"
            )
            assert direct[1] is None
            stored = await store.get_by_id(direct[0][0])
            assert verified_origin("memory", stored.to_dict()) == "request-direct"
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("batch", [False, True])
def test_failed_old_node_read_cannot_retain_an_old_producer(tmp_path, monkeypatch, batch):
    config = backend_config(tmp_path, "qdrant" if batch else "chroma")
    records = [stamp_memory_origin(memory(), "old-request") for _ in range(2 if batch else 1)]
    embedder = SimpleNamespace(
        embed_batch=AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts]),
        embed_queued=AsyncMock(return_value=[1.0, 0.0]),
    )
    writer = MemoryWriter(config, embed_service=embedder)
    writer._reconciler = SimpleNamespace(
        reconcile=AsyncMock(
            return_value=ReconcileResult(
                ops=[
                    ReconcileOp(op="UPDATE", memory_id=node.node_id, content="Unproven update")
                    for node in records
                ],
                success=True,
            )
        )
    )

    async def scenario():
        store = create_vector_store(config)
        await store.initialize()
        try:
            await store.upsert_batch(records)
            with monkeypatch.context() as patch:
                patch.setattr(store, "get_by_id", AsyncMock(side_effect=OSError("synthetic")))
                patch.setattr(store, "get_by_ids", AsyncMock(side_effect=OSError("synthetic")))
                result = await writer._reconcile_and_store(
                    new_memory_texts=["Unproven update"],
                    new_memories_meta=[{"content": "Unproven update"}],
                    request=WriteRequest(
                        content="Synthetic", user_id="u", agent_id="a", session_id="s"
                    ),
                    vector_store=store,
                    req_id="new-request",
                )
                assert result[1] is None
            for original in records:
                stored = await store.get_by_id(original.node_id)
                assert stored.content == "Unproven update"
                assert verified_origin("memory", stored.to_dict()) is None
        finally:
            await store.close()

    asyncio.run(scenario())
