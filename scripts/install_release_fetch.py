"""Fetch and verify the public NAS artifacts used by the first installer."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPOSITORY = "Tsou92/Cloud-Memory-Hub"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases"
DOWNLOAD_BASE = f"https://github.com/{REPOSITORY}/releases/download"
RELEASE_HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
TAG = re.compile(r"v(?:0|[1-9][0-9]{0,7})\.(?:0|[1-9][0-9]{0,7})\.(?:0|[1-9][0-9]{0,7})")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)")
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_METADATA_BYTES = 2 * 1024**2
MAX_SOURCE_BYTES = 128 * 1024**2
MAX_IMAGE_BYTES = 2 * 1024**3


class ReleaseFetchError(ValueError):
    """Report a fixed error code without remote URLs or subprocess output."""


def require(condition, code):
    if not condition:
        raise ReleaseFetchError(code)


def release_files(tag: str) -> tuple[str, ...]:
    require(isinstance(tag, str) and TAG.fullmatch(tag), "release_tag_invalid")
    return (
        "SHA256SUMS",
        "release.json",
        f"cloud-memory-hub-{tag}-source.tar.gz",
        f"cloud-memory-hub-{tag}-linux-amd64.tar.gz",
    )


def release_bounds(name):
    if name == "SHA256SUMS":
        return MAX_CHECKSUM_BYTES, 60
    if name.endswith(".json"):
        return MAX_METADATA_BYTES, 60
    if name.endswith("-source.tar.gz"):
        return MAX_SOURCE_BYTES, 180
    return MAX_IMAGE_BYTES, 1200


def regular_size(path, maximum):
    try:
        info = path.lstat()
    except OSError:
        raise ReleaseFetchError("release_asset_missing") from None
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "release_asset_not_regular")
    require(0 < info.st_size <= maximum, "release_asset_size_invalid")
    return info.st_size


def digest(path):
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise ReleaseFetchError("release_asset_read_failed") from None


def read_json(path):
    def unique_object(items):
        value = {}
        for key, item in items:
            require(key not in value, "release_metadata_invalid")
            value[key] = item
        return value

    regular_size(path, MAX_METADATA_BYTES)
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=unique_object)
    except (OSError, ValueError, RecursionError):
        raise ReleaseFetchError("release_metadata_invalid") from None
    require(isinstance(value, dict), "release_metadata_invalid")
    return value


def read_checksums(directory, tag):
    sums, manifest, source, image = release_files(tag)
    path = directory / sums
    regular_size(path, MAX_CHECKSUM_BYTES)
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise ReleaseFetchError("release_checksums_invalid") from None
    entries = {}
    for line in lines:
        match = CHECKSUM_LINE.fullmatch(line)
        require(match is not None and match[2] not in entries, "release_checksums_invalid")
        entries[match[2]] = match[1]
    expected = {
        manifest,
        source,
        image,
        f"cloud-memory-hub-client-{tag}-macos-arm64.tar.gz",
        f"cloud-memory-hub-{tag}-provenance.tar.gz",
        "RELEASE_NOTES.md",
    }
    require(entries.keys() == expected, "release_checksums_invalid")
    return entries


def is_digest(value):
    return isinstance(value, str) and value.startswith("sha256:") and SHA256.fullmatch(value[7:])


def validate_release(tag: str, directory: Path) -> dict:
    """Check this release's exact manifest and all four staged NAS files."""
    directory = Path(directory)
    _, manifest, source, image = release_files(tag)
    entries = read_checksums(directory, tag)
    sizes = {}
    for name in (manifest, source, image):
        path = directory / name
        sizes[name] = regular_size(path, release_bounds(name)[0])
        require(digest(path) == entries[name], "release_checksum_mismatch")

    metadata = read_json(directory / manifest)
    commit = metadata.get("commit")
    require(
        type(metadata.get("schema_version")) is int
        and metadata["schema_version"] == 1
        and metadata.get("tag") == tag
        and metadata.get("version") == tag[1:]
        and isinstance(commit, str)
        and COMMIT.fullmatch(commit),
        "release_manifest_identity_invalid",
    )
    receipt = metadata.get("image")
    require(isinstance(receipt, dict), "release_image_receipt_invalid")
    require(
        all(receipt.get(key) == metadata[key] for key in ("tag", "version", "commit"))
        and receipt.get("image_ref") == f"cloud-memory-hub:{tag}"
        and receipt.get("platform") == "linux/amd64"
        and is_digest(receipt.get("image_id"))
        and is_digest(receipt.get("config_digest"))
        and receipt.get("artifact")
        == {"filename": image, "bytes": sizes[image], "sha256": entries[image]},
        "release_image_receipt_invalid",
    )
    client = metadata.get("client")
    require(isinstance(client, dict), "release_client_receipt_invalid")
    artifact = client.get("artifact")
    require(isinstance(artifact, dict), "release_client_receipt_invalid")
    client_name = f"cloud-memory-hub-client-{tag}-macos-arm64.tar.gz"
    client_manifest = client.get("manifest_sha256")
    require(
        all(client.get(key) == metadata[key] for key in ("tag", "version", "commit"))
        and client.get("platform") == "macos/arm64"
        and isinstance(client_manifest, str)
        and SHA256.fullmatch(client_manifest)
        and artifact.get("filename") == client_name
        and artifact.get("sha256") == entries[client_name]
        and type(artifact.get("bytes")) is int
        and 0 < artifact["bytes"] <= MAX_SOURCE_BYTES,
        "release_client_receipt_invalid",
    )
    return metadata


