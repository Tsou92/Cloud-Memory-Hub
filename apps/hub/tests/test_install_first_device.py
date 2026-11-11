"""The first-install helper uses the same HTTP boundary as device onboarding."""

import importlib.util
import secrets
import socket
import time
from pathlib import Path
from threading import Thread

import httpx
import pytest
import uvicorn
from test_pairing_api import configured


def test_first_install_resumes_project_and_refuses_an_existing_device(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[3] / "scripts" / "install_first_device.py"
    spec = importlib.util.spec_from_file_location("install_first_device", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    config, app = configured(tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        local = f"http://127.0.0.1:{listener.getsockname()[1]}"
        monkeypatch.setattr(helper, "API", local + "/hub/v1/")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            args = (
                "Synthetic project",
                "Synthetic device",
                config.public_origin,
                config.admin_token,
            )
            issued = helper.provision(*args)
            resumed = helper.provision(*args)
            assert issued["project_id"] == resumed["project_id"]
            assert config.admin_token not in resumed["link"]
            with pytest.raises(helper.InstallError, match="first_device_project_conflict"):
                helper.provision("Different project", *args[1:])

            origin, pairing_id, invitation = helper.parse_link(resumed["link"])
            assert origin == config.public_origin
            with httpx.Client(base_url=local, trust_env=False) as client:
                claim = client.post(
                    "/hub/v1/pairings/claim",
                    headers={"Host": config.authority},
                    json={
                        "pairing_id": pairing_id,
                        "invitation_secret": invitation,
                        "claimant_secret": secrets.token_urlsafe(32),
                    },
                )
            assert claim.status_code == 200
            assert claim.json()["scope_id"] == resumed["project_id"]
            with pytest.raises(helper.InstallError, match="first_device_already_configured"):
                helper.provision(*args)
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive()
