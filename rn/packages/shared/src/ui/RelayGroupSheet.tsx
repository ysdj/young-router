/**
 * 分组管理, in a page.
 *
 * The native hosts give this sheet its own window and draw it with AppKit /
 * WinUI controls, so walking the pane's key list — rename a key, move it to
 * another group, add one, retire one, or hand the whole layout to 自动分组 —
 * was the one relay action a browser could not take at all: the pane disabled
 * 分组 because the host had no surface to put it in.  This is that surface,
 * drawn with the primitives every pane already uses.
 *
 * The draft rules are the sheet's, not this file's invention: the list is the
 * account's own keys, 自动分组 checked *previews* the one-key-per-group layout
 * over them, unchecking puts that preview away, and only a row the user staged
 * is handed to Core.  The pane writes what 保存并关闭 hands it and states the
 * outcome here, so one surface reports one save.
 */

import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import type {
  RelayGroupManagerGroup,
  RelayGroupManagerKey,
  RelayGroupManagerResult,
  RelayGroupManagerSnapshot,
} from "../types";
import {
  askCloseRelayGroupSheet,
  closeRelayGroupSheet,
  relayGroupSheetState,
  requestRelayGroupApply,
  setRelayGroupSheetCloseQuestion,
  subscribeRelayGroupSheet,
} from "../platform/relayGroupSheet";
import { requestHostChrome } from "../platform/hostChrome";
import { NativeButton, NativeCheckbox, NativePicker, NativeTable, NativeTextField, WINDOWS_CONTROL } from "./NativeControls";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** The models grid draws at most this many of a key's models, in order. */
const MODEL_LINES = 12;

type KeyRow = {
  id: string;
  name: string;
  groupID: string;
  groupLabel: string;
  multiplier: string;
  hint: string;
  models: string[];
  enabled: boolean;
  originalName: string;
  originalGroupID: string;
  originalEnabled: boolean;
  /** A row ＋ made: it has no key on the station yet. */
  draft: boolean;
  /** A row 自动分组's own layout derived for a group that has no key. */
  preview: boolean;
  deleted: boolean;
};

function keyRows(keys: RelayGroupManagerKey[]): KeyRow[] {
  return keys.map((key) => ({
    id: key.id,
    name: key.name,
    groupID: key.groupID,
    groupLabel: key.groupLabel,
    multiplier: key.multiplier,
    hint: key.hint,
    models: key.models,
    enabled: key.enabled,
    originalName: key.name,
    originalGroupID: key.groupID,
    originalEnabled: key.enabled,
    draft: false,
    preview: false,
    deleted: false,
  }));
}

/**
 * 自动分组's layout, derived from the account's rows.
 *
 * One key per group the station still offers, named after its group: for each
 * group in the picker's order the first key carrying it is kept and renamed,
 * every later key in that group and every key whose group is gone leaves the
 * list, and a group with no key gets the preview row that saving the switch
 * will create.  A draft ＋ made is the user's own edit and is left alone, and a
 * row already staged for deletion stays deleted.
 */
function autoGroupingLayout(source: KeyRow[], groups: RelayGroupManagerGroup[]): KeyRow[] {
  const layoutGroups = groups.filter((group) => group.id !== "");
  if (layoutGroups.length === 0) return source;
  const kept = new Set<string>();
  const present = new Set<string>();
  for (const row of source) {
    if (!row.draft && row.groupID !== "") present.add(row.groupID);
  }
  const staged: KeyRow[] = [];
  for (const row of source) {
    if (row.preview) continue;
    if (row.draft || row.deleted) { staged.push(row); continue; }
    if (row.groupID === "") continue;
    const group = layoutGroups.find((entry) => entry.id === row.groupID);
    if (group === undefined || kept.has(group.id)) continue;
    kept.add(group.id);
    staged.push({ ...row, name: group.name, enabled: true });
  }
  for (const group of layoutGroups) {
    if (kept.has(group.id) || present.has(group.id)) continue;
    staged.push({
      id: `draft-${group.id}`,
      name: group.name,
      groupID: group.id,
      groupLabel: group.name,
      multiplier: group.rate,
      hint: "",
      models: [],
      enabled: true,
      originalName: group.name,
      originalGroupID: group.id,
      originalEnabled: true,
      draft: true,
      preview: true,
      deleted: false,
    });
  }
  return staged;
}