def require_official_url(url):
    try:
        parsed = urlsplit(url)
        valid = (
            url.isascii()
            and not any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in url)
            and parsed.scheme == "https"
            and parsed.hostname in RELEASE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    require(valid, "release_redirect_refused")


def curl_fetch(url, target, maximum, deadline_seconds, *, direct, accept):
    deadline = time.monotonic() + deadline_seconds
    for _ in range(5):
        require_official_url(url)
        remaining = math.ceil(deadline - time.monotonic())
        require(remaining > 0, "release_download_timeout")
        command = [
            "curl",
            "--disable",
            "--silent",
            "--fail",
            "--globoff",
            "--http1.1",
            "--proto",
            "=https",
            "--connect-timeout",
            "12",
            "--max-time",
            str(remaining),
            "--max-filesize",
            str(maximum),
            "--speed-limit",
            "4096",
            "--speed-time",
            "30",
            "--header",
            f"Accept: {accept}",
            "--output",
            str(target),
            "--write-out",
            "%{http_code}\n%{redirect_url}",
        ]
        if direct:
            command.extend(("--noproxy", "*"))
        try:
            result = subprocess.run(
                [*command, "--", url],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=remaining + 2,
                check=False,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise ReleaseFetchError("release_download_failed") from None
        require(result.returncode == 0, "release_download_failed")
        status, _, redirect = result.stdout.partition("\n")
        if status == "200":
            regular_size(target, maximum)
            return
        require(
            status in {"301", "302", "303", "307", "308"} and redirect, "release_download_failed"
        )
        url = urljoin(url, redirect)
    raise ReleaseFetchError("release_redirect_refused")


def download(url, target, *, accept="application/octet-stream", expected=None):
    maximum, timeout = release_bounds(target.name)
    for direct in (False, True):
        target.unlink(missing_ok=True)
        try:
            curl_fetch(url, target, maximum, timeout, direct=direct, accept=accept)
            if expected is not None:
                size, checksum = expected
                require(
                    regular_size(target, maximum) == size and digest(target) == checksum,
                    "release_api_digest_mismatch",
                )
            return
        except ReleaseFetchError as error:
            if str(error) == "release_redirect_refused":
                raise
    target.unlink(missing_ok=True)
    raise ReleaseFetchError("release_download_failed")


def release_api_assets(tag, directory):
    wanted = set(release_files(tag))
    metadata_path = directory / "public-release-api.json"
    download(f"{RELEASE_API}/tags/{tag}", metadata_path, accept="application/vnd.github+json")
    metadata = read_json(metadata_path)
    require(
        metadata.get("tag_name") == tag
        and metadata.get("draft") is False
        and metadata.get("prerelease") is False
        and isinstance(metadata.get("assets"), list),
        "release_api_identity_invalid",
    )
    assets = {}
    for asset in metadata["assets"]:
        require(isinstance(asset, dict), "release_api_assets_invalid")
        name = asset.get("name")
        if not isinstance(name, str) or name not in wanted:
            continue
        size, identity, checksum = asset.get("size"), asset.get("id"), asset.get("digest")
        require(
            name not in assets
            and type(identity) is int
            and identity > 0
            and type(size) is int
            and 0 < size <= release_bounds(name)[0]
            and is_digest(checksum)
            and asset.get("state") == "uploaded"
            and asset.get("browser_download_url") == f"{DOWNLOAD_BASE}/{tag}/{name}",
            "release_api_assets_invalid",
        )
        assets[name] = (identity, size, checksum[7:])
    require(assets.keys() == wanted, "release_api_assets_invalid")
    return assets


def fetch_release(tag: str, directory: Path) -> dict:
    """Download public assets without using GitHub credentials or replacing files."""
    directory = Path(directory)
    filenames = release_files(tag)
    require(directory.is_dir() and not directory.is_symlink(), "release_target_invalid")
    require(not any(os.path.lexists(directory / name) for name in filenames), "release_target_busy")
    with tempfile.TemporaryDirectory(prefix=".cmh-assets-", dir=directory) as temporary:
        stage = Path(temporary)
        assets = None
        for name in filenames:
            target = stage / name
            try:
                download(f"{DOWNLOAD_BASE}/{tag}/{name}", target)
            except ReleaseFetchError as error:
                if str(error) == "release_redirect_refused":
                    raise
                if assets is None:
                    assets = release_api_assets(tag, stage)
                identity, size, checksum = assets[name]
                download(f"{RELEASE_API}/assets/{identity}", target, expected=(size, checksum))
        if assets is not None:
            for name in filenames:
                _, size, checksum = assets[name]
                require(
                    regular_size(stage / name, release_bounds(name)[0]) == size
                    and digest(stage / name) == checksum,
                    "release_api_digest_mismatch",
                )
        metadata = validate_release(tag, stage)
        for name in filenames:
            source = stage / name
            source.chmod(0o600)
            # Link with no replacement, then remove the temporary name.
            try:
                os.link(source, directory / name, follow_symlinks=False)
            except OSError:
                raise ReleaseFetchError("release_target_busy") from None
            source.unlink()
        return metadata
