"""Observe native chain rows by proven owner without opening or migrating a writer."""

import json
import math
import sqlite3
from pathlib import Path

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.sdk_productivity import existing_database

ROUND_LAYER = "coding_chain_round"
THREAD_LAYER = "coding_chain_thread"
_COLUMNS = {
    "thread": (
        "id",
        "owner",
        "branch",
        "file",
        "symbol",
        "pkey",
        "lo",
        "hi",
        "n_rounds",
        "updated_at",
        "body",
    ),
    "round": ("id", "thread_id", "seq", "said", "patch", "adds", "created_at"),
    "repo_symbol": ("name", "path", "line", "kind"),
}


def chain_id(kind, identity):
    if kind not in ("r", "t") or type(identity) is not int or not 0 < identity < 2**63:
        raise GovernanceError("governance_chain_identity_invalid")
    return f"cmhc:{kind}:{identity}"


def _check_schema(db):
    for table, expected in _COLUMNS.items():
        columns = db.execute(f"PRAGMA table_info({table})").fetchall()
        if {row[1] for row in columns} != set(expected):
            raise GovernanceError("governance_chain_schema_unknown")
    for index, expected in (
        ("ix_thread_bf", ("owner", "branch", "file", "pkey")),
        ("ix_round_t", ("thread_id", "seq")),
    ):
        if tuple(row[2] for row in db.execute(f"PRAGMA index_info({index})")) != expected:
            raise GovernanceError("governance_chain_index_invalid")


def _rows(db, table, where, owner, maximum, byte_limit):
    size = "+".join(f'COALESCE(length(CAST("{column}" AS BLOB)),0)' for column in _COLUMNS[table])
    count, total = db.execute(
        f"SELECT count(*),COALESCE(sum({size}),0) FROM {table} WHERE {where}", (owner,)
    ).fetchone()
    if count > maximum or total > byte_limit:
        raise GovernanceError("governance_scan_limit")
    return db.execute(
        f"SELECT * FROM {table} WHERE {where} ORDER BY id LIMIT ?", (owner, maximum + 1)
    ).fetchall()


def _text(row, *names):
    if any(not isinstance(row[name], str) for name in names):
        raise GovernanceError("governance_chain_record_invalid")


def _number(value):
    if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise GovernanceError("governance_chain_record_invalid")


def observe_chain(base_path, namespace, maximum, edge_limit, byte_limit):
    path = Path(base_path).parent / "hy_code_chain" / "hy_code.db"
    with existing_database(path) as db:
        if db is None:
            return {}
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        _check_schema(db)
        # This legacy global index has no owner. Never assign its content by filename.
        if db.execute("SELECT 1 FROM repo_symbol LIMIT 1").fetchone():
            raise GovernanceError("governance_chain_index_unscoped")
        if db.execute(
            "SELECT 1 FROM round LEFT JOIN thread ON round.thread_id=thread.id "
            "WHERE thread.id IS NULL LIMIT 1"
        ).fetchone():
            raise GovernanceError("governance_chain_orphan")
        owner = namespace + "|"
        # BINARY owner range uses the SDK's existing index, without reading bodies.
        if db.execute(
            "SELECT 1 FROM thread WHERE owner>=? AND owner<? AND owner<>? LIMIT 1",
            (owner, namespace + "}", owner),
        ).fetchone():
            raise GovernanceError("governance_chain_owner_unsupported")
        threads = _rows(db, "thread", "owner=?", owner, maximum, byte_limit)
        rounds = _rows(
            db,
            "round",
            "thread_id IN (SELECT id FROM thread WHERE owner=?)",
            owner,
            maximum,
            byte_limit,
        )

    nodes, grouped = {}, {}
    used_bytes, used_edges = 0, 0

    def add(node):
        nonlocal used_bytes, used_edges
        used_bytes += len(node.content.encode())
        used_edges += len(node.parents) + len(node.versions)
        if len(nodes) >= maximum or used_bytes > byte_limit or used_edges > edge_limit:
            raise GovernanceError("governance_scan_limit")
        if node.record_id in nodes:
            raise GovernanceError("governance_record_identity_mismatch")
        nodes[node.record_id] = node

    for row in threads:
        chain_id("t", row["id"])
        _text(row, "owner", "branch", "file", "symbol", "pkey", "body")
        if (
            row["owner"] != owner
            or not row["file"]
            or any(type(row[name]) is not int or row[name] < 0 for name in ("lo", "hi", "n_rounds"))
            or row["hi"] < row["lo"]
        ):
            raise GovernanceError("governance_chain_record_invalid")
        _number(row["updated_at"])
        grouped[row["id"]] = []
    for row in rounds:
        identity = chain_id("r", row["id"])
        chain_id("t", row["thread_id"])
        _text(row, "said", "patch", "adds")
        if type(row["seq"]) is not int or row["seq"] <= 0 or not row["patch"]:
            raise GovernanceError("governance_chain_record_invalid")
        _number(row["created_at"])
        adds = parse_json(row["adds"])
        if not isinstance(adds, list) or any(not isinstance(value, str) for value in adds):
            raise GovernanceError("governance_chain_record_invalid")
        if row["thread_id"] not in grouped:
            raise GovernanceError("governance_chain_orphan")
        grouped[row["thread_id"]].append(row)
    for thread in threads:
        history = sorted(grouped[thread["id"]], key=lambda row: row["seq"])
        if (
            not history
            or len(history) != thread["n_rounds"]
            or [row["seq"] for row in history] != list(range(1, len(history) + 1))
        ):
            raise GovernanceError("governance_chain_sequence_invalid")
        previous, members = None, set()
        for row in history:
            identity = chain_id("r", row["id"])
            members.add(identity)
            add(
                RelationNode(
                    identity,
                    "raw",
                    json.dumps(dict(row), ensure_ascii=False, sort_keys=True),
                    ROUND_LAYER,
                    "retained",
                    versions=frozenset({previous}) if previous else frozenset(),
                )
            )
            previous = identity
        add(
            RelationNode(
                chain_id("t", thread["id"]),
                "raw",
                json.dumps(dict(thread), ensure_ascii=False, sort_keys=True),
                THREAD_LAYER,
                "retained",
                parents=frozenset(members),
            )
        )
    return nodes


def require_chain_origins(nodes):
    if any(node.layer == ROUND_LAYER and not node.parents for node in nodes.values()):
        raise GovernanceError("governance_chain_lineage_incomplete")
