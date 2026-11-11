"""Run the exact copied command: a failed/partial download must never execute."""

import ast
import http.server
import json
import os
import shutil
import subprocess
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from test_onboarding_bootstrap import (
    isolated_proxy_environment,
    synthetic_download,
    synthetic_proxy,
    windows_test_ca,
)

SOURCE = Path(__file__).resolve().parents[1] / "apps/hub/src/cloud_memory_hub/onboarding_api.py"
NODE_HARNESS = """
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {
    value: id === 'agent' ? 'codex' : 'unix', selectedOptions: [{text:'Codex'}],
    addEventListener() {}, focus() {}, select() {}
  });
  return elements.get(id);
}};
vm.runInNewContext(input.script, {
  URLSearchParams, document,
  location: {hash: input.fragment, pathname:'/hub/connect'},
  history: {replaceState() {}}, navigator: {userAgent: input.windows ? 'Windows' : 'Mac'}
});
process.stdout.write(elements.get('command').value);
"""


@contextmanager
def bootstrap_server(mode, windows):
    marker = "Write-Output 'BOOTSTRAP_EXECUTED'" if windows else "echo BOOTSTRAP_EXECUTED"
    if mode == "installer_failure":
        marker += "\nthrow 'synthetic_install_failure'" if windows else "\nexit 19"
    content = b"" if mode == "empty" else (marker + "\n").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if mode == "redirect" and self.path != "/redirected":
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "not_found":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            size = len(content) + (100 if mode == "partial" else 0)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(content)
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@unittest.skipUnless(shutil.which("node"), "Command generation needs a JavaScript runtime")
class CopiedCommandTests(unittest.TestCase):
    def command(self, origin, *, windows):
        assignment = next(
            node
            for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign)
            and any(isinstance(name, ast.Name) and name.id == "SCRIPT" for name in node.targets)
        )
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"HUB_SHELL_FETCH", "HUB_POWERSHELL_FETCH"}
        }
        script = ast.literal_eval(assignment.value).replace("__ORIGIN__", json.dumps(origin))
        script = script.replace("__CMH_HUB_SHELL_FETCH__", json.dumps(values["HUB_SHELL_FETCH"]))
        script = script.replace(
            "__CMH_HUB_POWERSHELL_FETCH__", json.dumps(values["HUB_POWERSHELL_FETCH"])
        )
        fragment = "#pairing_id=00000000-0000-4000-8000-000000000001&secret=" + "A" * 43
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_HARNESS],
            input=json.dumps({"script": script, "fragment": fragment, "windows": windows}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    def check_download(self, *, windows, powershell_runtime=None):
        for mode in ("not_found", "partial", "empty", "redirect", "complete", "installer_failure"):
            with self.subTest(mode=mode), bootstrap_server(mode, windows) as origin:
                command = self.command(origin, windows=windows)
                if windows:
                    # A previous interactive attempt must not execute stale code
                    # when this attempt fails to download a replacement.
                    command = "$cmhScript = \"Write-Output 'STALE_SCRIPT_EXECUTED'\"; " + command
                    command = windows_test_ca(command, None)
                executable = (
                    shutil.which(powershell_runtime)
                    if powershell_runtime
                    else (shutil.which("pwsh") or shutil.which("powershell"))
                )
                arguments = (
                    [executable, "-NoProfile", "-NonInteractive", "-Command", command]
                    if windows
                    else ["sh", "-c", command]
                )
                result = subprocess.run(
                    arguments, capture_output=True, text=True, encoding="utf-8", timeout=15
                )
                self.assertNotIn("STALE_SCRIPT_EXECUTED", result.stdout)
                if mode == "complete":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("BOOTSTRAP_EXECUTED", result.stdout)
                elif mode == "installer_failure":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("BOOTSTRAP_EXECUTED", result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("BOOTSTRAP_EXECUTED", result.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX command is exercised on Mac/Linux")
    def test_copied_posix_command_requires_complete_successful_download(self):
        self.check_download(windows=False)

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "Copied Windows command requires PowerShell",
    )
    def test_copied_powershell_command_requires_complete_successful_download(self):
        self.check_download(windows=True)

    def check_proxy(self, *, windows, mode, unavailable=False):
        content = (
            "Write-Output 'BOOTSTRAP_EXECUTED'\n" if windows else "echo BOOTSTRAP_EXECUTED\n"
        ).encode()
        with (
            synthetic_download(
                content, tls=True, path="/hub/install.ps1" if windows else "/hub/install.sh"
            ) as (direct, ca_file),
            synthetic_proxy(content, mode, ("127.0.0.1", urlsplit(direct).port)) as (
                proxy,
                requests,
            ),
        ):
            origin = (
                "https://hub-proxy-only.invalid:65531"
                if unavailable or mode == "required"
                else direct
            )
            command = self.command(origin, windows=windows)
            if windows:
                command = windows_test_ca(command, ca_file)
            executable = shutil.which("pwsh") or shutil.which("powershell")
            arguments = (
                [executable, "-NoProfile", "-NonInteractive", "-Command", command]
                if windows
                else ["sh", "-c", command]
            )
            result = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                env=isolated_proxy_environment(proxy, ca_file),
            )
            self.assertTrue(requests)
            self.assertTrue(
                all(
                    "#" not in url and "secret=" not in url and "pairing_id=" not in url
                    for url in requests
                )
            )
            if unavailable:
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("BOOTSTRAP_EXECUTED", result.stdout)
                self.assertIn("DIRECT", result.stderr)
                self.assertNotIn("A" * 43, result.stderr)
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("BOOTSTRAP_EXECUTED", result.stdout)
                if mode == "required":
                    self.assertEqual(len(requests), 1)

    @unittest.skipIf(os.name == "nt", "POSIX command is exercised on Mac/Linux")
    def test_copied_posix_first_download_recovers_from_bad_proxy(self):
        self.check_proxy(windows=False, mode="reject")

    @unittest.skipIf(os.name == "nt", "POSIX command is exercised on Mac/Linux")
    def test_copied_posix_first_download_preserves_required_proxy(self):
        self.check_proxy(windows=False, mode="required")

    @unittest.skipIf(os.name == "nt", "POSIX command is exercised on Mac/Linux")
    def test_copied_posix_first_download_refuses_both_paths_failure(self):
        self.check_proxy(windows=False, mode="reject", unavailable=True)

    @unittest.skipUnless(shutil.which("pwsh"), "Proxy environment coverage requires PowerShell 7")
    def test_copied_powershell_first_download_recovers_from_bad_proxy(self):
        self.check_proxy(windows=True, mode="reject")

    @unittest.skipUnless(shutil.which("pwsh"), "Proxy environment coverage requires PowerShell 7")
    def test_copied_powershell_first_download_preserves_required_proxy(self):
        self.check_proxy(windows=True, mode="required")

    @unittest.skipUnless(shutil.which("pwsh"), "Proxy environment coverage requires PowerShell 7")
    def test_copied_powershell_first_download_refuses_both_paths_failure(self):
        self.check_proxy(windows=True, mode="reject", unavailable=True)

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("powershell"), "Requires real Windows PowerShell 5.1"
    )
    def test_windows_powershell_51_copied_command_download_failures(self):
        self.check_download(windows=True, powershell_runtime="powershell")


if __name__ == "__main__":
    unittest.main()
