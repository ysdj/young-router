import React from "react";
import {
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  type NativeSyntheticEvent,
  type StyleProp,
  type TextInputProps,
  type ViewStyle,
} from "react-native";
import {
  AppKitButton,
  AppKitCheckbox,
  AppKitPersistentScrollIndicator,
  AppKitPicker,
  AppKitSegmentedControl,
  AppKitSecureTextInput,
  AppKitSelectableRow,
  AppKitSplitView,
  AppKitTable,
  AppKitTextEditor,
  AppKitTextField,
  AppKitToggle,
} from "./AppKitControls";
import WinUIButton from "./windows/NativeButtonNativeComponent";
import WinUICheckbox from "./windows/NativeCheckboxNativeComponent";
import WinUIPicker from "./windows/NativePickerNativeComponent";
import WinUISegmented from "./windows/NativeSegmentedNativeComponent";
import WinUISecureTextInput from "./windows/NativeSecureTextInputNativeComponent";
import WinUITextInput from "./windows/NativeTextInputNativeComponent";
import WinUIToggle from "./windows/NativeToggleNativeComponent";
import WinUISelectableRow from "./windows/NativeSelectableRowNativeComponent";
import WinUISplitView from "./windows/NativeSplitViewNativeComponent";
import WinUITable from "./windows/NativeTableNativeComponent";
import WinUITextEditor from "./windows/NativeTextEditorNativeComponent";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

type ButtonProps = {
  title: string;
  symbol?: "check" | "close" | "copy" | "edit" | "import" | "minus" | "pause" | "play" | "plus" | "power-off" | "power-on" | "refresh" | "trash";
  toolTip?: string;
  accessibilityLabel?: string;
  disabled?: boolean;
  primary?: boolean;
  destructive?: boolean;
  compact?: boolean;
  link?: boolean;
  onPress?: () => void;
  style?: StyleProp<ViewStyle>;
};

type SegmentedProps = {
  labels: string[];
  selectedValue: string;
  disabled?: boolean;
  compact?: boolean;
  onChange?: (event: NativeSyntheticEvent<{ value: string; index: number }>) => void;
  style?: StyleProp<ViewStyle>;
};

type ToggleProps = {
  value: boolean;
  disabled?: boolean;
  onValueChange?: (value: boolean) => void;
  style?: StyleProp<ViewStyle>;
  accessibilityLabel?: string;
};

type SelectableRowProps = {
  title: string;
  detail?: string;
  selected?: boolean;
  disabled?: boolean;
  onPress?: () => void;
  style?: StyleProp<ViewStyle>;
};

type CheckboxProps = {
  label: string;
  value: boolean;
  disabled?: boolean;
  compact?: boolean;
  labelVisible?: boolean;
  onValueChange?: (value: boolean) => void;
  style?: StyleProp<ViewStyle>;
};

function nativeControlTextWidth(label: string): number {
  return Array.from(label).reduce((width, character) => {
    if (/\s/u.test(character)) return width + 4;
    return width + (/[^\u0000-\u024f]/u.test(character) ? 13 : 7.5);
  }, 0);
}

// Fabric lays out opaque native controls before AppKit/WinUI can report their
// intrinsic content size. Give translated labels enough shared layout width so
// buttons and checkboxes do not turn complete actions into truncated fragments.
function nativeButtonMinimumWidth(title: string, compact = false): number {
  return Math.max(72, Math.ceil((compact ? 32 : 38) + nativeControlTextWidth(title) * 1.15));
}

function nativeCheckboxMinimumWidth(label: string): number {
  const textWidth = nativeControlTextWidth(label);
  return Math.ceil(26 + Math.max(24, textWidth));
}

function StaticBooleanIndicator({ value }: { value: boolean }): React.JSX.Element {
  return <View style={[styles.staticBooleanIndicator, value && styles.staticBooleanIndicatorChecked]}><Text style={styles.staticBooleanIndicatorText}>{value ? "x" : ""}</Text></View>;
}

