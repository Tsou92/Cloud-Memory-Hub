"""Observe all pinned productivity rows and index copies without opening writers."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from chromadb.errors import NotFoundError
from hy_memory.coding.store import CodingMemoryStore, _variant_db_path
from hy_memory.data.vector_store_chroma import ChromaVectorStore, _run_in_vdb_pool
from hy_memory.models.memory import MemoryStatus
from one_memory.hy_memory_bridge import _joined_thread

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.privacy import parse_json
from cloud_memory_hub.productivity_read import _KINDS, _VARIANTS, _record, record_id
from cloud_memory_hub.sdk_productivity import existing_database


def trace_id(request_id):
    if not isinstance(request_id, str) or not request_id:
        raise GovernanceError("governance_trace_identity_invalid")
    return "cmht:" + hashlib.sha256(request_id.encode()).hexdigest()


def _references(value, variant, kind, maximum):
    values = parse_json(value)
    if not isinstance(values, list) or len(values) > maximum:
        raise GovernanceError("governance_reference_invalid")
    return frozenset(record_id(variant, kind, item) for item in values)


def _rows(db, table, namespace, maximum, byte_limit):
    # Bound total SQLite payload bytes before materializing potentially large bodies.
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        raise GovernanceError("governance_store_shape_unknown")
    expression = "+".join(
        'COALESCE(length(CAST("' + name.replace('"', '""') + '" AS BLOB)),0)' for name in columns
    )
    count, size = db.execute(
        f"SELECT count(*),COALESCE(sum({expression}),0) FROM {table} WHERE user_id=?",
        (namespace,),
    ).fetchone()
    if count > maximum or size > byte_limit:
        raise GovernanceError("governance_scan_limit")
    return db.execute(
        f"SELECT * FROM {table} WHERE user_id=? LIMIT ?", (namespace, maximum + 1)
    ).fetchall()


def _sqlite_inventory(base_path, namespace, maximum, edge_limit, byte_limit):
    records, reverse = {}, []
    used_bytes, used_edges = 0, 0

    def add(node):
        nonlocal used_bytes, used_edges
        used_bytes += len(node.content.encode())
        used_edges += len(node.parents) + len(node.versions)
        if len(records) >= maximum or used_bytes > byte_limit or used_edges > edge_limit:
            raise GovernanceError("governance_scan_limit")
        if node.record_id in records:
            raise GovernanceError("governance_record_identity_mismatch")
        records[node.record_id] = node

    for variant, _, suffix in _VARIANTS:
        with existing_database(_variant_db_path(str(base_path), suffix)) as db:
            if db is None:
                continue
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            for kind, (table, _, _, decode) in _KINDS.items():
                for row in _rows(db, table, namespace, maximum, byte_limit):
                    record = _record(decode(row), variant, kind)
                    if record.status not in {"active", "superseded"}:
                        raise GovernanceError("governance_record_invalid")
                    parents, versions = frozenset(), frozenset()
                    if kind == "e":
                        parents = _references(row["source_fact_ids"], variant, "f", edge_limit)
                    else:
                        if row["fact_link"] is not None:
                            versions = frozenset({record_id(variant, "f", row["fact_link"])})
                        derived = _references(
                            row["derived_experience_ids"], variant, "e", edge_limit
                        )
                        used_edges += len(derived)
                        if used_edges > edge_limit:
                            raise GovernanceError("governance_scan_limit")
                        reverse.extend((identity, record.memory_id) for identity in derived)
                    add(
                        RelationNode(
                            record.memory_id,
                            "memory",
                            record.content,
                            record.layer,
                            record.status,
                            parents,
                            versions,
                        )
                    )
    for identity, parent in reverse:
        if identity not in records:
            raise GovernanceError("governance_reference_unresolved")
        node = records[identity]
        records[identity] = replace(node, parents=node.parents | {parent})
    with existing_database(_variant_db_path(str(base_path), "_trace")) as db:
        if db is not None:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            for row in _rows(db, "coding_trace", namespace, maximum, byte_limit):
                messages = parse_json(row["messages_json"])
                if not isinstance(messages, list):
                    raise GovernanceError("governance_trace_invalid")
                identity = trace_id(row["request_id"])
                add(
                    RelationNode(
                        identity,
                        "raw",
                        json.dumps(dict(row), ensure_ascii=False),
                        "productivity_trace",
                        "retained",
                    )
                )
    # The pinned chain stores no exact request -> round identity. Do not hide it.
    path = Path(base_path).parent / "hy_code_chain" / "hy_code.db"
    with existing_database(path) as db:
        if db is not None:
            prefix = namespace + "|"
            if db.execute(
                "SELECT 1 FROM thread WHERE substr(owner,1,?)=? LIMIT 1",
                (len(prefix), prefix),
            ).fetchone():
                raise GovernanceError("governance_chain_inventory_incomplete")
    return records


async def observe_productivity(sdk, namespace, maximum, edge_limit, byte_limit):
    records = await _joined_thread(
        _sqlite_inventory,
        sdk._config.coding.db_path,
        namespace,
        maximum,
        edge_limit,
        byte_limit,
    )
    used_bytes = sum(len(node.content.encode()) for node in records.values())
    used_edges = sum(len(node.parents) + len(node.versions) for node in records.values())
    for variant, name, _ in _VARIANTS:
        store = CodingMemoryStore(sdk._config, sdk._embed_service, variant=name)
        for kind, config in (("e", store._coding_config), ("f", store._fact_config)):
            vector = ChromaVectorStore(config)
            vector._client = sdk._vector_store._client
            try:
                vector._collection = await _run_in_vdb_pool(
                    lambda vector=vector: vector._client.get_collection(
                        name=vector._collection_name
                    )
                )
            except NotFoundError:
                if any(identity.startswith(f"cmhp:{variant}:{kind}:") for identity in records):
                    raise GovernanceError("governance_productivity_index_missing") from None
                continue
            found = await _run_in_vdb_pool(
                lambda vector=vector: vector._collection.get(
                    where={"user_id": {"$eq": namespace}},
                    include=["metadatas"],
                    limit=maximum + 1,
                )
            )
            if len(found["ids"]) > maximum:
                raise GovernanceError("governance_scan_limit")
            for point, metadata in zip(found["ids"], found["metadatas"], strict=True):
                payload = vector._chroma_metadata_to_payload(metadata)
                custom = parse_json(metadata["custom"])
                native = custom["coding_experience_id" if kind == "e" else "working_fact_id"]
                parent = record_id(variant, kind, native)
                if payload["user_id"] != namespace or parent not in records:
                    raise GovernanceError("governance_reference_unresolved")
                if vector._node_id_to_point_id(payload["node_id"]) != point:
                    raise GovernanceError("governance_record_identity_mismatch")
                identity = f"cmhv:{variant}:{kind}:" + point
                content = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                used_bytes += len(content.encode())
                used_edges += 1
                if len(records) >= maximum or used_bytes > byte_limit or used_edges > edge_limit:
                    raise GovernanceError("governance_scan_limit")
                records[identity] = RelationNode(
                    identity,
                    "memory",
                    content,
                    f"{name}_{'experience' if kind == 'e' else 'fact'}_index",
                    MemoryStatus(payload["status"]).value,
                    frozenset({parent}),
                )
    return records
