/** Administrator-only metadata controls; no credentials, paths or archive contents. */
export interface BackupSchedule {
  schema_version: 1;
  enabled: boolean;
  interval_minutes?: number;
  keep?: number;
  configured_at?: string;
}

export interface BackupPlan {
  enabled: boolean;
  interval_minutes: number;
  keep: number;
}

export type BackupState =
  | "not_run"
  | "running"
  | "completed"
  | "failed"
  | "interrupted";

export interface BackupStatus {
  schedule: BackupSchedule;
  execution: {
    schema_version: 1;
    state: BackupState;
    last_attempt_at?: string;
    finished_at?: string;
    last_success_at?: string;
    last_backup_id?: string;
    last_request_id?: string;
    last_file_count?: number;
    last_total_bytes?: number;
    error?: string | null;
    retention_error?: string | null;
  };
  next_due_at: string | null;
  pending_request: string | null;
  supervisor_running: "not_checked";
}

export interface BackupVersion {
  backup_id: string;
  state: "unverified";
  created_at: string;
  revision: string;
  file_count: number;
  total_bytes: number;
}

export class BackupControlError extends Error {
  constructor(
    readonly code: string,
    readonly outcomeUnknown = false,
  ) {
    super(code);
  }
}

export function backupErrorMessage(error: unknown): string {
  if (error instanceof BackupControlError) {
    if (error.code === "unauthorized") return "管理员登录已失效，请重新登录。";
    if (error.code === "backup_request_pending")
      return "已有备份请求在等待处理，请刷新状态核对。";
    if (error.code === "invalid_request")
      return "请核对周期和保留版本数量后重试。";
    if (error.code === "unsupported")
      return "当前 Hub 尚不支持备份管理，请升级服务后重试。";
    if (error.code === "backup_storage_in_use")
      return "备份目录正在使用，请稍后刷新版本列表。";
    if (error.code === "backup_catalog_limit")
      return "备份清单超出网页读取上限，请通过 NAS 维护工具检查。";
    if (error.code === "backup_catalog_timeout")
      return "读取备份清单超时，请稍后刷新；本次未更新版本列表。";
    if (error.code === "backup_catalog_invalid")
      return "备份清单格式无法确认，列表未更新；请在 NAS 检查。";
  }
  return "暂时无法确认服务状态。备份期间服务可能短暂停止，请稍后刷新；仍保留上次确认的信息。";
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const versionId = /^cmh-backup-\d{8}T\d{12}Z-[0-9a-f]{32}$/;

function invalid(): never {
  throw new BackupControlError("invalid_response");
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}

function integer(
  value: unknown,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): value is number {
  return (
    Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max
  );
}

function time(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}

function identifier(value: unknown): value is string {
  return typeof value === "string" && uuid.test(value);
}

function schedule(value: unknown): BackupSchedule {
  const row = object(value);
  if (row.schema_version !== 1 || typeof row.enabled !== "boolean") invalid();
  if (
    !row.enabled &&
    row.interval_minutes === undefined &&
    row.keep === undefined &&
    row.configured_at === undefined
  )
    return { schema_version: 1, enabled: false };
  if (
    !integer(row.interval_minutes, 1, 525600) ||
    !integer(row.keep, 1, 365) ||
    !time(row.configured_at)
  )
    invalid();
  return {
    schema_version: 1,
    enabled: row.enabled,
    interval_minutes: row.interval_minutes,
    keep: row.keep,
    configured_at: row.configured_at,
  };
}

async function request(
  path: string,
  method: "GET" | "PUT" | "POST",
  body?: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`/hub/v1/backups/${path}`, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(30000)])
        : AbortSignal.timeout(30000),
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new BackupControlError("unavailable", method !== "GET");
  }
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = object(await response.json()).detail;
    } catch {
      /* Never display a raw server body. */
    }
    if (response.status === 401 || response.status === 403)
      throw new BackupControlError("unauthorized");
    if (response.status === 404) throw new BackupControlError("unsupported");
    if (response.status === 409 && detail === "backup_request_pending")
      throw new BackupControlError("backup_request_pending");
    if (
      (response.status === 422 || response.status === 413) &&
      ["invalid_request", "request_too_large", "json_required"].includes(
        String(detail),
      )
    )
      throw new BackupControlError("invalid_request");
    if (response.status === 503 && detail === "backup_storage_in_use")
      throw new BackupControlError("backup_storage_in_use", method !== "GET");
    if (
      response.status === 503 &&
      typeof detail === "string" &&
      [
        "backup_catalog_limit",
        "backup_catalog_timeout",
        "backup_catalog_invalid",
      ].includes(detail)
    )
      throw new BackupControlError(detail, method !== "GET");
    throw new BackupControlError("unavailable", method !== "GET");
  }
  try {
    if (response.status !== (method === "POST" ? 202 : 200)) invalid();
    return await response.json();
  } catch {
    throw new BackupControlError("invalid_response", method !== "GET");
  }
}

