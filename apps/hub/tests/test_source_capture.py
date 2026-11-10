import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from cloud_memory_hub.capture_store import CaptureError, capture_lock
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.source_capture import (
    SourceConfiguration,
    collect_source,
    initialize_capture,
)


def native(config, text="A synthetic project decision.", **changes):
    return {
        "type": "user",
        "sessionId": "synthetic-session",
        "uuid": str(uuid4()),
        "cwd": config.workspace,
        "timestamp": "2026-10-24T00:00:00Z",
        "message": {"role": "user", "content": text},
        **changes,
    }


def append(path, *rows):
    with path.open("a") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def collect(outbox, config, **kwargs):
    return asyncio.run(collect_source(outbox, config, **kwargs))


def payloads(outbox):
    return [json.loads(item.payload) for item in outbox.pending(limit=256)]


@pytest.fixture
def capture(tmp_path):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    config = SourceConfiguration(
        adapter_id="claude-code",
        source_id=str(uuid4()),
        scope_id=str(uuid4()),
        source_root=str(root),
        workspace=str(tmp_path / "synthetic-project"),
    )
    outbox = LocalOutbox.initialize(
        tmp_path / "connector" / "outbox.sqlite3",
        instance_id=str(uuid4()),
        device_id=str(uuid4()),
    )
    initialize_capture(outbox, config)
    return outbox, config, root / "session.jsonl"


def test_real_parser_history_append_restart_and_rescan(capture):
    outbox, config, path = capture
    first = native(config)
    append(path, first, native(config, "Acknowledged.", type="assistant"))
    assert collect(outbox, config)["queued"] == 2
    original = payloads(outbox)
    assert original[0]["scope_id"] == config.scope_id
    assert original[0]["content"]["metadata"]["native_message_id"] == first["uuid"]
    assert original[0]["conversation_id"] == original[1]["conversation_id"]
    reopened = LocalOutbox(outbox.path, instance_id=outbox.instance_id, device_id=outbox.device_id)
    assert collect(reopened, config)["queued"] == 0
    append(path, native(config, "One additional turn."))
    assert collect(reopened, config)["queued"] == 1
    # Explicitly replay the files, as after cursor recovery; mappings remain.
    with sqlite3.connect(outbox.path) as db:
        db.execute("DELETE FROM capture_cursors")
    result = collect(reopened, config)
    assert result["queued"] == 0 and result["duplicates"] == 3
    assert payloads(outbox)[:2] == original


def test_identical_messages_without_native_ids_keep_distinct_positions(capture):
    outbox, config, path = capture
    row = native(config, "Yes.", uuid=None)
    append(path, row, row)
    assert collect(outbox, config)["queued"] == 2
    original = payloads(outbox)
    assert len({value["record_id"] for value in original}) == 2
    with sqlite3.connect(outbox.path) as db:
        db.execute("DELETE FROM capture_cursors")
    assert collect(outbox, config)["duplicates"] == 2
    assert payloads(outbox) == original


def test_incomplete_tail_is_captured_only_after_terminator(capture):
    outbox, config, path = capture
    text = json.dumps(native(config))
    path.write_text(text[:30])
    assert collect(outbox, config)["queued"] == 0
    with path.open("a") as stream:
        stream.write(text[30:])
    assert collect(outbox, config)["queued"] == 0
    with path.open("a") as stream:
        stream.write("\n")
    assert collect(outbox, config)["queued"] == 1


def test_project_scope_must_match_exact_native_workspace(capture):
    outbox, config, path = capture
    append(
        path,
        native(config, cwd="/synthetic-other-project"),
        native(config, cwd=None),
        native(config),
    )
    result = collect(outbox, config)
    assert result["queued"] == 1 and result["skipped"]["workspace_mismatch"] == 2
    for change in ({"scope_id": str(uuid4())}, {"workspace": "/synthetic-other-project"}):
        with pytest.raises(CaptureError, match="capture_binding_mismatch"):
            collect(outbox, config.model_copy(update=change))


def test_atomic_record_and_cursor_rollback_on_failure(capture, monkeypatch):
    outbox, config, path = capture
    append(path, native(config))
    original = outbox._insert_event

    def fail_after_insert(*args):
        original(*args)
        raise RuntimeError("synthetic_crash_before_cursor_commit")

    monkeypatch.setattr(outbox, "_insert_event", fail_after_insert)
    with pytest.raises(RuntimeError, match="synthetic_crash"):
        collect(outbox, config)
    assert not outbox.pending()
    with sqlite3.connect(outbox.path) as db:
        assert db.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM capture_cursors").fetchone()[0] == 0
    monkeypatch.setattr(outbox, "_insert_event", original)
    assert collect(outbox, config)["queued"] == 1


