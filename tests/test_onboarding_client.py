"""Standalone installer contract: standard library only, no Hub/Core dependency."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import tarfile
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps/hub/src/cloud_memory_hub/onboarding/connect.py"
SPEC = importlib.util.spec_from_file_location("cmh_standalone_connect", SCRIPT)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)

ORIGIN = "https://hub.example.invalid"
PAIRING_ID = str(uuid.uuid4())
INVITATION = "i" * 43
LINK = f"{ORIGIN}/hub/connect#pairing_id={PAIRING_ID}&secret={INVITATION}"
BUNDLE = {
    "schema_version": 1,
    "kind": "cloud-memory-hub-pairing",
    "origin": ORIGIN,
    "instance_id": str(uuid.uuid4()),
    "device_id": str(uuid.uuid4()),
    "scope_id": str(uuid.uuid4()),
    "source_id": str(uuid.uuid4()),
}


class FakeRemote:
    """Protocol-shaped synthetic remote, never a host/model integration claim."""

    def __init__(self):
        self.calls = []
        self.claim_secrets = []
        self.lose_claim = False
        self.failure = None
        self.transform = lambda stage, value: value
        self.pending_check = lambda: None

    def __call__(self, origin):
        assert origin == ORIGIN
        return self

    def request(self, path, *, payload=None, token=None, notification=False):
        self.calls.append((path, copy.deepcopy(payload)))
        if self.failure:
            raise client.ConnectError(self.failure)
        if path.endswith("/claim"):
            self.pending_check()
            self.claim_secrets.append(payload["claimant_secret"])
            if self.lose_claim:
                self.lose_claim = False
                raise client.ConnectError("network_unavailable")
            return self.transform("claim", copy.deepcopy(BUNDLE))
        assert token == "cmhdev_v1_" + BUNDLE["device_id"] + "_" + self.claim_secrets[0]
        if path.endswith("/session"):
            value = {
                "protocol_version": 1,
                "instance_id": BUNDLE["instance_id"],
                "features": ["device_identity", "event_upload", "source_preflight"],
                "device": {
                    "device_id": BUNDLE["device_id"],
                    "status": "active",
                    "grants": [
                        {
                            "scope_id": BUNDLE["scope_id"],
                            "permissions": ["events:write", "memory:read", "history:read"],
                        }
                    ],
                },
            }
            return self.transform("session", value)
        if path.endswith("/access"):
            value = {
                key: BUNDLE[key] for key in ("instance_id", "device_id", "scope_id", "source_id")
            }
            value.update(protocol_version=1, allowed=True)
            return self.transform("source", value)
        if notification:
            return None
        method = payload["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": name} for name in client.TOOL_NAMES]}
        elif method == "tools/call":
            assert payload["params"] == {"name": "memory_status", "arguments": {}}
            status = {
                key: BUNDLE[key] for key in ("instance_id", "device_id", "scope_id", "source_id")
            }
            status.update(
                status="connected", transport="streamable_http", client_offline_queue=False
            )
            result = {"isError": False, "structuredContent": self.transform("status", status)}
        else:
            raise AssertionError(method)
        return self.transform(method, {"jsonrpc": "2.0", "id": payload["id"], "result": result})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Resolve the platform's temp-root alias before asserting no links below it.
        self.home = Path(self.temp.name).resolve() / "home with spaces"
        self.home.mkdir(mode=0o700)
        self.remote = FakeRemote()

    def connect(self, agent="claude"):
        return client.connect(LINK, agent, home=self.home, remote_factory=self.remote)

    def pending(self):
        paths = list((self.home / ".cloud-memory-hub/remote").glob("*/pending.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    def claude_path(self):
        return self.home / ".claude/.claude.json"

    @unittest.skipUnless(os.name == "nt", "Windows DACL requires a real Windows filesystem")
    def test_windows_acl_empty_directory_and_file_roundtrip(self):
        """Expose safe diagnostics only for this credential-free Windows fixture."""
        directory = self.home / "synthetic acl directory"
        directory.mkdir()
        target = directory / "synthetic-empty.txt"
        target.touch()
        actual_run = client.subprocess.run
        captured = []

        def capture(*args, **kwargs):
            result = actual_run(*args, **kwargs)
            captured.append(result)
            return result

        try:
            with patch.object(client.subprocess, "run", side_effect=capture):
                client.windows_acl(directory, apply=True)
                client.windows_acl(directory)
                client.windows_acl(target, apply=True)
                client.windows_acl(target)
                # Idempotent reapplication must retain the protected current-user DACL.
                client.windows_acl(directory, apply=True)
                client.windows_acl(target, apply=True)
        except client.ConnectError:
            result = captured[-1] if captured else None
            raw = (result.stderr if result else b"").decode("utf-8", errors="replace")
            if "<Objs" in raw:
                try:
                    document = ET.fromstring(raw[raw.index("<Objs") :])
                    raw = "\n".join(
                        element.text or ""
                        for element in document.iter()
                        if element.tag.rsplit("}", 1)[-1] == "S"
                        and element.attrib.get("S") == "Error"
                    )
                except ET.ParseError:
                    raw = "unparseable_powershell_diagnostic"
            raw = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda match: chr(int(match[1], 16)), raw)
            raw = raw.replace(str(directory), "<synthetic-directory>").replace(
                str(self.home), "<synthetic-home>"
            )
            raw = re.sub(r"S-\d+(?:-\d+)+", "<sid>", raw)
            returncode = result.returncode if result else "not_started"
            self.fail(f"Synthetic empty-path ACL failed: exit={returncode}; {raw[:2400]}")

    def test_stdlib_import_and_skill_template(self):
        expected = (ROOT / "packages/agent-skill/cloud-memory-hub-remote/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(client.REMOTE_SKILL, expected)
        self.assertNotIn("cloud_memory_hub.", SCRIPT.read_text(encoding="utf-8"))

    def test_claim_persisted_before_network_and_private(self):
        def check():
            path, state = self.pending()
            self.assertRegex(state["claimant_secret"], client.SECRET)
            self.assertNotIn(INVITATION, path.read_text(encoding="utf-8"))
            client.check_private(path)

        self.remote.pending_check = check
        result = self.connect()
        self.assertTrue(result["remote_verified"])
        self.assertTrue(result["host_reload_required"])
        client.check_private(self.claude_path())
        self.assertTrue(
            any(p.name == "SKILL.md" for p in (self.home / ".claude/skills").rglob("*"))
        )

    def test_pending_disk_failure_happens_before_claim(self):
        with patch.object(client, "atomic_write", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.connect()
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.claude_path().exists())

    def test_automation_reuses_pairing_and_selected_network_without_token_in_arguments(self):
        project = self.home / "project with spaces"
        project.mkdir()
        executable = self.home / "stable runtime/cloud-memory-setup"
        self.remote.network_status = {"selected": "environment", "action_required": False}
        handed_off = []

        def run(command, **options):
            if command[-1] == "--help":
                self.assertNotIn("input", options)
                return client.subprocess.CompletedProcess(command, 0, stdout=b"")
            self.assertEqual(
                command,
                [str(executable), "automation", "--agent", "codex", "--workspace", str(project)],
            )
            self.assertEqual(options["cwd"], project)
            pairing = json.loads(options["input"])
            self.assertEqual({key: pairing[key] for key in BUNDLE}, BUNDLE)
            self.assertEqual(pairing["network_mode"], "environment")
            self.assertNotIn(pairing["credential"], " ".join(command))
            handed_off.append(pairing)
            return client.subprocess.CompletedProcess(
                command, 0, stdout=b'{"status":"configured","background_delivery":"running"}'
            )

        with (
            patch.object(client, "installed_automation_setup", return_value=executable),
            patch.object(client.subprocess, "run", side_effect=run),
        ):
            for _ in range(2):
                result = client.connect(
                    LINK, "codex", home=self.home, remote_factory=self.remote, workspace=project
                )
                self.assertEqual(
                    result["automation"],
                    {
                        "status": "configured",
                        "background_delivery": "running",
                        "host_review_required": True,
                        "host_verified": False,
                        "workspace": str(project),
                    },
                )
        self.assertEqual(len(self.remote.claim_secrets), 1)
        self.assertEqual(handed_off, [handed_off[0]] * 2)

    def test_legacy_connection_without_explicit_workspace_never_installs_capture(self):
        with patch.object(client, "installed_automation_setup") as runtime:
            result = client.connect(LINK, "claude", home=self.home, remote_factory=self.remote)
        runtime.assert_not_called()
        self.assertEqual(result["automation"]["status"], "automation_workspace_required")

    def test_home_cannot_become_an_automation_workspace(self):
        with patch.object(client, "installed_automation_setup") as runtime:
            result = client.connect(
                LINK, "claude", home=self.home, remote_factory=self.remote, workspace=self.home
            )
        runtime.assert_not_called()
        self.assertEqual(result["automation"], {"status": "automation_workspace_required"})

    @unittest.skipIf(os.name == "nt", "Mac release discovery uses POSIX ownership")
    def test_automation_prepares_only_the_verified_matching_release(self):
        tag, version = "v1.2.3", "1.2.3"
        prefix = f"cloud-memory-hub-client-{tag}-macos-arm64"
        filename = prefix + ".tar.gz"
        local_archive = self.home / filename
        installer = b"raise SystemExit(0)\n"
        manifest = {
            "kind": "cloud-memory-hub-mac-client",
            "release": version,
            "architecture": "arm64",
            "files": {"install.py": client.digest(installer)},
        }
        with tarfile.open(local_archive, "w:gz") as archive:
            for name, content in (
                ("manifest.json", client.json_bytes(manifest)),
                ("install.py", installer),
            ):
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        data = local_archive.read_bytes()
        checksum = client.digest(data)
        target = self.home / client.CLIENT_RELEASE_ROOT / f"{version}-arm64"
        command = target / "venv/bin/cloud-memory-setup"
        fetched = []

        def download(_opener, url, destination, _limit):
            fetched.append(url)
            base = f"{client.CLIENT_RELEASE_ORIGIN}/{tag}/"
            if url == base + "SHA256SUMS":
                destination.write_text(f"{expected}  {filename}\n")
            else:
                self.assertEqual(url, base + filename)
                destination.write_bytes(data)
            return checksum

        python = self.home / "stable python/python3.12"

        def run(args, **options):
            self.assertEqual(args[:2], [str(python), "-I"])
            self.assertEqual(args[3:], ["--target", str(target)])
            self.assertEqual(Path(args[2]).read_bytes(), installer)
            self.assertEqual(options["cwd"].name, prefix)
            self.assertEqual(options["stdin"], client.subprocess.DEVNULL)
            command.parent.mkdir(parents=True, mode=0o700)
            command.write_text("#!/bin/sh\nexit 0\n")
            command.chmod(0o700)
            marker = target / "installation.json"
            marker.write_bytes(
                client.json_bytes(
                    {
                        "schema_version": 1,
                        "release": version,
                        "architecture": "arm64",
                        "distribution_sha256": client.digest(client.json_bytes(manifest)),
                    }
                )
            )
            marker.chmod(0o600)
            return client.subprocess.CompletedProcess(args, 0)

        with (
            patch.object(client, "CLIENT_RELEASE_TAG", tag),
            patch.object(client.sys, "platform", "darwin"),
            patch.object(client.platform, "machine", return_value="arm64"),
            patch.object(client, "stable_automation_python", return_value=python),
            patch.object(client, "download_client_asset", side_effect=download),
            patch.object(client.subprocess, "run", side_effect=run) as install,
        ):
            expected = "0" * 64
            with self.assertRaisesRegex(
                client.ConnectError, "automation_release_checksum_mismatch"
            ):
                client.prepare_automation_setup(self.home, {})
            install.assert_not_called()
            self.assertFalse(target.exists())
            expected = checksum
            self.assertEqual(client.prepare_automation_setup(self.home, {}), command)
            self.assertEqual(client.installed_automation_setup(self.home), command)
            self.assertEqual(len(fetched), 4)
            install.assert_called_once()

    def test_automation_archive_rejects_paths_and_links(self):
        prefix = "synthetic-client"
        for name, kind in (
            (f"{prefix}/../outside", tarfile.REGTYPE),
            (f"{prefix}/link", tarfile.SYMTYPE),
        ):
            with self.subTest(name=name):
                archive_path = self.home / "client.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "../outside"
                    archive.addfile(info)
                with self.assertRaisesRegex(
                    client.ConnectError, "automation_release_archive_unsafe"
                ):
                    client.unpack_client_release(archive_path, self.home, prefix)
                self.assertFalse((self.home / "outside").exists())

    def test_automation_archive_bounds_decompression_before_extraction(self):
        archive_path = self.home / "client.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            content = b"x" * 1024
            info = tarfile.TarInfo("synthetic-client/file")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        with (
            patch.object(client, "CLIENT_EXPANDED_LIMIT", 32),
            self.assertRaisesRegex(client.ConnectError, "automation_release_archive_limit"),
        ):
            client.unpack_client_release(archive_path, self.home, "synthetic-client")
        self.assertFalse((self.home / "synthetic-client").exists())

    def test_automation_release_redirects_remain_on_github_https(self):
        client.require_client_asset_url(
            "https://release-assets.githubusercontent.com/asset?sig=synthetic"
        )
        for url in (
            "http://github.com/file",
            "https://github.com.example.invalid/file",
            "https://github.com@untrusted.example.invalid/file",
            "https://github.com:444/file",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(client.ConnectError, "automation_release_redirect_rejected"),
            ):
                client.require_client_asset_url(url)

    def test_lost_claim_response_retries_same_secret(self):
        self.remote.lose_claim = True
        with self.assertRaisesRegex(client.ConnectError, "network_unavailable"):
            self.connect()
        self.assertFalse(self.claude_path().exists())
        self.connect()
        self.assertEqual(len(self.remote.claim_secrets), 2)
        self.assertEqual(*self.remote.claim_secrets)

    def test_idempotent_completed_install_no_new_claim_or_config_rewrite(self):
        self.connect()
        before = self.claude_path().stat().st_mtime_ns
        self.connect()
        self.assertEqual(before, self.claude_path().stat().st_mtime_ns)
        self.assertEqual(len(self.remote.claim_secrets), 1)

    def test_other_claude_configuration_preserved_and_backed_up(self):
        self.claude_path().parent.mkdir()
        original = b'{"mcpServers":{"other":{"command":"old"}},"custom":false}\n'
        self.claude_path().write_bytes(original)
        result = self.connect()
        actual = json.loads(self.claude_path().read_text(encoding="utf-8"))
        actual["mcpServers"].pop(result["server"])
        self.assertEqual(actual, json.loads(original))
        path, state = self.pending()
        first = state["plans"]["claude"][0]
        self.assertEqual((path.parent / first["backup"]).read_bytes(), original)
        client.check_private(path.parent / first["backup"])

    def test_codex_append_preserves_existing_toml_semantics(self):
        path = self.home / ".codex/config.toml"
        path.parent.mkdir()
        original = (
            b'# retained comment\nmodel = "synthetic"\n[mcp_servers.other]\ncommand = "other"\n'
        )
        path.write_bytes(original)
        result = self.connect("codex")
        actual = client.tomllib.loads(path.read_text(encoding="utf-8"))
        added = actual["mcp_servers"].pop(result["server"])
        self.assertTrue(added["http_headers"]["Authorization"].startswith("Bearer cmhdev_v1_"))
        self.assertEqual(actual, client.tomllib.loads(original.decode()))
        self.assertTrue(path.read_bytes().startswith(original))

    def test_generic_is_private_and_does_not_touch_agent_paths(self):
        result = self.connect("generic")
        path = Path(result["private_state"]) / "mcp.json"
        client.check_private(path)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_configuration_edit_after_install_is_conflict_and_retained(self):
        self.connect()
        self.claude_path().write_text('{"changed":"by user"}')
        with self.assertRaisesRegex(client.ConnectError, "managed_configuration_conflict"):
            self.connect()
        self.assertEqual(self.claude_path().read_text(encoding="utf-8"), '{"changed":"by user"}')

    def test_existing_same_name_not_adopted(self):
        self.claude_path().parent.mkdir()
        name = "cloud_memory_hub_" + BUNDLE["source_id"].replace("-", "")[:16]
        content = json.dumps({"mcpServers": {name: {"url": "https://other.invalid"}}})
        self.claude_path().write_text(content)
        with self.assertRaisesRegex(client.ConnectError, "managed_configuration_conflict"):
            self.connect()
        self.assertEqual(self.claude_path().read_text(encoding="utf-8"), content)

    def test_durable_plan_recovers_partial_write_without_replacing_identity(self):
        real_write = client.atomic_write

        def fail_skill(path, data):
            if path.name == "SKILL.md":
                raise OSError("synthetic disk failure")
            real_write(path, data)

        with patch.object(client, "atomic_write", side_effect=fail_skill):
            with self.assertRaises(OSError):
                self.connect()
        self.assertTrue(self.claude_path().exists())
        self.connect()
        self.assertEqual(len(self.remote.claim_secrets), 1)

    def test_config_edit_during_planning_is_not_adopted_as_baseline(self):
        self.claude_path().parent.mkdir()
        self.claude_path().write_text('{"model":"old"}')
        real_plan = client.Installation.plan

        def race(installation, agent, files):
            self.claude_path().write_text('{"model":"new"}')
            return real_plan(installation, agent, files)

        with patch.object(client.Installation, "plan", race):
            with self.assertRaisesRegex(client.ConnectError, "managed_configuration_conflict"):
                self.connect()
        self.assertEqual(
            json.loads(self.claude_path().read_text(encoding="utf-8")), {"model": "new"}
        )
        self.assertFalse((self.home / ".claude/skills").exists())
        _, state = self.pending()
        self.assertEqual(state["plans"], {})

    def test_dotenv_edit_during_planning_prevents_all_live_writes(self):
        directory = self.home / ".hermes"
        directory.mkdir()
        config = directory / "config.yaml"
        config.write_bytes(b"model: old\n")
        envfile = directory / ".env"
        envfile.write_bytes(b"SYNTHETIC=old\n")
        real_plan = client.Installation.plan

        def race(installation, agent, files):
            envfile.write_bytes(b"SYNTHETIC=new\n")
            return real_plan(installation, agent, files)

        with (
            patch.object(client, "staged_host_config", return_value=b"model: proposed\n"),
            patch.object(client.Installation, "plan", race),
        ):
            with self.assertRaisesRegex(client.ConnectError, "managed_configuration_conflict"):
                self.connect("hermes")
        self.assertEqual(envfile.read_bytes(), b"SYNTHETIC=new\n")
        self.assertEqual(config.read_bytes(), b"model: old\n")
        self.assertFalse((directory / "skills").exists())

    def test_skill_created_during_planning_is_not_overwritten(self):
        real_plan = client.Installation.plan
        created = []

        def race(installation, agent, files):
            path = next(path for path, _, _ in files if path.name == "SKILL.md")
            path.parent.mkdir(parents=True)
            path.write_text("external skill")
            created.append(path)
            return real_plan(installation, agent, files)

        with patch.object(client.Installation, "plan", race):
            with self.assertRaisesRegex(client.ConnectError, "managed_configuration_conflict"):
                self.connect()
        self.assertFalse(self.claude_path().exists())
        self.assertEqual(created[0].read_text(encoding="utf-8"), "external skill")

    def test_changed_opencode_credential_file_blocks_rerun(self):
        with patch.object(client, "staged_host_config", return_value=b"{}"):
            result = self.connect("opencode")
        header = Path(result["private_state"]) / "authorization.header"
        header.write_text("synthetic changed header")
        with self.assertRaisesRegex(client.ConnectError, "managed_configuration_conflict"):
            self.connect("opencode")
        self.assertEqual(header.read_text(encoding="utf-8"), "synthetic changed header")

    def test_pending_plan_cannot_redirect_staged_file(self):
        self.connect()
        path, state = self.pending()
        state["plans"]["claude"][0]["staged"] = "../foreign"
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(client.ConnectError, "pending_state_invalid"):
            self.connect()

    def test_mcp_text_result_is_supported_without_weakening_binding(self):
        def text_result(stage, value):
            if stage == "tools/call":
                status = value["result"].pop("structuredContent")
                value["result"]["content"] = [{"type": "text", "text": json.dumps(status)}]
            return value

        self.remote.transform = text_result
        self.connect()

    def test_errors_offer_distinct_recovery_and_generic_is_not_host_success(self):
        self.assertIn("Tailscale", client.recovery_message("network_unavailable"))
        self.assertIn("生成新连接", client.recovery_message("pairing_expired"))
        self.assertNotIn("重试原链接", client.recovery_message("pairing_expired"))
        self.assertIn("备份", client.recovery_message("managed_configuration_conflict"))
        self.assertIn("不需要 sudo", client.recovery_message("private_acl_unavailable"))
        result = {"agent": "generic", "server": "synthetic", "private_state": "/synthetic"}
        output = io.StringIO()
        with (
            patch.object(client, "connect", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(client.main(["--agent", "generic", "--link", LINK]), 0)
        self.assertIn("仍需将它添加", output.getvalue())
        self.assertNotIn("已配置 generic", output.getvalue())

    def test_changed_invitation_same_id_rejected(self):
        self.connect()
        with self.assertRaisesRegex(client.ConnectError, "pending_state_invalid"):
            client.connect(
                LINK.replace(INVITATION, "z" * 43),
                "claude",
                home=self.home,
                remote_factory=self.remote,
            )

    def test_network_action_stops_before_claim_or_host_config_and_retry_preserves_identity(self):
        self.remote.network_status = {
            "requested": "auto",
            "selected": "direct",
            "hostname": "hub.example.invalid",
            "environment_available": False,
            "direct_available": True,
            "action_required": True,
            "health_verified": True,
        }
        result = self.connect()
        self.assertEqual(result["status"], "network_action_required")
        self.assertFalse(result["remote_verified"])
        self.assertFalse(result["host_connection_verified"])
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.claude_path().exists())
        path, before = self.pending()
        self.assertNotIn("bundle", before)
        self.remote.network_status["action_required"] = False
        self.remote.network_status["environment_available"] = True
        self.remote.network_status["selected"] = "environment"
        self.assertEqual(self.connect()["status"], "configured")
        after = json.loads(path.read_text())
        self.assertEqual(before["claimant_secret"], after["claimant_secret"])
        self.assertEqual(self.remote.claim_secrets, [before["claimant_secret"]])

    def test_claimed_identity_and_pending_plan_survive_later_network_action(self):
        self.connect()
        path, before = self.pending()
        self.remote.network_status = {"action_required": True, "health_verified": True}
        calls = list(self.remote.calls)
        self.assertEqual(self.connect()["status"], "network_action_required")
        self.assertEqual(json.loads(path.read_text()), before)
        self.assertEqual(self.remote.calls, calls)

    def test_network_action_cli_never_says_connection_complete(self):
        output, errors = io.StringIO(), io.StringIO()
        pending = {"status": "network_action_required", "agent": "codex"}
        with (
            patch.object(client, "connect", return_value=pending),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(client.main(["--agent", "codex", "--link", LINK]), 2)
        self.assertNotIn("已配置", output.getvalue() + errors.getvalue())
        self.assertIn("DOMAIN,hub.example.invalid,DIRECT", errors.getvalue())
        self.assertIn("NO_PROXY", errors.getvalue())
        self.assertIn("图形客户端", errors.getvalue())
        self.assertIn("/mcp", errors.getvalue())
        self.assertIn("memory_status", errors.getvalue())
        self.assertNotIn(INVITATION, output.getvalue() + errors.getvalue())

    def test_unknown_response_fields_or_foreign_origin_rejected(self):
        for mutation in (
            {"credential": "must-not-be-returned"},
            {"origin": "https://foreign.invalid"},
            {"schema_version": True},
        ):
            with self.subTest(mutation=mutation):
                value = dict(BUNDLE, **mutation)
                with self.assertRaises(client.ConnectError):
                    client.validate_bundle(value, ORIGIN)

    def test_session_scope_source_and_mcp_binding_fail_closed(self):
        for stage, mutation in (
            ("session", {"instance_id": str(uuid.uuid4())}),
            ("source", {"allowed": False}),
            ("source", {"source_id": str(uuid.uuid4())}),
            ("status", {"device_id": str(uuid.uuid4())}),
            ("status", {"client_offline_queue": True}),
            ("tools/list", {"error": {"message": "unsafe raw text"}}),
        ):
            with self.subTest(stage=stage, mutation=mutation):
                self.remote.transform = lambda actual, value, mutation=mutation, stage=stage: (
                    dict(value, **mutation) if actual == stage else value
                )
                with self.assertRaises(client.ConnectError):
                    self.connect()
                self.assertFalse(self.claude_path().exists())

    def test_only_claim_and_read_only_mcp_status_are_called(self):
        self.connect()
        tools = [
            body["params"]["name"]
            for _, body in self.remote.calls
            if body and body.get("method") == "tools/call"
        ]
        self.assertEqual(tools, ["memory_status"])
        self.assertFalse(any(path.endswith("/events") for path, _ in self.remote.calls))

    def test_symlink_target_refused(self):
        target = self.home / "other"
        target.write_text("original")
        self.claude_path().parent.mkdir()
        try:
            self.claude_path().symlink_to(target)
        except OSError:
            self.skipTest("Host does not permit symlinks; Windows ACL tests still execute")
        with self.assertRaisesRegex(client.ConnectError, "unsafe_path"):
            self.connect()
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    @unittest.skipIf(os.name == "nt", "POSIX permission case; Windows uses ACL")
    def test_world_readable_pending_rejected(self):
        self.connect()
        path, _ = self.pending()
        path.chmod(0o644)
        with self.assertRaisesRegex(client.ConnectError, "private_permissions_required"):
            self.connect()

    def test_json_duplicate_keys_refused(self):
        with self.assertRaisesRegex(client.ConnectError, "invalid_json"):
            client.parse_json(b'{"x":1,"x":2}')

    def test_bad_link_and_nonloopback_http_rejected(self):
        for link in (
            LINK.replace("https://", "http://"),
            LINK.replace("#", "?"),
            LINK + "&secret=" + INVITATION,
            LINK.replace("/hub/connect", "/else"),
            LINK.replace("hub.example.invalid", "user@hub.example.invalid"),
        ):
            with self.subTest(link=link):
                with self.assertRaises(client.ConnectError):
                    client.parse_link(link)
        self.assertEqual(
            client.parse_link(LINK.replace(ORIGIN, "http://127.0.0.1:1234"))[0],
            "http://127.0.0.1:1234",
        )

    def test_default_environment_not_overridden(self):
        with patch.dict(
            os.environ, {"CODEX_HOME": "/synthetic/codex", "CLAUDE_CONFIG_DIR": "/synthetic/claude"}
        ):
            env = client.agent_environment()
            self.assertEqual(env["CODEX_HOME"], "/synthetic/codex")
            isolated = client.agent_environment(self.home)
            self.assertEqual(isolated["CODEX_HOME"], str(self.home / ".codex"))

    def test_jsonc_is_selected_without_parsing_or_mutating(self):
        directory = self.home / ".config/opencode"
        directory.mkdir(parents=True)
        path = directory / "opencode.jsonc"
        path.write_text('{ /* keep */ "mcp": {} }')
        selected, _ = client.paths_for("opencode", self.home, client.agent_environment(self.home))
        self.assertEqual(selected, path)
        self.assertIn("keep", path.read_text(encoding="utf-8"))

    def test_custom_opencode_override_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(client.ConnectError, "opencode_custom_config_requires_review"):
            client.paths_for("opencode", self.home, {"OPENCODE_CONFIG": "/synthetic/custom.jsonc"})

    def test_hermes_profile_respects_explicit_home_without_guessing(self):
        selected = self.home / "selected-profile"
        config, _ = client.paths_for(
            "hermes", self.home, {"HERMES_PROFILE": "synthetic", "HERMES_HOME": str(selected)}
        )
        self.assertEqual(config, selected / "config.yaml")
        with self.assertRaisesRegex(client.ConnectError, "hermes_profile_requires_review"):
            client.paths_for("hermes", self.home, {"HERMES_PROFILE": "synthetic"})

    def test_redirect_refused(self):
        with self.assertRaisesRegex(client.ConnectError, "redirect_refused"):
            client.NoRedirect().redirect_request(None, None, 302, "", {}, "https://else.invalid")

    def test_main_never_prints_token_or_invitation(self):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(client, "connect", side_effect=client.ConnectError("network_unavailable")),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(client.main(["--agent", "claude", "--link", LINK]), 1)
        self.assertNotIn(INVITATION, output.getvalue() + errors.getvalue())
        self.assertIn("network_unavailable", errors.getvalue())

    def test_windows_acl_command_never_interpolates_path(self):
        path = Path('synthetic " $(bad) secret path')
        completed = client.subprocess.CompletedProcess([], 0)
        with patch.object(client.subprocess, "run", return_value=completed) as run:
            client.windows_acl(path, apply=True)
        args, kwargs = run.call_args
        self.assertNotIn(str(path), " ".join(args[0]))
        self.assertEqual(kwargs["env"]["CMH_ACL_TARGET"], str(path))

    def test_windows_acl_rebuilds_child_module_path_without_mutating_parent(self):
        completed = client.subprocess.CompletedProcess([], 0)
        for name in ("PSModulePath", "pSmOdUlEpAtH"):
            with (
                self.subTest(name=name),
                patch.dict(
                    os.environ,
                    {name: "synthetic-incompatible-ps7-modules", "OTHER_SETTING": "preserved"},
                    clear=True,
                ),
            ):
                original = dict(os.environ)
                with patch.object(client.subprocess, "run", return_value=completed) as run:
                    client.windows_acl(self.home, apply=True)
                child_environment = run.call_args.kwargs["env"]
                self.assertFalse(any(key.upper() == "PSMODULEPATH" for key in child_environment))
                self.assertEqual(child_environment["OTHER_SETTING"], "preserved")
                self.assertEqual(child_environment["CMH_ACL_APPLY"], "yes")
                self.assertEqual(dict(os.environ), original)

    def test_failed_acl_refuses_file(self):
        completed = client.subprocess.CompletedProcess([], 1)
        with patch.object(client.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(client.ConnectError, "private_acl_unavailable"):
                client.windows_acl(self.home)


if __name__ == "__main__":
    unittest.main()
