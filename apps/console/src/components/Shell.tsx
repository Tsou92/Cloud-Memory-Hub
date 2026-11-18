import {
  ArrowUpRight,
  Check,
  ChevronDown,
  Database,
  FolderKanban,
  Globe2,
  Info,
  Laptop,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquareText,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  RefreshCw,
  Settings2,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { isAuthExpiredError } from "../api/hub-api";
import { useHub } from "../context/HubContext";
import type { ConsoleView, SettingsTab } from "../router";
import { CreateProjectModal } from "./CreateProjectModal";
import { Badge, Button, Modal } from "./ui";

const MOBILE_NAV_QUERY = "(max-width: 820px)";

function readMobileNavMatches(): boolean {
  if (typeof window === "undefined") return false;
  if (typeof window.matchMedia === "function") {
    return window.matchMedia(MOBILE_NAV_QUERY).matches;
  }
  return window.innerWidth <= 820;
}

const NAV_ITEMS: {
  id: ConsoleView;
  label: string;
  subtitle: string;
  Icon: typeof LayoutDashboard;
  projectScoped: boolean;
}[] = [
  {
    id: "overview",
    label: "总览",
    subtitle: "当前项目协同态势、近期动态与健康度",
    Icon: LayoutDashboard,
    projectScoped: true,
  },
  {
    id: "memories",
    label: "记忆库",
    subtitle: "浏览、语义检索、原地纠错与治理长期记忆",
    Icon: Database,
    projectScoped: true,
  },
  {
    id: "history",
    label: "历史原文",
    subtitle: "追溯已接纳的原始对话、任务交接与显式记录",
    Icon: MessageSquareText,
    projectScoped: true,
  },
  {
    id: "ask",
    label: "问记忆",
    subtitle: "基于当前范围记忆流式问答并核查引用来源",
    Icon: Sparkles,
    projectScoped: true,
  },
  {
    id: "devices",
    label: "设备与接入",
    subtitle: "配对新 Agent、查看已授权设备与管理访问权限",
    Icon: Laptop,
    projectScoped: false,
  },
  {
    id: "settings",
    label: "系统设置",
    subtitle: "记忆整理模型、调用费用估算、备份恢复与账户安全",
    Icon: Settings2,
    projectScoped: false,
  },
];

export function Shell({
  view,
  onNavigate,
  children,
}: {
  view: ConsoleView;
  onNavigate(next: ConsoleView, tab?: SettingsTab): void;
  children(props: { openCreateProject(): void }): ReactNode;
}) {
  const {
    bootstrap,
    project,
    select,
    addProject,
    moreProjects,
    refresh,
    loading,
    error,
  } = useHub();

  const [isMobileNav, setIsMobileNav] = useState<boolean>(readMobileNavMatches);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [projectFilter, setProjectFilter] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [noticeOpen, setNoticeOpen] = useState(false);
  const switcherRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.title = project
      ? `${project.display_name} · Cloud Memory Hub`
      : "Cloud Memory Hub";
  }, [project]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const syncViewport = () => {
      const matches = readMobileNavMatches();
      setIsMobileNav(matches);
      if (!matches) setMobileMenuOpen(false);
    };
    syncViewport();
    if (typeof window.matchMedia === "function") {
      const mql = window.matchMedia(MOBILE_NAV_QUERY);
      if (typeof mql.addEventListener === "function") {
        mql.addEventListener("change", syncViewport);
        return () => mql.removeEventListener("change", syncViewport);
      }
    }
    window.addEventListener("resize", syncViewport);
    return () => window.removeEventListener("resize", syncViewport);
  }, []);

  useEffect(() => {
    if (!mobileMenuOpen) return;
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileMenuOpen(false);
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [mobileMenuOpen]);

  useEffect(() => {
    if (!switcherOpen) return;
    const close = (event: MouseEvent) => {
      if (
        switcherRef.current &&
        !switcherRef.current.contains(event.target as Node)
      ) {
        setSwitcherOpen(false);
      }
    };
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSwitcherOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onEscape);
    };
  }, [switcherOpen]);

  const currentNav = NAV_ITEMS.find((item) => item.id === view) ?? NAV_ITEMS[0];
  const needsLogin = error ? isAuthExpiredError(error) : false;

  const filteredProjects = bootstrap.projects.filter((item) =>
    item.display_name.toLowerCase().includes(projectFilter.trim().toLowerCase()),
  );

  async function handleLogout() {
    const response = await fetch("/api/v1/ui/session/logout", {
      method: "POST",
      credentials: "same-origin",
    });
    if (response.ok) window.location.assign("/hub/login");
  }

  function handleSidebarToggle() {
    if (readMobileNavMatches()) {
      setIsMobileNav(true);
      setMobileMenuOpen((open) => !open);
      return;
    }
    setIsMobileNav(false);
    setMobileMenuOpen(false);
    setSidebarCollapsed((collapsed) => !collapsed);
  }

  const navToggleLabel = isMobileNav
    ? mobileMenuOpen
      ? "收起导航菜单"
      : "打开导航菜单"
    : sidebarCollapsed
      ? "展开侧边栏"
      : "收起侧边栏";

  return (
    <div
      className={`cmh-shell${!isMobileNav && sidebarCollapsed ? " is-sidebar-collapsed" : ""}`}
    >
      <a className="cmh-skip-link" href="#main-content">
        跳到主要内容
      </a>

      {isMobileNav && mobileMenuOpen ? (
        <button
          type="button"
          className="cmh-sidebar-backdrop"
          aria-label="关闭侧栏导航"
          onClick={() => setMobileMenuOpen(false)}
        />
      ) : null}

      <aside
        className={`cmh-sidebar${isMobileNav && mobileMenuOpen ? " is-open" : ""}`}
        aria-label="Cloud Memory Hub 导航"
        hidden={!isMobileNav && sidebarCollapsed}
      >
        <div className="cmh-sidebar__brand">
          <button
            type="button"
            className="cmh-brand-btn"
            onClick={() => {
              onNavigate("overview");
              setMobileMenuOpen(false);
            }}
          >
            <span className="cmh-brand-glyph" aria-hidden="true">
              ∞
            </span>
            <span className="cmh-brand-text">
              <strong>Cloud Memory Hub</strong>
              <small>共享记忆中枢</small>
            </span>
          </button>
          {isMobileNav ? (
            <button
              type="button"
              className="cmh-icon-btn cmh-sidebar__close"
              aria-label="收起导航"
              onClick={() => setMobileMenuOpen(false)}
            >
              <X size={18} />
            </button>
          ) : null}
        </div>

        {/* Prominent Project / Scope Switcher */}
        <div className="cmh-switcher" ref={switcherRef}>
          <div className="cmh-switcher__label">当前记忆空间</div>
          <button
            type="button"
            className="cmh-switcher__trigger"
            aria-haspopup="listbox"
            aria-expanded={switcherOpen}
            aria-label="切换当前项目"
            onClick={() => setSwitcherOpen((open) => !open)}
          >
            <span className="cmh-switcher__icon" aria-hidden="true">
              {project?.scope_id === "global" ? (
                <Globe2 size={16} />
              ) : (
                <FolderKanban size={16} />
              )}
            </span>
            <span className="cmh-switcher__current">
              {project ? project.display_name : "选择或创建项目…"}
            </span>
            <ChevronDown size={15} className="cmh-switcher__chevron" />
          </button>

          {switcherOpen ? (
            <div className="cmh-switcher__popover" role="listbox" aria-label="项目列表">
              {bootstrap.projects.length > 4 ? (
                <div className="cmh-switcher__search">
                  <input
                    className="cmh-input cmh-input--sm"
                    placeholder="筛选项目名称…"
                    value={projectFilter}
                    onChange={(event) => setProjectFilter(event.target.value)}
                  />
                </div>
              ) : null}

              <div className="cmh-switcher__list">
                <div className="cmh-switcher__group-title">共享项目</div>
                {filteredProjects.length > 0 ? (
                  filteredProjects.map((row) => {
                    const selected = project?.scope_id === row.scope_id;
                    return (
                      <button
                        key={row.scope_id}
                        type="button"
                        role="option"
                        aria-selected={selected}
                        className={`cmh-switcher__option${selected ? " is-selected" : ""}`}
                        onClick={() => {
                          select(row.scope_id);
                          setSwitcherOpen(false);
                        }}
                      >
                        <FolderKanban size={15} />
                        <span>{row.display_name}</span>
                        {selected ? <Check size={14} /> : null}
                      </button>
                    );
                  })
                ) : (
                  <p className="cmh-switcher__empty">
                    {bootstrap.projects.length === 0
                      ? "暂无共享项目"
                      : "无匹配项目"}
                  </p>
                )}

                {bootstrap.next_offset !== null ? (
                  <button
                    type="button"
                    className="cmh-switcher__more"
                    disabled={loading}
                    onClick={moreProjects}
                  >
                    加载更多项目…
                  </button>
                ) : null}

                <div className="cmh-switcher__divider" />
                <div className="cmh-switcher__group-title">特殊范围</div>
                <button
                  type="button"
                  role="option"
                  aria-selected={project?.scope_id === "global"}
                  className={`cmh-switcher__option${project?.scope_id === "global" ? " is-selected" : ""}`}
                  onClick={() => {
                    select("global");
                    setSwitcherOpen(false);
                  }}
                >
                  <Globe2 size={15} />
                  <span>全局偏好（跨项目独立范围）</span>
                  {project?.scope_id === "global" ? <Check size={14} /> : null}
                </button>
              </div>

              <div className="cmh-switcher__footer">
                <button
                  type="button"
                  className="cmh-switcher__create"
                  onClick={() => {
                    setSwitcherOpen(false);
                    setCreateOpen(true);
                  }}
                >
                  <Plus size={15} />
                  新建共享项目
                </button>
              </div>
            </div>
          ) : null}
        </div>

        <nav className="cmh-nav" aria-label="主要功能导航">
          {NAV_ITEMS.map(({ id, label, Icon }) => {
            const active = view === id;
            return (
              <button
                key={id}
                type="button"
                className={`cmh-nav__item${active ? " is-active" : ""}`}
                aria-current={active ? "page" : undefined}
                onClick={() => {
                  onNavigate(id);
                  setMobileMenuOpen(false);
                }}
              >
                <Icon size={18} strokeWidth={1.85} />
                <span>{label}</span>
              </button>
            );
          })}
        </nav>

        {/* Single consolidated status pod (eliminates triple duplication) */}
        <div className="cmh-sidebar__footer">
          <div className="cmh-service-pod" aria-label="NAS 服务连接状态">
            <div className="cmh-service-pod__row">
              <span className="cmh-service-pod__status">
                <i
                  className={`cmh-dot${error ? " is-offline" : loading ? " is-loading" : " is-online"}`}
                />
                {error ? "无法连接 NAS" : loading ? "正在同步…" : "NAS 服务已连接"}
              </span>
              <span className="cmh-mono cmh-service-pod__ver">
                {bootstrap.version ? `v${bootstrap.version.replace(/^v/, "")}` : "dev"}
              </span>
            </div>
          </div>

          <div className="cmh-sidebar__links">
            <a
              href="https://github.com/Tsou92/Cloud-Memory-Hub#readme"
              target="_blank"
              rel="noreferrer"
              className="cmh-sidebar__link"
            >
              使用文档 <ArrowUpRight size={13} />
            </a>
            <button
              type="button"
              className="cmh-sidebar__link"
              onClick={() => void handleLogout()}
            >
              <LogOut size={13} /> 退出登录
            </button>
          </div>
        </div>
      </aside>

      <div className="cmh-main-area">
        <header className="cmh-topbar">
          <div className="cmh-topbar__left">
            <button
              type="button"
              className="cmh-icon-btn cmh-topbar__menu"
              aria-label={navToggleLabel}
              aria-expanded={isMobileNav ? mobileMenuOpen : !sidebarCollapsed}
              title={navToggleLabel}
              onClick={handleSidebarToggle}
            >
              {isMobileNav ? (
                <Menu size={19} />
              ) : sidebarCollapsed ? (
                <PanelLeftOpen size={18} />
              ) : (
                <PanelLeftClose size={18} />
              )}
            </button>
            <div className="cmh-breadcrumbs">
              <span className="cmh-breadcrumbs__scope">
                {project ? project.display_name : "未选择项目"}
              </span>
              <span className="cmh-breadcrumbs__sep">/</span>
              <span className="cmh-breadcrumbs__view">{currentNav.label}</span>
            </div>
          </div>

          <div className="cmh-topbar__right">
            <button
              type="button"
              className="cmh-exp-pill"
              onClick={() => setNoticeOpen(true)}
              title="查看实验版本与数据隐私边界说明"
            >
              <Info size={13} />
              <span>实验版本 · 数据说明</span>
            </button>
            <Button
              variant="ghost"
              size="sm"
              disabled={loading}
              onClick={refresh}
              aria-label="刷新服务与项目列表"
              title="刷新服务与项目列表"
            >
              <RefreshCw size={15} className={loading ? "cmh-spin" : ""} />
              <span className="cmh-hide-sm">刷新</span>
            </Button>
          </div>
        </header>

        <main id="main-content" className="cmh-viewport">
          {error ? (
            <div role="alert" className="cmh-alert cmh-alert--danger cmh-mb-4">
              <ShieldAlert size={16} />
              <span>{error}</span>
              {needsLogin ? (
                <a className="cmh-btn cmh-btn--secondary cmh-btn--sm" href="/hub/login">
                  重新登录
                </a>
              ) : null}
            </div>
          ) : null}

          {currentNav.projectScoped && !project ? (
            <WorkspaceOnboardingEmpty
              projects={bootstrap.projects}
              onSelect={select}
              onCreate={() => setCreateOpen(true)}
              onNavigate={onNavigate}
            />
          ) : (
            children({ openCreateProject: () => setCreateOpen(true) })
          )}
        </main>
      </div>

      <CreateProjectModal
        open={createOpen}
        instance={bootstrap.instance_id}
        currentProject={project}
        onClose={() => setCreateOpen(false)}
        onCreated={addProject}
        onRefreshProjects={refresh}
      />

      <Modal
        open={noticeOpen}
        title="实验版本与数据流向说明"
        subtitle="了解记忆存储在哪里、何时发给外部模型，以及备份边界"
        onClose={() => setNoticeOpen(false)}
        footer={
          <div className="cmh-inline-actions">
            <a
              className="cmh-btn cmh-btn--secondary"
              href="https://github.com/Tsou92/Cloud-Memory-Hub/blob/main/docs/data-and-privacy.md"
              target="_blank"
              rel="noreferrer"
            >
              阅读完整《数据会去到哪里》 <ArrowUpRight size={14} />
            </a>
            <Button variant="primary" onClick={() => setNoticeOpen(false)}>
              知道了
            </Button>
          </div>
        }
      >
        <div className="cmh-stack cmh-prose-sm">
          <p>
            <strong>请勿把这里当作唯一备份来源。</strong>{" "}
            Cloud Memory Hub 目前处于实验阶段，重要工作资料与原始文档仍应通过 Git 或独立备份保存。
          </p>
          <ul className="cmh-bullet-list">
            <li>
              <strong>存储位置：</strong>所有接纳的原文、提炼记忆、设备授权与加密后的模型密钥均保存在你自己的 NAS（或本机）Docker 数据卷中。
            </li>
            <li>
              <strong>外部模型调用：</strong>当你启用了外部记忆整理模型或使用「问记忆」时，经凭证过滤后的当前范围文本会发送给你所配置的模型服务商。
            </li>
            <li>
              <strong>离线读取缓存：</strong>客户端显式读取成功后会在本机保留 30 天明文缓存副本（权限 0600），Hub 上的忘记／删除不会主动广播清除离线设备上的旧缓存。
            </li>
          </ul>
        </div>
      </Modal>
    </div>
  );
}

