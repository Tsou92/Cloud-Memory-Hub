"""Thin administrator routes using the existing authenticated Hub boundary."""

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Route

from cloud_memory_hub.console_read import ConsolePageRequest
from cloud_memory_hub.read_contract import READ_OPERATIONS, ListRequest, ScopeRequest
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_api import read_body, require_admin, response


def usage_routes(core):
    """Content-free model usage for the administrator console (requirement 9).

    Reuses the Core audit store that already records every precipitation call.
    It reports counts and token totals only — never prompts, responses or
    provider identifiers beyond the stage name.
    """

    async def usage(request: Request):
        require_admin(request)
        if request.method not in {"GET", "HEAD"}:
            raise AccessError("method_not_allowed", 405)
        if request.query_params:
            raise AccessError("invalid_request", 422)
        store = getattr(core.state, "model_request_audit_store", None)
        if store is None:
            raise AccessError("usage_unavailable", 503)
        try:
            report = await run_in_threadpool(store.report)
        except Exception:
            raise AccessError("usage_unavailable", 503) from None
        return response(
            {"schema_version": 1, **report, "billing": "provider_statement_is_authoritative"}
        )

    return [Route("/hub/v1/console/usage", usage, methods=["GET"])]


def console_routes(console):
    def endpoint(action):
        async def authorized(request: Request):
            require_admin(request)
            try:
                return response(await action(request, request.cookies.get("cmh_session", "")))
            except AccessError:
                raise
            except Exception:
                # Upstream and storage exceptions may contain configuration or content.
                raise AccessError("console_unavailable", 503) from None

        return authorized

    async def bootstrap(request, cookie):
        try:
            query = ConsolePageRequest.model_validate(dict(request.query_params))
        except ValidationError:
            raise AccessError("invalid_request", 422) from None
        return await console.bootstrap(query, session_credential=cookie)

    async def summary(request, cookie):
        body = await read_body(request, ListRequest)
        return await console.summary(body, session_credential=cookie)

    async def dashboard(request, cookie):
        body = await read_body(request, ScopeRequest)
        return await console.dashboard(body, session_credential=cookie)

    def reading(operation, model):
        async def read(request, cookie):
            body = await read_body(request, model)
            return await console.read(operation, body, session_credential=cookie)

        return endpoint(read)

    return [
        Route("/hub/v1/console/bootstrap", endpoint(bootstrap), methods=["GET"]),
        Route("/hub/v1/console/summary", endpoint(summary), methods=["POST"]),
        Route("/hub/v1/console/dashboard", endpoint(dashboard), methods=["POST"]),
        *[
            Route(
                "/hub/v1/console/read/" + operation,
                reading(operation, model),
                methods=["POST"],
            )
            for operation, (_, model) in READ_OPERATIONS.items()
            if not operation.startswith("session-")
        ],
    ]
