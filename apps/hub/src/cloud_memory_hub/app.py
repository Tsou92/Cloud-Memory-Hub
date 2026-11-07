"""Composition only: persistence, memory processing and UI remain upstream."""

import secrets
from contextlib import asynccontextmanager

from one_memory.api import _sdk_library_bridge, create_app
from one_memory.config import Settings
from pydantic import SecretStr

from cloud_memory_hub.boundary import CloudBoundary
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_execution import ExecutionStore, GovernanceExecutor
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_preview import GovernancePreview
from cloud_memory_hub.governance_purge import Purger
from cloud_memory_hub.output_lineage import OutputLineage
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.remote_api import create_remote_api
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.remote_read import RemoteRead
from cloud_memory_hub.sdk_admission import SdkAdmission
from cloud_memory_hub.sdk_reconcile import SdkReconciler
from cloud_memory_hub.secret_store import EncryptedSecretStore
from cloud_memory_hub.source_history import SourceHistory
from cloud_memory_hub.unified_read import UnifiedReadBridge


def create_hub(config: HubConfig) -> CloudBoundary:
    governance = open_authority(config)
    governance.require_ready()
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
    devices = governance.access
    sdk_admission = SdkAdmission()
    ingest = RemoteIngest(devices, lambda: core.state.memory_write_queue)
    lineage = OutputLineage(ingest, lambda: _sdk_library_bridge(core), governance)
    reconciler = SdkReconciler(
        lambda: core.state.memory_write_queue,
        lambda: _sdk_library_bridge(core),
        governance,
        observe_outputs=lineage.observe,
    )

    async def reconcile_unknown():
        # Finish a forget interrupted by a crash before any further writes.
        await core.state.cmh_executor.resume()
        await reconciler.run()
        await lineage.reconcile_completed()

    suppressions = ExecutionStore(governance.journal)
    ingest.suppressed = suppressions.suppressed

    def source_write_authorizer(source_key):
        try:
            governance.require_writes_ready()
        except GovernanceError:
            return False
        return ingest.source_write_authorizer(source_key)

    core = create_app(
        settings,
        secret_store=store,
        manage_runtime=True,
        source_write_authorizer=source_write_authorizer,
        sdk_operation_guard=sdk_admission.operation,
        sdk_reconcile_unknown=reconcile_unknown,
        sdk_output_observer=lineage.observe,
    )
    core.state.cmh_reconciler = reconciler
    core.state.cmh_lineage = lineage
    core.state.cmh_sdk_admission = sdk_admission
    core.state.cmh_ingest = ingest
    core.state.cmh_governance = governance
    originals = SourceHistory(ingest)
    reads = RemoteRead(
        devices,
        lambda: UnifiedReadBridge(_sdk_library_bridge(core), originals, lineage),
        suppressions.suppressed,
    )
    core.state.cmh_reads = reads
    previews = GovernancePreview(
        governance,
        lambda: _sdk_library_bridge(core),
        CredentialFilter((config.admin_token, config.encryption_key.decode())),
        lineage,
    )
    core.state.cmh_previews = previews
    executor = GovernanceExecutor(
        governance,
        lambda: _sdk_library_bridge(core),
        previews,
        sdk_admission,
        lineage,
        Purger(ingest, lambda: _sdk_library_bridge(core), config.data_dir),
    )
    core.state.cmh_executor = executor
    # Before serving or draining: finish interrupted executions and re-apply
    # completed purges to restored business data (ADR-0032).
    core_lifespan = core.router.lifespan_context

    @asynccontextmanager
    async def governed_lifespan(app):
        async with core_lifespan(app) as state:
            await executor.resume()
            yield state

    core.router.lifespan_context = governed_lifespan
    return CloudBoundary(
        core,
        config,
        internal_token,
        remote_api=create_remote_api(devices, ingest, reads, previews, executor),
        governance=governance,
    )
