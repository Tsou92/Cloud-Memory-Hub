"""Immutable ordinary-chat history, additive to older reconciliation archives."""

from cloud_memory_hub.evidence_archive import EvidenceArchive
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.reconcile_evidence import MAX_BYTES, MAX_ITEMS, encode

_FIELDS = {"node_id", "user_id", "agent_id", "session_id", "source", "content", "request_id"}


def validate(value, request=None):
    if (
        not isinstance(value, dict)
        or set(value) != {"scene", "capture"}
        or value["scene"] not in ("chat", "productivity")
    ):
        raise ValueError("lineage_chat_invalid")
    capture = value["capture"]
    if capture is None:
        return value
    if (
        value["scene"] != "chat"
        or not isinstance(capture, dict)
        or set(capture) != {"schema_version", "status", "records", "rendered"}
        or type(capture["schema_version"]) is not int
        or capture["schema_version"] != 1
        or capture["status"] not in ("complete", "not_used", "unavailable")
        or not isinstance(capture["records"], list)
        or len(capture["records"]) > MAX_ITEMS
        or not isinstance(capture["rendered"], str)
        or bool(capture["records"]) != bool(capture["rendered"])
        or (capture["status"] != "complete" and capture["records"])
    ):
        raise ValueError("lineage_chat_invalid")
    seen = set()
    for record in capture["records"]:
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise ValueError("lineage_chat_record_invalid")
        for key, item in record.items():
            if not isinstance(item, str) and not (
                key not in {"node_id", "user_id"} and item is None
            ):
                raise ValueError("lineage_chat_record_invalid")
        if not record["node_id"] or not record["user_id"] or record["node_id"] in seen:
            raise ValueError("lineage_chat_record_invalid")
        seen.add(record["node_id"])
        if request is not None:
            if (record["user_id"], record["agent_id"]) != (request.user_id, request.agent_id):
                raise ValueError("lineage_chat_scope_mismatch")
            if record["request_id"] == request.request_id:
                raise ValueError("lineage_chat_self_reference")
    return value


def capture_chat(result, request):
    value = {"scene": result["scene"], "capture": result.get("chat_history")}
    encoded = encode(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("lineage_chat_limit")
    return validate(parse_json(encoded), request)


_archive = EvidenceArchive("chat", "cmh_chat_schema", "cmh_event_chat_inputs", validate)
initialize_chat = _archive.initialize
check_chat = _archive.check
stored_chat = _archive.stored
commit_chat = _archive.commit
