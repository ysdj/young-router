import React from "react";
import {
  ActivityIndicator,
  NativeModules,
  Platform,
  Pressable,
  ScrollView,
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
  AppKitBusySpinner,
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
import WinUISecureTextInput from "./windows/NativeSecureTextInputNativeComponent";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";

type ButtonProps = {
  title: string;
  symbol?: "check" | "close" | "copy" | "edit" | "help" | "import" | "info" | "minus" | "pause" | "play" | "plus" | "power-off" | "power-on" | "refresh" | "trash" | "chevron-up" | "chevron-down" | "chevron-up-down";
  /** Keep the title visible next to a leading symbol instead of icon-only. */
  symbolWithTitle?: boolean;
  /** Draw that symbol after the title (menu buttons read "Title ▾"). */
  symbolTrailing?: boolean;
  toolTip?: string;
  accessibilityLabel?: string;
  disabled?: boolean;
  /**
   * The button's own action is running. The control deliberately stays
   * enabled: it keeps its title, width, and color and centers a small spinner
   * with the title (the loading button early iOS used). Callers keep whatever
   * guard their action needs, because the button stays live.
   */
  busy?: boolean;
  primary?: boolean;
  destructive?: boolean;
  compact?: boolean;
  link?: boolean;
  /**
   * ``plainLink`` keeps link behavior (pointer cursor, accessibility role) but
   * is the quiet variant: it draws no inline bezel, left-aligns the title for
   * a long value such as a file path that owns its whole column, reads in
   * secondary ink at the row's own weight instead of the accent-colored,
   * semibold navigation link, and stays underlined at rest — the underline is
   * what says "link", and neither the ink nor the decoration reacts to hover.
   */
  plainLink?: boolean;
  /**
   * ``tight`` lets a caller that owns its own short label hug the native
   * bezel; the default reservation stays for translated labels, which must
   * never truncate. ``flex`` skips the reservation entirely and lets the
   * caller's own container bound the control, so a long value such as a file
   * path truncates inside its column instead of widening the row.
   */
  titleWidth?: "auto" | "tight" | "flex";
  onPress?: () => void;
  style?: StyleProp<ViewStyle>;
};

type SegmentedProps = {
  labels: string[];
  selectedValue: string;
  disabled?: boolean;
  compact?: boolean;
  overlayLayer?: boolean;
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

/** Rough advance width of a native control label, in points. */
export function nativeControlTextWidth(label: string): number {
  return Array.from(label).reduce((width, character) => {
    if (/\s/u.test(character)) return width + 4;
    return width + (/[^\u0000-\u024f]/u.test(character) ? 13 : 7.5);
  }, 0);
}

function isCompactGlyphTitle(title: string): boolean {
  const trimmed = title.trim();
  return trimmed.length > 0 && Array.from(trimmed).length <= 2 && !/[\p{L}\p{N}]/u.test(trimmed);
}

// Fabric lays out opaque native controls before AppKit/WinUI can report their
// intrinsic content size. Give translated labels enough shared layout width so
// buttons and checkboxes do not turn complete actions into truncated fragments.
function nativeButtonMinimumWidth(title: string, compact = false): number {
  return Math.max(72, Math.ceil((compact ? 32 : 38) + nativeControlTextWidth(title) * 1.15));
}

// A native button draws its title inside a fixed bezel, so a reservation wider
// than the text reads as empty padding around the label. Callers that keep
// their own label short can ask for the bezel-hugging width instead; the
// shared default above stays for translated labels.
function nativeButtonTightWidth(title: string, plainLink = false): number {
  // A plain link draws no inline bezel, so it needs no bezel padding: the caller
  // that measures the leftover width gets that room back for its own text.
  if (plainLink) return Math.ceil(nativeControlTextWidth(title)) + 2;
  return Math.max(44, Math.ceil(nativeControlTextWidth(title) + 20));
}

// A busy button draws the spinner as its leading content, so the native
// control centers the icon together with the title as one group — the loading
// button early iOS showed. The shared reservation above already leaves room
// for that leading 12 pt icon, so a busy button keeps the exact width of its
// idle state; only a caller that reserves the bezel-hugging width has to add
// the slot itself, and it does so whether or not its action is running so the
// control never resizes mid-flight.
const BUSY_SPINNER_SIZE = 12;
const BUSY_SPINNER_GAP = 3;
const BUSY_SPINNER_BEZEL_ROOM = 4;
const BUSY_SPINNER_SLOT = 2 * (BUSY_SPINNER_SIZE + BUSY_SPINNER_GAP + BUSY_SPINNER_BEZEL_ROOM);

function nativeButtonBusyMinimumWidth(title: string, reservation: number, plainLink: boolean): number {
  if (plainLink) return reservation;
  return Math.max(reservation, Math.ceil(nativeControlTextWidth(title)) + BUSY_SPINNER_SLOT);
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
  /** A macOS overlay that must remain above native content controls. */
  overlayLayer?: boolean;
  onChange?: (event: NativeSyntheticEvent<{ value: string; index: number }>) => void;
  style?: StyleProp<ViewStyle>;
};

const pickerLabelWidth = (labels: string[], selectedValue: string): number => Math.ceil(Math.min(180, Math.max(80, ...([selectedValue, ...labels].map((label) => label.length * 13 * 0.72 + 32)))));

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
  /**
   * Compact density: 22 pt rows for a plain table, and 26 pt rows for a source
   * list — the pane's own rail against the sidebar's regular 30 pt.
   */
  compact?: boolean;
  followBottom?: boolean;
  /** One explicit table scroll request; -1 keeps the current viewport. */
  scrollToRow?: number;
  framed?: boolean;
  /** Sidebar source-list chrome: no header, single full-width column. */
  sourceList?: boolean;
  /** Per-row SF Symbol names (macOS) / semantic icon names (Windows). */
  rowSymbols?: string[];
  /** Per-row sRGB hex badge colors behind each source-list symbol. */
  rowSymbolColors?: string[];
  /** Per-row bundled icon-tile asset names; preferred over symbol/color. */
  rowImageNames?: string[];
  cellHorizontalPadding?: number;
  firstColumnHorizontalPadding?: number;
  preserveColumnWidths?: boolean;
  scrollTrailingColumnOverflow?: boolean;
  disabledRowKeys?: string[];
  secondaryCellKeys?: string[];
  /** Rows whose text renders in the warning (brown) color. */
  alertRowKeys?: string[];
  /**
   * Spanning (group) rows that are entries of their own: they keep the
   * spanning layout but a click selects them like an ordinary row (the
   * routes table's public-model rows).  Every other spanning row stays a
   * section header the native table refuses to select.
   */
  selectableSpanningRowKeys?: string[];
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


// The WinUI button's symbols were Segoe Fluent Icons glyphs. Keep the same
// glyphs on the React Native leaf so an icon button does not become a word.
const WINDOWS_BUTTON_GLYPHS: Record<NonNullable<ButtonProps["symbol"]>, string> = {
  check: "\uE73E",
  close: "\uE711",
  copy: "\uE8C8",
  edit: "\uE70F",
  help: "\uE897",
  import: "\uE8B5",
  info: "\uE946",
  minus: "\uE738",
  pause: "\uE769",
  play: "\uE768",
  plus: "\uE710",
  "power-off": "\uE7E8",
  "power-on": "\uE7E8",
  refresh: "\uE72C",
  trash: "\uE74D",
  "chevron-up": "\uE70E",
  "chevron-down": "\uE70D",
  "chevron-up-down": "\uE70D",
};

function WindowsButton({ title, symbol, symbolWithTitle, symbolTrailing, toolTip, accessibilityLabel, disabled, busy, primary, destructive, compact, link, plainLink, titleWidth: _titleWidth, onPress, style, ref }: ButtonProps & { ref?: React.Ref<any> }): React.JSX.Element {
  const showsTitle = symbolWithTitle === true || !symbol;
  const glyph = symbol ? WINDOWS_BUTTON_GLYPHS[symbol] ?? "\uE74D" : undefined;
  const ink = disabled ? "#98a2b3" : destructive ? "#c42b1c" : primary ? "#ffffff" : link ? "#005fb8" : "#1a1a1a";
  return <Pressable
    ref={ref as never}
    disabled={disabled}
    onPress={onPress}
    onAccessibilityTap={onPress}
    accessibilityRole={link ? "link" : "button"}
    accessibilityLabel={accessibilityLabel ?? title}
    accessibilityState={{ disabled: disabled === true }}
    {...({ tooltip: toolTip ?? title } as { tooltip: string })}
    style={[styles.windowsButton, compact && styles.windowsButtonCompact, link && styles.windowsButtonLink, plainLink && styles.windowsButtonPlainLink, primary && styles.windowsButtonPrimary, destructive && styles.windowsButtonDestructive, disabled && styles.windowsButtonDisabled, busy && styles.buttonBusy, style]}
  >
    {busy === true ? <ActivityIndicator size="small" color={ink} style={styles.buttonBusyIndicator} /> : null}
    {glyph && !symbolTrailing ? <Text style={[styles.windowsButtonGlyph, { color: ink }]}>{glyph}</Text> : null}
    {showsTitle ? <Text numberOfLines={1} style={[styles.windowsButtonTitle, (link || plainLink) && styles.windowsButtonLinkTitle, { color: ink }, plainLink && styles.windowsButtonPlainLinkTitle]}>{title}</Text> : null}
    {glyph && symbolTrailing ? <Text style={[styles.windowsButtonGlyph, { color: ink }]}>{glyph}</Text> : null}
  </Pressable>;
}

const WindowsSegmentedControl = React.forwardRef<any, SegmentedProps>(function WindowsSegmentedControl({ labels, selectedValue, disabled, compact = true, onChange, style }, ref): React.JSX.Element {
  const selectedIndex = labels.indexOf(selectedValue);
  const choose = (label: string, index: number): void => {
    if (index === selectedIndex) return;
    onChange?.({ nativeEvent: { value: label, index } } as NativeSyntheticEvent<{ value: string; index: number }>);
  };
  return <View ref={ref} style={[styles.windowsSegmented, compact && styles.windowsSegmentedCompact, disabled && styles.windowsDisabled, style]} accessibilityRole="tablist">
    {labels.map((label, index) => {
      const selected = index === selectedIndex;
      return <Pressable
        key={`${label}:${index}`}
        disabled={disabled}
        onPress={() => choose(label, index)}
        onAccessibilityTap={() => choose(label, index)}
        accessibilityRole="tab"
        accessibilityLabel={label}
        accessibilityState={{ selected, disabled: disabled === true }}
        style={[styles.windowsSegment, selected && styles.windowsSegmentSelected, index === 0 && styles.windowsSegmentFirst, index === labels.length - 1 && styles.windowsSegmentLast]}
      >
        <Text numberOfLines={1} style={[styles.windowsSegmentText, selected && styles.windowsSegmentTextSelected, disabled && styles.windowsDisabledText]}>{label}</Text>
      </Pressable>;
    })}
  </View>;
});

function WindowsCheckbox({ label, value, disabled, compact = true, labelVisible = true, onValueChange, style }: CheckboxProps): React.JSX.Element {
  const sizedStyle = [{ minWidth: labelVisible ? nativeCheckboxMinimumWidth(label) : 24 }, style];
  const toggle = (): void => onValueChange?.(!value);
  return <Pressable
    style={[styles.checkboxFallback, compact && styles.windowsCheckboxCompact, sizedStyle]}
    disabled={disabled}
    onPress={toggle}
    onAccessibilityTap={toggle}
    accessibilityRole="checkbox"
    accessibilityLabel={label}
    accessibilityState={{ checked: value, disabled: disabled === true }}
  >
    <View style={[styles.windowsCheckboxBox, value && styles.windowsCheckboxBoxChecked, disabled && styles.windowsDisabled]}>
      {value ? <Text style={styles.windowsCheckboxMark}>{"\u2713"}</Text> : null}
    </View>
    {labelVisible ? <Text numberOfLines={1} style={[styles.windowsCheckboxLabel, disabled && styles.windowsDisabledText]}>{label}</Text> : null}
  </Pressable>;
}

type WindowsActionMenuBridge = {
  showActionMenu?: (
    title: string,
    items: string[],
    anchor: { x: number; y: number; width: number; height: number },
  ) => Promise<number | undefined>;
};

function WindowsPicker({ labels, selectedValue, disabled, compact = true, onChange, style }: PickerProps): React.JSX.Element {
  const triggerRef = React.useRef<View>(null);
  const selectedIndex = labels.indexOf(selectedValue);
  const choose = (index: number): void => {
    const value = labels[index] ?? "";
    onChange?.({ nativeEvent: { value, index } } as NativeSyntheticEvent<{ value: string; index: number }>);
  };
  const openMenu = (): void => {
    if (disabled || labels.length === 0) return;
    const bridge = (NativeModules as { LiteLLMNativeLeaf?: WindowsActionMenuBridge }).LiteLLMNativeLeaf;
    triggerRef.current?.measureInWindow((x, y, width, height) => {
      void bridge?.showActionMenu?.(selectedValue || " ", labels, { x, y, width, height })
        .then((index) => {
          if (typeof index === "number" && index >= 0 && index < labels.length) choose(index);
        })
        .catch(() => undefined);
    });
  };
  return <Pressable
    ref={triggerRef}
    disabled={disabled}
    onPress={openMenu}
    onAccessibilityTap={openMenu}
    accessibilityRole="button"
    accessibilityLabel={selectedValue}
    accessibilityState={{ disabled: disabled === true, selected: selectedIndex >= 0 }}
    style={[styles.windowsPicker, compact && styles.windowsPickerCompact, disabled && styles.windowsPickerDisabled, style]}
  >
    <Text numberOfLines={1} style={[styles.windowsPickerText, disabled && styles.windowsDisabledText]}>{selectedValue}</Text>
    <Text style={styles.windowsPickerChevron}>{"\u25BE"}</Text>
  </Pressable>;
}

const NativeButtonWithRef = React.forwardRef<any, ButtonProps>(function NativeButtonWithRef({ titleWidth: titleWidthRequest, ...props }, ref): React.JSX.Element {
  const compact = props.compact ?? true;
  const buttonProps = {
    ...props,
    compact,
    disabled: props.disabled === true,
    busy: props.busy === true,
    primary: props.primary === true,
    destructive: props.destructive === true,
    link: props.link === true,
    plainLink: props.plainLink === true,
    symbolWithTitle: props.symbolWithTitle === true,
    symbolTrailing: props.symbolTrailing === true,
  };
  // A caller may enlarge a button, but must never reduce a translated title to
  // an ellipsis. Symbol-only and compact glyph buttons intentionally use their
  // caller/native icon size rather than the text-button minimum width. A
  // symbol-with-title button reserves room for the leading icon.
  const showsTitle = props.symbolWithTitle === true || !props.symbol;
  const reservation = titleWidthRequest === "tight"
    ? nativeButtonTightWidth(props.title, props.plainLink === true)
    : nativeButtonMinimumWidth(props.title, compact);
  const titleWidth = showsTitle && !(compact && isCompactGlyphTitle(props.title))
    ? titleWidthRequest === "flex"
      ? { minWidth: 0, flexShrink: 1 }
      : {
          minWidth: (props.busy === undefined ? reservation : nativeButtonBusyMinimumWidth(props.title, reservation, props.plainLink === true)) + (props.symbol ? 22 : 0),
          flexShrink: 0,
        }
    : undefined;
  const style = [props.link ? styles.linkButton : styles.button, props.style, titleWidth];
  if (Platform.OS === "windows") {
    return <WindowsButton {...buttonProps} ref={ref as never} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitButton {...buttonProps} ref={ref as never} style={[props.style, titleWidth]} />;
  }
    return <Pressable ref={ref as never} disabled={props.disabled} onPress={props.onPress} style={[style, props.busy === true && styles.buttonBusy]} accessibilityRole={props.link ? "link" : "button"}>{props.busy === true ? <ActivityIndicator size="small" style={styles.buttonBusyIndicator} /> : null}<Text style={styles.controlText}>{props.title}</Text></Pressable>;
});

// Keep the long-standing function export while allowing desktop callers to
// pass a native ref for anchored menus. React 19 exposes ref as a prop.
export function NativeButton(props: ButtonProps & { ref?: React.Ref<any> }): React.JSX.Element {
  return <NativeButtonWithRef {...props} ref={props.ref} />;
}

/**
 * The app's own busy wheel as a standalone control.
 *
 * Every wait the app reports draws the same wheel: the busy button, the
 * provider wizard's model step, and the 分组管理 sheet's 密钥 row all render
 * `AppKitBusySpinner`'s frames (`AppKitNativeLeaf.swift`).  A standalone wait
 * therefore mounts the native view that draws those frames rather than
 * `ActivityIndicator`, which renders AppKit's system progress indicator —
 * different spoke count, weight, and fade — inside a 12 pt box it paints past,
 * which is also what clipped the leading spokes against the row's left edge.
 * `label` is the accessibility name, because a wheel has no words.
 */
export function NativeBusySpinner({ label, style }: { label: string; style?: StyleProp<ViewStyle> }): React.JSX.Element {
  if (Platform.OS === "macos") {
    return <AppKitBusySpinner accessibilityLabel={label} running style={[styles.busySpinner, style]} />;
  }
  // Windows draws the same wait with the platform's own ring; the host's
  // `ProgressRing` is its one busy affordance (AGENTS.md).
  return <ActivityIndicator accessibilityLabel={label} size="small" style={[styles.busySpinner, style]} />;
}

const NativeSegmentedControlWithRef = React.forwardRef<any, SegmentedProps>(function NativeSegmentedControlWithRef({ labels, selectedValue, disabled, compact = true, onChange, style }, ref): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WindowsSegmentedControl ref={ref} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitSegmentedControl ref={ref} labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  return <Pressable ref={ref as never} style={[styles.button, style]} disabled={disabled} accessibilityRole="tab"><Text style={styles.controlText}>{labels.join(" / ")} ({selectedValue})</Text></Pressable>;
});

export function NativeSegmentedControl(props: SegmentedProps & { ref?: React.Ref<any> }): React.JSX.Element {
  return <NativeSegmentedControlWithRef {...props} ref={props.ref} />;
}

type TextFieldProps = TextInputProps & {
  search?: boolean;
  /** Draw this field without its native bezel when another border owns it. */
  borderless?: boolean;
};


export function NativeTextField({ style, onChangeText, onSubmitEditing, search = false, borderless = false, ...props }: TextFieldProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    // Windows text input is React Native's own Composition leaf: RichEdit
    // backed, with IME, selection, and secure entry already working inside the
    // island. A WinUI TextBox cannot be constructed there. The frame is a React
    // view so borders and invalid states stay in the shared style layer.
    // RNW 0.85 preview's UIA ValuePattern.SetValue crashes the host for this
    // leaf (read-only ValuePattern reads are safe); exercise typing with the
    // keyboard, not an automation SetValue, until that provider is fixed.
    const flatStyle = StyleSheet.flatten(style);
    const inputStyle = [
      styles.windowsTextFieldInput,
      flatStyle?.color !== undefined && { color: flatStyle.color },
      props.multiline === true && styles.windowsTextFieldInputMultiline,
    ];
    return <View style={[styles.windowsTextField, style, props.editable === false && styles.windowsTextFieldDisabled]}>
      <TextInput {...props} style={inputStyle} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} onBlur={props.onBlur} />
    </View>;
  }
  if (Platform.OS === "macos") {
    return <AppKitTextField {...props} search={search} borderless={borderless} style={style} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} />;
  }
  return <TextInput {...props} style={style} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} />;
}

