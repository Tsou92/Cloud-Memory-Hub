"""Build the ADR-0060 Mac installer from reviewed sources and the Hub lockfile."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

from scripts.check_repository import REPOSITORY, path_problem, source_files
from scripts.client_distribution.install import digest, inventory, verify
from scripts.prepare_upstream import git, prepare

UV = ["uv", "tool", "run", "--from", "uv==0.12.5", "uv"]
PRODUCTS = {"cloud-memory-hub", "one-memory", "hy-memory"}
BUILD_CONSTRAINTS = "uv_build==0.12.5\nsetuptools==84.0.0\nwheel==0.45.1\npackaging==26.3\n"


def run(arguments: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=True).stdout


def export_requirements(root: Path, excluded: set[str]) -> str:
    arguments = [
        *UV,
        "export",
        "--frozen",
        "--no-dev",
        "--no-header",
        "--no-annotate",
        "--no-emit-project",
        "--directory",
        str(root / "apps/hub"),
    ]
    for package in sorted(excluded):
        arguments += ["--no-emit-package", package]
    text = run(arguments)
    # Only pinned registry requirements and their hashes may cross this boundary.
    records = text.replace("\\\n", " ").splitlines()
    for record in records:
        if not record.strip():
            continue
        if (
            not re.match(r"^[a-zA-Z0-9_.-]+(?:\[[a-zA-Z0-9_,.-]+\])?==[^\s;]+", record)
            or "--hash=sha256:" not in record
            or any(value in record for value in (" @ ", "file:", "--index", "--extra-index", "-e "))
        ):
            raise ValueError("unlocked_or_local_requirement")
    return text


def stage_project(source: Path, target: Path, code_directory: str) -> dict[str, str]:
    files = source_files(source)
    selected = [
        name
        for name in files
        if name.parts[0] == code_directory
        or name.as_posix() in {"pyproject.toml", "README.md", "LICENSE", "NOTICE"}
    ]
    if Path("pyproject.toml") not in selected:
        raise ValueError("project_metadata_missing")
    checksums = {}
    for relative in selected:
        original = source / relative
        if path_problem(source, relative):
            raise ValueError("source_file_invalid")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        checksums[relative.as_posix()] = digest(destination)
    return checksums


def validate_wheel(path: Path, expected: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if any(
            name.endswith((".pth", ".pyc")) or ".." in Path(name).parts or Path(name).is_absolute()
            for name in names
        ):
            raise ValueError("wheel_contains_nonportable_files")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("wheel_metadata_invalid")
        text = wheel.read(metadata[0]).decode()
        if f"Name: {expected}\n" not in text or " @ file:" in text:
            raise ValueError("wheel_metadata_invalid")
        if not path.name.endswith("-none-any.whl"):
            raise ValueError("unexpected_native_project_wheel")


def build_wheel(source: Path, output: Path, constraints: Path, python: str, expected: str) -> Path:
    before = set(output.glob("*.whl"))
    run(
        [
            *UV,
            "build",
            "--wheel",
            "--python",
            python,
            "--no-python-downloads",
            "--build-constraints",
            str(constraints),
            "--out-dir",
            str(output),
            str(source),
        ]
    )
    created = set(output.glob("*.whl")) - before
    if len(created) != 1:
        raise ValueError("wheel_build_output_invalid")
    wheel = created.pop()
    validate_wheel(wheel, expected)
    return wheel


def download_locked_sdist(package: dict, directory: Path) -> Path:
    sdist = package["sdist"]
    if not sdist["url"].startswith("https://files.pythonhosted.org/"):
        raise ValueError("sdist_source_not_registry")
    destination = directory / f"{package['name']}-{package['version']}.tar.gz"
    with urllib.request.urlopen(sdist["url"], timeout=120) as response:
        with destination.open("xb") as stream:
            shutil.copyfileobj(response, stream)
    if "sha256:" + digest(destination) != sdist["hash"]:
        raise ValueError("sdist_checksum_mismatch")
    return destination


def build(root: Path, output: Path, release: str, python: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}", release):
        raise ValueError("release_name_invalid")
    architecture = platform.machine()
    if sys.platform != "darwin" or architecture not in {"arm64", "x86_64"}:
        raise ValueError("native_macos_build_required")
    identity = json.loads(
        run(
            [
                python,
                "-I",
                "-c",
                (
                    "import json,platform,sys;print(json.dumps([list(sys.version_info[:2]),"
                    "sys.platform,platform.machine()]))"
                ),
            ]
        )
    )
    if identity != [[3, 12], "darwin", architecture]:
        raise ValueError("native_python_3_12_required")
    archive = output.with_name(output.name + ".tar.gz")
    if output.exists() or output.is_symlink() or archive.exists():
        raise ValueError("distribution_output_exists")
    upstream = prepare(root)
    lockfile = root / "apps/hub/uv.lock"
    lock = tomllib.loads(lockfile.read_text())
    requirements = export_requirements(root, PRODUCTS)
    runtime_names = {line.split("==", 1)[0] for line in requirements.splitlines() if "==" in line}
    source_only = [
        package
        for package in lock["package"]
        if package["name"] in runtime_names and package.get("sdist") and not package.get("wheels")
    ]
    source_only_names = {package["name"] for package in source_only}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cmh-client-build-", dir=output.parent) as temporary:
        work = Path(temporary)
        distribution = work / output.name
        wheels = distribution / "wheels"
        wheels.mkdir(parents=True)
        constraints = work / "build-constraints.txt"
        constraints.write_text(BUILD_CONSTRAINTS)
        sources = {}
        projects = [
            ("cloud-memory-hub", root / "apps/hub", "src"),
            ("one-memory", upstream / "memhub/apps/core", "src"),
            ("hy-memory", upstream, "hy_memory"),
        ]
        for name, source, code in projects:
            staged = work / "sources" / name
            sources[name] = stage_project(source, staged, code)
            build_wheel(staged, wheels, constraints, python, name)
        for package in source_only:
            sdist = download_locked_sdist(package, work)
            build_wheel(sdist, wheels, constraints, python, package["name"])
        (distribution / "requirements.lock").write_text(
            export_requirements(root, PRODUCTS | source_only_names)
        )
        (distribution / "local-wheels.lock").write_text(
            "".join(
                f"./wheels/{wheel.name} --hash=sha256:{digest(wheel)}\n"
                for wheel in sorted(wheels.glob("*.whl"))
            )
        )
        for name in ("install.py", "install.sh", "README.md"):
            shutil.copyfile(root / "scripts/client_distribution" / name, distribution / name)
        (distribution / "install.sh").chmod(0o755)
        skill_root = root / "packages/agent-skill/cloud-memory-hub"
        # Public template whitelist: never collect an installed local-connection binding.
        for relative in (Path("SKILL.md"),):
            source = skill_root / relative
            if source.is_symlink() or not source.is_file():
                raise ValueError("skill_template_file_invalid")
            destination = distribution / "skill/cloud-memory-hub" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        provenance = distribution / "provenance"
        provenance.mkdir()
        shutil.copyfile(root / "upstream/manifest.json", provenance / "upstream-manifest.json")
        shutil.copyfile(lockfile, provenance / "uv.lock")
        shutil.copyfile(constraints, provenance / "build-constraints.txt")
        for source, name in (
            (upstream / "LICENSE", "LICENSE.hy-memory"),
            (upstream / "memhub/LICENSE", "LICENSE.one-memory"),
            (upstream / "memhub/NOTICE", "NOTICE.one-memory"),
            (upstream / "memhub/THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.upstream.md"),
            (upstream / "memhub/SBOM.spdx.json", "SBOM.upstream.spdx.json"),
        ):
            shutil.copyfile(source, provenance / name)
        if (root / "LICENSE").is_file():
            shutil.copyfile(root / "LICENSE", provenance / "LICENSE.cloud-memory-hub")
        if (root / "NOTICE").is_file():
            shutil.copyfile(root / "NOTICE", provenance / "NOTICE.cloud-memory-hub")
        upstream_manifest = json.loads((root / "upstream/manifest.json").read_text())
        for patch in upstream_manifest["patches"]:
            destination = provenance / patch["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "upstream" / patch["path"], destination)
        (provenance / "source-files.json").write_text(json.dumps(sources, indent=2) + "\n")
        (provenance / "rebuilt-dependencies.json").write_text(
            json.dumps(
                [
                    {"name": p["name"], "version": p["version"], "sdist": p["sdist"]}
                    for p in source_only
                ],
                indent=2,
            )
            + "\n"
        )
        manifest = {
            "schema_version": 1,
            "kind": "cloud-memory-hub-mac-client",
            "release": release,
            "platform": "darwin",
            "architecture": architecture,
            "python": "3.12",
            "hub_commit": git(root, "rev-parse", "HEAD"),
            "hub_worktree_dirty": bool(git(root, "status", "--porcelain")),
            "upstream_commit": upstream_manifest["reviewed_commit"],
            "lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
            "files": inventory(distribution),
        }
        (distribution / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        verify(distribution)
        shutil.make_archive(str(work / "archive"), "gztar", root_dir=work, base_dir=output.name)
        distribution.rename(output)
        (work / "archive.tar.gz").rename(archive)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--release", required=True)
    parser.add_argument("--python", default="python3.12", help="Native Python 3.12 executable")
    args = parser.parse_args()
    try:
        archive = build(REPOSITORY, args.output.absolute(), args.release, args.python)
        print(f"Client installer built: {archive}")
        print(f"SHA-256: {digest(archive)}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        print("Client build failed; no existing distribution was replaced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
