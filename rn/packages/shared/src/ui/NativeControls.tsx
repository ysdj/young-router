import React from "react";
import {
  ActivityIndicator,
  FlatList,
  Image,
  NativeModules,
  Platform,
  Pressable,
  ScrollView,
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
import { getBrowserSecretReader, getBrowserSecretStager } from "../platform/browserSecrets";

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

/**
 * The width a checkbox draws before its label, on this host.
 *
 * It is a control metric, not an estimate: AppKit's small checkbox draws a
 * 14.5 pt box and a 4.5 pt gap before the title, while the Windows and page
 * checkboxes draw a 16 pt box and a 6 pt gap.  The reservation used to keep a
 * flat 26 pt here, and the extra 4–7 pt of padding was invisible until another
 * control sat beside a checkbox — the 并发上限 row's help mark then read 10 pt
 * farther from 跟随全局 than the same mark reads from the edge of every other
 * control in the pane.  The label is what needs the cushion (see below), and
 * `nativeControlTextWidth` still gives it.
 */
function nativeCheckboxLeadingWidth(): number {
  return Platform.OS === "macos" ? 19 : 22;
}

function nativeCheckboxMinimumWidth(label: string): number {
  const textWidth = nativeControlTextWidth(label);
  return Math.ceil(nativeCheckboxLeadingWidth() + Math.max(24, textWidth));
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
  /** The row the picker belongs to; it names the control for a screen reader. */
  accessibilityLabel?: string;
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
  /**
   * One optional tooltip per cell, for a cell that shows less than it means.
   *
   * The AppKit table already sets every cell's tooltip to its own text, which
   * is right for a column that states exactly what it holds.  A cell that
   * abbreviates (`M→C`) passes the long form here instead, so hovering it
   * answers the question the abbreviation raised.  An empty entry keeps the
   * default rule, cell by cell.
   */
  tooltips?: string[];
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
  /**
   * Rows painted in the same secondary ink, still selectable: an entry that is
   * switched off is the door back to its own switch, so it dims without
   * losing the click the way `disabledRowKeys` does.
   */
  secondaryRowKeys?: string[];
  secondaryCellKeys?: string[];
  /**
   * One optional second line per row, printed small under the row's own text.
   * A source list that names its entries by title and dates them underneath
   * asks for this; a row with an empty entry keeps the one-line rhythm.
   */
  rowDetails?: string[];
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


// The glyph face is Windows' own symbol font, which exists on Windows and
// nowhere else.  A browser was asked for it anyway, and a font that does not
// exist does not fall back to the page's face for a symbol: every icon button
// in the web page was measured with a substituted font, so `+` and `−` sat a
// pixel off their labels and the toolbar buttons beside them had three
// different heights.  The web page draws Unicode glyphs instead
// (`WEB_BUTTON_GLYPHS`), and a Unicode glyph belongs to the page's own text
// face.
const GLYPH_FONT_FAMILY = Platform.OS === "web" ? undefined : "Segoe MDL2 Assets";

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

// Unicode has no font dependency: the browser must not draw Windows' private
// glyph codes as empty boxes, and an icon-only action must keep its own name.
const WEB_BUTTON_GLYPHS: Record<NonNullable<ButtonProps["symbol"]>, string> = {
  check: "✓", close: "×", copy: "⧉", edit: "✎", help: "?",
  import: "↓", info: "i", minus: "−", pause: "Ⅱ", play: "▶",
  plus: "+", "power-off": "⏻", "power-on": "⏻", refresh: "↻", trash: "⌫",
  "chevron-up": "⌃", "chevron-down": "⌄", "chevron-up-down": "↕",
};

// The two symbols that are a mark inside a ring: macOS draws `questionmark.circle`
// and `info.circle`, and Segoe's help and info glyphs are circled too.  The page
// printed the bare character, so a help tip beside a field read as a question
// mark in the sentence instead of the icon the other two hosts draw.  The ring
// is drawn rather than typed: no circled question mark is in every system face,
// and the ones that are spelled in Unicode arrive as colour emoji.
const WEB_RINGED_GLYPHS: Partial<Record<NonNullable<ButtonProps["symbol"]>, string>> = { help: "?", info: "i" };

/** One button symbol, drawn the way this host's face can draw it. */
function ButtonGlyph({ symbol, ink }: { symbol: NonNullable<ButtonProps["symbol"]>; ink: string }): React.JSX.Element {
  const mark = Platform.OS === "web" ? WEB_RINGED_GLYPHS[symbol] : undefined;
  if (mark !== undefined) return <View style={[styles.windowsButtonRing, { borderColor: ink }]}><Text style={[styles.windowsButtonRingMark, { color: ink }]}>{mark}</Text></View>;
  return <Text style={[styles.windowsButtonGlyph, { color: ink }]}>{(Platform.OS === "web" ? WEB_BUTTON_GLYPHS : WINDOWS_BUTTON_GLYPHS)[symbol]}</Text>;
}

function WindowsButton({ title, symbol, symbolWithTitle, symbolTrailing, toolTip, accessibilityLabel, disabled, busy, primary, destructive, compact, link, plainLink, titleWidth: _titleWidth, onPress, style, ref }: ButtonProps & { ref?: React.Ref<any> }): React.JSX.Element {
  const showsTitle = symbolWithTitle === true || !symbol;
  // A destructive (and a primary) button fills its bezel with that colour, so
  // the label has to be the on-accent ink: reading in `danger` on a `danger`
  // fill is red on red — the one button the user must be able to read.
  const ink = disabled ? WINDOWS_CONTROL.textDisabled : destructive ? WINDOWS_CONTROL.fill : primary ? WINDOWS_CONTROL.fill : link ? WINDOWS_CONTROL.accent : WINDOWS_CONTROL.text;
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
    {/* A symbol-only button *is* its glyph, so a busy one draws the wheel in
        that glyph's place.  Drawing both put two circles in a 24 pt bezel --
        the refresh arrow behind a spinner -- which is what the Qoder row's
        reader saw while the worker was answering. */}
    {symbol && !symbolTrailing && !(busy === true && !showsTitle) ? <ButtonGlyph symbol={symbol} ink={ink} /> : null}
    {showsTitle ? <Text numberOfLines={1} style={[styles.windowsButtonTitle, (link || plainLink) && styles.windowsButtonLinkTitle, { color: ink }, plainLink && styles.windowsButtonPlainLinkTitle]}>{title}</Text> : null}
    {symbol && symbolTrailing && !(busy === true && !showsTitle) ? <ButtonGlyph symbol={symbol} ink={ink} /> : null}
  </Pressable>;
}

const WindowsSegmentedControl = React.forwardRef<any, SegmentedProps>(function WindowsSegmentedControl({ labels, selectedValue, disabled, compact = true, onChange, style }, ref): React.JSX.Element {
  const selectedIndex = labels.indexOf(selectedValue);
  const choose = (label: string, index: number): void => {
    if (index === selectedIndex) return;
    onChange?.({ nativeEvent: { value: label, index } } as NativeSyntheticEvent<{ value: string; index: number }>);
  };
  const focusTab = (event: { key?: string; preventDefault?: () => void }, index: number): void => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault?.();
    const next = event.key === "Home" ? 0 : event.key === "End" ? labels.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + labels.length) % labels.length;
    if (labels[next] !== undefined) choose(labels[next], next);
  };
  // A native segmented control is one fixed-width strip; here the strip may be
  // wider than the row it sits in, so the caller's width reservation is dropped
  // and the segments size to their own labels. Without this the last tab was
  // clipped ("在线用量" ended mid-glyph) instead of scrolling into view.
  const tabs = <View ref={ref} style={[styles.windowsSegmented, compact && styles.windowsSegmentedCompact, disabled && styles.windowsDisabled, style, Platform.OS === "web" && { width: "auto", minWidth: 0 }]} accessibilityRole="tablist">
    {labels.map((label, index) => {
      const selected = index === selectedIndex;
      return <Pressable
        key={`${label}:${index}`}
        disabled={disabled}
        onPress={() => choose(label, index)}
        onAccessibilityTap={() => choose(label, index)}
        {...(Platform.OS === "web" ? { onKeyDown: (event: { key?: string; preventDefault?: () => void }) => focusTab(event, index) } : {})}
        accessibilityRole="tab"
        accessibilityLabel={label}
        accessibilityState={{ selected, disabled: disabled === true }}
        style={[styles.windowsSegment, selected && styles.windowsSegmentSelected, index === 0 && styles.windowsSegmentFirst, index === labels.length - 1 && styles.windowsSegmentLast, Platform.OS === "web" && styles.webSegment, Platform.OS === "web" && { minWidth: Math.ceil(nativeControlTextWidth(label)) + 26 }]}
      >
        <Text numberOfLines={1} style={[styles.windowsSegmentText, selected && styles.windowsSegmentTextSelected, disabled && styles.windowsDisabledText]}>{label}</Text>
      </Pressable>;
    })}
  </View>;
  return Platform.OS === "web" ? <ScrollView horizontal showsHorizontalScrollIndicator style={styles.webSegmentedScroll} contentContainerStyle={styles.webSegmentedContent}>{tabs}</ScrollView> : tabs;
});

