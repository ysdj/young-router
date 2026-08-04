import React from "react";
import {
  Platform,
  Pressable,
  StyleSheet,
  Switch,
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
  AppKitPicker,
  AppKitSegmentedControl,
  AppKitSecureTextEditor,
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
import WinUISecureTextEditor from "./windows/NativeSecureTextEditorNativeComponent";
import WinUISecureTextInput from "./windows/NativeSecureTextInputNativeComponent";
import WinUITextInput from "./windows/NativeTextInputNativeComponent";
import WinUIToggle from "./windows/NativeToggleNativeComponent";
import WinUISelectableRow from "./windows/NativeSelectableRowNativeComponent";
import WinUISplitView from "./windows/NativeSplitViewNativeComponent";
import WinUITable from "./windows/NativeTableNativeComponent";
import WinUITextEditor from "./windows/NativeTextEditorNativeComponent";

type ButtonProps = {
  title: string;
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
  onValueChange?: (value: boolean) => void;
  style?: StyleProp<ViewStyle>;
};

// Fabric lays out an opaque native component before AppKit/WinUI has an
// opportunity to report the checkbox title's intrinsic size.  Without an
// explicit minimum, a row-direction parent can therefore allocate only the
// checkmark square and clip the label.  Keep the sizing policy shared so a
// translated label is visible on both desktop hosts.
function nativeCheckboxMinimumWidth(label: string): number {
  const textWidth = Array.from(label).reduce((width, character) => {
    if (/\s/u.test(character)) return width + 4;
    return width + (/[^\u0000-\u024f]/u.test(character) ? 13 : 7.5);
  }, 0);
  return Math.ceil(26 + Math.max(24, textWidth));
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
};

type TableProps = {
  columns: TableColumn[];
  rows: TableRow[];
  selectedKey?: string;
  alternatingRows?: boolean;
  compact?: boolean;
  followBottom?: boolean;
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

export type SecureTextEditorState = Readonly<{
  revision: number;
  status: string;
  error: string;
}>;

export type SecureTextEditorProps = {
  editorToken: string;
  language: string;
  onEditorState?: (state: SecureTextEditorState) => void;
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
  const style = [props.link ? styles.linkButton : styles.button, props.style];
  if (Platform.OS === "windows") {
    return <WinUIButton {...props} ref={ref as never} onPress={() => props.onPress?.()} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitButton {...props} ref={ref as never} />;
  }
  return <Pressable ref={ref as never} disabled={props.disabled} onPress={props.onPress} style={style} accessibilityRole={props.link ? "link" : "button"}><Text>{props.title}</Text></Pressable>;
});

// Keep the long-standing function export while allowing desktop callers to
// pass a native ref for anchored menus. React 19 exposes ref as a prop.
export function NativeButton(props: ButtonProps & { ref?: React.Ref<any> }): React.JSX.Element {
  return <NativeButtonWithRef {...props} ref={props.ref} />;
}

const NativeSegmentedControlWithRef = React.forwardRef<any, SegmentedProps>(function NativeSegmentedControlWithRef({ labels, selectedValue, disabled, compact, onChange, style }, ref): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUISegmented ref={ref as never} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={(event) => onChange?.({ ...event, nativeEvent: event.nativeEvent })} style={[styles.segmented, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSegmentedControl ref={ref} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  return <Pressable ref={ref as never} style={[styles.button, style]} disabled={disabled} accessibilityRole="tab"><Text>{labels.join(" / ")} ({selectedValue})</Text></Pressable>;
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
  return <Switch value={value} disabled={disabled} onValueChange={onValueChange} style={style} accessibilityLabel={accessibilityLabel} />;
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

export function NativeCheckbox({ label, value, disabled, compact, onValueChange, style }: CheckboxProps): React.JSX.Element {
  const sizedStyle = [{ minWidth: nativeCheckboxMinimumWidth(label) }, style];
  if (Platform.OS === "windows") {
    return <WinUICheckbox label={label} value={value} disabled={disabled} compact={compact} onValueChange={(event) => onValueChange?.(event.nativeEvent.value)} style={sizedStyle} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitCheckbox label={label} value={value} disabled={disabled} compact={compact} onValueChange={onValueChange} style={sizedStyle} />;
  }
  return <Pressable style={[styles.checkboxFallback, sizedStyle]} disabled={disabled} onPress={() => onValueChange?.(!value)} accessibilityRole="checkbox" accessibilityState={{ checked: value, disabled }}><Switch value={value} disabled={disabled} pointerEvents="none" /><Text>{label}</Text></Pressable>;
}

export function NativePicker({ labels, selectedValue, disabled, compact, onChange, style }: PickerProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUIPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={(event) => onChange?.({ ...event, nativeEvent: event.nativeEvent })} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  return <NativeSegmentedControl labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
}


export function NativeTable({ columns, rows, selectedKey = "", alternatingRows = false, compact = false, followBottom = false, disabledRowKeys = [], secondaryCellKeys = [], onSelectionChange, onRowDoublePress, style }: TableProps): React.JSX.Element {
  const nativeProps = {
    columnLabels: columns.map((column) => column.label),
    columnWidths: columns.map((column) => column.width),
    rowKeys: rows.map((row) => row.key),
    cells: rows.flatMap((row) => row.cells),
    selectedKey,
    alternatingRows,
    compact,
    followBottom,
    disabledRowKeys,
    secondaryCellKeys,
  };
  if (Platform.OS === "windows") {
    return <WinUITable {...nativeProps} onSelectionChange={(event) => onSelectionChange?.(event.nativeEvent.key, event.nativeEvent.index)} onRowDoublePress={(event) => onRowDoublePress?.(event.nativeEvent.key, event.nativeEvent.index)} style={[styles.table, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitTable {...nativeProps} onSelectionChange={(event) => onSelectionChange?.(event.nativeEvent.key, event.nativeEvent.index)} onRowDoublePress={(event) => onRowDoublePress?.(event.nativeEvent.key, event.nativeEvent.index)} style={[styles.table, style]} />;
  }
  return <View style={[styles.tableFallback, style]}>{rows.map((row, index) => <Pressable key={row.key} onPress={() => onSelectionChange?.(row.key, index)} onLongPress={() => onRowDoublePress?.(row.key, index)}><NativeSelectableRow title={row.cells[0] ?? ""} detail={row.cells.slice(1).join("  ")} selected={row.key === selectedKey} /></Pressable>)}</View>;
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

export function NativeSecureTextEditor({ editorToken, language, onEditorState, style, unavailableLabel }: SecureTextEditorProps & { unavailableLabel: string }): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUISecureTextEditor editorToken={editorToken} language={language} onEditorState={(event) => onEditorState?.(event.nativeEvent)} style={[styles.editor, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSecureTextEditor editorToken={editorToken} language={language} onEditorState={(event) => onEditorState?.(event.nativeEvent)} style={[styles.editor, style]} />;
  }
  return <View accessibilityLabel={unavailableLabel} style={[styles.secureEditorUnavailable, style]}><Text style={styles.secureEditorUnavailableText}>{unavailableLabel}</Text></View>;
}

/** Native password leaves never pass secret text through React. */
export function NativeSecureTextInput({ domain, field, target = "", label, placeholder, plainText = false, autoCommit = false, disabled, commitRequest, resetRequest, onSecretState, style }: SecureTextInputProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WinUISecureTextInput domain={domain} field={field} target={target} label={label} placeholder={placeholder} plainText={plainText} autoCommit={autoCommit} disabled={disabled} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(event) => onSecretState?.(event.nativeEvent)} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={placeholder} plainText={plainText} autoCommit={autoCommit} disabled={disabled} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={(event) => onSecretState?.(event.nativeEvent)} style={style} />;
  }
  return <View accessibilityLabel={label} style={style} />;
}

export function NativeSplitView({ paneWidth, minPaneWidth, maxPaneWidth, paneOpen = true, disabled, onPaneWidthChange, children, style }: SplitViewProps): React.JSX.Element {
  const panes = React.Children.toArray(children);
  const leading = panes[0];
  const trailing = panes[1];
  if (Platform.OS === "windows") {
    return <View style={[styles.splitFallback, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><WinUISplitView paneWidth={paneWidth} minPaneWidth={minPaneWidth} maxPaneWidth={maxPaneWidth} paneOpen={paneOpen} disabled={disabled} onPaneWidthChange={(event) => onPaneWidthChange?.(event.nativeEvent.width)} style={styles.splitter} /><View style={styles.splitTrailing}>{trailing}</View></View>;
  }
  if (Platform.OS === "macos") {
    return <AppKitSplitView paneWidth={paneWidth} minPaneWidth={minPaneWidth} maxPaneWidth={maxPaneWidth} paneOpen={paneOpen} disabled={disabled} onPaneWidthChange={onPaneWidthChange} style={[styles.splitView, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></AppKitSplitView>;
  }
  return <View style={[styles.splitFallback, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></View>;
}

const styles = StyleSheet.create({
  button: { minWidth: 72, height: 28 },
  linkButton: { minWidth: 72, minHeight: 22 },
  segmented: { minHeight: 28 },
  checkboxFallback: { minHeight: 32, flexDirection: "row", alignItems: "center", gap: 8 },
  selectableRow: { minHeight: 44, justifyContent: "center", paddingHorizontal: 10 },
  selectableRowSelected: { backgroundColor: "#dbeafe" },
  selectableText: { gap: 2 },
  selectableTitle: { fontSize: 13 },
  selectableDetail: { fontSize: 11, opacity: 0.65 },
  table: { minHeight: 120 },
  // Let the surrounding window provide scrolling when a fallback host needs
  // it; `overflow: scroll` would force an empty scrollbar gutter on every
  // table, even when all rows fit.
  tableFallback: { minHeight: 120 },
  editor: { minHeight: 160 },
  editorFallback: { minHeight: 160, textAlignVertical: "top" },
  secureEditorUnavailable: { minHeight: 160, justifyContent: "center", alignItems: "center" },
  secureEditorUnavailableText: { fontSize: 13, opacity: 0.65 },
  splitView: { minHeight: 120, flexDirection: "row" },
  splitFallback: { minHeight: 120, flexDirection: "row" },
  splitLeading: { minWidth: 0, flexShrink: 0 },
  splitter: { width: 6, minWidth: 6, alignSelf: "stretch", zIndex: 1 },
  splitTrailing: { minWidth: 0, flex: 1 },
});
