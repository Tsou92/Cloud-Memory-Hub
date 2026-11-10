"""ADR-0062 / R2-04: bounded metadata, real receipts and a strictly read-only status command."""

import asyncio
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import test_transport
from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.client_processing import read_processing_sample
from cloud_memory_hub.client_setup import client_status, import_pairing
from cloud_memory_hub.local_outbox import TERMINAL_REJECTION_CODE, OutboxError
from cloud_memory_hub.transport import RemoteConnection, send_pending
from test_agent_access import read_grants
from test_client_setup import authorized, install, pairing
from test_read_transport import json_response
from test_transport import BytesBody, bind_live, connection_data, queue_for

live_hub = test_transport.live_hub


def response(value, status=200):
    return httpx.Response(
        status,
        stream=BytesBody(json.dumps(value).encode()),
        headers={"content-type": "application/json"},
    )


def receipt(connection, event, **changes):
    return {
        "protocol_version": 1,
        "instance_id": connection.instance_id,
        "device_id": connection.device_id,
        "event_id": event.event_id,
        "payload_digest": event.payload_digest,
        "receipt_id": event.receipt_id or str(uuid4()),
        "status": "accepted",
        "processing_state": "completed",
        "server_enqueued_at": "2026-11-10T08:00:00+00:00",
        "processing_updated_at": "2026-11-10T08:00:05+00:00",
        "extra_private_body": "synthetic remote body must never escape",
        **changes,
    }


def sample(tmp_path):
    connection = RemoteConnection(**connection_data())
    queue, raw = queue_for(tmp_path, connection)
    return (
        connection,
        queue,
        raw,
        queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])[0],
    )


def acknowledge(queue, event):
    queue.acknowledge(
        instance_id=queue.instance_id,
        device_id=queue.device_id,
        event_id=event.event_id,
        payload_digest=event.payload_digest,
        receipt_id=str(uuid4()),
    )


def test_recent_metadata_is_bounded_scoped_ordered_and_excludes_terminal_rejections(tmp_path):
    _, queue, raw, first = sample(tmp_path)
    acknowledge(queue, first)
    wanted = [raw["event_id"]]
    for index in range(8):
        event = {**raw, "event_id": str(uuid4()), "content": {"text": f"private-body-{index}"}}
        queue.enqueue(event)
        wanted.append(event["event_id"])
    last = queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])[0]
    queue.reject_delivery(
        instance_id=queue.instance_id,
        device_id=queue.device_id,
        event_id=last.event_id,
        payload_digest=last.payload_digest,
        code=TERMINAL_REJECTION_CODE,
    )
    for changes in ({"scope_id": str(uuid4())}, {"source_id": str(uuid4())}):
        queue.enqueue({**raw, **changes, "event_id": str(uuid4())})
    before = queue.path.read_bytes()
    rows = queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])
    assert [row.event_id for row in rows] == list(reversed(wanted[:-1]))[:5]
    assert [row.sequence for row in rows] == sorted((row.sequence for row in rows), reverse=True)
    assert "private-body" not in json.dumps([asdict(row) for row in rows])
    assert all("payload" not in asdict(row) for row in rows)
    assert queue.path.read_bytes() == before
    global_event = {**raw, "scope_id": "global", "event_id": str(uuid4())}
    queue.enqueue(global_event)
    assert (
        queue.recent_metadata(scope_id="global", source_id=raw["source_id"])[0].event_id
        == global_event["event_id"]
    )


@pytest.mark.parametrize("limit", [0, 6, True, "1"])
def test_recent_metadata_rejects_invalid_limits(tmp_path, limit):
    _, queue, raw, _ = sample(tmp_path)
    with pytest.raises(OutboxError, match="invalid_metadata_query"):
        queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"], limit=limit)


@pytest.mark.parametrize(
    "damage", ["json", "duplicate", "digest", "binding", "event", "receipt", "sequence"]
)
def test_recent_metadata_rejects_corruption_without_returning_content(tmp_path, damage):
    _, queue, raw, event = sample(tmp_path)
    with sqlite3.connect(queue.path) as db:
        payload = db.execute("SELECT payload FROM events").fetchone()[0]
        if damage in {"json", "duplicate", "binding"}:
            if damage == "json":
                payload = "private invalid JSON"
            elif damage == "duplicate":
                payload = '{"scope_id":' + json.dumps(raw["scope_id"]) + "," + payload[1:]
            else:
                value = json.loads(payload)
                value["device_id"] = str(uuid4())
                payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
            db.execute(
                "UPDATE events SET payload=?,payload_digest=?",
                (payload, hashlib.sha256(payload.encode()).hexdigest()),
            )
        elif damage == "digest":
            db.execute("UPDATE events SET payload_digest=?", ("0" * 64,))
        elif damage == "event":
            db.execute("UPDATE events SET event_id=?", (str(uuid4()),))
        elif damage == "receipt":
            db.execute("UPDATE events SET status='accepted',receipt_id=NULL")
        else:
            db.execute("UPDATE events SET sequence=-1")
    before = queue.path.read_bytes()
    with pytest.raises(OutboxError) as raised:
        queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])
    assert str(raised.value) in {"outbox_unavailable", "outbox_metadata_corrupted"}
    assert queue.path.read_bytes() == before


