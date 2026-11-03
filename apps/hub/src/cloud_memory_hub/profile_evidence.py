"""Archive the native user-level profile side path without changing old output digests."""

from hy_memory.agent.tools.basic_profile import (
    _assemble_full_kv_from_nodes,
    _latest_l0_heads_from_nodes,
    _sanitize_kv,
)
from hy_memory.coding.record_origin import verified_origin
from hy_memory.models.memory import MemoryNode
from one_memory.hy_memory_bridge import _joined_thread

from cloud_memory_hub.evidence_archive import EvidenceArchive
from cloud_memory_hub.native_evidence import validate_native_record
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.reconcile_evidence import MAX_BYTES, MAX_ITEMS, encode
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.sdk_reconcile import RESERVED_PREFIXES, _ids

LAYER = "l0_basic_info"
PREFIX = "cmhb:"
EFFECTS = ("attempted_output_ids", "output_ids", "attempted_supersede_ids", "superseded_ids")


def _identities(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("lineage_profile_effects_invalid")
    # Native head lists allow 1024; validate each ID with the existing Hub contract.
    if any(len(_ids([item])) != 1 or item.startswith(RESERVED_PREFIXES) for item in value):
        raise ValueError("lineage_profile_effects_invalid")
    if len(set(value)) != len(value):
        raise ValueError("lineage_profile_effects_invalid")
    return set(value)


def validate(value, request=None):
    if (
        not isinstance(value, dict)
        or set(value) != {"scene", "capture"}
        or value["scene"] not in ("chat", "productivity")
    ):
        raise ValueError("lineage_profile_invalid")
    capture = value["capture"]
    if capture is None:
        return value
    if (
        value["scene"] != "chat"
        or not isinstance(capture, dict)
        or set(capture) != {"schema_version", "status", "stages", "effects_complete", *EFFECTS}
        or type(capture["schema_version"]) is not int
        or capture["schema_version"] != 1
        or capture["status"] not in ("complete", "not_used", "unavailable")
        or type(capture["effects_complete"]) is not bool
        or not isinstance(capture["stages"], list)
        or len(capture["stages"]) != (1 if capture["status"] == "complete" else 0)
    ):
        raise ValueError("lineage_profile_invalid")
    effects = {key: _identities(capture[key], 1 if "output" in key else 1024) for key in EFFECTS}
    if (
        not effects["output_ids"] <= effects["attempted_output_ids"]
        or not effects["superseded_ids"] <= effects["attempted_supersede_ids"]
        or effects["attempted_output_ids"] & effects["attempted_supersede_ids"]
        or (capture["status"] == "not_used" and any(effects.values()))
        or (capture["status"] != "unavailable" and not capture["effects_complete"])
        or (
            capture["status"] == "complete"
            and (
                effects["output_ids"] != effects["attempted_output_ids"]
                or effects["superseded_ids"] != effects["attempted_supersede_ids"]
            )
        )
    ):
        raise ValueError("lineage_profile_effects_invalid")
    for stage in capture["stages"]:
        if (
            not isinstance(stage, dict)
            or set(stage) != {"stage", "status", "inputs", "rendered", "output_id"}
            or stage["stage"] != "basic_profile_update"
            or stage["status"] != "used"
            or stage["output_id"] is not None
            or not isinstance(stage["inputs"], list)
            or len(stage["inputs"]) > MAX_ITEMS
            or not isinstance(stage["rendered"], str)
        ):
            raise ValueError("lineage_profile_stage_invalid")
        rule = parse_json(stage["rendered"])
        if (
            not isinstance(rule, dict)
            or set(rule) != {"allowed_fields", "incoming_kv", "existing_kv", "head_ids"}
            or not isinstance(rule["allowed_fields"], list)
            or len(rule["allowed_fields"]) > MAX_ITEMS
            or any(not isinstance(field, str) or not field for field in rule["allowed_fields"])
            or not isinstance(rule["incoming_kv"], dict)
            or not isinstance(rule["existing_kv"], dict)
        ):
            raise ValueError("lineage_profile_rule_invalid")
        heads = _identities(rule["head_ids"], MAX_ITEMS)
        seen = set()
        nodes = []
        for item in stage["inputs"]:
            record = validate_native_record(item, "profile", request, error="lineage_profile")
            if record["layer"] != LAYER or item["id"] in seen:
                raise ValueError("lineage_profile_record_invalid")
            seen.add(item["id"])
            nodes.append(MemoryNode.from_dict(record))
        # Reuse the native deterministic rules; do not approximate their sorting,
        # legacy-head selection or KV assembly in a separate Hub implementation.
        native_heads = [node.node_id for node in _latest_l0_heads_from_nodes(nodes)]
        native_kv = _assemble_full_kv_from_nodes(nodes, allowed_fields=rule["allowed_fields"])
        if (
            native_heads != rule["head_ids"]
            or encode(native_kv) != encode(rule["existing_kv"])
            or encode(_sanitize_kv(rule["incoming_kv"], rule["allowed_fields"]))
            != encode(rule["incoming_kv"])
        ):
            raise ValueError("lineage_profile_rule_mismatch")
        if (
            not heads <= seen
            or effects["output_ids"] & seen
            or (effects["output_ids"] and effects["superseded_ids"] != heads)
            or (not effects["output_ids"] and effects["attempted_supersede_ids"])
        ):
            raise ValueError("lineage_profile_effects_invalid")
    return value


def capture_profile(result, request):
    value = {"scene": result["scene"], "capture": result.get("basic_profile_inputs")}
    encoded = encode(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("lineage_profile_limit")
    value = validate(parse_json(encoded), request)
    capture = value["capture"]
    if capture is not None and set(capture["output_ids"]) & {
        result.get("memory_id"),
        *result.get("memory_ids", []),
    }:
        raise ValueError("lineage_profile_output_collision")
    return value


async def read_profile(bridge, identity):
    sdk = await bridge._get_client()
    node = await _joined_thread(lambda: sdk._loop_thread.run(sdk._vector_store.get_by_id(identity)))
    if node is None:
        raise ValueError("lineage_profile_output_missing")
    payload = node.to_dict()
    payload.pop("embedding")
    validate_native_record(
        {"kind": "profile", "id": identity, "record": payload}, "profile", error="lineage_profile"
    )
    return payload


async def prove_profile_outputs(bridge, value, request):
    capture = value["capture"]
    if capture is None or not capture["output_ids"]:
        return
    for identity in capture["output_ids"]:
        payload = await read_profile(bridge, identity)
        if (
            payload["layer"] != LAYER
            or payload["user_id"] != request.user_id
            or payload["agent_id"] not in (None, "")
            or payload["session_id"] not in (None, "")
            or payload["source"] != "default"
            or verified_origin("profile", payload) != request.request_id
        ):
            raise ValueError("lineage_profile_output_unproven")


_archive = EvidenceArchive(
    "profile", "cmh_profile_inputs_schema", "cmh_event_profile_inputs", validate
)


def initialize_profile(db):
    schema = db.execute(
        "SELECT name FROM sqlite_master WHERE name='cmh_profile_inputs_schema'"
    ).fetchone()
    index = db.execute(
        "SELECT name FROM sqlite_master WHERE name='cmh_event_profile_outputs'"
    ).fetchone()
    if bool(schema) != bool(index):
        raise AccessError("lineage_profile_store_unrecognized", 503)
    _archive.initialize(db)
    if index is None:
        db.execute(
            "CREATE TABLE cmh_event_profile_outputs(receipt_id TEXT NOT NULL "
            "REFERENCES cmh_event_profile_inputs(receipt_id), output_id TEXT NOT NULL UNIQUE, "
            "PRIMARY KEY(receipt_id,output_id))"
        )
    check_profile(db)


def check_profile(db):
    _archive.check(db)
    db.execute("SELECT receipt_id,output_id FROM cmh_event_profile_outputs LIMIT 0")


def _outputs(value):
    return (
        ()
        if value is None or value["capture"] is None
        else tuple(sorted(value["capture"]["output_ids"]))
    )


def stored_profile(db, receipt, request):
    value = _archive.stored(db, receipt, request)
    ids = tuple(
        row[0]
        for row in db.execute(
            "SELECT output_id FROM cmh_event_profile_outputs WHERE receipt_id=? "
            "ORDER BY output_id LIMIT 2",
            (receipt,),
        )
    )
    if ids != _outputs(value):
        raise ValueError("lineage_profile_index_invalid")
    return value


def commit_profile(db, receipt, value, request):
    previous = stored_profile(db, receipt, request)
    _archive.commit(db, receipt, value, request)
    if previous is None:
        db.executemany(
            "INSERT INTO cmh_event_profile_outputs VALUES (?,?)",
            [(receipt, identity) for identity in _outputs(value)],
        )
