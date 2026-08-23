# LiteLLM Menu Repository Guide

## Scope

- Treat `litellm-menu` as a standalone repository. Do not inspect parent directories for repository instructions.
- Keep provider names, models, API keys, request IDs, local paths, traces, and logs out of committed files. Public examples use only neutral synthetic values.
- `config.example.yaml` is the public configuration example and must remain safe to publish.

## Source Of Truth

- `rn/packages/shared/` owns shared React/TypeScript routes, composition, interaction state, i18n, and the typed Core IPC client.
- `litellm_menu/core/` owns domain state, validation, staged configuration, service operations, persistence, and the authenticated versioned loopback IPC contract.
- `rn/apps/macos/` is the AppKit host and `rn/apps/windows/` is the WinUI 3 host. Native code owns platform leaves: status/tray menus, windows, native controls, system WebView hosts for shared code panes, secure inputs, file panels, alerts, shortcuts, and platform lifecycle.
- One feature has one shared UI and one Core domain implementation. Do not introduce a second platform-specific domain store, configuration writer, WebView application shell, Electron/Tauri layer, or legacy shell launcher. Embedded system WebViews are permitted only for the shared code panes.
- React holds view state only. It must not write configuration files, manage the proxy process, read arbitrary paths, or handle raw credentials. Versioned code-editor document text is the explicit exception.
- The language choice belongs in each host's native application menu. Shared UI consumes that preference; do not add an in-window language selector.
- All user-visible shared strings go through i18n. Keep platform titles and menu labels aligned with the selected language.

## UI And Native Boundaries

- Preserve native platform behavior and visual conventions. Shared UI describes the workflow; native leaves render controls that require AppKit or WinUI behavior.
- Default every desktop surface to compact native density. Avoid redundant in-content titles, oversized empty cards, web-like vertical whitespace, and detached action rows; prefer tightly grouped 24–28 px controls, concise helper text, content-sized route windows, and one fixed bottom action bar. Tabs must be genuinely switchable and render only their active pane. Compact layouts must still reflow before controls overlap or hide required content on either macOS or Windows.
- A route window's bottom-right action bar contains only `Close` and a conditional `Apply` when staged configuration is dirty. File selection, import/export, WebDAV testing, and synchronization actions belong in the active pane, never in that footer.
- Use typed IPC snapshots and actions. Never expose a path, secret, or unrestricted native capability through ordinary React props. Versioned code-editor document text may flow through the shared code-editor view.
- Route windows must render their first data-backed frame from the shared snapshot; do not paint empty shells and refill them or issue duplicate per-window snapshot/subscription requests.
- Secure inputs and file panels use opaque one-time native capabilities. Do not serialize secret values into IPC snapshots, logs, drafts, or test fixtures. Raw configuration documents use the authenticated versioned editor IPC instead of the retired native-only editor path.
- Keep configuration changes staged and explicit. Validation may update draft state, but writing configuration and restarting a service require an explicit Apply action.

## Runtime And Compatibility

- The runtime starts from `.litellm-runtime/config.yaml`; source configuration is validated and staged explicitly. No file mutation may silently restart the service.
- Keep proxy data-plane headers transparent. Outside an explicitly scoped compatibility hook or runtime patch, do not add, remove, rename, rewrite, normalize, mask, or impersonate request or response headers. Preserve downstream-supplied headers byte-for-byte when forwarding them upstream, including headers or values that identify LiteLLM; do not invent upstream identity headers or strip LiteLLM/SDK fingerprints merely to conceal the implementation. Every header exception must stay narrowly tied to its documented protocol or failure mode and have a focused regression test.
- Every artifact-producing desktop build must update `LITELLM_VERSION` from the latest installable stable LiteLLM release on PyPI before selecting or reusing a bundled runtime and before invoking the platform build. This applies to direct macOS and Windows builds, macOS build-and-install, and release packaging. If the update cannot complete, fail the build; never silently package a stale lock. A nested build may skip only a duplicate lookup already completed successfully by the same top-level build invocation.
- The macOS LiteLLM proxy must run with `LITELLM_NUM_WORKERS=16`. Do not lower the default, alter it during a restart, or use a smaller worker count as a performance or recovery workaround unless the user explicitly requests that configuration change.
- Every newly created proxy process must start with empty route-recovery and deployment-cooldown runtime state. Never carry `.litellm-runtime/route-recovery-state.json` or `.litellm-runtime/deployment-cooldowns.json` across app starts, service starts, restarts, or reloads.
- Preserve LiteLLM routing, Responses stream semantics, tool ordering, IDs, explicit terminal errors, and metadata-driven compatibility bridges. Do not add request-specific or provider-specific routing hacks.
- The Core IPC schema beside the implementation is the durable contract. Update its validators, typed client, host bridges, and focused tests together when the contract changes.
- The macOS Vision helper is built into `Contents/Resources/Core/bin/vision_ocr`. Keep its source in the RN macOS host and never restore a `Resources/App` lookup.

