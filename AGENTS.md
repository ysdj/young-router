# Young Router Repository Guide

## What Belongs Here

- This guide holds decisions a reader cannot recover from the tree: the trap, the boundary, the non-obvious consequence. Never restate what the source already states — a symbol's behaviour, a name that exists, a change that was made, or a line that was removed — because that copy drifts the moment the code moves. A rule whose whole content is "do not reintroduce what was deleted" is also pollution: the tests that fail without it are the record.

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

## UI And Native Boundaries

- Compact native density everywhere: no web-like whitespace, oversized empty cards, redundant in-content titles, or detached action rows. Tabs render only their active pane, and a compact layout reflows before controls overlap on either host.
- Labelled settings controls are fields of the one shared grid in `ui/YoungRouterApp.tsx` (`SETTINGS_FIELD_*`, `SETTINGS_PANE_INSET`, and the `SETTINGS_FIELD_*` styles). Panes spread those styles; restating the numbers or picking a pane's own inset is a regression.
- A pane that splits into a list plus a detail renders the shared `SettingsRail` and `SettingsDetailHeader` — one rail width, one row rhythm, one header height. A rail selection swaps the detail surface; it never scrolls continuously and drives the selection back.
- Every content list keeps its native frame: never drop a list's 1 pt box, because the box is how the user sees where a list ends, and removing the structure around it leaves the provider, model, key and log lists with no boundary at all. The two columns that carry no frame are not content lists. The settings sidebar's own source list sits on the vibrant sidebar material, so its box drew a top and a bottom rule that belonged to no row plus a right edge over the material. The `SettingsRail` is a column of its pane, not a list in it: it already draws its own one right divider, so the frame put a hairline under the pane title and another above the status strip and doubled an edge that already exists. Those two are the only permitted `framed={false}`, the pane-title and sidebar-title hairlines stay gone, and the rail's divider stays its only rule. A rule that reads as an artifact over a dark backdrop is fixed on that line alone.
- A settings pane has no action footer. The shell commits as the user edits (`IMMEDIATE_APPLY_DEBOUNCE_MS`), and its one permanently mounted bottom strip (`common.ready` → `saving` → `saved`) is the only report surface: one status per window, never a validation card over the content, never a Core step name. The rows a failure belongs to are marked in place (`alertRowKeys`).
- ＋ creates a *draft* (`enabled: false`) so an unfinished record is never reported as an error, and an apply enters the relay transaction only when relay-bound material actually changed.
- One on/off setting is a `NativeToggle`, only a multi-select is a `NativeCheckbox`; a required field wears the `＊` at its own label.
- Every child surface (provider wizard, raw file editor, 分组管理, sign-in browser, relay usage log, document viewer, official sign-in, model chooser) opens its own movable titled window above the window it was opened from, locked by `NativeChildPanelShield` through `presentChildPanel(_:in:prepare:)` and released by `endChildPanel`. Never `NSApp.runModal` (it freezes the React host) and never an attached sheet (undraggable, no title bar). A warm window is only created, never presented.
- A child surface that writes behind its own Save/Close creates nothing before that Save, states its result beside its own footer buttons, and keeps no second status bar; the window that opened it applies quietly and says nothing about the child's work.
- Every confirmation is one decision surface per host: `AppKitNativeLeaf.presentDecisionPanel(_:message:answers:in:locksParent:completion:)` / `WinUI3NativeLeaf::Confirm`, drawn the way `NSAlert` draws one and sized from its own constants. `destructive` is stated by the caller (`hasDestructiveAction`, the system critical ink), never inferred by matching a translated label; `cancelLabel` is a per-dialog word. Return, Escape, the parent going away, and the panel's own action each answer exactly once.
- Typed IPC snapshots and actions only: never a path, a secret, or a free native capability through ordinary props. Documents flow through the versioned editor IPC under `MAX_EDITOR_DOCUMENT_BYTES` / `MAX_MESSAGE_BYTES`, and an oversized document is `editor_too_large`, not a dead Core.
- Every `@objc` export must also be declared as `RCT_EXTERN_METHOD` in `AppKitNativeLeafBridge.m`; an undeclared method is silently `undefined` in JS and an optional chain no-ops with no log.
- A native surface reports a wait with the app's own busy wheel (`AppKitBusySpinner` frames / WinUI `ProgressRing`), never a progress-indicator view, and the pane scroll indicator stays mounted unconditionally.

