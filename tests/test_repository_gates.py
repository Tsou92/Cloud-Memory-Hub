"""Exercise policy failures and the actual scanner using only synthetic fixtures."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_repository import check_repository, path_problem, source_files
from scripts.check_secrets import parse_findings, scan_paths


class RepositoryGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_ignored_private_data_is_not_read(self):
        self.write(".gitignore", "data/\n")
        self.write("data/history.txt", "synthetic private conversation")
        self.assertNotIn(Path("data/history.txt"), source_files(self.root))
        self.assertEqual(check_repository(self.root), [])

    def test_force_tracked_private_data_is_rejected(self):
        self.write(".gitignore", "data/\n")
        self.write("data/history.txt", "synthetic private conversation")
        subprocess.run(
            ["git", "add", "-f", "data/history.txt"], cwd=self.root, check=True, capture_output=True
        )
        self.assertIn(Path("data/history.txt"), source_files(self.root))
        self.assertTrue(any("cannot be committed" in item for item in check_repository(self.root)))

    def test_untracked_new_files_are_checked(self):
        self.write("README.md", "[broken](not-created.md)\n")
        self.assertEqual(len(check_repository(self.root)), 1)

    def test_local_links_spaces_anchors_remote_and_fences(self):
        self.write("docs/with spaces.md", "# Example\n")
        self.write(
            "README.md",
            "[valid](<docs/with spaces.md>)\n"
            "[encoded](docs/with%20spaces.md#example)\n"
            "[anchor](#example) [remote](https://example.com/missing)\n"
            "```md\n[example](missing.md)\n```\n",
        )
        self.assertEqual(check_repository(self.root), [])

    def test_links_cannot_escape_repository(self):
        self.write("README.md", "[outside](../outside.md)\n")
        self.assertIn("escapes repository", check_repository(self.root)[0])

    def test_symlink_is_rejected_before_scanning(self):
        (self.root / "source.md").symlink_to(self.root / "nonexistent-private-file")
        self.assertIn("symlinks", check_repository(self.root)[0])
        with patch("scripts.check_secrets.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                scan_paths(self.root, [Path("source.md")])
            run.assert_not_called()

    def test_credentials_databases_and_placeholder_policy(self):
        for filename in [
            ".env",
            ".env.production",
            "private.key",
            "history.sqlite3",
            "store.db-wal",
            ".codex/config.toml",
            ".claude/settings.local.json",
        ]:
            with self.subTest(filename=filename):
                self.write(filename, "synthetic data")
                self.assertIsNotNone(path_problem(self.root, Path(filename)))
        self.write(".env.example", "ENDPOINT=replace-me\n")
        self.assertIsNone(path_problem(self.root, Path(".env.example")))

    def test_scanner_detects_synthetic_key_in_new_file(self):
        # Construct a recognisable fake key without committing a credential-shaped literal.
        fake_key = "AKIA" + "Z" * 16
        self.write("example_config.py", f"cloud_access_id = '{fake_key}'\n")
        findings = scan_paths(self.root, [Path("example_config.py")])
        self.assertTrue(any("AWS Access Key" in finding for finding in findings))
        self.assertNotIn(fake_key, "\n".join(findings))

    def test_clean_file_passes_actual_scanner(self):
        self.write("README.md", "# Cloud Memory Hub\n")
        self.assertEqual(scan_paths(self.root, [Path("README.md")]), [])

    def test_scanner_failure_never_prints_raw_output(self):
        self.write("README.md", "# Example\n")
        raw_output = "potential sensitive output"
        failed = subprocess.CompletedProcess([], 1, raw_output, raw_output)
        with patch("scripts.check_secrets.subprocess.run", return_value=failed):
            with self.assertRaises(RuntimeError) as context:
                scan_paths(self.root, [Path("README.md")])
        self.assertNotIn(raw_output, str(context.exception))

    def test_scanner_is_explicitly_offline_and_covers_new_files(self):
        self.write("README.md", "# Example\n")
        clean = subprocess.CompletedProcess([], 0, '{"results": {}}', "")
        with patch("scripts.check_secrets.subprocess.run", return_value=clean) as run:
            self.assertEqual(scan_paths(self.root, [Path("README.md")]), [])
        arguments = run.call_args.args[0]
        self.assertIn("--no-verify", arguments)
        self.assertIn("--all-files", arguments)
        self.assertIn(str(self.root / "README.md"), arguments)

    def test_scanner_missing_or_timed_out_cannot_pass(self):
        self.write("README.md", "# Example\n")
        for error in [FileNotFoundError(), subprocess.TimeoutExpired("detect-secrets", 120)]:
            with self.subTest(error=type(error).__name__):
                with patch("scripts.check_secrets.subprocess.run", side_effect=error):
                    with self.assertRaises(RuntimeError):
                        scan_paths(self.root, [Path("README.md")])


class ScannerReportTests(unittest.TestCase):
    def test_malformed_report_is_not_clean(self):
        for output in ["not json", "{}", "null", '{"results": []}', '{"results": {"x": [null]}}']:
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                parse_findings(output)

    def test_findings_only_include_location_and_type(self):
        synthetic_value = "hidden example value"
        report = {
            "results": {
                "file.py": [{"line_number": 2, "type": "Example", "secret": synthetic_value}]
            }
        }
        self.assertEqual(parse_findings(json.dumps(report)), ["file.py:2: Example"])


if __name__ == "__main__":
    unittest.main()