function WindowsToggle({ value, disabled, onValueChange, style, accessibilityLabel }: ToggleProps): React.JSX.Element {
  const toggle = (): void => onValueChange?.(!value);
  return <Pressable
    style={[styles.windowsToggle, disabled && styles.windowsDisabled, style]}
    disabled={disabled}
    onPress={toggle}
    onAccessibilityTap={toggle}
    accessibilityRole="switch"
    accessibilityLabel={accessibilityLabel}
    accessibilityState={{ checked: value, disabled: disabled === true }}
  >
    <View style={[styles.windowsToggleTrack, value && styles.windowsToggleTrackOn]}>
      <View style={[styles.windowsToggleKnob, value && styles.windowsToggleKnobOn]} />
    </View>
  </Pressable>;
}

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

function WindowsPicker({ labels, selectedValue, disabled, compact = true, accessibilityLabel, onChange, style }: PickerProps): React.JSX.Element {
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
    accessibilityLabel={accessibilityLabel ? `${accessibilityLabel}: ${selectedValue}` : selectedValue}
    accessibilityState={{ disabled: disabled === true, selected: selectedIndex >= 0 }}
    style={[styles.windowsPicker, compact && styles.windowsPickerCompact, disabled && styles.windowsPickerDisabled, style]}
  >
    <Text numberOfLines={1} style={[styles.windowsPickerText, disabled && styles.windowsDisabledText]}>{selectedValue}</Text>
    <Text style={styles.windowsPickerChevron}>{"\u25BE"}</Text>
  </Pressable>;
}

/**
 * A browser draws its own hover label from the host node's `title`.
 *
 * React Native Web forwards neither `tooltip` (that is a Windows leaf prop) nor
 * `title` (it is not among its forwarded props), so on the page an icon-only
 * button was a glyph with nothing to read under the pointer. The one attribute
 * a browser already understands is written onto the node, and the caller's ref
 * still reaches the leaf so anything measuring the button keeps working.
 */
function WebHoverLabelButton({ hoverLabel, ...props }: React.ComponentProps<typeof WindowsButton> & { hoverLabel?: string }): React.JSX.Element {
  const host = React.useRef<any>(null);
  const forwardedRef = props.ref;
  const setHost = React.useCallback((node: any) => {
    host.current = node;
    if (typeof forwardedRef === "function") forwardedRef(node);
    else if (forwardedRef) (forwardedRef as React.MutableRefObject<any>).current = node;
  }, [forwardedRef]);
  React.useEffect(() => {
    const node = host.current as { setAttribute?: (name: string, value: string) => void; removeAttribute?: (name: string) => void } | null;
    if (hoverLabel) node?.setAttribute?.("title", hoverLabel);
    else node?.removeAttribute?.("title");
  }, [hoverLabel]);
  return <WindowsButton {...props} ref={setHost} />;
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
  if (Platform.OS === "web") {
    // The page's hover label: an explicit hint, or the name of a button that
    // draws no words of its own.  A button that already shows its title has
    // said everything a tooltip would repeat.
    const hoverLabel = (props.toolTip ?? (showsTitle ? undefined : props.title)) || undefined;
    return <WebHoverLabelButton {...buttonProps} ref={ref as never} hoverLabel={hoverLabel} style={style} />;
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
  if (Platform.OS === "windows" || Platform.OS === "web") {
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

/**
 * The ink a browser field draws its hint in.
 *
 * React Native for Web paints `::placeholder` as
 * `color: var(--placeholderTextColor)` and fills that variable from the field's
 * own `placeholderTextColor`.  Neither browser leaf passed one, so the
 * declaration was invalid at computed-value time and the hint inherited the
 * field's own `#1b1b1b`: an empty 配置供应商 read as one that already had
 * `https://api.example.com/v1` typed into it.  AppKit and WinUI answer this
 * themselves (`NSColor.placeholderTextColor`, the control's placeholder
 * foreground), so only this host has to state it.
 */
// Read where it is used, not where it is declared: `WINDOWS_CONTROL` is a
// module constant defined further down this file, and a top-level read of
// it here evaluated to `undefined` -- the page threw on load and drew
// nothing at all.
const webPlaceholderInk = (): string => WINDOWS_CONTROL.textSecondary;

export function NativeTextField({ style, onChangeText, onSubmitEditing, onBlur, onFocus, search = false, borderless = false, ...props }: TextFieldProps): React.JSX.Element {
  const [focused, setFocused] = React.useState(false);
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
    // The frame is the only height the field can trust: the page styles reach
    // this leaf with their own `minHeight` (26 for a wizard row), so the frame
    // and the inner input both carry an explicit height instead of inheriting
    // one. Without that, RichEdit centres its single line against the taller
    // frame and the glyphs sit on the bottom edge, clipped.
    return <View style={[styles.windowsTextField, focused && styles.windowsTextFieldFocused, style, props.editable === false && styles.windowsTextFieldDisabled]}>
      <TextInput {...props} style={inputStyle} onFocus={(event) => { setFocused(true); onFocus?.(event); }} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} onBlur={(event) => { setFocused(false); onBlur?.(event); }} />
    </View>;
  }
  if (Platform.OS === "macos") {
    return <AppKitTextField {...props} onBlur={onBlur} search={search} borderless={borderless} style={style} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} />;
  }
  return <View style={[styles.windowsTextField, focused && styles.windowsTextFieldFocused, style, props.editable === false && styles.windowsTextFieldDisabled]}>
    <TextInput {...props} placeholderTextColor={props.placeholderTextColor ?? webPlaceholderInk()} style={[styles.webTextFieldInput, props.multiline === true && styles.webTextFieldInputMultiline]} onFocus={(event) => { setFocused(true); onFocus?.(event); }} onChangeText={onChangeText} onSubmitEditing={onSubmitEditing} onBlur={(event) => { setFocused(false); onBlur?.(event); }} />
  </View>;
}

export function NativeToggle({ value, disabled, onValueChange, style, accessibilityLabel }: ToggleProps): React.JSX.Element {
  if (Platform.OS === "windows" || Platform.OS === "web") {
    return <WindowsToggle value={value} disabled={disabled} onValueChange={onValueChange} style={style} accessibilityLabel={accessibilityLabel} />;
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
  return <Pressable style={[styles.selectableRow, selected && styles.selectableRowSelected, style]} disabled={disabled} onPress={onPress} accessibilityRole="button" accessibilityLabel={detail ? `${title}, ${detail}` : title} accessibilityState={{ selected: selected === true, disabled: disabled === true }}><View style={styles.selectableText}><Text style={styles.selectableTitle}>{title}</Text>{detail ? <Text style={styles.selectableDetail}>{detail}</Text> : null}</View></Pressable>;
}

export function NativeCheckbox({ label, value, disabled, compact = true, labelVisible = true, onValueChange, style }: CheckboxProps): React.JSX.Element {
  const sizedStyle = [{ minWidth: labelVisible ? nativeCheckboxMinimumWidth(label) : 24 }, style];
  if (Platform.OS === "windows" || Platform.OS === "web") {
    return <WindowsCheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={onValueChange} style={sizedStyle} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitCheckbox label={label} labelVisible={labelVisible} value={value} disabled={disabled} compact={compact} onValueChange={onValueChange} style={sizedStyle} />;
  }
  return <Pressable style={[styles.checkboxFallback, sizedStyle]} disabled={disabled} onPress={() => onValueChange?.(!value)} accessibilityRole="checkbox" accessibilityLabel={label} accessibilityState={{ checked: value, disabled }}><StaticBooleanIndicator value={value} />{labelVisible ? <Text style={styles.controlText}>{label}</Text> : null}</Pressable>;
}

export function NativePicker({ labels, selectedValue, disabled, compact = true, overlayLayer = false, accessibilityLabel, onChange, style }: PickerProps): React.JSX.Element {
  if (Platform.OS === "windows") {
    return <WindowsPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} accessibilityLabel={accessibilityLabel} onChange={onChange} style={style} />;
  }
  if (Platform.OS === "macos") {
    return <AppKitPicker labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} overlayLayer={overlayLayer} accessibilityLabel={accessibilityLabel} onChange={onChange} style={[compact && style === undefined ? { minWidth: pickerLabelWidth(labels, selectedValue) } : null, style]} />;
  }
  if (Platform.OS === "web") return <WebPicker labels={labels} selectedValue={selectedValue} disabled={disabled} accessibilityLabel={accessibilityLabel} onChange={onChange} style={style} />;
  return <NativeSegmentedControl labels={labels} selectedValue={selectedValue} disabled={disabled} compact={compact} onChange={onChange} style={style} />;
}

/**
 * The browser's picker: a real `<select>`, which is the one form control every
 * browser draws, keyboard-drives, and screen-reads the same way.
 *
 * The segmented fallback it replaces rendered the choices as one read-only
 * sentence ("a / b / c (b)"), so a pane with a picker could not be changed at
 * all in a browser.  `labels` and `selectedValue` are the same pair the native
 * pickers take; the index in the answer is what they report.
 */
function WebPicker({ labels, selectedValue, disabled, accessibilityLabel, onChange, style }: PickerProps): React.JSX.Element {
  return <View style={[styles.webPickerFrame, style]}>
    <select
      aria-label={accessibilityLabel}
      disabled={disabled}
      value={selectedValue}
      onChange={(event) => onChange?.({ nativeEvent: { value: event.target.value, index: labels.indexOf(event.target.value) } } as unknown as NativeSyntheticEvent<{ value: string; index: number }>)}
      style={WEB_SELECT_STYLE}
    >
      {labels.map((label) => <option key={label} value={label}>{label}</option>)}
    </select>
  </View>;
}

