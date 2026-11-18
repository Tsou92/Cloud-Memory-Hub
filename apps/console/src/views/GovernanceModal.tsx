import { AlertTriangle, Trash2, EyeOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  appendPreview,
  clearPending,
  executeForget,
  type ForgetPreview,
  type ForgetResult,
  type GovernanceAction,
  GovernanceError,
  type PendingForget,
  pendingKey,
  previewForget,
  readPending,
} from "../api/hub-governance";
import { Badge, Button, Modal } from "../components/ui";

const REASON_LABELS: Record<string, string> = {
  selected: "所选记忆",
  evolution: "关联历史版本",
  source: "来源原文",
  derived: "同源派生记忆",
};

export function GovernanceModal({
  instance,
  scope,
  recordId,
  onClose,
  onCompleted,
}: {
  instance: string;
  scope: string;
  recordId: string | null;
  onClose(): void;
  onCompleted(result: ForgetResult, action: GovernanceAction): void;
}) {
  const [action, setAction] = useState<GovernanceAction>("forget");
  const [preview, setPreview] = useState<ForgetPreview | null>(null);
  const [pending, setPending] = useState<PendingForget | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [storageError, setStorageError] = useState(false);
  const [changed, setChanged] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const target = useRef<string | null>(recordId);

  const load = useCallback(
    async (previous?: ForgetPreview) => {
      const selectedId = target.current;
      if (!selectedId) return;
      controller.current?.abort();
      const current = new AbortController();
      controller.current = current;
      setBusy(true);
      setError("");
      setConfirmed(false);
      if (!previous) setPreview(null);
      try {
        const page = await previewForget(
          scope,
          selectedId,
          previous?.next_offset ?? 0,
          previous?.view_digest,
          current.signal,
          action,
        );
        if (alive.current && !current.signal.aborted) {
          setPreview(previous ? appendPreview(previous, page) : page);
        }
      } catch (reason) {
        if (alive.current && !current.signal.aborted) {
          setError(
            reason instanceof GovernanceError
              ? reason.message
              : "无法读取影响范围，请检查连接后重新预览。",
          );
          setPreview(null);
        }
      } finally {
        if (alive.current && !current.signal.aborted) setBusy(false);
      }
    },
    [scope, action],
  );

  useEffect(() => {
    alive.current = true;
    try {
      const storedForget = readPending(instance, scope, "forget");
      const storedPurge = readPending(instance, scope, "purge");
      const stored =
        (action === "purge" ? storedPurge : storedForget) ??
        storedForget ??
        storedPurge;
      if (stored && stored.request.action !== action) {
        setAction(stored.request.action);
      }
      target.current = stored?.request.record_ids[0] ?? recordId;
      setPending(stored);
      if (target.current && !stored) void load();
    } catch {
      setStorageError(true);
    }
    return () => {
      alive.current = false;
      controller.current?.abort();
    };
  }, [instance, scope, recordId, action, load]);

  const complete = Boolean(
    preview &&
      preview.next_offset === null &&
      preview.items.length === preview.total &&
      preview.items.some((item) => item.record_id === target.current),
  );
  const unsupported = Boolean(
    preview?.items.some((item) =>
      /^(cmhp:|cmht:|cmhc:|cmhv:)/.test(item.record_id),
    ),
  );
  const eligible = Boolean(
    complete && !unsupported && preview?.unresolved_origin_count === 0,
  );
  const canSubmit =
    action === "purge"
      ? eligible && preview?.action === "purge" && confirmed && !busy
      : eligible && confirmed && !busy;

  async function execute(existing?: PendingForget) {
    if (busy || storageError) return;
    let operation = existing;
    if (!operation) {
      if (!preview || !target.current || !confirmed || !eligible) return;
      operation = {
        version: 1,
        instance,
        request: {
          scope_id: scope,
          action,
          record_ids: [target.current],
          expected_view: preview.view_digest,
          operation_id: crypto.randomUUID(),
        },
        total: preview.total,
        memoryCount: preview.memory_count,
        rawCount: preview.raw_count,
      };
      try {
        sessionStorage.setItem(
          pendingKey(instance, scope, action),
          JSON.stringify(operation),
        );
      } catch {
        setStorageError(true);
        return;
      }
      setPending(operation);
    }
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setBusy(true);
    setError("");
    setChanged(false);
    try {
      const result = await executeForget(operation, current.signal);
      if (!alive.current || current.signal.aborted) return;
      try {
        clearPending(instance, scope, operation.request.action);
      } catch {
        setStorageError(true);
        return;
      }
      setPending(null);
      setPreview(null);
      onCompleted(result, operation.request.action);
    } catch (reason) {
      if (alive.current && !current.signal.aborted) {
        setError(
          reason instanceof GovernanceError
            ? reason.message
            : "连接中断，执行结果尚未确认。请核对原操作，不要重复创建请求。",
        );
        setChanged(
          reason instanceof GovernanceError &&
            reason.code === "governance_preview_changed",
        );
      }
    } finally {
      if (alive.current && !current.signal.aborted) setBusy(false);
    }
  }

  function restartAfterChange() {
    if (!changed) return;
    try {
      clearPending(instance, scope, action);
    } catch {
      setStorageError(true);
      return;
    }
    setPending(null);
    setChanged(false);
    setConfirmed(false);
    void load();
  }

  const open = Boolean(recordId || pending || storageError);
  if (!open) return null;

  return (
    <Modal
      open={open}
      width="lg"
      busy={busy}
      title={
        pending
          ? pending.request.action === "purge"
            ? "核对未决的彻底删除操作"
            : "核对未决的忘记操作"
          : "记忆治理与影响范围核对"
      }
      subtitle="执行前先核对受影响的关联记忆与原始记录；已经发送给客户端的内容无法撤回。"
      onClose={onClose}
    >
      <div className="cmh-stack">
        {/* Action Selector Cards */}
        <div className="cmh-gov-modes" role="radiogroup" aria-label="处理方式">
          <label
            className={`cmh-gov-mode${action === "forget" ? " is-selected" : ""}`}
          >
            <input
              type="radio"
              name="governance-action"
              checked={action === "forget"}
              disabled={busy || Boolean(pending)}
              onChange={() => {
                setAction("forget");
                setConfirmed(false);
              }}
            />
            <EyeOff size={18} />
            <div>
              <strong>忘记（停止自动召回）</strong>
              <p>停止自动调用与再次自动提炼；原始历史仍可按记录 ID 主动查阅。</p>
            </div>
          </label>

          <label
            className={`cmh-gov-mode cmh-gov-mode--danger${action === "purge" ? " is-selected" : ""}`}
          >
            <input
              type="radio"
              name="governance-action"
              checked={action === "purge"}
              disabled={busy || Boolean(pending)}
              onChange={() => {
                setAction("purge");
                setConfirmed(false);
              }}
            />
            <Trash2 size={18} />
            <div>
              <strong>彻底删除（清除正文）</strong>
              <p>在服务能完整证明来源关系的范围内物理清除相关正文，不可恢复。</p>
            </div>
          </label>
        </div>

        {storageError ? (
          <p role="alert" className="cmh-alert cmh-alert--danger">
            浏览器无法保存或读取操作回执。当前未发起新操作，请恢复此站点的会话存储权限后再试。
          </p>
        ) : pending ? (
          <div className="cmh-stack">
            <div className="cmh-alert cmh-alert--amber">
              <AlertTriangle size={16} />
              <div>
                <strong>存在尚未确认结果的治理请求</strong>
                <p>
                  原操作涉及 {pending.memoryCount} 条记忆与 {pending.rawCount} 条原文（操作编号{" "}
                  <code className="cmh-mono">{pending.request.operation_id}</code>）。系统将按原幂等编号继续核对。
                </p>
              </div>
            </div>
            <div className="cmh-inline-actions">
              <Button
                variant={pending.request.action === "purge" ? "danger" : "primary"}
                loading={busy}
                onClick={() => void execute(pending)}
              >
                核对并继续原操作
              </Button>
              {changed ? (
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={restartAfterChange}
                >
                  重新预览最新影响范围
                </Button>
              ) : null}
            </div>
          </div>
        ) : (
          <>
            {busy && !preview ? (
              <p role="status" className="cmh-muted">
                正在计算关联版本、来源原文与派生影响范围…
              </p>
            ) : null}

            {preview ? (
              <div className="cmh-stack">
                <div className="cmh-gov-summary">
                  <span>
                    共影响 <strong>{preview.memory_count}</strong> 条记忆、
                    <strong>{preview.raw_count}</strong> 条原文
                  </span>
                  <span className="cmh-mono">
                    已加载 {preview.items.length} / {preview.total} 项
                  </span>
                </div>

                <ul className="cmh-impact-list">
                  {preview.items.map((item) => (
                    <li key={item.record_id} className="cmh-impact-item">
                      <div className="cmh-impact-item__head">
                        <Badge tone={item.kind === "memory" ? "indigo" : "neutral"}>
                          {item.kind === "memory" ? "记忆" : "原文"}
                        </Badge>
                        <span className="cmh-impact-item__reasons">
                          {item.reasons
                            .map((r) => REASON_LABELS[r] ?? r)
                            .join(" · ")}
                        </span>
                        <span className="cmh-impact-item__effect">
                          {action === "purge"
                            ? "清除正文"
                            : item.proposed_effect === "retain_for_explicit_read"
                              ? "保留供按 ID 查阅"
                              : "停止自动使用"}
                        </span>
                      </div>
                      <p>
                        {item.content}
                        {item.content_truncated ? "…（预览已截短）" : ""}
                      </p>
                    </li>
                  ))}
                </ul>

                {preview.next_offset !== null ? (
                  <Button
                    variant="secondary"
                    loading={busy}
                    onClick={() => void load(preview)}
                  >
                    加载剩余影响范围（需查看全部项后方可确认）
                  </Button>
                ) : null}

                {unsupported ? (
                  <p role="alert" className="cmh-alert cmh-alert--danger">
                    影响范围内包含暂不支持治理的工作或编程投影记录，当前不能执行。
                  </p>
                ) : null}

                {preview.unresolved_origin_count > 0 ? (
                  <p role="alert" className="cmh-alert cmh-alert--danger">
                    有 {preview.unresolved_origin_count} 条记忆缺少完整来源证明，服务拒绝不完整执行。
                  </p>
                ) : null}

                <label className="cmh-checkbox">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={!eligible || busy}
                    onChange={(event) => setConfirmed(event.target.checked)}
                  />
                  <span>
                    {action === "purge"
                      ? "我已核对全部影响范围，确认彻底清除这些记录的正文且不可恢复。"
                      : "我已核对全部影响范围，确认停止自动召回与再次提炼这些内容。"}
                  </span>
                </label>

                <div className="cmh-modal__inline-footer">
                  <Button variant="ghost" disabled={busy} onClick={onClose}>
                    取消
                  </Button>
                  <Button
                    variant={action === "purge" ? "danger" : "primary"}
                    loading={busy}
                    disabled={!canSubmit}
                    onClick={() => void execute()}
                  >
                    {action === "purge" ? "确认彻底删除" : "确认忘记"}
                  </Button>
                </div>
              </div>
            ) : !busy ? (
              <div className="cmh-inline-actions">
                <Button variant="secondary" onClick={() => void load()}>
                  重新预览影响范围
                </Button>
                <Button variant="ghost" onClick={onClose}>
                  取消
                </Button>
              </div>
            ) : null}
          </>
        )}

        {error ? (
          <p role="alert" className="cmh-alert cmh-alert--danger">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
