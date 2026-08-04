# LiteLLM Menu Repository Guide

## Scope

- Treat `litellm-menu` as a standalone repository. Do not inspect parent directories for repository instructions.
- Keep provider names, models, API keys, request IDs, local paths, traces, and logs out of committed files. Public examples use only neutral synthetic values.
- `config.example.yaml` is the public configuration example and must remain safe to publish.

## Source Of Truth

- `rn/packages/shared/` owns shared React/TypeScript routes, composition, interaction state, i18n, and the typed Core IPC client.
- `litellm_menu/core/` owns domain state, validation, staged configuration, service operations, persistence, and the authenticated versioned loopback IPC contract.
- `rn/apps/macos/` is the AppKit host and `rn/apps/windows/` is the WinUI 3 host. Native code owns platform leaves: status/tray menus, windows, native controls, secure editors, file panels, alerts, shortcuts, and platform lifecycle.
- One feature has one shared UI and one Core domain implementation. Do not introduce a second platform-specific domain store, configuration writer, WebView, Electron/Tauri layer, or legacy shell launcher.
- React holds view state only. It must not write configuration files, manage the proxy process, read arbitrary paths, or handle raw credentials.
- The language choice belongs in each host's native application menu. Shared UI consumes that preference; do not add an in-window language selector.
- All user-visible shared strings go through i18n. Keep platform titles and menu labels aligned with the selected language.

## UI And Native Boundaries

- Preserve native platform behavior and visual conventions. Shared UI describes the workflow; native leaves render controls that require AppKit or WinUI behavior.
- Use typed IPC snapshots and actions. Never expose a path, secret, raw editor value, or unrestricted native capability through ordinary React props.
- Secure editors and file panels use opaque one-time native capabilities. Do not serialize secret values into IPC snapshots, logs, drafts, or test fixtures.
- Keep configuration changes staged and explicit. Validation may update draft state, but writing configuration and restarting a service require an explicit Apply action.

## Runtime And Compatibility

- The runtime starts from `.litellm-runtime/config.yaml`; source configuration is validated and staged explicitly. No file mutation may silently restart the service.
- The macOS LiteLLM proxy must run with `LITELLM_NUM_WORKERS=16`. Do not lower the default, alter it during a restart, or use a smaller worker count as a performance or recovery workaround unless the user explicitly requests that configuration change.
- Preserve LiteLLM routing, Responses stream semantics, tool ordering, IDs, explicit terminal errors, and metadata-driven compatibility bridges. Do not add request-specific or provider-specific routing hacks.
- The Core IPC schema beside the implementation is the durable contract. Update its validators, typed client, host bridges, and focused tests together when the contract changes.
- The macOS Vision helper is built into `Contents/Resources/Core/bin/vision_ocr`. Keep its source in the RN macOS host and never restore a `Resources/App` lookup.

## Known Runtime Failure Modes

- Treat encrypted Responses history as an immutable replay prefix. Preserve genuine `encrypted_content`, compaction items, IDs, ordering, and any image bytes inside that signed history exactly; do not recompress, re-encode, normalize, or otherwise rewrite them. Compress newly added oversized images before they enter encrypted history, and when replaying mixed history limit image compression to the suffix after the latest encrypted item.
- An `encrypted_content` field containing obvious natural-language plaintext is malformed client history, not valid ciphertext. Repair only that narrow field shape back to ordinary text; do not decode, regenerate, or broadly rewrite opaque encrypted values.
- Treat signature/decryption failures such as `thinking_signature_invalid` as deterministic request errors. Return the explicit client error immediately; never send them into route cooldown or the long recovery polling loop.
- Do not infer that gateways are interchangeable merely because they may ultimately target the same upstream API. Diagnose the exact request, route, account, response event, and terminal status while keeping provider-specific workarounds out of the implementation.
- A copied, signed, or source-identical app bundle does not prove the running proxy loaded the new code. Any activation claim must show a newly started app/Core/proxy process, a healthy bound endpoint, and the unchanged configured worker count. The proxy process and its listening port are one lifecycle; do not describe a dead proxy as leaving an independently live service port.
- A stale desktop task status such as an earlier `systemError` is not evidence that the latest retry failed. Correlate the latest rollout turn with the proxy request record. A remote compaction is proven only by a successful request for the same task whose response contains a `compaction` item; a later successful ordinary continuation is not a substitute for that evidence.
- Keep image-size handling explicit: the service must still compress new oversized images to an accepted representation. Preserving signed historical images must not become a general exemption that allows new oversized images through unchanged.

## Development And Verification

- Prefer `rg` for source searches. Run the focused TypeScript and Python checks that exercise the changed behavior directly from the configured project runtime.
- Do not make `scripts/test.sh`, `scripts/check-rn.sh`, a full build, or a release package an ordinary approval gate. Those scripts may perform broader packaging or state checks that are not needed for a scoped change.
- For desktop UI work, a successful build, process launch, deep link, or accessibility-tree read does **not** count as visual verification. Call a UI state visually verified only after capturing the target app window and inspecting the rendered image.
- After every completed behavior change, run its focused checks. Deploy with `./scripts/build-and-install-macos.sh` when a change needs to be installed. The script builds in a temporary staging directory, gracefully quits the running LiteLLM Menu after staging is verified, replaces `/Applications/LiteLLM Menu.app`, and starts the installed app. This routine restart is part of deployment and does not require separate approval.
- Never stop, kill, or terminate the installed app, Core, proxy, or workers with a standalone command. Before the first termination signal, arm and include the matching app relaunch in the same shell or script operation so an interrupted operation still executes the restart; do not split stop and start across tool calls or leave the user to restart manually. Brief deployment downtime is acceptable. Never reduce the configured worker count as a recovery tactic; keep the current 16-worker setting during restarts.
- For macOS desktop UI verification, prefer a persistent `node_repl` Computer Use session with `@oai/sky`: obtain app state, capture a screenshot, and perform each click or keystroke as a short bounded call. When that target cannot be enumerated, use the in-app Browser connector or a Playwright browser only for web surfaces; do not claim those connectors exercise native AppKit controls. Browser, Chrome, and MCP facade backends require their matching injected client bridge and are not standalone desktop-control fallbacks.
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
