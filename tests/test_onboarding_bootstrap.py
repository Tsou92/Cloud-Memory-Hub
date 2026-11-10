"""Exercise downloaded installer verification without touching an Agent configuration."""

import hashlib
import http.server
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1] / "apps/hub/src/cloud_memory_hub/onboarding"
CONNECTOR = (
    b"import json,sys; print(json.dumps({'bootstrap_probe':sys.argv[1:],"
    b"'python_version':list(sys.version_info[:3])}))\n"
)


@contextmanager
def synthetic_download(content):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/hub/connect.py":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

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


class BootstrapTests(unittest.TestCase):
    def run_installer(self, *, windows, valid, missing_python=False):
        with tempfile.TemporaryDirectory(prefix="cmh-bootstrap-test-") as temporary:
            with synthetic_download(CONNECTOR) as origin:
                suffix = "ps1" if windows else "sh"
                script = (RESOURCES / f"install.{suffix}").read_text()
                if missing_python:
                    # Simulate only interpreter discovery. Download verification,
                    # extraction, uv and its Python preparation execute unchanged.
                    search = (
                        "@('python3.12', 'python3', 'python')"
                        if windows
                        else "for cmh_candidate in python3.12 python3 python;"
                    )
                    replacement = (
                        "@('cmh-python-missing-fixture')"
                        if windows
                        else "for cmh_candidate in cmh-python-missing-fixture;"
                    )
                    self.assertEqual(script.count(search), 1)
                    script = script.replace(search, replacement)
                script = script.replace("__CMH_ORIGIN_SHELL__", shlex.quote(origin))
                script = script.replace("__CMH_ORIGIN_POWERSHELL__", f"'{origin}'")
                script = script.replace(
                    "__CMH_CONNECT_SHA256__",
                    hashlib.sha256(CONNECTOR if valid else b"other-release").hexdigest(),
                )
                path = Path(temporary) / f"install.{suffix}"
                path.write_text(script, encoding="utf-8")
                # A non-secret placeholder also checks that shell metacharacters
                # are preserved as argument data, never interpreted as commands.
                link = origin + "/hub/connect#synthetic=A&B=literal"
                if windows:
                    escaped_path = str(path).replace("'", "''")
                    command = [
                        shutil.which("pwsh") or shutil.which("powershell"),
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"& '{escaped_path}' -Link '{link}' -Agent codex",
                    ]
                else:
                    command = ["sh", str(path), "--link", link, "--agent", "codex"]
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=480 if missing_python else 30,
                )
                if valid:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    markers = [
                        line for line in result.stdout.splitlines() if "bootstrap_probe" in line
                    ]
                    self.assertEqual(len(markers), 1, result.stdout)
                    payload = json.loads(markers[0])
                    self.assertEqual(
                        payload["bootstrap_probe"], ["--link", link, "--agent", "codex"]
                    )
                    if missing_python:
                        self.assertEqual(payload["python_version"], [3, 12, 13])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("bootstrap_probe", result.stdout)
                    self.assertNotIn(link, result.stdout + result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_download_integrity_and_argument_preservation(self):
        self.run_installer(windows=False, valid=True)

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_refuses_mixed_version_without_running_download(self):
        self.run_installer(windows=False, valid=False)

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell entry point requires a real PowerShell runtime",
    )
    def test_powershell_download_integrity_and_argument_preservation(self):
        self.run_installer(windows=True, valid=True)

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell entry point requires a real PowerShell runtime",
    )
    def test_powershell_refuses_mixed_version_without_running_download(self):
        self.run_installer(windows=True, valid=False)

    @unittest.skipUnless(
        os.environ.get("CMH_TEST_BOOTSTRAP_RUNTIME") == "1" and os.name != "nt",
        "Pinned runtime download is exercised by the dedicated OS workflow",
    )
    def test_posix_prepares_verified_runtime_when_python_is_missing(self):
        self.run_installer(windows=False, valid=True, missing_python=True)

    @unittest.skipUnless(
        os.environ.get("CMH_TEST_BOOTSTRAP_RUNTIME") == "1" and os.name == "nt",
        "Pinned Windows runtime download requires the dedicated Windows workflow",
    )
    def test_powershell_prepares_verified_runtime_when_python_is_missing(self):
        self.run_installer(windows=True, valid=True, missing_python=True)


if __name__ == "__main__":
    unittest.main()
