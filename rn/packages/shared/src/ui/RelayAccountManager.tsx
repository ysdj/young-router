import React, { useEffect, useMemo, useRef, useState } from "react";
import { Platform, PlatformColor, ScrollView, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";
import type { CoreSnapshot, NativeLeafAdapter } from "../types";
import { NativeButton, NativeCheckbox, NativePersistentScrollIndicator, NativePicker, NativeSegmentedControl, NativeSecureTextInput, NativeTable, NativeTextField } from "./NativeControls";
import { normalizeRelayOrigin, suggestedRelayStationName } from "./relayOrigin";
import { UI_FONT_SIZE } from "./typography";

export { normalizeRelayOrigin } from "./relayOrigin";

type UnknownRecord = Record<string, unknown>;
type Translate = (key: string, values?: Record<string, string | number>) => string;
type RelayType = "newapi" | "sub2api";
type LocalDependencyPolicy = "delete_models" | "detach";
type RemoteDeletePolicy = "delete_models" | "detach_disabled" | "detach_only";
type RelayAccount = {
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

type RelayGroup = {
  id: string;
  name: string;
  multiplier: number | null;
};

/** A station is the durable grouping identity for relay accounts. */
type RelayStation = {
  id: string;
  name: string;
  origin: string;
  type?: RelayType;
  persisted: boolean;
  accountIDs: string[];
  linkedModelCount: number;
  pendingOperationCount: number;
};

type RelayResource = {
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

type LocalRemovalIntent =
  | { kind: "station"; station: RelayStation }
  | { kind: "account"; account: RelayAccount };

type RemoteKeyDeleteIntent = { account: RelayAccount; resource: RelayResource };

type AddedRelayAccount = Pick<RelayAccount, "id" | "type" | "label" | "origin" | "username" | "rememberPassword">;
type AutoGroupingUpdateResult = { draftStaged: boolean };
export type AddAccountOptions = {
  stationID?: string;
  stationOrigin?: string;
  stationName?: string;
  stationType?: RelayType;
};

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

type PendingCredentialCleanup = {
  accountID: string;
  label: string;
  kind: "credentials";
};

type RelayTypeDetection = "checking" | RelayType | "unknown";
type SavedSessionRestore = "signed_in" | "expired" | "unavailable";
type AccountLoadingState = { session: boolean; resources: boolean };
type ResourceRefreshTarget = { id: string; resources?: RelayResource[] };
type AddStep = "origin" | "sign-in";
type StationDraft = Partial<Pick<RelayStation, "name" | "origin" | "type">>;
const INLINE_MODEL_LIMIT = 5;
const COLUMN_GAP = 8;

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function groupMultiplier(value: unknown): number | null {
  const multiplier = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(multiplier) && multiplier >= 0 ? multiplier : null;
}

function groupMultiplierLabel(multiplier: number | null, translate: Translate): string {
  if (multiplier === null) return translate("common.notAvailable");
  return `${Number.isInteger(multiplier) ? multiplier : multiplier.toString()}x`;
}

function groupLabel(group: RelayGroup, translate: Translate): string {
  return `${group.name} / ${groupMultiplierLabel(group.multiplier, translate)}`;
}

function resourceGroup(resource: RelayResource, groups: RelayGroup[]): RelayGroup | undefined {
  return resource.groupID ? groups.find((candidate) => candidate.id === resource.groupID) : undefined;
}

function resourceGroupName(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  if (!resource.groupID) return translate("relay.apiKeyUngrouped");
  return resourceGroup(resource, groups)?.name || resource.groupName || resource.groupID;
}

function resourceGroupMultiplier(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  return groupMultiplierLabel(resourceGroup(resource, groups)?.multiplier ?? null, translate);
}

function resourceGroupLabel(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {
  return `${resourceGroupName(resource, groups, translate)} / ${resourceGroupMultiplier(resource, groups, translate)}`;
}

function resourceGroupUnavailable(resource: RelayResource, groups: RelayGroup[]): boolean {
  return Boolean(resource.groupID) && !resourceGroup(resource, groups);
}

function resourceAutoGroupingUnavailable(resource: RelayResource, groups: RelayGroup[]): boolean {
  return !resource.groupID || resourceGroupUnavailable(resource, groups);
}

function runtimeSettingNumber(snapshot: CoreSnapshot | undefined, key: string, fallback: number): number {
  const domain = record(snapshot?.domains.runtime);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  const fields = Array.isArray(state.fields)
    ? state.fields
    : Array.isArray(state.settings)
      ? state.settings
      : [];
  const field = fields.map(record).find((item) => text(item.key) === key);
  const value = typeof field?.value === "number" ? field.value : Number(field?.value);
  return Number.isFinite(value) && value >= 1 ? Math.round(value) : fallback;
}

function relayAutoGroupIntervalMinutes(snapshot: CoreSnapshot | undefined): number {
  return runtimeSettingNumber(snapshot, "LITELLM_MENU_RELAY_AUTO_GROUP_INTERVAL_MINUTES", 30);
}

function accountsFromSnapshot(snapshot?: CoreSnapshot): RelayAccount[] {
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

function stationsFromSnapshot(snapshot: CoreSnapshot | undefined, accounts: RelayAccount[]): RelayStation[] {
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

function credentialCleanupsFromSnapshot(snapshot?: CoreSnapshot): PendingCredentialCleanup[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  return Array.isArray(state.pending_credential_cleanups) ? state.pending_credential_cleanups.flatMap((value) => {
    const item = record(value);
    const accountID = text(item.account_id);
    const label = text(item.label);
    const kind = item.kind === "credentials" ? "credentials" : undefined;
    if (!accountID || !label || !kind) return [];
    return [{ accountID, label, kind }];
  }) : [];
}

function relayTypeLabel(type: RelayType, translate: Translate): string {
  return translate(type === "newapi" ? "relay.type.newapi" : "relay.type.sub2api");
}

function resourceHint(account: RelayAccount, translate: Translate): string {
  switch (account.resourceError) {
    case "login_expired": return translate("relay.resourcesLoginExpired");
    case "no_api_keys": return translate("relay.resourcesNoApiKeys");
    case "no_models": return translate("relay.resourcesNoModels");
    case "unavailable": return translate("relay.resourcesUnavailable");
    default: return account.loginStatus === "signed_in" ? translate("relay.resourcesNotLoaded") : translate("relay.resourcesAfterLogin");
  }
}

function stationName(account: RelayAccount): string {
  const hostname = account.origin
    .replace(/^https?:\/\//iu, "")
    .split("/", 1)[0]
    .trim()
    .replace(/:\d+$/u, "");
  return hostname || account.label;
}

function originHostLabel(value: string): string {
  const normalized = normalizeRelayOrigin(value);
  if (!normalized) return "";
  try {
    return new URL(normalized).hostname || normalized;
  } catch {
    return normalized.replace(/^https?:\/\//iu, "").split("/", 1)[0];
  }
}

function stationDisplayName(station: RelayStation, translate: Translate): string {
  const name = station.name.trim();
  const origin = station.origin.trim();
  if (!name) return originHostLabel(origin) || translate("relay.station");
  if (origin && stationOriginKey(name) === stationOriginKey(origin)) return originHostLabel(origin) || name;
  return name;
}

function stationPickerLabel(station: RelayStation, translate: Translate): string {
  const name = stationDisplayName(station, translate);
  const origin = station.origin.trim();
  if (!origin || stationOriginKey(name) === stationOriginKey(origin)) return name || origin;
  return `${name} — ${origin}`;
}

function usernameShortName(account: RelayAccount): string {
  const username = account.username.trim();
  if (!username) return "";
  return username.split("@", 1)[0].trim() || username;
}

function accountDisplayName(account: RelayAccount, translate: Translate): string {
  const username = usernameShortName(account);
  if (username) return username;
  const label = account.label.trim();
  if (label && !/^https?:\/\//iu.test(label)) return label;
  return translate("relay.unsignedAccount");
}

function accountDetailTitle(account: RelayAccount, translate: Translate): string {
  return account.username.trim() || accountDisplayName(account, translate);
}

export type RelayNavigationItem = {
  key: string;
  kind: "station" | "account";
  id: string;
  label: string;
  secondary: string;
  accountID?: string;
  stationID?: string;
};

/** Masked relay metadata used by the unified service-provider navigation.
 * Credentials and generated API keys never enter this projection. */
export function relayNavigationItems(snapshot: CoreSnapshot | undefined, translate: Translate): RelayNavigationItem[] {
  const accounts = accountsFromSnapshot(snapshot);
  const stations = stationsFromSnapshot(snapshot, accounts);
  return stations.flatMap((station) => {
    const rows: RelayNavigationItem[] = [{
      key: "relay:station:" + station.id,
      kind: "station",
      id: station.id,
      stationID: station.id,
      label: stationDisplayName(station, translate),
      secondary: translate("relay.station"),
    }];
    for (const accountID of station.accountIDs) {
      const account = accounts.find((candidate) => candidate.id === accountID);
      if (!account) continue;
      rows.push({
        key: "relay:account:" + account.id,
        kind: "account",
        id: account.id,
        accountID: account.id,
        stationID: station.id,
        label: accountDisplayName(account, translate),
        secondary: relayTypeLabel(account.type, translate),
      });
    }
    return rows;
  });
}

function accountStationLabel(account: RelayAccount): string {
  const value = account.stationName.trim() || stationName(account);
  return /^https?:\/\//iu.test(value) ? originHostLabel(value) : value;
}

function balanceLabel(account: RelayAccount, translate: Translate): string {
  return account.balance === null ? translate("common.none") : `$${account.balance.toFixed(2)}`;
}

function visibleResourceModels(resource: RelayResource, showAll: boolean): string[] {
  if (showAll || resource.models.length <= INLINE_MODEL_LIMIT) return resource.models;
  return resource.models.slice(0, INLINE_MODEL_LIMIT);
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

function SetupProgress({ step, translate }: { step: AddStep; translate: Translate }): React.JSX.Element {
  return <NativeWizardProgress activeIndex={step === "origin" ? 0 : 1} steps={[translate("relay.setupStepStation"), translate("relay.stepSignIn")]} />;
}

function ResourceInspector({ account, resource, signedIn, disabled, autoGrouping, groupUnavailable, nameValue, resourceGroups, selectedResourceGroupLabel, onNameChange, onNameCommit, onGroupChange, onEnabledChange, onCopy, translate }: {
  account: RelayAccount;
  resource: RelayResource;
  signedIn: boolean;
  disabled: boolean;
  autoGrouping: boolean;
  groupUnavailable: boolean;
  nameValue: string;
  resourceGroups: RelayGroup[];
  selectedResourceGroupLabel: string;
  onNameChange: (value: string) => void;
  onNameCommit: () => Promise<void>;
  onGroupChange: (groupID: string) => void;
  onEnabledChange: (enabled: boolean) => void;
  onCopy: () => void;
  translate: Translate;
}): React.JSX.Element {
  const [showAllModels, setShowAllModels] = useState(false);
  const groupLabels = resourceGroups.map((group) => groupLabel(group, translate));
  const canRevealKey = signedIn;
  const secretTarget = `${account.id}:${resource.id}`;
  const models = visibleResourceModels(resource, showAllModels);
  const modelListIsCollapsible = resource.models.length > INLINE_MODEL_LIMIT;
  useEffect(() => setShowAllModels(false), [resource.id]);
  return <ScrollView style={styles.resourceInspectorScroll} contentContainerStyle={styles.resourceInspectorContent} showsVerticalScrollIndicator={showAllModels} showsHorizontalScrollIndicator={false}>
    {showAllModels ? <NativePersistentScrollIndicator style={styles.resourceInspectorScrollIndicator} /> : null}
    <View style={styles.resourceInspectorHeaderBlock}>
      <View style={styles.resourceInspectorHeader}>
        <View style={styles.resourceInspectorHeading}>
          <Text numberOfLines={1} style={styles.resourceInspectorTitle}>{nameValue}</Text>
          <Text numberOfLines={1} style={styles.resourceInspectorSubtitle}>{selectedResourceGroupLabel}</Text>
        </View>
      </View>
      <View style={styles.resourceInspectorDivider} />
    </View>
    <View style={styles.resourceInspectorForm}>
      <View style={styles.resourceInspectorToggleRow}>
        <NativeCheckbox label={resource.enabled ? translate("common.enable") : translate("common.disable")} value={resource.enabled} disabled={disabled || autoGrouping || groupUnavailable} onValueChange={onEnabledChange} />
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("common.name")}</Text>
        <View style={styles.resourceInspectorControlRow}>
          <NativeTextField value={nameValue} placeholder={translate("relay.apiKeyNamePlaceholder")} editable={!disabled && !autoGrouping} accessibilityLabel={`${translate("relay.apiKeyName")}: ${nameValue}`} onChangeText={onNameChange} onBlur={() => { if (!disabled && !autoGrouping) void onNameCommit(); }} style={styles.resourceInspectorTextInput} />
        </View>
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("relay.apiKeyGroup")}</Text>
        {groupLabels.length > 0 ? <NativePicker labels={groupLabels} selectedValue={selectedResourceGroupLabel} disabled={disabled || autoGrouping} onChange={({ nativeEvent }) => { const group = resourceGroups[nativeEvent.index]; if (group) onGroupChange(group.id); }} style={styles.resourceInspectorPicker} /> : <Text style={styles.resourceInspectorReadOnly}>{translate("common.none")}</Text>}
      </View>
      <View style={[styles.resourceInspectorRow, styles.resourceInspectorMultilineRow]}>
        <Text style={styles.resourceInspectorLabel}>{translate("providers.models")}</Text>
        <View style={styles.resourceInspectorModelValue}>
          <Text selectable style={styles.resourceInspectorModels}>{models.length > 0 ? models.join("\n") : translate("common.none")}</Text>
          {modelListIsCollapsible ? <NativeButton
            title={translate(showAllModels ? "relay.showFewerModels" : "relay.showAllModels", { count: resource.models.length })}
            compact
            link
            disabled={disabled}
            onPress={() => setShowAllModels((current) => !current)}
            style={styles.resourceInspectorModelsToggle}
          /> : null}
        </View>
      </View>
      <View style={[styles.resourceInspectorRow, styles.resourceInspectorKeyRow]}>
        <Text style={styles.resourceInspectorLabel}>{translate("relay.apiKeyValue")}</Text>
        <View style={styles.resourceInspectorControlRow}>
          {canRevealKey
            ? <NativeSecureTextInput domain="relay_accounts" field="api_key" target={secretTarget} label={`${translate("relay.apiKeyValue")}: ${nameValue}`} placeholder={resource.keyHint ? "" : translate("common.none")} plainText autoCommit disabled style={styles.resourceInspectorSecureInput} />
            : <NativeTextField value="" placeholder={resource.keyHint ? "" : translate("common.none")} editable={false} accessibilityLabel={`${translate("relay.apiKeyValue")}: ${nameValue}`} style={styles.resourceInspectorSecureInput} />}
          <NativeButton title={translate("relay.apiKeyCopy")} symbol="copy" compact disabled={disabled || !resource.keyHint} toolTip={translate("relay.apiKeyCopy")} accessibilityLabel={`${translate("relay.apiKeyCopy")}: ${nameValue}`} onPress={onCopy} style={styles.resourceInspectorAction} />
        </View>
      </View>
    </View>
  </ScrollView>;
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

function ApiKeyCreateDialog({ visible, groups, disabled, onClose, onCreate, translate }: {
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

type PolicyOption<T extends string> = { value: T; label: string; hint: string };

function DependencyPolicyDialog<T extends string>({ visible, title, message, options, value, disabled, confirmLabel, onValueChange, onClose, onConfirm, translate }: {
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

function RelayTablePane({ title, actions, style, children }: { title: string; actions: React.ReactNode; style?: StyleProp<ViewStyle>; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.tablePane, style]}>
    <View style={styles.tableTitleRow}>
      <Text style={styles.tableTitle}>{title}</Text>
      <View style={styles.tableActions}>{actions}</View>
    </View>
    {children}
  </View>;
}

export function RelayAccountManager({
  visible,
  setupOnly = false,
  hideNavigation = false,
  selectedNavigationKey,
  onNavigationSelectionChange,
  removeRequest,
  snapshot,
  native,
  busy,
  translate,
  onClose,
  onStatus,
  commit,
  detectType,
  refreshResources,
  apiKeyActions,
  addAccount,
  refreshAccounts,
}: {
  /** RN macOS renders this component in the ordinary route tree. */
  visible?: boolean;
  setupOnly?: boolean;
  /** Render only the detail pane when a parent owns the unified navigation. */
  hideNavigation?: boolean;
  selectedNavigationKey?: string;
  onNavigationSelectionChange?: (key: string) => void;
  removeRequest?: number;
  snapshot?: CoreSnapshot;
  native: NativeLeafAdapter;
  busy: boolean;
  translate: Translate;
  onClose?: () => void;
  onStatus?: (status?: string) => void;
  commit: (type: string, payload?: UnknownRecord, domain?: "relay_accounts") => Promise<void>;
  detectType: (origin: string) => Promise<RelayType | undefined>;
  refreshResources: (accountId: string) => Promise<"ready" | "unavailable">;
  apiKeyActions?: RelayApiKeyActions;
  addAccount: (type: RelayType, origin: string, rememberPassword: boolean, options?: AddAccountOptions) => Promise<AddedRelayAccount | undefined>;
  refreshAccounts: () => Promise<void>;
}): React.JSX.Element {
  const accounts = useMemo(() => accountsFromSnapshot(snapshot), [snapshot]);
  const stations = useMemo(() => stationsFromSnapshot(snapshot, accounts), [snapshot, accounts]);
  const pendingCredentialCleanups = useMemo(() => credentialCleanupsFromSnapshot(snapshot), [snapshot]);
  const snapshotAutoGroupIntervalMinutes = relayAutoGroupIntervalMinutes(snapshot);
  const appliedAutoGroupIntervalMinutes = useRef(30);
  const runtimeDraftDirty = Boolean(snapshot?.drafts.runtime?.dirty);
  useEffect(() => {
    if (!runtimeDraftDirty) appliedAutoGroupIntervalMinutes.current = snapshotAutoGroupIntervalMinutes;
  }, [runtimeDraftDirty, snapshotAutoGroupIntervalMinutes]);
  const [selectedID, setSelectedID] = useState<string>();
  const [selectedStationID, setSelectedStationID] = useState<string>();
  const [adding, setAdding] = useState(setupOnly);
  const [addStep, setAddStep] = useState<AddStep>("origin");
  const [addStationID, setAddStationID] = useState("__custom__");
  const [origin, setOrigin] = useState("");
  const [addStationName, setAddStationName] = useState("");
  const [rememberPassword, setRememberPassword] = useState(false);
  const [stationNameEdited, setStationNameEdited] = useState(false);
  const [typeDetection, setTypeDetection] = useState<RelayTypeDetection>();
  const [manualType, setManualType] = useState<RelayType>();
  const [apiKeyCreateOpen, setApiKeyCreateOpen] = useState(false);
  const [selectedResourceID, setSelectedResourceID] = useState<string>();
  const [autoGroupingControlRevision, setAutoGroupingControlRevision] = useState(0);
  const [rememberPasswordDrafts, setRememberPasswordDrafts] = useState<Record<string, boolean>>({});
  const [apiKeyNameDrafts, setApiKeyNameDrafts] = useState<Record<string, string>>({});
  const apiKeyNameDraftsRef = useRef(apiKeyNameDrafts);
  apiKeyNameDraftsRef.current = apiKeyNameDrafts;
  const [formBusy, setFormBusy] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [accountLoading, setAccountLoading] = useState<Record<string, AccountLoadingState>>({});
  const accountLoadingRef = useRef<Record<string, AccountLoadingState>>({});
  const [localSignedInIDs, setLocalSignedInIDs] = useState<Set<string>>(() => new Set());
  const [feedback, setFeedback] = useState<string>();
  const [stationDrafts, setStationDrafts] = useState<Record<string, StationDraft>>({});
  const stationDraftsRef = useRef(stationDrafts);
  stationDraftsRef.current = stationDrafts;
  const [stationFormBusy, setStationFormBusy] = useState(false);
  const [localRemoval, setLocalRemoval] = useState<LocalRemovalIntent>();
  const [localDependencyPolicy, setLocalDependencyPolicy] = useState<LocalDependencyPolicy>("detach");
  const handledRemoveRequest = useRef(removeRequest ?? 0);
  const [remoteKeyDelete, setRemoteKeyDelete] = useState<RemoteKeyDeleteIntent>();
  const [remoteDeletePolicy, setRemoteDeletePolicy] = useState<RemoteDeletePolicy>("detach_disabled");
  const [loginFailureIDs, setLoginFailureIDs] = useState<Set<string>>(() => new Set());
  const rememberPasswordWriteVersion = useRef(new Map<string, number>());
  const accountsRef = useRef(accounts);
  accountsRef.current = accounts;
  const typeDetectionRequest = useRef(0);
  // A setup login can outlive the React step that started it (for example
  // when the user presses Back while the native browser is still waiting).
  // Invalidate the attempt before restoring the form so a late native result
  // cannot close the wizard or overwrite the restored step.
  const setupLoginRequest = useRef(0);
  const openedAccountIDs = useRef(new Set<string>());
  // Session/resource discovery runs in the background. It must not tint the
  // workspace or disable ordinary controls; only an actual local mutation
  // owns the disabled state.
  const controlsBusy = busy || formBusy || cleanupBusy || stationFormBusy;
  const setupControlsBusy = controlsBusy || loginBusy;
  const passwordStorageAvailable = true;
  const externalSelection = hideNavigation ? selectedNavigationKey : undefined;
  const externalIsStation = externalSelection?.startsWith("relay:station:") === true;
  const externalAccountID = externalIsStation
    ? externalSelection?.slice("relay:station:".length)
    : externalSelection?.startsWith("relay:account:")
      ? externalSelection.slice("relay:account:".length)
      : undefined;
  const effectiveSelectedID = externalSelection && !externalIsStation ? externalAccountID : selectedID;
  const effectiveSelectedStationID = externalIsStation ? externalAccountID : selectedStationID;
  const selected = effectiveSelectedStationID ? undefined : accounts.find((account) => account.id === effectiveSelectedID) ?? accounts[0];
  const selectedGroups = selected?.groups ?? [];
  const clearGlobalStatus = (): void => onStatus?.(undefined);
  const publishGlobalFeedback = (message: string): void => {
    setFeedback(undefined);
    onStatus?.(message);
  };
  const setApiKeyNameDraft = (resourceID: string, value: string): void => {
    const current = apiKeyNameDraftsRef.current;
    if (current[resourceID] === value) return;
    const updated = { ...current, [resourceID]: value };
    apiKeyNameDraftsRef.current = updated;
    setApiKeyNameDrafts(updated);
  };
  const resourceDisplayName = (resource: RelayResource): string => apiKeyNameDrafts[resource.id] !== undefined
    ? apiKeyNameDrafts[resource.id]
    : resource.apiName || resource.name;
  const visibleResources = useMemo(() => (selected?.resources ?? []).filter((resource) => (
    !selected?.autoGrouping || !resourceAutoGroupingUnavailable(resource, selectedGroups)
  )), [selected?.autoGrouping, selected?.resources, selectedGroups]);
  const resourceTableRows = useMemo(() => visibleResources.map((resource) => ({
    key: resource.id,
    cells: [
      resourceDisplayName(resource),
      resourceGroupName(resource, selectedGroups, translate),
      resourceGroupMultiplier(resource, selectedGroups, translate),
    ],
  })), [apiKeyNameDrafts, selectedGroups, translate, visibleResources]);
  const resourceSecondaryCellKeys = useMemo(() => visibleResources.flatMap((resource) => !resource.enabled || resourceGroupUnavailable(resource, selectedGroups)
    ? [`${resource.id}\x1f0`, `${resource.id}\x1f1`, `${resource.id}\x1f2`]
    : []), [selectedGroups, visibleResources]);
  const resourceUnavailableRowKeys = useMemo(() => visibleResources
    .filter((resource) => resourceGroupUnavailable(resource, selectedGroups))
    .map((resource) => resource.id), [selectedGroups, visibleResources]);
  const selectedResource = visibleResources.find((resource) => resource.id === selectedResourceID) ?? visibleResources[0];
  const resourceGroups = useMemo(() => {
    if (!selected || !selectedResource) return [];
    const groups = selected.groups.filter((group) => group.id !== "");
    if (!selected.autoGrouping && selectedResource.groupID && !groups.some((group) => group.id === selectedResource.groupID)) {
      groups.push({ id: selectedResource.groupID, name: selectedResource.groupName || selectedResource.groupID, multiplier: null });
    }
    return groups;
  }, [selected?.autoGrouping, selected?.groups, selectedResource?.groupID, selectedResource?.groupName]);
  const selectedResourceGroup = resourceGroups.find((group) => group.id === selectedResource?.groupID);
  const selectedResourceGroupLabel = selectedResourceGroup
    ? groupLabel(selectedResourceGroup, translate)
    : selectedResource ? resourceGroupLabel(selectedResource, selected?.groups ?? [], translate) : translate("relay.apiKeyUngrouped");
  const selectedResourceGroupUnavailable = Boolean(selectedResource && resourceGroupUnavailable(selectedResource, selectedGroups));
  // Runtime settings are staged until Apply. Keep the last clean interval
  // while another runtime draft is being edited so an unapplied value cannot
  // change the auto-grouping cadence.
  const autoGroupIntervalMinutes = runtimeDraftDirty
    ? appliedAutoGroupIntervalMinutes.current
    : snapshotAutoGroupIntervalMinutes;
  const selectedRememberPassword = selected ? rememberPasswordDrafts[selected.id] ?? selected.rememberPassword : false;
  const selectedStation = effectiveSelectedStationID ? stations.find((station) => station.id === effectiveSelectedStationID) : undefined;
  const projectedStation = (station: RelayStation): RelayStation => {
    const draft = stationDrafts[station.id];
    if (!draft) return station;
    return {
      ...station,
      ...(draft.name !== undefined ? { name: draft.name } : {}),
      ...(draft.origin !== undefined ? { origin: draft.origin } : {}),
      ...(draft.type !== undefined ? { type: draft.type } : {}),
    };
  };
  const selectedStationProjection = selectedStation ? projectedStation(selectedStation) : undefined;
  const selectedStationDraft = selectedStation ? stationDrafts[selectedStation.id] : undefined;
  const stationNameDraft = selectedStation
    ? selectedStationDraft?.name ?? stationDisplayName(selectedStationProjection ?? selectedStation, translate)
    : "";
  const stationOriginDraft = selectedStation
    ? selectedStationDraft?.origin ?? selectedStation.origin
    : "";
  const stationTypeDraft = selectedStationDraft?.type ?? selectedStation?.type;
  const setStationDraft = (stationID: string, draft: StationDraft): void => {
    const current = stationDraftsRef.current;
    const previous = current[stationID] ?? {};
    const next = { ...previous, ...draft };
    if (Object.keys(next).every((key) => previous[key as keyof StationDraft] === next[key as keyof StationDraft])) return;
    const updated = { ...current, [stationID]: next };
    stationDraftsRef.current = updated;
    setStationDrafts(updated);
  };
  const stationDisplay = (station: RelayStation): string => stationDisplayName(projectedStation(station), translate);
  const accountStationFor = (account: RelayAccount): RelayStation | undefined => stations.find((item) => item.id === account.stationID)
    ?? stations.find((item) => stationOriginKey(item.origin) === stationOriginKey(account.origin));
  const accountStationDisplay = (account: RelayAccount): string => {
    const station = accountStationFor(account);
    return station ? stationDisplay(station) : accountStationLabel(account);
  };
  const selectedAccountStation = selected ? accountStationFor(selected) : undefined;
  const loadingFor = (accountID: string | undefined): AccountLoadingState => accountID ? accountLoading[accountID] ?? { session: false, resources: false } : { session: false, resources: false };
  const selectedLoading = loadingFor(selected?.id);
  const selectedStatusLoading = selectedLoading.session || selectedLoading.resources;
  const stationLoading = selectedStation ? selectedStation.accountIDs.some((accountID) => {
    const loading = loadingFor(accountID);
    return loading.session || loading.resources;
  }) : false;
  const selectedAddStation = stations.find((station) => station.id === addStationID);
  const effectiveLoginStatus = (account: RelayAccount): "signed_in" | "signed_out" | "expired" | "unknown" => {
    if (loginFailureIDs.has(account.id)) return "expired";
    if (localSignedInIDs.has(account.id)) return "signed_in";
    if (account.loginStatus === "signed_in" || account.loginStatus === "signed_out" || account.loginStatus === "expired") return account.loginStatus;
    return "unknown";
  };
  const selectedHeaderSignedIn = Boolean(selected && effectiveLoginStatus(selected) === "signed_in");
  const selectedHeaderShowsBalance = Boolean(selected && selectedHeaderSignedIn && selected.balance !== null);
  const selectedHeaderValue = selectedHeaderShowsBalance && selected
    ? balanceLabel(selected, translate)
    : translate("relay.status.loading");
  const updateAccountLoading = (accountID: string, kind: keyof AccountLoadingState, loading: boolean): void => {
    const current = accountLoadingRef.current;
    const previous = current[accountID] ?? { session: false, resources: false };
    const nextState = { ...previous, [kind]: loading };
    const next = { ...current };
    if (!nextState.session && !nextState.resources) delete next[accountID];
    else next[accountID] = nextState;
    accountLoadingRef.current = next;
    setAccountLoading(next);
  };
  const isAccountLoading = (accountID: string): boolean => {
    const loading = accountLoadingRef.current[accountID];
    return Boolean(loading?.session || loading?.resources);
  };
  const markLocalSignedIn = (accountID: string, signedIn: boolean): void => {
    setLocalSignedInIDs((current) => {
      const next = new Set(current);
      if (signedIn) next.add(accountID);
      else next.delete(accountID);
      return next;
    });
  };
  const stationAccounts = (station: RelayStation): RelayAccount[] => station.accountIDs
    .map((id) => accounts.find((account) => account.id === id))
    .filter((account): account is RelayAccount => Boolean(account));
  const relayTableRows = useMemo(() => stations.flatMap((station) => {
    // AppKit makes spanning rows non-selectable group headers. A relay station
    // has its own settings and removal workflow, so it must stay selectable.
    const rows: Array<{ key: string; cells: string[] }> = [{
      key: `station:${station.id}`,
      cells: [stationDisplay(station), ""],
    }];
    for (const account of stationAccounts(station)) {
      rows.push({
        key: `account:${account.id}`,
        cells: [`  ${accountDisplayName(account, translate)}`, balanceLabel(account, translate)],
      });
    }
    return rows;
  }), [stations, accounts, stationDrafts, translate]);
  const relayTableSelection = effectiveSelectedStationID ? "station:" + effectiveSelectedStationID : selected?.id ? "account:" + selected.id : "";
  useEffect(() => {
    if (effectiveSelectedStationID && !stations.some((station) => station.id === effectiveSelectedStationID)) {
      setSelectedStationID(undefined);
      return;
    }
    setStationDrafts((current) => {
      let next = current;
      const stationByID = new Map(stations.map((station) => [station.id, station]));
      for (const [stationID, draft] of Object.entries(current)) {
        const station = stationByID.get(stationID);
        if (!station) {
          if (next === current) next = { ...current };
          delete next[stationID];
          continue;
        }
        const remaining: StationDraft = { ...draft };
        if (remaining.name !== undefined && remaining.name === stationDisplayName(station, translate)) delete remaining.name;
        if (remaining.origin !== undefined && normalizeRelayOrigin(remaining.origin) === normalizeRelayOrigin(station.origin)) delete remaining.origin;
        if (remaining.type !== undefined && remaining.type === station.type) delete remaining.type;
        if (Object.keys(remaining).length === 0) {
          if (next === current) next = { ...current };
          delete next[stationID];
        } else if (Object.keys(remaining).length !== Object.keys(draft).length) {
          if (next === current) next = { ...current };
          next[stationID] = remaining;
        }
      }
      return next;
    });
  }, [effectiveSelectedStationID, stations, selectedStationID, translate]);
  useEffect(() => {
    const route = setupOnly ? "relay-add" : "relay-accounts";
    const showingLoginStep = setupOnly && adding && addStep === "sign-in";
    const width = setupOnly ? (showingLoginStep ? 900 : 620) : 820;
    const height = setupOnly
      ? (showingLoginStep ? 620 : 460)
      : Math.min(680, Math.max(440, 170 + relayTableRows.length * 22));
    void native.window.setContentSize?.(route, width, height);
  }, [addStep, adding, native.window, relayTableRows.length, setupOnly]);
  useEffect(() => {
    if (!selected) {
      apiKeyNameDraftsRef.current = {};
      setApiKeyNameDrafts({});
      setSelectedResourceID(undefined);
      return;
    }
    setApiKeyNameDrafts((current) => {
      const resourceIDs = new Set(selected.resources.map((resource) => resource.id));
      const next = Object.fromEntries(Object.entries(current).filter(([resourceID]) => resourceIDs.has(resourceID)));
      if (Object.keys(next).length === Object.keys(current).length) return current;
      apiKeyNameDraftsRef.current = next;
      return next;
    });
    const firstSelectable = selected.resources.find((resource) => (
      selected.autoGrouping
        ? !resourceAutoGroupingUnavailable(resource, selected.groups)
        : !resourceGroupUnavailable(resource, selected.groups)
    ));
    setSelectedResourceID((current) => {
      const currentResource = selected.resources.find((resource) => resource.id === current);
      const currentSelectable = currentResource && (
        selected.autoGrouping
          ? !resourceAutoGroupingUnavailable(currentResource, selected.groups)
          : !resourceGroupUnavailable(currentResource, selected.groups)
      );
      return currentSelectable ? current : firstSelectable?.id;
    });
  }, [selected?.autoGrouping, selected?.id, selected?.resources, selected?.groups]);
  useEffect(() => {
    setRememberPasswordDrafts((current) => {
      let next = current;
      for (const [accountID, value] of Object.entries(current)) {
        if (rememberPasswordWriteVersion.current.has(accountID)) continue;
        const account = accounts.find((item) => item.id === accountID);
        if (account && account.rememberPassword !== value) continue;
        if (next === current) next = { ...current };
        delete next[accountID];
      }
      return next;
    });
  }, [accounts]);
  useEffect(() => {
    const accountIDs = new Set(accounts.map((account) => account.id));
    const expiredIDs = new Set(accounts
      .filter((account) => account.loginStatus === "expired" || account.resourceError === "login_expired")
      .map((account) => account.id));
    setLocalSignedInIDs((current) => {
      let next = current;
      for (const accountID of current) {
        if (accountIDs.has(accountID) && !expiredIDs.has(accountID)) continue;
        if (next === current) next = new Set(current);
        next.delete(accountID);
      }
      return next;
    });
  }, [accounts]);
  const resetForm = (): void => {
    typeDetectionRequest.current += 1;
    setAdding(setupOnly);
    setupLoginRequest.current += 1;
    setAddStep("origin");
    setAddStationID("__custom__");
    setOrigin("");
    setAddStationName("");
    setRememberPassword(false);
    setStationNameEdited(false);
    setTypeDetection(undefined);
    setManualType(undefined);
  };
  const beginAdding = (): void => {
    if (!setupOnly) {
      native.window.open("relay-add");
      return;
    }
    resetForm();
    setFeedback(undefined);
    setAdding(true);
  };
  const detectRelayType = async (): Promise<RelayType | undefined> => {
    const candidate = normalizeRelayOrigin(origin);
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
  const markLoginFailure = (accountID: string, failed: boolean): void => {
    setLoginFailureIDs((current) => {
      const next = new Set(current);
      if (failed) next.add(accountID);
      else next.delete(accountID);
      return next;
    });
  };
  const restoreSavedSession = async (account: RelayAccount): Promise<SavedSessionRestore> => {
    updateAccountLoading(account.id, "session", true);
    try {
      const result = await native.restoreRelaySession({
        accountId: account.id,
        type: account.type,
        label: account.label,
        origin: account.origin,
        username: account.username || undefined,
      });
      if (!result) {
        markLoginFailure(account.id, false);
        markLocalSignedIn(account.id, false);
        return "unavailable";
      }
      const signedIn = result.loginStatus === "signed_in";
      markLoginFailure(account.id, !signedIn);
      markLocalSignedIn(account.id, signedIn);
      try {
        await refreshAccounts();
      } catch {
        // The native session result is authoritative. A transient snapshot
        // failure must not turn a verified login into a false failure state.
      }
      return signedIn ? "signed_in" : "expired";
    } catch {
      markLoginFailure(account.id, false);
      markLocalSignedIn(account.id, false);
      return "unavailable";
    } finally {
      updateAccountLoading(account.id, "session", false);
    }
  };
  const beginLogin = async (): Promise<void> => {
    const candidate = normalizeRelayOrigin(origin);
    if (!candidate) return;
    const request = ++setupLoginRequest.current;
    setFormBusy(true);
    setFeedback(undefined);
    let account: AddedRelayAccount | undefined;
    try {
      const chosenStation = selectedAddStation;
      const detected = chosenStation?.type || manualType ? undefined : await detectRelayType();
      const accountType = chosenStation?.type ?? manualType ?? detected ?? detectedAddType;
      if (!accountType) {
        // A white-label site can block the public detection probes while
        // still presenting a normal sign-in page. Require an explicit family
        // choice before opening the native browser.
        setFeedback(translate("relay.typeNotDetected"));
        return;
      }
      account = await addAccount(accountType, candidate, rememberPassword, chosenStation ? {
        stationID: chosenStation.id,
        stationOrigin: chosenStation.origin,
        stationName: chosenStation.name,
        stationType: chosenStation.type ?? accountType,
      } : {
        stationOrigin: candidate,
        stationName: addStationName.trim() || undefined,
        stationType: accountType,
      });
      if (!account) throw new Error("Relay account could not be created");
      setSelectedID(account.id);
      openedAccountIDs.current.add(account.id);
      if (setupOnly) {
        setAdding(true);
        setAddStep("sign-in");
      } else {
        setAdding(false);
      }
      const loggedIn = await loginAccount(account, setupOnly, false, request);
      // Back may have restored the first step while the native login promise
      // was still pending. Treat that result as stale and clean up the staged
      // account without navigating away from the restored form.
      const cancelled = setupOnly && request !== setupLoginRequest.current;
      if (cancelled || !loggedIn) {
        await deleteAccount(account);
      } else if (setupOnly) {
        onClose?.();
        native.window.focus("relay-accounts");
      }
    } catch {
      if (account) await deleteAccount(account);
      if (!setupOnly || request === setupLoginRequest.current) setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const returnToStationStep = (): void => {
    if (!setupOnly || !adding || addStep !== "sign-in") return;
    // Update the React step before asking the native host to tear down its
    // browser. Native cancellation can synchronously resolve the login
    // promise, so doing it first lets a late callback race this state change.
    setupLoginRequest.current += 1;
    setAddStep("origin");
    setFeedback(undefined);
    native.cancelRelayLogin();
  };
  const pendingCleanups = pendingCredentialCleanups;
  const retryCredentialCleanup = async (cleanup: PendingCredentialCleanup): Promise<void> => {
    setCleanupBusy(true);
    try {
      await native.clearRelayCredentials(cleanup.accountID);
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
  const clearRemovedAccountCredentials = async (accountIDs: string[]): Promise<void> => {
    for (const accountID of accountIDs) {
      try {
        await native.clearRelayCredentials(accountID);
        await commit("credential_cleanup_confirm", { id: accountID, kind: "credentials" }, "relay_accounts");
      } catch {
        // Core retains a secret-free cleanup tombstone for every unsuccessful
        // native erase so closing the window cannot lose the retry.
      }
    }
  };
  const deleteAccount = async (account: Pick<RelayAccount, "id" | "label">, dependencyPolicy?: LocalDependencyPolicy): Promise<void> => {
    setCleanupBusy(true);
    setFeedback(undefined);
    try {
      // Persist the deletion first. A Core write failure must leave the native
      // session and password untouched so the account remains usable.
      await commit("account.delete", {
        id: account.id,
        ...(dependencyPolicy ? { dependency_policy: dependencyPolicy } : {}),
      }, "relay_accounts");
    } catch {
      setFeedback(translate("relay.operationFailed"));
      setCleanupBusy(false);
      return;
    }
    setSelectedID(undefined);
    if (hideNavigation) onNavigationSelectionChange?.("");
    await clearRemovedAccountCredentials([account.id]);
    setCleanupBusy(false);
  };
  const openLocalRemoval = (): void => {
    const intent = effectiveSelectedStationID && selectedStation
      ? { kind: "station" as const, station: selectedStation }
      : selected
        ? { kind: "account" as const, account: selected }
        : undefined;
    if (!intent) return;
    setLocalDependencyPolicy("detach");
    setLocalRemoval(intent);
  };
  useEffect(() => {
    const request = removeRequest ?? 0;
    if (request <= handledRemoveRequest.current) return;
    handledRemoveRequest.current = request;
    openLocalRemoval();
  }, [removeRequest, selected?.id, selectedStation?.id]);
  const executeLocalRemoval = async (): Promise<void> => {
    if (!localRemoval) return;
    if (localRemoval.kind === "account") {
      await deleteAccount(localRemoval.account, localDependencyPolicy);
      setLocalRemoval(undefined);
      return;
    }
    setCleanupBusy(true);
    setFeedback(undefined);
    try {
      await commit("station.remove", {
        id: localRemoval.station.id,
        dependency_policy: localDependencyPolicy,
      }, "relay_accounts");
    } catch {
      setFeedback(translate("relay.operationFailed"));
      setCleanupBusy(false);
      return;
    }
    setSelectedStationID(undefined);
    if (hideNavigation) onNavigationSelectionChange?.("");
    await clearRemovedAccountCredentials(localRemoval.station.accountIDs);
    setCleanupBusy(false);
    setLocalRemoval(undefined);
  };
  const loginAccount = async (account: AddedRelayAccount, embedded = false, silent = false, setupRequest?: number): Promise<boolean> => {
    const setupRequestActive = (): boolean => !setupOnly || setupRequest === undefined || setupRequest === setupLoginRequest.current;
    setLoginBusy(true);
    updateAccountLoading(account.id, "session", true);
    if (!silent && setupRequestActive()) setFeedback(translate("relay.loginWorking"));
    let result: Awaited<ReturnType<NativeLeafAdapter["relayLogin"]>>;
    try {
      result = await native.relayLogin({
        accountId: account.id,
        type: account.type,
        label: account.label,
        origin: account.origin,
        language: snapshot?.language ?? "system",
        username: account.username || undefined,
        rememberPassword: passwordStorageAvailable && account.rememberPassword,
        embedded,
      });
    } catch {
      markLoginFailure(account.id, true);
      markLocalSignedIn(account.id, false);
      if (!silent && setupRequestActive()) setFeedback(translate("relay.operationFailed"));
      updateAccountLoading(account.id, "session", false);
      setLoginBusy(false);
      return false;
    }
    if (!result) {
      markLoginFailure(account.id, true);
      markLocalSignedIn(account.id, false);
      if (!silent && setupRequestActive()) setFeedback(translate("relay.loginNotCompleted"));
      updateAccountLoading(account.id, "session", false);
      setLoginBusy(false);
      return false;
    }
    markLoginFailure(account.id, false);
    markLocalSignedIn(account.id, true);
    updateAccountLoading(account.id, "session", false);
    setLoginBusy(false);
    if (embedded) return true;
    let resourceStatus: "ready" | "unavailable" = "unavailable";
    try {
      // The native bridge response already proves the login. Start metadata
      // discovery immediately; its completion performs the one snapshot
      // refresh needed to publish the new account data.
      resourceStatus = await refreshAccountResources(account, silent);
    } catch {
      // Keep the verified session even when resource discovery fails.
    }
    setSelectedID(account.id);
    if (!silent) setFeedback(translate(resourceStatus === "ready" ? "relay.loginComplete" : "relay.loginResourcesUnavailable"));
    return true;
  };
  const refreshAccountResources = async (account: ResourceRefreshTarget, silent = false): Promise<"ready" | "unavailable"> => {
    if (isAccountLoading(account.id)) {
      return accountsRef.current.find((item) => item.id === account.id)?.resourceStatus === "ready" ? "ready" : "unavailable";
    }
    const hasKnownResources = (account.resources?.length ?? 0) > 0;
    updateAccountLoading(account.id, "resources", true);
    if (!silent) setFeedback(undefined);
    try {
      const status = await refreshResources(account.id);
      try {
        await refreshAccounts();
      } catch {
        // The resource refresh already completed in Core; publish its result
        // when the next snapshot subscription becomes available.
      }
      setSelectedID(account.id);
      if (!silent && status !== "ready" && !hasKnownResources) setFeedback(translate("relay.resourcesUnavailable"));
      return status;
    } catch {
      if (!silent && !hasKnownResources) setFeedback(translate("relay.resourcesUnavailable"));
      return "unavailable";
    } finally {
      updateAccountLoading(account.id, "resources", false);
    }
  };
  const refreshLoginState = async (account: RelayAccount, automatic = false): Promise<void> => {
    if (isAccountLoading(account.id)) return;
    const restored = await restoreSavedSession(account);
    if (restored === "signed_in") {
      await refreshAccountResources(account, automatic);
      return;
    }
    const canAutoLogin = account.rememberPassword && account.passwordSaved && Boolean(account.username.trim());
    if (!automatic || canAutoLogin) await loginAccount(account, false, automatic);
  };
  useEffect(() => {
    if (visible === false) {
      openedAccountIDs.current.clear();
      return;
    }
    if (adding || !selected || openedAccountIDs.current.has(selected.id)) return;
    openedAccountIDs.current.add(selected.id);
    void refreshLoginState(selected, true);
  }, [visible, adding, selected?.id]);
  const runApiKeyAction = async (kind: "create" | "update" | "setEnabled" | "setGroup", resourceID?: string, value?: boolean | string | { name: string; groupID?: string; enabled: boolean }): Promise<void> => {
    if (!selected || (kind !== "create" && !resourceID)) return;
    if (selected.autoGrouping) return;
    const targetResource = resourceID ? selected.resources.find((resource) => resource.id === resourceID) : undefined;
    if (targetResource && resourceGroupUnavailable(targetResource, selectedGroups)) return;
    if (kind === "create" && (!apiKeyActions?.create || !value || typeof value === "boolean" || typeof value === "string")) return;
    if (kind === "update" && !apiKeyActions?.update) return;
    if (kind === "setEnabled" && (!apiKeyActions?.setEnabled || typeof value !== "boolean")) return;
    if (kind === "setGroup" && (!apiKeyActions?.setGroup || typeof value !== "string")) return;
    setFormBusy(true);
    setFeedback(undefined);
    clearGlobalStatus();
    try {
      if (kind === "create") {
        await apiKeyActions?.create?.(selected.id, value as { name: string; groupID?: string; enabled: boolean });
      }
      else if (kind === "update") {
        const resource = selected.resources.find((item) => item.id === resourceID);
        const name = (apiKeyNameDraftsRef.current[resourceID as string] ?? resource?.name ?? "").trim();
        if (!name || name === resource?.name) return;
        await apiKeyActions?.update?.(selected.id, resourceID as string, name);
      }
      else if (kind === "setEnabled") await apiKeyActions?.setEnabled?.(selected.id, resourceID as string, value as boolean);
      else if (kind === "setGroup") await apiKeyActions?.setGroup?.(selected.id, resourceID as string, value as string);
      await refreshAccounts();
      // CRUD actions only stage a secret-free relay draft. Apply is the sole
      // point that sends the remote mutation, so do not refresh the relay
      // here and accidentally imply the remote action already succeeded.
      const feedbackKey = kind === "create"
        ? "relay.apiKeyCreateStaged"
        : kind === "setEnabled"
          ? (value ? "relay.apiKeyEnableStaged" : "relay.apiKeyDisableStaged")
          : "relay.apiKeyUpdateStaged";
      publishGlobalFeedback(translate(feedbackKey));
    } catch {
      publishGlobalFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const updateAutoGrouping = async (enabled: boolean): Promise<void> => {
    if (!selected || !apiKeyActions?.setAutoGrouping) return;
    const accountID = selected.id;
    setSelectedResourceID(undefined);
    setFormBusy(true);
    setFeedback(undefined);
    clearGlobalStatus();
    try {
      const result = await apiKeyActions.setAutoGrouping(accountID, enabled);
      if (result.draftStaged) publishGlobalFeedback(translate("relay.apiKeyAutoGroupingStaged"));
      else clearGlobalStatus();
    } catch {
      // AppKit keeps the user's native click state while an async action is
      // pending. If Core rejects the draft transition, remount this one
      // control so it returns to the authoritative snapshot value instead of
      // displaying a checked box beside the still-manual resource list.
      setAutoGroupingControlRevision((current) => current + 1);
      publishGlobalFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  useEffect(() => {
    if (setupOnly || visible === false || !selected?.autoGrouping || !apiKeyActions?.alignAutoGrouping) return;
    let active = true;
    const interval = setInterval(() => {
      if (!active || controlsBusy || isAccountLoading(selected.id)) return;
      void (async () => {
        const status = await refreshAccountResources(selected, true);
        if (!active || status !== "ready" || isAccountLoading(selected.id)) return;
        try {
          await apiKeyActions.alignAutoGrouping?.(selected.id);
          if (active) await refreshAccounts();
        } catch {
          // Keep the staged state visible; the next interval can retry.
        }
      })();
    }, autoGroupIntervalMinutes * 60_000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [apiKeyActions, autoGroupIntervalMinutes, controlsBusy, refreshAccounts, selected?.autoGrouping, selected?.id, setupOnly, visible]);
  const openRemoteKeyDelete = (account: RelayAccount, resource: RelayResource): void => {
    if (account.autoGrouping || resourceGroupUnavailable(resource, account.groups)) return;
    setRemoteDeletePolicy("detach_disabled");
    setRemoteKeyDelete({ account, resource });
  };
  const executeRemoteKeyDelete = async (): Promise<void> => {
    if (!remoteKeyDelete) return;
    const { account, resource } = remoteKeyDelete;
    if (account.autoGrouping || resourceGroupUnavailable(resource, account.groups)) {
      setRemoteKeyDelete(undefined);
      return;
    }
    setFormBusy(true);
    setFeedback(undefined);
    clearGlobalStatus();
    try {
      if (remoteDeletePolicy === "detach_only") await apiKeyActions?.detach?.(account.id, resource.id);
      else await apiKeyActions?.remove?.(account.id, resource.id, remoteDeletePolicy);
      await refreshAccounts();
      if (resource.id === selectedResourceID) setSelectedResourceID(undefined);
      publishGlobalFeedback(translate(remoteDeletePolicy === "detach_only" ? "relay.apiKeyDetachStaged" : "relay.apiKeyDeleteStaged"));
      setRemoteKeyDelete(undefined);
    } catch {
      publishGlobalFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const copyApiKey = async (resource: RelayResource): Promise<void> => {
    if (!selected) return;
    clearGlobalStatus();
    try {
      const copied = await native.copySecret({
        domain: "relay_accounts",
        field: "api_key",
        target: `${selected.id}:${resource.id}`,
      });
      publishGlobalFeedback(translate(copied ? "relay.apiKeyCopied" : "relay.operationFailed"));
    } catch {
      publishGlobalFeedback(translate("relay.operationFailed"));
    }
  };
  const updateRememberPassword = async (next: boolean): Promise<void> => {
    if (!selected) return;
    const accountID = selected.id;
    const previous = selectedRememberPassword;
    const writeVersion = (rememberPasswordWriteVersion.current.get(accountID) ?? 0) + 1;
    rememberPasswordWriteVersion.current.set(accountID, writeVersion);
    setRememberPasswordDrafts((current) => ({ ...current, [accountID]: next }));
    setFeedback(undefined);
    try {
      await commit("account.update", { id: accountID, remember_password: next }, "relay_accounts");
      if (!next) {
        try {
          await native.clearRelayPassword(accountID);
        } catch {
          // Core already removed the saved password. Native stores may have no
          // additional password entry on this platform.
        }
      }
    } catch {
      if (rememberPasswordWriteVersion.current.get(accountID) === writeVersion) {
        setRememberPasswordDrafts((current) => current[accountID] === next ? { ...current, [accountID]: previous } : current);
        setFeedback(translate("relay.operationFailed"));
      }
    } finally {
      if (rememberPasswordWriteVersion.current.get(accountID) === writeVersion) {
        rememberPasswordWriteVersion.current.delete(accountID);
        setRememberPasswordDrafts((current) => {
          const account = accountsRef.current.find((item) => item.id === accountID);
          if (!account || current[accountID] !== account.rememberPassword) return current;
          const reconciled = { ...current };
          delete reconciled[accountID];
          return reconciled;
        });
      }
    }
  };
  const stageStationUpdate = async (stationID = effectiveSelectedStationID, overrides: { name?: string; origin?: string; type?: RelayType } = {}): Promise<void> => {
    const station = stationID ? stations.find((item) => item.id === stationID) : undefined;
    if (!station) return;
    const draft = stationDraftsRef.current[station.id] ?? {};
    const name = (overrides.name ?? draft.name ?? stationDisplayName(station, translate)).trim();
    const origin = normalizeRelayOrigin(overrides.origin ?? draft.origin ?? station.origin);
    const type = overrides.type ?? draft.type ?? station.type;
    if (!name || !origin) return;
    const dirty = name !== stationDisplayName(station, translate).trim()
      || origin !== normalizeRelayOrigin(station.origin)
      || type !== station.type;
    if (!dirty) return;
    setStationFormBusy(true);
    setFeedback(undefined);
    try {
      await commit("station.update", {
        id: station.id,
        name,
        origin,
        type,
      }, "relay_accounts");
      await refreshAccounts();
      setFeedback(translate("relay.stationUpdateStaged"));
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setStationFormBusy(false);
    }
  };

  if (visible === false) return <View style={styles.hidden} />;

  const selectAccount = (accountID: string): void => {
    if (hideNavigation) onNavigationSelectionChange?.("relay:account:" + accountID);
    setSelectedID(accountID);
    setSelectedStationID(undefined);
    setAdding(false);
    setSelectedResourceID(undefined);
    setFeedback(undefined);
  };
  const selectStation = (stationID: string): void => {
    if (hideNavigation) onNavigationSelectionChange?.("relay:station:" + stationID);
    const station = stations.find((item) => item.id === stationID);
    setSelectedID(undefined);
    setSelectedStationID(stationID);
    setAdding(false);
    setSelectedResourceID(undefined);
    setFeedback(undefined);
  };
  const selectRelayTableRow = (key: string): void => {
    const separator = key.indexOf(":");
    const kind = separator >= 0 ? key.slice(0, separator) : key;
    const id = separator >= 0 ? key.slice(separator + 1) : "";
    if (!id) return;
    if (kind === "station") selectStation(id);
    else if (kind === "account") selectAccount(id);
  };
  const updateAddOrigin = (value: string): void => {
    typeDetectionRequest.current += 1;
    setAddStationID("__custom__");
    setOrigin(value);
    if (!stationNameEdited) {
      setAddStationName(suggestedRelayStationName(value));
      setStationNameEdited(false);
    }
    setTypeDetection(undefined);
    setManualType(undefined);
  };
  const updateAddStationName = (value: string): void => {
    setAddStationName(value);
    setStationNameEdited(value.trim() !== suggestedRelayStationName(origin));
  };
  const detectedAddType = typeDetection !== "checking" && typeDetection !== "unknown" ? typeDetection : undefined;
  const selectedAddType = manualType ?? detectedAddType;
  const addStationMode = selectedAddStation ? "existing" : "custom";
  const addStationModeLabels = stations.length > 0
    ? [translate("relay.stationCustom"), translate("relay.stationExisting")]
    : [translate("relay.stationCustom")];
  const selectAddStation = (index: number): void => {
    const station = stations[index];
    typeDetectionRequest.current += 1;
    if (!station) {
      setAddStationID("__custom__");
      setOrigin("");
      setAddStationName("");
      setTypeDetection(undefined);
      setManualType(undefined);
      setStationNameEdited(false);
      return;
    }
    setAddStationID(station.id);
    setOrigin(station.origin);
    setAddStationName(stationDisplayName(station, translate));
    setStationNameEdited(false);
    setTypeDetection(station.type);
    setManualType(station.type);
  };
  const selectAddStationMode = (index: number): void => {
    if (index === 0) {
      typeDetectionRequest.current += 1;
      setAddStationID("__custom__");
      setOrigin("");
      setAddStationName("");
      setTypeDetection(undefined);
      setManualType(undefined);
      setStationNameEdited(false);
      return;
    }
    if (stations.length > 0) selectAddStation(0);
  };
  const localPolicyOptions: Array<PolicyOption<LocalDependencyPolicy>> = [
    { value: "detach", label: translate("relay.policyRelease"), hint: translate("relay.policyReleaseHint") },
    { value: "delete_models", label: translate("relay.policyDeleteModels"), hint: translate("relay.policyDeleteModelsHint") },
  ];
  const remotePolicyOptions: Array<PolicyOption<RemoteDeletePolicy>> = [
    { value: "detach_disabled", label: translate("relay.policyReleaseDisabled"), hint: translate("relay.policyReleaseDisabledHint") },
    { value: "delete_models", label: translate("relay.policyDeleteModels"), hint: translate("relay.policyDeleteModelsHint") },
    { value: "detach_only", label: translate("relay.apiKeyDetachOnly"), hint: translate("relay.apiKeyDetachOnlyHint") },
  ];
  const localResourceCount = localRemoval?.kind === "station"
    ? accounts.filter((account) => localRemoval.station.accountIDs.includes(account.id)).reduce((sum, account) => sum + account.resources.length, 0)
    : localRemoval?.kind === "account"
      ? localRemoval.account.resources.length
      : 0;
  const localRemovalMessage = localRemoval?.kind === "station"
    ? translate("relay.removeStationBody", { label: stationDisplay(localRemoval.station), accounts: localRemoval.station.accountIDs.length, keys: localResourceCount, models: localRemoval.station.linkedModelCount })
    : localRemoval?.kind === "account"
      ? translate("relay.removeAccountBody", { label: accountDisplayName(localRemoval.account, translate), keys: localResourceCount, models: localRemoval.account.linkedModelCount })
      : "";
  const selectedStationAccounts = selectedStation ? stationAccounts(selectedStation) : [];
  const selectedStationResourceCount = selectedStationAccounts.reduce((total, account) => total + account.resources.length, 0);
  const selectedStationModelCount = selectedStation?.linkedModelCount
    || selectedStationAccounts.reduce((total, account) => total + account.linkedModelCount, 0);
  return <View style={styles.workspace} accessibilityLabel={translate("relay.title")}>
    {!setupOnly && pendingCleanups.length > 0 ? <ScrollView style={styles.pendingCleanupList} contentContainerStyle={styles.pendingCleanupListContent} showsVerticalScrollIndicator={pendingCleanups.length > 2}>
      {pendingCleanups.map((cleanup) => <View key={cleanup.accountID} style={styles.pendingCleanup}>
        <Text numberOfLines={2} style={styles.pendingCleanupText}>{translate("relay.credentialsCleanupPending", { label: cleanup.label })}</Text>
        <NativeButton title={translate("relay.retryCleanup")} compact disabled={cleanupBusy} onPress={() => { void retryCredentialCleanup(cleanup); }} />
      </View>)}
    </ScrollView> : null}
    <View style={styles.relayLayout}>
      {!setupOnly && !hideNavigation ? <RelayTablePane title={translate("relay.accountsHeader")} style={styles.sidebar} actions={<>
        <NativeButton title={translate("relay.addAccount")} symbol="plus" toolTip={translate("relay.addAccount")} accessibilityLabel={translate("relay.addAccount")} compact disabled={adding} onPress={beginAdding} style={styles.sidebarAddButton} />
        <NativeButton title={translate("relay.removeLocal")} symbol="minus" toolTip={translate("relay.removeLocal")} accessibilityLabel={translate("relay.removeLocal")} destructive compact disabled={(!selected && !selectedStation) || adding} onPress={openLocalRemoval} style={styles.sidebarIconButton} />
      </>}>
        {stations.length > 0 ? <View style={styles.sidebarTableFrame}><NativeTable
          columns={[{ label: translate("common.name"), width: 118 }, { label: translate("relay.balance"), width: 78 }]}
          rows={relayTableRows}
          selectedKey={relayTableSelection}
          compact
          striped
          firstColumnHorizontalPadding={8}
          scrollTrailingColumnOverflow={false}
          onSelectionChange={selectRelayTableRow}
          style={styles.nativeRelayTable}
        /></View> : <View style={styles.sidebarEmpty}><Text style={styles.sidebarEmptyText}>{translate("relay.empty")}</Text></View>}
      </RelayTablePane> : null}
      <View style={[styles.detail, setupOnly && styles.setupDetail]}>
        {adding ? addStep === "sign-in" ? <View style={styles.detailWorkspace}>
          <View style={[styles.detailContent, styles.fixedDetailPane, compactStyles.detailContent, setupOnly && styles.setupContent, setupControlsBusy && styles.loadingSurface]}>
            <View style={setupOnly ? styles.setupSurface : undefined}>
              <SetupProgress step="sign-in" translate={translate} />
              <View style={[styles.detailHeader, setupOnly && styles.setupHeader]}>
                <Text style={styles.detailTitle}>{translate("relay.stepSignIn")}</Text>
                <Text style={styles.detailSubtitle}>{translate("relay.stepSignInDetail")}</Text>
              </View>
              <View style={styles.signInWaiting}><Text style={styles.formHint}>{translate("relay.loginWorking")}</Text></View>
            </View>
          </View>
          <View style={[styles.bottomBar, compactStyles.bottomBar, setupOnly && styles.setupBottomBar]}>
            <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.bottomStatus}>{feedback ?? translate("relay.loginWorking")}</Text>
            {setupOnly ? <View style={[styles.bottomActions, styles.setupBottomActions]}>
              <NativeButton title={translate("relay.back")} onPress={returnToStationStep} />
              {onClose ? <NativeButton title={translate("menu.close")} onPress={onClose} /> : null}
            </View> : null}
          </View>
        </View> : <View style={styles.detailWorkspace}>
          <View style={[styles.detailContent, styles.fixedDetailPane, compactStyles.detailContent, setupOnly && styles.setupContent, setupControlsBusy && styles.loadingSurface]}>
            <View style={setupOnly ? styles.setupSurface : undefined}>
              {setupOnly ? <SetupProgress step="origin" translate={translate} /> : null}
              <View style={[styles.detailHeader, setupOnly && styles.setupHeader]}>
                <Text style={styles.detailTitle}>{translate("relay.addAccount")}</Text>
              </View>
              <View style={[styles.formSection, setupOnly && styles.setupFormSection]}>
                <FormRow label={translate("relay.stationChoice")}><NativeSegmentedControl labels={addStationModeLabels} selectedValue={addStationMode === "existing" ? translate("relay.stationExisting") : translate("relay.stationCustom")} disabled={setupControlsBusy} onChange={({ nativeEvent }) => selectAddStationMode(nativeEvent.index)} style={styles.stationModeSelector} /></FormRow>
                {addStationMode === "existing" && selectedAddStation ? <FormRow label={translate("relay.station")}><NativePicker labels={stations.map((station) => stationPickerLabel(projectedStation(station), translate))} selectedValue={stationPickerLabel(projectedStation(selectedAddStation), translate)} disabled={setupControlsBusy} onChange={({ nativeEvent }) => selectAddStation(nativeEvent.index)} style={styles.typeSelector} /></FormRow> : null}
                <FormRow label={translate("relay.origin")}><NativeTextField value={origin} placeholder={translate("relay.originPlaceholder")} editable={!setupControlsBusy && !selectedAddStation} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={updateAddOrigin} style={[styles.control, compactStyles.control]} /></FormRow>
                <FormRow label={translate("relay.stationName")}><NativeTextField value={addStationName} placeholder={translate("relay.stationNamePlaceholder")} editable={!setupControlsBusy && !selectedAddStation} accessibilityLabel={translate("relay.stationName")} onChangeText={updateAddStationName} style={[styles.control, compactStyles.control]} /></FormRow>
                <FormRow label={translate("relay.type")}><NativePicker labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={selectedAddType ? relayTypeLabel(selectedAddType, translate) : ""} disabled={setupControlsBusy || Boolean(selectedAddStation?.type)} onChange={({ nativeEvent }) => { setManualType(nativeEvent.index === 1 ? "sub2api" : "newapi"); }} style={styles.typeSelector} /></FormRow>
                {passwordStorageAvailable ? <View style={styles.rememberPasswordRow}><NativeCheckbox label={translate("relay.rememberPassword")} value={rememberPassword} disabled={setupControlsBusy} onValueChange={setRememberPassword} /></View> : <Text style={styles.formHint}>{translate("relay.passwordNotSaved")}</Text>}
              </View>
            </View>
          </View>
          <View style={[styles.bottomBar, compactStyles.bottomBar, setupOnly && styles.setupBottomBar]}>
            {feedback || !setupOnly ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.bottomStatus}>{feedback ?? translate("relay.stepSignInDetail")}</Text> : null}
            <View style={[styles.bottomActions, setupOnly && styles.setupBottomActions]}>{setupOnly && onClose ? <NativeButton title={translate("menu.close")} onPress={onClose} /> : !setupOnly ? <NativeButton title={translate("menu.cancel")} disabled={setupControlsBusy} onPress={resetForm} /> : null}<NativeButton title={translate("relay.next")} primary disabled={setupControlsBusy || !origin.trim() || !addStationName.trim()} onPress={() => { void beginLogin(); }} /></View>
          </View>
        </View> : selectedStation ? <View style={styles.detailWorkspace}>
          <View style={styles.stationSimpleForm}>
            <View style={styles.stationHeader}>
              <View style={styles.stationHeaderHeading}>
                <Text numberOfLines={1} style={styles.detailTitle}>{stationNameDraft}</Text>
                <Text numberOfLines={1} selectable style={styles.detailSubtitle}>{stationOriginDraft || translate("common.notAvailable")}</Text>
              </View>
              <View style={styles.stationHeaderMetrics}>
                {stationLoading ? <Text accessibilityLiveRegion="polite" style={styles.loadingTip}>{translate("relay.status.loading")}</Text> : null}
                <Text style={styles.stationHeaderMetric}>{translate("relay.stationAccountCount", { count: selectedStationAccounts.length })}</Text>
                <Text style={styles.stationHeaderMetric}>{translate("relay.stationKeyCount", { count: selectedStationResourceCount })}</Text>
                <Text style={styles.stationHeaderMetric}>{translate("relay.modelsCount", { count: selectedStationModelCount })}</Text>
              </View>
            </View>
            <Text style={styles.stationSimpleHint}>{translate("relay.stationOverviewHint")}</Text>
            <View style={[styles.stationSettingsForm, compactStyles.stationSettingsForm]}>
              <Text style={styles.stationSettingsTitle}>{translate("relay.connectionDetails")}</Text>
              <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow]}>
                <Text style={styles.stationSettingsLabel}>{translate("relay.stationName")}</Text>
                <NativeTextField value={stationNameDraft} editable={!controlsBusy} accessibilityLabel={translate("relay.stationName")} onChangeText={(value) => setStationDraft(selectedStation.id, { name: value })} onBlur={() => { if (!controlsBusy) void stageStationUpdate(selectedStation.id); }} style={[styles.stationSettingsControl, compactStyles.control]} />
              </View>
              <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow]}>
                <Text style={styles.stationSettingsLabel}>{translate("relay.origin")}</Text>
                <NativeTextField value={stationOriginDraft} editable={!controlsBusy} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={(value) => setStationDraft(selectedStation.id, { origin: value })} onBlur={() => { if (!controlsBusy) void stageStationUpdate(selectedStation.id); }} style={[styles.stationSettingsControl, compactStyles.control]} />
              </View>
              <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow, styles.stationSettingsLastRow]}>
                <Text style={styles.stationSettingsLabel}>{translate("relay.type")}</Text>
                <NativePicker labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={stationTypeDraft ? relayTypeLabel(stationTypeDraft, translate) : ""} disabled={controlsBusy} onChange={({ nativeEvent }) => { const nextType = nativeEvent.index === 1 ? "sub2api" : "newapi"; setStationDraft(selectedStation.id, { type: nextType }); void stageStationUpdate(selectedStation.id, { type: nextType }); }} style={[styles.stationSettingsControl, compactStyles.control]} />
              </View>
            </View>
            {feedback ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.stationSettingsFeedback}>{feedback}</Text> : null}
          </View>
        </View> : !setupOnly && selected ? <View style={styles.detailWorkspace}>
          <View style={styles.accountDetailContent}>
            <View style={styles.accountHeader}>
              <View style={styles.accountBreadcrumb}>
                {selectedAccountStation ? <NativeButton
                  title={accountStationDisplay(selected)}
                  link
                  disabled={controlsBusy}
                  accessibilityLabel={`${translate("relay.station")}: ${accountStationDisplay(selected)}`}
                  onPress={() => selectStation(selectedAccountStation.id)}
                  style={styles.accountBreadcrumbStation}
                /> : <Text numberOfLines={1} style={styles.accountBreadcrumbStationText}>{accountStationDisplay(selected)}</Text>}
                <Text style={styles.accountBreadcrumbSeparator}>&gt;</Text>
                <Text accessibilityLabel={`${translate("relay.username")}: ${accountDetailTitle(selected, translate)}`} numberOfLines={1} style={styles.accountBreadcrumbAccount}>{accountDetailTitle(selected, translate)}</Text>
              </View>
              <View style={styles.accountHeaderRight}>
                {passwordStorageAvailable ? <NativeCheckbox label={translate("relay.rememberPassword")} value={selectedRememberPassword} disabled={controlsBusy} onValueChange={(next) => { void updateRememberPassword(next); }} style={styles.accountRememberPassword} /> : <Text style={styles.formHint}>{translate("relay.passwordNotSaved")}</Text>}
                <View style={styles.accountSessionSummary}>
                  <View style={[styles.statusDot, selectedHeaderSignedIn ? styles.statusDotOnline : styles.statusDotLoading]} />
                  <Text accessibilityLiveRegion="polite" accessibilityLabel={selectedHeaderValue} selectable={selectedHeaderShowsBalance} numberOfLines={1} style={[styles.accountSessionValue, !selectedHeaderShowsBalance && styles.loadingTip]}>{selectedHeaderValue}</Text>
                  <NativeButton title={translate("common.refresh")} symbol="refresh" compact disabled={controlsBusy} toolTip={translate("common.refresh")} accessibilityLabel={translate("common.refresh")} onPress={() => { if (!isAccountLoading(selected.id)) void refreshLoginState(selected); }} style={styles.accountRefreshButton} />
                </View>
              </View>
            </View>
            <View style={[styles.resourcesSection, compactStyles.resourcesSection]}>
              <View style={styles.resourcePane}>
                <View style={styles.resourceBody}>
                  <View style={styles.resourceColumns}>
                    <View style={styles.resourceListPane}>
                      <View style={[styles.resourceToolbar, compactStyles.resourceToolbar]}>
                        <View style={styles.resourceToolbarHeading}>
                          <Text style={styles.resourceToolbarTitle}>{translate("relay.apiKeysTitle")}</Text>
                          <NativeCheckbox
                            key={`auto-grouping:${selected.id}:${selected.autoGrouping}:${autoGroupingControlRevision}`}
                            label={translate("relay.apiKeyAutoGrouping")}
                            value={selected.autoGrouping}
                            disabled={controlsBusy || !apiKeyActions?.setAutoGrouping}
                            onValueChange={(enabled) => { void updateAutoGrouping(enabled); }}
                            style={styles.resourceAutoGroupingCheckbox}
                          />
                          <View style={styles.resourceToolbarCrud}>
                            <NativeButton title={translate("relay.apiKeyCreate")} symbol="plus" compact disabled={controlsBusy || selected.autoGrouping || !apiKeyActions?.create} toolTip={translate("relay.apiKeyCreate")} accessibilityLabel={translate("relay.apiKeyCreate")} onPress={() => setApiKeyCreateOpen(true)} style={styles.resourceToolbarCrudButton} />
                            <NativeButton title={translate("relay.apiKeyDelete")} symbol="minus" compact destructive disabled={controlsBusy || selected.autoGrouping || (!apiKeyActions?.remove && !apiKeyActions?.detach) || !selectedResource} toolTip={translate("relay.apiKeyDelete")} accessibilityLabel={selectedResource ? `${translate("relay.apiKeyDelete")}: ${resourceDisplayName(selectedResource)}` : translate("relay.apiKeyDelete")} onPress={() => { if (selectedResource) openRemoteKeyDelete(selected, selectedResource); }} style={styles.resourceToolbarCrudButton} />
                          </View>
                        </View>
                      </View>
                      <NativeTable
                        // Keep the three headers in the initial viewport at
                        // the relay route's minimum window width.  The native
                        // table has a vertical scroller which consumes part
                        // of the list pane; when the requested widths exceed
                        // that viewport it shrinks from the trailing column,
                        // making 倍率 disappear entirely.  These compact
                        // widths leave room for the scroller while retaining
                        // enough text space for the multiplier values.
                        columns={[{ label: translate("common.name"), width: 88 }, { label: translate("relay.apiKeyGroup"), width: 84 }, { label: translate("relay.apiKeyMultiplier"), width: 58 }]}
                        rows={resourceTableRows}
                        selectedKey={selectedResource?.id ?? ""}
                        disabledRowKeys={resourceUnavailableRowKeys}
                        secondaryCellKeys={resourceSecondaryCellKeys}
                        compact
                        striped
                        cellHorizontalPadding={6}
                        firstColumnHorizontalPadding={8}
                        scrollTrailingColumnOverflow={false}
                        onSelectionChange={setSelectedResourceID}
                        style={styles.resourceNativeTable}
                      />
                    </View>
                    <View style={styles.resourceInspectorPane}>
                      {selectedResource ? <ResourceInspector
                        account={selected}
                        resource={selectedResource}
                        signedIn={!selectedStatusLoading && effectiveLoginStatus(selected) === "signed_in"}
                        disabled={controlsBusy}
                        autoGrouping={selected.autoGrouping}
                        groupUnavailable={selectedResourceGroupUnavailable}
                        nameValue={resourceDisplayName(selectedResource)}
                        resourceGroups={resourceGroups}
                        selectedResourceGroupLabel={selectedResourceGroupLabel}
                        onNameChange={(value) => setApiKeyNameDraft(selectedResource.id, value)}
                        onNameCommit={() => runApiKeyAction("update", selectedResource.id)}
                        onGroupChange={(groupID) => { void runApiKeyAction("setGroup", selectedResource.id, groupID); }}
                        onEnabledChange={(enabled) => { void runApiKeyAction("setEnabled", selectedResource.id, enabled); }}
                        onCopy={() => { void copyApiKey(selectedResource); }}
                        translate={translate}
                      /> : <View style={styles.resourceEmpty}><Text style={styles.resourceEmptyTitle}>{translate(selected.resourceError === "no_api_keys" && !selectedLoading.resources && !selectedLoading.session ? "relay.resourcesEmptyTitle" : "relay.resources")}</Text><Text style={styles.resourceEmptyText}>{selectedLoading.resources || selectedLoading.session ? translate("relay.resourcesChecking") : resourceHint(selected, translate)}</Text></View>}
                    </View>
                  </View>
                </View>
              </View>
            </View>
          </View>
        </View> : <View style={styles.blank}><Text style={styles.empty}>{translate("relay.empty")}</Text><NativeButton title={translate("relay.add")} primary disabled={controlsBusy} onPress={beginAdding} /></View>}
      </View>
    </View>
    <ApiKeyCreateDialog visible={apiKeyCreateOpen} groups={selected?.groups ?? []} disabled={controlsBusy} onClose={() => setApiKeyCreateOpen(false)} onCreate={(options) => { setApiKeyCreateOpen(false); void runApiKeyAction("create", undefined, options); }} translate={translate} />
    <DependencyPolicyDialog visible={Boolean(localRemoval)} title={translate("relay.removeLocalTitle")} message={localRemovalMessage} options={localPolicyOptions} value={localDependencyPolicy} disabled={controlsBusy} confirmLabel={translate("relay.removeLocal")} onValueChange={setLocalDependencyPolicy} onClose={() => setLocalRemoval(undefined)} onConfirm={() => { void executeLocalRemoval(); }} translate={translate} />
    <DependencyPolicyDialog visible={Boolean(remoteKeyDelete)} title={translate("relay.apiKeyDeleteImpactTitle")} message={remoteKeyDelete ? translate("relay.apiKeyDeleteImpactBody", { count: remoteKeyDelete.resource.linkedModelCount, label: resourceDisplayName(remoteKeyDelete.resource) }) : ""} options={remotePolicyOptions} value={remoteDeletePolicy} disabled={controlsBusy} confirmLabel={remoteDeletePolicy === "detach_only" ? translate("screen.confirm") : translate("common.delete")} onValueChange={setRemoteDeletePolicy} onClose={() => setRemoteKeyDelete(undefined)} onConfirm={() => { void executeRemoteKeyDelete(); }} translate={translate} />
  </View>;
}

const colors = {
  window: Platform.OS === "macos" ? PlatformColor("windowBackgroundColor") : PlatformColor("Window"),
  text: Platform.OS === "macos" ? PlatformColor("labelColor") : PlatformColor("WindowText"),
  secondary: Platform.OS === "macos" ? PlatformColor("secondaryLabelColor") : PlatformColor("GrayText"),
  separator: Platform.OS === "macos" ? PlatformColor("separatorColor") : PlatformColor("ControlStrokeColorDefault"),
  panel: Platform.OS === "macos" ? PlatformColor("controlBackgroundColor") : PlatformColor("ControlFillColorDefault"),
  accent: Platform.OS === "macos" ? PlatformColor("systemBlueColor") : PlatformColor("AccentFillColorDefault"),
  accentText: Platform.OS === "macos" ? PlatformColor("alternateSelectedControlTextColor") : PlatformColor("TextOnAccentFillColorPrimary"),
  success: "#2A9D68",
  warning: "#D58A27",
};

const compactStyles = StyleSheet.create({
  detailContent: { paddingHorizontal: 12, paddingTop: 6, paddingBottom: 12, gap: 8 },
  formRow: { minHeight: 30, gap: 3 },
  formValue: { minHeight: 26, gap: 3 },
  control: { height: 26 },
  stationSettings: { gap: 6 },
  stationSettingsForm: { gap: 4 },
  stationSettingsRow: { minHeight: 26, gap: 6 },
  resourcesSection: { gap: 8, paddingTop: 4 },
  resourceToolbar: { minHeight: 32, paddingLeft: 0, paddingRight: 12, gap: 8 },
  bottomBar: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 6, gap: 6 },
});

const styles = StyleSheet.create({
  hidden: { display: "none" },
  workspace: { flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden", backgroundColor: colors.window },
  relayLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP },
  setupDetail: { width: "100%" },
  tablePane: { minWidth: 0, minHeight: 0, gap: 0 },
  tableTitleRow: { height: 38, minHeight: 38, paddingHorizontal: 10, flexDirection: "row", alignItems: "center" },
  tableTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  tableActions: { marginLeft: "auto", flexDirection: "row", alignItems: "center", gap: 6 },
  sidebar: { width: 200, minWidth: 200, maxWidth: 200, flexGrow: 0, flexShrink: 0 },
  sidebarTableFrame: { flex: 1, minWidth: 0, minHeight: 0 },
  sidebarEmpty: { flex: 1, minHeight: 120, justifyContent: "center", padding: 16, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel },
  sidebarEmptyText: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  nativeRelayTable: { flex: 1, minWidth: 0, minHeight: 0 },
  sidebarAddButton: { width: 22, minWidth: 22, height: 22 },
  sidebarIconButton: { width: 22, minWidth: 22, height: 22 },
  detail: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  detailWorkspace: { flex: 1, minWidth: 0, minHeight: 0 },
  detailContent: { flexGrow: 1, minWidth: 0, paddingHorizontal: 20, paddingTop: 16, paddingBottom: 20, gap: 14 },
  fixedDetailPane: { flex: 1, overflow: "hidden" },
  accountDetailContent: { flex: 1, minWidth: 0, minHeight: 0 },
  setupContent: { justifyContent: "flex-start", alignItems: "center", paddingHorizontal: 24, paddingTop: 18, paddingBottom: 12 },
  setupSurface: { width: "100%", maxWidth: 520, minWidth: 0, gap: 12 },
  setupProgress: { width: "100%", flexDirection: "row", alignItems: "center", minHeight: 22, gap: 18 },
  setupProgressStep: { flexDirection: "row", alignItems: "center", gap: 7, flexShrink: 0 },
  setupProgressBadge: { width: 20, height: 20, borderRadius: 10, borderWidth: 1, borderColor: colors.separator, alignItems: "center", justifyContent: "center", backgroundColor: colors.window },
  setupProgressBadgeCurrent: { borderColor: colors.accent, backgroundColor: colors.accent },
  setupProgressBadgeDone: { borderColor: colors.accent, backgroundColor: colors.window },
  setupProgressNumber: { color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  setupProgressNumberCurrent: { color: colors.accentText },
  setupProgressLabel: { color: colors.secondary, fontSize: UI_FONT_SIZE },
  setupProgressLabelCurrent: { color: colors.text, fontWeight: "600" },
  setupHeader: { paddingBottom: 0, gap: 6 },
  setupFormSection: { maxWidth: 520, paddingVertical: 0, gap: 10 },
  stationSimpleForm: { flex: 1, minWidth: 0, paddingHorizontal: 18, paddingTop: 14, paddingBottom: 14, gap: 8 },
  stationSimpleHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  stationHeader: { minWidth: 0, minHeight: 40, paddingBottom: 8, flexDirection: "row", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", columnGap: 14, rowGap: 5, borderBottomWidth: 1, borderBottomColor: colors.separator },
  stationHeaderHeading: { flex: 1, minWidth: 180, gap: 2 },
  stationHeaderMetrics: { flexShrink: 1, flexDirection: "row", flexWrap: "wrap", justifyContent: "flex-end", gap: 8 },
  stationHeaderMetric: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  stationSettingsTitle: { color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  stationSettingsForm: { width: "100%", gap: 4, paddingTop: 3 },
  stationSettingsRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  stationSettingsLastRow: {},
  stationSettingsLabel: { width: 72, flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  stationSettingsControl: { flex: 1, minWidth: 160, height: 26 },
  stationSettingsFeedback: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  accountHeader: { minWidth: 0, minHeight: 38, paddingHorizontal: 12, paddingVertical: 5, flexDirection: "row", alignItems: "center", columnGap: 6, backgroundColor: colors.window },
  accountBreadcrumb: { flex: 1, minWidth: 0, minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 },
  accountBreadcrumbStation: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  accountBreadcrumbStationText: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  accountBreadcrumbSeparator: { flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  accountBreadcrumbAccount: { minWidth: 0, flexShrink: 1, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  accountHeaderRight: { marginLeft: "auto", marginRight: -4, flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 4 },
  accountRememberPassword: { minWidth: 78, flexShrink: 0 },
  accountSessionSummary: { flexShrink: 0, minHeight: 22, flexDirection: "row", alignItems: "center", gap: 5 },
  accountSessionValue: { flexShrink: 0, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "500" },
  detailHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  detailTitle: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  detailSubtitle: { flexShrink: 1, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  loadingTip: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  statusDot: { width: 7, height: 7, borderRadius: 4 },
  statusDotOnline: { backgroundColor: colors.success },
  statusDotLoading: { backgroundColor: colors.secondary },
  accountRefreshButton: { width: 22, minWidth: 22, height: 22 },
  control: { width: "100%", minWidth: 0, height: 32 },
  stationModeSelector: { width: "100%", minWidth: 0, maxWidth: 520, alignSelf: "stretch" },
  typeSelector: { width: "100%", minWidth: 0, maxWidth: 520, alignSelf: "stretch" },
  formSection: { width: "100%", maxWidth: 720, minWidth: 0, paddingVertical: 0, gap: 8, backgroundColor: colors.window },
  formRow: { width: "100%", minHeight: 34, flexDirection: "column", alignItems: "stretch", gap: 6 },
  rememberPasswordRow: { minHeight: 24, justifyContent: "center" },
  formLabel: { width: "100%", minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  formValue: { width: "100%", minWidth: 0, minHeight: 30, justifyContent: "center", gap: 4 },
  formHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17, paddingVertical: 5 },
  signInWaiting: { minHeight: 160, alignItems: "center", justifyContent: "center", paddingHorizontal: 20 },
  resourcesSection: { flex: 1, minWidth: 0, minHeight: 0, borderTopWidth: 1, borderTopColor: colors.separator, paddingTop: 4 },
  resourcePane: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  resourceToolbar: { minHeight: 32, paddingHorizontal: 12, paddingVertical: 3, flexDirection: "row", alignItems: "center", gap: 8 },
  resourceToolbarHeading: { flex: 1, minWidth: 110, flexDirection: "row", alignItems: "center", gap: 6 },
  resourceToolbarTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  resourceAutoGroupingCheckbox: { flexShrink: 0 },
  resourceToolbarCrud: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 },
  resourceToolbarCrudButton: { width: 22, minWidth: 22, height: 22 },
  resourceBody: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  resourceColumns: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP },
  resourceListPane: { flex: 1, minWidth: 0, minHeight: 0 },
  resourceNativeTable: { flex: 1, minWidth: 0, minHeight: 0 },
  relayDialogLayer: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, zIndex: 100 },
  dialogBackdrop: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24, backgroundColor: "rgba(0, 0, 0, 0.22)" },
  dialogHeader: { height: 36, minHeight: 36, paddingHorizontal: 12, flexDirection: "row", alignItems: "center", borderBottomWidth: 1, borderBottomColor: colors.separator },
  apiKeyCreateDialog: { maxWidth: 640, minHeight: 300, borderRadius: 7, overflow: "hidden" },
  apiKeyDialogHeader: { height: 54, minHeight: 54, paddingHorizontal: 14, gap: 10 },
  apiKeyDialogTitleWrap: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "center", gap: 9 },
  apiKeyDialogIcon: { width: 28, height: 28, borderRadius: 7, alignItems: "center", justifyContent: "center", backgroundColor: colors.accent },
  apiKeyDialogIconText: { color: colors.accentText, fontSize: UI_FONT_SIZE, lineHeight: 22, fontWeight: "700" },
  apiKeyDialogTitleBlock: { flex: 1, minWidth: 0, gap: 1 },
  apiKeyDialogSubtitle: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  apiKeyDialogContent: { padding: 14, gap: 10 },
  apiKeyStageBanner: { minHeight: 42, paddingHorizontal: 10, paddingVertical: 7, borderWidth: 1, borderColor: colors.accent, borderRadius: 7, flexDirection: "row", alignItems: "flex-start", gap: 8, backgroundColor: colors.panel },
  apiKeyStageDot: { width: 8, height: 8, marginTop: 3, borderRadius: 4, backgroundColor: colors.accent },
  apiKeyStageCopy: { flex: 1, minWidth: 0, gap: 1 },
  apiKeyStageLabel: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 17, fontWeight: "600" },
  apiKeyStageHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
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
  apiKeyPreviewLabel: { flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  apiKeyPreviewValue: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, textAlign: "right" },
  apiKeyPreviewHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  dialogTitle: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  dialogClose: { width: 22, minWidth: 22, height: 22 },
  dialogFooter: { minHeight: 42, paddingHorizontal: 12, flexDirection: "row", alignItems: "center", gap: 8, borderTopWidth: 1, borderTopColor: colors.separator },
  dialogActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 6 },
  decisionDialog: { width: "100%", maxWidth: 430, minHeight: 204, backgroundColor: colors.window, borderWidth: 1, borderColor: colors.separator },
  decisionContent: { paddingHorizontal: 14, paddingVertical: 12, gap: 8 },
  decisionMessage: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  decisionHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  decisionField: { minHeight: 28, gap: 3 },
  decisionLabel: { color: colors.secondary, fontSize: UI_FONT_SIZE },
  decisionControl: { width: "100%", height: 26 },
  decisionSpacer: { flex: 1, minWidth: 0 },
  // Keep the details pane compact so the API-key table remains the primary
  // workspace.  The released width is absorbed by the table's flexing list
  // pane, making the three-column API-key list easier to scan.
  resourceInspectorPane: { width: 220, minWidth: 200, maxWidth: 220, flexGrow: 0, flexShrink: 0, minHeight: 0 },
  resourceInspectorScroll: { flex: 1, minWidth: 0, backgroundColor: colors.window },
  resourceInspectorScrollIndicator: { position: "absolute", width: 0, height: 0 },
  resourceInspectorContent: { flexGrow: 1, minWidth: 0, paddingTop: 6, paddingLeft: 0, paddingRight: 12, paddingBottom: 12, gap: 8 },
  resourceInspectorHeaderBlock: { minWidth: 0 },
  resourceInspectorHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  resourceInspectorHeading: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "baseline", gap: 5 },
  resourceInspectorTitle: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  resourceInspectorSubtitle: { flexShrink: 1, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  resourceInspectorDivider: { height: 1, backgroundColor: colors.separator },
  resourceInspectorForm: { minWidth: 0, gap: 6 },
  resourceInspectorToggleRow: { minHeight: 24, flexDirection: "row", alignItems: "center" },
  resourceInspectorRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 6 },
  resourceInspectorMultilineRow: { alignItems: "flex-start" },
  resourceInspectorKeyRow: { alignItems: "center" },
  resourceInspectorLabel: { width: 54, flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  resourceInspectorControlRow: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "center", gap: 3 },
  resourceInspectorTextInput: { flex: 1, minWidth: 90, height: 26 },
  resourceInspectorPicker: { flex: 1, minWidth: 120, height: 26 },
  resourceInspectorReadOnly: { flex: 1, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  resourceInspectorModelValue: { flex: 1, minWidth: 0, alignItems: "flex-start", gap: 2 },
  resourceInspectorModels: { width: "100%", minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  resourceInspectorModelsToggle: { minHeight: 20, alignSelf: "flex-start" },
  resourceInspectorSecureInput: { flex: 1, minWidth: 100, height: 26 },
  resourceInspectorAction: { width: 22, minWidth: 22, height: 22 },
  resourceEmpty: { flex: 1, minWidth: 0, minHeight: 138, paddingHorizontal: 24, paddingVertical: 18, alignItems: "center", justifyContent: "center", gap: 5, backgroundColor: colors.window },
  resourceEmptyTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600", lineHeight: 19, textAlign: "center" },
  resourceEmptyText: { maxWidth: 500, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18, textAlign: "center" },
  bottomBar: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 6, flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 6, backgroundColor: colors.panel },
  setupBottomBar: { minHeight: 46, paddingHorizontal: 20, paddingVertical: 8, borderTopWidth: 0, backgroundColor: colors.window },
  bottomStatus: { flex: 1, flexBasis: 220, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  bottomTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  bottomActions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 6 },
  setupBottomActions: { marginLeft: "auto" },
  pendingCleanupList: { maxHeight: 116, borderBottomWidth: 1, borderBottomColor: colors.separator, backgroundColor: colors.panel },
  pendingCleanupListContent: { paddingHorizontal: 18, paddingVertical: 8, gap: 6 },
  pendingCleanup: { minHeight: 30, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 12 },
  pendingCleanupText: { flex: 1, flexBasis: 260, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  loadingSurface: { opacity: 0.55 },
  empty: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 19, textAlign: "center" },
  blank: { flex: 1, minHeight: 240, alignItems: "center", justifyContent: "center", gap: 14, paddingHorizontal: 28, backgroundColor: colors.window },
});
