"""Bounded immutable reconciliation evidence stored with proven output receipts."""

import hashlib
import json
import math
from dataclasses import fields

from hy_memory.coding.record_origin import verified_origin
from hy_memory.coding.types import CodingMemory, WorkingFact

from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.productivity_read import record_id
from cloud_memory_hub.remote_access import AccessError

MAX_BYTES = 2 * 1024 * 1024 + 128  # SDK budget plus the fixed envelope.
MAX_ITEMS = 512
STAGES = {
    "experience_decision": ("experience", "e"),
    "fact_decision": ("fact", "f"),
    "fact_summary": ("fact", "f"),
    "fact_details": ("fact", "f"),
}
_FIELDS = {
    "experience": {item.name for item in fields(CodingMemory)},
    "fact": {item.name for item in fields(WorkingFact)},
}
_LISTS = {"search_keys", "files", "source_fact_ids", "derived_experience_ids"}
_NULLABLE = {
    "workspace_id",
    "branch",
    "session_id",
    "fact_link",
    "created_at",
    "updated_at",
    "origin_request_id",
    "origin_digest",
}


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _record(item, kind, variant):
    if not isinstance(item, dict) or set(item) != {"kind", "id", "record"}:
        raise ValueError("lineage_input_record_invalid")
    record = item["record"]
    expected = _FIELDS[kind]
    if (
        item["kind"] != kind
        or not isinstance(record, dict)
        or set(record) not in (expected, expected - {"origin_request_id", "origin_digest"})
        or item["id"] != record.get("experience_id" if kind == "experience" else "fact_id")
    ):
        raise ValueError("lineage_input_record_invalid")
    record_id(variant, "e" if kind == "experience" else "f", item["id"])
    for name, value in record.items():
        if name in _LISTS:
            valid = (
                isinstance(value, list)
                and len(value) <= MAX_ITEMS
                and all(isinstance(entry, str) for entry in value)
            )
        elif name == "confidence":
            valid = type(value) in (int, float) and math.isfinite(value)
        else:
            valid = isinstance(value, str) or (name in _NULLABLE and value is None)
        if not valid:
            raise ValueError("lineage_input_record_invalid")
    verified_origin(kind, record)


def validate(value, request=None):
    if not isinstance(value, dict) or set(value) != {"variant", "capture"}:
        raise ValueError("lineage_inputs_invalid")
    variant, capture = value["variant"], value["capture"]
    if variant not in (None, "w", "c"):
        raise ValueError("lineage_inputs_variant_invalid")
    if capture is None:
        return value
    if (
        variant is None
        or not isinstance(capture, dict)
        or set(capture) != {"schema_version", "status", "stages"}
        or type(capture["schema_version"]) is not int
        or capture["schema_version"] != 1
        or capture["status"] not in ("complete", "unavailable")
        or not isinstance(capture["stages"], list)
        or len(capture["stages"]) > MAX_ITEMS
        or (capture["status"] == "unavailable" and capture["stages"])
    ):
        raise ValueError("lineage_inputs_invalid")
    count, seen = 0, set()
    for stage in capture["stages"]:
        if (
            not isinstance(stage, dict)
            or set(stage) != {"stage", "status", "inputs", "rendered", "output_id"}
            or not isinstance(stage["stage"], str)
            or stage["stage"] not in STAGES
            or stage["status"] not in ("used", "fallback", "skipped")
            or not isinstance(stage["inputs"], list)
            or not isinstance(stage["rendered"], str)
        ):
            raise ValueError("lineage_input_stage_invalid")
        kind, short = STAGES[stage["stage"]]
        if stage["stage"].endswith("_decision"):
            if stage["output_id"] is not None:
                raise ValueError("lineage_input_target_invalid")
        else:
            record_id(variant, short, stage["output_id"])
        identity = (stage["stage"], stage["output_id"])
        if identity in seen:
            raise ValueError("lineage_input_stage_duplicate")
        seen.add(identity)
        count += len(stage["inputs"])
        if count > MAX_ITEMS:
            raise ValueError("lineage_inputs_limit")
        for item in stage["inputs"]:
            _record(item, kind, variant)
            if request is not None:
                record = item["record"]
                if record["user_id"] != request.user_id:
                    raise ValueError("lineage_input_scope_mismatch")
                if record.get("origin_request_id") == request.request_id:
                    raise ValueError("lineage_input_self_reference")
    return value


def capture_inputs(result, request):
    variant = {"working": "w", "coding": "c"}.get(result.get("productivity_variant"))
    capture = result.get("reconcile_inputs")
    if result["scene"] == "chat":
        if capture is not None:
            raise ValueError("lineage_inputs_scene_invalid")
        variant = None
    value = {"variant": variant, "capture": capture}
    encoded = encode(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("lineage_inputs_limit")
    checked = validate(parse_json(encoded), request)
    if capture is not None and capture["status"] == "complete":
        required = {
            stage
            for field, stage in (
                ("experience_ids", "experience_decision"),
                ("fact_ids", "fact_decision"),
            )
            if result.get(field)
        }
        if not required <= {stage["stage"] for stage in capture["stages"]}:
            raise ValueError("lineage_input_stage_missing")
    return checked


def initialize_inputs(db):
    if (
        db.execute("SELECT name FROM sqlite_master WHERE name='cmh_inputs_schema'").fetchone()
        is None
    ):
        if db.execute("SELECT name FROM sqlite_master WHERE name='cmh_event_inputs'").fetchone():
            raise AccessError("lineage_inputs_store_unrecognized", 503)
        db.execute("CREATE TABLE cmh_inputs_schema(id INTEGER PRIMARY KEY,version INTEGER)")
        db.execute("INSERT INTO cmh_inputs_schema VALUES (1,1)")
        db.execute(
            "CREATE TABLE cmh_event_inputs(receipt_id TEXT PRIMARY KEY "
            "REFERENCES cmh_event_outcomes(receipt_id), "
            "inputs_json TEXT NOT NULL, inputs_digest TEXT NOT NULL)"
        )
    check_inputs(db)


def check_inputs(db):
    if [tuple(row) for row in db.execute("SELECT id,version FROM cmh_inputs_schema")] != [(1, 1)]:
        raise AccessError("lineage_inputs_store_version_unsupported", 503)
    db.execute("SELECT receipt_id,inputs_json,inputs_digest FROM cmh_event_inputs LIMIT 0")


def stored_inputs(db, receipt, request):
    row = db.execute(
        "SELECT CASE WHEN length(CAST(inputs_json AS BLOB))<=? THEN inputs_json END,"
        "inputs_digest FROM cmh_event_inputs WHERE receipt_id=?",
        (MAX_BYTES, receipt),
    ).fetchone()
    if row is None:
        return None
    if row[0] is None or digest(row[0]) != row[1]:
        raise ValueError("lineage_inputs_digest_invalid")
    value = validate(parse_json(row[0]), request)
    if encode(value) != row[0]:
        raise ValueError("lineage_inputs_encoding_invalid")
    return value


def commit_inputs(db, receipt, value, request):
    validate(value, request)
    encoded = encode(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("lineage_inputs_limit")
    previous = stored_inputs(db, receipt, request)
    if previous is not None:
        if previous != value:
            raise ValueError("lineage_inputs_changed")
        return
    db.execute("INSERT INTO cmh_event_inputs VALUES (?,?,?)", (receipt, encoded, digest(encoded)))
