/** ADR-0065: invitation secrets stay in memory, never browser storage or query strings. */
import { validLabel } from "./hub-pairing";

export const agents = {
  claude: "Claude Code",
  codex: "Codex",
  antigravity: "T3 Code (Antigravity)",
  opencode: "OpenCode",
  hermes: "Hermes",
  generic: "其他支持 MCP 的 Agent",
};

export type Agent = keyof typeof agents;

export interface Invitation {
  pairing_id: string;
  invitation_secret: string;
  expires_at: string;
  origin: string;
}

export interface InvitationStatus {
  pairing_id: string;
  scope_id: string;
  state: "pending" | "claimed" | "cancelled" | "expired";
  expires_at: string;
  device_id: string | null;
  source_id: string | null;
}

export interface InvitationProgress {
  version: 1;
  pairing_id: string | null;
}

const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const messages: Record<string, string> = {
  administrator_required: "管理员登录已失效，请重新登录后查询原邀请。",
  authentication_required: "管理员登录已失效，请重新登录后查询原邀请。",
  invalid_request: "请检查项目和设备显示名后重试。",
  unknown_scope: "项目已不存在，请刷新并选择有效项目。",
  pairing_not_found: "服务器已找不到此邀请。可结束本次记录，再明确创建新邀请。",
  pairing_unavailable: "当前 Hub 尚未启用一次性接入，请升级服务后重试。",
  pairing_origin_invalid: "Hub 的公开访问地址配置有误，请修正服务地址后重试。",
  storage_unavailable:
    "浏览器无法保留邀请编号，请恢复站点会话存储后继续；不会自动新建邀请。",
  invalid_response: "邀请响应未能核对，请查询原邀请状态；不要重复生成链接。",
  request_failed: "请求结果尚未确认，请查询原邀请状态后再继续。",
};

export class InvitationError extends Error {
  constructor(
    readonly code: string,
    readonly rejected = false,
  ) {
    super(messages[code] ?? messages.request_failed);
  }
}

export function invitationMessage(reason: unknown) {
  return reason instanceof InvitationError
    ? reason.message
    : messages.request_failed;
}

function invalid(): never {
  throw new InvitationError("invalid_response");
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}

function identity(value: unknown): value is string {
  return typeof value === "string" && uuid.test(value);
}

