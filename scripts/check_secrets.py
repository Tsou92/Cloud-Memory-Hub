"""Use detect-secrets, never a homegrown credential detector or network verifier."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.check_repository import REPOSITORY, path_problem, source_files

# Reviewed public Git commits and reproducible test-file inventory checksums.
# The latter are the before/after filename lists in upstream patch 0019.
PUBLIC_UPSTREAM_FINGERPRINTS = (
    "0244ebe38ce0288a44a8b6fc7f209ab2bf6c3766",  # pragma: allowlist secret
    "b7d9f0e0f417eae30b192882f848b432484c20a6",  # pragma: allowlist secret
    "f69d3f33df5277febb09557d5bce7adb7d8719ed47e48a71f852e3e4ecf8fd74",  # pragma: allowlist secret
    "cc8c8282371eb13bd4e2a27be388d967e63ad7c58a0226bae3ec8a96b0c2a427",  # pragma: allowlist secret
)


def parse_findings(output: str) -> list[str]:
    try:
        document = json.loads(output)
        results = document["results"]
        if not isinstance(results, dict):
            raise ValueError("invalid results")
        findings = []
        for filename, entries in results.items():
            if not isinstance(entries, list):
                raise ValueError("invalid findings")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("line_number"), int):
                    raise ValueError("invalid finding")
                if not isinstance(entry.get("type"), str):
                    raise ValueError("invalid finding type")
                findings.append(f"{filename}:{entry['line_number']}: {entry['type']}")
        return findings
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            "Secret scanner returned an invalid report; refusing to pass."
        ) from error


def scan_paths(root: Path, paths: list[Path]) -> list[str]:
    findings = []
    for offset in range(0, len(paths), 64):
        batch = paths[offset : offset + 64]
        for relative in batch:
            if path_problem(root, relative):
                raise RuntimeError("Source path policy failed; refusing to scan unsafe paths.")
        try:
            result = subprocess.run(
                [
                    "detect-secrets",
                    "scan",
                    "--no-verify",
                    "--all-files",
                    "--exclude-secrets",
                    "^(?:" + "|".join(PUBLIC_UPSTREAM_FINGERPRINTS) + ")$",
                    "--",
                    *[str(root / path) for path in batch],
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Secret scanner could not complete; refusing to pass.") from error
        if result.returncode:
            raise RuntimeError("Secret scanner failed; refusing to pass.")
        findings.extend(parse_findings(result.stdout))
    return findings


def main() -> int:
    try:
        findings = scan_paths(REPOSITORY, source_files(REPOSITORY))
    except (RuntimeError, OSError, UnicodeError, subprocess.CalledProcessError):
        print("Secret scan could not complete; refusing to pass. Inspect tooling locally.")
        return 1
    for finding in findings:
        print(finding)
    if not findings:
        print("Source secret scan passed; candidate credential network verification disabled.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
