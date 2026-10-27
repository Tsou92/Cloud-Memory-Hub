"""Read-only adapter for the pinned SDK's existing working/coding stores."""

import json
import re
import sqlite3
from datetime import UTC
from importlib.metadata import version
from itertools import zip_longest

from hy_memory.coding.store import (
    CodingMemoryStore,
    _row_to_fact,
    _row_to_memory,
    _variant_db_path,
)
from hy_memory.data.vector_store_chroma import ChromaVectorStore, _run_in_vdb_pool
from one_memory.hy_memory_bridge import HySdkMemoryRecord, _joined_thread

from cloud_memory_hub.sdk_productivity import existing_database

PREFIX = "cmhp:"
_NATIVE_ID = re.compile(r"[A-Za-z0-9_:-]{1,191}\Z")
_VARIANTS = (("w", "working", ""), ("c", "coding", "_cv"))
_KINDS = {
    "e": ("coding_memory_meta", "experience_id", "1", _row_to_memory),
    "f": ("working_fact", "fact_id", "status='active'", _row_to_fact),
}


class ProductivityMemory(HySdkMemoryRecord):
    """Distinguish native source metadata from ordinary SDK backend labels."""


def record_id(variant, kind, native):
    if variant not in ("w", "c") or kind not in _KINDS or not isinstance(native, str):
        raise ValueError("productivity_identity_invalid")
    if not _NATIVE_ID.fullmatch(native):
        raise ValueError("productivity_identity_invalid")
    return f"{PREFIX}{variant}:{kind}:{native}"


def parse_id(identity):
    parts = identity.split(":", 3)
    if len(parts) != 4 or parts[0] != "cmhp":
        return None
    try:
        return tuple(parts[1:]) if record_id(*parts[1:]) == identity else None
    except ValueError:
        return None


def interleave(*groups):
    return [item for row in zip_longest(*groups) for item in row if item is not None]


def _record(row, variant, kind):
    if kind == "e":
        native = row.experience_id
        fields = ("task", "search_keys", "solution", "boundary_envs", "confidence")
        status, layer = "active", "experience"
    else:
        native = row.fact_id
        fields = ("subject", "statement", "details", "fact_chain_summary")
        status, layer = row.status, "fact"
    fields += ("workspace_id", "branch", "files")
    content = json.dumps({key: getattr(row, key) for key in fields}, ensure_ascii=False, indent=2)
    # Pinned SQLite timestamps without a zone represent UTC, independent of host TZ.
    stamp = row.created_at
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return ProductivityMemory(
        memory_id=record_id(variant, kind, native),
        content=content,
        user_id=row.user_id,
        agent_id=row.agent_id,
        session_id=row.session_id,
        source=row.source,
        layer=f"{'working' if variant == 'w' else 'coding'}_{layer}",
        status=status,
        is_latest=status == "active",
        memory_at=int(stamp.timestamp()) if stamp else None,
    )


def _page(base_path, namespace, limit, offset):
    records, total = [], 0
    for variant, _, suffix in _VARIANTS:
        with existing_database(_variant_db_path(str(base_path), suffix)) as db:
            if db is None:
                continue
            db.row_factory = sqlite3.Row
            for kind, (table, column, active, decode) in _KINDS.items():
                count = db.execute(
                    f"SELECT count(*) FROM {table} WHERE user_id=? AND {active}", (namespace,)
                ).fetchone()[0]
                local_offset = max(0, offset - total)
                if local_offset < count and len(records) < limit:
                    rows = db.execute(
                        f"SELECT * FROM {table} WHERE user_id=? AND {active} "
                        f"ORDER BY created_at DESC,{column} ASC LIMIT ? OFFSET ?",
                        (namespace, limit - len(records), local_offset),
                    ).fetchall()
                    records.extend(_record(decode(row), variant, kind) for row in rows)
                total += count
    return records, total


def _get(base_path, identity):
    parsed = parse_id(identity)
    if parsed is None:
        return None
    variant, kind, native = parsed
    table, column, active, decode = _KINDS[kind]
    with existing_database(_variant_db_path(str(base_path), "_cv" if variant == "c" else "")) as db:
        if db is None:
            return None
        db.row_factory = sqlite3.Row
        row = db.execute(
            f"SELECT * FROM {table} WHERE {column}=? AND {active}", (native,)
        ).fetchone()
        return _record(decode(row), variant, kind) if row is not None else None


async def _existing_vector(sdk, config):
    if not isinstance(sdk._vector_store, ChromaVectorStore):
        raise ValueError("productivity_backend_unsupported")
    vector = ChromaVectorStore(config)
    vector._client = sdk._vector_store._client
    # Never initialize: that creates missing collections and masks broken indexes.
    vector._collection = await _run_in_vdb_pool(
        lambda: vector._client.get_collection(name=vector._collection_name)
    )
    return vector


async def _search(sdk, namespace, query, limit):
    groups, embedding = [], None
    for variant, name, suffix in _VARIANTS:
        with existing_database(
            _variant_db_path(sdk._config.coding.db_path, suffix), cross_thread=True
        ) as db:
            if db is None:
                groups.append([])
                continue
            db.row_factory = sqlite3.Row
            present = {
                kind: db.execute(
                    f"SELECT 1 FROM {table} WHERE user_id=? AND {active} LIMIT 1", (namespace,)
                ).fetchone()
                is not None
                for kind, (table, _, active, _) in _KINDS.items()
            }
            if not any(present.values()):
                groups.append([])
                continue
            store = CodingMemoryStore(sdk._config, sdk._embed_service, variant=name)
            store._conn = db
            if embedding is None:
                embedding, _ = await sdk._embed_service.embed_with_meta(query)
            experiences, facts = [], []
            if present["e"]:
                store._vec = await _existing_vector(sdk, store._coding_config)
                hits = await store.search_by_query_embedding(embedding, namespace, top=limit)
                for hit in hits:
                    if hit["score"] < 0.40:
                        continue
                    row = await store.get_by_id(hit["experience_id"], user_id=namespace)
                    # The index is only a candidate source. SQLite owns content/scope.
                    if row is not None and row.user_id == namespace:
                        experiences.append(_record(row, variant, "e"))
            if present["f"]:
                store._fact_vec = await _existing_vector(sdk, store._fact_config)
                rows = await store.search_facts(
                    embedding, namespace, top=limit, query_text=query, min_score=0.30
                )
                facts = [
                    _record(row, variant, "f")
                    for row, _ in rows
                    if row.user_id == namespace and row.status == "active"
                ]
            groups.append(interleave(facts, experiences))
    return groups


class ProductivityRead:
    def __init__(self, bridge):
        self.bridge = bridge

    async def _run(self, operation, *args):
        if version("hy-memory") != "2.0.5.dev3":
            raise ValueError("productivity_sdk_version_unsupported")
        async with self.bridge.exclusive_access():
            sdk = await self.bridge._get_client()
            if operation == "search":
                return await _joined_thread(lambda: sdk._loop_thread.run(_search(sdk, *args)))
            function = _page if operation == "page" else _get
            return await _joined_thread(function, sdk._config.coding.db_path, *args)

    async def page(self, namespace, limit, offset):
        return await self._run("page", namespace, limit, offset)

    async def get(self, identity):
        return await self._run("get", identity)

    async def search(self, namespace, query, limit):
        return await self._run("search", namespace, query, limit)
