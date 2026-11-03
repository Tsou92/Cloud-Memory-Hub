import copy
import sqlite3
from types import SimpleNamespace

import pytest
from cloud_memory_hub.governance_inventory import _node
from cloud_memory_hub.memory_evidence import (
    capture_memory,
    commit_memory,
    initialize_memory,
    stored_memory,
)
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.sdk_reconcile import proven_write
from hy_memory.coding.record_origin import stamp_memory_origin
from hy_memory.models.memory import MemoryNode


def sample():
    node = stamp_memory_origin(
        MemoryNode(user_id="synthetic", content="Synthetic fact"), "producer"
    )
    record = node.to_dict()
    record.pop("embedding")
    return {
        "scene": "chat",
        "chat_reconcile_inputs": {
            "schema_version": 1,
            "status": "complete",
            "stages": [
                {
                    "stage": "memory_decision",
                    "status": "used",
                    "output_id": None,
                    "rendered": "Synthetic prompt",
                    "inputs": [
                        {
                            "kind": "memory",
                            "id": node.node_id,
                            "record": record,
                        }
                    ],
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "damage",
    [
        "scene",
        "schema",
        "empty_complete",
        "not_used",
        "pending",
        "output",
        "skipped",
        "kind",
        "layer",
        "field",
        "embedding",
        "scope",
        "self",
        "encoding",
    ],
)
def test_evidence_rejects_invalid_shapes_and_claims(damage):
    value = sample()
    capture = value["chat_reconcile_inputs"]
    stage = capture["stages"][0]
    item = stage["inputs"][0]
    request = SimpleNamespace(user_id="synthetic", request_id="consumer")
    if damage == "scene":
        value["scene"] = "productivity"
    elif damage == "schema":
        capture["schema_version"] = True
    elif damage == "empty_complete":
        capture["stages"] = []
    elif damage == "not_used":
        capture["status"] = "not_used"
    elif damage in {"pending", "skipped"}:
        stage["status"] = damage
    elif damage == "output":
        stage["output_id"] = "synthetic"
    elif damage == "kind":
        item["kind"] = "fact"
    elif damage == "layer":
        item["record"]["layer"] = []
    elif damage == "field":
        item["record"]["unexpected"] = True
    elif damage == "embedding":
        item["record"]["embedding"] = [0.1]
    elif damage == "scope":
        request.user_id = "other"
    elif damage == "self":
        request.request_id = "producer"
    else:
        item["record"]["confidence"] = float("nan")
    with pytest.raises(ValueError):
        capture_memory(value, request)


def test_legacy_record_fields_and_immutable_archive_version_checks():
    value = sample()
    item = value["chat_reconcile_inputs"]["stages"][0]["inputs"][0]["record"]
    item.pop("origin_request_id")
    item.pop("origin_digest")
    captured = capture_memory(value, None)
    with sqlite3.connect(":memory:") as db:
        initialize_memory(db)
        commit_memory(db, "receipt", captured, None)
        commit_memory(db, "receipt", captured, None)
        assert stored_memory(db, "receipt", None) == captured
        changed = copy.deepcopy(captured)
        changed["capture"]["stages"][0]["rendered"] += " changed"
        with pytest.raises(ValueError, match="lineage_memory_changed"):
            commit_memory(db, "receipt", changed, None)
        db.execute("UPDATE cmh_memory_inputs_schema SET version=2")
        with pytest.raises(AccessError, match="version_unsupported"):
            initialize_memory(db)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE cmh_event_memory_inputs(id TEXT)")
        with pytest.raises(AccessError, match="store_unrecognized"):
            initialize_memory(db)


def test_memory_stage_ids_cannot_be_sdk_nodes_or_outputs():
    import asyncio

    from cloud_memory_hub.governance_journal import GovernanceError

    with pytest.raises(GovernanceError):
        _node({"user_id": "synthetic", "node_id": "cmhm:receipt"}, "synthetic")
    request = SimpleNamespace(request_id="r", user_id="u", agent_id="a", session_id="s")

    async def outcome(identity):
        return dict(
            vars(request),
            status="succeeded",
            operation="add",
            result={
                "request_id": "r",
                "scene": "chat",
                "success": True,
                "memory_id": "cmhm:receipt",
                "memory_ids": [],
            },
        )

    with pytest.raises(ValueError, match="reserved_identity"):
        asyncio.run(
            proven_write(SimpleNamespace(get_sdk_write_outcome=outcome), "job", request, "add")
        )
