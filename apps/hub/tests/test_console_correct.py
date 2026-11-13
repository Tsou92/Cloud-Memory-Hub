"""Correction must never cross a project boundary.

The pinned SDK update() is unscoped, so the Hub re-reads the record first and
requires its user_id to match this scope's namespace. A record from another
project must answer exactly like a missing one.
"""

import asyncio
from types import SimpleNamespace

import pytest
from cloud_memory_hub.console_correct import CorrectRequest, correct_memory
from cloud_memory_hub.remote_access import AccessError


class _Bridge:
    def __init__(self, user_id, updated=None):
        self.user_id = user_id
        self.updated = updated
        self.updates = []

    async def get_sdk_memory(self, memory_id):
        if memory_id == "missing":
            return None
        if memory_id == "boom":
            raise RuntimeError("sdk down")
        return SimpleNamespace(memory_id=memory_id, user_id=self.user_id, content="旧内容")

    async def update(self, memory_id, content):
        self.updates.append((memory_id, content))


def _console(instance_id, bridge):
    async def require_scope(scope):
        return None

    return SimpleNamespace(
        access=SimpleNamespace(instance_id=instance_id),
        reads=SimpleNamespace(bridge=bridge),
        require_scope=require_scope,
    )


def test_corrects_a_record_in_this_scope():
    bridge = _Bridge("cmh:instance-1:11111111-1111-4111-8111-111111111111")
    request = CorrectRequest(
        scope_id="11111111-1111-4111-8111-111111111111", record_id="mem-1", content="新内容"
    )
    result = asyncio.run(correct_memory(_console("instance-1", bridge), request))
    assert result["state"] == "corrected" and result["record_id"] == "mem-1"
    assert bridge.updates == [("mem-1", "新内容")]


def test_other_project_record_is_indistinguishable_from_missing():
    bridge = _Bridge("cmh:instance-1:22222222-2222-4222-8222-222222222222")
    request = CorrectRequest(
        scope_id="11111111-1111-4111-8111-111111111111", record_id="mem-1", content="x"
    )
    with pytest.raises(AccessError) as error:
        asyncio.run(correct_memory(_console("instance-1", bridge), request))
    assert error.value.code == "record_not_found"
    # The critical part: nothing was written across the boundary.
    assert bridge.updates == []


def test_other_hub_instance_is_rejected():
    bridge = _Bridge("cmh:instance-2:11111111-1111-4111-8111-111111111111")
    request = CorrectRequest(
        scope_id="11111111-1111-4111-8111-111111111111", record_id="mem-1", content="x"
    )
    with pytest.raises(AccessError) as error:
        asyncio.run(correct_memory(_console("instance-1", bridge), request))
    assert error.value.code == "record_not_found"
    assert bridge.updates == []


def test_missing_and_failing_reads_do_not_write():
    for memory_id, code in (("missing", "record_not_found"), ("boom", "correct_unavailable")):
        bridge = _Bridge("cmh:instance-1:11111111-1111-4111-8111-111111111111")
        request = CorrectRequest(
            scope_id="11111111-1111-4111-8111-111111111111", record_id=memory_id, content="x"
        )
        with pytest.raises(AccessError) as error:
            asyncio.run(correct_memory(_console("instance-1", bridge), request))
        assert error.value.code == code
        assert bridge.updates == []


def test_content_is_bounded_and_rejects_whitespace():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CorrectRequest(
            scope_id="11111111-1111-4111-8111-111111111111", record_id="m", content="   "
        )
    with pytest.raises(ValidationError):
        CorrectRequest(
            scope_id="11111111-1111-4111-8111-111111111111", record_id="m", content="x" * 16385
        )