export function RelayGroupSheet({ translate, copyKey }: {
  translate: Translate;
  /** Copy one key through Core's lease; the value never enters this tree. */
  copyKey: (accountId: string, keyID: string) => Promise<boolean>;
}): React.JSX.Element | null {
  const state = React.useSyncExternalStore(subscribeRelayGroupSheet, relayGroupSheetState, relayGroupSheetState);
  // The drafts belong to one opening of the sheet, so the list is rebuilt when
  // a new account opens and replaced wholesale by a later read of the same one.
  const snapshot = state?.snapshot;
  const groups = React.useMemo(() => snapshot?.groups ?? [], [snapshot]);
  const [rows, setRows] = React.useState<KeyRow[]>(() => keyRows(snapshot?.keys ?? []));
  const [autoGrouping, setAutoGrouping] = React.useState<boolean>(snapshot?.autoGrouping === true);
  const baseline = React.useRef<{ rows: KeyRow[]; keys: Set<string> } | undefined>(undefined);
  const [selectedID, setSelectedID] = React.useState<string>("");
  const [note, setNote] = React.useState<string | undefined>(undefined);

  React.useEffect(() => {
    if (!state || !snapshot) return;
    // A later read of the same account is the station's own list again: the
    // sheet is read-only while that read is in flight, so no draft can be lost
    // to it (the pane only sends one while `loading` is set).  A window that
    // opens on a checked 自动分组 states the layout that switch means, exactly
    // as clicking it would — without this the list showed the account's raw
    // keys under a switch that says one key per group.
    const fresh = keyRows(snapshot.keys);
    const alignment = snapshot.autoGrouping
      ? { rows: fresh, keys: new Set(fresh.filter((row) => !row.draft).map((row) => row.id)) }
      : undefined;
    baseline.current = alignment;
    setRows(alignment ? autoGroupingLayout(fresh, snapshot.groups) : fresh);
    setAutoGrouping(snapshot.autoGrouping);
    setSelectedID((current) => (fresh.some((row) => row.id === current) ? current : fresh[0]?.id ?? ""));
  }, [snapshot, state?.accountId]);

  // A checked 自动分组 owns the layout, exactly as it does in the native
  // windows: Core refuses a manual key write while it is on, so the sheet must
  // not accept one.  The switch itself stays usable — it is the way back to
  // manual editing — and only the fields it owns are gated here.
  const composing = state !== undefined && !state.loading && !state.saving;
  const editing = composing && !autoGrouping;
  const layoutGroups = groups.filter((group) => group.id !== "");
  const groupNames = React.useMemo(() => new Map(groups.filter((group) => group.id !== "").map((group) => [group.id, group.name])), [groups]);
  const selected = rows.find((row) => row.id === selectedID);

  const groupLabelFor = (row: KeyRow): string => {
    if (row.groupID === "") return state?.labels.ungroupedLabel ?? "";
    return groupNames.get(row.groupID) ?? (row.groupLabel || (state?.labels.ungroupedLabel ?? ""));
  };
  const rateFor = (row: KeyRow): string => (row.groupID !== "" && groupNames.has(row.groupID) ? row.multiplier : "");
  const staged = (row: KeyRow): boolean => row.deleted || (row.draft && !row.preview);

  // A key already on the station cannot be saved without a name: Core refuses
  // `update` with a message that says only that something went wrong.  A name
  // cleared to nothing is therefore not a change to write but a row to fix, so
  // it keeps 保存并关闭 dark instead of travelling as an edit.
  const invalidName = (row: KeyRow): boolean => !row.draft && !row.deleted && row.name.trim() === "";

  const hasStagedChanges = React.useMemo(() => {
    if (state === undefined) return false;
    if (autoGrouping !== state.snapshot.autoGrouping) return true;
    const previewKeys = baseline.current?.keys;
    return rows.some((row) => {
      if (row.preview) return false;
      if (row.draft) return row.name.trim() !== "";
      if (autoGrouping && previewKeys?.has(row.id)) return row.enabled !== row.originalEnabled;
      return (!invalidName(row) && row.name !== row.originalName) || row.groupID !== row.originalGroupID || row.enabled !== row.originalEnabled;
    }) || rows.some((row) => row.deleted && !row.draft);
  }, [autoGrouping, rows, state]);

  const hasInvalidName = React.useMemo(() => rows.some(invalidName), [rows]);

  /** What 保存并关闭 hands the pane; the preview's own lines are Core's. */
  const stagedResult = (): RelayGroupManagerResult | undefined => {
    if (!state || !hasStagedChanges) return undefined;
    const previewKeys = baseline.current?.keys;
    return {
      autoGrouping,
      creates: rows.filter((row) => row.draft && !row.deleted && row.name.trim() !== "").map((row) => ({ name: row.name.trim(), groupID: row.groupID })),
      updates: rows
        .filter((row) => !row.draft && !row.deleted && (!autoGrouping || previewKeys?.has(row.id) !== true)
          && (row.name !== row.originalName || row.groupID !== row.originalGroupID || row.enabled !== row.originalEnabled))
        .map((row) => ({ keyID: row.id, name: row.name, groupID: row.groupID, enabled: row.enabled })),
      deletes: rows.filter((row) => !row.draft && row.deleted && (!autoGrouping || previewKeys?.has(row.id) === true)).map((row) => row.id),
    };
  };

  // The sheet's Close asks before it drops a draft; Escape and the layer's own
  // close button land here too, so the question is asked exactly once.
  const askClose = React.useCallback(() => {
    if (!hasStagedChanges || !state) {
      closeRelayGroupSheet();
      return;
    }
    void requestHostChrome<boolean>({
      kind: "confirm",
      title: state.labels.discardTitle,
      message: state.labels.discardBody,
      confirmLabel: state.labels.discardConfirm,
      cancelLabel: translate("status.cancel"),
      destructive: true,
    }).then((accepted) => {
      if (accepted) closeRelayGroupSheet();
    });
  }, [hasStagedChanges, state, translate]);

  React.useEffect(() => {
    setRelayGroupSheetCloseQuestion(askClose);
    return () => setRelayGroupSheetCloseQuestion(undefined);
  }, [askClose]);

  // The strip states whatever the sheet is doing: the pane's last outcome, the
  // station read that is still in flight (the rows are read-only until it lands,
  // so this is the sentence that says why), or the save.
  const loadingLabel = state?.labels.loadingLabel;
  React.useEffect(() => {
    if (state === undefined) return;
    setNote(state.status
      ?? (state.loading ? loadingLabel
        : state.saving ? translate("common.saving") : undefined));
  }, [loadingLabel, state, translate]);

  if (!state || !snapshot) return null;
  const labels = state.labels;

  const updateRow = (id: string, patch: Partial<KeyRow>): void => {
    setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  };

  const toggleAutoGrouping = (enabled: boolean): void => {
    setAutoGrouping(enabled);
    if (enabled) {
      if (baseline.current === undefined) {
        baseline.current = { rows, keys: new Set(rows.filter((row) => !row.draft).map((row) => row.id)) };
      }
      setRows(autoGroupingLayout(baseline.current.rows, groups));
      return;
    }
    if (baseline.current) setRows(baseline.current.rows);
    baseline.current = undefined;
  };

  const addDraft = (): void => {
    const group = layoutGroups[0] ?? groups[0];
    if (!group) return;
    const base = labels.newKeyName || "New key";
    const existing = new Set(rows.map((row) => row.name));
    let name = base;
    let suffix = 2;
    while (existing.has(name)) { name = `${base} ${suffix}`; suffix += 1; }
    const row: KeyRow = {
      id: `draft:${Date.now()}:${rows.length}`,
      name,
      groupID: group.id,
      groupLabel: group.name,
      multiplier: group.rate,
      hint: "",
      models: [],
      enabled: true,
      originalName: "",
      originalGroupID: group.id,
      originalEnabled: true,
      draft: true,
      preview: false,
      deleted: false,
    };
    setRows((current) => [...current, row]);
    setSelectedID(row.id);
  };

  const removeSelected = (): void => {
    if (!selected) return;
    if (selected.draft) {
      // A removed row leaves no selection, and every control in the detail
      // column reads the selected row: the next one (else the one above it)
      // takes over, so the column states a key's facts instead of going blank.
      const index = rows.findIndex((row) => row.id === selected.id);
      const neighbour = rows[index + 1] ?? rows[index - 1];
      setRows((current) => current.filter((row) => row.id !== selected.id));
      setSelectedID(neighbour?.id ?? "");
      return;
    }
    updateRow(selected.id, { deleted: !selected.deleted });
  };

  const apply = (): void => {
    const result = stagedResult();
    if (!result) return;
    setNote(translate("common.saving"));
    requestRelayGroupApply(result);
  };

  /**
   * Copy the selected key.  A copy that worked says nothing — the key is on the
   * clipboard — so only a failure takes the strip, and it clears itself.
   */
  const copySelected = async (): Promise<void> => {
    if (!selected || !selected.hint || selected.draft) return;
    const ok = await copyKey(state.accountId, selected.id).catch(() => false);
    if (ok) return;
    setNote(labels.failedLabel);
    setTimeout(() => setNote((current) => (current === labels.failedLabel ? undefined : current)), 4000);
  };

  const detailEditable = editing && selected !== undefined && !selected.deleted;
  const pickerOptions = groups.length > 0 ? groups : [{ id: "", label: labels.ungroupedLabel, name: labels.ungroupedLabel, rate: "" }];
  const selectedGroupIndex = selected ? pickerOptions.findIndex((group) => group.id === selected.groupID) : -1;
  const selectedGroupLabel = selectedGroupIndex >= 0 ? pickerOptions[selectedGroupIndex].label : labels.ungroupedLabel;

  return <View style={styles.root}>
    <View style={styles.body}>
      <View style={styles.listColumn}>
        <View style={styles.listHeader}>
          <Text style={styles.sectionTitle}>{labels.listLabel}</Text>
          <View style={styles.listActions}>
            <NativeButton title="" symbol="plus" compact toolTip={labels.addLabel} accessibilityLabel={labels.addLabel} disabled={!editing || groups.length === 0} onPress={addDraft} style={styles.iconButton} />
            <NativeButton title="" symbol="minus" compact toolTip={labels.removeLabel} accessibilityLabel={labels.removeLabel} disabled={!editing || selected === undefined} onPress={removeSelected} style={styles.iconButton} />
          </View>
        </View>
        <NativeTable
          columns={[
            { label: labels.nameLabel, width: 148 },
            { label: labels.groupLabel, width: 110 },
            { label: labels.multiplierLabel, width: 62 },
          ]}
          rows={rows.map((row) => ({
            key: row.id,
            cells: [row.name, groupLabelFor(row), rateFor(row)],
          }))}
          selectedKey={selectedID}
          secondaryRowKeys={rows.filter((row) => staged(row) || (autoGrouping && baseline.current?.keys.has(row.id))).map((row) => row.id)}
          alertRowKeys={rows.filter((row) => row.deleted || invalidName(row)).map((row) => row.id)}
          compact
          cellHorizontalPadding={8}
          firstColumnHorizontalPadding={8}
          onSelectionChange={(key) => setSelectedID(key)}
          style={styles.list}
        />
      </View>
      <View style={styles.detailColumn}>
        <NativeCheckbox label={labels.enabledLabel} value={selected?.enabled === true && selected?.deleted !== true} disabled={!detailEditable} onValueChange={(value) => { if (selected) updateRow(selected.id, { enabled: value }); }} />
        <View style={styles.field}>
          <Text style={styles.fieldLabel}>{labels.nameLabel}</Text>
          {/* The row's name is what makes the key addressable, so a cleared one
              is stated as the field being wrong — React Native's own props
              have no invalid state, so this is the DOM attribute the page
              reads (RN for Web forwards it; the native fields ignore it). */}
          <NativeTextField value={selected?.name ?? ""} editable={detailEditable} onChangeText={(value) => { if (selected) updateRow(selected.id, { name: value }); }} accessibilityLabel={labels.nameLabel} style={selected && invalidName(selected) ? [styles.control, styles.controlInvalid] : styles.control} {...({ ...(selected !== undefined && invalidName(selected) ? { "aria-invalid": true } : {}) } as object)} />
        </View>
        <View style={styles.field}>
          <Text style={styles.fieldLabel}>{labels.groupLabel}</Text>
          <NativePicker
            labels={pickerOptions.map((group) => group.label)}
            selectedValue={selectedGroupLabel}
            disabled={!detailEditable || pickerOptions.length === 0}
            accessibilityLabel={labels.groupLabel}
            onChange={({ nativeEvent }) => {
              const group = pickerOptions[nativeEvent.index];
              if (!selected || !group) return;
              updateRow(selected.id, { groupID: group.id, groupLabel: group.name, multiplier: group.rate });
            }}
            style={styles.control}
          />
        </View>
        <View style={styles.field}>
          <Text style={styles.fieldLabel}>{labels.multiplierLabel}</Text>
          <Text numberOfLines={1} style={styles.value}>{selected && rateFor(selected) !== "" ? rateFor(selected) : labels.emptyLabel}</Text>
        </View>
        <View style={styles.field}>
          <Text style={styles.fieldLabel}>{labels.valueLabel}</Text>
          <View style={styles.valueRow}>
            {/* The key itself never enters this tree: the copy action goes
                through Core's lease and stops at the clipboard, exactly as the
                native sheet's copy button does. */}
            <Text numberOfLines={1} style={styles.value}>{selected && selected.hint !== "" ? "••••••••" : labels.emptyLabel}</Text>
            <NativeButton title="" symbol="copy" compact toolTip={labels.copyLabel} accessibilityLabel={labels.copyLabel} disabled={!selected || selected.hint === "" || selected.draft} onPress={() => { void copySelected(); }} style={styles.iconButton} />
          </View>
        </View>
        <Text style={styles.fieldLabel}>{labels.modelsLabel}</Text>
        <ScrollView style={styles.models}>{selected && selected.models.length > 0
          ? selected.models.slice(0, MODEL_LINES).map((model) => <Text key={model} numberOfLines={1} style={styles.modelName}>{model}</Text>)
          : <Text style={styles.modelName}>{labels.emptyLabel}</Text>}</ScrollView>
      </View>
    </View>
    <View style={styles.footer}>
      {/* The switch states what it does before it is turned on, and the help
          belongs to the switch: the two travel as one block, so the sentence
          wraps under it instead of squeezing the buttons beside it. */}
      <View style={styles.footerSwitch}>
        <NativeCheckbox label={labels.autoGroupingLabel} value={autoGrouping} disabled={!composing || layoutGroups.length === 0} onValueChange={toggleAutoGrouping} />
        <Text numberOfLines={2} style={styles.help}>{labels.autoGroupingHelp}</Text>
      </View>
      <View style={styles.footerActions}>
        <Text numberOfLines={1} style={styles.status}>{note ?? ""}</Text>
        <View style={styles.footerSpacer} />
        <NativeButton title={labels.closeLabel} compact disabled={state.saving} onPress={askCloseRelayGroupSheet} />
        <NativeButton title={labels.applyLabel} primary compact disabled={!hasStagedChanges || hasInvalidName || state.saving || state.loading} onPress={apply} />
      </View>
    </View>
  </View>;
}

