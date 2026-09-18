import React, { createContext, useCallback, useEffect, useMemo, useRef, useState, useContext } from "react";
import { AppState, FlatList, Image, Platform, PlatformColor, Pressable, ScrollView, StyleSheet, Text, View, type HostInstance, type NativeScrollEvent, type NativeSyntheticEvent, type ScrollViewProps, type StyleProp, type TextStyle, type ViewStyle } from "react-native";
import { createTranslator } from "../i18n";
import { assistantSettingOptions, localizeCodexValidationMessage, type AssistantSettingOption } from "../i18n/assistantSettingsI18n";
import { runtimeCategoryLabel, runtimeFieldHelp, runtimeFieldLabel, runtimeOptionLabel, runtimeUnitLabel } from "../i18n/runtimeSettingsI18n";
import { canonicalWindowRoute, isSettingsPaneRoute, isSettingsShellRoute, LOG_TABS, routeMenuActions, ROUTES, SETTINGS_PANES, SETTINGS_PANE_PRESENTATION } from "../routes";
import { NativeButton, NativeCheckbox, NativePersistentScrollIndicator, NativePicker, NativeSecureTextInput, NativeSegmentedControl, NativeTable, NativeTextField, NativeToggle } from "./NativeControls";
import { CODE_EDITOR_HTML, CodeEditorWebView } from "./code-editor/CodeEditorWebView";
import {
  accountDisplayName,
  accountsFromSnapshot,
  ApiKeyCreateDialog,
  DependencyPolicyDialog,
  NativeFormRow,
  NativeWizardProgress,
  normalizeRelayOrigin,
  providedKeyRows,
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
import { isAssistantEditorOpen, isProviderWizardOpen, setAssistantEditorOpen, setProviderWizardOpen, subscribeProviderWizard } from "./providerWizardGate";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";
import type {
  AppRoute,
  CodexModelSelection,
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
type EditableDiskDomain = "codex" | "claude" | "providers_models" | "runtime" | "webdav";
type RawEditorConflictResolution = "reload" | "keep";
type AssistantSettingsDomain = "codex" | "claude";
type RawEditorConflictHandler = (domain: AssistantSettingsDomain, document: RawEditorDocument) => Promise<RawEditorConflictResolution>;
type RawEditorDocument = "config" | "auth" | "settings" | "desktop" | "developer";
type AssistantFileTarget = {
  domain: AssistantSettingsDomain;
  document: RawEditorDocument;
  language: "toml" | "json";
  label: string;
};
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

/** The merged workspace shows every provider kind; this drives row labels. */
type ProviderKind = "relay" | "openai" | "claude" | "apiKey";

function providerKind(provider: UnknownRecord): ProviderKind {
  if (providerAuthKind(provider) === "openai_login") return "openai";
  if (providerAuthKind(provider) === "claude_login") return "claude";
  return stringValue(provider.provider_type, "custom") === "relay" ? "relay" : "apiKey";
}

function officialStatusLabel(status: ProviderAuthStatus, translate: Translate): string {
  return status === "signed_in" ? translate("providers.authStatusSignedIn")
    : status === "authorizing" ? translate("providers.authStatusAuthorizing")
      : status === "expired" ? translate("providers.authStatusExpired")
        : status === "error" ? translate("providers.authStatusError")
          : status === "unsupported" ? translate("providers.authStatusUnsupported")
            : translate("providers.authStatusSignedOut");
}

function providerKindLabel(kind: ProviderKind, translate: Translate): string {
  return kind === "relay"
    ? translate("providers.type.relay")
    : kind === "openai"
      ? translate("providers.type.openai")
      : kind === "claude"
        ? translate("providers.type.claude")
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
const ROUTE_TRACE_REQUEST_ROW_HEIGHT = 70;
const ROUTE_TRACE_SCROLL_IDLE_MS = 150;
const ROUTE_TRACE_TIMELINE_MIN_WIDTH = 400;
const ROUTE_TRACE_SCROLLBAR_MIN_THUMB_WIDTH = 32;
const WEBDAV_FORM_LABEL_WIDTH = 108;
const SETTINGS_STRUCTURED_CONTENT_MIN_WIDTH = 360;
const SETTINGS_STRUCTURED_SCROLLBAR_GUTTER = 18;
// The settings window draws a full-height sidebar behind a transparent title
// bar, so the shared sidebar content starts below the traffic lights while the
// window material itself extends to the top edge.
const SETTINGS_TITLEBAR_INSET = Platform.OS === "macos" ? 32 : 0;
const COLUMN_GAP = 8;
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

export interface YoungRouterAppProps {
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
  // Match the key the way the model editor does: by slot id first, then by the
  // key name the model carries.  A stored id can move (Core re-derives a
  // renamed key's slot when it reads the document back), and matching by id
  // alone left the list under "undefined key" while the editor showed the key.
  const keyStates = providerKeyStates(provider);
  const key = keyStates.find((entry) => entry.id === stringValue(model.provider_key_id))
    ?? keyStates.find((entry) => entry.name === stringValue(model.api_key_name));
  return key ? keyName?.(key.id, key.name) ?? key.name : displayLabel(model.api_key_name, translate("providers.undefinedKey"));
}

function isApplyResult(value: unknown): value is IpcResults["apply"] {
  const result = asRecord(value);
  return (result.status === "applied" || result.status === "partial" || result.status === "failed")
    && typeof result.completed_operations === "number"
    && typeof result.pending_operations === "number"
    && Array.isArray(result.issues);
}

function applyResultMessage(result: IpcResults["apply"], translate: Translate, appliedKey?: string | null): string {
  if (result.status === "partial") return translate("relay.applyPartial", { completed: result.completed_operations, pending: result.pending_operations });
  if (result.status === "failed") return translate("relay.applyFailed", { pending: result.pending_operations });
  return translate(appliedKey ?? "common.applied");
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
  const known = keyByCode[code];
  if (known) return translate(known);
  // Surface the Core's already-sanitized failure detail instead of a generic
  // "error" so an actionable cause (for example a rejected value) is visible.
  const detail = stringValue(asRecord(reason).message).trim();
  if (detail && detail.length <= 160) return detail;
  return translate("error.generic");
}

function isEditorCapabilityConflict(reason: unknown): boolean {
  const code = stringValue(asRecord(reason).code);
  return code === "invalid_editor" || code === "revision_conflict";
}

function isRevisionConflict(reason: unknown): boolean {
  return stringValue(asRecord(reason).code) === "revision_conflict";
}

function isRevisionRetryableAction(type: string): boolean {
  const normalized = type.replace(/[.-]/g, "_").toLowerCase();
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
    || normalized === "service_autostart_disable";
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
  return value === "providers_models" || value === "codex" || value === "claude" || value === "runtime" || value === "webdav";
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

function containsPrivateMarker(value: unknown): boolean {
  if (typeof value === "string") return value.includes("configured") || value.includes("<private-path>");
  if (Array.isArray(value)) return value.some(containsPrivateMarker);
  return Object.values(asRecord(value)).some(containsPrivateMarker);
}

function editableRecord(value: UnknownRecord): UnknownRecord {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => !containsPrivateMarker(item)));
}

export function YoungRouterApp({ ipc, native, translate: hostTranslate, initialSnapshot, routeRequest, routeRequestSequence, logTabRequest, nativeAction, isPrimaryHost = true, isWindowManagerHost = false }: YoungRouterAppProps): React.JSX.Element {
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
      appTitle: translate("app.title"), about: translate("status.about"), autoStart: translate("status.autoStart"), serviceUnavailable: translate("error.coreUnavailable"),
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
      routeCodexSettings: translate("status.codex"), routeClaudeSettings: translate("card.claudeSettings"),
      routeRuntimeSettings: translate("card.runtimeSettings"),
      routeDataManagement: translate("card.dataManagement"), routeProviderWizard: translate("providers.wizard.title"), routeLogs: translate("card.logs"),
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

  // The provider wizard is the exempted sub-sheet: hold immediate apply while
  // it is mounted so its partial provider/key/model steps are not written and
  // reloaded before the user finishes. The provider pane applies the finished
  // draft as soon as the sheet closes.
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
        : <RouteSurface route={route} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={receiveSnapshot} onNavigate={setRoute} onClose={() => setRoute(route === "provider-wizard" && Platform.OS === "windows" ? "providers-models" : "home")} />) : null}
      {!error && route === "home" ? <View style={styles.menuBarHost} /> : null}
    </View>
  );
}

