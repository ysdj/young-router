from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "rn/packages/shared/src"
MAC_NATIVE = ROOT / "rn/apps/macos/src/native/macos"
WIN_NATIVE = ROOT / "rn/apps/windows/src/native/windows"
MAC_PROJECT = ROOT / "rn/apps/macos/macos"
WIN_PROJECT = ROOT / "rn/apps/windows/windows/LiteLLMMenu"


class ReactNativeNativeAcceptanceTests(unittest.TestCase):
    def test_hosts_shutdown_managed_service_through_authenticated_host_route(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        for source in (mac, windows):
            self.assertIn("host/shutdown", source)
            self.assertIn("Authorization", source)
        self.assertIn("private static let coreShutdownTimeout: TimeInterval = 4", mac)
        self.assertIn("timeoutInterval: Self.coreShutdownTimeout", mac)
        self.assertIn("shutdown_token, 4000", windows)

    def test_macos_upgrade_stops_the_proxy_before_replacing_the_bundle(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")

        self.assertNotIn("hasActiveServiceUpgradeLease", mac)
        self.assertNotIn("preserve-service-lease-file", mac)
        self.assertIn("if let shutdownEndpoint, let shutdownToken", mac)

    def test_hosts_require_the_portable_callback_bootstrap(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        self.assertIn('appendingPathComponent("sitecustomize.py")', mac)
        self.assertIn('L"sitecustomize.py"', windows)

    def test_windows_close_is_requested_through_shared_react_before_hiding(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")

        self.assertIn("message == WM_CLOSE && !quitting_", leaf)
        self.assertIn('DispatchAction("request-close-" + WideToUtf8(active_route_));', leaf)
        self.assertNotIn("CloseAll();", leaf)
        self.assertNotIn("CloseAll", leaf)
        self.assertIn('nativeAction?.id !== `request-close-${route}`', ui)
        self.assertIn("kQuitMessage", leaf)
        self.assertIn("CoreIPCBridge::Shared().Stop()", leaf)

    def test_windows_providers_route_uses_the_shared_react_host_and_generic_resize(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")
        module_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")

        for legacy_symbol in (
            "ProvidersWindowState",
            "ProvidersWindowProc",
            "ShowProvidersWindow",
            "RenderProvidersWindow",
            "RequestProvidersSnapshot",
            "providers_window_",
            'L"winui-providers-"',
        ):
            self.assertNotIn(legacy_symbol, leaf)
            self.assertNotIn(legacy_symbol, header)
        open_route = leaf.split("void WinUI3NativeLeaf::OpenRoute", 1)[1].split(
            "void WinUI3NativeLeaf::CloseRoute", 1
        )[0]
        self.assertNotIn('if (route == L"providers-models")', open_route)
        self.assertNotIn("CoreIPCBridge::Shared().Send(", leaf)
        self.assertIn("bool SetWindowContentSize(std::wstring_view route, double width, double height);", header)
        self.assertIn("bool WinUI3NativeLeaf::SetWindowContentSize(std::wstring_view route, double width, double height)", leaf)
        self.assertIn("std::isfinite(width)", leaf)
        self.assertIn("kMaximumContentExtent", leaf)
        self.assertIn("AdjustWindowRectExForDpi", leaf)
        self.assertIn('REACT_METHOD(SetWindowContentSize, L"setWindowContentSize")', module_header)
        self.assertIn("void SetWindowContentSize(", module_header)
        self.assertIn("void WinUI3NativeLeafModule::SetWindowContentSize(", module)
        self.assertIn("leaf->SetWindowContentSize(route, width, height)", module)

    def test_windows_window_minimums_follow_responsive_route_constraints_in_dpi_corrected_frame_pixels(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")

        self.assertIn("POINT MinimumTrackSizeForActiveRoute() const;", header)
        self.assertIn("ContentSize RouteMinimumContentSize(std::wstring_view route)", leaf)
        for route, width, height in (
            ("providers-models", 780, 560),
            ("runtime-settings", 800, 520),
            ("data-management", 500, 160),
            ("relay-accounts", 760, 500),
            ("relay-add", 720, 560),
            ("logs", 640, 420),
        ):
            self.assertIn(f'route == L"{route}") return {{{width}, {height}}};', leaf)
        self.assertIn('route == L"codex-settings" || route == L"claude-settings"', leaf)
        self.assertIn("return {1100, 640};", leaf)
        self.assertIn("MinimumTrackSizeForActiveRoute();", leaf)
        self.assertIn("DipToPhysicalPixels", leaf)
        self.assertIn("AdjustWindowRectExForDpi", leaf)
        self.assertNotIn("ptMinTrackSize.x = std::max<LONG>(minmax->ptMinTrackSize.x, 1080);", leaf)

    def test_native_routes_use_responsive_initial_content_sizes(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("ContentSize RouteInitialContentSize", leaf)
        for size in ("{780, 560}", "{1160, 700}", "{1080, 620}", "{520, 175}", "{900, 680}", "{920, 620}", "{900, 580}"):
            self.assertIn(size, leaf)
        self.assertIn("RouteInitialContentSize(route)", leaf)

    def test_macos_file_capability_exchange_never_blocks_appkit(self) -> None:
        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")

        capability = bridge.split("func registerFileCapability(", 1)[1].split("struct RelayLoginResult", 1)[0]
        self.assertIn("DispatchQueue.global(qos: .userInitiated).async", capability)
        self.assertIn("DispatchQueue.main.async { completion(token) }", capability)
        self.assertIn("beginSheetModal(for: owner", leaf)
        self.assertNotIn("panel.runModal()", leaf)
        self.assertIn("chooseImportFile(purpose: purpose) { token in resolve(token) }", module)
        self.assertIn("chooseExportFile(suggestedName: suggestedName) { token in resolve(token) }", module)

    def test_standalone_configuration_package_route_is_not_accepted_by_native_hosts(self) -> None:
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        win_leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        mac_app = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        win_app = (WIN_PROJECT / "LiteLLMMenu.cpp").read_text(encoding="utf-8")

        for source in (mac_leaf, win_leaf, mac_app, win_app):
            self.assertNotIn("configuration-package", source)
            self.assertNotIn("open-configuration-package", source)
            self.assertNotIn("routeConfigurationPackage", source)

        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        win_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        win_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        self.assertIn("saveFilePicker", mac_module)
        self.assertIn("saveFilePicker", mac_bridge)
        self.assertIn("chooseExportFile", mac_leaf)
        self.assertIn("SaveFilePicker", win_module)
        self.assertIn("SaveFilePicker", win_header)

    def test_macos_route_geometry_matches_the_responsive_constraints(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")

        for width, height, min_width, min_height in (
            (780, 460, 780, 460),
            (1160, 700, 1100, 640),
            (1080, 620, 800, 520),
            (500, 160, 500, 190),
            (900, 680, 720, 560),
            (900, 580, 640, 420),
        ):
            self.assertIn(f"contentSize: NSSize(width: {width}, height: {height})", leaf)
            self.assertIn(f"minSize: NSSize(width: {min_width}, height: {min_height})", leaf)
        relay_layout = leaf.split('case "relay-accounts":', 1)[1].split('case "relay-add":', 1)[0]
        self.assertIn("contentSize: NSSize(width: 960, height: 620)", relay_layout)
        self.assertIn("minSize: NSSize(width: 860, height: 540)", relay_layout)
        self.assertNotIn('case "configuration-package":', leaf)
        self.assertNotIn("maxSize: NSSize(width: 680, height: 386)", leaf)

    def test_windows_tray_left_click_does_not_reinterpret_menu_index_zero(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")

        self.assertIn("void DispatchDefaultTrayAction();", header)
        self.assertIn("void DispatchTrayAction(size_t index);", header)
        self.assertIn("DispatchDefaultTrayAction();", leaf)
        self.assertIn(
            "DispatchTrayAction(static_cast<size_t>(LOWORD(wparam) - kTrayMenuFirstCommand));",
            leaf,
        )
        self.assertNotIn("if (command == 0)", leaf)

    def test_native_trays_hide_the_retired_recovery_menu_action(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn('"open-logs", "separator"', mac)
        self.assertNotIn('"open-recovery", "open-logs"', mac)
        self.assertIn('"open-claude-settings", "open-recovery",', mac)
        self.assertIn('action.id != L"open-claude-settings" && action.id != L"open-recovery"', windows)

    def test_native_trays_keep_the_recovery_log_action_as_one_logs_entry(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")

        self.assertIn('"open-logs", "separator"', mac)
        self.assertIn('case "open-logs", "open-logs?tab=recovery": openLogs(tab:', mac)

    def test_native_trays_do_not_append_ellipses_to_direct_window_links(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        for route in (
            "open-providers-models",
            "open-relay-accounts",
            "open-runtime-settings",
            "open-codex-settings",
            "open-data-management",
        ):
            self.assertNotRegex(mac, rf'case "{route}"[^\n]+\+ "\.\.\."')
        self.assertNotIn('localized("routeRelayAccounts", fallback: "Relay Accounts") + "..."', mac)
        self.assertNotIn('title += L"...";', windows)

    def test_compatibility_claude_route_uses_the_combined_settings_window_title(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn('case "codex-settings", "claude-settings": return localized("routeCodexSettings", fallback: "Codex / Claude Settings")', mac)
        self.assertIn('route == L"codex-settings" || route == L"claude-settings"', windows)
        self.assertIn('Localized("routeCodexSettings", L"Codex / Claude Settings")', windows)

    def test_macos_settings_shortcut_opens_the_combined_settings_surface(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        shortcut = mac.split('if shortcuts["openMenu"]?.lowercased().contains("cmd+,") == true {', 1)[1].split('if shortcuts["reload"]', 1)[0]

        self.assertIn("action: #selector(openCodex)", shortcut)
        self.assertNotIn("action: #selector(openRuntime)", shortcut)

    def test_native_trays_show_a_status_header_and_checked_toggle_actions(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn('item.state = checked ? .on : .off', mac)
        status_item = mac.split("private func configureStatusMenuItem", 1)[1].split("private func ensureSystemEditMenu", 1)[0]
        self.assertIn("item.action = nil", status_item)
        self.assertIn("item.target = nil", status_item)
        self.assertIn("item.isEnabled = false", status_item)
        self.assertIn(".foregroundColor: NSColor.secondaryLabelColor", status_item)
        self.assertIn('AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, status_title_.c_str());', windows)
        self.assertIn('auto add_separator = [&menu, &needs_separator]()', windows)
        self.assertIn('action.id == L"open-providers-models" || action.id == L"webdav-status" ||', windows)
        self.assertIn('action.id == L"open-data-management" || action.id == L"open-logs" ||', windows)
        self.assertIn('menu.autoenablesItems = false', mac)
        self.assertNotIn('"webdav-status", "webdav-toggle"', mac)

    def test_native_trays_keep_language_controls_reachable_and_hide_auxiliary_lifecycle_actions(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn('"service-start", "service-stop", "service-restart", "service-reload", "service-health",', mac)
        self.assertIn('action.id != L"service-start"', windows)
        self.assertIn('HMENU language_menu = CreatePopupMenu();', windows)
        self.assertIn('if (is_language_choice) {', windows)

    def test_windows_uses_app_sdk_single_instance_and_hot_protocol_routing(self) -> None:
        source = (WIN_PROJECT / "LiteLLMMenu.cpp").read_text(encoding="utf-8")
        pch = (WIN_PROJECT / "pch.h").read_text(encoding="utf-8")

        self.assertIn("Microsoft.Windows.AppLifecycle.h", pch)
        self.assertIn("FindOrRegisterForKey", source)
        self.assertIn("RedirectActivationToAsync", source)
        self.assertIn("mainInstance.Activated", source)
        self.assertIn('DispatchAction("open-"', source)
        self.assertIn("ExtendedActivationKind::Protocol", source)
        self.assertIn("AllowedLogTab", source)
        self.assertIn('L"?tab="', source)
        self.assertIn('writer.WritePropertyName(L"initialLogTab")', source)
        self.assertNotIn("config-watch", source)

    def test_macos_prevents_duplicate_direct_bundle_launches_before_appkit_starts(self) -> None:
        source = (MAC_PROJECT / "LiteLLMMenu-macOS/main.m").read_text(encoding="utf-8")
        plist = (MAC_PROJECT / "LiteLLMMenu-macOS/Info.plist").read_text(encoding="utf-8")

        self.assertIn("LSMultipleInstancesProhibited", plist)
        self.assertIn("LiteLLMMenuExistingInstance", source)
        self.assertIn("NSWorkspace.sharedWorkspace.runningApplications", source)
        self.assertIn("LiteLLMMenuIsManagedApplication", source)
        self.assertIn("LiteLLMMenuAcquireInstanceLock", source)
        self.assertIn("NSApplicationSupportDirectory", source)
        self.assertIn('kLiteLLMMenuInstanceNamespace = @"menu.litellm.menu"', source)
        self.assertIn("Contents/MacOS/LiteLLMMenu", source)
        self.assertNotIn("NSTemporaryDirectory", source)
        self.assertIn("flock(descriptor, LOCK_EX | LOCK_NB)", source)
        self.assertIn("NSApplicationMain", source)
        self.assertLess(source.index("LiteLLMMenuAcquireInstanceLock"), source.rindex("NSApplicationMain"))

    def test_macos_starts_the_hidden_primary_host_for_live_menu_state(self) -> None:
        source = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        launch = source.split("- (void)applicationDidFinishLaunching:", 1)[1].split(
            "- (void)startReactHostWhenNeeded", 1
        )[0]

        self.assertIn("self.automaticallyLoadReactNativeWindow = NO;", launch)
        self.assertIn("[nativeLeaf setReactHostStarter:^{", launch)
        self.assertIn("[self startReactHostWhenNeeded];", launch)
        self.assertLess(
            launch.index("[nativeLeaf setReactHostStarter:^{"),
            launch.rindex("[self startReactHostWhenNeeded];"),
        )

    def test_core_replacement_waits_for_the_previous_process_to_exit(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        self.assertIn("private func stopCoreProcess", mac)
        self.assertIn('"--parent-pid"', mac)
        self.assertIn("String(ProcessInfo.processInfo.processIdentifier)", mac)
        self.assertIn("let deadline = Date().addingTimeInterval(1)", mac)
        self.assertIn("while process.isRunning && Date() < deadline", mac)
        self.assertIn("Darwin.kill(process.processIdentifier, SIGKILL)", mac)
        self.assertIn("process.waitUntilExit()", mac)
        self.assertLess(mac.index("Darwin.kill(process.processIdentifier, SIGKILL)"), mac.index("process.waitUntilExit()"))
        self.assertIn("stopCoreProcess(staleProcess, directory: staleDirectory)", mac)
        self.assertIn("WaitForSingleObject(process, INFINITE);", windows)
        self.assertIn('L" --parent-pid " + std::to_wstring(GetCurrentProcessId())', windows)
        self.assertIn("DWORD const grace_result = WaitForSingleObject(process, 8000);", windows)
        self.assertIn("if (grace_result != WAIT_OBJECT_0)", windows)
        self.assertIn("StopCoreProcess(process.hProcess, directory);", windows)
        self.assertNotIn("WaitForSingleObject(process.hProcess, INFINITE);", windows)

    def test_macos_core_bootstrap_waits_only_for_the_control_endpoint(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        start = mac.split("    private func startCoreLocked() throws -> Endpoint {", 1)[1].split(
            "    private func previewProfileEnvironment", 1
        )[0]

        self.assertIn("private static let coreStartupTimeout: TimeInterval = 10", mac)
        self.assertIn("Date().addingTimeInterval(Self.coreStartupTimeout)", start)
        self.assertIn("guard process.isRunning else { break }", start)
        self.assertIn("stopCoreProcess(process, directory: directory)", start)

    def test_macos_core_never_mutates_the_signed_bundle_with_bytecode(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")

        self.assertIn('childEnvironment["PYTHONDONTWRITEBYTECODE"] = "1"', mac)

    def test_macos_event_poll_retries_without_replacing_core(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        poll = mac.split("    private func poll(subscription: String, generation: Int) {", 1)[1].split(
            "    private func requestMetadata", 1
        )[0]

        self.assertIn("let retry = !pollCancelled", poll)
        self.assertIn("&& subscriptionID == subscription", poll)
        self.assertIn("&& self.generation == generation", poll)
        self.assertIn("guard retry else { return }", poll)
        self.assertIn("Thread.sleep(forTimeInterval: 1)", poll)
        self.assertNotIn("resetCore(expectedGeneration: generation)", poll)
        self.assertNotIn("scheduleSubscriptionRecovery()", poll)

    def test_hosts_renew_an_unexpired_ipc_session_before_replacing_core(self) -> None:
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        self.assertIn("private func exchangeSession", mac)
        self.assertIn("exchangeSession(endpoint: endpoint, credential: sessionToken)", mac)
        self.assertIn("if let endpoint, let sessionToken,", mac)
        self.assertIn("renewal_token = session_token_", windows)
        self.assertIn('Request(renewal_endpoint, L"hello", L"POST", "", renewal_token)', windows)
        self.assertNotIn("return EnsureSession();", windows)

    def test_native_code_webviews_sync_react_editor_state_without_host_editor_endpoints(self) -> None:
        mac_controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows_controls = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeCodeWebViewNativeComponent.ts").read_text(encoding="utf-8")
        windows_spec = (SHARED / "ui/windows/NativeCodeWebViewNativeComponent.ts").read_text(encoding="utf-8")
        wrapper = (SHARED / "ui/code-editor/CodeEditorWebView.tsx").read_text(encoding="utf-8")
        core_ipc = (ROOT / "litellm_menu/core/ipc.py").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_bridge = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        native_controls = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        appkit_controls = (SHARED / "ui/AppKitControls.tsx").read_text(encoding="utf-8")

        for spec in (mac_spec, windows_spec):
            for marker in (
                "html: string;",
                "documentKey: string;",
                "value: string;",
                "baseline: string;",
                "language: string;",
                "showDiff?: WithDefault<boolean, false>;",
                "onEditorChange?: DirectEventHandler<EditorChangeEvent>;",
            ):
                self.assertIn(marker, spec)
        self.assertIn("<NativeCodeWebView", wrapper)
        self.assertIn("onEditorChange", wrapper)

        for marker in (
            "WKWebView *_webView;",
            'name:@"litellmCodeEditor"',
            "configuration.processPool = LiteLLMCodeEditorProcessPool();",
            "recoverEditorPageWithError",
            "_htmlStateGeneration == _editorStateGeneration",
            "window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive",
            "onEditorChange(event)",
            "prepareForRecycle",
        ):
            self.assertIn(marker, mac_controls)
        for marker in (
            "struct CodeWebViewComponentView final",
            "WebView2{}",
            "NavigateToString(ToHString(props.html))",
            "ExecuteScriptAsync(script)",
            "WebMessageReceived",
            "RecoverEditorPage(\"page_load_failed\")",
            "html_state_generation_ == editor_state_generation_",
            "onEditorChange(std::move(event))",
        ):
            self.assertIn(marker, windows_controls)
        for obsolete in (
            "/v1/host/editor/read",
            "/v1/host/editor/stage",
            "ReadEditorDocument",
            "StageEditorDocument",
            "RefreshEditorDocument",
        ):
            self.assertNotIn(obsolete, core_ipc)
            self.assertNotIn(obsolete, mac_bridge)
            self.assertNotIn(obsolete, windows_bridge)
        self.assertNotIn("def read_editor(", core_ipc)
        self.assertNotIn("def stage_editor(", core_ipc)
        self.assertNotIn("NativeSecureTextEditor", native_controls)
        self.assertNotIn("SecureTextEditor", appkit_controls)
        self.assertNotIn("SecureTextEditorComponentView", mac_controls)
        self.assertNotIn("SecureTextEditorComponentView", windows_controls)
        self.assertFalse((SHARED / "ui/macos/NativeSecureTextEditorNativeComponent.ts").exists())
        self.assertFalse((SHARED / "ui/windows/NativeSecureTextEditorNativeComponent.ts").exists())

    def test_macos_structured_settings_keep_a_real_persistent_scroll_indicator(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        header = (MAC_NATIVE / "AppKitControlViews.h").read_text(encoding="utf-8")
        appkit = (SHARED / "ui/AppKitControls.tsx").read_text(encoding="utf-8")
        native = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        spec = (SHARED / "ui/macos/NativePersistentScrollIndicatorNativeComponent.ts").read_text(encoding="utf-8")

        self.assertIn("LiteLLMAppKitPersistentScrollIndicatorComponentView", header)
        self.assertIn('"LiteLLMAppKitPersistentScrollIndicator"', spec)
        self.assertIn("AppKitPersistentScrollIndicator", appkit)
        self.assertIn("NativePersistentScrollIndicator", native)
        component = controls.split("@implementation LiteLLMAppKitPersistentScrollIndicatorComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitPersistentScrollIndicatorCls",
            1,
        )[0]
        self.assertIn("[view isKindOfClass:NSScrollView.class]", component)
        self.assertIn("scrollView.scrollerStyle = NSScrollerStyleLegacy;", component)
        self.assertIn("scrollView.autohidesScrollers = NO;", component)
        self.assertIn("scrollView.scrollerStyle = NSScrollerStyleOverlay;", component)
        self.assertIn("scrollView.autohidesScrollers = YES;", component)

    def test_native_login_item_registration_follows_core_target_state(self) -> None:
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")

        self.assertIn("native.setLaunchAtLogin(enabled)", ui)
        self.assertIn('type: enabled ? "service.autostart_disable"', ui)
        self.assertIn("setLaunchAtLogin(_ enabled: Bool)", mac)
        self.assertIn("SetLaunchAtLogin(bool enabled)", windows)
        self.assertNotIn("toggleLaunchAtLogin", mac)
        self.assertNotIn("ToggleLaunchAtLogin", windows)
        self.assertIn("setLaunchAtLogin?: (enabled: boolean) => Promise<boolean>", platform)

    def test_macos_codex_catalog_toggle_uses_a_separate_non_modal_restart_confirmation(self) -> None:
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")

        self.assertIn('id: "toggle-codex-model-catalog"', ui)
        self.assertIn('checked: booleanValue(catalog.enabled)', ui)
        self.assertIn('type: "codex.model_catalog.set"', ui)
        self.assertNotIn("CodexRestartNotice", ui)
        self.assertIn("native.showCodexRestartConfirmation({", ui)
        self.assertIn("await native.restartCodex()", ui)
        self.assertIn('"toggle-autostart", "toggle-codex-model-catalog", "separator"', leaf)
        self.assertIn('withBundleIdentifier: "com.openai.codex"', leaf)
        self.assertIn("func restartCodex() -> Bool", leaf)
        restart = leaf.split("func restartCodex() -> Bool", 1)[1].split("func systemLocale", 1)[0]
        self.assertIn("application.forceTerminate()", restart)
        self.assertNotIn("application.terminate()", restart)
        confirmation = leaf.split("func showCodexRestartConfirmation(", 1)[1].split(
            "@objc private func selectCodexRestartLater", 1
        )[0]
        self.assertIn("NSPanel(", confirmation)
        self.assertIn("panel.isFloatingPanel = true", confirmation)
        self.assertNotIn("runModal", confirmation)
        self.assertIn("func showCodexRestartConfirmation", module)
        self.assertIn("@objc func restartCodex", module)
        self.assertIn("showCodexRestartConfirmation", bridge)
        self.assertIn("RCT_EXTERN_METHOD(restartCodex:", bridge)
        self.assertIn("showCodexRestartConfirmation?:", platform)
        self.assertIn("restartCodex?: () => Promise<boolean>", platform)

    def test_shared_ui_owns_lifecycle_menu_actions_startup_and_safe_recovery(self) -> None:
        """Both native leaves route lifecycle commands through one React IPC path."""
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        for action, operation in (
            ("service-start", "start"),
            ("service-stop", "stop"),
            ("service-restart", "restart"),
            ("service-reload", "reload"),
            ("service-health", "health"),
        ):
            self.assertIn(f'case "{action}": return "{operation}";', ui)
            self.assertIn(f'{{ id: "{action}",', ui)

        # Native leaves retain generic bridge routing; the compact status menu
        # intentionally follows the product menu and omits these auxiliary
        # lifecycle actions.
        self.assertIn("default: emitAction(id)", mac)
        self.assertIn('"service-start", "service-stop", "service-restart", "service-reload", "service-health",', mac)

        # WinUI dispatch remains generic for retained menu actions.
        self.assertIn("DispatchAction(WideToUtf8(item.id));", windows)
        self.assertIn('action.id != L"service-start"', windows)

        self.assertIn('await ipc.dispatch({ type: `service.${operation}` });', ui)
        self.assertIn("return await refreshSnapshot();", ui)
        self.assertIn('if (snapshot.service.state === "stopped") void runServiceOperation("start");', ui)
        self.assertIn('if (operation === "stop") serviceShouldBeRunning.current = false;', ui)
        self.assertNotIn("SERVICE_HEALTH_POLL_MS", ui)
        self.assertNotIn("SERVICE_RECOVERY_RETRY_MS", ui)
        self.assertNotIn("pollServiceHealth", ui)
        self.assertIn('void runServiceOperation("start");', ui)

        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        self.assertIn("public func warm()", bridge)
        self.assertIn("[CoreIPCBridge.shared warm];", app_delegate)

    def test_macos_deep_links_allow_only_the_logs_tab_parameter(self) -> None:
        app_delegate = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        info = (MAC_PROJECT / "LiteLLMMenu-macOS/Info.plist").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")

        self.assertIn('objectForInfoDictionaryKey:@"LiteLLMMenuRouteScheme"', app_delegate)
        self.assertIn('routeScheme = @"litellm-menu"', app_delegate)
        self.assertIn('[[url scheme] isEqualToString:routeScheme]', app_delegate)
        self.assertIn("<key>LiteLLMMenuRouteScheme</key>", info)
        self.assertIn("<string>litellm-menu</string>", info)
        self.assertIn("items.count == 1", app_delegate)
        self.assertIn('[item.name isEqualToString:@"tab"]', app_delegate)
        self.assertIn("openRouteFromDeepLink:route logTab:logTab", app_delegate)
        self.assertIn("route == \"logs\" && isAllowedLogTab", leaf)
        self.assertIn("initialLogTab: logTab", leaf)
        self.assertIn("@\"initialLogTab\"", app_delegate)
        self.assertIn('emitAction("open-logs?tab=', leaf)
        self.assertNotIn("config-watch", app_delegate)
        self.assertNotIn("config-watch", leaf)
        self.assertNotIn('language-settings', app_delegate)
        self.assertNotIn('language-settings', leaf)

    def test_macos_reopen_shows_the_primary_configuration_window(self) -> None:
        app_delegate = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")

        self.assertIn("applicationShouldHandleReopen", app_delegate)
        self.assertIn("hasVisibleWindows", app_delegate)
        self.assertIn('openRouteFromDeepLink:@"providers-models" logTab:nil', app_delegate)
        self.assertIn('representedObject = "native-open-relay-accounts"', leaf)
        self.assertIn("action: #selector(openRelayAccounts)", leaf)
        self.assertIn('representedObject = "native-open-data-management"', leaf)
        self.assertIn("action: #selector(openDataManagement)", leaf)
        self.assertIn("dataManagementItem.keyEquivalentModifierMask = [.command, .shift]", leaf)

    def test_macos_bundle_prohibits_duplicate_menu_bar_instances(self) -> None:
        """A second launch must activate the existing app, not add another LL item."""

        info = (MAC_PROJECT / "LiteLLMMenu-macOS/Info.plist").read_text(encoding="utf-8")
        main = (MAC_PROJECT / "LiteLLMMenu-macOS/main.m").read_text(encoding="utf-8")

        self.assertIn("<key>LSMultipleInstancesProhibited</key>", info)
        self.assertIn("<true/>", info.split("<key>LSMultipleInstancesProhibited</key>", 1)[1].split("</dict>", 1)[0])
        self.assertIn("NSWorkspace.sharedWorkspace.runningApplications", main)
        self.assertIn("LiteLLMMenuIsManagedApplication", main)
        self.assertIn("application.processIdentifier != currentPID", main)
        self.assertIn("activateWithOptions", main)
        self.assertLess(main.index("LiteLLMMenuExistingInstance()"), main.index("NSApplicationMain(argc, argv)"))

    def test_macos_preview_metadata_keeps_the_production_instance_identity(self) -> None:
        build = (ROOT / "rn/scripts/build-macos.sh").read_text(encoding="utf-8")
        main = (MAC_PROJECT / "LiteLLMMenu-macOS/main.m").read_text(encoding="utf-8")

        self.assertIn("LITELLM_MENU_MACOS_BUNDLE_IDENTIFIER is unsupported", build)
        self.assertIn("LITELLM_MENU_MACOS_DISPLAY_NAME", build)
        self.assertIn("LITELLM_MENU_MACOS_ROUTE_SCHEME", build)
        self.assertIn("LITELLM_MENU_MACOS_PREVIEW_PROFILE_ROOT", build)
        self.assertIn("LITELLM_MENU_MACOS_PREVIEW_PORT", build)
        self.assertIn("LiteLLMMenuPreviewProfileRoot", build)
        self.assertIn("LiteLLMMenuPreviewPort", build)
        self.assertIn('PREVIEW_BUNDLE_IDENTIFIER="menu.litellm.menu"', build)
        self.assertIn("Set :CFBundleIdentifier $PREVIEW_BUNDLE_IDENTIFIER", build)
        self.assertIn('kLiteLLMMenuInstanceNamespace = @"menu.litellm.menu"', main)
        self.assertNotIn("distinct preview bundle", main)

    def test_macos_preview_profile_is_embedded_without_a_second_instance_identity(self) -> None:
        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")

        self.assertIn("previewProfileEnvironment", bridge)
        self.assertIn('guard let rawRoot = Bundle.main.object(forInfoDictionaryKey: "LiteLLMMenuPreviewProfileRoot")', bridge)
        self.assertNotIn('Bundle.main.bundleIdentifier != "menu.litellm.menu"', bridge)
        self.assertIn('"LiteLLMMenuPreviewProfileRoot"', bridge)
        self.assertIn('"LiteLLMMenuPreviewPort"', bridge)
        for key in (
            "LITELLM_RUNTIME_ROOT",
            "LITELLM_CONFIG_FILE",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "LITELLM_MENU_RUNTIME_SETTINGS_FILE",
        ):
            self.assertIn(f'environment["{key}"]', bridge)

    def test_macos_uses_the_monochrome_double_l_menu_icon_asset(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        icon_contents = (
            MAC_PROJECT
            / "LiteLLMMenu-macOS/Assets.xcassets/MenuIcon.imageset/Contents.json"
        ).read_text(encoding="utf-8")

        self.assertIn('NSImage(named: NSImage.Name("MenuIcon"))!', leaf)
        self.assertIn("image.size = NSSize(width: 20, height: 20)", leaf)
        self.assertIn("image.isTemplate = true", leaf)
        self.assertNotIn('("L" as NSString).draw(', leaf)
        self.assertIn("NSStatusItem.squareLength", leaf)
        self.assertIn("statusItem.button?.image = Self.statusBarIcon", leaf)
        self.assertNotIn("systemSymbolName:", leaf)
        self.assertIn('"filename" : "menu_icon.png"', icon_contents)
        self.assertIn('"filename" : "menu_icon@2x.png"', icon_contents)
        self.assertNotIn("template-rendering-intent", icon_contents)

    def test_macos_status_item_is_ready_before_react_and_defers_menu_rebuilds_while_tracking(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        platform_entry = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")

        native_shell = app_delegate.index("AppKitNativeLeaf *nativeLeaf = AppKitNativeLeaf.shared;")
        core_warm = app_delegate.index("[CoreIPCBridge.shared warm];")
        react_start = app_delegate.index("[super applicationDidFinishLaunching:notification];")
        self.assertLess(native_shell, react_start)
        self.assertIn("self.automaticallyLoadReactNativeWindow = NO;", app_delegate)
        self.assertIn("[nativeLeaf setReactHostStarter:^{", app_delegate)
        self.assertIn("- (void)startReactHostWhenNeeded", app_delegate)
        self.assertIn("[self loadReactNativeWindow:nil];", app_delegate)
        self.assertLess(native_shell, core_warm)
        self.assertLess(native_shell, react_start)
        self.assertIn("private var reactHostStarter", leaf)
        self.assertIn("public func setReactHostStarter", leaf)
        self.assertIn("private func ensureReactHostStarted()", leaf)
        self.assertIn("guard routeWindowFactory == nil else { return }", leaf)
        self.assertIn("ensureReactHostStarted()\n        if let menuActionHandler", leaf)
        self.assertIn("guard title != statusTitle || running != statusRunning else { return }", leaf)
        self.assertIn("zip(nextActions, menuActions).contains", leaf)
        self.assertIn("private var menuTracking = false", leaf)
        self.assertIn("public func menuWillOpen(_ menu: NSMenu)", leaf)
        self.assertIn("public func menuDidClose(_ menu: NSMenu)", leaf)
        self.assertIn("guard !menuTracking else", leaf)
        self.assertNotIn("native.tray.setActions(routeActions);", platform_entry)
        self.assertNotIn("native.tray.setStatus(next.service);", ui)
        self.assertNotIn("native.tray.setActions(actions);", ui)

    def test_macos_quit_cancels_startup_and_finishes_off_the_main_thread(self) -> None:
        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")

        self.assertIn("private let stoppingLock = NSLock()", bridge)
        self.assertIn("guard beginStopping() else { return }", bridge)
        self.assertIn("guard !isStopping() else { break }", bridge)
        self.assertIn("func requestQuit() {\n        NSApp.terminate(nil)", leaf)
        self.assertIn("NSStatusBar.system.removeStatusItem(statusItem)", leaf)
        self.assertIn("applicationShouldTerminate", app_delegate)
        self.assertIn("return NSTerminateLater", app_delegate)
        self.assertIn("dispatch_get_global_queue(QOS_CLASS_USER_INITIATED", app_delegate)
        self.assertIn("replyToApplicationShouldTerminate:YES", app_delegate)
        self.assertNotIn("applicationWillTerminate", app_delegate)

    def test_language_is_a_state_backed_native_menu_submenu_not_a_route(self) -> None:
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        core_service = (ROOT / "litellm_menu/core/service.py").read_text(encoding="utf-8")

        for source in (ui, mac, windows):
            self.assertIn("language-menu", source)
            self.assertIn("set-language-system", source)
            self.assertIn("set-language-en", source)
            self.assertIn("set-language-zh-Hans", source)
            self.assertNotIn("open-language-settings", source)
        self.assertIn("checked?: boolean", types)
        self.assertIn("item.state = choice?.checked == true ? .on : .off", mac)
        self.assertIn("CreatePopupMenu", windows)
        self.assertNotIn('"language-settings"', core_service)
        self.assertNotIn('"language_settings"', core_service)

    def test_macos_physical_close_is_approved_by_shared_react_state(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")

        self.assertIn("NSWindowDelegate", leaf)
        self.assertIn("func windowShouldClose(_ sender: NSWindow) -> Bool", leaf)
        self.assertIn("approvedCloseRoutes", leaf)
        self.assertIn("routeForWindow(sender)", leaf)
        self.assertIn("requestClose(route: route, hiding: sender)", leaf)
        self.assertIn("window.orderOut(nil)", leaf)
        self.assertIn('emitAction("request-close-\\(route)")', leaf)
        self.assertIn("requestClose(route: window.flatMap(routeForWindow), hiding: window)", leaf)
        self.assertIn("request-close-", ui)
        self.assertIn("if (!needsDiscardConfirmation) {\n      closeRoute();\n      return;", ui)
        self.assertIn("onPress={requestClose}", ui)

    def test_macos_content_size_bridge_resizes_the_existing_react_window(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")

        self.assertIn("func setWindowContentSize(route: String, width: Double, height: Double) -> Bool", leaf)
        self.assertIn("width.isFinite", leaf)
        self.assertIn("height.isFinite", leaf)
        self.assertIn("let maximumContentExtent = 8_192.0", leaf)
        self.assertIn("let window = activeWindow()", leaf)
        self.assertIn("window.setContentSize(NSSize(width: width, height: height))", leaf)
        self.assertIn("@objc(setWindowContentSize:width:height:resolver:rejecter:)", module)
        self.assertIn(
            "resolve(leaf.setWindowContentSize(route: route, width: width.doubleValue, height: height.doubleValue))",
            module,
        )
        self.assertIn(
            "RCT_EXTERN_METHOD(setWindowContentSize:(NSString *)route width:(nonnull NSNumber *)width height:(nonnull NSNumber *)height",
            bridge,
        )

    def test_macos_routes_use_independent_react_windows_and_show_dock_only_with_ui(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        info = (MAC_PROJECT / "LiteLLMMenu-macOS/Info.plist").read_text(encoding="utf-8")

        self.assertIn("private var routeWindows: [String: NSWindow] = [:]", leaf)
        self.assertIn("setRouteWindowFactory", leaf)
        self.assertIn("routeWindowFactory?(route, initialLogTab, existing)", leaf)
        self.assertIn("NSWindow *existingWindow", app_delegate)
        self.assertIn('self.initialProps = @{ @"isPrimaryHost": @YES, @"isWindowManagerHost": @YES };', app_delegate)
        self.assertIn('@"isPrimaryHost": @NO', app_delegate)
        self.assertIn("viewWithModuleName:@\"LiteLLMMenu\" initialProperties:props", app_delegate)
        self.assertNotIn("NSApplicationActivationPolicyRegular", app_delegate)
        self.assertIn("LSUIElement", info)
        self.assertIn("<key>CFBundleIconFile</key>\n\t<string>AppIcon</string>", info)
        self.assertNotIn("CFBundleIconName", info)
        app_icon = (
            MAC_PROJECT
            / "LiteLLMMenu-macOS/Assets.xcassets/AppIcon.appiconset/icon_1024.png"
        )
        self.assertTrue(app_icon.is_file())
        self.assertIn("NSApp.setActivationPolicy(.accessory)", leaf)
        self.assertIn("NSApp.setActivationPolicy(.regular)", leaf)
        self.assertIn('Bundle.main.url(forResource: "AppIcon", withExtension: "icns")!', leaf)
        self.assertIn("NSApp.applicationIconImage = Self.applicationIcon", leaf)
        self.assertLess(
            leaf.index("NSApp.setActivationPolicy(.regular)"),
            leaf.index("NSApp.applicationIconImage = Self.applicationIcon"),
        )
        self.assertIn("window.center()", leaf)

    def test_action_menu_is_anchored_to_the_triggering_control_on_both_hosts(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_module_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")

        self.assertIn("interface NativeMenuAnchor", types)
        self.assertIn("anchor: NativeMenuAnchor", types)
        self.assertIn("showActionMenu(title: string, items: string[], anchor: NativeMenuAnchor)", bridge)
        self.assertIn("showActionMenu?: (title: string, items: string[], anchor: NativeMenuAnchor)", platform)
        self.assertNotIn("transferButtonRef", ui)
        self.assertIn("tabs.measureInWindow", ui)
        self.assertIn("anchor: { x, y, width, height }", ui)
        self.assertIn("func showActionMenu(title: String, items: [String], anchor: [String: NSNumber])", mac)
        self.assertIn("menu.popUp(positioning: nil, at: point, in: contentView)", mac)
        self.assertIn("let pointY = contentView.isFlipped ? y + height : y", mac)
        self.assertNotIn("NSEvent.mouseLocation", mac)
        self.assertIn("anchor:(NSDictionary *)anchor", mac_bridge)
        self.assertIn("showActionMenu:items:anchor:resolver:rejecter:", mac_module)
        self.assertIn("NativeMenuAnchor anchor", windows_header)
        self.assertIn("GetClientRect(window_handle_", windows)
        self.assertIn("ClientToScreen(window_handle_", windows)
        self.assertNotIn("GetCursorPos(&point)", windows)
        self.assertIn("JSValueObject const& anchor", windows_module_header)
        self.assertIn("TryGetDouble", windows_module)

    def test_log_original_record_uses_the_shared_read_only_codemirror_viewer(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")

        self.assertIn(
            'showReadOnlyText(options: { title: string; text: string; closeLabel: string; language: "json" | "toml" | "text"; html: string }): Promise<void>;',
            types,
        )
        self.assertIn(
            'showReadOnlyText(title: string, text: string, closeLabel: string, language: "json" | "toml" | "text", html: string): Promise<void>;',
            bridge,
        )
        self.assertIn(
            "showReadOnlyText: ({ title, text, closeLabel, language, html }) => bridge.showReadOnlyText(title, text, closeLabel, language, html)",
            bridge,
        )
        self.assertIn(
            'showReadOnlyText?: (title: string, text: string, closeLabel: string, language: "json" | "toml" | "text", html: string) => Promise<void>;',
            platform,
        )
        self.assertIn("await leaf.showReadOnlyText(title, text, closeLabel, language, html);", platform)

        self.assertIn('import { CODE_EDITOR_HTML, CodeEditorWebView', ui)
        self.assertEqual(2, ui.count("void native.showReadOnlyText({"))
        self.assertIn('text: selected.rows.map((row) => row.original).join("\\n\\n")', ui)
        self.assertIn("text: row.original", ui)
        self.assertIn('language: "json"', ui)
        self.assertIn("html: CODE_EDITOR_HTML", ui)
        for obsolete in ("const [originalRecord", "setOriginalRecord(", "if (originalRecord)", "log-original:"):
            self.assertNotIn(obsolete, ui)

        mac_viewer = mac.split("func showReadOnlyText(", 1)[1].split("func showActionMenu(", 1)[0]
        self.assertIn("let controller = NativeReadOnlyCodeController(", mac_viewer)
        self.assertIn("activeReadOnlyCodeController = controller", mac_viewer)
        self.assertIn("controller.present()", mac_viewer)
        self.assertNotIn("NSApp.runModal(for: panel)", mac_viewer)
        self.assertIn("private func readOnlyCodeEditorHTML(html: String, text: String, language: String) -> String", mac)
        self.assertIn('"readOnly": true', mac)
        self.assertIn('"showDiff": false', mac)
        self.assertIn('.replacingOccurrences(of: "</script", with: "<\\\\/script", options: .caseInsensitive)', mac)
        self.assertIn('let command = "<script>window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(\\(json));</script>"', mac)
        mac_controller = mac.split("private final class NativeReadOnlyCodeController", 1)[1].split(
            "private final class NativeActionMenuTarget", 1
        )[0]
        for marker in (
            "WKWebView(frame: .zero, configuration: configuration)",
            "documentHTML = readOnlyCodeEditorHTML(html: html, text: text, language: language)",
            "panel.contentView?.layoutSubtreeIfNeeded()",
            "webView.loadHTMLString(documentHTML, baseURL: nil)",
        ):
            self.assertIn(marker, mac_controller)
        for obsolete in (
            "WKScriptMessageHandler",
            'userContentController.add(self, name: "litellmCodeEditor")',
            "userContentController.addUserScript",
            "webView.navigationDelegate = self",
            "evaluateJavaScript",
        ):
            self.assertNotIn(obsolete, mac_controller)
        self.assertIn("@objc(showReadOnlyText:text:closeLabel:language:html:resolver:rejecter:)", mac_module)
        self.assertIn("language: String,\n        html: String,", mac_module)
        self.assertIn("DispatchQueue.main.async", mac_module)
        self.assertIn("language: language,\n                html: html,\n                completion: { resolve(nil) }", mac_module)
        self.assertIn(
            "RCT_EXTERN_METHOD(showReadOnlyText:(NSString *)title text:(NSString *)text closeLabel:(NSString *)closeLabel language:(NSString *)language html:(NSString *)html",
            mac_bridge,
        )

        windows_viewer = windows.split("void WinUI3NativeLeaf::ShowReadOnlyText(", 1)[1].split(
            "std::optional<size_t> WinUI3NativeLeaf::ShowActionMenu", 1
        )[0]
        self.assertIn("xaml::Window dialog;", windows_viewer)
        self.assertIn("controls::WebView2 viewer;", windows_viewer)
        self.assertIn("InitializeReadOnlyCodeViewer(weak_state, viewer_html, viewer_text, viewer_language);", windows_viewer)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_, {760, 520}, state->finished)", windows_viewer)
        windows_initializer = windows.split("winrt::fire_and_forget InitializeReadOnlyCodeViewer(", 1)[1].split(
            "}  // namespace", 1
        )[0]
        for marker in (
            "EnsureCoreWebView2Async",
            'payload.Insert(L"readOnly", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(true));',
            'payload.Insert(L"showDiff", winrt::Windows::Data::Json::JsonValue::CreateBooleanValue(false));',
            "window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive",
            "state->webview.NavigateToString(html);",
        ):
            self.assertIn(marker, windows_initializer)
        self.assertIn('REACT_METHOD(ShowReadOnlyText, L"showReadOnlyText")', windows_header)
        self.assertIn("std::wstring const& language,\n      std::wstring const& html,", windows_header)
        self.assertIn("void WinUI3NativeLeafModule::ShowReadOnlyText(", windows_module)
        self.assertIn("leaf->ShowReadOnlyText(title, text, close_label, language, html);", windows_module)

    def test_localization_crosses_the_native_leaf_contract(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("interface NativeLocalization", types)
        self.assertIn("setLocalization(strings: NativeLocalization)", types)
        self.assertIn("native.setLocalization({", ui)
        self.assertIn('translate("common.find")', ui)
        self.assertNotIn('webdavToggle: translate("webdav.enabled")', ui)
        self.assertIn('menuQuit: translate("menu.quit")', ui)
        self.assertIn("func setLocalization", mac)
        localization = mac.split("func setLocalization", 1)[1].split("func setMenuActions", 1)[0]
        self.assertIn("window.title = title", localization)
        self.assertNotIn("configure(window", localization)
        self.assertIn('localized("menuQuit", fallback: "Quit LiteLLM Menu")', mac)
        self.assertIn('"webdav-status"', mac)
        self.assertIn("void WinUI3NativeLeaf::SetLocalization", windows)


    def test_relay_login_is_a_native_browser_boundary_with_sanitized_results(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        core_ipc = (ROOT / "litellm_menu/core/ipc.py").read_text(encoding="utf-8")

        self.assertIn("relayLogin(options:", types)
        self.assertIn('type: "newapi" | "sub2api"', types)
        self.assertIn("language: LanguagePreference", types)
        self.assertNotIn("revision: number;\n  }):", types.split("relayLogin(options:", 1)[1].split("setLaunchAtLogin", 1)[0])
        self.assertIn('loginStatus: "signed_in"', types)
        self.assertIn("relayLogin(options:", bridge)
        self.assertIn("relayLogin?: (options:", platform)

        relay_ui = (SHARED / "ui/RelayAccountManager.tsx").read_text(encoding="utf-8")
        self.assertIn('language: snapshot?.language ?? "system"', relay_ui)
        self.assertIn("const rememberPasswordRef = useRef(false);", relay_ui)
        self.assertIn("const passwordStorageAvailable = true;", relay_ui)
        self.assertIn("const [manualType, setManualType] = useState<RelayType>();", relay_ui)
        self.assertIn("const accountType = detected ?? manualType;", relay_ui)
        self.assertIn("NativePicker", relay_ui)
        self.assertIn("addAccount(accountType, candidate, passwordStorageAvailable && rememberPasswordRef.current, {", relay_ui)
        self.assertIn(
            "typeDetectionRequest.current += 1;\n"
            "    setAdding(setupOnly);",
            relay_ui,
        )
        self.assertIn('const [addStep, setAddStep] = useState<AddStep>("origin");', relay_ui)
        self.assertIn('type AddStep = "origin" | "sign-in";', relay_ui)
        self.assertIn("onChangeText={updateAddOrigin}", relay_ui)
        self.assertNotIn('onBlur={() => { void detectRelayType(); }}', relay_ui)
        self.assertIn("setAddStationName(suggestedRelayStationName(value));", relay_ui)
        self.assertIn("const updateAddStationName = (value: string): void =>", relay_ui)
        self.assertIn('SetupProgress step="origin"', relay_ui)
        self.assertNotIn('SetupProgress step="select"', relay_ui)
        self.assertNotIn('setAddStep("select")', relay_ui)
        self.assertIn(': !setupOnly && selected ? <View style={styles.detailWorkspace}>', relay_ui)
        self.assertIn('onClose?.();\n        native.window.focus("relay-accounts");', relay_ui)
        self.assertIn("if (embedded) return true;", relay_ui)
        self.assertIn("NativeCheckbox", relay_ui)
        self.assertIn('title={translate("relay.importSelected")}', relay_ui)
        self.assertIn('type SavedSessionRestore = "signed_in" | "expired" | "unavailable";', relay_ui)
        self.assertIn("const openedAccountIDs = useRef(new Set<string>());", relay_ui)
        self.assertIn("const refreshLoginState = async (account: RelayAccount, automatic = false): Promise<void> => {", relay_ui)
        self.assertIn("void refreshLoginState(selected, true);", relay_ui)
        self.assertIn('title={translate("common.refresh")}', relay_ui)
        self.assertIn('native.window.open("relay-add")', relay_ui)
        self.assertIn('translate("relay.passwordNotSaved")', relay_ui)
        self.assertNotIn('onPress={() => { void refreshWorkspace(); }}', relay_ui)
        self.assertNotIn('translate("relay.status.signed_out")', relay_ui)

        self.assertIn("import WebKit", mac_leaf)
        self.assertIn("configuration.websiteDataStore = .nonPersistent()", mac_leaf)
        self.assertNotIn("configuration.websiteDataStore = .default()", mac_leaf)
        self.assertIn('Probe(path: "api/user/self"', mac_leaf)
        self.assertIn('Probe(path: "api/v1/auth/me"', mac_leaf)
        self.assertIn("sameOrigin(url)", mac_leaf)
        self.assertIn("NativeRelaySessionMemoryStore", mac_leaf)
        self.assertNotIn("NativeRelayCredentialStore", mac_leaf)
        self.assertNotIn("import Security", mac_leaf)
        self.assertNotIn("SecItem", mac_leaf)
        self.assertNotIn("kSec", mac_leaf)
        self.assertIn("accountType: type,", mac_leaf)
        self.assertIn("origin: originURL.absoluteString", mac_leaf)
        self.assertIn("restoreSessionAndLoad()", mac_leaf)
        self.assertIn("httpCookieStore.setCookie(cookie)", mac_leaf)
        self.assertIn("localStorage.setItem('access_token', accessToken)", mac_leaf)
        self.assertIn("localStorage.getItem('user')", mac_leaf)
        self.assertIn("user.token = accessToken", mac_leaf)
        self.assertIn("typeof user.token === 'string' ? user.token : ''", mac_leaf)
        self.assertIn("WKScriptMessageHandler", mac_leaf)
        self.assertIn('configuration.userContentController.add(self, name: "litellmRelayPassword")', mac_leaf)
        self.assertIn("window.webkit.messageHandlers.litellmRelayPassword.postMessage(password)", mac_leaf)
        self.assertIn("capturedPassword = password", mac_leaf)
        sign_in_check = mac_leaf.split("private func startSignInCheck", 1)[1].split("private func isCurrentCheck", 1)[0]
        self.assertNotIn("capturedPassword = nil", sign_in_check)
        self.assertIn("let probeAccessToken = capturedAccessToken ?? restoredSession?.accessToken", mac_leaf)
        self.assertIn("!(acceptedCookie?.isEmpty ?? true) || !(accessToken?.isEmpty ?? true)", mac_leaf)
        self.assertIn("guard !cookie.isEmpty || !accessToken.isEmpty else", mac_leaf)
        self.assertIn("let username = detectedUsername ?? self.presetUsername ?? \"\"", mac_leaf)
        self.assertIn("set(user, \\(safeUser))", mac_leaf)
        self.assertIn("input[type=email], input[type=text]", mac_leaf)
        self.assertIn("const words = new Set(['login', 'log in', 'sign in', '登录']);", mac_leaf)
        self.assertIn('guard type == "sub2api" else { return originURL }', mac_leaf)
        self.assertIn('originURL.appendingPathComponent("login")', mac_leaf)
        self.assertIn("private let loadingOverlay = NSVisualEffectView()", mac_leaf)
        self.assertNotIn("NSProgressIndicator", mac_leaf)
        self.assertNotIn("startAnimation", mac_leaf)
        self.assertIn("private static let immediateWebPresentationScript", mac_leaf)
        self.assertIn("animation: none !important;", mac_leaf)
        self.assertIn("transition: none !important;", mac_leaf)
        self.assertIn("const stripGradients", mac_leaf)
        self.assertNotIn("background-image: none !important;", mac_leaf)
        self.assertIn(': text("Loading sign-in page...", "正在加载登录页面...")', mac_leaf)
        self.assertIn("func webView(_ webView: WKWebView, didStartProvisionalNavigation", mac_leaf)
        self.assertIn("func webView(_ webView: WKWebView, didFailProvisionalNavigation", mac_leaf)
        self.assertIn("private var capturedPassword: String?", mac_leaf)
        self.assertIn("__litellm_menu_relay_password", mac_leaf)
        self.assertIn("let shouldRememberPassword = rememberPassword", mac_leaf)
        self.assertIn("let capturedPassword = self.capturedPassword", mac_leaf)
        self.assertIn("password: shouldRememberPassword ? capturedPassword : nil", mac_leaf)
        self.assertNotIn("autoLoginAttempt", mac_leaf)
        self.assertIn("private var automaticCheckProbe: DispatchWorkItem?", mac_leaf)
        self.assertIn("private var loginFormRevealProbe: DispatchWorkItem?", mac_leaf)
        self.assertIn("private func scheduleLoginFormReveal()", mac_leaf)
        self.assertIn("user.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });", mac_leaf)
        self.assertIn("user.focus({ preventScroll: true })", mac_leaf)
        self.assertIn("guard statusLabel.stringValue != value else { return }", mac_leaf)
        self.assertIn("if !automatically {", mac_leaf)
        self.assertIn("setStatus(waitingForSignInStatus)", mac_leaf)
        self.assertNotIn('text("Waiting for sign-in; success is detected automatically.', mac_leaf)
        self.assertIn('text("1 Relay URL  ›  2 Sign in", "1 中转站 URL  ›  2 登录")', mac_leaf)
        self.assertNotIn("3 Select resources", mac_leaf)
        self.assertIn("signInButton.isHidden = !showsReloadAction", mac_leaf)
        self.assertIn("cancelButton.isHidden = !showsPanelActions", mac_leaf)
        self.assertIn("parent.beginSheet(window)", mac_leaf)
        self.assertIn("parent.endSheet(window)", mac_leaf)
        self.assertIn("@objc private func closeEmbeddedWindow", mac_leaf)
        self.assertIn('self?.close(route: "relay-add")', mac_leaf)
        self.assertNotIn('text("Check Sign-In", "检查登录")', mac_leaf)
        self.assertIn('text("No valid sign-in was found.', mac_leaf)
        self.assertIn('L"Verify Sign-In", L"验证登录"', windows_relay)
        self.assertIn("LiteLLMTabStopIdentifier", mac_controls)
        self.assertIn("HandleLiteLLMTabCommand", mac_controls)
        self.assertIn("guard !finished, !checking else { return }", mac_leaf)
        self.assertIn("capturedAccessToken = nil", mac_leaf)
        self.assertIn("private func beginBrowserFlow()", mac_leaf)
        self.assertIn("beginBrowserFlow()", mac_leaf)
        check_sign_in = mac_leaf.split("@objc private func checkSignIn", 1)[1].split("private func isCurrentCheck", 1)[0]
        self.assertNotIn("capturedPassword = nil", check_sign_in)
        self.assertIn("private final class NativeRelayLoginAttempt", mac_leaf)
        self.assertIn("private var activeCheck: NativeRelayLoginAttempt?", mac_leaf)
        self.assertIn("private func isCurrentCheck(_ attempt: NativeRelayLoginAttempt) -> Bool", mac_leaf)
        self.assertIn("guard let self, let attempt, self.isCurrentCheck(attempt) else { return }", mac_leaf)
        self.assertIn("guard let attempt, attempt.isActive() else { return }", mac_leaf)
        self.assertIn("activeCheck?.requestCancellation()", mac_leaf)
        self.assertIn("guard attempt.beginCommit() else { return }", mac_leaf)
        self.assertIn("dismissWhileCommitting()", mac_leaf)
        self.assertIn("self.finish(accepted, session: session, attempt: attempt)", mac_leaf)
        self.assertIn("func windowShouldClose(_ sender: NSWindow) -> Bool", mac_leaf)
        close_method = mac_leaf.split("func windowShouldClose(_ sender: NSWindow) -> Bool", 1)[1].split("private func finishCheckingFailure", 1)[0]
        self.assertIn("true", close_method)
        self.assertIn("finishCheckingFailure", mac_leaf)
        self.assertIn("private static let relayLoginTimeout: TimeInterval = 60", mac_core)
        self.assertIn("timeoutInterval: Self.relayLoginTimeout", mac_core)
        self.assertIn("URLSessionTaskDelegate", mac_leaf)
        self.assertIn("willPerformHTTPRedirection", mac_leaf)
        self.assertIn('language == "zh-Hans" || (language == "system"', mac_leaf)
        self.assertIn("@objc(relayLogin:resolver:rejecter:)", mac_module)
        self.assertIn('"origin", "language", "username"', mac_module)
        self.assertIn('resolve(["revision": result.revision, "loginStatus": "signed_in", "username": result.username])', mac_module)
        self.assertIn('route: "host/relay/login"', mac_core)
        self.assertIn('payload["password"] = password', mac_core)
        self.assertIn('Set(object.keys) == Set(["protocol_version", "revision", "login_status", "username"])', mac_core)
        self.assertIn('"/v1/host/relay/login"', core_ipc)

        self.assertIn('REACT_METHOD(RelayLogin, L"relayLogin")', windows_header)
        self.assertIn("void WinUI3NativeLeafModule::RelayLogin(", windows_module)
        self.assertIn("promise.Resolve(std::nullopt);", windows_module.split("void WinUI3NativeLeafModule::RelayLogin(", 1)[1].split("std::string WinUI3NativeLeafModule::SystemLocale", 1)[0])
        self.assertIn("controls::WebView2", windows_relay)
        self.assertIn("IsInPrivateModeEnabled(true)", windows_relay)
        self.assertIn("ProfileName(ProfileName(state->options.account_id))", windows_relay)
        self.assertIn("CredWriteW", windows_relay)
        self.assertIn("ProbeEndpoint(state->options", windows_relay)
        self.assertIn("auto_submit_saved_password_pending", windows_relay)
        self.assertIn("did_auto_submit_saved_password", windows_relay)
        self.assertIn("auto_login_attempt", windows_relay)
        self.assertIn("localStorage.getItem('user')", windows_relay)
        self.assertIn("user.token=access", windows_relay)
        self.assertIn("userInput.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });", windows_relay)
        self.assertIn("userInput.focus({ preventScroll: true })", windows_relay)
        self.assertIn("bool UseChinese(WindowsRelayLoginOptions const& options)", windows_relay)
        self.assertIn('options.language == "zh-Hans"', windows_relay)
        self.assertIn('auto prior_password = state->options.remember_password', windows_relay)
        self.assertIn('if (state->options.remember_password) {\n        WriteChunkedCredential(state->options.account_id, L"password", prior_password);', windows_relay)
        self.assertIn('host == L"localhost"', windows_relay)
        self.assertIn('state->webview.Source(winrt::Windows::Foundation::Uri(Utf8ToWide(state->options.origin)));', windows_relay)
        probe_login = windows_relay.split("winrt::fire_and_forget ProbeLogin", 1)[1].split("winrt::fire_and_forget InitializeBrowser", 1)[0]
        self.assertNotIn("state->captured_password.reset();", probe_login)
        self.assertIn("if (state->options.remember_password && !password) password = state->captured_password;", windows_relay)
        self.assertIn("std::map<std::string, std::string> ParseCookieHeader(std::string const& header);", windows_relay)
        self.assertIn("if (credentials_saved && !accepted)", windows_relay)
        self.assertIn("class RelayLoginAttempt", windows_relay)
        self.assertIn("bool BeginCommit()", windows_relay)
        self.assertIn("CancellationOutcome RequestCancellation()", windows_relay)
        self.assertIn("void StartLoginCheck", windows_relay)
        self.assertIn("if (!attempt->BeginCommit()) co_return;", windows_relay)
        self.assertIn("current->dialog_closed_during_commit = true;", windows_relay)
        self.assertNotIn("credentials_saved && (state->canceled.load() || state->finished.load())", windows_relay)
        self.assertIn("bool* confirmed_authentication_rejection = nullptr", windows_relay)
        self.assertIn("saw_authentication_rejection && !saw_non_authentication_failure", windows_relay)
        self.assertIn("if (!confirmed_authentication_rejection) return std::nullopt;", windows_relay)
        self.assertIn("accepted_cookie.empty() && (!access || access->empty())", windows_relay)
        self.assertIn('auto const ui_language = language.value_or("system")', windows_module)
        self.assertIn('HostRequest(L"host/relay/login"', windows_core)
        self.assertIn('HostRequest(L"host/relay/login", body, false, 60000)', windows_core)
        self.assertIn('payload.SetNamedValue(L"password"', windows_core)

    def test_relay_login_headers_name_the_station_not_the_internal_adapter(self) -> None:
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")

        self.assertIn(": (originURL.host ?? originURL.absoluteString)", mac_leaf)
        self.assertNotIn('accountLabel.stringValue = "\\(type ==', mac_leaf)
        self.assertIn("account.Text(Utf8ToWide(state->options.origin));", windows_relay)
        self.assertNotIn("auto const account_type = state->options.account_type", windows_relay)

    def test_relay_session_restore_is_native_only_and_does_not_import_provider_models(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        relay_ui = (SHARED / "ui/RelayAccountManager.tsx").read_text(encoding="utf-8")
        logs_ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        core_ipc = (ROOT / "litellm_menu/core/ipc.py").read_text(encoding="utf-8")

        self.assertIn("restoreRelaySession(options:", types)
        self.assertIn("restoreRelaySession(options:", bridge)
        self.assertIn("restoreRelaySession?: (options:", platform)
        self.assertIn("native.restoreRelaySession", relay_ui)
        self.assertIn("username: account.username || undefined", relay_ui)
        self.assertIn("const refreshLoginState = async (account: RelayAccount, automatic = false): Promise<void> => {", relay_ui)
        self.assertIn("const restored = await restoreSavedSession(account);", relay_ui)
        self.assertIn('if (restored === "signed_in")', relay_ui)
        self.assertNotIn('translate("relay.lastUpdated"', relay_ui)
        self.assertNotIn("accountAvatar", relay_ui)
        self.assertNotIn("native.showActionMenu", relay_ui)
        self.assertNotIn("NativeToggle", relay_ui)
        resource_inspector = relay_ui.split("function ResourceInspector(", 1)[1].split(
            "export function RelayAccountManager(",
            1,
        )[0]
        self.assertIn('value={resource.enabled}', resource_inspector)
        self.assertIn('onValueChange={onEnabledChange}', resource_inspector)
        self.assertIn('native.copySecret({', relay_ui)
        self.assertIn('domain="relay_accounts" field="api_key"', relay_ui)
        self.assertIn('copySecret(options:', types)
        self.assertIn('copySecret(domain: "relay_accounts", field: "api_key", target: string)', bridge)
        self.assertIn('copySecret?: (domain: "relay_accounts", field: "api_key", target: string)', platform)
        self.assertIn('@objc(copySecret:field:target:resolver:rejecter:)', mac_module)
        self.assertIn('copySecret:(NSString *)domain field:(NSString *)field target:(NSString *)target', mac_bridge)
        self.assertIn('REACT_METHOD(CopySecret, L"copySecret")', windows_header)
        self.assertIn('void WinUI3NativeLeafModule::CopySecret(', windows_module)
        self.assertIn("@objc(restoreRelaySession:resolver:rejecter:)", mac_module)
        self.assertIn("restoreRelaySession:(NSDictionary *)options", mac_bridge)
        self.assertIn("func restoreRelaySession(", mac_leaf)
        self.assertIn("presetUsername: username?.trimmingCharacters(in: .whitespacesAndNewlines)", mac_leaf)
        self.assertIn("NativeRelaySessionProbe.verify", mac_leaf)
        self.assertIn("sawAuthenticationRejection && !sawNonAuthenticationFailure", mac_leaf)
        self.assertIn("NativeRelaySessionMemoryStore.writeSession(refreshedSession, accountID: accountID)", mac_leaf)
        usage_logs = logs_ui.split("const openRelayUsageLogs", 1)[1].split("return <View style={styles.logsWindow}", 1)[0]
        self.assertLess(usage_logs.index("native.restoreRelaySession"), usage_logs.index("native.openRelayLogs"))
        self.assertIn('session?.loginStatus !== "signed_in"', usage_logs)
        self.assertIn("native.relayLogin", usage_logs)
        self.assertIn('route: "host/relay/restore"', mac_core)
        self.assertIn('REACT_METHOD(RestoreRelaySession, L"restoreRelaySession")', windows_header)
        self.assertIn("void WinUI3NativeLeafModule::RestoreRelaySession(", windows_module)
        self.assertIn('auto username = field("username");', windows_module)
        self.assertIn("*account_id, *account_type, *label, *origin, username, false", windows_module)
        self.assertIn("RestoreWindowsRelaySession(", windows_relay)
        self.assertIn('HostRequest(L"host/relay/restore"', windows_core)
        self.assertIn('"/v1/host/relay/restore"', core_ipc)

    def test_relay_credentials_are_cleared_per_account_through_a_native_only_bridge(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")

        self.assertIn("clearRelayCredentials(accountId: string): Promise<void>", types)
        self.assertIn("clearRelayCredentials(accountId: string): Promise<void>", bridge)
        self.assertIn("clearRelayCredentials?: (accountId: string) => Promise<void>", platform)
        self.assertIn("if (!leaf.clearRelayCredentials)", platform)
        self.assertIn("func clearRelayCredentials(accountID: String) -> Bool", mac_leaf)
        self.assertIn("NativeRelaySessionMemoryStore.clear(accountID: accountID)", mac_leaf)
        self.assertNotIn("NativeRelayCredentialStore", mac_leaf)
        self.assertNotIn("import Security", mac_leaf)
        self.assertNotIn("SecItem", mac_leaf)
        self.assertNotIn("kSec", mac_leaf)
        self.assertIn("@objc(clearRelayCredentials:resolver:rejecter:)", mac_module)
        self.assertIn("clearRelayCredentials:(NSString *)accountID", mac_bridge)
        self.assertIn('REACT_METHOD(ClearRelayCredentials, L"clearRelayCredentials")', windows_header)
        self.assertIn('REACT_METHOD(ClearRelayPassword, L"clearRelayPassword")', windows_header)
        self.assertIn("void WinUI3NativeLeafModule::ClearRelayCredentials(", windows_module)
        self.assertIn("void WinUI3NativeLeafModule::ClearRelayPassword(", windows_module)
        self.assertIn('ClearChunkedCredential(account_id, L"password")', windows_relay)
        self.assertIn('ClearChunkedCredential(account_id, L"session")', windows_relay)

    def test_fetched_model_selection_is_a_native_promise_dialog_on_both_hosts(self) -> None:
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        windows_leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")

        self.assertIn("chooseModelsToAdd(options:", types)
        self.assertIn("chooseModelsToAdd(models: string[]", bridge)
        self.assertIn("chooseModelsToAdd?: (models: string[]", platform)
        self.assertIn("native.chooseModelsToAdd({ models: candidates, providerName, keyName })", ui)
        self.assertIn("candidateSet.has(model)", ui)
        self.assertNotIn("<Modal", ui)
        self.assertNotIn("FetchedModelsDialog", ui)

        self.assertIn("func chooseModelsToAdd(models: [String]", mac_leaf)
        self.assertIn("NSPanel(", mac_leaf)
        self.assertIn("NSApp.runModal(for: panel)", mac_leaf)
        self.assertIn('modelChooserButton(title: localized("modelChooserAll"', mac_leaf)
        self.assertIn('modelChooserButton(title: localized("modelChooserInvert"', mac_leaf)
        self.assertIn('modelChooserButton(title: "+"', mac_leaf)
        self.assertIn("NSButton(checkboxWithTitle:", mac_leaf)
        self.assertNotIn("let checkbox = NSBezierPath", mac_leaf)
        self.assertIn("@objc func chooseModelsToAdd(_ models: [String]", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(chooseModelsToAdd:", mac_bridge)

        self.assertIn("std::optional<std::vector<std::wstring>> WinUI3NativeLeaf::ChooseModelsToAdd(", windows_leaf)
        self.assertIn("xaml::Window dialog;", windows_leaf)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_", windows_leaf)
        self.assertNotIn("XamlUIService", windows_module)
        self.assertIn('all.Content(winrt::box_value(Localized("modelChooserAll"', windows_leaf)
        self.assertIn('invert.Content(winrt::box_value(Localized("modelChooserInvert"', windows_leaf)
        self.assertIn('modelChooserTitle: translate("modelChooser.title")', ui)
        self.assertIn('modelChooserCountFiltered: translate("modelChooser.countFiltered"', ui)
        self.assertIn('"modelChooserTitle": "Choose Models to Add"', mac_leaf)
        self.assertIn('"modelChooser.title": "选择要添加的模型"', (SHARED / "i18n/zh-Hans.ts").read_text(encoding="utf-8"))
        self.assertIn("state->add.IsEnabled(selected > 0);", windows_leaf)
        self.assertIn('REACT_METHOD(ChooseModelsToAdd, L"chooseModelsToAdd")', windows_header)
        self.assertIn("void WinUI3NativeLeafModule::ChooseModelsToAdd(", windows_module)

        chooser_section = windows_module.split("void WinUI3NativeLeafModule::ChooseModelsToAdd(", 1)[1].split(
            "void WinUI3NativeLeafModule::EditSecret(", 1
        )[0]
        for forbidden in ("CoreIPCBridge", "CreateSecretCapability", "RegisterFileCapability"):
            self.assertNotIn(forbidden, chooser_section)

    def test_native_clear_secret_stages_only_a_capability_clear(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")

        self.assertIn("clearSecret(options:", types)
        self.assertIn("clearSecret(", bridge)
        self.assertIn("clearSecret?: (", platform)
        self.assertIn("@objc(clearSecret:field:target:resolver:rejecter:)", mac)
        self.assertIn("stageSecret(\n                    capability.token,\n                    value: nil,\n                    clear: true", mac)
        self.assertIn('reject("E_NATIVE_SECRET_CAPABILITY"', mac)
        self.assertIn('reject("E_NATIVE_SECRET_STAGE"', mac)
        self.assertIn("RCT_EXTERN_METHOD(clearSecret:", mac_bridge)
        self.assertIn('REACT_METHOD(ClearSecret, L"clearSecret")', windows_header)
        self.assertIn("void WinUI3NativeLeafModule::ClearSecret(", windows)
        clear_section = windows.split("void WinUI3NativeLeafModule::ClearSecret(", 1)[1].split(
            "std::string WinUI3NativeLeafModule::SystemLocale", 1
        )[0]
        self.assertIn('CreateSecretCapability(domain, field, target, "settings")', clear_section)
        self.assertIn("StageSecret(capability->token, std::nullopt, true)", clear_section)
        self.assertNotIn("PasswordBox", clear_section)
        self.assertNotIn("ContentDialog", clear_section)
        self.assertNotIn("WideToUtf8", clear_section)

    def test_inline_secret_inputs_keep_passwords_inside_native_hosts(self) -> None:
        adapter = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeSecureTextInputNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        windows_spec = (SHARED / "ui/windows/NativeSecureTextInputNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        windows_codegen = (
            WIN_PROJECT
            / "codegen/react/components/LiteLLMMenu/LiteLLMWinUISecureTextInput.g.h"
        ).read_text(encoding="utf-8")

        self.assertIn("export function NativeSecureTextInput", adapter)
        self.assertIn("LiteLLMAppKitSecureTextInput", mac_spec)
        self.assertIn("LiteLLMWinUISecureTextInput", windows_spec)
        for spec in (mac_spec, windows_spec):
            self.assertNotIn("value?:", spec)
            self.assertIn("plainText?: WithDefault<boolean, false>;", spec)
            self.assertNotIn("onChangeText", spec)
            self.assertIn("onSecretState", spec)
        self.assertIn("NSSecureTextField *_field", mac)
        self.assertIn("NSTextField *_plainField", mac)
        self.assertIn("loadPlainTextSecretForGeneration", mac)
        self.assertIn("void ConfigureSingleLineTextField(NSTextField *field)", mac)
        self.assertIn("field.usesSingleLineMode = YES", mac)
        self.assertIn("field.lineBreakMode = NSLineBreakByTruncatingTail", mac)
        self.assertIn("cell.wraps = NO", mac)
        self.assertIn("cell.scrollable = YES", mac)
        self.assertIn("ConfigureSingleLineTextField(_field);", mac)
        self.assertIn("ConfigureSingleLineTextField(_plainField);", mac)
        self.assertEqual(2, mac.count("[self addCursorRect:self.bounds cursor:[NSCursor IBeamCursor]];"))
        self.assertIn("stageSecretForDomain", mac)
        self.assertIn("stageSecretForDomain(", mac_core)
        self.assertIn("PasswordBox password_box_", windows)
        self.assertIn("password_box_.MinHeight(30.0);", windows)
        self.assertIn(
            "password_box_.Padding(winrt::Microsoft::UI::Xaml::Thickness{8, 0, 8, 0});",
            windows,
        )
        self.assertIn(
            "password_box_.VerticalContentAlignment(winrt::Microsoft::UI::Xaml::VerticalAlignment::Center);",
            windows,
        )
        self.assertIn("PasswordRevealMode::Hidden", windows)
        self.assertIn("PasswordRevealMode::Visible", windows)
        self.assertIn("IsPlainTextAutoCommitField", windows)
        self.assertIn("CreateSecretCapability(", windows)
        self.assertIn("StageSecret(capability->token, secret, false)", windows)
        self.assertNotIn("onChangeText", windows_codegen)
        self.assertIn("OnSecretState", windows_codegen)
        self.assertIn("NativeSecretInputControl", ui)
        self.assertNotIn('onEdit={() => stageSecret({ domain: "runtime"', ui)

    def test_provider_api_key_plaintext_readback_is_narrow_and_native_only(self) -> None:
        ipc = (ROOT / "litellm_menu" / "core" / "ipc.py").read_text(encoding="utf-8")
        service = (ROOT / "litellm_menu" / "core" / "service.py").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "CoreIPCBridge.h").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        self.assertIn('route in {"/v1/host/secret/read-capability", "/v1/host/secret/read"}', ipc)
        self.assertIn("_PLAINTEXT_SECRET_FIELDS", service)
        self.assertIn('(\"codex\", \"api_key\")', service)
        self.assertIn('(\"claude\", \"deployment_token\")', service)
        self.assertIn("_SecretReadCapability", ipc)
        self.assertIn("read_secret_capability", ipc)
        self.assertIn("readPlainTextSecretForDomain", mac)
        self.assertIn("ReadPlainTextSecret", windows_header)
        self.assertIn("ReadPlainTextSecret", windows)
        self.assertNotIn("secret/read", (SHARED / "platform" / "nativeBridge.ts").read_text(encoding="utf-8"))

    def test_provider_api_key_uses_native_plaintext_auto_commit(self) -> None:
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeSecureTextInputNativeComponent.ts").read_text(encoding="utf-8")
        windows_spec = (SHARED / "ui/windows/NativeSecureTextInputNativeComponent.ts").read_text(encoding="utf-8")

        provider_editor = ui.split("function ProviderEditor", 1)[1].split("function CodexWorkspace", 1)[0]
        self.assertIn("<NativeSecretField plainText autoCommit", provider_editor)
        self.assertNotIn('setTitle={translate("common.set")}', provider_editor)
        self.assertNotIn('clearTitle={translate("common.clear")}', provider_editor)
        self.assertNotIn("onClear={() => clearSecret", provider_editor)
        self.assertNotIn("providers.apiKeyHint", provider_editor)
        for spec in (mac_spec, windows_spec):
            self.assertIn("plainText?: WithDefault<boolean, false>;", spec)
            self.assertIn("autoCommit?: WithDefault<boolean, false>;", spec)
        self.assertIn("_field.action = @selector(submitSecret:);", mac)
        self.assertIn("controlTextDidEndEditing", mac)
        self.assertIn("loadPlainTextSecretForGeneration", mac)
        self.assertIn("readPlainTextSecret", mac_core)
        self.assertIn("host/secret/read", mac_core)
        self.assertIn("ReadPlainTextSecret", windows_core)

    def test_secure_input_is_registered_in_both_fabric_hosts(self) -> None:
        mac_package = (ROOT / "rn/apps/macos/package.json").read_text(encoding="utf-8")
        mac_header = (MAC_NATIVE / "AppKitControlViews.h").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        self.assertIn('"LiteLLMAppKitSecureTextInput": "LiteLLMAppKitSecureTextInputComponentView"', mac_package)
        self.assertIn("LiteLLMAppKitSecureTextInputComponentView", mac_header)
        self.assertIn("LiteLLMAppKitSecureTextInputCls", mac)
        self.assertIn("RegisterLiteLLMWinUISecureTextInputNativeComponent", windows)

    def test_shared_form_primitives_have_codegen_native_components(self) -> None:
        adapter = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        mac_specs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((SHARED / "ui/macos").glob("*NativeComponent.ts"))
        )
        win_specs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((SHARED / "ui/windows").glob("*NativeComponent.ts"))
        )

        expected = {
            "NativeButton": ("LiteLLMAppKitButton", "LiteLLMWinUIButton"),
            "NativeSegmentedControl": (
                "LiteLLMAppKitSegmentedControl",
                "LiteLLMWinUISegmentedControl",
            ),
            "NativeTextField": ("LiteLLMAppKitTextField", "LiteLLMWinUITextInput"),
            "NativeToggle": ("LiteLLMAppKitSwitch", "LiteLLMWinUISwitch"),
            "NativeSelectableRow": (
                "LiteLLMAppKitSelectableRow",
                "LiteLLMWinUISelectableRow",
            ),
            "NativeCheckbox": ("LiteLLMAppKitCheckbox", "LiteLLMWinUICheckbox"),
            "NativePicker": ("LiteLLMAppKitPicker", "LiteLLMWinUIPicker"),
        }
        for adapter_name, (mac_name, win_name) in expected.items():
            self.assertIn(f"export function {adapter_name}", adapter)
            self.assertIn("codegenNativeComponent<", mac_specs)
            self.assertIn(f'("{mac_name}")', mac_specs)
            self.assertIn("codegenNativeComponent<", win_specs)
            self.assertIn(f'("{win_name}")', win_specs)

    def test_native_button_link_variant_is_a_system_navigation_control_on_each_platform(self) -> None:
        adapter = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeButtonNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        windows_spec = (SHARED / "ui/windows/NativeButtonNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        self.assertIn("link?: boolean;", adapter)
        for spec in (mac_spec, windows_spec):
            self.assertIn("link?: WithDefault<boolean, false>;", spec)
        self.assertIn("NSBezelStyleInline", mac)
        self.assertIn("LiteLLMNavigationLinkButton", mac)
        self.assertIn("NSCursor.pointingHandCursor", mac)
        self.assertIn("((LiteLLMNavigationLinkButton *)_button).linkMode = link;", mac)
        self.assertIn("HyperlinkButton", windows)
        self.assertIn("hyperlink_.Visibility(link", windows)

    def test_macos_controls_use_fabric_component_views_not_legacy_managers(self) -> None:
        controls_path = MAC_NATIVE / "AppKitControlViews.mm"
        self.assertTrue(controls_path.is_file(), "AppKit Fabric component view source is missing")
        controls = controls_path.read_text(encoding="utf-8")
        all_native_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(MAC_NATIVE.glob("*"))
            if path.suffix in {".h", ".m", ".mm", ".swift"}
        )

        self.assertIn("RCTViewComponentView", all_native_sources)
        self.assertIn("componentDescriptorProvider", controls)
        self.assertNotIn("RCTViewManager", all_native_sources)
        self.assertNotIn("RCT_EXPORT_MODULE(LiteLLMAppKit", all_native_sources)
        for native_class in (
            "NSButton",
            "NSPopUpButton",
            "NSSegmentedControl",
            "NSTextField",
            "LiteLLMTabSwitch",
        ):
            self.assertIn(native_class, controls)

    def test_macos_single_line_controls_and_table_cells_are_vertically_centered(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")

        self.assertIn("@interface LiteLLMAppKitControlHostView : NSView", controls)
        self.assertIn("NSMidY(bounds) - height / 2.0", controls)
        self.assertIn("control.intrinsicContentSize.height", controls)
        self.assertIn("[_activeControl isKindOfClass:NSScrollView.class]", controls)
        text_host = controls.split("@implementation LiteLLMAppKitTextFieldHostView", 1)[1].split("@end", 1)[0]
        self.assertNotIn("[_activeControl isKindOfClass:NSTextField.class]", text_host)
        self.assertIn("_host.fillsHeight = NO;", controls)
        for native_control in (
            "_host.control = _button;",
            "_host.control = _checkbox;",
            "_host.control = _picker;",
            "_host.control = _control;",
            "_host.control = _switch;",
            "_host.control = _field;",
        ):
            self.assertIn(native_control, controls)

        self.assertIn("NSTableCellView *cell", controls)
        self.assertIn("NSFont *TableCellFont()", controls)
        self.assertIn("label.font = TableCellFont();", controls)
        self.assertIn("column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);", controls)
        self.assertIn("constraintEqualToAnchor:cell.leadingAnchor constant:8", controls)

    def test_macos_tables_only_show_scrollers_for_overflow(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        self.assertIn("_scrollView.scrollerStyle = NSScrollerStyleLegacy", controls)
        self.assertIn("_scrollView.hasHorizontalScroller = NO", controls)
        self.assertIn("_scrollView.hasVerticalScroller = NO", controls)
        self.assertIn("_scrollView.horizontalScrollElasticity = NSScrollElasticityNone", controls)
        self.assertIn("_scrollView.verticalScrollElasticity = NSScrollElasticityNone", controls)
        self.assertIn("@interface LiteLLMTableScrollView : NSScrollView", controls)
        self.assertIn("- (void)scrollWheel:(NSEvent *)event", controls)
        self.assertIn("BOOL TableScrollViewCanConsume(NSScrollView *scrollView, NSEvent *event, BOOL acceptsVerticalScroll, BOOL acceptsHorizontalScroll);", controls)
        self.assertIn("BOOL ForwardWheelToParent(NSView *view, NSEvent *event);", controls)
        self.assertIn("if (TableScrollViewCanConsume(self, event, _acceptsVerticalScroll, _acceptsHorizontalScroll))", controls)
        self.assertIn("if (ForwardWheelToParent(self, event)) return;", controls)
        self.assertIn("_scrollView.acceptsVerticalScroll = NO", controls)
        self.assertIn("@interface LiteLLMTableClipView : NSClipView", controls)
        self.assertIn("_clipView.acceptsVerticalScroll = NO", controls)
        self.assertIn("_scrollView.contentView = _clipView;", controls)
        self.assertIn("_clipView.acceptsVerticalScroll = needsVerticalScroller;", controls)
        self.assertNotIn("constrainBoundsRect", controls)
        self.assertIn("@interface LiteLLMTableView : NSTableView", controls)
        self.assertIn("_tableView.acceptsVerticalScroll = NO", controls)
        self.assertIn("NSScrollView *owner = ParentScrollView(self);", controls)
        self.assertIn("TableScrollViewCanConsume(owner, event", controls)
        self.assertIn("BOOL TableScrollViewCanConsume(NSScrollView *scrollView, NSEvent *event", controls)
        self.assertIn("const NSRect visible = [scrollView.documentView convertRect:scrollView.contentView.bounds fromView:scrollView.contentView];", controls)
        self.assertIn("column.minWidth = 1;", controls)
        self.assertIn("column.maxWidth = CGFLOAT_MAX;", controls)
        self.assertIn("const bool rowsChanged = oldViewProps.rowKeys != newViewProps.rowKeys ||", controls)
        self.assertIn("oldViewProps.cells != newViewProps.cells ||", controls)
        self.assertIn("overflowBehaviorChanged || rowsChanged;", controls)
        self.assertNotIn("_dataSignature", controls)
        self.assertNotIn("nextDataSignature", controls)
        self.assertIn("- (void)updateScrollerVisibility", controls)
        self.assertIn("const CGFloat headerHeight = _tableView.headerView == nil ? 0 : NSHeight(_tableView.headerView.frame);", controls)
        self.assertIn("const CGFloat dataViewportHeight = MAX(0, NSHeight(_scrollView.contentView.bounds) - headerHeight);", controls)
        self.assertIn("const NSInteger rowCount = _tableView.numberOfRows;", controls)
        self.assertIn("NSMaxY([_tableView rectOfRow:rowCount - 1])", controls)
        self.assertNotIn("_tableView.numberOfRows * _tableView.rowHeight", controls)
        self.assertIn("const BOOL needsVerticalScroller = rowsHeight > dataViewportHeight;", controls)
        self.assertIn("- (void)updateColumnMinimumWidths", controls)
        self.assertIn("if (!viewProps.scrollTrailingColumnOverflow || columnCount == 0", controls)
        self.assertIn("if (newViewProps.scrollTrailingColumnOverflow) {\n      [self updateColumnMinimumWidths];", controls)
        self.assertIn("const CGFloat textWidth = ceil([value sizeWithAttributes:@{NSFontAttributeName: TableCellFont()}].width)", controls)
        self.assertIn("MIN(_requestedColumnWidths[index], minimumWidths[index])", controls)
        self.assertIn("const CGFloat availableColumnWidth = NSWidth(visibleBounds);", controls)
        self.assertIn("const auto minimumContentWidth = [&]()", controls)
        self.assertIn("const BOOL needsHorizontalScroller = minimumContentWidth() > availableColumnWidth + 0.5 ||", controls)
        self.assertIn("preferredContentWidth > availableColumnWidth + 0.5", controls)
        self.assertIn("std::vector<CGFloat> laidOutColumnWidths = _requestedColumnWidths;", controls)
        self.assertIn("laidOutColumnWidths[index] = MAX(laidOutColumnWidths[index], _tableView.tableColumns[index].minWidth);", controls)
        self.assertIn("const CGFloat reduction = MIN(deficit, MAX(0, laidOutColumnWidths[index] - minimumWidth));", controls)
        self.assertIn("_automaticColumnAdjustments[index] = width - _requestedColumnWidths[index];", controls)
        self.assertNotIn("contentWidth > availableColumnWidth + 0.5", controls)
        self.assertIn("cellHorizontalPadding?: WithDefault<Float, 8>;", (SHARED / "ui" / "macos" / "NativeTableNativeComponent.ts").read_text(encoding="utf-8"))
        self.assertIn("firstColumnHorizontalPadding?: WithDefault<Float, 8>;", (SHARED / "ui" / "macos" / "NativeTableNativeComponent.ts").read_text(encoding="utf-8"))
        self.assertIn("preserveColumnWidths?: WithDefault<boolean, false>;", (SHARED / "ui" / "macos" / "NativeTableNativeComponent.ts").read_text(encoding="utf-8"))
        self.assertIn("scrollTrailingColumnOverflow?: WithDefault<boolean, true>;", (SHARED / "ui" / "macos" / "NativeTableNativeComponent.ts").read_text(encoding="utf-8"))
        self.assertIn("borderless?: WithDefault<boolean, false>;", (SHARED / "ui" / "macos" / "NativeTableNativeComponent.ts").read_text(encoding="utf-8"))
        self.assertIn("borderless?: WithDefault<boolean, false>;", (SHARED / "ui" / "windows" / "NativeTableNativeComponent.ts").read_text(encoding="utf-8"))
        native_controls = (SHARED / "ui" / "NativeControls.tsx").read_text(encoding="utf-8")
        self.assertIn("framed?: boolean;", native_controls)
        self.assertIn("borderless: !framed,", native_controls)
        self.assertIn("borderless={borderless}", (SHARED / "ui" / "AppKitControls.tsx").read_text(encoding="utf-8"))
        self.assertIn("preserveColumnWidths?: boolean;", native_controls)
        self.assertIn("preserveColumnWidths={preserveColumnWidths}", native_controls)
        self.assertIn("scrollTrailingColumnOverflow?: boolean;", native_controls)
        self.assertIn("scrollTrailingColumnOverflow={scrollTrailingColumnOverflow}", native_controls)
        self.assertIn("cellHorizontalPadding={0}", (SHARED / "ui" / "LiteLLMMenuApp.tsx").read_text(encoding="utf-8"))
        self.assertIn("firstColumnHorizontalPadding={0}", (SHARED / "ui" / "LiteLLMMenuApp.tsx").read_text(encoding="utf-8"))
        logs_ui = (SHARED / "ui" / "LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        logs_workspace = logs_ui.split("function LogsWorkspace", 1)[1].split("function Section", 1)[0]
        self.assertIn(
            "columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths",
            logs_workspace,
        )
        self.assertNotIn("scrollTrailingColumnOverflow", logs_workspace)
        self.assertIn(
            "((viewProps.preserveColumnWidths || viewProps.scrollTrailingColumnOverflow || hasUserColumnResize())",
            controls,
        )
        self.assertIn("std::vector<CGFloat> _measuredColumnWidths;", controls)
        self.assertIn("std::vector<bool> _userResizedColumns;", controls)
        self.assertIn("_measuredColumnWidths = minimumWidths;", controls)
        self.assertIn("_userResizedColumns[resizedColumn] = true;", controls)
        self.assertIn(
            "_requestedColumnWidths[resizedColumn] = MAX(_tableView.tableColumns[resizedColumn].minWidth, width);",
            controls,
        )
        self.assertIn("_userResizedColumns.size() == columnCount && !_userResizedColumns.back()", controls)
        self.assertIn("laidOutColumnWidths.back() = MAX(", controls)
        self.assertIn("_measuredColumnWidths.back() + trailingContentInset", controls)
        self.assertNotIn("laidOutColumnWidths[index] = MAX(laidOutColumnWidths[index], _measuredColumnWidths[index]);", controls)
        self.assertIn("MAX(dataViewportHeight, rowsHeight));", controls)
        self.assertIn("_scrollView.acceptsVerticalScroll = needsVerticalScroller;", controls)
        self.assertIn("_tableView.acceptsVerticalScroll = needsVerticalScroller;", controls)
        self.assertIn("headerFrame.size.height = newViewProps.compact ? 24 : 28;", controls)
        self.assertIn("if (selectionChanged && _scrollView.hasVerticalScroller)", controls)
        self.assertIn("_tableView.action = @selector(handleRowClick:);", controls)
        self.assertIn("- (void)handleRowClick", controls)
        table_start = controls.index("@implementation LiteLLMAppKitTableComponentView")
        table_end = controls.index("Class<RCTComponentViewProtocol> LiteLLMAppKitTableCls", table_start)
        table = controls[table_start:table_end]
        self.assertIn("- (void)prepareForRecycle", table)
        self.assertIn("_props = defaultProps;", table)
        self.assertIn("[_tableView reloadData];", table)
        self.assertIn("_hasLoadedData = NO;", table)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", table)
        self.assertNotIn("_frameView.framed = YES;", table)

    def test_macos_menu_autostart_fallback_uses_localization(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertIn('"autoStart": "Auto Start at Login"', leaf)
        self.assertIn('case "toggle-autostart": return localized("autoStart", fallback: "Auto Start at Login")', leaf)

    def test_macos_text_editors_autohide_unused_scrollbars(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        editor_start = controls.index("@implementation LiteLLMAppKitTextEditorComponentView")
        editor_end = controls.index("@interface LiteLLMAppKitCodeWebViewComponentView", editor_start)
        editor = controls[editor_start:editor_end]
        self.assertIn("_scrollView.autohidesScrollers = YES;", editor)

    def test_macos_checkbox_and_switch_do_not_revert_the_native_click_state(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        checkbox_changed = controls.split("- (void)changed:(__unused id)sender", 1)[1].split("- (NSView *)accessibilityElement", 1)[0]
        switch_changed = controls.rsplit("- (void)changed:(__unused id)sender", 1)[1].split("- (NSView *)accessibilityElement", 1)[0]
        self.assertNotIn("_checkbox.state = viewProps.value", checkbox_changed)
        self.assertNotIn("_switch.state = viewProps.value", switch_changed)
        self.assertIn("constraintEqualToAnchor:cell.trailingAnchor constant:-8", controls)
        self.assertIn("constraintEqualToAnchor:cell.centerYAnchor", controls)

    def test_macos_boolean_controls_do_not_request_layout_for_value_only_updates(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        checkbox = controls.split("@implementation LiteLLMAppKitCheckboxComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitCheckboxCls", 1
        )[0]
        switch = controls.split("@implementation LiteLLMAppKitSwitchComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitSwitchCls", 1
        )[0]

        self.assertIn("const BOOL labelChanged = oldViewProps.label != newViewProps.label;", checkbox)
        self.assertIn("const BOOL labelVisibilityChanged = oldViewProps.labelVisible != newViewProps.labelVisible;", checkbox)
        self.assertIn('_checkbox.title = newViewProps.labelVisible ? label : @"";', checkbox)
        self.assertIn("_checkbox.accessibilityLabel = label;", checkbox)
        self.assertIn("const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;", checkbox)
        self.assertIn("if (labelChanged || labelVisibilityChanged || compactChanged) {\n    [_host setNeedsLayout:YES];\n  }", checkbox)
        self.assertNotIn("[_host setNeedsLayout:YES];", switch)

    def test_macos_choice_controls_keep_the_native_selection_until_react_confirms_it(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        picker = controls.split("@implementation LiteLLMAppKitPickerComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitPickerCls", 1
        )[0]
        segmented = controls.split("@implementation LiteLLMAppKitSegmentedControlComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitSegmentedControlCls", 1
        )[0]
        button = controls.split("@implementation LiteLLMAppKitButtonComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitButtonCls", 1
        )[0]

        self.assertIn("const BOOL labelsChanged = oldViewProps.labels != newViewProps.labels;", picker)
        self.assertIn("const BOOL compactChanged = oldViewProps.compact != newViewProps.compact;", picker)
        self.assertIn("if (labelsChanged || compactChanged) {\n    [_host setNeedsLayout:YES];\n  }", picker)
        self.assertNotIn("controlledIndex", picker)
        self.assertIn("const BOOL labelsChanged = oldViewProps.labels != newViewProps.labels;", segmented)
        self.assertIn("if (labelsChanged || compactChanged) {\n    [_host setNeedsLayout:YES];\n  }", segmented)
        self.assertNotIn("_control.selectedSegment = SegmentIndex(viewProps.labels", segmented)
        self.assertIn("const BOOL titleChanged = oldViewProps.title != newViewProps.title;", button)
        self.assertIn("if (titleChanged || symbolChanged || linkChanged || compactChanged) {\n    [_host setNeedsLayout:YES];\n  }", button)
        self.assertIn('symbolName = @"pause.fill";', controls)
        self.assertIn('symbolName = @"play.fill";', controls)
        self.assertIn('symbolName = @"minus";', controls)
        self.assertIn('symbolName = @"trash";', controls)
        self.assertIn("_button.imagePosition = symbolImage == nil ? NSNoImage : NSImageOnly;", button)

        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        self.assertIn('const auto symbol = props.symbol.value_or("");', windows)
        self.assertIn('icon.FontFamily(FontFamily(L"Segoe MDL2 Assets"));', windows)
        self.assertIn('if (symbol == "check")', windows)
        self.assertIn('else if (symbol == "power-on" || symbol == "power-off")', windows)
        self.assertIn('else if (symbol == "copy")', windows)
        self.assertIn('else if (symbol == "edit")', windows)

    def test_macos_buttons_clear_default_action_state_before_fabric_reuse(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        button = controls.split("@implementation LiteLLMAppKitButtonComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitButtonCls", 1
        )[0]
        recycle = button.split("- (void)prepareForRecycle", 1)[1].split("- (void)pressed:", 1)[0]

        self.assertIn("_props = defaultProps;", recycle)
        self.assertIn('((LiteLLMNavigationLinkButton *)_button).linkMode = NO;', recycle)
        self.assertIn('_button.keyEquivalent = @"";', recycle)
        self.assertIn('[_button.window setDefaultButtonCell:nil];', button)
        self.assertIn('if (!link && !newViewProps.primary) {', button)
        self.assertIn("_button.hasDestructiveAction = NO;", recycle)
        self.assertIn("_button.contentTintColor = nil;", recycle)
        self.assertIn("_button.enabled = YES;", recycle)

    def test_native_boolean_controls_skip_unrelated_prop_rewrites(self) -> None:
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        self.assertIn("const BOOL selectionChanged = oldViewProps.selectedKey != newViewProps.selectedKey;", mac)
        self.assertIn("if (selectionChanged || _tableView.selectedRow != selectedIndex)", mac)
        self.assertIn("if (selectionChanged && _scrollView.hasVerticalScroller) {\n      [_tableView scrollRowToVisible:selectedIndex];", mac)
        self.assertIn("BOOL TableIsFollowingBottom(NSScrollView *scrollView, NSTableView *tableView)", mac)
        self.assertIn("const BOOL initialDataLoad = !_hasLoadedData;", mac)
        self.assertIn("(initialDataLoad || TableIsFollowingBottom(_scrollView, _tableView))", mac)
        self.assertIn("const BOOL wasFollowingBottom = newViewProps.followBottom && dataChanged", mac)
        self.assertIn("newViewProps.followBottom && dataChanged && wasFollowingBottom", mac)
        checkbox = windows.split("struct CheckboxComponentView final", 1)[1].split(
            "struct TableComponentView final", 1
        )[0]
        switch = windows.split("struct SwitchComponentView final", 1)[1].split(
            "struct SelectableRowComponentView final", 1
        )[0]
        self.assertIn("ApplyProps(old_props);", checkbox)
        self.assertIn("const bool value_changed = !old_props || old_props->value != props.value;", checkbox)
        self.assertIn("if (value_changed) checkbox_.IsChecked(props.value.value_or(false));", checkbox)
        self.assertIn("ApplyProps(old_props);", switch)
        self.assertIn("if (value_changed) {\n      syncing_ = true;\n      value_ = props.value.value_or(false);\n      UpdateGlyph();", switch)

        table = windows.split("struct TableComponentView final", 1)[1].split(
            "struct TextEditorComponentView final", 1
        )[0]
        self.assertIn("bool ListIsFollowingBottom(ListView const& list)", windows)
        self.assertIn("const bool was_following_bottom = props.followBottom.value_or(false) && rows_changed", table)
        self.assertIn("(!has_applied_ || ListIsFollowingBottom(list_))", table)
        self.assertIn("props.followBottom.value_or(false) && rows_changed && was_following_bottom", table)

    def test_table_scrollbars_only_appear_when_they_are_needed(self) -> None:
        native_controls = (SHARED / "ui" / "NativeControls.tsx").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        self.assertIn('tableFallback: { minHeight: 120, borderWidth: 1, borderColor: "#d4d4d8", overflow: "hidden" }', native_controls)
        self.assertNotIn('tableFallback: { minHeight: 120, overflow: "scroll" }', native_controls)
        table = windows.split("struct TableComponentView final", 1)[1].split(
            "struct TextEditorComponentView final", 1
        )[0]
        self.assertIn("ScrollViewer::SetHorizontalScrollBarVisibility(", table)
        self.assertIn("list_.IsItemClickEnabled(true);", table)
        self.assertIn("list_.ItemClick", table)
        self.assertIn("ScrollBarVisibility::Disabled", table)
        self.assertIn("ScrollViewer::SetVerticalScrollBarVisibility(", table)
        self.assertIn("ScrollBarVisibility::Auto", table)
        self.assertIn("std::max(88.0, static_cast<double>(widths[index]))", windows)
        self.assertIn("table_.MinWidth(TableWidth(props.columnWidths, column_count));", table)
        self.assertIn("horizontal_scroller_.HorizontalScrollBarVisibility(", table)

    def test_native_text_inputs_do_not_rewrite_active_text_for_unrelated_prop_updates(self) -> None:
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        mac_text_field = mac.split(
            "@implementation LiteLLMAppKitTextFieldComponentView", 1
        )[1].split("Class<RCTComponentViewProtocol> LiteLLMAppKitTextFieldCls", 1)[0]
        self.assertIn(
            "const BOOL shouldSynchronizeText = activeControlChanged || oldViewProps.value != newViewProps.value;",
            mac_text_field,
        )
        self.assertIn(
            "if (shouldSynchronizeText && ![activeField.stringValue isEqualToString:value])",
            mac_text_field,
        )
        self.assertIn(
            "if (shouldSynchronizeText && ![_multilineField.string isEqualToString:value])",
            mac_text_field,
        )
        self.assertIn(
            "const BOOL shouldUpdateDisabled = activeControlChanged || oldViewProps.disabled != newViewProps.disabled;",
            mac_text_field,
        )
        self.assertIn("- (void)prepareForRecycle", mac_text_field)
        self.assertIn("_field.stringValue = @\"\";", mac_text_field)
        self.assertIn("_multilineField.string = @\"\";", mac_text_field)

        windows_text_field = windows.split("struct TextInputComponentView final", 1)[1].split(
            "struct SecureTextInputComponentView final", 1
        )[0]
        self.assertIn("ApplyProps(nullptr);", windows_text_field)
        self.assertIn("ApplyProps(old_props);", windows_text_field)
        self.assertIn("const bool text_changed = !old_props || old_props->value != props.value;", windows_text_field)
        self.assertIn("if (text_changed && text_box_.Text() != value)", windows_text_field)
        self.assertNotIn("if (text_box_.Text() != value) text_box_.Text(value);", windows_text_field)

    def test_macos_split_view_ignores_provisional_mount_widths(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")

        mount = controls.split("- (void)mountChildComponentView", 1)[1].split(
            "- (void)unmountChildComponentView", 1
        )[0]
        unmount = controls.split("- (void)unmountChildComponentView", 1)[1].split(
            "- (void)applyRequestedPaneWidth", 1
        )[0]
        for lifecycle in (mount, unmount):
            self.assertIn("_synchronizingDivider = YES;", lifecycle)
            self.assertIn("[_splitView adjustSubviews];", lifecycle)
            self.assertIn("_synchronizingDivider = NO;", lifecycle)
        self.assertIn("if (_synchronizingDivider || notification.object != _splitView", controls)
        self.assertIn('notification.userInfo[@"NSSplitViewDividerIndex"]', controls)
        self.assertIn("- (void)layout", controls)
        self.assertIn("_needsInitialPaneLayout", controls)
        self.assertIn("NSSplitView may choose an equal split", controls)
        self.assertIn("- (void)scheduleInitialPaneReplay", controls)
        self.assertIn("dispatch_async(dispatch_get_main_queue()", controls)
        self.assertIn("_paneReplayGeneration", controls)
        self.assertIn("resizeSubviewsWithOldSize", controls)

    def test_macos_split_view_gives_fabric_the_same_controlled_pane_geometry(self) -> None:
        native_controls = (SHARED / "ui" / "NativeControls.tsx").read_text(encoding="utf-8")

        self.assertIn("if (!paneOpen)", native_controls)
        self.assertIn(
            'return <View style={[styles.splitFallback, style]}><View style={styles.splitTrailing}>{trailing}</View></View>;',
            native_controls,
        )
        macos = native_controls.rsplit('if (Platform.OS === "macos")', 1)[1].split(
            'return <View style={[styles.splitFallback, style]}>', 1
        )[0]
        self.assertIn("<View style={[styles.splitLeading, { width: paneWidth }]}>", macos)
        self.assertIn("<View style={styles.splitTrailing}>{trailing}</View>", macos)
        self.assertIn('splitView: { minHeight: 120, flexDirection: "row" }', native_controls)

    def test_windows_controls_are_registered_fabric_winui_content_islands(self) -> None:
        controls = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        app = (WIN_PROJECT / "LiteLLMMenu.cpp").read_text(encoding="utf-8")

        self.assertIn("ContentIslandComponentView", controls)
        self.assertIn("RegisterWinUIControls(packageBuilder)", app)
        for native_class in (
            "Button",
            "CheckBox",
            "ComboBox",
            "ToggleButton",
            "TextBox",
            "TextBlock",
        ):
            self.assertIn(native_class, controls)
        self.assertNotIn("ToggleSwitch", controls)
        for component in (
            "RegisterLiteLLMWinUIButtonNativeComponent",
            "RegisterLiteLLMWinUISegmentedControlNativeComponent",
            "RegisterLiteLLMWinUITextInputNativeComponent",
            "RegisterLiteLLMWinUISwitchNativeComponent",
            "RegisterLiteLLMWinUISelectableRowNativeComponent",
            "RegisterLiteLLMWinUICheckboxNativeComponent",
            "RegisterLiteLLMWinUIPickerNativeComponent",
        ):
            self.assertIn(component, controls)

    def test_windows_selectable_rows_truncate_long_account_labels(self) -> None:
        controls = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        selectable_row = controls.split("struct SelectableRowComponentView final", 1)[1].split(
            "template <typename TComponent>", 1
        )[0]
        self.assertEqual(2, selectable_row.count("TextWrapping(winrt::Microsoft::UI::Xaml::TextWrapping::NoWrap)"))
        self.assertEqual(2, selectable_row.count("TextTrimming(winrt::Microsoft::UI::Xaml::TextTrimming::CharacterEllipsis)"))

    def test_windows_controls_use_winui_theme_resources_for_state_colors(self) -> None:
        controls = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        for resource in (
            "AccentFillColorDefaultBrush",
            "SystemFillColorCriticalBrush",
            "SubtleFillColorSecondaryBrush",
            "SubtleFillColorTransparentBrush",
        ):
            self.assertIn(resource, controls)
        self.assertIn("Application::Current().Resources()", controls)

    def test_native_text_editors_preserve_viewport_and_follow_log_tail(self) -> None:
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        adapter = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeTextEditorNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        windows_spec = (
            SHARED / "ui/windows/NativeTextEditorNativeComponent.ts"
        ).read_text(encoding="utf-8")
        windows_codegen = (
            WIN_PROJECT
            / "codegen/react/components/LiteLLMMenu/LiteLLMWinUITextEditor.g.h"
        ).read_text(encoding="utf-8")

        self.assertIn("TextEditorIsFollowingBottom", mac)
        self.assertIn("RestoreTextEditorViewport", mac)
        self.assertIn("CaptureTextEditorViewport", mac)
        self.assertIn("state.origin", mac)
        self.assertIn("state.selection", mac)
        self.assertIn("state.followsBottom", mac)
        self.assertIn("? maximumY", mac)
        self.assertIn("newViewProps.documentKey", mac)
        self.assertIn("_viewportStates", mac)

        self.assertIn("FindTextEditorScrollViewer", windows)
        self.assertIn("viewer.ScrollableHeight() - previous_vertical_offset <= 4.0", windows)
        self.assertIn("previous_selection_start", windows)
        self.assertIn("editor_.Text().size()", windows)
        self.assertIn("IReference<double>", windows)
        self.assertIn("viewer.ChangeView(", windows)
        self.assertIn("props.documentKey", windows)
        self.assertIn("viewport_states_", windows)

        for source in (mac_spec, windows_spec, adapter):
            self.assertIn("documentKey", source)
        # Logs are rendered with the native structured table. Text-editor
        # viewport behavior is still covered by the settings editors above;
        # the log surface must not regress to a giant raw text editor.
        self.assertNotIn('documentKey={`logs:${selected}`}', ui)
        self.assertIn("() => fitLogColumns(logColumns(selected, translate), tableWidth)", ui)
        self.assertIn("const nativeTableColumns = useMemo(", ui)
        self.assertIn("<NativeTable columns={nativeTableColumns}", ui)
        self.assertIn("REACT_FIELD(documentKey)", windows_codegen)


if __name__ == "__main__":
    unittest.main()
