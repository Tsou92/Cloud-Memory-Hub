"""Visible-turn capture, fresh authorized originals and nonblocking host output."""

import asyncio
import json
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import pytest
import test_agent_access
import test_transport
from cloud_memory_hub.agent_access import AgentAccessError
from cloud_memory_hub.agent_hook import MAX_HOOK_BYTES, SILENT, process_message, visible_message
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import SESSION_MARKDOWN_CHARS, ReadItem, ReadReply
from cloud_memory_hub.transport import RemoteConnection, TransportError
from test_agent_access import access_for, read_grants, second_device
from test_transport import bind_live, connection_data

live_hub = test_transport.live_hub
preserve_pytest_log_handlers = test_agent_access.preserve_pytest_log_handlers


def service_for(tmp_path, *, privacy=None):
    connection = RemoteConnection.model_validate(connection_data())
    raw = {"scope_id": str(uuid4()), "source_id": str(uuid4())}
    return access_for(tmp_path / "state", connection, raw, privacy=privacy)


def payload(workspace, *, event="UserPromptSubmit", text="Synthetic visible text."):
    return {
        "hook_event_name": event,
        "cwd": str(workspace),
        "session_id": "native-synthetic-session",
        "prompt_id": "native-synthetic-turn",
        "prompt" if event == "UserPromptSubmit" else "last_assistant_message": text,
        "transcript_path": "/must/not/read/transcript.jsonl",
    }


def run_hook(workspace, raw, config=None, *, arguments=()):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cloud_memory_hub.agent_cli",
            "--config-file",
            str(config or workspace / "absent.json"),
            "claude-hook",
            *arguments,
        ],
        input=raw if isinstance(raw, str) else json.dumps(raw),
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_native_helper_uses_only_visible_field_and_prompt_identity(tmp_path, monkeypatch):
    workspace = tmp_path.resolve()
    monkeypatch.chdir(workspace)
    raw = payload(workspace)
    raw.update(message_id="untrusted-alternate", messages=[{"role": "system", "text": "not used"}])
    message = visible_message(raw, workspace)
    assert message == (
        "UserPromptSubmit",
        "user",
        "Synthetic visible text.",
        "native-synthetic-session",
        "native-synthetic-turn",
    )
    stop = visible_message(payload(workspace, event="Stop", text="Final answer."), workspace)
    assert stop[:3] == ("Stop", "assistant", "Final answer.")


@pytest.mark.parametrize("event,role", [("UserPromptSubmit", "user"), ("Stop", "assistant")])
def test_codex_native_hook_uses_turn_id(tmp_path, monkeypatch, event, role):
    monkeypatch.chdir(tmp_path)
    raw = payload(tmp_path, event=event)
    raw["turn_id"] = raw.pop("prompt_id")
    message = visible_message(raw, tmp_path, adapter="codex")
    assert message == (
        event,
        role,
        "Synthetic visible text.",
        "native-synthetic-session",
        "native-synthetic-turn",
    )
    service = service_for(tmp_path)

    async def read(operation, _request):
        return read_reply(service, operation, ())

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", read)
    asyncio.run(process_message(service, message, adapter="codex"))
    captured = json.loads(service.outbox.pending()[0].payload)
    assert captured["content"]["metadata"]["adapter_id"] == "codex"
    raw["prompt_id"] = raw.pop("turn_id")
    with pytest.raises(AgentAccessError, match="hook_identity_missing_or_invalid"):
        visible_message(raw, tmp_path, adapter="codex")


@pytest.mark.parametrize("field", ["session_id", "prompt_id"])
@pytest.mark.parametrize("value", [None, "", " ", 42, "x" * 513])
def test_missing_or_invalid_native_identity_never_captures(tmp_path, monkeypatch, field, value):
    monkeypatch.chdir(tmp_path)
    raw = payload(tmp_path)
    raw[field] = value
    with pytest.raises(AgentAccessError, match="hook_identity_missing_or_invalid"):
        visible_message(raw, tmp_path)


