# Young Router Repository Guide

## What Belongs Here

- This guide holds decisions a reader cannot recover from the tree: the trap, the boundary, the non-obvious consequence. Never restate what the source already states — a symbol's behaviour, a name that exists, a change that was made, or a line that was removed — because that copy drifts the moment the code moves. A rule whose whole content is "do not reintroduce what was deleted" is also pollution: the tests that fail without it are the record. Prefer a comment at the site; a rule earns a line here only when it spans files a reader would not otherwise connect.

## Scope

- `young-router` is a standalone repository; do not look for instructions in parent directories. The distribution is `young-router`, the import package `young_router`.
- Layout: `young_router/core/` (Core IPC, domains, Core-owned documents), `young_router/proxy/` (the LiteLLM data plane), `young_router/adapters/` (staged third-party workers), `young_router/config/`, `young_router/webdav/`, plus the package-root names both sides share (`callbacks.py`, `browser_identity.py`, `atomic_io.py`, `node_runtime.py`, …). `sitecustomize.py` at the repository root is copied to the Core root under the name CPython loads at interpreter startup. `rn/` is the React Native workspace (`packages/shared`, `apps/macos`, `apps/windows`), `scripts/` the build/release/update tooling, `tests/` the Python suites.
- Entry points: `python -m young_router.core|.config|.webdav` and the `young_router.core.<tool>` modules; the console scripts are named in `pyproject.toml`.
- Keep provider names, models, API keys, request IDs, local paths, traces, and logs out of committed files; `config.example.yaml` must stay publishable.

## Source Of Truth

- `rn/packages/shared/` owns the shared UI, routes, composition, interaction state, i18n, and the typed Core IPC client.
- `young_router/core/` owns domain state, validation, staged configuration, service operations, persistence, and the authenticated versioned loopback IPC contract.
- `young_router/proxy/streaming.py` is the one streaming entry point and patch surface; `streaming_recovery.py` owns route fallback, the recovery poll and its cooldowns, keepalives, and the terminal rules, and is re-exported through it.
- `rn/apps/macos/` (AppKit) and `rn/apps/windows/` (WinUI 3) own platform leaves: menus, windows, native controls, WebView hosts for the shared code panes, secure inputs, file panels, alerts, shortcuts, lifecycle.
- One feature, one shared UI, one Core domain. No second domain store, configuration writer, application shell, or legacy launcher. Embedded WebViews only for the code panes.
- React holds view state only: no config writes, no proxy process, no arbitrary paths, no raw secrets. Versioned code-editor document text is the exception.
- All user-visible shared strings go through i18n, and the language choice lives in each host's native application menu.

## Cross-Cutting Boundaries

These are the rules that span files no single comment can hold. Everything narrower belongs at the site it governs.

