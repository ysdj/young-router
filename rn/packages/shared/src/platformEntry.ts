import { I18nManager, NativeEventEmitter, NativeModules } from "react-native";
import { createTranslator } from "./i18n";
import { createIpcClient } from "./ipc";
import { createNativeIpcTransport, createNativeLeafBridgeAdapter, type NativeIpcBridge, type NativeLeafBridge } from "./platform/nativeBridge";
import { routeMenuActions } from "./routes";
import { registerLiteLLMMenu } from "./bootstrap";
import type { LanguagePreference, NativeLocalization, NativeMenuAction, NativeMenuAnchor, ServiceStatus } from "./types";

type NativeModule = {
  send?: (request: string) => Promise<string>;
  shutdown?: () => void;
  openWindow?: (route: string) => void;
  closeWindow?: (route?: string) => void;
  focusWindow?: (route: string) => void;
  setWindowContentSize?: (route: string, width: number, height: number) => Promise<boolean>;
  setMenuBarStatus?: (title: string, running: boolean) => void;
  setMenuBarActions?: (actions: NativeMenuAction[]) => void;
  setTrayStatus?: (title: string, running: boolean) => void;
  setTrayActions?: (actions: NativeMenuAction[]) => void;
  openFilePicker?: (purpose: "import") => Promise<string | undefined>;
  saveFilePicker?: (suggestedName: string) => Promise<string | undefined>;
  showActionMenu?: (title: string, items: string[], anchor: NativeMenuAnchor) => Promise<number | undefined>;
  showConfirmation?: (title: string, message: string, confirmLabel: string) => Promise<boolean>;
  showReadOnlyText?: (title: string, text: string, closeLabel: string, language: "json" | "toml" | "text", html: string) => Promise<void>;
  showCodexRestartConfirmation?: (title: string, message: string, restartLabel: string, laterLabel: string) => Promise<"restart" | "later" | undefined>;
  chooseModelsToAdd?: (models: string[], providerName: string, keyName: string) => Promise<string[] | undefined>;
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
  copySecret?: (domain: "relay_accounts", field: "api_key", target: string) => Promise<boolean>;
  relayLogin?: (options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    language: LanguagePreference;
    username?: string;
    rememberPassword: boolean;
  }) => Promise<{ revision: number; loginStatus: "signed_in"; username: string } | undefined>;
  openRelayLogs?: (options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    language: LanguagePreference;
  }) => Promise<void>;
  restoreRelaySession?: (options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    username?: string;
  }) => Promise<{ revision: number; loginStatus: "signed_in" | "signed_out" | "expired"; username: string } | undefined>;
  clearRelayPassword?: (accountId: string) => Promise<void>;
  clearRelayCredentials?: (accountId: string) => Promise<void>;
  setLocalization?: (strings: NativeLocalization) => void;
  systemLocale?: () => string;
  setLaunchAtLogin?: (enabled: boolean) => Promise<boolean>;
  restartCodex?: () => Promise<boolean>;
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
  const states: Record<ServiceStatus["state"], string | undefined> = {
    starting: nativeStrings?.serviceStarting,
    running: nativeStrings?.serviceRunning,
    unhealthy: nativeStrings?.serviceUnhealthy,
    stopped: nativeStrings?.serviceStopped,
    unknown: nativeStrings?.serviceUnknown,
  };
  const state = status.state === "running" && typeof status.port === "number" &&
    Number.isInteger(status.port) && status.port >= 1 && status.port <= 65535
    ? (nativeStrings?.serviceRunningOnPort ?? "Running (port {port})").replace("{port}", String(status.port))
    : states[status.state] ?? status.state;
  return (nativeStrings?.serviceStatus ?? "Status: {status}").replace("{status}", state);
}

const nativeBridge: NativeLeafBridge = {
  openWindow: (route) => call("openWindow", route),
  closeWindow: (route) => call("closeWindow", route),
  focusWindow: (route) => call("focusWindow", route),
  setWindowContentSize: leaf.setWindowContentSize
    ? (route, width, height) => leaf.setWindowContentSize!(route, width, height)
    : undefined,
  setMenuBarStatus: (status) => call("setMenuBarStatus", statusTitle(status), status.state === "running"),
  setMenuBarActions: (actions) => call("setMenuBarActions", actions),
  setTrayStatus: (status) => call("setTrayStatus", statusTitle(status), status.state === "running"),
  setTrayActions: (actions) => call("setTrayActions", actions),
  openFilePicker: async (purpose) => leaf.openFilePicker?.(purpose),
  saveFilePicker: async (suggestedName) => leaf.saveFilePicker?.(suggestedName),
  showActionMenu: async (title, items, anchor) => leaf.showActionMenu?.(title, items, anchor),
  showConfirmation: async (title, message, confirmLabel) => leaf.showConfirmation?.(title, message, confirmLabel) ?? false,
  showReadOnlyText: async (title, text, closeLabel, language, html) => {
    if (!leaf.showReadOnlyText) throw new Error("The native code viewer is unavailable.");
    await leaf.showReadOnlyText(title, text, closeLabel, language, html);
  },
  showCodexRestartConfirmation: async (title, message, restartLabel, laterLabel) => leaf.showCodexRestartConfirmation?.(title, message, restartLabel, laterLabel),
  chooseModelsToAdd: async (models, providerName, keyName) => leaf.chooseModelsToAdd?.(models, providerName, keyName),
  editSecret: async (domain, field, target, title, allowClear) => leaf.editSecret?.(domain, field, target, title, allowClear),
  clearSecret: async (domain, field, target) => leaf.clearSecret?.(domain, field, target),
  copySecret: async (domain, field, target) => leaf.copySecret?.(domain, field, target) ?? false,
  relayLogin: async (options) => leaf.relayLogin?.(options),
  openRelayLogs: async (options) => { await leaf.openRelayLogs?.(options); },
  restoreRelaySession: async (options) => leaf.restoreRelaySession?.(options),
  clearRelayPassword: async (accountId) => {
    if (!leaf.clearRelayPassword) throw new Error("The native relay credential store is unavailable.");
    await leaf.clearRelayPassword(accountId);
  },
  clearRelayCredentials: async (accountId) => {
    if (!leaf.clearRelayCredentials) throw new Error("The native relay credential store is unavailable.");
    await leaf.clearRelayCredentials(accountId);
  },
  setLaunchAtLogin: async (enabled) => {
    if (!leaf.setLaunchAtLogin) throw new Error("The native login-item control is unavailable.");
    if (!await leaf.setLaunchAtLogin(enabled)) throw new Error("The system could not update the login item.");
  },
  restartCodex: async () => leaf.restartCodex?.() ?? false,
  setLocalization: (strings) => {
    nativeStrings = strings;
    call("setLocalization", strings);
  },
  setShortcuts: (shortcuts) => call("setShortcuts", shortcuts),
};

const bootstrapTranslate = createTranslator("system", systemLocale);
const routeActions: NativeMenuAction[] = routeMenuActions(bootstrapTranslate);

const native = createNativeLeafBridgeAdapter(nativeBridge);
native.menuBar.setActions(routeActions);

const ipc = createIpcClient(createNativeIpcTransport(ipcBridge));

// Start the single shared Core read while React Native is mounting. New route
// windows can then use this cached snapshot synchronously on their first frame.
void ipc.snapshot().catch(() => undefined);

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
