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
  openFilePicker(purpose: "import" | "claude-profile"): Promise<string | undefined>;
  saveFilePicker(purpose: "export"): Promise<string | undefined>;
  showConfirmation(title: string, message: string, confirmLabel: string): Promise<boolean>;
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
  setLaunchAtLogin(enabled: boolean): Promise<void>;
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
    saveFilePicker: ({ purpose }) => bridge.saveFilePicker(purpose),
    showConfirmation: ({ title, message, confirmLabel }) => bridge.showConfirmation(title, message, confirmLabel),
    chooseModelsToAdd: ({ models, providerName, keyName }) => bridge.chooseModelsToAdd(models, providerName, keyName),
    editSecureDocument: ({ editorToken, language, title }) => bridge.editSecureDocument(editorToken, language, title),
    editSecret: ({ domain, field, target, title, allowClear }) => bridge.editSecret(domain, field, target, title, allowClear),
    clearSecret: ({ domain, field, target }) => bridge.clearSecret(domain, field, target),
    setLaunchAtLogin: (enabled) => bridge.setLaunchAtLogin(enabled),
    setLocalization: (strings) => bridge.setLocalization(strings),
    setShortcuts: (shortcuts) => bridge.setShortcuts(shortcuts),
  };
}
