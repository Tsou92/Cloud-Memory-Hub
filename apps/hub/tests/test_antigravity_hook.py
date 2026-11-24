"""Visible-turn capture, transcript extraction, and live recall for Antigravity / T3 Code."""

import asyncio
import json
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
import test_agent_access
import test_transport
from cloud_memory_hub.agent_access import AgentAccessError
from cloud_memory_hub.agent_hook import MAX_HOOK_BYTES
from cloud_memory_hub.antigravity_hook import (
    SILENT,
    antigravity_message,
    process_antigravity_message,
)
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.read_contract import SESSION_MARKDOWN_CHARS
from test_agent_access import access_for, read_grants, second_device
from test_agent_hook import no_delivery, read_reply, service_for
from test_transport import bind_live

live_hub = test_transport.live_hub
preserve_pytest_log_handlers = test_agent_access.preserve_pytest_log_handlers


def pre_payload(
    workspace,
    *,
    text="Synthetic user task.",
    conversation_id="agy-synthetic-conv",
    initial_num_steps=0,
    invocation_num=1,
    transcript_path=None,
    workspace_paths=None,
):
    value = {
        "hookEventName": "PreInvocation",
        "cwd": str(workspace),
        "conversationId": conversation_id,
        "lastUserInput": text,
        "initialNumSteps": initial_num_steps,
        "invocationNum": invocation_num,
        "workspacePaths": [str(workspace)] if workspace_paths is None else workspace_paths,
    }
    if transcript_path is not None:
        value["transcriptPath"] = str(transcript_path)
    return value


def stop_payload(
    workspace,
    transcript_path,
    *,
    conversation_id="agy-synthetic-conv",
    termination_reason="NO_TOOL_CALL",
    text="Synthetic user task.",
):
    return {
        "hookEventName": "Stop",
        "cwd": str(workspace),
        "conversationId": conversation_id,
        "lastUserInput": text,
        "transcriptPath": str(transcript_path),
        "executionNum": 0,
        "terminationReason": termination_reason,
        "fullyIdle": True,
        "continuationActive": False,
        "workspacePaths": [str(workspace)],
    }


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))


def run_antigravity_hook(workspace, raw, config=None, *, cwd=None, arguments=()):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cloud_memory_hub.agent_cli",
            "--config-file",
            str(config or workspace / "absent.json"),
            "antigravity-hook",
            *arguments,
        ],
        input=raw if isinstance(raw, str) else json.dumps(raw),
        cwd=cwd or workspace,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_pre_invocation_accepts_agents_hook_cwd_and_extracts_visible_prompt(tmp_path, monkeypatch):
    workspace = tmp_path.resolve()
    agents_dir = workspace / ".agents"
    agents_dir.mkdir()
    monkeypatch.chdir(agents_dir)
    raw = pre_payload(workspace, initial_num_steps=4, invocation_num=1)
    message = antigravity_message(raw, workspace)
    assert message == (
        "PreInvocation",
        "user",
        "Synthetic user task.",
        "agy-synthetic-conv",
        "invocation:4",
    )


def test_workspace_checks_reject_foreign_cwd_forged_workspace_paths_and_symlink_agents(
    tmp_path,
):
    workspace = tmp_path.resolve() / "project"
    workspace.mkdir()
    other = tmp_path.resolve() / "other"
    other.mkdir()
    (other / ".agents").mkdir()

    missing = run_antigravity_hook(workspace, pre_payload(workspace))
    assert missing.returncode == 0 and json.loads(missing.stdout)["injectSteps"]
    assert json.loads(missing.stderr)["error"] == "hook_workspace_required"

    forged = run_antigravity_hook(
        workspace,
        pre_payload(other),
        cwd=workspace,
        arguments=("--workspace", str(workspace)),
    )
    assert json.loads(forged.stderr)["error"] == "hook_workspace_mismatch"

    foreign_cwd = run_antigravity_hook(
        workspace,
        pre_payload(workspace),
        cwd=other / ".agents",
        arguments=("--workspace", str(workspace)),
    )
    assert json.loads(foreign_cwd.stderr)["error"] == "hook_workspace_mismatch"

    symlink_agents = workspace / ".agents"
    symlink_agents.symlink_to(other / ".agents", target_is_directory=True)
    symlink_run = run_antigravity_hook(
        workspace,
        pre_payload(workspace),
        cwd=symlink_agents,
        arguments=("--workspace", str(workspace)),
    )
    assert json.loads(symlink_run.stderr)["error"] == "hook_workspace_mismatch"


