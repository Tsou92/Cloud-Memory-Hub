import {
  ArrowUpRight,
  Check,
  HardDrive,
  Play,
  RefreshCw,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  BackupControlError,
  type BackupSchedule,
  type BackupStatus,
  type BackupVersion,
  backupErrorMessage,
  buildExportCommand,
  buildRestoreCommand,
  getBackupStatus,
  getBackupVersions,
  requestBackup,
  saveBackupSchedule,
} from "../../api/hub-backups";
import {
  Badge,
  Button,
  CopyBlock,
  formatFullTimestamp,
  Switch,
} from "../../components/ui";

const STATE_META = {
  not_run: { label: "尚未执行备份", tone: "neutral" as const },
  running: { label: "正在生成备份…", tone: "blue" as const },
  completed: { label: "最近一次备份已完成", tone: "emerald" as const },
  failed: { label: "最近一次备份失败", tone: "danger" as const },
  interrupted: { label: "最近一次备份中断", tone: "amber" as const },
};

const INTERVAL_PRESETS = [
  { label: "每 6 小时", minutes: "360" },
  { label: "每 12 小时", minutes: "720" },
  { label: "每天一次", minutes: "1440" },
  { label: "每周一次", minutes: "10080" },
];

function formFor(plan: BackupSchedule) {
  return {
    enabled: plan.enabled,
    interval: String(plan.interval_minutes ?? 1440),
    keep: String(plan.keep ?? 7),
  };
}

