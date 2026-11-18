/** Administrator onboarding over existing Hub identities; never persist credentials. */
import type { HubProject, HubSource } from "./hub-api";

export type Permission =
  | "events:write"
  | "memory:read"
  | "history:read"
  | "sync:read";

export interface DeviceGrant {
  scope_id: string;
  permissions: Permission[];
}

export interface PairDevice {
  device_id: string;
  display_name: string;
  revision: number;
  status: "active" | "revoked";
  grants: DeviceGrant[];
  created_at: string;
}

export interface PairSource {
  source_id: string;
  device_id: string;
  scope_id: string;
  adapter_id: string;
  display_name: string;
  revision: number;
  status: "active" | "revoked";
}

export interface IssuedDevice {
  device: PairDevice;
  credential: string;
}

export interface PairingFile {
  schema_version: 1;
  kind: "cloud-memory-hub-pairing";
  origin: string;
  instance_id: string;
  device_id: string;
  scope_id: string;
  source_id: string;
  credential: string;
}

export type PairPhase =
  | "create_unknown"
  | "device"
  | "grants_unknown"
  | "source_unknown"
  | "choose_source"
  | "verify"
  | "ready"
  | "rotate_unknown";

export interface PairProgress {
  version: 1;
  instance: string;
  scope: string;
  phase: PairPhase;
  device_id: string | null;
  source_id: string | null;
  source_creation_unknown: boolean;
}

export const requiredPermissions: Permission[] = [
  "events:write",
  "memory:read",
  "history:read",
];

export const agentAdapter = "cloud-memory-hub.agent";

const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const phases = [
  "create_unknown",
  "device",
  "grants_unknown",
  "source_unknown",
  "choose_source",
  "verify",
  "ready",
  "rotate_unknown",
];

const messages: Record<string, string> = {
  revision_conflict: "设备或来源已被其他操作修改，请刷新列表核对后再继续。",
  device_revoked: "此设备已撤销，请选择其他设备或新增独立设备。",
  device_not_active: "此设备不可用，请刷新设备列表。",
  invalid_request: "填写的名称或配对信息不符合要求，请检查后重试。",
  administrator_required: "管理员登录已失效，请重新登录。",
  authentication_required: "管理员登录已失效，请重新登录。",
  invalid_device_credential: "设备凭据已失效或被轮换，需要明确重新签发。",
  source_not_allowed: "该设备目前没有此项目来源的访问权限，请核对授权。",
  secure_origin_required:
    "请使用 HTTPS 管理地址（或本机 localhost）生成可导入的配对文件。",
  unknown_scope: "当前项目已不存在，请刷新项目列表。",
};

export class PairingError extends Error {
  constructor(
    readonly code: string,
    readonly rejectedBeforeWrite = false,
  ) {
    super(
      messages[code] ??
        "请求结果尚未确认，请刷新列表核对当前阶段，不要重复创建对象。",
    );
  }
}

export function rejectedBeforeWrite(reason: unknown): boolean {
  return reason instanceof PairingError && reason.rejectedBeforeWrite;
}

function invalid(): never {
  throw new PairingError("invalid_response");
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}

function identity(value: unknown): value is string {
  return typeof value === "string" && uuid.test(value);
}

function scope(value: unknown): value is string {
  return value === "global" || identity(value);
}

