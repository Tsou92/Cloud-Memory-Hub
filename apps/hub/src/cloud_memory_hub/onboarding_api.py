"""Public installation instructions; invitations are consumed only by claim."""

import base64
import hashlib
import html
import json
import shlex
from importlib.resources import files

from starlette.responses import HTMLResponse, Response

PUBLIC_CONNECT_PATHS = frozenset(
    {"/hub/connect", "/hub/connect.py", "/hub/install.sh", "/hub/install.ps1"}
)

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>连接 Agent · Cloud Memory Hub</title><style>__STYLE__</style>
<main><h1>把你的 Agent 连接到 Cloud Memory Hub</h1>
<p>记忆保存在 NAS。这里只配置当前设备的 MCP 连接和使用说明，无须安装独立 App。</p>
<p>请确保这台设备能访问你的 NAS，例如已加入同一个 Tailscale 网络。</p>
<p id="notice" role="status">正在检查连接链接……</p>
<label for="agent">你要使用的 Agent</label>
<select id="agent"><option value="claude">Claude Code</option><option value="codex">Codex</option>
<option value="opencode">OpenCode</option><option value="hermes">Hermes</option>
<option value="generic">其他支持 HTTP MCP 的 Agent</option></select>
<h2>方法一：交给 Agent 安装</h2>
<p>把下面这句话复制到选定的 Agent。它会运行同一个连接器，不需要你编辑配置。</p>
<textarea id="prompt" rows="5" readonly></textarea>
<button id="copy-prompt">复制安装话术</button>
<h2>方法二：运行一条命令</h2>
<label for="platform">运行命令的位置</label>
<select id="platform"><option value="unix">Mac / Linux 终端</option>
<option value="windows">Windows PowerShell</option></select>
<p>脚本会先检查连接、备份相关配置，再添加 MCP 和 Skill。缺少 Python 时会临时下载运行环境。</p>
<textarea id="command" rows="5" readonly></textarea>
<button id="copy-command">复制命令</button>
<p>配置完成后，按提示让 Agent 重新加载 MCP；当前已打开的会话可能需要重新连接。</p>
<h2>确认可以使用</h2><ol><li>让 Agent 保存一条测试记忆。</li>
<li>在另一设备连接同一项目，再让它读取这条记忆。</li></ol>
<p>仅完成配对或显示 MCP 已连接，还不能代替这两步读写检查。</p>
<details><summary>Agent 安装说明与高级入口</summary>
<p>只使用当前 Hub 提供的同源安装脚本。
Mac/Linux 调用 /hub/install.sh，Windows 调用 /hub/install.ps1。
脚本接受 --link 与 --agent（PowerShell 为 -Link 与 -Agent）。不要手工拼接或输出长期设备凭证。
有 Python 3.11 以上时，可下载同源 /hub/connect.py，以隔离模式 python -I 执行同样参数。
保留其他 MCP 连接；安装失败时重试原链接以恢复进度。没有合适适配器时选择 generic 生成私有配置。</p>
<p>远程 MCP 提供共享记忆和交接工具；本地历史采集及离线补传使用高级连接器，需单独启用。</p>
</details><p><a href="/">返回 NAS 管理页面</a></p></main>
<script>__SCRIPT__</script></html>"""

STYLE = (
    "body{font-family:system-ui;line-height:1.6;margin:0;padding:20px}"
    "main{max-width:760px;margin:auto;overflow-wrap:anywhere}"
    "textarea{width:100%;box-sizing:border-box;font:inherit}"
    "button,select{font:inherit;padding:6px;max-width:100%}"
)

SCRIPT = r"""'use strict';
const origin = __ORIGIN__;
const fragment = new URLSearchParams(location.hash.slice(1));
const id = fragment.get('pairing_id');
const secret = fragment.get('secret');
const valid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id || '')
  && /^[A-Za-z0-9_-]{43}$/.test(secret || '') && [...fragment.keys()].length === 2;