@pytest.mark.parametrize("legacy", [False, True])
def test_processing_receipts_are_whitelisted_and_old_hubs_need_no_timestamps(tmp_path, legacy):
    connection, queue, _, event = sample(tmp_path)
    value = receipt(connection, event)
    if legacy:
        value.pop("server_enqueued_at")
        value.pop("processing_updated_at")
    methods = []

    def handle(request):
        methods.append(request.method)
        return response(value)

    before = queue.path.read_bytes()
    result = asyncio.run(
        read_processing_sample(connection, [event], transport=httpx.MockTransport(handle))
    )
    assert result["status"] == "checked"
    assert result["events"][0]["processing_state"] == "completed"
    assert result["latest_known_completed_update_at_in_sample"] == (
        None if legacy else value["processing_updated_at"]
    )
    assert result["events"][0]["server_enqueued_at"] == (
        None if legacy else value["server_enqueued_at"]
    )
    assert value["extra_private_body"] not in json.dumps(result)
    assert connection.credential.get_secret_value() not in json.dumps(result)
    assert queue.path.read_bytes() == before and methods == ["GET"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol_version", True),
        ("instance_id", str(uuid4())),
        ("device_id", str(uuid4())),
        ("event_id", str(uuid4())),
        ("payload_digest", "0" * 64),
        ("receipt_id", "invalid"),
        ("status", "queued"),
        ("processing_state", "private body"),
        ("processing_state", []),
        ("server_enqueued_at", "2026-11-10"),
        ("processing_updated_at", "not a time"),
    ],
)
def test_processing_rejects_forged_or_invalid_metadata(tmp_path, field, value):
    connection, _, _, event = sample(tmp_path)
    result = asyncio.run(
        read_processing_sample(
            connection,
            [event],
            transport=httpx.MockTransport(
                lambda request: response(receipt(connection, event, **{field: value}))
            ),
        )
    )
    assert result["status"] == "unavailable"
    assert result["events"][0]["remote_status"] == "unavailable"
    assert "processing_state" not in result["events"][0]
    assert "private body" not in json.dumps(result)


def test_known_receipt_identity_must_match_and_pending_404_is_distinct(tmp_path):
    connection, queue, raw, event = sample(tmp_path)
    missing = httpx.MockTransport(lambda request: response({"detail": "event_not_found"}, 404))
    result = asyncio.run(read_processing_sample(connection, [event], transport=missing))
    assert result["events"][0]["remote_status"] == "no_receipt"
    assert result["status"] == "checked"
    acknowledge(queue, event)
    accepted = queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])[0]
    for status, value, expected in (
        (404, {"detail": "event_not_found"}, "remote_receipt_missing"),
        (404, {"detail": "not_found"}, "remote_endpoint_unavailable"),
        (200, receipt(connection, accepted, receipt_id=str(uuid4())), "remote_receipt_mismatch"),
    ):
        result = asyncio.run(
            read_processing_sample(
                connection,
                [accepted],
                transport=httpx.MockTransport(
                    lambda request, value=value, status=status: response(value, status)
                ),
            )
        )
        assert result["events"][0]["error"] == expected


def test_status_keeps_successful_sample_after_one_failure_and_never_acknowledges(tmp_path):
    _, raw, config = install(tmp_path)
    service = AgentSettings.from_file(config).open()
    _, event = queue_for(tmp_path / "event", service.connection)
    event.update(source_id=raw["source_id"], scope_id=raw["scope_id"])
    service.outbox.enqueue(event)
    service.outbox.enqueue({**event, "event_id": str(uuid4())})
    metadata = service.outbox.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])
    handshake = authorized(raw)
    methods = []

    def handle(request):
        methods.append(request.method)
        if "/events/" not in request.url.path:
            return handshake.handle_request(request)
        if request.url.path.endswith(metadata[0].event_id):
            return json_response(receipt(service.connection, metadata[0]))
        raise httpx.ReadError("synthetic body must not be exposed")

    before = service.outbox.path.read_bytes()
    result = asyncio.run(
        client_status(config, check_remote=True, transport=httpx.MockTransport(handle))
    )
    assert result["remote"]["status"] == "authorized"
    assert result["processing"]["status"] == "partial"
    assert result["processing"]["events"][0]["remote_status"] == "accepted"
    assert result["processing"]["events"][1]["error"] == "transport_unavailable"
    assert service.outbox.path.read_bytes() == before
    assert service.outbox.status()["queued"] == 2
    assert set(methods) == {"GET"}
    assert "synthetic body" not in json.dumps(result)


