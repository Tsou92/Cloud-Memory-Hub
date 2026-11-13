"""Exercise the local setup and safety checks without a Docker daemon."""

import contextlib
import hashlib
import io
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import install_local_docker as local
from scripts import install_release_fetch as release

TAG = "v0.1.8"
IMAGE = "sha256:" + "a" * 64
COMMIT = "b" * 40
CONTAINER = "c" * 64
PROJECT = "cloud-memory-hub"
ORIGIN = "http://127.0.0.1:18880"
CONFIG = "sha256:" + "d" * 64


class FakeDocker:
    context = "test-local-context"

    def __init__(self, workspace):
        self.workspace = workspace
        self.commands = []
        self.image = ""
        self.created = False
        self.foreign_volume = False
        self.address = "127.0.0.1"

    @property
    def owner(self):
        return json.loads((self.workspace / ".cmh-install.json").read_bytes())["owner"]

    def run(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args[0] == "compose":
            command = args[args.index("-p") + 2 :]
            if command[0] == "ps":
                return CONTAINER if self.created else ""
            if command[0] == "up":
                self.created = True
            return ""
        if args[:2] == ("image", "ls"):
            return self.image
        if args[:2] == ("image", "load"):
            self.image = IMAGE
            return ""
        if args[:2] == ("image", "inspect"):
            return f"{self.image} linux/amd64 {COMMIT}"
        if args[0] == "ps":
            return f"{PROJECT}-hub-1" if self.created else ""
        if args[:2] == ("volume", "ls"):
            return f"{PROJECT}_hub_data" if self.foreign_volume else ""
        if args[:2] == ("network", "ls"):
            return ""
        if args[:2] == ("container", "inspect"):
            if "--format" in args:
                return self.owner
            return json.dumps(
                [
                    {
                        "Config": {"Labels": {local.INSTALL_LABEL: self.owner}},
                        "HostConfig": {
                            "PortBindings": {
                                "8800/tcp": [{"HostIp": self.address, "HostPort": "18880"}]
                            }
                        },
                        "Image": self.image,
                        "State": {"Health": {"Status": "healthy"}},
                    }
                ]
            )
        if args[0] == "exec":
            return '{"origin":"http://127.0.0.1:18880"}'
        raise AssertionError(args)


class LocalInstallTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        temporary = tempfile.TemporaryDirectory(prefix="cmh-local-install-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.assets = self.home / "artifacts"
        self.assets.mkdir()
        _, _, archive, image = release.release_files(TAG)
        self.image = self.assets / image
        self.write_image()
        compose = b"services:\n  hub:\n    image: ${CMH_IMAGE}\n"
        with tarfile.open(self.assets / archive, "w:gz") as stream:
            item = tarfile.TarInfo(f"cloud-memory-hub-{TAG}/compose.yaml")
            item.size = len(compose)
            stream.addfile(item, io.BytesIO(compose))
        self.args = SimpleNamespace(
            tag=TAG, compose_project=PROJECT, port=18880, assets_dir=self.assets
        )
        workspace = self.home / ".cloud-memory-hub" / "local-server" / PROJECT
        self.docker = FakeDocker(workspace)
        self.receipt = {
            "commit": COMMIT,
            "image": {"image_id": IMAGE, "config_digest": CONFIG},
        }

    def write_image(self, *, config=None, stored_digest=None):
        documents = {}
        digest = None
        if config is not None:
            full_ref = f"docker.io/library/cloud-memory-hub:{TAG}"
            manifest = json.dumps(
                {"schemaVersion": 2, "mediaType": local.OCI_MANIFEST, "config": {"digest": config}}
            ).encode()
            digest = stored_digest or "sha256:" + hashlib.sha256(manifest).hexdigest()
            documents["blobs/" + digest.replace(":", "/", 1)] = manifest
            documents["index.json"] = json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": local.OCI_INDEX,
                    "manifests": [
                        {
                            "mediaType": local.OCI_MANIFEST,
                            "digest": digest,
                            "size": len(manifest),
                            "annotations": {"io.containerd.image.name": full_ref},
                        }
                    ],
                }
            ).encode()
        with tarfile.open(self.image, "w:gz") as stream:
            for name, content in documents.items():
                item = tarfile.TarInfo(name)
                item.size = len(content)
                stream.addfile(item, io.BytesIO(content))
        return digest

    def deployment(self):
        with (
            patch.object(local, "Docker", return_value=self.docker),
            patch.object(release, "validate_release", return_value=self.receipt) as validate,
            patch.object(local, "_wait_healthy") as healthy,
            patch.object(local, "_check_port"),
        ):
            result = local.deploy_local(self.args, home=self.home, fetcher=release)
        return result, validate, healthy

    def test_local_deploy_binds_loopback_and_retries_without_download_or_reinitialize(self):
        first, verify, healthy = self.deployment()
        self.assertEqual(first, ORIGIN)
        verify.assert_called_once_with(TAG, self.assets)
        healthy.assert_called_once()
        environment = (self.docker.workspace / ".env").read_text()
        self.assertIn(f"CMH_PUBLIC_ORIGIN={ORIGIN}\n", environment)
        self.assertIn(
            "platform: linux/amd64", (self.docker.workspace / "compose.override.yaml").read_text()
        )
        commands = [command for command, _ in self.docker.commands]
        self.assertTrue(any(command[:2] == ("image", "load") for command in commands))
        self.assertTrue(any("setup" in command for command in commands))

        self.docker.commands.clear()
        second, verify, healthy = self.deployment()
        self.assertEqual(second, ORIGIN)
        verify.assert_not_called()
        healthy.assert_called_once()
        commands = [command for command, _ in self.docker.commands]
        self.assertFalse(any(command[:2] == ("image", "load") for command in commands))
        self.assertFalse(any("setup" in command for command in commands))

    def test_foreign_volume_stops_before_release_download(self):
        self.docker.foreign_volume = True
        with (
            patch.object(local, "Docker", return_value=self.docker),
            patch.object(release, "validate_release") as fetch,
            self.assertRaisesRegex(local.LocalInstallError, "local_docker_project_conflict"),
        ):
            local.deploy_local(self.args, home=self.home, fetcher=release)
        fetch.assert_not_called()

    def test_busy_port_stops_before_writing_intent_or_reading_release(self):
        with (
            socket.socket() as listener,
            patch.object(local, "Docker", return_value=self.docker),
            patch.object(release, "validate_release") as fetch,
        ):
            listener.bind(("127.0.0.1", 0))
            self.args.port = listener.getsockname()[1]
            with self.assertRaisesRegex(local.LocalInstallError, "local_port_busy"):
                local.deploy_local(self.args, home=self.home, fetcher=release)
        fetch.assert_not_called()
        self.assertFalse((self.docker.workspace / ".cmh-install.json").exists())

    def test_interrupted_config_publication_recovers_with_same_owner(self):
        with (
            patch.object(local, "Docker", return_value=self.docker),
            patch.object(local, "_check_port"),
            patch.object(release, "validate_release", return_value=self.receipt),
            patch.object(local, "_compose_files", side_effect=OSError("interrupted")),
            self.assertRaisesRegex(OSError, "interrupted"),
        ):
            local.deploy_local(self.args, home=self.home, fetcher=release)
        owner = self.docker.owner
        self.assertEqual(self.deployment()[0], ORIGIN)
        self.assertEqual(self.docker.owner, owner)

    def test_existing_wrong_image_is_not_replaced(self):
        self.docker.image = "sha256:" + "f" * 64
        with self.assertRaisesRegex(local.LocalInstallError, "local_docker_image_conflict"):
            self.deployment()
        self.assertFalse(
            any(command[:2] == ("image", "load") for command, _ in self.docker.commands)
        )

    def test_containerd_platform_manifest_matches_verified_release_config(self):
        manifest = self.write_image(config=CONFIG)
        self.docker.image = manifest
        self.assertEqual(self.deployment()[0], ORIGIN)
        self.assertFalse(
            any(command[:2] == ("image", "load") for command, _ in self.docker.commands)
        )
        recorded = json.loads((self.docker.workspace / ".cmh-install.json").read_text())
        self.assertEqual(recorded["image_ids"], [IMAGE, CONFIG, manifest])

    def test_manifest_rejects_a_different_config_or_wrong_blob_digest(self):
        for config, digest in ((IMAGE, None), (CONFIG, IMAGE)):
            with self.subTest(config=config, digest=digest):
                self.write_image(config=config, stored_digest=digest)
                with self.assertRaisesRegex(local.LocalInstallError, "local_image_archive_invalid"):
                    local._platform_manifest_id(self.image, TAG, CONFIG)

    def test_local_admin_pipe_accepts_only_owned_loopback_container(self):
        self.deployment()
        with patch.object(local, "Docker", return_value=self.docker):
            data = local.local_admin_entry(
                self.args, home=self.home, script=b"test payload", setup_only=True
            )
            self.assertEqual(json.loads(data), {"origin": ORIGIN})
            command, kwargs = self.docker.commands[-1]
            self.assertEqual(
                command, ("exec", "-i", CONTAINER, "/opt/cmh/venv/bin/python", "-", "--setup-only")
            )
            self.assertEqual(kwargs["body"], b"test payload")
            self.docker.address = "0.0.0.0"
            with self.assertRaisesRegex(local.LocalInstallError, "local_docker_project_conflict"):
                local.local_admin_entry(self.args, home=self.home, script=b"never executed")

    def test_remote_docker_context_is_refused_before_daemon_call(self):
        reply = [
            subprocess.CompletedProcess([], 0, b"my-engine\n", b""),
            subprocess.CompletedProcess([], 0, b"ssh://remote.invalid\n", b""),
        ]
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "ssh://override.invalid", "CMH_PORT": "12345"}),
            patch.object(local.shutil, "which", return_value="docker"),
            patch.object(local.subprocess, "run", side_effect=reply) as run,
            self.assertRaisesRegex(local.LocalInstallError, "local_docker_context_remote"),
        ):
            local.Docker()
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("DOCKER_HOST", run.call_args.kwargs["env"])
        self.assertNotIn("CMH_PORT", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
