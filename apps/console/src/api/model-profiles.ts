export type ProviderKind =
  | "hy_default"
  | "openai_compatible"
  | "anthropic"
  | "ollama"
  | "local_onnx";

export type CredentialState = "available" | "missing" | "unavailable";

export interface ModelProfile {
  profile_id: string;
  revision: number;
  name: string;
  provider_kind: ProviderKind;
  base_url: string | null;
  model: string;
  embedding_model?: string | null;
  embedding_provider_kind?: ProviderKind | null;
  embedding_base_url?: string | null;
  embedding_dimensions?: number | null;
  timeout_seconds?: number | null;
  max_concurrency?: number | null;
  context_window?: number | null;
  disable_thinking?: boolean;
  has_api_key: boolean;
  credential_state?: CredentialState | null;
  is_active: boolean;
  last_probed_at?: string | null;
  last_probe_ok?: boolean | null;
}

export interface ModelProfileInput {
  name: string;
  provider_kind: ProviderKind;
  base_url?: string | null;
  model: string;
  embedding_model?: string | null;
  embedding_provider_kind?: ProviderKind | null;
  embedding_base_url?: string | null;
  embedding_dimensions?: number | null;
  timeout_seconds?: number | null;
  max_concurrency?: number | null;
  context_window?: number | null;
  disable_thinking?: boolean;
  api_key?: string;
}

export interface ModelProfilePatch {
  expected_revision: number;
  name?: string;
  base_url?: string | null;
  model?: string;
  embedding_model?: string | null;
  embedding_provider_kind?: ProviderKind | null;
  embedding_base_url?: string | null;
  embedding_dimensions?: number | null;
  api_key?: string;
  clear_api_key?: boolean;
  timeout_seconds?: number | null;
  max_concurrency?: number | null;
  context_window?: number | null;
  disable_thinking?: boolean;
}

export type ProbeCode =
  | "ok"
  | "hy_default_unavailable"
  | "missing_api_key"
  | "unreachable"
  | "unauthorized"
  | "rate_limited"
  | "insufficient_balance"
  | "model_not_found"
  | "model_list_unsupported"
  | "invalid_response"
  | "structured_output_unsupported"
  | "embedding_model_required"
  | "embedding_unavailable";

export interface ModelProbeStage {
  stage:
    | "endpoint"
    | "provider"
    | "authentication"
    | "model"
    | "structured_output"
    | "embedding";
  ok: boolean;
  code: ProbeCode;
  latency_ms: number;
  embedding_dimensions?: number | null;
}

export interface ModelProbeResult {
  profile_id: string;
  profile_revision: number;
  input_profile_revision: number;
  stages: ModelProbeStage[];
  capabilities: {
    endpoint_reachable: boolean;
    model_available: boolean;
    structured_output: boolean;
    embedding_available: boolean;
  };
}

export const MODEL_CATALOG_CODES = [
  "ok",
  "unsupported",
  "missing_api_key",
  "unreachable",
  "unauthorized",
  "invalid_response",
  "rate_limited",
  "insufficient_balance",
  "profile_changed",
] as const;

export type ModelCatalogCode = (typeof MODEL_CATALOG_CODES)[number];

export interface ModelCatalogInput {
  provider_kind: ProviderKind;
  base_url: string;
  api_key?: string;
  profile_id?: string;
  expected_profile_revision?: number;
}

export interface ModelCatalogResult {
  code: ModelCatalogCode;
  models: Array<{ id: string; display_name: string }>;
  has_more: boolean;
}

export interface SedimentationState {
  enabled: boolean;
  updated_at?: string | null;
}

const PROVIDERS: readonly ProviderKind[] = [
  "hy_default",
  "openai_compatible",
  "anthropic",
  "ollama",
  "local_onnx",
];

const CREDENTIAL_STATES = ["available", "missing", "unavailable"] as const;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PENDING_PROBE_KEY = "one-memory.pending-model-probe.v1";

export class ModelApiError extends Error {
  constructor(
    readonly code: string,
    message?: string,
  ) {
    super(message ?? modelErrorMessage(code));
  }
}

