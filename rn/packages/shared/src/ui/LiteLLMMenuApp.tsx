import React, { createContext, useCallback, useEffect, useMemo, useRef, useState, useContext } from "react";
import { Platform, PlatformColor, ScrollView, StyleSheet, Text, View, type HostInstance, type StyleProp, type TextStyle, type ViewStyle } from "react-native";
import { createTranslator } from "../i18n";
import { assistantSettingOptions, codexFeatureLabel, localizeCodexValidationMessage, type AssistantSettingOption } from "../i18n/assistantSettingsI18n";
import { runtimeCategoryLabel, runtimeFieldHelp, runtimeFieldLabel, runtimeOptionLabel, runtimeUnitLabel } from "../i18n/runtimeSettingsI18n";
import { LOG_TABS, ROUTES } from "../routes";
import { NativeButton, NativeCheckbox, NativePicker, NativeSecureTextEditor, NativeSecureTextInput, NativeSegmentedControl, NativeSplitView, NativeTable, NativeTextEditor, NativeTextField } from "./NativeControls";
import { RelayAccountManager } from "./RelayAccountManager";
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
  ProviderSummary,
  ServiceStatus,
  ValidationSummary,
} from "../types";

type Translate = (key: string, values?: Record<string, string | number>) => string;
type UnknownRecord = Record<string, unknown>;
type Dispatch = (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<void>;
type ApplyProbedOrder = (providerId: string, modelId: string, nextOrder: string[]) => Promise<boolean>;
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

const CLAUDE_BUILTIN_THEMES = ["auto", "dark", "light", "dark-daltonized", "light-daltonized", "dark-ansi", "light-ansi"];
const CLAUDE_CUSTOM_THEME = /^custom:[A-Za-z0-9][A-Za-z0-9._-]*$/;

const PendingFieldContext = createContext<PendingFieldRegistry | undefined>(undefined);
const TranslationContext = createContext<Translate | undefined>(undefined);
// React Native macOS supports `tooltip` on Text, but its published TypeScript
// declaration has not caught up with that native prop. Keep the cast narrow so
// the full probe result is a real native hover tooltip, not an accessibility-
// only hint.
const TooltipText = Text as unknown as React.ComponentType<React.ComponentProps<typeof Text> & { tooltip?: string }>;
const SERVICE_HEALTH_POLL_MS = 10_000;
const SERVICE_RECOVERY_RETRY_MS = 15_000;
const SETTINGS_DISK_POLL_MS = 2_000;

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

function claudeThemeValues(value: unknown): string[] {
  const theme = stringValue(value, "auto");
  return CLAUDE_CUSTOM_THEME.test(theme) ? [theme, ...CLAUDE_BUILTIN_THEMES] : CLAUDE_BUILTIN_THEMES;
}

function vimInsertRemapLines(value: unknown): string {
  return Object.keys(asRecord(value)).filter((sequence) => sequence.length === 2).join("\n");
}

function vimInsertRemaps(value: string): UnknownRecord {
  return Object.fromEntries(splitLines(value).map((sequence) => [sequence, "<Esc>"]));
}

function claudeVoiceModeLabel(value: string, translate: Translate): string {
  return value === "hold" ? translate("claude.voiceHold") : translate("claude.voiceTap");
}

function claudeVoiceMode(label: string, translate: Translate): "hold" | "tap" {
  return label === translate("claude.voiceHold") ? "hold" : "tap";
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

export function LiteLLMMenuApp({ ipc, native, translate: hostTranslate, routeRequest, routeRequestSequence, logTabRequest, nativeAction, isPrimaryHost = true, isWindowManagerHost = false }: LiteLLMMenuAppProps): React.JSX.Element {
  const [route, setRoute] = useState<AppRoute>(routeRequest ?? "home");
  const [snapshot, setSnapshot] = useState<CoreSnapshot | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [serviceOperationPendingCount, setServiceOperationPendingCount] = useState(0);
  const serviceOperationPending = serviceOperationPendingCount > 0;
  const snapshotLanguage = snapshot?.language;
  const translate = useMemo<Translate>(() => !snapshotLanguage || snapshotLanguage === "system" ? hostTranslate : createTranslator(snapshotLanguage), [hostTranslate, snapshotLanguage]);
  const handledNativeActions = useRef(new Set<string>());
  // The desktop host owns a service while it is open, except after an
  // explicit Stop.  Keep this intent separate from the current controller
  // state so a stopped service is not immediately resurrected by the poller.
  const serviceShouldBeRunning = useRef(true);
  const startupAttempted = useRef(false);
  const serviceBackgroundOperationScheduled = useRef(false);
  const serviceOperationQueue = useRef<Promise<void>>(Promise.resolve());
  const lastServiceRecoveryAttempt = useRef(0);
  const acceptedSnapshotRevision = useRef<number>(-1);

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

  const runServiceOperation = useCallback((operation: ServiceOperation, background = false): Promise<CoreSnapshot | undefined> => {
    // Polling must never pile up behind a long lifecycle transition. Explicit
    // menu actions still serialize after an in-flight poll so a Stop is not
    // silently dropped if the timer fires at the same moment.
    if (background && serviceBackgroundOperationScheduled.current) return Promise.resolve(undefined);
    if (background) serviceBackgroundOperationScheduled.current = true;
    if (operation === "stop") serviceShouldBeRunning.current = false;
    if (operation === "start" || operation === "restart") {
      serviceShouldBeRunning.current = true;
      lastServiceRecoveryAttempt.current = Date.now();
    }
    if (!background) setServiceOperationPendingCount((count) => count + 1);
    const queued = serviceOperationQueue.current.catch(() => undefined).then(async () => {
      try {
        await ipc.dispatch({ type: `service.${operation}` });
        return await refreshSnapshot(!background);
      } catch {
        // A lifecycle operation can fail while Core itself is still healthy
        // (for example, a child process cannot bind its configured port).
        // Preserve the settings/menu surface and refresh its actual state;
        // only surface the global Core error if that refresh also fails.
        try {
          return await refreshSnapshot(!background);
        } catch {
          setError(hostTranslate("error.coreUnavailable"));
          return undefined;
        }
      } finally {
        if (background) serviceBackgroundOperationScheduled.current = false;
      }
    });
    serviceOperationQueue.current = queued.then(() => undefined, () => undefined);
    if (!background) {
      void queued.finally(() => setServiceOperationPendingCount((count) => Math.max(0, count - 1)));
    }
    return queued;
  }, [hostTranslate, ipc, refreshSnapshot]);

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
      routeWebdavSettings: translate("card.webdavSettings"), routeRelayAccounts: translate("route.relayAccounts"), routeLogs: translate("card.logs"),
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
    // instead of retaining a bootstrap title until the next health poll.
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
    void refreshSnapshot().catch(() => {
      if (mounted) setError(hostTranslate("error.coreUnavailable"));
    });
    const unsubscribe = ipc.subscribe((event) => receive(event.snapshot));
    if (isPrimaryHost) native.setShortcuts({ openMenu: "Cmd+, / Ctrl+,", closeWindow: "Esc", reload: "Cmd+R / Ctrl+R" });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [hostTranslate, ipc, isPrimaryHost, native, receiveSnapshot, refreshSnapshot]);

  useEffect(() => {
    if (!isPrimaryHost || !snapshot || startupAttempted.current || !serviceShouldBeRunning.current) return;
    startupAttempted.current = true;
    if (snapshot.service.state === "stopped") void runServiceOperation("start");
  }, [isPrimaryHost, runServiceOperation, snapshot?.service.state]);

  useEffect(() => {
    if (!isPrimaryHost) return;
    let active = true;
    const pollServiceHealth = async (): Promise<void> => {
      if (!serviceShouldBeRunning.current) return;
      const current = await runServiceOperation("health", true);
      if (!active || !current || !serviceShouldBeRunning.current) return;
      // Starting an owned-but-unhealthy process is rejected by Core and can
      // never repair it. A stopped state includes a verified orphan, which
      // Core reclaims before starting a new App/Core/proxy unit.
      if (current.service.state !== "stopped") return;
      const now = Date.now();
      if (now - lastServiceRecoveryAttempt.current < SERVICE_RECOVERY_RETRY_MS) return;
      lastServiceRecoveryAttempt.current = now;
      void runServiceOperation("start");
    };
    const timer = setInterval(() => { void pollServiceHealth(); }, SERVICE_HEALTH_POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [isPrimaryHost, runServiceOperation]);

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
    if (!isPrimaryHost || !snapshot) return;
    const serviceState = snapshot.service.state;
    const serviceActive = serviceState === "running" || serviceState === "starting" || serviceState === "unhealthy";
    const serviceStartAvailable = !serviceOperationPending && serviceState === "stopped";
    const serviceRestartAvailable = !serviceOperationPending && serviceState !== "unknown" && serviceState !== "starting";
    const serviceReloadAvailable = !serviceOperationPending && (serviceState === "running" || serviceState === "unhealthy");
    const actions = [
      { id: "toggle-autostart", title: translate("menu.autoStart"), enabled: true, checked: snapshot.service.auto_start_state === "enabled" },
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
      {!error && route !== "home" ? <RouteSurface route={route} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={receiveSnapshot} onClose={() => setRoute("home")} /> : null}
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

function IconButton({ label, title, disabled, onPress }: { label: string; title: string; disabled?: boolean; onPress: () => void }): React.JSX.Element {
  return <NativeButton title={label} toolTip={title} accessibilityLabel={title} compact disabled={disabled} onPress={onPress} style={styles.iconButton} />;
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
  const probedOrderApplyQueue = useRef<Promise<void>>(Promise.resolve());
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
    onSnapshot(next);
    return next;
  };
  const onSecretState = (state: SecretState): void => {
    if (state.status !== "saved" || state.revision < 0) return;
    revision.current = state.revision;
    void refresh().catch(() => undefined);
  };
  const clearSecret: NativeSecretClear = (options) => run(async () => {
    const staged = await native.clearSecret(options);
    if (staged) revision.current = staged.revision;
    return staged ?? { cancelled: true };
  }, "common.staged");
  const run = async (operation: () => Promise<unknown>, message = "common.applied", keepControlsEnabled = false): Promise<void> => {
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
        setResult(translate(message));
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
  const dispatch: Dispatch = (type, payload = {}, targetDomain = domain) => run(() => enqueueDispatch(type, payload, targetDomain), "common.staged", true);
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
    if (next === settingsTab || busy) return;
    void run(async () => {
      await flushPendingFields();
      setSettingsTab(next);
      return {};
    }, "common.staged");
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
  const applyProbedOrder: ApplyProbedOrder = (providerId, modelId, nextOrder) => {
    let applied = false;
    const queued = probedOrderApplyQueue.current.catch(() => undefined).then(async () => {
      const before = await ipc.snapshot();
      revision.current = before.revision;
      onSnapshot(before);
      const currentModel = providerModelByEditorId(before, providerId, modelId);
      if (!currentModel) throw new Error("The selected model is unavailable");
      const currentOrder = protocolOrder(currentModel);
      const selectedOrder = [nextOrder[0]];
      if (sameStringOrder(currentOrder, selectedOrder)) return;
      const diskChanged = before.disk.providers_models?.changed === true;
      const currentLabels = currentOrder.map((surface) => probeSurfaceLabel(surface, translate)).join(" → ") || translate("common.none");
      const nextLabels = selectedOrder.map((surface) => probeSurfaceLabel(surface, translate)).join(" → ") || translate("common.none");
      const confirmationMessage = [
        translate("providers.probeApplyMessage", { current: currentLabels, next: nextLabels }),
        diskChanged ? translate("settings.overwriteDiskConfirm") : "",
      ].filter(Boolean).join("\n\n");
      const confirmed = await native.showConfirmation({
        title: translate("providers.probeApplyTitle"),
        message: confirmationMessage,
        confirmLabel: translate("screen.confirm"),
      });
      if (!confirmed) return;
      await enqueueDispatch("model.patch", {
        provider_id: providerId,
        model_id: modelId,
        changes: {
          upstream_url_surface: selectedOrder[0],
          supported_upstream_url_surfaces: selectedOrder,
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
    probedOrderApplyQueue.current = queued.then(() => undefined, () => undefined);
    return queued.then(() => applied).catch((reason: unknown) => {
      setResult(errorMessage(reason, translate));
      return false;
    });
  };
  const closeRoute = (): void => {
    native.window.close(nativeWindowRoute(route));
    onClose();
  };
  const requestClose = (): void => {
    if (busy) return;
    void run(async () => {
      await flushPendingFields();
      const current = await ipc.snapshot();
      revision.current = current.revision;
      onSnapshot(current);
      const dirtyDomains = settingsRoute
        ? (["codex", "claude"] as const).filter((name) => current.drafts[name]?.dirty)
        : domain && current.drafts[domain]?.dirty ? [domain] : [];
      if (dirtyDomains.length === 0 && (!settingsRoute || !hasClaudeDeploymentChanges(current))) {
        closeRoute();
        return { cancelled: true };
      }
      const confirmed = await native.showConfirmation({
        title: translate("menu.close"),
        message: translate("common.discarded"),
        confirmLabel: translate("menu.close"),
      });
      if (!confirmed) return { cancelled: true };
      discardPendingFields();
      for (const name of dirtyDomains) await enqueueDispatch("cancel", {}, name);
      if (settingsRoute) {
        claudeDeploymentDraftRef.current = undefined;
        setClaudeDeploymentDraft(undefined);
      }
      closeRoute();
      return {};
    }, "common.discarded");
  };
  useEffect(() => {
    if (nativeAction?.id !== `request-close-${route}` && nativeAction?.id !== `request-close-${nativeWindowRoute(route)}`) return;
    requestClose();
  }, [nativeAction?.sequence]);
  const definition = ROUTES.find((item) => item.id === route);
  const windowTitle = settingsRoute ? translate("menu.codex") : translate(definition?.titleKey ?? "app.title");
  return <TranslationContext.Provider value={settingsRoute ? translate : undefined}><PendingFieldContext.Provider value={fieldRegistry}><View style={styles.windowSurface}>
    {route !== "providers-models" && route !== "logs" && route !== "relay-accounts" ? <WindowTitle title={windowTitle} validation={issues.length > 0 ? `${issues.length} ${translate("common.validationIssues")}` : undefined} /> : null}
    {route === "providers-models" || settingsRoute || route === "logs" || route === "relay-accounts" || route === "runtime-settings" || route === "webdav-settings" ? <View style={[styles.windowContent, styles.windowContentFixed, route === "providers-models" && styles.providersContent, settingsRoute && styles.settingsContent, route === "logs" && styles.logsContent, route === "relay-accounts" && styles.relayAccountsContent, route === "runtime-settings" && styles.runtimeContent, route === "webdav-settings" && styles.webDavContent]}>
    {route === "providers-models" ? <ProviderWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} applyProbedOrder={applyProbedOrder} /> : null}
    {settingsRoute ? <><View style={styles.settingsTabBar}><WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]} selected={settingsTab} disabled={busy} onSelect={(next) => switchSettingsTab(next as AssistantSettingsDomain)} style={styles.settingsTabs} /></View>{settingsTab === "codex" ? <CodexWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} rawReloadToken={settingsRawReloadToken} /> : <ClaudeScreen snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} deployment={claudeDeployment} onDeploymentChange={(key, value) => {
      const next = { ...(claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(snapshot)), [key]: value };
      claudeDeploymentDraftRef.current = next;
      setClaudeDeploymentDraft(next);
      return enqueueDispatch("patch_deployment", { [key]: value }, "claude").then(() => undefined);
    }} rawReloadToken={settingsRawReloadToken} />}</> : null}
    {route === "logs" ? <LogsWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} requestedTab={nativeAction?.id === "open-recovery" ? "recovery" : logTabRequest} /> : null}
    {route === "relay-accounts" ? <RelayAccountManager visible snapshot={snapshot} native={native} busy={busy} translate={translate} onClose={closeRoute} dispatch={dispatch} commit={commitRelayMetadata} detectType={async (origin) => {
      const staged = await enqueueDispatch("account.detect_type", { origin }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const detected = asRecord(next.action_summaries?.relay_accounts).detected_type;
      return detected === "newapi" || detected === "sub2api" ? detected : undefined;
    }} refreshResources={async (accountId) => {
      const staged = await enqueueDispatch("resources.refresh", { account_id: accountId }, "relay_accounts");
      revision.current = staged.revision;
    }} importResources={async (accountId, resourceIds) => {
      await commitRelayMetadata("resources.import", { account_id: accountId, resource_ids: resourceIds }, "relay_accounts");
    }} addAccount={async (type, origin, rememberPassword) => {
      const staged = await enqueueDispatch("account.add", { type, label: origin, origin, remember_password: rememberPassword }, "relay_accounts");
      revision.current = staged.revision;
      const next = await refresh();
      const accounts = asRecords(asRecord(next.domains.relay_accounts).accounts);
      const account = accounts.find((item) => item.origin === origin && item.type === type);
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
    {route !== "logs" && route !== "relay-accounts" ? <DialogFooter status={result} leading={route === "runtime-settings" ? <ActionButton title={translate("common.restoreDefaults")} disabled={busy} onPress={() => dispatch("restore_defaults")} /> : route === "webdav-settings" ? <View style={styles.webdavFooterLeading}><ActionButton title={translate("common.test")} disabled={busy} style={styles.wideButton} onPress={() => run(async () => { await flushPendingFields(); return ipc.probe(undefined, undefined, "webdav"); }, "webdav.probe")} /><Text numberOfLines={2} style={styles.webdavProbeStatus}>{snapshot ? webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate) : ""}</Text></View> : undefined}><><ActionButton title={translate("menu.close")} disabled={busy} style={route === "runtime-settings" || route === "webdav-settings" ? styles.wideButton : undefined} onPress={requestClose} /><ActionButton primary title={route === "runtime-settings" ? translate("common.saveAndApply") : translate("menu.apply")} disabled={busy || (settingsRoute ? !(snapshot?.drafts.codex?.dirty || snapshot?.drafts.claude?.dirty || hasClaudeDeploymentChanges(snapshot) || hasPendingFieldEdits()) : domain ? !(snapshot?.drafts[domain]?.dirty || hasPendingFieldEdits()) : false)} style={route === "runtime-settings" || route === "webdav-settings" ? styles.wideButton : undefined} onPress={apply} /></></DialogFooter> : null}
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

function ProviderWorkspace({ snapshot, ipc, onSnapshot, native, busy, translate, dispatch, onSecretState, applyProbedOrder }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; applyProbedOrder: ApplyProbedOrder }): React.JSX.Element {
  const state = domainState(snapshot, "providers_models");
  const details = asRecords(state.providers);
  const fallback = snapshot?.providers_models.providers ?? [];
  const providers = details.length > 0 ? details : fallback.map(providerRecord);
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const pendingProviderIds = useRef<Set<string> | undefined>(undefined);
  const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);
  const knownModelIdsByProvider = useRef<Map<string, Set<string>> | undefined>(undefined);
  const provider = providers.find((item) => editorIdentifier(item) === selectedProvider) ?? providers[0];
  const providerId = provider ? editorIdentifier(provider) : "";
  const models = provider ? asRecords(provider.models).map(modelRecord) : [];
  const [selectedModel, setSelectedModel] = useState<string>();
  const [providerSourceModel, setProviderSourceModel] = useState<string>();
  const model = models.find((item) => editorIdentifier(item) === selectedModel);
  const [viewMode, setViewMode] = useState<"providers" | "routes">("providers");
  const [selectedRoute, setSelectedRoute] = useState<string>();
  const [fetchKeyName, setFetchKeyName] = useState<string>();
  const [fetchedModelsOpen, setFetchedModelsOpen] = useState(false);
  const probingModelKeys = useRef(new Set<string>());
  const [, setProbeActivityRevision] = useState(0);
  const [probeResults, setProbeResults] = useState<Record<string, IpcResults["probe"]>>({});
  const multiplierRefreshStarted = useRef(false);
  const transferButtonRef = useRef<HostInstance | null>(null);
  const operation = asRecord(snapshot?.action_summaries?.providers_models);
  const operationSummary = asRecord(operation.operation_summary);
  const apiKeyNames = stringList(provider?.api_key_names);
  const fetchKeyOptions = apiKeyNames.map((name) => ({ value: name, label: apiKeyDisplayName(name, translate) }));
  const selectedFetchKey = fetchKeyName ?? apiKeyNames[0] ?? "";
  const selectedFetchLabel = fetchKeyOptions.find((option) => option.value === selectedFetchKey)?.label ?? translate("common.default");
  async function probeModel(targetProviderId: string, targetModelId: string): Promise<void> {
    const key = modelProbeKey(targetProviderId, targetModelId);
    if (probingModelKeys.current.has(key)) return;
    probingModelKeys.current.add(key);
    setProbeActivityRevision((value) => value + 1);
    try {
      const result = await ipc.probe(targetProviderId, targetModelId, "providers_models");
      setProbeResults((current) => ({ ...current, [key]: result }));
      onSnapshot(await ipc.snapshot());
      const nextOrder = stringList(result.recommended_order).filter(isProbeSurface);
      if (result.ok && nextOrder.length > 0) await applyProbedOrder(targetProviderId, targetModelId, nextOrder);
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
    if (!snapshot || multiplierRefreshStarted.current) return;
    multiplierRefreshStarted.current = true;
    let active = true;
    void (async () => {
      const current = await ipc.snapshot();
      const staged = await ipc.dispatch(
        { domain: "providers_models", type: "providers.refresh_multiplier" },
        current.revision,
      );
      const next = await ipc.snapshot();
      if (active && next.revision >= staged.revision) onSnapshot(next);
    })().catch(() => undefined);
    return () => { active = false; };
  }, [ipc, onSnapshot, snapshot]);
  const modelIdentitySignature = providers.map((entry) => `${editorIdentifier(entry)}:${asRecords(entry.models).map(editorIdentifier).join(",")}`).join("|");
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
    if (wasInitialized) void Promise.all(added.map(({ providerId: targetProviderId, modelId }) => probeModel(targetProviderId, modelId)));
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
  useEffect(() => {
    if (!fetchedModelsOpen || busy) return;
    if (stringValue(operationSummary.operation) !== "fetch_models" || stringValue(operationSummary.provider_id) !== providerId) return;
    const candidates = stringList(operationSummary.models);
    setFetchedModelsOpen(false);
    if (candidates.length === 0) return;
    const candidateSet = new Set(candidates);
    const providerName = stringValue(provider?.display_name, stringValue(provider?.name, providerId));
    const keyName = apiKeyDisplayName(fetchKeyName ?? "default", translate);
    void native.chooseModelsToAdd({ models: candidates, providerName, keyName }).then((selection) => {
      const selectedModels = (selection ?? []).filter((model, index, all) => candidateSet.has(model) && all.indexOf(model) === index);
      if (selectedModels.length === 0) return;
      void Promise.all(selectedModels.map((upstreamModel, index) => dispatch("model.add", { provider_id: providerId, model: { name: upstreamModel, upstream_model: upstreamModel, api_key_name: fetchKeyName, enabled: true, order: models.length + index + 1, upstream_url_surface: "openai/responses", supported_upstream_url_surfaces: ["openai/responses"] } })))
        .catch(() => undefined);
    }).catch(() => undefined);
  }, [busy, fetchedModelsOpen, fetchKeyName, native, operationSummary, provider, providerId, models.length, dispatch, translate]);
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
    void dispatch("model.add", { provider_id: providerId, model: { name: "", upstream_model: "", enabled: true, order: models.length + 1, upstream_url_surface: "openai/responses", supported_upstream_url_surfaces: ["openai/responses"] } });
  };
  const duplicateModel = (): void => {
    if (!model) return;
    void dispatch("model.duplicate", { provider_id: providerId, model_id: editorIdentifier(model) });
  };
  const routes = providers.flatMap((entry, providerIndex) => asRecords(entry.models).map(modelRecord).flatMap((entryModel, modelIndex) => {
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
  });
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
  const providerRows = providers.map((item) => ({ key: editorIdentifier(item), cells: [stringValue(item.display_name, stringValue(item.name, translate("providers.newProvider"))), String(asRecords(item.models).length || numberValue(item.model_count))] }));
  const disabledProviderKeys = providers.filter((item) => !booleanValue(item.enabled, true)).map(editorIdentifier);
  const modelRows = models.map((item) => ({ key: editorIdentifier(item), cells: [stringValue(item.display_name, stringValue(item.name, translate("providers.newModel"))), upstreamModelLabel(item), translate("providers.billingUnavailable"), `${apiKeyDisplayName(item.api_key_name, translate)} / ${numberValue(item.order, 1)}`] }));
  const disabledModelKeys = models.filter((item) => !booleanValue(provider?.enabled, true) || !booleanValue(item.model_enabled, booleanValue(item.enabled, true))).map(editorIdentifier);
  const routeRows = routes.map((entry, index) => {
    const startsGroup = index === 0 || routes[index - 1]?.publicModel !== entry.publicModel;
    const numericOrder = numberValue(entry.model.order, Number.NaN);
    const order = Number.isFinite(numericOrder) ? String(numericOrder) : stringValue(entry.model.order).trim();
    return { key: entry.key, cells: [startsGroup ? entry.publicModel : "", order || "-", `${stringValue(entry.provider.name)} / ${apiKeyDisplayName(entry.model.api_key_name, translate)}`, upstreamModelLabel(entry.model) || translate("common.notAvailable")] };
  });
  const disabledRouteKeys = routes.filter((entry) => !entry.providerEnabled || !entry.modelEnabled || !entry.keyAvailable).map((entry) => entry.key);
  const selectRoute = (routeId: string): void => {
    const selected = routes.find((entry) => entry.key === routeId);
    setSelectedRoute(routeId);
    if (selected) {
      setSelectedProvider(editorIdentifier(selected.provider));
      setSelectedModel(editorIdentifier(selected.model));
      setProviderSourceModel(undefined);
    }
  };
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
    <View style={styles.providerToolbar}>
      <WindowTabs values={[{ id: "providers", title: translate("providers.providers") }, { id: "routes", title: translate("providers.routes") }]} selected={viewMode} onSelect={(value) => chooseViewMode(value as "providers" | "routes")} />
      <ActionButton ref={transferButtonRef} title={`${translate("providers.importExport")} ▾`} disabled={busy} style={styles.importSourcePicker} onPress={showTransferMenu} />
      <View style={styles.toolbarSpacer} />
    </View>
    {viewMode === "routes" ? <View style={[styles.routeWorkspace, styles.routeWorkspaceWithInspector]}>
      <TablePane wide style={styles.routeTablePane} title={translate("providers.routes")} actions={<><IconButton label="↑" title={translate("common.moveUp")} disabled={busy || !canMoveRouteUp} onPress={() => moveRoute("up")} /><IconButton label="↓" title={translate("common.moveDown")} disabled={busy || !canMoveRouteDown} onPress={() => moveRoute("down")} /></>}>
        <NativeTable columns={[{ label: translate("providers.model"), width: 170 }, { label: translate("common.order"), width: 56 }, { label: `${translate("providers.provider")} / ${translate("providers.key")}`, width: 130 }, { label: translate("providers.upstream"), width: 164 }]} rows={routeRows} disabledRowKeys={disabledRouteKeys} selectedKey={selectedRoute ?? ""} alternatingRows onSelectionChange={selectRoute} style={styles.nativeRouteTable} />
      </TablePane>
      <View style={styles.providerInspector}>
        {activeRoute ? (providerSourceModel ? <ProviderEditor provider={activeRoute.provider} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} sourceModel={activeRoute.model} onReturnToModel={() => { setProviderSourceModel(undefined); setSelectedModel(editorIdentifier(activeRoute.model)); }} /> : <ModelInspector providers={providers} provider={activeRoute.provider} providerId={editorIdentifier(activeRoute.provider)} model={activeRoute.model} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} {...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model))} onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: editorIdentifier(activeRoute.provider), model_id: editorIdentifier(activeRoute.model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(activeRoute.model)); setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`); setProviderSourceModel(undefined); })} />) : <EmptyState translate={translate} />}
      </View>
    </View> : <View style={styles.providerWorkspace}>
      <View style={styles.providerLeftColumn}>
        <View style={styles.providerModelColumns}>
          <TablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !provider} onPress={confirmDeleteProvider} /></>}>
            <NativeTable columns={[{ label: translate("providers.provider"), width: 140 }, { label: translate("providers.models"), width: 48 }]} rows={providerRows} disabledRowKeys={disabledProviderKeys} selectedKey={providerId} onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }} style={styles.nativeProviderTable} />
          </TablePane>
          <TablePane style={styles.modelListPane} title={translate("providers.models")} actions={<><IconButton label="+" title={translate("providers.newModel")} disabled={busy || !provider} onPress={addModel} /><IconButton label="⧉" title={translate("common.copy")} disabled={busy || !model} onPress={duplicateModel} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !model} onPress={confirmDeleteModel} /></>}>
            <NativeTable columns={[{ label: translate("providers.model"), width: 118 }, { label: translate("providers.upstream"), width: 130 }, { label: translate("providers.balance"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 104 }]} rows={modelRows} disabledRowKeys={disabledModelKeys} selectedKey={selectedModel ?? ""} onSelectionChange={(key) => { setSelectedModel(key); setProviderSourceModel(undefined); }} style={styles.nativeModelTable} />
            <View style={styles.tableBottomRow}><NativePicker labels={fetchKeyOptions.length > 0 ? fetchKeyOptions.map((option) => option.label) : [translate("common.default")]} selectedValue={selectedFetchLabel} disabled={busy || !provider || apiKeyNames.length === 0} onChange={({ nativeEvent }) => { const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyName(option.value); }} style={styles.fetchKeyPicker} /><ActionButton title={translate("providers.fetch")} disabled={busy || !provider || !fetchKeyName} onPress={() => { void dispatch("providers.fetch_models", { provider_id: providerId, api_key_name: fetchKeyName }).then(() => setFetchedModelsOpen(true)); }} /></View>
          </TablePane>
        </View>
      </View>
      <View style={styles.providerInspector}>{provider && model ? <ModelInspector providers={providers} provider={provider} providerId={providerId} model={model} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={() => probeModel(providerId, editorIdentifier(model))} {...modelProbeProps(providerId, editorIdentifier(model))} onProviderClick={() => { setProviderSourceModel(editorIdentifier(model)); setSelectedModel(undefined); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: providerId, model_id: editorIdentifier(model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(editorIdentifier(model)); setProviderSourceModel(undefined); })} /> : provider ? <ProviderEditor provider={provider} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} sourceModel={models.find((item) => editorIdentifier(item) === providerSourceModel)} onReturnToModel={() => { if (providerSourceModel) setSelectedModel(providerSourceModel); setProviderSourceModel(undefined); }} /> : <EmptyState translate={translate} />}</View>
    </View>}
  </View>;
}

function TablePane({ title, actions, wide, style, children }: { title: string; actions: React.ReactNode; wide?: boolean; style?: StyleProp<ViewStyle>; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.tablePane, wide && styles.tablePaneWide, style]}><View style={styles.tableTitleRow}><Text style={styles.tableTitle}>{title}</Text><View style={styles.tableActions}>{actions}</View></View>{children}</View>;
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
  const billingSummary = `${translate("providers.balance")}: ${translate("providers.billingUnavailable")}  ${translate("providers.multiplier")}: ${billingMultiplierValue(model.multiplier, translate)}`;
  return <View style={styles.inspectorContent}><View style={styles.modelBreadcrumb}><NativeButton title={providerLabel} link disabled={busy} onPress={onProviderClick} style={styles.breadcrumbProvider} /><Text style={styles.breadcrumbSeparator}>&gt;</Text><Text numberOfLines={1} style={styles.inspectorHeading}>{stringValue(model.name, translate("providers.newModel"))}</Text></View><View style={styles.inspectorDivider} /><View style={styles.inspectorBody}><View style={styles.inspectorEnabledRow}><NativeCheckbox label={translate("common.enable")} value={booleanValue(model.model_enabled, booleanValue(model.enabled, true))} disabled={busy} onValueChange={(model_enabled) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { model_enabled } })} style={styles.inspectorEnableControl} /><ActionButton title={probing ? translate("providers.probing") : translate("providers.probe")} onPress={probe} /><TooltipText numberOfLines={2} tooltip={probePresentation.full} accessibilityHint={probePresentation.full} style={styles.probeSummary}>{probePresentation.compact}</TooltipText></View><Text numberOfLines={1} style={styles.billingSummaryText}>{billingSummary}</Text><TextField label={translate("providers.publicModel")} labelWidth={96} labelAlign="left" value={stringValue(model.name)} onCommit={(name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { name } })} /><PickerField label={translate("providers.provider")} labelWidth={96} labelAlign="left" value={providerLabel} values={providerLabels} disabled={busy || providers.length <= 1} onSelect={(label) => { const next = providers.find((item) => stringValue(item.name, translate("providers.newProvider")) === label); if (next) onProviderChange(editorIdentifier(next)); }} /><PickerField label={translate("common.apiKey")} labelWidth={96} labelAlign="left" value={selectedKey} values={keyOptions.length > 0 ? keyOptions : [{ value: "", label: translate("common.notAvailable") }]} disabled={busy || keyNames.length === 0} onSelect={(api_key_name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { api_key_name } })} /><TextField label={translate("providers.upstream")} labelWidth={96} labelAlign="left" value={upstreamModelLabel(model)} onCommit={(upstream_model) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_model } })} /><TextField label={translate("common.order")} labelWidth={96} labelAlign="left" controlWidth={64} value={String(numberValue(model.order, 1))} keyboardType="numeric" onCommit={(order) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { order: Number(order) || 1 } })} /><ProtocolOrderEditor providerId={providerId} model={model} busy={busy} translate={translate} dispatch={dispatch} /></View></View>;
}

function ProtocolOrderEditor({ providerId, model, busy, translate, dispatch }: { providerId: string; model: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch }): React.JSX.Element {
  const id = editorIdentifier(model);
  const supported = stringList(model.supported_upstream_url_surfaces);
  const current = supported.length > 0 ? supported : [stringValue(model.upstream_url_surface, "openai/responses")];
  const all = [...current, ...["openai/responses", "openai/chat", "anthropic"].filter((item) => !current.includes(item))];
  const patch = (next: string[]): void => { if (next.length > 0) void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_url_surface: next[0], supported_upstream_url_surfaces: next } }); };
  const toggle = (surface: string, enabled: boolean): void => patch(enabled ? [...current, surface].filter((item, index, list) => list.indexOf(item) === index) : current.filter((item) => item !== surface));
  const move = (surface: string, delta: number): void => { const source = current.indexOf(surface); const target = source + delta; if (source < 0 || target < 0 || target >= current.length) return; const next = [...current]; [next[source], next[target]] = [next[target], next[source]]; patch(next); };
  const title = (surface: string): string => surface === "openai/chat" ? translate("providers.chat") : surface === "anthropic" ? translate("providers.anthropic") : translate("providers.responses");
  return <View style={styles.protocolField}><Text style={styles.protocolFieldLabel}>{translate("providers.apiOrder")}</Text><View style={styles.protocolRows}>{all.map((surface) => { const rank = current.indexOf(surface); return <View key={surface} style={styles.protocolRow}><Text style={styles.protocolRank}>{rank >= 0 ? rank + 1 : ""}</Text><NativeCheckbox label={title(surface)} value={rank >= 0} disabled={busy || (rank >= 0 && current.length === 1)} onValueChange={(enabled) => toggle(surface, enabled)} style={styles.protocolCheckbox} /><IconButton label="↑" title={translate("common.moveUp")} disabled={busy || rank <= 0} onPress={() => move(surface, -1)} /><IconButton label="↓" title={translate("common.moveDown")} disabled={busy || rank < 0 || rank >= current.length - 1} onPress={() => move(surface, 1)} /></View>; })}</View></View>;
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

function sameStringOrder(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function protocolOrder(model: UnknownRecord): string[] {
  const supported = stringList(model.supported_upstream_url_surfaces).filter(isProbeSurface);
  if (supported.length > 0) return supported;
  const primary = stringValue(model.upstream_url_surface, "openai/responses");
  return isProbeSurface(primary) ? [primary] : ["openai/responses"];
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
    <TextField label={translate("providers.baseUrl")} labelWidth={96} labelAlign="left" value={stringValue(provider.endpoint, stringValue(provider.api_base))} onCommit={(endpoint) => dispatch("provider.patch", { provider_id: id, changes: { endpoint } })} />
    <TextField label={translate("providers.providerName")} labelWidth={96} labelAlign="left" value={stringValue(provider.name, stringValue(provider.display_name))} onCommit={(name) => dispatch("provider.patch", { provider_id: id, changes: { name } })} />
    <View style={styles.providerKeysEditor}>
      <View style={styles.providerKeysHeader}>
        <Text style={styles.providerKeysHeading}>{translate("providers.apiKeys")}</Text>
        <View style={styles.providerKeyActions}>
          <IconButton label="+" title={translate("common.add")} disabled={busy} onPress={addKey} />
          <IconButton label="−" title={translate("common.delete")} disabled={busy || keys.length <= 1 || !selectedKey} onPress={deleteKey} />
        </View>
      </View>
      <View style={styles.providerKeyGrid}>
        <NativeTable columns={[{ label: translate("providers.key"), width: 138 }]} rows={keyRows} selectedKey={selectedKey} onSelectionChange={setSelectedKey} style={styles.providerKeyTable} />
        <View style={styles.providerKeyFields}>
          {selectedKey ? <>
            <TextField label={translate("providers.keyName")} labelWidth={64} labelAlign="left" value={selectedKey} onCommit={renameKey} />
            <NativeSecretField plainText autoCommit label={translate("common.apiKey")} hint={selectedKeyConfigured ? translate("providers.apiKeySavedHint") : translate("providers.apiKeyInput")} labelWidth={64} labelAlign="left" busy={busy || !selectedKey} domain="providers_models" field="api_key" target={`${id}\x1f${selectedKey}`} onSecretState={onSecretState} />
          </> : <Text style={styles.empty}>{translate("common.notAvailable")}</Text>}
        </View>
      </View>
    </View>
    </View>
  </View>;
}

function CodexWorkspace({ snapshot, ipc, native, busy, translate, dispatch, onSecretState, clearSecret, rawReloadToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; rawReloadToken: number }): React.JSX.Element {
  const state = domainState(snapshot, "codex");
  const structured = asRecord(state.structured);
  const permissions = asRecord(structured.permissions);
  const advanced = asRecord(structured.advanced);
  const providers = asRecords(structured.providers);
  const mcpServers = asRecords(structured.mcp_servers).map(editableRecord);
  const plugins = asRecords(structured.plugins);
  const deployments = asRecords(state.models);
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const providerRows = providers.map(editableRecord);
  const provider = providerRows.find((item) => identifier(item) === selectedProvider) ?? providerRows[0];
  const [selectedMcp, setSelectedMcp] = useState<string>();
  const [selectedPlugin, setSelectedPlugin] = useState<string>();
  const mcp = mcpServers.find((item) => identifier(item) === selectedMcp) ?? mcpServers[0];
  const plugin = plugins.find((item) => identifier(item) === selectedPlugin) ?? plugins[0];
  const [structuredWidth, setStructuredWidth] = useState(470);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const validationErrors = stringList(state.validation_errors);
  const validationWarnings = stringList(state.warnings);
  const validationStatus = Object.keys(state).length === 0
    ? undefined
    : validationErrors.length > 0
      ? validationErrors.map((message) => localizeCodexValidationMessage(message, translate)).join(" · ")
      : validationWarnings.length > 0
        ? validationWarnings.map((message) => localizeCodexValidationMessage(message, translate)).join(" · ")
        : undefined;
  const validationStatusStyle = validationErrors.length > 0
    ? styles.codexValidationError
    : validationWarnings.length > 0
      ? styles.codexValidationWarning
      : undefined;
  const patchProvider = (changes: UnknownRecord): void => {
    if (!provider) return;
    dispatch("patch", { providers: providerRows.map((item) => identifier(item) === identifier(provider) ? { ...item, ...changes } : item) });
  };
  const patchMcp = (changes: UnknownRecord): void => {
    if (!mcp) return;
    dispatch("patch", { mcp_servers: mcpServers.map((item) => identifier(item) === identifier(mcp) ? { ...item, ...changes } : item) });
  };
  const fileMissing = state.config_exists === false;
  return <SettingsWorkspace validationStatus={validationStatus} validationStatusStyle={validationStatusStyle} structuredWidth={structuredWidth} onStructuredWidthChange={setStructuredWidth} workspaceWidth={workspaceWidth} onWorkspaceWidthChange={setWorkspaceWidth} translate={translate} missingMessage={fileMissing ? translate("settings.codexMissing") : undefined} structured={
    <>
    <Section title={translate("codex.litellmDeployment")}><View style={styles.form}>
      <PickerField label={translate("codex.activeDeployment")} value={stringValue(structured.model, translate("common.none"))} values={deployments.length > 0 ? deployments.map((item) => stringValue(item.model)) : [translate("common.none")]} disabled={busy || deployments.length === 0} onSelect={(model) => { const selection = deployments.find((item) => stringValue(item.model) === model); if (selection) dispatch("select_model", { selection: { model: selection.model, provider: selection.provider, deployment_id: selection.deployment_id } }); }} />
    </View></Section>
    <Section title={translate("codex.directConnection")}><View style={styles.form}>
      <TextField label={translate("common.model")} value={stringValue(structured.model)} onCommit={(model) => dispatch("patch", { model })} />
      <TextField label={translate("codex.reviewModel")} value={stringValue(structured.review_model)} onCommit={(review_model) => dispatch("patch", { review_model })} />
      <PickerField label={translate("codex.provider")} value={stringValue(asRecord(structured.direct_connection).provider, stringValue(structured.model_provider, "openai"))} values={["openai", "amazon-bedrock", "ollama", "lmstudio", ...providers.map((item) => identifier(item)).filter((id) => !["openai", "amazon-bedrock", "ollama", "lmstudio"].includes(id))]} disabled={busy} onSelect={(provider) => dispatch("patch", { direct_connection: { provider, base_url: stringValue(asRecord(structured.direct_connection).base_url, stringValue(structured.openai_base_url)) } })} />
      <TextField label={translate("codex.gateway")} value={stringValue(asRecord(structured.direct_connection).base_url, stringValue(structured.openai_base_url))} onCommit={(base_url) => dispatch("patch", { direct_connection: { provider: stringValue(asRecord(structured.direct_connection).provider, stringValue(structured.model_provider, "openai")), base_url } })} />
      <NativeSecretField label={translate("common.apiKey")} hint={structured.api_key ? translate("runtime.secretRetained") : undefined} busy={busy} domain="codex" field="api_key" onSecretState={onSecretState} setTitle={translate("common.set")} clearTitle={translate("common.clear")} clearDisabled={busy || !structured.api_key} onClear={() => clearSecret({ domain: "codex", field: "api_key" })} />
      <PickerField label={translate("settings.credentialStore")} value={stringValue(structured.cli_auth_credentials_store, translate("common.empty"))} values={[translate("common.empty"), "file", "keyring", "auto", "ephemeral"]} disabled={busy} onSelect={(cli_auth_credentials_store) => dispatch("patch", { cli_auth_credentials_store: emptyToNull(cli_auth_credentials_store, translate) })} />
      <PickerField label={translate("settings.forcedLogin")} value={stringValue(structured.forced_login_method, translate("common.empty"))} values={[translate("common.empty"), "chatgpt", "api"]} disabled={busy} onSelect={(forced_login_method) => dispatch("patch", { forced_login_method: emptyToNull(forced_login_method, translate) })} />
    </View></Section>
    <Section title={translate("codex.behavior")}><View style={styles.form}>
      <PickerField label={translate("codex.reasoning")} value={stringValue(structured.model_reasoning_effort, translate("common.empty"))} values={[translate("common.empty"), "minimal", "low", "medium", "high", "xhigh"]} disabled={busy} onSelect={(model_reasoning_effort) => dispatch("patch", { model_reasoning_effort: emptyToNull(model_reasoning_effort, translate) })} />
      <PickerField label={translate("codex.planReasoning")} value={stringValue(structured.plan_mode_reasoning_effort, translate("common.empty"))} values={[translate("common.empty"), "none", "minimal", "low", "medium", "high", "xhigh"]} disabled={busy} onSelect={(plan_mode_reasoning_effort) => dispatch("patch", { plan_mode_reasoning_effort: emptyToNull(plan_mode_reasoning_effort, translate) })} />
      <PickerField label={translate("settings.reasoningSummary")} value={stringValue(structured.model_reasoning_summary, translate("common.empty"))} values={[translate("common.empty"), "auto", "concise", "detailed", "none"]} disabled={busy} onSelect={(model_reasoning_summary) => dispatch("patch", { model_reasoning_summary: emptyToNull(model_reasoning_summary, translate) })} />
      <PickerField label={translate("codex.verbosity")} value={stringValue(structured.model_verbosity, translate("common.empty"))} values={[translate("common.empty"), "low", "medium", "high"]} disabled={busy} onSelect={(model_verbosity) => dispatch("patch", { model_verbosity: emptyToNull(model_verbosity, translate) })} />
      <PickerField label={translate("settings.personality")} value={stringValue(structured.personality, translate("common.empty"))} values={[translate("common.empty"), "none", "friendly", "pragmatic"]} disabled={busy} onSelect={(personality) => dispatch("patch", { personality: emptyToNull(personality, translate) })} />
      <PickerField label={translate("settings.serviceTier")} value={stringValue(structured.service_tier, translate("common.empty"))} values={[translate("common.empty"), "fast", "flex"]} disabled={busy} onSelect={(service_tier) => dispatch("patch", { service_tier: emptyToNull(service_tier, translate) })} />
      <PickerField label={translate("codex.webSearch")} value={stringValue(structured.web_search, translate("common.empty"))} values={[translate("common.empty"), "disabled", "cached", "indexed", "live"]} disabled={busy} onSelect={(web_search) => dispatch("patch", { web_search: emptyToNull(web_search, translate) })} />
      <TextField label={translate("codex.contextWindow")} value={stringValue(structured.model_context_window)} keyboardType="numeric" onCommit={(model_context_window) => dispatch("patch", { model_context_window })} />
      <TextField label={translate("settings.autoCompactLimit")} value={stringValue(structured.model_auto_compact_token_limit)} keyboardType="numeric" onCommit={(model_auto_compact_token_limit) => dispatch("patch", { model_auto_compact_token_limit })} />
      <TextField label={translate("codex.toolOutputLimit")} value={stringValue(structured.tool_output_token_limit)} keyboardType="numeric" onCommit={(tool_output_token_limit) => dispatch("patch", { tool_output_token_limit })} />
    </View></Section>
    <Section title={translate("codex.features")}><FeatureToggles value={asRecord(structured.features)} supported={stringList(structured.supported_features)} disabled={busy} onChange={(features) => dispatch("patch", { features })} translate={translate} /></Section>
    <Section title={translate("codex.permissions")}><View style={styles.form}>
      <SegmentedField label={translate("codex.permissionMode")} value={stringValue(permissions.mode, "legacy")} values={assistantSettingOptions(["legacy", "profile", "unset"], translate)} disabled={busy} onSelect={(mode) => dispatch("patch", { permissions: { mode } })} />
      <PickerField label={translate("codex.sandboxMode")} value={stringValue(permissions.sandbox_mode)} values={["read-only", "workspace-write", "danger-full-access"]} disabled={busy || permissions.mode === "profile"} onSelect={(sandbox_mode) => dispatch("patch", { permissions: { sandbox_mode } })} />
      <PickerField label={translate("codex.approvalPolicy")} value={stringValue(permissions.approval_policy)} values={["untrusted", "on-request", "never"]} disabled={busy} onSelect={(approval_policy) => dispatch("patch", { permissions: { approval_policy } })} />
      <PickerField label={translate("settings.approvalReviewer")} value={stringValue(permissions.approvals_reviewer, translate("common.empty"))} values={[translate("common.empty"), "user", "auto_review"]} disabled={busy} onSelect={(approvals_reviewer) => dispatch("patch", { permissions: { approvals_reviewer: emptyToNull(approvals_reviewer, translate) } })} />
      <ToggleRow label={translate("codex.network")} value={booleanValue(permissions.network_access)} disabled={busy} onChange={(network_access) => dispatch("patch", { permissions: { network_access } })} />
      <TextField label={translate("codex.writableRoots")} value={stringList(permissions.writable_roots).join("\n")} multiline onCommit={(writable_roots) => dispatch("patch", { permissions: { writable_roots: splitLines(writable_roots) } })} />
      <PickerField label={translate("settings.permissionProfile")} value={stringValue(permissions.default_permissions, translate("common.empty"))} values={[translate("common.empty"), ...stringList(structured.permission_profiles)]} disabled={busy || permissions.mode === "legacy"} onSelect={(default_permissions) => dispatch("patch", { permissions: { default_permissions: emptyToNull(default_permissions, translate) } })} />
    </View></Section>
    <Section title={translate("codex.providers")}><View style={styles.split}>
      <NativeTable columns={[{ label: translate("providers.providerId"), width: 116 }, { label: translate("providers.baseUrl"), width: 230 }, { label: translate("providers.authentication"), width: 84 }]} rows={providerRows.map((item) => ({ key: identifier(item), cells: [identifier(item), stringValue(item.base_url), stringValue(item.auth_mode, "none")] }))} selectedKey={identifier(provider ?? {})} onSelectionChange={setSelectedProvider} style={styles.codexListTable} />
      <View style={styles.detailPane}>{provider ? <View style={styles.form}><TextField label={translate("providers.providerId")} value={identifier(provider)} onCommit={(id) => patchProvider({ id })} /><TextField label={translate("providers.displayName")} value={stringValue(provider.name)} onCommit={(name) => patchProvider({ name })} /><TextField label={translate("common.endpoint")} value={stringValue(provider.base_url)} onCommit={(base_url) => patchProvider({ base_url })} /><PickerField label={translate("providers.protocol")} value={stringValue(provider.wire_api, "responses")} values={["responses"]} disabled={busy} onSelect={(wire_api) => patchProvider({ wire_api })} /><PickerField label={translate("providers.authentication")} value={stringValue(provider.auth_mode, "none")} values={["none", "env_key", "openai_auth", "command", "bearer"]} disabled={busy} onSelect={(auth_mode) => patchProvider({ auth_mode })} /><TextField label={translate("codex.environmentKey")} value={stringValue(provider.env_key)} onCommit={(env_key) => patchProvider({ env_key })} /><NativeCheckbox label={translate("providers.requiresOpenAIAuth")} value={booleanValue(provider.requires_openai_auth)} disabled={busy} onValueChange={(requires_openai_auth) => patchProvider({ requires_openai_auth })} /><TextField label={translate("providers.authCommand")} value={stringValue(provider.auth_command)} onCommit={(auth_command) => patchProvider({ auth_command })} /></View> : <EmptyState translate={translate} />}</View>
      <View style={styles.listToolRail}><IconButton label="+" title={translate("common.add")} disabled={busy} onPress={() => dispatch("patch", { providers: [...providerRows, { id: `provider-${providers.length + 1}`, name: "", base_url: "", wire_api: "responses", auth_mode: "none" }] })} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !provider} onPress={() => provider && dispatch("patch", { providers: providerRows.filter((item) => identifier(item) !== identifier(provider)) })} /></View>
    </View></Section>
    <Section title={translate("codex.mcpPlugins")}><View style={styles.split}>
      <NativeTable columns={[{ label: translate("settings.serverId"), width: 138 }, { label: translate("codex.transport"), width: 90 }, { label: translate("common.status"), width: 70 }]} rows={mcpServers.map((item) => ({ key: identifier(item), cells: [identifier(item), stringValue(item.transport), booleanValue(item.enabled, true) ? translate("settings.enabled") : translate("settings.disabled")] }))} selectedKey={identifier(mcp ?? {})} onSelectionChange={setSelectedMcp} style={styles.codexListTable} />
      <View style={styles.detailPane}>{mcp ? <View style={styles.form}><TextField label={translate("settings.serverId")} value={identifier(mcp)} onCommit={(id) => patchMcp({ id })} /><PickerField label={translate("codex.transport")} value={stringValue(mcp.transport, "stdio")} values={["stdio", "http"]} disabled={busy} onSelect={(transport) => patchMcp({ transport })} /><TextField label={translate("codex.command")} value={stringValue(mcp.command)} onCommit={(command) => patchMcp({ command })} /><TextField label={translate("webdav.url")} value={stringValue(mcp.url)} onCommit={(url) => patchMcp({ url })} /><ToggleRow label={translate("common.enabled")} value={booleanValue(mcp.enabled, true)} disabled={busy} onChange={(enabled) => patchMcp({ enabled })} /><ToggleRow label={translate("codex.required")} value={booleanValue(mcp.required)} disabled={busy} onChange={(required) => patchMcp({ required })} /></View> : <EmptyState translate={translate} />}</View>
      <View style={styles.listToolRail}><IconButton label="+" title={translate("common.add")} disabled={busy} onPress={() => dispatch("patch", { mcp_servers: [...mcpServers, { id: `mcp-${mcpServers.length + 1}`, transport: "stdio", command: "", enabled: true, required: false }] })} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !mcp} onPress={() => mcp && dispatch("patch", { mcp_servers: mcpServers.filter((item) => identifier(item) !== identifier(mcp)) })} /></View>
    </View><View style={styles.pluginEditor}><NativeTable columns={[{ label: translate("settings.pluginId"), width: 180 }, { label: translate("common.status"), width: 90 }]} rows={plugins.map((item) => ({ key: identifier(item), cells: [identifier(item), booleanValue(item.enabled) ? translate("settings.enabled") : translate("settings.disabled")] }))} selectedKey={identifier(plugin ?? {})} onSelectionChange={setSelectedPlugin} style={styles.pluginTable} />{plugin ? <View style={styles.pluginFields}><TextField label={translate("settings.pluginId")} value={identifier(plugin)} onCommit={(id) => dispatch("patch", { plugins: plugins.map((entry) => identifier(entry) === identifier(plugin) ? { ...entry, id } : entry) })} /><NativeCheckbox label={translate("common.enabled")} value={booleanValue(plugin.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { plugins: plugins.map((entry) => identifier(entry) === identifier(plugin) ? { ...entry, enabled } : entry) })} /></View> : null}</View></Section>
    <Section title={translate("codex.advanced")}><View style={styles.form}><PickerField label={translate("codex.shellEnvironment")} value={stringValue(advanced.shell_environment_inherit, translate("common.empty"))} values={[translate("common.empty"), "all", "core", "none"]} disabled={busy} onSelect={(shell_environment_inherit) => dispatch("patch", { advanced: { shell_environment_inherit: emptyToNull(shell_environment_inherit, translate) } })} /><PickerField label={translate("codex.history")} value={stringValue(advanced.history_persistence, translate("common.empty"))} values={[translate("common.empty"), "save-all", "none"]} disabled={busy} onSelect={(history_persistence) => dispatch("patch", { advanced: { history_persistence: emptyToNull(history_persistence, translate) } })} /><TextField label={translate("codex.agentThreads")} value={stringValue(advanced.agents_max_threads)} keyboardType="numeric" onCommit={(agents_max_threads) => dispatch("patch", { advanced: { agents_max_threads } })} /><TextField label={translate("codex.agentDepth")} value={stringValue(advanced.agents_max_depth)} keyboardType="numeric" onCommit={(agents_max_depth) => dispatch("patch", { advanced: { agents_max_depth } })} /><PickerField label={translate("settings.fileOpener")} value={stringValue(advanced.file_opener, translate("common.empty"))} values={[translate("common.empty"), "vscode", "vscode-insiders", "windsurf", "cursor", "none"]} disabled={busy} onSelect={(file_opener) => dispatch("patch", { advanced: { file_opener: emptyToNull(file_opener, translate) } })} /><PickerField label={translate("settings.mcpCredentialStore")} value={stringValue(advanced.mcp_oauth_credentials_store, translate("common.empty"))} values={[translate("common.empty"), "auto", "file", "keyring"]} disabled={busy} onSelect={(mcp_oauth_credentials_store) => dispatch("patch", { advanced: { mcp_oauth_credentials_store: emptyToNull(mcp_oauth_credentials_store, translate) } })} /></View></Section>
    </>
  } raw={<><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawToml")} domain="codex" document="config" language="toml" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawAuth")} domain="codex" document="auth" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /></>} />;
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

function FeatureToggles({ value, supported, disabled, onChange, translate }: { value: UnknownRecord; supported: string[]; disabled: boolean; onChange: (features: UnknownRecord) => void; translate: Translate }): React.JSX.Element {
  const keys = [...new Set([...supported, ...Object.keys(value)])];
  return <View style={styles.form}>{keys.length === 0 ? <EmptyState translate={translate} /> : keys.map((key) => <ToggleRow key={key} label={codexFeatureLabel(key, translate)} value={booleanValue(value[key])} disabled={disabled} onChange={(enabled) => onChange({ ...value, [key]: enabled })} />)}</View>;
}

function ClaudeScreen({ snapshot, ipc, native, busy, translate, dispatch, onSecretState, clearSecret, deployment, onDeploymentChange, rawReloadToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; deployment: ClaudeDeploymentDraft; onDeploymentChange: (key: keyof ClaudeDeploymentDraft, value: string) => Promise<void>; rawReloadToken: number }): React.JSX.Element {
  const state = domainState(snapshot, "claude");
  const settings = asRecord(state.settings);
  const permissions = asRecord(settings.permissions);
  const sandbox = asRecord(settings.sandbox);
  const filesystem = asRecord(sandbox.filesystem);
  const network = asRecord(sandbox.network);
  const skillOverrides = asRecord(settings.skillOverrides);
  const spinnerTipsOverride = asRecord(settings.spinnerTipsOverride);
  const spinnerVerbs = asRecord(settings.spinnerVerbs);
  const worktree = asRecord(settings.worktree);
  const [structuredWidth, setStructuredWidth] = useState(470);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const fileMissing = settings.file_exists === false;
  const unavailable = state.available === false;
  const validationStatus = unavailable ? translate("settings.claudeUnavailable") : undefined;
  const updateDeployment = (key: keyof ClaudeDeploymentDraft, value: string): Promise<void> => onDeploymentChange(key, value);
  return <SettingsWorkspace validationStatus={validationStatus} validationStatusStyle={unavailable ? styles.codexValidationError : undefined} structuredWidth={structuredWidth} onStructuredWidthChange={setStructuredWidth} workspaceWidth={workspaceWidth} onWorkspaceWidthChange={setWorkspaceWidth} translate={translate} missingMessage={fileMissing ? translate("settings.claudeMissing") : undefined} structured={<>
    <Section title={translate("claude.deployment")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.model")} value={deployment.model} onCommit={(value) => updateDeployment("model", value)} />
      <TextField label={translate("claude.gateway")} value={deployment.base_url} onCommit={(value) => updateDeployment("base_url", value)} />
      <NativeSecretField label={translate("claude.token")} hint={settings.token_configured === true ? translate("runtime.secretRetained") : undefined} busy={busy} domain="claude" field="deployment_token" onSecretState={onSecretState} setTitle={translate("common.set")} clearTitle={translate("common.clear")} clearDisabled={busy || settings.token_configured !== true} onClear={() => clearSecret({ domain: "claude", field: "deployment_token" })} />
    </View></Section>
    <Section title={translate("claude.memory")}><View style={styles.structuredForm}>
      <ToggleRow label={translate("claude.autoMemory")} value={booleanValue(settings.autoMemoryEnabled, true)} disabled={busy} onChange={(autoMemoryEnabled) => dispatch("patch", { autoMemoryEnabled })} />
      <NativeSecretField label={translate("claude.autoMemoryDirectory")} hint={translate(settings.autoMemoryDirectoryConfigured === true ? "claude.autoMemoryDirectoryConfigured" : "claude.autoMemoryDirectoryHint")} busy={busy} domain="claude" field="auto_memory_directory" onSecretState={onSecretState} setTitle={translate("common.set")} clearTitle={translate("common.clear")} clearDisabled={busy || settings.autoMemoryDirectoryConfigured !== true} onClear={() => clearSecret({ domain: "claude", field: "auto_memory_directory" })} />
    </View></Section>
    <Section title={translate("claude.gitAttribution")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.commitAttribution")} value={stringValue(asRecord(settings.attribution).commit)} multiline compactMultiline onCommit={(commit) => dispatch("patch", { attribution: { commit } })} />
      <TextField label={translate("claude.prAttribution")} value={stringValue(asRecord(settings.attribution).pr)} multiline compactMultiline onCommit={(pr) => dispatch("patch", { attribution: { pr } })} />
      <ToggleRow label={translate("claude.sessionUrlAttribution")} value={booleanValue(asRecord(settings.attribution).sessionUrl, true)} disabled={busy} onChange={(sessionUrl) => dispatch("patch", { attribution: { sessionUrl } })} />
    </View></Section>
    <Section title={translate("claude.autoMode")}><View style={styles.structuredForm}>
      <ToggleRow label={translate("claude.disableAutoMode")} value={stringValue(settings.disableAutoMode) === "disable"} disabled={busy} onChange={(disabled) => dispatch("patch", { disableAutoMode: disabled ? "disable" : null })} />
      <ToggleRow label={translate("claude.classifyAllShell")} value={booleanValue(asRecord(settings.autoMode).classifyAllShell)} disabled={busy} onChange={(classifyAllShell) => dispatch("patch", { autoMode: { classifyAllShell } })} />
      <TextField label={translate("claude.autoModeEnvironment")} hint={translate("claude.autoModeDefaultsHint")} value={stringList(asRecord(settings.autoMode).environment).join("\n")} multiline compactMultiline onCommit={(environment) => dispatch("patch", { autoMode: { environment: splitLines(environment) } })} />
      <TextField label={translate("claude.autoModeAllow")} hint={translate("claude.autoModeDefaultsHint")} value={stringList(asRecord(settings.autoMode).allow).join("\n")} multiline compactMultiline onCommit={(allow) => dispatch("patch", { autoMode: { allow: splitLines(allow) } })} />
      <TextField label={translate("claude.autoModeSoftDeny")} hint={translate("claude.autoModeDefaultsHint")} value={stringList(asRecord(settings.autoMode).soft_deny).join("\n")} multiline compactMultiline onCommit={(soft_deny) => dispatch("patch", { autoMode: { soft_deny: splitLines(soft_deny) } })} />
      <TextField label={translate("claude.autoModeHardDeny")} hint={translate("claude.autoModeDefaultsHint")} value={stringList(asRecord(settings.autoMode).hard_deny).join("\n")} multiline compactMultiline onCommit={(hard_deny) => dispatch("patch", { autoMode: { hard_deny: splitLines(hard_deny) } })} />
      <PickerField label={translate("claude.autoUpdatesChannel")} value={stringValue(settings.autoUpdatesChannel, "latest")} values={["latest", "stable"]} disabled={busy} onSelect={(autoUpdatesChannel) => dispatch("patch", { autoUpdatesChannel })} />
    </View></Section>
    <Section title={translate("claude.vim")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.vimInsertModeRemaps")} hint={translate("claude.vimInsertModeRemapsHint")} value={vimInsertRemapLines(settings.vimInsertModeRemaps)} multiline compactMultiline onCommit={(remaps) => dispatch("patch", { vimInsertModeRemaps: vimInsertRemaps(remaps) })} />
    </View></Section>
    <Section title={translate("claude.voice")}><View style={styles.structuredForm}>
      <ToggleRow label={translate("claude.voiceEnabled")} value={booleanValue(asRecord(settings.voice).enabled)} disabled={busy} onChange={(enabled) => dispatch("patch", { voice: { enabled } })} />
      <PickerField label={translate("claude.voiceMode")} value={claudeVoiceModeLabel(stringValue(asRecord(settings.voice).mode, "tap"), translate)} values={["hold", "tap"].map((mode) => claudeVoiceModeLabel(mode, translate))} disabled={busy} onSelect={(label) => dispatch("patch", { voice: { mode: claudeVoiceMode(label, translate) } })} />
      <ToggleRow label={translate("claude.voiceAutoSubmit")} value={booleanValue(asRecord(settings.voice).autoSubmit)} disabled={busy} onChange={(autoSubmit) => dispatch("patch", { voice: { autoSubmit } })} />
    </View></Section>
    <Section title={translate("claude.permissions")}><View style={styles.structuredForm}>
      <PickerField label={translate("claude.permissions")} value={claudePermissionLabel(stringValue(permissions.defaultMode, "default"), translate)} values={claudePermissionLabels(stringValue(permissions.defaultMode, "default"), translate)} disabled={busy} onSelect={(permissionsLabel) => { const defaultMode = claudePermissionMode(permissionsLabel, translate); if (defaultMode) dispatch("patch", { permissions: { defaultMode } }); }} />
      <ToggleRow label={translate("claude.disableBypassPermissions")} value={stringValue(permissions.disableBypassPermissionsMode) === "disable"} disabled={busy} onChange={(disabled) => dispatch("patch", { permissions: { disableBypassPermissionsMode: disabled ? "disable" : null } })} />
      {containsPrivateMarker(permissions.allow) ? <InfoPair label={translate("claude.allow")} value={translate("screen.configured")} /> : <TextField label={translate("claude.allow")} value={stringList(permissions.allow).join("\n")} multiline compactMultiline onCommit={(allow) => dispatch("patch", { permissions: { allow: splitLines(allow) } })} />}
      {containsPrivateMarker(permissions.ask) ? <InfoPair label={translate("claude.ask")} value={translate("screen.configured")} /> : <TextField label={translate("claude.ask")} value={stringList(permissions.ask).join("\n")} multiline compactMultiline onCommit={(ask) => dispatch("patch", { permissions: { ask: splitLines(ask) } })} />}
      {containsPrivateMarker(permissions.deny) ? <InfoPair label={translate("claude.deny")} value={translate("screen.configured")} /> : <TextField label={translate("claude.deny")} value={stringList(permissions.deny).join("\n")} multiline compactMultiline onCommit={(deny) => dispatch("patch", { permissions: { deny: splitLines(deny) } })} />}
      {containsPrivateMarker(permissions.additionalDirectories) ? <InfoPair label={translate("claude.additionalDirectories")} value={translate("screen.configured")} /> : <TextField label={translate("claude.additionalDirectories")} value={stringList(permissions.additionalDirectories).join("\n")} multiline compactMultiline onCommit={(additionalDirectories) => dispatch("patch", { permissions: { additionalDirectories: splitLines(additionalDirectories) } })} />}
    </View></Section>
    <Section title={translate("claude.sandbox")}><View style={styles.structuredForm}>
      <ToggleRow label={translate("claude.sandbox")} value={booleanValue(sandbox.enabled)} disabled={busy} onChange={(enabled) => dispatch("patch", { sandbox: { enabled } })} />
      <ToggleRow label={translate("claude.sandboxFailIfUnavailable")} value={booleanValue(sandbox.failIfUnavailable)} disabled={busy} onChange={(failIfUnavailable) => dispatch("patch", { sandbox: { failIfUnavailable } })} />
      <ToggleRow label={translate("claude.sandboxAutoAllowBash")} value={booleanValue(sandbox.autoAllowBashIfSandboxed, true)} disabled={busy} onChange={(autoAllowBashIfSandboxed) => dispatch("patch", { sandbox: { autoAllowBashIfSandboxed } })} />
      <ToggleRow label={translate("claude.sandboxAllowUnsandboxed")} value={booleanValue(sandbox.allowUnsandboxedCommands, true)} disabled={busy} onChange={(allowUnsandboxedCommands) => dispatch("patch", { sandbox: { allowUnsandboxedCommands } })} />
      <ToggleRow label={translate("claude.sandboxWeakerNested")} value={booleanValue(sandbox.enableWeakerNestedSandbox)} disabled={busy} onChange={(enableWeakerNestedSandbox) => dispatch("patch", { sandbox: { enableWeakerNestedSandbox } })} />
      <ToggleRow label={translate("claude.sandboxWeakerNetwork")} value={booleanValue(sandbox.enableWeakerNetworkIsolation)} disabled={busy} onChange={(enableWeakerNetworkIsolation) => dispatch("patch", { sandbox: { enableWeakerNetworkIsolation } })} />
      <ToggleRow label={translate("claude.sandboxAppleEvents")} value={booleanValue(sandbox.allowAppleEvents)} disabled={busy} onChange={(allowAppleEvents) => dispatch("patch", { sandbox: { allowAppleEvents } })} />
      {containsPrivateMarker(sandbox.excludedCommands) ? <InfoPair label={translate("claude.sandboxExcludedCommands")} value={translate("screen.configured")} /> : <TextField label={translate("claude.sandboxExcludedCommands")} value={stringList(sandbox.excludedCommands).join("\n")} multiline compactMultiline onCommit={(excludedCommands) => dispatch("patch", { sandbox: { excludedCommands: splitLines(excludedCommands) } })} />}
      <ToggleRow label={translate("claude.filesystem")} value={!booleanValue(filesystem.disabled)} disabled={busy} onChange={(enabled) => dispatch("patch", { sandbox: { filesystem: { disabled: !enabled } } })} />
      {containsPrivateMarker(filesystem.allowWrite) ? <InfoPair label={translate("claude.allowWrite")} value={translate("screen.configured")} /> : <TextField label={translate("claude.allowWrite")} value={stringList(filesystem.allowWrite).join("\n")} multiline compactMultiline onCommit={(allowWrite) => dispatch("patch", { sandbox: { filesystem: { allowWrite: splitLines(allowWrite) } } })} />}
      {containsPrivateMarker(filesystem.denyWrite) ? <InfoPair label={translate("claude.denyWrite")} value={translate("screen.configured")} /> : <TextField label={translate("claude.denyWrite")} value={stringList(filesystem.denyWrite).join("\n")} multiline compactMultiline onCommit={(denyWrite) => dispatch("patch", { sandbox: { filesystem: { denyWrite: splitLines(denyWrite) } } })} />}
      {containsPrivateMarker(filesystem.allowRead) ? <InfoPair label={translate("claude.allowRead")} value={translate("screen.configured")} /> : <TextField label={translate("claude.allowRead")} value={stringList(filesystem.allowRead).join("\n")} multiline compactMultiline onCommit={(allowRead) => dispatch("patch", { sandbox: { filesystem: { allowRead: splitLines(allowRead) } } })} />}
      {containsPrivateMarker(filesystem.denyRead) ? <InfoPair label={translate("claude.denyRead")} value={translate("screen.configured")} /> : <TextField label={translate("claude.denyRead")} value={stringList(filesystem.denyRead).join("\n")} multiline compactMultiline onCommit={(denyRead) => dispatch("patch", { sandbox: { filesystem: { denyRead: splitLines(denyRead) } } })} />}
      {containsPrivateMarker(network.allowedDomains) ? <InfoPair label={translate("claude.allowedDomains")} value={translate("screen.configured")} /> : <TextField label={translate("claude.allowedDomains")} value={stringList(network.allowedDomains).join("\n")} multiline compactMultiline onCommit={(allowedDomains) => dispatch("patch", { sandbox: { network: { allowedDomains: splitLines(allowedDomains) } } })} />}
      {containsPrivateMarker(network.deniedDomains) ? <InfoPair label={translate("claude.deniedDomains")} value={translate("screen.configured")} /> : <TextField label={translate("claude.deniedDomains")} value={stringList(network.deniedDomains).join("\n")} multiline compactMultiline onCommit={(deniedDomains) => dispatch("patch", { sandbox: { network: { deniedDomains: splitLines(deniedDomains) } } })} />}
      {containsPrivateMarker(network.allowUnixSockets) ? <InfoPair label={translate("claude.allowUnixSockets")} value={translate("screen.configured")} /> : <TextField label={translate("claude.allowUnixSockets")} value={stringList(network.allowUnixSockets).join("\n")} multiline compactMultiline onCommit={(allowUnixSockets) => dispatch("patch", { sandbox: { network: { allowUnixSockets: splitLines(allowUnixSockets) } } })} />}
      <TextField label={translate("claude.allowMachLookup")} value={stringList(network.allowMachLookup).join("\n")} multiline compactMultiline onCommit={(allowMachLookup) => dispatch("patch", { sandbox: { network: { allowMachLookup: splitLines(allowMachLookup) } } })} />
      <ToggleRow label={translate("claude.allowAllUnixSockets")} value={booleanValue(network.allowAllUnixSockets)} disabled={busy} onChange={(allowAllUnixSockets) => dispatch("patch", { sandbox: { network: { allowAllUnixSockets } } })} />
      <ToggleRow label={translate("claude.allowLocalBinding")} value={booleanValue(network.allowLocalBinding)} disabled={busy} onChange={(allowLocalBinding) => dispatch("patch", { sandbox: { network: { allowLocalBinding } } })} />
      <ToggleRow label={translate("claude.strictAllowlist")} value={booleanValue(network.strictAllowlist)} disabled={busy} onChange={(strictAllowlist) => dispatch("patch", { sandbox: { network: { strictAllowlist } } })} />
      <TextField label={translate("claude.httpProxyPort")} value={network.httpProxyPort === undefined ? "" : String(network.httpProxyPort)} keyboardType="numeric" onCommit={(httpProxyPort) => dispatch("patch", { sandbox: { network: { httpProxyPort: httpProxyPort.trim() ? Number(httpProxyPort) : null } } })} />
      <TextField label={translate("claude.socksProxyPort")} value={network.socksProxyPort === undefined ? "" : String(network.socksProxyPort)} keyboardType="numeric" onCommit={(socksProxyPort) => dispatch("patch", { sandbox: { network: { socksProxyPort: socksProxyPort.trim() ? Number(socksProxyPort) : null } } })} />
    </View></Section>
    <Section title={translate("claude.modelBehavior")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.fallbackModel")} value={stringList(settings.fallbackModel).join("\n") || stringValue(settings.fallbackModel)} multiline compactMultiline onCommit={(fallbackModel) => dispatch("patch", { fallbackModel: splitLines(fallbackModel) })} />
      <TextField label={translate("claude.availableModels")} value={stringList(settings.availableModels).join("\n")} multiline compactMultiline onCommit={(availableModels) => dispatch("patch", { availableModels: splitLines(availableModels) })} />
      <TextField label={translate("claude.advisorModel")} value={stringValue(settings.advisorModel)} onCommit={(advisorModel) => dispatch("patch", { advisorModel: advisorModel.trim() || null })} />
      <TextField label={translate("claude.agent")} value={stringValue(settings.agent)} onCommit={(agent) => dispatch("patch", { agent: agent.trim() || null })} />
      <TextField label={translate("claude.teammateDefaultModel")} value={stringValue(settings.teammateDefaultModel)} onCommit={(teammateDefaultModel) => dispatch("patch", { teammateDefaultModel: teammateDefaultModel.trim() || null })} />
      <PickerField label={translate("claude.effortLevel")} value={stringValue(settings.effortLevel, "medium")} values={["low", "medium", "high", "xhigh"]} disabled={busy} onSelect={(effortLevel) => dispatch("patch", { effortLevel })} />
      <ToggleRow label={translate("claude.alwaysThinking")} value={booleanValue(settings.alwaysThinkingEnabled)} disabled={busy} onChange={(alwaysThinkingEnabled) => dispatch("patch", { alwaysThinkingEnabled })} />
      <ToggleRow label={translate("claude.showThinkingSummaries")} value={booleanValue(settings.showThinkingSummaries)} disabled={busy} onChange={(showThinkingSummaries) => dispatch("patch", { showThinkingSummaries })} />
      <ToggleRow label={translate("claude.fastMode")} value={booleanValue(settings.fastMode)} disabled={busy} onChange={(fastMode) => dispatch("patch", { fastMode })} />
      <ToggleRow label={translate("claude.fastModePerSession")} value={booleanValue(settings.fastModePerSessionOptIn)} disabled={busy} onChange={(fastModePerSessionOptIn) => dispatch("patch", { fastModePerSessionOptIn })} />
      <ToggleRow label={translate("claude.autoCompact")} value={booleanValue(settings.autoCompactEnabled, true)} disabled={busy} onChange={(autoCompactEnabled) => dispatch("patch", { autoCompactEnabled })} />
      <ToggleRow label={translate("claude.fileCheckpoints")} value={booleanValue(settings.fileCheckpointingEnabled)} disabled={busy} onChange={(fileCheckpointingEnabled) => dispatch("patch", { fileCheckpointingEnabled })} />
      <TextField label={translate("claude.outputStyle")} value={stringValue(settings.outputStyle)} onCommit={(outputStyle) => dispatch("patch", { outputStyle: outputStyle.trim() || null })} />
      <TextField label={translate("claude.cleanupDays")} value={settings.cleanupPeriodDays === undefined ? "" : String(settings.cleanupPeriodDays)} keyboardType="numeric" onCommit={(cleanupPeriodDays) => dispatch("patch", { cleanupPeriodDays: cleanupPeriodDays.trim() ? Number(cleanupPeriodDays) : null })} />
    </View></Section>
    <Section title={translate("claude.notifications")}><View style={styles.structuredForm}>
      <PickerField label={translate("claude.editorMode")} value={stringValue(settings.editorMode, "normal")} values={["normal", "vim"]} disabled={busy} onSelect={(editorMode) => dispatch("patch", { editorMode })} />
      <PickerField label={translate("claude.defaultShell")} value={stringValue(settings.defaultShell, "bash")} values={["bash", "powershell"]} disabled={busy} onSelect={(defaultShell) => dispatch("patch", { defaultShell })} />
      <PickerField label={translate("claude.theme")} value={stringValue(settings.theme, "auto")} values={claudeThemeValues(settings.theme)} disabled={busy} onSelect={(theme) => dispatch("patch", { theme })} />
      <PickerField label={translate("claude.viewMode")} value={stringValue(settings.viewMode, "default")} values={["default", "verbose", "focus"]} disabled={busy} onSelect={(viewMode) => dispatch("patch", { viewMode })} />
      <PickerField label={translate("claude.tui")} value={stringValue(settings.tui, "default")} values={["default", "fullscreen"]} disabled={busy} onSelect={(tui) => dispatch("patch", { tui })} />
      <PickerField label={translate("claude.teammateMode")} value={stringValue(settings.teammateMode, "in-process")} values={["in-process", "auto", "tmux", "iterm2"]} disabled={busy} onSelect={(teammateMode) => dispatch("patch", { teammateMode })} />
      <PickerField label={translate("claude.preferredNotifChannel")} value={stringValue(settings.preferredNotifChannel, "auto")} values={["auto", "terminal_bell", "iterm2", "iterm2_with_bell", "kitty", "ghostty", "notifications_disabled"]} disabled={busy} onSelect={(preferredNotifChannel) => dispatch("patch", { preferredNotifChannel })} />
      <PickerField label={translate("claude.askUserQuestionTimeout")} value={stringValue(settings.askUserQuestionTimeout, "never")} values={["60s", "5m", "10m", "never"]} disabled={busy} onSelect={(askUserQuestionTimeout) => dispatch("patch", { askUserQuestionTimeout })} />
      <PickerField label={translate("claude.diffTool")} value={stringValue(settings.diffTool, "auto")} values={["auto", "terminal"]} disabled={busy} onSelect={(diffTool) => dispatch("patch", { diffTool })} />
      <TextField label={translate("claude.responseLanguage")} value={stringValue(settings.language)} onCommit={(language) => dispatch("patch", { language: language.trim() || null })} />
      <ToggleRow label={translate("claude.verbose")} value={booleanValue(settings.verbose)} disabled={busy} onChange={(verbose) => dispatch("patch", { verbose })} />
      <ToggleRow label={translate("claude.spinnerTips")} value={booleanValue(settings.spinnerTipsEnabled, true)} disabled={busy} onChange={(spinnerTipsEnabled) => dispatch("patch", { spinnerTipsEnabled })} />
      <ToggleRow label={translate("claude.terminalProgress")} value={booleanValue(settings.terminalProgressBarEnabled, true)} disabled={busy} onChange={(terminalProgressBarEnabled) => dispatch("patch", { terminalProgressBarEnabled })} />
      <ToggleRow label={translate("claude.reducedMotion")} value={booleanValue(settings.prefersReducedMotion)} disabled={busy} onChange={(prefersReducedMotion) => dispatch("patch", { prefersReducedMotion })} />
      <ToggleRow label={translate("claude.screenReader")} value={booleanValue(settings.axScreenReader)} disabled={busy} onChange={(axScreenReader) => dispatch("patch", { axScreenReader })} />
      <ToggleRow label={translate("claude.disableSyntaxHighlighting")} value={booleanValue(settings.syntaxHighlightingDisabled)} disabled={busy} onChange={(syntaxHighlightingDisabled) => dispatch("patch", { syntaxHighlightingDisabled })} />
      <ToggleRow label={translate("claude.autoScroll")} value={booleanValue(settings.autoScrollEnabled, true)} disabled={busy} onChange={(autoScrollEnabled) => dispatch("patch", { autoScrollEnabled })} />
      <ToggleRow label={translate("claude.wheelAcceleration")} value={booleanValue(settings.wheelScrollAccelerationEnabled, true)} disabled={busy} onChange={(wheelScrollAccelerationEnabled) => dispatch("patch", { wheelScrollAccelerationEnabled })} />
      <ToggleRow label={translate("claude.showTurnDuration")} value={booleanValue(settings.showTurnDuration, true)} disabled={busy} onChange={(showTurnDuration) => dispatch("patch", { showTurnDuration })} />
      <ToggleRow label={translate("claude.awaySummary")} value={booleanValue(settings.awaySummaryEnabled, true)} disabled={busy} onChange={(awaySummaryEnabled) => dispatch("patch", { awaySummaryEnabled })} />
      <ToggleRow label={translate("claude.pushWhenDone")} value={booleanValue(settings.agentPushNotifEnabled)} disabled={busy} onChange={(agentPushNotifEnabled) => dispatch("patch", { agentPushNotifEnabled })} />
      <ToggleRow label={translate("claude.pushWhenInputNeeded")} value={booleanValue(settings.inputNeededNotifEnabled)} disabled={busy} onChange={(inputNeededNotifEnabled) => dispatch("patch", { inputNeededNotifEnabled })} />
      <ToggleRow label={translate("claude.remoteControlAtStartup")} value={booleanValue(settings.remoteControlAtStartup)} disabled={busy} onChange={(remoteControlAtStartup) => dispatch("patch", { remoteControlAtStartup })} />
      <ToggleRow label={translate("claude.autoConnectIde")} value={booleanValue(settings.autoConnectIde)} disabled={busy} onChange={(autoConnectIde) => dispatch("patch", { autoConnectIde })} />
      <ToggleRow label={translate("claude.autoInstallIdeExtension")} value={booleanValue(settings.autoInstallIdeExtension, true)} disabled={busy} onChange={(autoInstallIdeExtension) => dispatch("patch", { autoInstallIdeExtension })} />
      <ToggleRow label={translate("claude.externalEditorContext")} value={booleanValue(settings.externalEditorContext)} disabled={busy} onChange={(externalEditorContext) => dispatch("patch", { externalEditorContext })} />
      <ToggleRow label={translate("claude.permissionExplainer")} value={booleanValue(settings.permissionExplainerEnabled, true)} disabled={busy} onChange={(permissionExplainerEnabled) => dispatch("patch", { permissionExplainerEnabled })} />
    </View></Section>
    <Section title={translate("claude.workflow")}><View style={styles.structuredForm}>
      <ToggleRow label={translate("claude.projectMcpServers")} value={booleanValue(settings.enableAllProjectMcpServers)} disabled={busy} onChange={(enableAllProjectMcpServers) => dispatch("patch", { enableAllProjectMcpServers })} />
      <TextField label={translate("claude.enabledMcpServers")} value={stringList(settings.enabledMcpjsonServers).join("\n")} multiline compactMultiline onCommit={(enabledMcpjsonServers) => dispatch("patch", { enabledMcpjsonServers: splitLines(enabledMcpjsonServers) })} />
      <TextField label={translate("claude.disabledMcpServers")} value={stringList(settings.disabledMcpjsonServers).join("\n")} multiline compactMultiline onCommit={(disabledMcpjsonServers) => dispatch("patch", { disabledMcpjsonServers: splitLines(disabledMcpjsonServers) })} />
      <ToggleRow label={translate("claude.respectGitignore")} value={booleanValue(settings.respectGitignore, true)} disabled={busy} onChange={(respectGitignore) => dispatch("patch", { respectGitignore })} />
      <ToggleRow label={translate("claude.includeGitInstructions")} value={booleanValue(settings.includeGitInstructions, true)} disabled={busy} onChange={(includeGitInstructions) => dispatch("patch", { includeGitInstructions })} />
      <ToggleRow label={translate("claude.enableArtifact")} value={booleanValue(settings.enableArtifact)} disabled={busy} onChange={(enableArtifact) => dispatch("patch", { enableArtifact })} />
      <ToggleRow label={translate("claude.disableWorkflows")} value={booleanValue(settings.disableWorkflows)} disabled={busy} onChange={(disableWorkflows) => dispatch("patch", { disableWorkflows })} />
      <ToggleRow label={translate("claude.workflowKeywordTrigger")} value={booleanValue(settings.workflowKeywordTriggerEnabled, true)} disabled={busy} onChange={(workflowKeywordTriggerEnabled) => dispatch("patch", { workflowKeywordTriggerEnabled })} />
      <PickerField label={translate("claude.workflowSize")} value={stringValue(settings.workflowSizeGuideline, "unrestricted")} values={["unrestricted", "small", "medium", "large"]} disabled={busy} onSelect={(workflowSizeGuideline) => dispatch("patch", { workflowSizeGuideline })} />
      <ToggleRow label={translate("claude.emojiCompletion")} value={booleanValue(settings.emojiCompletionEnabled, true)} disabled={busy} onChange={(emojiCompletionEnabled) => dispatch("patch", { emojiCompletionEnabled })} />
      <ToggleRow label={translate("claude.respondToShell")} value={booleanValue(settings.respondToBashCommands, true)} disabled={busy} onChange={(respondToBashCommands) => dispatch("patch", { respondToBashCommands })} />
      <ToggleRow label={translate("claude.showClearContext")} value={booleanValue(settings.showClearContextOnPlanAccept)} disabled={busy} onChange={(showClearContextOnPlanAccept) => dispatch("patch", { showClearContextOnPlanAccept })} />
      <ToggleRow label={translate("claude.switchModelsOnFlag")} value={booleanValue(settings.switchModelsOnFlag, true)} disabled={busy} onChange={(switchModelsOnFlag) => dispatch("patch", { switchModelsOnFlag })} />
      <ToggleRow label={translate("claude.useAutoModeDuringPlan")} value={booleanValue(settings.useAutoModeDuringPlan, true)} disabled={busy} onChange={(useAutoModeDuringPlan) => dispatch("patch", { useAutoModeDuringPlan })} />
    </View></Section>
    <Section title={translate("claude.capabilities")}><View style={styles.structuredForm}>
      <ToggleRow label={translate("claude.disableBundledSkills")} value={booleanValue(settings.disableBundledSkills)} disabled={busy} onChange={(disableBundledSkills) => dispatch("patch", { disableBundledSkills })} />
      <ToggleRow label={translate("claude.disableClaudeAiConnectors")} value={booleanValue(settings.disableClaudeAiConnectors)} disabled={busy} onChange={(disableClaudeAiConnectors) => dispatch("patch", { disableClaudeAiConnectors })} />
      <ToggleRow label={translate("claude.disableRemoteControl")} value={booleanValue(settings.disableRemoteControl)} disabled={busy} onChange={(disableRemoteControl) => dispatch("patch", { disableRemoteControl })} />
      <ToggleRow label={translate("claude.disableDeepLinkRegistration")} value={stringValue(settings.disableDeepLinkRegistration) === "disable"} disabled={busy} onChange={(disabled) => dispatch("patch", { disableDeepLinkRegistration: disabled ? "disable" : null })} />
      <ToggleRow label={translate("claude.disableSkillShellExecution")} value={booleanValue(settings.disableSkillShellExecution)} disabled={busy} onChange={(disableSkillShellExecution) => dispatch("patch", { disableSkillShellExecution })} />
      <ToggleRow label={translate("claude.disableAllHooks")} value={booleanValue(settings.disableAllHooks)} disabled={busy} onChange={(disableAllHooks) => dispatch("patch", { disableAllHooks })} />
      <ToggleRow label={translate("claude.disableAgentView")} value={booleanValue(settings.disableAgentView)} disabled={busy} onChange={(disableAgentView) => dispatch("patch", { disableAgentView })} />
      <ToggleRow label={translate("claude.disableArtifact")} value={booleanValue(settings.disableArtifact)} disabled={busy} onChange={(disableArtifact) => dispatch("patch", { disableArtifact })} />
      <ToggleRow label={translate("claude.skipWebFetchPreflight")} value={booleanValue(settings.skipWebFetchPreflight)} disabled={busy} onChange={(skipWebFetchPreflight) => dispatch("patch", { skipWebFetchPreflight })} />
    </View></Section>
    <Section title={translate("claude.skillSettings")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.skillOverrides")} hint={translate("claude.skillOverridesHint")} value={claudeSkillOverrideLines(skillOverrides)} multiline compactMultiline onCommit={(value) => dispatch("patch", { skillOverrides: claudeSkillOverridesPatch(skillOverrides, value) })} />
      <TextField label={translate("claude.spinnerTipsOverride")} hint={translate("claude.spinnerTipsOverrideHint")} value={stringList(spinnerTipsOverride.tips).join("\n")} multiline compactMultiline onCommit={(tips) => dispatch("patch", { spinnerTipsOverride: { tips: splitLines(tips) } })} />
      <ToggleRow label={translate("claude.excludeDefaultSpinnerTips")} value={booleanValue(spinnerTipsOverride.excludeDefault)} disabled={busy} onChange={(excludeDefault) => dispatch("patch", { spinnerTipsOverride: { excludeDefault } })} />
      <TextField label={translate("claude.spinnerVerbs")} hint={translate("claude.spinnerVerbsHint")} value={stringList(spinnerVerbs.verbs).join("\n")} multiline compactMultiline onCommit={(verbs) => dispatch("patch", { spinnerVerbs: { verbs: splitLines(verbs) } })} />
      <PickerField label={translate("claude.spinnerVerbMode")} value={stringValue(spinnerVerbs.mode, "append")} values={["append", "replace"]} disabled={busy} onSelect={(mode) => dispatch("patch", { spinnerVerbs: { mode } })} />
    </View></Section>
    <Section title={translate("claude.worktree")}><View style={styles.structuredForm}>
      <PickerField label={translate("claude.worktreeBaseRef")} value={stringValue(worktree.baseRef, translate("common.empty"))} values={[translate("common.empty"), "fresh", "head"]} disabled={busy} onSelect={(baseRef) => dispatch("patch", { worktree: { baseRef: emptyToNull(baseRef, translate) } })} />
      <PickerField label={translate("claude.worktreeBgIsolation")} value={stringValue(worktree.bgIsolation, translate("common.empty"))} values={[translate("common.empty"), "worktree", "none"]} disabled={busy} onSelect={(bgIsolation) => dispatch("patch", { worktree: { bgIsolation: emptyToNull(bgIsolation, translate) } })} />
    </View></Section>
    <Section title={translate("claude.advanced")}><View style={styles.structuredForm}>
      <TextField label={translate("claude.minimumVersion")} value={stringValue(settings.minimumVersion)} onCommit={(minimumVersion) => dispatch("patch", { minimumVersion: minimumVersion.trim() || null })} />
      <TextField label={translate("claude.feedbackSurveyRate")} value={stringValue(settings.feedbackSurveyRate)} keyboardType="numeric" onCommit={(feedbackSurveyRate) => dispatch("patch", { feedbackSurveyRate: feedbackSurveyRate.trim() ? numericOrText(feedbackSurveyRate) : null })} />
      <TextField label={translate("claude.skillListingBudgetFraction")} value={stringValue(settings.skillListingBudgetFraction)} keyboardType="numeric" onCommit={(skillListingBudgetFraction) => dispatch("patch", { skillListingBudgetFraction: skillListingBudgetFraction.trim() ? numericOrText(skillListingBudgetFraction) : null })} />
      <TextField label={translate("claude.skillListingMaxDescChars")} value={stringValue(settings.skillListingMaxDescChars)} keyboardType="numeric" onCommit={(skillListingMaxDescChars) => dispatch("patch", { skillListingMaxDescChars: skillListingMaxDescChars.trim() ? numericOrText(skillListingMaxDescChars) : null })} />
      <TextField label={translate("claude.companyAnnouncements")} value={stringList(settings.companyAnnouncements).join("\n")} multiline compactMultiline onCommit={(companyAnnouncements) => dispatch("patch", { companyAnnouncements: splitLines(companyAnnouncements) })} />
    </View></Section>
  </>} raw={<RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.rawJson")} domain="claude" document="settings" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} />} />;
}

function claudePermissionLabel(value: string, translate: Translate): string {
  const key = `claude.permission.${value}`;
  return CLAUDE_PERMISSION_MODES.includes(value) ? translate(key) : translate("claude.permission.unknown", { value });
}

const CLAUDE_PERMISSION_MODES = ["default", "manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "delegate"];

function claudePermissionLabels(current: string, translate: Translate): string[] {
  const labels = CLAUDE_PERMISSION_MODES.map((value) => claudePermissionLabel(value, translate));
  const currentLabel = claudePermissionLabel(current, translate);
  return labels.includes(currentLabel) ? labels : [currentLabel, ...labels];
}

function claudePermissionMode(label: string, translate: Translate): string | undefined {
  return CLAUDE_PERMISSION_MODES.find((value) => claudePermissionLabel(value, translate) === label);
}

function claudeSkillOverrideLines(value: unknown): string {
  return Object.entries(asRecord(value))
    .filter(([, mode]) => typeof mode === "string")
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, mode]) => `${name} = ${mode}`)
    .join("\n");
}

function claudeSkillOverridesPatch(current: unknown, text: string): UnknownRecord {
  const patch: UnknownRecord = {};
  for (const name of Object.keys(asRecord(current))) patch[name] = null;
  for (const line of splitLines(text)) {
    const separator = line.indexOf("=");
    const name = (separator < 0 ? line : line.slice(0, separator)).trim();
    const mode = (separator < 0 ? "" : line.slice(separator + 1)).trim();
    patch[name] = mode;
  }
  return patch;
}

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
    control = <NativeCheckbox label={label} value={booleanValue(item.value)} disabled={busy} onValueChange={(next) => dispatch("set_setting", { key, value: kind === "bool_auto" ? (next ? "auto" : "off") : next })} style={styles.runtimeBooleanControl} />;
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
  return <View style={styles.runtimeField}><View style={styles.runtimeInputRow}>{isBoolean ? <View style={styles.runtimeBooleanSlot}>{control}</View> : <><Text numberOfLines={2} style={styles.runtimeFieldLabel} accessibilityLabel={label}>{label}</Text><View style={styles.runtimeValueSlot}>{control}</View>{unit ? <Text numberOfLines={1} style={styles.runtimeUnit}>{unit}</Text> : null}<View style={styles.runtimeActionSlot}>{action}</View></>}</View><RuntimeFieldMeta item={item} translate={translate} /></View>;
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

type RenderedLogRecord = {
  key: string;
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

type LogColumn = { label: string; width: number; value: (row: RenderedLogRecord) => string };

function shortLogTimestamp(value: unknown): string {
  const raw = stringValue(value);
  if (!raw) return "";
  const parsed = new Date(raw);
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
  return details.join(" · ");
}

function recoveryStatusLabel(value: string, translate: Translate): string {
  const labels: Record<string, Parameters<Translate>[0]> = {
    waiting: "logs.recoveryStatus.waiting",
    polling: "logs.recoveryStatus.polling",
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
    const retry = part.match(/^retry=(.+)s$/);
    if (retry?.[1]) return translate("logs.recoveryDetail.retry", { value: retry[1] });
    const reason = part.match(/^reason=(.+)$/);
    if (reason?.[1]) return translate(reasonLabels[reason[1]] ?? "logs.recoveryReason.unknown");
    return "";
  }).filter(Boolean).join(" · ");
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
  return parts.join(" · ");
}

function safeOriginalLogRecord(record: unknown): string {
  if (typeof record === "string") return record;
  try {
    return JSON.stringify(record, null, 2);
  } catch {
    return compactLogValue(record);
  }
}

function parseTextLogRecord(record: string, tab: LogTab, index: number, translate: Translate): RenderedLogRecord {
  let detail = record.trim();
  let time = "";
  while (detail.startsWith("[")) {
    const match = detail.match(/^\[([^\]]+)\]\s*(.*)$/);
    if (!match || Number.isNaN(new Date(match[1]).getTime())) break;
    if (!time) time = shortLogTimestamp(match[1]);
    detail = match[2].trim();
  }
  if (!time) {
    const leadingTimestamp = detail.match(/^(Updated\s+)?(\d{4}-\d{2}-\d{2}T\S+)\s*(.*)$/);
    if (leadingTimestamp && !Number.isNaN(new Date(leadingTimestamp[2] ?? "").getTime())) {
      time = shortLogTimestamp(leadingTimestamp[2]);
      detail = `${leadingTimestamp[1] ?? ""}${leadingTimestamp[3] ?? ""}`.trim();
    }
  }
  let source = logTitle(tab, translate);
  let status = "";
  let model = "";
  let tokens = "";
  const servicePrefix = detail.match(/^\[(\d+)\]\s+\[([A-Z]+)\]\s*(.*)$/);
  if (servicePrefix) {
    source = `PID ${servicePrefix[1]}`;
    status = servicePrefix[2];
    detail = servicePrefix[3].trim();
  } else {
    const proxyPrefix = detail.match(/^(?:\d{2}:\d{2}:\d{2}\s+-\s+)?([^:]+):(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s*(.*)$/);
    const levelPrefix = detail.match(/^(?:\[([A-Z]+)\]|(DEBUG|INFO|WARNING|ERROR|CRITICAL):)\s*(.*)$/);
    if (proxyPrefix) {
      source = proxyPrefix[1]?.trim() || source;
      status = proxyPrefix[2] || "";
      detail = (proxyPrefix[3] ?? "").trim();
    } else if (levelPrefix) {
      status = levelPrefix[1] || levelPrefix[2] || "";
      detail = (levelPrefix[3] ?? "").trim();
      const process = detail.match(/\bprocess \[(\d+)\]/i);
      if (process?.[1]) source = `PID ${process[1]}`;
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
      detail = fields.join(" · ");
    }
  }
  const action = tab === "menu" ? detail.split(/[:;,]/, 1)[0]?.trim() ?? "" : "";
  return { key: `${tab}:${index}:${record}`, time, source, status, model, upstreamModel: "", provider: "", apiKeyName: "", event: "", action, duration: "", tokens, detail, original: record };
}

function renderLogRecord(record: unknown, tab: LogTab, index: number, translate: Translate): RenderedLogRecord {
  if (typeof record === "string") return parseTextLogRecord(record, tab, index, translate);
  const value = asRecord(record);
  const time = shortLogTimestamp(value.ts ?? value.timestamp ?? value.time ?? value.created_at ?? value.updated_at ?? value.checked_at);
  const provider = compactLogValue(value.provider);
  const apiKeyName = compactLogValue(value.api_key_name);
  const publicModel = compactLogValue(value.public_model ?? value.model_group ?? value.model);
  const upstreamModel = compactLogValue(value.upstream_model);
  const model = publicModel || upstreamModel;
  const source = compactLogValue(value.source ?? value.route_key) || provider || logTitle(tab, translate);
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
  const used = new Set(["ts", "timestamp", "time", "created_at", "updated_at", "checked_at", "source", "provider", "api_key_name", "model_group", "public_model", "route_key", "status", "result", "detail", "message", "event", "action", "error", "upstream_model", "model", "duration_ms", "usage", "total_tokens"]);
  for (const [key, item] of Object.entries(value)) {
    if (used.has(key)) continue;
    const display = compactLogValue(item);
    if (display) details.push(`${key}: ${display}`);
  }
  const recoveryFallback = tab === "recovery" ? translate("common.notAvailable") : "";
  return {
    key: `${tab}:${index}:${time}:${source}:${status}:${details.join(" ")}`,
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
    detail: details.filter(Boolean).join(" · "),
    original: safeOriginalLogRecord(record),
  };
}

function logColumns(tab: LogTab, translate: Translate): LogColumn[] {
  const time = { label: translate("logs.localTime"), width: 164, value: (row: RenderedLogRecord) => row.time };
  const status = { label: translate("common.status"), width: 58, value: (row: RenderedLogRecord) => row.status };
  const detail = { label: translate("logs.detail"), width: 230, value: (row: RenderedLogRecord) => row.detail };
  if (tab === "requests") return [
    time,
    { label: translate("providers.publicModel"), width: 126, value: (row) => row.model },
    { label: translate("providers.upstream"), width: 126, value: (row) => row.upstreamModel },
    { label: translate("common.provider"), width: 70, value: (row) => row.provider },
    { label: translate("logs.apiKeyName"), width: 90, value: (row) => row.apiKeyName },
    status,
    { label: translate("logs.duration"), width: 68, value: (row) => row.duration },
    { label: translate("logs.tokenCount"), width: 54, value: (row) => row.tokens },
    detail,
  ];
  if (tab === "route-trace") return [
    { ...time, width: 154 },
    { label: translate("logs.event"), width: 142, value: (row) => row.event },
    { label: translate("providers.publicModel"), width: 130, value: (row) => row.model },
    { label: translate("providers.upstream"), width: 130, value: (row) => row.upstreamModel },
    { label: translate("common.provider"), width: 104, value: (row) => row.provider },
    { ...detail, width: 192 },
  ];
  if (tab === "menu") return [
    time,
    { label: translate("logs.action"), width: 112, value: (row) => row.action },
    status,
  ];
  if (tab === "recovery") return [
    time,
    { label: translate("providers.publicModel"), width: 126, value: (row) => row.model },
    { label: translate("providers.upstream"), width: 126, value: (row) => row.upstreamModel },
    { label: translate("common.provider"), width: 104, value: (row) => row.provider },
    { label: translate("logs.apiKeyName"), width: 100, value: (row) => row.apiKeyName },
    { ...status, width: 76 },
    { ...detail, width: 260 },
  ];
  if (tab === "online-usage") return [
    time,
    { label: translate("logs.source"), width: 126, value: (row) => row.source },
    { label: translate("logs.tokenCount"), width: 82, value: (row) => row.tokens },
    { ...detail, width: 446 },
  ];
  return [
    time,
    { label: translate("logs.source"), width: 116, value: (row) => row.source },
    status,
    { ...detail, width: 500 },
  ];
}

function LogsWorkspace({ snapshot, ipc, native, busy, translate, dispatch, requestedTab }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; requestedTab?: typeof LOG_TABS[number] }): React.JSX.Element {
  const [selected, setSelected] = useState<typeof LOG_TABS[number]>("requests");
  const [active, setActive] = useState<LogView>();
  const [filterDraft, setFilterDraft] = useState("");
  const filterTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const tabsRef = useRef<HostInstance | null>(null);
  useEffect(() => { if (requestedTab) setSelected(requestedTab); }, [requestedTab]);
  useEffect(() => { setFilterDraft(active?.filter ?? ""); }, [active?.filter, selected]);
  useEffect(() => () => { if (filterTimer.current) clearTimeout(filterTimer.current); }, []);
  useEffect(() => {
    let mounted = true;
    let polling = false;
    let viewRevision: number | undefined;
    setActive(undefined);
    const poll = async (): Promise<void> => {
      if (polling) return;
      polling = true;
      try {
        const result = await ipc.logs(selected, viewRevision);
        if (!mounted) return;
        viewRevision = result.revision;
        if (result.changed && result.log) setActive(result.log);
      } catch {
        // Keep the last visible rows through a transient Core failure.
      } finally {
        polling = false;
      }
    };
    void poll();
    const interval = setInterval(() => { void poll(); }, selected === "online-usage" ? 8000 : 2000);
    return () => { mounted = false; clearInterval(interval); };
  }, [ipc, selected]);
  const rows = (active?.records ?? []).map((record, index) => renderLogRecord(record, selected, index, translate));
  const columns = logColumns(selected, translate);
  const tabOptions = LOG_TABS.map((tab) => ({ id: tab, title: logTitle(tab, translate) }));
  const lineCount = active?.line_count ?? rows.length;
  const statusParts = [
    translate(active && lineCount >= active.limit ? "logs.latestLinesAtLimit" : "common.lines", { count: lineCount }),
    active?.paused ? translate("logs.paused") : "",
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
        rememberPassword: value.remember_password === true,
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
            rememberPassword: account.rememberPassword,
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
        items: accounts.map((account) => `${account.label} · ${account.type === "newapi" ? "NewAPI" : "Sub2API"}`),
        anchor: { x, y, width, height },
      }).then((index) => { if (index !== undefined) open(index); });
    });
  };
  return <View style={styles.logsWindow}>
    <View style={styles.logsToolbar}>
      <View style={styles.logFilterRow}><Text style={styles.toolbarLabel}>{translate("common.filter")}</Text><NativeTextField style={styles.logFilterInput} value={filterDraft} placeholder={translate("logs.filterCurrent")} onChangeText={(filter) => { setFilterDraft(filter); if (filterTimer.current) clearTimeout(filterTimer.current); filterTimer.current = setTimeout(() => { void dispatch("logs.set_filter", { tab: selected, filter }, "logs"); }, 250); }} accessibilityLabel={translate("common.filter")} /></View>
      <View style={styles.logToolbarSpacer} />
      <View style={styles.logActionsRow}><ActionButton title={active?.paused ? translate("common.resume") : translate("common.pause")} disabled={busy} onPress={() => dispatch(active?.paused ? "logs.resume" : "logs.pause", { tab: selected }, "logs")} /><ActionButton title={translate("common.clearView")} disabled={busy} onPress={() => dispatch("logs.clear", { tab: selected }, "logs")} /><ActionButton title={translate("common.refresh")} disabled={busy} onPress={() => dispatch("logs.refresh", { tab: selected }, "logs")} /></View>
    </View>
    <WindowTabs nativeRef={tabsRef} values={tabOptions} selected={selected} disabled={busy} onSelect={(tab) => {
      setSelected(tab as LogTab);
      if (tab === "online-usage") openRelayUsageLogs();
    }} style={styles.logsTabs} />
    {rows.length > 0 ? <NativeTable columns={columns.map(({ label, width }) => ({ label, width }))} rows={rows.map((row) => ({ key: row.key, cells: columns.map((column) => column.value(row)) }))} compact followBottom onRowDoublePress={(_key, index) => {
      const row = rows[index];
      if (!row) return;
      void native.showReadOnlyText({ title: translate("logs.originalRecord"), text: row.original, closeLabel: translate("menu.close") });
    }} style={styles.logTable} /> : <View style={styles.logEmptySurface}><Text style={styles.logEmptyText}>{active ? translate("logs.empty") : translate("logs.loading")}</Text></View>}
    <View style={styles.logInfoBar}><Text numberOfLines={1} style={styles.cardHint}>{statusParts.join(" · ")}</Text></View>
  </View>;
}

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }): React.JSX.Element {
  return <View style={styles.section}><View style={styles.sectionHeader}><Text style={styles.sectionTitle}>{title}</Text>{action}</View>{children}</View>;
}

function EmptyState({ translate }: { translate: Translate }): React.JSX.Element { return <Text style={styles.empty}>{translate("screen.noData")}</Text>; }

const ActionButton = React.forwardRef<HostInstance, { title: string; onPress: () => void; disabled?: boolean; primary?: boolean; danger?: boolean; style?: StyleProp<ViewStyle> }>(function ActionButton({ title, onPress, disabled, primary, danger, style }, ref): React.JSX.Element {
  return <NativeButton ref={ref} title={title} disabled={disabled} primary={primary} destructive={danger} onPress={onPress} style={style} />;
});

function TextField({ label, value, onCommit, hint, secret, multiline, compactMultiline, keyboardType, stacked, labelWidth, labelAlign, controlWidth, suffix }: { label: string; value: string; onCommit: (value: string) => void | Promise<void>; hint?: string; secret?: boolean; multiline?: boolean; compactMultiline?: boolean; keyboardType?: "default" | "numeric"; stacked?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; suffix?: string }): React.JSX.Element {
  const field = usePendingTextField(value, onCommit, label);
  return <View style={[styles.formRow, (stacked || multiline) && styles.formRowStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }, (stacked || multiline) && styles.formRowLabelStacked]}>{label}</Text><View style={[styles.formRowControl, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]}><NativeTextField style={[styles.input, multiline && styles.textArea, compactMultiline && styles.compactTextArea]} value={field.draft} onChangeText={field.onChangeText} onBlur={() => { void field.commit().catch(() => undefined); }} onSubmitEditing={multiline ? undefined : () => { void field.commit().catch(() => undefined); }} multiline={multiline} secureTextEntry={secret} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />{hint ? <Text style={styles.fieldHint}>{hint}</Text> : null}</View>{suffix ? <Text style={styles.fieldHint}>{suffix}</Text> : null}</View>;
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
  return <View style={styles.nativeSecretControl}><NativeSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={hint ?? ""} plainText={plainText} autoCommit={autoCommit} disabled={busy} commitRequest={commitRequest} resetRequest={resetRequest + resetToken} onSecretState={(state) => {
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
  }} style={[styles.nativeSecretInput, inputMinWidth === undefined ? null : { minWidth: inputMinWidth }]} />{!autoCommit && !setBelow && setTitle ? <NativeButton title={setTitle} compact disabled={busy || status === "saving"} onPress={requestCommit} style={styles.nativeSecretSetButton} /> : null}</View>;
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
  return <View style={[styles.formRow, actionsBelow && styles.formRowSecretStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><View style={styles.formRowControl}>{actionsBelow ? <><NativeSecretInputControl label={label} hint={hint} busy={busy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} setBelow onSetReady={handleSetReady} inputMinWidth={110} /><View style={styles.secretFieldButtons}>{!autoCommit && setTitle ? <NativeButton title={setTitle} compact disabled={busy || saving} onPress={() => setAction.current()} style={styles.secretFieldButton} /> : null}{onClear && clearTitle ? <NativeButton title={clearTitle} compact disabled={clearDisabled ?? busy} onPress={handleClear} style={styles.secretFieldButton} /> : null}</View></> : <View style={styles.secretFieldActions}><NativeSecretInputControl label={label} hint={hint} busy={busy} domain={domain} field={field} target={target} plainText={plainText} autoCommit={autoCommit} resetToken={resetToken} onSecretState={onSecretState} setTitle={setTitle} />{onClear && clearTitle ? <ActionButton title={clearTitle} disabled={clearDisabled ?? busy} onPress={handleClear} /> : null}</View>}</View></View>;
}

function ToggleRow({ label, value, onChange, disabled }: { label: string; value: boolean; onChange: (value: boolean) => void; disabled?: boolean }): React.JSX.Element {
  return <View style={styles.toggleRow}><View style={styles.toggleControl}><NativeCheckbox label={label} value={value} onValueChange={onChange} disabled={disabled} style={styles.toggleNativeControl} /></View></View>;
}

function SegmentedField({ label, value, values, onSelect, disabled }: { label: string; value: string; values: Array<string | { value: string; label: string }>; onSelect: (value: string) => void; disabled?: boolean }): React.JSX.Element {
  const translate = useContext(TranslationContext);
  const options = translate ? assistantSettingOptions(values, translate) : values.map((option) => typeof option === "string" ? { value: option, label: option } : option);
  const selectedValue = options.find((option) => option.value === value)?.label ?? options[0]?.label ?? "";
  return <View style={styles.formRow}><Text style={styles.formRowLabel}>{label}</Text><NativeSegmentedControl labels={options.map((option) => option.label)} selectedValue={selectedValue} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={styles.formRowControl} /></View>;
}

function PickerField({ label, value, values, onSelect, disabled, labelWidth, labelAlign, controlWidth, translate }: { label: string; value: string; values: Array<string | AssistantSettingOption>; onSelect: (value: string) => void; disabled?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; translate?: Translate }): React.JSX.Element {
  const contextualTranslate = useContext(TranslationContext);
  const optionTranslator = translate ?? contextualTranslate;
  const options = optionTranslator ? assistantSettingOptions(values, optionTranslator) : values.map((option) => typeof option === "string" ? { value: option, label: option } : option);
  const selectedLabel = options.find((option) => option.value === value)?.label ?? options[0]?.label ?? "";
  return <View style={styles.formRow}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><NativePicker labels={options.map((option) => option.label)} selectedValue={selectedLabel} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={[styles.picker, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]} /></View>;
}

function RawEditor({ label, domain, document, language, ipc, busy, translate, showReload = true, codexPane = false, reloadToken = 0, style }: { label: string; domain: "codex" | "claude"; document: "config" | "auth" | "settings"; language: "toml" | "json"; ipc: IpcClient; busy: boolean; translate: Translate; showReload?: boolean; codexPane?: boolean; reloadToken?: number; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const [editorToken, setEditorToken] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [nativeStatus, setNativeStatus] = useState<string>();
  const [nativeErrorCode, setNativeErrorCode] = useState<string>();
  const [staged, setStaged] = useState(false);
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
    setStaged(false);
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
  return <View style={[styles.rawEditor, codexPane && styles.codexRawEditorBase, style]}><View style={[styles.rawEditorHeader, codexPane && styles.codexRawEditorHeader]}><Text style={[styles.fieldLabel, codexPane && styles.codexRawEditorLabel]}>{label}</Text>{showReload ? <ActionButton title={translate("menu.reload")} disabled={reloadDisabled} onPress={reloadEditor} /> : null}</View>{codexPane ? null : <Text style={styles.fieldHint}>{translate("settings.rawProtectedHint")}</Text>}{editorToken ? <View style={styles.rawNativeEditorFrame}><NativeSecureTextEditor editorToken={editorToken} language={language} unavailableLabel={translate("common.secureEditorUnavailable")} style={[styles.rawNativeEditor, codexPane && styles.codexRawNativeEditor]} onEditorState={({ status, error: nextNativeErrorCode }) => { nativeStatusRef.current = status; setNativeStatus(status); setStaged(status === "saved"); setNativeErrorCode(nextNativeErrorCode || undefined); const pending = pendingCommit.current; if (status === "dirty" || status === "saving") registry?.setDirty(fieldId.current, true); else { registry?.setDirty(fieldId.current, false); if (pending) { pendingCommit.current = undefined; if (status === "error") pending.reject(new Error(nextNativeErrorCode || "Raw editor could not be staged")); else pending.resolve(); } } if (!nextNativeErrorCode) { setError(undefined); return; } setError(nextNativeErrorCode === "stage_failed" ? translate("common.secureEditorStageFailed") : nextNativeErrorCode === "invalid_text" ? translate("common.invalidText") : translate("common.secureEditorReadFailed")); }} />{nativeLoading ? <View pointerEvents="none" style={styles.rawEditorOverlay}><Text style={styles.cardHint}>{translate("common.secureEditorLoading")}</Text></View> : null}{nativeReadFailed ? <View style={styles.rawEditorOverlay}><Text style={styles.error}>{error ?? translate("common.secureEditorReadFailed")}</Text><ActionButton title={translate("menu.reload")} disabled={busy} onPress={reloadEditor} /></View> : null}</View> : <View style={[styles.rawEditorLoading, codexPane && styles.codexRawEditorLoading]}><Text style={styles.cardHint}>{loading ? translate("common.loading") : translate("error.coreUnavailable")}</Text></View>}{staged ? <Text style={styles.result}>{translate("common.staged")}</Text> : null}{error && editorToken && !nativeReadFailed ? <Text style={styles.error}>{error}</Text> : null}</View>;
}

function InfoPair({ label, value, toolTip }: { label: string; value: string; toolTip?: string }): React.JSX.Element { return <View style={styles.infoPair}><Text style={styles.fieldLabel}>{label}</Text><Text style={styles.cardHint} accessibilityHint={toolTip}>{value}</Text></View>; }

function billingMultiplierValue(value: unknown, translate: Translate): string {
  const multiplier = asRecord(value);
  const number = numberValue(multiplier.value, Number.NaN);
  return stringValue(multiplier.status) === "ok" && Number.isFinite(number) ? `${number.toFixed(2)}x` : translate("providers.billingUnavailable");
}

function modelProbePresentation(model: UnknownRecord, result: IpcResults["probe"] | undefined, translate: Translate): { compact: string; full: string } {
  const resultRecord = result as UnknownRecord | undefined;
  const probe = resultRecord ?? asRecord(model.probe);
  if (Object.keys(probe).length === 0) {
    const text = translate("providers.probeNotRun");
    return { compact: text, full: text };
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
    ? translate("providers.probeSummaryAvailable", { surfaces: availableSurfaces.join("、") })
    : translate("providers.probeSummaryUnavailable");
  const summaryRecord = asRecord(probe.summary);
  const statuses = Object.entries(asRecord(summaryRecord.statuses))
    .map(([surface, status]) => `${probeSurfaceLabel(surface, translate)}: ${stringValue(status, "unavailable")}`)
    .join("；");
  const summary = [availabilitySummary, statuses].filter(Boolean).join("；");
  const requests = surfaces.map((surface) => ({
    surface: surface.surface,
    status: surface.status ?? "unavailable",
    original_request: surface.original_request ?? {},
  }));
  const compactRequest = requests.length > 0
    ? requests.map((item) => `${item.surface}: ${stringValue(asRecord(item.original_request).url) || translate("common.none")}`).join("；")
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
function apiKeyDisplayName(value: unknown, translate: Translate): string {
  const name = stringValue(value);
  if (!name) return translate("common.notAvailable");
  return name === "default" ? translate("providers.defaultKey") : name;
}
function emptyToNull(value: string, translate?: Translate): string | null { return value === "(Empty)" || value === translate?.("common.empty") ? null : value; }
function uniqueKeyName(existing: string[]): string { let suffix = 1; let value = `key-${suffix}`; while (existing.includes(value)) { suffix += 1; value = `key-${suffix}`; } return value; }
function splitLines(value: string): string[] { return value.split("\n").map((item) => item.trim()).filter(Boolean); }
function splitCommaLines(value: string): string[] { return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean); }
function numericOrText(value: string): number | string { const parsed = Number(value); return value.trim() !== "" && Number.isFinite(parsed) ? parsed : value.trim(); }
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
  red: semanticColor("systemRedColor", undefined, "#b00020"),
  green: semanticColor("systemGreenColor", undefined, "#2f6b3d"),
  brown: semanticColor("systemBrownColor", undefined, "#6f5500"),
} as const;

const styles = StyleSheet.create({
  root: { flex: 1, minWidth: 420, backgroundColor: systemColors.window },
  menuBarHost: { flex: 1 }, error: { margin: 20, color: systemColors.red, fontSize: 13 },
  windowSurface: { flex: 1, backgroundColor: systemColors.window }, windowContent: { flexGrow: 1, paddingHorizontal: 16, paddingTop: 16, paddingBottom: 8, gap: 12 }, windowContentFixed: { flex: 1, minHeight: 0 }, providersContent: { paddingBottom: 8, gap: 8 }, settingsContent: { paddingHorizontal: 20, paddingTop: 10, paddingBottom: 0, gap: 8 }, logsContent: { paddingHorizontal: 12, paddingTop: 10, paddingBottom: 0 }, runtimeContent: { paddingHorizontal: 20, paddingTop: 14, paddingBottom: 0 }, webDavContent: { paddingHorizontal: 20, paddingTop: 14, paddingBottom: 0 }, windowTitleBlock: { paddingHorizontal: 20, paddingTop: 16, paddingBottom: 4, gap: 4 }, windowTitle: { color: systemColors.label, fontSize: 16, fontWeight: "600" }, validationText: { color: systemColors.red, fontSize: 12 },
  footer: { height: 60, minHeight: 60, flexShrink: 0, flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingVertical: 12, gap: 8 }, footerStatus: { color: systemColors.secondaryLabel, fontSize: 12, flexShrink: 1 }, footerSpacer: { flex: 1 }, footerButtons: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, wideButton: { minWidth: 92 },
  providerToolbar: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, toolbarSpacer: { flex: 1 }, windowTabs: { width: 224, height: 28 }, settingsTabBar: { minHeight: 42, justifyContent: "center", borderBottomWidth: 1, borderBottomColor: systemColors.separator }, settingsTabs: { alignSelf: "flex-start", width: 250 }, windowTab: {}, windowTabSelected: {}, windowTabText: {},
  routeWorkspaceWithInspector: { flexDirection: "row", gap: 12 }, routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 },
  providersLayout: { flex: 1, minHeight: 0, gap: 8 }, providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 12 }, providerLeftColumn: { flex: 1, minWidth: 0 }, providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: 12 }, routeWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }, importSourcePicker: { width: 152, height: 26 }, fetchKeyPicker: { width: 190, height: 26, marginRight: 8, flexShrink: 0 }, providerThreePane: { flex: 1, minHeight: 0 }, providerListPane: { width: 190, minWidth: 190, maxWidth: 190, flexGrow: 0, flexShrink: 0 }, modelListPane: { flex: 1, minWidth: 464 }, providerInspectorPane: { minWidth: 340 }, tablePane: { flex: 1, minWidth: 0, gap: 8 }, tablePaneWide: { flex: 1, minWidth: 0 }, tableTitleRow: { height: 28, flexDirection: "row", alignItems: "center" }, tableTitle: { color: systemColors.label, fontSize: 13, fontWeight: "600" }, tableActions: { marginLeft: "auto", flexDirection: "row", gap: 8 }, iconButton: { minWidth: 24, width: 24, minHeight: 24, height: 24, alignItems: "center", justifyContent: "center" }, iconButtonText: { color: systemColors.secondaryLabel, fontSize: 17 }, tableHeader: { height: 28, flexDirection: "row", alignItems: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.window }, tableHeaderText: { color: systemColors.label, fontSize: 12, paddingHorizontal: 8, fontWeight: "500" }, tableScroll: { flex: 1, minHeight: 0, borderWidth: 1, borderTopWidth: 0, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, tableRows: { flexGrow: 1 }, tableRow: { minHeight: 28, flexDirection: "row", alignItems: "center" }, tableRowSelected: { backgroundColor: systemColors.control }, tableCellText: { color: systemColors.label, fontSize: 13, paddingHorizontal: 8 }, providerNameColumn: { flex: 1 }, countColumn: { width: 48, textAlign: "right" }, modelNameColumn: { width: 118 }, modelUpstreamColumn: { flex: 1, minWidth: 120 }, modelBillingColumn: { width: 112 }, routeModelColumn: { width: 170 }, routeOrderColumn: { width: 56, textAlign: "right" }, routeProviderColumn: { width: 130 }, routeUpstreamColumn: { flex: 1, minWidth: 164 }, tableBottomRow: { minHeight: 30, flexDirection: "row", alignItems: "center" }, nativeProviderTable: { flex: 1, minHeight: 0 }, nativeModelTable: { flex: 1, minHeight: 0 }, nativeRouteTable: { flex: 1, minHeight: 0 }, providerInspector: { width: 340, minWidth: 340, maxWidth: 340, flexGrow: 0, flexShrink: 0 }, providerEditorContent: { flex: 1, minHeight: 0, paddingTop: 4, paddingHorizontal: 14, paddingRight: 10, paddingBottom: 16, gap: 10 }, providerEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, providerEditorHeading: { flex: 1, color: systemColors.secondaryLabel, fontSize: 13, fontWeight: "600" }, providerReturnToModel: { flexShrink: 1 }, providerEditorSection: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 4, gap: 10 }, providerEnabledRow: { minHeight: 24, flexDirection: "row", alignItems: "center" }, inspectorContent: { paddingTop: 4, paddingHorizontal: 14, paddingRight: 6, paddingBottom: 16, gap: 10 }, inspectorBody: { gap: 10 }, modelBreadcrumb: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 5 }, breadcrumbProvider: { flexShrink: 1, color: systemColors.label, fontSize: 13, fontWeight: "600" }, breadcrumbSeparator: { color: systemColors.secondaryLabel, fontSize: 13 }, inspectorHeading: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: 13 }, inspectorDivider: { height: 1, backgroundColor: systemColors.separator }, inspectorEnabledRow: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, inspectorEnableControl: { flexShrink: 0 }, probeSummary: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: 12 }, billingSummaryText: { color: systemColors.secondaryLabel, fontSize: 12, paddingVertical: 4 }, protocolField: { gap: 4 }, protocolFieldLabel: { width: 96, color: systemColors.label, fontSize: 12 }, protocolRows: { gap: 4 }, protocolRank: { width: 20, textAlign: "right", color: systemColors.secondaryLabel, fontSize: 12 }, protocolCheckbox: { flex: 1, minWidth: 112 }, providerKeysEditor: { gap: 7 }, providerKeysHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8 }, providerKeysHeading: { flex: 1, color: systemColors.label, fontSize: 12, fontWeight: "600" }, providerKeyGrid: { flex: 1, minHeight: 142, flexDirection: "row", alignItems: "flex-start", gap: 12 }, providerKeyTable: { width: 138, minWidth: 138, maxWidth: 138, height: 142, minHeight: 142, flexShrink: 0 }, providerKeyFields: { flex: 1, minWidth: 0, gap: 8 }, providerKeyActions: { flexDirection: "row", gap: 6, flexShrink: 0 },
  codexWorkspace: { flex: 1, minHeight: 0 }, codexWorkspaceFrame: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexValidationStatus: { flexShrink: 0, marginHorizontal: 8, fontSize: 12 }, settingsMissingMessage: { flexShrink: 0, marginHorizontal: 8, color: systemColors.secondaryLabel, fontSize: 12 }, codexValidationWarning: { color: systemColors.brown }, codexValidationError: { color: systemColors.red }, codexSplit: { flex: 1, minWidth: 0, minHeight: 0 }, codexStructuredPane: { flex: 1, minWidth: 360, paddingHorizontal: 8 }, codexStructuredScroll: { flex: 1, minWidth: 0, marginTop: 7 }, codexStructured: { flexGrow: 1, gap: 14, paddingHorizontal: 16, paddingTop: 10, paddingBottom: 16 }, codexRawPane: { flex: 1, flexShrink: 1, minWidth: 320, minHeight: 0, gap: 8, paddingHorizontal: 8, overflow: "hidden" }, codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexRawEditorBase: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0, gap: 5 }, codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 0 }, codexRawEditorHeader: { minHeight: 18 }, codexRawEditorLabel: { fontFamily: Platform.select({ macos: "Menlo", windows: "Cascadia Mono", default: "monospace" }), fontWeight: "600" }, codexRawNativeEditor: { minHeight: 0 }, codexRawEditorLoading: { minHeight: 0 }, paneHeading: { color: systemColors.label, fontSize: 14, fontWeight: "600" }, section: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 10, gap: 8 }, sectionHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }, sectionTitle: { color: systemColors.label, fontSize: 13, fontWeight: "600" }, split: { flexDirection: "row", flexWrap: "wrap", borderWidth: 1, borderColor: systemColors.separator, minHeight: 150, backgroundColor: systemColors.textBackground }, codexListTable: { flex: 1, minWidth: 260, minHeight: 150 }, listToolRail: { width: 32, paddingTop: 8, alignItems: "center", gap: 5, borderLeftWidth: 1, borderLeftColor: systemColors.separator }, pluginEditor: { minHeight: 128, flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", gap: 12 }, pluginTable: { flex: 1, minWidth: 260, minHeight: 128 }, pluginFields: { flex: 1, minWidth: 220, gap: 7 }, masterPane: { width: "36%", minWidth: 220, borderRightWidth: 1, borderColor: systemColors.separator, padding: 8 }, detailPane: { flex: 1, minWidth: 240, padding: 12 }, listRow: { minHeight: 28, paddingHorizontal: 8, paddingVertical: 5 }, listRowSelected: { backgroundColor: systemColors.control }, listText: { flex: 1 },
  runtimeWorkspaceFrame: { flex: 1, minHeight: 0, gap: 8 }, runtimeFileToolbar: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 8 }, runtimeWorkspace: { padding: 14, gap: 12 }, runtimeScrollSurface: { flex: 1, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, runtimeTwoColumnForm: { flexDirection: "row", flexWrap: "wrap", columnGap: 20, rowGap: 8 }, runtimeOneColumnForm: { flexDirection: "column", flexWrap: "nowrap" }, runtimeField: { minWidth: 486, flexGrow: 1, flexBasis: 486, gap: 4 }, runtimeInputRow: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, runtimeFieldLabel: { width: 142, flexShrink: 0, color: systemColors.label, fontSize: 12, textAlign: "right" }, runtimeValueSlot: { width: 180, height: 26, flexShrink: 0, justifyContent: "center" }, runtimeValueControl: { width: 180, minWidth: 180, height: 26 }, runtimeBooleanSlot: { flex: 1, minWidth: 0, minHeight: 26, justifyContent: "center" }, runtimeBooleanControl: { alignSelf: "flex-start", minHeight: 26 }, runtimeUnit: { width: 60, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: 12 }, runtimeActionSlot: { width: 72, minHeight: 26, flexShrink: 0, justifyContent: "center" }, runtimeHelpSlot: { marginLeft: 150, paddingTop: 4 }, runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: 11, lineHeight: 15 },
  webDavForm: { flex: 1, gap: 14, paddingTop: 0 }, webdavStateRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 12, paddingBottom: 4, borderBottomWidth: 1, borderBottomColor: systemColors.separator }, webdavEnabledControl: { width: 190, flexGrow: 0, flexShrink: 0 }, webdavInlineStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: 12 }, webdavFormRows: { gap: 10 }, webdavWideControl: { flex: 1, minWidth: 0 }, webdavPasswordInput: { width: "100%", minHeight: 30 }, webdavFooterLeading: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 10, flex: 1, minWidth: 0 }, webdavProbeStatus: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: 12 },
  relayAccountsContent: { paddingHorizontal: 12, paddingTop: 10, paddingBottom: 12 }, logsWindow: { flex: 1, minHeight: 0, gap: 4 }, logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, logFilterRow: { width: 360, minWidth: 220, maxWidth: 360, height: 26, flexDirection: "row", alignItems: "center", gap: 8 }, logToolbarSpacer: { flex: 1, minWidth: 0 }, logActionsRow: { height: 26, flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8 }, toolbarLabel: { color: systemColors.label, fontSize: 12, flexShrink: 0 }, logFilterInput: { flex: 1, minWidth: 0, height: 26 }, logsTabs: { width: "100%", minWidth: 0, height: 28, flexShrink: 0, marginTop: 0, marginBottom: 0 }, logTable: { flex: 1, minHeight: 0 }, logEmptySurface: { flex: 1, minHeight: 0, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, logEmptyText: { color: systemColors.secondaryLabel, fontSize: 13, textAlign: "center", paddingHorizontal: 20 }, logInfoBar: { height: 21, minHeight: 21, flexShrink: 0, borderTopWidth: 1, borderColor: systemColors.separator, justifyContent: "center", paddingHorizontal: 4 },
  form: { gap: 7 }, structuredForm: { gap: 7 }, field: { gap: 5, minWidth: 220, flexGrow: 1, flexBasis: 300 }, fieldLabel: { color: systemColors.label, fontSize: 12, fontWeight: "500" }, fieldHint: { color: systemColors.secondaryLabel, fontSize: 11 }, input: { width: "100%", minHeight: 30, paddingHorizontal: 7, paddingVertical: 4, color: systemColors.label, fontSize: 13 }, textArea: { minHeight: 108, textAlignVertical: "top", fontFamily: "Menlo" }, compactTextArea: { minHeight: 56, maxHeight: 56 }, inputWithAction: { flexDirection: "row", alignItems: "center", gap: 6 }, inputFlex: { flex: 1 }, toggleRow: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 12 }, toggleControl: { flex: 1, minWidth: 0, minHeight: 22, justifyContent: "center" }, toggleNativeControl: { width: "100%", minWidth: 220, minHeight: 22 }, actions: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 4 }, secretFieldActions: { flexDirection: "row", alignItems: "center", gap: 8 }, secretFieldButtons: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 }, secretFieldButton: { flex: 1, minWidth: 0, height: 26 }, nativeSecretControl: { flex: 1, minWidth: 0, minHeight: 30, flexDirection: "row", alignItems: "center", gap: 6 }, nativeSecretInput: { flex: 1, minWidth: 86, minHeight: 30 }, nativeSecretSetButton: { minWidth: 42, height: 26 }, action: {}, actionPrimary: {}, actionDanger: {}, actionDisabled: {}, actionText: { color: systemColors.label, fontSize: 12, fontWeight: "500" }, actionTextPrimary: {}, actionTextDanger: {}, tabStrip: { flexDirection: "row", flexWrap: "wrap", gap: 7 }, tab: {}, tabSelected: {}, inlineMeta: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 8 }, rawEditor: { flex: 1, minHeight: 180, gap: 4 }, rawEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, rawNativeEditorFrame: { flex: 1, minHeight: 160, position: "relative" }, rawNativeEditor: { flex: 1, minHeight: 160 }, rawEditorOverlay: { position: "absolute", left: 0, right: 0, top: 0, bottom: 0, justifyContent: "center", alignItems: "center", gap: 8, paddingHorizontal: 12, backgroundColor: systemColors.textBackground }, rawEditorLoading: { flex: 1, minHeight: 160, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, infoPair: { gap: 2, minWidth: 160 }, rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }, logRecords: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, maxHeight: 360, overflow: "scroll", padding: 10, gap: 6 }, logRecord: { color: systemColors.label, fontFamily: "Menlo", fontSize: 12 }, empty: { color: systemColors.secondaryLabel, fontSize: 13, paddingVertical: 12 }, result: { color: systemColors.green, fontSize: 12 }, warning: { color: systemColors.brown, fontSize: 12, backgroundColor: systemColors.control, padding: 8, borderRadius: 4 }, issueBox: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.control, padding: 12, gap: 5 }, issue: { color: systemColors.red, fontSize: 12 }, cardTitle: { color: systemColors.label, fontSize: 13, fontWeight: "500" }, cardHint: { color: systemColors.secondaryLabel, fontSize: 12, marginTop: 2 },
  formRow: { width: "100%", minHeight: 30, flexDirection: "row", alignItems: "center", gap: 12 }, formRowStacked: { alignItems: "flex-start" }, formRowSecretStacked: { alignItems: "flex-start" }, formRowLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: 12, textAlign: "right" }, formRowLabelStacked: { paddingTop: 6 }, formRowControl: { flex: 1, minWidth: 0, gap: 3 }, picker: { flex: 1, minWidth: 180, height: 26 }, protocolRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 3 },
});
