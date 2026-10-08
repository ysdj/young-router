import React, { useEffect, useMemo, useRef, useState } from "react";
import { Platform, PlatformColor, Pressable, StyleSheet, Text, View } from "react-native";
import type { CoreSnapshot, NativeLeafAdapter, RelayGroupManagerResult } from "../types";
import { NativeButton, NativeCheckbox, NativePicker, NativeTable, NativeTextField } from "./NativeControls";
import { usePendingAction } from "./pendingAction";
import { setGroupManagerOpen } from "./providerWizardGate";
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
  /** Core already staged this key for deletion; Apply removes it remotely. */
  pendingDelete: boolean;
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
  /**
   * How long ago Core's login observation for this account was made, or null
   * when it cannot say.  The pane compares it with the station's re-check
   * window before it asks for another station round trip.
   */
  loginObservedSecondsAgo: number | null;
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

type ResourceRefreshTarget = { id: string; resources?: RelayResource[] };
export type StationDraft = Partial<Pick<RelayStation, "name" | "origin" | "type">>;
type PolicyOption<T extends string> = { value: T; label: string; hint: string };
/**
 * How many model names the 分组管理 window lays out for one key, and how much
 * text those names may take.  The window shows the whole list, so these are
 * sanity bounds for a station that reports hundreds of models rather than a
 * display truncation; a list that hits one states the rest with an ellipsis.
 */
const GROUP_MANAGER_MODEL_LIMIT = 256;
const GROUP_MANAGER_MODEL_LIST_CHARS = 12000;

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

/** The 分组 field: the group's name alone; 倍率 has its own row and column. */
export function groupLabel(group: RelayGroup, translate: Translate): string {
  return group.name || translate("relay.apiKeyUngrouped");
}

function resourceGroup(resource: RelayResource, groups: RelayGroup[]): RelayGroup | undefined {
  return groups.find((group) => group.id === resource.groupID);
}

function resourceGroupName(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  const group = resourceGroup(resource, groups);
  return group?.name || resource.groupName || (resource.groupID ? resource.groupID : translate("relay.apiKeyUngrouped"));
}

/** The 倍率 value: the group's rate, or nothing when it has none. */
function groupRateLabel(group: RelayGroup | undefined): string {
  const multiplier = group ? groupMultiplier(group.multiplier) : null;
  return multiplier === null ? "" : `×${multiplier}`;
}

/**
 * The 模型列表: every model the station reports for one key, in the station's
 * own order.  The window lays the names out itself, so the list travels as names
 * rather than a pre-joined sentence.
 */
