"""Actual stopped maintenance, signed repair and fresh-process file recovery."""

import json
import subprocess
import sys
from pathlib import Path


def test_physical_file_repair_survives_interruption_and_log_only_restore():
    script = Path(__file__).resolve().parents[3] / "scripts" / "probe_diagnostic_file_execution.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence == {
        "passed": True,
        "http_requests": 2,
        "native_legacy_records": 6,
        "readonly_cli": True,
        "migration_gap_recovered": True,
        "repair_intent": True,
        "interrupted_io_pending": True,
        "fresh_process_recovery": True,
        "handler_reopened": True,
        "exact_file_restore_recleaned": True,
        "strict_tail_preserved": True,
        "changed_record_blocked": True,
        "signed_multi_repair_recovery": True,
        "legacy_parent_authority_unchanged": True,
        "batch_interruption_and_restore": True,
        "whole_purge_completed": False,
        "external_models": False,
    }
