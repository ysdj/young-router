/**
 * The host's own surfaces — confirm/prompt dialogs, the record viewer, the
 * model picker, the anchored menu — as requests in a store rather than DOM.
 *
 * The native hosts express these as AppKit/WinUI surfaces. The browser host
 * used to build them with `document.createElement`, which meant a second,
 * hand-written stylesheet beside the app's own: the same question looked like a
 * different app, the read-only viewer's script never ran under the page's own
 * CSP, and each surface installed its own Escape listener so one Escape closed
 * every open layer at once.
 *
 * They are now drawn by `ui/HostChrome.tsx` with the same controls the panes
 * use, and Escape is answered once, by the topmost surface, through
 * `presentationStack`.
 *
 * This module imports no renderer: nothing under `packages/shared` may import
 * `react-dom`, or the two native bundles would try to resolve it.
 */

export type HostChromeMenuItem = { title: string; enabled?: boolean; checked?: boolean };

export type HostChromeAnchor = { x: number; y: number; width: number; height: number };

/** What the page's sign-in form hands back when the station accepted it. */
export type RelaySignInAnswer = { revision: number; loginStatus: "signed_in"; username: string };

/**
 * The answer one submit attempt gives the surface.
 *
 * A refusal stays *inside* the form: the surface draws the reason beside the
 * fields and keeps what the person typed, because the one thing a failed
 * station sign-in must not do is make someone retype their account and
 * password.  Only an accepted sign-in settles the request.
 */
export type RelaySignInOutcome =
  | { ok: true; value: RelaySignInAnswer }
  | { ok: false; message: string };

/** One sign-in's streamed screen: the latest frame and the input path back. */
export type MirrorSurface = {
  width: number;
  height: number;
  /** The newest frame as a data URL, or "" until the first one arrives. */
  frame: () => string;
  subscribe: (listener: () => void) => () => void;
  /** Forward one click, key or wheel to the browser Core is running. */
  send: (event: Record<string, unknown>) => void;
};

type Base = { id: number };

export type HostChromeRequest =
  | (Base & { kind: "confirm"; title: string; message: string; confirmLabel: string; cancelLabel: string; destructive: boolean; resolve: (accepted: boolean) => void })
  | (Base & { kind: "prompt"; title: string; value: string; confirmLabel: string; cancelLabel: string; resolve: (value: string | null) => void })
  | (Base & { kind: "viewer"; title: string; text: string; closeLabel: string; resolve: () => void })
  | (Base & {
      kind: "picker";
      title: string;
      models: string[];
      providerName: string;
      keyName: string;
      confirmLabel: string;
      cancelLabel: string;
      /**
       * The chooser's own words, because it is a surface and not a message: it
       * has a heading, a provider/key line, a search field, 全选 and 反选, a
       * count that follows the filter, and an empty result.  The native links
       * carry these in their localization table; this host has no table, so the
       * caller (which holds the same strings) states them.
       */
      labels: {
        providerLabel: string;
        keyLabel: string;
        search: string;
        selectAll: string;
        selectAllToolTip: string;
        invert: string;
        invertToolTip: string;
        count: string;
        countFiltered: string;
        countSelected: string;
        empty: string;
        noMatches: string;
      };
      resolve: (models: string[] | undefined) => void;
    })
  | (Base & {
      kind: "relaySignIn";
      title: string;
      origin: string;
      /** The account name the row already knows, pre-filled. */
      username: string;
      /** The account's own remember-password preference, as the box opens. */
      rememberPassword: boolean;
      hint: string;
      labels: {
        origin: string;
        username: string;
        password: string;
        remember: string;
        rememberHint: string;
        submit: string;
        cancel: string;
      };
      submit: (credentials: { username: string; password: string; rememberPassword: boolean }) => Promise<RelaySignInOutcome>;
      resolve: (answer: RelaySignInAnswer | undefined) => void;
    })
  | (Base & {
      /**
       * Waiting for a real browser window Core opened at the station.
       *
       * The window is the station's own page, so a captcha, a code, or an
       * OAuth redirect finishes there.  This surface states what is being
       * waited for and offers the one action a wait needs: cancel.
       */
      kind: "relayBrowser";
      title: string;
      body: string;
      browser: string;
      cancelLabel: string;
      /** Raise the window again, for one that opened behind everything else. */
      showLabel: string;
      show: () => void;
      /**
       * The station's own page, drawn from frames Core streams.
       *
       * A server has no screen to put a window on, and a page opened through a
       * reverse proxy is not on the machine running Core.  Both run the browser
       * headless and send its frames here: the person clicks and types into
       * this picture, and Core turns that back into input on the real page.
       */
      mirror?: MirrorSurface;
      resolve: () => void;
    })
  | (Base & {
      /**
       * One statement, one button.
       *
       * A page has no menu bar and no system alert, so "this host cannot do
       * that, here is what to do instead" needs a surface of its own: a
       * question (`confirm`) would ask for a decision nobody is making, and a
       * viewer would draw it as a record.  Used where a capability is absent
       * rather than refused — the editor's Paste item on a page served over
       * plain HTTP, where the browser withholds the clipboard read API.
       */
      kind: "notice";
      title: string;
      message: string;
      closeLabel: string;
      resolve: () => void;
    })
  | (Base & { kind: "menu"; title: string; items: HostChromeMenuItem[]; anchor: HostChromeAnchor; resolve: (index: number | undefined) => void });

