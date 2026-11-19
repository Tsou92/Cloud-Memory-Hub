import asyncio
import base64
import json
from uuid import uuid4

import pytest
import zstandard
from cloud_memory_hub.capture_store import CaptureError
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.source_capture import (
    SourceConfiguration,
    collect_source,
    initialize_capture,
)


def _line(value):
    return json.dumps(value, ensure_ascii=False)


def session_file(root, workspace, records):
    directory = root / "session-synthetic"
    directory.mkdir(mode=0o700)
    path = directory / "session.v3.jsonl.zstd"
    header = {
        "type": "session",
        "version": 3,
        "id": "session-synthetic",
        "createdAt": 1_789_000_000_000,
        "cwd": workspace,
    }
    raw = ("\n".join(_line(item) for item in [header, *records]) + "\n").encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(raw))
    path.chmod(0o600)
    return path


def _records(workspace, *, attachment=None):
    user_content = [{"type": "text", "text": "Please inspect the project."}]
    if attachment is not None:
        user_content.append(attachment)
    return [
        {
            "type": "turn/start",
            "seq": 0,
            "time": 1_789_000_000_050,
            "data": {"turn": 1},
        },
        {
            "type": "user/message",
            "seq": 1,
            "time": 1_789_000_000_100,
            "data": {
                "content": user_content,
                "source": {"kind": "user"},
                "role": "user",
                "id": "user-1",
            },
        },
        {
            "type": "assistant/message",
            "seq": 2,
            "time": 1_789_000_000_200,
            "data": {
                "message": {
                    "role": "assistant",
                    "id": "assistant-1",
                    "content": [
                        {"type": "reasoning", "text": "internal reasoning must not be imported"},
                        {"type": "text", "text": "I will list the files."},
                        {
                            "type": "tool-call",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": '{"command":"ls"}',
                        },
                    ],
                }
            },
        },
        {
            "type": "tool/call",
            "seq": 3,
            "time": 1_789_000_000_250,
            "data": {
                "callId": "call-1",
                "name": "bash",
                "arguments": '{"command":"ls"}',
            },
        },
        {
            "type": "tool/result",
            "seq": 4,
            "time": 1_789_000_000_300,
            "data": {
                "message": {
                    "source": {"kind": "tool", "callId": "call-1"},
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "call-1",
                            "content": [{"type": "text", "text": "synthetic\x01\x02 tool output"}],
                        }
                    ],
                }
            },
        },
        {
            "type": "user/message",
            "seq": 5,
            "time": 1_789_000_000_400,
            "data": {
                "content": [{"type": "text", "text": "approval policy changed"}],
                "source": {"kind": "plugin", "plugin": "user-approval"},
                "role": "user",
                "id": "plugin-1",
            },
        },
        {
            "type": "system/message",
            "seq": 6,
            "time": 1_789_000_000_500,
            "data": {"content": [{"type": "text", "text": "system notice"}]},
        },
    ]


def _capture(tmp_path, *, workspace=None, header_workspace=None, media_text_extraction=False):
    root = tmp_path / "dsh-sessions"
    root.mkdir(mode=0o700)
    workspace = workspace or str(tmp_path / "project")
    session_file(root, header_workspace or workspace, _records(workspace))
    config = SourceConfiguration(
        adapter_id="deepseek-harness",
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
    return outbox, config


def _payloads(outbox):
    return [json.loads(item.payload) for item in outbox.pending(limit=64)]


def test_dsh_history_import_maps_messages_and_tools(tmp_path):
    outbox, config = _capture(tmp_path)
    result = asyncio.run(collect_source(outbox, config))
    assert result["queued"] == 4
    assert result["duplicates"] == 0
    assert result["skipped"]["not_written_by_person"] == 1
    assert result["skipped"]["internal_reasoning_excluded"] == 1
    # The session header, turn/start and the duplicate tool/call are not rows.
    assert result["skipped"]["unknown_record"] == 3
    rows = _payloads(outbox)
    texts = [row["content"]["messages"][0]["text"] for row in rows]
    assert "Please inspect the project." in texts
    assert "I will list the files." in texts
    assert any("synthetic tool output" in text for text in texts)
    assert all("internal reasoning" not in text for text in texts)
    assert rows[0]["content"]["metadata"]["adapter_id"] == "deepseek-harness"
    assert asyncio.run(collect_source(outbox, config))["queued"] == 0


def test_dsh_workspace_mismatch_is_skipped(tmp_path):
    outbox, config = _capture(
        tmp_path,
        workspace=str(tmp_path / "different-project"),
        header_workspace=str(tmp_path / "project"),
    )
    result = asyncio.run(collect_source(outbox, config))
    assert result["queued"] == 0
    assert result["skipped"]["workspace_mismatch"] == 4


def test_dsh_media_is_rejected_by_default(tmp_path):
    root = tmp_path / "dsh-sessions"
    root.mkdir(mode=0o700)
    workspace = str(tmp_path / "project")
    attachment = {
        "type": "document",
        "name": "notes.txt",
        "source": {
            "type": "base64",
            "media_type": "text/plain",
            "data": base64.b64encode(b"hello attachment").decode(),
        },
    }
    session_file(root, workspace, _records(workspace, attachment=attachment))
    config = SourceConfiguration(
        adapter_id="deepseek-harness",
        source_id=str(uuid4()),
        scope_id=str(uuid4()),
        source_root=str(root),
        workspace=workspace,
    )
    outbox = LocalOutbox.initialize(
        tmp_path / "connector" / "outbox.sqlite3",
        instance_id=str(uuid4()),
        device_id=str(uuid4()),
    )
    initialize_capture(outbox, config)
    with pytest.raises(CaptureError) as error:
        asyncio.run(collect_source(outbox, config))
    assert str(error.value) == "source_media_not_supported"


def test_dsh_media_text_extraction_is_opt_in(tmp_path):
    root = tmp_path / "dsh-sessions"
    root.mkdir(mode=0o700)
    workspace = str(tmp_path / "project")
    attachment = {
        "type": "document",
        "name": "notes.txt",
        "source": {
            "type": "base64",
            "media_type": "text/plain",
            "data": base64.b64encode(b"hello attachment").decode(),
        },
    }
    session_file(root, workspace, _records(workspace, attachment=attachment))
    config = SourceConfiguration(
        adapter_id="deepseek-harness",
        source_id=str(uuid4()),
        scope_id=str(uuid4()),
        source_root=str(root),
        workspace=workspace,
        media_text_extraction=True,
    )
    outbox = LocalOutbox.initialize(
        tmp_path / "connector" / "outbox.sqlite3",
        instance_id=str(uuid4()),
        device_id=str(uuid4()),
    )
    initialize_capture(outbox, config)
    assert asyncio.run(collect_source(outbox, config))["queued"] == 4
    texts = [row["content"]["messages"][0]["text"] for row in _payloads(outbox)]
    assert any("hello attachment" in text for text in texts)
    assert all("aGVsbG8gYXR0YWNobWVudA==" not in text for text in texts)