## Known Runtime Failure Modes

### Observed macOS Energy-Use Incident (2026-08-19)

- The macOS Battery panel displayed LiteLLM Menu under “使用大量能耗” while the menu application was idle.
- While a settings route was mounted, `rn/packages/shared/src/ui/LiteLLMMenuApp.tsx` invoked `ipc.snapshot()` from a five-second disk-change timer.
- `CoreStore.snapshot()` invoked `_refresh_external_disk_state()` across every registered domain, including domains unrelated to the visible settings route.
- The pre-fix Codex disk detector invoked `_ensure_model_catalog_current()` and `_load_editor()`; `codex_config.load_editor()` queried the authenticated local `/v1/models` endpoint while constructing its editor payload.
- The resulting timer call chain was: settings timer → full Core snapshot → all-domain disk probes → Codex editor/catalog load → local proxy `/v1/models` request, including cycles in which no settings file had changed.
- The implemented fix adds the `disk_state` IPC method, has the settings timer request only its monitored domains, and requests a full snapshot only after a disk marker differs. The Codex disk detector now reads `config.toml` and `auth.json` directly for marker comparison and does not perform the endpoint/catalog query on that path.
- Focused Core and IPC tests, the targeted React Native parity tests, TypeScript type checking, and the IPC contract check passed after the change.
- The installed replacement process group consisted of a newly started App, Core, and proxy; the proxy health endpoint returned HTTP 200 and the proxy command retained `--workers 16`.
- Idle samples after installation showed the AppKit event loop, Core IPC polling, and proxy worker processes in wait states; CPU spikes observed during the investigation coincided with active model requests rather than the idle disk-marker poll.

