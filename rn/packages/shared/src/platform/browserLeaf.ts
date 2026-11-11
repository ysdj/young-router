/**
 * The browser host's `NativeLeafBridge`: the same adapter the AppKit and
 * WinUI leaves implement, expressed in page APIs.
 *
 * It exists so the third host shares one UI rather than one UI being copied.
 * `platformEntry.ts` is the native entry (it reads `NativeModules` and throws
 * when no native host is present); this module is the same wiring with a
 * `fetch` transport and a DOM leaf. Nothing here is imported by a native host,
 * and no native host's file imports it.
 *
 * Two rules this file keeps, both from the shared contract rather than from
 * taste:
 *
 * - A child surface stays in this document. The native hosts present the
 *   provider wizard, the raw editor and 分组管理 as their own movable windows,
 *   because an overlay over a pane is clipped by it and cannot be dragged. A
 *   browser has no windows to present, so the same surfaces are drawn here as
 *   a modal layer with a real title bar: draggable, sized on its own, and
 *   covering the pane it was opened from. That keeps one address, one back
 *   button, and one scroll container — a tab per child surface would put three
 *   entries in browser history for one settings change.
 * - Secret text never enters React state. `readSecret`/`stageSecret` go
 *   straight to Core's capability leases; `copySecret` performs the copy in
 *   this module and returns a boolean, exactly like the native leaves.
 */

import type { AppRoute, LanguagePreference, NativeLocalization, NativeMenuAction, NativeMenuAnchor, RelayGroupManagerLabels, RelayGroupManagerResult, RelayGroupManagerSnapshot, ServiceStatus } from "../types";
import type { NativeIpcBridge, NativeLeafBridge } from "./nativeBridge";
import { isSettingsShellRoute } from "../routes";
import { resolveLanguage } from "../i18n";
import { pushPresentation } from "./presentationStack";
import { cancelHostChrome, requestHostChrome, type MirrorSurface, type RelaySignInAnswer } from "./hostChrome";
import {
  finishRelayGroupApply,
  openRelayGroupSheet,
  takeRelayGroupApply,
  updateRelayGroupSheet,
} from "./relayGroupSheet";

/** One JSON-over-HTTP exchange with the local Core. */
export type BrowserCoreTransport = {
  /** Absolute Core base, e.g. empty when the page is served by Core itself. */
  baseURL: string;
};

/**
 * The page does not send `Authorization` at all.
 *
 * Its credential is the `HttpOnly` cookie Core set, and that cookie is the one
 * Core checks.  The native hosts put the lease in a header because they hold it
 * in process memory; a page cannot read the cookie, so a header it sent would
 * be a *second*, remembered copy — and two credentials that disagree are
 * refused on purpose.  That mismatch is not hypothetical: a reload replaces the
 * cookie while the previous document's calls are still in flight, so the old
 * document's header no longer matched the new cookie, Core answered 401, and
 * the 401 handler reloaded again — a page that never finished loading, under a
 * storm of refusals.  With the cookie as the only credential the question has
 * one answer.
 */

/**
 * Whether this document is the machine owner's own page.
 *
 * The same address rule Core's gate uses: a loopback name is the owner's door,
 * so the page is served without a key and a session is minted for it.  A page
 * reached under any other name is published, which is where the key is typed
 * and therefore the only place 退出登录 has something to end.
 */
/**
 * How long 退出登录 waits for Core to answer before it reloads anyway.
 *
 * Long enough for a busy Core to end the lease, short enough that the person
 * who pressed it sees the page change instead of wondering whether the press
 * landed.
 */
const SIGN_OUT_TIMEOUT_MS = 2500;

function isOwnerPage(): boolean {
  if (typeof window === "undefined") return false;
  const name = (window.location.hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  return name === "127.0.0.1" || name === "::1" || name === "localhost";
}

/**
 * The browser cannot read `X-LiteLLM-Core-Session`, so Core hands it the same
 * lease as an `HttpOnly` cookie. That cookie is the real credential and every
 * request carries it; the remembered value above is only what this document
 * already learned, so a reload does not have to ask a second time.
 */
function coreRequest(transport: BrowserCoreTransport, path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  return fetch(`${transport.baseURL}${path}`, {
    ...init,
    headers,
    // The session cookie is the credential; `include` is what sends it back
    // to the same origin. Core sets it `SameSite=Strict`, so nothing else can.
    credentials: "include",
    cache: "no-store",
  });
}

/**
 * How long one Core exchange may stay unanswered before this page gives up.
 *
 * A `fetch` nobody aborts stays pending for as long as its connection does, and
 * this page hangs its write queue on the answer.  A gateway replaces the proxy
 * on every Apply, and a POST in flight while that happens is answered by
 * nobody: the reverse proxy keeps the socket open, the browser never sees a
 * response, and the promise it is waiting on never settles.  Every later press
 * of 启用 or 跟随全局 then queues behind it and never reaches Core, while the
 * pane still draws 就绪 and the control still looks pressable -- a page where
 * every checkbox silently stopped working, forever, until a reload.
 *
 * Core answers an ordinary edit in milliseconds and runs the post-Apply proxy
 * restart on its own thread (it never makes the commit wait for it), so an
 * exchange still unanswered after this budget is a lost connection rather than
 * a slow commit.  Giving up is what lets the queue move again: the press that
 * timed out is reported in the strip instead of holding every later one.
 */
const CORE_CALL_TIMEOUT_MS = 20_000;

/** A failure with the code the shared UI maps to its own sentence. */
function codedFailure(code: string, message: string = code): Error & { code: string } {
  const failure = new Error(message) as Error & { code?: string };
  failure.code = code;
  return failure as Error & { code: string };
}

/** A Core call that outlived its budget, named so the pane can say why. */
function coreTimeout(): Error & { code: string } {
  return codedFailure("core_timeout", `Young Router's Core did not answer within ${Math.round(CORE_CALL_TIMEOUT_MS / 1000)} seconds`);
}

/** A call that never reached Core at all: this page's own connection is gone. */
function coreUnreachable(): Error & { code: string } {
  return codedFailure("core_unavailable", "Young Router's Core could not be reached from this page");
}

/** Whether a rejection is this module's own deadline firing. */
function isAbort(reason: unknown): boolean {
  return typeof reason === "object" && reason !== null && (reason as { name?: unknown }).name === "AbortError";
}

/**
 * Run one Core exchange with a deadline, in the shape the caller already uses.
 *
 * The signal reaches the request *and* the body read, so a response whose
 * headers arrived and whose bytes never do is covered by the same budget.
 */
function withDeadline<T>(operation: (signal: AbortSignal | undefined) => Promise<T>): Promise<T> {
  if (typeof AbortController !== "function") return operation(undefined);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CORE_CALL_TIMEOUT_MS);
  return operation(controller.signal).finally(() => clearTimeout(timer));
}

