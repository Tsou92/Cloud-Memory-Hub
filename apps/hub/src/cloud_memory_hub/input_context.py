"""Persist exact SDK history dependencies alongside, never as, write outputs."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from one_memory.memory_processing import MemoryTaskRunner

from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import write_request
from cloud_memory_hub.sdk_productivity import existing_database
from cloud_memory_hub.sdk_reconcile import MAX_REFERENCES, _ids
from cloud_memory_hub.source_history import PREFIX

MAX_CONTEXT_BYTES = 256 * 1024


@dataclass(frozen=True)
class InputContext:
    scene: str
    history_status: str
    history_request_ids: tuple[str, ...]
    chain_status: str
    chain_round_ids: tuple[int, ...]

    def encode(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value):
        raw = parse_json(value)
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("lineage_context_invalid")
        context = cls(
            raw["scene"],
            raw["history_status"],
            _ids(raw["history_request_ids"]),
            raw["chain_status"],
            _round_ids(raw["chain_round_ids"]),
        )
        if (
            context.scene not in ("chat", "productivity")
            or context.history_status not in ("captured", "unobserved")
            or context.chain_status not in ("captured", "unobserved", "unavailable")
            or (context.history_status != "captured" and context.history_request_ids)
            or (context.chain_status != "captured" and context.chain_round_ids)
            or (
                context.scene == "chat"
                and (context.history_status, context.chain_status) != ("unobserved", "unobserved")
            )
            or context.encode() != value
        ):
            raise ValueError("lineage_context_invalid")
        return context


def _round_ids(value):
    if (
        not isinstance(value, list)
        or len(value) > MAX_REFERENCES
        or any(type(item) is not int or not 0 < item < 2**63 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("lineage_chain_references_invalid")
    return tuple(value)


def capture_context(result, base_path, request):
    """The caller has already validated the SDK outcome's exact request identity."""
    if result["scene"] == "chat":
        return InputContext("chat", "unobserved", (), "unobserved", ())
    history = _ids(result["history_request_ids"]) if "history_request_ids" in result else ()
    history_status = "captured" if "history_request_ids" in result else "unobserved"
    chain_status, rounds = "unobserved", ()
    if "chain_context" in result:
        chain = result["chain_context"]
        if (
            not isinstance(chain, dict)
            or set(chain) != {"status", "round_ids"}
            or chain["status"] not in ("complete", "unavailable")
        ):
            raise ValueError("lineage_chain_context_invalid")
        rounds = _round_ids(chain["round_ids"])
        chain_status = "captured" if chain["status"] == "complete" else "unavailable"
        if (chain_status == "unavailable" and rounds) or (
            result.get("productivity_variant") == "working" and rounds
        ):
            raise ValueError("lineage_chain_context_invalid")
        if chain_status == "captured" and result.get("productivity_variant") == "coding":
            count = result.get("chain_rounds")
            if type(count) is not int or count < len(rounds) or bool(count) != bool(rounds):
                raise ValueError("lineage_chain_count_mismatch")
    if history_status == "captured" or chain_status != "unobserved":
        if result.get("productivity_variant") not in ("working", "coding"):
            raise ValueError("lineage_context_variant_unknown")
    if rounds:
        path = Path(base_path).parent / "hy_code_chain" / "hy_code.db"
        with existing_database(path) as db:
            if db is None:
                raise ValueError("lineage_chain_reference_missing")
            placeholders = ",".join("?" for _ in rounds)
            rows = db.execute(
                "SELECT round.id,thread.owner FROM round JOIN thread ON thread.id=round.thread_id "
                f"WHERE round.id IN ({placeholders})",
                rounds,
            ).fetchall()
            if {row[0] for row in rows} != set(rounds) or any(
                row[1] != request.user_id + "|" for row in rows
            ):
                raise ValueError("lineage_chain_reference_unproven")
    context = InputContext("productivity", history_status, history, chain_status, rounds)
    return InputContext.decode(context.encode())