export function BackupsSettingsTab() {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [versions, setVersions] = useState<BackupVersion[] | null>(null);
  const [selectedBackupId, setSelectedBackupId] = useState<string>("");
  const [statusError, setStatusError] = useState("");
  const [versionsError, setVersionsError] = useState("");
  const [statusLoading, setStatusLoading] = useState(false);
  const [versionsLoading, setVersionsLoading] = useState(false);

  const [form, setForm] = useState(() =>
    formFor({ schema_version: 1, enabled: false }),
  );
  const [dirty, setDirty] = useState(false);
  const [writing, setWriting] = useState<"schedule" | "request" | null>(null);
  const [unknown, setUnknown] = useState<"schedule" | "request" | null>(null);
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");

  const dirtyRef = useRef(false);
  const writeBusy = useRef(false);
  const statusBusy = useRef(false);
  const versionsBusy = useRef(false);
  const completedSeen = useRef<string | null>(null);

  const refreshVersions = useCallback(async () => {
    if (versionsBusy.current) return;
    versionsBusy.current = true;
    setVersionsLoading(true);
    try {
      const rows = await getBackupVersions();
      setVersions(rows);
      setVersionsError("");
      if (rows.length > 0) {
        setSelectedBackupId((prev) =>
          prev && rows.some((r) => r.backup_id === prev)
            ? prev
            : rows[0].backup_id,
        );
      }
    } catch (error) {
      setVersionsError(backupErrorMessage(error));
    } finally {
      versionsBusy.current = false;
      setVersionsLoading(false);
    }
  }, []);

  const refreshStatus = useCallback(
    async (manual = false) => {
      if (statusBusy.current || writeBusy.current) return;
      statusBusy.current = true;
      setStatusLoading(true);
      try {
        const next = await getBackupStatus();
        setStatus(next);
        setStatusError("");
        if (!dirtyRef.current) setForm(formFor(next.schedule));
        if (manual) {
          setUnknown(null);
          setActionError("");
        }
        if (
          next.execution.state === "completed" &&
          next.execution.last_backup_id &&
          completedSeen.current !== next.execution.last_backup_id
        ) {
          completedSeen.current = next.execution.last_backup_id;
          void refreshVersions();
        }
      } catch (error) {
        setStatusError(backupErrorMessage(error));
      } finally {
        statusBusy.current = false;
        setStatusLoading(false);
      }
    },
    [refreshVersions],
  );

  useEffect(() => {
    void refreshStatus();
    void refreshVersions();
    const timer = window.setInterval(() => {
      void refreshStatus();
    }, 10000);
    return () => window.clearInterval(timer);
  }, [refreshStatus, refreshVersions]);

  function editForm(patch: Partial<typeof form>) {
    dirtyRef.current = true;
    setDirty(true);
    setForm((cur) => ({ ...cur, ...patch }));
  }

  const controlsDisabled =
    !status || Boolean(statusError) || writing !== null || unknown !== null;

  async function handleSaveSchedule(event: FormEvent) {
    event.preventDefault();
    const interval = Number(form.interval);
    const keep = Number(form.keep);
    if (
      !Number.isInteger(interval) ||
      interval < 1 ||
      interval > 525600 ||
      !Number.isInteger(keep) ||
      keep < 1 ||
      keep > 365
    ) {
      setActionError("周期须为 1–525600 分钟整数，保留数量须为 1–365 个整数。");
      return;
    }
    if (controlsDisabled) return;
    writeBusy.current = true;
    setWriting("schedule");
    setMessage("");
    setActionError("");
    try {
      const saved = await saveBackupSchedule({
        enabled: form.enabled,
        interval_minutes: interval,
        keep,
      });
      dirtyRef.current = false;
      setDirty(false);
      setForm(formFor(saved));
      setStatus((cur) =>
        cur ? { ...cur, schedule: saved, next_due_at: null } : cur,
      );
      setMessage("自动备份计划已保存。");
    } catch (error) {
      setActionError(backupErrorMessage(error));
      if (!(error instanceof BackupControlError) || error.outcomeUnknown) {
        setUnknown("schedule");
      }
    } finally {
      writeBusy.current = false;
      setWriting(null);
    }
  }

  async function handleRequestBackup() {
    if (controlsDisabled) return;
    writeBusy.current = true;
    setWriting("request");
    setMessage("");
    setActionError("");
    try {
      const queued = await requestBackup();
      setStatus((cur) =>
        cur ? { ...cur, pending_request: queued.request_id } : cur,
      );
      setMessage("手动备份请求已入队，后台调度器将短暂暂停写入以生成一致快照。");
    } catch (error) {
      setActionError(backupErrorMessage(error));
      if (!(error instanceof BackupControlError) || error.outcomeUnknown) {
        setUnknown("request");
      }
    } finally {
      writeBusy.current = false;
      setWriting(null);
    }
  }

  const stateBadge = status ? STATE_META[status.execution.state] : null;
  const effectiveId = selectedBackupId || "BACKUP_ID";

  return (
    <div className="cmh-stack">
      {message ? (
        <p role="status" className="cmh-alert cmh-alert--emerald">
          {message}
        </p>
      ) : null}
      {actionError ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {actionError}
        </p>
      ) : null}
      {unknown ? (
        <p role="alert" className="cmh-alert cmh-alert--amber">
          上一次{unknown === "request" ? "备份请求" : "计划保存"}结果尚未确认。请先点击「刷新状态」核对服务器实际状态后再继续，系统不会盲目重复提交。
        </p>
      ) : null}

      <div className="cmh-two-col">
        {/* Current Status & Manual Trigger */}
        <section className="cmh-panel cmh-stack" aria-label="备份运行状态">
          <div className="cmh-panel__header">
            <div>
              <h2>当前备份状态</h2>
              <p>备份执行期间服务会短暂暂停，客户端待传队列会自动重试</p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              loading={statusLoading}
              disabled={writing !== null}
              onClick={() => void refreshStatus(true)}
            >
              <RefreshCw size={14} /> 刷新状态
            </Button>
          </div>

          {statusError ? (
            <p role="alert" className="cmh-alert cmh-alert--danger">
              {statusError}
            </p>
          ) : null}

          {status && stateBadge ? (
            <div className="cmh-stack">
              <div>
                <Badge tone={stateBadge.tone} dot>
                  {stateBadge.label}
                </Badge>
              </div>
              <div className="cmh-meta-grid">
                <div>
                  <span>最近成功时间</span>
                  <strong className="cmh-mono">
                    {formatFullTimestamp(status.execution.last_success_at)}
                  </strong>
                </div>
                <div>
                  <span>下次计划时间</span>
                  <strong className="cmh-mono">
                    {status.schedule.enabled
                      ? status.next_due_at
                        ? formatFullTimestamp(status.next_due_at)
                        : "等待调度确认"
                      : "未启用定期备份"}
                  </strong>
                </div>
              </div>
              {status.pending_request ? (
                <p className="cmh-alert cmh-alert--indigo">
                  存在排队中的备份请求，正在等待调度器执行。
                </p>
              ) : null}
              {status.execution.error ? (
                <p className="cmh-alert cmh-alert--danger">
                  最近一次执行遇到错误（{status.execution.error}），请检查 NAS 存储空间后重试。
                </p>
              ) : null}
            </div>
          ) : (
            <p className="cmh-muted">正在读取备份状态…</p>
          )}

          <div className="cmh-inline-actions">
            <Button
              variant="primary"
              loading={writing === "request"}
              disabled={
                controlsDisabled ||
                Boolean(status?.pending_request) ||
                status?.execution.state === "running"
              }
              onClick={() => void handleRequestBackup()}
            >
              <Play size={14} /> 立即请求一次备份
            </Button>
          </div>
        </section>

        {/* Automatic Backup Schedule */}
        <form
          className="cmh-panel cmh-stack"
          aria-label="自动定期备份计划"
          onSubmit={(e) => void handleSaveSchedule(e)}
        >
          <div className="cmh-panel__header">
            <div>
              <h2>自动定期备份</h2>
              <p>按周期自动生成完整三卷快照，并自动轮转经过校验的旧版本</p>
            </div>
            <Switch
              label="启用定期备份"
              checked={form.enabled}
              disabled={controlsDisabled}
              onChange={(enabled) => editForm({ enabled })}
            />
          </div>

          <div className="cmh-agent-pills">
            {INTERVAL_PRESETS.map((p) => (
              <button
                key={p.minutes}
                type="button"
                disabled={controlsDisabled}
                className={`cmh-agent-pill${form.interval === p.minutes ? " is-active" : ""}`}
                onClick={() => editForm({ enabled: true, interval: p.minutes })}
              >
                {p.label}
              </button>
            ))}
          </div>

          <div className="cmh-two-col">
            <div className="cmh-field">
              <label htmlFor="cmh-bk-interval">备份周期（分钟）</label>
              <input
                id="cmh-bk-interval"
                type="number"
                min={1}
                max={525600}
                required
                className="cmh-input cmh-mono"
                value={form.interval}
                disabled={controlsDisabled}
                onChange={(e) => editForm({ interval: e.target.value })}
              />
            </div>
            <div className="cmh-field">
              <label htmlFor="cmh-bk-keep">保留版本份数</label>
              <input
                id="cmh-bk-keep"
                type="number"
                min={1}
                max={365}
                required
                className="cmh-input cmh-mono"
                value={form.keep}
                disabled={controlsDisabled}
                onChange={(e) => editForm({ keep: e.target.value })}
              />
            </div>
          </div>

          <div className="cmh-inline-actions">
            <Button
              type="submit"
              variant="primary"
              loading={writing === "schedule"}
              disabled={controlsDisabled || !dirty}
            >
              保存备份计划
            </Button>
            {dirty ? (
              <span className="cmh-muted">有未保存的修改</span>
            ) : null}
          </div>
        </form>
      </div>

      {/* Backup Versions Table */}
      <section className="cmh-panel" aria-label="备份历史版本清单">
        <div className="cmh-panel__header">
          <div>
            <h2>
              历史备份版本 {versions ? `(${versions.length})` : ""}
            </h2>
            <p>
              点击任意版本即可将其版本号自动填入下方的停服导出与恢复命令中
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            loading={versionsLoading}
            onClick={() => void refreshVersions()}
          >
            <RefreshCw size={14} /> 刷新版本列表
          </Button>
        </div>

        {versionsError ? (
          <p role="alert" className="cmh-alert cmh-alert--danger">
            {versionsError}
          </p>
        ) : null}

        {!versions ? (
          <p className="cmh-muted">正在加载历史版本清单…</p>
        ) : versions.length === 0 ? (
          <div className="cmh-empty cmh-empty--compact">
            <HardDrive size={20} />
            <p>当前尚未生成任何备份版本。可点击上方「立即请求一次备份」生成首份快照。</p>
          </div>
        ) : (
          <div className="cmh-table-wrap">
            <table className="cmh-table">
              <thead>
                <tr>
                  <th>生成时间</th>
                  <th>版本编号 (Backup ID)</th>
                  <th>文件数 / 体积</th>
                  <th>构建标识</th>
                  <th>命令绑定</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((ver) => {
                  const selected = ver.backup_id === selectedBackupId;
                  return (
                    <tr
                      key={ver.backup_id}
                      className={selected ? "is-selected" : ""}
                    >
                      <td className="cmh-mono">
                        {formatFullTimestamp(ver.created_at)}
                      </td>
                      <td>
                        <code className="cmh-mono">{ver.backup_id}</code>
                      </td>
                      <td className="cmh-mono">
                        {ver.file_count} 个 ·{" "}
                        {(ver.total_bytes / 1024 / 1024).toFixed(1)} MiB
                      </td>
                      <td className="cmh-mono">{ver.revision}</td>
                      <td>
                        <Button
                          variant={selected ? "primary" : "secondary"}
                          size="sm"
                          onClick={() => setSelectedBackupId(ver.backup_id)}
                        >
                          {selected ? (
                            <>
                              <Check size={13} /> 已填入命令
                            </>
                          ) : (
                            "填入命令"
                          )}
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Stop-and-Lease Export / Restore Command Generator */}
      <section className="cmh-panel cmh-stack" aria-label="停服导出与恢复">
        <div>
          <h2>离线导出与版本回滚命令</h2>
          <p className="cmh-prose-sm cmh-muted">
            完整导出和历史版本恢复必须先停止在线 Hub 服务并获取独占存储租约，以确保三卷一致性且较新的忘记／删除约束继续生效。导出的归档包含敏感密钥，请作为私有凭证保管。
            {selectedBackupId
              ? ` 当前命令已自动绑定版本：${selectedBackupId}`
              : " 若上方暂无版本，请将 BACKUP_ID 替换为实际版本编号。"}
          </p>
        </div>

        <CopyBlock
          label="在 NAS 安装目录执行：导出为独立归档文件"
          code={buildExportCommand(effectiveId)}
          copyLabel="复制导出命令"
        />

        <CopyBlock
          label="在 NAS 安装目录执行：恢复至所选历史版本"
          code={buildRestoreCommand(effectiveId)}
          copyLabel="复制恢复命令"
        />

        <div>
          <a
            className="cmh-link-inline"
            href="https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/backup-and-recovery.md"
            target="_blank"
            rel="noreferrer"
          >
            查看完整《备份、校验与恢复演练指南》 <ArrowUpRight size={14} />
          </a>
        </div>
      </section>
    </div>
  );
}