def test_unchecked_and_empty_status_do_not_claim_a_success_time(tmp_path):
    _, raw, config = install(tmp_path)
    local = asyncio.run(
        client_status(
            config,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("status without --check-remote must not use network")
            ),
        )
    )
    assert local["processing"] == {"status": "not_checked"}
    checked = asyncio.run(client_status(config, check_remote=True, transport=authorized(raw)))
    assert checked["processing"]["events"] == []
    assert checked["processing"]["latest_known_enqueued_at_in_sample"] is None
    assert checked["processing"]["latest_known_completed_update_at_in_sample"] is None


def test_live_receipt_times_match_core_and_status_does_not_upload(live_hub, tmp_path):
    config, app, uploaded = live_hub

    async def scenario():
        connection, admin, _, event = await bind_live(config)
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, event["scope_id"])
        path, _ = pairing(
            tmp_path,
            **connection.model_dump(mode="json"),
            scope_id=event["scope_id"],
            source_id=event["source_id"],
        )
        downloaded = json.loads(path.read_text())
        downloaded["credential"] = connection.credential.get_secret_value()
        path.write_text(json.dumps(downloaded))
        installed = await import_pairing(path, client_root=tmp_path / "clients")
        settings = AgentSettings.from_file(Path(installed["config_file"]))
        service = settings.open()
        service.outbox.enqueue(event)
        await send_pending(connection, service.outbox)
        metadata = service.outbox.recent_metadata(
            scope_id=event["scope_id"], source_id=event["source_id"]
        )[0]
        # Synthetic terminal outcome verifies status plumbing, not model extraction.
        app.core.state.memory_write_queue.settle(
            metadata.receipt_id, state="completed", hy_invoked=True
        )
        before = service.outbox.path.read_bytes()
        uploaded_before = len(uploaded)
        result = await client_status(Path(installed["config_file"]), check_remote=True)
        returned = result["processing"]["events"][0]
        with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
            created, updated = db.execute(
                "SELECT created_at,updated_at FROM memory_write_jobs WHERE job_id=?",
                (metadata.receipt_id,),
            ).fetchone()
        assert returned["processing_state"] == "completed"
        assert datetime.fromisoformat(returned["server_enqueued_at"]) == datetime.fromisoformat(
            created
        )
        assert datetime.fromisoformat(returned["processing_updated_at"]) == datetime.fromisoformat(
            updated
        )
        assert service.outbox.path.read_bytes() == before
        assert len(uploaded) == uploaded_before == 1

    asyncio.run(scenario())


def test_processing_sample_bounds_concurrency_and_total_wait(tmp_path, monkeypatch):
    from cloud_memory_hub import client_processing

    connection, queue, raw, _ = sample(tmp_path)
    for _ in range(4):
        queue.enqueue({**raw, "event_id": str(uuid4())})
    events = queue.recent_metadata(scope_id=raw["scope_id"], source_id=raw["source_id"])
    monkeypatch.setattr(client_processing, "REQUEST_DEADLINE", 0.03)
    active = peak = 0

    async def slow(request):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1
        pytest.fail("slow response should have been cancelled")

    async def check():
        return await asyncio.wait_for(
            read_processing_sample(connection, events, transport=httpx.MockTransport(slow)),
            timeout=1,
        )

    result = asyncio.run(check())
    assert peak <= 2 and active == 0
    assert result["status"] == "unavailable" and len(result["events"]) == 5
    assert all(item["retryable"] for item in result["events"])


@pytest.mark.parametrize(
    "state,retryable,expected",
    [
        ("checked", False, 0),
        ("partial", False, 2),
        ("partial", True, 3),
        ("unavailable", False, 2),
    ],
)
def test_setup_status_exit_code_preserves_partial_failure(
    state, retryable, expected, monkeypatch, capsys
):
    from cloud_memory_hub import setup_cli

    async def status(*args, **kwargs):
        return {
            "remote": {"status": "authorized"},
            "processing": {"status": state, "events": [{"retryable": retryable}]},
        }

    monkeypatch.setattr(setup_cli, "client_status", status)
    monkeypatch.setattr(
        "sys.argv",
        ["cloud-memory-setup", "status", "--config-file", "/synthetic/config", "--check-remote"],
    )
    previous_umask = os.umask(0o077)
    try:
        assert setup_cli.main() == expected
    finally:
        os.umask(previous_umask)
    assert json.loads(capsys.readouterr().out)["processing"]["status"] == state