def test_native_tool_split_replay_after_interruption(capture, monkeypatch):
    outbox, config, path = capture
    append(
        path,
        native(
            config,
            type="assistant",
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will inspect the synthetic file."},
                    {
                        "type": "tool_use",
                        "id": "synthetic-tool",
                        "name": "Read",
                        "input": {"path": "/synthetic/file"},
                    },
                ],
            },
        ),
    )
    original = outbox._insert_event
    calls = 0

    def second_fails(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic_second_row_failure")
        return original(*args)

    monkeypatch.setattr(outbox, "_insert_event", second_fails)
    with pytest.raises(RuntimeError):
        collect(outbox, config)
    assert len(outbox.pending()) == 1
    monkeypatch.setattr(outbox, "_insert_event", original)
    result = collect(outbox, config)
    assert result["queued"] == 1 and result["duplicates"] == 1
    assert [p["content"]["messages"][0]["role"] for p in payloads(outbox)] == ["assistant", "tool"]


def test_process_death_rolls_back_event_and_progress_together(capture):
    outbox, config, path = capture
    append(path, native(config))
    program = """import asyncio, json, os, sys
from pathlib import Path
from cloud_memory_hub.local_outbox import LocalOutbox
from cloud_memory_hub.source_capture import SourceConfiguration, collect_source
value = json.load(sys.stdin)
outbox = LocalOutbox(Path(value['path']), instance_id=value['instance'], device_id=value['device'])
original = outbox._insert_event
def crash(*args):
    original(*args)
    os._exit(19)
outbox._insert_event = crash
asyncio.run(collect_source(outbox, SourceConfiguration.model_validate(value['source'])))
"""
    crashed = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(
            {
                "path": str(outbox.path),
                "instance": outbox.instance_id,
                "device": outbox.device_id,
                "source": config.model_dump(),
            }
        ),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert crashed.returncode == 19 and not crashed.stderr
    assert not outbox.pending()
    assert collect(outbox, config)["queued"] == 1
    assert len(outbox.pending()) == 1


def test_tail_rewrite_creates_revision_not_mutation(capture):
    outbox, config, path = capture
    row = native(config, "Choice A")
    append(path, row)
    assert collect(outbox, config)["queued"] == 1
    first = payloads(outbox)[0]
    stat = path.stat()
    row["message"]["content"] = "Choice B"
    path.write_text(json.dumps(row) + "\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert collect(outbox, config)["queued"] == 1
    messages = payloads(outbox)
    assert messages[0] == first
    assert messages[1]["record_id"] == first["record_id"]
    assert messages[1]["revision"] == 2
    assert messages[1]["event_id"] != first["event_id"]


def test_filtered_content_and_sealed_anchor_do_not_expose_secret(capture):
    outbox, config, path = capture
    secret = uuid4().hex
    outbox.privacy = CredentialFilter((secret,))
    append(path, native(config, "password=" + secret, uuid=secret))
    line_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert collect(outbox, config)["queued"] == 1
    for file in outbox.path.parent.iterdir():
        if file.is_file():
            assert secret.encode() not in file.read_bytes()
            assert line_digest.encode() not in file.read_bytes()
    assert "<redacted-secret>" in outbox.pending()[0].payload


def test_tool_credentials_and_capture_key_are_filtered(capture):
    outbox, config, path = capture
    key = (outbox.path.parent / "capture-key").read_text()
    secret = uuid4().hex
    append(
        path,
        native(
            config,
            type="assistant",
            message={
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "synthetic-tool",
                        "name": "Inspect",
                        "input": {"password": secret},
                    },
                ],
            },
        ),
        native(config, "Synthetic accidental key: " + key),
    )
    assert collect(outbox, config)["queued"] == 2
    assert secret.encode() not in outbox.path.read_bytes()
    assert key.encode() not in outbox.path.read_bytes()


def test_plaintext_privacy_rejection_does_not_commit_cursor(capture):
    from cloud_memory_hub.privacy import PrivacyError

    outbox, config, path = capture
    append(path, native(config, "unsafe\u0001content"))
    with pytest.raises(PrivacyError):
        collect(outbox, config)
    assert not outbox.pending()
    with sqlite3.connect(outbox.path) as db:
        assert db.execute("SELECT COUNT(*) FROM capture_cursors").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["malformed", "media", "unknown_media", "oversized"])
