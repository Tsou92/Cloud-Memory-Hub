"""Authenticate the NAS boundary before entering the unchanged loopback Core.

MemHub's UISessionStore supplies opaque expiring sessions. Its internal bearer
token and local browser cookies never cross this boundary. Device-scoped access
will be a separate capability, not a reuse of the administrator credential.
"""

from __future__ import annotations

import json
import time
from collections import deque
from urllib.parse import parse_qs

from one_memory.ui_session import UISessionStore
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from cloud_memory_hub.admin_credentials import AdminCredentialError, AdminCredentials
from cloud_memory_hub.admin_setup_page import SETUP_FORM, SETUP_SCRIPT, SETUP_SCRIPT_HASH
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_boundary import guarded_call
from cloud_memory_hub.onboarding_api import PUBLIC_CONNECT_PATHS, public_connection_response

STATIC_PATHS = frozenset({"/", "/index.html", "/favicon.ico", "/favicon.svg", "/brand-icon.svg"})
PUBLIC_STATIC_PATHS = frozenset({"/favicon.ico", "/favicon.svg", "/brand-icon.svg"})
ADMIN_CREDENTIAL_PATH = "/hub/v1/admin/credential"
ADMIN_SETUP_PATH = "/hub/setup"
AUTH_BODY_LIMIT = 4096