export async function getBackupStatus(
  signal?: AbortSignal,
): Promise<BackupStatus> {
  const row = object(await request("status", "GET", undefined, signal));
  const execution = object(row.execution);
  const states: BackupState[] = [
    "not_run",
    "running",
    "completed",
    "failed",
    "interrupted",
  ];
  if (
    execution.schema_version !== 1 ||
    !states.includes(execution.state as BackupState) ||
    row.supervisor_running !== "not_checked" ||
    !(row.next_due_at === null || time(row.next_due_at)) ||
    !(row.pending_request === null || identifier(row.pending_request))
  )
    invalid();
  const safe: BackupStatus["execution"] = {
    schema_version: 1,
    state: execution.state as BackupState,
  };
  for (const key of [
    "last_attempt_at",
    "finished_at",
    "last_success_at",
  ] as const) {
    if (execution[key] !== undefined) {
      if (!time(execution[key])) invalid();
      safe[key] = execution[key];
    }
  }
  for (const key of ["last_file_count", "last_total_bytes"] as const) {
    if (execution[key] !== undefined) {
      if (!integer(execution[key])) invalid();
      safe[key] = execution[key];
    }
  }
  if (execution.last_backup_id !== undefined) {
    if (
      typeof execution.last_backup_id !== "string" ||
      !versionId.test(execution.last_backup_id)
    )
      invalid();
    safe.last_backup_id = execution.last_backup_id;
  }
  if (execution.last_request_id !== undefined) {
    if (!identifier(execution.last_request_id)) invalid();
    safe.last_request_id = execution.last_request_id;
  }
  const codes = [
    null,
    "backup_create_failed",
    "backup_service_stop_failed",
    "backup_interrupted",
    "backup_retention_failed",
  ];
  for (const key of ["error", "retention_error"] as const) {
    if (execution[key] !== undefined) {
      if (!codes.includes(execution[key] as string | null)) invalid();
      safe[key] = execution[key] as string | null;
    }
  }
  return {
    schedule: schedule(row.schedule),
    execution: safe,
    next_due_at: row.next_due_at,
    pending_request: row.pending_request,
    supervisor_running: "not_checked",
  };
}

export async function getBackupVersions(
  signal?: AbortSignal,
): Promise<BackupVersion[]> {
  const row = object(await request("versions", "GET", undefined, signal));
  if (!Array.isArray(row.versions) || row.versions.length > 365) invalid();
  const ids = new Set<string>();
  return row.versions.map((raw) => {
    const item = object(raw);
    if (
      typeof item.backup_id !== "string" ||
      !versionId.test(item.backup_id) ||
      ids.has(item.backup_id)
    )
      invalid();
    ids.add(item.backup_id);
    if (
      item.state !== "unverified" ||
      !time(item.created_at) ||
      typeof item.revision !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/.test(item.revision) ||
      !integer(item.file_count) ||
      !integer(item.total_bytes)
    )
      invalid();
    return {
      backup_id: item.backup_id,
      state: "unverified",
      created_at: item.created_at,
      revision: item.revision,
      file_count: item.file_count,
      total_bytes: item.total_bytes,
    };
  });
}

export async function saveBackupSchedule(
  plan: BackupPlan,
): Promise<BackupSchedule> {
  try {
    const saved = schedule(await request("schedule", "PUT", plan));
    if (
      saved.enabled !== plan.enabled ||
      saved.interval_minutes !== plan.interval_minutes ||
      saved.keep !== plan.keep
    )
      invalid();
    return saved;
  } catch (error) {
    if (
      error instanceof BackupControlError &&
      error.code === "invalid_response"
    )
      throw new BackupControlError("invalid_response", true);
    throw error;
  }
}

export async function requestBackup(): Promise<{
  request_id: string;
  requested_at: string;
}> {
  let row: Record<string, unknown>;
  try {
    row = object(await request("request", "POST", {}));
  } catch (error) {
    if (
      error instanceof BackupControlError &&
      error.code === "invalid_response"
    )
      throw new BackupControlError("invalid_response", true);
    throw error;
  }
  if (
    row.status !== "queued" ||
    row.schema_version !== 1 ||
    !identifier(row.request_id) ||
    !time(row.requested_at)
  )
    throw new BackupControlError("invalid_response", true);
  return { request_id: row.request_id, requested_at: row.requested_at };
}

export function buildExportCommand(backupId = "BACKUP_ID"): string {
  return `docker compose stop hub && docker compose exec -T hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_maintenance export --version ${backupId} --output /backups/private.cmh.tar`;
}

export function buildRestoreCommand(backupId = "BACKUP_ID"): string {
  return `docker compose stop hub && docker compose exec -T hub /opt/cmh/venv/bin/python -m cloud_memory_hub.backup_maintenance restore --writers-stopped --version ${backupId}`;
}
