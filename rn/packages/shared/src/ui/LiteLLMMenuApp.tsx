import React, { createContext, useCallback, useEffect, useMemo, useRef, useState, useContext } from "react";
import { AppState, Dimensions, FlatList, Platform, PlatformColor, Pressable, ScrollView, StyleSheet, Text, View, type HostInstance, type StyleProp, type TextStyle, type ViewStyle } from "react-native";
import { createTranslator } from "../i18n";
import { assistantSettingOptions, codexFeatureLabel, localizeCodexValidationMessage, type AssistantSettingOption } from "../i18n/assistantSettingsI18n";
import { runtimeCategoryLabel, runtimeFieldHelp, runtimeFieldLabel, runtimeOptionLabel, runtimeUnitLabel } from "../i18n/runtimeSettingsI18n";
import { LOG_TABS, ROUTES } from "../routes";
import { NativeButton, NativeCheckbox, NativePicker, NativeSecureTextEditor, NativeSecureTextInput, NativeSegmentedControl, NativeSplitView, NativeTable, NativeTextEditor, NativeTextField } from "./NativeControls";
import { normalizeRelayOrigin, RelayAccountManager, stationOriginKey } from "./RelayAccountManager";
import { screenBoundedTooltipText } from "./tooltip";
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
type ServiceOperation = "start" | "stop" | "restart" | "reload" | "health";
type AssistantSettingsDomain = "codex" | "claude";
type EditableDiskDomain = AssistantSettingsDomain | "providers_models" | "runtime" | "webdav";
type ClaudeDeploymentDraft = { model: string; base_url: string };

const PendingFieldContext = createContext<PendingFieldRegistry | undefined>(undefined);
const TranslationContext = createContext<Translate | undefined>(undefined);
// React Native macOS supports `tooltip` on Text, but its published TypeScript
// declaration has not caught up with that native prop. Keep the cast narrow so
// the full probe result is a real native hover tooltip, not an accessibility-
// only hint.
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
const ONLINE_USAGE_POLL_MS = 15_000;

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

function domainState(snapshot: CoreSnapshot | undefined, domain: ConfigDomain): UnknownRecord {
  const record = asRecord(snapshot?.domains[domain]);
  const state = asRecord(record.state);
  return Object.keys(state).length > 0 ? state : record;
}

function codexModelCatalogState(snapshot: CoreSnapshot | undefined): UnknownRecord {
  return asRecord(domainState(snapshot, "codex").model_catalog);
}

function domainForRoute(route: AppRoute): ConfigDomain | undefined {
  switch (route) {
    case "providers-models": return "providers_models";
    case "codex-settings": return "codex";
    case "claude-settings": return "claude";
    case "runtime-settings": return "runtime";
    case "webdav-settings": return "webdav";
    case "logs": return "logs";
    default: return undefined;
  }
}

function isAssistantSettingsRoute(route: AppRoute): boolean {
  return route === "codex-settings" || route === "claude-settings";
}

function isSettingsRoute(route: AppRoute): boolean {
  return route === "providers-models" || route === "codex-settings" || route === "claude-settings" || route === "runtime-settings" || route === "webdav-settings";
}

// Codex and Claude are tabs in one native settings window. Keep the original
// Claude route as a deep-link compatibility target, but never let it create a
// second host window for the same workspace.
function nativeWindowRoute(route: AppRoute): AppRoute {
  return route === "claude-settings" ? "codex-settings" : route;
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

function statusLabel(status: ServiceStatus, translate: Translate): string {
  return translate(`service.${status.state}`);
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

function displayValue(value: unknown, fallback: string): string {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return fallback;
  }
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
  const presentedCatalogRestartEvent = useRef(0);
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
      routeWebdavSettings: translate("card.webdavSettings"), routeRelayAccounts: translate("route.relayAccounts"), routeRelayAdd: translate("relay.addAccount"), routeLogs: translate("card.logs"),
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
        const windowRoute = nativeWindowRoute(routeRequest);
        native.window.open(windowRoute);
        native.window.focus(windowRoute);
      }
      return;
    }
    setRoute(routeRequest);
    if (isPrimaryHost && routeRequest !== "home") {
      const windowRoute = nativeWindowRoute(routeRequest);
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
    const event = numberValue(catalog.change_event);
    if (!booleanValue(catalog.restart_required) || event <= presentedCatalogRestartEvent.current || catalogRestartConfirmationOpen.current) return;
    presentedCatalogRestartEvent.current = event;
    catalogRestartConfirmationOpen.current = true;
    void (async () => {
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
            await ipc.dispatch({ domain: "codex", type: "acknowledge_model_catalog_restart", payload: {} });
            receiveSnapshot(await ipc.snapshot());
            return;
          }
          restartFailed = true;
        }
      } catch {
        // The native panel is independent from every settings window. Keep
        // those windows usable if its acknowledgement is temporarily
        // unavailable, then present the same outstanding event again when
        // the next Core snapshot arrives.
        presentedCatalogRestartEvent.current = event - 1;
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
      { id: "open-providers-models", title: translate("menu.providers"), enabled: true },
      { id: "open-runtime-settings", title: translate("menu.runtime"), enabled: true },
      { id: "open-codex-settings", title: translate("menu.codex"), enabled: true },
      { id: "open-relay-accounts", title: translate("menu.relay"), enabled: true },
      { id: "webdav-status", title: `${translate("webdav.label")}: ${webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate)}`, enabled: false },
      { id: "open-webdav-settings", title: translate("menu.webdav"), enabled: true },
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
      {!error && route !== "home" && snapshot ? <RouteSurface route={route} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={receiveSnapshot} onClose={() => setRoute("home")} /> : null}
      {!error && route === "home" ? <View style={styles.menuBarHost} /> : null}
    </View>
  );
}

function WindowTitle({ title, validation }: { title: string; validation?: string }): React.JSX.Element {
  return <View style={styles.windowTitleBlock}><Text style={styles.windowTitle}>{title}</Text>{validation ? <Text style={styles.validationText}>{validation}</Text> : null}</View>;
}

function DialogFooter({ status, leading, children }: { status?: string; leading?: React.ReactNode; children: React.ReactNode }): React.JSX.Element {
  return <View style={styles.footer}>{leading ?? (status ? <Text numberOfLines={1} style={styles.footerStatus}>{status}</Text> : <View />)}<View style={styles.footerSpacer} /><View style={styles.footerButtons}>{children}</View></View>;
}

function IconButton({ label, symbol, title, disabled, onPress }: { label: string; symbol?: "pause" | "play" | "trash"; title: string; disabled?: boolean; onPress: () => void }): React.JSX.Element {
  return <NativeButton title={label} symbol={symbol} toolTip={title} accessibilityLabel={title} compact disabled={disabled} onPress={onPress} style={styles.iconButton} />;
}

function WindowTabs({ values, selected, disabled, onSelect, style, nativeRef }: { values: Array<{ id: string; title: string }>; selected: string; disabled?: boolean; onSelect: (id: string) => void; style?: StyleProp<ViewStyle>; nativeRef?: React.Ref<HostInstance> }): React.JSX.Element {
  const labels = values.map((item) => item.title);
  const selectedValue = values.find((item) => item.id === selected)?.title ?? labels[0] ?? "";
  return <NativeSegmentedControl ref={nativeRef} labels={labels} selectedValue={selectedValue} disabled={disabled} onChange={({ nativeEvent }) => { const next = values[nativeEvent.index]; if (next) onSelect(next.id); }} style={[styles.windowTabs, style]} />;
}

