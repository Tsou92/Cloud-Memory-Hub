export type ConsoleView =
  | "overview"
  | "memories"
  | "history"
  | "ask"
  | "devices"
  | "settings";

export type SettingsTab = "model" | "usage" | "backups" | "security";

export interface RouteState {
  view: ConsoleView;
  tab: SettingsTab;
}

const VIEWS = new Set<ConsoleView>([
  "overview",
  "memories",
  "history",
  "ask",
  "devices",
  "settings",
]);

const LEGACY_VIEW_ALIASES: Record<string, ConsoleView> = {
  home: "overview",
  library: "memories",
  connections: "devices",
};

const TABS = new Set<SettingsTab>(["model", "usage", "backups", "security"]);

export function parseRoute(search: string): RouteState {
  const params = new URLSearchParams(search);
  const rawView = params.get("view") ?? "";
  const mapped = LEGACY_VIEW_ALIASES[rawView] ?? rawView;
  const view: ConsoleView = VIEWS.has(mapped as ConsoleView)
    ? (mapped as ConsoleView)
    : "overview";
  const rawTab = params.get("tab") ?? "";
  const tab: SettingsTab = TABS.has(rawTab as SettingsTab)
    ? (rawTab as SettingsTab)
    : "model";
  return { view, tab };
}

export function pushRoute(next: {
  view: ConsoleView;
  tab?: SettingsTab;
}): RouteState {
  const current = parseRoute(window.location.search);
  const tab = next.tab ?? (next.view === "settings" ? current.tab : "model");
  const url = new URL(window.location.href);
  if (next.view === "overview") {
    url.searchParams.delete("view");
  } else {
    url.searchParams.set("view", next.view);
  }
  if (next.view === "settings" && tab !== "model") {
    url.searchParams.set("tab", tab);
  } else {
    url.searchParams.delete("tab");
  }
  if (url.href !== window.location.href) {
    window.history.pushState({}, "", url);
  }
  return { view: next.view, tab };
}
