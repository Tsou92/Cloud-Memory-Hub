"""Immutable ordinary-chat history, additive to older reconciliation archives."""

from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.reconcile_evidence import MAX_BYTES, MAX_ITEMS, digest, encode
from cloud_memory_hub.remote_access import AccessError

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


def initialize_chat(db):
    if db.execute("SELECT name FROM sqlite_master WHERE name='cmh_chat_schema'").fetchone() is None:
        if db.execute(
            "SELECT name FROM sqlite_master WHERE name='cmh_event_chat_inputs'"
        ).fetchone():
            raise AccessError("lineage_chat_store_unrecognized", 503)
        db.execute("CREATE TABLE cmh_chat_schema(id INTEGER PRIMARY KEY,version INTEGER)")
        db.execute("INSERT INTO cmh_chat_schema VALUES (1,1)")
        db.execute(
            "CREATE TABLE cmh_event_chat_inputs(receipt_id TEXT PRIMARY KEY "
            "REFERENCES cmh_event_outcomes(receipt_id), "
            "chat_json TEXT NOT NULL, chat_digest TEXT NOT NULL)"
        )
    check_chat(db)


def check_chat(db):
    if [tuple(row) for row in db.execute("SELECT id,version FROM cmh_chat_schema")] != [(1, 1)]:
        raise AccessError("lineage_chat_store_version_unsupported", 503)
    db.execute("SELECT receipt_id,chat_json,chat_digest FROM cmh_event_chat_inputs LIMIT 0")


def stored_chat(db, receipt, request):
    row = db.execute(
        "SELECT CASE WHEN length(CAST(chat_json AS BLOB))<=? THEN chat_json END,"
        "chat_digest FROM cmh_event_chat_inputs WHERE receipt_id=?",
        (MAX_BYTES, receipt),
    ).fetchone()
    if row is None:
        return None
    if row[0] is None or digest(row[0]) != row[1]:
        raise ValueError("lineage_chat_digest_invalid")
    value = validate(parse_json(row[0]), request)
    if encode(value) != row[0]:
        raise ValueError("lineage_chat_encoding_invalid")
    return value


def commit_chat(db, receipt, value, request):
    validate(value, request)
    encoded = encode(value)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("lineage_chat_limit")
    previous = stored_chat(db, receipt, request)
    if previous is not None:
        if previous != value:
            raise ValueError("lineage_chat_changed")
        return
    db.execute(
        "INSERT INTO cmh_event_chat_inputs VALUES (?,?,?)", (receipt, encoded, digest(encoded))
    )