/**
 * The single settings window. A native source list on the left selects one of
 * the shared route surfaces on the right. Every pane commits its staged
 * configuration as it changes, so there is no route-level Apply/Close footer;
 * only the provider wizard sheet keeps explicit Apply/Close actions.
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
  const flushActivePane = useRef<(() => Promise<boolean>) | undefined>(undefined);
  const registerFlush = useCallback((flush?: () => Promise<boolean>): void => {
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
  const [assistantDialog, setAssistantDialog] = useState<React.ReactNode>(null);
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
      <View style={styles.settingsSidebarDivider} />
      <NativeTable
        columns={[{ label: "", width: 186 }]}
        rows={paneRows}
        selectedKey={pane}
        striped={false}
        compact={false}
        framed={false}
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
      <View style={styles.settingsPaneDivider} />
      <View style={[styles.settingsDetailBody, (pane === "runtime-settings" || pane === "data-management") && styles.settingsDetailBodyBare]}>
        <View style={styles.settingsDetailPane}>
          <RouteSurface key={pane} route={pane} shell windowRoute={windowRoute} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={onSnapshot} onNavigate={onNavigate} onClose={onClose} onRegisterFlush={registerFlush} onRegisterAssistantDialog={setAssistantDialog} />
        </View>
      </View>
    </View>
    {assistantDialog}
  </View>;
}

function WindowTitle({ title, validation }: { title: string; validation?: string }): React.JSX.Element {
  return <View style={styles.windowTitleBlock}><Text style={styles.windowTitle}>{title}</Text>{validation ? <Text style={styles.validationText}>{validation}</Text> : null}</View>;
}

/** Immediate apply is blocked while a sub-sheet with explicit actions is open. */
function useImmediateApplyBlocked(): boolean {
  const [blocked, setBlocked] = useState<boolean>(() => isProviderWizardOpen() || isAssistantEditorOpen());
  useEffect(() => subscribeProviderWizard(() => setBlocked(isProviderWizardOpen() || isAssistantEditorOpen())), []);
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

function RouteSurface({ route, shell = false, windowRoute, snapshot, ipc, native, translate, logTabRequest, nativeAction, onSnapshot, onNavigate, onClose, onRegisterFlush, onRegisterAssistantDialog }: { route: AppRoute; shell?: boolean; windowRoute?: AppRoute; snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; translate: Translate; logTabRequest?: LogTab; nativeAction?: { id: string; sequence: number }; onSnapshot: (next: CoreSnapshot) => void; onNavigate: (route: AppRoute) => void; onClose: () => void; onRegisterFlush?: (flush?: () => Promise<boolean>) => void; onRegisterAssistantDialog?: (node: React.ReactNode) => void }): React.JSX.Element {
  const settingsRoute = isAssistantSettingsRoute(route);
  // The Codex and Claude settings routes are aliases for one shared surface.
  // Both domains stay visible and staged together, so there is no active tab
  // that can hide the other assistant's draft.
  const domain = settingsRoute ? undefined : domainForRoute(route);
  const [busy, setBusy] = useState(false);
  const [webDavOperationBusy, setWebDavOperationBusy] = useState(false);
  const [result, setResult] = useState<string>();
  const [issues, setIssues] = useState<ValidationSummary["issues"]>([]);
  const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);
  const [settingsRawBaselineToken, setSettingsRawBaselineToken] = useState(0);
  const [activeAssistantFile, setActiveAssistantFile] = useState<AssistantFileTarget>();
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
  const pendingFields = useRef(new Map<symbol, PendingField>());
  // The pending-field dirty set lives in a ref for synchronous reads, but a
  // state counter makes it reactive: the immediate-apply effect must re-run
  // when the last dirty field clears, otherwise a single text edit would wait
  // for an unrelated change before it reaches Core.
  const [pendingFieldRevision, forcePendingFieldDirtyRender] = useState(0);
  const pendingFieldDirtyIdsRef = useRef<ReadonlySet<symbol>>(new Set());
  useEffect(() => {
    if (!settingsRoute) setActiveAssistantFile(undefined);
  }, [settingsRoute]);
  // Relay CRUD and linked imports are one coordinated draft. The relay route
  // therefore applies both domains together whenever either side is dirty.
  const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {
    if (settingsRoute) {
      return (["codex", "claude"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);
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
        // An explicit caller message wins for the applied case so instant
        // applies can say "Saved" instead of the generic "Applied".
        publishResult(applyResultMessage(value, translate, message));
      } else {
        setIssues([]);
        publishResult(message === null ? undefined : translate(message));
      }
      if (refreshAfter) await refresh();
    } catch (reason: unknown) {
      publishResult(errorMessage(reason, translate));
      // A rejected Apply only says the draft is invalid. Ask the domain which
      // entries failed so the pane can name them instead of leaving the user
      // with a message that points at nothing.
      if (stringValue(asRecord(reason).code) === "validation_failed" && (domain === "providers_models" || route === "providers-models")) {
        try {
          const summary = await ipc.validate("providers_models");
          setIssues(summary.issues);
        } catch {
          // Keep the Apply failure; naming the issues is best-effort.
        }
      }
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
  // An ordinary route action carries its settings domain. A Core-level service
  // action (the launch-at-login preference) has none, so `null` explicitly
  // requests a domain-less action envelope; an omitted argument still means
  // "this route's domain" and stays an error when the route has none.
  const enqueueDispatch = (type: string, payload: UnknownRecord = {}, targetDomain: ConfigDomain | null | undefined = domain): Promise<IpcResults["dispatch"]> => {
    const actionDomain = targetDomain ?? undefined;
    if (!actionDomain && targetDomain !== null) return Promise.reject(new Error("A settings domain is required"));
    const queued = dispatchQueue.current.catch(() => undefined).then(async () => {
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
    });
    dispatchQueue.current = queued.then(() => undefined, () => undefined);
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
  // keeps their draft local and only reaches dispatch on blur/submit/Apply.
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
  const addOfficialAccount = async (kind: ServiceProviderKind): Promise<string> => {
    const currentProviders = serviceProviderRecords(latestSnapshot.current ?? snapshot);
    const name = nextServiceProviderName(currentProviders, kind);
    const next = await dispatchWithOutcome("service_provider.add", { kind, name }, "providers_models");
    if (!next) throw new Error("service_provider.add was rejected");
    const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
    const summaryID = stringValue(summary.provider_id);
    const added = serviceProviderRecords(next).find((provider) => {
      const displayName = stringValue(provider.display_name, stringValue(provider.name)).trim();
      return displayName === name;
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
  const openAssistantFile = (target: AssistantFileTarget): void => {
    // Native button actions can run before the focused AppKit field emits its
    // blur. Commit React-owned text drafts first so the first editor render
    // already includes every structured UI change for that document.
    void flushAssistantEditorFields()
      .then(() => setActiveAssistantFile(target))
      .catch((reason: unknown) => setResult(errorMessage(reason, translate)));
  };
  const hasPendingFieldEdits = useCallback((): boolean => pendingFieldDirtyIdsRef.current.size > 0
    || [...pendingFields.current.values()].some((field) => field.isDirty?.() === true), []);
  // Actions can finish with a fresh Core snapshot before the parent route has
  // rendered it into `snapshot`. Keep the footer and close decision on the
  // same newest projection so Apply cannot look enabled while Close sees a
  // clean draft (or vice versa). The ref is populated directly by IPC reads,
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
  const apply = (options?: { silent?: boolean; message?: string; keepControlsEnabled?: boolean }): Promise<void> => {
    if ((!settingsRoute && !domain) || domain === "logs") return Promise.resolve();
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
      if (domains.includes("codex") || domains.includes("claude")) {
        setSettingsRawBaselineToken((current) => current + 1);
      }
      // Core restarts the managed proxy for providers_models and runtime
      // applies on its own thread, so the pane never waits for the restart
      // and a later edit is not stuck behind it.
      if (diskConflicts.length > 0) setKeptDiskGeneration({});
      return result;
    }, options?.silent ? null : (options?.message ?? "common.applied"), options?.keepControlsEnabled === true);
  };
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
  const flushAndApply = useRef<() => Promise<boolean>>(() => Promise.resolve(true));
  const invalidCloseNotice = useRef(false);
  flushAndApply.current = async (): Promise<boolean> => {
    await flushPendingFields();
    if (hasPendingFieldEdits()) {
      invalidCloseNotice.current = true;
      setResult(translate("runtime.fixInvalidBeforeClose"));
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
  }, "webdav.probe");
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
  const requestClose = async (): Promise<void> => {
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
        title: translate("status.close"),
        message: translate("common.discarded"),
        confirmLabel: translate("status.close"),
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
    // active pane; only the provider wizard sheet closes itself here.
    if (shell) return;
    if (nativeAction?.id !== `request-close-${route}` && nativeAction?.id !== `request-close-${canonicalWindowRoute(route)}`) return;
    requestClose();
  }, [nativeAction?.sequence]);
  const definition = ROUTES.find((item) => item.id === route);
  const windowTitle = settingsRoute
    ? translate("status.codex")
    : translate(definition?.titleKey ?? "app.title");
  const providerWizardProviders = useMemo(() => {
    const state = domainState(snapshot, "providers_models");
    const details = asRecords(state.providers);
    const candidates = details.length > 0 ? details : (snapshot?.providers_models.providers ?? []).map(providerRecord);
    return candidates;
  }, [snapshot]);
  const providerWizardRelaySources = useMemo(() => relaySourcesFromSnapshot(snapshot), [snapshot]);
 const providerWizardRelayStations = useMemo(() => relayStationsFromSnapshot(snapshot), [snapshot]);
  const detectRelayType = useCallback(async (origin: string): Promise<RelayType | undefined> => {
    const staged = await enqueueDispatch("account.detect_type", { origin }, "relay_accounts");
    revision.current = staged.revision;
    const next = await refresh();
    const detected = asRecord(next.action_summaries?.relay_accounts).detected_type;
    return detected === "newapi" || detected === "sub2api" ? detected : undefined;
  }, [enqueueDispatch, refresh]);
  const refreshRelayResources = useCallback(async (accountId: string): Promise<"ready" | "unavailable"> => {
    const staged = await enqueueDispatch("resources.refresh", { account_id: accountId }, "relay_accounts");
    revision.current = staged.revision;
    // The refresh reports its own projection as the action summary; the
    // dispatch result itself only carries the revision and that summary.
    return asRecord(staged.action_summary).resource_status === "ready" ? "ready" : "unavailable";
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
  }), [commitRelayMetadata, detectRelayType, refresh, refreshRelayResources, relayApiKeyActions]);
  // The assistant file editor is a window-level sheet: RouteSurface owns its
  // state, but the shell mounts the node at the window root so it dims and
  // covers the whole settings window instead of being clipped by the detail
  // column it was opened from.
  const assistantDialogProps = useRef<React.ComponentProps<typeof AssistantFileEditorDialog>>(undefined as never);
  assistantDialogProps.current = {
    target: activeAssistantFile,
    ipc,
    busy,
    translate,
    onEditorConflict: resolveRawEditorConflict,
    rawReloadToken: settingsRawReloadToken,
    rawBaselineToken: settingsRawBaselineToken,
    syncRevision: activeAssistantFile ? snapshot?.revision : undefined,
    onFlushPendingFields: flushPendingFields,
    onSave: async () => {
      await flushAssistantEditorFields();
      await apply({ silent: true });
    },
    onClose: () => setActiveAssistantFile(undefined),
  };
  useEffect(() => {
    if (!shell || !onRegisterAssistantDialog) return undefined;
    // The editor sheet owns an explicit Save action; hold immediate apply
    // while it is open so the pane cannot write a half-finished document.
    setAssistantEditorOpen(activeAssistantFile !== undefined);
    onRegisterAssistantDialog(activeAssistantFile ? <AssistantFileEditorDialog {...assistantDialogProps.current} /> : null);
    return () => {
      setAssistantEditorOpen(false);
      onRegisterAssistantDialog(null);
    };
  }, [activeAssistantFile, busy, onRegisterAssistantDialog, settingsRawBaselineToken, settingsRawReloadToken, shell, snapshot?.revision]);

  return <TranslationContext.Provider value={settingsRoute ? translate : undefined}><PendingFieldContext.Provider value={fieldRegistry}><View style={styles.windowSurface}>
    {!shell && route !== "providers-models" && route !== "logs" && route !== "provider-wizard" && route !== "data-management" ? <WindowTitle title={windowTitle} validation={issues.length > 0 ? `${issues.length} ${translate("common.validationIssues")}` : undefined} /> : null}
    {route === "providers-models" || route === "provider-wizard" || settingsRoute || route === "logs" || route === "runtime-settings" || route === "data-management" || route === "general-settings" ? <View style={[styles.windowContent, compactStyles.windowContent, styles.windowContentFixed, route === "providers-models" && styles.providersContent, route === "provider-wizard" && styles.providerWizardRouteContent, settingsRoute && styles.settingsContent, route === "logs" && styles.logsContent, route === "runtime-settings" && styles.runtimeContent, route === "data-management" && styles.dataManagementContent]}>
    {route === "providers-models" ? <ProviderWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onStatus={setResult} onSecretState={onSecretState} applyProbedSurface={applyProbedSurface} onOpenWizard={() => { if (Platform.OS === "windows") onNavigate("provider-wizard"); native.window.open("provider-wizard"); }} relay={relayBridge} addOfficialAccount={addOfficialAccount} onActivateAndRestart={activateProviderAndRestart} /> : null}
    {route === "provider-wizard" ? <ProviderSetupWizard snapshot={snapshot} native={native} providers={providerWizardProviders} relaySources={providerWizardRelaySources} relayStations={providerWizardRelayStations} busy={busy} translate={translate} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onStatus={setResult} onClose={closeRoute} relay={relayBridge} addOfficialAccount={addOfficialAccount} /> : null}
    {settingsRoute ? <AssistantSettingsWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onOpenFile={openAssistantFile} /> : null}
    {route === "logs" ? <LogsWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onStatus={setResult} requestedTab={nativeAction?.id === "open-recovery" ? "recovery" : logTabRequest} requestedTabKey={nativeAction?.sequence ?? 0} /> : null}
    {route === "general-settings" ? <GeneralWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} dispatch={dispatch} dispatchServiceAction={enqueueServiceDispatch} translate={translate} onStatus={setResult} onSnapshot={onSnapshot} /> : null}
    {route === "runtime-settings" ? <RuntimeWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} /> : null}
    {route === "data-management" ? <DataManagementWorkspace snapshot={snapshot} busy={busy} webDavOperationBusy={webDavOperationBusy} statuses={dataManagementStatuses} translate={translate} dispatch={dispatchDataManagement} onSecretState={onSecretState} onFlushPendingFields={flushPendingFields} onTabSwitchError={(tab, reason) => setDataManagementStatuses((current) => ({ ...current, [tab]: errorMessage(reason, translate) }))} onInspectImport={inspectImportDataManagement} onImport={importDataManagement} onConfirmImportReplace={confirmImportDraftReplacement} onExport={exportDataManagement} onProbeWebDav={probeWebDav} onSyncWebDav={syncWebDav} /> : null}
    {issues.length > 0 ? <IssueList issues={issues} translate={translate} /> : null}
    </View> : null}
    {/* One permanent status bar per pane window: the last result replaces the
        idle Ready label, so the strip never appears or disappears mid-action
        and the pane below it keeps a stable height. */}
    {shell ? <View style={styles.routeStatusBar}><Text numberOfLines={2} style={styles.routeStatusText}>{result ?? translate("common.ready")}</Text></View> : null}
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

const INLINE_MODEL_LIMIT = 5;
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

function ProviderSetupWizard({ snapshot, native, providers, relaySources, relayStations, busy, translate, dispatchWithOutcome, onSecretState, onStatus, onClose, relay, addOfficialAccount }: { snapshot?: CoreSnapshot; native: NativeLeafAdapter; providers: UnknownRecord[]; relaySources: RelaySourceOption[]; relayStations: RelayStationOption[]; busy: boolean; translate: Translate; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>; onSecretState: (state: SecretState) => void; onStatus: (status?: string) => void; onClose: () => void; relay: RelayWorkspaceBridge; addOfficialAccount: (kind: ServiceProviderKind) => Promise<string> }): React.JSX.Element {
  type WizardStep = "provider" | "keys" | "model";
  type WizardType = "api" | "openai" | "claude";
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
  // Core-side name of the key slot a fresh provider received.  The wizard
  // presents that slot as「添加新的 API 密钥」instead of selecting it, so
  // the picker never shows the generated word as a pre-selected key.
  const [pendingNewKeyName, setPendingNewKeyName] = useState<string | undefined>(undefined);
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
  const shownChallenge = useRef<Record<string, string>>({});
  const setLoginFeedbackMessage = (message: string | undefined): void => {
    loginFeedback.current = message;
    forceLoginFeedbackRender((value) => value + 1);
  };

  const isLoginType = providerType === "openai" || providerType === "claude";
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
  const activeKeySelection = keySelection || (pendingNewKeyName ? PROVIDER_WIZARD_NEW_KEY : keyOptions[1]?.value ?? PROVIDER_WIZARD_NEW_KEY);
  const selectedKeyChoice = keyChoices.find((choice) => choice.id === activeKeySelection);
  const selectedProvidedChoice = providedChoices.find((source) => `relay:${relaySourceSelectionID(source)}` === providedKeySelection) ?? providedChoices[0];
  const manualSelectedKeyName = selectedKeyChoice?.name ?? (activeKeySelection === PROVIDER_WIZARD_NEW_KEY ? keyName.trim() : "");
  const selectedKeyReady = Boolean(selectedKeyChoice && selectedKeyChoice.kind === "independent" && selectedKeyChoice.state?.configured) || keyReady;
  const modelCandidates = useMemo(() => {
    const values = [...(selectedProvidedChoice?.models ?? []), ...fetchedModelCandidates];
    return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
  }, [fetchedModelCandidates, selectedProvidedChoice?.models]);
  const providerOptions = providers
    .filter((entry) => isLoginType ? providerKind(entry) === providerType : providerKind(entry) === "apiKey" || providerKind(entry) === "relay")
    .map((entry) => {
      const id = editorIdentifier(entry);
      return { value: id, label: stringValue(entry.display_name, stringValue(entry.name, id)) };
    });
  const officialStatus = isLoginType && selectedProvider ? providerAuthStatus(selectedProvider) : "signed_out";
  const modelChoicesForOfficial = useMemo(() => (isLoginType && selectedProvider ? asRecords(selectedProvider.models).map(modelRecord) : []), [isLoginType, selectedProvider]);
  const stepItems: Array<{ id: WizardStep; title: string }> = [
    { id: "provider", title: translate("providers.wizard.stepProvider") },
    { id: "keys", title: translate("providers.wizard.stepKeys") },
    { id: "model", title: translate("providers.wizard.stepModel") },
  ];

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
        html: CODE_EDITOR_HTML,
      });
    }
  };
  useEffect(() => {
    if (!isLoginType || officialStatus !== "authorizing" || !providerID) return;
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
    if (!selectedProvider || processing || pendingNewKeyName || keySelection === PROVIDER_WIZARD_NEW_KEY || keyOptions.some((choice) => choice.value === keySelection)) return;
    setKeySelection(keyOptions[1]?.value ?? PROVIDER_WIZARD_NEW_KEY);
    setKeyReady(false);
  }, [keyOptions, keySelection, pendingNewKeyName, processing, selectedProvider]);

  // Seed the editable key-name field once per key selection: a new key gets
  // a random word, an existing independent key gets its current name.
  const keyNameSeedSelection = useRef<string>("");
  useEffect(() => {
    if (step !== "keys" || keyNameSeedSelection.current === activeKeySelection) return;
    if (activeKeySelection === PROVIDER_WIZARD_NEW_KEY) {
      keyNameSeedSelection.current = activeKeySelection;
      setKeyName(pendingNewKeyName ?? randomKeyName([]));
      return;
    }
    if (selectedKeyChoice && selectedKeyChoice.kind === "independent") {
      keyNameSeedSelection.current = activeKeySelection;
      setKeyName(selectedKeyChoice.name);
    }
  }, [activeKeySelection, pendingNewKeyName, selectedKeyChoice, step]);

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
    setPendingNewKeyName(undefined);
    setValidation("");
    setKeyPath(value === "api" ? "manual" : "login");
    setSignedInAccountID(undefined);
    setProvidedKeySelection("");
  };
  const chooseExistingProvider = (value: string): void => {
    setProviderMode("existing");
    setProviderSelection(value);
    setKeySelection("");
    keyNameSeedSelection.current = "";
    setPendingNewKeyName(undefined);
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
    setKeySelection("");
    setKeyName("");
    setKeyReady(false);
    setValidation("");
    setSignedInAccountID(undefined);
    setProvidedKeySelection("");
  };
  // Tracks the last auto-suggested provider name so the name field follows
  // the URL as it is completed ("api" → "openai" for api.openai.com) until
  // the user edits the name manually.
  const lastSuggestedProviderName = useRef("");
  const updateProviderBaseURL = (value: string): void => {
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
    if (keyPath !== "manual") return;
    const keyChoice = selectedKeyChoice;
    const keyNameValue = manualSelectedKeyName;
    if (!providerID || !keyNameValue) return;
    const request = ++modelFetchRequest.current;
    setModelFetchState("loading");
    setFetchedModelCapabilities({});
    const relaySource = keyChoice?.kind === "relay" ? keyChoice.source : undefined;
    const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";
    const payload = relaySource ? {
      provider_id: providerID,
      station_id: relaySource.stationID,
      account_id: relaySource.accountID,
      resource_id: relaySource.resourceID,
    } : {
      provider_id: providerID,
      api_key_name: keyNameValue,
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
  const createProvider = async (): Promise<boolean> => {
    if (providerType === "openai" || providerType === "claude") {
      setProcessing(true);
      try {
        const providerNewID = await addOfficialAccount(providerType === "claude" ? "claude_login" : "openai_login");
        setProviderMode("existing");
        setProviderSelection(providerNewID);
        setValidation("");
        return true;
      } catch {
        setValidation(translate("relay.operationFailed"));
        return false;
      } finally {
        setProcessing(false);
      }
    }
    const name = providerName.trim();
    const baseURL = providerBaseURL.trim();
    if (!name || !baseURL) {
      setValidation(translate("providers.wizard.required"));
      return false;
    }
    if (providerNameExists(providers, name)) {
      setProviderName("");
      setProviderBaseURL("");
      setValidation(translate("providers.wizard.duplicateName"));
      return false;
    }
    const existingIDs = new Set(providers.map(editorIdentifier));
    setProcessing(true);
    try {
      const initialKeyName = randomKeyName([]);
      const next = await dispatchWithOutcome("provider.add", { provider: {
        name,
        api_base: baseURL,
        auth_kind: "api_key",
        enabled: true,
        models: [],
        create_default_api_key: true,
        initial_api_key_name: initialKeyName,
      } });
      if (!next) return false;
      const nextState = domainState(next, "providers_models");
      const nextProviders = asRecords(nextState.providers).length > 0
        ? asRecords(nextState.providers)
        : (next.providers_models.providers ?? []).map(providerRecord);
      const added = nextProviders.find((entry) => !existingIDs.has(editorIdentifier(entry)))
        ?? nextProviders.find((entry) => stringValue(entry.name).trim() === name);
      if (!added) return false;
      setPendingNewKeyName(initialKeyName);
      setKeyName(initialKeyName);
      setProviderMode("existing");
      setProviderSelection(editorIdentifier(added));
      setValidation("");
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
  // Embedded station sign-in: the vendor's base URL is the station origin.
  // After the login the vendor binds to the station so its provided keys
  // become selectable.
  const beginRelayLogin = async (): Promise<void> => {
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
      const status = await relay.refreshResources(pendingID);
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
    if (!providerID) return;
    const kind = providerType === "claude" ? "claude_login" : "openai_login";
    delete shownChallenge.current[providerID];
    const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: providerID }, "providers_models");
    presentAuthChallenge(next, kind, selectedProviderName || serviceProviderKindLabel(kind, translate), providerID);
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
    setValidation("");
    if (step === "provider") {
      if (providerMode === "new") {
        const ready = await createProvider();
        if (!ready) return;
      } else if (!providerID) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      setStep("keys");
      return;
    }
    if (step === "keys") {
      if (!selectedProvider) {
        setValidation(translate("providers.wizard.required"));
        return;
      }
      if (isLoginType) {
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
        if (pendingNewKeyName) {
          const editedName = keyName.trim();
          if (!editedName || !keyReady) {
            setValidation(translate("providers.wizard.required"));
            return;
          }
          if (editedName !== pendingNewKeyName) {
            setProcessing(true);
            try {
              const renamed = await dispatchWithOutcome("provider.key_patch", { provider_id: providerID, old_name: pendingNewKeyName, name: editedName });
              if (!renamed) return;
              setPendingNewKeyName(editedName);
            } finally {
              setProcessing(false);
            }
          }
          setStep("model");
          if (modelCandidates.length === 0) void fetchWizardModels();
          return;
        }
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
    if (isLoginType) {
      onStatus(translate("providers.wizard.complete"));
      onClose();
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
    if (!providerID || uniqueRequestedModels.length === 0 || (!usingProvidedKey && !manualSelectedKeyName)) {
      setValidation(translate("providers.wizard.selectAtLeastOneModel"));
      return;
    }
    setProcessing(true);
    try {
      const existingModelIDs = selectedProvider ? new Set(asRecords(selectedProvider.models).map(modelRecord).map(editorIdentifier)) : new Set<string>();
      const transientRelaySource = usingProvidedKey && selectedProvidedChoice ? {
        stationID: selectedProvidedChoice.stationID,
        accountID: selectedProvidedChoice.accountID,
        resourceID: selectedProvidedChoice.resourceID,
      } : undefined;
      const pendingKeyChoice = selectedProvider && pendingNewKeyName
        ? providerKeyStates(selectedProvider).find((key) => key.name === pendingNewKeyName)
        : undefined;
      const keyIDForModels = usingProvidedKey
        ? undefined
        : selectedKeyChoice?.id && selectedKeyChoice.kind === "independent" ? selectedKeyChoice.id : pendingKeyChoice?.id;
      const modelPayload = uniqueRequestedModels.map((model, index) => ({
        ...model,
        api_key_name: usingProvidedKey ? selectedProvidedChoice?.resourceLabel ?? "" : manualSelectedKeyName,
        ...(keyIDForModels ? { provider_key_id: keyIDForModels } : {}),
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
          <Text style={styles.providerWizardHint}>{loginFeedback.current ?? translate("relay.loginWorking")}</Text>
        </View> : <>
        {step === "provider" ? <View style={styles.providerWizardFormSection}>
          <NativeFormRow label={translate("providers.wizard.providerType")}>
            <NativeSegmentedControl labels={[translate("providers.wizard.typeApi"), translate("providers.type.openai"), translate("providers.type.claude")]} selectedValue={providerType === "api" ? translate("providers.wizard.typeApi") : providerKindLabel(providerType, translate)} disabled={wizardBusy} onChange={({ nativeEvent }) => { const kinds: WizardType[] = ["api", "openai", "claude"]; chooseProviderType(kinds[nativeEvent.index] ?? "api"); }} style={styles.providerWizardModeControl} />
          </NativeFormRow>
          {providerOptions.length > 0 ? <NativeFormRow label={translate("providers.wizard.sourceMode")}>
            <NativeSegmentedControl labels={[translate("providers.wizard.addProvider"), translate("providers.wizard.selectProvider")]} selectedValue={providerMode === "new" ? translate("providers.wizard.addProvider") : translate("providers.wizard.selectProvider")} disabled={wizardBusy} onChange={({ nativeEvent }) => chooseProviderMode(nativeEvent.index === 1 ? "existing" : "new")} style={styles.providerWizardModeControl} />
          </NativeFormRow> : null}
          {providerMode === "existing" ? <NativeFormRow label={translate("providers.wizard.selectProvider")}><NativePicker labels={providerPickerLabels} selectedValue={selectedProviderPickerLabel} disabled={wizardBusy || providerPickerLabels.length === 0} onChange={({ nativeEvent }) => { const option = providerOptions[nativeEvent.index]; if (option) chooseExistingProvider(option.value); }} style={styles.providerWizardPicker} /></NativeFormRow> : null}
          {providerMode === "new" && providerType === "api" ? <>
            <NativeFormRow label={translate("providers.wizard.baseUrl")}><NativeTextField value={providerBaseURL} placeholder={translate("providers.wizard.baseUrlPlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={updateProviderBaseURL} accessibilityLabel={translate("providers.wizard.baseUrl")} style={styles.providerWizardInput} /></NativeFormRow>
            <NativeFormRow label={translate("providers.wizard.providerName")}><NativeTextField value={providerName} placeholder={translate("providers.wizard.providerNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={updateProviderName} accessibilityLabel={translate("providers.wizard.providerName")} style={styles.providerWizardInput} /></NativeFormRow>
          </> : null}
          {providerMode === "new" && isLoginType ? <Text style={styles.providerWizardHint}>{providerType === "openai" ? translate("relay.officialProviderWebViewHint") : translate("relay.officialProviderBrowserHint")}</Text> : null}
          {providerMode === "existing" && selectedProvider ? <Text numberOfLines={1} style={styles.providerWizardHint}>{activeProviderBaseURL || translate("common.notAvailable")}</Text> : null}
        </View> : null}
        {step === "keys" ? <View style={styles.providerWizardFormSection}>
          <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.stepKeys")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
          {isLoginType ? <>
            <Text style={styles.providerWizardHint}>{translate("providers.wizard.loginKeyHint")}</Text>
            <View style={styles.providerWizardAuthRow}>
              <Text style={styles.providerWizardAuthStatus}>{officialStatusLabel(officialStatus, translate)}</Text>
              {officialStatus === "signed_in"
                ? <NativeButton title={translate("relay.officialProviderLogout")} compact disabled={wizardBusy} onPress={() => { void logoutOfficial(); }} />
                : officialStatus === "authorizing"
                  ? <NativeButton title={translate("relay.officialProviderCancel")} compact disabled={wizardBusy} onPress={() => { void cancelOfficialLogin(); }} />
                  : <NativeButton title={translate("relay.officialProviderLogin")} primary compact disabled={wizardBusy} onPress={() => { void startOfficialLogin(); }} />}
            </View>
          </> : <>
            <NativeFormRow label={translate("providers.wizard.keyPath")}>
              <NativeSegmentedControl labels={[translate("providers.wizard.pathManual"), translate("providers.wizard.pathLogin")]} selectedValue={keyPath === "login" ? translate("providers.wizard.pathLogin") : translate("providers.wizard.pathManual")} disabled={wizardBusy} onChange={({ nativeEvent }) => { setKeyPath(nativeEvent.index === 1 ? "login" : "manual"); setValidation(""); }} style={styles.providerWizardModeControl} />
            </NativeFormRow>
            {keyPath === "login" ? <>
              {providedChoices.length > 0 ? <>
                <Text style={styles.providerWizardHint}>{translate("providers.wizard.providedKeysHint", { count: providedChoices.length })}</Text>
                <View style={styles.providerWizardModelList}>
                  {providedChoices.map((source) => {
                    const value = `relay:${relaySourceSelectionID(source)}`;
                    return <NativeCheckbox
                      key={value}
                      label={`${source.resourceLabel} · ${source.accountLabel}`}
                      value={(selectedProvidedChoice && `relay:${relaySourceSelectionID(selectedProvidedChoice)}`) === value}
                      disabled={wizardBusy}
                      onValueChange={() => setProvidedKeySelection(value)}
                      style={styles.providerWizardModelCheckbox}
                    />;
                  })}
                </View>
              </> : <>
                <Text style={styles.providerWizardHint}>{signedInAccountID ? translate("relay.resourcesNotLoaded") : translate("providers.wizard.loginFirstHint")}</Text>
                <NativeButton title={loginBusy ? translate("relay.stepSignIn") : translate("relay.login")} primary compact disabled={wizardBusy || !providerID || !activeProviderBaseURL.trim()} onPress={() => { void beginRelayLogin(); }} />
              </>}
              {loginFeedback.current ? <Text style={styles.providerWizardHint}>{loginFeedback.current}</Text> : null}
            </> : <>
              <NativeFormRow label={translate("providers.wizard.selectApiKey")}><NativePicker labels={keyPickerLabels} selectedValue={selectedKeyPickerLabel} disabled={wizardBusy} onChange={({ nativeEvent }) => { const option = keyOptions[nativeEvent.index]; if (!option) return; setKeySelection(option.value); setKeyReady(false); setKeyName(""); setValidation(""); }} style={styles.providerWizardPicker} /></NativeFormRow>
              {activeKeySelection === PROVIDER_WIZARD_NEW_KEY ? <>
                <NativeFormRow label={translate("providers.wizard.apiKeyName")}><NativeTextField value={keyName} placeholder={translate("providers.wizard.apiKeyNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={setKeyName} accessibilityLabel={translate("providers.wizard.apiKeyName")} style={styles.providerWizardInput} /></NativeFormRow>
                {pendingNewKeyName ? (keyReady
                  ? <Text style={styles.providerWizardHint}>{pendingNewKeyName}</Text>
                  : <NativeFormRow label={translate("providers.wizard.apiKeyValue")}><NativeSecureTextInput label={translate("providers.wizard.apiKeyValue")} domain="providers_models" field="api_key" target={`${providerID}\u001f${pendingNewKeyName}`} plainText autoCommit disabled={wizardBusy} onSecretState={(state) => { setKeyReady(state.present); onSecretState(state); }} style={styles.providerWizardSecretInput} /></NativeFormRow>) : null}
              </>
                : selectedKeyChoice?.kind === "independent" ? <>
                    <NativeFormRow label={translate("providers.wizard.apiKeyName")}><NativeTextField value={keyName} placeholder={translate("providers.wizard.apiKeyNamePlaceholder")} editable={!wizardBusy} autoCapitalize="none" autoCorrect={false} onChangeText={setKeyName} accessibilityLabel={translate("providers.wizard.apiKeyName")} style={styles.providerWizardInput} /></NativeFormRow>
                    {selectedKeyReady ? <Text style={styles.providerWizardHint}>{selectedKeyChoice.name}</Text>
                      : <NativeFormRow label={translate("providers.wizard.apiKeyValue")}><NativeSecureTextInput label={translate("providers.wizard.apiKeyValue")} domain="providers_models" field="api_key" target={`${providerID}\u001f${selectedKeyChoice.name}`} plainText autoCommit disabled={wizardBusy} onSecretState={(state) => { setKeyReady(state.present); onSecretState(state); }} style={styles.providerWizardSecretInput} /></NativeFormRow>}
                  </> : <Text style={styles.providerWizardHint}>{translate("providers.wizard.selectApiKey")}</Text>}
            </>}
          </>}
        </View> : null}
        {step === "model" ? isLoginType ? <View style={styles.providerWizardFormSection}>
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
        </View> : <PersistentScrollView style={styles.providerWizardModelScroll} contentContainerStyle={styles.providerWizardModelScrollContent} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">
          <View style={styles.providerWizardFormSection}>
            <View style={styles.providerWizardSectionHeader}><Text style={styles.providerWizardPanelTitle}>{translate("providers.wizard.models")}</Text><Text numberOfLines={1} style={styles.providerWizardHint}>{selectedProviderName}</Text></View>
            {!usingProvidedKeyPath(keyPath) ? <View style={styles.providerWizardModelToolbar}>
              <Text numberOfLines={2} style={styles.providerWizardHint}>{modelFetchState === "loading" ? translate("providers.wizard.fetchingModels") : modelCandidates.length > 0 ? translate("providers.wizard.modelsFound", { count: modelCandidates.length }) : modelFetchState === "unavailable" ? translate("providers.wizard.modelsUnavailable") : modelFetchState === "empty" ? translate("providers.wizard.modelsEmpty") : translate("providers.wizard.noModels")}</Text>
              <NativeButton title={translate("providers.wizard.refreshModels")} compact link disabled={wizardBusy || modelFetchState === "loading" || !providerID || !manualSelectedKeyName} onPress={() => { void fetchWizardModels(); }} />
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
        {validation ? <Text style={styles.providerWizardValidation}>{validation}</Text> : null}
        </>}
      </View>
    </View>
    <View style={styles.providerWizardFooter}>
      {validation || loginFeedback.current ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.providerWizardFooterStatus}>{validation || loginFeedback.current}</Text> : <View style={styles.providerWizardFooterSpacer} />}
      <View style={styles.providerWizardFooterActions}>
        <NativeButton title={translate("status.close")} disabled={processing} onPress={onClose} />
        {step !== "provider" || loginPhase === "sign-in" ? <NativeButton title={translate("providers.wizard.back")} disabled={busy || processing} onPress={goBack} /> : null}
        {loginPhase === "sign-in"
          ? null
          : <NativeButton primary title={processing ? translate("providers.wizard.creating") : step === "model" ? translate("providers.wizard.finish") : translate("providers.wizard.next")} disabled={wizardBusy} onPress={() => { void goNext(); }} />}
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
      html: CODE_EDITOR_HTML,
    });
  }
}

function ProviderWorkspace({ snapshot, ipc, onSnapshot, native, busy, translate, dispatch, dispatchWithOutcome, onStatus, onSecretState, applyProbedSurface, onOpenWizard, relay, addOfficialAccount, onActivateAndRestart }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain, keepControlsEnabled?: boolean) => Promise<CoreSnapshot | undefined>; onStatus: (status?: string) => void; onSecretState: (state: SecretState) => void; applyProbedSurface: ApplyProbedSurface; onOpenWizard: () => void; relay: RelayWorkspaceBridge; addOfficialAccount: (kind: ServiceProviderKind) => Promise<string>; onActivateAndRestart: () => Promise<boolean> }): React.JSX.Element {
  const state = domainState(snapshot, "providers_models");
  const relaySources = useMemo(() => relaySourcesFromSnapshot(snapshot), [snapshot]);
  const relayStations = useMemo(() => relayStationsFromSnapshot(snapshot), [snapshot]);
  const relayAccounts = useMemo(() => accountsFromSnapshot(snapshot), [snapshot]);
  const relayStationsFull = useMemo(() => stationsFromSnapshot(snapshot, relayAccounts), [relayAccounts, snapshot]);
  const providers = useMemo(() => {
    const details = asRecords(state.providers);
    const candidates = details.length > 0 ? details : (snapshot?.providers_models.providers ?? []).map(providerRecord);
    return candidates;
  }, [snapshot?.providers_models.providers, state.providers]);
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
  // rebound to that station automatically (atlas.example and
  // www.atlas.example stay distinct sites).  Core treats a rebind that leaves
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
  const [fetchKeyID, setFetchKeyID] = useState<string>();
  const probingModelKeys = useRef(new Set<string>());
  const [, setProbeActivityRevision] = useState(0);
  const [probeResults, setProbeResults] = useState<Record<string, IpcResults["probe"]>>({});
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
    const keyName = fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? apiKeyDisplayName(apiKeyName, translate);
    void native.chooseModelsToAdd({ models: candidates, providerName, keyName }).then((selection) => {
      const selectedModels = (selection ?? []).filter((model, index, all) => candidateSet.has(model) && all.indexOf(model) === index);
      if (selectedModels.length === 0) return;
      void dispatch("model.add_many", {
        provider_id: providerId,
        models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, api_key_name: apiKeyName, enabled: true, order: 0 })).map((model) => ({
          ...model,
          ...modelRecordCapabilityChanges(modelCapabilities[model.upstream_model]),
        })),
      });
    }).catch(() => undefined);
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
  const addModel = (): void => {
    if (!provider) return;
    const knownModelIds = new Set(models.map(editorIdentifier));
    pendingModelIds.current = { providerId, ids: knownModelIds };
    // The pane commits every edit, so a new model is valid the moment it
    // exists: it carries the localized placeholder name (and that name as its
    // upstream route) that the user renames afterwards, instead of an empty
    // name that blocks the commit with a validation error nobody can act on.
    const base = translate("providers.newModel");
    const name = uniquePlaceholderName(models.map((item) => stringValue(item.model_name ?? item.name)), base);
    void dispatch("model.add", { provider_id: providerId, model: { name, upstream_model: name, enabled: true, order: 0 } });
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
    void dispatch("provider.add", { provider: { name, models: [] } });
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
  const activeRoute = routes.find((entry) => entry.key === selectedRoute);
  const activeRouteGroup = activeRoute ? routes.filter((entry) => entry.publicModel === activeRoute.publicModel) : [];
  const activeRouteIndex = activeRoute ? activeRouteGroup.findIndex((entry) => entry.key === activeRoute.key) : -1;
  const activeRouteUsesMultiplier = Boolean(activeRoute && modelOrderMode(activeRoute.model) === "relay_multiplier");
  const canMoveRouteUp = !activeRouteUsesMultiplier && activeRouteIndex > 0;
  const canMoveRouteDown = !activeRouteUsesMultiplier && activeRouteIndex >= 0 && activeRouteIndex < activeRouteGroup.length - 1;
  useEffect(() => {
    // A cleared route selection stays cleared; the first route only fills in
    // when the selected one is gone from the list.
    if (viewMode !== "routes" || routes.length === 0 || selectedRoute === "" || routes.some((entry) => entry.key === selectedRoute)) return;
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
    const kind = providerKindSelected;
    const action = kind === "openai" || kind === "claude"
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
    void native.showConfirmation({ title: translate("providers.deleteProvider"), message, confirmLabel: translate("common.delete") }).then((confirmed) => {
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
    void native.showConfirmation({ title: translate("providers.deleteModel"), message: modelDisplayName(providerId, model) || modelId, confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("model.delete", { provider_id: providerId, model_id: modelId }).then(() => setSelectedModel(undefined)) : undefined);
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
    // key's models listed (and indented) underneath it.
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [];
    const grouped = new Map<string, UnknownRecord[]>();
    for (const item of models) {
      const keyName = modelProviderKeyLabel(item, provider ?? {}, translate);
      const list = grouped.get(keyName);
      if (list) list.push(item);
      else grouped.set(keyName, [item]);
    }
    for (const [keyName, list] of grouped) {
      rows.push({ key: `key:${keyName}`, cells: [keyName], spanning: true });
      for (const item of list) {
        rows.push({ key: editorIdentifier(item), cells: [`\t${modelUpstreamDisplay(providerId, item)}`, modelDisplayName(providerId, item), modelOrderText(providerId, item)] });
      }
    }
    return rows;
  }, [modelDisplayName, modelOrderText, modelUpstreamDisplay, models, provider, providerId, translate]);
  const disabledModelKeys = useMemo(
    () => models.filter((item) => !booleanValue(provider?.enabled, true) || !booleanValue(item.model_enabled, booleanValue(item.enabled, true))).map(editorIdentifier),
    [models, provider?.enabled],
  );
  // Rows that cannot materialize a route keep the pane honest about what
  // blocks Apply: Core rejects a model without a public name, so the row that
  // needs the name is marked where it is edited.
  const alertModelKeys = useMemo(() => models.filter(modelNeedsAttention).map(editorIdentifier), [models]);
  const alertProviderKeys = useMemo(
    () => providers.filter((item) => asRecords(item.models).some(modelNeedsAttention)).map(editorIdentifier),
    [providers],
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
        cells: [`\t${providerDisplayName(entry.provider)}`, modelProviderKeyLabel(entry.model, entry.provider, translate), order, modelUpstreamDisplay(editorIdentifier(entry.provider), entry.model) || translate("common.notAvailable")],
      });
    }
    return rows;
  }, [modelOrderText, modelUpstreamDisplay, providerDisplayName, routes, translate]);
  const disabledRouteKeys = useMemo(
    () => routes.filter((entry) => !entry.providerEnabled || !entry.modelEnabled || !entry.keyAvailable).map((entry) => entry.key),
    [routes],
  );
  const selectRoute = useCallback((routeId: string): void => {
    if (!routeId) {
      // A click below the route rows clears the selection: the inspector
      // empties instead of keeping a route active without a highlighted row.
      setSelectedRoute("");
      setProviderSourceModel(undefined);
      return;
    }
    const selected = routes.find((entry) => entry.key === routeId);
    if (!selected) return;
    setSelectedRoute(routeId);
    setSelectedProvider(editorIdentifier(selected.provider));
    setSelectedModel(editorIdentifier(selected.model));
    setProviderSourceModel(undefined);
  }, [routes]);
  const chooseViewMode = (value: "providers" | "routes"): void => {
    if (value === viewMode) return;
    if (value === "routes") {
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
        <TablePane wide style={styles.routeTablePane} title={translate("providers.routes")} actions={<><IconButton label="↑" title={translate("common.moveUp")} disabled={busy || !canMoveRouteUp} onPress={() => moveRoute("up")} /><IconButton label="↓" title={translate("common.moveDown")} disabled={busy || !canMoveRouteDown} onPress={() => moveRoute("down")} /></>}>
          <NativeTable columns={[{ label: translate("providers.provider"), width: 96 }, { label: translate("providers.providerKey"), width: 130 }, { label: translate("common.order"), width: 64 }, { label: translate("providers.upstream"), width: 120 }]} rows={routeRows} disabledRowKeys={disabledRouteKeys} selectedKey={selectedRoute ?? ""} compact onSelectionChange={selectRoute} style={styles.nativeRouteTable} />
        </TablePane>
      </View> : <View style={styles.providerWorkspace}>
        <View style={styles.providerModelColumns}>
          <TablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} />{provider ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteProvider} /> : null}</>}>
            <NativeTable columns={[{ label: translate("providers.provider"), width: 132 }]} rows={providerRows} disabledRowKeys={disabledProviderKeys} alertRowKeys={alertProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }} style={styles.nativeProviderTable} />
          </TablePane>
          <View style={styles.providerMiddlePane}>
            <TablePane style={[styles.modelListPane]} title={translate("providers.models")} actions={<>{provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? <IconButton label="+" title={translate("providers.newModel")} disabled={busy} onPress={addModel} /> : null}{model ? <IconButton label="⧉" title={translate("common.copy")} disabled={busy} onPress={duplicateModel} /> : null}{model ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteModel} /> : null}</>}>
              <NativeTable columns={[{ label: translate("providers.upstream"), width: 120 }, { label: translate("providers.publicModel"), width: 100 }, { label: translate("common.order"), width: 60 }]} rows={modelRows} disabledRowKeys={disabledModelKeys} alertRowKeys={alertModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedModel(key); setProviderSourceModel(undefined); }} style={styles.nativeModelTable} />
              {provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? <View style={styles.tableBottomRow}><NativePicker labels={fetchKeyOptions.length > 0 ? fetchKeyOptions.map((option) => option.label) : [translate("common.default")]} selectedValue={fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? translate("common.default")} disabled={busy || fetchKeyChoices.length === 0} onChange={({ nativeEvent }) => { const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyID(option.value); }} style={styles.fetchKeyPicker} /><ActionButton title={translate("providers.fetch")} disabled={busy || !selectedFetchKey} onPress={fetchModels} /></View> : null}
            </TablePane>
          </View>
        </View>
      </View>}
    </View>
    <View style={styles.providerInspector}>{viewMode === "routes" ? (activeRoute ? (providerSourceModel ? <ProviderEditor key={`provider:${editorIdentifier(activeRoute.provider)}`} provider={activeRoute.provider} relaySources={relaySources} relayStations={relayStations} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onNameDraftChange={(value) => setProviderNameDraft(editorIdentifier(activeRoute.provider), value)} sourceModel={activeRoute.model} onReturnToModel={() => { setProviderSourceModel(undefined); setSelectedModel(editorIdentifier(activeRoute.model)); }} station={stationForProvider(activeRoute.provider)} stationAccounts={stationAccountsFor(activeRoute.provider)} relay={relay} addOfficialAccount={addOfficialAccount} onActivateAndRestart={onActivateAndRestart} onStatus={onStatus} language={snapshot?.language ?? "system"} snapshotForCleanups={snapshot} /> : <ModelInspector key={`model:${editorIdentifier(activeRoute.provider)}:${editorIdentifier(activeRoute.model)}`} providers={providers} providerLabels={providers.map(providerDisplayName)} provider={activeRoute.provider} providerId={editorIdentifier(activeRoute.provider)} model={activeRoute.model} modelName={modelDisplayName(editorIdentifier(activeRoute.provider), activeRoute.model)} relaySources={relaySources} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} {...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} onNameDraftChange={(value) => setModelNameDraft(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model), value)} onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: editorIdentifier(activeRoute.provider), model_id: editorIdentifier(activeRoute.model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(activeRoute.model)); setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`); setProviderSourceModel(undefined); })} />) : <EmptyState translate={translate} />) : provider && model ? <ModelInspector key={`model:${providerId}:${editorIdentifier(model)}`} providers={providers} providerLabels={providers.map(providerDisplayName)} provider={provider} providerId={providerId} model={model} modelName={modelDisplayName(providerId, model)} relaySources={relaySources} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(providerId, editorIdentifier(model))} {...modelProbeProps(providerId, editorIdentifier(model))} onNameDraftChange={(value) => setModelNameDraft(providerId, editorIdentifier(model), value)} onProviderClick={() => { setProviderSourceModel(editorIdentifier(model)); setSelectedModel(undefined); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: providerId, model_id: editorIdentifier(model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(model)); setProviderSourceModel(undefined); })} /> : provider ? <ProviderEditor key={`provider:${providerId}`} provider={provider} relaySources={relaySources} relayStations={relayStations} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onSecretState={onSecretState} onNameDraftChange={(value) => setProviderNameDraft(providerId, value)} sourceModel={models.find((item) => editorIdentifier(item) === providerSourceModel)} onReturnToModel={() => { if (providerSourceModel) setSelectedModel(providerSourceModel); setProviderSourceModel(undefined); }} station={selectedStation} stationAccounts={selectedStationAccounts} relay={relay} addOfficialAccount={addOfficialAccount} onActivateAndRestart={onActivateAndRestart} onStatus={onStatus} language={snapshot?.language ?? "system"} snapshotForCleanups={snapshot} /> : <EmptyState translate={translate} />}</View>
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
function ProviderKeysPanel({ provider, providerId, kind, stationAccounts, native, busy, translate, dispatch, onSecretState, relay, onStatus, language, paneViewportHeight = 0, paneContentHeight = 0, variant = "pane" }: { provider?: UnknownRecord; providerId: string; kind: ProviderKind; stationAccounts: RelayAccount[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; relay: RelayWorkspaceBridge; onStatus: (status?: string) => void; language: "system" | "en" | "zh-Hans"; snapshotForCleanups?: CoreSnapshot; paneViewportHeight?: number; paneContentHeight?: number; variant?: "pane" | "inline" }): React.JSX.Element {
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
  const pendingCustomKeyName = useRef<string | undefined>(undefined);
  const [providedNameDrafts, setProvidedNameDrafts] = useState<Record<string, string>>({});
  const [formBusy, setFormBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [remoteDelete, setRemoteDelete] = useState<{ account: RelayAccount; resource: RelayResource }>();
  const [remoteDeletePolicy, setRemoteDeletePolicy] = useState<"delete_models" | "detach_disabled" | "detach_only">("detach_disabled");
  const selectedCustom = selectedKey.startsWith("custom:")
    ? customKeys.find((key) => `custom:${key.id}` === selectedKey)
    : undefined;
  const selectedProvided = selectedKey.startsWith("provided:")
    ? providedRows.find((row) => `provided:${row.key}` === selectedKey)
    : undefined;
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
      await action();
      await relay.refreshAccounts();
      onStatus?.(translate(feedbackKey));
    } catch {
      onStatus?.(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  // Quietly keep station groups aligned while auto-grouping is on.
  const autoGroupingAccounts = useMemo(() => stationAccounts.filter((account) => account.autoGrouping), [stationAccounts]);
  useEffect(() => {
    if (!autoGrouping || !relay.apiKeyActions.alignAutoGrouping) return;
    let active = true;
    const interval = setInterval(() => {
      if (!active || controlsBusy) return;
      void (async () => {
        for (const account of autoGroupingAccounts) {
          if (!active) return;
          try {
            const status = await relay.refreshResources(account.id);
            if (!active || status !== "ready") continue;
            await relay.apiKeyActions.alignAutoGrouping?.(account.id);
          } catch {
            // The next interval can retry.
          }
        }
        if (active) await relay.refreshAccounts();
      })();
    }, 30 * 60_000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [autoGrouping, autoGroupingAccounts, controlsBusy, relay]);
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
    if (!pending) return;
    const added = customKeys.find((key) => key.name === pending);
    if (added) {
      pendingCustomKeyName.current = undefined;
      setSelectionCleared(false);
      setSelectedKey(`custom:${added.id}`);
    }
  }, [customKeys]);
  const deleteSelected = (): void => {
    if (selectedProvided) {
      if (autoGrouping) return;
      setRemoteDeletePolicy("detach_disabled");
      setRemoteDelete({ account: selectedProvided.account, resource: selectedProvided.resource });
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
  // The inline keys list sizes to the pane instead of a fixed row budget:
  // it shrinks until the whole inspector fits, so the pane keeps no scrollbar
  // of its own by default, and it still scrolls internally when it overflows.
  // Ordinary rows are 22 pt, spanning group rows 28 pt, plus the 24 pt header
  // and 2 pt of slack.  A previous pane measurement gives the height taken by
  // every sibling, so the list can take exactly the leftover room.
  const keysTableInlineRowHeights = tableRows.map((row) => (row.spanning ? 28 : 22));
  const keysTableInlineContentHeight = 26 + keysTableInlineRowHeights.reduce((height, rowHeight) => height + rowHeight, 0);
  const keysTableInlineRenderedHeight = useRef(0);
  const keysTableInlineSiblingHeight = paneViewportHeight > 0 && paneContentHeight > 0 && keysTableInlineRenderedHeight.current > 0
    ? paneContentHeight - keysTableInlineRenderedHeight.current
    : undefined;
  const keysTableInlineAvailableHeight = keysTableInlineSiblingHeight === undefined ? undefined : paneViewportHeight - keysTableInlineSiblingHeight - 1;
  const keysTableInlineMinHeight = 26 + keysTableInlineRowHeights.slice(0, 3).reduce((height, rowHeight) => height + rowHeight, 0);
  let keysTableInlineHeight = Math.min(keysTableInlineContentHeight, 26 + keysTableInlineRowHeights.slice(0, 10).reduce((height, rowHeight) => height + rowHeight, 0));
  if (keysTableInlineAvailableHeight !== undefined) {
    let fitted = 26;
    for (const rowHeight of keysTableInlineRowHeights) {
      if (fitted + rowHeight > keysTableInlineAvailableHeight) break;
      fitted += rowHeight;
    }
    keysTableInlineHeight = Math.min(keysTableInlineContentHeight, Math.max(fitted, Math.min(keysTableInlineMinHeight, keysTableInlineContentHeight)));
  }
  React.useLayoutEffect(() => { keysTableInlineRenderedHeight.current = keysTableInlineHeight; }, [keysTableInlineHeight]);
  const keysTable = <NativeTable
      columns={variant === "inline"
        ? [{ label: translate("providers.keys"), width: 264 }]
        : [{ label: translate("common.name"), width: 150 }, { label: translate("providers.detail"), width: 170 }]}
      rows={tableRows}
      selectedKey={selectedCustom ? `custom:${selectedCustom.id}` : selectedProvided ? `provided:${selectedProvided.key}` : ""}
      disabledRowKeys={providedRows.filter((row) => row.unavailable).map((row) => `provided:${row.key}`)}
      compact
      cellHorizontalPadding={6}
      firstColumnHorizontalPadding={6}
      scrollTrailingColumnOverflow={false}
      onSelectionChange={(key) => {
        if (key.startsWith("group:") || key.startsWith("account:")) return;
        setSelectionCleared(key === "");
        setSelectedKey(key);
      }}
      style={variant === "inline" ? [styles.keysTableInline, { height: keysTableInlineHeight, minHeight: keysTableInlineHeight }] : styles.keysTable}
    />;
  const keysEditorView = <View style={styles.keysEditor}>
      {selectedCustom ? <>
        <TextField
          key={`custom-key-name:${providerId}:${selectedCustom.id}`}
          label={translate("providers.keyName")}
          labelWidth={64}
          value={drafts?.providerKeyDisplayName(providerId, selectedCustom.id, selectedCustom.name) ?? selectedCustom.name}
          disabled={busy}
          onDraftChange={(value) => drafts?.setProviderKeyNameDraft(providerId, selectedCustom.id, value)}
          onCommit={(name) => {
            if (!name || name === selectedCustom.name) return;
            pendingCustomKeyName.current = name;
            void dispatch("provider.key_patch", { provider_id: providerId, old_name: selectedCustom.name, name });
          }}
        />
        <NativeSecretField plainText autoCommit label={translate("providers.keyValue")} hint={booleanValue(selectedCustom.configured) ? translate("providers.apiKeySavedHint") : translate("providers.apiKeyInput")} labelWidth={64} busy={busy} domain="providers_models" field="api_key" target={`${providerId}\u001f${selectedCustom.name}`} onSecretState={onSecretState} />
      </> : selectedProvided ? <>
        {/* Relay keys match the custom key editor: name + value only. */}
        <TextField
          key={`provided-key-name:${selectedProvided.key}`}
          label={translate("providers.keyName")}
          labelWidth={64}
          value={selectedProvidedName}
          disabled={controlsBusy || selectedProvided.account.autoGrouping}
          onDraftChange={(value) => setProvidedNameDrafts((current) => ({ ...current, [selectedProvided.key]: value }))}
          onCommit={(value) => {
            const name = value.trim();
            if (!name || name === selectedProvided.resource.name || selectedProvided.account.autoGrouping) return;
            void runProvidedAction(() => relay.apiKeyActions.update?.(selectedProvided.account.id, selectedProvided.resource.id, name) ?? Promise.resolve(), "relay.apiKeyUpdateStaged");
          }}
        />
        <View style={styles.keysEditorRow}>
          <NativeSecretField
            plainText
            autoCommit
            disabled
            label={translate("providers.keyValue")}
            hint={selectedProvided.resource.keyHint ? translate("providers.apiKeySavedHint") : translate("common.none")}
            labelWidth={64}
            busy={controlsBusy}
            domain="relay_accounts"
            field="api_key"
            target={`${selectedProvided.account.id}:${selectedProvided.resource.id}`}
            onSecretState={onSecretState}
          />
          <NativeButton title="" symbol="copy" compact disabled={controlsBusy || !selectedProvided.resource.keyHint} toolTip={translate("relay.apiKeyCopy")} accessibilityLabel={translate("relay.apiKeyCopy")} onPress={() => {
            void (async () => {
              try {
                const copied = await native.copySecret({ domain: "relay_accounts", field: "api_key", target: `${selectedProvided.account.id}:${selectedProvided.resource.id}` });
                onStatus?.(translate(copied ? "relay.apiKeyCopied" : "relay.operationFailed"));
              } catch {
                onStatus?.(translate("relay.operationFailed"));
              }
            })();
          }} style={styles.panelActionButton} />
        </View>
      </> : null}
    </View>;
  const dialogs = <>
      <ApiKeyCreateDialog
        visible={createOpen}
        groups={stationAccounts[0]?.groups.filter((group) => group.id !== "") ?? []}
        disabled={controlsBusy}
        onClose={() => setCreateOpen(false)}
        onCreate={(options) => {
          setCreateOpen(false);
          const account = stationAccounts[0];
          if (!account) return;
          void runProvidedAction(() => relay.apiKeyActions.create?.(account.id, options) ?? Promise.resolve(), "relay.apiKeyCreateStaged");
        }}
        translate={translate}
      />
      <DependencyPolicyDialog
        visible={Boolean(remoteDelete)}
        title={translate("relay.apiKeyDeleteImpactTitle")}
        message={remoteDelete ? translate("relay.apiKeyDeleteImpactBody", { count: remoteDelete.resource.linkedModelCount, label: remoteDelete.resource.apiName || remoteDelete.resource.name }) : ""}
        options={[
          { value: "detach_disabled", label: translate("relay.policyReleaseDisabled"), hint: translate("relay.policyReleaseDisabledHint") },
          { value: "delete_models", label: translate("relay.policyDeleteModels"), hint: translate("relay.policyDeleteModelsHint") },
          { value: "detach_only", label: translate("relay.apiKeyDetachOnly"), hint: translate("relay.apiKeyDetachOnlyHint") },
        ]}
        value={remoteDeletePolicy}
        disabled={controlsBusy}
        confirmLabel={remoteDeletePolicy === "detach_only" ? translate("screen.confirm") : translate("common.delete")}
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
      {tableRows.length > 0 ? keysTable : null}
      {keysEditorView}
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

function ModelInspector({ providers, providerLabels, provider, providerId, model, modelName, relaySources, native, busy, translate, dispatch, probe, probing, probeResult, onNameDraftChange, onProviderClick, onProviderChange }: { providers: UnknownRecord[]; providerLabels: string[]; provider: UnknownRecord; providerId: string; model: UnknownRecord; modelName: string; relaySources: RelaySourceOption[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; probe: () => void; probing: boolean; probeResult?: IpcResults["probe"]; onNameDraftChange?: (name: string) => void; onProviderClick: () => void; onProviderChange: (providerId: string) => void }): React.JSX.Element {
  const id = editorIdentifier(model);
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
      closeLabel: translate("status.close"),
      language: "text",
      html: CODE_EDITOR_HTML,
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
    <View style={styles.modelBreadcrumb}><NativeButton title={providerLabel} link disabled={busy} onPress={onProviderClick} style={styles.breadcrumbProvider} /><Text style={styles.breadcrumbSeparator}>&gt;</Text><Text numberOfLines={1} style={styles.inspectorHeading}>{displayLabel(modelName, translate("providers.unnamedModel"))}</Text></View>
    <View style={styles.inspectorDivider} />
    <View style={styles.inspectorBody}>
      <View style={styles.inspectorEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(model.model_enabled, booleanValue(model.enabled, true))} disabled={busy} onValueChange={(model_enabled) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { model_enabled } })} style={styles.inspectorEnableControl} /><ActionButton title={probing ? translate("providers.probing") : translate("providers.probe")} disabled={busy || probing || !probeReady} onPress={probe} />{probePresentation.compact ? <Pressable accessibilityRole="button" accessibilityLabel={probePresentation.compact} accessibilityHint={probeDetailHint} onPress={openProbeDetails} style={({ pressed }) => [styles.probeSummaryTrigger, pressed && styles.probeSummaryTriggerPressed]}><TooltipText numberOfLines={2} tooltip={probeDetailHint} style={styles.probeSummary}>{probePresentation.compact}</TooltipText></Pressable> : null}</View>
      <TextField label={translate("providers.publicModel")} labelWidth={60} value={modelFieldName} onDraftChange={onNameDraftChange} onCommit={(name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { name } })} />
      <PickerField label={translate("providers.provider")} labelWidth={60} allowShrink value={providerLabel} values={providerLabels} disabled={busy || providers.length <= 1} onSelect={(label) => { const next = providers[providerLabels.indexOf(label)]; if (next) onProviderChange(editorIdentifier(next)); }} />
      {providerKeyOptions.length > 0 ? <PickerField label={translate("providers.providerKey")} labelWidth={60} allowShrink value={selectedProviderKey?.id ?? ""} values={[{ value: "", label: translate("providers.undefinedKey") }, ...providerKeyOptions]} disabled={busy} onSelect={selectProviderKey} /> : null}
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

// A staged model that cannot materialize a route yet.  Without a public name
// (or an upstream model) Core rejects Apply, so the shared pane marks the row
// where the model is edited instead of only reporting that the draft is invalid.
function modelNeedsAttention(model: UnknownRecord): boolean {
  if (!booleanValue(model.model_enabled, booleanValue(model.enabled, true))) return false;
  const name = stringValue(model.model_name).trim() || stringValue(model.name).trim();
  if (!name) return true;
  return !(stringValue(model.litellm_model).trim() || stringValue(model.upstream_model).trim());
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

function ProviderSourceFields({ provider, providerID, relayStations, busy, translate, dispatch, onBaseUrlDraftChange, onNameDraftChange }: { provider: UnknownRecord; providerID: string; relayStations: RelayStationOption[]; busy: boolean; translate: Translate; dispatch: Dispatch; onBaseUrlDraftChange?: (baseURL: string) => void; onNameDraftChange?: (name: string) => void }): React.JSX.Element {
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

function ProviderEditor({ provider, relaySources, relayStations, native, busy, translate, dispatch, dispatchWithOutcome, onSecretState, onNameDraftChange, sourceModel, onReturnToModel, station, stationAccounts, relay, addOfficialAccount, onActivateAndRestart, onStatus, language, snapshotForCleanups }: { provider: UnknownRecord; relaySources: RelaySourceOption[]; relayStations: RelayStationOption[]; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<CoreSnapshot | undefined>; onSecretState: (state: SecretState) => void; onNameDraftChange?: (name: string) => void; sourceModel?: UnknownRecord; onReturnToModel: () => void; station?: RelayStation; stationAccounts: RelayAccount[]; relay: RelayWorkspaceBridge; addOfficialAccount: (kind: ServiceProviderKind) => Promise<string>; onActivateAndRestart: () => Promise<boolean>; onStatus: (status?: string) => void; language: "system" | "en" | "zh-Hans"; snapshotForCleanups?: CoreSnapshot }): React.JSX.Element {
  const id = editorIdentifier(provider);
  const drafts = useContext(ProviderWorkspaceDraftContext);
  const kind = providerKind(provider);
  const isLogin = kind === "openai" || kind === "claude";
  const [loginBusy, setLoginBusy] = useState(false);
  const [relayAddBusy, setRelayAddBusy] = useState(false);
  const [editorViewportHeight, setEditorViewportHeight] = useState(0);
  const [editorContentHeight, setEditorContentHeight] = useState(0);
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
  const startLogin = async (): Promise<void> => {
    const kind = providerKind(provider) === "claude" ? "claude_login" : "openai_login";
    delete shownChallenge.current[id];
    setLoginBusy(true);
    try {
      const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: id }, "providers_models");
      presentProviderAuthChallenge(native, translate, next, kind, providerName, id, shownChallenge.current);
    } finally {
      setLoginBusy(false);
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
    const activated = await dispatchWithOutcome("service_provider.auth_activate", { provider_id: id }, "providers_models");
    if (!activated) return;
    try {
      if (await onActivateAndRestart()) onStatus(translate("relay.officialProviderActive"));
    } catch (reason) {
      onStatus(errorMessage(reason, translate));
    }
  };
  const addSiblingAccount = async (): Promise<void> => {
    setLoginBusy(true);
    try {
      const newID = await addOfficialAccount(kind === "claude" ? "claude_login" : "openai_login");
      const kindLogin: ServiceProviderKind = kind === "claude" ? "claude_login" : "openai_login";
      const next = await dispatchWithOutcome("service_provider.auth_start", { provider_id: newID }, "providers_models");
      presentProviderAuthChallenge(native, translate, next, kindLogin, providerKindLabel(kind, translate), newID, shownChallenge.current);
    } catch (reason) {
      onStatus(errorMessage(reason, translate));
    } finally {
      setLoginBusy(false);
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
    const origin = normalizeRelayOrigin(drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base)));
    if (!origin) return;
    // What the sign-in may save is asked after the login completes, inside
    // the native browser flow; no pre-login prompt or checkbox runs here.
    setRelayAddBusy(true);
    const pendingID = `login-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const stationName = suggestedRelayStationName(origin) || origin;
    try {
      const result = await native.relayLogin({
        accountId: pendingID,
        type: "newapi",
        label: origin,
        origin,
        language,
        pendingAccount: true,
        stationName,
        stationType: "newapi",
        stationOrigin: origin,
      });
      if (!result) {
        onStatus?.(translate("relay.loginNotCompleted"));
        return;
      }
      await relay.refreshAccounts();
      await relay.refreshResources(pendingID);
      onStatus?.(translate("relay.loginComplete"));
    } catch {
      onStatus?.(translate("relay.operationFailed"));
    } finally {
      setRelayAddBusy(false);
    }
  };
  return <PersistentScrollView style={styles.providerEditorContent} contentContainerStyle={styles.providerEditorScrollContent} showsVerticalScrollIndicator nestedScrollEnabled onViewportHeightChange={setEditorViewportHeight} onContentHeightChange={setEditorContentHeight}>
    <View style={styles.providerEditorHeader}><Text numberOfLines={1} style={styles.providerEditorHeading}>{translate("providers.provider")}: {providerName}</Text>{sourceModel ? <NativeButton title={translate("providers.backToModel", { model: sourceModelLabel })} link disabled={busy} onPress={onReturnToModel} style={styles.providerReturnToModel} /> : null}</View>
    <View style={styles.providerEditorSection}>
    <View style={styles.providerEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(provider.enabled, true)} disabled={busy} onValueChange={(enabled) => dispatch(isLogin ? "service_provider.patch" : "provider.patch", isLogin ? { provider_id: id, provider: { enabled } } : { provider_id: id, changes: { enabled } })} /></View>
    {kind === "apiKey" || (kind === "relay" && !station) ? <ProviderSourceFields provider={provider} providerID={id} relayStations={relayStations} busy={busy} translate={translate} dispatch={dispatch} onBaseUrlDraftChange={(value) => drafts?.setProviderBaseUrlDraft(id, value)} onNameDraftChange={(value) => { if (drafts) drafts.setProviderNameDraft(id, value); else onNameDraftChange?.(value); }} /> : null}
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
    {kind !== "openai" && kind !== "claude" ? <ProviderKeysPanel
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
      paneViewportHeight={editorViewportHeight}
      paneContentHeight={editorContentHeight}
      variant="inline"
    /> : null}
    {isLogin ? <View style={styles.officialAccountSection}>
      <View style={styles.panelHeader}><Text style={styles.panelTitle}>{translate("providers.accounts")}</Text></View>
      <View style={styles.officialStatusRow}>
        <Text style={styles.providerAuthStatusLabel}>{translate("providers.authStatus")}</Text>
        <Text style={styles.providerAuthStatusValue}>{statusLabels[authStatus]}</Text>
      </View>
      <TextField
        key={`official-name:${id}`}
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
      <Text style={styles.providerAuthLine}>{translate("relay.officialProviderModels")}: {modelNameText}</Text>
      <Text style={styles.keysHint}>{kind === "openai" ? translate("relay.officialProviderWebViewHint") : translate("relay.officialProviderBrowserHint")}</Text>
      {kind === "openai" && authActive ? <Text style={styles.officialActiveHint}>{translate("relay.officialProviderActive")}</Text> : null}
      {kind === "openai" && authStatus === "signed_in" && !authActive ? <Text style={styles.keysHint}>{translate("relay.officialProviderRestartHint")}</Text> : null}
      <View style={styles.officialActionsRow}>
        {kind === "openai" && authStatus === "signed_in" && !authActive ? <NativeButton title={translate("relay.officialProviderActivate")} compact disabled={busy || loginBusy} onPress={() => { void activateProvider(); }} /> : null}
        <NativeButton title={authLabel} primary compact disabled={busy || loginBusy} onPress={() => { void runAuthAction(); }} />
        <NativeButton title={translate("relay.officialProviderAddLogin")} compact disabled={busy || loginBusy} onPress={() => { void addSiblingAccount(); }} />
      </View>
      {kind === "claude" && authStatus === "error" ? <NativeSecretField autoCommit label={translate("providers.authTypeClaude")} hint={translate("relay.officialProviderTokenHint")} busy={busy} disabled={busy} domain="providers_models" field="provider_auth_token" target={id} onSecretState={onSecretState} /> : null}
    </View> : null}
    {kind !== "openai" && kind !== "claude" && station ? <StationAccountsPanel
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
      detectType={relay.detectType}
      stationDraft={stationDraft}
      onStationDraftChange={setStationDraftValue}
      onStageStationUpdate={stageStationUpdate}
      onStatus={onStatus}
    /> : null}
    {kind !== "openai" && kind !== "claude" && !station ? <View style={styles.providerAccountsHeader}>
      <View style={styles.panelHeader}>
        <Text style={styles.panelTitle}>{translate("providers.accounts")}</Text>
        <View style={styles.panelActions}>
          {vendorBaseURL ? <NativeButton title="" symbol="plus" compact toolTip={translate("providers.addRelayAccount")} accessibilityLabel={translate("providers.addRelayAccount")} disabled={busy || relayAddBusy} onPress={() => { void addRelayAccountToVendor(); }} style={styles.iconButton} /> : null}
        </View>
      </View>
    </View> : null}
    </View>
  </PersistentScrollView>;
}

function CodexWorkspace({ snapshot, busy, translate, dispatch, onSecretState }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void }): React.JSX.Element {
  const state = domainState(snapshot, "codex");
  const structured = asRecord(state.structured);
  const providerRows = asRecords(structured.providers).map(editableRecord);
  const deployments = asRecords(state.models);
  const deploymentModels = [...new Set(deployments.map((item) => stringValue(item.model)).filter(Boolean))];
  const directProvider = stringValue(structured.model_provider);
  const provider = providerRows.find((item) => identifier(item) === directProvider);
  const [modelDraft, setModelDraft] = useState<string>();
  const displayedModel = modelDraft ?? stringValue(structured.model);
  const gateway = directProvider === "openai"
    ? stringValue(structured.openai_base_url)
    : stringValue(provider?.base_url);

  useEffect(() => {
    setModelDraft((current) => current !== undefined && current === stringValue(structured.model) ? undefined : current);
  }, [structured.model]);

  const commitProvider = (nextProvider: string): Promise<void> => {
    const normalized = nextProvider.trim();
    if (!normalized) return dispatch("patch", { model_provider: null }, "codex");
    const selected = providerRows.find((item) => identifier(item) === normalized);
    const base_url = normalized === "openai" ? stringValue(structured.openai_base_url) : stringValue(selected?.base_url);
    const patch: UnknownRecord = { model_provider: normalized };
    if (normalized === "openai" || selected) {
      patch.direct_connection = { provider: normalized, base_url };
    }
    return dispatch("patch", patch, "codex");
  };

  const commitGateway = (base_url: string): Promise<void> => {
    if (directProvider === "openai") {
      return dispatch("patch", { model_provider: "openai", direct_connection: { provider: "openai", base_url } }, "codex");
    }
    if (!provider) return Promise.resolve();
    return dispatch("patch", { providers: providerRows.map((item) => identifier(item) === directProvider ? { ...item, base_url } : item) }, "codex");
  };

  return <View style={assistantSettingsStyles.domainBody}>
    <View style={assistantSettingsStyles.quickFields}>
      <TextField label={translate("codex.provider")} value={directProvider} disabled={busy} onCommit={commitProvider} />
      {deploymentModels.length > 0
        ? <PickerField label={translate("common.model")} value={displayedModel} values={deploymentModels} disabled={busy} onSelect={(model) => {
          setModelDraft(model);
          const row = deployments.find((item) => stringValue(item.model) === model);
          if (row) {
            const rawCompactionSupport = row.supports_responses_compaction;
            const selection: CodexModelSelection = {
              model: stringValue(row.model),
              provider: stringValue(row.provider),
              deployment_id: stringValue(row.deployment_id),
              supports_responses_compaction: typeof rawCompactionSupport === "boolean" ? rawCompactionSupport : null,
            };
            void dispatch("select_model", { selection }, "codex");
          } else void dispatch("patch", { model }, "codex");
        }} />
        : <TextField label={translate("common.model")} value={displayedModel} disabled={busy} onDraftChange={setModelDraft} onCommit={(model) => dispatch("patch", { model }, "codex")} />}
      <View style={styles.assistantFieldRow}>
        <TextField label={translate("codex.gateway")} value={gateway} disabled={busy || !directProvider} onCommit={commitGateway} style={styles.assistantFieldFlex} />
        <NativeButton title={translate("settings.useLocalApi")} compact disabled={busy || !directProvider} toolTip={translate("settings.useLocalApiHint")} accessibilityLabel={translate("settings.useLocalApi")} onPress={() => { void dispatch("use_local_api", {}, "codex"); }} />
      </View>
      <NativeSecretField plainText autoCommit label={translate("common.apiKey")} busy={busy} domain="codex" field="api_key" onSecretState={onSecretState} />
    </View>
  </View>;
}

function SettingsWorkspace({ validationStatus, validationStatusStyle, translate, missingMessage, structured, files }: { validationStatus?: string; validationStatusStyle?: StyleProp<TextStyle>; translate: Translate; missingMessage?: string; structured: React.ReactNode; files: React.ReactNode }): React.JSX.Element {
  return <View style={styles.codexWorkspaceFrame}>
    {validationStatus ? <Text style={[styles.codexValidationStatus, validationStatusStyle]}>{validationStatus}</Text> : null}
    {missingMessage ? <Text style={styles.settingsMissingMessage}>{missingMessage}</Text> : null}
    <PersistentScrollView style={styles.assistantSettingsScroll} contentContainerStyle={[styles.assistantSettingsScrollContent, assistantSettingsLayoutStyles.boundedContent]} horizontal={false} showsVerticalScrollIndicator>
      <View style={[styles.assistantQuickSection, assistantSettingsLayoutStyles.boundedSection]}>
        <View style={[styles.assistantSectionHeader, assistantSettingsLayoutStyles.boundedSection]}>
          <Text style={styles.paneHeading}>{translate("settings.structured")}</Text>
          <Text style={styles.assistantSectionHint}>{translate("settings.basicFieldsHint")}</Text>
        </View>
        {structured}
      </View>
      <View style={[assistantFileSurfaceStyles.filesSection, assistantSettingsLayoutStyles.boundedSection]}>
        <View style={[styles.assistantSectionHeader, assistantSettingsLayoutStyles.boundedSection]}>
          <Text style={styles.paneHeading}>{translate("settings.files")}</Text>
          <Text style={styles.assistantSectionHint}>{translate("settings.filesHint")}</Text>
        </View>
        {files}
      </View>
    </PersistentScrollView>
  </View>;
}

function ClaudeScreen({ snapshot, busy, translate, dispatch, onSecretState }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void }): React.JSX.Element {
  const state = domainState(snapshot, "claude");
  const desktop = asRecord(state.desktop);
  const desktopAvailable = desktop.available !== false;
  const desktopProvider = stringValue(desktop.provider);
  const desktopModelNames = stringList(desktop.model_names);
  const desktopModel = desktopModelNames[0] ?? "";
  const updateDesktopModel = (value: string): Promise<void> => dispatch("desktop_models_patch", { model_names: splitLines(value) }, "claude");
  const updateDesktopProvider = (inferenceProvider: string): Promise<void> => dispatch("desktop_patch", { inferenceProvider: inferenceProvider || null }, "claude");
  const updateDesktopGateway = (inferenceGatewayBaseUrl: string): Promise<void> => dispatch("desktop_patch", { inferenceGatewayBaseUrl }, "claude");
  return <View style={assistantSettingsStyles.domainBody}>
    <View style={assistantSettingsStyles.subsection}>
      <View style={assistantSettingsStyles.subsectionHeader}>
        <Text style={assistantSettingsStyles.subsectionTitle}>{translate("claude.desktopSection")}</Text>
        <Text style={assistantSettingsStyles.subsectionHint}>{translate("claude.desktopSectionHint")}</Text>
      </View>
      <View style={assistantSettingsStyles.quickFields}>
        <PickerField label={translate("claude.desktopProvider")} value={desktopProvider} values={[{ value: "", label: translate("common.none") }, "gateway", "anthropic", "bedrock", "vertex", "foundry"]} disabled={busy || !desktopAvailable} onSelect={(value) => { void updateDesktopProvider(value); }} />
        <TextField label={translate("common.model")} value={desktopModel} disabled={busy || !desktopAvailable} onCommit={(value) => { void updateDesktopModel(value); }} />
        <View style={styles.assistantFieldRow}>
          <TextField label={translate("claude.desktopGateway")} value={stringValue(desktop.gateway_url)} disabled={busy || !desktopAvailable} onCommit={(value) => { void updateDesktopGateway(value); }} style={styles.assistantFieldFlex} />
          <NativeButton title={translate("settings.useLocalApi")} compact disabled={busy || !desktopAvailable} toolTip={translate("settings.useLocalApiHint")} accessibilityLabel={translate("settings.useLocalApi")} onPress={() => { void dispatch("use_local_api", {}, "claude"); }} />
        </View>
        <NativeSecretField plainText autoCommit label={translate("common.apiKey")} busy={busy || !desktopAvailable} domain="claude" field="desktop_gateway_api_key" onSecretState={onSecretState} />
      </View>
    </View>
    <View style={assistantSettingsStyles.subsection}>
      <View style={assistantSettingsStyles.subsectionHeader}>
        <Text style={assistantSettingsStyles.subsectionTitle}>{translate("claude.codeSection")}</Text>
        <Text style={assistantSettingsStyles.subsectionHint}>{translate("claude.codeSectionHint")}</Text>
      </View>
    </View>
  </View>;
}

