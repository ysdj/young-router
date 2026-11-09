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
import { NativeBusySpinner, NativeButton, NativeCheckbox, NativeTextField, WINDOWS_CONTROL } from "./NativeControls";
import { UI_FONT_SIZE, UI_TIP_FONT_SIZE } from "./typography";
import { pushPresentation, trapWithin } from "../platform/presentationStack";
import {
  dismissalFor,
  hostChromeSnapshot,
  settleHostChrome,
  subscribeHostChrome,
  topHostChrome,
  type HostChromeRequest,
  type MirrorSurface,
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

/**
 * The card of the dialog currently drawn.
 *
 * `trapWithin` needs the card element itself, not the scrim around it, and the
 * surfaces own their own JSX (`DialogFrame`, and the viewer's own frame), so
 * the element arrives here through a callback ref rather than a prop threaded
 * through every surface.  Only one surface is drawn at a time, so one slot is
 * enough.  The assignment ignores React's `null` detach: the effect below
 * re-reads the slot for every request, so a detached card is never the one a
 * new trap is installed for.
 */
let currentCard: HTMLElement | null = null;

function rememberCard(node: unknown): void {
  // Only the page mounts this component and only its refs are elements; a
  // host instance from a native bundle has no `HTMLElement` to test against.
  if (Platform.OS === "web" && node instanceof HTMLElement) currentCard = node;
}

const FOCUSABLE = "button, input, select, textarea, [tabindex]:not([tabindex='-1'])";

/**
 * Put the caret on the card's first control when the dialog opens.
 *
 * Without this the opener behind the scrim kept focus, so Return re-fired the
 * button that asked the question while the question was still up.  A surface
 * that already focused a field (the prompt, the picker's search) keeps it: its
 * `autoFocus` runs first, and moving focus away would undo it.
 */
function focusFirst(card: HTMLElement): void {
  if (card.contains(document.activeElement)) return;
  const control = Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE)).find((element) => !element.hasAttribute("disabled") && element.tabIndex !== -1);
  (control ?? card).focus();
}

export function HostChromeHost(): React.JSX.Element | null {
  const requests = React.useSyncExternalStore(subscribeHostChrome, hostChromeSnapshot, hostChromeSnapshot);
  // Only the topmost surface is drawn: two answers are never asked at once, and
  // a menu over a dialog must not compete with that dialog's keys.
  const request = requests[requests.length - 1];

  React.useEffect(() => {
    if (request === undefined) return undefined;
    // A menu is anchored to its opener and is not modal: locking the page
    // behind it would dim and disable the very control it belongs to.  Every
    // other surface is a dialog that owns the page while it is up.
    const modal = request.kind !== "menu";
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = modal ? document.getElementById("root") : null;
    if (root) root.inert = true;
    const card = modal ? currentCard : null;
    if (card) focusFirst(card);
    // The same stack the child layers use: Escape answering here is Escape
    // answering the surface on top of whatever is behind it.
    const release = pushPresentation({
      dismiss: () => settleHostChrome(request, dismissalFor(request)),
      ...(card ? { trap: trapWithin(card) } : {}),
    });
    return () => {
      release();
      if (root) root.inert = false;
      // Back to what asked the question, so the dialog closes without dropping
      // the person at the top of the page.  A surface that was replaced by
      // another one during this render leaves its own effect in charge.
      if (opener?.isConnected) opener.focus();
    };
  }, [request]);

  if (request === undefined) return null;
  switch (request.kind) {
    case "confirm": return <DecisionSurface request={request} />;
    case "prompt": return <PromptSurface request={request} />;
    case "viewer": return <ViewerSurface request={request} />;
    case "picker": return <PickerSurface request={request} />;
    case "relaySignIn": return <RelaySignInSurface request={request} />;
    case "relayBrowser": return <RelayBrowserSurface request={request} />;
    case "notice": return <NoticeSurface request={request} />;
    case "menu": return <MenuSurface request={request} />;
  }
}

