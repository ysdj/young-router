import React, { useEffect, useMemo, useRef, useState } from "react";
import { Platform, PlatformColor, ScrollView, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";
import type { CoreSnapshot, NativeLeafAdapter } from "../types";
import { NativeButton, NativeCheckbox, NativePersistentScrollIndicator, NativePicker, NativeSecureTextInput, NativeTable, NativeTextField } from "./NativeControls";
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

type RelayPendingOperation = {
  id: string;
  action: string;
  status: "staged" | "remote_applied" | "local_pending" | "completed";
};

type LocalRemovalIntent =
  | { kind: "station"; station: RelayStation }
  | { kind: "account"; account: RelayAccount };

type RemoteKeyDeleteIntent = { account: RelayAccount; resource: RelayResource };

type AddedRelayAccount = Pick<RelayAccount, "id" | "type" | "label" | "origin" | "username" | "rememberPassword">;
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
type AddStep = "origin" | "sign-in";
const INLINE_MODEL_LIMIT = 5;

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

function pendingOperationsFromSnapshot(snapshot?: CoreSnapshot): RelayPendingOperation[] {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  const values = Array.isArray(state.pending_operations) ? state.pending_operations : Array.isArray(domain.pending_operations) ? domain.pending_operations : [];
  return values.flatMap((value) => {
    const item = record(value);
    const status = item.status === "remote_applied" || item.status === "local_pending" || item.status === "completed" ? item.status : "staged";
    const id = text(item.id) || text(item.operation_id);
    const action = text(item.action) || text(item.type);
    return id && action ? [{ id, action, status }] : [];
  });
}

function relayPendingOperationCount(snapshot?: CoreSnapshot): number {
  const domain = record(snapshot?.domains.relay_accounts);
  const state = Object.keys(record(domain.state)).length > 0 ? record(domain.state) : domain;
  return count(state.pending_operation_count ?? domain.pending_operation_count);
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

function statusKey(status: string): string {
  if (status === "signed_in") return "relay.status.signed_in";
  if (status === "signed_out") return "relay.status.signed_out";
  if (status === "expired") return "relay.status.expired";
  return "relay.status.unknown";
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

function FormRow({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.formRow, compactStyles.formRow]}>
    <Text style={styles.formLabel}>{label}</Text>
    <View style={[styles.formValue, compactStyles.formValue]}>{children}</View>
  </View>;
}

function SetupProgress({ step, translate }: { step: AddStep; translate: Translate }): React.JSX.Element {
  const activeIndex = step === "origin" ? 0 : 1;
  const steps = [translate("relay.setupStepStation"), translate("relay.stepSignIn")];
  return <View style={styles.setupProgress} accessibilityLabel={steps.map((label, index) => `${index + 1} ${label}`).join(", ")}>
    {steps.map((label, index) => <View key={label} style={styles.setupProgressStep}>
      <View style={[styles.setupProgressBadge, index === activeIndex && styles.setupProgressBadgeCurrent, index < activeIndex && styles.setupProgressBadgeDone]}>
        <Text style={[styles.setupProgressNumber, index === activeIndex && styles.setupProgressNumberCurrent]}>{index + 1}</Text>
      </View>
      <Text style={[styles.setupProgressLabel, index === activeIndex && styles.setupProgressLabelCurrent]}>{label}</Text>
    </View>)}
  </View>;
}

function ResourceInspector({ account, resource, disabled, nameValue, resourceGroups, selectedResourceGroupLabel, onNameChange, onNameCommit, onGroupChange, onEnabledChange, onCopy, translate }: {
  account: RelayAccount;
  resource: RelayResource;
  disabled: boolean;
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
  const canRevealKey = account.loginStatus === "signed_in" && !disabled;
  const secretTarget = `${account.id}:${resource.id}`;
  const models = visibleResourceModels(resource, showAllModels);
  const modelListIsCollapsible = resource.models.length > INLINE_MODEL_LIMIT;
  useEffect(() => setShowAllModels(false), [resource.id]);
  return <ScrollView style={styles.resourceInspectorScroll} contentContainerStyle={styles.resourceInspectorContent} showsVerticalScrollIndicator={showAllModels} showsHorizontalScrollIndicator={false}>
    {showAllModels ? <NativePersistentScrollIndicator style={styles.resourceInspectorScrollIndicator} /> : null}
    <View style={styles.resourceInspectorHeader}>
      <View style={styles.resourceInspectorHeading}>
        <Text numberOfLines={1} style={styles.resourceInspectorTitle}>{resource.apiName}</Text>
        <Text numberOfLines={1} style={styles.resourceInspectorSubtitle}>{selectedResourceGroupLabel}</Text>
      </View>
    </View>
    <View style={styles.resourceInspectorDivider} />
    <View style={styles.resourceInspectorForm}>
      <View style={styles.resourceInspectorToggleRow}>
        <NativeCheckbox label={resource.enabled ? translate("common.enable") : translate("common.disable")} value={resource.enabled} disabled={disabled} onValueChange={onEnabledChange} />
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("common.name")}</Text>
        <View style={styles.resourceInspectorControlRow}>
          <NativeTextField value={nameValue} placeholder={translate("relay.apiKeyNamePlaceholder")} editable={!disabled} accessibilityLabel={`${translate("relay.apiKeyName")}: ${resource.apiName}`} onChangeText={onNameChange} onBlur={() => { if (!disabled) void onNameCommit(); }} style={styles.resourceInspectorTextInput} />
        </View>
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("relay.apiKeyGroup")}</Text>
        {groupLabels.length > 0 ? <NativePicker labels={groupLabels} selectedValue={selectedResourceGroupLabel} disabled={disabled} onChange={({ nativeEvent }) => { const group = resourceGroups[nativeEvent.index]; if (group) onGroupChange(group.id); }} style={styles.resourceInspectorPicker} /> : <Text style={styles.resourceInspectorReadOnly}>{translate("common.none")}</Text>}
      </View>
      {resource.pendingOperationCount > 0 ? <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("relay.pendingOperations")}</Text>
        <Text style={styles.resourceInspectorReadOnly}>{translate("relay.pendingOperationsCount", { count: resource.pendingOperationCount })}</Text>
      </View> : null}
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
            ? <NativeSecureTextInput domain="relay_accounts" field="api_key" target={secretTarget} label={`${translate("relay.apiKeyValue")}: ${resource.apiName}`} placeholder={resource.keyHint ? translate("relay.resourceKeyConfigured") : translate("common.none")} plainText autoCommit disabled style={styles.resourceInspectorSecureInput} />
            : <NativeTextField value="" placeholder={resource.keyHint ? translate("relay.resourceKeyConfigured") : translate("common.none")} editable={false} accessibilityLabel={`${translate("relay.apiKeyValue")}: ${resource.apiName}`} style={styles.resourceInspectorSecureInput} />}
          <NativeButton title={translate("relay.apiKeyCopy")} symbol="copy" compact disabled={disabled || !resource.keyHint} toolTip={translate("relay.apiKeyCopy")} accessibilityLabel={`${translate("relay.apiKeyCopy")}: ${resource.apiName}`} onPress={onCopy} style={styles.resourceInspectorAction} />
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
  snapshot,
  native,
  busy,
  translate,
  onClose,
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
  snapshot?: CoreSnapshot;
  native: NativeLeafAdapter;
  busy: boolean;
  translate: Translate;
  onClose?: () => void;
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
  const pendingOperations = useMemo(() => pendingOperationsFromSnapshot(snapshot), [snapshot]);
  const pendingOperationCount = relayPendingOperationCount(snapshot) || pendingOperations.filter((operation) => operation.status !== "completed").length;
  const [selectedID, setSelectedID] = useState<string>();
  const [selectedStationID, setSelectedStationID] = useState<string>();
  const [adding, setAdding] = useState(setupOnly);
  const [addStep, setAddStep] = useState<AddStep>("origin");
  const [addStationID, setAddStationID] = useState("__custom__");
  const [origin, setOrigin] = useState("");
  const [addStationName, setAddStationName] = useState("");
  const [stationNameEdited, setStationNameEdited] = useState(false);
  const [typeDetection, setTypeDetection] = useState<RelayTypeDetection>();
  const [manualType, setManualType] = useState<RelayType>();
  const [apiKeyCreateOpen, setApiKeyCreateOpen] = useState(false);
  const [selectedResourceID, setSelectedResourceID] = useState<string>();
  const [rememberPasswordDrafts, setRememberPasswordDrafts] = useState<Record<string, boolean>>({});
  const [apiKeyNameDrafts, setApiKeyNameDrafts] = useState<Record<string, string>>({});
  const [formBusy, setFormBusy] = useState(false);
  const [loginBusy, setLoginBusy] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [resourceBusy, setResourceBusy] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [feedback, setFeedback] = useState<string>();
  const [stationNameDraft, setStationNameDraft] = useState("");
  const [stationOriginDraft, setStationOriginDraft] = useState("");
  const [stationTypeDraft, setStationTypeDraft] = useState<RelayType>();
  const [stationFormBusy, setStationFormBusy] = useState(false);
  const [localRemoval, setLocalRemoval] = useState<LocalRemovalIntent>();
  const [localDependencyPolicy, setLocalDependencyPolicy] = useState<LocalDependencyPolicy>("detach");
  const [remoteKeyDelete, setRemoteKeyDelete] = useState<RemoteKeyDeleteIntent>();
  const [remoteDeletePolicy, setRemoteDeletePolicy] = useState<RemoteDeletePolicy>("detach_disabled");
  const [loginFailureIDs, setLoginFailureIDs] = useState<Set<string>>(() => new Set());
  const rememberPasswordWriteVersion = useRef(new Map<string, number>());
  const accountsRef = useRef(accounts);
  accountsRef.current = accounts;
  const typeDetectionRequest = useRef(0);
  const openedAccountIDs = useRef(new Set<string>());
  const controlsBusy = busy || formBusy || loginBusy || restoreBusy || resourceBusy || cleanupBusy || stationFormBusy;
  const passwordStorageAvailable = true;
  const selected = selectedStationID ? undefined : accounts.find((account) => account.id === selectedID) ?? accounts[0];
  const selectedGroups = selected?.groups ?? [];
  const resourceTableRows = useMemo(() => (selected?.resources ?? []).map((resource) => ({
    key: resource.id,
    cells: [
      resource.apiName,
      resourceGroupName(resource, selectedGroups, translate),
      resourceGroupMultiplier(resource, selectedGroups, translate),
    ],
  })), [selectedGroups, selected?.resources, translate]);
  const resourceSecondaryCellKeys = useMemo(() => (selected?.resources ?? []).flatMap((resource) => !resource.enabled
    ? [`${resource.id}\x1f0`, `${resource.id}\x1f1`, `${resource.id}\x1f2`]
    : []), [selected?.resources]);
  const selectedResource = selected?.resources.find((resource) => resource.id === selectedResourceID) ?? selected?.resources[0];
  const resourceGroups = useMemo(() => {
    if (!selected || !selectedResource) return [];
    const groups = selected.groups.filter((group) => group.id !== "");
    if (selectedResource.groupID && !groups.some((group) => group.id === selectedResource.groupID)) {
      groups.push({ id: selectedResource.groupID, name: selectedResource.groupName || selectedResource.groupID, multiplier: null });
    }
    return groups;
  }, [selected?.groups, selectedResource?.groupID, selectedResource?.groupName]);
  const selectedResourceGroup = resourceGroups.find((group) => group.id === selectedResource?.groupID);
  const selectedResourceGroupLabel = selectedResourceGroup
    ? groupLabel(selectedResourceGroup, translate)
    : selectedResource ? resourceGroupLabel(selectedResource, selected?.groups ?? [], translate) : translate("relay.apiKeyUngrouped");
  const selectedRememberPassword = selected ? rememberPasswordDrafts[selected.id] ?? selected.rememberPassword : false;
  const selectedStation = selectedStationID ? stations.find((station) => station.id === selectedStationID) : undefined;
  const selectedAddStation = stations.find((station) => station.id === addStationID);
  const effectiveLoginStatus = (account: RelayAccount): "signed_in" | "signed_out" | "expired" | "unknown" => {
    if (loginFailureIDs.has(account.id)) return "expired";
    if (account.loginStatus === "signed_in" || account.loginStatus === "signed_out" || account.loginStatus === "expired") return account.loginStatus;
    return "unknown";
  };
  const stationAccounts = (station: RelayStation): RelayAccount[] => station.accountIDs
    .map((id) => accounts.find((account) => account.id === id))
    .filter((account): account is RelayAccount => Boolean(account));
  const relayTableRows = useMemo(() => stations.flatMap((station) => {
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [{
      key: `station:${station.id}`,
      cells: [stationDisplayName(station, translate), ""],
      spanning: true,
    }];
    for (const account of stationAccounts(station)) {
      rows.push({
        key: `account:${account.id}`,
        cells: [accountDisplayName(account, translate), balanceLabel(account, translate)],
      });
    }
    return rows;
  }), [stations, accounts, translate]);
  const relayTableSelection = selectedStationID ? `station:${selectedStationID}` : selected?.id ? `account:${selected.id}` : "";
  useEffect(() => {
    if (selectedStationID && !stations.some((station) => station.id === selectedStationID)) {
      setSelectedStationID(undefined);
      return;
    }
    if (!selectedStationID || !selectedStation) return;
    setStationNameDraft(stationDisplayName(selectedStation, translate));
    setStationOriginDraft(selectedStation.origin);
    setStationTypeDraft(selectedStation.type);
  }, [selectedStationID, selectedStation?.id, selectedStation?.name, selectedStation?.origin, selectedStation?.type]);
  useEffect(() => {
    const route = setupOnly ? "relay-add" : "relay-accounts";
    const showingLoginStep = setupOnly && adding && addStep === "sign-in";
    const width = setupOnly ? (showingLoginStep ? 900 : 620) : 820;
    const height = setupOnly
      ? (showingLoginStep ? 760 : 350)
      : Math.max(440, 170 + relayTableRows.length * 22);
    void native.window.setContentSize?.(route, width, height);
  }, [addStep, adding, native.window, relayTableRows.length, setupOnly]);
  useEffect(() => {
    if (!selected) {
      setApiKeyNameDrafts({});
      setSelectedResourceID(undefined);
      return;
    }
    setApiKeyNameDrafts((current) => {
      const next: Record<string, string> = {};
      for (const resource of selected.resources) next[resource.id] = current[resource.id] ?? resource.name;
      return next;
    });
    setSelectedResourceID((current) => selected.resources.some((resource) => resource.id === current) ? current : selected.resources[0]?.id);
  }, [selected?.id, selected?.resources]);
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
  const resetForm = (): void => {
    typeDetectionRequest.current += 1;
    setAdding(setupOnly);
    setAddStep("origin");
    setAddStationID("__custom__");
    setOrigin("");
    setAddStationName("");
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
    setRestoreBusy(true);
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
        return "unavailable";
      }
      await refreshAccounts();
      const signedIn = result.loginStatus === "signed_in";
      markLoginFailure(account.id, !signedIn);
      return signedIn ? "signed_in" : "expired";
    } catch {
      markLoginFailure(account.id, false);
      return "unavailable";
    } finally {
      setRestoreBusy(false);
    }
  };
  const beginLogin = async (): Promise<void> => {
    const candidate = normalizeRelayOrigin(origin);
    if (!candidate) return;
    setFormBusy(true);
    setFeedback(undefined);
    let account: AddedRelayAccount | undefined;
    try {
      const detected = await detectRelayType();
    const accountType = detected ?? manualType ?? detectedAddType;
      if (!accountType) {
        // A white-label site can block the public detection probes while
        // still presenting a normal sign-in page. Require an explicit family
        // choice before opening the native browser.
        setFeedback(translate("relay.typeNotDetected"));
        return;
      }
      const chosenStation = selectedAddStation;
      account = await addAccount(accountType, candidate, false, chosenStation ? {
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
      const loggedIn = await loginAccount(account, setupOnly);
      if (setupOnly && loggedIn) {
        onClose?.();
        native.window.focus("relay-accounts");
      } else if (!loggedIn) {
        // A cancelled or failed setup must not leave an empty account row.
        await deleteAccount(account);
      }
    } catch {
      if (account) await deleteAccount(account);
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
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
    await clearRemovedAccountCredentials([account.id]);
    setCleanupBusy(false);
  };
  const openLocalRemoval = (): void => {
    const intent = selectedStationID && selectedStation
      ? { kind: "station" as const, station: selectedStation }
      : selected
        ? { kind: "account" as const, account: selected }
        : undefined;
    if (!intent) return;
    setLocalDependencyPolicy("detach");
    setLocalRemoval(intent);
  };
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
    await clearRemovedAccountCredentials(localRemoval.station.accountIDs);
    setCleanupBusy(false);
    setLocalRemoval(undefined);
  };
  const loginAccount = async (account: AddedRelayAccount, embedded = false): Promise<boolean> => {
    setLoginBusy(true);
    setFeedback(translate("relay.loginWorking"));
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
      setFeedback(translate("relay.operationFailed"));
      return false;
    } finally {
      setLoginBusy(false);
    }
    if (!result) {
      markLoginFailure(account.id, true);
      setFeedback(translate("relay.loginNotCompleted"));
      return false;
    }
    markLoginFailure(account.id, false);
    if (embedded) return true;
    let resourceStatus: "ready" | "unavailable" = "unavailable";
    try {
      resourceStatus = await refreshResources(account.id);
    } catch {
      // Keep the verified session even when resource discovery fails.
    }
    try {
      await refreshAccounts();
    } catch {
      // Core already accepted the login; a stale snapshot must not discard it.
    }
    setSelectedID(account.id);
    setFeedback(translate(resourceStatus === "ready" ? "relay.loginComplete" : "relay.loginResourcesUnavailable"));
    return true;
  };
  const refreshAccountResources = async (account: RelayAccount): Promise<void> => {
    const hasKnownResources = account.resources.length > 0;
    setResourceBusy(true);
    setFeedback(undefined);
    try {
      const status = await refreshResources(account.id);
      await refreshAccounts();
      setSelectedID(account.id);
      setFeedback(status === "ready" || hasKnownResources ? undefined : translate("relay.resourcesUnavailable"));
    } catch {
      setFeedback(hasKnownResources ? undefined : translate("relay.resourcesUnavailable"));
    } finally {
      setResourceBusy(false);
    }
  };
  const refreshLoginState = async (account: RelayAccount, automatic = false): Promise<void> => {
    const restored = await restoreSavedSession(account);
    if (restored === "signed_in") {
      await refreshAccountResources(account);
      return;
    }
    const canAutoLogin = account.rememberPassword && account.passwordSaved && Boolean(account.username.trim());
    if (!automatic || canAutoLogin) await loginAccount(account);
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
    if (kind === "create" && (!apiKeyActions?.create || !value || typeof value === "boolean" || typeof value === "string")) return;
    if (kind === "update" && !apiKeyActions?.update) return;
    if (kind === "setEnabled" && (!apiKeyActions?.setEnabled || typeof value !== "boolean")) return;
    if (kind === "setGroup" && (!apiKeyActions?.setGroup || typeof value !== "string")) return;
    setFormBusy(true);
    setFeedback(undefined);
    try {
      if (kind === "create") {
        await apiKeyActions?.create?.(selected.id, value as { name: string; groupID?: string; enabled: boolean });
      }
      else if (kind === "update") {
        const resource = selected.resources.find((item) => item.id === resourceID);
        const name = (apiKeyNameDrafts[resourceID as string] ?? resource?.name ?? "").trim();
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
      setFeedback(translate(feedbackKey));
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const openRemoteKeyDelete = (account: RelayAccount, resource: RelayResource): void => {
    setRemoteDeletePolicy("detach_disabled");
    setRemoteKeyDelete({ account, resource });
  };
  const executeRemoteKeyDelete = async (): Promise<void> => {
    if (!remoteKeyDelete) return;
    const { account, resource } = remoteKeyDelete;
    setFormBusy(true);
    setFeedback(undefined);
    try {
      if (remoteDeletePolicy === "detach_only") await apiKeyActions?.detach?.(account.id, resource.id);
      else await apiKeyActions?.remove?.(account.id, resource.id, remoteDeletePolicy);
      await refreshAccounts();
      if (resource.id === selectedResourceID) setSelectedResourceID(undefined);
      setFeedback(translate(remoteDeletePolicy === "detach_only" ? "relay.apiKeyDetachStaged" : "relay.apiKeyDeleteStaged"));
      setRemoteKeyDelete(undefined);
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const copyApiKey = async (resource: RelayResource): Promise<void> => {
    if (!selected) return;
    setFeedback(undefined);
    try {
      const copied = await native.copySecret({
        domain: "relay_accounts",
        field: "api_key",
        target: `${selected.id}:${resource.id}`,
      });
      setFeedback(translate(copied ? "relay.apiKeyCopied" : "relay.operationFailed"));
    } catch {
      setFeedback(translate("relay.operationFailed"));
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
  const stageStationUpdate = async (overrides: { name?: string; origin?: string; type?: RelayType } = {}): Promise<void> => {
    if (!selectedStation) return;
    const name = (overrides.name ?? stationNameDraft).trim();
    const origin = normalizeRelayOrigin(overrides.origin ?? stationOriginDraft);
    const type = overrides.type ?? stationTypeDraft ?? selectedStation.type;
    if (!name || !origin) return;
    const dirty = name !== stationDisplayName(selectedStation, translate).trim()
      || origin !== normalizeRelayOrigin(selectedStation.origin)
      || type !== selectedStation.type;
    if (!dirty) return;
    setStationFormBusy(true);
    setFeedback(undefined);
    try {
      await commit("station.update", {
        id: selectedStation.id,
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
    setSelectedID(accountID);
    setSelectedStationID(undefined);
    setAdding(false);
    setSelectedResourceID(undefined);
    setFeedback(undefined);
  };
  const selectStation = (stationID: string): void => {
    const station = stations.find((item) => item.id === stationID);
    setSelectedID(undefined);
    setSelectedStationID(stationID);
    if (station) {
      setStationNameDraft(stationDisplayName(station, translate));
      setStationOriginDraft(station.origin);
      setStationTypeDraft(station.type);
    }
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
  const addStationPickerLabels = [translate("relay.stationCustom"), ...stations.map((station) => stationPickerLabel(station, translate))];
  const addStationPickerValue = selectedAddStation ? stationPickerLabel(selectedAddStation, translate) : translate("relay.stationCustom");
  const selectAddStation = (index: number): void => {
    const station = stations[index - 1];
    typeDetectionRequest.current += 1;
    if (!station) {
      setAddStationID("__custom__");
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
    ? translate("relay.removeStationBody", { label: stationDisplayName(localRemoval.station, translate), accounts: localRemoval.station.accountIDs.length, keys: localResourceCount, models: localRemoval.station.linkedModelCount })
    : localRemoval?.kind === "account"
      ? translate("relay.removeAccountBody", { label: accountDisplayName(localRemoval.account, translate), keys: localResourceCount, models: localRemoval.account.linkedModelCount })
      : "";
  const selectedStationAccounts = selectedStation ? stationAccounts(selectedStation) : [];
  const selectedStationResourceCount = selectedStationAccounts.reduce((total, account) => total + account.resources.length, 0);
  const selectedStationModelCount = selectedStation?.linkedModelCount
    || selectedStationAccounts.reduce((total, account) => total + account.linkedModelCount, 0);
  return <View style={styles.workspace} accessibilityLabel={translate("relay.title")}>
    <View style={styles.relayLayout}>
      {!setupOnly ? <RelayTablePane title={translate("relay.accountsHeader")} style={styles.sidebar} actions={<>
        <NativeButton title={translate("relay.addAccount")} symbol="plus" toolTip={translate("relay.addAccount")} accessibilityLabel={translate("relay.addAccount")} compact disabled={adding} onPress={beginAdding} style={styles.sidebarAddButton} />
        <NativeButton title={translate("relay.removeLocal")} symbol="minus" toolTip={translate("relay.removeLocal")} accessibilityLabel={translate("relay.removeLocal")} destructive compact disabled={(!selected && !selectedStation) || adding} onPress={openLocalRemoval} style={styles.sidebarIconButton} />
      </>}>
        {stations.length > 0 ? <View style={styles.sidebarTableFrame}><NativeTable
          columns={[{ label: translate("relay.accounts"), width: 118 }, { label: translate("relay.balance"), width: 78 }]}
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
          <View style={[styles.detailContent, styles.fixedDetailPane, compactStyles.detailContent, setupOnly && styles.setupContent, controlsBusy && styles.loadingSurface]}>
            <View style={setupOnly ? styles.setupSurface : undefined}>
              <SetupProgress step="sign-in" translate={translate} />
              <View style={[styles.detailHeader, setupOnly && styles.setupHeader]}>
                <Text style={styles.detailTitle}>{translate("relay.stepSignIn")}</Text>
                <Text style={styles.detailSubtitle}>{translate("relay.stepSignInDetail")}</Text>
              </View>
              <View style={styles.signInWaiting}><Text style={styles.formHint}>{translate("relay.loginWorking")}</Text></View>
            </View>
          </View>
          <View style={[styles.bottomBar, compactStyles.bottomBar, setupOnly && styles.setupBottomBar]}><Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.bottomStatus}>{feedback ?? translate("relay.loginWorking")}</Text></View>
        </View> : <View style={styles.detailWorkspace}>
          <View style={[styles.detailContent, styles.fixedDetailPane, compactStyles.detailContent, setupOnly && styles.setupContent, controlsBusy && styles.loadingSurface]}>
            <View style={setupOnly ? styles.setupSurface : undefined}>
              {setupOnly ? <SetupProgress step="origin" translate={translate} /> : null}
              <View style={[styles.detailHeader, setupOnly && styles.setupHeader]}>
                <Text style={styles.detailTitle}>{translate("relay.addAccount")}</Text>
              </View>
              <View style={[styles.formSection, setupOnly && styles.setupFormSection]}>
                {!setupOnly && stations.length > 0 ? <FormRow label={translate("relay.station")}><NativePicker labels={addStationPickerLabels} selectedValue={addStationPickerValue} disabled={controlsBusy} onChange={({ nativeEvent }) => selectAddStation(nativeEvent.index)} style={styles.typeSelector} /></FormRow> : null}
                <FormRow label={translate("relay.origin")}><NativeTextField value={origin} placeholder={translate("relay.originPlaceholder")} editable={!controlsBusy && !selectedAddStation} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={updateAddOrigin} style={[styles.control, compactStyles.control]} /></FormRow>
                <FormRow label={translate("relay.stationName")}><NativeTextField value={addStationName} placeholder={translate("relay.stationNamePlaceholder")} editable={!controlsBusy && !selectedAddStation} accessibilityLabel={translate("relay.stationName")} onChangeText={updateAddStationName} style={[styles.control, compactStyles.control]} /></FormRow>
                <FormRow label={translate("relay.type")}><NativePicker labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={selectedAddType ? relayTypeLabel(selectedAddType, translate) : ""} disabled={controlsBusy || Boolean(selectedAddStation)} onChange={({ nativeEvent }) => { setManualType(nativeEvent.index === 1 ? "sub2api" : "newapi"); }} style={styles.typeSelector} /></FormRow>
              </View>
            </View>
          </View>
          <View style={[styles.bottomBar, compactStyles.bottomBar, setupOnly && styles.setupBottomBar]}>
            {feedback || !setupOnly ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.bottomStatus}>{feedback ?? translate("relay.stepSignInDetail")}</Text> : null}
            <View style={[styles.bottomActions, setupOnly && styles.setupBottomActions]}>{setupOnly && onClose ? <NativeButton title={translate("menu.close")} onPress={onClose} /> : !setupOnly ? <NativeButton title={translate("menu.cancel")} disabled={controlsBusy} onPress={resetForm} /> : null}<NativeButton title={translate("relay.next")} primary disabled={controlsBusy || !origin.trim() || !addStationName.trim() || !selectedAddType} onPress={() => { void beginLogin(); }} /></View>
          </View>
        </View> : selectedStation ? <View style={styles.detailWorkspace}>
          <View style={[styles.stationSimpleForm, controlsBusy && styles.loadingSurface]}>
            <View style={[styles.stationSettingsForm, compactStyles.stationSettingsForm]}>
              <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow]}>
                <Text style={styles.stationSettingsLabel}>{translate("relay.stationName")}</Text>
                <NativeTextField value={stationNameDraft} editable={!controlsBusy} accessibilityLabel={translate("relay.stationName")} onChangeText={setStationNameDraft} onBlur={() => { if (!controlsBusy) void stageStationUpdate(); }} style={[styles.stationSettingsControl, compactStyles.control]} />
              </View>
              <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow]}>
                <Text style={styles.stationSettingsLabel}>{translate("relay.origin")}</Text>
                <NativeTextField value={stationOriginDraft} editable={!controlsBusy} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={setStationOriginDraft} onBlur={() => { if (!controlsBusy) void stageStationUpdate(); }} style={[styles.stationSettingsControl, compactStyles.control]} />
              </View>
              <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow, styles.stationSettingsLastRow]}>
                <Text style={styles.stationSettingsLabel}>{translate("relay.type")}</Text>
                <NativePicker labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={stationTypeDraft ? relayTypeLabel(stationTypeDraft, translate) : ""} disabled={controlsBusy} onChange={({ nativeEvent }) => { const nextType = nativeEvent.index === 1 ? "sub2api" : "newapi"; setStationTypeDraft(nextType); void stageStationUpdate({ type: nextType }); }} style={[styles.stationSettingsControl, compactStyles.control]} />
              </View>
            </View>
            {feedback ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.stationSettingsFeedback}>{feedback}</Text> : null}
          </View>
        </View> : !setupOnly && selected ? <View style={styles.detailWorkspace}>
          <View style={[styles.accountDetailContent, controlsBusy && styles.loadingSurface]}>
            <View style={styles.accountHeader}>
              <View style={styles.accountIdentity}>
                <Text numberOfLines={1} style={styles.detailTitle}>{accountDetailTitle(selected, translate)}</Text>
                <Text style={styles.accountHeaderSeparator}>·</Text>
                <Text numberOfLines={1} style={styles.detailSubtitle}>{accountStationLabel(selected)}</Text>
              </View>
              <View style={styles.accountFacts}>
                <Text style={styles.accountHeaderSeparator}>·</Text>
                <Text accessibilityLabel={`${translate("relay.type")}: ${relayTypeLabel(selected.type, translate)}`} numberOfLines={1} style={styles.accountFactValue}>{relayTypeLabel(selected.type, translate)}</Text>
                <Text style={styles.accountHeaderSeparator}>·</Text>
                <Text accessibilityLabel={`${translate("relay.balance")}: ${balanceLabel(selected, translate)}`} selectable numberOfLines={1} style={styles.accountFactValue}>{balanceLabel(selected, translate)}</Text>
                {selected.pendingOperationCount > 0 ? <><Text style={styles.accountHeaderSeparator}>·</Text><Text numberOfLines={1} style={styles.accountPendingOperations}>{translate("relay.pendingOperationsCount", { count: selected.pendingOperationCount })}</Text></> : null}
                {passwordStorageAvailable ? <NativeCheckbox label={translate("relay.rememberPassword")} value={selectedRememberPassword} disabled={controlsBusy} onValueChange={(next) => { void updateRememberPassword(next); }} style={styles.accountRememberPassword} /> : <Text style={styles.formHint}>{translate("relay.passwordNotSaved")}</Text>}
              </View>
              <View style={styles.accountHeaderActions}>
                <Text style={styles.accountHeaderSeparator}>·</Text>
                <View style={styles.statusLine}><View style={[styles.statusDot, controlsBusy ? styles.statusDotLoading : effectiveLoginStatus(selected) === "signed_in" ? styles.statusDotOnline : styles.statusDotExpired]} /><Text style={styles.statusText}>{translate(controlsBusy ? "relay.status.loading" : statusKey(effectiveLoginStatus(selected)))}</Text></View>
                <NativeButton title={translate("common.refresh")} symbol="refresh" compact disabled={controlsBusy} toolTip={translate("common.refresh")} accessibilityLabel={translate("common.refresh")} onPress={() => { void refreshLoginState(selected); }} style={styles.accountRefreshButton} />
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
                          <View style={styles.resourceToolbarCrud}>
                            <NativeButton title={translate("relay.apiKeyCreate")} symbol="plus" compact disabled={controlsBusy || !apiKeyActions?.create} toolTip={translate("relay.apiKeyCreate")} accessibilityLabel={translate("relay.apiKeyCreate")} onPress={() => setApiKeyCreateOpen(true)} style={styles.resourceToolbarCrudButton} />
                            <NativeButton title={translate("relay.apiKeyDelete")} symbol="minus" compact destructive disabled={controlsBusy || (!apiKeyActions?.remove && !apiKeyActions?.detach) || !selectedResource} toolTip={translate("relay.apiKeyDelete")} accessibilityLabel={selectedResource ? `${translate("relay.apiKeyDelete")}: ${selectedResource.apiName}` : translate("relay.apiKeyDelete")} onPress={() => { if (selectedResource) openRemoteKeyDelete(selected, selectedResource); }} style={styles.resourceToolbarCrudButton} />
                          </View>
                        </View>
                      </View>
                      {feedback ? <Text accessibilityLiveRegion="polite" style={styles.resourcesFeedback}>{feedback}</Text> : null}
                      <NativeTable
                        columns={[{ label: translate("common.name"), width: 116 }, { label: translate("relay.apiKeyGroup"), width: 124 }, { label: translate("relay.apiKeyMultiplier"), width: 64 }]}
                        rows={resourceTableRows}
                        selectedKey={selectedResourceID ?? ""}
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
                    <View style={styles.resourceInspectorPane}>{selectedResource ? <ResourceInspector
                      account={selected}
                      resource={selectedResource}
                      disabled={controlsBusy}
                      nameValue={apiKeyNameDrafts[selectedResource.id] ?? selectedResource.name}
                      resourceGroups={resourceGroups}
                      selectedResourceGroupLabel={selectedResourceGroupLabel}
                      onNameChange={(value) => setApiKeyNameDrafts((current) => ({ ...current, [selectedResource.id]: value }))}
                      onNameCommit={() => runApiKeyAction("update", selectedResource.id)}
                      onGroupChange={(groupID) => { void runApiKeyAction("setGroup", selectedResource.id, groupID); }}
                      onEnabledChange={(enabled) => { void runApiKeyAction("setEnabled", selectedResource.id, enabled); }}
                      onCopy={() => { void copyApiKey(selectedResource); }}
                      translate={translate}
                    /> : <View style={styles.resourceEmpty}><Text style={styles.resourceEmptyTitle}>{translate(selected.resourceError === "no_api_keys" && !resourceBusy && !restoreBusy ? "relay.resourcesEmptyTitle" : "relay.resources")}</Text><Text style={styles.resourceEmptyText}>{resourceBusy || restoreBusy ? translate("relay.resourcesChecking") : resourceHint(selected, translate)}</Text></View>}</View>
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
    <DependencyPolicyDialog visible={Boolean(remoteKeyDelete)} title={translate("relay.apiKeyDeleteImpactTitle")} message={remoteKeyDelete ? translate("relay.apiKeyDeleteImpactBody", { count: remoteKeyDelete.resource.linkedModelCount, label: remoteKeyDelete.resource.apiName }) : ""} options={remotePolicyOptions} value={remoteDeletePolicy} disabled={controlsBusy} confirmLabel={remoteDeletePolicy === "detach_only" ? translate("screen.confirm") : translate("common.delete")} onValueChange={setRemoteDeletePolicy} onClose={() => setRemoteKeyDelete(undefined)} onConfirm={() => { void executeRemoteKeyDelete(); }} translate={translate} />
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
  resourceToolbar: { minHeight: 32, gap: 8 },
  bottomBar: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 6, gap: 6 },
});

const styles = StyleSheet.create({
  hidden: { display: "none" },
  workspace: { flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden", backgroundColor: colors.window },
  relayLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 0 },
  setupDetail: { width: "100%" },
  tablePane: { minWidth: 0, minHeight: 0, gap: 4 },
  tableTitleRow: { height: 30, minHeight: 30, paddingHorizontal: 10, flexDirection: "row", alignItems: "center" },
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
  detailScroll: { flex: 1, minWidth: 0 },
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
  stationDetailScroll: { flex: 1, minWidth: 0 },
  stationDetailContent: { flexGrow: 1, minWidth: 0, paddingTop: 8, paddingHorizontal: 12, paddingRight: 12, paddingBottom: 12, gap: 8 },
  stationSimpleForm: { flex: 1, minWidth: 0, paddingHorizontal: 18, paddingTop: 14, paddingBottom: 14, gap: 12 },
  stationSimpleHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  stationOverviewCard: { width: "100%", minWidth: 0, padding: 12, gap: 10, borderWidth: 1, borderColor: colors.separator, borderRadius: 7, backgroundColor: colors.panel },
  stationOverviewHeading: { minWidth: 0, flexDirection: "row", alignItems: "flex-start", justifyContent: "space-between", gap: 10 },
  stationConnectionPill: { flexShrink: 0, minHeight: 22, paddingHorizontal: 8, borderRadius: 11, flexDirection: "row", alignItems: "center", gap: 5, backgroundColor: colors.window },
  stationConnectionText: { color: colors.secondary, fontSize: UI_FONT_SIZE },
  stationMetricGrid: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  stationMetric: { flexGrow: 1, flexBasis: 110, minWidth: 96, paddingHorizontal: 9, paddingVertical: 7, borderRadius: 6, backgroundColor: colors.window },
  stationMetricValue: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 21, fontWeight: "700" },
  stationMetricLabel: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  stationDetailColumns: { width: "100%", minWidth: 0, flexDirection: "row", flexWrap: "wrap", gap: 8 },
  stationCard: { minWidth: 0, padding: 10, gap: 8, borderWidth: 1, borderColor: colors.separator, borderRadius: 7, backgroundColor: colors.window },
  stationAccountsCard: { flexGrow: 1, flexBasis: 300 },
  stationSettingsCard: { flexGrow: 1, flexBasis: 330 },
  stationCardHeader: { minWidth: 0, minHeight: 28, flexDirection: "row", alignItems: "flex-start", justifyContent: "space-between", gap: 8 },
  stationCardTitle: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  stationCardHint: { maxWidth: 420, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  stationCardAction: { width: 22, minWidth: 22, height: 22 },
  stationAccountList: { minWidth: 0, gap: 4 },
  stationAccountRow: { minHeight: 38, width: "100%" },
  stationAccountsEmpty: { minHeight: 78, paddingHorizontal: 12, paddingVertical: 10, alignItems: "center", justifyContent: "center", gap: 3, borderWidth: 1, borderStyle: "dashed", borderColor: colors.separator, borderRadius: 6, backgroundColor: colors.panel },
  stationEmptyTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  stationEmptyText: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15, textAlign: "center" },
  stationSettings: { width: "100%", minWidth: 0, gap: 6 },
  stationSettingsTitle: { color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  stationSettingsForm: { width: "100%", gap: 4, paddingTop: 3, borderTopWidth: 1, borderTopColor: colors.separator },
  stationSettingsRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  stationSettingsLastRow: {},
  stationSettingsLabel: { width: 72, flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  stationSettingsControl: { flex: 1, minWidth: 160, height: 26 },
  stationSettingsFeedback: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  stationPendingNotice: { minHeight: 26, paddingHorizontal: 8, paddingVertical: 5, borderRadius: 5, backgroundColor: colors.panel },
  stationPendingNoticeText: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  accountHeader: { minWidth: 0, minHeight: 38, paddingHorizontal: 12, paddingVertical: 5, flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 12, rowGap: 4, backgroundColor: colors.window },
  accountIdentity: { flexGrow: 0, flexShrink: 1, flexBasis: "auto", minWidth: 150, flexDirection: "row", alignItems: "baseline", gap: 5 },
  accountFacts: { flexShrink: 1, minWidth: 0, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 6 },
  accountFactValue: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "500" },
  accountHeaderSeparator: { flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  accountPendingOperations: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  accountRememberPassword: { minWidth: 78 },
  accountHeaderActions: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 6 },
  detailHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  detailHeading: { flexGrow: 1, flexShrink: 1, minWidth: 0, flexDirection: "row", alignItems: "baseline", gap: 5 },
  detailTitle: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  detailSubtitle: { flexShrink: 1, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  statusLine: { minHeight: 22, flexDirection: "row", alignItems: "center", gap: 5 },
  statusText: { color: colors.secondary, fontSize: UI_FONT_SIZE },
  statusDot: { width: 7, height: 7, borderRadius: 4 },
  statusDotOnline: { backgroundColor: colors.success },
  statusDotExpired: { backgroundColor: colors.warning },
  statusDotLoading: { backgroundColor: colors.warning },
  accountRefreshButton: { width: 22, minWidth: 22, height: 22 },
  control: { width: "100%", minWidth: 0, height: 32 },
  typeSelector: { width: "100%", minWidth: 0, maxWidth: 520, alignSelf: "stretch" },
  formSection: { width: "100%", maxWidth: 720, minWidth: 0, paddingVertical: 0, gap: 8, backgroundColor: colors.window },
  formRow: { width: "100%", minHeight: 34, flexDirection: "column", alignItems: "stretch", gap: 6 },
  checkboxFormRow: { width: "100%", minHeight: 24, justifyContent: "center" },
  formLabel: { width: "100%", minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  formValue: { width: "100%", minWidth: 0, minHeight: 30, justifyContent: "center", gap: 4 },
  formHint: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 17, paddingVertical: 5 },
  signInWaiting: { minHeight: 160, alignItems: "center", justifyContent: "center", paddingHorizontal: 20 },
  readOnlyValue: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  resourcesSection: { flex: 1, minWidth: 0, minHeight: 0, borderTopWidth: 1, borderTopColor: colors.separator, paddingTop: 4 },
  resourcePane: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  resourceToolbar: { minHeight: 32, paddingHorizontal: 12, paddingVertical: 3, flexDirection: "row", alignItems: "center", gap: 8 },
  resourceToolbarHeading: { flex: 1, minWidth: 110, flexDirection: "row", alignItems: "center", gap: 6 },
  resourceToolbarTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  resourceToolbarCrud: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 },
  resourceToolbarCrudButton: { width: 22, minWidth: 22, height: 22 },
  resourcesFeedback: { paddingHorizontal: 12, paddingVertical: 4, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18, backgroundColor: colors.window },
  resourceBody: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  resourceColumns: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 0 },
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
  resourceListCellDisabled: { color: colors.secondary },
  resourceInspectorPane: { width: 266, minWidth: 244, maxWidth: 266, flexGrow: 0, flexShrink: 0, minHeight: 0 },
  resourceInspectorScroll: { flex: 1, minWidth: 0, backgroundColor: colors.window },
  resourceInspectorScrollIndicator: { position: "absolute", width: 0, height: 0 },
  resourceInspectorContent: { flexGrow: 1, minWidth: 0, paddingTop: 6, paddingHorizontal: 12, paddingRight: 12, paddingBottom: 12, gap: 8 },
  resourceInspectorHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  resourceInspectorHeading: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "baseline", gap: 5 },
  resourceInspectorTitle: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  resourceInspectorSubtitle: { flexShrink: 1, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 15 },
  resourceInspectorDivider: { height: 1, backgroundColor: colors.separator },
  resourceInspectorForm: { minWidth: 0, gap: 6 },
  resourceInspectorToggleRow: { minHeight: 24, flexDirection: "row", alignItems: "center" },
  resourceInspectorRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 0 },
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
  pendingOperationBar: { minHeight: 30, paddingHorizontal: 12, paddingVertical: 5, flexDirection: "row", alignItems: "center", gap: 8, borderBottomWidth: 1, borderBottomColor: colors.separator, backgroundColor: colors.panel },
  pendingOperationTitle: { flexShrink: 0, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  pendingOperationText: { flex: 1, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  loadingSurface: { opacity: 0.55 },
  empty: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 19, textAlign: "center" },
  blank: { flex: 1, minHeight: 240, alignItems: "center", justifyContent: "center", gap: 14, paddingHorizontal: 28, backgroundColor: colors.window },
});