function AssistantFileEditorDialog({ target, ipc, busy, translate, onEditorConflict, rawReloadToken, rawBaselineToken, syncRevision, onFlushPendingFields, onSave, onClose }: { target?: AssistantFileTarget; ipc: IpcClient; busy: boolean; translate: Translate; onEditorConflict: RawEditorConflictHandler; rawReloadToken: number; rawBaselineToken: number; syncRevision?: number; onFlushPendingFields: () => Promise<void>; onSave: () => Promise<void>; onClose: () => void }): React.JSX.Element {
  if (!target) return <></>;
  const close = (): void => {
    // Close stages the editor text without writing it; the pane applies the
    // finished draft after the sheet is gone.
    void onFlushPendingFields().then(onClose).catch(() => undefined);
  };
  const save = (): void => {
    void onSave().then(onClose).catch(() => undefined);
  };
  return <View style={assistantFileSurfaceStyles.editorLayer} accessibilityViewIsModal onAccessibilityEscape={close}>
    <View style={assistantFileSurfaceStyles.editorDialog}>
      <View style={assistantFileSurfaceStyles.editorHeader}>
        <View style={assistantFileSurfaceStyles.editorHeaderCopy}>
          <Text style={assistantFileSurfaceStyles.editorTitle}>{target.label}</Text>
          <Text style={assistantFileSurfaceStyles.editorHint}>{translate("settings.fileEditorHint")}</Text>
        </View>
      </View>
      <RawEditor showLabel={false} showDiff codexPane syncRevision={syncRevision} style={assistantFileSurfaceStyles.editorRaw} label={target.label} domain={target.domain} document={target.document} language={target.language} ipc={ipc} translate={translate} onConflict={onEditorConflict} reloadToken={rawReloadToken} baselineToken={rawBaselineToken} />
      <View style={assistantFileSurfaceStyles.editorFooter}>
        <ActionButton title={translate("status.close")} disabled={busy} onPress={close} />
        <ActionButton primary title={translate("common.save")} disabled={busy} onPress={save} />
      </View>
    </View>
  </View>;
}