export function modelErrorMessage(code: string): string {
  const map: Record<string, string> = {
    authentication_required: "登录已失效，请重新登录管理网页。",
    name_invalid: "请填写配置名称（1–128 个字符）。",
    model_invalid: "请填写模型名称（1–256 个字符）。",
    base_url_invalid: "请填写有效的服务接口地址（HTTPS 或本机地址）。",
    provider_kind_invalid: "不支持的模型接口协议类型。",
    active_model_profile: "当前已启用的模型不能直接删除，请先切换启用其他配置。",
    model_profile_not_found: "该模型配置已不存在，请刷新列表。",
    revision_conflict: "模型配置已被其他页面更新，请刷新后重试。",
    model_probe_in_flight: "正在执行连接测试，请等待当前测试完成。",
    model_probe_operation_pending:
      "上一次连接测试尚未收到结果，请再次点击「核对测试结果」查询原测试状态。",
    model_probe_other_profile_pending:
      "另一个模型配置有一项未决的连接测试，请先在该配置上核对测试结果。",
    model_probe_operation_stale:
      "模型配置已变更或上一次探测未能完成，状态已重置，请重新发起测试。",
    model_probe_capability_unavailable:
      "无法发起连接测试，请检查后台服务连接状态。",
    sedimentation_receipt_invalid:
      "自动整理状态未能确认，仍显示上次确认的状态；请重试。",
  };
  return map[code] ?? "模型服务操作未完成，请检查连接或参数后重试。";
}

function boundedValue(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length <= max;
}

export function parseModelProfileReceipt(value: unknown): ModelProfile | null {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return null;
  const profile = value as Record<string, unknown>;
  const hasKey = profile.has_api_key;
  if (
    !boundedValue(profile.name, 256) ||
    !PROVIDERS.includes(profile.provider_kind as ProviderKind) ||
    !boundedValue(profile.model, 256) ||
    typeof hasKey !== "boolean" ||
    typeof profile.is_active !== "boolean" ||
    (profile.credential_state !== undefined &&
      profile.credential_state !== null &&
      !(CREDENTIAL_STATES as readonly unknown[]).includes(
        profile.credential_state,
      )) ||
    !boundedValue(profile.profile_id, 128) ||
    profile.profile_id.length < 1 ||
    !Number.isSafeInteger(profile.revision) ||
    (profile.revision as number) < 1
  )
    return null;
  return profile as unknown as ModelProfile;
}

async function coreRequest(
  path: string,
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE" = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<{ status: number; data: unknown }> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (response.status === 401 || response.status === 403) {
    throw new ModelApiError("authentication_required");
  }
  return { status: response.status, data };
}

export async function getModelProfiles(
  signal?: AbortSignal,
): Promise<ModelProfile[]> {
  const { status, data } = await coreRequest(
    "/api/v1/model-profiles",
    "GET",
    undefined,
    signal,
  );
  if (status < 200 || status >= 300 || !Array.isArray(data)) {
    throw new ModelApiError("model_profiles_invalid");
  }
  const parsed = data.map(parseModelProfileReceipt);
  if (parsed.some((item) => item === null)) {
    throw new ModelApiError("model_profiles_invalid");
  }
  return parsed as ModelProfile[];
}

export async function createModelProfile(
  input: ModelProfileInput,
): Promise<ModelProfile> {
  const name = input.name.trim();
  const model = input.model.trim();
  if (!name || name.length > 128) throw new ModelApiError("name_invalid");
  if (!model || model.length > 256) throw new ModelApiError("model_invalid");
  if (!PROVIDERS.includes(input.provider_kind))
    throw new ModelApiError("provider_kind_invalid");
  const payload: Record<string, unknown> = {
    name,
    provider_kind: input.provider_kind,
    model,
    creation_operation_id: crypto.randomUUID(),
    timeout_seconds: input.timeout_seconds ?? 60,
    max_concurrency: input.max_concurrency ?? 2,
  };
  if (input.base_url?.trim()) payload.base_url = input.base_url.trim();
  if (input.embedding_model?.trim())
    payload.embedding_model = input.embedding_model.trim();
  if (input.embedding_provider_kind)
    payload.embedding_provider_kind = input.embedding_provider_kind;
  if (input.embedding_base_url?.trim())
    payload.embedding_base_url = input.embedding_base_url.trim();
  if (input.embedding_dimensions != null)
    payload.embedding_dimensions = input.embedding_dimensions;
  if (input.context_window != null)
    payload.context_window = input.context_window;
  if (input.disable_thinking !== undefined)
    payload.disable_thinking = input.disable_thinking;
  if (input.api_key !== undefined && input.api_key !== "")
    payload.api_key = input.api_key;

  const { status, data } = await coreRequest(
    "/api/v1/model-profiles",
    "POST",
    payload,
  );
  if (status < 200 || status >= 300) {
    const detail =
      data && typeof data === "object"
        ? String((data as Record<string, unknown>).detail ?? "")
        : "";
    throw new ModelApiError(detail || "create_failed");
  }
  const receipt = parseModelProfileReceipt(data);
  if (!receipt) throw new ModelApiError("model_receipt_invalid");
  return receipt;
}