export function NativeToggle({ value, disabled, onValueChange, style, accessibilityLabel }: ToggleProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <Switch value={value} disabled={disabled} onValueChange={onValueChange} style={style} accessibilityLabel={accessibilityLabel} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitToggle value={value} disabled={disabled} onValueChange={onValueChange} style={style} accessibilityLabel={accessibilityLabel} />;
  }
  return <Pressable style={[styles.toggleFallback, style]} disabled={disabled} onPress={() => onValueChange?.(!value)} accessibilityRole="switch" accessibilityLabel={accessibilityLabel} accessibilityState={{ checked: value, disabled }}><StaticBooleanIndicator value={value} /></Pressable>;
}

export function NativeSelectableRow({ title, detail, selected, disabled, onPress, style }: SelectableRowProps): React.JSX.Element {
  if (Platform.OS === "macos") {
    return <AppKitSelectableRow title={title} detail={detail} selected={selected} disabled={disabled} onPress={onPress} style={style} />;
  }
  return <Pressable style={[styles.selectableRow, selected && styles.selectableRowSelected, style]} disabled={disabled} onPress={onPress} accessibilityRole="button"><View style={styles.selectableText}><Text style={styles.selectableTitle}>{title}</Text>{detail ? <Text style={styles.selectableDetail}>{detail}</Text> : null}</View></Pressable>;
}

