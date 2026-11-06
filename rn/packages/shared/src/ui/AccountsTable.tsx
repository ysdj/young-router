import React from "react";
import { Platform, PlatformColor, StyleSheet, Text, View } from "react-native";
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
  // Three rows plus the header, in each host's own metrics: the compact
  // AppKit table draws a 24 pt header over 22 pt rows, the Windows and browser
  // tables a 30 pt one over 30 pt rows, and both carry the table's own 1 pt
  // border on each side.  So 24 + 3 * 22 + 2 = 92 and 30 + 3 * 30 + 2 = 122.
  // The old 84 was one account's height on macOS — 58 pt of body, two rows and
  // a clipped third — and 116 was 6 pt short on the 30 pt hosts, which is the
  // same symptom the comment used to name while still producing it.
  tableHeight = Platform.OS === "macos" ? 92 : 122,
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

// The browser host has no AppKit semantic colors, and React Native for Web
// answers `PlatformColor` with the literal name it was given -- which CSS then
// ignores, so a pane would paint in the browser's default ink. The third
// argument is the same sRGB literal the shared `semanticColor` already keeps
// for exactly this host.
function color(name: string, fallback: string): ReturnType<typeof PlatformColor> | string {
  return Platform.OS === "web" ? fallback : PlatformColor(name);
}

const colors = {
  text: color("labelColor", "#1d1d1f"),
  secondary: color("secondaryLabelColor", "#6e6e73"),
  separator: color("separatorColor", "#d4d4d8"),
  panel: color("controlBackgroundColor", "#ffffff"),
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
