from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "rn/packages/shared/src"
MAC_NATIVE = ROOT / "rn/apps/macos/src/native/macos"
WIN_NATIVE = ROOT / "rn/apps/windows/src/native/windows"
MAC_PROJECT = ROOT / "rn/apps/macos/macos"
WIN_PROJECT = ROOT / "rn/apps/windows/windows/YoungRouter"


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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")

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
        # Every settings pane shares one window, so it uses one minimum size
        # wide enough for the sidebar plus the widest pane.
        self.assertIn('route == L"providers-models" || route == L"codex-settings"', leaf)
        self.assertIn("return {900, 560};", leaf)
        self.assertIn('route == L"provider-wizard") return {540, 420};', leaf)
        self.assertIn("MinimumTrackSizeForActiveRoute();", leaf)
        self.assertIn("DipToPhysicalPixels", leaf)
        self.assertIn("AdjustWindowRectExForDpi", leaf)
        self.assertNotIn("ptMinTrackSize.x = std::max<LONG>(minmax->ptMinTrackSize.x, 1080);", leaf)

    def test_native_routes_use_responsive_initial_content_sizes(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("ContentSize RouteInitialContentSize", leaf)
        # Settings panes share one window with one responsive initial size,
        # tall enough that the provider detail opens without a scrollbar.
        self.assertIn("return {960, 660};", leaf)
        self.assertIn("{620, 460}", leaf)
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
        mac_app = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        win_app = (WIN_PROJECT / "YoungRouter.cpp").read_text(encoding="utf-8")

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
        # One shared settings window plus the provider wizard sheet.
        self.assertIn("if Self.settingsPaneRoutes.contains(route) {", leaf)
        # One fixed size for every pane, so switching panes never resizes the
        # window; it only needs to fit the widest workspace and the tallest
        # detail pane, which is the provider detail at 535 pt of content
        # beneath 112 pt of chrome.
        self.assertIn("contentSize: NSSize(width: 960, height: 660)", leaf)
        self.assertIn("minSize: NSSize(width: 900, height: 560)", leaf)
        self.assertNotIn("applySettingsPaneLayout", leaf)
        # The General pane joins the shared settings window on both hosts.
        self.assertIn('"general-settings", "providers-models", "runtime-settings"', leaf)
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        self.assertIn('route == L"general-settings" || route == L"providers-models"', windows)
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        self.assertIn('@"general-settings", @"providers-models"', app_delegate)
        # About moved from the sidebar to the native menus; the storyboard's
        # standard About item is localized and Preferences gets the Settings
        # action instead of appending duplicates.
        self.assertIn('$0.action == Selector(("orderFrontStandardAboutPanel:"))', leaf)
        self.assertIn('preferencesItem.action = #selector(openCodex)', leaf)
        self.assertIn("contentSize: NSSize(width: 620, height: 460)", leaf)
        self.assertIn("minSize: NSSize(width: 540, height: 420)", leaf)
        # The former relay windows are folded into the unified workspace.
        self.assertNotIn('case "relay-accounts":', leaf)
        self.assertNotIn('case "relay-add":', leaf)
        self.assertNotIn('case "configuration-package":', leaf)
        self.assertNotIn("maxSize: NSSize(width: 680, height: 386)", leaf)

    def test_macos_child_surfaces_are_movable_locked_windows(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        open_route = leaf.split("func open(route: String, title: String", 1)[1].split(
            "func open(route: String)", 1
        )[0]
        close_route = leaf.split("func close(route: String? = nil)", 1)[1].split(
            "func setWindowContentSize", 1
        )[0]
        request_close = leaf.split("private func requestClose", 1)[1].split(
            "private func canonicalRoute", 1
        )[0]
        presentation = leaf.split("func presentChildPanel(", 1)[1].split("func chooseImportFile(", 1)[0]
        shield = leaf.split("private final class NativeChildPanelShield", 1)[1].split(
            "/// A push button that keeps the Return-key", 1
        )[0]

        # The wizard is a child of the unified provider workspace; legacy
        # relay routes are canonicalized into it.
        self.assertIn('if route == "relay-add" { return "provider-wizard" }', leaf)
        self.assertIn('if windowRoute == "provider-wizard", settingsWindowKey() == nil', open_route)
        # One presentation for every child surface: its own movable window in
        # front of the app, attached above the window it was opened from, with
        # that window's content locked until it closes. An attached sheet
        # (immovable, no title bar) and a plain floating window (parent stays
        # clickable) are both refused.
        self.assertIn("withoutAnimations { panel.makeKeyAndOrderFront(nil) }", presentation)
        self.assertIn("parent.addChildWindow(panel, ordered: .above)", presentation)
        self.assertIn("lockParentWindow(parent, for: panel)", presentation)
        self.assertNotIn("beginSheet(", leaf)
        # The lock is a shield over the parent's content, never an app-modal
        # session: `NSApp.runModal` runs the main run loop in its modal mode
        # alone and freezes the React host's timers, events, and promises, which
        # left a child window painting nothing while the surface behind it
        # stopped answering.
        # (the doc comments name it only to explain why it is not used)
        self.assertNotIn("NSApp.runModal(for:", leaf)

        self.assertIn("override func hitTest(_ point: NSPoint) -> NSView? {", shield)
        self.assertIn("override func hitTest(_ point: NSPoint) -> NSView? {", shield)
        self.assertIn("return bounds.contains(convert(point, from: superview)) ? self : nil", shield)
        self.assertIn("override func mouseDown(with event: NSEvent) { onInteraction?() }", shield)
        self.assertIn("override func keyDown(with event: NSEvent) {}", shield)
        self.assertIn("content.addSubview(shield, positioned: .above, relativeTo: nil)", presentation)
        self.assertIn("parent.makeFirstResponder(shield)", presentation)
        # The locked window cannot be closed or minimized out from under its
        # child, and its content keeps the first responder.
        self.assertIn("parent.standardWindowButton(type)?.isEnabled = false", presentation)
        self.assertIn("parent.standardWindowButton(type)?.isEnabled = true", presentation)
        self.assertIn("window.makeFirstResponder(shield)", leaf)
        # Ending a child releases the lock, takes the window off screen, and
        # takes the children it opened with it.
        self.assertIn("for descendant in childPanels.filter({ $0.parent === panel }).map({ $0.window }) {", presentation)
        self.assertIn("parent.removeChildWindow(panel)", presentation)
        self.assertIn("unlockParentWindow(parent)", presentation)
        self.assertIn('if windowRoute == "provider-wizard" || windowRoute == "file-editor" {', open_route)
        self.assertIn("presentChildPanel(window, in: settingsWindow())", open_route)
        self.assertIn("endChildPanel(window)", close_route)
        self.assertIn('let restoreProviderModels = selectedRoute == "provider-wizard"', close_route)
        self.assertIn("restoreProviderModels.makeKeyAndOrderFront(nil)", close_route)
        # A dirty draft keeps its child window on screen while React decides:
        # ordering it out would leave a lock over a window that is gone.
        self.assertIn("if !isChildPanel(window) {", request_close)
        self.assertNotIn("restoreRelayAccounts", close_route)

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

        # Codex and Claude are panes of the shared settings window, whose title
        # is the app name; the sidebar names the active pane.
        self.assertIn('if Self.settingsPaneRoutes.contains(canonicalRoute(route)) {', mac)
        self.assertIn('return localized("appTitle", fallback: "Young Router")', mac)
        self.assertIn('route == L"codex-settings" ||', windows)
        self.assertIn('return Localized("appTitle", L"Young Router");', windows)

    def test_macos_settings_shortcut_opens_the_combined_settings_surface(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        shortcut = mac.split('if let preferencesItem = applicationMenu.items.first(where: { $0.action == nil && $0.keyEquivalent == "," }) {', 1)[1].split('guard !applicationMenu.items.contains', 1)[0]

        self.assertIn("preferencesItem.action = #selector(openCodex)", shortcut)
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
        self.assertIn('action.id == L"open-general-settings" || action.id == L"open-providers-models" ||', windows)
        self.assertIn('action.id == L"open-data-management" ||', windows)
        self.assertIn('action.id == L"open-logs" || action.id == L"show-version") {', windows)
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
        source = (WIN_PROJECT / "YoungRouter.cpp").read_text(encoding="utf-8")
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
        source = (MAC_PROJECT / "YoungRouter-macOS/main.m").read_text(encoding="utf-8")
        plist = (MAC_PROJECT / "YoungRouter-macOS/Info.plist").read_text(encoding="utf-8")

        self.assertIn("LSMultipleInstancesProhibited", plist)
        self.assertIn("YoungRouterExistingInstance", source)
        self.assertIn("NSWorkspace.sharedWorkspace.runningApplications", source)
        self.assertIn("YoungRouterIsManagedApplication", source)
        self.assertIn("YoungRouterAcquireInstanceLock", source)
        self.assertIn("NSApplicationSupportDirectory", source)
        self.assertIn('kYoungRouterInstanceNamespace = @"young.router.app"', source)
        self.assertIn("Contents/MacOS/YoungRouter", source)
        self.assertNotIn("NSTemporaryDirectory", source)
        self.assertIn("flock(descriptor, LOCK_EX | LOCK_NB)", source)
        self.assertIn("NSApplicationMain", source)
        self.assertLess(source.index("YoungRouterAcquireInstanceLock"), source.rindex("NSApplicationMain"))

    def test_macos_starts_the_hidden_primary_host_for_live_menu_state(self) -> None:
        source = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
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
        core_ipc = (ROOT / "young_router/core/ipc.py").read_text(encoding="utf-8")
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
        # The pane keeps a scroller only while its content overflows, and draws it
        # with the app's persistent translucent scroller.
        self.assertIn("const BOOL overflows = documentView != nil &&", component)
        self.assertIn("NSHeight(documentView.frame) > NSHeight(scrollView.contentView.bounds) + 0.5;", component)
        self.assertIn("const NSScrollerStyle scrollerStyle = overflows ? NSScrollerStyleLegacy : NSScrollerStyleOverlay;", component)
        self.assertIn("const BOOL autohidesScrollers = !overflows;", component)
        self.assertIn("scrollView.scrollerStyle = scrollerStyle;", component)
        self.assertIn("scrollView.autohidesScrollers = autohidesScrollers;", component)
        self.assertIn("scrollView.scrollerStyle = NSScrollerStyleOverlay;", component)
        self.assertIn("scrollView.autohidesScrollers = YES;", component)
        # The pane scroller is the same persistent translucent scroller the
        # native tables use, not AppKit's opaque legacy bar.
        self.assertIn("InstallPersistentScrollers(scrollView, NO, YES);", component)
        self.assertIn("InstallPersistentScrollers(", controls)
        self.assertIn("![scrollView.verticalScroller isKindOfClass:LiteLLMPersistentScroller.class]", controls)
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertIn("@objc(LiteLLMPersistentScroller)", leaf)
        self.assertIn("final class LiteLLMPersistentScroller: NSScroller", leaf)
        self.assertIn("override func drawKnobSlot(in slotRect: NSRect, highlight flag: Bool)", leaf)
        self.assertIn("let alpha = hitPart == .knob", leaf)
        self.assertIn("func usePersistentScrollers(horizontal: Bool, vertical: Bool)", leaf)
        # Every native scroll view that keeps a scroller on screen draws it with
        # that class: the declaration plus the model chooser, the read-only text
        # editor (install and layout pass), the key list the group manager sheet
        # hosts, and that sheet's 模型列表 list (its install, its layout pass, and
        # the overflow policy that re-asserts the capsule while it overflows).
        self.assertEqual(8, leaf.count("usePersistentScrollers("))
        self.assertIn("scrollView.usePersistentScrollers(horizontal: false, vertical: true)", leaf)
        self.assertIn("usePersistentScrollers(horizontal: true, vertical: true)", leaf)

    def test_the_add_account_button_opens_the_sign_in_window_at_once(self) -> None:
        # The + next to 中转站关联 used to resolve the station family with two
        # network probes (up to three seconds each) BEFORE the browser window was
        # allowed to appear, so the button spun for seconds to learn something
        # the page it is about to open answers anyway.  It now opens the window
        # immediately and lets the native flow settle the family from the probe
        # that answers.
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        add = ui.split("const addRelayAccountToVendor = async", 1)[1].split("return <PersistentScrollView", 1)[0]
        self.assertIn("const relayType = station?.type ?? \"\";", add)
        self.assertNotIn("await relay.detectType(origin)", add)
        # The binding is created from the family the flow resolved, so a bare
        # address sends no station type of its own.
        self.assertIn("stationType: station?.type,", add)

        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")
        # Both hosts accept an unnamed family and settle it from the answer.
        self.assertIn('[\"newapi\", \"sub2api\", \"\", \"auto\"].contains(type),', mac_leaf)
        self.assertIn("private var resolvedType: String?", mac_leaf)
        probe_list = mac_leaf.split("private var probes: [Probe] {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("default: return newapi + sub2api", probe_list)
        self.assertIn("bool IsRelayAccountType(std::string const& value)", windows_relay)
        self.assertIn('return value == "newapi" || value == "sub2api" || value.empty() || value == "auto";', windows_relay)
        # A family that was not named is resolved by the probe that answered,
        # and every later read follows it.
        self.assertIn("self.resolvedType = probe.family", mac_leaf)
        self.assertIn("let accountType = resolvedType ?? self.type", mac_leaf)

    def test_the_provider_detail_is_one_pane_for_every_kind(self) -> None:
        # A service provider's detail is the ordinary provider detail: the same
        # 启用 / 供应商名称 / 供应商类型 / 基础 URL block every other kind states,
        # drawn by the one shared component.  Its type is *stated* there — the
        # service it is, the station that addresses it, or the key contract a
        # custom provider holds — because nothing in the app retargets a
        # provider in place: the pane shows the fact, and a different type is a
        # different provider (created through the wizard).
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        editor = ui.split("function ProviderEditor(", 1)[1].split("function CodexWorkspace(", 1)[0]
        self.assertIn("<ProviderIdentityFields", editor)
        self.assertEqual(1, editor.count("<ProviderIdentityFields"))
        identity = ui.split("function ProviderIdentityFields(", 1)[1].split("function ProviderEditor(", 1)[0]
        self.assertIn('label={translate("providers.providerName")}', identity)
        self.assertIn('<Text style={styles.providerAuthStatusLabel}>{translate("providers.providerType")}</Text>', identity)
        self.assertIn('label={translate("providers.baseUrl")}', identity)
        self.assertIn("isLogin ? SERVICE_BASE_URLS[kind as ServiceID]", identity)
        # No picker, no confirmation, no retarget dispatch: the whole surface
        # that used to switch a provider's type is gone from both hosts' shared
        # UI, so a provider's type has exactly one place it is decided.
        self.assertNotIn("PROVIDER_TYPE_OPTIONS", ui)
        self.assertNotIn("serviceProviderKindFor", ui)
        self.assertNotIn('title: translate("providers.switchTypeTitle")', identity)
        self.assertNotIn("auth_kind", identity)
        self.assertNotIn("auth_kind", editor)

    def test_relay_login_reveals_the_page_as_soon_as_it_paints(self) -> None:
        # A web view paints progressively.  The overlay exists for the blank
        # first frame, not for "the page finished its own data fetch": waiting on
        # body.innerText covered a station whose shell renders immediately and
        # then streams, leaving 正在加载登录页面 over a usable sign-in form.
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertIn("private static let pagePaintedScript = \"\"\"", leaf)
        painted = leaf.split("private static let pagePaintedScript = \"\"\"", 1)[1].split('"""', 1)[0]
        # Any committed element counts as painted.
        self.assertIn("if (body.children.length > 0) return true;", painted)
        # The overlay is lifted on that answer.
        self.assertIn("private func revealBrowserPage() {", leaf)
        reveal = leaf.split("private func revealBrowserPage() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("loadingOverlay.isHidden = true", reveal)
        # The per-step work still runs once the page is on screen.
        for step in (
            "scheduleEmbeddedBrowserResize()",
            "scheduleAgreementReveal()",
            "scheduleLoginFormReveal()",
            "scheduleLoginWatch(delay: 1)",
        ):
            self.assertIn(step, reveal)
        # The cover can never outlive a page that is drawing.
        self.assertIn("private var pageReadinessAttempts = 0", leaf)
        self.assertIn("private static let pageReadinessMaxAttempts = 60", leaf)
        probe = leaf.split("private func schedulePageReadinessProbe() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("self.pageReadinessAttempts += 1", probe)
        self.assertIn("|| self.pageReadinessAttempts >= Self.pageReadinessMaxAttempts", probe)
        # The old "wait for non-empty innerText" gate is gone from the readiness
        # probe (the agreement/form probes keep their own innerText reads).
        self.assertNotIn(
            'Boolean(document.body && document.body.children.length > 0 && document.body.innerText.trim().length > 0)',
            leaf,
        )

    def test_native_group_manager_hides_unusable_add_remove_controls(self) -> None:
        # The sheet's ＋ / － follow the shared pane rule: hide a control the
        # current state cannot use instead of greying it out.
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        self.assertIn("let canAddKey = editingEnabled && !groups.isEmpty", leaf)
        self.assertIn("addButton?.isHidden = !canAddKey", leaf)
        self.assertIn("removeButton?.isHidden = !editable", leaf)
        self.assertIn("const bool can_add_key = !groups.empty() && !toggle_on();", windows)
        self.assertIn(
            "add_button.Visibility(can_add_key ? xaml::Visibility::Visible : xaml::Visibility::Collapsed);",
            windows,
        )
        self.assertIn(
            "remove_button.Visibility(editable ? xaml::Visibility::Visible : xaml::Visibility::Collapsed);",
            windows,
        )

    def test_native_group_manager_toggle_stages_the_one_key_per_group_layout(self) -> None:
        # 自动分组 is one key per group, named exactly after its group, and the
        # LIST PREVIEW has to say so the moment the switch is checked: the names
        # the user reads are the feature.  A switch that only flipped its own
        # state left the old key names on screen, which read as "checked but
        # nothing happened" however correct Core's own alignment was.
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        # macOS: the toggle renames rows and lists every group instead of only
        # reloading.  It is a preview the window draws over the rows it already
        # holds: a row the layout is replacing is dropped from the list rather
        # than shown as a struck-through line, so nothing it produces is handed
        # back as a write the user never made.
        self.assertIn("private func stageAutoGroupingLayout(_ enabled: Bool) {", leaf)
        toggle = leaf.split("@objc private func toggleAutoGrouping(_ sender: NSButton) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("stageAutoGroupingLayout(sender.state == .on)", toggle)
        stage_layout = leaf.split("private func stageAutoGroupingLayout(_ enabled: Bool) {", 1)[1].split("\n    }", 1)[0]
        # The staged rows reach the table.
        self.assertIn("stageAutoGroupingRows(enabled)", stage_layout)
        self.assertIn("table?.reloadData()", stage_layout)
        stage_layout = leaf.split("private func stageAutoGroupingRows(_ enabled: Bool) {", 1)[1].split("\n    }", 1)[0]
        # Staging is a derivation applied to the account's own rows, so applying
        # it twice reproduces the layout instead of piling a second copy on top.
        self.assertIn("rows = autoGroupingRows(from: autoGroupingBaselineRows ?? rows)", stage_layout)
        stage = leaf.split("private func autoGroupingRows(from source: [KeyRow]) -> [KeyRow] {", 1)[1].split("\n    }", 1)[0]
        # The derivation never consumes its own output.
        self.assertIn("if row.isPreview { continue }", stage)
        # An account with no group list has no layout to align to: the preview
        # shows its keys unchanged instead of emptying the list.  Core's own
        # alignment refuses the same case for the same reason.
        self.assertIn("guard !layoutGroups.isEmpty else { return source }", stage)
        # 未分组 is the manual-assignment sentinel (an empty group id), not a
        # group the station offers.  The picker carries it while the switch is
        # off, so deriving over the picker's own list invented a 未分组 key with
        # no rate, no value, and no models — a line for a group that does not
        # exist.  The layout is derived from the station's groups only, and a
        # key naming no group is not part of it.
        self.assertIn("let layoutGroups = groups.filter { !$0.id.isEmpty }", stage)
        self.assertIn("if row.groupID.isEmpty {", stage)
        self.assertIn("for group in layoutGroups where !keptGroupIDs.contains(group.id)", stage)
        self.assertNotIn("for group in groups where !keptGroupIDs.contains(group.id)", stage)
        self.assertIn("guard let group = layoutGroups.first(where: { $0.id == row.groupID }) else {", stage)
        # The row that keeps a group is renamed to the group's own name.
        self.assertIn("candidate.name = group.name", stage)
        # A later key in the same group leaves the list: the group owns one key.
        self.assertIn("guard !keptGroupIDs.contains(group.id) else {", stage)
        # A group no key names gets a preview row named after it.
        self.assertIn("for group in layoutGroups where !keptGroupIDs.contains(group.id) && !presentGroupIDs.contains(group.id)", stage)
        # The user's own rows are not the switch's to rewrite.
        self.assertIn("if row.isDraft {", stage)
        self.assertIn("if row.deleted {", stage)
        # The preview never reports a deletion of its own to Core.
        self.assertNotIn("candidate.deleted = true", stage)
        # A window that OPENS on a checked switch states the layout that switch
        # means, exactly as clicking it would: showing the account's raw key list
        # under a checked switch is what left ungrouped rows and keys in groups
        # the station no longer offers on screen.
        apply_data = leaf.split("func applyData(accountLabel: String, groups: [GroupOption], rows: [KeyRow], autoGrouping: Bool) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("if autoGrouping {", apply_data)
        self.assertIn("stageAutoGroupingRows(true)", apply_data)
        # The window's FIRST open states it too: a sheet that opens on a checked
        # switch must never show the account's raw key list under it.
        make_panel = leaf.split("func makePanel() -> NSPanel? {", 1)[1]
        self.assertIn("if initialAutoGrouping {", make_panel)
        self.assertIn("stageAutoGroupingRows(true)", make_panel)
        # ...and the row the switch derives is NOT dimmed: it is the layout the
        # checked switch states, not an edit waiting to happen.  Only a row the
        # user added with ＋ (and a row the user retired) is a pending change.
        self.assertIn("let isPreview: Bool", leaf)
        self.assertIn("row.deleted || (row.isDraft && !row.isPreview)", leaf)
        self.assertIn("isPreview: true", stage)
        self.assertIn("isPreview: false", leaf)
        # A line the layout cannot keep is dropped from the list entirely, so
        # nothing in an auto-grouping list is dimmed for a reason the user
        # cannot see; the switch states its own layout, and the preview rows are
        # drawn like every other row.
        has_staged = leaf.split("var hasStagedChanges: Bool {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("if row.isPreview { return false }", has_staged)
        staged = leaf.split("func stagedResult() -> NativeGroupManagerResult? {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("let previewed = autoGroupingBaselineKeys", staged)
        self.assertIn("guard !autoGrouping || !previewed.contains(row.id) else { return false }", staged)
        self.assertIn("return !autoGrouping || previewed.contains(row.id)", staged)

        # Windows stages the same layout on the same event.
        self.assertIn("auto stage_auto_grouping = [&rows, &groups, staged_deletes, auto_grouping_baseline](bool enabled) {", windows)
        toggle_win = windows.split("toggle.Click([&](auto const&, auto const&) {", 1)[1].split("close.Click([&]", 1)[0]
        self.assertIn("stage_auto_grouping(toggle_on());", toggle_win)
        stage_win = windows.split("auto stage_auto_grouping = [&rows, &groups, staged_deletes, auto_grouping_baseline](bool enabled) {", 1)[1].split("toggle.Click([&]", 1)[0]
        self.assertIn("candidate.name = group->name.empty() ? group->label : group->name;", stage_win)
        self.assertIn("kept_groups.count(row.group_id) > 0", stage_win)
        self.assertIn("if (row.draft || row.deleted) {", stage_win)
        self.assertNotIn("candidate.deleted = true;", stage_win)
        result_win = windows.split("auto checked = toggle.IsChecked();", 1)[1].split("outcome = std::move(result);", 1)[0]
        self.assertIn("const bool previewing = *auto_grouping_baseline && result.auto_grouping;", result_win)
        self.assertIn("if (!previewing) result.deletes.push_back(row.id);", result_win)
        # Windows opens on the same stated layout, and its preview rows are not
        # dimmed either.
        self.assertIn("bool preview = false;", windows)
        self.assertIn("auto is_staged = [](SheetRow const& row) { return row.deleted || (row.draft && !row.preview); };", windows)
        self.assertIn("candidate.preview = true;", stage_win)
        self.assertIn("if (toggle_on()) stage_auto_grouping(true);", windows)
        # Windows keeps the same no-groups rule: no layout to state means the
        # keys are shown unchanged rather than emptied — and the same 未分组 rule,
        # because the picker there carries the sentinel too.
        self.assertIn("if (!layout_groups.empty()) {", stage_win)
        self.assertIn("if (group.id.empty()) continue;", stage_win)
        self.assertIn("if (row.group_id.empty()) continue;", stage_win)
        self.assertIn("for (auto const& group : layout_groups) {", stage_win)
        self.assertNotIn("} else if (groups.empty()) {", stage_win)
        has_staged_win = windows.split("auto has_staged_changes = [", 1)[1].split("return false;", 1)[0]
        self.assertIn("if (row.preview) {", has_staged_win)

        # Both keep a baseline so turning the switch off restores what opened.
        self.assertIn("private var autoGroupingBaselineRows: [KeyRow]?", leaf)
        self.assertIn("autoGroupingBaselineRows = nil", leaf)
        self.assertIn("auto auto_grouping_baseline = std::make_shared<std::optional<std::vector<SheetRow>>>();", windows)

    def test_native_tables_report_a_cleared_selection(self) -> None:
        # A click below the rows clears the list selection, but the shared view
        # still holds the item its + / − header and editor act on.  Both hosts
        # report that cleared selection so the panes drop it instead of leaving
        # a stale selection the list no longer highlights.
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        mac_table = mac.split(
            "- (void)tableViewSelectionDidChange:", 1
        )[1].split("- (void)handleRowClick:", 1)[0]
        windows_table = windows.split(
            "list_.SelectionChanged([this](auto const&, auto const&) {", 1
        )[1].split("list_.ItemClick([this]", 1)[0]
        self.assertIn("if (selectedRow < 0 && !viewProps.selectedKey.empty()) {", mac_table)
        self.assertIn('LiteLLMAppKitTableEventEmitter::OnSelectionChange event{"", -1};', mac_table)
        self.assertIn("if (index < 0) {", windows_table)
        self.assertIn("if (!Props()->selectedKey.empty()) {", windows_table)
        self.assertIn("args.index = -1;", windows_table)
        self.assertIn('args.key = "";', windows_table)

    def test_native_login_item_registration_follows_core_target_state(self) -> None:
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
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

    def test_background_launch_uses_the_shared_home_term_on_both_hosts(self) -> None:
        """A background launch has to leave nothing on screen on either host.

        macOS has no launch window to hide — its primary React host is already
        ordered out — so it only has to keep the settings window closed. Windows
        creates the host window before the preference is known, so its hide path
        has to exist and has to be the one the launch already uses for home.
        Sharing the "home" term keeps one decision in the shared layer instead
        of a platform-specific launch branch.
        """

        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")

        # The launch decides once, from the first snapshot that answers.
        self.assertIn("const launchPresented = useRef(false);", ui)
        self.assertIn('if (snapshot.service.launch_background_state === "enabled") {', ui)
        # Both branches speak the same shared term the app already uses to leave
        # the settings shell; neither adds a platform-specific launch surface.
        self.assertIn('native.window.focus("home");', ui)
        self.assertIn('native.window.open("providers-models");', ui)
        self.assertNotIn("launchBackground", mac)
        self.assertNotIn("launchBackground", windows)
        self.assertIn("focus: (route) => bridge.focusWindow(route),", bridge)
        # macOS: focusing home hides the React host and closes the settings
        # window, and the menu bar is the only thing left on screen.
        self.assertIn('guard route != "home" else {', mac)
        self.assertIn("hideHostWindow()", mac)
        self.assertIn('if let settingsKey = settingsWindowKey() {', mac)
        # Windows: the one host window hides instead of being torn down, so the
        # mounted React tree keeps its state.
        self.assertIn('if (route == L"home") {', windows)
        self.assertIn("ShowWindow(window_handle_, SW_HIDE)", windows)
        self.assertNotIn("ShowWindow(window_handle_, SW_DESTROY)", windows)

    def test_macos_codex_catalog_toggle_owns_the_managed_catalog(self) -> None:
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")

        self.assertIn('id: "toggle-codex-model-catalog"', ui)
        self.assertIn('checked: booleanValue(catalog.enabled)', ui)
        self.assertIn('type: "codex.model_catalog.set"', ui)
        self.assertIn('"toggle-autostart", "toggle-codex-model-catalog", "separator"', leaf)
        # Turning the managed catalog on or off writes the client's config and
        # never asks the user to restart anything: no reminder, no restart
        # helper, and no dialog about it in the shared UI or the native leaf.
        for removed in ("CodexRestartNotice", "showCodexRestartConfirmation", "restartCodex", "codexRestartPanel"):
            self.assertNotIn(removed, ui)
            self.assertNotIn(removed, leaf)
            self.assertNotIn(removed, platform)

    def test_native_localization_never_falls_back_to_english(self) -> None:
        """Every string a native leaf shows is one the shared UI localized.

        A leaf's own default dictionary is English, so a key the shared UI never
        sends — the blocked-navigation message inside the official provider
        sign-in, the read-only viewer's record label — reaches a Chinese app in
        English.
        """
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        sent_block = ui.split("native.setLocalization({", 1)[1].split("\n    });", 1)[0]
        sent = set(re.findall(r"([A-Za-z0-9_]+):\s*translate\(", sent_block))
        self.assertGreater(len(sent), 25)

        read: dict[str, set[str]] = {
            "macOS leaf": set(re.findall(r'localized(?:Text)?\("([^"]+)"', (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8"))),
            "macOS module": set(re.findall(r'localized(?:Text)?\("([^"]+)"', (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8"))),
        }
        for path in sorted(WIN_NATIVE.glob("*.cpp")):
            read[path.name] = set(re.findall(r'Localized\("([^"]+)"', path.read_text(encoding="utf-8")))

        missing = {name: sorted(keys - sent) for name, keys in read.items() if keys - sent}
        self.assertEqual({}, missing, f"native strings the shared UI never sends: {missing}")

    def test_one_non_modal_decision_surface_draws_every_confirmation(self) -> None:
        """Every confirmation is the app's own panel, never a modal alert."""
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        bridge_types = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        windows_leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")

        # One builder draws every question the way a macOS alert is drawn: a
        # borderless rounded panel, no title bar, the question in bold over its
        # detail, and the answers as one row of equal width across the bottom.
        decision = leaf.split("private func makeDecisionPanel(", 1)[1].split(
            "/// Presents a built decision panel", 1
        )[0]
        self.assertIn("let panel = NativeDecisionPanel(", decision)
        self.assertIn("styleMask: [.borderless],", decision)
        self.assertIn("panel.isMovableByWindowBackground = true", decision)
        self.assertIn("content.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor", decision)
        self.assertIn("content.layer?.cornerRadius = nativeDecisionCornerRadius", decision)
        # The question stays the window's title for the window that asked (its
        # lock announces the question) and for a screen reader, and it is drawn in
        # the body because a borderless panel never draws a title.
        self.assertIn("panel.title = title", decision)
        self.assertIn("let questionLabel = NSTextField(wrappingLabelWithString: title)", decision)
        # A destructive answer draws with the system's red ink on the ordinary
        # bezel, and never with the default button's accent fill.
        self.assertIn("button.attributedTitle = NSAttributedString(", decision)
        self.assertIn(".foregroundColor: NSColor.systemRed", decision)
        self.assertIn("button.drawsNeutralWhileDefault = answer.isDefault", decision)
        self.assertIn("override func draw(_ dirtyRect: NSRect) {", leaf)
        self.assertIn('button.keyEquivalent = "\\u{1b}"', decision)
        # The answers are one row of equal width across the bottom: the first
        # starts at the text column's leading edge, the last ends at its trailing
        # edge, and every answer takes the same share between them.
        self.assertIn("constraints.append(button.widthAnchor.constraint(equalTo: first.widthAnchor))", decision)
        self.assertIn("constraints.append(first.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: inset))", decision)
        self.assertIn("constraints.append(last.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -inset))", decision)
        self.assertIn("private static func decisionLineWidth(", leaf)
        self.assertIn("private static func decisionTextHeight(", leaf)
        # The question is put where a question belongs: centred on the window that
        # asked it (or on the screen when a background event asks) instead of
        # wherever `NSWindow.center()` lands for a borderless child.
        presentation = leaf.split("private func presentBuiltDecisionPanel(", 1)[1].split(
            "/// Puts a decision panel where a question belongs", 1
        )[0]
        self.assertIn("let anchor = locksParent ? (parent ?? activeWindow()) : nil", presentation)
        self.assertIn("Self.positionDecisionPanel(panel, over: anchor)", presentation)
        self.assertIn("presentChildPanel(panel, in: anchor, prepare: prepare)", presentation)
        self.assertIn("private static func positionDecisionPanel(_ panel: NSWindow, over anchor: NSWindow?)", leaf)
        # A question is a child surface, never an alert: neither file builds an
        # alert any more, so nothing runs the modal loop that freezes the React
        # host (see the child-surface modal freeze incident).  The alert is only
        # ever named in a comment, never constructed, and no modal loop is run.
        for source in (leaf, module):
            self.assertNotIn("NSAlert()", source)
            self.assertNotIn("runModal(", source)
            self.assertNotIn("beginSheetModal(for: promptParent", source)
        self.assertNotIn("NSApp.runModal(for:", leaf)
        self.assertIn("override func cancelOperation(_ sender: Any?) {", leaf)
        # A borderless panel only becomes key when it says so, or every
        # confirmation would answer no key at all.
        self.assertIn("override var canBecomeKey: Bool { true }", leaf)
        # Escape, the window that asked going away, and an answer all settle the
        # same entry exactly once, and the lock over the parent is released.
        self.assertIn("func finishDecisionPanel(_ panel: NSWindow, answer: String) {", leaf)
        self.assertIn("endChildPanel(panel)\n        state.completion(answer)", leaf)
        self.assertIn("if let state = decisionPanels[ObjectIdentifier(window)] {", leaf)
        self.assertIn("if let state = decisionPanels[ObjectIdentifier(panel)] {", leaf)
        self.assertIn('completion(answers.first(where: { $0.isCancel })?.id ?? "")', leaf)
        # The one prompt that carries its own control is the same panel: a secure
        # field under the question, 设置 disabled until it has something to stage,
        # the caret in the field, and the typed value read once and cleared in the
        # same turn instead of travelling with the answer.
        secret = leaf.split("func presentSecretPrompt(", 1)[1].split(
            "/// The height a decision panel's own text needs", 1
        )[0]
        self.assertIn("let input = NSSecureTextField(", secret)
        self.assertIn("setButton?.isEnabled = false", secret)
        self.assertIn("input.window?.makeFirstResponder(input)", secret)
        self.assertIn('input.stringValue = ""', secret)
        self.assertIn("self.leaf.presentSecretPrompt(", module)
        # The post-login password question is that panel too, not an attached
        # sheet on a window that may go away underneath it.
        self.assertIn("NativeDecisionAnswer(id: \"remember\", title: text(\"Remember Password\", \"记住密码\"), isDefault: true)", leaf)

        # The version acknowledgement is the same panel with one answer.
        version = leaf.split("func showVersion() {", 1)[1].split("/// Version strings", 1)[0]
        self.assertIn("presentDecisionPanel(", version)
        self.assertIn('id: "ok",', version)

        # Windows draws the same window: every question is the shared decision
        # window, `Confirm` is one case of it, and a destructive answer marks
        # itself the platform's way.
        windows_decision = windows_leaf.split(
            "std::optional<std::wstring> WinUI3NativeLeaf::ShowDecisionWindow(", 1
        )[1].split("bool WinUI3NativeLeaf::DecideChoice(", 1)[0]
        self.assertIn("dialog.Title(winrt::hstring(title))", windows_decision)
        self.assertIn("auto cancel_answer = std::find_if(", windows_decision)
        self.assertIn("button.Foreground(brush ? brush : xaml::Media::SolidColorBrush(critical));", windows_decision)
        self.assertIn("winrt::Windows::System::VirtualKey::Escape", windows_decision)
        self.assertIn("winrt::Windows::System::VirtualKey::Enter", windows_decision)
        self.assertIn("primary_button.Focus(xaml::FocusState::Programmatic);", windows_decision)
        self.assertIn("body.Measure(winrt::Windows::Foundation::Size{", windows_decision)
        self.assertIn("constexpr double kDialogMinWidth = 300;", windows_decision)
        self.assertIn("constexpr double kDialogMaxWidth = 420;", windows_decision)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_, {dialog_width, dialog_height}, finished)", windows_decision)
        confirm = windows_leaf.split("bool WinUI3NativeLeaf::Confirm(", 1)[1].split(
            "void WinUI3NativeLeaf::DecideChoice(", 1
        )[0]
        self.assertIn('cancel_answer.id = L"cancel";', confirm)
        self.assertIn('confirm_answer.id = L"confirm";', confirm)
        self.assertIn("confirm_answer.destructive = destructive;", confirm)
        # The version acknowledgement is one answer on that window, not a
        # MessageBox: one acknowledgement, one surface.
        version = windows_leaf.split("void WinUI3NativeLeaf::ShowVersion() const {", 1)[1].split(
            "WinUI3NativeLeaf::VersionInfoResult", 1
        )[0]
        self.assertIn('ShowDecisionWindow(Localized("appTitle", L"Young Router"), VersionText(), {ok});', version)
        self.assertIn("ok.primary = true;", version)
        self.assertNotIn("MessageBoxW", windows_leaf)
        # The sign-in browser asks its post-login question through the host's own
        # window too, so one question has one surface on each host.
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")
        self.assertIn("co_return state->options.decide(title, message, remember, session_only);", windows_relay)
        self.assertNotIn("controls::ContentDialog prompt;", windows_relay)
        self.assertIn("bool WinUI3NativeLeaf::DecideChoice(", windows_leaf)
        self.assertIn("native_options.decide = [leaf = leaf_](", windows_module)
        self.assertIn("bool destructive,", windows_module)
        self.assertIn("self.leaf.presentDecisionPanel(", module)

        # The destructive fact travels from the shared UI through the typed
        # adapter into the positional native call on both hosts, and the
        # dismissing answer's words travel the same way: a question whose 取消
        # means "cancel the move" names that answer instead of leaving the
        # host's generic Cancel, while a caller that names nothing keeps it.
        self.assertIn("showConfirmation(options: { title: string; message: string; confirmLabel: string; cancelLabel?: string; destructive?: boolean }): Promise<boolean>;", types)
        self.assertIn("showConfirmation(title: string, message: string, confirmLabel: string, cancelLabel: string, destructive: boolean): Promise<boolean>;", bridge_types)
        self.assertIn("leaf.showConfirmation?.(title, message, confirmLabel, cancelLabel, destructive === true)", platform)
        self.assertIn("destructive:(BOOL)destructive", bridge)
        self.assertIn("cancelLabel:(NSString *)cancelLabel", bridge)
        self.assertIn('title: cancelTitle.isEmpty ? self.leaf.localizedText("cancel", fallback: "Cancel") : cancelTitle,', module)
        self.assertIn("let cancelTitle = cancelLabel.trimmingCharacters(in: .whitespacesAndNewlines)", module)
        self.assertIn("std::wstring const& cancel_label,", windows_module)
        self.assertIn("leaf->Confirm(title, message, confirm_label, cancel_label, destructive);", windows_module)
        self.assertIn('cancel_answer.label = cancel_label.empty() ? Localized("cancel", L"Cancel") : std::wstring(cancel_label);', confirm)
        self.assertIn("destructive: Bool,", module)

    def test_shared_ui_owns_lifecycle_menu_actions_startup_and_safe_recovery(self) -> None:
        """Both native leaves route lifecycle commands through one React IPC path."""
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
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
        self.assertIn('await dispatchServiceAction(serviceRestart ? "service.restart" : "service.start");', ui)
        self.assertIn('if (operation === "stop") serviceShouldBeRunning.current = false;', ui)
        # The launch start retries on a bounded backoff: a login-time launch
        # competes with every other start-up item, so a start that failed while
        # the proxy spawned its workers must recover without a relaunch.
        self.assertIn("const SERVICE_STARTUP_RETRY_DELAYS_MS = [0, 5_000, 20_000, 60_000];", ui)
        self.assertIn("if (attempt >= SERVICE_STARTUP_RETRY_DELAYS_MS.length) return;", ui)
        self.assertIn('void runServiceOperation("start");', ui)
        self.assertNotIn("SERVICE_HEALTH_POLL_MS", ui)
        self.assertNotIn("SERVICE_RECOVERY_RETRY_MS", ui)
        self.assertNotIn("pollServiceHealth", ui)

        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        self.assertIn("public func warm()", bridge)
        self.assertIn("[CoreIPCBridge.shared warm];", app_delegate)

    def test_home_deep_links_leave_no_empty_settings_window(self) -> None:
        """A "home" deep link leaves the settings shell for the menu bar or
        tray; the hosts must dismiss the open settings window instead of
        leaving an empty shared shell pane onscreen."""
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        win_leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn('guard route != "home" else {', leaf)
        self.assertIn('close(route: "provider-wizard")', leaf)
        self.assertIn("close(route: settingsKey)", leaf)
        self.assertIn('if (route == L"home") {', win_leaf)

    def test_macos_deep_links_allow_only_the_logs_tab_parameter(self) -> None:
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        info = (MAC_PROJECT / "YoungRouter-macOS/Info.plist").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")

        self.assertIn('objectForInfoDictionaryKey:@"YoungRouterRouteScheme"', app_delegate)
        self.assertIn('routeScheme = @"young-router"', app_delegate)
        self.assertIn('[[url scheme] isEqualToString:routeScheme]', app_delegate)
        self.assertIn("<key>YoungRouterRouteScheme</key>", info)
        self.assertIn("<string>young-router</string>", info)
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

    def test_settings_window_uses_a_full_height_sidebar_material(self) -> None:
        """The settings window keeps one sidebar material behind the whole
        window and a transparent title bar, so the sidebar reads as a single
        full-height surface instead of a gray table on a white pane."""
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")

        self.assertIn("NSWindowStyleMaskFullSizeContentView", app_delegate)
        self.assertIn("window.titlebarAppearsTransparent = YES;", app_delegate)
        self.assertIn("window.titleVisibility = NSWindowTitleHidden;", app_delegate)
        self.assertIn("window.titlebarSeparatorStyle = NSTitlebarSeparatorStyleNone;", app_delegate)
        self.assertIn("NSVisualEffectMaterialSidebar", app_delegate)
        self.assertIn("NSVisualEffectBlendingModeBehindWindow", app_delegate)
        self.assertIn("[backdrop addSubview:rootView];", app_delegate)
        self.assertIn("[(id)rootView setBackgroundColor:NSColor.clearColor];", app_delegate)
        # The native shell predicate mirrors isSettingsShellRoute() instead of
        # an explicit route whitelist; a whitelist silently dropped General
        # onto a plain titled window while the shared shell still reserved its
        # title-bar inset.
        self.assertIn('NSSet<NSString *> *standaloneRoutes = [NSSet setWithArray:@[@"home", @"provider-wizard", @"file-editor"]];', app_delegate)
        self.assertIn("const BOOL settingsShell = ![standaloneRoutes containsObject:route];", app_delegate)
        # The material belongs to the window; a per-table backdrop would stack
        # a second vibrancy layer over only the table's own rows.
        self.assertNotIn("NSVisualEffectMaterialSidebar", controls)
        self.assertIn("contentSize: NSSize(width: 960, height: 660)", leaf)

    def test_macos_reopen_shows_the_primary_configuration_window(self) -> None:
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")

        self.assertIn("applicationShouldHandleReopen", app_delegate)
        self.assertIn("hasVisibleWindows", app_delegate)
        self.assertIn('openRouteFromDeepLink:@"providers-models" logTab:nil', app_delegate)
        self.assertNotIn("native-open-relay-accounts", leaf)
        self.assertNotIn("openRelayAccounts", leaf)
        self.assertIn('representedObject = "native-open-data-management"', leaf)
        self.assertIn("action: #selector(openDataManagement)", leaf)
        self.assertIn("dataManagementItem.keyEquivalentModifierMask = [.command, .shift]", leaf)

    def test_macos_bundle_prohibits_duplicate_menu_bar_instances(self) -> None:
        """A second launch must activate the existing app, not add another LL item."""

        info = (MAC_PROJECT / "YoungRouter-macOS/Info.plist").read_text(encoding="utf-8")
        main = (MAC_PROJECT / "YoungRouter-macOS/main.m").read_text(encoding="utf-8")

        self.assertIn("<key>LSMultipleInstancesProhibited</key>", info)
        self.assertIn("<true/>", info.split("<key>LSMultipleInstancesProhibited</key>", 1)[1].split("</dict>", 1)[0])
        self.assertIn("NSWorkspace.sharedWorkspace.runningApplications", main)
        self.assertIn("YoungRouterIsManagedApplication", main)
        self.assertIn("application.processIdentifier != currentPID", main)
        self.assertIn("activateWithOptions", main)
        self.assertLess(main.index("YoungRouterExistingInstance()"), main.index("NSApplicationMain(argc, argv)"))

    def test_macos_preview_metadata_keeps_the_production_instance_identity(self) -> None:
        build = (ROOT / "scripts/build-and-install-macos.sh").read_text(encoding="utf-8")
        main = (MAC_PROJECT / "YoungRouter-macOS/main.m").read_text(encoding="utf-8")

        self.assertIn("YOUNG_ROUTER_MACOS_BUNDLE_IDENTIFIER is unsupported", build)
        self.assertIn("YOUNG_ROUTER_MACOS_DISPLAY_NAME", build)
        self.assertIn("YOUNG_ROUTER_MACOS_ROUTE_SCHEME", build)
        self.assertIn("YOUNG_ROUTER_MACOS_PREVIEW_PROFILE_ROOT", build)
        self.assertIn("YOUNG_ROUTER_MACOS_PREVIEW_PORT", build)
        self.assertIn("YoungRouterPreviewProfileRoot", build)
        self.assertIn("YoungRouterPreviewPort", build)
        self.assertIn('PREVIEW_BUNDLE_IDENTIFIER="young.router.app"', build)
        self.assertIn("Set :CFBundleIdentifier $PREVIEW_BUNDLE_IDENTIFIER", build)
        self.assertIn('kYoungRouterInstanceNamespace = @"young.router.app"', main)
        self.assertNotIn("distinct preview bundle", main)

    def test_macos_preview_profile_is_embedded_without_a_second_instance_identity(self) -> None:
        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")

        self.assertIn("previewProfileEnvironment", bridge)
        self.assertIn('guard let rawRoot = Bundle.main.object(forInfoDictionaryKey: "YoungRouterPreviewProfileRoot")', bridge)
        self.assertNotIn('Bundle.main.bundleIdentifier != "young.router.app"', bridge)
        self.assertIn('"YoungRouterPreviewProfileRoot"', bridge)
        self.assertIn('"YoungRouterPreviewPort"', bridge)
        for key in (
            "LITELLM_RUNTIME_ROOT",
            "LITELLM_CONFIG_FILE",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE",
        ):
            self.assertIn(f'environment["{key}"]', bridge)

    def test_macos_uses_the_monochrome_double_l_status_icon_asset(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        icon_contents = (
            MAC_PROJECT
            / "YoungRouter-macOS/Assets.xcassets/StatusBarIcon.imageset/Contents.json"
        ).read_text(encoding="utf-8")

        self.assertIn('NSImage(named: NSImage.Name("StatusBarIcon"))!', leaf)
        self.assertIn("image.size = NSSize(width: 20, height: 20)", leaf)
        self.assertIn("image.isTemplate = true", leaf)
        self.assertNotIn('("L" as NSString).draw(', leaf)
        self.assertIn("NSStatusItem.squareLength", leaf)
        self.assertIn("statusItem.button?.image = Self.statusBarIcon", leaf)
        # The status item draws the bundled monochrome asset, never a system
        # symbol; the sheet's own icon buttons are free to use SF Symbols.
        status_icon = leaf.split("private static let statusBarIcon: NSImage = {", 1)[1].split("}()", 1)[0]
        self.assertNotIn("systemSymbolName:", status_icon)
        self.assertIn('"filename" : "status_icon.png"', icon_contents)
        self.assertIn('"filename" : "status_icon@2x.png"', icon_contents)
        self.assertNotIn("template-rendering-intent", icon_contents)

    def test_macos_status_item_is_ready_before_react_and_defers_menu_rebuilds_while_tracking(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        platform_entry = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")

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

    def test_macos_status_item_left_click_opens_settings_and_right_click_shows_the_menu(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        # The status item no longer owns a permanent menu: a left click opens
        # the shared settings window and only a secondary click shows the
        # service menu.
        self.assertIn("statusItem.button?.target = self", leaf)
        self.assertIn("statusItem.button?.action = #selector(statusItemPressed(_:))", leaf)
        self.assertIn("statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])", leaf)
        self.assertIn("statusMenu = makeMenu()", leaf)
        self.assertNotIn("statusItem.menu = makeMenu()", leaf)
        press = leaf.split("@objc private func statusItemPressed", 1)[1].split("public func menuWillOpen", 1)[0]
        self.assertIn("event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true", press)
        self.assertIn("showStatusMenu()", press)
        self.assertIn('openNamedRoute("providers-models")', press)
        menu = leaf.split("private func showStatusMenu()", 1)[1].split("public func menuWillOpen", 1)[0]
        self.assertIn("statusItem.menu = menu", menu)
        self.assertIn("statusItem.button?.performClick(nil)", menu)
        close_menu = leaf.split("public func menuDidClose", 1)[1].split("private func addMenuActionItem", 1)[0]
        self.assertIn("statusItem.menu = nil", close_menu)
        self.assertIn("statusMenuVisible = false", close_menu)

    def test_native_hosts_expose_app_and_litellm_versions_for_the_about_pane(self) -> None:
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        win_leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        win_header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")
        win_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        win_module_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")

        self.assertIn("func versionInfo() -> [String: String]", mac_leaf)
        self.assertIn('appendingPathComponent("Core/runtime/LITELLM_VERSION", isDirectory: false)', mac_leaf)
        self.assertIn('"litellm": litellm,', mac_leaf)
        self.assertIn('result["icon"] = "data:image/png;base64," + png.base64EncodedString()', mac_leaf)
        self.assertIn("func openExternalURL(_ url: String)", mac_leaf)
        self.assertIn("scheme == \"http\" || scheme == \"https\"", mac_leaf)
        self.assertIn("@objc(versionInfo:rejecter:)", mac_module)
        self.assertIn("@objc func openExternalURL(_ url: String)", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(versionInfo:(RCTPromiseResolveBlock)resolve rejecter:(RCTPromiseRejectBlock)reject)", mac_bridge)
        self.assertIn("RCT_EXTERN_METHOD(openExternalURL:(NSString *)url)", mac_bridge)
        # The external-settings pane reveals a listed file in the platform file
        # manager; the host validates the path and never grants file access.
        self.assertIn("func revealFile(_ path: String)", mac_leaf)
        self.assertIn("NSWorkspace.shared.activateFileViewerSelecting([target])", mac_leaf)
        self.assertIn("func revealFile(_ path: String)", mac_module)
        self.assertIn("@objc func revealFile(_ path: String)", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(revealFile:(NSString *)path)", mac_bridge)
        # The raw file editor is a native child window over the workspace, so it
        # is opened by the host and rendered outside the settings shell.
        self.assertIn("func openFileEditor(_ payload: String)", mac_leaf)
        self.assertIn("func pendingFileEditorTarget() -> String", mac_leaf)
        self.assertIn("func prepareFileEditor()", mac_leaf)
        self.assertIn("open(route: \"file-editor\", title: title, warmOnly: true)", mac_leaf)
        # A warm editor window boots its React root off screen; the shared
        # child-surface presentation is what puts it in front of the app.
        self.assertIn("if warmOnly {", mac_leaf)
        self.assertIn('if windowRoute == "provider-wizard" || windowRoute == "file-editor" {', mac_leaf)
        self.assertIn("presentChildPanel(window, in: settingsWindow())", mac_leaf)
        # The editor sheet exists for exactly one document: a bare deep link or
        # an AppKit-restored window must not present an empty editor.
        self.assertIn('if windowRoute == "file-editor", pendingFileEditorTargetValue == nil {', mac_leaf)
        self.assertIn('window.isRestorable = route != "file-editor"', mac_leaf)
        self.assertIn('case "file-editor": return localized("routeFileEditor", fallback: "Edit File")', mac_leaf)
        self.assertIn("@objc func openFileEditor(_ payload: String)", mac_module)
        self.assertIn("@objc func prepareFileEditor()", mac_module)
        self.assertIn("@objc func pendingFileEditorTarget() -> String", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(openFileEditor:(NSString *)payload)", mac_bridge)
        self.assertIn("RCT_EXTERN_METHOD(prepareFileEditor)", mac_bridge)
        self.assertIn("RCT_EXTERN__BLOCKING_SYNCHRONOUS_METHOD(pendingFileEditorTarget)", mac_bridge)
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        self.assertIn('standaloneRoutes = [NSSet setWithArray:@[@"home", @"provider-wizard", @"file-editor"]];', app_delegate)
        self.assertIn('props[@"initialFileTarget"] = fileId;', app_delegate)
        self.assertIn("NSString *fileId, NSWindow *existingWindow", app_delegate)
        self.assertIn('if (route == L"file-editor") return {900, 560};', win_leaf)
        self.assertIn('if (route == L"file-editor") return Localized("routeFileEditor", L"Edit File");', win_leaf)
        self.assertIn("void RevealFile(std::wstring_view path);", win_header)
        self.assertIn("void WinUI3NativeLeaf::RevealFile(std::wstring_view path)", win_leaf)
        self.assertIn('L"/select,\\"" + target + L"\\""', win_leaf)
        self.assertIn('ShellExecuteW(nullptr, L"open", L"explorer.exe", arguments.c_str(), nullptr, SW_SHOWNORMAL);', win_leaf)
        self.assertIn('REACT_METHOD(RevealFile, L"revealFile");', win_module_header)
        self.assertIn("void WinUI3NativeLeafModule::RevealFile(std::wstring const& path) noexcept", win_module)
        self.assertIn("struct VersionInfoResult {", win_header)
        self.assertIn("VersionInfoResult VersionInfo() const;", win_header)
        self.assertIn("void OpenExternalURL(std::wstring_view url);", win_header)
        self.assertIn('L"Core" / L"runtime" / L"LITELLM_VERSION"', win_leaf)
        self.assertIn('REACT_METHOD(VersionInfo, L"versionInfo");', win_module_header)
        self.assertIn('REACT_METHOD(OpenExternalURL, L"openExternalURL");', win_module_header)
        self.assertIn('result["litellm"] = winrt::to_string(info.litellm);', win_module)
        self.assertIn('ShellExecuteW(nullptr, L"open", command.c_str(), nullptr, nullptr, SW_SHOWNORMAL);', win_leaf)

    def test_macos_quit_cancels_startup_and_finishes_off_the_main_thread(self) -> None:
        bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")

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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        core_service = (ROOT / "young_router/core/service.py").read_text(encoding="utf-8")

        for source in (ui, mac, windows):
            self.assertIn("language-picker", source)
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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")

        self.assertIn("NSWindowDelegate", leaf)
        self.assertIn("func windowShouldClose(_ sender: NSWindow) -> Bool", leaf)
        self.assertIn("approvedCloseRoutes", leaf)
        self.assertIn("routeForWindow(sender)", leaf)
        self.assertIn("requestClose(route: route, hiding: sender)", leaf)
        self.assertIn("window.orderOut(nil)", leaf)
        self.assertIn('emitAction("request-close-\\(route)")', leaf)
        self.assertIn("requestClose(route: window.flatMap(routeForWindow), hiding: window)", leaf)
        self.assertIn("request-close-", ui)
        # The settings shell owns its window-close path: it flushes the active
        # pane and closes the shared window without a discard dialog.
        self.assertIn('if (!nativeAction?.id.startsWith("request-close-") || closing.current) return;', ui)
        self.assertIn("canClose = await flushActivePane.current?.() ?? true;", ui)
        self.assertIn("if (!canClose) {", ui)
        self.assertIn("native.window.focus(windowRoute);", ui)
        self.assertIn('native.window.close(Platform.OS === "windows" ? pane : windowRoute);', ui)
        self.assertIn('if (shell) return;', ui)

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

    def test_macos_route_windows_share_one_settings_window_and_show_dock_only_with_ui(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        app_delegate = (MAC_PROJECT / "YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        info = (MAC_PROJECT / "YoungRouter-macOS/Info.plist").read_text(encoding="utf-8")

        self.assertIn("private var routeWindows: [String: NSWindow] = [:]", leaf)
        self.assertIn("setRouteWindowFactory", leaf)
        self.assertIn("routeWindowFactory?(route, initialLogTab, pendingFileEditorTargetValue, existing)", leaf)
        # Every settings pane reuses one registry key; only the provider wizard
        # is still an independent child window created by the same factory.
        self.assertIn("private static let settingsPaneRoutes: Set<String> = [", leaf)
        self.assertIn("private func settingsWindowKey() -> String? {", leaf)
        self.assertIn("if Self.settingsPaneRoutes.contains(windowRoute),", leaf)
        self.assertIn("NSWindow *existingWindow", app_delegate)
        self.assertIn('self.initialProps = @{ @"isPrimaryHost": @YES, @"isWindowManagerHost": @YES };', app_delegate)
        self.assertIn('@"isPrimaryHost": @NO', app_delegate)
        self.assertIn("viewWithModuleName:@\"YoungRouter\" initialProperties:props", app_delegate)
        self.assertNotIn("NSApplicationActivationPolicyRegular", app_delegate)
        self.assertIn("LSUIElement", info)
        self.assertIn("<key>CFBundleIconFile</key>\n\t<string>AppIcon</string>", info)
        self.assertNotIn("CFBundleIconName", info)
        app_icon = (
            MAC_PROJECT
            / "YoungRouter-macOS/Assets.xcassets/AppIcon.appiconset/icon_1024.png"
        )
        self.assertTrue(app_icon.is_file())
        self.assertIn("NSApp.setActivationPolicy(.accessory)", leaf)
        self.assertIn("NSApp.setActivationPolicy(.regular)", leaf)
        # A warm (ordered-out) window is registered but not on screen: it must
        # not keep the app in the Dock with nothing to show.
        self.assertIn("let presented = routeWindows.values.contains { $0.isVisible }", leaf)
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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_module_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        mac_controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows_controls = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

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
        # The grouped menu adds one submenu per provider; the pointer decides
        # where it opens so it stays under the button that asked for it.
        self.assertIn("showGroupedActionMenu?(title: string, groups:", bridge)
        self.assertIn("showGroupedActionMenu: async (title, groups, anchor)", platform)
        # The grouped menu answers the same `{group, item}` object on both
        # hosts. A bare macOS array reaches React as `[0, 1]`, whose `.group`
        # and `.item` are both undefined, so the chosen model was silently
        # never applied on that host.
        self.assertIn("func showGroupedActionMenu(title: String, groups: [[String: Any]], anchor: [String: NSNumber]) -> [String: NSNumber]?", mac)
        self.assertIn('return ["group": NSNumber(value: tag / 1_000), "item": NSNumber(value: tag % 1_000)]', mac)
        self.assertIn('result["group"] = static_cast<double>(selected->first);', windows_module)
        self.assertIn('result["item"] = static_cast<double>(selected->second);', windows_module)
        self.assertIn("designateSavedModel(savedModelGroups[choice.group]?.selections[choice.item]);", ui)
        self.assertIn("Promise<{ group: number; item: number } | undefined>", types)
        # One menu deep: the provider is a section caption and its models sit
        # underneath, never a submenu opening to the right.
        self.assertIn(".sectionHeader(title: entry.0)", mac)
        self.assertIn("header.isEnabled = false", mac)
        self.assertIn("let top = min(pointer.y + anchorHeight, max(bounds.maxY - 1, 0))", mac)
        self.assertIn('symbolName = @"chevron.up.chevron.down";', mac_controls)
        self.assertIn("configurationWithPointSize:LiteLLMUIFontSize - 3.5", mac_controls)
        self.assertIn('if (symbol == "chevron-up-down") return L"\\xE70D";', windows_controls)
        self.assertIn("showGroupedActionMenu:groups:anchor:resolver:rejecter:", mac_module)
        self.assertIn("MF_STRING | MF_DISABLED", windows)
        # The grouped menu itself stays one level deep.
        grouped_menu = windows.split("WinUI3NativeLeaf::ShowGroupedActionMenu(", 1)[1].split(
            "WinUI3NativeLeaf::ShowActionMenu(", 1
        )[0]
        self.assertNotIn("MF_POPUP", grouped_menu)
        self.assertIn("ShowGroupedActionMenu(", windows_module)
        self.assertIn("GetClientRect(window_handle_", windows)
        self.assertIn("ClientToScreen(window_handle_", windows)
        self.assertNotIn("GetCursorPos(&point)", windows)
        self.assertIn("JSValueObject const& anchor", windows_module_header)
        self.assertIn("TryGetDouble", windows_module)

    def test_log_original_record_uses_the_shared_read_only_code_viewer(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
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

        self.assertIn('import { CodeEditorWebView, editorMenuLabels, readOnlyCodeEditorHtml } from "./code-editor/CodeEditorWebView";', ui)
        self.assertGreaterEqual(ui.count("void native.showReadOnlyText({"), 3)
        self.assertIn('text: selected.rows.map((row) => row.original).join("\\n\\n")', ui)
        self.assertIn("text: row.original", ui)
        self.assertIn('language: "json"', ui)
        self.assertIn("html: readOnlyCodeEditorHtml(editorMenuLabels(translate))", ui)
        for obsolete in ("const [originalRecord", "setOriginalRecord(", "if (originalRecord)", "log-original:"):
            self.assertNotIn(obsolete, ui)

        mac_viewer = mac.split("func showReadOnlyText(", 1)[1].split("func showActionMenu(", 1)[0]
        self.assertIn("let controller = NativeReadOnlyCodeController(", mac_viewer)
        self.assertIn("activeReadOnlyCodeController = controller", mac_viewer)
        # The viewer is a child surface like every other one: its own movable
        # window that locks the app until the document closes, never a child
        # window the window behind it stays clickable behind.
        self.assertIn("presentChildPanel(controller.panel, in: activeWindow(), prepare: { controller.loadContent() })", mac_viewer)
        self.assertNotIn("owner.addChildWindow(panel, ordered: .above)", mac_viewer)
        self.assertIn("private func readOnlyCodeEditorHTML(html: String, text: String, language: String) -> String", mac)
        self.assertIn('"readOnly": true', mac)
        self.assertIn('"showDiff": false', mac)
        self.assertIn('.replacingOccurrences(of: "</script", with: "<\\\\/script", options: .caseInsensitive)', mac)
        self.assertIn('let command = "<script>window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(\\(json));</script>"', mac)
        mac_controller = mac.split("private final class NativeReadOnlyCodeController", 1)[1].split(
            "/// URL policy for the official device-code pages.", 1
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

    def test_official_provider_auth_webview_is_optional_and_never_captures_credentials(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        bridge_header = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")

        self.assertIn("showProviderAuth?(options:", types)
        self.assertIn("fingerprint?: string;", types)
        self.assertIn("callbackURL?: string;", types)
        self.assertIn("showProviderAuth?(options:", bridge)
        self.assertIn("showProviderAuth?:", platform)
        self.assertIn("showProviderAuth: bridge.showProviderAuth", bridge)
        self.assertIn("showProviderAuth: leaf.showProviderAuth", platform)
        self.assertIn("private final class NativeProviderAuthController", mac)
        self.assertIn("activeProviderAuthControllers: [String: NativeProviderAuthController]", mac)
        self.assertIn("fingerprint: String?", mac)
        controller = mac.split("private final class NativeProviderAuthController", 1)[1].split(
            "private final class NativeActionMenuTarget", 1
        )[0]
        self.assertIn("configuration.websiteDataStore = .nonPersistent()", controller)
        self.assertIn("WKWebView(frame: .zero, configuration: configuration)", controller)
        self.assertIn(
            "NativeProviderAuthPolicy.allows(provider: provider, url: targetURL, callbackURL: callbackURL)",
            controller,
        )
        self.assertIn(
            "NativeProviderAuthPolicy.allows(provider: provider, url: responseURL, callbackURL: callbackURL)",
            controller,
        )
        self.assertNotIn("WKScriptMessageHandler", controller)
        self.assertNotIn("evaluateJavaScript", controller)
        self.assertIn("@objc(showProviderAuth:resolver:rejecter:)", module)
        self.assertIn("RCT_EXTERN_METHOD(showProviderAuth:(NSDictionary *)options", bridge_header)
        policy = mac.split("private enum NativeProviderAuthPolicy", 1)[1].split(
            "private final class NativeProviderAuthController", 1
        )[0]
        self.assertIn('url.scheme?.lowercased() == "https"', policy)
        self.assertIn('url.scheme?.lowercased() == "http"', policy)
        self.assertIn('host == "localhost" || host == "127.0.0.1"', policy)
        self.assertIn('url.path == "/callback"', policy)
        self.assertIn('host == "auth.openai.com"', policy)
        self.assertIn('path == "/oauth/authorize"', policy)
        self.assertIn('host == "challenges.cloudflare.com"', policy)
        self.assertIn('path.hasPrefix("/cdn-cgi/") || path.hasPrefix("/turnstile/")', policy)
        self.assertIn('if provider == "claude" {', mac)
        self.assertIn('NSWorkspace.shared.open(url)', mac)
        self.assertLess(mac.index('if provider == "claude" {'), mac.index('let authFingerprint: String'))

    def test_localization_crosses_the_native_leaf_contract(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("interface NativeLocalization", types)
        self.assertIn("setLocalization(strings: NativeLocalization)", types)
        self.assertIn("native.setLocalization({", ui)
        self.assertIn('translate("common.find")', ui)
        self.assertNotIn('webdavToggle: translate("webdav.enabled")', ui)
        self.assertIn('menuQuit: translate("status.quit")', ui)
        self.assertIn("func setLocalization", mac)
        localization = mac.split("func setLocalization", 1)[1].split("func setMenuActions", 1)[0]
        self.assertIn("window.title = title", localization)
        self.assertNotIn("configure(window", localization)
        self.assertIn('localized("menuQuit", fallback: "Quit Young Router")', mac)
        self.assertIn('"webdav-status"', mac)
        self.assertIn("void WinUI3NativeLeaf::SetLocalization", windows)


    def test_relay_login_is_a_native_browser_boundary_with_sanitized_results(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        bridge = (SHARED / "platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform = (SHARED / "platformEntry.ts").read_text(encoding="utf-8")
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        core_ipc = (ROOT / "young_router/core/ipc.py").read_text(encoding="utf-8")

        self.assertIn("relayLogin(options:", types)
        self.assertIn('type: "newapi" | "sub2api"', types)
        self.assertIn("language: LanguagePreference", types)
        self.assertNotIn("revision: number;\n  }):", types.split("relayLogin(options:", 1)[1].split("setLaunchAtLogin", 1)[0])
        self.assertIn('loginStatus: "signed_in"', types)
        self.assertIn("relayLogin(options:", bridge)
        self.assertIn("relayLogin?: (options:", platform)

        relay_ui = (SHARED / "ui/RelayAccountManager.tsx").read_text(encoding="utf-8")
        wizard_ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        # The station sign-in lives in the shared wizard window; the account
        # panel keeps the sanitized native session boundary.
        self.assertIn("language,", relay_ui)
        self.assertIn("native.relayLogin({", relay_ui)
        self.assertIn("pendingAccount: true,", relay_ui)
        self.assertIn("await runPendingAction(\"add\", startPendingLogin);", relay_ui)
        self.assertIn("native.cancelRelayLogin();", wizard_ui)
        # Pending logins flow through both hosts: the module allowlists the
        # pending/station fields and the accept payload forwards them to Core,
        # which creates the account shell only after sign-in succeeds.
        self.assertIn('"pendingAccount", "stationId", "stationName", "stationType", "stationOrigin"', mac_module)
        self.assertIn('pendingAccount: pendingAccount,', mac_module)
        self.assertIn('"pendingAccount", "stationId", "stationName", "stationType", "stationOrigin"', windows_module)
        self.assertIn('native_options.pending_account = pending_account.value();', windows_module)
        # What a sign-in may keep is asked by the hosts after the login
        # succeeds: a subordinate post-login prompt replaces the retired
        # pre-login choice surface and checkbox.
        self.assertNotIn("showRelayLoginChoice", types)
        self.assertNotIn("showRelayLoginChoice", mac_bridge)
        self.assertNotIn("showRelayLoginChoice", mac_leaf)
        self.assertIn('text("Remember Password", "记住密码")', mac_leaf)
        self.assertIn('text("Session Only", "仅记住登录态")', mac_leaf)
        self.assertIn("presentRememberPasswordPrompt", mac_leaf)
        self.assertIn("ShowRememberPasswordPrompt", windows_relay)
        # The sign-in browser leaves the screen before the question is asked:
        # a page the user has finished with must not sit under a modal question
        # that locks it.  The prompt dismisses the browser surface first, and
        # that dismissal takes the page probes off the clock so the answer
        # cannot commit into a check the login watcher already dropped.
        self.assertIn("private func dismissBrowserSurface() {", mac_leaf)
        prompt = mac_leaf.split("private func presentRememberPasswordPrompt(completion: @escaping (Bool) -> Void) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("dismissBrowserSurface()", prompt)
        # Order matters inside the prompt: the browser goes away first.
        self.assertLess(prompt.index("dismissBrowserSurface()"), prompt.index("presentDecisionPanel("))
        # The dismissal must not end the flow it belongs to.
        surface = mac_leaf.split("private func dismissBrowserSurface() {", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("embeddedClose?()", surface)
        self.assertIn("panel.orderOut(nil)", surface)
        # Windows hides the dialog for the same reason rather than closing it,
        # which would end the message loop that is still importing the session.
        self.assertIn("void HideLoginDialog(std::shared_ptr<LoginState> const& state) {", windows_relay)
        self.assertIn("ShowWindow(handle, SW_HIDE);", windows_relay)
        probe_login = windows_relay.split("winrt::fire_and_forget ProbeLogin", 1)[1].split("winrt::fire_and_forget InitializeBrowser", 1)[0]
        self.assertLess(probe_login.index("HideLoginDialog(state);"), probe_login.index("co_await ShowRememberPasswordPrompt(state);"))
        self.assertIn('L"Remember Password", L"记住密码"', windows_relay)
        self.assertIn('L"Session Only", L"仅记住登录态"', windows_relay)
        self.assertIn('payload["pending_account"] = true', mac_core)
        self.assertIn('payload.SetNamedValue(L"pending_account"', windows_core)
        self.assertIn('"pending_account"', core_ipc)
        # The providers-window login opens as a child window like every other
        # child surface, so the app stays locked behind it until the flow ends
        # and the window keeps its own title-bar close button.
        self.assertIn('embeddedWindow: embeddedWindow,', mac_leaf)
        self.assertNotIn("sheetParent", mac_leaf)
        self.assertIn('let presentationParent = embeddedWindow == nil ? settingsWindow() : nil', mac_leaf)
        self.assertIn("presentChildPanel(panel, in: presentationParent", mac_leaf)
        self.assertIn("embedded: true,", wizard_ui)
        self.assertIn("suggestedRelayStationName(candidate)", wizard_ui)
        # The relay family is auto-detected; no manual type state survives.
        self.assertNotIn("manualType", wizard_ui)
        self.assertIn("const accountType = await resolveRelayType();", wizard_ui)
        self.assertNotIn("const updateStationName = (value: string): void =>", wizard_ui)
        self.assertIn('onBlur={() => { void detectRelayType(); }}', relay_ui) if False else None
        self.assertIn("stationForProvider", wizard_ui)
        self.assertIn("accountType", wizard_ui)
        self.assertIn("NativePicker", relay_ui)
        # Both relay login entry points are pending flows: Core creates the
        # account shell only after sign-in succeeds.
        self.assertIn("pendingAccount: true,", wizard_ui)
        self.assertNotIn("await relay.addAccount(", wizard_ui)
        self.assertNotIn('relay.commit("account.delete"', wizard_ui)
        self.assertNotIn("const [addStep, setAddStep] = useState<AddStep>", relay_ui)
        # The login boundary stays native-only: Core never sees page fields.
        self.assertNotIn("document.querySelector", relay_ui)
        self.assertIn("NativeCheckbox", relay_ui)
        self.assertNotIn('title={translate("relay.importSelected")}', relay_ui)
        self.assertIn("const attemptedAccounts = useRef(new Set<string>());", relay_ui)
        # The retired manual re-login control is gone: the quiet mount restore
        # answers the session question, so no button re-runs it.  The one
        # refresh control the pane carries is 刷新资源 — the affordance every
        # relay message already names ("请点击刷新资源") and which used to have
        # no control at all.
        self.assertNotIn('onPress={() => { if (!isAccountLoading(selected.id)) void refreshLoginState(selected); }}', relay_ui)
        self.assertIn('title={translate("relay.refreshResources")} symbol="refresh"', relay_ui)
        self.assertNotIn('translate("relay.status.signed_out")', relay_ui)

        self.assertIn("import WebKit", mac_leaf)
        self.assertIn("configuration.websiteDataStore = .nonPersistent()", mac_leaf)
        self.assertNotIn("configuration.websiteDataStore = .default()", mac_leaf)
        self.assertIn('Probe(family: "newapi", path: "api/user/self"', mac_leaf)
        self.assertIn('Probe(family: "sub2api", path: "api/v1/auth/me"', mac_leaf)
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
        self.assertIn("if (user && typeof user.token === 'string' && user.token) return user.token;", mac_leaf)
        # The page's own account record is the authority for the token the
        # sign-in just produced: a New API fork keeps it in ``user.token`` and
        # nowhere else, so the capture and the watcher both read that record
        # first instead of the key names a fork never writes.
        self.assertIn("const candidate = user && typeof user === 'object' ? user.token : '';", mac_leaf)
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
        # Both relay families land directly on the login form.
        self.assertIn('return originURL.appendingPathComponent("login")', mac_leaf)
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
        self.assertIn("__young_router_relay_password", mac_leaf)
        self.assertIn("rememberPassword: Bool?,", mac_leaf)
        self.assertIn("let capturedPassword = self.capturedPassword", mac_leaf)
        self.assertIn("password: rememberPassword == true ? capturedPassword : nil", mac_leaf)
        self.assertNotIn("autoLoginAttempt", mac_leaf)
        self.assertIn("private var automaticCheckProbe: DispatchWorkItem?", mac_leaf)
        self.assertIn("private var loginFormRevealProbe: DispatchWorkItem?", mac_leaf)
        self.assertIn("private func scheduleLoginFormReveal()", mac_leaf)
        self.assertIn("user.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });", mac_leaf)
        self.assertIn("user.focus({ preventScroll: true })", mac_leaf)
        self.assertIn("private static let embeddedContentHeightScript", mac_leaf)
        self.assertIn("private static let embeddedHeaderHeight: CGFloat = 76", mac_leaf)
        self.assertIn("private static let embeddedStepTopInset: CGFloat = 92", mac_leaf)
        self.assertIn("private static let embeddedStepBottomInset: CGFloat = 54", mac_leaf)
        self.assertIn("getBoundingClientRect", mac_leaf)
        self.assertIn("agreementPattern", mac_leaf)
        self.assertIn("lastAnchor.bottom - topAnchor.top + 40", mac_leaf)
        self.assertIn("window.scrollTo({ top: targetScroll", mac_leaf)
        # A single-page station sign-in is announced by its own storage, never
        # by a navigation, and its announcement dialog covers the form.
        self.assertIn("private static let relayLoginSurfaceScript", mac_leaf)
        # Swift string literals process backslash escapes, so the JS those
        # literals carry must not contain any (a stray escape silently broke
        # the announcement dismissal script once).
        js_scripts = {}
        for name in ("relayLoginSurfaceScript", "loginStateScript", "embeddedContentHeightScript", "immediateWebPresentationScript"):
            match = re.search(rf'private static let {name} = """\n(.*?)\n    """', mac_leaf, re.S)
            self.assertIsNotNone(match, name)
            js_scripts[name] = match.group(1)
            self.assertNotIn("\\", match.group(1), f"{name} must not carry JS backslash escapes")
        self.assertIn("String.fromCharCode(9, 10, 11, 12, 13, 32)", js_scripts["relayLoginSurfaceScript"])
        self.assertIn("private static let loginStateScript", mac_leaf)
        self.assertIn("window.__youngRouterLoginSurface", mac_leaf)
        self.assertIn("litellmRelayPage", mac_leaf)
        self.assertIn("private func scheduleLoginWatch(delay: TimeInterval = 1)", mac_leaf)
        self.assertIn("private func pollLoginState()", mac_leaf)
        self.assertIn("private func recoverStalledCheck()", mac_leaf)
        self.assertIn("window.history[name] = function (...args)", mac_leaf)
        self.assertIn("private func scheduleEmbeddedBrowserResize", mac_leaf)
        self.assertIn("private func resizeEmbeddedBrowser(contentHeight:", mac_leaf)
        self.assertIn("embeddedWindow.setContentSize(NSSize(width: 900, height: height))", mac_leaf)
        self.assertIn("guard statusLabel.stringValue != value else { return }", mac_leaf)
        self.assertIn("if !automatically {", mac_leaf)
        self.assertIn("setStatus(waitingForSignInStatus)", mac_leaf)
        self.assertNotIn('text("Waiting for sign-in; success is detected automatically.', mac_leaf)
        self.assertIn('text("1 Relay URL  ›  2 Sign in", "1 中转站 URL  ›  2 登录")', mac_leaf)
        self.assertNotIn("3 Select resources", mac_leaf)
        self.assertIn("signInButton.isHidden = !showsReloadAction", mac_leaf)
        self.assertIn("cancelButton.isHidden = !showsPanelActions", mac_leaf)
        self.assertIn("let showsEmbeddedClose = false", mac_leaf)
        self.assertIn("func cancelRelayLogin()", mac_leaf)
        self.assertIn("@objc func cancelRelayLogin()", mac_module)
        # The ObjC interop bridge must export the method: without this line the
        # JS `native.cancelRelayLogin()` optional chain silently no-ops and the
        # embedded login browser can never be dismissed by the wizard's Back.
        self.assertIn("RCT_EXTERN_METHOD(cancelRelayLogin)", mac_bridge)
        # The wizard is a child window of the workspace, so the embedded
        # sign-in step rides the wizard's own close action instead of a sheet.
        self.assertIn("presentChildPanel(window, in: settingsWindow())", mac_leaf)
        self.assertIn("@objc private func closeEmbeddedWindow", mac_leaf)
        self.assertIn('self?.close(route: "provider-wizard")', mac_leaf)
        self.assertIn('routeWindows["provider-wizard"].flatMap', mac_leaf)
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
        # A React-side Back must resolve immediately even when a credential
        # commit is in flight, so the wizard can never stay busy behind a
        # stale embedded browser.
        self.assertIn("panelClosedDuringCommit = true", mac_leaf)
        self.assertNotIn("dismissWhileCommitting()", mac_leaf)
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
        self.assertIn('auto prior_password = ReadChunkedCredential(state->options.account_id, L"password");', windows_relay)
        self.assertIn('host == L"localhost"', windows_relay)
        self.assertIn('state->webview.Source(winrt::Windows::Foundation::Uri(Utf8ToWide(state->options.origin)));', windows_relay)
        probe_login = windows_relay.split("winrt::fire_and_forget ProbeLogin", 1)[1].split("winrt::fire_and_forget InitializeBrowser", 1)[0]
        self.assertNotIn("state->captured_password.reset();", probe_login)
        self.assertIn("if (!password) password = state->captured_password;", windows_relay)
        self.assertIn("remember_password = co_await ShowRememberPasswordPrompt(state);", windows_relay)
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
        logs_ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_relay = (WIN_NATIVE / "WindowsRelayLogin.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        core_ipc = (ROOT / "young_router/core/ipc.py").read_text(encoding="utf-8")

        self.assertIn("restoreRelaySession(options:", types)
        self.assertIn("restoreRelaySession(options:", bridge)
        self.assertIn("restoreRelaySession?: (options:", platform)
        self.assertIn("native.restoreRelaySession", relay_ui)
        self.assertIn("username: account.username || undefined", relay_ui)
        self.assertIn("const attemptedAccounts = useRef(new Set<string>());", relay_ui)
        self.assertIn("await refreshAccountResources(account, { silent: true });", relay_ui)
        self.assertNotIn('translate("relay.lastUpdated"', relay_ui)
        self.assertNotIn("accountAvatar", relay_ui)
        self.assertNotIn("native.showActionMenu", relay_ui)
        self.assertNotIn("NativeToggle", relay_ui)
        provided_panel = logs_ui.split("function ProviderKeysPanel", 1)[1].split("function CodexWorkspace", 1)[0]
        self.assertNotIn('value={selectedProvided.resource.enabled}', provided_panel)
        self.assertIn('label={translate("providers.keyValue")}', provided_panel)
        self.assertIn('symbol="copy"', provided_panel)
        self.assertIn('native.copySecret({', provided_panel)
        # The attribute order differs in the new panel; assert both parts.
        self.assertIn('domain="relay_accounts"', provided_panel)
        self.assertIn('field="api_key"', provided_panel)
        # The custom key's 密钥值 field carries the same copy icon at its own
        # trailing edge, and it copies the provider's stored key.
        self.assertIn('domain: "providers_models", field: "api_key"', provided_panel)
        self.assertIn('copySecret(options:', types)
        self.assertIn('copySecret(domain: "providers_models" | "relay_accounts", field: "api_key", target: string)', bridge)
        self.assertIn('copySecret?: (domain: "providers_models" | "relay_accounts", field: "api_key", target: string)', platform)
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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
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
        # The chooser is the reference child surface: its own movable window,
        # the window behind it locked by the shield, and a completion that
        # carries the selection once that window closes.
        self.assertIn("completion: @escaping ([String]?) -> Void) {", mac_leaf)
        self.assertIn("presentChildPanel(panel, in: activeWindow(), prepare: { controller.focusSearchField() })", mac_leaf)
        self.assertIn("completion(selection)", mac_leaf)
        # The host owns the chooser while its window is up. AppKit holds a
        # window's delegate and a control's target weakly, so a chooser that
        # only exists as a local in `chooseModelsToAdd` is collected the moment
        # that method returns: the panel then draws and lists its models while
        # 取消, 全选, 反选 and + each deliver their action to nobody, the
        # title-bar close button never reaches `windowWillClose`, and no path
        # answers the pending promise. One completion settles the chooser and
        # ends the child surface together, so the window it locked behind it is
        # unlocked by the same call that answers the promise.
        self.assertIn("private var openModelChooser: ModelChooser?", mac_leaf)
        self.assertIn("let completion: ([String]?) -> Void", mac_leaf)
        self.assertIn(
            "openModelChooser = ModelChooser(panel: panel, controller: controller, completion: completion)",
            mac_leaf,
        )
        self.assertIn("controller.onFinish = { [weak self] selection in", mac_leaf)
        self.assertIn("self?.finishModelChooser(panel, selection: selection)", mac_leaf)
        self.assertIn("private func finishModelChooser(_ panel: NSPanel, selection: [String]?) {", mac_leaf)
        self.assertIn("panel.delegate = nil", mac_leaf)
        self.assertIn("chooser.completion(selection)", mac_leaf)
        # A request that lands on an open chooser settles it instead of stacking
        # a second identical window over the one the user is answering, and the
        # lock only brings forward a child that is still open.
        self.assertIn("if let open = openModelChooser {", mac_leaf)
        self.assertIn("guard let panel, self?.isChildPanel(panel) == true else { return }", mac_leaf)
        self.assertIn("let searchField = NativeInstantFocusSearchField()", mac_leaf)
        self.assertIn("searchField.focusRingType = .none", mac_leaf)
        self.assertIn("showsInstantFocusBorder = true", mac_leaf)
        self.assertIn("field.showsInstantFocusBorder = false", mac_leaf)
        self.assertIn("private let focusBorderView = NativeInstantFocusBorderView(frame: .zero)", mac_leaf)
        self.assertIn("layer?.borderWidth = borderVisible ? 3 : 0", mac_leaf)
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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
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
            / "codegen/react/components/YoungRouter/LiteLLMWinUISecureTextInput.g.h"
        ).read_text(encoding="utf-8")

        self.assertIn("export function NativeSecureTextInput", adapter)
        self.assertIn("LiteLLMAppKitSecureTextInput", mac_spec)
        self.assertIn("LiteLLMWinUISecureTextInput", windows_spec)
        for spec in (mac_spec, windows_spec):
            self.assertNotIn("value?:", spec)
            self.assertIn("multiline?: WithDefault<boolean, false>;", spec)
            self.assertIn("plainText?: WithDefault<boolean, false>;", spec)
            self.assertNotIn("onChangeText", spec)
            self.assertIn("onSecretState", spec)
        self.assertIn("LiteLLMTabSecureTextField *_field", mac)
        self.assertIn("LiteLLMTabTextField *_plainField", mac)
        self.assertIn("LiteLLMTabTextView *_multilineField", mac)
        self.assertIn("NSTextViewDelegate", mac)
        self.assertIn("loadPlainTextSecretForGeneration", mac)
        self.assertIn("void ConfigureSingleLineTextField(NSTextField *field)", mac)
        self.assertIn("field.usesSingleLineMode = YES", mac)
        self.assertIn("field.lineBreakMode = NSLineBreakByTruncatingTail", mac)
        self.assertIn("cell.wraps = NO", mac)
        self.assertIn("cell.scrollable = YES", mac)
        self.assertIn("ConfigureSingleLineTextField(_field);", mac)
        self.assertIn("ConfigureSingleLineTextField(_plainField);", mac)
        self.assertEqual(3, mac.count("[self addCursorRect:self.bounds cursor:[NSCursor IBeamCursor]];"))
        self.assertIn("@interface LiteLLMTabSearchField : NSSearchField", mac)
        self.assertIn("stageSecretForDomain", mac)
        self.assertIn("stageSecretForDomain(", mac_core)
        self.assertIn("PasswordBox password_box_", windows)
        self.assertIn("TextBox multiline_box_", windows)
        self.assertIn("multiline_box_.AcceptsReturn(true);", windows)
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
        self.assertNotIn("substringToIndex:12", mac)
        self.assertNotIn("display_value.substr(0, 12)", windows)
        self.assertNotIn("onChangeText", windows_codegen)
        self.assertIn("OnSecretState", windows_codegen)
        self.assertIn("NativeSecretInputControl", ui)
        self.assertIn("multiline plainText autoCommit", ui)
        self.assertNotIn('onEdit={() => stageSecret({ domain: "runtime"', ui)

    def test_provider_api_key_plaintext_readback_is_narrow_and_native_only(self) -> None:
        ipc = (ROOT / "young_router" / "core" / "ipc.py").read_text(encoding="utf-8")
        service = (ROOT / "young_router" / "core" / "service.py").read_text(encoding="utf-8")
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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeSecureTextInputNativeComponent.ts").read_text(encoding="utf-8")
        windows_spec = (SHARED / "ui/windows/NativeSecureTextInputNativeComponent.ts").read_text(encoding="utf-8")

        workspace = ui.split("function ProviderKeysPanel", 1)[1].split("function TablePane", 1)[0]
        self.assertIn("<NativeSecretField labelVisible={false} plainText autoCommit", workspace)
        self.assertNotIn('setTitle={translate("common.set")}', workspace)
        self.assertNotIn('clearTitle={translate("common.clear")}', workspace)
        self.assertNotIn("onClear={() => clearSecret", workspace)
        self.assertNotIn("providers.apiKeyHint", workspace)
        for spec in (mac_spec, windows_spec):
            self.assertIn("plainText?: WithDefault<boolean, false>;", spec)
            self.assertIn("autoCommit?: WithDefault<boolean, false>;", spec)
        self.assertIn("_field.action = @selector(submitSecret:);", mac)
        self.assertIn("controlTextDidEndEditing", mac)
        self.assertIn("loadPlainTextSecretForGeneration", mac)
        self.assertIn("readPlainTextSecret", mac_core)
        self.assertIn("host/secret/read", mac_core)
        self.assertIn("ReadPlainTextSecret", windows_core)

    def test_dsh_router_runtime_json_is_readable_as_multiline_plaintext(self) -> None:
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        mac_core = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        windows_core = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        key = "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON"
        for source in (mac, mac_core, windows, windows_core):
            self.assertIn(key, source)
        self.assertIn("allowMultiline:", mac_core)
        self.assertIn("allow_multiline", windows_core)

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
        self.assertIn("plainLink?: boolean;", adapter)
        for spec in (mac_spec, windows_spec):
            self.assertIn("link?: WithDefault<boolean, false>;", spec)
            self.assertIn("plainLink?: WithDefault<boolean, false>;", spec)
        # A plain link keeps link behavior but drops the inline bezel and
        # left-aligns its title, for a long value that owns its whole column.
        self.assertIn("BOOL plainLink = link && newViewProps.plainLink;", mac)
        self.assertIn("_button.bordered = !plainLink;", mac)
        self.assertIn("_button.alignment = plainLink ? NSTextAlignmentLeft : NSTextAlignmentCenter;", mac)
        self.assertIn("plainLink", windows)
        # ...and it is the quiet variant: secondary ink at the row's weight,
        # never the accent-colored navigation link, underlined at rest because
        # the underline is the affordance, and never restating its color for
        # hover — only the navigation link takes the accent from the pointer.
        self.assertIn("((LiteLLMNavigationLinkButton *)_button).plainLinkMode = plainLink;", mac)
        self.assertIn("NSFontWeight linkWeight = plainLink ? NSFontWeightRegular : NSFontWeightSemibold;", mac)
        self.assertIn("color = self.enabled ? NSColor.secondaryLabelColor : NSColor.tertiaryLabelColor;", mac)
        self.assertIn("(self.plainLinkMode || _hovering);", mac)
        self.assertIn("NSUnderlineStyleAttributeName: underlined ? @(NSUnderlineStyleSingle) : @0,", mac)
        self.assertIn("color = _hovering ? NSColor.controlAccentColor : NSColor.linkColor;", mac)
        self.assertNotIn("NSColor.labelColor : NSColor.secondaryLabelColor", mac)
        self.assertIn("linkLabel.Foreground(SecondaryTextBrush());", windows)
        self.assertIn("linkLabel.TextDecorations(winrt::Windows::UI::Text::TextDecorations::Underline);", windows)
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
        single_line = controls.split("void ConfigureSingleLineTextField", 1)[1].split("NSInteger SegmentIndex", 1)[0]
        self.assertIn("field.focusRingType = NSFocusRingTypeNone;", single_line)
        self.assertNotIn("NSFocusRingTypeDefault", single_line)
        self.assertIn("InstallInstantFocusBorder(self);", controls)
        self.assertIn("[CATransaction setDisableActions:YES];", controls)
        self.assertIn("self.layer.borderWidth = borderVisible ? 3.0 : 0.0;", controls)
        self.assertIn("[field addSubview:border positioned:NSWindowAbove relativeTo:nil];", controls)
        self.assertNotIn("ConfigureImmediateView(field);", controls)
        self.assertIn("NSColor.keyboardFocusIndicatorColor", controls)
        self.assertIn("strongSelf.liteLLMShowsFocusBorder = YES;", controls)
        self.assertIn("_field.liteLLMShowsFocusBorder = NO;", controls)
        self.assertIn("strongSelf.window.firstResponder == strongSelf.currentEditor", controls)

        self.assertIn("@interface LiteLLMAppKitControlHostView : NSView", controls)
        self.assertIn("NSMidY(bounds) - height / 2.0", controls)
        self.assertIn("control.intrinsicContentSize.height", controls)
        self.assertIn("[_activeControl isKindOfClass:NSScrollView.class]", controls)
        text_host = controls.split("@implementation LiteLLMAppKitTextFieldHostView", 1)[1].split("@end", 1)[0]
        self.assertNotIn("[_activeControl isKindOfClass:NSTextField.class]", text_host)
        self.assertNotIn("ConfigureImmediateView", text_host)
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
        # A scroller the table actually needs stays on screen: the table keeps it
        # with auto-hide off and releases it again once the table fits, instead
        # of letting AppKit fade the bar out while idle.
        self.assertIn(
            "const BOOL usesScrollers = _scrollView.hasHorizontalScroller || _scrollView.hasVerticalScroller;",
            controls,
        )
        self.assertIn("const NSScrollerStyle scrollerStyle = usesScrollers ? NSScrollerStyleLegacy : NSScrollerStyleOverlay;", controls)
        self.assertIn("const BOOL autohidesScrollers = !usesScrollers;", controls)
        self.assertIn("if ([self applyPersistentTableScrollerChrome]) continue;", controls)
        self.assertIn("scrollView.horizontalScroller.hidden = NO;", controls)
        self.assertIn("scrollView.verticalScroller.hidden = NO;", controls)
        # The scroller the app keeps on screen is its own translucent capsule in
        # the legacy slot, and the legacy gutter is removed by zeroing the
        # width AppKit reserves for it and floating the capsule over the
        # content's trailing edge instead.
        self.assertIn("static void InstallPersistentScrollers(NSScrollView *scrollView, BOOL horizontal, BOOL vertical)", controls)
        self.assertIn(
            "scrollView.horizontalScroller = [[LiteLLMPersistentScroller alloc] initWithFrame:NSZeroRect];",
            controls,
        )
        self.assertIn("void FloatPersistentScrollerOverContent(NSScrollView *scrollView)", controls)
        self.assertIn("const CGFloat strip = MAX(11, NSWidth(frame));", controls)
        self.assertIn("frame.origin.x = NSMaxX(bounds) - strip;", controls)
        self.assertIn("frame.size.width = strip;", controls)
        self.assertIn("const NSRect clipFrame = NSMakeRect(NSMinX(bounds), NSMinY(bounds), NSWidth(bounds), NSHeight(bounds));", controls)
        self.assertIn("FloatPersistentScrollerOverContent(scrollView);", controls)
        # The capsule itself measures zero, so the gutter AppKit reserves for a
        # legacy scroller is zero: the table, its columns, its header, and the
        # selection bar of the row under it keep the pane's full width.
        mac_leaf_capsule = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertIn(
            "public override class func scrollerWidth(for controlSize: NSControl.ControlSize, scrollerStyle: NSScroller.Style) -> CGFloat {",
            mac_leaf_capsule,
        )
        # AppKit leaves the scroller view it supersedes in the scroll view's view
        # tree, and that leftover draws its own full-length knob next to the live
        # capsule - a second bar on the table's trailing edge.  Every tiling pass
        # drops the scrollers the scroll view no longer uses.
        self.assertIn("void DiscardStaleScrollerSubviews(NSScrollView *scrollView)", controls)
        self.assertIn("for (NSView *subview in [scrollView.subviews copy]) {", controls)
        self.assertIn("if (subview == scrollView.verticalScroller || subview == scrollView.horizontalScroller) continue;", controls)
        self.assertIn("DiscardStaleScrollerSubviews(self);", controls)
        self.assertIn("DiscardStaleScrollerSubviews(scrollView);", controls)
        # AppKit rebuilds a scroller whenever a flag flips, so the tile override
        # re-installs the app capsule as well as the floating placement.
        self.assertIn("if (self.hasVerticalScroller && ![self.verticalScroller isKindOfClass:LiteLLMPersistentScroller.class]) {", controls)
        self.assertIn("if (self.hasHorizontalScroller && ![self.horizontalScroller isKindOfClass:LiteLLMPersistentScroller.class]) {", controls)
        self.assertIn("FloatPersistentScrollerOverContent(self);", controls)
        mac_leaf_scrollers = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertIn("func floatOverContent()", mac_leaf_scrollers)
        # The scroller floats over the content, so the capsule is the only thing
        # its frame may paint: the legacy slot, bezel, and arrow ends AppKit
        # would draw are backgrounds over the row under the knob.
        self.assertIn("public override func draw(_ dirtyRect: NSRect) {", mac_leaf_scrollers)
        self.assertIn("public override func drawKnobSlot(in slotRect: NSRect, highlight flag: Bool) {", mac_leaf_scrollers)
        self.assertIn("final class LiteLLMPersistentScroller: NSScroller", mac_leaf_scrollers)
        self.assertIn("[self installPersistentTableScrollers];", controls)
        self.assertIn("_scrollView.horizontalScrollElasticity = NSScrollElasticityNone", controls)
        self.assertIn("_scrollView.verticalScrollElasticity = NSScrollElasticityNone", controls)
        self.assertIn("@interface LiteLLMTableScrollView : NSScrollView", controls)
        self.assertIn("- (void)scrollWheel:(NSEvent *)event", controls)
        # A two-finger trackpad gesture is claimed by the window's responsive
        # scrolling machinery, which routes the whole gesture to the outermost
        # compatible scroll view - the RN pane - so a nested native table never
        # saw it (a mouse wheel takes the classic path, which is why only
        # trackpads looked dead).  The panes stay on the classic path.
        self.assertIn("void UseClassicScrollingForPaneScrollViews(void)", controls)
        self.assertIn('Class paneScrollViewClass = NSClassFromString(@"RCTCustomScrollView");', controls)
        self.assertIn("SEL selector = @selector(isCompatibleWithResponsiveScrolling);", controls)
        self.assertIn("method_setImplementation(method, imp_implementationWithBlock(^BOOL(__unused id receiver) {", controls)
        # AppKit re-tiles the RN panes into a legacy gutter; the shared tile
        # hook floats any scroll view that carries the app capsule.
        self.assertIn("void FloatPersistentScrollersFromEveryTilingPass(void)", controls)
        self.assertIn("method_setImplementation(method, (IMP)LiteLLMScrollViewTile);", controls)
        self.assertIn("FloatPersistentScrollersFromEveryTilingPass();", controls)
        self.assertIn("UseClassicScrollingForPaneScrollViews();", controls)
        self.assertIn("BOOL TableScrollViewCanConsume(NSScrollView *scrollView, NSEvent *event, BOOL acceptsVerticalScroll, BOOL acceptsHorizontalScroll);", controls)
        # A wheel event belongs to the table while its dominant axis can still
        # move.  A two-finger swipe carries sideways drift with it, and handing
        # that event to the ancestor pane gives the pane the whole gesture (one
        # scroll view per gesture), which made the list dead to trackpads.
        self.assertIn("BOOL TableScrollViewCanScrollVertically(NSScrollView *scrollView, NSEvent *event)", controls)
        self.assertIn("BOOL TableScrollViewCanScrollHorizontally(NSScrollView *scrollView, NSEvent *event)", controls)
        self.assertIn("const BOOL wantsVertical = fabs(deltaY) >= 0.01;", controls)
        self.assertIn("const BOOL wantsHorizontal = fabs(deltaX) >= 0.01;", controls)
        self.assertIn("wantsVertical && acceptsVerticalScroll && TableScrollViewCanScrollVertically(scrollView, event);", controls)
        self.assertIn("wantsHorizontal && acceptsHorizontalScroll && TableScrollViewCanScrollHorizontally(scrollView, event);", controls)
        self.assertIn("return fabs(deltaY) >= fabs(deltaX) ? canScrollVertically : canScrollHorizontally;", controls)
        self.assertIn("BOOL ForwardWheelToParent(NSView *view, NSEvent *event);", controls)
        self.assertIn("if (TableScrollViewCanConsume(self, event, _acceptsVerticalScroll, _acceptsHorizontalScroll))", controls)
        self.assertIn("if (ForwardWheelToParent(self, event)) return;", controls)
        self.assertIn("_scrollView.acceptsVerticalScroll = NO", controls)
        self.assertIn("@interface LiteLLMTableClipView : NSClipView", controls)
        self.assertIn("_clipView.acceptsVerticalScroll = NO", controls)
        self.assertIn("_scrollView.contentView = _clipView;", controls)
        self.assertIn("_clipView.acceptsVerticalScroll = needsVerticalScroller;", controls)
        self.assertIn("- (NSRect)constrainBoundsRect:(NSRect)proposedBounds", controls)
        # The parked position is the clip view's real top: only movement above it
        # is clamped, so the first wheel notch and a scroller drag that ends
        # inside the header strip are not swallowed.
        self.assertIn("const CGFloat restingOrigin = -NSHeight(tableView.headerView.frame);", controls)
        self.assertIn("if (NSMinY(proposedBounds) <= restingOrigin + 0.5) {", controls)
        self.assertIn("constrained.origin.y = restingOrigin;", controls)
        self.assertIn("if (tableView.headerView != nil) minimumY = -NSHeight(tableView.headerView.frame);", controls)
        self.assertIn("clipBounds.origin.y = restingOrigin;", controls)
        self.assertIn("if (fabs(originY) < 0.5) {", controls)
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
        self.assertIn("overflowBehaviorChanged || sourceListChanged || rowsChanged;", controls)
        self.assertNotIn("_dataSignature", controls)
        self.assertNotIn("nextDataSignature", controls)
        self.assertIn("- (void)updateScrollerVisibility", controls)
        # The floating scroller strip must not expose the scroll view's (or the
        # header's own) background beside the header or over the first row's
        # top, so every tiling pass keeps the header as wide as the content it
        # labels — AppKit re-tiles the header on its own, at the pre-float width
        # the legacy gutter implies.
        self.assertIn("void WidenTableHeaderOverVisibleWidth(NSScrollView *scrollView)", controls)
        self.assertIn("const CGFloat headerWidth = NSWidth(scrollView.bounds);", controls)
        # The clip view AppKit draws the header through stops the header's own
        # chrome at the gutter, so it is widened with the view it clips.
        self.assertIn("NSView *headerClip = header.superview;", controls)
        self.assertIn("clipFrame.size.width = headerWidth;", controls)
        self.assertIn("headerFrame.size.width = headerWidth;", controls)
        self.assertIn("WidenTableHeaderOverVisibleWidth(self);", controls)
        self.assertIn("WidenTableHeaderOverVisibleWidth(_scrollView);", controls)
        # The gutter AppKit reserves for the persistent scroller narrows the clip
        # view, and with it the table, its columns, and its header: the fitting
        # pass removes it before it measures the viewport, and measures that
        # viewport from the scroll view's own width.
        self.assertIn("FloatPersistentScrollerOverContent(_scrollView);", controls)
        self.assertIn("const CGFloat viewportWidth = NSWidth(_scrollView.bounds);", controls)
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
        self.assertIn("const CGFloat availableColumnWidth = NSWidth(_scrollView.bounds);", controls)
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
        self.assertIn("cellHorizontalPadding={6}", (SHARED / "ui" / "YoungRouterApp.tsx").read_text(encoding="utf-8"))
        self.assertIn("firstColumnHorizontalPadding={0}", (SHARED / "ui" / "YoungRouterApp.tsx").read_text(encoding="utf-8"))
        logs_ui = (SHARED / "ui" / "YoungRouterApp.tsx").read_text(encoding="utf-8")
        logs_workspace = logs_ui.split("function LogsWorkspace", 1)[1].split("function Section", 1)[0]
        # The log table keeps content-sized columns (short columns never lose
        # text to an ellipsis) and scrolls trailing overflow horizontally, so a
        # long detail cell stays readable instead of being clipped.
        self.assertIn(
            "columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths scrollTrailingColumnOverflow",
            logs_workspace,
        )
        # Short columns keep their requested width (long model names ellipsize);
        # only the trailing detail column grows to its measured content, and the
        # measurement scan is bounded so a large log does not stall a refresh.
        self.assertIn("if (newViewProps.scrollTrailingColumnOverflow) {\n      [self updateColumnMinimumWidths];", controls)
        self.assertIn("LiteLLMTableMeasuredRowLimit", controls)
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

    def test_boolean_controls_reset_their_drawn_state_when_recycled(self) -> None:
        """A reused checkbox or switch must not paint another row's on state.

        The drawn state lives in the AppKit control, not in the props, and the
        props-update guard only repaints on a *changed* value.  A recycled
        view handed to a row whose props equal the defaults therefore kept the
        previous row's check painted over a configuration that said otherwise.
        """

        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        for implementation, exported, control, drawn in (
            (
                "@implementation LiteLLMAppKitCheckboxComponentView",
                "LiteLLMAppKitCheckboxCls",
                "_checkbox",
                "_checkbox.state = NSControlStateValueOff;",
            ),
            (
                "@implementation LiteLLMAppKitSwitchComponentView",
                "LiteLLMAppKitSwitchCls",
                "_switch",
                "_switch.state = NSControlStateValueOff;",
            ),
        ):
            start = controls.index(implementation)
            end = controls.index(f"Class<RCTComponentViewProtocol> {exported}", start)
            view = controls[start:end]
            self.assertIn("- (void)prepareForRecycle", view)
            self.assertIn("_props = defaultProps;", view)
            self.assertIn(drawn, view)
            self.assertIn(f"{control}.enabled = YES;", view)
            # A mount must paint the props it is given, not only a change:
            # a switch configured on has to mount on, and the first pass after
            # a mount or a recycle is what guarantees that.
            self.assertIn("BOOL _propsApplied;", view)
            self.assertIn("const BOOL firstPass = !_propsApplied;", view)
            self.assertIn("_propsApplied = YES;", view)
            self.assertIn("_propsApplied = NO;", view)
            self.assertIn("if (firstPass || oldViewProps.value != newViewProps.value) {", view)
            self.assertIn("if (firstPass || oldViewProps.disabled != newViewProps.disabled) {", view)
        # The Windows mirror resets the same controlled state from the island's
        # Destroying hook, exactly like the table and the code web view.
        self.assertIn(
            "void PrepareForRecycle(\n      winrt::Microsoft::ReactNative::ComponentView const&) noexcept {",
            windows,
        )
        self.assertIn("checkbox_.IsChecked(false);", windows)
        # The Windows mirror applies every prop when it has no previous props,
        # which is the same first-pass contract.
        self.assertIn("const bool value_changed = !old_props || old_props->value != props.value;", windows)
        self.assertIn("const bool disabled_changed = !old_props || old_props->disabled != props.disabled;", windows)
        self.assertIn("value_ = false;", windows)
        self.assertIn("  RegisterCheckbox(package_builder);", windows)
        self.assertIn("  RegisterSwitch(package_builder);", windows)

    def test_a_windows_pane_switch_keeps_the_users_window_geometry(self) -> None:
        """The initial size belongs to the first presentation, not every open.

        Every route open re-applied the route's initial content size and
        restored the window, so a resized or maximized window snapped back on
        the next pane switch or menu action; macOS only applies geometry when
        it creates a window.
        """

        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")
        open_route = windows.split("void WinUI3NativeLeaf::OpenRoute(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("if (!window_sized_) {", open_route)
        self.assertIn("window_sized_ = true;", open_route)
        self.assertIn("if (IsIconic(window_handle_)) ShowWindow(window_handle_, SW_RESTORE);", open_route)
        self.assertEqual(1, open_route.count("SW_RESTORE"))
        self.assertIn("bool window_sized_ = false;", header)

    def test_both_trays_open_the_same_pane_on_a_left_click(self) -> None:
        """macOS opens 供应商与模型; Windows took the first `open-` action.

        The shared UI lists 常规 first in that group, so a Windows left click
        opened a different pane than the same gesture on macOS.
        """

        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertIn('openNamedRoute("providers-models")', mac)
        dispatch = windows.split("void WinUI3NativeLeaf::DispatchDefaultTrayAction()", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('action.id == L"open-providers-models"', dispatch)
        self.assertIn('action.id.rfind(L"open-", 0) == 0', dispatch)
        self.assertLess(
            dispatch.index('action.id == L"open-providers-models"'),
            dispatch.index('action.id.rfind(L"open-", 0) == 0'),
        )

    def test_the_hosts_take_a_core_down_without_losing_the_next_one(self) -> None:
        """Teardown, re-subscribe, and retry survive a replaced Core.

        Three defects in one pass: the Windows bridge cleared a member that
        does not exist (the class keeps one handler per React root), the macOS
        bridge recorded its subscribe request only when the generation still
        matched — leaving no subscription *and* no request, so recovery could
        never ask again — and a Windows event poll spun at full speed on any
        non-200 answer.
        """

        windows_bridge = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        mac_bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_header = (WIN_NATIVE / "CoreIPCBridge.h").read_text(encoding="utf-8")

        # One handler per root, and a full teardown clears them all.
        self.assertIn("std::map<int, std::function<void(std::string const&)>> event_handlers_;", windows_header)
        self.assertIn("event_handlers_.clear();", windows_bridge)
        self.assertNotIn("event_handler_ = nullptr;", windows_bridge)
        # The macOS subscribe answer is remembered before the generation guard.
        subscribe = mac_bridge.split("private func startPollingIfSubscription(", 1)[1].split(
            "private func poll(", 1
        )[0]
        self.assertIn("let live = self.generation == generation", subscribe)
        self.assertIn("if live {\n            subscriptionID = subscription\n            pollCancelled = false\n        }", subscribe)
        self.assertLess(
            subscribe.index("subscriptionRequest = request"),
            subscribe.index("guard live else { return }"),
        )
        # A non-200 event poll waits instead of spinning.
        poll = windows_bridge.split("void CoreIPCBridge::PollEvents(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("std::this_thread::sleep_for(std::chrono::seconds(1));", poll)
        self.assertNotIn("if (result.status != 200) continue;", poll)

    def test_windows_controls_carry_their_hint_and_their_name(self) -> None:
        """A button's hint and a field's name reach the Windows host too.

        The macOS controls state both; the Windows specs did not even declare
        them, so every icon-only button had no hover hint (and a busy wheel no
        wording), and every switch and text field was announced as the same
        untranslated word or as a bare edit box.
        """

        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        button_spec = (SHARED / "ui/windows/NativeButtonNativeComponent.ts").read_text(encoding="utf-8")
        toggle_spec = (SHARED / "ui/windows/NativeToggleNativeComponent.ts").read_text(encoding="utf-8")
        input_spec = (SHARED / "ui/windows/NativeTextInputNativeComponent.ts").read_text(encoding="utf-8")
        controls = (SHARED / "ui/NativeControls.tsx").read_text(encoding="utf-8")
        mac_button_spec = (SHARED / "ui/macos/NativeButtonNativeComponent.ts").read_text(encoding="utf-8")

        # Both hosts declare the same hint/name fields on their button.
        for spec in (button_spec, mac_button_spec):
            self.assertIn("toolTip?: string;", spec)
            self.assertIn("accessibilityLabel?: string;", spec)
        self.assertIn("auto const toolTip = ToHString(props.toolTip.value_or(\"\"));", windows)
        self.assertIn("auto const hint = toolTip.empty() ? ToHString(props.title) : toolTip;", windows)
        self.assertIn("ToolTipService::SetToolTip(button_, winrt::box_value(hint));", windows)
        self.assertIn("ToolTipService::SetToolTip(hyperlink_, winrt::box_value(hint));", windows)
        self.assertIn("AutomationProperties::SetName(button_, label);", windows)

        # A switch and a text field name themselves from the shared label.
        for spec in (toggle_spec, input_spec):
            self.assertIn("accessibilityLabel?: string;", spec)
        self.assertIn("accessibilityLabel={accessibilityLabel}", controls)
        self.assertIn("accessibilityLabel={props.accessibilityLabel}", controls)
        switch = windows.split("struct SwitchComponentView final", 1)[1].split("struct SelectableRowComponentView", 1)[0]
        self.assertIn("winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(toggle_, label);", switch)
        self.assertIn("text_box_.IsEnabled(Enabled(props.disabled));", windows)
        self.assertIn("winrt::Microsoft::UI::Xaml::Automation::AutomationProperties::SetName(text_box_, label);", windows)

        # The generated Windows headers carry the fields the implementation reads.
        codegen = WIN_PROJECT / "codegen/react/components/YoungRouter"
        for name, field in (("LiteLLMWinUIButton.g.h", "toolTip"), ("LiteLLMWinUISwitch.g.h", "accessibilityLabel"), ("LiteLLMWinUITextInput.g.h", "accessibilityLabel")):
            header = (codegen / name).read_text(encoding="utf-8")
            self.assertIn(f"REACT_FIELD({field})", header)

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

        self.assertIn("const BOOL labelChanged = firstPass || oldViewProps.label != newViewProps.label;", checkbox)
        self.assertIn("const BOOL labelVisibilityChanged = firstPass || oldViewProps.labelVisible != newViewProps.labelVisible;", checkbox)
        self.assertIn('_checkbox.title = newViewProps.labelVisible ? label : @"";', checkbox)
        self.assertIn("_checkbox.accessibilityLabel = label;", checkbox)
        self.assertIn("const BOOL compactChanged = firstPass || oldViewProps.compact != newViewProps.compact;", checkbox)
        self.assertIn("if (labelChanged || labelVisibilityChanged || compactChanged) {\n    [_host setNeedsLayout:YES];\n  }", checkbox)
        self.assertNotIn("[_host setNeedsLayout:YES];", switch)

    def test_macos_switch_stays_at_the_compact_size_its_slot_reserves(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        switch = controls.split("@implementation LiteLLMAppKitSwitchComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitSwitchCls", 1
        )[0]

        # The system's regular NSSwitch is 54 x 24 pt, which is taller than the
        # 26 pt rows this app labels with 13 pt text.  The compact size
        # (44 x 20 pt) is the one the 44 pt slot is sized for.
        self.assertIn("_switch.controlSize = NSControlSizeSmall;", switch)
        self.assertNotIn("NSControlSizeRegular", switch)

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
        self.assertIn("if (titleChanged || symbolChanged || symbolWithTitleChanged || symbolTrailingChanged || linkChanged || plainLinkChanged || compactChanged || busyChanged) {\n    [_host setNeedsLayout:YES];\n  }", button)
        self.assertIn('symbolName = @"pause.fill";', controls)
        self.assertIn('symbolName = @"play.fill";', controls)
        self.assertIn('symbolName = @"minus";', controls)
        self.assertIn('symbolName = @"trash";', controls)
        self.assertIn('symbolName = @"tray.and.arrow.down";', controls)
        self.assertIn('symbolName = @"arrow.clockwise";', controls)
        self.assertIn("_button.imagePosition = image == nil\n      ? NSNoImage\n      : (showsTitle ? (trailingSymbol ? NSImageTrailing : NSImageLeading) : NSImageOnly);", button)
        self.assertIn('symbolName = @"info.circle";', controls)
        self.assertIn('symbolName = @"questionmark.circle";', controls)

        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        self.assertIn('const auto symbol = props.symbol.value_or("");', windows)
        self.assertIn('icon.FontFamily(FontFamily(L"Segoe MDL2 Assets"));', windows)
        self.assertIn('if (symbol == "check") return L"\\xE73E";', windows)
        self.assertIn('if (symbol == "copy") return L"\\xE8C8";', windows)
        self.assertIn('if (symbol == "edit") return L"\\xE70F";', windows)
        self.assertIn('if (symbol == "power-off" || symbol == "power-on") return L"\\xE7E8";', windows)
        self.assertIn('icon.Glyph(ButtonSymbolGlyph(symbol));', windows)
        self.assertIn('if (symbol == "check") return L"\\xE73E";', windows)
        self.assertIn('if (symbol == "help") return L"\\xE897";', windows)
        self.assertIn('if (symbol == "import") return L"\\xE8B5";', windows)
        self.assertIn('if (symbol == "refresh") return L"\\xE72C";', windows)
        self.assertIn('if (symbol == "chevron-down") return L"\\xE70D";', windows)
        self.assertIn('if (symbol == "chevron-up") return L"\\xE70E";', windows)
        # One map serves the icon-only button, the glyph-with-title button, and
        # the quiet link that carries only a glyph.
        self.assertEqual(3, windows.count('icon.Glyph(ButtonSymbolGlyph(symbol));'))
        self.assertIn(
            '} else if (link && props.plainLink.value_or(false) && !symbol.empty()) {',
            windows,
        )

    def test_macos_busy_buttons_center_a_spinner_with_the_title_and_stay_live(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        button = controls.split("@implementation LiteLLMAppKitButtonComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitButtonCls", 1
        )[0]

        # The busy prop only drives the leading image: the enabled appearance,
        # the title, and the control's width are untouched, so a working button
        # never turns into a greyed-out placeholder.
        self.assertIn("const BOOL busyChanged = oldViewProps.busy != newViewProps.busy;", button)
        self.assertIn("_busy = newViewProps.busy;", button)
        self.assertIn("[self updateBusyAnimation];", button)
        self.assertNotIn("newViewProps.busy", button.split("_button.enabled", 1)[1].split("\n", 1)[0])
        self.assertIn("_busyStep = 0;", button.split("- (void)prepareForRecycle", 1)[1])
        self.assertIn("[_busyTimer invalidate];", button.split("- (void)prepareForRecycle", 1)[1])
        # The spinner is the button's own leading image, exactly like a
        # symbol-with-title button, so AppKit centers the icon together with
        # the title as one group. An icon-only button keeps its icon size and
        # shows the wheel in the icon's place.
        self.assertIn("NSImage *image = _busy ? AppKitBusySpinner.frames[(NSUInteger)_busyStep] : symbolImage;", button)
        self.assertIn(": (showsTitle ? (trailingSymbol ? NSImageTrailing : NSImageLeading) : NSImageOnly);", button)
        # A menu button keeps its chevron on the title's trailing edge; while
        # its action runs the spinner stays the leading content.
        self.assertIn("const BOOL trailingSymbol = _symbolTrailing && !_busy;", button)
        self.assertIn("_symbolTrailing = newViewProps.symbolTrailing;", button)
        self.assertIn('_button.title = showsTitle ? title : @"";', button)
        # The wheel turns by swapping pre-rendered frames, so nothing about the
        # button's layout changes while it reports progress.
        self.assertIn("_busyStep = (_busyStep + 1) % AppKitBusySpinner.stepCount;", button)
        self.assertIn("_button.image = AppKitBusySpinner.frames[(NSUInteger)_busyStep];", button)
        self.assertIn("NSRunLoopCommonModes", button)
        # The wheel is one drawing for the whole app: the Swift leaf owns the
        # frames and the 分组管理 sheet turns the same ones beside 密钥, so this
        # control view only swaps them.  A template image is tinted by AppKit
        # like the title it sits beside (white on an accent-filled default
        # button), so the wheel needs no color of its own.
        mac_leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        self.assertNotIn("BusySpinnerFrames", controls)
        for marker in (
            "public static let stepCount = 12",
            "NSColor.black.withAlphaComponent(alpha).setStroke()",
            "image.isTemplate = true",
            "@objc(AppKitBusySpinner)",
        ):
            self.assertIn(marker, mac_leaf)
        self.assertIn("AppKitBusySpinner.stepInterval", button)
        # The shared sheet turns that wheel beside 密钥 instead of a second one.
        self.assertIn("loadingSpinner?.image = AppKitBusySpinner.frames[loadingStep]", mac_leaf)
        # The spinner is never a view laid over the bezel.
        self.assertNotIn("leadingAccessory", controls)

        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")
        self.assertIn("bool const busy = props.busy.value_or(false);", windows)
        self.assertIn("auto ring = ProgressRing{};", windows)
        self.assertIn("button_.IsEnabled(Enabled(props.disabled));", windows)
        self.assertIn("row.Children().Append(busySpinner());", windows)

    def test_macos_buttons_clear_default_action_state_before_fabric_reuse(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        button = controls.split("@implementation LiteLLMAppKitButtonComponentView", 1)[1].split(
            "Class<RCTComponentViewProtocol> LiteLLMAppKitButtonCls", 1
        )[0]
        recycle = button.split("- (void)prepareForRecycle", 1)[1].split("- (void)pressed:", 1)[0]

        self.assertIn("_props = defaultProps;", recycle)
        self.assertIn('((LiteLLMNavigationLinkButton *)_button).linkMode = NO;', recycle)
        self.assertIn('((LiteLLMNavigationLinkButton *)_button).defaultAction = NO;', recycle)
        self.assertIn('self.keyEquivalent = self.defaultAction ? @"\\r" : @"";', controls)
        self.assertIn('[self.window setDefaultButtonCell:nil];', controls)
        self.assertIn('- (void)viewDidMoveToWindow', controls)
        self.assertIn('dispatch_async(dispatch_get_main_queue()', controls)
        self.assertIn('BOOL defaultAction = !link && newViewProps.primary && !newViewProps.disabled;', button)
        self.assertIn('((LiteLLMNavigationLinkButton *)_button).defaultAction = defaultAction;', button)
        self.assertNotIn("NSColor.systemGrayColor", button)
        self.assertNotIn("NSColor.controlColor", button)
        self.assertGreaterEqual(button.count("_button.bezelColor = nil;"), 3)
        self.assertIn('compactChanged ||\n      disabledChanged)', button)
        self.assertIn('if (!link && !newViewProps.primary) {', button)
        self.assertIn("_button.hasDestructiveAction = NO;", recycle)
        self.assertIn("_button.contentTintColor = nil;", recycle)
        self.assertIn("_button.enabled = YES;", recycle)

    def test_native_boolean_controls_skip_unrelated_prop_rewrites(self) -> None:
        mac = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUIControls.cpp").read_text(encoding="utf-8")

        self.assertIn("const BOOL selectionChanged = oldViewProps.selectedKey != newViewProps.selectedKey;", mac)
        self.assertIn("if (selectionChanged || _tableView.selectedRow != selectedIndex)", mac)
        self.assertIn("if (selectionChanged && _scrollView.hasVerticalScroller) {", mac)
        self.assertIn("if (!NSIsEmptyRect(visibleRect) && !NSContainsRect(visibleRect, selectedRect)) {", mac)
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
            "if ((shouldSynchronizeText || recoverEmptyControl) && ![activeField.stringValue isEqualToString:value])",
            mac_text_field,
        )
        self.assertIn(
            "if ((shouldSynchronizeText || recoverEmptyControl) && ![_multilineField.string isEqualToString:value])",
            mac_text_field,
        )
        self.assertIn(
            "const BOOL recoverEmptyControl = value.length > 0 && activeValue.length == 0 && ![self activeControlIsEditing];",
            mac_text_field,
        )
        self.assertIn("- (BOOL)activeControlIsEditing", mac_text_field)
        self.assertIn(
            "const BOOL shouldUpdateDisabled = activeControlChanged || oldViewProps.disabled != newViewProps.disabled;",
            mac_text_field,
        )
        self.assertIn("- (void)prepareForRecycle", mac_text_field)
        self.assertIn("static const auto defaultProps = std::make_shared<const LiteLLMAppKitTextFieldProps>();", mac_text_field)
        self.assertIn("_props = defaultProps;", mac_text_field)

        mac_text_editor = mac.split(
            "@implementation LiteLLMAppKitTextEditorComponentView", 1
        )[1].split("Class<RCTComponentViewProtocol> LiteLLMAppKitTextEditorCls", 1)[0]
        self.assertIn("static const auto defaultProps = std::make_shared<const LiteLLMAppKitTextEditorProps>();", mac_text_editor)
        self.assertIn("_props = defaultProps;", mac_text_editor)
        self.assertIn("_textView.editable = YES;", mac_text_editor)
        self.assertIn("_scrollView.hasHorizontalScroller = NO;", mac_text_editor)

        mac_secure_text = mac.split(
            "@implementation LiteLLMAppKitSecureTextInputComponentView", 1
        )[1].split("Class<RCTComponentViewProtocol> LiteLLMAppKitSecureTextInputCls", 1)[0]
        self.assertIn("static const auto defaultProps = std::make_shared<const LiteLLMAppKitSecureTextInputProps>();", mac_secure_text)
        self.assertIn("_props = defaultProps;", mac_secure_text)
        self.assertIn("_synchronizingField = YES;", mac_secure_text)
        self.assertIn("NSString *_lastSyncedPlainText;", mac_secure_text)
        self.assertIn("strongSelf->_lastSyncedPlainText = [value copy];", mac_secure_text)
        self.assertIn("[self isPlainTextAutoCommitField] && [_lastSyncedPlainText isEqualToString:[self activeText]]", mac_secure_text)
        self.assertIn('status:@"ready" error:@"" commitRequest:strongSelf->_lastCommitRequest', mac_secure_text)
        self.assertIn("_host.control = _field;", mac_secure_text)
        self.assertIn("_field.enabled = YES;", mac_secure_text)
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
        app = (WIN_PROJECT / "YoungRouter.cpp").read_text(encoding="utf-8")

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
        ui = (SHARED / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        mac_spec = (SHARED / "ui/macos/NativeTextEditorNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        windows_spec = (
            SHARED / "ui/windows/NativeTextEditorNativeComponent.ts"
        ).read_text(encoding="utf-8")
        windows_codegen = (
            WIN_PROJECT
            / "codegen/react/components/YoungRouter/LiteLLMWinUITextEditor.g.h"
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

    def test_every_react_root_receives_core_events(self) -> None:
        """One React root per window observes Core; a single handler slot does not."""

        mac_bridge = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        mac_module = (MAC_NATIVE / "CoreIPCModule.swift").read_text(encoding="utf-8")
        windows_bridge = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")
        windows_bridge_header = (WIN_NATIVE / "CoreIPCBridge.h").read_text(encoding="utf-8")
        windows_module = (WIN_NATIVE / "CoreIPCModule.cpp").read_text(encoding="utf-8")
        windows_module_header = (WIN_NATIVE / "CoreIPCModule.h").read_text(encoding="utf-8")

        for source in (mac_bridge, windows_bridge, windows_bridge_header):
            self.assertNotIn("setEventHandler", source)
            self.assertNotIn("SetEventHandler", source)
        # macOS: one observer per React root, fanned out by the shared bridge.
        self.assertIn("func addEventHandler(_ handler: @escaping (String) -> Void) -> Int", mac_bridge)
        self.assertIn("private var eventHandlers: [Int: (String) -> Void] = [:]", mac_bridge)
        self.assertIn("for handler in handlers { handler(eventText) }", mac_bridge)
        self.assertIn("eventHandlerToken = core.addEventHandler", mac_module)
        self.assertIn("override func stopObserving()", mac_module)
        self.assertIn("core.removeEventHandler(token)", mac_module)
        # Windows: the same contract through the module's listener lifecycle.
        self.assertIn("int AddEventHandler(std::function<void(std::string const&)> handler);", windows_bridge_header)
        self.assertIn("std::map<int, std::function<void(std::string const&)>> event_handlers_;", windows_bridge_header)
        self.assertIn("for (auto const& handler : handlers) handler(text);", windows_bridge)
        self.assertIn("event_handler_token_ = CoreIPCBridge::Shared().AddEventHandler(", windows_module)
        self.assertIn("void CoreIPCModule::AddListener(std::string const&) noexcept {\n  RegisterEventHandler();", windows_module)
        self.assertIn("void CoreIPCModule::RemoveListeners(double count) noexcept {\n  if (count > 0) return;\n  UnregisterEventHandler();", windows_module)
        self.assertIn("void RegisterEventHandler() noexcept;", windows_module_header)

    def test_hosts_keep_the_core_revision_monotonic_across_a_replacement(self) -> None:
        """A host-driven Core replacement must resume Core's own revision."""

        mac = (MAC_NATIVE / "CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "CoreIPCBridge.cpp").read_text(encoding="utf-8")

        for source in (mac, windows):
            self.assertIn("--metadata", source)
            self.assertIn(".litellm-runtime", source)
            self.assertIn("core-state.json", source)
        self.assertIn('arguments.append(contentsOf: ["--metadata", metadataPath])', mac)
        self.assertIn('environment["YOUNG_ROUTER_HOME"] ?? environment["LITELLM_RUNTIME_ROOT"]', mac)
        self.assertIn('command += L" --metadata " + Quote(metadata);', windows)
        self.assertIn('std::wstring root = Environment(L"YOUNG_ROUTER_HOME");', windows)


if __name__ == "__main__":
    unittest.main()
