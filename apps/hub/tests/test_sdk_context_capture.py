import asyncio
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hy_memory.client import HyMemoryClient
from hy_memory.coding.chain_store import HyCodeMem
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, CodingMemoryDraft, ReconcileOp
from hy_memory.coding.writer import CodingWriter
from hy_memory.pipelines.base import ChatMessage
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_original_archive import config_for
from test_output_lineage import fixture


def edit_messages(said="Synthetic edit", path="synthetic.py", old="value=1\n", new="value=2\n"):
    return [
        ChatMessage(role="user", content=said),
        ChatMessage(
            role="tool",
            content=f"path='{path}'\nold='''{old}'''\nnew='''{new}'''",
            tool_name="edit",
        ),
    ]


@pytest.mark.parametrize("window", [1, 2, 5])
def test_actual_history_window_preserves_request_identity_without_content_matching(
    tmp_path, window
):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()

            async def run():
                store = CodingMemoryStore(
                    sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path
                )
                await store.initialize()
                try:
                    for index, identity in enumerate(("first", "second", "third")):
                        # Identical content is deliberately not an identity key.
                        messages = [
                            ChatMessage(role="user", content="same"),
                            ChatMessage(role="assistant", content="A" * 7000),
                        ]
                        if identity == "second":
                            messages += [
                                ChatMessage(role="user", content="same"),
                                ChatMessage(role="tool", content="T" * 9000),
                            ]
                        await store.append_trace(
                            messages, request_id=identity, user_id="scope", agent_id="agent"
                        )
                        with sqlite3.connect(store._trace_db_path) as db:
                            db.execute(
                                "UPDATE coding_trace SET created_at=? WHERE request_id=?",
                                (f"2026-01-0{index + 1}", identity),
                            )
                    await store.append_trace(
                        [ChatMessage(content="foreign")],
                        request_id="foreign",
                        user_id="other",
                        agent_id="agent",
                    )
                    await store.append_trace(
                        [], request_id="broken", user_id="scope", agent_id="agent"
                    )
                    with sqlite3.connect(store._trace_db_path) as db:
                        db.execute(
                            "UPDATE coding_trace SET messages_json='[bad',created_at='2026-01-04' "
                            "WHERE request_id='broken'"
                        )
                    identities = []
                    result = await store.get_recent_trace_messages(
                        user_id="scope",
                        agent_id="agent",
                        max_user_turns=window,
                        source_request_ids=identities,
                    )
                    assert (
                        identities
                        == {1: ["third"], 2: ["second", "third"], 5: ["first", "second", "third"]}[
                            window
                        ]
                    )
                    assert len([message for message in result if message.role == "user"]) == min(
                        window, 4
                    )
                    assert all(message.content != "foreign" for message in result)
                    assert all(
                        identity not in message.content
                        for identity in identities
                        for message in result
                    )
                    assert all(
                        len(message.content) < 7100
                        for message in result
                        if message.role == "assistant"
                    )
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(run()))

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["working", "coding"])
@pytest.mark.parametrize("with_output", [False, True])
def test_real_writer_captures_history_and_chain_in_durable_result(
    tmp_path, monkeypatch, variant, with_output
):
    config = config_for(tmp_path)
    request_id = str(uuid4())

    async def scenario():
        async with fixture(config) as (app, _):
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            captures = []
            store = CodingMemoryStore(
                sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
            )

            async def prepare():
                await store.initialize()
                await store.append_trace(
                    [ChatMessage(role="user", content="Previous synthetic request")],
                    request_id="previous",
                    user_id="scope",
                    agent_id="agent",
                )

            async def extract(messages, **kwargs):
                captures.append(kwargs["history_messages"])
                return ([CodingMemoryDraft(task="Synthetic draft")] if with_output else []), []

            async def reconcile(*args, **kwargs):
                identity = str(uuid4())
                await store.insert(
                    CodingMemory(
                        experience_id=identity,
                        task="Synthetic task",
                        solution="Synthetic solution",
                        user_id="scope",
                        agent_id="agent",
                    )
                )
                return [ReconcileOp(action="ADD", draft_idx=0, target_experience_id=identity)]

            writer = CodingWriter(
                store,
                SimpleNamespace(extract=extract),
                SimpleNamespace(reconcile=reconcile),
                SimpleNamespace(),
                sdk._embed_service,
                fact_reconciler=SimpleNamespace(),
            )

            async def get_writer(**kwargs):
                assert kwargs["variant"] == variant
                return writer

            monkeypatch.setattr(sdk, "_get_coding_writer", get_writer)
            await _joined_thread(lambda: sdk._loop_thread.run(prepare()))
            messages = [message.to_dict() for message in edit_messages()]
            options = dict(
                user_id="scope",
                agent_id="agent",
                request_id=request_id,
                scene=variant,
                branch="synthetic",
            )
            try:
                result = await _joined_thread(sdk.add, messages, **options)
                assert result["success"] is True
                assert result["history_request_ids"] == ["previous"]
                assert [message.content for message in captures[0]] == [
                    "Previous synthetic request"
                ]
                assert bool(result["experience_ids"]) == with_output
                chain = result["chain_context"]
                assert chain["status"] == "complete"
                assert bool(chain["round_ids"]) == (variant == "coding")
                if variant == "coding":
                    assert all(
                        type(identity) is int and identity > 0 for identity in chain["round_ids"]
                    )
                    actual = await _joined_thread(lambda: sdk._loop_thread.run(chain_rows(sdk)))
                    assert {row[0] for row in actual} == set(chain["round_ids"])
                    assert all(row[1] == "scope|" for row in actual)
                replay = await _joined_thread(sdk.add, messages, **options)
                assert replay["history_request_ids"] == result["history_request_ids"]
                assert replay["chain_context"] == chain and len(captures) == 1
                saved = await bridge.get_sdk_write_outcome(request_id)
                assert saved["result"]["chain_context"] == chain
            finally:
                await _joined_thread(lambda: sdk._loop_thread.run(store.close()))
        async with fixture(config) as (app, _):
            saved = await _sdk_library_bridge(app.core).get_sdk_write_outcome(request_id)
            assert saved["result"]["history_request_ids"] == ["previous"]
            assert saved["result"]["chain_context"] == chain

    asyncio.run(scenario())


