/** Administrator console transport. Never falls back to Core's local namespace. */
export interface HubProject {
  scope_id: string;
  display_name: string;
}

export interface HubBootstrap {
  schema_version: 1;
  mode: "hub";
  product: "Cloud Memory Hub";
  instance_id: string;
  version: string | null;
  build_revision: string | null;
  features: string[];
  projects: HubProject[];
  next_offset: number | null;
  global_scope: HubProject;
  preview?: "ui-copy-synthetic";
}

export interface HubSource {
  source_id: string;
  device_id: string;
  scope_id: string;
  display_name: string;
  adapter_id: string;
  status: string;
  device_display_name: string;
  device_status: string;
  /** Unix seconds of the last accepted upload; absent when it cannot be proven. */
  last_accepted_at?: number | null;
}

export interface HubSummary {
  scope: HubProject;
  sources: HubSource[];
  sources_total: number;
  next_offset: number | null;
  visible_memory_total: number | null;
  visible_history_total: number | null;
}

export interface HubDashboardData {
  schema_version: 1;
  instance_id: string;
  scope_id: string;
  generated_at: string;
  statistics: {
    device_connections: number;
    agent_connections: number;
    shared_records: number;
    handoffs: number;
  };
  record_types: {
    kind: "conversation" | "remember" | "handoff" | "other";
    label: string;
    count: number;
  }[];
  daily_activity: { date: string; count: number }[];
  activity_timezone: "UTC";
  activity_state: "ready" | "unavailable";
  missing_timestamps: number;
}

export interface HubItem {
  record_id: string;
  kind: "memory" | "history";
  content: string;
  content_offset: number;
  next_content_offset: number | null;
  layer: string | null;
  agent_id: string;
  session_id: string | null;
  source: string | null;
  source_raw_ids: string[];
  references_truncated?: boolean;
  memory_at: number | null;
}

export type HubReadOperation =
  | "memory-list"
  | "memory-get"
  | "memory-search"
  | "history-list"
  | "history-search"
  | "history-get";

export interface HubPage {
  items: HubItem[];
  next_offset: number | null;
  candidate_limit_reached: boolean;
}

export class HubError extends Error {}

const errors: Record<string, string> = {
  administrator_required: "登录已失效，请重新登录管理网页。",
  authentication_required: "登录已失效，请重新登录管理网页。",
  console_scope_not_found: "这个项目已不存在，请刷新项目列表后重新选择。",
  record_not_found: "这条内容已不可读取，请刷新当前项目。",
  read_busy: "记忆服务正在处理其他请求，请稍后重试。",
  read_unavailable: "记忆服务暂时不可用，请稍后重试。",
  query_requires_refiltering: "问题中含有需要保护的内容，请移除凭证后重试。",
  memory_answer_provider_unsupported:
    "当前模型尚不支持网页问答，请检查模型配置。",
  memory_answer_model_unavailable: "无法连接问答模型，请检查模型设置后重试。",
  memory_answer_model_failed: "问答模型未能完成请求，请检查模型设置后重试。",
  memory_not_editable: "这条记录属于只读投影或原始记录，不支持直接编辑。",
  invalid_request: "提交的内容不符合格式或长度要求，请检查后重试。",
};

export function hubErrorMessage(error: unknown): string {
  return error instanceof HubError
    ? error.message
    : "读取未完成，请检查连接后重试。";
}

export function isAuthExpiredError(message: string): boolean {
  return (
    message === "登录已失效，请重新登录管理网页。" ||
    message === "当前请求未获授权，请重新登录后重试。" ||
    message === "管理员登录已失效，请重新登录。"
  );
}

function invalid(): never {
  throw new HubError("服务返回的数据与当前项目不一致，请刷新后重试。");
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}

function cursor(value: unknown): value is number | null {
  return value === null || (Number.isSafeInteger(value) && Number(value) >= 0);
}

function project(value: unknown): HubProject {
  const row = object(value);
  if (
    typeof row.scope_id !== "string" ||
    !row.scope_id ||
    typeof row.display_name !== "string"
  )
    invalid();
  return row as unknown as HubProject;
}

