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
  { id: "data-management", titleKey: "menu.dataManagement" },
  { id: "provider-wizard", titleKey: "providers.wizard.title" },
  { id: "logs", titleKey: "menu.logs" },
];

export const MENU_ROUTES: readonly RouteDefinition[] = ROUTES.filter(
  ({ id }) => id !== "claude-settings" && id !== "provider-wizard",
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
  if (route === "claude-settings") return "codex-settings";
  // The former Service Provider Management surfaces are integrated into the
  // unified provider workspace; legacy deep links land there.
  if (route === "relay-accounts") return "providers-models";
  if (route === "relay-add") return "provider-wizard";
  return route;
}
