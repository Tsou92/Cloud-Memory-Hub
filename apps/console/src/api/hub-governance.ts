/** Existing Hub governance contract; observations are never execution authority. */
export interface ForgetItem {
  record_id: string;
  kind: "memory" | "raw";
  layer: string;
  status: string;
  content: string;
  content_truncated: boolean;
  reasons: string[];
  unresolved_origin: boolean;
  proposed_effect: "retain_for_explicit_read" | "suppress_automatic_use";
}

export type GovernanceAction = "forget" | "purge";

export interface ForgetPreview {
  scope_id: string;
  action: GovernanceAction;
  view_digest: string;
  execution_available: boolean;
  total: number;
  raw_count: number;
  memory_count: number;
  unresolved_origin_count: number;
  items: ForgetItem[];
  next_offset: number | null;
}

export interface ForgetRequest {
  scope_id: string;
  action: GovernanceAction;
  record_ids: [string];
  expected_view: string;
  operation_id: string;
}

export interface PendingForget {
  version: 1;
  instance: string;
  request: ForgetRequest;
  total: number;
  memoryCount: number;
  rawCount: number;
}

export interface ForgetResult {
  operation_id: string;
  scope_id: string;
  action: GovernanceAction;
  state: "completed";
  suppressed_count: number;
}

const messages: Record<string, string> = {
  governance_preview_changed:
    "影响范围已变化，本次尚未执行。请重新预览后再确认。",
  governance_selection_not_found: "这条记忆已不可处理，请刷新当前项目。",
  governance_scope_not_found: "当前项目已不存在，请重新选择项目。",
  governance_reference_unresolved: "部分来源关系不完整，暂时无法确认影响范围。",
  governance_forget_kind_unsupported:
    "影响范围包含暂不支持忘记的工作或编程记录。",
  governance_derivation_unmodeled:
    "当前项目有尚未覆盖的派生记录，服务暂不能执行忘记。",
  governance_preview_busy: "服务正在检查其他记录，请稍后重新预览。",
  administrator_required: "管理员登录已失效，请重新登录后继续。",
  authentication_required: "管理员登录已失效，请重新登录后继续。",
  origin_required: "请从已配置的 Hub 管理地址打开页面后重试。",
};

export class GovernanceError extends Error {
  constructor(readonly code: string) {
    super(
      messages[code] ??
        "服务未确认操作结果。请保留此页面并核对原操作，不要重复创建请求。",
    );
  }
}

const recordPattern = /^[A-Za-z0-9_:-]{1,200}$/;
const digestPattern = /^[0-9a-f]{64}$/;
const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function invalid(): never {
  throw new GovernanceError("invalid_response");
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}

function count(value: unknown): value is number {
  return (
    Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= 20_000
  );
}

