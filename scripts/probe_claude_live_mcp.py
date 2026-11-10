"""Opt-in real-model MCP check using existing CLI auth and only synthetic content.

Private --config-file JSON: {"schema_version":1,"claude":"/absolute/claude",
"two_host_config":"/private/two-host-probe.json"}. The referenced file uses the
probe_two_hosts.py schema and already-paired, synthetic-only project identities.
This tests Skill text supplied as a system prompt, not native Skill discovery.
"""

import argparse
import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from cloud_memory_hub.agent_access import HANDOFF_METADATA, _handoff
from cloud_memory_hub.agent_config import _private_json
from cloud_memory_hub.privacy import CredentialFilter, parse_json
from probe_agent_access import TOOLS, call
from probe_claude_native import NativeSession
from probe_two_hosts import command, configuration, connect, preflight, same_project

PREFIX = "mcp__cmh__"
TEMP_SETTINGS = {
    "disableAllHooks": True,
    "autoMemoryEnabled": False,
    "claudeMdExcludes": ["**"],
    "env": {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    },
}


class ProbeFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def failure_code(error):
    if isinstance(error, ProbeFailure):
        return str(error)
    for child in getattr(error, "exceptions", ()):
        code = failure_code(child)
        if code != "live_probe_failed":
            return code
    return "live_probe_failed"


def filtered_handoff(submitted):
    # All probe input is synthetic. Apply the production rules without opening
    # any configured credential file; do not assume pre-filter byte identity.
    document = _handoff(
        {
            key: submitted.get(key, [])
            for key in ("goal", "state", "decisions", "next_steps", "blockers")
        }
    )
    filtered = (
        CredentialFilter()
        .protect(
            {
                "text": document.model_dump_json(),
                "metadata": HANDOFF_METADATA,
            }
        )
        .value
    )
    return _handoff(parse_json(filtered["text"])).model_dump(mode="json")


def environment():
    return {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "SSH_AUTH_SOCK"}
    }