const link = valid ? origin + '/hub/connect#pairing_id=' + id + '&secret=' + secret : '';
// Keep the invitation in this page's memory only, never in browser storage.
history.replaceState(null, '', location.pathname);
const byId = (id) => document.getElementById(id);
const shellQuote = (s) => "'" + s.replaceAll("'", "'\\''") + "'";
const psQuote = (s) => "'" + s.replaceAll("'", "''") + "'";
byId('notice').textContent = valid
  ? '这是一条限时、一次性配对链接。请只在要连接的设备使用；配对结果以脚本检查为准。'
  : '链接不完整。请在 NAS 管理页的连接入口生成新的链接。';
if (navigator.userAgent.includes('Windows')) byId('platform').value = 'windows';
function render() {
  const agent = byId('agent').value;
  const unixProgram = 'umask 077; cmh_file=$(mktemp) || exit 1; '
    + 'trap \'rm -f -- "$cmh_file"\' EXIT; '
    + 'curl -fsSL --proto-redir \'=https\' "$1/hub/install.sh" -o "$cmh_file" '
    + '&& sh "$cmh_file" --link "$2" --agent "$3"';
  const command = byId('platform').value === 'windows'
    ? '$cmhScript = irm -ErrorAction Stop ' + psQuote(origin + '/hub/install.ps1')
      + '; & ([scriptblock]::Create($cmhScript)) -Link ' + psQuote(link) + ' -Agent ' + agent
    : 'sh -c ' + shellQuote(unixProgram) + ' cmh-connect ' + shellQuote(origin)
      + ' ' + shellQuote(link) + ' ' + agent;
  byId('command').value = valid ? command : '';
  byId('prompt').value = valid ? '请按此 Cloud Memory Hub 连接页的说明，使用官方连接器为 '
    + byId('agent').selectedOptions[0].text + ' 安装 MCP 和 Skill，并验证连接：' + link
    + '。保留其他配置，不要输出长期凭证；需要重新加载时告诉我。' : '';
  byId('copy-prompt').disabled = !valid;
  byId('copy-command').disabled = !valid;
}
for (const id of ['agent', 'platform']) byId(id).addEventListener('change', render);
for (const id of ['prompt', 'command']) byId('copy-' + id).addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(byId(id).value);
    byId('notice').textContent = '已复制。';
  } catch {
    byId(id).focus(); byId(id).select();
    byId('notice').textContent = '请复制已选中的文字。';
  }
});
render();"""


def public_connection_response(path: str, method: str, origin: str) -> Response:
    """The boundary has already checked Host/Origin; no credential is consulted."""
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if method not in {"GET", "HEAD"}:
        return Response(status_code=405, headers={**headers, "Allow": "GET, HEAD"})
    if path == "/hub/connect":
        script = SCRIPT.replace("__ORIGIN__", json.dumps(origin).replace("<", "\\u003c"))
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        style_digest = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
        headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'sha256-{digest}'; "
            f"style-src 'sha256-{style_digest}'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        page = PAGE.replace("__SCRIPT__", script).replace("__STYLE__", STYLE)
        return HTMLResponse(page, headers=headers)
    resource = files("cloud_memory_hub.onboarding")
    try:
        connector = resource.joinpath("connect.py").read_bytes()
        if path == "/hub/connect.py":
            return Response(connector, media_type="text/plain", headers=headers)
        if path not in {"/hub/install.sh", "/hub/install.ps1"}:
            return Response(status_code=404, headers=headers)
        content = resource.joinpath(path.rsplit("/", 1)[1]).read_text(encoding="utf-8")
        content = content.replace("__CMH_CONNECT_SHA256__", hashlib.sha256(connector).hexdigest())
        content = content.replace("__CMH_ORIGIN_SHELL__", shlex.quote(origin))
        content = content.replace(
            "__CMH_ORIGIN_POWERSHELL__", "'" + origin.replace("'", "''") + "'"
        )
        return Response(content, media_type="text/plain", headers=headers)
    except (OSError, UnicodeError):
        return Response(
            html.escape("连接组件暂时不可用，请稍后重试。"), status_code=503, headers=headers
        )
