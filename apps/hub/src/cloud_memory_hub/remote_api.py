"""Versioned device control plane; never proxy a device into the Core admin API."""

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cloud_memory_hub.remote_access import (
    AccessError,
    DeviceAccessStore,
    GrantRequest,
    IssuedCredential,
    NamedResource,
    RevisionRequest,
)

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


async def read_body(request: Request, model):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise AccessError("json_required", 422)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise AccessError("request_too_large", 413)
        body.extend(chunk)
    try:
        return model.model_validate_json(body)
    except (ValidationError, ValueError):
        # Pydantic's normal errors contain input values. Never send them back.
        raise AccessError("invalid_request", 422) from None


def issued_response(issued: IssuedCredential, status: int = 200) -> JSONResponse:
    return response(
        {"device": issued.device, "credential": issued.credential.get_secret_value()}, status
    )


def create_remote_api(store: DeviceAccessStore) -> Starlette:
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
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            raise AccessError("invalid_device_credential", 401)
        device = await run_in_threadpool(store.authenticate, credential)
        return response(
            {
                "protocol_version": 1,
                "instance_id": store.instance_id,
                "device": device,
                "features": ["device_identity"],
            }
        )

    async def access_error(request: Request, error: AccessError):
        return response({"detail": error.code}, error.status)

    async def http_error(request: Request, error: HTTPException):
        return response(
            {"detail": "not_found" if error.status_code == 404 else "method_not_allowed"},
            error.status_code,
        )

    return Starlette(
        routes=[
            Route("/hub/v1/projects", projects, methods=["GET", "POST"]),
            Route("/hub/v1/devices", devices, methods=["GET", "POST"]),
            Route("/hub/v1/devices/{device_id:uuid}/grants", grants, methods=["PUT"]),
            Route("/hub/v1/devices/{device_id:uuid}/rotate", rotate, methods=["POST"]),
            Route("/hub/v1/devices/{device_id:uuid}/revoke", revoke, methods=["POST"]),
            Route("/hub/v1/session", session, methods=["GET"]),
        ],
        exception_handlers={AccessError: access_error, HTTPException: http_error},
    )
