import React, { useEffect, useMemo, useRef, useState } from "react";
import { Platform, PlatformColor, ScrollView, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";
import type { CoreSnapshot, NativeLeafAdapter } from "../types";
import { NativeButton, NativeCheckbox, NativePicker, NativeSecureTextInput, NativeTable, NativeTextField } from "./NativeControls";
import { normalizeRelayOrigin, suggestedRelayStationName } from "./relayOrigin";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

export { normalizeRelayOrigin } from "./relayOrigin";

type UnknownRecord = Record<string, unknown>;
type Translate = (key: string, values?: Record<string, string | number>) => string;
type RelayType = "newapi" | "sub2api";
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
};

type AddedRelayAccount = Pick<RelayAccount, "id" | "type" | "label" | "origin" | "username" | "rememberPassword">;
export type AddAccountOptions = {
  stationID?: string;
  stationOrigin?: string;
  stationName?: string;
  stationType?: RelayType;
};

/**
 * API-key writes stay behind host/Core callbacks. The ordinary snapshot and
 * React state contain masked metadata only; Core performs the authenticated
 * remote mutation without returning a generated key value through IPC.
 */
export type RelayApiKeyActions = {
  create?: (accountID: string) => Promise<void>;
  update?: (accountID: string, resourceID: string, name: string) => Promise<void>;
  setEnabled?: (accountID: string, resourceID: string, enabled: boolean) => Promise<void>;
  setGroup?: (accountID: string, resourceID: string, groupID: string) => Promise<void>;
  remove?: (accountID: string, resourceID: string) => Promise<void>;
};

type PendingCredentialCleanup = {
  accountID: string;
  label: string;
  kind: "credentials";
};

type RelayTypeDetection = "checking" | RelayType | "unknown";
type SavedSessionRestore = "signed_in" | "expired" | "unavailable";
type AddStep = "origin" | "sign-in";

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function groupMultiplier(value: unknown): number | null {
  const multiplier = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(multiplier) && multiplier >= 0 ? multiplier : null;
}

