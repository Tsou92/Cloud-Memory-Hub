"""Validate native ordinary-memory candidates without inventing absent producers."""

from dataclasses import fields

from hy_memory.coding.record_origin import verified_origin
from hy_memory.models.memory import MemoryNode

from cloud_memory_hub.evidence_archive import EvidenceArchive
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.reconcile_evidence import MAX_BYTES, MAX_ITEMS, encode
from cloud_memory_hub.sdk_reconcile import _ids

_FIELDS = {item.name for item in fields(MemoryNode)} - {"embedding"}
_ORIGIN = {"origin_request_id", "origin_digest"}


def _record(item, request):
    if not isinstance(item, dict) or set(item) != {"kind", "id", "record"}:
        raise ValueError("lineage_memory_record_invalid")
    record = item["record"]
    if (
        item["kind"] != "memory"
        or not isinstance(record, dict)
        or set(record) not in (_FIELDS, _FIELDS - _ORIGIN)
        or item["id"] != record.get("node_id")
    ):
        raise ValueError("lineage_memory_record_invalid")
    _ids([item["id"]])
    # Reuse the native schema; reject lossy coercions rather than normalize evidence.
    try:
        normalized = MemoryNode.from_dict(record).to_dict()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError("lineage_memory_record_invalid") from exc
    normalized.pop("embedding")
    for name in _ORIGIN - record.keys():
        normalized.pop(name)
    if normalized != record:
        raise ValueError("lineage_memory_record_invalid")
    verified_origin("memory", record)
    if request is not None:
        if record["user_id"] != request.user_id:
            raise ValueError("lineage_memory_scope_mismatch")
        if record.get("origin_request_id") == request.request_id:
            raise ValueError("lineage_memory_self_reference")


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
