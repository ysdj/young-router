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
  AppRegistry.registerComponent(componentName, () => function DesktopHost(props: { initialRoute?: AppRoute; initialLogTab?: LogTab }): React.JSX.Element {
    const [routeRequest, setRouteRequest] = useState<AppRoute>();
    const [logTabRequest, setLogTabRequest] = useState<LogTab>();
    const [nativeAction, setNativeAction] = useState<{ id: string; sequence: number }>();
    useEffect(() => dependencies.subscribeNativeAction?.((id) => {
      setNativeAction((current) => ({ id, sequence: (current?.sequence ?? 0) + 1 }));
    }), []);
    useEffect(() => {
      if (!nativeAction?.id.startsWith("open-")) return;
      const raw = nativeAction.id === "open-recovery" ? "logs" : nativeAction.id.slice(5);
      const [routeText, query] = raw.split("?", 2);
      const route = routeText as AppRoute;
      if (DESKTOP_ROUTES.includes(route)) {
        setRouteRequest(route);
        const tab = query?.startsWith("tab=") ? query.slice(4) as LogTab : undefined;
        if (route === "logs" && tab && LOG_TABS.includes(tab)) setLogTabRequest(tab);
      }
    }, [nativeAction]);
    return <LiteLLMMenuApp {...dependencies} routeRequest={routeRequest ?? props.initialRoute} logTabRequest={logTabRequest ?? props.initialLogTab} nativeAction={nativeAction} />;
  });
}

const DESKTOP_ROUTES: readonly AppRoute[] = ["home", "providers-models", "codex-settings", "claude-settings", "runtime-settings", "configuration-package", "webdav-settings", "logs"];
const LOG_TABS: readonly LogTab[] = ["requests", "service", "menu", "route-trace", "recovery", "online-usage"];
