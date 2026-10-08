import { I18nManager, NativeEventEmitter, NativeModules } from "react-native";
import { createTranslator } from "./i18n";
import { createIpcClient } from "./ipc";
import { createNativeIpcTransport, createNativeLeafBridgeAdapter, type NativeIpcBridge, type NativeLeafBridge } from "./platform/nativeBridge";
import { routeMenuActions } from "./routes";
import { registerYoungRouter } from "./bootstrap";
import type { LanguagePreference, NativeLocalization, NativeMenuAction, NativeMenuAnchor, RelayGroupManagerGroup, RelayGroupManagerKey, RelayGroupManagerLabels, RelayGroupManagerResult, RelayGroupManagerSnapshot, ServiceStatus } from "./types";

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
  showGroupedActionMenu?: (title: string, groups: Array<{ title: string; items: string[] }>, anchor: NativeMenuAnchor) => Promise<{ group: number; item: number } | undefined>;
  showConfirmation?: (title: string, message: string, confirmLabel: string, cancelLabel: string, destructive: boolean) => Promise<boolean>;
  showReadOnlyText?: (title: string, text: string, closeLabel: string, language: "json" | "toml" | "text", html: string) => Promise<void>;
  showProviderAuth?: (options: {
    provider: "openai" | "claude";
    fingerprint?: string;
    verificationURL: string;
    userCode?: string;
    callbackURL?: string;
    title: string;
    closeLabel: string;
  }) => Promise<void>;
  chooseModelsToAdd?: (models: string[], providerName: string, keyName: string) => Promise<string[] | undefined>;
  showGroupManager?: (options: {
    title: string;
    accountLabel: string;
    accountId: string;
    groups: RelayGroupManagerGroup[];
    keys: RelayGroupManagerKey[];
    labels: RelayGroupManagerLabels;
    autoGrouping: boolean;
    loading?: boolean;
  }) => Promise<RelayGroupManagerResult | undefined>;
  awaitGroupManagerApply?: () => Promise<RelayGroupManagerResult | undefined>;
  finishGroupManagerApply?: (options: { status: string; close: boolean }) => Promise<void>;
  updateGroupManager?: (options: RelayGroupManagerSnapshot) => Promise<boolean>;
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
  copySecret?: (domain: "providers_models" | "relay_accounts", field: "api_key", target: string) => Promise<boolean>;
  relayLogin?: (options: {
    accountId: string;
    /** "" settles the station family from the page's own answer. */
    type: "newapi" | "sub2api" | "";
    label: string;
    origin: string;
    language: LanguagePreference;
    username?: string;
  }) => Promise<{ revision: number; loginStatus: "signed_in"; username: string } | undefined>;
  cancelRelayLogin?: () => void;
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
  showVersion?: () => void;
  versionInfo?: () => Promise<{ app: string; litellm: string; icon?: string }>;
  openExternalURL?: (url: string) => void;
  revealFile?: (path: string) => void;
  openFileEditor?: (target: string) => void;
  prepareFileEditor?: () => void;
  pendingFileEditorTarget?: () => string;
  quit?: () => void;
  setShortcuts?: (shortcuts: Record<string, string>) => void;
};

const core = NativeModules.LiteLLMCore as NativeModule | undefined;
const leaf = NativeModules.LiteLLMNativeLeaf as NativeModule | undefined;
if (!core?.send || !leaf) throw new Error("The Young Router native host is unavailable.");

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
  showGroupedActionMenu: async (title, groups, anchor) => leaf.showGroupedActionMenu?.(title, groups, anchor),
  showConfirmation: async (title, message, confirmLabel, cancelLabel, destructive) => leaf.showConfirmation?.(title, message, confirmLabel, cancelLabel, destructive === true) ?? false,
  showReadOnlyText: async (title, text, closeLabel, language, html) => {
    if (!leaf.showReadOnlyText) throw new Error("The native code viewer is unavailable.");
    await leaf.showReadOnlyText(title, text, closeLabel, language, html);
  },
  showProviderAuth: leaf.showProviderAuth
    ? async (options) => { await leaf.showProviderAuth!(options); }
    : undefined,
  chooseModelsToAdd: async (models, providerName, keyName) => leaf.chooseModelsToAdd?.(models, providerName, keyName),
  showGroupManager: async (options) => leaf.showGroupManager?.(options),
  awaitGroupManagerApply: leaf.awaitGroupManagerApply ? () => leaf.awaitGroupManagerApply!() : undefined,
  finishGroupManagerApply: leaf.finishGroupManagerApply
    ? async (options) => { await leaf.finishGroupManagerApply!(options); }
    : undefined,
  updateGroupManager: leaf.updateGroupManager
    ? async (options) => leaf.updateGroupManager!(options)
    : undefined,
  editSecret: async (domain, field, target, title, allowClear) => leaf.editSecret?.(domain, field, target, title, allowClear),
  clearSecret: async (domain, field, target) => leaf.clearSecret?.(domain, field, target),
  copySecret: async (domain, field, target) => leaf.copySecret?.(domain, field, target) ?? false,
  relayLogin: async (options) => leaf.relayLogin?.(options),
  cancelRelayLogin: () => leaf.cancelRelayLogin?.(),
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
  showVersion: () => call("showVersion"),
  versionInfo: leaf.versionInfo ? () => leaf.versionInfo!() : undefined,
  openExternalURL: (url) => call("openExternalURL", url),
  revealFile: (path) => call("revealFile", path),
  openFileEditor: (target) => call("openFileEditor", target),
  prepareFileEditor: () => call("prepareFileEditor"),
  pendingFileEditorTarget: () => (typeof leaf.pendingFileEditorTarget === "function" ? leaf.pendingFileEditorTarget() : ""),
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

registerYoungRouter("YoungRouter", {
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
