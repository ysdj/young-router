# Young Router

[English](#young-router) · [简体中文](#漾路由young-router)

**One local address for all your AI tools — and it keeps working when a provider doesn't.**

Young Router is a native macOS and Windows app that runs a local AI gateway (built on [LiteLLM](https://github.com/BerriAI/litellm)). Point Codex, Claude Code, or any OpenAI-compatible tool at one local address, and Young Router spreads every request across your providers, keys, official accounts, and relay stations — with automatic failover and capability bridges. No Docker, no Python, no terminal.

## Highlights

- **Never stops.** Every model can have several routes, failing over same-order → next-order → wraparound. Incompatible protocols switch automatically and are remembered for 10 minutes. Failing routes cool down (2 failures / 300 s by default) and a background loop restores them as soon as they answer; stalled streams fail over instead of hanging. The app owns the service — start, health checks, shutdown — and the local port steps forward when busy.
- **Vision bridge.** When a route explicitly rejects image input, Young Router describes the image (local OCR, Ollama / LM Studio, your HTTP vision providers, or a free fallback) and retries the same route with text context. Routes that support vision always get the original image untouched.
- **Search bridge.** Native hosted web search where supported; otherwise the bundled [pi-web-access](https://github.com/nicobailon/pi-web-access) worker does real searches and page fetches. OpenRouter routes use their native search tool. Transient failures are never remembered as "unsupported".
- **Relay-station management.** Add a New API / Sub2API station by URL — the app detects its type, you sign in inside the app, and it loads your API keys, groups, models, and balance. Key changes save and sync to the station as you make them; deleting a key first shows which models depend on it; keys auto-bind to providers with the same Base URL. Official OpenAI / Claude accounts live in the same window.
- **Degradation deep test.** The model detail pane's probe names what it will run: on a Responses route whose name matches a known Codex route it also runs the bundled [TraceOne](https://github.com/wangchao0708/TraceOne) fingerprint check and reports whether the answer came from the requested route or from a different one. A fingerprint mismatch is only reported — the model keeps its enable checkbox and its routing stays yours to change.
- **Image generation bridge.** Image-generation requests are routed to the deployments that support them, with a forced retry when a capable route returns nothing.
- **Codex & Claude in one click.** Codex Settings writes the local endpoint and key without touching your other settings, and adds model catalog, fast tier, compaction, reasoning-effort compatibility, and usage normalization. Claude Settings offers **Use local API**, plus models, permissions, and sandbox.
- **Private by default.** Prompts, message bodies, authorization headers, and API keys never enter the request log.
- **Validated, safe configuration.** Every edit is validated as it is saved, a rejected change leaves the previous configuration untouched, and credentials stay in private storage.
- **Native and self-contained.** Menu bar / tray app with English and Chinese UI; release builds bundle Python, Node.js, and a pinned LiteLLM, so nothing else needs installing.

Also included: route-trace HTML reports, online usage, and optional WebDAV config sync.

## Quick start

1. Install the app (see below).
2. Open Young Router — the local service starts automatically; the menu bar / tray icon shows its state.
3. Open **Service Provider Management…** to sign in to OpenAI / Claude, or add a New API / Sub2API station by URL. Keys and models load automatically.
   Prefer plain API keys? Add them in **Providers & Models…**, or import your current Codex / Claude setup.
4. Connect your tools:
   - **Codex** → **Codex Settings…** → choose a LiteLLM deployment
   - **Claude** → **Claude Settings…** → **Use local API**
   - **Anything OpenAI-compatible** → Base URL `http://127.0.0.1:12390/v1` (actual port in **General**), with your local gateway key

## Installation

Requirements: macOS 13+ (prebuilt Cask: Apple silicon); Windows via source build.

```bash
brew tap ysdj/young-router https://github.com/ysdj/young-router && brew trust ysdj/young-router && brew install --cask ysdj/young-router/young-router
```

Open **Young Router** from Applications. Updates: `brew upgrade --cask young-router`.

<details>
<summary>Build from source</summary>

macOS:

```bash
git clone https://github.com/ysdj/young-router.git && cd young-router/rn
pnpm run bootstrap:rnmacos && pnpm install --frozen-lockfile
YOUNG_ROUTER_MACOS_OUTPUT="$PWD/../artifacts/Young Router.app" pnpm run build:macos
```

Windows (Developer PowerShell, needs Windows App SDK + VS C++ desktop workload): `pnpm install --frozen-lockfile && pnpm run build:windows`.

Source builds also need Xcode, CocoaPods, Node.js 22+, pnpm 11, and [uv](https://docs.astral.sh/uv/).

</details>

## Configuration

Editable config: `~/.young-router/config.yaml` (sanitized example: [`config.example.yaml`](./config.example.yaml)). Every edit is validated as it is written and the service reloads itself when a saved change requires it; only the provider wizard, the raw file editor, and 分组管理 stage their edits until their own Save/Close. Runtime knobs (timeouts, cool-downs, port, vision / search / computer settings) live in **Runtime**; per-deployment capability flags (`upstream_protocol_mode`, `upstream_url_surface`, `supports_responses_web_search`, `supports_responses_image_generation_tool`, `supports_responses_compaction`) are documented in the example file.

<details>
<summary>Import / export, sync, deep links, development</summary>

- **Import/export:** bring providers in from Codex / Claude settings, files, or New API / CC Switch links; export configuration sections as JSON. Confirming the detected sections writes them right away. Optional WebDAV smart sync (push / pull / merge) is available under **Backup & Sync**.
- **Deep links:** open any screen directly — `open "young-router://open/<route>"` with `home`, `providers-models`, `provider-wizard`, `codex-settings`, `claude-settings`, `runtime-settings`, `relay-accounts`, `data-management`, or `logs?tab=…` (`requests`, `service`, `route-trace`, `recovery`, `online-usage`). Links cannot carry values or trigger a configuration write.
- **Development:** contributor guide in [`AGENTS.md`](./AGENTS.md); focused Python tests live in `tests/`; run `pnpm exec tsc --noEmit` inside `rn/`. `scripts/version.py` keeps `VERSION`, manifests, and the Cask in sync; builds advance `LITELLM_VERSION` to the latest stable release and bundle a matching pi-web-access + Node.js 22 runtime.

</details>

## License

MIT License. See [LICENSE](./LICENSE).

---
---

# 漾路由（Young Router）

[English](#young-router) · [简体中文](#漾路由young-router)

**一个本地地址，接上你所有的 AI 工具——上游有问题，它也不轻易停。**

漾路由是一款 macOS 与 Windows 原生应用，用于运行基于 [LiteLLM](https://github.com/BerriAI/litellm) 的本地 AI 网关。把 Codex、Claude Code 或任意 OpenAI 兼容工具指向同一个本地地址，漾路由就会把每个请求分发到你的所有供应商、密钥、官方账号和中转站之间，并自动回退、自动补齐缺失能力。不需要 Docker、Python 或命令行。

## 核心优势

- **永不停歇。** 每个模型可以配置多条线路，按同序 → 下一顺序 → 环绕自动回退；协议不兼容时自动切换并记忆 10 分钟；失败线路默认冷却（连续 2 次 / 300 秒），后台每 5 秒探测、恢复后自动重新启用；流式响应卡住会改走其他线路而不是一直挂起。服务由应用托管（启动、健康检查、退出关闭），本地端口被占用时自动顺延。
- **识图桥接。** 线路明确拒绝图片输入时，自动生成图片描述（本机 OCR、Ollama / LM Studio、你配置的 HTTP 视觉供应商或免费回退），转成文本后重试原线路；真正支持视觉的线路始终收到未被改动的原图。
- **搜索桥接。** 支持原生联网搜索时优先用原生；不支持时由内置的 [pi-web-access](https://github.com/nicobailon/pi-web-access) worker 执行真实搜索与网页抓取；OpenRouter 线路使用其原生搜索工具。瞬时失败不会被误记为“不支持”。
- **中转站智能管理。** 填一个 URL 即可添加 New API / Sub2API 中转站：自动识别类型、在应用内登录、自动加载 API 密钥、分组、模型和余额。密钥改动即时保存并同步到中转站；删除前会提示哪些模型正在使用；Base URL 相同的供应商会自动绑定这些密钥。OpenAI / Claude 官方账号也在同一个窗口管理。
- **降智深测。** 模型详情页的按钮会说明它将要执行什么：当线路走 responses 协议且模型名对应已知 Codex 线路时，同时运行内置的 [TraceOne](https://github.com/wangchao0708/TraceOne) 指纹探测，给出“指纹与所请求线路一致”还是“指纹更像另一条线路”。指纹不一致只作为结果展示——不会取消模型的启用勾选，路由怎么改仍由你决定。
- **图像生成桥接。** 图像生成请求会被路由到真正支持该能力的线路；能力线路返回空响应时会强制重试，避免请求白跑。
- **Codex 与 Claude 一键接入。** Codex 设置会写入本地端点与密钥且不碰你的其他配置，并提供模型目录、Fast 层级、压缩方式、推理强度兼容和用量归一化；Claude 设置提供**使用本机 API**，以及模型、权限和沙箱选项。
- **默认保护隐私。** 提示词正文、消息内容、授权头和 API 密钥不会进入请求日志。
- **配置安全可控。** 每次修改都会先校验再保存；校验不通过的修改不会覆盖现有配置，原始凭据保存在私有存储中。
- **原生、零额外依赖。** 菜单栏 / 托盘应用，中英文界面；发布包内置 Python、Node.js 和锁定版本的 LiteLLM，无需安装其他依赖。

另外还包含：路由追踪 HTML 报告、在线用量，以及可选的 WebDAV 配置同步。

## 快速开始

1. 安装应用（见下）。
2. 打开漾路由——本地服务自动启动，菜单栏 / 托盘图标显示服务状态。
3. 打开**服务商管理…** 登录 OpenAI / Claude，或填入 URL 添加 New API / Sub2API 中转站，密钥和模型会自动加载。
   如果习惯直接用 API 密钥，可以在**供应商与模型…** 中添加，或导入现有的 Codex / Claude 配置。
4. 接入工具：
   - **Codex** → **Codex 设置…** → 选择一个 LiteLLM 部署
   - **Claude** → **Claude 设置…** → **使用本机 API**
   - **其他 OpenAI 兼容工具** → Base URL `http://127.0.0.1:12390/v1`（实际端口见**常规**），密钥使用本地网关密钥

## 安装

系统要求：macOS 13+（预构建 Cask 为 Apple silicon）；Windows 需从源码构建。

```bash
brew tap ysdj/young-router https://github.com/ysdj/young-router && brew trust ysdj/young-router && brew install --cask ysdj/young-router/young-router
```

安装后从“应用程序”打开**漾路由**。更新：`brew upgrade --cask young-router`。

<details>
<summary>从源码构建</summary>

macOS：

```bash
git clone https://github.com/ysdj/young-router.git && cd young-router/rn
pnpm run bootstrap:rnmacos && pnpm install --frozen-lockfile
YOUNG_ROUTER_MACOS_OUTPUT="$PWD/../artifacts/Young Router.app" pnpm run build:macos
```

Windows（Developer PowerShell，需要 Windows App SDK 与 VS C++ 桌面工作负载）：`pnpm install --frozen-lockfile && pnpm run build:windows`。

源码构建还需要 Xcode、CocoaPods、Node.js 22+、pnpm 11 和 [uv](https://docs.astral.sh/uv/)。

</details>

## 配置

可编辑配置：`~/.young-router/config.yaml`（脱敏示例：[`config.example.yaml`](./config.example.yaml)）。所有修改都会先校验再写入，需要重启的改动由服务自行重载；只有供应商向导、原始文件编辑器与分组管理会把修改暂存到各自的保存 / 关闭动作。超时、冷却、端口、识图 / 搜索 / 电脑操作等运行时项都在**运行时**中调整；每个部署的能力标志（`upstream_protocol_mode`、`upstream_url_surface`、`supports_responses_web_search`、`supports_responses_image_generation_tool`、`supports_responses_compaction`）在示例文件中有说明。

<details>
<summary>导入导出、同步、深链、开发</summary>

- **导入导出：** 可从 Codex / Claude 设置、文件或 New API / CC Switch 链接导入供应商，按配置段导出 JSON；确认导入的配置段会立即写入。**备份与同步**中提供可选的 WebDAV 智能同步（推送 / 拉取 / 合并）。
- **深链：** 直接打开指定页面——`open "young-router://open/<route>"`，可用 `home`、`providers-models`、`provider-wizard`、`codex-settings`、`claude-settings`、`runtime-settings`、`relay-accounts`、`data-management`、`logs?tab=…`（`requests`、`service`、`route-trace`、`recovery`、`online-usage`）。深链不能携带配置值，也不会触发配置写入。
- **开发：** 贡献者指南见 [`AGENTS.md`](./AGENTS.md)；聚焦测试位于 `tests/`，共享 UI 在 `rn/` 下运行 `pnpm exec tsc --noEmit`。`scripts/version.py` 负责同步版本与 Cask；构建时会自动将 `LITELLM_VERSION` 推进到最新稳定版，并打包匹配的 pi-web-access 与 Node.js 22 运行时。

</details>

## 许可证

MIT 许可证。见 [LICENSE](./LICENSE)。