function AssistantSettingsWorkspace({ snapshot, busy, translate, dispatch, onSecretState, onOpenFile }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onOpenFile: (target: AssistantFileTarget) => void }): React.JSX.Element {
  const codexState = domainState(snapshot, "codex");
  const claudeState = domainState(snapshot, "claude");
  const codexErrors = stringList(codexState.validation_errors);
  const codexWarnings = stringList(codexState.warnings);
  const codexValidation = codexErrors.length > 0
    ? codexErrors.map((message) => localizeCodexValidationMessage(message, translate)).join("\n")
    : codexWarnings.map((message) => localizeCodexValidationMessage(message, translate)).join("\n");
  const claudeUnavailable = claudeState.available === false ? translate("settings.claudeUnavailable") : "";
  const validationStatus = [codexValidation, claudeUnavailable].filter(Boolean).join("\n") || undefined;
  const validationStatusStyle = codexErrors.length > 0 || claudeUnavailable ? styles.codexValidationError : codexWarnings.length > 0 ? styles.codexValidationWarning : undefined;
  const missingMessage = [
    codexState.config_exists === false ? translate("settings.codexMissing") : "",
    asRecord(claudeState.settings).file_exists === false ? translate("settings.claudeMissing") : "",
  ].filter(Boolean).join("\n") || undefined;

  const fileRow = (target: AssistantFileTarget): React.JSX.Element => <View key={`${target.domain}:${target.document}`} style={assistantFileSurfaceStyles.fileRow}>
    <View style={assistantFileSurfaceStyles.fileMeta}>
      <Text style={assistantFileSurfaceStyles.fileLabel}>{target.label}</Text>
      <Text style={assistantFileSurfaceStyles.fileHint}>{target.language.toUpperCase()}</Text>
    </View>
    <NativeButton title={translate("settings.editFile")} accessibilityLabel={`${translate("settings.editFile")}: ${target.label}`} disabled={busy} onPress={() => onOpenFile(target)} style={assistantFileSurfaceStyles.editFileButton} />
  </View>;
  const fileGroup = (title: string, hint: string, targets: AssistantFileTarget[]): React.JSX.Element => <View style={assistantSettingsStyles.fileGroup}>
    <View style={assistantSettingsStyles.fileGroupHeader}>
      <Text style={assistantSettingsStyles.fileGroupTitle}>{title}</Text>
      <Text style={assistantSettingsStyles.fileGroupHint}>{hint}</Text>
    </View>
    {targets.map(fileRow)}
  </View>;
  const fileTargets = {
    codex: [
      { domain: "codex", document: "config", language: "toml", label: translate("codex.rawToml") },
      { domain: "codex", document: "auth", language: "json", label: translate("codex.rawAuth") },
    ],
    desktop: [
      { domain: "claude", document: "desktop", language: "json", label: translate("claude.desktopRawJson") },
      { domain: "claude", document: "developer", language: "json", label: translate("claude.developerRawJson") },
    ],
    code: [
      { domain: "claude", document: "settings", language: "json", label: translate("claude.codeRawJson") },
    ],
  } satisfies Record<string, AssistantFileTarget[]>;
  return <SettingsWorkspace validationStatus={validationStatus} validationStatusStyle={validationStatusStyle} translate={translate} missingMessage={missingMessage} structured={<View style={[styles.assistantQuickGrid, assistantSettingsLayoutStyles.boundedGrid]}>
    <View style={assistantSettingsStyles.domainCard}><View style={assistantSettingsStyles.domainHeader}><Text style={assistantSettingsStyles.cardTitle}>{translate("card.codexSettings")}</Text></View><CodexWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} /></View>
    <View style={assistantSettingsStyles.domainCard}><View style={assistantSettingsStyles.domainHeader}><Text style={assistantSettingsStyles.cardTitle}>{translate("card.claudeSettings")}</Text></View><ClaudeScreen snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} /></View>
  </View>} files={<View style={assistantFileSurfaceStyles.fileGroups}>
    {fileGroup(translate("card.codexSettings"), translate("settings.codexFilesHint"), fileTargets.codex)}
    {fileGroup(translate("claude.desktopSection"), translate("settings.claudeDesktopFilesHint"), fileTargets.desktop)}
    {fileGroup(translate("claude.codeSection"), translate("settings.claudeCodeFilesHint"), fileTargets.code)}
  </View>} />;
}

