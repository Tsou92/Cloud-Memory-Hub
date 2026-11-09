"""Real native Core maintenance, isolated from the Hub's process-wide policy."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "probe_core_diagnostic_maintenance.py"


@pytest.mark.parametrize(
    "scenario",
    (
        "queue_timeout",
        "error_logging",
        "strict_overflow",
        "reset_retry",
        "configure_retry",
        "handler_rebind",
        "handler_failure",
        "constructor_race",
        "unknown_handler",
        "failed_domain_close",
        "hub_combined",
        "multiple_writers",
        "resume_thread_failure",
        "pause_all_before_start",
    ),
)
def test_native_core_maintenance(scenario):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--scenario", scenario],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