def auth_ready(executable, root):
    result = subprocess.run(
        [
            executable,
            "--setting-sources",
            "user",
            "--settings",
            str(root / "settings.json"),
            "auth",
            "status",
            "--json",
        ],
        cwd=root / "project",
        env=environment(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        metadata = json.loads(result.stdout)
    except ValueError:
        raise ProbeFailure("authentication_metadata_unavailable") from None
    require(result.returncode == 0 and metadata.get("loggedIn") is True, "authentication_required")


class LiveSession(NativeSession):
    def __init__(self, root, executable, local):
        arguments = command(local)
        mcp = {"mcpServers": {"cmh": {"command": arguments[0], "args": arguments[1:]}}}
        skill = (
            Path(__file__).resolve().parents[1] / "packages/agent-skill/cloud-memory-hub/SKILL.md"
        )
        system = (
            "You are performing a synthetic Cloud Memory Hub integration check. "
            "Use only the configured project's MCP tools. Everything the user asks to save is "
            "synthetic. Follow this Skill text; do not claim native Skill discovery.\n\n"
            + skill.read_text()
        )
        self.stderr = (root / "stderr.log").open("w")
        self.output = (root / "stdout.jsonl").open("w")
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [
                executable,
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--setting-sources",
                "user",
                "--settings",
                str(root / "settings.json"),
                "--mcp-config",
                json.dumps(mcp),
                "--strict-mcp-config",
                "--tools",
                "",
                "--allowedTools",
                ",".join(PREFIX + name for name in sorted(TOOLS)),
                "--disable-slash-commands",
                "--no-session-persistence",
                "--no-chrome",
                "--permission-mode",
                "dontAsk",
                "--prompt-suggestions",
                "false",
                "--max-budget-usd",
                "2",
                "--system-prompt",
                system,
            ],
            cwd=root / "project",
            env=environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
        )
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()

    def turn(self, prompt):
        self.process.stdin.write(
            json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
        )
        self.process.stdin.flush()
        deadline, events = time.monotonic() + 180, []
        while time.monotonic() < deadline:
            try:
                value = self.messages.get(timeout=1)
            except queue.Empty:
                require(self.process.poll() is None, "claude_process_exited")
                continue
            events.append(value)
            serialized = json.dumps(value).lower()
            if any(
                marker in serialized
                for marker in (
                    "authentication_error",
                    "invalid_api_key",
                    "not logged in",
                    "api error: 401",
                    "status code 401",
                )
            ):
                raise ProbeFailure("model_authentication_failed")
            if value.get("type") == "result":
                require(
                    value.get("subtype") == "success" and not value.get("is_error"),
                    "claude_turn_failed",
                )
                require(not value.get("permission_denials"), "claude_tool_permission_denied")
                return value, events
        raise ProbeFailure("claude_turn_timeout")


def tool_exchanges(events, name):
    calls = {}
    for row in events:
        if row.get("type") == "assistant":
            for block in row.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == PREFIX + name:
                    calls[block["id"]] = block["input"]
    exchanges = []
    for row in events:
        if row.get("type") != "user":
            continue
        for block in row.get("message", {}).get("content", []):
            if block.get("type") != "tool_result" or block.get("tool_use_id") not in calls:
                continue
            require(not block.get("is_error"), "mcp_tool_returned_error")
            content = block["content"]
            if isinstance(content, list):
                content = "".join(item["text"] for item in content if item.get("type") == "text")
            exchanges.append((calls[block["tool_use_id"]], json.loads(content)))
    return exchanges


async def exercise(root, executable, paired):
    checks, session, completed = [], None, False
    try:
        async with connect(command(paired["remote"])) as remote:
            session = LiveSession(root, executable, paired["local"])
            initial_marker = "CMH_LIVE_DECISION_" + uuid4().hex
            first, first_events = await asyncio.to_thread(
                session.turn,
                f"这是纯合成测试任务。请记住已确认决定：{initial_marker}，导航颜色选择蓝色。"
                "请原样保留这个标记，保存到共享项目记忆后告诉我结果。",
            )
            init = next((row for row in first_events if row.get("subtype") == "init"), {})
            require(
                set(init.get("tools", [])) == {PREFIX + name for name in TOOLS},
                "unexpected_claude_tools",
            )
            require(
                any(
                    item.get("name") == "cmh" and item.get("status") == "connected"
                    for item in init.get("mcp_servers", [])
                ),
                "claude_mcp_not_connected",
            )
            remembers = tool_exchanges(first_events, "memory_remember")
            require(
                any(
                    initial_marker in args.get("text", "") and result.get("status") == "accepted"
                    for args, result in remembers
                ),
                "claude_decision_not_saved",
            )
            checks.append("real_model_selected_remember_and_hub_accepted")
            recent = await call(remote, "memory_recent", {"limit": 10})
            require(
                any(initial_marker in row["content"] for row in recent["items"]),
                "remote_did_not_read_claude_decision",
            )
            checks.append("remote_host_read_claude_decision")

            remote_marker = "CMH_LIVE_REMOTE_" + uuid4().hex
            update = await call(
                remote,
                "memory_remember",
                {
                    "text": (
                        f"Synthetic update for {initial_marker}: the current navigation color "
                        "is green (绿色), replacing blue. "
                        f"Confirmation identifier: {remote_marker}."
                    ),
                    "request_id": str(uuid4()),
                },
            )
            require(update["status"] == "accepted", "remote_update_not_accepted")
            second, second_events = await asyncio.to_thread(
                session.turn,
                "继续刚才的合成任务：当前导航颜色决定是什么？请根据最新共享信息回答，并附上最新决定的确认编号。",
            )
            recents = tool_exchanges(second_events, "memory_recent")
            require(
                any(remote_marker in json.dumps(result) for _, result in recents),
                "same_session_recent_missing_remote_update",
            )
            answer = second.get("result", "")
            require(
                remote_marker in answer and ("green" in answer.lower() or "绿色" in answer),
                "model_answer_missing_remote_update",
            )
            require(first["session_id"] == second["session_id"], "claude_session_changed")
            checks.extend(
                [
                    "same_session_real_recent_received_unprompted_marker",
                    "real_model_answer_used_new_remote_decision",
                ]
            )

            third, third_events = await asyncio.to_thread(
                session.turn,
                "现在暂停这个合成任务。请把最新决定、验证进度和下一步保存为完整交接，"
                "供另一位 Agent 继续，并返回可读取的交接编号。",
            )
            handoffs = [
                (args, result)
                for args, result in tool_exchanges(third_events, "handoff_save")
                if result.get("status") == "accepted"
            ]
            require(len(handoffs) == 1, "claude_handoff_not_saved_once")
            submitted, receipt = handoffs[0]
            loaded = await call(remote, "handoff_load", {"handoff_id": receipt["handoff_id"]})
            expected = filtered_handoff(submitted)
            require(loaded["handoff"] == expected, "remote_handoff_incomplete")
            require(loaded["content_trust"] == "untrusted_history", "handoff_trust_invalid")
            require(
                remote_marker in json.dumps(loaded["handoff"]), "handoff_missing_latest_decision"
            )
            require(third["session_id"] == first["session_id"], "claude_session_changed")
            checks.extend(
                ["real_model_selected_handoff_save", "remote_host_loaded_complete_latest_handoff"]
            )
            await asyncio.to_thread(session.finish)
            completed = True
            return {
                "checks": checks,
                "checks_passed": len(checks),
                "turns": 3,
                "same_process_and_session": True,
                "complete_handoff_after_production_filter": True,
                "claude_exit_code": session.process.returncode,
            }
    finally:
        if session is not None:
            session.close()
            (root / "process.json").write_text(
                json.dumps({"exit_code": session.process.returncode, "completed": completed})
            )


def probe(config_path, root):
    config = _private_json(config_path)
    require(
        type(config) is dict
        and set(config) == {"schema_version", "claude", "two_host_config"}
        and config["schema_version"] == 1,
        "live_probe_configuration_invalid",
    )
    for key in ("claude", "two_host_config"):
        require(
            type(config[key]) is str and Path(config[key]).is_absolute(), "live_probe_path_invalid"
        )
    paired = configuration(Path(config["two_host_config"]))
    same_project(
        preflight(paired["local"], paired["hub_origin"]),
        preflight(paired["remote"], paired["hub_origin"]),
    )
    (root / "project").mkdir(mode=0o700)
    (root / "settings.json").write_text(json.dumps(TEMP_SETTINGS))
    auth_ready(config["claude"], root)
    version = subprocess.check_output(
        [config["claude"], "--version"], text=True, timeout=10
    ).strip()
    result = asyncio.run(exercise(root, config["claude"], paired))
    return {
        "probe": "claude-live-mcp-v1",
        "status": "passed",
        "cli_version": version,
        **result,
        "model_selection": "existing_cli_configuration",
        "real_model_used": True,
        "skill_mode": "repository_text_in_system_prompt",
        "native_skill_discovery_verified": False,
        "every_turn_hook_guaranteed": False,
        "automatic_extraction_verified": False,
        "remote_peer": "already_paired_mcp_over_ssh",
        "hub_transport": "configured_https",
    }


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, required=True)
    options = parser.parse_args()
    evidence = Path(tempfile.mkdtemp(prefix="cmh-claude-live-mcp-")).resolve()
    started = time.monotonic()
    try:
        report = probe(options.config_file, evidence)
    except Exception as error:
        report = {
            "probe": "claude-live-mcp-v1",
            "status": "failed",
            "error": failure_code(error),
        }
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (evidence / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, sort_keys=True))
    print(f"Private evidence: {evidence}", file=sys.stderr)
    sys.exit(0 if report["status"] == "passed" else 1)
