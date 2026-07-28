import React, { createContext, useCallback, useEffect, useMemo, useRef, useState, useContext } from "react";
import { Platform, PlatformColor, ScrollView, StyleSheet, Text, View, type StyleProp, type TextStyle, type ViewStyle } from "react-native";
import { createTranslator } from "../i18n";
import { LOG_TABS, ROUTES } from "../routes";
import { NativeButton, NativeCheckbox, NativePicker, NativeSecureTextEditor, NativeSecureTextInput, NativeSegmentedControl, NativeSplitView, NativeTable, NativeTextEditor, NativeTextField } from "./NativeControls";
import type {
  AppRoute,
  ConfigDomain,
  CoreSnapshot,
  IpcClient,
  IpcResults,
  LogTab,
  NativeLeafAdapter,
  ProviderSummary,
  ServiceStatus,
  ValidationSummary,
} from "../types";

type Translate = (key: string, values?: Record<string, string | number>) => string;
type UnknownRecord = Record<string, unknown>;
type PackageImportResult = IpcResults["import"];
type Dispatch = (type: string, payload?: UnknownRecord, domain?: ConfigDomain) => Promise<void>;
type NativeSecretClear = (options: {
  domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav";
  field: string;
  target?: string;
}) => Promise<void>;
type SecretState = { revision: number; present: boolean; status: string; error: string; commitRequest: number };
type PendingField = { commit: () => void | Promise<void>; reset: () => void };
type PendingFieldRegistry = { register: (id: symbol, field?: PendingField) => void };
type ServiceOperation = "start" | "stop" | "restart" | "reload" | "health";

const PendingFieldContext = createContext<PendingFieldRegistry | undefined>(undefined);
const SERVICE_HEALTH_POLL_MS = 10_000;
const SERVICE_RECOVERY_RETRY_MS = 15_000;

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
  logTabRequest?: LogTab;
  nativeAction?: { id: string; sequence: number };
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

function packageSections(): Array<"providers_models" | "runtime"> {
  return ["providers_models", "runtime"];
}

function statusLabel(status: ServiceStatus, translate: Translate): string {
  return translate(`service.${status.state}`);
}

function logTitle(tab: string, translate: Translate): string {
  return translate(`logs.${tab.replace(/-/g, "_")}`);
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

export function LiteLLMMenuApp({ ipc, native, translate: hostTranslate, routeRequest, logTabRequest, nativeAction }: LiteLLMMenuAppProps): React.JSX.Element {
  const [route, setRoute] = useState<AppRoute>("home");
  const [snapshot, setSnapshot] = useState<CoreSnapshot | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [serviceOperationPendingCount, setServiceOperationPendingCount] = useState(0);
  const serviceOperationPending = serviceOperationPendingCount > 0;
  const translate = useMemo<Translate>(() => !snapshot || snapshot.language === "system" ? hostTranslate : createTranslator(snapshot.language), [hostTranslate, snapshot]);
  const handledNativeActions = useRef(new Set<string>());
  // The desktop host owns a service while it is open, except after an
  // explicit Stop.  Keep this intent separate from the current controller
  // state so a stopped service is not immediately resurrected by the poller.
  const serviceShouldBeRunning = useRef(true);
  const startupAttempted = useRef(false);
  const serviceBackgroundOperationScheduled = useRef(false);
  const serviceOperationQueue = useRef<Promise<void>>(Promise.resolve());
  const lastServiceRecoveryAttempt = useRef(0);

  const receiveSnapshot = useCallback((next: CoreSnapshot): void => {
    setSnapshot(next);
    setError(undefined);
    native.menuBar.setStatus(next.service);
    native.tray.setStatus(next.service);
  }, [native]);

  const refreshSnapshot = useCallback(async (): Promise<CoreSnapshot> => {
    const next = await ipc.snapshot();
    receiveSnapshot(next);
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
        return await refreshSnapshot();
      } catch {
        // A lifecycle operation can fail while Core itself is still healthy
        // (for example, a child process cannot bind its configured port).
        // Preserve the settings/menu surface and refresh its actual state;
        // only surface the global Core error if that refresh also fails.
        try {
          return await refreshSnapshot();
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
    native.setLocalization({
      appTitle: translate("app.title"), serviceUnavailable: translate("error.coreUnavailable"),
      cancel: translate("menu.cancel"), set: translate("common.set"), clear: translate("common.clear"), stage: translate("common.stageRaw"), find: translate("common.find"),
      findNext: translate("common.findNext"), edit: translate("common.edit"), undo: translate("common.undo"),
      redo: translate("common.redo"), cut: translate("common.cut"), copy: translate("common.copy"),
      paste: translate("common.paste"), selectAll: translate("common.selectAll"), settings: translate("menu.codex"),
      reload: translate("menu.reload"), closeWindow: translate("menu.close"), version: translate("common.version"),
      build: translate("common.build"), ok: translate("common.ok"), invalidText: translate("common.invalidText"),
      routeHome: translate("route.home"), routeProvidersModels: translate("card.providersModels"),
      routeCodexSettings: translate("card.codexSettings"), routeClaudeSettings: translate("card.claudeSettings"),
      routeRuntimeSettings: translate("card.runtimeSettings"), routeConfigurationPackage: translate("card.configurationPackage"),
      routeWebdavSettings: translate("card.webdavSettings"), routeLogs: translate("card.logs"),
    });
  }, [native, translate]);

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
    native.setShortcuts({ openMenu: "Cmd+, / Ctrl+,", closeWindow: "Esc", reload: "Cmd+R / Ctrl+R" });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [hostTranslate, ipc, native, receiveSnapshot, refreshSnapshot]);

  useEffect(() => {
    if (!snapshot || startupAttempted.current || !serviceShouldBeRunning.current) return;
    startupAttempted.current = true;
    if (snapshot.service.state === "stopped") void runServiceOperation("start");
  }, [runServiceOperation, snapshot?.service.state]);

  useEffect(() => {
    let active = true;
    const pollServiceHealth = async (): Promise<void> => {
      if (!serviceShouldBeRunning.current) return;
      const current = await runServiceOperation("health", true);
      if (!active || !current || !serviceShouldBeRunning.current) return;
      if (current.service.state !== "stopped" && current.service.state !== "unhealthy") return;
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
  }, [runServiceOperation]);

  useEffect(() => {
    if (!routeRequest) return;
    setRoute(routeRequest);
    if (routeRequest !== "home") {
      native.window.open(routeRequest);
      native.window.focus(routeRequest);
    }
  }, [native, routeRequest]);

  useEffect(() => {
    const action = nativeAction?.id;
    if (!action || action.startsWith("open-")) return;
    const actionKey = `${nativeAction.sequence}:${action}`;
    if (handledNativeActions.current.has(actionKey)) return;
    const serviceOperation = serviceOperationForNativeAction(action);
    if (serviceOperation) {
      handledNativeActions.current.add(actionKey);
      void runServiceOperation(serviceOperation);
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
          setSnapshot(current);
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
    } else if (action === "webdav-toggle" && snapshot) {
      handledNativeActions.current.add(actionKey);
      void (async () => {
        try {
          const staged = await ipc.dispatch({ domain: "webdav", type: "patch", payload: { enabled: !snapshot.webdav.enabled } }, snapshot.revision);
          await ipc.apply("webdav", staged.revision);
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
          setSnapshot(await ipc.snapshot());
        } catch {
          setError(hostTranslate("error.coreUnavailable"));
        }
      })();
    }
  }, [hostTranslate, ipc, nativeAction, runServiceOperation, snapshot]);

  useEffect(() => {
    if (!snapshot) return;
    const serviceState = snapshot.service.state;
    const serviceActive = serviceState === "running" || serviceState === "starting" || serviceState === "unhealthy";
    const serviceStartAvailable = !serviceOperationPending && serviceState === "stopped";
    const serviceRestartAvailable = !serviceOperationPending && serviceState !== "unknown" && serviceState !== "starting";
    const serviceReloadAvailable = !serviceOperationPending && (serviceState === "running" || serviceState === "unhealthy");
    const actions = [
      { id: "toggle-autostart", title: translate("menu.autoStart"), enabled: true },
      { id: "open-providers-models", title: translate("menu.providers"), enabled: true },
      { id: "open-runtime-settings", title: translate("menu.runtime"), enabled: true },
      { id: "open-codex-settings", title: translate("menu.codex"), enabled: true },
      { id: "open-claude-settings", title: translate("menu.claude"), enabled: true },
      { id: "open-configuration-package", title: translate("menu.configuration"), enabled: true },
      { id: "webdav-toggle", title: snapshot.webdav.enabled ? translate("webdav.disable") : translate("webdav.enable"), enabled: true },
      { id: "open-webdav-settings", title: translate("menu.webdav"), enabled: true },
      { id: "open-recovery", title: translate("menu.recovery"), enabled: true },
      { id: "open-logs", title: translate("menu.logs"), enabled: true },
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
    native.tray.setActions(actions);
  }, [native, serviceOperationPending, snapshot, translate]);

  return (
    <View style={styles.root} accessibilityLabel={translate("app.title")}>
      {error ? <Text style={styles.error}>{error}</Text> : null}
      {!error && route !== "home" ? <RouteSurface route={route} snapshot={snapshot} ipc={ipc} native={native} translate={translate} logTabRequest={logTabRequest} nativeAction={nativeAction} onSnapshot={setSnapshot} /> : null}
      {!error && route === "home" ? <View style={styles.menuBarHost} /> : null}
    </View>
  );
}

const legacyWindowSpecs = {
  "providers-models": { width: 1052, height: 600, minWidth: 1052, minHeight: 560 },
  "codex-settings": { width: 1120, height: 680, minWidth: 1020, minHeight: 620 },
  "claude-settings": { width: 1120, height: 680, minWidth: 1020, minHeight: 620 },
  "runtime-settings": { width: 1080, height: 620, minWidth: 760, minHeight: 500 },
  "configuration-package": { width: 420, height: 208, minWidth: 420, minHeight: 132 },
  "webdav-settings": { width: 680, height: 386, minWidth: 680, minHeight: 386 },
  "logs": { width: 900, height: 580, minWidth: 640, minHeight: 420 },
} as const;

function LegacyWindowTitle({ title, subtitle, validation }: { title: string; subtitle?: string; validation?: string }): React.JSX.Element {
  return <View style={styles.windowTitleBlock}><Text style={styles.windowTitle}>{title}</Text>{subtitle ? <Text style={styles.windowSubtitle}>{subtitle}</Text> : null}{validation ? <Text style={styles.validationText}>{validation}</Text> : null}</View>;
}

function LegacyDialogFooter({ status, leading, exact, children }: { status?: string; leading?: React.ReactNode; exact?: boolean; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.legacyFooter, exact && styles.legacyFooterExact]}>{leading ?? (status ? <Text numberOfLines={1} style={styles.footerStatus}>{status}</Text> : <View />)}<View style={styles.footerSpacer} /><View style={styles.footerButtons}>{children}</View></View>;
}

function IconButton({ label, title, disabled, onPress }: { label: string; title: string; disabled?: boolean; onPress: () => void }): React.JSX.Element {
  return <NativeButton title={label} toolTip={title} accessibilityLabel={title} compact disabled={disabled} onPress={onPress} style={styles.iconButton} />;
}

function WindowTabs({ values, selected, onSelect, style }: { values: Array<{ id: string; title: string }>; selected: string; onSelect: (id: string) => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const labels = values.map((item) => item.title);
  const selectedValue = values.find((item) => item.id === selected)?.title ?? labels[0] ?? "";
  return <NativeSegmentedControl labels={labels} selectedValue={selectedValue} onChange={({ nativeEvent }) => { const next = values[nativeEvent.index]; if (next) onSelect(next.id); }} style={[styles.windowTabs, style]} />;
}

function RouteSurface({ route, snapshot, ipc, native, translate, logTabRequest, nativeAction, onSnapshot }: { route: AppRoute; snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; translate: Translate; logTabRequest?: LogTab; nativeAction?: { id: string; sequence: number }; onSnapshot: (next: CoreSnapshot) => void }): React.JSX.Element {
  const domain = domainForRoute(route);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string>();
  const [packagePreview, setPackagePreview] = useState<PackageImportResult>();
  const [issues, setIssues] = useState<ValidationSummary["issues"]>([]);
  const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);
  const activeRuns = useRef(0);
  const revision = useRef<number | undefined>(snapshot?.revision);
  const dispatchQueue = useRef<Promise<void>>(Promise.resolve());
  const lastDispatchError = useRef<unknown>(undefined);
  const pendingFields = useRef(new Map<symbol, PendingField>());
  const fieldRegistry = useMemo<PendingFieldRegistry>(() => ({
    register: (id, field) => {
      if (field) pendingFields.current.set(id, field);
      else pendingFields.current.delete(id);
    },
  }), []);

  useEffect(() => {
    if (snapshot) revision.current = snapshot.revision;
  }, [snapshot?.revision]);

  useEffect(() => {
    if (route === "home") return;
    const spec = legacyWindowSpecs[route];
    void native.window.setContentSize?.(spec.width, spec.height);
  }, [native, route]);

  const refresh = async (): Promise<CoreSnapshot> => {
    const next = await ipc.snapshot();
    revision.current = next.revision;
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
  const run = async (operation: () => Promise<unknown>, message = "common.applied"): Promise<void> => {
    activeRuns.current += 1;
    setBusy(true);
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
      if (activeRuns.current === 0) setBusy(false);
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
  const dispatch: Dispatch = (type, payload = {}, targetDomain = domain) => run(() => enqueueDispatch(type, payload, targetDomain));
  const flushPendingFields = async (): Promise<void> => {
    await Promise.all([...pendingFields.current.values()].map((field) => field.commit()));
    await dispatchQueue.current;
    if (lastDispatchError.current !== undefined) throw lastDispatchError.current;
  };
  const discardPendingFields = (): void => pendingFields.current.forEach((field) => field.reset());
  const reload = (): Promise<void> => {
    if (!domain) return Promise.resolve();
    discardPendingFields();
    return run(async () => {
      const reloaded = await ipc.reload(domain, revision.current);
      revision.current = reloaded.revision;
      // Core invalidates the native editor capabilities when a domain reloads.
      // Make both raw Codex editors fetch fresh capabilities and disk text only
      // after that reload has succeeded.
      if (domain === "codex" || domain === "claude") setSettingsRawReloadToken((current) => current + 1);
      return reloaded;
    }, "common.applied");
  };
  const cancel = (): Promise<void> => {
    if (!domain) return Promise.resolve();
    discardPendingFields();
    return run(() => enqueueDispatch("cancel", {}, domain), "common.discarded");
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
      const current = await ipc.snapshot();
      revision.current = current.revision;
      onSnapshot(current);
      const risks = riskCodes(current, domain);
      if (risks.length > 0) {
        const accepted = await native.showConfirmation({ title: translate("claude.confirmation.required"), message: translate("claude.confirmation.required"), confirmLabel: translate("screen.confirm") });
        if (!accepted) return { cancelled: true };
      }
      return ipc.apply(domain, current.revision, risks.length > 0 ? risks : undefined);
    });
  };
  const importPackage = (sections: ConfigDomain[]): Promise<void> => run(async () => {
    const current = await ipc.snapshot();
    revision.current = current.revision;
    const conflicts = sections.filter((section) => current.drafts[section]?.dirty);
    if (conflicts.length > 0) {
      const accepted = await native.showConfirmation({ title: translate("package.conflictTitle"), message: translate("package.conflictMessage", { count: conflicts.length }), confirmLabel: translate("screen.confirm") });
      if (!accepted) return { cancelled: true };
    }
    const sourceToken = await native.openFilePicker({ purpose: "import" });
    if (!sourceToken) return { cancelled: true };
    const imported = await ipc.import(sourceToken, current.revision, sections);
    setPackagePreview(imported);
    return imported;
  }, "common.staged");
  const exportPackage = (sections: ConfigDomain[]): Promise<void> => run(async () => {
    const destinationToken = await native.saveFilePicker({ purpose: "export" });
    if (!destinationToken) return { cancelled: true };
    return ipc.export(sections, destinationToken);
  }, "common.exported");
  const validatePackage = (): Promise<void> => {
    if (!packagePreview) return Promise.resolve();
    return run(async () => {
      const validations = await Promise.all(packagePreview.draft_domains.map(async (name) => ({ name, summary: await ipc.validate(name) })));
      const importedIssues = validations.flatMap(({ name, summary }) => summary.issues.map((issue) => ({ ...issue, path: `${name}.${issue.path}` })));
      return { valid: validations.every(({ summary }) => summary.valid), issues: importedIssues } satisfies ValidationSummary;
    });
  };
  const applyPackage = (): Promise<void> => {
    if (!packagePreview) return Promise.resolve();
    return run(async () => {
      const applied = await ipc.applyDomains(packagePreview.draft_domains, packagePreview.revision);
      setPackagePreview(undefined);
      return applied;
    });
  };
  const cancelPackage = (): Promise<void> => {
    if (!packagePreview) {
      native.window.close("configuration-package");
      return Promise.resolve();
    }
    return run(async () => {
      for (const name of packagePreview.draft_domains) await enqueueDispatch("cancel", {}, name);
      setPackagePreview(undefined);
      return {};
    }, "common.discarded");
  };
  const requestClose = (): void => {
    if (busy) return;
    if (route === "configuration-package") {
      if (!packagePreview) {
        native.window.close(route);
        return;
      }
      void native.showConfirmation({
        title: translate("menu.close"),
        message: translate("common.discarded"),
        confirmLabel: translate("menu.close"),
      }).then((confirmed) => {
        if (!confirmed) return;
        void run(async () => {
          for (const name of packagePreview.draft_domains) await enqueueDispatch("cancel", {}, name);
          setPackagePreview(undefined);
          native.window.close(route);
          return {};
        }, "common.discarded");
      });
      return;
    }
    if (!domain || !snapshot?.drafts[domain]?.dirty) {
      native.window.close(route);
      return;
    }
    void native.showConfirmation({
      title: translate("menu.close"),
      message: translate("common.discarded"),
      confirmLabel: translate("menu.close"),
    }).then((confirmed) => confirmed
      ? cancel().then(() => native.window.close(route))
      : undefined);
  };
  useEffect(() => {
    if (nativeAction?.id !== `request-close-${route}`) return;
    requestClose();
  }, [nativeAction?.sequence]);
  const attachClaudeProfile = (): Promise<void> => run(async () => {
    const fileToken = await native.openFilePicker({ purpose: "claude-profile" });
    if (!fileToken) return { cancelled: true };
    return enqueueDispatch("attach_profile", { file_token: fileToken }, "claude");
  }, "common.staged");
  const definition = ROUTES.find((item) => item.id === route);
  const windowTitle = translate(definition?.titleKey ?? "app.title");
  const legacySubtitle = route === "codex-settings" || route === "claude-settings"
    ? translate("settings.subtitle")
    : route === "runtime-settings"
      ? "Saving these runtime defaults applies them to the LiteLLM service."
    : route === "webdav-settings"
        ? "Syncs the current LiteLLM Menu config, including provider keys and model routes."
        : undefined;
  return <PendingFieldContext.Provider value={fieldRegistry}><View style={styles.windowSurface}>
    {route !== "providers-models" && route !== "logs" && route !== "configuration-package" ? <LegacyWindowTitle title={windowTitle} subtitle={legacySubtitle} validation={issues.length > 0 ? `${issues.length} ${translate("common.validationIssues")}` : undefined} /> : null}
    {route === "providers-models" || route === "codex-settings" || route === "claude-settings" || route === "logs" || route === "runtime-settings" || route === "webdav-settings" ? <View style={[styles.windowContent, styles.windowContentFixed, route === "providers-models" && styles.legacyProvidersContent, route === "codex-settings" && styles.legacySettingsContent, route === "claude-settings" && styles.legacySettingsContent, route === "logs" && styles.legacyLogsContent, route === "runtime-settings" && styles.legacyRuntimeContent, route === "webdav-settings" && styles.legacyWebDavContent]}>
    {route === "providers-models" ? <LegacyProviderWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} probe={(providerId, modelId) => run(() => ipc.probe(providerId, modelId, "providers_models"), "screen.probe")} /> : null}
    {route === "codex-settings" ? <LegacyCodexWorkspace snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} rawReloadToken={settingsRawReloadToken} /> : null}
    {route === "claude-settings" ? <ClaudeScreen snapshot={snapshot} ipc={ipc} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} attachProfile={attachClaudeProfile} rawReloadToken={settingsRawReloadToken} /> : null}
    {route === "logs" ? <LegacyLogsWorkspace snapshot={snapshot} ipc={ipc} onSnapshot={onSnapshot} busy={busy} translate={translate} dispatch={dispatch} requestedTab={nativeAction?.id === "open-recovery" ? "recovery" : logTabRequest} /> : null}
    {route === "runtime-settings" ? <LegacyRuntimeWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} /> : null}
    {route === "webdav-settings" ? <LegacyWebDavWorkspace snapshot={snapshot} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} /> : null}
    {issues.length > 0 ? <IssueList issues={issues} translate={translate} /> : null}
    </View> : <ScrollView contentContainerStyle={styles.windowContent} keyboardShouldPersistTaps="handled">
    {route === "configuration-package" ? <ConfigurationPackageScreen native={native} busy={busy} preview={packagePreview} translate={translate} onImport={importPackage} onExport={exportPackage} onValidate={validatePackage} onApply={applyPackage} onCancel={cancelPackage} /> : null}
    {issues.length > 0 ? <IssueList issues={issues} translate={translate} /> : null}
    </ScrollView>}
    {route !== "logs" && route !== "configuration-package" ? <LegacyDialogFooter status={result} exact={route === "providers-models"} leading={route === "runtime-settings" ? <ActionButton title="Restore Defaults…" disabled={busy} onPress={() => dispatch("restore_defaults")} /> : route === "webdav-settings" ? <View style={styles.webdavFooterLeading}><ActionButton title="Test" disabled={busy} style={styles.legacyWideButton} onPress={() => run(() => ipc.probe(undefined, undefined, "webdav"), "webdav.probe")} /><Text numberOfLines={1} style={styles.webdavProbeStatus}>{snapshot?.webdav.last_probe ? translate(`webdav.status.${snapshot.webdav.last_probe}`) : ""}</Text></View> : undefined}><>{route === "codex-settings" || route === "claude-settings" ? <ActionButton title={translate("settings.reloadFromDisk")} disabled={busy} onPress={reload} /> : null}<ActionButton title={translate("menu.close")} disabled={busy} style={route === "runtime-settings" || route === "webdav-settings" ? styles.legacyWideButton : undefined} onPress={requestClose} /><ActionButton primary title={route === "runtime-settings" ? "Save & Apply" : translate("menu.apply")} disabled={busy || (domain ? !snapshot?.drafts[domain]?.dirty : false)} style={route === "runtime-settings" || route === "webdav-settings" ? styles.legacyWideButton : undefined} onPress={apply} /></></LegacyDialogFooter> : null}
  </View></PendingFieldContext.Provider>;
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