export function NativeCheckbox({ label, value, disabled, compact = true, labelVisible = true, onValueChange, style }: CheckboxProps): React.JSX.Element {
  const sizedStyle = [{ minWidth: labelVisible ? nativeCheckboxMinimumWidth(label) : 24 }, style];
  if (Platform.OS === "windows") {
    return <WindowsCheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={onValueChange} style={sizedStyle} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitCheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={onValueChange} style={sizedStyle} />;
  }
  return <Pressable style={[styles.checkboxFallback, sizedStyle]} disabled={disabled} onPress={() => onValueChange?.(!value)} accessibilityRole="checkbox" accessibilityLabel={label} accessibilityState={{ checked: value, disabled }}><StaticBooleanIndicator value={value} />{labelVisible ? <Text style={styles.controlText}>{label}</Text> : null}</Pressable>;
}

export function NativePicker({ labels, selectedValue, disabled, compact = true, overlayLayer = false, onChange, style }: PickerProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WindowsPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} overlayLayer={overlayLayer} onChange={onChange} style={[compact && style === undefined ? { minWidth: pickerLabelWidth(labels, selectedValue) } : null, style]} />;
  }
  return <NativeSegmentedControl labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
}


function windowsSourceSymbol(symbol: string): string {
  const glyphs: Record<string, string> = {
    "slider.horizontal.3": "\uE700",
    "square.stack.3d.up": "\uE7B8",
    "gearshape.2": "\uE713",
    terminal: "\uE756",
    "arrow.up.arrow.down": "\uE895",
    "list.bullet.rectangle": "\uE8A5",
  };
  return glyphs[symbol] ?? "•";
}

