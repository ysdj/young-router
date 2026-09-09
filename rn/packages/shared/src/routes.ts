import type { AppRoute, LogTab } from "./types";

export interface RouteDefinition {
  id: AppRoute;
  titleKey: string;
}

export const ROUTES: readonly RouteDefinition[] = [
  { id: "general-settings", titleKey: "menu.general" },
  { id: "providers-models", titleKey: "menu.providers" },
  { id: "runtime-settings", titleKey: "menu.runtime" },
  { id: "codex-settings", titleKey: "menu.codex" },
  { id: "claude-settings", titleKey: "menu.claude" },
  { id: "data-management", titleKey: "menu.dataManagement" },
  { id: "provider-wizard", titleKey: "providers.wizard.title" },
  { id: "logs", titleKey: "menu.logs" },
];

export const MENU_ROUTES: readonly RouteDefinition[] = ROUTES.filter(
  ({ id }) => id !== "claude-settings" && id !== "provider-wizard",
);

/**
 * Panes of the single settings window. The native hosts keep one window for
 * every entry here; the shared settings shell renders the left menu list and
 * the matching detail surface on the right.
 */
export const SETTINGS_PANES: readonly RouteDefinition[] = MENU_ROUTES;

export const SETTINGS_PANE_ROUTES: readonly AppRoute[] = SETTINGS_PANES.map(({ id }) => id);

/**
 * Sidebar presentation for the settings shell. ``symbol`` is an SF Symbol on
 * macOS and maps to a Segoe Fluent Icons glyph on Windows; ``color`` is the
 * sRGB badge color behind the symbol; ``image`` is the bundled icon tile the
 * hosts prefer when it is available.
 */
export const SETTINGS_PANE_PRESENTATION: Partial<Record<AppRoute, { symbol: string; color: string; image: string }>> = {
  "general-settings": { symbol: "slider.horizontal.3", color: "#34C759", image: "SidebarGeneral" },
  "providers-models": { symbol: "square.stack.3d.up", color: "#0A84FF", image: "SidebarProviders" },
  "runtime-settings": { symbol: "gearshape.2", color: "#8E8E93", image: "SidebarRuntime" },
  "codex-settings": { symbol: "terminal", color: "#5E5CE6", image: "SidebarCodex" },
  "data-management": { symbol: "arrow.up.arrow.down", color: "#30B0C7", image: "SidebarData" },
  "logs": { symbol: "list.bullet.rectangle", color: "#FF9F0A", image: "SidebarLogs" },
};

export function isSettingsPaneRoute(route: AppRoute | undefined): boolean {
  return route !== undefined && SETTINGS_PANE_ROUTES.includes(route);
}

/** Every non-home, non-sheet route renders inside the settings shell. */
export function isSettingsShellRoute(route: AppRoute | undefined): boolean {
  return route !== undefined && route !== "home" && route !== "provider-wizard";
}

export function routeMenuActions(
  translate: (key: string) => string,
): Array<{ id: string; title: string; enabled: true }> {
  return MENU_ROUTES.map(({ id, titleKey }) => ({
    id: `open-${id}`,
    title: translate(titleKey),
    enabled: true,
  }));
}

export const DESKTOP_ROUTES: readonly AppRoute[] = ["home", ...ROUTES.map(({ id }) => id)];

export const LOG_TABS: readonly LogTab[] = [
  "requests",
  "service",
  "menu",
  "route-trace",
  "recovery",
  "online-usage",
];

export function canonicalWindowRoute(route: AppRoute): AppRoute {
  if (route === "claude-settings") return "codex-settings";
  // The former Service Provider Management surfaces are integrated into the
  // unified provider workspace; legacy deep links land there.
  if (route === "relay-accounts") return "providers-models";
  if (route === "relay-add") return "provider-wizard";
  return route;
}