function LegacyProviderWorkspace({ snapshot, ipc, onSnapshot, native, busy, translate, dispatch, onSecretState, clearSecret, probe }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; probe: (providerId?: string, modelId?: string) => void }): React.JSX.Element {
  const state = domainState(snapshot, "providers_models");
  const details = asRecords(state.providers);
  const fallback = snapshot?.providers_models.providers ?? [];
  const providers = details.length > 0 ? details : fallback.map(providerRecord);
  const [selectedProvider, setSelectedProvider] = useState<string>();
  const provider = providers.find((item) => identifier(item) === selectedProvider) ?? providers[0];
  const providerId = provider ? identifier(provider) : "";
  const models = provider ? asRecords(provider.models).map(modelRecord) : [];
  const [selectedModel, setSelectedModel] = useState<string>();
  const [providerSourceModel, setProviderSourceModel] = useState<string>();
  const model = models.find((item) => identifier(item) === selectedModel);
  const [viewMode, setViewMode] = useState<"providers" | "routes">("providers");
  const [selectedRoute, setSelectedRoute] = useState<string>();
  const importFrom = translate("providers.importFrom");
  const currentCodex = translate("providers.currentCodex");
  const configurationFile = translate("providers.configurationFile");
  const importLink = translate("providers.importLink");
  const [importSource, setImportSource] = useState(importFrom);
  const [fetchKeyName, setFetchKeyName] = useState<string>();
  const [fetchedModelsOpen, setFetchedModelsOpen] = useState(false);
  const operation = asRecord(snapshot?.action_summaries?.providers_models);
  const operationSummary = asRecord(operation.operation_summary);
  const apiKeyNames = stringList(provider?.api_key_names);
  const runtimeSettings = asRecords(domainState(snapshot, "runtime").settings);
  const billingRefreshSetting = runtimeSettings.find((item) => identifier(item) === "LITELLM_MENU_BALANCE_REFRESH_MINUTES");
  const billingRefreshMinutes = Math.max(0, Math.min(1_440, numberValue(billingRefreshSetting?.value, 5)));
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
    const keyName = fetchKeyName ?? "default";
    void native.chooseModelsToAdd({ models: candidates, providerName, keyName }).then((selection) => {
      const selectedModels = (selection ?? []).filter((model, index, all) => candidateSet.has(model) && all.indexOf(model) === index);
      if (selectedModels.length === 0) return;
      void Promise.all(selectedModels.map((upstreamModel, index) => dispatch("model.add", { provider_id: providerId, model: { name: upstreamModel, upstream_model: upstreamModel, api_key_name: fetchKeyName, enabled: true, order: models.length + index + 1, upstream_url_surface: "openai/responses", supported_upstream_url_surfaces: ["openai/responses"] } })));
    }).catch(() => undefined);
  }, [busy, fetchedModelsOpen, fetchKeyName, native, operationSummary, provider, providerId, models.length, dispatch]);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    const refreshBilling = async (): Promise<void> => {
      if (inFlight) return;
      inFlight = true;
      try {
        const current = await ipc.snapshot();
        const staged = await ipc.dispatch({ domain: "providers_models", type: "providers.refresh_billing" }, current.revision);
        const next = await ipc.snapshot();
        if (active && next.revision >= staged.revision) onSnapshot(next);
      } catch {
        // Live billing is optional and must not disturb the editable draft.
      } finally {
        inFlight = false;
      }
    };
    void refreshBilling();
    const interval = billingRefreshMinutes > 0
      ? setInterval(() => { void refreshBilling(); }, billingRefreshMinutes * 60 * 1000)
      : undefined;
    return () => { active = false; if (interval) clearInterval(interval); };
  }, [billingRefreshMinutes, ipc, onSnapshot]);
  const importSelected = async (): Promise<void> => {
    const fileToken = await native.openFilePicker({ purpose: "import" });
    if (fileToken) await dispatch("providers.import_selected", { file_token: fileToken });
  };
  const selectImportSource = (source: string): void => {
    setImportSource(source);
    const operation = source === currentCodex
      ? dispatch("providers.import_codex_current")
      : source === configurationFile
        ? importSelected()
      : source === importLink
          ? native.editSecret({ domain: "providers_models", field: "import_link", title: "Paste Provider Import Link", allowClear: false }).then(() => undefined)
          : Promise.resolve();
    void Promise.resolve(operation).finally(() => setImportSource(importFrom));
  };
  const addProvider = (): void => { void dispatch("provider.add", { provider: { name: "", enabled: true, models: [], create_default_api_key: true } }); };
  const addModel = (): void => {
    if (!provider) return;
    dispatch("model.add", { provider_id: providerId, model: { name: "", upstream_model: "", enabled: true, order: models.length + 1, upstream_url_surface: "openai/responses", supported_upstream_url_surfaces: ["openai/responses"] } });
  };
  const routes = providers.flatMap((entry) => asRecords(entry.models).map(modelRecord).map((entryModel) => ({ provider: entry, model: entryModel })));
  const activeRoute = routes.find(({ provider: routeProvider, model: routeModel }) => `${identifier(routeProvider)}-${identifier(routeModel)}` === selectedRoute);
  const moveRoute = (direction: "up" | "down"): void => {
    if (!activeRoute) return;
    void dispatch("model.move", { provider_id: identifier(activeRoute.provider), model_id: identifier(activeRoute.model), direction });
  };
  const confirmDeleteProvider = (): void => {
    if (!provider) return;
    const label = stringValue(provider.name, identifier(provider));
    void native.showConfirmation({ title: translate("providers.deleteProvider"), message: `${label} (${models.length} ${translate("providers.models")})`, confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("provider.delete", { provider_id: providerId }).then(() => { setSelectedProvider(undefined); setSelectedModel(undefined); setProviderSourceModel(undefined); }) : undefined);
  };
  const confirmDeleteModel = (): void => {
    if (!model) return;
    const modelId = identifier(model);
    void native.showConfirmation({ title: translate("providers.deleteModel"), message: stringValue(model.name, modelId), confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("model.delete", { provider_id: providerId, model_id: modelId }).then(() => setSelectedModel(undefined)) : undefined);
  };
  const providerRows = providers.map((item) => ({ key: identifier(item), cells: [stringValue(item.display_name, stringValue(item.name, identifier(item))), String(asRecords(item.models).length || numberValue(item.model_count))] }));
  const modelRows = models.map((item) => ({ key: identifier(item), cells: [stringValue(item.display_name, stringValue(item.name, identifier(item))), upstreamModelLabel(item), billingBalanceValue(item.billing), `${stringValue(item.api_key_name, "N/A")} / ${numberValue(item.order, 1)}`] }));
  const routeRows = routes.map(({ provider: routeProvider, model: routeModel }) => ({ key: `${identifier(routeProvider)}-${identifier(routeModel)}`, cells: [stringValue(routeModel.name), String(numberValue(routeModel.order, 1)), `${stringValue(routeProvider.name)} / ${stringValue(routeModel.api_key_name, "N/A")}`, upstreamModelLabel(routeModel)] }));
  return <View style={styles.legacyProvidersLayout}>
    <View style={styles.providerWorkspace}>
      <View style={styles.providerLeftColumn}><View style={styles.providerToolbar}>
        <WindowTabs values={[{ id: "providers", title: translate("providers.providers") }, { id: "routes", title: translate("providers.routes") }]} selected={viewMode} onSelect={(value) => setViewMode(value as "providers" | "routes")} />
        <NativePicker labels={[importFrom, currentCodex, configurationFile, importLink]} selectedValue={importSource} disabled={busy} onChange={({ nativeEvent }) => selectImportSource(nativeEvent.value)} style={styles.importSourcePicker} />
        <View style={styles.toolbarSpacer} />
      </View><View style={styles.providerModelColumns}>
        {viewMode === "providers" ? <><LegacyTablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !provider} onPress={confirmDeleteProvider} /></>}>
          {providers.length === 0 ? <EmptyState translate={translate} /> : <NativeTable columns={[{ label: translate("providers.provider"), width: 150 }, { label: translate("providers.models"), width: 48 }]} rows={providerRows} selectedKey={providerId} onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }} style={styles.nativeProviderTable} />}
        </LegacyTablePane>
        <LegacyTablePane style={styles.modelListPane} title={translate("providers.models")} actions={<><IconButton label="+" title={translate("providers.newModel")} disabled={busy || !provider} onPress={addModel} /><IconButton label="⧉" title={translate("common.copy")} disabled={busy || !model} onPress={() => model && dispatch("model.duplicate", { provider_id: providerId, model_id: identifier(model) })} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !model} onPress={confirmDeleteModel} /></>}>
          {models.length === 0 ? <EmptyState translate={translate} /> : <NativeTable columns={[{ label: translate("providers.model"), width: 118 }, { label: translate("providers.upstream"), width: 132 }, { label: translate("providers.balance"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 104 }]} rows={modelRows} selectedKey={selectedModel ?? ""} onSelectionChange={(key) => { setSelectedModel(key); setProviderSourceModel(undefined); }} style={styles.nativeModelTable} />}
          <View style={styles.tableBottomRow}><NativePicker labels={apiKeyNames.length > 0 ? apiKeyNames : ["default"]} selectedValue={fetchKeyName ?? apiKeyNames[0] ?? "default"} disabled={busy || !provider || apiKeyNames.length === 0} onChange={({ nativeEvent }) => setFetchKeyName(nativeEvent.value)} style={styles.fetchKeyPicker} /><ActionButton title={translate("providers.fetch")} disabled={busy || !provider || !fetchKeyName} onPress={() => { void dispatch("providers.fetch_models", { provider_id: providerId, api_key_name: fetchKeyName }).then(() => setFetchedModelsOpen(true)); }} /></View>
        </LegacyTablePane></> : <LegacyTablePane title={translate("providers.routes")} wide actions={<><IconButton label="↑" title={translate("common.moveUp")} disabled={busy || !activeRoute} onPress={() => moveRoute("up")} /><IconButton label="↓" title={translate("common.moveDown")} disabled={busy || !activeRoute} onPress={() => moveRoute("down")} /></>}>
        {routes.length === 0 ? <EmptyState translate={translate} /> : <NativeTable columns={[{ label: translate("providers.model"), width: 170 }, { label: translate("common.order"), width: 56 }, { label: `${translate("providers.provider")} / ${translate("providers.key")}`, width: 130 }, { label: translate("providers.upstream"), width: 164 }]} rows={routeRows} selectedKey={selectedRoute ?? ""} alternatingRows onSelectionChange={(routeId) => { const selected = routes.find(({ provider: routeProvider, model: routeModel }) => `${identifier(routeProvider)}-${identifier(routeModel)}` === routeId); setSelectedRoute(routeId); if (selected) { setSelectedProvider(identifier(selected.provider)); setSelectedModel(identifier(selected.model)); setProviderSourceModel(undefined); } }} style={styles.nativeRouteTable} />}
        </LegacyTablePane>}
      </View></View>
      <View style={styles.providerInspector}>{provider && model ? <LegacyModelInspector providers={providers} provider={provider} providerId={providerId} model={model} native={native} busy={busy} translate={translate} dispatch={dispatch} probe={probe} onProviderClick={() => { setProviderSourceModel(identifier(model)); setSelectedModel(undefined); }} onProviderChange={(destinationProviderId) => dispatch("model.move_provider", { provider_id: providerId, model_id: identifier(model), destination_provider_id: destinationProviderId }).then(() => { setSelectedProvider(destinationProviderId); setSelectedModel(identifier(model)); setProviderSourceModel(undefined); })} /> : provider ? <ProviderEditor provider={provider} native={native} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} sourceModel={models.find((item) => identifier(item) === providerSourceModel)} onReturnToModel={() => { if (providerSourceModel) setSelectedModel(providerSourceModel); setProviderSourceModel(undefined); }} /> : <EmptyState translate={translate} />}</View>
    </View>
  </View>;
}

