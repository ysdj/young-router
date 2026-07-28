import { I18nManager, NativeEventEmitter, NativeModules } from "react-native";
import { createTranslator } from "./i18n";
import { createIpcClient } from "./ipc";
import { createNativeIpcTransport, createNativeLeafBridgeAdapter, type NativeIpcBridge, type NativeLeafBridge } from "./platform/nativeBridge";
import { registerLiteLLMMenu } from "./bootstrap";
import type { AppRoute, LogTab, NativeLocalization, NativeMenuAction, ServiceStatus } from "./types";

type NativeModule = {
  send?: (request: string) => Promise<string>;
  shutdown?: () => void;
  openWindow?: (route: string) => void;
  closeWindow?: (route?: string) => void;
  focusWindow?: (route: string) => void;
  setWindowContentSize?: (width: number, height: number) => Promise<boolean>;
  setMenuBarStatus?: (title: string, running: boolean) => void;
  setMenuBarActions?: (actions: NativeMenuAction[]) => void;
  setTrayStatus?: (title: string, running: boolean) => void;
  setTrayActions?: (actions: NativeMenuAction[]) => void;
  openFilePicker?: (purpose: "import" | "claude-profile") => Promise<string | undefined>;
  saveFilePicker?: (purpose: "export") => Promise<string | undefined>;
  showConfirmation?: (title: string, message: string, confirmLabel: string) => Promise<boolean>;
  chooseModelsToAdd?: (models: string[], providerName: string, keyName: string) => Promise<string[] | undefined>;
  editSecureDocument?: (editorToken: string, language: "toml" | "json", title: string) => Promise<number | undefined>;
  editSecret?: (
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav",
    field: string,
    target: string | undefined,
    title: string,
    allowClear: boolean,
  ) => Promise<{ revision: number; present: boolean } | undefined>;
  clearSecret?: (
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav",
    field: string,
    target: string | undefined,
  ) => Promise<{ revision: number; present: boolean } | undefined>;
  setLocalization?: (strings: NativeLocalization) => void;
  systemLocale?: () => string;
  setLaunchAtLogin?: (enabled: boolean) => Promise<boolean>;
  quit?: () => void;
  setShortcuts?: (shortcuts: Record<string, string>) => void;
};

const core = NativeModules.LiteLLMCore as NativeModule | undefined;
const leaf = NativeModules.LiteLLMNativeLeaf as NativeModule | undefined;
if (!core?.send || !leaf) throw new Error("The LiteLLM Menu native host is unavailable.");

const coreEvents = new NativeEventEmitter(core as never);
const leafEvents = new NativeEventEmitter(leaf as never);
const systemLocale = leaf.systemLocale?.() ?? I18nManager.getConstants().localeIdentifier ?? "en";
const ipcBridge: NativeIpcBridge = {
  send: (request) => core.send!(request),
  subscribe: (listener) => {
    const subscription = coreEvents.addListener("coreEvent", listener);
    return () => subscription.remove();
  },
};

function call(method: keyof NativeModule, ...args: unknown[]): void {
  const target = leaf?.[method];
  if (typeof target === "function") (target as (...values: unknown[]) => void)(...args);
}

let nativeStrings: NativeLocalization | undefined;

function statusTitle(status: ServiceStatus): string {
  const title = nativeStrings?.appTitle ?? "LiteLLM Menu";
  return status.state === "running"
    ? title
    : `${title} - ${nativeStrings?.serviceUnavailable ?? "service unavailable"}`;
}

const nativeBridge: NativeLeafBridge = {
  openWindow: (route) => call("openWindow", route),
  closeWindow: (route) => call("closeWindow", route),
  focusWindow: (route) => call("focusWindow", route),
  setWindowContentSize: leaf.setWindowContentSize
    ? (width, height) => leaf.setWindowContentSize!(width, height)
    : undefined,
  setMenuBarStatus: (status) => call("setMenuBarStatus", statusTitle(status), status.state === "running"),
  setMenuBarActions: (actions) => call("setMenuBarActions", actions),
  setTrayStatus: (status) => call("setTrayStatus", statusTitle(status), status.state === "running"),
  setTrayActions: (actions) => call("setTrayActions", actions),
  openFilePicker: async (purpose) => leaf.openFilePicker?.(purpose),
  saveFilePicker: async (purpose) => leaf.saveFilePicker?.(purpose),
  showConfirmation: async (title, message, confirmLabel) => leaf.showConfirmation?.(title, message, confirmLabel) ?? false,
  chooseModelsToAdd: async (models, providerName, keyName) => leaf.chooseModelsToAdd?.(models, providerName, keyName),
  editSecureDocument: async (editorToken, language, title) => leaf.editSecureDocument?.(editorToken, language, title),
  editSecret: async (domain, field, target, title, allowClear) => leaf.editSecret?.(domain, field, target, title, allowClear),
  clearSecret: async (domain, field, target) => leaf.clearSecret?.(domain, field, target),
  setLaunchAtLogin: async (enabled) => {
    if (!leaf.setLaunchAtLogin) throw new Error("The native login-item control is unavailable.");
    if (!await leaf.setLaunchAtLogin(enabled)) throw new Error("The system could not update the login item.");
  },
  setLocalization: (strings) => {
    nativeStrings = strings;
    call("setLocalization", strings);
  },
  setShortcuts: (shortcuts) => call("setShortcuts", shortcuts),
};

const bootstrapTranslate = createTranslator("system", systemLocale);
const routeActions: NativeMenuAction[] = [
  ["providers-models", "menu.providers"], ["codex-settings", "menu.codex"], ["claude-settings", "menu.claude"],
  ["runtime-settings", "menu.runtime"], ["configuration-package", "menu.configuration"], ["webdav-settings", "menu.webdav"],
  ["logs", "menu.logs"],
].map(([route, key]) => ({ id: `open-${route}`, title: bootstrapTranslate(key), enabled: true }));

const native = createNativeLeafBridgeAdapter(nativeBridge);
native.menuBar.setActions(routeActions);
native.tray.setActions(routeActions);

const ipc = createIpcClient(createNativeIpcTransport(ipcBridge));

registerLiteLLMMenu("LiteLLMMenu", {
  ipc,
  native,
  translate: createTranslator("system", systemLocale),
  subscribeNativeAction: (listener) => {
    const subscription = leafEvents.addListener("menuAction", (action: string) => {
      listener(action);
    });
    return () => subscription.remove();
  },
});

export const DESKTOP_ROUTES: readonly AppRoute[] = ["home", "providers-models", "codex-settings", "claude-settings", "runtime-settings", "configuration-package", "webdav-settings", "logs"];
