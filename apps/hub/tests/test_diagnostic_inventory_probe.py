"""Actual HTTP admission through read-only proof and native file inspection."""

import json
import subprocess
import sys
from pathlib import Path


def test_diagnostic_preflight_preserves_all_synthetic_business_and_file_content():
    script = Path(__file__).resolve().parents[3] / "scripts" / "probe_diagnostic_inventory.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=150
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence == {
        "phase": "inspect",
        "passed": True,
        "selected_requests": 2,
        "legacy_records": 6,
        "database_and_files_unchanged": True,
        "unselected_legacy_blocked": True,
        "missing_grant_blocked": True,
        "physical_cleanup_enabled": False,
        "external_models": False,
    }
