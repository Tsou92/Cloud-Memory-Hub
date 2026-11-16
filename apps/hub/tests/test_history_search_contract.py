"""History full-text search: request shape, query filtering and paging.

The HTTP path is covered by the remote-read fixture; these tests pin the
parts that are easy to break by accident: the request must not be mistaken
for a list request, the query is normalized and filtered like every other
query, and paging slices the SDK's offset-less result without inventing an
endless cursor past the search cap.
"""

import asyncio
from types import SimpleNamespace

import pytest
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import (
    READ_OPERATIONS,
    HistorySearchRequest,
    ListRequest,
)
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_read import MAX_HISTORY_SEARCH, RemoteRead
from pydantic import ValidationError


class _Privacy:
    def protect(self, value):
        if "credential" in value.get("query", ""):
            from cloud_memory_hub.privacy import PrivacyError

            raise PrivacyError("query_requires_refiltering")
        return SimpleNamespace(value=value)


def test_request_is_not_a_list_request():
    """The read path dispatches ListRequest first; the types must stay distinct."""
    assert not issubclass(HistorySearchRequest, ListRequest)
    assert READ_OPERATIONS["history-search"] == (
        "/hub/v1/history/search",
        HistorySearchRequest,
    )


def test_query_is_normalized_and_filtered():
    reader = RemoteRead.__new__(RemoteRead)

    async def bounded(read):
        return await read()

    reader._bounded_read = bounded

    async def fake_read(operation, request, privacy):
        return request

    reader._read = fake_read
    request = HistorySearchRequest(scope_id="global", query="ＡＢＣ")
    result = asyncio.run(reader.query("history-search", request, privacy=_Privacy()))
    # NFKC normalization applies before the search reaches the SDK.
    assert result.query == "ABC"

    bad = HistorySearchRequest(scope_id="global", query="my credential here")
    with pytest.raises(AccessError) as error:
        asyncio.run(reader.query("history-search", bad, privacy=_Privacy()))
    assert error.value.code == "query_requires_refiltering"


def test_offset_is_validated():
    with pytest.raises(ValidationError):
        HistorySearchRequest(scope_id="global", query="x", offset=-1)
    with pytest.raises(ValidationError):
        HistorySearchRequest(scope_id="global", query="x", limit=11)
    assert MAX_HISTORY_SEARCH >= 10


def test_search_request_still_requires_a_query():
    with pytest.raises(ValidationError):
        HistorySearchRequest(scope_id="global", query="   ")


class _Access:
    instance_id = "instance-1"


class _Raw:
    def __init__(self, index):
        self.raw_id = f"raw-{index}"
        self.user_id = "cmh:instance-1:global"
        self.content = f"合成原文 {index}"
        self.agent_id = "synthetic"
        self.session_id = "synthetic"
        self.source = "synthetic"
        self.memory_at = 1


class _RawBridge:
    def __init__(self, total):
        self.total = total
        self.limits = []

    async def search_sdk_raw(self, query, *, user_id, limit):
        self.limits.append(limit)
        return [_Raw(index) for index in range(min(limit, self.total))]


def _pager(bridge):
    reader = RemoteRead(_Access(), lambda: bridge)

    async def bounded(read):
        return await read()

    reader._bounded_read = bounded
    return reader


def _page(reader, *, offset, limit):
    request = HistorySearchRequest(scope_id="global", query="原文", offset=offset, limit=limit)
    return asyncio.run(reader._read("history-search", request, CredentialFilter()))


def test_unified_bridge_delegates_raw_search():
    """The app wires RemoteRead to UnifiedReadBridge, not the raw SDK bridge."""
    from cloud_memory_hub.unified_read import UnifiedReadBridge

    calls = []

    class _Sdk:
        async def search_sdk_raw(self, query, *, user_id, limit):
            calls.append((query, user_id, limit))
            return [_Raw(0)]

    hits = asyncio.run(
        UnifiedReadBridge(_Sdk()).search_sdk_raw("原文", user_id="cmh:instance-1:global", limit=5)
    )
    assert [hit.raw_id for hit in hits] == ["raw-0"]
    assert calls == [("原文", "cmh:instance-1:global", 5)]


def test_unified_bridge_refuses_a_reserved_raw_identity():
    from cloud_memory_hub.unified_read import UnifiedReadBridge

    class _Colliding(_Raw):
        def __init__(self):
            super().__init__(0)
            self.raw_id = "cmhe:receipt-1"

    class _Sdk:
        async def search_sdk_raw(self, query, *, user_id, limit):
            return [_Colliding()]

    with pytest.raises(ValueError, match="read_reserved_identity_conflict"):
        asyncio.run(
            UnifiedReadBridge(_Sdk()).search_sdk_raw(
                "原文", user_id="cmh:instance-1:global", limit=5
            )
        )


def test_paging_advances_until_the_search_cap():
    bridge = _RawBridge(total=120)
    reader = _pager(bridge)

    first = _page(reader, offset=0, limit=10)
    assert [item.record_id for item in first.items] == [f"raw-{i}" for i in range(10)]
    assert first.next_offset == 10

    last = _page(reader, offset=MAX_HISTORY_SEARCH - 5, limit=10)
    assert [item.record_id for item in last.items] == [
        f"raw-{i}" for i in range(MAX_HISTORY_SEARCH - 5, MAX_HISTORY_SEARCH)
    ]
    assert last.next_offset is None

    beyond = _page(reader, offset=MAX_HISTORY_SEARCH, limit=10)
    assert beyond.items == () and beyond.next_offset is None
