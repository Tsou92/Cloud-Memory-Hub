"""Device-authorized reads of the upstream SDK authority; no second store."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from one_memory.hy_memory_bridge import HyMemoryBridge, HySdkMemoryRecord
from starlette.concurrency import run_in_threadpool

from cloud_memory_hub.privacy import CredentialFilter, PrivacyError
from cloud_memory_hub.productivity_read import ProductivityMemory
from cloud_memory_hub.read_contract import (
    READ_OPERATIONS,
    READ_RESPONSE_BYTES,
    TEXT_BLOCK,
    GetRequest,
    ListRequest,
    ReadItem,
    ReadReply,
    SearchRequest,
)
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore

MEMBER_BUDGET = 64


@dataclass(frozen=True)
class ScopedReadPage:
    """Content shared by device and administrator replies, without caller identity."""

    items: tuple[ReadItem, ...]
    next_offset: int | None
    candidate_limit_reached: bool


class RemoteRead:
    def __init__(
        self,
        access: DeviceAccessStore,
        bridge: Callable[[], HyMemoryBridge],
        suppressed: Callable[[str], dict] | None = None,
    ):
        self.access = access
        self.bridge = bridge
        # Hub forget table (ADR-0032), independent of SDK status which may change.
        self.suppressed = suppressed or (lambda scope: {})
        self._slots = asyncio.Semaphore(2)

    async def read(self, credential: str, operation: str, request) -> dict:
        permission = "history:read" if operation.startswith("history-") else "memory:read"
        before = await run_in_threadpool(
            self.access.require_permission, credential, request.scope_id, permission
        )
        privacy = CredentialFilter((credential,))
        page = await self.query(operation, request, privacy=privacy)
        result = self.response(
            ReadReply,
            page,
            privacy,
            scope_id=request.scope_id,
            instance_id=self.access.instance_id,
            device_id=before["device_id"],
            device_revision=before["revision"],
            operation=operation,
        )
        after = await run_in_threadpool(
            self.access.require_permission, credential, request.scope_id, permission
        )
        if after["revision"] != before["revision"]:
            raise AccessError("read_authorization_changed", 409)
        return result

    async def query(self, operation, request, *, privacy):
        """Run the common bounded read after the caller's own authorization checks."""
        if operation not in READ_OPERATIONS or not isinstance(
            request, READ_OPERATIONS[operation][1]
        ):
            raise AccessError("invalid_request", 422)
        if isinstance(request, SearchRequest):
            try:
                if privacy.protect({"query": request.query}).value["query"] != request.query:
                    raise AccessError("query_requires_refiltering", 422)
            except PrivacyError:
                raise AccessError("query_requires_refiltering", 422) from None
        # Keep the slot until upstream's joined worker has actually exited on cancellation.
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise AccessError("read_busy", 503) from None
        try:
            return await self._read(operation, request, privacy)
        except AccessError:
            raise
        except Exception:
            # SDK exceptions may contain input or model configuration. No cause/echo.
            raise AccessError("read_unavailable", 503) from None
        finally:
            self._slots.release()

    async def _read(self, operation, request, privacy):
        namespace = f"cmh:{self.access.instance_id}:{request.scope_id}"
        bridge = self.bridge()
        hidden = await run_in_threadpool(self.suppressed, request.scope_id)

        def visible(record, *, history):
            if record is None:
                return False
            identity = record.raw_id if history else record.memory_id
            # Only an exact history-get may read a forgotten original. Lists
            # also feed automatic recall; purged records stay hidden everywhere.
            if identity in hidden and (operation != "history-get" or hidden[identity] == "purged"):
                return False
            return self._visible(record, namespace, history=history)

        history = operation.startswith("history-")
        next_offset, limited = None, False
        if isinstance(request, SearchRequest):
            hits = await bridge.search_memories(
                request.query, user_id=namespace, agent_id="*", limit=request.limit
            )
            records, seen = [], set()
            for hit in hits:
                for memory_id in hit.member_ids or (hit.memory_id,):
                    if memory_id in seen:
                        continue
                    if len(seen) >= MEMBER_BUDGET:
                        limited = True
                        break
                    seen.add(memory_id)
                    record = await bridge.get_sdk_memory(memory_id)
                    if visible(record, history=False):
                        records.append(record)
                    if len(records) >= request.limit:
                        break
                if limited or len(records) >= request.limit:
                    break
            limited = limited or getattr(bridge, "candidate_limit_reached", False)
        elif isinstance(request, ListRequest):
            if history:
                records, total = await bridge.list_sdk_raw(
                    user_id=namespace, limit=request.limit, offset=request.offset
                )
            else:
                page = await bridge.list_sdk_memories(
                    user_id=namespace,
                    statuses=("active",),
                    limit=request.limit,
                    offset=request.offset,
                )
                # The pinned SDK list serializer omits is_latest. Its bridge defaults
                # that missing flag to True, so hydrate current authority by ID.
                records = [await bridge.get_sdk_memory(r.memory_id) for r in page.memories]
                total = page.total
            if request.offset + request.limit < total:
                next_offset = request.offset + request.limit
            records = [r for r in records if visible(r, history=history)]
        else:
            assert isinstance(request, GetRequest)
            record = await (
                bridge.get_sdk_raw(request.record_id)
                if history
                else bridge.get_sdk_memory(request.record_id)
            )
            if not visible(record, history=history):
                raise AccessError("record_not_found", 404)
            records = [record]
        enrich = getattr(bridge, "enrich_memory", None)
        if not history and enrich is not None:
            records = [await enrich(record) for record in records]
        offset = request.content_offset if isinstance(request, GetRequest) else 0
        return ScopedReadPage(
            items=tuple(self._item(record, privacy, offset) for record in records),
            next_offset=next_offset,
            candidate_limit_reached=limited,
        )

    @staticmethod
    def response(model, page, privacy, **identity):
        try:
            reply = model(
                **identity,
                items=page.items,
                next_offset=page.next_offset,
                candidate_limit_reached=page.candidate_limit_reached,
            )
            if len(reply.model_dump_json().encode()) > READ_RESPONSE_BYTES:
                raise AccessError("read_response_too_large", 503)
            # Reject unsafe identities/references instead of rewriting their meaning.
            value = reply.model_dump(mode="json")
            metadata = {
                **value,
                "items": [
                    {k: v for k, v in item.items() if k != "content"} for item in value["items"]
                ],
            }
            if privacy.protect(metadata).value != metadata:
                raise AccessError("read_metadata_requires_review", 503)
            return value
        except AccessError:
            raise
        except Exception:
            raise AccessError("read_unavailable", 503) from None

    @staticmethod
    def _visible(record, namespace, *, history):
        return (
            record is not None
            and record.user_id == namespace
            and (history or (record.status == "active" and record.is_latest))
        )

    @staticmethod
    def _item(record, privacy, offset):
        memory = isinstance(record, HySdkMemoryRecord)
        # Filter the complete text before slicing so secrets cannot straddle blocks.
        clean = privacy.protect(
            {
                "content": record.content,
                "agent_id": record.agent_id,
                "session_id": record.session_id,
                # SDK memory get() labels its storage backend as `source` (vdb/graph).
                # Native productivity records and raw records expose the actual source.
                "source": (
                    None if memory and not isinstance(record, ProductivityMemory) else record.source
                ),
            }
        ).value
        content = clean.pop("content")
        if offset > len(content):
            raise AccessError("content_offset_invalid", 422)
        references = record.source_raw_ids if memory else ()
        return ReadItem(
            record_id=record.memory_id if memory else record.raw_id,
            kind="memory" if memory else "history",
            content=content[offset : offset + TEXT_BLOCK],
            content_offset=offset,
            next_content_offset=offset + TEXT_BLOCK if len(content) > offset + TEXT_BLOCK else None,
            source_raw_ids=references[:32],
            references_truncated=len(references) > 32,
            layer=record.layer if memory else None,
            memory_at=record.memory_at,
            **clean,
        )
