"""Device control-plane checks in disposable containers; no real client setup."""

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://localhost:8800"
STATE = Path("/data/device-access-smoke.json")


def probe(phase: str) -> None:
    admin = Path("/config/admin-token").read_text().strip()

    def request(path, credential, method="GET", body=None):
        try:
            with urlopen(
                Request(
                    BASE + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={
                        "Authorization": "Bearer " + credential,
                        "Content-Type": "application/json",
                    },
                    method=method,
                ),
                timeout=20,
            ) as reply:
                assert reply.headers.get("Cache-Control") == "no-store"
                return reply.status, json.load(reply)
        except HTTPError as error:
            return error.code, None

    if phase == "before":
        status, project = request(
            "/hub/v1/projects", admin, "POST", {"display_name": "Synthetic project"}
        )
        assert status == 201
        status, issued = request(
            "/hub/v1/devices", admin, "POST", {"display_name": "Synthetic connector"}
        )
        assert status == 201
        device = issued["device"]["device_id"]
        original = issued["credential"]
        status, session = request("/hub/v1/session", original)
        assert status == 200 and session["device"]["grants"] == []
        assert session["features"] == ["device_identity", "source_registration", "event_upload"]
        assert request("/hub/v1/devices", original)[0] == 403
        assert request("/api/v1/model-profiles", original)[0] == 401
        grants = [{"scope_id": project["project_id"], "permissions": ["memory:read"]}]
        assert (
            request(
                f"/hub/v1/devices/{device}/grants",
                admin,
                "PUT",
                {"expected_revision": 1, "grants": grants},
            )[0]
            == 200
        )
        status, rotated = request(
            f"/hub/v1/devices/{device}/rotate", admin, "POST", {"expected_revision": 2}
        )
        assert status == 200
        assert request("/hub/v1/session", original)[0] == 401
        state = {
            "device_id": device,
            "credential": rotated["credential"],
            "instance_id": session["instance_id"],
            "grants": grants,
        }
        database = Path("/config/remote-access.sqlite3").read_bytes()
        assert original.encode() not in database and state["credential"].encode() not in database
        STATE.touch(mode=0o600, exist_ok=False)
        STATE.write_text(json.dumps(state))
    else:
        state = json.loads(STATE.read_text())
        credential = state["credential"]
        if phase == "after":
            status, session = request("/hub/v1/session", credential)
            assert status == 200
            assert session["instance_id"] == state["instance_id"]
            assert session["device"]["revision"] == 3
            assert session["device"]["grants"] == state["grants"]
            assert (
                request(
                    f"/hub/v1/devices/{state['device_id']}/revoke",
                    admin,
                    "POST",
                    {"expected_revision": 3},
                )[0]
                == 200
            )
        assert request("/hub/v1/session", credential)[0] == 401
        status, listing = request("/hub/v1/devices", admin)
        assert status == 200
        device = next(
            item for item in listing["devices"] if item["device_id"] == state["device_id"]
        )
        assert device["status"] == "revoked"
        assert "credential" not in json.dumps(listing)
    print(f"Device identity, scope receipts and revocation checks passed ({phase})")


if __name__ == "__main__":
    probe(sys.argv[1])