function RouteSurface({ route, snapshot, ipc, native, translate, logTabRequest, nativeAction, onSnapshot, onClose }: { route: AppRoute; snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; translate: Translate; logTabRequest?: LogTab; nativeAction?: { id: string; sequence: number }; onSnapshot: (next: CoreSnapshot) => void; onClose: () => void }): React.JSX.Element {
  const settingsRoute = isAssistantSettingsRoute(route);
  const [settingsTab, setSettingsTab] = useState<AssistantSettingsDomain>(route === "claude-settings" ? "claude" : "codex");
  const [claudeDeploymentDraft, setClaudeDeploymentDraft] = useState<ClaudeDeploymentDraft>();
  const claudeDeploymentDraftRef = useRef<ClaudeDeploymentDraft | undefined>(undefined);
  const domain = settingsRoute ? settingsTab : domainForRoute(route);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string>();
  const [issues, setIssues] = useState<ValidationSummary["issues"]>([]);
  const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);
  const [keptDiskGeneration, setKeptDiskGeneration] = useState<Partial<Record<EditableDiskDomain, number>>>({});
  const promptedDiskGeneration = useRef<Partial<Record<EditableDiskDomain, number>>>({});
  const diskPromptInFlight = useRef(false);
  const activeRuns = useRef(0);
  const revision = useRef<number | undefined>(snapshot?.revision);
  const latestSnapshot = useRef<CoreSnapshot | undefined>(snapshot);
  const dispatchQueue = useRef<Promise<void>>(Promise.resolve());
  const probedSurfaceApplyQueue = useRef<Promise<void>>(Promise.resolve());
  const lastDispatchError = useRef<unknown>(undefined);
  const pendingFields = useRef(new Map<symbol, PendingField>());
  const [, forcePendingFieldDirtyRender] = useState(0);
  const pendingFieldDirtyIdsRef = useRef<ReadonlySet<symbol>>(new Set());
  const claudeDeployment = claudeDeploymentDraft ?? claudeDeploymentFromSnapshot(snapshot);
  const hasClaudeDeploymentChanges = (currentSnapshot: CoreSnapshot | undefined): boolean => !sameClaudeDeployment(
    claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(currentSnapshot),
    claudeDeploymentFromSnapshot(currentSnapshot),
  );
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
    }
  }, [route]);

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
  const run = async (operation: () => Promise<unknown>, message: string | null = "common.applied", keepControlsEnabled = false): Promise<void> => {
    activeRuns.current += 1;
    if (!keepControlsEnabled) setBusy(true);
    setResult(undefined);
    try {
      const value = await operation();
      if (asRecord(value).cancelled === true) {
        setResult(undefined);
      } else if (isValidation(value)) {
        setIssues(value.issues);
        setResult(value.valid ? translate("common.applied") : `${value.issues.length} ${translate("common.validationIssues")}`);
      } else {
        setIssues([]);
        setResult(message === null ? undefined : translate(message));
      }
      await refresh();
    } catch (reason: unknown) {
      setResult(errorMessage(reason, translate));
    } finally {
      activeRuns.current -= 1;
      if (!keepControlsEnabled && activeRuns.current === 0) setBusy(false);
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
  }, null, true);
  const dispatchWithOutcome = async (type: string, payload: UnknownRecord = {}, targetDomain = domain): Promise<CoreSnapshot | undefined> => {
    let succeeded = false;
    await run(async () => {
      const staged = await enqueueDispatch(type, payload, targetDomain);
      succeeded = true;
      return staged;
    }, null);
    return succeeded ? latestSnapshot.current : undefined;
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
        const next = await ipc.snapshot();
        if (!active) return;
        const previous = latestSnapshot.current;
        revision.current = Math.max(revision.current ?? -1, next.revision);
        const diskStateChanged = monitoredDiskDomains.some((diskDomain) =>
          !sameDiskState(previous?.disk[diskDomain], next.disk[diskDomain]),
        );
        // This timer exists to observe external file changes. Publishing an
        // identical snapshot every two seconds needlessly re-renders native
        // tables and editors (and used to make focused inputs flash).
        if (!previous || diskStateChanged) {
          latestSnapshot.current = next;
          onSnapshot(next);
        }
        for (const diskDomain of monitoredDiskDomains) {
          const changedGeneration = next.disk[diskDomain]?.changed ? next.disk[diskDomain]?.generation : undefined;
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
          if ((next.disk[diskDomain]?.generation ?? 0) > priorGeneration && !next.disk[diskDomain]?.changed && (diskDomain === "codex" || diskDomain === "claude")) {
            setSettingsRawReloadToken((current) => current + 1);
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
      // Core invalidates the native editor capabilities when a domain reloads.
      // Make both raw Codex editors fetch fresh capabilities and disk text only
      // after that reload has succeeded.
      if (reloadDomain === "codex" || reloadDomain === "claude") setSettingsRawReloadToken((current) => current + 1);
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
  const validate = (): Promise<void> => {
    if (!domain) return Promise.resolve();
    return run(async () => {
      await flushPendingFields();
      return ipc.validate(domain, revision.current);
    }, "common.applied");
  };
  const apply = (): Promise<void> => {
    if (!domain || domain === "logs") return Promise.resolve();
    return run(async () => {
      await flushPendingFields();
      const refreshed = await ipc.snapshot();
      revision.current = refreshed.revision;
      onSnapshot(refreshed);
      const domains = settingsRoute
        ? (["codex", "claude"] as const).filter((name) => refreshed.drafts[name]?.dirty)
        : refreshed.drafts[domain]?.dirty ? [domain] : [];
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
      const result = settingsRoute
        ? await ipc.applyDomains([...domains], refreshed.revision, confirmations.length > 0 ? confirmations : undefined)
        : await ipc.apply(domain, refreshed.revision, confirmations.length > 0 ? confirmations : undefined);
      if (domains.includes("providers_models") && (refreshed.service.state === "running" || refreshed.service.state === "unhealthy")) {
        const reloaded = await ipc.dispatch({ type: "service.reload" }, result.revision);
        revision.current = reloaded.revision;
      }
      if (domains.includes("claude")) {
        claudeDeploymentDraftRef.current = undefined;
        setClaudeDeploymentDraft(undefined);
      }
      if (diskConflicts.length > 0) setKeptDiskGeneration({});
      return result;
    });
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
      const result = await ipc.apply("providers_models", staged.revision, confirmations);
      revision.current = result.revision;
      if (staged.service.state === "running" || staged.service.state === "unhealthy") {
        const reloaded = await ipc.dispatch({ type: "service.reload" }, result.revision);
        revision.current = reloaded.revision;
      }
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
    try {
      native.window.close(nativeWindowRoute(route));
    } finally {
      onClose();
    }
  };
  const requestClose = (): void => {
    const restoreWindow = (): void => {
      const windowRoute = nativeWindowRoute(route);
      native.window.open(windowRoute);
      native.window.focus(windowRoute);
    };
    const current = latestSnapshot.current ?? snapshot;
    const dirtyDomains = settingsRoute
      ? (["codex", "claude"] as const).filter((name) => current?.drafts[name]?.dirty)
      : domain && current?.drafts[domain]?.dirty ? [domain] : [];
    const needsDiscardConfirmation = hasPendingFieldEdits()
      || dirtyDomains.length > 0
      || (settingsRoute && hasClaudeDeploymentChanges(current));
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
            await enqueueDispatch("cancel", {}, name);
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
    if (nativeAction?.id !== `request-close-${route}` && nativeAction?.id !== `request-close-${nativeWindowRoute(route)}`) return;
    requestClose();
  }, [nativeAction?.sequence]);
  const definition = ROUTES.find((item) => item.id === route);
  const windowTitle = settingsRoute
    ? translate(settingsTab === "claude" ? "card.claudeSettings" : "card.codexSettings")
    : translate(definition?.titleKey ?? "app.title");
  return <TranslationContext.Provider value={settingsRoute ? translate : undefined}><PendingFieldContext.Provider value={fieldRegistry}><View style={styles.windowSurface}>
    {route !== "providers-models" && route !== "logs" && route !== "relay-accounts" && route !== "relay-add" ? <WindowTitle title={windowTitle} validation={issues.length > 0 ? `${issues.length} ${translate("common.validationIssues")}` : undefined} /> : null}
    {route === "providers-models" || settingsRoute || route === "logs" || route === "relay-accounts" || route === "relay-add" || route === "runtime-settings" || route === "webdav-settings" ? <View style={[styles.windowContent, compactStyles.windowContent, styles.windowContentFixed, route === "providers-models" && styles.providersContent, settingsRoute && styles.settingsContent, route === "logs" && styles.logsContent, (route === "relay-accounts" || route === "relay-add") && styles.relayAccountsContent, route === "runtime-settings" && styles.runtimeContent, route === "webdav-settings" && styles.webDavContent]}>
    {route === "providers-models" ? <ProviderWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} dispatchWithOutcome={dispatchWithOutcome} onStatus={setResult} onSecretState={onSecretState} applyProbedSurface={applyProbedSurface} /> : null}
    {settingsRoute ? <><View style={styles.settingsTabBar}><WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]} selected={settingsTab} disabled={busy} onSelect={(next) => switchSettingsTab(next as AssistantSettingsDomain)} style={styles.settingsTabs} /></View>{settingsTab === "codex" ? <CodexWorkspace snapshot={snapshot} ipc={ipc} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} rawReloadToken={settingsRawReloadToken} /> : <ClaudeScreen snapshot={snapshot} ipc={ipc} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} deployment={claudeDeployment} onDeploymentChange={(key, value) => {
      const next = { ...(claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(snapshot)), [key]: value };
      claudeDeploymentDraftRef.current = next;
      setClaudeDeploymentDraft(next);
      return enqueueDispatch("patch_deployment", { [key]: value }, "claude").then(() => {
        setSettingsRawReloadToken((current) => current + 1);
      });
    }} rawReloadToken={settingsRawReloadToken} />}</> : null}
    {route === "logs" ? <LogsWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} requestedTab={nativeAction?.id === "open-recovery" ? "recovery" : logTabRequest} requestedTabKey={nativeAction?.sequence ?? 0} /> : null}
    {route === "relay-accounts" || route === "relay-add" ? <RelayAccountManager visible setupOnly={route === "relay-add"} snapshot={snapshot} native={native} busy={busy} translate={translate} onClose={closeRoute} dispatch={dispatch} commit={commitRelayMetadata} detectType={async (origin) => {
      const staged = await enqueueDispatch("account.detect_type", { origin }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const detected = asRecord(next.action_summaries?.relay_accounts).detected_type;
      return detected === "newapi" || detected === "sub2api" ? detected : undefined;
    }} refreshResources={async (accountId) => {
      const staged = await enqueueDispatch("resources.refresh", { account_id: accountId }, "relay_accounts");
      revision.current = staged.revision;
      return asRecord(staged).resource_status === "ready" ? "ready" : "unavailable";
    }} importResources={async (accountId, resourceIds) => {
      await commitRelayMetadata("resources.import", { account_id: accountId, resource_ids: resourceIds }, "relay_accounts");
    }} apiKeyActions={{
      create: async (accountId) => {
        await commitRelayMetadata("api_key.create", { account_id: accountId }, "relay_accounts");
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
      remove: async (accountId, resourceId) => {
        await commitRelayMetadata("api_key.delete", { account_id: accountId, resource_id: resourceId }, "relay_accounts");
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
    {route === "runtime-settings" ? <RuntimeWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onSnapshot={onSnapshot} onSecretState={onSecretState} clearSecret={clearSecret} /> : null}
    {route === "webdav-settings" ? <WebDavWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} /> : null}
    {issues.length > 0 ? <IssueList issues={issues} translate={translate} /> : null}
    </View> : null}
    {route !== "logs" && route !== "relay-accounts" && route !== "relay-add" ? <DialogFooter status={result} leading={route === "runtime-settings" ? <ActionButton title={translate("common.restoreDefaults")} disabled={busy} style={styles.runtimeRestoreButton} onPress={() => dispatch("restore_defaults")} /> : route === "webdav-settings" ? <View style={styles.webdavFooterLeading}><ActionButton title={translate("common.test")} disabled={busy} style={styles.wideButton} onPress={() => run(async () => { await flushPendingFields(); return ipc.probe(undefined, undefined, "webdav"); }, "webdav.probe")} /><Text numberOfLines={2} style={styles.webdavProbeStatus}>{snapshot ? webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate) : ""}</Text></View> : undefined}><><ActionButton title={translate("menu.close")} disabled={busy} style={route === "runtime-settings" || route === "webdav-settings" ? styles.wideButton : undefined} onPress={requestClose} /><ActionButton primary title={route === "runtime-settings" ? translate("common.saveAndApply") : translate("menu.apply")} disabled={busy || (settingsRoute ? !(snapshot?.drafts.codex?.dirty || snapshot?.drafts.claude?.dirty || hasClaudeDeploymentChanges(snapshot) || hasPendingFieldEdits()) : domain ? !(snapshot?.drafts[domain]?.dirty || hasPendingFieldEdits()) : false)} style={route === "runtime-settings" || route === "webdav-settings" ? styles.wideButton : undefined} onPress={apply} /></></DialogFooter> : null}
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

function ProviderWorkspace({ snapshot, ipc, onSnapshot, native, busy, translate, dispatch, dispatchWithOutcome, onStatus, onSecretState, applyProbedSurface }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; dispatchWithOutcome: (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<CoreSnapshot | undefined>; onStatus: (status?: string) => void; onSecretState: (state: SecretState) => void; applyProbedSurface: ApplyProbedSurface }): React.JSX.Element {
  const state = domainState(snapshot, "providers_models");
  const providers = useMemo(() => {
    const details = asRecords(state.providers);
    if (details.length > 0) return details;
    return (snapshot?.providers_models.providers ?? []).map(providerRecord);
  }, [snapshot?.providers_models.providers, state.providers]);
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const pendingProviderIds = useRef<Set<string> | undefined>(undefined);
  const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);
  const knownModelIdsByProvider = useRef<Map<string, Set<string>> | undefined>(undefined);
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
  const [fetchKeyName, setFetchKeyName] = useState<string>();
  const probingModelKeys = useRef(new Set<string>());
  const [, setProbeActivityRevision] = useState(0);
  const [probeResults, setProbeResults] = useState<Record<string, IpcResults["probe"]>>({});
  const transferButtonRef = useRef<HostInstance | null>(null);
  const apiKeyNames = useMemo(() => stringList(provider?.api_key_names), [provider?.api_key_names]);
  const fetchKeyOptions = useMemo(
    () => apiKeyNames.map((name) => ({ value: name, label: apiKeyDisplayName(name, translate) })),
    [apiKeyNames, translate],
  );
  const selectedFetchKey = fetchKeyName ?? apiKeyNames[0] ?? "";
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
  const modelIdentitySignature = useMemo(
    () => providers.map((entry) => `${editorIdentifier(entry)}:${asRecords(entry.models).map(editorIdentifier).join(",")}`).join("|"),
    [providers],
  );
  useEffect(() => {
    if (!snapshot) return;
    const current = new Map<string, Set<string>>();
    const added: Array<{ providerId: string; modelId: string }> = [];
    for (const entry of providers) {
      const currentProviderId = editorIdentifier(entry);
      const currentModelIds = new Set(asRecords(entry.models).map(editorIdentifier));
      const previousModelIds = knownModelIdsByProvider.current?.get(currentProviderId);
      if (previousModelIds) {
        for (const modelId of currentModelIds) {
          if (!previousModelIds.has(modelId)) added.push({ providerId: currentProviderId, modelId });
        }
      }
      current.set(currentProviderId, currentModelIds);
    }
    const wasInitialized = knownModelIdsByProvider.current !== undefined;
    knownModelIdsByProvider.current = current;
    if (wasInitialized) void Promise.all(added.map(({ providerId: targetProviderId, modelId }) => probeModel(targetProviderId, modelId, { confirmRecommendation: false })));
  }, [modelIdentitySignature]);
  useEffect(() => {
    const pending = pendingProviderIds.current;
    if (pending) {
      const added = providers.find((item) => !pending.has(editorIdentifier(item)));
      if (added) {
        setSelectedProvider(editorIdentifier(added));
        setSelectedModel(undefined);
        setProviderSourceModel(undefined);
        pendingProviderIds.current = undefined;
        return;
      }
    }
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
    if (!apiKeyNames.includes(fetchKeyName ?? "")) setFetchKeyName(apiKeyNames[0]);
  }, [apiKeyNames, fetchKeyName, providerId]);
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
    const providerName = stringValue(provider?.display_name, stringValue(provider?.name, providerId));
    const keyName = apiKeyDisplayName(fetchKeyName ?? "default", translate);
    void native.chooseModelsToAdd({ models: candidates, providerName, keyName }).then((selection) => {
      const selectedModels = (selection ?? []).filter((model, index, all) => candidateSet.has(model) && all.indexOf(model) === index);
      if (selectedModels.length === 0) return;
      void Promise.all(selectedModels.map((upstreamModel, index) => dispatch("model.add", { provider_id: providerId, model: { name: upstreamModel, upstream_model: upstreamModel, api_key_name: fetchKeyName, enabled: true, order: models.length + index + 1 } })))
        .catch(() => undefined);
    }).catch(() => undefined);
  };
  const importSelected = async (): Promise<void> => {
    const fileToken = await native.openFilePicker({ purpose: "import" });
    if (fileToken) await dispatch("providers.import_selected", { file_token: fileToken });
  };
  const exportSelected = async (): Promise<void> => {
    const fileToken = await native.saveFilePicker({ suggestedName: "providers-models.json" });
    if (!fileToken) return;
    const exported = await ipc.export(["providers_models"], fileToken);
    onSnapshot(await ipc.snapshot());
    return void exported;
  };
  const transferActions = [
    translate("providers.currentCodex"),
    translate("providers.currentClaude"),
    translate("providers.configurationFile"),
    translate("providers.relay"),
    translate("providers.exportFile"),
  ];
  const selectTransferAction = (index: number): void => {
    if (index === 0) void dispatch("providers.import_codex_current");
    else if (index === 1) void dispatch("providers.import_claude_current");
    else if (index === 2) void importSelected();
    else if (index === 3) native.window.open("relay-accounts");
    else if (index === 4) void exportSelected();
  };
  const showTransferMenu = (): void => {
    const button = transferButtonRef.current;
    if (!button) return;
    button.measureInWindow((x, y, width, height) => {
      void native.showActionMenu({ title: translate("providers.transferActions"), items: transferActions, anchor: { x, y, width, height } }).then((index) => {
        if (index !== undefined) selectTransferAction(index);
      });
    });
  };
  const addProvider = (): void => {
    pendingProviderIds.current = new Set(providers.map(editorIdentifier));
    void dispatch("provider.add", { provider: { name: "", enabled: true, models: [], create_default_api_key: true } });
  };
  const addModel = (): void => {
    if (!provider) return;
    const knownModelIds = new Set(models.map(editorIdentifier));
    pendingModelIds.current = { providerId, ids: knownModelIds };
    void dispatch("model.add", { provider_id: providerId, model: { name: "", upstream_model: "", enabled: true, order: models.length + 1 } });
  };
  const fetchModels = (): void => {
    if (!provider || !fetchKeyName) return;
    void dispatchWithOutcome("providers.fetch_models", { provider_id: providerId, api_key_name: fetchKeyName }).then((next) => {
      if (!next) {
        onStatus(translate("providers.fetchFailed", { detail: translate("common.notAvailable") }));
        return;
      }
      const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);
      if (Object.keys(summary).length === 0) {
        onStatus(translate("providers.fetchFailed", { detail: translate("common.notAvailable") }));
        return;
      }
      handleFetchedModels(summary);
    });
  };
  const duplicateModel = (): void => {
    if (!model) return;
    void dispatch("model.duplicate", { provider_id: providerId, model_id: editorIdentifier(model) });
  };
  const routes = useMemo(() => providers.flatMap((entry, providerIndex) => asRecords(entry.models).map(modelRecord).flatMap((entryModel, modelIndex) => {
    const publicModel = stringValue(entryModel.name).trim();
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
    const leftOrder = numberValue(left.model.order, Number.MAX_SAFE_INTEGER);
    const rightOrder = numberValue(right.model.order, Number.MAX_SAFE_INTEGER);
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    const providerOrder = stringValue(left.provider.name).localeCompare(stringValue(right.provider.name), undefined, { sensitivity: "base" });
    if (providerOrder !== 0) return providerOrder;
    return left.deploymentID.localeCompare(right.deploymentID);
  }), [providers]);
  const activeRoute = routes.find((entry) => entry.key === selectedRoute);
  const activeRouteGroup = activeRoute ? routes.filter((entry) => entry.publicModel === activeRoute.publicModel) : [];
  const activeRouteIndex = activeRoute ? activeRouteGroup.findIndex((entry) => entry.key === activeRoute.key) : -1;
  const canMoveRouteUp = activeRouteIndex > 0;
  const canMoveRouteDown = activeRouteIndex >= 0 && activeRouteIndex < activeRouteGroup.length - 1;
  useEffect(() => {
    if (viewMode !== "routes" || routes.length === 0 || routes.some((entry) => entry.key === selectedRoute)) return;
    setSelectedRoute(routes[0].key);
  }, [routes, selectedRoute, viewMode]);
  const moveRoute = (direction: "up" | "down"): void => {
    if (!activeRoute || activeRouteIndex < 0) return;
    const targetIndex = direction === "up" ? activeRouteIndex - 1 : activeRouteIndex + 1;
    if (targetIndex < 0 || targetIndex >= activeRouteGroup.length) return;
    const reordered = [...activeRouteGroup];
    [reordered[activeRouteIndex], reordered[targetIndex]] = [reordered[targetIndex], reordered[activeRouteIndex]];
    void dispatch("routes.reorder_group", { public_model: activeRoute.publicModel, route_ids: reordered.map((entry) => entry.deploymentID) });
  };
  const confirmDeleteProvider = (): void => {
    if (!provider) return;
    const label = stringValue(provider.name, translate("providers.newProvider"));
    void native.showConfirmation({ title: translate("providers.deleteProvider"), message: `${label} (${models.length} ${translate("providers.models")})`, confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("provider.delete", { provider_id: providerId }).then(() => { setSelectedProvider(undefined); setSelectedModel(undefined); setProviderSourceModel(undefined); }) : undefined);
  };
  const confirmDeleteModel = (): void => {
    if (!model) return;
    const modelId = editorIdentifier(model);
    void native.showConfirmation({ title: translate("providers.deleteModel"), message: stringValue(model.name, modelId), confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("model.delete", { provider_id: providerId, model_id: modelId }).then(() => setSelectedModel(undefined)) : undefined);
  };
  const providerRows = useMemo(
    () => providers.map((item) => ({ key: editorIdentifier(item), cells: [stringValue(item.display_name, stringValue(item.name, translate("providers.newProvider"))), String(asRecords(item.models).length || numberValue(item.model_count))] })),
    [providers, translate],
  );
  const disabledProviderKeys = useMemo(
    () => providers.filter((item) => !booleanValue(item.enabled, true)).map(editorIdentifier),
    [providers],
  );
  const modelRows = useMemo(
    () => models.map((item) => ({ key: editorIdentifier(item), cells: [stringValue(item.display_name, stringValue(item.name, translate("providers.newModel"))), upstreamModelLabel(item), `${apiKeyDisplayName(item.api_key_name, translate)} / ${numberValue(item.order, 1)}`] })),
    [models, translate],
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
      const numericOrder = numberValue(entry.model.order, Number.NaN);
      const order = Number.isFinite(numericOrder) ? String(numericOrder) : stringValue(entry.model.order).trim();
      rows.push({
        key: entry.key,
        cells: [`\t${stringValue(entry.provider.display_name, stringValue(entry.provider.name, translate("providers.newProvider")))}`, order || "-", apiKeyDisplayName(entry.model.api_key_name, translate), upstreamModelLabel(entry.model) || translate("common.notAvailable")],
      });
    }
    return rows;
  }, [routes, translate]);
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
  return <View style={styles.providersLayout}>
    <View style={styles.providerLeftColumn}>
      <View style={styles.providerToolbar}>
        <WindowTabs values={[{ id: "providers", title: translate("providers.providers") }, { id: "routes", title: translate("providers.routes") }]} selected={viewMode} onSelect={(value) => chooseViewMode(value as "providers" | "routes")} />
        <ActionButton ref={transferButtonRef} title={`${translate("providers.importExport")} ▾`} disabled={busy} style={styles.importSourcePicker} onPress={showTransferMenu} />
        <View style={styles.toolbarSpacer} />
      </View>
      {viewMode === "routes" ? <View style={styles.routeWorkspace}>
        <TablePane wide style={styles.routeTablePane} title={translate("providers.routes")} actions={<><IconButton label="↑" title={translate("common.moveUp")} disabled={busy || !canMoveRouteUp} onPress={() => moveRoute("up")} /><IconButton label="↓" title={translate("common.moveDown")} disabled={busy || !canMoveRouteDown} onPress={() => moveRoute("down")} /></>}>
          <NativeTable columns={[{ label: translate("providers.provider"), width: 136 }, { label: translate("common.order"), width: 48 }, { label: translate("providers.keyName"), width: 112 }, { label: translate("providers.upstream"), width: 136 }]} rows={routeRows} disabledRowKeys={disabledRouteKeys} selectedKey={selectedRoute ?? ""} compact onSelectionChange={selectRoute} style={styles.nativeRouteTable} />
        </TablePane>
      </View> : <View style={styles.providerWorkspace}>
        <View style={styles.providerModelColumns}>
          <TablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !provider} onPress={confirmDeleteProvider} /></>}>
            <NativeTable columns={[{ label: translate("providers.provider"), width: 104 }, { label: translate("providers.modelCount"), width: 48 }]} rows={providerRows} disabledRowKeys={disabledProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }} style={styles.nativeProviderTable} />
          </TablePane>
          <TablePane style={styles.modelListPane} title={translate("providers.models")} actions={<><IconButton label="+" title={translate("providers.newModel")} disabled={busy || !provider} onPress={addModel} /><IconButton label="⧉" title={translate("common.copy")} disabled={busy || !model} onPress={duplicateModel} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !model} onPress={confirmDeleteModel} /></>}>
            <NativeTable columns={[{ label: translate("providers.model"), width: 96 }, { label: translate("providers.upstream"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 96 }]} rows={modelRows} disabledRowKeys={disabledModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} scrollTrailingColumnOverflow onSelectionChange={(key) => { setSelectedModel(key); setProviderSourceModel(undefined); }} style={styles.nativeModelTable} />
            <View style={styles.tableBottomRow}><NativePicker labels={fetchKeyOptions.length > 0 ? fetchKeyOptions.map((option) => option.label) : [translate("common.default")]} selectedValue={selectedFetchLabel} disabled={busy || !provider || apiKeyNames.length === 0} onChange={({ nativeEvent }) => { const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyName(option.value); }} style={styles.fetchKeyPicker} /><ActionButton title={translate("providers.fetch")} disabled={busy || !provider || !fetchKeyName} onPress={fetchModels} /></View>
          </TablePane>
        </View>
      </View>}
    </View>
    <View style={styles.providerInspector}>{viewMode === "routes" ? (activeRoute ? (providerSourceModel ? <ProviderEditor provider={activeRoute.provider} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} sourceModel={activeRoute.model} onReturnToModel={() => { setProviderSourceModel(undefined); setSelectedModel(editorIdentifier(activeRoute.model)); }} /> : <ModelInspector providers={providers} provider={activeRoute.provider} providerId={editorIdentifier(activeRoute.provider)} model={activeRoute.model} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} {...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: editorIdentifier(activeRoute.provider), model_id: editorIdentifier(activeRoute.model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(activeRoute.model)); setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`); setProviderSourceModel(undefined); })} />) : <EmptyState translate={translate} />) : provider && model ? <ModelInspector providers={providers} provider={provider} providerId={providerId} model={model} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(providerId, editorIdentifier(model))} {...modelProbeProps(providerId, editorIdentifier(model))} onProviderClick={() => { setProviderSourceModel(editorIdentifier(model)); setSelectedModel(undefined); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: providerId, model_id: editorIdentifier(model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(model)); setProviderSourceModel(undefined); })} /> : provider ? <ProviderEditor provider={provider} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} sourceModel={models.find((item) => editorIdentifier(item) === providerSourceModel)} onReturnToModel={() => { if (providerSourceModel) setSelectedModel(providerSourceModel); setProviderSourceModel(undefined); }} /> : <EmptyState translate={translate} />}</View>
  </View>;
}

function TablePane({ title, actions, wide, style, children }: { title: string; actions: React.ReactNode; wide?: boolean; style?: StyleProp<ViewStyle>; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.tablePane, compactStyles.tablePane, wide && styles.tablePaneWide, style]}><View style={[styles.tableTitleRow, compactStyles.tableTitleRow]}><Text style={styles.tableTitle}>{title}</Text><View style={[styles.tableActions, compactStyles.inlineGap]}>{actions}</View></View>{children}</View>;
}

function ModelInspector({ providers, provider, providerId, model, native, busy, translate, dispatch, probe, probing, probeResult, onProviderClick, onProviderChange }: { providers: UnknownRecord[]; provider: UnknownRecord; providerId: string; model: UnknownRecord; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; probe: () => void; probing: boolean; probeResult?: IpcResults["probe"]; onProviderClick: () => void; onProviderChange: (providerId: string) => void }): React.JSX.Element {
  const id = editorIdentifier(model);
  const providerLabels = providers.map((item) => stringValue(item.name, translate("providers.newProvider")));
  const providerIndex = providers.findIndex((item) => editorIdentifier(item) === providerId);
  const providerLabel = providerLabels[Math.max(0, providerIndex)] ?? "";
  const keyNames = stringList(provider.api_key_names);
  const keyOptions = keyNames.map((name) => ({ value: name, label: apiKeyDisplayName(name, translate) }));
  const selectedKey = stringValue(model.api_key_name, keyNames[0] ?? "");
  const probePresentation = modelProbePresentation(model, probeResult, translate);
  const probeTooltip = screenBoundedTooltipText(probePresentation.full, Dimensions.get("screen"));
  return <View style={styles.inspectorContent}><View style={styles.modelBreadcrumb}><NativeButton title={providerLabel} link disabled={busy} onPress={onProviderClick} style={styles.breadcrumbProvider} /><Text style={styles.breadcrumbSeparator}>&gt;</Text><Text numberOfLines={1} style={styles.inspectorHeading}>{stringValue(model.name, translate("providers.newModel"))}</Text></View><View style={styles.inspectorDivider} /><View style={styles.inspectorBody}><View style={styles.inspectorEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(model.model_enabled, booleanValue(model.enabled, true))} disabled={busy} onValueChange={(model_enabled) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { model_enabled } })} style={styles.inspectorEnableControl} /><ActionButton title={probing ? translate("providers.probing") : translate("providers.probe")} disabled={busy || probing} onPress={probe} />{probePresentation.compact ? <TooltipText numberOfLines={2} tooltip={probeTooltip} accessibilityHint={probePresentation.full} style={styles.probeSummary}>{probePresentation.compact}</TooltipText> : null}</View><TextField label={translate("providers.publicModel")} labelWidth={60} value={stringValue(model.name)} onCommit={(name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { name } })} /><PickerField label={translate("providers.provider")} labelWidth={60} value={providerLabel} values={providerLabels} disabled={busy || providers.length <= 1} onSelect={(label) => { const next = providers.find((item) => stringValue(item.name, translate("providers.newProvider")) === label); if (next) onProviderChange(editorIdentifier(next)); }} /><PickerField label={translate("providers.keyName")} labelWidth={60} value={selectedKey} values={keyOptions.length > 0 ? keyOptions : [{ value: "", label: translate("common.notAvailable") }]} disabled={busy || keyNames.length === 0} onSelect={(api_key_name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { api_key_name } })} /><TextField label={translate("providers.upstream")} labelWidth={60} value={upstreamModelLabel(model)} onCommit={(upstream_model) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_model } })} /><TextField label={translate("common.order")} labelWidth={60} controlWidth={64} value={String(numberValue(model.order, 1))} keyboardType="numeric" onCommit={(order) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { order: Number(order) || 1 } })} /><ProtocolPicker providerId={providerId} model={model} busy={busy} translate={translate} dispatch={dispatch} /></View></View>;
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
    <PickerField label={translate("providers.protocolMode")} labelWidth={60} value={fixed ? "fixed" : "fallback"} values={modeOptions} disabled={busy} onSelect={(upstream_protocol_mode) => { void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_protocol_mode } }); }} />
    <PickerField label={fixed ? translate("providers.fixedProtocol") : translate("providers.fallbackProtocol")} labelWidth={60} value={protocol} values={options} disabled={busy} onSelect={(upstream_url_surface) => { void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_url_surface } }); }} />
    <Text style={styles.protocolHint}>{translate(fixed ? "providers.protocolModeFixedHint" : "providers.protocolModeFallbackHint")}</Text>
  </View>;
}

function providerRecord(provider: ProviderSummary): UnknownRecord {
  return { id: provider.id, name: provider.display_name, display_name: provider.display_name, enabled: provider.enabled, endpoint: provider.endpoint, model_count: provider.model_count, models: provider.models ?? [] };
}

function modelRecord(model: UnknownRecord): UnknownRecord {
  const modelEnabled = booleanValue(model.model_enabled, booleanValue(model.enabled, true));
  return { ...model, id: editorIdentifier(model), name: stringValue(model.name, stringValue(model.display_name, stringValue(model.model))), display_name: stringValue(model.display_name, stringValue(model.name)), enabled: modelEnabled, model_enabled: modelEnabled, order: numberValue(model.order, 1) };
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

function ProviderEditor({ provider, native, busy, translate, dispatch, onSecretState, sourceModel, onReturnToModel }: { provider: UnknownRecord; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; sourceModel?: UnknownRecord; onReturnToModel: () => void }): React.JSX.Element {
  const id = editorIdentifier(provider);
  const keys = stringList(provider.api_key_names);
  const keyStates = asRecords(provider.key_states);
  const [selectedKey, setSelectedKey] = useState<string>(keys[0] ?? "");
  useEffect(() => { if (!keys.includes(selectedKey)) setSelectedKey(keys[0] ?? ""); }, [keys, selectedKey]);
  const addKey = (): void => { const name = uniqueKeyName(keys); void dispatch("provider.key_add", { provider_id: id, name }).then(() => setSelectedKey(name)); };
  const renameKey = (name: string): void => { if (!selectedKey || !name || name === selectedKey) return; void dispatch("provider.key_patch", { provider_id: id, old_name: selectedKey, name }).then(() => setSelectedKey(name)); };
  const deleteKey = (): void => {
    if (!selectedKey || keys.length <= 1) return;
    const replacement = keys.find((key) => key !== selectedKey) ?? "";
    void native.showConfirmation({
      title: translate("providers.deleteApiKey"),
      message: `${apiKeyDisplayName(selectedKey, translate)} -> ${apiKeyDisplayName(replacement || "default", translate)}`,
      confirmLabel: translate("common.delete"),
    }).then((confirmed) => confirmed ? dispatch("provider.key_delete", { provider_id: id, name: selectedKey }).then(() => setSelectedKey(replacement)) : undefined);
  };
  const providerLabel = stringValue(provider.display_name, stringValue(provider.name, translate("providers.newProvider")));
  const sourceModelLabel = sourceModel ? stringValue(sourceModel.name, translate("providers.newModel")) : "";
  const selectedKeyConfigured = booleanValue(keyStates.find((state) => stringValue(state.name) === selectedKey)?.configured);
  const keyRows = keys.map((key) => ({ key, cells: [apiKeyDisplayName(key, translate)] }));
  return <View style={styles.providerEditorContent}>
    <View style={styles.providerEditorHeader}><Text numberOfLines={1} style={styles.providerEditorHeading}>{translate("providers.provider")}: {providerLabel}</Text>{sourceModel ? <NativeButton title={translate("providers.backToModel", { model: sourceModelLabel })} link disabled={busy} onPress={onReturnToModel} style={styles.providerReturnToModel} /> : null}</View>
    <View style={styles.providerEditorSection}><View style={styles.providerEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(provider.enabled, true)} disabled={busy} onValueChange={(enabled) => dispatch("provider.patch", { provider_id: id, changes: { enabled } })} /></View>
    <TextField label={translate("providers.baseUrl")} labelWidth={68} value={stringValue(provider.endpoint, stringValue(provider.api_base))} onCommit={(endpoint) => dispatch("provider.patch", { provider_id: id, changes: { endpoint } })} />
    <TextField label={translate("providers.providerName")} labelWidth={68} value={stringValue(provider.name, stringValue(provider.display_name))} onCommit={(name) => dispatch("provider.patch", { provider_id: id, changes: { name } })} />
    <View style={[styles.providerKeysEditor, styles.providerKeysEditorCompact]}>
      <View style={[styles.providerKeysHeader, styles.providerKeysHeaderCompact]}>
        <Text style={styles.providerKeysHeading}>{translate("providers.apiKeys")}</Text>
      </View>
      <View style={[styles.providerKeyGrid, styles.providerKeyGridCompact]}>
        <View style={[styles.providerKeyList, styles.providerKeyListCompact]}>
          <NativeTable columns={[{ label: translate("providers.key"), width: 100 }]} rows={keyRows} selectedKey={selectedKey} compact cellHorizontalPadding={0} firstColumnHorizontalPadding={0} onSelectionChange={setSelectedKey} style={styles.providerKeyTable} />
          <View style={[styles.providerKeyActions, styles.providerKeyActionsCompact]}>
            <IconButton label="+" title={translate("common.add")} disabled={busy} onPress={addKey} />
            <IconButton label="−" title={translate("common.delete")} disabled={busy || keys.length <= 1 || !selectedKey} onPress={deleteKey} />
          </View>
        </View>
        <View style={[styles.providerKeyFields, styles.providerKeyFieldsCompact]}>
          {selectedKey ? <>
            <TextField label={translate("providers.keyName")} labelWidth={42} value={selectedKey} onCommit={renameKey} />
            <NativeSecretField plainText autoCommit label={translate("providers.keyValue")} hint={selectedKeyConfigured ? translate("providers.apiKeySavedHint") : translate("providers.apiKeyInput")} labelWidth={42} busy={busy || !selectedKey} domain="providers_models" field="api_key" target={`${id}\x1f${selectedKey}`} onSecretState={onSecretState} />
          </> : <Text style={styles.empty}>{translate("common.notAvailable")}</Text>}
        </View>
      </View>
    </View>
    </View>
  </View>;
}

function CodexWorkspace({ snapshot, ipc, busy, translate, dispatch, onSecretState, rawReloadToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; rawReloadToken: number }): React.JSX.Element {
  const state = domainState(snapshot, "codex");
  const structured = asRecord(state.structured);
  const permissions = asRecord(structured.permissions);
  const providers = asRecords(structured.providers);
  const deployments = asRecords(state.models);
  const deploymentModels = [...new Set(deployments.map((item) => stringValue(item.model)).filter(Boolean))];
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const providerRows = providers.map(editableRecord);
  const directProvider = stringValue(structured.model_provider);
  const provider = providerRows.find((item) => identifier(item) === (selectedProvider ?? directProvider)) ?? providerRows[0];
  const directBaseUrl = stringValue(structured.openai_base_url);
  const providerOptions: AssistantSettingOption[] = [
    { value: "", label: translate("common.none") },
    ...[...new Set(["openai", directProvider, ...providerRows.map(identifier).filter(Boolean)])]
      .filter(Boolean)
      .map((value) => ({ value, label: value })),
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
    const nextProvider = { ...provider, ...changes };
    const nextProviderId = identifier(nextProvider);
    const patch: UnknownRecord = {
      providers: providerRows.map((item) => identifier(item) === currentProviderId ? nextProvider : item),
    };
    if (currentProviderId === directProvider && nextProviderId !== currentProviderId) {
      patch.model_provider = nextProviderId;
      patch.direct_connection = { provider: nextProviderId, base_url: stringValue(nextProvider.base_url) };
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
        <NativeTable columns={[{ label: translate("providers.providerId"), width: 116 }, { label: translate("providers.displayName"), width: 230 }, { label: translate("providers.authentication"), width: 84 }]} rows={providerRows.map((item) => ({ key: identifier(item), cells: [identifier(item), stringValue(item.name), stringValue(item.auth_mode, "none")] }))} selectedKey={provider ? identifier(provider) : ""} onSelectionChange={setSelectedProvider} style={styles.codexListTable} />
        <View style={styles.detailPane}>{provider ? <View style={styles.form}><TextField label={translate("providers.providerId")} value={identifier(provider)} onCommit={(id) => patchProvider({ id })} /><TextField label={translate("providers.displayName")} value={stringValue(provider.name)} onCommit={(name) => patchProvider({ name })} /><TextField label={translate("providers.baseUrl")} value={stringValue(provider.base_url)} onCommit={(base_url) => patchProvider({ base_url })} /><PickerField label={translate("providers.protocol")} value={stringValue(provider.wire_api, "responses")} values={["responses"]} disabled={busy} onSelect={(wire_api) => patchProvider({ wire_api })} /><PickerField label={translate("providers.authentication")} value={stringValue(provider.auth_mode, "none")} values={["none", "env_key", "openai_auth", "command", "bearer"]} disabled={busy} onSelect={(auth_mode) => patchProvider({ auth_mode })} /><TextField label={translate("codex.environmentKey")} value={stringValue(provider.env_key)} onCommit={(env_key) => patchProvider({ env_key })} /><NativeCheckbox label={translate("providers.requiresOpenAIAuth")} value={booleanValue(provider.requires_openai_auth)} disabled={busy} onValueChange={(requires_openai_auth) => patchProvider({ requires_openai_auth })} /><TextField label={translate("providers.authCommand")} value={stringValue(provider.auth_command)} onCommit={(auth_command) => patchProvider({ auth_command })} /></View> : <EmptyState translate={translate} />}</View>
      </View>
      </View>
    </Section>
    <Section title={translate("codex.model")}><View style={styles.form}>
      <PickerField label={translate("codex.activeDeployment")} value={stringValue(structured.model)} values={deploymentModels.length > 0 ? deploymentModels : [{ value: "", label: translate("common.none") }]} disabled={busy || deploymentModels.length === 0} onSelect={(model) => { const selection = deployments.find((item) => stringValue(item.model) === model); if (selection) dispatch("select_model", { selection: { model: selection.model, provider: selection.provider, deployment_id: selection.deployment_id } }); }} />
      <TextField label={translate("common.model")} value={stringValue(structured.model)} onCommit={(model) => dispatch("patch", { model })} />
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
  </>} raw={<><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawToml")} domain="codex" document="config" language="toml" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawAuth")} domain="codex" document="auth" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /></>} />;
}

function SettingsWorkspace({ validationStatus, validationStatusStyle, structuredWidth, onStructuredWidthChange, workspaceWidth, onWorkspaceWidthChange, translate, missingMessage, structured, raw }: { validationStatus?: string; validationStatusStyle?: StyleProp<TextStyle>; structuredWidth: number; onStructuredWidthChange: (width: number) => void; workspaceWidth: number; onWorkspaceWidthChange: (width: number) => void; translate: Translate; missingMessage?: string; structured: React.ReactNode; raw: React.ReactNode }): React.JSX.Element {
  const rawPaneMinimum = 344;
  const minStructuredWidth = 360;
  const maxStructuredWidth = workspaceWidth > 0
    ? Math.max(minStructuredWidth, Math.min(680, workspaceWidth - rawPaneMinimum))
    : 470;
  const paneWidth = Math.max(minStructuredWidth, Math.min(structuredWidth, maxStructuredWidth));
  return <View style={styles.codexWorkspaceFrame} onLayout={({ nativeEvent }) => onWorkspaceWidthChange(nativeEvent.layout.width)}>{validationStatus ? <Text style={[styles.codexValidationStatus, validationStatusStyle]}>{validationStatus}</Text> : null}{missingMessage ? <Text style={styles.settingsMissingMessage}>{missingMessage}</Text> : null}<NativeSplitView paneWidth={paneWidth} minPaneWidth={minStructuredWidth} maxPaneWidth={maxStructuredWidth} onPaneWidthChange={(width) => onStructuredWidthChange(Math.max(minStructuredWidth, Math.min(width, maxStructuredWidth)))} style={styles.codexSplit}><View style={styles.codexStructuredPane}><Text style={styles.paneHeading}>{translate("settings.structured")}</Text><ScrollView style={styles.codexStructuredScroll} contentContainerStyle={styles.codexStructured}>{structured}</ScrollView></View><View style={styles.codexRawPane}><Text style={styles.paneHeading}>{translate("settings.rawLiveDraft")}</Text><View style={styles.codexRawEditors}>{raw}</View></View></NativeSplitView></View>;
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

function ClaudeScreen({ snapshot, ipc, busy, translate, dispatch, onSecretState, deployment, onDeploymentChange, rawReloadToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; deployment: ClaudeDeploymentDraft; onDeploymentChange: (key: keyof ClaudeDeploymentDraft, value: string) => Promise<void>; rawReloadToken: number }): React.JSX.Element {
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
  </>} raw={<><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.desktopRawJson")} domain="claude" document="desktop" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.developerRawJson")} domain="claude" document="developer" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.codeRawJson")} domain="claude" document="settings" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /></>} />;
}

function claudePermissionLabel(value: string, translate: Translate): string {
  const key = `claude.permission.${value}`;
  return CLAUDE_PERMISSION_MODES.includes(value) ? translate(key) : translate("claude.permission.unknown", { value });
}

const CLAUDE_PERMISSION_MODES = ["default", "manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "delegate"];

function RuntimeWorkspace({ snapshot, ipc, native, busy, translate, dispatch, onSnapshot, onSecretState, clearSecret }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSnapshot: (next: CoreSnapshot) => void; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear }): React.JSX.Element {
  const state = domainState(snapshot, "runtime");
  const settings = asRecords(state.settings).length > 0 ? asRecords(state.settings) : asRecords(state.categories).flatMap((category) => asRecords(category.settings));
  const groups = groupBy(settings, (item) => stringValue(item.category, translate("runtime.categories")));
  const [contentWidth, setContentWidth] = useState(0);
  const oneColumn = contentWidth > 0 && contentWidth < 1_000;
  const importFile = async (): Promise<void> => {
    const fileToken = await native.openFilePicker({ purpose: "import" });
    if (!fileToken || !snapshot) return;
    await ipc.import(fileToken, snapshot.revision, ["runtime"]);
    onSnapshot(await ipc.snapshot());
  };
  const exportFile = async (): Promise<void> => {
    const fileToken = await native.saveFilePicker({ suggestedName: "runtime-settings.json" });
    if (!fileToken) return;
    await ipc.export(["runtime"], fileToken);
  };
  return <View style={styles.runtimeWorkspaceFrame}><View style={styles.runtimeFileToolbar}><ActionButton title={translate("runtime.importFile")} disabled={busy} onPress={() => { void importFile(); }} /><ActionButton title={translate("runtime.exportFile")} disabled={busy} onPress={() => { void exportFile(); }} /></View><ScrollView style={styles.runtimeScrollSurface} contentContainerStyle={styles.runtimeWorkspace} onLayout={({ nativeEvent }) => setContentWidth(nativeEvent.layout.width)}>{Object.keys(groups).length === 0 ? <EmptyState translate={translate} /> : Object.entries(groups).map(([category, entries]) => <Section key={category} title={runtimeCategoryLabel(category, translate)}><View style={[styles.runtimeTwoColumnForm, oneColumn && styles.runtimeOneColumnForm]}>{entries.map((item) => <RuntimeField key={identifier(item)} item={item} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} />)}</View></Section>)}</ScrollView></View>;
}

function RuntimeField({ item, busy, translate, dispatch, onSecretState, clearSecret }: { item: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear }): React.JSX.Element {
  const key = identifier(item);
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
    const optionLabels = optionValues.map((option) => runtimeOptionLabel(key, option, translate));
    const selectedIndex = optionValues.indexOf(value);
    control = <NativePicker labels={optionLabels} selectedValue={optionLabels[selectedIndex] ?? optionLabels[0] ?? ""} disabled={busy} onChange={({ nativeEvent }) => { const next = optionValues[nativeEvent.index]; if (next !== undefined) void dispatch("set_setting", { key, value: next }); }} style={styles.runtimeValueControl} />;
  } else if (item.secret === true) {
    control = <NativeSecretInputControl label={label} hint={item.retained === true ? translate("runtime.secretRetained") : undefined} busy={busy} domain="runtime" field="setting" target={key} onSecretState={onSecretState} setTitle={translate("common.set")} />;
    action = <ActionButton title={item.will_clear === true ? translate("common.willClear") : translate("common.clear")} disabled={busy || item.retained !== true || item.will_clear === true} onPress={() => clearSecret({ domain: "runtime", field: "setting", target: key })} />;
  } else {
    control = <RuntimeValueField label={label} value={value} keyboardType={["number", "integer", "int", "float", "mb"].includes(storageKind) ? "numeric" : undefined} onCommit={(next) => dispatch("set_setting", { key, value: next })} />;
  }
  const isBoolean = kind === "boolean" || kind === "toggle" || kind === "bool" || kind === "bool_auto";
  return <View style={styles.runtimeField}><View style={styles.runtimeInputRow}><Text numberOfLines={2} style={styles.runtimeFieldLabel} accessibilityLabel={label}>{label}</Text><View style={styles.runtimeValueSlot}>{control}</View>{!isBoolean && unit ? <Text numberOfLines={1} style={styles.runtimeUnit}>{unit}</Text> : null}{!isBoolean ? <View style={styles.runtimeActionSlot}>{action}</View> : null}</View><RuntimeFieldMeta item={item} translate={translate} /></View>;
}

function RuntimeFieldMeta({ item, translate }: { item: UnknownRecord; translate: Translate }): React.JSX.Element {
  const key = identifier(item);
  const kind = stringValue(item.kind, "text");
  const rawDefaultValue = stringValue(item.default, translate("common.empty"));
  const defaultValue = kind === "select" || kind === "choice" || kind === "enum"
    ? runtimeOptionLabel(key, rawDefaultValue, translate)
    : rawDefaultValue;
  const help = runtimeFieldHelp(key, stringValue(item.help), translate);
  return <View style={styles.runtimeHelpSlot}><Text style={styles.runtimeHelpText}>{translate("common.default")}: {defaultValue}{help ? `\n${help}` : ""}</Text></View>;
}

type PendingTextFieldState = {
  draft: string;
  onChangeText: (next: string) => void;
  commit: () => Promise<void>;
  reset: () => void;
  isDirty: () => boolean;
};

// Keep text local while typing, advertise that local edit synchronously, and
// stage it after a short idle period. Apply/blur still call commit directly so
// no keystroke is lost when the user immediately leaves the field.
function usePendingTextField(value: string, onCommit: (next: string) => void | Promise<void>, label: string): PendingTextFieldState {
  const [draft, setDraft] = useState(value);
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(label));
  const draftRef = useRef(value);
  const committedRef = useRef(value);
  const valueRef = useRef(value);
  const dirtyRef = useRef(false);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const commitInFlight = useRef<Promise<void> | undefined>(undefined);
  const onCommitRef = useRef(onCommit);

  useEffect(() => { onCommitRef.current = onCommit; }, [onCommit]);
  useEffect(() => {
    valueRef.current = value;
    committedRef.current = value;
    if (!dirtyRef.current) {
      draftRef.current = value;
      setDraft(value);
    }
  }, [value]);

  const setDirty = useCallback((dirty: boolean): void => {
    dirtyRef.current = dirty;
    registry?.setDirty(fieldId.current, dirty);
  }, [registry]);

  const commit = useCallback(async (): Promise<void> => {
    if (debounceTimer.current !== undefined) {
      clearTimeout(debounceTimer.current);
      debounceTimer.current = undefined;
    }
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
    // If an earlier staged value is still in flight, returning to the last
    // known draft still needs one more stage after that write finishes.
    setDirty(next !== committedRef.current || commitInFlight.current !== undefined);
    if (debounceTimer.current !== undefined) clearTimeout(debounceTimer.current);
    if (next === committedRef.current) {
      debounceTimer.current = undefined;
      return;
    }
    debounceTimer.current = setTimeout(() => {
      debounceTimer.current = undefined;
      void commit().catch(() => undefined);
    }, 240);
  }, [commit, setDirty]);

  const reset = useCallback((): void => {
    if (debounceTimer.current !== undefined) {
      clearTimeout(debounceTimer.current);
      debounceTimer.current = undefined;
    }
    const next = valueRef.current;
    committedRef.current = next;
    draftRef.current = next;
    setDraft(next);
    setDirty(false);
  }, [setDirty]);

  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset, isDirty: () => dirtyRef.current });
    return () => {
      if (debounceTimer.current !== undefined) clearTimeout(debounceTimer.current);
      registry?.register(fieldId.current);
    };
  }, [commit, registry, reset]);

  return { draft, onChangeText, commit, reset, isDirty: () => dirtyRef.current };
}

function RuntimeValueField({ label, value, keyboardType, onCommit }: { label: string; value: string; keyboardType?: "default" | "numeric"; onCommit: (value: string) => void | Promise<void> }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label);
  return <NativeTextField style={[styles.input, styles.runtimeValueControl]} value={field.draft} onChangeText={field.onChangeText} onBlur={() => { void field.commit().catch(() => undefined); }} onSubmitEditing={() => { void field.commit().catch(() => undefined); }} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />;
}

function WebDavWorkspace({ snapshot, busy, translate, dispatch, onSecretState }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void }): React.JSX.Element {
  const state = domainState(snapshot, "webdav");
  return <View style={styles.webDavForm}><View style={styles.webdavStateRow}><NativeCheckbox label={translate("webdav.enabled")} value={booleanValue(state.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { enabled })} style={styles.webdavEnabledControl} /><Text numberOfLines={2} style={styles.webdavInlineStatus}>{snapshot ? webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate) : ""}</Text></View><View style={styles.webdavFormRows}>
    <TextField label={translate("webdav.url")} value={stringValue(state.url)} labelWidth={94} onCommit={(url) => dispatch("patch", { url })} />
    <TextField label={translate("webdav.username")} value={stringValue(state.username)} labelWidth={94} onCommit={(username) => dispatch("patch", { username })} />
    <WebDavPasswordField configured={snapshot?.webdav.password.present === true} busy={busy} translate={translate} onSecretState={onSecretState} />
    <TextField label={translate("webdav.remoteFile")} value={stringValue(state.remote_name)} labelWidth={94} onCommit={(remote_name) => dispatch("patch", { remote_name })} />
    <TextField label={translate("webdav.syncEvery")} value={stringValue(state.sync_interval)} labelWidth={94} controlWidth={140} suffix={translate("webdav.minutes")} keyboardType="numeric" onCommit={(sync_interval) => dispatch("patch", { sync_interval })} />
    <TextField label={translate("webdav.httpTimeout")} value={stringValue(state.timeout)} labelWidth={94} controlWidth={140} suffix={translate("webdav.seconds")} keyboardType="numeric" onCommit={(timeout) => dispatch("patch", { timeout })} />
  </View></View>;
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
  return <View style={styles.formRow}><Text style={[styles.formRowLabel, { width: 94 }]}>{translate("webdav.password")}</Text><View style={styles.formRowControl}><NativeSecureTextInput domain="webdav" field="password" label={translate("webdav.password")} placeholder={configured ? translate("webdav.passwordHintConfigured") : translate("webdav.passwordHintOptional")} disabled={busy || status === "saving"} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(state) => {
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
  const [hoveredKey, setHoveredKey] = useState<string>();
  const [appActive, setAppActive] = useState(() => AppState.currentState === "active");
  const selected = requests.find((request) => request.key === selectedKey) ?? requests[0];
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => setAppActive(state === "active"));
    return () => subscription.remove();
  }, []);
  const openOriginalRecords = (): void => {
    if (!selected) return;
    void native.showReadOnlyText({
      title: translate("logs.originalRecord"),
      text: selected.rows.map((row) => row.original).join("\n\n"),
      closeLabel: translate("menu.close"),
    });
  };
  return <View style={styles.routeTraceWorkspace}>
    <View style={styles.routeTraceRequestPane}>
      <FlatList
        style={styles.routeTraceRequestScroll}
        contentContainerStyle={styles.routeTraceRequestList}
        data={requests}
        keyExtractor={(request) => request.key}
        initialNumToRender={12}
        maxToRenderPerBatch={12}
        windowSize={7}
        renderItem={({ item: request }) => {
          const isSelected = selected?.key === request.key;
          const selectedTextStyle = isSelected && appActive ? styles.routeTraceRequestTextSelected : null;
          const requestDescription = [request.model, request.time, request.routePath, routeTraceOutcomeLabel(request.outcome, translate, request.attempts.length)].filter(Boolean).join(" · ");
          return <Pressable
            key={request.key}
            style={({ pressed }) => [
              styles.routeTraceRequestRow,
              !isSelected && hoveredKey === request.key && styles.routeTraceRequestRowHovered,
              !isSelected && pressed && styles.routeTraceRequestRowPressed,
              isSelected && (appActive ? styles.routeTraceRequestRowSelected : styles.routeTraceRequestRowSelectedInactive),
            ]}
            onPress={() => onSelect(request.key)}
            onFocus={() => onSelect(request.key)}
            onHoverIn={() => setHoveredKey(request.key)}
            onHoverOut={() => setHoveredKey((current) => current === request.key ? undefined : current)}
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
        <ScrollView style={styles.routeTraceTimelineScroll} contentContainerStyle={styles.routeTraceTimeline} showsVerticalScrollIndicator>
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
    const interval = setInterval(() => { void poll(); }, selected === "online-usage" ? ONLINE_USAGE_POLL_MS : LOG_VIEW_POLL_MS);
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
      <View style={styles.logActionsRow}><IconButton label="" symbol={paused ? "play" : "pause"} title={paused ? translate("common.resume") : translate("common.pause")} disabled={busy} onPress={togglePaused} /><IconButton label="" symbol="trash" title={translate("common.clearView")} disabled={busy} onPress={clearLogs} /></View>
    </View>
    <WindowTabs nativeRef={tabsRef} values={tabOptions} selected={selected} disabled={busy} onSelect={(tab) => {
      if (clearTabRef.current) return;
      setSelected(tab as LogTab);
      if (tab === "online-usage") openRelayUsageLogs();
    }} style={styles.logsTabs} />
    {rows.length > 0 ? selected === "route-trace"
      ? <RouteTraceWorkspace requests={routeTraceRequests} selectedKey={selectedKey} native={native} translate={translate} onSelect={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} />
      : <View style={styles.logTableFrame} onLayout={({ nativeEvent }) => setTableWidth(nativeEvent.layout.width)}><NativeTable columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact onSelectionChange={(key) => setSelectedKeys((current) => ({ ...current, [selected]: key }))} onRowDoublePress={(_key, index) => {
        const row = rows[index];
        if (!row) return;
        void native.showReadOnlyText({ title: translate("logs.originalRecord"), text: row.original, closeLabel: translate("menu.close") });
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

function TextField({ label, value, onCommit, hint, secret, multiline, compactMultiline, keyboardType, stacked, labelWidth, labelAlign, controlWidth, suffix, disabled }: { label: string; value: string; onCommit: (value: string) => void | Promise<void>; hint?: string; secret?: boolean; multiline?: boolean; compactMultiline?: boolean; keyboardType?: "default" | "numeric"; stacked?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; suffix?: string; disabled?: boolean }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label);
  return <View style={[styles.formRow, compactStyles.formRow, (stacked || multiline) && styles.formRowStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }, (stacked || multiline) && styles.formRowLabelStacked]}>{label}</Text><View style={[styles.formRowControl, compactStyles.formRowControl, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]}><NativeTextField style={[styles.input, compactStyles.input, multiline && styles.textArea, compactMultiline && styles.compactTextArea]} value={field.draft} editable={!disabled} onChangeText={field.onChangeText} onBlur={() => { if (!disabled) void field.commit().catch(() => undefined); }} onSubmitEditing={multiline ? undefined : () => { if (!disabled) void field.commit().catch(() => undefined); }} multiline={multiline} secureTextEntry={secret} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />{hint ? <Text style={styles.fieldHint}>{hint}</Text> : null}</View>{suffix ? <Text style={styles.fieldHint}>{suffix}</Text> : null}</View>;
}

function NativeSecretInputControl({ label, hint, busy, domain, field, target, plainText = false, autoCommit = false, resetToken = 0, onSecretState, setTitle, setBelow, onSetReady, inputMinWidth }: { label: string; hint?: string; busy: boolean; domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; plainText?: boolean; autoCommit?: boolean; resetToken?: number; onSecretState: (state: SecretState) => void; setTitle?: string; setBelow?: boolean; onSetReady?: (requestSet: () => void, saving: boolean) => void; inputMinWidth?: number }): React.JSX.Element {
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
  return <View style={[styles.nativeSecretControl, compactStyles.nativeSecretControl]}><NativeSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={hint ?? ""} plainText={plainText} autoCommit={autoCommit} disabled={busy} commitRequest={commitRequest} resetRequest={resetRequest + resetToken} onSecretState={(state) => {
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
        }, 240);
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
  }} style={[styles.nativeSecretInput, compactStyles.input, inputMinWidth === undefined ? null : { minWidth: inputMinWidth }]} />{!autoCommit && !setBelow && setTitle ? <NativeButton title={setTitle} compact disabled={busy || status === "saving"} onPress={requestCommit} style={styles.secretActionButton} /> : null}</View>;
}

function NativeSecretField({ label, hint, busy, domain, field, target, plainText = false, autoCommit = false, onSecretState, labelWidth, labelAlign, setTitle, clearTitle, clearDisabled, onClear, actionsBelow }: { label: string; hint?: string; busy: boolean; domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; plainText?: boolean; autoCommit?: boolean; onSecretState: (state: SecretState) => void; labelWidth?: number; labelAlign?: "left" | "right"; setTitle?: string; clearTitle?: string; clearDisabled?: boolean; onClear?: () => Promise<void>; actionsBelow?: boolean }): React.JSX.Element {
  const setAction = useRef<() => void>(() => undefined);
  const [saving, setSaving] = useState(false);
  const [resetToken, setResetToken] = useState(0);
  const handleSetReady = React.useCallback((requestSet: () => void, nextSaving: boolean): void => { setAction.current = requestSet; setSaving(nextSaving); }, []);
  const handleClear = React.useCallback((): void => {
    if (!onClear) return;
    void onClear().then(() => setResetToken((current) => current + 1));
  }, [onClear]);
  return <View style={[styles.formRow, compactStyles.formRow, actionsBelow && styles.formRowSecretStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><View style={[styles.formRowControl, compactStyles.formRowControl]}>{actionsBelow ? <><NativeSecretInputControl label={label} hint={hint} busy={busy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} setBelow onSetReady={handleSetReady} inputMinWidth={110} /><View style={[styles.secretFieldButtons, compactStyles.inlineGap]}>{!autoCommit && setTitle ? <NativeButton title={setTitle} compact disabled={busy || saving} onPress={() => setAction.current()} style={styles.secretFieldButton} /> : null}{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? busy} onPress={handleClear} style={styles.secretFieldButton} /> : null}</View></> : <View style={[styles.secretFieldActions, compactStyles.inlineGap]}><NativeSecretInputControl label={label} hint={hint} busy={busy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} />{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? busy} onPress={handleClear} style={styles.secretActionButton} /> : null}</View>}</View></View>;
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

function PickerField({ label, value, values, onSelect, disabled, labelWidth, labelAlign, controlWidth, translate }: { label: string; value: string; values: Array<string | AssistantSettingOption>; onSelect: (value: string) => void; disabled?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; translate?: Translate }): React.JSX.Element {
  const contextualTranslate = useContext(TranslationContext);
  const optionTranslator = translate ?? contextualTranslate;
  const options = ensureSelectedOption(optionTranslator ? assistantSettingOptions(values, optionTranslator) : values.map((option) => typeof option === "string" ? { value: option, label: option } : option), value);
  const selectedLabel = options.find((option) => option.value === value)?.label ?? value;
  return <View style={[styles.formRow, compactStyles.formRow]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><NativePicker labels={options.map((option) => option.label)} selectedValue={selectedLabel} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={[styles.picker, compactStyles.picker, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]} /></View>;
}

function RawEditor({ label, domain, document, language, ipc, busy, translate, showReload = true, codexPane = false, reloadToken = 0, style }: { label: string; domain: "codex" | "claude"; document: "config" | "auth" | "settings" | "desktop" | "developer"; language: "toml" | "json"; ipc: IpcClient; busy: boolean; translate: Translate; showReload?: boolean; codexPane?: boolean; reloadToken?: number; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const [editorToken, setEditorToken] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [nativeStatus, setNativeStatus] = useState<string>();
  const [nativeErrorCode, setNativeErrorCode] = useState<string>();
  const [error, setError] = useState<string>();
  const [reloadNonce, setReloadNonce] = useState(0);
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(`${domain}:${document}:raw`));
  const nativeStatusRef = useRef<string | undefined>(undefined);
  const pendingCommit = useRef<{ promise: Promise<void>; resolve: () => void; reject: (reason: Error) => void } | undefined>(undefined);
  const commit = useCallback((): Promise<void> => {
    if (nativeStatusRef.current !== "dirty" && nativeStatusRef.current !== "saving") return Promise.resolve();
    if (pendingCommit.current) return pendingCommit.current.promise;
    let resolvePromise!: () => void;
    let rejectPromise!: (reason: Error) => void;
    const promise = new Promise<void>((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    pendingCommit.current = { promise, resolve: resolvePromise, reject: rejectPromise };
    return promise;
  }, []);
  const reset = useCallback((): void => {
    pendingCommit.current?.resolve();
    pendingCommit.current = undefined;
    nativeStatusRef.current = "ready";
    registry?.setDirty(fieldId.current, false);
  }, [registry]);
  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset, isDirty: () => nativeStatusRef.current === "dirty" || nativeStatusRef.current === "saving" });
    return () => {
      pendingCommit.current?.resolve();
      pendingCommit.current = undefined;
      registry?.register(fieldId.current);
    };
  }, [commit, registry, reset]);
  useEffect(() => {
    let active = true;
    reset();
    setEditorToken(undefined);
    setLoading(true);
    setNativeStatus(undefined);
    setNativeErrorCode(undefined);
    setError(undefined);
    void ipc.editor(domain, document).then((descriptor) => {
      if (!active) return;
      setEditorToken(descriptor.editor_token);
      setLoading(false);
    }).catch(() => {
      if (!active) return;
      setLoading(false);
      setError(translate("error.coreUnavailable"));
    });
    return () => { active = false; };
  }, [document, domain, ipc, reloadNonce, reloadToken, reset, translate]);
  const reloadEditor = (): void => {
    if (nativeStatus === "dirty" || nativeStatus === "saving" || nativeErrorCode === "stage_failed" || nativeErrorCode === "invalid_text") return;
    setReloadNonce((value) => value + 1);
  };
  const nativeLoading = editorToken !== undefined && (nativeStatus === undefined || nativeStatus === "loading");
  const nativeReadFailed = nativeStatus === "error" && nativeErrorCode !== "stage_failed" && nativeErrorCode !== "invalid_text";
  const reloadDisabled = busy || loading || nativeLoading || nativeStatus === "dirty" || nativeStatus === "saving" || nativeErrorCode === "stage_failed" || nativeErrorCode === "invalid_text";
  return <View style={[styles.rawEditor, codexPane && styles.codexRawEditorBase, style]}><View style={[styles.rawEditorHeader, codexPane && styles.codexRawEditorHeader]}><Text style={[styles.fieldLabel, codexPane && styles.codexRawEditorLabel]}>{label}</Text>{showReload ? <ActionButton title={translate("menu.reload")} disabled={reloadDisabled} onPress={reloadEditor} /> : null}</View>{codexPane ? null : <Text style={styles.fieldHint}>{translate("settings.rawProtectedHint")}</Text>}{editorToken ? <View style={styles.rawNativeEditorFrame}><NativeSecureTextEditor editorToken={editorToken} language={language} unavailableLabel={translate("common.secureEditorUnavailable")} style={[styles.rawNativeEditor, codexPane && styles.codexRawNativeEditor]} onEditorState={({ status, error: nextNativeErrorCode }) => { nativeStatusRef.current = status; setNativeStatus(status); setNativeErrorCode(nextNativeErrorCode || undefined); const pending = pendingCommit.current; if (status === "dirty" || status === "saving") registry?.setDirty(fieldId.current, true); else { registry?.setDirty(fieldId.current, false); if (pending) { pendingCommit.current = undefined; if (status === "error") pending.reject(new Error(nextNativeErrorCode || "Raw editor could not be staged")); else pending.resolve(); } } if (!nextNativeErrorCode) { setError(undefined); return; } setError(nextNativeErrorCode === "stage_failed" ? translate("common.secureEditorStageFailed") : nextNativeErrorCode === "invalid_text" ? translate("common.invalidText") : translate("common.secureEditorReadFailed")); }} />{nativeLoading ? <View pointerEvents="none" style={styles.rawEditorOverlay}><Text style={styles.cardHint}>{translate("common.secureEditorLoading")}</Text></View> : null}{nativeReadFailed ? <View style={styles.rawEditorOverlay}><Text style={styles.error}>{error ?? translate("common.secureEditorReadFailed")}</Text><ActionButton title={translate("menu.reload")} disabled={busy} onPress={reloadEditor} /></View> : null}</View> : <View style={[styles.rawEditorLoading, codexPane && styles.codexRawEditorLoading]}><Text style={styles.cardHint}>{loading ? translate("common.loading") : translate("error.coreUnavailable")}</Text></View>}{error && editorToken && !nativeReadFailed ? <Text style={styles.error}>{error}</Text> : null}</View>;
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
  const compactRequest = requests.length > 0
    ? requests.map((item) => `${item.surface}: ${stringValue(asRecord(item.original_request).url) || translate("common.none")}`).join("\n")
    : result?.detail ?? "";
  const compact = [summary, compactRequest ? translate("providers.probeOriginalRequest", { request: compactRequest }) : ""].filter(Boolean).join("\n");
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
    const message = translate(keyByCode[issue.code] ?? "error.validationFailed");
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
function emptyToNull(value: string, translate?: Translate): string | null { return value === "(Empty)" || value === translate?.("common.empty") ? null : value; }
function uniqueKeyName(existing: string[]): string { let suffix = 1; let value = `key-${suffix}`; while (existing.includes(value)) { suffix += 1; value = `key-${suffix}`; } return value; }
function splitLines(value: string): string[] { return value.split("\n").map((item) => item.trim()).filter(Boolean); }
function splitCommaLines(value: string): string[] { return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean); }
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

const styles = StyleSheet.create({
  routeTraceWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 6 },
  routeTraceRequestPane: { width: "31%", minWidth: 252, maxWidth: 360, minHeight: 0, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground },
  routeTraceRequestScroll: { flex: 1, minHeight: 0 },
  routeTraceRequestList: { flexGrow: 1 },
  routeTraceRequestRow: { minHeight: 70, paddingHorizontal: 9, paddingVertical: 8, gap: 3, borderBottomWidth: 1, borderBottomColor: systemColors.separator },
  routeTraceRequestRowHovered: { backgroundColor: systemColors.unemphasizedSelectedContent },
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
  routeTraceTimelineScroll: { flex: 1, minHeight: 0, borderTopWidth: 1, borderTopColor: systemColors.separator },
  routeTraceTimeline: { flexGrow: 1, paddingHorizontal: 14, paddingVertical: 14, gap: 10 },
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
  routeTraceInfoText: { color: systemColors.label },
  root: { flex: 1, minWidth: 420, backgroundColor: systemColors.window },
  menuBarHost: { flex: 1 }, error: { margin: 20, color: systemColors.red, fontSize: UI_FONT_SIZE },
  windowSurface: { flex: 1, backgroundColor: systemColors.window }, windowContent: { flexGrow: 1, paddingHorizontal: 16, paddingTop: 12, paddingBottom: 6, gap: 8 }, windowContentFixed: { flex: 1, minHeight: 0 }, providersContent: { paddingBottom: 6, gap: 6 }, settingsContent: { paddingHorizontal: 20, paddingTop: 8, paddingBottom: 0, gap: 6 }, logsContent: { paddingHorizontal: 12, paddingTop: 8, paddingBottom: 0 }, runtimeContent: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 0 }, webDavContent: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 0 }, windowTitleBlock: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 3, gap: 3 }, windowTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, validationText: { color: systemColors.red, fontSize: UI_FONT_SIZE },
  footer: { height: 52, minHeight: 52, flexShrink: 0, flexDirection: "row", alignItems: "center", paddingHorizontal: 16, paddingVertical: 8, gap: 6 }, footerStatus: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, flexShrink: 1 }, footerSpacer: { flex: 1 }, footerButtons: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 }, wideButton: { minWidth: 92 }, runtimeRestoreButton: { minWidth: 120 },
  providerToolbar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, toolbarSpacer: { flex: 1 }, windowTabs: { width: 224, height: 24 }, settingsTabBar: { minHeight: 36, justifyContent: "center", borderBottomWidth: 1, borderBottomColor: systemColors.separator }, settingsTabs: { alignSelf: "flex-start", width: 250 }, windowTab: {}, windowTabSelected: {}, windowTabText: {},
  routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 },
  providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 6 }, providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }, providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: 6 }, routeWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, importSourcePicker: { width: 152, height: 24 }, fetchKeyPicker: { width: 170, height: 24, marginRight: 6, flexShrink: 0 }, providerThreePane: { flex: 1, minHeight: 0 }, providerListPane: { width: 154, minWidth: 154, maxWidth: 154, flexGrow: 0, flexShrink: 0 }, modelListPane: { flex: 1, minWidth: 0 }, providerInspectorPane: { minWidth: 280 }, tablePane: { flex: 1, minWidth: 0, gap: 6 }, tablePaneWide: { flex: 1, minWidth: 0 }, tableTitleRow: { height: 24, flexDirection: "row", alignItems: "center" }, tableTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, tableActions: { marginLeft: "auto", flexDirection: "row", gap: 6 }, iconButton: { minWidth: 22, width: 22, minHeight: 22, height: 22, alignItems: "center", justifyContent: "center" }, iconButtonText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, tableHeader: { height: 24, flexDirection: "row", alignItems: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.window }, tableHeaderText: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 6, fontWeight: "500" }, tableScroll: { flex: 1, minHeight: 0, borderWidth: 1, borderTopWidth: 0, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, tableRows: { flexGrow: 1 }, tableRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, tableRowSelected: { backgroundColor: systemColors.control }, tableCellText: { color: systemColors.label, fontSize: UI_FONT_SIZE, paddingHorizontal: 6 }, providerNameColumn: { flex: 1 }, countColumn: { width: 48, textAlign: "right" }, modelNameColumn: { width: 96 }, modelUpstreamColumn: { flex: 1, minWidth: 112 }, routeModelColumn: { width: 136 }, routeOrderColumn: { width: 48, textAlign: "right" }, routeProviderColumn: { width: 112 }, routeUpstreamColumn: { flex: 1, minWidth: 136 }, tableBottomRow: { minHeight: 26, flexDirection: "row", alignItems: "center" }, nativeProviderTable: { flex: 1, minHeight: 0 }, nativeModelTable: { flex: 1, minHeight: 0 }, nativeRouteTable: { flex: 1, minHeight: 0 }, providerInspector: { width: 280, minWidth: 280, maxWidth: 280, flexGrow: 0, flexShrink: 0 }, providerEditorContent: { flex: 1, minHeight: 0, paddingTop: 3, paddingHorizontal: 12, paddingRight: 8, paddingBottom: 12, gap: 6 }, providerEditorHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerEditorHeading: { flex: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerReturnToModel: { flexShrink: 1 }, providerEditorSection: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 3, gap: 4 }, providerEnabledRow: { minHeight: 22, flexDirection: "row", alignItems: "center" }, inspectorContent: { paddingTop: 3, paddingHorizontal: 12, paddingRight: 6, paddingBottom: 12, gap: 6 }, inspectorBody: { gap: 4 }, modelBreadcrumb: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }, breadcrumbProvider: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, breadcrumbSeparator: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorHeading: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, inspectorDivider: { height: 1, backgroundColor: systemColors.separator }, inspectorEnabledRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, inspectorEnableControl: { flexShrink: 0 }, probeSummary: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, protocolSettings: { gap: 4 }, protocolHint: { marginLeft: 62, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, providerKeysEditor: { gap: 4 }, providerKeysHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 }, providerKeysHeading: { flex: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, providerKeyGrid: { flex: 1, minHeight: 164, flexDirection: "row", alignItems: "flex-start", gap: 8 }, providerKeyList: { width: 100, minWidth: 100, maxWidth: 100, flexShrink: 0, gap: 3 }, providerKeyTable: { width: 100, minWidth: 100, maxWidth: 100, height: 136, minHeight: 136, flexShrink: 0 }, providerKeyFields: { flex: 1, minWidth: 0, gap: 4 }, providerKeyActions: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4, flexShrink: 0 },
  providerKeysEditorCompact: { gap: 4 }, providerKeysHeaderCompact: { minHeight: 22, gap: 4 }, providerKeyGridCompact: { minHeight: 164, gap: 8 }, providerKeyListCompact: { gap: 2 }, providerKeyFieldsCompact: { gap: 4 }, providerKeyActionsCompact: { minHeight: 22, gap: 3 },
  codexWorkspace: { flex: 1, minHeight: 0 }, codexWorkspaceFrame: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexValidationStatus: { flexShrink: 0, marginHorizontal: 8, fontSize: UI_FONT_SIZE }, settingsMissingMessage: { flexShrink: 0, marginHorizontal: 8, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, codexValidationWarning: { color: systemColors.brown }, codexValidationError: { color: systemColors.red }, codexSplit: { flex: 1, minWidth: 0, minHeight: 0 }, codexStructuredPane: { flex: 1, minWidth: 360, paddingHorizontal: 8 }, codexStructuredScroll: { flex: 1, minWidth: 0, marginTop: 7 }, codexStructured: { flexGrow: 1, gap: 14, paddingHorizontal: 16, paddingTop: 10, paddingBottom: 16 }, codexRawPane: { flex: 1, flexShrink: 1, minWidth: 320, minHeight: 0, gap: 8, paddingHorizontal: 8, overflow: "hidden" }, codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexRawEditorBase: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0, gap: 5 }, codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0 }, codexRawEditorHeader: { minHeight: 18 }, codexRawEditorLabel: { fontFamily: Platform.select({ macos: "Menlo", windows: "Cascadia Mono", default: "monospace" }), fontWeight: "600" }, codexRawNativeEditor: { minHeight: 0 }, codexRawEditorLoading: { minHeight: 0 }, paneHeading: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, section: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 10, gap: 8 }, sectionHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }, sectionTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, codexProviderEditor: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 6, backgroundColor: systemColors.control, overflow: "hidden" }, codexProviderToolbar: { minHeight: 42, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12, paddingHorizontal: 10, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: systemColors.separator, backgroundColor: systemColors.window }, codexProviderToolbarTitle: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }, codexProviderActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, codexProviderActionButton: { width: 30, minWidth: 30, height: 30, paddingHorizontal: 0 }, codexProviderSplit: { borderWidth: 0, borderRadius: 0 }, split: { flexDirection: "row", flexWrap: "wrap", borderWidth: 1, borderColor: systemColors.separator, minHeight: 150, backgroundColor: systemColors.textBackground }, codexListTable: { flex: 1, minWidth: 260, minHeight: 150 }, pluginEditor: { minHeight: 128, flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", gap: 12 }, pluginTable: { flex: 1, minWidth: 260, minHeight: 128 }, pluginFields: { flex: 1, minWidth: 220, gap: 7 }, masterPane: { width: "36%", minWidth: 220, borderRightWidth: 1, borderColor: systemColors.separator, padding: 8 }, detailPane: { flex: 1, minWidth: 240, padding: 12 }, listRow: { minHeight: 28, paddingHorizontal: 8, paddingVertical: 5 }, listRowSelected: { backgroundColor: systemColors.control }, listText: { flex: 1 },
  runtimeWorkspaceFrame: { flex: 1, minHeight: 0, gap: 8 }, runtimeFileToolbar: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 8 }, runtimeWorkspace: { padding: 14, gap: 12 }, runtimeScrollSurface: { flex: 1, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, runtimeTwoColumnForm: { flexDirection: "row", flexWrap: "wrap", columnGap: 20, rowGap: 8 }, runtimeOneColumnForm: { flexDirection: "column", flexWrap: "nowrap" }, runtimeField: { minWidth: 486, flexGrow: 1, flexBasis: 486, gap: 4 }, runtimeInputRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, runtimeFieldLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "right" }, runtimeValueSlot: { width: 180, height: 26, flexShrink: 0, justifyContent: "center" }, runtimeValueControl: { width: 180, minWidth: 180, height: 26 }, runtimeBooleanControl: { width: 24, minWidth: 24, height: 24, alignSelf: "flex-start" }, runtimeUnit: { width: 60, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, runtimeActionSlot: { width: 72, minHeight: 26, flexShrink: 0, justifyContent: "center" }, runtimeHelpSlot: { marginLeft: 134, paddingTop: 4 }, runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  webDavForm: { flex: 1, gap: 14, paddingTop: 0 }, webdavStateRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, paddingBottom: 4, borderBottomWidth: 1, borderBottomColor: systemColors.separator }, webdavEnabledControl: { width: 190, flexGrow: 0, flexShrink: 0 }, webdavInlineStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE }, webdavFormRows: { gap: 8 }, webdavWideControl: { flex: 1, minWidth: 0 }, webdavPasswordInput: { width: "100%", minHeight: 26 }, webdavFooterLeading: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flex: 1, minWidth: 0 }, webdavProbeStatus: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },
  relayAccountsContent: { paddingHorizontal: 12, paddingTop: 10, paddingBottom: 12 }, logsWindow: { flex: 1, minHeight: 0, gap: 4 }, logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, logFilterRow: { width: 360, minWidth: 220, maxWidth: 360, height: 26, flexDirection: "row", alignItems: "center", gap: 8 }, logToolbarSpacer: { flex: 1, minWidth: 0 }, logActionsRow: { height: 26, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, toolbarLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, flexShrink: 0 }, logFilterInput: { flex: 1, minWidth: 0, height: 26 }, logsTabs: { width: "100%", minWidth: 0, height: 28, flexShrink: 0, marginTop: 0, marginBottom: 0 }, logTableFrame: { flex: 1, minHeight: 0, minWidth: 0 }, logTable: { flex: 1, minHeight: 0 }, logEmptySurface: { flex: 1, minHeight: 0, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, logEmptyText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, textAlign: "center", paddingHorizontal: 20 }, logInfoBar: { height: 21, minHeight: 21, flexShrink: 0, borderTopWidth: 1, borderColor: systemColors.separator, justifyContent: "center", paddingHorizontal: 4 },
  form: { gap: 6 }, structuredForm: { gap: 6 }, featureGrid: { flexDirection: "row", flexWrap: "wrap", columnGap: 12, rowGap: 4 }, featureGridItem: { flexGrow: 1, flexBasis: 180, minWidth: 180 }, field: { gap: 5, minWidth: 220, flexGrow: 1, flexBasis: 300 }, fieldLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }, input: { width: "100%", minHeight: 26, color: systemColors.label, fontSize: UI_FONT_SIZE }, textArea: { minHeight: 108, textAlignVertical: "top", fontFamily: "Menlo" }, compactTextArea: { minHeight: 56, maxHeight: 56 }, inputWithAction: { flexDirection: "row", alignItems: "center", gap: 6 }, inputFlex: { flex: 1 }, toggleRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, toggleControl: { flex: 1, minWidth: 0, minHeight: 22, justifyContent: "center" }, toggleNativeControl: { width: "100%", minWidth: 220, minHeight: 22 }, actions: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginTop: 4 }, secretFieldActions: { flexDirection: "row", alignItems: "center", gap: 6 }, secretFieldButtons: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 }, secretFieldButton: { flex: 1, minWidth: 0, height: 26 }, nativeSecretControl: { flex: 1, minWidth: 0, minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, nativeSecretInput: { flex: 1, minWidth: 86, minHeight: 26 }, nativeSecretSetButton: { minWidth: 42, height: 26 }, action: {}, actionPrimary: {}, actionDanger: {}, actionDisabled: {}, actionText: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, actionTextPrimary: {}, actionTextDanger: {}, tabStrip: { flexDirection: "row", flexWrap: "wrap", gap: 6 }, tab: {}, tabSelected: {}, inlineMeta: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 6 }, rawEditor: { flex: 1, minHeight: 180, gap: 4 }, rawEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, rawNativeEditorFrame: { flex: 1, minHeight: 160, position: "relative" }, rawNativeEditor: { flex: 1, minHeight: 160 }, rawEditorOverlay: { position: "absolute", left: 0, right: 0, top: 0, bottom: 0, justifyContent: "center", alignItems: "center", gap: 8, paddingHorizontal: 12, backgroundColor: systemColors.textBackground }, rawEditorLoading: { flex: 1, minHeight: 160, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, infoPair: { gap: 2, minWidth: 160 }, rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 6 }, logRecords: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, maxHeight: 360, overflow: "scroll", padding: 10, gap: 6 }, logRecord: { color: systemColors.label, fontFamily: "Menlo", fontSize: UI_FONT_SIZE }, empty: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, paddingVertical: 12 }, result: { color: systemColors.green, fontSize: UI_FONT_SIZE }, warning: { color: systemColors.brown, fontSize: UI_FONT_SIZE, backgroundColor: systemColors.control, padding: 8, borderRadius: 4 }, issueBox: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.control, padding: 12, gap: 5 }, issue: { color: systemColors.red, fontSize: UI_FONT_SIZE }, cardTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "500" }, cardHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, marginTop: 2 },
  secretActionButton: { width: 64, minWidth: 64, height: 26, flexShrink: 0 },
  formRow: { width: "100%", minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }, formRowStacked: { alignItems: "flex-start" }, formRowSecretStacked: { alignItems: "flex-start" }, formRowLabel: { width: 112, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }, formRowLabelStacked: { paddingTop: 4 }, formRowControl: { flex: 1, minWidth: 0, gap: 3 }, picker: { flex: 1, minWidth: 180, height: 26 },
});
