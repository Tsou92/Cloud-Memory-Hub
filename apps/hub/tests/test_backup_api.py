"""ADR-0063: private administrator backup controls and real filesystem integration."""

import asyncio
import errno
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import test_backup_store
from cloud_memory_hub import backup_catalog, backup_runtime, backup_store
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.backup_api import backup_routes
from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.remote_access import DeviceAccessStore, NamedResource
from cloud_memory_hub.remote_api import create_remote_api
from cloud_memory_hub.storage_lease import StorageLease
from starlette.responses import JSONResponse
from test_boundary import configuration
from test_remote_ingest import config_for

PREFIX = "/hub/v1/backups/"
PLAN = {"enabled": True, "interval_minutes": 1440, "keep": 7}
OPERATIONS = [
    ("GET", "status", None),
    ("GET", "versions", None),
    ("PUT", "schedule", PLAN),
    ("POST", "request", {}),
]
layout = test_backup_store.layout


def boundary(config):
    async def core(scope, receive, send):
        await JSONResponse({"unexpected_core_call": True})(scope, receive, send)

    api = create_remote_api(None, None, None, None)
    api.routes.extend(backup_routes(config))
    return CloudBoundary(core, config, "synthetic-internal", remote_api=api)


@pytest.fixture
def setup(tmp_path):
    config = replace(configuration(tmp_path), backup_dir=tmp_path / "backups")
    return config, boundary(config)


def client(config, app, *, admin=True):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url=config.public_origin,
        headers={"Authorization": "Bearer " + config.admin_token} if admin else {},
    )


def test_real_control_files_persist_schedule_and_queue_only(setup):
    config, app = setup

    async def scenario():
        async with client(config, app) as web:
            initial = await web.get(PREFIX + "status")
            assert initial.status_code == 200
            assert initial.json()["execution"]["state"] == "not_run"
            assert initial.json()["supervisor_running"] == "not_checked"
            saved = await web.put(PREFIX + "schedule", json=PLAN)
            assert saved.status_code == 200 and saved.json() == backup_runtime.read_plan(
                config.backup_dir
            )
            queued = await web.post(PREFIX + "request", json={})
            assert queued.status_code == 202 and queued.json()["status"] == "queued"
            status = (await web.get(PREFIX + "status")).json()
            assert status["pending_request"] == queued.json()["request_id"]
            assert status["execution"]["state"] == "not_run"
            assert status["next_due_at"] is not None
            duplicate = await web.post(PREFIX + "request", json={})
            assert duplicate.status_code == 409
            assert duplicate.json() == {"detail": "backup_request_pending"}
            assert (await web.get(PREFIX + "status")).json()["pending_request"] == queued.json()[
                "request_id"
            ]
            assert (await web.get(PREFIX + "versions")).json() == {"versions": []}
            disabled = {"enabled": False, "interval_minutes": 525600, "keep": 365}
            assert (await web.put(PREFIX + "schedule", json=disabled)).status_code == 200
            assert (await web.get(PREFIX + "status")).json()["next_due_at"] is None
            for reply in (initial, saved, queued, duplicate):
                assert reply.headers["cache-control"] == "no-store"
                assert (
                    config.admin_token not in reply.text
                    and str(config.backup_dir) not in reply.text
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("method,operation,body", OPERATIONS)
def test_device_and_unauthenticated_calls_are_rejected_without_side_effects(
    setup, method, operation, body
):
    config, app = setup
    store = DeviceAccessStore(config.config_dir / "device-test.sqlite3")
    credential = store.create_device(
        NamedResource(display_name="Synthetic API device")
    ).credential.get_secret_value()
    store.authenticate(credential)

    async def scenario():
        async with client(config, app, admin=False) as web:
            for headers in ({}, {"Authorization": "Bearer " + credential}):
                reply = await web.request(method, PREFIX + operation, json=body, headers=headers)
                assert reply.status_code == 403
                assert reply.json() == {"detail": "administrator_required"}

    asyncio.run(scenario())
    assert not config.backup_dir.exists()


@pytest.mark.parametrize("method,operation,body", OPERATIONS)
def test_cross_origin_and_path_queries_rejected(setup, method, operation, body):
    config, app = setup

    async def scenario():
        async with client(config, app) as web:
            reply = await web.request(
                method, PREFIX + operation, json=body, headers={"Origin": "https://other.invalid"}
            )
            assert reply.status_code == 403 and reply.json() == {"detail": "origin_not_allowed"}
            reply = await web.request(
                method, PREFIX + operation + "?path=/synthetic/private", json=body
            )
            assert reply.status_code == 422 and reply.json() == {"detail": "invalid_request"}

    asyncio.run(scenario())
    assert not config.backup_dir.exists()


def test_cookie_mutations_require_same_origin(setup):
    config, app = setup

    async def scenario():
        async with client(config, app, admin=False) as web:
            login = await web.post(
                "/hub/login",
                data={"credential": config.admin_token},
                headers={"Origin": config.public_origin},
            )
            assert login.status_code == 303
            for method, operation, body in OPERATIONS[2:]:
                reply = await web.request(method, PREFIX + operation, json=body)
                assert reply.status_code == 403 and reply.json() == {"detail": "origin_required"}
                allowed = await web.request(
                    method, PREFIX + operation, json=body, headers={"Origin": config.public_origin}
                )
                assert allowed.status_code in {200, 202}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"enabled": "true"},
        {"interval_minutes": True},
        {"interval_minutes": 0},
        {"interval_minutes": 525601},
        {"interval_minutes": 1.5},
        {"keep": False},
        {"keep": 0},
        {"keep": 366},
        {"keep": "7"},
        {"path": "/synthetic/private"},
        {"command": "synthetic"},
    ],
)
def test_schedule_is_strict_and_rejects_extra_fields(setup, changes):
    config, app = setup

    async def scenario():
        async with client(config, app) as web:
            reply = await web.put(PREFIX + "schedule", json={**PLAN, **changes})
            assert reply.status_code == 422 and reply.json() == {"detail": "invalid_request"}

    asyncio.run(scenario())
    assert not config.backup_dir.exists()


