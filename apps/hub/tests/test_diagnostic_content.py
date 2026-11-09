"""FR-10/FR-11: dedicated-process output and original persistence boundaries."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[3] / "scripts" / "probe_diagnostic_content.py"
MARKER = "Synthetic confidential diagnostic orchard must never appear in output"


@pytest.mark.parametrize("phase", ["logging", "persistence", "server"])
def test_strict_real_sinks_keep_metadata_and_drop_bodies(tmp_path, phase):
    result = subprocess.run(
        [sys.executable, str(PROBE), "--phase", phase, "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert MARKER not in result.stdout + result.stderr
    summary = json.loads(result.stdout.splitlines()[-1])
    evidence_key = {
        "logging": "caller_fd_preserved",
        "persistence": "request_id_preserved",
        "server": "http_500",
    }[phase]
    assert summary == {
        "phase": phase,
        "passed": True,
        evidence_key: True,
    }


def test_cli_startup_failure_does_not_use_raw_exception_hook(tmp_path):
    script = f"""
from unittest.mock import patch
from cloud_memory_hub.__main__ import main
def fail():
    raise RuntimeError({MARKER!r})
with patch('cloud_memory_hub.__main__._main', fail):
    main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, cwd=tmp_path
    )
    assert result.returncode == 2
    assert MARKER not in result.stdout + result.stderr
    messages = [json.loads(line) for line in result.stderr.splitlines() if line]
    assert any(message.get("error_type") == "RuntimeError" for message in messages)
    assert all(message["content_omitted"] is True for message in messages)