def test_ignored_events_and_cursor_never_touch_private_config(tmp_path):
    for raw in (
        {"hookEventName": "SessionStart"},
        {"hookEventName": "PreToolUse"},
        {"hookEventName": "PostToolUse"},
        {"hookEventName": "PostInvocation"},
        {"hookEventName": "Stop", "cursor_version": "x"},
    ):
        result = run_antigravity_hook(tmp_path, raw)
        assert result.returncode == 0 and json.loads(result.stdout) == SILENT
        assert result.stderr == ""


def test_tool_loop_pre_invocation_dedup_and_same_prompt_next_turn(tmp_path, monkeypatch):
    workspace = tmp_path.resolve()
    monkeypatch.chdir(workspace)
    transcript = workspace / "transcript.jsonl"
    service = service_for(tmp_path)
    recall_calls = []

    async def reader(operation, request):
        recall_calls.append((operation, request))
        return read_reply(service, operation, ("SYNTHETIC_FACT",))

    monkeypatch.setattr(service, "_deliver", no_delivery)
    monkeypatch.setattr(service, "_read", reader)

    async def scenario():
        write_jsonl(
            transcript,
            [{"type": "USER_INPUT", "step_index": 0, "status": "DONE", "content": "continue"}],
        )
        first_msg = antigravity_message(
            pre_payload(
                workspace,
                text="continue",
                initial_num_steps=0,
                invocation_num=1,
                transcript_path=transcript,
            ),
            workspace,
        )
        output1, status1 = await process_antigravity_message(service, first_msg)
        assert status1["capture"] == "queued" and status1["recall"] == "retrieved"
        assert "SYNTHETIC_FACT" in output1["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert len(recall_calls) == 2

        # Tool-loop continuation in the same turn: transcript now shows PLANNER_RESPONSE.
        write_jsonl(
            transcript,
            [
                {"type": "USER_INPUT", "step_index": 0, "status": "DONE", "content": "continue"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "status": "DONE",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
                },
            ],
        )
        assert (
            antigravity_message(
                pre_payload(
                    workspace,
                    text="continue",
                    initial_num_steps=0,
                    invocation_num=2,
                    transcript_path=transcript,
                ),
                workspace,
            )
            is None
        )

        # Even without a transcript update, Outbox replay check prevents duplicate recall/injection.
        second_same_turn = antigravity_message(
            pre_payload(workspace, text="continue", initial_num_steps=0, invocation_num=2),
            workspace,
        )
        output2, status2 = await process_antigravity_message(service, second_same_turn)
        assert output2 == SILENT and status2["capture"] == "replayed"
        assert len(recall_calls) == 2

        # Mutated prompt with same initialNumSteps must fail closed as identity conflict.
        tampered = antigravity_message(
            pre_payload(workspace, text="mutated prompt", initial_num_steps=0, invocation_num=2),
            workspace,
        )
        with pytest.raises(AgentAccessError, match="request_identity_conflict"):
            await process_antigravity_message(service, tampered)

        # Turn 2 with identical text ("continue") before USER_INPUT #2 flushes must NOT be skipped.
        write_jsonl(
            transcript,
            [
                {"type": "USER_INPUT", "step_index": 0, "status": "DONE", "content": "continue"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "status": "DONE",
                    "content": "Done turn 1.",
                },
            ],
        )
        turn2_msg = antigravity_message(
            pre_payload(
                workspace,
                text="continue",
                initial_num_steps=2,
                invocation_num=1,
                transcript_path=transcript,
            ),
            workspace,
        )
        output3, status3 = await process_antigravity_message(service, turn2_msg)
        assert status3["capture"] == "queued" and status3["recall"] == "retrieved"
        assert len(recall_calls) == 4 and len(service.outbox.pending()) == 2
        assert output3["injectSteps"][0]["systemMessage"]["systemMessage"]

    asyncio.run(scenario())


def test_stop_extracts_final_planner_response_and_reads_full_when_truncated(tmp_path, monkeypatch):
    workspace = tmp_path.resolve()
    monkeypatch.chdir(workspace)
    transcript = workspace / "transcript.jsonl"
    transcript_full = workspace / "transcript_full.jsonl"

    long_reply = "Full final answer " + ("segment " * 60)
    write_jsonl(
        transcript,
        [
            {"type": "USER_INPUT", "step_index": 0, "status": "DONE", "content": "old turn"},
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 1,
                "status": "DONE",
                "content": "Old answer must not be captured.",
            },
            {"type": "USER_INPUT", "step_index": 2, "status": "DONE", "content": "current turn"},
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 3,
                "status": "DONE",
                "thinking": "internal thinking must stay out",
                "tool_calls": [{"name": "read_file", "args": {"Path": "/x"}}],
            },
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 4,
                "status": "DONE",
                "content": long_reply[:24],
                "truncated_fields": ["content"],
            },
            {"type": "GENERIC", "step_index": 5, "status": "DONE", "content": "metadata note"},
        ],
    )
    write_jsonl(
        transcript_full,
        [
            {
                "type": "PLANNER_RESPONSE",
                "step_index": 4,
                "status": "DONE",
                "content": long_reply,
            }
        ],
    )

    message = antigravity_message(stop_payload(workspace, transcript), workspace)
    assert message == ("Stop", "assistant", long_reply.strip(), "agy-synthetic-conv", "step:4")

    # Canceled or errored turns must not capture partial/stale answers.
    assert (
        antigravity_message(
            stop_payload(workspace, transcript, termination_reason="USER_CANCELED"),
            workspace,
        )
        is None
    )
    assert (
        antigravity_message(
            stop_payload(workspace, transcript, termination_reason="ERROR"),
            workspace,
        )
        is None
    )


