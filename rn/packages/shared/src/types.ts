export const IPC_PROTOCOL_VERSION = 1 as const;

export type IpcMethod =
  | "snapshot"
  | "disk_state"
  | "logs"
  | "editor"
  | "files"
  | "dispatch"
  | "subscribe"
  | "validate"
  | "apply"
  | "reload"
  | "probe"
  | "export"
  | "import_preview"
  | "import";

export type AppRoute =
  | "home"
  | "file-editor"
  | "general-settings"
  | "providers-models"
  | "codex-settings"
  | "claude-settings"
  | "runtime-settings"
  | "data-management"
  | "relay-accounts"
  | "relay-add"
  | "provider-wizard"
  | "logs";

export type LogTab =
  | "requests"
  | "service"
  | "actions"
  | "route-trace"
  | "recovery"
  | "online-usage";

export type ConfigDomain =
  | "providers_models"
  | "codex"
  | "claude"
  | "clients"
  | "runtime"
  | "webdav"
  | "logs"
  | "language"
  | "relay_accounts";

export type LanguagePreference = "system" | "en" | "zh-Hans";

/**
 * Domains that own a versioned raw editor document. Codex and Claude keep
 * their own structured settings; ``clients`` owns the remaining external
 * desktop client configuration files.
 */
export type EditorDomain = "codex" | "claude" | "clients";

/** Documents the authenticated raw editor can open, per owning domain. */
export type EditorDocument =
  | "config"
  | "auth"
  | "settings"
  | "desktop"
  | "developer"
  | "pi_settings"
  | "pi_models"
  | "pi_auth"
  | "dsh_settings"
  | "dsh_desktop_settings"
  | "opencode_config"
  | "opencode_auth"
  | "codex_model_catalog";

/**
 * One registered external client configuration file. ``path`` is the only
 * local path Core returns to the UI, and only through the read-only ``files``
 * operation that backs the external-settings listing; ``display_path`` is the
 * same file spelled with the user's home directory as ``~`` for display, while
 * every action addresses ``path`` itself.
 */
export interface ClientFile {
  id: string;
  client: "codex" | "claude" | "claudeCode" | "claudeDesktop" | "pi" | "dsh" | "opencode";
  domain: "codex" | "claude" | "clients";
  document:
    | "config"
    | "auth"
    | "settings"
    | "desktop"
    | "developer"
    | "pi_settings"
    | "pi_models"
    | "pi_auth"
    | "dsh_settings"
    | "dsh_desktop_settings"
    | "opencode_config"
    | "opencode_auth";
  name: string;
  path: string;
  display_path: string;
  language: "json" | "toml" | "yaml" | "text";
  exists: boolean;
  /**
   * View state of the editor sheet that owns this file: true while the sheet
   * is on screen. Core never sends it; the host's editor target does.
   */
  present?: boolean;
}

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
  /**
   * Whether every launch stays in the background (menu bar and proxy only).
   * Unset means the default: a launch presents the providers-and-models
   * window, and the General pane's 启动后在后台运行 switch is off.
   */
  launch_background_state?: "enabled" | "disabled";
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

export type RelayBindingStatus =
  | "independent"
  | "linked"
  | "missing_key"
  | "invalid_source"
  | "disabled"
  | "missing_multiplier"
  | "catalog_missing"
  | "login_expired"
  | "unavailable";

export interface RelayKeySource {
  kind: "independent" | "relay";
  station_id?: string;
  account_id?: string;
  resource_id?: string;
}

export interface ProviderKeySummary {
  id: string;
  name: string;
  configured: boolean;
  model_count: number;
  source: RelayKeySource;
  binding_status?: RelayBindingStatus;
}

export interface ModelBindingHealth {
  status: RelayBindingStatus;
  detail?: string;
}

export interface ApplyIssue {
  code?: string;
  message?: string;
  operation_id?: string;
  domain?: ConfigDomain;
  station_id?: string;
  account_id?: string;
  resource_id?: string;
  provider_id?: string;
  model_id?: string;
  retryable?: boolean;
}

export type ProviderAuthKind =
  | "api_key"
  | "openai_login"
  | "claude_login"
  | "workbuddy_login"
  | "workbuddy_ai_login";

export type ProviderAuthStatus = "signed_out" | "authorizing" | "signed_in" | "expired" | "error" | "unsupported";

