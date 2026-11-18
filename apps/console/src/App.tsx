import { useCallback, useEffect, useState } from "react";
import { Shell } from "./components/Shell";
import { HubProvider, useHub } from "./context/HubContext";
import {
  type ConsoleView,
  parseRoute,
  pushRoute,
  type RouteState,
  type SettingsTab,
} from "./router";
import { AskView } from "./views/AskView";
import { DevicesView } from "./views/DevicesView";
import { HistoryView } from "./views/HistoryView";
import { MemoriesView } from "./views/MemoriesView";
import { OverviewView } from "./views/OverviewView";
import { SettingsView } from "./views/SettingsView";

export function App() {
  return (
    <HubProvider>
      <ConsoleRouter />
    </HubProvider>
  );
}

function ConsoleRouter() {
  const { bootstrap, project } = useHub();
  const [route, setRoute] = useState<RouteState>(() =>
    parseRoute(window.location.search),
  );

  useEffect(() => {
    const sync = () => setRoute(parseRoute(window.location.search));
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  const navigate = useCallback((view: ConsoleView, tab?: SettingsTab) => {
    setRoute(pushRoute({ view, tab }));
    const viewport = document.getElementById("main-content");
    if (viewport) viewport.scrollTo?.({ top: 0 });
  }, []);

  return (
    <Shell view={route.view} onNavigate={navigate}>
      {({ openCreateProject }) => {
        if (route.view === "devices") {
          return (
            <DevicesView
              key={`${bootstrap.instance_id}:${project?.scope_id ?? "none"}`}
              instance={bootstrap.instance_id}
              project={project}
              onOpenCreateProject={openCreateProject}
            />
          );
        }
        if (route.view === "settings") {
          return (
            <SettingsView
              tab={route.tab}
              onSelectTab={(nextTab) => navigate("settings", nextTab)}
            />
          );
        }
        if (!project) return null;
        const scopeKey = `${bootstrap.instance_id}:${project.scope_id}`;
        if (route.view === "memories") {
          return (
            <MemoriesView
              key={scopeKey}
              project={project}
              onNavigate={navigate}
            />
          );
        }
        if (route.view === "history") {
          return <HistoryView key={scopeKey} project={project} />;
        }
        if (route.view === "ask") {
          return <AskView key={scopeKey} project={project} />;
        }
        return (
          <OverviewView
            key={scopeKey}
            project={project}
            onNavigate={navigate}
          />
        );
      }}
    </Shell>
  );
}
