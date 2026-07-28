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
        self.assertIn("timeoutInterval: 5", mac)
        self.assertIn("shutdown_token, 5000", windows)

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
        self.assertIn("bool SetWindowContentSize(double width, double height);", header)
        self.assertIn("bool WinUI3NativeLeaf::SetWindowContentSize(double width, double height)", leaf)
        self.assertIn("std::isfinite(width)", leaf)
        self.assertIn("kMaximumContentExtent", leaf)
        self.assertIn("AdjustWindowRectExForDpi", leaf)
        self.assertIn('REACT_METHOD(SetWindowContentSize, L"setWindowContentSize")', module_header)
        self.assertIn("void SetWindowContentSize(", module_header)
        self.assertIn("void WinUI3NativeLeafModule::SetWindowContentSize(", module)
        self.assertIn("leaf->SetWindowContentSize(width, height)", module)

    def test_windows_window_minimums_follow_each_route_in_dpi_corrected_frame_pixels(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        header = (WIN_NATIVE / "WinUI3NativeLeaf.h").read_text(encoding="utf-8")

        self.assertIn("POINT MinimumTrackSizeForActiveRoute() const;", header)
        self.assertIn("ContentSize RouteMinimumContentSize(std::wstring_view route)", leaf)
        for route, width, height in (
            ("providers-models", 1052, 560),
            ("runtime-settings", 760, 500),
            ("configuration-package", 420, 132),
            ("webdav-settings", 680, 386),
            ("logs", 640, 420),
        ):
            self.assertIn(f'route == L"{route}") return {{{width}, {height}}};', leaf)
        self.assertIn('route == L"codex-settings" || route == L"claude-settings"', leaf)
        self.assertIn("return {1020, 620};", leaf)
        self.assertIn("MinimumTrackSizeForActiveRoute();", leaf)
        self.assertIn("DipToPhysicalPixels", leaf)
        self.assertIn("AdjustWindowRectExForDpi", leaf)
        self.assertNotIn("ptMinTrackSize.x = std::max<LONG>(minmax->ptMinTrackSize.x, 1020);", leaf)

    def test_windows_routes_restore_legacy_initial_content_sizes(self) -> None:
        leaf = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("ContentSize RouteInitialContentSize", leaf)
        for size in ("{1052, 600}", "{1120, 680}", "{1080, 620}", "{420, 208}", "{900, 580}"):
            self.assertIn(size, leaf)
        self.assertIn("RouteInitialContentSize(route)", leaf)

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
            self.assertIn(f'"{action}"', mac)

        # WinUI consumes the same NativeMenuAction list generically; it must
        # not duplicate the shared lifecycle identifier table.
        self.assertIn("DispatchAction(WideToUtf8(item.id));", windows)
        self.assertIn("AppendMenuW(menu, flags,", windows)

        self.assertIn('await ipc.dispatch({ type: `service.${operation}` });', ui)
        self.assertIn("return await refreshSnapshot();", ui)
        self.assertIn('if (snapshot.service.state === "stopped") void runServiceOperation("start");', ui)
        self.assertIn('if (operation === "stop") serviceShouldBeRunning.current = false;', ui)
        self.assertIn("const SERVICE_HEALTH_POLL_MS = 10_000;", ui)
        self.assertIn("const SERVICE_RECOVERY_RETRY_MS = 15_000;", ui)
        self.assertIn('runServiceOperation("health", true)', ui)
        self.assertIn('void runServiceOperation("start");', ui)

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
        self.assertIn('emitAction("open-logs?tab=', leaf)
        self.assertNotIn("config-watch", app_delegate)
        self.assertNotIn("config-watch", leaf)
        self.assertNotIn('language-settings', app_delegate)
        self.assertNotIn('language-settings', leaf)

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
        self.assertIn("item.state = choice.checked ? .on : .off", mac)
        self.assertIn("CreatePopupMenu", windows)
        self.assertNotIn('"language-settings"', core_service)
        self.assertNotIn('"language_settings"', core_service)

    def test_macos_physical_close_is_approved_by_shared_react_state(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")

        self.assertIn("NSWindowDelegate", leaf)
        self.assertIn("func windowShouldClose(_ sender: NSWindow) -> Bool", leaf)
        self.assertIn("approvedCloseInProgress", leaf)
        self.assertIn('emitAction("request-close-\\(route)")', leaf)
        self.assertIn("@objc private func closeFromShortcut() { requestClose(route: activeRoute) }", leaf)
        self.assertIn("request-close-", ui)
        self.assertIn("onPress={requestClose}", ui)

    def test_macos_content_size_bridge_resizes_the_existing_react_window(self) -> None:
        leaf = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        module = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        bridge = (MAC_NATIVE / "AppKitNativeLeafBridge.m").read_text(encoding="utf-8")

        self.assertIn("func setWindowContentSize(width: Double, height: Double) -> Bool", leaf)
        self.assertIn("width.isFinite", leaf)
        self.assertIn("height.isFinite", leaf)
        self.assertIn("let maximumContentExtent = 8_192.0", leaf)
        self.assertIn("let window = hostWindow ?? reactHostWindow()", leaf)
        self.assertIn("window.setContentSize(NSSize(width: width, height: height))", leaf)
        self.assertIn("@objc(setWindowContentSize:height:resolver:rejecter:)", module)
        self.assertIn(
            "resolve(leaf.setWindowContentSize(width: width.doubleValue, height: height.doubleValue))",
            module,
        )
        self.assertIn(
            "RCT_EXTERN_METHOD(setWindowContentSize:(nonnull NSNumber *)width height:(nonnull NSNumber *)height",
            bridge,
        )

    def test_windows_secure_editor_is_composed_from_winui3_controls(self) -> None:
        source = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("auto editor = CreateTextEditor();", source)
        self.assertIn("controls::TextBox", source)
        self.assertIn("auto split = CreateSplitView();", source)
        self.assertIn("auto selector = CreateSelector();", source)
        self.assertIn("controls::MenuBar", source)
        self.assertNotIn('L"EDIT"', source)
        self.assertNotIn("EditorWindowProc", source)

    def test_localization_crosses_the_native_leaf_contract(self) -> None:
        types = (SHARED / "types.ts").read_text(encoding="utf-8")
        ui = (SHARED / "ui/LiteLLMMenuApp.tsx").read_text(encoding="utf-8")
        mac = (MAC_NATIVE / "AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        self.assertIn("interface NativeLocalization", types)
        self.assertIn("setLocalization(strings: NativeLocalization)", types)
        self.assertIn("native.setLocalization({", ui)
        self.assertIn('translate("common.find")', ui)
        self.assertIn("func setLocalization", mac)
        self.assertIn("void WinUI3NativeLeaf::SetLocalization", windows)

    def test_native_editor_errors_reject_instead_of_masquerading_as_cancel(self) -> None:
        mac = (MAC_NATIVE / "AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        windows = (WIN_NATIVE / "WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")

        self.assertIn("E_NATIVE_EDITOR_READ", mac)
        self.assertIn("E_NATIVE_EDITOR_STAGE", mac)
        self.assertIn('promise.Reject("The local Core could not read the document.")', windows)
        self.assertIn('promise.Reject("The local Core could not stage the document.")', windows)

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
        self.assertIn('modelChooserButton(title: "All"', mac_leaf)
        self.assertIn('modelChooserButton(title: "Invert"', mac_leaf)
        self.assertIn('modelChooserButton(title: "+"', mac_leaf)
        self.assertIn("NSButton(checkboxWithTitle:", mac_leaf)
        self.assertNotIn("let checkbox = NSBezierPath", mac_leaf)
        self.assertIn("@objc func chooseModelsToAdd(_ models: [String]", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(chooseModelsToAdd:", mac_bridge)

        self.assertIn("std::optional<std::vector<std::wstring>> WinUI3NativeLeaf::ChooseModelsToAdd(", windows_leaf)
        self.assertIn("xaml::Window dialog;", windows_leaf)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_", windows_leaf)
        self.assertNotIn("XamlUIService", windows_module)
        self.assertIn('all.Content(winrt::box_value(L"All"))', windows_leaf)
        self.assertIn('invert.Content(winrt::box_value(L"Invert"))', windows_leaf)
        self.assertIn("state->add.IsEnabled(selected > 0);", windows_leaf)
        self.assertIn('REACT_METHOD(ChooseModelsToAdd, L"chooseModelsToAdd")', windows_header)
        self.assertIn("void WinUI3NativeLeafModule::ChooseModelsToAdd(", windows_module)

        chooser_section = windows_module.split("void WinUI3NativeLeafModule::ChooseModelsToAdd(", 1)[1].split(
            "void WinUI3NativeLeafModule::EditSecureDocument(", 1
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
            self.assertNotIn("onChangeText", spec)
            self.assertIn("onSecretState", spec)
        self.assertIn("NSSecureTextField *_field", mac)
        self.assertIn("stageSecretForDomain", mac)
        self.assertIn("stageSecretForDomain(", mac_core)
        self.assertIn("PasswordBox password_box_", windows)
        self.assertIn("CreateSecretCapability(", windows)
        self.assertIn("StageSecret(capability->token, secret, false)", windows)
        self.assertNotIn("onChangeText", windows_codegen)
        self.assertIn("OnSecretState", windows_codegen)
        self.assertIn("NativeSecretInputControl", ui)
        self.assertNotIn('onEdit={() => stageSecret({ domain: "runtime"', ui)

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
            "NSSwitch",
        ):
            self.assertIn(native_class, controls)

    def test_macos_single_line_controls_and_table_cells_are_vertically_centered(self) -> None:
        controls = (MAC_NATIVE / "AppKitControlViews.mm").read_text(encoding="utf-8")

        self.assertIn("@interface LiteLLMAppKitControlHostView : NSView", controls)
        self.assertIn("NSMidY(bounds) - height / 2.0", controls)
        self.assertIn("control.intrinsicContentSize.height", controls)
        self.assertIn("[_activeControl isKindOfClass:NSScrollView.class]", controls)
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
        self.assertIn("label.font = [NSFont systemFontOfSize:13];", controls)
        self.assertIn("constraintEqualToAnchor:cell.leadingAnchor constant:8", controls)
        self.assertIn("constraintEqualToAnchor:cell.trailingAnchor constant:-8", controls)
        self.assertIn("constraintEqualToAnchor:cell.centerYAnchor", controls)

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
            "ToggleSwitch",
        ):
            self.assertIn(native_class, controls)
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
        self.assertIn('documentKey={`logs:${selected}`}', ui)
        self.assertIn("REACT_FIELD(documentKey)", windows_codegen)


if __name__ == "__main__":
    unittest.main()