def initialize_context(db):
    if (
        db.execute("SELECT name FROM sqlite_master WHERE name='cmh_context_schema'").fetchone()
        is None
    ):
        if db.execute("SELECT name FROM sqlite_master WHERE name='cmh_event_contexts'").fetchone():
            raise AccessError("lineage_context_store_unrecognized", 503)
        db.execute("CREATE TABLE cmh_context_schema(id INTEGER PRIMARY KEY,version INTEGER)")
        db.execute("INSERT INTO cmh_context_schema VALUES (1,1)")
        db.execute(
            "CREATE TABLE cmh_event_contexts(receipt_id TEXT PRIMARY KEY "
            "REFERENCES cmh_event_outcomes(receipt_id), "
            "context_json TEXT NOT NULL, context_digest TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX cmh_context_requests ON cmh_event_receipts(request_id)")
    check_context(db)


def check_context(db):
    rows = db.execute("SELECT id,version FROM cmh_context_schema").fetchall()
    if [tuple(row) for row in rows] != [(1, 1)]:
        raise AccessError("lineage_context_store_version_unsupported", 503)
    db.execute("SELECT receipt_id,context_json,context_digest FROM cmh_event_contexts LIMIT 0")
    columns = db.execute("PRAGMA index_info(cmh_context_requests)").fetchall()
    if [row[2] for row in columns] != ["request_id"]:
        raise AccessError("lineage_context_index_invalid", 503)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def stored_context(db, receipt):
    row = db.execute(
        "SELECT CASE WHEN length(CAST(context_json AS BLOB))<=? THEN context_json END,"
        "context_digest FROM cmh_event_contexts WHERE receipt_id=?",
        (MAX_CONTEXT_BYTES, receipt),
    ).fetchone()
    if row is None:
        return None
    if row[0] is None or _digest(row[0]) != row[1]:
        raise ValueError("lineage_context_digest_invalid")
    return InputContext.decode(row[0])


def context_parents(db, context, event):
    if context is None:
        return frozenset()
    row = db.execute(
        "SELECT * FROM cmh_event_receipts WHERE device_id=? AND event_id=? AND scope_id=?",
        (event.device_id, event.event_id, event.scope_id),
    ).fetchone()
    if row is None or validate_original(row, event.instance_id) != event:
        raise ValueError("lineage_context_original_changed")
    rebuilt = write_request(event, request_id=row["request_id"])
    request = MemoryTaskRunner.request_for(rebuilt.idempotency_key, rebuilt.input_records)
    parents = set()
    for identity in context.history_request_ids:
        if identity == request.request_id:
            raise ValueError("lineage_context_self_reference")
        rows = db.execute(
            "SELECT * FROM cmh_event_receipts WHERE request_id=? LIMIT 2", (identity,)
        ).fetchall()
        if not rows:
            raise ValueError("lineage_context_original_missing")
        if len(rows) != 1:
            raise ValueError("lineage_context_original_ambiguous")
        row = rows[0]
        prior = write_request(
            validate_original(row, event.instance_id), request_id=row["request_id"]
        )
        historical = MemoryTaskRunner.request_for(prior.idempotency_key, prior.input_records)
        if (historical.user_id, historical.agent_id, historical.source_id) != (
            request.user_id,
            request.agent_id,
            request.source_id,
        ):
            raise ValueError("lineage_context_scope_mismatch")
        parents.add(PREFIX + row["receipt_id"])
    return frozenset(parents)


def commit_context(db, receipt, context, event):
    context_parents(db, context, event)
    previous = stored_context(db, receipt)
    if previous is not None:
        if previous != context:
            raise ValueError("lineage_context_changed")
        return
    encoded = context.encode()
    db.execute(
        "INSERT INTO cmh_event_contexts VALUES (?,?,?)", (receipt, encoded, _digest(encoded))
    )