function date(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function safeOrigin(value: unknown): string {
  if (
    typeof value !== "string" ||
    !/^[\x21-\x7e]+$/.test(value) ||
    /['"\\]/.test(value)
  )
    invalid();
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return invalid();
  }
  const loopback =
    url.hostname === "localhost" ||
    url.hostname === "[::1]" ||
    /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
  if (
    !/^https?:\/\/[^/?#]+$/i.test(value) ||
    url.username ||
    url.password ||
    (url.protocol !== "https:" && !(url.protocol === "http:" && loopback))
  )
    invalid();
  return value;
}

async function request(
  path: string,
  signal: AbortSignal,
  body?: unknown,
): Promise<unknown> {
  const response = await fetch(`/hub/v1/pairings${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let code: unknown;
    try {
      code = object(await response.json()).detail;
    } catch {
      /* Never expose response text. */
    }
    const known =
      typeof code === "string" && Object.hasOwn(messages, code)
        ? code
        : "request_failed";
    const rejected =
      (response.status === 403 && code === "administrator_required") ||
      (response.status === 422 &&
        ["invalid_request", "unknown_scope"].includes(String(code))) ||
      (response.status === 503 &&
        ["pairing_unavailable", "pairing_origin_invalid"].includes(
          String(code),
        ));
    throw new InvitationError(known, rejected);
  }
  try {
    return await response.json();
  } catch {
    return invalid();
  }
}

export async function createInvitation(
  scope: string,
  name: string,
  signal: AbortSignal,
  onIdentity: (id: string) => void,
): Promise<Invitation> {
  if (!identity(scope)) throw new InvitationError("unknown_scope", true);
  const row = object(
    await request("", signal, {
      scope_id: scope,
      display_name: validLabel(name),
    }),
  );
  if (!identity(row.pairing_id)) invalid();
  if (signal.aborted) throw new DOMException("aborted", "AbortError");
  onIdentity(row.pairing_id);
  const invitationValue = row.invitation_secret;
  if (
    typeof invitationValue !== "string" ||
    !/^[A-Za-z0-9_-]{43}$/.test(invitationValue) ||
    !date(row.expires_at)
  )
    invalid();
  return {
    pairing_id: row.pairing_id,
    invitation_secret: invitationValue,
    expires_at: row.expires_at,
    origin: safeOrigin(row.origin),
  };
}

export async function readInvitation(
  id: string,
  scope: string,
  signal: AbortSignal,
  revoke = false,
): Promise<InvitationStatus> {
  if (!identity(id)) invalid();
  const row = object(
    await request(
      `/${id}${revoke ? "/revoke" : ""}`,
      signal,
      revoke ? {} : undefined,
    ),
  );
  if (
    row.pairing_id !== id ||
    row.scope_id !== scope ||
    !["pending", "claimed", "cancelled", "expired"].includes(
      String(row.state),
    ) ||
    !date(row.expires_at) ||
    !(row.device_id === null || identity(row.device_id)) ||
    !(row.source_id === null || identity(row.source_id)) ||
    (row.state === "claimed" &&
      (!identity(row.device_id) || !identity(row.source_id))) ||
    (revoke && row.state !== "cancelled")
  )
    invalid();
  return row as unknown as InvitationStatus;
}

export const invitationKey = (instance: string, scope: string) =>
  `cmh-invitation:${instance}:${scope}`;

export function readInvitationProgress(key: string): InvitationProgress | null {
  const raw = sessionStorage.getItem(key);
  if (raw === null) return null;
  const row = object(JSON.parse(raw));
  if (
    row.version !== 1 ||
    !(row.pairing_id === null || identity(row.pairing_id))
  )
    invalid();
  return { version: 1, pairing_id: row.pairing_id };
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

const unixInstallProgram =
  'umask 077; cmh_file=$(mktemp) || exit 1; trap \'rm -f -- "$cmh_file"\' EXIT; curl -fsSL --proto-redir \'=https\' "$1/hub/install.sh" -o "$cmh_file" || exit $?; set -- --link "$2" --agent "$3"; ';

export function invitationInstructions(invitation: Invitation, agent: Agent) {
  const origin = safeOrigin(invitation.origin);
  const link = `${origin}/hub/connect#pairing_id=${invitation.pairing_id}&secret=${invitation.invitation_secret}`;
  const projectAutomation =
    agent === "claude" || agent === "codex" || agent === "antigravity";
  const workspaceOption = projectAutomation
    ? 'if [ "$(uname -s)-$(uname -m)" = Darwin-arm64 ]; then set -- "$@" --managed-workspace; fi; '
    : "";
  const program = unixInstallProgram + workspaceOption + 'sh "$cmh_file" "$@"';
  const codexNotice =
    agent === "codex" ? "Codex 首次由我按界面提示审查并信任钩子。" : "";
  const workspace = projectAutomation
    ? "在 Apple Silicon Mac 上，如果我已明确打开要共享的本地项目目录，请用 --workspace 指定该目录。" +
      "其他情况下请用 --managed-workspace 为这个 Hub 项目准备专用目录。完成后告诉我在 Agent 中打开哪个目录。" +
      codexNotice
    : "";
  return {
    link,
    prompt: `请按连接页说明，使用官方连接器为 ${agents[agent]} 安装 MCP 和 Skill 并验证连接。请打开下面的连接页。\n${link}\n${workspace}保留其他配置，不输出长期凭证，需要重新加载时告诉我。`,
    unix: `sh -c ${shellQuote(program)} cmh-connect ${shellQuote(origin)} ${shellQuote(link)} ${agent}`,
    powershell: `& { $ErrorActionPreference = 'Stop'; $cmhScript = irm -ErrorAction Stop '${origin}/hub/install.ps1'; & ([scriptblock]::Create($cmhScript)) -Link '${link}' -Agent ${agent} }`,
  };
}
