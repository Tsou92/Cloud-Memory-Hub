import contextlib
import importlib.util
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

FILE = Path(__file__).resolve().parents[1] / "scripts/install_hub.py"
SPEC = importlib.util.spec_from_file_location("cmh_install_hub", FILE)
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)
ORIGIN = "https://hub.example.invalid"


class FirstInstallTests(unittest.TestCase):
    def setUp(self):
        self.connector = install.load_connector()
        self.args = SimpleNamespace(
            nas="nas-alias",
            ssh_port=None,
            tag="v0.1.4",
            install_dir="Cloud Hub",
            compose_project="cloud-memory-hub",
            port=8800,
            serve_port=443,
            origin=ORIGIN,
            tailscale_container="",
            agent="codex",
            network="direct",
            home=None,
            assets_dir=None,
            project="Synthetic project; echo unreachable",
            device="Synthetic device",
        )
        pairing_id = uuid4()
        invitation = "i" * 43
        self.pairing = {
            "link": f"{ORIGIN}/hub/connect#pairing_id={pairing_id}&secret={invitation}",
            "project_id": str(uuid4()),
        }

    def test_existing_ssh_alias_uses_its_config_and_pairing_arguments_are_quoted(self):
        with patch.object(
            install, "remote_script", return_value=json.dumps(self.pairing).encode()
        ) as remote:
            result = install.issue_pairing(
                self.args,
                "/volume/Cloud Hub",
                "sudo",
                self.args.project,
                self.args.device,
                self.connector,
                ORIGIN,
            )
        self.assertEqual(result, self.pairing)
        command = remote.call_args.args[1]
        self.assertEqual(shlex.split(command)[1], "/volume/Cloud Hub")
        self.assertEqual(shlex.split(command)[-2:], [self.args.project, self.args.device])
        self.assertNotIn("-p", install.ssh_command(self.args, command))
        self.assertNotIn(self.pairing["link"], command)
        self.assertTrue(remote.call_args.kwargs["private"])

    def test_rejects_wrong_pairing_origin_and_ssh_option_injection(self):
        with self.assertRaisesRegex(install.InstallError, "nas_pairing_result_invalid"):
            install.validate_pairing(
                json.dumps(self.pairing).encode(), self.connector, "https://wrong.invalid"
            )
        self.args.nas = "-oProxyCommand=some-command"
        with self.assertRaisesRegex(install.InstallError, "ssh_target_invalid"):
            install.check_arguments(self.args, self.connector)

    def test_resume_reuses_private_invitation_without_issuing_another_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            workspace = home / "Synthetic workspace"
            workspace.mkdir()
            remote = SimpleNamespace(network_status={"action_required": False})
            with (
                patch.object(self.connector, "Remote", return_value=remote),
                patch.object(install, "issue_pairing", return_value=self.pairing) as pairing,
                patch.object(
                    self.connector, "connect", return_value={"status": "configured"}
                ) as connect,
            ):
                for _ in range(2):
                    install.install_client(
                        self.args, self.connector, home, workspace, ORIGIN, "/safe", "docker"
                    )
                pairing.assert_called_once()
                self.assertEqual(connect.call_count, 2)
                self.assertEqual(connect.call_args.args[0], self.pairing["link"])
            saved = next(home.glob(".cloud-memory-hub/first-install/*/pairing.json"))
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_pending_codex_trust_is_not_an_automatic_ready_result(self):
        result = {
            "status": "configured",
            "automation": {"status": "configured", "host_review_required": True},
        }
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(install.show_client_result(result, "codex"), 3)
        self.assertIn("Review hooks", output.getvalue())

    def test_legacy_admin_opens_login_and_copies_the_secret_only_via_stdin(self):
        token = "synthetic" * 5
        finished = SimpleNamespace(returncode=0)
        with (
            patch.object(install.sys, "platform", "darwin"),
            patch.object(install, "read_admin_entry", return_value=(ORIGIN, "login", token)),
            patch.object(install.shutil, "which", return_value="/usr/bin/open"),
            patch.object(install.subprocess, "run", return_value=finished) as launch,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertTrue(install.open_admin_entry(self.args, self.connector))
        browser, copy = launch.call_args_list
        self.assertEqual(copy.args[0], ["/usr/bin/pbcopy"])
        self.assertEqual(copy.kwargs["input"], token.encode())
        self.assertEqual(browser.args[0], ["/usr/bin/open", ORIGIN + "/hub/login"])
        self.assertNotIn(token, output.getvalue())

    def test_setup_proof_is_only_opened_for_the_expected_hub(self):
        proof = "a" * 64
        response = json.dumps({"origin": ORIGIN, "proof": proof}).encode()
        with patch.object(install, "remote_script", return_value=response) as remote:
            self.assertEqual(
                install.read_admin_entry(
                    self.args, self.connector, expected_origin=ORIGIN, setup_only=True
                ),
                (ORIGIN, "setup", proof),
            )
            self.assertTrue(remote.call_args.kwargs["private"])
            self.assertIn("--setup-only", remote.call_args.args[1])
            with self.assertRaisesRegex(install.InstallError, "admin_entry_unavailable"):
                install.read_admin_entry(
                    self.args, self.connector, expected_origin="https://other.invalid"
                )


if __name__ == "__main__":
    unittest.main()