function GeneralWorkspace({ snapshot, ipc, native, busy, dispatch, dispatchServiceAction, translate, onStatus, onSnapshot }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; dispatch: Dispatch; dispatchServiceAction: (type: string) => Promise<unknown>; translate: Translate; onStatus: (message?: string) => void; onSnapshot: (next: CoreSnapshot) => void }): React.JSX.Element {
  const [autoStartBusy, setAutoStartBusy] = useState(false);
  const [requestedAutoStart, setRequestedAutoStart] = useState<boolean>();
  const autoStartEnabled = snapshot?.service.auto_start_state === "enabled";
  // The switch shows Core's stored preference, plus the value the user just
  // requested until Core confirms it. A rejected dispatch must not leave the
  // native switch claiming a preference Core never accepted.
  const autoStartValue = requestedAutoStart ?? autoStartEnabled;
  useEffect(() => {
    if (requestedAutoStart !== undefined && requestedAutoStart === autoStartEnabled) setRequestedAutoStart(undefined);
  }, [autoStartEnabled, requestedAutoStart]);
  const serviceState = snapshot?.service.state ?? "unknown";
  const runtimeSettings = asRecords(domainState(snapshot, "runtime").settings);
  const portItem = runtimeSettings.find((item) => identifier(item) === "LITELLM_PORT");
  const portValue = stringValue(portItem?.value, "");
  const portDefault = stringValue(portItem?.default, "12389");
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
      await native.setLaunchAtLogin(enabled);
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
  return <PersistentScrollView style={styles.generalScroll} contentContainerStyle={styles.generalContent}>
    <View style={styles.generalSection}>
      <Text style={styles.generalSectionTitle}>{translate("general.startup")}</Text>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.autoStart")}</Text>
        <NativeToggle value={autoStartValue} disabled={autoStartBusy || busy || snapshot === undefined} accessibilityLabel={translate("general.autoStart")} onValueChange={(next) => { void setAutoStart(next); }} style={styles.generalToggle} />
      </View>
      <Text style={styles.generalRowHint}>{translate("general.autoStartHint")}</Text>
    </View>
    <View style={styles.generalSection}>
      <Text style={styles.generalSectionTitle}>{translate("general.service")}</Text>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.serviceState")}</Text>
        <Text numberOfLines={1} style={styles.generalRowValue}>{serviceLabel}</Text>
      </View>
      <Text style={styles.generalRowHint}>{translate("general.serviceHint")}</Text>
      <View style={styles.generalRow}>
        <Text style={styles.generalRowLabel}>{translate("general.port")}</Text>
        <RuntimeValueField label={translate("general.port")} value={portValue} keyboardType="numeric" validate={validatePort} onCommit={(next) => dispatch("set_setting", { key: "LITELLM_PORT", value: next })} />
      </View>
      <Text style={styles.generalRowHint}>{translate("general.portHint", { default: portDefault })}</Text>
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
  // A visible table of contents instead of a hidden popup: every category is
  // one click away and the list follows the scroll position, so a 71-setting
  // pane stays navigable while each setting keeps its help on one compact row.
  const [activeCategory, setActiveCategory] = useState("");
  const [sectionOffsets, setSectionOffsets] = useState<Record<string, number>>({});
  const scrollRef = useRef<ScrollView>(null);
  const currentCategory = categories.includes(activeCategory) ? activeCategory : (categories[0] ?? "");
  const tocRows = useMemo(() => categories.map((name) => ({ key: name, cells: [runtimeCategoryLabel(name, translate)] })), [categories, translate]);
  const dshSyncToken = DSH_VISION_ROUTER_QUICK_KEYS.map((key) => `${key}:${stringValue(settings.find((item) => identifier(item) === key)?.value)}`).join("|");
  const jumpToCategory = (name: string): void => {
    // The table of contents always tracks the pane's scrolling surface, so a
    // cleared click keeps the current category instead of untracking it.
    if (!name) return;
    setActiveCategory(name);
    const offset = sectionOffsets[name];
    // Position instantly: a table-of-contents jump should land exactly on the
    // section rather than animate through every row in between.
    if (offset !== undefined) scrollRef.current?.scrollTo({ y: Math.max(0, offset - 2), animated: false });
  };
  const trackScroll = ({ nativeEvent }: NativeSyntheticEvent<NativeScrollEvent>): void => {
    const position = nativeEvent.contentOffset.y + 12;
    let current = categories[0] ?? "";
    for (const name of categories) {
      const offset = sectionOffsets[name];
      if (offset !== undefined && offset <= position) current = name;
    }
    setActiveCategory(current);
  };
  return <View style={styles.runtimeWorkspaceFrame}>
    <View style={styles.runtimeWorkspaceBody}>
      <View style={styles.runtimeToc}>
        <NativeTable
          columns={[{ label: "", width: 148 }]}
          rows={tocRows}
          selectedKey={currentCategory}
          striped={false}
          compact
          framed={false}
          sourceList
          cellHorizontalPadding={8}
          firstColumnHorizontalPadding={8}
          onSelectionChange={jumpToCategory}
          style={styles.runtimeTocList}
        />
      </View>
      <PersistentScrollView ref={scrollRef} style={styles.runtimeScrollSurface} contentContainerStyle={styles.runtimeWorkspace} onScroll={trackScroll} scrollEventThrottle={32}>
        {categories.length === 0 ? <EmptyState translate={translate} /> : categories.map((name) => <View key={name} style={styles.runtimeSection} onLayout={({ nativeEvent }) => { setSectionOffsets((current) => current[name] === nativeEvent.layout.y ? current : { ...current, [name]: nativeEvent.layout.y }); }}>
          <Text style={styles.runtimeSectionTitle}>{runtimeCategoryLabel(name, translate)}</Text>
          <View style={styles.runtimeFieldList}>{(groups[name] ?? []).map((item) => <RuntimeField key={identifier(item)} item={item} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} dshSyncToken={dshSyncToken} />)}</View>
        </View>)}
      </PersistentScrollView>
    </View>
  </View>;
}

