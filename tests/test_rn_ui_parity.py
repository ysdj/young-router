from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE = ROOT / "rn/packages/shared/src/ui/LiteLLMMenuApp.tsx"
NATIVE_CONTROLS = ROOT / "rn/packages/shared/src/ui/NativeControls.tsx"
MACOS_LEAF = ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift"
MACOS_PROJECT = ROOT / "rn/apps/macos/macos/LiteLLMMenu.xcodeproj/project.pbxproj"
PLATFORM_ENTRY = ROOT / "rn/packages/shared/src/platformEntry.ts"


class ReactNativeUiParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = UI_SOURCE.read_text(encoding="utf-8")
        cls.native_controls = NATIVE_CONTROLS.read_text(encoding="utf-8")
        cls.macos_leaf = MACOS_LEAF.read_text(encoding="utf-8")
        cls.macos_project = MACOS_PROJECT.read_text(encoding="utf-8")
        cls.platform_entry = PLATFORM_ENTRY.read_text(encoding="utf-8")

    def assert_ui_has(self, marker: str) -> None:
        self.assertIn(marker, self.ui, marker)

    def test_menu_bar_home_is_not_a_dashboard_or_sidebar_shell(self) -> None:
        """The legacy app opens settings from its status menu, not a home dashboard."""
        self.assert_ui_has('route === "home" ? <View style={styles.menuBarHost} />')
        for removed_shell in (
            "function Home(",
            "function NavigationItem(",
            "styles.navigation",
            "styles.cards",
            "styles.statusCard",
        ):
            self.assertNotIn(removed_shell, self.ui, removed_shell)

    def test_bootstrap_menu_uses_the_system_language_before_core_snapshot(self) -> None:
        self.assertIn('const bootstrapTranslate = createTranslator("system", systemLocale);', self.platform_entry)
        self.assertIn('title: bootstrapTranslate(key)', self.platform_entry)

    def test_route_window_specs_preserve_legacy_default_and_minimum_sizes(self) -> None:
        self.assert_ui_has("const legacyWindowSpecs = {")
        expected_specs = {
            "providers-models": (1052, 600, 1052, 560),
            "codex-settings": (1120, 680, 1020, 620),
            "runtime-settings": (1080, 620, 760, 500),
            "webdav-settings": (680, 386, 680, 386),
            "logs": (900, 580, 640, 420),
        }
        for route, (width, height, min_width, min_height) in expected_specs.items():
            self.assertRegex(
                self.ui,
                rf'"{re.escape(route)}": \{{\s*width: {width},\s*height: {height},\s*'
                rf"minWidth: {min_width},\s*minHeight: {min_height}\s*\}}",
            )

    def test_providers_workspace_keeps_legacy_segmented_three_pane_structure(self) -> None:
        self.assert_ui_has("function LegacyProviderWorkspace(")
        self.assert_ui_has('id: "providers", title: translate("providers.providers")')
        self.assert_ui_has('id: "routes", title: translate("providers.routes")')
        self.assert_ui_has("function LegacyTablePane(")
        for marker in (
            "legacyProvidersLayout:",
            "providerWorkspace:",
            "providerThreePane:",
            "providerListPane:",
            "modelListPane:",
            "providerInspectorPane:",
            "tableHeader:",
            "tableScroll:",
        ):
            self.assert_ui_has(marker)
        self.assert_ui_has("width: 196")
        self.assert_ui_has("providerInspector: { width: 340, minWidth: 340, maxWidth: 340")

    def test_native_tables_match_legacy_route_only_alternating_rows(self) -> None:
        mac_table_spec = (
            ROOT / "rn/packages/shared/src/ui/macos/NativeTableNativeComponent.ts"
        ).read_text(encoding="utf-8")
        windows_table_spec = (
            ROOT / "rn/packages/shared/src/ui/windows/NativeTableNativeComponent.ts"
        ).read_text(encoding="utf-8")
        mac_native = (
            ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
        ).read_text(encoding="utf-8")
        windows_native = (
            ROOT / "rn/apps/windows/src/native/windows/WinUIControls.cpp"
        ).read_text(encoding="utf-8")

        for spec in (mac_table_spec, windows_table_spec):
            self.assertIn("alternatingRows?: WithDefault<boolean, false>;", spec)
        self.assertIn("alternatingRows = false", self.native_controls)
        # The fetched-model picker is now a native modal leaf, so its old
        # React table no longer belongs to the shared window tree.
        self.assertEqual(self.ui.count("<NativeTable"), 7)
        self.assertEqual(self.ui.count("alternatingRows"), 1)
        self.assertIn("selectedKey={selectedRoute ?? \"\"} alternatingRows", self.ui)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", mac_native)
        self.assertIn(
            "_tableView.usesAlternatingRowBackgroundColors = newViewProps.alternatingRows;",
            mac_native,
        )
        self.assertIn("props.alternatingRows.value_or(false)", windows_native)
        self.assertIn("AlternatingRowBrush()", windows_native)

    def test_legacy_window_footers_keep_close_and_apply_actions(self) -> None:
        self.assert_ui_has("function LegacyDialogFooter(")
        self.assert_ui_has('title={translate("menu.close")}')
        self.assert_ui_has('title={translate("menu.apply")}')

    def test_shared_ui_uses_the_cross_platform_native_control_contract(self) -> None:
        page_controls = (
            "NativeButton",
            "NativeSegmentedControl",
            "NativeTextField",
            "NativeCheckbox",
            "NativePicker",
        )
        for control in page_controls:
            self.assertIn(control, self.ui)

        adapter_controls = (*page_controls, "NativeToggle", "NativeSelectableRow")
        for control in adapter_controls:
            self.assertIn(f"export function {control}", self.native_controls)

        # Semantic system colors may select a platform brush here; native
        # component registration and implementation must remain in the adapter.
        self.assertNotIn("requireNativeComponent", self.ui)
        self.assertNotIn('from "./AppKitControls"', self.ui)
        self.assertNotIn('from "./windows/', self.ui)

    def test_settings_workspaces_keep_their_legacy_layout_roots(self) -> None:
        expected_components = {
            "LegacyCodexWorkspace": ("legacyCodexWorkspace:", "codexRawPane:"),
            "LegacyRuntimeWorkspace": ("legacyRuntimeWorkspace:", "runtimeScrollSurface:"),
            "LegacyWebDavWorkspace": ("legacyWebDavForm:", "webdavFooterLeading:"),
            "LegacyLogsWorkspace": (
                "legacyLogsWindow:",
                "legacyLogsToolbar:",
                "legacyLogsTabs:",
                "legacyLogRecords:",
            ),
        }
        for component, markers in expected_components.items():
            self.assert_ui_has(f"function {component}(")
            for marker in markers:
                self.assert_ui_has(marker)
        self.assert_ui_has("legacyPackageDialog:")

    def test_codex_and_claude_share_the_settings_workspace_geometry(self) -> None:
        codex = self.ui.split("function LegacyCodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function RuntimeField", 1
        )[0]
        for screen in (codex, claude):
            self.assertIn("const [structuredWidth, setStructuredWidth] = useState(470);", screen)
            self.assertIn("<SettingsWorkspace", screen)
            self.assertIn("structuredWidth={structuredWidth}", screen)
        self.assertIn("minPaneWidth={420}", self.ui)
        self.assertIn("maxStructuredWidth = workspaceWidth > 0 ? Math.min(680", self.ui)
        self.assertIn("const paneWidth = Math.min(structuredWidth, maxStructuredWidth);", self.ui)
        self.assertIn("paneWidth={paneWidth}", self.ui)
        self.assertIn("paneWidth={paneWidth}", self.ui)

    def test_runtime_uses_core_projection_kinds_and_legacy_adaptive_layout(self) -> None:
        for marker in (
            'kind === "toggle"',
            'kind === "choice"',
            'storageKind',
            'contentWidth < 960',
            'runtimeOneColumnForm:',
            'title={item.will_clear === true ? "Will Clear"',
            'clearSecret({ domain: "runtime", field: "setting", target: key })',
        ):
            self.assert_ui_has(marker)

    def test_sensitive_settings_use_inline_native_password_controls(self) -> None:
        for marker in (
            "function NativeSecretInputControl(",
            "<NativeSecureTextInput domain={domain}",
            'domain="runtime" field="setting"',
            'domain="webdav" field="password"',
            'domain="claude" field="deployment_token"',
            'domain="codex" field="api_key"',
            'domain="providers_models" field="api_key"',
            'placeholder={hint ?? ""}',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("translateSecretAction", self.ui)
        self.assertNotIn("function NativeSecretField({ label, hint, busy, onEdit", self.ui)
        self.assertIn("hint && actionsBelow ? <Text", self.ui)

    def test_claude_settings_hydrates_the_public_gateway_and_localizes_permission_modes(self) -> None:
        for marker in (
            "const configuredGatewayUrl = stringValue(settings.gateway_url);",
            "setDeployment({ model: configuredModel, base_url: configuredGatewayUrl });",
            'translate("settings.claudeUnavailable")',
            'translate("claude.permission.default")',
            'translate("claude.permission.acceptEdits")',
            'translate("claude.permission.bypassPermissions")',
        ):
            self.assert_ui_has(marker)

    def test_runtime_form_rows_match_the_legacy_appkit_alignment_grid(self) -> None:
        for marker in (
            "runtimeInputRow:",
            "runtimeFieldLabel: { width: 150",
            "runtimeValueSlot: { width: 180",
            "runtimeUnit: { width: 60",
            "runtimeActionSlot: { width: 72",
            'runtimeInputRow: { height: 26, flexDirection: "row", alignItems: "center", gap: 8 }',
            "runtimeHelpSlot: { marginLeft: 158, paddingTop: 4 }",
            "runtimeHelpText:",
            "<NativeCheckbox label=\"\"",
            "<NativePicker labels={stringList(item.options)}",
            "<RuntimeValueField label={label}",
        ):
            self.assert_ui_has(marker)

        self.assertNotIn('label={`${label}${stringValue(item.unit)', self.ui)
        self.assertIn('Saving these runtime defaults applies them to the LiteLLM service.', self.ui)
        self.assertIn('title="Restore Defaults…"', self.ui)
        self.assertIn('route === "runtime-settings" ? "Save & Apply"', self.ui)

    def test_provider_billing_refresh_follows_runtime_interval_without_dirtying_draft(self) -> None:
        for marker in (
            'LITELLM_MENU_BALANCE_REFRESH_MINUTES',
            'providers.refresh_billing',
            'billingRefreshMinutes > 0',
            'Live billing is optional and must not disturb the editable draft.',
        ):
            self.assert_ui_has(marker)

    def test_model_inspector_matches_legacy_breadcrumb_and_compact_billing_surface(self) -> None:
        for marker in (
            'NativeButton title={providerLabel} link',
            'billingBalanceValue(model.billing)',
            'billingUsageValue(model.usage)',
            'billingMultiplierValue(model.multiplier)',
            'function billingToolTip(',
        ):
            self.assert_ui_has(marker)

    def test_fetched_models_use_the_native_legacy_chooser_instead_of_a_react_modal(self) -> None:
        mac_module = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeafModule.swift").read_text(
            encoding="utf-8"
        )
        windows_leaf = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(
            encoding="utf-8"
        )
        self.assert_ui_has("native.chooseModelsToAdd({ models: candidates, providerName, keyName })")
        self.assert_ui_has("candidateSet.has(model)")
        self.assertNotIn("Modal, Platform", self.ui)
        self.assertNotIn("fetchedModalBackdrop", self.ui)
        self.assertNotIn("function FetchedModelsDialog", self.ui)
        for marker in (
            'panel.title = "Choose Models to Add"',
            'panel.minSize = NSSize(width: 520, height: 340)',
            'let contentWidth: CGFloat = 620',
            'let rowHeight: CGFloat = 28',
            'let selectAllButton = modelChooserButton(title: "All"',
            'let invertButton = modelChooserButton(title: "Invert"',
            'let addButton = modelChooserButton(title: "+"',
            'NSButton(checkboxWithTitle: rows[rowIndex].title',
        ):
            self.assertIn(marker, self.macos_leaf)
        self.assertIn("models.count <= 10_000", mac_module)
        self.assertIn("xaml::Window dialog;", windows_leaf)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_", windows_leaf)
        self.assertIn("list.MinHeight(220);", windows_leaf)
        self.assertIn("list.Height(420);", windows_leaf)

    def test_provider_inspector_keeps_the_legacy_provider_form_and_return_link(self) -> None:
        """The AppKit editor uses a 96pt, left-aligned provider form and source-model return link."""
        for marker in (
            'const [providerSourceModel, setProviderSourceModel] = useState<string>();',
            'label={translate("providers.baseUrl")} labelWidth={96} labelAlign="left"',
            'label={translate("providers.providerName")} labelWidth={96} labelAlign="left"',
            'label={translate("providers.label")} labelWidth={48} labelAlign="left"',
            'title={translate("providers.backToModel", { model: sourceModelLabel })} link',
            'providerEditorHeader:',
            'providerEditorSection:',
            'providerKeysHeading:',
        ):
            self.assert_ui_has(marker)

    def test_provider_secrets_and_logs_keep_the_legacy_narrow_window_geometry(self) -> None:
        for marker in (
            'actionsBelow label={translate("common.apiKey")}',
            "providerKeyFields: { flex: 1, minWidth: 170",
            "secretFieldButtons:",
            "legacyLogsContent: { paddingHorizontal: 8, paddingTop: 8",
            "logFilterInput: { flex: 1, minWidth: 220, maxWidth: 360",
            'legacyLogsTabs: { width: "100%", minWidth: 0',
            "logInfoBar: { height: 27, minHeight: 27",
        ):
            self.assert_ui_has(marker)

    def test_webdav_form_matches_the_legacy_fixed_native_dialog(self) -> None:
        for marker in (
            'label="URL"',
            'label="Remote File"',
            'label="Sync Every"',
            'label="HTTP Timeout"',
            "function LegacyWebDavPasswordField(",
            'placeholder={configured ? "leave blank to keep current password" : "optional"}',
            "webdavWideControl: { width: 510, flex: 0 }",
            "webdavPasswordInput: { width: 510, height: 24 }",
            "legacyWideButton: { minWidth: 92 }",
        ):
            self.assert_ui_has(marker)
        self.assertNotIn(
            '<NativeSecretField label={translate("webdav.password")}',
            self.ui,
        )

    def test_codex_raw_editors_share_height_and_reload_from_the_footer(self) -> None:
        for marker in (
            "const [structuredWidth, setStructuredWidth] = useState(470);",
            "showReload={false} codexPane style={styles.codexRawEditor}",
            "codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0",
            "codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0",
            'title={translate("settings.reloadFromDisk")}',
            "const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);",
            'if (domain === "codex" || domain === "claude") setSettingsRawReloadToken((current) => current + 1);',
            "reloadToken={rawReloadToken}",
            "reloadNonce, reloadToken, translate",
        ):
            self.assert_ui_has(marker)

    def test_model_breadcrumb_uses_the_shared_native_link_button_contract(self) -> None:
        mac_button = (ROOT / "rn/packages/shared/src/ui/macos/NativeButtonNativeComponent.ts").read_text(
            encoding="utf-8"
        )
        windows_button = (
            ROOT / "rn/packages/shared/src/ui/windows/NativeButtonNativeComponent.ts"
        ).read_text(encoding="utf-8")
        mac_native = (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(
            encoding="utf-8"
        )
        windows_native = (ROOT / "rn/apps/windows/src/native/windows/WinUIControls.cpp").read_text(
            encoding="utf-8"
        )

        for spec in (mac_button, windows_button):
            self.assertIn("link?: WithDefault<boolean, false>;", spec)
        self.assertIn("NSBezelStyleInline", mac_native)
        self.assertIn("HyperlinkButton", windows_native)

    def test_package_mode_reflows_native_window_to_legacy_heights(self) -> None:
        self.assert_ui_has('native.window.setContentSize?.(420, mode === "import" ? 132 : 208)')

    def test_codex_and_webdav_keep_legacy_visible_labels(self) -> None:
        self.assert_ui_has('>{translate("settings.structured")}</Text>')
        self.assert_ui_has('title="Test"')
        self.assert_ui_has('webdavFooterLeading')
        self.assert_ui_has(
            'Syncs the current LiteLLM Menu config, including provider keys and model routes.'
        )

    def test_macos_leaf_localizes_window_titles_and_keeps_status_menu_order(self) -> None:
        for title in (
            'case "providers-models": return "LiteLLM " + localized("routeProvidersModels", fallback: "Providers & Models")',
            'case "webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Settings")',
            'case "logs": return "LiteLLM " + localized("routeLogs", fallback: "Logs")',
        ):
            self.assertIn(title, self.macos_leaf, title)
        self.assertIn("private static let legacyStatusMenuOrder", self.macos_leaf)
        for ordered_item in (
            '"toggle-autostart", "separator"',
            '"open-providers-models", "open-runtime-settings", "open-codex-settings", "open-configuration-package", "separator"',
            '"webdav-status", "webdav-toggle", "open-webdav-settings", "separator"',
            '"open-recovery", "open-logs", "separator"',
            '"show-version", "quit"',
        ):
            self.assertIn(ordered_item, self.macos_leaf, ordered_item)

    def test_language_uses_the_native_status_menu_without_a_dedicated_screen(self) -> None:
        for marker in (
            '{ id: "language-menu", title: translate("menu.language"), enabled: true }',
            'id: "set-language-system"',
            'id: "set-language-en"',
            'id: "set-language-zh-Hans"',
            'await ipc.apply("language", staged.revision)',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("language-settings", self.ui)
        self.assertNotIn("function LanguageScreen", self.ui)
        self.assertIn("private func addLanguageMenu(", self.macos_leaf)
        self.assertIn("item.state = choice.checked ? .on : .off", self.macos_leaf)

    def test_service_lifecycle_stays_in_the_shared_menu_surface(self) -> None:
        """Lifecycle labels and state-specific enablement are shared by AppKit and WinUI."""
        for marker in (
            'const serviceState = snapshot.service.state;',
            'const serviceStartAvailable = !serviceOperationPending && serviceState === "stopped";',
            'const serviceRestartAvailable = !serviceOperationPending && serviceState !== "unknown" && serviceState !== "starting";',
            'const serviceReloadAvailable = !serviceOperationPending && (serviceState === "running" || serviceState === "unhealthy");',
            '{ id: "service-start", title: translate("service.start"), enabled: serviceStartAvailable },',
            '{ id: "service-stop", title: translate("service.stop"), enabled: !serviceOperationPending && serviceActive },',
            '{ id: "service-restart", title: translate("service.restart"), enabled: serviceRestartAvailable },',
            '{ id: "service-reload", title: translate("service.reload"), enabled: serviceReloadAvailable },',
            '{ id: "service-health", title: translate("service.health"), enabled: !serviceOperationPending },',
        ):
            self.assert_ui_has(marker)

    def test_macos_project_has_no_separate_legacy_route_windows(self) -> None:
        """React owns the route windows; AppKit is limited to native leaf controls."""
        legacy_sources = (
            "AppKitLegacyAuxiliaryWindows.swift",
            "NativeProvidersWindow.swift",
        )
        for source in legacy_sources:
            self.assertFalse((ROOT / "rn/apps/macos/src/native/macos" / source).exists(), source)
            self.assertNotIn(source, self.macos_project, source)


if __name__ == "__main__":
    unittest.main()