## Runtime And Compatibility

- The runtime starts from `.litellm-runtime/config.yaml`; source configuration is validated and staged explicitly, and no file mutation may silently restart the service.
- Data-plane headers stay transparent: forward a downstream header byte-for-byte, never rewrite, normalize, strip, or impersonate one, and never invent an upstream identity header. Every exception needs its own focused regression test.
- Nothing this app sends may identify it: `young_router/browser_identity.py` is the one browser identity, mirrored by `RelayBrowserIdentity` (macOS) and `kRelayBrowserUserAgent` (Windows), and a staged helper that ships its own name is normalized while it is staged. Only a loopback-only control call may keep an internal marker.
- The macOS proxy runs with `LITELLM_NUM_WORKERS=16`. Never lower it, and never shrink it as a performance or recovery workaround.
- A newly created proxy process starts with empty route-recovery and deployment-cooldown state.
- Every artifact-producing build first syncs `LITELLM_VERSION` to the latest installable stable LiteLLM release and re-checks each staged integration against its upstream release (`pi-web-access`, `dsh-vision-router`, `Veridrop`, `dsh-workbuddy-connect`); a failed check fails the build. Resolving the release is mandatory and must stay mandatory: what a build may skip is only the *reinstall* of a release it has already staged — and only when the staged tree proves it is that exact release, by the record the staging script wrote (package, version, and the registry's published digest) plus its entry points, peers, and any staged adaptation still being intact. Anything else stages from scratch, so a stale, partial, changed, or tampered tree is never packaged. Never reimplement a staged protocol here or patch a staged package in place.
- Preserve LiteLLM routing, Responses stream semantics, tool ordering, ids, explicit terminal errors, and metadata-driven compatibility bridges. No request-specific or provider-specific routing hacks.
- The IPC schema beside the implementation is the contract: validators, the typed client, both host bridges, and the focused tests move together. Event delivery and the shared revision are per-window contracts — one Core event stream fans out to every registered window module, and every host passes Core's private `--metadata` path so a replacement Core resumes the revision.
- The macOS Vision helper is built into `Contents/Resources/Core/bin/vision_ocr` from the RN macOS host; never restore a `Resources/App` lookup.

## Known Runtime Failure Modes

Traps only — the code, its comments, and its tests hold the detail.

### Settings Window And Shared UI

- A snapshot is a projection, not a probe. Nothing that blocks on a subprocess, a network round trip, or a credential read may run on the path that serves `CoreStore.snapshot()`; a provider projection that reached a live worker once froze every window in the app.
- A read of external state never holds the pane-wide wait (it is acquired *before* the dispatch enqueues) and never occupies the buffered dispatch queue; live reads take `TRANSIENT_READ_ACTIONS`' own lane and report on their own control.
- Leaving a pane or closing the window flushes that pane's staged drafts: a debounced apply, a typed secret, and a code-editor draft all die with the unmount otherwise.
- A preference write must invalidate `SERVICE_STATUS_CACHE_SECONDS`, or the store's own projection keeps answering the pre-write value.
- The settings disk poll asks only for the domains it monitors; a full snapshot is a response to a changed marker, never the routine tick.
- AppleKit facts worth remembering: a controlled native boolean must repaint on its first props pass after a mount or recycle; `NSControl.target` and table `delegate`/`dataSource` are weak (a sheet that paints but answers nothing had its controller collected); `orderOut` sends no `windowWillClose`, so every dismissal path is wired explicitly; a borderless panel never becomes key without `canBecomeKey`.

### Providers, Models, And Routes

- A credential value is never a key's identity. Two slots may hold the same value; a route's key resolves by slot id, then by a name exactly one key carries, and a route that names no key stays unbound. The loader must not delete a same-value slot, the dumper refuses a model naming a key the provider does not carry, and an unbound route persists `x-young-router-key-binding: unbound`.
- The identity a document persists (slot id, deployment id, station resource id) decides its own row; a name is weaker and resolves only while it is unambiguous, and an unresolved id is reported rather than repaired onto the first key. A stored station label repairs only while unique.
- Apply writes in the order the pane shows: a live create is inserted before the provider's parked rows, because the dumper writes live entries ahead of parked ones and the read-back appends in that order.
- A probe's stored finding is a claim about the route's probe inputs (address, upstream model, key, protocol surface). Changing one drops it; the probe's own recommended-surface write re-keys instead of invalidating, and the pane shows its copy only while its own fingerprint matches. A route's credential is never a reason to refuse a probe.
- `探测` asks every address that is not this app's own published service; only a managed worker address is single-surface.
- A new row starts enabled, a new *route* starts disabled (it would take slot 0 of its own group and fail over on every request), and a ＋ draft is listed without an invented 未定义密钥 group.
- The model table carries no 顺序 column; order lives on the routes table (grouped by public model) and in the model's own field. A move asks before renumbering, 取消 truly cancels, a group following a multiplier is hand-ordered as a whole, and a swap between equal values says so instead of doing nothing.
- Selecting a public-model row in the routes table deletes the whole group — one action, one apply — not the route under it.
- The wizard's staged key is a token, never a secret target: the UI sends the bare token, `provider.add` peeks and pops only after the append, and a refused create keeps what it was given.
- A linked route that follows the multiplier is relay work: the relay projection names the key *and* the order mode, or the dumper's refusal surfaces as a validation failure the user cannot act on.
- A provider's own fields share the enable row's group; a bordered container is for a surface with its own identity (the key list, an account block, linked stations).
- A provider's type is fixed for that provider's lifetime: it owns the account contract, the address, the key slot, and every route's protocol surface, so adopting another type is a different provider rather than an edit of this one. Every surface states the type and none offers to change it, and a patch that names a different one is refused whole — no field of that patch lands, and no route is rewired.

### Relay And Station

- A model edit on a linked key must apply with the station fully down: a cached or already-persisted key value is enough, and a failed station read is deferred instead of published as the row's issue.
- A linked-key edit is a *dependency*, not the relay's own work: it validates nothing about the station's session or journal, runs no journal phase, and never lets another account's backlog refuse it. Only a genuine relay edit gates on the relay.
- A key's own `/v1/models` catalog is the authority for what it can call; a station's channel page is a fallback, never a veto.
- An aged-out station session is renewed once from the remembered password and never surrendered; the login status is an observation and never rewrites the key list.
- Reads get their own retry policy (a short first attempt, bounded resends) and documentary reads get one short attempt, because a stalled station answers nothing and only the socket timeout ends the read.
- A sync names its cause (`webdav_sync_failed` / `webdav_sync_incompatible` / `webdav_sync_conflict`); a foreign bundle is archived before a smart push, and a server that refuses DELETE/MOVE keeps the old file with a sentence saying so.
- The background sync loop reads committed state only (never an open window's draft), waits on one `Event` rather than polling, and counts a failed run as an attempt.

### Streaming, Recovery, And Proxy

- A stated budget must survive every rebuild between the turn and the call, and a non-streaming sub-call (a bridge synthesis) is invisible to the idle watchdog, so it carries its own bound.
- A gap budget is not a wall-clock budget. `stream_idle_timeout_seconds` bounds the silence *between chunks* on a streaming turn; on a non-streaming `acompletion` there are no chunks, so enforcing that number with `wait_for` caps the whole generation at a value chosen for inter-chunk silence. The bridge synthesis is the heaviest turn the bridge makes (it writes the entire final answer from the accumulated evidence) and legitimately outruns it — measured 177s, 256s, and one cut off past 300s mid-answer, which read as an intermittent failure because the recovery then completed the turn. A hidden non-streaming turn therefore carries `_EXTERNAL_WEB_SEARCH_CHAT_SUB_CALL_MAX_SECONDS` as its own wall-clock ceiling, and the streaming turns keep their gap budgets unchanged.
- Count failures by parsing each log line's `event` field, never by counting the substring `stream_idle_timeout`: it also occurs inside `deployment_failover_marked` exception text and inside `stuck` records' `body`. Copy a reproduction's own transcript to a file, because `server.log` rolls at ~10MB and a multi-minute bridge run can straddle the rotation boundary.
- A streamed frame says exactly what it carries: empty delta fields and an empty legacy `function_call` mirror are dropped, because clients and `stream_chunk_builder` read presence as meaning.
- A client opens a tool call on `response.output_item.added` and closes it on `response.output_item.done`; nothing else releases it, and a client that ends the turn still holding one reports `stream completed with an unfinished tool call`. The web-search bridge takeover restarts the output indexes at 0 and closes only the items it creates itself, so it must not abandon a call the client already saw — the model emits parallel calls (a `web_search` beside an `Agent`) and opens every item before closing any. The takeover therefore waits for the upstream to close a visible call, tracks what was delivered through `deliver_client_chunk` rather than the router's own pending map, and closes a still-open call with the arguments the upstream actually streamed. Closing it early is equally wrong: the search call's own `arguments.done` is the trigger, so a sibling closed there would run with `{}`.
- A wrapper an upstream framed as plain text is relayed, not produced here — strip it at delivery, and never let a path reference or a size reduction become the model's only route to an image (a durable copy beside the original, the inline preview when no copy can be written).
- Deterministic failures classify as themselves: `413`, `thinking_signature_invalid`, a context-size rejection, and a thinking-configuration rejection never enter a cooldown or the long recovery poll, and a missing upstream status stays absent instead of becoming a 502. A reasoning-configuration rejection is per-deployment route evidence and replays once in a compatible shape.
- A tool call's arguments are accumulated under every identity key the call declares. An item states both `id` and `call_id` while the argument events name only one, so one call's keys form one alias group, and a value is recorded under the whole group; a value that no parser accepts states nothing and never overwrites or shadows the valid value the stream already delivered to the client. An unreadable tool call is never a reason to end the turn: an output limit truncates a *prefix* of a JSON object, so the containers the fragment genuinely opened are closed (`_codex_closed_function_arguments`) and the event is delivered as the upstream wrote it, while a fragment cut inside a string or on a value-less separator is left untouched because closing that would invent a value the model never wrote. The cause is traced (`responses_stream_unreadable_tool_call_arguments`) instead of synthesized into a `response.failed`, which the Codex client treats as fatal and which once killed turns that were still being written.
- A cooldown may remove a failed route only when another eligible peer exists, and it must not erase the only configured deployment from a fresh request.
- Encrypted history is an immutable replay prefix: never recompress, re-encode, reorder, drop, or path-rewrite a signed item, and never inject `truncation=auto` because the prefix is large.
- Idle energy is a defect: patch uvicorn's 0.1 s tick to 1 s, space the supervisor's health ping, and cut each poll's cost before its interval — parse less, never poll less often or drop rows.
- Fabric recycles native views together with their props: a table or editor holding bulk rows, text, or caches must implement `prepareForRecycle`.

### Core, IPC, And Tests

- A wire value is untrusted: guard every set/dict membership with `isinstance(..., str)`, never key a fallback map by `id(exception)`, prune unreadable state timestamps instead of raising on them, and copy `reasoning_effort` into every failover rebuild.
- The pending-row registry evicts the least recently touched entry, and a stale in-flight row is settled with its own reason instead of rewritten or invented.
- Windows code compiles only against the macOS mirror: the handler map is `event_handlers_`, a subscribe request is recorded before the generation check, a failed poll sleeps like macOS's, and session expiry takes its subscriptions with it.
- Every key in `NativeControls`' `nativeProps` must appear in the macOS wrapper's parameter list — that wrapper forwards props by hand, so a prop added on one host and forgotten on the other arrives empty.
- `tests/` owns every test file; the hook's stub `litellm` answers unknown attributes from the installed package, and the runner exports the interpreter it selected (`LITELLM_TEST_PYTHON`) so subprocess probes launch the same one.

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
- Prefer `rg` for searches, and run the focused TypeScript and Python checks that exercise the changed behavior from the configured project runtime. `scripts/test.sh`, `scripts/check-rn.sh`, a full build, and a release package are not ordinary approval gates.
- For desktop UI work a build, launch, deep link, or accessibility read is **not** visual verification: capture the target window and inspect the image, or report it unverified. Drive native controls through computer use — `find_roots`, then `observe_ui({ mode: "visual" })` for the image, then `act_ui` to act — because a `semantic`/`fused` observation can fold to an outline-only look, where a `click` target is refused (`Coordinates require an image-bearing root`) and only the semantic actions remain. An `act_ui` coordinate is in the *captured image's* pixels, not the window's points, so a point computed from an accessibility rect is off by the capture scale. Only a proven failure to enumerate or control the target justifies a narrowly scoped `osascript` fallback, and then read-only.
- A functionality request is unfinished until that functionality has been exercised end to end in the session that claims it, with the evidence in hand — the flow driven, the command's real output, the window captured and looked at — and the evidence belongs to the request's own scope rather than to whatever else the tree happens to contain. Whatever could not be exercised is reported as unverified and named, never assumed to work.
- Deployment follows the code, not the commit: a change to something the bundle ships (`young_router/`, `rn/`, a staged integration) ends with its focused checks and then `./scripts/build-and-install-macos.sh`, which owns the graceful restart. A change that ships nothing — `AGENTS.md`, `README.md`, `tests/`, a staging script — is not a deploy: no build, and no restart of a running app. Never rebuild or restart to "make sure" once nothing differs. Never stop, kill, or start the app outside that script, and never split a stop and a start across separate operations. If the mandatory PyPI sync fails on DNS or TLS, retry the same deploy in a bounded loop (with `HTTPS_PROXY`/`HTTP_PROXY` when a local proxy is needed); never bypass it.
- Decide that mechanically, never from the commit message: `diff -rq --exclude=__pycache__ young_router "/Applications/Young Router.app/Contents/Resources/Core/young_router"` reports only the four generated adapter trees (`pi-web-access`, `veridrop`, `workbuddy-connect`, `dsh-vision-router`) when the installed build already carries the tree — any `Files … differ` line is a real deploy. The RN side is judged the same way before its build, and its minified bundle proves nothing afterwards, so confirm a UI change through the DerivedData object or the contract test.
- Build Windows only on a Windows host (`pnpm run build:windows`); `./scripts/package-release.sh` for a release archive. `scripts/version.py` owns `VERSION`, `BUILD_NUMBER`, and every platform's version metadata — do not add obsolete metadata back.
- A managed client catalog is generated, never hand-edited: an unknown enum value rejects the whole catalog, so verify every field against the installed client binary and regenerate through the same registry construction the Core uses.
- A managed catalog pointer must never dangle — Codex rejects its entire configuration while `model_catalog_json` names a missing file, so a deleted managed file is rebuilt, a historical spelling is migrated, and a foreign path is left alone.

## Development Efficiency Notes

- Fast loop for the string-based UI and bridge contracts (they run in well under a second):

  ```bash
  cd /path/to/young-router && PYTHONPATH=.:tests \
    "/Applications/Young Router.app/Contents/Resources/Core/runtime/bin/python" \
    -m unittest tests.test_rn_ui_parity tests.test_rn_native_acceptance -v
  ```

  When a change intentionally alters UI markup or a native bridge surface, update the matching contract expectations in the same commit — a contract failure with no expectation update is the fast signal that a boundary drifted.
- Gate a native build with the fast checks first: `pnpm run typecheck` in `rn/`, then the two contract suites, then the focused Core module for the touched domain. A full `unittest discover` run has environment-dependent failures; attribute one to your change only after it reproduces in isolation and touches a file you modified.
- Deployment takes minutes: run it with `nohup … > /tmp/lm_build.log 2>&1 &` and poll the log. Then prove the installed bundle actually carries the change — the JS bundle is minified, so grepping for source identifiers proves nothing; confirm via the DerivedData object file or the contract test, check the process group's start times, the `--workers 16` command line, and `/health/liveliness`.
- A reused CocoaPods workspace fails the *native* build when its codegen output is older than the checkout: xcodebuild reports `Build input file cannot be found: …/build/generated/ios/ReactCodegen/…` for files the `pod install` never wrote, while the same files are present on disk *after* the run because the script phase regenerated them behind the failing compile. That is not a source error — re-run the same deploy with `YOUNG_ROUTER_REFRESH_PODS=1`, which regenerates the generated tree and the matching `Podfile.lock` codegen checksums.
- Another session or the user may have rebuilt the app at any time. Before debugging a running app, re-check the installed binary's mtime and the process start time; otherwise you are reproducing against a stale binary.
- `NSLog` does not appear in `log stream` for the Release build. Trace natively to a temporary file (and remove it before the final build); `String.appendLine(to:)` does not exist in this runtime — write the accumulated message in one call.
- Reproduce UI flows through the agent harness's accessibility tree and inspect the captured window image; deep links open a route directly (`open "young-router://open/<route>"`). Take a fresh observation after any build, reopen, or sheet transition, and never reuse element numbers across one.
- A `NativeTable` column width is a promise that its cell text fits: measure the worst case in the host face (`NSFont.systemFont(ofSize: 13)` plus the cell padding on each side) instead of guessing. A fixed `labelWidth` narrower than the rendered CJK label wraps mid-word; prefer `width: undefined` with `numberOfLines={1}`.
- A new native prop starts in `ui/nativeSpecs/<Control>.spec.ts`, then `pnpm run specs:generate` renders both platform components, and the change must reach `NativeControls.tsx`, the hand-forwarding `AppKitControls.tsx` wrapper, `updateProps` equality in `AppKitControlViews.mm`, and `ApplyProps` equality in `WinUIControls.cpp`. Missing an equality check applies the prop once and never again; missing a platform render silently no-ops there.
- New native bridge methods with scalar parameters are positional at the native boundary: `platformEntry.ts` spreads an options object into positional args. Passing the object itself crashes at runtime while typecheck stays green — exercise the real flow once before declaring done.
- Diagnose Codex tool-surface questions from recorded data (`recent-requests.jsonl`, the rollout files, `litellm_route_trace` events), not by reading code. `codex exec` is not Desktop-equivalent: Desktop attaches extra namespaces, so diff the per-request tool lists.
- Bridged tool-schema repairs belong in `_responses_chat_bridge_tool_schema`, the single choke point every bridged parameter schema passes through; Desktop turns always carry namespaces, so they are always bridged.
- A/B a proxy fix against the real upstream on a spare port (repo code + bundled Python + the app's runtime config), then stop the process by port. Replayed requests need a Codex client marker (`Originator: codex_desktop`) or the bridge paths are silently skipped.
- macOS specifics worth not relearning: `NSControl.target` and table delegates are weak; a button with `keyEquivalent = "\r"` is painted with the accent fill (`NeutralDefaultButton` keeps Return neutral); a React `Pressable` row is not AX-activatable in this Fabric build, so a row that must be clickable, keyboard-navigable, and verifiable belongs in a `NativeTable`; option sets take `[]` and `accessibilityLabel` is a method, and `swiftc -parse` catches only syntax.
- The settings window owns one sidebar material (`NSVisualEffectView`, a transparent title bar, a cleared `RCTSurfaceHostingView`); source-list tables must stay transparent or the area double-blends. Dialogs rendered inside the provider inspector are clipped, so route-level dialogs belong to the route surface.
- System Events does not enumerate sheets as windows and its `window 1` is unreliable; verify sheets and their controls through the accessibility tree only. Windows native code cannot be compiled locally: declare C++ helper lambdas after the locals they capture and keep the WinUI structure a literal mirror of the macOS one.
- Requests-log rendering: the 服务 tab is a projected event stream (one row per event, bursts folded, records two workers glued together split apart), and the cheap strip-and-reject checks must stay ahead of redaction. Never gate a pane's scroll indicator on a JS content measurement, and keep every fixed log token in i18n.
- The working tree is shared: check `git status` and `git stash list` before starting and never revert another session's edits. Run focused commands from the repository root, never pipe failing output through `| tail`, and never re-run an unchanged failing command expecting a different result.

## Public Repository Hygiene

- Do not commit runtime directories, virtual environments, package installs, generated bundles, logs, screenshots containing real data, local traces, or WebDAV settings. The same things do not belong in the working tree either.
- Before every commit or push, inspect untracked files and the staged diff for credentials, private endpoints, provider/model names, request or task IDs, local paths, and copied configuration; replace real values with neutral fixtures.
- Re-run that check against the staged diff before pushing. Passing tests does not replace this review.
- Use the intended public or noreply Git identity for public remotes.