function groupLabel(group: RelayGroup): string {
  if (group.multiplier === null) return group.name;
  return `${group.name} / ${Number.isInteger(group.multiplier) ? group.multiplier : group.multiplier.toString()}x`;
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

function stationPickerLabel(station: RelayStation): string {
  const name = stationDisplayName(station, (key) => key);
  if (!station.origin || stationOriginKey(name) === stationOriginKey(station.origin)) return name || station.origin;
  return `${name} — ${station.origin}`;
}

function resourceModelsSummary(resource: RelayResource, translate: Translate): string {
  if (resource.models.length === 0) return translate("common.none");
  if (resource.models.length <= 2) return resource.models.join(", ");
  return `${resource.models.slice(0, 2).join(", ")} +${resource.models.length - 2}`;
}

function FormRow({ label, children }: { label: string; children: React.ReactNode }): React.JSX.Element {
  return <View style={[styles.formRow, compactStyles.formRow]}>
    <Text style={styles.formLabel}>{label}</Text>
    <View style={[styles.formValue, compactStyles.formValue]}>{children}</View>
  </View>;
}

function SetupProgress({ step, translate }: { step: AddStep; translate: Translate }): React.JSX.Element {
  const activeIndex = step === "origin" ? 0 : 1;
  const steps = [translate("relay.origin"), translate("relay.stepSignIn")];
  return <View style={styles.setupProgress} accessibilityLabel={steps.map((label, index) => `${index + 1} ${label}`).join(", ")}>
    {steps.map((label, index) => <React.Fragment key={label}>
      {index > 0 ? <View style={[styles.setupProgressConnector, index <= activeIndex && styles.setupProgressConnectorActive]} /> : null}
      <View style={styles.setupProgressStep}>
        <View style={[styles.setupProgressBadge, index === activeIndex && styles.setupProgressBadgeCurrent, index < activeIndex && styles.setupProgressBadgeDone]}>
          <Text style={[styles.setupProgressNumber, index === activeIndex && styles.setupProgressNumberCurrent]}>{index + 1}</Text>
        </View>
        <Text style={[styles.setupProgressLabel, index === activeIndex && styles.setupProgressLabelCurrent]}>{label}</Text>
      </View>
    </React.Fragment>)}
  </View>;
}

function ResourceInspector({ account, resource, disabled, nameValue, resourceGroups, selectedResourceGroupLabel, selectedForImport, onNameChange, onSaveName, onGroupChange, onEnabledChange, onImportChange, onCopy, translate }: {
  account: RelayAccount;
  resource: RelayResource;
  disabled: boolean;
  nameValue: string;
  resourceGroups: RelayGroup[];
  selectedResourceGroupLabel: string;
  selectedForImport: boolean;
  onNameChange: (value: string) => void;
  onSaveName: () => void;
  onGroupChange: (groupID: string) => void;
  onEnabledChange: (enabled: boolean) => void;
  onImportChange: (selected: boolean) => void;
  onCopy: () => void;
  translate: Translate;
}): React.JSX.Element {
  const groupLabels = resourceGroups.map(groupLabel);
  const canRevealKey = account.loginStatus === "signed_in" && !disabled;
  const secretTarget = `${account.id}:${resource.id}`;
  return <ScrollView style={styles.resourceInspectorScroll} contentContainerStyle={styles.resourceInspectorContent}>
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
        <NativeCheckbox label={translate("relay.apiKeyImport")} value={selectedForImport} disabled={disabled || !resource.enabled} onValueChange={onImportChange} />
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("common.name")}</Text>
        <View style={styles.resourceInspectorControlRow}>
          <NativeTextField value={nameValue} placeholder={translate("relay.apiKeyNamePlaceholder")} editable={!disabled} accessibilityLabel={`${translate("relay.apiKeyName")}: ${resource.apiName}`} onChangeText={onNameChange} style={styles.resourceInspectorTextInput} />
          <NativeButton title={translate("common.save")} symbol="check" compact primary disabled={disabled || !nameValue.trim() || nameValue.trim() === resource.name} toolTip={translate("common.save")} accessibilityLabel={translate("common.save")} onPress={onSaveName} style={styles.resourceInspectorAction} />
        </View>
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("relay.apiKeyGroup")}</Text>
        {groupLabels.length > 0 ? <NativePicker labels={groupLabels} selectedValue={selectedResourceGroupLabel} disabled={disabled} onChange={({ nativeEvent }) => { const group = resourceGroups[nativeEvent.index]; if (group) onGroupChange(group.id); }} style={styles.resourceInspectorPicker} /> : <Text style={styles.resourceInspectorReadOnly}>{translate("common.none")}</Text>}
      </View>
      <View style={styles.resourceInspectorRow}>
        <Text style={styles.resourceInspectorLabel}>{translate("providers.models")}</Text>
        <Text selectable numberOfLines={3} style={styles.resourceInspectorModels}>{resourceModelsSummary(resource, translate)}</Text>
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
  importResources,
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
  importResources: (accountId: string, resourceIds: string[]) => Promise<void>;
  apiKeyActions?: RelayApiKeyActions;
  addAccount: (type: RelayType, origin: string, rememberPassword: boolean, options?: AddAccountOptions) => Promise<AddedRelayAccount | undefined>;
  refreshAccounts: () => Promise<void>;
}): React.JSX.Element {
  const accounts = useMemo(() => accountsFromSnapshot(snapshot), [snapshot]);
  const stations = useMemo(() => stationsFromSnapshot(snapshot, accounts), [snapshot, accounts]);
  const pendingCredentialCleanups = useMemo(() => credentialCleanupsFromSnapshot(snapshot), [snapshot]);
  const [selectedID, setSelectedID] = useState<string>();
  const [selectedStationID, setSelectedStationID] = useState<string>();
  const [adding, setAdding] = useState(setupOnly);
  const [addStep, setAddStep] = useState<AddStep>("origin");
  const [origin, setOrigin] = useState("");
  const [addStationID, setAddStationID] = useState("__custom__");
  const [addStationName, setAddStationName] = useState("");
  const [stationNameEdited, setStationNameEdited] = useState(false);
  const [rememberPassword, setRememberPassword] = useState(false);
  const [typeDetection, setTypeDetection] = useState<RelayTypeDetection>();
  const [manualType, setManualType] = useState<RelayType>();
  const [selectedResources, setSelectedResources] = useState<string[]>([]);
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
  const [loginFailureIDs, setLoginFailureIDs] = useState<Set<string>>(() => new Set());
  const rememberPasswordRef = useRef(false);
  const rememberPasswordWriteVersion = useRef(new Map<string, number>());
  const accountsRef = useRef(accounts);
  accountsRef.current = accounts;
  const typeDetectionRequest = useRef(0);
  const openedAccountIDs = useRef(new Set<string>());
  const controlsBusy = busy || formBusy || loginBusy || restoreBusy || resourceBusy || cleanupBusy || stationFormBusy;
  const passwordStorageAvailable = true;
  const selected = selectedStationID ? undefined : accounts.find((account) => account.id === selectedID) ?? accounts[0];
  const resourceTableRows = useMemo(() => selected?.resources.map((resource) => ({
    key: resource.id,
    cells: [resource.apiName, resource.groupName || resource.groupID || translate("relay.apiKeyUngrouped")],
  })) ?? [], [selected?.resources, translate]);
  const resourceSecondaryCellKeys = useMemo(() => selected?.resources.flatMap((resource) => !resource.enabled
    ? [`${resource.id}\x1f0`, `${resource.id}\x1f1`]
    : []) ?? [], [selected?.resources]);
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
    ? groupLabel(selectedResourceGroup)
    : selectedResource?.groupName || translate("relay.apiKeyUngrouped");
  const selectedRememberPassword = selected ? rememberPasswordDrafts[selected.id] ?? selected.rememberPassword : false;
  const selectedStation = stations.find((station) => station.id === selectedStationID)
    ?? (selected ? stations.find((station) => station.accountIDs.includes(selected.id)) : undefined)
    ?? stations[0];
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
    const children = stationAccounts(station);
    const stationLabel = stationDisplayName(station, translate);
    const rows: Array<{ key: string; cells: string[]; spanning?: boolean }> = [{
      key: `station:${station.id}`,
      cells: [stationLabel, ""],
      spanning: true,
    }];
    for (const account of children) {
      rows.push({
        key: `account:${account.id}`,
        cells: [`\t${accountDisplayName(account, translate)}`, balanceLabel(account, translate)],
      });
    }
    return rows;
  }), [stations, accounts, translate]);
  const relayTableSelection = selectedStationID ? `station:${selectedStationID}` : selected?.id ? `account:${selected.id}` : "";
  const stationOriginValue = normalizeRelayOrigin(stationOriginDraft);
  const stationFormDirty = Boolean(selectedStation) && (
    stationNameDraft.trim() !== stationDisplayName(selectedStation, translate).trim()
    || stationOriginValue !== normalizeRelayOrigin(selectedStation.origin)
    || (stationTypeDraft ?? selectedStation.type) !== selectedStation.type
  );
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
    setOrigin("");
    setAddStationID("__custom__");
    setAddStationName("");
    setStationNameEdited(false);
    setTypeDetection(undefined);
    setManualType(undefined);
    setSelectedResources([]);
    rememberPasswordRef.current = false;
    setRememberPassword(false);
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
      const accountType = detected ?? manualType;
      if (!accountType) {
        // A white-label site can block the public detection probes while
        // still presenting a normal sign-in page. Require an explicit family
        // choice before opening the native browser.
        setFeedback(translate("relay.typeNotDetected"));
        return;
      }
      const chosenStation = addStationID !== "__custom__" ? stations.find((station) => station.id === addStationID) : undefined;
      account = await addAccount(accountType, candidate, passwordStorageAvailable && rememberPasswordRef.current, {
        stationID: chosenStation?.persisted ? chosenStation.id : undefined,
        stationOrigin: chosenStation?.origin,
        stationName: (chosenStation?.name || addStationName).trim() || undefined,
        stationType: chosenStation?.type ?? accountType,
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
  const deleteAccount = async (account: Pick<RelayAccount, "id" | "label">): Promise<void> => {
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
    setSelectedResources([]);
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
      setSelectedResources([]);
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
      if (setupOnly) onClose?.();
    } catch {
      setFeedback(translate("relay.operationFailed"));
    } finally {
      setFormBusy(false);
    }
  };
  const runApiKeyAction = async (kind: "create" | "update" | "setEnabled" | "setGroup" | "remove", resourceID?: string, value?: boolean | string): Promise<void> => {
    if (!selected || (kind !== "create" && !resourceID)) return;
    if (kind === "create" && !apiKeyActions?.create) return;
    if (kind === "update" && !apiKeyActions?.update) return;
    if (kind === "setEnabled" && (!apiKeyActions?.setEnabled || typeof value !== "boolean")) return;
    if (kind === "setGroup" && (!apiKeyActions?.setGroup || typeof value !== "string")) return;
    if (kind === "remove" && !apiKeyActions?.remove) return;
    if (kind === "remove") {
      const resource = selected.resources.find((item) => item.id === resourceID);
      const confirmed = await native.showConfirmation({
        title: translate("relay.apiKeyDeleteTitle"),
        message: translate("relay.apiKeyDeleteBody", { label: resource?.apiName || resourceID || "" }),
        confirmLabel: translate("common.delete"),
      });
      if (!confirmed) return;
    }
    setFormBusy(true);
    setFeedback(undefined);
    try {
      if (kind === "create") {
        await apiKeyActions?.create?.(selected.id);
      }
      else if (kind === "update") {
        const resource = selected.resources.find((item) => item.id === resourceID);
        const name = (apiKeyNameDrafts[resourceID as string] ?? resource?.name ?? "").trim();
        if (!name || name === resource?.name) return;
        await apiKeyActions?.update?.(selected.id, resourceID as string, name);
      }
      else if (kind === "setEnabled") await apiKeyActions?.setEnabled?.(selected.id, resourceID as string, value as boolean);
      else if (kind === "setGroup") await apiKeyActions?.setGroup?.(selected.id, resourceID as string, value as string);
      else if (kind === "remove") await apiKeyActions?.remove?.(selected.id, resourceID as string);
      await refreshAccounts();
      // Mutations intentionally return only masked metadata. Re-discover the
      // list after the remote write so the new name/group/count is visible
      // without ever placing the raw key in the ordinary snapshot.
      await refreshAccountResources(selected);
      if (kind === "remove" && resourceID === selectedResourceID) setSelectedResourceID(undefined);
      const feedbackKey = kind === "create"
        ? "relay.apiKeyCreated"
        : kind === "remove"
          ? "relay.apiKeyDeleted"
          : kind === "setEnabled"
            ? (value ? "relay.apiKeyEnabled" : "relay.apiKeyDisabled")
            : "relay.apiKeyUpdated";
      setFeedback(translate(feedbackKey));
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
  const selectAllResources = (): void => {
    if (!selected) return;
    setSelectedResources(selected.resources.filter((resource) => resource.enabled).map((resource) => resource.id));
  };
  const clearResourceSelection = (): void => {
    setSelectedResources([]);
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
  const updateStation = async (): Promise<void> => {
    if (!selectedStation) return;
    const name = stationNameDraft.trim();
    const origin = normalizeRelayOrigin(stationOriginDraft);
    if (!name || !origin) return;
    setStationFormBusy(true);
    setFeedback(undefined);
    try {
      await commit("station.update", {
        id: selectedStation.id,
        name,
        origin,
        type: stationTypeDraft ?? selectedStation.type,
      }, "relay_accounts");
      await refreshAccounts();
      setFeedback(translate("relay.stationUpdated"));
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
    setSelectedResources([]);
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
    setSelectedResources([]);
    setSelectedResourceID(undefined);
    setFeedback(undefined);
  };
  const selectRelayTableRow = (key: string): void => {
    const [kind, id] = key.split(":", 2);
    if (!id) return;
    if (kind === "station") selectStation(id);
    else if (kind === "account") selectAccount(id);
  };
  const updateAddOrigin = (value: string): void => {
    typeDetectionRequest.current += 1;
    setOrigin(value);
    const leavingExistingStation = addStationID !== "__custom__";
    if (leavingExistingStation) setAddStationID("__custom__");
    if (!stationNameEdited || leavingExistingStation) {
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

  return <View style={styles.workspace} accessibilityLabel={translate("relay.title")}>
    {!setupOnly && pendingCleanups.length > 0 ? <ScrollView style={styles.pendingCleanupList} contentContainerStyle={styles.pendingCleanupListContent}>{pendingCleanups.map((cleanup) => <View key={`${cleanup.accountID}:${cleanup.kind}`} style={styles.pendingCleanup}><Text style={styles.pendingCleanupText}>{translate("relay.credentialsCleanupPending", { label: cleanup.label })}</Text><NativeButton title={translate("relay.retryCleanup")} compact disabled={controlsBusy} onPress={() => { void retryCredentialCleanup(cleanup); }} /></View>)}</ScrollView> : null}
    <View style={styles.relayLayout}>
      {!setupOnly ? <RelayTablePane title={translate("relay.accounts")} style={styles.sidebar} actions={<>
        <NativeButton title={translate("relay.addAccount")} symbol="plus" toolTip={translate("relay.addAccount")} accessibilityLabel={translate("relay.addAccount")} compact disabled={controlsBusy} onPress={beginAdding} style={styles.sidebarAddButton} />
        <NativeButton title={translate("relay.delete")} symbol="minus" toolTip={translate("relay.delete")} accessibilityLabel={translate("relay.delete")} destructive compact disabled={controlsBusy || !selected || adding} onPress={remove} style={styles.sidebarIconButton} />
      </>}>
        {stations.length > 0 ? <View style={styles.sidebarTableFrame}><NativeTable
          columns={[{ label: translate("relay.accounts"), width: 122 }, { label: translate("relay.balance"), width: 78 }]}
          rows={relayTableRows}
          selectedKey={relayTableSelection}
          disabledRowKeys={controlsBusy ? relayTableRows.map((row) => row.key) : []}
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
          <ScrollView style={styles.detailScroll} contentContainerStyle={[styles.detailContent, compactStyles.detailContent, setupOnly && styles.setupContent]}>
            <View style={setupOnly ? styles.setupSurface : undefined}>
              <SetupProgress step="sign-in" translate={translate} />
              <View style={[styles.detailHeader, setupOnly && styles.setupHeader]}>
                <Text style={styles.detailTitle}>{translate("relay.stepSignIn")}</Text>
                <Text style={styles.detailSubtitle}>{translate("relay.stepSignInDetail")}</Text>
              </View>
              <View style={styles.signInWaiting}><Text style={styles.formHint}>{translate("relay.loginWorking")}</Text></View>
            </View>
          </ScrollView>
          <View style={[styles.bottomBar, compactStyles.bottomBar, setupOnly && styles.setupBottomBar]}><Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.bottomStatus}>{feedback ?? translate("relay.loginWorking")}</Text></View>
        </View> : <View style={styles.detailWorkspace}>
          <ScrollView style={styles.detailScroll} contentContainerStyle={[styles.detailContent, compactStyles.detailContent, setupOnly && styles.setupContent]}>
            <View style={setupOnly ? styles.setupSurface : undefined}>
              {setupOnly ? <SetupProgress step="origin" translate={translate} /> : null}
              <View style={[styles.detailHeader, setupOnly && styles.setupHeader]}>
                <Text style={styles.detailTitle}>{translate("relay.addAccount")}</Text>
              </View>
              <View style={[styles.formSection, setupOnly && styles.setupFormSection]}>
                <FormRow label={translate("relay.origin")}><NativeTextField value={origin} placeholder={translate("relay.originPlaceholder")} editable={!controlsBusy} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={updateAddOrigin} style={[styles.control, compactStyles.control]} /></FormRow>
                <FormRow label={translate("relay.stationName")}><NativeTextField value={addStationName} placeholder={translate("relay.stationNamePlaceholder")} editable={!controlsBusy && addStationID === "__custom__"} accessibilityLabel={translate("relay.stationName")} onChangeText={updateAddStationName} style={[styles.control, compactStyles.control]} /></FormRow>
                {stations.length > 0 ? <FormRow label={translate("relay.stationChoice")}><NativePicker labels={[translate("relay.stationCustom"), ...stations.map(stationPickerLabel)]} selectedValue={selectedAddStation ? stationPickerLabel(selectedAddStation) : translate("relay.stationCustom")} disabled={controlsBusy} onChange={({ nativeEvent }) => {
                  const station = stations[nativeEvent.index - 1];
                  if (!station) {
                    setAddStationID("__custom__");
                    setAddStationName(suggestedRelayStationName(origin));
                    setStationNameEdited(false);
                    return;
                  }
                  setAddStationID(station.id);
                  setAddStationName(station.name);
                  setStationNameEdited(false);
                  setOrigin(station.origin);
                  setTypeDetection(station.type);
                  setManualType(station.type);
                }} style={styles.typeSelector} /></FormRow> : null}
                {typeDetection ? <Text style={styles.formHint}>{typeDetection === "checking" ? translate("relay.detectingType") : typeDetection === "unknown" ? translate("relay.compatibilityHint") : translate("relay.stationReady")}</Text> : null}
                {typeDetection === "unknown" ? <FormRow label={translate("relay.type")}><NativePicker labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={manualType ? relayTypeLabel(manualType, translate) : ""} disabled={controlsBusy} onChange={({ nativeEvent }) => { setManualType(nativeEvent.index === 1 ? "sub2api" : "newapi"); }} style={styles.typeSelector} /></FormRow> : null}
                {passwordStorageAvailable ? <View style={styles.checkboxFormRow}><NativeCheckbox label={translate("relay.rememberPassword")} value={rememberPassword} disabled={controlsBusy} onValueChange={(next) => { rememberPasswordRef.current = next; setRememberPassword(next); }} /></View> : <Text style={styles.formHint}>{translate("relay.passwordNotSaved")}</Text>}
              </View>
            </View>
          </ScrollView>
          <View style={[styles.bottomBar, compactStyles.bottomBar, setupOnly && styles.setupBottomBar]}>
            {feedback || !setupOnly ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.bottomStatus}>{feedback ?? translate("relay.stepSignInDetail")}</Text> : null}
            <View style={[styles.bottomActions, setupOnly && styles.setupBottomActions]}>{setupOnly && onClose ? <NativeButton title={translate("menu.close")} disabled={controlsBusy} onPress={onClose} /> : !setupOnly ? <NativeButton title={translate("menu.cancel")} disabled={controlsBusy} onPress={resetForm} /> : null}<NativeButton title={translate("relay.next")} primary disabled={controlsBusy || !origin.trim() || (addStationID === "__custom__" && !addStationName.trim())} onPress={() => { void beginLogin(); }} /></View>
          </View>
        </View> : selectedStationID && selectedStation ? <View style={styles.detailWorkspace}>
          <View style={styles.stationDetailHeader}>
            <View style={styles.detailHeading}>
              <Text numberOfLines={1} style={styles.detailTitle}>{stationDisplayName(selectedStation, translate)}</Text>
              <Text numberOfLines={1} selectable style={styles.detailSubtitle}>{selectedStation.origin || translate("relay.stationDetails")}</Text>
            </View>
            <NativeButton title={translate("common.save")} compact primary disabled={controlsBusy || !stationNameDraft.trim() || !stationOriginValue || !stationFormDirty} onPress={() => { void updateStation(); }} />
          </View>
          <ScrollView style={styles.stationDetailScroll} contentContainerStyle={styles.stationDetailContent}>
            <View style={[styles.stationSettings, compactStyles.stationSettings]}>
              <Text style={styles.stationSettingsTitle}>{translate("relay.stationDetails")}</Text>
              <View style={[styles.stationSettingsForm, compactStyles.stationSettingsForm]}>
                <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow]}>
                  <Text style={styles.stationSettingsLabel}>{translate("relay.stationName")}</Text>
                  <NativeTextField value={stationNameDraft} editable={!controlsBusy} accessibilityLabel={translate("relay.stationName")} onChangeText={setStationNameDraft} style={[styles.stationSettingsControl, compactStyles.control]} />
                </View>
                <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow]}>
                  <Text style={styles.stationSettingsLabel}>{translate("relay.origin")}</Text>
                  <NativeTextField value={stationOriginDraft} editable={!controlsBusy} accessibilityLabel={translate("relay.origin")} autoCapitalize="none" autoCorrect={false} onChangeText={setStationOriginDraft} style={[styles.stationSettingsControl, compactStyles.control]} />
                </View>
                <View style={[styles.stationSettingsRow, compactStyles.stationSettingsRow, styles.stationSettingsLastRow]}>
                  <Text style={styles.stationSettingsLabel}>{translate("relay.type")}</Text>
                  <NativePicker labels={[relayTypeLabel("newapi", translate), relayTypeLabel("sub2api", translate)]} selectedValue={stationTypeDraft ? relayTypeLabel(stationTypeDraft, translate) : ""} disabled={controlsBusy} onChange={({ nativeEvent }) => setStationTypeDraft(nativeEvent.index === 1 ? "sub2api" : "newapi")} style={[styles.stationSettingsControl, compactStyles.control]} />
                </View>
              </View>
              {feedback ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.stationSettingsFeedback}>{feedback}</Text> : null}
            </View>
          </ScrollView>
        </View> : !setupOnly && selected ? <View style={styles.detailWorkspace}>
          <View style={styles.accountDetailContent}>
            <View style={styles.accountHeader}>
              <View style={styles.accountToolbar}>
                <View style={[styles.detailHeader, styles.accountDetailHeader]}>
                  <View style={[styles.detailHeading, styles.accountDetailHeading]}>
                  <Text numberOfLines={1} style={styles.detailTitle}>{accountDetailTitle(selected, translate)}</Text>
                  <Text numberOfLines={1} style={styles.detailSubtitle}>{accountStationLabel(selected)}</Text>
                  </View>
                  <View style={styles.detailHeaderActions}>
                    <View style={styles.statusLine}><View style={[styles.statusDot, effectiveLoginStatus(selected) === "signed_in" ? styles.statusDotOnline : styles.statusDotExpired]} /><Text style={styles.statusText}>{translate(statusKey(effectiveLoginStatus(selected)))}</Text></View>
                    <NativeButton title={translate("common.refresh")} compact disabled={controlsBusy} onPress={() => { void refreshLoginState(selected); }} />
                  </View>
                </View>
              </View>
              <View style={styles.accountMetadata}>
                <View style={styles.metaItem}><Text style={styles.metaLabel}>{translate("relay.balance")}</Text><Text selectable numberOfLines={1} style={styles.metaValue}>{balanceLabel(selected, translate)}</Text></View>
                <View style={styles.metaItem}><Text style={styles.metaLabel}>{translate("relay.type")}</Text><Text numberOfLines={1} style={styles.metaValue}>{relayTypeLabel(selected.type, translate)}</Text></View>
                {passwordStorageAvailable ? <View style={[styles.metaItem, styles.metaPreference]}><Text style={styles.metaLabel}>{translate("relay.rememberPassword")}</Text><NativeCheckbox label={selectedRememberPassword ? translate("common.enable") : translate("common.disable")} value={selectedRememberPassword} disabled={controlsBusy} onValueChange={(next) => { void updateRememberPassword(next); }} style={styles.metaCheckbox} /></View> : <Text style={styles.formHint}>{translate("relay.passwordNotSaved")}</Text>}
              </View>
            </View>
            <View style={[styles.resourcesSection, compactStyles.resourcesSection]}>
              <View style={styles.resourcePane}>
                <View style={[styles.resourceToolbar, compactStyles.resourceToolbar]}>
                  <View style={styles.resourceToolbarHeading}>
                    <Text style={styles.resourceToolbarTitle}>{translate("relay.apiKeysTitle")}</Text>
                    {selected.resources.length > 0 ? <Text style={styles.resourceCount}>{translate("relay.resourceCount", { count: selected.resources.length })}</Text> : null}
                    <View style={styles.resourceToolbarCrud}>
                      <NativeButton title={translate("relay.apiKeyCreate")} symbol="plus" compact disabled={controlsBusy || !apiKeyActions?.create} toolTip={translate("relay.apiKeyCreate")} accessibilityLabel={translate("relay.apiKeyCreate")} onPress={() => { void runApiKeyAction("create"); }} style={styles.resourceToolbarCrudButton} />
                      <NativeButton title={translate("relay.apiKeyDelete")} symbol="minus" compact destructive disabled={controlsBusy || !apiKeyActions?.remove || !selectedResource} toolTip={translate("relay.apiKeyDelete")} accessibilityLabel={selectedResource ? `${translate("relay.apiKeyDelete")}: ${selectedResource.apiName}` : translate("relay.apiKeyDelete")} onPress={() => { if (selectedResource) void runApiKeyAction("remove", selectedResource.id); }} style={styles.resourceToolbarCrudButton} />
                    </View>
                  </View>
                  <View style={styles.resourceToolbarActions}>
                    {selectedResources.length > 0 ? <Text numberOfLines={1} style={styles.resourceSelectionCount}>{translate("relay.selectedCount", { count: selectedResources.length })}</Text> : null}
                    <NativeButton title={translate("relay.selectAllResources")} compact disabled={controlsBusy || selected.resources.every((resource) => !resource.enabled)} onPress={selectAllResources} />
                    {selectedResources.length > 0 ? <NativeButton title={translate("relay.clearResourceSelection")} compact disabled={controlsBusy} onPress={clearResourceSelection} /> : null}
                    {selectedResources.length > 0 ? <NativeButton primary title={translate("relay.importSelected")} disabled={controlsBusy} onPress={() => { void importSelectedResources(); }} /> : null}
                  </View>
                </View>
                {feedback ? <Text accessibilityLiveRegion="polite" style={styles.resourcesFeedback}>{feedback}</Text> : null}
                <View style={styles.resourceBody}>
                  <View style={styles.resourceColumns}>
                    <View style={styles.resourceListPane}>
                      <NativeTable
                        columns={[{ label: translate("common.name"), width: 122 }, { label: translate("relay.apiKeyGroup"), width: 124 }]}
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
                      selectedForImport={selectedResources.includes(selectedResource.id)}
                      onNameChange={(value) => setApiKeyNameDrafts((current) => ({ ...current, [selectedResource.id]: value }))}
                      onSaveName={() => { void runApiKeyAction("update", selectedResource.id); }}
                      onGroupChange={(groupID) => { void runApiKeyAction("setGroup", selectedResource.id, groupID); }}
                      onEnabledChange={(enabled) => { if (!enabled) setSelectedResources((current) => current.filter((value) => value !== selectedResource.id)); void runApiKeyAction("setEnabled", selectedResource.id, enabled); }}
                      onImportChange={() => toggleResource(selectedResource.id)}
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
  formRow: { minHeight: 30, gap: 5 },
  formValue: { minHeight: 26, gap: 4 },
  control: { height: 26 },
  stationSettings: { gap: 6 },
  stationSettingsForm: { gap: 4 },
  stationSettingsRow: { minHeight: 26, gap: 6 },
  resourcesSection: { gap: 4, paddingTop: 0 },
  resourceToolbar: { minHeight: 24, gap: 6 },
  bottomBar: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 6, gap: 6 },
});

