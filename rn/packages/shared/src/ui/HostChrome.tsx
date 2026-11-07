/**
 * The browser host's own surfaces, drawn with the app's controls.
 *
 * A page has no windows and no menus, so the surfaces the native leaves
 * implement (a decision panel, a text prompt, a read-only viewer, a model
 * picker, an anchored menu) are layers in this document.  They used to be built
 * with `document.createElement`, which gave the web host a second hand-written
 * stylesheet: a question asked by the same app looked like a different program,
 * the viewer's script never ran under the page's own CSP, and every surface
 * listened for Escape itself, so one Escape closed all of them.
 *
 * This component draws them with the same primitives every pane uses and hands
 * Escape to the one presentation stack, so only the surface a person is looking
 * at answers a key.  Only the web entry mounts it; a native host keeps its
 * AppKit/WinUI surfaces and nothing here runs there.
 */

import React from "react";
import { Platform, ScrollView, StyleProp, StyleSheet, Text, View, ViewStyle } from "react-native";
import { NativeButton, NativeCheckbox, NativeTextField, WINDOWS_CONTROL } from "./NativeControls";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";
import { pushPresentation } from "../platform/presentationStack";
import {
  dismissalFor,
  hostChromeSnapshot,
  settleHostChrome,
  subscribeHostChrome,
  topHostChrome,
  type HostChromeRequest,
} from "../platform/hostChrome";

const COLOR = {
  surface: WINDOWS_CONTROL.fill,
  header: WINDOWS_CONTROL.fillSolid,
  // A card is not a control: the bezel of a button (#d1d1d1) is meant to sit on
  // a pane, and a white card carrying that bezel over a dimmed white page is a
  // colour block with no outline.  The edge of a floating surface is stated
  // with the stroke the child layers use.
  edge: "#a9a9b0",
  stroke: WINDOWS_CONTROL.stroke,
  rule: WINDOWS_CONTROL.strokeSubtle,
  text: WINDOWS_CONTROL.text,
  secondary: WINDOWS_CONTROL.textSecondary,
};

/** Where a menu is anchored, in viewport coordinates. */
const MENU_MIN_WIDTH = 180;

export function HostChromeHost(): React.JSX.Element | null {
  const requests = React.useSyncExternalStore(subscribeHostChrome, hostChromeSnapshot, hostChromeSnapshot);
  // Only the topmost surface is drawn: two answers are never asked at once, and
  // a menu over a dialog must not compete with that dialog's keys.
  const request = requests[requests.length - 1];

  React.useEffect(() => {
    if (request === undefined) return undefined;
    // The same stack the child layers use: Escape answering here is Escape
    // answering the surface on top of whatever is behind it.
    return pushPresentation({ dismiss: () => settleHostChrome(request, dismissalFor(request)) });
  }, [request]);

  if (request === undefined) return null;
  switch (request.kind) {
    case "confirm": return <DecisionSurface request={request} />;
    case "prompt": return <PromptSurface request={request} />;
    case "viewer": return <ViewerSurface request={request} />;
    case "picker": return <PickerSurface request={request} />;
    case "menu": return <MenuSurface request={request} />;
  }
}