AUTH_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{{title}} · Cloud Memory Hub</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}
body{font:14px/1.6 Inter,-apple-system,BlinkMacSystemFont,'SF Pro Text','PingFang SC',
'Microsoft YaHei',system-ui,sans-serif;color:#f1f3fb;margin:0;min-height:100svh;
display:grid;place-items:center;padding:32px 16px;background:
radial-gradient(ellipse 68% 38% at 54% -8%,#7c72ff24,transparent 70%),
radial-gradient(ellipse 45% 35% at 15% 100%,#3862b416,transparent 65%),#090b14;
-webkit-font-smoothing:antialiased}
main{width:min(100%,432px);padding:32px 30px;background:#111628;
border:1px solid #94a3da24;border-radius:16px;
box-shadow:0 28px 72px #00000080,inset 0 1px 0 #ffffff0a}
.head{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.glyph{width:38px;height:38px;border-radius:10px;display:block;object-fit:cover}
.titles strong{display:block;font-size:17px;font-weight:650;letter-spacing:-.02em}
.titles span{font-size:12px;color:#737c9c}
h1{font-size:20px;font-weight:650;letter-spacing:-.02em;margin:0 0 8px}
p{line-height:1.65;color:#a9b1cc;margin:0 0 18px;font-size:13.5px}
label{display:block;font-size:12.5px;font-weight:560;color:#a9b1cc;margin:14px 0 6px}
input,button{width:100%;padding:10px 13px;font:inherit;border-radius:8px}
input{background:#0a0d18;border:1px solid #94a3da28;color:#f1f3fb;font-size:14px}
input:focus{border-color:#8b7cf8}
button{background:linear-gradient(135deg,#7467ef,#937ff7);color:#fff;
border:1px solid #cec4ff59;font-weight:560;cursor:pointer;margin-top:18px;
box-shadow:0 2px 12px #7467ef47}
button.secondary{background:#ffffff0c;color:#f1f3fb;border-color:#94a3da28;
box-shadow:none;margin-top:10px}
button:hover:not(:disabled){filter:brightness(1.08)}
input:focus-visible,button:focus-visible{outline:2px solid #9f92ff;outline-offset:2px}
button:disabled{opacity:.48;cursor:not-allowed}
hr{border:0;border-top:1px solid #94a3da18;margin:22px 0 14px}
small{display:block;color:#737c9c;font-size:12px;line-height:1.6}
ol{margin:0 0 16px;padding-left:18px;color:#a9b1cc;font-size:13px}
li{margin-bottom:8px}
code{font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace;font-size:12px;
background:#0a0d18;border:1px solid #94a3da24;padding:2px 6px;border-radius:5px;
color:#dcd5ff;word-break:break-all}
.cmd{display:block;padding:9px 11px;margin-top:6px;line-height:1.5}
a{color:#9f92ff;text-decoration:none}a:hover{text-decoration:underline}
[role=alert]{color:#fecaca;background:#f8717124;border:1px solid #f871714d;
padding:10px 13px;border-radius:8px;margin-bottom:14px;font-size:13px}
@media(max-width:440px){main{padding:24px 20px}}</style>
<main><div class="head"><img class="glyph" src="/brand-icon.svg" alt="">
<div class="titles"><strong>Cloud Memory Hub</strong><span>共享记忆管理中枢</span></div></div>
{{body}}</main>{{script}}</html>"""


def _auth_page(title: str, body: str, script: str = "") -> str:
    return (
        AUTH_PAGE.replace("{{title}}", title)
        .replace("{{body}}", body)
        .replace("{{script}}", script)
    )


LOGIN_PAGE = _auth_page(
    "登录",
    """<h1>登录管理后台</h1>
<p>输入管理员密码，管理共享项目、设备接入、长期记忆与受控备份。</p>
{{error}}
<form method="post" action="/hub/login"><label for="credential">管理员密码</label>
<input id="credential" name="credential" type="password" autocomplete="current-password"
placeholder="输入当前管理员密码" required><button type="submit">进入控制台</button></form>
<hr><small>首次部署由安装器生成的初始化链接设密。遗忘密码时可在宿主运行环境离线重置，
不影响已连接的 Agent 与已有记忆；步骤见文档「重置管理员密码」。</small>""",
)
SETUP_PAGE = _auth_page("设置管理员密码", SETUP_FORM, f"<script>{SETUP_SCRIPT}</script>")
SETUP_REQUIRED_PAGE = _auth_page(
    "初始化",
    """<h1>完成首次密码初始化</h1>
<p>此实例尚未设置正式管理员密码，需通过安装器生成的一次性初始化链接完成。</p>
<ol>
<li>在运行 Hub 的主机安装目录执行：<code class="cmd">docker compose exec hub \
/opt/cmh/venv/bin/python -m cloud_memory_hub setup-link</code></li>
<li>复制命令输出的本机初始化链接并在浏览器中打开。</li>
<li>该链接仅对当前实例有效，命令不会回显或覆盖已有密码。</li>
</ol>
<p><b>本机 Docker：</b>默认目录为 <code>~/.cloud-memory-hub/local-server/&lt;项目名&gt;</code>，
也可重新运行安装命令附带 <code>--local --open-admin</code>（Windows 为
<code>-Local -OpenAdmin</code>）。<b>NAS：</b>默认目录为
<code>~/cloud-memory-hub-install</code>（以安装结束时打印路径为准）。</p>""",
)
SETUP_FINISHED_PAGE = _auth_page(
    "初始化链接已失效",
    (
        "<h1>此初始化链接已失效</h1>"
        '<p>当前实例已完成初始化，请使用管理员密码登录。<a href="/hub/login">返回登录</a></p>'
    ),
)


async def _read_auth_body(request: Request, limit: int = AUTH_BODY_LIMIT) -> bytearray | None:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return None
        body.extend(chunk)
    return body


class CloudBoundary:
    def __init__(
        self,
        core,
        config: HubConfig,
        internal_token: str,
        *,
        remote_api=None,
        governance=None,
        admin_credentials: AdminCredentials | None = None,
    ) -> None:
        self.core = core
        self.config = config
        self.internal_token = internal_token
        self.remote_api = remote_api
        self.governance = governance
        self.admin_credentials = admin_credentials or AdminCredentials(config)
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
        if path == ADMIN_SETUP_PATH:
            response = await self._initial_setup(request)
            script_hash = (
                SETUP_SCRIPT_HASH
                if request.method == "GET" and response.status_code == 200
                else None
            )
            await self._respond(response, scope, receive, send, script_hash=script_hash)
            return
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        bearer = scheme.lower() == "bearer" and self._valid_token(supplied)
        cookie = request.cookies.get("cmh_session", "")
        session = (
            self.admin_credentials.available
            and not self.admin_credentials.setup_pending
            and self.sessions.valid_session(cookie)
        )
        if request.method in {"GET", "HEAD"} and path in PUBLIC_STATIC_PATHS:
            # The login/setup pages need their icon before a session exists.
            await self.core(scope, receive, send)
            return
        if path == ADMIN_CREDENTIAL_PATH:
            response = await self._change_credential(request, authenticated=bearer or session)
            await self._respond(response, scope, receive, send)
            return
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
        return self.admin_credentials.valid(value)

    def _login_limited(self) -> bool:
        now = time.monotonic()
        while self.failed_logins and self.failed_logins[0] < now - 60:
            self.failed_logins.popleft()
        return len(self.failed_logins) >= 20

    async def _change_credential(self, request: Request, *, authenticated: bool):
        if request.method != "POST":
            return JSONResponse({"detail": "method_not_allowed"}, status_code=405)
        if request.headers.get("origin") != self.config.public_origin:
            return JSONResponse({"detail": "origin_required"}, status_code=403)
        if not authenticated:
            return JSONResponse({"detail": "authentication_required"}, status_code=401)
        if self._login_limited():
            return JSONResponse({"detail": "login_rate_limited"}, status_code=429)
        body = await _read_auth_body(request)
        if body is None:
            return JSONResponse({"detail": "request_too_large"}, status_code=413)
        if self._login_limited():
            return JSONResponse({"detail": "login_rate_limited"}, status_code=429)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            value = None
        if not isinstance(value, dict) or set(value) != {"current_credential", "new_credential"}:
            return JSONResponse({"detail": "invalid_request"}, status_code=400)
        try:
            self.admin_credentials.change(value["current_credential"], value["new_credential"])
        except AdminCredentialError as error:
            if error.code == "current_credential_incorrect":
                self.failed_logins.append(time.monotonic())
            response = JSONResponse({"detail": error.code}, status_code=error.status)
            if not self.admin_credentials.available:
                self.sessions = UISessionStore(instance_id=self.internal_token)
                response.delete_cookie("cmh_session", path="/")
            return response
        self.sessions = UISessionStore(instance_id=self.internal_token)
        response = JSONResponse(
            {"schema_version": 1, "state": "credential_changed", "login_required": True}
        )
        response.delete_cookie("cmh_session", path="/")
        return response

    async def _login(self, request: Request):
        if request.method == "GET":
            return HTMLResponse(
                SETUP_REQUIRED_PAGE
                if self.admin_credentials.setup_pending
                else LOGIN_PAGE.replace("{{error}}", "")
            )
        if request.method != "POST":
            return JSONResponse({"detail": "method_not_allowed"}, status_code=405)
        if request.headers.get("origin") != self.config.public_origin:
            return JSONResponse({"detail": "origin_required"}, status_code=403)
        if self.admin_credentials.setup_pending:
            return self._login_error(request, "initial_setup_required", 409)
        if self._login_limited():
            return self._login_error(request, "login_rate_limited", 429)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        # Native form submission can percent-encode each byte into three bytes.
        limit = (
            AUTH_BODY_LIMIT * 3
            if content_type == "application/x-www-form-urlencoded"
            else AUTH_BODY_LIMIT
        )
        body = await _read_auth_body(request, limit)
        if body is None:
            return self._login_error(request, "request_too_large", 413)
        if self._login_limited():
            return self._login_error(request, "login_rate_limited", 429)
        try:
            if content_type == "application/json":
                value = json.loads(body).get("credential", "")
            else:
                value = parse_qs(body.decode(), max_num_fields=4).get("credential", [""])[0]
        except (ValueError, UnicodeError, AttributeError):
            value = ""
        if not self.admin_credentials.valid_password(value):
            self.failed_logins.append(time.monotonic())
            return self._login_error(request, "invalid_credential", 401)
        return self._new_session(RedirectResponse("/", status_code=303))

    async def _initial_setup(self, request: Request):
        if request.method == "GET":
            if self.admin_credentials.setup_pending:
                return HTMLResponse(SETUP_PAGE)
            return HTMLResponse(SETUP_FINISHED_PAGE, status_code=409)
        if request.method != "POST":
            return JSONResponse({"detail": "method_not_allowed"}, status_code=405)
        if request.headers.get("origin") != self.config.public_origin:
            return JSONResponse({"detail": "origin_required"}, status_code=403)
        if not self.admin_credentials.setup_pending:
            return JSONResponse({"detail": "initial_setup_not_pending"}, status_code=409)
        if self._login_limited():
            return JSONResponse({"detail": "login_rate_limited"}, status_code=429)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse({"detail": "invalid_request"}, status_code=400)
        body = await _read_auth_body(request)
        if body is None:
            return JSONResponse({"detail": "request_too_large"}, status_code=413)
        if self._login_limited():
            return JSONResponse({"detail": "login_rate_limited"}, status_code=429)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            value = None
        if (
            not isinstance(value, dict)
            or set(value) != {"password", "proof", "new_credential", "confirm_credential"}
            or not all(isinstance(item, str) for item in value.values())
        ):
            return JSONResponse({"detail": "invalid_request"}, status_code=400)
        if value["new_credential"] != value["confirm_credential"]:
            return JSONResponse({"detail": "invalid_new_credential"}, status_code=422)
        try:
            self.admin_credentials.complete_initial_setup(
                value["password"], value["proof"], value["new_credential"]
            )
        except AdminCredentialError as error:
            if error.code == "invalid_initial_credential":
                self.failed_logins.append(time.monotonic())
            return JSONResponse({"detail": error.code}, status_code=error.status)
        self.sessions = UISessionStore(instance_id=self.internal_token)
        return self._new_session(JSONResponse({"state": "initial_setup_completed"}))

    def _new_session(self, response):
        code, _ = self.sessions.issue_exchange()
        session_id = self.sessions.consume_exchange(code)
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
        if detail == "initial_setup_required":
            return HTMLResponse(SETUP_REQUIRED_PAGE, status_code=status)
        messages = {
            "invalid_credential": "凭证不正确，请重新输入。",
            "login_rate_limited": "尝试次数较多，请稍后再试。",
            "request_too_large": "输入内容过长，请重新输入管理员密码。",
        }
        # Only constant, trusted messages enter HTML; never echo submitted credentials.
        message = '<p role="alert">' + messages[detail] + "</p>"
        return HTMLResponse(LOGIN_PAGE.replace("{{error}}", message), status_code=status)

    @staticmethod
    async def _respond(response, scope, receive, send, *, script_hash=None) -> None:
        response.headers["Cache-Control"] = "no-store"
        # same-origin, not no-referrer: Chrome sends `Origin: null` for form posts from a
        # no-referrer page, which the exact Origin check rejects. Same-origin still sends
        # nothing to other sites.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        if script_hash is not None:
            response.headers["Content-Security-Policy"] += (
                f"; script-src 'sha256-{script_hash}'; connect-src 'self'"
            )
        await response(scope, receive, send)
