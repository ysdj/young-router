import React from "react";
import {
  StyleSheet,
  type HostInstance,
  type NativeSyntheticEvent,
  type StyleProp,
  type TextInputProps,
  type ViewStyle,
} from "react-native";
import NativeAppKitButton from "./macos/NativeButtonNativeComponent";
import NativeAppKitCheckbox from "./macos/NativeCheckboxNativeComponent";
import NativeAppKitPicker from "./macos/NativePickerNativeComponent";
import NativeAppKitSegmentedControl from "./macos/NativeSegmentedNativeComponent";
import NativeAppKitSelectableRow from "./macos/NativeSelectableRowNativeComponent";
import NativeAppKitTextField from "./macos/NativeTextFieldNativeComponent";
import NativeAppKitSwitch from "./macos/NativeToggleNativeComponent";
import NativeAppKitTable from "./macos/NativeTableNativeComponent";
import NativeAppKitTextEditor from "./macos/NativeTextEditorNativeComponent";
import NativeAppKitSecureTextEditor from "./macos/NativeSecureTextEditorNativeComponent";
import NativeAppKitSecureTextInput from "./macos/NativeSecureTextInputNativeComponent";
import NativeAppKitSplitView from "./macos/NativeSplitViewNativeComponent";

export type NativeButtonProps = {
  title: string;
  symbol?: string;
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

type NativeSegmentedProps = {
  labels: string[];
  selectedValue: string;
  disabled?: boolean;
  compact?: boolean;
  onChange?: (event: NativeSyntheticEvent<{ value: string; index: number }>) => void;
  style?: StyleProp<ViewStyle>;
};

type NativePickerProps = {
  labels: string[];
  selectedValue: string;
  disabled?: boolean;
  compact?: boolean;
  onChange?: (event: NativeSyntheticEvent<{ value: string; index: number }>) => void;
  style?: StyleProp<ViewStyle>;
};

export const AppKitButton = React.forwardRef<any, NativeButtonProps>(function AppKitButton(props, ref): React.JSX.Element {
  return <NativeAppKitButton {...props} ref={ref as never} style={[props.link ? styles.linkButton : styles.button, props.style]} />;
});

export function AppKitSegmentedControl({ labels, selectedValue, disabled, compact, onChange, style, ref }: NativeSegmentedProps & { ref?: React.Ref<any> }): React.JSX.Element {
  return <NativeAppKitSegmentedControl ref={ref as never} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={[styles.segmented, style]} />;
}

export function AppKitPicker({ labels, selectedValue, disabled, compact, onChange, style }: NativePickerProps): React.JSX.Element {
  return <NativeAppKitPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={[styles.picker, style]} />;
}

export function AppKitCheckbox({ label, value, disabled, compact, labelVisible, onValueChange, style }: { label: string; value: boolean; disabled?: boolean; compact?: boolean; labelVisible?: boolean; onValueChange?: (value: boolean) => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitCheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={(event) => onValueChange?.(event.nativeEvent.value)} style={[styles.checkbox, style]} />;
}

export function AppKitTextField({ style, onChangeText, onSubmitEditing, onBlur, ...props }: TextInputProps): React.JSX.Element {
  return <NativeAppKitTextField value={props.value} placeholder={props.placeholder} multiline={props.multiline} secureTextEntry={props.secureTextEntry} disabled={props.editable === false} onChangeText={(event) => onChangeText?.(event.nativeEvent.text)} onBlur={() => onBlur?.({} as never)} onSubmitEditing={(event) => onSubmitEditing?.({ ...event, nativeEvent: { text: event.nativeEvent.text } } as never)} accessibilityLabel={props.accessibilityLabel} style={[styles.textField, style]} />;
}

export function AppKitToggle({ value, disabled, onValueChange, style, accessibilityLabel }: { value: boolean; disabled?: boolean; onValueChange?: (value: boolean) => void; style?: StyleProp<ViewStyle>; accessibilityLabel?: string }): React.JSX.Element {
  return <NativeAppKitSwitch value={value} disabled={disabled} onValueChange={(event) => onValueChange?.(event.nativeEvent.value)} accessibilityLabel={accessibilityLabel} style={style} />;
}

export function AppKitSelectableRow({ title, detail, selected, disabled, onPress, style }: { title: string; detail?: string; selected?: boolean; disabled?: boolean; onPress?: () => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitSelectableRow title={title} detail={detail} selected={selected} disabled={disabled} onPress={() => onPress?.()} style={[styles.selectableRow, style]} />;
}

export function AppKitTable({ columnLabels, columnWidths, rowKeys, cells, selectedKey, alternatingRows, compact, followBottom, disabledRowKeys, secondaryCellKeys, spanningRowKeys, onSelectionChange, onRowDoublePress, style }: { columnLabels: string[]; columnWidths: number[]; rowKeys: string[]; cells: string[]; selectedKey: string; alternatingRows?: boolean; compact?: boolean; followBottom?: boolean; disabledRowKeys?: string[]; secondaryCellKeys?: string[]; spanningRowKeys?: string[]; onSelectionChange?: (event: NativeSyntheticEvent<{ key: string; index: number }>) => void; onRowDoublePress?: (event: NativeSyntheticEvent<{ key: string; index: number }>) => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitTable columnLabels={columnLabels} columnWidths={columnWidths} rowKeys={rowKeys} cells={cells} selectedKey={selectedKey} alternatingRows={alternatingRows} compact={compact} followBottom={followBottom} disabledRowKeys={disabledRowKeys} secondaryCellKeys={secondaryCellKeys} spanningRowKeys={spanningRowKeys} onSelectionChange={onSelectionChange} onRowDoublePress={onRowDoublePress} style={[styles.table, style]} />;
}

export function AppKitTextEditor({ value, documentKey, readOnly, wrap, onChangeText, style }: { value: string; documentKey?: string; readOnly?: boolean; wrap?: boolean; onChangeText?: (text: string) => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitTextEditor value={value} documentKey={documentKey} readOnly={readOnly} wrap={wrap} onChangeText={(event) => onChangeText?.(event.nativeEvent.text)} style={[styles.editor, style]} />;
}

export type SecureTextEditorState = {
  revision: number;
  status: string;
  error: string;
};

export function AppKitSecureTextEditor({ editorToken, language, onEditorState, style }: { editorToken: string; language: string; onEditorState?: (event: NativeSyntheticEvent<SecureTextEditorState>) => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitSecureTextEditor editorToken={editorToken} language={language} onEditorState={onEditorState} style={[styles.editor, style]} />;
}

export type SecureTextInputState = {
  revision: number;
  present: boolean;
  status: string;
  error: string;
  commitRequest: number;
};

export function AppKitSecureTextInput({ domain, field, target = "", label, placeholder, plainText = false, autoCommit = false, disabled, commitRequest, resetRequest, onSecretState, style }: { domain: string; field: string; target?: string; label: string; placeholder?: string; plainText?: boolean; autoCommit?: boolean; disabled?: boolean; commitRequest?: number; resetRequest?: number; onSecretState?: (event: NativeSyntheticEvent<SecureTextInputState>) => void; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={placeholder} plainText={plainText} autoCommit={autoCommit} disabled={disabled} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={onSecretState} style={[styles.textField, style]} />;
}

export function AppKitSplitView({ paneWidth, minPaneWidth, maxPaneWidth, paneOpen, disabled, onPaneWidthChange, children, style }: { paneWidth: number; minPaneWidth: number; maxPaneWidth: number; paneOpen?: boolean; disabled?: boolean; onPaneWidthChange?: (width: number) => void; children?: React.ReactNode; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <NativeAppKitSplitView paneWidth={paneWidth} minPaneWidth={minPaneWidth} maxPaneWidth={maxPaneWidth} paneOpen={paneOpen} disabled={disabled} onPaneWidthChange={(event) => onPaneWidthChange?.(event.nativeEvent.width)} style={[styles.splitView, style]}>{children}</NativeAppKitSplitView>;
}

const styles = StyleSheet.create({
  button: { minWidth: 72, height: 28 },
  linkButton: { minWidth: 72, minHeight: 22 },
  segmented: { width: 224, height: 28 },
  picker: { minWidth: 160, height: 26 },
  checkbox: { minHeight: 22 },
  textField: { minHeight: 26 },
  selectableRow: { minHeight: 44 },
  table: { minHeight: 120 },
  editor: { minHeight: 160 },
  splitView: { minHeight: 120 },
});
