"""Opt-in macOS Seatbelt probe; never launches Codex or reads credential contents.

Only synthetic contents and public system files are read. Real home locations
are tested by opening directory handles without listing them. Network targets
are loopback or an IP of this same host, never a remote host. No model or
credential retrieval APIs are invoked. A failed assertion remains a failure;
this script is not a Codex launcher or an approved desktop isolation policy.
"""

import errno
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path

WORKER = r"""
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <servers/bootstrap.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *phase;
static void report(const char *name, int code) {
    printf("{\"phase\":\"%s\",\"case\":\"%s\",\"code\":%d}\n", phase, name, code);
    fflush(stdout);
}
static void file_test(const char *name, const char *path, int directory, int writing) {
    int flags = directory ? O_RDONLY | O_DIRECTORY : writing ? O_WRONLY | O_APPEND : O_RDONLY;
    int fd = open(path, flags);
    int code = fd < 0 ? errno : 0;
    if (fd >= 0 && !directory) {
        char byte;
        ssize_t count = writing ? write(fd, "X", 1) : read(fd, &byte, 1);
        if (count < 0) code = errno;
    }
    if (fd >= 0) close(fd);
    report(name, code);
}
static void network_test(const char *name, const char *address, int port, int ipv6, int udp) {
    int family = ipv6 ? AF_INET6 : AF_INET;
    int fd = socket(family, udp ? SOCK_DGRAM : SOCK_STREAM, 0);
    if (fd < 0) { report(name, errno); return; }
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
    report(name, result < 0 ? errno : 0);
    close(fd);
}
int main(int argc, char **argv) {
    if (argc != 15) return 64;
    phase = argv[14];
    file_test("allowed_fixture_read", argv[1], 0, 0);
    file_test("allowed_fixture_write", argv[1], 0, 1);
    file_test("outside_fixture_read", argv[2], 0, 0);
    file_test("symlink_escape_read", argv[3], 0, 0);
    file_test("synthetic_codex_read", argv[4], 0, 0);
    file_test("synthetic_agents_read", argv[5], 0, 0);
    file_test("synthetic_keychain_read", argv[6], 0, 0);
    file_test("real_home_directory_open", argv[7], 1, 0);
    file_test("real_codex_directory_open", argv[8], 1, 0);
    file_test("real_agents_directory_open", argv[9], 1, 0);
    file_test("real_keychain_directory_open", argv[10], 1, 0);
    file_test("public_system_read", "/System/Library/CoreServices/SystemVersion.plist", 0, 0);
    network_test("loopback_ipv4_tcp", "127.0.0.1", atoi(argv[12]), 0, 0);
    network_test("loopback_ipv6_tcp", "::1", atoi(argv[13]), 1, 0);
    network_test("nonloopback_same_host_udp", argv[11], atoi(argv[12]), 0, 1);
    network_test("nonloopback_same_host_tcp", argv[11], atoi(argv[12]), 0, 0);
    mach_port_t service = MACH_PORT_NULL;
    kern_return_t result = bootstrap_look_up(bootstrap_port, "com.apple.SecurityServer", &service);
    report("keychain_service_lookup", result);
    if (service != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), service);
    if (strcmp(phase, "parent") == 0) {
        pid_t child = fork();
        if (child < 0) return 65;
        if (child == 0) {
            argv[14] = "child";
            execv(argv[0], argv);
            _exit(66);
        }
        int status;
        if (waitpid(child, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status)) return 67;
    }
    return 0;
}
"""


def same_host_address():
    # Read only interface metadata; never include addresses in retained evidence.
    output = subprocess.check_output(["/sbin/ifconfig"], text=True)
    for value in re.findall(r"^\s+inet ([0-9.]+)\s", output, re.MULTILINE):
        address = ipaddress.ip_address(value)
        if not address.is_loopback and not address.is_unspecified:
            return str(address)
    raise RuntimeError("no_nonloopback_local_interface_for_control")


def listener(stack, ipv6=False, *, address=None, port=0):
    server = socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET, socket.SOCK_STREAM)
    stack.callback(server.close)
    server.bind((address or ("::1" if ipv6 else "127.0.0.1"), port))
    server.listen(8)
    server.settimeout(0.2)
    stopped = threading.Event()

    def accept():
        while not stopped.is_set():
            try:
                connection, _ = server.accept()
                connection.close()
            except TimeoutError:
                continue
            except OSError:
                break

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    stack.callback(thread.join, 1)
    stack.callback(stopped.set)
    return server.getsockname()[1]


def profile(allowed, ipv4_port, ipv6_port):
    return f"""(version 1)
(deny default)
(allow process-exec (literal {json.dumps(str(allowed / "worker"))}))
(allow process-fork)
; The installed dyld-support.sb documents libignition's openat root handle.
(allow file-read* (literal "/"))
(allow file-read*
    (subpath {json.dumps(str(allowed))})
    (subpath "/usr/lib")
    (subpath "/System/Library")
    (subpath "/System/Volumes/Preboot/Cryptexes/OS/usr/lib")
    (subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library"))
(allow network-outbound
    (remote tcp "localhost:{ipv4_port}")
    (remote tcp "localhost:{ipv6_port}"))
"""


