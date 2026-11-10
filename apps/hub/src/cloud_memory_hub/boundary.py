"""Authenticate the NAS boundary before entering the unchanged loopback Core.

MemHub's UISessionStore supplies opaque expiring sessions. Its internal bearer
token and local browser cookies never cross this boundary. Device-scoped access
will be a separate capability, not a reuse of the administrator credential.
"""

from __future__ import annotations

import hmac
import json
import time
from collections import deque
from urllib.parse import parse_qs

from one_memory.ui_session import UISessionStore
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_boundary import guarded_call
from cloud_memory_hub.onboarding_api import PUBLIC_CONNECT_PATHS, public_connection_response

STATIC_PATHS = frozenset({"/", "/index.html", "/favicon.ico", "/favicon.svg"})

LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 Cloud Memory Hub</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}
body{font:15px system-ui,-apple-system,'PingFang SC',sans-serif;color:#f3f4fc;margin:0;
min-height:100svh;display:grid;place-items:center;padding:32px 16px;background:
radial-gradient(ellipse at 62% 30%,#3b326444,transparent 54%),
radial-gradient(ellipse at 23% 78%,#23345655,transparent 50%),#0c1020}
main{width:min(100%,420px);padding:38px 34px;background:linear-gradient(145deg,#222840e8,#161a2de8);
border:1px solid #a0a6dc30;border-radius:22px;box-shadow:0 30px 100px #03061088}
.brand{display:block;color:#c5b1ff;font-size:60px;line-height:1;margin-bottom:20px;
text-shadow:0 0 30px #9880ff66}h1{font-size:25px;letter-spacing:-.025em;margin:0 0 12px}
input,button{width:100%;padding:13px 15px;margin-top:12px;font:inherit;border-radius:10px}
input{background:#0e1426;border:1px solid #a0a6dc44;color:#f3f4fc}
button{background:linear-gradient(110deg,#7775dc,#a18be6);color:white;border:1px solid #c7baff66;
font-weight:600;cursor:pointer;margin-top:22px;box-shadow:0 4px 22px #8670df33}
button:hover{filter:brightness(1.1)}input:focus-visible,button:focus-visible
{outline:2px solid #c5afff;outline-offset:3px}p{line-height:1.75;color:#b4b8d0;margin-bottom:26px}
label{font-weight:600}[role=alert]{color:#ffb0bd;background:#f28f9f12;border:1px solid #f28f9f40;
padding:12px;border-radius:10px}@media(max-width:420px){main{padding:30px 24px}}</style>
<main><span class="brand" aria-hidden="true">∞</span><h1>Cloud Memory Hub</h1>
<p>欢迎回来。输入初始化时生成的管理员凭证，进入你的共享记忆中枢。</p>
{{error}}
<form method="post" action="/hub/login"><label for="credential">管理员凭证</label>
<input id="credential" name="credential" type="password" autocomplete="current-password"
required maxlength="256"><button>登录</button></form></main></html>"""


class CloudBoundary:
    def __init__(
        self, core, config: HubConfig, internal_token: str, *, remote_api=None, governance=None
    ) -> None:
        self.core = core
        self.config = config
        self.internal_token = internal_token
        self.remote_api = remote_api
        self.governance = governance
        self.sessions = UISessionStore(instance_id=internal_token)
        self.failed_logins: deque[float] = deque(maxlen=20)

    async def __call__(self, scope, receive, send) -> None:
        if self.governance is not None:
            core_response = {"runtime_required": False}

            async def handle(scope, receive, send):
                await self._handle(scope, receive, send, core_response=core_response)

            await guarded_call(
                self.governance,
                handle,
                scope,
                receive,
                send,
                runtime_required=lambda: core_response["runtime_required"],
            )
        else:
            await self._handle(scope, receive, send)

    async def _handle(self, scope, receive, send, *, core_response=None) -> None:
        if scope["type"] == "lifespan":
            await self.core(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        path = scope["path"]
        if path == "/healthz" and request.method == "GET":
            # No library, version, token or personal metadata on the public probe.
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        origin = request.headers.get("origin")
        if (
            request.headers.get("host", "").lower() != self.config.authority
            or origin is not None
            and origin != self.config.public_origin
        ):
            await JSONResponse({"detail": "origin_not_allowed"}, status_code=403)(
                scope, receive, send
            )
            return
        if path in PUBLIC_CONNECT_PATHS:
            await public_connection_response(path, request.method, self.config.public_origin)(
                scope, receive, send
            )
            return
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        bearer = scheme.lower() == "bearer" and self._valid_token(supplied)
        cookie = request.cookies.get("cmh_session", "")
        session = self.sessions.valid_session(cookie)
        if path == "/hub/v1" or path.startswith("/hub/v1/"):
            if session and not bearer and request.method not in {"GET", "HEAD"}:
                if origin != self.config.public_origin:
                    await self._respond(
                        JSONResponse({"detail": "origin_required"}, status_code=403),
                        scope,
                        receive,
                        send,
                    )
                    return
            if self.remote_api is None:
                await JSONResponse({"detail": "not_found"}, status_code=404)(scope, receive, send)
            else:
                # Trusted ASGI state, never an external header or a Core grant.
                state = {**scope.get("state", {}), "cmh_admin": bearer or session}
                await self.remote_api({**scope, "state": state}, receive, send)
            return
        if path == "/hub/login":
            response = await self._login(request)
            await self._respond(response, scope, receive, send)
            return
        if not bearer and not session:
            response = (
                JSONResponse({"detail": "authentication_required"}, status_code=401)
                if path.startswith(("/api/", "/hub/"))
                else RedirectResponse("/hub/login", status_code=303)
            )
            await self._respond(response, scope, receive, send)
            return
        if (
            not bearer
            and request.method not in {"GET", "HEAD"}
            and origin != self.config.public_origin
        ):
            await JSONResponse({"detail": "origin_required"}, status_code=403)(scope, receive, send)
            return
        if path == "/api/v1/ui/session/logout" and request.method == "POST":
            self.sessions.revoke_session(cookie)
            response = JSONResponse({"schema_version": 1, "state": "logged_out"})
            response.delete_cookie("cmh_session", path="/")
            await self._respond(response, scope, receive, send)
            return
        if path.startswith(("/internal/", "/ui/", "/api/v1/ui/session")):
            await JSONResponse({"detail": "not_found"}, status_code=404)(scope, receive, send)
            return
        if (
            self.governance is not None
            and request.method == "DELETE"
            and path.startswith("/api/v2/memories/")
        ):
            # Upstream delete reactivates older versions and retains history.
            # Never present that operation as the Hub's durable purge workflow.
            await self._respond(
                JSONResponse({"detail": "governance_workflow_required"}, status_code=409),
                scope,
                receive,
                send,
            )
            return
        # Only after authorization: remove all external identity and browser
        # credentials. Never forward an attacker-supplied installation grant.
        headers = [
            (key, value)
            for key, value in scope["headers"]
            if key.lower() in {b"accept", b"content-type", b"content-length"}
        ]
        headers.extend(
            [
                (b"authorization", f"Bearer {self.internal_token}".encode()),
                (b"host", b"127.0.0.1"),
            ]
        )
        forwarded = {**scope, "headers": headers}
        static = request.method in {"GET", "HEAD"} and (
            path in STATIC_PATHS or path.startswith("/assets/")
        )
        if self.governance is not None and not static:
            core_response["runtime_required"] = True
            # Core's native reads do not apply Hub suppression. Keep its body
            # behind the runtime fence, including a fault after request entry.
            # Login, static assets and the authorized Hub maintenance/read API
            # retain their existing paths and the outer structural guard.
            await guarded_call(
                self.governance, self.core, forwarded, receive, send, runtime_only=True
            )
        else:
            await self.core(forwarded, receive, send)

    def _valid_token(self, value: str) -> bool:
        return value.isascii() and hmac.compare_digest(value, self.config.admin_token)

    async def _login(self, request: Request):
        if request.method == "GET":
            return HTMLResponse(LOGIN_PAGE.replace("{{error}}", ""))
        if request.method != "POST":
            return JSONResponse({"detail": "method_not_allowed"}, status_code=405)
        if request.headers.get("origin") != self.config.public_origin:
            return JSONResponse({"detail": "origin_required"}, status_code=403)
        now = time.monotonic()
        while self.failed_logins and self.failed_logins[0] < now - 60:
            self.failed_logins.popleft()
        if len(self.failed_logins) >= 20:
            return self._login_error(request, "login_rate_limited", 429)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return self._login_error(request, "request_too_large", 413)
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                value = json.loads(body).get("credential", "")
            else:
                value = parse_qs(body.decode(), max_num_fields=4).get("credential", [""])[0]
        except (ValueError, UnicodeError, AttributeError):
            value = ""
        if not isinstance(value, str) or not self._valid_token(value):
            self.failed_logins.append(now)
            return self._login_error(request, "invalid_credential", 401)
        code, _ = self.sessions.issue_exchange()
        session_id = self.sessions.consume_exchange(code)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "cmh_session",
            session_id,
            httponly=True,
            secure=self.config.secure_cookie,
            samesite="strict",
            max_age=12 * 3600,
            path="/",
        )
        return response

    @staticmethod
    def _login_error(request: Request, detail: str, status: int):
        if request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": detail}, status_code=status)
        messages = {
            "invalid_credential": "凭证不正确，请重新输入。",
            "login_rate_limited": "尝试次数较多，请稍后再试。",
            "request_too_large": "输入内容过长，请重新输入管理员凭证。",
        }
        # Only constant, trusted messages enter HTML; never echo submitted credentials.
        message = '<p role="alert">' + messages[detail] + "</p>"
        return HTMLResponse(LOGIN_PAGE.replace("{{error}}", message), status_code=status)

    @staticmethod
    async def _respond(response, scope, receive, send) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        await response(scope, receive, send)
