"""Opt-in host-local network contract; the old strict-loopback probe is unchanged.

An explicitly supplied SSH alias identifies a reachable remote TCP negative
control. Only connect/close is performed: no SSH authentication or payload is
sent, and no remote configuration changes. All other listeners are owned by
this probe. This is network evidence, not a desktop or product integration test.
"""

import argparse
import errno
import ipaddress
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from probe_codex_isolation import listener, profile, run, same_host_address

WORKER = r"""
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *phase;
static void connect_test(const char *name, const char *address, int port, int ipv6) {
    int fd = socket(ipv6 ? AF_INET6 : AF_INET, SOCK_STREAM, 0);
    int code = errno;
    if (fd >= 0) {
        struct timeval timeout = {2, 0};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        int result;
        if (ipv6) {
            struct sockaddr_in6 peer = {0};
            peer.sin6_family = AF_INET6;
            peer.sin6_port = htons(port);
            inet_pton(AF_INET6, address, &peer.sin6_addr);
            result = connect(fd, (struct sockaddr *)&peer, sizeof(peer));
        } else {
            struct sockaddr_in peer = {0};
            peer.sin_family = AF_INET;
            peer.sin_port = htons(port);
            inet_pton(AF_INET, address, &peer.sin_addr);
            result = connect(fd, (struct sockaddr *)&peer, sizeof(peer));
        }
        code = result < 0 ? errno : 0;
        close(fd);
    }
    printf("{\"phase\":\"%s\",\"case\":\"%s\",\"code\":%d}\n", phase, name, code);
    fflush(stdout);
}
int main(int argc, char **argv) {
    if (argc != 8) return 64;
    phase = argv[7];
    connect_test("loopback_ipv4", "127.0.0.1", atoi(argv[2]), 0);
    connect_test("same_host_ipv4", argv[1], atoi(argv[2]), 0);
    connect_test("loopback_ipv6", "::1", atoi(argv[3]), 1);
    connect_test("loopback_unlisted", "127.0.0.1", atoi(argv[4]), 0);
    connect_test("same_host_unlisted", argv[1], atoi(argv[4]), 0);
    connect_test("remote_same_allowed_port", argv[5], atoi(argv[6]), 0);
    if (strcmp(phase, "parent") == 0) {
        pid_t child = fork();
        if (child < 0) return 65;
        if (child == 0) {
            argv[7] = "child";
            execv(argv[0], argv);
            _exit(66);
        }
        int status;
        if (waitpid(child, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status)) return 67;
    }
    return 0;
}
"""


def ssh_endpoint(alias):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", alias) is None:
        raise ValueError("invalid_ssh_control_alias")
    # Effective destination metadata only; no key material is read by this script.
    result = subprocess.run(
        ["ssh", "-G", alias], capture_output=True, text=True, timeout=10, check=True
    )
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key in {"hostname", "port"}:
            values[key] = value
    address = ipaddress.IPv4Address(values["hostname"])
    port = int(values["port"])
    interfaces = subprocess.check_output(["/sbin/ifconfig"], text=True, timeout=10)
    local_addresses = re.findall(r"^\s+inet ([0-9.]+)\s", interfaces, re.MULTILINE)
    if (
        address.is_loopback
        or address.is_unspecified
        or str(address) in local_addresses
        or not 1 <= port <= 65535
    ):
        raise ValueError("remote_ipv4_ssh_control_required")
    return str(address), port


def assertions(control, restricted):
    positive = {"loopback_ipv4", "same_host_ipv4", "loopback_ipv6"}
    negative = {"loopback_unlisted", "same_host_unlisted", "remote_same_allowed_port"}
    baseline = {(row["phase"], row["case"]): row["code"] for row in control}
    actual = {(row["phase"], row["case"]): row["code"] for row in restricted}
    results = []
    for phase in ("parent", "child"):
        for case in sorted(positive | negative):
            key = phase, case
            expected = {0} if case in positive else {errno.EPERM, errno.EACCES}
            results.append(
                {
                    "phase": phase,
                    "case": case,
                    "control_code": baseline.get(key),
                    "restricted_code": actual.get(key),
                    "passed": baseline.get(key) == 0 and actual.get(key) in expected,
                }
            )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-control", required=True, help="explicit reachable remote SSH alias")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("macos_required")
    remote_address, remote_port = ssh_endpoint(args.ssh_control)
    root = Path(tempfile.mkdtemp(prefix="cmh-host-network-", dir="/private/tmp"))
    allowed = root / "allowed"
    allowed.mkdir()
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(allowed), "LANG": "C"}
    try:
        (allowed / "worker.c").write_text(WORKER)
        subprocess.run(
            [
                "/usr/bin/cc",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(allowed / "worker.c"),
                "-o",
                str(allowed / "worker"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        with ExitStack() as stack:
            local_address = same_host_address()
            ipv4, ipv6, unlisted = listener(stack), listener(stack, ipv6=True), listener(stack)
            listener(stack, address=local_address, port=ipv4)
            listener(stack, address=local_address, port=unlisted)
            command = [
                str(allowed / "worker"),
                local_address,
                str(ipv4),
                str(ipv6),
                str(unlisted),
                remote_address,
                str(remote_port),
                "parent",
            ]
            control = run(command, environment, root)
            # The remote control's port is explicitly allowed for *localhost*,
            # proving rejection by the host boundary rather than a port mismatch.
            base = profile(allowed, ipv4, ipv6)
            base += f'\n(allow network-outbound (remote tcp "localhost:{remote_port}"))\n'
            policies = {
                "default_deny": base,
                "desktop_network_deny": base.replace(
                    "(deny default)", "(allow default)\n(deny network*)", 1
                ),
            }
            checks = {}
            for name, policy in policies.items():
                policy_file = root / f"{name}.sb"
                policy_file.write_text(policy)
                restricted = run(
                    ["/usr/bin/sandbox-exec", "-f", str(policy_file), *command], environment, root
                )
                checks[name] = assertions(control, restricted)
        result = {
            "platform": platform.mac_ver()[0],
            "contract": "same-host-specific-tcp-ports-v1",
            "strict_loopback_contract_satisfied": False,
            "codex_started": False,
            "remote_control_connect_only": True,
            "authentication_or_payload_sent": False,
            "assertions": checks,
            "passed": all(row["passed"] for rows in checks.values() for row in rows),
        }
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        if not result["passed"]:
            raise RuntimeError("host_local_network_contract_failed")
    except BaseException:
        print(f"Synthetic failure fixture retained: {root}", file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