/** A request before the store assigns its id and settle callback. */
type RequestBody = Omit<Extract<HostChromeRequest, { kind: "confirm" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "prompt" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "viewer" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "picker" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "relaySignIn" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "relayBrowser" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "notice" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "menu" }>, "id" | "resolve">;

let requests: HostChromeRequest[] = [];
let serial = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function hostChromeSnapshot(): HostChromeRequest[] {
  return requests;
}

export function subscribeHostChrome(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** The surface a key press belongs to: the newest request still open. */
export function topHostChrome(): HostChromeRequest | undefined {
  return requests[requests.length - 1];
}

/** The answer a dismissing action gives, per surface kind. */
export function dismissalFor(request: HostChromeRequest): unknown {
  switch (request.kind) {
    case "confirm": return false;
    case "prompt": return null;
    case "viewer": return undefined;
    case "picker": return undefined;
    case "relaySignIn": return undefined;
    case "relayBrowser": return undefined;
    case "notice": return undefined;
    case "menu": return undefined;
  }
}

/**
 * Ask the page for one answer, drawn by `HostChrome`.
 *
 * The promise settles exactly once: by the surface's own answer, by Return or
 * Escape, or by `dismissHostChrome` when the layer underneath it went away.
 * The request leaves the stack before its answer is delivered, so a caller that
 * opens another surface from the answer starts from a clean stack.
 */
export function requestHostChrome<T>(body: RequestBody): Promise<T> {
  return new Promise<T>((resolve) => {
    const id = ++serial;
    const request = {
      ...body,
      id,
      resolve: (answer: unknown) => {
        if (!requests.some((entry) => entry.id === id)) return;
        requests = requests.filter((entry) => entry.id !== id);
        emit();
        resolve(answer as T);
      },
    } as HostChromeRequest;
    requests = [...requests, request];
    emit();
  });
}

/** Answer one open request. Answering the same request twice is a no-op. */
export function settleHostChrome(request: HostChromeRequest, answer: unknown): void {
  (request.resolve as (value: unknown) => void)(answer);
}

/** Close every open request with its dismissing answer, topmost first. */
export function dismissHostChrome(): void {
  for (const request of [...requests].reverse()) settleHostChrome(request, dismissalFor(request));
}

/**
 * End the open surface of one kind, if there is one.
 *
 * The relay sign-in is the one host surface a caller has to be able to end from
 * outside: 取消登录 in the wizard, a second press that starts a new sign-in, and
 * a host that is replaced while the form is up all mean the same thing — the
 * question is no longer being asked.  Closing every surface here would take the
 * decision dialog behind it as well, which is the failure the one stack exists
 * to avoid.
 */
export function cancelHostChrome(kind: HostChromeRequest["kind"]): void {
  for (const request of [...requests].reverse()) {
    if (request.kind !== kind) continue;
    settleHostChrome(request, dismissalFor(request));
    return;
  }
}