/** The frame every dialog shares: scrim, card, title, body, footer. */
function DialogFrame({ title, children, footer, cardStyle }: { title: string; children: React.ReactNode; footer: React.ReactNode; cardStyle?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <View style={styles.scrim} {...({ role: "dialog", "aria-modal": true, "aria-label": title } as object)}>
    <View style={[styles.card, cardStyle]}>
      <Text style={styles.cardTitle}>{title}</Text>
      {children}
      <View style={styles.footer}>{footer}</View>
    </View>
  </View>;
}

function DecisionSurface({ request }: { request: Extract<HostChromeRequest, { kind: "confirm" }> }): React.JSX.Element {
  return <DialogFrame
    title={request.title}
    footer={<>
      <View style={styles.footerSpacer} />
      <NativeButton title={request.cancelLabel} compact onPress={() => settleHostChrome(request, false)} />
      <NativeButton title={request.confirmLabel} compact primary={!request.destructive} destructive={request.destructive} onPress={() => settleHostChrome(request, true)} />
    </>}
  >
    <ScrollView style={styles.cardBody} contentContainerStyle={styles.cardBodyContent}>
      <Text style={styles.bodyText}>{request.message}</Text>
    </ScrollView>
  </DialogFrame>;
}

function PromptSurface({ request }: { request: Extract<HostChromeRequest, { kind: "prompt" }> }): React.JSX.Element {
  const [draft, setDraft] = React.useState(request.value);
  const submit = (): void => settleHostChrome(request, draft);
  // The same rule every save surface here follows: a primary button that would
  // hand back the value the prompt opened on has nothing to confirm.
  const pending = draft.trim() !== "" && draft !== request.value;
  return <DialogFrame
    title={request.title}
    footer={<>
      <View style={styles.footerSpacer} />
      <NativeButton title={request.cancelLabel} compact onPress={() => settleHostChrome(request, null)} />
      <NativeButton title={request.confirmLabel} compact primary disabled={!pending} onPress={submit} />
    </>}
  >
    <View style={styles.cardBodyContent}>
      <NativeTextField
        value={draft}
        autoFocus
        onChangeText={setDraft}
        onSubmitEditing={submit}
        style={styles.promptField}
        accessibilityLabel={request.title}
      />
    </View>
  </DialogFrame>;
}

function ViewerSurface({ request }: { request: Extract<HostChromeRequest, { kind: "viewer" }> }): React.JSX.Element {
  return <View style={styles.scrim} {...({ role: "dialog", "aria-modal": true, "aria-label": request.title } as object)}>
    <View style={styles.viewerCard}>
      <View style={styles.viewerHeader}>
        <Text numberOfLines={1} style={styles.viewerTitle}>{request.title}</Text>
        <NativeButton title={request.closeLabel} compact onPress={() => settleHostChrome(request, undefined)} />
      </View>
      <ScrollView style={styles.viewerBody} contentContainerStyle={styles.viewerBodyContent}>
        <Text selectable style={styles.viewerText}>{request.text}</Text>
      </ScrollView>
    </View>
  </View>;
}

/**
 * The model chooser: the one surface whose job is to pick several things out of
 * a list that is longer than the window.
 *
 * The two native hosts build it as a panel with a heading, the provider and key
 * it is choosing for, a search field that focuses itself, 全选 and 反选 over the
 * rows the filter left, and a count that says both how many there are and how
 * many are ticked.  This host drew the checkboxes and a footer, so a station
 * that offers ninety models could only be searched by scrolling — the same
 * window, missing the two controls a list of ninety needs.
 *
 * The filter matches the way the AppKit list does: case- and width-folded
 * substring, so a search for `flash` finds `GLM-5.3-FLASH-FREE`.
 */
function PickerSurface({ request }: { request: Extract<HostChromeRequest, { kind: "picker" }> }): React.JSX.Element {
  const labels = request.labels;
  // Nothing is ticked when the chooser opens.  This host used to open with the
  // whole list selected, so 添加已选模型 added every model a station offered
  // without anyone ticking one — and the count read "已选 12 个" beside a search
  // that had found three.  Both native hosts open at zero and keep Add disabled
  // until a row is ticked.
  const [selected, setSelected] = React.useState<string[]>([]);
  const [query, setQuery] = React.useState("");
  const folded = React.useMemo(() => request.models.map((model) => ({ model, key: model.normalize("NFKC").toLocaleLowerCase() })), [request.models]);
  const needle = query.trim().normalize("NFKC").toLocaleLowerCase();
  const visible = needle === "" ? request.models : folded.filter((entry) => entry.key.includes(needle)).map((entry) => entry.model);
  const toggle = (model: string, on: boolean): void => {
    setSelected((current) => on
      ? (current.includes(model) ? current : [...current, model])
      : current.filter((entry) => entry !== model));
  };
  // 全选 and 反选 act on the rows the filter left, exactly as 全选当前可见模型
  // says: ticking ninety models nobody can see is not what the button promises.
  const selectAllVisible = (): void => setSelected((current) => [...current, ...visible.filter((model) => !current.includes(model))]);
  const invertVisible = (): void => setSelected((current) => {
    const hidden = current.filter((model) => !visible.includes(model));
    return [...hidden, ...visible.filter((model) => !current.includes(model))];
  });
  const count = (template: string, values: Record<string, string>): string => Object.entries(values).reduce((text, [name, value]) => text.split(`{${name}}`).join(value), template);
  const summary = (needle === ""
    ? count(labels.count, { count: String(request.models.length) })
    : count(labels.countFiltered, { visible: String(visible.length), total: String(request.models.length) }))
    + (selected.length > 0 ? `  |  ${count(labels.countSelected, { count: String(selected.length) })}` : "");
  return <DialogFrame
    title={request.title}
    cardStyle={styles.pickerCard}
    footer={<>
      {/* The provider and key are stated once, in the head: the footer stated
          them again in a second notation two lines below. */}
      <View style={styles.footerSpacer} />
      <NativeButton title={request.cancelLabel} compact onPress={() => settleHostChrome(request, undefined)} />
      {/* Both native hosts disable their Add button until something is
          selected; an enabled one here would hand back an empty list. */}
      <NativeButton title={request.confirmLabel} compact primary disabled={selected.length === 0} onPress={() => settleHostChrome(request, selected)} />
    </>}
  >
    <View style={styles.pickerHead}>
      {/* The card's own title row is the heading: a native chooser states it in
          the window's title bar and again inside the window, and a card with no
          window chrome that repeats it draws the same words twice. */}
      <Text numberOfLines={1} style={styles.pickerSubtitle}>{`${labels.providerLabel}: ${request.providerName}    ${labels.keyLabel}: ${request.keyName}`}</Text>
      <NativeTextField
        value={query}
        autoFocus
        onChangeText={setQuery}
        placeholder={labels.search}
        accessibilityLabel={labels.search}
        style={styles.pickerSearch}
      />
      <View style={styles.pickerControls}>
        <NativeButton title={labels.selectAll} compact toolTip={labels.selectAllToolTip} disabled={visible.length === 0} onPress={selectAllVisible} />
        <NativeButton title={labels.invert} compact toolTip={labels.invertToolTip} disabled={visible.length === 0} onPress={invertVisible} />
        <View style={styles.footerSpacer} />
        <Text numberOfLines={1} style={styles.pickerCount}>{summary}</Text>
      </View>
    </View>
    <ScrollView style={[styles.cardBody, styles.pickerList]} contentContainerStyle={styles.pickerBody}>
      {visible.length === 0
        ? <Text style={styles.pickerEmpty}>{request.models.length === 0 ? labels.empty : labels.noMatches}</Text>
        : visible.map((model) => <NativeCheckbox
          key={model}
          label={model}
          value={selected.includes(model)}
          onValueChange={(on) => toggle(model, on)}
        />)}
    </ScrollView>
  </DialogFrame>;
}

function MenuSurface({ request }: { request: Extract<HostChromeRequest, { kind: "menu" }> }): React.JSX.Element {
  const width = typeof window === "undefined" ? MENU_MIN_WIDTH : Math.max(MENU_MIN_WIDTH, window.innerWidth - 16);
  const left = Math.max(8, Math.min(request.anchor.x, width - MENU_MIN_WIDTH - 8));
  return <View style={styles.menuScrim} onStartShouldSetResponder={() => true} onResponderRelease={() => settleHostChrome(request, undefined)}>
    <View style={[styles.menuCard, { left, top: request.anchor.y + request.anchor.height + 2 }]} {...({ role: "menu", "aria-label": request.title } as object)}>
      {request.items.map((item, index) => <NativeButton
        key={`${item.title}:${index}`}
        title={`${item.checked ? "✓ " : ""}${item.title}`}
        compact
        link
        disabled={item.enabled === false}
        onPress={() => settleHostChrome(request, index)}
        style={styles.menuRow}
      />)}
    </View>
  </View>;
}

const styles = StyleSheet.create({
  scrim: {
    // React Native has no `fixed`: an overlay pinned to its parent is
    // `absolute`, and the parent is already the window-sized layer.
    position: "absolute",
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(0,0,0,0.24)",
    padding: 24,
  },
  card: {
    width: 420,
    maxWidth: "100%",
    maxHeight: "90%",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: COLOR.edge,
    backgroundColor: COLOR.surface,
    // The card is the same white as the panes it covers, so the stroke and the
    // shadow together are what say the card is a surface in front, not another
    // pane.
    shadowColor: "#000000",
    shadowOpacity: 0.34,
    shadowRadius: 22,
    shadowOffset: { width: 0, height: 12 },
    overflow: "hidden",
  },
  cardTitle: {
    paddingHorizontal: 16,
    paddingTop: 14,
    paddingBottom: 8,
    color: COLOR.text,
    fontSize: UI_FONT_SIZE,
    fontWeight: "600",
  },
  cardBody: { maxHeight: 320 },
  // The chooser's list is the surface: 480 pt is the height the AppKit panel
  // gives it, and a heading, a search field and two buttons sit above it.
  pickerList: { maxHeight: 480 },
  cardBodyContent: { paddingHorizontal: 16, paddingBottom: 12, gap: 8 },
  bodyText: { color: COLOR.text, fontSize: UI_FONT_SIZE, lineHeight: 19 },
  footer: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 12,
    paddingTop: 8,
    paddingBottom: 12,
    borderTopWidth: 1,
    borderTopColor: COLOR.rule,
  },
  footerSpacer: { flex: 1 },
  footerStatus: { flex: 1, minWidth: 0, color: COLOR.secondary, fontSize: UI_TIP_FONT_SIZE },
  promptField: { minHeight: 26 },
  // A chooser holds names, not a sentence: the native panel is 620 pt wide
  // because a model id is read in full.  Its head states the provider and key
  // it is choosing for, and the count sits on the controls' baseline at the
  // trailing edge, as it does there.
  pickerCard: { width: 620 },
  pickerHead: { paddingHorizontal: 16, paddingTop: 2, paddingBottom: 8, gap: 6 },
  pickerSubtitle: { color: COLOR.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 },
  pickerSearch: { minHeight: 26 },
  pickerControls: { flexDirection: "row", alignItems: "center", gap: 8 },
  pickerCount: { color: COLOR.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16, flexShrink: 0 },
  pickerEmpty: { color: COLOR.secondary, fontSize: UI_FONT_SIZE, lineHeight: 19, paddingVertical: 6 },
  pickerBody: { paddingHorizontal: 16, paddingBottom: 10, gap: 2 },
  viewerCard: {
    width: 920,
    maxWidth: "100%",
    height: 640,
    maxHeight: "100%",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: COLOR.edge,
    backgroundColor: COLOR.surface,
    overflow: "hidden",
  },
  viewerHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderBottomWidth: 1,
    borderBottomColor: COLOR.rule,
    backgroundColor: COLOR.header,
  },
  viewerTitle: { flex: 1, minWidth: 0, color: COLOR.text, fontSize: UI_FONT_SIZE, fontWeight: "600" },
  viewerBody: { flex: 1, minHeight: 0, backgroundColor: COLOR.surface },
  viewerBodyContent: { paddingHorizontal: 16, paddingVertical: 14 },
  viewerText: {
    color: COLOR.text,
    fontSize: UI_TIP_FONT_SIZE,
    lineHeight: 18,
    fontFamily: Platform.OS === "web" ? "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" : "Menlo",
  },
  menuScrim: { position: "absolute", left: 0, right: 0, top: 0, bottom: 0 },
  menuCard: {
    position: "absolute",
    minWidth: MENU_MIN_WIDTH,
    paddingVertical: 4,
    paddingHorizontal: 4,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: COLOR.stroke,
    backgroundColor: COLOR.surface,
    shadowColor: "#000000",
    shadowOpacity: 0.18,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 4 },
  },
  menuRow: { minHeight: 24, justifyContent: "flex-start" },
});
