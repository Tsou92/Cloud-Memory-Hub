"""FR-10/12: real credential-file rotation and synthetic authenticated requests."""

import asyncio
import os
import stat
from dataclasses import replace

import pytest
from cloud_memory_hub import admin_credentials
from cloud_memory_hub.admin_credentials import AdminCredentialError, AdminCredentials
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.boundary import ADMIN_CREDENTIAL_PATH, CloudBoundary
from cloud_memory_hub.config import HubConfig, read_private_file
from cloud_memory_hub.console_read import ConsoleRead
from cloud_memory_hub.governance_authority import activate_authority
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse

OLD = "synthetic-old-" + "a" * 40
NEW = "synthetic-new-" + "b" * 40
OTHER = "synthetic-next-" + "c" * 40


def configuration(tmp_path):
    root = tmp_path / "config"
    root.mkdir(mode=0o700)
    key = Fernet.generate_key()
    for name, value in (("admin-token", OLD.encode()), ("encryption-key", key)):
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value + b"\n")
    return HubConfig(tmp_path / "data", root, "https://memory.example.test", OLD, key)


async def core(scope, receive, send):
    await JSONResponse({"accepted": True})(scope, receive, send)


def client(app, config):
    return AsyncClient(transport=ASGITransport(app), base_url=config.public_origin)


async def login(web, config, credential=OLD):
    response = await web.post(
        "/hub/login", json={"credential": credential}, headers={"Origin": config.public_origin}
    )
    assert response.status_code == 303
    return response


async def change(web, config, current=OLD, replacement=NEW, **kwargs):
    return await web.post(
        ADMIN_CREDENTIAL_PATH,
        json={"current_credential": current, "new_credential": replacement},
        headers={"Origin": config.public_origin, **kwargs},
    )


def test_rotation_revokes_all_browser_sessions_and_old_bearer_and_survives_restart(tmp_path):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")

    async def scenario():
        async with client(app, config) as first, client(app, config) as second:
            await login(first, config)
            await login(second, config)
            reply = await change(first, config)
            assert reply.status_code == 200
            assert reply.json() == {
                "schema_version": 1,
                "state": "credential_changed",
                "login_required": True,
            }
            assert reply.headers["cache-control"] == "no-store"
            assert "Max-Age=0" in reply.headers["set-cookie"]
            assert OLD not in reply.text and NEW not in reply.text
            assert OLD not in str(reply.headers) and NEW not in str(reply.headers)
            assert (await first.get("/api/example")).status_code == 401
            assert (await second.get("/api/example")).status_code == 401
            assert (
                await second.get("/api/example", headers={"Authorization": "Bearer " + OLD})
            ).status_code == 401
            assert (
                await second.get("/api/example", headers={"Authorization": "Bearer " + NEW})
            ).status_code == 200
            await login(first, config, NEW)
            assert (await first.get("/api/example")).status_code == 200
        persisted = read_private_file(config.config_dir / "admin-token").decode().strip()
        assert persisted == NEW
        assert stat.S_IMODE((config.config_dir / "admin-token").stat().st_mode) == 0o600
        restarted = CloudBoundary(core, replace(config, admin_token=persisted), "new-instance")
        async with client(restarted, config) as web:
            await login(web, config, NEW)
            denied = await web.post(
                "/hub/login", json={"credential": OLD}, headers={"Origin": config.public_origin}
            )
            assert denied.status_code == 401

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "replacement",
    [
        "",
        OLD,
        None,
        [],
    ],
)
def test_invalid_new_credentials_preserve_old_authority(tmp_path, replacement):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")

    async def scenario():
        async with client(app, config) as web:
            await login(web, config)
            response = await change(web, config, replacement=replacement)
            assert response.status_code == 422
            assert response.json() == {"detail": "invalid_new_credential"}
            assert (await web.get("/api/example")).status_code == 200
            assert app.admin_credentials.valid(OLD)
            assert read_private_file(config.config_dir / "admin-token").strip() == OLD.encode()

    asyncio.run(scenario())