const styles = StyleSheet.create({
  root: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: WINDOWS_CONTROL.fill },
  body: { flex: 1, minHeight: 0, flexDirection: "row", gap: 14, paddingHorizontal: 14, paddingTop: 12 },
  listColumn: { width: 344, flexShrink: 0, minHeight: 0, gap: 6 },
  listHeader: { minHeight: 22, flexDirection: "row", alignItems: "center" },
  sectionTitle: { flex: 1, color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  listActions: { flexDirection: "row", gap: 4 },
  iconButton: { width: 22, minWidth: 22, height: 22 },
  list: { flex: 1, minHeight: 0 },
  detailColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 8 },
  field: { flexDirection: "row", alignItems: "center", gap: 8 },
  fieldLabel: { width: 56, flexShrink: 0, color: WINDOWS_CONTROL.textSecondary, fontSize: UI_FONT_SIZE },
  control: { flex: 1, minWidth: 0 },
  // The panel's own invalid field: the same 1 pt danger frame the runtime pane
  // draws on a value it will not write.  The frame is a React view on Windows
  // and the page, so the mark is visible there; the AppKit field draws its own
  // bezel and keeps the row's warning ink as its share of the mark.
  controlInvalid: { borderWidth: 1, borderColor: WINDOWS_CONTROL.danger, borderRadius: WINDOWS_CONTROL.radius },
  value: { flexShrink: 1, minWidth: 0, color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE },
  valueRow: { flex: 1, minWidth: 0, flexDirection: "row", alignItems: "center", gap: 6 },
  models: { flex: 1, minHeight: 40, borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, borderRadius: WINDOWS_CONTROL.radius, paddingHorizontal: 8, paddingVertical: 4 },
  modelName: { color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE, lineHeight: 17 },
  footer: { flexShrink: 0, gap: 6, paddingHorizontal: 14, paddingTop: 8, paddingBottom: 10, borderTopWidth: 1, borderTopColor: WINDOWS_CONTROL.strokeSubtle },
  footerSwitch: { minWidth: 0, gap: 2 },
  help: { color: WINDOWS_CONTROL.textSecondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 },
  footerActions: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 },
  footerSpacer: { flex: 1, minWidth: 8 },
  status: { maxWidth: 200, color: WINDOWS_CONTROL.textSecondary, fontSize: UI_FONT_SIZE },
});
