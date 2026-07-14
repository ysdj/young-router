# Repository Instructions

These instructions apply to the entire `litellm-menu` repository.

## Scope

- Treat this repository as a standalone project. Do not inspect parent directories for additional instructions.
- Keep provider names, model names, API keys, request ids, local paths, and debug traces out of committed files unless they are clearly synthetic examples.
- Use `config.example.yaml` for public examples. It must stay generic and safe to publish.

## Development Workflow

- Prefer `rg` for file and text searches.
- Run tests through the repository entrypoint:

  ```bash
  ./scripts/test.sh
  ```

- For targeted tests, pass unittest module or test names to the same script.
- Build the macOS app bundle after changing Swift sources, Python service code,
  service scripts, runtime settings, or any bundled app resources. For installed
  menu app behavior, the completion path is: build the app, restart the real
  menu-owned service, then verify on the normal local service. Checkout-only
  tests and isolated services can help diagnose issues, but they do not prove
  the installed app is running the change.

  ```bash
  ./mac_menu/build.sh
  ```

- For macOS app or native UI changes, `swiftc -typecheck`, a temporary preview
  app, or a build using an alternate `LITELLM_APP_PATH` is supplemental
  verification only. Unless the user explicitly requests a non-installed
  artifact, completion requires running `./mac_menu/build.sh` with its default
  `/Applications/LiteLLM Menu.app` target, verifying the installed bundle and
  signature, restarting the real app, confirming a new app PID, and checking
  the menu-owned service on its normal local endpoint. If any required step is
  skipped or fails, report the task as incomplete; do not say it was built,
  deployed, executed, or completed.

- Keep `VERSION`, `BUILD_NUMBER`, `mac_menu/Info.plist`, and `Formula/litellm-menu.rb` version handling in sync through `scripts/version.py` when version changes are requested.

## Codex / Responses Completion Gate

- For Codex/Responses tool regressions, final proof requires a fresh
  `codex exec` through the Menu-configured local proxy; `resume`, UI `continue`,
  unit tests, and hand-written HTTP probes are not substitutes.
- The CLI must execute real command and namespace/MCP tools, and sanitized
  trace must confirm the tool types survived routing without duplicate calls.
- `No connected db` from an ad-hoc CLI usually means the local endpoint or
  master-key auth was not applied. Fix configuration before diagnosing tools.
- Completion requires targeted and full tests, app rebuild, menu-owned service
  restart/health, source-to-bundle match, and the fresh CLI check. Invocation or
  orchestration errors do not count; rerun once and use one waiter per process.

## Codex Compaction And Recovery

- Diagnose compaction stalls from protocol events, not elapsed time alone.
  Repeated explicit upstream 5xx responses are routing/deployment failures, not
  first-event timeouts, and raising a timeout must not be used to mask them.
- Bound structured compaction history by both text size and protocol item count.
  Preserve developer/system state and compaction markers, keep every tool call
  paired with its output, and never truncate encrypted compaction content.
- A complete, non-empty encrypted compaction output item is valid recovery
  evidence when a compatible upstream closes the stream without the final
  `response.completed`. Synthesize that terminal event only for this narrowly
  identified compaction case; never hide an explicit `response.failed`,
  `response.incomplete`, or `error` event.
- Do not treat `response.created` or mere stream establishment as deployment
  success. Clear failure cooldown only after a genuinely completed stream;
  otherwise recovery can retry the same failing deployment indefinitely.
- Keep ordinary first-event, compaction first-event, post-first-event idle, and
  total recovery limits independently configurable and exposed consistently in
  Runtime Settings. Compaction recovery may use the full recovery window, while
  ordinary turns retain their bounded retry window.
- A compaction/recovery fix is not complete until the installed launcher can
  resume a representative large local task, finish compaction, and proceed to a
  normal response or tool call. Keep the task id, provider, host, request ids,
  and trace content out of committed files.

## Native UI Screenshots

- Computer Use can inspect LiteLLM Menu when a normal editor or settings `NSWindow` is open. A menu-bar-only process exposes only `NSStatusItem` / `NSMenu`; `get_app_state` may time out in that state even when the app is healthy. Retry once by bundle id (`menu.litellm.menu`), then open the target window before treating the timeout as an app defect. Do not add a persistent or hidden window solely to satisfy screenshot tooling.
- Never use a real configuration window for public screenshots. Run an isolated, ad-hoc-signed preview `.app` with a synthetic `config.yaml` containing only reserved example hosts, neutral provider/model names, and replace-me keys.
- For a Computer Use capture, open the target normal window, address the app by name or full `.app` path, call `get_app_state`, and copy the returned single-window `screenshot.url`. A temporary preview should have a stable unique bundle id and be launched as a signed `.app`; an unregistered raw executable is not a reliable target.
- Capture only the intended window. `/usr/sbin/screencapture -x -l <window-number> <output.png>` is an acceptable fallback when the capturing process already has Screen Recording permission, but an ad-hoc preview app may not. Do not capture the desktop, menu bar, other apps, logs, or real credentials. For multi-state documentation images, capture each window state separately and compose those sanitized window images afterward.

## Architecture Rules

- LiteLLM Menu is app-first and native on macOS. The menu app owns the local service lifecycle.
- Runtime routes come from staged `.litellm-runtime/config.yaml`, not from LiteLLM DB model-management APIs.
- Keep LiteLLM routing and callback behavior in Python modules under `litellm_menu/`; do not add a sidecar proxy or JavaScript hook path for default behavior.
- Keep `litellm_menu/__init__.py` side-effect free. Callback object creation belongs in `litellm_menu/callbacks.py`.
- Route image-generation tool requests by structured request fields and deployment metadata, especially `model_info.supports_responses_image_generation_tool`, not by prompt wording or provider names.
- Preserve Responses stream semantics. Once assistant text has been emitted to the client, do not hide a later upstream error by synthesizing a successful completion.
- Preserve the client-facing native Responses protocol. Codex must not be asked
  to switch to a LiteLLM-specific API to work around proxy compatibility; fix
  the translation or passthrough in the proxy instead.
- Keep compatibility bridges generic: web search, vision fallback, image-generation fallback, and Responses tool mapping must not contain request-specific or provider-specific hacks.

## Public Repository Hygiene

- Do not commit `config.yaml`, `.litellm-runtime/`, `.venv/`, logs, local route traces, WebDAV settings, thread-watch artifacts, or copied runtime directories.
- Public examples should use `example.test`, `example.com`, `primary`, `backup`, `image`, `default-chat`, `fast-chat`, `image-chat`, and similar neutral placeholders.
- Before release-oriented changes, run a sensitive-string scan for private provider names, API hosts, local user paths, request/thread ids, and domain-specific fixture text.
- Before every commit, tag, or push, inspect untracked files and the exact staged diff for API keys or tokens, private provider/model names, private hosts, request/thread ids, local paths, logs, traces, and copied configuration values. Replace every real value with a neutral synthetic fixture before proceeding.
- Re-run the sensitive-string scan against `git diff --cached` after staging and again against the committed range before pushing. A successful test run does not replace this privacy gate; do not upload while any unexplained match remains.
- For public remotes, verify author and committer metadata before committing and use the intended public or noreply identity instead of a private email address.

## Detailed Runbooks

Read `docs/agent-runbooks.md` before changing routing, Codex/Responses client tools or streaming, web search bridge, vision bridge, image generation fallback, macOS lifecycle, WebDAV sync, or release packaging.
