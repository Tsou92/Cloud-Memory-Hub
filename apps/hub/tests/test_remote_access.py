import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    NamedResource,
    RevisionRequest,
)
from pydantic import ValidationError


def named(value):
    return NamedResource(display_name=value)


def update(revision, scope="global", permissions=("memory:read",)):
    return GrantRequest(
        expected_revision=revision, grants=[{"scope_id": scope, "permissions": permissions}]
    )


@pytest.fixture
def store(tmp_path):
    return DeviceAccessStore(tmp_path / "config" / "access.sqlite3")


def test_persistence_no_plaintext_and_explicit_scope_authorization(store):
    project = store.create_project(named("Project A"))
    other = store.create_project(named("Project B"))
    issued = store.create_device(named("Mac A"))
    token = issued.credential.get_secret_value()
    device = issued.device["device_id"]
    assert token not in repr(issued)
    assert issued.device["grants"] == []
    assert token.encode() not in store.path.read_bytes()
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(AccessError, match="scope_not_allowed"):
        store.require_permission(token, project["project_id"], "memory:read")
    changed = store.replace_grants(device, update(1, project["project_id"]))
    assert changed["revision"] == 2
    restarted = DeviceAccessStore(store.path)
    assert restarted.instance_id == store.instance_id
    assert restarted.authenticate(token)["revision"] == 2
    assert len(restarted.list_projects()) == 2
    assert restarted.require_permission(token, project["project_id"], "memory:read")
    for scope, permission in [
        ("global", "memory:read"),
        (other["project_id"], "memory:read"),
        (project["project_id"], "events:write"),
    ]:
        with pytest.raises(AccessError, match="scope_not_allowed"):
            restarted.require_permission(token, scope, permission)
    restarted.replace_grants(device, GrantRequest(expected_revision=2, grants=[]))
    with pytest.raises(AccessError, match="scope_not_allowed"):
        store.require_permission(token, project["project_id"], "memory:read")
    assert "credential_hash" not in str(store.list_devices())


def test_rotation_revocation_and_lost_response_recovery(store):
    issued = store.create_device(named("Mac"))
    device = issued.device["device_id"]
    original = issued.credential.get_secret_value()
    replacement = store.rotate(device, RevisionRequest(expected_revision=1))
    token = replacement.credential.get_secret_value()
    with pytest.raises(AccessError, match="invalid_device_credential"):
        store.authenticate(original)
    assert store.authenticate(token)["revision"] == 2
    with pytest.raises(AccessError, match="revision_conflict"):
        store.rotate(device, RevisionRequest(expected_revision=1))
    revoked = store.revoke(device, RevisionRequest(expected_revision=2))
    assert revoked["status"] == "revoked" and revoked["grants"] == []
    restarted = DeviceAccessStore(store.path)
    with pytest.raises(AccessError, match="invalid_device_credential"):
        restarted.authenticate(token)
    for operation, request in [
        (restarted.rotate, RevisionRequest(expected_revision=3)),
        (restarted.replace_grants, update(3)),
    ]:
        with pytest.raises(AccessError, match="device_revoked"):
            operation(device, request)
    fresh = restarted.create_device(named("Mac"))
    assert fresh.device["device_id"] != device
    assert token.encode() not in store.path.read_bytes()


def test_unknown_scope_rejects_whole_update_and_concurrent_revision_is_atomic(store):
    issued = store.create_device(named("Mac"))
    device = issued.device["device_id"]
    store.replace_grants(device, update(1))
    request = GrantRequest(
        expected_revision=2,
        grants=[
            {"scope_id": "global", "permissions": ["events:write"]},
            {"scope_id": str(uuid4()), "permissions": ["memory:read"]},
        ],
    )
    with pytest.raises(AccessError, match="unknown_scope"):
        store.replace_grants(device, request)
    assert store.list_devices()[0]["revision"] == 2
    assert store.list_devices()[0]["grants"][0]["permissions"] == ["memory:read"]

    def compete(permission):
        try:
            return store.replace_grants(device, update(2, permissions=(permission,)))["revision"]
        except AccessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ["memory:read", "events:write"]))
    assert sorted(map(str, results)) == ["3", "revision_conflict"]


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_revision": True, "grants": []},
        {"expected_revision": "1", "grants": []},
        {"expected_revision": 0, "grants": []},
        {"expected_revision": 1, "grants": [], "admin": True},
        {"expected_revision": 1, "grants": [{"scope_id": "*", "permissions": ["memory:read"]}]},
        {"expected_revision": 1, "grants": [{"scope_id": "global", "permissions": ["admin"]}]},
        {
            "expected_revision": 1,
            "grants": [{"scope_id": "global", "permissions": ["memory:read", "memory:read"]}],
        },
        {
            "expected_revision": 1,
            "grants": [{"scope_id": "global", "permissions": ["memory:read"]}] * 2,
        },
    ],
)
def test_contract_rejects_ambiguous_or_widened_grants(payload):
    with pytest.raises(ValidationError):
        GrantRequest.model_validate(payload)


def test_private_store_rejects_links_and_insecure_permissions(store):
    link = store.path.parent / "alias.sqlite3"
    link.symlink_to(store.path)
    with pytest.raises(AccessError, match="access_store_unavailable"):
        DeviceAccessStore(link)
    link.unlink()
    os.link(store.path, link)
    with pytest.raises(AccessError, match="unsafe"):
        DeviceAccessStore(link)
    link.unlink()
    store.path.chmod(0o644)
    with pytest.raises(AccessError, match="unsafe"):
        DeviceAccessStore(store.path)


def test_missing_changed_or_future_store_fails_closed(store):
    issued = store.create_device(named("Mac"))
    token = issued.credential.get_secret_value()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE instance SET instance_id=?", (str(uuid4()),))
    with pytest.raises(AccessError, match="access_store_changed"):
        store.authenticate(token)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(AccessError, match="version_unsupported"):
        DeviceAccessStore(store.path)
    store.path.unlink()
    with pytest.raises(AccessError, match="unavailable"):
        store.authenticate(token)
    assert not store.path.exists()


def test_corrupted_grants_are_not_used_as_authority(store):
    issued = store.create_device(named("Mac"))
    device = issued.device["device_id"]
    token = issued.credential.get_secret_value()
    store.replace_grants(device, update(1))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE grants SET permissions=?", ('["administrator"]',))
    with pytest.raises(AccessError, match="access_store_unrecognized"):
        store.authenticate(token)


def test_unrecognized_database_is_preserved_and_not_adopted(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    path = directory / "access.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserve me')")
    path.chmod(0o600)
    with pytest.raises(AccessError, match="access_store_unrecognized"):
        DeviceAccessStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone()[0] == "preserve me"


@pytest.mark.parametrize("token", ["", "a" * 40, "Bearer secret", "中文", "cmhdev_v1_bad"])
def test_invalid_credentials_are_generic(store, token):
    with pytest.raises(AccessError, match="invalid_device_credential") as failure:
        store.authenticate(token)
    assert failure.value.status == 401
