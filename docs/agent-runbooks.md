# LiteLLM Menu Maintainer Runbooks

This document keeps lower-frequency operational notes out of `AGENTS.md`. Read the relevant section before touching one of these subsystems.

## Routing And Fallback

- Preserve LiteLLM-native routing. Model groups, deployments, provider keys, and route order belong in `config.yaml` and staged runtime config.
- Deployment fallback is ordered by configured deployment order and route metadata. Request-local exclusions and global cooldowns must stay separate.
- Temporary cooldown should be limited to deployment/provider failures. Do not count request-shape errors, context-window errors, unsupported capability errors, or normal rate-limit polling as hard deployment failures.
- Learned context-window caps are runtime metadata derived from upstream errors. They should narrow future requests for the same route without rewriting the user's source config.

## Responses Streaming

- Responses stream compatibility is client-visible protocol behavior, not just an upstream retry concern.
- Once assistant text has been yielded, preserve it and emit a terminal failure if the upstream later errors, stalls, or ends without a real completion.
- Recovery and synthesized completion are only acceptable before any assistant answer text has been delivered, or while only tool/search UI events have been emitted.
- Preserve item ids and tool call ids when bridging function, custom, `tool_search`, or `web_search` events. Do not deduplicate repeated tool calls by command text or arguments.
- Keep context handling split by request kind. Bound a structured Codex remote
  compaction request before its first upstream call; never hide a malformed
  compaction request behind a retry. For an ordinary native Responses turn,
  an upstream `context_length_exceeded` before visible output may replay the
  already selected deployment once with the Responses API's documented
  `truncation: auto`. Because compatible endpoints may accept that field but
  fail to implement it, the retry may emulate its oldest-first item eviction
  locally while preserving protected instructions and complete tool
  call/output pairs. Preserve an explicit `disabled` choice, do not modify the
  first ordinary-turn request, do not re-enter Router selection, and never
  loop or mark a context error as a deployment failure.

## Codex Responses Client Tools

- Keep Codex on the native Responses protocol; fix proxy passthrough or mapping
  instead of requiring a LiteLLM-specific client payload.
- Merge top-level `tools` with leading `additional_tools` before compatibility
  processing. Preserve order, remaining input, `tool_choice`,
  `parallel_tool_calls`, and raw unfamiliar tool definitions.
- Capability is three-state: `true` and unknown try native first; only `false`
  may pre-bridge. After a native attempt, bridge once only for a clear
  tool/schema incompatibility, never for auth, quota, policy, network, timeout,
  or an unrelated bad request.
- When bridging `custom`/`namespace` tools, default omitted
  `parallel_tool_calls` to `false`; preserve explicit values and all item/call
  IDs. Never deduplicate calls by tool name or arguments.
- Treat an expired `wait` cell and a mode-specific `request_user_input`
  rejection as call-local runtime failures, not evidence that the tool set was
  removed. When structured history proves either failure, keep the other tools
  intact, add a bounded recovery hint, and remove only the tool the client has
  explicitly reported unavailable.
- HTTP success is insufficient: verify that the client actually receives and
  executes tools, and distinguish native, pre-bridge, and fallback in trace.

### Required Codex Integration Verification

- Run targeted tests and the full suite via `./scripts/test.sh`; invocation
  errors are not test results.
- Build the app, restart the menu-owned service, check health, and rebuild again
  if source changed afterward.
- Hash the existing `~/.codex/config.toml` and `auth.json`, then start a fresh
  `codex exec` (not `resume`/`continue`) through the installed `codex-litellm`
  launcher. The launcher must use process-local provider overrides and
  command-backed auth; do not call the retired persistent config switch.
- Recheck both hashes after the run. A partial provider override can cause
  `No connected db` by sending the wrong key, while a changed hash means the
  isolation contract failed.
- Execute a command tool and, when available, a namespace/MCP/file tool. Match
  the fresh task in sanitized trace and confirm tools survived fallback and
  each expected call occurred once.

## Web Search Bridge

- Native hosted `web_search` should be attempted first when route metadata indicates support or support is unknown on a Responses-capable route.
- Use the external bridge when a route is chat-only, explicitly lacks hosted web search support, or returns an unsupported hosted-tool error.
- The bridge should expose focused search queries and source URLs, not internal pseudo-actions. Page reads may be attached as evidence excerpts.
- Keep query planning model-driven. Do not add one-off query rewrites for a particular prompt, date, location, or topic.
- External search uses DDGS and optional Jina Reader excerpts. Keep result count, page read count, region, backend list, and fetch timeout configurable through runtime settings.

## Vision Bridge

- Attempt the vision bridge only when the original request contains image input and the selected route fails with a recognizable image/vision unsupported error.
- `auto` mode tries the configured OpenAI-compatible vision endpoint first, then falls back to the bundled local Vision OCR helper.
- The bridge rewrites image input into textual visual context and retries the original route. It should not silently switch the user's model group to an unrelated chat route.

## Image Generation

- Responses `tools: [{"type":"image_generation"}]` is distinct from standalone image model routes such as `/v1/images/generations`.
- Filter or prefer deployments from structured tool presence and `model_info.supports_responses_image_generation_tool`.
- If a capable route returns an empty response or an explicit image-tool-unavailable refusal, retry the same model/model group and force only `tool_choice: {"type":"image_generation"}`.
- Keep inline image bounding generic. It exists to prevent oversized data URLs from breaking compatible providers.

## macOS App And Service

- LiteLLM Menu is a native app that owns the local service lifecycle. The default path should not require Docker, a database, or a system Python install.
- Homebrew installs `uv`; the app uses it to create a private Python runtime under `~/.litellm-menu` on first launch.
- The service must start from `.litellm-runtime/config.yaml`. Editable `config.yaml` is the source, not the live file.
- Config watch should validate and stage changes, then require apply/restart. It should not silently restart the service on every file write.
- Autostart should launch the menu app, not a standalone proxy detached from the menu owner process.

## WebDAV Sync

- WebDAV sync is optional and disabled by default.
- Sync state must compare local state, remote state, and the last successful baseline. Avoid replacing bidirectional sync with upload-only behavior.
- WebDAV settings and synced config may contain provider API keys. Keep examples synthetic and warn users to use private paths.

## Release And Public Hygiene

- Keep public docs promotional and user-facing. Avoid internal debug language, private model names, request ids, local paths, or domain-specific examples.
- `config.example.yaml` should remain fully sanitized and runnable as a structure template.
- Before a public release, run tests, build the app bundle, validate the formula syntax, parse the example config, and run a sensitive-string scan.
- If the repository is being prepared as a first public release, reset git history only after docs, examples, tests, and scans are clean.
