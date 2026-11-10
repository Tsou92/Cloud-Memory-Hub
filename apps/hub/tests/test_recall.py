"""Shared rendering budget for relevant memories and fresh original history."""

import json
from uuid import uuid4

from cloud_memory_hub.read_contract import ReadItem, ReadReply
from cloud_memory_hub.recall import MAX_CONTEXT_BYTES, render_context


def test_mixed_reads_share_one_content_item_and_byte_budget():
    instance, device, scope = (str(uuid4()) for _ in range(3))
    replies = [
        ReadReply(
            instance_id=instance,
            device_id=device,
            scope_id=scope,
            device_revision=3,
            operation=operation,
            next_offset=5 if operation == "history-list" else None,
            items=tuple(
                ReadItem(
                    record_id=f"{operation}:{index}",
                    kind="history" if operation == "history-list" else "memory",
                    content="🧠" * 4096,
                    content_offset=0,
                    agent_id="synthetic",
                    source_raw_ids=("synthetic-source",),
                )
                for index in range(5)
            ),
        )
        for operation in ("memory-search", "history-list")
    ]
    result = render_context(replies, recent=True)
    records = [json.loads(line) for line in result.text.splitlines() if line.startswith("{")]
    assert records[0]["memory_id"] == "memory-search:0"
    assert records[1]["memory_id"] == "history-list:0"
    assert result.records == len(records) <= 5
    assert sum(len(record["content"]) for record in records) <= 6000
    assert len(result.text.encode()) <= MAX_CONTEXT_BYTES
    assert result.truncated and "recall budget" in result.text
    assert result.text.count("</cmh_memory_context>") == 1