async function hello(transport: BrowserCoreTransport, signal?: AbortSignal): Promise<void> {
  // No credential is presented here: the browser sends the session cookie Core
  // set when this page signed in with the web password, and that cookie is what
  // Core answers about.  The lease stays in it — a page may read neither the
  // header nor the cookie — so there is nothing to keep on this side.
  const response = await coreRequest(transport, "/ipc/hello", { method: "POST", body: "{}", signal });
  if (!response.ok) throw new Error("Core IPC authentication failed");
}

/** The bridge plus the two host-route doors only a host process may use. */
export type BrowserCoreBridge = NativeIpcBridge & {
  hostRoute(path: string, body: Record<string, unknown>): Promise<Record<string, unknown>>;
  /**
   * Hand Core one last message on the way out of the page.
   *
   * `beforeunload`/`pagehide` cannot await a fetch, and a request that has not
   * left before the document dies never arrives.  A beacon does: the browser
   * delivers it even while it replaces the page.
   */
  hostBeacon(path: string, body: Record<string, unknown>): boolean;
  request(method: string, params?: Record<string, unknown>): string;
  /**
   * End this page's session: Core drops the lease the cookie names, answers
   * with a cookie the browser must not keep, and the page is reloaded onto its
   * own sign-in gate.
   */
  signOut(): Promise<void>;
};