def test_stop_rejects_symlinked_transcript_and_oversized_input(tmp_path, monkeypatch):
    workspace = tmp_path.resolve()
    monkeypatch.chdir(workspace)
    real = workspace / "real.jsonl"
    write_jsonl(
        real,
        [
            {"type": "USER_INPUT", "step_index": 0, "status": "DONE", "content": "hi"},
            {"type": "PLANNER_RESPONSE", "step_index": 1, "status": "DONE", "content": "ok"},
        ],
    )
    linked = workspace / "linked.jsonl"
    linked.symlink_to(real)
    with pytest.raises(AgentAccessError, match="hook_visible_text_invalid"):
        antigravity_message(stop_payload(workspace, linked), workspace)

    oversized = "x" * (MAX_HOOK_BYTES + 1)
    with pytest.raises(AgentAccessError, match="hook_input_too_large"):
        antigravity_message(pre_payload(workspace, text=oversized), workspace)


def test_antigravity_credential_filtering_and_real_http_two_device_session_handoff(
    live_hub, tmp_path, monkeypatch
):
    config, app, _ = live_hub
    workspace = tmp_path.resolve() / "workspace"
    (workspace / ".agents").mkdir(parents=True)
    monkeypatch.chdir(workspace / ".agents")

    async def scenario():
        connection, admin, _, raw = await bind_live(config)
        secret = uuid4().hex
        service_a = access_for(
            tmp_path / "state-a", connection, raw, privacy=CredentialFilter((secret,))
        )
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, raw["scope_id"])
            transcript_a = workspace / "transcript-a.jsonl"
            transcript_full_a = workspace / "transcript_full.jsonl"
            write_jsonl(
                transcript_a,
                [
                    {
                        "type": "USER_INPUT",
                        "step_index": 0,
                        "status": "DONE",
                        "content": f"Synthetic design note with {secret}",
                    }
                ],
            )
            msg1 = antigravity_message(
                pre_payload(
                    workspace,
                    text=f"Synthetic design note with {secret}",
                    conversation_id="agy-conv-a",
                    initial_num_steps=0,
                    transcript_path=transcript_a,
                ),
                workspace,
            )
            out1, status1 = await process_antigravity_message(service_a, msg1)
            assert status1["capture"] == "accepted" and status1["recall"] == "empty"
            assert secret not in service_a.outbox.path.read_text(errors="ignore")
            assert "injectSteps" in out1

            lead = "Antigravity final decision "
            remaining = SESSION_MARKDOWN_CHARS - len(("## 助手\n\n" + lead).encode())
            tail = " TAIL_MARKER_ANTIGRAVITY"
            full_reply = lead + "x" * remaining + tail
            write_jsonl(
                transcript_a,
                [
                    {
                        "type": "USER_INPUT",
                        "step_index": 0,
                        "status": "DONE",
                        "content": "Synthetic design note",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": 1,
                        "status": "DONE",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "a"}}],
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": 2,
                        "status": "DONE",
                        "content": full_reply[:30],
                        "truncated_fields": ["content"],
                    },
                ],
            )
            write_jsonl(
                transcript_full_a,
                [
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": 2,
                        "status": "DONE",
                        "content": full_reply,
                    }
                ],
            )
            stop1 = antigravity_message(
                stop_payload(workspace, transcript_a, conversation_id="agy-conv-a"),
                workspace,
            )
            stop_out, stop_status = await process_antigravity_message(service_a, stop1)
            assert stop_out == SILENT and stop_status["capture"] == "accepted"
            assert service_a.outbox.status()["accepted"] == 2

            # Second device recalls session summary and reads full paged Markdown.
            conn_b, source_b = await second_device(client, admin, connection, raw["scope_id"])
            service_b = access_for(tmp_path / "state-b", conn_b, source_b)
            msg_b = antigravity_message(
                pre_payload(
                    workspace,
                    text="Continue Antigravity design",
                    conversation_id="agy-conv-b",
                    initial_num_steps=0,
                ),
                workspace,
            )
            out_b, status_b = await process_antigravity_message(service_b, msg_b)
            assert status_b["recall"] == "retrieved"
            injected_b = out_b["injectSteps"][0]["systemMessage"]["systemMessage"]
            assert "Antigravity final decision" in injected_b
            briefs = await service_b.session_briefs(query="Antigravity final decision")
            ref = briefs["items"][0]["record_id"]
            page = await service_b.session_markdown(ref)
            markdown = page["markdown"]
            for _ in range(4):
                if page["next_cursor"] is None:
                    break
                page = await service_b.session_markdown(ref, cursor=page["next_cursor"])
                markdown += page["markdown"]
            assert tail in markdown

            # Device B writes a new reply; Device A gets it on next turn in the SAME conversation.
            transcript_b = workspace / "transcript-b.jsonl"
            write_jsonl(
                transcript_b,
                [
                    {
                        "type": "USER_INPUT",
                        "step_index": 0,
                        "status": "DONE",
                        "content": "Continue",
                    },
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": 1,
                        "status": "DONE",
                        "content": "AGY_PEER_UPDATE: switch router to strict mode.",
                    },
                ],
            )
            stop_b = antigravity_message(
                stop_payload(workspace, transcript_b, conversation_id="agy-conv-b"),
                workspace,
            )
            await process_antigravity_message(service_b, stop_b)

            msg_a2 = antigravity_message(
                pre_payload(
                    workspace,
                    text="What changed on the peer device?",
                    conversation_id="agy-conv-a",
                    initial_num_steps=3,
                    transcript_path=transcript_a,
                ),
                workspace,
            )
            out_a2, status_a2 = await process_antigravity_message(service_a, msg_a2)
            assert status_a2["recall"] == "retrieved"
            assert "AGY_PEER_UPDATE" in out_a2["injectSteps"][0]["systemMessage"]["systemMessage"]

            # Offline capture queues locally, then drains on next online turn.
            app.core.state.transport_test_offline = True
            write_jsonl(
                transcript_a,
                [
                    {"type": "USER_INPUT", "step_index": 3, "status": "DONE", "content": "q2"},
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": 4,
                        "status": "DONE",
                        "content": "offline answer",
                    },
                ],
            )
            stop_a2 = antigravity_message(
                stop_payload(workspace, transcript_a, conversation_id="agy-conv-a"),
                workspace,
            )
            _, offline_status = await process_antigravity_message(service_a, stop_a2)
            assert offline_status["capture"] == "queued"
            app.core.state.transport_test_offline = False
            msg_a3 = antigravity_message(
                pre_payload(
                    workspace,
                    text="Sync check",
                    conversation_id="agy-conv-a",
                    initial_num_steps=5,
                ),
                workspace,
            )
            await process_antigravity_message(service_a, msg_a3)
            assert service_a.outbox.status()["queued"] == 0

            # Revoked read permission fails closed and injects denied notice.
            revoked = await client.put(
                f"/hub/v1/devices/{connection.device_id}/grants",
                headers=admin,
                json={
                    "expected_revision": 3,
                    "grants": [{"scope_id": raw["scope_id"], "permissions": ["events:write"]}],
                },
            )
            assert revoked.status_code == 200
            msg_a4 = antigravity_message(
                pre_payload(
                    workspace,
                    text="After revocation",
                    conversation_id="agy-conv-a",
                    initial_num_steps=6,
                ),
                workspace,
            )
            out_a4, status_a4 = await process_antigravity_message(service_a, msg_a4)
            assert status_a4["recall"] == "denied"
            assert status_a4["recall_error"] == "scope_permission_required"
            assert "AGY_PEER_UPDATE" not in json.dumps(out_a4)
            assert "denied" in out_a4["injectSteps"][0]["systemMessage"]["systemMessage"].lower()

    asyncio.run(scenario())
