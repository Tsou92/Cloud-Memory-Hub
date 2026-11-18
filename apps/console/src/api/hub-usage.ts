export interface HubUsageCost {
  currency: string;
  total: number;
  priced_requests: number;
  unpriced_models: string[];
}

export interface HubUsageReport {
  request_count: number;
  succeeded: number;
  failed: number;
  in_flight: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost?: HubUsageCost | null;
}

function integer(value: unknown): number | null {
  return Number.isSafeInteger(value) && Number(value) >= 0
    ? Number(value)
    : null;
}

function parseCost(value: unknown): HubUsageCost | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (row.available !== true) return null;
  const total =
    typeof row.total === "number" && Number.isFinite(row.total) && row.total >= 0
      ? row.total
      : null;
  if (total === null) return null;
  const models = Array.isArray(row.unpriced_models)
    ? row.unpriced_models.filter((item): item is string => typeof item === "string")
    : [];
  return {
    currency: typeof row.currency === "string" ? row.currency : "USD",
    total,
    priced_requests: integer(row.priced_requests) ?? 0,
    unpriced_models: models,
  };
}

export function parseUsage(value: unknown): HubUsageReport {
  const row = value as Record<string, unknown>;
  const report = {
    request_count: integer(row?.request_count),
    succeeded: integer(row?.succeeded),
    failed: integer(row?.failed),
    in_flight: integer(row?.in_flight),
    prompt_tokens: integer(row?.prompt_tokens),
    completion_tokens: integer(row?.completion_tokens),
    total_tokens: integer(row?.total_tokens),
  };
  if (Object.values(report).some((item) => item === null))
    throw new Error("usage_response_invalid");
  const parsed = report as unknown as HubUsageReport;
  const estimated = parseCost(row?.cost);
  return estimated ? { ...parsed, cost: estimated } : parsed;
}

export async function getHubUsage(signal?: AbortSignal): Promise<HubUsageReport> {
  const response = await fetch("/hub/v1/console/usage", {
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error("usage_unavailable");
  return parseUsage(await response.json());
}
