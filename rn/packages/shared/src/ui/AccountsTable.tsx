import React from "react";
import { PlatformColor, StyleSheet, Text, View } from "react-native";
import { NativeTable } from "./NativeControls";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

/**
 * The account list of a provider-with-accounts: a title row whose right side
 * carries the list's own buttons, then the table, then nothing.
 *
 * Both panes that own accounts draw this same list — a relay station and the
 * OpenAI row — so the shape (title, ±, 账号 / 状态 columns, empty state) lives
 * here once. What differs is only what each pane puts around it: a station
 * adds a third 余额 column and a session editor below; the OpenAI row adds a
 * 退出登录 row for the account the list highlights.
 */
export type AccountsTableRow = { key: string; cells: string[] };
export type AccountsTableColumn = { label: string; width: number };

export function AccountsTable({
  title,
  actions,
  columns,
  rows,
  selectedKey,
  onSelectionChange,
  alertRowKeys,
  emptyText,
  tableHeight = 84,
  alertRowKeysHint: _alertRowKeysHint,
}: {
  title: string;
  /** The list's own buttons, drawn to the right of the title. */
  actions?: React.ReactNode;
  columns: AccountsTableColumn[];
  rows: AccountsTableRow[];
  selectedKey?: string;
  onSelectionChange?: (key: string) => void;
  /** Rows whose login has gone stale; the native table paints them brown. */
  alertRowKeys?: string[];
  emptyText: string;
  /** Room for the rows; a longer list keeps its own height and scrolls. */
  tableHeight?: number;
  alertRowKeysHint?: string;
}): React.JSX.Element {
  return <View style={styles.panel}>
    <View style={styles.header}>
      <Text style={styles.title}>{title}</Text>
      {actions ? <View style={styles.actions}>{actions}</View> : null}
    </View>
    {rows.length > 0 ? <NativeTable
      columns={columns}
      rows={rows}
      selectedKey={selectedKey ?? ""}
      disabledRowKeys={[]}
      alertRowKeys={alertRowKeys ?? []}
      compact
      striped
      onSelectionChange={(key) => onSelectionChange?.(key)}
      style={[styles.table, { height: tableHeight, minHeight: tableHeight }]}
    /> : <View style={styles.empty}><Text style={styles.emptyText}>{emptyText}</Text></View>}
  </View>;
}

const colors = {
  text: PlatformColor("labelColor"),
  secondary: PlatformColor("secondaryLabelColor"),
  separator: PlatformColor("separatorColor"),
  panel: PlatformColor("controlBackgroundColor"),
};

const styles = StyleSheet.create({
  panel: { minWidth: 0, gap: 6 },
  header: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  title: { flex: 1, color: colors.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  actions: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 },
  table: { flexGrow: 0, flexShrink: 0 },
  empty: { minHeight: 40, alignItems: "center", justifyContent: "center", paddingHorizontal: 10, paddingVertical: 8, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel },
  emptyText: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, textAlign: "center" },
});