export interface ProviderSummary {
  id: string;
  display_name: string;
  enabled: boolean;
  model_count: number;
  api_key: SecretState;
  endpoint: string;
  provider_type?: "custom" | "relay";
  relay_station_id?: string;
  auth_kind?: ProviderAuthKind;
  auth_status?: ProviderAuthStatus;
  auth_active?: boolean;
  key_states?: ProviderKeySummary[];
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
  /** Surfaces whose request never completed (rejected or timed out). */
  unreachable_surfaces?: string[];
  /** Aggregate reason: refused, timeout, mixed, or empty when every surface answered. */
  transport?: string;
  statuses: Record<string, string>;
}

export type ProbeDegradationStatus =
  | "matched"
  | "mismatch"
  | "unknown"
  | "unavailable"
  | "unreachable"
  | "skipped"
  | "error";

export interface ProbeDegradationEngine {
  name: string;
  source?: string;
  license?: string;
  revision?: string;
  staged_at?: string;
  mode?: string;
  targets?: string[];
  available?: boolean;
}

/** Veridrop quick-mode result for the route behind one requested model name. */
export interface ProbeDegradationResult {
  status: ProbeDegradationStatus | string;
  target?: string | null;
  protocol?: string;
  /** Brands or self-reported identity the upstream answer carried. */
  label?: string | null;
  /** The upstream verdict: passed, marginal, or failed. */
  verdict?: string;
  score?: number | null;
  /** Non-Anthropic brands the identity probe found in the answer. */
  brands?: string[];
  detectors?: Array<{ name: string; status: string; score?: number | null }>;
  /** Raw request status behind an unreachable or failed deep test. */
  cause?: string;
  detail?: string;
  checked_at?: string;
  engine?: ProbeDegradationEngine;
}

/** What the model detail pane's deep test will run for this route. */
export interface ModelDeepProbePlan {
  includes_degradation: boolean;
  target?: string | null;
  protocol?: string;
  surface?: string;
}

export interface ProviderModelSummary {
  id: string;
  display_name: string;
  model_name?: string;
  public_model?: string;
  upstream_model: string;
  enabled: boolean;
  order: number | string;
  api_key_name?: string;
  provider_key_id?: string;
  catalog_mode?: "independent" | "relay_linked";
  source_model_id?: string;
  order_mode?: "manual" | "relay_multiplier";
  manual_order?: number;
  effective_order?: number;
  /** The user's public-model context window; absent means the client resolves it. */
  max_input_tokens?: number | null;
  binding_health?: ModelBindingHealth;
  upstream_protocol_mode?: "fallback" | "fixed";
  upstream_url_surface?: ProbeSurfaceName;
  deep_probe?: ModelDeepProbePlan;
  probe?: {
    available: boolean;
    recommended_surface?: ProbeSurfaceName | null;
    summary?: ProbeSummary;
    checked_at?: string;
    degradation?: ProbeDegradationResult;
    surfaces?: Record<string, Omit<ProbeSurfaceResult, "surface">>;
  };
}

export interface PublicModelContext {
  context_window: number;
  max_context_window: number;
}

export interface ProvidersModelsSummary {
  providers: ProviderSummary[];
  /** Registry defaults per public model, shown when the user sets no limit. */
  model_contexts?: Record<string, PublicModelContext>;
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
  disk_state: { domains: ConfigDomain[] };
  logs: { tab: LogTab; revision?: number };
  editor:
    | {
      domain: "codex" | "claude" | "clients";
      document:
        | "config"
        | "auth"
        | "settings"
        | "desktop"
        | "developer"
        | "pi_settings"
        | "pi_models"
        | "pi_auth"
        | "dsh_settings"
        | "dsh_desktop_settings"
        | "opencode_config"
        | "opencode_auth"
        | "codex_agents"
        | "codex_model_catalog";
    }
    | { editor_token: string; text: string };
  files: Record<string, never>;
  dispatch: { action: DispatchAction; revision?: number };
  subscribe: { topics?: "snapshot"[] };
  validate: { domain: ConfigDomain; revision?: number };
  apply: ({ domain: ConfigDomain; domains?: never } | { domains: ConfigDomain[]; domain?: never }) & { revision: number; confirmation?: string | string[] };
  reload: { domain?: ConfigDomain; revision?: number };
  probe: { domain?: "providers_models" | "webdav"; provider_id?: string; model_id?: string };
  export: { sections: ConfigDomain[]; destination_token: string };
  import_preview: { source_token: string; revision: number };
  import: { import_plan_token: string; sections: ConfigDomain[]; revision: number };
}

