"""Real local HTTP Hub + downloaded-style stdlib connector; synthetic data only."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from cloud_memory_hub.onboarding import connect as connector
from test_agent_access import preserve_pytest_log_handlers as preserve_pytest_log_handlers
from test_transport import live_hub as live_hub_fixture

live_hub = live_hub_fixture


def test_standalone_client_claims_verifies_real_mcp_and_retries_without_writes(live_hub, tmp_path):
    config, app, uploaded = live_hub
    admin = {"Authorization": "Bearer " + config.admin_token}
    with httpx.Client(base_url=config.public_origin, trust_env=False) as web:
        project = web.post(
            "/hub/v1/projects", headers=admin, json={"display_name": "Synthetic installer project"}
        ).json()
        created = web.post(
            "/hub/v1/pairings",
            headers=admin,
            json={"scope_id": project["project_id"], "display_name": "Synthetic installer device"},
        )
        assert created.status_code == 201
        invite = created.json()
        link = (
            config.public_origin
            + "/hub/connect#pairing_id="
            + invite["pairing_id"]
            + "&secret="
            + invite["invitation_secret"]
        )
        home = tmp_path / "isolated-home"
        home.mkdir(mode=0o700)
        command = [
            sys.executable,
            "-I",
            str(Path(connector.__file__).resolve()),
            "--link",
            link,
            "--agent",
            "generic",
            "--home",
            str(home),
        ]
        results = [
            subprocess.run(command, capture_output=True, env=os.environ.copy(), timeout=40)
            for _ in range(2)
        ]
        for result in results:
            assert result.returncode == 0, result.stderr.decode()
            assert b"cmhdev_v1_" not in result.stdout + result.stderr
            assert invite["invitation_secret"].encode() not in result.stdout + result.stderr
        pending_paths = list(home.glob(".cloud-memory-hub/remote/*/pending.json"))
        assert len(pending_paths) == 1
        pending = json.loads(pending_paths[0].read_bytes())
        assert pending["completed_agents"] == ["generic"]
        assert pending["bundle"]["scope_id"] == project["project_id"]
        status = web.get("/hub/v1/pairings/" + invite["pairing_id"], headers=admin)
        assert status.json()["state"] == "claimed"
        assert uploaded == []  # initialize/list/status did not create a memory event
        token = "cmhdev_v1_" + pending["bundle"]["device_id"] + "_" + pending["claimant_secret"]
        assert token not in results[0].stdout.decode()
        # A revoked identity cannot pass a rerun even with a complete local installation.
        device_id = pending["bundle"]["device_id"]
        details = web.get("/hub/v1/devices", headers=admin).json()
        device = next(item for item in details["devices"] if item["device_id"] == device_id)
        revoked = web.post(
            f"/hub/v1/devices/{device_id}/revoke",
            headers=admin,
            json={"expected_revision": device["revision"]},
        )
        assert revoked.status_code == 200
        result = subprocess.run(command, capture_output=True, env=os.environ.copy(), timeout=40)
        assert result.returncode == 1
        assert token.encode() not in result.stdout + result.stderr
