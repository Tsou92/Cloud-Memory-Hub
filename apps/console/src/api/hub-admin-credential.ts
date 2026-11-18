/** Hub credential changes never persist form values or include them in errors. */
export class CredentialChangeError extends Error {
  constructor(
    readonly code: string,
    readonly outcomeUnknown = false,
  ) {
    super(code);
  }
}

export function generateAdminCredential(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
}

export async function changeAdminCredential(
  current: string,
  next: string,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/hub/v1/admin/credential", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_credential: current,
        new_credential: next,
      }),
      signal: AbortSignal.timeout(30000),
    });
  } catch {
    throw new CredentialChangeError("connection_lost", true);
  }
  if (response.status === 401)
    throw new CredentialChangeError("authentication_required");
  if (response.status === 413)
    throw new CredentialChangeError("request_too_large");
  if (response.status === 429) throw new CredentialChangeError("rate_limited");
  if (response.status === 503)
    throw new CredentialChangeError("service_unavailable", true);
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new CredentialChangeError(
      "unconfirmed_response",
      response.ok || response.status >= 500,
    );
  }
  const body =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  if (response.status === 403) {
    throw new CredentialChangeError(
      body?.detail === "current_credential_incorrect"
        ? "current_credential_incorrect"
        : "request_refused",
    );
  }
  if (response.status === 422)
    throw new CredentialChangeError("invalid_new_credential");
  if (
    !response.ok ||
    body?.schema_version !== 1 ||
    body?.state !== "credential_changed" ||
    body?.login_required !== true
  ) {
    throw new CredentialChangeError("unconfirmed_response", true);
  }
}

export function credentialChangeMessage(error: unknown): string {
  if (error instanceof CredentialChangeError) {
    if (error.outcomeUnknown)
      return "无法确认是否修改成功。请先保存新凭证，再用新凭证重新登录；若不通过，再使用原凭证。";
    if (error.code === "current_credential_incorrect")
      return "当前凭证不正确，请重新输入。";
    if (error.code === "authentication_required")
      return "管理员登录已失效，请重新登录。";
    if (error.code === "invalid_new_credential")
      return "请填写不同于当前凭证的新凭证。";
    if (error.code === "request_too_large")
      return "输入内容过长，请缩短后重试。";
    if (error.code === "rate_limited") return "尝试次数过多，请稍后再试。";
    if (error.code === "service_unavailable")
      return "暂时无法修改凭证，请稍后再试。";
  }
  return "请求未通过，请重新登录后再试。";
}
