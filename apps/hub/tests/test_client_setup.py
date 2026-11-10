"""FR-02/03/06/07/12: pairing files, real authorization and retained local queues."""

import asyncio
import json
import os
import tomllib
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import test_transport
from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.client_setup import (
    ClientSetupError,
    PairingBundle,
    client_status,
    import_pairing,
)
from cloud_memory_hub.transport import TransportError
from test_agent_access import read_grants
from test_read_transport import json_response
from test_transport import bind_live, connection_data, queue_for

live_hub = test_transport.live_hub


def pairing(tmp_path, **overrides):
    raw = {
        **connection_data(),
        "kind": "cloud-memory-hub-pairing",
        "source_id": str(uuid4()),
        "scope_id": str(uuid4()),
        **overrides,
    }
    path = tmp_path / "pairing.json"
    path.write_text(json.dumps(raw))
    path.chmod(0o644)
    return path, raw


def authorized(raw, *, fail=None):
    def handle(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer " + raw["credential"]
        if fail:
            return httpx.Response(fail)
        if request.url.path.endswith("/session"):
            return json_response(
                {
                    "protocol_version": 1,
                    "instance_id": raw["instance_id"],
                    "features": ["device_identity", "event_upload", "source_preflight"],
                    "device": {
                        "device_id": raw["device_id"],
                        "status": "active",
                        "grants": [
                            {
                                "scope_id": raw["scope_id"],
                                "permissions": ["events:write", "memory:read", "history:read"],
                            }
                        ],
                    },
                }
            )
        return json_response(
            {
                "protocol_version": 1,
                **{key: raw[key] for key in ("instance_id", "device_id", "source_id", "scope_id")},
                "allowed": True,
            }
        )

    return httpx.MockTransport(handle)


def install(tmp_path):
    path, raw = pairing(tmp_path)
    result = asyncio.run(
        import_pairing(path, client_root=tmp_path / "clients", transport=authorized(raw))
    )
    return path, raw, Path(result["config_file"])


def test_import_checks_remote_and_tightens_download_and_configs(tmp_path):
    path, raw, config = install(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    settings = AgentSettings.from_file(config)
    assert settings.scope_id == raw["scope_id"]
    assert settings.open().outbox.status()["queued"] == 0
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert settings.connection_file.stat().st_mode & 0o777 == 0o600
    assert raw["credential"] not in config.read_text()
    assert str(config.parent) not in path.read_text()
    assert not list(config.parent.parent.glob(".pairing-*"))


def test_repeat_import_keeps_pending_event_and_original_binding(tmp_path):
    path, raw, config = install(tmp_path)
    settings = AgentSettings.from_file(config)
    service = settings.open()
    _, event = queue_for(tmp_path / "synthetic", service.connection)
    event.update(source_id=raw["source_id"], scope_id=raw["scope_id"])
    service.outbox.enqueue(event)
    before = settings.state_dir.joinpath("outbox.sqlite3").read_bytes()
    result = asyncio.run(
        import_pairing(path, client_root=config.parent.parent, transport=authorized(raw))
    )
    assert result["status"] == "already_installed"
    assert settings.state_dir.joinpath("outbox.sqlite3").read_bytes() == before
    assert settings.open().outbox.status()["queued"] == 1
    raw["source_id"] = str(uuid4())
    path.write_text(json.dumps(raw))
    with pytest.raises(ClientSetupError, match="existing_client_binding_conflict"):
        asyncio.run(
            import_pairing(path, client_root=config.parent.parent, transport=authorized(raw))
        )
    assert settings.state_dir.joinpath("outbox.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "device_authentication_required", False),
        (503, "remote_temporarily_unavailable", True),
        (302, "remote_protocol_invalid", False),
    ],
)
def test_failed_authorization_leaves_no_installed_configuration(tmp_path, status, code, retryable):
    path, raw = pairing(tmp_path)
    with pytest.raises(TransportError, match=code) as raised:
        asyncio.run(
            import_pairing(
                path, client_root=tmp_path / "clients", transport=authorized(raw, fail=status)
            )
        )
    assert raised.value.retryable is retryable
    assert not (tmp_path / "clients").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("kind", "foreign"),
        ("origin", "http://public.example.test"),
        ("scope_id", "wrong"),
        ("source_id", "wrong"),
        ("connection_file", "/tmp/foreign"),
        ("credential", "malformed-synthetic-device-value"),
    ],
)
def test_download_contract_rejects_unsafe_fields_without_echo(tmp_path, field, value):
    path, raw = pairing(tmp_path, **{field: value})
    with pytest.raises(ClientSetupError, match="pairing_file_invalid") as raised:
        PairingBundle.from_download(path)
    assert raw["credential"] not in str(raised.value)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "large"])