function revision(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function state(value: unknown): boolean {
  return value === "active" || value === "revoked";
}

function device(value: unknown): PairDevice {
  const row = object(value);
  if (
    !identity(row.device_id) ||
    typeof row.display_name !== "string" ||
    !revision(row.revision) ||
    !state(row.status) ||
    typeof row.created_at !== "string" ||
    !Array.isArray(row.grants) ||
    row.grants.length > 256
  )
    invalid();
  const scopes = new Set<string>();
  for (const raw of row.grants) {
    const grant = object(raw);
    if (
      !scope(grant.scope_id) ||
      scopes.has(grant.scope_id) ||
      !Array.isArray(grant.permissions) ||
      !grant.permissions.length ||
      new Set(grant.permissions).size !== grant.permissions.length ||
      !grant.permissions.every((p) =>
        ["events:write", "memory:read", "history:read", "sync:read"].includes(
          String(p),
        ),
      )
    )
      invalid();
    scopes.add(grant.scope_id);
  }
  return row as unknown as PairDevice;
}

function source(value: unknown): PairSource {
  const row = object(value);
  if (
    !identity(row.source_id) ||
    !identity(row.device_id) ||
    !scope(row.scope_id) ||
    typeof row.display_name !== "string" ||
    typeof row.adapter_id !== "string" ||
    !revision(row.revision) ||
    !state(row.status)
  )
    invalid();
  return row as unknown as PairSource;
}

function issued(value: unknown): IssuedDevice {
  const row = object(value);
  const record = device(row.device);
  if (
    record.status !== "active" ||
    typeof row.credential !== "string" ||
    !new RegExp(`^cmhdev_v1_${record.device_id}_[A-Za-z0-9_-]{43}$`).test(
      row.credential,
    )
  )
    invalid();
  return { device: record, credential: row.credential };
}

async function request(
  path: string,
  method: string,
  body?: unknown,
  signal?: AbortSignal,
  credential?: string,
): Promise<unknown> {
  const response = await fetch(`/hub/v1/${path}`, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(credential ? { Authorization: `Bearer ${credential}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let code: unknown;
    try {
      code = object(await response.json()).detail;
    } catch {
      /* No response body is exposed. */
    }
    throw new PairingError(
      typeof code === "string" && code in messages ? code : "request_failed",
      (response.status === 403 && code === "administrator_required") ||
        (response.status === 422 &&
          (code === "invalid_request" || code === "unknown_scope")) ||
        (response.status === 409 && code === "device_not_active"),
    );
  }
  try {
    const value = await response.json();
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    return value;
  } catch {
    return invalid();
  }
}

export function validLabel(value: string): string {
  const text = value.normalize("NFKC").trim();
  if (!text || [...text].length > 80 || /[\p{Cc}\p{Cf}]/u.test(text))
    throw new PairingError("invalid_request");
  return text;
}

export async function createProject(
  name: string,
  signal?: AbortSignal,
): Promise<HubProject> {
  const row = object(
    await request(
      "projects",
      "POST",
      { display_name: validLabel(name) },
      signal,
    ),
  );
  if (!identity(row.project_id) || typeof row.display_name !== "string")
    invalid();
  return { scope_id: row.project_id, display_name: row.display_name };
}

export async function getDevices(signal?: AbortSignal): Promise<PairDevice[]> {
  const row = object(await request("devices", "GET", undefined, signal));
  if (!Array.isArray(row.devices)) invalid();
  const result = row.devices.map(device);
  if (new Set(result.map((r) => r.device_id)).size !== result.length) invalid();
  return result;
}

export async function getSources(signal?: AbortSignal): Promise<PairSource[]> {
  const row = object(await request("sources", "GET", undefined, signal));
  if (!Array.isArray(row.sources)) invalid();
  const result = row.sources.map(source);
  if (new Set(result.map((r) => r.source_id)).size !== result.length) invalid();
  return result;
}

export async function createDevice(name: string, signal?: AbortSignal) {
  return issued(
    await request(
      "devices",
      "POST",
      { display_name: validLabel(name) },
      signal,
    ),
  );
}

export async function rotateDevice(current: PairDevice, signal?: AbortSignal) {
  const result = issued(
    await request(
      `devices/${current.device_id}/rotate`,
      "POST",
      { expected_revision: current.revision },
      signal,
    ),
  );
  if (
    result.device.device_id !== current.device_id ||
    result.device.revision !== current.revision + 1
  )
    invalid();
  return result;
}

export function hasPermissions(current: PairDevice, project: string): boolean {
  return (
    current.status === "active" &&
    requiredPermissions.every((permission) =>
      current.grants.some(
        (g) => g.scope_id === project && g.permissions.includes(permission),
      ),
    )
  );
}

export function effectiveHubSources(
  sources: HubSource[],
  devices: PairDevice[],
  scopeId: string,
): HubSource[] {
  const allowedDevices = new Set(
    devices
      .filter(
        (d) =>
          d.status === "active" &&
          d.grants.some(
            (g) =>
              g.scope_id === scopeId &&
              g.permissions.some((p) =>
                ["events:write", "memory:read", "history:read"].includes(p),
              ),
          ),
      )
      .map((d) => d.device_id),
  );
  return sources.filter(
    (s) =>
      s.scope_id === scopeId &&
      s.status === "active" &&
      s.device_status === "active" &&
      allowedDevices.has(s.device_id),
  );
}

export async function grantProject(
  current: PairDevice,
  project: string,
  signal?: AbortSignal,
) {
  const grants = current.grants.map((g) => ({
    scope_id: g.scope_id,
    permissions: [...g.permissions],
  }));
  const existing = grants.find((g) => g.scope_id === project);
  if (existing)
    existing.permissions = [
      ...new Set([...existing.permissions, ...requiredPermissions]),
    ];
  else
    grants.push({ scope_id: project, permissions: [...requiredPermissions] });
  const result = device(
    await request(
      `devices/${current.device_id}/grants`,
      "PUT",
      { expected_revision: current.revision, grants },
      signal,
    ),
  );
  if (
    result.device_id !== current.device_id ||
    result.revision !== current.revision + 1 ||
    !hasPermissions(result, project) ||
    grants.some(
      (g) =>
        !result.grants.some(
          (next) =>
            next.scope_id === g.scope_id &&
            next.permissions.length === g.permissions.length &&
            g.permissions.every((p) => next.permissions.includes(p)),
        ),
    )
  )
    invalid();
  return result;
}

export async function createSource(
  current: PairDevice,
  project: string,
  signal?: AbortSignal,
) {
  const result = source(
    await request(
      "sources",
      "POST",
      {
        display_name: "Cloud Memory Hub Agent",
        device_id: current.device_id,
        scope_id: project,
        adapter_id: agentAdapter,
      },
      signal,
    ),
  );
  if (
    result.device_id !== current.device_id ||
    result.scope_id !== project ||
    result.adapter_id !== agentAdapter ||
    result.status !== "active"
  )
    invalid();
  return result;
}

export async function revokeDevice(current: PairDevice, signal?: AbortSignal) {
  const result = device(
    await request(
      `devices/${current.device_id}/revoke`,
      "POST",
      { expected_revision: current.revision },
      signal,
    ),
  );
  if (
    result.device_id !== current.device_id ||
    result.status !== "revoked" ||
    result.revision !== current.revision + 1
  )
    invalid();
  return result;
}

export async function revokeSource(current: PairSource, signal?: AbortSignal) {
  const result = source(
    await request(
      `sources/${current.source_id}/revoke`,
      "POST",
      { expected_revision: current.revision },
      signal,
    ),
  );
  if (
    result.source_id !== current.source_id ||
    result.device_id !== current.device_id ||
    result.scope_id !== current.scope_id ||
    result.status !== "revoked" ||
    result.revision !== current.revision + 1
  )
    invalid();
  return result;
}

export function pairingOrigin(): string {
  const url = new URL(window.location.origin);
  if (
    url.protocol !== "https:" &&
    !(
      url.protocol === "http:" &&
      ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
    )
  )
    throw new PairingError("secure_origin_required");
  return url.origin;
}

export async function checkPairing(
  instance: string,
  project: string,
  current: IssuedDevice,
  selected: PairSource,
  signal?: AbortSignal,
): Promise<PairingFile> {
  const origin = pairingOrigin();
  if (
    !hasPermissions(current.device, project) ||
    selected.device_id !== current.device.device_id ||
    selected.scope_id !== project ||
    selected.status !== "active" ||
    selected.adapter_id !== agentAdapter
  )
    invalid();
  const row = object(
    await request(
      `sources/${selected.source_id}/access`,
      "GET",
      undefined,
      signal,
      current.credential,
    ),
  );
  if (
    row.protocol_version !== 1 ||
    row.allowed !== true ||
    row.instance_id !== instance ||
    row.device_id !== current.device.device_id ||
    row.scope_id !== project ||
    row.source_id !== selected.source_id ||
    row.device_revision !== current.device.revision ||
    row.source_revision !== selected.revision
  )
    invalid();
  return {
    schema_version: 1,
    kind: "cloud-memory-hub-pairing",
    origin,
    instance_id: instance,
    device_id: current.device.device_id,
    scope_id: project,
    source_id: selected.source_id,
    credential: current.credential,
  };
}

export function downloadPairing(pairing: PairingFile) {
  const url = URL.createObjectURL(
    new Blob([`${JSON.stringify(pairing, null, 2)}\n`], {
      type: "application/json",
    }),
  );
  const revokeURL = URL.revokeObjectURL.bind(URL);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "cloud-memory-hub-pairing.json";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => revokeURL(url), 1000);
  }
}

export const progressKey = (instance: string, project: string) =>
  `cmh-pairing:${instance}:${project}`;

export function readProgress(
  instance: string,
  project: string,
): PairProgress | null {
  const raw = sessionStorage.getItem(progressKey(instance, project));
  if (!raw) return null;
  const value = object(JSON.parse(raw));
  if (
    typeof value.source_creation_unknown !== "boolean" ||
    value.version !== 1 ||
    value.instance !== instance ||
    value.scope !== project ||
    !phases.includes(String(value.phase)) ||
    !(value.device_id === null || identity(value.device_id)) ||
    !(value.source_id === null || identity(value.source_id))
  )
    invalid();
  return {
    version: 1,
    instance,
    scope: project,
    phase: value.phase as PairPhase,
    device_id: value.device_id as string | null,
    source_id: value.source_id as string | null,
    source_creation_unknown: value.source_creation_unknown,
  };
}

export function saveProgress(value: PairProgress) {
  sessionStorage.setItem(
    progressKey(value.instance, value.scope),
    JSON.stringify({
      version: 1,
      instance: value.instance,
      scope: value.scope,
      phase: value.phase,
      device_id: value.device_id,
      source_id: value.source_id,
      source_creation_unknown: value.source_creation_unknown,
    }),
  );
}

export function pairingMessage(error: unknown): string {
  return error instanceof PairingError
    ? error.message
    : "连接中断，结果尚未确认。请刷新列表核对当前阶段。";
}
