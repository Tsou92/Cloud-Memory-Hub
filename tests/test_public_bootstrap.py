import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = "v0.1.7"
PREFIX = f"cloud-memory-hub-{TAG}"
SOURCE = f"{PREFIX}-source.tar.gz"
FILES = (
    "scripts/install-hub",
    "scripts/install_hub.py",
    "scripts/install_local_docker.py",
    "scripts/install_nas.sh",
    "scripts/install_release_fetch.py",
    "scripts/install_first_device.py",
    "scripts/admin_entry.py",
    "apps/hub/src/cloud_memory_hub/onboarding/connect.py",
)

MOCK_CURL = """#!/bin/sh
set -eu
route=environment
while [ "$#" -gt 0 ]; do
    case "$1" in
        --noproxy) [ "$2" = '*' ]; route=direct; shift ;;
        --proto) [ "$2" = '=https' ]; proto=yes; shift ;;
        --proto-redir) [ "$2" = '=https' ]; proto_redir=yes; shift ;;
        --max-time) maximum_time=$2; shift ;;
        -o) destination=$2; shift ;;
        https://github.com/Tsou92/Cloud-Memory-Hub/releases/download/v0.1.7/*) member=${1##*/} ;;
    esac
    shift
done
[ "$proto" = yes ] && [ "$proto_redir" = yes ]
printf '%s|%s|%s\n' "$member" "$route" "$maximum_time" >> "$CMH_TEST_ATTEMPTS"
if [ "${CMH_TEST_DIRECT_ONLY:-}" = yes ] && [ "$route" != direct ]; then exit 7; fi
if [ "$member" = "$CMH_TEST_TLS_MEMBER" ]; then
    attempt=$(grep -cF "$member|" "$CMH_TEST_ATTEMPTS")
    if [ "$attempt" -le "$CMH_TEST_TLS_FAILURES" ]; then exit 35; fi
fi
cp "$CMH_TEST_FIXTURE/$member" "$destination"
"""

MOCK_DATE = """#!/bin/sh
set -eu
[ "$1" = '+%s' ]
now=1000
if [ -f "$CMH_TEST_CLOCK" ]; then read -r now < "$CMH_TEST_CLOCK"; fi
printf '%s\n' "$((now + 5))" > "$CMH_TEST_CLOCK"
printf '%s\n' "$now"
"""


@unittest.skipUnless(os.name == "posix" and shutil.which("tar"), "POSIX bootstrap test")
class PublicBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cmh-bootstrap-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "test project"
        self.downloads = self.root / "downloads"
        self.bin = self.root / "bin"
        self.scratch = self.root / "scratch"
        self.attempts = self.root / "attempts"
        for directory in (self.project, self.downloads, self.bin, self.scratch):
            directory.mkdir()
        for name, script in (("curl", MOCK_CURL), ("date", MOCK_DATE), ("sleep", "#!/bin/sh\n")):
            fake = self.bin / name
            fake.write_text(script, encoding="utf-8")
            fake.chmod(0o700)

    def make_release(self, *, installer_kind="file", expected=None):
        source = self.downloads / SOURCE
        with tarfile.open(source, "w:gz") as archive:
            for name in FILES:
                info = tarfile.TarInfo(f"{PREFIX}/{name}")
                if name == "scripts/install-hub" and installer_kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/nonexistent-cmh-bootstrap-fixture"
                    archive.addfile(info)
                    continue
                data = (
                    b'#!/bin/sh\nprintf \'%s\\n\' "$PWD" "$@"\n'
                    if name == "scripts/install-hub"
                    else b"synthetic installer dependency\n"
                )
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        checksum = expected or hashlib.sha256(source.read_bytes()).hexdigest()
        (self.downloads / "SHA256SUMS").write_text(f"{checksum}  {SOURCE}\n", encoding="ascii")

    def run_bootstrap(self, *args, direct_only=False, tls_member="", tls_failures=0):
        return subprocess.run(
            ["sh", str(ROOT / "scripts/bootstrap-install.sh"), *args],
            cwd=self.project,
            env={
                **os.environ,
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "TMPDIR": str(self.scratch),
                "CMH_TEST_FIXTURE": str(self.downloads),
                "CMH_TEST_DIRECT_ONLY": "yes" if direct_only else "",
                "CMH_TEST_ATTEMPTS": str(self.attempts),
                "CMH_TEST_CLOCK": str(self.root / "clock"),
                "CMH_TEST_TLS_MEMBER": tls_member,
                "CMH_TEST_TLS_FAILURES": str(tls_failures),
            },
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_verified_source_keeps_project_and_args_through_direct_fallback(self):
        self.make_release()
        args = ["--nas", "user@nas", "--project", "Shared project", "--agent", "codex"]
        result = self.run_bootstrap(*args, direct_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.project), *args, "--tag", TAG])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_retries_transient_tls_failures_and_reports_stages(self):
        self.make_release()
        result = self.run_bootstrap("--nas", "user@nas", tls_member="SHA256SUMS", tls_failures=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("curl 35"), 2)
        self.assertIn("发行源码包校验通过，正在启动安装器", result.stderr)
        self.assertEqual(
            self.attempts.read_text().splitlines(),
            [
                "SHA256SUMS|environment|55",
                "SHA256SUMS|direct|45",
                "SHA256SUMS|environment|35",
                f"{SOURCE}|environment|175",
            ],
        )

    def test_source_tls_failure_stops_after_four_attempts_without_handoff(self):
        self.make_release()
        result = self.run_bootstrap("--nas", "user@nas", tls_member=SOURCE, tls_failures=100)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"官方发行附件 {SOURCE}（curl 35）", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(
            self.attempts.read_text().splitlines(),
            [
                "SHA256SUMS|environment|55",
                f"{SOURCE}|environment|175",
                f"{SOURCE}|direct|165",
                f"{SOURCE}|environment|155",
                f"{SOURCE}|direct|145",
            ],
        )

    def test_corrupt_source_never_runs_the_installer(self):
        self.make_release(expected="0" * 64)
        result = self.run_bootstrap("--nas", "user@nas")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("源码包校验失败", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_verified_source_with_linked_installer_still_fails_closed(self):
        self.make_release(installer_kind="symlink")
        result = self.run_bootstrap("--nas", "user@nas")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("安装文件不是普通文件", result.stderr)
        self.assertEqual(result.stdout, "")
