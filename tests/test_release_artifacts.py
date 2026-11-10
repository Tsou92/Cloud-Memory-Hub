"""R3-04/05: immutable source, final artifacts and draft-before-publish boundaries."""

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release_artifacts as release
from scripts.client_distribution.install import inventory


class ReleaseFixture(unittest.TestCase):
    release_version = "1.2.3rc1"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        (self.source / "apps/hub").mkdir(parents=True)
        (self.source / "apps/hub/pyproject.toml").write_text(
            f'[project]\nname="synthetic-release"\nversion="{self.release_version}"\n'
        )
        for name in ("LICENSE", "NOTICE", "CHANGELOG.md"):
            (self.source / name).write_text("Synthetic public fixture\n")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Synthetic release fixture",
        )
        self.commit = self.git("rev-parse", "HEAD")
        self.git("tag", "v" + self.release_version)
        self.identity = {
            "tag": "v" + self.release_version,
            "version": self.release_version,
            "commit": self.commit,
        }

    def git(self, *arguments):
        return release.command(["git", *arguments], cwd=self.source)

    def tar(self, path, entries):
        with tarfile.open(path, "w:gz") as archive:
            for name, data in entries:
                info = tarfile.TarInfo(name)
                if data is None:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "../outside"
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

    def client(self, directory, **changes):
        directory.mkdir(parents=True)
        package = self.root / (directory.name + "-package")
        package.mkdir()
        for name in (
            "install.py",
            "install.sh",
            "README.md",
            "requirements.lock",
            "local-wheels.lock",
            "skill/cloud-memory-hub/SKILL.md",
            "provenance/upstream-manifest.json",
            "provenance/uv.lock",
            "provenance/LICENSE.hy-memory",
        ):
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Synthetic public fixture\n")
        manifest = {
            "schema_version": 1,
            "kind": "cloud-memory-hub-mac-client",
            "release": self.identity["version"],
            "hub_commit": self.commit,
            "hub_worktree_dirty": False,
            "architecture": "arm64",
            "platform": "darwin",
            "python": "3.12",
            "files": inventory(package),
            **changes,
        }
        (package / "manifest.json").write_text(json.dumps(manifest))
        archive_path = directory / release.names(self.identity)["client"]
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(package, arcname=archive_path.name.removesuffix(".tar.gz"))
        return archive_path

    def image(self, directory, *, revision=None):
        directory.mkdir(parents=True)
        labels = {"org.opencontainers.image.revision": revision or self.commit}
        config = json.dumps(
            {
                "os": "linux",
                "architecture": "amd64",
                "config": {"Labels": labels},
            }
        ).encode()
        checksum = hashlib.sha256(config).hexdigest()
        inspection = [
            {
                "Os": "linux",
                "Architecture": "amd64",
                "Id": "sha256:" + checksum,
                "Config": {"Labels": labels},
            }
        ]
        manifests = [
            {
                "Config": checksum + ".json",
                "RepoTags": ["cloud-memory-hub:" + self.identity["tag"]],
                "Layers": [],
            }
        ]
        self.tar(
            directory / release.names(self.identity)["image"],
            [
                ("manifest.json", json.dumps(manifests).encode()),
                (checksum + ".json", config),
            ],
        )
        return inspection

    def oci_image(self, directory, *, index=True, change_manifest=None, change_index=None):
        directory.mkdir(parents=True)
        entries = {}

        def blob(value, media_type):
            data = json.dumps(value).encode()
            checksum = hashlib.sha256(data).hexdigest()
            entries["blobs/sha256/" + checksum] = data
            return {"mediaType": media_type, "digest": "sha256:" + checksum, "size": len(data)}

        labels = {"org.opencontainers.image.revision": self.commit}
        config = blob(
            {"os": "linux", "architecture": "amd64", "config": {"Labels": labels}},
            "application/vnd.oci.image.config.v1+json",
        )
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": dict(config),
            "layers": [],
        }
        if change_manifest:
            change_manifest(manifest)
        descriptor = blob(manifest, manifest["mediaType"])
        if index:
            attestation_config = blob(
                {"os": "unknown", "architecture": "unknown"}, config["mediaType"]
            )
            attestation = blob({**manifest, "config": attestation_config}, manifest["mediaType"])
            image_index = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {**descriptor, "platform": {"os": "linux", "architecture": "amd64"}},
                    {
                        **attestation,
                        "platform": {"os": "unknown", "architecture": "unknown"},
                        "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                    },
                ],
            }
            if change_index:
                change_index(image_index)
            descriptor = blob(image_index, image_index["mediaType"])
        entries["manifest.json"] = json.dumps(
            [
                {
                    "Config": "blobs/sha256/" + config["digest"].removeprefix("sha256:"),
                    "RepoTags": ["cloud-memory-hub:" + self.identity["tag"]],
                    "Layers": [],
                }
            ]
        ).encode()
        self.tar(directory / release.names(self.identity)["image"], list(entries.items()))
        inspection = [
            {
                "Id": descriptor["digest"],
                "Descriptor": descriptor,
                "Os": "linux",
                "Architecture": "amd64",
                "Config": {"Labels": labels},
            }
        ]
        return inspection, entries, config["digest"]

    def bundle(self):
        inputs = self.root / "inputs"
        image = inputs / "release-linux"
        inspection = self.image(image)
        release.image_receipt(image, self.identity, inspection)
        client = inputs / "release-mac"
        self.client(client)
        release.client_receipt(client, self.root / "verified-client", self.identity)
        output = self.root / "release"
        release.assemble(self.source, inputs, output, self.identity)
        return output


