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
  RelayGroupManagerLabels,
  RelayGroupManagerResult,
  RelayGroupManagerSnapshot,
  ServiceStatus,
} from "../types";

export interface NativeIpcBridge {
  send(request: string): Promise<string>;
  subscribe(listener: (event: string) => void): () => void;
  /**
   * A host-only route beside the protocol, when this host has one.
   *
   * The browser host owns two values that no configuration domain owns — the
   * port it listens on and the password that opens it — so they travel on
   * their own route.  Neither native host serves a page, so neither has one.
   */
  hostRoute?(path: string, body: Record<string, unknown>): Promise<Record<string, unknown>>;
}

export interface NativeLeafBridge {
  openWindow(route: AppRoute): void;
  closeWindow(route?: AppRoute): void;
  focusWindow(route: AppRoute): void;
  setWindowContentSize?(route: AppRoute, width: number, height: number): Promise<boolean>;
  setMenuBarStatus(status: ServiceStatus): void;
  setMenuBarActions(actions: NativeMenuAction[]): void;
  setTrayStatus(status: ServiceStatus): void;
  setTrayActions(actions: NativeMenuAction[]): void;
  openFilePicker(purpose: "import"): Promise<string | undefined>;
  saveFilePicker(suggestedName: string): Promise<string | undefined>;
  showActionMenu(title: string, items: string[], anchor: NativeMenuAnchor): Promise<number | undefined>;
  showGroupedActionMenu?(title: string, groups: Array<{ title: string; items: string[] }>, anchor: NativeMenuAnchor): Promise<{ group: number; item: number } | undefined>;
  showConfirmation(title: string, message: string, confirmLabel: string, cancelLabel: string, destructive: boolean): Promise<boolean>;
  textPrompt?(title: string, value: string, confirmLabel: string, cancelLabel: string): Promise<string | null>;
  showReadOnlyText(title: string, text: string, closeLabel: string, language: "json" | "toml" | "text", html: string): Promise<void>;
  showProviderAuth?(options: {
    provider: "openai";
    fingerprint?: string;
    verificationURL: string;
    userCode?: string;
    title: string;
    closeLabel: string;
    /** The window's own reload action: a page that stalled can be retried. */
    reloadLabel: string;
    /** The same page in the system browser, for a WebKit the site refuses. */
    browserLabel: string;
  }): Promise<void>;
  chooseModelsToAdd(models: string[], providerName: string, keyName: string): Promise<string[] | undefined>;
  /**
   * Optional: the sheet exists only on a host that can host it. A browser page
   * has no panel to put it in, so it leaves this undefined rather than
   * answering a no-op — the pane disables 分组 from the same absence.
   */
  showGroupManager?(options: {
    title: string;
    /** The account that owns the keys; names the copy action's secret target. */
    accountId: string;
    labels: RelayGroupManagerLabels;
    /** True while the account's aligned layout is still loading. */
    loading?: boolean;
  } & RelayGroupManagerSnapshot): Promise<RelayGroupManagerResult | undefined>;
  /**
   * The next 保存并关闭 request from the open 分组管理 sheet; the sheet stays
   * up (and locked over the window that opened it) until
   * `finishGroupManagerApply` answers it.  Undefined means the sheet ended
   * without saving, and a call with no sheet open answers immediately.
   */
  awaitGroupManagerApply?(): Promise<RelayGroupManagerResult | undefined>;
  /**
   * Answer an apply request: the sheet states `status` in its own status strip
   * and closes, or keeps its rows for another try.
   */
  finishGroupManagerApply?(options: { status: string; close: boolean }): Promise<void>;
  /**
   * Replace the open group manager sheet's content with a later snapshot of the
   * same account and end its loading state; false when no sheet is open.
   */
  updateGroupManager?(options: RelayGroupManagerSnapshot): Promise<boolean>;
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
  readSecret?(options: {
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav";
    field: string;
    target?: string;
  }): Promise<{ present: boolean; value?: string } | undefined>;
  stageSecret?(options: {
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav";
    field: string;
    target?: string;
    value: string;
  }): Promise<{ revision: number; present: boolean } | undefined>;
  copySecret(domain: "providers_models" | "relay_accounts", field: "api_key", target: string): Promise<boolean>;
  relayLogin(options: {
    accountId: string;
    /** "" (or "auto") settles the station family from the page's own answer, so
     * the sign-in window opens at once instead of waiting on a pair of network
     * probes that ask the same questions before it can appear. */
    type: "newapi" | "sub2api" | "";
    label: string;
    origin: string;
    language: LanguagePreference;
    username?: string;
    /** The account's own remember-password preference, so a re-sign-in does not silently clear it. */
    rememberPassword?: boolean;
    /** The account's own 自动分组 switch, so a re-sign-in need not ask again. */
    autoGrouping?: boolean;
    embedded?: boolean;
    /** Pending login: Core creates the account shell only after sign-in succeeds. */
    pendingAccount?: boolean;
    stationId?: string;
    stationName?: string;
    stationType?: "newapi" | "sub2api";
    stationOrigin?: string;
  }): Promise<{ revision: number; loginStatus: "signed_in"; username: string } | undefined>;
  cancelRelayLogin?: () => void;
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
  /** Optional: the host's authenticated station browser, for the dashboard. */
  openRelayDashboard?(options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    language: LanguagePreference;
  }): Promise<void>;
  clearRelayPassword(accountId: string): Promise<void>;
  clearRelayCredentials(accountId: string): Promise<void>;
  /** Absent on a host with no login item to register (the browser page). */
  setLaunchAtLogin?(enabled: boolean): Promise<void>;
  showVersion?(): void;
  versionInfo?(): Promise<{ app: string; litellm: string; icon?: string }>;
  openExternalURL?(url: string): void;
  revealFile?(path: string): void;
  openFileEditor?(target: string): void;
  prepareFileEditor?(): void;
  pendingFileEditorTarget?(): string;
  setLocalization(strings: NativeLocalization): void;
  setShortcuts(shortcuts: Record<string, string>): void;
  /**
   * End this page's own session.  Present only on a host that signed in with
   * something a person typed — the browser page — so its absence is what keeps
   * a desktop window from drawing a 退出登录 it has no session behind.
   */
  signOutWebSession?(): Promise<void>;
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
    hostRoute: bridge.hostRoute
      ? (path, body) => bridge.hostRoute!(path, body)
      : undefined,
  };
}

