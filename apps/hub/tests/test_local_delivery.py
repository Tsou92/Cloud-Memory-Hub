import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventError
from cloud_memory_hub.local_outbox import LocalOutbox, OutboxError
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError


def event(**changes):
    value = {
        key: str(uuid4())
        for key in (
            "event_id",
            "instance_id",
            "device_id",
            "source_id",
            "scope_id",
            "conversation_id",
            "record_id",
        )
    }
    return {
        **value,
        "revision": 1,
        "kind": "conversation_turn",
        "content": {"messages": [{"role": "user", "text": "约定使用 SQLite，后续验证 NAS 重启。"}]},
        **changes,
    }


@pytest.fixture
def queued(tmp_path):
    raw = event()
    store = LocalOutbox.initialize(
        tmp_path / "connector" / "outbox.sqlite3",
        instance_id=raw["instance_id"],
        device_id=raw["device_id"],
    )
    return store, raw


@pytest.mark.parametrize(
    "template",
    [
        "password={secret}",
        'config: api_key="{secret}"',
        "--password '{secret}'",
        "--client-secret={secret}",
        "Authorization: Bearer {secret}",
        "Cookie: session={secret}; second={secret}\nNext: ordinary",
        "Set-Cookie: session={secret}; HttpOnly",
        "密码是{secret}",
        "访问令牌：{secret}",
        "The password is {secret}",
        "Cookie=session={secret}; second={secret}",
        "AWS_SECRET_ACCESS_KEY={secret}",
        "postgresql://user:{secret}@example.invalid/db",
        "https://example.invalid/?refresh_token={secret}&page=2",
        "https://example.invalid/?pass%77ord={secret}&page=2",
    ],
)
def test_text_credentials_removed_and_filter_is_idempotent(template):
    secret = uuid4().hex
    privacy = CredentialFilter()
    result = privacy.protect(
        {"text": template.format(secret=secret), "project": "Cloud Memory Hub"}
    )
    assert secret not in json.dumps(result.value)
    assert result.value["project"] == "Cloud Memory Hub"
    assert privacy.protect(result.value).value == result.value
    assert result.redacted


def test_recursive_keys_values_tool_json_and_known_credentials():
    secret = uuid4().hex
    privacy = CredentialFilter((secret,))
    content = {
        "title": secret,
        "metadata": {"x-api-key": secret, "credentials": {"nested": secret}, secret: 1},
        "tool_result": json.dumps({"password": {"object": secret}, "text": secret}),
        "url": "https://example.invalid/?arbitrary=" + secret,
    }
    cleaned = privacy.protect(content)
    assert secret not in json.dumps(cleaned.value)
    assert secret not in repr(privacy) and secret not in repr(cleaned)
    assert privacy.protect(cleaned.value).value == cleaned.value
    assert json.loads(cleaned.value["tool_result"])["password"] == "<redacted-secret>"


def test_known_token_formats_and_benign_identifiers():
    tokens = [
        "sk-" + "synthetic" * 4,
        "ghp_" + "x" * 30,
        "cmhdev_v1_" + str(uuid4()) + "_" + "x" * 43,
        "cmhdev_v1_" + str(uuid4()) + "_" + "x" * 42 + "-",
        "eyJ" + "x" * 15 + "." + "y" * 20 + "." + "z" * 20,
    ]
    privacy = CredentialFilter()
    value = privacy.protect(
        {"text": " ".join(tokens), "conversation_id": "conv-123", "message_id": "msg-456"}
    ).value
    assert all(token not in json.dumps(value) for token in tokens)
    assert value["conversation_id"] == "conv-123" and value["message_id"] == "msg-456"


def test_ordinary_text_markdown_and_url_parameters_survive_repeated_filtering():
    content = {
        "text": "[README](https://example.invalid/doc?q=sync&page=2)\n项目下一步是验证同步。"
    }
    privacy = CredentialFilter()
    assert privacy.protect(content).value == content
    short = CredentialFilter(("red",))
    value = short.protect({"text": "red https://example.invalid/?password=hidden&page=2"}).value
    assert short.protect(value).value == value
    assert "page=2" in value["text"]
    assert privacy.protect({"text": "[2026-10-23] 进度记录"}).value["text"].startswith("[2026")
    assert (
        CredentialFilter(("1234",)).protect({"value": 1234}).value["value"] == "<redacted-secret>"
    )
    ending = ")" * 10_000
    assert (
        privacy.protect({"text": "https://example.invalid/a" + ending})
        .value["text"]
        .endswith(ending)
    )


