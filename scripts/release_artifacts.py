"""R3-04/05: bind existing tags, tested artifacts and a verified draft release.

Builds remain in the existing Dockerfile and Mac builder. Only the explicit
``publish`` command writes GitHub, and it never creates tags or replaces assets.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from scripts.check_repository import REPOSITORY
from scripts.client_distribution.install import digest
from scripts.client_distribution.install import verify as verify_client

MAX_ASSET_BYTES = 2 * 1024**3
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseError(ValueError):
    """Fixed codes; raw subprocess/provider output never enters reports."""


def require(condition, code):
    if not condition:
        raise ReleaseError(code)


def command(arguments, *, cwd=None):
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=False)
    require(result.returncode == 0, "release_command_failed")
    return result.stdout.strip()


def validate_source(root, tag, commit, ref=None, ref_type=None):
    require(isinstance(tag, str) and TAG.fullmatch(tag), "release_tag_invalid")
    require(isinstance(commit, str) and COMMIT.fullmatch(commit), "release_commit_invalid")
    require(ref is None or ref == "refs/tags/" + tag, "release_dispatch_ref_mismatch")
    require(ref_type is None or ref_type == "tag", "release_dispatch_ref_mismatch")

    def git(*args):
        return command(["git", *args], cwd=root)

    require(git("rev-parse", "HEAD") == commit, "release_checkout_mismatch")
    require(
        git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") == commit,
        "release_tag_commit_mismatch",
    )
    require(not git("status", "--porcelain", "--untracked-files=all"), "release_source_dirty")
    version = tomllib.loads((root / "apps/hub/pyproject.toml").read_text())["project"]["version"]
    require(version == tag[1:], "release_version_mismatch")
    return {"tag": tag, "version": version, "commit": commit}


def names(identity):
    tag = identity["tag"]
    return {
        "source": f"cloud-memory-hub-{tag}-source.tar.gz",
        "image": f"cloud-memory-hub-{tag}-linux-amd64.tar.gz",
        "client": f"cloud-memory-hub-client-{tag}-macos-arm64.tar.gz",
        "provenance": f"cloud-memory-hub-{tag}-provenance.tar.gz",
    }


def file_record(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "release_asset_not_regular")
    require(0 < info.st_size < MAX_ASSET_BYTES, "release_asset_size_invalid")
    return {"filename": path.name, "bytes": info.st_size, "sha256": digest(path)}


def write_json(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2)
        stream.write("\n")


def read_json(path):
    require(
        path.stat().st_size <= 2 * 1024**2 and not path.is_symlink(), "release_metadata_invalid"
    )
    return json.loads(path.read_text())


def archive_json(archive, member):
    info = archive.getmember(member)
    require(info.isfile() and info.size <= 2 * 1024**2, "release_archive_metadata_invalid")
    with archive.extractfile(info) as stream:
        return json.load(stream)


def image_receipt(directory, identity, inspection):
    path = directory / names(identity)["image"]
    record = file_record(path)
    require(isinstance(inspection, list) and len(inspection) == 1, "release_image_inspect_invalid")
    image = inspection[0]
    image_ref = "cloud-memory-hub:" + identity["tag"]
    require(
        image.get("Os") == "linux" and image.get("Architecture") == "amd64",
        "release_image_platform_mismatch",
    )
    image_id = image.get("Id", "")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id), "release_image_identity_invalid")
    require(
        image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
        == identity["commit"],
        "release_image_revision_mismatch",
    )
    # Prove docker-save contains the tested config/image, not merely a named file.
    with tarfile.open(path, "r:gz") as archive:
        manifests = archive_json(archive, "manifest.json")
        require(
            isinstance(manifests, list) and len(manifests) == 1, "release_image_archive_invalid"
        )
        manifest = manifests[0]
        require(manifest.get("RepoTags") == [image_ref], "release_image_tag_mismatch")
        config = manifest["Config"]
        info = archive.getmember(config)
        require(info.isfile() and info.size <= 2 * 1024**2, "release_image_archive_invalid")
        with archive.extractfile(info) as stream:
            encoded = stream.read()
        require(
            "sha256:" + hashlib.sha256(encoded).hexdigest() == image_id,
            "release_image_config_mismatch",
        )
        configuration = json.loads(encoded)
        require(
            configuration.get("os") == "linux"
            and configuration.get("architecture") == "amd64"
            and configuration.get("config", {})
            .get("Labels", {})
            .get("org.opencontainers.image.revision")
            == identity["commit"],
            "release_image_config_mismatch",
        )
    receipt = {
        **identity,
        "artifact": record,
        "image_ref": image_ref,
        "image_id": image_id,
        "platform": "linux/amd64",
    }
    write_json(directory / "image-receipt.json", receipt)
    return receipt


def unpack_client(archive_path, target, identity):
    file_record(archive_path)
    require(not target.exists() and not target.is_symlink(), "release_unpack_target_exists")
    target.mkdir(mode=0o700, parents=True)
    prefix = names(identity)["client"].removesuffix(".tar.gz")
    seen, total = set(), 0
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                require(
                    not relative.is_absolute()
                    and ".." not in relative.parts
                    and "\\" not in member.name
                    and relative.parts
                    and relative.parts[0] == prefix
                    and relative.as_posix() == member.name.rstrip("/")
                    and relative.as_posix() not in seen
                    and (member.isdir() or member.isfile()),
                    "release_client_archive_unsafe",
                )
                seen.add(relative.as_posix())
                total += member.size
                require(len(seen) <= 50000 and total <= 8 * 1024**3, "release_client_archive_limit")
                destination = target / relative
                if member.isdir():
                    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
        package = target / prefix
        manifest = verify_client(package)
        require(
            manifest.get("hub_commit") == identity["commit"]
            and manifest.get("hub_worktree_dirty") is False
            and manifest.get("release") == identity["version"]
            and manifest.get("architecture") == "arm64",
            "release_client_identity_mismatch",
        )
        return package
    except BaseException:
        shutil.rmtree(target)
        raise


def client_receipt(directory, target, identity):
    path = directory / names(identity)["client"]
    package = unpack_client(path, target, identity)
    receipt = {
        **identity,
        "artifact": file_record(path),
        "platform": "macos/arm64",
        "manifest_sha256": digest(package / "manifest.json"),
    }
    write_json(directory / "client-receipt.json", receipt)
    return receipt


def source_archive(root, output, identity):
    with (
        output.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed,
    ):
        process = subprocess.Popen(
            [
                "git",
                "archive",
                "--format=tar",
                "--prefix=cloud-memory-hub-" + identity["tag"] + "/",
                identity["commit"],
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            shutil.copyfileobj(process.stdout, compressed)
        finally:
            process.stdout.close()
        require(process.wait() == 0, "release_source_archive_failed")


def provenance_archive(output, files):
    with (
        output.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, path in sorted(files.items()):
                require(path.is_file() and not path.is_symlink(), "release_provenance_invalid")
                info = tarfile.TarInfo(name)
                info.size, info.mode = path.stat().st_size, 0o644
                with path.open("rb") as source:
                    archive.addfile(info, source)


def verify_receipt(directory, kind, identity):
    receipt = read_json(directory / (kind + "-receipt.json"))
    require(
        all(receipt.get(key) == value for key, value in identity.items()),
        "release_receipt_identity_mismatch",
    )
    expected = names(identity)[kind]
    require(
        receipt.get("artifact") == file_record(directory / expected),
        "release_receipt_checksum_mismatch",
    )
    require(
        {path.name for path in directory.iterdir()} == {expected, kind + "-receipt.json"},
        "release_receipt_files_mismatch",
    )
    return receipt


def assemble(root, inputs, output, identity):
    image = verify_receipt(inputs / "release-linux", "image", identity)
    client = verify_receipt(inputs / "release-mac", "client", identity)
    require(not output.exists() and not output.is_symlink(), "release_output_exists")
    output.mkdir(mode=0o700, parents=True)
    filenames = names(identity)
    for kind, directory in (("image", "release-linux"), ("client", "release-mac")):
        shutil.copyfile(inputs / directory / filenames[kind], output / filenames[kind])
    source_archive(root, output / filenames["source"], identity)
    write_json(
        output / "release.json", {"schema_version": 1, **identity, "image": image, "client": client}
    )
    with tempfile.TemporaryDirectory(prefix="cmh-release-provenance-") as temporary:
        package = unpack_client(output / filenames["client"], Path(temporary) / "client", identity)
        provenance = {
            "LICENSE": root / "LICENSE",
            "NOTICE": root / "NOTICE",
            "release.json": output / "release.json",
        }
        provenance.update(
            {
                "client/" + path.relative_to(package / "provenance").as_posix(): path
                for path in (package / "provenance").rglob("*")
                if path.is_file()
            }
        )
        require(
            "client/upstream-manifest.json" in provenance and "client/uv.lock" in provenance,
            "release_provenance_missing",
        )
        provenance_archive(output / filenames["provenance"], provenance)
    notes = (
        f"# Cloud Memory Hub {identity['tag']}\n\n源码提交：`{identity['commit']}`。\n\n"
        "本次制品：NAS Linux amd64、Mac arm64 / Python 3.12。先核对 SHA256SUMS。\n\n"
        "镜像使用 `gzip -dc <linux-amd64.tar.gz> | docker load` 导入；在 Compose 的 .env 中将 "
        f"CMH_IMAGE 设置为 `{image['image_ref']}`。Mac 包解压后按包内 README 安装。\n\n"
        "安装、升级与备份恢复请阅读源码包中的 README.md、docs/client-installation.md "
        "和 docs/backup-and-recovery.md。许可证与依赖来源见 provenance 包；其中上游 SBOM "
        "只代表复用来源，不能当作完整产品 SBOM。\n\n" + (root / "CHANGELOG.md").read_text()
    )
    (output / "RELEASE_NOTES.md").write_text(notes)
    records = [file_record(path) for path in sorted(output.iterdir())]
    (output / "SHA256SUMS").write_text(
        "".join(f"{r['sha256']}  {r['filename']}\n" for r in records)
    )
    return verify_bundle(output, identity)


def verify_bundle(directory, identity):
    expected = {*names(identity).values(), "release.json", "RELEASE_NOTES.md"}
    require(
        {path.name for path in directory.iterdir()} == expected | {"SHA256SUMS"},
        "release_assets_mismatch",
    )
    records = {path.name: file_record(path) for path in directory.iterdir()}
    entries = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None and match[2] not in entries, "release_checksums_invalid")
        entries[match[2]] = match[1]
    require(set(entries) == expected, "release_checksums_invalid")
    require(
        all(entries[name] == records[name]["sha256"] for name in expected),
        "release_checksum_mismatch",
    )
    metadata = read_json(directory / "release.json")
    require(
        metadata.get("schema_version") == 1
        and all(metadata.get(k) == v for k, v in identity.items()),
        "release_bundle_identity_mismatch",
    )
    for kind in ("image", "client"):
        receipt = metadata[kind]
        require(
            all(receipt.get(k) == v for k, v in identity.items())
            and receipt.get("artifact") == records[names(identity)[kind]],
            "release_receipt_checksum_mismatch",
        )
    return records


def gh_api(path):
    result = subprocess.run(
        ["gh", "api", "--include", "--method", "GET", path],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.match(r"HTTP/[0-9.]+ ([0-9]{3})[^\n]*\n", result.stdout)
    require(match is not None, "release_remote_response_unavailable")
    status = int(match[1])
    require(result.returncode == 0 and status == 200, "release_remote_request_failed")
    parts = re.split(r"\r?\n\r?\n", result.stdout, maxsplit=1)
    require(len(parts) == 2, "release_remote_response_invalid")
    return json.loads(parts[1])


def find_release(repository, tag):
    # The by-tag REST endpoint does not return drafts. A write-capable token's
    # complete list includes them; only a successful exhausted list means absent.
    found = None
    for page in range(1, 101):
        values = gh_api(f"repos/{repository}/releases?per_page=100&page={page}")
        require(isinstance(values, list), "release_remote_list_invalid")
        for value in values:
            require(isinstance(value, dict), "release_remote_list_invalid")
            if value.get("tag_name") == tag:
                require(found is None, "release_remote_tag_ambiguous")
                require(
                    type(value.get("id")) is int and value["id"] > 0, "release_remote_list_invalid"
                )
                found = value
        if len(values) < 100:
            return found
    raise ReleaseError("release_remote_list_incomplete")


def remote_tag(repository, identity):
    value = gh_api(f"repos/{repository}/git/ref/tags/{identity['tag']}")["object"]
    for _ in range(10):
        require(COMMIT.fullmatch(value.get("sha", "")), "release_remote_tag_invalid")
        if value.get("type") == "commit":
            require(value["sha"] == identity["commit"], "release_remote_tag_moved")
            return
        require(value.get("type") == "tag", "release_remote_tag_invalid")
        value = gh_api(f"repos/{repository}/git/tags/{value['sha']}")["object"]
    raise ReleaseError("release_remote_tag_invalid")


def remote_assets(repository, release_id, records):
    found = {}
    for page in range(1, 12):
        values = gh_api(f"repos/{repository}/releases/{release_id}/assets?per_page=100&page={page}")
        require(isinstance(values, list), "release_remote_assets_invalid")
        for value in values:
            name = value.get("name")
            require(
                name in records
                and name not in found
                and value.get("state") == "uploaded"
                and value.get("size") == records[name]["bytes"],
                "release_remote_assets_mismatch",
            )
            found[name] = value
        if len(values) < 100:
            require(set(found) == set(records), "release_remote_assets_mismatch")
            return
    raise ReleaseError("release_remote_assets_invalid")


def publish(directory, identity, repository):
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository),
        "release_repository_invalid",
    )
    records = verify_bundle(directory, identity)
    remote_tag(repository, identity)
    require(find_release(repository, identity["tag"]) is None, "release_already_exists")
    base = ["gh", "release"]
    command(
        [
            *base,
            "create",
            identity["tag"],
            "--repo",
            repository,
            "--verify-tag",
            "--draft",
            "--target",
            identity["commit"],
            "--title",
            "Cloud Memory Hub " + identity["tag"],
            "--notes-file",
            str(directory / "RELEASE_NOTES.md"),
            "--latest=false",
            *(["--prerelease"] if "rc" in identity["version"] else []),
        ]
    )
    created = find_release(repository, identity["tag"])
    require(created is not None and created.get("draft") is True, "release_draft_not_found")
    endpoint = f"repos/{repository}/releases/{created['id']}"
    release = gh_api(endpoint)
    require(
        release.get("id") == created["id"]
        and release.get("draft") is True
        and release.get("tag_name") == identity["tag"]
        and release.get("prerelease") is ("rc" in identity["version"]),
        "release_draft_identity_mismatch",
    )
    command(
        [
            *base,
            "upload",
            identity["tag"],
            "--repo",
            repository,
            *[str(directory / name) for name in sorted(records)],
        ]
    )
    remote_assets(repository, release["id"], records)
    with tempfile.TemporaryDirectory(prefix="cmh-release-download-") as temporary:
        command(
            [
                *base,
                "download",
                identity["tag"],
                "--repo",
                repository,
                "--pattern",
                "*",
                "--dir",
                temporary,
            ]
        )
        require(verify_bundle(Path(temporary), identity) == records, "release_download_mismatch")
    remote_tag(repository, identity)
    try:
        command(
            [
                *base,
                "edit",
                identity["tag"],
                "--repo",
                repository,
                "--verify-tag",
                "--draft=false",
                "--latest=false" if "rc" in identity["version"] else "--latest=true",
            ]
        )
        final = gh_api(endpoint)
        require(
            final.get("draft") is False
            and final.get("id") == release["id"]
            and final.get("tag_name") == identity["tag"]
            and final.get("prerelease") is ("rc" in identity["version"]),
            "release_publish_result_unknown",
        )
    except Exception:
        raise ReleaseError("release_publish_result_unknown") from None
    return {**identity, "state": "published", "asset_count": len(records)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["validate", "image", "client", "assemble", "verify", "publish"]
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--ref")
    parser.add_argument("--ref-type")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--inspect", type=Path)
    parser.add_argument("--unpack-to", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repository")
    args = parser.parse_args()
    try:
        identity = validate_source(REPOSITORY, args.tag, args.commit, args.ref, args.ref_type)
        if args.command == "validate":
            result = identity
            if args.github_output:
                with args.github_output.open("a") as stream:
                    stream.write("".join(f"{key}={value}\n" for key, value in identity.items()))
        elif args.command == "image":
            result = image_receipt(args.directory, identity, read_json(args.inspect))
        elif args.command == "client":
            result = client_receipt(args.directory, args.unpack_to, identity)
        elif args.command == "assemble":
            result = assemble(REPOSITORY, args.directory, args.output, identity)
        elif args.command == "verify":
            result = verify_bundle(args.directory, identity)
        else:
            result = publish(args.directory, identity, args.repository)
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        code = (
            str(error) if isinstance(error, ReleaseError) else "release_artifact_operation_failed"
        )
        print(json.dumps({"error": code}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