// RNW renders styles as CSS classes; a native `<select>` needs its own, and
// it must follow the pane's palette rather than the browser's default chrome.
const WEB_SELECT_STYLE: React.CSSProperties = {
  width: "100%",
  minHeight: 22,
  padding: "1px 4px",
  border: "1px solid var(--yr-separator, #d4d4d8)",
  borderRadius: 4,
  background: "var(--yr-control, #ffffff)",
  color: "inherit",
  font: "inherit",
};


// The Windows sidebar draws the platform's own navigation glyphs, one per SF
// Symbol the shared presentation names.  Each entry is the Fluent/MDL2 mark
// that means the same thing — not the macOS tile's shape scaled down: \uE700
// is the hamburger menu, so General read as a collapsed menu instead of
// settings, and a document (\uE8A5) stood in for the log list.  A missing name
// falls back to a neutral dot instead of borrowing another row's mark.
const WINDOWS_SOURCE_GLYPHS: Record<string, string> = {
  "slider.horizontal.3": "\uE9E9",
  "square.stack.3d.up": "\uE81E",
  "gearshape.2": "\uE713",
  terminal: "\uE756",
  "arrow.up.arrow.down": "\uE895",
  "list.bullet.rectangle": "\uE8FD",
};

function windowsSourceSymbol(symbol: string): string {
  return WINDOWS_SOURCE_GLYPHS[symbol] ?? "•";
}

function windowsSourceImage(imageName: string): number | undefined {
  // The sidebar tiles ship with the shared UI, so Metro bundles the bytes and
  // `Image` resolves them without a package-relative `ms-appx` URI.  The old
  // URI pointed at the package root, while the app payload lives under
  // `YoungRouter\`, so every tile silently rendered as an empty box.
  return WINDOWS_SOURCE_IMAGES[imageName];
}

const WINDOWS_SOURCE_IMAGES: Record<string, number> = {
  SidebarGeneral: require("./assets/sidebar/SidebarGeneral.png"),
  SidebarProviders: require("./assets/sidebar/SidebarProviders.png"),
  SidebarRuntime: require("./assets/sidebar/SidebarRuntime.png"),
  SidebarCodex: require("./assets/sidebar/SidebarCodex.png"),
  SidebarData: require("./assets/sidebar/SidebarData.png"),
  SidebarLogs: require("./assets/sidebar/SidebarLogs.png"),
};

// A virtualized table re-renders its mounted window on every scroll frame, so a
// row must not re-render just because its parent did.  The key lists arrive as
// Sets (see `WindowsTable`) so a row's lookups are O(1) rather than a scan per
// cell, which is what made a long log list stall while rows were mounted.
// A source list's columns are usually written inline at the call site, so a
// parent render hands the row a fresh array of the same widths.  Comparing the
// widths by value keeps that parent render from re-mounting every row — the
// cost a wide log table paid on each snapshot publish.
function sameColumns(left: TableColumn[], right: TableColumn[]): boolean {
  if (left === right) return true;
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) {
    if (left[index].label !== right[index].label || left[index].width !== right[index].width) return false;
  }
  return true;
}

function sameTableRow(previous: { row: TableRow; index: number; columns: TableColumn[]; compact: boolean; sourceList: boolean; stripedRows: boolean; selected: boolean; disabled: boolean; secondaryRow: boolean; alert: boolean; rowSymbol?: string; rowSymbolColor?: string; rowImageName?: string; secondaryCellKeys: Set<string>; selectableSpanning: boolean; onSelect: (row: TableRow, index: number) => void }, next: typeof previous): boolean {
  return previous.row === next.row
    && previous.index === next.index
    && previous.compact === next.compact
    && previous.sourceList === next.sourceList
    && previous.stripedRows === next.stripedRows
    && previous.selected === next.selected
    && previous.disabled === next.disabled
    && previous.secondaryRow === next.secondaryRow
    && previous.alert === next.alert
    && previous.rowSymbol === next.rowSymbol
    && previous.rowSymbolColor === next.rowSymbolColor
    && previous.rowImageName === next.rowImageName
    && previous.secondaryCellKeys === next.secondaryCellKeys
    && previous.selectableSpanning === next.selectableSpanning
    && previous.onSelect === next.onSelect
    && sameColumns(previous.columns, next.columns);
}

/**
 * The header's trailing gutter, dragged to set a column's width.
 *
 * It is 6 pt of hover target — the width of the gutter it lives in — so it
 * never covers the header's own text, and it draws nothing at rest: a table
 * that showed a grip beside every heading would read as a table that is always
 * mid-edit.  The floor is the column's own starting width, not 0: a column
 * dragged to nothing is a column that has been lost, and the header is where it
 * would have been found.
 */
const RESIZE_CURSOR: ViewStyle = { cursor: "col-resize" } as unknown as ViewStyle;

// `View`'s generated TypeScript face omits the hover events its implementation
// declares: the page forwards `onMouseEnter`/`onMouseLeave`, and a leaf reports
// `onHoverIn`/`onHoverOut`.  The handle asks for whichever its platform has.
type HoverableViewProps = React.ComponentProps<typeof View> & {
  onHoverIn?: () => void;
  onHoverOut?: () => void;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
};
const HoverableView = View as unknown as React.ComponentType<HoverableViewProps>;

function ColumnResizeHandle({ label, width, onResize }: { label: string; width: number; onResize: (width: number) => void }): React.JSX.Element {
  const [hovered, setHovered] = React.useState(false);
  const start = React.useRef({ x: 0, width });
  const current = React.useRef(width);
  current.current = width;
  return <HoverableView
    onStartShouldSetResponder={() => true}
    onResponderTerminationRequest={() => false}
    // No other view is the responder, so this one is granted by `start` rather
    // than by a `grant` handoff: the origin of the drag is read here.
    onResponderStart={(event) => { start.current = { x: event.nativeEvent.pageX, width: current.current }; }}
    onResponderMove={(event) => onResize(Math.max(24, Math.round(start.current.width + event.nativeEvent.pageX - start.current.x)))}
    // A leaf view reports hover through `onHoverIn`; a browser <div> has no
    // such event and only forwards the pointer ones, so the page asks for
    // those and the hosts keep the pair they know.
    onHoverIn={Platform.OS === "web" ? undefined : () => setHovered(true)}
    onHoverOut={Platform.OS === "web" ? undefined : () => setHovered(false)}
    onMouseEnter={Platform.OS === "web" ? () => setHovered(true) : undefined}
    onMouseLeave={Platform.OS === "web" ? () => setHovered(false) : undefined}
    role="separator"
    accessibilityLabel={label}
    // `cursor` is a browser style: a Windows leaf draws its own resize cursor
    // and warns about a property it does not model, so only the page asks.
    style={[windowsTableStyles.resizeHandle, Platform.OS === "web" ? RESIZE_CURSOR : null, hovered && windowsTableStyles.resizeHandleHovered]}
  />;
}

// One row height for the row itself, the accent pill beside it, and the
// virtualised offsets: three copies of `compact ? 30 : 36` were free to drift.
function windowsRowHeight(compact: boolean): number {
  return compact ? 30 : WINDOWS_CONTROL.sourceRowHeight;
}

