import {
  Bot,
  CheckCircle2,
  Clock,
  Download,
  FolderPlus,
  KeyRound,
  Link2,
  Monitor,
  Plus,
  RefreshCw,
  ShieldOff,
  Terminal,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getHubSummary,
  type HubProject,
} from "../api/hub-api";
import {
  type Agent,
  agents,
  createInvitation,
  type Invitation,
  InvitationError,
  type InvitationProgress,
  type InvitationStatus,
  invitationInstructions,
  invitationKey,
  invitationMessage,
  readInvitation,
  readInvitationProgress,
} from "../api/hub-invitation";
import {
  agentAdapter,
  checkPairing,
  createDevice,
  createSource,
  downloadPairing,
  getDevices,
  getSources,
  grantProject,
  hasPermissions,
  type IssuedDevice,
  type PairDevice,
  type PairingFile,
  type PairProgress,
  type PairSource,
  pairingMessage,
  pairingOrigin,
  progressKey,
  readProgress,
  revokeDevice,
  revokeSource,
  rotateDevice,
  saveProgress,
  validLabel,
} from "../api/hub-pairing";
import {
  Badge,
  Button,
  CopyBlock,
  EmptyState,
  formatFullTimestamp,
  Modal,
} from "../components/ui";

function formatUploadStatus(seconds: number | null | undefined): string {
  if (seconds === undefined) return "最近上传：暂时无法确认";
  if (seconds === null) return "最近上传：尚无记录";
  return `最近上传：${formatFullTimestamp(seconds)}`;
}