function resourceModelList(resource: RelayResource): string[] {
  const names: string[] = [];
  let length = 0;
  for (const model of resource.models) {
    if (names.length >= GROUP_MANAGER_MODEL_LIMIT || length + model.length > GROUP_MANAGER_MODEL_LIST_CHARS) break;
    names.push(model);
    length += model.length + 1;
  }
  if (names.length < resource.models.length) names.push("…");
  return names;
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
      loginObservedSecondsAgo: typeof item.login_observed_seconds_ago === "number" && Number.isFinite(item.login_observed_seconds_ago) ? item.login_observed_seconds_ago : null,
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
          pendingDelete: entry.pending_delete === true,
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

function balanceLabel(account: RelayAccount, translate: Translate): string {
  return account.balance === null ? translate("common.none") : `$${account.balance.toFixed(2)}`;
}

type PendingCredentialCleanup = {
  accountID: string;
  label: string;
  kind: "credentials";
};

/**
 * How long Core's login observation answers a later check for, in seconds.
 *
 * Core publishes one window for the whole relay domain and every panel applies
 * the same number, so the account pane can skip a station round trip the window
 * already covers instead of probing a session it verified moments ago.
 */
export function relaySessionRecheckSeconds(snapshot?: CoreSnapshot): number | undefined {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  const value = state.session_recheck_seconds;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

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

export function NativeFormRow({ label, required = false, children }: { label: string; required?: boolean; children: React.ReactNode }): React.JSX.Element {
  // A required control says so at its own label: a "fill in the required
  // fields" message is only actionable when the fields wear the mark.
  return <View style={[styles.formRow, compactStyles.formRow]}>
    <Text style={styles.formLabel}>{label}{required ? <Text style={styles.formLabelRequired}>＊</Text> : null}</Text>
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
  // The form resets when the dialog opens, never while it is up: `groups` is a
  // freshly built array on every parent render, so an effect keyed on it used
  // to clear the name the user was typing — and re-check 启用 — whenever the
  // pane behind it repainted.  A group list that arrives or changes while the
  // dialog is open only repairs a choice that no longer exists.
  const opened = useRef(false);
  useEffect(() => {
    if (!visible) {
      opened.current = false;
      return;
    }
    if (opened.current) {
      setGroupID((current) => groups.some((group) => group.id === current) ? current : groups[0]?.id ?? "");
      return;
    }
    opened.current = true;
    setName("");
    setGroupID(groups[0]?.id ?? "");
    setEnabled(true);
  }, [visible, groups]);
  const groupOptions = groups.length > 0 ? groups : [{ id: "", name: translate("relay.apiKeyUngrouped"), multiplier: null }];
  const selectedGroup = groupOptions.find((group) => group.id === groupID) ?? groupOptions[0];
  return <RelayDialogLayer visible={visible} onRequestClose={onClose}>
    <View style={styles.dialogBackdrop}>
      {/* The dimmed area outside the card is the dialog's own dismiss target:
          clicking it closes the dialog instead of a click being swallowed by
          the layer.  The card is rendered after this, so it stays on top. */}
      <Pressable accessible={false} onPress={onClose} style={StyleSheet.absoluteFill} />
      <View style={[styles.decisionDialog, styles.apiKeyCreateDialog]} accessibilityViewIsModal>
        <View style={[styles.dialogHeader, styles.apiKeyDialogHeader]}>
          <View style={styles.apiKeyDialogTitleWrap}>
            <View style={styles.apiKeyDialogIcon}><Text style={styles.apiKeyDialogIconText}>+</Text></View>
            <View style={styles.apiKeyDialogTitleBlock}>
              <Text style={styles.dialogTitle}>{translate("relay.apiKeyCreateTitle")}</Text>
              <Text numberOfLines={2} style={styles.apiKeyDialogSubtitle}>{translate("relay.apiKeyCreateDescription")}</Text>
            </View>
          </View>
          <NativeButton title={translate("status.close")} symbol="close" compact disabled={disabled} onPress={onClose} style={styles.dialogClose} />
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
                <View style={styles.apiKeyPreviewRow}><Text style={styles.apiKeyPreviewLabel}>{translate("relay.apiKeyMultiplier")}</Text><Text numberOfLines={1} style={styles.apiKeyPreviewValue}>{groupRateLabel(selectedGroup) || translate("common.none")}</Text></View>
                <View style={styles.apiKeyPreviewRow}><Text style={styles.apiKeyPreviewLabel}>{translate("relay.apiKeyEnabledField")}</Text><Text style={styles.apiKeyPreviewValue}>{enabled ? translate("common.enable") : translate("common.disable")}</Text></View>
              </View>
              <Text style={styles.apiKeyPreviewHint}>{translate("relay.apiKeyPreviewHint")}</Text>
            </View>
          </View>
        </View>
        <View style={styles.dialogFooter}><View style={styles.decisionSpacer} /><View style={styles.dialogActions}><NativeButton title={translate("status.cancel")} compact disabled={disabled} onPress={onClose} /><NativeButton title={translate("relay.apiKeyCreate")} primary disabled={disabled || !name.trim()} onPress={() => onCreate({ name: name.trim(), groupID: groupID || undefined, enabled })} /></View></View>
      </View>
    </View>
  </RelayDialogLayer>;
}

export function DependencyPolicyDialog<T extends string>({ visible, title, message, options, value, disabled, busy = false, confirmLabel, destructive = false, onValueChange, onClose, onConfirm, translate }: {
  visible: boolean;
  title: string;
  message: string;
  options: Array<PolicyOption<T>>;
  value: T;
  disabled: boolean;
  /** The confirm action is running: it reports progress in place instead of graying out. */
  busy?: boolean;
  confirmLabel: string;
  /**
   * The answer cannot be undone: it draws destructive, like every other
   * destructive answer in the app.  The caller states it instead of the dialog
   * inferring it from the label, which only a 删除 translation happened to match.
   */
  destructive?: boolean;
  onValueChange: (value: T) => void;
  onClose: () => void;
  onConfirm: () => void;
  translate: Translate;
}): React.JSX.Element {
  const selectedOption = options.find((option) => option.value === value) ?? options[0];
  return <RelayDialogLayer visible={visible} onRequestClose={onClose}>
    <View style={styles.dialogBackdrop}>
      {/* Same dismiss target as the key dialog: the scrim belongs to the
          question it dims, and a click on it is an answer. */}
      <Pressable accessible={false} onPress={onClose} style={StyleSheet.absoluteFill} />
      <View style={styles.decisionDialog} accessibilityViewIsModal>
        <View style={styles.dialogHeader}><Text style={styles.dialogTitle}>{title}</Text><NativeButton title={translate("status.close")} symbol="close" compact disabled={disabled} onPress={onClose} style={styles.dialogClose} /></View>
        <View style={styles.decisionContent}>
          <Text style={styles.decisionMessage}>{message}</Text>
          <View style={styles.decisionField}><Text style={styles.decisionLabel}>{translate("relay.dependencyPolicy")}</Text><NativePicker labels={options.map((option) => option.label)} selectedValue={selectedOption.label} disabled={disabled} onChange={({ nativeEvent }) => { const option = options[nativeEvent.index]; if (option) onValueChange(option.value); }} style={styles.decisionControl} /></View>
          <Text style={styles.decisionHint}>{selectedOption.hint}</Text>
        </View>
        <View style={styles.dialogFooter}><View style={styles.decisionSpacer} /><View style={styles.dialogActions}><NativeButton title={translate("status.cancel")} compact disabled={disabled} onPress={onClose} /><NativeButton title={confirmLabel} primary destructive={destructive} busy={busy} disabled={disabled && !busy} onPress={onConfirm} /></View></View>
      </View>
    </View>
  </RelayDialogLayer>;
}

/**
 * The lanes one account's work is reported in.
 *
 * ``session`` is a sign-in the user started (去登录, or the ＋ account flow):
 * that wait owns the login column's 登录中.  ``resources`` is the station round
 * trip lane — a silent session restore and the key-list read are the same wait
 * on the same station, so they share it, and neither repaints a login state the
 * pane is about to agree with.
 */
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
  detectType,
  language,
  cleanups,
  sessionRecheckSeconds,
  stationDraft: stationDraftProp,
  onStationDraftChange,
  onStageStationUpdate,
  showConnectionFields = true,
  onStatus,
  applyStagedQuietly,
}: {
  station: RelayStation;
  accounts: RelayAccount[];
  native: NativeLeafAdapter;
  language: "system" | "en" | "zh-Hans";
  cleanups?: PendingCredentialCleanup[];
  /**
   * How long Core's login observation answers a later check for.  The pane
   * applies it before asking the host to probe, so re-entering within the
   * window costs no station round trip.
   */
  sessionRecheckSeconds?: number;
  busy: boolean;
  translate: Translate;
  commit: RelayCommit;
  refreshAccounts: () => Promise<CoreSnapshot | void>;
  /** ``force`` skips the reuse window: a sign-in that just landed reads again. */
  refreshResources: (accountID: string, options?: { force?: boolean }) => Promise<"ready" | "unavailable">;
  apiKeyActions?: RelayApiKeyActions;
  /** Relay-family auto-detection; the station type has no manual select. */
  detectType?: (origin: string) => Promise<RelayType | undefined>;
  onStatus?: (status?: string) => void;
  /**
   * Applies the staged relay draft without a word in this window's status bar:
   * 分组管理 applies its own save while its sheet is on screen, so the sheet
   * states the outcome and the pane it was opened from stays silent.
   */
  applyStagedQuietly: () => Promise<void>;
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
  // Which account action is running: the button that started one keeps its own
  // inline spinner instead of graying out.
  const [pendingAction, runPendingAction] = usePendingAction<"cleanup" | "add" | "login" | "remove" | "refresh">();
  const [removal, setRemoval] = useState<{ account: RelayAccount }>();
  const [removalPolicy, setRemovalPolicy] = useState<LocalDependencyPolicy>("detach");
  // What a sign-in may save is asked by the native login flow after the
  // webview login succeeds; adding an account goes straight to sign-in.
  const beginAddLogin = async (): Promise<void> => {
    if (pendingAction === "add") return;
    await runPendingAction("add", startPendingLogin);
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
  const accountsRef = useRef(stationAccounts);
  accountsRef.current = stationAccounts;
  // The account list owns the selection: an explicitly cleared one (a click
  // below the rows) leaves the editor and its − empty instead of acting on an
  // account the list no longer highlights, while the first account still
  // opens the pane on load.
  const selected = stationAccounts.find((account) => account.id === selectedID)
    ?? (selectedID === undefined ? stationAccounts[0] : undefined);
  const controlsBusy = busy || formBusy || stationBusy;
  const publish = (message: string): void => {
    onStatus?.(message);
  };
  const effectiveLoginStatus = (account: RelayAccount): "signed_in" | "signed_out" | "expired" | "unknown" => {
    if (loginFailure.has(account.id)) return "expired";
    if (localSignedIn.has(account.id)) return "signed_in";
    if (account.loginStatus === "signed_in" || account.loginStatus === "signed_out" || account.loginStatus === "expired") return account.loginStatus;
    return "unknown";
  };
  // The status column and both account actions read this one state: 已登录,
  // 登录中 while a sign-in this pane started is in flight, and 未登录 for every
  // other case (signed out, expired, not probed yet).  Only the session lane
  // says 登录中: a silent mount probe is a station round trip (the resources
  // lane), not a sign-in, and it must not repaint the row it is about to agree
  // with every time the pane opens.
  const relayLoginState = (account: RelayAccount): "signed_in" | "signing_in" | "signed_out" => {
    if (loading[account.id]?.session) return "signing_in";
    return effectiveLoginStatus(account) === "signed_in" ? "signed_in" : "signed_out";
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
    if (selectedID === "") return;
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
  const refreshAccountResources = async (target: ResourceRefreshTarget, options: { silent?: boolean; force?: boolean } = {}): Promise<"ready" | "unavailable"> => {
    const { silent = false, force = false } = options;
    if (isAccountLoading(target.id)) {
      return accountsRef.current.find((item) => item.id === target.id)?.resourceStatus === "ready" ? "ready" : "unavailable";
    }
    updateLoading(target.id, "resources", true);
    if (!silent) onStatus?.(undefined);
    try {
      const status = await refreshResources(target.id, { force });
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
    // A silent mount probe is stated by nothing: it is a station round trip
    // (the resources lane, which it shares with the key-list read it precedes),
    // never a sign-in.  The login column keeps showing what the pane already
    // knows while it runs, so re-entering the pane does not flicker a verified
    // account through 登录中.
    updateLoading(account.id, "resources", true);
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
      updateLoading(account.id, "resources", false);
    }
  };
  const loginAccount = async (account: AddedRelayAccount): Promise<boolean> => {
    setFormBusy(true);
    updateLoading(account.id, "session", true);
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
      // The station round trip after a sign-in is paid for once: the key list
      // this pane shows must be the one the fresh session can read, so it
      // forces a read instead of reusing a pre-login result.
      await refreshAccountResources({ id: account.id }, { silent: true, force: true });
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
  // fresh login state and resources without a manual refresh click.  A restore
  // that fails never opens the login page on its own: the account reads 未登录
  // and keeps its cached keys until the user presses 去登录 (or +).
  //
  // The mount is not a reason to re-authenticate: Core answers with the
  // observation it already has while that observation is inside the station's
  // re-check window (运行时 adjusts it), so stepping into the pane and back
  // within the window costs no station round trip and never repaints the login
  // state.  Only a session the window no longer covers is probed again.
  const attemptedAccounts = useRef(new Set<string>());
  useEffect(() => {
    if (busy) return;
    for (const account of stationAccounts) {
      if (attemptedAccounts.current.has(account.id) || isAccountLoading(account.id)) continue;
      attemptedAccounts.current.add(account.id);
      const observed = account.loginObservedSecondsAgo;
      const fresh = account.loginStatus === "signed_in"
        && observed !== null
        && typeof sessionRecheckSeconds === "number"
        && sessionRecheckSeconds > 0
        && observed < sessionRecheckSeconds;
      void (async () => {
        // Opening the station is the probe: restore the login, then refresh the
        // key list.  The refresh runs even when the session check failed — a
        // locally known key is local data, and Core still reads the station
        // through an explicitly remembered session when it has one.  A session
        // the window already covers skips the probe entirely, and the pane goes
        // straight to the key list it actually came to show.
        if (!fresh) {
          await restoreSavedSession(account);
        }
        await refreshAccountResources(account, { silent: true });
      })();
    }
  }, [busy, sessionRecheckSeconds, stationAccounts]);
  // The add flow never reserves an account slot. The webview login runs
  // first (its own modal window over the workspace); Core creates the account
  // shell only when sign-in actually succeeds (pending_account), so a
  // cancelled login leaves nothing behind. Whether the password is kept is
  // decided by the post-login prompt inside the native flow. The relay
  // family comes from the station's type, or auto-detection — never a select.
  const startPendingLogin = async (): Promise<void> => {
    setFormBusy(true);
    const pendingID = `login-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const stationType = station.type ?? await detectType?.(station.origin);
    if (!stationType) {
      setFormBusy(false);
      publish(translate("relay.typeNotDetected"));
      return;
    }
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
  // 去登录 opens the login page at once: the silent session/password check
  // already ran when the account appeared (and its network timeout is exactly
  // the wait the user must not sit through), while the native login window
  // restores the remembered browser session itself and closes again when that
  // session still works.
  // 刷新资源 is the control every relay message already names: a session that
  // cannot read the station is reported with 「请点击刷新资源」, so the row that
  // states it carries the button that performs it.  It is a forced station
  // round trip, so it reports its own progress and its own result.
  const refreshSelected = async (): Promise<void> => {
    if (!selected) return;
    if (pendingAction === "refresh") return;
    const account = selected;
    await runPendingAction("refresh", async () => {
      setFormBusy(true);
      try {
        const status = await refreshAccountResources({ id: account.id }, { force: true });
        publish(translate(status === "ready" ? "relay.loginComplete" : "relay.resourcesUnavailable"));
      } finally {
        setFormBusy(false);
      }
    });
  };
  const loginSelected = async (): Promise<void> => {
    if (!selected) return;
    if (effectiveLoginStatus(selected) === "signed_in") return;
    if (pendingAction === "login") return;
    const account = selected;
    await runPendingAction("login", () => loginAccount(account));
  };
  const removeSelected = async (): Promise<void> => {
    if (!removal) return;
    if (pendingAction === "remove") return;
    await runPendingAction("remove", async () => {
      setFormBusy(true);
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
    });
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
  // 分组管理 is a native subordinate window of the workspace: the
  // pre-refactor master-detail editor with the key list (＋ / －), the selected
  // key's detail, and Close / Apply at the bottom.  Apply returns the staged
  // edits; Close discards them.  Core rejects manual key writes while
  // automatic grouping owns the layout, so the switch is staged around them.
  //
  // 自动分组 owns the key layout, so the list the window shows comes from the
  // aligned draft: let Core stage its one-key-per-group layout over the groups
  // it already holds, then read the account back so the list never shows keys
  // the switch is already replacing.  The layout is built from held facts — a
  // station round trip can only add groups Core has not seen — so the window
  // reads the station only when there is no usable key list to align yet.
  // Otherwise opening 分组管理 right after the account pane's own read would
  // make the user wait for a second station round trip it does not need, which
  // is exactly the wait this window's wheel used to state.
  const alignAutoGroupingAction = apiKeyActions?.alignAutoGrouping;
  const groupManagerNeedsAlignment = (current: RelayAccount): boolean =>
    Boolean(alignAutoGroupingAction) && current.autoGrouping && current.groups.length > 0;
  // The held facts a layout still needs: a ready account read with at least one
  // key.  Without one there is nothing to align, so the window reads first.
  const groupManagerHasUsableFacts = (current: RelayAccount): boolean =>
    current.resourceStatus === "ready" && current.resources.length > 0;
  // The account facts the window's rows are built from: the aligned draft while
  // 自动分组 owns the layout, otherwise the account as this pane already holds
  // it.  A station that cannot be refreshed keeps the keys it last reported.
  // The station is read only when the account has no usable key list, and that
  // read reuses one the pane already ran when it is seconds old, so the usual
  // opening never pays for a station round trip twice.
  const loadGroupManagerAccount = async (current: RelayAccount): Promise<RelayAccount> => {
    if (!alignAutoGroupingAction || !groupManagerNeedsAlignment(current)) return current;
    try {
      if (!groupManagerHasUsableFacts(current) && await refreshResources(current.id) !== "ready") return current;
      await alignAutoGroupingAction(current.id);
      const snapshot = await refreshAccounts();
      return (snapshot ? accountsFromSnapshot(snapshot) : []).find((entry) => entry.id === current.id) ?? current;
    } catch {
      // A station that cannot be refreshed keeps the keys it last reported.
      return current;
    }
  };
  // The window's content for one account: the rows, the groups they can be
  // assigned to, and the caption the window shows above them.  The same content
  // serves the opening request and the later update, so a load can never change
  // the shape of what the window holds.
  const groupManagerSnapshot = (current: RelayAccount) => {
    // The picker keeps the rate so a group is identifiable while it is chosen;
    // the list column shows the name alone because 倍率 has its own column.
    const groups = current.groups
      .filter((group) => group.id !== "")
      .map((group) => ({ id: group.id, label: groupLabel(group, translate), name: group.name || translate("relay.apiKeyUngrouped"), rate: groupRateLabel(group) }));
    // Nothing is left without a group while 自动分组 owns the layout, so the
    // ungrouped item only exists while the switch is off.
    if (current.type === "newapi" && !current.autoGrouping) {
      groups.unshift({ id: "", label: translate("relay.apiKeyUngrouped"), name: translate("relay.apiKeyUngrouped"), rate: "" });
    }
    // A key can point at a group the station no longer offers; keep it
    // selectable so the picker never shows a different group than the row.
    for (const resource of current.resources) {
      if (resource.groupID && !groups.some((group) => group.id === resource.groupID)) {
        const name = resourceGroupName(resource, current.groups, translate);
        groups.push({ id: resource.groupID, label: name, name, rate: "" });
      }
    }
    // While 自动分组 is on, the list is the layout that switch owns: one key
    // per group, so a dropped group or a key Core already replaced is never
    // listed as an ungrouped row.
    const keyResources = current.autoGrouping && current.groups.length > 0
      ? current.resources.filter((resource) => !resource.pendingDelete && resourceGroup(resource, current.groups) !== undefined)
      : current.resources;
    return {
      accountLabel: accountDisplayName(current, translate),
      groups,
      keys: keyResources.map((resource) => ({
        id: resource.id,
        name: resource.apiName || resource.name,
        groupID: resource.groupID,
        groupLabel: resourceGroupName(resource, current.groups, translate),
        multiplier: groupRateLabel(resourceGroup(resource, current.groups)),
        // Core reports only whether a credential exists; the window reads the
        // real value through the native capability and shows it in place.
        hint: resource.keyHint,
        models: resourceModelList(resource),
        enabled: resource.enabled,
      })),
      autoGrouping: current.autoGrouping,
    };
  };
  // Every label the window's native controls read, resolved once per request.
  const groupManagerLabels = () => ({
    listLabel: translate("providers.keys"),
    addLabel: translate("common.add"),
    removeLabel: translate("common.delete"),
    nameLabel: translate("providers.keyName"),
    groupLabel: translate("relay.apiKeyGroup"),
    multiplierLabel: translate("relay.apiKeyMultiplier"),
    valueLabel: translate("providers.keyValue"),
    // The copy is an icon button beside the value; these words ride it as its
    // tooltip and its accessibility label.
    copyLabel: translate("relay.apiKeyCopy"),
    failedLabel: translate("relay.operationFailed"),
    modelsLabel: translate("relay.apiKeyModelList"),
    emptyLabel: translate("common.none"),
    enabledLabel: translate("common.enable"),
    newKeyName: translate("relay.apiKeyNewName"),
    autoGroupingLabel: translate("relay.apiKeyAutoGrouping"),
    autoGroupingHelp: translate("relay.apiKeyAutoGroupingHelp"),
    ungroupedLabel: translate("relay.apiKeyUngrouped"),
    closeLabel: translate("status.close"),
    applyLabel: translate("status.saveAndClose"),
    discardTitle: translate("relay.groupManagerDiscardTitle"),
    discardBody: translate("relay.groupManagerDiscardBody"),
    discardConfirm: translate("common.discard"),
    // The window's key list header carries the load: the wheel beside 密钥 turns
    // while the station round trip is in flight, and these words ride it as its
    // tooltip, so the wait is stated on the rows it is about to replace.
    loadingLabel: translate("relay.groupManagerLoading"),
  });
  // The window's opening request: the content above plus the chrome only a
  // request carries — the title, the account that owns the keys, and every
  // label the native controls read.
  const groupManagerRequest = (current: RelayAccount) => ({
    title: translate("relay.groupManager"),
    accountId: current.id,
    labels: groupManagerLabels(),
    ...groupManagerSnapshot(current),
  });
  // 分组管理 owns its work: 保存并关闭 hands this pane its staged edits, which
  // are written and applied while the sheet is still on screen, and the sheet
  // states the outcome in its own status bar.  The gate keeps the pane's own
  // immediate apply away while the sheet is up and the pane's strip stays
  // silent about work it did not start, so one surface reports one save.
  const openGroupManager = async (): Promise<void> => {
    const account = selected;
    if (!account || !native.showGroupManager) return;
    // The window appears first and loads second: 自动分组's aligned draft costs a
    // station round trip, and the window must never stay closed for it.  The
    // window opens on the account facts this pane already holds, keeps its rows
    // read-only while the load is in flight, and takes the aligned draft
    // through the native update when it lands.  A host without that update
    // opens the window on the loaded facts instead, as it always has — the
    // window there can only ever show data that is already loaded.
    const update = native.updateGroupManager;
    const pending = update && groupManagerNeedsAlignment(account) ? loadGroupManagerAccount(account) : undefined;
    let current = account;
    if (!update) current = await loadGroupManagerAccount(account);
    if (pending) {
      void pending.then((aligned) => {
        const push = native.updateGroupManager;
        if (!push) return;
        void push(groupManagerSnapshot(aligned)).catch(() => {
          // A payload the host refuses would leave the window on its loading
          // line forever, so the facts it opened on clear it instead.
          void push(groupManagerSnapshot(account)).catch(() => undefined);
        });
      });
    }
    // What the sheet has already handed over.  A save that failed leaves these
    // edits staged in Core, so the next 保存并关闭 stages only what it added on
    // top instead of writing the same keys twice.  The record is kept as each
    // edit lands — a batch that throws half-way still knows what it staged —
    // and each update records only the fields that actually reached Core, so a
    // retry writes exactly the remainder.
    let handedOver: RelayGroupManagerResult | undefined;
    const stageEdits = async (edits: RelayGroupManagerResult): Promise<void> => {
      // The staged edits are reconciled against the loaded account, which is
      // the one the window's rows came from.
      if (pending) current = await pending;
      const previous = handedOver;
      const alreadyCreated = new Set((previous?.creates ?? []).map((create) => `${create.name}\u0000${create.groupID}`));
      const alreadyDeleted = new Set(previous?.deletes ?? []);
      const staged: RelayGroupManagerResult = {
        autoGrouping: previous?.autoGrouping ?? current.autoGrouping,
        creates: [...(previous?.creates ?? [])],
        updates: (previous?.updates ?? []).map((entry) => ({ ...entry })),
        deletes: [...(previous?.deletes ?? [])],
      };
      handedOver = staged;
      // Manual key writes are rejected while auto-grouping owns the layout, so
      // turning it off is staged first and turning it on is staged last.
      const turningOff = current.autoGrouping && !edits.autoGrouping && staged.autoGrouping;
      if (turningOff) {
        await apiKeyActions?.setAutoGrouping?.(account.id, false);
        staged.autoGrouping = false;
      }
      for (const create of edits.creates) {
        if (alreadyCreated.has(`${create.name}\u0000${create.groupID}`)) continue;
        await apiKeyActions?.create?.(account.id, { name: create.name, groupID: create.groupID, enabled: true });
        alreadyCreated.add(`${create.name}\u0000${create.groupID}`);
        staged.creates.push(create);
      }
      for (const edit of edits.updates) {
        const resource = current.resources.find((item) => item.id === edit.keyID);
        if (!resource) continue;
        let stagedEdit = staged.updates.find((entry) => entry.keyID === edit.keyID);
        if (!stagedEdit) {
          stagedEdit = { keyID: edit.keyID, name: resource.apiName || resource.name, groupID: resource.groupID, enabled: resource.enabled };
          staged.updates.push(stagedEdit);
        }
        if (edit.name !== (resource.apiName || resource.name) && stagedEdit.name !== edit.name) {
          await apiKeyActions?.update?.(account.id, edit.keyID, edit.name);
          stagedEdit.name = edit.name;
        }
        if (edit.groupID !== resource.groupID && stagedEdit.groupID !== edit.groupID) {
          await apiKeyActions?.setGroup?.(account.id, edit.keyID, edit.groupID);
          stagedEdit.groupID = edit.groupID;
        }
        if (edit.enabled !== resource.enabled && stagedEdit.enabled !== edit.enabled) {
          await apiKeyActions?.setEnabled?.(account.id, edit.keyID, edit.enabled);
          stagedEdit.enabled = edit.enabled;
        }
      }
      for (const keyID of edits.deletes) {
        if (alreadyDeleted.has(keyID)) continue;
        await apiKeyActions?.remove?.(account.id, keyID, "detach_disabled");
        alreadyDeleted.add(keyID);
        staged.deletes.push(keyID);
      }
      const turningOn = !current.autoGrouping && edits.autoGrouping && !staged.autoGrouping;
      if (turningOn) {
        await apiKeyActions?.setAutoGrouping?.(account.id, true);
        staged.autoGrouping = true;
      }
    };
    const answersApplies = Boolean(native.awaitGroupManagerApply && native.finishGroupManagerApply);
    const answerOneApplyRequest = async (): Promise<void> => {
      const ask = native.awaitGroupManagerApply?.();
      if (!ask) return;
      const edits = await ask;
      if (!edits) return;
      setFormBusy(true);
        let status = translate("common.saved");
      let close = true;
      try {
        await stageEdits(edits);
        // The write is applied by the sheet's own request, so the pane it was
        // opened from states nothing about it.
        await applyStagedQuietly();
        await refreshAccounts();
      } catch {
        // The sheet keeps its rows and its 保存并关闭, states the failure in
        // its own strip, and can be tried again once the cause is fixed.
        status = translate("common.notApplied");
        close = false;
      } finally {
        setFormBusy(false);
        await native.finishGroupManagerApply?.({ status, close }).catch(() => undefined);
        if (!close) void answerOneApplyRequest();
      }
    };
    setGroupManagerOpen(true);
    try {
      const shown = native.showGroupManager({ ...groupManagerRequest(current), loading: Boolean(pending) });
      if (answersApplies) void answerOneApplyRequest();
      const result = await shown;
      // A host whose sheet can only save on close stages the same edits after
      // it is gone; the sheet itself states nothing there, so this pane keeps
      // the outcome for that host only.
      if (result && !answersApplies) {
        setFormBusy(true);
        try {
          await stageEdits(result);
          await applyStagedQuietly();
          await refreshAccounts();
          onStatus?.(translate("common.saved"));
        } catch {
          onStatus?.(translate("common.notApplied"));
        } finally {
          setFormBusy(false);
        }
      }
    } finally {
      setGroupManagerOpen(false);
    }
  };
  const selectedLoginState = selected ? relayLoginState(selected) : "signed_out";
  const accountRows = stationAccounts.map((account) => ({
    key: account.id,
    cells: [
      accountDisplayName(account, translate),
      translate(`relay.status.${relayLoginState(account)}`),
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
    if (pendingAction === "cleanup") return;
    await runPendingAction("cleanup", async () => {
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
    });
  };
  const stationCleanups = (cleanups ?? []).filter((cleanup) => station.accountIDs.includes(cleanup.accountID));
  return <View style={styles.accountsPanel}>
    {stationCleanups.map((cleanup) => <View key={`cleanup:${cleanup.accountID}`} style={styles.cleanupRow}>
      <Text numberOfLines={2} style={styles.cleanupText}>{translate("relay.credentialsCleanupPending", { label: cleanup.label })}</Text>
      <NativeButton title={translate("relay.retryCleanup")} compact busy={pendingAction === "cleanup"} disabled={controlsBusy && pendingAction !== "cleanup"} onPress={() => { void retryCleanup(cleanup); }} style={styles.panelActionButton} />
    </View>)}
    <View style={styles.panelHeader}>
      <Text style={styles.panelTitle}>{translate("providers.accounts")}</Text>
      <View style={styles.panelActions}>
        {stationAccounts.length < 8 ? <NativeButton title="" symbol="plus" compact toolTip={translate("relay.addAccount")} accessibilityLabel={translate("relay.addAccount")} busy={pendingAction === "add"} disabled={controlsBusy && pendingAction !== "add"} onPress={() => { void beginAddLogin(); }} style={styles.panelActionButton} /> : null}
        {selected ? <NativeButton title="" symbol="minus" compact destructive toolTip={translate("relay.removeLocal")} accessibilityLabel={translate("relay.removeLocal")} disabled={controlsBusy} onPress={() => { setRemovalPolicy("detach"); setRemoval({ account: selected }); }} style={styles.panelActionButton} /> : null}
      </View>
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
        {selectedLoginState === "signed_out" ? <NativeButton title={translate("relay.goLogin")} compact busy={pendingAction === "login"} disabled={controlsBusy && pendingAction !== "login"} onPress={() => { void loginSelected(); }} /> : null}
      </View>
      <View style={styles.accountActionsRow}>
        <NativeButton title={translate("relay.refreshResources")} symbol="refresh" compact busy={pendingAction === "refresh"} disabled={controlsBusy && pendingAction !== "refresh"} onPress={() => { void refreshSelected(); }} />
        <NativeButton title={translate("relay.groupManager")} compact disabled={controlsBusy || !native.showGroupManager} onPress={() => { void openGroupManager(); }} />
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
      disabled={controlsBusy && pendingAction !== "remove"}
      busy={pendingAction === "remove"}
      confirmLabel={translate("relay.removeLocal")}
      destructive
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
    label: `${accountKeyPrefix(account, translate)}/${resourceGroupName(resource, account.groups, translate)}`,
    accountLabel: accountDisplayName(account, translate),
    group: resourceGroupName(resource, account.groups, translate),
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
  /** ``force`` skips the reuse window: a sign-in that just landed reads again. */
  refreshResources: (accountID: string, options?: { force?: boolean }) => Promise<"ready" | "unavailable">;
  /** Returns the refreshed snapshot so callers avoid stale projections. */
  refreshAccounts: () => Promise<CoreSnapshot | void>;
  /**
   * Commit the staged relay draft without a word in this window's status bar:
   * 分组管理 writes its own edits and applies them while its sheet is still up,
   * so the sheet states the outcome and the pane it was opened from stays
   * silent about work it did not start.
   */
  applyStagedQuietly: () => Promise<void>;
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
  /** The required marker beside a form label. */
  required: Platform.OS === "macos" ? PlatformColor("systemRedColor") : PlatformColor("SystemFillColorCriticalBrush"),
};

const compactStyles = StyleSheet.create({
  formRow: { minHeight: 26, gap: 3 },
  formValue: { minHeight: 24, gap: 3 },
});

const styles = StyleSheet.create({
  accountsPanel: { minWidth: 0, gap: 6, paddingTop: 6, borderTopWidth: 1, borderTopColor: colors.separator },
  cleanupRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" },
  cleanupText: { flex: 1, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  panelHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  panelTitle: { flex: 1, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  panelActions: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 },
  panelActionButton: { width: 22, minWidth: 22, height: 22 },
  accountsTable: { flex: 0, height: 84, minHeight: 84, flexShrink: 0 },
  accountsEmpty: { minHeight: 40, alignItems: "center", justifyContent: "center", paddingHorizontal: 10, paddingVertical: 8, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel },
  accountsEmptyText: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, textAlign: "center" },
  accountEditor: { minWidth: 0, gap: 5 },
  accountNameValue: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE },
  accountActionsRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" },
  fieldRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  fieldLabel: { width: 64, flexShrink: 0, color: colors.text, fontSize: UI_FONT_SIZE },
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
  formLabelRequired: { color: colors.required },
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