const WindowsTableRow = React.memo(function WindowsTableRow({
  row,
  index,
  rowHeight,
  columns,
  compact,
  sourceList,
  stripedRows,
  selected,
  disabled,
  secondaryRow,
  alert,
  rowSymbol,
  rowSymbolColor,
  rowImageName,
  secondaryCellKeys,
  detail,
  selectableSpanning,
  cellPadding,
  firstColumnPadding,
  onSelect,
}: {
  row: TableRow;
  index: number;
  rowHeight: number;
  columns: TableColumn[];
  compact: boolean;
  sourceList: boolean;
  stripedRows: boolean;
  selected: boolean;
  disabled: boolean;
  secondaryRow: boolean;
  alert: boolean;
  rowSymbol?: string;
  rowSymbolColor?: string;
  rowImageName?: string;
  secondaryCellKeys: Set<string>;
  /** The row's own second line, printed small under its text. */
  detail?: string;
  selectableSpanning: boolean;
  /** This table's own horizontal cell inset, the same number its header uses. */
  cellPadding: number;
  /** The first column's inset: a source list starts its rows at the pane edge. */
  firstColumnPadding: number;
  onSelect: (row: TableRow, index: number) => void;
}): React.JSX.Element {
  const spanning = row.spanning === true;
  const [hovered, setHovered] = React.useState(false);
  const stripe = stripedRows && !selected && !disabled && index % 2 === 1;
  const textColor = alert ? WINDOWS_CONTROL.warning : (disabled ? WINDOWS_CONTROL.textDisabled : (secondaryRow ? WINDOWS_CONTROL.textSecondary : WINDOWS_CONTROL.text));
  // The row is the whole of this list's interactive surface, so what a screen
  // reader announces has to come from it: without a role, a label and its
  // selected state, a Windows table was a list of anonymous press targets —
  // the source list and the settings rail included.  The label joins every
  // cell there is, because a row's meaning is its cells taken together, and a
  // spanning group row is a heading rather than a choice.
  // A leading tab is a layout mark, not text: it is how a row says "this cell
  // starts where its neighbour's symbol would have".  The cell itself drops it
  // before drawing, and the name a screen reader reads has to drop it too —
  // otherwise every model row is announced with a tab in the middle of it.
  const rowLabel = row.cells
    .map((cell) => cell.replace(/^\t/u, ""))
    .filter((cell) => cell.length > 0)
    .join(", ");
  const rowStyle = [
    windowsTableStyles.row,
    { height: rowHeight },
    sourceList && windowsTableStyles.sourceRow,
    stripe && windowsTableStyles.rowStripe,
    // A NavigationView-style source list selects with a quiet fill and a short
    // accent pill, not the full-height blue bar a data table uses.  The
    // sidebar and the settings rail both go through this branch.
    sourceList && selected && windowsTableStyles.sourceRowSelected,
    sourceList && hovered && !selected && windowsTableStyles.sourceRowHover,
    !sourceList && selected && windowsTableStyles.rowSelected,
    disabled && windowsTableStyles.rowDisabled,
  ];
  return <Pressable
    disabled={disabled || (spanning && !selectableSpanning)}
    onPress={() => onSelect(row, index)}
    onHoverIn={sourceList ? () => setHovered(true) : undefined}
    onHoverOut={sourceList ? () => setHovered(false) : undefined}
    accessibilityRole={spanning ? "header" : "button"}
    accessibilityLabel={rowLabel}
    accessibilityState={spanning ? { disabled: disabled || !selectableSpanning } : { selected, disabled }}
    // React Native for Web drops `accessibilityState` and reads the aria
    // attributes themselves, so a row that only stated its selection that way
    // was announced as an unselected, enabled button on every page: the
    // sidebar's current pane, the selected provider and a switched-off entry
    // all read alike.  The hosts keep the state they do honour.
    aria-selected={spanning ? undefined : selected}
    aria-disabled={spanning ? (disabled || !selectableSpanning) || undefined : disabled || undefined}
    style={rowStyle}
  >
    {sourceList && selected ? <View style={[windowsTableStyles.sourceSelectionIndicator, { top: rowHeight / 2 - 8 }]} /> : null}
    {spanning ? <Text numberOfLines={1} style={[windowsTableStyles.groupText, { color: textColor }]}>{row.cells[0] ?? ""}</Text> : <View style={windowsTableStyles.cells}>
      {columns.map((column, columnIndex) => {
        const cell = row.cells[columnIndex] ?? "";
        const cellKey = `${row.key}\x1f${columnIndex}`;
        const secondary = secondaryCellKeys.has(cellKey);
        const cellColor = secondary ? WINDOWS_CONTROL.textSecondary : textColor;
        // A leading tab is the layout's own statement that this cell is a
        // child of the group row above it, not a character to print.  Dropping
        // the mark and keeping the column's inset was what put a model at the
        // list's left edge while the key that owns it sat 12 pt in: the
        // hierarchy read upside down.  The mark now adds the indent it stands
        // for, on both fallback hosts.
        const marked = cell.startsWith("\t");
        const visibleCell = marked ? cell.slice(1) : cell;
        const inset = tableColumnInset(columnIndex === 0 ? firstColumnPadding : cellPadding) + (marked ? TABLE_CELL_MARK_INDENT : 0);
        const sourceImage = rowImageName ? windowsSourceImage(rowImageName) : undefined;
        if (columnIndex === 0 && sourceList) {
          // A source list never scrolls sideways: the row has to fit the pane it
          // navigates.  A fixed column width here (plus the cell and row insets)
          // overflowed the sidebar, and that overflow was scrollable, so the
          // whole column slid by a different amount on every selection.
          //
          // It does keep its inset, though.  This branch used to drop it along
          // with the width, so every source-list row — the sidebar and the
          // settings rail — began its text on the row's own leading edge, 4 pt
          // from the pane's border: 视觉路由 read as if it were struck through
          // by the rail's edge.  AppKit insets these cells (`badge` at 8, a
          // text-only rail row at the column's own padding), and the fallback
          // hosts now do the same: 8 pt beside a tile, the column's inset for a
          // row that is only text.
          const leadingInset = sourceImage !== undefined || rowSymbol ? TABLE_MIN_HORIZONTAL_PADDING : inset;
          return <View key={`${row.key}:${columnIndex}`} style={[windowsTableStyles.cellView, windowsTableStyles.sourceCellViewFlex, { paddingLeft: leadingInset, paddingRight: TABLE_MIN_HORIZONTAL_PADDING }]}>
            {/* A source list's tile is the same bundled PNG on every host.
                What must not happen on the web is the glyph fallback below: a
                browser has no "Segoe MDL2 Assets", so a glyph character renders
                as a missing-glyph box beside every sidebar row. A tile is a
                tile, and a missing one leaves the slot empty. */}
            {sourceImage !== undefined
              ? <Image source={sourceImage} style={windowsTableStyles.sourceImage} />
              : Platform.OS !== "web" && rowSymbol ? <Text style={[windowsTableStyles.symbol, { color: disabled ? WINDOWS_CONTROL.textDisabled : selected ? WINDOWS_CONTROL.text : WINDOWS_CONTROL.textSecondary }]}>{windowsSourceSymbol(rowSymbol)}</Text> : null}
            <View style={windowsTableStyles.sourceCellTextColumn}>
              <Text numberOfLines={1} style={[windowsTableStyles.cellText, { color: cellColor }]}>{visibleCell}</Text>
              {detail ? <Text numberOfLines={1} style={windowsTableStyles.sourceCellDetail}>{detail}</Text> : null}
            </View>
          </View>;
        }
        return <Text key={`${row.key}:${columnIndex}`} numberOfLines={1} style={[windowsTableStyles.cell, { width: column.width, paddingHorizontal: inset, color: cellColor }]}>{visibleCell}</Text>;
      })}
    </View>}
  </Pressable>;
}, sameTableRow);

