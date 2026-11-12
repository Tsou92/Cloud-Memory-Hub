import asyncio
import time
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import SESSION_MARKDOWN_CHARS
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    NamedResource,
)
from cloud_memory_hub.remote_ingest import RemoteIngest, SourceRegistration
from cloud_memory_hub.source_history import PREFIX, SourceHistory
from one_memory.memory_server.durable_queue import DurableWriteQueue


@pytest.fixture
def archive(tmp_path):
    access = DeviceAccessStore(tmp_path / "config" / "access.sqlite3")
    queue = DurableWriteQueue(tmp_path / "queue.sqlite3")
    ingest = RemoteIngest(access, lambda: queue)
    projects = [
        access.create_project(NamedResource(display_name=name))["project_id"]
        for name in ("Synthetic project", "Separate project")
    ]
    issued = access.create_device(NamedResource(display_name="Synthetic device"))
    device_id = issued.device["device_id"]
    access.replace_grants(
        device_id,
        GrantRequest(
            expected_revision=1,
            grants=[{"scope_id": scope, "permissions": ["events:write"]} for scope in projects],
        ),
    )
    token = issued.credential.get_secret_value()
    conversation = str(uuid4())

    def source(scope_id):
        source_id = ingest.create_source(
            SourceRegistration(
                display_name="Synthetic source",
                adapter_id="synthetic",
                device_id=device_id,
                scope_id=scope_id,
            )
        )["source_id"]

        def append(*messages, record_id=None, revision=1):
            event = EventEnvelope(
                event_id=str(uuid4()),
                instance_id=access.instance_id,
                device_id=device_id,
                source_id=source_id,
                scope_id=scope_id,
                conversation_id=conversation,
                record_id=record_id or str(uuid4()),
                revision=revision,
                kind="conversation_turn",
                content={"messages": [{"role": role, "text": text} for role, text in messages]},
            )
            result = ingest.accept(token, event)
            assert result["processing_state"] == "pending"
            return PREFIX + result["receipt_id"]

        return append

    return SourceHistory(ingest), projects, conversation, source


async def briefs(history, scope, *, query=None, exclude=None):
    return await history.session_briefs(
        scope, limit=3, query=query, exclude_conversation_id=exclude, privacy=CredentialFilter()
    )


async def markdown(history, scope, anchor, *, cursor=None, privacy=None):
    return await history.session_markdown(
        scope, session_ref=anchor, cursor=cursor, privacy=privacy or CredentialFilter()
    )


def test_briefs_rank_recent_sources_and_keep_project_boundaries(archive):
    history, (scope, foreign), conversation, source = archive
    alpha, beta, private = source(scope), source(scope), source(foreign)
    alpha(("user", "请完成合成队列验收。"))
    first = alpha(("assistant", "合成队列验收通过。"))
    second = beta(("assistant", "另一个来源的工作。"))
    outside = private(("assistant", "另一个项目的原文。"))

    async def scenario():
        selected = await briefs(history, scope, query="合成队列")
        assert [item["session_ref"] for item in selected] == [first, second]
        assert "请完成合成队列验收" in selected[0]["summary"]
        assert "合成队列验收通过" in selected[0]["summary"]
        assert "合成队列" not in selected[1]["summary"]
        assert await briefs(history, scope, exclude=conversation) == []
        page = await markdown(history, scope, second)
        assert "另一个来源的工作" in page["markdown"]
        assert "合成队列" not in page["markdown"]
        assert page["next_cursor"] is None
        with pytest.raises(AccessError, match="record_not_found"):
            await markdown(history, scope, outside)

    asyncio.run(scenario())


