"""History full-text search: request shape, query filtering and paging.

The archive itself is covered by the session-history fixture; these tests pin
the parts that are easy to break by accident: the request must not be mistaken
for a list request, the query is normalized and filtered like every other
query, and paging follows the archive's has-more answer at any depth.
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
from cloud_memory_hub.remote_read import RemoteRead
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


class _Originals:
    def __init__(self, total):
        self.total = total
        self.calls = []

    async def search(self, namespace, query, *, limit, offset):
        self.calls.append((namespace, query, limit, offset))
        items = [_Raw(index) for index in range(offset, min(offset + limit, self.total))]
        return items, offset + limit < self.total


def _pager(originals):
    reader = RemoteRead(_Access(), lambda: None, originals=originals)

    async def bounded(read):
        return await read()

    reader._bounded_read = bounded
    return reader


def _page(reader, *, offset, limit):
    request = HistorySearchRequest(scope_id="global", query="原文", offset=offset, limit=limit)
    return asyncio.run(reader._read("history-search", request, CredentialFilter()))


def test_paging_follows_the_archive_at_any_depth():
    originals = _Originals(total=120)
    reader = _pager(originals)

    first = _page(reader, offset=0, limit=10)
    assert [item.record_id for item in first.items] == [f"raw-{i}" for i in range(10)]
    assert first.next_offset == 10

    last = _page(reader, offset=115, limit=10)
    assert [item.record_id for item in last.items] == [f"raw-{i}" for i in range(115, 120)]
    assert last.next_offset is None

    beyond = _page(reader, offset=1000, limit=10)
    assert beyond.items == () and beyond.next_offset is None
    assert originals.calls[0][:3] == ("cmh:instance-1:global", "原文", 10)


def test_search_without_the_archive_is_unavailable():
    reader = _pager(None)
    with pytest.raises(AccessError) as error:
        _page(reader, offset=0, limit=10)
    assert error.value.code == "read_unavailable"
