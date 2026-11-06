/**
 * The browser host: a third way to drive one Core, on platforms neither native
 * host builds for.
 *
 * It is deliberately thin. `platformEntry.ts` does three things for the native
 * hosts — pick a transport, build the leaf adapter, register the app — and this
 * module does the same three with page APIs (`platform/browserLeaf.ts`) in
 * place of `NativeModules`. Everything above that line — routes, i18n, IPC
 * types, panes, the settings grid, the controls' Windows/macOS fallbacks — is
 * the code the two native hosts already run.
 *
 * The page is served by the Core it talks to, so `/ipc` is same-origin and the
 * session is an `HttpOnly` cookie. Nothing rides in the URL: Core serves a
 * password form until this browser holds a session, and that cookie is the
 * whole identity afterwards. A reload re-enters on it without asking again.
 */

import React from "react";
import { AppRegistry } from "react-native";
// A DOM renderer belongs to the host, not to the shared package, and its types
// are resolved from this app's own dependency tree.
import { createRoot } from "react-dom/client";
import { createTranslator, resolveLanguage } from "./i18n";
import { createIpcClient } from "./ipc";
import { createNativeIpcTransport, createNativeLeafBridgeAdapter } from "./platform/nativeBridge";
import { createBrowserIpcBridge, createBrowserLeafBridge, type BrowserCoreTransport } from "./platform/browserLeaf";
import { setBrowserSecretReader, setBrowserSecretStager } from "./platform/browserSecrets";
import { dismissChildLayer, presentChildLayer } from "./platform/childLayers";
import { HostChromeHost } from "./ui/HostChrome";
import { registerYoungRouter } from "./bootstrap";
import { canonicalWindowRoute, isSettingsShellRoute, ROUTES } from "./routes";
import type { AppRoute } from "./types";