def test_ignored_events_and_cursor_never_open_private_configuration(tmp_path):
    for raw in (
        {"hook_event_name": "SubagentStop"},
        {"hook_event_name": "Stop", "cursor_version": "x"},
    ):
        result = run_hook(tmp_path, raw)
        assert result.returncode == 0 and json.loads(result.stdout) == SILENT
        assert result.stderr == ""


@pytest.mark.parametrize("body", ["not-json", "x" * (MAX_HOOK_BYTES + 1), '"not-an-object"'])
def test_malformed_or_oversized_input_is_nonblocking_and_not_echoed(tmp_path, body):
    result = run_hook(tmp_path, body)
    assert result.returncode == 0 and json.loads(result.stdout)["continue"] is True
    status = json.loads(result.stderr)
    assert status["capture"] == "unavailable"
    assert body not in result.stdout + result.stderr


def test_workspace_is_required_and_both_actual_and_payload_paths_must_match(tmp_path):
    raw = payload(tmp_path)
    missing = run_hook(tmp_path, raw)
    assert json.loads(missing.stderr)["error"] == "hook_workspace_required"
    other = tmp_path / "other"
    other.mkdir()
    actual_mismatch = run_hook(tmp_path, payload(other), arguments=("--workspace", str(other)))
    forged_payload = run_hook(tmp_path, payload(other), arguments=("--workspace", str(tmp_path)))
    for result in (actual_mismatch, forged_payload):
        assert result.returncode == 0
        assert json.loads(result.stderr)["error"] == "hook_workspace_mismatch"
        assert str(tmp_path) not in result.stdout + result.stderr


def test_stop_failure_stays_silent_and_does_not_echo_text(tmp_path):
    result = run_hook(
        tmp_path,
        payload(tmp_path, event="Stop", text="private synthetic answer"),
        arguments=("--workspace", str(tmp_path)),
    )
    assert result.returncode == 0 and json.loads(result.stdout) == SILENT
    assert json.loads(result.stderr)["capture"] == "unavailable"
    assert "private synthetic answer" not in result.stdout + result.stderr


