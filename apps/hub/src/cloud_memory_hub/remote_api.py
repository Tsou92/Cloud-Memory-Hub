"""Versioned device control plane; never proxy a device into the Core admin API."""

from contextlib import nullcontext

from one_memory.hy_memory_bridge import HyMemoryBridgeError, _joined_thread
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_execution import ExecuteRequest
from cloud_memory_hub.governance_plan import PreviewRequest
from cloud_memory_hub.governance_preview import GovernancePreview
from cloud_memory_hub.privacy import MAX_CONTENT_BYTES, PrivacyError, parse_json
from cloud_memory_hub.read_contract import READ_OPERATIONS
from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    IssuedCredential,
    NamedResource,
    RevisionRequest,
)
from cloud_memory_hub.remote_ingest import RemoteIngest, SourceRegistration
from cloud_memory_hub.remote_pairing import (
    PairingClaim,
    PairingRequest,
    PairingRevoke,
    PairingStore,
)
from cloud_memory_hub.remote_read import RemoteRead

MAX_BODY_BYTES = 64 * 1024


def response(value, status: int = 200) -> JSONResponse:
    return JSONResponse(
        value,
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def require_admin(request: Request) -> None:
    if not request.scope.get("state", {}).get("cmh_admin", False):
        raise AccessError("administrator_required", 403)


async def read_body(request: Request, model, *, max_bytes=MAX_BODY_BYTES):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise AccessError("json_required", 422)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise AccessError("request_too_large", 413)
        body.extend(chunk)
    try:
        return model.model_validate(parse_json(body))
    except (ValidationError, ValueError, PrivacyError):
        # Pydantic's normal errors contain input values. Never send them back.
        raise AccessError("invalid_request", 422) from None


def issued_response(issued: IssuedCredential, status: int = 200) -> JSONResponse:
    return response(
        {"device": issued.device, "credential": issued.credential.get_secret_value()}, status
    )


def device_credential(request: Request) -> str:
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        raise AccessError("invalid_device_credential", 401)
    return credential


def create_remote_api(
    store: DeviceAccessStore,
    ingest: RemoteIngest,
    reads: RemoteRead,
    previews: GovernancePreview,
    executor=None,
    *,
    admission=None,
    public_origin=None,
) -> Starlette:
    pairing_store = PairingStore(ingest, public_origin) if public_origin is not None else None

    def configured_pairings():
        if pairing_store is None:
            raise AccessError("pairing_unavailable", 503)
        return pairing_store

    async def create_pairing(request: Request):
        require_admin(request)
        pairings = configured_pairings()
        body = await read_body(request, PairingRequest)
        issued = await run_in_threadpool(pairings.create, body)
        return response(
            {**issued.pairing, "invitation_secret": issued.invitation_secret.get_secret_value()},
            201,
        )

    async def pairing_status(request: Request):
        require_admin(request)
        return response(
            await run_in_threadpool(
                configured_pairings().get, str(request.path_params["pairing_id"])
            )
        )

    async def revoke_pairing(request: Request):
        require_admin(request)
        pairings = configured_pairings()
        await read_body(request, PairingRevoke)
        return response(
            await run_in_threadpool(pairings.revoke, str(request.path_params["pairing_id"]))
        )

    async def claim_pairing(request: Request):
        pairings = configured_pairings()
        body = await read_body(request, PairingClaim)
        return response(await run_in_threadpool(pairings.claim, body))

    async def projects(request: Request):
        require_admin(request)
        if request.method in {"GET", "HEAD"}:
            return response({"projects": await run_in_threadpool(store.list_projects)})
        body = await read_body(request, NamedResource)
        return response(await run_in_threadpool(store.create_project, body), 201)

    async def devices(request: Request):
        require_admin(request)
        if request.method in {"GET", "HEAD"}:
            return response({"devices": await run_in_threadpool(store.list_devices)})
        body = await read_body(request, NamedResource)
        return issued_response(await run_in_threadpool(store.create_device, body), 201)

    async def grants(request: Request):
        require_admin(request)
        body = await read_body(request, GrantRequest)
        return response(
            await run_in_threadpool(
                store.replace_grants, str(request.path_params["device_id"]), body
            )
        )

    async def rotate(request: Request):
        require_admin(request)
        body = await read_body(request, RevisionRequest)
        return issued_response(
            await run_in_threadpool(store.rotate, str(request.path_params["device_id"]), body)
        )

    async def revoke(request: Request):
        require_admin(request)
        body = await read_body(request, RevisionRequest)
        return response(
            await run_in_threadpool(store.revoke, str(request.path_params["device_id"]), body)
        )

    async def session(request: Request):
        device = await run_in_threadpool(store.authenticate, device_credential(request))
        return response(
            {
                "protocol_version": 1,
                "instance_id": store.instance_id,
                "device": device,
                "features": [
                    "device_identity",
                    "source_registration",
                    "event_upload",
                    "source_preflight",
                    "scoped_reads",
                ],
            }
        )

    async def sources(request: Request):
        require_admin(request)
        if request.method in {"GET", "HEAD"}:
            return response({"sources": await run_in_threadpool(ingest.list_sources)})
        body = await read_body(request, SourceRegistration)
        return response(await run_in_threadpool(ingest.create_source, body), 201)

    async def revoke_source(request: Request):
        require_admin(request)
        body = await read_body(request, RevisionRequest)
        return response(
            await run_in_threadpool(
                ingest.revoke_source, str(request.path_params["source_id"]), body
            )
        )

    async def source_access(request: Request):
        return response(
            await run_in_threadpool(
                ingest.source_access,
                device_credential(request),
                str(request.path_params["source_id"]),
            )
        )

    async def events(request: Request):
        credential = device_credential(request)
        await run_in_threadpool(store.authenticate, credential)
        event = await read_body(request, EventEnvelope, max_bytes=MAX_CONTENT_BYTES + 4096)
        try:
            async with admission.operation() if admission is not None else nullcontext():
                return response(await _joined_thread(ingest.accept, credential, event), 202)
        except HyMemoryBridgeError as error:
            if str(error) == "hy_memory_maintenance_in_progress":
                raise AccessError("governance_maintenance_in_progress", 503) from None
            raise

    async def event_status(request: Request):
        return response(
            await run_in_threadpool(
                ingest.event_status,
                device_credential(request),
                str(request.path_params["event_id"]),
            )
        )

    async def access_error(request: Request, error: AccessError):
        return response({"detail": error.code}, error.status)

    async def governance_preview(request: Request):
        require_admin(request)
        body = await read_body(request, PreviewRequest)
        return response(await previews.preview(body))

    async def governance_execute(request: Request):
        require_admin(request)
        if executor is None:
            raise AccessError("not_found", 404)
        body = await read_body(request, ExecuteRequest)
        return response(await executor.run(body))

    def read_endpoint(operation, model):
        async def read(request: Request):
            credential = device_credential(request)
            await run_in_threadpool(store.authenticate, credential)
            body = await read_body(request, model)
            return response(await reads.read(credential, operation, body))

        return read

    async def http_error(request: Request, error: HTTPException):
        return response(
            {"detail": "not_found" if error.status_code == 404 else "method_not_allowed"},
            error.status_code,
        )

    return Starlette(
        routes=[
            Route("/hub/v1/pairings", create_pairing, methods=["POST"]),
            Route("/hub/v1/pairings/claim", claim_pairing, methods=["POST"]),
            Route("/hub/v1/pairings/{pairing_id:uuid}", pairing_status, methods=["GET"]),
            Route("/hub/v1/pairings/{pairing_id:uuid}/revoke", revoke_pairing, methods=["POST"]),
            Route("/hub/v1/governance/preview", governance_preview, methods=["POST"]),
            Route("/hub/v1/governance/execute", governance_execute, methods=["POST"]),
            Route("/hub/v1/projects", projects, methods=["GET", "POST"]),
            Route("/hub/v1/devices", devices, methods=["GET", "POST"]),
            Route("/hub/v1/devices/{device_id:uuid}/grants", grants, methods=["PUT"]),
            Route("/hub/v1/devices/{device_id:uuid}/rotate", rotate, methods=["POST"]),
            Route("/hub/v1/devices/{device_id:uuid}/revoke", revoke, methods=["POST"]),
            Route("/hub/v1/session", session, methods=["GET"]),
            Route("/hub/v1/sources", sources, methods=["GET", "POST"]),
            Route("/hub/v1/sources/{source_id:uuid}/revoke", revoke_source, methods=["POST"]),
            Route("/hub/v1/sources/{source_id:uuid}/access", source_access, methods=["GET"]),
            Route("/hub/v1/events", events, methods=["POST"]),
            Route("/hub/v1/events/{event_id:uuid}", event_status, methods=["GET"]),
            *[
                Route(path, read_endpoint(operation, model), methods=["POST"])
                for operation, (path, model) in READ_OPERATIONS.items()
            ],
        ],
        exception_handlers={AccessError: access_error, HTTPException: http_error},
    )
