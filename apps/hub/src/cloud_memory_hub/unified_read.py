"""Compose ordinary SDK reads with both native productivity variants."""

from one_memory.hy_memory_bridge import HySdkMemoryPage, SemanticMemoryHit

from cloud_memory_hub.productivity_read import PREFIX, ProductivityRead, interleave
from cloud_memory_hub.source_history import PREFIX as ORIGINAL_PREFIX


class UnifiedReadBridge:
    def __init__(self, bridge, originals=None, lineage=None):
        self.bridge = bridge
        self.originals = originals
        self.lineage = lineage
        self.productivity = ProductivityRead(bridge)
        self.candidate_limit_reached = False

    async def enrich_memory(self, record):
        return await self.lineage.enrich(record) if self.lineage is not None else record

    async def list_sdk_raw(self, *, user_id, limit, offset):
        if self.originals is None:
            return await self.bridge.list_sdk_raw(user_id=user_id, limit=limit, offset=offset)
        records, original_total = await self.originals.page(user_id, limit, offset)
        remaining = max(0, offset + limit - max(offset, original_total))
        # The ordinary bridge's list contract requires a positive limit. Use one
        # metadata page to learn its total when this page is wholly in originals.
        native, native_total = await self.bridge.list_sdk_raw(
            user_id=user_id, limit=max(1, remaining), offset=max(0, offset - original_total)
        )
        if any(record.raw_id.startswith(ORIGINAL_PREFIX) for record in native):
            raise ValueError("read_reserved_identity_conflict")
        return list(records) + list(native[:remaining]), original_total + native_total

    async def get_sdk_raw(self, identity):
        if self.originals is not None and identity.startswith(ORIGINAL_PREFIX):
            if await self.bridge.get_sdk_raw(identity) is not None:
                raise ValueError("read_reserved_identity_conflict")
            return await self.originals.get(identity)
        return await self.bridge.get_sdk_raw(identity)

    async def get_sdk_memory(self, identity):
        if identity.startswith(ORIGINAL_PREFIX):
            return None
        if identity.startswith(PREFIX):
            if await self.bridge.get_sdk_memory(identity) is not None:
                raise ValueError("read_reserved_identity_conflict")
            return await self.productivity.get(identity)
        return await self.bridge.get_sdk_memory(identity)

    @staticmethod
    def _ordinary(records):
        if any(
            identity.startswith((PREFIX, ORIGINAL_PREFIX))
            for record in records
            for identity in (record.memory_id, *getattr(record, "member_ids", ()))
        ):
            raise ValueError("read_reserved_identity_conflict")
        return records

    async def list_sdk_memories(self, *, user_id, statuses, limit, offset):
        async with self.bridge.exclusive_access():
            page = await self.bridge.list_sdk_memories(
                user_id=user_id, statuses=statuses, limit=limit, offset=offset
            )
            normal = self._ordinary(page.memories)
            # Offset counts native slots, including ordinary rows filtered at hydration.
            remaining = max(0, offset + limit - max(offset, page.total))
            added, total = await self.productivity.page(
                user_id, remaining, max(0, offset - page.total)
            )
            return HySdkMemoryPage(
                memories=tuple(normal) + tuple(added),
                total=page.total + total,
                limit=limit,
                offset=offset,
            )

    async def search_memories(self, query, *, user_id, agent_id, limit):
        async with self.bridge.exclusive_access():
            normal = self._ordinary(
                await self.bridge.search_memories(
                    query, user_id=user_id, agent_id=agent_id, limit=limit
                )
            )
            groups = await self.productivity.search(user_id, query, limit)
            # A normal hit can represent many memories. Interleave individual IDs,
            # never let the first aggregate consume the whole response budget.
            ordinary, seen = [], set()
            for hit in normal:
                for identity in hit.member_ids or (hit.memory_id,):
                    if identity in seen:
                        continue
                    if len(seen) >= 64:
                        self.candidate_limit_reached = True
                        break
                    seen.add(identity)
                    ordinary.append(SemanticMemoryHit(memory_id=identity, score=hit.score))
                if self.candidate_limit_reached:
                    break
            channels = [ordinary] + [
                [SemanticMemoryHit(memory_id=row.memory_id, score=0.0) for row in group]
                for group in groups
            ]
            # RemoteRead hydrates and filters before applying the overall limit.
            return tuple(interleave(*channels))
