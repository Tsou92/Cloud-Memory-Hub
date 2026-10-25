"""Fail closed at the whole Core/remote HTTP boundary, including streamed output."""

import asyncio

from starlette.responses import JSONResponse

from cloud_memory_hub.governance_journal import GovernanceError


async def guarded_call(authority, app, scope, receive, send):
    if scope["type"] == "lifespan":
        first = await receive()
        if first["type"] == "lifespan.startup":
            try:
                await asyncio.to_thread(authority.require_ready)
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

    async def unavailable():
        await JSONResponse(
            {"detail": "memory_authority_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

    try:
        await asyncio.to_thread(authority.require_ready)
    except GovernanceError:
        await unavailable()
        return
    started, rejected = False, False
    pending_start = None

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
            await asyncio.to_thread(authority.require_ready)
        except GovernanceError:
            rejected = True
            if started:
                # Already emitted chunks cannot be recalled. End this response;
                # content-length clients reject a truncated result, SSE stops.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await unavailable()
            return
        if pending_start is not None:
            await send(pending_start)
            pending_start = None
            started = True
        await send(message)

    await app(scope, receive, checked_send)
