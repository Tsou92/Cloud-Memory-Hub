"""Fail closed at the whole Core/remote HTTP boundary, including streamed output."""

import asyncio

from starlette.responses import JSONResponse

from cloud_memory_hub.governance_journal import GovernanceError


async def guarded_call(
    authority, app, scope, receive, send, *, runtime_only=False, runtime_required=None
):
    """Recheck either structural authority or the lightweight Core fault fence.

    The outer Hub boundary owns database verification. Its nested Core-only
    guard checks the process event without repeating that database work.
    """

    def require_runtime():
        if (
            runtime_only or runtime_required is not None and runtime_required()
        ) and authority.runtime_recovery_required:
            raise GovernanceError("governance_recovery_required")

    async def require_ready():
        if not runtime_only:
            await asyncio.to_thread(authority.require_ready)
        # A nested Core guard may have checked before the outer database await.
        # Recheck the event last, immediately before publishing this response.
        require_runtime()

    if scope["type"] == "lifespan":
        first = await receive()
        if first["type"] == "lifespan.startup":
            try:
                await require_ready()
            except GovernanceError:
                await send(
                    {"type": "lifespan.startup.failed", "message": "memory_authority_unavailable"}
                )
                return

        async def replay_startup():
            nonlocal first
            if first is not None:
                message, first = first, None
                return message
            return await receive()

        await app(scope, replay_startup, send)
        return
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    async def unavailable(error):
        await JSONResponse(
            {
                "detail": "governance_recovery_required"
                if str(error) == "governance_recovery_required"
                else "memory_authority_unavailable"
            },
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

    try:
        await require_ready()
    except GovernanceError as error:
        await unavailable(error)
        return
    started, rejected = False, False
    pending_start = None

    async def reject(error):
        nonlocal rejected
        rejected = True
        if started:
            # Already emitted chunks cannot be recalled. End this response;
            # content-length clients reject a truncated result, SSE stops.
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        else:
            await unavailable(error)

    async def checked_send(message):
        nonlocal started, rejected, pending_start
        if rejected:
            return
        # Hold the headers until the first body chunk so a changed authority
        # during response preparation can still produce a genuine 503.
        if message["type"] == "http.response.start":
            pending_start = message
            return
        try:
            await require_ready()
        except GovernanceError as error:
            await reject(error)
            return
        if pending_start is not None:
            await send(pending_start)
            pending_start = None
            started = True
        # The transport may have yielded while sending headers. Do not emit
        # the first body if recovery failed in that interval.
        try:
            require_runtime()
        except GovernanceError as error:
            await reject(error)
            return
        await send(message)

    await app(scope, receive, checked_send)