async def chain_rows(sdk):
    return sdk._chain_store.db.execute(
        "SELECT r.id,t.owner FROM round r JOIN thread t ON t.id=r.thread_id"
    ).fetchall()


def test_history_read_failure_does_not_report_unused_partial_ids(tmp_path, monkeypatch):
    async def scenario():
        async with fixture(config_for(tmp_path)) as (app, _):
            sdk = await _sdk_library_bridge(app.core)._get_client()
            store = CodingMemoryStore(
                sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path
            )

            async def failing(**kwargs):
                kwargs["source_request_ids"].append("not-used")
                raise OSError("synthetic history read failure")

            async def extract(messages, **kwargs):
                assert kwargs["history_messages"] == []
                return [], []

            async def run():
                await store.initialize()
                monkeypatch.setattr(store, "get_recent_trace_messages", failing)
                try:
                    writer = CodingWriter(
                        store,
                        SimpleNamespace(extract=extract),
                        SimpleNamespace(),
                        SimpleNamespace(),
                        sdk._embed_service,
                        fact_reconciler=SimpleNamespace(),
                    )
                    result = await writer.write([ChatMessage(content="Synthetic")], user_id="scope")
                    assert result["history_request_ids"] == []
                finally:
                    await store.close()

            await _joined_thread(lambda: sdk._loop_thread.run(run()))

    asyncio.run(scenario())


def test_chain_ids_are_actual_updated_rows_and_failed_write_rolls_back(tmp_path):
    chain = HyCodeMem(str(tmp_path / "chain"), owner="scope|")
    client = SimpleNamespace(
        _get_chain_store=lambda owner: chain, _chain_text=HyMemoryClient._chain_text
    )
    try:
        first = {}
        assert (
            HyMemoryClient._observe_chain(
                client, edit_messages(), user_id="scope", branch="synthetic", provenance=first
            )
            > 0
        )
        again = {}
        assert (
            HyMemoryClient._observe_chain(
                client,
                edit_messages(new="value=3\n"),
                user_id="scope",
                branch="synthetic",
                provenance=again,
            )
            > 0
        )
        assert again == first  # same said/thread updates the actual original row
        before = chain.db.execute("SELECT * FROM round").fetchall()
        chain.db.execute(
            "CREATE TRIGGER fail_round BEFORE INSERT ON round "
            "BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
        chain.db.commit()
        failed = {}
        assert (
            HyMemoryClient._observe_chain(
                client,
                edit_messages(said="Different request", path="other.py"),
                user_id="scope",
                branch="synthetic",
                provenance=failed,
            )
            == 0
        )
        assert failed == {"status": "unavailable", "round_ids": []}
        assert chain.db.execute("SELECT * FROM round").fetchall() == before
        assert chain.db.execute("SELECT COUNT(*) FROM thread").fetchone()[0] == 1
        assert not chain.db.in_transaction
        empty = {}
        assert (
            HyMemoryClient._observe_chain(
                client, [ChatMessage(content="No edit")], provenance=empty
            )
            == 0
        )
        assert empty == {"status": "complete", "round_ids": []}
    finally:
        chain.close()
