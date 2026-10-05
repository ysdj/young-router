import type { TranslationKey } from "../i18n/types";
import React, { createContext, useCallback, useEffect, useMemo, useRef, useState, useContext } from "react";
import { AppState, FlatList, Image, Platform, PlatformColor, Pressable, ScrollView, StyleSheet, Text, View, type HostInstance, type ScrollViewProps, type StyleProp, type TextStyle, type ViewStyle } from "react-native";
import { createTranslator } from "../i18n";
import { assistantSettingOptions, type AssistantSettingOption } from "../i18n/assistantSettingsI18n";
import { runtimeCategoryLabel, runtimeFieldHelp, runtimeFieldLabel, runtimeOptionLabel, runtimeUnitLabel } from "../i18n/runtimeSettingsI18n";
import { canonicalWindowRoute, isSettingsPaneRoute, isSettingsShellRoute, LOG_TABS, routeMenuActions, ROUTES, SETTINGS_PANES, SETTINGS_PANE_PRESENTATION } from "../routes";
import { NativeButton, NativeCheckbox, NativePersistentScrollIndicator, NativePicker, NativeSecureTextInput, NativeSegmentedControl, NativeTable, NativeTextField, NativeToggle } from "./NativeControls";
import { CodeEditorWebView, editorMenuLabels, readOnlyCodeEditorHtml } from "./code-editor/CodeEditorWebView";
import { usePendingAction } from "./pendingAction";
import {
  accountDisplayName,
  accountsFromSnapshot,
  ApiKeyCreateDialog,
  DependencyPolicyDialog,
  NativeFormRow,
  NativeWizardProgress,
  normalizeRelayOrigin,
  providedKeyRows,
  type ProvidedKeyRow,
  pendingCredentialCleanups,
  StationAccountsPanel,
  stationDisplayName,
  stationOriginKey,
  stationsFromSnapshot,
  type RelayAccount,
  type RelayApiKeyActions,
  type RelayCommit,
  type RelayResource,
  type RelayStation,
  type RelayType,
  type RelayWorkspaceBridge,
  type StationDraft,
} from "./RelayAccountManager";
import { suggestedProviderName, suggestedRelayStationName } from "./relayOrigin";
import { isAssistantEditorOpen, isGroupManagerOpen, isProviderWizardOpen, setAssistantEditorOpen, setGroupManagerOpen, setProviderWizardOpen, subscribeProviderWizard } from "./providerWizardGate";
import { SOURCE_LIST_FONT_SIZE, UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";
import type {
  AppRoute,
  ClientFile,
  ConfigDomain,
  EditorDocument,
  CoreSnapshot,
  DiskState,
  IpcClient,
  IpcResults,
  LogTab,
  LogView,
  NativeLeafAdapter,
  ProbeSurfaceName,
  ProviderSummary,
  ProviderAuthKind,
  ProviderAuthStatus,
  ServiceStatus,
  ValidationSummary,
} from "../types";

type Translate = (key: string, values?: Record<string, string | number>) => string;
type UnknownRecord = Record<string, unknown>;
type Dispatch = (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<void>;
type ApplyProbedSurface = (providerId: string, modelId: string, surface: ProbeSurfaceName, options?: { confirmRecommendation?: boolean }) => Promise<boolean>;
type NativeSecretClear = (options: {
  domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav";
  field: string;
  target?: string;
}) => Promise<void>;
type SecretState = { revision: number; present: boolean; status: string; error: string; commitRequest: number };
type PendingField = { commit: () => void | Promise<void>; reset: () => void; isDirty?: () => boolean; hasError?: () => boolean; flushBeforeAssistantEditor?: boolean };
type PendingFieldRegistry = {
  register: (id: symbol, field?: PendingField) => void;
  setDirty: (id: symbol, dirty: boolean) => void;
};
type ProviderWorkspaceDraftProjection = {
  providers: UnknownRecord[];
  providerDisplayName: (provider: UnknownRecord) => string;
  modelDisplayName: (providerID: string, model: UnknownRecord) => string;
  providerBaseURL: (provider: UnknownRecord) => string;
  modelUpstreamDisplay: (providerID: string, model: UnknownRecord) => string;
  modelOrderText: (providerID: string, model: UnknownRecord) => string;
  providerKeyDisplayName: (providerID: string, keyID: string, fallback: string) => string;
  setProviderNameDraft: (providerID: string, value: string) => void;
  setProviderBaseUrlDraft: (providerID: string, value: string) => void;
  setModelNameDraft: (providerID: string, modelID: string, value: string) => void;
  setModelUpstreamDraft: (providerID: string, modelID: string, value: string) => void;
  setModelOrderDraft: (providerID: string, modelID: string, value: string) => void;
  setProviderKeyNameDraft: (providerID: string, keyID: string, value: string) => void;
};
type ServiceOperation = "start" | "stop" | "restart" | "reload" | "health";
// Launch-time service starts are retried on this backoff: a login-time launch
// competes with every other start-up item, and a proxy whose workers failed to
// spawn then must come back without the user relaunching the app.
const SERVICE_STARTUP_RETRY_DELAYS_MS = [0, 5_000, 20_000, 60_000];
type EditableDiskDomain = "codex" | "claude" | "clients" | "providers_models" | "runtime" | "webdav";
type RawEditorConflictResolution = "reload" | "keep";
type AssistantSettingsDomain = "codex" | "claude" | "clients";
type RawEditorConflictHandler = (domain: AssistantSettingsDomain, document: EditorDocument) => Promise<RawEditorConflictResolution>;
type RawEditorDocument = EditorDocument;
type DataManagementTab = "import" | "export" | "webdav";
/** Which data-management action is running, so only its own button spins. */
type DataManagementAction = "inspect" | "import" | "export" | "probe" | "sync";
type WebDavSyncAction = "sync" | "push" | "pull";
/**
 * How a finished pane action reads in the window's one status strip: a fixed
 * message key, no message at all, or the key its own result picks — a probe
 * that reached the server and one that did not are not the same sentence, and
 * repeating the button's own name states neither.
 */
type ResultMessage = string | null | ((value: unknown) => string | null);

const PROVIDER_AUTH_OPTIONS = ["api_key", "openai_login", "claude_login", "workbuddy_login", "workbuddy_ai_login"] as const satisfies readonly ProviderAuthKind[];

function providerAuthKind(provider: UnknownRecord | undefined): ProviderAuthKind {
  const value = stringValue(provider?.auth_kind, "api_key");
  return PROVIDER_AUTH_OPTIONS.includes(value as ProviderAuthKind) ? value as ProviderAuthKind : "api_key";
}

function providerAuthStatus(provider: UnknownRecord | undefined): ProviderAuthStatus {
  const value = stringValue(provider?.auth_status, "signed_out");
  const statuses: readonly ProviderAuthStatus[] = ["signed_out", "authorizing", "signed_in", "expired", "error", "unsupported"];
  return statuses.includes(value as ProviderAuthStatus) ? value as ProviderAuthStatus : "signed_out";
}

/** The merged workspace shows every provider kind; this drives row labels. */
type ProviderKind = "relay" | "openai" | "claude" | "workbuddy" | "workbuddyAI" | "apiKey";

function providerKind(provider: UnknownRecord | undefined): ProviderKind {
  const auth = providerAuthKind(provider);
  if (auth === "openai_login") return "openai";
  if (auth === "claude_login") return "claude";
  if (auth === "workbuddy_login") return "workbuddy";
  if (auth === "workbuddy_ai_login") return "workbuddyAI";
  return stringValue(provider?.provider_type, "custom") === "relay" ? "relay" : "apiKey";
}

/**
 * The official services this app can configure.
 *
 * `openai` is OpenAI's own subscription service, named OpenAI everywhere it is
 * shown (Core registers the login kind as `openai_login`, and the provider the
 * wizard mints carries that name); `workbuddy`/`workbuddyAI` are the two
 * WorkBuddy desktop products. A service decides two things everywhere below:
 * which address a provider carries, and whether its association section reads
 * as a service link rather than a relay station link.
 */
type ServiceID = "openai" | "claude" | "workbuddy" | "workbuddyAI";

/** The provider-key slot a WorkBuddy provider carries for its loopback route. */
const WORKBUDDY_LOCAL_KEY_NAME = "workbuddy-local";

/** The address each service is addressed by, used for the wizard's URL field. */
const SERVICE_BASE_URLS: Record<ServiceID, string> = {
  openai: "https://api.openai.com/v1",
  claude: "https://api.anthropic.com",
  workbuddy: "https://copilot.tencent.com",
  workbuddyAI: "https://www.workbuddy.ai",
};

/** Service origins, so a hand-typed provider URL is recognized the same way. */
function serviceFromURL(value: string): ServiceID | undefined {
  const host = (() => {
    const text = value.trim();
    if (!text) return "";
    try {
      return new URL(text.includes("://") ? text : `https://${text}`).hostname.toLowerCase();
    } catch {
      return "";
    }
  })();
  if (!host) return undefined;
  const matches = (suffix: string): boolean => host === suffix || host.endsWith(`.${suffix}`);
  if (matches("workbuddy.ai")) return "workbuddyAI";
  if (matches("codebuddy.ai") || matches("codebuddy.cn") || matches("copilot.tencent.com")) return "workbuddy";
  if (matches("api.openai.com") || matches("openai.com") || matches("chatgpt.com")) return "openai";
  if (matches("api.anthropic.com") || matches("anthropic.com") || matches("claude.ai")) return "claude";
  return undefined;
}

/** The service behind one provider entry: its kind first, then its address. */
function providerService(provider: UnknownRecord | undefined): ServiceID | undefined {
  const kind = providerKind(provider ?? {});
  if (kind === "openai" || kind === "claude" || kind === "workbuddy" || kind === "workbuddyAI") return kind;
  return serviceFromURL(stringValue(provider?.endpoint, stringValue(provider?.api_base)));
}

/**
 * One service's catalog, keyed by model id and refreshed on a short interval.
 * The rate the picker shows has to survive into the detail pane without a
 * fetch per selected model.
 */
const SERVICE_RATE_TTL_MS = 5 * 60 * 1000;
const serviceRates = new Map<string, { at: number; rates: Record<string, string> }>();

function peekServiceRate(service: ServiceID | undefined, modelID: string): string | undefined {
  if (!service || !modelID) return undefined;
  const cached = serviceRates.get(service);
  if (!cached || Date.now() - cached.at >= SERVICE_RATE_TTL_MS) return undefined;
  return cached.rates[modelID];
}

/** Whether this service's catalog is already loaded and still fresh. */
function hasFreshServiceRates(service: ServiceID | undefined): boolean {
  if (!service) return false;
  const cached = serviceRates.get(service);
  return cached !== undefined && Date.now() - cached.at < SERVICE_RATE_TTL_MS;
}

async function serviceModelRate(
  service: ServiceID,
  read: (providerId: string) => Promise<CoreSnapshot | undefined>,
): Promise<Record<string, string>> {
  const cached = serviceRates.get(service);
  if (cached && Date.now() - cached.at < SERVICE_RATE_TTL_MS) return cached.rates;
  const providerId = service === "workbuddyAI" ? "workbuddy-ai" : service === "workbuddy" ? "workbuddy" : "";
  if (!providerId) return {};
  const snapshot = await read(providerId);
  const summary = asRecord(asRecord(snapshot?.action_summaries?.providers_models).operation_summary);
  const rates: Record<string, string> = {};
  if (summary.available === true) {
    for (const entry of asRecords(summary.models)) {
      const modelID = stringValue(entry.id).trim();
      if (!modelID) continue;
      const billing = asRecord(entry.billing);
      const credits = stringValue(billing.credits).trim();
      rates[modelID] = credits ? credits.replace(/\s*credits?$/i, "") : billing.free === true ? "x0.00" : "";
    }
  }
  serviceRates.set(service, { at: Date.now(), rates });
  return rates;
}

/** Which WorkBuddy product a provider kind borrows its sign-in from. */
function workbuddyProviderIDFor(kind: ProviderKind): "workbuddy" | "workbuddy-ai" | undefined {
  if (kind === "workbuddy") return "workbuddy";
  if (kind === "workbuddyAI") return "workbuddy-ai";
  return undefined;
}

/** The desktop app a WorkBuddy account signs in through. */
function workbuddyAppName(kind: ProviderKind, translate: Translate): string {
  return translate(kind === "workbuddyAI" ? "providers.type.workbuddyAI" : "providers.type.workbuddy");
}

function officialStatusLabel(status: ProviderAuthStatus, translate: Translate): string {  return status === "signed_in" ? translate("providers.authStatusSignedIn")
    : status === "authorizing" ? translate("providers.authStatusAuthorizing")
      : status === "expired" ? translate("providers.authStatusExpired")
        : status === "error" ? translate("providers.authStatusError")
          : status === "unsupported" ? translate("providers.authStatusUnsupported")
            : translate("providers.authStatusSignedOut");
}

/**
 * The WorkBuddy account's remaining credit, as the desktop app reports it.
 *
 * The value is the upstream's own number of credits, not a currency: the
 * wizard shows it beside the account so a signed-in-but-out-of-credit account
 * is visible before models are added.
 */
function workbuddyCreditsText(account: UnknownRecord | undefined, translate: Translate): string {
  const credits = asRecord(account?.credits);
  if (credits.unlimited === true) return translate("providers.wizard.workbuddyUnlimited");
  const total = credits.total;
  return typeof total === "number" && Number.isFinite(total) ? String(total) : "";
}

/** One model's credit multiplier, as the catalog spells it (e.g. `x0.29`). */
function workbuddyModelRate(model: UnknownRecord, translate: Translate): string {
  const billing = asRecord(model.billing);
  const credits = stringValue(billing.credits).trim();
  if (credits) return credits.replace(/\s*credits?$/i, "");
  return billing.free === true ? translate("providers.wizard.workbuddyFree") : "";
}

/** The shared busy word, or nothing when the read has settled. */
function progressText(busy: boolean, translate: Translate): string | undefined {
  return busy ? translate("common.loading") : undefined;
}

/** The login types a service provider can be switched between, in menu order. */
const SERVICE_KIND_OPTIONS: ReadonlyArray<{ kind: ProviderKind; label: TranslationKey }> = [
  { kind: "openai", label: "providers.type.openai" },
  { kind: "claude", label: "providers.type.claude" },
  { kind: "workbuddy", label: "providers.type.workbuddy" },
  { kind: "workbuddyAI", label: "providers.type.workbuddyAI" },
];

function providerKindLabel(kind: ProviderKind, translate: Translate): string {
  return kind === "relay"
    ? translate("providers.type.relay")
    : kind === "openai"
      ? translate("providers.type.openai")
      : kind === "claude"
        ? translate("providers.type.claude")
        : kind === "workbuddy"
          ? translate("providers.type.workbuddy")
          : kind === "workbuddyAI"
            ? translate("providers.type.workbuddyAI")
            : translate("providers.type.apiKey");
}

const DATA_PACKAGE_SECTIONS: ReadonlyArray<{ domain: ConfigDomain; labelKey: string }> = [
  { domain: "providers_models", labelKey: "dataManagement.section.providersModels" },
  { domain: "runtime", labelKey: "dataManagement.section.runtime" },
  { domain: "relay_accounts", labelKey: "dataManagement.section.relayAccounts" },
  { domain: "codex", labelKey: "dataManagement.section.codex" },
  { domain: "claude", labelKey: "dataManagement.section.claude" },
  { domain: "webdav", labelKey: "dataManagement.section.webdavSettings" },
  { domain: "language", labelKey: "dataManagement.section.language" },
];
const DATA_PACKAGE_DOMAINS = DATA_PACKAGE_SECTIONS.map(({ domain }) => domain);
const DATA_MANAGEMENT_DIRTY_DOMAINS: readonly ConfigDomain[] = DATA_PACKAGE_DOMAINS;
const WEBDAV_SYNC_DOMAINS: readonly ConfigDomain[] = ["providers_models", "relay_accounts"];
/**
 * The three directions a WebDAV sync can take. Each names itself and its own
 * consequence, so the field where the direction is chosen is also where the
 * choice is explained.
 */
const WEBDAV_SYNC_OPTIONS: ReadonlyArray<{ id: WebDavSyncAction; labelKey: TranslationKey; hintKey: TranslationKey }> = [
  { id: "sync", labelKey: "dataManagement.syncSmart", hintKey: "dataManagement.syncSmartHint" },
  { id: "push", labelKey: "dataManagement.syncPush", hintKey: "dataManagement.syncPushHint" },
  { id: "pull", labelKey: "dataManagement.syncPull", hintKey: "dataManagement.syncPullHint" },
];

const PendingFieldContext = createContext<PendingFieldRegistry | undefined>(undefined);
const TranslationContext = createContext<Translate | undefined>(undefined);
const ProviderWorkspaceDraftContext = createContext<ProviderWorkspaceDraftProjection | undefined>(undefined);

function providerModelDraftKey(providerID: string, modelID: string): string {
  return `${providerID}\x1f${modelID}`;
}

function pruneStringDrafts(drafts: Record<string, string>, keep: (key: string, value: string) => boolean): Record<string, string> {
  let next = drafts;
  for (const [key, value] of Object.entries(drafts)) {
    if (keep(key, value)) continue;
    if (next === drafts) next = { ...drafts };
    delete next[key];
  }
  return next;
}
// React Native macOS supports `tooltip` on Text, but its published TypeScript
// declaration has not caught up with that native prop. Keep the cast narrow so
// the probe status has a short native hover hint; the full result opens in the
// shared read-only code viewer.
const TooltipText = Text as unknown as React.ComponentType<React.ComponentProps<typeof Text> & { tooltip?: string }>;

// Shared dense overrides keep forms and section chrome consistent across all
// settings surfaces while preserving the page-specific layout styles below.
const compactStyles = StyleSheet.create({
  windowContent: { gap: 6 },
  tablePane: { gap: 4 },
  tableTitleRow: { height: 22 },
  inlineGap: { gap: 4 },
  formRow: { minHeight: 24, gap: 2 },
  formRowControl: { gap: 1 },
  input: { minHeight: 24 },
  picker: { height: 24 },
  nativeSecretControl: { minHeight: 24, gap: 4 },
});

// Core subscriptions carry ordinary state changes. These timers only watch
// files edited by another process, so a low-frequency check avoids waking the
// Core and native table surfaces continuously while retaining bounded pickup.
const SETTINGS_DISK_POLL_MS = 5_000;
// Immediate settings commit: a staged control change applies shortly after the
// last edit so the runtime picks it up without an explicit Apply button. The
// short debounce coalesces rapid toggles into one Core apply.
const IMMEDIATE_APPLY_DEBOUNCE_MS = 500;
const LOG_VIEW_POLL_MS = 5_000;
const REQUEST_LOG_POLL_MS = 1_000;
const RECOVERY_LOG_POLL_MS = 1_000;
const ONLINE_USAGE_POLL_MS = 15_000;
// How long one relay read answers later implicit callers for the same account.
// It exists to stop the account pane's mount read and 分组管理's aligned draft
// from paying for the same station round trip twice; an explicit read (a
// sign-in that just landed) passes ``force`` and never uses it.
const RELAY_RESOURCE_REUSE_MS = 15_000;
const ROUTE_TRACE_REQUEST_ROW_HEIGHT = 70;
const ROUTE_TRACE_SCROLL_IDLE_MS = 150;
const ROUTE_TRACE_TIMELINE_MIN_WIDTH = 400;
const ROUTE_TRACE_SCROLLBAR_MIN_THUMB_WIDTH = 32;
// The inline keys panel puts the list and its editor side by side: the list
// keeps its own height instead of losing the editor's rows to the stack below
// it, and the editor column keeps each label above its control (a side-by-side
// label would leave a 密钥值 field unreadably short).  The list's height is a
// constant: switching vendors or working in the sections below must never
// resize a list the user is reading or clicking in, so six ordinary rows (24 pt
// header + 2 pt slack) stay put and scroll internally when a vendor has more.
const KEYS_INLINE_EDITOR_WIDTH = 124;
const KEYS_INLINE_LIST_HEIGHT = 26 + 6 * 22;
// The settings window draws a full-height sidebar behind a transparent title
// bar, so the shared sidebar content starts below the traffic lights while the
// window material itself extends to the top edge.
const SETTINGS_TITLEBAR_INSET = Platform.OS === "macos" ? 32 : 0;
// One header band, one height: the sidebar's app icon and the pane's own
// title are set to the same height so the two headers align.  Neither header
// draws a hairline of its own — the window states its structure with the box
// around each list and the sidebar's own right edge, never with a rule that
// crosses the header band (see the settings column edges).
const SETTINGS_HEADER_CONTENT_HEIGHT = 20;
const COLUMN_GAP = 8;
/**
 * The model detail's label column.  Every row in that pane shares it, so the
 * column is sized by the pane's longest rendered label rather than by the
 * shared form grid: Codex 上下文 measures 80.9 pt in the 13 pt host face, and
 * COLUMN_GAP is the breathing room before the control column starts.  The
 * 290 pt pane keeps the total; this is what hands its room to the controls.
 */
const MODEL_INSPECTOR_LABEL_WIDTH = 89;
const DSH_VISION_ROUTER_QUICK_KEYS = [
  "YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED",
  "YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND",
  "YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK",
  "YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
  "YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS",
  "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED",
  "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED",
] as const;
const DSH_VISION_ROUTER_CONFIG_KEY = "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON";
const DSH_VISION_ROUTER_QUICK_DEFAULTS: Record<string, string> = {
  YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED: "on",
  YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND: "auto",
  YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK: "on",
  YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS: "45",
  YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS: "4096",
  YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED: "off",
  YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED: "off",
};
// Ordinary configuration text stays local until blur, submit, or the shell's
// automatic save. Core
// publishes a full settings snapshot for every staged change, so committing
// active typing would make every settings surface pay for unrelated edits.
// Native secret inputs keep a short quiet-period commit because their values
// intentionally never enter React state.
const SECRET_INPUT_COMMIT_DEBOUNCE_MS = 150;
const RAW_EDITOR_SYNC_INTERVAL_MS = 120;

function sameDiskState(left: DiskState | undefined, right: DiskState | undefined): boolean {
  return left?.changed === right?.changed
    && left?.generation === right?.generation
    && left?.keep_draft === right?.keep_draft;
}

function serviceOperationForNativeAction(action: string): ServiceOperation | undefined {
  switch (action) {
    case "service-start": return "start";
    case "service-stop": return "stop";
    case "service-restart": return "restart";
    case "service-reload": return "reload";
    case "service-health": return "health";
    default: return undefined;
  }
}

export interface YoungRouterAppProps {
  ipc: IpcClient;
  native: NativeLeafAdapter;
  translate: Translate;
  initialSnapshot?: CoreSnapshot;
  routeRequest?: AppRoute;
  routeRequestSequence?: number;
  /** Document the host opened this window for (the file editor window). */
  fileIdRequest?: string;
  logTabRequest?: LogTab;
  nativeAction?: { id: string; sequence: number };
  isPrimaryHost?: boolean;
  isWindowManagerHost?: boolean;
}

function asRecord(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function asRecords(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(asRecord).filter((item) => Object.keys(item).length > 0) : [];
}

function stringValue(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  return fallback;
}

// A provider, model, or key that was created but not named yet stores an empty
// string.  Treat that exactly like a missing value so the row shows the
// localized placeholder instead of rendering blank.
function displayLabel(value: unknown, fallback: string): string {
  return stringValue(value).trim() || fallback;
}

function booleanValue(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["1", "true", "yes", "on", "auto", "enabled"].includes(normalized)) return true;
    if (["0", "false", "no", "off", "disabled", ""].includes(normalized)) return false;
  }
  return fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

type RelaySourceOption = {
  stationID: string;
  accountID: string;
  resourceID: string;
  baseURL: string;
  stationLabel: string;
  accountLabel: string;
  resourceLabel: string;
  models: string[];
  enabled: boolean;
  multiplier?: number;
};

type RelayStationOption = {
  id: string;
  name: string;
  baseURL: string;
  type?: RelayType;
};

type ProviderKeyState = {
  id: string;
  name: string;
  configured: boolean;
  modelCount: number;
  source: { kind: "independent" | "relay"; stationID: string; accountID: string; resourceID: string };
};

type ProviderKeyChoice = {
  id: string;
  name: string;
  kind: "independent" | "relay";
  state?: ProviderKeyState;
  source?: RelaySourceOption;
};

function relaySourcesFromSnapshot(snapshot: CoreSnapshot | undefined): RelaySourceOption[] {
  const relay = domainState(snapshot, "relay_accounts");
  const accounts = asRecords(relay.accounts);
  const stationByID = new Map(asRecords(relay.stations).map((station) => [stringValue(station.id), {
    label: stringValue(station.name, stringValue(station.label)),
    origin: stringValue(station.origin, stringValue(station.base_url, stringValue(station.url))),
  }]));
  return accounts.flatMap((account) => {
    const accountID = stringValue(account.id);
    if (!accountID) return [];
    const stationID = stringValue(account.station_id);
    const station = stationByID.get(stationID);
    const stationLabel = station?.label || stringValue(account.station_name, stringValue(account.origin, stationID));
    const stationOrigin = station?.origin || stringValue(account.origin);
    const username = stringValue(account.username).trim();
    const accountLabel = username ? username.split("@", 1)[0].trim() || username : stringValue(account.label, accountID).trim();
    const groups = new Map(asRecords(account.groups).map((group) => [stringValue(group.id), numberValue(group.multiplier, Number.NaN)]));
    const resources = asRecords(Array.isArray(account.resources) ? account.resources : account.api_keys);
    return resources.flatMap((resource) => {
      const resourceID = stringValue(resource.id);
      if (!resourceID) return [];
      const multiplier = groups.get(stringValue(resource.group_id));
      return [{
        stationID,
        accountID,
        resourceID,
        baseURL: stringValue(resource.api_base, stationOrigin),
        stationLabel: stationLabel || stationID,
        accountLabel: accountLabel || accountID,
        resourceLabel: stringValue(resource.api_name, stringValue(resource.name, resourceID)),
        models: stringList(resource.models),
        enabled: resource.enabled !== false,
        ...(Number.isFinite(multiplier) ? { multiplier } : {}),
      }];
    });
  });
}

function relayStationsFromSnapshot(snapshot: CoreSnapshot | undefined): RelayStationOption[] {
  const relay = domainState(snapshot, "relay_accounts");
  const stations = asRecords(relay.stations);
  return stations.flatMap((station) => {
    const id = stringValue(station.id).trim();
    const name = stringValue(station.name, stringValue(station.label)).trim();
    const baseURL = stringValue(station.origin, stringValue(station.base_url, stringValue(station.url))).trim();
    const type = stringValue(station.type) === "sub2api" ? "sub2api" as const : stringValue(station.type) === "newapi" ? "newapi" as const : undefined;
    return id && name && baseURL ? [{ id, name, baseURL, ...(type ? { type } : {}) }] : [];
  });
}

function relaySourcesForBaseUrl(value: string, relaySources: RelaySourceOption[]): RelaySourceOption[] {
  const target = stationOriginKey(value);
  if (!target) return [];
  return relaySources.filter((source) => source.enabled && stationOriginKey(source.baseURL) === target);
}

function relayStationForBaseUrl(value: string, relayStations: RelayStationOption[]): RelayStationOption | undefined {
  const target = stationOriginKey(value);
  if (!target) return undefined;
  return relayStations.find((station) => stationOriginKey(station.baseURL) === target);
}

function providerKeyStates(provider: UnknownRecord): ProviderKeyState[] {
  return asRecords(provider.key_states).flatMap((value) => {
    const source = asRecord(value.source);
    const kind = source.kind === "relay" ? "relay" : source.kind === "independent" ? "independent" : undefined;
    const id = stringValue(value.id);
    const name = stringValue(value.name, stringValue(value.api_key_name));
    if (!id || !name || !kind) return [];
    return [{
      id,
      name,
      configured: booleanValue(value.configured),
      modelCount: numberValue(value.model_count),
      source: {
        kind,
        stationID: stringValue(source.station_id),
        accountID: stringValue(source.account_id),
        resourceID: stringValue(source.resource_id),
      },
    }];
  });
}

function relaySourceForKey(key: ProviderKeyState | undefined, relaySources: RelaySourceOption[]): RelaySourceOption | undefined {
  if (!key || key.source.kind !== "relay") return undefined;
  return relaySources.find((source) => source.stationID === key.source.stationID && source.accountID === key.source.accountID && source.resourceID === key.source.resourceID);
}

function relaySourceSelectionID(source: Pick<RelaySourceOption, "stationID" | "accountID" | "resourceID">): string {
  return `${source.stationID}\x1f${source.accountID}\x1f${source.resourceID}`;
}

function providerKeyChoices(provider: UnknownRecord, relaySources: RelaySourceOption[], baseURL?: string): ProviderKeyChoice[] {
  const keyStates = providerKeyStates(provider);
  const providerBaseURL = baseURL ?? stringValue(provider.endpoint, stringValue(provider.api_base));
  const matchingRelaySources = relaySourcesForBaseUrl(providerBaseURL, relaySources);
  const persistedRelaySourceIDs = new Set(
    keyStates
      .filter((key) => key.source.kind === "relay")
      .map((key) => relaySourceSelectionID(key.source)),
  );
  return [
    ...keyStates.map((key) => ({
      id: key.id,
      name: key.name,
      kind: key.source.kind,
      state: key,
      source: relaySourceForKey(key, relaySources),
    })),
    ...matchingRelaySources
      .filter((source) => !persistedRelaySourceIDs.has(relaySourceSelectionID(source)))
      .map((source) => ({
        id: `relay:${relaySourceSelectionID(source)}`,
        name: source.resourceLabel,
        kind: "relay" as const,
        source,
      })),
  ];
}

function providerKeyChoiceLabel(choice: Pick<ProviderKeyChoice, "name" | "kind" | "source">, translate: Translate): string {
  const name = apiKeyDisplayName(choice.name, translate);
  if (choice.kind !== "relay") return name;
  const sourceName = relaySourceName(choice.source) || name;
  const multiplier = choice.source && Number.isFinite(choice.source.multiplier) ? ` (${choice.source.multiplier}x)` : "";
  return `${sourceName}${multiplier}`;
}

function modelOrderMode(model: UnknownRecord): "manual" | "relay_multiplier" {
  return model.order_mode === "relay_multiplier" ? "relay_multiplier" : "manual";
}

function modelEffectiveOrder(model: UnknownRecord): number {
  return numberValue(model.effective_order, numberValue(model.order, 0));
}

function modelProviderKeyState(model: UnknownRecord | undefined, provider: UnknownRecord): ProviderKeyState | undefined {
  if (!model) return undefined;
  // Match the key the way the model editor does: by slot id first, then by the
  // key name the model carries.  A stored id can move (Core re-derives a
  // renamed key's slot when it reads the document back), and matching by id
  // alone left the list under "undefined key" while the editor showed the key.
  // A name is the weaker identity and only resolves while it is unambiguous: a
  // station key and a custom key that happen to share a name are two keys, and
  // one of them is never read as the other.
  const keyStates = providerKeyStates(provider);
  const keyID = stringValue(model.provider_key_id);
  const keyName = stringValue(model.api_key_name);
  const byID = keyStates.find((entry) => entry.id === keyID);
  if (byID) return byID;
  const byName = keyStates.filter((entry) => entry.name === keyName && keyName !== "");
  return byName.length === 1 ? byName[0] : undefined;
}

function relaySourceName(source: RelaySourceOption | undefined): string {
  // Relay keys are named by the account and the group they belong to
  // (`account/GroupName`): the provider's own slot name is only the group
  // name, which reads as a truncated key wherever the account is not already
  // on screen (the model list's group rows, the routes table, the wizard's
  // provided-key list).
  return source ? [source.accountLabel, source.resourceLabel].filter(Boolean).join("/") : "";
}

function modelProviderKeyLabel(model: UnknownRecord, provider: UnknownRecord, translate: Translate, keyName?: (keyID: string, name: string) => string, relaySources: RelaySourceOption[] = []): string {
  const key = modelProviderKeyState(model, provider);
  if (!key) return displayLabel(model.api_key_name, translate("providers.undefinedKey"));
  const name = keyName?.(key.id, key.name) ?? key.name;
  return relaySourceName(relaySourceForKey(key, relaySources)) || name;
}

// Why a relay-bound Apply refused, in the user's own words: Core names the kind
// of binding problem on the row it belongs to (`binding_issues`), and the pane
// states that sentence where the key is edited — the strip keeps the outcome.
const RELAY_BINDING_ISSUE_KEYS: Record<string, TranslationKey> = {
  account_unavailable: "providers.bindingIssueAccount",
  refresh_failed: "providers.bindingIssueAccount",
  login_required: "providers.bindingIssueAccount",
  resource_unavailable: "providers.bindingIssueResource",
  resource_missing: "providers.bindingIssueResource",
  resource_disabled: "providers.bindingIssueResource",
  created_resource_unresolved: "providers.bindingIssueResource",
  resource_key_unavailable: "providers.bindingIssueCredential",
  credential_missing: "providers.bindingIssueCredential",
  catalog_model_missing: "providers.bindingIssueCatalog",
  multiplier_missing: "providers.bindingIssueCatalog",
  api_base_missing: "providers.bindingIssueBaseUrl",
  provider_api_base_conflict: "providers.bindingIssueBaseUrl",
};

function relayBindingIssueText(issue: UnknownRecord, keyLabel: string, translate: Translate): string {
  const key = RELAY_BINDING_ISSUE_KEYS[stringValue(issue.code)] ?? "providers.bindingIssueGeneric";
  return translate(key, { key: keyLabel });
}

function isApplyResult(value: unknown): value is IpcResults["apply"] {
  const result = asRecord(value);
  return (result.status === "applied" || result.status === "partial" || result.status === "failed")
    && typeof result.completed_operations === "number"
    && typeof result.pending_operations === "number"
    && Array.isArray(result.issues);
}

function applyResultMessage(result: IpcResults["apply"], translate: Translate, appliedKey?: string | null): string {
  // A write that did not finish is reported for what the user can see: the
  // change is not in effect.  Core's own operation accounting (the completed
  // and pending relay steps) stays internal, and no pane renders an issue card
  // over its content — the permanent status strip states the outcome.
  if (result.status === "partial") return translate("common.notAppliedPartial");
  if (result.status === "failed") return translate("common.notApplied");
  return translate(appliedKey ?? "common.saved");
}

// A bare lowercase step name (`relay_binding_materialization_failed`) names the
// Core step that failed, not anything the user can act on, so it never reaches
// the status strip: the pane reports the outcome instead.
const INTERNAL_STEP_NAME = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/;

function errorMessage(reason: unknown, translate: Translate): string {
  const code = stringValue(asRecord(reason).code);
  if (reason instanceof Error && reason.message === "Claude supports at most 3 fallback models") {
    return translate("validation.claudeFallbackModelLimit");
  }
  const keyByCode: Record<string, string> = {
    // The host bridge names a Core that never answered; the editor names a
    // document Core refused to hand over because it is past the raw editor's
    // own size budget.  Neither may read as the other.
    core_unavailable: "error.coreUnavailable",
    editor_too_large: "error.editorTooLarge",
    confirmation_required: "error.confirmationRequired",
    revision_conflict: "error.revisionConflict",
    validation_failed: "error.validationFailed",
    // Core names the domain that refused the write: a provider/model draft
    // that is genuinely unusable reads as a validation failure, while a relay
    // that is not ready states its own cause instead of blaming the edit.
    provider_model_invalid: "error.validationFailed",
    relay_preflight_failed: "error.relayNotReady",
    relay_not_ready: "error.relayNotReady",
    // A linked relay key that could not be resolved names its own cause: the
    // route's resource, base URL, or upstream model, never a bare rollback.
    relay_binding_failed: "error.relayBindingFailed",
    // A write that rolled back: name the outcome the pane can show, never the
    // relay step that raised it.
    apply_failed: "common.notApplied",
    // A WebDAV sync names its cause: the remote bundle is from another app
    // version, the two sides diverged, or the sync simply failed.  Each case
    // states the way out, because the probe beside it already said the
    // connection itself is fine.
    webdav_sync_incompatible: "error.webdavSyncIncompatible",
    webdav_sync_conflict: "error.webdavSyncConflict",
    webdav_sync_failed: "error.webdavSyncFailed",
  };
  const known = keyByCode[code];
  if (known) return translate(known);
  // Surface the Core's already-sanitized failure detail instead of a generic
  // "error" so an actionable cause (for example a rejected value) is visible.
  const detail = stringValue(asRecord(reason).message).trim();
  if (detail && detail.length <= 160 && !INTERNAL_STEP_NAME.test(detail)) return detail;
  return translate("error.generic");
}

function isEditorCapabilityConflict(reason: unknown): boolean {
  const code = stringValue(asRecord(reason).code);
  return code === "invalid_editor" || code === "revision_conflict";
}

function isRevisionConflict(reason: unknown): boolean {
  return stringValue(asRecord(reason).code) === "revision_conflict";
}

/**
 * Actions that answer a question about external state instead of staging an
 * edit: a WorkBuddy account or catalog read, and an official-account
 * authorization poll.  None of them mutates the draft, and each one can block
 * for as long as its backend does — starting a Node worker, reading the
 * desktop app's credential, refreshing a token, or waiting on a relay round
 * trip.  They therefore run on their own lane instead of the buffered queue:
 * a worker's cold start once held that queue for seconds, and because every
 * ordinary `dispatch` acquires the pane-wide wait *before* it enqueues, the
 * user could not even name the wait that was disabling their window.  Their
 * answers are delivered as a Core revision and a snapshot refresh, which the
 * subscription also delivers, so nothing depends on queue order for them.
 */
const TRANSIENT_READ_ACTIONS: ReadonlySet<string> = new Set([
  "workbuddy_status",
  "workbuddy_models",
  "workbuddy_login",
  "service_provider_auth_status",
  "provider_auth_status",
]);

function isTransientReadAction(type: string): boolean {
  return TRANSIENT_READ_ACTIONS.has(type.replace(/[.-]/g, "_").toLowerCase());
}

function isRevisionRetryableAction(type: string): boolean {
  const normalized = type.replace(/[.-]/g, "_").toLowerCase();
  // A live projection is not a staged edit: it carries no absolute value a
  // later revision could invalidate, so it rebases like the editor actions do.
  if (TRANSIENT_READ_ACTIONS.has(normalized)) return true;
  // Provider/model editor actions address their target by a stable editor id
  // and set absolute values, so they never depend on the revision they were
  // queued with. Core checks that revision only after the request acquires the
  // store lock, and a provider Apply holds it while it rewrites the config and
  // reloads the managed proxy. A control therefore reads Core's shared
  // revision before an in-flight operation (this pane's own immediate Apply,
  // a probe, a relay refresh, another window, or a host menu dispatch) has
  // released it and finished bumping it. Rebasing once on the authoritative
  // snapshot keeps the user's edit instead of reporting a conflict for a
  // change that never happened.
  const editorMutation = normalized.startsWith("model_") || normalized.startsWith("provider_");
  return editorMutation
    || normalized === "service_provider_add"
    || normalized.startsWith("service_provider_auth_")
    || normalized.startsWith("provider_auth_")
    || normalized === "account_add"
    || normalized === "account_detect_type"
    || normalized === "resources_refresh"
    || normalized === "provider_fetch_models"
    || normalized === "providers_fetch_models"
    || normalized === "fetch_models"
    || normalized === "provider_fetch_relay_resource_models"
    || normalized === "api_key_set_auto_grouping"
    || normalized === "api_key_auto_group_align"
    // Station rebinding is idempotent and Core keeps a no-op rebind clean,
    // so the automatic base-URL match may safely retry after a snapshot
    // refresh instead of surfacing a transient revision conflict.
    || normalized === "provider_select_relay_station"
    // Launch-at-login is an absolute Core preference written immediately, not
    // a staged draft, and the shared revision also advances for work this
    // window never saw (a clean-draft external settings reload while the pane's
    // snapshot was catching up). Rebasing once keeps the switch honest instead
    // of claiming the user changed settings outside the window.
    || normalized === "service_autostart_enable"
    || normalized === "service_autostart_disable"
    // The background-launch promise is the same kind of write: an absolute
    // Core preference, never a staged draft.
    || normalized === "service_launch_background_enable"
    || normalized === "service_launch_background_disable";
}

function domainState(snapshot: CoreSnapshot | undefined, domain: ConfigDomain): UnknownRecord {
  const record = asRecord(snapshot?.domains[domain]);
  const state = asRecord(record.state);
  return Object.keys(state).length > 0 ? state : record;
}

function codexModelCatalogState(snapshot: CoreSnapshot | undefined): UnknownRecord {
  return asRecord(domainState(snapshot, "codex").model_catalog);
}

function codexModelCatalogRestartSignature(catalog: UnknownRecord): string {
  const models = Array.isArray(catalog.public_models)
    ? Array.from(new Set(catalog.public_models
        .filter((value): value is string => typeof value === "string")
        .map((value) => value.trim())
        .filter(Boolean))).sort()
    : [];
  return JSON.stringify({ enabled: booleanValue(catalog.enabled), models });
}

function domainForRoute(route: AppRoute): ConfigDomain | undefined {
  switch (route) {
    case "providers-models": return "providers_models";
    case "provider-wizard": return "providers_models";
    case "codex-settings": return "codex";
    case "claude-settings": return "claude";
    case "runtime-settings": return "runtime";
    case "general-settings": return "runtime";
    case "logs": return "logs";
    default: return undefined;
  }
}

function isAssistantSettingsRoute(route: AppRoute): boolean {
  return route === "codex-settings" || route === "claude-settings";
}

function isSettingsRoute(route: AppRoute): boolean {
  return route === "providers-models" || route === "codex-settings" || route === "claude-settings" || route === "runtime-settings";
}

function isEditableDiskDomain(value: ConfigDomain | undefined): value is EditableDiskDomain {
  return value === "providers_models" || value === "codex" || value === "claude" || value === "clients" || value === "runtime" || value === "webdav";
}

function ensureSelectedOption(options: AssistantSettingOption[], value: string): AssistantSettingOption[] {
  if (!value || options.some((option) => option.value === value)) return options;
  // A stale/unknown value must remain visible and selected. Falling back to
  // the first candidate makes the control claim that another model/provider
  // is active, which is especially dangerous for the deployment picker.
  return [{ value, label: value }, ...options];
}

function logTitle(tab: string, translate: Translate): string {
  return translate(`logs.${tab.replace(/-/g, "_")}`);
}

function recoveryLogMenuTitle(status: ServiceStatus, translate: Translate): string {
  const recovery = status.route_recovery;
  const recovering = typeof recovery?.recovering === "number" && recovery.recovering >= 0 ? recovery.recovering : 0;
  const cooldown = typeof recovery?.cooldown === "number" && recovery.cooldown >= 0 ? recovery.cooldown : 0;
  return translate("status.logsSummary", { recovering, cooldown });
}

function webdavMenuStatus(serviceWebdav: ServiceStatus["webdav"] | undefined, enabled: boolean, translate: Translate): string {
  if (!(serviceWebdav?.enabled ?? enabled)) return translate("webdav.status.disabled");
  const checkedAt = serviceWebdav?.checked_at;
  if (!checkedAt) return translate("webdav.status.unknown");
  const time = formatMenuTimestamp(checkedAt);
  if (serviceWebdav.ok === true) {
    return translate(serviceWebdav.action === "probe" ? "webdav.status.connectionOk" : "webdav.status.enabled", { time });
  }
  if (serviceWebdav.ok === false) return translate("webdav.status.failed", { time });
  return translate("webdav.status.unknown");
}

function formatMenuTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }).format(parsed);
}

/**
 * The stamp beside a pane's command: today reads as a time, anything older
 * carries its date, because a sync that last landed weeks ago must not read
 * like one that just ran.
 */
function formatActionTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const now = new Date();
  const sameDay = parsed.getFullYear() === now.getFullYear() && parsed.getMonth() === now.getMonth() && parsed.getDate() === now.getDate();
  const options: Intl.DateTimeFormatOptions = sameDay
    ? { hour: "2-digit", minute: "2-digit", hour12: false }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
  return new Intl.DateTimeFormat(undefined, options).format(parsed);
}

function containsPrivateMarker(value: unknown): boolean {
  if (typeof value === "string") return value.includes("configured") || value.includes("<private-path>");
  if (Array.isArray(value)) return value.some(containsPrivateMarker);
  return Object.values(asRecord(value)).some(containsPrivateMarker);
}

export function YoungRouterApp({ ipc, native, translate: hostTranslate, initialSnapshot, routeRequest, routeRequestSequence, logTabRequest, fileIdRequest, nativeAction, isPrimaryHost = true, isWindowManagerHost = false }: YoungRouterAppProps): React.JSX.Element {
  const [route, setRoute] = useState<AppRoute>(routeRequest ?? "home");
  const [snapshot, setSnapshot] = useState<CoreSnapshot | undefined>(initialSnapshot);
  const [error, setError] = useState<string | undefined>();
  const [serviceOperationPendingCount, setServiceOperationPendingCount] = useState(0);
  const serviceOperationPending = serviceOperationPendingCount > 0;
  const snapshotLanguage = snapshot?.language;
  const translate = useMemo<Translate>(() => !snapshotLanguage || snapshotLanguage === "system" ? hostTranslate : createTranslator(snapshotLanguage), [hostTranslate, snapshotLanguage]);
  const handledNativeActions = useRef(new Set<string>());
  // The desktop host starts its service once while opening, except after an
  // explicit Stop issued before that startup attempt completes.
  const serviceShouldBeRunning = useRef(true);
  const startupAttempts = useRef(0);
  const serviceOperationQueue = useRef<Promise<void>>(Promise.resolve());
  // The launch presents the providers-and-models window unless the user asked
  // for a background-only launch. One decision per process, taken from the
  // first snapshot that answers the question, so a deep link or a Dock reopen
  // never re-runs it.
  const launchPresented = useRef(false);
  const acceptedSnapshotRevision = useRef<number>(initialSnapshot?.revision ?? -1);
  // Core can be recreated after an IPC/subscription recovery, so its local
  // change_event counter may start over. Deduplicate by the actual catalog
  // signature instead of by that process-local counter.
  const presentedCatalogRestartSignature = useRef<string | undefined>(undefined);
  const acknowledgedCatalogRestartSignature = useRef<string | undefined>(undefined);
  const catalogRestartConfirmationOpen = useRef(false);

  const recordMenuAction = useCallback(async (action: string): Promise<void> => {
    try {
      await ipc.dispatch({ domain: "logs", type: "logs.record_menu_action", payload: { tab: "actions", menu_action: action } });
    } catch {
      // Menu telemetry is local and non-critical; the requested action has
      // already completed and must not be reported as failed because its
      // diagnostic line could not be appended.
    }
  }, [ipc]);

  const receiveSnapshot = useCallback((next: CoreSnapshot): void => {
    // The Core revision orders mutations.  Same-revision snapshots remain
    // useful for live log projections, so only discard stale responses.
    if (next.revision < acceptedSnapshotRevision.current) return;
    acceptedSnapshotRevision.current = next.revision;
    setSnapshot(next);
    setError(undefined);
    if (isPrimaryHost) {
      native.menuBar.setStatus(next.service);
    }
  }, [isPrimaryHost, native]);

  const refreshSnapshot = useCallback(async (publishUnchanged = true): Promise<CoreSnapshot> => {
    const next = await ipc.snapshot();
    if (publishUnchanged || next.revision !== acceptedSnapshotRevision.current) receiveSnapshot(next);
    return next;
  }, [ipc, receiveSnapshot]);

  const runServiceOperation = useCallback((operation: ServiceOperation): Promise<CoreSnapshot | undefined> => {
    if (operation === "stop") serviceShouldBeRunning.current = false;
    if (operation === "start" || operation === "restart") serviceShouldBeRunning.current = true;
    if (snapshot && (operation === "start" || operation === "restart" || operation === "reload")) {
      receiveSnapshot({ ...snapshot, service: { ...snapshot.service, state: "starting" } });
    }
    setServiceOperationPendingCount((count) => count + 1);
    const queued = serviceOperationQueue.current.catch(() => undefined).then(async () => {
      try {
        await ipc.dispatch({ type: `service.${operation}` });
        return await refreshSnapshot();
      } catch {
        // A lifecycle operation can fail while Core itself is still healthy
        // (for example, a child process cannot bind its configured port).
        // Preserve the settings/menu surface and refresh its actual state;
        // only surface the global Core error if that refresh also fails.
        try {
          return await refreshSnapshot();
        } catch {
          native.menuBar.setStatus({ state: "unknown" });
          setError(hostTranslate("error.coreUnavailable"));
          return undefined;
        }
      }
    });
    serviceOperationQueue.current = queued.then(() => undefined, () => undefined);
    void queued.finally(() => setServiceOperationPendingCount((count) => Math.max(0, count - 1)));
    return queued;
  }, [hostTranslate, ipc, native, receiveSnapshot, refreshSnapshot, snapshot]);

  useEffect(() => {
    if (!isPrimaryHost) return;
    native.setLocalization({
      appTitle: translate("app.title"), about: translate("status.about"), autoStart: translate("status.autoStart"), codexModelCatalog: translate("status.codexModelCatalog"), serviceUnavailable: translate("error.coreUnavailable"),
      serviceStatus: translate("service.status", { status: "{status}" }),
      serviceStarting: translate("service.starting"), serviceRunning: translate("service.running"),
      serviceRunningOnPort: translate("service.runningOnPort", { port: "{port}" }),
      serviceUnhealthy: translate("service.unhealthy"), serviceStopped: translate("service.stopped"),
      serviceUnknown: translate("service.unknown"),
      languageMenu: translate("status.language"), languageSystem: translate("language.system"),
      languageEnglish: translate("language.english"), languageSimplifiedChinese: translate("language.simplified_chinese"),
      cancel: translate("status.cancel"), set: translate("common.set"), clear: translate("common.clear"), stage: translate("common.stageRaw"), find: translate("common.find"),
      findNext: translate("common.findNext"), edit: translate("common.edit"), undo: translate("common.undo"),
      redo: translate("common.redo"), cut: translate("common.cut"), copy: translate("common.copy"),
      paste: translate("common.paste"), selectAll: translate("common.selectAll"), settings: translate("status.settings"),
      reload: translate("status.reload"), closeWindow: translate("status.close"), menuQuit: translate("status.quit"), version: translate("common.version"),
      build: translate("common.build"), ok: translate("common.ok"), invalidText: translate("common.invalidText"),
      routeHome: translate("route.home"), routeProvidersModels: translate("card.providersModels"),
      routeGeneralSettings: translate("status.general"),
      routeCodexSettings: translate("status.codex"), routeClaudeSettings: translate("card.claudeSettings"),
      routeRuntimeSettings: translate("card.runtimeSettings"),
      routeDataManagement: translate("card.dataManagement"), routeProviderWizard: translate("providers.wizard.title"), routeFileEditor: translate("settings.editFile"), routeLogs: translate("card.logs"),
      providerAuthInstruction: translate("relay.officialProviderWebViewHint"),
      providerAuthBlocked: translate("relay.officialProviderAuthBlocked"),
      logOriginal: translate("logs.originalRecord"),
      providerAuthCode: translate("providers.authUserCode"),
      providerAuthCopy: translate("common.copy"),
      modelChooserTitle: translate("modelChooser.title"), modelChooserHeading: translate("modelChooser.heading"),
      childSaving: translate("common.saving"),
      modelChooserProvider: translate("modelChooser.provider"), modelChooserKey: translate("modelChooser.key"),
      modelChooserSearch: translate("modelChooser.search"), modelChooserAll: translate("modelChooser.all"),
      modelChooserSelectAllVisible: translate("modelChooser.selectAllVisible"), modelChooserInvert: translate("modelChooser.invert"),
      modelChooserInvertVisible: translate("modelChooser.invertVisible"), modelChooserAddSelected: translate("modelChooser.addSelected"),
      modelChooserCount: translate("modelChooser.count", { count: "{count}" }),
      modelChooserCountFiltered: translate("modelChooser.countFiltered", { visible: "{visible}", total: "{total}" }),
      modelChooserCountSelected: translate("modelChooser.countSelected", { count: "{count}" }),
      modelChooserEmpty: translate("modelChooser.empty"), modelChooserNoMatches: translate("modelChooser.noMatches"),
      fileFilterJson: translate("fileFilter.json"), fileFilterAll: translate("fileFilter.all"),
    });
    // The first Core snapshot can arrive before localization. Re-project the
    // current service state so the native status row gains its actual port
    // instead of retaining a bootstrap title until a later service update.
    if (snapshot) {
      native.menuBar.setStatus(snapshot.service);
    }
  }, [isPrimaryHost, native, snapshotLanguage, translate]);

  useEffect(() => {
    let mounted = true;
    const receive = (next: CoreSnapshot): void => {
      if (!mounted) return;
      receiveSnapshot(next);
    };
    const unsubscribe = ipc.subscribe((event) => receive(event.snapshot));
    const latest = ipc.latestSnapshot();
    if (latest && latest !== initialSnapshot) receive(latest);
    if (!latest) {
      void refreshSnapshot().catch(() => {
        if (mounted) {
          native.menuBar.setStatus({ state: "unknown" });
          setError(hostTranslate("error.coreUnavailable"));
        }
      });
    }
    if (isPrimaryHost) native.setShortcuts({ openMenu: "Cmd+, / Ctrl+,", closeWindow: "Esc", reload: "Cmd+R / Ctrl+R" });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [hostTranslate, initialSnapshot, ipc, isPrimaryHost, native, receiveSnapshot, refreshSnapshot]);

  useEffect(() => {
    if (!isPrimaryHost || !snapshot || launchPresented.current) return;
    // A host that already has a route to show (a deep link, a restored
    // window, the login item handing over a pane) has presented the launch
    // itself; only a windowless launch still owes the user a window.
    if (routeRequest && routeRequest !== "home") return;
    launchPresented.current = true;
    if (snapshot.service.launch_background_state === "enabled") {
      // "home" is the shared term for leaving the settings shell behind the
      // menu bar: the macOS host closes the settings window and keeps only the
      // hidden React host, the Windows host hides the one window its launch
      // already created, so neither presents anything.
      native.window.focus("home");
      return;
    }
    native.window.open("providers-models");
    native.window.focus("providers-models");
  }, [isPrimaryHost, native, routeRequest, snapshot]);

  useEffect(() => {
    if (!isPrimaryHost || !snapshot || !serviceShouldBeRunning.current) return;
    const serviceState = snapshot.service.state;
    if (serviceState === "running") {
      // A healthy service restores the launch budget for a later stop.
      startupAttempts.current = 0;
      return;
    }
    if (serviceState !== "stopped") return;
    // A login-time launch races every other start-up item on the machine, so
    // the proxy can fail while spawning its workers. Retry the launch start on
    // a backoff instead of leaving the app stopped with no way back.
    const attempt = startupAttempts.current;
    if (attempt >= SERVICE_STARTUP_RETRY_DELAYS_MS.length) return;
    startupAttempts.current = attempt + 1;
    const delay = SERVICE_STARTUP_RETRY_DELAYS_MS[attempt];
    if (delay <= 0) {
      void runServiceOperation("start");
      return;
    }
    const timer = setTimeout(() => { void runServiceOperation("start"); }, delay);
    return () => clearTimeout(timer);
  }, [isPrimaryHost, runServiceOperation, snapshot?.service.state]);

  useEffect(() => {
    if (!routeRequest) return;
    if (isWindowManagerHost) {
      if (routeRequest !== "home") {
        const windowRoute = canonicalWindowRoute(routeRequest);
        // The native host already opened or focused the shared settings
        // window before it emitted this action. Re-opening a settings pane
        // here would emit the same action again and loop, so only non-pane
        // surfaces (the provider wizard) need the React-side open.
        if (!isSettingsPaneRoute(windowRoute)) {
          native.window.open(windowRoute);
          native.window.focus(windowRoute);
        }
      }
      return;
    }
    setRoute(routeRequest);
    if (isPrimaryHost && routeRequest !== "home") {
      const windowRoute = canonicalWindowRoute(routeRequest);
      native.window.open(windowRoute);
      native.window.focus(windowRoute);
    }
  }, [isPrimaryHost, isWindowManagerHost, native, routeRequest, routeRequestSequence]);

  useEffect(() => {
    if (!isPrimaryHost) return;
    const action = nativeAction?.id;
    if (!action) return;
    const actionKey = `${nativeAction.sequence}:${action}`;
    if (handledNativeActions.current.has(actionKey)) return;
    if (action.startsWith("open-")) {
      handledNativeActions.current.add(actionKey);
      void recordMenuAction(action);
      return;
    }
    const serviceOperation = serviceOperationForNativeAction(action);
    if (serviceOperation) {
      handledNativeActions.current.add(actionKey);
      void runServiceOperation(serviceOperation).then(() => recordMenuAction(action));
    } else if (action === "toggle-autostart" && snapshot) {
      handledNativeActions.current.add(actionKey);
      const enabled = snapshot.service.auto_start_state !== "enabled";
      const operation = !enabled
        ? "service.autostart_disable"
        : "service.autostart_enable";
      void (async () => {
        let coreRevision = snapshot.revision;
        try {
          const updated = await ipc.dispatch({ type: operation }, snapshot.revision);
          coreRevision = updated.revision;
          await native.setLaunchAtLogin(enabled);
          const current = await ipc.snapshot();
          receiveSnapshot(current);
          await recordMenuAction(action);
        } catch {
          if (coreRevision !== snapshot.revision) {
            try {
              await ipc.dispatch({
                type: enabled ? "service.autostart_disable" : "service.autostart_enable",
              }, coreRevision);
            } catch {
              // Keep the original failure safe for the shared error surface.
            }
          }
          setError(hostTranslate("error.coreUnavailable"));
        }
      })();
    } else if (action === "toggle-codex-model-catalog" && snapshot) {
      handledNativeActions.current.add(actionKey);
      const enabled = !booleanValue(codexModelCatalogState(snapshot).enabled);
      void (async () => {
        try {
          await ipc.dispatch({ domain: "codex", type: "codex.model_catalog.set", payload: { enabled } });
          receiveSnapshot(await ipc.snapshot());
          await recordMenuAction(action);
        } catch {
          setError(hostTranslate("error.coreUnavailable"));
        }
      })();
    } else if (action.startsWith("set-language-") && snapshot) {
      const language = action.slice("set-language-".length);
      if (language !== "system" && language !== "en" && language !== "zh-Hans") return;
      handledNativeActions.current.add(actionKey);
      void (async () => {
        try {
          const staged = await ipc.dispatch({ domain: "language", type: "set_language", payload: { language } }, snapshot.revision);
          await ipc.apply("language", staged.revision);
          receiveSnapshot(await ipc.snapshot());
          await recordMenuAction(action);
        } catch {
          setError(hostTranslate("error.coreUnavailable"));
        }
      })();
    }
  }, [hostTranslate, ipc, isPrimaryHost, nativeAction, receiveSnapshot, recordMenuAction, runServiceOperation, snapshot]);

  useEffect(() => {
    if (!isPrimaryHost || Platform.OS !== "macos" || !snapshot) return;
    const catalog = codexModelCatalogState(snapshot);
    const signature = codexModelCatalogRestartSignature(catalog);
    if (!booleanValue(catalog.restart_required)) {
      presentedCatalogRestartSignature.current = undefined;
      return;
    }
    if (signature === presentedCatalogRestartSignature.current
      || signature === acknowledgedCatalogRestartSignature.current
      || catalogRestartConfirmationOpen.current) return;
    presentedCatalogRestartSignature.current = signature;
    catalogRestartConfirmationOpen.current = true;
    void (async () => {
      let acknowledgementCommitted = false;
      const acknowledge = async (): Promise<void> => {
        try {
          await ipc.dispatch({ domain: "codex", type: "acknowledge_model_catalog_restart", payload: {} });
          // The Core action is committed before it emits a snapshot. Do not
          // turn a follow-up projection failure into a second prompt.
          acknowledgementCommitted = true;
          acknowledgedCatalogRestartSignature.current = signature;
        } catch (reason) {
          // A Core action can be committed while its post-action emission
          // fails (for example while the service status is transient). Read
          // the authoritative snapshot before deciding to present again.
          try {
            const current = await ipc.snapshot();
            const currentCatalog = codexModelCatalogState(current);
            receiveSnapshot(current);
            if (!booleanValue(currentCatalog.restart_required)) {
              acknowledgementCommitted = true;
              acknowledgedCatalogRestartSignature.current = codexModelCatalogRestartSignature(currentCatalog);
              return;
            }
          } catch {
            // Preserve the original failure below.
          }
          throw reason;
        }
        try {
          receiveSnapshot(await ipc.snapshot());
        } catch {
          // The acknowledgement is already committed; the next subscription
          // event or explicit snapshot will refresh the UI state.
        }
      };
      try {
        let restartFailed = false;
        for (;;) {
          const choice = await native.showCodexRestartConfirmation({
            title: translate("codex.modelCatalogRestartTitle"),
            message: restartFailed
              ? `${translate("codex.modelCatalogRestartBody")}\n\n${translate("codex.modelCatalogRestartFailed")}`
              : translate("codex.modelCatalogRestartBody"),
            restartLabel: translate("codex.modelCatalogRestartNow"),
            laterLabel: translate("codex.modelCatalogRestartLater"),
          });
          if (choice !== "restart" || await native.restartCodex()) {
            await acknowledge();
            return;
          }
          restartFailed = true;
        }
      } catch {
        // The native panel is independent from every settings window. Keep
        // those windows usable if its acknowledgement is temporarily
        // unavailable, then present the same outstanding event again when
        // the next Core snapshot arrives.
        if (!acknowledgementCommitted) presentedCatalogRestartSignature.current = undefined;
      } finally {
        catalogRestartConfirmationOpen.current = false;
      }
    })();
  }, [hostTranslate, ipc, isPrimaryHost, native, receiveSnapshot, snapshot, translate]);

  useEffect(() => {
    if (!isPrimaryHost || !snapshot) return;
    const serviceState = snapshot.service.state;
    const serviceActive = serviceState === "running" || serviceState === "starting" || serviceState === "unhealthy";
    const serviceStartAvailable = !serviceOperationPending && serviceState === "stopped";
    const serviceRestartAvailable = !serviceOperationPending && serviceState !== "unknown" && serviceState !== "starting";
    const serviceReloadAvailable = !serviceOperationPending && (serviceState === "running" || serviceState === "unhealthy");
    const catalog = codexModelCatalogState(snapshot);
    const actions = [
      { id: "toggle-autostart", title: translate("status.autoStart"), enabled: true, checked: snapshot.service.auto_start_state === "enabled" },
      ...(Platform.OS === "macos" ? [{ id: "toggle-codex-model-catalog", title: translate("status.codexModelCatalog"), enabled: true, checked: booleanValue(catalog.enabled) }] : []),
      ...routeMenuActions(translate).filter(({ id }) => id !== "open-data-management" && id !== "open-logs"),
      { id: "webdav-status", title: `${translate("webdav.label")}: ${webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate)}`, enabled: false },
      ...routeMenuActions(translate).filter(({ id }) => id === "open-data-management"),
      { id: "open-logs", title: recoveryLogMenuTitle(snapshot.service, translate), enabled: true },
      { id: "service-start", title: translate("service.start"), enabled: serviceStartAvailable },
      { id: "service-stop", title: translate("service.stop"), enabled: !serviceOperationPending && serviceActive },
      { id: "service-restart", title: translate("service.restart"), enabled: serviceRestartAvailable },
      { id: "service-reload", title: translate("service.reload"), enabled: serviceReloadAvailable },
      { id: "service-health", title: translate("service.health"), enabled: !serviceOperationPending },
      { id: "language-picker", title: translate("status.language"), enabled: true },
      { id: "set-language-system", title: translate("language.system"), enabled: true, checked: snapshot.language === "system" },
      { id: "set-language-en", title: translate("language.english"), enabled: true, checked: snapshot.language === "en" },
      { id: "set-language-zh-Hans", title: translate("language.simplified_chinese"), enabled: true, checked: snapshot.language === "zh-Hans" },
      { id: "show-version", title: translate("status.version"), enabled: true },
      { id: "quit", title: translate("status.quit"), enabled: true },
    ];
    native.menuBar.setActions(actions);
  }, [isPrimaryHost, native, serviceOperationPending, snapshot, translate]);

  // The native host keeps one window for every settings pane. Capture the
  // route the window was created with so a pane switch can still close that
  // exact native window; later panes only change the shared route state.
  const settingsWindowRoute = useRef<AppRoute | undefined>(undefined);
  if (settingsWindowRoute.current === undefined && isSettingsShellRoute(route)) {
    settingsWindowRoute.current = canonicalWindowRoute(route);
  }

  // The provider wizard is the exempted sub-window: hold immediate apply while
  // it is mounted so its partial provider/key/model steps are not written and
  // reloaded before the user finishes. The provider pane applies the finished
  // draft as soon as the window closes.
  useEffect(() => {
    if (route !== "provider-wizard") return;
    setProviderWizardOpen(true);
    return () => setProviderWizardOpen(false);
  }, [route]);

  return (
    <View style={styles.root} accessibilityLabel={translate("app.title")}>
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {!error && route !== "home" && snapshot ? (isSettingsShellRoute(route)
        ? <SettingsShell route={route} windowRoute={settingsWindowRoute.current ?? canonicalWindowRoute(route)} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={receiveSnapshot} onNavigate={setRoute} onClose={() => setRoute("home")} />
        : <RouteSurface route={route} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} fileIdRequest={fileIdRequest} nativeAction={nativeAction} onSnapshot={receiveSnapshot} onNavigate={setRoute} onClose={() => setRoute(route === "provider-wizard" && Platform.OS === "windows" ? "providers-models" : "home")} />) : null}
      {!error && route === "home" ? <View style={styles.menuBarHost} /> : null}
    </View>
  );
}

/**
 * The single settings window. A native source list on the left selects one of
 * the shared route surfaces on the right. Every pane commits its staged
 * configuration as it changes, so there is no route-level Apply/Close footer;
 * only the child surfaces (the provider wizard, the raw file editor, and
 * 分组管理) keep explicit actions.
 */
function SettingsShell({ route, windowRoute, snapshot, ipc, native, translate, logTabRequest, nativeAction, onSnapshot, onNavigate, onClose, onRegisterFlush }: {
  route: AppRoute;
  windowRoute: AppRoute;
  snapshot: CoreSnapshot;
  ipc: IpcClient;
  native: NativeLeafAdapter;
  translate: Translate;
  logTabRequest?: LogTab;
  nativeAction?: { id: string; sequence: number };
  onSnapshot: (next: CoreSnapshot) => void;
  onNavigate: (route: AppRoute) => void;
  onClose: () => void;
  onRegisterFlush?: (flush?: () => Promise<boolean>) => void;
}): React.JSX.Element {
  const flushActivePane = useRef<((purpose?: "close" | "navigate") => Promise<boolean>) | undefined>(undefined);
  const registerFlush = useCallback((flush?: (purpose?: "close" | "navigate") => Promise<boolean>): void => {
    flushActivePane.current = flush;
  }, []);
  const closing = useRef(false);
  const pane = canonicalWindowRoute(route);
  useEffect(() => {
    if (!nativeAction?.id.startsWith("request-close-") || closing.current) return;
    closing.current = true;
    void (async () => {
      let canClose = true;
      try {
        // Commit the focused field before the window disappears so the last
        // edit still reaches Core and applies immediately. A field with an
        // invalid draft reports false and keeps the window open for repair.
        canClose = await flushActivePane.current?.() ?? true;
      } catch {
        // A rejected commit must not strand the window on screen.
      }
      if (!canClose) {
        closing.current = false;
        // The native host ordered the window out before asking React to
        // confirm. Bring it back so the invalid field and its inline error
        // stay visible for repair.
        native.window.focus(windowRoute);
        return;
      }
      try {
        // macOS closes the registered settings window by its registry key;
        // the Windows host tracks the active pane in its single window.
        native.window.close(Platform.OS === "windows" ? pane : windowRoute);
      } finally {
        onClose();
      }
    })();
  }, [native.window, nativeAction?.id, nativeAction?.sequence, onClose, pane, windowRoute]);
  // Window-level dialog slot: a pane publishes its dialog node here so it
  // covers the whole settings window instead of the pane that opened it.

  const [appInfo, setAppInfo] = useState<{ app: string; litellm: string; icon?: string }>();
  useEffect(() => {
    let active = true;
    if (!native.versionInfo) return undefined;
    void native.versionInfo().then((next) => { if (active) setAppInfo(next); }).catch(() => undefined);
    return () => { active = false; };
  }, [native]);
  // Beta Display's native settings layout: an app identity header and a pane
  // source list. About lives in the native application/tray menu.
  const paneRows = useMemo(() => SETTINGS_PANES.map(({ id, titleKey }) => ({ key: id, cells: [translate(titleKey)] })), [translate]);
  const paneSymbols = useMemo(() => SETTINGS_PANES.map(({ id }) => SETTINGS_PANE_PRESENTATION[id]?.symbol ?? ""), []);
  const paneSymbolColors = useMemo(() => SETTINGS_PANES.map(({ id }) => SETTINGS_PANE_PRESENTATION[id]?.color ?? ""), []);
  const paneImageNames = useMemo(() => SETTINGS_PANES.map(({ id }) => SETTINGS_PANE_PRESENTATION[id]?.image ?? ""), []);
  const paneTitleKey = SETTINGS_PANES.find(({ id }) => id === pane)?.titleKey ?? "app.title";
  const lastSelection = useRef<{ key: string; at: number }>({ key: "", at: 0 });
  const selectPane = (key: string): void => {
    // The native table emits both a selection change and the row action for a
    // single click; keep one logical selection.
    const now = Date.now();
    if (lastSelection.current.key === key && now - lastSelection.current.at < 250) return;
    lastSelection.current = { key, at: now };
    const next = SETTINGS_PANES.find(({ id }) => id === key)?.id;
    if (!next) return;
    // Leaving a pane commits and applies what that pane staged, exactly as
    // closing the window does.  A field that commits on blur, a secret that
    // only stages when asked, and the pane's own debounced apply all belong to
    // the surface being left: without this, a provider edit stayed an
    // unapplied draft (the proxy kept serving the old configuration) and a
    // typed WebDAV password or raw-editor draft was dropped with the unmount.
    void flushActivePane.current?.("navigate").catch(() => undefined);
    onNavigate(next);
    // Let the native host follow the pane: it retitles the Windows window and
    // keeps the active route in sync so a later close hides that window.
    native.window.open(next);
  };
  return <View style={styles.settingsShell}>
    <View style={styles.settingsSidebar}>
      <View style={styles.settingsSidebarHeader}>
        {appInfo?.icon ? <Image source={{ uri: appInfo.icon }} style={styles.settingsSidebarAppIcon} /> : null}
        <Text numberOfLines={1} style={styles.settingsSidebarTitle}>{translate("app.title")}</Text>
      </View>
      <NativeTable
        columns={[{ label: "", width: 186 }]}
        rows={paneRows}
        selectedKey={pane}
        striped={false}
        compact={false}
        sourceList
        rowSymbols={paneSymbols}
        rowSymbolColors={paneSymbolColors}
        rowImageNames={paneImageNames}
        cellHorizontalPadding={12}
        firstColumnHorizontalPadding={12}
        scrollTrailingColumnOverflow
        onSelectionChange={selectPane}
        style={styles.settingsSidebarList}
      />
      <View style={styles.settingsSidebarSpacer} />
    </View>
    <View style={styles.settingsDetail}>
      <View style={styles.settingsPaneHeader}><Text numberOfLines={1} style={styles.settingsPaneTitle}>{translate(paneTitleKey)}</Text></View>
      <View style={[styles.settingsDetailBody, (pane === "runtime-settings" || pane === "data-management" || isAssistantSettingsRoute(pane)) && styles.settingsDetailBodyBare]}>
        <View style={styles.settingsDetailPane}>
          <RouteSurface key={pane} route={pane} shell windowRoute={windowRoute} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={onSnapshot} onNavigate={onNavigate} onClose={onClose} onRegisterFlush={registerFlush} />
        </View>
      </View>
    </View>
  </View>;
}

/**
 * The settings window's subordinate rail. A pane that splits its body into a
 * list plus a detail — the runtime settings table of contents, the backup &
 * sync tabs, and the external client list — renders this one surface, so the
 * second level is one control, not three hand-built variants.
 *
 * The rail is a *compact* source list: it keeps the sidebar's chrome, type step,
 * and ink, and drops one density step below the sidebar's 30 pt row, because the
 * platform's own in-pane rows sit under its sidebar rows (a macOS sidebar row
 * measures 32 pt, a single-line row inside a pane about 28 pt).  A source list
 * draws no box of its own either: the rail is already bounded by its one right
 * divider, so the list it renders stays borderless, and its selection capsule
 * keeps the platform's inset on both ends — the same rhythm the sidebar's own
 * rows are drawn with.
 */
const SETTINGS_RAIL_WIDTH = 156;
const SETTINGS_RAIL_COLUMN_WIDTH = 148;
/**
 * The one settings-field grid every settings pane uses (general, runtime
 * settings, backup and sync, external clients): a left-aligned label column, the control
 * column that holds the value or switch, and a tip wrapped under that control
 * column.  The columns move together — a pane that picks its own widths or its
 * own indentation stops lining up with the pane the user just left — and the
 * geometry lives here as style objects so every pane spreads the same row
 * instead of restating the numbers.
 */
const SETTINGS_FIELD_LEAD = 8;
const SETTINGS_FIELD_LABEL_WIDTH = 128;
const SETTINGS_FIELD_GAP = 6;
/** The wider column a control whose value is a route name takes. */
const SETTINGS_FIELD_ROUTE_WIDTH = 200;
const SETTINGS_FIELD_VALUE_WIDTH = 160;
/** Where the control column and the tips under it start, inside one row. */
const SETTINGS_FIELD_HELP_INDENT = SETTINGS_FIELD_LEAD + SETTINGS_FIELD_LABEL_WIDTH + SETTINGS_FIELD_GAP;
/** The one content inset every settings pane body starts from. */
const SETTINGS_PANE_INSET = 14;

function SettingsRail({ rows, selectedKey, onSelectionChange }: {
  rows: { key: string; cells: string[] }[];
  selectedKey: string;
  onSelectionChange: (key: string) => void;
}): React.JSX.Element {
  // The rail draws its one divider, and the list inside it keeps the native
  // frame: a bordered table is how the user knows where a list ends, so the
  // rail is the one column that shows a list box as well as a right edge.
  return <View style={styles.settingsRail}>
    <NativeTable
      columns={[{ label: "", width: SETTINGS_RAIL_COLUMN_WIDTH }]}
      rows={rows}
      selectedKey={selectedKey}
      striped={false}
      compact
      sourceList
      cellHorizontalPadding={8}
      firstColumnHorizontalPadding={8}
      onSelectionChange={onSelectionChange}
      style={styles.settingsRailList}
    />
    <View style={styles.settingsRailDivider} />
  </View>;
}

/**
 * The detail header of a split settings pane — the selected rail row's name,
 * its one-line hint when the pane has one, and the pane's own controls on the
 * trailing edge. External clients and runtime settings render this one surface,
 * so the third column keeps one header height, one type step, and one divider
 * across panes instead of each pane drawing its own header band.
 */
function SettingsDetailHeader({ title, hint, actions }: { title: string; hint?: string; actions?: React.ReactNode }): React.JSX.Element {
  return <View style={styles.settingsRailDetailHeader}>
    <View style={styles.settingsRailDetailTitleBlock}>
      <Text numberOfLines={1} style={styles.settingsRailDetailTitle}>{title}</Text>
      {hint ? <Text numberOfLines={1} style={styles.settingsRailDetailHint}>{hint}</Text> : null}
    </View>
    <View style={styles.settingsRailDetailActions}>{actions}</View>
  </View>;
}

function WindowTitle({ title }: { title: string }): React.JSX.Element {
  return <View style={styles.windowTitleBlock}><Text style={styles.windowTitle}>{title}</Text></View>;
}

/** Immediate apply is blocked while a sub-window with explicit actions is open. */
function immediateApplyBlocked(): boolean {
  return isProviderWizardOpen() || isAssistantEditorOpen() || isGroupManagerOpen();
}

function useImmediateApplyBlocked(): boolean {
  const [blocked, setBlocked] = useState<boolean>(immediateApplyBlocked);
  useEffect(() => subscribeProviderWizard(() => setBlocked(immediateApplyBlocked())), []);
  return blocked;
}

function IconButton({ label, symbol, title, disabled, onPress }: { label: string; symbol?: "chevron-up" | "chevron-down" | "copy" | "minus" | "pause" | "play" | "plus" | "trash"; title: string; disabled?: boolean; onPress: () => void }): React.JSX.Element {
  const nativeSymbol: "chevron-up" | "chevron-down" | "copy" | "minus" | "pause" | "play" | "plus" | "trash" | undefined = symbol
    ?? (label === "+" ? "plus" : label === "−" ? "minus" : label === "⧉" ? "copy" : label === "↑" ? "chevron-up" : label === "↓" ? "chevron-down" : undefined);
  return <NativeButton title={label} symbol={nativeSymbol} toolTip={title} accessibilityLabel={title} compact disabled={disabled} onPress={onPress} style={styles.iconButton} />;
}

function WindowTabs({ values, selected, disabled, onSelect, style, nativeRef }: { values: Array<{ id: string; title: string }>; selected: string; disabled?: boolean; onSelect: (id: string) => void; style?: StyleProp<ViewStyle>; nativeRef?: React.Ref<HostInstance> }): React.JSX.Element {
  const labels = values.map((item) => item.title);
  const selectedValue = values.find((item) => item.id === selected)?.title ?? labels[0] ?? "";
  return <NativeSegmentedControl ref={nativeRef} labels={labels} selectedValue={selectedValue} disabled={disabled} onChange={({ nativeEvent }) => { const next = values[nativeEvent.index]; if (next) onSelect(next.id); }} style={[styles.windowTabs, style]} />;
}

function RouteSurface({ route, shell = false, windowRoute, snapshot, ipc, native, translate, logTabRequest, fileIdRequest, nativeAction, onSnapshot, onNavigate, onClose, onRegisterFlush }: { route: AppRoute; shell?: boolean; windowRoute?: AppRoute; snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; translate: Translate; logTabRequest?: LogTab; fileIdRequest?: string; nativeAction?: { id: string; sequence: number }; onSnapshot: (next: CoreSnapshot) => void; onNavigate: (route: AppRoute) => void; onClose: () => void; onRegisterFlush?: (flush?: () => Promise<boolean>) => void }): React.JSX.Element {
  const settingsRoute = isAssistantSettingsRoute(route);
  // The Codex and Claude settings routes are aliases for one shared surface.
  // Both domains stay visible and staged together, so there is no active tab
  // that can hide the other assistant's draft.
  const domain = settingsRoute ? undefined : domainForRoute(route);
  const [busy, setBusy] = useState(false);
  const [webDavOperationBusy, setWebDavOperationBusy] = useState(false);
  const [result, setResult] = useState<string>();
  const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);
  const [settingsRawBaselineToken, setSettingsRawBaselineToken] = useState(0);
  // Windows hosts one window, so its editor is a plain route the shared UI
  // navigates to (exactly like the provider wizard); macOS opens the same route
  // in a native child window of its own, which needs the host to show it.
  const [inlineEditorFile, setInlineEditorFile] = useState<ClientFile>();
  useEffect(() => {
    // Warm the host's editor window while the pane is open: creating the window
    // boots the embedded editor, so opening a file only presents the window.
    native.prepareFileEditor?.();
  }, [native]);
  const [dataManagementStatuses, setDataManagementStatuses] = useState<Partial<Record<DataManagementTab, string>>>({});
  // Which backup & sync tab is showing, so the window's one status strip
  // reports that tab's own result instead of a message another tab produced.
  const [dataManagementTab, setDataManagementTab] = useState<DataManagementTab>("import");
  const [keptDiskGeneration, setKeptDiskGeneration] = useState<Partial<Record<EditableDiskDomain, number>>>({});
  const promptedDiskGeneration = useRef<Partial<Record<EditableDiskDomain, number>>>({});
  const diskPromptInFlight = useRef(false);
  const activeRuns = useRef(0);
  // Only the runs that ask for the pane-wide wait are counted by it. A run
  // that keeps the controls available (a block's own read) must never be the
  // one that holds — or clears — that wait.
  const busyRuns = useRef(0);
  const webDavOperationInFlight = useRef(false);
  const revision = useRef<number | undefined>(snapshot?.revision);
  const latestSnapshot = useRef<CoreSnapshot | undefined>(snapshot);
  const dispatchQueue = useRef<Promise<void>>(Promise.resolve());
  // One relay read owns each account at a time. The account pane's mount read
  // and 分组管理's aligned draft ask for the same station facts seconds apart,
  // and each read costs the station one round trip of five requests, so a read
  // already in flight is joined instead of repeated and a read that just
  // succeeded answers the next implicit caller from its own result.
  const relayRefreshInFlight = useRef(new Map<string, Promise<"ready" | "unavailable">>());
  const relayRefreshFresh = useRef(new Map<string, { at: number; status: "ready" | "unavailable" }>());
  const probedSurfaceApplyQueue = useRef<Promise<void>>(Promise.resolve());
  const importPlanToken = useRef<string | undefined>(undefined);
  // The token the provider wizard staged its key value under, reported by the
  // wizard itself, so closing that surface can drop exactly what it staged.
  const providerWizardKeyToken = useRef("");
  const pendingFields = useRef(new Map<symbol, PendingField>());
  // The pending-field dirty set lives in a ref for synchronous reads, but a
  // state counter makes it reactive: the immediate-apply effect must re-run
  // when the last dirty field clears, otherwise a single text edit would wait
  // for an unrelated change before it reaches Core.
  const [pendingFieldRevision, forcePendingFieldDirtyRender] = useState(0);
  const pendingFieldDirtyIdsRef = useRef<ReadonlySet<symbol>>(new Set());

  // Relay CRUD and linked imports are one coordinated draft. The relay route
  // therefore applies both domains together whenever either side is dirty.
  const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {
    if (settingsRoute) {
      // The external-settings pane stages the raw client documents of all
      // three owning domains together; one Apply writes every dirty file.
      return (["codex", "claude", "clients"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);
    }
    if (route === "data-management") {
      return DATA_MANAGEMENT_DIRTY_DOMAINS.filter((name) => currentSnapshot?.drafts[name]?.dirty);
    }
    // The unified provider workspace stages relay account metadata and the
    // provider/model draft as one coordinated Apply.
    if (route === "providers-models") {
      return (["providers_models", "relay_accounts"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);
    }
    return domain && currentSnapshot?.drafts[domain]?.dirty ? [domain] : [];
  }, [domain, route, settingsRoute]);
  const setPendingFieldDirty = useCallback((id: symbol, dirty: boolean): void => {
    const current = pendingFieldDirtyIdsRef.current;
    if (current.has(id) === dirty) return;
    const next = new Set(current);
    if (dirty) next.add(id);
    else next.delete(id);
    pendingFieldDirtyIdsRef.current = next;
    forcePendingFieldDirtyRender((revision) => revision + 1);
  }, []);
  const fieldRegistry = useMemo<PendingFieldRegistry>(() => ({
    register: (id, field) => {
      if (field) pendingFields.current.set(id, field);
      else {
        pendingFields.current.delete(id);
        setPendingFieldDirty(id, false);
      }
    },
    setDirty: setPendingFieldDirty,
  }), [setPendingFieldDirty]);

  useEffect(() => {
    if (snapshot) {
      revision.current = snapshot.revision;
      latestSnapshot.current = snapshot;
    }
  }, [snapshot]);


  const refresh = async (): Promise<CoreSnapshot> => {
    const next = await ipc.snapshot();
    revision.current = Math.max(revision.current ?? -1, next.revision);
    latestSnapshot.current = next;
    onSnapshot(next);
    return next;
  };
  const onSecretState = (state: SecretState): void => {
    if (state.status !== "saved" || state.revision < 0) return;
    revision.current = state.revision;
    if (isAssistantSettingsRoute(route)) setSettingsRawReloadToken((current) => current + 1);
    void refresh().catch(() => undefined);
  };
  const clearSecret: NativeSecretClear = (options) => run(async () => {
    const staged = await native.clearSecret(options);
    if (staged) revision.current = staged.revision;
    return staged ?? { cancelled: true };
  }, null);
  const run = async (
    operation: () => Promise<unknown>,
    message: ResultMessage = "common.saved",
    keepControlsEnabled = false,
    refreshAfter = true,
    dataManagementTab?: DataManagementTab,
    // A child surface's own apply: it states the outcome in its own status
    // bar, so this window publishes nothing at all — not the wait, not the
    // result, and not the failure.
    quiet = false,
  ): Promise<void> => {
    const publishResult = (next: string | undefined): void => {
      if (quiet) return;
      if (dataManagementTab) {
        setDataManagementStatuses((current) => ({ ...current, [dataManagementTab]: next }));
        return;
      }
      setResult(next);
    };
    activeRuns.current += 1;
    if (!keepControlsEnabled) {
      busyRuns.current += 1;
      setBusy(true);
    }
    publishResult(undefined);
    try {
      const value = await operation();
      if (asRecord(value).cancelled === true) {
        publishResult(undefined);
      } else if (isValidation(value)) {
        // A rejected draft leaves its own rows marked in the pane
        // (`alertRowKeys`), so the strip states the outcome instead of a
        // second report of the same problem.
        publishResult(value.valid ? translate("common.saved") : translate("error.validationFailed"));
      } else if (isApplyResult(value)) {
        // An explicit caller message wins for the applied case so instant
        // applies can say "Saved" instead of the generic "Applied".
        publishResult(applyResultMessage(value, translate, typeof message === "string" ? message : null));
      } else if (typeof message === "function") {
        const key = message(value);
        publishResult(key === null ? undefined : translate(key));
      } else {
        publishResult(message === null ? undefined : translate(message));
      }
      if (refreshAfter) await refresh();
    } catch (reason: unknown) {
      publishResult(errorMessage(reason, translate));
    } finally {
      activeRuns.current -= 1;
      if (!keepControlsEnabled) {
        busyRuns.current = Math.max(busyRuns.current - 1, 0);
        if (busyRuns.current === 0) setBusy(false);
      }
    }
  };
  const runDataManagement = (tab: DataManagementTab, operation: () => Promise<unknown>, message: ResultMessage, keepControlsEnabled = false): Promise<void> => run(operation, message, keepControlsEnabled, true, tab);
  const runWebDavOperation = async (operation: () => Promise<unknown>, message: ResultMessage): Promise<void> => {
    if (webDavOperationInFlight.current) return;
    webDavOperationInFlight.current = true;
    setWebDavOperationBusy(true);
    try {
      // Core and WebDAV work is already asynchronous. Keep the route's
      // navigation and close path available while this form waits for it.
      await runDataManagement("webdav", operation, message, true);
    } finally {
      webDavOperationInFlight.current = false;
      setWebDavOperationBusy(false);
    }
  };
  // An ordinary route action carries its settings domain. A Core-level service
  // action (the launch-at-login preference) has none, so `null` explicitly
  // requests a domain-less action envelope; an omitted argument still means
  // "this route's domain" and stays an error when the route has none.
  const enqueueDispatch = (type: string, payload: UnknownRecord = {}, targetDomain: ConfigDomain | null | undefined = domain): Promise<IpcResults["dispatch"]> => {
    const actionDomain = targetDomain ?? undefined;
    if (!actionDomain && targetDomain !== null) return Promise.reject(new Error("A settings domain is required"));
    const issue = async (): Promise<IpcResults["dispatch"]> => {
      if (revision.current === undefined) await refresh();
      const action = actionDomain === undefined ? { type, payload } : { domain: actionDomain, type, payload };
      let staged: IpcResults["dispatch"];
      try {
        staged = await ipc.dispatch(action, revision.current);
      } catch (reason: unknown) {
        if (!isRevisionConflict(reason) || !isRevisionRetryableAction(type)) throw reason;
        // Every route window observes the same Core revision. Another window
        // or an official-account status refresh can advance it between this
        // surface's last snapshot and a semantic action such as Add account.
        // Rebase that action on the authoritative snapshot once; a second
        // conflict remains visible instead of hiding a real concurrent edit.
        const current = await ipc.snapshot();
        revision.current = current.revision;
        latestSnapshot.current = current;
        onSnapshot(current);
        staged = await ipc.dispatch(action, current.revision);
      }
      revision.current = staged.revision;
      return staged;
    };
    const transient = isTransientReadAction(type);
    const queued = transient
      ? issue()
      : dispatchQueue.current.catch(() => undefined).then(issue);
    if (!transient) dispatchQueue.current = queued.then(() => undefined, () => undefined);
    return queued;
  };
  // The General pane's service actions share the buffered queue, the live route
  // revision, and that one rebase, so they never fail because another surface
  // advanced the shared revision first.
  const enqueueServiceDispatch = (type: string, payload: UnknownRecord = {}): Promise<IpcResults["dispatch"]> => enqueueDispatch(type, payload, null);
  const dispatch: Dispatch = (type, payload = {}, targetDomain = domain) => run(async () => {
    const staged = await enqueueDispatch(type, payload, targetDomain);
    if (targetDomain === "codex" || targetDomain === "claude") {
      setSettingsRawReloadToken((current) => current + 1);
    }
    return staged;
  // Text inputs no longer call this path per keystroke: usePendingTextField
  // keeps their draft local and only reaches dispatch on blur/submit or the
  // shell's auto-apply flush.
  // Refresh each committed action so toggles and pickers also project their
  // new Core state immediately instead of waiting for the disk watcher.
  }, null, true);
  const dispatchWithOutcome = async (type: string, payload: UnknownRecord = {}, targetDomain = domain, keepControlsEnabled = false): Promise<CoreSnapshot | undefined> => {
    // Keep a stable, narrowed domain for the asynchronous outcome projection.
    // `targetDomain` is optional on the public helper because most callers use
    // the route's domain, but action summaries are keyed by a concrete domain.
    const actionDomain = targetDomain;
    let succeeded = false;
    let outcome: CoreSnapshot | undefined;
    await run(async () => {
      const staged = await enqueueDispatch(type, payload, targetDomain);
      succeeded = true;
      // Capture the refresh belonging to this action. Reading
      // latestSnapshot.current after run() returns is racy with the
      // authorisation status poll, which can overwrite its operation summary.
      outcome = await refresh();
      const actionSummary = staged.action_summary;
      if (actionSummary && actionDomain) {
        const currentDomainSummary = asRecord(outcome.action_summaries?.[actionDomain]);
        outcome = {
          ...outcome,
          action_summaries: {
            ...outcome.action_summaries,
            [actionDomain]: {
              ...currentDomainSummary,
              operation_summary: actionSummary,
            },
          },
        };
        latestSnapshot.current = outcome;
        onSnapshot(outcome);
      }
      return staged;
    }, null, keepControlsEnabled, false);
    return succeeded ? outcome : undefined;
  };
  const addOfficialAccount = async (kind: ServiceProviderKind, name?: string): Promise<string> => {
    const currentProviders = serviceProviderRecords(latestSnapshot.current ?? snapshot);
    // A caller that holds the account's own name wins: the surface the user
    // typed it into is the one that names the entry.
    const resolvedName = (name ?? "").trim() || nextServiceProviderName(currentProviders, kind);
    const next = await dispatchWithOutcome("service_provider.add", { kind, name: resolvedName }, "providers_models");
    if (!next) throw new Error("service_provider.add was rejected");
    const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
    const summaryID = stringValue(summary.provider_id);
    const added = serviceProviderRecords(next).find((provider) => {
      const displayName = stringValue(provider.display_name, stringValue(provider.name)).trim();
      return displayName === resolvedName;
    });
    const providerID = summaryID || (added ? editorIdentifier(added) : "");
    if (!providerID) throw new Error("service_provider.add did not return provider_id");
    return providerID;
  };
  const commitRelayMetadata: RelayCommit = async (type, payload = {}) => {
    // Relay credential cleanup needs the Core acknowledgement itself, not the
    // UI-oriented `dispatch` wrapper: that wrapper deliberately absorbs
    // failures so ordinary controls can display them in the footer.
    await enqueueDispatch(type, payload, "relay_accounts");
    // A successful dispatch is the commit point. Do not turn a subsequent
    // snapshot refresh failure into a false "not committed" result that would
    // leave credentials around forever; the subscription will reconcile it.
    try {
      await refresh();
    } catch {
      // The Core has already accepted the metadata transition.
    }
  };
  const flushPendingFields = async (): Promise<void> => {
    // Text fields and native secret fields advance the same Core revision, but
    // native secret commits use a separate host bridge. Waiting for each field
    // before starting the next one prevents a secret capability from becoming
    // stale while another pending field is being dispatched.
    for (const field of [...pendingFields.current.values()]) {
      await field.commit();
    }
    // Every field commit awaits its own enqueueDispatch promise, so its error
    // already belongs to this Apply and propagates above. The shared queue is
    // deliberately made non-rejecting for serialization; do not replay an
    // error from an unrelated, already-handled background action here.
    await dispatchQueue.current;
  };
  const flushAssistantEditorFields = async (): Promise<void> => {
    const fields = [...pendingFields.current.values()].filter((field) => (
      field.flushBeforeAssistantEditor === true && field.isDirty?.() === true
    ));
    if (fields.length === 0) return;
    for (const field of fields) {
      await field.commit();
    }
    await dispatchQueue.current;
  };
  const openAssistantFile = (file: ClientFile): void => {
    // Native button actions can run before the focused AppKit field emits its
    // blur. Commit React-owned text drafts first so the editor's first render
    // already includes every staged change for that document.
    void flushAssistantEditorFields()
      .then(() => {
        if (Platform.OS === "windows" || native.openFileEditor === undefined) {
          setInlineEditorFile(file);
          onNavigate("file-editor");
          return;
        }
        native.openFileEditor(JSON.stringify(editorTargetPayload(file, true)));
      })
      .catch((reason: unknown) => setResult(errorMessage(reason, translate)));
  };
  const hasPendingFieldEdits = useCallback((): boolean => pendingFieldDirtyIdsRef.current.size > 0
    || [...pendingFields.current.values()].some((field) => field.isDirty?.() === true), []);
  // Actions can finish with a fresh Core snapshot before the parent route has
  // rendered it into `snapshot`. Keep the close decision on the same newest
  // projection so the discard prompt sees the change the status strip already
  // reported. The ref is populated directly by IPC reads,
  // so equal revisions must prefer it too: the parent prop can still be one
  // render behind after a same-revision projection (for example an action
  // summary update).
  const actionSnapshot = latestSnapshot.current && latestSnapshot.current.revision >= (snapshot?.revision ?? -1)
    ? latestSnapshot.current
    : snapshot;
  const routeHasStagedChanges = useCallback((currentSnapshot: CoreSnapshot | undefined): boolean => (
    stagedDomainsForRoute(currentSnapshot).length > 0
    || hasPendingFieldEdits()
  ), [hasPendingFieldEdits, stagedDomainsForRoute]);
  const monitoredDiskDomains = useMemo<EditableDiskDomain[]>(() => {
    if (!isSettingsRoute(route)) return [];
    if (settingsRoute) return ["codex", "claude", "clients"];
    return isEditableDiskDomain(domain) ? [domain] : [];
  }, [domain, route, settingsRoute]);
  useEffect(() => {
    if (monitoredDiskDomains.length === 0) return;
    let active = true;
    let polling = false;
    const poll = async (): Promise<void> => {
      if (polling || busy) return;
      // Do not stage an actively edited field merely because the disk watcher
      // ticks. Its blur/explicit action will commit it before the next check.
      if (hasPendingFieldEdits()) return;
      polling = true;
      try {
        const next = await ipc.diskState(monitoredDiskDomains);
        if (!active) return;
        const previous = latestSnapshot.current;
        revision.current = Math.max(revision.current ?? -1, next.revision);
        const diskStateChanged = monitoredDiskDomains.some((diskDomain) =>
          !sameDiskState(previous?.disk[diskDomain], next.disk[diskDomain]),
        );
        let currentDisk = next.disk;
        if (!previous || diskStateChanged) {
          // Only materialize the full snapshot when the cheap disk probe found
          // a change that the editor actually needs to render.
          try {
            const refreshed = await ipc.snapshot();
            if (!active) return;
            revision.current = Math.max(revision.current ?? -1, refreshed.revision);
            latestSnapshot.current = refreshed;
            currentDisk = refreshed.disk;
            onSnapshot(refreshed);
          } catch {
            // Keep the disk marker usable during a transient full-snapshot failure.
          }
        }
        // This timer exists to observe external file changes. Publishing an
        // identical snapshot every five seconds needlessly re-renders native
        // tables and editors (and used to make focused inputs flash).
        for (const diskDomain of monitoredDiskDomains) {
          const changedGeneration = currentDisk[diskDomain]?.changed ? currentDisk[diskDomain]?.generation : undefined;
          if (!diskPromptInFlight.current && changedGeneration !== undefined && promptedDiskGeneration.current[diskDomain] !== changedGeneration) {
            // A dirty draft and a newer on-disk file require one native decision.
            promptedDiskGeneration.current = { ...promptedDiskGeneration.current, [diskDomain]: changedGeneration };
            diskPromptInFlight.current = true;
            void native.showConfirmation({
              title: translate("settings.diskChangedTitle"),
              message: translate("settings.diskChangedBody"),
              confirmLabel: translate("settings.useDisk"),
            }).then(async (useDisk) => {
              if (!active) return;
              if (useDisk) {
                await reload(diskDomain);
                return;
              }
              setKeptDiskGeneration((current) => ({ ...current, [diskDomain]: changedGeneration }));
            }).catch(() => {
              if (active) setKeptDiskGeneration((current) => ({ ...current, [diskDomain]: changedGeneration }));
            }).finally(() => {
              diskPromptInFlight.current = false;
            });
            break;
          }
          const priorGeneration = snapshot?.disk[diskDomain]?.generation ?? 0;
          if ((currentDisk[diskDomain]?.generation ?? 0) > priorGeneration && !currentDisk[diskDomain]?.changed && (diskDomain === "codex" || diskDomain === "claude")) {
            setSettingsRawBaselineToken((current) => current + 1);
          }
        }
      } catch {
        // Keep the current editor usable during a transient Core failure.
      } finally {
        polling = false;
      }
    };
    const timer = setInterval(() => { void poll(); }, SETTINGS_DISK_POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [busy, domain, hasPendingFieldEdits, ipc, monitoredDiskDomains, native, onSnapshot, snapshot?.disk.codex?.generation, snapshot?.disk.claude?.generation, snapshot?.disk.providers_models?.generation, snapshot?.disk.runtime?.generation, snapshot?.disk.webdav?.generation, translate]);
  const discardPendingFields = (): void => pendingFields.current.forEach((field) => field.reset());
  const reload = (reloadDomain = domain): Promise<void> => {
    if (!reloadDomain) return Promise.resolve();
    if (reloadDomain === domain) discardPendingFields();
    return run(async () => {
      const reloaded = await ipc.reload(reloadDomain, revision.current);
      revision.current = reloaded.revision;
      // Core invalidates editor revisions when a domain reloads. Make both raw
      // Codex editors fetch fresh documents only after that reload succeeds.
      if (reloadDomain === "codex" || reloadDomain === "claude") setSettingsRawBaselineToken((current) => current + 1);
      if (isEditableDiskDomain(reloadDomain)) {
        setKeptDiskGeneration((current) => ({ ...current, [reloadDomain]: undefined }));
      }
      return reloaded;
    }, "common.reloaded");
  };
  const resolveRawEditorConflict = useCallback<RawEditorConflictHandler>(async (editorDomain, _document) => {
    const refreshed = await ipc.snapshot();
    revision.current = Math.max(revision.current ?? -1, refreshed.revision);
    latestSnapshot.current = refreshed;
    onSnapshot(refreshed);
    const diskState = refreshed.disk[editorDomain];
    const diskChanged = diskState?.changed === true;
    const useLatest = await native.showConfirmation({
      title: translate(diskChanged ? "settings.diskChangedTitle" : "settings.editorChangedTitle"),
      message: translate(diskChanged ? "settings.diskChangedBody" : "settings.editorChangedBody"),
      confirmLabel: translate("settings.useDisk"),
    });
    if (useLatest) {
      if (diskChanged) {
        const reloaded = await ipc.reload(editorDomain, refreshed.revision);
        revision.current = reloaded.revision;
        const after = await ipc.snapshot();
        revision.current = Math.max(revision.current ?? -1, after.revision);
        latestSnapshot.current = after;
        onSnapshot(after);
        setKeptDiskGeneration((current) => ({ ...current, [editorDomain]: undefined }));
        promptedDiskGeneration.current = { ...promptedDiskGeneration.current, [editorDomain]: undefined };
      }
      return "reload";
    }
    if (diskChanged && diskState?.generation !== undefined) {
      promptedDiskGeneration.current = { ...promptedDiskGeneration.current, [editorDomain]: diskState.generation };
      setKeptDiskGeneration((current) => ({ ...current, [editorDomain]: diskState.generation }));
    }
    return "keep";
  }, [ipc, native, onSnapshot, translate]);
  const apply = (options?: { silent?: boolean; quiet?: boolean; message?: string; keepControlsEnabled?: boolean }): Promise<void> => {
    if ((!settingsRoute && !domain) || domain === "logs") return Promise.resolve();
    return run(async () => {
      await flushPendingFields();
      // Inline relay edits stage through the shared dispatch queue when their
      // fields lose focus. Wait for that queue before taking the apply
      // snapshot so the immediate apply writes the newest value.
      await dispatchQueue.current;
      const refreshed = await ipc.snapshot();
      revision.current = refreshed.revision;
      onSnapshot(refreshed);
      const domains = stagedDomainsForRoute(refreshed);
      if (domains.length === 0) return { cancelled: true };
      const risks = domains.includes("claude") ? riskCodes(refreshed, "claude") : [];
      const diskConflicts = domains.filter((name) => refreshed.disk[name]?.changed);
      const unacknowledgedDiskConflicts = diskConflicts.filter((name) => !isEditableDiskDomain(name) || keptDiskGeneration[name] !== refreshed.disk[name]?.generation);
      if (unacknowledgedDiskConflicts.length > 0) {
        const accepted = await native.showConfirmation({ title: translate("settings.diskChangedTitle"), message: translate("settings.overwriteDiskConfirm"), confirmLabel: translate("settings.keepDraft") });
        if (!accepted) return { cancelled: true };
      }
      if (risks.length > 0) {
        const accepted = await native.showConfirmation({ title: translate("claude.confirmation.title"), message: translate("claude.confirmation.required"), confirmLabel: translate("screen.confirm") });
        if (!accepted) return { cancelled: true };
      }
      const confirmations = [...risks, ...diskConflicts.map((name) => `overwrite_external_${name}`)];
      const applyOnce = (nextRevision: number): Promise<IpcResults["apply"]> => (
        settingsRoute || route === "providers-models" || route === "data-management" || domain === undefined
          ? ipc.applyDomains([...domains], nextRevision, confirmations.length > 0 ? confirmations : undefined)
          : ipc.apply(domain, nextRevision, confirmations.length > 0 ? confirmations : undefined)
      );
      let result: IpcResults["apply"];
      try {
        result = await applyOnce(refreshed.revision);
      } catch (reason: unknown) {
        if (!isRevisionConflict(reason)) throw reason;
        // Relay login/resource discovery and official-account auth status are
        // intentionally live projections. They can advance Core's shared
        // revision between this snapshot and Apply without changing the
        // staged settings the user is applying. Rebase once on the
        // authoritative snapshot; a second conflict remains visible instead
        // of silently overwriting a concurrent edit.
        const current = await ipc.snapshot();
        const currentDomains = stagedDomainsForRoute(current);
        const sameDomains = currentDomains.length === domains.length
          && domains.every((name) => currentDomains.includes(name));
        const sameDiskState = domains.every((name) => (
          current.disk[name]?.changed === refreshed.disk[name]?.changed
          && current.disk[name]?.generation === refreshed.disk[name]?.generation
        ));
        if (!sameDomains || !sameDiskState) throw reason;
        revision.current = current.revision;
        latestSnapshot.current = current;
        onSnapshot(current);
        result = await applyOnce(current.revision);
      }
      if (domains.includes("codex") || domains.includes("claude") || domains.includes("clients")) {
        setSettingsRawBaselineToken((current) => current + 1);
      }
      // Core restarts the managed proxy for providers_models and runtime
      // applies on its own thread, so the pane never waits for the restart
      // and a later edit is not stuck behind it.
      if (diskConflicts.length > 0) setKeptDiskGeneration({});
      return result;
    }, options?.silent ? null : (options?.message ?? "common.saved"), options?.keepControlsEnabled === true, true, undefined, options?.quiet === true);
  };
  // A child surface applies the draft it wrote itself (分组管理's 保存并关闭):
  // the pane it was opened from must stay silent about work it did not start,
  // and the child reports the outcome in its own status bar.
  const applyStagedQuietly = (): Promise<void> => apply({ quiet: true });
  // The settings shell commits as the user changes controls. Wait for the
  // route's serial dispatch queue, then apply the staged domains; the debounce
  // coalesces a burst of toggles into one Core apply. A failed apply keeps its
  // draft dirty but is not retried until the next edit changes the revision,
  // so a validation error cannot turn into an apply loop.
  const autoAppliedKey = useRef("");
  const immediateApplyBlocked = useImmediateApplyBlocked();
  const autoApplyKey = shell && actionSnapshot && !immediateApplyBlocked
    ? (() => {
      const domains = stagedDomainsForRoute(actionSnapshot);
      return domains.length === 0 ? "" : `${domains.join(",")}@${actionSnapshot.revision}`;
    })()
    : "";
  useEffect(() => {
    if (!shell) return;
    if (!autoApplyKey) {
      autoAppliedKey.current = "";
      return;
    }
    if (busy || hasPendingFieldEdits()) return;
    if (autoAppliedKey.current === autoApplyKey) return;
    // Instant-apply settings show their lifecycle in the status bar: saving
    // while the debounce runs, then a brief confirmation once Core accepted
    // the staged value.
    setResult(translate("common.saving"));
    const timer = setTimeout(() => {
      if (autoAppliedKey.current === autoApplyKey) return;
      autoAppliedKey.current = autoApplyKey;
      // Keep the pane interactive while the background apply runs; the
      // dispatch queue serializes any edit made in the meantime.
      void apply({ message: "common.saved", keepControlsEnabled: true });
    }, IMMEDIATE_APPLY_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [autoApplyKey, busy, hasPendingFieldEdits, pendingFieldRevision, shell, translate]);
  // The shell owns the window-close path; expose one flush so it can commit a
  // focused text/secret field and apply it before the native window closes.
  // It reports false when a field still holds an invalid draft, so the shell
  // can keep the window open instead of silently discarding the edit.
  const flushAndApply = useRef<(purpose?: "close" | "navigate") => Promise<boolean>>(() => Promise.resolve(true));
  const invalidCloseNotice = useRef(false);
  flushAndApply.current = async (purpose: "close" | "navigate" = "close"): Promise<boolean> => {
    await flushPendingFields();
    if (hasPendingFieldEdits()) {
      // The same refusal serves both exits, and each states its own next step:
      // closing keeps the window open, leaving a pane keeps that pane's draft
      // until its field is fixed.
      invalidCloseNotice.current = true;
      setResult(translate(purpose === "navigate" ? "runtime.fixInvalidBeforeLeaving" : "runtime.fixInvalidBeforeClose"));
      return false;
    }
    await dispatchQueue.current;
    await apply({ silent: true });
    return true;
  };
  // Clear the close-blocked notice as soon as the invalid drafts are fixed.
  useEffect(() => {
    if (!invalidCloseNotice.current || hasPendingFieldEdits()) return;
    invalidCloseNotice.current = false;
    setResult(undefined);
  }, [hasPendingFieldEdits, pendingFieldRevision]);
  useEffect(() => {
    if (!shell || !onRegisterFlush) return;
    onRegisterFlush(() => flushAndApply.current());
    return () => onRegisterFlush(undefined);
  }, [onRegisterFlush, shell]);
  const activateProviderAndRestart = async (): Promise<boolean> => {
    let completed = false;
    await run(async () => {
      await flushPendingFields();
      await dispatchQueue.current;
      const refreshed = await ipc.snapshot();
      revision.current = refreshed.revision;
      latestSnapshot.current = refreshed;
      onSnapshot(refreshed);
      if (refreshed.drafts.providers_models?.dirty !== true) return { cancelled: true };
      const diskChanged = refreshed.disk.providers_models?.changed === true;
      if (diskChanged) {
        const accepted = await native.showConfirmation({
          title: translate("settings.diskChangedTitle"),
          message: translate("settings.overwriteDiskConfirm"),
          confirmLabel: translate("settings.keepDraft"),
        });
        if (!accepted) return { cancelled: true };
      }
      const applied = await ipc.applyDomains(["providers_models"], refreshed.revision, diskChanged ? ["overwrite_external_providers_models"] : undefined);
      revision.current = applied.revision;
      // Core restarts the managed proxy in the background for this apply, so
      // activation must not wait for or race a second explicit restart here.
      await refresh();
      completed = applied.status === "applied";
      return applied;
    }, null, false, false);
    return completed;
  };
  const inspectImportDataManagement = async (): Promise<IpcResults["import_preview"] | undefined> => {
    let inspected: IpcResults["import_preview"] | undefined;
    await runDataManagement("import", async () => {
      const fileToken = await native.openFilePicker({ purpose: "import" });
      if (!fileToken) return { cancelled: true };
      if (revision.current === undefined) await refresh();
      inspected = await ipc.previewImport(fileToken, revision.current ?? 0);
      revision.current = inspected.revision;
      importPlanToken.current = inspected.import_plan_token;
      return inspected;
    }, "dataManagement.importInspected");
    return inspected;
  };
  const importDataManagement = async (sections: ConfigDomain[]): Promise<IpcResults["import"] | undefined> => {
    let imported: IpcResults["import"] | undefined;
    await runDataManagement("import", async () => {
      const planToken = importPlanToken.current;
      if (!planToken) throw new Error(translate("dataManagement.importHint"));
      importPlanToken.current = undefined;
      imported = await ipc.importPlan(planToken, revision.current ?? 0, sections);
      revision.current = imported.revision;
      return imported;
    }, "dataManagement.imported");
    return imported;
  };
  const confirmImportDraftReplacement = (sections: string[]): Promise<boolean> => native.showConfirmation({
    title: translate("dataManagement.tab.import"),
    message: translate("dataManagement.importReplaceDraftWarning", { sections: sections.join(" · ") }),
    confirmLabel: translate("dataManagement.importSelected"),
  });
  const exportDataManagement = (sections: ConfigDomain[]): Promise<void> => runDataManagement("export", async () => {
    const fileToken = await native.saveFilePicker({ suggestedName: "young-router-data.json" });
    if (!fileToken) return { cancelled: true };
    return ipc.export(sections, fileToken);
  }, "dataManagement.exported");
  const probeWebDav = (): Promise<void> => runWebDavOperation(async () => {
    await flushPendingFields();
    return ipc.probe(undefined, undefined, "webdav");
  }, (value) => (asRecord(value).ok === true ? "webdav.probeOk" : "webdav.probeFailed"));
  const syncWebDav = (action: WebDavSyncAction): Promise<void> => runWebDavOperation(async () => {
    await flushPendingFields();
    const refreshed = await ipc.snapshot();
    revision.current = refreshed.revision;
    latestSnapshot.current = refreshed;
    onSnapshot(refreshed);
    if (refreshed.drafts.webdav?.dirty) {
      const diskChanged = refreshed.disk.webdav?.changed === true;
      if (diskChanged) {
        const accepted = await native.showConfirmation({ title: translate("settings.diskChangedTitle"), message: translate("settings.overwriteDiskConfirm"), confirmLabel: translate("settings.keepDraft") });
        if (!accepted) return { cancelled: true };
      }
      const applied = await ipc.apply("webdav", refreshed.revision, diskChanged ? ["overwrite_external_webdav"] : undefined);
      revision.current = applied.revision;
      if (diskChanged) setKeptDiskGeneration((current) => ({ ...current, webdav: undefined }));
    }
    const dispatched = await ipc.dispatch({ domain: "webdav", type: action, payload: { sections: [...WEBDAV_SYNC_DOMAINS] } }, revision.current);
    revision.current = dispatched.revision;
    return dispatched;
  }, (value) => {
    const summary = asRecord(asRecord(value).action_summary);
    if (stringValue(summary.archived_remote) !== "") return "dataManagement.syncedReplacingRemote";
    // A setting renamed from an earlier default copies its file; a server that
    // refuses DELETE keeps the old one, which the reader has to clear.
    const adopted = asRecord(summary.adopted_remote);
    if (stringValue(adopted.name) !== "" && adopted.removed !== true) return "dataManagement.syncedLeftoverRemote";
    return "dataManagement.synced";
  });
  const dispatchDataManagement: Dispatch = (type, payload = {}, targetDomain = "webdav") => runDataManagement("webdav", async () => enqueueDispatch(type, payload, targetDomain), null, true);
  // A WebDAV setting is what the interval loop reads, so changing one applies
  // it instead of leaving it in an open window's draft: the enable switch has
  // to reach the flag file the loop checks, or it would mean nothing.  Commits
  // are serialized because each one is its own stage-then-apply pair.
  const webDavSettingsQueue = useRef<Promise<void>>(Promise.resolve());
  const commitWebDavSettings = (patch: UnknownRecord): Promise<void> => {
    const commit = async (): Promise<void> => {
      await flushPendingFields();
      const refreshed = await ipc.snapshot();
      revision.current = refreshed.revision;
      latestSnapshot.current = refreshed;
      onSnapshot(refreshed);
      const staged = await enqueueDispatch("patch", patch, "webdav");
      revision.current = staged.revision;
      const diskChanged = refreshed.disk.webdav?.changed === true;
      const applied = await ipc.apply("webdav", revision.current, diskChanged ? ["overwrite_external_webdav"] : undefined);
      revision.current = applied.revision;
    };
    const next = webDavSettingsQueue.current.catch(() => undefined).then(() => runDataManagement("webdav", commit, "common.saved"));
    webDavSettingsQueue.current = next.catch(() => undefined);
    return next;
  };
  const applyProbedSurface: ApplyProbedSurface = (providerId, modelId, nextSurface, options) => {
    let applied = false;
    const queued = probedSurfaceApplyQueue.current.catch(() => undefined).then(async () => {
      const before = await ipc.snapshot();
      revision.current = before.revision;
      onSnapshot(before);
      const currentModel = providerModelByEditorId(before, providerId, modelId);
      if (!currentModel) throw new Error("The selected model is unavailable");
      const currentSurface = stringValue(currentModel.upstream_url_surface, "openai/responses");
      if (currentSurface === nextSurface) return;
      const diskChanged = before.disk.providers_models?.changed === true;
      let confirmed = true;
      if (options?.confirmRecommendation !== false) {
        const confirmationMessage = [
          translate("providers.probeApplyMessage", {
            current: probeSurfaceLabel(currentSurface, translate),
            next: probeSurfaceLabel(nextSurface, translate),
          }),
          diskChanged ? translate("settings.overwriteDiskConfirm") : "",
        ].filter(Boolean).join("\n\n");
        confirmed = await native.showConfirmation({
          title: translate("providers.probeApplyTitle"),
          message: confirmationMessage,
          confirmLabel: translate("screen.confirm"),
        });
      } else if (diskChanged) {
        confirmed = await native.showConfirmation({
          title: translate("settings.diskChangedTitle"),
          message: translate("settings.overwriteDiskConfirm"),
          confirmLabel: translate("settings.keepDraft"),
        });
      }
      if (!confirmed) return;
      await enqueueDispatch("model.patch", {
        provider_id: providerId,
        model_id: modelId,
        changes: {
          upstream_url_surface: nextSurface,
        },
      }, "providers_models");
      const staged = await ipc.snapshot();
      revision.current = staged.revision;
      onSnapshot(staged);
      const confirmations = staged.disk.providers_models?.changed ? ["overwrite_external_providers_models"] : undefined;
      const applyStagedSurface = (nextRevision: number): Promise<IpcResults["apply"]> => (
        ipc.apply("providers_models", nextRevision, confirmations)
      );
      let result: IpcResults["apply"];
      try {
        result = await applyStagedSurface(staged.revision);
      } catch (reason: unknown) {
        if (!isRevisionConflict(reason)) throw reason;
        // The staged surface edit above dirties the draft, so this pane's own
        // immediate Apply can commit it (and advance Core's shared revision)
        // before this apply is accepted. Rebase once on the authoritative
        // snapshot; a still-staged draft means the surface was not committed
        // yet, and a clean one already has this exact surface applied.
        const current = await ipc.snapshot();
        revision.current = current.revision;
        latestSnapshot.current = current;
        onSnapshot(current);
        result = current.drafts.providers_models?.dirty === true
          ? await applyStagedSurface(current.revision)
          : { revision: current.revision, applied: true, status: "applied", domains: ["providers_models"], completed_operations: 0, pending_operations: 0, issues: [] };
      }
      revision.current = result.revision;
      await refresh();
      setResult(translate("common.saved"));
      applied = true;
    });
    probedSurfaceApplyQueue.current = queued.then(() => undefined, () => undefined);
    return queued.then(() => applied).catch((reason: unknown) => {
      setResult(errorMessage(reason, translate));
      return false;
    });
  };
  // The provider wizard reports the token its staged key is filed under, so
  // every dismissal path of that surface can drop what it staged: a wizard the
  // user closes reserves nothing.  The drop is quiet — a child surface's own
  // bookkeeping never reports into the window that opened it — and it is a
  // no-op once 完成 adopted the key.
  const registerProviderWizardKeyToken = (token: string): void => {
    providerWizardKeyToken.current = token;
  };
  const dropProviderWizardStagedKey = (): void => {
    const token = providerWizardKeyToken.current;
    providerWizardKeyToken.current = "";
    if (!token) return;
    void enqueueDispatch(
      "provider.discard_pending_key",
      { pending_api_key: token },
      "providers_models",
    ).catch(() => undefined);
  };
  const closeRoute = (): void => {
    // Keep the React route close independent from the native window registry.
    // A stale/missing native window must not strand the route on screen.
    if (route === "provider-wizard") dropProviderWizardStagedKey();
    if (route === "data-management") importPlanToken.current = undefined;
    if (route === "file-editor") {
      // The editor owns one document of the settings pane; closing it returns
      // to that pane instead of closing the settings shell.
      if (Platform.OS === "windows") native.window.open("codex-settings");
      onClose();
      return;
    }
    if (route === "provider-wizard" && Platform.OS === "windows") {
      // Windows currently owns one React host window. Restore the parent
      // route in that host instead of hiding it as a second native window.
      native.window.open("providers-models");
      onClose();
      return;
    }
    try {
      native.window.close(canonicalWindowRoute(route));
    } finally {
      onClose();
    }
  };
  const requestClose = async (): Promise<void> => {
    // The provider wizard is a native modal child window. Its Cancel button
    // and title-bar close both dismiss that child directly.
    if (route === "provider-wizard") {
      closeRoute();
      return;
    }
    const restoreWindow = (): void => {
      const windowRoute = canonicalWindowRoute(route);
      native.window.open(windowRoute);
      native.window.focus(windowRoute);
    };
    try {
      // A title-bar action can arrive before the subscription has rendered
      // the latest Core projection into this route. Read the authoritative
      // snapshot before deciding whether a discard confirmation is needed;
      // this also handles same-revision projections without treating a
      // stale prop as an edited form. If Core is temporarily unavailable,
      // retain the last local projection as the safe fallback.
      let current = actionSnapshot;
      try {
        const authoritative = await ipc.snapshot();
        if (!current || authoritative.revision >= current.revision) {
          current = authoritative;
          revision.current = authoritative.revision;
          latestSnapshot.current = authoritative;
          onSnapshot(authoritative);
        }
      } catch {
        // Fall back to the newest local projection below.
      }
      const dirtyDomains = stagedDomainsForRoute(current);
      const needsDiscardConfirmation = routeHasStagedChanges(current);
      if (!needsDiscardConfirmation) {
        closeRoute();
        return;
      }
      const confirmed = await native.showConfirmation({
        title: translate("settings.discardDraftTitle"),
        message: translate("common.discarded"),
        confirmLabel: translate("status.close"),
        destructive: true,
      });
      if (!confirmed) {
        restoreWindow();
        return;
      }
      discardPendingFields();
      closeRoute();
      for (const name of dirtyDomains) {
        try {
          if (name === "relay_accounts") {
            const reloaded = await ipc.reload(name, revision.current);
            revision.current = reloaded.revision;
          } else if (name === "language") {
            const reloaded = await ipc.reload(name, revision.current);
            revision.current = reloaded.revision;
          } else {
            await enqueueDispatch("cancel", {}, name);
          }
        } catch {
          // The user already chose to discard and the window is gone. Core
          // will reconcile the draft on the next open instead of blocking UI.
        }
      }
    } catch {
      restoreWindow();
    }
  };
  useEffect(() => {
    // The settings shell owns its window-close path so one flush covers the
    // active pane; only the provider wizard window closes itself here.
    if (shell) return;
    if (nativeAction?.id !== `request-close-${route}` && nativeAction?.id !== `request-close-${canonicalWindowRoute(route)}`) return;
    requestClose();
  }, [nativeAction?.sequence]);
  const definition = ROUTES.find((item) => item.id === route);
  const windowTitle = settingsRoute
    ? translate("status.codex")
    : translate(definition?.titleKey ?? "app.title");
  const providerWizardProviders = useMemo(() => snapshotProviderRecords(snapshot), [snapshot]);
  const providerWizardRelaySources = useMemo(() => relaySourcesFromSnapshot(snapshot), [snapshot]);
 const providerWizardRelayStations = useMemo(() => relayStationsFromSnapshot(snapshot), [snapshot]);
  const detectRelayType = useCallback(async (origin: string): Promise<RelayType | undefined> => {
    const staged = await enqueueDispatch("account.detect_type", { origin }, "relay_accounts");
    revision.current = staged.revision;
    const next = await refresh();
    const detected = asRecord(next.action_summaries?.relay_accounts).detected_type;
    return detected === "newapi" || detected === "sub2api" ? detected : undefined;
  }, [enqueueDispatch, refresh]);
  const refreshRelayResources = useCallback(async (accountId: string, options?: { force?: boolean }): Promise<"ready" | "unavailable"> => {
    // Only a caller that must not see stale facts — a sign-in that just landed
    // — passes ``force``: it always pays for a station round trip, while an
    // implicit caller reuses a read that is running or one that just finished.
    // A failed read is never reused, so the next implicit caller retries the
    // station instead of inheriting the failure.
    const forced = options?.force === true;
    const inFlight = relayRefreshInFlight.current.get(accountId);
    if (!forced && inFlight) return inFlight;
    if (!forced) {
      const fresh = relayRefreshFresh.current.get(accountId);
      if (fresh && Date.now() - fresh.at < RELAY_RESOURCE_REUSE_MS) return fresh.status;
    }
    const pending = (async (): Promise<"ready" | "unavailable"> => {
      const staged = await enqueueDispatch("resources.refresh", { account_id: accountId }, "relay_accounts");
      revision.current = staged.revision;
      // The refresh reports its own projection as the action summary; the
      // dispatch result itself only carries the revision and that summary.
      const status = asRecord(staged.action_summary).resource_status === "ready" ? "ready" : "unavailable";
      if (status === "ready") relayRefreshFresh.current.set(accountId, { at: Date.now(), status });
      return status;
    })();
    relayRefreshInFlight.current.set(accountId, pending);
    try {
      return await pending;
    } finally {
      relayRefreshInFlight.current.delete(accountId);
    }
  }, [enqueueDispatch]);
  const relayApiKeyActions = useMemo<RelayApiKeyActions>(() => ({
    create: async (accountId, options) => {
      await commitRelayMetadata("api_key.create", {
        account_id: accountId,
        name: options.name,
        ...(options.groupID ? { group_id: options.groupID } : {}),
        enabled: options.enabled,
      });
    },
    update: async (accountId, resourceId, name) => {
      await commitRelayMetadata("api_key.update", { account_id: accountId, resource_id: resourceId, name });
    },
    setEnabled: async (accountId, resourceId, enabled) => {
      await commitRelayMetadata("api_key.set_enabled", { account_id: accountId, resource_id: resourceId, enabled });
    },
    setGroup: async (accountId, resourceId, groupId) => {
      await commitRelayMetadata("api_key.set_group", { account_id: accountId, resource_id: resourceId, group_id: groupId });
    },
    setAutoGrouping: async (accountId, enabled) => {
      await enqueueDispatch("api_key.set_auto_grouping", { account_id: accountId, enabled }, "relay_accounts");
      try {
        const next = await refresh();
        return { draftStaged: next.drafts.relay_accounts?.dirty === true };
      } catch {
        // Core accepted the toggle; keep the staged message until the next
        // snapshot can confirm whether the draft was reverted.
        return { draftStaged: true };
      }
    },
    alignAutoGrouping: async (accountId) => {
      await commitRelayMetadata("api_key.auto_group_align", { account_id: accountId });
    },
    remove: async (accountId, resourceId, dependencyPolicy) => {
      await commitRelayMetadata("api_key.delete", {
        account_id: accountId,
        resource_id: resourceId,
        dependency_policy: dependencyPolicy,
      });
    },
    detach: async (accountId, resourceId) => {
      await commitRelayMetadata("api_key.detach", { account_id: accountId, resource_id: resourceId });
    },
  }), [commitRelayMetadata, enqueueDispatch, refresh]);
  // One host-facing bridge carries every staged relay mutation the merged
  // provider workspace and its wizard need; metadata stays secret-free.
  const relayBridge = useMemo<RelayWorkspaceBridge>(() => ({
    commit: commitRelayMetadata,
    detectType: detectRelayType,
    refreshResources: refreshRelayResources,
    refreshAccounts: async () => {
      // Returning the refreshed snapshot lets the wizard rebind a freshly
      // created station without racing its own stale props.
      return refresh();
    },
    apiKeyActions: relayApiKeyActions,
    applyStagedQuietly,
  }), [applyStagedQuietly, commitRelayMetadata, detectRelayType, refresh, refreshRelayResources, relayApiKeyActions]);
  // The assistant file editor is its own window on macOS and a route on
  // Windows: RouteSurface owns its state, and the host presents the window
  // instead of a pane-clipped overlay in the detail column it opened from.
  // Codex is the one client whose file names its own gateway, so the pane
  // item can tell whether it already points at this app's proxy — and, when
  // it points somewhere else, which provider and model that target is.
  const codexState = asRecord(domainState(snapshot, "codex"));
  const codexUsesLocalApi = booleanValue(codexState.uses_local_api);
  const codexStructured = asRecord(codexState.structured);
  const codexClientProvider = stringValue(codexStructured.model_provider).trim();
  const codexClientModel = stringValue(codexStructured.model).trim();
  // The router's own model list backs the pane's restore action: the client
  // chooses among the models this app serves instead of a hand-typed slug.
  const codexModelRows = asRecords(domainState(snapshot, "codex").models);
  // The pane's Codex switch mirrors the managed catalog the macOS status menu
  // toggles: checked installs this app's public model list as Codex's own
  // catalog, unchecked removes the pointer so Codex keeps its built-in list.
  const codexModelCatalogEnabled = booleanValue(codexModelCatalogState(snapshot).enabled);
  // One strip per window. Backup & sync reports per tab, so the strip shows the
  // active tab's own result; every other route reports its own work here.
  const routeStatus = result;

  return <TranslationContext.Provider value={settingsRoute ? translate : undefined}><PendingFieldContext.Provider value={fieldRegistry}><View style={styles.windowSurface}>
    {!shell && route !== "providers-models" && route !== "logs" && route !== "provider-wizard" && route !== "data-management" ? <WindowTitle title={windowTitle} /> : null}
    {route === "providers-models" || route === "provider-wizard" || route === "file-editor" || settingsRoute || route === "logs" || route === "runtime-settings" || route === "data-management" || route === "general-settings" ? <View style={[styles.windowContent, compactStyles.windowContent, styles.windowContentFixed, route === "file-editor" && styles.fileEditorRouteContent, route === "providers-models" && styles.providersContent, route === "provider-wizard" && styles.providerWizardRouteContent, settingsRoute && styles.assistantSettingsContent, route === "logs" && styles.logsContent, route === "runtime-settings" && styles.runtimeContent, route === "data-management" && styles.dataManagementContent]}>
    {route === "providers-models" ? <ProviderWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onStatus={setResult} onSecretState={onSecretState} applyProbedSurface={applyProbedSurface} onOpenWizard={() => { if (Platform.OS === "windows") onNavigate("provider-wizard"); native.window.open("provider-wizard"); }} relay={relayBridge} addOfficialAccount={addOfficialAccount} onActivateAndRestart={activateProviderAndRestart} /> : null}
    {route === "file-editor" ? <FileEditorWorkspace file={inlineEditorFile} fileId={fileIdRequest} nativeAction={nativeAction} ipc={ipc} native={native} translate={translate} busy={busy} onEditorConflict={resolveRawEditorConflict} reloadToken={settingsRawReloadToken} baselineToken={settingsRawBaselineToken} syncRevision={snapshot?.revision} onFlushPendingFields={flushPendingFields} onClose={closeRoute} /> : null}
    {route === "provider-wizard" ? <ProviderSetupWizard snapshot={snapshot} native={native} providers={providerWizardProviders} relaySources={providerWizardRelaySources} relayStations={providerWizardRelayStations} busy={busy} translate={translate} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onStatus={setResult} onClose={closeRoute} onKeyToken={registerProviderWizardKeyToken} relay={relayBridge} addOfficialAccount={addOfficialAccount} status={result} /> : null}
    {settingsRoute ? <AssistantSettingsWorkspace busy={busy} native={native} codexModels={codexModelRows} codexModelCatalogEnabled={codexModelCatalogEnabled} clientProvider={codexClientProvider} clientModel={codexClientModel} localApiActive={codexUsesLocalApi} onToggleCodexModelCatalog={(enabled) => dispatch("codex.model_catalog.set", { enabled }, "codex")} onUseLocalApi={(selection) => dispatch("use_local_api", selection, "codex")} onUseSavedModel={(selection) => dispatch("use_saved_model", selection, "codex")} translate={translate} ipc={ipc} filesToken={settingsRawBaselineToken} onOpenFile={openAssistantFile} /> : null}
    {route === "logs" ? <LogsWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onStatus={setResult} requestedTab={nativeAction?.id === "open-recovery" ? "recovery" : logTabRequest} requestedTabKey={nativeAction?.sequence ?? 0} /> : null}
    {route === "general-settings" ? <GeneralWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} dispatch={dispatch} dispatchServiceAction={enqueueServiceDispatch} translate={translate} onStatus={setResult} onSnapshot={onSnapshot} /> : null}
    {route === "runtime-settings" ? <RuntimeWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} /> : null}
    {route === "data-management" ? <DataManagementWorkspace snapshot={snapshot} tab={dataManagementTab} onTabChange={setDataManagementTab} statuses={dataManagementStatuses} busy={busy} webDavOperationBusy={webDavOperationBusy} translate={translate} dispatch={dispatchDataManagement} onSecretState={onSecretState} onFlushPendingFields={flushPendingFields} onTabSwitchError={(tab, reason) => setDataManagementStatuses((current) => ({ ...current, [tab]: errorMessage(reason, translate) }))} onInspectImport={inspectImportDataManagement} onImport={importDataManagement} onConfirmImportReplace={confirmImportDraftReplacement} onExport={exportDataManagement} onProbeWebDav={probeWebDav} onSyncWebDav={syncWebDav} onCommitWebDavSettings={commitWebDavSettings} /> : null}
    </View> : null}
    {/* One permanent status strip per settings window: the last result replaces
        the idle Ready label, so the strip never appears or disappears
        mid-action and the pane below it keeps a stable height.  A child window
        keeps no strip of its own: it states its result beside its own footer
        buttons instead. */}
    {shell ? <View style={styles.routeStatusBar}><Text numberOfLines={2} style={styles.routeStatusText}>{routeStatus ?? translate("common.ready")}</Text></View> : null}
  </View></PendingFieldContext.Provider></TranslationContext.Provider>;
}

function isValidation(value: unknown): value is ValidationSummary {
  const record = asRecord(value);
  return typeof record.valid === "boolean" && Array.isArray(record.issues);
}

function riskCodes(snapshot: CoreSnapshot, domain: ConfigDomain): string[] {
  if (domain !== "claude") return [];
  const settings = asRecord(domainState(snapshot, "claude").settings);
  return Array.isArray(settings.risk_confirmations) ? settings.risk_confirmations.filter((item): item is string => typeof item === "string") : [];
}

const PROVIDER_WIZARD_NEW_PROVIDER = "__provider_wizard_new_provider__";
const PROVIDER_WIZARD_NEW_KEY = "__provider_wizard_new_key__";
/**
 * The token a wizard files its key value under while the provider it belongs
 * to does not exist yet: 添加向导 stages every field in its own steps, and
 * 完成 is the one act that creates the provider, its key, and its models
 * together. Core holds that value (its `_WIZARD_KEY_TARGET_PREFIX`) and
 * `provider.add` adopts it — so 下一步 never mints a provider, and a wizard
 * the user closes reserves nothing.
 *
 * The prefix namespaces the *secret target* a native secure field stages
 * under, never the token itself: Core parses the target, files the value
 * under the bare token, and every action that adopts, lists, or drops the
 * staged key (`provider.add`, `providers.fetch_models`, and
 * `provider.discard_pending_key`) addresses that token verbatim.  Sending the
 * target back where Core expects the token reads as an unknown token, so the
 * wizard's own create would be refused and its staged value left behind.
 */
const WIZARD_PENDING_KEY_PREFIX = "__wizard_provider__";

function providerWizardKeyTarget(token: string, keyName: string): string {
  return `${WIZARD_PENDING_KEY_PREFIX}${token}\u001f${keyName}`;
}

type ServiceProviderKind = "openai_login" | "claude_login" | "workbuddy_login" | "workbuddy_ai_login";

/**
 * Every provider the snapshot carries, whatever its kind.
 *
 * The wizard's own lookups go through this: a provider it has just created
 * — a WorkBuddy service entry included — has to be found in the same list the
 * create answered from, and a lookup that filtered by kind would report a
 * create it never saw as "nothing happened".
 */
function snapshotProviderRecords(snapshot: CoreSnapshot | undefined): UnknownRecord[] {
  const state = domainState(snapshot, "providers_models");
  const details = asRecords(state.providers);
  return details.length > 0 ? details : (snapshot?.providers_models.providers ?? []).map(providerRecord);
}

function serviceProviderRecords(snapshot: CoreSnapshot | undefined): UnknownRecord[] {
  return snapshotProviderRecords(snapshot).filter((provider) => {
    const kind = providerAuthKind(provider);
    return kind === "openai_login" || kind === "claude_login";
  });
}

function serviceProviderKindLabel(kind: ServiceProviderKind, translate: Translate): string {
  return kind === "openai_login" ? translate("relay.officialProviderOpenAI") : translate("relay.officialProviderClaude");
}

/**
 * The persisted login kind behind each service type the pane offers.
 *
 * The pane's own vocabulary for a service provider is its short type
 * (`openai`, `claude`, `workbuddy`, `workbuddyAI`), while Core persists and
 * validates the login kind (`openai_login`, `claude_login`,
 * `workbuddy_login`, `workbuddy_ai_login`).  Anything that retargets a
 * provider through `service_provider.patch` has to translate between the two:
 * the short name is not in Core's `_SERVICE_PROVIDER_KINDS`, so a dispatch
 * that sent it would be refused as an unavailable login type.
 */
function serviceProviderKindFor(kind: ProviderKind): ServiceProviderKind | undefined {
  return kind === "openai" ? "openai_login"
    : kind === "claude" ? "claude_login"
      : kind === "workbuddy" ? "workbuddy_login"
        : kind === "workbuddyAI" ? "workbuddy_ai_login"
          : undefined;
}

function nextServiceProviderName(providers: UnknownRecord[], kind: ServiceProviderKind): string {
  const base = kind === "openai_login" ? "OpenAI"
    : kind === "claude_login" ? "Claude"
      : kind === "workbuddy_login" ? "WorkBuddy"
        : "WorkBuddy AI";
  const names = new Set(providers.map((provider) => stringValue(provider.display_name, stringValue(provider.name)).trim().toLocaleLowerCase()).filter(Boolean));
  if (!names.has(base.toLocaleLowerCase())) return base;
  let suffix = 2;
  while (names.has(`${base} ${suffix}`.toLocaleLowerCase())) suffix += 1;
  return `${base} ${suffix}`;
}

function ProviderSetupWizard({ snapshot, native, providers, relaySources, relayStations, busy, translate, dispatchWithOutcome, onSecretState, onStatus, onClose, onKeyToken, relay, addOfficialAccount, status }: { snapshot?: CoreSnapshot; native: NativeLeafAdapter; providers: UnknownRecord[]; relaySources: RelaySourceOption[]; relayStations: RelayStationOption[]; busy: boolean; translate: Translate; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>; onSecretState: (state: SecretState) => void; onStatus: (status?: string) => void; onClose: () => void; onKeyToken: (token: string) => void; relay: RelayWorkspaceBridge; addOfficialAccount: (kind: ServiceProviderKind, name?: string) => Promise<string>; /** The window's own result: a child window keeps no strip, so its footer states it. */ status?: string }): React.JSX.Element {
  type WizardStep = "provider" | "keys" | "model";
  type WizardType = "api" | "openai" | "claude" | "workbuddy" | "workbuddyAI";
  const [step, setStep] = useState<WizardStep>("provider");
  const [providerType, setProviderType] = useState<WizardType>("api");
  const [providerMode, setProviderMode] = useState<"new" | "existing">("new");
  const [providerSelection, setProviderSelection] = useState(PROVIDER_WIZARD_NEW_PROVIDER);
  const [providerName, setProviderName] = useState("");
  const [providerBaseURL, setProviderBaseURL] = useState("");
  const [typeDetection, setTypeDetection] = useState<"checking" | RelayType | "unknown" | undefined>(undefined);
  // Keys step.
  const [keyPath, setKeyPath] = useState<"login" | "manual">("manual");
  const [loginPhase, setLoginPhase] = useState<"idle" | "sign-in">("idle");
  const [loginBusy, setLoginBusy] = useState(false);
  const loginFeedback = useRef<string | undefined>(undefined);
  const [, forceLoginFeedbackRender] = useState(0);
  const setupLoginRequest = useRef(0);
  const [signedInAccountID, setSignedInAccountID] = useState<string>();
  const [providedKeySelection, setProvidedKeySelection] = useState("");
  const [keySelection, setKeySelection] = useState("");
  const [keyName, setKeyName] = useState("");
  const [keyReady, setKeyReady] = useState(false);
  // The key the wizard will give the provider it creates, and the token its
  // value is staged under until then.  The value's target never moves while
  // the user types: it carries this session's token and this session's key
  // name, and the finished step names the key the user actually kept.
  const [stagedKeyName] = useState(() => randomKeyName([]));
  const [pendingKeyToken] = useState(() => `wizard-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`);
  useEffect(() => {
    // The window that opened this surface drops the staged key on every
    // dismissal path, so it needs the token while the wizard is still up.
    onKeyToken(pendingKeyToken);
  }, [onKeyToken, pendingKeyToken]);
  // Model step.
  const [modelName, setModelName] = useState("");
  const [upstreamModel, setUpstreamModel] = useState("");
  const [fetchedModelCandidates, setFetchedModelCandidates] = useState<string[]>([]);
  const [fetchedModelCapabilities, setFetchedModelCapabilities] = useState<Record<string, UnknownRecord>>({});
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [manualModels, setManualModels] = useState<Array<{ id: string; name: string; upstream_model: string }>>([]);
  const [selectedManualModelIDs, setSelectedManualModelIDs] = useState<string[]>([]);
  const [modelFetchState, setModelFetchState] = useState<"idle" | "loading" | "ready" | "empty" | "unavailable">("idle");
  const modelFetchRequest = useRef(0);
  const manualModelID = useRef(0);
  const [processing, setProcessing] = useState(false);
  const [validation, setValidation] = useState("");
  // WorkBuddy: the account is the desktop app's own sign-in and the roster is
  // its live catalog, so the wizard reads both instead of asking for a secret.
  const [workbuddyAccount, setWorkbuddyAccount] = useState<UnknownRecord | undefined>(undefined);
  const [workbuddyBusy, setWorkbuddyBusy] = useState(false);
  // The wizard's one status line: its own validation or feedback, and the wait
  // while the embedded sign-in page is up.  No step body repeats it.
  const wizardStatus = validation || loginFeedback.current || (loginPhase === "sign-in" ? translate("relay.loginWorking") : "");
  // 完成 and every other wizard action report their outcome through the
  // window's result.  This window renders no strip — its own footer is its one
  // status line — so a refused create has to land here or the user sees a
  // press that did nothing at all.
  const wizardFooterStatus = wizardStatus || status || "";
  const shownChallenge = useRef<Record<string, string>>({});
  const setLoginFeedbackMessage = (message: string | undefined): void => {
    loginFeedback.current = message;
    forceLoginFeedbackRender((value) => value + 1);
  };

  const isOfficialLoginType = providerType === "openai" || providerType === "claude";
  const isWorkBuddyType = providerType === "workbuddy" || providerType === "workbuddyAI";
  const isLoginType = isOfficialLoginType || isWorkBuddyType;
  const workbuddyProvider = providerType === "workbuddyAI" ? "workbuddy-ai" : "workbuddy";
  const workbuddyAuthKind: ServiceProviderKind = providerType === "workbuddyAI" ? "workbuddy_ai_login" : "workbuddy_login";
  const selectedProvider = providers.find((entry) => editorIdentifier(entry) === providerSelection);
  const providerID = selectedProvider ? editorIdentifier(selectedProvider) : "";
  const selectedProviderName = selectedProvider
    ? stringValue(selectedProvider.display_name, stringValue(selectedProvider.name, providerID))
    : providerName.trim();
  const activeProviderBaseURL = selectedProvider
    ? stringValue(selectedProvider.endpoint, stringValue(selectedProvider.api_base))
    : providerBaseURL;
  // A vendor is tied to a station purely by its base URL; no vendor type.
  const stationForProvider = useMemo(
    () => relayStations.find((station) => stationOriginKey(station.baseURL) === stationOriginKey(activeProviderBaseURL)),
    [activeProviderBaseURL, relayStations],
  );
  const relayAccounts = useMemo(() => accountsFromSnapshot(snapshot), [snapshot]);
  // Provided keys stay scoped to the selected provider's station: either the
  // account this wizard session signed into, or the station bound to the
  // provider's base URL. An unscoped fallback would list every station's
  // keys, which is why unrelated providers once leaked into this list.
  const providedChoices = useMemo(() => {
    const scoped = signedInAccountID
      ? relaySources.filter((source) => source.accountID === signedInAccountID)
      : stationForProvider
        ? relaySources.filter((source) => stationOriginKey(source.baseURL) === stationOriginKey(stationForProvider.baseURL))
        : [];
    return scoped.filter((source) => source.enabled);
  }, [relaySources, signedInAccountID, stationForProvider]);
  const keyChoices = selectedProvider ? providerKeyChoices(selectedProvider, relaySources, activeProviderBaseURL) : [];
  const keyOptions = [
    { value: PROVIDER_WIZARD_NEW_KEY, label: translate("providers.wizard.addApiKey") },
    ...keyChoices.filter((choice) => choice.kind === "independent").map((choice) => ({ value: choice.id, label: providerKeyChoiceLabel(choice, translate) })),
  ];
  const activeKeySelection = keySelection || (keyOptions[1]?.value ?? PROVIDER_WIZARD_NEW_KEY);
  const selectedKeyChoice = keyChoices.find((choice) => choice.id === activeKeySelection);
  // A provider this wizard is creating does not exist yet, so its key value is
  // staged under the wizard's own token (see `providerWizardKeyTarget`).
  const creatingProvider = providerMode === "new" && !providerID;
  const stagedKeyTarget = providerWizardKeyTarget(pendingKeyToken, stagedKeyName);
  const finishedProviderKeyName = keyName.trim() || stagedKeyName;
  const selectedProvidedChoice = providedChoices.find((source) => `relay:${relaySourceSelectionID(source)}` === providedKeySelection) ?? providedChoices[0];
  const manualSelectedKeyName = selectedKeyChoice?.name ?? (activeKeySelection === PROVIDER_WIZARD_NEW_KEY ? keyName.trim() : "");
  const selectedKeyReady = Boolean(selectedKeyChoice && selectedKeyChoice.kind === "independent" && selectedKeyChoice.state?.configured) || keyReady;
  const modelCandidates = useMemo(() => {
    const values = [...(selectedProvidedChoice?.models ?? []), ...fetchedModelCandidates];
    return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
  }, [fetchedModelCandidates, selectedProvidedChoice?.models]);
  // "Select an existing provider" matches by service: an official address
  // typed by hand is the same service as the one the wizard injects.
  const wizardService: ServiceID | undefined = providerType === "api" ? undefined : providerType;
  const providerOptions = providers
    .filter((entry) => wizardService === undefined
      ? providerKind(entry) === "apiKey" || providerKind(entry) === "relay"
      : providerService(entry) === wizardService && !(providerAuthKind(entry) === "api_key" && keyPath === "login"))
    .map((entry) => {
      const id = editorIdentifier(entry);
      return { value: id, label: stringValue(entry.display_name, stringValue(entry.name, id)) };
    });
  const officialStatus = isOfficialLoginType && selectedProvider ? providerAuthStatus(selectedProvider) : "signed_out";
  const modelChoicesForOfficial = useMemo(() => (isOfficialLoginType && selectedProvider ? asRecords(selectedProvider.models).map(modelRecord) : []), [isOfficialLoginType, selectedProvider]);
  const servicePickerItems: Array<{ type: WizardType; label: string }> = [
    { type: "api", label: translate("providers.wizard.typeApi") },
    { type: "openai", label: translate("providers.type.openai") },
    { type: "claude", label: translate("providers.type.claude") },
    { type: "workbuddy", label: translate("providers.type.workbuddy") },
    { type: "workbuddyAI", label: translate("providers.type.workbuddyAI") },
  ];
  const servicePickerLabels = servicePickerItems.map((item) => item.label);
  // A service provider always carries its own address; only the custom entry
  // edits the URL field.
  const serviceBaseURL = providerType === "api" ? providerBaseURL : SERVICE_BASE_URLS[providerType];
  const selectedServiceLabel = servicePickerItems.find((item) => item.type === providerType)?.label ?? servicePickerLabels[0] ?? "";
  const stepItems: Array<{ id: WizardStep; title: string }> = [
    { id: "provider", title: translate("providers.wizard.stepProvider") },
    { id: "keys", title: translate("providers.wizard.stepKeys") },
    { id: "model", title: translate("providers.wizard.stepModel") },
  ];

  const loadWorkBuddyAccount = async (refresh = false): Promise<UnknownRecord | undefined> => {
    setWorkbuddyBusy(true);
    try {
      // Reading the account is this step's own wait: it can start the worker
      // and ask the desktop app for the credit, so every other control — the
      // name, the type, the address, Back and 完成 — stays usable throughout.
      // This is the same seam the provider editor's association block uses.
      const next = await dispatchWithOutcome("workbuddy_status", { refresh }, "providers_models", true);
      if (!next) return undefined;
      const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
      const account = asRecord(asRecord(summary.providers)[workbuddyProvider]);
      setWorkbuddyAccount(account);
      return account;
    } finally {
      setWorkbuddyBusy(false);
    }
  };
  useEffect(() => {
    if (step === "keys" && isWorkBuddyType) void loadWorkBuddyAccount(true);
    // The selected type and the reached step are the only triggers: a reload
    // on every render would fight the refresh button beside the account.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, providerType]);
  const providerStepValid = (): boolean => {
    const name = providerName.trim();
    if (!name) {
      setValidation(translate("providers.wizard.required"));
      return false;
    }
    if (providerNameExists(providers, name, providerMode === "existing" ? providerID : "")) {
      setValidation(translate("providers.wizard.duplicateName"));
      return false;
    }
    // A custom provider carries its own address, and the step that asks for
    // it is where its absence is stated.
    if (providerMode === "new" && !serviceBaseURL.trim()) {
      setValidation(translate("providers.wizard.required"));
      return false;
    }
    return true;
  };
  // Present the device-code challenge for official account logins.
  const presentAuthChallenge = (next: CoreSnapshot | undefined, kind: ServiceProviderKind, label: string, accountFingerprint: string): void => {
    const summary = asRecord(asRecord(next?.action_summaries?.providers_models).operation_summary);
    const verificationURL = stringValue(summary.verification_uri);
    const userCode = stringValue(summary.user_code);
    const callbackURL = stringValue(summary.redirect_uri);
    if (!verificationURL || (!userCode && !callbackURL)) return;
    const fingerprint = `${accountFingerprint}|${kind}|${verificationURL}|${userCode}|${callbackURL}`;
    if (shownChallenge.current[accountFingerprint] === fingerprint) return;
    shownChallenge.current[accountFingerprint] = fingerprint;
    const options = {
      title: label + " " + translate("relay.officialProviderLogin"),
      closeLabel: translate("status.close"),
    };
    if (native.showProviderAuth) {
      void native.showProviderAuth({
        provider: kind === "openai_login" ? "openai" : "claude",
        fingerprint: accountFingerprint,
        verificationURL,
        ...(userCode ? { userCode } : {}),
        ...(callbackURL ? { callbackURL } : {}),
        ...options,
      }).catch(() => undefined);
    } else {
      void native.showReadOnlyText({
        ...options,
        text: [verificationURL, userCode || callbackURL].join("\n"),
        language: "text",
        html: readOnlyCodeEditorHtml(editorMenuLabels(translate)),
      });
    }
  };
  useEffect(() => {
    if (!isOfficialLoginType || officialStatus !== "authorizing" || !providerID) return;
    const kind: ServiceProviderKind = providerType === "claude" ? "claude_login" : "openai_login";
    const timer = setInterval(() => {
      void dispatchWithOutcome("service_provider.auth_status", { provider_id: providerID }, "providers_models", true)
        .then((next) => presentAuthChallenge(next, kind, selectedProviderName || serviceProviderKindLabel(kind, translate), providerID))
        .catch(() => undefined);
    }, 1_000);
    return () => clearInterval(timer);
    // The challenge presentation deduplicates by fingerprint, so re-running
    // the poll on label or status changes is safe.
  }, [dispatchWithOutcome, isLoginType, officialStatus, providerID, providerType, selectedProviderName, translate]);

  // Keep window geometry aligned with the embedded sign-in step.
  useEffect(() => {
    const showingSignIn = loginPhase === "sign-in";
    void native.window.setContentSize?.("provider-wizard", showingSignIn ? 900 : 620, showingSignIn ? 620 : 460);
  }, [loginPhase, native.window]);

  useEffect(() => {
    if (!selectedProvider || processing || keySelection === PROVIDER_WIZARD_NEW_KEY || keyOptions.some((choice) => choice.value === keySelection)) return;
    setKeySelection(keyOptions[1]?.value ?? PROVIDER_WIZARD_NEW_KEY);
    setKeyReady(false);
  }, [keyOptions, keySelection, processing, selectedProvider]);

  // Seed the editable key-name field once per key selection: the name the
  // staged value is filed under for a provider this wizard will create, a
  // random word for a new key of an existing one, and an existing independent
  // key's own name.
  const keyNameSeedSelection = useRef<string>("");
  useEffect(() => {
    if (step !== "keys" || keyNameSeedSelection.current === activeKeySelection) return;
    if (activeKeySelection === PROVIDER_WIZARD_NEW_KEY) {
      keyNameSeedSelection.current = activeKeySelection;
      setKeyName(creatingProvider ? stagedKeyName : randomKeyName([]));
      return;
    }
    if (selectedKeyChoice && selectedKeyChoice.kind === "independent") {
      keyNameSeedSelection.current = activeKeySelection;
      setKeyName(selectedKeyChoice.name);
    }
  }, [activeKeySelection, creatingProvider, selectedKeyChoice, stagedKeyName, step]);

  useEffect(() => {
    modelFetchRequest.current += 1;
    setModelName("");
    setUpstreamModel("");
    setFetchedModelCandidates([]);
    setFetchedModelCapabilities({});
    setSelectedModels([]);
    setManualModels([]);
    setSelectedManualModelIDs([]);
    setModelFetchState("idle");
  }, [activeKeySelection, providedKeySelection, providerID]);

  const chooseProviderType = (value: WizardType): void => {
    setProviderType(value);
    setProviderMode("new");
    setProviderSelection(PROVIDER_WIZARD_NEW_PROVIDER);
    setKeySelection("");
    setKeyName("");
    setKeyReady(false);
    // The keys step re-seeds its own fields for the type just chosen.
    keyNameSeedSelection.current = "";
    setValidation("");
    // An official service signs in through its own web login first; a custom
    // key stays available as the second choice for OpenAI and Claude.
    setKeyPath(value === "api" ? "manual" : "login");
    setSignedInAccountID(undefined);
    setProvidedKeySelection("");
    // A non-custom type carries the service's own address, so the URL field is
    // filled from the service table and every later step treats it as fixed.
    setProviderBaseURL(value === "api" ? "" : SERVICE_BASE_URLS[value]);
    // The entry is named after its own product, and a second entry for the
    // same account has to stay distinguishable.
    setProviderName(value === "workbuddy" ? nextServiceProviderName(providers, "workbuddy_login")
      : value === "workbuddyAI" ? nextServiceProviderName(providers, "workbuddy_ai_login")
        : value === "openai" ? nextServiceProviderName(providers, "openai_login")
          : value === "claude" ? nextServiceProviderName(providers, "claude_login")
            : "");
    setWorkbuddyAccount(undefined);
    setSelectedModels([]);
    setModelFetchState("idle");
  };
  useEffect(() => {
    // Keep a service entry's suggested name unique. The first suggestion runs
    // from the picker's own callback, which may still see the provider list of
    // the render that mounted it; this effect re-runs whenever that list
    // arrives and repairs the untouched default (never a name the user typed).
    // Only the provider step renames: once the entry exists, the suggestion is
    // no longer this draft's to change.
    if (step !== "provider" || providerMode !== "new" || providerType === "api") return;
    const kind: ServiceProviderKind = providerType === "workbuddy" ? "workbuddy_login"
      : providerType === "workbuddyAI" ? "workbuddy_ai_login"
        : providerType === "claude" ? "claude_login"
          : "openai_login";
    const baseName = nextServiceProviderName([], kind);
    const suggested = nextServiceProviderName(providers, kind);
    setProviderName((current) => (current.trim() === "" || current.trim() === baseName ? suggested : current));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providers, providerType, providerMode, keyPath, step]);

  /** Switch one official service between its web login and a custom key. */
  const chooseOfficialAuthPath = (path: "login" | "manual"): void => {
    setKeyPath(path);
    setKeySelection("");
    setKeyName("");
    setKeyReady(false);
    keyNameSeedSelection.current = "";
    setValidation("");
    setProviderName(path === "login"
      ? nextServiceProviderName(providers, providerType === "claude" ? "claude_login" : "openai_login")
      : "");
  };
  const chooseExistingProvider = (value: string): void => {
    setProviderMode("existing");
    setProviderSelection(value);
    // An existing account-backed entry keeps its own key path: a login
    // provider shows the account, a custom-key entry shows its key.
    const existing = providers.find((entry) => editorIdentifier(entry) === value);
    setKeyPath(existing && providerAuthKind(existing) === "api_key" ? "manual" : "login");
    setKeySelection("");
    keyNameSeedSelection.current = "";
    setKeyName("");
    setKeyReady(false);
    setValidation("");
    setSignedInAccountID(undefined);
    setProvidedKeySelection("");
  };
  const chooseProviderMode = (value: "new" | "existing"): void => {
    const first = providerOptions[0];
    setProviderMode(value);
    setProviderSelection(value === "new" ? PROVIDER_WIZARD_NEW_PROVIDER : first?.value ?? PROVIDER_WIZARD_NEW_PROVIDER);
    setKeyPath(value === "new" ? (providerType === "api" ? "manual" : "login") : first && providerAuthKind(first) === "api_key" ? "manual" : "login");
    setKeySelection("");
    setKeyName("");
    setKeyReady(false);
    keyNameSeedSelection.current = "";
    setValidation("");
    setSignedInAccountID(undefined);
    setProvidedKeySelection("");
  };
  // Tracks the last auto-suggested provider name so the name field follows
  // the URL as it is completed ("api" → "openai" for api.openai.com) until
  // the user edits the name manually.
  const lastSuggestedProviderName = useRef("");
  const updateProviderBaseURL = (value: string): void => {
    if (providerType !== "api") return;
    setProviderBaseURL(value);
    const suggested = suggestedProviderName(value);
    const current = providerName.trim();
    if (!current || current === lastSuggestedProviderName.current) {
      lastSuggestedProviderName.current = suggested;
      if (suggested) setProviderName(suggested);
    }
  };
  const updateProviderName = (value: string): void => {
    lastSuggestedProviderName.current = "";
    setProviderName(value);
  };
  const detectRelayType = async (): Promise<RelayType | undefined> => {
    const candidate = normalizeRelayOrigin(activeProviderBaseURL);
    if (!candidate) return undefined;
    setTypeDetection("checking");
    try {
      const detected = await relay.detectType(candidate);
      setTypeDetection(detected ?? "unknown");
      return detected;
    } catch {
      setTypeDetection("unknown");
      return undefined;
    }
  };
  // The relay family always comes from auto-detection: the bound station's
  // type first, then a live probe of the provider URL. No manual select.
  const resolveRelayType = async (): Promise<RelayType | undefined> => {
    if (typeDetection === "newapi" || typeDetection === "sub2api") return typeDetection;
    if (stationForProvider?.type) return stationForProvider.type;
    return detectRelayType();
  };
  const addManualModel = (): void => {
    const name = modelName.trim();
    const upstream = upstreamModel.trim();
    if (!name || !upstream) {
      // These two are one entry, not two required fields: the message names
      // the pair instead of a mark it cannot carry.
      setValidation(translate("providers.wizard.manualModelPairRequired"));
      return;
    }
    const existing = manualModels.find((model) => model.name === name && model.upstream_model === upstream);
    if (existing) {
      setSelectedManualModelIDs((current) => current.includes(existing.id) ? current : [...current, existing.id]);
      setModelName("");
      setUpstreamModel("");
      setValidation("");
      return;
    }
    const id = `manual-${++manualModelID.current}`;
    setManualModels((current) => [...current, { id, name, upstream_model: upstream }]);
    setSelectedManualModelIDs((current) => [...current, id]);
    setModelName("");
    setUpstreamModel("");
    setValidation("");
  };
  const removeManualModel = (id: string): void => {
    setManualModels((current) => current.filter((model) => model.id !== id));
    setSelectedManualModelIDs((current) => current.filter((modelID) => modelID !== id));
  };
  const fetchWizardModels = async (): Promise<void> => {
    if (keyPath !== "manual") return;
    if (modelFetchState === "loading") return;
    const keyChoice = selectedKeyChoice;
    const keyNameValue = manualSelectedKeyName;
    // A provider this wizard has not created yet is addressed by the wizard's
    // own token and the address its steps hold: the listing reads the staged
    // key and leaves no provider behind.
    const pendingAddress = serviceBaseURL.trim();
    const fetchIdentity = creatingProvider ? pendingKeyToken : providerID;
    if (!keyNameValue || (creatingProvider ? !pendingAddress : !providerID)) return;
    const request = ++modelFetchRequest.current;
    setModelFetchState("loading");
    setFetchedModelCapabilities({});
    const relaySource = !creatingProvider && keyChoice?.kind === "relay" ? keyChoice.source : undefined;
    const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";
    const payload = relaySource ? {
      provider_id: providerID,
      station_id: relaySource.stationID,
      account_id: relaySource.accountID,
      resource_id: relaySource.resourceID,
    } : creatingProvider ? {
      pending_provider: {
        pending_api_key: pendingKeyToken,
        name: providerName.trim(),
        api_base: pendingAddress,
        api_key_name: keyNameValue,
      },
    } : {
      provider_id: providerID,
      api_key_name: keyNameValue,
    };
    try {
      // Listing a provider's models is a read of the upstream (and, for a
      // WorkBuddy key, of the desktop app's own live catalog).  The step
      // reports it on 获取模型 alone, so the pane stays editable throughout.
      const next = await dispatchWithOutcome(action, payload, "providers_models", true);
      if (request !== modelFetchRequest.current) return;
      const summary = asRecord(asRecord(next?.action_summaries?.providers_models).operation_summary);
      const summaryProviderID = stringValue(summary.provider_id);
      const providerIdentity = selectedProvider ? identifier(selectedProvider) : "";
      if (stringValue(summary.operation) !== "fetch_models"
        || (summaryProviderID !== fetchIdentity && summaryProviderID !== providerIdentity)) {
        setModelFetchState("unavailable");
        return;
      }
      if (summary.available === false) {
        setModelFetchState("unavailable");
        return;
      }
      const candidates = [...new Set(stringList(summary.models).map((value) => value.trim()).filter(Boolean))];
      if (candidates.length === 0) {
        setModelFetchState("empty");
        return;
      }
      setFetchedModelCandidates(candidates);
      const capabilityRecords: Record<string, UnknownRecord> = {};
      for (const [modelID, value] of Object.entries(asRecord(summary.model_capabilities))) {
        const capabilities = modelRecordCapabilityChanges(value);
        if (Object.keys(capabilities).length > 0) capabilityRecords[modelID] = capabilities;
      }
      setFetchedModelCapabilities(capabilityRecords);
      const available = new Set([...(selectedProvidedChoice?.models ?? []), ...candidates]);
      setSelectedModels((current) => current.filter((model) => available.has(model)));
      setModelFetchState("ready");
    } catch {
      if (request === modelFetchRequest.current) {
        setModelFetchState("unavailable");
      }
    }
  };
  /**
   * The wizard's one create — 完成 is what mints the provider.
   *
   * Its own steps staged the name, the address, and (for a custom key) the
   * key value, so 下一步 never creates anything and a wizard the user closes
   * reserves nothing.  The key value is adopted here through the token the
   * wizard filed it under; the models are added by the caller right after.
   */
  const commitWizardProvider = async (): Promise<string | undefined> => {
    const name = providerName.trim();
    if (!name || !providerStepValid()) return undefined;
    if (isWorkBuddyType) {
      // The service entry is created empty: the desktop app's account and its
      // model roster are associated in the provider's 「服务商关联」 section.
      const next = await dispatchWithOutcome("service_provider.add", {
        kind: workbuddyAuthKind,
        name,
        models: [],
      });
      if (!next) return undefined;
      const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
      const added = snapshotProviderRecords(next).find((entry) => editorIdentifier(entry) === stringValue(summary.provider_id))
        ?? snapshotProviderRecords(next).find((entry) => stringValue(entry.display_name, stringValue(entry.name)).trim() === name);
      if (!added) return undefined;
      const createdServiceID = editorIdentifier(added);
      setProviderMode("existing");
      setProviderSelection(createdServiceID);
      return createdServiceID;
    }
    const baseURL = serviceBaseURL.trim();
    if (!baseURL) {
      setValidation(translate("providers.wizard.required"));
      return undefined;
    }
    const existingIDs = new Set(providers.map(editorIdentifier));
    const usingProvidedKey = keyPath === "login";
    const next = await dispatchWithOutcome("provider.add", {
      provider: {
        name,
        api_base: baseURL,
        auth_kind: "api_key",
        enabled: true,
        models: [],
        // A provided key is the provider's key: it arrives with the model's
        // own relay resource, so no empty independent slot is created.
        ...(usingProvidedKey ? {} : {
          create_default_api_key: true,
          initial_api_key_name: finishedProviderKeyName,
        }),
      },
      ...(usingProvidedKey ? {} : { pending_api_key: pendingKeyToken }),
    });
    if (!next) return undefined;
    const nextProviders = snapshotProviderRecords(next);
    const added = nextProviders.find((entry) => !existingIDs.has(editorIdentifier(entry)))
      ?? nextProviders.find((entry) => stringValue(entry.name).trim() === name);
    if (!added) return undefined;
    const createdID = editorIdentifier(added);
    // The wizard now edits the provider it just made: a 完成 that still has
    // work left (its models) continues on this entry instead of creating a
    // second one with the same name.
    setProviderMode("existing");
    setProviderSelection(createdID);
    // A provider that belongs to a relay station is bound to it now, while
    // its provided keys are what its models route through.
    if (usingProvidedKey) {
      const station = stationForProvider
        ?? relayStations.find((candidate) => candidate.id === (selectedProvidedChoice?.stationID ?? ""));
      if (station) await dispatchWithOutcome("provider.select_relay_station", { provider_id: createdID, station_id: station.id });
    }
    setValidation("");
    return createdID;
  };
  const createKey = async (): Promise<boolean> => {
    if (!providerID || !keyName.trim()) {
      setValidation(translate("providers.wizard.required"));
      return false;
    }
    const name = keyName.trim();
    setProcessing(true);
    try {
      const next = await dispatchWithOutcome("provider.key_add", { provider_id: providerID, name });
      if (!next) return false;
      const nextProvider = snapshotProviderRecords(next).find((entry) => editorIdentifier(entry) === providerID);
      const addedKey = nextProvider ? providerKeyStates(nextProvider).find((entry) => entry.name === name) : undefined;
      setKeySelection(addedKey?.id ?? PROVIDER_WIZARD_NEW_KEY);
      setKeyReady(false);
      setValidation("");
      return true;
    } finally {
      setProcessing(false);
    }
  };
  // Embedded station sign-in: the vendor's base URL is the station origin.
  // After the login the vendor binds to the station so its provided keys
  // become selectable.
  const beginRelayLogin = async (): Promise<void> => {
    // The button reports progress instead of going dead, so a second press
    // must not start a parallel sign-in.
    if (loginBusy) return;
    const request = ++setupLoginRequest.current;
    setLoginBusy(true);
    setLoginFeedbackMessage(translate("relay.loginWorking"));
    const candidate = normalizeRelayOrigin(activeProviderBaseURL);
    if (!candidate) {
      setLoginBusy(false);
      setValidation(translate("providers.wizard.required"));
      return;
    }
    try {
      const existingAccounts = relayAccounts.filter((item) => stationOriginKey(item.origin) === stationOriginKey(candidate));
      const reusable = existingAccounts.find((item) => item.loginStatus === "signed_in");
      if (reusable) {
        setSignedInAccountID(reusable.id);
        setLoginBusy(false);
        setLoginFeedbackMessage(undefined);
        return;
      }
      const accountType = await resolveRelayType();
      if (!accountType) {
        setLoginBusy(false);
        setLoginFeedbackMessage(translate("relay.typeNotDetected"));
        return;
      }
      // Pending login: Core creates the account shell only after sign-in
      // succeeds, so a cancelled flow reserves nothing and can no longer
      // cascade an empty station away from its bound provider.
      const pendingID = `login-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
      setSignedInAccountID(pendingID);
      setLoginPhase("sign-in");
      const result = await native.relayLogin({
        accountId: pendingID,
        type: accountType,
        label: (stationForProvider?.name ?? suggestedRelayStationName(candidate)) || candidate,
        origin: candidate,
        language: snapshot?.language ?? "system",
        pendingAccount: true,
        ...(stationForProvider ? { stationId: stationForProvider.id } : {}),
        stationName: (stationForProvider?.name ?? suggestedRelayStationName(candidate)) || candidate,
        stationType: accountType,
        stationOrigin: candidate,
        embedded: true,
      });
      const cancelled = request !== setupLoginRequest.current;
      if (cancelled) return;
      if (!result) {
        setLoginFeedbackMessage(translate("relay.loginNotCompleted"));
        if (signedInAccountID === pendingID) setSignedInAccountID(undefined);
        return;
      }
      const status = await relay.refreshResources(pendingID, { force: true });
      if (status !== "ready") setLoginFeedbackMessage(translate("relay.loginResourcesUnavailable"));
      else setLoginFeedbackMessage(translate("relay.loginComplete"));
      // Bind the vendor to the station so its keys become linkable.
      const next = await relay.refreshAccounts();
      const accounts = accountsFromSnapshot(next ?? undefined);
      const target = stationOriginKey(candidate);
      const boundAccount = accounts.find((item) => stationOriginKey(item.origin) === target);
      const stationID = boundAccount?.stationID
        ?? stationsFromSnapshot(next ?? undefined, accounts).find((station) => stationOriginKey(station.origin) === target)?.id;
      if (providerID && stationID) {
        await dispatchWithOutcome("provider.select_relay_station", { provider_id: providerID, station_id: stationID });
      }
      setSignedInAccountID(pendingID);
      setLoginPhase("idle");
    } catch {
      if (request === setupLoginRequest.current) setLoginFeedbackMessage(translate("relay.operationFailed"));
    } finally {
      if (request === setupLoginRequest.current) setLoginBusy(false);
    }
  };
  const cancelRelaySignIn = (): void => {
    if (loginPhase !== "sign-in") return;
    setupLoginRequest.current += 1;
    setLoginPhase("idle");
    setLoginBusy(false);
    setLoginFeedbackMessage(undefined);
    native.cancelRelayLogin();
  };
  const startOfficialLogin = async (): Promise<void> => {
    // An official service's account is the one record this wizard creates
    // before 完成: the sign-in needs the provider it belongs to, so pressing
    // 登录 is what mints it — never 下一步.
    let target = providerID;
    if (!target) {
      if (!providerStepValid()) return;
      setProcessing(true);
      try {
        target = await addOfficialAccount(
          providerType === "claude" ? "claude_login" : "openai_login",
          providerName.trim(),
        );
      } catch {
        setValidation(translate("relay.operationFailed"));
        return;
      } finally {
        setProcessing(false);
      }
      setProviderMode("existing");
      setProviderSelection(target);
      setValidation("");
    }
    const kind = providerType === "claude" ? "claude_login" : "openai_login";
    delete shownChallenge.current[target];
    const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: target }, "providers_models");
    presentAuthChallenge(next, kind, selectedProviderName || serviceProviderKindLabel(kind, translate), target);
  };
  const cancelOfficialLogin = async (): Promise<void> => {
    if (!providerID) return;
    await dispatchWithOutcome("service_provider.auth_cancel", { provider_id: providerID }, "providers_models");
  };
  const logoutOfficial = async (): Promise<void> => {
    if (!providerID) return;
    await dispatchWithOutcome("service_provider.auth_logout", { provider_id: providerID }, "providers_models");
  };
  const goNext = async (): Promise<void> => {
    // The primary button keeps reporting its own progress, so the guard that
    // used to come from `disabled` lives here.
    if (processing) return;
    setValidation("");
    if (step === "provider") {
      if (providerMode === "new") {
        // 下一步 stages the step's own fields; it never creates the provider.
        // 完成 is the one create (a WorkBuddy entry and a custom provider
        // both wait for it), and an official account is created by the press
        // that signs in to it.
        if (!providerStepValid()) return;
        setStep("keys");
        return;
      }
      if (!providerID) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      setStep("keys");
      return;
    }
    if (step === "keys") {
      if (isWorkBuddyType) {
        // Nothing is typed here: the desktop app owns the sign-in, and the
        // account is associated in the provider's 「服务商关联」 section.
        setStep("model");
        return;
      }
      if (creatingProvider) {
        // The provider does not exist yet, so this step states the field its
        // own create will carry: a custom key that is staged, or the provided
        // key this provider's models will route through.
        if (!usingProvidedKeyPath(keyPath) && (!keyName.trim() || !keyReady)) {
          setValidation(translate("providers.wizard.required"));
          return;
        }
        if (usingProvidedKeyPath(keyPath) && !selectedProvidedChoice) {
          setValidation(translate("providers.wizard.loginRequired"));
          return;
        }
        setStep("model");
        if (!usingProvidedKeyPath(keyPath) && modelCandidates.length === 0) void fetchWizardModels();
        return;
      }
      if (!selectedProvider) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      if (isOfficialLoginType && keyPath === "login") {
        if (officialStatus !== "signed_in") {
          setValidation(translate("providers.wizard.loginRequired"));
          return;
        }
        setStep("model");
        return;
      }
      if (keyPath === "login") {
        if (!selectedProvidedChoice) {
          setValidation(translate("providers.wizard.loginRequired"));
          return;
        }
        setStep("model");
        if (modelCandidates.length === 0) setModelFetchState("idle");
        return;
      }
      if (activeKeySelection === PROVIDER_WIZARD_NEW_KEY) {
        if (await createKey()) return;
        return;
      }
      if (!selectedKeyChoice || (!selectedKeyReady && selectedKeyChoice.kind === "independent")) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      if (selectedKeyChoice.kind === "independent") {
        const editedName = keyName.trim();
        if (!editedName) {
          setValidation(translate("providers.wizard.required"));
          return;
        }
        if (editedName !== selectedKeyChoice.name) {
          setProcessing(true);
          try {
            const renamed = await dispatchWithOutcome("provider.key_patch", { provider_id: providerID, old_name: selectedKeyChoice.name, name: editedName });
            if (!renamed) return;
          } finally {
            setProcessing(false);
          }
        }
      }
      setStep("model");
      if (modelCandidates.length === 0) void fetchWizardModels();
      return;
    }
    // Model step.
    if (isOfficialLoginType && keyPath === "login") {
      onStatus(translate("providers.wizard.complete"));
      onClose();
      return;
    }
    // WorkBuddy imported nothing here: its models arrive with the account in
    // the provider's own association section, and 完成 is what creates its
    // service entry.
    if (isWorkBuddyType) {
      setProcessing(true);
      try {
        const created = providerID || await commitWizardProvider();
        if (!created) return;
        onStatus(translate("providers.wizard.complete"));
        onClose();
      } finally {
        setProcessing(false);
      }
      return;
    }
    const draftModelName = modelName.trim();
    const draftUpstreamModel = upstreamModel.trim();
    if ((draftModelName && !draftUpstreamModel) || (!draftModelName && draftUpstreamModel)) {
      setValidation(translate("providers.wizard.required"));
      return;
    }
    const draftModel = draftModelName && draftUpstreamModel
      ? { name: draftModelName, upstream_model: draftUpstreamModel }
      : undefined;
    const usingProvidedKey = keyPath === "login";
    const requestedModels = usingProvidedKey ? [
      ...modelCandidates
        .filter((name) => selectedModels.includes(name))
        .map((name) => ({ name, upstream_model: name })),
    ] : [
      ...modelCandidates
        .filter((name) => selectedModels.includes(name))
        .map((name) => ({ name, upstream_model: name, ...modelRecordCapabilityChanges(fetchedModelCapabilities[name]) })),
      ...manualModels
        .filter((model) => selectedManualModelIDs.includes(model.id))
        .map(({ name, upstream_model }) => ({ name, upstream_model })),
      ...(draftModel ? [draftModel] : []),
    ];
    const uniqueRequestedModels = requestedModels.filter((model, index, all) => all.findIndex((candidate) => candidate.name === model.name && candidate.upstream_model === model.upstream_model) === index);
    if (uniqueRequestedModels.length === 0 || (!usingProvidedKey && !manualSelectedKeyName)) {
      setValidation(translate("providers.wizard.selectAtLeastOneModel"));
      return;
    }
    setProcessing(true);
    try {
      // The wizard's create lands here, with everything its steps staged: the
      // provider arrives with its key, and the models follow on it.
      const targetProviderID = providerID || await commitWizardProvider();
      if (!targetProviderID) return;
      const existingModelIDs = selectedProvider ? new Set(asRecords(selectedProvider.models).map(modelRecord).map(editorIdentifier)) : new Set<string>();
      const transientRelaySource = usingProvidedKey && selectedProvidedChoice ? {
        stationID: selectedProvidedChoice.stationID,
        accountID: selectedProvidedChoice.accountID,
        resourceID: selectedProvidedChoice.resourceID,
      } : undefined;
      const keyIDForModels = usingProvidedKey
        ? undefined
        : selectedKeyChoice?.id && selectedKeyChoice.kind === "independent" ? selectedKeyChoice.id : undefined;
      const modelPayload = uniqueRequestedModels.map((model, index) => ({
        ...model,
        api_key_name: usingProvidedKey ? selectedProvidedChoice?.resourceLabel ?? "" : creatingProvider ? finishedProviderKeyName : manualSelectedKeyName,
        ...(keyIDForModels ? { provider_key_id: keyIDForModels } : {}),
        enabled: true,
      }));
      const next = await dispatchWithOutcome("model.add_many", { provider_id: targetProviderID, models: modelPayload });
      if (!next) return;
      if (transientRelaySource) {
        const nextProvider = snapshotProviderRecords(next).find((entry) => editorIdentifier(entry) === targetProviderID);
        const addedModels = nextProvider ? asRecords(nextProvider.models).map(modelRecord) : [];
        for (const requested of uniqueRequestedModels) {
          const addedModel = addedModels.find((entry) => !existingModelIDs.has(editorIdentifier(entry)) && stringValue(entry.name).trim() === requested.name);
          if (!addedModel) return;
          const relayed = await dispatchWithOutcome("model.select_relay_resource", {
            provider_id: targetProviderID,
            model_id: editorIdentifier(addedModel),
            source: {
              kind: "relay",
              station_id: transientRelaySource.stationID,
              account_id: transientRelaySource.accountID,
              resource_id: transientRelaySource.resourceID,
            },
          });
          if (!relayed) return;
        }
      }
      onStatus(translate("providers.wizard.complete"));
      onClose();
    } finally {
      setProcessing(false);
    }
  };
  const goBack = (): void => {
    setValidation("");
    if (loginPhase === "sign-in") {
      cancelRelaySignIn();
      return;
    }
    if (step === "model") setStep("keys");
    else if (step === "keys") setStep("provider");
  };
  const providerPickerLabels = providerOptions.map((option) => option.label);
  const selectedProviderPickerLabel = providerOptions.find((option) => option.value === providerSelection)?.label ?? providerPickerLabels[0] ?? "";
  const keyPickerLabels = keyOptions.map((option) => option.label);
  const selectedKeyPickerLabel = keyOptions.find((option) => option.value === activeKeySelection)?.label ?? keyPickerLabels[0] ?? "";
  const wizardBusy = busy || processing || loginBusy;
  return <View style={styles.providerWizardSurface} accessibilityViewIsModal>
    <View style={styles.providerWizardSetupContent}>
      <View style={[styles.providerWizardSetupSurface, step === "model" && styles.providerWizardSetupSurfaceModel]}>
        <NativeWizardProgress steps={stepItems.map((item) => item.title)} activeIndex={stepItems.findIndex((item) => item.id === step)} />
        <View style={styles.providerWizardHeader}>
          <Text style={styles.providerWizardTitle}>{translate("providers.wizard.title")}</Text>
          <Text style={styles.providerWizardDescription}>{translate("providers.wizard.description")}</Text>
        </View>
        {loginPhase === "sign-in" ? <View style={styles.providerWizardSignInPanel}>
          <Text style={styles.providerWizardPanelTitle}>{translate("relay.stepSignIn")}</Text>
        </View> : <>
        {step === "provider" ? <View style={styles.providerWizardFormSection}>
          <NativeFormRow label={translate("providers.wizard.providerType")}>
            <NativePicker labels={servicePickerLabels} selectedValue={selectedServiceLabel} disabled={wizardBusy} onChange={({ nativeEvent }) => { const kinds: WizardType[] = ["api", "openai", "claude", "workbuddy", "workbuddyAI"]; chooseProviderType(kinds[nativeEvent.index] ?? "api"); }} style={styles.providerWizardPicker} />
          </NativeFormRow>
          {providerMode === "new" && isOfficialLoginType ? <NativeFormRow label={translate("providers.wizard.authPath")}>
            <NativePicker labels={[translate("providers.wizard.pathWebLogin"), translate("providers.wizard.pathCustomKey")]} selectedValue={keyPath === "login" ? translate("providers.wizard.pathWebLogin") : translate("providers.wizard.pathCustomKey")} disabled={wizardBusy} onChange={({ nativeEvent }) => { chooseOfficialAuthPath(nativeEvent.index === 1 ? "manual" : "login"); }} style={styles.providerWizardPicker} />
          </NativeFormRow> : null}
          {providerOptions.length > 0 ? <NativeFormRow label={translate("providers.wizard.sourceMode")}>
            <NativeSegmentedControl labels={[translate("providers.wizard.addProvider"), translate("providers.wizard.selectProvider")]} selectedValue={providerMode === "new" ? translate("providers.wizard.addProvider") : translate("providers.wizard.selectProvider")} disabled={wizardBusy} onChange={({ nativeEvent }) => chooseProviderMode(nativeEvent.index === 1 ? "existing" : "new")} style={styles.providerWizardModeControl} />
          </NativeFormRow> : null}
          {providerMode === "existing" ? <NativeFormRow required label={translate("providers.wizard.selectProvider")}><NativePicker labels={providerPickerLabels} selectedValue={selectedProviderPickerLabel} disabled={wizardBusy || providerPickerLabels.length === 0} onChange={({ nativeEvent }) => { const option = providerOptions[nativeEvent.index]; if (option) chooseExistingProvider(option.value); }} style={styles.providerWizardPicker} /></NativeFormRow> : null}
          {providerMode === "new" ? <>
            <NativeFormRow required label={translate("providers.wizard.baseUrl")}><NativeTextField value={serviceBaseURL} placeholder={translate("providers.wizard.baseUrlPlaceholder")} editable={providerType === "api" && !wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={updateProviderBaseURL} accessibilityLabel={translate("providers.wizard.baseUrl")} style={styles.providerWizardInput} /></NativeFormRow>
            <NativeFormRow required label={translate("providers.wizard.providerName")}><NativeTextField value={providerName} placeholder={translate("providers.wizard.providerNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={updateProviderName} accessibilityLabel={translate("providers.wizard.providerName")} style={styles.providerWizardInput} /></NativeFormRow>
          </> : null}
          {providerMode === "new" && providerType !== "api" ? <Text style={styles.providerWizardHint}>{translate("providers.wizard.serviceUrlFixed")}</Text> : null}
          {providerMode === "new" && isOfficialLoginType && keyPath === "login" ? <Text style={styles.providerWizardHint}>{providerType === "openai" ? translate("relay.officialProviderWebViewHint") : translate("relay.officialProviderBrowserHint")}</Text> : null}
          {providerMode === "new" && isWorkBuddyType ? <Text style={styles.providerWizardHint}>{translate("providers.wizard.workbuddyAppHint")}</Text> : null}
          {providerMode === "existing" && selectedProvider ? <Text numberOfLines={1} style={styles.providerWizardHint}>{activeProviderBaseURL || translate("common.notAvailable")}</Text> : null}
        </View> : null}
        {step === "keys" ? <View style={styles.providerWizardFormSection}>
          <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.stepKeys")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
          {isOfficialLoginType && keyPath === "login" ? <>
            <Text style={styles.providerWizardHint}>{translate("providers.wizard.loginKeyHint")}</Text>
            <View style={styles.providerWizardAuthRow}>
              <Text style={styles.providerWizardAuthStatus}>{officialStatusLabel(officialStatus, translate)}</Text>
              {officialStatus === "signed_in"
                ? <NativeButton title={translate("relay.officialProviderLogout")} compact disabled={wizardBusy} onPress={() => { void logoutOfficial(); }} />
                : officialStatus === "authorizing"
                  ? <NativeButton title={translate("relay.officialProviderCancel")} compact disabled={wizardBusy} onPress={() => { void cancelOfficialLogin(); }} />
                  : <NativeButton title={translate("relay.officialProviderLogin")} primary compact disabled={wizardBusy} onPress={() => { void startOfficialLogin(); }} />}
            </View>
          </> : isWorkBuddyType ? <>
            {/* The account is associated in the provider's own 「服务商关联」
                section, so this step only states the desktop app's sign-in and
                never asks for a WorkBuddy secret. */}
            <Text style={styles.providerWizardHint}>{translate("providers.wizard.workbuddyKeyHint")}</Text>
            <View style={styles.providerWizardAuthRow}>
              <Text style={styles.providerWizardAuthStatus}>{stringValue(workbuddyAccount?.state) === "signed-in" ? translate("providers.authStatusSignedIn") : progressText(workbuddyBusy, translate) ?? translate("providers.authStatusSignedOut")}</Text>
              <NativeButton title={translate("providers.wizard.workbuddyRefresh")} compact busy={workbuddyBusy} disabled={busy && !workbuddyBusy} onPress={() => { void loadWorkBuddyAccount(true); }} />
            </View>
            {stringValue(workbuddyAccount?.state) === "signed-in" ? <>
              <Text numberOfLines={1} style={styles.providerWizardHint}>{translate("providers.wizard.workbuddyAccount")}: {stringValue(workbuddyAccount?.nickname) || translate("common.notAvailable")} · {stringValue(workbuddyAccount?.domain)}</Text>
              {workbuddyCreditsText(workbuddyAccount, translate) ? <Text numberOfLines={1} style={styles.providerWizardHint}>{translate("providers.wizard.workbuddyCredits")}: {workbuddyCreditsText(workbuddyAccount, translate)}</Text> : null}
            </> : <Text style={styles.providerWizardHint}>{translate("providers.wizard.workbuddySignInRequired")}</Text>}
            <Text style={styles.providerWizardHint}>{translate("providers.wizard.workbuddyAssociationHint")}</Text>
          </> : <>
            {providerType === "api" ? <NativeFormRow label={translate("providers.wizard.keyPath")}>
              <NativeSegmentedControl labels={[translate("providers.wizard.pathManual"), translate("providers.wizard.pathLogin")]} selectedValue={keyPath === "login" ? translate("providers.wizard.pathLogin") : translate("providers.wizard.pathManual")} disabled={wizardBusy} onChange={({ nativeEvent }) => { setKeyPath(nativeEvent.index === 1 ? "login" : "manual"); setValidation(""); }} style={styles.providerWizardModeControl} />
            </NativeFormRow> : null}
            {keyPath === "login" ? <>
              {providedChoices.length > 0 ? <>
                <Text style={styles.providerWizardHint}>{translate("providers.wizard.providedKeysHint", { count: providedChoices.length })}</Text>
                <View style={styles.providerWizardModelList}>
                  {providedChoices.map((source) => {
                    const value = `relay:${relaySourceSelectionID(source)}`;
                    return <NativeCheckbox
                      key={value}
                      label={relaySourceName(source) || source.resourceLabel}
                      value={(selectedProvidedChoice && `relay:${relaySourceSelectionID(selectedProvidedChoice)}`) === value}
                      disabled={wizardBusy}
                      onValueChange={() => setProvidedKeySelection(value)}
                      style={styles.providerWizardModelCheckbox}
                    />;
                  })}
                </View>
              </> : <>
                <Text style={styles.providerWizardHint}>{signedInAccountID ? translate("relay.resourcesNotLoaded") : translate("providers.wizard.loginFirstHint")}</Text>
                <NativeButton title={translate("relay.login")} primary compact busy={loginBusy} disabled={(wizardBusy && !loginBusy) || !activeProviderBaseURL.trim()} onPress={() => { void beginRelayLogin(); }} />
              </>}
            </> : <>
              <NativeFormRow required label={translate("providers.wizard.selectApiKey")}><NativePicker labels={keyPickerLabels} selectedValue={selectedKeyPickerLabel} disabled={wizardBusy} onChange={({ nativeEvent }) => { const option = keyOptions[nativeEvent.index]; if (!option) return; setKeySelection(option.value); setKeyReady(false); setKeyName(""); setValidation(""); }} style={styles.providerWizardPicker} /></NativeFormRow>
              {activeKeySelection === PROVIDER_WIZARD_NEW_KEY ? <>
                <NativeFormRow required label={translate("providers.wizard.apiKeyName")}><NativeTextField value={keyName} placeholder={translate("providers.wizard.apiKeyNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={setKeyName} accessibilityLabel={translate("providers.wizard.apiKeyName")} style={styles.providerWizardInput} /></NativeFormRow>
                {creatingProvider ? (keyReady
                  ? <Text style={styles.providerWizardHint}>{finishedProviderKeyName}</Text>
                  : <NativeFormRow required label={translate("providers.wizard.apiKeyValue")}><NativeSecureTextInput label={translate("providers.wizard.apiKeyValue")} domain="providers_models" field="api_key" target={stagedKeyTarget} plainText autoCommit disabled={wizardBusy} onSecretState={(state) => { setKeyReady(state.present); onSecretState(state); }} style={styles.providerWizardSecretInput} /></NativeFormRow>) : null}
              </>
                : selectedKeyChoice?.kind === "independent" ? <>
                    <NativeFormRow required label={translate("providers.wizard.apiKeyName")}><NativeTextField value={keyName} placeholder={translate("providers.wizard.apiKeyNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={setKeyName} accessibilityLabel={translate("providers.wizard.apiKeyName")} style={styles.providerWizardInput} /></NativeFormRow>
                    {selectedKeyReady ? <Text style={styles.providerWizardHint}>{selectedKeyChoice.name}</Text>
                      : <NativeFormRow required label={translate("providers.wizard.apiKeyValue")}><NativeSecureTextInput label={translate("providers.wizard.apiKeyValue")} domain="providers_models" field="api_key" target={`${providerID}\u001f${selectedKeyChoice.name}`} plainText autoCommit disabled={wizardBusy} onSecretState={(state) => { setKeyReady(state.present); onSecretState(state); }} style={styles.providerWizardSecretInput} /></NativeFormRow>}
                  </> : <Text style={styles.providerWizardHint}>{translate("providers.wizard.selectApiKey")}</Text>}
            </>}
          </>}
        </View> : null}
        {step === "model" ? isOfficialLoginType && keyPath === "login" ? <View style={styles.providerWizardFormSection}>
          <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.models")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
          <Text style={styles.providerWizardHint}>{translate("providers.wizard.officialModelsHint")}</Text>
          <View style={styles.providerWizardModelList}>
            {modelChoicesForOfficial.map((model) => {
              const modelID = editorIdentifier(model);
              return <View key={modelID} style={styles.providerWizardManualModelRow}>
                <Text numberOfLines={1} style={styles.providerWizardManualModelUpstream}>{stringValue(model.display_name, stringValue(model.name, modelID))}</Text>
                {stringValue(model.upstream_model) ? <Text numberOfLines={1} style={styles.providerWizardManualModelUpstream}>{stringValue(model.upstream_model)}</Text> : null}
              </View>;
            })}
            {modelChoicesForOfficial.length === 0 ? <Text style={styles.providerWizardHint}>{translate("common.loading")}</Text> : null}
          </View>
        </View> : isWorkBuddyType ? <View style={styles.providerWizardFormSection}>
          <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.models")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
          <Text style={styles.providerWizardHint}>{translate("providers.wizard.workbuddyAssociationHint")}</Text>
        </View> : <PersistentScrollView style={styles.providerWizardModelScroll} contentContainerStyle={styles.providerWizardModelScrollContent} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">
          <View style={styles.providerWizardFormSection}>
            <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.models")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
            {!usingProvidedKeyPath(keyPath) ? <View style={styles.providerWizardModelToolbar}>
              <Text numberOfLines={2} style={styles.providerWizardHint}>{modelFetchState === "loading" ? translate("providers.wizard.fetchingModels") : modelCandidates.length > 0 ? translate("providers.wizard.modelsFound", { count: modelCandidates.length }) : modelFetchState === "unavailable" ? translate("providers.wizard.modelsUnavailable") : modelFetchState === "empty" ? translate("providers.wizard.modelsEmpty") : translate("providers.wizard.noModels")}</Text>
              <NativeButton title={translate("providers.wizard.refreshModels")} compact link busy={modelFetchState === "loading"} disabled={(wizardBusy && modelFetchState !== "loading") || !manualSelectedKeyName || (creatingProvider ? !serviceBaseURL.trim() : !providerID)} onPress={() => { void fetchWizardModels(); }} />
            </View> : <View style={styles.providerWizardModelToolbar}>
              <Text numberOfLines={2} style={styles.providerWizardHint}>{modelCandidates.length > 0 ? translate("providers.wizard.modelsFound", { count: modelCandidates.length }) : translate("relay.resourcesNoModels")}</Text>
            </View>}
            {modelCandidates.length > 0 ? <View style={styles.providerWizardModelGroup}>
              <View style={styles.providerWizardModelGroupHeader}><Text style={styles.providerWizardPanelTitle}>{translate(usingProvidedKeyPath(keyPath) ? "providers.wizard.providedModels" : "providers.wizard.discoveredModels")}</Text><Text style={styles.providerWizardHint}>{translate("providers.wizard.selectedModels", { count: selectedModels.length })}</Text></View>
              <View style={styles.providerWizardModelList}>
                {modelCandidates.map((name) => <NativeCheckbox key={`discovered:${name}`} label={name} value={selectedModels.includes(name)} disabled={wizardBusy} onValueChange={(checked) => { setSelectedModels((current) => checked ? (current.includes(name) ? current : [...current, name]) : current.filter((item) => item !== name)); setValidation(""); }} style={styles.providerWizardModelCheckbox} />)}
              </View>
            </View> : null}
            {!usingProvidedKeyPath(keyPath) ? <View style={styles.providerWizardModelGroup}>
              <Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.manualModels")}</Text>
              {manualModels.map((model) => <View key={model.id} style={styles.providerWizardManualModelRow}>
                <NativeCheckbox label={model.name} value={selectedManualModelIDs.includes(model.id)} disabled={wizardBusy} onValueChange={(checked) => { setSelectedManualModelIDs((current) => checked ? (current.includes(model.id) ? current : [...current, model.id]) : current.filter((item) => item !== model.id)); setValidation(""); }} style={styles.providerWizardManualModelCheckbox} />
                <Text numberOfLines={1} style={styles.providerWizardManualModelUpstream}>{model.upstream_model}</Text>
                <NativeButton title={translate("providers.wizard.removeManualModel")} compact link disabled={wizardBusy} onPress={() => removeManualModel(model.id)} />
              </View>)}
              <NativeFormRow label={translate("providers.wizard.modelName")}><NativeTextField value={modelName} placeholder={translate("providers.wizard.modelNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={setModelName} accessibilityLabel={translate("providers.wizard.modelName")} style={styles.providerWizardInput} /></NativeFormRow>
              <NativeFormRow label={translate("providers.wizard.upstreamModel")}><NativeTextField value={upstreamModel} placeholder={translate("providers.wizard.upstreamModelPlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={setUpstreamModel} accessibilityLabel={translate("providers.wizard.upstreamModel")} style={styles.providerWizardInput} /></NativeFormRow>
              <NativeButton title={translate("providers.wizard.addManualModel")} compact link disabled={wizardBusy} onPress={addManualModel} />
            </View> : null}
            <Text style={styles.providerWizardModelSummary}>{translate("providers.wizard.modelsToAdd", { count: usingProvidedKeyPath(keyPath) ? selectedModels.length : selectedModels.length + selectedManualModelIDs.length + Number(Boolean(modelName.trim() && upstreamModel.trim())) })}</Text>
          </View>
        </PersistentScrollView> : null}
        </>}
      </View>
    </View>
    <View style={styles.providerWizardFooter}>
      {/* The wizard's one status line: its own validation or feedback, and the
          wait while the embedded sign-in page is up.  The step body never
          repeats it. */}
      {wizardFooterStatus ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.providerWizardFooterStatus}>{wizardFooterStatus}</Text> : <View style={styles.providerWizardFooterSpacer} />}
      <View style={styles.providerWizardFooterActions}>
        <NativeButton title={translate("status.close")} disabled={processing} onPress={onClose} />
        {step !== "provider" || loginPhase === "sign-in" ? <NativeButton title={translate("providers.wizard.back")} disabled={busy || processing} onPress={goBack} /> : null}
        {loginPhase === "sign-in"
          ? null
          : <NativeButton primary title={step === "model" ? translate("providers.wizard.finish") : translate("providers.wizard.next")} busy={processing} disabled={wizardBusy && !processing} onPress={() => { void goNext(); }} />}
      </View>
    </View>
  </View>;
}

function usingProvidedKeyPath(keyPath: "login" | "manual"): boolean {
  return keyPath === "login";
}

function presentProviderAuthChallenge(native: NativeLeafAdapter, translate: Translate, next: CoreSnapshot | undefined, kind: ServiceProviderKind, label: string, accountFingerprint: string, shownChallenge: Record<string, string>): void {
  const summary = asRecord(asRecord(next?.action_summaries?.providers_models).operation_summary);
  const verificationURL = stringValue(summary.verification_uri);
  const userCode = stringValue(summary.user_code);
  const callbackURL = stringValue(summary.redirect_uri);
  if (!verificationURL || (!userCode && !callbackURL)) return;
  const fingerprint = `${accountFingerprint}|${kind}|${verificationURL}|${userCode}|${callbackURL}`;
  if (shownChallenge[accountFingerprint] === fingerprint) return;
  shownChallenge[accountFingerprint] = fingerprint;
  const options = {
    title: label + " " + translate("relay.officialProviderLogin"),
    closeLabel: translate("status.close"),
  };
  if (native.showProviderAuth) {
    void native.showProviderAuth({
      provider: kind === "openai_login" ? "openai" : "claude",
      fingerprint: accountFingerprint,
      verificationURL,
      ...(userCode ? { userCode } : {}),
      ...(callbackURL ? { callbackURL } : {}),
      ...options,
    }).catch(() => undefined);
  } else {
    void native.showReadOnlyText({
      ...options,
      text: [verificationURL, userCode || callbackURL].join("\n"),
      language: "text",
      html: readOnlyCodeEditorHtml(editorMenuLabels(translate)),
    });
  }
}

function ProviderWorkspace({ snapshot, ipc, onSnapshot, native, busy, translate, dispatch, dispatchWithOutcome, onStatus, onSecretState, applyProbedSurface, onOpenWizard, relay, addOfficialAccount, onActivateAndRestart }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>; onStatus: (status?: string) => void; onSecretState: (state: SecretState) => void; applyProbedSurface: ApplyProbedSurface; onOpenWizard: () => void; relay: RelayWorkspaceBridge; addOfficialAccount: (kind: ServiceProviderKind) => Promise<string>; onActivateAndRestart: () => Promise<boolean> }): React.JSX.Element {
  const state = domainState(snapshot, "providers_models");
  const relaySources = useMemo(() => relaySourcesFromSnapshot(snapshot), [snapshot]);
  const relayStations = useMemo(() => relayStationsFromSnapshot(snapshot), [snapshot]);
  const relayAccounts = useMemo(() => accountsFromSnapshot(snapshot), [snapshot]);
  const relayStationsFull = useMemo(() => stationsFromSnapshot(snapshot, relayAccounts), [relayAccounts, snapshot]);
  const providers = useMemo(() => snapshotProviderRecords(snapshot), [snapshot?.providers_models.providers, state.providers]);
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const [providerNameDrafts, setProviderNameDrafts] = useState<Record<string, string>>({});
  const [providerBaseUrlDrafts, setProviderBaseUrlDrafts] = useState<Record<string, string>>({});
  const [modelNameDrafts, setModelNameDrafts] = useState<Record<string, string>>({});
  const [modelUpstreamDrafts, setModelUpstreamDrafts] = useState<Record<string, string>>({});
  const [modelOrderDrafts, setModelOrderDrafts] = useState<Record<string, string>>({});
  const setProviderNameDraft = useCallback((providerID: string, value: string): void => {
    setProviderNameDrafts((current) => current[providerID] === value ? current : { ...current, [providerID]: value });
  }, []);
  const setProviderBaseUrlDraft = useCallback((providerID: string, value: string): void => {
    setProviderBaseUrlDrafts((current) => current[providerID] === value ? current : { ...current, [providerID]: value });
  }, []);
  const setModelNameDraft = useCallback((providerID: string, modelID: string, value: string): void => {
    const key = providerModelDraftKey(providerID, modelID);
    setModelNameDrafts((current) => current[key] === value ? current : { ...current, [key]: value });
  }, []);
  const setModelUpstreamDraft = useCallback((providerID: string, modelID: string, value: string): void => {
    const key = providerModelDraftKey(providerID, modelID);
    setModelUpstreamDrafts((current) => current[key] === value ? current : { ...current, [key]: value });
  }, []);
  const setModelOrderDraft = useCallback((providerID: string, modelID: string, value: string): void => {
    const key = providerModelDraftKey(providerID, modelID);
    setModelOrderDrafts((current) => current[key] === value ? current : { ...current, [key]: value });
  }, []);
  const providerBaseURL = useCallback((entry: UnknownRecord): string => {
    const entryID = editorIdentifier(entry);
    return providerBaseUrlDrafts[entryID] !== undefined
      ? providerBaseUrlDrafts[entryID]
      : stringValue(entry.endpoint, stringValue(entry.api_base));
  }, [providerBaseUrlDrafts]);
  // A custom provider whose base URL already targets a relay station is
  // rebound to that station automatically (`station.example.test` and
  // `www.station.example.test` stay distinct sites).  Core treats a rebind that leaves
  // the visible name and URL untouched as cosmetic, so the draft stays clean
  // and closing the window never asks to discard a change that altered
  // nothing visible.
  const autoRelaySelectionKeys = useRef(new Set<string>());
  useEffect(() => {
    const activeKeys = new Set<string>();
    if (!busy) {
      for (const entry of providers) {
        if (providerKind(entry) !== "apiKey") continue;
        const providerID = editorIdentifier(entry);
        const baseURL = stringValue(entry.endpoint, stringValue(entry.api_base)).trim();
        const station = relayStationForBaseUrl(baseURL, relayStations);
        if (!station) continue;
        if (providerNameExists(providers, station.name, providerID)) continue;
        const selectionKey = `${providerID}\x1f${station.id}\x1f${stationOriginKey(baseURL)}`;
        activeKeys.add(selectionKey);
        if (autoRelaySelectionKeys.current.has(selectionKey)) continue;
        autoRelaySelectionKeys.current.add(selectionKey);
        void dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id });
      }
    }
    for (const selectionKey of autoRelaySelectionKeys.current) {
      if (!activeKeys.has(selectionKey)) autoRelaySelectionKeys.current.delete(selectionKey);
    }
  }, [busy, dispatch, providers, relayStations]);
  const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);
  // A route ＋ created is selected once the routes list carries it.  The key is
  // not known until the snapshot arrives a render later, and setting a key the
  // table does not know would be repaired to the first route — the pane would
  // then edit the wrong row.
  const pendingRouteKey = useRef<string | undefined>(undefined);
  // The list owns the selection: an explicitly cleared one (a click below the
  // rows) leaves the panes empty instead of acting on a provider the list no
  // longer highlights, while the first provider still opens the pane on load.
  const provider = useMemo(
    () => providers.find((item) => editorIdentifier(item) === selectedProvider)
      ?? (selectedProvider === undefined ? providers[0] : undefined),
    [providers, selectedProvider],
  );
  const providerId = provider ? editorIdentifier(provider) : "";
  const providerKindSelected = provider ? providerKind(provider) : "apiKey" as ProviderKind;
  const models = useMemo(
    () => provider ? asRecords(provider.models).map(modelRecord) : [],
    [provider],
  );
  const [selectedModel, setSelectedModel] = useState<string>();
  const [providerSourceModel, setProviderSourceModel] = useState<string>();
  // Adding a provider from the list header selects it as soon as the snapshot
  // carries it, so the editor below opens on the provider that was just made.
  const pendingProviderIds = useRef<Set<string> | undefined>(undefined);
  const model = useMemo(
    () => models.find((item) => editorIdentifier(item) === selectedModel),
    [models, selectedModel],
  );
  const [viewMode, setViewMode] = useState<"providers" | "routes">("providers");
  const [selectedRoute, setSelectedRoute] = useState<string>();
  // The routes view selects either one route (its model detail) or one public
  // model's group (its own settings surface).
  const [selectedPublicModel, setSelectedPublicModel] = useState<string>();
  // Where 设置 came from: the route detail the public-model pane can return to.
  const [publicModelReturn, setPublicModelReturn] = useState<{ routeKey: string; label: string }>();
  const [fetchKeyID, setFetchKeyID] = useState<string>();
  const [fetchModelsBusy, setFetchModelsBusy] = useState(false);
  const probingModelKeys = useRef(new Set<string>());
  const [, setProbeActivityRevision] = useState(0);
  // A result is kept with the inputs it was measured on: a route the user has
  // since edited never shows a verdict about the route it replaced.
  const [probeResults, setProbeResults] = useState<Record<string, { inputs: string; result: IpcResults["probe"] }>>({});
  // The routes whose finding was taken off screen and has not been answered
  // again.  A press asks the route a new question, and a route the user edits
  // is no longer the route the finding describes; either way the pane shows
  // nothing until this pane measures the route it now has.
  const [droppedProbeResults, setDroppedProbeResults] = useState<Record<string, true>>({});
  const shownChallenge = useRef<Record<string, string>>({});
  const fetchKeyChoices = useMemo(
    () => provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? providerKeyChoices(provider, relaySources, providerBaseURL(provider)) : [],
    [provider, providerBaseURL, providerKindSelected, relaySources],
  );
  const fetchKeyOptions = useMemo(
    () => fetchKeyChoices.map((choice) => ({
      value: choice.id,
      label: providerKeyChoiceLabel({ ...choice, name: choice.name }, translate),
    })),
    [fetchKeyChoices, translate],
  );
  const selectedFetchKey = fetchKeyID ?? fetchKeyChoices[0]?.id ?? "";
  const selectedService = provider ? providerService(provider) : undefined;
  useEffect(() => {
    // Read the service's catalog as soon as its provider is selected, so every
    // model switch paints its rate from the cache instead of asking mid-switch.
    if (!selectedService || hasFreshServiceRates(selectedService)) return;
    void serviceModelRate(selectedService, (providerId) => dispatchWithOutcome("workbuddy_models", { provider: providerId }, "providers_models", true))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedService]);
  async function probeModel(targetProviderId: string, targetModelId: string, inputs: string, options?: { confirmRecommendation?: boolean }): Promise<void> {
    const key = modelProbeKey(targetProviderId, targetModelId);
    if (probingModelKeys.current.has(key)) return;
    probingModelKeys.current.add(key);
    // Asking again takes the previous finding away: it answered the question
    // this press replaced.  Both copies go — the one this pane measured and
    // the one Core kept for the route.
    setProbeResults((current) => withoutRecordEntry(current, key));
    setDroppedProbeResults((current) => ({ ...current, [key]: true }));
    setProbeActivityRevision((value) => value + 1);
    let result: IpcResults["probe"];
    try {
      result = await ipc.probe(targetProviderId, targetModelId, "providers_models");
    } catch (reason: unknown) {
      result = { ok: false, protocols: [], detail: errorMessage(reason, translate), provider_id: targetProviderId, model_id: targetModelId };
    }
    // The probe is over the moment its answer is in hand: the button reports
    // this question's progress, so it stands down here instead of staying
    // disabled through the write the answer goes on to ask for.
    probingModelKeys.current.delete(key);
    setProbeActivityRevision((value) => value + 1);
    setProbeResults((current) => ({ ...current, [key]: { inputs, result } }));
    setDroppedProbeResults((current) => withoutRecordEntry(current, key));
    try {
      onSnapshot(await ipc.snapshot());
      const nextSurface = stringValue(result.recommended_surface);
      if (result.ok && isProbeSurface(nextSurface)) {
        const applied = await applyProbedSurface(targetProviderId, targetModelId, nextSurface, options);
        if (applied) {
          // The recommended surface is the probe's own write, not an edit the
          // user made.  Re-key the finding on the route that write produced,
          // or the press that just measured it would hide its own answer (a
          // new route moves from ``fallback`` to the recommended surface, so
          // the stored inputs no longer matched).
          const refreshed = await ipc.snapshot();
          const providerRecord = snapshotProviderRecords(refreshed).find(
            (entry) => editorIdentifier(entry) === targetProviderId,
          );
          const modelRecord = providerModelByEditorId(refreshed, targetProviderId, targetModelId);
          if (providerRecord && modelRecord) {
            const nextInputs = probeInputFingerprint(
              providerBaseURL(providerRecord),
              modelUpstreamDisplay(targetProviderId, modelRecord),
              modelRecord,
            );
            setProbeResults((current) => current[key] ? { ...current, [key]: { inputs: nextInputs, result } } : current);
          }
        }
      }
    } catch (reason: unknown) {
      onStatus(errorMessage(reason, translate));
    }
  }
  const modelProbeProps = (targetProviderId: string, targetModelId: string, inputs: string): { probing: boolean; probeResult?: IpcResults["probe"] | null; probe: () => void } => {
    const key = modelProbeKey(targetProviderId, targetModelId);
    const record = probeResults[key];
    // The pane's own copy decides what it shows.  No copy leaves the finding
    // Core kept for the route on screen; a copy measured on other inputs, or
    // one a press took down, leaves nothing — the route moved on, and Core's
    // copy describes the route it left behind.
    const measuredHere = record !== undefined && record.inputs === inputs;
    return {
      probing: probingModelKeys.current.has(key),
      probeResult: droppedProbeResults[key] === true || (record !== undefined && !measuredHere)
        ? null
        : measuredHere ? record.result : undefined,
      probe: () => probeModel(targetProviderId, targetModelId, inputs),
    };
  };
  // Poll official-account authorizations so a login that starts here (or in
  // the wizard window) still completes its device-code challenge.
  useEffect(() => {
    const authorizing = providers.filter((entry) => providerAuthStatus(entry) === "authorizing");
    if (authorizing.length === 0) return;
    const timer = setInterval(() => {
      for (const entry of authorizing) {
        const kind = providerAuthKind(entry);
        if (kind !== "openai_login" && kind !== "claude_login") continue;
        const providerID = editorIdentifier(entry);
        const label = stringValue(entry.display_name, stringValue(entry.name, serviceProviderKindLabel(kind, translate)));
        void dispatchWithOutcome("service_provider.auth_status", { provider_id: providerID }, "providers_models", true)
          .then((next) => presentProviderAuthChallenge(native, translate, next, kind, label, providerID, shownChallenge.current))
          .catch(() => undefined);
      }
    }, 1_000);
    return () => clearInterval(timer);
  }, [dispatchWithOutcome, native, providers, translate]);
  useEffect(() => {
    if (providers.length === 0) {
      if (selectedProvider !== undefined) setSelectedProvider(undefined);
      return;
    }
    // A cleared selection stays cleared: the provider list, the + / − headers,
    // and the editor all read the one selection the user can see.
    if (selectedProvider === "") return;
    if (!providers.some((item) => editorIdentifier(item) === selectedProvider)) {
      setSelectedProvider(editorIdentifier(providers[0]));
    }
  }, [providers, selectedProvider]);
  useEffect(() => {
    const pending = pendingModelIds.current;
    if (pending?.providerId === providerId) {
      const added = models.find((item) => !pending.ids.has(editorIdentifier(item)));
      if (added) {
        const addedId = editorIdentifier(added);
        setSelectedModel(addedId);
        setProviderSourceModel(undefined);
        pendingModelIds.current = undefined;
        return;
      }
    }
    if (selectedModel !== undefined && !models.some((item) => editorIdentifier(item) === selectedModel)) {
      setSelectedModel(undefined);
    }
  }, [models, providerId, selectedModel]);
  useEffect(() => {
    const pending = pendingProviderIds.current;
    if (!pending) return;
    const added = providers.find((item) => !pending.has(editorIdentifier(item)));
    if (!added) return;
    pendingProviderIds.current = undefined;
    setSelectedProvider(editorIdentifier(added));
  }, [providers]);
  useEffect(() => {
    if (!fetchKeyChoices.some((choice) => choice.id === fetchKeyID)) setFetchKeyID(fetchKeyChoices[0]?.id);
  }, [fetchKeyChoices, fetchKeyID, providerId]);
  const handleFetchedModels = (summary: UnknownRecord): void => {
    const summaryProviderId = stringValue(summary.provider_id);
    const providerIdentity = provider ? identifier(provider) : "";
    if (stringValue(summary.operation) !== "fetch_models" || (summaryProviderId !== providerId && summaryProviderId !== providerIdentity)) return;
    const candidates = stringList(summary.models);
    if (summary.available === false) {
      onStatus(translate("providers.fetchFailed", { detail: stringValue(summary.detail, translate("common.notAvailable")) }));
      return;
    }
    if (candidates.length === 0) {
      onStatus(translate("providers.fetchEmpty"));
      return;
    }
    onStatus(undefined);
    const candidateSet = new Set(candidates);
    const modelCapabilities = asRecord(summary.model_capabilities);
    const providerName = provider ? providerDisplayName(provider) : providerId;
    const apiKeyName = stringValue(summary.api_key_name);
    if (!apiKeyName) {
      onStatus(translate("providers.fetchFailed", { detail: translate("common.notAvailable") }));
      return;
    }
    // A relay fetch stages the key slot it listed the models for, and the
    // answer names it by id.  The rows are filed on that slot, not on its
    // label: a key is its own slot, and the name a fetch happened to carry is
    // the weaker identity ("one key is one slot, never one name").
    const apiKeyID = stringValue(summary.slot_id);
    const keyName = fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? apiKeyDisplayName(apiKeyName, translate);
    void native.chooseModelsToAdd({ models: candidates, providerName, keyName }).then((selection) => {
      const selectedModels = (selection ?? []).filter((model, index, all) => candidateSet.has(model) && all.indexOf(model) === index);
      if (selectedModels.length === 0) return;
      void dispatch("model.add_many", {
        provider_id: providerId,
        models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, api_key_name: apiKeyName, ...(apiKeyID ? { provider_key_id: apiKeyID } : {}), enabled: true, order: 0 })).map((model) => ({
          ...model,
          ...modelRecordCapabilityChanges(modelCapabilities[model.upstream_model]),
        })),
      });
    }).catch(() => undefined);
  };
  const fetchModels = async (): Promise<void> => {
    // The fetch button reports its own progress instead of graying out, so a
    // second press must not run a parallel fetch.
    if (fetchModelsBusy) return;
    const choice = fetchKeyChoices.find((item) => item.id === selectedFetchKey);
    if (!provider || !choice) return;
    const relaySource = choice.kind === "relay" ? choice.source : undefined;
    const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";
    const payload = relaySource ? {
      provider_id: providerId,
      station_id: relaySource.stationID,
      account_id: relaySource.accountID,
      resource_id: relaySource.resourceID,
    } : {
      provider_id: providerId,
      api_key_name: choice.name,
    };
    setFetchModelsBusy(true);
    try {
      // 获取模型 reports its own progress (a relay round trip, or the desktop
      // app's live catalog), so the rest of the pane stays usable while it
      // waits.  The row list it stages still applies as a normal write.
      const next = await dispatchWithOutcome(action, payload, "providers_models", true);
      if (!next) {
        onStatus(translate("providers.fetchFailed", { detail: translate("common.notAvailable") }));
        return;
      }
      const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
      if (Object.keys(summary).length === 0) {
        onStatus(translate("providers.fetchFailed", { detail: translate("common.notAvailable") }));
        return;
      }
      const slotID = stringValue(summary.slot_id);
      if (slotID) setFetchKeyID(slotID);
      handleFetchedModels(summary);
    } finally {
      setFetchModelsBusy(false);
    }
  };
  const addModel = (): void => {
    if (!provider) return;
    const knownModelIds = new Set(models.map(editorIdentifier));
    pendingModelIds.current = { providerId, ids: knownModelIds };
    // A new model starts **enabled**: the user asked for rows to be live
    // without a second press, and the placeholder name (carried as its own
    // upstream route) keeps the document valid while the user fills it in —
    // pressing ＋ never raises a validation failure.  The pane marks the row
    // (`modelNeedsAttention`) and its inspector states what is left, so a
    // placeholder name is never mistaken for a finished route.
    const base = translate("providers.newModel");
    const name = uniquePlaceholderName(models.map((item) => stringValue(item.model_name ?? item.name)), base);
    // The row inherits the key the user is working in — the selected model's
    // key, else the key the pane's own key picker points at, else the
    // provider's first key — so ＋ files it in a real key group instead of one
    // the app invents.  The picker is how a key that owns no model yet is
    // addressed, so ＋ beside a key the user just chose in it must land on that
    // key rather than on whichever key happens to be first.  A provider with no
    // key yet gets the keyless draft row, and its inspector asks for the key.
    const inheritedKey = modelProviderKeyState(model, provider)
      ?? providerKeyStates(provider).find((key) => key.id === selectedFetchKey)
      ?? providerKeyStates(provider)[0];
    void dispatch("model.add", { provider_id: providerId, model: { name, upstream_model: name, enabled: true, order: 0, ...(inheritedKey ? { api_key_name: inheritedKey.name, provider_key_id: inheritedKey.id } : {}) } });
  };
  const addProvider = (): void => {
    // The pane commits every edit, so a new provider is valid the moment it
    // exists: a placeholder name and no key or model yet, with the editor
    // below collecting the URL, key, and models.  The wizard keeps its own
    // button.
    const base = translate("providers.newProvider");
    const name = uniquePlaceholderName(providers.map((item) => providerDisplayName(item)), base);
    pendingProviderIds.current = new Set(providers.map(editorIdentifier));
    setSelectedModel(undefined);
    setProviderSourceModel(undefined);
    // A new provider starts enabled the way a new model does: it carries no
    // model yet, so it contributes no deployment until the user adds one.
    void dispatch("provider.add", { provider: { name, models: [], enabled: true } });
  };
  const duplicateModel = (): void => {
    if (!model) return;
    void dispatch("model.duplicate", { provider_id: providerId, model_id: editorIdentifier(model) });
  };
  const providerDisplayName = useCallback((entry: UnknownRecord): string => {
    const entryID = editorIdentifier(entry);
    return providerNameDrafts[entryID] !== undefined
      ? providerNameDrafts[entryID]
      : displayLabel(entry.display_name, displayLabel(entry.name, translate("providers.newProvider")));
  }, [providerNameDrafts, translate]);
  const modelDisplayName = useCallback((entryProviderID: string, entryModel: UnknownRecord): string => {
    const entryModelID = editorIdentifier(entryModel);
    const draft = modelNameDrafts[providerModelDraftKey(entryProviderID, entryModelID)];
    return draft !== undefined
      ? draft
      : displayLabel(entryModel.display_name, displayLabel(entryModel.model_name ?? entryModel.name, translate("providers.unnamedModel")));
  }, [modelNameDrafts, translate]);
  const modelUpstreamDisplay = useCallback((entryProviderID: string, entryModel: UnknownRecord): string => {
    const entryModelID = editorIdentifier(entryModel);
    const draft = modelUpstreamDrafts[providerModelDraftKey(entryProviderID, entryModelID)];
    return draft !== undefined
      ? draft
      : upstreamModelLabel(entryModel);
  }, [modelUpstreamDrafts]);
  const modelOrderText = useCallback((entryProviderID: string, entryModel: UnknownRecord): string => {
    const entryModelID = editorIdentifier(entryModel);
    const draft = modelOrderDrafts[providerModelDraftKey(entryProviderID, entryModelID)];
    return draft !== undefined
      ? draft
      : String(modelEffectiveOrder(entryModel));
  }, [modelOrderDrafts]);
  const modelOrderValue = useCallback((entryProviderID: string, entryModel: UnknownRecord): number => {
    const parsed = Number(modelOrderText(entryProviderID, entryModel));
    return Number.isFinite(parsed) ? parsed : modelEffectiveOrder(entryModel);
  }, [modelOrderText]);
  // Keep every affected entity projected until its own snapshot entry catches
  // up. Multiple blur commits may be in flight while the user moves through
  // the tables, so one field's next draft must never hide another field's.
  useEffect(() => {
    const providerByID = new Map(providers.map((entry) => [editorIdentifier(entry), entry]));
    const modelByKey = new Map(providers.flatMap((entry) => asRecords(entry.models).map(modelRecord).map((model) => [providerModelDraftKey(editorIdentifier(entry), editorIdentifier(model)), model] as const)));
    setProviderNameDrafts((current) => pruneStringDrafts(current, (providerID, value) => {
      const entry = providerByID.get(providerID);
      return entry !== undefined && stringValue(entry.display_name, stringValue(entry.name, translate("providers.newProvider"))) !== value;
    }));
    setProviderBaseUrlDrafts((current) => pruneStringDrafts(current, (providerID, value) => {
      const entry = providerByID.get(providerID);
      return entry !== undefined && stringValue(entry.endpoint, stringValue(entry.api_base)) !== value;
    }));
    setModelNameDrafts((current) => pruneStringDrafts(current, (key, value) => {
      const model = modelByKey.get(key);
      return model !== undefined && stringValue(model.name) !== value;
    }));
    setModelUpstreamDrafts((current) => pruneStringDrafts(current, (key, value) => {
      const model = modelByKey.get(key);
      return model !== undefined && upstreamModelLabel(model) !== value;
    }));
    setModelOrderDrafts((current) => pruneStringDrafts(current, (key, value) => {
      const model = modelByKey.get(key);
      return model !== undefined && String(modelEffectiveOrder(model)) !== value;
    }));
  }, [modelNameDrafts, modelOrderDrafts, modelUpstreamDrafts, providerBaseUrlDrafts, providerNameDrafts, providers, translate]);
  const routes = useMemo(() => providers.flatMap((entry, providerIndex) => asRecords(entry.models).map(modelRecord).flatMap((entryModel, modelIndex) => {
    const publicModel = modelDisplayName(editorIdentifier(entry), entryModel).trim();
    const deploymentID = stringValue(entryModel.editor_id, stringValue(entryModel.deployment_id, identifier(entryModel))).trim();
    if (!publicModel || !deploymentID) return [];
    const keyNames = new Set(stringList(entry.api_key_names));
    const keyName = stringValue(entryModel.api_key_name).trim();
    const providerEnabled = booleanValue(entry.enabled, true);
    const modelEnabled = booleanValue(entryModel.model_enabled, booleanValue(entryModel.enabled, true));
    const keyAvailable = booleanValue(entryModel.api_key_configured, !keyName || keyNames.has(keyName));
    return [{
      key: `${editorIdentifier(entry)}:${deploymentID}`,
      deploymentID,
      publicModel,
      provider: entry,
      providerIndex,
      model: entryModel,
      modelIndex,
      providerEnabled,
      modelEnabled,
      keyAvailable,
    }];
  })).sort((left, right) => {
    const modelOrder = left.publicModel.localeCompare(right.publicModel, undefined, { sensitivity: "base" });
    if (modelOrder !== 0) return modelOrder;
    const leftOrder = modelOrderValue(editorIdentifier(left.provider), left.model);
    const rightOrder = modelOrderValue(editorIdentifier(right.provider), right.model);
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    const providerOrder = providerDisplayName(left.provider).localeCompare(providerDisplayName(right.provider), undefined, { sensitivity: "base" });
    if (providerOrder !== 0) return providerOrder;
    return left.deploymentID.localeCompare(right.deploymentID);
  }), [modelDisplayName, modelOrderValue, providerDisplayName, providers]);
  // Routes grouped by their client-facing name: one public model is one group
  // in the routes table and one settings surface.
  const routeGroups = useMemo(() => {
    const groups = new Map<string, Array<(typeof routes)[number]>>();
    for (const entry of routes) {
      const list = groups.get(entry.publicModel);
      if (list) list.push(entry);
      else groups.set(entry.publicModel, [entry]);
    }
    return Array.from(groups, ([name, entries]) => ({ name, entries }));
  }, [routes]);
  const modelContexts = asRecord(state.model_contexts);
  // Why the last relay-bound Apply refused: Core hands back the rows it named,
  // so the pane marks the key or route instead of only stating the outcome.
  const bindingIssues = asRecords(state.binding_issues);
  const activePublicGroup = selectedPublicModel === undefined
    ? undefined
    : routeGroups.find((group) => group.name === selectedPublicModel);
  const activeRoute = routes.find((entry) => entry.key === selectedRoute);
  const activeRouteGroup = activeRoute ? routes.filter((entry) => entry.publicModel === activeRoute.publicModel) : [];
  const activeRouteIndex = activeRoute ? activeRouteGroup.findIndex((entry) => entry.key === activeRoute.key) : -1;
  // One route that follows a relay multiplier locks the whole group: its order
  // is the station's own number, so Core refuses to permute the group's typed
  // values at all (moving a plain route in such a group is the same rewrite).
  // The buttons state that here instead of offering a press Core will refuse.
  const activeRouteGroupUsesMultiplier = activeRouteGroup.some((entry) => modelOrderMode(entry.model) === "relay_multiplier");
  const routeMoveTitleKey = activeRouteGroupUsesMultiplier ? "providers.reorderMultiplierLocked" : undefined;
  const canMoveRouteUp = !activeRouteGroupUsesMultiplier && activeRouteIndex > 0;
  const canMoveRouteDown = !activeRouteGroupUsesMultiplier && activeRouteIndex >= 0 && activeRouteIndex < activeRouteGroup.length - 1;
  useEffect(() => {
    // A cleared route selection stays cleared; the first route only fills in
    // when the selected one is gone from the list.
    if (viewMode !== "routes" || routes.length === 0 || selectedRoute === "" || routes.some((entry) => entry.key === selectedRoute)) return;
    setSelectedRoute(routes[0].key);
  }, [routes, selectedRoute, viewMode]);
  useEffect(() => {
    const pending = pendingRouteKey.current;
    if (!pending || !routes.some((entry) => entry.key === pending)) return;
    pendingRouteKey.current = undefined;
    setSelectedPublicModel(undefined);
    setSelectedRoute(pending);
  }, [routes]);
  // The group's own numbers, in list order: a route that follows a relay
  // multiplier has no typed number of its own, so it never reaches here.
  const routeOrderValues = activeRouteGroup.map((entry) => modelOrderValue(editorIdentifier(entry.provider), entry.model));
  const moveRoute = (direction: "up" | "down"): void => {
    if (!activeRoute || activeRouteIndex < 0 || activeRouteGroupUsesMultiplier) return;
    const targetIndex = direction === "up" ? activeRouteIndex - 1 : activeRouteIndex + 1;
    if (targetIndex < 0 || targetIndex >= activeRouteGroup.length) return;
    // The values travel with the routes, so two routes the group already lists
    // with the same number cannot trade one: Core would assign the same values
    // back and the press would look like it did nothing.  The strip says why
    // instead of letting a live button answer with silence.
    if (routeOrderValues[activeRouteIndex] === routeOrderValues[targetIndex]) {
      onStatus(translate("providers.reorderSameOrder", { order: String(routeOrderValues[activeRouteIndex]) }));
      return;
    }
    const reordered = [...activeRouteGroup];
    [reordered[activeRouteIndex], reordered[targetIndex]] = [reordered[targetIndex], reordered[activeRouteIndex]];
    const reorder = (renumber: boolean): Promise<unknown> => dispatch("routes.reorder_group", { public_model: activeRoute.publicModel, route_ids: reordered.map((entry) => entry.deploymentID), ...(renumber ? { renumber: true } : {}) });
    // A move keeps the numbers the group already carries — they travel with the
    // routes — so a group holding decimals is asked once whether it should
    // become 1..n instead of being renumbered behind the user's back.  Integers
    // are already the plain order, so they move without a question.  The
    // dismissing answer cancels the move: a group whose numbers are a rate
    // (0.1, 0.12, …) has no plain order to fall back on, and 取消 is read as
    // "leave everything as it is" rather than "renumber me differently".
    const decimals = routeOrderValues.filter((value) => !Number.isInteger(value));
    if (decimals.length === 0) {
      void reorder(false);
      return;
    }
    void native.showConfirmation({
      title: translate("providers.reorderIntegerTitle"),
      message: translate("providers.reorderIntegerMessage", { orders: decimals.join(translate("providers.orderListSeparator")) }),
      confirmLabel: translate("providers.reorderIntegerConfirm"),
      cancelLabel: translate("providers.reorderCancelMove"),
    }).then((renumber) => {
      if (renumber) void reorder(true);
    });
  };
  // ＋ adds a route to the group the user is looking at: a draft on the
  // selected route's provider (else the first one) carrying the group's public
  // name, so the row appears in this group and the inspector asks for the
  // upstream model and the key.  A disabled draft never reaches the runtime.
  const addRoute = (): void => {
    const publicModel = (selectedPublicModel ?? activeRoute?.publicModel ?? "").trim();
    const targetProvider = activeRoute?.provider ?? providers[0];
    if (!targetProvider) return;
    const targetProviderID = editorIdentifier(targetProvider);
    const targetModels = asRecords(targetProvider.models).map(modelRecord);
    const knownModelIds = new Set(targetModels.map(editorIdentifier));
    pendingModelIds.current = { providerId: targetProviderID, ids: knownModelIds };
    const name = publicModel || uniquePlaceholderName(targetModels.map((item) => stringValue(item.model_name ?? item.name)), translate("providers.newModel"));
    // The row inherits the key the user is working in, the way the models pane
    // does, so ＋ files it in a real key group instead of an invented one.
    const inheritedKey = (activeRoute && editorIdentifier(activeRoute.provider) === targetProviderID ? modelProviderKeyState(activeRoute.model, targetProvider) : undefined)
      ?? providerKeyStates(targetProvider).find((key) => key.id === selectedFetchKey)
      ?? providerKeyStates(targetProvider)[0];
    void dispatchWithOutcome("model.add", {
      provider_id: targetProviderID,
      model: {
        name,
        upstream_model: publicModel || "",
        enabled: false,
        order: 0,
        ...(inheritedKey ? { api_key_name: inheritedKey.name, provider_key_id: inheritedKey.id } : {}),
      },
    }).then((next) => {
      if (!next || !publicModel) return;
      const nextProvider = snapshotProviderRecords(next).find((entry) => editorIdentifier(entry) === targetProviderID);
      const added = nextProvider ? asRecords(nextProvider.models).map(modelRecord).find((entry) => !knownModelIds.has(editorIdentifier(entry)) && stringValue(entry.model_name ?? entry.name).trim() === name) : undefined;
      pendingModelIds.current = undefined;
      if (added) pendingRouteKey.current = `${targetProviderID}:${stringValue(added.editor_id, stringValue(added.deployment_id, identifier(added))).trim()}`;
    });
  };
  // − deletes what the user selected: one route, or — with a public model's own
  // row selected — every route that serves that name.  The group case is one
  // Core action, so the group is removed in one apply instead of route by
  // route, and the confirmation states the count it is about to remove.
  const confirmDeleteRoute = (): void => {
    const selectedGroup = selectedPublicModel !== undefined ? routeGroups.find((group) => group.name === selectedPublicModel) : undefined;
    if (selectedGroup) {
      void native.showConfirmation({
        title: translate("providers.deletePublicModel", { model: selectedGroup.name }),
        message: translate("providers.deletePublicModelMessage", { routes: selectedGroup.entries.length }),
        confirmLabel: translate("common.delete"),
        destructive: true,
      }).then((confirmed) => confirmed ? dispatch("public.model_delete", { public_model: selectedGroup.name }).then(() => { setSelectedPublicModel(undefined); setSelectedRoute(""); }) : undefined);
      return;
    }
    if (!activeRoute) return;
    const routeProviderID = editorIdentifier(activeRoute.provider);
    const routeModelID = editorIdentifier(activeRoute.model);
    const upstream = modelUpstreamDisplay(routeProviderID, activeRoute.model) || activeRoute.publicModel;
    void native.showConfirmation({
      title: translate("providers.deleteRoute"),
      message: translate("providers.deleteRouteMessage", { provider: providerDisplayName(activeRoute.provider), upstream, model: activeRoute.publicModel }),
      confirmLabel: translate("common.delete"),
      destructive: true,
    }).then((confirmed) => confirmed ? dispatch("model.delete", { provider_id: routeProviderID, model_id: routeModelID }).then(() => setSelectedRoute("")) : undefined);
  };
  const confirmDeleteProvider = (): void => {
    if (!provider) return;
    const label = providerDisplayName(provider);
    const kind = providerKindSelected;
    const action = kind === "openai" || kind === "claude" || workbuddyProviderIDFor(kind) !== undefined
      ? "service_provider.delete"
      : "provider.delete";
    const stationBeingRemoved = kind === "relay" ? stationForProvider(provider) : undefined;
    const message = stationBeingRemoved
      ? translate("providers.deleteRelayProviderBody", {
          label,
          accounts: stationBeingRemoved.accountIDs.length,
          keys: stationAccountsFor(provider).reduce((total, account) => total + account.resources.length, 0),
          models: models.length,
        })
      : `${label} (${models.length} ${translate("providers.models")})`;
    void native.showConfirmation({ title: translate("providers.deleteProvider"), message, confirmLabel: translate("common.delete"), destructive: true }).then((confirmed) => {
      if (!confirmed) return undefined;
      return dispatch(action, { provider_id: providerId }).then(async () => {
        // A 中转站 provider owns its station connection: once the last
        // provider bound to it goes away, remove the station, its accounts,
        // and their native sessions just like the old workspace did.
        if (stationBeingRemoved) {
          const stillBound = providers.some((entry) => entry !== provider
            && providerKind(entry) === "relay"
            && stringValue(entry.relay_station_id).trim() === stationBeingRemoved.id);
          if (!stillBound) {
            try {
              await relay.commit("station.remove", { id: stationBeingRemoved.id, dependency_policy: "detach" });
              for (const accountID of stationBeingRemoved.accountIDs) {
                try {
                  await native.clearRelayCredentials(accountID);
                  await relay.commit("credential_cleanup_confirm", { id: accountID, kind: "credentials" });
                } catch {
                  // Core retains a secret-free cleanup tombstone for retry.
                }
              }
            } catch (reason) {
              onStatus(errorMessage(reason, translate));
            }
          }
        }
        setSelectedProvider(undefined);
        setSelectedModel(undefined);
        setProviderSourceModel(undefined);
      });
    });
  };
  const confirmDeleteModel = (): void => {
    if (!model) return;
    const modelId = editorIdentifier(model);
    void native.showConfirmation({ title: translate("providers.deleteModel"), message: modelDisplayName(providerId, model) || modelId, confirmLabel: translate("common.delete"), destructive: true }).then((confirmed) => confirmed ? dispatch("model.delete", { provider_id: providerId, model_id: modelId }).then(() => setSelectedModel(undefined)) : undefined);
  };
  const providerRows = useMemo(
    () => providers.map((item) => ({
      key: editorIdentifier(item),
      cells: [providerDisplayName(item)],
    })),
    [providerDisplayName, providers],
  );
  const disabledProviderKeys = useMemo(
    () => providers.filter((item) => !booleanValue(item.enabled, true)).map(editorIdentifier),
    [providers],
  );
  const modelRows = useMemo(() => {
    // The key name is its own column: one spanning row per key, with that
    // key's models listed (and indented) underneath it.  A model that belongs
    // to no key yet — the draft ＋ just created on a provider that has none —
    // is listed first *without* a group row: filing it under an invented
    // 未定义密钥 group would present a group the app made up as its route.
    // One group is one provider key, addressed by its own slot id: a station
    // key and a custom key that read the same are two keys with two groups,
    // and a model is filed under the key it names, never under a namesake.
    // This table carries no 顺序 column: a model's order orders the routes
    // that share one public name, and these rows are grouped by key, so the
    // same column read down the pane would compare numbers that belong to
    // different public models — three models with three public names have no
    // one sequence between them.  The value is edited where it belongs (the
    // inspector's own 顺序 field) and read where its group is the list: the
    // routes table, whose rows are the public model's routes in that order.
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [];
    const ungrouped: UnknownRecord[] = [];
    const grouped = new Map<string, { label: string; models: UnknownRecord[] }>();
    for (const item of models) {
      const key = modelProviderKeyState(item, provider ?? {});
      if (!key) {
        ungrouped.push(item);
        continue;
      }
      const list = grouped.get(key.id);
      if (list) {
        list.models.push(item);
        continue;
      }
      grouped.set(key.id, { label: modelProviderKeyLabel(item, provider ?? {}, translate, undefined, relaySources), models: [item] });
    }
    for (const item of ungrouped) {
      rows.push({ key: editorIdentifier(item), cells: [`\t${modelUpstreamDisplay(providerId, item)}`, modelDisplayName(providerId, item)] });
    }
    for (const [keyID, group] of grouped) {
      rows.push({ key: `key:${keyID}`, cells: [group.label], spanning: true });
      for (const item of group.models) {
        rows.push({ key: editorIdentifier(item), cells: [`\t${modelUpstreamDisplay(providerId, item)}`, modelDisplayName(providerId, item)] });
      }
    }
    return rows;
  }, [modelDisplayName, modelUpstreamDisplay, models, provider, providerId, relaySources, translate]);
  const disabledModelKeys = useMemo(
    () => models.filter((item) => !booleanValue(provider?.enabled, true) || !booleanValue(item.model_enabled, booleanValue(item.enabled, true))).map(editorIdentifier),
    [models, provider?.enabled],
  );
  // Rows that cannot materialize a route keep the pane honest about what
  // blocks Apply: Core rejects a model without a public name, so the row that
  // needs the name is marked where it is edited — and a linked route the relay
  // could not resolve is marked on the row Core named, because that key or
  // route, not the pane, is what refused the write.
  const bindingIssueFor = useCallback((model: UnknownRecord): UnknownRecord | undefined => {
    const modelID = editorIdentifier(model);
    const keyID = stringValue(model.provider_key_id);
    return bindingIssues.find((issue) => {
      const issueModel = stringValue(issue.model_id);
      if (issueModel && issueModel === modelID) return true;
      const issueKey = stringValue(issue.provider_key_id);
      return Boolean(issueKey) && issueKey === keyID;
    });
  }, [bindingIssues]);
  const alertModelKeys = useMemo(
    () => models.filter((item) => modelNeedsAttention(item, translate) || Boolean(bindingIssueFor(item))).map(editorIdentifier),
    [bindingIssueFor, models, translate],
  );
  const alertProviderKeys = useMemo(
    () => providers.filter((item) => {
      if (bindingIssues.some((issue) => stringValue(issue.provider) === editorIdentifier(item))) return true;
      return asRecords(item.models).some((model) => modelNeedsAttention(model, translate) || Boolean(bindingIssueFor(model)));
    }).map(editorIdentifier),
    [bindingIssueFor, bindingIssues, providers, translate],
  );
  const routeRows = useMemo(() => {
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [];
    for (const group of routeGroups) {
      rows.push({
        key: routePublicModelRowKey(group.name),
        cells: [group.name, "", "", ""],
        spanning: true,
      });
      for (const entry of group.entries) {
        const order = modelOrderText(editorIdentifier(entry.provider), entry.model);
        rows.push({
          key: entry.key,
          // The upstream model leads the row: it is what tells the routes of
          // one public model apart, before the provider that serves it.
          cells: [`\t${modelUpstreamDisplay(editorIdentifier(entry.provider), entry.model) || translate("common.notAvailable")}`, providerDisplayName(entry.provider), modelProviderKeyLabel(entry.model, entry.provider, translate, undefined, relaySources), order],
        });
      }
    }
    return rows;
  }, [modelOrderText, modelUpstreamDisplay, providerDisplayName, relaySources, routeGroups, translate]);
  const selectableRouteGroupKeys = useMemo(
    () => routeGroups.map((group) => routePublicModelRowKey(group.name)),
    [routeGroups],
  );
  const alertRouteKeys = useMemo(
    () => routeGroups.flatMap((group) => group.entries.filter((entry) => Boolean(bindingIssueFor(entry.model))).map((entry) => entry.key)),
    [bindingIssueFor, routeGroups],
  );
  const disabledRouteKeys = useMemo(
    () => routes.filter((entry) => !entry.providerEnabled || !entry.modelEnabled || !entry.keyAvailable).map((entry) => entry.key),
    [routes],
  );
  const selectRoute = useCallback((routeId: string): void => {
    setPublicModelReturn(undefined);
    if (!routeId) {
      // A click below the route rows clears the selection: the inspector
      // empties instead of keeping a route active without a highlighted row.
      setSelectedRoute("");
      setSelectedPublicModel(undefined);
      setProviderSourceModel(undefined);
      return;
    }
    const selected = routes.find((entry) => entry.key === routeId);
    if (!selected) return;
    setSelectedRoute(routeId);
    setSelectedPublicModel(undefined);
    setSelectedProvider(editorIdentifier(selected.provider));
    setSelectedModel(editorIdentifier(selected.model));
    setProviderSourceModel(undefined);
  }, [routes]);
  // One routes table answers clicks for both row kinds: a public-model row
  // opens that group's own settings, a route row opens the model detail.
  const selectRouteTableRow = useCallback((rowKey: string): void => {
    if (!rowKey) {
      selectRoute("");
      return;
    }
    if (rowKey.startsWith(ROUTE_PUBLIC_MODEL_PREFIX)) {
      const publicModel = rowKey.slice(ROUTE_PUBLIC_MODEL_PREFIX.length);
      const group = routeGroups.find((entry) => entry.name === publicModel);
      if (!group) return;
      setSelectedPublicModel(publicModel);
      setSelectedRoute("");
      setProviderSourceModel(undefined);
      // A row click has no origin model to return to; only 设置 records one.
      setPublicModelReturn(undefined);
      const first = group.entries[0];
      setSelectedProvider(editorIdentifier(first.provider));
      setSelectedModel(editorIdentifier(first.model));
      return;
    }
    selectRoute(rowKey);
  }, [routeGroups, selectRoute]);
  // A public model the draft no longer carries (renamed elsewhere, or its last
  // route removed) drops its selection instead of leaving an empty pane.
  useEffect(() => {
    if (selectedPublicModel === undefined) return;
    if (routeGroups.some((group) => group.name === selectedPublicModel)) return;
    setSelectedPublicModel(undefined);
    if (viewMode === "routes" && routes.length > 0) setSelectedRoute(routes[0].key);
  }, [routeGroups, routes, selectedPublicModel, viewMode]);
  const returnToPublicModelOrigin = useCallback((): void => {
    if (!publicModelReturn) return;
    selectRoute(publicModelReturn.routeKey);
  }, [publicModelReturn, selectRoute]);
  const chooseViewMode = (value: "providers" | "routes"): void => {
    if (value === viewMode) return;
    if (value === "routes" && selectedPublicModel === undefined) {
      const first = routes[0];
      if (first) selectRoute(first.key);
    }
    setViewMode(value);
  };
  const providerDraftProjection = useMemo<ProviderWorkspaceDraftProjection>(() => ({
    providers,
    providerDisplayName,
    modelDisplayName,
    providerBaseURL,
    modelUpstreamDisplay,
    modelOrderText,
    providerKeyDisplayName: (providerID, keyID, fallback) => fallback,
    setProviderNameDraft,
    setProviderBaseUrlDraft,
    setModelNameDraft,
    setModelUpstreamDraft,
    setModelOrderDraft,
    setProviderKeyNameDraft: () => undefined,
  }), [modelDisplayName, modelOrderText, modelUpstreamDisplay, providerBaseURL, providerDisplayName, providers, setModelNameDraft, setModelOrderDraft, setModelUpstreamDraft, setProviderBaseUrlDraft, setProviderNameDraft]);
  // 账号管理 data for any provider row (used by the detail pane).
  const stationForProvider = useCallback((entry: UnknownRecord | undefined): RelayStation | undefined => {
    if (!entry || providerKind(entry) !== "relay") return undefined;
    const stationID = stringValue(entry.relay_station_id).trim();
    return relayStationsFull.find((station) => station.id === stationID)
      ?? relayStationsFull.find((station) => stationOriginKey(station.origin) === stationOriginKey(providerBaseURL(entry)));
  }, [providerBaseURL, relayStationsFull]);
  const stationAccountsFor = useCallback((entry: UnknownRecord | undefined): RelayAccount[] => {
    const station = stationForProvider(entry);
    return station ? relayAccounts.filter((account) => station.accountIDs.includes(account.id)) : [];
  }, [relayAccounts, stationForProvider]);
  const selectedStation = provider ? stationForProvider(provider) : undefined;
  const selectedStationAccounts = stationAccountsFor(provider);
  const customKeyStates = useMemo(() => provider ? providerKeyStates(provider).filter((key) => key.source.kind === "independent") : [], [provider]);
  return <ProviderWorkspaceDraftContext.Provider value={providerDraftProjection}><View style={styles.providersLayout}>
    <View style={styles.providerLeftColumn}>
      <View style={styles.providerToolbar}>
        <WindowTabs values={[{ id: "providers", title: translate("providers.providers") }, { id: "routes", title: translate("providers.routes") }]} selected={viewMode} onSelect={(value) => chooseViewMode(value as "providers" | "routes")} />
        {/* The wizard is an action, not a third tab: keep it right-aligned so
            it never reads as part of the segmented control. */}
        <View style={styles.toolbarSpacer} />
        <ActionButton title={translate("providers.addWizard")} disabled={busy} style={styles.providerWizardToolbarButton} onPress={onOpenWizard} />
      </View>
      {viewMode === "routes" ? <View style={styles.routeWorkspace}>
        <TablePane wide style={styles.routeTablePane} title={translate("providers.routes")} actions={<><IconButton label="+" title={translate("providers.newRoute")} disabled={busy || providers.length === 0} onPress={addRoute} />{activeRoute || selectedPublicModel !== undefined ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteRoute} /> : null}<IconButton label="↑" title={routeMoveTitleKey ? translate(routeMoveTitleKey) : translate("common.moveUp")} disabled={busy || !canMoveRouteUp} onPress={() => moveRoute("up")} /><IconButton label="↓" title={routeMoveTitleKey ? translate(routeMoveTitleKey) : translate("common.moveDown")} disabled={busy || !canMoveRouteDown} onPress={() => moveRoute("down")} /></>}>
          <NativeTable columns={[{ label: translate("providers.upstream"), width: 120 }, { label: translate("providers.provider"), width: 96 }, { label: translate("providers.providerKey"), width: 130 }, { label: translate("common.order"), width: 64 }]} rows={routeRows} disabledRowKeys={disabledRouteKeys} alertRowKeys={alertRouteKeys} selectedKey={selectedPublicModel !== undefined ? routePublicModelRowKey(selectedPublicModel) : (selectedRoute ?? "")} compact selectableSpanningRowKeys={selectableRouteGroupKeys} onSelectionChange={(key) => selectRouteTableRow(key)} style={styles.nativeRouteTable} />
        </TablePane>
      </View> : <View style={styles.providerWorkspace}>
        <View style={styles.providerModelColumns}>
          <TablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} />{provider ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteProvider} /> : null}</>}>
            <NativeTable columns={[{ label: translate("providers.provider"), width: 132 }]} rows={providerRows} disabledRowKeys={disabledProviderKeys} alertRowKeys={alertProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }} style={styles.nativeProviderTable} />
          </TablePane>
          <View style={styles.providerMiddlePane}>
            <TablePane style={[styles.modelListPane]} title={translate("providers.models")} actions={<>{provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? <IconButton label="+" title={translate("providers.newModel")} disabled={busy} onPress={addModel} /> : null}{model ? <IconButton label="⧉" title={translate("common.copy")} disabled={busy} onPress={duplicateModel} /> : null}{model ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteModel} /> : null}</>}>
              <NativeTable columns={[{ label: translate("providers.upstream"), width: 120 }, { label: translate("providers.publicModel"), width: 160 }]} rows={modelRows} disabledRowKeys={disabledModelKeys} alertRowKeys={alertModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedModel(key); setProviderSourceModel(undefined); }} style={styles.nativeModelTable} />
              {provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? <View style={styles.tableBottomRow}><NativePicker labels={fetchKeyOptions.length > 0 ? fetchKeyOptions.map((option) => option.label) : [translate("common.default")]} selectedValue={fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? translate("common.default")} disabled={busy || fetchKeyChoices.length === 0} onChange={({ nativeEvent }) => { const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyID(option.value); }} style={styles.fetchKeyPicker} /><ActionButton title={translate("providers.fetch")} busy={fetchModelsBusy} disabled={(busy && !fetchModelsBusy) || !selectedFetchKey} onPress={() => { void fetchModels(); }} /></View> : null}
            </TablePane>
          </View>
        </View>
      </View>}
    </View>
    <View style={styles.providerInspector}>{viewMode === "routes" ? (activePublicGroup ? <PublicModelInspector key={`public:${activePublicGroup.name}`} group={activePublicGroup} modelContexts={modelContexts} backLabel={publicModelReturn?.label} onBackToModel={returnToPublicModelOrigin} busy={busy} translate={translate} dispatch={dispatch} dispatchSnapshot={dispatchWithOutcome} onRenamed={(next) => { setSelectedPublicModel(next); setSelectedRoute(""); }} /> : activeRoute ? (providerSourceModel ? <ProviderEditor key={`provider:${editorIdentifier(activeRoute.provider)}`} provider={activeRoute.provider} relaySources={relaySources} relayStations={relayStations} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onNameDraftChange={(value) => setProviderNameDraft(editorIdentifier(activeRoute.provider), value)} sourceModel={activeRoute.model} onReturnToModel={() => { setProviderSourceModel(undefined); setSelectedModel(editorIdentifier(activeRoute.model)); }} station={stationForProvider(activeRoute.provider)} stationAccounts={stationAccountsFor(activeRoute.provider)} relay={relay} addOfficialAccount={addOfficialAccount} onActivateAndRestart={onActivateAndRestart} onStatus={onStatus} language={snapshot?.language ?? "system"} bindingIssues={bindingIssues} snapshotForCleanups={snapshot} /> : <ModelInspector key={`model:${editorIdentifier(activeRoute.provider)}:${editorIdentifier(activeRoute.model)}`} providers={providers} providerLabels={providers.map(providerDisplayName)} provider={activeRoute.provider} providerId={editorIdentifier(activeRoute.provider)} model={activeRoute.model} modelName={modelDisplayName(editorIdentifier(activeRoute.provider), activeRoute.model)} relaySources={relaySources} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchSnapshot={dispatchWithOutcome} modelContexts={modelContexts} bindingIssue={bindingIssueFor(activeRoute.model)} {...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model), probeInputFingerprint(providerBaseURL(activeRoute.provider), modelUpstreamDisplay(editorIdentifier(activeRoute.provider), activeRoute.model), activeRoute.model))} onNameDraftChange={(value) => setModelNameDraft(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model), value)} onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))} onOpenPublicModel={() => { // The pane is keyed by the name the routes table groups under, which is
                  // the display name the drafts already project, so 设置 opens that
                  // group's settings even while the name field holds a draft.
                  const publicModel = activeRoute.publicModel.trim(); if (!publicModel) return; selectRouteTableRow(routePublicModelRowKey(publicModel)); setPublicModelReturn({ routeKey: activeRoute.key, label: modelUpstreamDisplay(editorIdentifier(activeRoute.provider), activeRoute.model) || publicModel }); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: editorIdentifier(activeRoute.provider), model_id: editorIdentifier(activeRoute.model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(activeRoute.model)); setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`); setProviderSourceModel(undefined); })} />) : <EmptyState translate={translate} />) : provider && model ? <ModelInspector key={`model:${providerId}:${editorIdentifier(model)}`} providers={providers} providerLabels={providers.map(providerDisplayName)} provider={provider} providerId={providerId} model={model} modelName={modelDisplayName(providerId, model)} relaySources={relaySources} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchSnapshot={dispatchWithOutcome} modelContexts={modelContexts} bindingIssue={bindingIssueFor(model)} {...modelProbeProps(providerId, editorIdentifier(model), probeInputFingerprint(providerBaseURL(provider), modelUpstreamDisplay(providerId, model), model))} onNameDraftChange={(value) => setModelNameDraft(providerId, editorIdentifier(model), value)} onProviderClick={() => { setProviderSourceModel(editorIdentifier(model)); setSelectedModel(undefined); }} onOpenPublicModel={() => { const publicModel = modelDisplayName(providerId, model).trim(); if (!publicModel) return; const originRouteKey = `${providerId}:${stringValue(model.editor_id, stringValue(model.deployment_id, identifier(model))).trim()}`; setViewMode("routes"); selectRouteTableRow(routePublicModelRowKey(publicModel)); setPublicModelReturn({ routeKey: originRouteKey, label: modelUpstreamDisplay(providerId, model) || publicModel }); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: providerId, model_id: editorIdentifier(model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(model)); setProviderSourceModel(undefined); })} /> : provider ? <ProviderEditor key={`provider:${providerId}`} provider={provider} relaySources={relaySources} relayStations={relayStations} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onNameDraftChange={(value) => setProviderNameDraft(providerId, value)} sourceModel={models.find((item) => editorIdentifier(item) === providerSourceModel)} onReturnToModel={() => { if (providerSourceModel) setSelectedModel(providerSourceModel); setProviderSourceModel(undefined); }} station={selectedStation} stationAccounts={selectedStationAccounts} relay={relay} addOfficialAccount={addOfficialAccount} onActivateAndRestart={onActivateAndRestart} onStatus={onStatus} language={snapshot?.language ?? "system"} bindingIssues={bindingIssues} snapshotForCleanups={snapshot} /> : <EmptyState translate={translate} />}</View>
  </View></ProviderWorkspaceDraftContext.Provider>;
}

/**
 * One key table for both key kinds: spanning group rows split 自定义密钥 and
 * 供应商提供, and the editor below always edits exactly the selected key.
 */
/**
 * One key table for both key kinds: spanning group rows split 自定义密钥 and
 * 供应商提供, and the editor below always edits exactly the selected key.
 * variant "pane" renders its own titled pane for the middle column;
 * variant "inline" renders a compact section for the right detail pane.
 */
function ProviderKeysPanel({ provider, providerId, kind, stationAccounts, native, busy, translate, dispatch, onSecretState, relay, onStatus, language, bindingIssues, variant = "pane" }: { provider?: UnknownRecord; providerId: string; kind: ProviderKind; stationAccounts: RelayAccount[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; relay: RelayWorkspaceBridge; onStatus: (status?: string) => void; language: "system" | "en" | "zh-Hans"; bindingIssues?: UnknownRecord[]; snapshotForCleanups?: CoreSnapshot; variant?: "pane" | "inline" }): React.JSX.Element {
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const isRelay = kind === "relay";
  const customKeys = useMemo(() => provider ? providerKeyStates(provider).filter((key) => key.source.kind === "independent") : [], [provider]);
  const customKeyNames = useMemo(() => stringList(provider?.api_key_names), [provider?.api_key_names]);
  const providedRows = useMemo(() => providedKeyRows(stationAccounts, translate), [stationAccounts, translate]);
  const firstSelectable = customKeys[0] ? `custom:${customKeys[0].id}` : providedRows[0] ? `provided:${providedRows[0].key}` : "";
  const [selectedKey, setSelectedKey] = useState<string>(firstSelectable);
  // A cleared key selection (a click below the key rows) survives the repair
  // below: the panel keeps no key selected, hides the −, and leaves its editor
  // empty instead of quietly editing the first key again.
  const [selectionCleared, setSelectionCleared] = useState(false);
  // The key a staged edit is waiting for: a ＋ names the new key by the only
  // identity it has before it exists, while a rename names the slot it edits
  // and the name it asked for, so a refused rename can never select another
  // key that already carries that name.
  const pendingCustomKeyName = useRef<string | undefined>(undefined);
  const pendingCustomKeyRename = useRef<{ id: string; name: string } | undefined>(undefined);
  const [providedNameDrafts, setProvidedNameDrafts] = useState<Record<string, string>>({});
  const [formBusy, setFormBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [remoteDelete, setRemoteDelete] = useState<{ account: RelayAccount; resource: RelayResource; label: string }>();
  const [remoteDeletePolicy, setRemoteDeletePolicy] = useState<"delete_models" | "detach_disabled" | "detach_only">("detach_disabled");
  const selectedCustom = selectedKey.startsWith("custom:")
    ? customKeys.find((key) => `custom:${key.id}` === selectedKey)
    : undefined;
  const selectedProvided = selectedKey.startsWith("provided:")
    ? providedRows.find((row) => `provided:${row.key}` === selectedKey)
    : undefined;
  // A relay key is one row in this table and one slot in the provider: the
  // issue Core reported is keyed by the slot, so the row is matched back
  // through the relay source the slot carries, and the reason is stated
  // beside the key it belongs to.
  const bindingIssueForProvidedRow = useCallback((row: ProvidedKeyRow): UnknownRecord | undefined => {
    if (!provider || !bindingIssues || bindingIssues.length === 0) return undefined;
    const slot = providerKeyStates(provider).find((key) => key.source.kind === "relay"
      && key.source.accountID === row.account.id
      && key.source.resourceID === row.resource.id);
    if (!slot) return undefined;
    return bindingIssues.find((issue) => stringValue(issue.provider_key_id) === slot.id);
  }, [bindingIssues, provider]);
  const selectedGroup: "custom" | "provided" = selectedProvided ? "provided" : "custom";
  const autoGrouping = stationAccounts.some((account) => account.autoGrouping);
  const controlsBusy = busy || formBusy;
  const tableRows = useMemo(() => {
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [];
    // Group headers only separate the two kinds; a single-kind list skips
    // them so custom-only vendors see a plain key table.
    const showHeaders = providedRows.length > 0 && customKeys.length > 0;
    if (showHeaders) rows.push({ key: "group:custom", cells: [`${translate("providers.keysCustom")} · ${customKeys.length}`], spanning: true });
    for (const key of customKeys) {
      // Custom keys indent under their group header the same way relay keys
      // indent under their account row.
      rows.push({ key: `custom:${key.id}`, cells: [showHeaders ? `\t${key.name}` : key.name] });
    }
    // Relay keys nest under their account the way routes nest under a
    // public model: spanning account rows, indented key rows beneath.
    for (const account of stationAccounts) {
      const accountProvidedRows = providedRows.filter((row) => row.account.id === account.id);
      if (accountProvidedRows.length === 0) continue;
      rows.push({ key: `account:${account.id}`, cells: [accountDisplayName(account, translate)], spanning: true });
      for (const row of accountProvidedRows) {
        rows.push({
          key: `provided:${row.key}`,
          cells: [`\t${providedNameDrafts[row.key] ?? row.label}`],
        });
      }
    }
    return rows;
  }, [customKeys, providedNameDrafts, providedRows, stationAccounts, translate]);
  const secondaryCellKeys: string[] = [];
  const runProvidedAction = async (action: () => Promise<void>, feedbackKey: string): Promise<void> => {
    setFormBusy(true);
    try {
      try {
        await action();
      } catch {
        onStatus?.(translate("relay.operationFailed"));
        return;
      }
      // The commit is the result the user asked for; the refresh that repaints
      // the merged key list is best-effort.  A snapshot read that fails while
      // Core is busy must not report a staged edit as failed — the same rule
      // the relay metadata commit states — so the next snapshot settles it.
      try {
        await relay.refreshAccounts();
      } catch {
        // The subscription, or the next pane action, repaints the staged row.
      }
      onStatus?.(translate(feedbackKey));
    } finally {
      setFormBusy(false);
    }
  };
  // Quietly keep station groups aligned while auto-grouping is on.
  //
  // The tick reads the accounts, the bridge, and the busy guard through a ref:
  // this pane re-renders on every snapshot, and an effect that depended on
  // those values directly would tear its timer down and restart the half-hour
  // countdown on each render — so the alignment would effectively never run.
  // Only the enabled flag and the set of accounts the timer covers re-arm it.
  const autoGroupingAccountIDs = useMemo(
    () => stationAccounts.filter((account) => account.autoGrouping).map((account) => account.id),
    [stationAccounts],
  );
  const autoGroupingKey = autoGroupingAccountIDs.join("\n");
  const autoGroupingTick = useRef({ relay, controlsBusy, accountIDs: autoGroupingAccountIDs });
  useEffect(() => {
    autoGroupingTick.current = { relay, controlsBusy, accountIDs: autoGroupingAccountIDs };
  });
  useEffect(() => {
    if (!autoGrouping || !autoGroupingKey) return;
    let active = true;
    const interval = setInterval(() => {
      const current = autoGroupingTick.current;
      if (!active || current.controlsBusy) return;
      const align = current.relay.apiKeyActions.alignAutoGrouping;
      if (!align) return;
      void (async () => {
        for (const accountID of current.accountIDs) {
          if (!active) return;
          try {
            const status = await current.relay.refreshResources(accountID);
            if (!active || status !== "ready") continue;
            await align(accountID);
          } catch {
            // The next interval can retry.
          }
        }
        if (active) await current.relay.refreshAccounts();
      })();
    }, 30 * 60_000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [autoGrouping, autoGroupingKey]);
  useEffect(() => {
    if (selectionCleared) return;
    // tableRows keys already carry the custom:/provided: prefix; compare the
    // selection verbatim or every click would snap back to the first row.
    const valid = new Set(tableRows.filter((row) => !row.spanning).map((row) => row.key));
    if (valid.has(selectedKey)) return;
    setSelectedKey(customKeys[0] ? `custom:${customKeys[0].id}` : providedRows[0] ? `provided:${providedRows[0].key}` : "");
  }, [customKeys, providedRows, selectedKey, selectionCleared, tableRows]);
  const addKey = (): void => {
    if (selectedGroup === "provided" && isRelay) {
      if (autoGrouping || stationAccounts.length === 0) return;
      setCreateOpen(true);
      return;
    }
    const name = uniqueKeyName(customKeyNames);
    pendingCustomKeyName.current = name;
    void dispatch("provider.key_add", { provider_id: providerId, name });
  };
  React.useLayoutEffect(() => {
    const pending = pendingCustomKeyName.current;
    if (pending) {
      const added = customKeys.find((key) => key.name === pending);
      if (added) {
        pendingCustomKeyName.current = undefined;
        setSelectionCleared(false);
        setSelectedKey(`custom:${added.id}`);
      }
    }
    const rename = pendingCustomKeyRename.current;
    if (rename) {
      const renamed = customKeys.find((key) => key.id === rename.id);
      // The renamed slot either carries the new name now (accepted) or is
      // gone; a namesake that already held the requested name is not this key
      // and must not become the selection.
      if (!renamed) {
        pendingCustomKeyRename.current = undefined;
      } else if (renamed.name === rename.name) {
        pendingCustomKeyRename.current = undefined;
        setSelectionCleared(false);
        setSelectedKey(`custom:${renamed.id}`);
      }
    }
  }, [customKeys]);
  const deleteSelected = (): void => {
    if (selectedProvided) {
      if (autoGrouping) return;
      setRemoteDeletePolicy("detach_disabled");
      setRemoteDelete({ account: selectedProvided.account, resource: selectedProvided.resource, label: selectedProvided.label });
      return;
    }
    if (!selectedCustom) return;
    const selectedKeyName = selectedCustom.name;
    const affectedModelLines = asRecords(provider?.models)
      .filter((model) => stringValue(model.api_key_name).trim() === selectedKeyName)
      .map((model, index) => {
        const publicName = displayLabel(model.model_name ?? model.name, translate("providers.unnamedModel")).trim();
        const upstreamName = upstreamModelLabel(model).trim();
        const label = upstreamName && upstreamName !== publicName ? `${publicName} (${upstreamName})` : publicName;
        return `${index + 1}. ${label}`;
      });
    void native.showConfirmation({
      title: translate("providers.deleteApiKey", { key: apiKeyDisplayName(selectedKeyName, translate) }),
      message: affectedModelLines.length > 0
        ? translate("providers.deleteApiKeyModelsMessage", { models: affectedModelLines.join("\n") })
        : translate("providers.deleteApiKeyNoModelsMessage"),
      confirmLabel: translate("common.delete"),
      destructive: true,
    }).then((confirmed) => {
      if (!confirmed) return undefined;
      return dispatch("provider.key_delete", { provider_id: providerId, name: selectedKeyName });
    });
  };
  // A + / − the current state cannot use is hidden instead of greyed out: the
  // list header keeps only the actions that apply to what is selected.
  const canAddKey = !(selectedGroup === "provided" && (autoGrouping || stationAccounts.length === 0 || !relay.apiKeyActions.create));
  const canDeleteKey = Boolean(selectedCustom) || Boolean(selectedProvided && !autoGrouping);
  const toolbar = <>
    {canAddKey ? <IconButton label="+" title={translate("providers.addKey")} disabled={controlsBusy} onPress={addKey} /> : null}
    {canDeleteKey ? <IconButton label="−" title={translate("common.delete")} disabled={controlsBusy} onPress={deleteSelected} /> : null}
  </>;
  const selectedProvidedName = selectedProvided ? providedNameDrafts[selectedProvided.key] ?? selectedProvided.keyName : "";
  const selectedProvidedIssue = selectedProvided ? bindingIssueForProvidedRow(selectedProvided) : undefined;
  // The inline keys panel shows one list of a fixed height beside its editor
  // (see KEYS_INLINE_LIST_HEIGHT): a fixed frame is what keeps the panel calm
  // while the user switches vendors, loads data, or works in the sections
  // below, and the list scrolls internally when the vendor has more keys.
  const keysTable = <NativeTable
      columns={variant === "inline"
        ? [{ label: translate("providers.keys"), width: 264 }]
        : [{ label: translate("common.name"), width: 150 }, { label: translate("providers.detail"), width: 170 }]}
      rows={tableRows}
      selectedKey={selectedCustom ? `custom:${selectedCustom.id}` : selectedProvided ? `provided:${selectedProvided.key}` : ""}
      disabledRowKeys={providedRows.filter((row) => row.unavailable).map((row) => `provided:${row.key}`)}
      alertRowKeys={providedRows.filter((row) => Boolean(bindingIssueForProvidedRow(row))).map((row) => `provided:${row.key}`)}
      compact
      cellHorizontalPadding={6}
      firstColumnHorizontalPadding={6}
      scrollTrailingColumnOverflow={false}
      onSelectionChange={(key) => {
        if (key.startsWith("group:") || key.startsWith("account:")) return;
        setSelectionCleared(key === "");
        setSelectedKey(key);
      }}
      style={variant === "inline" ? styles.keysTableInline : styles.keysTable}
    />;
  // Each inline editor row stacks its label above the control; the provided
  // key's copy button shares the 密钥值 label line instead of adding a row,
  // and the custom key's copy rides its own 密钥值 field.
  const keysEditorField = (key: string, label: string, control: React.ReactNode, accessory?: React.ReactNode): React.JSX.Element => (
    <View key={key} style={styles.keysEditorField}>
      <View style={styles.keysEditorFieldHeader}>
        <Text numberOfLines={1} style={styles.keysEditorFieldLabel}>{label}</Text>
        {accessory ?? null}
      </View>
      {control}
    </View>
  );
  const keysEditorView = <View style={variant === "inline" ? styles.keysEditorInline : styles.keysEditor}>
      {selectedCustom ? <>
        {keysEditorField("key-name", translate("providers.keyName"), <TextField
          key={`custom-key-name:${providerId}:${selectedCustom.id}`}
          labelVisible={false}
          label={translate("providers.keyName")}
          value={drafts?.providerKeyDisplayName(providerId, selectedCustom.id, selectedCustom.name) ?? selectedCustom.name}
          disabled={busy}
          onDraftChange={(value) => drafts?.setProviderKeyNameDraft(providerId, selectedCustom.id, value)}
          onCommit={(name) => {
            if (!name || name === selectedCustom.name) return;
            pendingCustomKeyRename.current = { id: selectedCustom.id, name };
            void dispatch("provider.key_patch", { provider_id: providerId, old_name: selectedCustom.name, name }).then(() => {
              // A rename Core refused leaves the key under its old name: the
              // marker has no pending selection left to make.
              if (pendingCustomKeyRename.current?.id === selectedCustom.id && pendingCustomKeyRename.current?.name === name) {
                pendingCustomKeyRename.current = undefined;
              }
            });
          }}
        />)}
        {keysEditorField("key-value", translate("providers.keyValue"), <View style={styles.keysEditorValueRow}>
          <View style={styles.keysEditorValueField}><NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")} hint={booleanValue(selectedCustom.configured) ? translate("providers.apiKeySavedHint") : translate("providers.apiKeyInput")} busy={busy} domain="providers_models" field="api_key" target={`${providerId}\u001f${selectedCustom.name}`} onSecretState={onSecretState} /></View>
          <NativeButton title="" symbol="copy" compact disabled={controlsBusy || !booleanValue(selectedCustom.configured)} toolTip={translate("relay.apiKeyCopy")} accessibilityLabel={translate("relay.apiKeyCopy")} onPress={() => {
            void (async () => {
              try {
                // A copy that worked says nothing: only a failure is worth a
                // word, and even that leaves every button where it was.
                const copied = await native.copySecret({ domain: "providers_models", field: "api_key", target: `${providerId}\u001f${selectedCustom.name}` });
                if (!copied) onStatus?.(translate("relay.operationFailed"));
              } catch {
                onStatus?.(translate("relay.operationFailed"));
              }
            })();
          }} style={styles.panelActionButton} />
        </View>)}
      </> : selectedProvided ? <>
        {/* Relay keys match the custom key editor: name + value only. */}
        {keysEditorField("key-name", translate("providers.keyName"), <TextField
          key={`provided-key-name:${selectedProvided.key}`}
          labelVisible={false}
          label={translate("providers.keyName")}
          value={selectedProvidedName}
          disabled={controlsBusy || selectedProvided.account.autoGrouping}
          onDraftChange={(value) => setProvidedNameDrafts((current) => ({ ...current, [selectedProvided.key]: value }))}
          onCommit={(value) => {
            const name = value.trim();
            if (!name || name === selectedProvided.resource.name || selectedProvided.account.autoGrouping) return;
            void runProvidedAction(() => relay.apiKeyActions.update?.(selectedProvided.account.id, selectedProvided.resource.id, name) ?? Promise.resolve(), "relay.apiKeyUpdateStaged");
          }}
        />)}
        {keysEditorField("key-value", translate("providers.keyValue"), <NativeSecretField
          labelVisible={false}
          plainText
          autoCommit
          disabled
          label={translate("providers.keyValue")}
          hint={selectedProvided.resource.keyHint ? translate("providers.apiKeySavedHint") : translate("common.none")}
          busy={controlsBusy}
          domain="relay_accounts"
          field="api_key"
          target={`${selectedProvided.account.id}:${selectedProvided.resource.id}`}
          onSecretState={onSecretState}
        />, <NativeButton title="" symbol="copy" compact disabled={controlsBusy || !selectedProvided.resource.keyHint} toolTip={translate("relay.apiKeyCopy")} accessibilityLabel={translate("relay.apiKeyCopy")} onPress={() => {
          void (async () => {
            try {
              // A copy that worked says nothing: only a failure is worth a word.
              const copied = await native.copySecret({ domain: "relay_accounts", field: "api_key", target: `${selectedProvided.account.id}:${selectedProvided.resource.id}` });
              if (!copied) onStatus?.(translate("relay.operationFailed"));
            } catch {
              onStatus?.(translate("relay.operationFailed"));
            }
          })();
        }} style={styles.panelActionButton} />)}
        {selectedProvidedIssue ? <Text style={styles.fieldHint}>{relayBindingIssueText(selectedProvidedIssue, selectedProvided.label, translate)}</Text> : null}
      </> : null}
    </View>;
  // 新建密钥 creates on the account the user is looking at: this pane's key
  // list merges every account of the provider's station, so the selected
  // key's own account is the one whose groups the dialog offers and whose
  // token the create writes.  Only an empty selection falls back to the first
  // account, the same fallback the first row already provides.
  const createKeyAccount = selectedProvided?.account ?? stationAccounts[0];
  const dialogs = <>
      <ApiKeyCreateDialog
        visible={createOpen}
        groups={createKeyAccount?.groups.filter((group) => group.id !== "") ?? []}
        disabled={controlsBusy}
        onClose={() => setCreateOpen(false)}
        onCreate={(options) => {
          setCreateOpen(false);
          const account = createKeyAccount;
          if (!account) return;
          void runProvidedAction(() => relay.apiKeyActions.create?.(account.id, options) ?? Promise.resolve(), "relay.apiKeyCreateStaged");
        }}
        translate={translate}
      />
      <DependencyPolicyDialog
        visible={Boolean(remoteDelete)}
        title={translate("relay.apiKeyDeleteImpactTitle")}
        message={remoteDelete ? translate("relay.apiKeyDeleteImpactBody", { count: remoteDelete.resource.linkedModelCount, label: remoteDelete.label }) : ""}
        options={[
          { value: "detach_disabled", label: translate("relay.policyReleaseDisabled"), hint: translate("relay.policyReleaseDisabledHint") },
          { value: "delete_models", label: translate("relay.policyDeleteModels"), hint: translate("relay.policyDeleteModelsHint") },
          { value: "detach_only", label: translate("relay.apiKeyDetachOnly"), hint: translate("relay.apiKeyDetachOnlyHint") },
        ]}
        value={remoteDeletePolicy}
        disabled={controlsBusy}
        confirmLabel={remoteDeletePolicy === "detach_only" ? translate("screen.confirm") : translate("common.delete")}
        destructive={remoteDeletePolicy !== "detach_only"}
        onValueChange={setRemoteDeletePolicy}
        onClose={() => setRemoteDelete(undefined)}
        onConfirm={() => {
          if (!remoteDelete) return;
          const { account, resource } = remoteDelete;
          setRemoteDelete(undefined);
          void runProvidedAction(
            () => (remoteDeletePolicy === "detach_only"
              ? relay.apiKeyActions.detach?.(account.id, resource.id)
              : relay.apiKeyActions.remove?.(account.id, resource.id, remoteDeletePolicy)) ?? Promise.resolve(),
            remoteDeletePolicy === "detach_only" ? "relay.apiKeyDetachStaged" : "relay.apiKeyDeleteStaged",
          );
        }}
        translate={translate}
      />
    </>;
  if (variant === "inline") {
    return <View style={styles.keysInline}>
      <View style={styles.panelHeader}>
        <Text style={styles.panelTitle}>{translate("providers.keys")}</Text>
        <View style={styles.panelActions}>{toolbar}</View>
      </View>
      {tableRows.length > 0 ? <View style={styles.keysInlineBody}>
        {keysTable}
        {keysEditorView}
      </View> : keysEditorView}
      {dialogs}
    </View>;
  }
  return <TablePane style={[styles.keysPane]} title={translate("providers.keys")} actions={toolbar}>
    {tableRows.length > 0 ? keysTable : null}
    {keysEditorView}
    {dialogs}
  </TablePane>;
}

function TablePane({ title, actions, wide, style, children }: { title: string; actions: React.ReactNode; wide?: boolean; style?: StyleProp<ViewStyle>; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.tablePane, compactStyles.tablePane, wide && styles.tablePaneWide, style]}><View style={[styles.tableTitleRow, compactStyles.tableTitleRow]}><Text style={styles.tableTitle}>{title}</Text><View style={[styles.tableActions, compactStyles.inlineGap]}>{actions}</View></View>{children}</View>;
}

/**
 * The scrolling surface every settings pane uses.  The native indicator keeps a
 * scroller on the pane only while its content actually overflows, drawn as the
 * app's persistent translucent scroller instead of AppKit's overlay bar that
 * fades out after a scroll, and horizontal scrolling is left to scroll views
 * that own their own horizontal affordance.
 */
const PersistentScrollView = React.forwardRef<ScrollView, ScrollViewProps & { onViewportHeightChange?: (height: number) => void; onContentHeightChange?: (height: number) => void }>(function PersistentScrollView({ style, contentContainerStyle, children, onViewportHeightChange, onContentHeightChange, onLayout, onContentSizeChange, ...props }, ref): React.JSX.Element {
  return <ScrollView
    {...props}
    ref={ref}
    style={style}
    contentContainerStyle={contentContainerStyle}
    showsHorizontalScrollIndicator={false}
    onLayout={(event) => { onViewportHeightChange?.(event.nativeEvent.layout.height); onLayout?.(event); }}
    onContentSizeChange={(width, height) => { onContentHeightChange?.(height); onContentSizeChange?.(width, height); }}
  >
    {children}
    <NativePersistentScrollIndicator style={styles.persistentScrollIndicator} />
  </ScrollView>;
});

// One public model's own settings: the client-facing name, the limits its
// routes declare, and the order numbers of those routes.
type RouteGroupEntry = {
  key: string;
  deploymentID: string;
  publicModel: string;
  provider: UnknownRecord;
  model: UnknownRecord;
};

function PublicModelInspector({ group, modelContexts, backLabel, busy, translate, dispatch, dispatchSnapshot, onBackToModel, onRenamed }: { group: { name: string; entries: RouteGroupEntry[] }; modelContexts: UnknownRecord; backLabel?: string; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchSnapshot: (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<CoreSnapshot | undefined>; onBackToModel?: () => void; onRenamed: (next: string) => void }): React.JSX.Element {
  const providers = useMemo(() => group.entries.map((entry) => entry.provider), [group.entries]);
  // The group's own value is the smallest one its routes declare, so the field
  // shows what every route can honour and a commit writes it to all of them.
  const customContext = publicModelCustomLimit(providers, group.name, "max_input_tokens");
  const defaultContext = publicModelDefault(modelContexts, group.name);
  // The routes table is a list view read dozens of rows at a time, so this pane
  // states the window and nothing else: the number is a declaration handed to
  // Codex, it enforces nothing here, and a registry escape hatch buried in a
  // high-frequency scan reads as a proxy limit the proxy never applies.  The
  // per-model editor lives in the model detail, one click from either entrance.
  const resolvedContext = customContext !== undefined ? customContext : defaultContext;
  return <View style={styles.inspectorContent}>
    {/* The provider editor's own header shape: the title takes the row and the
        return link sits on the trailing edge, never in front of the title.  A
        route count beside the title is not part of it — the table beside the
        pane already lists those rows, and the number only squeezed the name the
        pane is editing out of it.  The return link states its action in its own
        two words and names the model it returns to in its tooltip: the pane is
        290 pt wide, and the full sentence as a label took the width the title
        needs for the record it shows. */}
    <View style={styles.providerEditorHeader}>
      <Text numberOfLines={1} style={styles.providerEditorHeading}>{group.name}</Text>
      {backLabel && onBackToModel ? <NativeButton title={translate("providers.backToModelShort")} toolTip={translate("providers.backToModel", { model: backLabel })} link disabled={busy} onPress={onBackToModel} style={styles.providerReturnToModel} /> : null}
    </View>
    <View style={styles.inspectorDivider} />
    <View style={styles.inspectorBody}>
      <TextField label={translate("providers.publicModel")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} value={group.name} disabled={busy} onCommit={(next) => {
        const renamed = next.trim();
        if (!renamed || renamed === group.name) return undefined;
        return dispatchSnapshot("public.model_patch", { public_model: group.name, changes: { name: renamed } }).then((snapshot) => { if (snapshot) onRenamed(renamed); });
      }} />
      {resolvedContext !== undefined ? <View style={styles.formRow}>
        <Text numberOfLines={1} style={styles.modelInspectorLabel}>{translate("providers.contextWindow")}</Text>
        <Text numberOfLines={1} style={styles.modelWindowText}>{publicModelTokensText(resolvedContext, translate)}</Text>
      </View> : null}
    </View>
  </View>;
}

function ModelInspector({ providers, providerLabels, provider, providerId, model, modelName, relaySources, native, busy, translate, dispatch, probe, probing, probeResult, modelContexts, bindingIssue, onNameDraftChange, onProviderClick, onProviderChange, onOpenPublicModel, dispatchSnapshot }: { providers: UnknownRecord[]; providerLabels: string[]; provider: UnknownRecord; providerId: string; model: UnknownRecord; modelName: string; relaySources: RelaySourceOption[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; probe: () => void; probing: boolean; probeResult?: IpcResults["probe"] | null; modelContexts?: UnknownRecord; bindingIssue?: UnknownRecord; onNameDraftChange?: (name: string) => void; onProviderClick: () => void; onProviderChange: (providerId: string) => void; onOpenPublicModel?: () => void; dispatchSnapshot: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined> }): React.JSX.Element {
  const id = editorIdentifier(model);
  // A service's own catalog states each model's credit rate; the rate is shown
  // beside the route it belongs to, so the picker's `x0.79` stays visible here.
  const service = providerService(provider);
  const upstreamModelID = stringValue(model.upstream_model).trim();
  const [serviceRate, setServiceRate] = useState<string>(() => peekServiceRate(service, upstreamModelID) ?? "");
  useEffect(() => {
    if (!service || !upstreamModelID) {
      setServiceRate("");
      return;
    }
    // Switching models remounts this pane, so it paints the cached rate in the
    // same frame; only a cold catalog fills in a moment later, and the row
    // itself is always mounted so nothing below it moves.
    const cached = peekServiceRate(service, upstreamModelID);
    if (cached !== undefined) {
      setServiceRate(cached);
      return;
    }
    setServiceRate("");
    let cancelled = false;
    // `dispatch` hands back the snapshot this action produced, so the catalog
    // travels with the action that asked for it.  Reading the rate is this
    // row's own wait — it reaches the upstream, and for a WorkBuddy route the
    // desktop app's live catalog — so it never holds the pane-wide wait.
    void serviceModelRate(service, (providerId) => dispatchSnapshot("workbuddy_models", { provider: providerId }, "providers_models", true)).then((rates) => {
      if (!cancelled) setServiceRate(rates[upstreamModelID] ?? "");
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [service, upstreamModelID]);
  const providerIndex = providers.findIndex((item) => editorIdentifier(item) === providerId);
  const providerLabel = providerLabels[Math.max(0, providerIndex)] ?? "";
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const upstreamName = drafts?.modelUpstreamDisplay(providerId, model) ?? upstreamModelLabel(model);
  const upstreamFieldValue = upstreamName;
  const providerBaseUrl = drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base));
  // The editable field shows the model's real value: the localized placeholder
  // only labels the list row and the breadcrumb, so the pane never looks like a
  // name is set while validation still reports it missing.
  const modelFieldName = stringValue(model.model_name, stringValue(model.name));
  // The public model's client-facing context window: the value the user
  // declared wins, otherwise the registry default the managed catalog
  // resolves.  This is where the window is edited — the routes table's
  // public-model pane states it read-only, because that list view is scanned
  // dozens of rows at a time and an escape hatch there reads as a proxy limit.
  const publicModelName = modelFieldName.trim();
  const publicModelWindow = publicModelDefault(modelContexts ?? {}, publicModelName);
  const customWindow = publicModelCustomLimit(providers, publicModelName, "max_input_tokens");
  const [windowTipOpen, setWindowTipOpen] = useState(false);
  const windowHint = translate("providers.publicModelDefaultHint", { value: publicModelTokensText(publicModelWindow, translate) });
  const keyStates = providerAuthKind(provider) === "api_key" ? providerKeyStates(provider) : [];
  const selectedProviderKey = keyStates.find((key) => key.id === stringValue(model.provider_key_id))
    ?? keyStates.find((key) => key.name === stringValue(model.api_key_name));
  const usesRelayKey = selectedProviderKey?.source.kind === "relay";
  const relayMultiplier = relaySourceForKey(selectedProviderKey, relaySources)?.multiplier;
  const canFollowMultiplier = usesRelayKey && relayMultiplier !== undefined;
  const manualOrder = numberValue(model.manual_order, modelEffectiveOrder(model));
  const followsMultiplier = canFollowMultiplier && modelOrderMode(model) === "relay_multiplier";
  const displayedOrder = followsMultiplier
    ? String(relayMultiplier ?? modelEffectiveOrder(model))
    : drafts?.modelOrderText(providerId, model) ?? String(manualOrder);
  const matchingRelaySources = providerAuthKind(provider) === "api_key" ? relaySourcesForBaseUrl(providerBaseUrl, relaySources) : [];
  const persistedRelaySourceIDs = new Set(keyStates.filter((key) => key.source.kind === "relay").map((key) => relaySourceSelectionID(key.source)));
  const providerKeyOptions = [...keyStates.map((key) => ({
    value: key.id,
    label: providerKeyChoiceLabel({ name: drafts?.providerKeyDisplayName(providerId, key.id, key.name) ?? key.name, kind: key.source.kind, source: relaySourceForKey(key, relaySources) }, translate),
  })), ...matchingRelaySources.filter((source) => !persistedRelaySourceIDs.has(relaySourceSelectionID(source))).map((source) => ({
    value: `relay:${relaySourceSelectionID(source)}`,
    label: providerKeyChoiceLabel({ name: source.resourceLabel, kind: "relay", source }, translate),
  }))];
  // The finding shares one line with the enable checkbox and the probe button.
  // It is the row's only flexible cell: the text takes the room that is left,
  // ellipsizes what does not fit, and its hover hint keeps the whole sentence.
  const probePresentation = modelProbePresentation(model, probeResult, translate);
  // The deep test names itself before it runs: a Responses route whose name the
  // staged degradation engine can attribute adds the fingerprint probe, every
  // other route keeps the plain availability probe.
  const degradationIncluded = booleanValue(asRecord(model.deep_probe).includes_degradation);
  // The probe button keeps its own name while it runs: the button carries the
  // progress as a leading spinner instead of renaming itself.
  const probeTitle = degradationIncluded
    ? translate("providers.deepTest")
    : translate("providers.probe");
  // Probing a route is how its credential gets verified, so the pane does not
  // refuse the press on its own belief about that credential: the probe answers
  // with the truth (`providers.probeInlineInvalidConfig` when there is nothing
  // to try), and a route whose key Core just materialized can be checked at
  // once instead of staying grayed out on a belief the user cannot change from
  // here.  Only an account-backed provider that has not signed in has no route
  // to probe at all, and that state belongs to the provider, not to one model.
  const probeReady = Boolean(
    providerBaseUrl.trim()
    && upstreamName.trim()
    && (providerAuthKind(provider) === "api_key" || providerAuthStatus(provider) === "signed_in"),
  );
  // A linked key the relay could not resolve says so here, on the route that
  // failed, naming the key in the same `账号/分组` form the picker uses.
  const bindingIssueText = bindingIssue
    ? relayBindingIssueText(
      bindingIssue,
      selectedProviderKey
        ? providerKeyChoiceLabel({ name: selectedProviderKey.name, kind: selectedProviderKey.source.kind, source: relaySourceForKey(selectedProviderKey, relaySources) }, translate)
        : providerLabel,
      translate,
    )
    : "";
  const openProbeDetails = (): void => {
    if (!probePresentation.full) return;
    void native.showReadOnlyText({
      title: translate("providers.probeDetails"),
      text: probePresentation.full,
      closeLabel: translate("status.close"),
      language: "text",
      html: readOnlyCodeEditorHtml(editorMenuLabels(translate)),
    }).catch(() => undefined);
  };
  const selectProviderKey = (providerKeyID: string): void => {
    if (!providerKeyID) {
      // Back to the provider's default key: drop the model's own binding
      // instead of pinning it to whichever key happens to be first.
      void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { provider_key_id: "", api_key_name: "" } });
      return;
    }
    if (providerKeyID.startsWith("relay:")) {
      const sourceID = providerKeyID.slice("relay:".length);
      const source = matchingRelaySources.find((candidate) => relaySourceSelectionID(candidate) === sourceID);
      if (!source) return;
      void dispatch("model.select_relay_resource", {
        provider_id: providerId,
        model_id: id,
        station_id: source.stationID,
        account_id: source.accountID,
        resource_id: source.resourceID,
      });
      return;
    }
    const providerKey = keyStates.find((key) => key.id === providerKeyID);
    if (!providerKey) return;
    const providerKeyName = drafts?.providerKeyDisplayName(providerId, providerKey.id, providerKey.name) ?? providerKey.name;
    void dispatch("model.patch", {
      provider_id: providerId,
      model_id: id,
      changes: { provider_key_id: providerKey.id, api_key_name: providerKeyName },
    });
  };
  return <View style={styles.inspectorContent}>
    <View style={styles.modelBreadcrumb}><NativeButton title={providerLabel} link disabled={busy} onPress={onProviderClick} style={styles.breadcrumbProvider} /><Text style={styles.breadcrumbSeparator}>&gt;</Text>{onOpenPublicModel ? <NativeButton title={displayLabel(modelName, translate("providers.unnamedModel"))} link disabled={busy || !publicModelName} onPress={() => onOpenPublicModel?.()} style={styles.breadcrumbProvider} /> : <Text numberOfLines={1} style={styles.inspectorHeading}>{displayLabel(modelName, translate("providers.unnamedModel"))}</Text>}</View>
    <View style={styles.inspectorDivider} />
    <View style={styles.inspectorBody}>
      <View style={styles.inspectorEnabledRow}><View style={styles.inspectorEnableControl}><NativeCheckbox label={translate("common.enable")} value={booleanValue(model.model_enabled, booleanValue(model.enabled, true))} disabled={busy} onValueChange={(model_enabled) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { model_enabled } })} /></View><ActionButton title={probeTitle} titleWidth="tight" busy={probing} toolTip={(probePresentation.compact ? probePresentation.tooltip : "") || (degradationIncluded ? translate("providers.deepTestHint") : undefined)} disabled={(busy && !probing) || !probeReady} onPress={probe} />{probePresentation.compactSentence ? <Pressable style={styles.inspectorProbeFinding} onPress={openProbeDetails} accessibilityRole="link" accessibilityLabel={probePresentation.compactSentence}><TooltipText numberOfLines={1} ellipsizeMode="tail" tooltip={probePresentation.tooltip} style={styles.inspectorProbeFindingText}>{probePresentation.compactSentence}</TooltipText></Pressable> : null}</View>
      <TextField label={translate("providers.publicModel")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} value={modelFieldName} onDraftChange={onNameDraftChange} onCommit={(name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { name } })} />
      <PickerField label={translate("providers.provider")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink value={providerLabel} values={providerLabels} disabled={busy || providers.length <= 1} onSelect={(label) => { const next = providers[providerLabels.indexOf(label)]; if (next) onProviderChange(editorIdentifier(next)); }} />
      {isDraftModel(model, translate) ? <Text style={styles.fieldHint}>{translate(selectedProviderKey ? "providers.draftModelHint" : "providers.draftModelKeylessHint")}</Text> : null}
      {providerKeyOptions.length > 0 ? <PickerField label={translate("providers.providerKey")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink value={selectedProviderKey?.id ?? ""} values={[{ value: "", label: translate("providers.undefinedKey") }, ...providerKeyOptions]} disabled={busy} onSelect={selectProviderKey} /> : null}
      {bindingIssueText ? <Text style={styles.fieldHint}>{bindingIssueText}</Text> : null}
      {service ? <View style={styles.formRow}>
        <Text numberOfLines={1} style={styles.modelInspectorLabel}>{translate("providers.modelRate")}</Text>
        <Text numberOfLines={1} style={styles.providerAuthStatusValue}>{serviceRate.trim() || translate("common.none")}</Text>
      </View> : null}
      <TextField label={translate("providers.upstream")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} value={upstreamFieldValue} onDraftChange={(value) => drafts?.setModelUpstreamDraft(providerId, id, value)} onCommit={(upstream_model) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_model } })} />
      <View style={styles.orderEditorRow}>
        <TextField label={translate("providers.order")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} controlWidth={64} value={String(displayedOrder)} keyboardType="numeric" disabled={busy || followsMultiplier} onDraftChange={(value) => drafts?.setModelOrderDraft(providerId, id, value)} validate={(nextOrder) => {
          // The order is a number the user typed (decimals included).  A draft
          // that is not one stays in the field with its own message instead of
          // silently becoming 0, which is the rule the runtime panes' number
          // fields already follow.
          const trimmed = nextOrder.trim();
          return trimmed === "" || Number.isFinite(Number(trimmed)) ? undefined : translate("runtime.invalidNumber");
        }} onCommit={(nextOrder) => {
          const parsed = Number(nextOrder.trim());
          const order = Number.isFinite(parsed) ? parsed : 0;
          return dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { manual_order: order, order } });
        }} style={styles.orderEditorField} />
        {canFollowMultiplier ? <NativeCheckbox label={translate("providers.followMultiplier")} value={followsMultiplier} disabled={busy} onValueChange={(follow) => dispatch("model.patch", {
          provider_id: providerId,
          model_id: id,
          changes: {
            order_mode: follow ? "relay_multiplier" : "manual",
            manual_order: manualOrder,
            ...(follow ? {} : { order: manualOrder }),
          },
        })} style={styles.orderFollowControl} /> : null}
      </View>
      <ProtocolPicker providerId={providerId} model={model} busy={busy} translate={translate} dispatch={dispatch} />
      {/* The window is the pane's last row: it is a per-model escape hatch for
          a registry that resolved this route wrong, not part of how the route
          is called, so it sits below 协议方式 instead of splitting the calling
          fields apart. */}
      {publicModelName ? <TextField label={translate("providers.contextWindow")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} value={customWindow !== undefined ? String(customWindow) : ""} placeholder={windowHint} keyboardType="numeric" disabled={busy} onCommit={(next) => dispatch("public.model_patch", { public_model: publicModelName, changes: { max_input_tokens: next.trim() } })} accessory={<HelpTip open={windowTipOpen} text={translate("providers.contextWindowHelp")} title={translate("providers.contextWindowHelpTitle")} onToggle={() => setWindowTipOpen((current) => !current)} />} /> : null}
    </View>
  </View>;
}

// The protocol-mode sentence is a tip, not a standing row: the picker keeps a
// clickable question mark beside it, and the tip opens over the pane when it is
// asked for. The mark is the platform's own help glyph — an SF Symbol on macOS,
// the MDL2 glyph on Windows — drawn as a quiet icon-only link, so it is neither
// a button bezel nor a text mark, and the picker keeps its 60 pt label column
// intact. The panel is anchored upward because these rows sit at the bottom of
// the model inspector, where a panel opening downward would fall outside the
// workspace.
function HelpTip({ open, text, title, onToggle }: { open: boolean; text: string; title: string; onToggle: () => void }): React.JSX.Element {
  return <View style={styles.helpTipAnchor}>
    {open ? <Pressable accessible={false} onPress={onToggle} style={styles.helpTipDismiss} /> : null}
    <NativeButton title="" symbol="help" link plainLink toolTip={title} accessibilityLabel={title} onPress={onToggle} style={styles.helpTipButton} />
    {open ? <View style={styles.helpTipPopup}><Text style={styles.helpTipText}>{text}</Text></View> : null}
  </View>;
}

function ProtocolPicker({ providerId, model, busy, translate, dispatch }: { providerId: string; model: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch }): React.JSX.Element {
  const id = editorIdentifier(model);
  const mode = stringValue(model.upstream_protocol_mode, "fallback");
  const protocol = stringValue(model.upstream_url_surface, "openai/chat");
  const [tipOpen, setTipOpen] = useState(false);
  const options: AssistantSettingOption[] = [
    { value: "openai/responses", label: translate("providers.responses") },
    { value: "openai/chat", label: translate("providers.chat") },
    { value: "anthropic", label: translate("providers.anthropic") },
  ];
  const modeOptions: AssistantSettingOption[] = [
    { value: "fallback", label: translate("providers.protocolModeFallback") },
    { value: "fixed", label: translate("providers.protocolModeFixed") },
  ];
  const fixed = mode === "fixed";
  const protocolHint = translate(fixed ? "providers.protocolModeFixedHint" : "providers.protocolModeFallbackHint");
  // Choosing either value answers the question the tip was opened for, so the
  // panel closes with the change instead of lingering over the next row.
  const selectMode = (upstream_protocol_mode: string): void => {
    setTipOpen(false);
    void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_protocol_mode } });
  };
  const selectProtocol = (upstream_url_surface: string): void => {
    setTipOpen(false);
    void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_url_surface } });
  };
  return <View style={styles.protocolSettings}>
    <PickerField label={translate("providers.protocolMode")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink value={fixed ? "fixed" : "fallback"} values={modeOptions} disabled={busy} onSelect={selectMode} />
    <PickerField label={fixed ? translate("providers.fixedProtocol") : translate("providers.fallbackProtocol")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink value={protocol} values={options} disabled={busy} onSelect={selectProtocol} accessory={<HelpTip open={tipOpen} text={protocolHint} title={translate("providers.protocolModeHelp")} onToggle={() => setTipOpen((current) => !current)} />} />
  </View>;
}

function providerRecord(provider: ProviderSummary): UnknownRecord {
  return {
    id: provider.id,
    name: provider.display_name,
    display_name: provider.display_name,
    enabled: provider.enabled,
    endpoint: provider.endpoint,
    model_count: provider.model_count,
    provider_type: provider.provider_type ?? "custom",
    relay_station_id: provider.relay_station_id ?? "",
    auth_kind: provider.auth_kind ?? "api_key",
    auth_status: provider.auth_status ?? "signed_out",
    auth_active: provider.auth_active ?? false,
    key_states: provider.key_states ?? [],
    api_key_names: provider.key_states?.map((key) => key.name) ?? [],
    models: provider.models ?? [],
  };
}

function modelRecord(model: UnknownRecord): UnknownRecord {
  const modelEnabled = booleanValue(model.model_enabled, booleanValue(model.enabled, true));
  const effectiveOrder = numberValue(model.effective_order, numberValue(model.order, 0));
  return {
    ...model,
    id: editorIdentifier(model),
    name: stringValue(model.name, stringValue(model.display_name, stringValue(model.model))),
    display_name: stringValue(model.display_name, stringValue(model.name)),
    enabled: modelEnabled,
    model_enabled: modelEnabled,
    effective_order: effectiveOrder,
    order: effectiveOrder,
    manual_order: numberValue(model.manual_order, effectiveOrder),
  };
}

// A staged model that cannot materialize a route yet.  Without a public name
// (or an upstream model) Core rejects Apply, so the shared pane marks the row
// where the model is edited instead of only reporting that the draft is invalid.
/**
 * A model ＋ created and not finished yet: still carrying the placeholder name
 * it was born with.  Its fields say what is left instead of reporting an
 * incomplete new record as an error.
 */
function isDraftModel(model: UnknownRecord, translate: Translate): boolean {
  const name = stringValue(model.model_name, stringValue(model.name)).trim();
  if (!name) return false;
  return name === translate("providers.newModel") || name.startsWith(`${translate("providers.newModel")} `);
}

// A row the pane marks: an enabled route that cannot materialize (no name or
// no upstream), or a freshly created row that still carries the placeholder
// name — it is enabled, so the pane says it is not finished rather than
// letting the placeholder read as a real public model.
function modelNeedsAttention(model: UnknownRecord, translate: Translate): boolean {
  if (!booleanValue(model.model_enabled, booleanValue(model.enabled, true))) return false;
  const name = stringValue(model.model_name).trim() || stringValue(model.name).trim();
  if (!name) return true;
  if (isDraftModel(model, translate)) return true;
  return !(stringValue(model.litellm_model).trim() || stringValue(model.upstream_model).trim());
}

function isProbeSurface(value: string): value is "openai/responses" | "openai/chat" | "anthropic" {
  return value === "openai/responses" || value === "openai/chat" || value === "anthropic";
}

function modelProbeKey(providerId: string, modelId: string): string {
  return `${providerId}\x1f${modelId}`;
}

/** The map without one entry, unchanged when it never held it: a pane that
 * drops a finding must not re-create its state on every render. */
function withoutRecordEntry<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (record[key] === undefined) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

/** The inputs one probe result is evidence for: the address it called, the
 * model name it asked for, the protocol it tried, and the credential slot that
 * answered.  A result measured on other inputs describes a route that no longer
 * exists, so the pane drops it instead of showing it beside the new one.  The
 * address and the model name are the pane's own values, a pending edit
 * included: the finding goes as soon as the field it belongs to is edited. */
function probeInputFingerprint(providerBaseUrl: string, upstreamModel: string, model: UnknownRecord): string {
  return [
    providerBaseUrl,
    upstreamModel,
    stringValue(model.litellm_model),
    stringValue(model.api_base),
    stringValue(model.provider_key_id, stringValue(model.api_key_name)),
    stringValue(model.upstream_protocol_mode, "fallback"),
    stringValue(model.upstream_url_surface),
    booleanValue(model.api_key_configured) ? "1" : "0",
  ].join("\x1f");
}

function providerModelsByEditorId(snapshot: CoreSnapshot, providerId: string): UnknownRecord[] {
  const state = domainState(snapshot, "providers_models");
  const providers = asRecords(state.providers);
  const provider = providers.find((item) => editorIdentifier(item) === providerId);
  return provider ? asRecords(provider.models) : [];
}

function providerModelByEditorId(snapshot: CoreSnapshot, providerId: string, modelId: string): UnknownRecord | undefined {
  return providerModelsByEditorId(snapshot, providerId).find((item) => editorIdentifier(item) === modelId);
}

function upstreamModelLabel(model: UnknownRecord): string {
  const value = stringValue(model.upstream_model, stringValue(model.litellm_model));
  const separator = value.indexOf("/");
  return separator >= 0 ? value.slice(separator + 1) : value;
}

// The public-model row key the routes table uses for one public model.  The
// name itself is the selection identity, so the key prefix only marks the
// row kind.
const ROUTE_PUBLIC_MODEL_PREFIX = "route-public-model:";

function routePublicModelRowKey(publicModel: string): string {
  return `${ROUTE_PUBLIC_MODEL_PREFIX}${publicModel}`;
}

function publicModelTokensText(value: number | null | undefined, translate: Translate): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value <= 0) return translate("common.none");
  return Math.round(value).toLocaleString("en-US");
}

// One public model's declared limit: the smallest value its routes carry, so a
// group never promises more than every route behind it can deliver.  Absent
// means the registry default stands.
function publicModelCustomLimit(providers: UnknownRecord[], publicModel: string, field: "max_input_tokens"): number | undefined {
  const name = publicModel.trim();
  if (!name) return undefined;
  const values: number[] = [];
  for (const provider of providers) {
    for (const raw of asRecords(provider.models)) {
      const model = modelRecord(raw);
      if (stringValue(model.model_name, stringValue(model.name)).trim() !== name) continue;
      const value = numberValue(model[field], 0);
      if (value > 0) values.push(value);
    }
  }
  return values.length > 0 ? Math.min(...values) : undefined;
}

function publicModelDefault(contexts: UnknownRecord, publicModel: string): number | undefined {
  const record = asRecord(contexts[publicModel]);
  return numberValue(record.context_window, 0) || undefined;
}

function identifier(record: UnknownRecord): string {
  return stringValue(record.id, stringValue(record.editor_id, stringValue(record.name, "new-item")));
}

function editorIdentifier(record: UnknownRecord): string {
  return stringValue(record.editor_id, identifier(record));
}

function providerNameExists(providers: UnknownRecord[], name: string, excludeID = ""): boolean {
  const normalized = name.trim().toLocaleLowerCase();
  if (!normalized) return false;
  return providers.some((entry) => editorIdentifier(entry) !== excludeID
    && stringValue(entry.display_name, stringValue(entry.name)).trim().toLocaleLowerCase() === normalized);
}

// A name derived from a provider's URL must not collide with an existing
// provider: Core rejects the duplicate, so the suffix walks up 2, 3, … until
// the name is free.
function uniqueProviderName(providers: UnknownRecord[], name: string, excludeID = ""): string {
  let candidate: string = name;
  let suffix = 2;
  while (providerNameExists(providers, candidate, excludeID)) {
    candidate = `${name}${suffix}`;
    suffix += 1;
  }
  return candidate;
}

function ProviderSourceFields({ provider, providerID, relayStations, busy, translate, dispatch, onStatus, onBaseUrlDraftChange, onNameDraftChange }: { provider: UnknownRecord; providerID: string; relayStations: RelayStationOption[]; busy: boolean; translate: Translate; dispatch: Dispatch; onStatus?: (status?: string) => void; onBaseUrlDraftChange?: (baseURL: string) => void; onNameDraftChange?: (name: string) => void }): React.JSX.Element {
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const [sourceResetToken, setSourceResetToken] = useState(0);
  const providerName = drafts?.providerDisplayName(provider) ?? stringValue(provider.name, stringValue(provider.display_name));
  const providerBaseURL = drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base));
  // There is no explicit source switcher: a base URL that matches a station
  // binds to it automatically, and the always-present 中转站关联
  // section carries the account workflow.
  const commitBaseURL = (endpoint: string): void | Promise<void> => {
    const station = relayStationForBaseUrl(endpoint, relayStations);
    if (station) {
      if (providerNameExists(drafts?.providers ?? [], station.name, providerID)) {
        // The bind needs this provider to take the station's name, and Core
        // refuses a duplicate name.  The field goes back to its previous value,
        // so the cause is stated instead of looking like a lost keystroke.
        onStatus?.(translate("providers.relayNameTaken", { name: station.name }));
        onBaseUrlDraftChange?.("");
        onNameDraftChange?.("");
        setSourceResetToken((value) => value + 1);
        return;
      }
      return dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id });
    }
    // A relay provider whose station is gone keeps its dangling source, and
    // the Core rejects direct URL/name edits on relay providers.  Editing the
    // address therefore turns it back into a plain custom provider.
    const changes: UnknownRecord = providerKind(provider) === "relay" ? { provider_type: "custom", endpoint } : { endpoint };
    // The name follows the first URL the provider is given: derive it from the
    // host while the provider still has no address, and leave it alone after
    // that so a later edit cannot overwrite a name the user chose.
    const previousBaseURL = stringValue(provider.endpoint, stringValue(provider.api_base));
    if (!previousBaseURL.trim() && endpoint.trim()) {
      const suggested = suggestedProviderName(endpoint);
      if (suggested && suggested.toLocaleLowerCase() !== providerName.trim().toLocaleLowerCase()) {
        const name = uniqueProviderName(drafts?.providers ?? [], suggested, providerID);
        changes.name = name;
        onNameDraftChange?.(name);
      }
    }
    return dispatch("provider.patch", { provider_id: providerID, changes });
  };
  return <View style={styles.providerSourceFields}>
    <TextField key={"provider-base-url:" + sourceResetToken} label={translate("providers.baseUrl")} labelWidth={88} value={providerBaseURL} disabled={busy} onDraftChange={onBaseUrlDraftChange} onCommit={commitBaseURL} />
    <TextField key={"provider-name:" + sourceResetToken} label={translate("providers.providerName")} labelWidth={88} value={providerName} disabled={busy} onDraftChange={onNameDraftChange} onCommit={(name) => dispatch("provider.patch", { provider_id: providerID, changes: providerKind(provider) === "relay" ? { provider_type: "custom", name } : { name } })} />
  </View>;
}

function ProviderEditor({ provider, relaySources, relayStations, native, busy, translate, dispatch, dispatchWithOutcome, onSecretState, onNameDraftChange, sourceModel, onReturnToModel, station, stationAccounts, relay, addOfficialAccount, onActivateAndRestart, onStatus, language, bindingIssues, snapshotForCleanups }: { provider: UnknownRecord; relaySources: RelaySourceOption[]; relayStations: RelayStationOption[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>; onSecretState: (state: SecretState) => void; onNameDraftChange?: (name: string) => void; sourceModel?: UnknownRecord; onReturnToModel: () => void; station?: RelayStation; stationAccounts: RelayAccount[]; relay: RelayWorkspaceBridge; addOfficialAccount: (kind: ServiceProviderKind) => Promise<string>; onActivateAndRestart: () => Promise<boolean>; onStatus: (status?: string) => void; language: "system" | "en" | "zh-Hans"; bindingIssues?: UnknownRecord[]; snapshotForCleanups?: CoreSnapshot }): React.JSX.Element {
  const id = editorIdentifier(provider);
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const kind = providerKind(provider);
  const service = providerService(provider);
  const isOfficialAccount = kind === "openai" || kind === "claude";
  const isWorkBuddyAccount = kind === "workbuddy" || kind === "workbuddyAI";
  const isLogin = isOfficialAccount || isWorkBuddyAccount;
  // Which account action is running, so only the button that started it shows
  // the spinner; every sibling stays disabled while one of them works.
  const [authPending, setAuthPending] = useState<"auth" | "activate" | "sibling">();
  const loginBusy = authPending !== undefined;
  const [relayAddBusy, setRelayAddBusy] = useState(false);
  const [stationDraft, setStationDraft] = useState<StationDraft>({});
  const stationDraftRef = useRef<StationDraft>({});
  stationDraftRef.current = stationDraft;
  const setStationDraftValue = (draft: StationDraft): void => {
    setStationDraft((current) => ({ ...current, ...draft }));
  };
  const stageStationUpdate = async (overrides: StationDraft = {}): Promise<void> => {
    if (!station) return;
    const draft = stationDraftRef.current;
    const name = (overrides.name ?? draft.name ?? stationDisplayName(station, translate)).trim();
    const origin = normalizeRelayOrigin(overrides.origin ?? draft.origin ?? station.origin);
    const type = overrides.type ?? draft.type ?? station.type;
    if (!name || !origin) return;
    const dirty = name !== stationDisplayName(station, translate).trim()
      || origin !== normalizeRelayOrigin(station.origin)
      || type !== station.type;
    if (!dirty) return;
    try {
      await relay.commit("station.update", { id: station.id, name, origin, type });
      await relay.refreshAccounts();
      onStatus?.(translate("relay.stationUpdateStaged"));
    } catch {
      onStatus?.(translate("relay.operationFailed"));
    }
  };
  const shownChallenge = useRef<Record<string, string>>({});
  const providerName = drafts?.providerDisplayName(provider) ?? stringValue(provider.display_name, stringValue(provider.name, translate("providers.newProvider")));
  const sourceModelLabel = sourceModel ? drafts?.modelDisplayName(id, sourceModel) ?? displayLabel(sourceModel.model_name ?? sourceModel.name, translate("providers.unnamedModel")) : "";
  const authStatus = providerAuthStatus(provider);
  const authActive = booleanValue(provider.auth_active);
  const statusLabels: Record<ProviderAuthStatus, string> = {
    signed_out: translate("providers.authStatusSignedOut"),
    authorizing: translate("providers.authStatusAuthorizing"),
    signed_in: translate("providers.authStatusSignedIn"),
    expired: translate("providers.authStatusExpired"),
    error: translate("providers.authStatusError"),
    unsupported: translate("providers.authStatusUnsupported"),
  };
  const model = asRecords(provider.models)[0];
  const modelNameText = model ? stringValue(model.display_name, stringValue(model.name, stringValue(model.upstream_model, translate("common.notAvailable")))) : translate("common.notAvailable");
  useEffect(() => {
    // Opening a WorkBuddy entry reads its desktop-app account and credit once;
    // the section's own 刷新 button re-reads them on demand.
    if (!workbuddyProviderIDFor(kind)) return;
    void loadWorkBuddyAccount(false).then((account) => {
      // A worker that was just started can answer its first read before it has
      // the desktop credential in hand; re-read once so the block never states
      // a stale “not signed in”.
      if (account && stringValue(account.state) !== "signed-in") void loadWorkBuddyAccount(true);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, kind]);
  // A WorkBuddy account is read through the Core-owned worker: the editor
  // links the desktop app's own sign-in here and never asks for a secret.
  const [workbuddyStatusBusy, setWorkbuddyStatusBusy] = useState(false);
  const [workbuddyAccount, setWorkbuddyAccount] = useState<UnknownRecord | undefined>(undefined);
  const loadWorkBuddyAccount = async (refresh: boolean): Promise<UnknownRecord | undefined> => {
    const providerKey = workbuddyProviderIDFor(kind);
    if (!providerKey) return undefined;
    setWorkbuddyStatusBusy(true);
    try {
      // Reading the account is this block's own wait: the name field, the
      // association controls and every other input stay usable throughout.
      const next = await dispatchWithOutcome("workbuddy_status", { refresh }, "providers_models", true);
      if (!next) return undefined;
      const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
      const account = asRecord(asRecord(summary.providers)[providerKey]);
      setWorkbuddyAccount(account);
      return account;
    } finally {
      setWorkbuddyStatusBusy(false);
    }
  };
  // The account is the desktop app's own sign-in, so this block only re-reads
  // it and states what it found; its models come through the key path below.
  const refreshWorkBuddyAccount = async (): Promise<void> => {
    if (workbuddyStatusBusy) return;
    try {
      const account = await loadWorkBuddyAccount(true);
      if (!account) return;
      if (stringValue(account.state) !== "signed-in") {
        onStatus?.(translate("providers.wizard.workbuddySignInRequired"));
        return;
      }
      const credits = workbuddyCreditsText(account, translate);
      onStatus?.(credits ? `${translate("providers.wizard.workbuddyCredits")}: ${credits}` : translate("providers.authStatusSignedIn"));
    } catch {
      onStatus?.(translate("relay.operationFailed"));
    }
  };
  // An account that is not signed in is handed to the desktop app that owns
  // the sign-in: this pane can only open it and then re-read what it wrote.
  const openWorkBuddyApp = async (): Promise<void> => {
    if (workbuddyStatusBusy) return;
    try {
      const providerKey = workbuddyProviderIDFor(kind);
      if (!providerKey) return;
      await dispatchWithOutcome("workbuddy_login", { provider: providerKey }, "providers_models", true);
    } catch {
      // Opening the app is a courtesy: the confirm below still asks whether
      // the user signed in, so a failure here is not reported as one.
    }
    setWorkbuddyStatusBusy(true);
    try {
      const accepted = await native.showConfirmation({
        title: translate("providers.wizard.workbuddyLoginTitle"),
        message: translate("providers.wizard.workbuddyLoginPrompt", { name: workbuddyAppName(kind, translate) }),
        confirmLabel: translate("providers.wizard.workbuddyLoggedIn"),
      });
      if (!accepted) return;
      const account = await loadWorkBuddyAccount(true);
      const state = stringValue(account?.state);
      const credits = workbuddyCreditsText(account, translate);
      if (state === "signed-in") {
        onStatus?.(credits ? `${translate("providers.wizard.workbuddyCredits")}: ${credits}` : translate("providers.authStatusSignedIn"));
      } else {
        onStatus?.(translate("providers.authStatusSignedOut"));
      }
    } finally {
      setWorkbuddyStatusBusy(false);
    }
  };
  const workbuddySignedIn = stringValue(workbuddyAccount?.state) === "signed-in"
    // Until this block has read the account itself, the sign-in state is the
    // one Core last observed rather than a guess: the block may be showing a
    // provider another window just linked, and Core projects its last observed
    // state instead of calling the worker (a cold start here once froze every
    // window). The mount read below fills it in moments after the pane opens.
    || stringValue(asRecord(provider.auth_observed).state) === "signed-in";
  const startLogin = async (): Promise<void> => {
    if (authPending !== undefined) return;
    const kind = providerKind(provider) === "claude" ? "claude_login" : "openai_login";
    delete shownChallenge.current[id];
    setAuthPending("auth");
    try {
      const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: id }, "providers_models");
      presentProviderAuthChallenge(native, translate, next, kind, providerName, id, shownChallenge.current);
    } finally {
      setAuthPending(undefined);
    }
  };
  const authAction = authStatus === "signed_in"
    ? "service_provider.auth_logout"
    : authStatus === "authorizing"
      ? "service_provider.auth_cancel"
      : "service_provider.auth_start";
  const authLabel = authStatus === "signed_in"
    ? translate("relay.officialProviderLogout")
    : authStatus === "authorizing"
      ? translate("relay.officialProviderCancel")
      : translate("relay.officialProviderLogin");
  const runAuthAction = async (): Promise<void> => {
    if (authAction === "service_provider.auth_start") {
      await startLogin();
      return;
    }
    await dispatch(authAction, { provider_id: id }, "providers_models");
  };
  const activateProvider = async (): Promise<void> => {
    if (kind !== "openai") return;
    if (authPending !== undefined) return;
    setAuthPending("activate");
    try {
      const activated = await dispatchWithOutcome("service_provider.auth_activate", { provider_id: id }, "providers_models");
      if (!activated) return;
      try {
        if (await onActivateAndRestart()) onStatus(translate("relay.officialProviderActive"));
      } catch (reason) {
        onStatus(errorMessage(reason, translate));
      }
    } finally {
      setAuthPending(undefined);
    }
  };
  const addSiblingAccount = async (): Promise<void> => {
    if (authPending !== undefined) return;
    setAuthPending("sibling");
    try {
      const newID = await addOfficialAccount(kind === "claude" ? "claude_login" : "openai_login");
      const kindLogin: ServiceProviderKind = kind === "claude" ? "claude_login" : "openai_login";
      const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: newID }, "providers_models");
      presentProviderAuthChallenge(native, translate, next, kindLogin, providerKindLabel(kind, translate), newID, shownChallenge.current);
    } catch (reason) {
      onStatus(errorMessage(reason, translate));
    } finally {
      setAuthPending(undefined);
    }
  };
  // 任意供应商都能加中转账号: use this vendor's base URL as the station
  // origin and run the native webview sign-in first. Core creates the
  // account shell only after sign-in succeeds (pending_account), so a
  // cancelled flow reserves nothing and can no longer cascade an empty
  // station away from its bound provider. After success the workspace
  // auto-binds the provider by origin so the vendor picks up the
  // station's provided keys.
  // The relay sign-in needs the vendor URL first: without one the + has nothing
  // to do, so the header hides it instead of showing it greyed out.
  const vendorBaseURL = (drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base))).trim();
  const addRelayAccountToVendor = async (): Promise<void> => {
    if (relayAddBusy) return;
    const providerBase = drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base));
    const origin = normalizeRelayOrigin(providerBase);
    if (!origin) return;
    // What the sign-in may save is asked after the login completes, inside
    // the native browser flow; no pre-login prompt or checkbox runs here.
    setRelayAddBusy(true);
    const pendingID = `login-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const stationName = suggestedRelayStationName(origin) || origin;
    try {
      // The station family decides which login probes the native flow runs
      // and which session it keeps, so it is resolved before the sign-in the
      // way every other surface resolves it: a station this provider is
      // already bound to states its own type, and a bare address is asked.
      // Guessing one family here would run the wrong probes against the other
      // station and end in a sign-in that can never complete.
      const relayType = station?.type ?? await relay.detectType(origin);
      if (!relayType) {
        onStatus?.(translate("relay.typeNotDetected"));
        return;
      }
      const result = await native.relayLogin({
        accountId: pendingID,
        type: relayType,
        label: origin,
        origin,
        language,
        pendingAccount: true,
        stationName,
        stationType: relayType,
        stationOrigin: origin,
      });
      if (!result) {
        onStatus?.(translate("relay.loginNotCompleted"));
        return;
      }
      // Resource discovery is reported, not assumed: the sign-in succeeded
      // either way, but a station whose keys could not be read says so instead
      // of claiming 登录成功 over an empty key list.
      const resourceStatus = await relay.refreshResources(pendingID, { force: true });
      // Bind this vendor to the station it just signed in to, the way the
      // wizard binds the vendor it creates: that is what makes the station's
      // keys linkable here, and it is why the pane does not depend on the
      // workspace's background rebind (which skips a name collision silently).
      const next = await relay.refreshAccounts();
      const accounts = accountsFromSnapshot(next ?? undefined);
      const targetOrigin = stationOriginKey(origin);
      const boundAccount = accounts.find((item) => stationOriginKey(item.origin) === targetOrigin);
      const stationID = boundAccount?.stationID
        ?? stationsFromSnapshot(next ?? undefined, accounts).find((item) => stationOriginKey(item.origin) === targetOrigin)?.id;
      if (stationID) {
        const bound = await dispatchWithOutcome("provider.select_relay_station", { provider_id: id, station_id: stationID });
        if (!bound) return;
      }
      onStatus?.(translate(resourceStatus === "ready" ? "relay.loginComplete" : "relay.loginResourcesUnavailable"));
    } catch {
      onStatus?.(translate("relay.operationFailed"));
    } finally {
      setRelayAddBusy(false);
    }
  };
  return <PersistentScrollView style={styles.providerEditorContent} contentContainerStyle={styles.providerEditorScrollContent} showsVerticalScrollIndicator nestedScrollEnabled>
    <View style={styles.providerEditorHeader}><Text numberOfLines={1} style={styles.providerEditorHeading}>{translate("providers.provider")}: {providerName}</Text>{sourceModel ? <NativeButton title={translate("providers.backToModel", { model: sourceModelLabel })} link disabled={busy} onPress={onReturnToModel} style={styles.providerReturnToModel} /> : null}</View>
    <View style={styles.providerEditorSection}>
    <View style={styles.providerEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(provider.enabled, true)} disabled={busy} onValueChange={(enabled) => dispatch(isLogin ? "service_provider.patch" : "provider.patch", isLogin ? { provider_id: id, provider: { enabled } } : { provider_id: id, changes: { enabled } })} /></View>
    {kind === "apiKey" || (kind === "relay" && !station) ? <ProviderSourceFields provider={provider} providerID={id} relayStations={relayStations} busy={busy} translate={translate} dispatch={dispatch} onStatus={onStatus} onBaseUrlDraftChange={(value) => drafts?.setProviderBaseUrlDraft(id, value)} onNameDraftChange={(value) => { if (drafts) drafts.setProviderNameDraft(id, value); else onNameDraftChange?.(value); }} /> : null}
    {kind === "relay" && station ? <>
      <TextField
        key={`vendor-name:${station.id}`}
        label={translate("providers.providerName")}
        labelWidth={88}
        value={stationDraft.name ?? stationDisplayName(station, translate)}
        disabled={busy}
        onDraftChange={(value) => setStationDraftValue({ name: value })}
        onCommit={() => { void stageStationUpdate(); }}
      />
      <TextField
        key={`vendor-url:${station.id}`}
        label={translate("providers.providerUrl")}
        labelWidth={88}
        value={stationDraft.origin ?? station.origin}
        disabled={busy}
        onDraftChange={(value) => setStationDraftValue({ origin: value })}
        onCommit={() => { void stageStationUpdate(); }}
      />
    </> : null}
    {!isLogin ? <ProviderKeysPanel
      provider={provider}
      providerId={id}
      kind={kind}
      stationAccounts={stationAccounts}
      native={native}
      busy={busy}
      translate={translate}
      dispatch={dispatch}
      onSecretState={onSecretState}
      relay={relay}
      onStatus={onStatus}
      language={language}
      bindingIssues={bindingIssues}
      variant="inline"
    /> : null}
    {service ? <View style={styles.providerSourceFields}>
      {/* The service's own identity: the name stays editable here, while the
          type and the address are what this provider is bound to.  These are
          the same fields a custom provider states — the 启用 row above them
          belongs to this group — so they share that container and draw no
          rule under the enable row; the dividers below open the groups that
          are genuinely other surfaces (the keys, the service's account, the
          stations linked to it). */}
      <TextField
        key={`service-name:${id}`}
        label={translate("providers.providerName")}
        labelWidth={88}
        value={providerName}
        disabled={busy}
        onDraftChange={(value) => drafts?.setProviderNameDraft(id, value)}
        onCommit={(name) => {
          const next = name.trim();
          if (next && next !== providerName) void dispatch("service_provider.patch", { provider_id: id, provider: { name: next } });
        }}
      />
      <PickerField
        label={translate("providers.wizard.providerType")}
        labelWidth={88}
        value={kind}
        values={SERVICE_KIND_OPTIONS.map((option) => ({ value: option.kind, label: translate(option.label) }))}
        disabled={busy}
        onSelect={(next) => {
          const authKind = serviceProviderKindFor(next as ProviderKind);
          if (authKind && next !== kind) void dispatch("service_provider.patch", { provider_id: id, provider: { auth_kind: authKind } });
        }}
      />
      <View style={styles.officialStatusRow}>
        <Text style={styles.providerAuthStatusLabel}>{translate("providers.wizard.baseUrl")}</Text>
        <Text numberOfLines={1} style={styles.providerAuthStatusValue}>{SERVICE_BASE_URLS[service]}</Text>
      </View>
    </View> : null}
    {isOfficialAccount ? <View style={styles.officialAccountSection}>
      <View style={styles.panelHeader}><Text style={styles.panelTitle}>{translate("providers.serviceLinks")}</Text></View>
      <View style={styles.officialStatusRow}>
        <Text style={styles.providerAuthStatusLabel}>{translate("providers.authStatus")}</Text>
        <Text style={styles.providerAuthStatusValue}>{statusLabels[authStatus]}</Text>
      </View>
      <Text style={styles.providerAuthLine}>{translate("relay.officialProviderModels")}: {modelNameText}</Text>
      <Text style={styles.keysHint}>{kind === "openai" ? translate("relay.officialProviderWebViewHint") : translate("relay.officialProviderBrowserHint")}</Text>
      {kind === "openai" && authActive ? <Text style={styles.officialActiveHint}>{translate("relay.officialProviderActive")}</Text> : null}
      {kind === "openai" && authStatus === "signed_in" && !authActive ? <Text style={styles.keysHint}>{translate("relay.officialProviderRestartHint")}</Text> : null}
      <View style={styles.officialActionsRow}>
        {kind === "openai" && authStatus === "signed_in" && !authActive ? <NativeButton title={translate("relay.officialProviderActivate")} compact busy={authPending === "activate"} disabled={(busy || loginBusy) && authPending !== "activate"} onPress={() => { void activateProvider(); }} /> : null}
        <NativeButton title={authLabel} primary compact busy={authPending === "auth"} disabled={(busy || loginBusy) && authPending !== "auth"} onPress={() => { void runAuthAction(); }} />
        <NativeButton title={translate("relay.officialProviderAddLogin")} compact busy={authPending === "sibling"} disabled={(busy || loginBusy) && authPending !== "sibling"} onPress={() => { void addSiblingAccount(); }} />
      </View>
      {kind === "claude" && authStatus === "error" ? <NativeSecretField autoCommit label={translate("providers.authTypeClaude")} hint={translate("relay.officialProviderTokenHint")} busy={busy} disabled={busy} domain="providers_models" field="provider_auth_token" target={id} onSecretState={onSecretState} /> : null}
    </View> : null}
    {!isLogin && station ? <StationAccountsPanel
      key={`station-accounts:${station.id}`}
      station={station}
      accounts={stationAccounts}
      native={native}
      language={language}
      cleanups={pendingCredentialCleanups(snapshotForCleanups)}
      busy={busy}
      translate={translate}
      commit={relay.commit}
      refreshAccounts={relay.refreshAccounts}
      refreshResources={relay.refreshResources}
      apiKeyActions={relay.apiKeyActions}
      applyStagedQuietly={relay.applyStagedQuietly}
      detectType={relay.detectType}
      stationDraft={stationDraft}
      onStationDraftChange={setStationDraftValue}
      onStageStationUpdate={stageStationUpdate}
      onStatus={onStatus}
    /> : null}
    {(!isLogin || isWorkBuddyAccount) && !station ? <View style={styles.providerAccountsHeader}>
      <View style={styles.panelHeader}>
        {/* The one association block: a station-backed provider links a relay
            station, a provider addressed at an official service links that
            service's own account.  The label follows the provider. */}
        <Text style={styles.panelTitle}>{translate(providerService(provider) ? "providers.serviceLinks" : "providers.accounts")}</Text>
        <View style={styles.panelActions}>
          {isWorkBuddyAccount && workbuddySignedIn ? <NativeButton title="" symbol="refresh" compact toolTip={translate("providers.wizard.workbuddyRefresh")} accessibilityLabel={translate("providers.wizard.workbuddyRefresh")} busy={workbuddyStatusBusy} disabled={busy && !workbuddyStatusBusy} onPress={() => { void refreshWorkBuddyAccount(); }} style={styles.iconButton} /> : null}
          {isWorkBuddyAccount && !workbuddySignedIn && workbuddyAccount ? <NativeButton title={translate("providers.wizard.workbuddyLogin")} compact busy={workbuddyStatusBusy} disabled={busy && !workbuddyStatusBusy} onPress={() => { void openWorkBuddyApp(); }} /> : null}
          {vendorBaseURL && !providerService(provider) ? <NativeButton title="" symbol="plus" compact toolTip={translate("providers.addRelayAccount")} accessibilityLabel={translate("providers.addRelayAccount")} busy={relayAddBusy} disabled={busy} onPress={() => { void addRelayAccountToVendor(); }} style={styles.iconButton} /> : null}
        </View>
      </View>
      {isWorkBuddyAccount ? <>
        <View style={styles.officialStatusRow}>
          <Text style={styles.providerAuthStatusLabel}>{translate("providers.workbuddyAccount")}</Text>
          <Text numberOfLines={1} style={styles.providerAuthStatusValue}>{stringValue(workbuddyAccount?.state) === "signed-in"
            ? [stringValue(workbuddyAccount?.nickname), stringValue(workbuddyAccount?.domain)].filter(Boolean).join(" · ")
            : progressText(workbuddyStatusBusy, translate) ?? translate("providers.wizard.workbuddySignInRequired", { name: workbuddyAppName(kind, translate) })}</Text>
        </View>
        {workbuddyCreditsText(workbuddyAccount, translate) ? <View style={styles.officialStatusRow}>
          <Text style={styles.providerAuthStatusLabel}>{translate("providers.wizard.workbuddyCredits")}</Text>
          <Text numberOfLines={1} style={styles.providerAuthStatusValue}>{workbuddyCreditsText(workbuddyAccount, translate)}</Text>
        </View> : null}
      </> : null}
      {providerService(provider) && !isWorkBuddyAccount ? <Text style={styles.keysHint}>{translate("providers.serviceKeyHint")}</Text> : null}
    </View> : null}
    </View>
  </PersistentScrollView>;
}

/**
 * External client configuration groups, in the pane's display order. Every
 * listed file is registered in Core, which is also what supplies its path.
 */
const CLIENT_FILE_GROUPS: ReadonlyArray<{ client: ClientFile["client"]; titleKey: string; hintKey: string }> = [
  { client: "codex", titleKey: "clients.codex", hintKey: "settings.codexFilesHint" },
  { client: "claudeCode", titleKey: "claude.codeSection", hintKey: "clients.claudeCodeFilesHint" },
  { client: "claudeDesktop", titleKey: "claude.desktopSection", hintKey: "clients.claudeDesktopFilesHint" },
  { client: "pi", titleKey: "clients.pi", hintKey: "clients.piFilesHint" },
  { client: "dsh", titleKey: "clients.dsh", hintKey: "clients.dshFilesHint" },
  { client: "opencode", titleKey: "clients.opencode", hintKey: "clients.opencodeFilesHint" },
];

/**
 * The managed model catalog row Core registers for Codex.  This app rewrites
 * that one file from its public model list, so the pane asks before opening
 * it while the switch is on; every other listed file opens directly.
 */
const CODEX_MODEL_CATALOG_FILE_ID = "codex_model_catalog";

/**
 * The external-settings file editor. Each host presents it as its own
 * subordinate surface — the macOS host opens it as a movable child window with
 * the workspace locked until it closes, the Windows host shows it as a plain
 * route in its single window — while Core stays the only writer.
 *
 * The surface owns explicit Save/Close actions, so the pane that lists the
 * file keeps its staged draft until the user saves: the shared gate below
 * blocks the pane's immediate apply while this surface is mounted.
 */
function editorTargetPayload(file: ClientFile, present: boolean): Record<string, string | boolean> {
  return {
    id: file.id,
    client: file.client,
    name: file.name,
    path: file.path,
    display_path: file.display_path,
    language: file.language,
    domain: file.domain,
    document: file.document,
    // A warmed window holds the editor open behind the pane; only a presented
    // one owns the file and blocks the pane's immediate apply.
    present,
  };
}

/** Read one host-provided editor target, rejecting anything malformed. */
function editorTargetFromPayload(payload: string | undefined): ClientFile | undefined {
  if (!payload) return undefined;
  try {
    const parsed = JSON.parse(payload) as Record<string, unknown>;
    const fields = ["id", "client", "name", "path", "language", "domain", "document"] as const;
    const payloadPresented = parsed.present === true;
    if (fields.some((field) => typeof parsed[field] !== "string" || !parsed[field])) return undefined;
    const language = parsed.language as ClientFile["language"];
    if (!["json", "toml", "yaml", "text"].includes(language)) return undefined;
    return {
      id: parsed.id as string,
      client: parsed.client as ClientFile["client"],
      name: parsed.name as string,
      path: parsed.path as string,
      // A host route that predates the display spelling still shows the file's
      // own path rather than an empty line.
      display_path: typeof parsed.display_path === "string" && parsed.display_path ? parsed.display_path : parsed.path as string,
      language,
      domain: parsed.domain as ClientFile["domain"],
      document: parsed.document as ClientFile["document"],
      // The listing already told the pane whether the file exists.
      exists: true,
      present: parsed.present === true,
    };
  } catch {
    return undefined;
  }
}

function FileEditorWorkspace({ file, fileId, nativeAction, ipc, native, translate, busy, onEditorConflict, reloadToken, baselineToken, syncRevision, onFlushPendingFields, onClose }: {
  file?: ClientFile;
  fileId?: string;
  nativeAction?: { id: string; sequence: number };
  ipc: IpcClient;
  native: NativeLeafAdapter;
  translate: Translate;
  busy: boolean;
  onEditorConflict: RawEditorConflictHandler;
  reloadToken: number;
  baselineToken: number;
  syncRevision?: number;
  onFlushPendingFields: () => Promise<void>;
  onClose: () => void;
}): React.JSX.Element {
  const [listed, setListed] = useState<ClientFile>();
  const [loadError, setLoadError] = useState<string>();
  const [saving, setSaving] = useState(false);
  // This child window states its own save result beside its footer buttons.
  const [footerStatus, setFooterStatus] = useState<string>();
  const readHostTarget = useCallback((): ClientFile | undefined => (
    editorTargetFromPayload(native.pendingFileEditorTarget?.())
    ?? editorTargetFromPayload(fileId)
  ), [fileId, native]);
  const [hostTarget, setHostTarget] = useState<ClientFile | undefined>(readHostTarget);
  const target = file ?? hostTarget ?? listed;
  // A warmed window has no requested document yet: it shows one existing file
  // so the embedded editor is already booted when the user asks for one.
  const requested = file !== undefined || hostTarget !== undefined;
  useEffect(() => {
    if (native.pendingFileEditorTarget === undefined) return undefined;
    setHostTarget(readHostTarget());
  }, [native, readHostTarget, nativeAction?.sequence]);
  useEffect(() => {
    if (target) return undefined;
    let active = true;
    void ipc.files()
      .then((result) => {
        if (!active) return;
        const candidate = result.files.find((item) => item.id === fileId)
          ?? result.files.find((item) => item.exists);
        if (candidate) setListed(candidate);
        else if (fileId) setLoadError(translate("error.generic"));
      })
      .catch((reason: unknown) => {
        if (active) setLoadError(errorMessage(reason, translate));
      });
    return () => { active = false; };
  }, [fileId, ipc, target, translate]);
  useEffect(() => {
    // The explicit Save/Close actions own the document while the window is on
    // screen; a merely warmed window blocks nothing.
    setAssistantEditorOpen(requested);
    return () => setAssistantEditorOpen(false);
  }, [requested]);
  const closeWindow = (): void => {
    setAssistantEditorOpen(false);
    if (Platform.OS === "windows") {
      // Windows owns one host window: restore the pane instead of hiding it.
      native.window.open("codex-settings");
      onClose();
      return;
    }
    try {
      native.window.close("file-editor");
    } finally {
      onClose();
    }
  };
  // Close owns the last word on an unsaved draft: warn once, then discard it.
  const close = (): void => {
    if (saving || busy) return;
    if (target === undefined) {
      closeWindow();
      return;
    }
    void (async () => {
      let current: CoreSnapshot | undefined;
      try {
        current = await ipc.snapshot();
      } catch {
        // A failed probe must never trap the user in the editor; the discard
        // below still runs, because closing means dropping the draft anyway.
        current = undefined;
      }
      if (current !== undefined && current.drafts[target.domain]?.dirty !== true) {
        closeWindow();
        return;
      }
      if (current !== undefined) {
        const confirmed = await native.showConfirmation({
          title: translate("settings.discardDraftTitle"),
          message: translate("settings.discardDraftBody"),
          confirmLabel: translate("common.discard"),
          destructive: true,
        });
        if (!confirmed) return;
      }
      // Core owns the staged document, so an explicit cancel is what makes
      // Close mean "throw the edits away" instead of leaving them staged for
      // the next open.
      try {
        await ipc.dispatch({ domain: target.domain, type: "cancel", payload: {} }, current?.revision);
      } catch {
        // The user already chose to discard and the window is going away; Core
        // reconciles the draft on the next open instead of blocking the close.
      }
      closeWindow();
    })();
  };
  const save = (): void => {
    if (!target || saving) return;
    setSaving(true);
    setFooterStatus(translate("common.saving"));
    void (async () => {
      await onFlushPendingFields();
      const refreshed = await ipc.snapshot();
      const domains = (["codex", "claude", "clients"] as const).filter((name) => refreshed.drafts[name]?.dirty);
      if (domains.length > 0) {
        try {
          await ipc.applyDomains([...domains], refreshed.revision);
        } catch (reason: unknown) {
          if (!isRevisionConflict(reason)) throw reason;
          // One rebase for a revision another surface advanced while the window
          // was open; a second conflict stays visible instead of overwriting.
          const current = await ipc.snapshot();
          await ipc.applyDomains([...domains], current.revision);
        }
      }
      setFooterStatus(translate("common.saved"));
    })().then(closeWindow).catch((reason: unknown) => {
      setFooterStatus(errorMessage(reason, translate));
      setSaving(false);
    });
  };
  return <View style={assistantFileSurfaceStyles.editorRoute}>
    <View style={assistantFileSurfaceStyles.editorHeader}>
      <View style={assistantFileSurfaceStyles.editorHeaderCopy}>
        <Text numberOfLines={1} style={assistantFileSurfaceStyles.editorTitle}>{target?.name ?? translate("settings.editFile")}</Text>
        <Text numberOfLines={1} ellipsizeMode="middle" style={assistantFileSurfaceStyles.editorPath}>{target?.display_path ?? ""}</Text>
      </View>
      {loadError === undefined ? null : <Text style={assistantFileSurfaceStyles.editorError}>{loadError}</Text>}
    </View>
    {target === undefined
      ? <View style={assistantFileSurfaceStyles.editorLoading}><Text style={assistantFileSurfaceStyles.filesStatus}>{translate("common.loading")}</Text></View>
      : <RawEditor showLabel={false} showDiff codexPane syncRevision={syncRevision} style={assistantFileSurfaceStyles.editorRouteRaw} label={target.name} domain={target.domain} document={target.document} language={target.language === "text" ? "json" : target.language} ipc={ipc} translate={translate} onConflict={onEditorConflict} reloadToken={reloadToken} baselineToken={baselineToken} />}
    <View style={assistantFileSurfaceStyles.editorFooter}>
      {/* The child states its own result beside its buttons; the buttons keep
          their trailing edge, so a message never moves them. */}
      <Text numberOfLines={1} style={assistantFileSurfaceStyles.editorFooterStatus}>{footerStatus ?? ""}</Text>
      <View style={assistantFileSurfaceStyles.editorFooterActions}>
        <ActionButton title={translate("status.close")} disabled={busy || saving} onPress={close} />
        <ActionButton primary title={translate("status.saveAndClose")} busy={saving} disabled={(busy && !saving) || target === undefined} onPress={save} />
      </View>
    </View>
  </View>;
}

/**
 * The native action menu rejects a list above this shared host limit, so a
 * long model list is offered up to the count both apps can actually pop up.
 */
const NATIVE_ACTION_MENU_ITEM_LIMIT = 32;

/** One saved provider route, as the pane's controls name and send it. */
type ClientRoute = { model: string; provider: string; deployment_id: string; label: string };

function AssistantSettingsWorkspace({ busy, native, codexModels, codexModelCatalogEnabled, clientProvider, clientModel, localApiActive, onToggleCodexModelCatalog, onUseLocalApi, onUseSavedModel, translate, ipc, filesToken, onOpenFile }: { busy: boolean; native: NativeLeafAdapter; codexModels: UnknownRecord[]; codexModelCatalogEnabled: boolean; clientProvider: string; clientModel: string; localApiActive: boolean; onToggleCodexModelCatalog: (enabled: boolean) => Promise<void>; onUseLocalApi: (selection: UnknownRecord) => Promise<void>; onUseSavedModel: (selection: UnknownRecord) => Promise<void>; translate: Translate; ipc: IpcClient; filesToken: number; onOpenFile: (file: ClientFile) => void }): React.JSX.Element {
  const [files, setFiles] = useState<ClientFile[]>();
  const [error, setError] = useState<string>();
  // The rail selects one client; its detail lists that client's files only.
  const [selectedClient, setSelectedClient] = useState<ClientFile["client"]>("codex");
  useEffect(() => {
    let active = true;
    // The listing is a read-only Core projection. Reloading it after every
    // Apply is how the pane learns about a file it just created.
    setError(undefined);
    void ipc.files()
      .then((result) => { if (active) setFiles(result.files); })
      .catch((reason: unknown) => { if (active) setError(errorMessage(reason, translate)); });
    return () => { active = false; };
  }, [filesToken, ipc, translate]);

  const clientFiles = (client: ClientFile["client"]): ClientFile[] => (files ?? []).filter((file) => file.client === client);
  // A client Core does not register (an older Core, or a client that reports
  // nothing) must not leave the detail empty while the rail shows entries.
  useEffect(() => {
    if (!files || files.length === 0) return;
    if (clientFiles(selectedClient).length > 0) return;
    const fallback = files[0]?.client;
    if (fallback) setSelectedClient(fallback);
  }, [files, selectedClient]);
  const railRows = useMemo(() => CLIENT_FILE_GROUPS.map(({ client, titleKey }) => ({
    key: client,
    cells: [translate(titleKey)],
  })), [translate]);
  const selectedGroup = CLIENT_FILE_GROUPS.find(({ client }) => client === selectedClient) ?? CLIENT_FILE_GROUPS[0];
  // Finder on macOS, Explorer on Windows: the label names the platform's own
  // file manager, and the host only ever opens it at the listed path.
  const revealFileLabel = translate(Platform.OS === "windows" ? "settings.revealInExplorer" : "settings.revealInFinder");
  // Codex's own config names its gateway, so this client has exactly two
  // backends: this app's proxy, or one saved provider route.  The field's
  // switch is the real two-way switch between them and mirrors the
  // configuration; the control beside it decides which saved route the off
  // position means.  A controlled native checkbox only repaints on a changed
  // value prop, so an uncheck that cannot name a saved route re-mounts the box
  // onto the configured value instead of drifting from the file.
  const [clientActionBusy, setClientActionBusy] = useState(false);
  const [clientSwitchGeneration, setClientSwitchGeneration] = useState(0);
  const settleClientSwitch = (): void => setClientSwitchGeneration((current) => current + 1);
  // Codex's own model list is one Core switch, not a document edit: checked
  // installs this app's public model list as Codex's catalog, unchecked drops
  // the managed pointer so the client falls back to its built-in list. Core
  // owns the value; the switch only guards the request while it is in flight,
  // and macOS follows a change with the Codex restart prompt.
  const [catalogBusy, setCatalogBusy] = useState(false);
  const toggleCatalog = (enabled: boolean): void => {
    if (catalogBusy) return;
    setCatalogBusy(true);
    void onToggleCodexModelCatalog(enabled).finally(() => setCatalogBusy(false));
  };
  // The catalog file only stays the user's while the switch is off: with it
  // on, Core rewrites that file from the public model list whenever the list
  // changes, so an edit needs one explicit confirmation first.
  const openClientFile = (file: ClientFile): void => {
    if (file.id !== CODEX_MODEL_CATALOG_FILE_ID || !codexModelCatalogEnabled) {
      onOpenFile(file);
      return;
    }
    void native.showConfirmation({
      title: translate("codex.modelCatalogEditTitle"),
      message: translate("codex.modelCatalogEditBody"),
      confirmLabel: translate("codex.modelCatalogEditConfirm"),
    }).then((confirmed) => { if (confirmed) onOpenFile(file); });
  };
  // The designate action puts the client straight onto one saved provider
  // route: its own model, endpoint, and key, taken from Young Router's stored
  // model list.
  const [designateBusy, setDesignateBusy] = useState(false);
  const designateButtonRef = useRef<HostInstance | null>(null);
  const savedModelGroups = useMemo(() => {
    const rows = codexModels.flatMap((row) => {
      const model = stringValue(row.model).trim();
      const provider = stringValue(row.provider).trim();
      const deploymentId = stringValue(row.deployment_id).trim();
      if (!model || !provider || !deploymentId) return [];
      return [{ model, provider, deploymentId, keyName: stringValue(row.api_key_name).trim() }];
    });
    // One route is one menu row: a provider can serve one public name from two
    // routes (two keys, a fallback ordering), and collapsing them by name
    // offered only the first — the second could never be designated.  Where a
    // name is shared, the row says which key it answers with.
    const shared = new Map<string, number>();
    for (const row of rows) {
      const identity = `${row.provider}\u001f${row.model}`;
      shared.set(identity, (shared.get(identity) ?? 0) + 1);
    }
    const providers = new Map<string, { labels: string[]; selections: UnknownRecord[] }>();
    const seen = new Set<string>();
    for (const row of rows) {
      const identity = `${row.provider}\u001f${row.deploymentId}`;
      if (seen.has(identity)) continue;
      seen.add(identity);
      const duplicated = (shared.get(`${row.provider}\u001f${row.model}`) ?? 0) > 1;
      const entry = providers.get(row.provider) ?? { labels: [], selections: [] };
      entry.labels.push(duplicated ? `${row.model} · ${row.keyName || translate("providers.undefinedKey")}` : row.model);
      entry.selections.push({ model: row.model, provider: row.provider, deployment_id: row.deploymentId });
      providers.set(row.provider, entry);
    }
    return [...providers].map(([provider, entry]) => ({ provider, labels: entry.labels, selections: entry.selections }));
  }, [codexModels, translate]);
  const savedModelCount = savedModelGroups.reduce((total, group) => total + group.labels.length, 0);
  const designateSavedModel = (selection: UnknownRecord | undefined): void => {
    const chosen = selection ?? undefined;
    if (!chosen || designateBusy || clientActionBusy) return;
    const provider = stringValue(chosen.provider).trim();
    const model = stringValue(chosen.model).trim();
    if (provider && model) setLastRoute({ model, provider, deployment_id: stringValue(chosen.deployment_id).trim(), label: `${provider} / ${model}` });
    setDesignateBusy(true);
    void onUseSavedModel(chosen).finally(() => setDesignateBusy(false));
  };
  // One menu backs both controls: a provider is the menu's own level, so a
  // long model list stays navigable, and each provider keeps its models in
  // stored order.  The host answers the chosen `{group, item}` pair, or
  // nothing when the menu was dismissed, which leaves the client as it was.
  const openSavedModelMenu = (anchor: HostInstance | null): void => {
    const showGroupedActionMenu = native.showGroupedActionMenu;
    if (!anchor || !showGroupedActionMenu || savedModelCount === 0) return;
    const groups = savedModelGroups
      .slice(0, NATIVE_ACTION_MENU_ITEM_LIMIT)
      .map((group) => ({ title: group.provider, items: group.labels }));
    anchor.measureInWindow((x, y, width, height) => {
      void showGroupedActionMenu({ title: translate("settings.designateAs"), groups, anchor: { x, y, width, height } })
        .then((choice) => { if (choice) designateSavedModel(savedModelGroups[choice.group]?.selections[choice.item]); });
    });
  };
  // The client names the route's own provider model id, which can carry a
  // LiteLLM adapter prefix and the provider's own decoration, while the menu
  // beside it offers the saved route's public model.  Resolve the saved route
  // the client points at once, so both the control's label and the switch's
  // off direction use the spelling the menu itself offers; a target this pane
  // cannot resolve keeps the client's own spelling and can only be left
  // through the menu.
  const clientRoute = useMemo(() => {
    const tail = (value: string): string => value.slice(value.lastIndexOf("/") + 1).trim().toLowerCase();
    const model = clientModel.trim();
    if (!model) return undefined;
    // The client names its route either by the provider's own model id (when
    // it talks to the provider directly) or by the public name this app's
    // proxy serves, so both spellings identify the same saved route — which is
    // what lets the switch name the route it would go back to.
    const matches = (row: UnknownRecord): boolean => {
      const publicModel = stringValue(row.model).trim();
      if (publicModel && publicModel === model) return true;
      return tail(stringValue(row.upstream_model)) === tail(model);
    };
    // The provider ``model_provider`` names is the route's own provider on
    // both sides of the switch, so that pairing identifies the route exactly;
    // a model name on its own is only a fallback for a client this pane does
    // not recognize.
    const matching = codexModels.filter(matches);
    const saved = codexModels.find((row) => clientProvider !== "" && stringValue(row.provider).trim() === clientProvider && matches(row))
      // Without a provider this pane recognizes, the model name alone decides
      // only while one saved route carries it: two providers can expose the
      // same upstream tail, and picking the first would put the client on a
      // route it was never on.  An unresolved target keeps the client's own
      // spelling and can still be left through the designate menu.
      ?? (matching.length === 1 ? matching[0] : undefined);
    if (!saved) return undefined;
    const savedProvider = stringValue(saved.provider).trim();
    const mapped = stringValue(saved.model).trim();
    const deploymentId = stringValue(saved.deployment_id).trim();
    if (!savedProvider || !mapped || !deploymentId) return undefined;
    return { model: mapped, provider: savedProvider, deployment_id: deploymentId, label: `${savedProvider} / ${mapped}` };
  }, [clientModel, clientProvider, codexModels]);
  // The route the user last put this client on.  On the proxy the client's
  // model is only a public name, which several saved routes may share, so the
  // remembered choice — not the first matching row — decides where "off" goes.
  const [lastRoute, setLastRoute] = useState<ClientRoute | undefined>(undefined);
  useEffect(() => {
    if (clientRoute) setLastRoute(clientRoute);
  }, [clientRoute]);
  const offRoute = localApiActive ? lastRoute ?? clientRoute : clientRoute;
  const clientRouteLabel = clientRoute?.label
    ?? [clientProvider, clientModel].filter((part) => part.length > 0).join(" / ");
  // The control names the API this client uses.  On this app's proxy no
  // specific API is selected — the client talks to the proxy — so the field
  // stays empty there, and picking a route is what fills it and moves the
  // switch off.  The route remembered for a later "off" is deliberately not
  // shown: it is where the switch would go, not where the client is.
  const designatedRoute = localApiActive ? "" : clientRouteLabel;
  // A real two-way switch: on adopts this app's proxy through the provider row
  // this app owns, off puts the client back on the saved route the control
  // names — resolved from the client's own provider and model, so unchecking
  // opens no menu.  An action that cannot name a saved route changes nothing
  // and re-mounts the switch onto the configured value (a controlled native
  // control only repaints on a *changed* value prop).
  const toggleLocalApi = (enabled: boolean): void => {
    if (clientActionBusy || designateBusy) { settleClientSwitch(); return; }
    if (enabled) {
      setClientActionBusy(true);
      // The proxy serves public model names, so adopting it also names the
      // route the client is leaving.
      void onUseLocalApi(offRoute ?? {}).finally(() => setClientActionBusy(false));
      return;
    }
    if (!offRoute) { settleClientSwitch(); return; }
    designateSavedModel(offRoute);
  };
  const designateAction = selectedClient === "codex"
    ? <NativeButton
        ref={designateButtonRef}
        title={designatedRoute}
        toolTip={translate(savedModelCount > 0 ? "settings.designateAsHint" : "settings.designateAsUnavailable")}
        accessibilityLabel={designatedRoute ? `${translate("settings.designateAs")}: ${designatedRoute}` : translate("settings.designateAs")}
        titleWidth="flex"
        symbol="chevron-up-down"
        symbolWithTitle
        symbolTrailing
        disabled={(busy && !designateBusy) || designateBusy || clientActionBusy || savedModelCount === 0}
        busy={designateBusy}
        onPress={() => {
          if (designateBusy || clientActionBusy) return;
          openSavedModelMenu(designateButtonRef.current);
        }}
      />
    : null;

  return <View style={styles.externalSettingsWorkspace}>
    <SettingsRail
      rows={railRows}
      selectedKey={selectedClient}
      onSelectionChange={(key) => { if (key) setSelectedClient(key as ClientFile["client"]); }}
    />
    <View style={styles.settingsRailDetail}>
      <SettingsDetailHeader
        title={translate(selectedGroup.titleKey)}
        hint={translate(selectedGroup.hintKey)}
      />
      <PersistentScrollView style={styles.externalSettingsPane} contentContainerStyle={styles.externalSettingsPaneContent} horizontal={false} showsVerticalScrollIndicator>
        {/* The Codex client's controls are fields of the same grid the runtime
            pane's settings use — one left-aligned label column, one control
            column, and the tip wrapped underneath that column — so both detail
            panes read as one surface.  The switch moves the client between
            this app's proxy and the saved route the control names. */}
        {selectedClient === "codex" ? <View style={styles.externalSettingsFieldList}>
          <View style={styles.externalSettingsField}>
            <View style={styles.externalSettingsInputRow}>
              <TooltipText numberOfLines={1} tooltip={translate("settings.useLocalApi")} style={styles.externalSettingsFieldLabel} accessibilityLabel={translate("settings.useLocalApi")}>{translate("settings.useLocalApi")}</TooltipText>
              <View style={styles.externalSettingsValueSlot}>
                <NativeToggle
                  key={`use-local-api-${clientSwitchGeneration}`}
                  accessibilityLabel={translate("settings.useLocalApi")}
                  value={localApiActive}
                  disabled={(busy && !(clientActionBusy || designateBusy)) || clientActionBusy || designateBusy}
                  onValueChange={toggleLocalApi}
                  style={styles.externalSettingsBooleanControl}
                />
              </View>
            </View>
            <View style={styles.externalSettingsHelpSlot}><Text style={styles.externalSettingsHelpText}>{translate("settings.useLocalApiHint")}</Text></View>
          </View>
          <View style={styles.externalSettingsField}>
            <View style={styles.externalSettingsInputRow}>
              <TooltipText numberOfLines={1} tooltip={translate("settings.designateAs")} style={styles.externalSettingsFieldLabel} accessibilityLabel={translate("settings.designateAs")}>{translate("settings.designateAs")}</TooltipText>
              <View style={styles.externalSettingsValueSlot}>{designateAction}</View>
            </View>
          </View>
          <View style={styles.externalSettingsField}>
            <View style={styles.externalSettingsInputRow}>
              <TooltipText numberOfLines={1} tooltip={translate("codex.modelCatalog")} style={styles.externalSettingsFieldLabel} accessibilityLabel={translate("codex.modelCatalog")}>{translate("codex.modelCatalog")}</TooltipText>
              <View style={styles.externalSettingsValueSlot}>
                <NativeToggle
                  accessibilityLabel={translate("codex.modelCatalog")}
                  value={codexModelCatalogEnabled}
                  disabled={(busy && !catalogBusy) || catalogBusy}
                  onValueChange={toggleCatalog}
                  style={styles.externalSettingsBooleanControl}
                />
              </View>
            </View>
            <View style={styles.externalSettingsHelpSlot}><Text style={styles.externalSettingsHelpText}>{translate("codex.modelCatalogHint")}</Text></View>
          </View>
        </View> : null}
        {error !== undefined
          ? <Text style={assistantFileSurfaceStyles.filesStatus}>{error}</Text>
          : files === undefined
            ? <Text style={assistantFileSurfaceStyles.filesStatus}>{translate("common.loading")}</Text>
            : clientFiles(selectedClient).map((file) => <View key={file.id} style={assistantFileSurfaceStyles.fileRow}>
              <View style={assistantFileSurfaceStyles.fileMeta}>
                <Text numberOfLines={1} style={assistantFileSurfaceStyles.fileLabel}>{file.name}</Text>
                {/* The path itself is the reveal control, so a click opens the
                    platform file manager at that file (or its directory). */}
                <NativeButton
                  link
                  plainLink
                  titleWidth="flex"
                  title={file.display_path}
                  toolTip={revealFileLabel}
                  accessibilityLabel={`${revealFileLabel}: ${file.display_path}`}
                  disabled={native.revealFile === undefined}
                  onPress={() => native.revealFile?.(file.path)}
                  style={assistantFileSurfaceStyles.filePathLink}
                />
              </View>
              {file.exists ? null : <Text style={assistantFileSurfaceStyles.fileMissing}>{translate("clients.fileMissing")}</Text>}
              <NativeButton title={translate("settings.editFile")} toolTip={file.exists ? undefined : translate("clients.fileMissing")} accessibilityLabel={`${translate("settings.editFile")}: ${file.display_path}`} disabled={busy || !file.exists} onPress={() => openClientFile(file)} style={assistantFileSurfaceStyles.editFileButton} />
            </View>)}
      </PersistentScrollView>
    </View>
  </View>;
}

function GeneralWorkspace({ snapshot, ipc, native, busy, dispatch, dispatchServiceAction, translate, onStatus, onSnapshot }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; dispatch: Dispatch; dispatchServiceAction: (type: string) => Promise<unknown>; translate: Translate; onStatus: (message?: string) => void; onSnapshot: (next: CoreSnapshot) => void }): React.JSX.Element {
  const [autoStartBusy, setAutoStartBusy] = useState(false);
  const [serviceBusy, setServiceBusy] = useState(false);
  const [requestedAutoStart, setRequestedAutoStart] = useState<boolean>();
  const [requestedBackground, setRequestedBackground] = useState<boolean>();
  const [backgroundBusy, setBackgroundBusy] = useState(false);
  const autoStartEnabled = snapshot?.service.auto_start_state === "enabled";
  // The switch shows Core's stored preference, plus the value the user just
  // requested until Core confirms it. A rejected dispatch must not leave the
  // native switch claiming a preference Core never accepted.
  const autoStartValue = requestedAutoStart ?? autoStartEnabled;
  useEffect(() => {
    if (requestedAutoStart !== undefined && requestedAutoStart === autoStartEnabled) setRequestedAutoStart(undefined);
  }, [autoStartEnabled, requestedAutoStart]);
  // An absent marker is the default: a launch presents its window.
  const backgroundEnabled = snapshot?.service.launch_background_state === "enabled";
  const backgroundValue = requestedBackground ?? backgroundEnabled;
  useEffect(() => {
    if (requestedBackground !== undefined && requestedBackground === backgroundEnabled) setRequestedBackground(undefined);
  }, [backgroundEnabled, requestedBackground]);
  const serviceState = snapshot?.service.state ?? "unknown";
  const runtimeSettings = asRecords(domainState(snapshot, "runtime").settings);
  const portItem = runtimeSettings.find((item) => identifier(item) === "LITELLM_PORT");
  const portValue = stringValue(portItem?.value, "");
  const portDefault = stringValue(portItem?.default, "12390");
  const validatePort = (next: string): string | undefined => {
    const trimmed = next.trim();
    if (!/^\d+$/.test(trimmed)) return translate("runtime.invalidInteger");
    const parsed = Number(trimmed);
    if (!(parsed >= 1 && parsed <= 65535)) return translate("runtime.outOfRange", { min: "1", max: "65535" });
    return undefined;
  };
  const serviceLabel = serviceState === "running"
    ? translate("service.runningOnPort", { port: String(snapshot?.service.port ?? "") })
    : serviceState === "starting" ? translate("service.starting")
      : serviceState === "unhealthy" ? translate("service.unhealthy")
        : serviceState === "stopped" ? translate("service.stopped")
          : translate("service.unknown");
  // The proxy follows the app, and a launch-time start can lose its race with
  // the rest of the login session, so a stopped or unhealthy service keeps one
  // recovery control in this pane instead of only in the status menu.
  const serviceRestart = serviceState === "unhealthy";
  const serviceActionAvailable = serviceState === "stopped" || serviceRestart;
  const runServiceAction = async (): Promise<void> => {
    if (!snapshot || serviceBusy) return;
    setServiceBusy(true);
    // Pane results belong to the window's one permanent status strip.
    onStatus(undefined);
    try {
      await dispatchServiceAction(serviceRestart ? "service.restart" : "service.start");
      onSnapshot(await ipc.snapshot());
      // The 服务 row states the state itself; the strip names the action that
      // just landed instead of repeating the state word beside it.
      onStatus(translate(serviceRestart ? "service.restarted" : "service.started"));
    } catch (reason: unknown) {
      onStatus(errorMessage(reason, translate));
    } finally {
      setServiceBusy(false);
    }
  };
  const setAutoStart = async (enabled: boolean): Promise<void> => {
    if (!snapshot || autoStartBusy) return;
    setAutoStartBusy(true);
    setRequestedAutoStart(enabled);
    // Pane results belong to the window's one permanent status strip, exactly
    // like every other settings pane; the pane body never grows a message row.
    onStatus(undefined);
    try {
      // Launch-at-login is two-sided: Core records the preference and the host
      // registers or removes the login item. The Core write takes the same
      // buffered service dispatch as the rest of the app, so this pane's
      // snapshot revision may rebase once instead of reporting a conflict the
      // switch never caused.
      await dispatchServiceAction(enabled ? "service.autostart_enable" : "service.autostart_disable");
      try {
        await native.setLaunchAtLogin(enabled);
      } catch (hostReason) {
        // The host could not register or remove the login item, so Core's
        // preference is handed back — the same rollback the status menu's own
        // toggle performs — instead of leaving a preference the system will
        // not honour.
        try {
          await dispatchServiceAction(enabled ? "service.autostart_disable" : "service.autostart_enable");
        } catch {
          // The original failure is the one worth reporting.
        }
        throw hostReason;
      }
      onSnapshot(await ipc.snapshot());
      onStatus(translate("common.saved"));
    } catch (reason: unknown) {
      // Hand the switch back to the preference Core actually kept.
      setRequestedAutoStart(undefined);
      onStatus(errorMessage(reason, translate));
    } finally {
      setAutoStartBusy(false);
    }
  };
  const setRunInBackground = async (enabled: boolean): Promise<void> => {
    if (!snapshot || backgroundBusy) return;
    setBackgroundBusy(true);
    setRequestedBackground(enabled);
    // Pane results belong to the window's one permanent status strip, exactly
    // like every other settings pane; the pane body never grows a message row.
    onStatus(undefined);
    try {
      // The preference is stored before anything moves on screen: a launch
      // promise the host acts on must never outlive a failed Core write.
      await dispatchServiceAction(enabled ? "service.launch_background_enable" : "service.launch_background_disable");
      onSnapshot(await ipc.snapshot());
      if (enabled) {
        // Turning the promise on takes effect at once — the window the user is
        // looking at is the one a background launch would not have shown, so it
        // closes instead of leaving the app claiming a mode it is not in. This
        // pane stages nothing, so leaving the shell cannot discard a draft.
        // Turning it off deliberately does not reopen anything: the next launch
        // presents its window, and popping one up here would interrupt whatever
        // the user moved on to.
        native.window.focus("home");
      }
      onStatus(translate("common.saved"));
    } catch (reason: unknown) {
      // Hand the switch back to the preference Core actually kept.
      setRequestedBackground(undefined);
      onStatus(errorMessage(reason, translate));
    } finally {
      setBackgroundBusy(false);
    }
  };
  return <PersistentScrollView style={styles.generalScroll} contentContainerStyle={styles.generalContent}>
    <View style={styles.generalSection}>
      <Text style={styles.generalSectionTitle}>{translate("general.startup")}</Text>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.autoStart")}</Text>
        <NativeToggle value={autoStartValue} disabled={autoStartBusy || busy || snapshot === undefined} accessibilityLabel={translate("general.autoStart")} onValueChange={(next) => { void setAutoStart(next); }} style={styles.generalToggle} />
      </View>
      <View style={styles.generalHelpSlot}><Text style={styles.generalHelpText}>{translate("general.autoStartHint")}</Text></View>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.runInBackground")}</Text>
        <NativeToggle value={backgroundValue} disabled={backgroundBusy || busy || snapshot === undefined} accessibilityLabel={translate("general.runInBackground")} onValueChange={(next) => { void setRunInBackground(next); }} style={styles.generalToggle} />
      </View>
      <View style={styles.generalHelpSlot}><Text style={styles.generalHelpText}>{translate("general.runInBackgroundHint")}</Text></View>
    </View>
    <View style={styles.generalSection}>
      <Text style={styles.generalSectionTitle}>{translate("general.service")}</Text>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.serviceState")}</Text>
        <Text numberOfLines={1} style={styles.generalRowValue}>{serviceLabel}</Text>
        {serviceActionAvailable ? <NativeButton compact primary={!serviceRestart} busy={serviceBusy} disabled={snapshot === undefined || (busy && !serviceBusy)} title={serviceRestart ? translate("service.restart") : translate("service.start")} accessibilityLabel={serviceRestart ? translate("service.restart") : translate("service.start")} onPress={() => { void runServiceAction(); }} style={styles.generalServiceAction} /> : null}
      </View>
      <View style={styles.generalHelpSlot}><Text style={styles.generalHelpText}>{translate("general.serviceHint")}</Text></View>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.port")}</Text>
        <RuntimeValueField label={translate("general.port")} value={portValue} keyboardType="numeric" validate={validatePort} onCommit={(next) => dispatch("set_setting", { key: "LITELLM_PORT", value: next })} />
      </View>
      <View style={styles.generalHelpSlot}><Text style={styles.generalHelpText}>{translate("general.portHint", { default: portDefault })}</Text></View>
    </View>
  </PersistentScrollView>;
}

function RuntimeWorkspace({ snapshot, busy, translate, dispatch, onSecretState, clearSecret }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear }): React.JSX.Element {
  const state = domainState(snapshot, "runtime");
  const allSettings = asRecords(state.settings).length > 0 ? asRecords(state.settings) : asRecords(state.categories).flatMap((category) => asRecords(category.settings));
  // Settings owned by the General pane (for example the proxy port) stay in
  // the runtime domain but are not repeated here.
  const settings = allSettings.filter((item) => item.hidden !== true);
  const groups = groupBy(settings, (item) => stringValue(item.category, translate("runtime.categories")));
  const categories = Object.keys(groups);
  // One category is one surface, the way a backup tab and an external client
  // are: the rail selects the category and the pane renders only that
  // category's settings, so the whole domain stays navigable without one
  // continuous scroll whose position has to drive the selection back.
  const [activeCategory, setActiveCategory] = useState("");
  const selectedCategory = categories.includes(activeCategory) ? activeCategory : (categories[0] ?? "");
  const tocRows = useMemo(() => categories.map((name) => ({ key: name, cells: [runtimeCategoryLabel(name, translate)] })), [categories, translate]);
  const dshSyncToken = DSH_VISION_ROUTER_QUICK_KEYS.map((key) => `${key}:${stringValue(settings.find((item) => identifier(item) === key)?.value)}`).join("|");
  const selectCategory = (name: string): void => {
    // A rail click always names a real category, so a cleared selection keeps
    // the current one instead of untracking it.
    if (!name) return;
    setActiveCategory(name);
  };
  return <View style={styles.runtimeWorkspaceFrame}>
    <View style={styles.runtimeWorkspaceBody}>
      <SettingsRail rows={tocRows} selectedKey={selectedCategory} onSelectionChange={selectCategory} />
      <View style={styles.settingsRailDetail}>
        {selectedCategory === "" ? null : <SettingsDetailHeader title={runtimeCategoryLabel(selectedCategory, translate)} />}
        {/* The surface is keyed by its category, so a switch starts at the top of
            the new category instead of inheriting the previous scroll offset. */}
        <PersistentScrollView key={selectedCategory} style={styles.runtimeScrollSurface} contentContainerStyle={styles.runtimeWorkspace}>
          {selectedCategory === "" ? <EmptyState translate={translate} /> : <View style={styles.runtimeFieldList}>{(groups[selectedCategory] ?? []).map((item) => <RuntimeField key={identifier(item)} item={item} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} dshSyncToken={dshSyncToken} />)}</View>}
        </PersistentScrollView>
      </View>
    </View>
  </View>;
}

function DataManagementWorkspace({ snapshot, tab, onTabChange, statuses, busy, webDavOperationBusy, translate, dispatch, onSecretState, onFlushPendingFields, onTabSwitchError, onInspectImport, onImport, onConfirmImportReplace, onExport, onProbeWebDav, onSyncWebDav, onCommitWebDavSettings }: {
  snapshot?: CoreSnapshot;
  /** The active tab is the route surface's, because it decides whose result the
   * window's one status strip reports. */
  tab: DataManagementTab;
  onTabChange: (tab: DataManagementTab) => void;
  /** Each pane's last result, whose own header band states it. */
  statuses: Partial<Record<DataManagementTab, string>>;
  busy: boolean;
  webDavOperationBusy: boolean;
  translate: Translate;
  dispatch: Dispatch;
  onSecretState: (state: SecretState) => void;
  onFlushPendingFields: () => Promise<void>;
  onTabSwitchError: (tab: DataManagementTab, reason: unknown) => void;
  onInspectImport: () => Promise<IpcResults["import_preview"] | undefined>;
  onImport: (sections: ConfigDomain[]) => Promise<IpcResults["import"] | undefined>;
  onConfirmImportReplace: (sections: string[]) => Promise<boolean>;
  onExport: (sections: ConfigDomain[]) => Promise<void>;
  onProbeWebDav: () => Promise<void>;
  onSyncWebDav: (action: WebDavSyncAction) => Promise<void>;
  onCommitWebDavSettings: (patch: UnknownRecord) => Promise<void>;
}): React.JSX.Element {
  // Which data operation is running: only the button that started it shows the
  // spinner, while the pane's other controls stay disabled as before.
  const [pendingAction, runPendingAction] = usePendingAction<DataManagementAction>();
  const controlsBusy = (action?: DataManagementAction): boolean => busy && pendingAction !== action;
  const webDavBusy = (action: "probe" | "sync"): boolean => (busy || webDavOperationBusy) && pendingAction !== action;
  const [importPreview, setImportPreview] = useState<IpcResults["import_preview"]>();
  const [importSections, setImportSections] = useState<ConfigDomain[]>([]);
  const [stagedSections, setStagedSections] = useState<ConfigDomain[]>([]);
  const [exportSections, setExportSections] = useState<ConfigDomain[]>([...DATA_PACKAGE_DOMAINS]);
  // A pane's command is a field like any other: the label column names the row,
  // the control column holds the button, and the result of that command stands
  // on the same line, immediately beside it.
  // The row keeps the label column (so its controls land on the pane's control
  // column) and leaves it empty: the buttons name their own actions.
  const actionRow = (controls: React.ReactNode, result?: string, hint?: string): React.JSX.Element => <View style={SETTINGS_FIELD}>
    <View style={SETTINGS_FIELD_ROW_INDENTED}>
      <Text style={SETTINGS_FIELD_LABEL}>{""}</Text>
      {controls}
      <Text numberOfLines={2} style={styles.dataManagementActionStatus}>{result ?? ""}</Text>
    </View>
    {hint === undefined ? null : <View style={SETTINGS_FIELD_HELP_SLOT}><Text style={SETTINGS_FIELD_HELP_TEXT}>{hint}</Text></View>}
  </View>;
  const dataManagementTabRows = useMemo(() => ([
    { id: "import" as DataManagementTab, title: translate("dataManagement.tab.import") },
    { id: "export" as DataManagementTab, title: translate("dataManagement.tab.export") },
    { id: "webdav" as DataManagementTab, title: translate("dataManagement.tab.webdav") },
  ]).map(({ id, title }) => ({ key: id, cells: [title] })), [translate]);
  const detectedImportSections = useMemo(() => DATA_PACKAGE_DOMAINS.filter((domain) => importPreview?.detected_sections.includes(domain)), [importPreview]);
  const importReviewReady = importPreview !== undefined && stagedSections.length === 0;
  const replacingDraftSections = importSections.filter((domain) => importPreview?.preview[domain]?.will_replace_draft === true);
  const switchDataManagementTab = (next: DataManagementTab): void => {
    if (next === tab) return;
    const previous = tab;
    // Keep navigation immediate even if Core is slow or unavailable. Pending
    // field writes finish in the background and report their own failure.
    const pending = onFlushPendingFields();
    onTabChange(next);
    void pending.catch((reason: unknown) => {
      onTabSwitchError(previous, reason);
    });
  };
  const toggleSection = (setter: React.Dispatch<React.SetStateAction<ConfigDomain[]>>, domain: ConfigDomain, enabled: boolean): void => setter((current) => enabled
    ? DATA_PACKAGE_DOMAINS.filter((item) => item === domain || current.includes(item))
    : current.filter((item) => item !== domain));
  const sectionList = (available: readonly ConfigDomain[], selected: readonly ConfigDomain[], disabled: boolean, onToggle: (domain: ConfigDomain, enabled: boolean) => void): React.JSX.Element => {
    const sections = DATA_PACKAGE_SECTIONS.filter(({ domain }) => available.includes(domain));
    // Choosing among many saved sections is a multi-select, so these stay
    // checkboxes: a switch is for one on/off setting, not for a checkbox list.
    // One section is one checkbox row, and the checkbox carries its own label:
    // a short list of names stays one tight column instead of stretching a
    // label to the far edge of the pane, where the eye has to travel for it.
    return <View style={styles.dataManagementSectionList}>{sections.map(({ domain, labelKey }) => <NativeCheckbox key={domain} label={translate(labelKey)} value={selected.includes(domain)} disabled={disabled} onValueChange={(enabled) => onToggle(domain, enabled)} style={styles.dataManagementSectionItem} />)}</View>;
  };
  // The list's own line: what is chosen now, and the tool that chooses it all.
  const sectionListHeader = (summary: string, tools?: React.ReactNode): React.JSX.Element => <View style={styles.dataManagementSelectionBar}><Text numberOfLines={1} style={styles.dataManagementSelectionCount}>{summary}</Text><View style={styles.dataManagementToolbarButtons}>{tools}</View></View>;
  const chooseImportFile = async (): Promise<void> => {
    if (pendingAction === "inspect") return;
    await runPendingAction("inspect", async () => {
      const inspected = await onInspectImport();
      if (!inspected) return;
      const detected = DATA_PACKAGE_DOMAINS.filter((domain) => inspected.detected_sections.includes(domain));
      setImportPreview(inspected);
      setImportSections(detected);
      setStagedSections([]);
    });
  };
  const importSelected = async (): Promise<void> => {
    if (pendingAction === "import") return;
    if (!importPreview || importSections.length === 0) return;
    if (replacingDraftSections.length > 0) {
      const labels = DATA_PACKAGE_SECTIONS.filter(({ domain }) => replacingDraftSections.includes(domain)).map(({ labelKey }) => translate(labelKey));
      if (!await onConfirmImportReplace(labels)) return;
    }
    await runPendingAction("import", async () => {
      const imported = await onImport(importSections);
      if (!imported) {
        setImportPreview(undefined);
        setImportSections([]);
        setStagedSections([]);
        return;
      }
      setStagedSections(DATA_PACKAGE_DOMAINS.filter((domain) => imported.draft_domains.includes(domain)));
    });
  };
  const selectionTool = (selectedCount: number, availableCount: number, onSelectAll: () => void, onDeselectAll: () => void): React.JSX.Element => {
    const allSelected = availableCount > 0 && selectedCount === availableCount;
    return <ActionButton title={translate(allSelected ? "dataManagement.deselectAll" : "dataManagement.selectAll")} disabled={busy || availableCount === 0} onPress={allSelected ? onDeselectAll : onSelectAll} />;
  };
  return <View style={styles.dataManagementWorkspace}>
    <SettingsRail
      rows={dataManagementTabRows}
      selectedKey={tab}
      onSelectionChange={(key) => { if (key) switchDataManagementTab(key as DataManagementTab); }}
    />
    <View style={styles.settingsRailDetail}>
    {/* One shared header band names the active tab, exactly as the runtime
        and external panes name their selection, so the pane body below it
        opens straight on its content instead of a second heading. */}
    <SettingsDetailHeader title={dataManagementTabRows.find((row) => row.key === tab)?.cells[0] ?? ""} />
    {tab === "import" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>
      {!importPreview ? <View style={[styles.dataManagementImportIntro, dataManagementPolishStyles.importIntro]}><View style={styles.dataManagementImportFileRow}><Text style={styles.dataManagementImportFileLabel}>{translate("dataManagement.importFile")}</Text><View style={styles.dataManagementImportFileValue}><Text numberOfLines={1} style={styles.dataManagementImportFilePlaceholder}>{translate("dataManagement.noImportFile")}</Text></View><ActionButton title={translate("dataManagement.chooseImportFile")} busy={pendingAction === "inspect"} disabled={controlsBusy("inspect")} onPress={() => { void chooseImportFile(); }} /></View><Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.importHint")}</Text></View> : null}
      {importReviewReady ? <>
        <Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.importRecognizedHint")}</Text>
        <View style={SETTINGS_FIELD}>
          <View style={SETTINGS_FIELD_ROW_INDENTED}>
            <Text style={SETTINGS_FIELD_LABEL}>{translate("dataManagement.sections")}</Text>
            <View style={styles.dataManagementSectionsField}>
              {sectionListHeader(`${translate("dataManagement.importDetectedCount", { count: detectedImportSections.length })} · ${translate("dataManagement.selectedCount", { count: importSections.length })}`, <><ActionButton title={translate("dataManagement.changeImportFile")} busy={pendingAction === "inspect"} disabled={controlsBusy("inspect")} onPress={() => { void chooseImportFile(); }} />{selectionTool(importSections.length, detectedImportSections.length, () => setImportSections([...detectedImportSections]), () => setImportSections([]))}</>)}
              {sectionList(detectedImportSections, importSections, busy, (domain, enabled) => toggleSection(setImportSections, domain, enabled))}
            </View>
          </View>
          {replacingDraftSections.length > 0 ? <View style={SETTINGS_FIELD_HELP_SLOT}><Text numberOfLines={2} style={styles.dataManagementSensitiveHint}>{translate("dataManagement.importReplaceDraftWarning", { sections: DATA_PACKAGE_SECTIONS.filter(({ domain }) => replacingDraftSections.includes(domain)).map(({ labelKey }) => translate(labelKey)).join(" · ") })}</Text></View> : null}
          {actionRow(<ActionButton primary title={translate("dataManagement.importSelected")} busy={pendingAction === "import"} disabled={controlsBusy("import") || importSections.length === 0} onPress={() => { void importSelected(); }} />, statuses.import)}
        </View>
      </> : null}
      {stagedSections.length > 0 ? <View style={SETTINGS_FIELD}>
        <View style={SETTINGS_FIELD_ROW_INDENTED}>
          <Text style={SETTINGS_FIELD_LABEL}>{translate("dataManagement.sections")}</Text>
          <View style={styles.dataManagementSectionsField}>
            {/* A staged import is this pane's result, not its end: the same
                choose-file control stays beside it, so a second package can be
                opened without leaving the pane. */}
            {sectionListHeader(statuses.import ?? translate("dataManagement.selectedCount", { count: stagedSections.length }), <ActionButton title={translate("dataManagement.changeImportFile")} busy={pendingAction === "inspect"} disabled={controlsBusy("inspect")} onPress={() => { void chooseImportFile(); }} />)}
            {sectionList(stagedSections, stagedSections, true, () => undefined)}
          </View>
        </View>
      </View> : null}
    </PersistentScrollView> : null}
    {tab === "export" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>
      <Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.exportHint")}</Text>
      <View style={SETTINGS_FIELD}>
        <View style={SETTINGS_FIELD_ROW_INDENTED}>
          <Text style={SETTINGS_FIELD_LABEL}>{translate("dataManagement.sections")}</Text>
          <View style={styles.dataManagementSectionsField}>
            {sectionListHeader(translate("dataManagement.selectedCount", { count: exportSections.length }), selectionTool(exportSections.length, DATA_PACKAGE_DOMAINS.length, () => setExportSections([...DATA_PACKAGE_DOMAINS]), () => setExportSections([])))}
            {sectionList(DATA_PACKAGE_DOMAINS, exportSections, busy, (domain, enabled) => toggleSection(setExportSections, domain, enabled))}
          </View>
        </View>
        {actionRow(<ActionButton primary title={translate("dataManagement.exportSelected")} busy={pendingAction === "export"} disabled={controlsBusy("export") || exportSections.length === 0} onPress={() => { void runPendingAction("export", () => onExport(exportSections)); }} />, statuses.export)}
        {/* The standing warning is this field's tip, where every pane explains
            the row it draws. */}
        <View style={SETTINGS_FIELD_HELP_SLOT}><Text numberOfLines={2} style={SETTINGS_FIELD_HELP_TEXT}>{translate("dataManagement.sensitiveHint")}</Text></View>
      </View>
    </PersistentScrollView> : null}
    {tab === "webdav" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>
      <Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.webdavHint")}</Text>
      <WebDavWorkspace snapshot={snapshot} busy={busy || webDavOperationBusy} pendingAction={pendingAction} webDavBusy={webDavBusy} actionRow={actionRow} onProbe={() => { void runPendingAction("probe", onProbeWebDav); }} onSync={(direction) => { void runPendingAction("sync", () => onSyncWebDav(direction)); }} status={statuses.webdav} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onCommitSetting={(patch) => { void onCommitWebDavSettings(patch); }} />
    </PersistentScrollView> : null}
    </View>
  </View>;
}

function RuntimeField({ item, busy, translate, dispatch, onSecretState, clearSecret, dshSyncToken }: { item: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; dshSyncToken: string }): React.JSX.Element {
  const [jsonResetToken, setJsonResetToken] = useState(0);
  const [valueError, setValueError] = useState<string>();
  const [resetToken, setResetToken] = useState(0);
  const key = identifier(item);
  const previousDshSyncToken = useRef(dshSyncToken);
  useEffect(() => {
    if (key === DSH_VISION_ROUTER_CONFIG_KEY && previousDshSyncToken.current !== dshSyncToken) {
      setJsonResetToken((current) => current + 1);
    }
    previousDshSyncToken.current = dshSyncToken;
  }, [dshSyncToken, key]);
  const label = runtimeFieldLabel(key, stringValue(item.label, key), translate);
  const kind = stringValue(item.kind, "text");
  const storageKind = stringValue(item.storage_kind, kind);
  const value = stringValue(item.value);
  const unit = runtimeUnitLabel(stringValue(item.unit), translate);
  const isBoolean = kind === "boolean" || kind === "toggle" || kind === "bool" || kind === "bool_auto";
  const isSelect = kind === "select" || kind === "choice" || kind === "enum";
  const isMultiline = kind === "json";
  const numericKind = ["number", "integer", "int", "float", "mb", "optional_int", "optional_float", "optional_mb"].includes(storageKind);
  const rawDefaultValue = DSH_VISION_ROUTER_QUICK_DEFAULTS[key] ?? stringValue(item.default);
  const booleanDefaultValue = rawDefaultValue === "1" || rawDefaultValue.toLowerCase() === "true";
  // A setting reads as modified only when it holds an explicit non-default
  // value; an empty optional field means "use the default" and is not a change.
  const modified = value !== "" && (isBoolean
    ? (booleanValue(item.value) ? "1" : "0") !== (booleanDefaultValue ? "1" : "0")
    : value !== rawDefaultValue);
  const minimum = numberValue(item.minimum, Number.NaN);
  const maximum = numberValue(item.maximum, Number.NaN);
  // Commit-time validation: an invalid draft stays in the field with an inline
  // message instead of reaching Core, so a typo can never be applied silently.
  const validate = numericKind ? (next: string): string | undefined => {
    const trimmed = next.trim();
    if (trimmed === "") return storageKind.startsWith("optional") ? undefined : translate("runtime.invalidNumber");
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed)) return translate("runtime.invalidNumber");
    if ((storageKind === "integer" || storageKind === "int") && !/^-?\d+$/.test(trimmed)) return translate("runtime.invalidInteger");
    if ((Number.isFinite(minimum) && parsed < minimum) || (Number.isFinite(maximum) && parsed > maximum)) {
      return translate("runtime.outOfRange", { min: String(minimum), max: String(maximum) });
    }
    return undefined;
  } : undefined;
  const resetToDefault = (): void => {
    setResetToken((current) => current + 1);
    void dispatch("set_setting", { key, value: isBoolean ? booleanDefaultValue : rawDefaultValue });
  };
  let control: React.ReactNode;
  let action: React.ReactNode;
  if (isBoolean) {
    control = <NativeToggle value={booleanValue(item.value)} disabled={busy} accessibilityLabel={label} onValueChange={(next) => dispatch("set_setting", { key, value: kind === "bool_auto" ? (next ? "auto" : "off") : next })} style={styles.runtimeBooleanControl} />;
  } else if (isSelect) {
    const optionValues = stringList(item.options);
    const visibleOptionValues = optionValues.filter((option) => !(DSH_VISION_ROUTER_QUICK_KEYS.some((quickKey) => quickKey === key) && option === "inherit"));
    const optionLabels = visibleOptionValues.map((option) => runtimeOptionLabel(key, option, translate));
    const selectedIndex = visibleOptionValues.indexOf(value);
    control = <NativePicker labels={optionLabels} selectedValue={optionLabels[selectedIndex] ?? optionLabels[0] ?? ""} disabled={busy} onChange={({ nativeEvent }) => { const next = optionValues[nativeEvent.index]; const visibleNext = visibleOptionValues[nativeEvent.index] ?? next; if (visibleNext !== undefined) void dispatch("set_setting", { key, value: visibleNext }); }} style={styles.runtimeValueControl} />;
  } else if (isMultiline) {
    // JSON may contain provider credentials. Keep the document in the native
    // editor and stage it through the one-time Core secret capability while
    // still giving the user a real multiline textarea.
    control = <NativeSecretInputControl label={label} hint={item.retained === true ? translate("runtime.secretRetained") : translate("runtime.jsonPlaceholder")} busy={busy} domain="runtime" field="setting" target={key} multiline plainText autoCommit resetToken={jsonResetToken} onSecretState={onSecretState} />;
    action = <ActionButton title={item.will_clear === true ? translate("common.willClear") : translate("common.clear")} disabled={busy || item.retained !== true || item.will_clear === true} onPress={() => { void clearSecret({ domain: "runtime", field: "setting", target: key }).then(() => setJsonResetToken((current) => current + 1)); }} />;
  } else if (item.secret === true) {
    control = <NativeSecretInputControl label={label} hint={item.retained === true ? translate("runtime.secretRetained") : undefined} busy={busy} domain="runtime" field="setting" target={key} onSecretState={onSecretState} setTitle={translate("common.set")} />;
    action = <ActionButton title={item.will_clear === true ? translate("common.willClear") : translate("common.clear")} disabled={busy || item.retained !== true || item.will_clear === true} onPress={() => clearSecret({ domain: "runtime", field: "setting", target: key })} />;
  } else {
    control = <RuntimeValueField label={label} value={value} keyboardType={numericKind ? "numeric" : undefined} validate={validate} resetToken={resetToken} resetValue={rawDefaultValue} onCommit={(next) => dispatch("set_setting", { key, value: next })} onErrorChange={setValueError} />;
  }
  // Keep the reset clickable while an apply is in flight: the dispatch queue
  // serializes it, and a disabled button here reads as a dead control.
  const resetAction = modified && action === undefined
    ? <NativeButton title="" symbol="refresh" compact toolTip={`${translate("runtime.resetToDefault")} (${rawDefaultValue || translate("common.empty")})`} accessibilityLabel={translate("runtime.resetToDefault")} onPress={resetToDefault} style={styles.runtimeResetButton} />
    : null;
  if (isMultiline) {
    return <View style={[styles.runtimeField, styles.runtimeMultilineField]}>
      <View style={styles.runtimeMultilineHeader}>
        <View style={[styles.runtimeModifiedBarInline, modified && styles.runtimeModifiedBarActive]} />
        <Text style={styles.runtimeMultilineLabel} accessibilityLabel={label}>{label}</Text>
        {action ? <View style={styles.runtimeMultilineHeaderActions}>{action}</View> : null}
      </View>
      <View style={styles.runtimeMultilineEditor}>{control}</View>
      <RuntimeFieldMeta item={item} translate={translate} multiline />
    </View>;
  }
  return <View style={styles.runtimeField}>
    <View style={styles.runtimeInputRow}>
      <View style={[styles.runtimeModifiedBar, modified && styles.runtimeModifiedBarActive]} />
      <TooltipText numberOfLines={1} tooltip={label} style={styles.runtimeFieldLabel} accessibilityLabel={label}>{label}</TooltipText>
      <View style={styles.runtimeValueSlot}>{control}</View>
      {!isBoolean && unit ? <Text numberOfLines={1} style={styles.runtimeUnit}>{unit}</Text> : null}
      <View style={styles.runtimeActionSlot}>{action ?? resetAction}</View>
    </View>
    {valueError ? <Text style={styles.runtimeFieldError}>{valueError}</Text> : null}
    <RuntimeFieldMeta item={item} translate={translate} />
  </View>;
}

function RuntimeFieldMeta({ item, translate, multiline = false }: { item: UnknownRecord; translate: Translate; multiline?: boolean }): React.JSX.Element {
  const key = identifier(item);
  const kind = stringValue(item.kind, "text");
  const rawDefaultValue = DSH_VISION_ROUTER_QUICK_DEFAULTS[key] ?? stringValue(item.default, translate("common.empty"));
  // Boolean settings carry "1"/"0" in the schema; show the switch state a
  // user actually reads instead of the raw flag.
  const booleanDefault = rawDefaultValue === "1" || rawDefaultValue.toLowerCase() === "true";
  const defaultValue = kind === "boolean" || kind === "toggle" || kind === "bool" || kind === "bool_auto"
    ? translate(booleanDefault ? "common.enabled" : "common.disabled")
    : kind === "select" || kind === "choice" || kind === "enum"
      ? runtimeOptionLabel(key, rawDefaultValue, translate)
      : rawDefaultValue;
  const help = runtimeFieldHelp(key, stringValue(item.help), translate);
  if (multiline) {
    return <View style={[styles.runtimeHelpSlot, styles.runtimeMultilineHelpSlot]}>
      <Text style={styles.runtimeJsonDefaultHint}>{translate("runtime.jsonDefaultHint")}</Text>
      {help ? <Text style={styles.runtimeHelpText}>{help}</Text> : null}
    </View>;
  }
  // The full tip is always visible: it wraps under the row instead of being
  // clipped into a tooltip-only single line.
  const meta = `${translate("common.default")}: ${defaultValue}${help ? ` · ${help}` : ""}`;
  return <View style={styles.runtimeHelpSlot}><Text style={styles.runtimeHelpText}>{meta}</Text></View>;
}

type PendingTextFieldState = {
  draft: string;
  error?: string;
  onChangeText: (next: string) => void;
  commit: () => Promise<void>;
  reset: () => void;
  /** Replace the local draft with an explicit value (reset-to-default). */
  resetTo: (next: string) => void;
  isDirty: () => boolean;
  hasError: () => boolean;
};

// Keep ordinary text local across every settings surface while typing. Blur,
// submit, and Apply are the only commit points, so active typing never waits
// for a Core dispatch or a full snapshot publication. A validator runs at the
// commit point only: an invalid draft stays local, keeps the row dirty, and
// surfaces an inline error instead of being written to Core.
function usePendingTextField(value: string, onCommit: (next: string) => void | Promise<void>, label: string, onDraftChange?: (next: string) => void, validate?: (next: string) => string | undefined): PendingTextFieldState {
  const [draft, setDraft] = useState(value);
  const [error, setError] = useState<string>();
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(label));
  const draftRef = useRef(value);
  const committedRef = useRef(value);
  const valueRef = useRef(value);
  const dirtyRef = useRef(false);
  const errorRef = useRef<string | undefined>(undefined);
  const commitInFlight = useRef<Promise<void> | undefined>(undefined);
  const onCommitRef = useRef(onCommit);
  const onDraftChangeRef = useRef(onDraftChange);
  const validateRef = useRef(validate);

  useEffect(() => { onCommitRef.current = onCommit; }, [onCommit]);
  useEffect(() => { onDraftChangeRef.current = onDraftChange; }, [onDraftChange]);
  useEffect(() => { validateRef.current = validate; }, [validate]);
  useEffect(() => {
    valueRef.current = value;
    if (!dirtyRef.current) {
      committedRef.current = value;
      draftRef.current = value;
      setDraft(value);
      onDraftChangeRef.current?.(value);
    }
  }, [value]);

  const setFieldError = useCallback((next: string | undefined): void => {
    errorRef.current = next;
    setError(next);
  }, []);

  const setDirty = useCallback((dirty: boolean): void => {
    dirtyRef.current = dirty;
    registry?.setDirty(fieldId.current, dirty);
  }, [registry]);

  const commit = useCallback(async (): Promise<void> => {
    while (dirtyRef.current) {
      const existing = commitInFlight.current;
      if (existing) {
        await existing;
        continue;
      }
      const submitted = draftRef.current;
      const validationMessage = validateRef.current?.(submitted);
      if (validationMessage) {
        // Keep the draft and the dirty flag so the user can correct it; the
        // row stays visibly unsaved until the value validates.
        setFieldError(validationMessage);
        return;
      }
      setFieldError(undefined);
      const operation = Promise.resolve(onCommitRef.current(submitted)).then(() => {
        committedRef.current = submitted;
        if (draftRef.current === submitted) setDirty(false);
      });
      commitInFlight.current = operation;
      try {
        await operation;
      } finally {
        if (commitInFlight.current === operation) commitInFlight.current = undefined;
      }
    }
  }, [setDirty, setFieldError]);

  const onChangeText = useCallback((next: string): void => {
    draftRef.current = next;
    setDraft(next);
    onDraftChangeRef.current?.(next);
    // Clear a stale inline error while the user edits; the next commit
    // re-validates before anything reaches Core.
    if (errorRef.current !== undefined) setFieldError(undefined);
    // If an earlier staged value is still in flight, returning to the last
    // known draft still needs one more stage after that write finishes.
    setDirty(next !== committedRef.current || commitInFlight.current !== undefined);
  }, [setDirty, setFieldError]);

  const reset = useCallback((): void => {
    const next = valueRef.current;
    committedRef.current = next;
    draftRef.current = next;
    setDraft(next);
    onDraftChangeRef.current?.(next);
    setFieldError(undefined);
    setDirty(false);
  }, [setDirty, setFieldError]);

  // Reset-to-default must clear an uncommitted local draft too, otherwise the
  // field keeps showing the old value after the Core value returns to default.
  const resetTo = useCallback((next: string): void => {
    committedRef.current = next;
    draftRef.current = next;
    setDraft(next);
    onDraftChangeRef.current?.(next);
    setFieldError(undefined);
    setDirty(false);
  }, [setDirty, setFieldError]);

  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset, isDirty: () => dirtyRef.current, hasError: () => errorRef.current !== undefined, flushBeforeAssistantEditor: true });
    return () => {
      // Selection changes can remove the editor before AppKit/WinUI delivers
      // its blur event. Preserve the local draft instead of silently dropping
      // it when the field leaves the tree.
      if (dirtyRef.current) void commit().catch(() => undefined);
      registry?.register(fieldId.current);
    };
  }, [commit, registry, reset]);

  return { draft, error, onChangeText, commit, reset, resetTo, isDirty: () => dirtyRef.current, hasError: () => errorRef.current !== undefined };
}

function RuntimeValueField({ label, value, keyboardType, validate, resetToken = 0, resetValue = "", onCommit, onErrorChange }: { label: string; value: string; keyboardType?: "default" | "numeric"; validate?: (next: string) => string | undefined; resetToken?: number; resetValue?: string; onCommit: (value: string) => void | Promise<void>; onErrorChange?: (message?: string) => void }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label, undefined, validate);
  useEffect(() => { onErrorChange?.(field.error); }, [field.error, onErrorChange]);
  // A reset click replaces any uncommitted draft with the default value.
  useEffect(() => { if (resetToken > 0) field.resetTo(resetValue); }, [resetToken]);
  return <NativeTextField style={[styles.input, styles.runtimeValueControl, field.error !== undefined && styles.runtimeValueControlInvalid]} value={field.draft} onChangeText={field.onChangeText} onBlur={() => { void field.commit().catch(() => undefined); }} onSubmitEditing={() => { void field.commit().catch(() => undefined); }} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />;
}

/**
 * The WebDAV fields, drawn on the one settings grid every other pane uses:
 * the enable switch, the connection fields, the sync scope and direction, and
 * the pane's two actions as the last field row.  The pane's work reports in the
 * window's one status strip, so no footer bar carries it.
 */
function WebDavWorkspace({ snapshot, busy, pendingAction, webDavBusy, actionRow, onProbe, onSync, status, translate, dispatch, onSecretState, onCommitSetting }: { snapshot?: CoreSnapshot; busy: boolean; pendingAction?: DataManagementAction; webDavBusy: (action: "probe" | "sync") => boolean; actionRow: (controls: React.ReactNode, result?: string, hint?: string) => React.JSX.Element; onProbe: () => void; onSync: (action: WebDavSyncAction) => void; status?: string; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onCommitSetting: (patch: UnknownRecord) => void }): React.JSX.Element {
  const state = domainState(snapshot, "webdav");
  // The scope sentence names the sections Core actually syncs, so it cannot
  // drift from WEBDAV_SYNC_DOMAINS the way a hand-written copy would.
  const syncScope = DATA_PACKAGE_SECTIONS.filter(({ domain }) => WEBDAV_SYNC_DOMAINS.includes(domain)).map(({ labelKey }) => translate(labelKey)).join(" · ");
  // When the last sync landed, from the stamp Core keeps for it.
  const lastSyncAt = stringValue(asRecord(asRecord(state.last_sync)).at);
  const lastSync = lastSyncAt === "" ? undefined : translate("dataManagement.lastSync", { time: formatActionTimestamp(lastSyncAt) });
  // The direction is saved, not per-window state: the interval loop syncs the
  // way this field says, so an automatic run cannot disagree with the button.
  const syncOptions = WEBDAV_SYNC_OPTIONS.map((option) => ({ ...option, title: translate(option.labelKey) }));
  const savedDirection = stringValue(state.sync_direction);
  const selectedSync = syncOptions.find(({ id }) => id === (savedDirection === "smart" ? "sync" : savedDirection)) ?? syncOptions[0];
  // What the last recorded run did, so a window opened after an automatic
  // failure says why the stamp stopped instead of looking merely stale.
  const lastResult = asRecord(state.last_result);
  const lastFailed = lastResult.ok === false && ["sync", "push", "pull"].includes(stringValue(lastResult.action));
  const actionHint = lastFailed && lastSyncAt !== ""
    ? translate("dataManagement.lastSyncFailed", { time: formatActionTimestamp(lastSyncAt) })
    : lastSync;
  return <View style={styles.webdavFieldList}>
    {/* The switch carries its own control style, the way every other switch
        row in the app does: a wrapper view does not size an unstyled native
        toggle, so the pill drew a row above its own label.  The row states
        the setting only — the connection state is this pane's work, and the
        window's strip reports it (webdav.probeOk / webdav.probeFailed). */}
    <View style={SETTINGS_FIELD_ROW_INDENTED}>
      <Text style={SETTINGS_FIELD_LABEL}>{translate("webdav.enabled")}</Text>
      <NativeToggle accessibilityLabel={translate("webdav.enabled")} value={booleanValue(state.enabled)} disabled={busy} onValueChange={(enabled) => onCommitSetting({ enabled })} style={SETTINGS_FIELD_SWITCH_SLOT} />
    </View>
    <TextField label={translate("webdav.url")} value={stringValue(state.url)} labelWidth={SETTINGS_FIELD_LABEL_WIDTH} controlWidth={SETTINGS_FIELD_VALUE_WIDTH} style={SETTINGS_FIELD_ROW_INDENTED} onCommit={(url) => onCommitSetting({ url })} />
    <TextField label={translate("webdav.username")} value={stringValue(state.username)} labelWidth={SETTINGS_FIELD_LABEL_WIDTH} controlWidth={SETTINGS_FIELD_VALUE_WIDTH} style={SETTINGS_FIELD_ROW_INDENTED} onCommit={(username) => onCommitSetting({ username })} />
    <WebDavPasswordField configured={snapshot?.webdav.password.present === true} busy={busy} translate={translate} onSecretState={onSecretState} />
    <View style={SETTINGS_FIELD}>
      <TextField label={translate("webdav.remoteFile")} value={stringValue(state.remote_name)} labelWidth={SETTINGS_FIELD_LABEL_WIDTH} controlWidth={SETTINGS_FIELD_VALUE_WIDTH} style={SETTINGS_FIELD_ROW_INDENTED} onCommit={(remote_name) => onCommitSetting({ remote_name })} />
      <View style={SETTINGS_FIELD_HELP_SLOT}><Text style={SETTINGS_FIELD_HELP_TEXT}>{translate("webdav.remoteFileHint")}</Text></View>
    </View>
    <View style={SETTINGS_FIELD}>
      <TextField label={translate("webdav.syncEvery")} value={stringValue(state.sync_interval)} labelWidth={SETTINGS_FIELD_LABEL_WIDTH} controlWidth={SETTINGS_FIELD_VALUE_WIDTH} style={SETTINGS_FIELD_ROW_INDENTED} suffix={translate("webdav.minutes")} hintStyle={styles.webdavFieldUnit} keyboardType="numeric" onCommit={(sync_interval) => onCommitSetting({ sync_interval })} />
      {/* The interval is the automatic sync's cadence, so the row says when it
          runs and what 0 means instead of leaving the number unexplained. */}
      <View style={SETTINGS_FIELD_HELP_SLOT}><Text style={SETTINGS_FIELD_HELP_TEXT}>{translate("webdav.syncEveryHint")}</Text></View>
    </View>
    <TextField label={translate("webdav.httpTimeout")} value={stringValue(state.timeout)} labelWidth={SETTINGS_FIELD_LABEL_WIDTH} controlWidth={SETTINGS_FIELD_VALUE_WIDTH} style={SETTINGS_FIELD_ROW_INDENTED} suffix={translate("webdav.seconds")} hintStyle={styles.webdavFieldUnit} keyboardType="numeric" onCommit={(timeout) => onCommitSetting({ timeout })} />
    <View style={SETTINGS_FIELD}>
      <View style={SETTINGS_FIELD_ROW_INDENTED}>
        <Text style={SETTINGS_FIELD_LABEL}>{translate("dataManagement.webdavScope")}</Text>
        <Text numberOfLines={1} style={styles.dataManagementSyncScopeValue}>{syncScope}</Text>
      </View>
    </View>
    <View style={SETTINGS_FIELD}>
      <View style={SETTINGS_FIELD_ROW_INDENTED}>
        <Text style={SETTINGS_FIELD_LABEL}>{translate("dataManagement.syncDirection")}</Text>
        <NativePicker labels={syncOptions.map(({ title }) => title)} selectedValue={selectedSync.title} disabled={busy} onChange={({ nativeEvent }) => { const option = syncOptions[nativeEvent.index]; if (option) onCommitSetting({ sync_direction: option.id === "sync" ? "smart" : option.id }); }} style={styles.dataManagementDirectionPicker} />
      </View>
      {/* The chosen direction states what it will do, so the picker and its
          consequence read as one field. */}
      <View style={SETTINGS_FIELD_HELP_SLOT}><Text style={SETTINGS_FIELD_HELP_TEXT}>{translate(selectedSync.hintKey)}</Text></View>
    </View>
    {actionRow(<><ActionButton title={translate("dataManagement.testConnection")} busy={pendingAction === "probe"} disabled={webDavBusy("probe")} onPress={onProbe} /><ActionButton primary title={translate("dataManagement.syncNow")} busy={pendingAction === "sync"} disabled={webDavBusy("sync") || snapshot?.webdav.enabled !== true} onPress={() => onSync(selectedSync.id)} /></>, status, actionHint)}
  </View>;
}

function WebDavPasswordField({ configured, busy, translate, onSecretState }: { configured: boolean; busy: boolean; translate: Translate; onSecretState: (state: SecretState) => void }): React.JSX.Element {
  const [commitRequest, setCommitRequest] = useState(0);
  const [resetRequest, setResetRequest] = useState(0);
  const [status, setStatus] = useState("ready");
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol("WebDAV Password"));
  const dirtyRef = useRef(false);
  const commitSequence = useRef(0);
  const pendingCommit = useRef<{ request: number; resolve: () => void; reject: (reason: Error) => void } | undefined>(undefined);
  const requestCommit = useCallback((): Promise<void> => {
    const request = commitSequence.current + 1;
    commitSequence.current = request;
    return new Promise<void>((resolve, reject) => {
      pendingCommit.current?.resolve();
      pendingCommit.current = { request, resolve, reject };
      setCommitRequest(request);
    });
  }, []);
  const reset = useCallback((): void => {
    dirtyRef.current = false;
    registry?.setDirty(fieldId.current, false);
    setResetRequest((current) => current + 1);
  }, [registry]);
  useEffect(() => {
    registry?.register(fieldId.current, { commit: requestCommit, reset, isDirty: () => dirtyRef.current });
    return () => {
      pendingCommit.current?.resolve();
      pendingCommit.current = undefined;
      registry?.register(fieldId.current);
    };
  }, [registry, requestCommit, reset]);
  return <View style={SETTINGS_FIELD_ROW_INDENTED}><Text style={SETTINGS_FIELD_LABEL}>{translate("webdav.password")}</Text><NativeSecureTextInput domain="webdav" field="password" label={translate("webdav.password")} placeholder={configured ? translate("webdav.passwordHintConfigured") : translate("webdav.passwordHintOptional")} disabled={busy || status === "saving"} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(state) => {
    setStatus(state.status);
    if (state.status === "dirty") {
      dirtyRef.current = true;
      registry?.setDirty(fieldId.current, true);
    } else if (state.status === "saved" || state.status === "ready" || state.status === "error") {
      dirtyRef.current = false;
      registry?.setDirty(fieldId.current, false);
    }
    const pending = pendingCommit.current;
    if (pending && state.commitRequest >= pending.request && state.status !== "saving" && state.status !== "dirty") {
      pendingCommit.current = undefined;
      if (state.status === "error") pending.reject(new Error(state.error || "WebDAV password could not be staged"));
      else pending.resolve();
    }
    if (state.status === "saved") {
      setResetRequest((current) => current + 1);
      onSecretState(state);
    }
  }} style={styles.webdavPasswordInput} /></View>;
}

type RouteTraceAttempt = {
  label: string;
  state: "selected" | "failed" | "attempted";
  detail: string;
  time: string;
};

type RenderedLogRecord = {
  key: string;
  requestKey: string;
  routeAttempts: RouteTraceAttempt[];
  time: string;
  source: string;
  status: string;
  model: string;
  upstreamModel: string;
  provider: string;
  apiKeyName: string;
  event: string;
  action: string;
  duration: string;
  tokens: string;
  detail: string;
  original: string;
};

type RouteTraceRequest = {
  key: string;
  time: string;
  model: string;
  attempts: RouteTraceAttempt[];
  rows: RenderedLogRecord[];
  routePath: string;
  outcome: "direct" | "fallback" | "failed" | "unavailable";
};

type ActiveLogView = { tab: LogTab; log: LogView };

type LogColumn = { label: string; width: number; flex?: boolean; value: (row: RenderedLogRecord) => string };

function shortLogTimestamp(value: unknown): string {
  const raw = stringValue(value);
  if (!raw) return "";
  const numeric = Number(raw);
  const parsed = Number.isFinite(numeric) && /^[-+]?\d+(?:\.\d+)?$/.test(raw.trim())
    ? new Date(Math.abs(numeric) < 100_000_000_000 ? numeric * 1000 : numeric)
    : new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw;
  const pad = (part: number): string => String(part).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}

function compactLogValue(value: unknown): string {
  if (typeof value === "string") return value.replace(/\s+/g, " ").trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(compactLogValue).filter(Boolean).join(", ");
  if (value && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return "";
}

function compactUpstreamLogModel(value: unknown): string {
  const model = compactLogValue(value);
  const separator = model.indexOf("/");
  return separator >= 0 ? model.slice(separator + 1) : model;
}

function formatLogDuration(value: string): string {
  // Display-only: milliseconds are recorded raw and shown as seconds with
  // at most one decimal place. The "(s)" unit lives in the column header.
  if (!value) return "";
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return value;
  return `${Math.round(milliseconds / 100) / 10}`;
}

function formatLogTokens(value: string): string {
  // Display-only: token counts are recorded raw and shown in kilo tokens
  // with at most one decimal place. The "(k)" unit lives in the column
  // header, e.g. 12000 -> "12", 500 -> "0.5".
  if (!value) return "";
  const tokens = Number(value);
  if (!Number.isFinite(tokens)) return value;
  return `${Math.round(tokens / 100) / 10}`;
}

function routeTraceEventLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    selected_deployment: "logs.routeEvent.selected",
    filter_deployments: "logs.routeEvent.filtered",
    generic_fallback_helper_start: "logs.routeEvent.fallback",
    generic_fallback_helper_error: "logs.routeEvent.fallbackFailed",
    deployment_failover_marked: "logs.routeEvent.failoverMarked",
    same_deployment_protocol_fallback_available: "logs.routeEvent.protocolFallback",
    protocol_fallback_cache_hit: "logs.routeEvent.protocolFallbackCacheHit",
    protocol_fallback_success: "logs.routeEvent.protocolFallbackSuccess",
    protocol_fallback_cache_cleared: "logs.routeEvent.protocolFallbackCleared",
    fallback_deployment_cooldown_filter: "logs.routeEvent.cooldownFilter",
    next_order_fallback_available: "logs.routeEvent.nextOrder",
    final_order_fallback_retry_start: "logs.routeEvent.finalOrder",
    deployment_cooldown_started: "logs.routeEvent.cooldownStarted",
    stream_start_timeout: "logs.routeEvent.streamStartTimeout",
    codex_fast_default_service_tier_injected: "logs.routeEvent.serviceTier",
    responses_request_gzip_enabled: "logs.routeEvent.compression",
    responses_chat_bridge_preemptive: "logs.routeEvent.chatBridge",
    responses_chat_bridge_preemptive_start: "logs.routeEvent.chatBridge",
    responses_chat_bridge_preemptive_retry_start: "logs.routeEvent.chatBridge",
    responses_chat_bridge_preemptive_error: "logs.routeEvent.chatBridge",
    external_web_search_bridge_chat_tool_start: "logs.routeEvent.webSearchToolStart",
    external_web_search_bridge_chat_tool_done: "logs.routeEvent.webSearchToolDone",
    external_web_search_bridge_chat_tool_malformed_retry: "logs.routeEvent.webSearchRetry",
    external_web_search_bridge_chat_tool_progress_retry: "logs.routeEvent.webSearchRetry",
    external_web_search_bridge_actions_executed: "logs.routeEvent.webSearchActions",
    external_web_search_bridge_continuation_start: "logs.routeEvent.webSearchContinuationStart",
    external_web_search_bridge_continuation_done: "logs.routeEvent.webSearchContinuationDone",
    external_web_search_bridge_continuation_error: "logs.routeEvent.webSearchContinuationFailed",
    external_web_search_bridge_empty_continuation_synthesis: "logs.routeEvent.webSearchSynthesisFallback",
    external_web_search_bridge_synthesis_start: "logs.routeEvent.webSearchSynthesisStart",
    external_web_search_bridge_synthesis_done: "logs.routeEvent.webSearchSynthesisDone",
    external_web_search_bridge_synthesis_error: "logs.routeEvent.webSearchSynthesisFailed",
    external_web_search_bridge_synthesis_chat_start: "logs.routeEvent.webSearchSynthesisChatStart",
    external_web_search_bridge_synthesis_chat_done: "logs.routeEvent.webSearchSynthesisChatDone",
    external_web_search_bridge_final_invalid: "logs.routeEvent.webSearchFinalInvalid",
    external_web_search_bridge_initial_no_action_invalid: "logs.routeEvent.webSearchInitialInvalid",
    external_web_search_bridge_model_retry: "logs.routeEvent.webSearchModelRetry",
    route_recovery_poll_start: "logs.routeEvent.recoveryStart",
    route_recovery_poll_waiting_for_cooldown: "logs.routeEvent.recoveryWaiting",
    route_recovery_poll_attempt_start: "logs.routeEvent.recoveryAttempt",
    route_recovery_poll_next_attempt_scheduled: "logs.routeEvent.recoveryRetry",
    route_recovery_poll_success: "logs.routeEvent.recoverySuccess",
    route_recovery_poll_attempt_failed: "logs.routeEvent.recoveryFailed",
    route_recovery_poll_attempt_empty: "logs.routeEvent.recoveryFailed",
    route_recovery_poll_terminal_error: "logs.routeEvent.recoveryEnded",
    route_recovery_poll_max_duration_reached: "logs.routeEvent.recoveryEnded",
    route_recovery_poll_context_size_error: "logs.routeEvent.recoveryEnded",
    route_recovery_poll_route_pool_reset: "logs.routeEvent.recoveryReset",
    standalone_web_search_start: "logs.routeEvent.standaloneWebSearchStart",
    standalone_web_search_completed: "logs.routeEvent.standaloneWebSearchCompleted",
  };
  const key = labels[value];
  if (key) return translate(key);
  if (value.startsWith("external_web_search_bridge_")) return translate("logs.routeEvent.webSearch");
  if (value.startsWith("responses_chat_bridge_")) return translate("logs.routeEvent.chatBridge");
  if (value.startsWith("responses_external_web_search_bridge_")) return translate("logs.routeEvent.webSearch");
  if (value.startsWith("responses_")) return translate("logs.routeEvent.responses");
  if (value.includes("fallback")) return translate("logs.routeEvent.fallback");
  return translate("logs.routeEvent.routeEvent");
}

function routeTraceServiceTierLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    priority: "logs.routeTrace.serviceTierPriority",
    flex: "logs.routeTrace.serviceTierFlex",
    default: "logs.routeTrace.serviceTierDefault",
    standard: "logs.routeTrace.serviceTierDefault",
    auto: "logs.routeTrace.serviceTierAuto",
  };
  return translate(labels[value.trim().toLowerCase()] ?? "logs.routeTrace.serviceTierOther");
}

function routeTraceProtocolLabel(value: string, translate: Translate): string {
  const normalized = value.trim().toLowerCase();
  if (normalized.includes("responses")) return translate("logs.routeTrace.protocolResponses");
  if (normalized.includes("chat")) return translate("logs.routeTrace.protocolChat");
  if (normalized.includes("messages") || normalized.includes("anthropic")) return translate("logs.routeTrace.protocolMessages");
  return translate("logs.routeTrace.protocolOther");
}

const ROUTE_REASON_KEYS: Record<string, Parameters<Translate>[0]> = {
  "upstream-auth-or-balance": "logs.routeTrace.reasonAuth",
  "upstream-compatible-bad-request": "logs.routeTrace.reasonCompatibility",
  "upstream-gateway-bad-request": "logs.routeTrace.reasonGateway",
  "responses-schema-unsupported": "logs.routeTrace.reasonResponses",
  "image-parameter-or-capability-bad-request": "logs.routeTrace.reasonResponses",
  "upstream-network-connectivity": "logs.routeTrace.reasonNetwork",
  "upstream-temporary-class": "logs.routeTrace.reasonTemporary",
  "upstream-temporary-text": "logs.routeTrace.reasonTemporary",
  "terminal-prompt-or-policy": "logs.routeTrace.reasonTerminal",
  "stream_start_timeout": "logs.routeTrace.reasonStreamStartTimeout",
  "stream_idle_timeout": "logs.routeTrace.reasonStreamIdleTimeout",
  "responses_endpoint_unsupported": "logs.routeTrace.reasonResponsesUnsupported",
  "malformed_web_search_function_call": "logs.routeTrace.reasonWebSearchFormat",
  "upstream-stream-incomplete": "logs.routeTrace.reasonStreamIncomplete",
  "upstream-request-body-capacity": "logs.routeTrace.reasonBodyCapacity",
  "image-generation-tool-all-deployments-unsupported": "logs.routeTrace.reasonImageUnsupported",
  "codex-compaction-unsupported": "logs.routeTrace.reasonCompactionUnsupported",
  "image-generation-tool-runtime-fallback": "logs.routeTrace.reasonImageFallback",
  "no-available-deployment": "logs.noAvailableRoute",
  "model-not-configured": "logs.modelNotConfigured",
  other: "logs.routeTrace.reasonUnknown",
};

function upstreamStatusReasonLabel(value: string, translate: Translate): string | undefined {
  const upstreamStatus = value.match(/^upstream-status-(\d+)$/);
  return upstreamStatus?.[1]
    ? translate("logs.routeTrace.upstreamStatus", { status: upstreamStatus[1] })
    : undefined;
}

function routeTraceReasonLabel(value: string, translate: Translate): string {
  const normalized = value.trim();
  return upstreamStatusReasonLabel(normalized, translate)
    ?? translate(ROUTE_REASON_KEYS[normalized.toLowerCase()] ?? "logs.routeTrace.reasonUnknown");
}

function requestErrorReasonLabel(value: string, translate: Translate): string {
  const normalized = value.trim();
  const key = ROUTE_REASON_KEYS[normalized.toLowerCase()];
  // An unmapped upstream reason stays readable instead of collapsing into the
  // generic upstream-error label.
  return upstreamStatusReasonLabel(normalized, translate) ?? (key ? translate(key) : normalized);
}

function requestStatusLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    pending: "logs.sending",
    sending: "logs.sending",
    stream: "logs.streaming",
    success: "logs.success",
    succeeded: "logs.success",
    failure: "logs.failed",
    failed: "logs.failed",
    error: "logs.failed",
    stuck: "logs.stuck",
    aborted: "logs.aborted",
  };
  const key = labels[value.trim().toLowerCase()];
  return key ? translate(key) : value;
}

function logLevelLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    DEBUG: "logs.level.debug",
    INFO: "logs.level.info",
    WARNING: "logs.level.warning",
    ERROR: "logs.level.error",
    CRITICAL: "logs.level.critical",
  };
  const key = labels[value.trim().toUpperCase()];
  return key ? translate(key) : value;
}

function menuActionLabel(value: string, translate: Translate): string {
  const normalized = value.trim();
  const labels: Record<string, Parameters<Translate>[0]> = {
    "open-providers-models": "status.providers",
    "open-runtime-settings": "status.runtime",
    "open-codex-settings": "status.codex",
    "open-claude-settings": "status.claude",
    "open-relay-accounts": "relay.relayAccounts",
    "open-data-management": "status.dataManagement",
    "open-logs": "status.logs",
    "toggle-autostart": "status.autoStart",
    "service-start": "service.start",
    "service-stop": "service.stop",
    "service-restart": "service.restart",
    "service-reload": "service.reload",
    "service-health": "service.health",
    "set-language-system": "language.system",
    "set-language-en": "language.english",
    "set-language-zh-Hans": "language.simplified_chinese",
  };
  const key = labels[normalized] ?? (normalized.startsWith("open-logs?tab=") ? "status.logs" : undefined);
  return key ? translate(key) : normalized;
}

function routeTraceDetailPartLabel(value: string, translate: Translate): string {
  const candidates = value.match(/^candidates=(\d+)$/);
  if (candidates?.[1]) return translate("logs.routeTrace.candidates", { count: candidates[1] });
  const selected = value.match(/^selected=(\d+)$/);
  if (selected?.[1]) return translate("logs.routeTrace.selected", { count: selected[1] });
  const excluded = value.match(/^excluded=(\d+)$/);
  if (excluded?.[1]) return translate("logs.routeTrace.excluded", { count: excluded[1] });
  const failedOrder = value.match(/^failed_order=(.+)$/);
  if (failedOrder?.[1]) return translate("logs.routeTrace.failedOrder", { value: failedOrder[1] });
  const nextOrder = value.match(/^next_order=(.+)$/);
  if (nextOrder?.[1]) return translate("logs.routeTrace.nextOrder", { value: nextOrder[1] });
  const retry = value.match(/^retry=(.+)$/);
  if (retry?.[1]) return translate("logs.routeTrace.retry", { value: retry[1] });
  const maxRetries = value.match(/^max_retries=(.+)$/);
  if (maxRetries?.[1]) return translate("logs.routeTrace.maxRetries", { value: maxRetries[1] });
  const retryDelay = value.match(/^retry_delay=(.+)s$/);
  if (retryDelay?.[1]) return translate("logs.routeTrace.retryDelay", { value: retryDelay[1] });
  const protocol = value.match(/^protocol=(.+)$/);
  if (protocol?.[1]) return routeTraceProtocolLabel(protocol[1], translate);
  const fromProtocol = value.match(/^from_protocol=(.+)$/);
  if (fromProtocol?.[1]) return translate("logs.routeTrace.fallbackFromProtocol", { value: routeTraceProtocolLabel(fromProtocol[1], translate) });
  const fallbackProtocol = value.match(/^fallback_protocol=(.+)$/);
  if (fallbackProtocol?.[1]) return translate("logs.routeTrace.fallbackToProtocol", { value: routeTraceProtocolLabel(fallbackProtocol[1], translate) });
  const ttl = value.match(/^ttl=(.+)s$/);
  if (ttl?.[1]) return translate("logs.routeTrace.protocolMemory", { value: ttl[1] });
  const remaining = value.match(/^remaining=(.+)s$/);
  if (remaining?.[1]) return translate("logs.routeTrace.protocolRemaining", { value: remaining[1] });
  if (value === "stream=true") return translate("logs.routeTrace.streaming");
  if (value === "stream=false") return translate("logs.routeTrace.nonStreaming");
  const cooling = value.match(/^cooling=(\d+)$/);
  if (cooling?.[1]) return translate("logs.routeTrace.cooling", { count: cooling[1] });
  if (value === "all_cooled=true") return translate("logs.routeTrace.allCooled");
  if (value === "all_cooled=false") return translate("logs.routeTrace.usableRoutesRemain");
  const originalBytes = value.match(/^original_bytes=(\d+)$/);
  if (originalBytes?.[1]) return translate("logs.routeTrace.originalBytes", { value: originalBytes[1] });
  const compressedBytes = value.match(/^compressed_bytes=(\d+)$/);
  if (compressedBytes?.[1]) return translate("logs.routeTrace.compressedBytes", { value: compressedBytes[1] });
  if (value === "phase=initial") return translate("logs.routeTrace.phaseInitial");
  if (value === "phase=continuation") return translate("logs.routeTrace.phaseContinuation");
  if (value === "phase=synthesis") return translate("logs.routeTrace.phaseSynthesis");
  const actions = value.match(/^actions=(\d+)$/);
  if (actions?.[1]) return translate("logs.routeTrace.actions", { count: actions[1] });
  const sources = value.match(/^sources=(\d+)$/);
  if (sources?.[1]) return translate("logs.routeTrace.sources", { count: sources[1] });
  const evidence = value.match(/^evidence=(\d+)$/);
  if (evidence?.[1]) return translate("logs.routeTrace.evidence", { value: evidence[1] });
  const continuationEvidence = value.match(/^continuation_evidence=(\d+)$/);
  if (continuationEvidence?.[1]) return translate("logs.routeTrace.continuationEvidence", { value: continuationEvidence[1] });
  const input = value.match(/^input=(\d+)$/);
  if (input?.[1]) return translate("logs.routeTrace.input", { value: input[1] });
  const outputLimit = value.match(/^output_limit=(\d+)$/);
  if (outputLimit?.[1]) return translate("logs.routeTrace.outputLimit", { value: outputLimit[1] });
  const queries = value.match(/^queries=(\d+)$/);
  if (queries?.[1]) return translate("logs.routeTrace.queries", { count: queries[1] });
  const nextActions = value.match(/^next_actions=(\d+)$/);
  if (nextActions?.[1]) return translate("logs.routeTrace.nextActions", { count: nextActions[1] });
  const nextQueries = value.match(/^next_queries=(\d+)$/);
  if (nextQueries?.[1]) return translate("logs.routeTrace.nextQueries", { count: nextQueries[1] });
  const failures = value.match(/^failures=(\d+)$/);
  if (failures?.[1]) return translate("logs.routeTrace.failures", { value: failures[1] });
  const threshold = value.match(/^threshold=(\d+)$/);
  if (threshold?.[1]) return translate("logs.routeTrace.threshold", { value: threshold[1] });
  const timeout = value.match(/^timeout=(.+)s$/);
  if (timeout?.[1]) return translate("logs.routeTrace.timeout", { value: timeout[1] });
  const buffered = value.match(/^buffered=(\d+)$/);
  if (buffered?.[1]) return translate("logs.routeTrace.buffered", { value: buffered[1] });
  if (value === "saw_chunk=true") return translate("logs.routeTrace.hasOutput");
  if (value === "saw_chunk=false") return translate("logs.routeTrace.noOutput");
  const serviceTier = value.match(/^service_tier=(.+)$/);
  if (serviceTier?.[1]) return translate("logs.routeTrace.serviceTier", { value: routeTraceServiceTierLabel(serviceTier[1], translate) });
  const order = value.match(/^order=(.+)$/);
  if (order?.[1]) return translate("logs.routeTrace.order", { value: order[1] });
  const cooldown = value.match(/^cooldown=(.+)s$/);
  if (cooldown?.[1]) return translate("logs.routeTrace.cooldown", { value: cooldown[1] });
  const round = value.match(/^round=(.+)$/);
  if (round?.[1]) return translate("logs.routeTrace.round", { value: round[1] });
  const reason = value.match(/^reason=(.+)$/);
  if (reason?.[1]) return routeTraceReasonLabel(reason[1], translate);
  if (value === "upstream-auth-or-balance") return translate("logs.routeTrace.upstreamAuth");
  const upstreamStatus = value.match(/^upstream-status-(\d+)$/);
  if (upstreamStatus?.[1]) return translate("logs.routeTrace.upstreamStatus", { status: upstreamStatus[1] });
  return "";
}

function routeTraceDetailLabel(value: string, translate: Translate): string {
  const details = value.split(" · ").map((part) => routeTraceDetailPartLabel(part, translate)).filter(Boolean);
  return details.join(" | ");
}

function recoveryStatusLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    waiting: "logs.recoveryStatus.waiting",
    polling: "logs.recoveryStatus.polling",
    cooldown: "logs.recoveryStatus.cooldown",
    success: "logs.recoveryStatus.success",
    succeeded: "logs.recoveryStatus.success",
    failure: "logs.recoveryStatus.failed",
    failed: "logs.recoveryStatus.failed",
    error: "logs.recoveryStatus.failed",
  };
  const normalized = value.trim().toLowerCase();
  return normalized ? translate(labels[normalized] ?? "logs.recoveryStatus.other") : "";
}

function recoveryDetailLabel(value: string, translate: Translate): string {
  const reasonLabels: Record<string, Parameters<Translate>[0]> = {
    billing: "logs.recoveryReason.billing",
    authentication: "logs.recoveryReason.authentication",
    network: "logs.recoveryReason.network",
    rate_limit: "logs.recoveryReason.rateLimit",
    request_size: "logs.recoveryReason.requestSize",
    timeout: "logs.recoveryReason.timeout",
    unknown: "logs.recoveryReason.unknown",
  };
  return value.split(" · ").map((part) => {
    const attempt = part.match(/^attempt=(.+)$/);
    if (attempt?.[1]) return translate("logs.recoveryDetail.attempt", { value: attempt[1] });
    const timeout = part.match(/^timeout=(.+)s$/);
    if (timeout?.[1]) return translate("logs.recoveryDetail.timeout", { value: timeout[1] });
    const cooldown = part.match(/^cooldown=(.+)s$/);
    if (cooldown?.[1]) return translate("logs.recoveryDetail.cooldown", { value: cooldown[1] });
    const failures = part.match(/^failures=(\d+)$/);
    if (failures?.[1]) return translate("logs.recoveryDetail.failures", { value: failures[1] });
    const retry = part.match(/^retry=(.+)s$/);
    if (retry?.[1]) return translate("logs.recoveryDetail.retry", { value: retry[1] });
    const reason = part.match(/^reason=(.+)$/);
    if (reason?.[1]) return translate(reasonLabels[reason[1]] ?? "logs.recoveryReason.unknown");
    return "";
  }).filter(Boolean).join(" | ");
}

function requestAbortedDetail(value: unknown, translate: Translate): string {
  const aborted = asRecord(value);
  const reason = compactLogValue(aborted.reason);
  if (!reason) return "";
  switch (reason) {
    case "no_terminal_callback": return translate("logs.aborted.reasonNoTerminalCallback");
    case "service_restart": return translate("logs.aborted.reasonServiceRestart");
    case "stale": return translate("logs.aborted.reasonStale");
    default: return reason;
  }
}

function logErrorDetail(value: unknown, translate: Translate): string {
  const error = asRecord(value);
  if (Object.keys(error).length === 0) return compactLogValue(value);
  const reason = compactLogValue(error.reason);
  const parts = [
    error.status_code === undefined ? "" : `HTTP ${compactLogValue(error.status_code)}`,
    compactLogValue(error.type),
    compactLogValue(error.code),
    reason ? requestErrorReasonLabel(reason, translate) : "",
    error.failed_deployment_order === undefined ? "" : `order=${compactLogValue(error.failed_deployment_order)}`,
    error.failed_route_key === undefined ? "" : `route=${compactLogValue(error.failed_route_key)}`,
    error.failed_deployment_id === undefined ? "" : `deployment=${compactLogValue(error.failed_deployment_id)}`,
  ].filter(Boolean);
  return parts.join(" | ");
}

function safeOriginalLogRecord(record: unknown): string {
  if (typeof record === "string") return record;
  try {
    return JSON.stringify(record, null, 2);
  } catch {
    return compactLogValue(record);
  }
}

function routeIdentityLabel(value: unknown, fallback: { model: string; upstreamModel: string; provider: string }, translate: Translate): string {
  const route = asRecord(value);
  if (Object.keys(route).length === 0) return "";
  const provider = compactLogValue(route.provider) || fallback.provider;
  const upstream = compactUpstreamLogModel(route.upstream_model) || fallback.upstreamModel;
  const publicModel = compactLogValue(route.public_model) || fallback.model;
  const order = compactLogValue(route.order);
  const identity = [provider, upstream || publicModel].filter(Boolean).join(" / ");
  if (!identity) return "";
  return order ? `${identity} · ${translate("logs.routeTrace.order", { value: order })}` : identity;
}

function logRecordBaseKey(tab: LogTab, time: string, requestKey: string, event: string, action: string, original: string): string {
  const identity = [requestKey, time, event, action].filter(Boolean);
  const stablePart = identity.length > 0 ? identity.join(":") : original.split(" | ", 1)[0]?.slice(0, 180);
  return `${tab}:${stablePart || "record"}`;
}

function parseTextLogRecord(record: string, tab: LogTab, _index: number, translate: Translate): RenderedLogRecord {
  let detail = record.trim();
  let time = "";
  while (detail.startsWith("[")) {
    const match = detail.match(/^\[([^\]]+)\]\s*(.*)$/);
    if (!match || Number.isNaN(new Date(match[1]).getTime())) break;
    if (!time) time = shortLogTimestamp(match[1]);
    detail = match[2].trim();
  }
  if (!time) {
    const leadingTimestamp = detail.match(/^(Updated\s+)?(\d{4}-\d{2}-\d{2}[T ]\S+)\s*(.*)$/);
    if (leadingTimestamp && !Number.isNaN(new Date(leadingTimestamp[2] ?? "").getTime())) {
      time = shortLogTimestamp(leadingTimestamp[2]);
      detail = `${leadingTimestamp[1] ?? ""}${leadingTimestamp[3] ?? ""}`.trim();
    }
  }
  let source = tab === "service" ? translate("logs.service") : logTitle(tab, translate);
  let status = "";
  let model = "";
  let tokens = "";
  const servicePrefix = detail.match(/^\[(\d+)\]\s+\[([A-Z]+)\]\s*(.*)$/);
  if (servicePrefix) {
    if (tab !== "service") source = `PID ${servicePrefix[1]}`;
    status = logLevelLabel(servicePrefix[2], translate);
    detail = servicePrefix[3].trim();
  } else {
    const proxyPrefix = detail.match(/^(?:\d{2}:\d{2}:\d{2}\s+-\s+)?([^:]+):(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s*(.*)$/);
    const levelPrefix = detail.match(/^(?:\[([A-Z]+)\]|(DEBUG|INFO|WARNING|ERROR|CRITICAL):)\s*(.*)$/);
    if (proxyPrefix) {
      if (tab !== "service") source = proxyPrefix[1]?.trim() || source;
      status = logLevelLabel(proxyPrefix[2] || "", translate);
      detail = (proxyPrefix[3] ?? "").trim();
    } else if (levelPrefix) {
      status = logLevelLabel(levelPrefix[1] || levelPrefix[2] || "", translate);
      detail = (levelPrefix[3] ?? "").trim();
      const process = detail.match(/\bprocess \[(\d+)\]/i);
      if (process?.[1] && tab !== "service") source = `PID ${process[1]}`;
    } else if (/\b(error|failed|timeout|exception)\b/i.test(detail)) {
      status = translate("logs.failed");
    }
  }
  if (tab === "online-usage" && time) {
    const fields = detail.split(/\s{2,}/).filter(Boolean);
    if (fields.length > 0 && fields[0] !== "Updated") {
      model = fields.shift() ?? "";
      const tokenIndex = fields.findIndex((field) => field.startsWith("tokens="));
      if (tokenIndex >= 0) tokens = fields.splice(tokenIndex, 1)[0]?.slice("tokens=".length) ?? "";
      status = fields.shift() ?? status;
      detail = fields.join(" | ");
    }
  }
  const action = tab === "actions" ? menuActionLabel(detail.split(/[:;,]/, 1)[0]?.trim() ?? "", translate) : "";
  const requestKey = `${tab}:${time || "un-timed"}:${source}`;
  return {
    key: logRecordBaseKey(tab, time, requestKey, "", action, record),
    requestKey,
    routeAttempts: [],
    time,
    source,
    status,
    model,
    upstreamModel: "",
    provider: "",
    apiKeyName: "",
    event: "",
    action,
    duration: "",
    tokens,
    detail,
    original: record,
  };
}

function renderLogRecord(record: unknown, tab: LogTab, index: number, translate: Translate): RenderedLogRecord {
  if (typeof record === "string") return parseTextLogRecord(record, tab, index, translate);
  const value = asRecord(record);
  const time = shortLogTimestamp(value.ts ?? value.timestamp ?? value.time ?? value.created_at ?? value.updated_at ?? value.checked_at ?? value.heartbeat_at ?? value.started_at);
  const routingState = compactLogValue(value.routing_state);
  const provider = compactLogValue(value.provider)
    || (tab === "requests"
      ? routingState === "no_available_deployment"
        ? translate("logs.noAvailableRoute")
        : routingState === "model_not_configured"
          ? translate("logs.modelNotConfigured")
          : routingState === "unselected"
            ? translate("logs.notRouted")
            : ""
      : "");
  const apiKeyName = compactLogValue(value.api_key_name);
  const publicModel = compactLogValue(value.public_model ?? value.model_group ?? value.model);
  const upstreamModel = compactLogValue(value.upstream_model);
  const model = publicModel || upstreamModel;
  const source = tab === "service"
    ? translate("logs.service")
    : compactLogValue(value.source) || provider || logTitle(tab, translate);
  const rawStatus = compactLogValue(value.status ?? value.result);
  const status = tab === "recovery"
    ? recoveryStatusLabel(rawStatus, translate)
    : tab === "requests"
      ? requestStatusLabel(rawStatus, translate) || (value.error ? translate("logs.failed") : "")
      : rawStatus || (value.error ? translate("logs.failed") : "");
  const rawEvent = compactLogValue(value.event);
  const event = tab === "route-trace" ? routeTraceEventLabel(rawEvent, translate) : rawEvent;
  const action = tab === "actions" ? menuActionLabel(compactLogValue(value.action), translate) : compactLogValue(value.action);
  const duration = formatLogDuration(compactLogValue(value.duration_ms));
  const usage = asRecord(value.usage);
  const sentTokens = compactLogValue(usage.input_tokens ?? usage.prompt_tokens ?? value.input_tokens ?? value.prompt_tokens);
  const receivedTokens = compactLogValue(usage.output_tokens ?? usage.completion_tokens ?? value.output_tokens ?? value.completion_tokens);
  const totalTokens = compactLogValue(usage.total_tokens ?? value.total_tokens);
  const tokens = sentTokens && receivedTokens
    ? `${formatLogTokens(sentTokens)} / ${formatLogTokens(receivedTokens)}`
    : formatLogTokens(totalTokens);
  const details: string[] = [];
  const abortedDetail = tab === "requests" ? requestAbortedDetail(value.aborted, translate) : "";
  if (abortedDetail) details.push(abortedDetail);
  const directDetail = value.error === undefined
    ? compactLogValue(value.detail ?? value.message)
    : logErrorDetail(value.error, translate);
  if (directDetail) details.push(
    tab === "route-trace"
      ? routeTraceDetailLabel(directDetail, translate)
      : tab === "recovery" ? recoveryDetailLabel(directDetail, translate) : directDetail,
  );
  const used = new Set(["ts", "timestamp", "time", "created_at", "updated_at", "checked_at", "started_at", "heartbeat_at", "source", "provider", "api_key_name", "model_group", "public_model", "route_key", "routing_state", "status", "result", "detail", "message", "event", "action", "error", "aborted", "heartbeat", "upstream_model", "model", "duration_ms", "usage", "total_tokens", "request_id", "requestId", "session", "session_id", "deployment_id", "deployment_order", "target_order", "route", "failed_route", "failed_route_key", "failed_deployment_id", "candidate_routes", "candidates", "after_constraints", "selected_candidates", "cooldown_deployments"]);
  for (const [key, item] of Object.entries(value)) {
    if (used.has(key)) continue;
    const display = compactLogValue(item);
    if (display) details.push(`${key}: ${display}`);
  }
  const recoveryFallback = tab === "recovery" ? translate("common.notAvailable") : "";
  const requestId = compactLogValue(value.request_id ?? value.requestId);
  const session = asRecord(value.session);
  const sessionId = compactLogValue(value.session_id ?? session.id);
  const requestKey = requestId || sessionId || `${publicModel || model}:${time || "un-timed"}`;
  const fallbackRoute = { model: model || recoveryFallback, upstreamModel: compactUpstreamLogModel(upstreamModel), provider };
  const routeAttempts: RouteTraceAttempt[] = [];
  const route = routeIdentityLabel(value.route, fallbackRoute, translate);
  const failedRoute = routeIdentityLabel(value.failed_route, fallbackRoute, translate);
  if (tab === "route-trace") {
    const hasError = value.error !== undefined && value.error !== null;
    const failed = failedRoute === route || (!failedRoute && (hasError || /(?:error|failed|timeout)/i.test(rawEvent)));
    if (route) {
      routeAttempts.push({
        label: route,
        state: failed ? "failed" : rawEvent === "selected_deployment" ? "selected" : "attempted",
        detail: details.filter(Boolean).join(" | "),
        time,
      });
    }
    if (failedRoute && failedRoute !== route) {
      routeAttempts.push({
        label: failedRoute,
        state: "failed",
        detail: details.filter(Boolean).join(" | "),
        time,
      });
    }
  }
  const original = safeOriginalLogRecord(record);
  const keyTime = tab === "requests" && requestId ? "" : time;
  return {
    key: logRecordBaseKey(tab, keyTime, requestKey, rawEvent, action, original),
    requestKey,
    routeAttempts,
    time,
    source,
    status,
    model: model || recoveryFallback,
    upstreamModel: compactUpstreamLogModel(upstreamModel) || recoveryFallback,
    provider: provider || recoveryFallback,
    apiKeyName: apiKeyName || recoveryFallback,
    event,
    action,
    duration,
    tokens,
    detail: details.filter(Boolean).join(" | "),
    original,
  };
}

function logTimestampNumber(value: string): number | undefined {
  if (!value) return undefined;
  const numeric = Number(value);
  if (Number.isFinite(numeric) && /^[-+]?\d+(?:\.\d+)?$/.test(value.trim())) {
    return Math.abs(numeric) < 100_000_000_000 ? numeric * 1000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function renderLogRecords(records: Array<Record<string, unknown> | string>, tab: LogTab, translate: Translate): RenderedLogRecord[] {
  const rendered = records.map((record, index) => ({ row: renderLogRecord(record, tab, index, translate), index }));
  rendered.sort((left, right) => {
    const leftRow = left.row;
    const rightRow = right.row;
    const leftTime = logTimestampNumber(leftRow.time);
    const rightTime = logTimestampNumber(rightRow.time);
    if (leftTime === undefined && rightTime === undefined) return right.index - left.index;
    if (leftTime === undefined) return 1;
    if (rightTime === undefined) return -1;
    return rightTime - leftTime || right.index - left.index;
  });
  const occurrences = new Map<string, number>();
  return rendered.map(({ row }) => {
    const occurrence = occurrences.get(row.key) ?? 0;
    occurrences.set(row.key, occurrence + 1);
    return { ...row, key: `${row.key}:${occurrence}` };
  });
}

function groupRouteTraceRequests(rows: RenderedLogRecord[]): RouteTraceRequest[] {
  const groups = new Map<string, { time: string; model: string; rows: RenderedLogRecord[] }>();
  for (const row of rows) {
    const key = row.requestKey || row.key;
    const group = groups.get(key) ?? { time: row.time, model: row.model, rows: [] };
    group.rows.push(row);
    if (row.time && (!group.time || (logTimestampNumber(row.time) ?? -Infinity) > (logTimestampNumber(group.time) ?? -Infinity))) group.time = row.time;
    if (!group.model && row.model) group.model = row.model;
    groups.set(key, group);
  }
  return Array.from(groups.entries()).flatMap(([key, group]) => {
    const attempts: RouteTraceAttempt[] = [];
    // The shared log rows are newest-first. Rebuild each request in event order
    // so the route timeline reads from the requested model to the final path.
    for (const row of [...group.rows].reverse()) {
      for (const attempt of row.routeAttempts) {
        const existing = attempts.find((candidate) => candidate.label === attempt.label);
        if (!existing) {
          attempts.push({ ...attempt });
          continue;
        }
        // A route can be logged once when selected and again when it fails (or
        // when a recovery poll selects it again). Keep the path compact while
        // allowing a later explicit selection/failure to become the visible
        // state.
        if (attempt.state !== "attempted") existing.state = attempt.state;
        if (attempt.detail && (attempt.state !== "attempted" || !existing.detail)) existing.detail = attempt.detail;
        if (!existing.time && attempt.time) existing.time = attempt.time;
      }
    }
    // Route tracing is useful only after a concrete upstream route is known.
    // Keep pre-route failures in the request logs, but do not render them as
    // empty "not available" route-trace requests.
    if (attempts.length === 0) return [];
    const failedCount = attempts.filter((attempt) => attempt.state === "failed").length;
    const lastAttempt = attempts[attempts.length - 1];
    const outcome: RouteTraceRequest["outcome"] = lastAttempt?.state === "failed"
        ? "failed"
        : failedCount > 0 || attempts.length > 1 ? "fallback" : "direct";
    return {
      key,
      time: group.time,
      model: group.model,
      attempts,
      rows: group.rows,
      routePath: attempts.map((attempt) => attempt.label).join(" → "),
      outcome,
    };
  });
}

function routeTraceOutcomeLabel(outcome: RouteTraceRequest["outcome"], translate: Translate, attempts: number): string {
  if (outcome === "unavailable") return translate("logs.routeTrace.noRoute");
  if (outcome === "failed") return translate("logs.routeTrace.routeFailed");
  if (outcome === "fallback") return translate("logs.routeTrace.fallbackCount", { count: Math.max(1, attempts - 1) });
  return translate("logs.routeTrace.direct");
}

const LOG_DETAIL_COLUMN_FLOOR = 240;
// Every log record renders its stamp as `YYYY-MM-DD HH:MM:SS` in the table's
// 13 pt face. Measured against the widest possible digit run
// ("2026-09-30 22:58:58" -> 136 pt) plus the 8 pt cell padding on each side,
// 168 keeps the seconds visible for every row: the requests tab used to ask for
// 148, which clipped the tail of the widest digit runs into an ellipsis.
const LOG_TIME_COLUMN_WIDTH = 168;

function logColumns(tab: LogTab, translate: Translate): LogColumn[] {
  const time = { label: translate("logs.localTime"), width: LOG_TIME_COLUMN_WIDTH, value: (row: RenderedLogRecord) => row.time };
  const status = { label: translate("common.status"), width: 68, value: (row: RenderedLogRecord) => row.status };
  const detail = { label: translate("logs.detail"), width: 260, flex: true, value: (row: RenderedLogRecord) => row.detail };
  if (tab === "requests") return [
    time,
    { label: translate("providers.publicModel"), width: 106, value: (row) => row.model },
    { label: translate("providers.upstream"), width: 106, value: (row) => row.upstreamModel },
    { label: translate("common.provider"), width: 78, value: (row) => row.provider },
    { label: translate("logs.apiKeyName"), width: 64, value: (row) => row.apiKeyName },
    { ...status, width: 68 },
    { label: translate("logs.duration"), width: 62, value: (row) => row.duration },
    { label: translate("logs.tokenCountK"), width: 79, value: (row) => row.tokens },
    { ...detail, width: 240 },
  ];
  if (tab === "actions") return [
    time,
    { label: translate("logs.action"), width: 180, flex: true, value: (row) => row.action },
    status,
  ];
  if (tab === "recovery") return [
    time,
    { label: translate("providers.publicModel"), width: 142, value: (row) => row.model },
    { label: translate("providers.upstream"), width: 142, value: (row) => row.upstreamModel },
    { label: translate("common.provider"), width: 104, value: (row) => row.provider },
    { label: translate("logs.apiKeyName"), width: 120, value: (row) => row.apiKeyName },
    { ...status, width: 68 },
    { ...detail, width: 300 },
  ];
  if (tab === "online-usage") return [
    time,
    { label: translate("logs.source"), width: 142, value: (row) => row.source },
    { label: translate("logs.tokenCount"), width: 96, value: (row) => row.tokens },
    { ...detail, width: 420 },
  ];
  return [
    time,
    { label: translate("logs.source"), width: 132, value: (row) => row.source },
    status,
    { ...detail, width: 500 },
  ];
}

function fitLogColumns(columns: LogColumn[], availableWidth: number): LogColumn[] {
  if (!Number.isFinite(availableWidth) || availableWidth <= 0) return columns;
  const usableWidth = Math.max(0, availableWidth - 20);
  const flexible = columns.filter((column) => column.flex);
  if (flexible.length === 0) return columns;
  const fixedWidth = columns.reduce((total, column) => (column.flex ? total : total + column.width), 0);
  // Fixed columns keep their content-sized widths so timestamps, model names,
  // provider, key name, status, and the sent/received token pair stay fully
  // visible. The detail column takes the remaining width, and anything that
  // still does not fit stays reachable through the table's horizontal scroller
  // instead of being squeezed into an ellipsis.
  const flexFloor = LOG_DETAIL_COLUMN_FLOOR;
  const remaining = usableWidth - fixedWidth;
  return columns.map((column) => column.flex
    ? { ...column, width: Math.max(flexFloor, remaining) }
    : column);
}

function routeTraceAttemptLabel(state: RouteTraceAttempt["state"], translate: Translate): string {
  if (state === "failed") return translate("logs.routeTrace.failedRoute");
  if (state === "selected") return translate("logs.routeTrace.selectedRoute");
  return translate("logs.routeTrace.attemptedRoute");
}

function routeTraceAttemptIcon(state: RouteTraceAttempt["state"]): string {
  if (state === "failed") return "×";
  if (state === "selected") return "✓";
  return "•";
}

function RouteTraceWorkspace({ requests, selectedKey, native, translate, onSelect }: { requests: RouteTraceRequest[]; selectedKey: string; native: NativeLeafAdapter; translate: Translate; onSelect: (key: string) => void }): React.JSX.Element {
  const [appActive, setAppActive] = useState(() => AppState.currentState === "active");
  const [visibleRequests, setVisibleRequests] = useState(requests);
  const [timelineViewportWidth, setTimelineViewportWidth] = useState(0);
  const [timelineContentWidth, setTimelineContentWidth] = useState(0);
  const [timelineHorizontalOffset, setTimelineHorizontalOffset] = useState(0);
  const [timelineScrollbarTrackWidth, setTimelineScrollbarTrackWidth] = useState(0);
  const scrolling = useRef(false);
  const pendingRequests = useRef<RouteTraceRequest[] | undefined>(undefined);
  const scrollIdleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const timelineScrollRef = useRef<ScrollView>(null);
  const selected = visibleRequests.find((request) => request.key === selectedKey) ?? visibleRequests[0];
  const hasTimelineHorizontalOverflow = timelineContentWidth > timelineViewportWidth + 1;
  const timelineMaximumHorizontalOffset = Math.max(0, timelineContentWidth - timelineViewportWidth);
  const timelineScrollbarThumbWidth = hasTimelineHorizontalOverflow && timelineScrollbarTrackWidth > 0
    ? Math.min(timelineScrollbarTrackWidth, Math.max(ROUTE_TRACE_SCROLLBAR_MIN_THUMB_WIDTH, timelineScrollbarTrackWidth * timelineViewportWidth / timelineContentWidth))
    : 0;
  const timelineScrollbarTravel = Math.max(0, timelineScrollbarTrackWidth - timelineScrollbarThumbWidth);
  const timelineScrollbarThumbOffset = timelineMaximumHorizontalOffset > 0
    ? timelineScrollbarTravel * Math.max(0, Math.min(1, timelineHorizontalOffset / timelineMaximumHorizontalOffset))
    : 0;
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => setAppActive(state === "active"));
    return () => subscription.remove();
  }, []);
  const deferLiveRequestRefresh = useCallback((): void => {
    scrolling.current = true;
    if (scrollIdleTimer.current !== undefined) clearTimeout(scrollIdleTimer.current);
    scrollIdleTimer.current = setTimeout(() => {
      scrolling.current = false;
      scrollIdleTimer.current = undefined;
      const pending = pendingRequests.current;
      pendingRequests.current = undefined;
      if (pending) setVisibleRequests(pending);
    }, ROUTE_TRACE_SCROLL_IDLE_MS);
  }, []);
  useEffect(() => {
    if (scrolling.current) {
      pendingRequests.current = requests;
      return;
    }
    setVisibleRequests(requests);
  }, [requests]);
  useEffect(() => () => {
    if (scrollIdleTimer.current !== undefined) clearTimeout(scrollIdleTimer.current);
  }, []);
  const scrollTimelineToIndicatorPosition = useCallback((locationX: number): void => {
    if (timelineMaximumHorizontalOffset <= 0 || timelineScrollbarTravel <= 0) return;
    const thumbOffset = Math.max(0, Math.min(timelineScrollbarTravel, locationX - timelineScrollbarThumbWidth / 2));
    const offset = timelineMaximumHorizontalOffset * thumbOffset / timelineScrollbarTravel;
    setTimelineHorizontalOffset(offset);
    timelineScrollRef.current?.scrollTo({ x: offset, animated: false });
  }, [timelineMaximumHorizontalOffset, timelineScrollbarThumbWidth, timelineScrollbarTravel]);
  const openOriginalRecords = (): void => {
    if (!selected) return;
    void native.showReadOnlyText({
      title: translate("logs.originalRecord"),
      text: selected.rows.map((row) => row.original).join("\n\n"),
      closeLabel: translate("status.close"),
      language: "json",
      html: readOnlyCodeEditorHtml(editorMenuLabels(translate)),
    });
  };
  return <View style={styles.routeTraceWorkspace}>
    <View style={styles.routeTraceRequestPane}>
      <FlatList
        style={styles.routeTraceRequestScroll}
        contentContainerStyle={styles.routeTraceRequestList}
        data={visibleRequests}
        keyExtractor={(request) => request.key}
        initialNumToRender={12}
        maxToRenderPerBatch={12}
        windowSize={7}
        getItemLayout={(_data, index) => ({
          length: ROUTE_TRACE_REQUEST_ROW_HEIGHT,
          offset: ROUTE_TRACE_REQUEST_ROW_HEIGHT * index,
          index,
        })}
        removeClippedSubviews={false}
        onScrollBeginDrag={deferLiveRequestRefresh}
        onMomentumScrollBegin={deferLiveRequestRefresh}
        onScroll={deferLiveRequestRefresh}
        scrollEventThrottle={16}
        renderItem={({ item: request }) => {
          const isSelected = selected?.key === request.key;
          const selectedTextStyle = isSelected && appActive ? styles.routeTraceRequestTextSelected : null;
          const requestDescription = [request.model, request.time, request.routePath, routeTraceOutcomeLabel(request.outcome, translate, request.attempts.length)].filter(Boolean).join(" · ");
          return <Pressable
            key={request.key}
            style={({ pressed }) => [
              styles.routeTraceRequestRow,
              !isSelected && pressed && styles.routeTraceRequestRowPressed,
              isSelected && (appActive ? styles.routeTraceRequestRowSelected : styles.routeTraceRequestRowSelectedInactive),
            ]}
            onPress={() => onSelect(request.key)}
            onFocus={() => onSelect(request.key)}
            focusable
            accessibilityRole="button"
            accessibilityLabel={requestDescription}
            accessibilityState={{ selected: isSelected }}
          >
            <View style={styles.routeTraceRequestHeading}>
              <Text numberOfLines={1} style={[styles.routeTraceRequestModel, selectedTextStyle]}>{request.model || translate("common.notAvailable")}</Text>
              <Text style={[styles.routeTraceRequestTime, selectedTextStyle]}>{request.time || translate("common.notAvailable")}</Text>
            </View>
            <Text numberOfLines={1} style={[styles.routeTraceRequestPath, selectedTextStyle]}>{request.routePath || translate("logs.routeTrace.noRoute")}</Text>
            <Text style={[styles.routeTraceOutcome, request.outcome === "failed" ? styles.routeTraceOutcomeFailed : request.outcome === "fallback" ? styles.routeTraceOutcomeFallback : styles.routeTraceOutcomeDirect, selectedTextStyle]}>{routeTraceOutcomeLabel(request.outcome, translate, request.attempts.length)}</Text>
          </Pressable>;
        }}
        alwaysBounceHorizontal={false}
        showsHorizontalScrollIndicator={false}
        showsVerticalScrollIndicator
      />
    </View>
    <View style={styles.routeTraceDetailPane}>
      {selected ? <>
        <View style={styles.routeTraceDetailHeader}>
          <View style={styles.routeTraceDetailTitleBlock}>
            <Text numberOfLines={1} style={styles.routeTraceDetailTitle}>{selected.model || translate("common.notAvailable")}</Text>
            <Text style={styles.routeTraceDetailMeta}>{[selected.time, routeTraceOutcomeLabel(selected.outcome, translate, selected.attempts.length)].filter(Boolean).join(" · ")}</Text>
          </View>
          <NativeButton title={translate("logs.originalRecord")} compact link onPress={openOriginalRecords} />
        </View>
        <View style={styles.routeTracePathHeader}>
          <Text style={styles.routeTraceSectionTitle}>{translate("logs.routeTrace.actualPath")}</Text>
          <Text style={styles.routeTracePathCount}>{translate("logs.routeTrace.routeCount", { count: selected.attempts.length })}</Text>
        </View>
        <Text numberOfLines={2} style={styles.routeTracePathSummary}>{selected.routePath || translate("logs.routeTrace.noRoute")}</Text>
        <View style={styles.routeTraceTimelineFrame}>
          <PersistentScrollView
            ref={timelineScrollRef}
            style={styles.routeTraceTimelineScroll}
            contentContainerStyle={[styles.routeTraceTimeline, hasTimelineHorizontalOverflow && styles.routeTraceTimelineWithHorizontalScrollbar]}
            alwaysBounceHorizontal={false}
            showsVerticalScrollIndicator
            onLayout={({ nativeEvent }) => setTimelineViewportWidth(nativeEvent.layout.width)}
            onContentSizeChange={(width) => setTimelineContentWidth(width)}
            onScroll={({ nativeEvent }) => setTimelineHorizontalOffset((current) => Math.abs(current - nativeEvent.contentOffset.x) < 0.5 ? current : nativeEvent.contentOffset.x)}
            scrollEventThrottle={16}
          >
          <View style={styles.routeTraceTimelineRow}>
            <View style={styles.routeTraceTimelineRail}>
              {selected.attempts.length > 0 ? <View style={styles.routeTraceTimelineLine} /> : null}
              <View style={[styles.routeTraceTimelineNode, styles.routeTraceTimelineNodeStart]}>
                <Text style={[styles.routeTraceTimelineNodeText, styles.routeTraceTimelineNodeTextActive]}>0</Text>
              </View>
            </View>
            <View style={styles.routeTraceStartCard}>
              <View style={styles.routeTraceStepMetaRow}>
                <Text style={styles.routeTraceStepNumber}>{translate("logs.routeTrace.startPoint")}</Text>
                <Text style={styles.routeTraceStepLabel}>{translate("logs.routeTrace.requestedModel")}</Text>
              </View>
              <Text numberOfLines={2} style={styles.routeTraceStepValue}>{selected.model || translate("common.notAvailable")}</Text>
            </View>
          </View>
          {selected.attempts.map((attempt, index) => {
            const failed = attempt.state === "failed";
            const chosen = attempt.state === "selected";
            return <View key={`${attempt.label}:${index}`} style={styles.routeTraceTimelineRow}>
              <View style={styles.routeTraceTimelineRail}>
                {index < selected.attempts.length - 1 ? <View style={styles.routeTraceTimelineLine} /> : null}
                <View style={[styles.routeTraceTimelineNode, failed ? styles.routeTraceTimelineNodeFailed : chosen ? styles.routeTraceTimelineNodeSelected : styles.routeTraceTimelineNodeAttempted]}>
                  <Text style={[styles.routeTraceTimelineNodeText, (failed || chosen) && styles.routeTraceTimelineNodeTextActive]}>{index + 1}</Text>
                </View>
              </View>
              <View style={[styles.routeTraceStepCard, failed ? styles.routeTraceStepCardFailed : chosen ? styles.routeTraceStepCardSelected : null]}>
                <View style={styles.routeTraceStepMetaRow}>
                  <Text style={styles.routeTraceStepNumber}>{translate("logs.routeTrace.stepProgress", { current: index + 1, total: selected.attempts.length })}</Text>
                  <View style={styles.routeTraceStepState}>
                    <View style={[styles.routeTraceStepStateIcon, failed ? styles.routeTraceStepStateIconFailed : chosen ? styles.routeTraceStepStateIconSelected : styles.routeTraceStepStateIconAttempted]}>
                      <Text style={[styles.routeTraceStepStateIconText, !failed && !chosen && styles.routeTraceStepStateIconTextAttempted]}>{routeTraceAttemptIcon(attempt.state)}</Text>
                    </View>
                    <Text style={[styles.routeTraceStepStateText, failed ? styles.routeTraceStepStateFailed : chosen ? styles.routeTraceStepStateSelected : styles.routeTraceStepStateAttempted]}>{routeTraceAttemptLabel(attempt.state, translate)}</Text>
                  </View>
                </View>
                <Text numberOfLines={2} style={styles.routeTraceStepTitle}>{attempt.label}</Text>
                {attempt.detail ? <Text numberOfLines={3} style={styles.routeTraceStepDetail}>{attempt.detail}</Text> : null}
              </View>
            </View>;
          })}
          {selected.attempts.length === 0 ? <View style={styles.routeTraceNoPath}><Text style={styles.routeTraceNoPathText}>{translate("logs.routeTrace.noPathRecorded")}</Text></View> : null}
          </PersistentScrollView>
          {hasTimelineHorizontalOverflow ? <View
            style={styles.routeTraceTimelineHorizontalScrollbarTrack}
            onLayout={({ nativeEvent }) => setTimelineScrollbarTrackWidth(nativeEvent.layout.width)}
            onStartShouldSetResponder={() => true}
            onResponderGrant={({ nativeEvent }) => scrollTimelineToIndicatorPosition(nativeEvent.locationX)}
            onResponderMove={({ nativeEvent }) => scrollTimelineToIndicatorPosition(nativeEvent.locationX)}
          >
            <View pointerEvents="none" style={[styles.routeTraceTimelineHorizontalScrollbarThumb, { width: timelineScrollbarThumbWidth, transform: [{ translateX: timelineScrollbarThumbOffset }] }]} />
          </View> : null}
        </View>
      </> : <View style={styles.routeTraceNoSelection}><Text style={styles.routeTraceNoSelectionText}>{translate("logs.routeTrace.selectRequest")}</Text></View>}
    </View>
  </View>;
}

function LogsWorkspace({ snapshot, ipc, native, busy, translate, dispatch, requestedTab, requestedTabKey = 0, onStatus }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; requestedTab?: typeof LOG_TABS[number]; requestedTabKey?: number; onStatus?: (status?: string) => void }): React.JSX.Element {
  type PauseIntent = { tab: typeof LOG_TABS[number]; paused: boolean; token: number };
  type ClearIntent = { tab: typeof LOG_TABS[number]; token: number };
  const [selected, setSelected] = useState<typeof LOG_TABS[number]>(() => requestedTab ?? "requests");
  const [activeState, setActiveState] = useState<ActiveLogView>();
  const [filterDraft, setFilterDraft] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Partial<Record<LogTab, string>>>({});
  const [pauseIntent, setPauseIntent] = useState<PauseIntent>();
  const [clearIntent, setClearIntent] = useState<ClearIntent>();
  const [cooldownClearPending, setCooldownClearPending] = useState(false);
  const [tableWidth, setTableWidth] = useState(0);
  const filterTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const tabsRef = useRef<HostInstance | null>(null);
  const pauseIntentToken = useRef(0);
  const clearIntentToken = useRef(0);
  const appliedTabRequestKey = useRef(requestedTabKey);
  const clearTabRef = useRef<typeof LOG_TABS[number] | undefined>(undefined);
  const viewRevisionRef = useRef<number | undefined>(undefined);
  // The filter field owns its text until Core confirms it.  A poll that still
  // carries the previous filter lands between a keystroke and the 250 ms
  // dispatch, and syncing the field from it deleted the character the user had
  // just typed.
  const filterDirty = useRef(false);
  const filterDraftRef = useRef("");
  filterDraftRef.current = filterDraft;
  const selectedTabRef = useRef(selected);
  selectedTabRef.current = selected;
  const active = activeState?.tab === selected ? activeState.log : undefined;
  useEffect(() => {
    if (appliedTabRequestKey.current === requestedTabKey) return;
    appliedTabRequestKey.current = requestedTabKey;
    if (requestedTab) setSelected(requestedTab);
  }, [requestedTab, requestedTabKey]);
  useEffect(() => {
    // A tab switch starts from that tab's own filter, so the dirty guard is
    // dropped before the sync effect below reads the new tab's value.
    filterDirty.current = false;
  }, [selected]);
  useEffect(() => {
    const coreFilter = active?.filter ?? "";
    if (filterDirty.current && coreFilter !== filterDraftRef.current) return;
    filterDirty.current = false;
    setFilterDraft(coreFilter);
  }, [active?.filter, selected]);
  useEffect(() => () => { if (filterTimer.current) clearTimeout(filterTimer.current); }, []);
  useEffect(() => {
    let mounted = true;
    let polling = false;
    viewRevisionRef.current = undefined;
    setPauseIntent(undefined);
    setClearIntent(undefined);
    setCooldownClearPending(false);
    const poll = async (): Promise<void> => {
      if (polling) return;
      polling = true;
      try {
        const result = await ipc.logs(selected, viewRevisionRef.current);
        if (!mounted) return;
        viewRevisionRef.current = result.revision;
        if (result.changed && result.log) {
          setActiveState({ tab: selected, log: result.log });
          setPauseIntent((current) => current?.tab === selected && current.paused === result.log?.paused ? undefined : current);
          setClearIntent((current) => current?.tab === selected && result.log?.line_count === 0 ? undefined : current);
        }
      } catch {
        // Keep the last visible rows through a transient Core failure.
      } finally {
        polling = false;
      }
    };
    void poll();
    const intervalMs = selected === "recovery"
      ? RECOVERY_LOG_POLL_MS
      : selected === "requests" ? REQUEST_LOG_POLL_MS
      : selected === "online-usage" ? ONLINE_USAGE_POLL_MS : LOG_VIEW_POLL_MS;
    const interval = setInterval(() => { void poll(); }, intervalMs);
    return () => { mounted = false; clearInterval(interval); };
  }, [ipc, selected]);
  const clearing = clearIntent?.tab === selected;
  const rows = useMemo(
    () => renderLogRecords(clearing ? [] : (active?.records ?? []), selected, translate),
    [active?.records, clearing, selected, translate],
  );
  const columns = useMemo(
    () => fitLogColumns(logColumns(selected, translate), tableWidth),
    [selected, tableWidth, translate],
  );
  const nativeTableColumns = useMemo(
    () => columns.map(({ label, width }) => ({ label, width })),
    [columns],
  );
  const nativeTableRows = useMemo(
    () => selected === "route-trace" ? [] : rows.map((row) => ({
      key: row.key,
      cells: columns.map((column) => column.value(row)),
    })),
    [columns, rows, selected],
  );
  const selectedKey = selectedKeys[selected] ?? "";
  const routeTraceRequests = useMemo(
    () => selected === "route-trace" ? groupRouteTraceRequests(rows) : [],
    [rows, selected],
  );
  useEffect(() => {
    if (selected !== "route-trace" || routeTraceRequests.length === 0) return;
    setSelectedKeys((current) => {
      const currentKey = current["route-trace"];
      if (currentKey && routeTraceRequests.some((request) => request.key === currentKey)) return current;
      return { ...current, "route-trace": routeTraceRequests[0].key };
    });
  }, [routeTraceRequests, selected]);
  const paused = pauseIntent?.tab === selected ? pauseIntent.paused : active?.paused ?? false;
  const togglePaused = (): void => {
    const tab = selected;
    const nextPaused = !paused;
    const token = pauseIntentToken.current + 1;
    pauseIntentToken.current = token;
    setPauseIntent({ tab, paused: nextPaused, token });
    void dispatch(nextPaused ? "logs.pause" : "logs.resume", { tab }, "logs").then(async () => {
      try {
        const result = await ipc.logs(tab);
        if (selectedTabRef.current !== tab) return;
        viewRevisionRef.current = result.revision;
        if (result.log) setActiveState({ tab, log: result.log });
      } catch {
        // The regular log poll will reconcile the confirmed Core state.
      } finally {
        setPauseIntent((current) => current?.token === token ? undefined : current);
      }
    });
  };
  const clearLogs = (): void => {
    const tab = selected;
    const token = clearIntentToken.current + 1;
    clearIntentToken.current = token;
    clearTabRef.current = tab;
    setClearIntent({ tab, token });
    setSelectedKeys((current) => ({ ...current, [tab]: undefined }));
    void dispatch("logs.clear", { tab }, "logs").then(async () => {
      try {
        const result = await ipc.logs(tab);
        if (selectedTabRef.current !== tab) return;
        viewRevisionRef.current = result.revision;
        if (result.log) setActiveState({ tab, log: result.log });
      } catch {
        // The regular log poll will restore the confirmed Core view.
      } finally {
        if (clearTabRef.current === tab) clearTabRef.current = undefined;
        setClearIntent((current) => current?.token === token ? undefined : current);
      }
    });
  };
  const clearCooldowns = (): void => {
    const tab = selected;
    if (tab !== "recovery") return;
    // The button reports progress instead of going dead, so a second press
    // must not queue a second clear.
    if (cooldownClearPending) return;
    setCooldownClearPending(true);
    void dispatch("logs.clear_recovery_and_cooldowns", { tab }, "logs").then(async () => {
      try {
        const result = await ipc.logs(tab);
        if (selectedTabRef.current !== tab) return;
        viewRevisionRef.current = result.revision;
        if (result.log) setActiveState({ tab, log: result.log });
      } catch {
        // The one-second recovery poll will reconcile the confirmed Core state.
      } finally {
        setCooldownClearPending(false);
      }
    }).catch(() => {
      setCooldownClearPending(false);
    });
  };
  const tabOptions = LOG_TABS.map((tab) => ({ id: tab, title: logTitle(tab, translate) }));
  const lineCount = clearing ? 0 : active?.line_count ?? rows.length;
  // The window keeps one permanent bottom status strip: the line count and the
  // pause state are that strip's idle text, never a second bar of their own.
  const statusLine = [
    translate(active && lineCount >= active.limit ? "logs.latestLinesAtLimit" : "common.lines", { count: lineCount }),
    paused ? translate("logs.paused") : "",
  ].filter(Boolean).join(" | ");
  useEffect(() => { onStatus?.(statusLine); }, [onStatus, statusLine]);
  const openRelayUsageLogs = (): void => {
    const relayDomain = asRecord(snapshot?.domains.relay_accounts);
    const relayState = Object.keys(asRecord(relayDomain.state)).length > 0 ? asRecord(relayDomain.state) : relayDomain;
    const accounts = asRecords(relayState.accounts).flatMap((value) => {
      const accountId = stringValue(value.id).trim();
      const type: "newapi" | "sub2api" | undefined = value.type === "newapi"
        ? "newapi"
        : value.type === "sub2api" ? "sub2api" : undefined;
      const origin = stringValue(value.origin).trim();
      if (!accountId || !type || !origin) return [];
      return [{
        accountId,
        type,
        origin,
        label: stringValue(value.label, origin),
        username: stringValue(value.username).trim(),
        signedIn: value.login_status === "signed_in",
      }];
    }).sort((left, right) => Number(right.signedIn) - Number(left.signedIn));
    if (accounts.length === 0) return;
    const open = (index: number): void => {
      const account = accounts[index];
      if (!account) return;
      void (async () => {
        const session = await native.restoreRelaySession({
          accountId: account.accountId,
          type: account.type,
          label: account.label,
          origin: account.origin,
          username: account.username || undefined,
        });
        if (session?.loginStatus !== "signed_in") {
          const login = await native.relayLogin({
            accountId: account.accountId,
            type: account.type,
            label: account.label,
            origin: account.origin,
            language: snapshot?.language ?? "system",
            username: account.username || undefined,
          });
          if (!login) return;
        }
        await native.openRelayLogs({
          accountId: account.accountId,
          type: account.type,
          label: account.label,
          origin: account.origin,
          language: snapshot?.language ?? "system",
        });
      })();
    };
    if (accounts.length === 1) {
      open(0);
      return;
    }
    const tabs = tabsRef.current;
    if (!tabs) {
      open(0);
      return;
    }
    tabs.measureInWindow((x, y, width, height) => {
      void native.showActionMenu({
        title: translate("logs.onlineUsageSummary"),
        items: accounts.map((account) => `${account.label} - ${account.type === "newapi" ? "NewAPI" : "Sub2API"}`),
        anchor: { x, y, width, height },
      }).then((index) => { if (index !== undefined) open(index); });
    });
  };
  return <View style={styles.logsWindow}>
    <View style={styles.logsToolbar}>
      <View style={styles.logFilterRow}><Text style={styles.toolbarLabel}>{translate("common.filter")}</Text><NativeTextField style={styles.logFilterInput} value={filterDraft} placeholder={translate("logs.filterCurrent")} onChangeText={(filter) => { filterDirty.current = true; setFilterDraft(filter); if (filterTimer.current) clearTimeout(filterTimer.current); filterTimer.current = setTimeout(() => { void dispatch("logs.set_filter", { tab: selected, filter }, "logs"); }, 250); }} accessibilityLabel={translate("common.filter")} /></View>
      <View style={styles.logToolbarSpacer} />
      <View style={styles.logActionsRow}>{selected === "recovery" ? <NativeButton title={translate("logs.clearRecoveryCooldown")} accessibilityLabel={translate("logs.clearRecoveryCooldown")} compact busy={cooldownClearPending} disabled={busy && !cooldownClearPending} onPress={clearCooldowns} style={styles.clearCooldownButton} /> : null}<IconButton label="" symbol={paused ? "play" : "pause"} title={paused ? translate("common.resume") : translate("common.pause")} disabled={busy} onPress={togglePaused} /><IconButton label="" symbol="trash" title={translate("common.clearView")} disabled={busy} onPress={clearLogs} /></View>
    </View>
    <WindowTabs nativeRef={tabsRef} values={tabOptions} selected={selected} disabled={busy} onSelect={(tab) => {
      if (clearTabRef.current) return;
      setSelected(tab as LogTab);
      if (tab === "online-usage") openRelayUsageLogs();
    }} style={styles.logsTabs} />
    {rows.length > 0 ? selected === "route-trace"
      ? <RouteTraceWorkspace requests={routeTraceRequests} selectedKey={selectedKey} native={native} translate={translate} onSelect={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} />
      : <View style={styles.logTableFrame} onLayout={({ nativeEvent }) => setTableWidth(nativeEvent.layout.width)}><NativeTable columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths scrollTrailingColumnOverflow onSelectionChange={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} onRowDoublePress={(_key, index) => {
        const row = rows[index];
        if (!row) return;
        void native.showReadOnlyText({ title: translate("logs.originalRecord"), text: row.original, closeLabel: translate("status.close"), language: "json", html: readOnlyCodeEditorHtml(editorMenuLabels(translate)) });
      }} style={styles.logTable} /></View>
      : <View style={styles.logEmptySurface}><Text style={styles.logEmptyText}>{clearing || active ? translate("logs.empty") : translate("logs.loading")}</Text></View>}
  </View>;
}

function EmptyState({ translate }: { translate: Translate }): React.JSX.Element { return <Text style={styles.empty}>{translate("screen.noData")}</Text>; }

const ActionButton = React.forwardRef<HostInstance, { title: string; onPress: () => void; disabled?: boolean; busy?: boolean; primary?: boolean; danger?: boolean; titleWidth?: "auto" | "tight"; toolTip?: string; style?: StyleProp<ViewStyle> }>(function ActionButton({ title, onPress, disabled, busy, primary, danger, titleWidth, toolTip, style }, ref): React.JSX.Element {
  return <NativeButton ref={ref} title={title} disabled={disabled} busy={busy} primary={primary} destructive={danger} titleWidth={titleWidth} toolTip={toolTip} onPress={onPress} style={style} />;
});

function TextField({ label, value, onCommit, onDraftChange, hint, hintStyle, placeholder, secret, multiline, compactMultiline, keyboardType, stacked, labelWidth, labelAlign, controlWidth, labelVisible = true, suffix, disabled, validate, accessory, style }: { label: string; value: string; onCommit: (value: string) => void | Promise<void>; onDraftChange?: (value: string) => void; hint?: string; hintStyle?: StyleProp<TextStyle>; placeholder?: string; secret?: boolean; multiline?: boolean; compactMultiline?: boolean; keyboardType?: "default" | "numeric"; stacked?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; /** The caller renders the label itself (a stacked keys-editor row). */ labelVisible?: boolean; suffix?: string; disabled?: boolean; /** Commit-time validation: an invalid draft stays in the field with its message instead of reaching Core. */ validate?: (next: string) => string | undefined; /** A control that belongs to this row beside the field, such as its help mark. It rides the same grid level as PickerField's, so a field with an accessory and a picker with one draw the same column. */ accessory?: React.ReactNode; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label, onDraftChange, validate);
  return <View style={[styles.formRow, compactStyles.formRow, (stacked || multiline) && styles.formRowStacked, style]}>{labelVisible ? <Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }, (stacked || multiline) && styles.formRowLabelStacked]}>{label}</Text> : null}<View style={[styles.formRowControl, compactStyles.formRowControl, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]}><NativeTextField style={[styles.input, compactStyles.input, multiline && styles.textArea, compactMultiline && styles.compactTextArea, field.error !== undefined && styles.inputInvalid]} value={field.draft} placeholder={placeholder} editable={!disabled} onChangeText={field.onChangeText} onBlur={() => { if (!disabled) void field.commit().catch(() => undefined); }} onSubmitEditing={multiline ? undefined : () => { if (!disabled) void field.commit().catch(() => undefined); }} multiline={multiline} secureTextEntry={secret} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />{hint ? <Text style={[styles.fieldHint, hintStyle]}>{hint}</Text> : null}{field.error !== undefined ? <Text style={[styles.fieldError, hintStyle]} accessibilityLiveRegion="polite">{field.error}</Text> : null}</View>{accessory ?? null}{suffix ? <Text style={[styles.fieldHint, hintStyle]}>{suffix}</Text> : null}</View>;
}

function NativeSecretInputControl({ label, hint, busy, domain, field, target, multiline = false, plainText = false, autoCommit = false, resetToken = 0, onSecretState, setTitle, setBelow, onSetReady, inputMinWidth }: { label: string; hint?: string; busy: boolean; domain: "providers_models" | "relay_accounts" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; multiline?: boolean; plainText?: boolean; autoCommit?: boolean; resetToken?: number; onSecretState: (state: SecretState) => void; setTitle?: string; setBelow?: boolean; onSetReady?: (requestSet: () => void, saving: boolean) => void; inputMinWidth?: number }): React.JSX.Element {
  const [commitRequest, setCommitRequest] = useState(0);
  const [resetRequest, setResetRequest] = useState(0);
  const [status, setStatus] = useState("ready");
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(`${domain}:${field}:${target ?? ""}`));
  const statusRef = useRef(status);
  const dirtyRef = useRef(false);
  const secretDebounceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const commitSequence = useRef(0);
  const pendingCommit = useRef<{ promise: Promise<void>; resolve: () => void; reject: (reason: Error) => void } | undefined>(undefined);
  const requestSecretCommit = useCallback((): Promise<void> => {
    const currentStatus = statusRef.current;
    if (currentStatus !== "dirty" && currentStatus !== "saving") return Promise.resolve();
    if (pendingCommit.current) return pendingCommit.current.promise;
    const request = commitSequence.current + 1;
    commitSequence.current = request;
    let resolvePromise!: () => void;
    let rejectPromise!: (reason: Error) => void;
    const promise = new Promise<void>((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    pendingCommit.current = { promise, resolve: resolvePromise, reject: rejectPromise };
    if (currentStatus !== "saving") setCommitRequest(request);
    return promise;
  }, []);
  const requestCommit = useCallback((): void => {
    if (autoCommit) {
      void requestSecretCommit().catch(() => undefined);
      return;
    }
    const request = commitSequence.current + 1;
    commitSequence.current = request;
    setCommitRequest(request);
  }, [autoCommit, requestSecretCommit]);
  const commit = useCallback((): Promise<void> => requestSecretCommit(), [requestSecretCommit]);
  const reset = useCallback((): void => {
    if (secretDebounceTimer.current !== undefined) {
      clearTimeout(secretDebounceTimer.current);
      secretDebounceTimer.current = undefined;
    }
    pendingCommit.current?.resolve();
    pendingCommit.current = undefined;
    dirtyRef.current = false;
    registry?.setDirty(fieldId.current, false);
    if (!autoCommit) setResetRequest((current) => current + 1);
  }, [autoCommit, registry]);
  useEffect(() => {
    if (secretDebounceTimer.current !== undefined) {
      clearTimeout(secretDebounceTimer.current);
      secretDebounceTimer.current = undefined;
    }
    pendingCommit.current?.resolve();
    pendingCommit.current = undefined;
    statusRef.current = "ready";
    dirtyRef.current = false;
    registry?.setDirty(fieldId.current, false);
  }, [autoCommit, domain, field, registry, target]);
  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset, isDirty: () => dirtyRef.current });
    return () => {
      if (secretDebounceTimer.current !== undefined) clearTimeout(secretDebounceTimer.current);
      pendingCommit.current?.resolve();
      pendingCommit.current = undefined;
      registry?.register(fieldId.current);
    };
  }, [commit, registry, reset]);
  useEffect(() => { onSetReady?.(requestCommit, status === "saving"); }, [onSetReady, status]);
  return <View style={[styles.nativeSecretControl, compactStyles.nativeSecretControl, multiline && styles.nativeSecretMultilineControl]}><NativeSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={hint ?? ""} multiline={multiline} plainText={plainText} autoCommit={autoCommit} disabled={busy} commitRequest={commitRequest} resetRequest={resetRequest + resetToken} onSecretState={(state) => {
    statusRef.current = state.status;
    setStatus(state.status);
    if (state.status === "dirty") {
      dirtyRef.current = true;
      registry?.setDirty(fieldId.current, true);
      if (autoCommit) {
        if (secretDebounceTimer.current !== undefined) clearTimeout(secretDebounceTimer.current);
        secretDebounceTimer.current = setTimeout(() => {
          secretDebounceTimer.current = undefined;
          void requestSecretCommit().catch(() => undefined);
        }, SECRET_INPUT_COMMIT_DEBOUNCE_MS);
      }
    } else if (state.status === "saved" || state.status === "ready" || state.status === "error") {
      if (secretDebounceTimer.current !== undefined) {
        clearTimeout(secretDebounceTimer.current);
        secretDebounceTimer.current = undefined;
      }
      dirtyRef.current = false;
      registry?.setDirty(fieldId.current, false);
      const pending = pendingCommit.current;
      if (pending) {
        pendingCommit.current = undefined;
        if (state.status === "error") pending.reject(new Error(state.error || "Secret could not be staged"));
        else pending.resolve();
      }
    }
    if (state.status === "saved") {
      if (!autoCommit) setResetRequest((current) => current + 1);
      onSecretState(state);
    }
  }} style={[styles.nativeSecretInput, compactStyles.input, multiline && styles.nativeSecretTextArea, inputMinWidth === undefined ? null : { minWidth: inputMinWidth }]} />{!autoCommit && !setBelow && setTitle ? <NativeButton title={setTitle} compact busy={status === "saving"} disabled={busy} onPress={requestCommit} style={styles.secretActionButton} /> : null}</View>;
}

function NativeSecretField({ label, hint, busy, disabled = false, domain, field, target, plainText = false, autoCommit = false, onSecretState, labelWidth, labelAlign, labelVisible = true, setTitle, clearTitle, clearDisabled, onClear, actionsBelow }: { label: string; hint?: string; busy: boolean; disabled?: boolean; domain: "providers_models" | "relay_accounts" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; plainText?: boolean; autoCommit?: boolean; onSecretState: (state: SecretState) => void; labelWidth?: number; labelAlign?: "left" | "right"; /** The caller renders the label itself (a stacked keys-editor row). */ labelVisible?: boolean; setTitle?: string; clearTitle?: string; clearDisabled?: boolean; onClear?: () => Promise<void>; actionsBelow?: boolean }): React.JSX.Element {
  const setAction = useRef<() => void>(() => undefined);
  const [saving, setSaving] = useState(false);
  const [resetToken, setResetToken] = useState(0);
  const inputBusy = busy || disabled;
  const handleSetReady = React.useCallback((requestSet: () => void, nextSaving: boolean): void => { setAction.current = requestSet; setSaving(nextSaving); }, []);
  const handleClear = React.useCallback((): void => {
    if (!onClear) return;
    void onClear().then(() => setResetToken((current) => current + 1));
  }, [onClear]);
  return <View style={[styles.formRow, compactStyles.formRow, actionsBelow && styles.formRowSecretStacked]}>{labelVisible ? <Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text> : null}<View style={[styles.formRowControl, compactStyles.formRowControl]}>{actionsBelow ? <><NativeSecretInputControl label={label} hint={hint} busy={inputBusy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} setBelow onSetReady={handleSetReady} inputMinWidth={110} /><View style={[styles.secretFieldButtons, compactStyles.inlineGap]}>{!autoCommit && setTitle ? <NativeButton title={setTitle} compact busy={saving} disabled={inputBusy && !saving} onPress={() => setAction.current()} style={styles.secretFieldButton} /> : null}{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? inputBusy} onPress={handleClear} style={styles.secretFieldButton} /> : null}</View></> : <View style={[styles.secretFieldActions, compactStyles.inlineGap]}><NativeSecretInputControl label={label} hint={hint} busy={inputBusy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} />{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? inputBusy} onPress={handleClear} style={styles.secretActionButton} /> : null}</View>}</View></View>;
}

function PickerField({ label, value, values, onSelect, disabled, labelWidth, labelAlign, controlWidth, allowShrink = false, translate, accessory }: { label: string; value: string; values: Array<string | AssistantSettingOption>; onSelect: (value: string) => void; disabled?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; allowShrink?: boolean; translate?: Translate; /** Shared control that belongs to the picker's own row, such as its help mark. */ accessory?: React.ReactNode }): React.JSX.Element {
  const contextualTranslate = useContext(TranslationContext);
  const optionTranslator = translate ?? contextualTranslate;
  const options = ensureSelectedOption(optionTranslator ? assistantSettingOptions(values, optionTranslator) : values.map((option) => typeof option === "string" ? { value: option, label: option } : option), value);
  const selectedLabel = options.find((option) => option.value === value)?.label ?? value;
  return <View style={[styles.formRow, compactStyles.formRow]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><NativePicker labels={options.map((option) => option.label)} selectedValue={selectedLabel} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={[styles.picker, compactStyles.picker, allowShrink && styles.pickerShrink, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]} />{accessory ?? null}</View>;
}

function RawEditor({ label, domain, document, language, ipc, translate, showLabel = true, showDiff = true, codexPane = false, onConflict, reloadToken = 0, baselineToken = 0, syncRevision, style }: { label: string; domain: AssistantSettingsDomain; document: RawEditorDocument; language: "toml" | "json" | "yaml"; ipc: IpcClient; translate: Translate; showLabel?: boolean; showDiff?: boolean; codexPane?: boolean; onConflict: RawEditorConflictHandler; reloadToken?: number; baselineToken?: number; syncRevision?: number; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const [documentKey, setDocumentKey] = useState("");
  const [draft, setDraft] = useState("");
  const [baseline, setBaseline] = useState("");
  const [editorRenderRevision, setEditorRenderRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<"ready" | "dirty" | "saving" | "error">("ready");
  const [error, setError] = useState<string>();
  const [reloadNonce, setReloadNonce] = useState(0);
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(`${domain}:${document}:raw`));
  const tokenRef = useRef<string | undefined>(undefined);
  const draftRef = useRef("");
  const baselineRef = useRef("");
  const stagedTextRef = useRef("");
  const stageTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const stagePromise = useRef<Promise<void> | undefined>(undefined);
  const flushStageRef = useRef(false);
  const scheduleStageRef = useRef<() => void>(() => undefined);
  const mountedRef = useRef(true);
  const initializedRef = useRef(false);
  const lastStagedRevisionRef = useRef<number | undefined>(undefined);
  const observedSyncRevisionRef = useRef<number | undefined>(undefined);
  const externalSyncInFlightRef = useRef(false);
  const appliedReloadNonceRef = useRef(reloadNonce);
  const appliedBaselineTokenRef = useRef(baselineToken);

  const normalizeEditorText = (value: string): string => value.replace(/\r\n?/g, "\n");

  const setPending = useCallback((pending: boolean): void => {
    registry?.setDirty(fieldId.current, pending);
    if (pending) setStatus((current) => current === "saving" ? current : "dirty");
    else setStatus("ready");
  }, [registry]);

  const stageEditorText = useCallback(async (editorToken: string, submitted: string): Promise<IpcResults["editor"]> => {
    return ipc.stageEditor(editorToken, submitted);
  }, [ipc]);

  const stageLatest = useCallback((flush = true): Promise<void> => {
    if (stageTimer.current) {
      clearTimeout(stageTimer.current);
      stageTimer.current = undefined;
    }
    if (flush) flushStageRef.current = true;
    if (stagePromise.current) return stagePromise.current;
    const run = async (): Promise<void> => {
      for (;;) {
        const editorToken = tokenRef.current;
        const submitted = draftRef.current;
        if (!editorToken || submitted === stagedTextRef.current) {
          if (mountedRef.current) {
            setPending(false);
            setError(undefined);
          }
          return;
        }
        if (mountedRef.current) {
          setStatus("saving");
          setError(undefined);
        }
        try {
          const staged = await stageEditorText(editorToken, submitted);
          tokenRef.current = staged.editor_token;
          lastStagedRevisionRef.current = staged.revision;
          observedSyncRevisionRef.current = staged.revision;
          stagedTextRef.current = submitted;
          if (!flushStageRef.current) {
            if (draftRef.current === stagedTextRef.current) {
              if (mountedRef.current) {
                setPending(false);
                setError(undefined);
              }
            } else {
              setPending(true);
              scheduleStageRef.current();
            }
            return;
          }
        } catch (reason: unknown) {
          if (isEditorCapabilityConflict(reason)) {
            try {
              // A capability can become stale when unrelated Core state
              // advances, its previous response is lost, or the WebView
              // restarts. None of those conditions means this document was
              // changed elsewhere. Read the actual document first and only
              // ask the user when its content no longer matches our staged
              // baseline.
              let descriptor = await ipc.editor(domain, document);
              if (descriptor.text === submitted) {
                tokenRef.current = descriptor.editor_token;
                stagedTextRef.current = submitted;
                continue;
              }
              if (descriptor.text === stagedTextRef.current) {
                tokenRef.current = descriptor.editor_token;
                continue;
              }

              const resolution = await onConflict(domain, document);
              if (resolution === "reload") {
                if (mountedRef.current) {
                  setPending(false);
                  setError(undefined);
                }
                setReloadNonce((value) => value + 1);
                return;
              }

              // The confirmation path may refresh Core state. Acquire a
              // fresh token before preserving this window's draft.
              descriptor = await ipc.editor(domain, document);
              if (descriptor.text === submitted) {
                stagedTextRef.current = submitted;
              }
              tokenRef.current = descriptor.editor_token;
              continue;
            } catch (recoveryReason: unknown) {
              reason = recoveryReason;
            }
          }
          if (mountedRef.current) {
            setStatus("error");
            registry?.setDirty(fieldId.current, true);
            // A real editor conflict was already presented through the native
            // decision dialog above. If recovering that dialog/read fails,
            // keep the local draft and surface the transport problem rather
            // than falsely claiming an outside setting change.
            setError(isEditorCapabilityConflict(reason) ? translate("error.generic") : errorMessage(reason, translate));
          }
          throw reason;
        }
      }
    };
    const tracked = run().finally(() => {
      if (stagePromise.current === tracked) {
        stagePromise.current = undefined;
        flushStageRef.current = false;
      }
    });
    stagePromise.current = tracked;
    return tracked;
  }, [document, domain, ipc, onConflict, registry, setPending, stageEditorText, translate]);

  const scheduleStage = useCallback((): void => {
    if (stageTimer.current !== undefined) return;
    stageTimer.current = setTimeout(() => {
      stageTimer.current = undefined;
      void stageLatest(false).catch(() => undefined);
    }, RAW_EDITOR_SYNC_INTERVAL_MS);
  }, [stageLatest]);

  useEffect(() => {
    scheduleStageRef.current = scheduleStage;
    return () => { scheduleStageRef.current = () => undefined; };
  }, [scheduleStage]);

  const reset = useCallback((): void => {
    if (stageTimer.current) {
      clearTimeout(stageTimer.current);
      stageTimer.current = undefined;
    }
    draftRef.current = baselineRef.current;
    stagedTextRef.current = baselineRef.current;
    setDraft(baselineRef.current);
    setEditorRenderRevision((value) => value + 1);
    setStatus("ready");
    setError(undefined);
    registry?.setDirty(fieldId.current, false);
  }, [registry]);

  useEffect(() => {
    mountedRef.current = true;
    registry?.register(fieldId.current, {
      commit: stageLatest,
      reset,
      isDirty: () => draftRef.current !== stagedTextRef.current || stagePromise.current !== undefined,
    });
    return () => {
      mountedRef.current = false;
      if (stageTimer.current) clearTimeout(stageTimer.current);
      registry?.register(fieldId.current);
    };
  }, [registry, reset, stageLatest]);

  useEffect(() => {
    let active = true;
    const resetBaseline = !initializedRef.current
      || reloadNonce !== appliedReloadNonceRef.current
      || baselineToken !== appliedBaselineTokenRef.current;
    if (!initializedRef.current) {
      reset();
      tokenRef.current = undefined;
      setLoading(true);
    }
    setError(undefined);
    void ipc.editor(domain, document).then((descriptor) => {
      if (!active) return;
      tokenRef.current = descriptor.editor_token;
      draftRef.current = descriptor.text;
      stagedTextRef.current = descriptor.text;
      setDraft(descriptor.text);
      if (resetBaseline || descriptor.baseline !== baselineRef.current) {
        baselineRef.current = descriptor.baseline;
        setBaseline(descriptor.baseline);
        if (initializedRef.current) setEditorRenderRevision((value) => value + 1);
      }
      if (!initializedRef.current) setDocumentKey([domain, document].join(":"));
      initializedRef.current = true;
      appliedReloadNonceRef.current = reloadNonce;
      appliedBaselineTokenRef.current = baselineToken;
      registry?.setDirty(fieldId.current, false);
      setStatus("ready");
      setLoading(false);
    }).catch((reason: unknown) => {
      if (!active) return;
      setLoading(false);
      setError(errorMessage(reason, translate));
    });
    return () => { active = false; };
  }, [baselineToken, document, domain, ipc, registry, reloadNonce, reloadToken, reset, translate]);

  useEffect(() => {
    if (!initializedRef.current || syncRevision === undefined) return;
    if (observedSyncRevisionRef.current === syncRevision) return;
    if (stagePromise.current) {
      observedSyncRevisionRef.current = undefined;
      return;
    }
    observedSyncRevisionRef.current = syncRevision;
    if (lastStagedRevisionRef.current === syncRevision || externalSyncInFlightRef.current) return;
    externalSyncInFlightRef.current = true;
    let active = true;
    const synchronize = async (): Promise<void> => {
      try {
        let descriptor = await ipc.editor(domain, document);
        if (!active) return;
        if (descriptor.text === draftRef.current && descriptor.text === stagedTextRef.current) {
          tokenRef.current = descriptor.editor_token;
          if (descriptor.baseline !== baselineRef.current) {
            baselineRef.current = descriptor.baseline;
            setBaseline(descriptor.baseline);
            setEditorRenderRevision((value) => value + 1);
          }
          return;
        }
        if (descriptor.text === stagedTextRef.current) {
          // Core revisions also advance for unrelated settings. Keep a local
          // draft intact when the raw document itself did not change, while
          // refreshing its capability token for the next stage.
          tokenRef.current = descriptor.editor_token;
          if (descriptor.baseline !== baselineRef.current) {
            baselineRef.current = descriptor.baseline;
            setBaseline(descriptor.baseline);
            setEditorRenderRevision((value) => value + 1);
          }
          return;
        }
        if (draftRef.current !== stagedTextRef.current) {
          const resolution = await onConflict(domain, document);
          if (!active) return;
          if (resolution === "keep") {
            descriptor = await ipc.editor(domain, document);
            if (!active) return;
            tokenRef.current = descriptor.editor_token;
            if (descriptor.baseline !== baselineRef.current) {
              baselineRef.current = descriptor.baseline;
              setBaseline(descriptor.baseline);
              setEditorRenderRevision((value) => value + 1);
            }
            return;
          }
          setReloadNonce((value) => value + 1);
          return;
        }
        tokenRef.current = descriptor.editor_token;
        draftRef.current = descriptor.text;
        stagedTextRef.current = descriptor.text;
        baselineRef.current = descriptor.baseline;
        setDraft(descriptor.text);
        setBaseline(descriptor.baseline);
        setEditorRenderRevision((value) => value + 1);
        setPending(false);
        setError(undefined);
      } catch (reason: unknown) {
        if (active) setError(errorMessage(reason, translate));
      } finally {
        externalSyncInFlightRef.current = false;
      }
    };
    void synchronize();
    return () => { active = false; };
  }, [document, domain, ipc, onConflict, setPending, syncRevision, translate]);

  return <View style={[styles.rawEditor, codexPane && styles.codexRawEditorBase, style]}>
    {showLabel ? <View style={[styles.rawEditorHeader, codexPane && styles.codexRawEditorHeader]}><Text style={[styles.fieldLabel, codexPane && styles.codexRawEditorLabel]}>{label}</Text></View> : null}
    {documentKey
      ? <View style={styles.rawNativeEditorFrame}>
        <CodeEditorWebView
          documentKey={`${documentKey}:${editorRenderRevision}`}
          value={draft}
          baseline={baseline}
          language={language}
          readOnly={false}
          showDiff={showDiff}
          menuLabels={editorMenuLabels(translate)}
          style={[styles.rawNativeEditor, codexPane && styles.codexRawNativeEditor]}
          onChange={(text) => {
            if (!initializedRef.current || normalizeEditorText(text) === normalizeEditorText(draftRef.current)) return;
            draftRef.current = text;
            const pending = text !== stagedTextRef.current;
            setPending(pending);
            if (pending) scheduleStage();
          }}
          onError={() => {
            setStatus("error");
            setError(translate("common.secureEditorReadFailed"));
          }}
        />
        {loading ? <View pointerEvents="none" style={styles.rawEditorOverlay}><Text style={styles.cardHint}>{translate("common.secureEditorLoading")}</Text></View> : null}
      </View>
      : <View style={[styles.rawEditorLoading, codexPane && styles.codexRawEditorLoading]}><Text style={styles.cardHint}>{loading ? translate("common.loading") : translate("error.coreUnavailable")}</Text></View>}
    {error ? <Text style={styles.error}>{error}</Text> : null}
  </View>;
}

function modelProbePresentation(model: UnknownRecord, result: IpcResults["probe"] | null | undefined, translate: Translate): { compact: string; compactSentence: string; tooltip: string; full: string } {
  // `null` is the pane saying it has no finding for this route — it was just
  // asked again, or edited since — so Core's own copy of it is not shown
  // either.  `undefined` is a pane that has not measured the route: it paints
  // whatever Core kept.
  const resultRecord = result === null ? undefined : result as UnknownRecord | undefined;
  const probe = result === null ? ({} as UnknownRecord) : resultRecord ?? asRecord(model.probe);
  if (Object.keys(probe).length === 0) {
    return { compact: "", compactSentence: "", tooltip: "", full: "" };
  }
  const surfaces: Array<{ surface: string; available?: boolean; status?: string; original_request?: unknown }> = result?.surfaces
    ?? Object.entries(asRecord(probe.surfaces)).map(([surface, value]) => ({
      surface,
      available: booleanValue(asRecord(value).available),
      status: stringValue(asRecord(value).status),
      original_request: asRecord(value).original_request,
    }));
  const availableCount = surfaces.filter((surface) => surface.available === true).length;
  const unreachableCount = surfaces.filter((surface) => surface.status === "network_error").length;
  // A count keeps the line short: the per-surface detail lives in the details
  // view. A probe whose every request failed at the transport layer is reported
  // as unreachable rather than unavailable.
  // The row says one plain thing; the numbers and the long form live in the
  // hover hint and the details view.
  const transport = stringValue(asRecord(probe.summary).transport);
  // When every surface failed the same definitive way, name that way: a 403 is
  // a rejected key, not an unusable protocol.
  const surfaceStatuses = new Set(surfaces.map((surface) => stringValue(surface.status)));
  const uniformStatus = surfaceStatuses.size === 1 ? [...surfaceStatuses][0] : "";
  const availabilityInline = surfaces.length > 0
    ? availableCount === surfaces.length
      ? translate("providers.probeInlineReady")
      : availableCount > 0
        ? translate("providers.probeInlinePartial")
        : transport === "refused"
          ? translate("providers.probeInlineRefused")
          : transport === "timeout"
            ? translate("providers.probeInlineTimeout")
            : transport === "mixed"
              ? translate("providers.probeInlineMixed")
              : uniformStatus === "auth_error"
                ? translate("providers.probeInlineAuthError")
                : uniformStatus === "unsupported"
                  ? translate("providers.probeInlineUnsupported")
                  : uniformStatus === "http_error"
                    ? translate("providers.probeInlineHttpError")
                    : uniformStatus === "invalid_response"
                      ? translate("providers.probeInlineInvalidResponse")
                      : uniformStatus === "invalid_config"
                        ? translate("providers.probeInlineInvalidConfig")
                        : translate("providers.probeInlineBlocked")
    : booleanValue(probe.unreachable)
      ? translate("providers.probeSummaryUnreachable")
      : translate("providers.probeSummaryUnavailable");
  const availabilitySentence = surfaces.length > 0
    ? translate("providers.probeAvailabilityCount", { available: availableCount, total: surfaces.length })
    : availabilityInline;
  void unreachableCount;
  const summaryRecord = asRecord(probe.summary);
  const statuses = Object.entries(asRecord(summaryRecord.statuses))
    .map(([surface, status]) => `${probeSurfaceLabel(surface, translate)}: ${stringValue(status, "unavailable")}`)
    .join("; ");
  const summary = [availabilityInline, statuses].filter(Boolean).join("; ");
  const requests = surfaces.map((surface) => ({
    surface: surface.surface,
    status: surface.status ?? "unavailable",
    original_request: surface.original_request ?? {},
  }));
  const degradation = asRecord(result?.degradation ?? probe.degradation);
  // The row shows a word; the hover hint spells the same finding out.
  const degradationLine = probeDegradationLine(degradation, translate);
  const degradationSentence = probeDegradationLine(degradation, translate, true);
  // The row reads "3/3 可用; 未降智": a plain count first, then the one finding
  // worth knowing. Routes the staged engine cannot fingerprint carry no
  // degradation wording at all - not "no degradation", nothing.
  const deepTestIncluded = booleanValue(asRecord(model.deep_probe).includes_degradation);
  const availabilityCount = surfaces.length > 0
    ? translate("providers.probeAvailabilityCount", { available: availableCount, total: surfaces.length })
    : availabilityInline;
  const compactReason = (deepTestIncluded ? degradationLine : "")
    || (surfaces.length > 0 && availableCount === surfaces.length ? "" : availabilityInline);
  const compact = [availabilityCount, compactReason].filter(Boolean).join("; ");
  const full = [
    summary,
    probeDegradationDetails(degradation, translate),
    result?.detail ?? "",
    translate("providers.probeOriginalRequest", { request: JSON.stringify(requests, null, 2) }),
  ].filter(Boolean).join("\n\n");
  // One line: "结果: 3/3 可用; 未降智". The row owns the width, the text
  // ellipsizes instead of wrapping, and the hover hint repeats that line in full
  // before it spells the same finding out.
  const compactSentence = `${translate("providers.probeResultPrefix")} ${compact}`;
  const tooltip = joinProbeSummary([compactSentence, availabilitySentence, degradationSentence]);
  return { compact, compactSentence, tooltip, full };
}

/**
 * The wording for a request that never produced a result.
 *
 * "Rejected" is a rejection, "timed out" is silence, and a definitive upstream
 * answer (auth, missing protocol, error) keeps its own name. Nothing here may
 * call a rejection "no response".
 */
function probeFailureKey(degradation: UnknownRecord): TranslationKey {
  switch (stringValue(degradation.cause)) {
    case "refused":
      return "providers.probeInlineRefused";
    case "timeout":
      return "providers.probeInlineTimeout";
    case "auth_error":
      return "providers.probeInlineAuthError";
    case "unsupported":
      return "providers.probeInlineUnsupported";
    case "http_error":
      return "providers.probeInlineHttpError";
    case "invalid_response":
      return "providers.probeInlineInvalidResponse";
    case "network_error":
    case "skipped":
      return "providers.degradationUnreachable";
    default:
      return "providers.degradationError";
  }
}

/** Join probe findings without repeating one inside another. */
function joinProbeSummary(parts: string[]): string {
  const unique = parts.filter(Boolean).filter((text, index, all) => all.indexOf(text) === index);
  const kept = unique.filter((text) => !unique.some((other) => other !== text && other.includes(text)));
  return kept.join(" · ");
}

/**
 * One-line degradation summary for the model detail pane.
 *
 * A fingerprint mismatch never disables the model or clears its enable
 * checkbox: the pane reports the finding and leaves the routing decision to
 * the user.
 */
function probeDegradationLine(degradation: UnknownRecord, translate: Translate, detailed = false): string {
  if (Object.keys(degradation).length === 0) return "";
  const target = stringValue(degradation.target);
  const label = stringValue(degradation.label);
  switch (stringValue(degradation.status)) {
    case "matched":
      return detailed
        ? translate("providers.degradationMatchedDetail", { target: label || target })
        : translate("providers.degradationMatched");
    case "mismatch":
      return detailed
        ? translate("providers.degradationMismatchDetail", { label: label || "?", target })
        : translate("providers.degradationMismatch");
    case "unknown":
      return translate("providers.degradationUnknown");
    case "unavailable":
      return translate("providers.degradationUnavailable");
    case "error":
      return translate(probeFailureKey(degradation) === "providers.degradationUnreachable"
        ? "providers.degradationError"
        : probeFailureKey(degradation));
    case "unreachable":
      return translate(probeFailureKey(degradation));
    case "refused":
      return translate("providers.probeInlineRefused");
    case "timeout":
      return translate("providers.probeInlineTimeout");
    case "skipped":
      return translate("providers.degradationSkipped");
    default:
      return "";
  }
}

/** Detail block: the engine revision, the finding, and the parsed-answer size. */
function probeDegradationDetails(degradation: UnknownRecord, translate: Translate): string {
  if (Object.keys(degradation).length === 0) return "";
  const status = stringValue(degradation.status);
  if (status === "skipped") return "";
  const engine = asRecord(degradation.engine);
  const revision = stringValue(engine.revision).slice(0, 12);
  const engineLine = stringValue(engine.name)
    ? translate("providers.degradationEngine", { engine: stringValue(engine.name), revision: revision || "?" })
    : "";
  const detail = stringValue(degradation.detail);
  return [
    detail ? translate("providers.probeDegradationMessage", { result: detail }) : "",
    engineLine,
  ].filter(Boolean).join("\n");
}

function probeSurfaceLabel(surface: string, translate: Translate): string {
  if (surface === "openai/responses") return translate("providers.responses");
  if (surface === "openai/chat") return translate("providers.chat");
  if (surface === "anthropic") return translate("providers.anthropic");
  return translate("providers.probeNotRun");
}

function stringList(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }
function webSearchCapabilityChanges(value: unknown): UnknownRecord {
  const source = asRecord(value);
  const changes: UnknownRecord = {};
  for (const key of ["supports_responses_web_search", "supports_web_search"] as const) {
    if (typeof source[key] === "boolean") changes[key] = source[key];
  }
  return changes;
}
function responsesCompactionCapabilityChanges(value: unknown): UnknownRecord {
  // Only an explicit boolean in the model record/catalog counts as an opt-in;
  // an absent capability stays unknown (local checkpoint summary default).
  const source = asRecord(value);
  return typeof source.supports_responses_compaction === "boolean"
    ? { supports_responses_compaction: source.supports_responses_compaction }
    : {};
}
function modelRecordCapabilityChanges(value: unknown): UnknownRecord {
  return { ...webSearchCapabilityChanges(value), ...responsesCompactionCapabilityChanges(value) };
}
function apiKeyDisplayName(value: unknown, translate: Translate): string {
  const name = stringValue(value);
  if (!name) return translate("common.notAvailable");
  return name === "default" ? translate("providers.defaultKey") : name;
}
// A placeholder name every entry starts under until the user replaces it: a
// name already in use gets the next free numeric suffix ("新建供应商2").
function uniquePlaceholderName(existing: string[], base: string): string {
  const taken = new Set(existing.map((value) => value.trim().toLocaleLowerCase()));
  let candidate: string = base;
  let suffix = 2;
  while (taken.has(candidate.toLocaleLowerCase())) {
    candidate = `${base}${suffix}`;
    suffix += 1;
  }
  return candidate;
}
function uniqueKeyName(existing: string[]): string { let suffix = 1; let value = `key-${suffix}`; while (existing.includes(value)) { suffix += 1; value = `key-${suffix}`; } return value; }
const RANDOM_KEY_NAME_WORDS = ["coral", "maple", "cedar", "orbit", "nova", "pixel", "delta", "ember", "falcon", "grove", "harbor", "ivy", "jade", "lumen", "meadow", "nimbus", "oasis", "pebble", "quartz", "raven", "sable", "tide", "umber", "willow"] as const;
// A friendly default key name that is never the bare word "default": the
// wizard pre-fills the editable key name with a random word and the user can
// change it freely.
function randomKeyName(existing: string[]): string { const base: string = RANDOM_KEY_NAME_WORDS[Math.floor(Math.random() * RANDOM_KEY_NAME_WORDS.length)]; let candidate: string = base; let suffix = 2; while (existing.includes(candidate)) { candidate = `${base}-${suffix}`; suffix += 1; } return candidate; }
function groupBy(items: UnknownRecord[], key: (item: UnknownRecord) => string): Record<string, UnknownRecord[]> { return items.reduce<Record<string, UnknownRecord[]>>((groups, item) => { const group = key(item); (groups[group] ??= []).push(item); return groups; }, {}); }

function semanticColor(macos: string, windows: string | undefined, fallback: string): ReturnType<typeof PlatformColor> | string {
  if (Platform.OS === "macos") return PlatformColor(macos);
  if (Platform.OS === "windows" && windows) return PlatformColor(windows);
  return fallback;
}

const systemColors = {
  window: semanticColor("windowBackgroundColor", "SolidBackgroundFillColorBase", "#f7f7f7"),
  control: semanticColor("controlBackgroundColor", "ControlFillColorDefault", "#ffffff"),
  textBackground: semanticColor("textBackgroundColor", "Window", "#ffffff"),
  label: semanticColor("labelColor", "TextFillColorPrimary", "#1d1d1f"),
  secondaryLabel: semanticColor("secondaryLabelColor", "TextFillColorSecondary", "#6e6e73"),
  separator: semanticColor("separatorColor", "ControlStrokeColorDefault", "#d4d4d8"),
  blue: semanticColor("systemBlueColor", "AccentTextFillColorPrimary", "#0a84ff"),
  selectedContent: semanticColor("selectedContentBackgroundColor", "AccentFillColorDefault", "#0a84ff"),
  unemphasizedSelectedContent: semanticColor("unemphasizedSelectedContentBackgroundColor", "SubtleFillColorSecondary", "#e5e5ea"),
  selectedControlText: semanticColor("alternateSelectedControlTextColor", "TextOnAccentFillColorPrimary", "#ffffff"),
  red: semanticColor("systemRedColor", undefined, "#b00020"),
  green: semanticColor("systemGreenColor", undefined, "#2f6b3d"),
  brown: semanticColor("systemBrownColor", undefined, "#6f5500"),
} as const;

/**
 * The shared grid's own style objects, spread by every pane's field rows so
 * one edit to the geometry moves all three panes together.  A pane whose rows
 * carry the modified marker (runtime settings) leaves the marker plus the row
 * gap fill the lead, so its labels land on the same x as a pane that indents
 * the row instead.
 */
const SETTINGS_FIELD_LIST: ViewStyle = { gap: 10 };
const SETTINGS_FIELD: ViewStyle = { minWidth: 0, alignSelf: "stretch", gap: 2 };
const SETTINGS_FIELD_ROW: ViewStyle = { minHeight: 26, flexDirection: "row", alignItems: "center", gap: SETTINGS_FIELD_GAP };
const SETTINGS_FIELD_ROW_INDENTED: ViewStyle = { ...SETTINGS_FIELD_ROW, paddingLeft: SETTINGS_FIELD_LEAD };
const SETTINGS_FIELD_LABEL: TextStyle = { width: SETTINGS_FIELD_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" };
const SETTINGS_FIELD_VALUE_SLOT: ViewStyle = { width: SETTINGS_FIELD_VALUE_WIDTH, height: 26, flexShrink: 0, justifyContent: "center" };
const SETTINGS_FIELD_ROUTE_SLOT: ViewStyle = { ...SETTINGS_FIELD_VALUE_SLOT, width: SETTINGS_FIELD_ROUTE_WIDTH };
const SETTINGS_FIELD_HELP_SLOT: ViewStyle = { paddingLeft: SETTINGS_FIELD_HELP_INDENT, paddingTop: 1, minWidth: 0, alignSelf: "stretch" };
const SETTINGS_FIELD_HELP_TEXT: TextStyle = { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14, minWidth: 0, flexShrink: 1 };
/**
 * The switch slot the boolean fields of every pane share.  A switch starts
 * flush with the control column, exactly like a picker, a text field, or an
 * action button on the same grid: an inset switch reads as a broken column.
 */
const SETTINGS_FIELD_SWITCH_SLOT: ViewStyle = { width: 44, minWidth: 44, height: 24, alignSelf: "flex-start" };

// Data-management is a utility window, but its active pane still needs a
// readable rhythm. Keep these adjustments together so the three panes share
// the same inset, helper-copy treatment, and native-preference spacing.
const dataManagementPolishStyles = StyleSheet.create({
  paneScrollContent: { flexGrow: 1, paddingTop: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: SETTINGS_PANE_INSET, gap: 14 },
  paneHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  importIntro: { minHeight: 0, paddingHorizontal: 12, paddingVertical: 0, gap: 10 },
});

const assistantFileSurfaceStyles = StyleSheet.create({
  editorRoute: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.window },
  editorRouteRaw: { flex: 1, minHeight: 0 },
  editorLoading: { flex: 1, minHeight: 0, justifyContent: "center", paddingHorizontal: 14 },
  editorError: { flexShrink: 0, color: systemColors.red, fontSize: UI_TIP_FONT_SIZE },
  filesStatus: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  // A file row is a pane item, not a table row: its name, path, and action
  // are separated from the next file by the pane's own rhythm. A rule under
  // every row (and one more under the last) turns four files into a stack of
  // dividers, so the detail body stays rule-free below its header band.
  // The file rows start on the same x as the field labels above them: the
  // lead every field row carries is part of the pane's grid, not the fields'.
  fileRow: { minHeight: 36, flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 4, paddingLeft: SETTINGS_FIELD_LEAD },
  fileMeta: { flex: 1, minWidth: 0, gap: 1 },
  fileLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  filePathLink: { alignSelf: "stretch", minWidth: 0, flexGrow: 0, flexShrink: 1 },
  fileMissing: { flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  editFileButton: { minWidth: 92 },
  editorHeader: { minHeight: 52, flexDirection: "row", alignItems: "center", gap: 10, paddingHorizontal: 14, paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: systemColors.separator, backgroundColor: systemColors.window },
  editorHeaderCopy: { flex: 1, minWidth: 0, gap: 2 },
  editorTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  editorPath: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  // One footer row holds the only two actions this surface owns.  Removing
  // the old hint leaves the actions right-aligned like every other pane
  // footer, with an explicit gap so a mis-click cannot land on the other one.
  editorFooter: { minHeight: 50, flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 12, paddingHorizontal: 12, paddingVertical: 9, borderTopWidth: 1, borderTopColor: systemColors.separator, backgroundColor: systemColors.window }, editorFooterStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  editorFooterActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 10 },
});

const styles = StyleSheet.create({
  routeTraceWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 6 },
  routeTraceRequestPane: { width: "31%", minWidth: 252, maxWidth: 360, minHeight: 0, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground },
  routeTraceRequestScroll: { flex: 1, minHeight: 0 },
  routeTraceRequestList: { flexGrow: 1 },
  routeTraceRequestRow: { height: ROUTE_TRACE_REQUEST_ROW_HEIGHT, paddingHorizontal: 9, paddingVertical: 8, gap: 3, borderBottomWidth: 1, borderBottomColor: systemColors.separator },
  routeTraceRequestRowPressed: { backgroundColor: systemColors.separator },
  routeTraceRequestRowSelected: { backgroundColor: systemColors.selectedContent, borderBottomColor: systemColors.selectedContent },
  routeTraceRequestRowSelectedInactive: { backgroundColor: systemColors.unemphasizedSelectedContent, borderBottomColor: systemColors.unemphasizedSelectedContent },
  routeTraceRequestTextSelected: { color: systemColors.selectedControlText },
  routeTraceRequestHeading: { flexDirection: "row", alignItems: "center", gap: 8 },
  routeTraceRequestModel: { flex: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  routeTraceRequestTime: { flexShrink: 0, color: systemColors.label, fontSize: UI_TIP_FONT_SIZE },
  routeTraceRequestPath: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  routeTraceOutcome: { alignSelf: "flex-start", fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  routeTraceOutcomeDirect: { color: systemColors.label },
  routeTraceOutcomeFallback: { color: systemColors.brown },
  routeTraceOutcomeFailed: { color: systemColors.red },
  routeTraceDetailPane: { flex: 1, minWidth: 0, minHeight: 0, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground },
  routeTraceDetailHeader: { minHeight: 54, flexDirection: "row", alignItems: "center", gap: 12, paddingHorizontal: 14, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: systemColors.separator, backgroundColor: systemColors.window },
  routeTraceDetailTitleBlock: { flex: 1, minWidth: 0, gap: 2 },
  routeTraceDetailTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  routeTraceDetailMeta: { color: systemColors.label, fontSize: UI_TIP_FONT_SIZE },
  routeTracePathHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8, paddingHorizontal: 14, paddingTop: 12 },
  routeTraceSectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  routeTracePathCount: { color: systemColors.label, fontSize: UI_TIP_FONT_SIZE },
  routeTracePathSummary: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 14, paddingTop: 5, paddingBottom: 10 },
  routeTraceTimelineFrame: { flex: 1, minWidth: 0, minHeight: 0, position: "relative" },
  routeTraceTimelineScroll: { flex: 1, minHeight: 0, borderTopWidth: 1, borderTopColor: systemColors.separator },
  routeTraceTimeline: { flexGrow: 1, minWidth: ROUTE_TRACE_TIMELINE_MIN_WIDTH, paddingHorizontal: 14, paddingVertical: 14, gap: 10 },
  routeTraceTimelineWithHorizontalScrollbar: { paddingBottom: 26 },
  routeTraceTimelineHorizontalScrollbarTrack: { position: "absolute", left: 14, right: 14, bottom: 4, height: 10, borderRadius: 5, backgroundColor: systemColors.separator, overflow: "hidden" },
  routeTraceTimelineHorizontalScrollbarThumb: { height: 10, borderRadius: 5, backgroundColor: systemColors.secondaryLabel, opacity: 0.85 },
  routeTraceTimelineRow: { flexDirection: "row", minWidth: 0, gap: 10 },
  routeTraceTimelineRail: { width: 22, minHeight: 58, flexShrink: 0, position: "relative", alignItems: "center", paddingTop: 7 },
  routeTraceTimelineLine: { position: "absolute", top: 25, bottom: -17, left: 10, width: 2, backgroundColor: systemColors.separator },
  routeTraceTimelineNode: { width: 18, height: 18, borderRadius: 9, alignItems: "center", justifyContent: "center", zIndex: 1, backgroundColor: systemColors.control },
  routeTraceTimelineNodeStart: { backgroundColor: systemColors.secondaryLabel },
  routeTraceTimelineNodeSelected: { backgroundColor: systemColors.green },
  routeTraceTimelineNodeFailed: { backgroundColor: systemColors.red },
  routeTraceTimelineNodeAttempted: { borderWidth: 1, borderColor: systemColors.secondaryLabel, backgroundColor: systemColors.control },
  routeTraceTimelineNodeText: { color: systemColors.label, fontSize: 10, fontWeight: "400", lineHeight: 12 },
  routeTraceTimelineNodeTextActive: { color: systemColors.selectedControlText },
  routeTraceStartCard: { flex: 1, minWidth: 0, minHeight: 58, paddingHorizontal: 11, paddingVertical: 9, gap: 4, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 6, backgroundColor: systemColors.control },
  routeTraceStepMetaRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 },
  routeTraceStepNumber: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  routeTraceStepLabel: { color: systemColors.label, fontSize: UI_TIP_FONT_SIZE },
  routeTraceStepValue: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  routeTraceStepCard: { flex: 1, minWidth: 0, minHeight: 58, paddingHorizontal: 11, paddingVertical: 9, gap: 4, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 6, backgroundColor: systemColors.control },
  routeTraceStepCardSelected: { borderColor: systemColors.green, borderWidth: 2 },
  routeTraceStepCardFailed: { borderColor: systemColors.red, borderWidth: 2 },
  routeTraceStepTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  routeTraceStepState: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 5 },
  routeTraceStepStateIcon: { width: 14, height: 14, borderRadius: 7, alignItems: "center", justifyContent: "center" },
  routeTraceStepStateIconSelected: { backgroundColor: systemColors.green },
  routeTraceStepStateIconFailed: { backgroundColor: systemColors.red },
  routeTraceStepStateIconAttempted: { borderWidth: 1, borderColor: systemColors.secondaryLabel, backgroundColor: systemColors.control },
  routeTraceStepStateIconText: { width: 14, height: 14, color: systemColors.selectedControlText, fontSize: 10, fontWeight: "700", lineHeight: 14, textAlign: "center" },
  routeTraceStepStateIconTextAttempted: { color: systemColors.secondaryLabel },
  routeTraceStepStateText: { fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  routeTraceStepStateSelected: { color: systemColors.green },
  routeTraceStepStateFailed: { color: systemColors.red },
  routeTraceStepStateAttempted: { color: systemColors.label },
  routeTraceStepDetail: { color: systemColors.label, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 },
  routeTraceNoPath: { padding: 14, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.window },
  routeTraceNoPathText: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  routeTraceNoSelection: { flex: 1, alignItems: "center", justifyContent: "center", padding: 20 },
  routeTraceNoSelectionText: { color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "center" },
  root: { flex: 1, minWidth: 420 },
  menuBarHost: { flex: 1 }, error: { margin: 20, color: systemColors.red, fontSize: UI_FONT_SIZE },
  windowSurface: { flex: 1, position: "relative", backgroundColor: systemColors.window }, windowContent: { flexGrow: 1, paddingHorizontal: 16, paddingTop: 12, paddingBottom: 6, gap: 8 }, windowContentFixed: { flex: 1, minHeight: 0 }, fileEditorRouteContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0, gap: 0 }, providersContent: { paddingBottom: 6, gap: 6 }, providerWizardRouteContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0, gap: 0 }, providerWizardSurface: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.window }, logsContent: { paddingHorizontal: 12, paddingTop: 8, paddingBottom: 0 }, runtimeContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 }, dataManagementContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 }, assistantSettingsContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 }, windowTitleBlock: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 3, gap: 3 }, windowTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  // Settings window: a native source-list sidebar next to the active pane.
  settingsShell: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row" }, settingsSidebar: { width: 200, flexShrink: 0, minHeight: 0, borderRightWidth: 1, borderRightColor: systemColors.separator }, settingsSidebarHeader: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 16, paddingTop: SETTINGS_TITLEBAR_INSET + 10, paddingBottom: 8 }, settingsSidebarAppIcon: { width: SETTINGS_HEADER_CONTENT_HEIGHT, height: SETTINGS_HEADER_CONTENT_HEIGHT, borderRadius: 4 }, settingsSidebarTitle: { color: systemColors.label, fontSize: SOURCE_LIST_FONT_SIZE, fontWeight: "600" }, settingsSidebarSpacer: { flex: 1, minHeight: 8 }, settingsSidebarList: { flex: 1, minHeight: 0 }, settingsRail: { width: SETTINGS_RAIL_WIDTH, flexShrink: 0, minHeight: 0, paddingTop: 6, overflow: "hidden" }, settingsRailList: { flex: 1, minHeight: 0 }, settingsRailDivider: { position: "absolute", top: 0, bottom: 0, right: 0, width: 1, backgroundColor: systemColors.separator }, settingsRailDetail: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.textBackground }, settingsRailDetailHeader: { minHeight: 50, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 12, paddingHorizontal: 14, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: systemColors.separator, backgroundColor: systemColors.window }, settingsRailDetailTitleBlock: { flex: 1, minWidth: 0, gap: 2 }, settingsRailDetailActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 }, settingsRailDetailTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, settingsRailDetailHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 }, settingsDetail: { minWidth: 0, flex: 1, paddingTop: SETTINGS_TITLEBAR_INSET }, settingsDetailBody: { flex: 1, minHeight: 0, backgroundColor: systemColors.textBackground }, settingsDetailBodyBare: { backgroundColor: "transparent" }, settingsDetailPane: { flex: 1, minWidth: 0 }, settingsPaneHeader: { flexShrink: 0, paddingHorizontal: 20, paddingTop: 10, paddingBottom: 8 }, settingsPaneTitle: { color: systemColors.label, fontSize: 15, fontWeight: "600", lineHeight: SETTINGS_HEADER_CONTENT_HEIGHT },  routeStatusBar: { minHeight: 24, flexShrink: 0, justifyContent: "center", paddingHorizontal: 16, paddingVertical: 4, borderTopWidth: 1, borderTopColor: systemColors.separator }, routeStatusText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  generalScroll: { flex: 1, minHeight: 0, backgroundColor: systemColors.textBackground }, generalContent: { paddingTop: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: 16, gap: 18 }, generalSection: { gap: 4 }, generalSectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, generalRow: { ...SETTINGS_FIELD_ROW_INDENTED }, generalRowLabel: { ...SETTINGS_FIELD_LABEL }, generalRowValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, generalToggle: { ...SETTINGS_FIELD_SWITCH_SLOT }, generalHelpSlot: { ...SETTINGS_FIELD_HELP_SLOT }, generalHelpText: { ...SETTINGS_FIELD_HELP_TEXT }, generalServiceAction: { minWidth: 96 }, providerToolbar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardToolbarButton: { minWidth: 104 }, toolbarSpacer: { flex: 1 }, windowTabs: { width: 224, height: 24 },
  providerWizardSetupContent: { flex: 1, minHeight: 0, justifyContent: "flex-start", alignItems: "center", paddingHorizontal: 24, paddingTop: 18, paddingBottom: 12 }, providerWizardSetupSurface: { width: "100%", maxWidth: 520, minWidth: 0, gap: 12 }, providerWizardSetupSurfaceModel: { flex: 1, minHeight: 0 }, providerWizardSignInPanel: { width: "100%", minHeight: 160, justifyContent: "center", gap: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 7, backgroundColor: systemColors.control, paddingHorizontal: 16, paddingVertical: 18 }, providerWizardAuthRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 8 }, providerWizardAuthStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  providerMiddlePane: { flex: 1, minWidth: 0, gap: 6 },
  keysPane: { flex: 1, minWidth: 0, minHeight: 0 }, keysInline: { minWidth: 0, gap: 6, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator }, keysInlineBody: { minWidth: 0, height: KEYS_INLINE_LIST_HEIGHT, flexDirection: "row", alignItems: "flex-start", gap: 6 }, keysTableInline: { flex: 1, minWidth: 0, height: KEYS_INLINE_LIST_HEIGHT, minHeight: KEYS_INLINE_LIST_HEIGHT }, keysEditorInline: { width: KEYS_INLINE_EDITOR_WIDTH, minWidth: 0, flexGrow: 0, flexShrink: 0, gap: 5 }, keysEditorField: { minWidth: 0, gap: 2 }, keysEditorFieldHeader: { height: 22, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 4 }, keysEditorFieldLabel: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE },
  keysTable: { flex: 1, minHeight: 120 },
  keysEditor: { minWidth: 0, gap: 5, paddingTop: 5, paddingLeft: 8, borderLeftWidth: 2, borderLeftColor: systemColors.separator },
  keysHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, flexShrink: 1 },
  panelActionButton: { width: 22, minWidth: 22, height: 22 }, keysEditorValueRow: { minWidth: 0, flexDirection: "row", alignItems: "center", gap: 6 }, keysEditorValueField: { flex: 1, minWidth: 0 },
  providerAccountsHeader: { minWidth: 0, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator },
  panelHeader: { minHeight: 22, flexDirection: "row", alignItems: "center", gap: 6 },
  panelTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "700" },
  panelActions: { marginLeft: "auto", flexDirection: "row", alignItems: "center", gap: 4 },
  officialAccountSection: { minWidth: 0, gap: 5, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator },
  officialStatusRow: { minHeight: 22, flexDirection: "row", alignItems: "center", gap: 6 },
  providerAuthLine: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  officialActiveHint: { color: systemColors.green, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  officialActionsRow: { minHeight: 28, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 6 },
  providerWizardHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerWizardDescription: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 17 }, providerWizardSectionHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardPanelTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerWizardFormSection: { width: "100%", maxWidth: 520, paddingVertical: 0, gap: 10 }, providerWizardModelScroll: { flex: 1, minHeight: 0, width: "100%" }, providerWizardModelScrollContent: { width: "100%", paddingBottom: 8 }, providerWizardModelToolbar: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, providerWizardModelGroup: { gap: 6, paddingTop: 4 }, providerWizardModelGroupHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, providerWizardModelList: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, paddingHorizontal: 8, paddingVertical: 5, gap: 1 }, providerWizardModelCheckbox: { width: "100%", minHeight: 24 }, providerWizardManualModelRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardManualModelCheckbox: { flex: 1, minWidth: 0 }, providerWizardManualModelUpstream: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE }, providerWizardModelSummary: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500", paddingTop: 4 }, providerWizardModeControl: { width: "100%", height: 26, flexShrink: 0 }, providerWizardPicker: { width: "100%", minWidth: 0, height: 26 }, providerWizardInput: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, providerWizardSecretInput: { width: "100%", minHeight: 26 }, providerWizardHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, paddingVertical: 2 }, providerWizardFooter: { minHeight: 46, paddingHorizontal: 20, paddingVertical: 8, flexDirection: "row", alignItems: "center", gap: 6, borderTopWidth: 0, backgroundColor: systemColors.window }, providerWizardFooterSpacer: { flex: 1 }, providerWizardFooterStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, providerWizardFooterActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 },
  routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 },
  // The model detail's own label column, so every row in that pane — the
  // fields, the rate row, the protocol pickers, and the window row — draws its
  // label on one grid.  Its width is the pane's longest rendered label
  // (Codex 上下文, measured 80.9 pt in the 13 pt host face) plus COLUMN_GAP:
  // a column wider than that leaves the control column short of the room the
  // fixed 290 pt pane was built to give it, and a narrower one wraps.  Every
  // row in the pane must use it — a row that falls back to formRowLabel's 112
  // steps out of the column and reads as broken alignment.
  modelInspectorLabel: { width: MODEL_INSPECTOR_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" },
  providerAuthStatusLabel: { width: 88, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE },
  providerAuthStatusValue: { flex: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE },
  // The provider workspace's first row is the left column's 24 pt toolbar (the
  // 供应商 / 路由 tabs and the 添加向导… button). The inspector pane's own header
  // row is the same 24 pt row and starts at the pane's top with no extra inset,
  // so 供应商: <name> reads on one line with that button instead of sitting a few
  // points below it, and the sections under it keep the left column's row pitch.
  providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }, providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }, providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }, routeWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, fetchKeyPicker: { width: 170, height: 24, marginRight: 6, flexShrink: 0 }, providerListPane: { width: 140, minWidth: 140, maxWidth: 140, flexGrow: 0, flexShrink: 0 }, modelListPane: { flex: 1, minWidth: 0 }, tablePane: { flex: 1, minWidth: 0, gap: 6 }, tablePaneWide: { flex: 1, minWidth: 0 }, tableTitleRow: { height: 24, flexDirection: "row", alignItems: "center" }, tableTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, tableActions: { marginLeft: "auto", flexDirection: "row", gap: 6 }, iconButton: { minWidth: 22, width: 22, minHeight: 22, height: 22, alignItems: "center", justifyContent: "center" }, tableBottomRow: { minHeight: 26, flexDirection: "row", alignItems: "center" }, nativeProviderTable: { flex: 1, minHeight: 0 }, nativeModelTable: { flex: 1, minHeight: 0 }, nativeRouteTable: { flex: 1, minHeight: 0 }, providerInspector: { width: 290, minWidth: 290, maxWidth: 290, flexGrow: 0, flexShrink: 0 }, providerEditorContent: { flex: 1, minHeight: 0 }, providerEditorScrollContent: { paddingLeft: 0, paddingRight: 16, paddingBottom: 12, gap: 6 }, persistentScrollIndicator: { position: "absolute", width: 0, height: 0 }, providerEditorHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerEditorHeading: { flex: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerReturnToModel: { flexShrink: 1 }, providerEditorSection: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 3, gap: 4 }, providerEnabledRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, providerSourceFields: { minWidth: 0, gap: 4 }, inspectorContent: { paddingLeft: 0, paddingRight: 6, paddingBottom: 12, gap: 6 }, inspectorBody: { gap: 4 }, modelBreadcrumb: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }, breadcrumbProvider: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, breadcrumbSeparator: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorHeading: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorDivider: { height: 1, backgroundColor: systemColors.separator }, inspectorEnabledRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, // A link is also a cursor: the row shows the pointing hand over the whole
// finding, label included, exactly like every native link in the app.
inspectorProbeFinding: { flexShrink: 1, minWidth: 0, cursor: "pointer" }, inspectorProbeFindingText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, textDecorationLine: "underline" }, inspectorEnableControl: { flexShrink: 0 }, orderEditorRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, orderEditorField: { flex: 1, width: undefined }, orderFollowControl: { flexShrink: 0 }, modelWindowText: { flexShrink: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE }, protocolSettings: { gap: 4 }, helpTipAnchor: { position: "relative", zIndex: 2 }, helpTipDismiss: { position: "absolute", left: -2400, right: -2400, top: -2400, bottom: -2400 }, helpTipButton: { width: 16, height: 16, minWidth: 16, minHeight: 16 }, helpTipPopup: { position: "absolute", right: 0, bottom: 22, width: 208, paddingHorizontal: 8, paddingVertical: 6, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 6, backgroundColor: systemColors.control, shadowColor: "#000000", shadowOpacity: 0.18, shadowRadius: 8, shadowOffset: { width: 0, height: 2 } }, helpTipText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  externalSettingsWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 8 }, externalSettingsFieldList: { ...SETTINGS_FIELD_LIST }, externalSettingsField: { ...SETTINGS_FIELD }, externalSettingsInputRow: { ...SETTINGS_FIELD_ROW_INDENTED }, externalSettingsFieldLabel: { ...SETTINGS_FIELD_LABEL }, externalSettingsValueSlot: { ...SETTINGS_FIELD_ROUTE_SLOT }, externalSettingsBooleanControl: { ...SETTINGS_FIELD_SWITCH_SLOT }, externalSettingsHelpSlot: { ...SETTINGS_FIELD_HELP_SLOT }, externalSettingsHelpText: { ...SETTINGS_FIELD_HELP_TEXT }, externalSettingsPane: { flex: 1, minHeight: 0 }, externalSettingsPaneContent: { paddingVertical: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET, gap: 14 }, codexRawEditorBase: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0, gap: 5 }, codexRawEditorHeader: { minHeight: 18 }, codexRawEditorLabel: { fontFamily: Platform.select({ macos: "Menlo", windows: "Cascadia Mono", default: "monospace" }), fontWeight: "600" }, codexRawNativeEditor: { minHeight: 0 }, codexRawEditorLoading: { minHeight: 0 },
  runtimeWorkspaceFrame: { flex: 1, minHeight: 0, gap: 6 }, runtimeWorkspaceBody: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 8 }, runtimeWorkspace: { width: "100%", padding: SETTINGS_PANE_INSET, gap: 14 }, runtimeScrollSurface: { flex: 1, minWidth: 0, backgroundColor: systemColors.textBackground }, runtimeFieldList: { ...SETTINGS_FIELD_LIST }, runtimeField: { ...SETTINGS_FIELD }, runtimeInputRow: { ...SETTINGS_FIELD_ROW_INDENTED }, runtimeModifiedBar: { position: "absolute", left: 0, top: 3, bottom: 3, width: 2, borderRadius: 1, backgroundColor: "transparent" }, runtimeModifiedBarInline: { width: 2, alignSelf: "center", height: 16, borderRadius: 1, backgroundColor: "transparent" }, runtimeModifiedBarActive: { backgroundColor: systemColors.blue }, runtimeFieldError: { marginLeft: SETTINGS_FIELD_HELP_INDENT, color: systemColors.red, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, runtimeValueControlInvalid: { borderWidth: 1, borderColor: systemColors.red, borderRadius: 4 }, runtimeResetButton: { minWidth: 28, width: 28, height: 22, paddingHorizontal: 0 }, runtimeFieldLabel: { ...SETTINGS_FIELD_LABEL }, runtimeValueSlot: { ...SETTINGS_FIELD_VALUE_SLOT }, runtimeValueControl: { width: SETTINGS_FIELD_VALUE_WIDTH, minWidth: SETTINGS_FIELD_VALUE_WIDTH, height: 26 }, runtimeBooleanControl: { ...SETTINGS_FIELD_SWITCH_SLOT }, runtimeUnit: { width: 68, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, runtimeActionSlot: { width: 72, minHeight: 26, flexShrink: 0, justifyContent: "center" }, runtimeHelpSlot: { ...SETTINGS_FIELD_HELP_SLOT }, runtimeHelpText: { ...SETTINGS_FIELD_HELP_TEXT }, runtimeMultilineField: { minWidth: 0, flexGrow: 1, flexBasis: "100%", maxWidth: "100%" }, runtimeMultilineHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8, paddingLeft: SETTINGS_FIELD_LEAD }, runtimeMultilineLabel: { flex: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, runtimeMultilineHeaderActions: { flexShrink: 0, minHeight: 26, justifyContent: "center" }, runtimeMultilineEditor: { width: "100%", minWidth: 0, height: 108, flex: 1, alignSelf: "stretch" }, runtimeMultilineHelpSlot: { marginLeft: 0, maxWidth: "100%", minWidth: 0, paddingTop: 6, gap: 3 }, runtimeJsonDefaultHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, fontWeight: "600", minWidth: 0 },
  dataManagementWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 8 }, dataManagementPane: { flex: 1, minHeight: 0 }, dataManagementPaneScrollContent: { paddingTop: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: 4, gap: 10 }, dataManagementImportIntro: { width: "100%", minHeight: 72, paddingHorizontal: 12, paddingVertical: 12, justifyContent: "center" }, dataManagementImportFileRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: SETTINGS_FIELD_GAP, paddingLeft: SETTINGS_FIELD_LEAD }, dataManagementImportFileLabel: { ...SETTINGS_FIELD_LABEL }, dataManagementImportFileValue: { flex: 1, minWidth: 0, minHeight: 26, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.textBackground }, dataManagementImportFilePlaceholder: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, dataManagementSelectionBar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementSelectionCount: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementToolbarButtons: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 }, dataManagementSectionsField: { flex: 1, minWidth: 0, gap: 6 }, dataManagementSectionList: { gap: 4 }, dataManagementSectionItem: { minHeight: 22 }, dataManagementActionStatus: { flexShrink: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14 }, dataManagementSensitiveHint: { color: systemColors.brown, fontSize: UI_FONT_SIZE, lineHeight: 16, paddingVertical: 5, paddingHorizontal: 7, backgroundColor: Platform.select({ macos: (PlatformColor("systemYellow") as unknown as { withAlphaComponent?: (alpha: number) => string })?.withAlphaComponent?.(0.08) ?? "rgba(255, 204, 0, 0.08)", default: "rgba(255, 204, 0, 0.08)" }), borderRadius: 4, borderWidth: 1, borderColor: Platform.select({ macos: (PlatformColor("systemYellow") as unknown as { withAlphaComponent?: (alpha: number) => string })?.withAlphaComponent?.(0.2) ?? "rgba(255, 204, 0, 0.2)", default: "rgba(255, 204, 0, 0.2)" }) }, dataManagementSyncScopeValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementDirectionPicker: { width: SETTINGS_FIELD_VALUE_WIDTH, height: 24, flexGrow: 0, flexShrink: 0 },
  // The WebDAV tab is fields of the same grid the other panes draw, so its
  // list is the shared list rather than a form of its own.
  webdavFieldList: { ...SETTINGS_FIELD_LIST }, webdavPasswordInput: { width: SETTINGS_FIELD_VALUE_WIDTH, minHeight: 26 }, webdavFieldUnit: { width: 68, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  logsWindow: { flex: 1, minHeight: 0, gap: 4 }, logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, logFilterRow: { width: 360, minWidth: 220, maxWidth: 360, height: 26, flexDirection: "row", alignItems: "center", gap: 8 }, logToolbarSpacer: { flex: 1, minWidth: 0 }, logActionsRow: { height: 26, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, clearCooldownButton: { minWidth: 96, height: 22 }, toolbarLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, flexShrink: 0 }, logFilterInput: { flex: 1, minWidth: 0, height: 26 }, logsTabs: { width: 640, maxWidth: "100%", minWidth: 0, height: 28, flexShrink: 0 }, logTableFrame: { flex: 1, minHeight: 0, minWidth: 0 }, logTable: { flex: 1, minHeight: 0 }, logEmptySurface: { flex: 1, minHeight: 0, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, logEmptyText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, textAlign: "center", paddingHorizontal: 20 }, fieldLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, fieldError: { color: systemColors.red, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, input: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, inputInvalid: { borderWidth: 1, borderColor: systemColors.red, borderRadius: 4 }, textArea: { minHeight: 108, textAlignVertical: "top", fontFamily: "Menlo" }, compactTextArea: { minHeight: 56, maxHeight: 56 }, secretFieldActions: { flexDirection: "row", alignItems: "center", gap: 6 }, secretFieldButtons: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 }, secretFieldButton: { flex: 1, minWidth: 0, height: 26 }, nativeSecretControl: { flex: 1, minWidth: 0, minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, nativeSecretInput: { flex: 1, minWidth: 86, minHeight: 26 }, rawEditor: { flex: 1, minHeight: 180, gap: 4 }, rawEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, rawNativeEditorFrame: { flex: 1, minHeight: 160, position: "relative" }, rawNativeEditor: { flex: 1, minHeight: 160 }, rawEditorOverlay: { position: "absolute", left: 0, right: 0, top: 0, bottom: 0, justifyContent: "center", alignItems: "center", gap: 8, paddingHorizontal: 12, backgroundColor: systemColors.textBackground }, rawEditorLoading: { flex: 1, minHeight: 160, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, empty: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, paddingVertical: 12 }, cardHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, marginTop: 2 },
  nativeSecretMultilineControl: { alignItems: "flex-start", minHeight: 108 },
  nativeSecretTextArea: { minHeight: 108, height: 108, alignSelf: "stretch" },
  secretActionButton: { width: 64, minWidth: 64, height: 26, flexShrink: 0 },
  formRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, formRowStacked: { alignItems: "flex-start" }, formRowSecretStacked: { alignItems: "flex-start" }, formRowLabel: { width: 112, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, formRowLabelStacked: { paddingTop: 4 }, formRowControl: { flex: 1, minWidth: 0, gap: 3 }, picker: { flex: 1, minWidth: 180, height: 26 }, pickerShrink: { minWidth: 0 },
});