@pytest.mark.parametrize(
    "operation,method,body,content_type,expected",
    [
        ("request", "POST", '{"path":"/synthetic/private"}', "application/json", 422),
        ("request", "POST", "[]", "application/json", 422),
        ("schedule", "PUT", "{}", "application/json", 422),
        (
            "schedule",
            "PUT",
            '{"enabled":true,"enabled":false,"interval_minutes":1,"keep":1}',
            "application/json",
            422,
        ),
        ("request", "POST", "{}", "text/plain", 422),
        ("request", "POST", " " * 4097, "application/json", 413),
    ],
)
def test_invalid_bodies_never_mutate_control_files(
    setup, operation, method, body, content_type, expected
):
    config, app = setup

    async def scenario():
        async with client(config, app) as web:
            reply = await web.request(
                method, PREFIX + operation, content=body, headers={"Content-Type": content_type}
            )
            assert reply.status_code == expected
            assert "synthetic/private" not in reply.text

    asyncio.run(scenario())
    assert not config.backup_dir.exists()


def test_versions_only_read_unverified_summaries_and_never_return_files(layout):
    original, root = layout
    config = replace(original, backup_dir=root)
    manifest = backup_store.create_backup(config, root, revision="synthetic-api-1")
    app = boundary(config)

    async def scenario():
        async with client(config, app) as web:
            reply = await web.get(PREFIX + "versions")
            assert reply.status_code == 200
            versions = reply.json()["versions"]
            assert len(versions) == 1
            assert set(versions[0]) == {
                "backup_id",
                "state",
                "created_at",
                "revision",
                "file_count",
                "total_bytes",
            }
            assert (
                versions[0]["backup_id"] == manifest["backup_id"]
                and versions[0]["state"] == "unverified"
            )
            assert (
                config.admin_token not in reply.text
                and "synthetic-probe.private.json" not in reply.text
            )
            (root / manifest["backup_id"] / "config/synthetic-probe.private.json").write_bytes(
                b"synthetic corrupted contents"
            )
            damaged = await web.get(PREFIX + "versions")
            # Payload corruption is deliberately not a claim this online catalog can detect.
            assert damaged.json() == reply.json()
            (root / manifest["backup_id"] / backup_store.MANIFEST).write_text('{"secret":"bad"}')
            invalid = await web.get(PREFIX + "versions")
            assert invalid.status_code == 503
            assert invalid.json() == {"detail": "backup_catalog_invalid"}
            with StorageLease(root):
                busy = await web.get(PREFIX + "versions")
            assert busy.status_code == 503 and busy.json() == {"detail": "backup_storage_in_use"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "setting,code",
    [
        ("MAX_CANDIDATES", "backup_catalog_limit"),
        ("MAX_TOTAL_BYTES", "backup_catalog_limit"),
        ("DEADLINE_SECONDS", "backup_catalog_timeout"),
    ],
)
def test_catalog_budget_refusals_are_fixed_503_without_partial_versions(
    layout, monkeypatch, setting, code
):
    original, root = layout
    config = replace(original, backup_dir=root)
    backup_store.create_backup(config, root, revision="synthetic-api-limit")
    monkeypatch.setattr(backup_catalog, setting, 0)

    async def scenario():
        async with client(config, boundary(config)) as web:
            reply = await web.get(PREFIX + "versions")
            assert reply.status_code == 503
            assert reply.json() == {"detail": code}

    asyncio.run(scenario())
    with StorageLease(root):
        pass


