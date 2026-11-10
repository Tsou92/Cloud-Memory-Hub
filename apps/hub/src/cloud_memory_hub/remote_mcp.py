"""ADR-0065: request-bound MCP over the existing authorized Hub HTTP operations."""

import httpx
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from cloud_memory_hub.agent_access import AgentAccessError, AgentOperations
from cloud_memory_hub.agent_mcp import create_server
from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.privacy import CredentialFilter
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_api import device_credential, response
from cloud_memory_hub.transport import (
    RemoteConnection,
    TransportError,
    _request,
    verify_connection,
)

MCP_PATH = "/hub/v1/mcp/{source_id:uuid}"
_SERVICE_STATE = "cmh_remote_agent"


class RemoteAgentOperations(AgentOperations):
    """No persistent client state: a write succeeds only after the existing Hub receipt."""

    def __init__(self, connection, binding, remote_api):
        self.connection = connection
        self.scope_id = binding["scope_id"]
        self.source_id = binding["source_id"]
        self.privacy = CredentialFilter((connection.credential.get_secret_value(),))
        # Keep the same REST admission, revocation and read checks without an
        # extra network listener or the privileges of the administrator boundary.
        self.transport = httpx.ASGITransport(app=remote_api)

    async def _save(self, content, *, request_id, session_id):
        event = self.prepare_event(content, request_id=request_id, session_id=session_id)
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False
            ) as client:
                receipt = await _request(
                    client,
                    self.connection,
                    "/hub/v1/events",
                    payload=event.canonical_payload(),
                    expected=202,
                )
        except TransportError as error:
            raise AgentAccessError(error.code, retryable=error.retryable) from None
        try:
            if (
                type(receipt.get("protocol_version")) is not int
                or receipt["protocol_version"] != 1
                or receipt.get("status") != "accepted"
                or receipt.get("instance_id") != self.connection.instance_id
                or receipt.get("device_id") != self.connection.device_id
                or receipt.get("event_id") != event.event_id
                or receipt.get("payload_digest") != event.payload_digest()
            ):
                raise ValueError
            canonical_uuid(receipt.get("receipt_id"))
        except (ValueError, TypeError, AttributeError):
            raise AgentAccessError("remote_receipt_mismatch") from None
        # accepted is the durable ingress receipt, not a promise that extraction
        # finished. Unlike stdio there is no local queued state to acknowledge.
        return {**receipt, "processing_state": "not_checked", "client_offline_queue": False}

    async def status(self, *, sync=False):
        if type(sync) is not bool:
            raise AgentAccessError("status_request_invalid")
        try:
            await verify_connection(
                self.connection,
                scope_id=self.scope_id,
                source_id=self.source_id,
                transport=self.transport,
            )
        except TransportError as error:
            raise AgentAccessError(error.code, retryable=error.retryable) from None
        return {
            "status": "connected",
            "transport": "streamable_http",
            "instance_id": self.connection.instance_id,
            "device_id": self.connection.device_id,
            "scope_id": self.scope_id,
            "source_id": self.source_id,
            "client_offline_queue": False,
            "local_history_upload": False,
            "delivery": "not_applicable",
            "processing_state": "not_checked",
        }


def _request_service(context):
    request = context.request_context.request
    service = request.scope.get("state", {}).get(_SERVICE_STATE)
    if not isinstance(service, RemoteAgentOperations):
        raise AgentAccessError("agent_binding_invalid")
    return service


class RemoteMCP:
    """ASGI route; its manager is started once by the owning Hub lifespan."""

    def __init__(self, config, ingest, remote_api):
        self.config = config
        self.ingest = ingest
        self.remote_api = remote_api
        self.server = create_server(service_factory=_request_service, remote=True)
        self.app = self.server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[config.authority],
                allowed_origins=[config.public_origin],
            ),
        )

    @property
    def session_manager(self):
        return self.server.session_manager

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        try:
            credential = device_credential(request)
            source_id = canonical_uuid(str(request.path_params.get("source_id", "")))
            binding = await run_in_threadpool(self.ingest.source_access, credential, source_id)
            connection = RemoteConnection(
                origin=self.config.public_origin,
                instance_id=binding["instance_id"],
                device_id=binding["device_id"],
                credential=credential,
            )
            service = RemoteAgentOperations(connection, binding, self.remote_api)
        except AccessError as error:
            await response({"detail": error.code}, error.status)(scope, receive, send)
            return
        except (ValueError, TypeError, AttributeError):
            await response({"detail": "agent_binding_invalid"}, 422)(scope, receive, send)
            return
        state = {**scope.get("state", {}), _SERVICE_STATE: service}

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"cache-control", b"x-content-type-options"}
                ]
                message = {
                    **message,
                    "headers": [
                        *headers,
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            await send(message)

        await self.app({**scope, "state": state}, receive, private_send)
