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

- Keep `VERSION`, `BUILD_NUMBER`, `mac_menu/Info.plist`, and `Formula/litellm-menu.rb` version handling in sync through `scripts/version.py` when version changes are requested.

## Architecture Rules

- LiteLLM Menu is app-first and native on macOS. The menu app owns the local service lifecycle.
- Runtime routes come from staged `.litellm-runtime/config.yaml`, not from LiteLLM DB model-management APIs.
- Keep LiteLLM routing and callback behavior in Python modules under `litellm_menu/`; do not add a sidecar proxy or JavaScript hook path for default behavior.
- Keep `litellm_menu/__init__.py` side-effect free. Callback object creation belongs in `litellm_menu/callbacks.py`.
- Route image-generation tool requests by structured request fields and deployment metadata, especially `model_info.supports_responses_image_generation_tool`, not by prompt wording or provider names.
- Preserve Responses stream semantics. Once assistant text has been emitted to the client, do not hide a later upstream error by synthesizing a successful completion.
- Keep compatibility bridges generic: web search, vision fallback, image-generation fallback, and Responses tool mapping must not contain request-specific or provider-specific hacks.

## Public Repository Hygiene

- Do not commit `config.yaml`, `.litellm-runtime/`, `.venv/`, logs, local route traces, WebDAV settings, thread-watch artifacts, or copied runtime directories.
- Public examples should use `example.test`, `example.com`, `primary`, `backup`, `image`, `default-chat`, `fast-chat`, `image-chat`, and similar neutral placeholders.
- Before release-oriented changes, run a sensitive-string scan for private provider names, API hosts, local user paths, request/thread ids, and domain-specific fixture text.

## Detailed Runbooks

Read `docs/agent-runbooks.md` before changing routing, Responses streaming, web search bridge, vision bridge, image generation fallback, macOS lifecycle, WebDAV sync, or release packaging.