def test_native_replay_uses_role_and_turn_identity_and_filters_before_queue(tmp_path, monkeypatch):
    secret = uuid4().hex
    service = service_for(tmp_path, privacy=CredentialFilter((secret,)))

    async def offline():
        return TransportError("transport_unavailable", retryable=True)

    async def unavailable(*args):
        raise AgentAccessError("transport_unavailable", retryable=True)

    monkeypatch.setattr(service, "_deliver", offline)
    monkeypatch.setattr(service, "_read", unavailable)
    user = ("UserPromptSubmit", "user", "visible " + secret, "native-session", "turn-1")
    assistant = ("Stop", "assistant", "final reply", "native-session", "turn-1")

    async def scenario():
        await process_message(service, user)
        await process_message(service, assistant)
        await process_message(service, user)
        await process_message(service, assistant)
        await process_message(service, (*user[:4], "turn-2"))
        records = [json.loads(item.payload) for item in service.outbox.pending()]
        assert len(records) == 3
        assert len({row["event_id"] for row in records}) == 3
        assert len({row["conversation_id"] for row in records}) == 1
        assert all(UUID(row["event_id"]).version == 4 for row in records)
        assert all(row["kind"] == "conversation_turn" for row in records)
        assert [row["content"]["messages"][0]["role"] for row in records] == [
            "user",
            "assistant",
            "user",
        ]
        assert secret not in json.dumps(records)
        assert secret.encode() not in service.outbox.path.read_bytes()
        with pytest.raises(AgentAccessError, match="request_identity_conflict"):
            await process_message(
                service, ("Stop", "assistant", "changed reply", "native-session", "turn-1")
            )
        assert service.outbox.status()["queued"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("event,role", [("UserPromptSubmit", "user"), ("Stop", "assistant")])
def test_timeout_cancels_network_but_preserves_queued_capture(tmp_path, monkeypatch, event, role):
    service = service_for(tmp_path)
    cancelled = []
    monkeypatch.setattr("cloud_memory_hub.agent_hook.HOOK_NETWORK_DEADLINE", 0.02)

    async def stalled():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(service, "_deliver", stalled)
    output, status = asyncio.run(
        process_message(service, (event, role, "Synthetic.", "session", "turn"))
    )
    assert cancelled == [True]
    assert status["capture"] == "queued" and service.outbox.status()["queued"] == 1
    assert status["delivery_error"] == "hook_network_deadline_exceeded"
    assert status["recall"] == ("unavailable" if role == "user" else "not_requested")
    assert output["continue"] is True
    if role == "assistant":
        assert output == SILENT


def test_unexpected_network_failure_preserves_capture_without_error_text(tmp_path, monkeypatch):
    service = service_for(tmp_path)

    async def broken():
        raise RuntimeError("private synthetic diagnostics")

    monkeypatch.setattr(service, "_deliver", broken)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    assert status["capture"] == "queued"
    assert "private synthetic" not in json.dumps([output, status])


def test_old_hub_fallback_preserves_authorized_original_paging(tmp_path, monkeypatch):
    service = service_for(tmp_path)
    calls = []

    async def offline():
        return TransportError("transport_unavailable", retryable=True)

    async def reader(operation, request):
        calls.append((operation, request))
        if operation == "session-briefs":
            raise AgentAccessError("remote_endpoint_unavailable")
        return ReadReply(
            instance_id=service.connection.instance_id,
            device_id=service.connection.device_id,
            device_revision=1,
            scope_id=service.scope_id,
            operation=operation,
            items=(),
            next_offset=5 if operation == "history-list" else None,
        )

    monkeypatch.setattr(service, "_deliver", offline)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    text = output["hookSpecificOutput"]["additionalContext"]
    assert "recent-history list and relevant-memory search returned no records" in text
    assert "Do not infer that no memories exist" in text
    assert status["capture"] == "queued" and status["recall"] == "empty" and status["truncated"]
    assert [operation for operation, _ in calls] == [
        "session-briefs",
        "memory-search",
        "history-list",
    ]
    assert calls[0][1]["query"] == "visible"
    assert calls[1][1]["scope_id"] == service.scope_id
    assert calls[2][1] == {"limit": 5, "offset": 0}


def read_reply(service, operation, texts=("SYNTHETIC_REFERENCE",), **updates):
    return ReadReply(
        instance_id=service.connection.instance_id,
        device_id=service.connection.device_id,
        device_revision=1,
        scope_id=service.scope_id,
        operation=operation,
        items=tuple(
            ReadItem(
                record_id=f"{operation}:{index}",
                kind="history" if operation in {"history-list", "session-briefs"} else "memory",
                layer="session_summary" if operation == "session-briefs" else None,
                content=text,
                content_offset=0,
                agent_id="synthetic",
                source_raw_ids=("synthetic-source",),
            )
            for index, text in enumerate(texts)
        ),
    ).model_copy(update=updates)


def test_session_summary_and_relevant_older_memory_are_combined_in_budget(tmp_path, monkeypatch):
    service = service_for(tmp_path)
    calls = {}

    async def scenario():
        both_started = asyncio.Event()

        async def reader(operation, request):
            calls[operation] = request
            if len(calls) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            texts = (
                ("OLDER_DECISION: navigation uses green.", "SECOND_DECISION", "THIRD_DECISION")
                if operation == "memory-search"
                else tuple(f"recent summary {index}" for index in range(3))
            )
            return read_reply(service, operation, texts)

        monkeypatch.setattr(service, "_read", reader)
        output, status = await process_message(
            service, ("UserPromptSubmit", "user", "What navigation color?", "session", "turn")
        )
        text = output["hookSpecificOutput"]["additionalContext"]
        records = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        assert status["recall"] == "retrieved" and status["records"] == 5
        assert records[0]["content"] == "recent summary 0"
        assert records[0]["session_ref"] == "session-briefs:0"
        assert records[1]["content"] == "OLDER_DECISION: navigation uses green."
        assert "THIRD_DECISION" not in text
        assert status["truncated"]

    monkeypatch.setattr(service, "_deliver", no_delivery)
    asyncio.run(scenario())
    assert calls["memory-search"]["query"] == "What navigation color?"


async def no_delivery():
    return None


@pytest.mark.parametrize("failed_operation", ["memory-search", "session-briefs"])
@pytest.mark.parametrize("empty", [True, False])
def test_one_temporary_failure_has_explicit_partial_context(
    tmp_path, monkeypatch, failed_operation, empty
):
    service = service_for(tmp_path)

    async def reader(operation, request):
        if operation == failed_operation:
            raise AgentAccessError("transport_unavailable", retryable=True)
        return read_reply(service, operation, () if empty else ("AVAILABLE_REFERENCE",))

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    text = output["hookSpecificOutput"]["additionalContext"]
    assert status["recall"] == "partial"
    assert status["records"] == (0 if empty else 1)
    assert "is unavailable for this turn" in text and "context is incomplete" in text
    if empty:
        assert "available authorized read returned no records" in text
    else:
        assert "AVAILABLE_REFERENCE" in text


@pytest.mark.parametrize(
    "code,retryable,state",
    [
        ("scope_permission_required", False, "denied"),
        ("device_authentication_required", True, "denied"),
        ("remote_read_invalid", False, "unavailable"),
    ],
)
def test_permanent_or_permission_error_discards_other_read(
    tmp_path, monkeypatch, code, retryable, state
):
    service = service_for(tmp_path)

    async def reader(operation, request):
        if operation == "memory-search":
            raise AgentAccessError(code, retryable=retryable)
        return read_reply(service, operation)

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    assert status["recall"] == state and status["recall_error"] == code
    assert "SYNTHETIC_REFERENCE" not in json.dumps(output)


@pytest.mark.parametrize("field", ["instance_id", "device_id", "scope_id", "device_revision"])
def test_authority_mismatch_discards_both_read_results(tmp_path, monkeypatch, field):
    service = service_for(tmp_path)

    async def reader(operation, request):
        updates = {field: 2 if field == "device_revision" else str(uuid4())}
        return read_reply(service, operation, **(updates if operation == "memory-search" else {}))

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    assert status["recall"] == "denied" and status["recall_error"] == "recall_authority_changed"
    assert "SYNTHETIC_REFERENCE" not in json.dumps(output)


def test_wrong_operation_discards_successful_other_read(tmp_path, monkeypatch):
    service = service_for(tmp_path)

    async def reader(operation, request):
        return read_reply(service, "history-list")

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    assert status["recall"] == "unavailable" and status["recall_error"] == "recall_response_invalid"
    assert "SYNTHETIC_REFERENCE" not in json.dumps(output)


def test_both_temporary_read_failures_are_unavailable_without_partial_claim(tmp_path, monkeypatch):
    service = service_for(tmp_path)

    async def reader(operation, request):
        raise AgentAccessError("transport_unavailable", retryable=True)

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    assert status["recall"] == "unavailable" and status["recall_error"] == "transport_unavailable"
    assert "partial" not in json.dumps([output, status])
    assert "cmh_memory_context" not in json.dumps(output)


def test_search_query_filters_full_prompt_before_cutting_and_never_logs_it(tmp_path, monkeypatch):
    secret = uuid4().hex
    service = service_for(tmp_path, privacy=CredentialFilter((secret,)))
    requests = []

    async def reader(operation, request):
        requests.append(request)
        return read_reply(service, operation, ())

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(
            service,
            ("UserPromptSubmit", "user", "x" * 4080 + secret + " tail" * 100, "session", "turn"),
        )
    )
    assert len(requests[0]["query"]) == 4096 and status["truncated"]
    assert secret[:12] not in json.dumps([requests, output, status])
    assert "x" * 100 not in json.dumps([output, status])


