"""Exercise downloaded installer verification without touching an Agent configuration."""

import ast
import hashlib
import http.server
import json
import os
import select
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

RESOURCES = Path(__file__).resolve().parents[1] / "apps/hub/src/cloud_memory_hub/onboarding"
CONNECTOR = (
    b"import json,sys; print(json.dumps({'bootstrap_probe':sys.argv[1:],"
    b"'python_version':list(sys.version_info[:3])}))\n"
)


@contextmanager
def synthetic_download(content, *, tls=False, path="/hub/connect.py", redirect=False):
    temporary = tempfile.TemporaryDirectory(prefix="cmh-test-tls-")
    cert = Path(temporary.name) / "ca.pem"
    key = Path(temporary.name) / "key.pem"
    if tls:
        subprocess.run(
            [
                shutil.which("openssl"),
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,DNS:hub-proxy-only.invalid,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if redirect and self.path == path:
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path != path and not (redirect and self.path == "/redirected"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"{'https://localhost' if tls else 'http://127.0.0.1'}:{server.server_port}",
            cert if tls else None,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        temporary.cleanup()


@contextmanager
def synthetic_proxy(content, mode=None, tunnel_target=None):
    requests = []
    if mode is None:
        yield None, requests
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = content if mode == "required" else b""
            self.send_response(200 if mode == "required" else 502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_CONNECT(self):
            requests.append("CONNECT " + self.path)
            if mode != "required" or not tunnel_target:
                self.send_error(502)
                return
            with socket.create_connection(tunnel_target, timeout=5) as target:
                self.send_response(200)
                self.end_headers()
                sockets = [self.connection, target]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 5)
                    if not ready:
                        return
                    for source in ready:
                        try:
                            data = source.recv(65536)
                            if not data:
                                return
                            (target if source is self.connection else self.connection).sendall(data)
                        except OSError:
                            # TLS rejection intentionally closes the tunnel abruptly.
                            return

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def isolated_proxy_environment(proxy, ca_file=None):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "psmodulepath"}
    }
    if proxy:
        environment.update(
            http_proxy=proxy,
            https_proxy=proxy,
            all_proxy=proxy,
            HTTP_PROXY=proxy,
            HTTPS_PROXY=proxy,
            ALL_PROXY=proxy,
        )
    if ca_file:
        environment.update(CURL_CA_BUNDLE=str(ca_file), SSL_CERT_FILE=str(ca_file))
    return environment


POWERSHELL_PRIVATE_CA = r"""
function Invoke-WebRequest {
    [CmdletBinding()]
    param([switch]$UseBasicParsing, [Uri]$Uri, [string]$OutFile,
          [int]$TimeoutSec = 180, [int]$MaximumRedirection = 0,
          [switch]$PassThru, [switch]$NoProxy)
    $session = $null
    $cmhTestStage = 'module_import'
    try {
        # This function shadows Invoke-WebRequest, so load its built-in module
        # before resolving WebRequestSession in a cold PowerShell process.
        Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
        $cmhTestStage = 'session_constructor'
        $session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
        $cmhTestStage = 'session_options'
        $flags = [Reflection.BindingFlags]'Instance,Public,NonPublic'
        $type = $session.GetType()
        $type.GetProperty('NoProxy', $flags).SetValue($session, $NoProxy.IsPresent)
        $type.GetProperty('ConnectionTimeout', $flags).SetValue(
            $session, [TimeSpan]::FromSeconds($TimeoutSec))
        $session.MaximumRedirection = $MaximumRedirection
        $cmhTestStage = 'client'
        $arguments = [object[]]@($false, $false)
        $client = $type.GetMethod('GetHttpClient', $flags).Invoke($session, $arguments)
        # Fixture-only deadline keeps intentional DNS/CONNECT failures bounded.
        $client.Timeout = [TimeSpan]::FromSeconds(10)
        $handler = [Net.Http.HttpMessageInvoker].GetField('_handler', $flags).GetValue($client)
        $cmhTestStage = 'policy'
        $policy = [Security.Cryptography.X509Certificates.X509ChainPolicy]::new()
        $policy.TrustMode =
            [Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
        $policy.RevocationMode = $handler.SslOptions.CertificateRevocationCheckMode
        $null = $policy.ApplicationPolicy.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.1'))
        $null = $policy.CustomTrustStore.Add(
            [Security.Cryptography.X509Certificates.X509Certificate2]::new('__CA__'))
        $handler.SslOptions.CertificateChainPolicy = $policy
        $cmhTestStage = 'request'
        Microsoft.PowerShell.Utility\Invoke-WebRequest @PSBoundParameters -WebSession $session
    } catch {
        [Console]::Error.WriteLine('cmh_fixture_stage=' + $cmhTestStage)
        [Console]::Error.WriteLine('cmh_fixture_error_type=' + $_.Exception.GetType().FullName)
        throw
    } finally { if ($session) { $session.Dispose() } }
}
"""


def windows_test_ca(command, ca_file, *, legacy=False):
    if os.name == "nt":
        command = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            "$OutputEncoding = [Console]::OutputEncoding; " + command
        )
    if not ca_file:
        return command
    cert = str(ca_file).replace("'", "''")
    if not legacy:
        # Test-only session policy: real IWR/TLS/proxy transport, one private root.
        # Hostname, expiry and chain validation remain enabled; no OS trust mutation.
        return POWERSHELL_PRIVATE_CA.replace("__CA__", cert) + command
    if (
        os.name != "nt"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise unittest.SkipTest("PS5 TLS fixture trust is restricted to disposable Windows CI")
    return (
        "$cmhTestCert = [Security.Cryptography.X509Certificates.X509Certificate2]::new('"
        + cert
        + "'); "
        "$cmhTestStore = "
        "[Security.Cryptography.X509Certificates.X509Store]::new('Root','CurrentUser'); "
        "[Console]::Error.WriteLine('cmh_legacy_fixture_stage=trust_open'); "
        "$cmhTestStore.Open('ReadWrite'); try { "
        "[Console]::Error.WriteLine('cmh_legacy_fixture_stage=trust_add'); "
        "$cmhTestStore.Add($cmhTestCert); try { "
        "[Console]::Error.WriteLine('cmh_legacy_fixture_stage=legacy_download'); "
        + command
        + " } finally { "
        "[Console]::Error.WriteLine('cmh_legacy_fixture_stage=trust_remove'); "
        "$cmhTestStore.Remove($cmhTestCert) } "
        "} finally { $cmhTestStore.Close() }"
    )


class BootstrapTests(unittest.TestCase):
    def run_installer(
        self,
        *,
        windows,
        valid,
        missing_python=False,
        proxy_mode=None,
        unreachable=False,
        expect_success=None,
        untrusted=False,
        redirect=False,
        powershell_runtime=None,
        broken_candidate=False,
        wrong_hostname=False,
    ):
        with (
            tempfile.TemporaryDirectory(prefix="cmh bootstrap test ") as temporary,
            ExitStack() as extra,
        ):
            with (
                synthetic_download(CONNECTOR, tls=bool(proxy_mode), redirect=redirect) as (
                    origin,
                    ca_file,
                ),
                synthetic_proxy(CONNECTOR, proxy_mode, ("127.0.0.1", urlsplit(origin).port)) as (
                    proxy,
                    proxy_requests,
                ),
            ):
                if unreachable or proxy_mode == "required":
                    origin = "https://hub-proxy-only.invalid:65531"
                if wrong_hostname:
                    origin = "https://wrong-host.invalid:65531"
                client_ca = ca_file
                if windows and untrusted:
                    _, client_ca = extra.enter_context(synthetic_download(b"unused", tls=True))
                suffix = "ps1" if windows else "sh"
                script = (RESOURCES / f"install.{suffix}").read_text(encoding="utf-8")
                values = {
                    node.targets[0].id: ast.literal_eval(node.value)
                    for node in ast.parse(
                        (RESOURCES.parent / "onboarding_api.py").read_text(encoding="utf-8")
                    ).body
                    if isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in {"HUB_SHELL_FETCH", "HUB_POWERSHELL_FETCH"}
                }
                script = script.replace("__CMH_HUB_SHELL_FETCH__", values["HUB_SHELL_FETCH"])
                script = script.replace(
                    "__CMH_HUB_POWERSHELL_FETCH__", values["HUB_POWERSHELL_FETCH"]
                )
                if broken_candidate:
                    self.assertTrue(windows)
                    script = script.replace(
                        "@('python3.12', 'python3', 'python')",
                        "@('cmh-broken-python-probe', 'python3.12', 'python3', 'python')",
                    )
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
                path.write_text(script, encoding="utf-8-sig" if windows else "utf-8")
                # A non-secret placeholder also checks that shell metacharacters
                # are preserved as argument data, never interpreted as commands.
                link = origin + "/hub/connect#synthetic=A&B=literal with spaces"
                if windows:
                    escaped_path = str(path).replace("'", "''")
                    command = [
                        shutil.which(powershell_runtime)
                        if powershell_runtime
                        else (shutil.which("pwsh") or shutil.which("powershell")),
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        windows_test_ca(
                            f"& '{escaped_path}' -Link '{link}' -Agent codex",
                            client_ca,
                            legacy=powershell_runtime == "powershell",
                        ),
                    ]
                else:
                    command = ["sh", str(path), "--link", link, "--agent", "codex"]
                environment = isolated_proxy_environment(proxy, ca_file)
                if broken_candidate:
                    probe_bin = Path(temporary) / "candidate bin"
                    probe_bin.mkdir()
                    probe = probe_bin / (
                        "cmh-broken-python-probe.cmd"
                        if os.name == "nt"
                        else "cmh-broken-python-probe"
                    )
                    probe.write_text(
                        "@echo off\necho SYNTHETIC_UNAVAILABLE_PROBE 1>&2\nexit /b 19\n"
                        if os.name == "nt"
                        else "#!/bin/sh\necho SYNTHETIC_UNAVAILABLE_PROBE >&2\nexit 19\n",
                        encoding="ascii",
                    )
                    probe.chmod(0o700)
                    environment["PATH"] = str(probe_bin) + os.pathsep + environment.get("PATH", "")
                if untrusted:
                    environment.pop("CURL_CA_BUNDLE", None)
                    environment.pop("SSL_CERT_FILE", None)
                    curl_home = Path(temporary) / "curl settings"
                    curl_home.mkdir()
                    (curl_home / ".curlrc").write_text("insecure\nlocation\n", encoding="ascii")
                    environment["CURL_HOME"] = str(curl_home)
                expected = expect_success if expect_success is not None else valid
                timeout = 480 if missing_python else 30
                try:
                    result = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        env=environment,
                    )
                except subprocess.TimeoutExpired as error:
                    if powershell_runtime != "powershell":
                        raise
                    stderr = error.stderr or b""
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode("utf-8", errors="replace")
                    prefix = "cmh_legacy_fixture_stage="
                    stages = {"trust_open", "trust_add", "legacy_download", "trust_remove"}
                    seen = [
                        line.removeprefix(prefix)
                        for line in stderr.splitlines()
                        if line.startswith(prefix) and line.removeprefix(prefix) in stages
                    ]
                    connects = sum(request.startswith("CONNECT ") for request in proxy_requests)
                    raise self.failureException(
                        f"Windows PowerShell 5.1 exceeded {timeout}s; "
                        f"last fixture stage={seen[-1] if seen else 'unknown'}; "
                        f"proxy CONNECT requests={connects}"
                    ) from None
                if expected:
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
                self.assertNotIn("SYNTHETIC_UNAVAILABLE_PROBE", result.stdout + result.stderr)
                self.assertTrue(
                    all("#" not in url and "synthetic=" not in url for url in proxy_requests)
                )
                return result, proxy_requests

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

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_hub_download_falls_back_only_after_environment_proxy_failure(self):
        result, requests = self.run_installer(windows=False, valid=True, proxy_mode="reject")
        self.assertTrue(requests)
        self.assertIn("绕过代理", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_hub_download_keeps_a_required_environment_proxy(self):
        result, requests = self.run_installer(windows=False, valid=True, proxy_mode="required")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("绕过代理", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_both_paths_failure_is_private_and_actionable(self):
        result, requests = self.run_installer(
            windows=False, valid=True, proxy_mode="reject", unreachable=True, expect_success=False
        )
        self.assertTrue(requests)
        self.assertIn("Tailscale", result.stderr)
        self.assertIn("DIRECT", result.stderr)
        self.assertIn("Clash", result.stderr)
        self.assertNotIn("hub-proxy-only.invalid", result.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "Proxy environment coverage requires PowerShell 7")
    def test_powershell_hub_download_falls_back_after_environment_proxy_failure(self):
        result, requests = self.run_installer(windows=True, valid=True, proxy_mode="reject")
        self.assertTrue(requests)

    @unittest.skipUnless(shutil.which("pwsh"), "Proxy environment coverage requires PowerShell 7")
    def test_powershell_hub_download_keeps_a_required_environment_proxy(self):
        self.run_installer(windows=True, valid=True, proxy_mode="required")

    @unittest.skipUnless(shutil.which("pwsh"), "Proxy environment coverage requires PowerShell 7")
    def test_powershell_both_paths_failure_is_private_and_actionable(self):
        result, requests = self.run_installer(
            windows=True, valid=True, proxy_mode="reject", unreachable=True, expect_success=False
        )
        self.assertTrue(requests, f"exit={result.returncode}; stderr={result.stderr}")
        self.assertIn("DIRECT", result.stderr)
        self.assertNotIn("hub-proxy-only.invalid", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_hub_download_rejects_redirect_without_executing_target(self):
        self.run_installer(windows=False, valid=True, redirect=True, expect_success=False)

    @unittest.skipIf(os.name == "nt", "POSIX entry point is verified on Mac/Linux")
    def test_posix_hub_download_ignores_insecure_curlrc_and_keeps_tls_verification(self):
        self.run_installer(
            windows=False, valid=True, proxy_mode="required", untrusted=True, expect_success=False
        )

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("powershell"), "Requires real Windows PowerShell 5.1"
    )
    def test_windows_powershell_51_direct_download_and_integrity(self):
        self.run_installer(
            windows=True, valid=True, powershell_runtime="powershell", broken_candidate=True
        )
        self.run_installer(windows=True, valid=False, powershell_runtime="powershell")
        self.run_installer(
            windows=True, valid=True, proxy_mode="required", powershell_runtime="powershell"
        )

    @unittest.skipUnless(shutil.which("pwsh"), "Requires a real PowerShell runtime")
    def test_powershell_skips_a_stderr_failing_python_candidate(self):
        self.run_installer(windows=True, valid=True, broken_candidate=True)

    @unittest.skipUnless(shutil.which("pwsh"), "Requires a real PowerShell runtime")
    def test_powershell_private_ca_policy_rejects_an_untrusted_server_ca(self):
        _, requests = self.run_installer(
            windows=True,
            valid=True,
            proxy_mode="required",
            untrusted=True,
            expect_success=False,
        )
        self.assertTrue(any(request.startswith("CONNECT ") for request in requests))

    @unittest.skipUnless(shutil.which("pwsh"), "Requires a real PowerShell runtime")
    def test_powershell_private_ca_policy_rejects_a_wrong_hostname(self):
        _, requests = self.run_installer(
            windows=True,
            valid=True,
            proxy_mode="required",
            wrong_hostname=True,
            expect_success=False,
        )
        self.assertTrue(any(request.startswith("CONNECT ") for request in requests))

    def test_child_environment_rebuilds_legacy_powershell_module_paths(self):
        with patch.dict(os.environ, {"pSmOdUlEpAtH": "synthetic-parent-modules"}):
            environment = isolated_proxy_environment(None)
            self.assertFalse(any(key.lower() == "psmodulepath" for key in environment))
            self.assertEqual(os.environ["pSmOdUlEpAtH"], "synthetic-parent-modules")


if __name__ == "__main__":
    unittest.main()