export function createNativeLeafBridgeAdapter(bridge: NativeLeafBridge): NativeLeafAdapter {
  return {
    window: {
      open: (route) => bridge.openWindow(route),
      close: (route) => bridge.closeWindow(route),
      focus: (route) => bridge.focusWindow(route),
      setContentSize: bridge.setWindowContentSize
        ? (route, width, height) => bridge.setWindowContentSize!(route, width, height)
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
    showGroupedActionMenu: bridge.showGroupedActionMenu
      ? ({ title, groups, anchor }) => bridge.showGroupedActionMenu!(title, groups, anchor)
      : undefined,
    showConfirmation: ({ title, message, confirmLabel, cancelLabel, destructive }) => bridge.showConfirmation(title, message, confirmLabel, cancelLabel ?? "", destructive === true),
    textPrompt: bridge.textPrompt
      ? ({ title, value, confirmLabel, cancelLabel }) => bridge.textPrompt!(title, value, confirmLabel, cancelLabel ?? "")
      : undefined,
    showReadOnlyText: ({ title, text, closeLabel, language, html }) => bridge.showReadOnlyText(title, text, closeLabel, language, html),
    showProviderAuth: bridge.showProviderAuth
      ? (options) => bridge.showProviderAuth!(options)
      : undefined,
    chooseModelsToAdd: ({ models, providerName, keyName }) => bridge.chooseModelsToAdd(models, providerName, keyName),
    showGroupManager: bridge.showGroupManager ? (options) => bridge.showGroupManager!(options) : undefined,
    awaitGroupManagerApply: bridge.awaitGroupManagerApply
      ? () => bridge.awaitGroupManagerApply!()
      : undefined,
    finishGroupManagerApply: bridge.finishGroupManagerApply
      ? (options) => bridge.finishGroupManagerApply!(options)
      : undefined,
    updateGroupManager: bridge.updateGroupManager
      ? (options) => bridge.updateGroupManager!(options)
      : undefined,
    editSecret: ({ domain, field, target, title, allowClear }) => bridge.editSecret(domain, field, target, title, allowClear),
    clearSecret: ({ domain, field, target }) => bridge.clearSecret(domain, field, target),
    readSecret: bridge.readSecret ? (options) => bridge.readSecret!(options) : undefined,
    stageSecret: bridge.stageSecret ? (options) => bridge.stageSecret!(options) : undefined,
    copySecret: ({ domain, field, target }) => bridge.copySecret(domain, field, target),
    relayLogin: (options) => bridge.relayLogin(options),
    cancelRelayLogin: () => bridge.cancelRelayLogin?.(),
    restoreRelaySession: (options) => bridge.restoreRelaySession(options),
    openRelayLogs: (options) => bridge.openRelayLogs(options),
    openRelayDashboard: bridge.openRelayDashboard
      ? (options) => bridge.openRelayDashboard!(options)
      : undefined,
    clearRelayPassword: (accountId) => bridge.clearRelayPassword(accountId),
    clearRelayCredentials: (accountId) => bridge.clearRelayCredentials(accountId),
    setLaunchAtLogin: bridge.setLaunchAtLogin ? (enabled) => bridge.setLaunchAtLogin!(enabled) : undefined,
    showVersion: () => bridge.showVersion?.(),
    versionInfo: bridge.versionInfo ? () => bridge.versionInfo!() : undefined,
    openExternalURL: bridge.openExternalURL ? (url) => bridge.openExternalURL!(url) : undefined,
    revealFile: bridge.revealFile ? (path) => bridge.revealFile!(path) : undefined,
    openFileEditor: bridge.openFileEditor ? (target) => bridge.openFileEditor!(target) : undefined,
    prepareFileEditor: bridge.prepareFileEditor ? () => bridge.prepareFileEditor!() : undefined,
    pendingFileEditorTarget: bridge.pendingFileEditorTarget ? () => bridge.pendingFileEditorTarget!() : undefined,
    setLocalization: (strings) => bridge.setLocalization(strings),
    setShortcuts: (shortcuts) => bridge.setShortcuts(shortcuts),
    signOutWebSession: bridge.signOutWebSession ? () => bridge.signOutWebSession!() : undefined,
  };
}
