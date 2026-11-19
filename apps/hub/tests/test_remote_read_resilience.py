import asyncio

from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import ListRequest
from cloud_memory_hub.remote_read import RemoteRead
from one_memory.hy_memory_bridge import HySdkMemoryPage, HySdkMemoryRecord


class _Access:
    instance_id = "test-instance"


def _record(memory_id):
    return HySdkMemoryRecord(
        memory_id=memory_id,
        content=f"synthetic memory {memory_id}",
        layer="l2_fact",
        status="active",
        is_latest=True,
        user_id="cmh:test-instance:global",
        agent_id="agent-a",
        memory_at=1,
        gmt_created=1,
    )


class _Bridge:
    async def list_sdk_memories(self, *, user_id, statuses, limit, offset):
        return HySdkMemoryPage(
            memories=(_record("a"), _record("b")),
            total=2,
            limit=limit,
            offset=offset,
        )

    async def get_sdk_memory(self, memory_id):
        if memory_id == "b":
            raise RuntimeError("synthetic hydration failure")
        return _record(memory_id)

    async def enrich_memory(self, record):
        if record.memory_id == "a":
            raise RuntimeError("synthetic lineage failure")
        return record


def test_one_failed_hydration_or_lineage_does_not_hide_the_library():
    reader = RemoteRead(_Access(), _Bridge)
    page = asyncio.run(
        reader.query(
            "memory-list",
            ListRequest(scope_id="global", limit=10, offset=0),
            privacy=CredentialFilter(),
        )
    )
    assert {item.record_id for item in page.items} == {"a", "b"}
    assert all(item.kind == "memory" for item in page.items)