- Compact native density everywhere: no web-like whitespace, oversized empty cards, redundant in-content titles, or detached action rows. Tabs render only their active pane, and a compact layout reflows before controls overlap on either host.
- Labelled settings controls are fields of the one shared grid in `ui/YoungRouterApp.tsx` (`SETTINGS_FIELD_*`, `SETTINGS_PANE_INSET`, and the `SETTINGS_FIELD_*` styles). Panes spread those styles; restating the numbers or picking a pane's own inset is a regression.
- A pane that splits into a list plus a detail renders the shared `SettingsRail` and `SettingsDetailHeader` — one rail width, one row rhythm, one header height. A rail selection swaps the detail surface; it never scrolls continuously and drives the selection back.
- Every content list keeps its native frame: never drop a list's 1 pt box, because the box is how the user sees where a list ends. The settings sidebar's own source list and the `SettingsRail` are the only permitted `framed={false}`: the sidebar's box drew rules that belonged to no row over the vibrant material, and the rail is a column of its pane that already draws its own one right divider.
- A settings pane has no action footer. The shell commits as the user edits (`IMMEDIATE_APPLY_DEBOUNCE_MS`), and its one permanently mounted bottom strip is the only report surface: one status per window, never a validation card over the content, never a Core step name. The rows a failure belongs to are marked in place (`alertRowKeys`).
- One on/off setting is a `NativeToggle`, only a multi-select is a `NativeCheckbox`; a required field wears the `＊` at its own label.
- Every child surface (provider wizard, raw file editor, 分组管理, sign-in browser, relay usage log, document viewer, official sign-in, model chooser) opens its own movable titled window above the window it was opened from, locked by `NativeChildPanelShield` through `presentChildPanel(_:in:prepare:)` and released by `endChildPanel`. Never `NSApp.runModal` (it freezes the React host) and never an attached sheet (undraggable, no title bar). A warm window is only created, never presented.
- A child surface that writes behind its own Save/Close creates nothing before that Save, states its result beside its own footer buttons, and keeps no second status bar; the window that opened it applies quietly and says nothing about the child's work.
- Every confirmation is one decision surface per host: `AppKitNativeLeaf.presentDecisionPanel(_:message:answers:in:locksParent:completion:)` / `WinUI3NativeLeaf::Confirm`, drawn the way `NSAlert` draws one and sized from its own constants. `destructive` is stated by the caller (`hasDestructiveAction`, the system critical ink), never inferred by matching a translated label; `cancelLabel` is a per-dialog word. Return, Escape, the parent going away, and the panel's own action each answer exactly once.
- Typed IPC snapshots and actions only: never a path, a secret, or a free native capability through ordinary props. Documents flow through the versioned editor IPC under `MAX_EDITOR_DOCUMENT_BYTES` / `MAX_MESSAGE_BYTES`, and an oversized document is `editor_too_large`, not a dead Core.
- Every `@objc` export must also be declared as `RCT_EXTERN_METHOD` in `AppKitNativeLeafBridge.m`; an undeclared method is silently `undefined` in JS and an optional chain no-ops with no log.
- A native surface reports a wait with the app's own busy wheel (`AppKitBusySpinner` frames / WinUI `ProgressRing`), never a progress-indicator view, and the pane scroll indicator stays mounted unconditionally.
- The runtime starts from `.litellm-runtime/config.yaml`; source configuration is validated and staged explicitly, and no file mutation may silently restart the service.
- Data-plane headers stay transparent: forward a downstream header byte-for-byte, never rewrite, normalize, strip, or impersonate one, and never invent an upstream identity header. Every exception needs its own focused regression test.
- Nothing this app sends may identify it: `young_router/browser_identity.py` is the one browser identity, mirrored by `RelayBrowserIdentity` (macOS) and `kRelayBrowserUserAgent` (Windows), and a staged helper that ships its own name is normalized while it is staged. Only a loopback-only control call may keep an internal marker.
- The macOS proxy runs with `LITELLM_NUM_WORKERS=16`. Never lower it, and never shrink it as a performance or recovery workaround.
- Every artifact-producing build first syncs `LITELLM_VERSION` to the latest installable stable LiteLLM release and re-checks each staged integration against its upstream release (`pi-web-access`, `dsh-vision-router`, `Veridrop`, `dsh-workbuddy-connect`); a failed check fails the build. Resolving the release is mandatory and must stay mandatory: what a build may skip is only the *reinstall* of a release it has already staged — and only when the staged tree proves it is that exact release, by the record the staging script wrote (package, version, and the registry's published digest) plus its entry points, peers, and any staged adaptation still being intact. Anything else stages from scratch, so a stale, partial, changed, or tampered tree is never packaged. Never reimplement a staged protocol here or patch a staged package in place.
- Preserve LiteLLM routing, Responses stream semantics, tool ordering, ids, explicit terminal errors, and metadata-driven compatibility bridges. No request-specific or provider-specific routing hacks. A cooldown may remove a failed route only when another eligible peer exists, and it must not erase the only configured deployment from a fresh request.
- The IPC schema beside the implementation is the contract: validators, the typed client, both host bridges, and the focused tests move together. Event delivery and the shared revision are per-window contracts — one Core event stream fans out to every registered window module, and every host passes Core's private `--metadata` path so a replacement Core resumes the revision.
- The macOS Vision helper is built into `Contents/Resources/Core/bin/vision_ocr` from the RN macOS host; never restore a `Resources/App` lookup.
- A reviewer's own environment is not evidence about another's: a failure that reproduces only in an installed bundle, only with a populated config, or only on one host is reported as such, with the boundary named.

## Diagnostics That Are Not Code Facts

Procedures a reader needs while investigating, whose subject is a log or a recorded stream rather than a function.

- Count failures by parsing each log line's `event` field, never by counting the substring `stream_idle_timeout`: it also occurs inside `deployment_failover_marked` exception text and inside `stuck` records' `body`. Copy a reproduction's own transcript to a file first, because `server.log` rolls at ~10MB and a multi-minute bridge run can straddle the rotation boundary.
- A streamed frame says exactly what it carries: empty delta fields and an empty legacy `function_call` mirror are dropped, because clients and `stream_chunk_builder` read presence as meaning.

## Hosted Computer-Use Requests
- The router runs no computer-use executor: a hosted Responses `computer` tool is served only by an upstream that supports it, or answered with an explicit unavailability message before any attempt.
- Never bridge a request that declares a hosted `computer` tool into a chat request unless the client brings its own computer or browser tools — the bridge would drop the declaration and answer as if it were never asked. Codex declares those namespaces inside leading `additional_tools` input items, so every "does the client bring computer use" decision inspects `additional_tools` as well as `tools`.
- When a route has no Responses endpoint, the bridged chat request keeps the client's computer use: namespace tools are flattened to functions carrying `x-young-router-responses-namespace`, and their calls come back with the namespace restored.

## Codex Task-ID Incident Triage

- A supplied task/thread ID is the primary lookup key. Do not start with repository scans or broad log searches.
- Three-hop first pass: `read_thread` on the newest 1–2 turns, the exact rollout `task_complete` event, then LiteLLM records for the same task ID in a narrow window. Keep parent-thread status, latest-turn status, and child-task status separate — a child continuing after a parent transport failure is expected.
- Prefer Codex task tools over filesystem inspection; resolve a rollout path by exact task ID from the read-only state database, never by scanning all of `~/.codex/sessions`. Query logs by exact task/request ID, event, and timestamp, printing only timestamp, event, request id, deployment, status, and a sanitized exception.
- Read source only after the exact error text or trace event names the owning branch, then stop once the causal chain is closed and make the smallest fix with its focused regression test.

## Development And Verification

- Keep all project-owned test sources directly under `tests/`, and keep the working tree free of `work/`, `tmp/`, and preview bundles: ephemeral probes belong in a system temporary directory that is removed when the run ends.
- A change to something the bundle ships (`young_router/`, `rn/`, a staged integration) ends with its focused checks and then `./scripts/build-and-install-macos.sh`, which owns the graceful restart. A change that ships nothing — `AGENTS.md`, `README.md`, `tests/`, a staging script — is not a deploy: no build, and no restart of a running app. Never stop, kill, or start the app outside that script, and never split a stop and a start across separate operations. If the mandatory PyPI sync fails on DNS or TLS, retry the same deploy in a bounded loop (with `HTTPS_PROXY`/`HTTP_PROXY` when a local proxy is needed); never bypass it.
- Decide that mechanically, never from the commit message: `diff -rq --exclude=__pycache__ young_router "/Applications/Young Router.app/Contents/Resources/Core/young_router"` reports only the four generated adapter trees when the installed build already carries the tree; any `Files … differ` line is a real deploy. The RN side is judged the same way, and its minified bundle proves nothing afterwards.
- Build Windows only on a Windows host (`pnpm run build:windows`); `./scripts/package-release.sh` for a release archive. `scripts/version.py` owns `VERSION`, `BUILD_NUMBER`, and every platform's version metadata — do not add obsolete metadata back.
- A managed client catalog is generated, never hand-edited: an unknown enum value rejects the whole catalog, so verify every field against the installed client binary and regenerate through the same registry construction the Core uses.
- A managed catalog pointer must never dangle — Codex rejects its entire configuration while `model_catalog_json` names a missing file, so a deleted managed file is rebuilt, a historical spelling is migrated, and a foreign path is left alone.
- For desktop UI work a build, launch, deep link, or accessibility read is **not** visual verification: capture the target window and inspect the image, or report it unverified. `semantic`/`fused` observations can fold to an outline-only look, where a `click` target is refused and only semantic actions remain. Only a proven failure to enumerate or control the target justifies a narrowly scoped `osascript` fallback, and then read-only.
- A functionality request is unfinished until that functionality has been exercised end to end in the session that claims it, with the evidence in hand. Whatever could not be exercised is reported as unverified and named, never assumed to work.

## Public Repository Hygiene

- Do not commit runtime directories, virtual environments, package installs, generated bundles, logs, screenshots containing real data, local traces, or WebDAV settings. The same things do not belong in the working tree either.
- Before every commit or push, inspect untracked files and the staged diff for credentials, private endpoints, provider/model names, request or task IDs, local paths, and copied configuration; replace real values with neutral fixtures. Passing tests does not replace this review.
- Use the intended public or noreply Git identity for public remotes.
