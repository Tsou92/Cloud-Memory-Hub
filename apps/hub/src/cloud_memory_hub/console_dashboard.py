"""Content-free, project-scoped dashboard over existing receipts and queue metadata."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from time import monotonic

from one_memory.database_access import main_database

from cloud_memory_hub.events import canonical_uuid, split_request_identity
from cloud_memory_hub.remote_access import AccessError, ScopeGrant

RECORD_LABELS = {
    "conversation": "会话记录",
    "remember": "保存的记忆",
    "handoff": "任务交接",
    "other": "其他记录",
}
_BATCH_SIZE = 200
_READ_SECONDS = 3.0


def _connections(db, scope_id):
    rows = db.execute(
        "SELECT s.source_id,s.device_id,s.status,d.status AS device_status,g.permissions "
        "FROM cmh_sources s LEFT JOIN devices d USING(device_id) "
        "LEFT JOIN grants g ON g.device_id=s.device_id AND g.scope_id=s.scope_id "
        "WHERE s.scope_id=?",
        (scope_id,),
    )
    devices, sources = set(), set()
    for row in rows:
        if row["device_status"] not in {"active", "revoked"} or row["status"] not in {
            "active",
            "revoked",
        }:
            raise AccessError("console_dashboard_unavailable", 503)
        permissions = ()
        if row["permissions"] is not None:
            try:
                permissions = ScopeGrant(
                    scope_id=scope_id, permissions=json.loads(row["permissions"])
                ).permissions
            except (ValueError, TypeError):
                raise AccessError("access_store_unrecognized", 503) from None
        if row["status"] == row["device_status"] == "active" and set(permissions) & {
            "events:write",
            "memory:read",
            "history:read",
        }:
            devices.add(row["device_id"])
            sources.add(row["source_id"])
    return {"device_connections": len(devices), "agent_connections": len(sources)}


def _receipt_cursor(db, scope_id, instance_id):
    # SQLite projects classification only; payload text never leaves the database.
    return db.execute(
        "SELECT device_id,event_id,receipt_id,request_id,CASE "
        "WHEN NOT json_valid(payload_json) THEN NULL "
        "WHEN json_extract(payload_json,'$.scope_id') IS NOT scope_id "
        "OR json_extract(payload_json,'$.instance_id') IS NOT ? "
        "OR json_extract(payload_json,'$.device_id') IS NOT device_id "
        "OR json_extract(payload_json,'$.source_id') IS NOT source_id "
        "OR json_extract(payload_json,'$.event_id') IS NOT event_id THEN NULL "
        "WHEN json_extract(payload_json,'$.kind')='conversation_turn' THEN 'conversation' "
        "WHEN json_extract(payload_json,'$.kind')='operation' THEN CASE "
        "WHEN json_type(payload_json,'$.content.metadata.agent_access_version')='integer' "
        "AND json_extract(payload_json,'$.content.metadata.agent_access_version')=1 "
        "AND json_extract(payload_json,'$.content.metadata.agent_access_kind') "
        "IN ('remember','handoff') "
        "THEN json_extract(payload_json,'$.content.metadata.agent_access_kind') "
        "ELSE 'other' END ELSE NULL END AS record_type "
        "FROM cmh_event_receipts WHERE scope_id=? ORDER BY rowid",
        (instance_id, scope_id),
    )


def _queue_times(queue, rows, deadline):
    placeholders = ",".join("?" for _ in rows)
    remaining_ms = max(0, int((deadline - monotonic()) * 1000))
    with main_database(queue.database_path).read(
        row_factory=sqlite3.Row, busy_timeout_ms=remaining_ms
    ) as db:
        records = db.execute(
            "SELECT job_id,idempotency_key,created_at FROM memory_write_jobs "
            f"WHERE idempotency_key IN ({placeholders})",
            [row["request_id"] for row in rows],
        ).fetchall()
    return {row["idempotency_key"]: (row["job_id"], row["created_at"]) for row in records}


def dashboard(console, scope_id, privacy, *, now=None):
    now = now or datetime.now(UTC)
    days = {str((now - timedelta(days=offset)).date()): 0 for offset in range(6, -1, -1)}
    counts = dict.fromkeys(RECORD_LABELS, 0)
    hidden = console.reads.suppressed(scope_id)
    missing = 0
    deadline = monotonic() + _READ_SECONDS
    with console.ingest._database() as db:
        console._project(db, scope_id)
        statistics = _connections(db, scope_id)
        cursor = _receipt_cursor(db, scope_id, console.access.instance_id)
        while batch := cursor.fetchmany(_BATCH_SIZE):
            if monotonic() > deadline:
                raise AccessError("console_dashboard_busy", 503)
            visible = []
            for row in batch:
                canonical_uuid(row["receipt_id"])
                if "cmhe:" + row["receipt_id"] in hidden:
                    continue
                identity = split_request_identity(row["request_id"])
                if (
                    identity[:3]
                    != (
                        console.access.instance_id,
                        row["device_id"],
                        row["event_id"],
                    )
                    or row["record_type"] not in counts
                ):
                    raise AccessError("console_dashboard_unavailable", 503)
                counts[row["record_type"]] += 1
                visible.append(row)
            if not visible:
                continue
            try:
                times = _queue_times(console.ingest._queue(), visible, deadline)
            except (sqlite3.Error, OSError, AttributeError, AccessError):
                # Counts remain valid, but unavailable timestamps are never drawn as zero.
                times = {}
            for row in visible:
                matched = times.get(row["request_id"])
                try:
                    if matched is None or matched[0] != row["receipt_id"]:
                        raise ValueError("timestamp_unavailable")
                    created = datetime.fromisoformat(matched[1])
                    if created.tzinfo is None or created > now:
                        raise ValueError("timestamp_unavailable")
                    day = str(created.astimezone(UTC).date())
                except (TypeError, ValueError):
                    missing += 1
                    continue
                if day in days:
                    days[day] += 1
    if monotonic() > deadline:
        raise AccessError("console_dashboard_busy", 503)
    if hidden != console.reads.suppressed(scope_id):
        raise AccessError("read_authorization_changed", 409)
    console._require_scope(scope_id)
    statistics.update(shared_records=sum(counts.values()), handoffs=counts["handoff"])
    return console._metadata(
        {
            "schema_version": 1,
            "instance_id": console.access.instance_id,
            "scope_id": scope_id,
            "generated_at": now.isoformat(),
            "statistics": statistics,
            "record_types": [
                {"kind": kind, "label": label, "count": counts[kind]}
                for kind, label in RECORD_LABELS.items()
            ],
            "daily_activity": []
            if missing
            else [{"date": day, "count": count} for day, count in days.items()],
            "activity_timezone": "UTC",
            "activity_state": "unavailable" if missing else "ready",
            "missing_timestamps": missing,
        },
        privacy,
    )
