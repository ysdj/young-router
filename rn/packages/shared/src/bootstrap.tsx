import React, { useEffect, useState } from "react";
import { AppRegistry } from "react-native";
import { LiteLLMMenuApp } from "./ui/LiteLLMMenuApp";
import type { AppRoute, IpcClient, LogTab, NativeLeafAdapter } from "./types";

export interface DesktopHostDependencies {
  ipc: IpcClient;
  native: NativeLeafAdapter;
  translate: (key: string, values?: Record<string, string | number>) => string;
  subscribeNativeAction?: (listener: (action: string) => void) => () => void;
}

export function registerLiteLLMMenu(componentName: string, dependencies: DesktopHostDependencies): void {
  AppRegistry.registerComponent(componentName, () => function DesktopHost(props: { initialRoute?: AppRoute; initialLogTab?: LogTab; isPrimaryHost?: boolean; isWindowManagerHost?: boolean }): React.JSX.Element {
    const isPrimaryHost = props.isPrimaryHost !== false;
    // Every route window has its own React root, but all roots in the desktop
    // process share this IPC client. Seed the new root before its first render
    // so native tables are built once with real rows instead of an empty shell.
    const [initialSnapshot] = useState(() => dependencies.ipc.latestSnapshot());
    const [routeRequest, setRouteRequest] = useState<AppRoute>();
    const [routeRequestSequence, setRouteRequestSequence] = useState(0);
    const [logTabRequest, setLogTabRequest] = useState<LogTab>();
    const [nativeAction, setNativeAction] = useState<{ id: string; sequence: number }>();
    useEffect(() => dependencies.subscribeNativeAction?.((id) => {
      setNativeAction((current) => ({ id, sequence: (current?.sequence ?? 0) + 1 }));
    }), []);
    useEffect(() => {
      if (!nativeAction?.id.startsWith("open-")) return;
      const raw = nativeAction.id === "open-recovery" ? "logs?tab=recovery" : nativeAction.id.slice(5);
      const [routeText, query] = raw.split("?", 2);
      const route = routeText as AppRoute;
      if (DESKTOP_ROUTES.includes(route)) {
        const tab = query?.startsWith("tab=") ? query.slice(4) as LogTab : undefined;
        if (route === "logs") setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");
        const requestedWindow = canonicalWindowRoute(route);
        const currentWindow = canonicalWindowRoute(props.initialRoute ?? "home");
        if (!isPrimaryHost && requestedWindow !== currentWindow) return;
        setRouteRequest(route);
        setRouteRequestSequence((current) => current + 1);
      }
    }, [isPrimaryHost, nativeAction, props.initialRoute]);
    return <LiteLLMMenuApp {...dependencies} initialSnapshot={initialSnapshot} isPrimaryHost={isPrimaryHost} isWindowManagerHost={props.isWindowManagerHost === true} routeRequest={routeRequest ?? props.initialRoute} routeRequestSequence={routeRequestSequence} logTabRequest={logTabRequest ?? props.initialLogTab} nativeAction={nativeAction} />;
  });
}

const DESKTOP_ROUTES: readonly AppRoute[] = ["home", "providers-models", "codex-settings", "claude-settings", "runtime-settings", "webdav-settings", "relay-accounts", "relay-add", "logs"];
const LOG_TABS: readonly LogTab[] = ["requests", "service", "menu", "route-trace", "recovery", "online-usage"];

function canonicalWindowRoute(route: AppRoute): AppRoute {
  return route === "claude-settings" ? "codex-settings" : route;
}
