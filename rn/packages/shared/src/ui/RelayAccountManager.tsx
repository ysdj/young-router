import React, { useEffect, useMemo, useRef, useState } from "react";
import { Platform, PlatformColor, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";
import type { CoreSnapshot, NativeLeafAdapter } from "../types";
import { NativeButton, NativeCheckbox, NativePicker, NativeSecureTextInput, NativeTable, NativeTextField } from "./NativeControls";
import { normalizeRelayOrigin } from "./relayOrigin";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

export { normalizeRelayOrigin } from "./relayOrigin";

type UnknownRecord = Record<string, unknown>;
type Translate = (key: string, values?: Record<string, string | number>) => string;
export type RelayType = "newapi" | "sub2api";
type LocalDependencyPolicy = "delete_models" | "detach";
type RemoteDeletePolicy = "delete_models" | "detach_disabled" | "detach_only";
export type RelayGroup = {
  id: string;
  name: string;
  multiplier: number | null;
};
export type RelayResource = {
  id: string;
  name: string;
  apiName: string;
  apiBase: string;
  keyHint: string;
  enabled: boolean;
  models: string[];
  groupID: string;
  groupName: string;
  linkedModelCount: number;
  pendingOperationCount: number;
};
export type RelayAccount = {
  id: string;
  type: RelayType;
  label: string;
  origin: string;
  stationID: string;
  stationName: string;
  username: string;
  loginStatus: string;
  rememberPassword: boolean;
  passwordSaved: boolean;
  autoGrouping: boolean;
  balance: number | null;
  resourceStatus: "idle" | "ready" | "unavailable";
  resourceError: "none" | "login_expired" | "no_api_keys" | "no_models" | "unavailable";
  linkedModelCount: number;
  pendingOperationCount: number;
  resources: RelayResource[];
  groups: RelayGroup[];
};
/** A station is the durable grouping identity for relay accounts. */
export type RelayStation = {
  id: string;
  name: string;
  origin: string;
  type?: RelayType;
  persisted: boolean;
  accountIDs: string[];
  linkedModelCount: number;
  pendingOperationCount: number;
};
export type AddedRelayAccount = Pick<RelayAccount, "id" | "type" | "label" | "origin" | "username">;
type AutoGroupingUpdateResult = { draftStaged: boolean };

/**
 * API-key writes stay behind host/Core callbacks. The ordinary snapshot and
 * React state contain masked metadata only; Core stages the change here and
 * performs the authenticated remote mutation during Apply without returning
 * a generated key value through IPC.
 */
export type RelayApiKeyActions = {
  create?: (accountID: string, options: { name: string; groupID?: string; enabled: boolean }) => Promise<void>;
  update?: (accountID: string, resourceID: string, name: string) => Promise<void>;
  setEnabled?: (accountID: string, resourceID: string, enabled: boolean) => Promise<void>;
  setGroup?: (accountID: string, resourceID: string, groupID: string) => Promise<void>;
  setAutoGrouping?: (accountID: string, enabled: boolean) => Promise<AutoGroupingUpdateResult>;
  alignAutoGrouping?: (accountID: string) => Promise<void>;
  remove?: (accountID: string, resourceID: string, dependencyPolicy: Exclude<RemoteDeletePolicy, "detach_only">) => Promise<void>;
  detach?: (accountID: string, resourceID: string) => Promise<void>;
};

/** Staged relay metadata commits go through this opaque host callback. */
export type RelayCommit = (type: string, payload?: UnknownRecord) => Promise<void>;

type AccountLoadingState = { session: boolean; resources: boolean };
type ResourceRefreshTarget = { id: string; resources?: RelayResource[] };
export type StationDraft = Partial<Pick<RelayStation, "name" | "origin" | "type">>;
type PolicyOption<T extends string> = { value: T; label: string; hint: string };
const INLINE_MODEL_LIMIT = 5;

function record(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? { ...value } as UnknownRecord : {};
}
function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
function groupMultiplier(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function groupMultiplierLabel(multiplier: number | null, translate: Translate): string {
  return multiplier === null ? translate("relay.apiKeyUngrouped") : `×${multiplier}`;
}

export function groupLabel(group: RelayGroup, translate: Translate): string {
  const multiplier = groupMultiplier(group.multiplier);
  const name = group.name || translate("relay.apiKeyUngrouped");
  return multiplier === null ? name : `${name} ×${multiplier}`;
}

function resourceGroup(resource: RelayResource, groups: RelayGroup[]): RelayGroup | undefined {
  return groups.find((group) => group.id === resource.groupID);
}

function resourceGroupName(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  const group = resourceGroup(resource, groups);
  return group?.name || resource.groupName || (resource.groupID ? resource.groupID : translate("relay.apiKeyUngrouped"));
}

function resourceGroupMultiplier(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  const group = resourceGroup(resource, groups);
  return groupMultiplierLabel(group ? group.multiplier : null, translate);
}

function resourceGroupLabel(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  return `${resourceGroupName(resource, groups, translate)} ${resourceGroupMultiplier(resource, groups, translate)}`.trim();
}

function resourceGroupUnavailable(resource: RelayResource, groups: RelayGroup[]): boolean {
  return resource.enabled && resourceGroup(resource, groups) === undefined && Boolean(resource.groupID);
}

function resourceAutoGroupingUnavailable(resource: RelayResource, groups: RelayGroup[]): boolean {
  return resource.enabled && resourceGroup(resource, groups) === undefined;
}

export function accountsFromSnapshot(snapshot?: CoreSnapshot): RelayAccount[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  return Array.isArray(state.accounts) ? state.accounts.flatMap((value) => {
    const item = record(value);
    const type = item.type === "sub2api" ? "sub2api" : item.type === "newapi" ? "newapi" : undefined;
    const id = text(item.id);
    if (!type || !id) return [];
    const origin = text(item.origin) || text(item.base_url) || text(item.url);
    const stationID = text(item.station_id) || text(item.group_id) || text(item.relay_id);
    return [{
      id,
      type,
      label: text(item.label),
      origin,
      stationID,
      stationName: text(item.station_name) || text(item.station_label),
      username: text(item.username),
      loginStatus: text(item.login_status) || "unknown",
      rememberPassword: item.remember_password === true,
      passwordSaved: item.password_saved === true,
      autoGrouping: item.auto_grouping === true,
      balance: typeof item.balance === "number" && Number.isFinite(item.balance) ? item.balance : null,
      resourceStatus: item.resource_status === "ready" || item.resource_status === "unavailable" ? item.resource_status : "idle",
      resourceError: item.resource_error === "login_expired" || item.resource_error === "no_api_keys" || item.resource_error === "no_models" || item.resource_error === "unavailable" ? item.resource_error : "none",
      linkedModelCount: count(item.linked_model_count),
      pendingOperationCount: count(item.pending_operation_count),
      groups: (Array.isArray(item.groups) ? item.groups : []).flatMap((group) => {
        const entry = record(group);
        const id = text(entry.id);
        if (!id) return [];
        return [{ id, name: text(entry.name) || id, multiplier: groupMultiplier(entry.multiplier ?? entry.rate_multiplier ?? entry.ratio) }];
      }),
      resources: (Array.isArray(item.resources) ? item.resources : Array.isArray(item.api_keys) ? item.api_keys : []).flatMap((resource) => {
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
          enabled: entry.enabled !== false,
          models: Array.isArray(entry.models) ? entry.models.filter((model): model is string => typeof model === "string" && Boolean(model)) : [],
          groupID: text(entry.group_id),
          groupName: text(entry.group_name),
          linkedModelCount: count(entry.linked_model_count),
          pendingOperationCount: count(entry.pending_operation_count),
        }];
      }),
    }];
  }) : [];
}