type PickerProps = {
  labels: string[];
  selectedValue: string;
  disabled?: boolean;
  compact?: boolean;
  onChange?: (event: NativeSyntheticEvent<{ value: string; index: number }>) => void;
  style?: StyleProp<ViewStyle>;
};

type TableColumn = {
  label: string;
  width: number;
};

type TableRow = {
  key: string;
  cells: string[];
  spanning?: boolean;
};

type TableProps = {
  columns: TableColumn[];
  rows: TableRow[];
  selectedKey?: string;
  striped?: boolean;
  alternatingRows?: boolean;
  compact?: boolean;
  followBottom?: boolean;
  framed?: boolean;
  cellHorizontalPadding?: number;
  firstColumnHorizontalPadding?: number;
  preserveColumnWidths?: boolean;
  scrollTrailingColumnOverflow?: boolean;
  disabledRowKeys?: string[];
  secondaryCellKeys?: string[];
  onSelectionChange?: (key: string, index: number) => void;
  onRowDoublePress?: (key: string, index: number) => void;
  style?: StyleProp<ViewStyle>;
};

type TextEditorProps = {
  value: string;
  documentKey?: string;
  readOnly?: boolean;
  wrap?: boolean;
  onChangeText?: (text: string) => void;
  style?: StyleProp<ViewStyle>;
};

export type SecureTextInputState = Readonly<{
  revision: number;
  present: boolean;
  status: string;
  error: string;
  commitRequest: number;
}>;

export type SecureTextInputProps = {
  domain: string;
  field: string;
  target?: string;
  label: string;
  placeholder?: string;
  multiline?: boolean;
  plainText?: boolean;
  autoCommit?: boolean;
  disabled?: boolean;
  commitRequest?: number;
  resetRequest?: number;
  onSecretState?: (state: SecureTextInputState) => void;
  style?: StyleProp<ViewStyle>;
};

type SplitViewProps = {
  paneWidth: number;
  minPaneWidth: number;
  maxPaneWidth: number;
  paneOpen?: boolean;
  disabled?: boolean;
  onPaneWidthChange?: (width: number) => void;
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
};

const NativeButtonWithRef = React.forwardRef<any, ButtonProps>(function NativeButtonWithRef(props, ref): React.JSX.Element {
  const compact = props.compact ?? true;
  const buttonProps = {
    ...props,
    compact,
    disabled: props.disabled === true,
    primary: props.primary === true,
    destructive: props.destructive === true,
    link: props.link === true,
  };
  // A caller may enlarge a button, but must never reduce a translated title to
  // an ellipsis. Symbol-only buttons intentionally use their native icon size.
  const titleWidth = !props.symbol ? { minWidth: nativeButtonMinimumWidth(props.title, compact), flexShrink: 0 } : undefined;
  const style = [props.link ? styles.linkButton : styles.button, props.style, titleWidth];
  if (Platform.OS === "windows") {
    return <WinUIButton {...buttonProps} ref={ref as never} onPress={() => props.onPress?.()} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitButton {...buttonProps} ref={ref as never} style={[props.style, titleWidth]} />;
  }
    return <Pressable ref={ref as never} disabled={props.disabled} onPress={props.onPress} style={style} accessibilityRole={props.link ? "link" : "button"}><Text style={styles.controlText}>{props.title}</Text></Pressable>;
});

// Keep the long-standing function export while allowing desktop callers to
// pass a native ref for anchored menus. React 19 exposes ref as a prop.
export function NativeButton(props: ButtonProps & { ref?: React.Ref<any> }): React.JSX.Element {
  return <NativeButtonWithRef {...props} ref={props.ref} />;
}

