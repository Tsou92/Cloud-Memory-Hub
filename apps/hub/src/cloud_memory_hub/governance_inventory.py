"""Strict observation of the pinned SDK stores; never a cross-store snapshot.

Reuse the SDK's Chroma pool/decoder and Kuzu query executor. Ordinary retrieval
omits stored lineage and can mask failed graph queries; see ADR-0012.
"""

import re
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from hy_memory.data.vector_store_chroma import ChromaVectorStore, _run_in_vdb_pool
from hy_memory.models.memory import MemoryLayer, MemoryStatus
from one_memory.hy_memory_bridge import _joined_thread

from cloud_memory_hub.chain_inventory import require_chain_origins
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import RelationNode
from cloud_memory_hub.privacy import PrivacyError, parse_json
from cloud_memory_hub.productivity_inventory import observe_productivity

MAX_RECORDS = 10_000
MAX_EDGES = 100_000
MAX_CONTENT_BYTES = 32 * 1024 * 1024
_IDENTITY = re.compile(r"[A-Za-z0-9_:-]{1,200}\Z")


def _identity(value):
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise GovernanceError("governance_record_invalid")
    return value


def _references(value):
    if value is None:
        return set()
    if not isinstance(value, list) or len(value) > MAX_EDGES:
        raise GovernanceError("governance_reference_invalid")
    return {_identity(item) for item in value}


def _json_reference(value):
    try:
        return parse_json(value)
    except PrivacyError:
        raise GovernanceError("governance_reference_invalid") from None


def _node(payload, namespace):
    if payload.get("user_id") != namespace:
        raise GovernanceError("governance_scope_mismatch")
    identity = _identity(payload.get("node_id"))
    if identity.startswith(("cmhp:", "cmhe:", "cmht:", "cmhv:", "cmhc:", "cmhi:", "cmhj:")):
        raise GovernanceError("governance_record_identity_mismatch")
    content = payload.get("content")
    if not isinstance(content, str) or len(content.encode()) > MAX_CONTENT_BYTES:
        raise GovernanceError("governance_record_invalid")
    layer, status = MemoryLayer(payload["layer"]), MemoryStatus(payload["status"])
    custom = payload.get("custom")
    if not isinstance(custom, dict):
        raise GovernanceError("governance_reference_invalid")
    parents = _references(payload.get("evidence_chain"))
    for key in ("source_raw_memory_ids", "evidence_raw_ids", "source_memory_ids"):
        parents.update(_references(custom.get(key)))
    singular = payload.get("source_raw_memory_id")
    if singular is not None:
        parents.add(_identity(singular))
    versions = _references(payload.get("supersedes")) | _references(payload.get("superseded_by"))
    for key in ("previous_version_id", "superseded_by_id"):
        if payload.get(key) not in (None, ""):
            versions.add(_identity(payload[key]))
    return RelationNode(
        identity,
        "raw" if layer == MemoryLayer.L1_RAW else "memory",
        content,
        layer.value,
        status.value,
        frozenset(parents),
        frozenset(versions),
    )


async def _check_ownership(vector, graph, records, vector_ids, namespace):
    if not records:
        return
    identities = sorted(records)
    # Check collisions without fetching bodies belonging to another project.
    # A graph-owned ID present in the vector store but absent from the scoped
    # vector scan is a conflicting (or concurrently changed) identity.
    observed = await _run_in_vdb_pool(
        lambda: vector._collection.get(
            ids=[vector._node_id_to_point_id(identity) for identity in identities], include=[]
        )
    )
    if not isinstance(observed, dict) or not isinstance(observed.get("ids"), list):
        raise GovernanceError("governance_store_shape_unknown")
    if set(observed["ids"]) != set(vector_ids):
        raise GovernanceError("governance_backend_conflict")
    owners = await graph._run(
        "MATCH (m:Memory) WHERE m.node_id IN $ids RETURN m.node_id,m.user_id "
        f"LIMIT {MAX_RECORDS + 1}",
        {"ids": identities},
    )
    if not isinstance(owners, list) or len(owners) > MAX_RECORDS:
        raise GovernanceError("governance_store_shape_unknown")
    for row in owners:
        if len(row) != 2 or row[0] not in records or row[1] != namespace:
            raise GovernanceError("governance_backend_conflict")