const styles = StyleSheet.create({
  hidden: { display: "none" },
  workspace: { flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden", backgroundColor: colors.window },
  relayLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 6 },
  setupDetail: { width: "100%" },
  tablePane: { minWidth: 0, minHeight: 0, gap: 6 },
  tableTitleRow: { height: 24, minHeight: 24, paddingHorizontal: 16, flexDirection: "row", alignItems: "center" },
  tableTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  tableActions: { marginLeft: "auto", flexDirection: "row", alignItems: "center", gap: 6 },
  sidebar: { width: 220, minWidth: 220, maxWidth: 220, flexGrow: 0, flexShrink: 0 },
  sidebarTableFrame: { flex: 1, minWidth: 0, minHeight: 0 },
  sidebarEmpty: { flex: 1, minHeight: 120, justifyContent: "center", padding: 16, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel },
  sidebarEmptyText: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 17 },
  nativeRelayTable: { flex: 1, minWidth: 0, minHeight: 0 },
  sidebarAddButton: { width: 22, minWidth: 22, height: 22 },
  sidebarIconButton: { width: 22, minWidth: 22, height: 22 },
  detail: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  detailWorkspace: { flex: 1, minWidth: 0, minHeight: 0 },
  detailScroll: { flex: 1, minWidth: 0 },
  detailContent: { flexGrow: 1, minWidth: 0, paddingHorizontal: 20, paddingTop: 16, paddingBottom: 20, gap: 14 },
  accountDetailContent: { flex: 1, minWidth: 0, minHeight: 0 },
  setupContent: { justifyContent: "center", alignItems: "center", paddingHorizontal: 28, paddingTop: 18, paddingBottom: 18 },
  setupSurface: { width: "100%", maxWidth: 560, minWidth: 0, gap: 18 },
  setupProgress: { width: "100%", flexDirection: "row", alignItems: "center", minHeight: 26 },
  setupProgressStep: { flexDirection: "row", alignItems: "center", gap: 7, flexShrink: 0 },
  setupProgressConnector: { flex: 1, minWidth: 12, height: 1, marginHorizontal: 10, backgroundColor: colors.separator },
  setupProgressConnectorActive: { backgroundColor: colors.accent },
  setupProgressBadge: { width: 22, height: 22, borderRadius: 11, borderWidth: 1, borderColor: colors.separator, alignItems: "center", justifyContent: "center", backgroundColor: colors.window },
  setupProgressBadgeCurrent: { borderColor: colors.accent, backgroundColor: colors.accent },
  setupProgressBadgeDone: { borderColor: colors.accent, backgroundColor: colors.window },
  setupProgressNumber: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  setupProgressNumberCurrent: { color: colors.accentText },
  setupProgressLabel: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE },
  setupProgressLabelCurrent: { color: colors.text, fontWeight: "600" },
  setupHeader: { paddingBottom: 0, gap: 6 },
  setupFormSection: { maxWidth: 560, paddingVertical: 0, gap: 16 },
  stationDetailHeader: { minHeight: 30, paddingHorizontal: 12, paddingTop: 3, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 6 },
  stationDetailScroll: { flex: 1, minWidth: 0 },
  stationDetailContent: { flexGrow: 1, minWidth: 0, paddingTop: 3, paddingHorizontal: 12, paddingRight: 8, paddingBottom: 12, alignItems: "flex-start" },
  stationSettings: { width: "100%", minWidth: 0, gap: 6 },
  stationSettingsTitle: { color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  stationSettingsForm: { width: "100%", gap: 4, paddingTop: 3, borderTopWidth: 1, borderTopColor: colors.separator },
  stationSettingsRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 },
  stationSettingsLastRow: {},
  stationSettingsLabel: { width: 72, flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  stationSettingsControl: { flex: 1, minWidth: 220, height: 26 },
  stationSettingsFeedback: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 17 },
  accountHeader: { minWidth: 0, backgroundColor: colors.window },
  accountToolbar: { minHeight: 42, paddingHorizontal: 16, paddingVertical: 4, justifyContent: "center", backgroundColor: colors.window },
  detailHeader: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  detailHeading: { flexGrow: 1, flexShrink: 1, minWidth: 0, flexDirection: "row", alignItems: "baseline", gap: 5 },
  accountDetailHeader: { minHeight: 34 },
  accountDetailHeading: { flexDirection: "column", alignItems: "flex-start", gap: 1 },
  detailTitle: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  detailSubtitle: { flexShrink: 1, color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  detailHeaderActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 6 },
  statusLine: { minHeight: 22, flexDirection: "row", alignItems: "center", gap: 5 },
  statusText: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE },
  statusDot: { width: 7, height: 7, borderRadius: 4 },
  statusDotOnline: { backgroundColor: colors.success },
  statusDotExpired: { backgroundColor: colors.warning },
  control: { width: "100%", minWidth: 0, height: 32 },
  typeSelector: { width: "100%", minWidth: 0, maxWidth: 520, alignSelf: "stretch" },
  formSection: { width: "100%", maxWidth: 720, minWidth: 0, paddingVertical: 4, gap: 8, borderTopWidth: 1, borderTopColor: colors.separator, backgroundColor: colors.window },
  accountMetadata: { minHeight: 34, paddingHorizontal: 16, paddingVertical: 4, flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 20, rowGap: 3, backgroundColor: colors.window },
  metaItem: { minWidth: 108, minHeight: 26, flexDirection: "row", alignItems: "center", gap: 7 },
  metaPreference: { minWidth: 160 },
  metaLabel: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  metaValue: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "500" },
  metaCheckbox: { minWidth: 0 },
  formRow: { width: "100%", minHeight: 34, flexDirection: "column", alignItems: "stretch", gap: 6 },
  checkboxFormRow: { width: "100%", minHeight: 32, justifyContent: "center" },
  formLabel: { width: "100%", minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  formValue: { width: "100%", minWidth: 0, minHeight: 30, justifyContent: "center", gap: 4 },
  formHint: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 17, paddingVertical: 5 },
  signInWaiting: { minHeight: 160, alignItems: "center", justifyContent: "center", paddingHorizontal: 20 },
  readOnlyValue: { color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  resourcesSection: { flex: 1, minWidth: 0, minHeight: 0 },
  resourcePane: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  resourceToolbar: { minHeight: 24, paddingHorizontal: 16, flexDirection: "row", alignItems: "center", gap: 8 },
  resourceToolbarHeading: { flex: 1, minWidth: 110, flexDirection: "row", alignItems: "baseline", gap: 6 },
  resourceToolbarTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  resourceToolbarCrud: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4, marginLeft: 2 },
  resourceToolbarCrudButton: { width: 22, minWidth: 22, height: 22 },
  resourceToolbarActions: { marginLeft: "auto", flexShrink: 1, flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 6 },
  resourceCount: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  resourceSelectionCount: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  resourcesFeedback: { paddingHorizontal: 16, paddingVertical: 4, color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16, backgroundColor: colors.window },
  resourceBody: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window },
  resourceColumns: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 6 },
  resourceListPane: { width: 260, minWidth: 220, maxWidth: 320, flexGrow: 0, flexShrink: 1, minHeight: 0 },
  resourceNativeTable: { flex: 1, minWidth: 0, minHeight: 0 },
  resourceInspectorPane: { flex: 1, minWidth: 0, minHeight: 0 },
  resourceInspectorScroll: { flex: 1, minWidth: 0, backgroundColor: colors.window },
  resourceInspectorContent: { flexGrow: 1, minWidth: 0, paddingTop: 0, paddingHorizontal: 16, paddingRight: 12, paddingBottom: 10, gap: 5 },
  resourceInspectorHeader: { height: 24, minHeight: 24, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 6 },
  resourceInspectorHeading: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "baseline", gap: 5 },
  resourceInspectorTitle: { flexShrink: 1, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18, fontWeight: "600" },
  resourceInspectorSubtitle: { flexShrink: 1, color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  resourceInspectorDivider: { height: 1, backgroundColor: colors.separator },
  resourceInspectorForm: { minWidth: 0, gap: 6 },
  resourceInspectorToggleRow: { minHeight: 26, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 10 },
  resourceInspectorRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 8 },
  resourceInspectorKeyRow: { alignItems: "center" },
  resourceInspectorLabel: { width: 68, flexShrink: 0, color: colors.secondary, fontSize: UI_FONT_SIZE },
  resourceInspectorControlRow: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "center", gap: 4 },
  resourceInspectorTextInput: { flex: 1, minWidth: 90, height: 26 },
  resourceInspectorPicker: { flex: 1, minWidth: 120, height: 26 },
  resourceInspectorReadOnly: { flex: 1, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  resourceInspectorModels: { flex: 1, minWidth: 0, color: colors.text, fontSize: UI_FONT_SIZE, lineHeight: 18 },
  resourceInspectorSecureInput: { flex: 1, minWidth: 100, height: 26 },
  resourceInspectorAction: { width: 22, minWidth: 22, height: 22 },
  resourceEmpty: { flex: 1, minWidth: 0, minHeight: 138, paddingHorizontal: 24, paddingVertical: 18, alignItems: "center", justifyContent: "center", gap: 5, backgroundColor: colors.window },
  resourceEmptyTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600", lineHeight: 19, textAlign: "center" },
  resourceEmptyText: { maxWidth: 500, color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 18, textAlign: "center" },
  bottomBar: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 6, flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 6, borderTopWidth: 1, borderTopColor: colors.separator, backgroundColor: colors.panel },
  setupBottomBar: { minHeight: 58, paddingHorizontal: 28, paddingVertical: 12, borderTopWidth: 0, backgroundColor: colors.window },
  bottomStatus: { flex: 1, flexBasis: 220, minWidth: 0, color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 },
  bottomTitle: { color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  bottomActions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 6 },
  setupBottomActions: { marginLeft: "auto" },
  pendingCleanupList: { maxHeight: 116, borderBottomWidth: 1, borderBottomColor: colors.separator, backgroundColor: colors.panel },
  pendingCleanupListContent: { paddingHorizontal: 18, paddingVertical: 8, gap: 6 },
  pendingCleanup: { minHeight: 30, flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 12 },
  pendingCleanupText: { flex: 1, flexBasis: 260, minWidth: 0, color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  empty: { color: colors.secondary, fontSize: UI_FONT_SIZE, lineHeight: 19, textAlign: "center" },
  blank: { flex: 1, minHeight: 240, alignItems: "center", justifyContent: "center", gap: 14, paddingHorizontal: 28, backgroundColor: colors.window },
});
