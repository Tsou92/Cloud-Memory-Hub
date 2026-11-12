"""Verify the PowerShell release bootstrap using synthetic official-asset stand-ins."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts/bootstrap-install.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
WINDOWS_POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
TAG = "v0.1.7"
PREFIX = f"cloud-memory-hub-{TAG}"
ARCHIVE = f"{PREFIX}-source.tar.gz"
FILES = (
    "scripts/install_hub.py",
    "scripts/install_local_docker.py",
    "scripts/install_nas.sh",
    "scripts/install_release_fetch.py",
    "scripts/install_first_device.py",
    "scripts/admin_entry.py",
    "apps/hub/src/cloud_memory_hub/onboarding/connect.py",
)
FAKE_INSTALLER = """import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
Path(os.environ["CMH_FIXTURE_MARKER"]).write_text(
    json.dumps({
        "args": sys.argv[1:],
        "cwd": str(Path.cwd()),
        "files": sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()),
    }),
    encoding="utf-8",
)
"""
FAKE_CURL = r"""
$ErrorActionPreference = 'Stop'
$offset = [Array]::IndexOf($args, '-o')
$url = $args[$offset - 1]
$target = $args[$offset + 1]
$base = 'https://github.com/Tsou92/Cloud-Memory-Hub/releases/download/v0.1.7/'
if (-not $url.StartsWith($base) -or
    $args -notcontains '--proto' -or $args -notcontains '--proto-redir') {
    throw 'unexpected curl request'
}
$member = $url.Substring($base.Length)
Add-Content -LiteralPath $env:CMH_FIXTURE_TRACE -Value $member -Encoding ascii
$attempts = @(Get-Content -LiteralPath $env:CMH_FIXTURE_TRACE | Where-Object { $_ -eq $member })
if ($env:CMH_FIXTURE_RETRY -eq '1' -and $attempts.Count -lt 3) {
    $global:LASTEXITCODE = 35
    return
}
[IO.File]::Copy((Join-Path $env:CMH_FIXTURE_ASSETS $member), $target, $true)
$global:LASTEXITCODE = 0
"""
DRIVER = r"""
$ErrorActionPreference = 'Stop'
function Get-Command {
    [CmdletBinding()]
    param([Parameter(Position=0)][string]$Name, [Management.Automation.CommandTypes]$CommandType)
    if ($Name -in @('curl.exe', 'curl')) {
        [pscustomobject]@{ Source = $env:CMH_FIXTURE_CURL }
        return
    }
    if ($Name -eq 'ssh.exe') {
        if ($env:CMH_FIXTURE_DEPLOY -ne '1') { throw 'verify-only probed SSH' }
        [pscustomobject]@{ Source = 'synthetic-ssh.exe' }
        return
    }
    Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}
$script = [scriptblock]::Create([IO.File]::ReadAllText($env:CMH_FIXTURE_BOOTSTRAP))
if ($env:CMH_FIXTURE_DEPLOY -eq 'local') {
    & $script -Local -Port 8890
} elseif ($env:CMH_FIXTURE_DEPLOY -eq '1') {
    & $script -Nas 'nas-fixture' -SshPort 2222 `
        -Origin 'https://hub.example.invalid' -TailscaleContainer 'edge'
} else {
    & $script -VerifyOnly
}
"""


@unittest.skipUnless(POWERSHELL, "PowerShell is required for the NAS bootstrap tests")
class PowerShellNASBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cmh-powershell-bootstrap-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.curl = self.root / "fake-curl.ps1"
        self.curl.write_text(FAKE_CURL, encoding="utf-8")
        self.driver = self.root / "driver.ps1"
        self.driver.write_text(DRIVER, encoding="utf-8")
        self.marker = self.root / "installer.json"
        self.trace = self.root / "downloads.log"

    def release(self, *, bad_checksum=False, linked_installer=False):
        archive = self.root / ARCHIVE
        with tarfile.open(archive, "w:gz") as source:
            for name in (*FILES, "../unexpected"):
                entry = tarfile.TarInfo(f"{PREFIX}/{name}")
                if linked_installer and name == FILES[0]:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "unexpected"
                    source.addfile(entry)
                    continue
                content = FAKE_INSTALLER.encode() if name == FILES[0] else b"synthetic fixture"
                entry.size = len(content)
                source.addfile(entry, io.BytesIO(content))
        digest = "0" * 64 if bad_checksum else hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.root / "SHA256SUMS").write_text(f"{digest}  {ARCHIVE}\n", encoding="ascii")

    def run_entrypoint(self, *, retry=False, deploy="0", executable=POWERSHELL):
        return subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.driver),
            ],
            cwd=self.project,
            env={
                **os.environ,
                "CMH_FIXTURE_CURL": str(self.curl),
                "CMH_FIXTURE_BOOTSTRAP": str(BOOTSTRAP),
                "CMH_FIXTURE_ASSETS": str(self.root),
                "CMH_FIXTURE_MARKER": str(self.marker),
                "CMH_FIXTURE_TRACE": str(self.trace),
                "CMH_FIXTURE_RETRY": "1" if retry else "0",
                "CMH_FIXTURE_DEPLOY": deploy,
            },
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=45,
            check=False,
        )

    def test_verify_only_uses_verified_files_and_preserves_cwd_after_tls_retry(self):
        self.release()
        result = self.run_entrypoint(retry=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(
            report, {"args": ["--help"], "cwd": str(self.project), "files": sorted(FILES)}
        )
        self.assertEqual(self.trace.read_text().splitlines(), ["SHA256SUMS"] * 3 + [ARCHIVE] * 3)

    def test_mismatched_checksum_and_linked_installer_stop_before_execution(self):
        for case in ("bad_checksum", "linked_installer"):
            with self.subTest(case=case):
                self.release(**{case: True})
                result = self.run_entrypoint()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.marker.exists())
                if case == "linked_installer":
                    self.assertIn("cmh_release_member_invalid", result.stdout + result.stderr)

    def assert_nas_only_options_are_forwarded(self, executable):
        self.release()
        result = self.run_entrypoint(deploy="1", executable=executable)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(
            report["args"],
            [
                "--tag",
                TAG,
                "--nas",
                "nas-fixture",
                "--nas-only",
                "--ssh-port",
                "2222",
                "--origin",
                "https://hub.example.invalid",
                "--tailscale-container",
                "edge",
            ],
        )

    @unittest.skipUnless(os.name == "nt", "Windows-only deployment branch")
    def test_nas_only_options_are_forwarded_on_windows(self):
        self.assert_nas_only_options_are_forwarded(POWERSHELL)

    @unittest.skipUnless(os.name == "nt", "Windows-only deployment branch")
    def test_local_deployment_never_probes_ssh(self):
        self.release()
        result = self.run_entrypoint(deploy="local")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(report["args"], ["--tag", TAG, "--local", "--port", "8890"])

    @unittest.skipUnless(WINDOWS_POWERSHELL, "Windows PowerShell 5.1 is required")
    def test_windows_powershell_51_forwards_nas_only_options(self):
        self.assert_nas_only_options_are_forwarded(WINDOWS_POWERSHELL)
