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
RELAY_MANAGER = ROOT / "rn/packages/shared/src/ui/RelayAccountManager.tsx"
RELAY_ORIGIN = ROOT / "rn/packages/shared/src/ui/relayOrigin.ts"
TYPOGRAPHY = ROOT / "rn/packages/shared/src/ui/typography.ts"
MACOS_CONTROLS = ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
WINDOWS_CONTROLS = ROOT / "rn/apps/windows/src/native/windows/WinUIControls.cpp"
WINDOWS_LEAF = ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp"
WINDOWS_RELAY = ROOT / "rn/apps/windows/src/native/windows/WindowsRelayLogin.cpp"
ZH_HANS = ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts"
EN = ROOT / "rn/packages/shared/src/i18n/en.ts"


class ReactNativeUiParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = UI_SOURCE.read_text(encoding="utf-8")
        cls.native_controls = NATIVE_CONTROLS.read_text(encoding="utf-8")
        cls.macos_leaf = MACOS_LEAF.read_text(encoding="utf-8")
        cls.macos_project = MACOS_PROJECT.read_text(encoding="utf-8")
        cls.platform_entry = PLATFORM_ENTRY.read_text(encoding="utf-8")
        cls.zh = ZH_HANS.read_text(encoding="utf-8")
        cls.en = EN.read_text(encoding="utf-8")

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
        self.assertIn('["logs", "menu.logs"]', self.platform_entry)
        self.assertNotIn('["claude-settings", "menu.claude"]', self.platform_entry)

    def test_status_menu_uses_one_localized_recovery_logs_action(self) -> None:
        self.assertIn('function recoveryLogMenuTitle(', self.ui)
        self.assertIn('translate("menu.logsSummary", { recovering, cooldown })', self.ui)
        self.assertIn('autoStart: translate("menu.autoStart")', self.ui)
        self.assertIn('{ id: "open-logs", title: recoveryLogMenuTitle(snapshot.service, translate), enabled: true },', self.ui)
        self.assertNotIn('{ id: "open-claude-settings", title: translate("menu.claude"), enabled: true },', self.ui)
        self.assertNotIn('{ id: "open-recovery", title: translate("menu.recovery"), enabled: true },', self.ui)
        self.assertIn('checked: snapshot.service.auto_start_state === "enabled"', self.ui)
        self.assertNotIn('{ id: "webdav-toggle",', self.ui)
        self.assertNotIn('action === "webdav-toggle"', self.ui)
        self.assertIn('{ id: "webdav-status", title: `${translate("webdav.label")}: ${webdavMenuStatus(snapshot.service.webdav, snapshot.webdav.enabled, translate)}`, enabled: false },', self.ui)
        self.assertEqual(1, self.ui.count('{ id: "open-relay-accounts", title: translate("menu.relay"), enabled: true },'))

    def test_status_title_includes_a_valid_running_port_only(self) -> None:
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")

        self.assertIn("port?: number;", types)
        self.assertIn("serviceRunningOnPort: string;", types)
        self.assertIn('status.state === "running" && typeof status.port === "number"', self.platform_entry)
        self.assertIn("Number.isInteger(status.port) && status.port >= 1 && status.port <= 65535", self.platform_entry)
        self.assertIn('serviceRunningOnPort ?? "Running (port {port})"', self.platform_entry)
        self.assertIn('"service.runningOnPort": "Running (port {port})"', english)
        self.assertIn('"service.runningOnPort": "运行中 (端口 {port})"', chinese)

    def test_non_sentence_chinese_ui_copy_uses_ascii_punctuation(self) -> None:
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for marker in (
            '"menu.logsSummary": "日志 (路由恢复 {recovering}, 冷却 {cooldown})"',
            '"claude.permission.unknown": "其他 ({value})"',
            '"logs.duration": "耗时 (毫秒)"',
            '"service.status": "状态: {status}"',
            '"common.empty": "(空)"',
            '"providers.probeApplyTitle": "使用推荐协议?"',
            '"settings.rawLiveDraft": "原始文件 - 实时草稿"',
            '"relay.typeDetected": "已识别: {type}"',
        ):
            self.assertIn(marker, chinese)
        self.assertIn('{translate("providers.provider")}: {providerLabel}', self.ui)
        self.assertIn('{translate("common.default")}: {defaultValue}', self.ui)
        self.assertIn('return details.join(" | ");', self.ui)

    def test_assistant_settings_use_one_tabbed_surface_with_active_domain_actions(self) -> None:
        for marker in (
            'type AssistantSettingsDomain = "codex" | "claude";',
            'const settingsRoute = isAssistantSettingsRoute(route);',
            'const domain = settingsRoute ? settingsTab : domainForRoute(route);',
            'title: "Codex" }, { id: "claude", title: "Claude"',
            'selected={settingsTab} disabled={busy}',
            'await flushPendingFields();',
            'const dirtyDomains = settingsRoute',
            '? (["codex", "claude"] as const).filter((name) => current?.drafts[name]?.dirty)',
            'for (const name of dirtyDomains) {',
            "const claudeDeploymentDraftRef = useRef<ClaudeDeploymentDraft | undefined>(undefined);",
            "const hasClaudeDeploymentChanges = (currentSnapshot: CoreSnapshot | undefined)",
            'const needsDiscardConfirmation = hasPendingFieldEdits()',
            'if (!needsDiscardConfirmation) {',
            "claudeDeploymentDraftRef.current = next;",
            'if (reloadDomain === "claude") {',
            '}, [route]);',
        ):
            self.assert_ui_has(marker)

    def test_compatibility_claude_route_reuses_the_combined_native_window(self) -> None:
        for marker in (
            'function nativeWindowRoute(route: AppRoute): AppRoute',
            'return route === "claude-settings" ? "codex-settings" : route;',
            'const windowRoute = nativeWindowRoute(routeRequest);',
            'native.window.open(windowRoute);',
            'native.window.focus(windowRoute);',
            'native.window.close(nativeWindowRoute(route))',
        ):
            self.assert_ui_has(marker)

    def test_desktop_route_actions_reopen_the_same_route_and_reset_bare_logs(self) -> None:
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn("const [routeRequestSequence, setRouteRequestSequence] = useState(0);", bootstrap)
        self.assertIn("setRouteRequestSequence((current) => current + 1);", bootstrap)
        self.assertIn('setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");', bootstrap)
        self.assertIn("routeRequestSequence?: number;", self.ui)
        self.assertIn("[isPrimaryHost, isWindowManagerHost, native, routeRequest, routeRequestSequence]", self.ui)

    def test_shared_snapshot_refresh_does_not_reset_native_window_geometry(self) -> None:
        self.assertNotIn("const windowSpecs = {", self.ui)
        self.assertNotIn("native.window.setContentSize", self.ui)
        self.assertIn("}, [isPrimaryHost, native, snapshotLanguage, translate]);", self.ui)

    def test_provider_probe_applies_a_changed_recommendation_without_locking_the_workspace(self) -> None:
        for marker in (
            'title={probing ? translate("providers.probing") : translate("providers.probe")}',
            'function modelProbePresentation(',
            'translate("providers.probeSummaryAvailable"',
            'tooltip={probeTooltip}',
            'screenBoundedTooltipText(probePresentation.full, Dimensions.get("screen"))',
            'translate("providers.probeOriginalRequest"',
            'const applyProbedSurface: ApplyProbedSurface = (providerId, modelId, nextSurface, options) => {',
            'const currentSurface = stringValue(currentModel.upstream_url_surface, "openai/responses");',
            'if (currentSurface === nextSurface) return;',
            'await enqueueDispatch("model.patch", {',
            'await ipc.apply("providers_models", staged.revision, confirmations);',
            'upstream_url_surface: nextSurface,',
            'const nextSurface = stringValue(result.recommended_surface);',
            'isProbeSurface(nextSurface)',
            'confirmRecommendation: false',
            'model: { name: upstreamModel, upstream_model: upstreamModel, api_key_name: fetchKeyName, enabled: true, order:',
            '<ProtocolPicker providerId={providerId}',
            'function ProtocolPicker(',
            'const mode = stringValue(model.upstream_protocol_mode, "fallback");',
            'PickerField label={translate("providers.protocolMode")}',
            'value={fixed ? "fixed" : "fallback"}',
            'label={fixed ? translate("providers.fixedProtocol") : translate("providers.fallbackProtocol")}',
            'translate(fixed ? "providers.protocolModeFixedHint" : "providers.protocolModeFallbackHint")',
            'label={translate("common.enable")}',
            'changes: { model_enabled }',
        ):
            self.assert_ui_has(marker)

    def test_unprobed_models_do_not_render_a_status_placeholder(self) -> None:
        self.assert_ui_has('return { compact: "", full: "" };')
        self.assert_ui_has('{probePresentation.compact ? <TooltipText')
        self.assertNotIn(
            '<TooltipText numberOfLines={2} tooltip={probeTooltip} accessibilityHint={probePresentation.full} style={styles.probeSummary}>{probePresentation.compact}</TooltipText></View>',
            self.ui,
        )
        self.assertNotIn("function defaultUpstreamSurface(", self.ui)
        self.assertNotIn("ProtocolOrderEditor", self.ui)
        self.assertNotIn("supported_upstream_url_surfaces", self.ui)

    def test_protocol_copy_separates_auto_mode_from_backup_protocol(self) -> None:
        for marker in (
            '"providers.protocolModeFallback": "自动适配"',
            '"providers.fallbackProtocol": "备用协议"',
            '"providers.protocolModeFallbackHint": "优先沿用当前请求协议；上游不支持时切换到备用协议。"',
        ):
            self.assertIn(marker, self.zh)
        self.assertNotIn('"providers.protocolModeFallback": "兜底协议"', self.zh)
        self.assertIn('"providers.protocolModeFallback": "Auto-adapt"', self.en)
        self.assertIn('"providers.fallbackProtocol": "Backup protocol"', self.en)

    def test_provider_probe_button_is_disabled_while_probing(self) -> None:
        self.assert_ui_has(
            '<ActionButton title={probing ? translate("providers.probing") : translate("providers.probe")} disabled={busy || probing} onPress={probe} />'
        )
        self.assertNotIn('await flushPendingFields();\n      const before = await ipc.snapshot();', self.ui)

    def test_provider_selection_and_new_models_keep_independent_stable_state(self) -> None:
        self.assert_ui_has('onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }}')
        self.assert_ui_has('const knownModelIdsByProvider = useRef<Map<string, Set<string>> | undefined>(undefined);')
        self.assert_ui_has('Promise.all(added.map(({ providerId: targetProviderId, modelId }) => probeModel(targetProviderId, modelId, { confirmRecommendation: false })))')
        self.assert_ui_has('const key = modelProbeKey(targetProviderId, targetModelId);')

    def test_log_view_identifies_when_the_recent_record_limit_is_reached(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        self.assert_ui_has('active && lineCount >= active.limit ? "logs.latestLinesAtLimit" : "common.lines"')
        self.assertIn('"logs.latestLinesAtLimit": "Latest {count} lines (view limit)"', english)
        self.assertIn('"logs.latestLinesAtLimit": "最近 {count} 行 (视图上限)"', chinese)

    def test_providers_workspace_keeps_fixed_three_pane_structure(self) -> None:
        self.assert_ui_has("function ProviderWorkspace(")
        self.assert_ui_has('id: "providers", title: translate("providers.providers")')
        self.assert_ui_has('id: "routes", title: translate("providers.routes")')
        self.assert_ui_has("function TablePane(")
        for marker in (
            "providersLayout:",
            "providerWorkspace:",
            "providerThreePane:",
            "providerListPane:",
            "modelListPane:",
            "providerInspectorPane:",
            "tableHeader:",
            "tableScroll:",
        ):
            self.assert_ui_has(marker)
        self.assert_ui_has('providerInspector: { width: 280, minWidth: 280, maxWidth: 280')
        self.assertNotIn("<ScrollView contentContainerStyle={styles.providerEditorScroll}><ProviderEditor", self.ui)
        self.assert_ui_has("providerEditorContent: { flex: 1, minHeight: 0, paddingTop: 3, paddingHorizontal: 12, paddingRight: 8, paddingBottom: 12")
        self.assert_ui_has('providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 6 }')
        self.assert_ui_has("providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }")
        self.assert_ui_has("providerListPane: { width: 154, minWidth: 154, maxWidth: 154")
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 104 }, { label: translate("providers.modelCount"), width: 48 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.model"), width: 96 }, { label: translate("providers.upstream"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 96 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.key"), width: 100 }]}')
        self.assert_ui_has('cellHorizontalPadding={0}')
        self.assert_ui_has('firstColumnHorizontalPadding={0}')
        self.assert_ui_has('label={translate("providers.keyName")} labelWidth={42}')
        self.assert_ui_has('NativeSecretField plainText autoCommit label={translate("providers.keyValue")}')
        self.assert_ui_has('<View style={[styles.providerKeyList, styles.providerKeyListCompact]}>')
        self.assert_ui_has('providerKeyGrid: { flex: 1, minHeight: 164, flexDirection: "row", alignItems: "flex-start", gap: 8 }')
        self.assert_ui_has('providerKeyList: { width: 100, minWidth: 100, maxWidth: 100, flexShrink: 0, gap: 3 }')
        self.assert_ui_has('providerKeyTable: { width: 100, minWidth: 100, maxWidth: 100, height: 136, minHeight: 136, flexShrink: 0 }')
        self.assert_ui_has('providerKeysEditorCompact: { gap: 4 }')
        self.assert_ui_has('providerKeyGridCompact: { minHeight: 164, gap: 8 }')
        self.assert_ui_has('providerKeyActionsCompact: { minHeight: 22, gap: 3 }')
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('return <View style={styles.providersLayout}>')
        self.assert_ui_has('<View style={styles.providerLeftColumn}>')
        self.assert_ui_has('{viewMode === "routes" ? <View style={styles.routeWorkspace}>')
        self.assert_ui_has('<TablePane wide style={styles.routeTablePane}')
        self.assert_ui_has('<View style={styles.providerInspector}>')
        self.assertEqual(1, self.ui.count('<View style={styles.providerInspector}>'))
        self.assertNotIn('viewMode === "routes" ? <View style={styles.providerWorkspace}', self.ui)
        self.assertNotIn('routeWorkspaceWithInspector', self.ui)
        self.assert_ui_has('routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('onSelectionChange={selectRoute}')
        self.assert_ui_has('label: translate("providers.provider"), width: 136 }, { label: translate("common.order"), width: 48')
        self.assert_ui_has('width: 112 }, { label: translate("providers.upstream"), width: 136')
        self.assert_ui_has('label: translate("providers.keyName"), width: 112')
        self.assertNotIn('const displayRoutes = useMemo', self.ui)
        self.assert_ui_has('key: `route-public-model:${entry.publicModel}`')
        self.assert_ui_has('spanning: true')
        self.assert_ui_has(r'cells: [`\t${stringValue(entry.provider.display_name')
        self.assert_ui_has('rows={routeRows} disabledRowKeys={disabledRouteKeys} selectedKey={selectedRoute ?? ""} compact onSelectionChange={selectRoute}')
        self.assert_ui_has('rows={providerRows} disabledRowKeys={disabledProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange=')
        self.assert_ui_has('rows={modelRows} disabledRowKeys={disabledModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} scrollTrailingColumnOverflow onSelectionChange=')
        self.assert_ui_has('rows={keyRows} selectedKey={selectedKey} compact cellHorizontalPadding={0} firstColumnHorizontalPadding={0} onSelectionChange={setSelectedKey}')
        select_route = self.ui.split('const selectRoute = useCallback', 1)[1].split('const chooseViewMode', 1)[0]
        self.assertLess(select_route.index('if (!selected) return;'), select_route.index('setSelectedRoute(routeId);'))
        self.assert_ui_has('providerSourceModel ? <ProviderEditor provider={activeRoute.provider}')
        self.assert_ui_has('model={activeRoute.model}')
        self.assert_ui_has('onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))}')
        self.assert_ui_has('setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`)')
        self.assert_ui_has('disabledRowKeys={disabledModelKeys}')
        self.assert_ui_has('disabledRowKeys={disabledRouteKeys}')
        self.assertNotIn('secondaryCellKeys={routeSecondaryCellKeys}', self.ui)
        self.assertNotIn('<ScrollView contentContainerStyle={styles.inspectorContent}>', self.ui)
        self.assert_ui_has('native.window.open("relay-accounts")')
        self.assert_ui_has('route === "relay-accounts" || route === "relay-add" ? <RelayAccountManager visible setupOnly={route === "relay-add"}')

    def test_provider_empty_state_keeps_the_native_table_frames(self) -> None:
        for marker in (
            'rows={providerRows}',
            'rows={modelRows}',
            'rows={routeRows}',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('{providers.length === 0 ? <EmptyState translate={translate} /> : <NativeTable', self.ui)
        self.assertNotIn('{models.length === 0 ? <EmptyState translate={translate} /> : <NativeTable', self.ui)
        self.assertNotIn('{routes.length === 0 ? <EmptyState translate={translate} /> : <NativeTable', self.ui)

    def test_provider_transfer_dropdown_contains_every_import_and_export_action(self) -> None:
        self.assert_ui_has("const transferActions = [")
        for marker in (
            'translate("providers.currentCodex")',
            'translate("providers.currentClaude")',
            'translate("providers.configurationFile")',
            'translate("providers.relay")',
            'translate("providers.exportFile")',
            'else if (index === 4) void exportSelected();',
            'title={`${translate("providers.importExport")} ▾`}',
            'title: translate("providers.transferActions"), items: transferActions',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('title={translate("providers.exportFile")}', self.ui)

    def test_provider_transfer_dropdown_is_anchored_to_its_trigger(self) -> None:
        self.assertIn("const transferButtonRef = useRef<HostInstance | null>(null);", self.ui)
        self.assertIn("button.measureInWindow", self.ui)
        self.assertIn("anchor: { x, y, width, height }", self.ui)
        self.assertIn("ref={transferButtonRef}", self.ui)

    def test_provider_footer_uses_the_standard_inset_action_bar(self) -> None:
        self.assertIn('footer: { height: 52, minHeight: 52, flexShrink: 0', self.ui)
        self.assertIn('paddingHorizontal: 16, paddingVertical: 8', self.ui)
        self.assertIn('footerButtons: { flexShrink: 0', self.ui)
        self.assertNotIn("footerExact", self.ui)
        self.assertNotIn('exact={route === "providers-models"}', self.ui)

    def test_application_fonts_use_medium_size_with_smaller_form_tips(self) -> None:
        typography = TYPOGRAPHY.read_text(encoding="utf-8")
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        macos_controls = MACOS_CONTROLS.read_text(encoding="utf-8")
        windows_controls = WINDOWS_CONTROLS.read_text(encoding="utf-8")
        windows_leaf = WINDOWS_LEAF.read_text(encoding="utf-8")
        windows_relay = WINDOWS_RELAY.read_text(encoding="utf-8")

        self.assertIn("export const UI_FONT_SIZE = 13;", typography)
        self.assertIn("export const UI_TIP_FONT_SIZE = 12;", typography)
        self.assertEqual(
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE", "10"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", self.ui)},
        )
        self.assertEqual(
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", relay)},
        )
        self.assertEqual(
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", self.native_controls)},
        )
        self.assertIn("runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)
        self.assertIn("fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)
        self.assertIn("formHint: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE", relay)

        self.assertIn("constexpr CGFloat LiteLLMUIFontSize = 13.0;", macos_controls)
        self.assertNotIn("systemFontSizeForControlSize", macos_controls)
        self.assertNotRegex(macos_controls, r"(?:systemFontOfSize|monospacedSystemFontOfSize):\d")
        self.assertIn("column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);", macos_controls)
        self.assertIn("NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightMedium]", macos_controls)

        self.assertIn("private let nativeUIFontSize: CGFloat = 13", self.macos_leaf)
        application_leaf_ui = self.macos_leaf.split("func showReadOnlyText", 1)[1]
        self.assertIn("ofSize: readOnlyCodeFontSize", application_leaf_ui)
        self.assertNotRegex(application_leaf_ui, r"ofSize:\s*\d")

        for source in (windows_controls, windows_leaf, windows_relay):
            self.assertIn("constexpr double kUIFontSize = 13.0;", source)
            self.assertNotRegex(source, r"FontSize\((?!kUIFontSize\))")

    def test_logs_default_to_requests_and_show_latest_first(self) -> None:
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn('setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");', bootstrap)
        self.assert_ui_has('useState<typeof LOG_TABS[number]>(() => requestedTab ?? "requests")')
        self.assert_ui_has('return rightTime - leftTime || right.index - left.index;')
        self.assert_ui_has('compact onSelectionChange=')
        self.assertNotIn('compact followBottom onSelectionChange=', self.ui)
        self.assert_ui_has('requestedTabKey={nativeAction?.sequence ?? 0}')
        self.assert_ui_has('const clearTabRef = useRef<typeof LOG_TABS[number] | undefined>(undefined);')
        self.assert_ui_has('if (clearTabRef.current) return;')
        self.assert_ui_has('const [selectedKeys, setSelectedKeys] = useState<Partial<Record<LogTab, string>>>({});')
        self.assert_ui_has('const currentKey = current["route-trace"];')
        self.assert_ui_has('if (currentKey && routeTraceRequests.some((request) => request.key === currentKey)) return current;')
        self.assert_ui_has('return { ...current, "route-trace": routeTraceRequests[0].key };')
        self.assert_ui_has('type PauseIntent = { tab: typeof LOG_TABS[number]; paused: boolean; token: number };')
        self.assert_ui_has('type ClearIntent = { tab: typeof LOG_TABS[number]; token: number };')
        self.assert_ui_has('const paused = pauseIntent?.tab === selected ? pauseIntent.paused : active?.paused ?? false;')
        self.assert_ui_has('const nextPaused = !paused;')
        self.assert_ui_has('const result = await ipc.logs(tab);')
        self.assert_ui_has('const clearing = clearIntent?.tab === selected;')
        self.assert_ui_has('setSelectedKeys((current) => ({ ...current, [tab]: undefined }));')
        self.assert_ui_has('void dispatch("logs.clear", { tab }, "logs").then(async () => {')
        self.assert_ui_has('const lineCount = clearing ? 0 : active?.line_count ?? rows.length;')
        self.assert_ui_has('IconButton label="" symbol={paused ? "play" : "pause"}')
        self.assert_ui_has('IconButton label="" symbol="trash" title={translate("common.clearView")} disabled={busy} onPress={clearLogs}')
        self.assertNotIn('dispatch("logs.refresh", { tab: selected }, "logs")', self.ui)
        self.assert_ui_has('translate("logs.apiKeyName")')
        self.assert_ui_has('value.api_key_name')
        self.assert_ui_has('`${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}')

    def test_service_health_is_manual_and_has_no_background_poll(self) -> None:
        self.assert_ui_has('case "service-health": return "health";')
        self.assertNotIn("SERVICE_HEALTH_POLL_MS", self.ui)
        self.assertNotIn("SERVICE_RECOVERY_RETRY_MS", self.ui)
        self.assertNotIn("pollServiceHealth", self.ui)

    def test_native_menu_actions_append_local_diagnostics_after_success(self) -> None:
        self.assert_ui_has('type: "logs.record_menu_action"')
        self.assert_ui_has('payload: { tab: "menu", menu_action: action }')
        self.assert_ui_has('runServiceOperation(serviceOperation).then(() => recordMenuAction(action))')

    def test_provider_transfer_copy_names_each_import_or_export_direction(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for text in (english, chinese):
            for key in (
                "providers.currentCodex",
                "providers.currentClaude",
                "providers.configurationFile",
                "providers.relay",
                "providers.exportFile",
            ):
                self.assertIn(f'"{key}":', text)
        for value in (
            '"providers.currentCodex": "Import from Current Codex Settings"',
            '"providers.currentClaude": "Import from Current Claude Settings"',
            '"providers.configurationFile": "Import from File..."',
            '"providers.relay": "Import from Relay..."',
            '"providers.exportFile": "Export to File..."',
        ):
            self.assertIn(value, english)
        for value in (
            '"providers.currentCodex": "从当前 Codex 设置导入"',
            '"providers.currentClaude": "从当前 Claude 设置导入"',
            '"providers.configurationFile": "从文件导入..."',
            '"providers.relay": "从中转站导入..."',
            '"providers.exportFile": "导出到文件..."',
        ):
            self.assertIn(value, chinese)

    def test_claude_permission_picker_includes_the_official_delegate_mode(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        self.assert_ui_has('"bypassPermissions", "delegate"]')
        self.assertIn('"claude.permission.delegate": "Delegate"', english)
        self.assertIn('"claude.permission.delegate": "委派"', chinese)

    def test_claude_structured_forms_are_named_for_their_single_column_layout(self) -> None:
        self.assert_ui_has("styles.structuredForm")
        self.assert_ui_has("structuredForm: { gap: 6 }")
        self.assertNotIn("twoColumnForm", self.ui)

    def test_claude_settings_expose_only_the_compact_safe_capability_controls(self) -> None:
        for marker in (
            'translate("claude.disableBypassPermissions")',
            'translate("claude.capabilities")',
            'translate("claude.disableBundledSkills")',
            'translate("claude.disableClaudeAiConnectors")',
            'translate("claude.disableRemoteControl")',
            'translate("claude.disableAllHooks")',
            'translate("claude.desktopRawJson")',
            'translate("claude.codeRawJson")',
        ):
            self.assert_ui_has(marker)

    def test_relay_metadata_commits_before_native_credential_cleanup_and_persists_a_retry(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        route = self.ui.split("const commitRelayMetadata", 1)[1].split("const flushPendingFields", 1)[0]
        delete = relay.split("const deleteAccount", 1)[1].split("const remove", 1)[0]
        password = relay.split("const updateRememberPassword", 1)[1].split("return <Modal", 1)[0]

        self.assertIn("type PendingCredentialCleanup", relay)
        self.assertIn("const retryCredentialCleanup", relay)
        self.assertIn("credentialCleanupsFromSnapshot", relay)
        self.assertIn("pending_credential_cleanups", relay)
        self.assertIn('commit("credential_cleanup_confirm"', relay)
        self.assertNotIn('item.kind === "password"', relay)
        self.assertIn("commitRelayMetadata", self.ui)
        self.assertIn("await enqueueDispatch(type, payload, targetDomain);", route)
        self.assertIn("commit={commitRelayMetadata}", self.ui)
        self.assertLess(
            delete.index('await commit("account.delete"'),
            delete.index("await native.clearRelayCredentials(account.id)"),
        )
        self.assertIn('kind: "credentials"', delete)
        self.assertIn("secret-free Core", delete)
        self.assertLess(
            password.index('await commit("account.update"'),
            password.index("await native.clearRelayPassword(accountID)"),
        )
        self.assertNotIn('kind: "password"', password)
        self.assertNotIn('credential_cleanup_confirm', password)
        self.assertIn('translate("relay.retryCleanup")', relay)

    def test_native_tables_use_framed_platform_list_chrome_and_grouping(self) -> None:
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
            self.assertIn("secondaryCellKeys?: ReadonlyArray<string>;", spec)
            self.assertIn("spanningRowKeys?: ReadonlyArray<string>;", spec)
        self.assertIn("striped = true, alternatingRows = false", self.native_controls)
        self.assertIn("const stripedRows = striped && (alternatingRows || rows.length > 0);", self.native_controls)
        self.assertIn("alternatingRows: stripedRows,", self.native_controls)
        self.assertIn("secondaryCellKeys = []", self.native_controls)
        self.assertIn("secondaryCellKeys,", self.native_controls)
        self.assertIn("const spanningRowKeys = rows.filter((row) => row.spanning).map((row) => row.key);", self.native_controls)
        self.assertIn("spanningRowKeys,", self.native_controls)
        # The fetched-model picker is now a native modal leaf, so its old
        # React table no longer belongs to the shared window tree.
        self.assertEqual(self.ui.count("<NativeTable"), 6)
        self.assertEqual(self.ui.count("alternatingRows"), 0)
        self.assertNotIn("selectedKey={selectedRoute ?? \"\"} alternatingRows", self.ui)
        self.assertNotIn("striped={false}", self.ui)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = YES;", mac_native)
        self.assertIn("_tableView.style = NSTableViewStylePlain;", mac_native)
        self.assertIn("constexpr CGFloat LiteLLMTableMinimumHorizontalPadding = 8.0;", mac_native)
        self.assertIn("constexpr CGFloat LiteLLMTableHeaderHorizontalPadding = 6.0;", mac_native)
        self.assertIn("paragraph.firstLineHeadIndent = LiteLLMTableHeaderHorizontalPadding;", mac_native)
        self.assertIn("paragraph.tailIndent = -LiteLLMTableHeaderHorizontalPadding;", mac_native)
        self.assertIn("_tableView.gridStyleMask = NSTableViewGridNone;", mac_native)
        self.assertNotIn("NSTableViewSolidVerticalGridLineMask", mac_native)
        self.assertNotIn("NSTableViewSolidHorizontalGridLineMask", mac_native)
        table_native = mac_native.split("@implementation LiteLLMAppKitTableComponentView", 1)[1].split("@end", 1)[0]
        self.assertIn("LiteLLMTableFrameView", mac_native)
        self.assertIn("_frameView.framedContentView = _scrollView;", table_native)
        self.assertIn("self.contentView = _frameView;", table_native)
        self.assertIn("_scrollView.borderType = NSNoBorder;", table_native)
        self.assertNotIn("_scrollView.borderType = NSLineBorder;", table_native)
        self.assertNotIn("_scrollView.borderType = NSBezelBorder;", table_native)
        self.assertIn("column.headerCell.bordered = NO;", table_native)
        self.assertIn("column.headerCell.bezeled = NO;", table_native)
        self.assertIn("column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);", table_native)
        self.assertIn("_tableView.floatsGroupRows = NO;", table_native)
        self.assertIn("isGroupRow:(NSInteger)row", table_native)
        self.assertIn("shouldSelectRow:(NSInteger)row", table_native)
        self.assertIn("return ![self isSpanningRow:row];", table_native)
        self.assertIn('identifier = @"LiteLLMAppKitTableGroupCell";', table_native)
        self.assertIn("@interface LiteLLMTableGroupCellView : NSView", mac_native)
        self.assertIn("cell = [[LiteLLMTableGroupCellView alloc] initWithFrame:NSZeroRect];", table_native)
        self.assertIn("NSFont *TableCellFont()", mac_native)
        self.assertIn("NSAttributedString *TableCellTitle(NSString *title, NSColor *color)", mac_native)
        self.assertIn("NSFontAttributeName: TableCellFont()", mac_native)
        self.assertIn("label.font = TableCellFont();", table_native)
        self.assertIn("cell.label.font = TableCellFont();", table_native)
        self.assertIn("MAX(\n        LiteLLMTableMinimumHorizontalPadding,\n        static_cast<CGFloat>(viewProps.firstColumnHorizontalPadding))", table_native)
        self.assertIn("cell.label.textColor = NSColor.labelColor;", table_native)
        self.assertIn("cell.label.attributedStringValue = TableCellTitle(value, NSColor.labelColor);", table_native)
        self.assertIn("label.attributedStringValue = TableCellTitle(value, textColor);", table_native)
        self.assertIn("heightOfRow:(NSInteger)row", table_native)
        self.assertIn("NSMaxY([_tableView rectOfRow:rowCount - 1])", table_native)
        self.assertNotIn("_tableView.numberOfRows * _tableView.rowHeight", table_native)
        self.assertIn(
            "_tableView.usesAlternatingRowBackgroundColors = newViewProps.alternatingRows;",
            mac_native,
        )
        self.assertIn("props.alternatingRows.value_or(false)", windows_native)
        self.assertIn("table_frame_ = Border{};", windows_native)
        self.assertIn("table_frame_.BorderThickness(Thickness{1, 1, 1, 1});", windows_native)
        self.assertIn("header_frame_.BorderThickness(Thickness{0, 0, 0, 0});", windows_native)
        self.assertNotIn("header_frame_.BorderThickness(Thickness{0, 0, 0, 1});", windows_native)
        self.assertIn("FontWeights::SemiBold()", windows_native)
        spanning_block = windows_native.split("if (spanning) {", 1)[1].split("} else {", 1)[0]
        self.assertIn("label.FontSize(kUIFontSize);", spanning_block)
        self.assertIn("FontWeights::Normal()", spanning_block)
        self.assertNotIn("FontWeights::SemiBold()", spanning_block)
        self.assertNotIn("SecondaryTextBrush()", spanning_block)
        self.assertIn("style={[styles.selectableTitle, styles.tableFallbackGroupText]}", self.native_controls)
        self.assertIn('tableFallbackGroupText: { fontWeight: "400" },', self.native_controls)
        self.assertIn("spanningRowKeys", windows_native)
        self.assertIn("Grid::SetColumnSpan(label", windows_native)
        self.assertIn("if (IsSpanningKey(Props()->rowKeys[index])) return;", windows_native)
        self.assertIn("RestoreControlledSelection();", windows_native)
        self.assertIn('rowKey + "\\x1f" + std::to_string(columnIndex)', mac_native)
        self.assertIn("static_cast<size_t>(row) >= viewProps.rowKeys.size()", mac_native)
        self.assertIn("static_cast<size_t>(columnIndex) >= columnCount", mac_native)
        self.assertIn("label.textColor = textColor;", mac_native)
        self.assertIn('props.rowKeys[row_index] + "\\x1f" + std::to_string(column_index)', windows_native)
        self.assertIn("if (disabled || secondary) cell.Foreground(SecondaryTextBrush());", windows_native)

    def test_route_trace_uses_primary_text_except_active_selection(self) -> None:
        for style in (
            "routeTraceRequestTime: { flexShrink: 0, color: systemColors.label",
            "routeTraceRequestPath: { color: systemColors.label",
            "routeTraceOutcomeDirect: { color: systemColors.label }",
            "routeTraceDetailMeta: { color: systemColors.label",
            "routeTracePathCount: { color: systemColors.label",
            "routeTraceStepLabel: { color: systemColors.label",
            "routeTraceStepStateAttempted: { color: systemColors.label }",
            "routeTraceStepDetail: { color: systemColors.label",
            "routeTraceNoPathText: { color: systemColors.label",
            "routeTraceNoSelectionText: { color: systemColors.label",
            "routeTraceInfoText: { color: systemColors.label }",
        ):
            self.assertIn(style, self.ui)
        self.assertIn("routeTraceRequestTextSelected: { color: systemColors.selectedControlText }", self.ui)
        self.assertIn('secondaryLabel: semanticColor("secondaryLabelColor", "TextFillColorSecondary", "#6e6e73")', self.ui)
        self.assertIn("routeTraceTimelineNodeStart: { backgroundColor: systemColors.secondaryLabel }", self.ui)
        self.assertIn("routeTraceTimelineNodeSelected: { backgroundColor: systemColors.green }", self.ui)
        self.assertIn("routeTraceTimelineNodeFailed: { backgroundColor: systemColors.red }", self.ui)
        self.assertIn('routeTraceTimelineNode: { width: 18, height: 18, borderRadius: 9', self.ui)
        self.assertIn("routeTraceTimelineNodeAttempted: { borderWidth: 1, borderColor: systemColors.secondaryLabel", self.ui)
        self.assertIn('routeTraceTimelineNodeText: { color: systemColors.label, fontSize: 10, fontWeight: "400"', self.ui)
        self.assertIn("routeTraceStepCardSelected: { borderColor: systemColors.green, borderWidth: 2 }", self.ui)
        self.assertIn("routeTraceStepStateSelected: { color: systemColors.green }", self.ui)

    def test_native_log_tables_support_compact_rows_on_both_hosts(self) -> None:
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
            self.assertIn("compact?: WithDefault<boolean, false>;", spec)
        self.assertIn("_tableView.rowHeight = newViewProps.compact ? 22 : 28;", mac_native)
        self.assertIn("row.MinHeight(props.compact.value_or(false) ? 22.0 : 28.0);", windows_native)
        self.assertIn("AlternatingRowBrush()", windows_native)

    def test_native_tables_keep_short_log_columns_readable_and_scroll_overflow(self) -> None:
        mac_native = (
            ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
        ).read_text(encoding="utf-8")
        windows_native = (
            ROOT / "rn/apps/windows/src/native/windows/WinUIControls.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("column.minWidth = 1;", mac_native)
        self.assertIn("column.maxWidth = CGFLOAT_MAX;", mac_native)
        self.assertIn("_scrollView.hasHorizontalScroller = needsHorizontalScroller;", mac_native)
        self.assertIn("NSTableViewNoColumnAutoresizing", mac_native)
        self.assertIn("std::vector<CGFloat> _requestedColumnWidths;", mac_native)
        self.assertIn("NSTableViewColumnDidResizeNotification", mac_native)
        self.assertIn("- (void)updateColumnMinimumWidths", mac_native)
        self.assertIn("MIN(_requestedColumnWidths[index], minimumWidths[index])", mac_native)
        self.assertIn("const CGFloat availableColumnWidth = NSWidth(visibleBounds);", mac_native)
        self.assertIn("MAX(NSWidth(visibleBounds), laidOutContentWidth)", mac_native)
        self.assertIn("std::max(88.0, static_cast<double>(widths[index]))", windows_native)
        self.assertIn("ScrollBarVisibility::Auto", windows_native)
        self.assertIn("horizontal_scroller_.Content(table_);", windows_native)
        self.assertIn("table_.MinWidth(TableWidth(props.columnWidths, column_count));", windows_native)

    def test_legacy_window_footers_keep_close_and_apply_actions(self) -> None:
        self.assert_ui_has("function DialogFooter(")
        self.assert_ui_has('title={translate("menu.close")}')
        self.assert_ui_has('route === "runtime-settings" ? translate("common.saveAndApply") : translate("menu.apply")')

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

    def test_native_checkboxes_reserve_width_for_their_translated_labels(self) -> None:
        """Opaque Fabric controls must not collapse to a bare checkmark square."""
        self.assertIn("function nativeCheckboxMinimumWidth(label: string)", self.native_controls)
        self.assertIn("Array.from(label)", self.native_controls)
        self.assertIn("const sizedStyle = [{ minWidth: labelVisible ? nativeCheckboxMinimumWidth(label) : 24 }, style];", self.native_controls)
        self.assertIn("style={sizedStyle}", self.native_controls)
        self.assertIn('<NativeCheckbox label={label} labelVisible={false}', self.ui)
        self.assertIn('<Text numberOfLines={2} style={styles.runtimeFieldLabel}', self.ui)
        self.assertIn('<View style={styles.runtimeValueSlot}>{control}</View>', self.ui)
        self.assertIn('runtimeBooleanControl: { width: 24, minWidth: 24, height: 24', self.ui)
        self.assertNotIn("runtimeBooleanSlot", self.ui)
        self.assertNotIn("runtimeBooleanHelpSlot", self.ui)

    def test_settings_workspaces_keep_their_legacy_layout_roots(self) -> None:
        expected_components = {
            "CodexWorkspace": ("codexWorkspace:", "codexRawPane:"),
            "RuntimeWorkspace": ("runtimeWorkspace:", "runtimeScrollSurface:"),
            "WebDavWorkspace": ("webDavForm:", "webdavFooterLeading:"),
            "LogsWorkspace": (
                "logsWindow:",
                "logsToolbar:",
                "logsTabs:",
                "logTable:",
            ),
        }
        for component, markers in expected_components.items():
            self.assert_ui_has(f"function {component}(")
            for marker in markers:
                self.assert_ui_has(marker)

    def test_assistant_settings_tabs_have_a_dedicated_aligned_tab_bar(self) -> None:
        for marker in (
            "<View style={styles.settingsTabBar}>",
            '<WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]}',
            "style={styles.settingsTabs}",
            "settingsTabBar:",
            "settingsTabs:",
            "borderBottomWidth: 1",
            "alignSelf: \"flex-start\"",
        ):
            self.assert_ui_has(marker)

    def test_codex_provider_editor_actions_have_a_clear_textual_hierarchy(self) -> None:
        for marker in (
            "codexProviderEditor",
            "codexProviderToolbar",
            "codexProviderToolbarTitle",
            "codexProviderActions",
            "codexProviderActionButton",
            "codexProviderSplit",
            'translate("screen.configured")',
            'title={translate("common.add")}',
            'symbol="plus"',
            'title={translate("common.delete")}',
            'symbol="minus"',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("<View style={styles.listToolRail}>", self.ui)

    def test_codex_provider_url_is_only_edited_in_the_provider_detail(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        self.assertIn(
            'identifier(item) === (selectedProvider ?? directProvider)', codex
        )
        self.assertIn(
            'setSelectedProvider(configuredProvider ? nextProvider : undefined)', codex
        )
        self.assertIn(
            '{directProvider === "openai" ? <TextField label={translate("codex.gateway")}',
            codex,
        )
        self.assertIn(
            '{ label: translate("providers.displayName"), width: 230 }', codex
        )
        self.assertIn(
            'cells: [identifier(item), stringValue(item.name), stringValue(item.auth_mode, "none")]',
            codex,
        )
        self.assertNotIn(
            '{ label: translate("providers.baseUrl"), width: 230 }', codex
        )
        self.assertIn(
            '<TextField label={translate("providers.baseUrl")} value={stringValue(provider.base_url)}',
            codex,
        )
        self.assertNotIn(
            '<TextField label={translate("common.endpoint")} value={stringValue(provider.base_url)}',
            codex,
        )

    def test_codex_model_picker_tracks_the_saved_model_without_duplicate_labels(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        self.assertIn(
            "const deploymentModels = [...new Set(deployments.map((item) => stringValue(item.model)).filter(Boolean))];",
            codex,
        )
        self.assertIn(
            '<PickerField label={translate("codex.activeDeployment")} value={stringValue(structured.model)} values={deploymentModels.length > 0 ? deploymentModels',
            codex,
        )
        self.assertIn(
            "const selection = deployments.find((item) => stringValue(item.model) === model);",
            codex,
        )
        self.assertIn(
            "selection: { model: selection.model, provider: selection.provider, deployment_id: selection.deployment_id }",
            codex,
        )
        self.assertIn('"codex.activeDeployment": "从 LiteLLM 选择"', self.zh)
        self.assertIn('"codex.activeDeployment": "Select from LiteLLM"', self.en)

    def test_assistant_plaintext_credentials_have_no_set_or_clear_buttons(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function claudePermissionLabel", 1
        )[0]
        for screen in (codex, claude):
            self.assertIn("<NativeSecretField plainText autoCommit", screen)
            self.assertNotIn("setTitle=", screen)
            self.assertNotIn("clearTitle=", screen)
            self.assertNotIn("onClear=", screen)

    def test_codex_and_claude_share_the_settings_workspace_geometry(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function RuntimeField", 1
        )[0]
        for screen in (codex, claude):
            self.assertIn("const [structuredWidth, setStructuredWidth] = useState(470);", screen)
            self.assertIn("<SettingsWorkspace", screen)
            self.assertIn("structuredWidth={structuredWidth}", screen)
        self.assertIn("minPaneWidth={minStructuredWidth}", self.ui)
        self.assertIn("const rawPaneMinimum = 344;", self.ui)
        self.assertIn("const minStructuredWidth = 360;", self.ui)
        self.assertIn("Math.max(minStructuredWidth, Math.min(680, workspaceWidth - rawPaneMinimum))", self.ui)
        self.assertIn("const paneWidth = Math.max(minStructuredWidth, Math.min(structuredWidth, maxStructuredWidth));", self.ui)
        self.assertIn("paneWidth={paneWidth}", self.ui)
        self.assertIn("paneWidth={paneWidth}", self.ui)

    def test_runtime_uses_core_projection_kinds_and_adaptive_layout(self) -> None:
        for marker in (
            'kind === "toggle"',
            'kind === "choice"',
            'storageKind',
            'const [contentWidth, setContentWidth] = useState(0);',
            'const oneColumn = contentWidth > 0 && contentWidth < 1_000;',
            'onLayout={({ nativeEvent }) => setContentWidth(nativeEvent.layout.width)}',
            'styles.runtimeTwoColumnForm, oneColumn && styles.runtimeOneColumnForm',
            'runtimeOneColumnForm:',
            'translate("common.willClear")',
            'clearSecret({ domain: "runtime", field: "setting", target: key })',
            'runtimeCategoryLabel(category, translate)',
            'runtimeFieldLabel(key, stringValue(item.label, key), translate)',
            'runtimeFieldHelp(key, stringValue(item.help), translate)',
            'runtimeUnitLabel(stringValue(item.unit), translate)',
            'runtimeOptionLabel(key, option, translate)',
        ):
            self.assert_ui_has(marker)

    def test_runtime_metadata_has_complete_chinese_projection(self) -> None:
        schema = (ROOT / "litellm_menu/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        localized = (ROOT / "rn/packages/shared/src/i18n/runtimeSettingsI18n.ts").read_text(encoding="utf-8")
        keys = re.findall(r"'key': '([^']+)'", schema)
        self.assertEqual(60, len(keys))
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIn(f"  {key}: {{ label:", localized)
        for category in ("Timeouts", "Recovery", "Web Search", "Vision Bridge", "Model Context", "Fallback", "Computer Facade", "MCP", "Logs", "Network", "Service"):
            self.assertIn(f'  "{category}":', localized)
        self.assertIn("const optionValues = stringList(item.options);", self.ui)
        self.assertIn("const next = optionValues[nativeEvent.index];", self.ui)
        self.assertNotIn('const label = stringValue(item.label, key);', self.ui)

    def test_assistant_settings_localize_display_values_without_changing_saved_values(self) -> None:
        assistant_i18n = (ROOT / "rn/packages/shared/src/i18n/assistantSettingsI18n.ts").read_text(encoding="utf-8")
        codex_config = (ROOT / "codex_config.py").read_text(encoding="utf-8")
        for feature in re.findall(r'"([a-z0-9_]+)",', codex_config.split("SUPPORTED_FEATURE_KEYS =", 1)[1].split(")", 1)[0]):
            self.assertIn(f"  {feature}: ", assistant_i18n)
        self.assertIn("function FeatureToggles", self.ui)
        self.assertIn("label={codexFeatureLabel(key, translate)}", self.ui)
        self.assertNotIn("label={key}", self.ui)
        self.assertIn("function PickerField", self.ui)
        self.assertIn("function ensureSelectedOption", self.ui)
        self.assertIn("A stale/unknown value must remain visible and selected", self.ui)
        self.assertIn("const selectedLabel = options.find((option) => option.value === value)?.label ?? value;", self.ui)
        self.assertIn("const selectedValue = options.find((option) => option.value === value)?.label ?? value;", self.ui)
        self.assertIn("values: Array<string | AssistantSettingOption>", self.ui)
        self.assertIn("const option = options[nativeEvent.index];", self.ui)
        self.assertIn("if (option) onSelect(option.value);", self.ui)
        self.assertIn("function SegmentedField", self.ui)
        self.assertIn("assistantSettingOptions(values, translate)", self.ui)
        self.assertIn('translate(settingsTab === "claude" ? "card.claudeSettings" : "card.codexSettings")', self.ui)
        self.assertIn("localizeCodexValidationMessage(message, translate)", self.ui)

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
        self.assertNotIn("hint && actionsBelow ? <Text", self.ui)

    def test_snapshot_polling_does_not_recreate_the_translator_or_reset_secure_editors(self) -> None:
        self.assertIn("const snapshotLanguage = snapshot?.language;", self.ui)
        self.assertIn("[hostTranslate, snapshotLanguage]", self.ui)
        self.assertNotIn("[hostTranslate, snapshot]", self.ui)

    def test_snapshot_revisions_drop_stale_responses_without_suppressing_live_log_updates(self) -> None:
        self.assertIn("const acceptedSnapshotRevision = useRef<number>(initialSnapshot?.revision ?? -1);", self.ui)
        self.assertIn("if (next.revision < acceptedSnapshotRevision.current) return;", self.ui)
        self.assertIn("Same-revision snapshots remain", self.ui)
        self.assertNotIn("acceptedSnapshotFingerprint", self.ui)

    def test_route_windows_render_from_the_shared_snapshot_on_the_first_frame(self) -> None:
        ipc = (ROOT / "rn/packages/shared/src/ipc.ts").read_text(encoding="utf-8")
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        platform_entry = (ROOT / "rn/packages/shared/src/platformEntry.ts").read_text(encoding="utf-8")
        self.assertIn("latestSnapshot(): CoreSnapshot | undefined;", (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8"))
        self.assertIn("let initialSnapshotRequest: Promise<CoreSnapshot> | undefined;", ipc)
        self.assertIn('if (latestSnapshot) return call("snapshot", {})', ipc)
        self.assertIn("if (!initialSnapshotRequest)", ipc)
        self.assertIn("rememberSnapshot(event.snapshot);", ipc)
        self.assertIn("const snapshotListeners = new Set<(event: IpcEvent) => void>();", ipc)
        self.assertIn("for (const snapshotListener of snapshotListeners) snapshotListener(event);", ipc)
        self.assertIn("if (!subscriptionStarted)", ipc)
        self.assertIn("const [initialSnapshot] = useState(() => dependencies.ipc.latestSnapshot());", bootstrap)
        self.assertIn("void ipc.snapshot().catch(() => undefined);", platform_entry)
        self.assertIn("const [snapshot, setSnapshot] = useState<CoreSnapshot | undefined>(initialSnapshot);", self.ui)
        self.assertIn('!error && route !== "home" && snapshot ? <RouteSurface', self.ui)

    def test_explicit_service_operations_republish_the_latest_snapshot(self) -> None:
        self.assertIn("const refreshSnapshot = useCallback(async (publishUnchanged = true)", self.ui)
        self.assertIn("if (publishUnchanged || next.revision !== acceptedSnapshotRevision.current) receiveSnapshot(next);", self.ui)
        self.assertIn("return await refreshSnapshot();", self.ui)
        self.assertNotIn("refreshSnapshot(!background)", self.ui)
        self.assertNotIn("runServiceOperation(\"health\", true)", self.ui)

    def test_settings_disk_polling_ignores_unrelated_snapshot_revisions(self) -> None:
        self.assertIn("const latestSnapshot = useRef<CoreSnapshot | undefined>(snapshot);", self.ui)
        self.assertIn("const SETTINGS_DISK_POLL_MS = 5_000;", self.ui)
        self.assertIn("const LOG_VIEW_POLL_MS = 5_000;", self.ui)
        self.assertIn("const ONLINE_USAGE_POLL_MS = 15_000;", self.ui)
        self.assertIn('selected === "online-usage" ? ONLINE_USAGE_POLL_MS : LOG_VIEW_POLL_MS', self.ui)
        self.assertIn("const diskStateChanged = monitoredDiskDomains.some", self.ui)
        self.assertIn("function sameDiskState(left: DiskState | undefined, right: DiskState | undefined)", self.ui)
        self.assertIn("if (!previous || diskStateChanged)", self.ui)
        self.assertIn("if (hasPendingFieldEdits()) return;", self.ui)
        self.assertIn("revision.current = Math.max(revision.current ?? -1, next.revision);", self.ui)
        self.assertIn("This timer exists to observe external file changes", self.ui)

    def test_raw_editors_surface_native_loading_and_failures_without_replacing_the_editor(self) -> None:
        self.assertIn('nativeStatus === undefined || nativeStatus === "loading"', self.ui)
        self.assertIn('nativeErrorCode !== "stage_failed" && nativeErrorCode !== "invalid_text"', self.ui)
        self.assertIn('<View style={styles.rawNativeEditorFrame}><NativeSecureTextEditor', self.ui)
        self.assertIn('nextNativeErrorCode === "stage_failed" ? translate("common.secureEditorStageFailed")', self.ui)
        self.assertIn('style={styles.rawEditorOverlay}', self.ui)
        self.assertIn('<ActionButton title={translate("menu.reload")} disabled={busy} onPress={reloadEditor} />', self.ui)
        self.assertIn('error && editorToken && !nativeReadFailed', self.ui)
        self.assertIn('nativeStatus === "dirty" || nativeStatus === "saving" || nativeErrorCode === "stage_failed"', self.ui)
        self.assertIn('disabled={reloadDisabled} onPress={reloadEditor}', self.ui)
        self.assertNotIn('nextNativeErrorCode === "stage_failed" ? setEditorToken(undefined)', self.ui)

    def test_assistant_setting_option_labels_cover_user_visible_non_brand_values(self) -> None:
        assistant_i18n = (ROOT / "rn/packages/shared/src/i18n/assistantSettingsI18n.ts").read_text(encoding="utf-8")
        for marker in ('"amazon-bedrock": "Amazon Bedrock"', 'lmstudio: "LM Studio"', 'vscode: "VS Code"', 'terminal: "终端"'):
            self.assertIn(marker, assistant_i18n)

    def test_logs_show_empty_state_after_a_loaded_but_missing_log_source(self) -> None:
        self.assertIn('active ? translate("logs.empty") : translate("logs.loading")', self.ui)

    def test_claude_settings_separates_desktop_and_code_sources_without_inventing_saved_defaults(self) -> None:
        for marker in (
            "function claudeDeploymentFromSnapshot(snapshot: CoreSnapshot | undefined)",
            "deployment: ClaudeDeploymentDraft",
            "onDeploymentChange(key, value)",
            "const next = { ...(claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(snapshot)), [key]: value };",
            'enqueueDispatch("patch_deployment", { [key]: value }, "claude")',
            'translate("settings.claudeUnavailable")',
            "const desktop = asRecord(state.desktop);",
            'translate("claude.desktop")',
            'dispatch("desktop_patch", { inferenceProvider: inferenceProvider || null })',
            'dispatch("desktop_patch", { inferenceGatewayBaseUrl })',
            "const desktopModelNames = stringList(desktop.model_names);",
            'translate("claude.desktopModels")',
            'translate("claude.desktopModelsHint")',
            'dispatch("desktop_models_patch", { model_names: splitLines(value) })',
            'field="desktop_gateway_api_key"',
            'field="deployment_token"',
            'plainText autoCommit label={translate("claude.desktopApiKey")}',
            'plainText autoCommit label={translate("claude.token")}',
            'translate("claude.desktopDeveloperMode")',
            'dispatch("developer_patch", { allowDevTools })',
            'document="desktop"',
            'document="developer"',
            'document="settings"',
            "function claudePermissionLabel(value: string, translate: Translate)",
            '<PickerField label={translate("claude.permissions")}',
            'const permissionMode = stringValue(permissions.defaultMode);',
            'defaultMode: defaultMode || null',
            'const CLAUDE_PERMISSION_MODES = ["default", "manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "delegate"];',
            'translate("claude.permission.unknown", { value })',
            'translate("claude.sandboxFailIfUnavailable")',
            'translate("claude.sandboxAutoAllowBash")',
            'translate("claude.sandboxAllowUnsandboxed")',
            'translate("claude.filesystem")',
            'translate("claude.modelBehavior")',
            'translate("claude.fallbackModel")',
            'translate("claude.effortLevel")',
            'translate("claude.capabilities")',
            'translate("claude.disableAllHooks")',
            'function hasBooleanSetting(value: UnknownRecord, key: string): boolean',
            'hasBooleanSetting(settings, "autoMemoryEnabled")',
            'hasBooleanSetting(sandbox, "enabled")',
            'hasBooleanSetting(filesystem, "disabled")',
            'hasBooleanSetting(settings, "autoCompactEnabled")',
            'hasBooleanSetting(settings, "disableAllHooks")',
        ):
            self.assert_ui_has(marker)
        for invented_default in (
            "booleanValue(settings.autoMemoryEnabled, true)",
            "booleanValue(sandbox.autoAllowBashIfSandboxed, true)",
            "booleanValue(sandbox.allowUnsandboxedCommands, true)",
            "booleanValue(settings.autoCompactEnabled, true)",
            'stringValue(settings.effortLevel, "medium")',
            '!booleanValue(filesystem.disabled)',
        ):
            self.assertNotIn(invented_default, self.ui)

    def test_claude_settings_keep_the_compact_memory_permission_sandbox_and_capability_groups(self) -> None:
        for marker in (
            'translate("claude.memory")',
            'translate("claude.autoMemory")',
            'translate("claude.permissions")',
            'translate("claude.disableBypassPermissions")',
            'translate("claude.sandbox")',
            'translate("claude.modelBehavior")',
            'translate("claude.capabilities")',
        ):
            self.assert_ui_has(marker)
        self.assertIn('translate("codex.network")', self.ui)
        self.assertIn('hasBooleanSetting(permissions, "network_access")', self.ui)
        self.assertNotIn('translate("claude.network")', self.ui)

    def test_runtime_form_rows_keep_labels_and_controls_aligned_when_reflowed(self) -> None:
        for marker in (
            "runtimeInputRow:",
            "runtimeFieldLabel:",
            'runtimeFieldLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "right" }',
            "runtimeValueSlot:",
            "runtimeUnit:",
            "runtimeActionSlot:",
            "runtimeHelpSlot:",
            "runtimeTwoColumnForm:",
            "runtimeOneColumnForm:",
            "runtimeField: { minWidth:",
            "runtimeHelpText:",
            "<NativeCheckbox label={label}",
            "<NativePicker labels={optionLabels}",
            "<RuntimeValueField label={label}",
            "accessibilityLabel={label}",
        ):
            self.assert_ui_has(marker)

        self.assertNotIn('label={`${label}${stringValue(item.unit)', self.ui)
        self.assertIn('runtimeOptionLabel(key, rawDefaultValue, translate)', self.ui)
        self.assertNotIn('translate("runtime.subtitle")', self.ui)
        self.assertIn('title={translate("common.restoreDefaults")}', self.ui)
        self.assertIn('style={styles.runtimeRestoreButton}', self.ui)
        self.assertIn('runtimeRestoreButton: { minWidth: 120 }', self.ui)
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        self.assertIn('"common.restoreDefaults": "恢复默认"', chinese)
        self.assertIn('route === "runtime-settings" ? translate("common.saveAndApply")', self.ui)

    def test_provider_workspace_does_not_poll_upstream_billing(self) -> None:
        for marker in ('providers.refresh_billing', 'providers.refresh_multiplier', 'multiplierRefreshStarted', 'multiplierRefreshTimer', 'billingRefreshMinutes', 'billingUsageValue(', 'providers.balance', 'providers.multiplier', 'providers.billingUnavailable'):
            self.assertNotIn(marker, self.ui)

    def test_model_inspector_has_no_upstream_billing_surface(self) -> None:
        self.assert_ui_has('NativeButton title={providerLabel} link')
        for marker in ('billingMultiplierValue', 'billingSummary', 'billingSummaryText', 'providers.balance', 'providers.multiplier', 'providers.billingUnavailable'):
            self.assertNotIn(marker, self.ui)

    def test_provider_machine_key_placeholders_are_localized_without_changing_key_ids(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for text in (english, chinese):
            self.assertIn('"common.default":', text)
            self.assertIn('"common.notAvailable":', text)

        # Native pickers expose labels to users but continue returning an
        # index. Resolve that index through the raw value so "默认" never
        # becomes the persisted API-key identifier.
        for marker in (
            '() => apiKeyNames.map((name) => ({ value: name, label: apiKeyDisplayName(name, translate) })),',
            'const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyName(option.value);',
            'const keyOptions = keyNames.map((name) => ({ value: name, label: apiKeyDisplayName(name, translate) }));',
            'values={keyOptions.length > 0 ? keyOptions : [{ value: "", label: translate("common.notAvailable") }]}',
            'rows={keyRows}',
            'const replacement = keys.find((key) => key !== selectedKey) ?? "";',
            'dispatch("provider.key_delete", { provider_id: id, name: selectedKey }).then(() => setSelectedKey(replacement))',
            'function apiKeyDisplayName(value: unknown, translate: Translate): string {',
            'if (!name) return translate("common.notAvailable");',
            'return name === "default" ? translate("providers.defaultKey") : name;',
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
            'panel.title = localized("modelChooserTitle", fallback: "Choose Models to Add")',
            'panel.minSize = NSSize(width: 520, height: 340)',
            'let contentWidth: CGFloat = 620',
            'let rowHeight: CGFloat = 28',
            'let selectAllButton = modelChooserButton(title: localized("modelChooserAll"',
            'let invertButton = modelChooserButton(title: localized("modelChooserInvert"',
            'let addButton = modelChooserButton(title: "+"',
            'NSButton(checkboxWithTitle: rows[rowIndex].title',
        ):
            self.assertIn(marker, self.macos_leaf)
        self.assertIn("models.count <= 10_000", mac_module)
        self.assertIn("xaml::Window dialog;", windows_leaf)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_", windows_leaf)
        self.assertIn("list.MinHeight(220);", windows_leaf)
        self.assertIn("list.Height(420);", windows_leaf)

    def test_fetch_models_reports_empty_or_unavailable_results_and_does_not_open_stale_chooser(self) -> None:
        for marker in (
            'const dispatchWithOutcome = async (type: string, payload: UnknownRecord = {}, targetDomain = domain): Promise<CoreSnapshot | undefined>',
            'const handleFetchedModels = (summary: UnknownRecord): void => {',
            'const providerIdentity = identifier(provider);',
            'summaryProviderId !== providerId && summaryProviderId !== providerIdentity',
            'if (summary.available === false)',
            'translate("providers.fetchFailed"',
            'translate("providers.fetchEmpty")',
            'const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);',
        ):
            self.assert_ui_has(marker)
        self.assertIn('"providers.fetchFailed": "获取模型失败：{detail}"', self.zh)
        self.assertIn('"providers.fetchEmpty": "供应商未返回模型。"', self.zh)
        self.assertIn('"providers.fetch": "获取模型"', self.zh)
        self.assertIn('"providers.fetchFailed": "Could not fetch models: {detail}"', self.en)
        self.assertIn('"providers.fetchEmpty": "The provider returned no models."', self.en)
        self.assertIn('"providers.fetch": "Fetch models"', self.en)

    def test_provider_inspector_keeps_the_compact_provider_form_and_return_link(self) -> None:
        """The provider editor uses compact, consistently aligned rows and a source-model return link."""
        for marker in (
            'const [providerSourceModel, setProviderSourceModel] = useState<string>();',
            'label={translate("providers.baseUrl")} labelWidth={68}',
            'label={translate("providers.providerName")} labelWidth={68}',
            'label={translate("providers.keyName")} labelWidth={42}',
            'NativeSecretField plainText autoCommit label={translate("providers.keyValue")} hint={selectedKeyConfigured',
            'title={translate("providers.backToModel", { model: sourceModelLabel })} link',
            'providerEditorHeader:',
            'providerEditorSection:',
            'providerKeysHeading:',
            'NativeSecretField plainText autoCommit label={translate("providers.keyValue")}',
            'formRow: { width: "100%", minHeight: 26',
            'formRowLabel: { width: 112, flexShrink: 0',
            'textAlign: "left"',
            'formRowControl: { flex: 1, minWidth: 0, gap: 3',
        ):
            self.assert_ui_has(marker)
        self.assert_ui_has('label={translate("providers.publicModel")} labelWidth={60}')
        self.assert_ui_has('inspectorBody: { gap: 4 }')
        self.assert_ui_has('protocolSettings: { gap: 4 }')
        self.assert_ui_has('protocolHint: { marginLeft: 62')

    def test_logs_keep_the_legacy_dense_toolbar_and_table_frame(self) -> None:
        for marker in (
            "function LogsWorkspace(",
            "<WindowTabs nativeRef={tabsRef} values={tabOptions} selected={selected}",
            "function renderLogRecord(",
            'routingState === "no_available_deployment"',
            'translate("logs.noAvailableRoute")',
            'routingState === "model_not_configured"',
            'translate("logs.modelNotConfigured")',
            'routingState === "unselected"',
            'translate("logs.notRouted")',
            "function logColumns(",
            '<NativeTable columns={nativeTableColumns} rows={nativeTableRows}',
            "translate(\"logs.failed\")",
            "const proxyPrefix = detail.match",
            'translate("logs.duration")',
            'translate("logs.tokenCount")',
            "const columns = useMemo(",
            "() => fitLogColumns(logColumns(selected, translate), tableWidth)",
            "const rows = useMemo(",
            "() => renderLogRecords(clearing ? [] : (active?.records ?? []), selected, translate)",
            "const active = activeState?.tab === selected ? activeState.log : undefined;",
            "const nativeTableColumns = useMemo(",
            "const nativeTableRows = useMemo(",
            '() => selected === "route-trace" ? [] : rows.map((row)',
            "selectedKey={selectedKey}",
            "logTableFrame:",
            "logFilterRow: { width: 360, minWidth: 220, maxWidth: 360",
            "logToolbarSpacer: { flex: 1, minWidth: 0",
            "logActionsRow: { height: 26, flexShrink: 0",
            "logFilterInput: { flex: 1, minWidth:",
            "logTable: { flex: 1, minHeight: 0",
            "logInfoBar: { height: 21, minHeight: 21",
            'logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row"',
        ):
            self.assert_ui_has(marker)

    def test_logs_always_show_public_and_upstream_model_columns(self) -> None:
        for marker in (
            "const publicModel = compactLogValue(value.public_model ?? value.model_group ?? value.model);",
            "upstreamModel: compactUpstreamLogModel(upstreamModel)",
            '{ label: translate("providers.publicModel"), width: 142, value: (row) => row.model }',
            '{ label: translate("providers.upstream"), width: 142, value: (row) => row.upstreamModel }',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("function conditionalUpstreamLogModel(", self.ui)

    def test_logs_project_recovery_route_identity_and_localize_route_diagnostics(self) -> None:
        for marker in (
            "function recoveryStatusLabel(",
            "function recoveryDetailLabel(",
            'cooldown: "logs.recoveryStatus.cooldown",',
            'translate("logs.recoveryDetail.failures", { value: failures[1] })',
            "function routeTraceServiceTierLabel(",
            'return translate("logs.routeEvent.routeEvent");',
            'let source = tab === "service" ? translate("logs.service") : logTitle(tab, translate);',
            '{ label: translate("common.provider"), width: 104, value: (row) => row.provider },',
            '{ label: translate("logs.apiKeyName"), width: 120, value: (row) => row.apiKeyName },',
            'model: model || recoveryFallback,',
            'upstreamModel: compactUpstreamLogModel(upstreamModel) || recoveryFallback,',
        ):
            self.assert_ui_has(marker)
        self.assertIn('"logs.recovery": "恢复 / 冷却"', self.zh)
        self.assertIn('"logs.recoveryStatus.cooldown": "冷却中"', self.zh)

    def test_menu_logs_do_not_repeat_actions_as_detail(self) -> None:
        self.assertIn(
            'if (tab === "menu") return [\n'
            '    time,\n'
            '    { label: translate("logs.action"), width: 180, flex: true, value: (row) => row.action },\n'
            '    status,\n'
            '  ];',
            self.ui,
        )

    def test_route_trace_logs_group_requests_and_show_the_actual_path(self) -> None:
        for marker in (
            'type RouteTraceAttempt = {',
            'type RouteTraceRequest = {',
            'function groupRouteTraceRequests(rows: RenderedLogRecord[]): RouteTraceRequest[] {',
            'if (attempts.length === 0) return [];',
            'Keep pre-route failures in the request logs, but do not render them as',
            'function RouteTraceWorkspace(',
            'const routeTraceRequests = useMemo(',
            '() => selected === "route-trace" ? groupRouteTraceRequests(rows) : []',
            'if (currentKey && routeTraceRequests.some((request) => request.key === currentKey)) return current;',
            '<RouteTraceWorkspace requests={routeTraceRequests}',
            '<FlatList',
            'initialNumToRender={12}',
            'maxToRenderPerBatch={12}',
            'windowSize={7}',
            'translate("logs.routeTrace.actualPath")',
            'selected.attempts.map((attempt, index)',
            'routeTraceWorkspace:',
            'routeTraceRequestPane:',
            'AppState.addEventListener("change", (state) => setAppActive(state === "active"))',
            'onHoverIn={() => setHoveredKey(request.key)}',
            'onFocus={() => onSelect(request.key)}',
            'accessibilityState={{ selected: isSelected }}',
            'selectedContentBackgroundColor',
            'unemphasizedSelectedContentBackgroundColor',
            'alternateSelectedControlTextColor',
            'routeTraceRequestRowSelected: { backgroundColor: systemColors.selectedContent',
            'routeTraceRequestTextSelected: { color: systemColors.selectedControlText }',
            'routeTraceTimeline:',
            'translate("logs.routeTrace.stepProgress", { current: index + 1, total: selected.attempts.length })',
            'routeTraceAttemptIcon(attempt.state)',
            'routeTraceTimelineNodeSelected:',
            'routeTraceTimelineNodeFailed:',
            'routeTraceStepStateIconSelected:',
            'routeTraceStepStateIconFailed:',
            'routeTraceStepStateIconText: { width: 14, height: 14',
            'lineHeight: 14, textAlign: "center"',
        ):
            self.assert_ui_has(marker)
        self.assertIn('"logs.routeTrace.startPoint": "起点"', self.zh)
        self.assertIn('"logs.routeTrace.stepProgress": "第 {current}/{total} 步"', self.zh)
        self.assertNotIn('if (tab === "route-trace") return [', self.ui)
        self.assertNotIn('translate("logs.routePath")', self.ui)
        self.assertNotIn('logs.routeTrace.requestList', self.ui)
        self.assertNotIn('routeTracePaneHeader:', self.ui)
        self.assertNotIn('routeTraceRequestRowSelected: { backgroundColor: systemColors.window, borderLeftWidth:', self.ui)
        for marker in (
            "function routeTraceEventLabel(value: string, translate: Translate): string {",
            "function routeTraceReasonLabel(value: string, translate: Translate): string {",
            "function routeTraceProtocolLabel(value: string, translate: Translate): string {",
            "function routeTraceDetailPartLabel(value: string, translate: Translate): string {",
            "function routeTraceDetailLabel(value: string, translate: Translate): string {",
            'selected_deployment: "logs.routeEvent.selected",',
            'deployment_failover_marked: "logs.routeEvent.failoverMarked",',
            'next_order_fallback_available: "logs.routeEvent.nextOrder",',
            'external_web_search_bridge_synthesis_done: "logs.routeEvent.webSearchSynthesisDone",',
            'return details.join(" | ");',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("logs.routeTrace.otherDetail", self.ui)

    def test_relay_manager_restores_sessions_and_refreshes_resources_on_open(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        self.assertIn("const detected = await detectRelayType();", relay)
        self.assertIn('refreshResources: (accountId: string) => Promise<"ready" | "unavailable">;', relay)
        self.assertIn("await refreshResources(account.id);", relay)
        self.assertIn('title={translate("common.refresh")}', relay)
        self.assertNotIn('translate("relay.refreshResources")', relay)
        self.assertIn('account.loginStatus === "signed_in" ? translate("relay.resourcesNotLoaded")', relay)
        self.assertIn('case "login_expired": return translate("relay.resourcesLoginExpired")', relay)
        self.assertIn('title={translate("relay.importSelected")}', relay)
        self.assertIn("const accountType = detected ?? manualType;", relay)
        self.assertIn("account = await addAccount(accountType, candidate, passwordStorageAvailable && rememberPasswordRef.current, {", relay)
        self.assertIn("await deleteAccount(account);", relay)
        self.assertIn("const beforeRelayState = asRecord(beforeRelay.state);", self.ui)
        self.assertIn("const existingIDs = new Set(beforeRelayAccounts.map((item) => stringValue(item.id)).filter(Boolean));", self.ui)
        self.assertIn("const normalizedOrigin = normalizeRelayOrigin(origin);", self.ui)
        self.assertIn("origin: normalizedOrigin", self.ui)
        self.assertIn("stationOriginKey(stringValue(item.origin)) === originKey && item.type === type", self.ui)
        self.assertIn("NativePicker", relay)
        self.assertIn('title={translate("relay.next")}', relay)
        self.assertIn('title={translate("relay.importSelected")}', relay)
        self.assertNotIn("NativeSegmentedControl", relay)
        self.assertNotIn("const restorationAttempts", relay)
        self.assertIn("const openedAccountIDs = useRef(new Set<string>());", relay)
        self.assertIn('type SavedSessionRestore = "signed_in" | "expired" | "unavailable";', relay)
        self.assertIn("const refreshLoginState = async (account: RelayAccount, automatic = false): Promise<void> => {", relay)
        self.assertIn("const canAutoLogin = account.rememberPassword && account.passwordSaved && Boolean(account.username.trim());", relay)
        self.assertIn("void refreshLoginState(selected, true);", relay)
        self.assertIn('if (status === "signed_in") return "relay.status.signed_in";', relay)
        self.assertIn('if (status === "signed_out") return "relay.status.signed_out";', relay)
        self.assertIn('if (status === "expired") return "relay.status.expired";', relay)
        self.assertIn('title={translate("common.refresh")}', relay)
        self.assertNotIn('title={translate("relay.login")}', relay)
        self.assertIn('const passwordStorageAvailable = true;', relay)
        self.assertIn('native.window.open("relay-add")', relay)
        self.assertIn('translate("relay.passwordNotSaved")', relay)
        self.assertNotIn('onPress={() => { void refreshWorkspace(); }}', relay)
        self.assertNotIn('translate("relay.status.signed_out")', relay)
        self.assertNotIn('translate("relay.checkSession")', relay)

    def test_relay_checkbox_updates_optimistically_without_disabling_the_workspace(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        remember_password_update = relay.split(
            "const updateRememberPassword = async (next: boolean): Promise<void> => {",
            1,
        )[1].split("const updateStation = async", 1)[0]

        self.assertIn("const [rememberPasswordDrafts, setRememberPasswordDrafts]", relay)
        self.assertIn("const selectedRememberPassword = selected ?", relay)
        self.assertIn('value={selectedRememberPassword}', relay)
        self.assertIn("setRememberPasswordDrafts", remember_password_update)
        self.assertNotIn("setCleanupBusy", remember_password_update)

    def test_relay_manager_groups_accounts_by_station_and_uses_a_compact_key_workspace(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        relay_origin = RELAY_ORIGIN.read_text(encoding="utf-8")

        for marker in (
            "export function stationOriginKey(value: string): string {",
            "function stationName(account: RelayAccount): string {",
            "function stationsFromSnapshot(snapshot: CoreSnapshot | undefined, accounts: RelayAccount[]): RelayStation[] {",
            "const rawStations = Array.isArray(state.stations) ? state.stations : Array.isArray(state.groups) ? state.groups : [];",
            "const byOrigin = new Map<string, RelayStation>();",
            "const station = (account.stationID && byID.get(account.stationID)) || (originKey && byOrigin.get(originKey))",
            "function usernameShortName(account: RelayAccount): string {",
            "const stations = useMemo(() => stationsFromSnapshot(snapshot, accounts), [snapshot, accounts]);",
            "stationAccounts(station)",
            "function accountDisplayName(account: RelayAccount, translate: Translate): string {",
            "function accountDetailTitle(account: RelayAccount, translate: Translate): string {",
            "function accountStationLabel(account: RelayAccount): string {",
            "const relayTableRows = useMemo(() => stations.flatMap((station) => {",
            'key: `station:${station.id}`',
            'cells: [stationLabel, ""]',
            'key: `account:${account.id}`',
            'cells: [`\\t${accountDisplayName(account, translate)}`',
            "const stationFormDirty = Boolean(selectedStation)",
            'const relayTableSelection = selectedStationID ? `station:${selectedStationID}` : selected?.id ? `account:${selected.id}` : "";',
            'symbol="plus"',
            'symbol="minus"',
            "NativeSplitView",
            "NativeTable",
            'columns={[{ label: translate("relay.accounts"), width: 150 }, { label: translate("relay.balance"), width: 76 }]}',
            "onSelectionChange={selectRelayTableRow}",
            "nativeRelayTable:",
            "nativeRelayTable:",
            "selectedStation",
            "selectedStationID",
            "stationDetailContent:",
            "stationSettingsForm:",
            "stationSettingsRow:",
            "stationSettingsFeedback:",
            'title={translate("common.save")}',
            "function ResourceRow(",
            'NativeCheckbox label={resource.apiName} labelVisible={false}',
            'function resourceModelsSummary(resource: RelayResource, translate: Translate): string {',
            '<View style={styles.resourceList}>',
            'translate("relay.apiKeysTitle")',
            'translate("relay.selectAllResources")',
            'translate("relay.clearResourceSelection")',
            'translate("relay.apiKeyNamePlaceholder")',
            'translate("relay.apiKeyEdit")',
            'translate("relay.apiKeyDelete")',
            'translate("relay.apiKeyCreate")',
            "resourceHeaderActions",
            "resourceItemActions",
            "NativeSecureTextInput",
            'symbol="copy"',
            'symbol={resource.enabled ? "power-off" : "power-on"}',
            'symbol="edit"',
            'symbol="trash"',
            'translate("relay.apiKeyGroup")',
            "function groupLabel(group: RelayGroup): string {",
            "return `${group.name} / ${Number.isInteger(group.multiplier) ? group.multiplier : group.multiplier.toString()}x`;",
            "multiplier: groupMultiplier(entry.multiplier ?? entry.rate_multiplier ?? entry.ratio)",
            'domain="relay_accounts"',
            'resourceRowDisabled',
            "apiKeyNameDrafts",
            "editingResourceID",
            "compactMeta",
            "resourceEmptyText",
            'accountStationLabel(selected)',
            'translate("relay.balance")',
            'title={translate("relay.addAccount")}',
            'typeDetection === "unknown" ? <FormRow label={translate("relay.type")}',
        ):
            self.assertIn(marker, relay)
        self.assertIn("export function normalizeRelayOrigin(value: string): string {", relay_origin)
        self.assertNotIn('`${stationName(account)} / ${account.username || translate("relay.unsignedAccount")}`', relay)
        self.assertNotIn('translate("relay.station")} / ${translate("relay.username")}', relay)
        self.assertNotIn("const stationAccountRows = useMemo(() => {", relay)
        self.assertNotIn("stationAccountTable:", relay)
        self.assertNotIn("stationAccountsPane:", relay)
        self.assertNotIn("bottomStatusSpacer:", relay)
        self.assertNotIn('feedback ?? translate("relay.stationDetails")', relay)
        self.assertNotIn("accountAvatar", relay)
        self.assertNotIn("accountAvatarText", relay)
        self.assertNotIn('translate("relay.resourceSelectionSubtitle")', relay)
        self.assertNotIn('translate("relay.applyReminder")', relay)
        self.assertNotIn("resourceEndpoint", relay)
        self.assertNotIn("resourceColumnStatus", relay)
        self.assertNotIn("resourceKeyStatus", relay)
        self.assertNotIn("native.showActionMenu", relay)
        self.assertIn('selected.resourceError === "no_api_keys" && !resourceBusy && !restoreBusy', relay)
        self.assertIn('resourceBusy || restoreBusy ? translate("relay.resourcesChecking") : resourceHint(selected, translate)', relay)
        self.assertIn('"relay.resourceCount": "共 {count} 项"', self.zh)
        self.assertIn('"relay.resourceCount": "{count} total"', self.en)
        self.assertIn('selectedResources.length > 0 ? <View style={[styles.bottomBar, compactStyles.bottomBar]}>', relay)
        self.assertIn("apiKeyActions", relay)
        self.assertIn('commitRelayMetadata("api_key.create"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.update"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.set_enabled"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.set_group"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.delete"', self.ui)
        self.assertNotIn('resource_ids: [resourceId]', self.ui)
        self.assertIn('relayTypeLabel(selected.type', relay)

    def test_relay_empty_state_keeps_an_inline_add_affordance(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        # Keep this semantic: the empty view must offer the add action, but
        # the visual contract should not freeze a large placeholder height.
        self.assertIn(
            '<View style={styles.blank}><Text style={styles.empty}>{translate("relay.empty")}</Text><NativeButton title={translate("relay.add")} primary disabled={controlsBusy} onPress={beginAdding} /></View>',
            relay,
        )
        self.assertNotIn('blank: { height:', relay)
        self.assertNotIn('blank: { width:', relay)

    def test_relay_close_always_releases_the_react_route(self) -> None:
        self.assertIn(
            "try {\n"
            "      native.window.close(nativeWindowRoute(route));\n"
            "    } finally {\n"
            "      onClose();\n"
            "    }",
            self.ui,
        )

    def test_shared_workspaces_wrap_fixed_width_controls_before_they_overlap(self) -> None:
        """Nested Codex controls, disk choices, and toolbar actions reflow instead of clipping."""
        for marker in (
            'split: { flexDirection: "row", flexWrap: "wrap"',
            'pluginEditor: { minHeight: 128, flexDirection: "row", flexWrap: "wrap"',
            'const promptedDiskGeneration = useRef<Partial<Record<EditableDiskDomain, number>>>({});',
            'void native.showConfirmation({',
            'message: translate("settings.diskChangedBody"),',
        ):
            self.assert_ui_has(marker)

        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        for marker in (
            'splitView: { flex: 1, minHeight: 0, minWidth: 0',
            'sidebarIconButton: { width: 28, minWidth: 28 }',
            'formRow: { width: "100%", minHeight: 34, flexDirection: "column", alignItems: "stretch", gap: 6 }',
            'bottomBar: { minHeight: 42, paddingHorizontal: 14, paddingVertical: 6, flexDirection: "row", flexWrap: "wrap"',
            'resourcesSection: { minWidth: 0, gap: 7',
            'pendingCleanupList: { maxHeight: 116',
            '<ScrollView style={styles.pendingCleanupList} contentContainerStyle={styles.pendingCleanupListContent}>',
            'pendingCleanup: { minHeight: 30, flexDirection: "row", flexWrap: "wrap"',
        ):
            self.assertIn(marker, relay, marker)

    def test_provider_table_columns_fit_the_fixed_provider_pane(self) -> None:
        self.assertIn('"providers.modelCount": "Count"', self.en)
        self.assertIn('"providers.modelCount": "模型数"', self.zh)
        self.assertIn('"providers.apiKeyOrder": "密钥/顺序"', self.zh)
        self.assertIn('"providers.keyName": "密钥名"', self.zh)
        self.assertIn('"providers.keyValue": "密钥值"', self.zh)
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 104 }, { label: translate("providers.modelCount"), width: 48 }]}')
        self.assert_ui_has('providerListPane: { width: 154, minWidth: 154, maxWidth: 154')
        self.assert_ui_has('columns={[{ label: translate("providers.model"), width: 96 }, { label: translate("providers.upstream"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 96 }]}')
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('providerKeysHeader: { minHeight: 24, flexDirection: "row"')
        self.assert_ui_has('<View style={[styles.providerKeyList, styles.providerKeyListCompact]}>')
        self.assert_ui_has('providerKeyGrid: { flex: 1, minHeight: 164, flexDirection: "row", alignItems: "flex-start", gap: 8 }')
        self.assert_ui_has('providerKeyList: { width: 100, minWidth: 100, maxWidth: 100, flexShrink: 0, gap: 3 }')
        self.assert_ui_has('providerKeyTable: { width: 100, minWidth: 100, maxWidth: 100, height: 136, minHeight: 136')
        self.assert_ui_has('providerKeysEditorCompact: { gap: 4 }')
        self.assert_ui_has('providerKeyGridCompact: { minHeight: 164, gap: 8 }')

    def test_shared_native_controls_default_to_compact_density(self) -> None:
        native_controls = (ROOT / "rn/packages/shared/src/ui/NativeControls.tsx").read_text(encoding="utf-8")
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        for marker in (
            "const compact = props.compact ?? true;",
            "compact = true, onChange",
            "compact = true, followBottom",
            "button: { minWidth: 72, height: 24 }",
            "selectableRow: { minHeight: 28",
        ):
            self.assertIn(marker, native_controls)
        for marker in (
            "button: { minWidth: 72, height: 24 }",
            "segmented: { width: 224, height: 24 }",
            "picker: { minWidth: 160, height: 24 }",
            "textField: { minHeight: 24 }",
        ):
            self.assertIn(marker, appkit_controls)
        self.assert_ui_has("const compactStyles = StyleSheet.create({")
        self.assert_ui_has("formRow: { minHeight: 24, gap: 2 }")
        self.assert_ui_has("formRowControl: { gap: 1 }")
        self.assertIn("const compactStyles = StyleSheet.create({", relay)
        self.assertIn("resourceRow: { minHeight: 34, paddingVertical: 1, gap: 4 }", relay)

    def test_webdav_form_has_an_enable_row_and_fluid_controls(self) -> None:
        for marker in (
            "function WebDavWorkspace(",
            '<NativeCheckbox label={translate("webdav.enabled")} value={booleanValue(state.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { enabled })} style={styles.webdavEnabledControl} />',
            "webdavStateRow:",
            "webdavEnabledControl: { width: 190, flexGrow: 0, flexShrink: 0 }",
            "webdavInlineStatus:",
            "webdavFormRows:",
            'label={translate("webdav.url")}',
            'label={translate("webdav.remoteFile")}',
            'label={translate("webdav.syncEvery")}',
            'label={translate("webdav.httpTimeout")}',
            "function WebDavPasswordField(",
            'placeholder={configured ? translate("webdav.passwordHintConfigured") : translate("webdav.passwordHintOptional")}',
            "webdavWideControl: { flex: 1, minWidth: 0 }",
            'webdavPasswordInput: { width: "100%", minHeight: 26 }',
            "wideButton: { minWidth: 92 }",
        ):
            self.assert_ui_has(marker)
        self.assertNotIn(
            '<NativeSecretField label={translate("webdav.password")}',
            self.ui,
        )

    def test_text_fields_mark_apply_immediately_and_stage_after_a_short_idle(self) -> None:
        for marker in (
            "function usePendingTextField(",
            "setDirty(next !== committedRef.current || commitInFlight.current !== undefined);",
            "}, 240);",
            "void field.commit().catch(() => undefined);",
            "hasPendingFieldEdits())",
            "registry?.setDirty(fieldId.current, true);",
            "const secretDebounceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);",
            "if (autoCommit) {",
            "}, 240);",
            "state.status === \"saved\" || state.status === \"ready\" || state.status === \"error\"",
            "function WebDavPasswordField(",
            'isDirty: () => dirtyRef.current',
            "<NativeSecretField plainText autoCommit label={translate(\"common.apiKey\")}",
            'input: { width: "100%", minHeight: 26',
            'formRow: { width: "100%", minHeight: 26',
            'form: { gap: 6 }',
            'structuredForm: { gap: 6 }',
        ):
            self.assert_ui_has(marker)
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        native_controls = (ROOT / "rn/packages/shared/src/ui/NativeControls.tsx").read_text(encoding="utf-8")
        self.assertIn("textField: { minHeight: 24 }", appkit_controls)
        self.assertIn("compact = true", native_controls)
        self.assertNotIn("providers.apiKeyHint", self.ui)

    def test_protocol_names_are_not_localized(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for catalog in (english, chinese):
            self.assertIn('"providers.responses": "OpenAI Responses"', catalog)
            self.assertIn('"providers.chat": "OpenAI Chat Completions"', catalog)
        self.assertNotIn('"providers.responses": "响应接口"', chinese)
        self.assertNotIn('"providers.chat": "聊天接口"', chinese)

    def test_codex_raw_editors_share_height_and_follow_disk_generation(self) -> None:
        for marker in (
            "const [structuredWidth, setStructuredWidth] = useState(470);",
            "showReload={false} codexPane style={styles.codexRawEditor}",
            "codexRawEditors: { flex: 1, minWidth: 0, minHeight: 0",
            "codexRawEditor: { flexGrow: 1, flexShrink: 1, flexBasis: 0",
            "const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);",
            'if (reloadDomain === "codex" || reloadDomain === "claude") setSettingsRawReloadToken((current) => current + 1);',
            'if ((next.disk[diskDomain]?.generation ?? 0) > priorGeneration && !next.disk[diskDomain]?.changed && (diskDomain === "codex" || diskDomain === "claude"))',
            "await flushPendingFields();",
            "reloadToken={rawReloadToken}",
            "reloadNonce, reloadToken, reset, translate",
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

    def test_standalone_configuration_package_surface_is_removed(self) -> None:
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        windows_leaf = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")

        for source in (self.ui, routes, types, bootstrap, self.platform_entry, self.macos_leaf, windows_leaf):
            self.assertNotIn("configuration-package", source)
            self.assertNotIn("open-configuration-package", source)
            self.assertNotIn("routeConfigurationPackage", source)
        self.assertNotIn("ConfigurationPackageScreen", self.ui)
        self.assertNotIn("legacyPackageDialog", self.ui)
        self.assertNotIn('translate("package.', self.ui)
        self.assertIn("saveFilePicker", types)
        self.assertIn("saveFilePicker", self.platform_entry)

    def test_codex_and_webdav_keep_legacy_visible_labels(self) -> None:
        self.assert_ui_has('>{translate("settings.structured")}</Text>')
        self.assert_ui_has('title={translate("common.test")}')
        self.assert_ui_has('webdavFooterLeading')
        self.assertNotIn('translate("webdav.subtitle")', self.ui)

    def test_settings_surfaces_omit_static_draft_tips_but_keep_actionable_disk_conflicts(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        for marker in (
            'translate("settings.subtitle")',
            'translate("settings.rawDraftHint")',
            'translate("settings.synchronized")',
            'translate("common.staged")',
            'setStaged(',
            'run(() => enqueueDispatch(type, payload, targetDomain), "common.applied"',
        ):
            self.assertNotIn(marker, self.ui)
        self.assertIn('const staged = await enqueueDispatch(type, payload, targetDomain);', self.ui)
        self.assertIn('setSettingsRawReloadToken((current) => current + 1)', self.ui)
        for marker in (
            'translate("settings.diskChangedTitle")',
            'message: translate("settings.diskChangedBody")',
            'confirmLabel: translate("settings.useDisk")',
            ):
            self.assert_ui_has(marker)
        for source in (english, chinese, translation_keys):
            self.assertNotIn('"common.staged"', source)
        self.assertNotIn('"已暂存"', chinese)
        self.assertIn('"relay.resourcesImported": "所选 API 资源已导入“供应商与模型”。请前往该页面并点击“应用”以启用连接。"', chinese)

    def test_macos_leaf_localizes_window_titles_and_keeps_status_menu_order(self) -> None:
        for title in (
            'case "providers-models": return "LiteLLM " + localized("routeProvidersModels", fallback: "Providers & Models")',
            'case "codex-settings", "claude-settings": return localized("routeCodexSettings", fallback: "Codex / Claude Settings")',
            'case "webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Sync Settings")',
            'case "logs": return "LiteLLM " + localized("routeLogs", fallback: "Logs")',
        ):
            self.assertIn(title, self.macos_leaf, title)
        self.assertIn("private static let statusMenuOrder", self.macos_leaf)
        for ordered_item in (
            '"toggle-autostart", "toggle-codex-model-catalog", "separator"',
            '"open-providers-models", "open-runtime-settings", "open-codex-settings", "open-relay-accounts", "separator"',
            '"webdav-status", "open-webdav-settings", "separator"',
            '"open-logs", "separator"',
            '"show-version", "quit"',
        ):
            self.assertIn(ordered_item, self.macos_leaf, ordered_item)

    def test_language_uses_the_native_application_menu_without_a_dedicated_screen(self) -> None:
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
        self.assertIn("private func installLanguageMenu(in applicationMenu: NSMenu)", self.macos_leaf)
        self.assertIn("item.state = choice?.checked == true ? .on : .off", self.macos_leaf)

    def test_service_lifecycle_stays_in_the_shared_menu_surface(self) -> None:
        """Lifecycle labels and state-specific enablement are shared by AppKit and WinUI."""
        for marker in (
            'receiveSnapshot({ ...snapshot, service: { ...snapshot.service, state: "starting" } });',
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