def test_read_deadline_discards_finished_context_and_cancels_other_read(tmp_path, monkeypatch):
    service = service_for(tmp_path)
    cancelled = []
    monkeypatch.setattr("cloud_memory_hub.agent_hook.HOOK_NETWORK_DEADLINE", 0.02)

    async def reader(operation, request):
        if operation == "session-briefs":
            return read_reply(service, operation)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(operation)

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)
    output, status = asyncio.run(
        process_message(service, ("UserPromptSubmit", "user", "visible", "session", "turn"))
    )
    assert cancelled == ["memory-search"]
    assert status["recall"] == "unavailable"
    assert status["recall_error"] == "hook_network_deadline_exceeded"
    assert "SYNTHETIC_REFERENCE" not in json.dumps(output)
    assert status["capture"] == "queued"


def test_real_http_visible_turn_replay_recent_update_and_revocation(live_hub, tmp_path):
    config, app, uploaded = live_hub

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        service = access_for(tmp_path / "state", connection, raw)
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, raw["scope_id"])
            turn1 = ("UserPromptSubmit", "user", "synthetic user text", "session", "turn-1")
            output, status = await process_message(service, turn1)
            assert status["capture"] == "accepted" and status["recall"] == "empty"
            assert "synthetic user text" not in output["hookSpecificOutput"]["additionalContext"]
            lead = "synthetic final reply "
            remaining = SESSION_MARKDOWN_CHARS - len(("## 助手\n\n" + lead).encode())
            fragment = '{"example":"continued"}\n\nMore Markdown.'
            stop = ("Stop", "assistant", lead + "x" * remaining + fragment, "session", "turn-1")
            assert (await process_message(service, stop))[0] == SILENT
            count = len(uploaded)
            await process_message(service, stop)
            assert len(uploaded) == count
            assert service.outbox.status()["accepted"] == 2
            other_connection, other_source = await second_device(
                client, admin, connection, raw["scope_id"]
            )
            other = access_for(tmp_path / "other", other_connection, other_source)
            _, other_status = await process_message(
                other, ("UserPromptSubmit", "user", "Continue this work", "other-session", "one")
            )
            assert other_status["recall"] == "retrieved", other_status
            briefs = await other.session_briefs(query="synthetic final reply")
            ref = next(
                item["record_id"]
                for item in briefs["items"]
                if "synthetic final reply" in item["content"]
            )
            page = await other.session_markdown(ref)
            markdown = page["markdown"]
            saw_fragment = False
            for _ in range(4):
                if page["next_cursor"] is None:
                    break
                page = await other.session_markdown(ref, cursor=page["next_cursor"])
                saw_fragment |= page["markdown"].startswith(fragment)
                markdown += page["markdown"]
            else:
                pytest.fail("Synthetic two-record session did not finish paging")
            assert "synthetic user text" in markdown and "synthetic final reply" in markdown
            assert saw_fragment
            await process_message(
                other,
                (
                    "Stop",
                    "assistant",
                    "OTHER_DEVICE_STYLE_UPDATE: use green navigation.",
                    "other-session",
                    "one",
                ),
            )
            output, status = await process_message(service, (*turn1[:4], "turn-2"))
            assert status["recall"] == "retrieved"
            assert "OTHER_DEVICE_STYLE_UPDATE" in output["hookSpecificOutput"]["additionalContext"]
            app.core.state.transport_test_offline = True
            queued = await process_message(
                service, ("Stop", "assistant", "offline visible reply", "session", "turn-2")
            )
            assert queued[1]["capture"] == "queued"
            app.core.state.transport_test_offline = False
            await process_message(
                service, ("UserPromptSubmit", "user", "continue", "session", "turn-3")
            )
            assert service.outbox.status()["queued"] == 0
            response = await client.put(
                f"/hub/v1/devices/{connection.device_id}/grants",
                headers=admin,
                json={
                    "expected_revision": 3,
                    "grants": [{"scope_id": raw["scope_id"], "permissions": ["events:write"]}],
                },
            )
            assert response.status_code == 200
            output, status = await process_message(service, (*turn1[:4], "turn-4"))
            assert status["capture"] == "accepted" and status["recall"] == "denied"
            assert "OTHER_DEVICE_STYLE_UPDATE" not in json.dumps(output)
            assert status["recall_error"] == "scope_permission_required"

    asyncio.run(scenario())
