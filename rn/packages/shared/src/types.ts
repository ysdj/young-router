export const IPC_PROTOCOL_VERSION = 1 as const;

export type IpcMethod =
  | "snapshot"
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
  | "configuration-package"
  | "webdav-settings"
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
  | "language";

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
  auto_start_state?: "enabled" | "disabled";
}

export interface DraftState {
  dirty: boolean;
  base_revision: number;
  validation: ValidationSummary;
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

export interface ProviderModelSummary {
  id: string;
  display_name: string;
  public_model?: string;
  upstream_model: string;
  enabled: boolean;
  order: number | string;
  billing?: string;
  usage?: string;
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
  records: Array<Record<string, unknown> | string>;
  filter: string;
  limit: number;
}

export interface CoreSnapshot {
  protocol_version: typeof IPC_PROTOCOL_VERSION;
  revision: number;
  service: ServiceStatus;
  providers_models: ProvidersModelsSummary;
  drafts: Partial<Record<ConfigDomain, DraftState>>;
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
  editor: { domain: "codex" | "claude"; document: "config" | "auth" | "settings"; editor_token: string; revision: number };
  dispatch: { revision: number };
  subscribe: { subscription_id: string };
  validate: { validate: ValidationSummary };
  apply: { revision: number; applied: true; domains?: ConfigDomain[] };
  reload: { revision: number };
  probe: { ok: boolean; protocols: string[]; detail?: string };
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
  serviceUnavailable: string;
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
  version: string;
  build: string;
  ok: string;
  invalidText: string;
  routeHome: string;
  routeProvidersModels: string;
  routeCodexSettings: string;
  routeClaudeSettings: string;
  routeRuntimeSettings: string;
  routeConfigurationPackage: string;
  routeWebdavSettings: string;
  routeLogs: string;
}

export interface NativeLeafAdapter {
  window: NativeWindow;
  menuBar: NativeMenuBar;
  tray: NativeTray;
  openFilePicker(options: { purpose: "import" | "claude-profile" }): Promise<string | undefined>;
  saveFilePicker(options: { purpose: "export" }): Promise<string | undefined>;
  showConfirmation(options: { title: string; message: string; confirmLabel: string }): Promise<boolean>;
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
  setLaunchAtLogin(enabled: boolean): Promise<void>;
  setLocalization(strings: NativeLocalization): void;
  setShortcuts(shortcuts: Record<string, string>): void;
}