- React Native Fabric may keep an unmounted native component in its recycle pool together with its last large props. Native tables, editors, or lists that hold bulk rows/text or derived caches must implement `prepareForRecycle` that resets `_props` to defaults and clears their AppKit backing data, columns, selection, and caches; removing the React element alone does not release that memory.
- Do not render unbounded log/trace collections with `ScrollView` plus `map`, duplicate every table cell into a data signature, or measure every cell on ordinary table updates. Virtualize long lists, memoize derived rows/columns and native props, compare old/new props directly, and reserve full content-width scans for controls whose contract requires them.
- For log-tab memory fixes, verify the installed app by repeatedly switching populated tabs and checking that RSS settles or falls after warm-up. A successful render or a single before/after sample does not disprove a retention leak.
- Treat encrypted Responses history as an immutable replay prefix. Preserve genuine `encrypted_content`, compaction items, IDs, ordering, and any image bytes inside that signed history exactly; do not recompress, re-encode, normalize, or otherwise rewrite them. Compress newly added oversized images before they enter encrypted history, and when replaying mixed history limit image compression to the suffix after the latest encrypted item.
- Do not inject `truncation=auto` or another request-control field merely because inline images in the encrypted replay prefix exceed a local image budget. Responses-compatible gateways may reject that parameter, after which route failover and stream recovery can masquerade as a long hang. Image-byte thresholds are diagnostic only; add truncation solely through the existing explicit context-size-error fallback, never preemptively from signed-prefix size, upstream 5xx, or stream timeouts.
- Large signed image history may remain in the same task while new turns are appended. To make the replay prefix smaller, accept a genuine upstream `compaction` item or start a new task; never synthesize, forge, drop, reorder, recompress, or path-rewrite signed items. Path-backed previews and image compression apply only before signing or in the mutable suffix after the latest encrypted item.
- An `encrypted_content` field containing obvious natural-language plaintext is malformed client history, not valid ciphertext. Repair only that narrow field shape back to ordinary text; do not decode, regenerate, or broadly rewrite opaque encrypted values.
- Treat signature/decryption failures such as `thinking_signature_invalid` as deterministic request errors. Return the explicit client error immediately; never send them into route cooldown or the long recovery polling loop.
- Do not infer that gateways are interchangeable merely because they may ultimately target the same upstream API. Diagnose the exact request, route, account, response event, and terminal status while keeping provider-specific workarounds out of the implementation.
- A copied, signed, or source-identical app bundle does not prove the running proxy loaded the new code. Any activation claim must show a newly started app/Core/proxy process, a healthy bound endpoint, and the unchanged configured worker count. The proxy process and its listening port are one lifecycle; do not describe a dead proxy as leaving an independently live service port.
- The native status row is an informational service-state display, not the app title or an enable/disable control. During bootstrap it must say an explicit starting state, and after localization or a Core error it must project the real running, unhealthy, stopped, or unknown state. Never leave a generic app name in that row and call it a runtime status.
- A restart is proven only when the old App/Core/proxy process group has exited and replacement processes have later start times, then the replacement proxy passes its health endpoint. A transient health-check failure while workers are booting is an intermediate startup state; only the configured stable-check threshold may produce the final running state.
- A stale desktop task status such as an earlier `systemError` is not evidence that the latest retry failed. Correlate the latest rollout turn with the proxy request record. A remote compaction is proven only by a successful request for the same task whose response contains a `compaction` item; a later successful ordinary continuation is not a substitute for that evidence.
- Do not infer a request-size limit from a structured compaction transport error or upstream 5xx. Require an explicit context-size rejection, preserve the exact upstream status instead of rewriting it to 502, and when a stream terminates without any upstream HTTP status leave the status absent rather than inventing 502. Treat prior successful compactions at comparable token counts as counterevidence to a size diagnosis.
- Deployment cooldown may remove a failed route when another eligible peer exists. It must not erase the only configured eligible deployment from a fresh request; route-recovery polls may still wait for cooldown expiry. Log pre-route failures as no available deployment or model not configured instead of the ambiguous `unselected` label.
- Keep image-size handling explicit: the service must still compress new oversized images to an accepted representation. Preserving signed historical images must not become a general exemption that allows new oversized images through unchanged.

## Codex Task-ID Incident Triage

- Treat a supplied Codex task/thread ID as the primary lookup key. Do not begin with repository scans, source review, or broad log searches.
- Use this three-hop first pass, in order: (1) `read_thread` with only the newest 1-2 turns, (2) the exact rollout `task_complete` event, and (3) LiteLLM records for the same task ID and a narrow time window around that event. The first pass should return the parent status, latest-turn status/error, final assistant message, active child tasks, exact proxy request IDs, and upstream statuses.
- Prefer Codex task tools over filesystem inspection. If raw evidence is required, resolve the rollout path by exact task ID from the read-only Codex state database; never scan all of `~/.codex/sessions` or dump an entire rollout file.
- Query logs structurally and narrowly: filter by exact task ID, request ID, event name, and timestamp; print only timestamp, event, request ID, provider/deployment, status, and sanitized exception. Do not use unbounded `rg -C`, full log tails, or outputs containing request bodies, prompts, credentials, or API keys.
- Keep parent thread status, latest parent turn status, and child-task status separate. A child continuing after a parent transport failure is expected; a stale parent `systemError` with a completed latest turn is a task-state inconsistency, not evidence of a new project failure.
- Read source only after the exact error text or trace event identifies the owning branch. Stop investigating once the causal chain is closed, for example: task error -> proxy event -> upstream request statuses. Then make only the smallest requested fix and run its focused regression test.

## Development And Verification