export function stationOriginKey(value: string): string {
  const normalized = normalizeRelayOrigin(value);
  if (!normalized) return "";
  try {
    const parsed = new URL(normalized);
    const port = parsed.port && !((parsed.protocol === "https:" && parsed.port === "443") || (parsed.protocol === "http:" && parsed.port === "80")) ? `:${parsed.port}` : "";
    const path = parsed.pathname.replace(/\/+$/u, "");
    return `${parsed.protocol.toLowerCase()}//${parsed.hostname.toLowerCase()}${port}${path}`;
  } catch {
    return normalized.toLowerCase();
  }
}

export function stationsFromSnapshot(snapshot: CoreSnapshot | undefined, accounts: RelayAccount[]): RelayStation[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  const rawStations = Array.isArray(state.stations) ? state.stations : Array.isArray(state.groups) ? state.groups : [];
  const stations: RelayStation[] = [];
  const byID = new Map<string, RelayStation>();
  const byOrigin = new Map<string, RelayStation>();
  const add = (raw: unknown, fallbackAccount?: RelayAccount): RelayStation | undefined => {
    const item = record(raw);
    const origin = text(item.origin) || text(item.base_url) || text(item.url) || fallbackAccount?.origin || "";
    const name = text(item.name) || text(item.label) || fallbackAccount?.stationName || (fallbackAccount ? stationName(fallbackAccount) : "");
    const originKey = stationOriginKey(origin);
    const id = text(item.id) || text(item.station_id) || text(item.group_id) || (originKey ? `station:${originKey}` : name ? `station:${name.toLowerCase()}` : "");
    if (!id && !originKey) return undefined;
    const existing = byID.get(id) ?? (originKey ? byOrigin.get(originKey) : undefined);
    if (existing) {
      if (id) byID.set(id, existing);
      if (!existing.name && name) existing.name = name;
      if (!existing.origin && origin) existing.origin = normalizeRelayOrigin(origin);
      if (!existing.type && (item.type === "newapi" || item.type === "sub2api")) existing.type = item.type;
      return existing;
    }
    const station: RelayStation = {
      id: id || `station:${originKey}`,
      name: name || origin || translateStationName(fallbackAccount),
      origin: normalizeRelayOrigin(origin),
      type: item.type === "newapi" || item.type === "sub2api" ? item.type : fallbackAccount?.type,
      persisted: rawStations.includes(raw),
      accountIDs: [],
      linkedModelCount: count(item.linked_model_count),
      pendingOperationCount: count(item.pending_operation_count),
    };
    stations.push(station);
    byID.set(station.id, station);
    if (originKey) byOrigin.set(originKey, station);
    return station;
  };
  for (const raw of rawStations) add(raw);
  for (const account of accounts) {
    const originKey = stationOriginKey(account.origin);
    const station = (account.stationID && byID.get(account.stationID)) || (originKey && byOrigin.get(originKey)) || add({ id: account.stationID, origin: account.origin, name: account.stationName }, account);
    if (!station) continue;
    if (!station.origin && account.origin) {
      station.origin = normalizeRelayOrigin(account.origin);
      const accountOriginKey = stationOriginKey(station.origin);
      if (accountOriginKey) byOrigin.set(accountOriginKey, station);
    }
    if (!station.name && account.stationName) station.name = account.stationName;
    if (!station.type) station.type = account.type;
    if (!station.accountIDs.includes(account.id)) station.accountIDs.push(account.id);
    if (!account.stationID) account.stationID = station.id;
    if (!account.stationName) account.stationName = station.name;
  }
  for (const station of stations) {
    const stationAccounts = accounts.filter((account) => station.accountIDs.includes(account.id));
    if (station.linkedModelCount === 0) station.linkedModelCount = stationAccounts.reduce((sum, account) => sum + account.linkedModelCount, 0);
    if (station.pendingOperationCount === 0) station.pendingOperationCount = stationAccounts.reduce((sum, account) => sum + account.pendingOperationCount, 0);
  }
  return stations;
}

function translateStationName(account?: RelayAccount): string {
  return account ? stationName(account) : "";
}


export function relayTypeLabel(type: RelayType, translate: Translate): string {
  return translate(type === "newapi" ? "relay.type.newapi" : "relay.type.sub2api");
}

function resourceHint(account: RelayAccount, translate: Translate): string {
  if (account.resourceStatus === "unavailable" || account.resourceError === "login_expired") return translate("relay.resourcesLoginExpired");
  if (account.resourceError === "no_api_keys") return translate("relay.resourcesNoApiKeys");
  if (account.resourceError === "no_models") return translate("relay.resourcesNoModels");
  if (account.resourceStatus === "idle") return translate("relay.resourcesNotLoaded");
  return translate("relay.resourcesUnavailable");
}

function stationName(account: RelayAccount): string {
  return account.stationName || account.label || account.origin;
}

