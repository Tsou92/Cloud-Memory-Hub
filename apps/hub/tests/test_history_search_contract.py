"""History full-text search: request shape, query filtering and paging.

The end-to-end path is covered by the existing remote-read fixture; these
tests pin the parts that are easy to break by accident: the request must not
be mistaken for a list request, the query is normalized and filtered like
every other query, and paging slices the SDK's offset-less result.
"""

import asyncio
from types import SimpleNamespace

import pytest
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