function WindowsTable({ columns, rows, selectedKey, stripedRows, compact, followBottom, scrollToRow, framed, sourceList, rowSymbols, rowSymbolColors, rowImageNames, disabledRowKeys, secondaryRowKeys, secondaryCellKeys, rowDetails, selectableSpanningKeys, cellHorizontalPadding, firstColumnHorizontalPadding, onSelectionChange, onRowDoublePress, style, alertRowKeys }: {
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
  rowImageNames: string[];
  disabledRowKeys: string[];
  secondaryRowKeys: string[];
  secondaryCellKeys: string[];
  rowDetails: string[];
  selectableSpanningKeys: Set<string>;
  cellHorizontalPadding: number;
  firstColumnHorizontalPadding: number;
  onSelectionChange?: (key: string, index: number) => void;
  onRowDoublePress?: (key: string, index: number) => void;
  style?: StyleProp<ViewStyle>;
  alertRowKeys: string[];
}): React.JSX.Element {
  const lastPress = React.useRef<{ key: string; at: number } | undefined>(undefined);
  const verticalList = React.useRef<FlatList<TableRow>>(null);
  // One height for the whole list: `getItemLayout` is an arithmetic promise to
  // the list, so a row that grew taller than its neighbours can only exist if
  // every row grew with it.
  const twoLine = rowDetails.some((detail) => detail.length > 0);
  const rowHeight = windowsRowHeight(compact) + (twoLine ? 14 : 0);
  // Column widths a user dragged.  The macOS table lets a column be resized and
  // the browser table did not at all, so a long 密钥名 could only be read by
  // widening the whole window.  The drag is the one table behaviour that has to
  // be the same on both hosts, so it is kept here as a per-column override on
  // top of the caller's own widths: a table whose columns change (another
  // provider selected, another public model expanded) drops the overrides,
  // because a width dragged for one shape of table means nothing in another.
  const [draggedWidths, setDraggedWidths] = React.useState<Record<string, number>>({});
  const columnLabels = React.useMemo(() => columns.map((column) => column.label).join("\u001f"), [columns]);
  const dragShape = React.useRef(columnLabels);
  React.useEffect(() => {
    if (dragShape.current === columnLabels) return;
    dragShape.current = columnLabels;
    setDraggedWidths({});
  }, [columnLabels]);
  const laidOutColumns = React.useMemo(
    () => columns.map((column) => (draggedWidths[column.label] === undefined ? column : { ...column, width: draggedWidths[column.label] })),
    [columns, draggedWidths],
  );
  const totalWidth = laidOutColumns.reduce((sum, column) => sum + column.width, 0);
  // Built once per prop change instead of once per mounted row per cell: a
  // log that appends 20 entries a second otherwise rebuilt the same lookups
  // for every visible row on every frame.
  const disabledKeys = React.useMemo(() => new Set(disabledRowKeys), [disabledRowKeys]);
  const secondaryRowKeysSet = React.useMemo(() => new Set(secondaryRowKeys), [secondaryRowKeys]);
  const alertKeys = React.useMemo(() => new Set(alertRowKeys), [alertRowKeys]);
  const secondaryKeys = React.useMemo(() => new Set(secondaryCellKeys), [secondaryCellKeys]);
  React.useEffect(() => {
    if (scrollToRow < 0) return;
    // `FlatList.scrollToIndex` throws on an index past the data, and the old
    // `scrollTo({ y })` did not: a log tab that keeps a target while its rows
    // shrink (a filter, a cleared tab, a shorter page) would otherwise throw
    // out of this effect. That escape leaves React through the host's C++
    // boundary and aborts the process, so the target is clamped here and a
    // miss is a no-op rather than a crash.
    if (scrollToRow >= rows.length) return;
    verticalList.current?.scrollToIndex({ index: scrollToRow, animated: false });
  }, [scrollToRow, rows.length]);
  React.useEffect(() => {
    if (!followBottom) return;
    verticalList.current?.scrollToEnd({ animated: false });
  }, [followBottom, rows.length]);
  // The selectable set is read through a ref so the one stable callback keeps
  // its identity across a table re-render: a new closure here was enough to
  // invalidate every mounted row.
  const selectableSpanningRef = React.useRef(selectableSpanningKeys);
  selectableSpanningRef.current = selectableSpanningKeys;
  const select = React.useCallback((row: TableProps["rows"][number], index: number): void => {
    if (row.spanning && !selectableSpanningRef.current.has(row.key)) return;
    const now = Date.now();
    if (lastPress.current?.key === row.key && now - lastPress.current.at < 360) onRowDoublePress?.(row.key, index);
    lastPress.current = { key: row.key, at: now };
    onSelectionChange?.(row.key, index);
  }, [onRowDoublePress, onSelectionChange]);
  // The vertical thumb, the same 4 pt capsule `PersistentScrollView` draws:
  // the FlatList's overflow is the browser's own `overflow: auto`, and macOS
  // Chrome paints that as an overlay bar that only appears mid-scroll — a list
  // with more rows than it shows had no affordance at all saying so. The
  // metrics are read off the list node: RNW's `onScroll` fires, but the
  // content/viewport pair is cheaper here and stays right when rows resize.
  const listNode = React.useRef<HTMLElement | null>(null);
  const [scrollMetrics, setScrollMetrics] = React.useState({ overflow: false, height: 0, offset: 0 });
  const readScrollMetrics = React.useCallback(() => {
    const node = listNode.current;
    if (!node) return;
    const overflow = node.scrollHeight > node.clientHeight + 1;
    const height = overflow ? Math.max(24, (node.clientHeight * node.clientHeight) / node.scrollHeight) : 0;
    const range = Math.max(1, node.scrollHeight - node.clientHeight);
    const travel = Math.max(0, node.clientHeight - height - 4);
    setScrollMetrics({ overflow, height, offset: Math.min(travel, (node.scrollTop / range) * travel) });
  }, []);
  React.useEffect(() => {
    readScrollMetrics();
    // Rows arriving, resizing, or the pane changing height all move the ratio.
    if (!listNode.current || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(readScrollMetrics);
    observer.observe(listNode.current);
    return () => observer.disconnect();
  }, [readScrollMetrics, rows.length]);
  const renderRow = React.useCallback(({ item, index }: { item: TableRow; index: number }): React.JSX.Element => <WindowsTableRow
    row={item}
    index={index}
    rowHeight={rowHeight}
    columns={laidOutColumns}
    compact={compact}
    sourceList={sourceList}
    stripedRows={stripedRows}
    selected={item.key === selectedKey}
    disabled={disabledKeys.has(item.key)}
    secondaryRow={secondaryRowKeysSet.has(item.key)}
    alert={alertKeys.has(item.key)}
    rowSymbol={rowSymbols[index]}
    rowSymbolColor={rowSymbolColors[index]}
    rowImageName={rowImageNames[index]}
    secondaryCellKeys={secondaryKeys}
    detail={rowDetails[index] || undefined}
    selectableSpanning={selectableSpanningKeys.has(item.key)}
    cellPadding={cellHorizontalPadding}
    firstColumnPadding={firstColumnHorizontalPadding}
    onSelect={select}
  />, [alertKeys, cellHorizontalPadding, laidOutColumns, compact, disabledKeys, firstColumnHorizontalPadding, rowDetails, rowImageNames, rowSymbols, rowSymbolColors, secondaryKeys, secondaryRowKeysSet, selectableSpanningKeys, select, selectedKey, sourceList, stripedRows]);
  const keyExtractor = React.useCallback((row: TableRow): string => row.key, []);
  const body = <View style={[windowsTableStyles.horizontalBody, !sourceList && { minWidth: totalWidth }]}>
    {!sourceList ? <View style={[windowsTableStyles.header, { minWidth: totalWidth }]}>
      {/* A header cell is inset by the same number its column's cells are, and
          only by that number: the header row used to add its own 8 on top of
          this, so every header sat 8 pt to the right of the text under it and
          a table whose first column has no leading inset began its headings in
          the wrong place.  The inset lives on a cell wrapper rather than on the
          Text so the drag handle can sit on the cell's trailing edge, in the
          gutter between this column and the next. */}
      {laidOutColumns.map((column, columnIndex) => <View key={column.label} style={[windowsTableStyles.headerCell, { width: column.width, paddingLeft: tableColumnInset(columnIndex === 0 ? firstColumnHorizontalPadding : cellHorizontalPadding) }]}>
        <Text numberOfLines={1} style={windowsTableStyles.headerText}>{column.label}</Text>
        <ColumnResizeHandle label={column.label} width={column.width} onResize={(width) => setDraggedWidths((current) => ({ ...current, [column.label]: width }))} />
      </View>)}
    </View> : null}
    <FlatList
      ref={(instance) => {
        verticalList.current = instance;
        // The ref is the VirtualizedList, not a node: its scroll component (the
        // RNW ScrollView) answers `getScrollableNode`, which is the element the
        // browser's own `overflow: auto` lives on.  On the native leaves this
        // host never renders, so a node that has none simply leaves the thumb
        // unassembled.
        const scrollable = (instance as unknown as { getScrollableNode?: () => unknown } | null)?.getScrollableNode?.();
        listNode.current = scrollable instanceof HTMLElement ? scrollable : null;
      }}
      data={rows}
      renderItem={renderRow}
      keyExtractor={keyExtractor}
      style={windowsTableStyles.verticalList}
      contentContainerStyle={windowsTableStyles.body}
      nestedScrollEnabled
      initialNumToRender={24}
      maxToRenderPerBatch={24}
      windowSize={7}
      getItemLayout={(_data, index) => ({ length: rowHeight, offset: rowHeight * index, index })}
      scrollEventThrottle={16}
      onScroll={readScrollMetrics}
      // A target that scrolls out of range while the list is still laying
      // out is dropped instead of thrown: `scrollToIndex` fails loudly by
      // design, and a thrown error here escapes into the host process.
      onScrollToIndexFailed={() => undefined}
    />
    {scrollMetrics.overflow && scrollMetrics.height > 0 ? <View pointerEvents="none" style={[windowsTableStyles.tableScrollThumb, { height: scrollMetrics.height, transform: [{ translateY: scrollMetrics.offset }] }]} /> : null}
  </View>;
  return <View style={[windowsTableStyles.root, !framed && windowsTableStyles.borderless, style]}>
    {/* A source list is a navigation pane, so it lays its rows out at the pane's
        own width and never scrolls horizontally.  Wrapping it in the data
        table's horizontal scroller made the sidebar slide sideways by a
        per-selection amount once a row was a few points too wide. */}
    {sourceList ? body : <ScrollView horizontal showsHorizontalScrollIndicator style={windowsTableStyles.horizontalScroll} contentContainerStyle={windowsTableStyles.horizontalContent}>{body}</ScrollView>}
  </View>;
}

// The Windows control layer's one palette and metric set (Fluent 2, light).
//
// Every Windows-only style below reads these instead of repeating a hex value:
// the surface had drifted into four border greys (#c8c8c8, #d0d5dd, #d4d4d8,
// #e4e7ec), three body inks (#1a1a1a, #1f2937, #111827), three corner radii
// (3, 4, 6) and two focus colours (#0078d4 beside the accent #005fb8), so a
// button, a field and a table frame from the same pane never quite matched.
// The values are Fluent 2's own solid fills and strokes, flattened because the
// composition host paints over a solid window rather than a themed brush.
export const WINDOWS_CONTROL = {
  /** ControlStrokeColorDefault: the 1 pt bezel of a button, field or picker. */
  stroke: "#d1d1d1",
  /** ControlStrokeColorSecondary: dividers, row rules, the pane's right edge. */
  strokeSubtle: "#e5e5e5",
  /** ControlStrongStrokeColorDefault: a checkbox or radio box outline. */
  strokeStrong: "#8a8a8a",
  /** ControlFillColorDefault. */
  fill: "#ffffff",
  /** The quiet header band above a table. */
  fillSubtle: "#fafafa",
  /** SolidBackgroundFillColorBase: the shell, its sidebar and its status bar. */
  fillSolid: "#f3f3f3",
  /** SubtleFillColorSecondary: the selected row of a source list. */
  fillSelected: "#e9e9e9",
  /** One step lighter, so hover trails selection instead of matching it. */
  fillHover: "#ececec",
  /** TextFillColorPrimary. */
  text: "#1b1b1b",
  /**
   * The secondary ink: a row that is set aside, a column heading, a hint.
   *
   * It was `#5d5d5d` — 36% ink beside 89% ink — and a pane whose rows say
   * "nothing switched on" and rows that say nothing of the kind became the same
   * block of near-black at 13 pt.  The AppKit table has always drawn this state
   * in `NSColor.secondaryLabelColor`, which is 50% black in the light
   * appearance; that is the value here, so one list reads the same on all three
   * hosts and grey is visibly not black.
   */
  textSecondary: "#808080",
  /** TextFillColorTertiary: glyphs beside a label. */
  textTertiary: "#8a8a8a",
  /** TextFillColorDisabled. */
  textDisabled: "#98a2b3",
  /** AccentFillColorDefault. */
  accent: "#005fb8",
  /** A selected segment's wash behind accent ink. */
  accentSubtle: "#eaf3ff",
  /** SystemFillColorCritical. */
  danger: "#c42b1c",
  /** SystemFillColorCaution, for a row that needs attention. */
  warning: "#8a5a00",
  /** ControlCornerRadius. */
  radius: 4,
  /** The compact control height every Windows button, field and picker shares. */
  controlHeight: 24,
  /** A row of a source list, matching the shell's own 36 pt navigation row. */
  sourceRowHeight: 36,
} as const;

/**
 * The smallest inset a table column gets, on every host.
 *
 * The AppKit table has always clamped both paddings to 8 (`LiteLLMTableMinimumHorizontalPadding`),
 * so a pane that asked for 0 on macOS still put 8 pt between the frame and the
 * first character.  The two fallback hosts took the request literally, and the
 * model list — whose first column is declared with a 0 inset — began its text
 * on the frame's own stroke.  The clamp lives here so the three hosts agree on
 * one number instead of on one author's intent.
 */
const TABLE_MIN_HORIZONTAL_PADDING = 8;
/**
 * The indent a leading `\t` mark stands for, in a cell that has one.
 *
 * The mark says "this row is filed under the group above it".  AppKit prints
 * the character itself and the tab stop indents it; the fallback hosts dropped
 * it, which left the child of a group at the left edge and the group heading
 * indented — the hierarchy upside down.
 */
const TABLE_CELL_MARK_INDENT = 12;

function tableColumnInset(requested: number): number {
  return Math.max(TABLE_MIN_HORIZONTAL_PADDING, requested);
}

const windowsTableStyles = StyleSheet.create({
  root: { minHeight: 120, overflow: "hidden", borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, borderRadius: WINDOWS_CONTROL.radius, backgroundColor: WINDOWS_CONTROL.fill },
  borderless: { borderWidth: 0, borderRadius: 0, backgroundColor: "transparent" },
  header: { flexDirection: "row", alignItems: "center", minHeight: 30, backgroundColor: WINDOWS_CONTROL.fillSubtle, borderBottomWidth: 1, borderBottomColor: WINDOWS_CONTROL.strokeSubtle },
  headerCell: { flexDirection: "row", alignItems: "center", height: "100%" },
  headerText: { flexShrink: 1, color: WINDOWS_CONTROL.textSecondary, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
  // The drag target is the gutter itself: 6 pt wide, full height of the header,
  // and invisible until the pointer is over it.
  resizeHandle: { width: 6, alignSelf: "stretch" },
  resizeHandleHovered: { backgroundColor: WINDOWS_CONTROL.stroke },
  horizontalScroll: { flex: 1, minHeight: 0 },
  horizontalContent: { flexGrow: 1, minHeight: 0 },
  horizontalBody: { flex: 1, minHeight: 0 },
  verticalList: { flex: 1, minHeight: 0 },
  // The table's persistent 4 pt capsule, over the rows' trailing edge.  A
  // browser's own `overflow: auto` bar is an overlay that shows itself only
  // mid-scroll, so a list with more rows than it shows read as a list that
  // ended — the keys list in the provider inspector, four rows of nine.
  tableScrollThumb: { position: "absolute", top: 2, right: 2, width: 4, borderRadius: 2, backgroundColor: "rgba(96,96,96,0.55)" },
  body: { flexGrow: 1 },
  row: { minWidth: "100%", borderBottomWidth: 1, borderBottomColor: WINDOWS_CONTROL.strokeSubtle, justifyContent: "center" },
  // A source list is a navigation surface, not a data table: no row rules,
  // a small inset, and a rounded quiet fill.  It is what Windows Settings,
  // Explorer's navigation pane, and NavigationView draw.
  sourceRow: { minWidth: 0, marginHorizontal: 4, borderRadius: 4, borderBottomWidth: 0 },
  sourceRowSelected: { backgroundColor: WINDOWS_CONTROL.fillSelected },
  sourceRowHover: { backgroundColor: WINDOWS_CONTROL.fillHover },
  sourceSelectionIndicator: { position: "absolute", left: 0, width: 3, height: 16, borderRadius: 1.5, backgroundColor: WINDOWS_CONTROL.accent },
  rowCompact: { minHeight: 30 },
  rowRelaxed: { minHeight: 36 },
  rowStripe: { backgroundColor: WINDOWS_CONTROL.fillSubtle },
  rowSelected: { backgroundColor: WINDOWS_CONTROL.accentSubtle, borderLeftWidth: 3, borderLeftColor: WINDOWS_CONTROL.accent },
  rowDisabled: { opacity: 0.72 },
  // The cells row adds no inset of its own: a cell's inset is the table's
    // `cellHorizontalPadding`, applied per column below, and a container that
    // also padded pushed every cell (and the header above it) off by 8.
  cells: { flexDirection: "row", alignItems: "center" },
  sourceCellTextColumn: { flex: 1, minWidth: 0, justifyContent: "center" },
  sourceCellDetail: { color: WINDOWS_CONTROL.textSecondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14 },
  cellView: { flexDirection: "row", alignItems: "center", minWidth: 0 },
  sourceCellViewFlex: { flex: 1, minWidth: 0 },
  cellText: { flexShrink: 1, minWidth: 0, fontSize: UI_FONT_SIZE },
  cell: { color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE },
  sourceCell: { flexDirection: "row", alignItems: "center" },
  sourceImage: { width: 20, height: 20, marginRight: 8, borderRadius: 4 },
  symbol: { width: 20, height: 20, marginRight: 8, fontFamily: GLYPH_FONT_FAMILY, fontSize: 16, lineHeight: 20, textAlign: "center", transform: [{ translateY: 1 }] },
  groupText: { paddingHorizontal: 12, color: WINDOWS_CONTROL.textSecondary, fontSize: UI_TIP_FONT_SIZE, fontWeight: "600" },
});

// The empty lists are module constants, not fresh `[]` defaults: a new array
// on every render invalidated the memoized lookup Sets in `WindowsTable` (and
// with them every mounted row) even when no prop changed.
const EMPTY_STRINGS: string[] = [];

export function NativeTable({ columns, rows, selectedKey = "", striped = true, compact = true, followBottom = false, scrollToRow = -1, framed = true, sourceList = false, rowSymbols = EMPTY_STRINGS, rowSymbolColors = EMPTY_STRINGS, rowImageNames = EMPTY_STRINGS, cellHorizontalPadding = 8, firstColumnHorizontalPadding = 8, preserveColumnWidths = false, scrollTrailingColumnOverflow = false, disabledRowKeys = EMPTY_STRINGS, secondaryRowKeys = EMPTY_STRINGS, secondaryCellKeys = EMPTY_STRINGS, rowDetails = EMPTY_STRINGS, selectableSpanningRowKeys = EMPTY_STRINGS, onSelectionChange, onRowDoublePress, style, alertRowKeys = EMPTY_STRINGS }: TableProps): React.JSX.Element {
  // Striped rows belong to the list surface, not to its contents: the native
  // table paints its alternating bands across the whole viewport, so an empty
  // list shows the same stripes and reads as a list that happens to have no
  // rows. Deriving this from `rows.length` made every empty list a blank white
  // block, indistinguishable from the pane behind it.
  const stripedRows = striped && !sourceList;
  // One Set per data change, not one per render: `WindowsTable` hands it to
  // every row, so a fresh Set defeated the row memo and re-rendered the whole
  // mounted window whenever the parent published anything.
  const spanningRowKeys = React.useMemo(() => rows.filter((row) => row.spanning).map((row) => row.key), [rows]);
  const selectableSpanningKeys = React.useMemo(() => {
    const spanning = new Set(spanningRowKeys);
    return new Set(selectableSpanningRowKeys.filter((key) => spanning.has(key)));
  }, [spanningRowKeys, selectableSpanningRowKeys]);
  if (Platform.OS === "windows" || Platform.OS === "web") {
    // This React table is the only Windows table path, and the browser's too:
    // a browser has no AppKit table to delegate to, and the fallback below is a
    // one-line-per-row list that drops every column this app reads.  The
    // Windows leaf is already a virtualized React table, so the browser host
    // gets the same rows, columns, stripes and selection instead of a stub.
    return <WindowsTable columns={columns} rows={rows} selectedKey={selectedKey} stripedRows={stripedRows} compact={compact} followBottom={followBottom} scrollToRow={scrollToRow} framed={framed} sourceList={sourceList} rowSymbols={rowSymbols} rowSymbolColors={rowSymbolColors} rowImageNames={rowImageNames} disabledRowKeys={disabledRowKeys} secondaryRowKeys={secondaryRowKeys} secondaryCellKeys={secondaryCellKeys} rowDetails={rowDetails} selectableSpanningKeys={selectableSpanningKeys}
      // A caller that names its own inset expects it on the web too: these two
      // reached only the macOS table, so a list that asked for 6 or 0 was drawn
      // at this table's fixed 8 and its header 16 — the pane's own alignment
      // was a value the browser silently ignored.
      cellHorizontalPadding={cellHorizontalPadding} firstColumnHorizontalPadding={firstColumnHorizontalPadding}
      onSelectionChange={onSelectionChange} onRowDoublePress={onRowDoublePress} alertRowKeys={alertRowKeys} style={[styles.table, style]} />;
  }
  // The macOS bridge props are built per render, and the list above them can be
  // thousands of rows: building them before the platform branch made a Windows
  // log table flatten every cell into a second copy nobody read.
  if (Platform.OS === "macos") {
    // Every key here must also appear in the macOS wrapper's own parameter list:
    // `AppKitControls.tsx` forwards props by hand, so a prop added on one host and
    // forgotten on the other arrives empty instead of failing to compile.
    const nativeProps = {
      columnLabels: columns.map((column) => column.label),
      columnWidths: columns.map((column) => column.width),
      rowKeys: rows.map((row) => row.key),
      // The native tables address cells positionally by row * columnCount, so
      // every row owns exactly one columnCount slice.  A spanning (group) row
      // carries one value for its full-width cell, and a trailing spacer column
      // carries none, so both are padded here — an unpadded row shifted every
      // row after it into its neighbour's columns.
      cells: rows.flatMap((row) => {
        const cells = row.spanning ? [row.cells[0] ?? ""] : row.cells.slice(0, columns.length);
        return [...cells, ...Array(Math.max(0, columns.length - cells.length)).fill("")];
      }),
      // Same positional rule as `cells`, for the same reason: the native table
      // indexes a cell's tooltip by row * columnCount.  A table whose cells all
      // say what they mean sends nothing, not a second copy of the grid.
      cellTooltips: rows.some((row) => (row.tooltips?.length ?? 0) > 0)
        ? rows.flatMap((row) => {
          const tooltips = row.spanning ? [row.tooltips?.[0] ?? ""] : (row.tooltips ?? []).slice(0, columns.length);
          return [...tooltips, ...Array(Math.max(0, columns.length - tooltips.length)).fill("")];
        })
        : EMPTY_STRINGS,
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
      secondaryRowKeys,
      secondaryCellKeys,
      // An all-empty list is the same as none: the native table reads a non-empty
      // list as "these rows are two lines tall", so the padding below never has to
      // invent a second line the UI did not ask for.
      rowDetails: rows.some((_, index) => (rowDetails[index] ?? "").length > 0)
        ? rows.map((_, index) => rowDetails[index] ?? "")
        : EMPTY_STRINGS,
      alertRowKeys,
      spanningRowKeys,
      selectableSpanningRowKeys: Array.from(selectableSpanningKeys),
    };
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
  return <WebSecureTextInput domain={domain} field={field} target={target} label={label} placeholder={placeholder} multiline={multiline} plainText={plainText} disabled={disabled} commitRequest={commitRequest} resetRequest={resetRequest} onSecretState={onSecretState} style={style} />;
}

/**
 * The browser's password field.
 *
 * The native hosts keep the secret inside their own field so it never enters
 * React state; a browser has no such leaf — the value has to live in an
 * `<input>` in this document.  It is therefore the browser host's own copy of
 * the one exception the Windows host already documents (General's plaintext
 * field): the value is held by the control, handed to Core through the
 * read/stage capability leases, and reported to the pane only as a status —
 * never echoed into a snapshot.
 *
 * What it deliberately does not do: cache a value across mounts, write it to
 * storage, or report it back through `onSecretState`.  A pane learns `dirty`,
 * `saving`, `saved`, `error`, exactly like the native leaves tell it.
 */
function WebSecureTextInput({ domain, field, target, label, placeholder, multiline, plainText, disabled, commitRequest, resetRequest, onSecretState, style }: {
  domain: string;
  field: string;
  target: string;
  label: string;
  placeholder?: string;
  multiline: boolean;
  plainText: boolean;
  disabled?: boolean;
  commitRequest?: number;
  resetRequest?: number;
  onSecretState?: (state: SecureTextInputState) => void;
  style?: StyleProp<ViewStyle>;
}): React.JSX.Element {
  // The shared UI owns commit timing for a field that commits on demand
  // (`NativeSecretInputControl` in `YoungRouterApp.tsx`): it asks the host to
  // commit through `commitRequest`.  An `autoCommit` field has no such asker —
  // the native leaves stage it after their own quiet period — so this leaf
  // owns that timer itself.
  const inputRef = React.useRef<TextInput>(null);
  const valueRef = React.useRef("");
  // The loaded value is written straight into the field's DOM node. React
  // state must not carry a secret, and React Native for Web's `setNativeProps`
  // does not update an uncontrolled `<input>`'s value.
  const fieldId = React.useRef(`yr-secret-${nextSecretFieldId()}`);
  const setFieldValue = (next: string): void => {
    const node = document.getElementById(fieldId.current);
    if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) node.value = next;
  };
  const lastCommit = React.useRef(0);
  const lastReset = React.useRef(resetRequest ?? 0);
  const report = (status: string, present: boolean, error = "", revision = 0): void => {
    onSecretState?.({ revision, present, status, error, commitRequest: commitRequest ?? 0 });
  };
  const stage = React.useCallback((): void => {
    const stager = getBrowserSecretStager();
    if (!stager) {
      report("error", valueRef.current.length > 0, "The secret field is unavailable");
      return;
    }
    report("saving", valueRef.current.length > 0);
    void stager({ domain, field, target, value: valueRef.current }).then((staged) => {
      report("ready", staged.present, "", staged.revision);
    }).catch((error: unknown) => report("error", valueRef.current.length > 0, error instanceof Error ? error.message : "The secret could not be saved"));
  }, [domain, field, target]);
  React.useEffect(() => {
    let current = true;
    valueRef.current = "";
    setFieldValue("");
    inputRef.current?.clear();
    const reader = getBrowserSecretReader();
    if (!reader) return () => { current = false; };
    report("loading", false);
    void reader({ domain, field, target }).then((secret) => {
      if (!current) return;
      const value = secret.value ?? "";
      valueRef.current = value;
      if (value) setFieldValue(value);
      report("ready", secret.present);
    }).catch(() => { if (current) report("error", false, "The secret field is unavailable"); });
    return () => { current = false; };
  }, [domain, field, target]);
  React.useEffect(() => {
    if (lastReset.current === (resetRequest ?? 0)) return;
    lastReset.current = resetRequest ?? 0;
    valueRef.current = "";
    setFieldValue("");
    inputRef.current?.clear();
    report("ready", false);
  }, [resetRequest]);
  React.useEffect(() => {
    // This leaf is the one that asked, so its own timer's write must not be
    // mistaken for a caller's request (`lastCommit` tracks the latter).
    const request = commitRequest ?? 0;
    if (request === 0 || request === lastCommit.current) return;
    lastCommit.current = request;
    stage();
  }, [commitRequest, stage]);
  return <TextInput
    accessibilityLabel={label}
    placeholder={placeholder}
    placeholderTextColor={webPlaceholderInk()}
    nativeID={fieldId.current}
    ref={inputRef}
    editable={disabled !== true}
    multiline={multiline}
    secureTextEntry={!plainText}
    autoCapitalize="none"
    autoCorrect={false}
    onChangeText={(next) => {
      valueRef.current = next;
      // `dirty` is what makes the shared controller schedule its quiet-period
      // commit (for an auto-commit field) or enable its own button.  The write
      // itself is one call, from `commitRequest`: a second timer here would
      // stage the same edit twice and the second answer is a revision conflict.
      report("dirty", next.length > 0);
    }}
    onBlur={() => report("ready", valueRef.current.length > 0)}
    style={[styles.webSecretField, multiline && styles.webSecretFieldMultiline, style]}
  />;
}

/** One stable DOM id per secret field, so the host can fill it without state. */
let secretFieldSerial = 0;
function nextSecretFieldId(): number {
  secretFieldSerial += 1;
  return secretFieldSerial;
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
  windowsButton: { minHeight: WINDOWS_CONTROL.controlHeight, minWidth: 28, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 4, paddingHorizontal: 8, borderRadius: WINDOWS_CONTROL.radius, borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, backgroundColor: WINDOWS_CONTROL.fill },
  windowsButtonCompact: { minHeight: WINDOWS_CONTROL.controlHeight, paddingHorizontal: 6 },
  windowsButtonLink: { borderWidth: 0, backgroundColor: "transparent", paddingHorizontal: 0 },
  windowsButtonPlainLink: { borderWidth: 0, backgroundColor: "transparent", paddingHorizontal: 0, justifyContent: "flex-start" },
  windowsButtonPrimary: { borderColor: WINDOWS_CONTROL.accent, backgroundColor: WINDOWS_CONTROL.accent },
  windowsButtonDestructive: { borderColor: WINDOWS_CONTROL.danger, backgroundColor: WINDOWS_CONTROL.danger },
  windowsButtonDisabled: { opacity: 0.55 },
  windowsButtonGlyph: { fontFamily: GLYPH_FONT_FAMILY, fontSize: UI_FONT_SIZE, lineHeight: 16 },
  // A ringed mark, sized to the glyph it replaces: 13 pt of circle beside a
  // label, with the character centred inside it by its own line height rather
  // than by the 16 pt the plain glyph uses.
  windowsButtonRing: { width: 13, height: 13, borderRadius: 6.5, borderWidth: 1, alignItems: "center", justifyContent: "center" },
  windowsButtonRingMark: { fontSize: 9, lineHeight: 11, fontWeight: "600" },
  windowsButtonTitle: { fontSize: UI_FONT_SIZE, flexShrink: 1 },
  windowsButtonLinkTitle: { textDecorationLine: "underline" },
  windowsButtonPlainLinkTitle: { textDecorationLine: "underline", fontWeight: "400" },
  windowsSegmented: { flexDirection: "row", alignItems: "stretch", minHeight: WINDOWS_CONTROL.controlHeight, borderRadius: WINDOWS_CONTROL.radius, borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, overflow: "hidden", backgroundColor: WINDOWS_CONTROL.fill },
  windowsSegmentedCompact: { minHeight: WINDOWS_CONTROL.controlHeight },
  windowsSegment: { flex: 1, minWidth: 0, alignItems: "center", justifyContent: "center", paddingHorizontal: 6, borderRightWidth: 1, borderRightColor: WINDOWS_CONTROL.strokeSubtle },
  windowsSegmentFirst: { borderTopLeftRadius: WINDOWS_CONTROL.radius - 1, borderBottomLeftRadius: WINDOWS_CONTROL.radius - 1 },
  windowsSegmentLast: { borderRightWidth: 0, borderTopRightRadius: WINDOWS_CONTROL.radius - 1, borderBottomRightRadius: WINDOWS_CONTROL.radius - 1 },
  // One ink for both the tab labels and the toolbar buttons beside them: the
  // 12 pt secondary grey read as a different control family next to a 13 pt
  // button label on the same row.
  windowsSegmentSelected: { backgroundColor: WINDOWS_CONTROL.accentSubtle },
  windowsSegmentText: { color: WINDOWS_CONTROL.textSecondary, fontSize: UI_FONT_SIZE },
  windowsSegmentTextSelected: { color: WINDOWS_CONTROL.accent, fontWeight: "600" },
  windowsCheckboxCompact: { minHeight: WINDOWS_CONTROL.controlHeight },
  // Fluent 2's ToggleSwitch: a 40 x 20 track with a 12 pt knob.  RNW's
  // <Switch> draws its own UIKit-shaped control, so the one surface outside
  // this palette was a grey track with a dark knob in both states, beside
  // Fluent buttons that had just been unified.
  windowsToggle: { minWidth: 40, minHeight: WINDOWS_CONTROL.controlHeight, justifyContent: "center" },
  windowsToggleTrack: { width: 40, height: 20, flexDirection: "row", alignItems: "center", justifyContent: "flex-start", paddingHorizontal: 3, borderWidth: 1, borderRadius: 10, borderColor: WINDOWS_CONTROL.strokeStrong, backgroundColor: "transparent" },
  // The knob's travel comes from the track's own alignment, so no
  // absolutely-positioned child can be left behind by a re-render.
  windowsToggleTrackOn: { justifyContent: "flex-end", borderColor: WINDOWS_CONTROL.accent, backgroundColor: WINDOWS_CONTROL.accent },
  windowsToggleKnob: { width: 12, height: 12, borderRadius: 6, backgroundColor: WINDOWS_CONTROL.textSecondary },
  windowsToggleKnobOn: { backgroundColor: WINDOWS_CONTROL.fill },
  windowsCheckboxBox: { width: 16, height: 16, alignItems: "center", justifyContent: "center", borderWidth: 1, borderColor: WINDOWS_CONTROL.strokeStrong, borderRadius: WINDOWS_CONTROL.radius - 2, backgroundColor: WINDOWS_CONTROL.fill },
  windowsCheckboxBoxChecked: { borderColor: WINDOWS_CONTROL.accent, backgroundColor: WINDOWS_CONTROL.accent },
  windowsCheckboxMark: { color: WINDOWS_CONTROL.fill, fontSize: 12, lineHeight: 14, fontWeight: "700" },
  windowsCheckboxLabel: { color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE, flexShrink: 1 },
  windowsDisabled: { opacity: 0.55 },
  windowsDisabledText: { color: WINDOWS_CONTROL.textDisabled },
  windowsPicker: { minHeight: WINDOWS_CONTROL.controlHeight, flexDirection: "row", alignItems: "center", paddingHorizontal: 6, borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, borderRadius: WINDOWS_CONTROL.radius, backgroundColor: WINDOWS_CONTROL.fill },
  windowsPickerCompact: { minHeight: WINDOWS_CONTROL.controlHeight },
  windowsPickerDisabled: { opacity: 0.55 },
  windowsPickerText: { flex: 1, minWidth: 0, color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE },
  windowsPickerChevron: { color: WINDOWS_CONTROL.textSecondary, fontSize: UI_TIP_FONT_SIZE, marginLeft: 4 },
  linkButton: { minWidth: 72, minHeight: 22 },
  segmented: { minHeight: 24 },
  toggleFallback: { minWidth: 24, minHeight: 24, justifyContent: "center" },
  checkboxFallback: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6 },
  staticBooleanIndicator: { width: 16, height: 16, borderWidth: 1, borderColor: WINDOWS_CONTROL.strokeStrong, alignItems: "center", justifyContent: "center", backgroundColor: WINDOWS_CONTROL.fill },
  staticBooleanIndicatorChecked: { backgroundColor: WINDOWS_CONTROL.accentSubtle, borderColor: WINDOWS_CONTROL.accent },
  staticBooleanIndicatorText: { fontSize: UI_TIP_FONT_SIZE, lineHeight: 14, color: WINDOWS_CONTROL.text, fontWeight: "700" },
  selectableRow: { minHeight: 28, justifyContent: "center", paddingHorizontal: 8 },
  selectableRowSelected: { backgroundColor: WINDOWS_CONTROL.accentSubtle },
  selectableText: { gap: 2 },
  selectableTitle: { fontSize: UI_FONT_SIZE },
  selectableDetail: { fontSize: UI_FONT_SIZE, opacity: 0.65 },
  table: { minHeight: 120 },
  // Let the surrounding window provide scrolling when a fallback host needs
  // it; `overflow: scroll` would force an empty scrollbar gutter on every
  // table, even when all rows fit.
  tableFallback: { minHeight: 120, borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, overflow: "hidden" },
  tableFallbackUnframed: { borderWidth: 0 },
  tableFallbackStripe: { backgroundColor: WINDOWS_CONTROL.fillSubtle },
  tableFallbackGroupRow: { minHeight: 26, justifyContent: "center", paddingHorizontal: 8 },
  tableFallbackGroupText: { fontWeight: "400" },
  editor: { minHeight: 160 },
  editorFallback: { minHeight: 160, textAlignVertical: "top", fontSize: UI_FONT_SIZE },
  webPickerFrame: { minHeight: WINDOWS_CONTROL.controlHeight, justifyContent: "center" },
  // The browser host's secret field keeps the pane's own frame: it is the only
  // credential a browser draws, so it must not look like a browser widget.
  webSecretField: { minHeight: WINDOWS_CONTROL.controlHeight, paddingHorizontal: 6, borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, borderRadius: WINDOWS_CONTROL.radius, backgroundColor: WINDOWS_CONTROL.fill, color: WINDOWS_CONTROL.text, fontSize: UI_FONT_SIZE },
  webSecretFieldMultiline: { minHeight: 108, paddingTop: 4, paddingBottom: 4, textAlignVertical: "top" },
  windowsTextField: { minHeight: WINDOWS_CONTROL.controlHeight, justifyContent: "center", overflow: "hidden", borderWidth: 1, borderColor: WINDOWS_CONTROL.stroke, borderRadius: WINDOWS_CONTROL.radius, backgroundColor: WINDOWS_CONTROL.fill },
  webTextFieldInput: { flex: 1, minWidth: 0, paddingHorizontal: 8, paddingVertical: 3, fontSize: UI_FONT_SIZE, color: WINDOWS_CONTROL.text, backgroundColor: "transparent" },
  webTextFieldInputMultiline: { paddingTop: 6, paddingBottom: 6, textAlignVertical: "top" },
  // The strip fills the row it was given and scrolls inside it: a browser can
  // show a segmented control wider than its container, a native one cannot.
  // `flexGrow: 0` undoes React Native for Web's own ScrollView default, which
  // otherwise eats the whole column and pushed the toolbar to mid-pane.
  webSegmentedScroll: { width: "100%", flexGrow: 0, flexShrink: 1, minWidth: 0 },
  webSegmentedContent: { minWidth: "100%" },
  webSegment: { flexGrow: 1, flexShrink: 0, minWidth: 64 },
  windowsTextFieldFocused: { borderColor: WINDOWS_CONTROL.accent },
  windowsTextFieldDisabled: { opacity: 0.5 },
  // RNW's Windows single-line leaf ignores `textAlignVertical`, and its own
  // vertical placement is `paddingTop + (contentFrame - naturalHeight) / 2`,
  // where the natural height measures as zero. The line therefore sits at
  // `(frame + paddingTop - paddingBottom) / 2 + half a line`, so the only way to
  // centre it at any frame height is a constant difference between the two
  // paddings: 21 DIP, i.e. one line plus its leading. Measured on the installed
  // build at 192 DPI, this puts a 24 DIP field's ink on the optical centre of
  // its bezel instead of against the bottom edge.
  windowsTextFieldInput: { flex: 1, minWidth: 0, paddingHorizontal: 8, paddingTop: 0, paddingBottom: 21, fontSize: UI_FONT_SIZE, color: WINDOWS_CONTROL.text, backgroundColor: "transparent" },
  // A multiline leaf aligns to the top instead (the host returns early for it),
  // so it only needs even padding around the block of text.
  windowsTextFieldInputMultiline: { paddingTop: 4, paddingBottom: 4, paddingRight: 12, textAlignVertical: "top" },
  splitView: { minHeight: 120, flexDirection: "row" },
  splitFallback: { minHeight: 120, flexDirection: "row" },
  splitLeading: { minWidth: 0, flexShrink: 0 },
  splitter: { width: 6, minWidth: 6, alignSelf: "stretch", zIndex: 1 },
  splitTrailing: { minWidth: 0, flex: 1 },
});
