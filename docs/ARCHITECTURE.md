# Architecture / 架构

This document records the current ownership boundaries and a safe path for
incremental cleanup. It describes the code as it exists today and keeps the
runtime and IPC contracts explicit while modules are reorganized.

本文说明当前代码归属、允许的依赖方向和渐进式整理顺序，并在模块整理期间明确保持运行与 IPC 契约不变。

## System Shape / 系统形态

```text
macOS AppKit host / Windows WinUI host
        | native lifecycle, tray/menu, secure controls, file panels
        v
rn/packages/shared
        | shared React Native UI, i18n, typed IPC client, view state
        v
litellm_menu/core
        | staged domains, validation, service operations, persistence, IPC
        v
configuration and compatibility adapters
        | config_editor_core, codex_config, runtime_settings_io, webdav, etc.

LiteLLM proxy hook (litellm_menu/ outside core)
        | routing, Responses bridges, streaming, trace and tool adaptation
        v
LiteLLM runtime
```

The native hosts start and supervise Core. Core is the sole owner of mutable
application state and configuration writes. The React UI is a consumer of
versioned snapshots and actions, not a second application backend.

原生宿主负责启动和监管 Core；Core 是可变应用状态与配置写入的唯一所有者。React UI
只消费带版本的快照和操作，不能成为第二个后端或配置写入层。

## Directory Ownership / 目录归属

| Path | Owner and responsibility |
| --- | --- |
| `rn/packages/shared/` | Shared React/TypeScript UI, routes, view state, i18n, typed Core IPC client, and cross-platform native-control adapters. |
| `rn/apps/macos/` | AppKit host and macOS-only leaves: status menu, windows, CodeMirror WebView host, secure inputs, file panels, shortcuts, Core bridge, and Vision helper source. |
| `rn/apps/windows/` | WinUI 3 host and Windows-only leaves: tray, windows, CodeMirror WebView2 host, secure inputs, Core bridge, and packaging integration. |
| `litellm_menu/core/` | Authoritative domain state, validation, staged Apply workflow, service control, persistence, security filtering, and versioned loopback IPC. |
| `litellm_menu/core/domains/` | Focused Core domain adapters. Providers & Models, Codex, Runtime, WebDAV, Claude, logs, language, and relay accounts each have an owning module; `_shared.py` contains only private cross-domain helpers. |
| `litellm_menu/` outside `core/` | LiteLLM proxy extension: routing, stream handling, Responses compatibility, tool bridges, tracing, and hook registration. |
| `config_editor_core/`, `webdav/`, root-level `*_config.py`/`*_io.py` modules | Existing parser, persistence, import, billing, and sync adapters used by Core. They remain part of the bundled Core surface today. |
| `scripts/`, `rn/scripts/` | Test, version, build, runtime, and release packaging automation. |
| `tests/` | Python behavior, IPC, UI parity, host acceptance, and packaging coverage. |

The root-level adapters look scattered, but they are not unowned code. macOS
and Windows packaging currently copy and verify their present paths, so moving
them without a coordinated packaging change will break portable builds.

根目录适配器看起来分散，但并非无主代码。macOS 和 Windows 打包脚本会复制并校验这些
现有路径；在同步修改打包链路前移动它们会破坏可移植构建。

## Dependency Rules / 依赖规则

Allowed direction:

```text
native host -> shared UI -> typed IPC -> Core -> domain adapter
proxy hook -> LiteLLM runtime
build scripts -> source and bundle layout
```

- Shared UI may call typed IPC and native leaf capabilities only. Outside the
  explicit versioned code-editor method, it must not read files, write
  configuration, manage the proxy, or receive raw secrets.
- Raw configuration documents travel through the authenticated, versioned
  editor IPC into the shared CodeMirror UI. Native leaves own the system
  WebView hosts and secret-input platform APIs; credential fields still use
  opaque, one-time Core capabilities rather than React props.
- This CodeMirror path intentionally supersedes the repository's former
  native-only raw-editor boundary; the obsolete secure-editor and host-editor
  endpoints are removed rather than retained as compatibility paths.
- Core may use configuration adapters and service operations. It must not
  import React Native or take ownership of macOS/Windows presentation logic.
- Proxy-hook modules may depend on LiteLLM-compatible runtime behavior. They
  are separate from desktop state management and must not become UI domains.
- A feature has one shared UI and one Core domain implementation. Platform
  hosts may render native controls, but must not duplicate domain stores or
  configuration writers.

允许依赖应从宿主流向共享 UI、IPC、Core 和领域适配器。禁止反向依赖，也禁止绕过 Core
直接从 React 或原生窗口写配置。

## Current Architecture / 当前架构

- The desktop shell has one shared React Native UI with AppKit/WinUI leaves.
  Code panes use embedded system WebViews for CodeMirror, but there is no
  WebView application shell, Electron shell, or parallel legacy UI.
- Core now provides the staged state source and authenticated IPC contract.
  Configuration logic stays in focused domain adapters instead of being
  duplicated in the UI.
- Providers & Models, Codex, Runtime, and WebDAV each have focused domain
  modules. Production Core wiring and tests import those modules directly.
- Routes and log tabs are intentionally allowlisted in Core, shared TypeScript,
  and native hosts. Keep those boundaries explicit while consolidating shared
  literals; do not weaken native input validation merely to reduce repetition.

## Safe Refactor Plan / 安全整理计划

1. **Stabilize current UI work.** Finish menu, settings-tab, log-entry, and
   localization behavior with focused RN/IPC/native tests. Do not combine this
   with broad file moves.
2. **Split Core domains (complete).** `providers_models.py`, `codex.py`,
   `runtime.py`, and `webdav.py` now own their adapters. `_shared.py` owns only
   private mechanical helpers, and all Core wiring uses the focused modules.
3. **Split shared UI by screen.** Keep `LiteLLMMenuApp.tsx` as the application
   shell, snapshot subscription, and route dispatch. Move screen components,
   reusable field components, and styles into explicit `ui/screens/`,
   `ui/components/`, and `ui/styles/` modules without changing IPC actions.
4. **Keep boundary catalogs explicit.** Retain the native allowlists and make
   focused behavior changes in TypeScript, Core, and both native hosts together.
   Do not expose unrestricted routes merely to remove duplicated literals.
5. **Unify Core packaging before root moves.** Coordinate macOS and Windows
   packaging code before moving root-level adapters under a clearly owned Core
   support package, then update ordinary imports and focused packaging tests.

Each phase should be independently reviewable, preserve the IPC schema unless
an intentional API change is made, and run the focused type and behavior tests
for the shared UI, IPC, or native host that changed.

## Non-Goals / 非目标

- Do not add a platform-specific configuration store, a second settings UI,
  Electron/Tauri application shell, or compatibility launcher. Embedded
  system WebViews remain limited to shared CodeMirror code panes.
- Do not move files solely for visual neatness while package scripts, native
  startup checks, or public imports still name their current paths.
- Do not relax path or credential-field capability boundaries to simplify
  component APIs.