function WindowsTable({ columns, rows, selectedKey, stripedRows, compact, followBottom, scrollToRow, framed, sourceList, rowSymbols, rowSymbolColors, disabledRowKeys, secondaryCellKeys, selectableSpanningKeys, onSelectionChange, onRowDoublePress, style, alertRowKeys }: {
  columns: TableProps["columns"];
  rows: TableProps["rows"];
  selectedKey: string;
  stripedRows: boolean;
  compact: boolean;
  followBottom: boolean;
  scrollToRow: number;
  framed: boolean;
  sourceList: boolean;
  rowSymbols: string[];
  rowSymbolColors: string[];
  disabledRowKeys: string[];
  secondaryCellKeys: string[];
  selectableSpanningKeys: Set<string>;
  onSelectionChange?: (key: string, index: number) => void;
  onRowDoublePress?: (key: string, index: number) => void;
  style?: StyleProp<ViewStyle>;
  alertRowKeys: string[];
}): React.JSX.Element {
  const lastPress = React.useRef<{ key: string; at: number } | undefined>(undefined);
  const verticalScroll = React.useRef<ScrollView>(null);
  const rowHeight = compact ? 30 : 36;
  const totalWidth = columns.reduce((sum, column) => sum + column.width, 0);
  React.useEffect(() => {
    if (scrollToRow < 0) return;
    verticalScroll.current?.scrollTo({ y: scrollToRow * rowHeight, animated: false });
  }, [scrollToRow, rowHeight]);
  React.useEffect(() => {
    if (!followBottom) return;
    verticalScroll.current?.scrollToEnd({ animated: false });
  }, [followBottom, rows.length]);
  const select = (row: TableProps["rows"][number], index: number): void => {
    if (row.spanning && !selectableSpanningKeys.has(row.key)) return;
    const now = Date.now();
    if (lastPress.current?.key === row.key && now - lastPress.current.at < 360) onRowDoublePress?.(row.key, index);
    lastPress.current = { key: row.key, at: now };
    onSelectionChange?.(row.key, index);
  };
  return <View style={[windowsTableStyles.root, !framed && windowsTableStyles.borderless, style]}>
    {!sourceList ? <View style={[windowsTableStyles.header, { minWidth: totalWidth }]}>
      {columns.map((column) => <Text key={column.label} numberOfLines={1} style={[windowsTableStyles.headerText, { width: column.width }]}>{column.label}</Text>)}
    </View> : null}
    <ScrollView horizontal showsHorizontalScrollIndicator={!sourceList} contentContainerStyle={windowsTableStyles.horizontalContent}>
      <ScrollView ref={verticalScroll} style={windowsTableStyles.verticalScroll} contentContainerStyle={[windowsTableStyles.body, { minWidth: totalWidth }]} nestedScrollEnabled>
        {rows.map((row, index) => {
          const selected = row.key === selectedKey;
          const disabled = disabledRowKeys.includes(row.key);
          const alert = alertRowKeys.includes(row.key);
          const spanning = row.spanning === true;
          const stripe = stripedRows && !selected && index % 2 === 1;
          const textColor = alert ? "#8a5a00" : (disabled ? "#98a2b3" : "#1f2937");
          const rowStyle = [windowsTableStyles.row, compact ? windowsTableStyles.rowCompact : windowsTableStyles.rowRelaxed, stripe && windowsTableStyles.rowStripe, selected && windowsTableStyles.rowSelected, disabled && windowsTableStyles.rowDisabled];
          return <Pressable key={row.key} disabled={disabled || (spanning && !selectableSpanningKeys.has(row.key))} onPress={() => select(row, index)} style={rowStyle}>
            {spanning ? <Text numberOfLines={1} style={[windowsTableStyles.groupText, { color: textColor }]}>{row.cells[0] ?? ""}</Text> : <View style={windowsTableStyles.cells}>
              {columns.map((column, columnIndex) => {
                const cell = row.cells[columnIndex] ?? "";
                const cellKey = `${row.key}\x1f${columnIndex}`;
                const secondary = secondaryCellKeys.includes(cellKey);
                return <Text key={`${row.key}:${columnIndex}`} numberOfLines={1} style={[windowsTableStyles.cell, { width: column.width, color: secondary ? "#667085" : textColor }, columnIndex === 0 && sourceList && windowsTableStyles.sourceCell]}>
                  {columnIndex === 0 && sourceList && rowSymbols[index] ? <Text style={[windowsTableStyles.symbol, { backgroundColor: rowSymbolColors[index] || "#667085" }]}>{windowsSourceSymbol(rowSymbols[index])}</Text> : null}{cell.replace(/^\t/u, "")}
                </Text>;
              })}
            </View>}
          </Pressable>;
        })}
      </ScrollView>
    </ScrollView>
  </View>;
}