def test_encoded_json_fields_and_cookie_objects_are_filtered():
    secret = uuid4().hex
    tool = '{"pass\\u0077ord":"' + secret + '","useful":"decision"}'
    result = CredentialFilter().protect({"tool": tool, "cookies": {"session": secret}})
    assert secret not in json.dumps(result.value)
    assert "decision" in result.value["tool"]


def test_url_components_cannot_hide_nested_json_credentials_or_control_characters():
    sample = uuid4().hex
    nested = '{"pass\\u0077ord":"' + sample + '","useful":"decision"}'
    text = "https://example.invalid/?" + urlencode({"data": nested, "page": "2"})
    privacy = CredentialFilter()
    result = privacy.protect({"text": text}).value
    assert sample not in json.dumps(result)
    assert "decision" in result["text"] and "page=2" in result["text"]
    assert privacy.protect(result).value == result
    with pytest.raises(PrivacyError, match="hidden_control_character"):
        privacy.protect({"text": "https://example.invalid/?data=pass%00word"})


@pytest.mark.parametrize(
    "content",
    [
        {"image": "private image"},
        {"attachments": [{"text": "hidden"}]},
        {"text": "data:image/png;base64,AA=="},
        {"text": "pass\u200bword=secret"},
        {"text": '{"password":1,"password":2}'},
        {"text": '{"password":"unterminated'},
        {"value": float("nan")},
        {"text": b"unsupported"},
        {1: "invalid key"},
    ],
)
def test_unverifiable_content_is_rejected_without_echo(content):
    with pytest.raises(PrivacyError) as captured:
        CredentialFilter().protect(content)
    assert str(captured.value) in {
        "unsupported_media",
        "hidden_control_character",
        "duplicate_json_key",
        "invalid_json",
        "invalid_content",
        "invalid_metadata_key",
    }


def test_content_limits_and_key_collision():
    nested = "secret"
    for _ in range(30):
        nested = [nested]
    for content in ({"nested": nested}, {"text": "a" * (1024 * 1024)}, {"a": [1] * 20_001}):
        with pytest.raises(PrivacyError):
            CredentialFilter().protect(content)
    with pytest.raises(PrivacyError, match="filtered_key_collision"):
        CredentialFilter(("alpha-secret", "beta-secret")).protect(
            {"alpha-secret": 1, "beta-secret": 2}
        )


def test_queue_only_persists_filtered_content_and_fixed_rejections(queued):
    store, raw = queued
    secret = uuid4().hex
    raw["content"] = {"text": f"password={secret}", "tool": {"cookie": {"nested": secret}}}
    receipt = store.enqueue(raw)
    assert receipt["status"] == "queued" and receipt["receipt_id"] is None
    for file in store.path.parent.iterdir():
        if file.is_file():
            assert secret.encode() not in file.read_bytes()
    assert store.path.stat().st_mode & 0o777 == 0o600
    pending = store.pending()[0]
    assert secret not in pending.payload and "password" not in repr(pending)
    assert hashlib.sha256(pending.payload.encode()).hexdigest() == pending.payload_digest
    raw["content"] = {"attachments": [secret]}
    with pytest.raises(PrivacyError):
        store.enqueue(raw)
    assert store.status() == {"queued": 1, "accepted": 0, "rejected": {"privacy_rejected": 1}}
    assert secret.encode() not in store.path.read_bytes()


def test_restart_duplicate_capture_lost_ack_and_receipt_integrity(queued):
    store, raw = queued
    first = store.enqueue(raw)
    restarted = LocalOutbox(store.path, instance_id=store.instance_id, device_id=store.device_id)
    assert restarted.enqueue(raw) == first
    item = restarted.pending()[0]
    assert restarted.pending()[0] == item  # Sending or losing an acknowledgment never removes it.
    ack = dict(
        instance_id=store.instance_id,
        device_id=store.device_id,
        event_id=item.event_id,
        payload_digest=item.payload_digest,
        receipt_id=str(uuid4()),
    )
    for wrong in (
        {"instance_id": str(uuid4())},
        {"device_id": str(uuid4())},
        {"event_id": str(uuid4())},
        {"payload_digest": "bad"},
    ):
        with pytest.raises(OutboxError):
            restarted.acknowledge(**{**ack, **wrong})
    assert len(restarted.pending()) == 1
    restarted.acknowledge(**ack)
    restarted.acknowledge(**ack)
    again = LocalOutbox(store.path, instance_id=store.instance_id, device_id=store.device_id)
    assert again.pending() == () and again.status()["accepted"] == 1
    assert again.enqueue(raw)["status"] == "accepted"
    with pytest.raises(OutboxError, match="receipt_conflict"):
        again.acknowledge(**{**ack, "receipt_id": str(uuid4())})