async def _observe(sdk, namespace):
    vector = sdk._vector_store
    if not isinstance(vector, ChromaVectorStore):
        raise GovernanceError("governance_backend_unsupported")
    # Inspect authoritative IDs before decoding; MemoryNode.from_dict otherwise
    # invents a new UUID for a damaged/missing ID, which is unsafe for a plan.
    raw = await _run_in_vdb_pool(
        lambda: vector._collection.get(
            where={"user_id": {"$eq": namespace}},
            include=["metadatas"],
            limit=MAX_RECORDS + 1,
        )
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("ids"), list):
        raise GovernanceError("governance_store_shape_unknown")
    if len(raw["ids"]) > MAX_RECORDS:
        raise GovernanceError("governance_scan_limit")
    if not isinstance(raw.get("metadatas"), list) or len(raw["metadatas"]) != len(raw["ids"]):
        raise GovernanceError("governance_store_shape_unknown")
    records, used = {}, 0

    def add(payload, expected_id=None):
        nonlocal used
        node = _node(payload, namespace)
        if expected_id is not None and expected_id != vector._node_id_to_point_id(node.record_id):
            raise GovernanceError("governance_record_identity_mismatch")
        used += len(node.content.encode())
        if used > MAX_CONTENT_BYTES:
            raise GovernanceError("governance_scan_limit")
        previous = records.get(node.record_id)
        if previous:
            if (previous.kind, previous.content, previous.layer, previous.status) != (
                node.kind,
                node.content,
                node.layer,
                node.status,
            ):
                raise GovernanceError("governance_backend_conflict")
            node = replace(
                node,
                parents=node.parents | previous.parents,
                versions=node.versions | previous.versions,
            )
        records[node.record_id] = node
        if len(records) > MAX_RECORDS:
            raise GovernanceError("governance_scan_limit")

    for identity, metadata in zip(raw["ids"], raw["metadatas"], strict=True):
        if not isinstance(metadata, dict):
            raise GovernanceError("governance_store_shape_unknown")
        payload = vector._chroma_metadata_to_payload(metadata)
        # The pinned encoder stores None as an empty string. Its decoder leaves
        # that sentinel unchanged for JSON fields. Normalize only this exact
        # storage representation, never malformed JSON or custom references.
        for key in ("supersedes", "superseded_by", "evidence_chain", "custom"):
            value = metadata.get(key)
            if value == "" and key != "custom":
                payload[key] = None
            elif isinstance(value, str):
                payload[key] = _json_reference(value)
        if metadata.get("content") == "":
            payload["content"] = ""
        add(payload, identity)
    graph = sdk._graph_store
    if graph is None:
        path = Path(sdk._config.graph_store.db_path)
        # A disabled graph may still hold older data at the original path.
        if path.exists() or path.with_name("graph").exists():
            raise GovernanceError("governance_graph_unavailable")
    else:
        from hy_memory.data.graph_store_kuzu import KuzuGraphStore

        if not isinstance(graph, KuzuGraphStore):
            raise GovernanceError("governance_backend_unsupported")
        if not graph._available:
            raise GovernanceError("governance_graph_unavailable")
        rows = await graph._run(
            "MATCH (m:Memory) WHERE m.user_id=$scope "
            "RETURN m.node_id,m.user_id,m.content,m.layer,m.status,m.custom_json,"
            "m.evidence_chain,m.previous_version_id,m.superseded_by_id "
            f"LIMIT {MAX_RECORDS + 1}",
            {"scope": namespace},
        )
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
            raise GovernanceError("governance_scan_limit")
        for row in rows:
            if len(row) != 9:
                raise GovernanceError("governance_store_shape_unknown")
            add(
                dict(
                    zip(
                        (
                            "node_id",
                            "user_id",
                            "content",
                            "layer",
                            "status",
                            "custom",
                            "evidence_chain",
                            "previous_version_id",
                            "superseded_by_id",
                        ),
                        (*row[:5], _json_reference(row[5]), _json_reference(row[6]), *row[7:]),
                        strict=True,
                    )
                )
            )
        await _check_ownership(vector, graph, records, raw["ids"], namespace)
        edges = await graph._run(
            "MATCH (m:Memory)-[:DERIVED_FROM]->(v:VdbRef) WHERE m.user_id=$scope "
            f"RETURN m.node_id,v.node_id LIMIT {MAX_EDGES + 1}",
            {"scope": namespace},
        )
        if not isinstance(edges, list) or len(edges) > MAX_EDGES:
            raise GovernanceError("governance_scan_limit")
        for row in edges:
            if len(row) != 2 or row[0] not in records:
                raise GovernanceError("governance_store_shape_unknown")
            node = records[row[0]]
            records[row[0]] = replace(node, parents=node.parents | {_identity(row[1])})
    records.update(
        await observe_productivity(sdk, namespace, MAX_RECORDS, MAX_EDGES, MAX_CONTENT_BYTES)
    )
    check_budget(records)
    return records


def check_budget(records):
    if (
        len(records) > MAX_RECORDS
        or sum(len(node.parents) + len(node.versions) for node in records.values()) > MAX_EDGES
        or sum(len(node.content.encode()) for node in records.values()) > MAX_CONTENT_BYTES
    ):
        raise GovernanceError("governance_scan_limit")


async def observe_inventory(bridge, namespace, lineage=None):
    try:
        if version("hy-memory") != "2.0.5.dev3":
            raise GovernanceError("governance_sdk_version_unsupported")
        async with bridge.exclusive_access():
            sdk = await bridge._get_client()
            nodes = await _joined_thread(lambda: sdk._loop_thread.run(_observe(sdk, namespace)))
            if lineage is not None:
                nodes = await _joined_thread(lineage.inventory, namespace, nodes)
            require_chain_origins(nodes)
            check_budget(nodes)
            return nodes
    except GovernanceError:
        raise
    except Exception:
        # SDK and JSON errors can embed stored content. Keep the public code fixed.
        raise GovernanceError("governance_inventory_unavailable") from None