const windowsTableStyles = StyleSheet.create({
  root: { minHeight: 120, overflow: "hidden", borderWidth: 1, borderColor: "#d0d5dd", borderRadius: 6, backgroundColor: "#ffffff" },
  borderless: { borderWidth: 0, borderRadius: 0, backgroundColor: "transparent" },
  header: { flexDirection: "row", alignItems: "center", minHeight: 30, paddingHorizontal: 8, backgroundColor: "#f8fafc", borderBottomWidth: 1, borderBottomColor: "#e4e7ec" },
  headerText: { paddingHorizontal: 8, color: "#667085", fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  horizontalContent: { flexGrow: 1 },
  verticalScroll: { flex: 1, minHeight: 0 },
  body: { flexGrow: 1 },
  row: { minWidth: "100%", borderBottomWidth: 1, borderBottomColor: "#eef2f6", justifyContent: "center" },
  rowCompact: { minHeight: 30 },
  rowRelaxed: { minHeight: 36 },
  rowStripe: { backgroundColor: "#fafbfc" },
  rowSelected: { backgroundColor: "#eaf3ff", borderLeftWidth: 3, borderLeftColor: "#1677d2" },
  rowDisabled: { opacity: 0.72 },
  cells: { flexDirection: "row", alignItems: "center", paddingHorizontal: 8 },
  cell: { paddingHorizontal: 8, color: "#1f2937", fontSize: UI_FONT_SIZE },
  sourceCell: { flexDirection: "row", alignItems: "center" },
  symbol: { width: 18, height: 18, marginRight: 7, borderRadius: 5, color: "#ffffff", fontFamily: "Segoe MDL2 Assets", fontSize: 12, lineHeight: 18, textAlign: "center" },
  groupText: { paddingHorizontal: 12, color: "#475467", fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
});

export function NativeTable({ columns, rows, selectedKey = "", striped = true, compact = true, followBottom = false, scrollToRow = -1, framed = true, sourceList = false, rowSymbols = [], rowSymbolColors = [], rowImageNames = [], cellHorizontalPadding = 8, firstColumnHorizontalPadding = 8, preserveColumnWidths = false, scrollTrailingColumnOverflow = false, disabledRowKeys = [], secondaryCellKeys = [], selectableSpanningRowKeys = [], onSelectionChange, onRowDoublePress, style, alertRowKeys = [] }: TableProps): React.JSX.Element {
  // Striped rows belong to the list surface, not to its contents: the native
  // table paints its alternating bands across the whole viewport, so an empty
  // list shows the same stripes and reads as a list that happens to have no
  // rows. Deriving this from `rows.length` made every empty list a blank white
  // block, indistinguishable from the pane behind it.
  const stripedRows = striped && !sourceList;
  const spanningRowKeys = rows.filter((row) => row.spanning).map((row) => row.key);
  const selectableSpanningKeys = new Set(selectableSpanningRowKeys.filter((key) => spanningRowKeys.includes(key)));
  // Every key here must also appear in the macOS wrapper's own parameter list:
  // `AppKitControls.tsx` forwards props by hand, so a prop added on one host and
  // forgotten on the other arrives empty instead of failing to compile.
  const nativeProps = {
    columnLabels: columns.map((column) => column.label),
    columnWidths: columns.map((column) => column.width),
    rowKeys: rows.map((row) => row.key),
    // Spanning (group) rows paint one full-width cell, but the native tables
    // address cells positionally by row * columnCount. Pad every spanning row
    // to the column count so following rows keep their columns aligned.
    cells: rows.flatMap((row) => row.spanning
      ? [row.cells[0] ?? "", ...Array(Math.max(0, columns.length - 1)).fill("")]
      : row.cells),
    selectedKey,
    alternatingRows: stripedRows,
    compact,
    followBottom,
    scrollToRow,
    borderless: !framed,
    sourceList,
    rowSymbols,
    rowSymbolColors,
    rowImageNames,
    disabledRowKeys,
    secondaryCellKeys,
    alertRowKeys,
    spanningRowKeys,
    selectableSpanningRowKeys: Array.from(selectableSpanningKeys),
  };
  if (Platform.OS === "windows") {
    // This React table is the only Windows table path.
    return <WindowsTable columns={columns} rows={rows} selectedKey={selectedKey} stripedRows={stripedRows} compact={compact} followBottom={followBottom} scrollToRow={scrollToRow} framed={framed} sourceList={sourceList} rowSymbols={rowSymbols} rowSymbolColors={rowSymbolColors} disabledRowKeys={disabledRowKeys} secondaryCellKeys={secondaryCellKeys} selectableSpanningKeys={selectableSpanningKeys} onSelectionChange={onSelectionChange} onRowDoublePress={onRowDoublePress} alertRowKeys={alertRowKeys} style={[styles.table, style]} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitTable {...nativeProps} cellHorizontalPadding={cellHorizontalPadding} firstColumnHorizontalPadding={firstColumnHorizontalPadding} preserveColumnWidths={preserveColumnWidths} scrollTrailingColumnOverflow={scrollTrailingColumnOverflow} onSelectionChange={(event) => onSelectionChange?.(event.nativeEvent.key, event.nativeEvent.index)} onRowDoublePress={(event) => onRowDoublePress?.(event.nativeEvent.key, event.nativeEvent.index)} style={[styles.table, style]} />;
  }
  return <View style={[styles.tableFallback, !framed && styles.tableFallbackUnframed, style]}>{rows.map((row, index) => {
    const selected = row.key === selectedKey;
    const stripe = stripedRows && !selected && index % 2 === 1 ? styles.tableFallbackStripe : undefined;
    if (row.spanning) {
      if (!selectableSpanningKeys.has(row.key)) {
        return <View key={row.key} style={[styles.tableFallbackGroupRow, stripe]}><Text numberOfLines={1} style={[styles.selectableTitle, styles.tableFallbackGroupText]}>{row.cells[0] ?? ""}</Text></View>;
      }
      return <Pressable key={row.key} onPress={() => onSelectionChange?.(row.key, index)} onLongPress={() => onRowDoublePress?.(row.key, index)}><NativeSelectableRow title={row.cells[0] ?? ""} selected={selected} style={[styles.tableFallbackGroupRow, stripe]} /></Pressable>;
    }
    return <Pressable key={row.key} onPress={() => onSelectionChange?.(row.key, index)} onLongPress={() => onRowDoublePress?.(row.key, index)}><NativeSelectableRow title={row.cells[0] ?? ""} detail={row.cells.slice(1).join(" | ")} selected={selected} style={stripe} /></Pressable>;
  })}</View>;
}

export function NativeTextEditor({ value, documentKey, readOnly, wrap = true, onChangeText, style }: TextEditorProps): React.JSX.Element {
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
    return <View style={[styles.splitFallback, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></View>;
  }
  if (Platform.OS === "macos") {
    return <AppKitSplitView paneWidth={paneWidth} minPaneWidth={minPaneWidth} maxPaneWidth={maxPaneWidth} paneOpen={paneOpen} disabled={disabled} onPaneWidthChange={onPaneWidthChange} style={[styles.splitView, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></AppKitSplitView>;
  }
  return <View style={[styles.splitFallback, style]}><View style={[styles.splitLeading, { width: paneWidth }]}>{leading}</View><View style={styles.splitTrailing}>{trailing}</View></View>;
}

const styles = StyleSheet.create({
  button: { minWidth: 28, height: 24 },
  buttonBusy: { flexDirection: "row", alignItems: "center", justifyContent: "center", gap: BUSY_SPINNER_GAP },
  buttonBusyIndicator: { width: BUSY_SPINNER_SIZE, height: BUSY_SPINNER_SIZE },
  busySpinner: { width: BUSY_SPINNER_SIZE, height: BUSY_SPINNER_SIZE },
  controlText: { fontSize: UI_FONT_SIZE },
  windowsButton: { minHeight: 24, minWidth: 28, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 4, paddingHorizontal: 8, borderRadius: 4, borderWidth: 1, borderColor: "#c8c8c8", backgroundColor: "#ffffff" },
  windowsButtonCompact: { minHeight: 24, paddingHorizontal: 6 },
  windowsButtonLink: { borderWidth: 0, backgroundColor: "transparent", paddingHorizontal: 0 },
  windowsButtonPlainLink: { borderWidth: 0, backgroundColor: "transparent", paddingHorizontal: 0, justifyContent: "flex-start" },
  windowsButtonPrimary: { borderColor: "#005fb8", backgroundColor: "#005fb8" },
  windowsButtonDestructive: { borderColor: "#c42b1c", backgroundColor: "#c42b1c" },
  windowsButtonDisabled: { opacity: 0.55 },
  windowsButtonGlyph: { fontFamily: "Segoe MDL2 Assets", fontSize: UI_FONT_SIZE, lineHeight: 16 },
  windowsButtonTitle: { fontSize: UI_FONT_SIZE, flexShrink: 1 },
  windowsButtonLinkTitle: { textDecorationLine: "underline" },
  windowsButtonPlainLinkTitle: { textDecorationLine: "underline", fontWeight: "400" },
  windowsSegmented: { flexDirection: "row", alignItems: "stretch", minHeight: 24, borderRadius: 4, borderWidth: 1, borderColor: "#c8c8c8", overflow: "hidden", backgroundColor: "#ffffff" },
  windowsSegmentedCompact: { minHeight: 24 },
  windowsSegment: { flex: 1, minWidth: 0, alignItems: "center", justifyContent: "center", paddingHorizontal: 6, borderRightWidth: 1, borderRightColor: "#e4e7ec" },
  windowsSegmentFirst: { borderTopLeftRadius: 3, borderBottomLeftRadius: 3 },
  windowsSegmentLast: { borderRightWidth: 0, borderTopRightRadius: 3, borderBottomRightRadius: 3 },
  windowsSegmentSelected: { backgroundColor: "#eaf3ff" },
  windowsSegmentText: { color: "#475467", fontSize: UI_TIP_FONT_SIZE },
  windowsSegmentTextSelected: { color: "#005fb8", fontWeight: "600" },
  windowsCheckboxCompact: { minHeight: 24 },
  windowsCheckboxBox: { width: 16, height: 16, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: "#71717a", borderRadius: 3, backgroundColor: "#ffffff" },
  windowsCheckboxBoxChecked: { borderColor: "#005fb8", backgroundColor: "#005fb8" },
  windowsCheckboxMark: { color: "#ffffff", fontSize: 12, lineHeight: 14, fontWeight: "700" },
  windowsCheckboxLabel: { color: "#1a1a1a", fontSize: UI_FONT_SIZE, flexShrink: 1 },
  windowsDisabled: { opacity: 0.55 },
  windowsDisabledText: { color: "#98a2b3" },
  windowsPicker: { minHeight: 24, flexDirection: "row", alignItems: "center", paddingHorizontal: 6, borderWidth: 1, borderColor: "#c8c8c8", borderRadius: 4, backgroundColor: "#ffffff" },
  windowsPickerCompact: { minHeight: 24 },
  windowsPickerDisabled: { opacity: 0.55 },
  windowsPickerText: { flex: 1, minWidth: 0, color: "#1a1a1a", fontSize: UI_FONT_SIZE },
  windowsPickerChevron: { color: "#667085", fontSize: UI_TIP_FONT_SIZE, marginLeft: 4 },
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
  windowsTextField: { minHeight: 26, justifyContent: "center", overflow: "hidden", borderWidth: 1, borderColor: "#c8c8c8", borderRadius: 4, backgroundColor: "#ffffff" },
  windowsTextFieldDisabled: { opacity: 0.5 },
  windowsTextFieldInput: { flex: 1, minWidth: 0, paddingHorizontal: 8, paddingVertical: 0, fontSize: UI_FONT_SIZE, color: "#1a1a1a", backgroundColor: "transparent" },
  windowsTextFieldInputMultiline: { textAlignVertical: "top", paddingVertical: 4 },
  splitView: { minHeight: 120, flexDirection: "row" },
  splitFallback: { minHeight: 120, flexDirection: "row" },
  splitLeading: { minWidth: 0, flexShrink: 0 },
  splitter: { width: 6, minWidth: 6, alignSelf: "stretch", zIndex: 1 },
  splitTrailing: { minWidth: 0, flex: 1 },
});