export async function updateModelProfile(
  profileId: string,
  patch: ModelProfilePatch,
): Promise<ModelProfile> {
  const { status, data } = await coreRequest(
    `/api/v1/model-profiles/${encodeURIComponent(profileId)}`,
    "PATCH",
    patch,
  );
  if (status < 200 || status >= 300) {
    const detail =
      data && typeof data === "object"
        ? String((data as Record<string, unknown>).detail ?? "")
        : "";
    throw new ModelApiError(detail || "update_failed");
  }
  const receipt = parseModelProfileReceipt(data);
  if (!receipt) throw new ModelApiError("model_profile_receipt_invalid");
  return receipt;
}

export async function activateModelProfile(
  profileId: string,
): Promise<ModelProfile> {
  const { status, data } = await coreRequest(
    `/api/v1/model-profiles/${encodeURIComponent(profileId)}/activate`,
    "POST",
  );
  if (status < 200 || status >= 300) {
    const detail =
      data && typeof data === "object"
        ? String((data as Record<string, unknown>).detail ?? "")
        : "";
    throw new ModelApiError(detail || "activate_failed");
  }
  const receipt = parseModelProfileReceipt(data);
  if (!receipt) throw new ModelApiError("model_profile_receipt_invalid");
  return receipt;
}

export async function deleteModelProfile(profileId: string): Promise<void> {
  const { status, data } = await coreRequest(
    `/api/v1/model-profiles/${encodeURIComponent(profileId)}`,
    "DELETE",
  );
  if (status >= 200 && status < 300) return;
  const detail =
    data && typeof data === "object"
      ? String((data as Record<string, unknown>).detail ?? "")
      : "";
  if (detail === "active_model_profile" || status === 409) {
    throw new ModelApiError("active_model_profile");
  }
  if (detail === "model_profile_not_found" || status === 404) {
    throw new ModelApiError("model_profile_not_found");
  }
  throw new ModelApiError(detail || `http_${status}`);
}

export async function getModelCatalog(
  input: ModelCatalogInput,
  signal?: AbortSignal,
): Promise<ModelCatalogResult> {
  const { status, data } = await coreRequest(
    "/api/v1/model-catalog",
    "POST",
    input,
    signal,
  );
  if (status < 200 || status >= 300 || !data || typeof data !== "object") {
    throw new ModelApiError("model_catalog_invalid");
  }
  const row = data as Record<string, unknown>;
  if (
    !MODEL_CATALOG_CODES.includes(row.code as ModelCatalogCode) ||
    typeof row.has_more !== "boolean" ||
    !Array.isArray(row.models) ||
    row.models.length > 200
  ) {
    throw new ModelApiError("model_catalog_invalid");
  }
  const ids = new Set<string>();
  const models = row.models.map((item: unknown) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new ModelApiError("model_catalog_invalid");
    }
    const entry = item as Record<string, unknown>;
    if (
      typeof entry.id !== "string" ||
      !entry.id.trim() ||
      entry.id.length > 256 ||
      typeof entry.display_name !== "string" ||
      !entry.display_name.trim() ||
      entry.display_name.length > 256 ||
      ids.has(entry.id)
    ) {
      throw new ModelApiError("model_catalog_invalid");
    }
    ids.add(entry.id);
    return { id: entry.id, display_name: entry.display_name };
  });
  if (row.code !== "ok" && (models.length || row.has_more)) {
    throw new ModelApiError("model_catalog_invalid");
  }
  return {
    code: row.code as ModelCatalogCode,
    models,
    has_more: row.has_more,
  };
}

interface PendingProbe {
  operationId: string;
  profileId: string;
  inputRevision: number;
}

