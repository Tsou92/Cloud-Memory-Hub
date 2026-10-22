"""Prove pinned source preparation rejects drift instead of resetting user edits."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_upstream import git, prepare


class UpstreamPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.origin = self.root / "source.git"
        self.origin.mkdir()
        git(self.origin, "init", "-q")
        (self.origin / "module.py").write_text("original\n")
        git(self.origin, "add", "module.py")
        git(
            self.origin,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        self.commit = git(self.origin, "rev-parse", "HEAD")
        (self.origin / "module.py").write_text("reviewed change\n")
        patch = git(self.origin, "diff") + "\n"
        (self.origin / "module.py").write_text("original\n")
        (self.root / "upstream/patches").mkdir(parents=True)
        (self.root / "upstream/patches/reviewed.patch").write_text(patch)
        self.manifest = {
            "repository": str(self.origin).removesuffix(".git"),
            "reviewed_commit": self.commit,
            "patches": [
                {
                    "path": "patches/reviewed.patch",
                    "digest": "sha256:" + hashlib.sha256(patch.encode()).hexdigest(),
                }
            ],
        }
        self.write_manifest()

    def write_manifest(self):
        (self.root / "upstream/manifest.json").write_text(json.dumps(self.manifest))

    def test_fetch_apply_and_repeat_produce_exact_reviewed_tree(self):
        checkout = prepare(self.root)
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.commit)
        self.assertEqual((checkout / "module.py").read_text(), "reviewed change\n")
        self.assertEqual(prepare(self.root), checkout)

    def test_unreviewed_change_is_preserved_and_rejected(self):
        checkout = prepare(self.root)
        (checkout / "module.py").write_text("unreviewed edit\n")
        with self.assertRaises(subprocess.CalledProcessError):
            prepare(self.root)
        self.assertEqual((checkout / "module.py").read_text(), "unreviewed edit\n")

    def test_untracked_file_is_preserved_and_rejected(self):
        checkout = prepare(self.root)
        (checkout / "unknown.txt").write_text("unreviewed\n")
        with self.assertRaisesRegex(RuntimeError, "untracked"):
            prepare(self.root)
        self.assertTrue((checkout / "unknown.txt").exists())

    def test_modified_patch_is_rejected_before_application(self):
        (self.root / "upstream/patches/reviewed.patch").write_text("modified patch\n")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            prepare(self.root)
        self.assertEqual(
            (self.root / ".local/upstream/hy-memory/module.py").read_text(), "original\n"
        )

    def test_commit_drift_never_resets_checkout(self):
        checkout = prepare(self.root)
        self.manifest["reviewed_commit"] = "0" * 40
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "commit mismatch"):
            prepare(self.root)
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.commit)
