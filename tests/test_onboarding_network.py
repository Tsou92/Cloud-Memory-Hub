"""Synthetic route-policy contracts; no external host or credentials are contacted."""

import http.client
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cmh_network_connect", ROOT / "apps/hub/src/cloud_memory_hub/onboarding/connect.py"
)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "cmh_network_bootstrap_fixtures", ROOT / "tests/test_onboarding_bootstrap.py"
)
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)
ORIGIN = "https://hub.example.test"


class Response:
    status = 200

    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.data


class Opener:
    def __init__(self, healthy=True, *, post_error=False):
        self.healthy = healthy
        self.post_error = post_error
        self.requests = []

    def open(self, request, **_kwargs):
        self.requests.append(request)
        if request.full_url.endswith("/healthz"):
            if isinstance(self.healthy, Exception):
                raise self.healthy
            if not self.healthy:
                raise urllib.error.URLError("synthetic private proxy detail")
            return Response({"status": "ok"})
        if self.post_error:
            if isinstance(self.post_error, Exception):
                raise self.post_error
            raise urllib.error.URLError("synthetic response lost after remote acceptance")
        return Response({"accepted": True})


class NetworkTests(unittest.TestCase):
    def make_remote(self, *, environment=True, direct=True, network="auto", proxies=None):
        self.direct = Opener(direct)
        self.environment = Opener(environment)
        self.handlers = []

        def build(handler, _redirect):
            self.handlers.append(handler.proxies)
            return self.direct if len(self.handlers) == 1 else self.environment

        with (
            patch.object(
                client.urllib.request, "getproxies_environment", return_value=proxies or {}
            ),
            patch.object(client.urllib.request, "build_opener", side_effect=build),
        ):
            return client.Remote(ORIGIN, network=network)

    def test_no_proxy_environment_uses_available_environment_path(self):
        remote = self.make_remote()
        self.assertEqual(remote.network_status["selected"], "environment")
        self.assertFalse(remote.network_status["action_required"])
        self.assertIs(remote.opener, self.environment)
        self.assertEqual(len(self.direct.requests), 0)

    def test_proxy_required_environment_is_supported_without_direct_probe(self):
        remote = self.make_remote(
            environment=True, direct=False, proxies={"https": "http://proxy:8080"}
        )
        self.assertEqual(remote.network_status["selected"], "environment")
        self.assertEqual(remote.request("/session", token="synthetic-device"), {"accepted": True})
        self.assertEqual(self.handlers[1], {"https": "http://proxy:8080"})
        self.assertFalse(self.direct.requests)

    def test_bad_proxy_direct_success_requires_host_network_repair_before_credentials(self):
        remote = self.make_remote(environment=False, direct=True)
        self.assertTrue(remote.network_status["action_required"])
        self.assertEqual(remote.network_status["selected"], "direct")
        with self.assertRaisesRegex(client.ConnectError, "network_action_required"):
            # Intentionally synthetic invitation; the repair gate must not send it.
            invitation = "synthetic"
            remote.request("/claim", payload={"invitation_secret": invitation})
        for request in self.direct.requests + self.environment.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertNotIn("Authorization", dict(request.header_items()))

    def test_both_routes_fail_with_fixed_error_not_proxy_exception(self):
        with self.assertRaises(client.ConnectError) as raised:
            self.make_remote(environment=False, direct=False)
        self.assertEqual(str(raised.exception), "network_unavailable")

    def test_explicit_environment_never_falls_back_to_direct(self):
        with self.assertRaisesRegex(client.ConnectError, "network_unavailable"):
            self.make_remote(environment=False, direct=True, network="environment")
        self.assertFalse(self.direct.requests)

    def test_explicit_direct_still_reports_incompatible_host_environment(self):
        remote = self.make_remote(environment=False, direct=True, network="direct")
        self.assertTrue(remote.network_status["action_required"])
        remote = self.make_remote(environment=True, direct=True, network="direct")
        self.assertFalse(remote.network_status["action_required"])
        self.assertIs(remote.opener, self.direct)

    def test_unknown_post_result_is_not_replayed_on_another_route(self):
        remote = self.make_remote()
        self.environment.post_error = True
        with self.assertRaisesRegex(client.ConnectError, "network_unavailable"):
            claimant = "synthetic"
            remote.request("/claim", payload={"claimant_secret": claimant})
        self.assertEqual(sum(r.get_method() == "POST" for r in self.environment.requests), 1)
        self.assertFalse(self.direct.requests)

    def test_http_and_https_loopback_never_probe_or_send_through_environment_proxy(self):
        for origin in ("http://127.0.0.1:8800", "http://localhost:8800", "http://[::1]:8800"):
            for mode in ("auto", "environment", "direct"):
                with self.subTest(origin=origin, mode=mode):
                    direct = Opener()
                    with (
                        patch.object(
                            client.urllib.request,
                            "getproxies_environment",
                            return_value={"http": "http://proxy:8080"},
                        ),
                        patch.object(
                            client.urllib.request, "build_opener", return_value=direct
                        ) as build,
                        patch.object(
                            client.urllib.request, "proxy_bypass_environment", return_value=False
                        ),
                    ):
                        remote = client.Remote(origin, network=mode)
                    self.assertEqual(build.call_count, 1)
                    self.assertEqual(build.call_args.args[0].proxies, {})
                    self.assertTrue(remote.network_status["loopback_direct"])
                    self.assertTrue(remote.network_status["action_required"])
                    with self.assertRaisesRegex(client.ConnectError, "network_action_required"):
                        invitation = "synthetic"
                        remote.request("/claim", payload={"invitation_secret": invitation})
                    self.assertEqual([r.get_method() for r in direct.requests], ["GET"])

    def test_http_loopback_with_exact_bypass_can_complete_direct_requests(self):
        direct = Opener()
        with (
            patch.object(
                client.urllib.request,
                "getproxies_environment",
                return_value={"http": "http://proxy:8080", "no": "127.0.0.1"},
            ),
            patch.object(client.urllib.request, "build_opener", return_value=direct),
            patch.object(client.urllib.request, "proxy_bypass_environment", return_value=True),
        ):
            remote = client.Remote("http://127.0.0.1:8800")
        self.assertFalse(remote.network_status["action_required"])
        self.assertEqual(remote.request("/claim", payload={"synthetic": True}), {"accepted": True})

    def test_bad_proxy_url_is_reported_without_userinfo(self):
        direct = Opener()
        userinfo = "private-user:private-password"
        proxy_url = f"http://{userinfo}@proxy:badport"
        with (
            patch.object(
                client.urllib.request,
                "getproxies_environment",
                # Intentionally synthetic userinfo to verify fixed, redacted errors.
                return_value={"https": proxy_url},
            ),
            patch.object(
                client.urllib.request,
                "build_opener",
                side_effect=[direct, ValueError("private-user:private-password")],
            ),
        ):
            remote = client.Remote(ORIGIN)
        self.assertTrue(remote.network_status["action_required"])
        self.assertNotIn("private-user", json.dumps(remote.network_status))
        self.assertNotIn("private-password", json.dumps(remote.network_status))

    def test_http_protocol_errors_in_probe_and_post_never_escape_as_raw_exceptions(self):
        detail = "synthetic private proxy userinfo"
        for error in (http.client.InvalidURL(detail), http.client.BadStatusLine(detail)):
            with self.subTest(error=type(error).__name__):
                remote = self.make_remote(environment=error, direct=True)
                self.assertTrue(remote.network_status["action_required"])
                self.assertNotIn(detail, json.dumps(remote.network_status))
                remote = self.make_remote()
                self.environment.post_error = error
                with self.assertRaises(client.ConnectError) as raised:
                    remote.request("/claim", payload={"synthetic": True})
                self.assertEqual(str(raised.exception), "network_unavailable")

    @unittest.skipUnless(shutil.which("openssl"), "Synthetic TLS fixture requires openssl")
    def test_real_tls_health_is_reachable_only_via_connect_proxy(self):
        for mode in ("auto", "environment"):
            with (
                self.subTest(mode=mode),
                fixtures.synthetic_download(b'{"status":"ok"}', tls=True, path="/healthz") as (
                    target,
                    ca,
                ),
            ):
                target_port = client.urllib.parse.urlsplit(target).port
                with fixtures.synthetic_proxy(b"", "required", ("127.0.0.1", target_port)) as (
                    proxy,
                    requests,
                ):
                    with patch.dict(
                        os.environ, fixtures.isolated_proxy_environment(proxy, ca), clear=True
                    ):
                        remote = client.Remote("https://hub-proxy-only.invalid:65531", network=mode)
                    self.assertEqual(remote.network_status["selected"], "environment")
                    self.assertTrue(remote.network_status["environment_available"])
                    self.assertIsNone(remote.network_status["direct_available"])
                    self.assertFalse(remote.network_status["action_required"])
                    self.assertEqual(requests, ["CONNECT hub-proxy-only.invalid:65531"])

    @unittest.skipUnless(shutil.which("openssl"), "Synthetic TLS fixture requires openssl")
    def test_real_tls_direct_health_with_broken_connect_proxy_requires_repair_before_claim(self):
        with fixtures.synthetic_download(b'{"status":"ok"}', tls=True, path="/healthz") as (
            target,
            ca,
        ):
            port = client.urllib.parse.urlsplit(target).port
            with fixtures.synthetic_proxy(b"", "broken") as (proxy, requests):
                resolve = socket.getaddrinfo

                def exact_fixture_dns(host, service, *args, **kwargs):
                    if host == "hub-proxy-only.invalid":
                        host = "127.0.0.1"
                    return resolve(host, service, *args, **kwargs)

                with (
                    patch.dict(
                        os.environ, fixtures.isolated_proxy_environment(proxy, ca), clear=True
                    ),
                    patch.object(socket, "getaddrinfo", side_effect=exact_fixture_dns),
                ):
                    remote = client.Remote(f"https://hub-proxy-only.invalid:{port}")
                    self.assertTrue(remote.network_status["action_required"])
                    self.assertFalse(remote.network_status["environment_available"])
                    self.assertTrue(remote.network_status["direct_available"])
                    with self.assertRaisesRegex(client.ConnectError, "network_action_required"):
                        remote.request("/hub/v1/pairings/claim", payload={"synthetic": True})
                self.assertEqual(requests, [f"CONNECT hub-proxy-only.invalid:{port}"])

    def test_proxy_fallback_fields_are_preserved_without_ignoring_environment(self):
        self.make_remote(proxies={"http": "http://proxy:8080", "no": "example.test"})
        self.assertEqual(self.handlers[1]["https"], "http://proxy:8080")
        self.assertEqual(self.handlers[1]["no"], "example.test")

    def test_guidance_uses_exact_safe_host_and_correct_ip_rule(self):
        for origin, rule in [
            (ORIGIN, "DOMAIN,hub.example.test,DIRECT"),
            ("http://127.0.0.1:8800", "IP-CIDR,127.0.0.1/32,DIRECT"),
            ("http://[::1]:8800", "IP-CIDR6,::1/128,DIRECT"),
        ]:
            with self.subTest(origin=origin):
                text = client.network_guidance(origin, "codex")
                self.assertIn(rule, text)
                self.assertIn("--network environment", text)
                self.assertIn("NO_PROXY", text)
                self.assertIn("/mcp", text)
                self.assertIn("proxy-and-network.md", text)
                self.assertNotIn('NO_PROXY="*"', text)
        for origin in (
            "https://*.test",
            "https://$(id).test",
            "https://a;id.test",
            "https://.test",
            "https://[fe80::1%foo$(id)]",
            "https://[fe80::1%en0]",
            "https://[fe80::1%foo`id`]",
        ):
            with self.subTest(origin=origin), self.assertRaises(client.ConnectError):
                client.network_guidance(origin, "claude")

    @unittest.skipIf(os.name == "nt", "POSIX shell probe")
    def test_suggested_shell_bypass_preserves_existing_data_and_is_idempotent(self):
        lines = client.network_guidance(ORIGIN, "claude").splitlines()
        commands = "\n".join(line for line in lines if line.startswith("case "))
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "unexpected"
            existing = f"localhost,$(touch {marker}),`touch {marker}`"
            command = commands + "\n" + commands + '\nprintf "%s\\n%s\\n" "$NO_PROXY" "$no_proxy"'
            result = subprocess.run(
                ["/bin/sh", "-c", command],
                env={**os.environ, "NO_PROXY": existing, "no_proxy": existing},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertFalse(marker.exists())
            self.assertEqual(result.stdout.splitlines(), [existing + ",hub.example.test"] * 2)

    def test_powershell_guidance_checks_existing_case_insensitive_environment_entries(self):
        with patch.object(client.os, "name", "nt"):
            text = client.network_guidance(ORIGIN, "codex")
        commands = [line for line in text.splitlines() if line.startswith("if (")]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all('-notcontains "hub.example.test"' in line for line in commands))
        self.assertIn("$env:NO_PROXY", commands[0])
        self.assertIn("$env:no_proxy", commands[1])


if __name__ == "__main__":
    unittest.main()