def test_rotation_requires_identity_origin_even_for_bearer_and_current_credential(tmp_path):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")

    async def scenario():
        async with client(app, config) as web:
            body = {"current_credential": OLD, "new_credential": NEW}
            assert (await change(web, config)).status_code == 401
            response = await web.post(
                ADMIN_CREDENTIAL_PATH, json=body, headers={"Authorization": "Bearer " + OLD}
            )
            assert response.status_code == 403
            assert response.json() == {"detail": "origin_required"}
            response = await change(web, config, Authorization="Bearer synthetic-device-token")
            assert response.status_code == 401
            await login(web, config)
            response = await change(web, config, current="incorrect")
            assert response.status_code == 403
            assert response.json() == {"detail": "current_credential_incorrect"}
            response = await web.post(
                ADMIN_CREDENTIAL_PATH,
                json=body,
                headers={"Origin": "https://attacker.example", "Authorization": "Bearer " + OLD},
            )
            assert response.status_code == 403
            assert app.admin_credentials.valid(OLD)

    asyncio.run(scenario())


def test_rotation_shares_login_throttle_and_bounds_body(tmp_path):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")

    async def scenario():
        async with client(app, config) as web:
            await login(web, config)
            response = await web.post(
                ADMIN_CREDENTIAL_PATH,
                content=b"x" * 4097,
                headers={"Origin": config.public_origin},
            )
            assert response.status_code == 413
            for malformed in (b"broken", b"[]", b"{}"):
                response = await web.post(
                    ADMIN_CREDENTIAL_PATH,
                    content=malformed,
                    headers={"Origin": config.public_origin},
                )
                assert response.status_code == 400
            for _ in range(20):
                assert (await change(web, config, current="incorrect")).status_code == 403
            assert (await change(web, config)).status_code == 429
            response = await web.post(
                "/hub/login", json={"credential": OLD}, headers={"Origin": config.public_origin}
            )
            assert response.status_code == 429
            assert app.admin_credentials.valid(OLD)

    asyncio.run(scenario())


def test_write_failure_before_publication_preserves_old_cookie_and_file(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")

    def fail(*_args):
        raise OSError("synthetic private path " + OLD + NEW)

    monkeypatch.setattr(admin_credentials.os, "replace", fail)

    async def scenario():
        async with client(app, config) as web:
            await login(web, config)
            response = await change(web, config)
            assert response.status_code == 503
            assert response.json() == {"detail": "credential_change_unavailable"}
            assert OLD not in response.text and NEW not in response.text
            assert (await web.get("/api/example")).status_code == 200
            assert app.admin_credentials.valid(OLD)
            assert read_private_file(config.config_dir / "admin-token").strip() == OLD.encode()
            assert not list(config.config_dir.glob(".admin-token-*"))

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["replace_after_publication", "directory_fsync"])
def test_publication_error_reads_back_new_authority_and_revokes_sessions(
    tmp_path, monkeypatch, failure
):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")
    if failure == "replace_after_publication":
        original = os.replace

        def replace_then_fail(*args):
            original(*args)
            raise OSError("synthetic publication uncertainty")

        monkeypatch.setattr(admin_credentials.os, "replace", replace_then_fail)
    else:
        original = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic fsync uncertainty")
            original(fd)

        monkeypatch.setattr(admin_credentials.os, "fsync", fail_directory)

    async def scenario():
        async with client(app, config) as first, client(app, config) as second:
            await login(first, config)
            await login(second, config)
            response = await change(first, config)
            assert response.status_code == 200
            assert (await second.get("/api/example")).status_code == 401
            assert not app.admin_credentials.valid(OLD) and app.admin_credentials.valid(NEW)
            assert read_private_file(config.config_dir / "admin-token").strip() == NEW.encode()

    asyncio.run(scenario())


