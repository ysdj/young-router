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
import { Platform, ScrollView, StyleSheet, Text, View } from "react-native";
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
function DialogFrame({ title, children, footer }: { title: string; children: React.ReactNode; footer: React.ReactNode }): React.JSX.Element {
  return <View style={styles.scrim} {...({ role: "dialog", "aria-modal": true, "aria-label": title } as object)}>
    <View style={styles.card}>
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
  return <DialogFrame
    title={request.title}
    footer={<>
      <View style={styles.footerSpacer} />
      <NativeButton title={request.cancelLabel} compact onPress={() => settleHostChrome(request, null)} />
      <NativeButton title={request.confirmLabel} compact primary onPress={submit} />
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

function PickerSurface({ request }: { request: Extract<HostChromeRequest, { kind: "picker" }> }): React.JSX.Element {
  const [selected, setSelected] = React.useState<string[]>(request.models);
  const toggle = (model: string, on: boolean): void => {
    setSelected((current) => on
      ? (current.includes(model) ? current : [...current, model])
      : current.filter((entry) => entry !== model));
  };
  return <DialogFrame
    title={request.title}
    footer={<>
      <Text numberOfLines={1} style={styles.footerStatus}>{`${request.providerName} · ${request.keyName}`}</Text>
      <NativeButton title={request.cancelLabel} compact onPress={() => settleHostChrome(request, undefined)} />
      <NativeButton title={request.confirmLabel} compact primary onPress={() => settleHostChrome(request, selected)} />
    </>}
  >
    <ScrollView style={styles.cardBody} contentContainerStyle={styles.pickerBody}>
      {request.models.map((model) => <NativeCheckbox
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
    borderColor: COLOR.stroke,
    backgroundColor: COLOR.surface,
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
  pickerBody: { paddingHorizontal: 16, paddingBottom: 10, gap: 2 },
  viewerCard: {
    width: 920,
    maxWidth: "100%",
    height: 640,
    maxHeight: "100%",
    borderRadius: 8,
    borderWidth: 1,
    borderColor: COLOR.stroke,
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