def test_markdown_preserves_long_filtered_session_at_summary_anchor(archive):
    history, (scope, _), _, source = archive
    append = source(scope)
    retired_value = "synthetic retired service phrase"
    before = "后续步骤。" * 9000
    after = "继续验证。" * 9000
    append(("user", before + retired_value + after))
    anchor = append(("assistant", "任务已交接。"), ("tool", "合成操作输出。"))
    privacy = CredentialFilter((retired_value,))

    async def scenario():
        page = await markdown(history, scope, anchor, privacy=privacy)
        assert page["next_cursor"] is not None
        append(("assistant", "摘要生成后才追加的内容。"))
        pages = []
        for _ in range(32):
            assert len(page["markdown"].encode()) <= SESSION_MARKDOWN_CHARS
            pages.append(page["markdown"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            page = await markdown(history, scope, anchor, cursor=cursor, privacy=privacy)
        else:
            pytest.fail("Markdown pagination did not reach the anchor")
        filtered = privacy.protect(before + retired_value + after).value
        assert "".join(pages) == (
            f"# 会话原文\n\n## 用户\n\n{filtered}\n\n"
            "## 助手\n\n任务已交接。\n\n## 工具\n\n合成操作输出。\n\n"
        )
        assert retired_value not in "".join(pages)

    asyncio.run(scenario())


def test_forgetting_latest_revision_never_restores_older_original(archive):
    history, (scope, _), _, source = archive
    append = source(scope)
    identity = str(uuid4())
    append(("user", "过时的合成原文。"), record_id=identity)
    newest = append(("user", "更新后的合成原文。"), record_id=identity, revision=2)
    anchor = append(("assistant", "仍可共享的进展。"))

    async def scenario():
        assert "更新后的" in (await briefs(history, scope))[0]["summary"]
        history.ingest.suppressed = lambda requested: {newest: "raw"} if requested == scope else {}
        assert await briefs(history, scope) == [
            {"session_ref": anchor, "summary": "最近进展:仍可共享的进展。"}
        ]
        page = await markdown(history, scope, anchor)
        assert page["markdown"] == "# 会话原文\n\n## 助手\n\n仍可共享的进展。\n\n"
        assert page["next_cursor"] is None
        with pytest.raises(AccessError, match="record_not_found"):
            await markdown(history, scope, newest)

    asyncio.run(scenario())


def test_markdown_continuation_rejects_changed_parent_and_rechecks_forget(archive):
    history, (scope, _), _, source = archive
    append = source(scope)
    identity = str(uuid4())
    append(("user", "合成的长原文。" * 4000), record_id=identity)
    anchor = append(("assistant", "接续进度。"))

    async def scenario():
        page = await markdown(history, scope, anchor)
        assert page["next_cursor"] and not page["next_cursor"].endswith(":0")
        append(("user", "修订后的原文。"), record_id=identity, revision=2)
        with pytest.raises(AccessError, match="session_changed"):
            await markdown(history, scope, anchor, cursor=page["next_cursor"])
        states = iter(({}, {anchor: "purged"}))
        history.ingest.suppressed = lambda _scope: next(states)
        with pytest.raises(AccessError, match="session_changed"):
            await markdown(history, scope, anchor)

    asyncio.run(scenario())


def test_purging_latest_revision_keeps_its_source_rule_on_older_original(archive):
    history, (scope, _), _, source = archive
    append = source(scope)
    identity = str(uuid4())
    append(("user", "过时的合成原文。"), record_id=identity)
    newest = append(("user", "已彻底删除的合成原文。"), record_id=identity, revision=2)
    anchor = append(("assistant", "可继续共享的进展。"))
    with history.ingest._database(writing=True) as db:
        db.execute(
            "DELETE FROM cmh_event_receipts WHERE receipt_id=?", (newest.removeprefix(PREFIX),)
        )
    history.ingest.suppressed = lambda _scope: {newest: "purged"}

    def source_allowed(event):
        if event.record_id == identity:
            raise AccessError("governance_source_suppressed", 409)

    history.ingest.require_source_allowed = source_allowed

    async def scenario():
        assert await briefs(history, scope) == [
            {"session_ref": anchor, "summary": "最近进展:可继续共享的进展。"}
        ]
        page = await markdown(history, scope, anchor)
        assert page["markdown"] == "# 会话原文\n\n## 助手\n\n可继续共享的进展。\n\n"

    asyncio.run(scenario())


def test_history_without_client_time_reports_hub_acceptance_time(archive):
    # The admin activity list and handoff overlay show a date for every original.
    history, (scope, _), _, source = archive
    before = int(time.time()) - 1
    anchor = source(scope)(("assistant", "没有客户端时间的合成原文。"))
    namespace = f"cmh:{history.ingest.access.instance_id}:{scope}"

    async def scenario():
        records, total = await history.page(namespace, 5, 0)
        assert total == 1 and records[0].raw_id == anchor
        assert before <= records[0].memory_at <= int(time.time()) + 1
        assert (await history.get(anchor)).memory_at == records[0].memory_at

    asyncio.run(scenario())