function WorkspaceOnboardingEmpty({
  projects,
  onSelect,
  onCreate,
  onNavigate,
}: {
  projects: { scope_id: string; display_name: string }[];
  onSelect(scope: string): void;
  onCreate(): void;
  onNavigate(view: ConsoleView, tab?: SettingsTab): void;
}) {
  return (
    <section className="cmh-onboarding-hero" aria-label="选择或创建共享项目">
      <div className="cmh-onboarding-hero__head">
        <Badge tone="indigo">开始使用 Cloud Memory Hub</Badge>
        <h1>
          {projects.length > 0 ? "选择一个共享项目工作区" : "创建你的第一个共享项目"}
        </h1>
        <p>
          「项目」是多台设备与不同 AI Agent 共用的隔离记忆空间。选择同一项目的设备可以自动同步进展、接续任务。
        </p>
      </div>

      {projects.length > 0 ? (
        <div className="cmh-project-picker-grid">
          {projects.map((item) => (
            <button
              key={item.scope_id}
              type="button"
              className="cmh-project-card"
              onClick={() => onSelect(item.scope_id)}
            >
              <FolderKanban size={20} />
              <div>
                <strong>{item.display_name}</strong>
                <span>点击进入项目空间</span>
              </div>
            </button>
          ))}
          <button
            type="button"
            className="cmh-project-card cmh-project-card--new"
            onClick={onCreate}
          >
            <Plus size={20} />
            <div>
              <strong>新建另一个项目</strong>
              <span>为独立事务创建隔离空间</span>
            </div>
          </button>
        </div>
      ) : (
        <div className="cmh-steps-grid">
          <div className="cmh-step-card">
            <span className="cmh-step-num">01</span>
            <h3>创建共享项目</h3>
            <p>为正在推进的工作起一个名字，作为多端共享边界。</p>
            <Button variant="primary" onClick={onCreate}>
              <Plus size={15} /> 创建第一个项目
            </Button>
          </div>
          <div className="cmh-step-card">
            <span className="cmh-step-num">02</span>
            <h3>配对电脑上的 Agent</h3>
            <p>生成 10 分钟一次性链接，复制一句话交给 Claude Code 或 Codex 即可接入。</p>
            <Button variant="secondary" onClick={() => onNavigate("devices")}>
              前往设备接入
            </Button>
          </div>
          <div className="cmh-step-card">
            <span className="cmh-step-num">03</span>
            <h3>配置整理模型（可选）</h3>
            <p>无模型也可会话接续；配置 API 后可自动提炼长期结构化记忆。</p>
            <Button
              variant="secondary"
              onClick={() => onNavigate("settings", "model")}
            >
              配置模型服务
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
