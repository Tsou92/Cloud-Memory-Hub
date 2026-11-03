import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from hy_memory.agent.tools.basic_profile import upsert_basic_profile
from hy_memory.agent.tools.profile_capture import ProfileInputCapture
from hy_memory.coding.input_capture import ReconcileInputCapture
from hy_memory.coding.record_origin import stamp_profile_origin, verified_origin
from hy_memory.data.vector_store_base import VectorStoreBase
from hy_memory.data.vector_store_chroma import ChromaVectorStore
from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus


class Store:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)
        self.reads = 0
        self.fail = None

    async def list_by_user(self, **kwargs):
        self.reads += 1
        assert kwargs["with_vectors"] is False and kwargs["limit"] == 1000
        if self.fail == "read":
            raise RuntimeError("synthetic read failure")
        return list(self.nodes)

    async def upsert(self, node):
        self.nodes.append(node)
        if self.fail == "upsert":
            raise RuntimeError("synthetic error after write")
        return node.node_id

    async def update_payload(self, node_id, updates):
        if self.fail == "supersede":
            raise RuntimeError("synthetic supersede error")
        if self.fail == "false":
            return False
        node = next(n for n in self.nodes if n.node_id == node_id)
        for key, value in updates.items():
            setattr(node, key, MemoryStatus(value) if key == "status" else value)
        return True


async def update(store, kv, *, request="synthetic-request", capture=None, embed=None):
    return await upsert_basic_profile(
        user_id="synthetic-user",
        kv=kv,
        vector_store=store,
        embed_service=embed
        or type("Embed", (), {"embed_queued": AsyncMock(return_value=[1, 0])})(),
        request_id=request,
        capture=capture,
    )


def test_profile_diff_and_noop_freeze_actual_pre_mutation_state():
    async def scenario():
        store, first, second, unchanged = Store(), {}, {}, {}
        a = await update(store, {"name": "Synthetic", "age": 30}, capture=first, request="first")
        assert first["status"] == "complete"
        assert first["output_ids"] == first["attempted_output_ids"] == [a.node_id]
        assert first["stages"][0]["inputs"] == []
        b = await update(store, {"name": "Synthetic", "age": 31}, capture=second, request="second")
        assert b.diff_kv == {"age": 31}
        assert store.nodes[1].custom["basic_info_kv"] == {"age": 31}
        assert store.nodes[0].is_latest is False
        assert second["superseded_ids"] == second["attempted_supersede_ids"] == [a.node_id]
        frozen = second["stages"][0]["inputs"][0]["record"]
        assert frozen["is_latest"] is True
        assert "embedding" not in frozen
        assert frozen["custom"]["basic_info_kv"] == {"age": 30, "name": "Synthetic"}
        assert verified_origin("profile", frozen) == "first"
        rule = json.loads(second["stages"][0]["rendered"])
        assert rule["existing_kv"] == {"age": 30, "name": "Synthetic"}
        assert rule["head_ids"] == [a.node_id]
        result = await update(store, {"age": 31}, capture=unchanged)
        assert result.success and not result.changed
        assert unchanged["status"] == "complete" and unchanged["output_ids"] == []
        assert len(unchanged["stages"][0]["inputs"]) == 2
        assert len(store.nodes) == 2 and store.reads == 3
        for node, request in zip(store.nodes, ("first", "second"), strict=True):
            assert node.agent_id == node.session_id == ""
            assert verified_origin("profile", node.to_dict()) == request
            with pytest.raises(ValueError):
                verified_origin("memory", node.to_dict())
        assert "basic_profile_inputs" not in b.to_dict()

    asyncio.run(scenario())


def test_legacy_migration_copies_full_kv_and_marks_all_heads():
    async def scenario():
        store = Store()
        await update(store, {"name": "Synthetic", "age": 30})
        old = store.nodes[0]
        old.agent_id = "legacy-agent"
        capture = {}
        result = await update(store, {"age": 30}, capture=capture)
        assert result.success and result.changed and not result.diff_kv
        assert store.nodes[-1].custom["basic_info_kv"] == {"name": "Synthetic", "age": 30}
        assert capture["stages"][0]["inputs"][0]["record"]["agent_id"] == "legacy-agent"
        assert capture["superseded_ids"] == [old.node_id]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["read", "embed", "upsert", "supersede", "false"])
def test_failures_do_not_disguise_partial_effects_or_change_native_result(failure):
    async def scenario():
        store, capture = Store(), {}
        await update(store, {"age": 30})
        store.fail = failure
        embed = type("Embed", (), {"embed_queued": AsyncMock(side_effect=RuntimeError("embed"))})()
        if failure == "read":
            with pytest.raises(RuntimeError, match="read failure"):
                await update(store, {"age": 31}, capture=capture)
        else:
            result = await update(
                store, {"age": 31}, capture=capture, embed=embed if failure == "embed" else None
            )
            assert result.success is (failure in {"supersede", "false"})
        assert capture["status"] == "unavailable" and capture["stages"] == []
        assert capture["effects_complete"] is True
        assert len(capture["attempted_output_ids"]) == int(failure not in {"read", "embed"})
        assert len(capture["output_ids"]) == int(failure in {"supersede", "false"})
        assert capture["superseded_ids"] == []
        assert len(capture["attempted_supersede_ids"]) == int(failure in {"supersede", "false"})

    asyncio.run(scenario())