@pytest.mark.parametrize(
    "operation,method,body", [("schedule", "PUT", PLAN), ("request", "POST", {})]
)
@pytest.mark.parametrize("error_number", [errno.EROFS, errno.ENOSPC])
def test_write_faults_return_fixed_codes_without_paths(
    setup, monkeypatch, operation, method, body, error_number
):
    config, app = setup

    def fail(*args):
        raise OSError(
            error_number, "synthetic credential payload", str(config.backup_dir / "private.json")
        )

    monkeypatch.setattr(backup_runtime, "_write", fail)

    async def scenario():
        async with client(config, app) as web:
            reply = await web.request(method, PREFIX + operation, json=body)
            assert reply.status_code == 503 and reply.json() == {
                "detail": "backup_control_unavailable"
            }

    asyncio.run(scenario())


def test_invalid_control_file_and_unsafe_versions_root_return_fixed_errors(setup):
    config, app = setup
    config.backup_dir.mkdir(mode=0o700)
    control = config.backup_dir / "schedule.json"
    control.write_text('{"private":"synthetic secret"}')
    control.chmod(0o600)

    async def scenario():
        async with client(config, app) as web:
            reply = await web.get(PREFIX + "status")
            assert reply.status_code == 503 and reply.json() == {
                "detail": "backup_control_unavailable"
            }
            config.backup_dir.chmod(0o755)
            reply = await web.get(PREFIX + "versions")
            assert reply.status_code == 503 and reply.json() == {"detail": "backup_catalog_invalid"}

    asyncio.run(scenario())


@pytest.mark.parametrize("method,operation,body", OPERATIONS)
def test_sync_backup_operations_leave_event_loop_responsive(
    setup, monkeypatch, method, operation, body
):
    config, app = setup
    module, name = {
        "status": (backup_runtime, "status"),
        "versions": (backup_catalog, "list_catalog"),
        "schedule": (backup_runtime, "configure"),
        "request": (backup_runtime, "request_backup"),
    }[operation]
    original = getattr(module, name)
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()

    def blocked(*args, **kwargs):
        assert threading.get_ident() != caller
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, blocked)

    async def scenario():
        async with client(config, app) as web:
            work = asyncio.create_task(web.request(method, PREFIX + operation, json=body))
            try:
                async with asyncio.timeout(1):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                    health = await web.get("/healthz")
                    assert health.status_code == 200
            finally:
                release.set()
            assert (await work).status_code in {200, 202}

    asyncio.run(scenario())


def test_real_hub_route_registration_and_environment_configuration(tmp_path, monkeypatch):
    config = replace(config_for(tmp_path), backup_dir=tmp_path / "backups")
    app = create_hub(config)

    async def scenario():
        async with client(config, app) as web:
            assert (await web.get(PREFIX + "status")).status_code == 200
            assert (await web.put(PREFIX + "schedule", json=PLAN)).status_code == 200

    asyncio.run(scenario())
    monkeypatch.setenv("CMH_CONFIG_DIR", str(config.config_dir))
    monkeypatch.setenv("CMH_BACKUP_DIR", str(config.backup_dir))
    monkeypatch.setattr(
        "cloud_memory_hub.config.read_private_file",
        lambda path: config.encryption_key
        if path.name == "encryption-key"
        else config.admin_token.encode(),
    )
    assert HubConfig.from_environment().backup_dir == config.backup_dir
    monkeypatch.delenv("CMH_BACKUP_DIR")
    assert HubConfig.from_environment().backup_dir == Path("/backups")