function DataManagementWorkspace({ snapshot, busy, webDavOperationBusy, statuses, translate, dispatch, onSecretState, onFlushPendingFields, onTabSwitchError, onInspectImport, onImport, onConfirmImportReplace, onExport, onProbeWebDav, onSyncWebDav }: {
  snapshot?: CoreSnapshot;
  busy: boolean;
  webDavOperationBusy: boolean;
  statuses: Partial<Record<DataManagementTab, string>>;
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
}): React.JSX.Element {
  const [tab, setTab] = useState<DataManagementTab>("import");
  const [importPreview, setImportPreview] = useState<IpcResults["import_preview"]>();
  const [importSections, setImportSections] = useState<ConfigDomain[]>([]);
  const [stagedSections, setStagedSections] = useState<ConfigDomain[]>([]);
  const [exportSections, setExportSections] = useState<ConfigDomain[]>([...DATA_PACKAGE_DOMAINS]);
  const [syncAction, setSyncAction] = useState<WebDavSyncAction>("sync");
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
    <View style={styles.dataManagementRail}>
      <NativeTable
        columns={[{ label: "", width: 132 }]}
        rows={dataManagementTabRows}
        selectedKey={tab}
        striped={false}
        compact
        framed={false}
        sourceList
        cellHorizontalPadding={8}
        firstColumnHorizontalPadding={8}
        onSelectionChange={(key) => { if (key) switchDataManagementTab(key as DataManagementTab); }}
        style={styles.dataManagementRailList}
      />
    </View>
    <View style={styles.dataManagementDetail}>
    {tab === "import" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>
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
        <View style={styles.dataManagementSelectionBar}><Text style={[styles.dataManagementSelectionCount, dataManagementPolishStyles.compactText]}>{translate("dataManagement.selectedCount", { count: stagedSections.length })}</Text></View>
          {sectionList(stagedSections, stagedSections, true, () => undefined)}
      </DataManagementGroup> : null}
      {statuses.import ? <Text style={[styles.dataManagementStatus, dataManagementPolishStyles.compactText]}>{statuses.import}</Text> : null}
    </PersistentScrollView> : null}
    {tab === "export" ? <View style={styles.dataManagementPane}>
      <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>
        <View style={dataManagementPolishStyles.paneIntro}><Text style={dataManagementPolishStyles.paneHeading}>{translate("dataManagement.exportContent")}</Text><Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.exportHint")}</Text></View>
        <DataManagementGroup>
          <View style={styles.dataManagementSelectionBar}><Text style={[styles.dataManagementSelectionCount, dataManagementPolishStyles.compactText]}>{translate("dataManagement.selectedCount", { count: exportSections.length })}</Text><View style={styles.dataManagementToolbarButtons}>{selectionTool(exportSections.length, DATA_PACKAGE_DOMAINS.length, () => setExportSections([...DATA_PACKAGE_DOMAINS]), () => setExportSections([]))}</View></View>
          {sectionList(DATA_PACKAGE_DOMAINS, exportSections, busy, (domain, enabled) => toggleSection(setExportSections, domain, enabled))}
        </DataManagementGroup>
      </PersistentScrollView>
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
      <WebDavWorkspace snapshot={snapshot} busy={busy || webDavOperationBusy} status={statuses.webdav} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onProbe={onProbeWebDav}>
        <View style={[styles.dataManagementSyncContent, dataManagementPolishStyles.syncContent]}>
          <View style={styles.dataManagementSyncScope}><Text style={styles.dataManagementSyncScopeLabel}>{translate("dataManagement.webdavScope")}</Text><Text numberOfLines={2} style={styles.dataManagementSyncScopeValue}>{translate("dataManagement.section.providersModels")} · {translate("dataManagement.section.relayAccounts")}</Text></View>
          <View style={styles.dataManagementDirection}><Text style={styles.dataManagementDirectionLabel}>{translate("dataManagement.syncDirection")}</Text><NativePicker labels={syncOptions.map(({ title }) => title)} selectedValue={selectedSyncLabel} disabled={busy || webDavOperationBusy} onChange={({ nativeEvent }) => { const option = syncOptions[nativeEvent.index]; if (option) setSyncAction(option.id); }} style={styles.dataManagementDirectionPicker} /><ActionButton title={translate("dataManagement.syncNow")} disabled={busy || webDavOperationBusy || snapshot?.webdav.enabled !== true} onPress={() => { void onSyncWebDav(syncAction); }} /></View>
        </View>
      </WebDavWorkspace>
    </View> : null}
    </View>
  </View>;
}

