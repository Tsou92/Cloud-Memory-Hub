"""FR-06/10/11: explicit remote refusal is durable without blocking valid events."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from cloud_memory_hub.local_outbox import LocalOutbox, OutboxError
from cloud_memory_hub.transport import RemoteConnection, TransportError, send_pending
from test_agent_hook import service_for
from test_transport import BytesBody, connection_data, json_response, mock_server, queue_for

REASON = "governance_content_suppressed"


def rejection_server(connection, first, *, response=None, fail_at="/hub/v1/events"):
    fallback = mock_server(connection, first)
    uploaded = []

    def handler(request):
        if request.url.path == "/hub/v1/events":
            uploaded.append(json.loads(request.content)["event_id"])
        if request.url.path == fail_at and (
            fail_at != "/hub/v1/events" or uploaded[-1] == first["event_id"]
        ):
            return response() if response else json_response({"detail": REASON}, 409)
        return fallback.handler(request)

    return httpx.MockTransport(handler), uploaded


def two_events(tmp_path):
    connection = RemoteConnection.model_validate(connection_data())
    outbox, first = queue_for(tmp_path, connection)
    second = {**first, "event_id": str(uuid4()), "record_id": str(uuid4())}
    second["content"] = {"text": "A different synthetic decision may continue uploading."}
    outbox.enqueue(second)
    return connection, outbox, first, second


def test_explicit_refusal_is_persistent_and_does_not_block_next_event(tmp_path):
    connection, outbox, first, second = two_events(tmp_path)
    transport, uploaded = rejection_server(connection, first)
    assert asyncio.run(send_pending(connection, outbox, transport=transport)) == 1
    assert uploaded == [first["event_id"], second["event_id"]]
    assert outbox.status() == {"queued": 0, "accepted": 1, "rejected": {REASON: 1}}
    reopened = LocalOutbox(
        outbox.path, instance_id=connection.instance_id, device_id=connection.device_id
    )
    expected = {
        "event_id": first["event_id"],
        "status": "rejected",
        "receipt_id": None,
        "error": REASON,
        "retryable": False,
    }
    assert reopened.enqueue(first) == expected
    assert reopened.enqueue(first) == expected
    assert reopened.pending() == ()
    assert asyncio.run(send_pending(connection, reopened, transport=transport)) == 0
    assert uploaded == [first["event_id"], second["event_id"]]
    assert reopened.status()["rejected"] == {REASON: 1}
    with sqlite3.connect(outbox.path) as db:
        payload = db.execute(
            "SELECT payload FROM events WHERE event_id=?", (first["event_id"],)
        ).fetchone()[0]
    assert json.loads(payload)["content"] == first["content"]


@pytest.mark.parametrize(
    "body,status,headers",
    [
        ({"detail": "event_identity_conflict"}, 409, {}),
        ({"detail": REASON, "extra": "untrusted"}, 409, {}),
        ({"detail": REASON}, 403, {}),
        ({"detail": REASON}, 409, {"content-type": "text/html"}),
        ({"detail": REASON}, 409, {"content-encoding": "gzip"}),
        ("not-json", 409, {}),
        ("x" * 4097, 409, {}),
    ],
)
def test_other_errors_retain_queue_and_stop(tmp_path, body, status, headers):
    connection, outbox, first, _ = two_events(tmp_path)

    def response():
        encoded = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        return httpx.Response(
            status,
            headers={"content-type": "application/json", **headers},
            stream=BytesBody(encoded),
        )

    transport, uploaded = rejection_server(connection, first, response=response)
    with pytest.raises(TransportError):
        asyncio.run(send_pending(connection, outbox, transport=transport))
    assert uploaded == [first["event_id"]]
    assert outbox.status() == {"queued": 2, "accepted": 0, "rejected": {}}


def test_same_reason_outside_upload_is_not_a_terminal_event(tmp_path):
    connection, outbox, first, _ = two_events(tmp_path)
    transport, uploaded = rejection_server(connection, first, fail_at="/hub/v1/session")
    with pytest.raises(TransportError, match="remote_identity_conflict"):
        asyncio.run(send_pending(connection, outbox, transport=transport))
    assert uploaded == [] and outbox.status()["queued"] == 2


def downgrade_empty_rejection_schema(path):
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE IF EXISTS delivery_rejections")
        db.execute("PRAGMA user_version=1")


def test_explicit_upgrade_backs_up_old_state_and_preserves_capture_tables(tmp_path):
    connection, outbox, first, second = two_events(tmp_path)
    pending = outbox.pending()
    receipt = str(uuid4())
    outbox.acknowledge(
        instance_id=connection.instance_id,
        device_id=connection.device_id,
        event_id=second["event_id"],
        payload_digest=pending[1].payload_digest,
        receipt_id=receipt,
    )
    with sqlite3.connect(outbox.path) as db:
        db.execute("CREATE TABLE capture_cursors(source_id TEXT, sealed BLOB)")
        db.execute(
            "INSERT INTO capture_cursors VALUES (?,?)",
            (first["source_id"], b"synthetic-sealed-cursor"),
        )
    downgrade_empty_rejection_schema(outbox.path)
    before = outbox.path.read_bytes()
    with pytest.raises(OutboxError, match="outbox_upgrade_required"):
        LocalOutbox(outbox.path, instance_id=connection.instance_id, device_id=connection.device_id)
    assert outbox.path.read_bytes() == before
    backup = outbox.path.parent / "before.sqlite3"
    result = LocalOutbox.upgrade(
        outbox.path,
        instance_id=connection.instance_id,
        device_id=connection.device_id,
        backup_file=backup,
    )
    assert result == {"status": "upgraded", "schema_version": 2}
    with sqlite3.connect(backup) as old, sqlite3.connect(outbox.path) as new:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert new.execute("PRAGMA user_version").fetchone()[0] == 2
        for table in ("binding", "events", "rejected", "capture_cursors"):
            assert (
                old.execute(f"SELECT * FROM {table}").fetchall()
                == new.execute(f"SELECT * FROM {table}").fetchall()
            )
    assert backup.stat().st_mode & 0o777 == 0o600
    assert LocalOutbox.upgrade(
        outbox.path,
        instance_id=connection.instance_id,
        device_id=connection.device_id,
        backup_file=backup,
    ) == {"status": "already_current", "schema_version": 2}


def test_rejection_write_failure_never_skips_the_event(tmp_path, monkeypatch):
    connection, outbox, first, _ = two_events(tmp_path)
    transport, uploaded = rejection_server(connection, first)

    def fail(**kwargs):
        raise OutboxError("outbox_unavailable")

    monkeypatch.setattr(outbox, "reject_delivery", fail, raising=False)
    with pytest.raises((TransportError, OutboxError)):
        asyncio.run(send_pending(connection, outbox, transport=transport))
    assert uploaded == [first["event_id"]]
    assert outbox.status()["queued"] == 2


def rejection_arguments(connection, pending):
    return {
        "instance_id": connection.instance_id,
        "device_id": connection.device_id,
        "event_id": pending.event_id,
        "payload_digest": pending.payload_digest,
        "code": REASON,
    }


def test_concurrent_rejection_is_idempotent_and_cannot_be_acknowledged(tmp_path):
    connection, outbox, first, _ = two_events(tmp_path)
    pending = outbox.pending()[0]
    arguments = rejection_arguments(connection, pending)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _: outbox.reject_delivery(**arguments), range(8)))
    assert outbox.status() == {"queued": 1, "accepted": 0, "rejected": {REASON: 1}}
    with pytest.raises(OutboxError, match="receipt_conflict"):
        outbox.acknowledge(
            **{k: v for k, v in arguments.items() if k != "code"}, receipt_id=str(uuid4())
        )
    with pytest.raises(OutboxError, match="event_identity_conflict"):
        outbox.enqueue({**first, "content": {"text": "Changed synthetic input."}})


@pytest.mark.parametrize(
    "change",
    [
        {"instance_id": str(uuid4())},
        {"device_id": str(uuid4())},
        {"event_id": str(uuid4())},
        {"event_id": "invalid"},
        {"payload_digest": "wrong"},
        {"code": "unknown-reason"},
    ],
)
def test_wrong_rejection_binding_or_evidence_never_changes_queue(tmp_path, change):
    connection, outbox, _, _ = two_events(tmp_path)
    arguments = {**rejection_arguments(connection, outbox.pending()[0]), **change}
    with pytest.raises(OutboxError):
        outbox.reject_delivery(**arguments)
    assert outbox.status() == {"queued": 2, "accepted": 0, "rejected": {}}


def test_accepted_event_cannot_become_rejected(tmp_path):
    connection, outbox, _, _ = two_events(tmp_path)
    pending = outbox.pending()[0]
    arguments = rejection_arguments(connection, pending)
    outbox.acknowledge(
        **{k: v for k, v in arguments.items() if k != "code"}, receipt_id=str(uuid4())
    )
    with pytest.raises(OutboxError, match="rejection_conflict"):
        outbox.reject_delivery(**arguments)
    assert outbox.status() == {"queued": 1, "accepted": 1, "rejected": {}}


def test_upgrade_failure_after_consistent_backup_rolls_back_source(tmp_path, monkeypatch):
    import cloud_memory_hub.local_outbox as module

    connection, outbox, _, _ = two_events(tmp_path)
    downgrade_empty_rejection_schema(outbox.path)
    before = outbox.path.read_bytes()
    monkeypatch.setattr(module, "_REJECTIONS_SCHEMA", "not valid SQL")
    backup = outbox.path.parent / "before.sqlite3"
    with pytest.raises(OutboxError, match="outbox_upgrade_failed"):
        LocalOutbox.upgrade(
            outbox.path,
            instance_id=connection.instance_id,
            device_id=connection.device_id,
            backup_file=backup,
        )
    assert outbox.path.read_bytes() == before
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE events",
        "DROP TABLE rejected",
        "ALTER TABLE events RENAME COLUMN payload TO removed_payload",
        "ALTER TABLE rejected RENAME COLUMN count TO removed_count",
    ],
)
def test_upgrade_refuses_missing_required_tables_or_columns(tmp_path, damage):
    connection, outbox, _, _ = two_events(tmp_path)
    downgrade_empty_rejection_schema(outbox.path)
    with sqlite3.connect(outbox.path) as db:
        db.execute(damage)
    before = outbox.path.read_bytes()
    backup = outbox.path.parent / "before.sqlite3"
    with pytest.raises(OutboxError, match="outbox_upgrade_failed"):
        LocalOutbox.upgrade(
            outbox.path,
            instance_id=connection.instance_id,
            device_id=connection.device_id,
            backup_file=backup,
        )
    assert outbox.path.read_bytes() == before
    with sqlite3.connect(outbox.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_rejections'").fetchone()
            is None
        )


@pytest.mark.parametrize("failure", ["existing_backup", "wrong_identity", "unsafe_backup_dir"])
def test_upgrade_refuses_unsafe_or_conflicting_inputs_without_modification(tmp_path, failure):
    connection, outbox, _, _ = two_events(tmp_path)
    downgrade_empty_rejection_schema(outbox.path)
    before = outbox.path.read_bytes()
    backup = outbox.path.parent / "before.sqlite3"
    device_id = connection.device_id
    if failure == "existing_backup":
        backup.write_bytes(b"do not replace existing file")
    elif failure == "wrong_identity":
        device_id = str(uuid4())
    else:
        backup = tmp_path / "unsafe" / "before.sqlite3"
        backup.parent.mkdir(mode=0o755)
        backup.parent.chmod(0o755)
    with pytest.raises(OutboxError):
        LocalOutbox.upgrade(
            outbox.path, instance_id=connection.instance_id, device_id=device_id, backup_file=backup
        )
    assert outbox.path.read_bytes() == before
    if failure == "existing_backup":
        assert backup.read_bytes() == b"do not replace existing file"
    else:
        assert not backup.exists()


def test_upgrade_snapshot_contains_committed_wal(tmp_path):
    connection, outbox, _, _ = two_events(tmp_path)
    downgrade_empty_rejection_schema(outbox.path)
    writer = sqlite3.connect(outbox.path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO rejected VALUES ('privacy_rejected',7)")
        writer.commit()
        assert outbox.path.with_name(outbox.path.name + "-wal").exists()
        backup = outbox.path.parent / "before.sqlite3"
        LocalOutbox.upgrade(
            outbox.path,
            instance_id=connection.instance_id,
            device_id=connection.device_id,
            backup_file=backup,
        )
        with sqlite3.connect(backup) as restored:
            assert restored.execute(
                "SELECT count FROM rejected WHERE code='privacy_rejected'"
            ).fetchone() == (7,)
            assert restored.execute("PRAGMA user_version").fetchone() == (1,)
    finally:
        writer.close()


@pytest.mark.parametrize("entrypoint", ["agent_cli", "connector"])
def test_explicit_cli_upgrade_is_local_and_reports_new_version(tmp_path, entrypoint):
    connection, outbox, first, _ = two_events(tmp_path)
    downgrade_empty_rejection_schema(outbox.path)
    connection_file = outbox.path.parent / "connection.json"
    data = connection.model_dump(mode="json")
    data["credential"] = connection.credential.get_secret_value()
    connection_file.write_text(json.dumps(data))
    connection_file.chmod(0o600)
    backup = outbox.path.parent / "before.sqlite3"
    command = [sys.executable, "-m", "cloud_memory_hub." + entrypoint]
    if entrypoint == "agent_cli":
        config = outbox.path.parent / "agent.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "connection_file": str(connection_file),
                    "state_dir": str(outbox.path.parent),
                    "scope_id": first["scope_id"],
                    "source_id": first["source_id"],
                }
            )
        )
        config.chmod(0o600)
        command += ["--config-file", str(config), "upgrade-outbox"]
    else:
        command += [
            "upgrade-outbox",
            "--connection-file",
            str(connection_file),
            "--state-dir",
            str(outbox.path.parent),
        ]
    command += ["--backup-file", str(backup)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "upgraded", "schema_version": 2}
    assert connection.credential.get_secret_value() not in result.stdout + result.stderr


def test_agent_remember_reports_terminal_reason_and_never_claims_acceptance(tmp_path):
    service = service_for(tmp_path)
    identity = str(uuid4())
    raw = {"event_id": identity, "source_id": service.source_id, "scope_id": service.scope_id}
    transport, uploaded = rejection_server(service.connection, raw)
    service.transport = transport
    text = "Synthetic already forgotten decision remains locally reviewable."
    result = asyncio.run(service.remember(text, request_id=identity))
    assert result == {
        "event_id": identity,
        "status": "rejected",
        "receipt_id": None,
        "error": REASON,
        "retryable": False,
        "processing_state": "not_checked",
    }
    assert asyncio.run(service.remember(text, request_id=identity)) == result
    assert uploaded == [identity]
    assert asyncio.run(service.status())["rejected"] == {REASON: 1}


def test_stop_hook_reports_rejected_capture_without_blocking_host(tmp_path):
    from cloud_memory_hub.agent_hook import SILENT, _identity, process_message

    service = service_for(tmp_path)
    session, prompt = "synthetic-session", "synthetic-prompt"
    identity = _identity(
        service.connection.instance_id,
        service.connection.device_id,
        service.scope_id,
        service.source_id,
        session,
        prompt,
        "assistant",
    )
    raw = {"event_id": identity, "source_id": service.source_id, "scope_id": service.scope_id}
    service.transport, uploaded = rejection_server(service.connection, raw)
    output, status = asyncio.run(
        process_message(
            service,
            ("Stop", "assistant", "A synthetic forgotten statement repeats.", session, prompt),
        )
    )
    assert output == SILENT
    assert status["capture"] == "rejected" and status["capture_error"] == REASON
    assert status["recall"] == "not_requested"
    assert uploaded == [identity]
