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
- Preserve LiteLLM routing, Responses stream semantics, tool ordering, IDs, explicit terminal errors, and metadata-driven compatibility bridges. Do not add request-specific or provider-specific routing hacks.
- The Core IPC schema beside the implementation is the durable contract. Update its validators, typed client, host bridges, and focused tests together when the contract changes.
- The macOS Vision helper is built into `Contents/Resources/Core/bin/vision_ocr`. Keep its source in the RN macOS host and never restore a `Resources/App` lookup.

## Development And Verification

- Prefer `rg` for source searches. Run Python tests through:

  ```bash
  ./scripts/test.sh
  ```

- For a focused test, pass a unittest module or test name to the same script. Run `./scripts/check-rn.sh` after shared UI, IPC, or native host changes.
- Install and build the shared workspace from `rn/` with `pnpm install --frozen-lockfile` and `pnpm run build`.
- Build macOS with Xcode beta and an isolated output path; do not overwrite `/Applications/LiteLLM Menu.app` during routine verification:

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