export interface IpcResults {
  snapshot: { snapshot: CoreSnapshot };
  disk_state: { revision: number; disk: Partial<Record<ConfigDomain, DiskState>> };
  logs: { changed: boolean; revision: number; log: LogView | null };
  editor: {
    domain: "codex" | "claude" | "clients";
    document:
      | "config"
      | "auth"
      | "settings"
      | "desktop"
      | "developer"
      | "pi_settings"
      | "pi_models"
      | "pi_auth"
      | "dsh_settings"
      | "dsh_desktop_settings"
      | "opencode_config"
      | "opencode_auth"
      | "codex_agents"
      | "codex_model_catalog";
    editor_token: string;
    revision: number;
    text: string;
    baseline: string;
  };
  files: { revision: number; files: ClientFile[] };
  dispatch: { revision: number; action_summary?: Record<string, unknown> };
  subscribe: { subscription_id: string };
  validate: { validate: ValidationSummary };
  apply: {
    revision: number;
    applied: boolean;
    domains?: ConfigDomain[];
    status: "applied" | "partial" | "failed";
    completed_operations: number;
    pending_operations: number;
    issues: ApplyIssue[];
  };
  reload: { revision: number };
  probe: {
    ok: boolean;
    protocols: string[];
    detail?: string;
    available?: boolean;
    provider_id?: string;
    model_id?: string;
    unreachable?: boolean;
    recommended_surface?: "openai/responses" | "openai/chat" | "anthropic" | null;
    providers?: { id: string; available: boolean; model_count: number }[];
    models?: string[];
    model_count?: number;
    summary?: { available_surfaces: string[]; unavailable_surfaces: string[]; unreachable_surfaces?: string[]; transport?: string; statuses: Record<string, string> };
    degradation?: ProbeDegradationResult;
    surfaces?: { surface: string; available: boolean; status?: string; original_request?: { method: string; url: string; headers: Record<string, string>; body: Record<string, unknown> } }[];
  };
  export: { revision: number; section_count: number; sections?: ConfigDomain[] };
  import_preview: { revision: number; import_plan_token: string; detected_sections: ConfigDomain[]; preview: Partial<Record<ConfigDomain, { available: boolean; will_replace_draft: boolean }>> };
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

/**
 * The event a subscription can ask for.  One topic exists because Core
 * publishes one event kind: a subscriber that names it is sent those events,
 * a subscriber that names none is sent every event, and a subscriber that
 * names an empty list is sent nothing.
 */
export type IpcEventTopic = IpcEvent["event"];

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
  /** The newest snapshot already received by this shared desktop runtime. */
  latestSnapshot(): CoreSnapshot | undefined;
  snapshot(): Promise<CoreSnapshot>;
  diskState(domains: ConfigDomain[]): Promise<IpcResults["disk_state"]>;
  logs(tab: LogTab, revision?: number): Promise<IpcResults["logs"]>;
  editor(domain: EditorDomain, document: EditorDocument): Promise<IpcResults["editor"]>;
  files(): Promise<IpcResults["files"]>;
  stageEditor(editorToken: string, text: string): Promise<IpcResults["editor"]>;
  dispatch(action: DispatchAction, revision?: number): Promise<IpcResults["dispatch"]>;
  subscribe(listener: (event: IpcEvent) => void, topics?: IpcEventTopic[]): () => void;
  validate(domain: ConfigDomain, revision?: number): Promise<ValidationSummary>;
  apply(domain: ConfigDomain, revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]>;
  applyDomains(domains: ConfigDomain[], revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]>;
  reload(domain?: ConfigDomain, revision?: number): Promise<{ revision: number }>;
  probe(providerId?: string, modelId?: string, domain?: "providers_models" | "webdav"): Promise<IpcResults["probe"]>;
  export(sections: ConfigDomain[], destinationToken: string): Promise<IpcResults["export"]>;
  previewImport(sourceToken: string, revision: number): Promise<IpcResults["import_preview"]>;
  importPlan(importPlanToken: string, revision: number, sections: ConfigDomain[]): Promise<IpcResults["import"]>;
}

