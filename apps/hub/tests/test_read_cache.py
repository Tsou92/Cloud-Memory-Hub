"""Offline reads serve the last reply only for retryable transport failures."""

import asyncio
import os
from uuid import uuid4

import pytest
from cloud_memory_hub.agent_access import AgentAccess, AgentAccessError
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.read_cache import ReadCache
from cloud_memory_hub.read_contract import ReadReply
from cloud_memory_hub.transport import RemoteConnection, TransportError


def _ids():
    return str(uuid4()), str(uuid4())


def _cache(tmp_path, instance_id, device_id):
    return ReadCache(tmp_path / "read-cache.sqlite3", instance_id=instance_id, device_id=device_id)


def _reply(instance_id, device_id, operation="memory-search"):
    return ReadReply(
        instance_id=instance_id,
        device_id=device_id,
        device_revision=1,
        scope_id="global",
        operation=operation,
        items=(),
        next_offset=None,
    )


def _access(tmp_path, cache, instance_id, device_id):
    connection = RemoteConnection(
        origin="http://127.0.0.1:9",
        instance_id=instance_id,
        device_id=device_id,
        credential="cmhdev_v1_" + device_id + "_" + "c" * 43,
    )
    outbox = LocalOutbox.initialize(
        tmp_path / "outbox.sqlite3", instance_id=instance_id, device_id=device_id
    )
    return AgentAccess(connection, outbox, scope_id="global", source_id=str(uuid4()), cache=cache)


def test_roundtrip_marks_the_reply_and_keeps_it_private(tmp_path):
    instance_id, device_id = _ids()
    cache = _cache(tmp_path, instance_id, device_id)
    cache.put("global", "memory-search", {"query": "x"}, {"items": [1], "next_offset": None})
    value = cache.get("global", "memory-search", {"query": "x"})
    assert value["items"] == [1]
    assert value["served_from_cache"] is True
    assert value["cached_at"].endswith("+00:00")
    assert os.stat(cache.path).st_mode & 0o777 == 0o600


def test_a_different_request_scope_or_operation_misses(tmp_path):
    instance_id, device_id = _ids()
    cache = _cache(tmp_path, instance_id, device_id)
    cache.put("global", "memory-search", {"query": "x"}, {"items": [1]})
    assert cache.get("global", "memory-search", {"query": "y"}) is None
    assert cache.get("other-scope", "memory-search", {"query": "x"}) is None
    assert cache.get("global", "history-list", {"query": "x"}) is None


def test_entries_expire(tmp_path, monkeypatch):
    instance_id, device_id = _ids()
    cache = _cache(tmp_path, instance_id, device_id)
    cache.put("global", "memory-search", {"query": "x"}, {"items": []})
    now = __import__("time").time()
    monkeypatch.setattr("cloud_memory_hub.read_cache.time.time", lambda: now + 31 * 24 * 3600)
    assert cache.get("global", "memory-search", {"query": "x"}) is None


def test_a_corrupt_cache_only_disables_offline_reads(tmp_path):
    path = tmp_path / "read-cache.sqlite3"
    path.write_bytes(b"not a database")
    cache = ReadCache(path, instance_id=str(uuid4()), device_id=str(uuid4()))
    assert cache.get("global", "memory-search", {"query": "x"}) is None
    cache.put("global", "memory-search", {"query": "x"}, {"items": []})


def test_success_is_cached_and_served_when_the_transport_fails(tmp_path, monkeypatch):
    instance_id, device_id = _ids()
    cache = _cache(tmp_path, instance_id, device_id)
    access = _access(tmp_path, cache, instance_id, device_id)
    reply = _reply(instance_id, device_id)

    async def online(*_args, **_kwargs):
        return reply

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", online)
    first = asyncio.run(access.search("synthetic"))
    assert "served_from_cache" not in first

    async def offline(*_args, **_kwargs):
        raise TransportError("transport_unavailable", retryable=True)

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", offline)
    second = asyncio.run(access.search("synthetic"))
    assert second["served_from_cache"] is True
    assert second["cached_at"]
    assert second["items"] == first["items"]


def test_a_non_retryable_failure_is_never_masked(tmp_path, monkeypatch):
    instance_id, device_id = _ids()
    cache = _cache(tmp_path, instance_id, device_id)
    access = _access(tmp_path, cache, instance_id, device_id)

    async def online(*_args, **_kwargs):
        return _reply(instance_id, device_id)

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", online)
    asyncio.run(access.search("synthetic"))

    async def revoked(*_args, **_kwargs):
        raise TransportError("scope_permission_required")

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", revoked)
    with pytest.raises(AgentAccessError) as error:
        asyncio.run(access.search("synthetic"))
    assert error.value.code == "scope_permission_required"


def test_a_different_query_is_not_served_from_another_requests_cache(tmp_path, monkeypatch):
    instance_id, device_id = _ids()
    cache = _cache(tmp_path, instance_id, device_id)
    access = _access(tmp_path, cache, instance_id, device_id)

    async def online(*_args, **_kwargs):
        return _reply(instance_id, device_id)

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", online)
    asyncio.run(access.search("first"))

    async def offline(*_args, **_kwargs):
        raise TransportError("transport_unavailable", retryable=True)

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", offline)
    with pytest.raises(AgentAccessError):
        asyncio.run(access.search("second"))


def test_without_a_cache_a_transport_failure_still_raises(tmp_path, monkeypatch):
    instance_id, device_id = _ids()
    access = _access(tmp_path, None, instance_id, device_id)

    async def offline(*_args, **_kwargs):
        raise TransportError("transport_unavailable", retryable=True)

    monkeypatch.setattr("cloud_memory_hub.agent_access.read_remote", offline)
    with pytest.raises(AgentAccessError):
        asyncio.run(access.search("synthetic"))