class ArtifactTests(ReleaseFixture):
    def test_existing_tag_commit_clean_tree_and_exact_version(self):
        self.assertEqual(
            release.validate_source(
                self.source,
                "v1.2.3rc1",
                self.commit,
                "refs/tags/v1.2.3rc1",
                "tag",
            ),
            self.identity,
        )
        self.git("tag", "v1.2.3")
        with self.assertRaisesRegex(release.ReleaseError, "version_mismatch"):
            release.validate_source(self.source, "v1.2.3", self.commit)
        (self.source / "untracked").write_text("fixture")
        with self.assertRaisesRegex(release.ReleaseError, "source_dirty"):
            release.validate_source(self.source, "v1.2.3rc1", self.commit)

    def test_invalid_version_aliases_and_dispatch_branch_are_rejected(self):
        for tag in ("v01.2.3", "v1.2.3rc0", "v1.2.3-rc1", "v1.2.3.dev0", "1.2.3", "v1.2.3\n"):
            with self.subTest(tag=tag), self.assertRaisesRegex(release.ReleaseError, "tag_invalid"):
                release.validate_source(self.source, tag, self.commit)
        for ref, ref_type in (
            ("refs/heads/v1.2.3rc1", "branch"),
            ("refs/tags/v1.2.3rc1", "branch"),
        ):
            with (
                self.subTest(ref=ref),
                self.assertRaisesRegex(release.ReleaseError, "ref_mismatch"),
            ):
                release.validate_source(self.source, "v1.2.3rc1", self.commit, ref, ref_type)
        with self.assertRaisesRegex(release.ReleaseError, "checkout_mismatch"):
            release.validate_source(self.source, "v1.2.3rc1", "0" * 40)

    def test_annotated_local_tag_peels_to_same_commit(self):
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "tag",
            "-f",
            "-a",
            "v1.2.3rc1",
            "-m",
            "Synthetic annotated tag",
        )
        self.assertEqual(
            release.validate_source(self.source, "v1.2.3rc1", self.commit), self.identity
        )

    def test_image_receipt_matches_actual_docker_save_config(self):
        directory = self.root / "image"
        inspection = self.image(directory)
        receipt = release.image_receipt(directory, self.identity, inspection)
        self.assertEqual(receipt["image_id"], inspection[0]["Id"])
        self.assertEqual(receipt["config_digest"], inspection[0]["Id"])
        self.assertNotIn("descriptor", receipt)
        self.assertEqual(receipt["commit"], self.commit)
        inspection[0]["Id"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(release.ReleaseError, "config_mismatch"):
            release.image_receipt(directory, self.identity, inspection)

    def test_oci_index_and_direct_manifest_bind_the_tested_native_id(self):
        for index in (True, False):
            with self.subTest(index=index):
                directory = self.root / ("oci-index" if index else "oci-manifest")
                inspection, _, config_digest = self.oci_image(directory, index=index)
                receipt = release.image_receipt(directory, self.identity, inspection)
                self.assertNotEqual(inspection[0]["Id"], config_digest)
                self.assertEqual(receipt["image_id"], inspection[0]["Id"])
                self.assertEqual(receipt["config_digest"], config_digest)
                self.assertEqual(receipt["descriptor"], inspection[0]["Descriptor"])

    def test_oci_descriptor_identity_type_and_size_are_checked(self):
        for field, value in (
            ("digest", "sha256:" + "0" * 64),
            ("mediaType", "application/unknown"),
            ("size", True),
            ("size", 2 * 1024**2 + 1),
            ("size", 1),
        ):
            with self.subTest(field=field, value=value):
                directory = self.root / f"descriptor-{field}-{value}"
                inspection, _, _ = self.oci_image(directory)
                inspection[0]["Descriptor"][field] = value
                with self.assertRaises(release.ReleaseError):
                    release.image_receipt(directory, self.identity, inspection)
                self.assertFalse((directory / "image-receipt.json").exists())

    def test_oci_missing_corrupt_and_duplicate_root_blobs_are_refused(self):
        for damage in ("missing", "corrupt", "duplicate"):
            with self.subTest(damage=damage):
                directory = self.root / damage
                inspection, entries, _ = self.oci_image(directory)
                root = "blobs/sha256/" + inspection[0]["Id"].removeprefix("sha256:")
                if damage == "missing":
                    del entries[root]
                elif damage == "corrupt":
                    entries[root] = entries[root].replace(
                        b'"schemaVersion": 2', b'"schemaVersion": 1'
                    )
                members = list(entries.items())
                if damage == "duplicate":
                    members.append((root, entries[root]))
                self.tar(directory / release.names(self.identity)["image"], members)
                with self.assertRaises(release.ReleaseError):
                    release.image_receipt(directory, self.identity, inspection)

    def test_oci_requires_one_matching_platform(self):
        mutations = {
            "missing": lambda index: index["manifests"].pop(0),
            "ambiguous": lambda index: index["manifests"].append(dict(index["manifests"][0])),
            "wrong": lambda index: index["manifests"][0]["platform"].update(architecture="arm64"),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                directory = self.root / name
                inspection, _, _ = self.oci_image(directory, change_index=mutation)
                with self.assertRaises(release.ReleaseError):
                    release.image_receipt(directory, self.identity, inspection)

    def test_oci_child_manifest_and_config_blobs_are_verified(self):
        for node in ("manifest", "config"):
            for damage in ("missing", "corrupt"):
                with self.subTest(node=node, damage=damage):
                    directory = self.root / (node + "-" + damage)
                    inspection, entries, config_digest = self.oci_image(directory)
                    root = "blobs/sha256/" + inspection[0]["Id"].removeprefix("sha256:")
                    child = json.loads(entries[root])["manifests"][0]["digest"]
                    target = "blobs/sha256/" + (
                        child if node == "manifest" else config_digest
                    ).removeprefix("sha256:")
                    if damage == "missing":
                        del entries[target]
                    else:
                        # Same size and JSON semantics: only digest validation detects this.
                        changed = entries[target].replace(b", ", b",\t", 1)
                        self.assertEqual(len(changed), len(entries[target]))
                        self.assertNotEqual(changed, entries[target])
                        entries[target] = changed
                    self.tar(
                        directory / release.names(self.identity)["image"], list(entries.items())
                    )
                    with self.assertRaises(release.ReleaseError):
                        release.image_receipt(directory, self.identity, inspection)

    def test_oci_valid_chain_to_another_config_with_same_labels_is_refused(self):
        directory = self.root / "another-config"
        inspection, entries, config_digest = self.oci_image(directory, index=False)

        def add_blob(value, media_type):
            encoded = json.dumps(value).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            entries["blobs/sha256/" + digest] = encoded
            return {"mediaType": media_type, "digest": "sha256:" + digest, "size": len(encoded)}

        configuration = json.loads(entries["blobs/sha256/" + config_digest[7:]])
        configuration["config"]["Env"] = ["SYNTHETIC_DIFFERENT_BUILD=1"]
        manifest = json.loads(entries["blobs/sha256/" + inspection[0]["Id"][7:]])
        manifest["config"] = add_blob(configuration, "application/vnd.oci.image.config.v1+json")
        descriptor = add_blob(manifest, manifest["mediaType"])
        inspection[0].update(Id=descriptor["digest"], Descriptor=descriptor)
        self.tar(directory / release.names(self.identity)["image"], list(entries.items()))
        with self.assertRaisesRegex(release.ReleaseError, "release_image_config_mismatch"):
            release.image_receipt(directory, self.identity, inspection)

    def test_oci_config_binding_cannot_be_replaced_by_same_revision_label(self):
        for field, value in (("digest", "sha256:" + "0" * 64), ("size", 1)):
            with self.subTest(field=field):
                directory = self.root / field
                inspection, _, _ = self.oci_image(
                    directory,
                    change_manifest=lambda manifest, field=field, value=value: manifest[
                        "config"
                    ].update({field: value}),
                )
                with self.assertRaises(release.ReleaseError):
                    release.image_receipt(directory, self.identity, inspection)

    def test_wrong_image_revision_or_platform_fails(self):
        directory = self.root / "image"
        inspection = self.image(directory, revision="0" * 40)
        with self.assertRaisesRegex(release.ReleaseError, "revision_mismatch"):
            release.image_receipt(directory, self.identity, inspection)
        inspection[0]["Architecture"] = "arm64"
        with self.assertRaisesRegex(release.ReleaseError, "platform_mismatch"):
            release.image_receipt(directory, self.identity, inspection)

    def test_final_client_tar_verified_without_installing_or_launchd(self):
        directory = self.root / "client"
        archive = self.client(directory)
        target = self.root / "unpacked"
        package = release.unpack_client(archive, target, self.identity)
        self.assertEqual(
            json.loads((package / "manifest.json").read_text())["hub_commit"], self.commit
        )
        with self.assertRaisesRegex(release.ReleaseError, "target_exists"):
            release.unpack_client(archive, target, self.identity)
        self.assertTrue(package.exists())

    def test_wrong_or_dirty_client_source_is_rejected_and_stage_removed(self):
        for index, changes in enumerate(
            (
                {"hub_commit": "0" * 40},
                {"hub_worktree_dirty": True},
                {"release": "1.2.3"},
                {"architecture": "x86_64"},
            )
        ):
            with self.subTest(changes=changes):
                path = self.client(self.root / str(index), **changes)
                target = self.root / ("unpack-" + str(index))
                with self.assertRaisesRegex(release.ReleaseError, "identity_mismatch"):
                    release.unpack_client(path, target, self.identity)
                self.assertFalse(target.exists())

    def test_client_archive_links_traversal_duplicate_and_tamper_are_rejected(self):
        path = self.root / "unsafe.tar.gz"
        prefix = release.names(self.identity)["client"].removesuffix(".tar.gz")
        for entries in (
            [(prefix + "/link", None)],
            [(prefix + "/../escape", b"x")],
            [(prefix + "/same", b"a"), (prefix + "/same", b"b")],
        ):
            self.tar(path, entries)
            with self.assertRaisesRegex(release.ReleaseError, "archive_unsafe"):
                release.unpack_client(path, self.root / "unsafe-stage", self.identity)
            self.assertFalse((self.root / "unsafe-stage").exists())
        valid = self.client(self.root / "client")
        with tarfile.open(valid, "r:gz") as archive:
            entries = [(m.name, archive.extractfile(m).read()) for m in archive if m.isfile()]
        entries = [
            (name, b"tampered" if name.endswith("README.md") else data) for name, data in entries
        ]
        self.tar(path, entries)
        with self.assertRaisesRegex(ValueError, "checksum_mismatch"):
            release.unpack_client(path, self.root / "unsafe-stage", self.identity)

    def test_bundle_binds_source_binaries_licenses_and_six_checksums(self):
        output = self.bundle()
        records = release.verify_bundle(output, self.identity)
        self.assertEqual(len(records), 7)
        self.assertEqual(len((output / "SHA256SUMS").read_text().splitlines()), 6)
        with tarfile.open(output / release.names(self.identity)["source"], "r:gz") as archive:
            self.assertIn("cloud-memory-hub-v1.2.3rc1/LICENSE", archive.getnames())
        with tarfile.open(output / release.names(self.identity)["provenance"], "r:gz") as archive:
            self.assertIn("client/upstream-manifest.json", archive.getnames())
            self.assertIn("client/LICENSE.hy-memory", archive.getnames())
            self.assertEqual(release.archive_json(archive, "release.json")["commit"], self.commit)
        first = self.root / "first.gz"
        second = self.root / "second.gz"
        release.source_archive(self.source, first, self.identity)
        release.source_archive(self.source, second, self.identity)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with gzip.open(first) as stream:
            self.assertTrue(stream.read())

    def test_bundle_rejects_tamper_extra_missing_and_duplicate_checksum(self):
        output = self.bundle()
        notes = output / "RELEASE_NOTES.md"
        original = notes.read_bytes()
        notes.write_bytes(original + b"tampered")
        with self.assertRaisesRegex(release.ReleaseError, "checksum_mismatch"):
            release.verify_bundle(output, self.identity)
        notes.write_bytes(original)
        extra = output / "unexpected"
        extra.write_text("x")
        with self.assertRaisesRegex(release.ReleaseError, "assets_mismatch"):
            release.verify_bundle(output, self.identity)
        extra.unlink()
        sums = output / "SHA256SUMS"
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + "\n")
        with self.assertRaisesRegex(release.ReleaseError, "checksums_invalid"):
            release.verify_bundle(output, self.identity)
        notes.unlink()
        with self.assertRaisesRegex(release.ReleaseError, "assets_mismatch"):
            release.verify_bundle(output, self.identity)

    def test_asset_size_and_symlink_rejected_before_reading_content(self):
        path = self.root / "large"
        with path.open("wb") as stream:
            stream.truncate(release.MAX_ASSET_BYTES)
        with self.assertRaisesRegex(release.ReleaseError, "size_invalid"):
            release.file_record(path)
        linked = self.root / "linked"
        linked.symlink_to(path)
        with self.assertRaisesRegex(release.ReleaseError, "not_regular"):
            release.file_record(linked)


class PublicationTests(ReleaseFixture):
    def setUp(self):
        super().setUp()
        self.output = self.bundle()
        self.records = release.verify_bundle(self.output, self.identity)
        self.commands = []
        self.created = False
        self.published = False
        self.draft = {
            "id": 42,
            "draft": True,
            "tag_name": self.identity["tag"],
            "target_commitish": self.commit,
            "prerelease": "rc" in self.release_version,
        }
        self.assets = [
            {"name": name, "size": record["bytes"], "state": "uploaded"}
            for name, record in self.records.items()
        ]

    def api(self, path):
        if "/git/ref/" in path:
            return {"object": {"type": "commit", "sha": self.commit}}
        if "/assets?" in path:
            return self.assets
        if "/releases?" in path:
            return [self.draft] if self.created else []
        if "/releases/tags/" in path:
            raise release.ReleaseError("release_remote_request_failed")
        self.assertTrue(path.endswith("/releases/42"))
        return {**self.draft, "draft": not self.published}

    def cli(self, arguments, **_kwargs):
        self.commands.append(arguments)
        if arguments[2] == "create":
            self.created = True
        if arguments[2] == "download":
            target = Path(arguments[arguments.index("--dir") + 1])
            self.assertEqual(list(target.iterdir()), [])
            for source in self.output.iterdir():
                shutil.copyfile(source, target / source.name)
        if arguments[2] == "edit":
            self.published = True
        return ""

    def publish(self, api=None, cli=None):
        with (
            patch.object(release, "gh_api", side_effect=api or self.api),
            patch.object(
                release,
                "command",
                side_effect=cli or self.cli,
            ),
        ):
            return release.publish(self.output, self.identity, "example/cloud-memory-hub")

    def test_upload_draft_download_verify_then_publish_prerelease(self):
        # For existing tags this display field is not the commit authority.
        # Both remote tag reads still have to peel to the immutable source SHA.
        self.draft["target_commitish"] = "main"
        result = self.publish()
        self.assertEqual(result["state"], "published")
        self.assertEqual(result["asset_count"], 7)
        self.assertEqual(
            [args[2] for args in self.commands], ["create", "upload", "download", "edit"]
        )
        self.assertIn("--verify-tag", self.commands[0])
        self.assertIn("--draft", self.commands[0])
        self.assertIn("--prerelease", self.commands[0])
        self.assertIn("--latest=false", self.commands[-1])
        self.assertNotIn("--clobber", self.commands[1])

    def test_existing_draft_is_preserved_even_when_by_tag_would_return404(self):
        self.created = True
        with self.assertRaisesRegex(release.ReleaseError, "already_exists"):
            self.publish()
        self.assertEqual(self.commands, [])

    def test_stable_release_is_not_prerelease_and_updates_latest(self):
        self.release_version = "1.2.3"
        self.setUp()
        result = self.publish()
        self.assertEqual(result["state"], "published")
        self.assertNotIn("--prerelease", self.commands[0])
        self.assertIn("--latest=true", self.commands[-1])

    def test_extra_duplicate_or_incomplete_remote_assets_cannot_publish(self):
        original = list(self.assets)
        for assets in (original[:-1], original + original[:1], original + [{"name": "extra"}]):
            self.assets = assets
            self.commands.clear()
            self.created = False
            with self.assertRaisesRegex(release.ReleaseError, "assets_mismatch"):
                self.publish()
            self.assertFalse(self.published)
            self.assertNotIn("edit", [args[2] for args in self.commands])

    def test_download_corruption_keeps_draft(self):
        def cli(arguments, **kwargs):
            self.cli(arguments, **kwargs)
            if arguments[2] == "download":
                directory = Path(arguments[arguments.index("--dir") + 1])
                (directory / "RELEASE_NOTES.md").write_text("corrupted")

        with self.assertRaisesRegex(release.ReleaseError, "checksum_mismatch"):
            self.publish(cli=cli)
        self.assertFalse(self.published)

    def test_remote_tag_moved_after_download_cannot_publish(self):
        reads = 0

        def api(path):
            nonlocal reads
            if "/git/ref/" in path:
                reads += 1
                return {
                    "object": {"type": "commit", "sha": self.commit if reads == 1 else "0" * 40}
                }
            return self.api(path)

        with self.assertRaisesRegex(release.ReleaseError, "tag_moved"):
            self.publish(api=api)
        self.assertFalse(self.published)

    def test_post_publish_readback_failure_is_unknown_and_never_rolls_back(self):
        def api(path):
            if self.published:
                raise release.ReleaseError("release_remote_request_failed")
            return self.api(path)

        with self.assertRaisesRegex(release.ReleaseError, "publish_result_unknown"):
            self.publish(api=api)
        self.assertTrue(self.published)
        self.assertEqual(self.commands[-1][2], "edit")
        self.assertNotIn("delete", [args[2] for args in self.commands])

    def test_edit_transport_failure_is_unknown_and_not_retried(self):
        def cli(arguments, **kwargs):
            self.cli(arguments, **kwargs)
            if arguments[2] == "edit":
                raise release.ReleaseError("release_command_failed")

        with self.assertRaisesRegex(release.ReleaseError, "publish_result_unknown"):
            self.publish(cli=cli)
        self.assertEqual([args[2] for args in self.commands].count("edit"), 1)


class ProviderResponseTests(unittest.TestCase):
    def test_failed_list_never_means_release_absent(self):
        for code, stdout, expected in (
            (0, 'HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"id":42}', {"id": 42}),
            (0, "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n[]", []),
        ):
            with patch.object(
                release.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [],
                    code,
                    stdout=stdout,
                    stderr="private provider detail",
                ),
            ):
                self.assertEqual(
                    release.gh_api("repos/example/project/releases?per_page=100&page=1"),
                    expected,
                )
        for stdout in (
            "",
            "HTTP/2.0 401 Unauthorized\n\n{}",
            "HTTP/2.0 429 Slow down\n\n{}",
            "HTTP/2.0 404 Not Found\n\n{}",
        ):
            with (
                patch.object(
                    release.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [],
                        1,
                        stdout=stdout,
                        stderr="private provider detail",
                    ),
                ),
                self.assertRaises(release.ReleaseError) as error,
            ):
                release.find_release("example/project", "v1.2.3")
            self.assertNotIn("private", str(error.exception))

    def test_release_list_follows_all_pages_and_rejects_ambiguous_tags(self):
        other = [{"id": index + 1, "tag_name": f"v9.0.{index}"} for index in range(100)]
        draft = {"id": 101, "tag_name": "v1.2.3", "draft": True}
        with patch.object(release, "gh_api", side_effect=[other, [draft]]) as api:
            self.assertEqual(release.find_release("example/project", "v1.2.3"), draft)
        self.assertIn("page=2", api.call_args.args[0])
        with patch.object(release, "gh_api", return_value=[draft, draft]):
            with self.assertRaisesRegex(release.ReleaseError, "tag_ambiguous"):
                release.find_release("example/project", "v1.2.3")

    def test_remote_annotated_tag_is_peeled_and_other_object_rejected(self):
        identity = {"tag": "v1.2.3", "commit": "a" * 40}
        with patch.object(
            release,
            "gh_api",
            side_effect=[
                {"object": {"type": "tag", "sha": "b" * 40}},
                {"object": {"type": "commit", "sha": "a" * 40}},
            ],
        ) as api:
            release.remote_tag("example/project", identity)
        self.assertIn("/git/tags/", api.call_args.args[0])
        with patch.object(
            release, "gh_api", return_value={"object": {"type": "tree", "sha": "a" * 40}}
        ):
            with self.assertRaisesRegex(release.ReleaseError, "tag_invalid"):
                release.remote_tag("example/project", identity)

    def test_asset_listing_follows_pagination_and_rejects_later_extras(self):
        records = {str(index): {"bytes": 1} for index in range(101)}
        assets = [{"name": name, "size": 1, "state": "uploaded"} for name in records]
        with patch.object(release, "gh_api", side_effect=[assets[:100], assets[100:]]) as api:
            release.remote_assets("example/project", 42, records)
        self.assertIn("page=2", api.call_args.args[0])
        with patch.object(release, "gh_api", side_effect=[assets[:100], [{"name": "extra"}]]):
            with self.assertRaisesRegex(release.ReleaseError, "assets_mismatch"):
                release.remote_assets("example/project", 42, records)


if __name__ == "__main__":
    unittest.main()
