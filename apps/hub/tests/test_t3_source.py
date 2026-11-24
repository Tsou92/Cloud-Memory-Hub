"""Isolated tests for unified T3 Code statev2 SQLite source capture."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from cloud_memory_hub.capture_store import CaptureError
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.source_capture import (
    SourceConfiguration,
    collect_source,
    initialize_capture,
)

_SCHEMA = """
CREATE TABLE projection_projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    scripts_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE orchestration_v2_projection_threads (
    thread_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    default_provider TEXT NOT NULL,
    runtime_mode TEXT NOT NULL DEFAULT 'full-access',
    interaction_mode TEXT NOT NULL DEFAULT 'default',
    active_provider_thread_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    deleted_at TEXT,
    payload_json TEXT NOT NULL,
    provider_instance_id TEXT
);
CREATE TABLE orchestration_v2_projection_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT,
    node_id TEXT,
    role TEXT NOT NULL,
    streaming INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    search_text TEXT
);
"""


def _init_state_db(path, *, wal=False):
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _add_project(con, project_id, workspace_root, *, title="Synthetic Project", deleted_at=None):
    con.execute(
        "INSERT INTO projection_projects VALUES (?, ?, ?, '[]', ?, ?, ?)",
        (
            project_id,
            title,
            workspace_root,
            "2026-11-24T00:00:00.000Z",
            "2026-11-24T00:00:00.000Z",
            deleted_at,
        ),
    )


def _add_thread(
    con,
    thread_id,
    project_id,
    *,
    provider="codex",
    provider_instance_id=None,
    worktree_path=None,
    parent_thread_id=None,
    deleted_at=None,
):
    payload = {
        "id": thread_id,
        "projectId": project_id,
        "providerInstanceId": provider_instance_id or provider,
        "worktreePath": worktree_path,
        "lineage": {
            "parentThreadId": parent_thread_id,
            "relationshipToParent": "subagent" if parent_thread_id else None,
            "rootThreadId": parent_thread_id or thread_id,
        },
    }
    con.execute(
        "INSERT INTO orchestration_v2_projection_threads "
        "(thread_id, project_id, title, default_provider, created_at, updated_at, "
        "deleted_at, payload_json, provider_instance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            thread_id,
            project_id,
            "Synthetic thread",
            provider,
            "2026-11-24T00:00:00.000Z",
            "2026-11-24T00:00:00.000Z",
            deleted_at,
            json.dumps(payload, ensure_ascii=False),
            provider_instance_id or provider,
        ),
    )


def _add_message(
    con,
    message_id,
    thread_id,
    *,
    role="user",
    text="Synthetic turn.",
    streaming=0,
    created_at="2026-11-24T01:00:00.000Z",
    updated_at=None,
    created_by=None,
    creation_source="web",
    attachments=None,
    context=None,
    notification=None,
):
    updated_at = updated_at or created_at
    payload = {
        "id": message_id,
        "threadId": thread_id,
        "role": role,
        "text": text,
        "streaming": bool(streaming),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "createdBy": created_by or ("user" if role == "user" else "agent"),
        "creationSource": creation_source if role == "user" else "provider",
        "attachments": attachments or [],
    }
    if context is not None:
        payload["context"] = context
    if notification is not None:
        payload["notification"] = notification
    con.execute(
        "INSERT INTO orchestration_v2_projection_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            thread_id,
            None,
            None,
            role,
            streaming,
            created_at,
            updated_at,
            json.dumps(payload, ensure_ascii=False),
            text,
        ),
    )


def _setup(tmp_path, *, workspace=None, media_text_extraction=False, wal=False):
    root = tmp_path / "t3-userdata"
    root.mkdir(mode=0o700, exist_ok=True)
    db_path = root / "statev2.sqlite"
    con = _init_state_db(db_path, wal=wal)
    workspace = workspace or str(tmp_path / "bound-project")
    config = SourceConfiguration(
        adapter_id="t3-code",
        source_id=str(uuid4()),
        scope_id=str(uuid4()),
        source_root=str(root),
        workspace=workspace,
        media_text_extraction=media_text_extraction,
    )
    outbox = LocalOutbox.initialize(
        tmp_path / "connector" / "outbox.sqlite3",
        instance_id=str(uuid4()),
        device_id=str(uuid4()),
    )
    initialize_capture(outbox, config)
    return outbox, config, root, db_path, con


def _payloads(outbox):
    return [json.loads(item.payload) for item in outbox.pending(limit=256)]


def test_t3_history_import_collects_all_providers_and_worktrees(tmp_path):
    outbox, config, _, _, con = _setup(tmp_path)
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_project(con, "proj-other", str(tmp_path / "other-project"))
        _add_project(con, "proj-deleted", config.workspace, deleted_at="2026-11-24T00:05:00Z")

        _add_thread(con, "th-codex", "proj-1", provider="codex")
        _add_thread(
            con,
            "th-claude-wt",
            "proj-1",
            provider="claudeAgent",
            worktree_path=str(tmp_path / "secret-worktree-path"),
        )
        _add_thread(con, "th-agy", "proj-1", provider="antigravity")
        _add_thread(con, "th-del", "proj-1", provider="codex", deleted_at="2026-11-24T00:05:00Z")
        _add_thread(con, "th-other", "proj-other", provider="codex")
        _add_thread(con, "th-proj-del", "proj-deleted", provider="codex")

        _add_message(
            con,
            "m1",
            "th-codex",
            role="user",
            text="Codex prompt.",
            created_at="2026-11-24T01:00:01.000Z",
        )
        _add_message(
            con,
            "m2",
            "th-codex",
            role="assistant",
            text="Codex reply.",
            created_at="2026-11-24T01:00:02.000Z",
        )
        _add_message(
            con,
            "m3",
            "th-claude-wt",
            role="user",
            text="Worktree Claude prompt.",
            created_at="2026-11-24T01:00:03.000Z",
        )
        _add_message(
            con,
            "m4",
            "th-claude-wt",
            role="assistant",
            text="Worktree Claude reply.",
            created_at="2026-11-24T01:00:04.000Z",
        )
        _add_message(
            con,
            "m5",
            "th-agy",
            role="user",
            text="Antigravity prompt.",
            created_at="2026-11-24T01:00:05.000Z",
        )
        _add_message(
            con,
            "m6",
            "th-agy",
            role="assistant",
            text="Antigravity reply.",
            created_at="2026-11-24T01:00:06.000Z",
        )
        # Unrelated project has an image attachment: must skip without failing bound project.
        _add_message(
            con,
            "m-other-img",
            "th-other",
            role="user",
            text="Screenshot in other project.",
            attachments=[{"id": "img-other", "type": "image", "mimeType": "image/png"}],
            created_at="2026-11-24T01:00:07.000Z",
        )
        _add_message(
            con,
            "m-del",
            "th-del",
            role="user",
            text="Deleted thread.",
            created_at="2026-11-24T01:00:08.000Z",
        )
        _add_message(
            con,
            "m-pdel",
            "th-proj-del",
            role="user",
            text="Deleted project.",
            created_at="2026-11-24T01:00:09.000Z",
        )
        con.commit()
    finally:
        con.close()

    result = asyncio.run(collect_source(outbox, config))
    assert result["queued"] == 6
    assert result["duplicates"] == 0
    assert result["skipped"]["workspace_mismatch"] == 3

    rows = _payloads(outbox)
    texts = [row["content"]["messages"][0]["text"] for row in rows]
    assert texts == [
        "Codex prompt.",
        "Codex reply.",
        "Worktree Claude prompt.",
        "Worktree Claude reply.",
        "Antigravity prompt.",
        "Antigravity reply.",
    ]
    assert {row["scope_id"] for row in rows} == {config.scope_id}
    providers = [
        row["content"]["metadata"]["origin"]["file"]["raw_meta"]["provider"] for row in rows
    ]
    assert providers == [
        "codex",
        "codex",
        "claudeAgent",
        "claudeAgent",
        "antigravity",
        "antigravity",
    ]
    assert rows[2]["content"]["metadata"]["origin"]["file"]["raw_meta"]["worktree"] is True
    assert "secret-worktree-path" not in json.dumps(rows)

    # Idle re-run must not emit duplicates.
    again = asyncio.run(collect_source(outbox, config))
    assert again["queued"] == 0 and again["duplicates"] == 0


def test_t3_wal_append_out_of_order_streaming_and_edits(tmp_path):
    outbox, config, _, db_path, con = _setup(tmp_path, wal=True)
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_thread(con, "th-a", "proj-1", provider="codex")
        _add_thread(con, "th-b", "proj-1", provider="claudeAgent")
        # Thread A starts streaming BEFORE Thread B sends a finalized message.
        _add_message(
            con,
            "m-a1",
            "th-a",
            role="assistant",
            text="",
            streaming=1,
            created_at="2026-11-24T01:00:01.000Z",
            updated_at="2026-11-24T01:00:01.000Z",
        )
        _add_message(
            con,
            "m-b1",
            "th-b",
            role="user",
            text="Thread B user.",
            streaming=0,
            created_at="2026-11-24T01:00:02.000Z",
            updated_at="2026-11-24T01:00:02.000Z",
        )
        con.commit()

        db_before = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
        first = asyncio.run(collect_source(outbox, config))
        assert first["queued"] == 1
        assert [p["content"]["messages"][0]["text"] for p in _payloads(outbox)] == [
            "Thread B user."
        ]

        # Finish streaming A1 ONLY inside the WAL file (main DB unchanged).
        payload_a1 = {
            "id": "m-a1",
            "threadId": "th-a",
            "role": "assistant",
            "text": "Thread A finished later.",
            "streaming": False,
            "createdAt": "2026-11-24T01:00:01.000Z",
            "updatedAt": "2026-11-24T01:00:05.000Z",
            "createdBy": "agent",
            "creationSource": "provider",
            "attachments": [],
        }
        con.execute(
            "UPDATE orchestration_v2_projection_messages "
            "SET streaming=0, updated_at=?, payload_json=?, search_text=? WHERE message_id=?",
            (
                "2026-11-24T01:00:05.000Z",
                json.dumps(payload_a1),
                "Thread A finished later.",
                "m-a1",
            ),
        )
        con.commit()
        assert (db_path.stat().st_size, db_path.stat().st_mtime_ns) == db_before

        second = asyncio.run(collect_source(outbox, config))
        assert second["queued"] == 1
        assert [p["content"]["messages"][0]["text"] for p in _payloads(outbox)] == [
            "Thread B user.",
            "Thread A finished later.",
        ]

        # Revise B1 in-place -> emits revision 2 on existing record_id.
        payload_b1_edit = {
            "id": "m-b1",
            "threadId": "th-b",
            "role": "user",
            "text": "Thread B edited.",
            "streaming": False,
            "createdAt": "2026-11-24T01:00:02.000Z",
            "updatedAt": "2026-11-24T01:00:06.000Z",
            "createdBy": "user",
            "creationSource": "web",
            "attachments": [],
        }
        con.execute(
            "UPDATE orchestration_v2_projection_messages "
            "SET updated_at=?, payload_json=?, search_text=? WHERE message_id=?",
            (
                "2026-11-24T01:00:06.000Z",
                json.dumps(payload_b1_edit),
                "Thread B edited.",
                "m-b1",
            ),
        )
        con.commit()

        third = asyncio.run(collect_source(outbox, config))
        assert third["queued"] == 1
        rows = _payloads(outbox)
        assert len(rows) == 3
        assert rows[2]["record_id"] == rows[0]["record_id"]
        assert rows[2]["revision"] == 2
        assert rows[2]["content"]["messages"][0]["text"] == "Thread B edited."
    finally:
        con.close()


def test_t3_skips_notifications_injected_blocks_and_subagent_toggle(tmp_path, monkeypatch):
    outbox, config, _, _, con = _setup(tmp_path)
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_thread(con, "th-main", "proj-1", provider="antigravity")
        _add_thread(con, "th-child", "proj-1", provider="claudeAgent", parent_thread_id="th-main")

        _add_message(
            con,
            "m-notif",
            "th-main",
            role="user",
            text="Task notification wakeup.",
            created_by="agent",
            creation_source="provider",
            notification={"type": "task"},
            created_at="2026-11-24T01:00:01Z",
        )
        _add_message(
            con,
            "m-injected",
            "th-main",
            role="user",
            text=(
                "<system-reminder>host</system-reminder>\n"
                "<memhub_memory_context>recalled</memhub_memory_context>\n"
                "Actual user question."
            ),
            created_at="2026-11-24T01:00:02.000Z",
        )
        _add_message(
            con,
            "m-child",
            "th-child",
            role="user",
            text="Delegated child prompt.",
            created_at="2026-11-24T01:00:03.000Z",
        )
        con.commit()
    finally:
        con.close()

    monkeypatch.setenv("ONE_MEMORY_IMPORT_SUBAGENT_TRACE", "0")
    res_off = asyncio.run(collect_source(outbox, config))
    assert res_off["queued"] == 1
    assert res_off["skipped"]["not_written_by_person"] == 1
    assert res_off["skipped"]["subagent_trace_disabled"] == 1
    assert _payloads(outbox)[0]["content"]["messages"][0]["text"] == "Actual user question."

    # Turning subagent tracing back on + replaying without cursors captures child turn as AGENT.
    monkeypatch.setenv("ONE_MEMORY_IMPORT_SUBAGENT_TRACE", "1")
    with sqlite3.connect(outbox.path) as db:
        db.execute("DELETE FROM capture_cursors")
    res_on = asyncio.run(collect_source(outbox, config))
    assert res_on["queued"] == 1
    assert res_on["duplicates"] == 1


def test_t3_media_fail_closed_and_opt_in_attachment_extraction(tmp_path):
    outbox, config, root, _, con = _setup(tmp_path, media_text_extraction=False)
    att_dir = root / "attachments"
    att_dir.mkdir(mode=0o700)
    (att_dir / "att-txt-1.txt").write_text("Derived T3 attachment note.", encoding="utf-8")
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_thread(con, "th-1", "proj-1", provider="codex")
        _add_message(
            con,
            "m-doc",
            "th-1",
            role="user",
            text="Please inspect attached notes.",
            attachments=[
                {"id": "att-txt-1", "type": "file", "name": "notes.txt", "mimeType": "text/plain"}
            ],
            created_at="2026-11-24T01:00:01.000Z",
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(CaptureError, match="source_media_not_supported"):
        asyncio.run(collect_source(outbox, config))
    assert not outbox.pending()

    enabled = config.model_copy(update={"media_text_extraction": True})
    res = asyncio.run(collect_source(outbox, enabled))
    assert res["queued"] == 1
    text = _payloads(outbox)[0]["content"]["messages"][0]["text"]
    assert "Please inspect attached notes." in text
    assert "Derived T3 attachment note." in text
    assert "notes.txt" in text

    # Adding an image attachment in context.records still fails closed even with extraction on.
    con = sqlite3.connect(root / "statev2.sqlite")
    try:
        _add_message(
            con,
            "m-img",
            "th-1",
            role="user",
            text="See screenshot.",
            context={
                "records": [{"kind": "image", "attachmentId": "att-img-1", "mimeType": "image/png"}]
            },
            created_at="2026-11-24T01:00:02.000Z",
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(CaptureError, match="source_media_not_supported"):
        asyncio.run(collect_source(outbox, enabled))


def test_t3_schema_validation_secret_redaction_and_large_db(tmp_path):
    outbox, config, _, db_path, con = _setup(tmp_path)
    secret = uuid4().hex
    outbox.privacy = CredentialFilter((secret,))
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_thread(con, "th-1", "proj-1", provider="claudeAgent")
        _add_message(
            con,
            "m-secret",
            "th-1",
            role="user",
            text="token=" + secret,
            created_at="2026-11-24T01:00:01.000Z",
        )
        # Pad DB past 8 MiB to verify SQLite capture does not reuse JSONL's 8 MiB cap.
        con.execute("CREATE TABLE padding (blob_data BLOB)")
        chunk = b"x" * (1024 * 1024)
        for _ in range(9):
            con.execute("INSERT INTO padding VALUES (?)", (chunk,))
        con.commit()
    finally:
        con.close()

    assert db_path.stat().st_size > 8 * 1024 * 1024
    res = asyncio.run(collect_source(outbox, config))
    assert res["queued"] == 1
    assert "<redacted-secret>" in outbox.pending()[0].payload
    assert secret.encode() not in outbox.path.read_bytes()

    # Corrupt schema fails closed as unsupported_schema.
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE projection_projects")
        con.commit()
    finally:
        con.close()
    con = sqlite3.connect(outbox.path)
    try:
        con.execute("DELETE FROM capture_cursors")
        con.commit()
    finally:
        con.close()
    with pytest.raises(CaptureError, match="source_scan_unsupported_schema"):
        asyncio.run(collect_source(outbox, config))


def test_t3_cli_capture_init_and_collect(tmp_path):
    outbox, config, _, _, con = _setup(tmp_path)
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_thread(con, "th-1", "proj-1", provider="codex")
        _add_message(con, "m-cli", "th-1", role="user", text="CLI T3 capture.")
        con.commit()
    finally:
        con.close()

    source_file = tmp_path / "t3-source.json"
    source_file.write_text(config.model_dump_json(), encoding="utf-8")
    source_file.chmod(0o600)
    cmd = [
        sys.executable,
        "-m",
        "cloud_memory_hub.connector",
        "collect",
        "--state-dir",
        str(outbox.path.parent),
        "--instance-id",
        outbox.instance_id,
        "--device-id",
        outbox.device_id,
        "--source-file",
        str(source_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(res.stdout)["queued"] == 1
    assert (
        json.loads(
            subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True).stdout
        )["queued"]
        == 0
    )


def test_t3_symlink_attachment_is_refused(tmp_path):
    outbox, config, root, _, con = _setup(tmp_path, media_text_extraction=True)
    att_dir = root / "attachments"
    att_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret note", encoding="utf-8")
    (att_dir / "att-sym.txt").symlink_to(outside)
    try:
        _add_project(con, "proj-1", config.workspace)
        _add_thread(con, "th-1", "proj-1", provider="codex")
        _add_message(
            con,
            "m-sym",
            "th-1",
            role="user",
            text="Check symlinked attachment.",
            attachments=[
                {"id": "att-sym", "type": "file", "name": "sym.txt", "mimeType": "text/plain"}
            ],
            created_at="2026-11-24T01:00:01.000Z",
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(CaptureError, match="source_access_denied"):
        asyncio.run(collect_source(outbox, config))
    assert not outbox.pending()
