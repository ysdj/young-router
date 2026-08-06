import type {
  AppRoute,
  IpcEvent,
  IpcMethod,
  IpcRequest,
  IpcResponse,
  IpcTransport,
  NativeLeafAdapter,
  NativeLocalization,
  NativeMenuAction,
  NativeMenuAnchor,
  LanguagePreference,
  ServiceStatus,
} from "../types";

export interface NativeIpcBridge {
  send(request: string): Promise<string>;
  subscribe(listener: (event: string) => void): () => void;
}

export interface NativeLeafBridge {
  openWindow(route: AppRoute): void;
  closeWindow(route?: AppRoute): void;
  focusWindow(route: AppRoute): void;
  setWindowContentSize?(width: number, height: number): Promise<boolean>;
  setMenuBarStatus(status: ServiceStatus): void;
  setMenuBarActions(actions: NativeMenuAction[]): void;
  setTrayStatus(status: ServiceStatus): void;
  setTrayActions(actions: NativeMenuAction[]): void;
  openFilePicker(purpose: "import"): Promise<string | undefined>;
  saveFilePicker(suggestedName: string): Promise<string | undefined>;
  showActionMenu(title: string, items: string[], anchor: NativeMenuAnchor): Promise<number | undefined>;
  showConfirmation(title: string, message: string, confirmLabel: string): Promise<boolean>;
  showCodexRestartConfirmation(title: string, message: string, restartLabel: string, laterLabel: string): Promise<"restart" | "later" | undefined>;
  showReadOnlyText(title: string, text: string, closeLabel: string): Promise<void>;
  chooseModelsToAdd(models: string[], providerName: string, keyName: string): Promise<string[] | undefined>;
  editSecureDocument(editorToken: string, language: "toml" | "json", title: string): Promise<number | undefined>;
  editSecret(
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav",
    field: string,
    target: string | undefined,
    title: string,
    allowClear: boolean,
  ): Promise<{ revision: number; present: boolean } | undefined>;
  clearSecret(
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav",
    field: string,
    target: string | undefined,
  ): Promise<{ revision: number; present: boolean } | undefined>;
  relayLogin(options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    language: LanguagePreference;
    username?: string;
    rememberPassword: boolean;
  }): Promise<{ revision: number; loginStatus: "signed_in"; username: string } | undefined>;
  restoreRelaySession(options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    username?: string;
  }): Promise<{ revision: number; loginStatus: "signed_in" | "signed_out" | "expired"; username: string } | undefined>;
  openRelayLogs(options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    language: LanguagePreference;
  }): Promise<void>;
  clearRelayPassword(accountId: string): Promise<void>;
  clearRelayCredentials(accountId: string): Promise<void>;
  setLaunchAtLogin(enabled: boolean): Promise<void>;
  restartCodex(): Promise<boolean>;
  setLocalization(strings: NativeLocalization): void;
  setShortcuts(shortcuts: Record<string, string>): void;
}

function parseJson<T>(json: string, label: string): T {
  try {
    return JSON.parse(json) as T;
  } catch {
    throw new Error(`Native ${label} payload is invalid.`);
  }
}

export function createNativeIpcTransport(bridge: NativeIpcBridge): IpcTransport {
  return {
    async send<M extends IpcMethod>(request: IpcRequest<M>): Promise<IpcResponse<M>> {
      return parseJson<IpcResponse<M>>(await bridge.send(JSON.stringify(request)), "IPC response");
    },
    subscribe(listener: (event: IpcEvent) => void): () => void {
      return bridge.subscribe((event) => listener(parseJson<IpcEvent>(event, "IPC event")));
    },
  };
}

export function createNativeLeafBridgeAdapter(bridge: NativeLeafBridge): NativeLeafAdapter {
  return {
    window: {
      open: (route) => bridge.openWindow(route),
      close: (route) => bridge.closeWindow(route),
      focus: (route) => bridge.focusWindow(route),
      setContentSize: bridge.setWindowContentSize
        ? (width, height) => bridge.setWindowContentSize!(width, height)
        : undefined,
    },
    menuBar: {
      setStatus: (status) => bridge.setMenuBarStatus(status),
      setActions: (actions) => bridge.setMenuBarActions(actions),
    },
    tray: {
      setStatus: (status) => bridge.setTrayStatus(status),
      setActions: (actions) => bridge.setTrayActions(actions),
    },
    openFilePicker: ({ purpose }) => bridge.openFilePicker(purpose),
    saveFilePicker: ({ suggestedName }) => bridge.saveFilePicker(suggestedName),
    showActionMenu: ({ title, items, anchor }) => bridge.showActionMenu(title, items, anchor),
    showConfirmation: ({ title, message, confirmLabel }) => bridge.showConfirmation(title, message, confirmLabel),
    showCodexRestartConfirmation: ({ title, message, restartLabel, laterLabel }) => bridge.showCodexRestartConfirmation(title, message, restartLabel, laterLabel),
    showReadOnlyText: ({ title, text, closeLabel }) => bridge.showReadOnlyText(title, text, closeLabel),
    chooseModelsToAdd: ({ models, providerName, keyName }) => bridge.chooseModelsToAdd(models, providerName, keyName),
    editSecureDocument: ({ editorToken, language, title }) => bridge.editSecureDocument(editorToken, language, title),
    editSecret: ({ domain, field, target, title, allowClear }) => bridge.editSecret(domain, field, target, title, allowClear),
    clearSecret: ({ domain, field, target }) => bridge.clearSecret(domain, field, target),
    relayLogin: (options) => bridge.relayLogin(options),
    restoreRelaySession: (options) => bridge.restoreRelaySession(options),
    openRelayLogs: (options) => bridge.openRelayLogs(options),
    clearRelayPassword: (accountId) => bridge.clearRelayPassword(accountId),
    clearRelayCredentials: (accountId) => bridge.clearRelayCredentials(accountId),
    setLaunchAtLogin: (enabled) => bridge.setLaunchAtLogin(enabled),
    restartCodex: () => bridge.restartCodex(),
    setLocalization: (strings) => bridge.setLocalization(strings),
    setShortcuts: (shortcuts) => bridge.setShortcuts(shortcuts),
  };
}
