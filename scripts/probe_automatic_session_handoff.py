"""Run two isolated native Claude sessions against a loopback Hub and model stub."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import probe_claude_native
from cloud_memory_hub.client_service import ClientServiceError
from cloud_memory_hub.client_setup import install_automation
from probe_agent_access import device_config
from probe_claude_native import ModelStub, NativeSession
from probe_claude_recall import LiveHub
from probe_claude_visible_hook import agent_call, recent, session_rows
from probe_two_hosts import same_project


def launch_native(root, executable, model, config_file):
    project = root / "project"
    project.mkdir()
    (root / "config").mkdir()
    setup = Path(sys.executable).absolute().with_name("cloud-memory-agent")
    with patch(
        "cloud_memory_hub.client_setup.service_install",
        side_effect=ClientServiceError("synthetic_probe_no_launch_service"),
    ):
        result = install_automation(
            config_file, agent="claude", workspace=project, home=root, executable=setup
        )
    assert result["status"] == "configured", "project_hooks_not_configured"

    def start_with_local_settings(arguments, **kwargs):
        settings_index = arguments.index("--settings")
        del arguments[settings_index : settings_index + 2]
        arguments[arguments.index("--setting-sources") + 1] = "local"
        return subprocess.Popen(arguments, **kwargs)

    # Reuse the isolated provider and CLI transport while letting Claude load
    # the real installer-generated .claude/settings.local.json from the project.
    launcher = SimpleNamespace(Popen=start_with_local_settings, PIPE=subprocess.PIPE)
    with patch.object(probe_claude_native, "subprocess", launcher):
        return NativeSession(root, executable, model)


def captured_session(endpoint, prompt, reply):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = recent(endpoint)
        matches = [row for row in rows if prompt in row["content"]]
        if matches:
            conversation = matches[0]["session_id"]
            captured = session_rows(rows, conversation)
            if any(reply in row["content"] for row in captured):
                assert len(matches) == 1 and len(captured) == 2, "visible_turn_duplicated"
                return conversation
        time.sleep(0.1)
    raise AssertionError("native_turn_not_shared")


def model_context(request, marker):
    for part in request["messages"][-1]["content"]:
        content = part.get("text", "")
        if "<cmh_memory_context" in content and marker in content:
            start = content.index("<cmh_memory_context")
            end = content.index("</cmh_memory_context>", start)
            return content[start:end]
    raise AssertionError("native_model_request_missing_session_context")


def read_markdown(endpoint, session_ref):
    pages = []
    cursor = None
    for _ in range(8):
        page = agent_call(
            endpoint, "session-markdown", {"session_ref": session_ref, "cursor": cursor}
        )
        assert page["session_ref"] == session_ref, "markdown_session_reference_changed"
        assert page["content_trust"] == "untrusted_history", "markdown_trust_boundary_missing"
        pages.append(page["markdown"])
        cursor = page["next_cursor"]
        if cursor is None:
            return "".join(pages)
    raise AssertionError("markdown_pagination_did_not_finish")


def probe():
    executable = shutil.which("claude")
    assert executable, "claude_cli_not_installed"
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
    started = time.monotonic()
    os.umask(0o077)
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cmh-native-handoff-")))
        root = root.resolve()
        hub = LiveHub(root)
        stack.callback(hub.close)
        scope = hub.call(
            "/hub/v1/projects",
            method="POST",
            status=201,
            body={"display_name": "Synthetic handoff"},
        )["project_id"]
        first, second = (
            {
                "python": sys.executable,
                "agent_config": str(device_config(hub, root / role, scope, role)),
            }
            for role in ("first", "second")
        )
        same_project(agent_call(first, "status"), agent_call(second, "status"))
        model = ModelStub("synthetic-" + uuid4().hex)
        model_thread = threading.Thread(target=model.serve_forever, daemon=True)
        model_thread.start()
        stack.callback(model_thread.join, 5)
        stack.callback(model.server_close)
        stack.callback(model.shutdown)
        session_a = launch_native(root / "first", executable, model, Path(first["agent_config"]))
        stack.callback(session_a.close)
        session_b = launch_native(root / "second", executable, model, Path(second["agent_config"]))
        stack.callback(session_b.close)

        prompt_a = "For the sample design, paint the CALICO arch blue and test the hinge."
        prompt_b = "Continue the sample design and report what is left to test."
        reply_a = "CMH_SYNTHETIC_REPLY_1"
        reply_b = "CMH_SYNTHETIC_REPLY_2"
        first_turn = session_a.turn(prompt_a)
        conversation_a = captured_session(second, prompt_a, reply_a)
        assert len(model.requests) == 1, "unexpected_first_model_requests"

        session_b.turn(prompt_b)
        conversation_b = captured_session(first, prompt_b, reply_b)
        assert conversation_a != conversation_b, "native_conversations_not_independent"
        assert len(model.requests) == 2, "unexpected_second_model_requests"
        context_b = model_context(model.requests[1], "CALICO arch")
        summary_a = next(
            json.loads(line) for line in context_b.splitlines() if line.startswith('{"scope_id"')
        )
        assert summary_a["kind"] == "session_summary", "session_summary_not_first"
        assert reply_a in summary_a["content"], "peer_final_answer_missing_from_summary"
        assert prompt_b not in context_b, "current_session_recalled_itself"
        markdown = read_markdown(second, summary_a["session_ref"])
        assert f"## 用户\n\n{prompt_a}\n\n" in markdown, "markdown_user_message_missing"
        assert f"## 助手\n\n{reply_a}\n\n" in markdown, "markdown_final_answer_missing"
        assert prompt_b not in markdown, "markdown_mixed_sessions"

        next_turn = session_a.turn("What should the sample design do next?")
        assert next_turn["session_id"] == first_turn["session_id"], "original_session_reopened"
        assert len(model.requests) == 3, "unexpected_third_model_requests"
        context_a = model_context(model.requests[2], reply_b)
        assert prompt_b in context_a, "existing_session_did_not_receive_latest_peer_turn"
        session_a.finish()
        session_b.finish()
    return {
        "probe": "automatic-native-session-handoff",
        "status": "passed",
        "claude_version": version,
        "native_sessions": 2,
        "physical_hosts": 1,
        "project_local_hooks_loaded": True,
        "ordinary_turns": 3,
        "session_summary_first": True,
        "markdown_user_and_final_reply_verified": True,
        "existing_session_received_peer_turn": True,
        "model": "local_text_only_test_double",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(probe(), sort_keys=True))
    except AssertionError as error:
        print(
            json.dumps(
                {
                    "probe": "automatic-native-session-handoff",
                    "status": "failed",
                    "stage": str(error),
                }
            )
        )
        raise SystemExit(1) from None
    except Exception as error:
        print(
            json.dumps(
                {
                    "probe": "automatic-native-session-handoff",
                    "status": "failed",
                    "error_type": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None