const NativeSegmentedControlWithRef = React.forwardRef<any, SegmentedProps>(function NativeSegmentedControlWithRef({ labels, selectedValue, disabled, compact = true, onChange, style }, ref): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUISegmented ref={ref as never} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={(event) => onChange?.({ ...event, nativeEvent: event.nativeEvent })} style={[styles.segmented, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSegmentedControl ref={ref} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  return <Pressable ref={ref as never} style={[styles.button, style]} disabled={disabled} accessibilityRole="tab"><Text style={styles.controlText}>{labels.join(" / ")} ({selectedValue})</Text></Pressable>;
});

export function NativeSegmentedControl(props: SegmentedProps & { ref?: React.Ref<any> }): React.JSX.Element {
  return <NativeSegmentedControlWithRef {...props} ref={props.ref} />;
}

export function NativeTextField({ style, onChangeText, onSubmitEditing, ...props }: TextInputProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUITextInput value={props.value} placeholder={props.placeholder} multiline={props.multiline} secureTextEntry={props.secureTextEntry} disabled={props.editable === false} keyboardType={props.keyboardType} onChangeText={(event) => onChangeText?.(event.nativeEvent.text)} onBlur={() => props.onBlur?.({} as never)} onSubmitEditing={(event) => onSubmitEditing?.({ ...event, nativeEvent: { text: event.nativeEvent.text } } as never)} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitTextField {...props} style={style} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} />;
  }
  return <TextInput {...props} style={style} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} />;
}

export function NativeToggle({ value, disabled, onValueChange, style, accessibilityLabel }: ToggleProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUIToggle value={value} disabled={disabled} onValueChange={(event) => onValueChange?.(event.nativeEvent.value)} style={style} accessibilityLabel={accessibilityLabel} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitToggle value={value} disabled={disabled} onValueChange={onValueChange} style={style} accessibilityLabel={accessibilityLabel} />;
  }
  return <Pressable style={[styles.toggleFallback, style]} disabled={disabled} onPress={() => onValueChange?.(!value)} accessibilityRole="switch" accessibilityLabel={accessibilityLabel} accessibilityState={{ checked: value, disabled }}><StaticBooleanIndicator value={value} /></Pressable>;
}

export function NativeSelectableRow({ title, detail, selected, disabled, onPress, style }: SelectableRowProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUISelectableRow title={title} detail={detail} selected={selected} disabled={disabled} onPress={() => onPress?.()} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSelectableRow title={title} detail={detail} selected={selected} disabled={disabled} onPress={onPress} style={style} />;
  }
  return <Pressable style={[styles.selectableRow, selected && styles.selectableRowSelected, style]} disabled={disabled} onPress={onPress} accessibilityRole="button"><View style={styles.selectableText}><Text style={styles.selectableTitle}>{title}</Text>{detail ? <Text style={styles.selectableDetail}>{detail}</Text> : null}</View></Pressable>;
}

export function NativeCheckbox({ label, value, disabled, compact = true, labelVisible = true, onValueChange, style }: CheckboxProps): React.JSX.Element {
  const sizedStyle = [{ minWidth: labelVisible ? nativeCheckboxMinimumWidth(label) : 24 }, style];
  if (Platform.OS === "windows") {
    return <WinUICheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={(event) => onValueChange?.(event.nativeEvent.value)} style={sizedStyle} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitCheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={onValueChange} style={sizedStyle} />;
  }
  return <Pressable style={[styles.checkboxFallback, sizedStyle]} disabled={disabled} onPress={() => onValueChange?.(!value)} accessibilityRole="checkbox" accessibilityLabel={label} accessibilityState={{ checked: value, disabled }}><StaticBooleanIndicator value={value} />{labelVisible ? <Text style={styles.controlText}>{label}</Text> : null}</Pressable>;
}

export function NativePicker({ labels, selectedValue, disabled, compact = true, onChange, style }: PickerProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUIPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={(event) => onChange?.({ ...event, nativeEvent: event.nativeEvent })} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  return <NativeSegmentedControl labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
}