function routeFromHash(): AppRoute {
  // The fragment is the route and nothing else: the password is typed into the
  // page, so no credential ever reached the address bar.
  const fragment = window.location.hash.replace(/^#/, "");
  const route = fragment.startsWith("/") ? fragment.slice(1) : fragment;
  if (!route) return "home";
  // Every route is one document here, so a carried-over route the app no
  // longer knows must fall back rather than render an empty surface.
  return canonicalWindowRoute(route as AppRoute) as AppRoute;
}

// Same origin: this page came from the listener that answers `/ipc`, and Core
// served it only because this browser already holds a session cookie. There is
// no credential to carry: the cookie is sent by the browser itself.
const transport: BrowserCoreTransport = {
  baseURL: "",
};

const systemLocale = navigator.language || "en";
const language = resolveLanguage("system", systemLocale);

// One document per window, so a route is the address rather than a window
// registry: opening a second tab is `window.open` and coming back is history.
let currentRoute = routeFromHash();

/**
 * Record the route the app is already drawing in the address bar.
 *
 * This host is the window for every pane, so the shared shell (a sidebar click,
 * a launch presentation) changes its own route and asks the host to open that
 * window; here that request is answered by writing the address down, never by
 * re-rendering. A remount would throw away the pane's staged fields for a
 * navigation that has already happened.
 */
function navigate(route: AppRoute): void {
  const next = canonicalWindowRoute(route) as AppRoute;
  if (next === currentRoute) return;
  currentRoute = next;
  window.location.hash = next === "home" ? "" : `#/${next}`;
}

// One bridge for this document: several would each exchange the one-shot
// bootstrap token, and the second would be refused.
const core = createBrowserIpcBridge(transport);

// The page's bootstrap table. Every other string is drawn by the app, which
// switches language from Core's snapshot; the host-level chrome (a child
// layer's title, a dialog's buttons) is drawn outside the React tree, so it
// asks the snapshot the same question the tree asks (`presentChild` below).
const translate = createTranslator("system", systemLocale);

/**
 * One child surface, in this page.
 *
 * The native hosts mount a second `RCTRootView` of the same component with a
 * different `initialRoute`; this mounts the same component in a second React
 * root inside the layer. Sharing the app's one root would make the layer
 * replace the pane it was opened from — the exact behaviour every native child
 * panel exists to avoid.
 */
let visibleChildRoute: AppRoute | undefined;
// The pane the page has drawn. A child surface arrives over it, and an address
// that names the child (a typed fragment, a second tab) still leaves this one
// underneath, which is where the address returns when the layer closes.
let shellRoute: AppRoute = "home";

function presentChild(route: AppRoute): void {
  const underneath = currentRoute === route ? shellRoute : currentRoute;
  visibleChildRoute = route;
  // The layer's title follows the language the panes are drawn in, which is the
  // snapshot's choice, not this page's accept-language: a browser set to en-US
  // was titling a 中文 wizard "Configure provider". The raw editor is a child
  // surface without a ROUTES entry (native hosts title it from `routeFileEditor`
  // in their localization table), so its id would otherwise be the literal text
  // `file-editor` on the title bar.
  const titleKey = ROUTES.find((entry) => entry.id === route)?.titleKey
    ?? (route === "file-editor" ? "settings.editFile" : route);
  // `ipc.latestSnapshot()` is the same projection the panes read, so the layer
  // and the pane it covers cannot disagree about the language.
  const language = ipc.latestSnapshot()?.language ?? "system";
  const chrome = createTranslator(language, systemLocale);
  presentChildLayer({
    route,
    title: chrome(titleKey),
    closeLabel: chrome("status.close"),
    mount: (container) => {
      // `appKey` is the name `registerYoungRouter` registered, so the layer gets
      // the same `DesktopHost` wrapper — its own RenderFaultBoundary, its own
      // snapshot seeding — that a native child window gets.
      const application = (AppRegistry as unknown as {
        getApplication(key: string, parameters?: { initialProps?: Record<string, unknown> }): { element: React.ReactElement };
      }).getApplication("YoungRouter", {
        // React Native for Web takes the root's props under `initialProps`,
        // exactly as `runApplication` does for the first window.
        initialProps: {
          // A child root states its own route, and is not the primary host: the
          // primary host owns menu bar actions, which a layer must not answer.
          initialRoute: route,
          isPrimaryHost: false,
          // A child surface is a window, not a window manager: the host has
          // already opened it, so it must not ask for another.
          isWindowManagerHost: true,
        },
      });
      const root = createRoot(container);
      root.render(application.element);
      return () => root.unmount();
    },
    onDismiss: () => {
      // A layer's own Close ends it; the pane underneath keeps every staged
      // value it already held, which is what the native child panels promise.
      // The host's stack can already be empty when the shared pane calls
      // closeWindow: the dismissal is still real, and must not navigate away.
      if (visibleChildRoute === route) visibleChildRoute = undefined;
      // An address that named the child would otherwise stay on screen as a
      // surface that no longer exists: the page went back to its pane, so the
      // address says that pane again.
      if (currentRoute === route) navigate(underneath);
      native.window.close(route);
    },
  });
}

// The same two wrappers the native entry uses: this host builds the same
// `NativeLeafBridge`, and everything above the bridge is shared code.
const native = createNativeLeafBridgeAdapter(createBrowserLeafBridge(core, {
  initialRoute: currentRoute,
  navigate,
  language,
  systemLocale,
  presentChild,
  dismissChild: (route) => dismissChildLayer(route),
}));

const ipc = createIpcClient(createNativeIpcTransport(core));

const browserFiles = {
  stageSecret: ({ domain, field, target, value }: { domain: string; field: string; target?: string; value: string }) => {
    // The host owns the DOM input's value until this commit: React only ever
    // receives the capability's revision and presence, never the secret.
    return core.hostRoute("/ipc/host/secret/capability", { domain, field, target, purpose: "settings" }).then((capability) => {
      const token = typeof capability.secret_token === "string" ? capability.secret_token : "";
      if (!token) throw new Error("The secret field is unavailable");
      return core.hostRoute("/ipc/host/secret/stage", { secret_token: token, value });
    }).then((staged) => ({
      revision: typeof staged.revision === "number" ? staged.revision : 0,
      present: staged.present === true,
    }));
  },
};
setBrowserSecretStager(browserFiles.stageSecret);
setBrowserSecretReader(({ domain, field, target }) => core.hostRoute("/ipc/host/secret/read-capability", { domain, field, target }).then((capability) => {
  const token = typeof capability.secret_read_token === "string" ? capability.secret_read_token : "";
  if (!token) throw new Error("The secret field is unavailable");
  return core.hostRoute("/ipc/host/secret/read", { secret_read_token: token });
}).then((lease) => ({ present: typeof lease.value === "string" && lease.value.length > 0, value: typeof lease.value === "string" ? lease.value : undefined })));

void ipc.snapshot().catch(() => {
  // The session cookie expired while this tab was open (Core was restarted, or
  // the session aged out). Reloading is the honest recovery: Core then answers
  // with its password form instead of this page sitting on a 401 it cannot
  // explain. Nothing is done here if the reload itself fails, because a Core
  // that stopped answering is not something a page can fix.
  window.location.reload();
});

registerYoungRouter("YoungRouter", { ipc, native, translate });

// The page's one React root. Re-rendering it with a new `initialRoute` is how
// this host answers an address it did not write (a typed fragment, Back, a
// second tab): the shared shell receives that route as a request and moves to
// it. `isWindowManagerHost: false` is the load-bearing part — with it true the
// shell answers a route request by asking the host to open a window it has
// already opened, so the pane never changed and the address bar described a
// surface the page was not showing.
let root: ReturnType<typeof createRoot> | undefined;
function presentRoute(route: AppRoute): void {
  shellRoute = route;
  const application = (AppRegistry as unknown as {
    getApplication(key: string, parameters?: { initialProps?: Record<string, unknown> }): { element: React.ReactElement };
  }).getApplication("YoungRouter", {
    initialProps: { initialRoute: route, isPrimaryHost: true, isWindowManagerHost: false },
  });
  root ??= createRoot(document.getElementById("root") as HTMLElement);
  root.render(application.element);
}

/** The routes this host draws as a layer rather than as the page's pane. */
const CHILD_SURFACES: readonly AppRoute[] = ["provider-wizard", "file-editor", "webdav-settings"];

/**
 * Adopt an address this page did not write.
 *
 * `navigate` above writes `currentRoute` before it touches `location.hash`, so
 * the hash change it causes arrives here already equal and is ignored — this
 * handler is only for a person typing a fragment, a bookmark, a second tab, or
 * Back/Forward. A child surface has no address of its own (see
 * `platform/childLayers.ts`): landing on one presents its layer over the pane
 * already on screen instead of replacing that pane with a second copy.
 */
function adoptAddress(): void {
  const next = routeFromHash();
  if (next === currentRoute) return;
  currentRoute = next;
  if (CHILD_SURFACES.includes(next)) {
    presentChild(next);
    return;
  }
  presentRoute(next);
}

window.addEventListener("hashchange", adoptAddress);
window.addEventListener("popstate", adoptAddress);

// A launch that arrives on a child surface's address still needs the page
// underneath it: the layer is drawn over the settings shell, exactly as it is
// when a pane opens it. Landing the shell on the child route as well would
// draw a second copy of the surface behind its own layer.
if (CHILD_SURFACES.includes(currentRoute)) {
  const surface = currentRoute;
  presentRoute("providers-models");
  presentChild(surface);
} else {
  presentRoute(currentRoute);
}

// The host's own surfaces — a decision panel, a text prompt, the record viewer,
// the model picker, an anchored menu — are React, mounted beside the app root so
// they draw above it and answer Escape through the same presentation stack the
// child layers use. One root of their own: sharing the app's root would make a
// dialog replace the pane it was asked from.
const chromeRoot = document.getElementById("host-chrome");
if (chromeRoot) createRoot(chromeRoot).render(React.createElement(HostChromeHost));