def test_download_rejects_unsafe_files(tmp_path, kind):
    source, _ = pairing(tmp_path)
    path = tmp_path / "unsafe"
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b" " * 4097)
    with pytest.raises(ClientSetupError, match="pairing_file_invalid"):
        PairingBundle.from_download(path)


def test_local_failure_publishes_no_partial_client(tmp_path, monkeypatch):
    import cloud_memory_hub.client_setup as setup

    path, raw = pairing(tmp_path)
    root = tmp_path / "clients"

    def failed(*args, **kwargs):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(setup.LocalOutbox, "initialize", failed)
    with pytest.raises(OSError):
        asyncio.run(import_pairing(path, client_root=root, transport=authorized(raw)))
    assert not list(root.iterdir())


def test_empty_queue_status_performs_explicit_remote_check(tmp_path):
    _, raw, config = install(tmp_path)
    local = asyncio.run(client_status(config))
    assert local["queue"]["queued"] == 0
    assert local["remote"]["status"] == "not_checked"
    remote = asyncio.run(
        client_status(config, check_remote=True, transport=authorized(raw, fail=401))
    )
    assert remote["remote"] == {
        "status": "unavailable",
        "error": "device_authentication_required",
        "retryable": False,
    }
    assert raw["credential"] not in json.dumps(remote)


@pytest.mark.parametrize("change", ["instance", "read_grant", "source", "scope"])
def test_probe_rejects_mismatched_authority_before_install(tmp_path, change):
    path, raw = pairing(tmp_path)
    original = authorized(raw)

    def handle(request):
        response = original.handler(request)
        body = json.loads(response.stream.data)
        if change == "instance":
            body["instance_id"] = str(uuid4())
        elif change == "read_grant" and "device" in body:
            body["device"]["grants"][0]["permissions"] = ["events:write"]
        elif change in {"source", "scope"} and "allowed" in body:
            body[change + "_id"] = str(uuid4())
        return json_response(body)

    with pytest.raises(TransportError):
        asyncio.run(
            import_pairing(
                path, client_root=tmp_path / "clients", transport=httpx.MockTransport(handle)
            )
        )
    assert not (tmp_path / "clients").exists()


@pytest.mark.parametrize("format_name", ["json", "codex"])
def test_cli_mcp_configuration_contains_paths_without_credentials(
    tmp_path, monkeypatch, capsys, format_name
):
    import cloud_memory_hub.setup_cli as cli

    _, raw, config = install(tmp_path)
    commands = tmp_path / "installed/bin"
    commands.mkdir(parents=True)
    executable = commands / "cloud-memory-mcp"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setattr(cli.sys, "executable", str(commands / "python"))
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["cloud-memory-setup", "mcp-config", "--config-file", str(config), "--format", format_name],
    )
    previous_umask = os.umask(0o077)
    try:
        assert cli.main() == 0
    finally:
        os.umask(previous_umask)
    output = capsys.readouterr()
    parsed = tomllib.loads(output.out) if format_name == "codex" else json.loads(output.out)
    server = parsed["mcp_servers" if format_name == "codex" else "mcpServers"]["cloud-memory-hub"]
    assert server == {"command": str(executable), "args": ["--config-file", str(config)]}
    assert raw["credential"] not in output.out + output.err


def test_live_pairing_accepts_granted_project_and_observes_revocation(live_hub, tmp_path):
    config, _, _ = live_hub

    async def scenario():
        connection, admin, _, event = await bind_live(config)
        async with httpx.AsyncClient(base_url=config.public_origin, trust_env=False) as client:
            await read_grants(client, admin, connection.device_id, event["scope_id"])
            path, raw = pairing(
                tmp_path,
                **connection.model_dump(mode="json"),
                source_id=event["source_id"],
                scope_id=event["scope_id"],
            )
            raw["credential"] = connection.credential.get_secret_value()
            path.write_text(json.dumps(raw))
            installed = await import_pairing(path, client_root=tmp_path / "clients")
            location = Path(installed["config_file"])
            assert (await client_status(location, check_remote=True))["remote"][
                "status"
            ] == "authorized"
            revoked = await client.post(
                f"/hub/v1/devices/{connection.device_id}/revoke",
                headers=admin,
                json={"expected_revision": 3},
            )
            assert revoked.status_code == 200
            assert (await client_status(location, check_remote=True))["remote"][
                "error"
            ] == "device_authentication_required"
            with pytest.raises(TransportError, match="device_authentication_required"):
                await import_pairing(path, client_root=tmp_path / "second")

    asyncio.run(scenario())
