"""ADR-0060 / FR-06: relocatable release input, tamper rejection and safe installation."""

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build_client_distribution as builder
from scripts.client_distribution import install as installer


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.package = self.root / "client"
        self.package.mkdir()
        for name in (
            "install.py",
            "install.sh",
            "README.md",
            "requirements.lock",
            "local-wheels.lock",
        ):
            (self.package / name).write_text("fixture\n")
        skill = self.package / "skill/cloud-memory-hub"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("public template\n")
        self.manifest = {
            "schema_version": 1,
            "kind": "cloud-memory-hub-mac-client",
            "release": "0.1.0-preview.1",
            "architecture": "arm64",
            "platform": "darwin",
            "python": "3.12",
            "files": installer.inventory(self.package),
        }
        self.write_manifest()

    def write_manifest(self):
        (self.package / "manifest.json").write_text(json.dumps(self.manifest))

    def test_archive_can_move_without_invalidating_manifest(self):
        moved = self.root / "a directory with spaces"
        self.package.rename(moved)
        self.assertEqual(installer.verify(moved), self.manifest)

    def test_tamper_missing_file_and_unlisted_file_fail_before_install(self):
        for change in ("tamper", "remove", "extra"):
            with self.subTest(change=change):
                target = self.package / "README.md"
                target.write_text("fixture\n")
                extra = self.package / "unlisted.txt"
                extra.unlink(missing_ok=True)
                if change == "tamper":
                    target.write_text("changed")
                elif change == "remove":
                    target.unlink()
                else:
                    extra.write_text("changed")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    installer.verify(self.package)

    def test_nested_manifest_is_not_excluded_from_verification(self):
        nested = self.package / "nested"
        nested.mkdir()
        (nested / "manifest.json").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "checksum"):
            installer.verify(self.package)

    def test_symlink_and_traversal_are_rejected(self):
        (self.package / "linked").symlink_to(self.package / "README.md")
        with self.assertRaisesRegex(ValueError, "unsafe_file"):
            installer.verify(self.package)
        (self.package / "linked").unlink()
        self.manifest["files"]["../escape"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "manifest_invalid"):
            installer.verify(self.package)

    @patch.object(installer.sys, "version_info", (3, 12, 9))
    @patch.object(installer.sys, "platform", "darwin")
    @patch.object(installer.platform, "machine", return_value="arm64")
    @patch.object(installer.subprocess, "run")
    def test_install_uses_fixed_target_hashed_dependencies_and_no_resolution(self, run, _machine):
        target = self.root / "new installation"
        installer.install(self.package, target, self.manifest)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[0][-1], str(target / "venv"))
        installs = [command for command in calls if "install" in command]
        self.assertEqual(len(installs), 2)
        for command in installs:
            self.assertIn("--require-hashes", command)
            self.assertIn("--no-deps", command)
            self.assertIn("--only-binary=:all:", command)
            self.assertTrue(command[0].startswith(str(target)))
        self.assertIn("--no-index", installs[1])
        self.assertTrue((target / "installation.json").is_file())
        self.assertFalse(any("cloud-memory-hub" == Path(command[0]).name for command in calls))
        with self.assertRaisesRegex(ValueError, "target_exists"):
            installer.install(self.package, target, self.manifest)
        self.assertTrue((target / "installation.json").is_file())

    @patch.object(installer.sys, "version_info", (3, 12, 9))
    @patch.object(installer.sys, "platform", "darwin")
    @patch.object(installer.platform, "machine", return_value="arm64")
    @patch.object(installer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "pip"))
    def test_failed_install_cleans_only_its_new_directory(self, _run, _machine):
        preserved = self.root / "existing"
        preserved.mkdir()
        (preserved / "queue").write_text("preserve")
        target = self.root / "failed"
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.package, target, self.manifest)
        self.assertFalse(target.exists())
        self.assertEqual((preserved / "queue").read_text(), "preserve")

    @patch.object(installer.sys, "version_info", (3, 12, 9))
    @patch.object(installer.sys, "platform", "darwin")
    @patch.object(installer.platform, "machine", return_value="x86_64")
    def test_other_architecture_is_rejected_without_creating_target(self, _machine):
        target = self.root / "wrong_architecture"
        with self.assertRaisesRegex(ValueError, "platform_mismatch"):
            installer.install(self.package, target, self.manifest)
        self.assertFalse(target.exists())

    @patch.object(builder, "run")
    def test_export_retains_hashes_and_rejects_local_or_unlocked_dependencies(self, run):
        valid = "example==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
        run.return_value = valid
        self.assertEqual(builder.export_requirements(self.root, builder.PRODUCTS), valid)
        arguments = run.call_args.args[0]
        self.assertIn("--frozen", arguments)
        self.assertNotIn("--prune", arguments)
        for invalid in ("example>=1", "-e /private/source", "example @ file:///private/source"):
            run.return_value = invalid
            with self.assertRaisesRegex(ValueError, "unlocked_or_local"):
                builder.export_requirements(self.root, builder.PRODUCTS)

    def test_wheel_rejects_editable_and_absolute_source_metadata(self):
        path = self.root / "example-1.0-py3-none-any.whl"
        for extra, metadata in (
            ("", "Name: example\nVersion: 1.0\n"),
            ("example.pth", "Name: example\nVersion: 1.0\n"),
            ("", "Name: example\nRequires-Dist: other @ file:///private/source\n"),
        ):
            with zipfile.ZipFile(path, "w") as wheel:
                wheel.writestr("example-1.0.dist-info/METADATA", metadata)
                if extra:
                    wheel.writestr(extra, "private path")
            if extra or "file:" in metadata:
                with self.assertRaises(ValueError):
                    builder.validate_wheel(path, "example")
            else:
                builder.validate_wheel(path, "example")

    @patch.object(builder, "source_files")
    def test_source_snapshot_excludes_tests_and_rejects_runtime_data(self, files):
        source = self.root / "source"
        (source / "src/example").mkdir(parents=True)
        (source / "tests").mkdir()
        (source / "pyproject.toml").write_text("[project]\nname='example'\n")
        (source / "src/example/__init__.py").write_text("VALUE = 1\n")
        (source / "tests/private.json").write_text("not part of distribution")
        files.return_value = [
            Path("pyproject.toml"),
            Path("src/example/__init__.py"),
            Path("tests/private.json"),
        ]
        target = self.root / "staged"
        selected = builder.stage_project(source, target, "src")
        self.assertEqual(len(selected), 2)
        self.assertFalse((target / "tests").exists())
        (source / "src/example/runtime.sqlite3").write_text("private data")
        files.return_value.append(Path("src/example/runtime.sqlite3"))
        with self.assertRaisesRegex(ValueError, "source_file_invalid"):
            builder.stage_project(source, self.root / "rejected", "src")

    @patch.object(builder, "run")
    def test_build_constraints_work_under_a_directory_with_spaces(self, run):
        work = self.root / "checkout with spaces"
        source = work / "sources/example"
        source.mkdir(parents=True)
        output = work / "package/wheels"
        output.mkdir(parents=True)
        constraints = work / "build-constraints.txt"
        constraints.write_text(builder.BUILD_CONSTRAINTS)
        wheel = output / "example-1.0-py3-none-any.whl"

        def build(arguments, *, cwd):
            constraint_arg = arguments[arguments.index("--build-constraints") + 1]
            # Match uv's build-constraint propagation: whitespace splits paths.
            self.assertEqual(len(constraint_arg.split()), 1)
            self.assertEqual((cwd / constraint_arg).read_text(), builder.BUILD_CONSTRAINTS)
            self.assertEqual(cwd / arguments[-1], source)
            self.assertEqual(cwd / arguments[arguments.index("--out-dir") + 1], output)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("example-1.0.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
            return ""

        run.side_effect = build
        self.assertEqual(
            builder.build_wheel(source, output, constraints, "python3.12", "example"), wheel
        )

    @patch.object(builder, "source_files")
    def test_reviewed_sdk_data_package_allows_python_but_not_a_database(self, files):
        source = self.root / "sdk"
        (source / "hy_memory/data").mkdir(parents=True)
        (source / "pyproject.toml").write_text("[project]\nname='hy-memory'\n")
        module = Path("hy_memory/data/cache.py")
        (source / module).write_text("VALUE = 1\n")
        files.return_value = [Path("pyproject.toml"), module]
        target = self.root / "sdk-staged"
        builder.stage_project(source, target, "hy_memory")
        self.assertEqual((target / module).read_text(), "VALUE = 1\n")
        database = Path("hy_memory/data/runtime.sqlite3")
        (source / database).write_text("private data")
        files.return_value.append(database)
        with self.assertRaisesRegex(ValueError, "source_file_invalid"):
            builder.stage_project(source, self.root / "sdk-rejected", "hy_memory")


if __name__ == "__main__":
    unittest.main()