def test_failed_record_does_not_advance_and_can_be_repaired(capture, failure):
    outbox, config, path = capture
    if failure == "malformed":
        path.write_text('{"type":bad}\n')
    else:
        blocks = [{"type": "text", "text": "Safe text."}]
        if failure == "oversized":
            blocks[0]["text"] = "x" * (1024 * 1024 + 1)
        else:
            blocks.append(
                {
                    "type": "image" if failure == "media" else "document",
                    "source": {"data": "synthetic"},
                }
            )
        append(path, native(config, message={"role": "user", "content": blocks}))
    with pytest.raises(CaptureError):
        collect(outbox, config)
    assert not outbox.pending()
    path.write_text("")
    append(path, native(config, "Now readable."))
    assert collect(outbox, config)["queued"] == 1


def test_key_loss_and_cursor_tampering_fail_closed(capture):
    outbox, config, path = capture
    append(path, native(config))
    collect(outbox, config)
    key = outbox.path.parent / "capture-key"
    saved = key.read_bytes()
    key.unlink()
    with pytest.raises(CaptureError, match="capture_file_unavailable"):
        initialize_capture(outbox, config)
    assert not key.exists()
    key.write_bytes(saved)
    key.chmod(0o600)
    with sqlite3.connect(outbox.path) as db:
        db.execute("UPDATE capture_cursors SET sealed=?", (b"invalid",))
    with pytest.raises(CaptureError, match="capture_state_corrupted"):
        collect(outbox, config)
    assert outbox.status()["queued"] == 1


def test_source_root_replacement_and_symlink_refused(capture):
    outbox, config, path = capture
    root = path.parent
    moved = root.with_name("moved-source")
    root.rename(moved)
    root.mkdir()
    append(path, native(config))
    with pytest.raises(CaptureError, match="capture_binding_mismatch"):
        collect(outbox, config)
    path.unlink()
    root.rmdir()
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(CaptureError):
        collect(outbox, config)


@pytest.mark.parametrize("operation", ["initialize", "collect"])
def test_legacy_root_binding_stays_closed_without_changing_saved_capture(capture, operation):
    outbox, config, path = capture
    append(path, native(config))
    assert collect(outbox, config)["queued"] == 1
    root = path.parent.resolve()
    details = root.stat()
    legacy = (
        "root:v2:"
        + hashlib.sha256(
            f"{root}\0{details.st_ino}:{details.st_mode}:{details.st_uid}:{details.st_gid}".encode()
        ).hexdigest()
    )
    with sqlite3.connect(outbox.path) as db:
        db.execute(
            "UPDATE capture_sources SET root_identity=? WHERE source_id=?",
            (legacy, config.source_id),
        )
    with sqlite3.connect(outbox.path) as db:
        before = tuple(db.iterdump())
    key_before = (outbox.path.parent / "capture-key").read_bytes()
    append(path, native(config, "New content must wait for explicit source authorization."))
    reopened = LocalOutbox(outbox.path, instance_id=outbox.instance_id, device_id=outbox.device_id)

    with pytest.raises(CaptureError, match="capture_binding_mismatch"):
        if operation == "initialize":
            initialize_capture(reopened, config)
        else:
            collect(reopened, config)

    with sqlite3.connect(outbox.path) as db:
        assert tuple(db.iterdump()) == before
    assert (outbox.path.parent / "capture-key").read_bytes() == key_before
    assert reopened.status()["queued"] == 1


def test_source_lock_and_old_sender_remain_compatible(capture):
    outbox, config, path = capture
    with capture_lock(outbox), pytest.raises(CaptureError, match="capture_already_running"):
        collect(outbox, config)
    append(path, native(config))
    collect(outbox, config)
    pending = outbox.pending()[0]
    outbox.acknowledge(
        instance_id=outbox.instance_id,
        device_id=outbox.device_id,
        event_id=pending.event_id,
        payload_digest=pending.payload_digest,
        receipt_id=str(uuid4()),
    )
    assert outbox.status()["accepted"] == 1
    assert collect(outbox, config)["queued"] == 0


def test_cli_collect_is_explicit_local_and_private(capture, tmp_path):
    outbox, config, path = capture
    source_file = tmp_path / "source.json"
    source_file.write_text(config.model_dump_json())
    source_file.chmod(0o600)
    append(path, native(config))
    command = [
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
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["queued"] == 1
    source_file.chmod(0o644)
    failed = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert failed.returncode == 2
    assert json.loads(failed.stderr)["error"] == "unsafe_capture_file"
    assert str(tmp_path) not in failed.stderr