function LegacyTablePane({ title, actions, wide, style, children }: { title: string; actions: React.ReactNode; wide?: boolean; style?: StyleProp<ViewStyle>; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.legacyTablePane, wide && styles.legacyTablePaneWide, style]}><View style={styles.tableTitleRow}><Text style={styles.tableTitle}>{title}</Text><View style={styles.tableActions}>{actions}</View></View>{children}</View>;
}

function LegacyModelInspector({ providers, provider, providerId, model, native, busy, translate, dispatch, probe, onProviderClick, onProviderChange }: { providers: UnknownRecord[]; provider: UnknownRecord; providerId: string; model: UnknownRecord; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; probe: (providerId?: string, modelId?: string) => void; onProviderClick: () => void; onProviderChange: (providerId: string) => void }): React.JSX.Element {
  const id = identifier(model);
  const providerLabels = providers.map((item) => stringValue(item.name, identifier(item)));
  const providerIndex = providers.findIndex((item) => identifier(item) === providerId);
  const providerLabel = providerLabels[Math.max(0, providerIndex)] ?? "";
  const keyNames = stringList(provider.api_key_names);
  const selectedKey = stringValue(model.api_key_name, keyNames[0] ?? "");
  const billingTip = billingToolTip(model);
  return <ScrollView contentContainerStyle={styles.inspectorContent}><View style={styles.modelBreadcrumb}><NativeButton title={providerLabel} link disabled={busy} onPress={onProviderClick} style={styles.breadcrumbProvider} /><Text style={styles.breadcrumbSeparator}>&gt;</Text><Text numberOfLines={1} style={styles.inspectorHeading}>{stringValue(model.name, id)}</Text></View><View style={styles.inspectorDivider} /><View style={styles.inspectorBody}><View style={styles.inspectorEnabledRow}><NativeCheckbox label={translate("common.enabled")} value={booleanValue(model.enabled, true)} disabled={busy} onValueChange={(enabled) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { enabled } })} /><ActionButton title={translate("screen.probe")} disabled={busy} onPress={() => probe(providerId, id)} /></View><View style={styles.billingGrid}><InspectorInfoRow label={translate("providers.balance")} value={billingBalanceValue(model.billing)} toolTip={billingTip} /><InspectorInfoRow label={translate("common.usage")} value={billingUsageValue(model.usage)} toolTip={billingTip} /><InspectorInfoRow label={translate("providers.multiplier")} value={billingMultiplierValue(model.multiplier)} toolTip={billingTip} /></View><TextField label={translate("providers.publicModel")} labelWidth={96} labelAlign="left" value={stringValue(model.name)} onCommit={(name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { name } })} /><PickerField label={translate("providers.provider")} labelWidth={96} labelAlign="left" value={providerLabel} values={providerLabels} disabled={busy || providers.length <= 1} onSelect={(label) => { const next = providers.find((item) => stringValue(item.name, identifier(item)) === label); if (next) onProviderChange(identifier(next)); }} /><PickerField label={translate("common.apiKey")} labelWidth={96} labelAlign="left" value={selectedKey} values={keyNames.length > 0 ? keyNames : [selectedKey || "N/A"]} disabled={busy || keyNames.length === 0} onSelect={(api_key_name) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { api_key_name } })} /><TextField label={translate("providers.upstream")} labelWidth={96} labelAlign="left" value={stringValue(model.upstream_model)} onCommit={(upstream_model) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_model } })} /><TextField label={translate("common.order")} labelWidth={96} labelAlign="left" controlWidth={64} value={String(numberValue(model.order, 1))} keyboardType="numeric" onCommit={(order) => dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { order: Number(order) || 1 } })} /><ProtocolOrderEditor providerId={providerId} model={model} busy={busy} translate={translate} dispatch={dispatch} /></View></ScrollView>;
}

function ProtocolOrderEditor({ providerId, model, busy, translate, dispatch }: { providerId: string; model: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch }): React.JSX.Element {
  const id = identifier(model);
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
  return { ...model, id: identifier(model), name: stringValue(model.name, stringValue(model.display_name, stringValue(model.model))), display_name: stringValue(model.display_name, stringValue(model.name)), enabled: booleanValue(model.enabled, true), order: numberValue(model.order, 1) };
}

function upstreamModelLabel(model: UnknownRecord): string {
  const value = stringValue(model.upstream_model, stringValue(model.litellm_model));
  const separator = value.indexOf("/");
  return separator >= 0 ? value.slice(separator + 1) : value;
}

function identifier(record: UnknownRecord): string {
  return stringValue(record.id, stringValue(record.editor_id, stringValue(record.name, "new-item")));
}

