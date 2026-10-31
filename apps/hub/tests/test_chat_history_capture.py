import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from hy_memory.config import MemoryConfig
from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus
from hy_memory.pipelines.writer import MemoryWriter, _chat_history_evidence
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_original_archive import config_for
from test_output_lineage import fixture


def node(index, text, **kwargs):
    return MemoryNode(
        node_id=str(index),
        user_id="synthetic-user",
        agent_id="synthetic-agent",
        content=text,
        layer=MemoryLayer.L1_RAW,
        gmt_created=datetime(2026, 1, 1) + timedelta(seconds=index),
        custom={"request_id": "request-" + str(index)},
        **kwargs,
    )


async def history(nodes, *, writer=None, **kwargs):
    async def query(**query_args):
        assert query_args["user_id"] == "synthetic-user"
        assert query_args["agent_id"] == "synthetic-agent"
        assert query_args["layers"] == [MemoryLayer.L1_RAW]
        assert query_args["status_filter"] == [MemoryStatus.ACTIVE, MemoryStatus.SHADOW]
        assert query_args["allow_raw_layer"] is True
        assert query_args["with_vectors"] is False
        return list(nodes)

    capture = {}
    rendered = await (writer or MemoryWriter(MemoryConfig()))._get_recent_history(
        SimpleNamespace(list_by_user=query),
        "synthetic-user",
        "synthetic-agent",
        capture=capture,
        **kwargs,
    )
    return rendered, capture


@pytest.mark.parametrize("window,expected", [(1, [3]), (2, [2, 3]), (3, [2, 3]), (9, [1, 2, 3])])
def test_chat_history_window_tracks_actual_messages(window, expected):
    nodes = [
        node(1, "[user]: same\n[assistant]: first"),
        node(2, "[user]: same\nsecond line\n[assistant]: " + "X" * 20),
        node(3, "[user]: same"),
        node(4, "[user]: current"),
    ]
    text, capture = asyncio.run(
        history(
            list(reversed(nodes)), max_turns=window, max_chars_assistant=5, exclude_memory_id="4"
        )
    )
    assert capture["status"] == "complete"
    assert capture["rendered"] == text
    assert [row["node_id"] for row in capture["records"]] == list(map(str, expected))
    assert all(row["request_id"] == "request-" + row["node_id"] for row in capture["records"])
    assert "current" not in text
    if window > 1:
        assert "[assistant]: XXXXX..." in text
    # Mutating a returned native object cannot rewrite the archived input.
    nodes[2].content = "changed after capture"
    assert capture["records"][-1]["content"] == "[user]: same"


def test_name_outside_window_and_cross_node_continuation_are_tracked():
    text, capture = asyncio.run(
        history(
            [
                node(1, "用户名叫Tim\n[user]: omitted"),
                node(2, "[user]: first line"),
                node(3, "continuation\n[assistant]: answer"),
            ],
            max_turns=2,
        )
    )
    assert "Tim" in text and "omitted" not in text
    assert "[user]: first line\ncontinuation" in text
    assert [row["node_id"] for row in capture["records"]] == ["1", "2", "3"]


@pytest.mark.parametrize("limit,expected", [(5, [1]), (30, [1, 2]), (-1, [1, 2])])
def test_trimmed_continuation_is_not_a_visible_source(limit, expected):
    _, capture = asyncio.run(
        history([node(1, "[assistant]: abcdef"), node(2, "later text")], max_chars_assistant=limit)
    )
    assert [row["node_id"] for row in capture["records"]] == list(map(str, expected))


@pytest.mark.parametrize("nodes", [[], [node(1, "用户名叫Tim")], [node(1, "[user]: current")]])
def test_observed_empty_history_is_complete(nodes):
    text, capture = asyncio.run(history(nodes, exclude_memory_id="1"))
    assert text == ""
    assert capture == {"schema_version": 1, "status": "complete", "records": [], "rendered": ""}


def test_legacy_unknown_origin_is_captured_without_inventing_a_request():
    original = node(1, "[user]: older import")
    original.custom = {}
    _, capture = asyncio.run(history([original]))
    assert capture["status"] == "complete"
    assert capture["records"][0]["request_id"] is None


def test_read_failure_is_not_claimed_as_an_observed_empty_history():
    async def fail(**kwargs):
        raise OSError("synthetic store unavailable")

    capture = {}
    result = asyncio.run(
        MemoryWriter(MemoryConfig())._get_recent_history(
            SimpleNamespace(list_by_user=fail), "user", capture=capture
        )
    )
    assert result == ""
    assert capture == {"schema_version": 1, "status": "unavailable", "records": [], "rendered": ""}