def test_over_budget_and_unserializable_input_preserve_write(monkeypatch):
    async def scenario():
        store = Store()
        await update(store, {"age": 30})
        for mode in ("records", "bytes", "serialization"):
            capture = {}
            with monkeypatch.context() as local:
                if mode == "records":
                    local.setattr(ReconcileInputCapture, "MAX_RECORDS", 0)
                elif mode == "bytes":
                    local.setattr(ReconcileInputCapture, "MAX_BYTES", 400)
                else:
                    store.nodes[0].custom["invalid"] = object()
                result = await update(store, {"age": 31 + len(store.nodes)}, capture=capture)
            assert result.success
            assert capture["status"] == "unavailable" and capture["stages"] == []
            assert capture["output_ids"] == [result.node_id]

    asyncio.run(scenario())


def test_unbounded_effect_ids_never_look_like_complete_zero_side_effects(monkeypatch):
    async def scenario():
        capture = {}
        monkeypatch.setattr(ProfileInputCapture, "MAX_EFFECTS", 0)
        result = await update(Store(), {"age": 30}, capture=capture)
        assert result.success
        assert capture["status"] == "unavailable" and capture["effects_complete"] is False

    asyncio.run(scenario())


def test_partial_supersede_keeps_successful_and_failed_ids_separate():
    async def scenario():
        store = Store()
        await update(store, {"age": 30})
        second = copy.deepcopy(store.nodes[0])
        second.node_id = "second-legacy-head"
        store.nodes.append(second)
        native = store.update_payload

        async def partial(identity, changes):
            if identity == second.node_id:
                raise RuntimeError("one head failed")
            return await native(identity, changes)

        store.update_payload = partial
        capture = {}
        result = await update(store, {"age": 31}, capture=capture)
        assert result.success
        assert capture["status"] == "unavailable"
        assert capture["output_ids"] == [result.node_id]
        assert set(capture["attempted_supersede_ids"]) == {store.nodes[0].node_id, second.node_id}
        assert capture["superseded_ids"] == [store.nodes[0].node_id]

    asyncio.run(scenario())


def test_cancellation_after_write_preserves_attempt_and_propagates():
    async def scenario():
        store, capture = Store(), {}
        native = store.upsert

        async def cancelled(node):
            await native(node)
            raise asyncio.CancelledError()

        store.upsert = cancelled
        with pytest.raises(asyncio.CancelledError):
            await update(store, {"age": 30}, capture=capture)
        assert capture["status"] == "unavailable"
        assert capture["attempted_output_ids"] == [store.nodes[0].node_id]
        assert capture["output_ids"] == []

    asyncio.run(scenario())


def test_nonfinite_kv_preserves_native_write_but_has_no_valid_origin():
    async def scenario():
        store, capture = Store(), {}
        result = await update(store, {"location": float("inf")}, capture=capture)
        assert result.success and capture["status"] == "unavailable"
        assert capture["output_ids"] == [result.node_id]
        assert verified_origin("profile", store.nodes[0].to_dict()) is None

    asyncio.run(scenario())


def test_empty_sanitized_input_skips_read_and_parallel_calls_remain_separate():
    async def scenario():
        store, empty = Store(), {}
        result = await update(store, {"ignored": "value"}, capture=empty)
        assert result.success and not result.changed and store.reads == 0
        assert empty["status"] == "complete" and not empty["output_ids"]
        captures = [{}, {}]
        results = await asyncio.gather(
            *(update(store, {"age": 30 + i}, capture=captures[i], request=str(i)) for i in range(2))
        )
        assert [c["output_ids"] for c in captures] == [[r.node_id] for r in results]
        assert captures[1]["superseded_ids"] == [results[0].node_id]
        assert len([n for n in store.nodes if n.is_latest]) == 1

    asyncio.run(scenario())


def test_profile_digest_includes_kv_types_and_roundtrips():
    node = stamp_profile_origin(
        MemoryNode(
            node_id="synthetic",
            user_id="u",
            content="Synthetic age 30",
            layer=MemoryLayer.L0_BASIC_INFO,
            custom={"basic_info_kv": {"age": 30}},
        ),
        "request",
    )
    assert verified_origin("profile", MemoryNode.from_dict(node.to_dict()).to_dict()) == "request"
    changed = copy.deepcopy(node.to_dict())
    changed["custom"]["basic_info_kv"]["age"] = "30"
    with pytest.raises(ValueError):
        verified_origin("profile", changed)
    node.status = MemoryStatus.SUPERSEDED
    assert verified_origin("profile", node.to_dict()) == "request"


def test_native_chroma_empty_user_level_scope_keeps_the_producer():
    node = stamp_profile_origin(
        MemoryNode(
            node_id="synthetic",
            user_id="u",
            agent_id="",
            session_id="",
            content="Synthetic",
            layer=MemoryLayer.L0_BASIC_INFO,
            custom={"basic_info_kv": {"age": 30}},
        ),
        "request",
    )
    payload = VectorStoreBase._node_to_payload(node)
    metadata = ChromaVectorStore._payload_to_chroma_metadata(payload)
    restored = VectorStoreBase._payload_to_node(
        ChromaVectorStore._chroma_metadata_to_payload(metadata)
    )
    assert restored.agent_id is restored.session_id is None
    assert verified_origin("profile", restored.to_dict()) == "request"
    restored.agent_id = "another-agent"
    with pytest.raises(ValueError):
        verified_origin("profile", restored.to_dict())


@pytest.mark.parametrize("producer", [None, "", "x" * 513, object()])
def test_legacy_or_invalid_origin_does_not_break_native_profile_write(producer):
    async def scenario():
        store = Store()
        result = await update(store, {"age": 30}, request=producer)
        assert result.success and verified_origin("profile", store.nodes[0].to_dict()) is None

    asyncio.run(scenario())