def run(command, environment, directory):
    process = subprocess.Popen(
        command,
        env=environment,
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        # Only this probe's newly created process group, never the daily app.
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        (directory / "worker-stderr.txt").write_text(stderr)
        (directory / "worker-stdout.jsonl").write_text(stdout)
        raise RuntimeError("worker_timeout") from None
    except BaseException:
        # Ctrl-C does not reach a start_new_session child. End only the group
        # created above, including any fork/exec descendants, before re-raising.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if process.returncode:
        # Raw errors may contain local paths; keep them only in this synthetic fixture.
        (directory / "worker-stderr.txt").write_text(stderr)
        (directory / "worker-stdout.jsonl").write_text(stdout)
        raise RuntimeError(f"worker_failed_{process.returncode}")
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def verify(control, restricted):
    control = {(row["phase"], row["case"]): row["code"] for row in control}
    restricted = {(row["phase"], row["case"]): row["code"] for row in restricted}
    allowed = {
        "allowed_fixture_read",
        "public_system_read",
        "loopback_ipv4_tcp",
        "loopback_ipv6_tcp",
    }
    denied = {
        "allowed_fixture_write",
        "outside_fixture_read",
        "symlink_escape_read",
        "synthetic_codex_read",
        "synthetic_agents_read",
        "synthetic_keychain_read",
        "real_home_directory_open",
        "real_codex_directory_open",
        "real_agents_directory_open",
        "real_keychain_directory_open",
        "nonloopback_same_host_udp",
        "nonloopback_same_host_tcp",
    }
    assertions = []
    for phase in ("parent", "child"):
        for name in sorted(allowed | denied):
            key = phase, name
            baseline, actual = control.get(key), restricted.get(key)
            control_ok = baseline == 0
            expected = {0} if name in allowed else {errno.EPERM, errno.EACCES}
            assertions.append(
                {
                    "phase": phase,
                    "case": name,
                    "control_code": baseline,
                    "restricted_code": actual,
                    "passed": control_ok and actual in expected,
                }
            )
        key = phase, "keychain_service_lookup"
        assertions.append(
            {
                "phase": phase,
                "case": key[1],
                "control_code": control.get(key),
                "restricted_code": restricted.get(key),
                # BOOTSTRAP_NOT_PRIVILEGED, not a missing service or a timeout.
                "passed": control.get(key) == 0 and restricted.get(key) == 1100,
            }
        )
    return assertions


def main():
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise SystemExit("macos_sandbox_exec_required")
    root = Path(tempfile.mkdtemp(prefix="cmh-codex-isolation-", dir="/private/tmp"))
    allowed, outside = root / "allowed", root / "outside"
    allowed.mkdir()
    outside.mkdir()
    synthetic_home = allowed / "home"
    synthetic_home.mkdir()
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(synthetic_home),
        "CODEX_HOME": str(synthetic_home / ".codex"),
        "LANG": "C",
    }
    try:
        (allowed / "worker.c").write_text(WORKER)
        compile_result = subprocess.run(
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
            env=environment,
            timeout=60,
        )
        if compile_result.returncode:
            (root / "compile-stderr.txt").write_text(compile_result.stderr)
            raise RuntimeError("synthetic_worker_compile_failed")
        canaries = [
            allowed / "fixture",
            outside / "fixture",
            outside / ".codex" / "auth.canary",
            outside / ".agents" / "skill.canary",
            outside / "Keychains" / "key.canary",
        ]
        for path in canaries:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("CMH_SYNTHETIC_CANARY\n")
        (allowed / "escape").symlink_to(outside / "fixture")
        home = Path.home()
        with ExitStack() as stack:
            local_address = same_host_address()
            ipv4_port, ipv6_port = listener(stack), listener(stack, ipv6=True)
            # A disposable accept-and-close endpoint on this host proves the
            # negative TCP case; a closed port cannot prove sandbox rejection.
            listener(stack, address=local_address, port=ipv4_port)
            profile_file = root / "isolation.sb"
            profile_file.write_text(profile(allowed, ipv4_port, ipv6_port))
            arguments = [
                str(allowed / "worker"),
                str(canaries[0]),
                str(canaries[1]),
                str(allowed / "escape"),
                *(str(path) for path in canaries[2:]),
                str(home),
                str(home / ".codex"),
                str(home / ".agents"),
                str(home / "Library" / "Keychains"),
                local_address,
                str(ipv4_port),
                str(ipv6_port),
                "parent",
            ]
            control = run(arguments, environment, root)
            restricted = run(
                ["/usr/bin/sandbox-exec", "-f", str(profile_file), *arguments],
                environment,
                root,
            )
        assertions = verify(control, restricted)
        result = {
            "platform": platform.mac_ver()[0],
            "synthetic_worker": True,
            "codex_started": False,
            "credential_contents_read": False,
            "remote_host_contacted": False,
            "assertions": assertions,
            "passed": all(row["passed"] for row in assertions),
        }
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        if not result["passed"]:
            raise RuntimeError("isolation_assertion_failed")
    except BaseException:
        print(f"Synthetic failure fixture retained: {root}", file=sys.stderr)
        raise
    else:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
