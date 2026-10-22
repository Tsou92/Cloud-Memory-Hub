"""Fetch the reviewed upstream commit and verify the exact, reviewed patch set."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from scripts.check_repository import REPOSITORY


def git(checkout: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def prepare(root: Path = REPOSITORY) -> Path:
    manifest = json.loads((root / "upstream/manifest.json").read_text())
    checkout = root / ".local/upstream/hy-memory"
    if checkout.is_symlink():
        raise RuntimeError("Refusing a symlinked upstream checkout")
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        git(checkout.parent, "init", str(checkout))
        git(checkout, "remote", "add", "origin", manifest["repository"] + ".git")
    expected_origin = manifest["repository"].removesuffix(".git")
    if git(checkout, "remote", "get-url", "origin").removesuffix(".git") != expected_origin:
        raise RuntimeError("Upstream checkout has a different remote; refusing to change it")
    try:
        head = git(checkout, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        # Only an empty, newly initialized checkout may be populated automatically.
        if git(checkout, "ls-files", "--cached", "--others", "--exclude-standard"):
            raise RuntimeError("Uncommitted files in an uninitialized checkout") from None
        git(checkout, "fetch", "--depth", "1", "origin", manifest["reviewed_commit"])
        git(checkout, "checkout", "--detach", "FETCH_HEAD")
        head = git(checkout, "rev-parse", "HEAD")
    if head != manifest["reviewed_commit"]:
        raise RuntimeError("Upstream commit mismatch; no automatic reset is permitted")
    patch_paths = []
    for item in manifest.get("patches", []):
        path = root / "upstream" / item["path"]
        if path.is_symlink() or not path.resolve().is_relative_to((root / "upstream").resolve()):
            raise RuntimeError("Invalid patch path")
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != item["digest"]:
            raise RuntimeError("Patch checksum mismatch")
        patch_paths.append(path)
    clean = not git(checkout, "status", "--porcelain", "--untracked-files=no")
    if clean:
        for path in patch_paths:
            git(checkout, "apply", "--check", str(path))
            git(checkout, "apply", str(path))
    # Construct the expected tree in a temporary index, without resetting user edits.
    with tempfile.TemporaryDirectory(prefix="cmh-upstream-index-") as temporary:
        environment = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"))
        git(checkout, "read-tree", "HEAD", env=environment)
        for path in patch_paths:
            git(checkout, "apply", "--cached", str(path), env=environment)
        git(checkout, "diff", "--exit-code", "--no-ext-diff", env=environment)
        if git(checkout, "ls-files", "--others", "--exclude-standard", env=environment):
            raise RuntimeError("Unexpected untracked upstream files")
    return checkout


def main() -> int:
    try:
        prepare()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        print("Upstream preparation failed; existing files were not reset. Inspect locally.")
        return 1
    print("Pinned upstream source and reviewed patches verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