export function NativeTable({ columns, rows, selectedKey = "", striped = true, alternatingRows = false, compact = true, followBottom = false, framed = true, cellHorizontalPadding = 8, firstColumnHorizontalPadding = 8, preserveColumnWidths = false, scrollTrailingColumnOverflow = false, disabledRowKeys = [], secondaryCellKeys = [], onSelectionChange, onRowDoublePress, style }: TableProps): React.JSX.Element {
  const stripedRows = striped && (alternatingRows || rows.length > 0);
  const spanningRowKeys = rows.filter((row) => row.spanning).map((row) => row.key);
  const nativeProps = {
    columnLabels: columns.map((column) => column.label),
    columnWidths: columns.map((column) => column.width),
    rowKeys: rows.map((row) => row.key),
    cells: rows.flatMap((row) => row.cells),
    selectedKey,
    alternatingRows: stripedRows,
    compact,
    followBottom,
    borderless: !framed,
    disabledRowKeys,
    secondaryCellKeys,
    spanningRowKeys,
  };
  if (Platform.OS === "windows") {
    return <WinUITable {...nativeProps} onSelectionChange={(event) => onSelectionChange?.(event.nativeEvent.key, event.nativeEvent.index)} onRowDoublePress={(event) => onRowDoublePress?.(event.nativeEvent.key, event.nativeEvent.index)} style={[styles.table, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitTable {...nativeProps} cellHorizontalPadding={cellHorizontalPadding} firstColumnHorizontalPadding={firstColumnHorizontalPadding} preserveColumnWidths={preserveColumnWidths} scrollTrailingColumnOverflow={scrollTrailingColumnOverflow} onSelectionChange={(event) => onSelectionChange?.(event.nativeEvent.key, event.nativeEvent.index)} onRowDoublePress={(event) => onRowDoublePress?.(event.nativeEvent.key, event.nativeEvent.index)} style={[styles.table, style]} />;
  }
  return <View style={[styles.tableFallback, !framed && styles.tableFallbackUnframed, style]}>{rows.map((row, index) => {
    const selected = row.key === selectedKey;
    const stripe = stripedRows && !selected && index % 2 === 1 ? styles.tableFallbackStripe : undefined;
    if (row.spanning) {
      return <View key={row.key} style={[styles.tableFallbackGroupRow, stripe]}><Text numberOfLines={1} style={[styles.selectableTitle, styles.tableFallbackGroupText]}>{row.cells[0] ?? ""}</Text></View>;
    }
    return <Pressable key={row.key} onPress={() => onSelectionChange?.(row.key, index)} onLongPress={() => onRowDoublePress?.(row.key, index)}><NativeSelectableRow title={row.cells[0] ?? ""} detail={row.cells.slice(1).join(" | ")} selected={selected} style={stripe} /></Pressable>;
  })}</View>;
}

export function NativeTextEditor({ value, documentKey, readOnly, wrap = true, onChangeText, style }: TextEditorProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUITextEditor value={value} documentKey={documentKey} readOnly={readOnly} wrap={wrap} onChangeText={(event) => onChangeText?.(event.nativeEvent.text)} style={[styles.editor, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitTextEditor value={value} documentKey={documentKey} readOnly={readOnly} wrap={wrap} onChangeText={onChangeText} style={[styles.editor, style]} />;
  }
  return <TextInput value={value} editable={!readOnly} multiline onChangeText={onChangeText} style={[styles.editorFallback, style]} />;
}

export function NativePersistentScrollIndicator({ style }: { style?: StyleProp<ViewStyle> }): React.JSX.Element {
  if (Platform.OS === "macos") return <AppKitPersistentScrollIndicator style={style} />;
  return <View pointerEvents="none" style={style} />;
}

/** Native password leaves never pass secret text through React. */
export function NativeSecureTextInput({ domain, field, target = "", label, placeholder, multiline = false, plainText = false, autoCommit = false, disabled, commitRequest, resetRequest, onSecretState, style }: SecureTextInputProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUISecureTextInput domain={domain} field={field} target={target} label={label} placeholder={placeholder} multiline={multiline} plainText={plainText} autoCommit={autoCommit} disabled={disabled} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(event) => onSecretState?.(event.nativeEvent)} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={placeholder} multiline={multiline} plainText={plainText} autoCommit={autoCommit} disabled={disabled} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(event) => onSecretState?.(event.nativeEvent)} style={style} />;
  }
  return <View accessibilityLabel={label} style={style} />;
}

