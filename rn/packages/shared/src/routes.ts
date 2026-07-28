import type { AppRoute } from "./types";

export interface RouteDefinition {
  id: AppRoute;
  titleKey: string;
  nativeWindow: boolean;
}

export const ROUTES: readonly RouteDefinition[] = [
  { id: "providers-models", titleKey: "menu.providers", nativeWindow: true },
  { id: "codex-settings", titleKey: "menu.codex", nativeWindow: true },
  { id: "claude-settings", titleKey: "menu.claude", nativeWindow: true },
  { id: "runtime-settings", titleKey: "menu.runtime", nativeWindow: true },
  { id: "configuration-package", titleKey: "menu.configuration", nativeWindow: true },
  { id: "webdav-settings", titleKey: "menu.webdav", nativeWindow: true },
  { id: "logs", titleKey: "menu.logs", nativeWindow: true },
];

export const LOG_TABS = [
  "requests",
  "service",
  "menu",
  "route-trace",
  "recovery",
  "online-usage",
] as const;
