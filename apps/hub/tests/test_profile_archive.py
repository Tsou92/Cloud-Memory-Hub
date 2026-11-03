import copy
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.profile_evidence import (
    capture_profile,
    commit_profile,
    initialize_profile,
    stored_profile,
)
from cloud_memory_hub.remote_access import AccessError
from hy_memory.agent.tools.profile_capture import ProfileInputCapture
from hy_memory.coding.record_origin import stamp_profile_origin
from hy_memory.models.memory import MemoryLayer, MemoryNode


def sample():
    old = stamp_profile_origin(
        MemoryNode(
            node_id=str(uuid4()),
            user_id="synthetic",
            agent_id="",
            session_id="",
            layer=MemoryLayer.L0_BASIC_INFO,
            content="Synthetic age 30",
            custom={"basic_info_kv": {"age": 30}},
        ),
        "producer",
    )
    capture = {}
    evidence = ProfileInputCapture(capture)
    evidence.decision([old], ["age"], {"age": 31}, {"age": 30}, [old])
    output = str(uuid4())
    for key in ("attempted_output_ids", "output_ids"):
        evidence.effect(key, output)
    for key in ("attempted_supersede_ids", "superseded_ids"):
        evidence.effect(key, old.node_id)
    evidence.finish(SimpleNamespace(success=True))
    return {"scene": "chat", "memory_id": str(uuid4()), "basic_profile_inputs": capture}


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "scene",
        "stage",
        "kind",
        "duplicate",
        "heads",
        "scope",
        "self",
        "digest",
        "kv_type",
        "reserved",
        "collision",
        "unconfirmed",
        "flag",
        "not_used",
        "encoding",
    ],
)
def test_malformed_profile_claims_are_not_archived(damage):
    result = sample()
    capture = result["basic_profile_inputs"]
    stage = capture["stages"][0]
    item = stage["inputs"][0]
    request = SimpleNamespace(user_id="synthetic", request_id="consumer")
    if damage == "schema":
        capture["schema_version"] = True
    elif damage == "scene":
        result["scene"] = "productivity"
    elif damage == "stage":
        stage["status"] = "fallback"
    elif damage == "kind":
        item["kind"] = "memory"
    elif damage == "duplicate":
        stage["inputs"].append(copy.deepcopy(item))
    elif damage == "heads":
        rule = json.loads(stage["rendered"])
        rule["head_ids"] = ["not-a-candidate"]
        stage["rendered"] = json.dumps(rule)
    elif damage == "scope":
        request.user_id = "other"
    elif damage == "self":
        request.request_id = "producer"
    elif damage == "digest":
        item["record"]["origin_digest"] = "0" * 64
    elif damage == "kv_type":
        item["record"]["custom"]["basic_info_kv"]["age"] = "30"
    elif damage == "reserved":
        capture["output_ids"] = capture["attempted_output_ids"] = ["cmhm:other"]
    elif damage == "collision":
        result["memory_id"] = capture["output_ids"][0]
    elif damage == "unconfirmed":
        capture["superseded_ids"] = []
    elif damage == "flag":
        capture["effects_complete"] = False
    elif damage == "not_used":
        capture.update(status="not_used", stages=[])
    else:
        item["record"]["confidence"] = float("nan")
    with pytest.raises(ValueError):
        capture_profile(result, request)


def test_immutable_side_index_and_missing_legacy_fields():
    value = capture_profile(sample(), None)
    with sqlite3.connect(":memory:") as db:
        initialize_profile(db)
        commit_profile(db, "receipt", value, None)
        commit_profile(db, "receipt", value, None)
        assert stored_profile(db, "receipt", None) == value
        assert db.execute("SELECT count(*) FROM cmh_event_profile_outputs").fetchone()[0] == 1
        db.commit()
        with pytest.raises(sqlite3.IntegrityError), db:
            commit_profile(db, "other-producer", value, None)
        assert stored_profile(db, "other-producer", None) is None
        assert stored_profile(db, "receipt", None) == value
        changed = copy.deepcopy(value)
        changed["capture"]["stages"][0]["rendered"] += " "
        with pytest.raises(ValueError, match="changed"):
            commit_profile(db, "receipt", changed, None)
        old = capture_profile({"scene": "chat"}, None)
        assert old["capture"] is None
        commit_profile(db, "legacy", old, None)
        assert stored_profile(db, "legacy", None) == old
        db.execute("DELETE FROM cmh_event_profile_outputs WHERE receipt_id='receipt'")
        with pytest.raises(ValueError, match="index_invalid"):
            stored_profile(db, "receipt", None)


def test_partial_effects_are_preserved_with_explicit_incomplete_status():
    result = sample()
    capture = result["basic_profile_inputs"]
    capture.update(status="unavailable", stages=[], superseded_ids=[])
    value = capture_profile(result, None)
    with sqlite3.connect(":memory:") as db:
        initialize_profile(db)
        commit_profile(db, "partial", value, None)
        saved = stored_profile(db, "partial", None)["capture"]
        assert saved["output_ids"] and saved["attempted_supersede_ids"]
        assert saved["superseded_ids"] == [] and saved["status"] == "unavailable"


def test_unknown_profile_schema_and_partial_store_are_rejected():
    with sqlite3.connect(":memory:") as db:
        initialize_profile(db)
        db.execute("UPDATE cmh_profile_inputs_schema SET version=2")
        with pytest.raises(AccessError, match="version_unsupported"):
            initialize_profile(db)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE cmh_event_profile_outputs(id TEXT)")
        with pytest.raises(AccessError, match="store_unrecognized"):
            initialize_profile(db)