- Keep all project-owned automated test source files directly under `tests/`; package scripts may invoke them from there, but do not scatter tests under source, script, `work/`, or `tmp/` directories.
- Do not retain repository-root `work/` or `tmp/` directories. Ephemeral probes and build scratch data must use a system temporary directory created for that run and remove it when the run finishes.
- Prefer `rg` for source searches. Run the focused TypeScript and Python checks that exercise the changed behavior directly from the configured project runtime.
- Do not make `scripts/test.sh`, `scripts/check-rn.sh`, a full build, or a release package an ordinary approval gate. Those scripts may perform broader packaging or state checks that are not needed for a scoped change.
- For desktop UI work, a successful build, process launch, deep link, or accessibility-tree read does **not** count as visual verification. Call a UI state visually verified only after capturing the target app window and inspecting the rendered image.
- After every completed behavior change, run its focused checks. Deploy with `./scripts/build-and-install-macos.sh` when a change needs to be installed. The script builds in a temporary staging directory, gracefully quits the running LiteLLM Menu after staging is verified, replaces `/Applications/LiteLLM Menu.app`, and starts the installed app. This routine restart is part of deployment and does not require separate approval.
- For any macOS UI or behavior change intended for the installed app, a passing test run is not completion: after focused checks, always run `./scripts/build-and-install-macos.sh` and require its staged-bundle validation, replacement, graceful App/Core/proxy restart, and readiness checks to succeed. Keep the task open if deployment fails; do not stop at tests or leave the old installed bundle running.
- Never stop, kill, or terminate the installed app, Core, proxy, or workers with a standalone command. Before the first termination signal, arm and include the matching app relaunch in the same shell or script operation so an interrupted operation still executes the restart; do not split stop and start across tool calls or leave the user to restart manually. Brief deployment downtime is acceptable. Never reduce the configured worker count as a recovery tactic; keep the current 16-worker setting during restarts.
- For macOS desktop UI verification, **begin with and use a persistent `node_repl` Computer Use session with `@oai/sky`**: obtain app state, capture a screenshot, and perform each click or keystroke as a short bounded call. Do not replace Computer Use with `osascript`, JXA, System Events, CGEvent synthesis, or coordinate shell commands for convenience. Only after a concrete Computer Use attempt proves that the target cannot be enumerated or controlled may a narrowly scoped `osascript`/System Events fallback be used, and then only for read-only inspection unless the user explicitly requests the UI action. When the target is a web surface, use the in-app Browser connector or Playwright; do not claim those connectors exercise native AppKit controls. Browser, Chrome, and MCP facade backends require their matching injected client bridge and are not standalone desktop-control fallbacks.
- A UI result is visually verified only after capturing and inspecting the target window image. If the target cannot be enumerated or the capture is blank/broken, report it as unverified and continue with static checks; do not infer a pass or failure from process state alone.
- Use `./scripts/build-and-install-macos.sh` for the normal macOS deployment above; it owns the graceful restart. For an explicitly requested preview that must not replace the installed app, build with Xcode beta and an isolated output path:

  ```bash
  cd rn
  DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
    LITELLM_MENU_MACOS_OUTPUT="$(mktemp -d)/LiteLLM Menu.app" \
    pnpm run build:macos
  ```

- Build Windows only on a Windows host with `pnpm run build:windows`. Run `./scripts/package-release.sh` for a release archive; it must prove the host, relocatable Core runtime, signing, and bundle layout.
- `scripts/version.py` synchronizes `VERSION`, `BUILD_NUMBER`, the RN macOS plist/Xcode project, Windows manifests, and the Cask. Do not add obsolete platform metadata back into version handling.

## Public Repository Hygiene

- Do not commit runtime directories, virtual environments, package installs, generated app bundles, logs, screenshots containing real data, local traces, or WebDAV settings.
- Before every commit or push, inspect untracked files and the staged diff for credentials, private endpoints, provider/model names, request or task IDs, local paths, and copied configuration. Replace real values with neutral fixtures.
- Re-run the sensitive-data check against the staged diff before pushing. Tests passing does not replace this review.
- Use the intended public or noreply Git identity for public remotes.