function ProviderEditor({ provider, native, busy, translate, dispatch, onSecretState, clearSecret, sourceModel, onReturnToModel }: { provider: UnknownRecord; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; sourceModel?: UnknownRecord; onReturnToModel: () => void }): React.JSX.Element {
  const id = identifier(provider);
  const keys = stringList(provider.api_key_names);
  const [selectedKey, setSelectedKey] = useState<string>(keys[0] ?? "");
  useEffect(() => { if (!keys.includes(selectedKey)) setSelectedKey(keys[0] ?? ""); }, [keys, selectedKey]);
  const addKey = (): void => { void dispatch("provider.key_add", { provider_id: id, name: uniqueKeyName(keys) }).then(() => setSelectedKey(uniqueKeyName(keys))); };
  const renameKey = (name: string): void => { if (!selectedKey || !name || name === selectedKey) return; void dispatch("provider.key_patch", { provider_id: id, old_name: selectedKey, name }).then(() => setSelectedKey(name)); };
  const deleteKey = (): void => { if (!selectedKey || keys.length <= 1) return; void native.showConfirmation({ title: translate("providers.deleteApiKey"), message: `${selectedKey} → ${keys.find((key) => key !== selectedKey) ?? "default"}`, confirmLabel: translate("common.delete") }).then((confirmed) => confirmed ? dispatch("provider.key_delete", { provider_id: id, name: selectedKey }).then(() => setSelectedKey(keys.find((key) => key !== selectedKey) ?? "")) : undefined); };
  const providerLabel = stringValue(provider.display_name, stringValue(provider.name, id));
  const sourceModelLabel = sourceModel ? stringValue(sourceModel.name, identifier(sourceModel)) : "";
  return <View style={styles.providerEditorContent}>
    <View style={styles.providerEditorHeader}><Text numberOfLines={1} style={styles.providerEditorHeading}>{translate("providers.provider")}: {providerLabel}</Text>{sourceModel ? <NativeButton title={translate("providers.backToModel", { model: sourceModelLabel })} link disabled={busy} onPress={onReturnToModel} style={styles.providerReturnToModel} /> : null}</View>
    <View style={styles.providerEditorSection}><View style={styles.providerEnabledRow}><NativeCheckbox label={translate("common.enabled")} value={booleanValue(provider.enabled, true)} disabled={busy} onValueChange={(enabled) => dispatch("provider.patch", { provider_id: id, changes: { enabled } })} /></View>
    <TextField label={translate("providers.baseUrl")} labelWidth={96} labelAlign="left" value={stringValue(provider.endpoint, stringValue(provider.api_base))} onCommit={(endpoint) => dispatch("provider.patch", { provider_id: id, changes: { endpoint } })} />
    <TextField label={translate("providers.providerName")} labelWidth={96} labelAlign="left" value={stringValue(provider.name, stringValue(provider.display_name))} onCommit={(name) => dispatch("provider.patch", { provider_id: id, changes: { name } })} />
    <View style={styles.providerKeysEditor}><Text style={styles.providerKeysHeading}>{translate("providers.apiKeys")}</Text><View style={styles.providerKeyGrid}><NativeTable columns={[{ label: translate("providers.key"), width: 118 }]} rows={keys.map((key) => ({ key, cells: [key] }))} selectedKey={selectedKey} onSelectionChange={setSelectedKey} style={styles.providerKeyTable} /><View style={styles.providerKeyFields}><TextField label={translate("providers.label")} labelWidth={48} labelAlign="left" value={selectedKey} onCommit={renameKey} /><NativeSecretField actionsBelow label={translate("common.apiKey")} labelWidth={48} labelAlign="left" hint={translate("providers.apiKeyHint")} busy={busy || !selectedKey} domain="providers_models" field="api_key" target={`${id}\x1f${selectedKey}`} onSecretState={onSecretState} setTitle={translate("common.set")} clearTitle={translate("common.clear")} clearDisabled={busy || !selectedKey} onClear={() => clearSecret({ domain: "providers_models", field: "api_key", target: `${id}\x1f${selectedKey}` })} /></View></View><View style={styles.providerKeyActions}><IconButton label="+" title={translate("common.add")} disabled={busy} onPress={addKey} /><IconButton label="−" title={translate("common.delete")} disabled={busy || keys.length <= 1 || !selectedKey} onPress={deleteKey} /></View></View></View>
  </View>;
}

function LegacyCodexWorkspace({ snapshot, ipc, native, busy, translate, dispatch, onSecretState, clearSecret, rawReloadToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; rawReloadToken: number }): React.JSX.Element {
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
      ? validationErrors.join(" · ")
      : validationWarnings.length > 0
        ? validationWarnings.join(" · ")
        : translate("settings.synchronized");
  const validationStatusStyle = validationErrors.length > 0
    ? styles.codexValidationError
    : validationWarnings.length > 0
      ? styles.codexValidationWarning
      : styles.codexValidationValid;
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
      <PickerField label={translate("codex.reasoning")} value={stringValue(structured.model_reasoning_effort, "(Empty)")} values={["(Empty)", "minimal", "low", "medium", "high", "xhigh"]} disabled={busy} onSelect={(model_reasoning_effort) => dispatch("patch", { model_reasoning_effort: emptyToNull(model_reasoning_effort) })} />
      <PickerField label={translate("codex.planReasoning")} value={stringValue(structured.plan_mode_reasoning_effort, "(Empty)")} values={["(Empty)", "none", "minimal", "low", "medium", "high", "xhigh"]} disabled={busy} onSelect={(plan_mode_reasoning_effort) => dispatch("patch", { plan_mode_reasoning_effort: emptyToNull(plan_mode_reasoning_effort) })} />
      <PickerField label={translate("settings.reasoningSummary")} value={stringValue(structured.model_reasoning_summary, translate("common.empty"))} values={[translate("common.empty"), "auto", "concise", "detailed", "none"]} disabled={busy} onSelect={(model_reasoning_summary) => dispatch("patch", { model_reasoning_summary: emptyToNull(model_reasoning_summary, translate) })} />
      <PickerField label={translate("codex.verbosity")} value={stringValue(structured.model_verbosity, "(Empty)")} values={["(Empty)", "low", "medium", "high"]} disabled={busy} onSelect={(model_verbosity) => dispatch("patch", { model_verbosity: emptyToNull(model_verbosity) })} />
      <PickerField label={translate("settings.personality")} value={stringValue(structured.personality, translate("common.empty"))} values={[translate("common.empty"), "none", "friendly", "pragmatic"]} disabled={busy} onSelect={(personality) => dispatch("patch", { personality: emptyToNull(personality, translate) })} />
      <PickerField label={translate("settings.serviceTier")} value={stringValue(structured.service_tier, translate("common.empty"))} values={[translate("common.empty"), "fast", "flex"]} disabled={busy} onSelect={(service_tier) => dispatch("patch", { service_tier: emptyToNull(service_tier, translate) })} />
      <PickerField label={translate("codex.webSearch")} value={stringValue(structured.web_search, "(Empty)")} values={["(Empty)", "disabled", "cached", "indexed", "live"]} disabled={busy} onSelect={(web_search) => dispatch("patch", { web_search: emptyToNull(web_search) })} />
      <TextField label={translate("codex.contextWindow")} value={stringValue(structured.model_context_window)} keyboardType="numeric" onCommit={(model_context_window) => dispatch("patch", { model_context_window })} />
      <TextField label={translate("settings.autoCompactLimit")} value={stringValue(structured.model_auto_compact_token_limit)} keyboardType="numeric" onCommit={(model_auto_compact_token_limit) => dispatch("patch", { model_auto_compact_token_limit })} />
      <TextField label={translate("codex.toolOutputLimit")} value={stringValue(structured.tool_output_token_limit)} keyboardType="numeric" onCommit={(tool_output_token_limit) => dispatch("patch", { tool_output_token_limit })} />
    </View></Section>
    <Section title={translate("codex.features")}><FeatureToggles value={asRecord(structured.features)} supported={stringList(structured.supported_features)} disabled={busy} onChange={(features) => dispatch("patch", { features })} translate={translate} /></Section>
    <Section title={translate("codex.permissions")}><View style={styles.form}>
      <SegmentedField label={translate("codex.permissionMode")} value={stringValue(permissions.mode, "legacy")} values={["legacy", "profile", "unset"]} disabled={busy} onSelect={(mode) => dispatch("patch", { permissions: { mode } })} />
      <PickerField label={translate("codex.sandboxMode")} value={stringValue(permissions.sandbox_mode)} values={["read-only", "workspace-write", "danger-full-access"]} disabled={busy || permissions.mode === "profile"} onSelect={(sandbox_mode) => dispatch("patch", { permissions: { sandbox_mode } })} />
      <PickerField label={translate("codex.approvalPolicy")} value={stringValue(permissions.approval_policy)} values={["untrusted", "on-request", "never"]} disabled={busy} onSelect={(approval_policy) => dispatch("patch", { permissions: { approval_policy } })} />
      <PickerField label={translate("settings.approvalReviewer")} value={stringValue(permissions.approvals_reviewer, translate("common.empty"))} values={[translate("common.empty"), "user", "auto_review"]} disabled={busy} onSelect={(approvals_reviewer) => dispatch("patch", { permissions: { approvals_reviewer: emptyToNull(approvals_reviewer, translate) } })} />
      <ToggleRow label={translate("claude.network")} value={booleanValue(permissions.network_access)} disabled={busy} onChange={(network_access) => dispatch("patch", { permissions: { network_access } })} />
      <TextField label={translate("codex.writableRoots")} value={stringList(permissions.writable_roots).join("\n")} multiline onCommit={(writable_roots) => dispatch("patch", { permissions: { writable_roots: splitLines(writable_roots) } })} />
      <PickerField label={translate("settings.permissionProfile")} value={stringValue(permissions.default_permissions, translate("common.empty"))} values={[translate("common.empty"), ...stringList(structured.permission_profiles)]} disabled={busy || permissions.mode === "legacy"} onSelect={(default_permissions) => dispatch("patch", { permissions: { default_permissions: emptyToNull(default_permissions, translate) } })} />
    </View></Section>
    <Section title={translate("codex.providers")}><View style={styles.split}>
      <NativeTable columns={[{ label: "ID", width: 116 }, { label: translate("providers.baseUrl"), width: 230 }, { label: translate("providers.authentication"), width: 84 }]} rows={providerRows.map((item) => ({ key: identifier(item), cells: [identifier(item), stringValue(item.base_url), stringValue(item.auth_mode, "none")] }))} selectedKey={identifier(provider ?? {})} onSelectionChange={setSelectedProvider} style={styles.codexListTable} />
      <View style={styles.detailPane}>{provider ? <View style={styles.form}><TextField label={translate("providers.providerId")} value={identifier(provider)} onCommit={(id) => patchProvider({ id })} /><TextField label={translate("providers.displayName")} value={stringValue(provider.name)} onCommit={(name) => patchProvider({ name })} /><TextField label={translate("common.endpoint")} value={stringValue(provider.base_url)} onCommit={(base_url) => patchProvider({ base_url })} /><PickerField label={translate("providers.protocol")} value={stringValue(provider.wire_api, "responses")} values={["responses"]} disabled={busy} onSelect={(wire_api) => patchProvider({ wire_api })} /><PickerField label={translate("providers.authentication")} value={stringValue(provider.auth_mode, "none")} values={["none", "env_key", "openai_auth", "command", "bearer"]} disabled={busy} onSelect={(auth_mode) => patchProvider({ auth_mode })} /><TextField label={translate("codex.environmentKey")} value={stringValue(provider.env_key)} onCommit={(env_key) => patchProvider({ env_key })} /><NativeCheckbox label={translate("providers.requiresOpenAIAuth")} value={booleanValue(provider.requires_openai_auth)} disabled={busy} onValueChange={(requires_openai_auth) => patchProvider({ requires_openai_auth })} /><TextField label={translate("providers.authCommand")} value={stringValue(provider.auth_command)} onCommit={(auth_command) => patchProvider({ auth_command })} /></View> : <EmptyState translate={translate} />}</View>
      <View style={styles.listToolRail}><IconButton label="+" title={translate("common.add")} disabled={busy} onPress={() => dispatch("patch", { providers: [...providerRows, { id: `provider-${providers.length + 1}`, name: "", base_url: "", wire_api: "responses", auth_mode: "none" }] })} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !provider} onPress={() => provider && dispatch("patch", { providers: providerRows.filter((item) => identifier(item) !== identifier(provider)) })} /></View>
    </View></Section>
    <Section title={translate("codex.mcpPlugins")}><View style={styles.split}>
      <NativeTable columns={[{ label: translate("settings.serverId"), width: 138 }, { label: translate("codex.transport"), width: 90 }, { label: translate("common.status"), width: 70 }]} rows={mcpServers.map((item) => ({ key: identifier(item), cells: [identifier(item), stringValue(item.transport), booleanValue(item.enabled, true) ? translate("settings.enabled") : translate("settings.disabled")] }))} selectedKey={identifier(mcp ?? {})} onSelectionChange={setSelectedMcp} style={styles.codexListTable} />
      <View style={styles.detailPane}>{mcp ? <View style={styles.form}><TextField label={translate("settings.serverId")} value={identifier(mcp)} onCommit={(id) => patchMcp({ id })} /><PickerField label={translate("codex.transport")} value={stringValue(mcp.transport, "stdio")} values={["stdio", "http"]} disabled={busy} onSelect={(transport) => patchMcp({ transport })} /><TextField label={translate("codex.command")} value={stringValue(mcp.command)} onCommit={(command) => patchMcp({ command })} /><TextField label={translate("webdav.url")} value={stringValue(mcp.url)} onCommit={(url) => patchMcp({ url })} /><ToggleRow label={translate("common.enabled")} value={booleanValue(mcp.enabled, true)} disabled={busy} onChange={(enabled) => patchMcp({ enabled })} /><ToggleRow label={translate("codex.required")} value={booleanValue(mcp.required)} disabled={busy} onChange={(required) => patchMcp({ required })} /></View> : <EmptyState translate={translate} />}</View>
      <View style={styles.listToolRail}><IconButton label="+" title={translate("common.add")} disabled={busy} onPress={() => dispatch("patch", { mcp_servers: [...mcpServers, { id: `mcp-${mcpServers.length + 1}`, transport: "stdio", command: "", enabled: true, required: false }] })} /><IconButton label="−" title={translate("common.delete")} disabled={busy || !mcp} onPress={() => mcp && dispatch("patch", { mcp_servers: mcpServers.filter((item) => identifier(item) !== identifier(mcp)) })} /></View>
    </View><View style={styles.pluginEditor}><NativeTable columns={[{ label: "Plugin", width: 180 }, { label: translate("common.status"), width: 90 }]} rows={plugins.map((item) => ({ key: identifier(item), cells: [identifier(item), booleanValue(item.enabled) ? translate("settings.enabled") : translate("settings.disabled")] }))} selectedKey={identifier(plugin ?? {})} onSelectionChange={setSelectedPlugin} style={styles.pluginTable} />{plugin ? <View style={styles.pluginFields}><TextField label={translate("settings.pluginId")} value={identifier(plugin)} onCommit={(id) => dispatch("patch", { plugins: plugins.map((entry) => identifier(entry) === identifier(plugin) ? { ...entry, id } : entry) })} /><NativeCheckbox label={translate("common.enabled")} value={booleanValue(plugin.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { plugins: plugins.map((entry) => identifier(entry) === identifier(plugin) ? { ...entry, enabled } : entry) })} /></View> : null}</View></Section>
    <Section title={translate("codex.advanced")}><View style={styles.form}><PickerField label={translate("codex.shellEnvironment")} value={stringValue(advanced.shell_environment_inherit, translate("common.empty"))} values={[translate("common.empty"), "all", "core", "none"]} disabled={busy} onSelect={(shell_environment_inherit) => dispatch("patch", { advanced: { shell_environment_inherit: emptyToNull(shell_environment_inherit, translate) } })} /><PickerField label={translate("codex.history")} value={stringValue(advanced.history_persistence, translate("common.empty"))} values={[translate("common.empty"), "save-all", "none"]} disabled={busy} onSelect={(history_persistence) => dispatch("patch", { advanced: { history_persistence: emptyToNull(history_persistence, translate) } })} /><TextField label={translate("codex.agentThreads")} value={stringValue(advanced.agents_max_threads)} keyboardType="numeric" onCommit={(agents_max_threads) => dispatch("patch", { advanced: { agents_max_threads } })} /><TextField label={translate("codex.agentDepth")} value={stringValue(advanced.agents_max_depth)} keyboardType="numeric" onCommit={(agents_max_depth) => dispatch("patch", { advanced: { agents_max_depth } })} /><PickerField label={translate("settings.fileOpener")} value={stringValue(advanced.file_opener, translate("common.empty"))} values={[translate("common.empty"), "vscode", "vscode-insiders", "windsurf", "cursor", "none"]} disabled={busy} onSelect={(file_opener) => dispatch("patch", { advanced: { file_opener: emptyToNull(file_opener, translate) } })} /><PickerField label={translate("settings.mcpCredentialStore")} value={stringValue(advanced.mcp_oauth_credentials_store, translate("common.empty"))} values={[translate("common.empty"), "auto", "file", "keyring"]} disabled={busy} onSelect={(mcp_oauth_credentials_store) => dispatch("patch", { advanced: { mcp_oauth_credentials_store: emptyToNull(mcp_oauth_credentials_store, translate) } })} /></View></Section>
    </>
  } raw={<><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawToml")} domain="codex" document="config" language="toml" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /><RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("codex.rawAuth")} domain="codex" document="auth" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} /></>} />;
}

