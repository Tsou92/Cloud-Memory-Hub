"""Real isolated Claude hooks sharing visible turns through a paired Hub.

Use --config-file with the private two-host JSON documented by probe_two_hosts.py.
The local Python must include the new claude-hook command. The remote Python only
needs the existing Agent CLI. SSH carries commands, not a forwarded Hub port.
--local-fixture uses two local identities and never proves two physical hosts.
The generation model is a local text-only double, not an autonomous MCP caller.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from probe_agent_access import device_config
from probe_claude_native import ModelStub, NativeSession
from probe_claude_recall import LiveHub
from probe_two_hosts import configuration, launch_environment, preflight, same_project

OBSERVER = """import json,subprocess,sys
from pathlib import Path
root=Path(__file__).parent
raw=sys.stdin.read()
value=json.loads(raw)
result=subprocess.run(json.loads((root/'hook-command.json').read_text()),
    input=raw,text=True,capture_output=True,timeout=18)
with (root/'hook-events.jsonl').open('a') as output:
    output.write(json.dumps({'input':value,'stdout':result.stdout,
        'stderr':result.stderr,'returncode':result.returncode})+'\\n')
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
"""


def agent_command(endpoint, operation):
    arguments = [
        endpoint["python"],
        "-m",
        "cloud_memory_hub.agent_cli",
        "--config-file",
        endpoint["agent_config"],
        operation,
    ]
    if "ssh" in endpoint:
        ssh = endpoint["ssh"]
        return [
            ssh[0],
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ClearAllForwardings=yes",
            *ssh[1:],
            shlex.join(arguments),
        ]
    return arguments


def agent_call(endpoint, operation, payload=None):
    result = subprocess.run(
        agent_command(endpoint, operation),
        input=json.dumps(payload or {}),
        capture_output=True,
        text=True,
        timeout=40,
        env=launch_environment(),
    )
    assert result.returncode == 0, "paired_cli_action_failed"
    return json.loads(result.stdout)


def accepted_hook(stderr, event):
    statuses = []
    for line in stderr.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("component") == "agent_hook":
            statuses.append(value)
    assert len(statuses) == 1, "hook_status_missing_or_ambiguous"
    status = statuses[0]
    assert status.get("event") == event and status.get("capture") == "accepted", (
        "native_visible_message_not_accepted"
    )
    assert not any(key == "error" or key.endswith("_error") for key in status), (
        "native_hook_reported_error"
    )
    expected_recall = "retrieved" if event == "UserPromptSubmit" else "not_requested"
    assert status.get("recall") == expected_recall, "native_recall_state_invalid"
    return status


def observed_hooks(root, count):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        path = root / "hook-events.jsonl"
        if path.exists():
            values = [json.loads(line) for line in path.read_text().splitlines()]
            if len(values) >= count:
                assert all(row["returncode"] == 0 for row in values), "hook_process_failed"
                for row in values:
                    accepted_hook(row["stderr"], row["input"]["hook_event_name"])
                return values
        time.sleep(0.05)
    raise AssertionError("native_hook_completion_missing")


def recent(endpoint):
    reply = agent_call(endpoint, "recent", {"limit": 10})
    assert reply["content_trust"] == "untrusted_history", "history_trust_missing"
    return reply["items"]


def session_rows(rows, session_id):
    return [row for row in rows if row.get("session_id") == session_id]


def exercise(root, first, second, executable):
    project = root / "project"
    project.mkdir()
    (root / "config").mkdir()
    hook_command = agent_command(first, "claude-hook") + ["--workspace", str(project)]
    (root / "hook-command.json").write_text(json.dumps(hook_command))
    (root / "observer.py").write_text(OBSERVER)
    observer = shlex.join([sys.executable, str(root / "observer.py")])
    (root / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [{"hooks": [{"type": "command", "command": observer, "timeout": 20}]}]
                    for event in ("UserPromptSubmit", "Stop")
                }
            }
        )
    )
    model = ModelStub("synthetic-" + uuid4().hex)
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    session = None
    checks = []
    nonce = uuid4().hex
    first_prompt = f"Synthetic visible turn {nonce}: the initial design is blue."
    second_prompt = f"Continue synthetic task {nonce} from the latest shared context."
    update = f"CMH_VISIBLE_HOOK_REMOTE_{uuid4().hex}: the design is now green."
    assert update not in first_prompt and update not in second_prompt
    try:
        session = NativeSession(root, executable, model)
        first_turn = session.turn(first_prompt)
        hooks = observed_hooks(root, 2)
        assert [h["input"]["hook_event_name"] for h in hooks] == ["UserPromptSubmit", "Stop"]
        assert hooks[0]["input"]["prompt_id"] == hooks[1]["input"]["prompt_id"]
        assert len(model.requests) == 1, "unexpected_model_requests"
        rows = recent(second)
        first_records = [row for row in rows if first_prompt in row["content"]]
        assert len(first_records) == 1, "remote_user_turn_missing_or_duplicated"
        conversation = first_records[0]["session_id"]
        captured = session_rows(rows, conversation)
        assert len(captured) == 2 and any(
            "CMH_SYNTHETIC_REPLY_1" in row["content"] for row in captured
        ), "remote_assistant_turn_missing"
        checks.append("native_user_and_final_assistant_visible_on_peer")

        saved = agent_call(second, "remember", {"text": update, "request_id": str(uuid4())})
        assert saved["status"] == "accepted", "peer_update_not_accepted"
        started = time.monotonic()
        second_turn = session.turn(second_prompt)
        hooks = observed_hooks(root, 4)
        assert first_turn["session_id"] == second_turn["session_id"], "native_session_changed"
        assert len(model.requests) == 2, "unexpected_model_requests"
        latest = json.dumps(model.requests[1]["messages"][-1])
        assert update in latest and "cmh_memory_context" in latest, "fresh_context_not_injected"
        assert update not in json.dumps(model.requests[0]), "update_was_preseeded"
        assert hooks[2]["input"]["prompt_id"] == hooks[3]["input"]["prompt_id"]
        assert hooks[0]["input"]["prompt_id"] != hooks[2]["input"]["prompt_id"]
        checks.append("same_claude_process_next_turn_receives_peer_update")
        update_seconds = round(time.monotonic() - started, 3)

        captured = session_rows(recent(second), conversation)
        assert len(captured) == 4, "four_visible_messages_not_shared"
        assert all(update not in row["content"] for row in captured), "context_echo_recaptured"
        assert any(second_prompt in row["content"] for row in captured)
        assert any("CMH_SYNTHETIC_REPLY_2" in row["content"] for row in captured)
        checks.append("second_visible_turn_shared_without_injected_context_echo")

        replay = subprocess.run(
            hook_command,
            input=json.dumps(hooks[3]["input"]),
            capture_output=True,
            text=True,
            cwd=project,
            env=launch_environment(),
            timeout=20,
        )
        assert replay.returncode == 0, "hook_replay_failed"
        replay_status = accepted_hook(replay.stderr, "Stop")
        (root / "replay-status.json").write_text(json.dumps(replay_status))
        after = session_rows(recent(second), conversation)
        assert {row["record_id"] for row in after} == {row["record_id"] for row in captured}, (
            "hook_replay_created_another_record"
        )
        assert len(after) == 4, "hook_replay_duplicated_visible_message"
        checks.append("replayed_native_stop_keeps_same_shared_records")
        session.finish()
        return checks, update_seconds
    finally:
        if session is not None:
            session.close()
        model.shutdown()
        model.server_close()
        model_thread.join(timeout=5)


def probe(path):
    os.umask(0o077)
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="cmh-visible-hook-")).resolve()
    print(f"Private synthetic evidence: {root}", file=sys.stderr, flush=True)
    executable = shutil.which("claude")
    assert executable is not None, "claude_cli_not_installed"
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
    hub = None
    try:
        if path is None:
            hub = LiveHub(root)
            scope = hub.call(
                "/hub/v1/projects",
                method="POST",
                status=201,
                body={"display_name": "Synthetic visible hook"},
            )["project_id"]
            first, second = (
                {
                    "python": sys.executable,
                    "agent_config": str(device_config(hub, root / role, scope, role)),
                }
                for role in ("local", "remote")
            )
            same_project(agent_call(first, "status"), agent_call(second, "status"))
        else:
            value = configuration(path)
            first, second = value["local"], value["remote"]
            same_project(
                preflight(first, value["hub_origin"]), preflight(second, value["hub_origin"])
            )
        checks, update_seconds = exercise(root, first, second, executable)
        report = {
            "probe": "claude-visible-hook-v1",
            "status": "passed",
            "cli_version": version,
            "checks": checks,
            "checks_passed": len(checks),
            "same_claude_process_and_session": True,
            "second_turn_seconds": update_seconds,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "execution": "paired_https_and_ssh" if path else "two_local_fixture_identities",
            "both_client_connections_use_configured_https": path is not None,
            "physical_host_identity_verification": "external_to_probe" if path else "not_verified",
            "model": "local_text_only_test_double",
            "autonomous_model_tool_selection_verified": False,
            "automatic_model_extraction_verified": False,
            "daily_agent_configuration_changed": False,
        }
        (root / "report.json").write_text(json.dumps(report, indent=2))
        return report
    finally:
        if hub is not None:
            hub.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config-file", type=Path)
    source.add_argument("--local-fixture", action="store_true")
    options = parser.parse_args()
    try:
        print(json.dumps(probe(options.config_file), sort_keys=True))
    except Exception:
        print(json.dumps({"probe": "claude-visible-hook-v1", "status": "failed"}))
        raise SystemExit(1) from None