export function createBrowserIpcBridge(transport: BrowserCoreTransport): BrowserCoreBridge {
  let established = false;
  let pending: Promise<void> | undefined;

  /**
   * Ask Core for this page's session, exactly once.
   *
   * Several callers reach this in the same tick on first load — the snapshot
   * read, the event long poll, and whatever the first screen asks for — and the
   * one shared promise is what keeps them from racing. The credential is the
   * cookie Core set when this page signed in, so nothing is spent by asking:
   * a reload re-enters the same way.
   */
  const establish = (): Promise<void> => {
    if (established) return Promise.resolve();
    if (pending !== undefined) return pending;
    const exchange = withDeadline((signal) => hello(transport, signal)).then(() => {
      // One greeting per document: it queues this document behind Core's
      // session, so every later call carries the cookie Core just answered
      // for.  A greeting per call would be one extra round trip per control.
      established = true;
    }, (reason: unknown) => {
      // The greeting is the first exchange of every later call, so one that
      // hangs would hold them all: it carries the same deadline, and its
      // failure is the answer the caller's own strip states.
      throw isAbort(reason) ? coreTimeout() : reason;
    });
    // Cleared by identity, and only after the assignment below, so two callers
    // that arrive in the same tick both await the exchange instead of one of
    // them starting a second (and being refused).
    pending = exchange;
    void exchange.finally(() => { if (pending === exchange) pending = undefined; });
    return exchange;
  };

  /**
   * One Core call.
   *
   * A 401 is not retried: the cookie either is a session or is not, and asking
   * twice cannot change Core's answer. It is, however, the moment this page has
   * to leave: a Core that restarted (the app replaced it, or it was upgraded)
   * forgot every session, so this document's tab would otherwise keep asking a
   * server that will answer 401 forever — the app frozen on its last snapshot
   * under a stream of failed requests, with no surface saying why. Reloading
   * hands the decision back to Core, which answers the shell with its password
   * form. One reload per document, so a Core that refuses the fresh sign-in
   * cannot turn the page into a reload loop.
   */
  let leavingForSignIn = false;
  const leaveForSignIn = (): void => {
    if (leavingForSignIn) return;
    leavingForSignIn = true;
    window.location.reload();
  };

  const call = async (path: string, body: string, signal?: AbortSignal): Promise<Response> => {
    await establish();
    let response: Response;
    try {
      response = await coreRequest(transport, path, {
        method: "POST",
        body,
        headers: { "Content-Type": "application/json" },
        signal,
      });
    } catch (reason: unknown) {
      // `fetch` rejects for exactly two reasons here: this page's deadline
      // fired, or the request never reached Core at all.  Both are the pane's
      // business, and neither may leave a caller waiting on a promise that can
      // no longer settle.
      throw isAbort(reason) ? coreTimeout() : coreUnreachable();
    }
    if (response.status === 401) leaveForSignIn();
    return response;
  };

  const send = async (request: string): Promise<string> => withDeadline(async (signal) => {
    const response = await call("/ipc", request, signal);
    if (response.status >= 500) throw new Error("The local core is unavailable.");
    // A 400 carries a protocol envelope the shared client renders itself; only
    // a refused credential or an empty body is an error here.
    const text = await response.text();
    if (!text) throw new Error("The local core returned no response.");
    return text;
  }).catch((reason: unknown) => { throw isAbort(reason) ? coreTimeout() : reason; });

  const request = (method: string, params: Record<string, unknown> = {}): string => JSON.stringify({
    protocol_version: 1,
    request_id: `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    method,
    params,
  });

  /** The host-only capability leases, which travel on their own routes. */
  const hostBeacon = (path: string, body: Record<string, unknown>): boolean => {
    try {
      const payload = new Blob([JSON.stringify(body)], { type: "application/json" });
      return navigator.sendBeacon(`${transport.baseURL}${path}`, payload);
    } catch {
      return false;
    }
  };

  const hostRoute = async (path: string, body: Record<string, unknown>): Promise<Record<string, unknown>> => withDeadline(async (signal) => {
    const response = await call(path, JSON.stringify(body), signal);
    const text = await response.text();
    if (!response.ok || !text) {
      // A refused host exchange can carry a reason the caller can act on: the
      // station rejecting a password reads differently from a station that
      // never answered, and the relay sign-in form draws that difference.  The
      // code travels on the error; the message stays the capability wording
      // every other caller already ignores.
      let code = "";
      try {
        const payload = JSON.parse(text) as { error?: { code?: unknown } };
        code = typeof payload?.error?.code === "string" ? payload.error.code : "";
      } catch {
        code = "";
      }
      const failure = new Error("The requested secret field is unavailable") as Error & { code?: string };
      failure.code = code;
      throw failure;
    }
    return JSON.parse(text) as Record<string, unknown>;
  }).catch((reason: unknown) => { throw isAbort(reason) ? coreTimeout() : reason; });

  /**
   * Sign this page out.
   *
   * The cookie is the only credential this host has, so signing out is Core
   * ending the lease that cookie names and answering with one the browser must
   * not keep.  The reload is the second half and is not optional: the shell on
   * screen was drawn from a session that no longer exists, and asking Core for
   * the page again is what draws the gate.
   *
   * It goes through `fetch` rather than `call` because `call` first establishes
   * a session, which is exactly the work signing out must not do.  A failure is
   * not a reason to stay: a Core that cannot answer is not a page that is still
   * signed in.
   *
   * The wait for that answer is bounded.  An `await` with no deadline is a
   * button that does nothing: measured on lv, pressing 退出登录 while Core was
   * rolling the proxy out left the request pending, so the reload behind it
   * never ran and the page sat there still showing the session.  A Core that
   * has not answered in a couple of seconds still has the cookie cleared on the
   * next request it serves, and the gate the reload draws is the answer either
   * way.
   */
  const signOut = async (): Promise<void> => {
    const deadline = typeof AbortController === "function" ? new AbortController() : undefined;
    const timer = deadline ? setTimeout(() => deadline.abort(), SIGN_OUT_TIMEOUT_MS) : undefined;
    try {
      await fetch(`${transport.baseURL}/ipc/web/logout`, {
        method: "POST",
        body: "{}",
        credentials: "include",
        cache: "no-store",
        signal: deadline?.signal,
        headers: { "Content-Type": "application/json" },
      });
    } catch {
      // The reload below is the honest answer either way.
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      window.location.reload();
    }
  };

  const subscribe = (listener: (event: string) => void): (() => void) => {
    let cancelled = false;
    let subscription: string | undefined;
    let controller: AbortController | undefined;

    const poll = async (): Promise<void> => {
      while (!cancelled) {
        try {
          if (subscription === undefined) {
            const response = await call("/ipc", request("subscribe"));
            const payload = await response.json() as { result?: { subscription_id?: string } };
            subscription = payload.result?.subscription_id;
            if (subscription === undefined) {
              await new Promise((resolve) => setTimeout(resolve, 1_000));
              continue;
            }
          }
          await establish();
          controller = new AbortController();
          const response = await fetch(
            `${transport.baseURL}/ipc/events?subscription_id=${encodeURIComponent(subscription)}&timeout=25`,
            {
              credentials: "include",
              cache: "no-store",
              signal: controller.signal,
            },
          );
          if (response.status === 401) {
            // The event stream is the page's first request after a Core
            // restart notices nothing; a refused stream is the same answer a
            // refused call gives, and the page leaves the same way.
            leaveForSignIn();
            return;
          }
          if (!response.ok) {
            subscription = undefined;
            await new Promise((resolve) => setTimeout(resolve, 1_000));
            continue;
          }
          const payload = await response.json() as { event?: unknown };
          // Core answers `{event: null}` when the long poll expires with
          // nothing to report; that is a keep-alive, not a message.
          if (payload.event) listener(JSON.stringify(payload.event));
        } catch {
          if (cancelled) return;
          subscription = undefined;
          await new Promise((resolve) => setTimeout(resolve, 1_500));
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      controller?.abort();
    };
  };

  /**
   * One subscription for this page.
   *
   * The shared IPC client calls `subscribe` once per pane that wants events,
   * and each call would otherwise open its own long poll against Core.  A page
   * has one document and one set of panes, so they share one subscription:
   * opening six log tabs must not create six event streams that all wait on
   * the same idle queue and never answer.
   */
  let unsubscribeEvents: (() => void) | undefined;
  const listeners = new Set<(event: string) => void>();
  const sharedSubscribe = (listener: (event: string) => void): (() => void) => {
    listeners.add(listener);
    if (!unsubscribeEvents) unsubscribeEvents = subscribe((event) => { for (const next of listeners) next(event); });
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0 && unsubscribeEvents) {
        unsubscribeEvents();
        unsubscribeEvents = undefined;
      }
    };
  };

  return { send, subscribe: sharedSubscribe, hostRoute, hostBeacon, request, signOut };
}

/** The service state in the app's words, as the title bar states it. */
function serviceStateWord(strings: NativeLocalization, state: ServiceStatus["state"]): string {
  switch (state) {
    case "starting": return strings.serviceStarting;
    case "running": return strings.serviceRunning;
    case "unhealthy": return strings.serviceUnhealthy;
    case "stopped": return strings.serviceStopped;
    default: return strings.serviceUnknown;
  }
}

/** One `{label}` placeholder in a host string, the shape `translate` uses. */
function fillLabel(template: string, value: string): string {
  return template.split("{label}").join(value);
}

/**
 * Put text on the pasteboard, whichever way this origin allows.
 *
 * `navigator.clipboard` only exists in a secure context, so a page published
 * over plain HTTP from a server has no copy API at all — and a copy button that
 * silently does nothing is how an API key looked "broken" there.  The legacy
 * editing command still works from a user gesture, which is what a press is, so
 * that is the fallback.  It is the same pair the editor page uses for its own
 * copy item, for the same reason.
 */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && typeof navigator.clipboard?.writeText === "function") {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Denied or unavailable: the editing command below is the other way.
  }
  if (typeof document === "undefined") return false;
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "true");
  scratch.setAttribute("aria-hidden", "true");
  scratch.style.position = "fixed";
  scratch.style.top = "-10000px";
  scratch.style.left = "0";
  scratch.style.opacity = "0";
  document.body.appendChild(scratch);
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let copied = false;
  try {
    scratch.select();
    scratch.setSelectionRange(0, text.length);
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    scratch.remove();
    previous?.focus();
  }
  return copied;
}

/** What the form says about the window that could not be shown. */
function browserLoginFallbackHint(strings: NativeLocalization | undefined): string {
  switch (lastBrowserLoginReason) {
    case "launch_failed": return strings?.relayBrowserLaunchFailed ?? "";
    default: return strings?.relayBrowserUnavailable ?? "";
  }
}

/** The code a refused host exchange carried, or "". */
function hostErrorCode(error: unknown): string {
  return typeof error === "object" && error !== null && typeof (error as { code?: unknown }).code === "string"
    ? (error as { code: string }).code
    : "";
}

/**
 * Why a window could not be shown, as Core named it.
 *
 * The form that follows states this, because "why is this page asking for my
 * password when the station has a website" is the question a person actually
 * has: a page published from a server is open in their own browser, and a
 * window on the server's screen is not theirs to see.
 */
let lastBrowserLoginReason = "";

/** How often the page asks Core whether the browser finished the sign-in. */
const BROWSER_LOGIN_POLL_MS = 900;

/** The first pause after a refused frame, and the run of them that ends the stream. */
const MIRROR_RETRY_MS = 400;
const MIRROR_RETRY_LIMIT = 5;

/** One pause a cancel can cut short: the loop below checks `cancelled` after it. */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

/**
 * The streamed screen of one sign-in: poll Core for the newest frame, and send
 * back what the person does to it.
 *
 * A frame is a JPEG in a data URL — one picture, replaced as the station draws
 * — and the poll is a long one: Core holds the request until something changed
 * or a second passes, so an idle login form costs nothing and a page that draws
 * answers immediately.  The loop ends when the surface does: cancel, a finished
 * login, a closed browser and the ten-minute limit all clear `cancelled`.
 */
function startMirrorSurface(
  core: BrowserCoreBridge,
  loginId: string,
  spec: unknown,
  cancelled: () => boolean,
): MirrorSurface | undefined {
  if (typeof spec !== "object" || spec === null) return undefined;
  const size = spec as { width?: unknown; height?: unknown };
  if (typeof size.width !== "number" || typeof size.height !== "number") return undefined;
  const listeners = new Set<() => void>();
  let frame = "";
  let seq = 0;
  const publish = (next: string) => {
    frame = next;
    for (const listener of listeners) listener();
  };
  const pump = async () => {
    // A restart of Core, or a session that lapsed and was renewed, refuses one
    // poll and answers the next: ending the stream on the first refusal left
    // the last picture on screen forever, under a Cancel button that still
    // worked.  The run is counted instead, the pause grows with it, and only a
    // run that outlasts a Core restart ends the stream — and that ending is
    // published as an empty frame, so the surface reads as a wait again rather
    // than as a picture of a page that is no longer there.
    let failures = 0;
    while (!cancelled()) {
      let answer: Record<string, unknown>;
      try {
        answer = await core.hostRoute("/ipc/host/relay/browser/frames", { login_id: loginId, since: seq });
      } catch {
        failures += 1;
        if (failures > MIRROR_RETRY_LIMIT) {
          publish("");
          return;
        }
        await sleep(MIRROR_RETRY_MS * failures);
        continue;
      }
      failures = 0;
      const next = Number(answer.seq ?? seq);
      if (Number.isFinite(next)) seq = next;
      if (typeof answer.frame === "string" && answer.frame) {
        publish(`data:image/jpeg;base64,${answer.frame}`);
      }
    }
  };
  // Started after this call returns, so a caller cannot be reading a binding
  // that its own function has not finished initializing.
  void Promise.resolve().then(pump);
  return {
    width: size.width,
    height: size.height,
    frame: () => frame,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    send: (event) => {
      void core.hostRoute("/ipc/host/relay/browser/input", { login_id: loginId, event }).catch(() => undefined);
    },
  };
}

/**
 * Sign in by letting Core open the station in a real browser.
 *
 * `"unavailable"` is the answer that means "this host has no browser to open,
 * use the credential form instead" — a capability answer, not a failed
 * sign-in.  Every other answer is what the pane already reads from a native
 * sign-in: the account's state, or `undefined` when the person cancelled.
 *
 * The wait is a surface of its own: the person is working in another window,
 * so this document says what is being waited for and offers 取消, instead of
 * leaving a dead button on screen.  Cancel, a closed window, and the ten
 * minutes Core allows all end the same way, and Core is told to close the
 * window in every one of them.
 */
async function browserWindowLogin(
  core: BrowserCoreBridge,
  options: {
    accountId: string;
    type: string;
    label: string;
    origin: string;
    username?: string;
    pendingAccount?: boolean;
    stationId?: string;
    stationName?: string;
    stationType?: string;
    stationOrigin?: string;
  },
  strings: NativeLocalization | undefined,
): Promise<{ revision: number; loginStatus: "signed_in"; username: string } | undefined | "unavailable"> {
  const body: Record<string, unknown> = {
    account_id: options.accountId,
    type: options.type,
    label: options.label,
    origin: options.origin,
    username: options.username ?? "",
    pending_account: options.pendingAccount === true,
  };
  if (options.stationId) body.station_id = options.stationId;
  if (options.stationName) body.station_name = options.stationName;
  if (options.stationType) body.station_type = options.stationType;
  if (options.stationOrigin) body.station_origin = options.stationOrigin;
  let started: Record<string, unknown>;
  try {
    started = await core.hostRoute("/ipc/host/relay/browser/start", body);
  } catch (error) {
    // A refused start is answered, not thrown: `unsupported` below is the one
    // answer that means "this host cannot show you the station", and it falls
    // back to the form.  Anything else is a failed sign-in and stays one.
    return hostErrorCode(error) === "relay_browser_unavailable" ? "unavailable" : undefined;
  }
  if (String(started.status ?? "") === "unsupported") {
    // `reason` names which side is missing, so the form can say it: a page
    // opened on another machine than Core (`remote`), a host with no browser
    // (`no_browser`), no screen (`no_display`), or a launch that never
    // reached its window (`launch_failed`).
    lastBrowserLoginReason = String(started.reason ?? "");
    return "unavailable";
  }
  lastBrowserLoginReason = "";
  const loginId = typeof started.login_id === "string" ? started.login_id : "";
  if (!loginId) return undefined;
  const browserName = typeof started.browser === "string" ? started.browser : "";
  // Declared before the stream, not after it: the stream's loop asks this on its
  // first tick, and a `let` that has not been initialized yet throws rather than
  // reading as `false` — measured on the live page as
  // `ReferenceError: Cannot access 's' before initialization`, with the dialog
  // drawn black and no frame ever requested.
  let cancelled = false;
  // A page that is closed or reloaded mid sign-in cannot await anything, and
  // the browser Core started for it would go on running until the ten-minute
  // limit.  One beacon on the way out ends it now: `beforeunload` is the reload
  // a person asked for, `pagehide` a tab closed or discarded in the background.
  // Measured before this existed: reloading the page left three Chrome
  // processes and three profiles behind on the host.
  const abandon = () => {
    core.hostBeacon("/ipc/host/relay/browser/cancel", { login_id: loginId });
  };
  window.addEventListener("beforeunload", abandon);
  window.addEventListener("pagehide", abandon);
  // A Core with no screen of its own, or a page opened through a proxy, cannot
  // show a window anybody is looking at.  It answers with the frames of a
  // headless browser instead, and this surface draws them.
  const mirror = startMirrorSurface(core, loginId, started.mirror, () => cancelled);
  cancelHostChrome("relayBrowser");
  const wait = requestHostChrome<"form" | undefined>({
    kind: "relayBrowser",
    title: fillLabel(strings?.relaySignInTitle ?? "Sign in", options.label),
    body: strings?.relayBrowserWaiting ?? "",
    browser: browserName,
    cancelLabel: strings?.cancel ?? "Cancel",
    showLabel: strings?.relayBrowserShow ?? "",
    // A window can open behind the person's own browser — or onto a Space they
    // are not looking at — and then "nothing happened" is exactly what they
    // see.  One press raises it again; Core recreates it if it was dismissed.
    show: () => { void core.hostRoute("/ipc/host/relay/browser/focus", { login_id: loginId }).catch(() => undefined); },
    mirror,
  });
  void wait.then(() => { cancelled = true; });
  try {
    while (!cancelled) {
      let answer: Record<string, unknown>;
      try {
        answer = await core.hostRoute("/ipc/host/relay/browser/poll", { login_id: loginId });
      } catch {
        return undefined;
      }
      const status = String(answer.status ?? "");
      if (status === "signed_in") {
        return {
          revision: Number(answer.revision ?? 0),
          loginStatus: "signed_in",
          username: String(answer.username ?? options.username ?? ""),
        };
      }
      if (status === "closed" || status === "expired" || status === "unknown" || status === "rejected") return undefined;
      await sleep(BROWSER_LOGIN_POLL_MS);
    }
    return undefined;
  } finally {
    window.removeEventListener("beforeunload", abandon);
    window.removeEventListener("pagehide", abandon);
    cancelHostChrome("relayBrowser");
    void core.hostRoute("/ipc/host/relay/browser/cancel", { login_id: loginId }).catch(() => undefined);
  }
}

/**
 * What a refused page sign-in says beside its own fields.
 *
 * Core's codes are the three answers a person can act on differently: the
 * station refused the credential, the station is already holding as many
 * sessions as it allows, or the station was never reached.  Anything else is a
 * generic refusal rather than a claim about the password — Core's English
 * prose is never printed into a localized pane, so the code chooses the row.
 */
function relaySignInFailure(error: unknown, strings: NativeLocalization | undefined): string {
  const code = typeof error === "object" && error !== null && typeof (error as { code?: unknown }).code === "string"
    ? (error as { code: string }).code
    : "";
  if (code === "relay_session_limit") return strings?.relaySignInLimit || strings?.relaySignInRejected || "";
  if (code === "relay_unreachable") return strings?.relaySignInUnreachable || "";
  return strings?.relaySignInRejected || "";
}

export function createBrowserLeafBridge(core: BrowserCoreBridge, options: {
  /** The route this document is showing. */
  initialRoute: AppRoute;
  navigate: (route: AppRoute) => void;
  language: LanguagePreference;
  systemLocale: string;
  /**
   * Present one child route as a layer over this document, and end it again.
   * The browser host supplies these; a page with no layer implementation
   * (a test harness) has no child surfaces at all, which is stated by omission
   * rather than faked with a route swap.
   */
  presentChild?: (route: AppRoute) => void;
  dismissChild?: (route: AppRoute) => boolean;
  dismissPresentation?: () => void;
}): NativeLeafBridge {
  let strings: NativeLocalization | undefined;
  let menuActions: NativeMenuAction[] = [];
  // The last service state this host was told.  The tab's title is that state
  // in the app's words, so the state is held here until the words arrive.
  let routeStatus: ServiceStatus | undefined;
  // The document the raw editor layer was last asked to show. The native hosts
  // hand it to the child window's root through this one query, and the shared
  // pane reads it the same way.
  let pendingFileEditorTarget = "";
  // A secret lease is two host-only exchanges: mint a token, then spend it.
  // Both travel on Core's own host routes, so no secret ever passes through
  // the public IPC schema or through React state.
  const secretCapability = async (route: string, body: Record<string, unknown>): Promise<Record<string, unknown> | undefined> => {
    try {
      return await core.hostRoute(route, body);
    } catch {
      return undefined;
    }
  };

  /**
   * The tab's title, once there are words to say it in.
   *
   * A title is a sentence in the language this document speaks, and the only
   * statement of that language is `setLocalization`: before it arrives this
   * host knows no words at all, so a title written then is a guess, and a guess
   * in the wrong language is what a Chinese browser showed in its tab before
   * the table replaced it.  The title is therefore held until Core's answer is
   * here -- and re-stated whenever that answer changes, so the 常规 language row
   * reaches the tab in the same commit as the panes instead of waiting for the
   * service state to move.
   */
  const writeDocumentTitle = (): void => {
    if (!strings || !routeStatus || typeof document === "undefined") return;
    document.title = `${strings.appTitle}: ${serviceStateWord(strings, routeStatus.state)}`;
  };

  /**
   * Which child routes are on screen, outermost last. A child surface is not a
   * second document, so its own Close ends it while the pane underneath keeps
   * every staged value it already held.
   */
  const openChildren: AppRoute[] = [];

  const closeChild = (route?: AppRoute): boolean => {
    if (openChildren.length === 0) return false;
    const target = route ?? openChildren[openChildren.length - 1];
    const index = openChildren.lastIndexOf(target);
    if (index < 0) return false;
    // Topmost first: a layer stacked above the one being closed cannot stay on
    // screen once the surface it was opened over is gone.
    for (const open of openChildren.slice(index).reverse()) options.dismissChild?.(open);
    openChildren.length = index;
    return true;
  };

  const download = (name: string, data: string): void => {
    const url = URL.createObjectURL(new Blob([data], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    // The anchor has to be in the document for a click to count as a download
    // in every browser, and removing it afterwards keeps the page untouched.
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    // Revoking on the next tick cancelled any download the browser had not
    // finished opening — a large export failed with no error, because the blob
    // was gone before the save began.  A minute is far past the read; the
    // object is released either way, so nothing is leaked past this window.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  const downloadCapability = async (token: string, name: string): Promise<void> => {
    const response = await core.hostRoute("/ipc/host/file-download", { token });
    const data = typeof response.data === "string" ? response.data : "";
    if (!data) throw new Error("The export is empty");
    const bytes = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name || "young-router-data.json";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    // Same hold as `download`: a revoked blob cancels a save that has not begun.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  // The transport this host wraps.  Captured before the override: a wrapper
  // that called `core.send` would call itself.
  const sharedSend = core.send;

  const sendWithBrowserDownloads = async (request: string): Promise<string> => {
    // The browser's save picker cannot write a file, so it answers with the
    // destination capability wrapped together with the filename:
    // `web-download:<name>:<token>`.  Core must see the bare token — a wrapped
    // spelling is an unknown capability — and the filename is this host's to
    // keep until the export answers.
    let outbound = request;
    let download: { name: string; token: string } | undefined;
    try {
      const body = JSON.parse(request) as { method?: string; params?: { destination_token?: string } };
      const wrapped = body.method === "export" ? body.params?.destination_token : undefined;
      if (typeof wrapped === "string" && wrapped.startsWith("web-download:")) {
        const rest = wrapped.slice("web-download:".length);
        const at = rest.lastIndexOf(":");
        if (at > 0) {
          download = { name: rest.slice(0, at), token: rest.slice(at + 1) };
          body.params = { ...body.params, destination_token: download.token };
          outbound = JSON.stringify(body);
        }
      }
    } catch {
      // Not JSON this host wrote: send it exactly as the caller built it.
    }
    // The plain transport, not `core.send`: that property is replaced by this
    // wrapper below, so calling it here would call this function again.
    const response = await sharedSend(outbound);
    if (download !== undefined) {
      // Only a successful export has bytes to take; a refused one leaves the
      // capability for Core's own cleanup.
      try {
        const payload = JSON.parse(response) as { ok?: boolean };
        if (payload.ok !== false) await downloadCapability(download.token, download.name || "young-router-data.json");
      } catch {
        // Core's error envelope is returned unchanged to the shared UI.
      }
    }
    return response;
  };

  // The host's transport wraps the shared one so export completion is still a
  // single Core request from the pane's point of view.
  (core as BrowserCoreBridge).send = sendWithBrowserDownloads;

  return {
    openWindow: (route) => {
      // A settings pane is not a child surface. On a native host, opening one
      // focuses the settings *window* the host already has; this host's
      // settings window is the page itself, so the faithful answer is to show
      // that pane in place — which also keeps the address bar describing what
      // is on screen. A layer here would put the pane on top of the same pane.
      // `home` is that same answer: a page has no hidden window to bring back,
      // so asking for it changes nothing rather than presenting a blank layer.
      if (route === "home" || isSettingsShellRoute(route)) {
        if (route !== "home") options.navigate(route);
        return;
      }
      // Everything else is a child surface: the provider wizard, the raw editor
      // and the WebDAV detail form. On a native host each is a movable window
      // over the settings window; here each is a layer over that same page.
      if (!options.presentChild) return;
      if (!openChildren.includes(route)) openChildren.push(route);
      options.presentChild(route);
    },
    closeWindow: (route) => {
      // A child closes itself; closing the shell returns to the launcher, which
      // is the one route this host still has to change in place.
      if (closeChild(route)) return;
      options.navigate("home");
    },
    focusWindow: (route) => {
      // A settings pane is the page, so focusing it is showing it — the same
      // answer `openWindow` gives, and the same reason a native host focuses
      // the settings window rather than a child surface.
      if (route === "home" || isSettingsShellRoute(route)) {
        if (route !== "home") options.navigate(route);
        return;
      }
      // A child surface focuses the layer that already shows it; the layer
      // itself decides that, so there is no second bookkeeping list to keep in
      // step.
      options.presentChild?.(route);
    },
    // No geometry control: the page is the browser's to size.
    setMenuBarStatus: (status) => {
      routeStatus = status;
      // Core states the service as `running`; a tab that reads the wire's own
      // spelling beside a Chinese title was the one place in this host that
      // spoke the protocol instead of the app.  The words are the localization
      // table's, which is what the native hosts put in their status menu.
      writeDocumentTitle();
    },
    setMenuBarActions: (actions) => { menuActions = actions; },
    setTrayStatus: (status) => { routeStatus = status; },
    setTrayActions: (actions) => { menuActions = actions; },
    openFilePicker: async () => {
      const file = await chooseFile();
      if (!file) return undefined;
      // The browser cannot name a local path safely, so the host uploads the
      // bytes to Core and receives an opaque capability token instead. React
      // then sees the same shape it does on native hosts, and no path ever
      // leaves this page.
      //
      // A refusal is not a cancel: Core rejects a file by suffix, size or a
      // length mismatch, and returning `undefined` here made that
      // indistinguishable from the person dismissing the dialog — the window
      // went back to 就绪 with nothing said and no way to learn why.
      return uploadFileCapability(core, file, "import");
    },
    saveFilePicker: async (suggestedName) => {
      try {
        const token = await createExportCapability(core, suggestedName || "export.json");
        return `web-download:${suggestedName}:${token}`;
      } catch {
        return undefined;
      }
    },
    showActionMenu: async (title, items, anchor) => requestHostChrome<number | undefined>({
      kind: "menu",
      title,
      items: items.map((item) => ({ title: item })),
      anchor,
    }),
    // The page's menu surface has no sections, so a grouped menu is flattened
    // into one list whose rows carry their group's name.  Without this the
    // leaf answered nothing at all, and the pane's own guard (`!showGrouped…
    // → return`) made 使用特定API a button that did nothing on click — the
    // one control in the External pane that silently did not exist here.
    // A row is labelled with its group only when the name adds something, so
    // a single-group menu still reads as a plain list.
    showGroupedActionMenu: async (title, groups, anchor) => {
      const rows = groups.flatMap((group, groupIndex) => group.items.map((label, itemIndex) => ({ groupIndex, itemIndex, label, groupTitle: group.title })));
      const multiGroup = groups.filter((group) => group.items.length > 0).length > 1;
      const choice = await requestHostChrome<number | undefined>({
        kind: "menu",
        title,
        items: rows.map((row) => ({ title: multiGroup ? `${row.groupTitle}  ${row.label}` : row.label })),
        anchor,
      });
      const picked = choice === undefined ? undefined : rows[choice];
      return picked ? { group: picked.groupIndex, item: picked.itemIndex } : undefined;
    },
    showConfirmation: async (title, message, confirmLabel, cancelLabel, destructive) => requestHostChrome<boolean>({
      // The app draws its own decisions; the same panel, in this page.  The
      // caller's two labels and its own destructive accent travel unchanged.
      kind: "confirm",
      title,
      message,
      confirmLabel,
      // An empty cancel label is the caller saying "no words of my own"; the
      // native leaves fall back to the host's localized 取消 and so must this.
      cancelLabel: cancelLabel || strings?.cancel || "",
      destructive: destructive === true,
    }),
    textPrompt: async (title, value, confirmLabel, cancelLabel) => requestHostChrome<string | null>({
      kind: "prompt",
      title,
      value,
      confirmLabel,
      cancelLabel,
    }),
    openFileEditor: (target) => {
      // The raw document editor is a child surface on both native hosts, and a
      // layer here: the pane it was opened from must still be behind it, with
      // its own staged values intact.
      pendingFileEditorTarget = target;
      if (!openChildren.includes("file-editor")) openChildren.push("file-editor");
      options.presentChild?.("file-editor");
    },
    prepareFileEditor: () => undefined,
    pendingFileEditorTarget: () => pendingFileEditorTarget,
    showReadOnlyText: async (title, text, closeLabel) => {
      // The native hosts open their own code-viewer window. A page draws the
      // same record in a layer, with selectable text: the editor's page is
      // built from inline scripts, and this document's CSP (`script-src
      // 'self'`) is inherited by every frame it creates, so that page would
      // load without ever running.
      await requestHostChrome<void>({ kind: "viewer", title, text, closeLabel });
    },
    // No `showProviderAuth`: a page has no WebKit the provider could talk to,
    // and defining a no-op here would tell the shared UI the device-code
    // challenge was shown while nothing appeared. Leaving it undefined is what
    // selects the read-only viewer fallback, which states the URL and code.
    chooseModelsToAdd: async (models, providerName, keyName) => requestHostChrome<string[] | undefined>({
      kind: "picker",
      title: strings?.modelChooserTitle ?? providerName,
      models,
      providerName,
      keyName,
      confirmLabel: strings?.modelChooserAddSelected ?? "",
      cancelLabel: strings?.cancel ?? "",
      // The chooser is a surface, not a sentence: the native hosts build it with
      // a heading, a provider/key line, a search field, 全选/反选 and a count,
      // and this host drew only the checkboxes and the two buttons.  Every word
      // below is the same string the native leaves read from their table, so the
      // three hosts offer the same chooser.
      labels: {
        providerLabel: strings?.modelChooserProvider ?? "",
        keyLabel: strings?.modelChooserKey ?? "",
        search: strings?.modelChooserSearch ?? "",
        selectAll: strings?.modelChooserAll ?? "",
        selectAllToolTip: strings?.modelChooserSelectAllVisible ?? "",
        invert: strings?.modelChooserInvert ?? "",
        invertToolTip: strings?.modelChooserInvertVisible ?? "",
        count: strings?.modelChooserCount ?? "",
        countFiltered: strings?.modelChooserCountFiltered ?? "",
        countSelected: strings?.modelChooserCountSelected ?? "",
        empty: strings?.modelChooserEmpty ?? "",
        noMatches: strings?.modelChooserNoMatches ?? "",
      },
    }),
    editSecret: async () => undefined,
    clearSecret: async (domain, field, target) => {
      // Clearing is the same two host-only exchanges as staging, with the
      // `clear` form of the stage body: no value, an empty sentinel, or a
      // second editor ever reaches React.
      const capability = await secretCapability("/ipc/host/secret/capability", { domain, field, target, purpose: "settings" });
      const token = typeof capability?.secret_token === "string" ? capability.secret_token : undefined;
      if (token === undefined || capability?.present !== true) return undefined;
      const staged = await secretCapability("/ipc/host/secret/stage", { secret_token: token, clear: true });
      const revision = typeof staged?.revision === "number" ? staged.revision : undefined;
      return revision === undefined ? undefined : { revision, present: staged?.present === true };
    },
    readSecret: async ({ domain, field, target }) => {
      const capability = await secretCapability("/ipc/host/secret/read-capability", { domain, field, target });
      const token = typeof capability?.secret_read_token === "string" ? capability.secret_read_token : undefined;
      if (token === undefined) return undefined;
      const lease = await secretCapability("/ipc/host/secret/read", { secret_read_token: token });
      return typeof lease?.value === "string" ? { present: true, value: lease.value } : { present: false };
    },
    stageSecret: async ({ domain, field, target, value }) => {
      const capability = await secretCapability("/ipc/host/secret/capability", { domain, field, target, purpose: "settings" });
      const token = typeof capability?.secret_token === "string" ? capability.secret_token : undefined;
      if (token === undefined) return undefined;
      const staged = await secretCapability("/ipc/host/secret/stage", { secret_token: token, value });
      const revision = typeof staged?.revision === "number" ? staged.revision : undefined;
      return revision === undefined ? undefined : { revision, present: staged?.present === true };
    },
    copySecret: async (domain, field, target) => {
      // The value is copied inside this module and never returned to React:
      // the shared UI gets the same boolean the native leaves answer.
      const capability = await secretCapability("/ipc/host/secret/read-capability", { domain, field, target });
      const token = typeof capability?.secret_read_token === "string" ? capability.secret_read_token : undefined;
      if (token === undefined) return false;
      const lease = await secretCapability("/ipc/host/secret/read", { secret_read_token: token });
      if (typeof lease?.value !== "string") return false;
      return writeClipboard(lease.value);
    },
    // 分组管理 is a layer here instead of a window: the four calls below are the
    // same contract the native sheets implement, and `platform/relayGroupSheet`
    // is where their state lives.  A page that answered these with undefined
    // (as this host used to) could only disable 分组, and it looked like a dead
    // button rather than a surface this host cannot draw.
    showGroupManager: (options) => openRelayGroupSheet(options),
    awaitGroupManagerApply: () => takeRelayGroupApply(),
    finishGroupManagerApply: async (options) => { finishRelayGroupApply(options); },
    updateGroupManager: async (snapshot) => updateRelayGroupSheet(snapshot),
    relayLogin: async (options) => {
      // Ask Core first, for every account that might already have a session.
      // That is the silent sign-in: the saved cookie, or the password this
      // account already remembers, is used without a form.  A pending account
      // (the add flow) has no such session and does not ask.
      if (options.pendingAccount !== true) {
        try {
          const body = {
            account_id: options.accountId,
            type: options.type || "",
            label: options.label,
            origin: options.origin,
            username: options.username ?? "",
            login_status: "signed_out",
          };
          const result = await core.hostRoute("/ipc/host/relay/restore", body);
          if (result.login_status === "signed_in") {
            return { revision: Number(result.revision ?? 0), loginStatus: "signed_in", username: String(result.username ?? options.username ?? "") };
          }
        } catch {
          // No saved session Core can use. The form below is the next answer,
          // not a second failure: an unreachable station or a refused password
          // is stated inside it, where the person can act on it.
        }
      }
      // The station's own login page, in a real browser: Core opens the
      // window on this machine (the one the page is served from) at the
      // station's address, so a captcha, a verification code, or an OAuth
      // redirect finishes exactly where it does in any browser — the page
      // itself cannot present another origin's form, and a cookie another tab
      // minted is unreadable from here.  Core reads the session that window
      // holds and finishes the same verified-login boundary the native hosts
      // use.  A machine with no Chromium-family browser answers
      // `relay_browser_unavailable` and the credential form below is the
      // fallback, not a second feature.
      const browserLogin = await browserWindowLogin(core, options, strings);
      if (browserLogin !== "unavailable") return browserLogin;
      // No browser to open: the form says so first, because a station page
      // this host cannot show is the reason a password is being asked for.
      // A page cannot run the station's login page under this app's own
      // origin.  On a host with no browser to open, asking the station
      // account's own credentials and letting Core post the same form the
      // station's page would have posted is what still signs in; the refusal
      // is drawn beside fields that keep what was typed.
      cancelHostChrome("relaySignIn");
      return requestHostChrome<RelaySignInAnswer | undefined>({
        kind: "relaySignIn",
        title: fillLabel(strings?.relaySignInTitle ?? "Sign in", options.label),
        origin: options.origin,
        username: options.username ?? "",
        rememberPassword: options.rememberPassword === true,
        // Why a page is asking for this at all when the station has a website:
        // the answer is the reason Core named, in the person's own words.
        hint: [browserLoginFallbackHint(strings), strings?.relaySignInHint].filter((part) => part && part !== "").join(" "),
        labels: {
          origin: strings?.relaySignInOrigin ?? "",
          username: strings?.relaySignInAccount ?? "",
          password: strings?.relaySignInPassword ?? "",
          remember: strings?.relaySignInRemember ?? "",
          rememberHint: strings?.relaySignInRememberHint ?? "",
          submit: strings?.relaySignInSubmit ?? "Sign In",
          cancel: strings?.cancel ?? "Cancel",
        },
        submit: async ({ username, password, rememberPassword }) => {
          try {
            const body: Record<string, unknown> = {
              account_id: options.accountId,
              type: options.type,
              label: options.label,
              origin: options.origin,
              username,
              password,
              remember_password: rememberPassword,
              pending_account: options.pendingAccount === true,
            };
            if (options.stationId) body.station_id = options.stationId;
            if (options.stationName) body.station_name = options.stationName;
            if (options.stationType) body.station_type = options.stationType;
            if (options.stationOrigin) body.station_origin = options.stationOrigin;
            const result = await core.hostRoute("/ipc/host/relay/login", body);
            if (result.login_status !== "signed_in") {
              return { ok: false as const, message: strings?.relaySignInRejected ?? "" };
            }
            return {
              ok: true as const,
              value: {
                revision: Number(result.revision ?? 0),
                loginStatus: "signed_in" as const,
                username: String(result.username ?? username),
              },
            };
          } catch (error) {
            return { ok: false as const, message: relaySignInFailure(error, strings) };
          }
        },
      });
    },
    cancelRelayLogin: () => {
      cancelHostChrome("relayBrowser");
      cancelHostChrome("relaySignIn");
    },
    restoreRelaySession: async (options) => {
      try {
        const body = {
          account_id: options.accountId,
          type: options.type,
          label: options.label,
          origin: options.origin,
          username: options.username ?? "",
          login_status: "signed_out",
        };
        // Core's answer is the whole point of the call: a `signed_out`
        // observation still probes the password this account already
        // remembers and answers `signed_in` when the saved session works.
        // Dropping it made the pane's own success branch unreachable on this
        // host — a proven session still drew as signed out — and spent a
        // round trip per row for nothing.
        const result = await core.hostRoute("/ipc/host/relay/restore", body);
        if (result.login_status !== "signed_in") return undefined;
        return {
          revision: Number(result.revision ?? 0),
          loginStatus: "signed_in" as const,
          username: String(result.username ?? options.username ?? ""),
        };
      } catch {
        // An unreachable station is not a signed-out account: it says nothing
        // about the session, so the row keeps whatever it already knew.
        return undefined;
      }
    },
    openRelayLogs: async () => undefined,
    openRelayDashboard: async (options) => { window.open(options.origin, "_blank", "noopener"); },
    clearRelayPassword: async () => undefined,
    clearRelayCredentials: async () => undefined,
    // 开机自启动 on this host.  The desktop apps register their own login item
    // in their own process; a page has none, but the Core behind it can be that
    // app's Core on the same machine, so it writes the OS's login item and
    // answers whether it could.  That is only the owner's own page: on a
    // published page the person reading it is not at the machine Core runs on
    // (a gateway has no desktop session at all), so offering the switch there
    // was a control that could only ever fail -- and it failed with a raw
    // English sentence, after Core had already stored the preference.  Leaving
    // the method undefined is what selects the pane's own 此页面不提供 sentence
    // and its disabled switch, which is the truth for every other address.
    setLaunchAtLogin: isOwnerPage()
      ? async (enabled) => {
        const answer = await core.hostRoute("/ipc/host/autostart", { enabled }).catch(() => undefined);
        if (!answer) throw codedFailure("core_unavailable");
        if (answer.available === false) throw codedFailure("autostart_unavailable");
        if (answer.registered !== true) throw codedFailure("autostart_failed");
      }
      : undefined,
    versionInfo: async () => ({ app: "web", litellm: "" }),
    openExternalURL: (url) => { window.open(url, "_blank", "noopener"); },
    // This host is the one that signs in with a key, so it is the one host with
    // a session to end.  A native host leaves this undefined -- its lease is
    // this process's memory and ends with the app -- and so does the owner's
    // own page: Core signs a loopback page in without a key, so signing out of
    // it would reload into a fresh session and read as a button that did
    // nothing.
    signOutWebSession: isOwnerPage() ? undefined : () => core.signOut(),
    setLocalization: (next) => {
      strings = next;
      // The document's `lang` was written once by the build template as
      // English, while this host resolves the language from the browser and
      // Core's snapshot can change it again.  A Chinese interface under an
      // English document is read aloud in English phone.  The native hosts
      // set this on their own window; a page states it on its root element.
      const language = next.language ?? resolveLanguage("system", navigator.languages?.[0] ?? navigator.language);
      if (typeof document !== "undefined") document.documentElement.lang = language === "zh-Hans" ? "zh-Hans" : "en";
      // The service state may already have arrived while this page had no words
      // for it: state the title now, and re-state it on a later language choice.
      writeDocumentTitle();
    },
    setShortcuts: () => undefined,
  };
}

/**
 * The browser's file exchange is one upload plus one capability token.
 *
 * A native host hands Core a local path and receives an opaque token back; a
 * page cannot name a path, so it sends the bytes instead and lets Core mint
 * the same kind of token from them. The shared UI then sees exactly the shape
 * it already expects on macOS and Windows, and no filesystem path ever leaves
 * this document.
 */
async function chooseFile(): Promise<File | undefined> {
  return new Promise<File | undefined>((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = false;
    let settled = false;
    const settle = (file: File | undefined): void => {
      if (settled) return;
      settled = true;
      input.remove();
      window.removeEventListener("focus", onFocus);
      resolve(file);
    };
    input.onchange = () => settle(input.files?.[0]);
    // A cancelled dialog fires `cancel` in current browsers, but not in every
    // one: the fallback is the window's own focus returning, which is the only
    // signal a browser gives after the picker closes without a change.  Without
    // one of these the promise never settled, and the import/export flow that
    // awaits it stayed busy for the rest of the page's life.
    input.oncancel = () => settle(undefined);
    const onFocus = (): void => {
      // A change event is delivered before focus returns, so a short grace
      // period lets the chosen file win; a picker that closed empty lands here.
      setTimeout(() => settle(undefined), 300);
    };
    window.addEventListener("focus", onFocus);
    input.click();
  });
}

async function uploadFileCapability(core: BrowserCoreBridge, file: File, purpose: "import"): Promise<string> {
  // Core's existing file-capability route accepts a path, not bytes, so the
  // browser must first land the file somewhere Core can read it. The safest
  // place is the session itself: an authenticated POST that returns a fresh
  // capability tied to this page, never to any other reader on the port.
  const buffer = await file.arrayBuffer();
  const response = await core.hostRoute("/ipc/host/file-upload", {
    purpose,
    name: file.name,
    size: file.size,
    data: arrayBufferToBase64(buffer),
  });
  const token = typeof response.token === "string" ? response.token : "";
  if (!token) throw new Error("The selected file could not be staged");
  return token;
}

async function createExportCapability(core: BrowserCoreBridge, suggestedName: string): Promise<string> {
  // Export is the inverse: Core writes the package, and the browser downloads
  // it. The token returned here is what the shared UI later passes back into
  // `export`, so the two hosts stay symmetric even though one writes to disk
  // and the other streams bytes to the page.
  const response = await core.hostRoute("/ipc/host/file-download", {
    purpose: "export",
    name: suggestedName,
  });
  const token = typeof response.token === "string" ? response.token : "";
  if (!token) throw new Error("The export destination could not be prepared");
  return token;
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(offset, offset + chunk)));
  }
  return btoa(binary);
}
