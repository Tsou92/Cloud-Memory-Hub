#!/usr/bin/env python3
"""Install a verified ADR-0060 client into a new, private Python 3.12 environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

COMMANDS = (
    "cloud-memory-agent",
    "cloud-memory-mcp",
    "cloud-memory-connector",
    "cloud-memory-setup",
)


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def inventory(directory: Path) -> dict[str, str]:
    files = {}
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("distribution_unsafe_file")
        if path.is_file() and path != directory / "manifest.json":
            files[path.relative_to(directory).as_posix()] = digest(path)
    return files


def verify(directory: Path) -> dict:
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("distribution_manifest_invalid")
    manifest = json.loads(manifest_path.read_text())
    if type(manifest) is not dict:
        raise ValueError("distribution_manifest_invalid")
    if any(
        type(manifest.get(name)) is not str
        for name in ("kind", "python", "platform", "architecture", "release")
    ):
        raise ValueError("distribution_manifest_invalid")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "cloud-memory-hub-mac-client"
        or manifest.get("python") != "3.12"
        or manifest.get("platform") != "darwin"
        or manifest.get("architecture") not in {"arm64", "x86_64"}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}", manifest.get("release", ""))
    ):
        raise ValueError("distribution_manifest_invalid")
    files = manifest.get("files")
    if type(files) is not dict:
        raise ValueError("distribution_manifest_invalid")
    for name, checksum in files.items():
        if type(name) is not str or type(checksum) is not str:
            raise ValueError("distribution_manifest_invalid")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != name
            or "\\" in name
            or not re.fullmatch(r"[a-f0-9]{64}", checksum)
        ):
            raise ValueError("distribution_manifest_invalid")
    required = {
        "install.py",
        "install.sh",
        "requirements.lock",
        "local-wheels.lock",
        "README.md",
        "skill/cloud-memory-hub/SKILL.md",
    }
    if not required <= files.keys() or inventory(directory) != files:
        raise ValueError("distribution_checksum_mismatch")
    return manifest


def base_interpreter() -> Path:
    executable = getattr(sys, "_base_executable", None)
    if not isinstance(executable, str) or not executable or not Path(executable).is_absolute():
        raise ValueError("python_base_executable_unavailable")
    try:
        resolved = Path(executable).resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError("python_base_executable_unavailable")
    except (OSError, RuntimeError):
        raise ValueError("python_base_executable_unavailable") from None
    return resolved


def install(directory: Path, target: Path, manifest: dict) -> None:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("python_3_12_required")
    if sys.platform != "darwin" or platform.machine() != manifest["architecture"]:
        raise ValueError("distribution_platform_mismatch")
    # A uv shim can make venv record the shim directory as its stdlib home.
    # Resolve this running Python's base; never search PATH for another Python.
    python_base = base_interpreter()
    target = target.absolute()
    if target.exists() or target.is_symlink():
        raise ValueError("installation_target_exists")
    if any(parent.is_symlink() for parent in target.parents):
        raise ValueError("installation_target_symlink")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    try:
        subprocess.run(
            [str(python_base), "-I", "-m", "venv", str(target / "venv")],
            check=True,
            env=environment,
        )
        python = target / "venv/bin/python"
        pip = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
        for requirements, offline in (("requirements.lock", False), ("local-wheels.lock", True)):
            subprocess.run(
                [
                    *pip,
                    "install",
                    "--no-input",
                    "--require-hashes",
                    "--no-deps",
                    "--only-binary=:all:",
                    *(["--no-index"] if offline else []),
                    "-r",
                    requirements,
                ],
                cwd=directory,
                check=True,
                env=environment,
            )
        subprocess.run([*pip, "check"], check=True, env=environment)
        for command in COMMANDS:
            subprocess.run(
                [str(target / "venv/bin" / command), "--help"],
                check=True,
                stdout=subprocess.DEVNULL,
                env=environment,
            )
        shutil.copytree(directory / "skill", target / "skill")
        (target / "installation.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release": manifest["release"],
                    "architecture": manifest["architecture"],
                    "distribution_sha256": digest(directory / "manifest.json"),
                },
                indent=2,
            )
            + "\n"
        )
    except BaseException:
        # Only the new directory exclusively created above belongs to this attempt.
        shutil.rmtree(target)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloud Memory Hub Mac 客户端安装器")
    parser.add_argument("--target", type=Path, help="新的安装目录；不会覆盖已有版本")
    parser.add_argument("--verify-only", action="store_true", help="仅校验安装包，不安装")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = Path(__file__).resolve().parent
        manifest = verify(directory)
        if args.verify_only:
            print("安装包校验通过。")
            return 0
        target = args.target or (
            Path.home()
            / "Library/Application Support/CloudMemoryHub/client-releases"
            / f"{manifest['release']}-{manifest['architecture']}"
        )
        install(directory, target, manifest)
        print(f"客户端已安装：{target.absolute() / 'venv/bin'}")
        print("下一步：使用该目录的 cloud-memory-setup import --pairing-file <网页下载的配对文件>")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        code = str(error) if type(error) is ValueError else "client_installation_failed"
        print(f"安装未完成：{code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