async function request(path: string, body: unknown, signal?: AbortSignal) {
  const response = await fetch(`/hub/v1/governance/${path}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let code: unknown;
    try {
      code = object(await response.json()).detail;
    } catch {
      /* Do not echo response bodies. */
    }
    throw new GovernanceError(
      typeof code === "string" && code in messages ? code : "request_failed",
    );
  }
  try {
    return object(await response.json());
  } catch {
    return invalid();
  }
}

export async function previewForget(
  scope: string,
  id: string,
  offset = 0,
  expectedView?: string,
  signal?: AbortSignal,
  action: GovernanceAction = "forget",
): Promise<ForgetPreview> {
  const row = await request(
    "preview",
    {
      scope_id: scope,
      action,
      record_ids: [id],
      offset,
      limit: 50,
      ...(expectedView ? { expected_view: expectedView } : {}),
    },
    signal,
  );
  if (
    row.scope_id !== scope ||
    row.action !== action ||
    typeof row.view_digest !== "string" ||
    !digestPattern.test(row.view_digest) ||
    (expectedView && row.view_digest !== expectedView) ||
    row.consistency !== "observational" ||
    row.coverage !== "explicit_stored_relations" ||
    row.content_trust !== "untrusted_history" ||
    typeof row.execution_available !== "boolean" ||
    !count(row.total) ||
    !count(row.raw_count) ||
    !count(row.memory_count) ||
    row.raw_count + row.memory_count !== row.total ||
    !count(row.unresolved_origin_count) ||
    row.unresolved_origin_count > row.memory_count ||
    !Array.isArray(row.items) ||
    row.items.length > 50
  )
    invalid();
  const end = offset + row.items.length;
  if (
    end > row.total ||
    row.next_offset !== (end < row.total ? end : null) ||
    (end < row.total && end === offset)
  )
    invalid();
  const ids = new Set<string>();
  for (const value of row.items) {
    const item = object(value);
    if (
      typeof item.record_id !== "string" ||
      !recordPattern.test(item.record_id) ||
      ids.has(item.record_id) ||
      !["memory", "raw"].includes(String(item.kind)) ||
      typeof item.layer !== "string" ||
      typeof item.status !== "string" ||
      typeof item.content !== "string" ||
      [...item.content].length > 512 ||
      typeof item.content_truncated !== "boolean" ||
      typeof item.unresolved_origin !== "boolean" ||
      !Array.isArray(item.reasons) ||
      !item.reasons.every((reason) =>
        ["selected", "evolution", "source", "derived"].includes(String(reason)),
      ) ||
      item.proposed_effect !==
        (item.kind === "raw"
          ? "retain_for_explicit_read"
          : "suppress_automatic_use")
    )
      invalid();
    ids.add(item.record_id);
  }
  return row as unknown as ForgetPreview;
}

export function appendPreview(
  previous: ForgetPreview,
  page: ForgetPreview,
): ForgetPreview {
  if (
    [
      "scope_id",
      "view_digest",
      "total",
      "raw_count",
      "memory_count",
      "unresolved_origin_count",
    ].some(
      (key) =>
        previous[key as keyof ForgetPreview] !==
        page[key as keyof ForgetPreview],
    )
  )
    invalid();
  const items = [...previous.items, ...page.items];
  if (
    new Set(items.map((item) => item.record_id)).size !== items.length ||
    items.length > page.total
  )
    invalid();
  return { ...page, items };
}

export async function executeForget(
  pending: PendingForget,
  signal?: AbortSignal,
): Promise<ForgetResult> {
  const row = await request("execute", pending.request, signal);
  if (
    row.scope_id !== pending.request.scope_id ||
    row.action !== pending.request.action ||
    row.operation_id !== pending.request.operation_id ||
    row.state !== "completed" ||
    row.suppressed_count !== pending.total
  )
    invalid();
  return row as unknown as ForgetResult;
}

export function pendingKey(
  instance: string,
  scope: string,
  action: GovernanceAction = "forget",
) {
  return `cmh-governance:${action}:${encodeURIComponent(instance)}:${encodeURIComponent(scope)}`;
}

function legacyPendingKey(instance: string, scope: string) {
  return `cmh-forget:${encodeURIComponent(instance)}:${encodeURIComponent(scope)}`;
}

export function clearPending(
  instance: string,
  scope: string,
  action: GovernanceAction = "forget",
) {
  sessionStorage.removeItem(pendingKey(instance, scope, action));
  if (action === "forget")
    sessionStorage.removeItem(legacyPendingKey(instance, scope));
}

export function readPending(
  instance: string,
  scope: string,
  action: GovernanceAction = "forget",
): PendingForget | null {
  const serialized =
    sessionStorage.getItem(pendingKey(instance, scope, action)) ??
    (action === "forget"
      ? sessionStorage.getItem(legacyPendingKey(instance, scope))
      : null);
  if (!serialized) return null;
  const value = object(JSON.parse(serialized));
  const requestObj = object(value.request);
  if (
    value.version !== 1 ||
    requestObj.action !== action ||
    value.instance !== instance ||
    requestObj.scope_id !== scope ||
    (requestObj.action !== "forget" && requestObj.action !== "purge") ||
    !Array.isArray(requestObj.record_ids) ||
    requestObj.record_ids.length !== 1 ||
    typeof requestObj.record_ids[0] !== "string" ||
    !recordPattern.test(requestObj.record_ids[0]) ||
    typeof requestObj.expected_view !== "string" ||
    !digestPattern.test(requestObj.expected_view) ||
    typeof requestObj.operation_id !== "string" ||
    !uuidPattern.test(requestObj.operation_id) ||
    !count(value.total) ||
    !count(value.memoryCount) ||
    !count(value.rawCount) ||
    value.rawCount + value.memoryCount !== value.total
  )
    invalid();
  return value as unknown as PendingForget;
}
