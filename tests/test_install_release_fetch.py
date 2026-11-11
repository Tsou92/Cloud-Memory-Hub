"""Contract tests for the first installer release download fallback."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_release_fetch as fetcher

TAG = "v0.1.4"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def release_fixture():
    sums, manifest, source, image = fetcher.release_files(TAG)
    client = f"cloud-memory-hub-client-{TAG}-macos-arm64.tar.gz"
    body = {
        source: b"synthetic source archive",
        image: b"synthetic Linux image archive",
        client: b"synthetic Mac client archive",
        f"cloud-memory-hub-{TAG}-provenance.tar.gz": b"synthetic provenance",
        "RELEASE_NOTES.md": b"synthetic notes",
    }

    def receipt(name):
        return {"filename": name, "bytes": len(body[name]), "sha256": sha256(body[name])}

    identity = {"tag": TAG, "version": TAG[1:], "commit": "a" * 40}
    body[manifest] = json.dumps(
        {
            **identity,
            "schema_version": 1,
            "image": {
                **identity,
                "platform": "linux/amd64",
                "image_ref": "cloud-memory-hub:" + TAG,
                "image_id": "sha256:" + "b" * 64,
                "config_digest": "sha256:" + "c" * 64,
                "artifact": receipt(image),
            },
            "client": {
                **identity,
                "platform": "macos/arm64",
                "manifest_sha256": "d" * 64,
                "artifact": receipt(client),
            },
        }
    ).encode()
    body[sums] = "".join(f"{sha256(value)}  {name}\n" for name, value in body.items()).encode()
    return {name: body[name] for name in fetcher.release_files(TAG)}


class ReleaseFetchTests(unittest.TestCase):
    def test_validation_rejects_changed_source_or_ambiguous_checksum(self):
        body = release_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name, contents in body.items():
                (directory / name).write_bytes(contents)
            self.assertEqual(fetcher.validate_release(TAG, directory)["tag"], TAG)
            _, _, source, _ = fetcher.release_files(TAG)
            (directory / source).write_bytes(b"changed archive")
            with self.assertRaisesRegex(fetcher.ReleaseFetchError, "release_checksum_mismatch"):
                fetcher.validate_release(TAG, directory)
            (directory / source).write_bytes(body[source])
            (directory / "SHA256SUMS").write_bytes(body["SHA256SUMS"] * 2)
            with self.assertRaisesRegex(fetcher.ReleaseFetchError, "release_checksums_invalid"):
                fetcher.validate_release(TAG, directory)

    def test_browser_failure_falls_back_to_exact_public_asset_and_private_files(self):
        body = release_fixture()
        assets = [
            {
                "name": name,
                "id": index,
                "size": len(contents),
                "digest": "sha256:" + sha256(contents),
                "state": "uploaded",
                "browser_download_url": f"{fetcher.DOWNLOAD_BASE}/{TAG}/{name}",
            }
            for index, (name, contents) in enumerate(body.items(), start=1)
        ]
        calls = []

        def download(url, target, **options):
            calls.append((url, options))
            if url.startswith(fetcher.DOWNLOAD_BASE) and target.name == "SHA256SUMS":
                raise fetcher.ReleaseFetchError("release_download_failed")
            if url == f"{fetcher.RELEASE_API}/tags/{TAG}":
                target.write_text(
                    json.dumps(
                        {
                            "tag_name": TAG,
                            "draft": False,
                            "prerelease": False,
                            "assets": assets,
                        }
                    )
                )
            else:
                target.write_bytes(body[target.name])

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(fetcher, "download", side_effect=download),
        ):
            directory = Path(temporary)
            self.assertEqual(fetcher.fetch_release(TAG, directory)["tag"], TAG)
            self.assertEqual({p.name for p in directory.iterdir()}, set(body))
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir()))
            checksum = body["SHA256SUMS"]
            self.assertIn(
                (
                    f"{fetcher.RELEASE_API}/assets/1",
                    {"expected": (len(checksum), sha256(checksum))},
                ),
                calls,
            )
            with self.assertRaisesRegex(fetcher.ReleaseFetchError, "release_target_busy"):
                fetcher.fetch_release(TAG, directory)

    def test_curl_refuses_offsite_redirect_before_following(self):
        result = fetcher.subprocess.CompletedProcess([], 0, "302\nhttps://example.invalid/file")
        with patch.object(fetcher.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(fetcher.ReleaseFetchError, "release_redirect_refused"):
                fetcher.curl_fetch(
                    fetcher.DOWNLOAD_BASE,
                    Path("synthetic-file"),
                    4096,
                    60,
                    direct=False,
                    accept="application/octet-stream",
                )
        self.assertEqual(run.call_count, 1)