export function NativeSplitView({ paneWidth, minPaneWidth, maxPaneWidth, paneOpen = true, disabled, onPaneWidthChange, children, style }: SplitViewProps): React.JSX.Element {
  const panes = React.Children.toArray(children);
  const leading = panes[0];
  const trailing = panes[1];
  if (!paneOpen) {
    return <View style={[styles.splitFallback, style]}><View style={styles.splitTrailing}>{trailing}</View></View>;
  }
  if (Platform.OS === "windows") {
    return <View style={[styles.splitFallback, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><WinUISplitView paneWidth={paneWidth} minPaneWidth={minPaneWidth} maxPaneWidth={maxPaneWidth} paneOpen={paneOpen} disabled={disabled} onPaneWidthChange={(event) => onPaneWidthChange?.(event.nativeEvent.width)} style={styles.splitter} /><View style={styles.splitTrailing}>{trailing}</View></View>;
  }
  if (Platform.OS === "macos") {
    return <AppKitSplitView paneWidth={paneWidth} minPaneWidth={minPaneWidth} maxPaneWidth={maxPaneWidth} paneOpen={paneOpen} disabled={disabled} onPaneWidthChange={onPaneWidthChange} style={[styles.splitView, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></AppKitSplitView>;
  }
  return <View style={[styles.splitFallback, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></View>;
}

const styles = StyleSheet.create({
  button: { minWidth: 72, height: 24 },
  controlText: { fontSize: UI_FONT_SIZE },
  linkButton: { minWidth: 72, minHeight: 22 },
  segmented: { minHeight: 24 },
  toggleFallback: { minWidth: 24, minHeight: 24, justifyContent: "center" },
  checkboxFallback: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  staticBooleanIndicator: { width: 16, height: 16, borderWidth: 1, borderColor: "#71717a", alignItems: "center", justifyContent: "center", backgroundColor: "#ffffff" },
  staticBooleanIndicatorChecked: { backgroundColor: "#dbeafe", borderColor: "#1d4ed8" },
  staticBooleanIndicatorText: { fontSize: UI_TIP_FONT_SIZE, lineHeight: 14, color: "#111827", fontWeight: "700" },
  selectableRow: { minHeight: 28, justifyContent: "center", paddingHorizontal: 8 },
  selectableRowSelected: { backgroundColor: "#dbeafe" },
  selectableText: { gap: 2 },
  selectableTitle: { fontSize: UI_FONT_SIZE },
  selectableDetail: { fontSize: UI_FONT_SIZE, opacity: 0.65 },
  table: { minHeight: 120 },
  // Let the surrounding window provide scrolling when a fallback host needs
  // it; `overflow: scroll` would force an empty scrollbar gutter on every
  // table, even when all rows fit.
  tableFallback: { minHeight: 120, borderWidth: 1, borderColor: "#d4d4d8", overflow: "hidden" },
  tableFallbackUnframed: { borderWidth: 0 },
  tableFallbackStripe: { backgroundColor: "#f1f1f3" },
  tableFallbackGroupRow: { minHeight: 26, justifyContent: "center", paddingHorizontal: 8 },
  tableFallbackGroupText: { fontWeight: "400" },
  editor: { minHeight: 160 },
  editorFallback: { minHeight: 160, textAlignVertical: "top", fontSize: UI_FONT_SIZE },
  splitView: { minHeight: 120, flexDirection: "row" },
  splitFallback: { minHeight: 120, flexDirection: "row" },
  splitLeading: { minWidth: 0, flexShrink: 0 },
  splitter: { width: 6, minWidth: 6, alignSelf: "stretch", zIndex: 1 },
  splitTrailing: { minWidth: 0, flex: 1 },
});