function SettingsWorkspace({ validationStatus, validationStatusStyle, structuredWidth, onStructuredWidthChange, workspaceWidth, onWorkspaceWidthChange, translate, missingMessage, structured, raw }: { validationStatus?: string; validationStatusStyle?: StyleProp<TextStyle>; structuredWidth: number; onStructuredWidthChange: (width: number) => void; workspaceWidth: number; onWorkspaceWidthChange: (width: number) => void; translate: Translate; missingMessage?: string; structured: React.ReactNode; raw: React.ReactNode }): React.JSX.Element {
  const maxStructuredWidth = workspaceWidth > 0 ? Math.min(680, Math.max(420, workspaceWidth - 501)) : 470;
  const paneWidth = Math.min(structuredWidth, maxStructuredWidth);
  return <View style={styles.codexWorkspaceFrame} onLayout={({ nativeEvent }) => onWorkspaceWidthChange(nativeEvent.layout.width)}>{validationStatus ? <Text style={[styles.codexValidationStatus, validationStatusStyle]}>{validationStatus}</Text> : null}{missingMessage ? <Text style={styles.settingsMissingMessage}>{missingMessage}</Text> : null}<NativeSplitView paneWidth={paneWidth} minPaneWidth={420} maxPaneWidth={maxStructuredWidth} onPaneWidthChange={(width) => onStructuredWidthChange(Math.min(width, maxStructuredWidth))} style={styles.codexSplit}><View style={styles.codexStructuredPane}><Text style={styles.paneHeading}>{translate("settings.structured")}</Text><ScrollView style={styles.codexStructuredScroll} contentContainerStyle={styles.codexStructured}>{structured}</ScrollView></View><View style={styles.codexRawPane}><View style={styles.codexRawIntro}><Text style={styles.paneHeading}>{translate("settings.rawLiveDraft")}</Text><Text style={styles.cardHint}>{translate("settings.rawDraftHint")}</Text></View><View style={styles.codexRawEditors}>{raw}</View></View></NativeSplitView></View>;
}

function FeatureToggles({ value, supported, disabled, onChange, translate }: { value: UnknownRecord; supported: string[]; disabled: boolean; onChange: (features: UnknownRecord) => void; translate: Translate }): React.JSX.Element {
  const keys = [...new Set([...supported, ...Object.keys(value)])];
  return <View style={styles.form}>{keys.length === 0 ? <EmptyState translate={translate} /> : keys.map((key) => <ToggleRow key={key} label={key} value={booleanValue(value[key])} disabled={disabled} onChange={(enabled) => onChange({ ...value, [key]: enabled })} />)}</View>;
}

function ClaudeScreen({ snapshot, ipc, native, busy, translate, dispatch, onSecretState, clearSecret, attachProfile, rawReloadToken }: { snapshot?: CoreSnapshot; ipc: IpcClient; native: NativeLeafAdapter; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear; attachProfile: () => void; rawReloadToken: number }): React.JSX.Element {
  const state = domainState(snapshot, "claude");
  const settings = asRecord(state.settings);
  const permissions = asRecord(settings.permissions);
  const sandbox = asRecord(settings.sandbox);
  const network = asRecord(settings.network);
  const configuredModel = stringValue(settings.model);
  const configuredGatewayUrl = stringValue(settings.gateway_url);
  const [deployment, setDeployment] = useState({ model: configuredModel, base_url: configuredGatewayUrl });
  const [structuredWidth, setStructuredWidth] = useState(470);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const fileMissing = settings.file_exists === false;
  const unavailable = state.available === false;
  useEffect(() => { setDeployment({ model: configuredModel, base_url: configuredGatewayUrl }); }, [configuredGatewayUrl, configuredModel]);
  const validationStatus = unavailable ? translate("settings.claudeUnavailable") : Object.keys(state).length === 0 ? undefined : translate("settings.synchronized");
  const updateDeployment = (key: "model" | "base_url", value: string): void => setDeployment((current) => ({ ...current, [key]: value }));
  return <SettingsWorkspace validationStatus={validationStatus} validationStatusStyle={unavailable ? styles.codexValidationError : styles.codexValidationValid} structuredWidth={structuredWidth} onStructuredWidthChange={setStructuredWidth} workspaceWidth={workspaceWidth} onWorkspaceWidthChange={setWorkspaceWidth} translate={translate} missingMessage={fileMissing ? translate("settings.claudeMissing") : undefined} structured={<>
    <Section title={translate("claude.deployment")}><View style={styles.twoColumnForm}>
      <TextField label={translate("claude.model")} value={deployment.model} onCommit={(value) => updateDeployment("model", value)} />
      <TextField label={translate("claude.gateway")} value={deployment.base_url} onCommit={(value) => updateDeployment("base_url", value)} />
      <NativeSecretField label={translate("claude.token")} hint={settings.token_configured === true ? translate("runtime.secretRetained") : undefined} busy={busy} domain="claude" field="deployment_token" onSecretState={onSecretState} setTitle={translate("common.set")} clearTitle={translate("common.clear")} clearDisabled={busy || settings.token_configured !== true} onClear={() => clearSecret({ domain: "claude", field: "deployment_token" })} />
    </View><View style={styles.actions}><ActionButton title={translate("common.save")} disabled={busy || !deployment.model || !deployment.base_url} onPress={() => dispatch("select_deployment", { deployment })} /></View></Section>
    <Section title={translate("claude.permissions")}><View style={styles.twoColumnForm}>
      <SegmentedField label={translate("claude.permissions")} value={stringValue(settings.permissions_mode, "default")} values={[{ value: "default", label: translate("claude.permission.default") }, { value: "acceptEdits", label: translate("claude.permission.acceptEdits") }, { value: "plan", label: translate("claude.permission.plan") }, { value: "bypassPermissions", label: translate("claude.permission.bypassPermissions") }]} disabled={busy} onSelect={(permissions_mode) => dispatch("patch", { permissions_mode })} />
      {containsPrivateMarker(permissions.allow) ? <InfoPair label={translate("claude.allow")} value={translate("screen.configured")} /> : <TextField label={translate("claude.allow")} value={stringList(permissions.allow).join("\n")} multiline onCommit={(allow) => dispatch("patch", { permissions: { allow: splitLines(allow) } })} />}
      {containsPrivateMarker(permissions.ask) ? <InfoPair label={translate("claude.ask")} value={translate("screen.configured")} /> : <TextField label={translate("claude.ask")} value={stringList(permissions.ask).join("\n")} multiline onCommit={(ask) => dispatch("patch", { permissions: { ask: splitLines(ask) } })} />}
      {containsPrivateMarker(permissions.deny) ? <InfoPair label={translate("claude.deny")} value={translate("screen.configured")} /> : <TextField label={translate("claude.deny")} value={stringList(permissions.deny).join("\n")} multiline onCommit={(deny) => dispatch("patch", { permissions: { deny: splitLines(deny) } })} />}
    </View></Section>
    <Section title={translate("claude.sandbox")}><View style={styles.twoColumnForm}><ToggleRow label={translate("claude.sandbox")} value={booleanValue(sandbox.enabled, true)} disabled={busy} onChange={(enabled) => dispatch("patch", { sandbox: { enabled } })} /><ToggleRow label={translate("claude.network")} value={booleanValue(network.enabled)} disabled={busy} onChange={(enabled) => dispatch("patch", { network: { enabled } })} />{containsPrivateMarker(sandbox.writable_paths) ? <InfoPair label={translate("claude.filesystem")} value={translate("screen.configured")} /> : <TextField label={translate("claude.filesystem")} value={stringList(sandbox.writable_paths).join("\n")} multiline onCommit={(writable_paths) => dispatch("patch", { sandbox: { writable_paths: splitLines(writable_paths) } })} />}</View></Section>
    <Section title={translate("claude.desktop_profile")} action={<ActionButton title={translate("claude.desktop_profile")} disabled={busy} onPress={attachProfile} />}><InfoPair label={translate("claude.profileAttached")} value={settings.desktop_profile_attached === true ? translate("screen.configured") : translate("screen.notConfigured")} /></Section>
  </>} raw={<RawEditor showReload={false} codexPane style={styles.codexRawEditor} label={translate("claude.rawJson")} domain="claude" document="settings" language="json" ipc={ipc} busy={busy} translate={translate} reloadToken={rawReloadToken} />} />;
}

function LegacyRuntimeWorkspace({ snapshot, busy, translate, dispatch, onSecretState, clearSecret }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear }): React.JSX.Element {
  const state = domainState(snapshot, "runtime");
  const settings = asRecords(state.settings).length > 0 ? asRecords(state.settings) : asRecords(state.categories).flatMap((category) => asRecords(category.settings));
  const groups = groupBy(settings, (item) => stringValue(item.category, translate("runtime.categories")));
  const [contentWidth, setContentWidth] = useState(0);
  const oneColumn = contentWidth > 0 && contentWidth < 960;
  return <ScrollView style={styles.runtimeScrollSurface} contentContainerStyle={styles.legacyRuntimeWorkspace} onLayout={({ nativeEvent }) => setContentWidth(nativeEvent.layout.width)}>{Object.keys(groups).length === 0 ? <EmptyState translate={translate} /> : Object.entries(groups).map(([category, entries]) => <Section key={category} title={category}><View style={[styles.runtimeTwoColumnForm, oneColumn && styles.runtimeOneColumnForm]}>{entries.map((item) => <RuntimeField key={identifier(item)} item={item} busy={busy} translate={translate} dispatch={dispatch} onSecretState={onSecretState} clearSecret={clearSecret} />)}</View></Section>)}</ScrollView>;
}

