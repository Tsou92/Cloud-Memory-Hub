"""Public installation instructions; invitations are consumed only by claim."""

import base64
import hashlib
import html
import json
import re
import shlex
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files

from starlette.responses import HTMLResponse, Response

PUBLIC_CONNECT_PATHS = frozenset(
    {
        "/hub/connect",
        "/hub/connect.py",
        "/hub/install.sh",
        "/hub/install.ps1",
        "/hub/assets/hub-orb-v1.png",
    }
)


def _client_release_tag():
    try:
        release = version("cloud-memory-hub")
    except PackageNotFoundError:
        return None
    return f"v{release}" if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release) else None


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>连接 Agent · Cloud Memory Hub</title><style>__STYLE__</style>
<main><h1>把你的 Agent 连接到 Cloud Memory Hub</h1>
<p>记忆保存在 NAS。Apple Silicon Mac 上的 Claude Code CLI 和 Codex 支持自动共享对话和接续。
安装器可为这个 Hub 项目准备专用目录，无须安装独立 App。</p>
<p>请确保这台设备能访问你的 NAS，例如已加入同一个 Tailscale 网络。</p>
<p id="notice" role="status">正在检查连接链接……</p>
<label for="agent">你要使用的 Agent</label>
<select id="agent"><option value="claude">Claude Code</option><option value="codex">Codex</option>
<option value="opencode">OpenCode</option><option value="hermes">Hermes</option>
<option value="generic">其他支持 HTTP MCP 的 Agent</option></select>
<h2>方法一：交给 Agent 安装</h2>
<p>把下面的话交给你要连接的 Agent。如果要沿用已有项目，可以先在 Agent 中打开该目录。</p>
<textarea id="prompt" rows="5" readonly></textarea>
<button id="copy-prompt">复制安装话术</button>
<h2>方法二：运行一条命令</h2>
<label for="platform">运行命令的位置</label>
<select id="platform"><option value="unix">Mac / Linux 终端</option>
<option value="windows">Windows PowerShell</option></select>
<p>复制后在终端运行。安装器会保留其他配置，并准备 MCP、Skill 和当前设备支持的功能。
Linux 和 Windows 可使用 MCP；项目钩子的自动安装目前适用于 Apple Silicon Mac。</p>
<textarea id="command" rows="5" readonly></textarea>
<button id="copy-command">复制命令</button>
<p>安装结果显示共享目录时，请在 Agent 中打开它。安装器会写好钩子配置。
Codex 首次使用需要按提示审查并信任项目钩子一次。</p>
<h2>开始接续</h2><p>自动化配置后，正常对话即可。另一端连接同一项目时先读摘要，需要细节再读原文。</p>
<details><summary>Agent 安装说明与高级入口</summary>
<p>只使用当前 Hub 提供的同源安装脚本。
Mac/Linux 调用 /hub/install.sh，Windows 调用 /hub/install.ps1。
脚本接受 --link 与 --agent（PowerShell 为 -Link 与 -Agent）。
在 Apple Silicon Mac 上使用 --managed-workspace 可为 Hub 项目准备专用目录。
若要绑定已有项目，请改用 --workspace 并指定项目目录。不要输出长期设备凭证。
有 Python 3.11 以上时，可下载同源 /hub/connect.py，以隔离模式 python -I 执行同样参数。
保留其他 MCP 连接；安装失败时重试原链接以恢复进度。没有合适适配器时选择 generic 生成私有配置。</p>
<p>项目自动化只采集绑定目录中接入后的用户消息和最终可见回答，断网时由本机队列补传。
没有绑定目录或宿主不支持时，仅配置远程 MCP。</p>
</details><p><a href="/">返回 NAS 管理页面</a></p></main>
<script>__SCRIPT__</script></html>"""

STYLE = (
    ":root{color-scheme:dark}*{box-sizing:border-box}"
    "body{font:15px/1.7 system-ui,-apple-system,'PingFang SC',sans-serif;"
    "color:#f3f4fc;background:radial-gradient(ellipse at 72% 0,#262640 0,transparent 55%),"
    "#0c1020;margin:0;padding:40px 20px}"
    "main{max-width:800px;margin:auto;overflow-wrap:anywhere;background:"
    "linear-gradient(145deg,#222840e8,#151a2de8);border:1px solid #a0a6dc26;"
    "border-radius:22px;padding:40px;box-shadow:0 28px 90px #03061066}"
    "h1{font-size:28px;line-height:1.35;letter-spacing:-.02em;margin:0 0 20px}"
    "h1:before{content:'∞';display:block;font-size:54px;line-height:1.15;color:#c6b5ff;"
    "text-shadow:0 0 24px #8670ff66;margin-bottom:20px}"
    "h2{font-size:20px;margin-top:32px}p,li{color:#b4b8d0}"
    "label{display:block;color:#e4e5f4;margin:16px 0 8px;font-weight:600}"
    "textarea,select{font:inherit;color:#f3f4fc;background:#101528;"
    "border:1px solid #a0a6dc3b;border-radius:10px;padding:12px;max-width:100%}"
    "textarea{width:100%;resize:vertical;font-size:13px}"
    "button{font:inherit;cursor:pointer;color:white;border:1px solid #c8bcff55;"
    "background:linear-gradient(120deg,#7775dc,#9b80e5);border-radius:10px;"
    "padding:10px 20px;max-width:100%;margin-top:12px;font-weight:600}"
    "button:hover{filter:brightness(1.12)}button:disabled{opacity:.45;cursor:not-allowed}"
    "button:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible"
    "{outline:2px solid #c5afff;outline-offset:3px}"
    "#notice{padding:12px 16px;border:1px solid #a78bfa33;background:#a78bfa12;"
    "border-radius:10px;color:#ddd3ff}details{border-top:1px solid #a0a6dc26;"
    "margin-top:32px;padding-top:20px}summary{cursor:pointer;color:#c9b8ff}"
    "@media(max-width:600px){body{padding:16px 12px}main{padding:24px 20px}"
    "h1{font-size:23px}button,select{width:100%}}"
)


# Hub requests may retry directly, without altering proxy handling for public downloads.
HUB_SHELL_FETCH = r"""cmh_hub_request() {
  cmh_hub_url=$1; cmh_hub_output=$2; shift 2
  cmh_hub_code=$(curl -q --fail --silent --proto '=http,https' \
    --connect-timeout 15 --max-time 180 --write-out '%{http_code}' \
    "$@" "$cmh_hub_url" -o "$cmh_hub_output") || return 1
  case "$cmh_hub_code" in 2??) [ -s "$cmh_hub_output" ] ;; *) return 1 ;; esac
}
cmh_fetch_hub() {
  case "$1" in http://*) cmh_hub_request "$1" "$2" --noproxy '*' && return 0 ;; esac
  case "$1" in https://*)
  cmh_hub_request "$1" "$2" && return 0
  if cmh_hub_request "$1" "$2" --noproxy '*'; then
    printf '%s\n' '已为本次 Hub 下载绕过代理；其他下载仍沿用原代理设置。' >&2
    return 0
  fi
  esac
  printf '%s\n' \
    '无法下载连接组件。请先确认 NAS 服务可访问；使用 Tailscale 时也要确认已连接。' \
    '如用局域网或 Tailscale，在 Clash/Shadowrocket 为服务的精确域名添加 DIRECT 规则。' \
    '如 Hub 必须通过代理访问，请检查代理节点和规则。然后重新打开 Agent，重试并验证 MCP 连接。' \
    '排查指南：https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/proxy-and-network.md' >&2
  return 1
}
"""

HUB_POWERSHELL_FETCH = r"""function Get-CmhHubFile {
    param([string]$Uri, [string]$OutFile)
    $cmhParameters = @{
        UseBasicParsing = $true; Uri = $Uri; OutFile = $OutFile
        TimeoutSec = 180; MaximumRedirection = 0; ErrorAction = 'Stop'; PassThru = $true
    }
    if (([Uri]$Uri).Scheme -eq 'https') {
        try {
            if ($PSVersionTable.PSVersion.Major -lt 6) {
                # PS 5.1 needs explicit environment proxy support; no system settings change.
                $cmhHost = ([Uri]$Uri).DnsSafeHost.Trim([char[]]'[]').ToLowerInvariant()
                $cmhBypass = $false
                $cmhNoProxy = [Environment]::GetEnvironmentVariable('no_proxy')
                foreach ($cmhEntry in ($cmhNoProxy -split ',')) {
                    $cmhEntry = $cmhEntry.Trim().ToLowerInvariant()
                    if ($cmhEntry -eq '*') { $cmhBypass = $true; break }
                    if ($cmhEntry -match '^(?:\[([^]]+)\]|([^:]+))(?::([0-9]+))?$') {
                        $cmhEntryHost = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
                        $cmhEntryPort = $Matches[3]
                        $cmhEntryHost = $cmhEntryHost.TrimStart('.')
                        if ($cmhEntryHost -and
                            (!$cmhEntryPort -or [int]$cmhEntryPort -eq ([Uri]$Uri).Port) -and
                            ($cmhHost -eq $cmhEntryHost -or
                             $cmhHost.EndsWith('.' + $cmhEntryHost))) { $cmhBypass = $true }
                    }
                }
                if ($cmhBypass) { throw 'environment_proxy_bypassed' }
                $cmhProxyValue = $null
                foreach ($cmhName in @('https_proxy','HTTPS_PROXY','all_proxy','ALL_PROXY')) {
                    $cmhProxyValue = [Environment]::GetEnvironmentVariable($cmhName)
                    if ($cmhProxyValue) { break }
                }
                if ($cmhProxyValue) {
                    $cmhProxy = [Uri]$cmhProxyValue
                    $cmhParameters.Proxy = $cmhProxy.Scheme + '://' + $cmhProxy.Authority
                    if ($cmhProxy.UserInfo) {
                        $cmhParts = $cmhProxy.UserInfo.Split(':', 2)
                        $cmhUsername = [Uri]::UnescapeDataString($cmhParts[0])
                        $cmhPassword = [Security.SecureString]::new()
                        if ($cmhParts.Count -gt 1 -and $cmhParts[1]) {
                            $cmhPassword = ConvertTo-SecureString -AsPlainText -Force -String (
                                [Uri]::UnescapeDataString($cmhParts[1]))
                        }
                        $cmhParameters.ProxyCredential =
                            [Management.Automation.PSCredential]::new($cmhUsername, $cmhPassword)
                    }
                }
            }
            $cmhResponse = Invoke-WebRequest @cmhParameters
            if ([int]$cmhResponse.StatusCode -ge 200 -and
                [int]$cmhResponse.StatusCode -lt 300 -and
                (Get-Item -LiteralPath $OutFile).Length -gt 0) { return }
        } catch { }
    }
    try {
        if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey('NoProxy')) {
            $cmhParameters.NoProxy = $true
            $cmhResponse = Invoke-WebRequest @cmhParameters
            if ([int]$cmhResponse.StatusCode -lt 200 -or
                [int]$cmhResponse.StatusCode -ge 300 -or
                (Get-Item -LiteralPath $OutFile).Length -eq 0) { throw 'hub_status_invalid' }
        } else {
            # Windows PowerShell 5.1: disable the proxy on this request only.
            $cmhRequest = [Net.HttpWebRequest]::Create([Uri]$Uri)
            $cmhRequest.Proxy = $null
            $cmhRequest.AllowAutoRedirect = $false
            $cmhRequest.Timeout = 180000
            $cmhRequest.ReadWriteTimeout = 180000
            $cmhResponse = $null; $cmhInput = $null; $cmhOutput = $null
            try {
                $cmhResponse = $cmhRequest.GetResponse()
                if ([int]$cmhResponse.StatusCode -lt 200 -or
                [int]$cmhResponse.StatusCode -ge 300) { throw 'hub_status_invalid' }
                $cmhInput = $cmhResponse.GetResponseStream()
                $cmhOutput = [IO.File]::Open($OutFile, [IO.FileMode]::Create,
                    [IO.FileAccess]::Write, [IO.FileShare]::None)
                $cmhInput.CopyTo($cmhOutput)
                if ($cmhOutput.Length -eq 0 -or
                    ($cmhResponse.ContentLength -ge 0 -and
                     $cmhOutput.Length -ne $cmhResponse.ContentLength)) {
                    throw 'hub_download_incomplete'
                }
                $cmhOutput.Flush($true)
            } finally {
                if ($cmhOutput) { $cmhOutput.Dispose() }
                if ($cmhInput) { $cmhInput.Dispose() }
                if ($cmhResponse) { $cmhResponse.Dispose() }
            }
        }
        return
    } catch {
        [Console]::Error.WriteLine(
            ('无法下载连接组件。请先确认 NAS 服务可访问；使用 Tailscale 时也要确认已连接。' +
            '如用局域网或 Tailscale，在 Clash/Shadowrocket 为服务的精确域名添加 DIRECT 规则。' +
            '如 Hub 必须通过代理访问，请检查代理节点和规则。' +
            '然后重新打开 Agent，重试并验证 MCP 连接。排查指南：' +
            'https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/proxy-and-network.md'))
        throw 'cmh_hub_download_failed'
    }
}
"""

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
  const windows = byId('platform').value === 'windows';
  const projectAutomation = agent === 'claude' || agent === 'codex';
  const unixProgram = 'umask 077; cmh_file=$(mktemp) || exit 1; '
    + 'trap \'rm -f -- "$cmh_file"\' EXIT; '
    + __CMH_HUB_SHELL_FETCH__
    + 'cmh_fetch_hub "$1/hub/install.sh" "$cmh_file" || exit $?; '
    + 'set -- --link "$2" --agent "$3"; '
    + (projectAutomation
      ? 'if [ "$(uname -s)-$(uname -m)" = Darwin-arm64 ]; '
        + 'then set -- "$@" --managed-workspace; fi; '
      : '')
    + 'sh "$cmh_file" "$@"';
  const windowsProgram = "& { $ErrorActionPreference = 'Stop'; "
    + __CMH_HUB_POWERSHELL_FETCH__
    + '$cmhFile = [IO.Path]::GetTempFileName(); try { Get-CmhHubFile -Uri '
    + psQuote(origin + '/hub/install.ps1')
    + ' -OutFile $cmhFile; & ([scriptblock]::Create([IO.File]::ReadAllText($cmhFile))) -Link '
    + psQuote(link) + ' -Agent ' + agent
    + ' } finally { Remove-Item -LiteralPath $cmhFile -Force } }';
  const command = windows
    ? windowsProgram
    : 'sh -c ' + shellQuote(unixProgram) + ' cmh-connect ' + shellQuote(origin)
      + ' ' + shellQuote(link) + ' ' + agent;
  byId('command').value = valid ? command : '';
  byId('prompt').value = valid ? '请按此 Cloud Memory Hub 连接页的说明，使用官方连接器为 '
    + byId('agent').selectedOptions[0].text
    + ' 安装 MCP 和 Skill 并验证连接。请打开下面的连接页。\n' + link + '\n'
    + (projectAutomation && !windows
      ? '在 Apple Silicon Mac 上，如果我已明确打开要共享的本地项目目录，'
        + '请用 --workspace 指定该目录。'
        + '其他情况下请用 --managed-workspace 为这个 Hub 项目准备专用目录。'
        + '完成后告诉我在 Agent 中打开哪个目录。Codex 首次由我按界面提示审查并信任钩子。'
      : '')
    + '保留其他配置，不要输出长期凭证。网络或代理问题请按 '
    + 'https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/proxy-and-network.md '
    + '排查，按诊断选择此 Hub 直连或代理，不改其他服务设置。' : '';
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
    if path == "/hub/assets/hub-orb-v1.png":
        try:
            image = files("cloud_memory_hub").joinpath("web_assets", "hub-orb.png").read_bytes()
        except OSError:
            return Response(status_code=503, headers=headers)
        return Response(
            image,
            media_type="image/png",
            headers={**headers, "Cache-Control": "public, max-age=86400"},
        )
    if path == "/hub/connect":
        script = SCRIPT.replace("__ORIGIN__", json.dumps(origin).replace("<", "\\u003c"))
        script = script.replace("__CMH_HUB_SHELL_FETCH__", json.dumps(HUB_SHELL_FETCH))
        script = script.replace("__CMH_HUB_POWERSHELL_FETCH__", json.dumps(HUB_POWERSHELL_FETCH))
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
        release_tag = _client_release_tag()
        connector = connector.replace(
            b"CLIENT_RELEASE_TAG = None  # CMH_RELEASE_TAG_PLACEHOLDER",
            f"CLIENT_RELEASE_TAG = {release_tag!r}".encode("ascii"),
        )
        if path == "/hub/connect.py":
            return Response(connector, media_type="text/plain", headers=headers)
        if path not in {"/hub/install.sh", "/hub/install.ps1"}:
            return Response(status_code=404, headers=headers)
        content = resource.joinpath(path.rsplit("/", 1)[1]).read_text(encoding="utf-8")
        content = content.replace("__CMH_CONNECT_SHA256__", hashlib.sha256(connector).hexdigest())
        content = content.replace(
            "__CMH_CLIENT_RELEASE_TAG_SHELL__", shlex.quote(release_tag or "")
        )
        content = content.replace("__CMH_HUB_SHELL_FETCH__", HUB_SHELL_FETCH)
        content = content.replace("__CMH_HUB_POWERSHELL_FETCH__", HUB_POWERSHELL_FETCH)
        content = content.replace("__CMH_ORIGIN_SHELL__", shlex.quote(origin))
        content = content.replace(
            "__CMH_ORIGIN_POWERSHELL__", "'" + origin.replace("'", "''") + "'"
        )
        return Response(content, media_type="text/plain", headers=headers)
    except (OSError, UnicodeError):
        return Response(
            html.escape("连接组件暂时不可用，请稍后重试。"), status_code=503, headers=headers
        )
