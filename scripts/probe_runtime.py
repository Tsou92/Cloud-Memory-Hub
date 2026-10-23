"""Run inside a disposable smoke container; never print credentials or bodies."""

import hashlib
import json
import secrets
import sqlite3
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from cryptography.fernet import Fernet

BASE = "http://localhost:8800"
STATE = Path("/data/smoke-state.json")


def probe(phase: str) -> None:
    token = Path("/config/admin-token").read_text().strip()
    cookies = CookieJar()
    client = build_opener(HTTPCookieProcessor(cookies))

    def request(path, *, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        supplied = {"Content-Type": "application/json", **(headers or {})}
        try:
            with client.open(
                Request(BASE + path, data=data, headers=supplied, method=method), timeout=20
            ) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    auth = {"Authorization": "Bearer " + token}
    assert request("/healthz")[0] == 200
    assert request("/api/v1/model-profiles")[0] == 401
    assert request("/api/v1/health", headers=auth)[0] == 200
    status, body = request("/api/v2/memory-authority", headers=auth)
    assert status == 200 and json.loads(body) == {"authority": "sdk", "migration_id": None}
    status, body = request("/api/v2/memory-library", headers=auth)
    assert status == 200 and json.loads(body)["total"] == 0
    status, body = request("/api/v2/home-summary", headers=auth)
    assert status == 200 and json.loads(body)["memory_count"] == 0
    assert request("/api/v1/health", headers={**auth, "Origin": "https://invalid.test"})[0] == 403
    assert request("/internal/test", headers=auth)[0] == 404
    assert request("/api/v1/ui/session", headers=auth)[0] == 404
    assert request("/api/v1/health", headers={**auth, "Host": "invalid.test"})[0] == 403

    if phase == "before":
        value = secrets.token_urlsafe(48)
        status, body = request(
            "/api/v1/model-profiles",
            method="POST",
            headers=auth,
            body={
                "name": "Container smoke test",
                "provider_kind": "openai_compatible",
                "base_url": "https://example.invalid/v1",
                "model": "synthetic-model",
                "embedding_model": "synthetic-embedding",
                "embedding_provider_kind": "openai_compatible",
                "embedding_base_url": "https://example.invalid/v1",
                "api_key": value,
                "max_concurrency": 1,
            },
        )
        assert status == 200, "Synthetic configuration creation failed"
        profile = json.loads(body)
        assert profile["has_api_key"] and not profile["is_active"]
        assert value.encode() not in body
        assert token.encode() not in body
        assert (
            request(
                "/hub/login",
                method="POST",
                body={"credential": token},
                headers={"Origin": BASE},
            )[0]
            == 200
        )
        cookie = next(item.value for item in cookies if item.name == "cmh_session")
        assert request("/api/v1/model-profiles")[0] == 200
        assert request("/api/v1/ui/session/logout", method="POST")[0] == 403
        state = {
            "profile_id": profile["profile_id"],
            "credential_hash": hashlib.sha256(value.encode()).hexdigest(),
            "old_session": cookie,
        }
        STATE.touch(mode=0o600, exist_ok=False)
        STATE.write_text(json.dumps(state))
        for path in (Path("/data"), Path("/config")):
            for database in path.glob("*.sqlite3*"):
                assert value.encode() not in database.read_bytes(), "Plaintext credential found"
    else:
        state = json.loads(STATE.read_text())
        assert (
            request(
                "/api/v1/model-profiles", headers={"Cookie": "cmh_session=" + state["old_session"]}
            )[0]
            == 401
        )
        status, body = request("/api/v1/model-profiles", headers=auth)
        assert status == 200
        profiles = json.loads(body)
        profile = next(item for item in profiles if item["profile_id"] == state["profile_id"])
        assert profile["has_api_key"] and not profile["is_active"]
        cipher = Fernet(Path("/config/encryption-key").read_bytes().strip())
        with sqlite3.connect("/config/model-credentials.sqlite3") as database:
            rows = database.execute("SELECT ciphertext FROM credentials").fetchall()
        assert any(
            hashlib.sha256(cipher.decrypt(row[0])).hexdigest() == state["credential_hash"]
            for row in rows
        ), "Encrypted credential did not survive restart"
    print(f"Container {phase}-restart checks passed")


if __name__ == "__main__":
    probe(sys.argv[1])