function originHostLabel(value: string): string {
  const origin = normalizeRelayOrigin(value);
  try {
    return new URL(origin).host;
  } catch {
    return origin.replace(/^[a-z][a-z\d+.-]*:\/\//iu, "");
  }
}

export function stationDisplayName(station: RelayStation, translate: Translate): string {
  const name = station.name.trim();
  if (name && !/^https?:\/\//iu.test(name)) return name;
  return originHostLabel(station.origin) || name;
}

function stationPickerLabel(station: RelayStation, translate: Translate): string {
  return `${stationDisplayName(station, translate)} (${relayTypeLabel(station.type ?? "newapi", translate)})`;
}

function usernameShortName(account: RelayAccount): string {
  return account.username.trim();
}

export function accountDisplayName(account: RelayAccount, translate: Translate): string {
  const username = usernameShortName(account);
  if (username) return username;
  const label = account.label.trim();
  if (label && !/^https?:\/\//iu.test(label)) return label;
  return originHostLabel(account.origin) || translate("relay.unsignedAccount");
}

function accountDetailTitle(account: RelayAccount, translate: Translate): string {
  return usernameShortName(account) || accountDisplayName(account, translate);
}

function balanceLabel(account: RelayAccount, translate: Translate): string {
  return account.balance === null ? translate("common.none") : `$${account.balance.toFixed(2)}`;
}

type PendingCredentialCleanup = {
  accountID: string;
  label: string;
  kind: "credentials";
};

/** Secret-free cleanup tombstones Core keeps until native storage confirms the erase. */
export function pendingCredentialCleanups(snapshot?: CoreSnapshot): PendingCredentialCleanup[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  const rows = Array.isArray(state.pending_credential_cleanups) ? state.pending_credential_cleanups : [];
  return rows.flatMap((item) => {
    const value = record(item);
    const accountID = text(value.account_id);
    if (!accountID) return [];
    return [{ accountID, label: text(value.label), kind: "credentials" as const }];
  });
}

export function NativeFormRow({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.formRow, compactStyles.formRow]}>
    <Text style={styles.formLabel}>{label}</Text>
    <View style={[styles.formValue, compactStyles.formValue]}>{children}</View>
  </View>;
}

export function NativeWizardProgress({ steps, activeIndex }: { steps: string[]; activeIndex: number }): React.JSX.Element {
  return <View style={styles.setupProgress} accessibilityLabel={steps.map((label, index) => `${index + 1} ${label}`).join(", ")}>
    {steps.map((label, index) => <View key={label} style={styles.setupProgressStep}>
      <View style={[styles.setupProgressBadge, index === activeIndex && styles.setupProgressBadgeCurrent, index < activeIndex && styles.setupProgressBadgeDone]}>
        <Text style={[styles.setupProgressNumber, index === activeIndex && styles.setupProgressNumberCurrent]}>{index + 1}</Text>
      </View>
      <Text style={[styles.setupProgressLabel, index === activeIndex && styles.setupProgressLabelCurrent]}>{label}</Text>
    </View>)}
  </View>;
}

function FormRow({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element {
  return <NativeFormRow label={label}>{children}</NativeFormRow>;
}

function RelayDialogLayer({ visible, onRequestClose, children }: {
  visible: boolean;
  onRequestClose: () => void;
  children: React.ReactNode;
}): React.JSX.Element | null {
  if (!visible) return null;
  return <View style={styles.relayDialogLayer} accessibilityViewIsModal onAccessibilityEscape={onRequestClose}>
    {children}
  </View>;
}

export function ApiKeyCreateDialog({ visible, groups, disabled, onClose, onCreate, translate }: {
  visible: boolean;
  groups: RelayGroup[];
  disabled: boolean;
  onClose: () => void;
  onCreate: (options: { name: string; groupID?: string; enabled: boolean }) => void;
  translate: Translate;
}): React.JSX.Element {
  const [name, setName] = useState("");
  const [groupID, setGroupID] = useState("");
  const [enabled, setEnabled] = useState(true);
  useEffect(() => {
    if (!visible) return;
    setName("");
    setGroupID(groups[0]?.id ?? "");
    setEnabled(true);
  }, [visible, groups]);
  const groupOptions = groups.length > 0 ? groups : [{ id: "", name: translate("relay.apiKeyUngrouped"), multiplier: null }];
  const selectedGroup = groupOptions.find((group) => group.id === groupID) ?? groupOptions[0];
  return <RelayDialogLayer visible={visible} onRequestClose={onClose}>
    <View style={styles.dialogBackdrop}>
      <View style={[styles.decisionDialog, styles.apiKeyCreateDialog]} accessibilityViewIsModal>
        <View style={[styles.dialogHeader, styles.apiKeyDialogHeader]}>
          <View style={styles.apiKeyDialogTitleWrap}>
            <View style={styles.apiKeyDialogIcon}><Text style={styles.apiKeyDialogIconText}>+</Text></View>
            <View style={styles.apiKeyDialogTitleBlock}>
              <Text style={styles.dialogTitle}>{translate("relay.apiKeyCreateTitle")}</Text>
              <Text numberOfLines={2} style={styles.apiKeyDialogSubtitle}>{translate("relay.apiKeyCreateDescription")}</Text>
            </View>
          </View>
          <NativeButton title={translate("menu.close")} symbol="close" compact disabled={disabled} onPress={onClose} style={styles.dialogClose} />
        </View>
        <View style={styles.apiKeyDialogContent}>
          <View style={styles.apiKeyStageBanner}>
            <View style={styles.apiKeyStageDot} />
            <View style={styles.apiKeyStageCopy}>
              <Text style={styles.apiKeyStageLabel}>{translate("relay.apiKeyDraftStatus")}</Text>
              <Text numberOfLines={2} style={styles.apiKeyStageHint}>{translate("relay.apiKeyCreateHint")}</Text>
            </View>
          </View>
          <View style={styles.apiKeyDialogColumns}>
            <View style={[styles.apiKeyDialogPanel, styles.apiKeyFormPanel]}>
              <Text style={styles.apiKeyPanelTitle}>{translate("relay.apiKeyDetails")}</Text>
              <View style={styles.apiKeyPanelField}>
                <Text style={styles.decisionLabel}>{translate("relay.apiKeyName")}</Text>
                <NativeTextField value={name} placeholder={translate("relay.apiKeyNamePlaceholder")} editable={!disabled} onChangeText={setName} style={styles.decisionControl} />
              </View>
              <View style={styles.apiKeyPanelField}>
                <Text style={styles.decisionLabel}>{translate("relay.apiKeyGroup")}</Text>
                <NativePicker labels={groupOptions.map((group) => groupLabel(group, translate))} selectedValue={groupLabel(selectedGroup, translate)} disabled={disabled} onChange={({ nativeEvent }) => setGroupID(groupOptions[nativeEvent.index]?.id ?? "")} style={styles.decisionControl} />
              </View>
              <View style={styles.apiKeyCheckboxRow}>
                <NativeCheckbox label={translate("relay.apiKeyEnabledField")} value={enabled} disabled={disabled} onValueChange={setEnabled} />
              </View>
            </View>
            <View style={[styles.apiKeyDialogPanel, styles.apiKeyPreviewPanel]}>
              <Text style={styles.apiKeyPanelTitle}>{translate("relay.apiKeyPreviewTitle")}</Text>
              <View style={styles.apiKeyPreviewKey}>
                <Text numberOfLines={1} style={styles.apiKeyPreviewName}>{name.trim() || translate("relay.apiKeyNamePlaceholder")}</Text>
                <Text style={styles.apiKeyPreviewState}>{translate("relay.apiKeyDraftStatus")}</Text>
              </View>
              <View style={styles.apiKeyPreviewRows}>
                <View style={styles.apiKeyPreviewRow}><Text style={styles.apiKeyPreviewLabel}>{translate("relay.apiKeyGroup")}</Text><Text numberOfLines={1} style={styles.apiKeyPreviewValue}>{groupLabel(selectedGroup, translate)}</Text></View>
                <View style={styles.apiKeyPreviewRow}><Text style={styles.apiKeyPreviewLabel}>{translate("relay.apiKeyEnabledField")}</Text><Text style={styles.apiKeyPreviewValue}>{enabled ? translate("common.enable") : translate("common.disable")}</Text></View>
              </View>
              <Text style={styles.apiKeyPreviewHint}>{translate("relay.apiKeyPreviewHint")}</Text>
            </View>
          </View>
        </View>
        <View style={styles.dialogFooter}><View style={styles.decisionSpacer} /><View style={styles.dialogActions}><NativeButton title={translate("menu.cancel")} compact disabled={disabled} onPress={onClose} /><NativeButton title={translate("relay.apiKeyCreate")} primary disabled={disabled || !name.trim()} onPress={() => onCreate({ name: name.trim(), groupID: groupID || undefined, enabled })} /></View></View>
      </View>
    </View>
  </RelayDialogLayer>;
}

export function DependencyPolicyDialog<T extends string>({ visible, title, message, options, value, disabled, confirmLabel, onValueChange, onClose, onConfirm, translate }: {
  visible: boolean;
  title: string;
  message: string;
  options: Array<PolicyOption<T>>;
  value: T;
  disabled: boolean;
  confirmLabel: string;
  onValueChange: (value: T) => void;
  onClose: () => void;
  onConfirm: () => void;
  translate: Translate;
}): React.JSX.Element {
  const selectedOption = options.find((option) => option.value === value) ?? options[0];
  return <RelayDialogLayer visible={visible} onRequestClose={onClose}>
    <View style={styles.dialogBackdrop}>
      <View style={styles.decisionDialog} accessibilityViewIsModal>
        <View style={styles.dialogHeader}><Text style={styles.dialogTitle}>{title}</Text><NativeButton title={translate("menu.close")} symbol="close" compact disabled={disabled} onPress={onClose} style={styles.dialogClose} /></View>
        <View style={styles.decisionContent}>
          <Text style={styles.decisionMessage}>{message}</Text>
          <View style={styles.decisionField}><Text style={styles.decisionLabel}>{translate("relay.dependencyPolicy")}</Text><NativePicker labels={options.map((option) => option.label)} selectedValue={selectedOption.label} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onValueChange(option.value); }} style={styles.decisionControl} /></View>
          <Text style={styles.decisionHint}>{selectedOption.hint}</Text>
        </View>
        <View style={styles.dialogFooter}><View style={styles.decisionSpacer} /><View style={styles.dialogActions}><NativeButton title={translate("menu.cancel")} compact disabled={disabled} onPress={onClose} /><NativeButton title={confirmLabel} primary destructive={confirmLabel === translate("common.delete")} disabled={disabled} onPress={onConfirm} /></View></View>
      </View>
    </View>
  </RelayDialogLayer>;
}

type AccountLoading = { session: boolean; resources: boolean };

/**
 * 账号管理 for one relay station: list, add (native webview login), re-login,
 * rename, remember-password, refresh, and remove accounts, plus the station
 * connection details. Rendered inside the provider detail pane.
 */
export function StationAccountsPanel({
  station,
  accounts,
  native,
  busy,
  translate,
  commit,
  refreshAccounts,
  refreshResources,
  apiKeyActions,
  language,
  cleanups,
  stationDraft: stationDraftProp,
  onStationDraftChange,
  onStageStationUpdate,
  showConnectionFields = true,
  onStatus,
}: {
  station: RelayStation;
  accounts: RelayAccount[];
  native: NativeLeafAdapter;
  language: "system" | "en" | "zh-Hans";
  cleanups?: PendingCredentialCleanup[];
  busy: boolean;
  translate: Translate;
  commit: RelayCommit;
  refreshAccounts: () => Promise<CoreSnapshot | void>;
  refreshResources: (accountID: string) => Promise<"ready" | "unavailable">;
  apiKeyActions?: RelayApiKeyActions;
  onStatus?: (status?: string) => void;
  stationDraft?: StationDraft;
  onStationDraftChange?: (draft: StationDraft) => void;
  onStageStationUpdate?: (overrides?: StationDraft) => Promise<void>;
  /** Connection fields render at the top of the provider detail instead. */
  showConnectionFields?: boolean;
}): React.JSX.Element {
  const stationAccounts = useMemo(() => station.accountIDs
    .map((id) => accounts.find((account) => account.id === id))
    .filter((account): account is RelayAccount => Boolean(account)), [accounts, station.accountIDs]);
  const [selectedID, setSelectedID] = useState<string>();
  const [loading, setLoading] = useState<Record<string, AccountLoading>>({});
  const loadingRef = useRef(loading);
  loadingRef.current = loading;
  const [localSignedIn, setLocalSignedIn] = useState<Set<string>>(() => new Set());
  const [loginFailure, setLoginFailure] = useState<Set<string>>(() => new Set());
  const [formBusy, setFormBusy] = useState(false);
  const [removal, setRemoval] = useState<{ account: RelayAccount }>();
  const [removalPolicy, setRemovalPolicy] = useState<LocalDependencyPolicy>("detach");
  // What a sign-in may save is asked by the native login flow after the
  // webview login succeeds; adding an account goes straight to sign-in.
  const beginAddLogin = async (): Promise<void> => {
    await startPendingLogin();
  };
  const [internalStationDraft, setInternalStationDraft] = useState<StationDraft>({});
  const stationDraft = stationDraftProp ?? internalStationDraft;
  const setStationDraftValue = (draft: StationDraft): void => {
    if (onStationDraftChange) onStationDraftChange({ ...stationDraftRef.current, ...draft });
    else setInternalStationDraft((current) => ({ ...current, ...draft }));
  };
  const stationDraftRef = useRef<StationDraft>({});
  stationDraftRef.current = stationDraft;
  const [stationBusy, setStationBusy] = useState(false);
  const [feedback, setFeedback] = useState<string>();
  const accountsRef = useRef(stationAccounts);
  accountsRef.current = stationAccounts;
  const selected = stationAccounts.find((account) => account.id === selectedID) ?? stationAccounts[0];
  const controlsBusy = busy || formBusy || stationBusy;
  const publish = (message: string): void => {
    setFeedback(undefined);
    onStatus?.(message);
  };
  const effectiveLoginStatus = (account: RelayAccount): "signed_in" | "signed_out" | "expired" | "unknown" => {
    if (loginFailure.has(account.id)) return "expired";
    if (localSignedIn.has(account.id)) return "signed_in";
    if (account.loginStatus === "signed_in" || account.loginStatus === "signed_out" || account.loginStatus === "expired") return account.loginStatus;
    return "unknown";
  };
  const updateLoading = (accountID: string, kind: keyof AccountLoading, value: boolean): void => {
    const current = loadingRef.current;
    const previous = current[accountID] ?? { session: false, resources: false };
    const next = { ...current };
    const merged = { ...previous, [kind]: value };
    if (!merged.session && !merged.resources) delete next[accountID];
    else next[accountID] = merged;
    loadingRef.current = next;
    setLoading(next);
  };
  const isAccountLoading = (accountID: string): boolean => {
    const state = loadingRef.current[accountID];
    return Boolean(state?.session || state?.resources);
  };
  useEffect(() => {
    if (selected && selected.id === selectedID) return;
    setSelectedID(selected?.id);
  }, [selected, selectedID]);
  useEffect(() => {
    const ids = new Set(stationAccounts.map((account) => account.id));
    setLocalSignedIn((current) => {
      const next = new Set([...current].filter((id) => ids.has(id)));
      return next.size === current.size ? current : next;
    });
    setLoginFailure((current) => {
      const next = new Set([...current].filter((id) => ids.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [stationAccounts]);
  const markLoginFailure = (accountID: string, failed: boolean): void => {
    setLoginFailure((current) => {
      const next = new Set(current);
      if (failed) next.add(accountID);
      else next.delete(accountID);
      return next;
    });
  };
  const markLocalSignedIn = (accountID: string, signedIn: boolean): void => {
    setLocalSignedIn((current) => {
      const next = new Set(current);
      if (signedIn) next.add(accountID);
      else next.delete(accountID);
      return next;
    });
  };
  const refreshAccountResources = async (target: ResourceRefreshTarget, silent = false): Promise<"ready" | "unavailable"> => {
    if (isAccountLoading(target.id)) {
      return accountsRef.current.find((item) => item.id === target.id)?.resourceStatus === "ready" ? "ready" : "unavailable";
    }
    updateLoading(target.id, "resources", true);
    if (!silent) setFeedback(undefined);
    try {
      const status = await refreshResources(target.id);
      try {
        await refreshAccounts();
      } catch {
        // The resource refresh already completed in Core.
      }
      return status;
    } catch {
      if (!silent) publish(translate("relay.resourcesUnavailable"));
      return "unavailable";
    } finally {
      updateLoading(target.id, "resources", false);
    }
  };
  const restoreSavedSession = async (account: RelayAccount): Promise<boolean> => {
    updateLoading(account.id, "session", true);
    try {
      const result = await native.restoreRelaySession({
        accountId: account.id,
        type: account.type,
        label: account.label,
        origin: account.origin,
        username: account.username || undefined,
      });
      const signedIn = result?.loginStatus === "signed_in";
      markLoginFailure(account.id, !signedIn);
      markLocalSignedIn(account.id, signedIn);
      try {
        await refreshAccounts();
      } catch {
        // The native session result is authoritative.
      }
      return signedIn;
    } catch {
      markLoginFailure(account.id, false);
      markLocalSignedIn(account.id, false);
      return false;
    } finally {
      updateLoading(account.id, "session", false);
    }
  };
  const loginAccount = async (account: AddedRelayAccount): Promise<boolean> => {
    setFormBusy(true);
    updateLoading(account.id, "session", true);
    setFeedback(undefined);
    try {
      const result = await native.relayLogin({
        accountId: account.id,
        type: account.type,
        label: account.label,
        origin: account.origin,
        language,
        username: account.username || undefined,
      });
      if (!result) {
        // A cancelled or failed sign-in only surfaces as the stale brown
        // account row; no login-status copy appears in the status line.
        markLoginFailure(account.id, true);
        markLocalSignedIn(account.id, false);
        return false;
      }
      markLoginFailure(account.id, false);
      markLocalSignedIn(account.id, true);
      const username = text(result.username).trim();
      if (username && username !== account.username) {
        try {
          await commit("account.update", { id: account.id, username });
        } catch {
          // The username label is cosmetic.
        }
      }
      await refreshAccountResources({ id: account.id }, true);
      return true;
    } catch {
      markLoginFailure(account.id, true);
      markLocalSignedIn(account.id, false);
      return false;
    } finally {
      updateLoading(account.id, "session", false);
      setFormBusy(false);
    }
  };
  // Restore saved sessions quietly once per account per panel mount. This
  // mirrors the old workspace behaviour: selecting a provider must surface
  // fresh login state and resources without a manual refresh click.
  const attemptedAccounts = useRef(new Set<string>());
  useEffect(() => {
    if (busy) return;
    for (const account of stationAccounts) {
      if (attemptedAccounts.current.has(account.id) || isAccountLoading(account.id)) continue;
      attemptedAccounts.current.add(account.id);
      void (async () => {
        if (await restoreSavedSession(account)) {
          // Opening the station is the probe: refresh resources so the
          // provided keys reflect the live account state.
          await refreshAccountResources(account, true);
          return;
        }
        const canAutoLogin = account.rememberPassword && account.passwordSaved && Boolean(account.username.trim());
        if (canAutoLogin) await loginAccount(account);
      })();
    }
  }, [busy, stationAccounts]);
  // The add flow never reserves an account slot. The webview login runs
  // first (modal sheet on the provider window); Core creates the account
  // shell only when sign-in actually succeeds (pending_account), so a
  // cancelled login leaves nothing behind. Whether the password is kept is
  // decided by the post-login prompt inside the native flow.
  const startPendingLogin = async (): Promise<void> => {
    setFormBusy(true);
    setFeedback(undefined);
    const pendingID = `login-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const stationType = station.type ?? "newapi";
    try {
      const result = await native.relayLogin({
        accountId: pendingID,
        type: stationType,
        label: stationDisplayName(station, translate),
        origin: station.origin,
        language,
        pendingAccount: true,
        ...(station.persisted ? { stationId: station.id } : {}),
        stationName: stationDisplayName(station, translate),
        stationType,
        stationOrigin: station.origin,
      });
      if (!result) return;
      await refreshAccounts();
      setSelectedID(pendingID);
    } catch {
      // A failed pending login leaves nothing behind and stays silent.
    } finally {
      setFormBusy(false);
    }
  };
  const loginSelected = async (): Promise<void> => {
    if (!selected) return;
    if (effectiveLoginStatus(selected) === "signed_in") return;
    if (await restoreSavedSession(selected)) {
      await refreshAccountResources(selected);
      return;
    }
    await loginAccount(selected);
  };
  const removeSelected = async (): Promise<void> => {
    if (!removal) return;
    setFormBusy(true);
    setFeedback(undefined);
    try {
      await commit("account.delete", { id: removal.account.id, dependency_policy: removalPolicy });
      setSelectedID(undefined);
      try {
        await native.clearRelayCredentials(removal.account.id);
        await commit("credential_cleanup_confirm", { id: removal.account.id, kind: "credentials" });
      } catch {
        // Core retains a secret-free cleanup tombstone for retry.
      }
      await refreshAccounts();
      setRemoval(undefined);
    } catch {
      publish(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const stageStationUpdate = async (overrides: StationDraft = {}): Promise<void> => {
    const draft = stationDraftRef.current;
    const name = (overrides.name ?? draft.name ?? stationDisplayName(station, translate)).trim();
    const origin = normalizeRelayOrigin(overrides.origin ?? draft.origin ?? station.origin);
    const type = overrides.type ?? draft.type ?? station.type;
    if (!name || !origin) return;
    const dirty = name !== stationDisplayName(station, translate).trim()
      || origin !== normalizeRelayOrigin(station.origin)
      || type !== station.type;
    if (!dirty) return;
    setStationBusy(true);
    setFeedback(undefined);
    try {
      await commit("station.update", { id: station.id, name, origin, type });
      await refreshAccounts();
      publish(translate("relay.stationUpdateStaged"));
    } catch {
      publish(translate("relay.operationFailed"));
    } finally {
      setStationBusy(false);
    }
  };
  const selectedStatus = selected ? effectiveLoginStatus(selected) : "unknown";
  const selectedSignedIn = selectedStatus === "signed_in";
  const accountRows = stationAccounts.map((account) => ({
    key: account.id,
    cells: [
      accountDisplayName(account, translate),
      translate(`relay.status.${effectiveLoginStatus(account)}`),
      account.balance === null ? translate("common.none") : balanceLabel(account, translate),
    ],
  }));
  // A stale login turns the whole row brown until the next successful probe.
  const alertAccountRows = stationAccounts
    .filter((account) => effectiveLoginStatus(account) === "expired")
    .map((account) => account.id);
  const removalKeys = removal?.account.resources.length ?? 0;
  const selectedRemovalModels = removal?.account.linkedModelCount ?? 0;
  const retryCleanup = async (cleanup: PendingCredentialCleanup): Promise<void> => {
    setFormBusy(true);
    try {
      await native.clearRelayCredentials(cleanup.accountID);
      await commit("credential_cleanup_confirm", { id: cleanup.accountID, kind: cleanup.kind });
      await refreshAccounts();
    } catch {
      // Core keeps the tombstone for a later retry.
    } finally {
      setFormBusy(false);
    }
  };
  const stationCleanups = (cleanups ?? []).filter((cleanup) => station.accountIDs.includes(cleanup.accountID));
  return <View style={styles.accountsPanel}>
    {stationCleanups.map((cleanup) => <View key={`cleanup:${cleanup.accountID}`} style={styles.cleanupRow}>
      <Text numberOfLines={2} style={styles.cleanupText}>{translate("relay.credentialsCleanupPending", { label: cleanup.label })}</Text>
      <NativeButton title={translate("relay.retryCleanup")} compact disabled={controlsBusy} onPress={() => { void retryCleanup(cleanup); }} style={styles.panelActionButton} />
    </View>)}
    <View style={styles.panelHeader}>
      <Text style={styles.panelTitle}>{translate("providers.accounts")}</Text>
      <View style={styles.panelActions}>
        <NativeButton title="" symbol="plus" compact toolTip={translate("relay.addAccount")} accessibilityLabel={translate("relay.addAccount")} disabled={controlsBusy || stationAccounts.length >= 8} onPress={() => { void beginAddLogin(); }} style={styles.panelActionButton} />
        <NativeButton title="" symbol="minus" compact destructive toolTip={translate("relay.removeLocal")} accessibilityLabel={translate("relay.removeLocal")} disabled={controlsBusy || !selected} onPress={() => { if (selected) { setRemovalPolicy("detach"); setRemoval({ account: selected }); } }} style={styles.panelActionButton} />
      </View>
    </View>
    <View style={styles.fieldRow}>
      <Text numberOfLines={1} style={[styles.fieldLabel, { width: undefined }]}>{translate("relay.type")}</Text>
      <NativePicker
        labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]}
        selectedValue={relayTypeLabel(stationDraft.type ?? station.type ?? "newapi", translate)}
        disabled={controlsBusy || stationAccounts.length === 0}
        onChange={({ nativeEvent }) => {
          const nextType = nativeEvent.index === 1 ? "sub2api" : "newapi";
          setStationDraftValue({ type: nextType });
          void stageStationUpdate({ type: nextType });
        }}
        style={styles.relayTypeInline}
      />
    </View>
    {accountRows.length > 0 ? <NativeTable
      columns={[{ label: translate("relay.accounts"), width: 96 }, { label: translate("providers.authStatus"), width: 74 }, { label: translate("relay.balance"), width: 58 }]}
      rows={accountRows}
      selectedKey={selected?.id ?? ""}
      disabledRowKeys={[]}
      alertRowKeys={alertAccountRows}
      compact
      striped
      onSelectionChange={setSelectedID}
      style={styles.accountsTable}
    /> : <View style={styles.accountsEmpty}><Text style={styles.accountsEmptyText}>{translate("relay.stationNoAccounts")}</Text></View>}
    {selected ? <View style={styles.accountEditor}>
      <View style={styles.fieldRow}>
        <Text style={[styles.fieldLabel, { width: 44 }]}>{translate("relay.accountField")}</Text>
        <Text numberOfLines={1} style={styles.accountNameValue}>{accountDisplayName(selected, translate)}</Text>
        {!selectedSignedIn ? <NativeButton title={translate("relay.goLogin")} compact disabled={controlsBusy || isAccountLoading(selected.id)} onPress={() => { void loginSelected(); }} /> : null}
      </View>
      <View style={styles.accountActionsRow}>
        <NativeCheckbox
          key={`auto-grouping:${selected.id}`}
          label={translate("relay.apiKeyAutoGrouping")}
          value={selected.autoGrouping}
          disabled={controlsBusy || !apiKeyActions?.setAutoGrouping}
          onValueChange={(enabled) => {
            void (async () => {
              setFormBusy(true);
              try {
                await apiKeyActions?.setAutoGrouping?.(selected.id, enabled);
                await refreshAccounts();
                onStatus?.(translate("relay.apiKeyAutoGroupingStaged"));
              } catch {
                onStatus?.(translate("relay.operationFailed"));
              } finally {
                setFormBusy(false);
              }
            })();
          }}
          style={styles.autoGroupingInline}
        />
      </View>
    </View> : null}
    <DependencyPolicyDialog
      visible={Boolean(removal)}
      title={translate("relay.removeLocalTitle")}
      message={removal ? translate("relay.removeAccountBody", { label: accountDisplayName(removal.account, translate), keys: removalKeys, models: selectedRemovalModels }) : ""}
      options={[
        { value: "detach", label: translate("relay.policyRelease"), hint: translate("relay.policyReleaseHint") },
        { value: "delete_models", label: translate("relay.policyDeleteModels"), hint: translate("relay.policyDeleteModelsHint") },
      ]}
      value={removalPolicy}
      disabled={controlsBusy}
      confirmLabel={translate("relay.removeLocal")}
      onValueChange={setRemovalPolicy}
      onClose={() => setRemoval(undefined)}
      onConfirm={() => { void removeSelected(); }}
      translate={translate}
    />
  </View>;
}

/** A provided (station-managed) key row projected from one relay account. */
export type ProvidedKeyRow = {
  key: string;
  account: RelayAccount;
  resource: RelayResource;
  label: string;
  keyName: string;
  accountLabel: string;
  group: string;
  unavailable: boolean;
};

/** The account prefix in relay key labels: email accounts collapse to the local part. */
export function accountKeyPrefix(account: RelayAccount, translate: Translate): string {
  const base = account.username.trim() || accountDisplayName(account, translate);
  const at = base.indexOf("@");
  return at > 0 ? base.slice(0, at) : base;
}

export function providedKeyRows(accounts: RelayAccount[], translate: Translate): ProvidedKeyRow[] {
  return accounts.flatMap((account) => (account.autoGrouping
    ? account.resources.filter((resource) => !resourceAutoGroupingUnavailable(resource, account.groups))
    : account.resources
  ).map((resource) => ({
    key: `${account.id}:${resource.id}`,
    account,
    resource,
    keyName: resource.apiName || resource.name,
    // Relay keys display uniformly as 账号名（邮箱取前缀）/分组名.
    label: `${accountKeyPrefix(account, translate)}/${resourceGroupLabel(resource, account.groups, translate)}`,
    accountLabel: accountDisplayName(account, translate),
    group: resourceGroupLabel(resource, account.groups, translate),
    unavailable: resourceGroupUnavailable(resource, account.groups),
  })));
}

/**
 * 供应商提供的密钥列表: station-managed API keys across the bound station's
 * accounts. Keys are created, updated, and deleted through staged relay
 * actions; values stay behind the native secure-input capability.
 */
/** Shared host callbacks the provider workspace hands to every relay panel. */
export type RelayWorkspaceBridge = {
  commit: RelayCommit;
  detectType: (origin: string) => Promise<RelayType | undefined>;
  refreshResources: (accountID: string) => Promise<"ready" | "unavailable">;
  /** Returns the refreshed snapshot so callers avoid stale projections. */
  refreshAccounts: () => Promise<CoreSnapshot | void>;
  apiKeyActions: RelayApiKeyActions;
};

const colors = {
  window: Platform.OS === "macos" ? PlatformColor("windowBackgroundColor") : PlatformColor("Window"),
  text: Platform.OS === "macos" ? PlatformColor("labelColor") : PlatformColor("WindowText"),
  secondary: Platform.OS === "macos" ? PlatformColor("secondaryLabelColor") : PlatformColor("GrayText"),
  separator: Platform.OS === "macos" ? PlatformColor("separatorColor") : PlatformColor("ControlStrokeColorDefault"),
  panel: Platform.OS === "macos" ? PlatformColor("controlBackgroundColor") : PlatformColor("ControlFillColorDefault"),
  accent: Platform.OS === "macos" ? PlatformColor("systemBlueColor") : PlatformColor("AccentFillColorDefault"),
  accentText: Platform.OS === "macos" ? PlatformColor("alternateSelectedControlTextColor") : PlatformColor("TextOnAccentFillColorPrimary"),
  success: "#2A9D68",
};

const compactStyles = StyleSheet.create({
  formRow: { minHeight: 26, gap: 3 },
  formValue: { minHeight: 24, gap: 3 },
});

const styles = StyleSheet.create({
  hidden: { display: "none" },
  accountsPanel: { minWidth: 0, gap: 6, paddingTop: 6, borderTopWidth: 1, borderTopColor: colors.separator },
  accountsPanelGap: { minWidth: 0, gap: 4 },
  cleanupRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" },
  cleanupText: { flex: 1, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  panelHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  panelTitle: { flex: 1, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  panelActions: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 },
  panelActionButton: { width: 22, minWidth: 22, height: 22 },
  panelFeedback: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  accountsTable: { flex: 0, height: 84, minHeight: 84, flexShrink: 0 },
  accountsEmpty: { minHeight: 40, alignItems: "center", justifyContent: "center", paddingHorizontal: 10, paddingVertical: 8, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel },
  accountsEmptyText: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, textAlign: "center" },
  accountEditor: { minWidth: 0, gap: 5 },
  accountNameValue: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE },
  relayTypeInline: { width: 96, height: 26, flexShrink: 0 },
  autoGroupingInline: { flexShrink: 0, flexGrow: 0 },
  accountActionsRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" },
  fieldRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  fieldLabel: { width: 64, flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  fieldControl: { flex: 1, minWidth: 0, height: 26 },
  autoGroupingCheckbox: { flexShrink: 0 },
  setupProgress: { width: "100%", flexDirection: "row", alignItems: "center", minHeight: 22, gap: 18 },
  setupProgressStep: { flexDirection: "row", alignItems: "center", gap: 7, flexShrink: 0 },
  setupProgressBadge: { width: 20, height: 20, borderRadius: 10, borderWidth: 1, borderColor: colors.separator, alignItems: "center", justifyContent: "center", backgroundColor: colors.window },
  setupProgressBadgeCurrent: { borderColor: colors.accent, backgroundColor: colors.accent },
  setupProgressBadgeDone: { borderColor: colors.accent, backgroundColor: colors.window },
  setupProgressNumber: { color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  setupProgressNumberCurrent: { color: colors.accentText },
  setupProgressLabel: { color: colors.secondary, fontSize: UI_FONT_SIZE },
  setupProgressLabelCurrent: { color: colors.text, fontWeight: "600" },
  formRow: { width: "100%", minHeight: 30, flexDirection: "column", alignItems: "stretch", gap: 5 },
  formLabel: { width: "100%", minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  formValue: { width: "100%", minWidth: 0, minHeight: 26, justifyContent: "center", gap: 4 },
  relayDialogLayer: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, zIndex: 100 },
  dialogBackdrop: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24, backgroundColor: "rgba(0, 0, 0, 0.22)" },
  dialogHeader: { height: 36, minHeight: 36, paddingHorizontal: 12, flexDirection: "row", alignItems: "center", borderBottomWidth: 1, borderBottomColor: colors.separator },
  dialogClose: { width: 22, minWidth: 22, height: 22 },
  dialogTitle: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  dialogFooter: { minHeight: 44, paddingHorizontal: 12, paddingVertical: 7, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 6, borderTopWidth: 1, borderTopColor: colors.separator },
  decisionDialog: { width: "94%", maxWidth: 560, minHeight: 220, maxHeight: "86%", borderRadius: 7, overflow: "hidden", backgroundColor: colors.window },
  decisionContent: { padding: 13, gap: 8 },
  decisionMessage: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  decisionField: { minWidth: 0, gap: 3 },
  decisionLabel: { color: colors.secondary, fontSize: UI_FONT_SIZE },
  decisionControl: { width: "100%", height: 26 },
  decisionHint: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  decisionSpacer: { flex: 1 },
  dialogActions: { flexDirection: "row", alignItems: "center", gap: 6 },
  apiKeyCreateDialog: { maxWidth: 640, minHeight: 300 },
  apiKeyDialogHeader: { height: 54, minHeight: 54, paddingHorizontal: 14, gap: 10 },
  apiKeyDialogTitleWrap: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "center", gap: 9 },
  apiKeyDialogIcon: { width: 28, height: 28, borderRadius: 7, alignItems: "center", justifyContent: "center", backgroundColor: colors.accent },
  apiKeyDialogIconText: { color: colors.accentText, fontSize: UI_FONT_SIZE, lineHeight: 22, fontWeight: "700" },
  apiKeyDialogTitleBlock: { flex: 1, minWidth: 0, gap: 1 },
  apiKeyDialogSubtitle: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  apiKeyDialogContent: { padding: 14, gap: 10 },
  apiKeyStageBanner: { minHeight: 42, paddingHorizontal: 10, paddingVertical: 7, borderWidth: 1, borderColor: colors.accent, borderRadius: 7, flexDirection: "row", alignItems: "flex-start", gap: 8, backgroundColor: colors.panel },
  apiKeyStageDot: { width: 8, height: 8, marginTop: 3, borderRadius: 4, backgroundColor: colors.accent },
  apiKeyStageCopy: { flex: 1, minWidth: 0, gap: 1 },
  apiKeyStageLabel: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 17, fontWeight: "600" },
  apiKeyStageHint: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  apiKeyDialogColumns: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  apiKeyDialogPanel: { minWidth: 0, padding: 10, gap: 9, borderWidth: 1, borderColor: colors.separator, borderRadius: 7 },
  apiKeyFormPanel: { flexGrow: 1, flexBasis: 280 },
  apiKeyPreviewPanel: { flexGrow: 1, flexBasis: 250, backgroundColor: colors.panel },
  apiKeyPanelTitle: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  apiKeyPanelField: { minWidth: 0, gap: 3 },
  apiKeyCheckboxRow: { minHeight: 24, justifyContent: "center" },
  apiKeyPreviewKey: { minHeight: 34, paddingHorizontal: 9, paddingVertical: 6, borderRadius: 6, flexDirection: "row", alignItems: "center", gap: 7, backgroundColor: colors.window },
  apiKeyPreviewName: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  apiKeyPreviewState: { flexShrink: 0, paddingHorizontal: 5, paddingVertical: 2, borderRadius: 4, color: colors.accent, fontSize: UI_FONT_SIZE, fontWeight: "600", backgroundColor: colors.panel },
  apiKeyPreviewRows: { gap: 5 },
  apiKeyPreviewRow: { minWidth: 0, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 },
  apiKeyPreviewLabel: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE },
  apiKeyPreviewValue: { color: colors.text, fontSize: UI_TIP_FONT_SIZE },
  apiKeyPreviewHint: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
});

export type { LocalDependencyPolicy, RemoteDeletePolicy };