async function request(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  const response = await fetch(`/hub/v1/console/${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let code: unknown;
    try {
      code = object(await response.json()).detail;
    } catch {
      /* Never echo a server body. */
    }
    throw new HubError(
      typeof code === "string" && errors[code]
        ? errors[code]
        : response.status === 404
          ? "当前服务尚不支持这个网页接口，请升级 Hub 后重试。"
          : response.status === 401 || response.status === 403
            ? "当前请求未获授权，请重新登录后重试。"
            : "读取未完成，请检查服务状态后重试。",
    );
  }
  try {
    return await response.json();
  } catch {
    return invalid();
  }
}

export async function getHubBootstrap(
  offset = 0,
  signal?: AbortSignal,
): Promise<HubBootstrap> {
  const value = object(
    await request(`bootstrap?limit=50&offset=${offset}`, undefined, signal),
  );
  if (
    value.schema_version !== 1 ||
    value.mode !== "hub" ||
    value.product !== "Cloud Memory Hub" ||
    typeof value.instance_id !== "string" ||
    !Array.isArray(value.projects) ||
    !Array.isArray(value.features) ||
    !value.features.every((v) => typeof v === "string") ||
    !cursor(value.next_offset) ||
    !(value.version === null || typeof value.version === "string") ||
    !(value.build_revision === null || typeof value.build_revision === "string")
  )
    invalid();
  value.projects.forEach(project);
  if (project(value.global_scope).scope_id !== "global") invalid();
  return value as unknown as HubBootstrap;
}

export async function getHubSummary(
  instance: string,
  scope: string,
  offset = 0,
  signal?: AbortSignal,
): Promise<HubSummary> {
  const value = object(
    await request("summary", { scope_id: scope, limit: 10, offset }, signal),
  );
  if (
    value.schema_version !== 1 ||
    value.instance_id !== instance ||
    project(value.scope).scope_id !== scope ||
    !Array.isArray(value.sources) ||
    !cursor(value.next_offset) ||
    !Number.isSafeInteger(value.sources_total) ||
    !cursor(value.visible_memory_total) ||
    !cursor(value.visible_history_total)
  )
    invalid();
  for (const raw of value.sources) {
    const row = object(raw);
    if (
      row.scope_id !== scope ||
      ![
        "source_id",
        "device_id",
        "display_name",
        "adapter_id",
        "status",
        "device_display_name",
        "device_status",
      ].every((key) => typeof row[key] === "string")
    )
      invalid();
    if (
      row.last_accepted_at !== undefined &&
      row.last_accepted_at !== null &&
      !Number.isSafeInteger(row.last_accepted_at)
    )
      invalid();
  }
  return value as unknown as HubSummary;
}

export async function getHubDashboard(
  instance: string,
  scope: string,
  signal?: AbortSignal,
): Promise<HubDashboardData> {
  const value = object(await request("dashboard", { scope_id: scope }, signal));
  const statistics = object(value.statistics);
  const count = (number: unknown) =>
    Number.isSafeInteger(number) && Number(number) >= 0;
  const recordKinds = new Set(["conversation", "remember", "handoff", "other"]);
  if (
    value.schema_version !== 1 ||
    value.instance_id !== instance ||
    value.scope_id !== scope ||
    typeof value.generated_at !== "string" ||
    !/(?:Z|\+00:00)$/.test(value.generated_at) ||
    !Number.isFinite(Date.parse(value.generated_at)) ||
    ![
      "device_connections",
      "agent_connections",
      "shared_records",
      "handoffs",
    ].every((key) => count(statistics[key])) ||
    !Array.isArray(value.record_types) ||
    value.record_types.length !== 4 ||
    !Array.isArray(value.daily_activity) ||
    value.activity_timezone !== "UTC" ||
    !["ready", "unavailable"].includes(String(value.activity_state)) ||
    !count(value.missing_timestamps)
  )
    invalid();
  for (const entry of value.record_types) {
    const row = object(entry);
    if (
      typeof row.kind !== "string" ||
      !recordKinds.delete(row.kind) ||
      typeof row.label !== "string" ||
      !count(row.count)
    )
      invalid();
  }
  if (
    value.activity_state === "ready"
      ? value.daily_activity.length !== 7 || value.missing_timestamps !== 0
      : value.daily_activity.length !== 0 || value.missing_timestamps === 0
  )
    invalid();
  let previous = "";
  for (const entry of value.daily_activity) {
    const row = object(entry);
    if (
      typeof row.date !== "string" ||
      !/^\d{4}-\d{2}-\d{2}$/.test(row.date) ||
      !Number.isFinite(Date.parse(row.date)) ||
      (previous !== "" &&
        Date.parse(row.date) - Date.parse(previous) !== 86400000) ||
      new Date(row.date).toISOString().slice(0, 10) !== row.date ||
      !count(row.count)
    )
      invalid();
    previous = row.date;
  }
  if (
    value.activity_state === "ready" &&
    previous !== value.generated_at.slice(0, 10)
  )
    invalid();
  return value as unknown as HubDashboardData;
}

export async function readHub(
  instance: string,
  scope: string,
  operation: HubReadOperation,
  parameters: Record<string, unknown> = {},
  signal?: AbortSignal,
): Promise<HubPage> {
  const value = object(
    await request(
      `read/${operation}`,
      { ...parameters, scope_id: scope },
      signal,
    ),
  );
  if (
    value.schema_version !== 1 ||
    value.authority !== "administrator" ||
    value.instance_id !== instance ||
    value.scope_id !== scope ||
    value.operation !== operation ||
    value.content_trust !== "untrusted_history" ||
    !Array.isArray(value.items) ||
    value.items.length > 10 ||
    value.returned_count !== value.items.length ||
    !cursor(value.next_offset) ||
    typeof value.candidate_limit_reached !== "boolean"
  )
    invalid();
  const kind = operation.startsWith("history-") ? "history" : "memory";
  for (const raw of value.items) {
    const item = object(raw);
    if (
      item.kind !== kind ||
      typeof item.record_id !== "string" ||
      typeof item.content !== "string" ||
      !cursor(item.content_offset) ||
      item.content_offset === null ||
      !cursor(item.next_content_offset) ||
      typeof item.agent_id !== "string" ||
      !(item.layer === null || typeof item.layer === "string") ||
      !(item.source === null || typeof item.source === "string") ||
      !(item.session_id === null || typeof item.session_id === "string") ||
      !Array.isArray(item.source_raw_ids) ||
      !item.source_raw_ids.every((v) => typeof v === "string") ||
      ("references_truncated" in item &&
        typeof item.references_truncated !== "boolean") ||
      !cursor(item.memory_at)
    )
      invalid();
    if (
      operation.endsWith("-get") &&
      (item.record_id !== parameters.record_id ||
        item.content_offset !== (parameters.content_offset ?? 0))
    )
      invalid();
  }
  if (operation.endsWith("-get") && value.items.length !== 1) invalid();
  return value as unknown as HubPage;
}

export async function correctHubMemory(
  scope: string,
  recordId: string,
  content: string,
): Promise<{ record_id: string; content: string }> {
  const value = object(
    await request("correct", { scope_id: scope, record_id: recordId, content }),
  );
  if (
    value.schema_version !== 1 ||
    value.scope_id !== scope ||
    value.record_id !== recordId ||
    typeof value.content !== "string" ||
    value.state !== "corrected"
  )
    invalid();
  return { record_id: recordId, content: value.content };
}

export async function getAllHubSources(
  instance: string,
  scope: string,
  signal?: AbortSignal,
): Promise<HubSource[]> {
  const seen = new Map<string, HubSource>();
  let offset = 0;
  for (let pageIndex = 0; pageIndex < 100; pageIndex += 1) {
    const page = await getHubSummary(instance, scope, offset, signal);
    for (const source of page.sources) {
      if (!seen.has(source.source_id)) seen.set(source.source_id, source);
    }
    if (page.next_offset === null) return [...seen.values()];
    if (page.next_offset <= offset) invalid();
    offset = page.next_offset;
  }
  throw new HubError("当前项目来源数量超出控制台分页预算，请通过命令行检查。");
}
