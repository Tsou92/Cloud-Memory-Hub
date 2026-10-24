"""Composition only: persistence, memory processing and UI remain upstream."""

import secrets

from one_memory.api import _sdk_library_bridge, create_app
from one_memory.config import Settings
from pydantic import SecretStr

from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.remote_access import DeviceAccessStore
from cloud_memory_hub.remote_api import create_remote_api
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.remote_read import RemoteRead
from cloud_memory_hub.secret_store import EncryptedSecretStore


def create_hub(config: HubConfig) -> CloudBoundary:
    internal_token = secrets.token_urlsafe(48)
    settings = Settings(
        _env_file=None,
        data_dir=config.data_dir,
        source_home=config.data_dir / "remote-sources",
        api_token=SecretStr(internal_token),
        imports_enabled=False,
        live_sync_enabled=False,
        precipitation_max_concurrent_tasks=1,
    )
    store = EncryptedSecretStore(
        config.config_dir / "model-credentials.sqlite3", config.encryption_key
    )
    devices = DeviceAccessStore(config.config_dir / "remote-access.sqlite3")
    ingest = RemoteIngest(devices, lambda: core.state.memory_write_queue)
    core = create_app(
        settings,
        secret_store=store,
        manage_runtime=True,
        source_write_authorizer=ingest.source_write_authorizer,
    )
    core.state.cmh_ingest = ingest
    reads = RemoteRead(devices, lambda: _sdk_library_bridge(core))
    core.state.cmh_reads = reads
    return CloudBoundary(
        core, config, internal_token, remote_api=create_remote_api(devices, ingest, reads)
    )