/** The frame every dialog shares: scrim, card, title, body, footer. */
function DialogFrame({ title, children, footer, cardStyle }: { title: string; children: React.ReactNode; footer: React.ReactNode; cardStyle?: StyleProp<ViewStyle> }): React.JSX.Element {
  return <View style={styles.scrim} {...({ role: "dialog", "aria-modal": true, "aria-label": title } as object)}>
    {/* `tabIndex` -1: a card with no focusable control of its own is still the
        dialog, and focusing it beats leaving the caret on the page behind. */}
    <View ref={rememberCard} tabIndex={-1} style={[styles.card, cardStyle]}>
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

/**
 * The page's answer to the native hosts' sign-in window.
 *
 * A page cannot present the station's own login page: that document belongs to
 * another origin, so its cookies are unreadable here and an iframe of it would
 * still be another site's session.  What the page can do is collect the
 * station account's credentials and let Core post the same form the station's
 * page would have posted — the session then arrives through the one verified
 * login boundary every host already uses.  The form stays up on a refusal: the
 * station's reason is drawn beside fields that still hold what was typed.
 *
 * The password never enters React state.  It lives in a ref filled by the
 * field's own change event, the same way the settings secret field keeps Core's
 * plaintext out of the render path; only "something was typed" is state, so the
 * submit button can enable.
 */
function RelaySignInSurface({ request }: { request: Extract<HostChromeRequest, { kind: "relaySignIn" }> }): React.JSX.Element {
  const labels = request.labels;
  const [username, setUsername] = React.useState(request.username);
  // A re-sign-in opens on the account's own answer: an existing 记住密码 that
  // silently flipped off would clear a saved password nobody asked to clear.
  const [remember, setRemember] = React.useState(request.rememberPassword);
  const [hasPassword, setHasPassword] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState("");
  const password = React.useRef("");
  const ready = username.trim() !== "" && hasPassword && !busy;
  const submit = (): void => {
    if (!ready) return;
    setBusy(true);
    setFailure("");
    void request.submit({ username: username.trim(), password: password.current, rememberPassword: remember }).then((outcome) => {
      if (outcome.ok) {
        settleHostChrome(request, outcome.value);
        return;
      }
      setBusy(false);
      setFailure(outcome.message);
    }).catch(() => {
      // The submit is the host's own exchange and states its refusals itself;
      // a throw here is a broken bridge, and a form left busy forever would be
      // worse than one that lets the person try again.
      setBusy(false);
    });
  };
  return <DialogFrame
    title={request.title}
    footer={<>
      <View style={styles.footerSpacer} />
      <NativeButton title={labels.cancel} compact disabled={busy} onPress={() => settleHostChrome(request, undefined)} />
      <NativeButton title={labels.submit} compact primary busy={busy} disabled={!ready} onPress={submit} />
    </>}
  >
    <ScrollView style={styles.cardBody} contentContainerStyle={styles.cardBodyContent}>
      <Text style={styles.bodyText}>{request.hint}</Text>
      <View style={styles.signInField}>
        <Text style={styles.signInLabel}>{labels.origin}</Text>
        <Text selectable numberOfLines={2} style={styles.signInOrigin}>{request.origin}</Text>
      </View>
      <View style={styles.signInField}>
        <Text style={styles.signInLabel}>{labels.username}</Text>
        <NativeTextField
          value={username}
          // A row that already knows the account starts at the password; an
          // empty one starts at the account. Exactly one field owns the focus.
          autoFocus={request.username.trim() === ""}
          editable={!busy}
          autoCapitalize="none"
          autoCorrect={false}
          onChangeText={(next) => { setUsername(next); setFailure(""); }}
          onSubmitEditing={submit}
          accessibilityLabel={labels.username}
          style={styles.signInInput}
        />
      </View>
      <View style={styles.signInField}>
        <Text style={styles.signInLabel}>{labels.password}</Text>
        <NativeTextField
          secureTextEntry
          autoFocus={request.username.trim() !== ""}
          editable={!busy}
          autoCapitalize="none"
          autoCorrect={false}
          onChangeText={(next) => { password.current = next; setHasPassword(next !== ""); setFailure(""); }}
          onSubmitEditing={submit}
          accessibilityLabel={labels.password}
          style={styles.signInInput}
        />
      </View>
      <NativeCheckbox label={labels.remember} value={remember} disabled={busy} onValueChange={setRemember} />
      <Text style={styles.signInHint}>{labels.rememberHint}</Text>
      {failure ? <Text accessibilityLiveRegion="polite" style={styles.signInFailure}>{failure}</Text> : null}
    </ScrollView>
  </DialogFrame>;
}

/**
 * Waiting for the browser window Core opened at the station.
 *
 * The window is the station's own page and the person is using it, not this
 * document; all this surface does is state that the sign-in is happening, name
 * the browser it happens in, and offer 取消.  It deliberately has no fields: a
 * captcha, a code, or a redirect finished in the real window is what this host
 * cannot draw, and drawing an empty box where a login page should be is the
 * failure it replaces.
 */
function RelayBrowserSurface({ request }: { request: Extract<HostChromeRequest, { kind: "relayBrowser" }> }): React.JSX.Element {
  return <DialogFrame
    title={request.title}
    footer={<>
      <View style={styles.footerSpacer} />
      {request.mirror ? null : <NativeButton title={request.showLabel} compact onPress={request.show} />}
      <NativeButton title={request.cancelLabel} compact onPress={() => settleHostChrome(request, undefined)} />
    </>}
  >
    {request.mirror
      ? <MirrorScreen mirror={request.mirror} title={request.title} />
      : <View style={styles.cardBodyContent}>
          <View style={styles.browserWaitRow}>
            <NativeBusySpinner label={request.title} />
            <Text style={styles.bodyText}>{request.body}</Text>
          </View>
          <Text style={styles.signInHint}>{request.browser}</Text>
        </View>}
  </DialogFrame>;
}

/**
 * The station's own page, as the frames Core sends.
 *
 * Every way a station signs a person in — a password form, a code, a captcha,
 * an OAuth redirect — happens on that real page, so this draws what it draws
 * and forwards what the person does: a click at the picture's own coordinates,
 * a key, a wheel.  The picture is not an `<img>` to be saved or dragged; it is
 * a screen, so the click that focuses it is the click that reaches the page.
 */
/**
 * Where in the station's own 560x820 display a click on this picture landed.
 *
 * Two things had to be right and neither was:
 *
 * - The picture is drawn with `object-fit: contain`, so it is letterboxed: a
 *   560x820 frame in a 418x520 box is 355 px wide with 31 px of black on each
 *   side.  Dividing by the box put every click off by that margin.
 * - React delegates events to its root, so `event.nativeEvent.currentTarget`
 *   is the *root container*, not the picture: the box measured was the whole
 *   page and every click mapped outside the display, where Core refuses it.
 *   Measured live as "nothing on the picture can be clicked".
 *
 * So this takes the picture's own rect and the pointer's client coordinates,
 * which mean the same thing no matter who is listening.
 */
export function mirrorPoint(
  mirror: { width: number; height: number },
  rect: { left: number; top: number; width: number; height: number },
  clientX: number,
  clientY: number,
): { x: number; y: number } | null {
  const boxWidth = Math.max(1, rect.width);
  const boxHeight = Math.max(1, rect.height);
  const scale = Math.min(boxWidth / mirror.width, boxHeight / mirror.height);
  if (!Number.isFinite(scale) || scale <= 0) return null;
  const insetX = (boxWidth - mirror.width * scale) / 2;
  const insetY = (boxHeight - mirror.height * scale) / 2;
  const x = Math.round((clientX - rect.left - insetX) / scale);
  const y = Math.round((clientY - rect.top - insetY) / scale);
  if (x < 0 || y < 0 || x >= mirror.width || y >= mirror.height) return null;
  return { x, y };
}

function MirrorScreen({ mirror, title }: { mirror: MirrorSurface; title: string }): React.JSX.Element {
  const frame = React.useSyncExternalStore(mirror.subscribe, mirror.frame, mirror.frame);
  // The station's display is 560x820, and the frame keeps that shape instead
  // of taking the card's width: at 100% of a wide card the picture grew past
  // the window and its top and bottom were clipped away -- the wizard behind it
  // showed through where the picture should have been.  A fixed height with the
  // frame's own aspect ratio makes it 355 pt wide however wide the window is,
  // and `maxHeight` keeps it inside a short one.
  return <View style={[styles.browserFrame, { aspectRatio: mirror.width / mirror.height, width: undefined, alignSelf: "center" }]}>
    {frame
      ? React.createElement("img", {
          // The picture carries no words of its own; the dialog title names it.
          alt: title,
          src: frame,
          draggable: false,
          tabIndex: 0,
          onDragStart: (event: { preventDefault: () => void }) => event.preventDefault(),
          onClick: (event: { currentTarget?: unknown; target?: unknown; nativeEvent?: { clientX?: number; clientY?: number } }) => {
            const node = (event.currentTarget ?? event.target) as { getBoundingClientRect?: () => DOMRect } | null;
            const native = event.nativeEvent;
            if (!node || typeof node.getBoundingClientRect !== "function" || !native) return;
            if (typeof native.clientX !== "number" || typeof native.clientY !== "number") return;
            const point = mirrorPoint(mirror, node.getBoundingClientRect(), native.clientX, native.clientY);
            // A click in the letterbox is not a click on the station.
            if (point) mirror.send({ type: "click", ...point });
          },
          onKeyDown: (event: { key: string; ctrlKey: boolean; metaKey: boolean; altKey: boolean; preventDefault: () => void }) => {
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            if (event.key.length === 1) {
              mirror.send({ type: "text", text: event.key });
              return;
            }
            // Enter, Tab, Backspace and the arrows are the keys a form moves
            // with; anything else is left to the browser's own shortcut.
            if (MIRROR_KEYS.has(event.key)) {
              event.preventDefault();
              mirror.send({ type: "key", key: event.key });
            }
          },
          onWheel: (event: { deltaY: number; preventDefault: () => void }) => {
            event.preventDefault();
            mirror.send({ type: "wheel", deltaY: Math.round(event.deltaY) });
          },
          style: { width: "100%", height: "100%", display: "block", objectFit: "contain", cursor: "default", outline: "none" },
        })
      : <View style={styles.browserWaitRow}>
          <NativeBusySpinner label={title} />
        </View>}
  </View>;
}

/** The keys this pane forwards; everything else stays the browser's. */
const MIRROR_KEYS = new Set(["Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"]);

/** One statement and its one button; see `notice` in `platform/hostChrome`. */
function NoticeSurface({ request }: { request: Extract<HostChromeRequest, { kind: "notice" }> }): React.JSX.Element {
  return <DialogFrame
    title={request.title}
    footer={<>
      <View style={styles.footerSpacer} />
      <NativeButton title={request.closeLabel} compact primary onPress={() => settleHostChrome(request, undefined)} />
    </>}
  >
    <ScrollView style={styles.cardBody} contentContainerStyle={styles.cardBodyContent}>
      <Text style={styles.bodyText}>{request.message}</Text>
    </ScrollView>
  </DialogFrame>;
}

function ViewerSurface({ request }: { request: Extract<HostChromeRequest, { kind: "viewer" }> }): React.JSX.Element {
  return <View style={styles.scrim} {...({ role: "dialog", "aria-modal": true, "aria-label": request.title } as object)}>
    {/* The viewer states its own frame instead of using `DialogFrame`, so it
        registers that frame with the same trap the other dialogs use. */}
    <View ref={rememberCard} tabIndex={-1} style={styles.viewerCard}>
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
  // The card's height depends on how many rows the caller sent, so it is
  // measured rather than guessed.  Before this, only the left edge was clamped:
  // a menu asked for by a control near the bottom of the pane (the model list's
  // 获取 key rows, the log toolbar) opened downwards past the viewport and the
  // last rows could not be reached at all.  A menu that does not fit below
  // flips above its anchor; one that fits in neither direction is capped and
  // scrolls, so every row stays reachable.
  const [cardHeight, setCardHeight] = React.useState(0);
  const viewportHeight = typeof window === "undefined" ? 0 : window.innerHeight;
  const below = request.anchor.y + request.anchor.height + 2;
  const above = Math.max(8, request.anchor.y - cardHeight - 2);
  const opensUpward = viewportHeight > 0 && cardHeight > 0 && below + cardHeight > viewportHeight - 8 && above > 8;
  const top = opensUpward ? above : below;
  const maxHeight = viewportHeight > 0 ? Math.max(120, viewportHeight - top - 8) : undefined;
  return <View style={styles.menuScrim} onStartShouldSetResponder={() => true} onResponderRelease={() => settleHostChrome(request, undefined)}>
    <View
      style={[styles.menuCard, { left, top, maxHeight }]}
      onLayout={({ nativeEvent }) => { const measured = Math.round(nativeEvent.layout.height); if (measured > 0 && measured !== cardHeight) setCardHeight(measured); }}
      {...({ role: "menu", "aria-label": request.title } as object)}
    >
      <ScrollView style={styles.menuCardScroll} contentContainerStyle={styles.menuCardContent}>
        {request.items.map((item, index) => <NativeButton
          key={`${item.title}:${index}`}
          title={`${item.checked ? "✓ " : ""}${item.title}`}
          compact
          link
          disabled={item.enabled === false}
          onPress={() => settleHostChrome(request, index)}
          style={styles.menuRow}
        />)}
      </ScrollView>
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
  // The sign-in form is a field stack, not a sentence: the label owns the row
  // above its own control, as every settings field does, and the station is a
  // value (a selectable address), not a second title.
  signInField: { gap: 3 },
  signInLabel: { color: COLOR.secondary, fontSize: UI_TIP_FONT_SIZE },
  signInOrigin: { color: COLOR.text, fontSize: UI_TIP_FONT_SIZE, fontFamily: Platform.OS === "web" ? "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" : "Menlo" },
  signInInput: { minHeight: 26 },
  signInHint: { color: COLOR.secondary, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 },
  signInFailure: { color: WINDOWS_CONTROL.danger, fontSize: UI_TIP_FONT_SIZE, lineHeight: 16 },
  browserWaitRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  // The streamed screen is a 560x820 station page.  The box takes the pane's
  // width and keeps that page's own aspect, so a click lands where the person
  // aimed: the picture is scaled, never re-laid-out.
  browserFrame: { height: 520, maxHeight: "70vh" as never, overflow: "hidden", backgroundColor: "#000" },
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
    // The rows live in a ScrollView whose content carries the padding, so the
    // card itself must not add any: padding on a clipped box shrinks the
    // scrollable area and its first and last rows never reach the edge.
    paddingVertical: 0,
    paddingHorizontal: 0,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: COLOR.stroke,
    backgroundColor: COLOR.surface,
    shadowColor: "#000000",
    shadowOpacity: 0.18,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 4 },
  },
  menuCardScroll: { minWidth: 0, maxWidth: 520 },
  menuCardContent: { paddingVertical: 4, paddingHorizontal: 4 },
  menuRow: { minHeight: 24, justifyContent: "flex-start" },
});