def test_unreadable_publication_fails_closed_including_old_sessions(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    app = CloudBoundary(core, config, "synthetic-internal")
    read = admin_credentials.read_private_file
    replace_file = os.replace
    published = False

    def replace_then_fail(*args):
        nonlocal published
        replace_file(*args)
        published = True
        raise OSError("synthetic uncertainty")

    def unreadable(path):
        if published:
            raise OSError("synthetic inaccessible credential")
        return read(path)

    monkeypatch.setattr(admin_credentials.os, "replace", replace_then_fail)
    monkeypatch.setattr(admin_credentials, "read_private_file", unreadable)

    async def scenario():
        async with client(app, config) as web:
            await login(web, config)
            response = await change(web, config)
            assert response.status_code == 503
            assert response.json() == {"detail": "admin_credential_unavailable"}
            assert "Max-Age=0" in response.headers["set-cookie"]
            assert (await web.get("/api/example")).status_code == 401
            assert not app.admin_credentials.valid(OLD) and not app.admin_credentials.valid(NEW)

    asyncio.run(scenario())


def test_concurrent_old_credential_changes_do_not_overwrite_new_authority(tmp_path):
    config = configuration(tmp_path)
    state = AdminCredentials(config)

    async def scenario():
        async def attempt(value):
            try:
                await asyncio.to_thread(state.change, OLD, value)
                return "changed"
            except AdminCredentialError as error:
                return error.code

        results = await asyncio.gather(attempt(NEW), attempt(OTHER))
        assert sorted(results) == ["changed", "current_credential_incorrect"]
        assert state.valid(NEW) != state.valid(OTHER)
        assert not state.valid(OLD)

    asyncio.run(scenario())


def test_preexisting_request_filter_observes_new_and_previous_credentials(tmp_path):
    config = configuration(tmp_path)
    state = AdminCredentials(config)
    privacy = CredentialFilter(known_credentials_source=state.known_credentials)
    console = ConsoleRead(None, None, None, privacy)
    request_filter = console.request_privacy("synthetic-browser-session")
    state.change(OLD, NEW)
    text = OLD + " " + NEW + " synthetic-browser-session"
    assert OLD not in request_filter.protect({"text": text}).value["text"]
    assert NEW not in request_filter.protect({"text": text}).value["text"]
    assert NEW not in request_filter.protect_text_fragment(text)
    assert "synthetic-browser-session" not in request_filter.protect_text_fragment(text)
    state.change(NEW, OTHER)
    assert NEW not in privacy.protect({"text": NEW + " " + OTHER}).value["text"]
    assert OTHER not in privacy.protect_text_fragment(OTHER)


def test_privacy_budget_failure_occurs_before_file_change(tmp_path):
    config = configuration(tmp_path)
    state = AdminCredentials(config)
    state._known = tuple(f"synthetic-{n:04d}-" + "x" * 32 for n in range(255))
    with pytest.raises(AdminCredentialError) as raised:
        state.change(OLD, NEW)
    assert raised.value.code == "credential_change_unavailable"
    assert state.valid(OLD)
    assert read_private_file(config.config_dir / "admin-token").strip() == OLD.encode()


def test_dynamic_filter_provider_failure_never_returns_unfiltered_content():
    def unavailable():
        raise RuntimeError("synthetic private detail")

    privacy = CredentialFilter(known_credentials_source=unavailable)
    with pytest.raises(PrivacyError, match="known_credentials_unavailable"):
        privacy.protect({"text": NEW})
    with pytest.raises(PrivacyError, match="known_credentials_unavailable"):
        privacy.protect_text_fragment(NEW)


def test_real_hub_composition_updates_console_preview_and_preserves_device_credential(tmp_path):
    config = replace(configuration(tmp_path), governance_dir=tmp_path / "governance")
    activate_authority(config.config_dir, config.governance_dir, config.encryption_key, new=True)
    app = create_hub(config)

    async def scenario():
        async with app.core.router.lifespan_context(app.core):
            async with client(app, config) as web:
                issued = await web.post(
                    "/hub/v1/devices",
                    headers={"Authorization": "Bearer " + OLD},
                    json={"display_name": "Synthetic paired device"},
                )
                assert issued.status_code == 201
                device_credential = issued.json()["credential"]
                before_device = app.governance.access.authenticate(device_credential)
                request_filter = app.core.state.cmh_console.request_privacy("synthetic-session")
                preview_filter = app.core.state.cmh_previews.privacy
                await login(web, config)
                assert (await change(web, config)).status_code == 200
                assert NEW not in request_filter.protect_text_fragment(NEW)
                assert NEW not in preview_filter.protect({"text": NEW}).value["text"]
                assert app.governance.access.authenticate(device_credential) == before_device

    asyncio.run(scenario())
