"""ENG-01: reject registry or mixed SDK imports before upstream collection."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import verify_core_sdk_source as guard


class CoreSdkSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.upstream = Path(temporary.name)
        self.modules = {}
        for name, relative in guard.MODULE_FILES.items():
            path = self.upstream / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Synthetic source fixture.\n")
            self.modules[name] = SimpleNamespace(__file__=str(path))

    def test_both_modules_from_prepared_tree_are_required(self):
        with patch.object(guard, "import_module", side_effect=self.modules.__getitem__) as importer:
            guard.verify_source(self.upstream)
        self.assertEqual(
            [call.args[0] for call in importer.call_args_list], list(guard.MODULE_FILES)
        )

    def test_registry_package_or_mixed_anthropic_module_is_rejected(self):
        for name in self.modules:
            with self.subTest(module=name):
                modules = {
                    **self.modules,
                    name: SimpleNamespace(__file__="/synthetic/site-packages/sdk.py"),
                }
                with patch.object(guard, "import_module", side_effect=modules.__getitem__):
                    with self.assertRaisesRegex(RuntimeError, "core_sdk_source_mismatch"):
                        guard.verify_source(self.upstream)

    def test_missing_patch_or_missing_origin_is_rejected(self):
        with patch.object(
            guard,
            "import_module",
            side_effect=[
                self.modules["hy_memory"],
                ModuleNotFoundError("synthetic missing patch"),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "core_sdk_source_unavailable"):
                guard.verify_source(self.upstream)
        with patch.object(guard, "import_module", return_value=SimpleNamespace(__file__=None)):
            with self.assertRaisesRegex(RuntimeError, "core_sdk_source_mismatch"):
                guard.verify_source(self.upstream)

    def test_module_must_have_an_existing_pinned_source_file(self):
        (self.upstream / guard.MODULE_FILES["hy_memory"]).unlink()
        with patch.object(guard, "import_module", side_effect=self.modules.__getitem__):
            with self.assertRaisesRegex(RuntimeError, "core_sdk_source_mismatch"):
                guard.verify_source(self.upstream)

    def test_package_only_view_preserves_core_namespace_tests(self):
        sdk_tests = self.upstream / "tests"
        sdk_tests.mkdir()
        (sdk_tests / "__init__.py").write_text("# SDK tests is a regular package.\n")
        core = self.upstream / "memhub/apps/core"
        (core / "tests").mkdir(parents=True)
        (core / "tests/core_test_classification.py").write_text("# Core tests is a namespace.\n")
        view = self.upstream / "sdk-view"
        view.mkdir()
        (view / "hy_memory").symlink_to(self.upstream / "hy_memory", target_is_directory=True)
        script = (
            "import sys; from pathlib import Path; import verify_core_sdk_source as guard; "
            "guard.verify_source(Path(sys.argv[1])); import tests.core_test_classification; "
            "print('fixed SDK and Core namespace verified')"
        )
        for sdk_path, expected_exit in ((self.upstream, 1), (view, 0)):
            with self.subTest(package_view=sdk_path == view):
                environment = {
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": os.pathsep.join(
                        (str(core), str(sdk_path), str(Path(guard.__file__).parent))
                    ),
                }
                result = subprocess.run(
                    [sys.executable, "-S", "-c", script, str(self.upstream)],
                    cwd=core,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                if expected_exit:
                    self.assertIn("tests.core_test_classification", result.stderr)
                else:
                    self.assertIn("fixed SDK and Core namespace verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
