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
import { pushPresentation } from "./presentationStack";
import { requestHostChrome } from "./hostChrome";
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
 * The session this document learned, kept in memory for this page only.
 *
 * It is deliberately not written to `sessionStorage`. The credential is the
 * `HttpOnly` cookie Core set at sign-in, and a remembered copy in page storage
 * outlived the Core that minted it: after a Core restart the browser sent the
 * stale lease in `Authorization`, the fresh cookie disagreed with it, and Core
 * refused every request — a page stuck on 加载中 under a storm of 401s. Asking
 * `hello` once per document costs one round trip and cannot outlive its Core.
 */
let rememberedSession: string | undefined;

/**
 * The browser cannot read `X-LiteLLM-Core-Session`, so Core hands it the same
 * lease as an `HttpOnly` cookie. That cookie is the real credential and every
 * request carries it; the remembered value above is only what this document
 * already learned, so a reload does not have to ask a second time.
 */
function coreRequest(transport: BrowserCoreTransport, path: string, init: RequestInit, session: string | undefined): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (session) headers.set("Authorization", `Bearer ${session}`);
  return fetch(`${transport.baseURL}${path}`, {
    ...init,
    headers,
    // The session cookie is the credential; `include` is what sends it back
    // to the same origin. Core sets it `SameSite=Strict`, so nothing else can.
    credentials: "include",
    cache: "no-store",
  });
}

async function hello(transport: BrowserCoreTransport): Promise<string> {
  // No credential is presented here: the browser sends the session cookie Core
  // set when this page signed in with the web password. Presenting one is what
  // makes `/ipc/hello` answer with the session this page keeps.
  const response = await coreRequest(
    transport,
    "/ipc/hello",
    { method: "POST", body: "{}" },
    undefined,
  );
  if (!response.ok) throw new Error("Core IPC authentication failed");
  // The lease itself stays in the HttpOnly cookie; a page may not read the
  // header, so an empty value simply means the cookie is doing the work.
  return response.headers.get("X-LiteLLM-Core-Session") ?? "";
}

/** The bridge plus the two host-route doors only a host process may use. */
export type BrowserCoreBridge = NativeIpcBridge & {
  hostRoute(path: string, body: Record<string, unknown>): Promise<Record<string, unknown>>;
  request(method: string, params?: Record<string, unknown>): string;
};

export function createBrowserIpcBridge(transport: BrowserCoreTransport): BrowserCoreBridge {
  let session: string | undefined = rememberedSession;
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
    if (session !== undefined) return Promise.resolve();
    if (pending !== undefined) return pending;
    const exchange = (async () => {
      session = await hello(transport);
      rememberedSession = session;
    })();
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

  const call = async (path: string, body: string): Promise<Response> => {
    await establish();
    const response = await coreRequest(transport, path, {
      method: "POST",
      body,
      headers: { "Content-Type": "application/json" },
    }, session);
    if (response.status === 401) leaveForSignIn();
    return response;
  };

  const send = async (request: string): Promise<string> => {
    const response = await call("/ipc", request);
    if (response.status >= 500) throw new Error("The local core is unavailable.");
    // A 400 carries a protocol envelope the shared client renders itself; only
    // a refused credential or an empty body is an error here.
    const text = await response.text();
    if (!text) throw new Error("The local core returned no response.");
    return text;
  };

  const request = (method: string, params: Record<string, unknown> = {}): string => JSON.stringify({
    protocol_version: 1,
    request_id: `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    method,
    params,
  });

  /** The host-only capability leases, which travel on their own routes. */
  const hostRoute = async (path: string, body: Record<string, unknown>): Promise<Record<string, unknown>> => {
    const response = await call(path, JSON.stringify(body));
    const text = await response.text();
    if (!response.ok || !text) throw new Error("The requested secret field is unavailable");
    return JSON.parse(text) as Record<string, unknown>;
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
              headers: session ? { Authorization: `Bearer ${session}` } : {},
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

  return { send, subscribe: sharedSubscribe, hostRoute, request };
}

/** The service state in the app's words, as the title bar states it. */
function serviceStateWord(strings: NativeLocalization | undefined, state: ServiceStatus["state"]): string {
  switch (state) {
    case "starting": return strings?.serviceStarting || "Starting";
    case "running": return strings?.serviceRunning || "Running";
    case "unhealthy": return strings?.serviceUnhealthy || "Unhealthy";
    case "stopped": return strings?.serviceStopped || "Stopped";
    default: return strings?.serviceUnknown || "Unknown";
  }
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
  let routeStatus: ServiceStatus = { state: "unknown" };
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
    anchor.click();
    // The blob would otherwise live until this document unloads, once per
    // export press.
    setTimeout(() => URL.revokeObjectURL(url), 0);
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
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
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
      document.title = `${strings?.appTitle ?? "Young Router"}: ${serviceStateWord(strings, status.state)}`;
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
      try {
        const token = await uploadFileCapability(core, file, "import");
        return token;
      } catch {
        return undefined;
      }
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
      try {
        await navigator.clipboard.writeText(lease.value);
        return true;
      } catch {
        return false;
      }
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
      // A page has no authenticated browser to put the station's login form in,
      // and a cookie minted in another tab is unreadable here. What a page can
      // do is ask Core to use the credential it already holds: the restore route
      // probes the saved session first and, when that is refused, signs in with
      // the remembered password. The answer is the login state, exactly the
      // shape the pane reads from a native sign-in — a station Core cannot sign
      // in to without a browser answers `signed_out`, and the pane says so
      // instead of the row waiting on a sign-in that never started.
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
        return result.login_status === "signed_in"
          ? { revision: Number(result.revision ?? 0), loginStatus: "signed_in", username: String(result.username ?? options.username ?? "") }
          : undefined;
      } catch {
        return undefined;
      }
    },
    cancelRelayLogin: () => undefined,
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
        await core.hostRoute("/ipc/host/relay/restore", body);
        return undefined;
      } catch {
        return undefined;
      }
    },
    openRelayLogs: async () => undefined,
    openRelayDashboard: async (options) => { window.open(options.origin, "_blank", "noopener"); },
    clearRelayPassword: async () => undefined,
    clearRelayCredentials: async () => undefined,
    // 开机自启动 on this host.  The desktop apps register their own login item
    // in their own process; a page has none, but the Core behind it is that
    // app's Core on the same machine, so it writes the OS's login item and
    // answers whether it could.  A Core from a working tree has no installed
    // app to point at and answers `registered: false`, which the pane reads as
    // a failed save and hands the preference back.
    setLaunchAtLogin: async (enabled) => {
      const answer = await core.hostRoute("/ipc/host/autostart", { enabled }).catch(() => undefined);
      if (answer?.registered !== true) throw new Error("The login item could not be updated");
    },
    versionInfo: async () => ({ app: "web", litellm: "" }),
    openExternalURL: (url) => { window.open(url, "_blank", "noopener"); },
    setLocalization: (next) => { strings = next; },
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
    input.onchange = () => {
      const file = input.files?.[0];
      resolve(file);
    };
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