function RuntimeField({ item, busy, translate, dispatch, onSecretState, clearSecret }: { item: UnknownRecord; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void; clearSecret: NativeSecretClear }): React.JSX.Element {
  const key = identifier(item);
  const label = stringValue(item.label, key);
  const kind = stringValue(item.kind, "text");
  const storageKind = stringValue(item.storage_kind, kind);
  const value = stringValue(item.value);
  const unit = stringValue(item.unit);
  let control: React.ReactNode;
  let action: React.ReactNode;
  if (kind === "boolean" || kind === "toggle" || kind === "bool" || kind === "bool_auto") {
    control = <NativeCheckbox label="" value={booleanValue(item.value)} disabled={busy} onValueChange={(next) => dispatch("set_setting", { key, value: kind === "bool_auto" ? (next ? "auto" : "off") : next })} style={styles.runtimeBooleanControl} />;
  } else if (kind === "select" || kind === "choice" || kind === "enum") {
    control = <NativePicker labels={stringList(item.options)} selectedValue={value} disabled={busy} onChange={({ nativeEvent }) => dispatch("set_setting", { key, value: nativeEvent.value })} style={styles.runtimeValueControl} />;
  } else if (item.secret === true) {
    control = <NativeSecretInputControl label={label} hint={item.retained === true ? translate("runtime.secretRetained") : undefined} busy={busy} domain="runtime" field="setting" target={key} onSecretState={onSecretState} setTitle={translate("common.set")} />;
    action = <ActionButton title={item.will_clear === true ? "Will Clear" : translate("common.clear")} disabled={busy || item.retained !== true || item.will_clear === true} onPress={() => clearSecret({ domain: "runtime", field: "setting", target: key })} />;
  } else {
    control = <RuntimeValueField label={label} value={value} keyboardType={["number", "integer", "int", "float", "mb"].includes(storageKind) ? "numeric" : undefined} onCommit={(next) => dispatch("set_setting", { key, value: next })} />;
  }
  return <View style={styles.runtimeField}><View style={styles.runtimeInputRow}><Text numberOfLines={1} style={styles.runtimeFieldLabel}>{label}</Text><View style={styles.runtimeValueSlot}>{control}</View><Text numberOfLines={1} style={styles.runtimeUnit}>{unit}</Text><View style={styles.runtimeActionSlot}>{action}</View></View><RuntimeFieldMeta item={item} translate={translate} /></View>;
}

function RuntimeFieldMeta({ item, translate }: { item: UnknownRecord; translate: Translate }): React.JSX.Element {
  const defaultValue = stringValue(item.default, "(empty)");
  const help = stringValue(item.help);
  return <View style={styles.runtimeHelpSlot}><Text style={styles.runtimeHelpText}>{translate("common.default")}: {defaultValue}{help ? `\n${help}` : ""}</Text></View>;
}

function RuntimeValueField({ label, value, keyboardType, onCommit }: { label: string; value: string; keyboardType?: "default" | "numeric"; onCommit: (value: string) => void | Promise<void> }): React.JSX.Element {
  const [draft, setDraft] = useState(value);
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(label));
  const draftRef = useRef(draft);
  const valueRef = useRef(value);
  useEffect(() => setDraft(value), [value]);
  useEffect(() => { draftRef.current = draft; }, [draft]);
  useEffect(() => { valueRef.current = value; }, [value]);
  const commit = (): void | Promise<void> => {
    if (draftRef.current === valueRef.current) return;
    const submitted = draftRef.current;
    valueRef.current = submitted;
    return Promise.resolve(onCommit(submitted)).catch((reason: unknown) => {
      valueRef.current = value;
      throw reason;
    });
  };
  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset: () => setDraft(valueRef.current) });
    return () => registry?.register(fieldId.current);
  }, [registry, onCommit]);
  return <NativeTextField style={[styles.input, styles.runtimeValueControl]} value={draft} onChangeText={setDraft} onBlur={() => { void commit(); }} onSubmitEditing={() => { void commit(); }} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />;
}

function ConfigurationPackageScreen({ native, busy, preview, translate, onImport, onExport, onValidate, onApply, onCancel }: { native: NativeLeafAdapter; busy: boolean; preview?: PackageImportResult; translate: Translate; onImport: (sections: ConfigDomain[]) => void; onExport: (sections: ConfigDomain[]) => void; onValidate: () => void; onApply: () => void; onCancel: () => void }): React.JSX.Element {
  const [selected, setSelected] = useState<Array<"providers_models" | "runtime">>(packageSections());
  const [mode, setMode] = useState<"import" | "export">("import");
  useEffect(() => { void native.window.setContentSize?.(420, mode === "import" ? 132 : 208); }, [mode, native]);
  const flip = (section: "providers_models" | "runtime"): void => setSelected((current) => current.includes(section) ? current.filter((item) => item !== section) : [...current, section]);
  const sections = mode === "export" ? <View style={styles.packageSections}><Text style={styles.packageSectionTitle}>Sections</Text>{packageSections().map((section) => <NativeCheckbox key={section} label={translate(section === "providers_models" ? "package.providersModels" : "package.runtime")} value={selected.includes(section)} disabled={busy} onValueChange={() => flip(section)} />)}</View> : null;
  return <View style={[styles.legacyPackageDialog, mode === "import" ? styles.legacyPackageDialogImport : styles.legacyPackageDialogExport]}><WindowTabs values={[{ id: "import", title: "Import" }, { id: "export", title: "Export" }]} selected={mode} onSelect={(value) => setMode(value as "import" | "export")} style={styles.packageModeTabs} />{sections}<View style={styles.packageButtonRow}><ActionButton title={translate("menu.cancel")} disabled={busy} onPress={onCancel} /><ActionButton primary title={mode === "import" ? "Choose File…" : "Export…"} disabled={busy || (mode === "export" && selected.length === 0)} onPress={() => mode === "import" ? onImport(packageSections()) : onExport(selected)} /></View>{preview ? <Section title={translate("package.previewTitle")}><Text style={styles.cardHint}>{translate("package.previewMessage", { count: preview.draft_domains.length })}</Text>{preview.draft_domains.map((section) => <View key={section} style={styles.rowBetween}><Text style={styles.cardTitle}>{translate(section === "providers_models" ? "package.providersModels" : "package.runtime")}</Text><Text style={styles.cardHint}>{preview.preview[section]?.will_replace_draft ? translate("package.replacedDraft") : translate("common.staged")}</Text></View>)}<Text style={styles.warning}>{translate("package.sensitiveWarning")}</Text><View style={styles.actions}><ActionButton title={translate("screen.validate")} disabled={busy} onPress={onValidate} /><ActionButton primary title={translate("menu.apply")} disabled={busy} onPress={onApply} /><ActionButton title={translate("menu.cancel")} disabled={busy} onPress={onCancel} /></View></Section> : null}</View>;
}

function LegacyWebDavWorkspace({ snapshot, busy, translate, dispatch, onSecretState }: { snapshot?: CoreSnapshot; busy: boolean; translate: Translate; dispatch: Dispatch; onSecretState: (state: SecretState) => void }): React.JSX.Element {
  const state = domainState(snapshot, "webdav");
  return <View style={styles.legacyWebDavForm}><View style={styles.webdavFormRows}>
    <TextField label="URL" value={stringValue(state.url)} labelWidth={94} controlWidth={510} onCommit={(url) => dispatch("patch", { url })} />
    <TextField label={translate("webdav.username")} value={stringValue(state.username)} labelWidth={94} controlWidth={510} onCommit={(username) => dispatch("patch", { username })} />
    <LegacyWebDavPasswordField configured={snapshot?.webdav.password.present === true} busy={busy} onSecretState={onSecretState} />
    <TextField label="Remote File" value={stringValue(state.remote_name)} labelWidth={94} controlWidth={510} onCommit={(remote_name) => dispatch("patch", { remote_name })} />
    <TextField label="Sync Every" value={stringValue(state.sync_interval)} labelWidth={94} controlWidth={140} suffix="minutes" keyboardType="numeric" onCommit={(sync_interval) => dispatch("patch", { sync_interval })} />
    <TextField label="HTTP Timeout" value={stringValue(state.timeout)} labelWidth={94} controlWidth={140} suffix="seconds" keyboardType="numeric" onCommit={(timeout) => dispatch("patch", { timeout })} />
  </View></View>;
}

function LegacyWebDavPasswordField({ configured, busy, onSecretState }: { configured: boolean; busy: boolean; onSecretState: (state: SecretState) => void }): React.JSX.Element {
  const [commitRequest, setCommitRequest] = useState(0);
  const [resetRequest, setResetRequest] = useState(0);
  const [status, setStatus] = useState("ready");
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol("WebDAV Password"));
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
  useEffect(() => {
    registry?.register(fieldId.current, { commit: requestCommit, reset: () => setResetRequest((current) => current + 1) });
    return () => {
      pendingCommit.current?.resolve();
      pendingCommit.current = undefined;
      registry?.register(fieldId.current);
    };
  }, [registry, requestCommit]);
  return <View style={styles.formRow}><Text style={[styles.formRowLabel, { width: 94 }]}>Password</Text><View style={[styles.formRowControl, styles.webdavWideControl]}><NativeSecureTextInput domain="webdav" field="password" label="Password" placeholder={configured ? "leave blank to keep current password" : "optional"} disabled={busy || status === "saving"} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(state) => {
    setStatus(state.status);
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

function LegacyLogsWorkspace({ snapshot, ipc, onSnapshot, busy, translate, dispatch, requestedTab }: { snapshot?: CoreSnapshot; ipc: IpcClient; onSnapshot: (next: CoreSnapshot) => void; busy: boolean; translate: Translate; dispatch: Dispatch; requestedTab?: typeof LOG_TABS[number] }): React.JSX.Element {
  const [selected, setSelected] = useState<typeof LOG_TABS[number]>("requests");
  useEffect(() => { if (requestedTab) setSelected(requestedTab); }, [requestedTab]);
  const active = snapshot?.logs[selected];
  useEffect(() => {
    let activeRequest = true;
    const interval = setInterval(() => {
      if (active?.paused) return;
      void ipc.snapshot().then((next) => { if (activeRequest) onSnapshot(next); }).catch(() => undefined);
    }, selected === "online-usage" ? 8000 : 800);
    return () => { activeRequest = false; clearInterval(interval); };
  }, [active?.paused, ipc, onSnapshot, selected]);
  const records = active?.records ?? [];
  const text = records.length > 0 ? records.map((record) => typeof record === "string" ? record : JSON.stringify(record)).join("\n") : active?.available ? translate("logs.empty") : translate("logs.loading");
  return <View style={styles.legacyLogsWindow}><View style={styles.legacyLogsToolbar}><Text style={styles.toolbarLabel}>{translate("common.filter")}</Text><NativeTextField style={styles.logFilterInput} value={active?.filter ?? ""} placeholder="Filter current tab" onChangeText={(filter) => { void dispatch("logs.set_filter", { tab: selected, filter }, "logs"); }} accessibilityLabel={translate("common.filter")} /><View style={styles.toolbarSpacer} /><ActionButton title={active?.paused ? translate("common.resume") : translate("common.pause")} disabled={busy} onPress={() => dispatch(active?.paused ? "logs.resume" : "logs.pause", { tab: selected }, "logs")} /><ActionButton title="Clear View" disabled={busy} onPress={() => dispatch("logs.clear", { tab: selected }, "logs")} /><ActionButton title={translate("common.refresh")} disabled={busy} onPress={() => dispatch("logs.refresh", { tab: selected }, "logs")} /></View><NativeSegmentedControl labels={LOG_TABS.map((tab) => logTitle(tab, translate))} selectedValue={logTitle(selected, translate)} onChange={({ nativeEvent }) => { const tab = LOG_TABS[nativeEvent.index]; if (tab) setSelected(tab); }} style={styles.legacyLogsTabs} /><NativeTextEditor value={text} documentKey={`logs:${selected}`} readOnly wrap={false} style={styles.legacyLogEditor} /><View style={styles.logInfoBar}><Text style={styles.cardHint}>{active?.line_count ?? 0} lines</Text></View></View>;
}

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }): React.JSX.Element {
  return <View style={styles.section}><View style={styles.sectionHeader}><Text style={styles.sectionTitle}>{title}</Text>{action}</View>{children}</View>;
}

function EmptyState({ translate }: { translate: Translate }): React.JSX.Element { return <Text style={styles.empty}>{translate("screen.noData")}</Text>; }

function ActionButton({ title, onPress, disabled, primary, danger, style }: { title: string; onPress: () => void; disabled?: boolean; primary?: boolean; danger?: boolean; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeButton title={title} disabled={disabled} primary={primary} destructive={danger} onPress={onPress} style={style} />;
}

function TextField({ label, value, onCommit, hint, secret, multiline, keyboardType, stacked, labelWidth, labelAlign, controlWidth, suffix }: { label: string; value: string; onCommit: (value: string) => void | Promise<void>; hint?: string; secret?: boolean; multiline?: boolean; keyboardType?: "default" | "numeric"; stacked?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number; suffix?: string }): React.JSX.Element {
  const [draft, setDraft] = useState(value);
  const registry = useContext(PendingFieldContext);
  const fieldId = useRef(Symbol(label));
  const draftRef = useRef(draft);
  const valueRef = useRef(value);
  useEffect(() => setDraft(value), [value]);
  useEffect(() => { draftRef.current = draft; }, [draft]);
  useEffect(() => { valueRef.current = value; }, [value]);
  const commit = (): void | Promise<void> => {
    if (draftRef.current === valueRef.current) return;
    const submitted = draftRef.current;
    valueRef.current = submitted;
    return Promise.resolve(onCommit(submitted)).catch((reason: unknown) => {
      valueRef.current = value;
      throw reason;
    });
  };
  useEffect(() => {
    registry?.register(fieldId.current, { commit, reset: () => setDraft(valueRef.current) });
    return () => registry?.register(fieldId.current);
  }, [registry, onCommit]);
  return <View style={[styles.formRow, (stacked || multiline) && styles.formRowStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }, (stacked || multiline) && styles.formRowLabelStacked]}>{label}</Text><View style={[styles.formRowControl, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]}><NativeTextField style={[styles.input, multiline && styles.textArea]} value={draft} onChangeText={setDraft} onBlur={() => { void commit(); }} onSubmitEditing={multiline ? undefined : () => { void commit(); }} multiline={multiline} secureTextEntry={secret} autoCapitalize="none" autoCorrect={false} keyboardType={keyboardType} accessibilityLabel={label} />{hint ? <Text style={styles.fieldHint}>{hint}</Text> : null}</View>{suffix ? <Text style={styles.fieldHint}>{suffix}</Text> : null}</View>;
}

