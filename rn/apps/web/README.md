# The browser host

The third host for the same Core, for platforms neither native host builds for
(Linux, Android tablets, a locked-down machine): one page served by Core itself,
rendering `rn/packages/shared` through React Native for Web.

It is not a second UI. There is no component in this directory; every pane,
control, translation, route and IPC call comes from `packages/shared`, and this
app supplies only what the two native apps also supply — a transport, a leaf
adapter, and a build.

## Why a browser and not a desktop shell

The router is the product; the window is not. A page needs no installer, no
code signing, and no per-architecture build, and it reaches a machine that
already runs Core. A Tauri/Electron wrapper would add a second native stack to
keep building, which is the cost this host exists to avoid.

## What serves it

The page rides the port a person and a client already know: the router's own
local port. The proxy owns that listener, so it forwards `/ui` (this bundle) and
`/ipc` (Core's settings API) back to Core, and everything else stays LiteLLM's
`/v1`. One origin, therefore:

- no CORS answer and no preflight: the page calls the same host and port it was
  served from;
- no second port for anyone to remember, and none for a firewall to explain;
- the session is an `HttpOnly; SameSite=Strict` cookie (`yr-core-session`)
  rather than the header the native hosts use, because page script may read
  neither that header nor a token in `localStorage`. It states `Max-Age` for
  thirty days and Core records the digest beside its metadata, so neither a
  closed browser nor a replaced Core (a deploy, or the app's own restart)
  turns a signed-in page back into the key form; **退出登录**, at the bottom of
  the sidebar, ends the lease and its record and reloads onto the gate.
  `Secure` is added when the request reached Core over TLS, which the proxy
  states on the hop it forwards;
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
  it was built from. The publisher's `MANIFEST.json` names the page by what it
  decodes to, not by its encoding.

The last two matter most on the host this page exists for: a headless machine
serving it over a link nobody sized for it.

## What is deliberately not the same

- **A child surface is a layer, not a window.** `NativeWindow.open` presents
  the provider wizard, the raw editor and 分组管理 as a modal layer pinned to
  the viewport with a draggable title bar, on the same page. It is fixed to the
  viewport so no pane can clip it, it locks the page behind it while it is up,
  and it keeps one address: a tab per child surface would put three history
  entries behind one settings change. Each layer mounts its own React root of
  the same component, which is exactly what a native host does when it makes a
  second window.
- **No menu bar, no tray, no launch-at-login.** A page has none of them. The
  address bar is the menu.
- **No relay WebView sign-in.** The native hosts own an authenticated browser
  for it; a page cannot. 记住的密码 sign-in works because Core does it over
  HTTP.
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
- **One column when narrow.** A page has no minimum window size, so below
  760 pt the settings sidebar becomes a route strip above the pane instead of
  taking half a phone's width.
- **A fixed-position tab strip.** Native segmented controls are one fixed
  width; a browser's may be wider than its row, so the web strip scrolls and
  sizes each segment to its own label.
