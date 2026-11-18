import {
  ArrowRight,
  Bot,
  Cpu,
  Database,
  HardDrive,
  Laptop,
  MessageSquareText,
  MessagesSquare,
  Monitor,
  Plus,
  Smartphone,
  Sparkles,
} from "lucide-react";
import { useEffect, useState } from "react";
import { summarizeHistoryItem } from "../api/history-display";
import {
  getAllHubSources,
  getHubDashboard,
  type HubDashboardData,
  type HubItem,
  type HubPage,
  type HubProject,
  type HubSource,
  hubErrorMessage,
  readHub,
} from "../api/hub-api";
import {
  type BackupStatus,
  backupErrorMessage,
  getBackupStatus,
} from "../api/hub-backups";
import { effectiveHubSources, getDevices } from "../api/hub-pairing";
import { getModelProfiles, type ModelProfile } from "../api/model-profiles";
import { OriginalDrawer } from "../components/OriginalDrawer";
import {
  Badge,
  Button,
  EmptyState,
  formatTimestamp,
} from "../components/ui";
import { useHub } from "../context/HubContext";
import type { ConsoleView, SettingsTab } from "../router";

type Resource<T> = { data: T | null; error: string; loading: boolean };
const initialResource = <T,>(): Resource<T> => ({
  data: null,
  error: "",
  loading: true,
});

function deviceIcon(name: string) {
  const lower = name.toLowerCase();
  if (lower.includes("book") || lower.includes("laptop")) return Laptop;
  if (lower.includes("phone") || lower.includes("ios")) return Smartphone;
  return Monitor;
}

