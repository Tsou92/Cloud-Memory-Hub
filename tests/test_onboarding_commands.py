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
    content = (marker + "\n").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
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
            for node in ast.parse(SOURCE.read_text()).body
            if isinstance(node, ast.Assign)
            and any(isinstance(name, ast.Name) and name.id == "SCRIPT" for name in node.targets)
        )
        script = ast.literal_eval(assignment.value).replace("__ORIGIN__", json.dumps(origin))
        fragment = "#pairing_id=00000000-0000-4000-8000-000000000001&secret=" + "A" * 43
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_HARNESS],
            input=json.dumps({"script": script, "fragment": fragment, "windows": windows}),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        return result.stdout

    def check_download(self, *, windows):
        for mode in ("not_found", "partial", "complete"):
            with self.subTest(mode=mode), bootstrap_server(mode, windows) as origin:
                command = self.command(origin, windows=windows)
                executable = shutil.which("pwsh") or shutil.which("powershell")
                arguments = (
                    [executable, "-NoProfile", "-NonInteractive", "-Command", command]
                    if windows
                    else ["sh", "-c", command]
                )
                result = subprocess.run(arguments, capture_output=True, text=True, timeout=15)
                if mode == "complete":
                    self.assertEqual(result.returncode, 0, result.stderr)
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


if __name__ == "__main__":
    unittest.main()