function NativeSecretInputControl({ label, hint, busy, domain, field, target, onSecretState, setTitle = "Set", setBelow, onSetReady, inputMinWidth }: { label: string; hint?: string; busy: boolean; domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; onSecretState: (state: SecretState) => void; setTitle?: string; setBelow?: boolean; onSetReady?: (requestSet: () => void, saving: boolean) => void; inputMinWidth?: number }): React.JSX.Element {
  const [commitRequest, setCommitRequest] = useState(0);
  const [resetRequest, setResetRequest] = useState(0);
  const [status, setStatus] = useState("ready");
  const requestCommit = (): void => setCommitRequest((current) => current + 1);
  useEffect(() => { onSetReady?.(requestCommit, status === "saving"); }, [onSetReady, status]);
  return <View style={styles.nativeSecretControl}><NativeSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={hint ?? ""} disabled={busy} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(state) => {
    setStatus(state.status);
    if (state.status === "saved") {
      setResetRequest((current) => current + 1);
      onSecretState(state);
    }
  }} style={[styles.nativeSecretInput, inputMinWidth === undefined ? null : { minWidth: inputMinWidth }]} />{!setBelow ? <NativeButton title={setTitle} compact disabled={busy || status === "saving"} onPress={requestCommit} style={styles.nativeSecretSetButton} /> : null}</View>;
}

function NativeSecretField({ label, hint, busy, domain, field, target, onSecretState, labelWidth, labelAlign, setTitle = "Set", clearTitle, clearDisabled, onClear, actionsBelow }: { label: string; hint?: string; busy: boolean; domain: "providers_models" | "codex" | "claude" | "runtime" | "webdav"; field: string; target?: string; onSecretState: (state: SecretState) => void; labelWidth?: number; labelAlign?: "left" | "right"; setTitle?: string; clearTitle?: string; clearDisabled?: boolean; onClear?: () => Promise<void>; actionsBelow?: boolean }): React.JSX.Element {
  const setAction = useRef<() => void>(() => undefined);
  const [saving, setSaving] = useState(false);
  const handleSetReady = React.useCallback((requestSet: () => void, nextSaving: boolean): void => { setAction.current = requestSet; setSaving(nextSaving); }, []);
  return <View style={[styles.formRow, actionsBelow && styles.formRowSecretStacked]}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><View style={styles.formRowControl}>{actionsBelow ? <><NativeSecretInputControl label={label} hint={hint} busy={busy} domain={domain} field={field} target={target} onSecretState={onSecretState} setTitle={setTitle} setBelow onSetReady={handleSetReady} inputMinWidth={110} /><View style={styles.secretFieldButtons}><NativeButton title={setTitle} compact disabled={busy || saving} onPress={() => setAction.current()} style={styles.secretFieldButton} />{onClear ? <NativeButton title={clearTitle ?? "Clear"} compact disabled={clearDisabled ?? busy} onPress={() => { void onClear(); }} style={styles.secretFieldButton} /> : null}</View></> : <View style={styles.secretFieldActions}><NativeSecretInputControl label={label} hint={hint} busy={busy} domain={domain} field={field} target={target} onSecretState={onSecretState} setTitle={setTitle} />{onClear ? <ActionButton title={clearTitle ?? "Clear"} disabled={clearDisabled ?? busy} onPress={() => { void onClear(); }} /> : null}</View>}{hint && actionsBelow ? <Text style={styles.fieldHint}>{hint}</Text> : null}</View></View>;
}

function ToggleRow({ label, value, onChange, disabled }: { label: string; value: boolean; onChange: (value: boolean) => void; disabled?: boolean }): React.JSX.Element {
  return <View style={styles.toggleRow}><NativeCheckbox label={label} value={value} onValueChange={onChange} disabled={disabled} /></View>;
}

function SegmentedField({ label, value, values, onSelect, disabled }: { label: string; value: string; values: Array<string | { value: string; label: string }>; onSelect: (value: string) => void; disabled?: boolean }): React.JSX.Element {
  const options = values.map((option) => typeof option === "string" ? { value: option, label: option } : option);
  const selectedValue = options.find((option) => option.value === value)?.label ?? options[0]?.label ?? "";
  return <View style={styles.formRow}><Text style={styles.formRowLabel}>{label}</Text><NativeSegmentedControl labels={options.map((option) => option.label)} selectedValue={selectedValue} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onSelect(option.value); }} style={styles.formRowControl} /></View>;
}

function PickerField({ label, value, values, onSelect, disabled, labelWidth, labelAlign, controlWidth }: { label: string; value: string; values: string[]; onSelect: (value: string) => void; disabled?: boolean; labelWidth?: number; labelAlign?: "left" | "right"; controlWidth?: number }): React.JSX.Element {
  return <View style={styles.formRow}><Text style={[styles.formRowLabel, labelWidth === undefined ? null : { width: labelWidth }, labelAlign === undefined ? null : { textAlign: labelAlign }]}>{label}</Text><NativePicker labels={values} selectedValue={value} disabled={disabled} onChange={({ nativeEvent }) => onSelect(nativeEvent.value)} style={[styles.picker, controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]} /></View>;
}