function DataManagementGroup({ children, style }: { children?: React.ReactNode; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <View style={[styles.dataManagementGroup, style]}>
    {children ? <View style={[styles.dataManagementGroupBody, dataManagementPolishStyles.groupBody]}>{children}</View> : null}
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
        <View style={[styles.runtimeModifiedBar, styles.runtimeModifiedBarInline, modified && styles.runtimeModifiedBarActive]} />
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

function WebDavWorkspace({ snapshot, busy, status, translate, dispatch, onSecretState, onProbe, children }: { snapshot?: CoreSnapshot; busy: boolean; status?: string; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; onProbe: () => Promise<void>; children: React.ReactNode }): React.JSX.Element {
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
      <View style={[styles.webdavActionRow, dataManagementPolishStyles.webDavActionRow]}><ActionButton title={translate("dataManagement.testConnection")} disabled={busy} onPress={() => { void onProbe(); }} />{status ? <Text numberOfLines={1} style={[styles.webdavActionStatus, dataManagementPolishStyles.compactText]}>{status}</Text> : null}</View>
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

function logColumns(tab: LogTab, translate: Translate): LogColumn[] {
  const time = { label: translate("logs.localTime"), width: 164, value: (row: RenderedLogRecord) => row.time };
  const status = { label: translate("common.status"), width: 68, value: (row: RenderedLogRecord) => row.status };
  const detail = { label: translate("logs.detail"), width: 260, flex: true, value: (row: RenderedLogRecord) => row.detail };
  if (tab === "requests") return [
    { ...time, width: 148 },
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
      : <View style={styles.logTableFrame} onLayout={({ nativeEvent }) => setTableWidth(nativeEvent.layout.width)}><NativeTable columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths scrollTrailingColumnOverflow onSelectionChange={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} onRowDoublePress={(_key, index) => {
        const row = rows[index];
        if (!row) return;
        void native.showReadOnlyText({ title: translate("logs.originalRecord"), text: row.original, closeLabel: translate("status.close"), language: "json", html: CODE_EDITOR_HTML });
      }} style={styles.logTable} /></View>
      : <View style={styles.logEmptySurface}><Text style={styles.logEmptyText}>{clearing || active ? translate("logs.empty") : translate("logs.loading")}</Text></View>}
  </View>;
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

function RawEditor({ label, domain, document, language, ipc, translate, showLabel = true, showDiff = true, codexPane = false, onConflict, reloadToken = 0, baselineToken = 0, syncRevision, style }: { label: string; domain: "codex" | "claude"; document: RawEditorDocument; language: "toml" | "json"; ipc: IpcClient; translate: Translate; showLabel?: boolean; showDiff?: boolean; codexPane?: boolean; onConflict: RawEditorConflictHandler; reloadToken?: number; baselineToken?: number; syncRevision?: number; style?: StyleProp<ViewStyle> }): React.JSX.Element {
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
    }).catch(() => {
      if (!active) return;
      setLoading(false);
      setError(translate("error.coreUnavailable"));
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
    model_name_required: "validation.modelNameRequired",
    model_upstream_required: "validation.modelUpstreamRequired",
    revision_conflict: "error.revisionConflict",
  };
  return <View style={styles.issueBox}><Text style={styles.sectionTitle}>{translate("common.validationIssues")}</Text>{issues.map((issue, index) => {
    const message = keyByCode[issue.code] ? translate(keyByCode[issue.code]) : issue.message || translate("error.validationFailed");
    return <Text key={`${issue.path}-${index}`} style={styles.issue}>{issue.path ? `${issue.path}: ${message}` : message}</Text>;
  })}</View>;
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
function hasBooleanSetting(value: UnknownRecord, key: string): boolean { return typeof value[key] === "boolean"; }
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

// Data-management is a utility window, but its active pane still needs a
// readable rhythm. Keep these adjustments together so the three panes share
// the same inset, helper-copy treatment, and native-preference spacing.
const dataManagementPolishStyles = StyleSheet.create({
  tabBar: { height: 42, minHeight: 42 },
  tabs: { width: 272, height: 28 },
  paneScrollContent: { flexGrow: 1, paddingTop: 14, paddingHorizontal: 12, paddingBottom: 14, gap: 14 },
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

const assistantSettingsStyles = {
  domainCard: { minWidth: 0, alignSelf: "stretch" as const, gap: 10, paddingVertical: 12 },
  domainHeader: { gap: 2, paddingBottom: 6, borderBottomWidth: 1, borderBottomColor: systemColors.separator },
  cardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" as const },
  domainHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  domainBody: { gap: 8 },
  quickFields: { gap: 8 },
  subsection: { gap: 7, paddingTop: 8, borderTopWidth: 1, borderTopColor: systemColors.separator },
  subsectionHeader: { gap: 2 },
  subsectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" as const },
  subsectionHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  fileGroup: { gap: 5 },
  fileGroupHeader: { gap: 2, paddingBottom: 6, borderBottomWidth: 1, borderBottomColor: systemColors.separator },
  fileGroupTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" as const },
  fileGroupHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
};

const assistantSettingsLayoutStyles = StyleSheet.create({
  boundedContent: { width: "100%", minWidth: 0, alignSelf: "stretch" },
  boundedSection: { minWidth: 0 },
  boundedGrid: { width: "100%", minWidth: 0 },
});

const assistantFileSurfaceStyles = StyleSheet.create({
  filesSection: { gap: 8, paddingTop: 2, borderTopWidth: 1, borderTopColor: systemColors.separator },
  fileGroups: { gap: 10 },
  fileRow: { minHeight: 36, flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 4, borderBottomWidth: 1, borderBottomColor: systemColors.separator },
  fileMeta: { flex: 1, minWidth: 0, gap: 2 },
  fileLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  fileHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  editFileButton: { minWidth: 92 },
  editorLayer: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, zIndex: 100, alignItems: "center", justifyContent: "center", padding: 20, backgroundColor: "rgba(0, 0, 0, 0.28)" },
  editorDialog: { width: "94%", height: "90%", minWidth: 620, minHeight: 380, maxWidth: 1200, maxHeight: 780, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 7, overflow: "hidden", backgroundColor: systemColors.window, shadowColor: "#000000", shadowOpacity: 0.22, shadowRadius: 18, shadowOffset: { width: 0, height: 8 }, elevation: 8 },
  editorHeader: { minHeight: 52, flexDirection: "row", alignItems: "center", gap: 10, paddingHorizontal: 14, paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: systemColors.separator, backgroundColor: systemColors.window },
  editorHeaderCopy: { flex: 1, minWidth: 0, gap: 2 },
  editorTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  editorHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  editorRaw: { flex: 1, minHeight: 0 },
  editorFooter: { minHeight: 50, flexShrink: 0, flexDirection: "row", justifyContent: "flex-end", alignItems: "center", paddingHorizontal: 12, paddingVertical: 9, borderTopWidth: 1, borderTopColor: systemColors.separator, backgroundColor: systemColors.window },
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
  root: { flex: 1, minWidth: 420 }, assistantFieldRow: { minWidth: 0, flexDirection: "row", alignItems: "flex-end", gap: 8 }, assistantFieldFlex: { flex: 1, minWidth: 0 },
  menuBarHost: { flex: 1 }, error: { margin: 20, color: systemColors.red, fontSize: UI_FONT_SIZE },
  windowSurface: { flex: 1, position: "relative", backgroundColor: systemColors.window }, windowContent: { flexGrow: 1, paddingHorizontal: 16, paddingTop: 12, paddingBottom: 6, gap: 8 }, windowContentFixed: { flex: 1, minHeight: 0 }, providersContent: { paddingBottom: 6, gap: 6 }, providerWizardRouteContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0, gap: 0 }, providerWizardSurface: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.window }, settingsContent: { paddingHorizontal: 16, paddingTop: 6, paddingBottom: 0, gap: 6 }, logsContent: { paddingHorizontal: 12, paddingTop: 8, paddingBottom: 0 }, runtimeContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 }, dataManagementContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 }, windowTitleBlock: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 3, gap: 3 }, windowTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, validationText: { color: systemColors.red, fontSize: UI_FONT_SIZE },
  // Settings window: a native source-list sidebar next to the active pane.
  settingsShell: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row" }, settingsSidebar: { width: 200, flexShrink: 0, minHeight: 0, borderRightWidth: 1, borderRightColor: systemColors.separator }, settingsSidebarHeader: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 16, paddingTop: SETTINGS_TITLEBAR_INSET + 10, paddingBottom: 8 }, settingsSidebarAppIcon: { width: 20, height: 20, borderRadius: 4 }, settingsSidebarTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, settingsSidebarDivider: { height: 1, flexShrink: 0, marginHorizontal: 12, backgroundColor: systemColors.separator }, settingsSidebarSpacer: { flex: 1, minHeight: 8 }, settingsSidebarList: { flex: 1, minHeight: 0 }, settingsDetail: { minWidth: 0, flex: 1, paddingTop: SETTINGS_TITLEBAR_INSET }, settingsDetailBody: { flex: 1, minHeight: 0, backgroundColor: systemColors.textBackground }, settingsDetailBodyBare: { backgroundColor: "transparent" }, settingsDetailPane: { flex: 1, minWidth: 0 }, settingsPaneHeader: { flexShrink: 0, paddingHorizontal: 20, paddingTop: 10, paddingBottom: 8 }, settingsPaneTitle: { color: systemColors.label, fontSize: 15, fontWeight: "600" }, settingsPaneDivider: { height: 1, flexShrink: 0, backgroundColor: systemColors.separator },  routeStatusBar: { minHeight: 24, flexShrink: 0, justifyContent: "center", paddingHorizontal: 16, paddingVertical: 4, borderTopWidth: 1, borderTopColor: systemColors.separator }, routeStatusText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  generalScroll: { flex: 1, minHeight: 0, backgroundColor: systemColors.textBackground }, generalContent: { paddingHorizontal: 20, paddingTop: 14, paddingBottom: 16, gap: 18 }, generalSection: { gap: 4 }, generalSectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, generalRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, generalRowLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "right" }, generalRowValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, generalToggle: { width: 44, minWidth: 44, height: 24, marginLeft: 6 }, generalRowHint: { marginLeft: 136, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, providerToolbar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardToolbarButton: { minWidth: 104 }, toolbarSpacer: { flex: 1 }, windowTabs: { width: 224, height: 24 }, windowTab: {}, windowTabSelected: {}, windowTabText: {},
  providerWizardSetupContent: { flex: 1, minHeight: 0, justifyContent: "flex-start", alignItems: "center", paddingHorizontal: 24, paddingTop: 18, paddingBottom: 12 }, providerWizardSetupSurface: { width: "100%", maxWidth: 520, minWidth: 0, gap: 12 }, providerWizardSetupSurfaceModel: { flex: 1, minHeight: 0 }, providerWizardSignInPanel: { width: "100%", minHeight: 160, justifyContent: "center", gap: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 7, backgroundColor: systemColors.control, paddingHorizontal: 16, paddingVertical: 18 }, providerWizardAuthRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 8 }, providerWizardAuthStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  providerMiddlePane: { flex: 1, minWidth: 0, gap: 6 },
  keysSection: { flex: 3, minHeight: 170 }, keysSectionContent: { paddingBottom: 4, gap: 4 }, keysSectionLogin: { flex: 0, minHeight: 40 }, modelPane: { flex: 1, minWidth: 0, minHeight: 130, paddingTop: 2, borderTopWidth: 1, borderTopColor: systemColors.separator },
  keysPane: { flex: 1, minWidth: 0, minHeight: 0 }, keysInline: { minWidth: 0, gap: 6, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator }, keysTableInline: { flex: 0, flexShrink: 0 },
  keysTable: { flex: 1, minHeight: 120 },
  keysEditor: { minWidth: 0, gap: 5, paddingTop: 5, paddingLeft: 8, borderLeftWidth: 2, borderLeftColor: systemColors.separator },
  keysEditorRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" },
  keysEditorField: { flex: 1, minWidth: 0 },
  keysGroupPicker: { flex: 1, minWidth: 120, height: 26 },
  keysSecret: { flex: 1, minWidth: 0, minHeight: 26 },
  keysHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, flexShrink: 1 },
  panelActionButton: { width: 22, minWidth: 22, height: 22 },
  toolbarCheckbox: { flexShrink: 0 },
  providerAccountsHeader: { minWidth: 0, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator },
  panelHeader: { minHeight: 22, flexDirection: "row", alignItems: "center", gap: 6 },
  panelTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "700" },
  panelActions: { marginLeft: "auto", flexDirection: "row", alignItems: "center", gap: 4 },
  relayBindingHint: { minHeight: 18 },
  officialAccountSection: { minWidth: 0, gap: 5, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator },
  officialStatusRow: { minHeight: 22, flexDirection: "row", alignItems: "center", gap: 6 },
  providerAuthLine: { color: systemColors.label, fontSize: UI_FONT_SIZE },
  officialActiveHint: { color: systemColors.green, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  officialActionsRow: { minHeight: 28, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 6 },
  accountsEmptyText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, textAlign: "center" }, providerWizardHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerWizardDescription: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 17 }, providerWizardSectionHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardPanelTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerWizardFormSection: { width: "100%", maxWidth: 520, paddingVertical: 0, gap: 10 }, providerWizardModelScroll: { flex: 1, minHeight: 0, width: "100%" }, providerWizardModelScrollContent: { width: "100%", paddingBottom: 8 }, providerWizardModelToolbar: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, providerWizardModelGroup: { gap: 6, paddingTop: 4 }, providerWizardModelGroupHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, providerWizardModelList: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, paddingHorizontal: 8, paddingVertical: 5, gap: 1 }, providerWizardModelCheckbox: { width: "100%", minHeight: 24 }, providerWizardManualModelRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, providerWizardManualModelCheckbox: { flex: 1, minWidth: 0 }, providerWizardManualModelUpstream: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE }, providerWizardModelSummary: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500", paddingTop: 4 }, providerWizardModeControl: { width: "100%", height: 26, flexShrink: 0 }, providerWizardPicker: { width: "100%", minWidth: 0, height: 26 }, providerWizardInput: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, providerWizardSecretInput: { width: "100%", minHeight: 26 }, providerWizardHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, paddingVertical: 2 }, providerWizardValidation: { color: systemColors.red, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, providerWizardFooter: { minHeight: 46, paddingHorizontal: 20, paddingVertical: 8, flexDirection: "row", alignItems: "center", gap: 6, borderTopWidth: 0, backgroundColor: systemColors.window }, providerWizardFooterSpacer: { flex: 1 }, providerWizardFooterStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, providerWizardFooterActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 },
  routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 },
  providerAuthFields: { minWidth: 0, gap: 4, paddingTop: 2 },
  providerAuthStatusRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  providerAuthStatusLabel: { width: 68, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  providerAuthStatusValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE },
  providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }, providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }, providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }, routeWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, fetchKeyPicker: { width: 170, height: 24, marginRight: 6, flexShrink: 0 }, providerThreePane: { flex: 1, minHeight: 0 }, providerListPane: { width: 140, minWidth: 140, maxWidth: 140, flexGrow: 0, flexShrink: 0 }, modelListPane: { flex: 1, minWidth: 0 }, providerInspectorPane: { minWidth: 280 }, tablePane: { flex: 1, minWidth: 0, gap: 6 }, tablePaneWide: { flex: 1, minWidth: 0 }, tableTitleRow: { height: 24, flexDirection: "row", alignItems: "center" }, tableTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, tableActions: { marginLeft: "auto", flexDirection: "row", gap: 6 }, iconButton: { minWidth: 22, width: 22, minHeight: 22, height: 22, alignItems: "center", justifyContent: "center" }, iconButtonText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, tableHeader: { height: 24, flexDirection: "row", alignItems: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.window }, tableHeaderText: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 6, fontWeight: "500" }, tableScroll: { flex: 1, minHeight: 0, borderWidth: 1, borderTopWidth: 0, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, tableRows: { flexGrow: 1 }, tableRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, tableRowSelected: { backgroundColor: systemColors.control }, tableCellText: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 6 }, providerNameColumn: { flex: 1 }, countColumn: { width: 48, textAlign: "right" }, modelNameColumn: { width: 96 }, modelUpstreamColumn: { flex: 1, minWidth: 112 }, routeModelColumn: { width: 136 }, routeOrderColumn: { width: 48, textAlign: "right" }, routeProviderColumn: { width: 112 }, routeUpstreamColumn: { flex: 1, minWidth: 136 }, tableBottomRow: { minHeight: 26, flexDirection: "row", alignItems: "center" }, nativeProviderTable: { flex: 1, minHeight: 0 }, nativeModelTable: { flex: 1, minHeight: 0 }, nativeRouteTable: { flex: 1, minHeight: 0 }, providerInspector: { width: 290, minWidth: 290, maxWidth: 290, flexGrow: 0, flexShrink: 0 }, providerEditorContent: { flex: 1, minHeight: 0 }, providerEditorScrollContent: { paddingTop: 3, paddingLeft: 0, paddingRight: 16, paddingBottom: 12, gap: 6 }, persistentScrollIndicator: { position: "absolute", width: 0, height: 0 }, providerEditorHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerEditorHeading: { flex: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerReturnToModel: { flexShrink: 1 }, providerEditorSection: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 3, gap: 4 }, providerEnabledRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, providerSourceFields: { minWidth: 0, gap: 4 }, inspectorContent: { paddingTop: 3, paddingLeft: 0, paddingRight: 6, paddingBottom: 12, gap: 6 }, inspectorBody: { gap: 4 }, modelBreadcrumb: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }, breadcrumbProvider: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, breadcrumbSeparator: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorHeading: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorDivider: { height: 1, backgroundColor: systemColors.separator }, inspectorEnabledRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, inspectorEnableControl: { flexShrink: 0 }, orderEditorRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, orderEditorField: { flex: 1, width: undefined }, orderFollowControl: { flexShrink: 0 }, probeSummaryTrigger: { flex: 1, minWidth: 0, minHeight: 22, justifyContent: "center" }, probeSummaryTriggerPressed: { opacity: 0.65 }, probeSummary: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, protocolSettings: { gap: 4 }, protocolHint: { marginLeft: 62, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 15 }, providerKeysEditor: { gap: 4 }, providerKeysHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerKeysHeading: { flex: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerKeyActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 }, providerKeyTable: { width: "100%", height: 112, minHeight: 112, flexShrink: 0 }, providerKeyFields: { minWidth: 0, gap: 4 },
  codexWorkspace: { flex: 1, minHeight: 0 }, codexWorkspaceFrame: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexValidationStatus: { flexShrink: 0, marginHorizontal: 8, fontSize: UI_FONT_SIZE }, settingsMissingMessage: { flexShrink: 0, marginHorizontal: 8, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, codexValidationWarning: { color: systemColors.brown }, codexValidationError: { color: systemColors.red }, assistantSettingsScroll: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.textBackground, borderWidth: 1, borderColor: systemColors.separator }, assistantSettingsScrollContent: { flexGrow: 1, paddingHorizontal: 14, paddingTop: 10, paddingBottom: 14, gap: 14 }, assistantQuickSection: { gap: 8 }, assistantSectionHeader: { flexDirection: "row", alignItems: "baseline", justifyContent: "space-between", gap: 12 }, assistantSectionHint: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, textAlign: "right" }, assistantQuickGrid: { flexDirection: "column", gap: 10 }, assistantRawSection: { gap: 10, paddingTop: 2, borderTopWidth: 1, borderTopColor: systemColors.separator }, assistantRawGrid: { flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", gap: 12 }, assistantRawEditor: { flex: 0, flexGrow: 1, flexShrink: 1, flexBasis: 480, minWidth: 360, height: 286, minHeight: 240 }, codexStructuredPane: { flex: 1, minWidth: 0, paddingHorizontal: 8 }, codexStructuredScroll: { flex: 1, minWidth: 0, marginTop: 7 }, codexStructuredScrollIndicator: { position: "absolute", width: 0, height: 0 }, codexStructured: { flexGrow: 1, flexShrink: 0, minWidth: SETTINGS_STRUCTURED_CONTENT_MIN_WIDTH, alignSelf: "stretch", gap: 14, paddingLeft: 16, paddingRight: 16 + SETTINGS_STRUCTURED_SCROLLBAR_GUTTER, paddingTop: 10, paddingBottom: 16 }, codexStructuredWithHorizontalScrollbar: { paddingBottom: 32 }, codexRawPane: { flex: 1, flexShrink: 1, minWidth: 320, minHeight: 0, gap: 8, paddingHorizontal: 8, overflow: "hidden" }, codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexRawEditorBase: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0, gap: 5 }, codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0 }, codexRawEditorHeader: { minHeight: 18 }, codexRawEditorLabel: { fontFamily: Platform.select({ macos: "Menlo", windows: "Cascadia Mono", default: "monospace" }), fontWeight: "600" }, codexRawNativeEditor: { minHeight: 0 }, codexRawEditorLoading: { minHeight: 0 }, paneHeading: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, sectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, codexProviderEditor: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 6, backgroundColor: systemColors.control, overflow: "hidden" }, codexProviderToolbar: { minHeight: 42, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12, paddingHorizontal: 10, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: systemColors.separator }, codexProviderToolbarTitle: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, codexProviderActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, codexProviderActionButton: { width: 30, minWidth: 30, height: 30, paddingHorizontal: 0 }, codexProviderSplit: { borderWidth: 0, borderRadius: 0 }, split: { flexDirection: "row", flexWrap: "wrap", borderWidth: 1, borderColor: systemColors.separator, minHeight: 150, backgroundColor: systemColors.textBackground }, codexListTable: { flex: 1, minWidth: 260, minHeight: 150 }, pluginEditor: { minHeight: 128, flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", gap: 12 }, pluginTable: { flex: 1, minWidth: 260, minHeight: 128 }, pluginFields: { flex: 1, minWidth: 220, gap: 7 }, masterPane: { width: "36%", minWidth: 220, borderRightWidth: 1, borderColor: systemColors.separator, padding: 8 }, detailPane: { flex: 1, minWidth: 240, padding: 12 }, listRow: { minHeight: 28, paddingHorizontal: 8, paddingVertical: 5 }, listRowSelected: { backgroundColor: systemColors.control }, listText: { flex: 1 },
  runtimeWorkspaceFrame: { flex: 1, minHeight: 0, gap: 6 }, runtimeWorkspaceBody: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 8 }, runtimeToc: { width: 156, flexShrink: 0, minHeight: 0, paddingTop: 6, borderRightWidth: 1, borderRightColor: systemColors.separator }, runtimeTocList: { flex: 1, minHeight: 0 }, runtimeWorkspace: { width: "100%", padding: 14, gap: 14 }, runtimeScrollSurface: { flex: 1, minWidth: 0, backgroundColor: systemColors.textBackground }, runtimeSection: { gap: 6 }, runtimeSectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, runtimeFieldList: { gap: 10 }, runtimeField: { minWidth: 0, alignSelf: "stretch", gap: 2 }, runtimeInputRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, runtimeModifiedBar: { width: 2, alignSelf: "stretch", marginRight: 6, borderRadius: 1, backgroundColor: "transparent" }, runtimeModifiedBarInline: { alignSelf: "center", height: 16, marginRight: 6 }, runtimeModifiedBarActive: { backgroundColor: systemColors.blue }, runtimeFieldError: { marginLeft: 142, color: systemColors.red, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, runtimeValueControlInvalid: { borderWidth: 1, borderColor: systemColors.red, borderRadius: 4 }, runtimeResetButton: { minWidth: 28, width: 28, height: 22, paddingHorizontal: 0 }, runtimeFieldLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "right" }, runtimeValueSlot: { width: 160, height: 26, flexShrink: 0, justifyContent: "center" }, runtimeValueControl: { width: 160, minWidth: 160, height: 26 }, runtimeBooleanControl: { width: 44, minWidth: 44, height: 24, alignSelf: "flex-start", marginLeft: 8 }, runtimeUnit: { width: 68, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, runtimeActionSlot: { width: 72, minHeight: 26, flexShrink: 0, justifyContent: "center" }, runtimeHelpSlot: { paddingLeft: 142, paddingTop: 1, minWidth: 0, alignSelf: "stretch" }, runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14, minWidth: 0, flexShrink: 1 }, runtimeMultilineField: { minWidth: 0, flexGrow: 1, flexBasis: "100%", maxWidth: "100%" }, runtimeMultilineHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, runtimeMultilineLabel: { flex: 1, minWidth: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, runtimeMultilineHeaderActions: { flexShrink: 0, minHeight: 26, justifyContent: "center" }, runtimeMultilineEditor: { width: "100%", minWidth: 0, height: 108, flex: 1, alignSelf: "stretch" }, runtimeMultilineHelpSlot: { marginLeft: 0, maxWidth: "100%", minWidth: 0, paddingTop: 6, gap: 3 }, runtimeJsonDefaultHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, fontWeight: "600", minWidth: 0 },
  dataManagementWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 8 }, dataManagementRail: { width: 140, flexShrink: 0, minHeight: 0, paddingTop: 6, borderRightWidth: 1, borderRightColor: systemColors.separator }, dataManagementRailList: { flex: 1, minHeight: 0 }, dataManagementDetail: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: systemColors.textBackground }, dataManagementTabBar: { height: 34, minHeight: 34, flexShrink: 0, paddingHorizontal: 12, justifyContent: "flex-start", borderBottomWidth: 1, borderBottomColor: systemColors.separator }, dataManagementTabs: { width: 272, height: 24, alignSelf: "flex-start", flexShrink: 0 }, dataManagementPane: { flex: 1, minHeight: 0 }, dataManagementPaneScrollContent: { paddingTop: 10, paddingHorizontal: 4, paddingBottom: 4, gap: 10 }, dataManagementWebDavPane: { flex: 1, minHeight: 0 }, dataManagementWebDavContent: { gap: 10, paddingTop: 10, paddingHorizontal: 4, paddingBottom: 14 }, dataManagementImportIntro: { width: "100%", minHeight: 72, paddingHorizontal: 12, paddingVertical: 12, justifyContent: "center" }, dataManagementImportFileRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementImportFileLabel: { width: 72, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE }, dataManagementImportFileValue: { flex: 1, minWidth: 0, minHeight: 26, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.textBackground }, dataManagementImportFilePlaceholder: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, dataManagementGroup: { gap: 6 }, dataManagementGroupBody: { gap: 5 }, dataManagementSelectionBar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementSelectionCount: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementToolbarButtons: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 }, dataManagementBottomActions: { minHeight: 26, flexDirection: "row", alignItems: "flex-end", justifyContent: "flex-end", gap: 8 }, dataManagementBottomMessage: { flex: 1, minWidth: 0, gap: 2 }, dataManagementSectionPicker: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 14, rowGap: 2, paddingVertical: 2 }, dataManagementSectionControl: { minWidth: 150, minHeight: 22, justifyContent: "center" }, dataManagementSensitiveHint: { color: systemColors.brown, fontSize: UI_FONT_SIZE, lineHeight: 16, paddingVertical: 5, paddingHorizontal: 7, backgroundColor: Platform.select({ macos: (PlatformColor("systemYellow") as unknown as { withAlphaComponent?: (alpha: number) => string })?.withAlphaComponent?.(0.08) ?? "rgba(255, 204, 0, 0.08)", default: "rgba(255, 204, 0, 0.08)" }), borderRadius: 4, borderWidth: 1, borderColor: Platform.select({ macos: (PlatformColor("systemYellow") as unknown as { withAlphaComponent?: (alpha: number) => string })?.withAlphaComponent?.(0.2) ?? "rgba(255, 204, 0, 0.2)", default: "rgba(255, 204, 0, 0.2)" }) }, dataManagementSensitiveNote: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementSyncContent: { gap: 6 }, dataManagementSyncScope: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementSyncScopeLabel: { width: WEBDAV_FORM_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, dataManagementSyncScopeValue: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }, dataManagementDirection: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, dataManagementDirectionLabel: { width: WEBDAV_FORM_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, dataManagementDirectionPicker: { width: 210, height: 24, flexGrow: 0, flexShrink: 0 }, dataManagementStatus: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  webDavForm: { flexGrow: 0, paddingHorizontal: 2 }, webdavFormBody: { gap: 6 }, webdavStateRow: { minHeight: 24, flexDirection: "row", alignItems: "center", justifyContent: "flex-start" }, webdavSyncArea: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 8, marginTop: 2 }, webdavActionRow: { minHeight: 32, flexDirection: "row", alignItems: "center", gap: 8, borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 8, marginTop: 2 }, webdavActionStatus: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, webdavEnabledControl: { flexGrow: 0, flexShrink: 0, alignSelf: "flex-start" }, webdavStateSpacer: { flex: 1 }, webdavStateStatus: { maxWidth: 180, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, textAlign: "right", lineHeight: 15 }, webdavFormRows: { width: "60%", gap: 5 }, webdavPasswordInput: { width: "100%", minHeight: 26 },
  logsWindow: { flex: 1, minHeight: 0, gap: 4 }, logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, logFilterRow: { width: 360, minWidth: 220, maxWidth: 360, height: 26, flexDirection: "row", alignItems: "center", gap: 8 }, logToolbarSpacer: { flex: 1, minWidth: 0 }, logActionsRow: { height: 26, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, clearCooldownButton: { minWidth: 96, height: 22 }, toolbarLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, flexShrink: 0 }, logFilterInput: { flex: 1, minWidth: 0, height: 26 }, logsTabs: { width: 640, maxWidth: "100%", minWidth: 0, height: 28, flexShrink: 0 }, logTableFrame: { flex: 1, minHeight: 0, minWidth: 0 }, logTable: { flex: 1, minHeight: 0 }, logEmptySurface: { flex: 1, minHeight: 0, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, logEmptyText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, textAlign: "center", paddingHorizontal: 20 },
  form: { gap: 6 }, structuredForm: { gap: 6 }, featureGrid: { flexDirection: "row", flexWrap: "wrap", columnGap: 12, rowGap: 4 }, featureGridItem: { flexGrow: 1, flexBasis: 180, minWidth: 180 }, field: { gap: 5, minWidth: 220, flexGrow: 1, flexBasis: 300 }, fieldLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, input: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, textArea: { minHeight: 108, textAlignVertical: "top", fontFamily: "Menlo" }, compactTextArea: { minHeight: 56, maxHeight: 56 }, inputWithAction: { flexDirection: "row", alignItems: "center", gap: 6 }, inputFlex: { flex: 1 }, toggleRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, toggleControl: { flex: 1, minWidth: 0, minHeight: 22, justifyContent: "center" }, toggleNativeControl: { width: "100%", minWidth: 220, minHeight: 22 }, actions: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 4 }, secretFieldActions: { flexDirection: "row", alignItems: "center", gap: 6 }, secretFieldButtons: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 }, secretFieldButton: { flex: 1, minWidth: 0, height: 26 }, nativeSecretControl: { flex: 1, minWidth: 0, minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, nativeSecretInput: { flex: 1, minWidth: 86, minHeight: 26 }, nativeSecretSetButton: { minWidth: 42, height: 26 }, action: {}, actionPrimary: {}, actionDanger: {}, actionDisabled: {}, actionText: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, actionTextPrimary: {}, actionTextDanger: {}, tabStrip: { flexDirection: "row", flexWrap: "wrap", gap: 6 }, tab: {}, tabSelected: {}, inlineMeta: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 6 }, rawEditor: { flex: 1, minHeight: 180, gap: 4 }, rawEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, rawNativeEditorFrame: { flex: 1, minHeight: 160, position: "relative" }, rawNativeEditor: { flex: 1, minHeight: 160 }, rawEditorOverlay: { position: "absolute", left: 0, right: 0, top: 0, bottom: 0, justifyContent: "center", alignItems: "center", gap: 8, paddingHorizontal: 12, backgroundColor: systemColors.textBackground }, rawEditorLoading: { flex: 1, minHeight: 160, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, infoPair: { gap: 2, minWidth: 160 }, rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 6 }, logRecords: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, maxHeight: 360, overflow: "scroll", padding: 10, gap: 6 }, logRecord: { color: systemColors.label, fontFamily: "Menlo", fontSize: UI_FONT_SIZE }, empty: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, paddingVertical: 12 }, result: { color: systemColors.green, fontSize: UI_FONT_SIZE }, warning: { color: systemColors.brown, fontSize: UI_FONT_SIZE, backgroundColor: systemColors.control, padding: 8, borderRadius: 4 }, issueBox: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.control, padding: 12, gap: 5 }, issue: { color: systemColors.red, fontSize: UI_FONT_SIZE }, cardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, cardHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, marginTop: 2 },
  nativeSecretMultilineControl: { alignItems: "flex-start", minHeight: 108 },
  nativeSecretTextArea: { minHeight: 108, height: 108, alignSelf: "stretch" },
  secretActionButton: { width: 64, minWidth: 64, height: 26, flexShrink: 0 },
  formRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, formRowStacked: { alignItems: "flex-start" }, formRowSecretStacked: { alignItems: "flex-start" }, formRowLabel: { width: 112, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, formRowLabelStacked: { paddingTop: 4 }, formRowControl: { flex: 1, minWidth: 0, gap: 3 }, picker: { flex: 1, minWidth: 180, height: 26 }, pickerShrink: { minWidth: 0 },
});