@pytest.mark.parametrize("case", ["bytes", "count", "invalid", "duplicate", "missing"])
def test_capture_failure_never_returns_partial_complete_evidence(case):
    nodes, identities, rendered = [node(1, "text")], {"1"}, "text"
    if case == "bytes":
        nodes[0].content = "界" * (1024 * 1024)
    elif case == "count":
        identities = set(map(str, range(513)))
    elif case == "invalid":
        nodes[0].custom = {"request_id": {"invalid": True}}
    elif case == "duplicate":
        nodes += nodes
    else:
        identities = {"absent"}
    assert _chat_history_evidence(nodes, identities, rendered) == {
        "schema_version": 1,
        "status": "unavailable",
        "records": [],
        "rendered": "",
    }


def test_capture_is_call_local_under_concurrent_reads():
    async def scenario():
        writer = MemoryWriter(MemoryConfig())
        results = await asyncio.gather(
            *(history([node(i, f"[user]: {i}")], writer=writer) for i in range(1, 9))
        )
        assert [[row["node_id"] for row in value[1]["records"]] for value in results] == [
            [str(i)] for i in range(1, 9)
        ]

    asyncio.run(scenario())


def test_only_last_name_source_survives_when_older_messages_are_cropped():
    text, capture = asyncio.run(
        history(
            [
                node(1, "用户名叫Tim\n[user]: old"),
                node(2, "用户名叫Amy\n[user]: old"),
                node(3, "[user]: new"),
            ],
            max_turns=1,
        )
    )
    assert "Amy" in text and "Tim" not in text
    assert [row["node_id"] for row in capture["records"]] == ["2", "3"]


def test_capture_budget_does_not_change_the_existing_history_text():
    body = "[user]: " + "界" * (1024 * 1024)
    text, capture = asyncio.run(history([node(1, body)]))
    assert text == body
    assert capture == {"schema_version": 1, "status": "unavailable", "records": [], "rendered": ""}


def test_real_chat_writer_persists_exact_history_and_replays_without_agent(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    requests = [str(uuid4()), str(uuid4())]
    results, seen = [], []

    async def agent(self, **kwargs):
        seen.append(kwargs["history_context"])
        return AgentResult(success=True, extracted_info={"memory": [], "entities": []})

    monkeypatch.setattr(MemAgent, "process_add", agent)

    async def scenario():
        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            for request in requests:
                result = await _joined_thread(
                    sdk.add,
                    [{"role": "user", "content": "Synthetic chat history"}],
                    user_id="scope",
                    agent_id="agent",
                    request_id=request,
                    scene="chat",
                )
                assert result["success"] is True
                results.append(result)
            assert results[0]["chat_history"]["records"] == []
            capture = results[1]["chat_history"]
            assert capture["status"] == "complete"
            assert capture["rendered"] == seen[1]
            assert capture["records"][0]["node_id"] == results[0]["memory_id"]
            # Native L1 writes carry no request marker. Hub must resolve the
            # exact raw ID through its proven output links, never invent one.
            assert capture["records"][0]["request_id"] is None

        async def forbidden(*args, **kwargs):
            pytest.fail("idempotent replay reran the model stage")

        monkeypatch.setattr(MemAgent, "process_add", forbidden)
        async with fixture(config) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            replay = await _joined_thread(
                sdk.add,
                [{"role": "user", "content": "Synthetic chat history"}],
                user_id="scope",
                agent_id="agent",
                request_id=requests[1],
                scene="chat",
            )
            assert replay["chat_history"] == results[1]["chat_history"]
        assert len(seen) == 2

    asyncio.run(scenario())


def test_lite_write_explicitly_records_that_history_was_not_used(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("lite writer accessed model history")

    monkeypatch.setattr(MemoryWriter, "_get_recent_history", forbidden)

    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            sdk._mode = "lite"
            result = await _joined_thread(
                sdk.add,
                "Synthetic lite write",
                user_id="scope",
                request_id=str(uuid4()),
                scene="chat",
            )
            assert result["success"] is True
            assert result["chat_history"] == {
                "schema_version": 1,
                "status": "not_used",
                "records": [],
                "rendered": "",
            }
            assert result["chat_reconcile_inputs"] == {
                "schema_version": 1,
                "status": "not_used",
                "stages": [],
            }

    asyncio.run(scenario())
