"""Validate native ordinary-memory candidates without inventing absent producers."""

from cloud_memory_hub.evidence_archive import EvidenceArchive
from cloud_memory_hub.native_evidence import validate_native_record
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.reconcile_evidence import MAX_BYTES, MAX_ITEMS, encode


def _record(item, request):
    validate_native_record(item, "memory", request)


def validate(value, request=None):
    if (
        not isinstance(value, dict)
        or set(value) != {"scene", "capture"}
        or value["scene"] not in ("chat", "productivity")
    ):
        raise ValueError("lineage_memory_invalid")
    capture = value["capture"]
    if capture is None:
        return value
    if (
        value["scene"] != "chat"
        or not isinstance(capture, dict)
        or set(capture) != {"schema_version", "status", "stages"}
        or type(capture["schema_version"]) is not int
        or capture["schema_version"] != 1
        or capture["status"] not in ("complete", "not_used", "unavailable")
        or not isinstance(capture["stages"], list)
        or len(capture["stages"]) != (1 if capture["status"] == "complete" else 0)
    ):
        raise ValueError("lineage_memory_invalid")
    for stage in capture["stages"]:
        if (
            not isinstance(stage, dict)
            or set(stage) != {"stage", "status", "inputs", "rendered", "output_id"}
            or stage["stage"] != "memory_decision"
            or stage["status"] not in ("used", "fallback", "skipped")
            or stage["output_id"] is not None
            or not isinstance(stage["inputs"], list)
            or len(stage["inputs"]) > MAX_ITEMS
            or not isinstance(stage["rendered"], str)
            or (stage["status"] == "skipped" and (stage["inputs"] or stage["rendered"]))
            or (stage["status"] != "skipped" and (not stage["inputs"] or not stage["rendered"]))
        ):
            raise ValueError("lineage_memory_stage_invalid")
        seen = set()
        for item in stage["inputs"]:
            _record(item, request)
            if item["id"] in seen:
                raise ValueError("lineage_memory_record_duplicate")
            seen.add(item["id"])
    return value


def capture_memory(result, request):
    value = {"scene": result["scene"], "capture": result.get("chat_reconcile_inputs")}
    encoded = encode(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("lineage_memory_limit")
    return validate(parse_json(encoded), request)


_archive = EvidenceArchive(
    "memory", "cmh_memory_inputs_schema", "cmh_event_memory_inputs", validate
)
initialize_memory = _archive.initialize
check_memory = _archive.check
stored_memory = _archive.stored
commit_memory = _archive.commit
