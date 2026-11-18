import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  getHubBootstrap,
  type HubBootstrap,
  type HubProject,
  hubErrorMessage,
} from "../api/hub-api";

export interface HubState {
  bootstrap: HubBootstrap;
  project: HubProject | null;
  select(scope: string): void;
  addProject(project: HubProject): void;
  moreProjects(): void;
  refresh(): void;
  loading: boolean;
  error: string;
}

const Context = createContext<HubState | null>(null);

export function useHub(): HubState {
  const value = useContext(Context);
  if (!value) throw new Error("hub_context_required");
  return value;
}

const scopeKey = (instanceId: string) => `cmh_hub_scope:${instanceId}`;

function readStoredScope(instanceId: string): string {
  try {
    return localStorage.getItem(scopeKey(instanceId)) ?? "";
  } catch {
    return "";
  }
}

function writeStoredScope(instanceId: string, scope: string) {
  try {
    if (scope) localStorage.setItem(scopeKey(instanceId), scope);
    else localStorage.removeItem(scopeKey(instanceId));
  } catch {
    // Private browsing or disabled storage falls back to session memory.
  }
}

export function HubProvider({ children }: { children: ReactNode }) {
  const [bootstrap, setBootstrap] = useState<HubBootstrap | null>(null);
  const [scope, setScopeState] = useState("");
  const [offset, setOffset] = useState(0);
  const instance = useRef<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const select = useCallback((next: string) => {
    setScopeState(next);
    if (instance.current) writeStoredScope(instance.current, next);
  }, []);

  useEffect(() => {
    if (!bootstrap || scope) return;
    const stored = readStoredScope(bootstrap.instance_id);
    const known = [
      ...bootstrap.projects.map((row) => row.scope_id),
      bootstrap.global_scope.scope_id,
    ];
    if (stored && known.includes(stored)) {
      setScopeState(stored);
      return;
    }
    if (bootstrap.projects.length === 1) {
      select(bootstrap.projects[0].scope_id);
    }
  }, [bootstrap, scope, select]);

  useEffect(() => {
    void revision;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void getHubBootstrap(offset, controller.signal)
      .then(
        (next) => {
          if (controller.signal.aborted) return;
          if (offset > 0 && instance.current !== next.instance_id) {
            setScopeState("");
            setBootstrap(null);
            setError("服务实例已改变，请刷新项目列表后重新选择。");
            return;
          }
          instance.current = next.instance_id;
          setBootstrap((previous) => {
            if (offset === 0 || !previous) return next;
            const projects = new Map(
              previous.projects.map((row) => [row.scope_id, row]),
            );
            for (const row of next.projects) projects.set(row.scope_id, row);
            return { ...next, projects: [...projects.values()] };
          });
        },
        (reason) => {
          if (!controller.signal.aborted) setError(hubErrorMessage(reason));
        },
      )
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [offset, revision]);

  const refresh = useCallback(() => {
    setOffset(0);
    setRevision((v) => v + 1);
  }, []);

  if (!bootstrap) {
    return (
      <main className="cmh-boot-screen" aria-label="Cloud Memory Hub">
        <div className="cmh-boot-card">
          <div className="cmh-boot-logo" aria-hidden="true">
            ∞
          </div>
          <h1>Cloud Memory Hub</h1>
          {error ? (
            <>
              <p role="alert" className="cmh-alert cmh-alert--danger">
                {error}
              </p>
              <div className="cmh-boot-actions">
                <button
                  type="button"
                  className="cmh-btn cmh-btn--primary"
                  onClick={refresh}
                >
                  重试连接
                </button>
                <a className="cmh-btn cmh-btn--secondary" href="/hub/login">
                  前往登录页
                </a>
              </div>
            </>
          ) : (
            <p role="status" className="cmh-boot-status">
              <span className="cmh-spinner" aria-hidden="true" />
              正在连接 NAS 记忆中枢…
            </p>
          )}
        </div>
      </main>
    );
  }

  const project =
    [...bootstrap.projects, bootstrap.global_scope].find(
      (row) => row.scope_id === scope,
    ) ?? null;

  return (
    <Context.Provider
      value={{
        bootstrap,
        project,
        error,
        loading,
        refresh,
        select,
        addProject: (created) => {
          setBootstrap((previous) =>
            previous
              ? {
                  ...previous,
                  projects: [
                    ...previous.projects.filter(
                      (row) => row.scope_id !== created.scope_id,
                    ),
                    created,
                  ],
                }
              : previous,
          );
          select(created.scope_id);
        },
        moreProjects: () => {
          if (bootstrap.next_offset !== null) setOffset(bootstrap.next_offset);
        },
      }}
    >
      {children}
    </Context.Provider>
  );
}