def test_concurrent_duplicates_and_identity_conflicts(queued):
    store, raw = queued
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(store.enqueue, [raw] * 8))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert store.status()["queued"] == 1
    original = store.pending()[0]
    with pytest.raises(OutboxError, match="event_identity_conflict"):
        store.enqueue({**raw, "content": {"text": "different revision needs a new event ID"}})
    assert store.pending()[0] == original
    assert store.status()["rejected"] == {"identity_conflict": 1}


def test_changed_filter_or_damaged_payload_stops_delivery(queued):
    store, raw = queued
    newly_known = "synthetic-" + uuid4().hex
    raw["content"] = {"text": newly_known}
    store.enqueue(raw)
    stricter = LocalOutbox(
        store.path,
        instance_id=store.instance_id,
        device_id=store.device_id,
        privacy=CredentialFilter((newly_known,)),
    )
    with pytest.raises(OutboxError, match="requires_review"):
        stricter.pending()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE events SET payload='changed'")
    with pytest.raises(OutboxError, match="corrupted"):
        store.pending()


def test_binding_storage_and_contract_fail_closed(queued):
    store, raw = queued
    with pytest.raises(OutboxError, match="already_exists"):
        LocalOutbox.initialize(store.path, instance_id=store.instance_id, device_id=store.device_id)
    with pytest.raises(OutboxError, match="binding_mismatch"):
        LocalOutbox(store.path, instance_id=str(uuid4()), device_id=store.device_id)
    with pytest.raises(OutboxError, match="binding_mismatch"):
        store.enqueue({**raw, "device_id": str(uuid4())})
    for changes in (
        {"protocol_version": True},
        {"revision": True},
        {"scope_id": "*"},
        {"source_id": "claude-code"},
        {"extra": "secret"},
        {"occurred_at": "2026-10-23T00:00:00"},
    ):
        with pytest.raises(EventError, match="invalid_event"):
            store.enqueue({**raw, **changes})
    assert store.status()["queued"] == 0
    os.chmod(store.path, 0o644)
    with pytest.raises(OutboxError, match="unsafe"):
        store.status()
    os.chmod(store.path, 0o600)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(OutboxError, match="version"):
        store.status()


def test_missing_symlink_and_hardlink_databases_are_not_rebuilt(queued):
    store, _ = queued
    link = store.path.parent / "alias.sqlite3"
    link.symlink_to(store.path)
    with pytest.raises(OutboxError, match="unsafe"):
        LocalOutbox(link, instance_id=store.instance_id, device_id=store.device_id)
    link.unlink()
    os.link(store.path, link)
    with pytest.raises(OutboxError, match="unsafe"):
        store.pending()
    link.unlink()
    store.path.unlink()
    with pytest.raises(OutboxError, match="unavailable"):
        store.pending()
    assert not store.path.exists()


def test_committed_queue_survives_abrupt_process_exit(queued):
    store, raw = queued
    script = """
import json, os, sys
from pathlib import Path
from cloud_memory_hub.local_outbox import LocalOutbox
data = json.load(sys.stdin)
store = LocalOutbox(Path(data['path']), instance_id=data['event']['instance_id'],
                    device_id=data['event']['device_id'])
store.enqueue(data['event'])
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"path": str(store.path), "event": raw}),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    recovered = LocalOutbox(store.path, instance_id=store.instance_id, device_id=store.device_id)
    assert [item.event_id for item in recovered.pending()] == [raw["event_id"]]


def test_cli_uses_stdin_and_emits_only_receipts_or_safe_errors(tmp_path):
    raw = event()
    secret = uuid4().hex
    raw["content"] = {"text": "token=" + secret}
    args = [sys.executable, "-m", "cloud_memory_hub.connector"]
    binding = [
        "--state-dir",
        str(tmp_path / "state"),
        "--instance-id",
        raw["instance_id"],
        "--device-id",
        raw["device_id"],
    ]

    def run(command, payload=""):
        result = subprocess.run(
            [*args, command, *binding], input=payload, text=True, capture_output=True, timeout=15
        )
        assert secret not in result.stdout + result.stderr
        return result

    assert run("init").returncode == 0
    submitted = run("enqueue", json.dumps(raw))
    assert submitted.returncode == 0 and json.loads(submitted.stdout)["status"] == "queued"
    assert run("enqueue", '{"unparsed":"' + secret).returncode == 2
    state = json.loads(run("status").stdout)
    assert state == {"queued": 1, "accepted": 0, "rejected": {"privacy_rejected": 1}}
    assert secret.encode() not in (tmp_path / "state" / "outbox.sqlite3").read_bytes()
