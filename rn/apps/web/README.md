# The browser host

The browser runs the same panes, controls, translations, and IPC client as the
macOS and Windows apps, from `rn/packages/shared`. React Native for Web
provides the browser renderer. This directory builds the assets Core serves.

## Signing in to a station from a server

The macOS host presents a station's login page in a WebKit view; Windows uses
WebView2. A page cannot: another origin's page cannot be framed, its cookies
are unreadable, and a proxied copy carries this app's hostname into a captcha's
site key. So Core opens the station itself — and *where* depends on whether
this machine has a screen:

- For a local page, Core opens a browser window when a display is available.
- For a published page or a machine without a display, Core streams the login
  page and forwards input from the current dialog.

Core verifies the session with the station before saving it and closes the
temporary browser afterward. If the browser cannot launch, the page offers a
credential form.

The one prerequisite is a Chromium-family browser. `scripts/install-chromium.sh`
installs one when a host has none (apt, dnf, zypper, pacman, apk or brew) and is
idempotent; `scripts/build-and-install-macos.sh` runs it as part of the install
and only warns when it cannot. `YOUNG_ROUTER_BROWSER` states which browser to
drive, by path or by name, for a host whose only one is `chromium`.

## What serves it

The page rides the port a person and a client already know: the router's own
local port. The proxy owns that listener, so it forwards `/ui` (this bundle) and
`/ipc` (Core's settings API) back to Core, and everything else stays LiteLLM's
`/v1`. One origin, therefore:

- no CORS answer and no preflight: the page calls the same host and port it was
  served from;
- no second port for anyone to remember, and none for a firewall to explain;
- The browser session uses only the `HttpOnly; SameSite=Strict`
  `yr-core-session` cookie, with `Secure` when served over TLS. Sessions last
  thirty days unless the user signs out. Local loopback access signs in
  directly;
- the sign-in key is `YOUNG_ROUTER_WEB_PASSWORD`, generated on first use and
  editable only in the app's General pane, which reads it over the same
  plaintext lease as the proxy key.

For development, `young-router-core --web-host <dir>` states the bundle
explicitly and prints the address; an installed build finds it at `Core/web`
without a flag. Without a bundle, Core serves no page at all.

## Build

    node rn/apps/web/build.mjs                       # → rn/apps/web/dist

The output is a directory Core can serve as-is: `index.html`, one script, the
editor page it frames with the editor's own script beside it, and the shared
sidebar tiles inlined into the shell.

Everything in that directory is what a browser on the other end of a link
downloads, so the build spends its effort on size without changing what runs:

- **One script, and no editor in it.** `bundle.<digest>.js` is the whole app,
  addressed by its own bytes because Core answers it `immutable`.
  `ui/code-editor/CodeEditorWebBundle` is half a megabyte of Ace that the two
  native hosts inline into the page they hand a WebView; this host frames the
  page Core serves instead, so `CodeEditorWebBundle.web.ts` answers that import
  with an empty string and esbuild resolves it first (`resolveExtensions`).
  Metro never picks it: `web` is not in either native app's platform list.
- **The editor is a file, not an inlined string.** `editor.html` is a few KB and
  names `editor.<digest>.js`; the native hosts keep the inline copy because a
  WebView has no origin to fetch from. `editor.html` is answered `no-store`, so
  an inlined editor was fetched again on every open of the raw editor.
- **Every text asset ships precompressed.** Beside each `.js`/`.html` are `.br`
  (brotli, quality 11) and `.gz` siblings, and Core answers the one the
  browser's `Accept-Encoding` admits with `Content-Encoding` + `Vary`. Doing
  this here rather than in Core keeps a worker thread off the first page load
  of every browser, and the sibling cannot go stale: it is named after the file
  it was built from. The publisher's `MANIFEST.json` (`scripts/build-web-page.sh`) names the page by what it
  decodes to, not by its encoding.

The last two matter most on the host this page exists for: a headless machine
serving it over a link nobody sized for it.

## What is deliberately not the same

- **A narrow page changes shape; it does not clip.** The two native hosts
  refuse a window smaller than their own minimum, so no shared pane ever had to
  answer "what if there is no second column". A phone has no minimum, so the
  page answers it: below 760 pt the sidebar becomes a strip of route tabs, and
  the second-level rails of 运行时, 外部 and 备份与同步 become horizontal strips of
  chips above the detail they select. Below 1024 pt the providers pane draws one
  column — the provider list and the model table — and a pressed row's
  inspector replaces them, with a back bar returning to the list; three columns
  need about 900 pt before the middle table folds to truncated cells. Both
  breakpoints live in `useWebMediaQuery` in the shared UI (`useCompactWebShell`
  and `useCompactProvidersPane`), so a wide page and both native hosts keep the
  layout they had.
- **A child surface is a layer, not a window.** `NativeWindow.open` presents
  the provider wizard, the raw editor and 分组管理 as a modal layer pinned to
  the viewport with a draggable title bar, on the same page. It is fixed to the
  viewport so no pane can clip it, it locks the page behind it while it is up,
  and it keeps one address: a tab per child surface would put three history
  entries behind one settings change. Each layer mounts its own React root of
  the same component, which is exactly what a native host does when it makes a
  second window.
- **No menu bar or tray.** A page uses its address bar for navigation. When
  opened on the machine running Core, it can still change that machine's
  launch-at-login setting; a remotely published page cannot.
- **Clipboard work is best effort by design.** `navigator.clipboard` exists only
  in a secure context, so a page published over plain HTTP from a server has no
  copy API; copying a key falls back to the editing command inside the press, and
  the editor's Paste item says to use ⌘V/Ctrl+V (which the frame receives as a
  normal paste event) instead of silently doing nothing.
- **`Platform.OS` is `web`.** `NativeControls` answers those branches with
  plain React (`NativeButton`, `NativeSegmentedControl`/a scrollable strip,
  `NativeTextField`, the Windows toggle and checkbox); this host adds a real
  `<select>` picker, an `<input>` secret field over Core's read/stage leases,
  and reuses the Windows table, which is already a virtualized React table.
- **Host surfaces are React.** A decision panel, a text prompt, the read-only
  record viewer, the model picker and an anchored menu are requests in
  `platform/hostChrome.ts`, drawn by `ui/HostChrome.tsx` beside the app root
  (`#host-chrome`) with the same primitives every pane uses. They used to be
  `document.createElement`: a second hand-written stylesheet, a viewer whose
  script the page's own CSP refused to run, and one Escape listener per layer
  that closed every open surface at once. Escape and Tab now go through
  `platform/presentationStack.ts`, so only the topmost layer answers.
- **A fixed-position tab strip.** Native segmented controls are one fixed
  width; a browser's may be wider than its row, so the web strip scrolls and
  sizes each segment to its own label.
