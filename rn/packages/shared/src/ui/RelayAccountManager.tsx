import React, { useMemo, useRef, useState } from "react";
import { Platform, PlatformColor, ScrollView, StyleSheet, Text, View } from "react-native";
import type { CoreSnapshot, NativeLeafAdapter } from "../types";
import { NativeButton, NativeCheckbox, NativeSegmentedControl, NativeSelectableRow, NativeTextField } from "./NativeControls";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

type UnknownRecord = Record<string, unknown>;
type Translate = (key: string, values?: Record<string, string | number>) => string;
type RelayType = "newapi" | "sub2api";
type RelayAccount = {
  id: string;
  type: RelayType;
  label: string;
  origin: string;
  username: string;
  loginStatus: string;
  rememberPassword: boolean;
  resourceStatus: "idle" | "ready" | "unavailable";
  resources: RelayResource[];
};

type RelayResource = {
  id: string;
  name: string;
  apiName: string;
  apiBase: string;
  keyHint: string;
  models: string[];
};

type AddedRelayAccount = Pick<RelayAccount, "id" | "type" | "label" | "origin" | "username" | "rememberPassword">;

type PendingCredentialCleanup = {
  accountID: string;
  label: string;
  kind: "credentials" | "password";
};

type RelayTypeDetection = "checking" | RelayType | "unknown";

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function accountsFromSnapshot(snapshot?: CoreSnapshot): RelayAccount[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  return Array.isArray(state.accounts) ? state.accounts.flatMap((value) => {
    const item = record(value);
    const type = item.type === "sub2api" ? "sub2api" : item.type === "newapi" ? "newapi" : undefined;
    const id = text(item.id);
    if (!type || !id) return [];
    return [{
      id,
      type,
      label: text(item.label),
      origin: text(item.origin),
      username: text(item.username),
      loginStatus: text(item.login_status) || "unknown",
      rememberPassword: item.remember_password === true,
      resourceStatus: item.resource_status === "ready" || item.resource_status === "unavailable" ? item.resource_status : "idle",
      resources: Array.isArray(item.resources) ? item.resources.flatMap((resource) => {
        const entry = record(resource);
        const id = text(entry.id);
        const name = text(entry.name);
        if (!id || !name) return [];
        return [{
          id,
          name,
          apiName: text(entry.api_name) || name,
          apiBase: text(entry.api_base),
          keyHint: text(entry.key_hint),
          models: Array.isArray(entry.models) ? entry.models.filter((model): model is string => typeof model === "string" && Boolean(model)) : [],
        }];
      }) : [],
    }];
  }) : [];
}

function credentialCleanupsFromSnapshot(snapshot?: CoreSnapshot): PendingCredentialCleanup[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  return Array.isArray(state.pending_credential_cleanups) ? state.pending_credential_cleanups.flatMap((value) => {
    const item = record(value);
    const accountID = text(item.account_id);
    const label = text(item.label);
    const kind = item.kind === "credentials" ? "credentials" : item.kind === "password" ? "password" : undefined;
    if (!accountID || !label || !kind) return [];
    return [{ accountID, label, kind }];
  }) : [];
}

function statusKey(status: string): string {
  return ["signed_in", "signed_out", "expired"].includes(status) ? `relay.status.${status}` : "relay.status.unknown";
}

function relayTypeLabel(type: RelayType, translate: Translate): string {
  return translate(type === "newapi" ? "relay.type.newapi" : "relay.type.sub2api");
}