export interface NativeWindow {
  open(route: AppRoute): void;
  close(route?: AppRoute): void;
  focus(route: AppRoute): void;
  setContentSize?(route: AppRoute, width: number, height: number): Promise<boolean>;
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

export interface NativeLocalization {
  appTitle: string;
  /** Application-menu About item. */
  about: string;
  autoStart: string;
  /** The status menu's Codex model-catalog switch. */
  codexModelCatalog: string;
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
  routeGeneralSettings: string;
  routeRuntimeSettings: string;
  routeDataManagement: string;
  routeProviderWizard: string;
  routeFileEditor: string;
  routeLogs: string;
  /**
   * The words a child surface states beside its own footer buttons while a save
   * it handed over is in flight.  A child reports its own work there — never in
   * a status bar of its own, and never in the window that opened it.
   */
  childSaving: string;
  providerAuthInstruction?: string;
  providerAuthCode?: string;
  providerAuthCopy?: string;
  providerAuthBlocked?: string;
  /** The read-only code viewer's own record label, read to a screen reader. */
  logOriginal?: string;
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

/**
 * One relay group offered by the station, as the group manager sheet sees it:
 * `label` heads the picker with its rate, `name` fills the list's group column,
 * and `rate` reports the group's 倍率 on its own.
 */
export type RelayGroupManagerGroup = { id: string; label: string; name: string; rate: string };
/**
 * One relay API key with the group it currently belongs to: its rate, whether
 * Core holds a credential for it, and the models the station reports for it.
 * Even a masked prefix/suffix is credential material, so `hint` is only Core's
 * presence sentinel; the sheet reads the real value through Core's native
 * capability and shows it in the key-value row.
 */
export type RelayGroupManagerKey = {
  id: string;
  name: string;
  groupID: string;
  groupLabel: string;
  multiplier: string;
  hint: string;
  /** The station's model list for the key, in its own order (模型列表). */
  models: string[];
  enabled: boolean;
};
/**
 * The group manager sheet's own content: what the sheet opens on, and what a
 * live update replaces it with while the sheet stays open.  `accountLabel` is
 * the caption under the title, `autoGrouping` the switch's state.
 */
export type RelayGroupManagerSnapshot = {
  accountLabel: string;
  groups: RelayGroupManagerGroup[];
  keys: RelayGroupManagerKey[];
  autoGrouping: boolean;
};
/** Localized labels for the native group manager sheet. */
export type RelayGroupManagerLabels = {
  listLabel: string;
  addLabel: string;
  removeLabel: string;
  nameLabel: string;
  groupLabel: string;
  multiplierLabel: string;
  /**
   * The plaintext value row, which the sheet fills through Core's native
   * capability; the value never crosses this boundary.
   */
  valueLabel: string;
  /** The copy icon button's tooltip and accessibility name. */
  copyLabel: string;
  failedLabel: string;
  /** The selected key's model list section title (模型列表). */
  modelsLabel: string;
  /** `common.none`: the placeholder for an empty detail value. */
  emptyLabel: string;
  enabledLabel: string;
  newKeyName: string;
  autoGroupingLabel: string;
  /** The label for a key whose group the station no longer offers. */
  ungroupedLabel: string;
  closeLabel: string;
  applyLabel: string;
  /** The Close confirmation shown while the draft would lose edits. */
  discardTitle: string;
  discardBody: string;
  /** The confirmation's own action label, e.g. 放弃更改. */
  discardConfirm: string;
  /** The footer line the sheet shows while its rows are still loading. */
  loadingLabel: string;
};
/**
 * What the sheet returns: the auto-grouping switch plus the key edits the user
 * staged in it. The provider window stages them through the Core actions.
 */
export type RelayGroupManagerResult = {
  autoGrouping: boolean;
  creates: Array<{ name: string; groupID: string }>;
  updates: Array<{ keyID: string; name: string; groupID: string; enabled: boolean }>;
  deletes: string[];
};

export interface NativeLeafAdapter {
  window: NativeWindow;
  menuBar: NativeMenuBar;
  tray: NativeTray;
  openFilePicker(options: { purpose: "import" }): Promise<string | undefined>;
  saveFilePicker(options: { suggestedName: string }): Promise<string | undefined>;
  showActionMenu(options: { title: string; items: string[]; anchor: NativeMenuAnchor }): Promise<number | undefined>;
  /**
   * The same menu with one submenu per group, so a long model list stays
   * navigable under its provider.  Resolves the chosen group/item pair.
   */
  showGroupedActionMenu?(options: { title: string; groups: Array<{ title: string; items: string[] }>; anchor: NativeMenuAnchor }): Promise<{ group: number; item: number } | undefined>;
  /**
   * One question with one primary answer, drawn by the app's own decision
   * surface on both hosts.  `destructive` marks an answer that cannot be
   * undone (删除, 放弃更改), so it draws the way every other destructive answer
   * in the app draws.  `cancelLabel` names the answer that dismisses the
   * question — the host's own 取消 unless the caller's question means more by
   * it (a move that would be cancelled rather than a question that would be
   * left unanswered).
   */
  showConfirmation(options: { title: string; message: string; confirmLabel: string; cancelLabel?: string; destructive?: boolean }): Promise<boolean>;
  showReadOnlyText(options: { title: string; text: string; closeLabel: string; language: "json" | "toml" | "text"; html: string }): Promise<void>;
  /**
   * Show an official provider login through the platform-owned auth surface.
   * The native host never returns page fields, cookies, tokens, or other
   * credential material to the shared UI.
   */
  showProviderAuth?(options: {
    provider: "openai" | "claude";
    /** Stable per-account identity used to isolate concurrent native auth windows. */
    fingerprint?: string;
    verificationURL: string;
    userCode?: string;
    callbackURL?: string;
    title: string;
    closeLabel: string;
  }): Promise<void>;
  showCodexRestartConfirmation(options: {
    title: string;
    message: string;
    restartLabel: string;
    laterLabel: string;
  }): Promise<"restart" | "later" | undefined>;
  /** Native subordinate sheet: the station's keys with their groups. */
  showGroupManager(options: {
    title: string;
    /** The account that owns the keys; names the copy action's secret target. */
    accountId: string;
    labels: RelayGroupManagerLabels;
    /**
     * True while the account's aligned layout is still loading: the sheet shows
     * the rows it opened on behind its loading line and keeps them read-only
     * until `updateGroupManager` replaces them.
     */
    loading?: boolean;
  } & RelayGroupManagerSnapshot): Promise<RelayGroupManagerResult | undefined>;
  /**
   * The next 保存并关闭 request from the open 分组管理 sheet, held until the
   * sheet hands its staged edits over.  The sheet stays open (and locked over
   * the window that opened it) while the caller writes and applies them, and
   * states the outcome in its own status strip through
   * `finishGroupManagerApply`.  Resolves undefined when the sheet ends instead
   * of saving — a call with no sheet open answers immediately, so a caller
   * waits for the next request by asking again.
   */
  awaitGroupManagerApply?(): Promise<RelayGroupManagerResult | undefined>;
  /**
   * Answer an apply request: the sheet shows `status` in its own status strip
   * and either closes (the save landed) or keeps its rows — and its 保存并关闭 —
   * for another try.
   */
  finishGroupManagerApply?(options: { status: string; close: boolean }): Promise<void>;
  /**
   * Replace the open sheet's content with a later snapshot of the same account
   * and end its loading state, so the sheet never holds the window closed for a
   * station round trip.  Resolves false when no sheet is open — a load that
   * lands after Close changes nothing.
   */
  updateGroupManager?(options: RelayGroupManagerSnapshot): Promise<boolean>;
  chooseModelsToAdd(options: {
    models: string[];
    providerName: string;
    keyName: string;
  }): Promise<string[] | undefined>;
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
  copySecret(options: {
    // A key the workspace can copy without ever receiving it: the relay
    // station's key and the provider's own stored key, both read through
    // Core's one-shot plaintext capability.
    domain: "providers_models" | "relay_accounts";
    field: "api_key";
    target: string;
  }): Promise<boolean>;
  relayLogin(options: {
    accountId: string;
    type: "newapi" | "sub2api";
    label: string;
    origin: string;
    language: LanguagePreference;
    username?: string;
    embedded?: boolean;
    /** Pending login: Core creates the account shell only after sign-in succeeds. */
    pendingAccount?: boolean;
    stationId?: string;
    stationName?: string;
    stationType?: "newapi" | "sub2api";
    stationOrigin?: string;
  }): Promise<{ revision: number; loginStatus: "signed_in"; username: string } | undefined>;
  cancelRelayLogin(): void;
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
  /** Native About/version panel (app version and build). */
  showVersion?(): void;
  /** App and bundled LiteLLM versions for the shared About pane. */
  versionInfo?(): Promise<{ app: string; litellm: string; icon?: string }>;
  /** Open an http(s) URL in the user's browser. */
  openExternalURL?(url: string): void;
  /**
   * Reveal one registered configuration file in Finder or Explorer. A file
   * that does not exist yet opens its nearest existing parent directory. The
   * native host validates the path; this never grants filesystem access.
   */
  revealFile?(path: string): void;
  /**
   * Present the raw file editor as this platform's own sub-sheet for one
   * registered client document. macOS attaches it to the settings window,
   * where it blocks the pane while it is open (mirroring the provider wizard).
   */
  openFileEditor?(target: string): void;
  /** Create the editor sheet's window (and boot its editor) ahead of time. */
  prepareFileEditor?(): void;
  /** The document the host currently asks the editor sheet to show, as JSON. */
  pendingFileEditorTarget?(): string;
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

/**
 * Codex model selection payload sent from Codex Settings to the Core editor.
 *
 * ``model``/``provider``/``deployment_id`` identify the configured LiteLLM
 * model row. ``supports_responses_compaction`` mirrors the explicit per-model
 * metadata opt-in (``model_info.supports_responses_compaction``) so the UI
 * selection carries the compaction capability; the Core apply still resolves
 * the authoritative value from the runtime configuration row.
 */
