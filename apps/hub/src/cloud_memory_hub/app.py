"""Composition only: persistence, memory processing and UI remain upstream."""

import secrets
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.routing import Route

from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.diagnostics import initialize_diagnostics


def create_hub(config: HubConfig):
    initialize_diagnostics()
    from one_memory.api import _sdk_library_bridge, create_app
    from one_memory.config import Settings
    from pydantic import SecretStr

    from cloud_memory_hub.admin_credentials import AdminCredentials
    from cloud_memory_hub.backup_api import backup_routes
    from cloud_memory_hub.boundary import CloudBoundary
    from cloud_memory_hub.console_api import console_routes, usage_routes
    from cloud_memory_hub.console_qa import ConsoleQuestions, console_question_routes
    from cloud_memory_hub.console_read import ConsoleRead
    from cloud_memory_hub.governance_authority import open_authority
    from cloud_memory_hub.governance_execution import GovernanceExecutor
    from cloud_memory_hub.governance_inflight import InflightGovernance
    from cloud_memory_hub.governance_journal import GovernanceError
    from cloud_memory_hub.governance_preview import GovernancePreview
    from cloud_memory_hub.governance_purge import Purger
    from cloud_memory_hub.governance_sources import SourceSuppression
    from cloud_memory_hub.output_lineage import OutputLineage
    from cloud_memory_hub.privacy import CredentialFilter
    from cloud_memory_hub.remote_api import create_remote_api
    from cloud_memory_hub.remote_ingest import RemoteIngest
    from cloud_memory_hub.remote_mcp import MCP_PATH, RemoteMCP
    from cloud_memory_hub.remote_read import RemoteRead
    from cloud_memory_hub.sdk_admission import SdkAdmission
    from cloud_memory_hub.sdk_generation import SdkGeneration
    from cloud_memory_hub.sdk_reconcile import SdkReconciler
    from cloud_memory_hub.secret_store import EncryptedSecretStore
    from cloud_memory_hub.source_history import SourceHistory
    from cloud_memory_hub.unified_read import UnifiedReadBridge

    governance = open_authority(config)
    governance.require_ready()
    internal_token = secrets.token_urlsafe(48)
    admin_credentials = AdminCredentials(config)
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

    suppressions = governance.execution
    ingest.suppressed = suppressions.suppressed
    source_suppressions = SourceSuppression(suppressions, ingest)
    ingest.require_source_allowed = source_suppressions.require_allowed
    ingest.require_source_rules = source_suppressions.targets
    generation = SdkGeneration(governance, source_suppressions)
    ingest.enroll_request = generation.enroll

    def claims_ready():
        if not core.state.cmh_executor.ready:
            return False
        try:
            governance.require_writes_ready()
        except GovernanceError:
            return False
        return True

    def source_write_authorizer(source_key):
        # Preserve the established runtime-fault fence at every invocation.
        # Only the separate claim hook observes startup readiness; quiescence
        # never changes a worker's source permission while it is finishing.
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
        sdk_claim_ready=claims_ready,
        sdk_operation_guard=sdk_admission.operation,
        sdk_reconcile_unknown=reconcile_unknown,
        sdk_output_observer=lineage.observe,
        sdk_generation_permit=generation.permit,
    )
    core.state.cmh_reconciler = reconciler
    core.state.cmh_lineage = lineage
    core.state.cmh_sdk_admission = sdk_admission
    core.state.cmh_ingest = ingest
    core.state.cmh_governance = governance
    core.state.cmh_generation = generation
    originals = SourceHistory(ingest)
    reads = RemoteRead(
        devices,
        lambda: UnifiedReadBridge(_sdk_library_bridge(core), originals, lineage),
        suppressions.suppressed,
        originals=originals,
        known_credentials_source=admin_credentials.known_credentials,
    )
    core.state.cmh_reads = reads
    previews = GovernancePreview(
        governance,
        lambda: _sdk_library_bridge(core),
        CredentialFilter(known_credentials_source=admin_credentials.known_credentials),
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
        drain=lambda: core.state.memory_write_drain,
        inflight=InflightGovernance(ingest, generation.admissions, governance.journal),
        execution=governance.execution,
    )
    core.state.cmh_executor = executor
    # Before serving or draining: finish interrupted executions and re-apply
    # completed purges to restored business data (ADR-0032).
    core_lifespan = core.router.lifespan_context

    @asynccontextmanager
    async def governed_lifespan(app):
        async with core_lifespan(app) as state:
            async with AsyncExitStack() as resources:
                mcp_lifespan = remote_mcp.session_manager.run()
                await mcp_lifespan.__aenter__()
                # Close transport resources without injecting Hub startup/body
                # errors into the SDK task group and changing their exception type.
                # Transport cleanup failures still propagate, with the Hub error
                # retained as context; no exception group is flattened or ignored.
                resources.push_async_callback(mcp_lifespan.__aexit__, None, None, None)
                # MCP startup yields to Core's maintenance task. Finish the
                # serialized recovery after all transport startup awaits so
                # serving starts with the same readiness guarantee as before.
                await executor.resume()
                yield state

    core.router.lifespan_context = governed_lifespan
    console = ConsoleRead(
        devices,
        ingest,
        reads,
        CredentialFilter(known_credentials_source=admin_credentials.known_credentials),
    )
    core.state.cmh_console = console
    remote_api = create_remote_api(
        devices,
        ingest,
        reads,
        previews,
        executor,
        admission=sdk_admission,
        public_origin=config.public_origin,
    )
    remote_mcp = RemoteMCP(config, ingest, remote_api)
    remote_api.routes.append(Route(MCP_PATH, remote_mcp))
    remote_api.routes.extend(console_routes(console))
    remote_api.routes.extend(usage_routes(core))
    remote_api.routes.extend(backup_routes(config))
    questions = ConsoleQuestions(core, console)
    core.state.cmh_questions = questions
    remote_api.routes.extend(console_question_routes(questions))
    return CloudBoundary(
        core,
        config,
        internal_token,
        remote_api=remote_api,
        governance=governance,
        admin_credentials=admin_credentials,
    )