export function RelayAccountManager({
  visible,
  snapshot,
  native,
  busy,
  translate,
  onClose,
  dispatch,
  commit,
  detectType,
  refreshResources,
  importResources,
  addAccount,
  refreshAccounts,
}: {
  /**
   * Kept optional while callers migrate from the old provider-page overlay.
   * RN macOS has no Modal host implementation, so this component must remain
   * in the ordinary route tree.
   */
  visible?: boolean;
  snapshot?: CoreSnapshot;
  native: NativeLeafAdapter;
  busy: boolean;
  translate: Translate;
  onClose?: () => void;
  dispatch: (type: string, payload?: UnknownRecord, domain?: "relay_accounts") => Promise<void>;
  commit: (type: string, payload?: UnknownRecord, domain?: "relay_accounts") => Promise<void>;
  detectType: (origin: string) => Promise<RelayType | undefined>;
  refreshResources: (accountId: string) => Promise<void>;
  importResources: (accountId: string, resourceIds: string[]) => Promise<void>;
  addAccount: (type: RelayType, origin: string, rememberPassword: boolean) => Promise<AddedRelayAccount | undefined>;
  refreshAccounts: () => Promise<void>;
}): React.JSX.Element {
  const accounts = useMemo(() => accountsFromSnapshot(snapshot), [snapshot]);
  const pendingCredentialCleanups = useMemo(() => credentialCleanupsFromSnapshot(snapshot), [snapshot]);
  const [selectedID, setSelectedID] = useState<string>();
  const [adding, setAdding] = useState(false);
  const [origin, setOrigin] = useState("");
  const [rememberPassword, setRememberPassword] = useState(false);
  const [typeDetection, setTypeDetection] = useState<RelayTypeDetection>();
  const [manualType, setManualType] = useState<RelayType>();
  const [selectedResources, setSelectedResources] = useState<string[]>([]);
  const [formBusy, setFormBusy] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [feedback, setFeedback] = useState<string>();
  const rememberPasswordRef = useRef(false);
  const typeDetectionRequest = useRef(0);
  const controlsBusy = busy || formBusy || loginBusy || restoreBusy || cleanupBusy;
  const selected = accounts.find((account) => account.id === selectedID) ?? accounts[0];
  const resetForm = (): void => {
    typeDetectionRequest.current += 1;
    setAdding(false);
    setOrigin("");
    setTypeDetection(undefined);
    setManualType(undefined);
    setSelectedResources([]);
    rememberPasswordRef.current = false;
    setRememberPassword(false);
  };
  const beginAdding = (): void => {
    resetForm();
    setFeedback(undefined);
    setAdding(true);
  };
  const detectRelayType = async (): Promise<RelayType | undefined> => {
    const candidate = origin.trim();
    if (!candidate) return undefined;
    const request = ++typeDetectionRequest.current;
    setTypeDetection("checking");
    try {
      const detected = await detectType(candidate);
      if (request !== typeDetectionRequest.current) return undefined;
      setTypeDetection(detected ?? "unknown");
      return detected;
    } catch {
      if (request === typeDetectionRequest.current) setTypeDetection("unknown");
      return undefined;
    }
  };
  const restoreSession = async (account: RelayAccount): Promise<void> => {
    setRestoreBusy(true);
    setFeedback(translate("relay.sessionChecking"));
    try {
      const result = await native.restoreRelaySession({
        accountId: account.id,
        type: account.type,
        label: account.label,
        origin: account.origin,
        username: account.username || undefined,
      });
      if (result) await refreshAccounts();
      setFeedback(translate(result ? "relay.sessionChecked" : "relay.sessionCheckUnavailable"));
    } catch {
      setFeedback(translate("relay.sessionCheckUnavailable"));
    } finally {
      setRestoreBusy(false);
    }
  };
  const beginLogin = async (): Promise<void> => {
    if (!origin.trim()) return;
    setFormBusy(true);
    setFeedback(undefined);
    try {
      const detected = await detectRelayType();
      const accountType = detected ?? manualType;
      if (!accountType) {
        // A white-label site can block the public detection probes while
        // still presenting a normal sign-in page. Let the user choose the
        // station family, then open the native browser as usual.
        setManualType("newapi");
        setFeedback(translate("relay.typeNotDetected"));
        return;
      }
      const account = await addAccount(accountType, origin.trim(), rememberPasswordRef.current);
      if (!account) throw new Error("Relay account could not be created");
      resetForm();
      setSelectedID(account.id);
      await loginAccount(account);
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const pendingCleanups = pendingCredentialCleanups;
  const retryCredentialCleanup = async (cleanup: PendingCredentialCleanup): Promise<void> => {
    setCleanupBusy(true);
    try {
      if (cleanup.kind === "credentials") {
        await native.clearRelayCredentials(cleanup.accountID);
      } else {
        await native.clearRelayPassword(cleanup.accountID);
      }
      // The Core tombstone is durable until native storage confirms the
      // browser session or opted-out password has been erased. A window close
      // or Core restart therefore cannot lose this retry work.
      await commit("credential_cleanup_confirm", { id: cleanup.accountID, kind: cleanup.kind }, "relay_accounts");
      setFeedback(undefined);
    } catch {
      // The Core keeps this opaque account-ID tombstone for a later retry.
    } finally {
      setCleanupBusy(false);
    }
  };
  const deleteAccount = async (account: RelayAccount): Promise<void> => {
    setCleanupBusy(true);
    setFeedback(undefined);
    try {
      // Persist the deletion first. A Core write failure must leave the native
      // session and password untouched so the account remains usable.
      await commit("account.delete", { id: account.id }, "relay_accounts");
    } catch {
      setFeedback(translate("relay.operationFailed"));
      setCleanupBusy(false);
      return;
    }
    setSelectedID(undefined);
    try {
      await native.clearRelayCredentials(account.id);
      await commit("credential_cleanup_confirm", { id: account.id, kind: "credentials" }, "relay_accounts");
    } catch {
      // The deletion transaction has already persisted a secret-free Core
      // tombstone. It remains visible after this window closes until a native
      // credential erase succeeds and confirms it.
    } finally {
      setCleanupBusy(false);
    }
  };
  const remove = (): void => {
    if (!selected) return;
    void native.showConfirmation({
      title: translate("relay.deleteTitle"),
      message: translate("relay.deleteBody", { label: selected.label }),
      confirmLabel: translate("common.delete"),
    }).then((confirmed) => {
      if (confirmed) void deleteAccount(selected);
    }).catch(() => setFeedback(translate("relay.operationFailed")));
  };
  const loginAccount = async (account: AddedRelayAccount): Promise<void> => {
    setLoginBusy(true);
    setFeedback(translate("relay.loginWorking"));
    try {
      const result = await native.relayLogin({
        accountId: account.id,
        type: account.type,
        label: account.label,
        origin: account.origin,
        language: snapshot?.language ?? "system",
        username: account.username || undefined,
        rememberPassword: account.rememberPassword,
      });
      if (result) {
        try {
          await refreshResources(account.id);
        } catch {
          // The authenticated browser session remains valid when resource
          // discovery is temporarily unavailable; the account pane will show
          // the unavailable state after the snapshot refresh below.
        }
        await refreshAccounts();
        setSelectedID(account.id);
        setSelectedResources([]);
      }
      setFeedback(translate(result ? "relay.loginComplete" : "relay.loginNotCompleted"));
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setLoginBusy(false);
    }
  };
  const login = async (): Promise<void> => {
    if (!selected) return;
    await loginAccount(selected);
  };
  const toggleResource = (resourceID: string): void => {
    setSelectedResources((current) => current.includes(resourceID) ? current.filter((value) => value !== resourceID) : [...current, resourceID]);
  };
  const importSelectedResources = async (): Promise<void> => {
    if (!selected || selectedResources.length === 0) return;
    setFormBusy(true);
    setFeedback(undefined);
    try {
      await importResources(selected.id, selectedResources);
      setFeedback(translate("relay.resourcesImported"));
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const updateRememberPassword = async (next: boolean): Promise<void> => {
    if (!selected) return;
    setFeedback(undefined);
    setCleanupBusy(true);
    try {
      await commit("account.update", { id: selected.id, remember_password: next }, "relay_accounts");
    } catch {
      setFeedback(translate("relay.operationFailed"));
      setCleanupBusy(false);
      return;
    }
    if (next) {
      setCleanupBusy(false);
      return;
    }
    try {
      // Clear after the Core commit: a metadata write failure cannot erase a
      // password while the account still claims to remember it.
      await native.clearRelayPassword(selected.id);
      await commit("credential_cleanup_confirm", { id: selected.id, kind: "password" }, "relay_accounts");
    } catch {
      // `account.update` persisted a secret-free password-cleanup tombstone.
    } finally {
      setCleanupBusy(false);
    }
  };

  if (visible === false) return <View style={styles.hidden} />;

  return <View style={styles.workspace} accessibilityLabel={translate("relay.title")}>
    <View style={styles.header}>
      <View style={styles.headerCopy}>
        <Text style={styles.title}>{translate("relay.title")}</Text>
        <Text style={styles.subtitle}>{translate("relay.subtitle")}</Text>
      </View>
      {onClose ? <NativeButton title={translate("menu.close")} compact disabled={controlsBusy} onPress={onClose} /> : null}
    </View>
    {pendingCleanups.length > 0 ? <ScrollView style={styles.pendingCleanupList} contentContainerStyle={styles.pendingCleanupListContent}>{pendingCleanups.map((cleanup) => <View key={`${cleanup.accountID}:${cleanup.kind}`} style={styles.pendingCleanup}><Text style={styles.pendingCleanupText}>{translate(cleanup.kind === "credentials" ? "relay.credentialsCleanupPending" : "relay.passwordCleanupPending", { label: cleanup.label })}</Text><NativeButton title={translate("relay.retryCleanup")} compact disabled={controlsBusy} onPress={() => { void retryCredentialCleanup(cleanup); }} /></View>)}</ScrollView> : null}
      <View style={styles.body}>
      <View style={styles.sidebar}>
        <View style={styles.sidebarHeader}>
          <Text style={styles.sidebarTitle}>{translate("relay.accounts")}</Text>
          <View style={styles.sidebarActions}>
            <NativeButton title={translate("relay.add")} toolTip={translate("relay.add")} compact primary disabled={controlsBusy} onPress={beginAdding} style={styles.addButton} />
          </View>
        </View>
        <ScrollView style={styles.accountList} contentContainerStyle={styles.accountListContent}>
          {accounts.map((account) => <NativeSelectableRow key={account.id} title={account.label} detail={account.username || translate(statusKey(account.loginStatus))} selected={account.id === (selected?.id ?? "")} onPress={() => { setSelectedID(account.id); setAdding(false); setSelectedResources([]); setFeedback(undefined); }} style={styles.accountRow} />)}
        </ScrollView>
      </View>
      <ScrollView style={styles.detail} contentContainerStyle={styles.detailContent}>
        {adding ? <View key="relay-add-account" style={styles.detailFrame}>
          <Text style={styles.sectionTitle}>{translate("relay.add")}</Text>
          <View style={styles.formCard}>
            <Field label={translate("relay.origin")}><View style={styles.fieldControl}><NativeTextField value={origin} placeholder={translate("relay.originPlaceholder")} editable={!controlsBusy} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={(value) => { typeDetectionRequest.current += 1; setOrigin(value); setTypeDetection(undefined); setManualType(undefined); }} onBlur={() => { void detectRelayType(); }} style={styles.control} />{typeDetection ? <Text style={styles.typeDetection}>{typeDetection === "checking" ? translate("relay.detectingType") : typeDetection === "unknown" ? translate("relay.typeNotDetected") : translate("relay.typeDetected", { type: relayTypeLabel(typeDetection, translate) })}</Text> : null}</View></Field>
            {typeDetection === "unknown" ? <Field label={translate("relay.type")}><NativeSegmentedControl labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={relayTypeLabel(manualType ?? "newapi", translate)} disabled={controlsBusy} onChange={({ nativeEvent }) => { setManualType(nativeEvent.index === 1 ? "sub2api" : "newapi"); }} style={styles.typeSelector} /></Field> : null}
            <NativeCheckbox label={translate("relay.rememberPassword")} value={rememberPassword} disabled={controlsBusy} onValueChange={(next) => { rememberPasswordRef.current = next; setRememberPassword(next); }} />
          </View>
          {feedback ? <Text accessibilityLiveRegion="polite" style={styles.feedback}>{feedback}</Text> : null}
          <View style={styles.detailActions}><NativeButton title={translate("menu.cancel")} disabled={controlsBusy} onPress={resetForm} /><NativeButton title={translate("relay.next")} primary disabled={controlsBusy || !origin.trim()} onPress={() => { void beginLogin(); }} /></View>
        </View> : selected ? <View key={`relay-account-${selected.id}`} style={styles.detailFrame}>
          <View style={styles.accountSummary}>
            <View style={styles.accountSummaryCopy}>
              <Text numberOfLines={1} style={styles.sectionTitle}>{selected.label}</Text>
              <Text numberOfLines={1} style={styles.summaryDetail}>{`${relayTypeLabel(selected.type, translate)} - ${selected.origin}`}</Text>
            </View>
            <Text numberOfLines={1} style={styles.statusBadge}>{translate(statusKey(selected.loginStatus))}</Text>
          </View>
          <View style={styles.detailsCard}>
            <Info label={translate("relay.type")} value={relayTypeLabel(selected.type, translate)} />
            <Info label={translate("relay.origin")} value={selected.origin} />
            <Info label={translate("relay.username")} value={selected.username || translate("common.none")} />
          </View>
          <View style={styles.preferenceBlock}><NativeCheckbox label={translate("relay.rememberPassword")} value={selected.rememberPassword} disabled={controlsBusy} onValueChange={(next) => { void updateRememberPassword(next); }} /></View>
          <View style={styles.resourcePane}>
            <View style={styles.resourceHeader}><Text style={styles.resourceTitle}>{translate("relay.resources")}</Text>{selected.resourceStatus === "ready" ? <Text style={styles.resourceCount}>{translate("relay.resourceCount", { count: selected.resources.length })}</Text> : null}</View>
            {selected.resources.length > 0 ? <ScrollView style={styles.resourceList} contentContainerStyle={styles.resourceListContent}>{selected.resources.map((resource) => <View key={resource.id} style={styles.resourceRow}><NativeCheckbox label={`${resource.apiName}${resource.keyHint ? ` | ${resource.keyHint}` : ""}`} value={selectedResources.includes(resource.id)} disabled={controlsBusy} onValueChange={() => toggleResource(resource.id)} /><Text numberOfLines={1} style={styles.resourceDetail}>{`${resource.apiBase} | ${translate("relay.modelsCount", { count: resource.models.length })}`}</Text></View>)}</ScrollView> : <Text style={styles.hint}>{selected.resourceStatus === "unavailable" ? translate("relay.resourcesUnavailable") : translate("relay.resourcesAfterLogin")}</Text>}
          </View>
          {feedback ? <Text accessibilityLiveRegion="polite" style={styles.feedback}>{feedback}</Text> : null}
          <View style={styles.detailActions}>
            <NativeButton title={translate("relay.delete")} destructive disabled={controlsBusy} onPress={remove} />
            <NativeButton title={translate("relay.checkSession")} disabled={controlsBusy} onPress={() => { void restoreSession(selected); }} />
            <NativeButton title={translate("relay.login")} disabled={controlsBusy} onPress={() => { void login(); }} style={styles.loginButton} />
            <NativeButton primary title={translate("relay.importSelected")} disabled={controlsBusy || selectedResources.length === 0} onPress={() => { void importSelectedResources(); }} style={styles.loginButton} />
          </View>
        </View> : <View style={styles.blank}><Text style={styles.empty}>{translate("relay.empty")}</Text></View>}
      </ScrollView>
    </View>
  </View>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element {
  return <View style={styles.field}><Text style={styles.label}>{label}</Text>{children}</View>;
}

function Info({ label, value }: { label: string; value: string }): React.JSX.Element {
  return <View style={styles.info}><Text style={styles.label}>{label}</Text><Text selectable numberOfLines={3} style={styles.value}>{value}</Text></View>;
}

const colors = {
  window: Platform.OS === "macos" ? PlatformColor("windowBackgroundColor") : PlatformColor("Window"),
  text: Platform.OS === "macos" ? PlatformColor("labelColor") : PlatformColor("WindowText"),
  secondary: Platform.OS === "macos" ? PlatformColor("secondaryLabelColor") : PlatformColor("GrayText"),
  separator: Platform.OS === "macos" ? PlatformColor("separatorColor") : PlatformColor("ControlStrokeColorDefault"),
  panel: Platform.OS === "macos" ? PlatformColor("controlBackgroundColor") : PlatformColor("ControlFillColorDefault"),
  accent: Platform.OS === "macos" ? PlatformColor("controlAccentColor") : PlatformColor("AccentColor"),
};

const styles = StyleSheet.create({
  hidden: { display: "none" },
  workspace: { flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden", backgroundColor: colors.window },
  header: { minHeight: 68, paddingHorizontal: 18, paddingVertical: 12, flexDirection: "row", flexWrap: "wrap", alignItems: "center", borderBottomWidth: 1, borderBottomColor: colors.separator, gap: 12 },
  headerCopy: { flex: 1, flexBasis: 260, minWidth: 0, gap: 3 }, title: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" }, subtitle: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  body: { flex: 1, minHeight: 0, minWidth: 0, flexDirection: "row" }, sidebar: { width: 268, minWidth: 220, flexShrink: 1, borderRightWidth: 1, borderRightColor: colors.separator, backgroundColor: colors.panel },
  sidebarHeader: { minHeight: 48, paddingHorizontal: 12, flexDirection: "row", alignItems: "center", justifyContent: "space-between", borderBottomWidth: 1, borderBottomColor: colors.separator }, sidebarTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  sidebarActions: { minHeight: 28, flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 6 }, addButton: { minWidth: 88 },
  accountList: { flex: 1, minWidth: 0 }, accountListContent: { paddingHorizontal: 6, paddingVertical: 6, minWidth: 0 }, accountRow: { minHeight: 48, marginBottom: 4 },
  detail: { flex: 1, minWidth: 0, backgroundColor: colors.window }, detailContent: { flexGrow: 1, minWidth: 0, paddingHorizontal: 24, paddingTop: 22, paddingBottom: 28 }, detailFrame: { width: "100%", maxWidth: 680, minWidth: 0, gap: 14 }, sectionTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600", marginBottom: 1 },
  formCard: { minWidth: 0, padding: 16, gap: 12, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel }, field: { minHeight: 32, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 12 }, label: { width: 128, flexShrink: 0, color: colors.text, fontSize: UI_FONT_SIZE }, fieldControl: { flex: 1, minWidth: 180, gap: 4 }, control: { flex: 1, flexShrink: 1, minWidth: 180, height: 28 }, typeSelector: { minWidth: 220, flexShrink: 1 }, typeDetection: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  accountSummary: { minHeight: 54, paddingHorizontal: 16, paddingVertical: 12, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 10, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel }, accountSummaryCopy: { flex: 1, minWidth: 180, gap: 3 }, summaryDetail: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 }, statusBadge: { flexShrink: 1, maxWidth: "100%", paddingHorizontal: 8, paddingVertical: 3, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600", borderWidth: 1, borderColor: colors.separator, borderRadius: 4, backgroundColor: colors.window },
  detailsCard: { minWidth: 0, padding: 16, gap: 10, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.window }, info: { minHeight: 28, flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", gap: 12 }, value: { flex: 1, minWidth: 180, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  preferenceBlock: { minWidth: 0, paddingTop: 12, borderTopWidth: 1, borderTopColor: colors.separator, gap: 6 }, hint: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  resourcePane: { minWidth: 0, maxHeight: 260, padding: 12, gap: 8, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel }, resourceHeader: { minHeight: 20, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, resourceTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" }, resourceCount: { color: colors.secondary, fontSize: UI_FONT_SIZE }, resourceList: { minHeight: 0 }, resourceListContent: { gap: 6 }, resourceRow: { minWidth: 0, paddingVertical: 4, gap: 3, borderBottomWidth: 1, borderBottomColor: colors.separator }, resourceDetail: { paddingLeft: 4, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  detailActions: { minHeight: 36, marginTop: 4, flexDirection: "row", flexWrap: "wrap", justifyContent: "flex-end", alignItems: "center", gap: 8 }, loginButton: { minWidth: 136 }, feedback: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  pendingCleanupList: { maxHeight: 116, borderBottomWidth: 1, borderBottomColor: colors.separator, backgroundColor: colors.panel }, pendingCleanupListContent: { paddingHorizontal: 18, paddingVertical: 8, gap: 6 }, pendingCleanup: { minHeight: 30, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 12 }, pendingCleanupText: { flex: 1, flexBasis: 260, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  empty: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18, textAlign: "center" }, blank: { flex: 1, minHeight: 200, alignItems: "center", justifyContent: "center", gap: 12, paddingHorizontal: 20 },
});
