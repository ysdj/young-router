import type { AppRoute, LogTab } from "./types";

export interface RouteDefinition {
  id: AppRoute;
  titleKey: string;
}

export const ROUTES: readonly RouteDefinition[] = [
  { id: "providers-models", titleKey: "menu.providers" },
  { id: "runtime-settings", titleKey: "menu.runtime" },
  { id: "codex-settings", titleKey: "menu.codex" },
  { id: "claude-settings", titleKey: "menu.claude" },
  { id: "relay-accounts", titleKey: "menu.relay" },
  { id: "data-management", titleKey: "menu.dataManagement" },
  { id: "relay-add", titleKey: "relay.addAccount" },
  { id: "logs", titleKey: "menu.logs" },
];

export const MENU_ROUTES: readonly RouteDefinition[] = ROUTES.filter(
  ({ id }) => id !== "claude-settings" && id !== "relay-add",
);

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
  return route === "claude-settings" ? "codex-settings" : route;
}