export function OverviewView({
  project,
  onNavigate,
}: {
  project: HubProject;
  onNavigate(view: ConsoleView, tab?: SettingsTab): void;
}) {
  const { bootstrap } = useHub();
  const instance = bootstrap.instance_id;
  const scope = project.scope_id;
  const isGlobal = scope === "global";

  const [dashboard, setDashboard] =
    useState<Resource<HubDashboardData>>(initialResource);
  const [sources, setSources] = useState<Resource<HubSource[]>>(initialResource);
  const [activity, setActivity] = useState<Resource<HubPage>>(initialResource);
  const [backups, setBackups] = useState<Resource<BackupStatus>>(initialResource);
  const [models, setModels] = useState<Resource<ModelProfile[]>>(initialResource);
  const [drawerItem, setDrawerItem] = useState<HubItem | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;

    function run<T>(
      promise: Promise<T>,
      setter: React.Dispatch<React.SetStateAction<Resource<T>>>,
      formatErr: (err: unknown) => string = hubErrorMessage,
    ) {
      setter({ data: null, error: "", loading: true });
      void promise.then(
        (data) => {
          if (!signal.aborted) setter({ data, error: "", loading: false });
        },
        (err) => {
          if (!signal.aborted)
            setter({ data: null, error: formatErr(err), loading: false });
        },
      );
    }

    run(getHubDashboard(instance, scope, signal), setDashboard);
    run(
      Promise.all([
        getAllHubSources(instance, scope, signal),
        getDevices(signal),
      ]).then(([rows, devs]) => effectiveHubSources(rows, devs, scope)),
      setSources,
    );
    run(
      readHub(instance, scope, "history-list", { limit: 6, offset: 0 }, signal),
      setActivity,
    );
    run(getBackupStatus(signal), setBackups, backupErrorMessage);
    run(getModelProfiles(signal), setModels, () => "无法读取模型状态");

    return () => controller.abort();
  }, [instance, scope]);

  const stats = dashboard.data?.statistics;
  const activeModel = models.data?.find((m) => m.is_active) ?? null;
  const sourceRows = sources.data ?? [];

  const groupedDevices = new Map<
    string,
    {
      name: string;
      sources: HubSource[];
      lastAccepted: number | null | undefined;
    }
  >();
  for (const src of sourceRows) {
    const prev = groupedDevices.get(src.device_id);
    const prevStamp = prev?.lastAccepted;
    const nextStamp =
      src.last_accepted_at !== undefined && src.last_accepted_at !== null
        ? prevStamp && src.last_accepted_at < prevStamp
          ? prevStamp
          : src.last_accepted_at
        : (prevStamp ?? src.last_accepted_at);
    groupedDevices.set(src.device_id, {
      name: src.device_display_name,
      sources: [...(prev?.sources ?? []), src],
      lastAccepted: nextStamp,
    });
  }
  const deviceCards = [...groupedDevices.entries()].map(([id, info]) => ({
    id,
    ...info,
  }));

  const backupBadge = (() => {
    if (backups.loading) return { text: "读取中…", tone: "neutral" as const };
    if (backups.error || !backups.data)
      return { text: "待核对", tone: "amber" as const };
    const map = {
      completed: { text: "最近已完成", tone: "emerald" as const },
      running: { text: "正在备份", tone: "blue" as const },
      not_run: { text: "尚未执行", tone: "neutral" as const },
      failed: { text: "最近失败", tone: "danger" as const },
      interrupted: { text: "已中断", tone: "amber" as const },
    };
    return map[backups.data.execution.state];
  })();

  const showSetupBanner =
    !isGlobal &&
    !sources.loading &&
    !sources.error &&
    sourceRows.length === 0;

  return (
    <div className="cmh-page">
      <header className="cmh-page-header">
        <div>
          <div className="cmh-page-header__title-row">
            <h1>{project.display_name}</h1>
            <Badge tone={isGlobal ? "blue" : "indigo"}>
              {isGlobal ? "全局独立偏好" : "共享项目空间"}
            </Badge>
          </div>
          <p>
            {isGlobal
              ? "保存跨项目通用的个人偏好与习惯，不会自动混合单个项目的业务上下文。"
              : "查看当前项目的设备连接、共享会话动态、接纳趋势与系统健康度。"}
          </p>
        </div>
        <div className="cmh-page-header__actions">
          <Button variant="secondary" onClick={() => onNavigate("ask")}>
            <Sparkles size={15} />
            问记忆
          </Button>
          {!isGlobal ? (
            <Button variant="primary" onClick={() => onNavigate("devices")}>
              <Plus size={15} />
              连接新 Agent
            </Button>
          ) : null}
        </div>
      </header>

      {showSetupBanner ? (
        <section className="cmh-callout" aria-label="项目接入引导">
          <div className="cmh-callout__body">
            <strong>这个项目还没有连接任何设备。</strong>
            <p>
              前往「设备与接入」生成 10 分钟一次性配对链接，复制一句话发给电脑上的 Claude Code 或 Codex 即可开始共享记忆与接续会话。
            </p>
          </div>
          <Button variant="primary" onClick={() => onNavigate("devices")}>
            生成连接链接 <ArrowRight size={15} />
          </Button>
        </section>
      ) : null}

      {dashboard.error ? (
        <p role="alert" className="cmh-alert cmh-alert--danger">
          {dashboard.error}
        </p>
      ) : null}

      {/* Metric Strip */}
      <section className="cmh-kpi-grid" aria-label="项目统计指标">
        <button
          type="button"
          className="cmh-kpi-card"
          onClick={() => onNavigate("devices")}
        >
          <div className="cmh-kpi-card__top">
            <span>已授权设备</span>
            <Monitor size={17} />
          </div>
          <strong className="cmh-kpi-card__value">
            {stats ? stats.device_connections.toLocaleString("zh-CN") : "—"}
          </strong>
          <small>拥有当前范围读写授权的设备</small>
        </button>

        <button
          type="button"
          className="cmh-kpi-card"
          onClick={() => onNavigate("devices")}
        >
          <div className="cmh-kpi-card__top">
            <span>Agent 来源</span>
            <Bot size={17} />
          </div>
          <strong className="cmh-kpi-card__value">
            {stats ? stats.agent_connections.toLocaleString("zh-CN") : "—"}
          </strong>
          <small>当前范围处于有效状态的来源</small>
        </button>

        <button
          type="button"
          className="cmh-kpi-card"
          onClick={() => onNavigate("history")}
        >
          <div className="cmh-kpi-card__top">
            <span>共享记录</span>
            <MessageSquareText size={17} />
          </div>
          <strong className="cmh-kpi-card__value">
            {stats ? stats.shared_records.toLocaleString("zh-CN") : "—"}
          </strong>
          <small>当前范围已接纳且可见的原文</small>
        </button>

        <button
          type="button"
          className="cmh-kpi-card"
          onClick={() => onNavigate("history")}
        >
          <div className="cmh-kpi-card__top">
            <span>任务交接</span>
            <MessagesSquare size={17} />
          </div>
          <strong className="cmh-kpi-card__value">
            {stats ? stats.handoffs.toLocaleString("zh-CN") : "—"}
          </strong>
          <small>跨设备接续提交的交接单</small>
        </button>
      </section>

      {/* Main Two-Column Workbench */}
      <div className="cmh-overview-grid">
        <div className="cmh-stack">
          {/* Recent Activity Feed */}
          <section className="cmh-panel" aria-label="近期共享动态">
            <div className="cmh-panel__header">
              <div>
                <h2>近期共享动态</h2>
                <p>按 Hub 接纳顺序展示最新记录，点击任意条目查看完整原文</p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onNavigate("history")}
              >
                全部原文 <ArrowRight size={14} />
              </Button>
            </div>

            {activity.loading ? (
              <p className="cmh-muted">正在读取近期活动…</p>
            ) : activity.error ? (
              <p role="alert" className="cmh-alert cmh-alert--danger">
                {activity.error}
              </p>
            ) : !activity.data?.items.length ? (
              <EmptyState
                title="暂无共享记录"
                description="已连接的 Agent 完成对话或调用记忆保存工具后，接纳的记录会实时显示在这里。"
                actions={
                  !isGlobal ? (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => onNavigate("devices")}
                    >
                      检查设备连接
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <ul className="cmh-activity-list">
                {activity.data.items.map((item) => {
                  const preview = summarizeHistoryItem({
                    raw_id: item.record_id,
                    content: item.content,
                    truncated: item.next_content_offset !== null,
                  });
                  return (
                    <li key={item.record_id}>
                      <button
                        type="button"
                        className="cmh-activity-row"
                        onClick={() => setDrawerItem(item)}
                      >
                        <div className="cmh-activity-row__head">
                          <Badge tone={preview.badgeTone}>
                            {preview.kindLabel}
                          </Badge>
                          <span className="cmh-mono cmh-activity-row__meta">
                            {item.source ? `${item.source} · ` : ""}
                            {formatTimestamp(item.memory_at)}
                          </span>
                        </div>
                        <p className="cmh-activity-row__title">
                          {preview.headline}
                        </p>
                        <span className="cmh-activity-row__sub">
                          {preview.subline}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          {/* 7-Day UTC Activity Chart */}
          <section className="cmh-panel" aria-label="七日共享记录趋势">
            <div className="cmh-panel__header">
              <div>
                <h2>近 7 日接纳趋势（UTC）</h2>
                <p>按服务端持久队列实际接纳时间统计，不用客户端本地时间替代</p>
              </div>
            </div>
            {dashboard.loading ? (
              <p className="cmh-muted">正在加载趋势统计…</p>
            ) : !dashboard.data ? (
              <p className="cmh-muted">暂时无法读取趋势数据。</p>
            ) : dashboard.data.activity_state === "unavailable" ? (
              <div className="cmh-alert cmh-alert--amber">
                有 {dashboard.data.missing_timestamps} 条可见回执缺少队列时间记录，为保持统计诚实，暂不绘制折算趋势。
              </div>
            ) : (
              <ActivityBarChart days={dashboard.data.daily_activity} />
            )}
          </section>
        </div>

        <div className="cmh-stack">
          {/* Connected Devices Summary */}
          <section className="cmh-panel" aria-label="已连接设备概览">
            <div className="cmh-panel__header">
              <div>
                <h2>已授权设备</h2>
                <p>仅展示当前范围有效授权的连接，不代表实时在线</p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onNavigate("devices")}
              >
                管理 <ArrowRight size={14} />
              </Button>
            </div>

            {sources.loading ? (
              <p className="cmh-muted">正在核对设备授权…</p>
            ) : sources.error ? (
              <p role="alert" className="cmh-alert cmh-alert--danger">
                {sources.error}
              </p>
            ) : deviceCards.length === 0 ? (
              <EmptyState
                title={isGlobal ? "全局范围不直接绑定设备" : "尚未接入设备"}
                description={
                  isGlobal
                    ? "设备按具体项目配对授权；全局偏好由已获授权的客户端按需调用。"
                    : "为你的 Mac 或 PC 生成连接链接，安装轻量 MCP 与项目钩子。"
                }
                actions={
                  !isGlobal ? (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => onNavigate("devices")}
                    >
                      <Plus size={14} /> 连接第一台设备
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <ul className="cmh-compact-devices">
                {deviceCards.map((dev) => {
                  const DevIcon = deviceIcon(dev.name);
                  return (
                    <li key={dev.id} className="cmh-compact-device">
                      <span className="cmh-compact-device__icon">
                        <DevIcon size={17} />
                      </span>
                      <div className="cmh-compact-device__info">
                        <strong>{dev.name}</strong>
                        <span>
                          {dev.sources.length} 个来源 ·{" "}
                          {dev.lastAccepted
                            ? `最近上传 ${formatTimestamp(dev.lastAccepted)}`
                            : dev.lastAccepted === null
                              ? "尚无上传记录"
                              : "上传时间待确认"}
                        </span>
                      </div>
                      <Badge tone="emerald" dot>
                        已授权
                      </Badge>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          {/* Record Composition */}
          <section className="cmh-panel" aria-label="记录组成">
            <div className="cmh-panel__header">
              <div>
                <h2>记录组成</h2>
                <p>当前范围已接纳事件的分类占比</p>
              </div>
            </div>
            {dashboard.data ? (
              <RecordComposition
                total={dashboard.data.statistics.shared_records}
                types={dashboard.data.record_types}
              />
            ) : (
              <p className="cmh-muted">
                {dashboard.loading ? "读取中…" : "暂无组成统计"}
              </p>
            )}
          </section>

          {/* Service & Model Health Snapshot */}
          <section className="cmh-panel" aria-label="运行配置状态">
            <div className="cmh-panel__header">
              <div>
                <h2>核心能力状态</h2>
                <p>模型提炼与定期备份的当前配置</p>
              </div>
            </div>
            <div className="cmh-health-rows">
              <button
                type="button"
                className="cmh-health-row"
                onClick={() => onNavigate("settings", "model")}
              >
                <Cpu size={16} />
                <div>
                  <strong>记忆整理模型</strong>
                  <span>
                    {models.loading
                      ? "正在读取配置…"
                      : activeModel
                        ? `${activeModel.name} (${activeModel.model})`
                        : "尚未启用模型（仅保留原文与交接）"}
                  </span>
                </div>
                <Badge tone={activeModel ? "emerald" : "amber"}>
                  {activeModel ? "已启用" : "未配置"}
                </Badge>
              </button>

              <button
                type="button"
                className="cmh-health-row"
                onClick={() => onNavigate("memories")}
              >
                <Database size={16} />
                <div>
                  <strong>长期记忆库</strong>
                  <span>查看已提炼的事实、摘要与画像并支持纠正</span>
                </div>
                <ArrowRight size={14} className="cmh-muted" />
              </button>

              <button
                type="button"
                className="cmh-health-row"
                onClick={() => onNavigate("settings", "backups")}
              >
                <HardDrive size={16} />
                <div>
                  <strong>NAS 受控备份</strong>
                  <span>
                    {backups.data?.schedule.enabled
                      ? `定期备份已开启（保留 ${backups.data.schedule.keep ?? 7} 份）`
                      : "定期自动备份尚未开启"}
                  </span>
                </div>
                <Badge tone={backupBadge.tone}>{backupBadge.text}</Badge>
              </button>
            </div>
          </section>
        </div>
      </div>

      <OriginalDrawer
        instance={instance}
        scope={scope}
        recordId={drawerItem?.record_id ?? null}
        initialItem={drawerItem}
        onClose={() => setDrawerItem(null)}
      />
    </div>
  );
}

function ActivityBarChart({
  days,
}: {
  days: { date: string; count: number }[];
}) {
  const max = Math.max(1, ...days.map((d) => d.count));
  return (
    <div className="cmh-bar-chart" role="img" aria-label="近七日记录接纳柱状图">
      {days.map((day) => {
        const pct = Math.max(day.count > 0 ? 8 : 2, Math.round((day.count / max) * 100));
        return (
          <div key={day.date} className="cmh-bar-chart__col">
            <span className="cmh-bar-chart__count cmh-mono">{day.count}</span>
            <div className="cmh-bar-chart__track">
              <div
                className={`cmh-bar-chart__fill${day.count === 0 ? " is-zero" : ""}`}
                style={{ height: `${pct}%` }}
              />
            </div>
            <span className="cmh-bar-chart__label cmh-mono">
              {day.date.slice(5)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function RecordComposition({
  total,
  types,
}: {
  total: number;
  types: HubDashboardData["record_types"];
}) {
  const toneClass: Record<string, string> = {
    conversation: "is-indigo",
    remember: "is-emerald",
    handoff: "is-blue",
    other: "is-neutral",
  };
  return (
    <div className="cmh-composition">
      <div className="cmh-composition__bar" aria-hidden="true">
        {total > 0 ? (
          types
            .filter((t) => t.count > 0)
            .map((t) => (
              <span
                key={t.kind}
                className={`cmh-composition__seg ${toneClass[t.kind] ?? "is-neutral"}`}
                style={{ width: `${Math.max(4, (t.count / total) * 100)}%` }}
              />
            ))
        ) : (
          <span className="cmh-composition__seg is-empty" style={{ width: "100%" }} />
        )}
      </div>
      <ul className="cmh-composition__legend">
        {types.map((t) => (
          <li key={t.kind}>
            <span className="cmh-composition__name">
              <i className={`cmh-composition__dot ${toneClass[t.kind] ?? "is-neutral"}`} />
              {t.label}
            </span>
            <strong className="cmh-mono">{t.count.toLocaleString("zh-CN")}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}