function RawEditor({ label, domain, document, language, ipc, busy, translate, showReload = true, codexPane = false, reloadToken = 0, style }: { label: string; domain: "codex" | "claude"; document: "config" | "auth" | "settings"; language: "toml" | "json"; ipc: IpcClient; busy: boolean; translate: Translate; showReload?: boolean; codexPane?: boolean; reloadToken?: number; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  const [editorToken, setEditorToken] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [staged, setStaged] = useState(false);
  const [error, setError] = useState<string>();
  const [reloadNonce, setReloadNonce] = useState(0);
  useEffect(() => {
    let active = true;
    setEditorToken(undefined);
    setLoading(true);
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
  }, [document, domain, ipc, reloadNonce, reloadToken, translate]);
  return <View style={[styles.rawEditor, codexPane && styles.codexRawEditorBase, style]}><View style={[styles.rawEditorHeader, codexPane && styles.codexRawEditorHeader]}><Text style={[styles.fieldLabel, codexPane && styles.codexRawEditorLabel]}>{label}</Text>{showReload ? <ActionButton title={translate("menu.reload")} disabled={busy || loading} onPress={() => setReloadNonce((value) => value + 1)} /> : null}</View>{codexPane ? null : <Text style={styles.fieldHint}>{translate("settings.rawProtectedHint")}</Text>}{editorToken ? <NativeSecureTextEditor editorToken={editorToken} language={language} style={[styles.rawNativeEditor, codexPane && styles.codexRawNativeEditor]} onEditorState={({ status, error: nativeError }) => { if (nativeError) { setError(translate("error.coreUnavailable")); return; } setError(undefined); setStaged(status === "saved"); }} /> : <View style={[styles.rawEditorLoading, codexPane && styles.codexRawEditorLoading]}><Text style={styles.cardHint}>{loading ? translate("common.loading") : translate("error.coreUnavailable")}</Text></View>}{staged ? <Text style={styles.result}>{translate("common.staged")}</Text> : null}{error ? <Text style={styles.error}>{error}</Text> : null}</View>;
}

function InfoPair({ label, value, toolTip }: { label: string; value: string; toolTip?: string }): React.JSX.Element { return <View style={styles.infoPair}><Text style={styles.fieldLabel}>{label}</Text><Text style={styles.cardHint} accessibilityHint={toolTip}>{value}</Text></View>; }

function InspectorInfoRow({ label, value, toolTip }: { label: string; value: string; toolTip?: string }): React.JSX.Element { return <View style={styles.inspectorInfoRow}><Text style={styles.inspectorInfoLabel}>{label}</Text><Text style={styles.inspectorInfoValue} accessibilityHint={toolTip}>{value}</Text></View>; }

function compactNumber(value: number): string {
  const absolute = Math.abs(value);
  const [scaled, suffix] = absolute >= 1_000_000_000 ? [value / 1_000_000_000, "B"] : absolute >= 1_000_000 ? [value / 1_000_000, "M"] : absolute >= 1_000 ? [value / 1_000, "K"] : [value, ""];
  return `${Number.isInteger(scaled) ? scaled.toFixed(0) : scaled.toFixed(2)}${suffix}`;
}

function billingBalanceValue(value: unknown): string {
  const balance = asRecord(asRecord(value).balance);
  const number = numberValue(balance.value, Number.NaN);
  if (!Number.isFinite(number) || number < 0 || (stringValue(balance.kind) !== "balance" && stringValue(balance.kind) !== "remaining_quota")) return "N/A";
  return [compactNumber(number), stringValue(balance.unit)].filter(Boolean).join(" ");
}

function billingUsageValue(value: unknown): string {
  const usage = asRecord(value);
  const used = numberValue(usage.used, Number.NaN);
  const limit = numberValue(usage.limit, Number.NaN);
  if (!Number.isFinite(used) || !Number.isFinite(limit)) return "N/A";
  return `${compactNumber(used)} / ${compactNumber(limit)}${stringValue(usage.unit) ? ` ${stringValue(usage.unit)}` : ""}`;
}

function billingMultiplierValue(value: unknown): string {
  const multiplier = asRecord(value);
  const number = numberValue(multiplier.value, Number.NaN);
  return stringValue(multiplier.status) === "ok" && Number.isFinite(number) ? `${number.toFixed(2)}x` : "N/A";
}

function billingStatusText(status: string): string {
  return ({ ok: "Live", partial: "Partial", timeout: "Timed out", network_error: "Offline", auth_error: "Auth error", rate_limited: "Rate limited", http_error: "HTTP error", credential_unavailable: "No credential", permission_required: "Permission required", invalid_config: "Invalid config", unsupported: "N/A" } as Record<string, string>)[status] ?? "Unavailable";
}

function billingToolTip(model: UnknownRecord): string | undefined {
  const billing = asRecord(model.billing);
  if (Object.keys(billing).length === 0) return undefined;
  const status = stringValue(billing.status);
  const balance = billingBalanceValue(billing);
  const usage = billingUsageValue(model.usage);
  const multiplier = billingMultiplierValue(model.multiplier);
  return [`Status: ${billingStatusText(status)}`, balance !== "N/A" ? `Balance ${balance}` : stringValue(billing.detail), usage !== "N/A" ? `Usage ${usage}` : "", multiplier !== "N/A" ? `Multiplier ${multiplier}` : ""].filter(Boolean).join("\n") || undefined;
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
function emptyToNull(value: string, translate?: Translate): string | null { return value === "(Empty)" || value === translate?.("common.empty") ? null : value; }
function uniqueKeyName(existing: string[]): string { let suffix = 1; let value = "key"; while (existing.includes(value)) { suffix += 1; value = `key-${suffix}`; } return value; }
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
  windowSurface: { flex: 1, backgroundColor: systemColors.window }, windowContent: { flexGrow: 1, paddingHorizontal: 16, paddingTop: 16, paddingBottom: 8, gap: 12 }, windowContentFixed: { minHeight: 0 }, legacyProvidersContent: { paddingBottom: 8, gap: 8 }, legacySettingsContent: { paddingHorizontal: 20, paddingTop: 14, paddingBottom: 0, gap: 8 }, legacyLogsContent: { paddingHorizontal: 8, paddingTop: 8, paddingBottom: 0 }, legacyRuntimeContent: { paddingHorizontal: 20, paddingTop: 14, paddingBottom: 0 }, legacyWebDavContent: { paddingHorizontal: 20, paddingTop: 18, paddingBottom: 0 }, windowTitleBlock: { paddingHorizontal: 20, paddingTop: 18, gap: 5 }, windowTitle: { color: systemColors.label, fontSize: 16, fontWeight: "600" }, windowSubtitle: { color: systemColors.secondaryLabel, fontSize: 13, maxWidth: 940 }, validationText: { color: systemColors.red, fontSize: 12 },
  legacyFooter: { minHeight: 48, flexDirection: "row", alignItems: "center", paddingHorizontal: 20, paddingBottom: 16, gap: 8 }, legacyFooterExact: { minHeight: 32, height: 32, paddingHorizontal: 0, paddingBottom: 0 }, footerStatus: { color: systemColors.secondaryLabel, fontSize: 12, flexShrink: 1 }, footerSpacer: { flex: 1 }, footerButtons: { flexDirection: "row", alignItems: "center", gap: 8 }, legacyWideButton: { minWidth: 92 },
  providerToolbar: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, toolbarSpacer: { flex: 1 }, windowTabs: { width: 224, height: 28 }, windowTab: {}, windowTabSelected: {}, windowTabText: {},
  legacyProvidersLayout: { flex: 1, minHeight: 0, gap: 8 }, providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 12 }, providerLeftColumn: { flex: 1, minWidth: 0, maxWidth: 668, gap: 14 }, providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: 12 }, importSourcePicker: { width: 152, height: 26 }, fetchKeyPicker: { width: 190, height: 26, marginRight: 8, flexShrink: 0 }, providerThreePane: { flex: 1, minHeight: 0 }, providerListPane: { width: 196, minWidth: 196, maxWidth: 196, flexGrow: 0, flexShrink: 0 }, modelListPane: { flex: 1, minWidth: 0 }, providerInspectorPane: { minWidth: 340 }, legacyTablePane: { flex: 1, minWidth: 0, gap: 8 }, legacyTablePaneWide: { flex: 1, minWidth: 0 }, tableTitleRow: { height: 28, flexDirection: "row", alignItems: "center" }, tableTitle: { color: systemColors.label, fontSize: 13, fontWeight: "600" }, tableActions: { marginLeft: "auto", flexDirection: "row", gap: 8 }, iconButton: { minWidth: 24, width: 24, minHeight: 24, height: 24, alignItems: "center", justifyContent: "center" }, iconButtonText: { color: systemColors.secondaryLabel, fontSize: 17 }, tableHeader: { height: 28, flexDirection: "row", alignItems: "center", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.window }, tableHeaderText: { color: systemColors.label, fontSize: 12, paddingHorizontal: 8, fontWeight: "500" }, tableScroll: { flex: 1, minHeight: 0, borderWidth: 1, borderTopWidth: 0, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, tableRows: { flexGrow: 1 }, tableRow: { minHeight: 28, flexDirection: "row", alignItems: "center" }, tableRowSelected: { backgroundColor: systemColors.control }, tableCellText: { color: systemColors.label, fontSize: 13, paddingHorizontal: 8 }, providerNameColumn: { flex: 1 }, countColumn: { width: 48, textAlign: "right" }, modelNameColumn: { width: 118 }, modelUpstreamColumn: { flex: 1, minWidth: 120 }, modelBillingColumn: { width: 112 }, routeModelColumn: { width: 170 }, routeOrderColumn: { width: 56, textAlign: "right" }, routeProviderColumn: { width: 130 }, routeUpstreamColumn: { flex: 1, minWidth: 164 }, tableBottomRow: { minHeight: 30, flexDirection: "row", alignItems: "center" }, nativeProviderTable: { flex: 1, minHeight: 0 }, nativeModelTable: { flex: 1, minHeight: 0 }, nativeRouteTable: { flex: 1, minHeight: 0 }, providerInspector: { width: 340, minWidth: 340, maxWidth: 340, flexGrow: 0, flexShrink: 0 }, providerEditorContent: { paddingTop: 4, paddingHorizontal: 14, paddingRight: 6, paddingBottom: 12, gap: 10 }, providerEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, providerEditorHeading: { flex: 1, color: systemColors.secondaryLabel, fontSize: 13, fontWeight: "600" }, providerReturnToModel: { flexShrink: 1 }, providerEditorSection: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 4, gap: 10 }, providerEnabledRow: { minHeight: 24, flexDirection: "row", alignItems: "center" }, inspectorContent: { paddingTop: 4, paddingHorizontal: 14, paddingRight: 6, paddingBottom: 12, gap: 10 }, inspectorBody: { gap: 10 }, modelBreadcrumb: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 5 }, breadcrumbProvider: { flexShrink: 1, color: systemColors.label, fontSize: 13, fontWeight: "600" }, breadcrumbSeparator: { color: systemColors.secondaryLabel, fontSize: 13 }, inspectorHeading: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: 13, fontWeight: "600" }, inspectorDivider: { height: 1, backgroundColor: systemColors.separator }, inspectorEnabledRow: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }, billingGrid: { gap: 5, paddingVertical: 4 }, inspectorInfoRow: { minHeight: 20, flexDirection: "row", alignItems: "center", gap: 8 }, inspectorInfoLabel: { width: 96, color: systemColors.label, fontSize: 12 }, inspectorInfoValue: { color: systemColors.secondaryLabel, fontSize: 12 }, protocolField: { gap: 4 }, protocolFieldLabel: { width: 96, color: systemColors.label, fontSize: 12 }, protocolRows: { gap: 4 }, protocolRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 3 }, protocolRank: { width: 20, textAlign: "right", color: systemColors.secondaryLabel, fontSize: 12 }, protocolCheckbox: { flex: 1, minWidth: 112 }, providerKeysEditor: { gap: 7 }, providerKeysHeading: { color: systemColors.secondaryLabel, fontSize: 12, fontWeight: "600" }, providerKeyGrid: { flexDirection: "row", gap: 12, minHeight: 118 }, providerKeyTable: { width: 130, minHeight: 112 }, providerKeyFields: { flex: 1, minWidth: 170, gap: 8 }, providerKeyActions: { flexDirection: "row", gap: 8 },
  legacyCodexWorkspace: { minHeight: 520 }, codexWorkspaceFrame: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 }, codexValidationStatus: { flexShrink: 0, marginHorizontal: 8, fontSize: 12 }, settingsMissingMessage: { flexShrink: 0, marginHorizontal: 8, color: systemColors.secondaryLabel, fontSize: 12 }, codexValidationValid: { color: systemColors.green }, codexValidationWarning: { color: systemColors.brown }, codexValidationError: { color: systemColors.red }, codexSplit: { flex: 1, minWidth: 0, minHeight: 0 }, codexStructuredPane: { flex: 1, minWidth: 420, paddingHorizontal: 8 }, codexStructuredScroll: { flex: 1, minWidth: 420, marginTop: 7 }, codexStructured: { flexGrow: 1, gap: 16, paddingHorizontal: 16, paddingTop: 14, paddingBottom: 16 }, codexRawPane: { flex: 1, flexShrink: 1, minWidth: 0, minHeight: 0, gap: 8, paddingHorizontal: 8, overflow: "visible" }, codexRawIntro: { flexShrink: 0, gap: 4 }, codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0, gap: 10 }, codexRawEditorBase: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 180, gap: 5 }, codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0, minHeight: 180 }, codexRawEditorHeader: { minHeight: 18 }, codexRawEditorLabel: { fontFamily: Platform.select({ macos: "Menlo", windows: "Cascadia Mono", default: "monospace" }), fontWeight: "600" }, codexRawNativeEditor: { minHeight: 0 }, codexRawEditorLoading: { minHeight: 0 }, paneHeading: { color: systemColors.label, fontSize: 14, fontWeight: "600" }, section: { borderTopWidth: 1, borderTopColor: systemColors.separator, paddingTop: 12, gap: 9 }, sectionHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }, sectionTitle: { color: systemColors.label, fontSize: 13, fontWeight: "600" }, split: { flexDirection: "row", borderWidth: 1, borderColor: systemColors.separator, minHeight: 150, backgroundColor: systemColors.textBackground }, codexListTable: { flex: 1, minWidth: 260, minHeight: 150 }, listToolRail: { width: 32, paddingTop: 8, alignItems: "center", gap: 5, borderLeftWidth: 1, borderLeftColor: systemColors.separator }, pluginEditor: { minHeight: 128, flexDirection: "row", alignItems: "flex-start", gap: 12 }, pluginTable: { flex: 1, minWidth: 260, minHeight: 128 }, pluginFields: { flex: 1, minWidth: 220, gap: 7 }, masterPane: { width: "36%", minWidth: 220, borderRightWidth: 1, borderColor: systemColors.separator, padding: 8 }, detailPane: { flex: 1, minWidth: 240, padding: 12 }, listRow: { minHeight: 28, paddingHorizontal: 8, paddingVertical: 5 }, listRowSelected: { backgroundColor: systemColors.control }, listText: { flex: 1 },
  legacyRuntimeWorkspace: { padding: 14, gap: 12 }, runtimeScrollSurface: { flex: 1, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, runtimeTwoColumnForm: { flexDirection: "row", flexWrap: "wrap", columnGap: 20, rowGap: 8 }, runtimeOneColumnForm: { flexDirection: "column", flexWrap: "nowrap" }, runtimeField: { minWidth: 486, flexGrow: 1, flexBasis: 486, gap: 4 }, runtimeInputRow: { height: 26, flexDirection: "row", alignItems: "center", gap: 8 }, runtimeFieldLabel: { width: 150, flexShrink: 0, color: systemColors.label, fontSize: 12, textAlign: "right" }, runtimeValueSlot: { width: 180, height: 26, flexShrink: 0, justifyContent: "center" }, runtimeValueControl: { width: 180, minWidth: 180, height: 26 }, runtimeBooleanControl: { width: 180, minWidth: 180, minHeight: 26 }, runtimeUnit: { width: 60, flexShrink: 0, color: systemColors.secondaryLabel, fontSize: 12 }, runtimeActionSlot: { width: 72, height: 26, flexShrink: 0, justifyContent: "center" }, runtimeHelpSlot: { marginLeft: 158, paddingTop: 4 }, runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: 11, lineHeight: 15 },
  legacyWebDavForm: { flex: 1, gap: 18, paddingTop: 0 }, webdavFormRows: { gap: 10 }, webdavWideControl: { width: 510, flex: 0 }, webdavPasswordInput: { width: 510, height: 24 }, webdavFooterLeading: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 10, flexShrink: 1 }, webdavProbeStatus: { flexShrink: 1, color: systemColors.secondaryLabel, fontSize: 12 }, legacyPackageDialog: { flex: 1, alignSelf: "stretch", paddingHorizontal: 4 }, legacyPackageDialogImport: { minHeight: 100 }, legacyPackageDialogExport: { minHeight: 176 }, packageModeTabs: { width: 240, height: 28 }, packageSections: { gap: 7, marginTop: 16 }, packageSectionTitle: { color: systemColors.secondaryLabel, fontSize: 12, fontWeight: "600" }, packageButtonRow: { marginTop: "auto", flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 8 },
  legacyLogsWindow: { flex: 1, minHeight: 420, gap: 0 }, legacyLogsToolbar: { height: 26, minHeight: 26, flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 2 }, toolbarLabel: { color: systemColors.label, fontSize: 12 }, logFilterInput: { flex: 1, minWidth: 220, maxWidth: 360, height: 26 }, legacyLogsTabs: { width: "100%", minWidth: 0, height: 28, marginTop: 4, marginBottom: 4 }, logTab: {}, logTabSelected: {}, logTabText: {}, legacyLogRecords: { flex: 1, minHeight: 280, backgroundColor: systemColors.textBackground, paddingHorizontal: 8, paddingVertical: 7 }, legacyLogEditor: { flex: 1, minHeight: 280 }, logInfoBar: { height: 27, minHeight: 27, borderTopWidth: 1, borderColor: systemColors.separator, justifyContent: "center", paddingHorizontal: 4, paddingBottom: 2 },
  form: { gap: 7 }, twoColumnForm: { gap: 7 }, field: { gap: 5, minWidth: 220, flexGrow: 1, flexBasis: 300 }, fieldLabel: { color: systemColors.label, fontSize: 12, fontWeight: "500" }, fieldHint: { color: systemColors.secondaryLabel, fontSize: 11 }, input: { width: "100%", minHeight: 26, paddingHorizontal: 7, paddingVertical: 4, color: systemColors.label, fontSize: 13 }, textArea: { minHeight: 108, textAlignVertical: "top", fontFamily: "Menlo" }, inputWithAction: { flexDirection: "row", alignItems: "center", gap: 6 }, inputFlex: { flex: 1 }, toggleRow: { minHeight: 28, flexDirection: "row", alignItems: "center", gap: 12 }, actions: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 4 }, secretFieldActions: { flexDirection: "row", alignItems: "center", gap: 8 }, secretFieldButtons: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 }, secretFieldButton: { flex: 1, minWidth: 0, height: 26 }, nativeSecretControl: { flex: 1, minWidth: 0, height: 26, flexDirection: "row", alignItems: "center", gap: 6 }, nativeSecretInput: { flex: 1, minWidth: 86, height: 26 }, nativeSecretSetButton: { minWidth: 42, height: 26 }, action: {}, actionPrimary: {}, actionDanger: {}, actionDisabled: {}, actionText: { color: systemColors.label, fontSize: 12, fontWeight: "500" }, actionTextPrimary: {}, actionTextDanger: {}, tabStrip: { flexDirection: "row", flexWrap: "wrap", gap: 7 }, tab: {}, tabSelected: {}, inlineMeta: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 8 }, rawEditor: { flex: 1, minHeight: 180, gap: 4 }, rawEditorHeader: { minHeight: 28, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, rawNativeEditor: { flex: 1, minHeight: 160 }, rawEditorLoading: { flex: 1, minHeight: 160, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground }, infoPair: { gap: 2, minWidth: 160 }, rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }, logRecords: { borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, maxHeight: 360, overflow: "scroll", padding: 10, gap: 6 }, logRecord: { color: systemColors.label, fontFamily: "Menlo", fontSize: 12 }, empty: { color: systemColors.secondaryLabel, fontSize: 13, paddingVertical: 12 }, result: { color: systemColors.green, fontSize: 12 }, warning: { color: systemColors.brown, fontSize: 12, backgroundColor: systemColors.control, padding: 8, borderRadius: 4 }, issueBox: { borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.control, padding: 12, gap: 5 }, issue: { color: systemColors.red, fontSize: 12 }, cardTitle: { color: systemColors.label, fontSize: 13, fontWeight: "500" }, cardHint: { color: systemColors.secondaryLabel, fontSize: 12, marginTop: 2 },
  formRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: 12 }, formRowStacked: { alignItems: "flex-start" }, formRowSecretStacked: { alignItems: "flex-start" }, formRowLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: 12, textAlign: "right" }, formRowLabelStacked: { paddingTop: 6 }, formRowControl: { flex: 1, minWidth: 0, gap: 3 }, picker: { flex: 1, minWidth: 180, height: 26 },
});
