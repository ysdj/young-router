export const IPC_PROTOCOL_VERSION = 1 as const;

export type IpcMethod =
  | "snapshot"
  | "logs"
  | "editor"
  | "dispatch"
  | "subscribe"
  | "validate"
  | "apply"
  | "reload"
  | "probe"
  | "export"
  | "import";

export type AppRoute =
  | "home"
  | "providers-models"
  | "codex-settings"
  | "claude-settings"
  | "runtime-settings"
  | "webdav-settings"
  | "relay-accounts"
  | "logs";

export type LogTab =
  | "requests"
  | "service"
  | "menu"
  | "route-trace"
  | "recovery"
  | "online-usage";

export type ConfigDomain =
  | "providers_models"
  | "codex"
  | "claude"
  | "runtime"
  | "webdav"
  | "logs"
  | "language"
  | "relay_accounts";

export type LanguagePreference = "system" | "en" | "zh-Hans";

export interface SecretState {
  present: boolean;
}

export interface IpcEndpoint {
  kind: "unix_socket" | "named_pipe" | "loopback";
  address: string;
  port?: number;
  one_time_auth: true;
}

export interface ServiceStatus {
  state: "starting" | "running" | "unhealthy" | "stopped" | "unknown";
  detail?: string;
  pid?: number;
  port?: number;
  auto_start_state?: "enabled" | "disabled";
  route_recovery?: {
    recovering?: number;
    cooldown?: number;
  };
  webdav?: {
    enabled?: boolean;
    ok?: boolean | null;
    checked_at?: string | null;
    action?: string | null;
  };
}

export interface DraftState {
  dirty: boolean;
  base_revision: number;
  validation: ValidationSummary;
}

export interface DiskState {
  changed: boolean;
  generation: number;
  keep_draft?: boolean;
}

export interface ValidationSummary {
  valid: boolean;
  issues: ValidationIssue[];
}

export interface ValidationIssue {
  path: string;
  code: string;
  message: string;
  severity: "error" | "warning";
}

export interface ProviderSummary {
  id: string;
  display_name: string;
  enabled: boolean;
  model_count: number;
  api_key: SecretState;
  endpoint: string;
  models?: ProviderModelSummary[];
}

export type ProbeSurfaceName = "openai/responses" | "openai/chat" | "anthropic";

