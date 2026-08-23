import React, { createContext, useCallback, useEffect, useMemo, useRef, useState, useContext } from "react";
import { AppState, FlatList, Platform, PlatformColor, Pressable, ScrollView, StyleSheet, Text, View, type HostInstance, type StyleProp, type TextStyle, type ViewStyle } from "react-native";
import { createTranslator } from "../i18n";
import { assistantSettingOptions, codexFeatureLabel, localizeCodexValidationMessage, type AssistantSettingOption } from "../i18n/assistantSettingsI18n";
import { runtimeCategoryLabel, runtimeFieldHelp, runtimeFieldLabel, runtimeOptionLabel, runtimeUnitLabel } from "../i18n/runtimeSettingsI18n";
import { canonicalWindowRoute, LOG_TABS, routeMenuActions, ROUTES } from "../routes";
import { NativeButton, NativeCheckbox, NativePersistentScrollIndicator, NativePicker, NativeSecureTextInput, NativeSegmentedControl, NativeSplitView, NativeTable, NativeTextField } from "./NativeControls";
import { CODE_EDITOR_HTML, CodeEditorWebView } from "./code-editor/CodeEditorWebView";
import { NativeFormRow, NativeWizardProgress, normalizeRelayOrigin, relayNavigationItems, RelayAccountManager, stationOriginKey } from "./RelayAccountManager";
import { suggestedRelayStationName } from "./relayOrigin";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";
import type {
  AppRoute,
  ConfigDomain,
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
type PendingField = { commit: () => void | Promise<void>; reset: () => void; isDirty?: () => boolean };
type PendingFieldRegistry = {
  register: (id: symbol, field?: PendingField) => void;
  setDirty: (id: symbol, dirty: boolean) => void;
};
type ProviderWorkspaceDraftProjection = {
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
type AssistantSettingsDomain = "codex" | "claude";
type EditableDiskDomain = AssistantSettingsDomain | "providers_models" | "runtime" | "webdav";
type RawEditorConflictResolution = "reload" | "keep";
type RawEditorConflictHandler = (domain: AssistantSettingsDomain, document: RawEditorDocument) => Promise<RawEditorConflictResolution>;
type RawEditorDocument = "config" | "auth" | "settings" | "desktop" | "developer";
type ClaudeDeploymentDraft = { model: string; base_url: string };
type DataManagementTab = "import" | "export" | "webdav";
type WebDavSyncAction = "sync" | "push" | "pull";

const PROVIDER_AUTH_OPTIONS = ["api_key", "openai_login", "claude_login"] as const satisfies readonly ProviderAuthKind[];

function providerAuthKind(provider: UnknownRecord | undefined): ProviderAuthKind {
  const value = stringValue(provider?.auth_kind, "api_key");
  return PROVIDER_AUTH_OPTIONS.includes(value as ProviderAuthKind) ? value as ProviderAuthKind : "api_key";
}

function providerAuthStatus(provider: UnknownRecord | undefined): ProviderAuthStatus {
  const value = stringValue(provider?.auth_status, "signed_out");
  const statuses: readonly ProviderAuthStatus[] = ["signed_out", "authorizing", "signed_in", "expired", "error", "unsupported"];
  return statuses.includes(value as ProviderAuthStatus) ? value as ProviderAuthStatus : "signed_out";
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

const PendingFieldContext = createContext<PendingFieldRegistry | undefined>(undefined);
const TranslationContext = createContext<Translate | undefined>(undefined);
const ProviderWorkspaceDraftContext = createContext<ProviderWorkspaceDraftProjection | undefined>(undefined);

function providerModelDraftKey(providerID: string, modelID: string): string {
  return `${providerID}\x1f${modelID}`;
}

function providerKeyDraftKey(providerID: string, keyID: string): string {
  return `${providerID}\x1f${keyID}`;
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
  section: { paddingTop: 8, gap: 6 },
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
const LOG_VIEW_POLL_MS = 5_000;
const RECOVERY_LOG_POLL_MS = 1_000;
const ONLINE_USAGE_POLL_MS = 15_000;
const ROUTE_TRACE_REQUEST_ROW_HEIGHT = 70;
const ROUTE_TRACE_SCROLL_IDLE_MS = 150;
const ROUTE_TRACE_TIMELINE_MIN_WIDTH = 400;
const ROUTE_TRACE_SCROLLBAR_MIN_THUMB_WIDTH = 32;
const SETTINGS_STRUCTURED_CONTENT_MIN_WIDTH = 420;
const WEBDAV_FORM_LABEL_WIDTH = 108;
const SETTINGS_STRUCTURED_SCROLLBAR_GUTTER = 18;
const COLUMN_GAP = 8;
const DSH_VISION_ROUTER_QUICK_KEYS = [
  "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED",
  "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND",
  "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK",
  "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
  "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS",
  "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED",
  "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED",
] as const;
const DSH_VISION_ROUTER_CONFIG_KEY = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON";
const DSH_VISION_ROUTER_QUICK_DEFAULTS: Record<string, string> = {
  LITELLM_MENU_DSH_VISION_ROUTER_ENABLED: "on",
  LITELLM_MENU_DSH_VISION_ROUTER_BACKEND: "auto",
  LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK: "on",
  LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS: "45",
  LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS: "4096",
  LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED: "off",
  LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED: "off",
};
// Ordinary configuration text stays local until blur, submit, or Apply. Core
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

export interface LiteLLMMenuAppProps {
  ipc: IpcClient;
  native: NativeLeafAdapter;
  translate: Translate;
  initialSnapshot?: CoreSnapshot;
  routeRequest?: AppRoute;
  routeRequestSequence?: number;
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
    return id && name && baseURL ? [{ id, name, baseURL }] : [];
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
  const sourceName = choice.source
    ? [choice.source.accountLabel, choice.source.resourceLabel].filter(Boolean).join("/")
    : name;
  const multiplier = choice.source && Number.isFinite(choice.source.multiplier) ? ` (${choice.source.multiplier}x)` : "";
  return `${sourceName || name}${multiplier}`;
}

function modelOrderMode(model: UnknownRecord): "manual" | "relay_multiplier" {
  return model.order_mode === "relay_multiplier" ? "relay_multiplier" : "manual";
}

function modelEffectiveOrder(model: UnknownRecord): number {
  return numberValue(model.effective_order, numberValue(model.order, 0));
}

function modelProviderKeyLabel(model: UnknownRecord, provider: UnknownRecord, translate: Translate, keyName?: (keyID: string, name: string) => string): string {
  const key = providerKeyStates(provider).find((entry) => entry.id === stringValue(model.provider_key_id));
  return key ? keyName?.(key.id, key.name) ?? key.name : stringValue(model.api_key_name, translate("common.default"));
}

function isApplyResult(value: unknown): value is IpcResults["apply"] {
  const result = asRecord(value);
  return (result.status === "applied" || result.status === "partial" || result.status === "failed")
    && typeof result.completed_operations === "number"
    && typeof result.pending_operations === "number"
    && Array.isArray(result.issues);
}

function applyResultMessage(result: IpcResults["apply"], translate: Translate): string {
  if (result.status === "partial") return translate("relay.applyPartial", { completed: result.completed_operations, pending: result.pending_operations });
  if (result.status === "failed") return translate("relay.applyFailed", { pending: result.pending_operations });
  return translate("common.applied");
}

function applyIssuesForDisplay(result: IpcResults["apply"]): ValidationSummary["issues"] {
  return result.issues.map((issue) => ({
    path: [issue.domain, issue.station_id, issue.account_id, issue.resource_id, issue.provider_id, issue.model_id].filter(Boolean).join(" / "),
    code: issue.code ?? "relay_apply",
    message: issue.message ?? "",
    severity: result.status === "partial" ? "warning" : "error",
  }));
}

function errorMessage(reason: unknown, translate: Translate): string {
  const code = stringValue(asRecord(reason).code);
  if (reason instanceof Error && reason.message === "Claude supports at most 3 fallback models") {
    return translate("validation.claudeFallbackModelLimit");
  }
  const keyByCode: Record<string, string> = {
    confirmation_required: "error.confirmationRequired",
    revision_conflict: "error.revisionConflict",
    validation_failed: "error.validationFailed",
  };
  return translate(keyByCode[code] ?? "error.generic");
}

function isEditorCapabilityConflict(reason: unknown): boolean {
  const code = stringValue(asRecord(reason).code);
  return code === "invalid_editor" || code === "revision_conflict";
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
  return value === "providers_models" || value === "codex" || value === "claude" || value === "runtime" || value === "webdav";
}

function claudeDeploymentFromSnapshot(snapshot: CoreSnapshot | undefined): ClaudeDeploymentDraft {
  const settings = asRecord(domainState(snapshot, "claude").settings);
  return { model: stringValue(settings.model), base_url: stringValue(settings.gateway_url) };
}

function sameClaudeDeployment(left: ClaudeDeploymentDraft, right: ClaudeDeploymentDraft): boolean {
  return left.model === right.model && left.base_url === right.base_url;
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
  return translate("menu.logsSummary", { recovering, cooldown });
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

function containsPrivateMarker(value: unknown): boolean {
  if (typeof value === "string") return value.includes("configured") || value.includes("<private-path>");
  if (Array.isArray(value)) return value.some(containsPrivateMarker);
  return Object.values(asRecord(value)).some(containsPrivateMarker);
}

function editableRecord(value: UnknownRecord): UnknownRecord {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => !containsPrivateMarker(item)));
}

export function LiteLLMMenuApp({ ipc, native, translate: hostTranslate, initialSnapshot, routeRequest, routeRequestSequence, logTabRequest, nativeAction, isPrimaryHost = true, isWindowManagerHost = false }: LiteLLMMenuAppProps): React.JSX.Element {
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
  const startupAttempted = useRef(false);
  const serviceOperationQueue = useRef<Promise<void>>(Promise.resolve());
  const acceptedSnapshotRevision = useRef<number>(initialSnapshot?.revision ?? -1);
  // Core can be recreated after an IPC/subscription recovery, so its local
  // change_event counter may start over. Deduplicate by the actual catalog
  // signature instead of by that process-local counter.
  const presentedCatalogRestartSignature = useRef<string | undefined>(undefined);
  const acknowledgedCatalogRestartSignature = useRef<string | undefined>(undefined);
  const catalogRestartConfirmationOpen = useRef(false);

  const recordMenuAction = useCallback(async (action: string): Promise<void> => {
    try {
      await ipc.dispatch({ domain: "logs", type: "logs.record_menu_action", payload: { tab: "menu", menu_action: action } });
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
      appTitle: translate("app.title"), autoStart: translate("menu.autoStart"), serviceUnavailable: translate("error.coreUnavailable"),
      serviceStatus: translate("service.status", { status: "{status}" }),
      serviceStarting: translate("service.starting"), serviceRunning: translate("service.running"),
      serviceRunningOnPort: translate("service.runningOnPort", { port: "{port}" }),
      serviceUnhealthy: translate("service.unhealthy"), serviceStopped: translate("service.stopped"),
      serviceUnknown: translate("service.unknown"),
      languageMenu: translate("menu.language"), languageSystem: translate("language.system"),
      languageEnglish: translate("language.english"), languageSimplifiedChinese: translate("language.simplified_chinese"),
      cancel: translate("menu.cancel"), set: translate("common.set"), clear: translate("common.clear"), stage: translate("common.stageRaw"), find: translate("common.find"),
      findNext: translate("common.findNext"), edit: translate("common.edit"), undo: translate("common.undo"),
      redo: translate("common.redo"), cut: translate("common.cut"), copy: translate("common.copy"),
      paste: translate("common.paste"), selectAll: translate("common.selectAll"), settings: translate("menu.codex"),
      reload: translate("menu.reload"), closeWindow: translate("menu.close"), menuQuit: translate("menu.quit"), version: translate("common.version"),
      build: translate("common.build"), ok: translate("common.ok"), invalidText: translate("common.invalidText"),
      routeHome: translate("route.home"), routeProvidersModels: translate("card.providersModels"),
      routeCodexSettings: translate("menu.codex"), routeClaudeSettings: translate("card.claudeSettings"),
      routeRuntimeSettings: translate("card.runtimeSettings"),
      routeDataManagement: translate("card.dataManagement"), routeRelayAccounts: translate("route.relayAccounts"), routeRelayAdd: translate("relay.addAccount"), routeProviderWizard: translate("providers.wizard.title"), routeLogs: translate("card.logs"),
      providerAuthInstruction: translate("relay.officialProviderWebViewHint"),
      providerAuthCode: translate("providers.authUserCode"),
      providerAuthCopy: translate("common.copy"),
      modelChooserTitle: translate("modelChooser.title"), modelChooserHeading: translate("modelChooser.heading"),
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
    if (!isPrimaryHost || !snapshot || startupAttempted.current || !serviceShouldBeRunning.current) return;
    startupAttempted.current = true;
    if (snapshot.service.state === "stopped") void runServiceOperation("start");
  }, [isPrimaryHost, runServiceOperation, snapshot?.service.state]);

  useEffect(() => {
    if (!routeRequest) return;
    if (isWindowManagerHost) {
      if (routeRequest !== "home") {
        const windowRoute = canonicalWindowRoute(routeRequest);
        native.window.open(windowRoute);
        native.window.focus(windowRoute);
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
      { id: "toggle-autostart", title: translate("menu.autoStart"), enabled: true, checked: snapshot.service.auto_start_state === "enabled" },
      ...(Platform.OS === "macos" ? [{ id: "toggle-codex-model-catalog", title: translate("menu.codexModelCatalog"), enabled: true, checked: booleanValue(catalog.enabled) }] : []),
      ...routeMenuActions(translate).filter(({ id }) => id !== "open-data-management" && id !== "open-logs"),
      { id: "webdav-status", title: `${translate("webdav.label")}: ${webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate)}`, enabled: false },
      ...routeMenuActions(translate).filter(({ id }) => id === "open-data-management"),
      { id: "open-logs", title: recoveryLogMenuTitle(snapshot.service, translate), enabled: true },
      { id: "service-start", title: translate("service.start"), enabled: serviceStartAvailable },
      { id: "service-stop", title: translate("service.stop"), enabled: !serviceOperationPending && serviceActive },
      { id: "service-restart", title: translate("service.restart"), enabled: serviceRestartAvailable },
      { id: "service-reload", title: translate("service.reload"), enabled: serviceReloadAvailable },
      { id: "service-health", title: translate("service.health"), enabled: !serviceOperationPending },
      { id: "language-menu", title: translate("menu.language"), enabled: true },
      { id: "set-language-system", title: translate("language.system"), enabled: true, checked: snapshot.language === "system" },
      { id: "set-language-en", title: translate("language.english"), enabled: true, checked: snapshot.language === "en" },
      { id: "set-language-zh-Hans", title: translate("language.simplified_chinese"), enabled: true, checked: snapshot.language === "zh-Hans" },
      { id: "show-version", title: translate("menu.version"), enabled: true },
      { id: "quit", title: translate("menu.quit"), enabled: true },
    ];
    native.menuBar.setActions(actions);
  }, [isPrimaryHost, native, serviceOperationPending, snapshot, translate]);

  return (
    <View style={styles.root} accessibilityLabel={translate("app.title")}>
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {!error && route !== "home" && snapshot ? <RouteSurface route={route} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={receiveSnapshot} onNavigate={setRoute} onClose={() => setRoute(route === "provider-wizard" && Platform.OS === "windows" ? "providers-models" : "home")} /> : null}
      {!error && route === "home" ? <View style={styles.menuBarHost} /> : null}
    </View>
  );
}

function WindowTitle({ title, validation }: { title: string; validation?: string }): React.JSX.Element {
  return <View style={styles.windowTitleBlock}><Text style={styles.windowTitle}>{title}</Text>{validation ? <Text style={styles.validationText}>{validation}</Text> : null}</View>;
}

function DialogFooter({ status, leading, children, compact = false, borderless = false }: { status?: string; leading?: React.ReactNode; children: React.ReactNode; compact?: boolean; borderless?: boolean }): React.JSX.Element {
  return <View style={[styles.footer, compact && styles.footerCompact, borderless && styles.footerBorderless]}>{leading ?? (status ? <Text numberOfLines={1} style={styles.footerStatus}>{status}</Text> : <View />)}<View style={styles.footerSpacer} /><View style={styles.footerButtons}>{children}</View></View>;
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

function RouteSurface({ route, snapshot, ipc, native, translate, logTabRequest, nativeAction, onSnapshot, onNavigate, onClose }: { route: AppRoute; snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; translate: Translate; logTabRequest?: LogTab; nativeAction?: { id: string; sequence: number }; onSnapshot: (next: CoreSnapshot) => void; onNavigate: (route: AppRoute) => void; onClose: () => void }): React.JSX.Element {
  const settingsRoute = isAssistantSettingsRoute(route);
  const [settingsTab, setSettingsTab] = useState<AssistantSettingsDomain>(route === "claude-settings" ? "claude" : "codex");
  const [serviceProviderSelection, setServiceProviderSelection] = useState<string>();
  const serviceProviderAddButtonRef = useRef<HostInstance | null>(null);
  const [claudeDeploymentDraft, setClaudeDeploymentDraft] = useState<ClaudeDeploymentDraft>();
  const claudeDeploymentDraftRef = useRef<ClaudeDeploymentDraft | undefined>(undefined);
  const domain = settingsRoute ? settingsTab : domainForRoute(route);
  const [busy, setBusy] = useState(false);
  const [webDavOperationBusy, setWebDavOperationBusy] = useState(false);
  const [result, setResult] = useState<string>();
  const [issues, setIssues] = useState<ValidationSummary["issues"]>([]);
  const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);
  const [settingsRawBaselineToken, setSettingsRawBaselineToken] = useState(0);
  const [dataManagementStatuses, setDataManagementStatuses] = useState<Partial<Record<DataManagementTab, string>>>({});
  const [keptDiskGeneration, setKeptDiskGeneration] = useState<Partial<Record<EditableDiskDomain, number>>>({});
  const promptedDiskGeneration = useRef<Partial<Record<EditableDiskDomain, number>>>({});
  const diskPromptInFlight = useRef(false);
  const activeRuns = useRef(0);
  const webDavOperationInFlight = useRef(false);
  const revision = useRef<number | undefined>(snapshot?.revision);
  const latestSnapshot = useRef<CoreSnapshot | undefined>(snapshot);
  const dispatchQueue = useRef<Promise<void>>(Promise.resolve());
  const probedSurfaceApplyQueue = useRef<Promise<void>>(Promise.resolve());
  const importPlanToken = useRef<string | undefined>(undefined);
  const lastDispatchError = useRef<unknown>(undefined);
  const pendingFields = useRef(new Map<symbol, PendingField>());
  const [, forcePendingFieldDirtyRender] = useState(0);
  const pendingFieldDirtyIdsRef = useRef<ReadonlySet<symbol>>(new Set());
  const claudeDeployment = claudeDeploymentDraft ?? claudeDeploymentFromSnapshot(snapshot);
  const hasClaudeDeploymentChanges = (currentSnapshot: CoreSnapshot | undefined): boolean => !sameClaudeDeployment(
    claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(currentSnapshot),
    claudeDeploymentFromSnapshot(currentSnapshot),
  );
  // Relay CRUD and linked imports are one coordinated draft. The relay route
  // therefore applies both domains together whenever either side is dirty.
  const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {
    if (settingsRoute) {
      return (["codex", "claude"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);
    }
    if (route === "data-management") {
      return DATA_MANAGEMENT_DIRTY_DOMAINS.filter((name) => currentSnapshot?.drafts[name]?.dirty);
    }
    if (route === "relay-accounts") {
      return (["relay_accounts", "providers_models"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);
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

  useEffect(() => {
    if (isAssistantSettingsRoute(route)) {
      setSettingsTab(route === "claude-settings" ? "claude" : "codex");
      claudeDeploymentDraftRef.current = undefined;
      setClaudeDeploymentDraft(undefined);
    } else {
      claudeDeploymentDraftRef.current = undefined;
      setClaudeDeploymentDraft(undefined);
    }
  }, [route]);

  const serviceProviderRows = useMemo(() => {
    const statusLabels: Record<ProviderAuthStatus, string> = {
      signed_out: translate("providers.authStatusSignedOut"),
      authorizing: translate("providers.authStatusAuthorizing"),
      signed_in: translate("providers.authStatusSignedIn"),
      expired: translate("providers.authStatusExpired"),
      error: translate("providers.authStatusError"),
      unsupported: translate("providers.authStatusUnsupported"),
    };
    const officialRows = serviceProviderRecords(snapshot).map((provider) => {
      const kind = providerAuthKind(provider) === "claude_login" ? "claude_login" : "openai_login";
      return {
        key: "provider:" + editorIdentifier(provider),
        cells: [stringValue(provider.display_name, stringValue(provider.name, serviceProviderKindLabel(kind, translate))), serviceProviderKindLabel(kind, translate), statusLabels[providerAuthStatus(provider)]],
      };
    });
    const relayRows = relayNavigationItems(snapshot, translate).map((item) => ({
      key: item.key,
      // Keep the station → account hierarchy visible in the compact unified
      // list. NativeTable preserves the leading spaces in the first cell.
      cells: [item.kind === "account" ? "  " + item.label : item.label, item.secondary, item.kind === "account" ? item.secondary.split(" · ").slice(-1)[0] : translate("relay.station")],
    }));
    return [...officialRows, ...relayRows];
  }, [snapshot, translate]);
  useEffect(() => {
    if (route !== "relay-accounts") {
      setServiceProviderSelection(undefined);
      return;
    }
    if (serviceProviderSelection && serviceProviderRows.some((row) => row.key === serviceProviderSelection)) return;
    setServiceProviderSelection(serviceProviderRows[0]?.key);
  }, [route, serviceProviderRows, serviceProviderSelection]);

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
    message: string | null = "common.applied",
    keepControlsEnabled = false,
    refreshAfter = true,
    dataManagementTab?: DataManagementTab,
  ): Promise<void> => {
    const publishResult = (next: string | undefined): void => {
      if (dataManagementTab) {
        setDataManagementStatuses((current) => ({ ...current, [dataManagementTab]: next }));
        return;
      }
      setResult(next);
    };
    activeRuns.current += 1;
    if (!keepControlsEnabled) setBusy(true);
    publishResult(undefined);
    try {
      const value = await operation();
      if (asRecord(value).cancelled === true) {
        publishResult(undefined);
      } else if (isValidation(value)) {
        setIssues(value.issues);
        publishResult(value.valid ? translate("common.applied") : `${value.issues.length} ${translate("common.validationIssues")}`);
      } else if (isApplyResult(value)) {
        setIssues(applyIssuesForDisplay(value));
        publishResult(applyResultMessage(value, translate));
      } else {
        setIssues([]);
        publishResult(message === null ? undefined : translate(message));
      }
      if (refreshAfter) await refresh();
    } catch (reason: unknown) {
      publishResult(errorMessage(reason, translate));
    } finally {
      activeRuns.current -= 1;
      if (!keepControlsEnabled && activeRuns.current === 0) setBusy(false);
    }
  };
  const runDataManagement = (tab: DataManagementTab, operation: () => Promise<unknown>, message: string | null, keepControlsEnabled = false): Promise<void> => run(operation, message, keepControlsEnabled, true, tab);
  const runWebDavOperation = async (operation: () => Promise<unknown>, message: string): Promise<void> => {
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
  const enqueueDispatch = (type: string, payload: UnknownRecord = {}, targetDomain = domain): Promise<{ revision: number }> => {
    if (!targetDomain) return Promise.reject(new Error("A settings domain is required"));
    const queued = dispatchQueue.current.catch(() => undefined).then(async () => {
      lastDispatchError.current = undefined;
      if (revision.current === undefined) await refresh();
      const staged = await ipc.dispatch({ domain: targetDomain, type, payload }, revision.current);
      revision.current = staged.revision;
      return staged;
    }).catch((reason: unknown) => {
      lastDispatchError.current = reason;
      throw reason;
    });
    dispatchQueue.current = queued.then(() => undefined, () => undefined);
    return queued;
  };
  const dispatch: Dispatch = (type, payload = {}, targetDomain = domain) => run(async () => {
    const staged = await enqueueDispatch(type, payload, targetDomain);
    if (targetDomain === "codex" || targetDomain === "claude") {
      setSettingsRawReloadToken((current) => current + 1);
    }
    return staged;
  // Text inputs no longer call this path per keystroke: usePendingTextField
  // keeps their draft local and only reaches dispatch on blur/submit/Apply.
  // Refresh each committed action so toggles and pickers also project their
  // new Core state immediately instead of waiting for the disk watcher.
  }, null, true);
  const dispatchWithOutcome = async (type: string, payload: UnknownRecord = {}, targetDomain = domain, keepControlsEnabled = false): Promise<CoreSnapshot | undefined> => {
    let succeeded = false;
    let outcome: CoreSnapshot | undefined;
    await run(async () => {
      const staged = await enqueueDispatch(type, payload, targetDomain);
      succeeded = true;
      // Capture the refresh belonging to this action. Reading
      // latestSnapshot.current after run() returns is racy with the
      // authorisation status poll, which can overwrite its operation summary.
      outcome = await refresh();
      return staged;
    }, null, keepControlsEnabled, false);
    return succeeded ? outcome : undefined;
  };
  const addOfficialAccount = async (kind: ServiceProviderKind): Promise<void> => {
    const currentProviders = serviceProviderRecords(latestSnapshot.current ?? snapshot);
    const name = nextServiceProviderName(currentProviders, kind);
    const next = await dispatchWithOutcome("service_provider.add", { kind, name }, "providers_models");
    if (!next) return;
    const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
    const summaryID = stringValue(summary.provider_id);
    const added = serviceProviderRecords(next).find((provider) => {
      const displayName = stringValue(provider.display_name, stringValue(provider.name)).trim();
      return displayName === name;
    });
    const providerID = summaryID || (added ? editorIdentifier(added) : "");
    if (!providerID) throw new Error("service_provider.add did not return provider_id");
    setServiceProviderSelection("provider:" + providerID);
    await dispatchWithOutcome("service_provider.auth_start", { provider_id: providerID }, "providers_models");
  };
  const openServiceProviderAddMenu = (): void => {
    const items = [
      serviceProviderKindLabel("openai_login", translate) + " " + translate("relay.officialProviderAddLogin"),
      serviceProviderKindLabel("claude_login", translate) + " " + translate("relay.officialProviderAddLogin"),
      translate("relay.addAccount"),
    ];
    const choose = (index: number | undefined): void => {
      if (index === 0) void addOfficialAccount("openai_login").catch((reason) => setResult(errorMessage(reason, translate)));
      else if (index === 1) void addOfficialAccount("claude_login").catch((reason) => setResult(errorMessage(reason, translate)));
      else if (index === 2) native.window.open("relay-add");
    };
    const button = serviceProviderAddButtonRef.current;
    if (!button || typeof button.measureInWindow !== "function") {
      void native.showActionMenu({ title: translate("relay.chooseProviderType"), items, anchor: { x: 0, y: 0, width: 0, height: 0 } }).then(choose);
      return;
    }
    button.measureInWindow((x, y, width, height) => {
      void native.showActionMenu({ title: translate("relay.chooseProviderType"), items, anchor: { x, y, width, height } }).then(choose);
    });
  };
  const commitRelayMetadata: Dispatch = async (type, payload = {}, targetDomain = domain) => {
    // Relay credential cleanup needs the Core acknowledgement itself, not the
    // UI-oriented `dispatch` wrapper: that wrapper deliberately absorbs
    // failures so ordinary controls can display them in the footer.
    await enqueueDispatch(type, payload, targetDomain);
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
    await Promise.all([...pendingFields.current.values()].map((field) => field.commit()));
    await dispatchQueue.current;
    if (lastDispatchError.current !== undefined) throw lastDispatchError.current;
  };
  const hasPendingFieldEdits = useCallback((): boolean => pendingFieldDirtyIdsRef.current.size > 0
    || [...pendingFields.current.values()].some((field) => field.isDirty?.() === true), []);
  // Actions can finish with a fresh Core snapshot before the parent route has
  // rendered it into `snapshot`. Keep the footer and close decision on the
  // same newest projection so Apply cannot look enabled while Close sees a
  // clean draft (or vice versa). Equal revisions prefer the prop: it is the
  // rendered source of truth and may contain a newer same-revision projection.
  const actionSnapshot = latestSnapshot.current && latestSnapshot.current.revision > (snapshot?.revision ?? -1)
    ? latestSnapshot.current
    : snapshot;
  const routeHasStagedChanges = useCallback((currentSnapshot: CoreSnapshot | undefined): boolean => (
    stagedDomainsForRoute(currentSnapshot).length > 0
    || hasPendingFieldEdits()
    || (settingsRoute && hasClaudeDeploymentChanges(currentSnapshot))
  ), [hasPendingFieldEdits, settingsRoute, stagedDomainsForRoute]);
  const monitoredDiskDomains = useMemo<EditableDiskDomain[]>(() => {
    if (!isSettingsRoute(route)) return [];
    if (settingsRoute) return ["codex", "claude"];
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
  const switchSettingsTab = (next: AssistantSettingsDomain): void => {
    if (next === settingsTab) return;
    const previous = settingsTab;
    // Start pending commits before changing the tree so the focused field is
    // not discarded, but change the tab immediately so the native segmented
    // control never appears inert while an IPC write is in flight.
    const pending = flushPendingFields();
    setSettingsTab(next);
    void pending.catch((reason: unknown) => {
      setSettingsTab(previous);
      setResult(errorMessage(reason, translate));
    });
  };
  const reload = (reloadDomain = domain): Promise<void> => {
    if (!reloadDomain) return Promise.resolve();
    if (reloadDomain === domain) discardPendingFields();
    return run(async () => {
      const reloaded = await ipc.reload(reloadDomain, revision.current);
      revision.current = reloaded.revision;
      // Core invalidates editor revisions when a domain reloads. Make both raw
      // Codex editors fetch fresh documents only after that reload succeeds.
      if (reloadDomain === "codex" || reloadDomain === "claude") setSettingsRawBaselineToken((current) => current + 1);
      if (reloadDomain === "claude") {
        claudeDeploymentDraftRef.current = undefined;
        setClaudeDeploymentDraft(undefined);
      }
      if (isEditableDiskDomain(reloadDomain)) {
        setKeptDiskGeneration((current) => ({ ...current, [reloadDomain]: undefined }));
      }
      return reloaded;
    }, "common.applied");
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
      confirmLabel: translate("menu.reload"),
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
  const apply = (): Promise<void> => {
    if ((route !== "relay-accounts" && !domain) || domain === "logs") return Promise.resolve();
    return run(async () => {
      await flushPendingFields();
      // Inline relay edits stage through the shared dispatch queue when their
      // fields lose focus. Wait for that queue before taking the apply
      // snapshot so the footer Apply button is the only remote commit point.
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
        const accepted = await native.showConfirmation({ title: translate("claude.confirmation.required"), message: translate("claude.confirmation.required"), confirmLabel: translate("screen.confirm") });
        if (!accepted) return { cancelled: true };
      }
      const confirmations = [...risks, ...diskConflicts.map((name) => `overwrite_external_${name}`)];
      const result = settingsRoute || route === "relay-accounts"
        ? await ipc.applyDomains([...domains], refreshed.revision, confirmations.length > 0 ? confirmations : undefined)
        : domain === undefined
          ? await ipc.applyDomains([...domains], refreshed.revision, confirmations.length > 0 ? confirmations : undefined)
          : await ipc.apply(domain, refreshed.revision, confirmations.length > 0 ? confirmations : undefined);
      if (domains.includes("codex") || domains.includes("claude")) {
        setSettingsRawBaselineToken((current) => current + 1);
      }
      if (!result.domains?.includes("relay_accounts") && result.status === "applied" && (domains.includes("providers_models") || domains.includes("runtime")) && (refreshed.service.state === "running" || refreshed.service.state === "unhealthy")) {
        // Provider Apply reloads the managed proxy in CoreStore. Runtime-only
        // changes still use the existing UI lifecycle operation.
        if (!domains.includes("providers_models")) {
          const reloaded = await ipc.dispatch({ type: "service.reload" }, result.revision);
          revision.current = reloaded.revision;
        }
      }
      if (domains.includes("claude")) {
        claudeDeploymentDraftRef.current = undefined;
        setClaudeDeploymentDraft(undefined);
      }
      if (diskConflicts.length > 0) setKeptDiskGeneration({});
      return result;
    });
  };
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
      const afterApply = await refresh();
      if (applied.status === "applied" && ["running", "unhealthy", "starting"].includes(afterApply.service.state)) {
        const restarted = await ipc.dispatch({ type: "service.restart" }, revision.current ?? afterApply.revision);
        revision.current = restarted.revision;
        await refresh();
      }
      completed = applied.status === "applied";
      return applied;
    }, null, false, false);
    return completed;
  };
  const applyDataManagement = (selectedSections: ConfigDomain[]): Promise<void> => runDataManagement("import", async () => {
    await flushPendingFields();
    const refreshed = await ipc.snapshot();
    revision.current = refreshed.revision;
    latestSnapshot.current = refreshed;
    onSnapshot(refreshed);
    const domains = selectedSections.filter((name) => refreshed.drafts[name]?.dirty);
    if (domains.length === 0) return { cancelled: true };
    const risks = domains.includes("claude") ? riskCodes(refreshed, "claude") : [];
    const diskConflicts = domains.filter((name) => refreshed.disk[name]?.changed);
    const unacknowledgedDiskConflicts = diskConflicts.filter((name) => !isEditableDiskDomain(name) || keptDiskGeneration[name] !== refreshed.disk[name]?.generation);
    if (unacknowledgedDiskConflicts.length > 0) {
      const accepted = await native.showConfirmation({ title: translate("settings.diskChangedTitle"), message: translate("settings.overwriteDiskConfirm"), confirmLabel: translate("settings.keepDraft") });
      if (!accepted) return { cancelled: true };
    }
    if (risks.length > 0) {
      const accepted = await native.showConfirmation({ title: translate("claude.confirmation.required"), message: translate("claude.confirmation.required"), confirmLabel: translate("screen.confirm") });
      if (!accepted) return { cancelled: true };
    }
    const confirmations = [...risks, ...diskConflicts.map((name) => `overwrite_external_${name}`)];
    const applied = await ipc.applyDomains(domains, refreshed.revision, confirmations.length > 0 ? confirmations : undefined);
    revision.current = applied.revision;
    if (!applied.domains?.includes("relay_accounts") && applied.status === "applied" && (domains.includes("providers_models") || domains.includes("runtime")) && (refreshed.service.state === "running" || refreshed.service.state === "unhealthy")) {
      // Provider Apply reloads the managed proxy in CoreStore. Runtime-only
      // changes still use the existing UI lifecycle operation.
      if (!domains.includes("providers_models")) {
        const reloaded = await ipc.dispatch({ type: "service.reload" }, applied.revision);
        revision.current = reloaded.revision;
      }
    }
    if (domains.includes("codex") || domains.includes("claude")) setSettingsRawBaselineToken((current) => current + 1);
    if (domains.includes("claude")) {
      claudeDeploymentDraftRef.current = undefined;
      setClaudeDeploymentDraft(undefined);
    }
    if (diskConflicts.length > 0) setKeptDiskGeneration({});
    return applied;
  }, "common.applied");
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
  const resizeDataManagement = useCallback((width: number, height: number): Promise<boolean> => native.window.setContentSize?.("data-management", width, height) ?? Promise.resolve(false), [native.window]);
  const exportDataManagement = (sections: ConfigDomain[]): Promise<void> => runDataManagement("export", async () => {
    const fileToken = await native.saveFilePicker({ suggestedName: "litellm-menu-data.json" });
    if (!fileToken) return { cancelled: true };
    return ipc.export(sections, fileToken);
  }, "dataManagement.exported");
  const probeWebDav = (): Promise<void> => runWebDavOperation(async () => {
    await flushPendingFields();
    return ipc.probe(undefined, undefined, "webdav");
  }, "webdav.probe");
  const applyWebDav = (): Promise<void> => runWebDavOperation(async () => {
    await flushPendingFields();
    const refreshed = await ipc.snapshot();
    revision.current = refreshed.revision;
    latestSnapshot.current = refreshed;
    onSnapshot(refreshed);
    if (!refreshed.drafts.webdav?.dirty) return { cancelled: true };
    const diskChanged = refreshed.disk.webdav?.changed === true;
    if (diskChanged) {
      const accepted = await native.showConfirmation({ title: translate("settings.diskChangedTitle"), message: translate("settings.overwriteDiskConfirm"), confirmLabel: translate("settings.keepDraft") });
      if (!accepted) return { cancelled: true };
    }
    const applied = await ipc.apply("webdav", refreshed.revision, diskChanged ? ["overwrite_external_webdav"] : undefined);
    revision.current = applied.revision;
    if (diskChanged) setKeptDiskGeneration((current) => ({ ...current, webdav: undefined }));
    return applied;
  }, "common.applied");
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
  }, "dataManagement.synced");
  const dispatchDataManagement: Dispatch = (type, payload = {}, targetDomain = "webdav") => runDataManagement("webdav", async () => enqueueDispatch(type, payload, targetDomain), null, true);
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
      const result = await ipc.apply("providers_models", staged.revision, confirmations);
      revision.current = result.revision;
      await refresh();
      setResult(translate("common.applied"));
      applied = true;
    });
    probedSurfaceApplyQueue.current = queued.then(() => undefined, () => undefined);
    return queued.then(() => applied).catch((reason: unknown) => {
      setResult(errorMessage(reason, translate));
      return false;
    });
  };
  const closeRoute = (): void => {
    // Keep the React route close independent from the native window registry.
    // A stale/missing native window must not strand the route on screen.
    if (settingsRoute) {
      claudeDeploymentDraftRef.current = undefined;
      setClaudeDeploymentDraft(undefined);
    }
    if (route === "data-management") importPlanToken.current = undefined;
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
  const requestClose = (): void => {
    // The provider wizard is a native modal child window. Its Cancel button
    // and title-bar close both dismiss that child directly; partial staged
    // provider edits remain in Core just as they did when the wizard was an
    // in-window surface.
    if (route === "provider-wizard") {
      closeRoute();
      return;
    }
    const restoreWindow = (): void => {
      const windowRoute = canonicalWindowRoute(route);
      native.window.open(windowRoute);
      native.window.focus(windowRoute);
    };
    const current = actionSnapshot;
    const dirtyDomains = stagedDomainsForRoute(current);
    const needsDiscardConfirmation = routeHasStagedChanges(current);
    if (!needsDiscardConfirmation) {
      closeRoute();
      return;
    }
    void (async () => {
      try {
        const confirmed = await native.showConfirmation({
          title: translate("menu.close"),
          message: translate("common.discarded"),
          confirmLabel: translate("menu.close"),
        });
        if (!confirmed) {
          restoreWindow();
          return;
        }
        discardPendingFields();
        if (settingsRoute) {
          claudeDeploymentDraftRef.current = undefined;
        }
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
    })();
  };
  useEffect(() => {
    if (nativeAction?.id !== `request-close-${route}` && nativeAction?.id !== `request-close-${canonicalWindowRoute(route)}`) return;
    requestClose();
  }, [nativeAction?.sequence]);
  const definition = ROUTES.find((item) => item.id === route);
  const windowTitle = settingsRoute
    ? translate(settingsTab === "claude" ? "card.claudeSettings" : "card.codexSettings")
    : translate(definition?.titleKey ?? "app.title");
  const providerWizardProviders = useMemo(() => {
    const state = domainState(snapshot, "providers_models");
    const details = asRecords(state.providers);
    const candidates = details.length > 0 ? details : (snapshot?.providers_models.providers ?? []).map(providerRecord);
    return candidates.filter((provider) => stringValue(provider.auth_kind, "api_key") === "api_key");
  }, [snapshot]);
  const providerWizardRelaySources = useMemo(() => relaySourcesFromSnapshot(snapshot), [snapshot]);
 const providerWizardRelayStations = useMemo(() => relayStationsFromSnapshot(snapshot), [snapshot]);
  const renderRelayManager = (options: { setupOnly: boolean; hideNavigation?: boolean; selectedNavigationKey?: string; onNavigationSelectionChange?: (key: string) => void }): React.JSX.Element => (
<RelayAccountManager visible setupOnly={options.setupOnly} hideNavigation={options.hideNavigation} selectedNavigationKey={options.selectedNavigationKey} onNavigationSelectionChange={options.onNavigationSelectionChange} snapshot={snapshot} native={native} busy={busy} translate={translate} onClose={closeRoute} onStatus={setResult} commit={commitRelayMetadata} detectType={async (origin) => {
      const staged = await enqueueDispatch("account.detect_type", { origin }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const detected = asRecord(next.action_summaries?.relay_accounts).detected_type;
      return detected === "newapi" || detected === "sub2api" ? detected : undefined;
    }} refreshResources={async (accountId) => {
      const staged = await enqueueDispatch("resources.refresh", { account_id: accountId }, "relay_accounts");
      revision.current = staged.revision;
      return asRecord(staged).resource_status === "ready" ? "ready" : "unavailable";
    }} apiKeyActions={{
      create: async (accountId, options) => {
        await commitRelayMetadata("api_key.create", {
          account_id: accountId,
          name: options.name,
          ...(options.groupID ? { group_id: options.groupID } : {}),
          enabled: options.enabled,
        }, "relay_accounts");
      },
      update: async (accountId, resourceId, name) => {
        await commitRelayMetadata("api_key.update", { account_id: accountId, resource_id: resourceId, name }, "relay_accounts");
      },
      setEnabled: async (accountId, resourceId, enabled) => {
        await commitRelayMetadata("api_key.set_enabled", { account_id: accountId, resource_id: resourceId, enabled }, "relay_accounts");
      },
      setGroup: async (accountId, resourceId, groupId) => {
        await commitRelayMetadata("api_key.set_group", { account_id: accountId, resource_id: resourceId, group_id: groupId }, "relay_accounts");
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
        await commitRelayMetadata("api_key.auto_group_align", { account_id: accountId }, "relay_accounts");
      },
      remove: async (accountId, resourceId, dependencyPolicy) => {
        await commitRelayMetadata("api_key.delete", {
          account_id: accountId,
          resource_id: resourceId,
          dependency_policy: dependencyPolicy,
        }, "relay_accounts");
      },
      detach: async (accountId, resourceId) => {
        await commitRelayMetadata("api_key.detach", { account_id: accountId, resource_id: resourceId }, "relay_accounts");
      },
    }} addAccount={async (type, origin, rememberPassword, stationOptions = {}) => {
      const before = await refresh();
      const beforeRelay = asRecord(before.domains.relay_accounts);
      const beforeRelayState = asRecord(beforeRelay.state);
      const beforeRelayAccounts = asRecords(beforeRelayState.accounts ?? beforeRelay.accounts);
      const existingIDs = new Set(beforeRelayAccounts.map((item) => stringValue(item.id)).filter(Boolean));
      const normalizedOrigin = normalizeRelayOrigin(origin);
      const originKey = stationOriginKey(normalizedOrigin);
      const staged = await enqueueDispatch("account.add", {
        type,
        label: origin,
        origin: normalizedOrigin,
        remember_password: rememberPassword,
        ...(stationOptions.stationID ? { station_id: stationOptions.stationID } : {}),
        ...(stationOptions.stationOrigin ? { station_origin: stationOptions.stationOrigin } : {}),
        ...(stationOptions.stationName ? { station_name: stationOptions.stationName } : {}),
        ...(stationOptions.stationType ? { station_type: stationOptions.stationType } : {}),
      }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const nextRelay = asRecord(next.domains.relay_accounts);
      const nextRelayState = asRecord(nextRelay.state);
      const accounts = asRecords(nextRelayState.accounts ?? nextRelay.accounts);
      const account = accounts.find((item) => {
        const id = stringValue(item.id);
        return Boolean(id && !existingIDs.has(id) && stationOriginKey(stringValue(item.origin)) === originKey && item.type === type);
      });
      if (!account) return undefined;
      const id = stringValue(account.id);
      const label = stringValue(account.label);
      if (!id || !label) return undefined;
      return {
        id,
        type,
        label,
        origin: stringValue(account.origin),
        username: stringValue(account.username),
        rememberPassword: account.remember_password === true,
      };
    }} refreshAccounts={async () => {
      await refresh();
    }} />
  );
  return <TranslationContext.Provider value={settingsRoute ? translate : undefined}><PendingFieldContext.Provider value={fieldRegistry}><View style={styles.windowSurface}>
    {route !== "providers-models" && route !== "logs" && route !== "relay-accounts" && route !== "relay-add" && route !== "provider-wizard" && route !== "data-management" ? <WindowTitle title={windowTitle} validation={issues.length > 0 ? `${issues.length} ${translate("common.validationIssues")}` : undefined} /> : null}
    {route === "providers-models" || route === "provider-wizard" || settingsRoute || route === "logs" || route === "relay-accounts" || route === "relay-add" || route === "runtime-settings" || route === "data-management" ? <View style={[styles.windowContent, compactStyles.windowContent, styles.windowContentFixed, route === "providers-models" && styles.providersContent, route === "provider-wizard" && styles.providerWizardRouteContent, settingsRoute && styles.settingsContent, route === "logs" && styles.logsContent, (route === "relay-accounts" || route === "relay-add") && styles.relayAccountsContent, route === "runtime-settings" && styles.runtimeContent, route === "data-management" && styles.dataManagementContent]}>
    {route === "providers-models" ? <ProviderWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onStatus={setResult} onSecretState={onSecretState} applyProbedSurface={applyProbedSurface} onOpenWizard={() => { if (Platform.OS === "windows") onNavigate("provider-wizard"); native.window.open("provider-wizard"); }} /> : null}
    {route === "provider-wizard" ? <ProviderSetupWizard providers={providerWizardProviders} relaySources={providerWizardRelaySources} relayStations={providerWizardRelayStations} busy={busy} translate={translate} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onStatus={setResult} onClose={closeRoute} /> : null}
    {settingsRoute ? <><View style={styles.settingsTabBar}><WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]} selected={settingsTab} disabled={busy} onSelect={(next) => switchSettingsTab(next as AssistantSettingsDomain)} style={styles.settingsTabs} /></View>{settingsTab === "codex" ? <CodexWorkspace snapshot={snapshot} ipc={ipc} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onEditorConflict={resolveRawEditorConflict} rawReloadToken={settingsRawReloadToken} rawBaselineToken={settingsRawBaselineToken} /> : <ClaudeScreen snapshot={snapshot} ipc={ipc} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onEditorConflict={resolveRawEditorConflict} deployment={claudeDeployment} onDeploymentChange={(key, value) => {
      const next = { ...(claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(snapshot)), [key]: value };
      claudeDeploymentDraftRef.current = next;
      setClaudeDeploymentDraft(next);
      return enqueueDispatch("patch_deployment", { [key]: value }, "claude").then(() => {
        setSettingsRawReloadToken((current) => current + 1);
      });
    }} rawReloadToken={settingsRawReloadToken} rawBaselineToken={settingsRawBaselineToken} />}</> : null}
    {route === "logs" ? <LogsWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} requestedTab={nativeAction?.id === "open-recovery" ? "recovery" : logTabRequest} requestedTabKey={nativeAction?.sequence ?? 0} /> : null}
    {route === "relay-accounts" ? <View style={serviceProviderStyles.workspace}>
      <View style={serviceProviderStyles.unifiedHeader}>
        <View style={serviceProviderStyles.intro}><Text style={serviceProviderStyles.heading}>{translate("route.relayAccounts")}</Text><Text style={serviceProviderStyles.hint}>{translate("relay.officialAccountsHint")}</Text></View>
        <NativeButton ref={serviceProviderAddButtonRef} title="" symbol="plus" compact toolTip={translate("relay.chooseProviderType")} accessibilityLabel={translate("relay.chooseProviderType")} primary disabled={busy} onPress={openServiceProviderAddMenu} />
      </View>
      <View style={serviceProviderStyles.columns}>
        <View style={serviceProviderStyles.listPane}>
          <NativeTable
            columns={[{ label: translate("common.name"), width: 118 }, { label: translate("providers.authentication"), width: 72 }, { label: translate("common.status"), width: 78 }]}
            rows={serviceProviderRows}
            selectedKey={serviceProviderSelection ?? ""}
            compact
            onSelectionChange={setServiceProviderSelection}
            style={serviceProviderStyles.table}
          />
        </View>
        <View style={serviceProviderStyles.detailPane}>
          {serviceProviderSelection?.startsWith("provider:") ? <ServiceProviderManager snapshot={snapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onActivateAndRestart={activateProviderAndRestart} onStatus={setResult} onSecretState={onSecretState} hideNavigation selectedNavigationKey={serviceProviderSelection} onNavigationSelectionChange={setServiceProviderSelection} /> : serviceProviderSelection?.startsWith("relay:") ? renderRelayManager({ setupOnly: false, hideNavigation: true, selectedNavigationKey: serviceProviderSelection, onNavigationSelectionChange: setServiceProviderSelection }) : <View style={serviceProviderStyles.emptyDetail}><Text style={serviceProviderStyles.detailTitle}>{translate("route.relayAccounts")}</Text><Text style={serviceProviderStyles.hint}>{translate("relay.empty")}</Text></View>}
        </View>
      </View>
    </View> : null}
    {route === "relay-add" ? <RelayAccountManager visible setupOnly snapshot={snapshot} native={native} busy={busy} translate={translate} onClose={closeRoute} onStatus={setResult} commit={commitRelayMetadata} detectType={async (origin) => {
      const staged = await enqueueDispatch("account.detect_type", { origin }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const detected = asRecord(next.action_summaries?.relay_accounts).detected_type;
      return detected === "newapi" || detected === "sub2api" ? detected : undefined;
    }} refreshResources={async (accountId) => {
      const staged = await enqueueDispatch("resources.refresh", { account_id: accountId }, "relay_accounts");
      revision.current = staged.revision;
      return asRecord(staged).resource_status === "ready" ? "ready" : "unavailable";
    }} apiKeyActions={{
      create: async (accountId, options) => {
        await commitRelayMetadata("api_key.create", {
          account_id: accountId,
          name: options.name,
          ...(options.groupID ? { group_id: options.groupID } : {}),
          enabled: options.enabled,
        }, "relay_accounts");
      },
      update: async (accountId, resourceId, name) => {
        await commitRelayMetadata("api_key.update", { account_id: accountId, resource_id: resourceId, name }, "relay_accounts");
      },
      setEnabled: async (accountId, resourceId, enabled) => {
        await commitRelayMetadata("api_key.set_enabled", { account_id: accountId, resource_id: resourceId, enabled }, "relay_accounts");
      },
      setGroup: async (accountId, resourceId, groupId) => {
        await commitRelayMetadata("api_key.set_group", { account_id: accountId, resource_id: resourceId, group_id: groupId }, "relay_accounts");
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
        await commitRelayMetadata("api_key.auto_group_align", { account_id: accountId }, "relay_accounts");
      },
      remove: async (accountId, resourceId, dependencyPolicy) => {
        await commitRelayMetadata("api_key.delete", {
          account_id: accountId,
          resource_id: resourceId,
          dependency_policy: dependencyPolicy,
        }, "relay_accounts");
      },
      detach: async (accountId, resourceId) => {
        await commitRelayMetadata("api_key.detach", { account_id: accountId, resource_id: resourceId }, "relay_accounts");
      },
    }} addAccount={async (type, origin, rememberPassword, stationOptions = {}) => {
      const before = await refresh();
      const beforeRelay = asRecord(before.domains.relay_accounts);
      const beforeRelayState = asRecord(beforeRelay.state);
      const beforeRelayAccounts = asRecords(beforeRelayState.accounts ?? beforeRelay.accounts);
      const existingIDs = new Set(beforeRelayAccounts.map((item) => stringValue(item.id)).filter(Boolean));
      const normalizedOrigin = normalizeRelayOrigin(origin);
      const originKey = stationOriginKey(normalizedOrigin);
      const staged = await enqueueDispatch("account.add", {
        type,
        label: origin,
        origin: normalizedOrigin,
        remember_password: rememberPassword,
        ...(stationOptions.stationID ? { station_id: stationOptions.stationID } : {}),
        ...(stationOptions.stationOrigin ? { station_origin: stationOptions.stationOrigin } : {}),
        ...(stationOptions.stationName ? { station_name: stationOptions.stationName } : {}),
        ...(stationOptions.stationType ? { station_type: stationOptions.stationType } : {}),
      }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const nextRelay = asRecord(next.domains.relay_accounts);
      const nextRelayState = asRecord(nextRelay.state);
      const accounts = asRecords(nextRelayState.accounts ?? nextRelay.accounts);
      const account = accounts.find((item) => {
        const id = stringValue(item.id);
        return Boolean(id && !existingIDs.has(id) && stationOriginKey(stringValue(item.origin)) === originKey && item.type === type);
      });
      if (!account) return undefined;
      const id = stringValue(account.id);
      const label = stringValue(account.label);
      if (!id || !label) return undefined;
      return {
        id,
        type,
        label,
        origin: stringValue(account.origin),
        username: stringValue(account.username),
        rememberPassword: account.remember_password === true,
      };
    }} refreshAccounts={async () => {
      await refresh();
    }} /> : null}
    {route === "runtime-settings" ? <RuntimeWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} /> : null}
    {route === "data-management" ? <DataManagementWorkspace snapshot={snapshot} busy={busy} webDavOperationBusy={webDavOperationBusy} statuses={dataManagementStatuses} hasPendingChanges={hasPendingFieldEdits()} translate={translate} dispatch={dispatchDataManagement} onSecretState={onSecretState} onResize={resizeDataManagement} onFlushPendingFields={flushPendingFields} onTabSwitchError={(tab, reason) => setDataManagementStatuses((current) => ({ ...current, [tab]: errorMessage(reason, translate) }))} onInspectImport={inspectImportDataManagement} onImport={importDataManagement} onConfirmImportReplace={confirmImportDraftReplacement} onExport={exportDataManagement} onApplyImported={applyDataManagement} onProbeWebDav={probeWebDav} onApplyWebDav={applyWebDav} onSyncWebDav={syncWebDav} /> : null}
    {issues.length > 0 ? <IssueList issues={issues} translate={translate} /> : null}
    </View> : null}
    {route === "relay-accounts" ? <DialogFooter compact borderless status={result}><ActionButton title={translate("menu.close")} onPress={requestClose} /><ActionButton primary title={translate("menu.apply")} disabled={busy || !routeHasStagedChanges(actionSnapshot)} onPress={apply} /></DialogFooter> : route !== "logs" && route !== "relay-add" && route !== "provider-wizard" && route !== "data-management" ? <DialogFooter status={result} leading={route === "runtime-settings" ? <ActionButton title={translate("common.restoreDefaults")} disabled={busy} style={styles.runtimeRestoreButton} onPress={() => dispatch("restore_defaults")} /> : undefined}><><ActionButton title={translate("menu.close")} disabled={busy} style={route === "runtime-settings" ? styles.wideButton : undefined} onPress={requestClose} /><ActionButton primary title={route === "runtime-settings" ? translate("common.saveAndApply") : translate("menu.apply")} disabled={busy || !routeHasStagedChanges(actionSnapshot)} style={route === "runtime-settings" ? styles.wideButton : undefined} onPress={apply} /></></DialogFooter> : null}
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

type ServiceProviderKind = "openai_login" | "claude_login";

function serviceProviderRecords(snapshot: CoreSnapshot | undefined): UnknownRecord[] {
  const state = domainState(snapshot, "providers_models");
  const details = asRecords(state.providers);
  const candidates = details.length > 0 ? details : (snapshot?.providers_models.providers ?? []).map(providerRecord);
  return candidates.filter((provider) => {
    const kind = providerAuthKind(provider);
    return kind === "openai_login" || kind === "claude_login";
  });
}

function serviceProviderKindLabel(kind: ServiceProviderKind, translate: Translate): string {
  return kind === "openai_login" ? translate("relay.officialProviderOpenAI") : translate("relay.officialProviderClaude");
}

function nextServiceProviderName(providers: UnknownRecord[], kind: ServiceProviderKind): string {
  const base = kind === "openai_login" ? "OpenAI" : "Claude";
  const names = new Set(providers.map((provider) => stringValue(provider.display_name, stringValue(provider.name)).trim().toLocaleLowerCase()).filter(Boolean));
  if (!names.has(base.toLocaleLowerCase())) return base;
  let suffix = 2;
  while (names.has(`${base} ${suffix}`.toLocaleLowerCase())) suffix += 1;
  return `${base} ${suffix}`;
}

function ServiceProviderManager({ snapshot, native, busy, translate, dispatch, dispatchWithOutcome, onActivateAndRestart, onStatus, onSecretState, hideNavigation = false, selectedNavigationKey, onNavigationSelectionChange }: {
  snapshot?: CoreSnapshot;
  native: NativeLeafAdapter;
  busy: boolean;
  translate: Translate;
  dispatch: Dispatch;
  dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>;
  onActivateAndRestart: () => Promise<boolean>;
  onStatus: (status?: string) => void;
  onSecretState: (state: SecretState) => void;
  hideNavigation?: boolean;
  selectedNavigationKey?: string;
  onNavigationSelectionChange?: (key: string) => void;
}): React.JSX.Element {
  const providers = serviceProviderRecords(snapshot);
  const [localSelectedProviderID, setLocalSelectedProviderID] = useState<string>();
  const selectedProviderID = hideNavigation && selectedNavigationKey?.startsWith("provider:")
    ? selectedNavigationKey.slice("provider:".length)
    : localSelectedProviderID;
  const selected = providers.find((provider) => editorIdentifier(provider) === selectedProviderID) ?? (selectedProviderID ? undefined : providers[0]);
  const selectedKind: ServiceProviderKind = selected && (providerAuthKind(selected) === "openai_login" || providerAuthKind(selected) === "claude_login")
    ? providerAuthKind(selected) as ServiceProviderKind
    : "openai_login";
  const status = providerAuthStatus(selected);
  const active = booleanValue(selected?.auth_active);
  const statusLabels: Record<ProviderAuthStatus, string> = {
    signed_out: translate("providers.authStatusSignedOut"),
    authorizing: translate("providers.authStatusAuthorizing"),
    signed_in: translate("providers.authStatusSignedIn"),
    expired: translate("providers.authStatusExpired"),
    error: translate("providers.authStatusError"),
    unsupported: translate("providers.authStatusUnsupported"),
  };
  const displayName = selected
    ? stringValue(selected.display_name, stringValue(selected.name, selectedKind === "openai_login" ? "OpenAI" : "Claude"))
    : selectedKind === "openai_login" ? translate("relay.officialProviderOpenAI") : translate("relay.officialProviderClaude");
  const model = selected ? asRecords(selected.models)[0] : undefined;
  const modelName = model ? stringValue(model.display_name, stringValue(model.name, stringValue(model.upstream_model, translate("common.notAvailable")))) : translate("common.notAvailable");
  const providerID = selected ? editorIdentifier(selected) : "";
  const shownChallenge = useRef<Record<string, string>>({});

  useEffect(() => {
    if (hideNavigation) return;
    if (selectedProviderID && !providers.some((provider) => editorIdentifier(provider) === selectedProviderID)) {
      setLocalSelectedProviderID(providers[0] ? editorIdentifier(providers[0]) : undefined);
    } else if (!selectedProviderID && providers.length > 0) {
      setLocalSelectedProviderID(editorIdentifier(providers[0]));
    }
  }, [hideNavigation, providers, selectedProviderID]);

  const presentAuthChallenge = (next: CoreSnapshot | undefined, kind: ServiceProviderKind, label: string, accountFingerprint: string): void => {
    const summary = asRecord(asRecord(next?.action_summaries?.providers_models).operation_summary);
    const verificationURL = stringValue(summary.verification_uri);
    const userCode = stringValue(summary.user_code);
    if (!verificationURL || !userCode) return;
    const fingerprint = `${accountFingerprint}|${kind}|${verificationURL}|${userCode}`;
    if (shownChallenge.current[accountFingerprint] === fingerprint) return;
    shownChallenge.current[accountFingerprint] = fingerprint;
    const options = {
      title: label + " " + translate("relay.officialProviderLogin"),
      closeLabel: translate("menu.close"),
    };
    if (native.showProviderAuth) {
      void native.showProviderAuth({
        provider: kind === "openai_login" ? "openai" : "claude",
        fingerprint: accountFingerprint,
        verificationURL,
        userCode,
        ...options,
      }).catch(() => undefined);
    } else {
      void native.showReadOnlyText({
        ...options,
        text: [verificationURL, userCode].join("\n"),
        language: "text",
        html: CODE_EDITOR_HTML,
      });
    }
  };

  useEffect(() => {
    const authorizing = providers.filter((provider) => providerAuthStatus(provider) === "authorizing");
    if (authorizing.length === 0) return;
    const timer = setInterval(() => {
      for (const provider of authorizing) {
        const kind = providerAuthKind(provider);
        if (kind !== "openai_login" && kind !== "claude_login") continue;
        const label = stringValue(provider.display_name, stringValue(provider.name, kind === "openai_login" ? translate("relay.officialProviderOpenAI") : translate("relay.officialProviderClaude")));
        const accountFingerprint = editorIdentifier(provider);
        void dispatchWithOutcome("service_provider.auth_status", { provider_id: accountFingerprint }, "providers_models", true)
          .then((next) => presentAuthChallenge(next, kind, label, accountFingerprint))
          .catch(() => undefined);
      }
    }, 1_000);
    return () => clearInterval(timer);
  }, [dispatchWithOutcome, providers, translate]);

  const startLogin = async (targetProviderID = providerID): Promise<void> => {
    if (!targetProviderID) return;
    const target = serviceProviderRecords(snapshot).find((provider) => editorIdentifier(provider) === targetProviderID);
    const targetKind = target && providerAuthKind(target) === "claude_login" ? "claude_login" : "openai_login";
    const targetLabel = target ? stringValue(target.display_name, stringValue(target.name, serviceProviderKindLabel(targetKind, translate))) : serviceProviderKindLabel(targetKind, translate);
    delete shownChallenge.current[targetProviderID];
    const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: targetProviderID }, "providers_models");
    presentAuthChallenge(next, targetKind, targetLabel, targetProviderID);
  };

  const deleteProvider = async (): Promise<void> => {
    if (!providerID) return;
    const confirmed = await native.showConfirmation({
      title: translate("relay.officialProviderDelete"),
      message: displayName,
      confirmLabel: translate("common.delete"),
    });
    if (!confirmed) return;
    await dispatchWithOutcome("service_provider.delete", { provider_id: providerID }, "providers_models");
    if (hideNavigation) onNavigationSelectionChange?.("");
    onStatus(undefined);
  };
 const activateProvider = async (): Promise<void> => {
   if (!providerID || selectedKind !== "openai_login") return;
    const activated = await dispatchWithOutcome("service_provider.auth_activate", { provider_id: providerID }, "providers_models");
    if (!activated) return;
    try {
      if (await onActivateAndRestart()) onStatus(translate("relay.officialProviderActive"));
    } catch (reason) {
      onStatus(errorMessage(reason, translate));
    }
  };

  const authAction = status === "signed_in"
    ? "service_provider.auth_logout"
    : status === "authorizing"
      ? "service_provider.auth_cancel"
      : "service_provider.auth_start";
  const authLabel = status === "signed_in"
    ? translate("relay.officialProviderLogout")
    : status === "authorizing"
      ? translate("relay.officialProviderCancel")
      : translate("relay.officialProviderLogin");
  const rows = providers.map((provider) => {
    const kind = providerAuthKind(provider);
    const loginKind = kind === "claude_login" ? "claude_login" : "openai_login";
    const label = stringValue(provider.display_name, stringValue(provider.name, serviceProviderKindLabel(loginKind, translate)));
    return {
      key: editorIdentifier(provider),
      cells: [label, serviceProviderKindLabel(loginKind, translate), statusLabels[providerAuthStatus(provider)]],
    };
  });

  return <View style={serviceProviderStyles.pane}>
      <View style={serviceProviderStyles.intro}><Text style={serviceProviderStyles.heading}>{translate("relay.officialAccounts")}</Text><Text style={serviceProviderStyles.hint}>{translate("relay.officialAccountsHint")}</Text></View>
      <View style={serviceProviderStyles.columns}>
        {!hideNavigation ? <View style={serviceProviderStyles.listPane}>
          <NativeTable columns={[{ label: translate("providers.provider"), width: 150 }, { label: translate("providers.authentication"), width: 92 }, { label: translate("relay.officialProviderStatus"), width: 118 }]} rows={rows} selectedKey={providerID} compact onSelectionChange={(key) => setLocalSelectedProviderID(key)} style={serviceProviderStyles.table} />
        </View> : null}
        <View style={serviceProviderStyles.detailPane}>
          <View style={serviceProviderStyles.detailHeader}><Text style={serviceProviderStyles.detailTitle}>{displayName}</Text><Text style={serviceProviderStyles.status}>{statusLabels[status]}</Text></View>
          <View style={serviceProviderStyles.rule} />
          <Text style={serviceProviderStyles.detailLine}>{translate("relay.officialProviderModels")}: {modelName}</Text>
          <Text style={serviceProviderStyles.hint}>{selectedKind === "openai_login" ? translate("relay.officialProviderWebViewHint") : translate("relay.officialProviderCliHint")}</Text>
          {selectedKind === "claude_login" ? <Text style={serviceProviderStyles.hint}>{translate("relay.officialProviderTokenHint")}</Text> : null}
          {selectedKind === "openai_login" && active ? <Text style={serviceProviderStyles.activeHint}>{translate("relay.officialProviderActive")}</Text> : null}
          {selectedKind === "openai_login" && selected && status === "signed_in" && !active ? <Text style={serviceProviderStyles.hint}>{translate("relay.officialProviderRestartHint")}</Text> : null}
          <View style={serviceProviderStyles.actions}>
            {selected ? <>
              {selectedKind === "openai_login" && status === "signed_in" && !active ? <NativeButton title={translate("relay.officialProviderActivate")} compact disabled={busy} onPress={() => { void activateProvider(); }} /> : null}
              <NativeButton title={authLabel} primary compact disabled={busy} onPress={() => { if (authAction === "service_provider.auth_start") void startLogin(); else void dispatch(authAction, { provider_id: providerID }, "providers_models"); }} />
              <NativeButton title={translate("relay.officialProviderDelete")} compact disabled={busy || status === "authorizing"} onPress={() => { void deleteProvider(); }} />
            </> : null}
          </View>
          {selectedKind === "claude_login" && selected && (status === "unsupported" || status === "error") ? <NativeSecretField autoCommit label={translate("providers.authTypeClaude")} hint={translate("relay.officialProviderTokenHint")} busy={busy} disabled={busy} domain="providers_models" field="provider_auth_token" target={providerID} onSecretState={onSecretState} /> : null}
        </View>
      </View>
    </View>;
}

function ProviderSetupWizard({ providers, relaySources, relayStations, busy, translate, dispatchWithOutcome, onSecretState, onStatus, onClose }: { providers: UnknownRecord[]; relaySources: RelaySourceOption[]; relayStations: RelayStationOption[]; busy: boolean; translate: Translate; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>; onSecretState: (state: SecretState) => void; onStatus: (status?: string) => void; onClose: () => void }): React.JSX.Element {
  type WizardStep = "provider" | "apiKey" | "model";
  const [step, setStep] = useState<WizardStep>("provider");
  const [providerMode, setProviderMode] = useState<"new" | "existing">("new");
  const [providerSelection, setProviderSelection] = useState(PROVIDER_WIZARD_NEW_PROVIDER);
  const [providerName, setProviderName] = useState("");
  const [providerBaseURL, setProviderBaseURL] = useState("");
  const [keySelection, setKeySelection] = useState("");
  const [keyName, setKeyName] = useState("");
  const [keyReady, setKeyReady] = useState(false);
  const [modelName, setModelName] = useState("");
  const [upstreamModel, setUpstreamModel] = useState("");
  const [fetchedModelCandidates, setFetchedModelCandidates] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [manualModels, setManualModels] = useState<Array<{ id: string; name: string; upstream_model: string }>>([]);
  const [selectedManualModelIDs, setSelectedManualModelIDs] = useState<string[]>([]);
  const [modelFetchState, setModelFetchState] = useState<"idle" | "loading" | "ready" | "empty" | "unavailable">("idle");
  const modelFetchRequest = useRef(0);
  const manualModelID = useRef(0);
  const [processing, setProcessing] = useState(false);
  const [validation, setValidation] = useState("");
  // Authentication is intentionally not selectable in this surface anymore.
  // Official account login lives in Service Provider Management; this wizard
  // creates and edits API-key providers only.
  const activeAuthKind: ProviderAuthKind = "api_key";
  const isNewProvider = providerMode === "new";
  const selectedProvider = providers.find((entry) => editorIdentifier(entry) === providerSelection);
  const providerID = selectedProvider ? editorIdentifier(selectedProvider) : "";
  const selectedProviderName = selectedProvider
    ? stringValue(selectedProvider.display_name, stringValue(selectedProvider.name, providerID))
    : providerName.trim();
  const activeProviderBaseURL = selectedProvider
    ? stringValue(selectedProvider.endpoint, stringValue(selectedProvider.api_base))
    : providerBaseURL;
  const keyChoices = selectedProvider ? providerKeyChoices(selectedProvider, relaySources, activeProviderBaseURL) : [];
  const keyOptions = [
    { value: PROVIDER_WIZARD_NEW_KEY, label: translate("providers.wizard.addApiKey") },
    ...keyChoices.map((choice) => ({ value: choice.id, label: providerKeyChoiceLabel(choice, translate) })),
  ];
  const activeKeySelection = keySelection || keyChoices[0]?.id || PROVIDER_WIZARD_NEW_KEY;
  const selectedKeyChoice = keyChoices.find((choice) => choice.id === activeKeySelection);
  const selectedKeyName = selectedKeyChoice?.name ?? (activeKeySelection === PROVIDER_WIZARD_NEW_KEY ? keyName.trim() : "");
  const selectedKeyReady = Boolean(selectedKeyChoice && (selectedKeyChoice.kind === "relay" || selectedKeyChoice.state?.configured)) || keyReady;
  const modelCandidates = useMemo(() => {
    const values = [...(selectedKeyChoice?.source?.models ?? []), ...fetchedModelCandidates];
    return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
  }, [fetchedModelCandidates, selectedKeyChoice?.source?.models]);
  const providerOptions = providers.map((entry) => {
      const id = editorIdentifier(entry);
      return { value: id, label: stringValue(entry.display_name, stringValue(entry.name, id)) };
    });

  useEffect(() => {
    if (!selectedProvider || processing || keySelection === PROVIDER_WIZARD_NEW_KEY || keyChoices.some((choice) => choice.id === keySelection)) return;
    setKeySelection(keyChoices[0]?.id ?? PROVIDER_WIZARD_NEW_KEY);
    setKeyReady(false);
  }, [keyChoices, keySelection, processing, selectedProvider]);

  useEffect(() => {
    modelFetchRequest.current += 1;
    setModelName("");
    setUpstreamModel("");
    setFetchedModelCandidates([]);
    setSelectedModels([]);
    setManualModels([]);
    setSelectedManualModelIDs([]);
    setModelFetchState("idle");
  }, [activeKeySelection, providerID]);

  const chooseExistingProvider = (value: string): void => {
    const nextProvider = providers.find((entry) => editorIdentifier(entry) === value);
    setProviderMode("existing");
    setProviderSelection(value);
    setKeySelection("");
    setKeyName("");
    setKeyReady(false);
    setValidation("");
  };
  const chooseProviderMode = (value: "new" | "existing"): void => {
    const nextProvider = value === "existing" ? providers[0] : undefined;
    setProviderMode(value);
    setProviderSelection(value === "new" ? PROVIDER_WIZARD_NEW_PROVIDER : editorIdentifier(nextProvider ?? {}));
    setKeySelection("");
    setKeyName("");
    setKeyReady(false);
    setValidation("");
  };
  const updateProviderBaseURL = (value: string): void => {
    setProviderBaseURL(value);
    if (!providerName.trim()) setProviderName(suggestedRelayStationName(value));
  };
  const updateProviderName = (value: string): void => {
    setProviderName(value);
  };
  const addManualModel = (): void => {
    const name = modelName.trim();
    const upstream = upstreamModel.trim();
    if (!name || !upstream) {
      setValidation(translate("providers.wizard.required"));
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
    if (activeAuthKind !== "api_key") return;
    const keyChoice = selectedKeyChoice;
    const keyName = selectedKeyName.trim();
    if (!providerID || !keyName) return;
    const request = ++modelFetchRequest.current;
    setModelFetchState("loading");
    const relaySource = keyChoice?.kind === "relay" ? keyChoice.source : undefined;
    const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";
    const payload = relaySource ? {
      provider_id: providerID,
      station_id: relaySource.stationID,
      account_id: relaySource.accountID,
      resource_id: relaySource.resourceID,
    } : {
      provider_id: providerID,
      api_key_name: keyName,
    };
    try {
      const next = await dispatchWithOutcome(action, payload);
      if (request !== modelFetchRequest.current) return;
      const summary = asRecord(asRecord(next?.action_summaries?.providers_models).operation_summary);
      const summaryProviderID = stringValue(summary.provider_id);
      const providerIdentity = selectedProvider ? identifier(selectedProvider) : "";
      if (stringValue(summary.operation) !== "fetch_models"
        || (summaryProviderID !== providerID && summaryProviderID !== providerIdentity)) {
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
      const available = new Set([...(selectedKeyChoice?.source?.models ?? []), ...candidates]);
      setSelectedModels((current) => current.filter((model) => available.has(model)));
      setModelFetchState("ready");
    } catch {
      if (request === modelFetchRequest.current) {
        setModelFetchState("unavailable");
      }
    }
  };
  const createProvider = async (): Promise<boolean> => {
    const name = providerName.trim();
    const baseURL = providerBaseURL.trim();
    if (!name || (activeAuthKind === "api_key" && !baseURL)) {
      setValidation(translate("providers.wizard.required"));
      return false;
    }
    const existingIDs = new Set(providers.map(editorIdentifier));
    setProcessing(true);
    try {
      const next = await dispatchWithOutcome("provider.add", { provider: { name, api_base: baseURL, auth_kind: "api_key", enabled: true, models: [], create_default_api_key: true } });
      if (!next) return false;
      const nextState = domainState(next, "providers_models");
      const nextProviders = asRecords(nextState.providers).length > 0
        ? asRecords(nextState.providers)
        : (next.providers_models.providers ?? []).map(providerRecord);
      const added = nextProviders.find((entry) => !existingIDs.has(editorIdentifier(entry)))
        ?? nextProviders.find((entry) => stringValue(entry.name).trim() === name);
      if (!added) return false;
      const relayStation = relayStationForBaseUrl(baseURL, relayStations);
      if (relayStation) {
        const rebound = await dispatchWithOutcome("provider.select_relay_station", { provider_id: editorIdentifier(added), station_id: relayStation.id });
        if (!rebound) return false;
      }
      setProviderMode("existing");
      setProviderSelection(editorIdentifier(added));
      setValidation("");
      setStep(activeAuthKind === "api_key" ? "apiKey" : "model");
      return true;
    } finally {
      setProcessing(false);
    }
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
      const nextState = domainState(next, "providers_models");
      const nextProviders = asRecords(nextState.providers).length > 0
        ? asRecords(nextState.providers)
        : (next.providers_models.providers ?? []).map(providerRecord);
      const nextProvider = nextProviders.find((entry) => editorIdentifier(entry) === providerID);
      const addedKey = nextProvider ? providerKeyStates(nextProvider).find((entry) => entry.name === name) : undefined;
      setKeySelection(addedKey?.id ?? PROVIDER_WIZARD_NEW_KEY);
      setKeyReady(false);
      setValidation("");
      return true;
    } finally {
      setProcessing(false);
    }
  };
  const goNext = async (): Promise<void> => {
    setValidation("");
    if (step === "provider") {
      if (isNewProvider) {
        await createProvider();
      } else if (providerID) {
        setStep(activeAuthKind === "api_key" ? "apiKey" : "model");
      } else {
        setValidation(translate("providers.wizard.required"));
      }
      return;
    }
    if (step === "apiKey") {
      if (!selectedProvider) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      if (activeKeySelection === PROVIDER_WIZARD_NEW_KEY) {
        if (await createKey()) return;
        return;
      }
      if (!selectedKeyChoice || !selectedKeyReady) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      setStep("model");
      if (modelCandidates.length === 0) void fetchWizardModels();
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
    const requestedModels = [
      ...modelCandidates
        .filter((name) => selectedModels.includes(name))
        .map((name) => ({ name, upstream_model: name })),
      ...manualModels
        .filter((model) => selectedManualModelIDs.includes(model.id))
        .map(({ name, upstream_model }) => ({ name, upstream_model })),
      ...(draftModel ? [draftModel] : []),
    ];
    const uniqueRequestedModels = requestedModels.filter((model, index, all) => all.findIndex((candidate) => candidate.name === model.name && candidate.upstream_model === model.upstream_model) === index);
    if (!providerID || (activeAuthKind === "api_key" && !selectedKeyName) || uniqueRequestedModels.length === 0) {
      setValidation(translate("providers.wizard.selectAtLeastOneModel"));
      return;
    }
    setProcessing(true);
    try {
      const existingModelIDs = selectedProvider ? new Set(asRecords(selectedProvider.models).map(modelRecord).map(editorIdentifier)) : new Set<string>();
      const transientRelaySource = activeAuthKind === "api_key" && selectedKeyChoice?.kind === "relay" && !selectedKeyChoice.state ? selectedKeyChoice.source : undefined;
      const modelPayload = uniqueRequestedModels.map((model, index) => ({
        ...model,
        api_key_name: selectedKeyName,
        ...(selectedKeyChoice?.id && !transientRelaySource ? { provider_key_id: selectedKeyChoice.id } : {}),
        enabled: true,
        order: index + 1,
      }));
      const next = await dispatchWithOutcome("model.add_many", { provider_id: providerID, models: modelPayload });
      if (!next) return;
      if (transientRelaySource) {
        const nextState = domainState(next, "providers_models");
        const nextProviders = asRecords(nextState.providers).length > 0
          ? asRecords(nextState.providers)
          : (next.providers_models.providers ?? []).map(providerRecord);
        const nextProvider = nextProviders.find((entry) => editorIdentifier(entry) === providerID);
        const addedModels = nextProvider ? asRecords(nextProvider.models).map(modelRecord) : [];
        for (const requested of uniqueRequestedModels) {
          const addedModel = addedModels.find((entry) => !existingModelIDs.has(editorIdentifier(entry)) && stringValue(entry.name).trim() === requested.name);
          if (!addedModel) return;
          const relayed = await dispatchWithOutcome("model.select_relay_resource", {
            provider_id: providerID,
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
    if (step === "model") setStep(activeAuthKind === "api_key" ? "apiKey" : "provider");
    else if (step === "apiKey") setStep("provider");
  };
  const stepItems: Array<{ id: WizardStep; title: string }> = [
    { id: "provider", title: translate("providers.wizard.stepProvider") },
    ...(activeAuthKind === "api_key" ? [{ id: "apiKey" as const, title: translate("providers.wizard.stepApiKey") }] : []),
    { id: "model", title: translate("providers.wizard.stepModel") },
  ];
  const providerPickerLabels = providerOptions.map((option) => option.label);
  const selectedProviderPickerLabel = providerOptions.find((option) => option.value === providerSelection)?.label ?? providerPickerLabels[0] ?? "";
  const keyPickerLabels = keyOptions.map((option) => option.label);
  const selectedKeyPickerLabel = keyOptions.find((option) => option.value === activeKeySelection)?.label ?? keyPickerLabels[0] ?? "";
  return <View style={styles.providerWizardSurface} accessibilityViewIsModal>
    <View style={styles.providerWizardSetupContent}>
      <View style={[styles.providerWizardSetupSurface, step === "model" && styles.providerWizardSetupSurfaceModel]}>
        <NativeWizardProgress steps={stepItems.map((item) => item.title)} activeIndex={stepItems.findIndex((item) => item.id === step)} />
        <View style={styles.providerWizardHeader}>
          <Text style={styles.providerWizardTitle}>{translate("providers.wizard.title")}</Text>
          <Text style={styles.providerWizardDescription}>{translate("providers.wizard.description")}</Text>
        </View>
        {step === "provider" ? <View style={styles.providerWizardFormSection}>
          <NativeFormRow label={translate("providers.wizard.provider")}>
            <NativeSegmentedControl labels={providers.length > 0 ? [translate("providers.wizard.addProvider"), translate("providers.wizard.selectProvider")] : [translate("providers.wizard.addProvider")]} selectedValue={isNewProvider ? translate("providers.wizard.addProvider") : translate("providers.wizard.selectProvider")} disabled={busy || processing} onChange={({ nativeEvent }) => { chooseProviderMode(providers.length > 0 && nativeEvent.index === 1 ? "existing" : "new"); }} style={styles.providerWizardModeControl} />
          </NativeFormRow>
          {!isNewProvider ? <NativeFormRow label={translate("providers.wizard.selectProvider")}><NativePicker labels={providerPickerLabels} selectedValue={selectedProviderPickerLabel} disabled={busy || processing} onChange={({ nativeEvent }) => { const option = providerOptions[nativeEvent.index]; if (option) chooseExistingProvider(option.value); }} style={styles.providerWizardPicker} /></NativeFormRow> : null}
          {isNewProvider ? <>
            <NativeFormRow label={translate("providers.wizard.baseUrl")}><NativeTextField value={providerBaseURL} placeholder={translate("providers.wizard.baseUrlPlaceholder")} editable={!busy && !processing} autoCapitalize="none" autoCorrect={false} onChangeText={updateProviderBaseURL} accessibilityLabel={translate("providers.wizard.baseUrl")} style={styles.providerWizardInput} /></NativeFormRow>
            <NativeFormRow label={translate("providers.wizard.providerName")}><NativeTextField value={providerName} placeholder={translate("providers.wizard.providerNamePlaceholder")} editable={!busy && !processing} autoCapitalize="none" autoCorrect={false} onChangeText={updateProviderName} accessibilityLabel={translate("providers.wizard.providerName")} style={styles.providerWizardInput} /></NativeFormRow>
          </> : <Text numberOfLines={1} style={styles.providerWizardHint}>{activeProviderBaseURL || translate("common.notAvailable")}</Text>}
        </View> : null}
        {step === "apiKey" && activeAuthKind === "api_key" ? <View style={styles.providerWizardFormSection}>
          <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.apiKey")}</Text><Text style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
          <NativeFormRow label={translate("providers.wizard.selectApiKey")}><NativePicker labels={keyPickerLabels} selectedValue={selectedKeyPickerLabel} disabled={busy || processing} onChange={({ nativeEvent }) => { const option = keyOptions[nativeEvent.index]; if (!option) return; setKeySelection(option.value); setKeyReady(false); setKeyName(""); setValidation(""); }} style={styles.providerWizardPicker} /></NativeFormRow>
          {activeKeySelection === PROVIDER_WIZARD_NEW_KEY ? <NativeFormRow label={translate("providers.wizard.apiKeyName")}><NativeTextField value={keyName} placeholder={translate("providers.wizard.apiKeyNamePlaceholder")} editable={!busy && !processing} autoCapitalize="none" autoCorrect={false} onChangeText={setKeyName} accessibilityLabel={translate("providers.wizard.apiKeyName")} style={styles.providerWizardInput} /></NativeFormRow>
            : selectedKeyChoice?.kind === "relay" ? <Text style={styles.providerWizardHint}>{translate("relay.apiKeyPreviewHint")}</Text>
              : selectedKeyChoice && !selectedKeyReady ? <NativeFormRow label={translate("providers.wizard.apiKeyValue")}><NativeSecureTextInput label={translate("providers.wizard.apiKeyValue")} domain="providers_models" field="api_key" target={`${providerID}\x1f${selectedKeyChoice.name}`} plainText autoCommit disabled={busy || processing} onSecretState={(state) => { setKeyReady(state.present); onSecretState(state); }} style={styles.providerWizardSecretInput} /></NativeFormRow>
                : <Text style={styles.providerWizardHint}>{selectedKeyChoice?.name ?? translate("providers.wizard.selectApiKey")}</Text>}
        </View> : null}
        {step === "model" ? <ScrollView style={styles.providerWizardModelScroll} contentContainerStyle={styles.providerWizardModelScrollContent} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">
          <View style={styles.providerWizardFormSection}>
            <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.models")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
            {activeAuthKind === "api_key" ? <View style={styles.providerWizardModelToolbar}>
              <Text numberOfLines={2} style={styles.providerWizardHint}>{modelFetchState === "loading" ? translate("providers.wizard.fetchingModels") : modelCandidates.length > 0 ? translate("providers.wizard.modelsFound", { count: modelCandidates.length }) : modelFetchState === "unavailable" ? translate("providers.wizard.modelsUnavailable") : modelFetchState === "empty" ? translate("providers.wizard.modelsEmpty") : translate("providers.wizard.noModels")}</Text>
              <NativeButton title={translate("providers.wizard.refreshModels")} compact link disabled={busy || processing || modelFetchState === "loading" || !providerID || !selectedKeyName} onPress={() => { void fetchWizardModels(); }} />
            </View> : null}
            {modelCandidates.length > 0 ? <View style={styles.providerWizardModelGroup}>
              <View style={styles.providerWizardModelGroupHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.discoveredModels")}</Text><Text style={styles.providerWizardHint}>{translate("providers.wizard.selectedModels", { count: selectedModels.length })}</Text></View>
              <View style={styles.providerWizardModelList}>
                {modelCandidates.map((name) => <NativeCheckbox key={`discovered:${name}`} label={name} value={selectedModels.includes(name)} disabled={busy || processing} onValueChange={(checked) => { setSelectedModels((current) => checked ? (current.includes(name) ? current : [...current, name]) : current.filter((item) => item !== name)); setValidation(""); }} style={styles.providerWizardModelCheckbox} />)}
              </View>
            </View> : null}
            <View style={styles.providerWizardModelGroup}>
              <Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.manualModels")}</Text>
              {manualModels.map((model) => <View key={model.id} style={styles.providerWizardManualModelRow}>
                <NativeCheckbox label={model.name} value={selectedManualModelIDs.includes(model.id)} disabled={busy || processing} onValueChange={(checked) => { setSelectedManualModelIDs((current) => checked ? (current.includes(model.id) ? current : [...current, model.id]) : current.filter((item) => item !== model.id)); setValidation(""); }} style={styles.providerWizardManualModelCheckbox} />
                <Text numberOfLines={1} style={styles.providerWizardManualModelUpstream}>{model.upstream_model}</Text>
                <NativeButton title={translate("providers.wizard.removeManualModel")} compact link disabled={busy || processing} onPress={() => removeManualModel(model.id)} />
              </View>)}
              <NativeFormRow label={translate("providers.wizard.modelName")}><NativeTextField value={modelName} placeholder={translate("providers.wizard.modelNamePlaceholder")} editable={!busy && !processing} autoCapitalize="none" autoCorrect={false} onChangeText={setModelName} accessibilityLabel={translate("providers.wizard.modelName")} style={styles.providerWizardInput} /></NativeFormRow>
              <NativeFormRow label={translate("providers.wizard.upstreamModel")}><NativeTextField value={upstreamModel} placeholder={translate("providers.wizard.upstreamModelPlaceholder")} editable={!busy && !processing} autoCapitalize="none" autoCorrect={false} onChangeText={setUpstreamModel} accessibilityLabel={translate("providers.wizard.upstreamModel")} style={styles.providerWizardInput} /></NativeFormRow>
              <NativeButton title={translate("providers.wizard.addManualModel")} compact link disabled={busy || processing} onPress={addManualModel} />
            </View>
            <Text style={styles.providerWizardModelSummary}>{translate("providers.wizard.modelsToAdd", { count: selectedModels.length + selectedManualModelIDs.length + Number(Boolean(modelName.trim() && upstreamModel.trim())) })}</Text>
          </View>
        </ScrollView> : null}
        {validation ? <Text style={styles.providerWizardValidation}>{validation}</Text> : null}
      </View>
    </View>
    <View style={styles.providerWizardFooter}>
      {validation ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.providerWizardFooterStatus}>{validation}</Text> : <View style={styles.providerWizardFooterSpacer} />}
      <View style={styles.providerWizardFooterActions}>
        <NativeButton title={translate("menu.close")} disabled={processing} onPress={onClose} />
        {step !== "provider" ? <NativeButton title={translate("providers.wizard.back")} disabled={busy || processing} onPress={goBack} /> : null}
        <NativeButton primary title={processing ? translate("providers.wizard.creating") : step === "model" ? translate("providers.wizard.finish") : translate("providers.wizard.next")} disabled={busy || processing} onPress={() => { void goNext(); }} />
      </View>
    </View>
  </View>;
}

function ProviderWorkspace({ snapshot, ipc, onSnapshot, native, busy, translate, dispatch, dispatchWithOutcome, onStatus, onSecretState, applyProbedSurface, onOpenWizard }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<CoreSnapshot | undefined>; onStatus: (status?: string) => void; onSecretState: (state: SecretState) => void; applyProbedSurface: ApplyProbedSurface; onOpenWizard: () => void }): React.JSX.Element {
  const state = domainState(snapshot, "providers_models");
  const relaySources = useMemo(() => relaySourcesFromSnapshot(snapshot), [snapshot]);
  const relayStations = useMemo(() => relayStationsFromSnapshot(snapshot), [snapshot]);
  const providers = useMemo(() => {
    const details = asRecords(state.providers);
    const candidates = details.length > 0 ? details : (snapshot?.providers_models.providers ?? []).map(providerRecord);
    return candidates.filter((provider) => providerAuthKind(provider) === "api_key");
  }, [snapshot?.providers_models.providers, state.providers]);
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const [providerNameDrafts, setProviderNameDrafts] = useState<Record<string, string>>({});
  const [providerBaseUrlDrafts, setProviderBaseUrlDrafts] = useState<Record<string, string>>({});
  const [modelNameDrafts, setModelNameDrafts] = useState<Record<string, string>>({});
  const [modelUpstreamDrafts, setModelUpstreamDrafts] = useState<Record<string, string>>({});
  const [modelOrderDrafts, setModelOrderDrafts] = useState<Record<string, string>>({});
  const [providerKeyNameDrafts, setProviderKeyNameDrafts] = useState<Record<string, string>>({});
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
  const setProviderKeyNameDraft = useCallback((providerID: string, keyID: string, value: string): void => {
    const key = providerKeyDraftKey(providerID, keyID);
    setProviderKeyNameDrafts((current) => current[key] === value ? current : { ...current, [key]: value });
  }, []);
  const providerBaseURL = useCallback((entry: UnknownRecord): string => {
    const entryID = editorIdentifier(entry);
    return providerBaseUrlDrafts[entryID] !== undefined
      ? providerBaseUrlDrafts[entryID]
      : stringValue(entry.endpoint, stringValue(entry.api_base));
  }, [providerBaseUrlDrafts]);
  const autoRelaySelectionKeys = useRef(new Set<string>());
  useEffect(() => {
    const activeKeys = new Set<string>();
    if (!busy) {
      for (const entry of providers) {
        if (stringValue(entry.provider_type, "custom") === "relay") continue;
        const baseURL = stringValue(entry.endpoint, stringValue(entry.api_base)).trim();
        const station = relayStationForBaseUrl(baseURL, relayStations);
        if (!station) continue;
        const providerID = editorIdentifier(entry);
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
  const providerKeyDisplayName = useCallback((entryProviderID: string, keyID: string, fallback: string): string => {
    const draft = providerKeyNameDrafts[providerKeyDraftKey(entryProviderID, keyID)];
    return draft !== undefined
      ? draft
      : fallback;
  }, [providerKeyNameDrafts]);
  const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);
  const provider = useMemo(
    () => providers.find((item) => editorIdentifier(item) === selectedProvider) ?? providers[0],
    [providers, selectedProvider],
  );
  const providerId = provider ? editorIdentifier(provider) : "";
  const models = useMemo(
    () => provider ? asRecords(provider.models).map(modelRecord) : [],
    [provider],
  );
  const [selectedModel, setSelectedModel] = useState<string>();
  const [providerSourceModel, setProviderSourceModel] = useState<string>();
  const model = useMemo(
    () => models.find((item) => editorIdentifier(item) === selectedModel),
    [models, selectedModel],
  );
  const [viewMode, setViewMode] = useState<"providers" | "routes">("providers");
  const [selectedRoute, setSelectedRoute] = useState<string>();
  const [fetchKeyID, setFetchKeyID] = useState<string>();
  const probingModelKeys = useRef(new Set<string>());
  const [, setProbeActivityRevision] = useState(0);
  const [probeResults, setProbeResults] = useState<Record<string, IpcResults["probe"]>>({});
  const fetchKeyChoices = useMemo(
    () => provider ? providerKeyChoices(provider, relaySources, providerBaseURL(provider)) : [],
    [provider, providerBaseURL, relaySources],
  );
  const fetchKeyOptions = useMemo(
    () => fetchKeyChoices.map((choice) => ({
      value: choice.id,
      label: providerKeyChoiceLabel({ ...choice, name: providerKeyDisplayName(providerId, choice.id, choice.name) }, translate),
    })),
    [fetchKeyChoices, providerId, providerKeyDisplayName, translate],
  );
  const selectedFetchKey = fetchKeyID ?? fetchKeyChoices[0]?.id ?? "";
  const selectedFetchLabel = fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? translate("common.default");
  async function probeModel(targetProviderId: string, targetModelId: string, options?: { confirmRecommendation?: boolean }): Promise<void> {
    const key = modelProbeKey(targetProviderId, targetModelId);
    if (probingModelKeys.current.has(key)) return;
    probingModelKeys.current.add(key);
    setProbeActivityRevision((value) => value + 1);
    try {
      const result = await ipc.probe(targetProviderId, targetModelId, "providers_models");
      setProbeResults((current) => ({ ...current, [key]: result }));
      onSnapshot(await ipc.snapshot());
      const nextSurface = stringValue(result.recommended_surface);
      if (result.ok && isProbeSurface(nextSurface)) await applyProbedSurface(targetProviderId, targetModelId, nextSurface, options);
    } catch (reason: unknown) {
      setProbeResults((current) => ({
        ...current,
        [key]: { ok: false, protocols: [], detail: errorMessage(reason, translate), provider_id: targetProviderId, model_id: targetModelId },
      }));
    } finally {
      probingModelKeys.current.delete(key);
      setProbeActivityRevision((value) => value + 1);
    }
  }
  const modelProbeProps = (targetProviderId: string, targetModelId: string): { probing: boolean; probeResult?: IpcResults["probe"] } => {
    const key = modelProbeKey(targetProviderId, targetModelId);
    return { probing: probingModelKeys.current.has(key), probeResult: probeResults[key] };
  };
  useEffect(() => {
    if (providers.length === 0) {
      if (selectedProvider !== undefined) setSelectedProvider(undefined);
      return;
    }
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
    if (!fetchKeyChoices.some((choice) => choice.id === fetchKeyID)) setFetchKeyID(fetchKeyChoices[0]?.id);
  }, [fetchKeyChoices, fetchKeyID, providerId]);
  const handleFetchedModels = (summary: UnknownRecord): void => {
    const summaryProviderId = stringValue(summary.provider_id);
    const providerIdentity = identifier(provider);
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
    const providerName = provider ? providerDisplayName(provider) : providerId;
    const apiKeyName = stringValue(summary.api_key_name);
    if (!apiKeyName) {
      onStatus(translate("providers.fetchFailed", { detail: translate("common.notAvailable") }));
      return;
    }
    const keyName = fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? apiKeyDisplayName(apiKeyName, translate);
    void native.chooseModelsToAdd({ models: candidates, providerName, keyName }).then((selection) => {
      const selectedModels = (selection ?? []).filter((model, index, all) => candidateSet.has(model) && all.indexOf(model) === index);
      if (selectedModels.length === 0) return;
      void dispatch("model.add_many", {
        provider_id: providerId,
        models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, api_key_name: apiKeyName, enabled: true, order: 0 })),
      });
    }).catch(() => undefined);
  };
  // The provider-list plus button follows the same auth-first wizard as the
  // dedicated “Add with wizard” action. This prevents an implicit API-key
  // provider from being created before the user chooses account login.
  const addProvider = (): void => {
    onOpenWizard();
  };
  const addModel = (): void => {
    if (!provider) return;
    const knownModelIds = new Set(models.map(editorIdentifier));
    pendingModelIds.current = { providerId, ids: knownModelIds };
    void dispatch("model.add", { provider_id: providerId, model: { name: "", upstream_model: "", enabled: true, order: 0 } });
  };
  const fetchModels = (): void => {
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
    void dispatchWithOutcome(action, payload).then((next) => {
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
    });
  };
  const duplicateModel = (): void => {
    if (!model) return;
    void dispatch("model.duplicate", { provider_id: providerId, model_id: editorIdentifier(model) });
  };
  const providerDisplayName = useCallback((entry: UnknownRecord): string => {
    const entryID = editorIdentifier(entry);
    return providerNameDrafts[entryID] !== undefined
      ? providerNameDrafts[entryID]
      : stringValue(entry.display_name, stringValue(entry.name, translate("providers.newProvider")));
  }, [providerNameDrafts, translate]);
  const modelDisplayName = useCallback((entryProviderID: string, entryModel: UnknownRecord): string => {
    const entryModelID = editorIdentifier(entryModel);
    const draft = modelNameDrafts[providerModelDraftKey(entryProviderID, entryModelID)];
    return draft !== undefined
      ? draft
      : stringValue(entryModel.display_name, stringValue(entryModel.name, translate("providers.newModel")));
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
    const keyByKey = new Map(providers.flatMap((entry) => providerKeyStates(entry).map((key) => [providerKeyDraftKey(editorIdentifier(entry), key.id), key] as const)));
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
    setProviderKeyNameDrafts((current) => pruneStringDrafts(current, (key, value) => keyByKey.get(key)?.name !== value));
  }, [modelNameDrafts, modelOrderDrafts, modelUpstreamDrafts, providerBaseUrlDrafts, providerKeyNameDrafts, providerNameDrafts, providers, translate]);
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
  const activeRoute = routes.find((entry) => entry.key === selectedRoute);
  const activeRouteGroup = activeRoute ? routes.filter((entry) => entry.publicModel === activeRoute.publicModel) : [];
  const activeRouteIndex = activeRoute ? activeRouteGroup.findIndex((entry) => entry.key === activeRoute.key) : -1;
  const activeRouteUsesMultiplier = Boolean(activeRoute && modelOrderMode(activeRoute.model) === "relay_multiplier");
  const canMoveRouteUp = !activeRouteUsesMultiplier && activeRouteIndex > 0;
  const canMoveRouteDown = !activeRouteUsesMultiplier && activeRouteIndex >= 0 && activeRouteIndex < activeRouteGroup.length - 1;
  useEffect(() => {
    if (viewMode !== "routes" || routes.length === 0 || routes.some((entry) => entry.key === selectedRoute)) return;
    setSelectedRoute(routes[0].key);
  }, [routes, selectedRoute, viewMode]);
  const moveRoute = (direction: "up" | "down"): void => {
    if (!activeRoute || activeRouteIndex < 0 || modelOrderMode(activeRoute.model) === "relay_multiplier") return;
    const targetIndex = direction === "up" ? activeRouteIndex - 1 : activeRouteIndex + 1;
    if (targetIndex < 0 || targetIndex >= activeRouteGroup.length) return;
    const reordered = [...activeRouteGroup];
    [reordered[activeRouteIndex], reordered[targetIndex]] = [reordered[targetIndex], reordered[activeRouteIndex]];
    void dispatch("routes.reorder_group", { public_model: activeRoute.publicModel, route_ids: reordered.map((entry) => entry.deploymentID) });
  };
  const confirmDeleteProvider = (): void => {
    if (!provider) return;
    const label = providerDisplayName(provider);
    void native.showConfirmation({ title: translate("providers.deleteProvider"), message: `${label} (${models.length} ${translate("providers.models")})`, confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("provider.delete", { provider_id: providerId }).then(() => { setSelectedProvider(undefined); setSelectedModel(undefined); setProviderSourceModel(undefined); }) : undefined);
  };
  const confirmDeleteModel = (): void => {
    if (!model) return;
    const modelId = editorIdentifier(model);
    void native.showConfirmation({ title: translate("providers.deleteModel"), message: modelDisplayName(providerId, model) || modelId, confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("model.delete", { provider_id: providerId, model_id: modelId }).then(() => setSelectedModel(undefined)) : undefined);
  };
  const providerRows = useMemo(
    () => providers.map((item) => ({ key: editorIdentifier(item), cells: [providerDisplayName(item), String(asRecords(item.models).length || numberValue(item.model_count))] })),
    [providerDisplayName, providers],
  );
  const disabledProviderKeys = useMemo(
    () => providers.filter((item) => !booleanValue(item.enabled, true)).map(editorIdentifier),
    [providers],
  );
  const modelRows = useMemo(
    () => models.map((item) => ({ key: editorIdentifier(item), cells: [modelDisplayName(providerId, item), modelUpstreamDisplay(providerId, item), modelProviderKeyLabel(item, provider ?? {}, translate, (keyID, name) => providerKeyDisplayName(providerId, keyID, name))] })),
    [modelDisplayName, modelProviderKeyLabel, modelUpstreamDisplay, models, provider, providerId, providerKeyDisplayName, translate],
  );
  const disabledModelKeys = useMemo(
    () => models.filter((item) => !booleanValue(provider?.enabled, true) || !booleanValue(item.model_enabled, booleanValue(item.enabled, true))).map(editorIdentifier),
    [models, provider?.enabled],
  );
  const routeRows = useMemo(() => {
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [];
    let previousPublicModel = "";
    for (const entry of routes) {
      if (entry.publicModel !== previousPublicModel) {
        rows.push({
          key: `route-public-model:${entry.publicModel}`,
          cells: [entry.publicModel, "", "", ""],
          spanning: true,
        });
        previousPublicModel = entry.publicModel;
      }
      const order = modelOrderText(editorIdentifier(entry.provider), entry.model);
      rows.push({
        key: entry.key,
        cells: [`\t${providerDisplayName(entry.provider)}`, modelProviderKeyLabel(entry.model, entry.provider, translate, (keyID, name) => providerKeyDisplayName(editorIdentifier(entry.provider), keyID, name)), order, modelUpstreamDisplay(editorIdentifier(entry.provider), entry.model) || translate("common.notAvailable")],
      });
    }
    return rows;
  }, [modelOrderText, modelUpstreamDisplay, providerDisplayName, providerKeyDisplayName, routes, translate]);
  const disabledRouteKeys = useMemo(
    () => routes.filter((entry) => !entry.providerEnabled || !entry.modelEnabled || !entry.keyAvailable).map((entry) => entry.key),
    [routes],
  );
  const selectRoute = useCallback((routeId: string): void => {
    const selected = routes.find((entry) => entry.key === routeId);
    if (!selected) return;
    setSelectedRoute(routeId);
    setSelectedProvider(editorIdentifier(selected.provider));
    setSelectedModel(editorIdentifier(selected.model));
    setProviderSourceModel(undefined);
  }, [routes]);
  const chooseViewMode = (value: "providers" | "routes"): void => {
    if (value === viewMode) return;
    const switchMode = async (): Promise<void> => {
      if (value === "routes") {
        const first = routes[0];
        if (first) selectRoute(first.key);
      }
      setViewMode(value);
    };
    void switchMode();
  };
  const providerDraftProjection = useMemo<ProviderWorkspaceDraftProjection>(() => ({
    providerDisplayName,
    modelDisplayName,
    providerBaseURL,
    modelUpstreamDisplay,
    modelOrderText,
    providerKeyDisplayName,
    setProviderNameDraft,
    setProviderBaseUrlDraft,
    setModelNameDraft,
    setModelUpstreamDraft,
    setModelOrderDraft,
    setProviderKeyNameDraft,
  }), [modelDisplayName, modelOrderText, modelUpstreamDisplay, providerBaseURL, providerDisplayName, providerKeyDisplayName]);
  return <ProviderWorkspaceDraftContext.Provider value={providerDraftProjection}><View style={styles.providersLayout}>
    <View style={styles.providerLeftColumn}>
      <View style={styles.providerToolbar}>
        <WindowTabs values={[{ id: "providers", title: translate("providers.providers") }, { id: "routes", title: translate("providers.routes") }]} selected={viewMode} onSelect={(value) => chooseViewMode(value as "providers" | "routes")} />
        <ActionButton title={translate("providers.addWizard")} disabled={busy} style={styles.providerWizardToolbarButton} onPress={onOpenWizard} />
        <View style={styles.toolbarSpacer} />
      </View>
      {viewMode === "routes" ? <View style={styles.routeWorkspace}>
        <TablePane wide style={styles.routeTablePane} title={translate("providers.routes")} actions={<><IconButton label="↑" title={translate("common.moveUp")} disabled={busy || !canMoveRouteUp} onPress={() => moveRoute("up")} /><IconButton label="↓" title={translate("common.moveDown")} disabled={busy || !canMoveRouteDown} onPress={() => moveRoute("down")} /></>}>
          <NativeTable columns={[{ label: translate("providers.provider"), width: 116 }, { label: translate("providers.providerKey"), width: 166 }, { label: translate("common.order"), width: 72 }, { label: translate("providers.upstream"), width: 136 }]} rows={routeRows} disabledRowKeys={disabledRouteKeys} selectedKey={selectedRoute ?? ""} compact onSelectionChange={selectRoute} style={styles.nativeRouteTable} />
        </TablePane>
      </View> : <View style={styles.providerWorkspace}>
        <View style={styles.providerModelColumns}>
          <TablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !provider} onPress={confirmDeleteProvider} /></>}>
            <NativeTable columns={[{ label: translate("providers.provider"), width: 88 }, { label: translate("providers.modelCount"), width: 64 }]} rows={providerRows} disabledRowKeys={disabledProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }} style={styles.nativeProviderTable} />
          </TablePane>
          <TablePane style={styles.modelListPane} title={translate("providers.models")} actions={<><IconButton label="+" title={translate("providers.newModel")} disabled={busy || !provider} onPress={addModel} /><IconButton label="⧉" title={translate("common.copy")} disabled={busy || !model} onPress={duplicateModel} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !model} onPress={confirmDeleteModel} /></>}>
            <NativeTable columns={[{ label: translate("providers.model"), width: 96 }, { label: translate("providers.upstream"), width: 112 }, { label: translate("providers.providerKey"), width: 128 }]} rows={modelRows} disabledRowKeys={disabledModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedModel(key); setProviderSourceModel(undefined); }} style={styles.nativeModelTable} />
            {provider && providerAuthKind(provider) === "api_key" ? <View style={styles.tableBottomRow}><NativePicker labels={fetchKeyOptions.length > 0 ? fetchKeyOptions.map((option) => option.label) : [translate("common.default")]} selectedValue={selectedFetchLabel} disabled={busy || fetchKeyChoices.length === 0} onChange={({ nativeEvent }) => { const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyID(option.value); }} style={styles.fetchKeyPicker} /><ActionButton title={translate("providers.fetch")} disabled={busy || !selectedFetchKey} onPress={fetchModels} /></View> : null}
          </TablePane>
        </View>
      </View>}
    </View>
    <View style={styles.providerInspector}>{viewMode === "routes" ? (activeRoute ? (providerSourceModel ? <ProviderEditor key={`provider:${editorIdentifier(activeRoute.provider)}`} provider={activeRoute.provider} relaySources={relaySources} relayStations={relayStations} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onNameDraftChange={(value) => setProviderNameDraft(editorIdentifier(activeRoute.provider), value)} sourceModel={activeRoute.model} onReturnToModel={() => { setProviderSourceModel(undefined); setSelectedModel(editorIdentifier(activeRoute.model)); }} /> : <ModelInspector key={`model:${editorIdentifier(activeRoute.provider)}:${editorIdentifier(activeRoute.model)}`} providers={providers} providerLabels={providers.map(providerDisplayName)} provider={activeRoute.provider} providerId={editorIdentifier(activeRoute.provider)} model={activeRoute.model} modelName={modelDisplayName(editorIdentifier(activeRoute.provider), activeRoute.model)} relaySources={relaySources} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} {...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} onNameDraftChange={(value) => setModelNameDraft(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model), value)} onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: editorIdentifier(activeRoute.provider), model_id: editorIdentifier(activeRoute.model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(activeRoute.model)); setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`); setProviderSourceModel(undefined); })} />) : <EmptyState translate={translate} />) : provider && model ? <ModelInspector key={`model:${providerId}:${editorIdentifier(model)}`} providers={providers} providerLabels={providers.map(providerDisplayName)} provider={provider} providerId={providerId} model={model} modelName={modelDisplayName(providerId, model)} relaySources={relaySources} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(providerId, editorIdentifier(model))} {...modelProbeProps(providerId, editorIdentifier(model))} onNameDraftChange={(value) => setModelNameDraft(providerId, editorIdentifier(model), value)} onProviderClick={() => { setProviderSourceModel(editorIdentifier(model)); setSelectedModel(undefined); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: providerId, model_id: editorIdentifier(model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(model)); setProviderSourceModel(undefined); })} /> : provider ? <ProviderEditor key={`provider:${providerId}`} provider={provider} relaySources={relaySources} relayStations={relayStations} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onNameDraftChange={(value) => setProviderNameDraft(providerId, value)} sourceModel={models.find((item) => editorIdentifier(item) === providerSourceModel)} onReturnToModel={() => { if (providerSourceModel) setSelectedModel(providerSourceModel); setProviderSourceModel(undefined); }} /> : <EmptyState translate={translate} />}</View>
  </View></ProviderWorkspaceDraftContext.Provider>;
}

function TablePane({ title, actions, wide, style, children }: { title: string; actions: React.ReactNode; wide?: boolean; style?: StyleProp<ViewStyle>; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.tablePane, compactStyles.tablePane, wide && styles.tablePaneWide, style]}><View style={[styles.tableTitleRow, compactStyles.tableTitleRow]}><Text style={styles.tableTitle}>{title}</Text><View style={[styles.tableActions, compactStyles.inlineGap]}>{actions}</View></View>{children}</View>;
}

function ModelInspector({ providers, providerLabels, provider, providerId, model, modelName, relaySources, native, busy, translate, dispatch, probe, probing, probeResult, onNameDraftChange, onProviderClick, onProviderChange }: { providers: UnknownRecord[]; providerLabels: string[]; provider: UnknownRecord; providerId: string; model: UnknownRecord; modelName: string; relaySources: RelaySourceOption[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; probe: () => void; probing: boolean; probeResult?: IpcResults["probe"]; onNameDraftChange?: (name: string) => void; onProviderClick: () => void; onProviderChange: (providerId: string) => void }): React.JSX.Element {
  const id = editorIdentifier(model);
  const providerIndex = providers.findIndex((item) => editorIdentifier(item) === providerId);
  const providerLabel = providerLabels[Math.max(0, providerIndex)] ?? "";
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const upstreamName = drafts?.modelUpstreamDisplay(providerId, model) ?? upstreamModelLabel(model);
  const upstreamFieldValue = upstreamName;
  const providerBaseUrl = drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base));
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
  const probePresentation = modelProbePresentation(model, probeResult, translate);
  const probeDetailHint = translate("providers.probeDetailsHint");
  const authenticationReady = providerAuthKind(provider) === "api_key"
    ? booleanValue(model.api_key_configured)
    : providerAuthStatus(provider) === "signed_in";
  const probeReady = Boolean(providerBaseUrl.trim() && upstreamName.trim() && authenticationReady);
  const openProbeDetails = (): void => {
    if (!probePresentation.full) return;
    void native.showReadOnlyText({
      title: translate("providers.probeDetails"),
      text: probePresentation.full,
      closeLabel: translate("menu.close"),
      language: "text",
      html: CODE_EDITOR_HTML,
    }).catch(() => undefined);
  };
  const selectProviderKey = (providerKeyID: string): void => {
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
    <View style={styles.modelBreadcrumb}><NativeButton title={providerLabel} link disabled={busy} onPress={onProviderClick} style={styles.breadcrumbProvider} /><Text style={styles.breadcrumbSeparator}>&gt;</Text><Text numberOfLines={1} style={styles.inspectorHeading}>{modelName}</Text></View>
    <View style={styles.inspectorDivider} />
    <View style={styles.inspectorBody}>
      <View style={styles.inspectorEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(model.model_enabled, booleanValue(model.enabled, true))} disabled={busy} onValueChange={(model_enabled) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { model_enabled } })} style={styles.inspectorEnableControl} /><ActionButton title={probing ? translate("providers.probing") : translate("providers.probe")} disabled={busy || probing || !probeReady} onPress={probe} />{probePresentation.compact ? <Pressable accessibilityRole="button" accessibilityLabel={probePresentation.compact} accessibilityHint={probeDetailHint} onPress={openProbeDetails} style={({ pressed }) => [styles.probeSummaryTrigger, pressed && styles.probeSummaryTriggerPressed]}><TooltipText numberOfLines={2} tooltip={probeDetailHint} style={styles.probeSummary}>{probePresentation.compact}</TooltipText></Pressable> : null}</View>
      <TextField label={translate("providers.publicModel")} labelWidth={60} value={modelName} onDraftChange={onNameDraftChange} onCommit={(name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { name } })} />
      <PickerField label={translate("providers.provider")} labelWidth={60} allowShrink value={providerLabel} values={providerLabels} disabled={busy || providers.length <= 1} onSelect={(label) => { const next = providers[providerLabels.indexOf(label)]; if (next) onProviderChange(editorIdentifier(next)); }} />
      {providerKeyOptions.length > 0 ? <PickerField label={translate("providers.providerKey")} labelWidth={60} allowShrink value={selectedProviderKey?.id ?? providerKeyOptions[0]?.value ?? ""} values={providerKeyOptions} disabled={busy || providerKeyOptions.length <= 1} onSelect={selectProviderKey} /> : null}
      <TextField label={translate("providers.upstream")} labelWidth={60} value={upstreamFieldValue} onDraftChange={(value) => drafts?.setModelUpstreamDraft(providerId, id, value)} onCommit={(upstream_model) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_model } })} />
      <View style={styles.orderEditorRow}>
        <TextField label={translate("providers.order")} labelWidth={60} controlWidth={64} value={String(displayedOrder)} keyboardType="numeric" disabled={busy || followsMultiplier} onDraftChange={(value) => drafts?.setModelOrderDraft(providerId, id, value)} onCommit={(nextOrder) => {
          const parsed = Number(nextOrder);
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
    </View>
  </View>;
}

function ProtocolPicker({ providerId, model, busy, translate, dispatch }: { providerId: string; model: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch }): React.JSX.Element {
  const id = editorIdentifier(model);
  const mode = stringValue(model.upstream_protocol_mode, "fallback");
  const protocol = stringValue(model.upstream_url_surface, "openai/chat");
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
  return <View style={styles.protocolSettings}>
    <PickerField label={translate("providers.protocolMode")} labelWidth={60} allowShrink value={fixed ? "fixed" : "fallback"} values={modeOptions} disabled={busy} onSelect={(upstream_protocol_mode) => { void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_protocol_mode } }); }} />
    <PickerField label={fixed ? translate("providers.fixedProtocol") : translate("providers.fallbackProtocol")} labelWidth={60} allowShrink value={protocol} values={options} disabled={busy} onSelect={(upstream_url_surface) => { void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_url_surface } }); }} />
    <Text style={styles.protocolHint}>{translate(fixed ? "providers.protocolModeFixedHint" : "providers.protocolModeFallbackHint")}</Text>
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

function isProbeSurface(value: string): value is "openai/responses" | "openai/chat" | "anthropic" {
  return value === "openai/responses" || value === "openai/chat" || value === "anthropic";
}

function modelProbeKey(providerId: string, modelId: string): string {
  return `${providerId}\x1f${modelId}`;
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

function identifier(record: UnknownRecord): string {
  return stringValue(record.id, stringValue(record.editor_id, stringValue(record.name, "new-item")));
}

function editorIdentifier(record: UnknownRecord): string {
  return stringValue(record.editor_id, identifier(record));
}

const CUSTOM_BASE_URL_SOURCE = "__custom__";

function ProviderSourceFields({ provider, providerID, relayStations, busy, translate, dispatch, onBaseUrlDraftChange, onNameDraftChange }: { provider: UnknownRecord; providerID: string; relayStations: RelayStationOption[]; busy: boolean; translate: Translate; dispatch: Dispatch; onBaseUrlDraftChange?: (baseURL: string) => void; onNameDraftChange?: (name: string) => void }): React.JSX.Element {
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const providerType = stringValue(provider.provider_type, "custom") === "relay" ? "relay" : "custom";
  const stationID = stringValue(provider.relay_station_id).trim();
  const selectedStation = relayStations.find((station) => station.id === stationID);
  const stationOptions = relayStations.map((station) => ({ value: `relay:${station.id}`, label: `${translate("providers.endpointSourceRelay")}: ${station.name}` }));
  const selectedValue = providerType === "relay" ? `relay:${stationID}` : CUSTOM_BASE_URL_SOURCE;
  const sourceOptions: AssistantSettingOption[] = [
    { value: CUSTOM_BASE_URL_SOURCE, label: translate("providers.endpointSourceCustom") },
    ...stationOptions,
    ...(providerType === "relay" && stationID && !stationOptions.some((option) => option.value === selectedValue)
      ? [{ value: selectedValue, label: `${translate("providers.endpointSourceRelay")}: ${stationID}` }]
      : []),
  ];
  const providerName = drafts?.providerDisplayName(provider) ?? stringValue(provider.name, stringValue(provider.display_name));
  const providerBaseURL = drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base));
  const effectiveName = providerType === "relay" ? stringValue(selectedStation?.name, providerName) : providerName;
  const effectiveBaseURL = providerType === "relay" ? stringValue(selectedStation?.baseURL, providerBaseURL) : providerBaseURL;
  const selectSource = (value: string): void => {
    if (value === CUSTOM_BASE_URL_SOURCE) {
      void dispatch("provider.patch", { provider_id: providerID, changes: { provider_type: "custom", relay_station_id: "" } });
      return;
    }
    const nextStationID = value.startsWith("relay:") ? value.slice("relay:".length) : "";
    if (!nextStationID) return;
    void dispatch("provider.select_relay_station", { provider_id: providerID, station_id: nextStationID });
  };
  const commitBaseURL = (endpoint: string): void | Promise<void> => {
    if (providerType !== "custom") return;
    const station = relayStationForBaseUrl(endpoint, relayStations);
    if (station) {
      return dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id });
    }
    return dispatch("provider.patch", { provider_id: providerID, changes: { endpoint } });
  };
  return <View style={styles.providerSourceFields}>
    <PickerField label={translate("providers.endpointSource")} labelWidth={68} value={selectedValue} values={sourceOptions} disabled={busy} onSelect={selectSource} />
    <TextField label={translate("providers.baseUrl")} labelWidth={68} value={effectiveBaseURL} disabled={busy || providerType === "relay"} onDraftChange={onBaseUrlDraftChange} onCommit={commitBaseURL} />
    <TextField label={translate("providers.providerName")} labelWidth={68} value={effectiveName} disabled={busy || providerType === "relay"} onDraftChange={onNameDraftChange} onCommit={(name) => providerType === "custom" ? dispatch("provider.patch", { provider_id: providerID, changes: { name } }) : undefined} />
  </View>;
}

function ProviderEditor({ provider, relaySources, relayStations, native, busy, translate, dispatch, onSecretState, onNameDraftChange, sourceModel, onReturnToModel }: { provider: UnknownRecord; relaySources: RelaySourceOption[]; relayStations: RelayStationOption[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onNameDraftChange?: (name: string) => void; sourceModel?: UnknownRecord; onReturnToModel: () => void }): React.JSX.Element {
  const id = editorIdentifier(provider);
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const keys = useMemo(() => stringList(provider.api_key_names), [provider.api_key_names]);
  const keyStates = useMemo(() => providerKeyStates(provider), [provider.key_states]);
  const providerBaseUrl = drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base));
  const keyChoices = useMemo(() => providerKeyChoices(provider, relaySources, providerBaseUrl), [provider, providerBaseUrl, relaySources]);
  const [selectedKeyID, setSelectedKeyID] = useState<string>(keyChoices[0]?.id ?? "");
  const pendingKeySelection = useRef<string | undefined>(undefined);
  const selectedChoice = keyChoices.find((choice) => choice.id === selectedKeyID) ?? keyChoices[0];
  const selectedKeyState = selectedChoice?.state;
  const selectedRelaySource = selectedChoice?.kind === "relay" ? selectedChoice.source : undefined;
  React.useLayoutEffect(() => {
    const pending = pendingKeySelection.current;
    const pendingState = pending ? keyStates.find((key) => key.name === pending) : undefined;
    if (pendingState) {
      pendingKeySelection.current = undefined;
      if (selectedKeyID !== pendingState.id) setSelectedKeyID(pendingState.id);
      return;
    }
    if (!keyChoices.some((choice) => choice.id === selectedKeyID)) setSelectedKeyID(keyChoices[0]?.id ?? "");
  }, [keyChoices, keyStates, selectedKeyID]);
  const addKey = (): void => {
    const name = uniqueKeyName(keys);
    pendingKeySelection.current = name;
    void dispatch("provider.key_add", { provider_id: id, name });
  };
  const renameKey = (name: string): void => {
    if (!selectedKeyState || selectedKeyState.source.kind !== "independent" || !name || name === selectedKeyState.name) return;
    pendingKeySelection.current = name;
    void dispatch("provider.key_patch", { provider_id: id, old_name: selectedKeyState.name, name });
  };
  const deleteKey = (): void => {
    if (!selectedKeyState) return;
    const selectedKey = selectedKeyState.name;
    const affectedModelLines = asRecords(provider.models)
      .filter((model) => stringValue(model.api_key_name).trim() === selectedKey)
      .map((model, index) => {
        const publicName = stringValue(model.name, translate("providers.newModel")).trim();
        const upstreamName = upstreamModelLabel(model).trim();
        const label = upstreamName && upstreamName !== publicName ? `${publicName} (${upstreamName})` : publicName;
        return `${index + 1}. ${label}`;
      });
    void native.showConfirmation({
      title: translate("providers.deleteApiKey", { key: apiKeyDisplayName(selectedKey, translate) }),
      message: affectedModelLines.length > 0
        ? translate("providers.deleteApiKeyModelsMessage", { models: affectedModelLines.join("\n") })
        : translate("providers.deleteApiKeyNoModelsMessage"),
      confirmLabel: translate("common.delete"),
    }).then((confirmed) => {
      if (!confirmed) return undefined;
      pendingKeySelection.current = undefined;
      return dispatch("provider.key_delete", { provider_id: id, name: selectedKey });
    });
  };
  const providerName = drafts?.providerDisplayName(provider) ?? stringValue(provider.display_name, stringValue(provider.name, translate("providers.newProvider")));
  const providerIsCustom = stringValue(provider.provider_type, "custom") !== "relay";
  const providerLabel = providerIsCustom ? providerName : stringValue(provider.display_name, stringValue(provider.name, translate("providers.newProvider")));
  const sourceModelLabel = sourceModel ? drafts?.modelDisplayName(id, sourceModel) ?? stringValue(sourceModel.name, translate("providers.newModel")) : "";
  const selectedKeyConfigured = booleanValue(selectedKeyState?.configured);
  const relayAccountID = selectedChoice?.kind === "relay"
    ? selectedRelaySource?.accountID ?? selectedChoice.state?.source.accountID ?? ""
    : "";
  const relayResourceID = selectedChoice?.kind === "relay"
    ? selectedRelaySource?.resourceID ?? selectedChoice.state?.source.resourceID ?? ""
    : "";
  const relaySecretTarget = relayAccountID && relayResourceID ? `${relayAccountID}:${relayResourceID}` : "";
  const keyRows = keyChoices.map((choice) => ({
    key: choice.id,
    cells: [providerKeyChoiceLabel({ ...choice, name: drafts?.providerKeyDisplayName(id, choice.id, choice.name) ?? choice.name }, translate)],
  }));
  return <View style={styles.providerEditorContent}>
    <View style={styles.providerEditorHeader}><Text numberOfLines={1} style={styles.providerEditorHeading}>{translate("providers.provider")}: {providerLabel}</Text>{sourceModel ? <NativeButton title={translate("providers.backToModel", { model: sourceModelLabel })} link disabled={busy} onPress={onReturnToModel} style={styles.providerReturnToModel} /> : null}</View>
    <View style={styles.providerEditorSection}>
    <View style={styles.providerEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(provider.enabled, true)} disabled={busy} onValueChange={(enabled) => dispatch("provider.patch", { provider_id: id, changes: { enabled } })} /></View>
    <ProviderSourceFields provider={provider} providerID={id} relayStations={relayStations} busy={busy} translate={translate} dispatch={dispatch} onBaseUrlDraftChange={(value) => drafts?.setProviderBaseUrlDraft(id, value)} onNameDraftChange={(value) => { if (drafts) drafts.setProviderNameDraft(id, value); else onNameDraftChange?.(value); }} />
    <View style={styles.providerKeysEditor}>
      <View style={styles.providerKeysHeader}>
        <Text style={styles.providerKeysHeading}>{translate("providers.apiKeys")}</Text>
        <View style={styles.providerKeyActions}>
          <IconButton label="+" title={translate("common.add")} disabled={busy} onPress={addKey} />
          <IconButton label="−" title={translate("common.delete")} disabled={busy || !selectedKeyState} onPress={deleteKey} />
        </View>
      </View>
      <NativeTable columns={[{ label: translate("providers.key"), width: 260 }]} rows={keyRows} selectedKey={selectedChoice?.id ?? ""} compact cellHorizontalPadding={0} firstColumnHorizontalPadding={0} onSelectionChange={setSelectedKeyID} style={styles.providerKeyTable} />
      <View style={styles.providerKeyFields}>
        {selectedChoice ? <>
          <TextField key={`provider-key:${id}:${selectedChoice.id}`} label={translate("providers.keyName")} labelWidth={68} value={drafts?.providerKeyDisplayName(id, selectedChoice.id, selectedChoice.name) ?? selectedChoice.name} disabled={!selectedKeyState || selectedChoice.kind === "relay"} onDraftChange={(value) => drafts?.setProviderKeyNameDraft(id, selectedChoice.id, value)} onCommit={renameKey} />
          {selectedChoice.kind === "relay" ? relaySecretTarget
            ? <NativeSecretField plainText autoCommit label={translate("providers.keyValue")} hint={translate("providers.relayKeyValueHint")} labelWidth={68} busy={busy} disabled domain="relay_accounts" field="api_key" target={relaySecretTarget} onSecretState={onSecretState} />
            : <TextField label={translate("providers.keyValue")} labelWidth={68} value={translate("providers.relayKeyValueHint")} disabled onCommit={() => undefined} />
            : selectedKeyState ? <NativeSecretField plainText autoCommit label={translate("providers.keyValue")} hint={selectedKeyConfigured ? translate("providers.apiKeySavedHint") : translate("providers.apiKeyInput")} labelWidth={68} busy={busy} domain="providers_models" field="api_key" target={`${id}\x1f${selectedKeyState.name}`} onSecretState={onSecretState} /> : null}
        </> : <Text style={styles.empty}>{translate("common.notAvailable")}</Text>}
      </View>
    </View>
    </View>
  </View>;
}

function CodexWorkspace({ snapshot, ipc, busy, translate, dispatch, onSecretState, onEditorConflict, rawReloadToken, rawBaselineToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onEditorConflict: RawEditorConflictHandler; rawReloadToken: number; rawBaselineToken: number }): React.JSX.Element {
  const state = domainState(snapshot, "codex");
  const structured = asRecord(state.structured);
  const permissions = asRecord(structured.permissions);
  const providers = asRecords(structured.providers);
  const deployments = asRecords(state.models);
  const deploymentModels = [...new Set(deployments.map((item) => stringValue(item.model)).filter(Boolean))];
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const [providerFieldDrafts, setProviderFieldDrafts] = useState<Record<string, Partial<Record<"id" | "name", string>>>>({});
  const providerFieldDraftsRef = useRef(providerFieldDrafts);
  providerFieldDraftsRef.current = providerFieldDrafts;
  const [modelDraft, setModelDraft] = useState<string>();
  const providerRows = providers.map(editableRecord);
  const setProviderFieldDraft = useCallback((providerID: string, field: "id" | "name", value: string): void => {
    const current = providerFieldDraftsRef.current;
    if (current[providerID]?.[field] === value) return;
    const updated = { ...current, [providerID]: { ...current[providerID], [field]: value } };
    providerFieldDraftsRef.current = updated;
    setProviderFieldDrafts(updated);
  }, []);
  const providerDisplayID = useCallback((entry: UnknownRecord): string => {
    const providerID = identifier(entry);
    return providerFieldDrafts[providerID]?.id ?? providerID;
  }, [providerFieldDrafts]);
  const providerDisplayName = useCallback((entry: UnknownRecord): string => {
    const providerID = identifier(entry);
    return providerFieldDrafts[providerID]?.name ?? stringValue(entry.name);
  }, [providerFieldDrafts]);
  const directProvider = stringValue(structured.model_provider);
  const selectedProviderID = selectedProvider ?? directProvider;
  const provider = providerRows.find((item) => identifier(item) === selectedProviderID)
    ?? providerRows.find((item) => providerDisplayID(item) === selectedProviderID)
    ?? providerRows[0];
  const directBaseUrl = stringValue(structured.openai_base_url);
  const displayedModel = modelDraft ?? stringValue(structured.model);
  useEffect(() => {
    setProviderFieldDrafts((current) => {
      let next = current;
      for (const [draftID, draft] of Object.entries(current)) {
        const entry = providerRows.find((item) => identifier(item) === draftID)
          ?? (draft.id !== undefined ? providerRows.find((item) => identifier(item) === draft.id) : undefined);
        if (!entry) {
          if (next === current) next = { ...current };
          delete next[draftID];
          continue;
        }
        const persistedID = identifier(entry);
        const persistedName = stringValue(entry.name);
        const remainingID = draft.id !== undefined && draft.id !== persistedID ? draft.id : undefined;
        const remainingName = draft.name !== undefined && draft.name !== persistedName ? draft.name : undefined;
        if (remainingID === undefined && remainingName === undefined) {
          if (next === current) next = { ...current };
          delete next[draftID];
          continue;
        }
        const nextDraft = {
          ...(remainingID !== undefined ? { id: remainingID } : {}),
          ...(remainingName !== undefined ? { name: remainingName } : {}),
        };
        if (persistedID !== draftID) {
          if (next === current) next = { ...current };
          delete next[draftID];
          next[persistedID] = { ...next[persistedID], ...nextDraft };
        } else if (draft.id !== remainingID || draft.name !== remainingName) {
          if (next === current) next = { ...current };
          next[draftID] = nextDraft;
        }
      }
      return next;
    });
    setModelDraft((current) => current !== undefined && current === stringValue(structured.model) ? undefined : current);
  }, [modelDraft, providerFieldDrafts, providerRows, structured.model]);
  const providerOptions: AssistantSettingOption[] = [
    { value: "", label: translate("common.none") },
    ...[...new Set(["openai", directProvider, ...providerRows.map(identifier).filter(Boolean)])]
      .filter(Boolean)
      .map((value) => {
        const configured = providerRows.find((item) => identifier(item) === value);
        return { value, label: configured ? providerDisplayID(configured) : value };
      }),
  ];
  const sandboxMode = stringValue(permissions.sandbox_mode);
  const approvalPolicy = stringValue(permissions.approval_policy);
  const [structuredWidth, setStructuredWidth] = useState(470);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const validationErrors = stringList(state.validation_errors);
  const validationWarnings = stringList(state.warnings);
  const validationStatus = Object.keys(state).length === 0
    ? undefined
    : validationErrors.length > 0
      ? validationErrors.map((message) => localizeCodexValidationMessage(message, translate)).join("\n")
      : validationWarnings.length > 0
        ? validationWarnings.map((message) => localizeCodexValidationMessage(message, translate)).join("\n")
        : undefined;
  const validationStatusStyle = validationErrors.length > 0
    ? styles.codexValidationError
    : validationWarnings.length > 0
      ? styles.codexValidationWarning
      : undefined;
  const selectDirectProvider = (nextProvider: string): void => {
    if (!nextProvider) {
      void dispatch("patch", { model_provider: null }).then(() => setSelectedProvider(undefined));
      return;
    }
    const configuredProvider = providerRows.find((item) => identifier(item) === nextProvider);
    const base_url = nextProvider === "openai" ? stringValue(structured.openai_base_url) : stringValue(configuredProvider?.base_url);
    void dispatch("patch", { model_provider: nextProvider, direct_connection: { provider: nextProvider, base_url } }).then(() => setSelectedProvider(configuredProvider ? nextProvider : undefined));
  };
  const patchProvider = (changes: UnknownRecord): Promise<void> => {
    if (!provider) return Promise.resolve();
    const currentProviderId = identifier(provider);
    const draft = providerFieldDraftsRef.current[currentProviderId] ?? {};
    const nextProvider = {
      ...provider,
      ...(draft.id !== undefined ? { id: draft.id } : {}),
      ...(draft.name !== undefined ? { name: draft.name } : {}),
      ...changes,
    };
    const nextProviderId = identifier(nextProvider);
    const patch: UnknownRecord = {
      providers: providerRows.map((item) => identifier(item) === currentProviderId ? nextProvider : item),
    };
    if (currentProviderId === directProvider && nextProviderId !== currentProviderId) {
      patch.model_provider = nextProviderId;
      patch.direct_connection = { provider: nextProviderId, base_url: stringValue(asRecord(nextProvider).base_url) };
    }
    return dispatch("patch", patch).then(() => {
      if (nextProviderId !== currentProviderId) setSelectedProvider(nextProviderId);
    });
  };
  const addProvider = (): void => {
    const existingIds = new Set(providerRows.map(identifier));
    let suffix = 1;
    while (existingIds.has(`provider-${suffix}`)) suffix += 1;
    const id = `provider-${suffix}`;
    void dispatch("patch", { providers: [...providerRows, { id, name: "", base_url: "", wire_api: "responses", auth_mode: "none" }] }).then(() => setSelectedProvider(id));
  };
  const deleteProvider = (): void => {
    if (!provider) return;
    const providerId = identifier(provider);
    const patch: UnknownRecord = { providers: providerRows.filter((item) => identifier(item) !== providerId) };
    if (providerId === directProvider) {
      patch.model_provider = "openai";
      patch.direct_connection = { provider: "openai", base_url: stringValue(structured.openai_base_url) };
    }
    void dispatch("patch", patch).then(() => setSelectedProvider(undefined));
  };
  const fileMissing = state.config_exists === false;
  return <SettingsWorkspace validationStatus={validationStatus} validationStatusStyle={validationStatusStyle} structuredWidth={structuredWidth} onStructuredWidthChange={setStructuredWidth} workspaceWidth={workspaceWidth} onWorkspaceWidthChange={setWorkspaceWidth} translate={translate} missingMessage={fileMissing ? translate("settings.codexMissing") : undefined} structured={<>
    <Section title={translate("codex.providers")}>
      <View style={styles.form}>
        <PickerField label={translate("codex.provider")} value={directProvider} values={providerOptions} disabled={busy} onSelect={selectDirectProvider} />
        {directProvider === "openai" ? <TextField label={translate("codex.gateway")} value={directBaseUrl} disabled={busy} onCommit={(base_url) => dispatch("patch", { model_provider: "openai", direct_connection: { provider: "openai", base_url } })} /> : null}
      </View>
      <View style={styles.codexProviderEditor}>
        <View style={styles.codexProviderToolbar}>
          <Text style={styles.codexProviderToolbarTitle}>{`${translate("screen.configured")} ${translate("codex.providers")} (${providerRows.length})`}</Text>
          <View style={styles.codexProviderActions}>
            <NativeButton title={translate("common.add")} symbol="plus" toolTip={translate("common.add")} accessibilityLabel={translate("common.add")} compact primary disabled={busy} onPress={addProvider} style={styles.codexProviderActionButton} />
            <NativeButton title={translate("common.delete")} symbol="minus" toolTip={translate("common.delete")} accessibilityLabel={translate("common.delete")} compact destructive disabled={busy || !provider} onPress={deleteProvider} style={styles.codexProviderActionButton} />
          </View>
      </View>
      <View style={[styles.split, styles.codexProviderSplit]}>
        <NativeTable columns={[{ label: translate("providers.providerId"), width: 116 }, { label: translate("providers.displayName"), width: 230 }, { label: translate("providers.authentication"), width: 84 }]} rows={providerRows.map((item) => ({ key: identifier(item), cells: [providerDisplayID(item), providerDisplayName(item), stringValue(item.auth_mode, "none")] }))} selectedKey={provider ? identifier(provider) : ""} onSelectionChange={setSelectedProvider} style={styles.codexListTable} />
        <View style={styles.detailPane}>{provider ? <View key={`codex-provider:${identifier(provider)}`} style={styles.form}><TextField label={translate("providers.providerId")} value={providerDisplayID(provider)} onDraftChange={(value) => setProviderFieldDraft(identifier(provider), "id", value)} onCommit={(id) => patchProvider({ id })} /><TextField label={translate("providers.displayName")} value={providerDisplayName(provider)} onDraftChange={(value) => setProviderFieldDraft(identifier(provider), "name", value)} onCommit={(name) => patchProvider({ name })} /><TextField label={translate("providers.baseUrl")} value={stringValue(provider.base_url)} onCommit={(base_url) => patchProvider({ base_url })} /><PickerField label={translate("providers.protocol")} value={stringValue(provider.wire_api, "responses")} values={["responses"]} disabled={busy} onSelect={(wire_api) => patchProvider({ wire_api })} /><PickerField label={translate("providers.authentication")} value={stringValue(provider.auth_mode, "none")} values={["none", "env_key", "openai_auth", "command", "bearer"]} disabled={busy} onSelect={(auth_mode) => patchProvider({ auth_mode })} /><TextField label={translate("codex.environmentKey")} value={stringValue(provider.env_key)} onCommit={(env_key) => patchProvider({ env_key })} /><NativeCheckbox label={translate("providers.requiresOpenAIAuth")} value={booleanValue(provider.requires_openai_auth)} disabled={busy} onValueChange={(requires_openai_auth) => patchProvider({ requires_openai_auth })} /><TextField label={translate("providers.authCommand")} value={stringValue(provider.auth_command)} onCommit={(auth_command) => patchProvider({ auth_command })} /></View> : <EmptyState translate={translate} />}</View>
      </View>
      </View>
    </Section>
    <Section title={translate("codex.model")}><View style={styles.form}>
      <PickerField label={translate("codex.activeDeployment")} value={displayedModel} values={deploymentModels.length > 0 ? deploymentModels : [{ value: "", label: translate("common.none") }]} disabled={busy || deploymentModels.length === 0} onSelect={(model) => { setModelDraft(model); const selection = deployments.find((item) => stringValue(item.model) === model); if (selection) dispatch("select_model", { selection: { model: selection.model, provider: selection.provider, deployment_id: selection.deployment_id } }); }} />
      <TextField label={translate("common.model")} value={displayedModel} onDraftChange={setModelDraft} onCommit={(model) => dispatch("patch", { model })} />
      <TextField label={translate("codex.reviewModel")} value={stringValue(structured.review_model)} onCommit={(review_model) => dispatch("patch", { review_model })} />
    </View></Section>
    <Section title={translate("codex.authentication")}><View style={styles.form}>
      <NativeSecretField plainText autoCommit label={translate("common.apiKey")} busy={busy} domain="codex" field="api_key" onSecretState={onSecretState} />
    </View></Section>
    <Section title={translate("codex.permissions")}><View style={styles.form}>
      <SegmentedField label={translate("codex.permissionMode")} value={stringValue(permissions.mode, "unset")} values={assistantSettingOptions(["legacy", "profile", "unset"], translate)} disabled={busy} onSelect={(mode) => dispatch("patch", { permissions: { mode } })} />
      <PickerField label={translate("codex.sandboxMode")} value={sandboxMode} values={[...new Set([sandboxMode, "read-only", "workspace-write", "danger-full-access"].filter(Boolean))]} disabled={busy || permissions.mode === "profile"} onSelect={(sandbox_mode) => dispatch("patch", { permissions: { sandbox_mode } })} />
      <PickerField label={translate("codex.approvalPolicy")} value={approvalPolicy} values={[...new Set([approvalPolicy, "untrusted", "on-request", "never"].filter(Boolean))]} disabled={busy} onSelect={(approval_policy) => dispatch("patch", { permissions: { approval_policy } })} />
      {hasBooleanSetting(permissions, "network_access") ? <ToggleRow label={translate("codex.network")} value={booleanValue(permissions.network_access)} disabled={busy} onChange={(network_access) => dispatch("patch", { permissions: { network_access } })} /> : null}
      <TextField label={translate("codex.writableRoots")} value={stringList(permissions.writable_roots).join("\n")} multiline onCommit={(writable_roots) => dispatch("patch", { permissions: { writable_roots: splitLines(writable_roots) } })} />
    </View></Section>
    <Section title={translate("codex.features")}><FeatureToggles value={asRecord(structured.features)} disabled={busy} onChange={(features) => dispatch("patch", { features })} translate={translate} /></Section>
  </>} raw={<><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawToml")} domain="codex" document="config" language="toml" ipc={ipc} busy={busy} translate={translate} onConflict={onEditorConflict} reloadToken={rawReloadToken} baselineToken={rawBaselineToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawAuth")} domain="codex" document="auth" language="json" ipc={ipc} busy={busy} translate={translate} onConflict={onEditorConflict} reloadToken={rawReloadToken} baselineToken={rawBaselineToken} /></>} />;
}

function SettingsWorkspace({ validationStatus, validationStatusStyle, structuredWidth, onStructuredWidthChange, workspaceWidth, onWorkspaceWidthChange, translate, missingMessage, structured, raw }: { validationStatus?: string; validationStatusStyle?: StyleProp<TextStyle>; structuredWidth: number; onStructuredWidthChange: (width: number) => void; workspaceWidth: number; onWorkspaceWidthChange: (width: number) => void; translate: Translate; missingMessage?: string; structured: React.ReactNode; raw: React.ReactNode }): React.JSX.Element {
  const rawPaneMinimum = 344;
  const minStructuredWidth = 360;
  const [structuredViewportWidth, setStructuredViewportWidth] = useState(0);
  const maxStructuredWidth = workspaceWidth > 0
    ? Math.max(minStructuredWidth, Math.min(680, workspaceWidth - rawPaneMinimum))
    : 470;
  const paneWidth = Math.max(minStructuredWidth, Math.min(structuredWidth, maxStructuredWidth));
  const hasStructuredHorizontalOverflow = structuredViewportWidth > 0 && structuredViewportWidth < SETTINGS_STRUCTURED_CONTENT_MIN_WIDTH;
  return <View style={styles.codexWorkspaceFrame} onLayout={({ nativeEvent }) => onWorkspaceWidthChange(nativeEvent.layout.width)}>{validationStatus ? <Text style={[styles.codexValidationStatus, validationStatusStyle]}>{validationStatus}</Text> : null}{missingMessage ? <Text style={styles.settingsMissingMessage}>{missingMessage}</Text> : null}<NativeSplitView paneWidth={paneWidth} minPaneWidth={minStructuredWidth} maxPaneWidth={maxStructuredWidth} onPaneWidthChange={(width) => onStructuredWidthChange(Math.max(minStructuredWidth, Math.min(width, maxStructuredWidth)))} style={styles.codexSplit}><View style={styles.codexStructuredPane}><Text style={styles.paneHeading}>{translate("settings.structured")}</Text><ScrollView style={styles.codexStructuredScroll} contentContainerStyle={[styles.codexStructured, hasStructuredHorizontalOverflow && styles.codexStructuredWithHorizontalScrollbar]} horizontal={false} alwaysBounceHorizontal={false} showsHorizontalScrollIndicator={hasStructuredHorizontalOverflow} showsVerticalScrollIndicator onLayout={({ nativeEvent }) => setStructuredViewportWidth(nativeEvent.layout.width)}><NativePersistentScrollIndicator style={styles.codexStructuredScrollIndicator} />{structured}</ScrollView></View><View style={styles.codexRawPane}><Text style={styles.paneHeading}>{translate("settings.rawLiveDraft")}</Text><View style={styles.codexRawEditors}>{raw}</View></View></NativeSplitView></View>;
}

function FeatureToggles({ value, disabled, onChange, translate }: { value: UnknownRecord; disabled: boolean; onChange: (features: UnknownRecord) => void; translate: Translate }): React.JSX.Element {
  // Only render feature flags that are actually present in config.toml.
  // ``supported`` is a capability catalog, not evidence that the user saved
  // a value; showing every absent flag as an unchecked setting was the same
  // source-of-truth error that made the old Claude panel misleading.
  const keys = Object.keys(value).filter((key) => typeof value[key] === "boolean");
  return <CompactToggleGrid>{keys.length === 0 ? <EmptyState translate={translate} /> : keys.map((key) => <ToggleRow key={key} label={codexFeatureLabel(key, translate)} value={booleanValue(value[key])} disabled={disabled} onChange={(enabled) => onChange({ ...value, [key]: enabled })} />)}</CompactToggleGrid>;
}

function CompactToggleGrid({ children }: { children: React.ReactNode }): React.JSX.Element {
  const items = React.Children.toArray(children);
  return <View style={styles.featureGrid}>{items.map((child, index) => <View key={child && typeof child === "object" && "key" in child && child.key != null ? String(child.key) : String(index)} style={styles.featureGridItem}>{child}</View>)}</View>;
}

function ClaudeScreen({ snapshot, ipc, busy, translate, dispatch, onSecretState, onEditorConflict, deployment, onDeploymentChange, rawReloadToken, rawBaselineToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onEditorConflict: RawEditorConflictHandler; deployment: ClaudeDeploymentDraft; onDeploymentChange: (key: keyof ClaudeDeploymentDraft, value: string) => Promise<void>; rawReloadToken: number; rawBaselineToken: number }): React.JSX.Element {
  const state = domainState(snapshot, "claude");
  const settings = asRecord(state.settings);
  const desktop = asRecord(state.desktop);
  const developer = asRecord(state.developer);
  const permissions = asRecord(settings.permissions);
  const sandbox = asRecord(settings.sandbox);
  const filesystem = asRecord(sandbox.filesystem);
  const [structuredWidth, setStructuredWidth] = useState(470);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const fileMissing = settings.file_exists === false;
  const unavailable = state.available === false;
  const validationStatus = unavailable ? translate("settings.claudeUnavailable") : undefined;
  const updateDeployment = (key: keyof ClaudeDeploymentDraft, value: string): Promise<void> => onDeploymentChange(key, value);
  const desktopAvailable = desktop.available !== false;
  const developerAvailable = developer.available !== false;
  const desktopConfigured = desktop.config_exists === true;
  const desktopProvider = stringValue(desktop.provider);
  const desktopAuthScheme = desktopConfigured ? stringValue(desktop.auth_scheme, "bearer") : "";
  const desktopModelNames = stringList(desktop.model_names);
  const permissionMode = stringValue(permissions.defaultMode);
  const effortLevel = stringValue(settings.effortLevel);
  const hasSandboxBoolean = [
    "enabled",
    "failIfUnavailable",
    "autoAllowBashIfSandboxed",
    "allowUnsandboxedCommands",
  ].some((field) => hasBooleanSetting(sandbox, field));
  const hasCapabilityBoolean = [
    "disableBundledSkills",
    "disableClaudeAiConnectors",
    "disableRemoteControl",
    "disableAllHooks",
  ].some((field) => hasBooleanSetting(settings, field));
  return <SettingsWorkspace validationStatus={validationStatus} validationStatusStyle={unavailable ? styles.codexValidationError : undefined} structuredWidth={structuredWidth} onStructuredWidthChange={setStructuredWidth} workspaceWidth={workspaceWidth} onWorkspaceWidthChange={setWorkspaceWidth} translate={translate} missingMessage={fileMissing ? translate("settings.claudeMissing") : undefined} structured={<>
    <Section title={translate("claude.desktop")}><Text style={styles.cardHint}>{translate("claude.desktopSourceHint")}</Text><View style={styles.structuredForm}>
      <PickerField label={translate("claude.desktopProvider")} value={desktopProvider} values={[{ value: "", label: translate("common.none") }, "gateway", "anthropic", "bedrock", "vertex", "foundry"]} disabled={busy || !desktopAvailable} onSelect={(inferenceProvider) => dispatch("desktop_patch", { inferenceProvider: inferenceProvider || null })} />
      <TextField label={translate("claude.desktopGateway")} value={stringValue(desktop.gateway_url)} disabled={busy || !desktopAvailable} onCommit={(inferenceGatewayBaseUrl) => dispatch("desktop_patch", { inferenceGatewayBaseUrl })} />
      <PickerField label={translate("claude.desktopAuthScheme")} value={desktopAuthScheme} values={[{ value: "", label: translate("common.none") }, "bearer", "x-api-key"]} disabled={busy || !desktopAvailable} onSelect={(inferenceGatewayAuthScheme) => dispatch("desktop_patch", { inferenceGatewayAuthScheme: inferenceGatewayAuthScheme || null })} />
      <TextField label={translate("claude.desktopModels")} value={desktopModelNames.join("\n")} hint={translate("claude.desktopModelsHint")} multiline compactMultiline disabled={busy || !desktopAvailable} onCommit={(value) => dispatch("desktop_models_patch", { model_names: splitLines(value) })} />
      <NativeSecretField plainText autoCommit label={translate("claude.desktopApiKey")} busy={busy || !desktopAvailable} domain="claude" field="desktop_gateway_api_key" onSecretState={onSecretState} />
      <ToggleRow label={translate("claude.desktopDeveloperMode")} value={booleanValue(developer.developer_mode_enabled)} disabled={busy || !developerAvailable} onChange={(allowDevTools) => dispatch("developer_patch", { allowDevTools })} />
      <Text style={styles.cardHint}>{translate("claude.desktopDeveloperModeHint")}</Text>
    </View></Section>
    <Section title={translate("claude.deployment")}><Text style={styles.cardHint}>{translate("claude.codeSourceHint")}</Text><View style={styles.structuredForm}>
      <TextField label={translate("claude.model")} value={deployment.model} disabled={busy} onCommit={(value) => updateDeployment("model", value)} />
      <TextField label={translate("claude.gateway")} value={deployment.base_url} disabled={busy} onCommit={(value) => updateDeployment("base_url", value)} />
      <NativeSecretField plainText autoCommit label={translate("claude.token")} busy={busy} domain="claude" field="deployment_token" onSecretState={onSecretState} />
    </View></Section>
    <Section title={translate("claude.memory")}><View style={styles.structuredForm}>
      {hasBooleanSetting(settings, "autoMemoryEnabled") ? <ToggleRow label={translate("claude.autoMemory")} value={booleanValue(settings.autoMemoryEnabled)} disabled={busy} onChange={(autoMemoryEnabled) => dispatch("patch", { autoMemoryEnabled })} /> : <EmptyState translate={translate} />}
    </View></Section>
    <Section title={translate("claude.permissions")}><View style={styles.structuredForm}>
      <PickerField label={translate("claude.permissions")} value={permissionMode} values={[{ value: "", label: translate("common.none") }, ...CLAUDE_PERMISSION_MODES.map((value) => ({ value, label: claudePermissionLabel(value, translate) }))]} disabled={busy} onSelect={(defaultMode) => dispatch("patch", { permissions: { defaultMode: defaultMode || null } })} />
      {permissions.disableBypassPermissionsMode !== undefined ? <ToggleRow label={translate("claude.disableBypassPermissions")} value={stringValue(permissions.disableBypassPermissionsMode) === "disable"} disabled={busy} onChange={(disabled) => dispatch("patch", { permissions: { disableBypassPermissionsMode: disabled ? "disable" : null } })} /> : null}
    </View></Section>
    <Section title={translate("claude.sandbox")}><View style={styles.structuredForm}>
      {hasSandboxBoolean || hasBooleanSetting(filesystem, "disabled") ? <>
        {hasBooleanSetting(sandbox, "enabled") ? <ToggleRow label={translate("claude.sandbox")} value={booleanValue(sandbox.enabled)} disabled={busy} onChange={(enabled) => dispatch("patch", { sandbox: { enabled } })} /> : null}
        {hasBooleanSetting(sandbox, "failIfUnavailable") ? <ToggleRow label={translate("claude.sandboxFailIfUnavailable")} value={booleanValue(sandbox.failIfUnavailable)} disabled={busy} onChange={(failIfUnavailable) => dispatch("patch", { sandbox: { failIfUnavailable } })} /> : null}
        {hasBooleanSetting(sandbox, "autoAllowBashIfSandboxed") ? <ToggleRow label={translate("claude.sandboxAutoAllowBash")} value={booleanValue(sandbox.autoAllowBashIfSandboxed)} disabled={busy} onChange={(autoAllowBashIfSandboxed) => dispatch("patch", { sandbox: { autoAllowBashIfSandboxed } })} /> : null}
        {hasBooleanSetting(sandbox, "allowUnsandboxedCommands") ? <ToggleRow label={translate("claude.sandboxAllowUnsandboxed")} value={booleanValue(sandbox.allowUnsandboxedCommands)} disabled={busy} onChange={(allowUnsandboxedCommands) => dispatch("patch", { sandbox: { allowUnsandboxedCommands } })} /> : null}
        {hasBooleanSetting(filesystem, "disabled") ? <ToggleRow label={translate("claude.filesystem")} value={filesystem.disabled === false} disabled={busy} onChange={(enabled) => dispatch("patch", { sandbox: { filesystem: { disabled: !enabled } } })} /> : null}
      </> : <EmptyState translate={translate} />}
    </View></Section>
    <Section title={translate("claude.modelBehavior")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.fallbackModel")} value={stringList(settings.fallbackModel).join("\n") || stringValue(settings.fallbackModel)} multiline compactMultiline onCommit={(fallbackModel) => dispatch("patch", { fallbackModel: splitLines(fallbackModel) })} />
      <PickerField label={translate("claude.effortLevel")} value={effortLevel} values={[{ value: "", label: translate("common.none") }, "low", "medium", "high", "xhigh"]} disabled={busy} onSelect={(nextEffortLevel) => dispatch("patch", { effortLevel: nextEffortLevel || null })} />
      {hasBooleanSetting(settings, "autoCompactEnabled") ? <ToggleRow label={translate("claude.autoCompact")} value={booleanValue(settings.autoCompactEnabled)} disabled={busy} onChange={(autoCompactEnabled) => dispatch("patch", { autoCompactEnabled })} /> : null}
    </View></Section>
    <Section title={translate("claude.capabilities")}>{hasCapabilityBoolean ? <CompactToggleGrid>
      {hasBooleanSetting(settings, "disableBundledSkills") ? <ToggleRow label={translate("claude.disableBundledSkills")} value={booleanValue(settings.disableBundledSkills)} disabled={busy} onChange={(disableBundledSkills) => dispatch("patch", { disableBundledSkills })} /> : null}
      {hasBooleanSetting(settings, "disableClaudeAiConnectors") ? <ToggleRow label={translate("claude.disableClaudeAiConnectors")} value={booleanValue(settings.disableClaudeAiConnectors)} disabled={busy} onChange={(disableClaudeAiConnectors) => dispatch("patch", { disableClaudeAiConnectors })} /> : null}
      {hasBooleanSetting(settings, "disableRemoteControl") ? <ToggleRow label={translate("claude.disableRemoteControl")} value={booleanValue(settings.disableRemoteControl)} disabled={busy} onChange={(disableRemoteControl) => dispatch("patch", { disableRemoteControl })} /> : null}
      {hasBooleanSetting(settings, "disableAllHooks") ? <ToggleRow label={translate("claude.disableAllHooks")} value={booleanValue(settings.disableAllHooks)} disabled={busy} onChange={(disableAllHooks) => dispatch("patch", { disableAllHooks })} /> : null}
    </CompactToggleGrid> : <EmptyState translate={translate} />}</Section>
  </>} raw={<><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.desktopRawJson")} domain="claude" document="desktop" language="json" ipc={ipc} busy={busy} translate={translate} onConflict={onEditorConflict} reloadToken={rawReloadToken} baselineToken={rawBaselineToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.developerRawJson")} domain="claude" document="developer" language="json" ipc={ipc} busy={busy} translate={translate} onConflict={onEditorConflict} reloadToken={rawReloadToken} baselineToken={rawBaselineToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.codeRawJson")} domain="claude" document="settings" language="json" ipc={ipc} busy={busy} translate={translate} onConflict={onEditorConflict} reloadToken={rawReloadToken} baselineToken={rawBaselineToken} /></>} />;
}

function claudePermissionLabel(value: string, translate: Translate): string {
  const key = `claude.permission.${value}`;
  return CLAUDE_PERMISSION_MODES.includes(value) ? translate(key) : translate("claude.permission.unknown", { value });
}

const CLAUDE_PERMISSION_MODES = ["default", "manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "delegate"];

function RuntimeWorkspace({ snapshot, busy, translate, dispatch, onSecretState, clearSecret }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear }): React.JSX.Element {
  const state = domainState(snapshot, "runtime");
  const settings = asRecords(state.settings).length > 0 ? asRecords(state.settings) : asRecords(state.categories).flatMap((category) => asRecords(category.settings));
  const groups = groupBy(settings, (item) => stringValue(item.category, translate("runtime.categories")));
  const [contentWidth, setContentWidth] = useState(0);
  const oneColumn = contentWidth > 0 && contentWidth < 1_000;
  const dshSyncToken = DSH_VISION_ROUTER_QUICK_KEYS.map((key) => `${key}:${stringValue(settings.find((item) => identifier(item) === key)?.value)}`).join("|");
  return <View style={styles.runtimeWorkspaceFrame}><ScrollView style={styles.runtimeScrollSurface} contentContainerStyle={styles.runtimeWorkspace} onLayout={({ nativeEvent }) => setContentWidth(nativeEvent.layout.width)}>{Object.keys(groups).length === 0 ? <EmptyState translate={translate} /> : Object.entries(groups).map(([category, entries]) => <Section key={category} title={runtimeCategoryLabel(category, translate)}><View style={[styles.runtimeTwoColumnForm, oneColumn && styles.runtimeOneColumnForm]}>{entries.map((item) => <RuntimeField key={identifier(item)} item={item} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} dshSyncToken={dshSyncToken} />)}</View></Section>)}</ScrollView></View>;
}

function DataManagementWorkspace({ snapshot, busy, webDavOperationBusy, statuses, hasPendingChanges, translate, dispatch, onSecretState, onResize, onFlushPendingFields, onTabSwitchError, onInspectImport, onImport, onConfirmImportReplace, onExport, onApplyImported, onProbeWebDav, onApplyWebDav, onSyncWebDav }: {
  snapshot?: CoreSnapshot;
  busy: boolean;
  webDavOperationBusy: boolean;
  statuses: Partial<Record<DataManagementTab, string>>;
  hasPendingChanges: boolean;
  translate: Translate;
  dispatch: Dispatch;
  onSecretState: (state: SecretState) => void;
  onResize: (width: number, height: number) => Promise<boolean>;
  onFlushPendingFields: () => Promise<void>;
  onTabSwitchError: (tab: DataManagementTab, reason: unknown) => void;
  onInspectImport: () => Promise<IpcResults["import_preview"] | undefined>;
  onImport: (sections: ConfigDomain[]) => Promise<IpcResults["import"] | undefined>;
  onConfirmImportReplace: (sections: string[]) => Promise<boolean>;
  onExport: (sections: ConfigDomain[]) => Promise<void>;
  onApplyImported: (sections: ConfigDomain[]) => Promise<void>;
  onProbeWebDav: () => Promise<void>;
  onApplyWebDav: () => Promise<void>;
  onSyncWebDav: (action: WebDavSyncAction) => Promise<void>;
}): React.JSX.Element {
  const [tab, setTab] = useState<DataManagementTab>("import");
  const [importPreview, setImportPreview] = useState<IpcResults["import_preview"]>();
  const [importSections, setImportSections] = useState<ConfigDomain[]>([]);
  const [stagedSections, setStagedSections] = useState<ConfigDomain[]>([]);
  const [exportSections, setExportSections] = useState<ConfigDomain[]>([...DATA_PACKAGE_DOMAINS]);
  const [syncAction, setSyncAction] = useState<WebDavSyncAction>("sync");
  const detectedImportSections = useMemo(() => DATA_PACKAGE_DOMAINS.filter((domain) => importPreview?.detected_sections.includes(domain)), [importPreview]);
  const importedDirty = stagedSections.some((domain) => snapshot?.drafts[domain]?.dirty);
  const importReviewReady = importPreview !== undefined && stagedSections.length === 0;
  const replacingDraftSections = importSections.filter((domain) => importPreview?.preview[domain]?.will_replace_draft === true);
  const windows = Platform.OS === "windows";
  const importItemCount = stagedSections.length || detectedImportSections.length;
  const importHeight = importItemCount === 0
    ? windows ? 168 : 148
    : importItemCount <= 2
      ? windows ? (replacingDraftSections.length > 0 ? 320 : 285) : (replacingDraftSections.length > 0 ? 305 : 270)
      : windows ? (replacingDraftSections.length > 0 ? 385 : 350) : (replacingDraftSections.length > 0 ? 370 : 335);
  useEffect(() => {
    // Keep each pane content-sized. The default landing height matches the
    // compact utility-window baseline; review panes expand only when content
    // requires it.
    const size = tab === "webdav"
      ? { width: windows ? 660 : 640, height: windows ? 520 : 480 }
      : tab === "export"
        ? { width: windows ? 640 : 620, height: windows ? 340 : 320 }
        : importItemCount === 0
          ? { width: windows ? 620 : 600, height: importHeight }
          : importItemCount <= 2
            ? { width: windows ? 640 : 620, height: importHeight }
            : { width: windows ? 660 : 640, height: importHeight };
    void onResize(size.width, size.height);
  }, [detectedImportSections.length, importHeight, onResize, stagedSections.length, tab]);
  const switchDataManagementTab = (next: DataManagementTab): void => {
    if (next === tab) return;
    const previous = tab;
    // Keep navigation immediate even if Core is slow or unavailable. Pending
    // field writes finish in the background and report their own failure.
    const pending = onFlushPendingFields();
    setTab(next);
    void pending.catch((reason: unknown) => {
      onTabSwitchError(previous, reason);
    });
  };
  const toggleSection = (setter: React.Dispatch<React.SetStateAction<ConfigDomain[]>>, domain: ConfigDomain, enabled: boolean): void => setter((current) => enabled
    ? DATA_PACKAGE_DOMAINS.filter((item) => item === domain || current.includes(item))
    : current.filter((item) => item !== domain));
  const sectionList = (available: readonly ConfigDomain[], selected: readonly ConfigDomain[], disabled: boolean, onToggle: (domain: ConfigDomain, enabled: boolean) => void): React.JSX.Element => {
    const sections = DATA_PACKAGE_SECTIONS.filter(({ domain }) => available.includes(domain));
    return <View style={[styles.dataManagementSectionPicker, dataManagementPolishStyles.sectionPicker]}>{sections.map(({ domain, labelKey }) => <NativeCheckbox key={domain} label={translate(labelKey)} value={selected.includes(domain)} disabled={disabled} onValueChange={(enabled) => onToggle(domain, enabled)} style={styles.dataManagementSectionControl} />)}</View>;
  };
  const chooseImportFile = async (): Promise<void> => {
    const inspected = await onInspectImport();
    if (!inspected) return;
    const detected = DATA_PACKAGE_DOMAINS.filter((domain) => inspected.detected_sections.includes(domain));
    setImportPreview(inspected);
    setImportSections(detected);
    setStagedSections([]);
  };
  const importSelected = async (): Promise<void> => {
    if (!importPreview || importSections.length === 0) return;
    if (replacingDraftSections.length > 0) {
      const labels = DATA_PACKAGE_SECTIONS.filter(({ domain }) => replacingDraftSections.includes(domain)).map(({ labelKey }) => translate(labelKey));
      if (!await onConfirmImportReplace(labels)) return;
    }
    const imported = await onImport(importSections);
    if (!imported) {
      setImportPreview(undefined);
      setImportSections([]);
      setStagedSections([]);
      return;
    }
    setStagedSections(DATA_PACKAGE_DOMAINS.filter((domain) => imported.draft_domains.includes(domain)));
  };
  const hasWebDavChanges = snapshot?.drafts.webdav?.dirty === true;
  const syncOptions: Array<{ id: WebDavSyncAction; title: string }> = [
    { id: "sync", title: translate("dataManagement.syncSmart") },
    { id: "push", title: translate("dataManagement.syncPush") },
    { id: "pull", title: translate("dataManagement.syncPull") },
  ];
  const selectedSyncLabel = syncOptions.find(({ id }) => id === syncAction)?.title ?? syncOptions[0].title;
  const selectionTool = (selectedCount: number, availableCount: number, onSelectAll: () => void, onDeselectAll: () => void): React.JSX.Element => {
    const allSelected = availableCount > 0 && selectedCount === availableCount;
    return <ActionButton title={translate(allSelected ? "dataManagement.deselectAll" : "dataManagement.selectAll")} disabled={busy || availableCount === 0} onPress={allSelected ? onDeselectAll : onSelectAll} />;
  };
  return <View style={styles.dataManagementWorkspace}>
    <View style={[styles.dataManagementTabBar, dataManagementPolishStyles.tabBar]}><WindowTabs values={[
      { id: "import", title: translate("dataManagement.tab.import") },
      { id: "export", title: translate("dataManagement.tab.export") },
      { id: "webdav", title: translate("dataManagement.tab.webdav") },
    ]} selected={tab} onSelect={(next) => switchDataManagementTab(next as DataManagementTab)} style={[styles.dataManagementTabs, dataManagementPolishStyles.tabs]} /></View>
    {tab === "import" ? <ScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent, !importPreview && dataManagementPolishStyles.importLandingContent]}>
      {!importPreview ? <View style={[styles.dataManagementImportIntro, dataManagementPolishStyles.importIntro]}><View style={styles.dataManagementImportFileRow}><Text style={styles.dataManagementImportFileLabel}>{translate("dataManagement.importFile")}</Text><View style={styles.dataManagementImportFileValue}><Text numberOfLines={1} style={styles.dataManagementImportFilePlaceholder}>{translate("dataManagement.noImportFile")}</Text></View><ActionButton title={translate("dataManagement.chooseImportFile")} disabled={busy} onPress={() => { void chooseImportFile(); }} /></View><Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.importHint")}</Text></View> : null}
      {importReviewReady ? <>
        <View style={dataManagementPolishStyles.paneIntro}><Text style={dataManagementPolishStyles.paneHeading}>{translate("dataManagement.importContent")}</Text><Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.importRecognizedHint")}</Text></View>
        <DataManagementGroup>
          <View style={styles.dataManagementSelectionBar}><Text style={[styles.dataManagementSelectionCount, dataManagementPolishStyles.compactText]}>{translate("dataManagement.importDetectedCount", { count: detectedImportSections.length })} · {translate("dataManagement.selectedCount", { count: importSections.length })}</Text><View style={styles.dataManagementToolbarButtons}><ActionButton title={translate("dataManagement.changeImportFile")} disabled={busy} onPress={() => { void chooseImportFile(); }} />{selectionTool(importSections.length, detectedImportSections.length, () => setImportSections([...detectedImportSections]), () => setImportSections([]))}<ActionButton title={translate("dataManagement.importSelected")} disabled={busy || importSections.length === 0} onPress={() => { void importSelected(); }} /></View></View>
          {sectionList(detectedImportSections, importSections, busy, (domain, enabled) => toggleSection(setImportSections, domain, enabled))}
          {replacingDraftSections.length > 0 ? <Text numberOfLines={2} style={[styles.dataManagementSensitiveHint, dataManagementPolishStyles.compactText]}>{translate("dataManagement.importReplaceDraftWarning", { sections: DATA_PACKAGE_SECTIONS.filter(({ domain }) => replacingDraftSections.includes(domain)).map(({ labelKey }) => translate(labelKey)).join(" · ") })}</Text> : null}
        </DataManagementGroup>
      </> : null}
      {stagedSections.length > 0 ? <DataManagementGroup>
        <View style={styles.dataManagementSelectionBar}><Text style={[styles.dataManagementSelectionCount, dataManagementPolishStyles.compactText]}>{translate("dataManagement.selectedCount", { count: stagedSections.length })}</Text>{importedDirty ? <ActionButton title={translate("menu.apply")} disabled={busy} onPress={() => { void onApplyImported(stagedSections); }} /> : null}</View>
          {sectionList(stagedSections, stagedSections, true, () => undefined)}
      </DataManagementGroup> : null}
      {statuses.import ? <Text style={[styles.dataManagementStatus, dataManagementPolishStyles.compactText]}>{statuses.import}</Text> : null}
    </ScrollView> : null}
    {tab === "export" ? <View style={styles.dataManagementPane}>
      <ScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>
        <View style={dataManagementPolishStyles.paneIntro}><Text style={dataManagementPolishStyles.paneHeading}>{translate("dataManagement.exportContent")}</Text><Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.exportHint")}</Text></View>
        <DataManagementGroup>
          <View style={styles.dataManagementSelectionBar}><Text style={[styles.dataManagementSelectionCount, dataManagementPolishStyles.compactText]}>{translate("dataManagement.selectedCount", { count: exportSections.length })}</Text><View style={styles.dataManagementToolbarButtons}>{selectionTool(exportSections.length, DATA_PACKAGE_DOMAINS.length, () => setExportSections([...DATA_PACKAGE_DOMAINS]), () => setExportSections([]))}</View></View>
          {sectionList(DATA_PACKAGE_DOMAINS, exportSections, busy, (domain, enabled) => toggleSection(setExportSections, domain, enabled))}
        </DataManagementGroup>
      </ScrollView>
      <View style={[styles.dataManagementBottomActions, dataManagementPolishStyles.bottomActions]}>
        <View style={styles.dataManagementBottomMessage}>
          <Text numberOfLines={2} style={[styles.dataManagementSensitiveNote, dataManagementPolishStyles.compactText]}>{translate("dataManagement.sensitiveHint")}</Text>
          {statuses.export ? <Text style={[styles.dataManagementStatus, dataManagementPolishStyles.compactText]}>{statuses.export}</Text> : null}
        </View>
        <ActionButton primary title={translate("dataManagement.exportSelected")} disabled={busy || exportSections.length === 0} onPress={() => { void onExport(exportSections); }} />
      </View>
    </View> : null}
    {tab === "webdav" ? <View style={[styles.dataManagementWebDavPane, styles.dataManagementWebDavContent, dataManagementPolishStyles.webDavContent]}>
      <View style={dataManagementPolishStyles.paneIntro}><Text style={dataManagementPolishStyles.paneHeading}>{translate("dataManagement.syncSettings")}</Text><Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.webdavHint")}</Text></View>
      <WebDavWorkspace snapshot={snapshot} busy={busy || webDavOperationBusy} status={statuses.webdav} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onProbe={onProbeWebDav} hasChanges={hasWebDavChanges || hasPendingChanges} onApply={onApplyWebDav}>
        <View style={[styles.dataManagementSyncContent, dataManagementPolishStyles.syncContent]}>
          <View style={styles.dataManagementSyncScope}><Text style={styles.dataManagementSyncScopeLabel}>{translate("dataManagement.webdavScope")}</Text><Text numberOfLines={2} style={styles.dataManagementSyncScopeValue}>{translate("dataManagement.section.providersModels")} · {translate("dataManagement.section.relayAccounts")}</Text></View>
          <View style={styles.dataManagementDirection}><Text style={styles.dataManagementDirectionLabel}>{translate("dataManagement.syncDirection")}</Text><NativePicker labels={syncOptions.map(({ title }) => title)} selectedValue={selectedSyncLabel} disabled={busy || webDavOperationBusy} onChange={({ nativeEvent }) => { const option = syncOptions[nativeEvent.index]; if (option) setSyncAction(option.id); }} style={styles.dataManagementDirectionPicker} /><ActionButton title={translate("dataManagement.syncNow")} disabled={busy || webDavOperationBusy || snapshot?.webdav.enabled !== true} onPress={() => { void onSyncWebDav(syncAction); }} /></View>
        </View>
      </WebDavWorkspace>
    </View> : null}
  </View>;
}

function DataManagementGroup({ children, style }: { children?: React.ReactNode; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <View style={[styles.dataManagementGroup, style]}>
    {children ? <View style={[styles.dataManagementGroupBody, dataManagementPolishStyles.groupBody]}>{children}</View> : null}
  </View>;
}

function RuntimeField({ item, busy, translate, dispatch, onSecretState, clearSecret, dshSyncToken }: { item: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; dshSyncToken: string }): React.JSX.Element {
  const [jsonResetToken, setJsonResetToken] = useState(0);
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
  let control: React.ReactNode;
  let action: React.ReactNode;
  if (kind === "boolean" || kind === "toggle" || kind === "bool" || kind === "bool_auto") {
    control = <NativeCheckbox label={label} labelVisible={false} value={booleanValue(item.value)} disabled={busy} onValueChange={(next) => dispatch("set_setting", { key, value: kind === "bool_auto" ? (next ? "auto" : "off") : next })} style={styles.runtimeBooleanControl} />;
  } else if (kind === "select" || kind === "choice" || kind === "enum") {
    const optionValues = stringList(item.options);
    const visibleOptionValues = optionValues.filter((option) => !(DSH_VISION_ROUTER_QUICK_KEYS.some((quickKey) => quickKey === key) && option === "inherit"));
    const optionLabels = visibleOptionValues.map((option) => runtimeOptionLabel(key, option, translate));
    const selectedIndex = visibleOptionValues.indexOf(value);
    control = <NativePicker labels={optionLabels} selectedValue={optionLabels[selectedIndex] ?? optionLabels[0] ?? ""} disabled={busy} onChange={({ nativeEvent }) => { const next = optionValues[nativeEvent.index]; const visibleNext = visibleOptionValues[nativeEvent.index] ?? next; if (visibleNext !== undefined) void dispatch("set_setting", { key, value: visibleNext }); }} style={styles.runtimeValueControl} />;
  } else if (kind === "json") {
    // JSON may contain provider credentials. Keep the document in the native
    // editor and stage it through the one-time Core secret capability while
    // still giving the user a real multiline textarea.
    control = <NativeSecretInputControl label={label} hint={item.retained === true ? translate("runtime.secretRetained") : translate("runtime.jsonPlaceholder")} busy={busy} domain="runtime" field="setting" target={key} multiline plainText autoCommit resetToken={jsonResetToken} onSecretState={onSecretState} />;
    action = <ActionButton title={item.will_clear === true ? translate("common.willClear") : translate("common.clear")} disabled={busy || item.retained !== true || item.will_clear === true} onPress={() => { void clearSecret({ domain: "runtime", field: "setting", target: key }).then(() => setJsonResetToken((current) => current + 1)); }} />;
  } else if (item.secret === true) {
    control = <NativeSecretInputControl label={label} hint={item.retained === true ? translate("runtime.secretRetained") : undefined} busy={busy} domain="runtime" field="setting" target={key} onSecretState={onSecretState} setTitle={translate("common.set")} />;
    action = <ActionButton title={item.will_clear === true ? translate("common.willClear") : translate("common.clear")} disabled={busy || item.retained !== true || item.will_clear === true} onPress={() => clearSecret({ domain: "runtime", field: "setting", target: key })} />;
  } else {
    control = <RuntimeValueField label={label} value={value} keyboardType={["number", "integer", "int", "float", "mb", "optional_int", "optional_float", "optional_mb"].includes(storageKind) ? "numeric" : undefined} onCommit={(next) => dispatch("set_setting", { key, value: next })} />;
  }
  const isBoolean = kind === "boolean" || kind === "toggle" || kind === "bool" || kind === "bool_auto";
  const isMultiline = kind === "json";
  if (isMultiline) {
    return <View style={[styles.runtimeField, styles.runtimeMultilineField]}>
      <View style={styles.runtimeMultilineHeader}>
        <Text style={styles.runtimeMultilineLabel} accessibilityLabel={label}>{label}</Text>
        {action ? <View style={styles.runtimeMultilineHeaderActions}>{action}</View> : null}
      </View>
      <View style={styles.runtimeMultilineEditor}>{control}</View>
      <RuntimeFieldMeta item={item} translate={translate} multiline />
    </View>;
  }
  return <View style={styles.runtimeField}><View style={styles.runtimeInputRow}><Text numberOfLines={2} style={styles.runtimeFieldLabel} accessibilityLabel={label}>{label}</Text><View style={styles.runtimeValueSlot}>{control}</View>{!isBoolean && unit ? <Text numberOfLines={1} style={styles.runtimeUnit}>{unit}</Text> : null}{!isBoolean ? <View style={styles.runtimeActionSlot}>{action}</View> : null}</View><RuntimeFieldMeta item={item} translate={translate} /></View>;
}

function RuntimeFieldMeta({ item, translate, multiline = false }: { item: UnknownRecord; translate: Translate; multiline?: boolean }): React.JSX.Element {
  const key = identifier(item);
  const kind = stringValue(item.kind, "text");
  const rawDefaultValue = DSH_VISION_ROUTER_QUICK_DEFAULTS[key] ?? stringValue(item.default, translate("common.empty"));
  const defaultValue = kind === "select" || kind === "choice" || kind === "enum"
    ? runtimeOptionLabel(key, rawDefaultValue, translate)
    : rawDefaultValue;
  const help = runtimeFieldHelp(key, stringValue(item.help), translate);
  if (multiline) {
    return <View style={[styles.runtimeHelpSlot, styles.runtimeMultilineHelpSlot]}>
      <Text style={styles.runtimeJsonDefaultHint}>{translate("runtime.jsonDefaultHint")}</Text>
      {help ? <Text style={styles.runtimeHelpText}>{help}</Text> : null}
    </View>;
  }
  return <View style={styles.runtimeHelpSlot}><Text style={styles.runtimeHelpText}>{translate("common.default")}: {defaultValue}{help ? `\n${help}` : ""}</Text></View>;
}

type PendingTextFieldState = {
  draft: string;
  onChangeText: (next: string) => void;
  commit: () => Promise<void>;
  reset: () => void;
  isDirty: () => boolean;
};

// Keep ordinary text local across every settings surface while typing. Blur,
// submit, and Apply are the only commit points, so active typing never waits
// for a Core dispatch or a full snapshot publication.
function usePendingTextField(value: string, onCommit: (next: string) => void | Promise<void>, label: string, onDraftChange?: (next: string) => void): PendingTextFieldState {
  const [draft, setDraft] = useState(value);
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(label));
  const draftRef = useRef(value);
  const committedRef = useRef(value);
  const valueRef = useRef(value);
  const dirtyRef = useRef(false);
  const commitInFlight = useRef<Promise<void> | undefined>(undefined);
  const onCommitRef = useRef(onCommit);
  const onDraftChangeRef = useRef(onDraftChange);

  useEffect(() => { onCommitRef.current = onCommit; }, [onCommit]);
  useEffect(() => { onDraftChangeRef.current = onDraftChange; }, [onDraftChange]);
  useEffect(() => {
    valueRef.current = value;
    if (!dirtyRef.current) {
      committedRef.current = value;
      draftRef.current = value;
      setDraft(value);
      onDraftChangeRef.current?.(value);
    }
  }, [value]);

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
  }, [setDirty]);

  const onChangeText = useCallback((next: string): void => {
    draftRef.current = next;
    setDraft(next);
    onDraftChangeRef.current?.(next);
    // If an earlier staged value is still in flight, returning to the last
    // known draft still needs one more stage after that write finishes.
    setDirty(next !== committedRef.current || commitInFlight.current !== undefined);
  }, [setDirty]);

  const reset = useCallback((): void => {
    const next = valueRef.current;
    committedRef.current = next;
    draftRef.current = next;
    setDraft(next);
    onDraftChangeRef.current?.(next);
    setDirty(false);
  }, [setDirty]);

  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset, isDirty: () => dirtyRef.current });
    return () => {
      // Selection changes can remove the editor before AppKit/WinUI delivers
      // its blur event. Preserve the local draft instead of silently dropping
      // it when the field leaves the tree.
      if (dirtyRef.current) void commit().catch(() => undefined);
      registry?.register(fieldId.current);
    };
  }, [commit, registry, reset]);

  return { draft, onChangeText, commit, reset, isDirty: () => dirtyRef.current };
}

function RuntimeValueField({ label, value, keyboardType, onCommit }: { label: string; value: string; keyboardType?: "default" | "numeric"; onCommit: (value: string) => void | Promise<void> }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label);
  return <NativeTextField style={[styles.input, styles.runtimeValueControl]} value={field.draft} onChangeText={field.onChangeText} onBlur={() => { void field.commit().catch(() => undefined); }} onSubmitEditing={() => { void field.commit().catch(() => undefined); }} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />;
}

function WebDavWorkspace({ snapshot, busy, status, translate, dispatch, onSecretState, onProbe, hasChanges, onApply, children }: { snapshot?: CoreSnapshot; busy: boolean; status?: string; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onProbe: () => Promise<void>; hasChanges: boolean; onApply: () => Promise<void>; children: React.ReactNode }): React.JSX.Element {
  const state = domainState(snapshot, "webdav");
  const labelAlign = "left";
  return <DataManagementGroup style={styles.webDavForm}>
    <View style={[styles.webdavFormBody, dataManagementPolishStyles.webDavFormBody]}>
      <View style={styles.webdavStateRow}><NativeCheckbox label={translate("webdav.enabled")} value={booleanValue(state.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { enabled })} style={styles.webdavEnabledControl} /><View style={styles.webdavStateSpacer} /><Text numberOfLines={1} style={[styles.webdavStateStatus, dataManagementPolishStyles.compactText]}>{snapshot ? webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate) : ""}</Text></View>
      <View style={[styles.webdavFormRows, dataManagementPolishStyles.webDavFormRows]}>
        <TextField label={translate("webdav.url")} value={stringValue(state.url)} labelWidth={WEBDAV_FORM_LABEL_WIDTH} labelAlign={labelAlign} hintStyle={dataManagementPolishStyles.compactText} onCommit={(url) => dispatch("patch", { url })} />
        <TextField label={translate("webdav.username")} value={stringValue(state.username)} labelWidth={WEBDAV_FORM_LABEL_WIDTH} labelAlign={labelAlign} hintStyle={dataManagementPolishStyles.compactText} onCommit={(username) => dispatch("patch", { username })} />
        <WebDavPasswordField labelWidth={WEBDAV_FORM_LABEL_WIDTH} labelAlign={labelAlign} configured={snapshot?.webdav.password.present === true} busy={busy} translate={translate} onSecretState={onSecretState} />
        <TextField label={translate("webdav.remoteFile")} value={stringValue(state.remote_name)} labelWidth={WEBDAV_FORM_LABEL_WIDTH} labelAlign={labelAlign} hintStyle={dataManagementPolishStyles.compactText} onCommit={(remote_name) => dispatch("patch", { remote_name })} />
        <TextField label={translate("webdav.syncEvery")} value={stringValue(state.sync_interval)} labelWidth={WEBDAV_FORM_LABEL_WIDTH} labelAlign={labelAlign} controlWidth={150} suffix={translate("webdav.minutes")} hintStyle={dataManagementPolishStyles.compactText} keyboardType="numeric" onCommit={(sync_interval) => dispatch("patch", { sync_interval })} />
        <TextField label={translate("webdav.httpTimeout")} value={stringValue(state.timeout)} labelWidth={WEBDAV_FORM_LABEL_WIDTH} labelAlign={labelAlign} controlWidth={150} suffix={translate("webdav.seconds")} hintStyle={dataManagementPolishStyles.compactText} keyboardType="numeric" onCommit={(timeout) => dispatch("patch", { timeout })} />
      </View>
      <View style={[styles.webdavSyncArea, dataManagementPolishStyles.webDavSyncArea]}>{children}</View>
      <View style={[styles.webdavActionRow, dataManagementPolishStyles.webDavActionRow]}><ActionButton title={translate("dataManagement.testConnection")} disabled={busy} onPress={() => { void onProbe(); }} />{status ? <Text numberOfLines={1} style={[styles.webdavActionStatus, dataManagementPolishStyles.compactText]}>{status}</Text> : null}<View style={styles.webdavActionSpacer} /><ActionButton primary title={translate("common.saveAndApply")} disabled={busy || !hasChanges} onPress={() => { void onApply(); }} /></View>
    </View>
  </DataManagementGroup>;
}

function WebDavPasswordField({ configured, busy, translate, onSecretState, labelWidth = 94, labelAlign = "left", style }: { configured: boolean; busy: boolean; translate: Translate; onSecretState: (state: SecretState) => void; labelWidth?: number; labelAlign?: "left" | "right"; style?: StyleProp<ViewStyle> }): React.JSX.Element {
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
  return <View style={[styles.formRow, compactStyles.formRow, style]}><Text style={[styles.formRowLabel, { width: labelWidth, textAlign: labelAlign }]}>{translate("webdav.password")}</Text><View style={[styles.formRowControl, compactStyles.formRowControl]}><NativeSecureTextInput domain="webdav" field="password" label={translate("webdav.password")} placeholder={configured ? translate("webdav.passwordHintConfigured") : translate("webdav.passwordHintOptional")} disabled={busy || status === "saving"} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(state) => {
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
  }} style={styles.webdavPasswordInput} /></View></View>;
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

function routeTraceReasonLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
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
    "responses_endpoint_unsupported": "logs.routeTrace.reasonResponsesUnsupported",
    "malformed_web_search_function_call": "logs.routeTrace.reasonWebSearchFormat",
  };
  const upstreamStatus = value.match(/^upstream-status-(\d+)$/);
  if (upstreamStatus?.[1]) return translate("logs.routeTrace.upstreamStatus", { status: upstreamStatus[1] });
  return translate(labels[value.trim().toLowerCase()] ?? "logs.routeTrace.reasonUnknown");
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

function logErrorDetail(value: unknown): string {
  const error = asRecord(value);
  if (Object.keys(error).length === 0) return compactLogValue(value);
  const parts = [
    error.status_code === undefined ? "" : `HTTP ${compactLogValue(error.status_code)}`,
    compactLogValue(error.type),
    compactLogValue(error.code),
    compactLogValue(error.reason),
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
    status = servicePrefix[2];
    detail = servicePrefix[3].trim();
  } else {
    const proxyPrefix = detail.match(/^(?:\d{2}:\d{2}:\d{2}\s+-\s+)?([^:]+):(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s*(.*)$/);
    const levelPrefix = detail.match(/^(?:\[([A-Z]+)\]|(DEBUG|INFO|WARNING|ERROR|CRITICAL):)\s*(.*)$/);
    if (proxyPrefix) {
      if (tab !== "service") source = proxyPrefix[1]?.trim() || source;
      status = proxyPrefix[2] || "";
      detail = (proxyPrefix[3] ?? "").trim();
    } else if (levelPrefix) {
      status = levelPrefix[1] || levelPrefix[2] || "";
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
  const action = tab === "menu" ? detail.split(/[:;,]/, 1)[0]?.trim() ?? "" : "";
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
    : rawStatus || (value.error ? translate("logs.failed") : "");
  const rawEvent = compactLogValue(value.event);
  const event = tab === "route-trace" ? routeTraceEventLabel(rawEvent, translate) : rawEvent;
  const action = compactLogValue(value.action);
  const duration = compactLogValue(value.duration_ms);
  const usage = asRecord(value.usage);
  const tokens = compactLogValue(usage.total_tokens ?? value.total_tokens);
  const details: string[] = [];
  const directDetail = value.error === undefined
    ? compactLogValue(value.detail ?? value.message)
    : logErrorDetail(value.error);
  if (directDetail) details.push(
    tab === "route-trace"
      ? routeTraceDetailLabel(directDetail, translate)
      : tab === "recovery" ? recoveryDetailLabel(directDetail, translate) : directDetail,
  );
  const used = new Set(["ts", "timestamp", "time", "created_at", "updated_at", "checked_at", "started_at", "heartbeat_at", "source", "provider", "api_key_name", "model_group", "public_model", "route_key", "routing_state", "status", "result", "detail", "message", "event", "action", "error", "upstream_model", "model", "duration_ms", "usage", "total_tokens", "request_id", "requestId", "session", "session_id", "deployment_id", "deployment_order", "target_order", "route", "failed_route", "failed_route_key", "failed_deployment_id", "candidate_routes", "candidates", "after_constraints", "selected_candidates", "cooldown_deployments"]);
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
  return {
    key: logRecordBaseKey(tab, time, requestKey, rawEvent, action, original),
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

function logColumns(tab: LogTab, translate: Translate): LogColumn[] {
  const time = { label: translate("logs.localTime"), width: 164, value: (row: RenderedLogRecord) => row.time };
  const status = { label: translate("common.status"), width: 88, value: (row: RenderedLogRecord) => row.status };
  const detail = { label: translate("logs.detail"), width: 260, flex: true, value: (row: RenderedLogRecord) => row.detail };
  if (tab === "requests") return [
    time,
    { label: translate("providers.publicModel"), width: 142, value: (row) => row.model },
    { label: translate("providers.upstream"), width: 142, value: (row) => row.upstreamModel },
    { label: translate("common.provider"), width: 104, value: (row) => row.provider },
    { label: translate("logs.apiKeyName"), width: 120, value: (row) => row.apiKeyName },
    status,
    { label: translate("logs.duration"), width: 92, value: (row) => row.duration },
    { label: translate("logs.tokenCount"), width: 96, value: (row) => row.tokens },
    detail,
  ];
  if (tab === "menu") return [
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
    { ...status, width: 76 },
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
  const fixedWidth = columns.reduce((total, column) => total + column.width, 0);
  const flexible = columns.filter((column) => column.flex);
  const extra = usableWidth - fixedWidth;
  if (extra <= 0 || flexible.length === 0) return columns;
  const share = extra / flexible.length;
  return columns.map((column) => column.flex ? { ...column, width: column.width + share } : column);
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
      closeLabel: translate("menu.close"),
      language: "json",
      html: CODE_EDITOR_HTML,
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
          <ScrollView
            ref={timelineScrollRef}
            style={styles.routeTraceTimelineScroll}
            contentContainerStyle={[styles.routeTraceTimeline, hasTimelineHorizontalOverflow && styles.routeTraceTimelineWithHorizontalScrollbar]}
            alwaysBounceHorizontal={false}
            showsHorizontalScrollIndicator={false}
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
          </ScrollView>
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

function LogsWorkspace({ snapshot, ipc, native, busy, translate, dispatch, requestedTab, requestedTabKey = 0 }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; requestedTab?: typeof LOG_TABS[number]; requestedTabKey?: number }): React.JSX.Element {
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
  const selectedTabRef = useRef(selected);
  selectedTabRef.current = selected;
  const active = activeState?.tab === selected ? activeState.log : undefined;
  useEffect(() => {
    if (appliedTabRequestKey.current === requestedTabKey) return;
    appliedTabRequestKey.current = requestedTabKey;
    if (requestedTab) setSelected(requestedTab);
  }, [requestedTab, requestedTabKey]);
  useEffect(() => { setFilterDraft(active?.filter ?? ""); }, [active?.filter, selected]);
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
  const statusParts = [
    translate(active && lineCount >= active.limit ? "logs.latestLinesAtLimit" : "common.lines", { count: lineCount }),
    paused ? translate("logs.paused") : "",
  ].filter(Boolean);
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
        rememberPassword: Platform.OS !== "macos" && value.remember_password === true,
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
            rememberPassword: Platform.OS !== "macos" && account.rememberPassword,
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
      <View style={styles.logFilterRow}><Text style={styles.toolbarLabel}>{translate("common.filter")}</Text><NativeTextField style={styles.logFilterInput} value={filterDraft} placeholder={translate("logs.filterCurrent")} onChangeText={(filter) => { setFilterDraft(filter); if (filterTimer.current) clearTimeout(filterTimer.current); filterTimer.current = setTimeout(() => { void dispatch("logs.set_filter", { tab: selected, filter }, "logs"); }, 250); }} accessibilityLabel={translate("common.filter")} /></View>
      <View style={styles.logToolbarSpacer} />
      <View style={styles.logActionsRow}>{selected === "recovery" ? <NativeButton title={translate("logs.clearRecoveryCooldown")} accessibilityLabel={translate("logs.clearRecoveryCooldown")} compact disabled={busy || cooldownClearPending} onPress={clearCooldowns} style={styles.clearCooldownButton} /> : null}<IconButton label="" symbol={paused ? "play" : "pause"} title={paused ? translate("common.resume") : translate("common.pause")} disabled={busy} onPress={togglePaused} /><IconButton label="" symbol="trash" title={translate("common.clearView")} disabled={busy} onPress={clearLogs} /></View>
    </View>
    <WindowTabs nativeRef={tabsRef} values={tabOptions} selected={selected} disabled={busy} onSelect={(tab) => {
      if (clearTabRef.current) return;
      setSelected(tab as LogTab);
      if (tab === "online-usage") openRelayUsageLogs();
    }} style={styles.logsTabs} />
    {rows.length > 0 ? selected === "route-trace"
      ? <RouteTraceWorkspace requests={routeTraceRequests} selectedKey={selectedKey} native={native} translate={translate} onSelect={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} />
      : <View style={styles.logTableFrame} onLayout={({ nativeEvent }) => setTableWidth(nativeEvent.layout.width)}><NativeTable columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths onSelectionChange={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} onRowDoublePress={(_key, index) => {
        const row = rows[index];
        if (!row) return;
        void native.showReadOnlyText({ title: translate("logs.originalRecord"), text: row.original, closeLabel: translate("menu.close"), language: "json", html: CODE_EDITOR_HTML });
      }} style={styles.logTable} /></View>
      : <View style={styles.logEmptySurface}><Text style={styles.logEmptyText}>{clearing || active ? translate("logs.empty") : translate("logs.loading")}</Text></View>}
    <View style={styles.logInfoBar}><Text numberOfLines={1} style={[styles.cardHint, selected === "route-trace" && styles.routeTraceInfoText]}>{statusParts.join(" | ")}</Text></View>
  </View>;
}

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.section, compactStyles.section]}><View style={[styles.sectionHeader, compactStyles.inlineGap]}><Text style={styles.sectionTitle}>{title}</Text>{action}</View>{children}</View>;
}

function EmptyState({ translate }: { translate: Translate }): React.JSX.Element { return <Text style={styles.empty}>{translate("screen.noData")}</Text>; }

const ActionButton = React.forwardRef<HostInstance, { title: string; onPress: () => void; disabled?: boolean; primary?: boolean; danger?: boolean; style?: StyleProp<ViewStyle> }>(function ActionButton({ title, onPress, disabled, primary, danger, style }, ref): React.JSX.Element {
  return <NativeButton ref={ref} title={title} disabled={disabled} primary={primary} destructive={danger} onPress={onPress} style={style} />;
});

function TextField({ label, value, onCommit, onDraftChange, hint, hintStyle, secret, multiline, compactMultiline, keyboardType, stacked, labelWidth, labelAlign, controlWidth, suffix, disabled, style }: { label: string; value: string; onCommit: (value: string) => void | Promise<void>; onDraftChange?: (value: string) => void; hint?: string; hintStyle?: StyleProp<TextStyle>; secret?: boolean; multiline?: boolean; compactMultiline?: boolean; keyboardType?: "default" | "numeric"; stacked?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; suffix?: string; disabled?: boolean; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label, onDraftChange);
  return <View style={[styles.formRow, compactStyles.formRow, (stacked || multiline) && styles.formRowStacked, style]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }, (stacked || multiline) && styles.formRowLabelStacked]}>{label}</Text><View style={[styles.formRowControl, compactStyles.formRowControl, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]}><NativeTextField style={[styles.input, compactStyles.input, multiline && styles.textArea, compactMultiline && styles.compactTextArea]} value={field.draft} editable={!disabled} onChangeText={field.onChangeText} onBlur={() => { if (!disabled) void field.commit().catch(() => undefined); }} onSubmitEditing={multiline ? undefined : () => { if (!disabled) void field.commit().catch(() => undefined); }} multiline={multiline} secureTextEntry={secret} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />{hint ? <Text style={[styles.fieldHint, hintStyle]}>{hint}</Text> : null}</View>{suffix ? <Text style={[styles.fieldHint, hintStyle]}>{suffix}</Text> : null}</View>;
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
  }} style={[styles.nativeSecretInput, compactStyles.input, multiline && styles.nativeSecretTextArea, inputMinWidth === undefined ? null : { minWidth: inputMinWidth }]} />{!autoCommit && !setBelow && setTitle ? <NativeButton title={setTitle} compact disabled={busy || status === "saving"} onPress={requestCommit} style={styles.secretActionButton} /> : null}</View>;
}

function NativeSecretField({ label, hint, busy, disabled = false, domain, field, target, plainText = false, autoCommit = false, onSecretState, labelWidth, labelAlign, setTitle, clearTitle, clearDisabled, onClear, actionsBelow }: { label: string; hint?: string; busy: boolean; disabled?: boolean; domain: "providers_models" | "relay_accounts" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; plainText?: boolean; autoCommit?: boolean; onSecretState: (state: SecretState) => void; labelWidth?: number; labelAlign?: "left" | "right"; setTitle?: string; clearTitle?: string; clearDisabled?: boolean; onClear?: () => Promise<void>; actionsBelow?: boolean }): React.JSX.Element {
  const setAction = useRef<() => void>(() => undefined);
  const [saving, setSaving] = useState(false);
  const [resetToken, setResetToken] = useState(0);
  const inputBusy = busy || disabled;
  const handleSetReady = React.useCallback((requestSet: () => void, nextSaving: boolean): void => { setAction.current = requestSet; setSaving(nextSaving); }, []);
  const handleClear = React.useCallback((): void => {
    if (!onClear) return;
    void onClear().then(() => setResetToken((current) => current + 1));
  }, [onClear]);
  return <View style={[styles.formRow, compactStyles.formRow, actionsBelow && styles.formRowSecretStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><View style={[styles.formRowControl, compactStyles.formRowControl]}>{actionsBelow ? <><NativeSecretInputControl label={label} hint={hint} busy={inputBusy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} setBelow onSetReady={handleSetReady} inputMinWidth={110} /><View style={[styles.secretFieldButtons, compactStyles.inlineGap]}>{!autoCommit && setTitle ? <NativeButton title={setTitle} compact disabled={inputBusy || saving} onPress={() => setAction.current()} style={styles.secretFieldButton} /> : null}{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? inputBusy} onPress={handleClear} style={styles.secretFieldButton} /> : null}</View></> : <View style={[styles.secretFieldActions, compactStyles.inlineGap]}><NativeSecretInputControl label={label} hint={hint} busy={inputBusy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} />{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? inputBusy} onPress={handleClear} style={styles.secretActionButton} /> : null}</View>}</View></View>;
}

function ToggleRow({ label, value, onChange, disabled }: { label: string; value: boolean; onChange: (value: boolean) => void; disabled?: boolean }): React.JSX.Element {
  return <View style={[styles.toggleRow, compactStyles.formRow]}><View style={styles.toggleControl}><NativeCheckbox label={label} value={value} onValueChange={onChange} disabled={disabled} style={styles.toggleNativeControl} /></View></View>;
}

function SegmentedField({ label, value, values, onSelect, disabled }: { label: string; value: string; values: Array<string | { value: string; label: string }>; onSelect: (value: string) => void; disabled?: boolean }): React.JSX.Element {
  const translate = useContext(TranslationContext);
  const options = ensureSelectedOption(translate ? assistantSettingOptions(values, translate) : values.map((option) => typeof option === "string" ? { value: option, label: option } : option), value);
  const selectedValue = options.find((option) => option.value === value)?.label ?? value;
  return <View style={[styles.formRow, compactStyles.formRow]}><Text style={styles.formRowLabel}>{label}</Text><NativeSegmentedControl labels={options.map((option) => option.label)} selectedValue={selectedValue} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={[styles.formRowControl, compactStyles.formRowControl]} /></View>;
}

function PickerField({ label, value, values, onSelect, disabled, labelWidth, labelAlign, controlWidth, allowShrink = false, translate }: { label: string; value: string; values: Array<string | AssistantSettingOption>; onSelect: (value: string) => void; disabled?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; allowShrink?: boolean; translate?: Translate }): React.JSX.Element {
  const contextualTranslate = useContext(TranslationContext);
  const optionTranslator = translate ?? contextualTranslate;
  const options = ensureSelectedOption(optionTranslator ? assistantSettingOptions(values, optionTranslator) : values.map((option) => typeof option === "string" ? { value: option, label: option } : option), value);
  const selectedLabel = options.find((option) => option.value === value)?.label ?? value;
  return <View style={[styles.formRow, compactStyles.formRow]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><NativePicker labels={options.map((option) => option.label)} selectedValue={selectedLabel} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={[styles.picker, compactStyles.picker, allowShrink && styles.pickerShrink, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]} /></View>;
}

function RawEditor({ label, domain, document, language, ipc, busy, translate, showReload = true, codexPane = false, onConflict, reloadToken = 0, baselineToken = 0, style }: { label: string; domain: "codex" | "claude"; document: RawEditorDocument; language: "toml" | "json"; ipc: IpcClient; busy: boolean; translate: Translate; showReload?: boolean; codexPane?: boolean; onConflict: RawEditorConflictHandler; reloadToken?: number; baselineToken?: number; style?: StyleProp<ViewStyle> }): React.JSX.Element {
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
      if (resetBaseline) {
        baselineRef.current = descriptor.text;
        setBaseline(descriptor.text);
        if (initializedRef.current) setEditorRenderRevision((value) => value + 1);
      }
      if (!initializedRef.current) setDocumentKey([domain, document].join(":"));
      initializedRef.current = true;
      appliedReloadNonceRef.current = reloadNonce;
      appliedBaselineTokenRef.current = baselineToken;
      registry?.setDirty(fieldId.current, false);
      setStatus("ready");
      setLoading(false);
    }).catch(() => {
      if (!active) return;
      setLoading(false);
      setError(translate("error.coreUnavailable"));
    });
    return () => { active = false; };
  }, [baselineToken, document, domain, ipc, registry, reloadNonce, reloadToken, reset, translate]);

  const reloadEditor = (): void => {
    if (draftRef.current !== stagedTextRef.current || status === "saving") return;
    setReloadNonce((value) => value + 1);
  };
  const reloadDisabled = busy || loading || status === "saving" || draftRef.current !== stagedTextRef.current;
  return <View style={[styles.rawEditor, codexPane && styles.codexRawEditorBase, style]}>
    <View style={[styles.rawEditorHeader, codexPane && styles.codexRawEditorHeader]}>
      <Text style={[styles.fieldLabel, codexPane && styles.codexRawEditorLabel]}>{label}</Text>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        {showReload ? <ActionButton title={translate("menu.reload")} disabled={reloadDisabled} onPress={reloadEditor} /> : null}
      </View>
    </View>
    {documentKey
      ? <View style={styles.rawNativeEditorFrame}>
        <CodeEditorWebView
          documentKey={`${documentKey}:${editorRenderRevision}`}
          value={draft}
          baseline={baseline}
          language={language}
          readOnly={false}
          showDiff
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

function modelProbePresentation(model: UnknownRecord, result: IpcResults["probe"] | undefined, translate: Translate): { compact: string; full: string } {
  const resultRecord = result as UnknownRecord | undefined;
  const probe = resultRecord ?? asRecord(model.probe);
  if (Object.keys(probe).length === 0) {
    return { compact: "", full: "" };
  }
  const surfaces: Array<{ surface: string; available?: boolean; status?: string; original_request?: unknown }> = result?.surfaces
    ?? Object.entries(asRecord(probe.surfaces)).map(([surface, value]) => ({
      surface,
      available: booleanValue(asRecord(value).available),
      status: stringValue(asRecord(value).status),
      original_request: asRecord(value).original_request,
    }));
  const availableSurfaces = surfaces
    .filter((surface) => surface.available === true)
    .map((surface) => probeSurfaceLabel(surface.surface, translate));
  const availabilitySummary = availableSurfaces.length > 0
    ? translate("providers.probeSummaryAvailable", { surfaces: availableSurfaces.join(", ") })
    : translate("providers.probeSummaryUnavailable");
  const summaryRecord = asRecord(probe.summary);
  const statuses = Object.entries(asRecord(summaryRecord.statuses))
    .map(([surface, status]) => `${probeSurfaceLabel(surface, translate)}: ${stringValue(status, "unavailable")}`)
    .join("; ");
  const summary = [availabilitySummary, statuses].filter(Boolean).join("; ");
  const requests = surfaces.map((surface) => ({
    surface: surface.surface,
    status: surface.status ?? "unavailable",
    original_request: surface.original_request ?? {},
  }));
  const compact = availabilitySummary;
  const full = [summary, result?.detail ?? "", translate("providers.probeOriginalRequest", { request: JSON.stringify(requests, null, 2) })].filter(Boolean).join("\n\n");
  return { compact, full };
}

function probeSurfaceLabel(surface: string, translate: Translate): string {
  if (surface === "openai/responses") return translate("providers.responses");
  if (surface === "openai/chat") return translate("providers.chat");
  if (surface === "anthropic") return translate("providers.anthropic");
  return translate("providers.probeNotRun");
}

function IssueList({ issues, translate }: { issues: ValidationSummary["issues"]; translate: Translate }): React.JSX.Element {
  const keyByCode: Record<string, string> = {
    confirmation_required: "error.confirmationRequired",
    invalid_language: "validation.invalid_language",
    invalid_settings: "validation.invalid_settings",
    revision_conflict: "error.revisionConflict",
  };
  return <View style={styles.issueBox}><Text style={styles.sectionTitle}>{translate("common.validationIssues")}</Text>{issues.map((issue, index) => {
    const message = keyByCode[issue.code] ? translate(keyByCode[issue.code]) : issue.message || translate("error.validationFailed");
    return <Text key={`${issue.path}-${index}`} style={styles.issue}>{issue.path ? `${issue.path}: ${message}` : message}</Text>;
  })}</View>;
}

function stringList(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }
function hasBooleanSetting(value: UnknownRecord, key: string): boolean { return typeof value[key] === "boolean"; }
function apiKeyDisplayName(value: unknown, translate: Translate): string {
  const name = stringValue(value);
  if (!name) return translate("common.notAvailable");
  return name === "default" ? translate("providers.defaultKey") : name;
}
function uniqueKeyName(existing: string[]): string { let suffix = 1; let value = `key-${suffix}`; while (existing.includes(value)) { suffix += 1; value = `key-${suffix}`; } return value; }
function splitLines(value: string): string[] { return value.split("\n").map((item) => item.trim()).filter(Boolean); }
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

const serviceProviderStyles = StyleSheet.create({
  workspace: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 },
  unifiedHeader: { minHeight: 36, flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingHorizontal: 4, gap: 8 },
  tabBar: { minHeight: 34, justifyContent: "center", borderBottomWidth: 1, borderBottomColor: systemColors.separator },
  tabs: { width: 280, height: 26, alignSelf: "center" },
  pane: { flex: 1, minWidth: 0, minHeight: 0, paddingHorizontal: 4, gap: 8 },
  intro: { gap: 3, paddingVertical: 2 },
  heading: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  hint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 },
  columns: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP },
  listPane: { width: 270, minWidth: 230, maxWidth: 290, minHeight: 0 },
  table: { flex: 1, minHeight: 0 },
  detailPane: { flex: 1, minWidth: 300, minHeight: 0, paddingHorizontal: 12, paddingVertical: 4, gap: 8 },
  emptyDetail: { flex: 1, minHeight: 160, alignItems: "center", justifyContent: "center", gap: 8 },
  detailHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 },
  detailTitle: { flex: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  status: { flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  activeHint: { flexShrink: 0, color: systemColors.green, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  rule: { height: 1, backgroundColor: systemColors.separator },
  detailLine: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  actions: { flexDirection: "row", alignItems: "center", gap: 6 },
});

// Data-management is a utility window, but its active pane still needs a
// readable rhythm. Keep these adjustments together so the three panes share
// the same inset, helper-copy treatment, and native-preference spacing.
const dataManagementPolishStyles = StyleSheet.create({
  tabBar: { height: 42, minHeight: 42 },
  tabs: { width: 360, height: 28 },
  paneScrollContent: { flexGrow: 1, paddingTop: 14, paddingHorizontal: 12, paddingBottom: 14, gap: 14 },
  importLandingContent: { justifyContent: "center" },
  webDavContent: { flexGrow: 1, gap: 14, paddingTop: 14, paddingHorizontal: 12, paddingBottom: 14 },
  paneIntro: { gap: 3, paddingHorizontal: 2 },
  paneHeading: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  compactText: { fontSize: UI_FONT_SIZE, lineHeight: 16 },
  paneHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  importIntro: { minHeight: 0, paddingHorizontal: 12, paddingVertical: 0, gap: 10 },
  groupBody: { gap: 8, paddingVertical: 6 },
  sectionPicker: { rowGap: 8, paddingVertical: 6 },
  bottomActions: { minHeight: 58, flexShrink: 0, alignItems: "center", paddingHorizontal: 12, paddingTop: 10, paddingBottom: 14, borderTopWidth: 1, borderTopColor: systemColors.separator },
  syncContent: { gap: 10, paddingVertical: 2 },
  webDavFormBody: { gap: 10 },
  webDavFormRows: { width: "100%", maxWidth: 560, gap: 7 },
  webDavSyncArea: { borderTopWidth: 0, paddingTop: 4, marginTop: 2 },
  webDavActionRow: { borderTopWidth: 0, paddingTop: 4, marginTop: 10 },
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
  dataManagementPaneIntro: { paddingHorizontal: 12, paddingVertical: 4, gap: 3 },
  dataManagementPaneHeading: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  dataManagementPaneHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 },
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
  routeTraceInfoText: { color: systemColors.label },
  root: { flex: 1, minWidth: 420, backgroundColor: systemColors.window },
  menuBarHost: { flex: 1 }, error: { margin: 20, color: systemColors.red, fontSize: UI_FONT_SIZE },
  windowSurface: { flex: 1, backgroundColor: systemColors.window }, windowContent: { flexGrow: 1, paddingHorizontal: 16, paddingTop: 12, paddingBottom: 6, gap: 8 }, windowContentFixed: { flex: 1, minHeight: 0 }, providersContent: { paddingBottom: 6, gap: 6 }, providerWizardRouteContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0, gap: 0 }, providerWizardSurface: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.window }, settingsContent: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 0, gap: 6 }, logsContent: { paddingHorizontal: 12, paddingTop: 8, paddingBottom: 0 }, runtimeContent: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 0 }, dataManagementContent: { paddingHorizontal: 16, paddingTop: 8, paddingBottom: 0 }, windowTitleBlock: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 3, gap: 3 }, windowTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, validationText: { color: systemColors.red, fontSize: UI_FONT_SIZE },
  footer: { height: 52, minHeight: 52, flexShrink: 0, flexDirection: "row", alignItems: "center", paddingHorizontal: 16, paddingVertical: 8, gap: 6 }, footerCompact: { height: 48, minHeight: 48, paddingHorizontal: 14, paddingVertical: 8, borderTopWidth: 1, borderTopColor: systemColors.separator, backgroundColor: systemColors.control }, footerBorderless: { borderTopWidth: 0 }, footerStatus: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, flexShrink: 1 }, footerSpacer: { flex: 1 }, footerButtons: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, wideButton: { minWidth: 92 }, runtimeRestoreButton: { minWidth: 120 },
  providerToolbar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardToolbarButton: { minWidth: 104 }, toolbarSpacer: { flex: 1 }, windowTabs: { width: 224, height: 24 }, settingsTabBar: { minHeight: 36, justifyContent: "center", borderBottomWidth: 1, borderBottomColor: systemColors.separator }, settingsTabs: { alignSelf: "flex-start", width: 250 }, windowTab: {}, windowTabSelected: {}, windowTabText: {},
  providerWizardSetupContent: { flex: 1, minHeight: 0, justifyContent: "flex-start", alignItems: "center", paddingHorizontal: 24, paddingTop: 18, paddingBottom: 12 }, providerWizardSetupSurface: { width: "100%", maxWidth: 520, minWidth: 0, gap: 12 }, providerWizardSetupSurfaceModel: { flex: 1, minHeight: 0 }, providerWizardHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerWizardDescription: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 17 }, providerWizardSectionHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardPanelTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerWizardFormSection: { width: "100%", maxWidth: 520, paddingVertical: 0, gap: 10 }, providerWizardModelScroll: { flex: 1, minHeight: 0, width: "100%" }, providerWizardModelScrollContent: { width: "100%", paddingBottom: 8 }, providerWizardModelToolbar: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, providerWizardModelGroup: { gap: 6, paddingTop: 4 }, providerWizardModelGroupHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, providerWizardModelList: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, paddingHorizontal: 8, paddingVertical: 5, gap: 1 }, providerWizardModelCheckbox: { width: "100%", minHeight: 24 }, providerWizardManualModelRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardManualModelCheckbox: { flex: 1, minWidth: 0 }, providerWizardManualModelUpstream: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE }, providerWizardModelSummary: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500", paddingTop: 4 }, providerWizardModeControl: { width: "100%", height: 26, flexShrink: 0 }, providerWizardPicker: { width: "100%", minWidth: 0, height: 26 }, providerWizardInput: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, providerWizardSecretInput: { width: "100%", minHeight: 26 }, providerWizardHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, paddingVertical: 2 }, providerWizardValidation: { color: systemColors.red, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, providerWizardFooter: { minHeight: 46, paddingHorizontal: 20, paddingVertical: 8, flexDirection: "row", alignItems: "center", gap: 6, borderTopWidth: 0, backgroundColor: systemColors.window }, providerWizardFooterSpacer: { flex: 1 }, providerWizardFooterStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, providerWizardFooterActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 },
  routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 },
  providerAuthFields: { minWidth: 0, gap: 4, paddingTop: 2 },
  providerAuthStatusRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  providerAuthStatusLabel: { width: 68, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  providerAuthStatusValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }, providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }, providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }, routeWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, fetchKeyPicker: { width: 170, height: 24, marginRight: 6, flexShrink: 0 }, providerThreePane: { flex: 1, minHeight: 0 }, providerListPane: { width: 154, minWidth: 154, maxWidth: 154, flexGrow: 0, flexShrink: 0 }, modelListPane: { flex: 1, minWidth: 0 }, providerInspectorPane: { minWidth: 280 }, tablePane: { flex: 1, minWidth: 0, gap: 6 }, tablePaneWide: { flex: 1, minWidth: 0 }, tableTitleRow: { height: 24, flexDirection: "row", alignItems: "center" }, tableTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, tableActions: { marginLeft: "auto", flexDirection: "row", gap: 6 }, iconButton: { minWidth: 22, width: 22, minHeight: 22, height: 22, alignItems: "center", justifyContent: "center" }, iconButtonText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, tableHeader: { height: 24, flexDirection: "row", alignItems: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.window }, tableHeaderText: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 6, fontWeight: "500" }, tableScroll: { flex: 1, minHeight: 0, borderWidth: 1, borderTopWidth: 0, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, tableRows: { flexGrow: 1 }, tableRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, tableRowSelected: { backgroundColor: systemColors.control }, tableCellText: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 6 }, providerNameColumn: { flex: 1 }, countColumn: { width: 48, textAlign: "right" }, modelNameColumn: { width: 96 }, modelUpstreamColumn: { flex: 1, minWidth: 112 }, routeModelColumn: { width: 136 }, routeOrderColumn: { width: 48, textAlign: "right" }, routeProviderColumn: { width: 112 }, routeUpstreamColumn: { flex: 1, minWidth: 136 }, tableBottomRow: { minHeight: 26, flexDirection: "row", alignItems: "center" }, nativeProviderTable: { flex: 1, minHeight: 0 }, nativeModelTable: { flex: 1, minHeight: 0 }, nativeRouteTable: { flex: 1, minHeight: 0 }, providerInspector: { width: 280, minWidth: 280, maxWidth: 280, flexGrow: 0, flexShrink: 0 }, providerEditorContent: { flex: 1, minHeight: 0, paddingTop: 3, paddingLeft: 0, paddingRight: 8, paddingBottom: 12, gap: 6 }, providerEditorHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerEditorHeading: { flex: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerReturnToModel: { flexShrink: 1 }, providerEditorSection: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 3, gap: 4 }, providerEnabledRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, providerSourceFields: { minWidth: 0, gap: 4 }, inspectorContent: { paddingTop: 3, paddingLeft: 0, paddingRight: 6, paddingBottom: 12, gap: 6 }, inspectorBody: { gap: 4 }, modelBreadcrumb: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }, breadcrumbProvider: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, breadcrumbSeparator: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorHeading: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorDivider: { height: 1, backgroundColor: systemColors.separator }, inspectorEnabledRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, inspectorEnableControl: { flexShrink: 0 }, orderEditorRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, orderEditorField: { flex: 1, width: undefined }, orderFollowControl: { flexShrink: 0 }, probeSummaryTrigger: { flex: 1, minWidth: 0, minHeight: 22, justifyContent: "center" }, probeSummaryTriggerPressed: { opacity: 0.65 }, probeSummary: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, protocolSettings: { gap: 4 }, protocolHint: { marginLeft: 62, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 15 }, providerKeysEditor: { gap: 4 }, providerKeysHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerKeysHeading: { flex: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerKeyActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 }, providerKeyTable: { width: "100%", height: 112, minHeight: 112, flexShrink: 0 }, providerKeyFields: { minWidth: 0, gap: 4 },
  codexWorkspace: { flex: 1, minHeight: 0 }, codexWorkspaceFrame: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexValidationStatus: { flexShrink: 0, marginHorizontal: 8, fontSize: UI_FONT_SIZE }, settingsMissingMessage: { flexShrink: 0, marginHorizontal: 8, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, codexValidationWarning: { color: systemColors.brown }, codexValidationError: { color: systemColors.red }, codexSplit: { flex: 1, minWidth: 0, minHeight: 0 }, codexStructuredPane: { flex: 1, minWidth: 0, paddingHorizontal: 8 }, codexStructuredScroll: { flex: 1, minWidth: 0, marginTop: 7 }, codexStructuredScrollIndicator: { position: "absolute", width: 0, height: 0 }, codexStructured: { flexGrow: 1, flexShrink: 0, minWidth: SETTINGS_STRUCTURED_CONTENT_MIN_WIDTH, alignSelf: "stretch", gap: 14, paddingLeft: 16, paddingRight: 16 + SETTINGS_STRUCTURED_SCROLLBAR_GUTTER, paddingTop: 10, paddingBottom: 16 }, codexStructuredWithHorizontalScrollbar: { paddingBottom: 32 }, codexRawPane: { flex: 1, flexShrink: 1, minWidth: 320, minHeight: 0, gap: 8, paddingHorizontal: 8, overflow: "hidden" }, codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexRawEditorBase: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0, gap: 5 }, codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0 }, codexRawEditorHeader: { minHeight: 18 }, codexRawEditorLabel: { fontFamily: Platform.select({ macos: "Menlo", windows: "Cascadia Mono", default: "monospace" }), fontWeight: "600" }, codexRawNativeEditor: { minHeight: 0 }, codexRawEditorLoading: { minHeight: 0 }, paneHeading: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, section: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 10, gap: 8 }, sectionHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }, sectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, codexProviderEditor: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 6, backgroundColor: systemColors.control, overflow: "hidden" }, codexProviderToolbar: { minHeight: 42, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12, paddingHorizontal: 10, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: systemColors.separator, backgroundColor: systemColors.window }, codexProviderToolbarTitle: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, codexProviderActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, codexProviderActionButton: { width: 30, minWidth: 30, height: 30, paddingHorizontal: 0 }, codexProviderSplit: { borderWidth: 0, borderRadius: 0 }, split: { flexDirection: "row", flexWrap: "wrap", borderWidth: 1, borderColor: systemColors.separator, minHeight: 150, backgroundColor: systemColors.textBackground }, codexListTable: { flex: 1, minWidth: 260, minHeight: 150 }, pluginEditor: { minHeight: 128, flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", gap: 12 }, pluginTable: { flex: 1, minWidth: 260, minHeight: 128 }, pluginFields: { flex: 1, minWidth: 220, gap: 7 }, masterPane: { width: "36%", minWidth: 220, borderRightWidth: 1, borderColor: systemColors.separator, padding: 8 }, detailPane: { flex: 1, minWidth: 240, padding: 12 }, listRow: { minHeight: 28, paddingHorizontal: 8, paddingVertical: 5 }, listRowSelected: { backgroundColor: systemColors.control }, listText: { flex: 1 },
  runtimeWorkspaceFrame: { flex: 1, minHeight: 0, gap: 8 }, runtimeWorkspace: { padding: 14, gap: 12 }, runtimeScrollSurface: { flex: 1, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, runtimeTwoColumnForm: { flexDirection: "row", flexWrap: "wrap", columnGap: 20, rowGap: 8 }, runtimeOneColumnForm: { flexDirection: "column", flexWrap: "nowrap" }, runtimeField: { minWidth: 486, flexGrow: 1, flexBasis: 486, gap: 4 }, runtimeInputRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, runtimeFieldLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "right" }, runtimeValueSlot: { width: 180, height: 26, flexShrink: 0, justifyContent: "center" }, runtimeValueControl: { width: 180, minWidth: 180, height: 26 }, runtimeBooleanControl: { width: 24, minWidth: 24, height: 24, alignSelf: "flex-start" }, runtimeUnit: { width: 60, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, runtimeActionSlot: { width: 72, minHeight: 26, flexShrink: 0, justifyContent: "center" }, runtimeHelpSlot: { marginLeft: 134, paddingTop: 4, minWidth: 0 }, runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, minWidth: 0 }, runtimeMultilineField: { minWidth: 486, flexGrow: 1, flexBasis: "100%", maxWidth: "100%" }, runtimeMultilineHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, runtimeMultilineLabel: { flex: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, runtimeMultilineHeaderActions: { flexShrink: 0, minHeight: 26, justifyContent: "center" }, runtimeMultilineEditor: { width: "100%", minWidth: 0, height: 108, flex: 1, alignSelf: "stretch" }, runtimeMultilineHelpSlot: { marginLeft: 0, maxWidth: "100%", minWidth: 0, paddingTop: 6, gap: 3 }, runtimeJsonDefaultHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, fontWeight: "600", minWidth: 0 },
  dataManagementWorkspace: { flex: 1, minHeight: 0 }, dataManagementTabBar: { height: 34, minHeight: 34, flexShrink: 0, justifyContent: "center", borderBottomWidth: 1, borderBottomColor: systemColors.separator }, dataManagementTabs: { width: 280, height: 24, alignSelf: "center", flexShrink: 0 }, dataManagementPane: { flex: 1, minHeight: 0 }, dataManagementPaneScrollContent: { paddingTop: 10, paddingHorizontal: 4, paddingBottom: 4, gap: 10 }, dataManagementWebDavPane: { flex: 1, minHeight: 0 }, dataManagementWebDavContent: { gap: 10, paddingTop: 10, paddingHorizontal: 4, paddingBottom: 14 }, dataManagementImportIntro: { width: "100%", minHeight: 72, paddingHorizontal: 12, paddingVertical: 12, justifyContent: "center" }, dataManagementImportFileRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementImportFileLabel: { width: 72, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE }, dataManagementImportFileValue: { flex: 1, minWidth: 0, minHeight: 26, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.textBackground }, dataManagementImportFilePlaceholder: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, dataManagementGroup: { gap: 6 }, dataManagementGroupBody: { gap: 5 }, dataManagementSelectionBar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementSelectionCount: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementToolbarButtons: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 }, dataManagementBottomActions: { minHeight: 26, flexDirection: "row", alignItems: "flex-end", justifyContent: "flex-end", gap: 8 }, dataManagementBottomMessage: { flex: 1, minWidth: 0, gap: 2 }, dataManagementSectionPicker: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 14, rowGap: 2, paddingVertical: 2 }, dataManagementSectionControl: { minWidth: 150, minHeight: 22, justifyContent: "center" }, dataManagementSensitiveHint: { color: systemColors.brown, fontSize: UI_FONT_SIZE, lineHeight: 16, paddingVertical: 5, paddingHorizontal: 7, backgroundColor: Platform.select({ macos: (PlatformColor("systemYellow") as unknown as { withAlphaComponent?: (alpha: number) => string })?.withAlphaComponent?.(0.08) ?? "rgba(255, 204, 0, 0.08)", default: "rgba(255, 204, 0, 0.08)" }), borderRadius: 4, borderWidth: 1, borderColor: Platform.select({ macos: (PlatformColor("systemYellow") as unknown as { withAlphaComponent?: (alpha: number) => string })?.withAlphaComponent?.(0.2) ?? "rgba(255, 204, 0, 0.2)", default: "rgba(255, 204, 0, 0.2)" }) }, dataManagementSensitiveNote: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementSyncContent: { gap: 6 }, dataManagementSyncScope: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementSyncScopeLabel: { width: WEBDAV_FORM_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, dataManagementSyncScopeValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementDirection: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementDirectionLabel: { width: WEBDAV_FORM_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, dataManagementDirectionPicker: { width: 210, height: 24, flexGrow: 0, flexShrink: 0 }, dataManagementStatus: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  webDavForm: { flexGrow: 0, paddingHorizontal: 2 }, webdavFormBody: { gap: 6 }, webdavStateRow: { minHeight: 24, flexDirection: "row", alignItems: "center", justifyContent: "flex-start" }, webdavSyncArea: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 8, marginTop: 2 }, webdavActionRow: { minHeight: 32, flexDirection: "row", alignItems: "center", gap: 8, borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 8, marginTop: 2 }, webdavActionStatus: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, webdavActionSpacer: { flex: 1 }, webdavEnabledControl: { flexGrow: 0, flexShrink: 0, alignSelf: "flex-start" }, webdavStateSpacer: { flex: 1 }, webdavStateStatus: { maxWidth: 180, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, textAlign: "right", lineHeight: 15 }, webdavFormRows: { width: "60%", gap: 5 }, webdavPasswordInput: { width: "100%", minHeight: 26 },
  relayAccountsContent: { paddingBottom: 6, gap: 6 }, logsWindow: { flex: 1, minHeight: 0, gap: 4 }, logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, logFilterRow: { width: 360, minWidth: 220, maxWidth: 360, height: 26, flexDirection: "row", alignItems: "center", gap: 8 }, logToolbarSpacer: { flex: 1, minWidth: 0 }, logActionsRow: { height: 26, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, clearCooldownButton: { minWidth: 96, height: 22 }, toolbarLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, flexShrink: 0 }, logFilterInput: { flex: 1, minWidth: 0, height: 26 }, logsTabs: { width: "100%", minWidth: 0, height: 28, flexShrink: 0, marginTop: 0, marginBottom: 0 }, logTableFrame: { flex: 1, minHeight: 0, minWidth: 0 }, logTable: { flex: 1, minHeight: 0 }, logEmptySurface: { flex: 1, minHeight: 0, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, logEmptyText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, textAlign: "center", paddingHorizontal: 20 }, logInfoBar: { height: 21, minHeight: 21, flexShrink: 0, borderTopWidth: 1, borderColor: systemColors.separator, justifyContent: "center", paddingHorizontal: 4 },
  form: { gap: 6 }, structuredForm: { gap: 6 }, featureGrid: { flexDirection: "row", flexWrap: "wrap", columnGap: 12, rowGap: 4 }, featureGridItem: { flexGrow: 1, flexBasis: 180, minWidth: 180 }, field: { gap: 5, minWidth: 220, flexGrow: 1, flexBasis: 300 }, fieldLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, input: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, textArea: { minHeight: 108, textAlignVertical: "top", fontFamily: "Menlo" }, compactTextArea: { minHeight: 56, maxHeight: 56 }, inputWithAction: { flexDirection: "row", alignItems: "center", gap: 6 }, inputFlex: { flex: 1 }, toggleRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, toggleControl: { flex: 1, minWidth: 0, minHeight: 22, justifyContent: "center" }, toggleNativeControl: { width: "100%", minWidth: 220, minHeight: 22 }, actions: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 4 }, secretFieldActions: { flexDirection: "row", alignItems: "center", gap: 6 }, secretFieldButtons: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 }, secretFieldButton: { flex: 1, minWidth: 0, height: 26 }, nativeSecretControl: { flex: 1, minWidth: 0, minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, nativeSecretInput: { flex: 1, minWidth: 86, minHeight: 26 }, nativeSecretSetButton: { minWidth: 42, height: 26 }, action: {}, actionPrimary: {}, actionDanger: {}, actionDisabled: {}, actionText: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, actionTextPrimary: {}, actionTextDanger: {}, tabStrip: { flexDirection: "row", flexWrap: "wrap", gap: 6 }, tab: {}, tabSelected: {}, inlineMeta: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 6 }, rawEditor: { flex: 1, minHeight: 180, gap: 4 }, rawEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, rawNativeEditorFrame: { flex: 1, minHeight: 160, position: "relative" }, rawNativeEditor: { flex: 1, minHeight: 160 }, rawEditorOverlay: { position: "absolute", left: 0, right: 0, top: 0, bottom: 0, justifyContent: "center", alignItems: "center", gap: 8, paddingHorizontal: 12, backgroundColor: systemColors.textBackground }, rawEditorLoading: { flex: 1, minHeight: 160, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, infoPair: { gap: 2, minWidth: 160 }, rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 6 }, logRecords: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, maxHeight: 360, overflow: "scroll", padding: 10, gap: 6 }, logRecord: { color: systemColors.label, fontFamily: "Menlo", fontSize: UI_FONT_SIZE }, empty: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, paddingVertical: 12 }, result: { color: systemColors.green, fontSize: UI_FONT_SIZE }, warning: { color: systemColors.brown, fontSize: UI_FONT_SIZE, backgroundColor: systemColors.control, padding: 8, borderRadius: 4 }, issueBox: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.control, padding: 12, gap: 5 }, issue: { color: systemColors.red, fontSize: UI_FONT_SIZE }, cardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, cardHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, marginTop: 2 },
  nativeSecretMultilineControl: { alignItems: "flex-start", minHeight: 108 },
  nativeSecretTextArea: { minHeight: 108, height: 108, alignSelf: "stretch" },
  secretActionButton: { width: 64, minWidth: 64, height: 26, flexShrink: 0 },
  formRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, formRowStacked: { alignItems: "flex-start" }, formRowSecretStacked: { alignItems: "flex-start" }, formRowLabel: { width: 112, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, formRowLabelStacked: { paddingTop: 4 }, formRowControl: { flex: 1, minWidth: 0, gap: 3 }, picker: { flex: 1, minWidth: 180, height: 26 }, pickerShrink: { minWidth: 0 },
});
