# LiteLLM Menu

LiteLLM Menu is a native macOS and Windows desktop application that runs and manages a local [LiteLLM](https://github.com/BerriAI/litellm) proxy service. It consolidates multi-provider model routing, deployment fallback, Responses API compatibility, vision bridging, web search bridging, image generation tool adaptation, and selectable Codex configuration into a single app-owned local endpoint.

The desktop UI is delivered through the shared React Native workspace in
[`rn/`](./rn/): React/TypeScript owns routes, components, interaction and
i18n; `litellm_menu/core/` owns the single staged state source and versioned
local IPC; AppKit and WinUI 3 provide only native leaves.

See [Architecture / 架构](./docs/ARCHITECTURE.md) for directory ownership,
dependency rules, and the staged cleanup plan.

---

## Features

### Native Desktop Hosts

LiteLLM Menu uses one shared React/TypeScript UI with an AppKit status item and native macOS controls, plus a WinUI 3 window, native controls, and Windows tray. The app automatically starts and monitors its local LiteLLM proxy while it is open, and shuts that proxy down before it exits. No Docker container, database, virtual environment, or system Python installation is required. Release builds include a self-contained Python runtime, Python Core, and pinned LiteLLM dependencies.

The native menu is grouped by task:

- **App** — control launch at login; the owned LiteLLM service is kept running while the app is open.
- **Configuration** — open **Providers & Models...**, **Codex / Claude Settings...**, or **Runtime Settings...**. Providers can import current Codex/Claude settings, a file, or a signed-in New API/Sub2API relay account; provider and runtime files are imported/exported in their owning screen.
- **Diagnostics** — record or inspect route traces, inspect recovery state and recent requests, open service logs, and configure WebDAV sync.

The status item uses a neutral native template icon. During route recovery, its hover text and the clickable recovery row in the menu show the current step, classified cause, attempt, heartbeat age, and whether recovery is still progressing or may be stuck.

### Deployment Fallback and Routing

Model groups contain multiple deployments across providers. Fallback is ordered by configured deployment `order`:

1. **Ordered protocol fallback within one deployment** — each deployment uses its `supported_upstream_url_surfaces` list exactly as configured, for example `openai/responses` → `openai/chat` → `anthropic`.
2. **Same-order peer fallback** — only after that deployment's selected protocols are exhausted does the proxy try another deployment with the same `order` value.
3. **Next-order fallback** — if no same-order peer is available, the proxy advances to the next higher `order`.
4. **Wrapped-order fallback** — if the failed deployment was the highest order, the proxy wraps to the lowest available order.

Cooldown is tracked independently for every deployment/protocol pair, so a failed Responses endpoint cannot cool down a healthy Chat or Anthropic endpoint on the same deployment. A deployment is excluded only while all of its configured protocols are cooling down. Cooldown and route-recovery state may use local files to coordinate workers, but both are cleared on service restart and successful Apply.

The routing constraint patch integrates exclusions and cooldowns directly into LiteLLM's `Router._get_all_deployments` and `get_available_deployment`, so all routing paths respect the active constraints.

### Responses API Compatibility

Upstreams can expose any ordered combination of OpenAI Responses (`/v1/responses`), Anthropic Messages (`/v1/messages`), and OpenAI Chat Completions (`/v1/chat/completions`). LiteLLM Menu preserves the user's protocol order and adapts a Responses client request to the selected upstream protocol.

The bridge handles:

- **Tool mapping** — Responses `function`, `custom`, and `tool_search` tool types are converted to Chat Completions function tools. Item IDs and tool call IDs are preserved.
- **User-defined surface order** — `supported_upstream_url_surfaces` is the single order source; its first item is the deployment's primary protocol.
- **Preemptive adaptation** — Chat and Anthropic selections are adapted before the upstream call rather than waiting for a Responses failure.
- **Streaming conversion** — Chat Completions stream chunks are converted to Responses stream events (`response.created`, `response.output_item.added`, `response.output_text.delta`, `response.completed`) for clients expecting Responses streaming.

### Image Generation Tool Adaptation

Responses API requests with `tools: [{"type": "image_generation"}]` are routed to deployments that declare `supports_responses_image_generation_tool: true` in their `model_info`. Routing is determined by structured metadata, not by prompt content or provider names.

If a capable deployment returns an empty response or an explicit image-tool-unavailable refusal, the proxy retries the same model group with `tool_choice: {"type": "image_generation"}` forced. Inline image data URLs are bounded to prevent oversized payloads from breaking compatible providers.

Standalone image model routes (`/v1/images/generations`) are handled separately from the Responses image generation tool.

### Vision Bridge

When a request contains image input and the selected route fails with a vision-unsupported error, the vision bridge rewrites the request to replace image content with textual visual context and retries the original route.

The bridge operates in three modes:

- **`auto`** (default) — tries a configured OpenAI-compatible vision endpoint first, then falls back to the bundled local Vision OCR helper in the portable Core.
- **`api`** — uses only the configured vision endpoint.
- **`local`** — uses only the bundled `Core/bin/vision_ocr` binary, which leverages macOS Vision framework for OCR text recognition and rectangle detection to produce layout summaries. Development hosts may override it with `LITELLM_MENU_VISION_HELPER`.

The bridge does not switch the user's model group to an unrelated chat route. It removes image parts from the original request, appends the visual context as text, and retries the same route.

### Web Search Bridge

When a Responses API request includes `web_search` tool usage:

1. **Native hosted search** is attempted first when route metadata indicates support or support is unknown on a Responses-capable route.
2. **External bridge** activates when the route is chat-only, explicitly lacks hosted web search support, or returns an unsupported hosted-tool error.

The external bridge uses DDGS (DuckDuckGo Search) for query execution and direct retrieval of model-selected source pages. Search parameters — result count, page read character limit, region, backend list, fetch timeout, max rounds, max queries, max open pages — are configurable through runtime settings.

The bridge exposes focused search queries and source URLs to the model. Query planning is model-driven; the bridge does not add request-specific query rewrites.

### Computer Facade

The computer facade adapts hosted `computer-use` tool requests for routes that do not natively support the computer tool. `playwright` is the standalone local browser backend; `browser`, `chrome`, and `mcp` require a matching injected client bridge. Supported backends:

- `auto` (default), `browser`, `chrome`, `playwright`, `mcp`, `mock`

The facade intercepts computer action calls, routes them to the configured backend, and returns observations. Action denylists, max steps, trace logging, and screenshot tracing are configurable through environment variables and runtime settings.

### Codex Optimization

LiteLLM Menu includes targeted optimizations for [Codex](https://github.com/openai/codex) CLI and similar Responses API clients:

- **Codex Settings** — a single staged editor combines typed settings for connection, behavior, features, permissions, providers, MCP/plugins, and advanced options with synchronized `config.toml` and `auth.json` text views. Selecting a LiteLLM deployment stages its local endpoint/key without overwriting unrelated Codex settings or credentials.
- **Fast default tier** — when effective `CODEX_HOME/config.toml` has `service_tier = "fast"` (or `"priority"`) and `[features].fast_mode = true`, Menu injects the upstream value `"priority"` only into reliably identified Codex native `/v1/responses` requests whose original body has no `service_tier`. The config is refreshed on the next request after Apply; explicit tiers and non-Codex traffic are never overwritten. Known boundary: if Desktop strips a manual Standard selection into an absent HTTP field while config remains Fast, the gateway cannot distinguish it; the config file is this shim's explicit switch.
- **Compaction controls** — request pre-processing adds compaction-related metadata and headers that Codex expects.
- **Reasoning effort compatibility** — when an upstream returns an error indicating `xhigh` reasoning effort is unsupported, the proxy retries with a compatible effort level (`high` or `max`) and records the compat retry.
- **Usage normalization** — the `response.completed` event's `usage` block is normalized to the Codex-expected schema (`input_tokens`, `output_tokens`, `input_tokens_details.cached_tokens`, `output_tokens_details.reasoning_tokens`, `total_tokens`), including conversion from Chat Completions `prompt_tokens`/`completion_tokens` naming.
- **Browser-compatible headers** — for providers that require standard browser headers, the proxy injects a browser User-Agent and Accept headers on retry.
- **Internal context stripping** — Codex internal context fragments (environment context, permissions, app context, skill/plugin instructions) are recognized and excluded from trace logging and request summaries.

### WebDAV Config Sync

Optional bidirectional configuration sync via WebDAV. The sync compares local state, remote state, and the last successful baseline to determine whether to push, pull, or merge. Settings (URL, credentials, remote name, sync interval, timeout) are configurable through the menu. Synced config bundles may contain API keys; credentials are stored locally with file permissions set to `0600`.

### Route Trace and Observability

Route trace logging records deployment selection, fallback decisions, surface bridging, vision bridge rewrites, web search bridge activity, image generation fallback, and streaming error recovery. The trace is viewable as an HTML report with per-request cards showing deployment metadata, event timeline, tool call details, and session context.

A recent requests log stores routing and status metadata in JSONL format. Prompt bodies, message content, authorization headers, and API keys are not stored.

### Configuration Validation

The Core validates the editable configuration and stages the validated result to `.litellm-runtime/config.yaml`; use the explicit Apply action to activate it. The runtime service always starts from staged configuration, not directly from the editable source file.

---

## Installation

### Requirements

- macOS 13.0 or later
- Apple silicon Mac for the prebuilt Homebrew Cask
- Xcode, CocoaPods, Node.js 22 or later, and pnpm 11 when building the macOS host
- Windows App SDK and Visual Studio's C++ desktop workload when building the Windows host
- [uv](https://docs.astral.sh/uv/) when building from source (or set `LITELLM_UV_BIN` to a custom path)

### Homebrew (Recommended)

Install the app and its self-contained runtime with one command:

```bash
brew tap ysdj/litellm-menu https://github.com/ysdj/litellm-menu && brew trust ysdj/litellm-menu && brew install --cask ysdj/litellm-menu/litellm-menu
```

Open **LiteLLM Menu** from Applications after installation. Future updates only need `brew upgrade --cask litellm-menu`.

Homebrew downloads the prebuilt app and bundled runtime. Opening it starts the menu-owned service without a first-run dependency install.

### Manual Build

Clone the repository and build the app bundle:

```bash
git clone https://github.com/ysdj/litellm-menu.git
cd litellm-menu
cd rn
pnpm run bootstrap:rnmacos
pnpm install --frozen-lockfile
LITELLM_MENU_MACOS_OUTPUT="$PWD/../artifacts/LiteLLM Menu.app" pnpm run build:macos
```

The desktop dependency line is pinned to React Native `0.85.3`, React Native
Windows `0.85.0-preview.1`, and an exact official `react-native-macos`
`0.85-merge` source commit. `build:macos` verifies that source checkout and its
separate Hermes toolchain before invoking CocoaPods/Xcode.

The output is a signed React Native app with its relocatable Core runtime. Use an isolated output for a preview and never overwrite or launch over a user-owned installation during routine verification. Launch an explicit artifact only after reviewing it:

```bash
open "$PWD/../artifacts/LiteLLM Menu.app"
```

On Windows, run the equivalent host build from a Developer PowerShell:

```powershell
cd rn
pnpm install --frozen-lockfile
pnpm run build:windows
```

### First Launch

1. Open LiteLLM Menu from its menu bar icon.
2. The application starts the local LiteLLM proxy and Python Core from its bundled runtime.
3. Click **Providers & Models...** to configure providers, API keys, models, and deployment order.
4. Click **Apply Config** to stage and activate the configuration.
5. To use Codex through LiteLLM, open **Codex Settings...**, choose a LiteLLM deployment in **Connection & model**, and choose **Apply**. The change is staged first, then updates only the user-level Codex `config.toml` / `auth.json` fields you selected while preserving unrelated values and the model group's LiteLLM route order.

---

## Configuration

The primary configuration file is `~/.litellm-menu/config.yaml`. A sanitized example is provided as `config.example.yaml`.

### Key Sections

- **`providers`** — named provider groups with `api_base` and `api_keys` (each key has `name` and `value`; remove keys that are no longer used).
- **`model_list`** — model group entries with `litellm_params` (model, api_base, api_key, order) and `model_info` (id, provider, route_key, api_key_name, capability flags).
- **`litellm_settings`** — callbacks registration, `public_model_groups`, `drop_params`.
- **`router_settings`** — routing strategy, retry policy, max fallbacks, cooldown configuration.
- **`general_settings`** — master key, UI toggle.

The model editor keeps the client-facing public model name separate from the exact upstream model ID. It derives LiteLLM's internal provider prefix from the first configured upstream API surface (`openai` for OpenAI Responses or Chat, `anthropic` for Anthropic Messages), so there is no separate adapter setting. This permits mappings such as a GPT-compatible public route backed by a differently named OpenAI-compatible upstream model without changing the upstream ID.

### Providers & Models Editor

The **Providers** view is a fixed three-column workspace: provider list, model list, and selected deployment details. Selecting a provider always opens its provider settings; selecting a model opens that model. Provider identity and model count remain distinct. Each model has an independent **Probe** action that does not lock the rest of the editor. Its result shows a summary and the sanitized original requests; hover it for the full text. If the available protocol order differs from the current order, confirmation immediately applies it; matching orders do not prompt. New models are probed once after being added. The compact account line always renders balance as unavailable and reads the credential's effective multiplier once when the window opens; it does not request usage or balance endpoints. Upstream ID and route order remain in the selected deployment form and the dedicated **Routes** view.

The **Routes** view groups deployments by public model and shows order, provider/key, upstream ID, and explicit state: **Available**, **Unavailable**, **Uncertain**, **Not probed**, or **Disabled**. Route order controls change only the selected deployment's configured fallback position.

Provider Base URLs may be entered as a host/root, with or without `/v1` and a trailing slash, or as a complete compatible endpoint such as `/v1/responses`, `/v1/chat/completions`, `/v1/messages`, `/v1/completions`, or their unversioned equivalents. The editor preserves an explicit endpoint, uses the same parsing for **Fetch /v1/models** and **Probe**, and replaces rather than duplicates endpoint suffixes.

### Capability Flags

Each deployment's `model_info` supports:

- `supports_responses_image_generation_tool` — whether the upstream supports the Responses image generation tool.
- `supported_upstream_url_surfaces` — enabled API surfaces in numbered order (`openai/responses`, `openai/chat`, `anthropic`); the first item is primary.
- `x-litellm-menu-upstream-url-surface-order` — editor metadata that preserves the complete numbered order, including unchecked protocols.
- `upstream_url_surface` — generated mirror of the first ordered surface.
- `supports_responses_web_search` / `supports_web_search` — web search capability.

Each model deployment has its own **Probe** action. For text models it checks Responses, Chat Completions, Anthropic Messages, and the credential's effective multiplier. A usable result stages the model as enabled and selects exactly one recommended protocol: Anthropic for Claude-family model identifiers when available, otherwise Responses before Chat and Anthropic. The staged recommendation becomes the protocol fallback only when **Apply** succeeds; additional checked protocols remain an explicit user choice. **Upstream API order** is the fallback order from LiteLLM to the deployment endpoint and does not change the client-facing API. Known standalone image models probe only `/v1/images/generations`; an unconfigured model may also test that endpoint only after all three text APIs are definitively unavailable.

### Provider and Model Imports

**Import Providers & Models** in the editor accepts only an explicit source and stages the result as a reviewable draft:

- **Current Codex** reads the current `CODEX_HOME/config.toml`. It uses the current file-backed OpenAI credential only for the selected built-in OpenAI route or a selected custom provider that declares `requires_openai_auth = true`; it never resolves arbitrary provider environment variables.
- **Choose File** accepts Codex, LiteLLM/OpenAI-compatible, and generic JSON/TOML/YAML; documented CLIProxyAPI `openai-compatibility` and `codex-api-key` data; and current CC Switch `.sql` text exports. SQL is parsed as text and is never executed.
- **Paste Link** accepts a New API or CC Switch `ccswitch://v1/import` provider link through a masked field and standard input so its embedded key is not placed in process arguments.

Imported providers and models are not written until **Apply Config**.

### Runtime Settings

Adjustable through the menu without editing config files:

| Setting | Environment Variable | Default |
|---|---|---|
| Stall timeout | `LITELLM_MENU_STALL_TIMEOUT_SECONDS` | 120 |
| Request timeout | `LITELLM_MENU_REQUEST_TIMEOUT_SECONDS` | 7200 |
| Codex parent completion barrier | `LITELLM_MENU_CODEX_DESCENDANT_CLEANUP` | 1 (on) |
| Deployment cooldown failures | `LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES` | 2 |
| Deployment cooldown seconds | `LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS` | 300 |
| Web search max results | `LITELLM_MENU_WEB_SEARCH_MAX_RESULTS` | 5 |
| Web fetch timeout | `LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS` | 12 |
| Vision bridge backend | `LITELLM_MENU_VISION_BRIDGE_BACKEND` | auto |
| Vision bridge model | `LITELLM_MENU_VISION_BRIDGE_MODEL` | qwen2.5vl:3b |
| Computer facade backend | `LITELLM_MENU_COMPUTER_FACADE_BACKEND` | auto |
| Computer facade max steps | `LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS` | 20 |
| Route recovery interval | `LITELLM_MENU_RECOVERY_INTERVAL_SECONDS` | 5 |

### Configuration Files

Provider/model import and export live in **Providers & Models**; runtime import and export live in **Runtime Settings**. Each export is a single-domain JSON file with restrictive `0600` permissions and may contain credentials. Import validates the selected file and replaces only that screen's draft. Nothing is written until the normal **Apply** or **Save & Apply** confirmation.

---

## Deep Links

Both native hosts accept fixed `litellm-menu://open/<route>` links. For example, macOS can open settings without relying on status-item automation:

```bash
open "litellm-menu://open/providers-models"
open "litellm-menu://open/codex-settings"
open "litellm-menu://open/logs?tab=service"
```

Deep links only navigate to allowlisted local routes. They cannot carry configuration values or trigger Apply.

---

## Development

### Running Tests

Run focused TypeScript and Python behavior checks for the code you changed.
Invoke the relevant unittest modules directly with the project's configured
runtime; do not require a release build or packaging script for a scoped
change.

### Building the App

Install the pinned shared dependencies and run the focused type check:

```bash
cd rn
pnpm install --frozen-lockfile
pnpm exec tsc --noEmit
```

Build a macOS preview or release only when explicitly needed, with an isolated output path. Windows builds use `pnpm run build:windows` on a Windows host.

### Version Management

`VERSION`, `BUILD_NUMBER`, the RN macOS plist/Xcode project, both Windows manifests, and `Casks/litellm-menu.rb` are kept in sync through `scripts/version.py`.

LiteLLM is checked and advanced to the latest stable PyPI release at the beginning of every macOS and Windows build. `./scripts/update-litellm.sh --check` reports whether `LITELLM_VERSION` is current. A build may compile LiteLLM from its source distribution in the controlled build environment when PyPI has not published a matching wheel; the completed app still packages and runs the exact locked version, so app startup never compiles or upgrades LiteLLM.

---

## License

MIT License. See [LICENSE](LICENSE).

---

# LiteLLM Menu（中文）

LiteLLM Menu 是一个 macOS 与 Windows 原生桌面应用，用于运行和管理本地 [LiteLLM](https://github.com/BerriAI/litellm) 代理服务。它将多供应商模型路由、部署回退、Responses API 兼容、视觉桥接、网页搜索桥接、图像生成工具适配，以及可选择模型的 Codex 配置整合到一个由应用管理的本地端点中。

---

## 功能特性

### 双平台原生宿主

LiteLLM Menu 由一套共享 React/TypeScript UI、AppKit 状态项与 macOS 原生控件，以及 WinUI 3 窗口、Windows 原生控件和托盘组成。应用打开时会自动启动并监控本地 LiteLLM 代理，退出前关闭所辖服务。无需 Docker、数据库、虚拟环境或系统 Python；发布包内置独立 Python 运行时、Python Core 和锁定版本的 LiteLLM 依赖。

原生菜单按任务分为三组：

- **App** — 控制登录时自动启动；应用打开期间会保持其所辖 LiteLLM 服务运行。
- **Configuration** — 打开 **Providers & Models...**、**Codex / Claude Settings...** 或 **Runtime Settings...**。供应商页可从当前 Codex/Claude 设置、文件或已登录的 New API/Sub2API 中转站账号导入；供应商和运行时文件分别在各自页面导入或导出。
- **Diagnostics** — 记录或查看路由追踪，检查恢复状态与最近请求，打开服务或配置监听日志，以及配置 WebDAV 同步。

状态项始终保持中性的 `LL`，不会用颜色或符号表达异常。发生路由恢复时，悬停文字和菜单中的可点击恢复状态行会显示当前步骤、分类原因、尝试次数、心跳时间，以及恢复仍在推进还是可能卡住。

### 部署回退与路由

模型组包含跨供应商的多个部署。回退按配置的部署 `order` 排序：

1. **部署内有序协议回退** — 每个部署严格按 `supported_upstream_url_surfaces` 配置尝试，例如 `openai/responses` → `openai/chat` → `anthropic`。
2. **同序对等回退** — 当前部署选中的协议都尝试完后，才尝试同一 `order` 下的其他供应商或 API 密钥。
3. **下一序回退** — 无同序对等部署可用时，代理前进到下一个更高的 `order`。
4. **环绕回退** — 失败部署已是最高序时，代理环绕到最低可用序。

冷却按“部署 + 协议”独立记录，因此坏掉的 Responses 不会连累同一部署中健康的 Chat 或 Anthropic。只有配置的全部协议都处于冷却时，部署才会被排除。冷却和 recovery 可以用本地文件协调多个 worker，但服务重启和成功 Apply 后都会清空。

路由约束补丁将排除和冷却直接集成到 LiteLLM 的 `Router._get_all_deployments` 和 `get_available_deployment` 中，使所有路由路径均遵循当前约束。

### Responses API 兼容

上游可按任意顺序支持 OpenAI Responses（`/v1/responses`）、Anthropic Messages（`/v1/messages`）和 OpenAI Chat Completions（`/v1/chat/completions`）。LiteLLM Menu 保留用户配置的协议顺序，并将 Responses 客户端请求适配到选中的上游协议。

桥接器处理以下内容：

- **工具映射** — Responses 的 `function`、`custom` 和 `tool_search` 工具类型转换为 Chat Completions 的 function 工具。项 ID 和工具调用 ID 予以保留。
- **用户自定义接口顺序** — `supported_upstream_url_surfaces` 是唯一顺序真源，第一项为主协议。
- **预先适配** — 选中 Chat 或 Anthropic 时，在调用上游前完成适配，无需先等待 Responses 失败。
- **流式转换** — Chat Completions 流式数据块转换为 Responses 流式事件（`response.created`、`response.output_item.added`、`response.output_text.delta`、`response.completed`），供期望 Responses 流式的客户端使用。

### 图像生成工具适配

包含 `tools: [{"type": "image_generation"}]` 的 Responses API 请求被路由到在 `model_info` 中声明 `supports_responses_image_generation_tool: true` 的部署。路由由结构化元数据决定，而非提示词内容或供应商名称。

当能力部署返回空响应或明确的图像工具不可用拒绝时，代理以强制 `tool_choice: {"type": "image_generation"}` 重试同一模型组。内联图像 data URL 受到大小限制，以防止过大负载破坏兼容供应商。

独立图像模型路由（`/v1/images/generations`）与 Responses 图像生成工具分开处理。

### 视觉桥接

当请求包含图像输入且所选路由返回视觉不支持的错误时，视觉桥接器将请求中的图像内容替换为文本视觉上下文，并重试原始路由。

桥接器有三种模式：

- **`auto`**（默认）— 先尝试已配置的 OpenAI 兼容视觉端点，失败后回退到可迁移 Core 中的内置本地 Vision OCR 辅助工具。
- **`api`** — 仅使用已配置的视觉端点。
- **`local`** — 仅使用内置 `Core/bin/vision_ocr` 二进制工具，该工具利用 macOS Vision 框架进行 OCR 文本识别和矩形检测，生成布局摘要。开发宿主可用 `LITELLM_MENU_VISION_HELPER` 覆盖该路径。

桥接器不会将用户的模型组切换到无关的聊天路由。它移除原始请求中的图像部分，以文本形式附加视觉上下文，并重试同一路由。

### 网页搜索桥接

当 Responses API 请求包含 `web_search` 工具使用时：

1. **原生托管搜索** — 路由元数据表明支持或支持状态未知时，优先在支持 Responses 的路由上尝试原生托管搜索。
2. **外部桥接** — 路由仅支持聊天、明确不支持托管网页搜索，或返回不支持托管工具错误时激活。

外部桥接使用 DDGS（DuckDuckGo Search）执行查询，并直接读取模型选定的来源页面。默认先用 Yahoo；只有结果不足时才继续用 Bing，因此正常查询保持快速，同时不会因单个后端临时空结果而只给模型一条空白线索。搜索参数 — 结果数量、读取字符限制、区域、后端列表、获取超时、最大轮次、最大查询数、最大打开页面数 — 均可通过运行时设置配置。

桥接器向模型暴露聚焦的搜索查询和来源 URL。查询规划由模型驱动：模型决定是否直接回答、换词、查看下一页或打开某个来源；默认四个动作轮次让它在多次换词后仍有机会查看刚找到的来源。桥接器不添加特定于请求的查询重写。

### Computer Facade

Computer facade 为不支持原生 computer 工具的路由适配托管的 `computer-use` 工具请求。`playwright` 是独立的本地浏览器后端；`browser`、`chrome` 和 `mcp` 需要注入匹配的客户端桥接。支持的后端：

- `auto`（默认）、`browser`、`chrome`、`playwright`、`mcp`、`mock`

Facade 拦截 computer 动作调用，路由到已配置的后端，并返回观察结果。动作拒绝列表、最大步数、追踪日志和截图追踪可通过环境变量和运行时设置配置。

### Codex 优化

LiteLLM Menu 包含针对 [Codex](https://github.com/openai/codex) CLI 及类似 Responses API 客户端的定向优化：

- **Codex Settings** — 单一草稿窗口把连接、行为、功能开关、权限、provider、MCP/plugin 与高级选项的结构化控制，同 `config.toml` / `auth.json` 的实时文本视图同步；选择 LiteLLM 部署只会暂存本地端点与密钥，直到点击 Apply 才会写入，且保留其余 Codex 设置和认证字段。
- **Fast 默认 tier** — 当有效 `CODEX_HOME/config.toml` 同时包含 `service_tier = "fast"`（也兼容 `"priority"`）和 `[features].fast_mode = true` 时，Menu 只会为可可靠识别的 Codex 原生 `/v1/responses` 请求、且原始请求未提供 `service_tier` 时注入上游标准值 `"priority"`。配置文件每次请求按变更刷新，因此 Codex Settings 的 Apply 后下一请求生效；显式 tier 与非 Codex 流量绝不覆盖。已知边界：若 Desktop 在用户手动选择 Standard 后先把该选择剥为“字段缺失”，而配置仍为 Fast，网关无法仅从 HTTP 请求区分这种情形；此 shim 的明确开关是配置文件。
- **压缩控制** — 请求预处理添加 Codex 所需的压缩相关元数据和头信息。
- **推理强度兼容** — 上游返回表明 `xhigh` 推理强度不支持的错误时，代理以兼容强度级别（`high` 或 `max`）重试，并记录兼容重试。
- **用量归一化** — `response.completed` 事件的 `usage` 块被归一化为 Codex 期望的架构（`input_tokens`、`output_tokens`、`input_tokens_details.cached_tokens`、`output_tokens_details.reasoning_tokens`、`total_tokens`），包括从 Chat Completions 的 `prompt_tokens`/`completion_tokens` 命名转换。
- **浏览器兼容头** — 对需要标准浏览器头的供应商，代理在重试时注入浏览器 User-Agent 和 Accept 头。
- **内部上下文剥离** — Codex 内部上下文片段（环境上下文、权限、应用上下文、技能/插件指令）被识别并从追踪日志和请求摘要中排除。

### WebDAV 配置同步

通过 WebDAV 进行可选的双向配置同步。同步逻辑比较本地状态、远程状态和上次成功基线，决定推送、拉取还是合并。设置（URL、凭据、远程名称、同步间隔、超时）可通过菜单配置。同步的配置包可能包含 API 密钥；凭据以本地文件存储，文件权限设为 `0600`。

### 路由追踪与可观测性

路由追踪日志记录部署选择、回退决策、接口桥接、视觉桥接重写、网页搜索桥接活动、图像生成回退和流式错误恢复。追踪以 HTML 报告形式查看，包含每个请求的卡片，展示部署元数据、事件时间线、工具调用详情和会话上下文。

最近请求日志以 JSONL 格式存储路由和状态元数据。不存储提示词正文、消息内容、授权头和 API 密钥。

### 配置验证

Python Core 验证可编辑配置，并将验证通过的结果暂存到 `.litellm-runtime/config.yaml`；需通过显式的 Apply 操作激活。运行时服务始终从暂存配置启动，而非直接使用可编辑的源文件。

---

## 安装

### 系统要求

- macOS 13.0 或更高版本
- 预构建 Homebrew Cask 目前要求 Apple silicon Mac
- 构建 macOS 宿主需要 Xcode、CocoaPods、Node.js 22 或以上和 pnpm 11
- 构建 Windows 宿主需要 Windows App SDK 和 Visual Studio C++ 桌面工作负载
- 源码构建需要 [uv](https://docs.astral.sh/uv/)（也可设置 `LITELLM_UV_BIN` 指向自定义路径）

### Homebrew 安装（推荐）

使用一条命令安装应用及其自带运行时：

```bash
brew tap ysdj/litellm-menu https://github.com/ysdj/litellm-menu && brew trust ysdj/litellm-menu && brew install --cask ysdj/litellm-menu/litellm-menu
```

安装后直接从“应用程序”打开 **LiteLLM Menu**。以后更新只需运行 `brew upgrade --cask litellm-menu`。

Homebrew 会下载预构建应用及其内置运行时。打开后直接启动由菜单管理的服务，首次运行不会安装依赖。

### 手动构建

克隆仓库并构建应用包：

```bash
git clone https://github.com/ysdj/litellm-menu.git
cd litellm-menu
cd rn
pnpm run bootstrap:rnmacos
pnpm install --frozen-lockfile
LITELLM_MENU_MACOS_OUTPUT="$PWD/../artifacts/LiteLLM Menu.app" pnpm run build:macos
```

桌面依赖线固定为 React Native `0.85.3`、React Native Windows
`0.85.0-preview.1` 和官方 `react-native-macos` `0.85-merge` 的精确源码
commit。`build:macos` 会在调用 CocoaPods/Xcode 前校验该源码及其独立 Hermes
工具链。

输出是带可迁移 Core runtime 的已签名 RN 应用。预览必须使用隔离输出，日常验证不得覆盖或替换用户正在使用的安装；检查产物后才可显式启动：

```bash
open "$PWD/../artifacts/LiteLLM Menu.app"
```

Windows 请从 Developer PowerShell 执行：

```powershell
cd rn
pnpm install --frozen-lockfile
pnpm run build:windows
```

### 首次启动

1. 从菜单栏图标（"LL" 状态项）打开 LiteLLM Menu。
2. 应用从内置运行时启动本地 LiteLLM 代理和 Python Core。
3. 点击 **Providers & Models...** 配置供应商、API 密钥、模型和部署顺序。
4. 点击 **Apply Config** 暂存并激活配置。
5. 如需让 Codex 通过 LiteLLM 运行，点击 **Codex Settings...**，在 **Connection & model** 中选择一个已配置的 provider/model 部署，然后点击 **Apply**。

---

## 配置

主配置文件为 `~/.litellm-menu/config.yaml`。仓库提供已脱敏的示例文件 `config.example.yaml`。

### 主要配置段

- **`providers`** — 命名供应商组，包含 `api_base` 和 `api_keys`（每个密钥只有 `name` 和 `value`；不再使用的密钥应直接删除）。
- **`model_list`** — 模型组条目，包含 `litellm_params`（model、api_base、api_key、order）和 `model_info`（id、provider、route_key、api_key_name、能力标志）。
- **`litellm_settings`** — 回调注册、`public_model_groups`、`drop_params`。
- **`router_settings`** — 路由策略、重试策略、最大回退数、冷却配置。
- **`general_settings`** — 主密钥、UI 开关。

模型编辑器将客户端看到的公开模型名与上游原始模型 ID 分开保存。LiteLLM 内部供应商前缀由第一个上游 API 协议自动派生（OpenAI Responses 或 Chat 使用 `openai`，Anthropic Messages 使用 `anthropic`），不再单独配置 adapter。这样可以把 GPT 兼容的公开路由映射到名称不同的 OpenAI-compatible 上游模型，同时保持上游 ID 不变。

### Providers & Models 编辑器

**Providers** 视图将 provider 名称和模型数量分列显示。点击供应商始终打开其供应商设置，点击模型打开该模型。每个模型的 **探测** 相互独立，不会锁住其余编辑控件；结果显示总结和脱敏后的原始请求，鼠标悬停可查看全文。若可用协议顺序与当前不同，确认后会立即应用；相同时不弹确认。新增模型后会自动探测一次。紧凑账户行中的余额固定显示为不可用；窗口打开时只读取一次当前凭据的有效倍率，不请求用量或余额端点。上游 ID 与路由顺序保留在右侧部署表单和专用 **Routes** 视图中。

**Routes** 视图按公开模型对部署分组，显示顺序、provider/API key、上游 ID 和明确状态：**Available**、**Unavailable**、**Uncertain**、**Not probed** 或 **Disabled**。路由顺序按钮只修改所选部署的配置回退位置。

Provider Base URL 可以填写主机/根路径、带或不带 `/v1`、带或不带末尾斜杠，也可以直接填写 `/v1/responses`、`/v1/chat/completions`、`/v1/messages`、`/v1/completions` 及其无版本完整端点。编辑器会保留明确填写的端点，**Fetch /v1/models** 与 **Probe** 也使用同一解析逻辑，因此会替换已有端点后缀而不会重复拼接。

### 能力标志

每个部署的 `model_info` 支持：

- `supports_responses_image_generation_tool` — 上游是否支持 Responses 图像生成工具。
- `supported_upstream_url_surfaces` — 按编号排序的已启用 API 接口列表（`openai/responses`、`openai/chat`、`anthropic`），第一项是主协议。
- `x-litellm-menu-upstream-url-surface-order` — 编辑器元数据，用于保留包括未勾选协议在内的完整编号顺序。
- `upstream_url_surface` — 自动生成的第一项镜像。
- `supports_responses_web_search` / `supports_web_search` — 网页搜索能力。

每个模型部署都有独立的 **Probe** 操作。文本模型会同时探测 Responses、Chat Completions、Anthropic Messages 和当前凭据的有效倍率。探测不会改变模型开关；Claude 系列的可用顺序优先 Anthropic，其余依次为 Responses、Chat、Anthropic。若探测顺序变化，用户确认后会立即写入并应用；相同则保持不变。**上游 API 顺序** 指 LiteLLM 到该部署端点的回退顺序，不会改变客户端接口。上游模型输入和列表只显示原始模型名；系统按首选协议自动写入 `openai/` 或 `anthropic/` 前缀。已知的独立图片模型只探测 `/v1/images/generations`；尚未配置的模型也只会在三个文本 API 都明确不可用后才尝试该端点。

### Provider 与模型导入

编辑器中的 **Import Providers & Models** 只接受显式选择的来源，并将结果暂存为可检查的草稿：

- **Current Codex** 读取当前 `CODEX_HOME/config.toml`。只有选中内置 OpenAI 路由，或选中的自定义 provider 明确设置 `requires_openai_auth = true` 时，才使用当前文件型 OpenAI 凭据；不会解析任意 provider 的环境变量。
- **Choose File** 支持 Codex、LiteLLM/OpenAI-compatible 及通用 JSON/TOML/YAML，CLIProxyAPI 文档格式中的 `openai-compatibility` 与 `codex-api-key`，以及当前 CC Switch `.sql` 文本导出。SQL 只按文本解析，绝不会执行。
- **Paste Link** 通过掩码输入框和标准输入接收 New API 或 CC Switch 的 `ccswitch://v1/import` provider 链接，内嵌 API key 不会进入进程参数。

导入的 provider 和模型只有点击 **Apply Config** 后才会写入。

### 运行时设置

可通过菜单调整，无需编辑配置文件：

| 设置项 | 环境变量 | 默认值 |
|---|---|---|
| 停滞超时 | `LITELLM_MENU_STALL_TIMEOUT_SECONDS` | 120 |
| 请求超时 | `LITELLM_MENU_REQUEST_TIMEOUT_SECONDS` | 7200 |
| Codex 父线程完成屏障 | `LITELLM_MENU_CODEX_DESCENDANT_CLEANUP` | 1（开启） |
| 部署冷却失败次数 | `LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES` | 2 |
| 部署冷却秒数 | `LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS` | 300 |
| 网页搜索最大结果数 | `LITELLM_MENU_WEB_SEARCH_MAX_RESULTS` | 5 |
| 网页获取超时 | `LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS` | 12 |
| 视觉桥接后端 | `LITELLM_MENU_VISION_BRIDGE_BACKEND` | auto |
| 视觉桥接模型 | `LITELLM_MENU_VISION_BRIDGE_MODEL` | qwen2.5vl:3b |
| Computer facade 后端 | `LITELLM_MENU_COMPUTER_FACADE_BACKEND` | auto |
| Computer facade 最大步数 | `LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS` | 20 |
| 路由恢复间隔 | `LITELLM_MENU_RECOVERY_INTERVAL_SECONDS` | 5 |

### 配置文件

供应商/模型的导入与导出位于 **Providers & Models**，运行时的导入与导出位于 **Runtime Settings**。每次导出只生成对应分区的 JSON 文件，权限为 `0600`，其中可能包含凭据。导入会先校验且只替换当前页面的草稿；只有点击正常的 **Apply** 或 **Save & Apply** 后才会写入配置。

---

## 深链

两个原生宿主都接受固定的 `litellm-menu://open/<route>` 深链。例如在 macOS 打开设置：

```bash
open "litellm-menu://open/providers-models"
open "litellm-menu://open/codex-settings"
open "litellm-menu://open/logs?tab=service"
```

深链只能导航到允许的本地页面，不能携带配置值或触发 Apply。

---

## 开发

### 运行测试

针对改动运行聚焦的 TypeScript 与 Python 行为检查。用项目配置的运行时直接执行
对应 unittest 模块；不要把发布构建或打包脚本作为小范围改动的必要条件。

### 构建应用

安装锁定依赖并执行聚焦的类型检查：

```bash
cd rn
pnpm install --frozen-lockfile
pnpm exec tsc --noEmit
```

仅在明确需要预览或发布时构建 macOS，且必须使用隔离输出路径。Windows 在 Windows 主机运行 `pnpm run build:windows`。

### 版本管理

`VERSION`、`BUILD_NUMBER`、RN macOS plist/Xcode 工程、两个 Windows manifest 和 `Casks/litellm-menu.rb` 通过 `scripts/version.py` 保持同步。

每次 macOS 与 Windows 构建开始时，LiteLLM 都会检查并推进到 PyPI 最新稳定版。`./scripts/update-litellm.sh --check` 可检查 `LITELLM_VERSION` 是否最新。若 PyPI 尚未提供匹配 wheel，构建环境会受控地从源码分发构建 LiteLLM；完成后的应用仍打包并运行这个精确锁定版本，因此应用启动时不会编译或自动升级 LiteLLM。

---

## 许可证

MIT 许可证。详见 [LICENSE](LICENSE)。