export function DevicesView({
  instance,
  project,
  onOpenCreateProject,
}: {
  instance: string;
  project: HubProject | null;
  onOpenCreateProject(): void;
}) {
  const scope = project?.scope_id ?? "";
  const isGlobal = scope === "global";
  const hasValidProject = Boolean(project && !isGlobal);

  const [devices, setDevices] = useState<PairDevice[]>([]);
  const [sources, setSources] = useState<PairSource[]>([]);
  const [acceptedAt, setAcceptedAt] = useState<Record<string, number | null>>(
    {},
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [connectModalOpen, setConnectModalOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<
    | { kind: "device"; value: PairDevice }
    | { kind: "source"; value: PairSource }
    | null
  >(null);
  const [revoking, setRevoking] = useState(false);

  const listCtrl = useRef<AbortController | null>(null);

  const refreshLists = useCallback(async () => {
    listCtrl.current?.abort();
    const ctrl = new AbortController();
    listCtrl.current = ctrl;
    setLoading(true);
    setError("");
    try {
      const [nextDevs, nextSrcs] = await Promise.all([
        getDevices(ctrl.signal),
        getSources(ctrl.signal),
      ]);
      if (ctrl.signal.aborted) return;
      setDevices(nextDevs);
      setSources(nextSrcs);

      if (scope && !isGlobal) {
        void getHubSummary(instance, scope, 0, ctrl.signal).then(
          (summary) => {
            if (ctrl.signal.aborted) return;
            const times: Record<string, number | null> = {};
            for (const row of summary.sources) {
              const stamp = row.last_accepted_at ?? null;
              if (stamp === null) {
                if (times[row.device_id] === undefined)
                  times[row.device_id] = null;
                continue;
              }
              const prev = times[row.device_id];
              if (prev === undefined || prev === null || stamp > prev) {
                times[row.device_id] = stamp;
              }
            }
            setAcceptedAt(times);
          },
          () => {
            if (!ctrl.signal.aborted) setAcceptedAt({});
          },
        );
      }
    } catch (reason) {
      if (!ctrl.signal.aborted) setError(pairingMessage(reason));
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }, [instance, scope, isGlobal]);

  useEffect(() => {
    void refreshLists();
    return () => listCtrl.current?.abort();
  }, [refreshLists]);

  // Open wizard automatically if an uncompleted invitation exists in sessionStorage
  useEffect(() => {
    if (!hasValidProject || !project) return;
    try {
      if (readInvitationProgress(invitationKey(instance, project.scope_id))) {
        setConnectModalOpen(true);
      }
    } catch {
      // Handled inside ConnectAgentModal when opened
    }
  }, [hasValidProject, instance, project]);

  async function confirmRevoke() {
    if (!revokeTarget || revoking) return;
    setRevoking(true);
    setError("");
    try {
      if (revokeTarget.kind === "device") {
        await revokeDevice(revokeTarget.value);
        setNotice(`已撤销设备「${revokeTarget.value.display_name}」的访问权限。`);
      } else {
        await revokeSource(revokeTarget.value);
        setNotice(`已断开来源「${revokeTarget.value.display_name}」。`);
      }
      setRevokeTarget(null);
      await refreshLists();
    } catch (reason) {
      setError(pairingMessage(reason));
    } finally {
      setRevoking(false);
    }
  }

  const activeDevices = devices.filter((d) => d.status === "active");
  const projectDevices = hasValidProject
    ? activeDevices.filter(
        (d) =>
          d.grants.some((g) => g.scope_id === scope) ||
          sources.some((s) => s.device_id === d.device_id && s.scope_id === scope),
      )
    : activeDevices;

  const revokedDevices = devices.filter((d) => d.status === "revoked");

  return (
    <div className="cmh-page">
      <header className="cmh-page-header">
        <div>
          <div className="cmh-page-header__title-row">
            <h1>设备与接入</h1>
            {project ? (
              <Badge tone={isGlobal ? "amber" : "indigo"}>
                {isGlobal ? "当前选中：全局偏好" : `项目：${project.display_name}`}
              </Badge>
            ) : null}
          </div>
          <p>
            使用 10 分钟一次性链接为电脑上的 Claude Code、Codex 等添加远程 MCP 与项目钩子，或管理已授权设备。
          </p>
        </div>
        <div className="cmh-page-header__actions">
          <Button variant="secondary" size="sm" onClick={onOpenCreateProject}>
            <FolderPlus size={14} /> 新建项目
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={loading}
            onClick={() => void refreshLists()}
          >
            <RefreshCw size={14} className={loading ? "cmh-spin" : ""} /> 刷新
          </Button>
          {hasValidProject ? (
            <Button
              variant="primary"
              onClick={() => setConnectModalOpen(true)}
            >
              <Plus size={15} /> 连接新 Agent
            </Button>
          ) : null}
        </div>
      </header>

      {!hasValidProject ? (
        <section className="cmh-callout" aria-label="选择项目提示">
          <div className="cmh-callout__body">
            <strong>
              {isGlobal
                ? "「全局偏好」不能直接用于设备配对。"
                : "请先在左上角选择或创建一个共享项目。"}
            </strong>
            <p>
              每台设备都需要绑定到一个具体的共享项目以获得读写授权。切换或新建项目后即可生成一次性连接链接。
            </p>
          </div>
          <Button variant="primary" onClick={onOpenCreateProject}>
            <FolderPlus size={15} /> 新建共享项目
          </Button>
        </section>
      ) : null}

      {notice ? (
        <p role="status" className="cmh-alert cmh-alert--emerald">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {error}
        </p>
      ) : null}

      {/* Primary Section: Connected Devices & Sources */}
      <section className="cmh-panel" aria-label="已连接设备与来源">
        <div className="cmh-panel__header">
          <div>
            <h2>
              {hasValidProject
                ? `「${project?.display_name}」已关联设备 (${projectDevices.length})`
                : `全部活跃设备 (${activeDevices.length})`}
            </h2>
            <p>
              「已授权」仅代表凭证与范围有效，不代表设备此刻在线；撤销后该设备将立即无法读写。
            </p>
          </div>
        </div>

        {loading && devices.length === 0 ? (
          <p className="cmh-muted">正在加载设备与授权清单…</p>
        ) : projectDevices.length === 0 ? (
          <EmptyState
            icon={<Monitor size={24} />}
            title="还没有已授权的设备连接"
            description={
              hasValidProject
                ? "点击「连接新 Agent」生成一次性链接，在你的 Mac 或 PC 上运行一条命令或把话术发给 Agent 即可完成配对。"
                : "目前整个 Hub 实例尚未注册活跃设备。"
            }
            actions={
              hasValidProject ? (
                <Button
                  variant="primary"
                  onClick={() => setConnectModalOpen(true)}
                >
                  <Link2 size={15} /> 生成 10 分钟连接链接
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="cmh-device-grid">
            {projectDevices.map((dev) => {
              const devSources = sources.filter(
                (s) =>
                  s.device_id === dev.device_id &&
                  (!hasValidProject || s.scope_id === scope),
              );
              const activeDevSources = devSources.filter(
                (s) => s.status === "active",
              );
              const authorized = hasValidProject
                ? hasPermissions(dev, scope)
                : true;
              return (
                <article key={dev.device_id} className="cmh-device-card">
                  <header className="cmh-device-card__head">
                    <div className="cmh-device-card__title">
                      <span className="cmh-device-card__icon">
                        <Monitor size={18} />
                      </span>
                      <div>
                        <h3>{dev.display_name}</h3>
                        <span className="cmh-mono">
                          {hasValidProject
                            ? formatUploadStatus(acceptedAt[dev.device_id])
                            : `创建于 ${formatFullTimestamp(dev.created_at)}`}
                        </span>
                      </div>
                    </div>
                    <div className="cmh-inline-actions">
                      <Badge tone={authorized ? "emerald" : "amber"} dot>
                        {authorized ? "项目已授权" : "部分权限"}
                      </Badge>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          setRevokeTarget({ kind: "device", value: dev })
                        }
                      >
                        <ShieldOff size={14} /> 撤销设备
                      </Button>
                    </div>
                  </header>

                  <div className="cmh-device-card__sources">
                    {activeDevSources.length > 0 ? (
                      <ul className="cmh-source-rows">
                        {activeDevSources.map((src) => (
                          <li key={src.source_id} className="cmh-source-row">
                            <div className="cmh-source-row__main">
                              <Bot size={15} />
                              <strong>{src.display_name}</strong>
                              <code className="cmh-mono">{src.adapter_id}</code>
                            </div>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() =>
                                setRevokeTarget({ kind: "source", value: src })
                              }
                            >
                              断开来源
                            </Button>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="cmh-muted">该设备在此项目下暂无活跃来源。</p>
                    )}
                  </div>

                  <footer className="cmh-device-card__foot cmh-mono">
                    <span>设备 UUID: {dev.device_id}</span>
                    <span>版本 r{dev.revision}</span>
                  </footer>
                </article>
              );
            })}
          </div>
        )}

        {revokedDevices.length > 0 ? (
          <details className="cmh-CleanDetails cmh-mt-4">
            <summary>已撤销的历史设备（{revokedDevices.length}）</summary>
            <ul className="cmh-revoked-list">
              {revokedDevices.map((d) => (
                <li key={d.device_id}>
                  <span>{d.display_name}</span>
                  <code className="cmh-mono">{d.device_id}</code>
                  <Badge tone="neutral">已撤销</Badge>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </section>

      {/* Collapsible Advanced Manual JSON Pairing */}
      {hasValidProject && project ? (
        <details className="cmh-advanced-box">
          <summary>
            <KeyRound size={15} />
            <span>高级选项：手动签发 JSON 配对文件与轮换设备凭据</span>
          </summary>
          <div className="cmh-advanced-box__body">
            <AdvancedManualPairing
              instance={instance}
              project={project}
              devices={devices}
              sources={sources}
              onChanged={refreshLists}
            />
          </div>
        </details>
      ) : null}

      {/* One-Link Agent Connect Wizard Modal */}
      {hasValidProject && project ? (
        <ConnectAgentModal
          open={connectModalOpen}
          instance={instance}
          project={project}
          onClose={() => setConnectModalOpen(false)}
          onPaired={() => void refreshLists()}
        />
      ) : null}

      {/* Revoke Confirmation Modal */}
      <Modal
        open={Boolean(revokeTarget)}
        title={
          revokeTarget?.kind === "device" ? "确认撤销整台设备？" : "确认断开此来源？"
        }
        subtitle="撤销立即在服务端生效，已被客户端读取的本地缓存不会远端抹除"
        busy={revoking}
        onClose={() => setRevokeTarget(null)}
        footer={
          <div className="cmh-inline-actions">
            <Button
              variant="ghost"
              disabled={revoking}
              onClick={() => setRevokeTarget(null)}
            >
              取消
            </Button>
            <Button
              variant="danger"
              loading={revoking}
              onClick={() => void confirmRevoke()}
            >
              {revokeTarget?.kind === "device" ? "确认撤销设备" : "确认断开来源"}
            </Button>
          </div>
        }
      >
        {revokeTarget ? (
          <p className="cmh-prose-sm">
            {revokeTarget.kind === "device"
              ? `撤销设备「${revokeTarget.value.display_name}」后，该设备在所有项目下的凭证将立即失效，后续上传与在线读取均会被拒绝。`
              : `断开来源「${revokeTarget.value.display_name}」（${revokeTarget.value.adapter_id}）后，该来源不能再向当前项目写入或通过对应 MCP 路径访问。`}
          </p>
        ) : null}
      </Modal>
    </div>
  );
}

function ConnectAgentModal({
  open,
  instance,
  project,
  onClose,
  onPaired,
}: {
  open: boolean;
  instance: string;
  project: HubProject;
  onClose(): void;
  onPaired(): void;
}) {
  const key = invitationKey(instance, project.scope_id);
  const [name, setName] = useState("");
  const [agent, setAgent] = useState<Agent>("claude");
  const [methodTab, setMethodTab] = useState<"prompt" | "unix" | "powershell">(
    "prompt",
  );
  const [progress, setProgress] = useState<InvitationProgress | null>(null);
  const [invitation, setInvitation] = useState<Invitation | null>(null);
  const [status, setStatus] = useState<InvitationStatus | null>(null);
  const [storageError, setStorageError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revokeConfirmed, setRevokeConfirmed] = useState(false);

  const writer = useRef<AbortController | null>(null);

  function save(next: InvitationProgress | null) {
    setProgress(next);
    try {
      if (next) sessionStorage.setItem(key, JSON.stringify(next));
      else sessionStorage.removeItem(key);
    } catch {
      setStorageError(true);
      throw new InvitationError("storage_unavailable");
    }
  }

  useEffect(() => {
    if (!open) return;
    try {
      setProgress(readInvitationProgress(key));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
    return () => writer.current?.abort();
  }, [open, key]);

  useEffect(() => {
    const id = progress?.pairing_id;
    if (!open || !id || busy) return;
    let ctrl: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      ctrl?.abort();
      clearTimeout(timer);
      const current = new AbortController();
      ctrl = current;
      let keepPolling = true;
      try {
        const next = await readInvitation(
          id!,
          project.scope_id,
          current.signal,
        );
        if (current.signal.aborted) return;
        setStatus(next);
        setError("");
        keepPolling = next.state === "pending";
        if (next.state === "claimed") {
          setInvitation(null);
          onPaired();
        } else if (!keepPolling) {
          setInvitation(null);
        }
      } catch (reason) {
        if (current.signal.aborted) return;
        setError(invitationMessage(reason));
        keepPolling = !(
          reason instanceof InvitationError &&
          [
            "administrator_required",
            "authentication_required",
            "pairing_not_found",
          ].includes(reason.code)
        );
        if (!keepPolling) setInvitation(null);
      }
      if (keepPolling && !current.signal.aborted) {
        timer = setTimeout(() => void poll(), 5000);
      }
    }
    void poll();
    return () => {
      ctrl?.abort();
      clearTimeout(timer);
    };
  }, [open, progress?.pairing_id, project.scope_id, busy, onPaired]);

  async function mutate(revoke: boolean) {
    if (busy || storageError) return;
    let label = "";
    if (!revoke) {
      try {
        label = validLabel(name);
      } catch (reason) {
        setError(pairingMessage(reason));
        return;
      }
    }
    const ctrl = new AbortController();
    writer.current = ctrl;
    setBusy(true);
    setError("");
    setRevokeConfirmed(false);
    try {
      if (revoke) {
        const pairingId = progress?.pairing_id;
        if (!pairingId) throw new InvitationError("invalid_response");
        const next = await readInvitation(
          pairingId,
          project.scope_id,
          ctrl.signal,
          true,
        );
        if (!ctrl.signal.aborted) {
          setStatus(next);
          setInvitation(null);
        }
      } else {
        save({ version: 1, pairing_id: null });
        const created = await createInvitation(
          project.scope_id,
          label,
          ctrl.signal,
          (id) => save({ version: 1, pairing_id: id }),
        );
        if (!ctrl.signal.aborted) {
          setInvitation(created);
          setStatus(null);
        }
      }
    } catch (reason) {
      if (!ctrl.signal.aborted) {
        if (!revoke && reason instanceof InvitationError && reason.rejected) {
          try {
            save(null);
          } catch {
            /* preserve storage error */
          }
        }
        setError(invitationMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  function resetWizard() {
    try {
      save(null);
      setInvitation(null);
      setStatus(null);
      setName("");
      setError("");
      setRevokeConfirmed(false);
    } catch (reason) {
      setError(invitationMessage(reason));
    }
  }

  const instructions = invitation && invitationInstructions(invitation, agent);
  const terminal = status && status.state !== "pending";
  const expiry = status?.expires_at ?? invitation?.expires_at;

  return (
    <Modal
      open={open}
      width="lg"
      busy={busy}
      title={`连接新 Agent 到「${project.display_name}」`}
      subtitle="无需安装独立桌面客户端。每台电脑单独生成一条 10 分钟有效的一次性配对链接。"
      onClose={onClose}
    >
      <div className="cmh-stack">
        {/* Agent Picker */}
        <div className="cmh-field">
          <span className="cmh-field__label">1. 选择目标 AI 应用</span>
          <div className="cmh-agent-pills" role="radiogroup" aria-label="要连接的 Agent">
            {(Object.entries(agents) as [Agent, string][]).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={agent === id}
                className={`cmh-agent-pill${agent === id ? " is-active" : ""}`}
                onClick={() => setAgent(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {!progress ? (
          <form
            className="cmh-stack"
            onSubmit={(event) => {
              event.preventDefault();
              void mutate(false);
            }}
          >
            <div className="cmh-field">
              <label htmlFor="cmh-device-name">2. 为这台电脑起一个易识别的设备名</label>
              <input
                id="cmh-device-name"
                className="cmh-input"
                placeholder="例如：家里 MacBook Pro、办公室 Mac mini"
                value={name}
                disabled={busy}
                onChange={(event) => setName(event.target.value)}
              />
            </div>

            {error ? (
              <p role="alert" className="cmh-alert cmh-alert--danger">
                {error}
              </p>
            ) : null}

            <div className="cmh-modal__inline-footer">
              <Button variant="ghost" onClick={onClose} disabled={busy}>
                取消
              </Button>
              <Button
                type="submit"
                variant="primary"
                loading={busy}
                disabled={!name.trim() || storageError}
              >
                <Link2 size={15} /> 生成 10 分钟连接链接
              </Button>
            </div>
          </form>
        ) : (
          <div className="cmh-stack">
            <div
              role="status"
              className={`cmh-alert ${
                status?.state === "claimed"
                  ? "cmh-alert--emerald"
                  : terminal
                    ? "cmh-alert--amber"
                    : "cmh-alert--indigo"
              }`}
            >
              {status?.state === "claimed" ? (
                <CheckCircle2 size={17} />
              ) : (
                <Clock size={17} />
              )}
              <div>
                <strong>
                  {status?.state === "claimed"
                    ? "配对成功！设备凭证已签发。"
                    : status?.state === "expired"
                      ? "该一次性连接链接已过期。"
                      : status?.state === "cancelled"
                        ? "该连接邀请已被撤销。"
                        : progress.pairing_id
                          ? "等待目标电脑领取配对（每 5 秒自动检测状态）…"
                          : "创建结果尚未确认，未取得可用链接。"}
                </strong>
                {expiry && !terminal ? (
                  <p>
                    有效期至：{new Date(expiry).toLocaleString("zh-CN")} · 离开或刷新页面后密钥不再重现，请勿公开转发。
                  </p>
                ) : null}
                {status?.state === "claimed" ? (
                  <p>
                    请按安装输出重新加载 Agent 的 MCP 或打开专用共享目录开始对话。
                  </p>
                ) : null}
              </div>
            </div>

            {instructions && !terminal ? (
              <div className="cmh-stack">
                <div className="cmh-tabs" role="tablist" aria-label="安装方式">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={methodTab === "prompt"}
                    className={methodTab === "prompt" ? "is-active" : ""}
                    onClick={() => setMethodTab("prompt")}
                  >
                    方式一：发给 Agent 一句话（推荐）
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={methodTab === "unix"}
                    className={methodTab === "unix" ? "is-active" : ""}
                    onClick={() => setMethodTab("unix")}
                  >
                    <Terminal size={13} /> Mac / Linux 命令
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={methodTab === "powershell"}
                    className={methodTab === "powershell" ? "is-active" : ""}
                    onClick={() => setMethodTab("powershell")}
                  >
                    Windows PowerShell
                  </button>
                </div>

                {methodTab === "prompt" ? (
                  <CopyBlock
                    label={`直接粘贴发给 ${agents[agent]}`}
                    code={instructions.prompt}
                    copyLabel="复制安装话术"
                  />
                ) : methodTab === "unix" ? (
                  <CopyBlock
                    label="在目标 Mac / Linux 终端运行"
                    code={instructions.unix}
                    copyLabel="复制终端命令"
                  />
                ) : (
                  <CopyBlock
                    label="在目标 Windows PowerShell 运行"
                    code={instructions.powershell}
                    copyLabel="复制 PowerShell 命令"
                  />
                )}

                <details className="cmh-CleanDetails">
                  <summary>使用 Clash / Shadowrocket 等网络代理？</summary>
                  <p className="cmh-prose-sm">
                    若通过局域网或 Tailscale 访问 NAS，请将{" "}
                    <code className="cmh-mono">{invitation.origin}</code>{" "}
                    对应的域名加入代理软件的 <code className="cmh-mono">DIRECT</code>{" "}
                    直连规则，重启 Agent 后再验证 MCP 连接。
                  </p>
                </details>
              </div>
            ) : null}

            {!instructions && !terminal && progress.pairing_id ? (
              <p className="cmh-muted">
                由于页面刷新，一次性密钥不再回显。若尚未在目标电脑复制使用，可在下方勾选并撤销此邀请后重新生成。
              </p>
            ) : null}

            {error ? (
              <p role="alert" className="cmh-alert cmh-alert--danger">
                {error}
              </p>
            ) : null}

            {!terminal && progress.pairing_id ? (
              <div className="cmh-revoke-box">
                <label className="cmh-checkbox">
                  <input
                    type="checkbox"
                    checked={revokeConfirmed}
                    disabled={busy}
                    onChange={(e) => setRevokeConfirmed(e.target.checked)}
                  />
                  <span>确认作废当前未领取的链接</span>
                </label>
                <Button
                  variant="danger"
                  size="sm"
                  loading={busy}
                  disabled={!revokeConfirmed}
                  onClick={() => void mutate(true)}
                >
                  撤销此邀请
                </Button>
              </div>
            ) : null}

            <div className="cmh-modal__inline-footer">
              {terminal || !progress.pairing_id ? (
                <Button variant="secondary" onClick={resetWizard}>
                  继续配对另一台设备
                </Button>
              ) : null}
              <Button variant="primary" onClick={onClose}>
                {status?.state === "claimed" ? "完成" : "后台保留并关闭窗口"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

function AdvancedManualPairing({
  instance,
  project,
  devices,
  sources,
  onChanged,
}: {
  instance: string;
  project: HubProject;
  devices: PairDevice[];
  sources: PairSource[];
  onChanged(): Promise<void>;
}) {
  const scope = project.scope_id;
  const [name, setName] = useState("");
  const [chosenDevice, setChosenDevice] = useState("");
  const [rotateConfirmed, setRotateConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [progress, setProgress] = useState<PairProgress | null>(null);
  const [ready, setReady] = useState(false);
  const credentialRef = useRef<IssuedDevice | null>(null);
  const pairingRef = useRef<PairingFile | null>(null);

  useEffect(() => {
    try {
      setProgress(readProgress(instance, scope));
    } catch {
      setError("无法读取本地配对阶段记录。");
    }
  }, [instance, scope]);

  async function buildJsonForNewDevice() {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      pairingOrigin();
      const issued = await createDevice(name);
      credentialRef.current = issued;
      const authorized = hasPermissions(issued.device, scope)
        ? issued.device
        : await grantProject(issued.device, scope);
      const authedIssued = { ...issued, device: authorized };
      credentialRef.current = authedIssued;
      const src = await createSource(authorized, scope);
      const file = await checkPairing(instance, scope, authedIssued, src);
      pairingRef.current = file;
      const next: PairProgress = {
        version: 1,
        instance,
        scope,
        phase: "ready",
        device_id: authorized.device_id,
        source_id: src.source_id,
        source_creation_unknown: false,
      };
      saveProgress(next);
      setProgress(next);
      setReady(true);
      setName("");
      setNotice("手动配对文件已生成并通过权限核验，请立即下载妥善保管。");
      await onChanged();
    } catch (reason) {
      setError(pairingMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleRotateExisting() {
    const target = devices.find(
      (d) => d.device_id === chosenDevice && d.status === "active",
    );
    if (!target || !rotateConfirmed || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      pairingOrigin();
      const rotated = await rotateDevice(target);
      const authorized = hasPermissions(rotated.device, scope)
        ? rotated.device
        : await grantProject(rotated.device, scope);
      const authedIssued = { ...rotated, device: authorized };
      const existingSrc =
        sources.find(
          (s) =>
            s.device_id === authorized.device_id &&
            s.scope_id === scope &&
            s.adapter_id === agentAdapter &&
            s.status === "active",
        ) ?? (await createSource(authorized, scope));
      const file = await checkPairing(
        instance,
        scope,
        authedIssued,
        existingSrc,
      );
      pairingRef.current = file;
      setReady(true);
      setRotateConfirmed(false);
      setNotice("设备凭证已轮换生效（旧连接已失效），请下载新配对文件导入。");
      await onChanged();
    } catch (reason) {
      setError(pairingMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  const activeDevs = devices.filter((d) => d.status === "active");

  return (
    <div className="cmh-stack">
      <p className="cmh-prose-sm">
        适用于使用本地后台采集守护进程（<code className="cmh-mono">cloud-memory-setup import</code>）的高级场景。日常接入推荐使用上方的 10 分钟一次性链接。
      </p>

      <div className="cmh-two-col">
        <form
          className="cmh-panel cmh-panel--inset cmh-stack"
          onSubmit={(e) => {
            e.preventDefault();
            void buildJsonForNewDevice();
          }}
        >
          <h4>新建设备并生成 JSON 配对文件</h4>
          <div className="cmh-field">
            <label htmlFor="cmh-adv-dev-name">新设备名称</label>
            <input
              id="cmh-adv-dev-name"
              className="cmh-input"
              placeholder="例如：Mac Studio 本地采集器"
              value={name}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <Button
            type="submit"
            variant="secondary"
            loading={busy}
            disabled={!name.trim()}
          >
            签发新设备并核验配对文件
          </Button>
        </form>

        <div className="cmh-panel cmh-panel--inset cmh-stack">
          <h4>轮换已有设备凭证</h4>
          <div className="cmh-field">
            <label htmlFor="cmh-rotate-select">选择活跃设备</label>
            <select
              id="cmh-rotate-select"
              className="cmh-select"
              value={chosenDevice}
              disabled={busy}
              onChange={(e) => setChosenDevice(e.target.value)}
            >
              <option value="">选择设备…</option>
              {activeDevs.map((d) => (
                <option key={d.device_id} value={d.device_id}>
                  {d.display_name}
                </option>
              ))}
            </select>
          </div>
          <label className="cmh-checkbox">
            <input
              type="checkbox"
              checked={rotateConfirmed}
              disabled={busy || !chosenDevice}
              onChange={(e) => setRotateConfirmed(e.target.checked)}
            />
            <span>已知晓轮换会使该设备旧连接立即失效</span>
          </label>
          <Button
            variant="secondary"
            loading={busy}
            disabled={!chosenDevice || !rotateConfirmed}
            onClick={() => void handleRotateExisting()}
          >
            重新签发凭证并准备 JSON
          </Button>
        </div>
      </div>

      {progress ? (
        <div className="cmh-inline-actions">
          <span className="cmh-muted cmh-mono">
            上次记录阶段：{progress.phase}
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              sessionStorage.removeItem(progressKey(instance, scope));
              setProgress(null);
              setReady(false);
              pairingRef.current = null;
            }}
          >
            清除阶段记录
          </Button>
        </div>
      ) : null}

      {notice ? (
        <p role="status" className="cmh-alert cmh-alert--emerald">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {error}
        </p>
      ) : null}

      {ready && pairingRef.current ? (
        <div className="cmh-inline-actions">
          <Button
            variant="primary"
            onClick={() => {
              if (pairingRef.current) downloadPairing(pairingRef.current);
            }}
          >
            <Download size={15} /> 下载 cloud-memory-hub-pairing.json
          </Button>
        </div>
      ) : null}
    </div>
  );
}