export interface ProbeOriginalRequest {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

export interface ProbeSurfaceResult {
  surface: ProbeSurfaceName | string;
  available: boolean;
  status?: string;
  original_request?: ProbeOriginalRequest;
}

export interface ProbeSummary {
  available_surfaces: string[];
  unavailable_surfaces: string[];
  statuses: Record<string, string>;
}

export interface ProviderModelSummary {
  id: string;
  display_name: string;
  public_model?: string;
  upstream_model: string;
  enabled: boolean;
  order: number | string;
  billing?: string;
  probe?: {
    available: boolean;
    recommended_surface?: ProbeSurfaceName | null;
    recommended_order?: ProbeSurfaceName[];
    summary?: ProbeSummary;
    checked_at?: string;
    surfaces?: Record<string, Omit<ProbeSurfaceResult, "surface">>;
  };
}

export interface ProvidersModelsSummary {
  providers: ProviderSummary[];
  revision: number;
}

export interface WebDavStatus {
  enabled: boolean;
  configured: boolean;
  last_probe: "unknown" | "ok" | "failed";
  password: SecretState;
}

export interface LogSummary {
  tab: LogTab;
  available: boolean;
  paused: boolean;
  line_count: number;
  filter: string;
  limit: number;
}

export interface LogView extends LogSummary {
  records: Array<Record<string, unknown> | string>;
}

export interface CoreSnapshot {
  protocol_version: typeof IPC_PROTOCOL_VERSION;
  revision: number;
  service: ServiceStatus;
  providers_models: ProvidersModelsSummary;
  drafts: Partial<Record<ConfigDomain, DraftState>>;
  disk: Partial<Record<ConfigDomain, DiskState>>;
  webdav: WebDavStatus;
  logs: Record<LogTab, LogSummary>;
  language: LanguagePreference;
  action_summaries?: Partial<Record<ConfigDomain, Record<string, unknown>>>;
  domains: Record<string, unknown>;
}

export interface DispatchAction {
  type: string;
  domain?: ConfigDomain;
  payload?: Record<string, unknown>;
}

export interface IpcParams {
  snapshot: Record<string, never>;
  logs: { tab: LogTab; revision?: number };
  editor: { domain: "codex" | "claude"; document: "config" | "auth" | "settings" };
  dispatch: { action: DispatchAction; revision?: number };
  subscribe: { topics?: string[] };
  validate: { domain: ConfigDomain; revision?: number };
  apply: ({ domain: ConfigDomain; domains?: never } | { domains: ConfigDomain[]; domain?: never }) & { revision: number; confirmation?: string | string[] };
  reload: { domain?: ConfigDomain; revision?: number };
  probe: { domain?: "providers_models" | "webdav"; provider_id?: string; model_id?: string };
  export: { sections: ConfigDomain[]; destination_token: string };
  import: { source_token: string; sections?: ConfigDomain[]; revision: number };
}

export interface IpcResults {
  snapshot: { snapshot: CoreSnapshot };
  logs: { changed: boolean; revision: number; log: LogView | null };
  editor: { domain: "codex" | "claude"; document: "config" | "auth" | "settings"; editor_token: string; revision: number };
  dispatch: { revision: number };
  subscribe: { subscription_id: string };
  validate: { validate: ValidationSummary };
  apply: { revision: number; applied: true; domains?: ConfigDomain[] };
  reload: { revision: number };
  probe: {
    ok: boolean;
    protocols: string[];
    detail?: string;
    available?: boolean;
    provider_id?: string;
    model_id?: string;
    recommended_surface?: "openai/responses" | "openai/chat" | "anthropic" | null;
    recommended_order?: ("openai/responses" | "openai/chat" | "anthropic")[];
    summary?: { available_surfaces: string[]; unavailable_surfaces: string[]; statuses: Record<string, string> };
    surfaces?: { surface: string; available: boolean; status?: string; original_request?: { method: string; url: string; headers: Record<string, string>; body: Record<string, unknown> } }[];
  };
  export: { revision: number; section_count: number; sections?: ConfigDomain[] };
  import: {
    revision: number;
    draft_domains: ConfigDomain[];
    preview: Partial<Record<ConfigDomain, { available: boolean; will_replace_draft: boolean }>>;
  };
}

export interface IpcRequest<M extends IpcMethod = IpcMethod> {
  protocol_version: typeof IPC_PROTOCOL_VERSION;
  request_id: string;
  method: M;
  params: IpcParams[M];
}

export interface IpcError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface IpcResponse<M extends IpcMethod = IpcMethod> {
  protocol_version: typeof IPC_PROTOCOL_VERSION;
  request_id: string;
  ok: boolean;
  result?: IpcResults[M];
  error?: IpcError;
}

export interface IpcEvent {
  protocol_version: typeof IPC_PROTOCOL_VERSION;
  event: "snapshot";
  revision: number;
  snapshot: CoreSnapshot;
}

export interface IpcTransport {
  send<M extends IpcMethod>(request: IpcRequest<M>): Promise<IpcResponse<M>>;
  subscribe(listener: (event: IpcEvent) => void): () => void;
  close?(): void;
}

export interface IpcClient {
  readonly endpoint?: IpcEndpoint;
  snapshot(): Promise<CoreSnapshot>;
  logs(tab: LogTab, revision?: number): Promise<IpcResults["logs"]>;
  editor(domain: "codex" | "claude", document: "config" | "auth" | "settings"): Promise<IpcResults["editor"]>;
  dispatch(action: DispatchAction, revision?: number): Promise<{ revision: number }>;
  subscribe(listener: (event: IpcEvent) => void, topics?: string[]): () => void;
  validate(domain: ConfigDomain, revision?: number): Promise<ValidationSummary>;
  apply(domain: ConfigDomain, revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]>;
  applyDomains(domains: ConfigDomain[], revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]>;
  reload(domain?: ConfigDomain, revision?: number): Promise<{ revision: number }>;
  probe(providerId?: string, modelId?: string, domain?: "providers_models" | "webdav"): Promise<IpcResults["probe"]>;
  export(sections: ConfigDomain[], destinationToken: string): Promise<IpcResults["export"]>;
  import(sourceToken: string, revision: number, sections?: ConfigDomain[]): Promise<IpcResults["import"]>;
}

export interface NativeWindow {
  open(route: AppRoute): void;
  close(route?: AppRoute): void;
  focus(route: AppRoute): void;
  setContentSize?(width: number, height: number): Promise<boolean>;
}

export interface NativeMenuBar {
  setStatus(state: ServiceStatus): void;
  setActions(actions: NativeMenuAction[]): void;
}

export interface NativeTray {
  setStatus(state: ServiceStatus): void;
  setActions(actions: NativeMenuAction[]): void;
}

export interface NativeMenuAction {
  id: string;
  title: string;
  enabled: boolean;
  checked?: boolean;
}

export interface NativeSplitView {
  setPaneWidth(width: number): void;
}

export interface NativeTextEditor {
  setContent(content: string): void;
  setReadOnly(readOnly: boolean): void;
  focus(): void;
}

export interface NativeSegmentedControl {
  setSelectedIndex(index: number): void;
}

export type NativeWindowAdapter = NativeWindow;
export type NativeMenuBarAdapter = NativeMenuBar;
export type NativeTrayAdapter = NativeTray;

export interface NativeLocalization {
  appTitle: string;
  autoStart: string;
  serviceUnavailable: string;
  serviceStatus: string;
  serviceStarting: string;
  serviceRunning: string;
  serviceRunningOnPort: string;
  serviceUnhealthy: string;
  serviceStopped: string;
  serviceUnknown: string;
  languageMenu: string;
  languageSystem: string;
  languageEnglish: string;
  languageSimplifiedChinese: string;
  cancel: string;
  set: string;
  clear: string;
  stage: string;
  find: string;
  findNext: string;
  edit: string;
  undo: string;
  redo: string;
  cut: string;
  copy: string;
  paste: string;
  selectAll: string;
  settings: string;
  reload: string;
  closeWindow: string;
  menuQuit: string;
  version: string;
  build: string;
  ok: string;
  invalidText: string;
  routeHome: string;
  routeProvidersModels: string;
  routeCodexSettings: string;
  routeClaudeSettings: string;
  routeRuntimeSettings: string;
  routeWebdavSettings: string;
  routeRelayAccounts: string;
  routeLogs: string;
  modelChooserTitle: string;
  modelChooserHeading: string;
  modelChooserProvider: string;
  modelChooserKey: string;
  modelChooserSearch: string;
  modelChooserAll: string;
  modelChooserSelectAllVisible: string;
  modelChooserInvert: string;
  modelChooserInvertVisible: string;
  modelChooserAddSelected: string;
  modelChooserCount: string;
  modelChooserCountFiltered: string;
  modelChooserCountSelected: string;
  modelChooserEmpty: string;
  modelChooserNoMatches: string;
  fileFilterJson: string;
  fileFilterAll: string;
}

export interface NativeLeafAdapter {
  window: NativeWindow;
  menuBar: NativeMenuBar;
  tray: NativeTray;
  openFilePicker(options: { purpose: "import" }): Promise<string | undefined>;
  saveFilePicker(options: { suggestedName: string }): Promise<string | undefined>;
  showActionMenu(options: { title: string; items: string[]; anchor: NativeMenuAnchor }): Promise<number | undefined>;
  showConfirmation(options: { title: string; message: string; confirmLabel: string }): Promise<boolean>;
  showReadOnlyText(options: { title: string; text: string; closeLabel: string }): Promise<void>;
  chooseModelsToAdd(options: {
    models: string[];
    providerName: string;
    keyName: string;
  }): Promise<string[] | undefined>;
  editSecureDocument(options: { editorToken: string; language: "toml" | "json"; title: string }): Promise<number | undefined>;
  editSecret(options: {
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav";
    field: string;
    target?: string;
    title: string;
    allowClear: boolean;
  }): Promise<{ revision: number; present: boolean } | undefined>;
  clearSecret(options: {
    domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav";
    field: string;
    target?: string;
  }): Promise<{ revision: number; present: boolean } | undefined>;
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
  setLocalization(strings: NativeLocalization): void;
  setShortcuts(shortcuts: Record<string, string>): void;
}

/** The triggering control's window-local rectangle, in React Native DIPs. */
export interface NativeMenuAnchor {
  x: number;
  y: number;
  width: number;
  height: number;
}