export function readPendingProbe(): PendingProbe | null {
  try {
    const raw = window.localStorage.getItem(PENDING_PROBE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as {
      schemaVersion?: unknown;
      value?: PendingProbe;
    };
    if (
      parsed.schemaVersion !== 1 ||
      !parsed.value ||
      !UUID.test(parsed.value.operationId) ||
      !UUID.test(parsed.value.profileId) ||
      !Number.isSafeInteger(parsed.value.inputRevision) ||
      parsed.value.inputRevision < 1
    ) {
      return null;
    }
    return parsed.value;
  } catch {
    return null;
  }
}

function savePendingProbe(value: PendingProbe) {
  window.localStorage.setItem(
    PENDING_PROBE_KEY,
    JSON.stringify({ schemaVersion: 1, value }),
  );
}

export function clearPendingProbe(operationId: string) {
  const existing = readPendingProbe();
  if (existing && existing.operationId === operationId) {
    window.localStorage.removeItem(PENDING_PROBE_KEY);
  }
}

let probeInFlight = false;

export async function probeModelProfile(
  profileId: string,
  inputRevision: number,
): Promise<ModelProbeResult> {
  if (probeInFlight) throw new ModelApiError("model_probe_in_flight");
  probeInFlight = true;
  try {
    const existing = readPendingProbe();
    const consumeReceipt = (
      raw: unknown,
      pending: PendingProbe,
    ): ModelProbeResult => {
      if (!raw || typeof raw !== "object")
        throw new ModelApiError("model_probe_operation_unavailable");
      const item = raw as Record<string, unknown>;
      if (item.state === "not_found" || item.state === "pending") {
        throw new ModelApiError("model_probe_operation_pending");
      }
      if (item.state === "failed") {
        clearPendingProbe(pending.operationId);
        throw new ModelApiError("model_probe_operation_stale");
      }
      if (item.state !== "completed" || !item.result) {
        throw new ModelApiError("model_probe_operation_unavailable");
      }
      clearPendingProbe(pending.operationId);
      if (item.stale === true) {
        throw new ModelApiError("model_probe_operation_stale");
      }
      return item.result as ModelProbeResult;
    };

    if (existing) {
      if (existing.profileId !== profileId) {
        throw new ModelApiError("model_probe_other_profile_pending");
      }
      const { status, data } = await coreRequest(
        `/api/v1/model-probe-operations/${encodeURIComponent(existing.operationId)}`,
      );
      if (status < 200 || status >= 300) {
        throw new ModelApiError("model_probe_operation_pending");
      }
      return consumeReceipt(data, existing);
    }

    const operationId = crypto.randomUUID();
    const check = await coreRequest(
      `/api/v1/model-probe-operations/${encodeURIComponent(operationId)}`,
    );
    if (
      check.status < 200 ||
      check.status >= 300 ||
      (check.data as Record<string, unknown> | null)?.state !== "not_found"
    ) {
      throw new ModelApiError("model_probe_capability_unavailable");
    }
    const pending: PendingProbe = { operationId, profileId, inputRevision };
    savePendingProbe(pending);
    let response: { status: number; data: unknown };
    try {
      response = await coreRequest(
        `/api/v1/model-profiles/${encodeURIComponent(profileId)}/probe`,
        "POST",
        {
          operation_id: operationId,
          expected_profile_revision: inputRevision,
        },
      );
    } catch {
      throw new ModelApiError("model_probe_operation_pending");
    }
    if (response.status < 200 || response.status >= 300) {
      throw new ModelApiError("model_probe_operation_pending");
    }
    return consumeReceipt(response.data, pending);
  } finally {
    probeInFlight = false;
  }
}

export async function getSedimentation(
  signal?: AbortSignal,
): Promise<SedimentationState> {
  const { status, data } = await coreRequest(
    "/api/v1/sedimentation",
    "GET",
    undefined,
    signal,
  );
  if (
    status < 200 ||
    status >= 300 ||
    !data ||
    typeof (data as Record<string, unknown>).enabled !== "boolean"
  ) {
    throw new ModelApiError("sedimentation_receipt_invalid");
  }
  return data as SedimentationState;
}

export async function setSedimentation(
  enabled: boolean,
): Promise<SedimentationState> {
  const { status, data } = await coreRequest("/api/v1/sedimentation", "PUT", {
    enabled,
  });
  if (
    status < 200 ||
    status >= 300 ||
    !data ||
    typeof (data as Record<string, unknown>).enabled !== "boolean"
  ) {
    throw new ModelApiError("sedimentation_receipt_invalid");
  }
  return data as SedimentationState;
}
