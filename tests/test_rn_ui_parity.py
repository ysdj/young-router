from __future__ import annotations

import json
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
CODE_EDITOR_WEB = ROOT / "rn/packages/shared/src/ui/code-editor/CodeEditorWeb.ts"
CODE_EDITOR_WRAPPER = ROOT / "rn/packages/shared/src/ui/code-editor/CodeEditorWebView.tsx"
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
        cls.windows_leaf = WINDOWS_LEAF.read_text(encoding="utf-8")
        cls.code_editor_web = CODE_EDITOR_WEB.read_text(encoding="utf-8")
        cls.code_editor_wrapper = CODE_EDITOR_WRAPPER.read_text(encoding="utf-8")
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
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        self.assertIn('const bootstrapTranslate = createTranslator("system", systemLocale);', self.platform_entry)
        self.assertIn('import { routeMenuActions } from "./routes";', self.platform_entry)
        self.assertIn('routeMenuActions(bootstrapTranslate)', self.platform_entry)
        self.assertIn('{ id: "logs", titleKey: "menu.logs" }', routes)
        self.assertIn('id !== "claude-settings" && id !== "relay-add"', routes)

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
        self.assertIn('...routeMenuActions(translate).filter(({ id }) => id !== "open-data-management" && id !== "open-logs")', self.ui)
        self.assertIn('...routeMenuActions(translate).filter(({ id }) => id === "open-data-management")', self.ui)

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
            '"relay.typeDetected": "已识别: {type}"',
        ):
            self.assertIn(marker, chinese)
        self.assertIn('{translate("providers.provider")}: {providerLabel}', self.ui)
        self.assertIn('{translate("common.default")}: {defaultValue}', self.ui)
        self.assertIn('return details.join(" | ");', self.ui)

    def test_assistant_settings_use_one_shared_surface_with_active_domain_actions(self) -> None:
        for marker in (
            'type AssistantSettingsDomain = "codex" | "claude";',
            'const settingsRoute = isAssistantSettingsRoute(route);',
            'const domain = settingsRoute ? undefined : domainForRoute(route);',
            '<AssistantSettingsWorkspace',
            'assistantSettingsScroll',
            'assistantQuickGrid',
            'assistantRawGrid',
            'await flushPendingFields();',
            'const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {',
            'if (settingsRoute) {',
            'return (["codex", "claude"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
            'const dirtyDomains = stagedDomainsForRoute(current);',
            'for (const name of dirtyDomains) {',
            'const needsDiscardConfirmation = routeHasStagedChanges(current);',
            'if (!needsDiscardConfirmation) {',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('selected={settingsTab}', self.ui)
        self.assertNotIn('<WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]}', self.ui)
        self.assertNotIn('patch_deployment', self.ui)
    def test_apply_serializes_pending_core_field_commits(self) -> None:
        flush = self.ui.split("const flushPendingFields = async (): Promise<void> => {", 1)[1].split(
            "const hasPendingFieldEdits",
            1,
        )[0]
        self.assertIn("for (const field of [...pendingFields.current.values()])", flush)
        self.assertIn("await field.commit();", flush)
        self.assertNotIn(
            "Promise.all([...pendingFields.current.values()].map((field) => field.commit()))",
            flush,
        )

    def test_shared_assistant_apply_is_not_blocked_by_the_undefined_default_domain(self) -> None:
        apply_body = self.ui.split('const apply = (): Promise<void> => {', 1)[1].split(
            "const applyDataManagement",
            1,
        )[0]
        self.assertIn('if ((!settingsRoute && route !== "relay-accounts" && !domain) || domain === "logs")', apply_body)
        self.assertIn('settingsRoute || route === "relay-accounts"', apply_body)
        self.assertIn("stagedDomainsForRoute(refreshed)", apply_body)

    def test_route_close_and_apply_share_one_dirty_projection(self) -> None:
        for marker in (
            'const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {',
            'const actionSnapshot = latestSnapshot.current && latestSnapshot.current.revision > (snapshot?.revision ?? -1)',
            'const routeHasStagedChanges = useCallback((currentSnapshot: CoreSnapshot | undefined): boolean => (',
            'return (["relay_accounts", "providers_models"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
            '|| hasPendingFieldEdits()',
            'const needsDiscardConfirmation = routeHasStagedChanges(current);',
            'disabled={busy || !routeHasStagedChanges(actionSnapshot)}',
        ):
            self.assert_ui_has(marker)
        self.assertEqual(2, self.ui.count('disabled={busy || !routeHasStagedChanges(actionSnapshot)}'))
        self.assertNotIn('disabled={busy || stagedDomainsForRoute(snapshot).length === 0}', self.ui)
        self.assertNotIn('snapshot?.drafts.codex?.dirty || snapshot?.drafts.claude?.dirty || hasClaudeDeploymentChanges(snapshot) || hasPendingFieldEdits()', self.ui)

    def test_compatibility_claude_route_reuses_the_combined_native_window(self) -> None:
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        for marker in (
            'import { canonicalWindowRoute, LOG_TABS, routeMenuActions, ROUTES } from "../routes";',
            'const windowRoute = canonicalWindowRoute(routeRequest);',
            'native.window.open(windowRoute);',
            'native.window.focus(windowRoute);',
            'native.window.close(canonicalWindowRoute(route))',
        ):
            self.assert_ui_has(marker)
        self.assertIn('return route === "claude-settings" ? "codex-settings" : route;', routes)

    def test_desktop_route_actions_reopen_the_same_route_and_reset_bare_logs(self) -> None:
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn("const [routeRequestSequence, setRouteRequestSequence] = useState(0);", bootstrap)
        self.assertIn("setRouteRequestSequence((current) => current + 1);", bootstrap)
        self.assertIn('setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");', bootstrap)
        self.assertIn("routeRequestSequence?: number;", self.ui)
        self.assertIn("[isPrimaryHost, isWindowManagerHost, native, routeRequest, routeRequestSequence]", self.ui)

    def test_shared_snapshot_refresh_does_not_reset_native_window_geometry(self) -> None:
        self.assertNotIn("const windowSpecs = {", self.ui)
        # Data Management deliberately resizes its compact, content-driven
        # surface when its active tab or detected import set changes.  The
        # shared snapshot refresh path must remain geometry-neutral.
        self.assertIn("const resizeDataManagement = useCallback", self.ui)
        refresh = self.ui.split("const refresh = async (): Promise<CoreSnapshot> => {", 1)[1].split(
            "  const onSecretState",
            1,
        )[0]
        self.assertNotIn("setContentSize", refresh)
        self.assertIn("}, [isPrimaryHost, native, snapshotLanguage, translate]);", self.ui)

    def test_provider_probe_applies_a_changed_recommendation_without_locking_the_workspace(self) -> None:
        for marker in (
            'title={probing ? translate("providers.probing") : translate("providers.probe")}',
            'function modelProbePresentation(',
            'translate("providers.probeSummaryAvailable"',
            'tooltip={probeDetailHint}',
            'accessibilityHint={probeDetailHint}',
            'void native.showReadOnlyText({',
            'language: "text"',
            'translate("providers.probeOriginalRequest"',
            'const applyProbedSurface: ApplyProbedSurface = (providerId, modelId, nextSurface, options) => {',
            'const currentSurface = stringValue(currentModel.upstream_url_surface, "openai/responses");',
            'if (currentSurface === nextSurface) return;',
            'await enqueueDispatch("model.patch", {',
            'await ipc.apply("providers_models", staged.revision, confirmations);',
            'upstream_url_surface: nextSurface,',
            'const nextSurface = stringValue(result.recommended_surface);',
            'isProbeSurface(nextSurface)',
            'dispatch("model.add_many", {',
            'models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, api_key_name: apiKeyName, enabled: true, order: 0 }))',
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
        self.assert_ui_has('{probePresentation.compact ? <Pressable')
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
            '<ActionButton title={probing ? translate("providers.probing") : translate("providers.probe")} disabled={busy || probing || !probeReady} onPress={probe} />'
        )
        self.assert_ui_has('const probeReady = Boolean(')
        self.assertNotIn('await flushPendingFields();\n      const before = await ipc.snapshot();', self.ui)

    def test_provider_selection_and_new_models_keep_independent_stable_state(self) -> None:
        self.assert_ui_has('onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }}')
        self.assert_ui_has('const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);')
        self.assertNotIn('knownModelIdsByProvider', self.ui)
        self.assertNotIn('Promise.all(added.map(', self.ui)
        self.assert_ui_has('disabled={busy || probing || !probeReady}')

    def test_new_models_use_zero_order_without_coercing_zero_to_one(self) -> None:
        self.assert_ui_has('model: { name: "", upstream_model: "", enabled: true, order: 0 }')
        self.assert_ui_has('models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, api_key_name: apiKeyName, enabled: true, order: 0 }))')
        self.assert_ui_has('value={String(displayedOrder)}')
        self.assert_ui_has('label={translate("providers.order")}')
        self.assert_ui_has('label={translate("providers.followMultiplier")}')
        self.assert_ui_has('const canFollowMultiplier = usesRelayKey && relayMultiplier !== undefined;')
        self.assert_ui_has('{canFollowMultiplier ? <NativeCheckbox')
        self.assert_ui_has('const order = Number.isFinite(parsed) ? parsed : 0;')
        self.assert_ui_has('changes: { manual_order: order, order }')
        self.assertNotIn('models.length + index + 1', self.ui)
        self.assertNotIn('Number(order) || 1', self.ui)

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
        self.assert_ui_has("providerEditorContent: { flex: 1, minHeight: 0, paddingTop: 3, paddingLeft: 0, paddingRight: 8, paddingBottom: 12")
        self.assert_ui_has('providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('inspectorContent: { paddingTop: 3, paddingLeft: 0, paddingRight: 6')
        self.assert_ui_has("providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }")
        self.assert_ui_has("providerListPane: { width: 154, minWidth: 154, maxWidth: 154")
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 88 }, { label: translate("providers.modelCount"), width: 64 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.model"), width: 96 }, { label: translate("providers.upstream"), width: 112 }, { label: translate("providers.providerKey"), width: 128 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.key"), width: 260 }]}')
        self.assert_ui_has('cellHorizontalPadding={0}')
        self.assert_ui_has('firstColumnHorizontalPadding={0}')
        self.assert_ui_has('label={translate("providers.keyName")} labelWidth={68}')
        self.assert_ui_has('NativeSecretField plainText autoCommit label={translate("providers.keyValue")}')
        self.assert_ui_has('<View style={styles.providerKeyActions}>')
        self.assert_ui_has('providerKeyTable: { width: "100%", height: 112, minHeight: 112, flexShrink: 0 }')
        self.assert_ui_has('providerKeyFields: { minWidth: 0, gap: 4 }')
        self.assert_ui_has('label={translate("providers.provider")} labelWidth={60} allowShrink')
        self.assert_ui_has('label={translate("providers.protocolMode")} labelWidth={60} allowShrink')
        self.assert_ui_has('pickerShrink: { minWidth: 0 }')
        self.assert_ui_has('allowShrink && styles.pickerShrink')
        self.assertNotIn('providerKeyGrid:', self.ui)
        self.assertNotIn('providerKeyList:', self.ui)
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('return <ProviderWorkspaceDraftContext.Provider value={providerDraftProjection}><View style={styles.providersLayout}>')
        self.assert_ui_has('<View style={styles.providerLeftColumn}>')
        self.assert_ui_has('{viewMode === "routes" ? <View style={styles.routeWorkspace}>')
        self.assert_ui_has('<TablePane wide style={styles.routeTablePane}')
        self.assert_ui_has('<View style={styles.providerInspector}>')
        self.assertEqual(1, self.ui.count('<View style={styles.providerInspector}>'))
        self.assertNotIn('viewMode === "routes" ? <View style={styles.providerWorkspace}', self.ui)
        self.assertNotIn('routeWorkspaceWithInspector', self.ui)
        self.assert_ui_has('routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('onSelectionChange={selectRoute}')
        self.assert_ui_has('label: translate("providers.provider"), width: 116 }, { label: translate("providers.providerKey"), width: 166')
        self.assert_ui_has('label: translate("common.order"), width: 72 }, { label: translate("providers.upstream"), width: 136')
        self.assertNotIn('providers.orderSource', self.ui)
        self.assertNotIn('providers.effectiveOrder', self.ui)
        self.assert_ui_has('modelOrderMode(activeRoute.model) === "relay_multiplier"')
        self.assertNotIn('const displayRoutes = useMemo', self.ui)
        self.assert_ui_has('key: `route-public-model:${entry.publicModel}`')
        self.assert_ui_has('spanning: true')
        self.assert_ui_has(r'cells: [`\t${providerDisplayName(entry.provider)}`')
        self.assert_ui_has('rows={routeRows} disabledRowKeys={disabledRouteKeys} selectedKey={selectedRoute ?? ""} compact onSelectionChange={selectRoute}')
        self.assert_ui_has('rows={providerRows} disabledRowKeys={disabledProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange=')
        self.assert_ui_has('rows={modelRows} disabledRowKeys={disabledModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} onSelectionChange=')
        self.assert_ui_has('columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths')
        self.assert_ui_has('rows={keyRows} selectedKey={selectedChoice?.id ?? ""} compact cellHorizontalPadding={0} firstColumnHorizontalPadding={0} onSelectionChange={setSelectedKeyID}')
        select_route = self.ui.split('const selectRoute = useCallback', 1)[1].split('const chooseViewMode', 1)[0]
        self.assertLess(select_route.index('if (!selected) return;'), select_route.index('setSelectedRoute(routeId);'))
        self.assert_ui_has('providerSourceModel ? <ProviderEditor key={`provider:${editorIdentifier(activeRoute.provider)}`} provider={activeRoute.provider}')
        self.assert_ui_has('model={activeRoute.model}')
        self.assert_ui_has('onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))}')
        self.assert_ui_has('setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`)')
        self.assert_ui_has('disabledRowKeys={disabledModelKeys}')
        self.assert_ui_has('disabledRowKeys={disabledRouteKeys}')
        self.assertNotIn('secondaryCellKeys={routeSecondaryCellKeys}', self.ui)
        self.assertNotIn('<ScrollView contentContainerStyle={styles.inspectorContent}>', self.ui)
        self.assertNotIn('native.window.open("relay-accounts")', self.ui)
        self.assert_ui_has('route === "relay-accounts" ? <View style={serviceProviderStyles.workspace}')
        self.assert_ui_has('route === "relay-accounts" ? <View style={serviceProviderStyles.workspace}')
        self.assert_ui_has('rows={serviceProviderRows}')
        self.assert_ui_has('serviceProviderSelection?.startsWith("provider:")')
        self.assert_ui_has('renderRelayManager({ setupOnly: false, hideNavigation: true')
        self.assert_ui_has('route === "relay-add" ? <RelayAccountManager visible setupOnly')
        self.assertNotIn('serviceProviderTab', self.ui)

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

    def test_unified_data_management_tabs_switch_one_content_surface_at_a_time(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'type DataManagementTab = "import" | "export" | "webdav";',
            'function DataManagementWorkspace(',
            '<WindowTabs values={[',
            '{ id: "import", title: translate("dataManagement.tab.import") }',
            '{ id: "export", title: translate("dataManagement.tab.export") }',
            '{ id: "webdav", title: translate("dataManagement.tab.webdav") }',
            'selected={tab}',
            'onSelect={(next) => switchDataManagementTab(next as DataManagementTab)}',
            'const switchDataManagementTab = (next: DataManagementTab): void => {',
            'const previous = tab;',
            'const pending = onFlushPendingFields();',
            'setTab(next);',
            'void pending.catch((reason: unknown) => {',
            'onTabSwitchError(previous, reason);',
            '{tab === "import" ? <ScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent, !importPreview && dataManagementPolishStyles.importLandingContent]}>',
            '{tab === "export" ? <View style={styles.dataManagementPane}>',
            '{tab === "webdav" ? <View style={[styles.dataManagementWebDavPane, styles.dataManagementWebDavContent, dataManagementPolishStyles.webDavContent]}>',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('<ScrollView style={styles.dataManagementWebDavPane}', workspace)
        self.assertEqual(3, workspace.count('{tab === "'))

    def test_data_management_status_messages_stay_with_their_originating_tab(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'const runDataManagement = (tab: DataManagementTab, operation: () => Promise<unknown>, message: string | null, keepControlsEnabled = false): Promise<void> => run(operation, message, keepControlsEnabled, true, tab);',
            'statuses={dataManagementStatuses}',
            'onTabSwitchError={(tab, reason) => setDataManagementStatuses((current) => ({ ...current, [tab]: errorMessage(reason, translate) }))}',
            'statuses.import ? <Text',
            'statuses.export ? <Text',
            'status={statuses.webdav}',
            'const [dataManagementStatuses, setDataManagementStatuses] = useState<Partial<Record<DataManagementTab, string>>>({});',
            'const publishResult = (next: string | undefined): void => {',
            'setDataManagementStatuses((current) => ({ ...current, [dataManagementTab]: next }))',
            'const dispatchDataManagement: Dispatch = (type, payload = {}, targetDomain = "webdav")',
        ):
            self.assert_ui_has(marker)
        export_pane = workspace.split('{tab === "export"', 1)[1].split(
            '{tab === "webdav"',
            1,
        )[0]
        self.assertNotIn('{status ? <Text', export_pane)

    def test_import_starts_with_file_selection_while_export_defaults_to_every_section(self) -> None:
        section_block = self.ui.split("const DATA_PACKAGE_SECTIONS", 1)[1].split(
            "];",
            1,
        )[0]
        for domain in (
            "providers_models",
            "runtime",
            "relay_accounts",
            "codex",
            "claude",
            "webdav",
            "language",
        ):
            self.assertIn(f'{{ domain: "{domain}",', section_block)
        self.assert_ui_has('const DATA_PACKAGE_DOMAINS = DATA_PACKAGE_SECTIONS.map(({ domain }) => domain);')
        self.assert_ui_has('const [importSections, setImportSections] = useState<ConfigDomain[]>([]);')
        self.assert_ui_has('const [exportSections, setExportSections] = useState<ConfigDomain[]>([...DATA_PACKAGE_DOMAINS]);')

        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'const [importPreview, setImportPreview] = useState<IpcResults["import_preview"]>();',
            '!importPreview ? <View style={[styles.dataManagementImportIntro, dataManagementPolishStyles.importIntro]}><View style={styles.dataManagementImportFileRow}><Text style={styles.dataManagementImportFileLabel}>{translate("dataManagement.importFile")}</Text>',
            '<Text numberOfLines={1} style={styles.dataManagementImportFilePlaceholder}>{translate("dataManagement.noImportFile")}</Text>',
            '<Text style={dataManagementPolishStyles.paneHint}>{translate("dataManagement.importHint")}</Text>',
            '{importReviewReady ? <>',
            '{sectionList(detectedImportSections, importSections, busy,',
            'title={translate("dataManagement.chooseImportFile")}',
        ):
            self.assertIn(marker, workspace)
        self.assertNotIn(
            'sectionList(DATA_PACKAGE_DOMAINS, importSections',
            workspace,
        )

    def test_import_preview_detects_sections_and_selects_only_detected_items_by_default(self) -> None:
        ipc = (ROOT / "rn/packages/shared/src/ipc.ts").read_text(encoding="utf-8")
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        for marker in (
            'const inspectImportDataManagement = async (): Promise<IpcResults["import_preview"] | undefined> => {',
            'const fileToken = await native.openFilePicker({ purpose: "import" });',
            'inspected = await ipc.previewImport(fileToken, revision.current ?? 0);',
            'importPlanToken.current = inspected.import_plan_token;',
            'const detected = DATA_PACKAGE_DOMAINS.filter((domain) => inspected.detected_sections.includes(domain));',
            'setImportSections(detected);',
            'const importReviewReady = importPreview !== undefined && stagedSections.length === 0;',
            'selectionTool(importSections.length, detectedImportSections.length, () => setImportSections([...detectedImportSections]), () => setImportSections([]))',
            'const allSelected = availableCount > 0 && selectedCount === availableCount;',
            'title={translate(allSelected ? "dataManagement.deselectAll" : "dataManagement.selectAll")}',
            'const imported = await onImport(importSections);',
        ):
            self.assert_ui_has(marker)
        self.assertIn(
            'previewImport: async (sourceToken: string, revision: number): Promise<IpcResults["import_preview"]> => call("import_preview", { source_token: sourceToken, revision })',
            ipc,
        )
        self.assertIn(
            'importPlan: async (importPlanToken: string, revision: number, sections: ConfigDomain[]): Promise<IpcResults["import"]> => call("import", { import_plan_token: importPlanToken, sections, revision })',
            ipc,
        )
        self.assertIn('| "import_preview"', types)
        self.assertIn('import_preview: { source_token: string; revision: number };', types)
        self.assertIn('import_plan_token: string;', types)
        self.assertIn('imported = await ipc.importPlan(planToken, revision.current ?? 0, sections);', self.ui)
        self.assertNotIn('ipc.import(fileToken', self.ui)

    def test_data_management_window_uses_balanced_tab_specific_content_sizes(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'const windows = Platform.OS === "windows";',
            '{ width: windows ? 660 : 640, height: windows ? 520 : 480 }',
            '{ width: windows ? 640 : 620, height: windows ? 340 : 320 }',
            'const importHeight = importItemCount === 0',
            '? windows ? 168 : 148',
            ': importItemCount <= 2',
            '? windows ? (replacingDraftSections.length > 0 ? 320 : 285) : (replacingDraftSections.length > 0 ? 305 : 270)',
            ': windows ? (replacingDraftSections.length > 0 ? 385 : 350) : (replacingDraftSections.length > 0 ? 370 : 335)',
            '{ width: windows ? 620 : 600, height: importHeight }',
            '{ width: windows ? 640 : 620, height: importHeight }',
            '{ width: windows ? 660 : 640, height: importHeight }',
            'void onResize(size.width, size.height);',
        ):
            self.assertIn(marker, workspace)
        self.assert_ui_has('const dataManagementPolishStyles = StyleSheet.create({')
        self.assert_ui_has('tabBar: { height: 42, minHeight: 42 }')
        self.assert_ui_has('tabs: { width: 360, height: 28 }')
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: 14, paddingHorizontal: 12, paddingBottom: 14, gap: 14 }')
        self.assert_ui_has('importLandingContent: { justifyContent: "center" }')
        self.assert_ui_has('importIntro: { minHeight: 0, paddingHorizontal: 12, paddingVertical: 0, gap: 10 }')
        self.assert_ui_has('paneHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }')
        self.assert_ui_has('compactText: { fontSize: UI_FONT_SIZE, lineHeight: 16 }')
        self.assert_ui_has('dataManagementGroup: { gap: 6 }')
        self.assert_ui_has('dataManagementSectionPicker: { flexDirection: "row", flexWrap: "wrap"')
        self.assertNotIn('dataManagementWarningPanel:', self.ui)
        self.assertNotIn('dataManagementImportEmpty:', self.ui)
        self.assertNotIn('setContentSize?.("data-management", Platform.OS === "windows" ? 740 : 720', self.ui)

    def test_export_action_uses_the_compact_bottom_right_row(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        export_pane = workspace.split('{tab === "export"', 1)[1].split(
            '{tab === "webdav"',
            1,
        )[0]
        self.assertIn('<View style={styles.dataManagementPane}>', export_pane)
        self.assertIn('<ScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>', export_pane)
        self.assertIn('<View style={[styles.dataManagementBottomActions, dataManagementPolishStyles.bottomActions]}>', export_pane)
        self.assertGreater(
            export_pane.index('<View style={[styles.dataManagementBottomActions, dataManagementPolishStyles.bottomActions]}>'),
            export_pane.index('</ScrollView>'),
        )
        self.assertIn('<View style={styles.dataManagementBottomMessage}>', export_pane)
        self.assertIn('title={translate("dataManagement.exportSelected")}', export_pane)
        self.assertLess(
            export_pane.index('styles.dataManagementSensitiveNote, dataManagementPolishStyles.compactText'),
            export_pane.index('title={translate("dataManagement.exportSelected")}'),
        )
        self.assertIn('<ActionButton primary title={translate("dataManagement.exportSelected")}', export_pane)
        for header_tip in (
            'description={translate("dataManagement.importHint")}',
            'description={translate("dataManagement.importRecognizedHint")}',
            'description={translate("dataManagement.exportHint")}',
        ):
            self.assertNotIn(header_tip, workspace)
        self.assert_ui_has('bottomActions: { minHeight: 58, flexShrink: 0, alignItems: "center", paddingHorizontal: 12, paddingTop: 10, paddingBottom: 14, borderTopWidth: 1, borderTopColor: systemColors.separator }')
        self.assert_ui_has('dataManagementBottomMessage: { flex: 1, minWidth: 0, gap: 2 }')
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: 14, paddingHorizontal: 12, paddingBottom: 14, gap: 14 }')
        self.assert_ui_has('compactText: { fontSize: UI_FONT_SIZE, lineHeight: 16 }')

    def test_data_management_uses_native_preference_groups_not_web_cards(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            "webdavSyncArea:",
            "dataManagementToolbarButtons:",
            "const WEBDAV_FORM_LABEL_WIDTH = 108;",
            'const labelAlign = "left";',
            'dataManagementSyncScopeLabel: { width: WEBDAV_FORM_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }',
            'dataManagementDirectionLabel: { width: WEBDAV_FORM_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" }',
        ):
            self.assert_ui_has(marker)
        for legacy_card in (
            "dataManagementOutlinedPanel:",
            "dataManagementCardHeader:",
            "dataManagementCardTitle:",
            "dataManagementSyncCard:",
            "dataManagementInlineActionRow:",
        ):
            self.assertNotIn(legacy_card, self.ui)
        self.assertNotIn('dataManagementWarningPanel:', self.ui)
        self.assertNotIn('webDavForm: { flexGrow: 0, borderWidth:', self.ui)
        self.assertNotIn('webdavStateRow: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 6, marginLeft:', self.ui)
        self.assertNotIn('dataManagementSelectionList:', self.ui)
        self.assertIn('dataManagementSectionPicker: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 14, rowGap: 2', self.ui)
        self.assertNotIn('dataManagementSectionRow:', self.ui)
        self.assertNotIn('dataManagementSectionGrid:', self.ui)
        self.assertNotIn('dataManagementSectionRowFirstColumn:', self.ui)
        self.assertNotIn('dataManagementSectionRowWide:', self.ui)
        self.assertIn('dataManagementSelectionBar: { minHeight: 24, flexDirection: "row", alignItems: "center", gap: 8 }', self.ui)
        self.assertNotIn('dataManagementGroupHeader:', self.ui)
        self.assertNotIn('dataManagementGroupTitle:', self.ui)
        self.assertNotIn('DataManagementGroup title=', workspace)
        self.assertIn('dataManagementImportFileRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: 8 }', self.ui)
        self.assertIn('dataManagementImportFileValue: { flex: 1, minWidth: 0, minHeight: 26, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.textBackground }', self.ui)
        self.assertIn('<NativePicker labels={syncOptions.map(({ title }) => title)} selectedValue={selectedSyncLabel}', workspace)
        self.assertIn('dataManagementDirectionPicker: { width: 210, height: 24', self.ui)
        self.assertNotIn('<WindowTabs values={syncOptions}', workspace)
        self.assertNotIn('<ActionButton primary title={translate("dataManagement.chooseImportFile")}', workspace)
        self.assertNotIn('<ActionButton primary title={translate("dataManagement.importSelected")}', workspace)
        self.assertIn('<ActionButton primary title={translate("dataManagement.exportSelected")}', workspace)
        self.assertNotIn('<ActionButton primary title={translate("dataManagement.testConnection")}', workspace)
        self.assertNotIn('<ActionButton primary title={translate("dataManagement.syncNow")}', workspace)

    def test_import_apply_uses_only_the_sections_staged_by_core(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'const [stagedSections, setStagedSections] = useState<ConfigDomain[]>([]);',
            'setStagedSections(DATA_PACKAGE_DOMAINS.filter((domain) => imported.draft_domains.includes(domain)));',
            '{sectionList(stagedSections, stagedSections, true, () => undefined)}',
            '{importedDirty ? <ActionButton title={translate("menu.apply")}',
            'void onApplyImported(stagedSections);',
        ):
            self.assertIn(marker, workspace)
        import_selected = workspace.split(
            'const importSelected = async (): Promise<void> => {',
            1,
        )[1].split(
            'const hasWebDavChanges =',
            1,
        )[0]
        self.assertNotIn('onApplyImported(', import_selected)
        self.assertNotIn('onApplyImported(importSections)', workspace)

    def test_data_management_has_no_bottom_action_bar(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        self.assertIn('dataManagementToolbarButtons', workspace)
        self.assertNotIn('dataManagementFooter', workspace)
        self.assertNotIn('<DialogFooter', workspace)
        self.assertNotIn('title={translate("menu.close")}', workspace)
        self.assertNotIn('onClose:', workspace)
        self.assertIn('{importedDirty ? <ActionButton title={translate("menu.apply")}', workspace)
        self.assertNotIn('{importedDirty ? <ActionButton primary', workspace)
        webdav = self.ui.split("function WebDavWorkspace(", 1)[1].split("function RuntimeField(", 1)[0]
        self.assertIn('title={translate("dataManagement.testConnection")}', webdav)
        self.assertIn('webdavActionRow', webdav)
        self.assertIn('webdavActionSpacer', webdav)
        self.assertIn('title={translate("common.saveAndApply")} disabled={busy || !hasChanges}', webdav)
        self.assertNotIn('{hasChanges ? <ActionButton primary title={translate("menu.apply")}', webdav)

    def test_provider_and_runtime_surfaces_have_no_individual_transfer_entry(self) -> None:
        provider_workspace = self.ui.split("function ProviderWorkspace(", 1)[1].split(
            "function ProviderList(",
            1,
        )[0]
        runtime_workspace = self.ui.split("function RuntimeWorkspace(", 1)[1].split(
            "function DataManagementWorkspace(",
            1,
        )[0]
        for removed in (
            "const transferActions = [",
            "transferButtonRef",
            'translate("providers.currentCodex")',
            'translate("providers.currentClaude")',
            'translate("providers.configurationFile")',
            'translate("providers.exportFile")',
            'providers.import_selected',
        ):
            self.assertNotIn(removed, provider_workspace)
        for removed in (
            "runtimeFileToolbar",
            "openFilePicker",
            "saveFilePicker",
            "ipc.import",
            "ipc.export",
        ):
            self.assertNotIn(removed, runtime_workspace)

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
            {"UI_FONT_SIZE"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", relay)},
        )
        self.assertEqual(
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", self.native_controls)},
        )
        self.assertIn("runtimeHelpText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)
        self.assertIn("fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)
        self.assertIn("formHint: { color: colors.secondary, fontSize: UI_FONT_SIZE", relay)

        self.assertIn("constexpr CGFloat LiteLLMUIFontSize = 13.0;", macos_controls)
        self.assertNotIn("systemFontSizeForControlSize", macos_controls)
        self.assertNotRegex(macos_controls, r"(?:systemFontOfSize|monospacedSystemFontOfSize):\d")
        self.assertIn("column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);", macos_controls)
        self.assertIn("NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightMedium]", macos_controls)

        self.assertIn("private let nativeUIFontSize: CGFloat = 13", self.macos_leaf)
        self.assertIn("private final class NativeReadOnlyCodeController", self.macos_leaf)

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
        self.assert_ui_has('void dispatch("logs.clear_recovery_and_cooldowns", { tab }, "logs").then(async () => {')
        self.assert_ui_has('NativeButton title={translate("logs.clearRecoveryCooldown")}')
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

    def test_data_management_copy_names_unified_sections_and_actions(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for text in (english, chinese):
            for key in (
                "menu.dataManagement",
                "dataManagement.tab.import",
                "dataManagement.tab.export",
                "dataManagement.tab.webdav",
                "dataManagement.section.providersModels",
                "dataManagement.section.runtime",
                "dataManagement.section.relayAccounts",
                "dataManagement.section.codex",
                "dataManagement.section.claude",
                "dataManagement.section.webdavSettings",
                "dataManagement.section.language",
                "dataManagement.importHint",
                "dataManagement.importRecognizedHint",
                "dataManagement.importDetectedCount",
                "dataManagement.chooseImportFile",
                "dataManagement.changeImportFile",
                "dataManagement.importReplaceDraftWarning",
                "dataManagement.importInspected",
                "dataManagement.importSelected",
                "dataManagement.exportSelected",
                "dataManagement.syncSettings",
            ):
                self.assertIn(f'"{key}":', text)
        for value in (
            '"menu.dataManagement": "Import, Export & Sync"',
            '"dataManagement.tab.import": "Import"',
            '"dataManagement.tab.export": "Export"',
            '"dataManagement.tab.webdav": "WebDAV Sync"',
            '"dataManagement.section.providersModels": "Providers & Models"',
            '"dataManagement.section.relayAccounts": "Service Provider Management"',
            '"dataManagement.importHint": "Choose a file to detect its importable configuration automatically."',
            '"dataManagement.importRecognizedHint": "These configuration areas were detected in the selected file and are selected by default."',
            '"dataManagement.syncSettings": "Sync settings"',
        ):
            self.assertIn(value, english)
        for value in (
            '"menu.dataManagement": "导入、导出与同步"',
            '"dataManagement.tab.import": "导入"',
            '"dataManagement.tab.export": "导出"',
            '"dataManagement.tab.webdav": "WebDAV 同步"',
            '"dataManagement.section.providersModels": "供应商与模型"',
            '"dataManagement.section.relayAccounts": "服务商管理"',
            '"dataManagement.importHint": "选择文件后会自动识别可导入的配置项。"',
            '"dataManagement.importRecognizedHint": "以下为文件中识别到的配置项，默认全选。"',
            '"dataManagement.syncSettings": "同步设置"',
        ):
            self.assertIn(value, chinese)

    def test_claude_permission_picker_includes_the_official_delegate_mode(self) -> None:
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function AssistantSettingsWorkspace", 1
        )[0]
        self.assertIn('translate("claude.desktopProvider")', claude)
        self.assertNotIn('translate("claude.permissions")', claude)
        self.assertNotIn("CLAUDE_PERMISSION_MODES", claude)

    def test_claude_structured_forms_are_named_for_their_single_column_layout(self) -> None:
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function AssistantSettingsWorkspace", 1
        )[0]
        self.assertIn("assistantSettingsStyles.domainBody", claude)
        self.assertIn("assistantSettingsStyles.quickFields", claude)
        self.assertIn("assistantQuickGrid", self.ui)
        self.assertNotIn("twoColumnForm", self.ui)

    def test_claude_settings_expose_only_the_compact_safe_capability_controls(self) -> None:
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function AssistantSettingsWorkspace", 1
        )[0]
        for marker in (
            'translate("claude.desktopProvider")',
            'translate("common.model")',
            'translate("claude.desktopGateway")',
            'field="desktop_gateway_api_key"',
        ):
            self.assertIn(marker, claude)
        self.assertNotIn('translate("claude.deployment")', claude)
        self.assertNotIn('field="deployment_token"', claude)
        for removed in (
            'translate("claude.permissions")',
            'translate("claude.capabilities")',
            'translate("claude.sandbox")',
            'translate("claude.memory")',
        ):
            self.assertNotIn(removed, claude)

    def test_relay_metadata_commits_before_native_credential_cleanup_and_persists_a_retry(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        route = self.ui.split("const commitRelayMetadata", 1)[1].split("const flushPendingFields", 1)[0]
        deletion = relay.split("const clearRemovedAccountCredentials", 1)[1].split("const openLocalRemoval", 1)[0]
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
            deletion.index('await commit("account.delete"'),
            deletion.index("await clearRemovedAccountCredentials([account.id])"),
        )
        self.assertIn("await native.clearRelayCredentials(accountID)", deletion)
        self.assertIn('kind: "credentials"', deletion)
        self.assertIn("secret-free cleanup tombstone", deletion)
        self.assertLess(
            password.index('await commit("account.update"'),
            password.index("await native.clearRelayPassword(accountID)"),
        )
        self.assertNotIn('kind: "password"', password)
        self.assertNotIn('credential_cleanup_confirm', password)
        self.assertIn('translate("relay.retryCleanup")', relay)

    def test_native_tables_support_platform_list_chrome_and_grouping(self) -> None:
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
            self.assertIn("borderless?: WithDefault<boolean, false>;", spec)
            self.assertIn("secondaryCellKeys?: ReadonlyArray<string>;", spec)
            self.assertIn("spanningRowKeys?: ReadonlyArray<string>;", spec)
        self.assertIn("striped = true, alternatingRows = false", self.native_controls)
        self.assertIn("const stripedRows = striped && (alternatingRows || rows.length > 0);", self.native_controls)
        self.assertIn("alternatingRows: stripedRows,", self.native_controls)
        self.assertIn("framed = true", self.native_controls)
        self.assertIn("borderless: !framed,", self.native_controls)
        self.assertIn("secondaryCellKeys = []", self.native_controls)
        self.assertIn("secondaryCellKeys,", self.native_controls)
        self.assertIn("const spanningRowKeys = rows.filter((row) => row.spanning).map((row) => row.key);", self.native_controls)
        self.assertIn("spanningRowKeys,", self.native_controls)
        # The fetched-model picker is now a native modal leaf, so its old
        # React table no longer belongs to the shared window tree.
        self.assertEqual(self.ui.count("<NativeTable"), 7)
        self.assertEqual(self.ui.count("alternatingRows"), 0)
        self.assertNotIn("selectedKey={selectedRoute ?? \"\"} alternatingRows", self.ui)
        self.assertNotIn("striped={false}", self.ui)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", mac_native)
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
        self.assertIn("@property(nonatomic, assign, getter=isFramed) BOOL framed;", mac_native)
        self.assertIn("const CGFloat inset = self.framed ? 1.0 : 0.0;", mac_native)
        self.assertIn("_frameView.framed = !newViewProps.borderless;", mac_native)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", table_native)
        self.assertIn("props.alternatingRows.value_or(false)", windows_native)
        self.assertIn("props.borderless.value_or(false) ? Thickness{0, 0, 0, 0} : Thickness{1, 1, 1, 1}", windows_native)
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        self.assertIn("borderless={borderless}", appkit_controls)
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
        self.assertIn("_userResizedColumns.size() == columnCount && !_userResizedColumns.back()", mac_native)
        self.assertIn("_measuredColumnWidths.back() + trailingContentInset", mac_native)
        self.assertNotIn("laidOutColumnWidths[index] = MAX(laidOutColumnWidths[index], _measuredColumnWidths[index]);", mac_native)
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

    def test_runtime_save_and_apply_reloads_the_running_proxy(self) -> None:
        self.assert_ui_has(
            '(domains.includes("providers_models") || domains.includes("runtime")) '
            '&& (refreshed.service.state === "running" || refreshed.service.state === "unhealthy")'
        )
        self.assert_ui_has('await ipc.dispatch({ type: "service.reload" }, result.revision)')

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
        self.assertIn('styles.runtimeMultilineEditor', self.ui)
        self.assertIn('runtimeBooleanControl: { width: 24, minWidth: 24, height: 24', self.ui)
        self.assertNotIn("runtimeBooleanSlot", self.ui)
        self.assertNotIn("runtimeBooleanHelpSlot", self.ui)

    def test_native_text_buttons_reserve_full_titles_after_local_styles(self) -> None:
        """Translated command labels must not be reduced to an ellipsis."""
        self.assertIn(
            "function isCompactGlyphTitle(title: string): boolean",
            self.native_controls,
        )
        self.assertIn(
            "return trimmed.length > 0 && Array.from(trimmed).length <= 2 && !/[\\p{L}\\p{N}]/u.test(trimmed);",
            self.native_controls,
        )
        self.assertIn(
            "return Math.max(72, Math.ceil((compact ? 32 : 38) + nativeControlTextWidth(title) * 1.15));",
            self.native_controls,
        )
        self.assertIn(
            "const titleWidth = !props.symbol && !(compact && isCompactGlyphTitle(props.title))",
            self.native_controls,
        )
        self.assertIn(
            "const style = [props.link ? styles.linkButton : styles.button, props.style, titleWidth];",
            self.native_controls,
        )
        self.assertIn(
            "return <AppKitButton {...buttonProps} ref={ref as never} style={[props.style, titleWidth]} />;",
            self.native_controls,
        )

    def test_icon_buttons_do_not_reserve_text_button_width(self) -> None:
        """Every compact glyph action must stay icon-sized across shared callers."""
        self.assertIn('label === "+" ? "plus"', self.ui)
        self.assertIn('label === "−" ? "minus"', self.ui)
        self.assertIn('label === "⧉" ? "copy"', self.ui)
        self.assertIn('label === "↑" ? "chevron-up"', self.ui)
        self.assertIn('label === "↓" ? "chevron-down"', self.ui)
        self.assertIn('iconButton: { minWidth: 22, width: 22, minHeight: 22, height: 22', self.ui)
        self.assertIn('button: { minWidth: 28, height: 24 }', self.native_controls)
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        self.assertIn('button: { minWidth: 28, height: 24 }', appkit_controls)

    def test_settings_workspaces_keep_their_legacy_layout_roots(self) -> None:
        expected_components = {
            "CodexWorkspace": ("codexWorkspace:",),
            "RuntimeWorkspace": ("runtimeWorkspace:", "runtimeScrollSurface:"),
            "DataManagementWorkspace": ("dataManagementWorkspace:", "dataManagementTabs:"),
            "WebDavWorkspace": ("webDavForm:", "webdavFormRows:"),
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

    def test_assistant_settings_have_one_outer_scroll_surface_without_tabs(self) -> None:
        for marker in (
            '<ScrollView style={styles.assistantSettingsScroll}',
            "assistantSettingsScrollContent",
            "assistantQuickSection",
            "assistantFileSurfaceStyles.filesSection",
            'translate("settings.files")',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("settingsTabBar", self.ui)
        self.assertNotIn("settingsTabs", self.ui)
        self.assertNotIn('<WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]}', self.ui)

    def test_codex_provider_editor_actions_have_a_clear_textual_hierarchy(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        for marker in (
            'translate("codex.provider")',
            'translate("common.model")',
            'translate("codex.gateway")',
            'field="api_key"',
            "const commitProvider",
            "const commitGateway",
            '<TextField label={translate("codex.provider")} value={directProvider}',
        ):
            self.assertIn(marker, codex)
        self.assertNotIn('<PickerField label={translate("codex.provider")}', codex)
        self.assertNotIn("<View style={styles.listToolRail}>", self.ui)

    def test_codex_provider_url_is_only_edited_in_the_provider_detail(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        self.assertIn("const directProvider = stringValue(structured.model_provider);", codex)
        self.assertIn("const provider = providerRows.find((item) => identifier(item) === directProvider);", codex)
        self.assertIn('const gateway = directProvider === "openai"', codex)
        self.assertIn('label={translate("codex.gateway")}', codex)
        self.assertIn("const commitGateway", codex)
        self.assertNotIn('label={translate("providers.baseUrl")}', codex)
        self.assertNotIn('label={translate("common.endpoint")}', codex)

    def test_codex_model_picker_tracks_the_saved_model_without_duplicate_labels(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        self.assertIn(
            "const deploymentModels = [...new Set(deployments.map((item) => stringValue(item.model)).filter(Boolean))];",
            codex,
        )
        self.assertIn(
            '<PickerField label={translate("common.model")} value={displayedModel} values={deploymentModels}',
            codex,
        )
        self.assertIn(
            '<TextField label={translate("common.model")} value={displayedModel} disabled={busy} onDraftChange={setModelDraft}',
            codex,
        )
        self.assertIn(
            'const displayedModel = modelDraft ?? stringValue(structured.model);',
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
        self.assertNotIn('translate("codex.activeDeployment")', codex)

    def test_assistant_plaintext_credentials_have_no_set_or_clear_buttons(self) -> None:
        codex = self.ui.split("function CodexWorkspace", 1)[1].split(
            "function SettingsWorkspace", 1
        )[0]
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function AssistantSettingsWorkspace", 1
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
            "function AssistantSettingsWorkspace", 1
        )[0]
        for screen in (codex, claude):
            self.assertIn("assistantSettingsStyles.domainBody", screen)
            self.assertIn("assistantSettingsStyles.quickFields", screen)
        self.assertIn("assistantSettingsScroll", self.ui)
        self.assertIn("assistantQuickGrid", self.ui)
        self.assertIn("assistantFileSurfaceStyles.fileGroups", self.ui)
        self.assertIn('horizontal={false}', self.ui)
        self.assertIn('showsHorizontalScrollIndicator={false}', self.ui)
        self.assertIn('assistantSettingsLayoutStyles.boundedContent', self.ui)

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

    def test_dsh_router_keeps_advanced_json_below_native_quick_controls(self) -> None:
        schema = (ROOT / "litellm_menu/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        quick_keys = (
            "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED",
            "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND",
            "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK",
            "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
            "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS",
            "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED",
            "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED",
        )
        advanced = schema.index("LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON")
        for key in quick_keys:
            self.assertLess(schema.index(key), advanced)
        self.assertIn("<NativeCheckbox", self.ui)
        self.assertIn("<NativePicker", self.ui)
        self.assertIn("<RuntimeValueField", self.ui)
        self.assertIn("<NativeSecretInputControl", self.ui)

    def test_runtime_metadata_has_complete_chinese_projection(self) -> None:
        schema = (ROOT / "litellm_menu/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        localized = (ROOT / "rn/packages/shared/src/i18n/runtimeSettingsI18n.ts").read_text(encoding="utf-8")
        keys = re.findall(r"'key': '([^']+)'", schema)
        self.assertEqual(61, len(keys))
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIn(f"  {key}: {{ label:", localized)
        for category in ("Timeouts", "Recovery", "Web Search", "Vision Router", "Model Context", "Fallback", "Computer Facade", "MCP", "Logs", "Network", "Service", "Relay"):
            self.assertIn(f'  "{category}":', localized)
        self.assertNotIn("Vision Bridge", localized)
        self.assertNotIn("LITELLM_MENU_VISION_BRIDGE_", schema)
        for key in (
            "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED",
            "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND",
            "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK",
            "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
            "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS",
            "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED",
            "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED",
        ):
            self.assertIn(f"  {key}: {{ label:", localized)
        self.assertIn("const optionValues = stringList(item.options);", self.ui)
        self.assertIn("const next = optionValues[nativeEvent.index];", self.ui)
        self.assertNotIn('const label = stringValue(item.label, key);', self.ui)

    def test_assistant_settings_localize_display_values_without_changing_saved_values(self) -> None:
        assistant_i18n = (ROOT / "rn/packages/shared/src/i18n/assistantSettingsI18n.ts").read_text(encoding="utf-8")
        codex_config = (ROOT / "codex_config.py").read_text(encoding="utf-8")
        for feature in re.findall(r'"([a-z0-9_]+)",', codex_config.split("SUPPORTED_FEATURE_KEYS =", 1)[1].split(")", 1)[0]):
            self.assertIn(f"  {feature}: ", assistant_i18n)
        self.assertNotIn("function FeatureToggles", self.ui)
        self.assertNotIn("codexFeatureLabel", self.ui)
        self.assertNotIn("label={key}", self.ui)
        self.assertIn("function PickerField", self.ui)
        self.assertIn("function ensureSelectedOption", self.ui)
        self.assertIn("A stale/unknown value must remain visible and selected", self.ui)
        self.assertIn("const selectedLabel = options.find((option) => option.value === value)?.label ?? value;", self.ui)
        self.assertIn("const selectedValue = options.find((option) => option.value === value)?.label ?? value;", self.ui)
        self.assertIn("values: Array<string | AssistantSettingOption>", self.ui)
        self.assertIn("const option = options[nativeEvent.index];", self.ui)
        self.assertIn("if (option) onSelect(option.value);", self.ui)
        self.assertIn("assistantSettingOptions(values, translate)", self.ui)
        self.assertIn('translate("card.codexSettings")', self.ui)
        self.assertIn('translate("card.claudeSettings")', self.ui)
        self.assertIn("localizeCodexValidationMessage(message, translate)", self.ui)

    def test_sensitive_settings_use_inline_native_password_controls(self) -> None:
        for marker in (
            "function NativeSecretInputControl(",
            "<NativeSecureTextInput domain={domain}",
            'domain="runtime" field="setting"',
            'domain="webdav" field="password"',
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
        self.assertIn("const RECOVERY_LOG_POLL_MS = 1_000;", self.ui)
        self.assertIn("const ONLINE_USAGE_POLL_MS = 15_000;", self.ui)
        self.assertIn('selected === "recovery"\n      ? RECOVERY_LOG_POLL_MS', self.ui)
        self.assertIn('selected === "online-usage" ? ONLINE_USAGE_POLL_MS : LOG_VIEW_POLL_MS', self.ui)
        self.assertIn("const next = await ipc.diskState(monitoredDiskDomains);", self.ui)
        self.assertIn("const refreshed = await ipc.snapshot();", self.ui)
        disk_watcher = self.ui.split("const monitoredDiskDomains = useMemo", 1)[1].split("const discardPendingFields", 1)[0]
        self.assertNotIn("const next = await ipc.snapshot();", disk_watcher)
        self.assertIn("const diskStateChanged = monitoredDiskDomains.some", self.ui)
        self.assertIn("function sameDiskState(left: DiskState | undefined, right: DiskState | undefined)", self.ui)
        self.assertIn("if (!previous || diskStateChanged)", self.ui)
        self.assertIn("if (hasPendingFieldEdits()) return;", self.ui)
        self.assertIn("revision.current = Math.max(revision.current ?? -1, next.revision);", self.ui)
        self.assertIn("This timer exists to observe external file changes", self.ui)

    def test_raw_editors_surface_loading_staging_and_failures_without_replacing_the_editor(self) -> None:
        raw_editor = self.ui.split("function RawEditor(", 1)[1].split("function modelProbePresentation", 1)[0]

        self.assertIn("const [draft, setDraft] = useState(\"\");", raw_editor)
        self.assertIn("const [baseline, setBaseline] = useState(\"\");", raw_editor)
        self.assertNotIn("setDiff", raw_editor)
        self.assertIn("const [editorRenderRevision, setEditorRenderRevision] = useState(0);", raw_editor)
        self.assertIn("const initializedRef = useRef(false);", raw_editor)
        self.assertIn("const appliedBaselineTokenRef = useRef(baselineToken);", raw_editor)
        self.assertIn("let descriptor = await ipc.editor(domain, document);", raw_editor)
        self.assertIn("if (descriptor.text === submitted) {", raw_editor)
        self.assertIn("const staged = await stageEditorText(editorToken, submitted);", raw_editor)
        self.assertIn("if (isEditorCapabilityConflict(reason)) {", raw_editor)
        self.assertIn("if (descriptor.text === stagedTextRef.current)", raw_editor)
        self.assertIn("const resolution = await onConflict(domain, document);", raw_editor)
        self.assertIn("setError(isEditorCapabilityConflict(reason) ? translate(\"error.generic\") : errorMessage(reason, translate));", raw_editor)
        self.assertNotIn("throw new IpcProtocolError", raw_editor)
        self.assertLess(
            raw_editor.index("if (descriptor.text === stagedTextRef.current)"),
            raw_editor.index("const resolution = await onConflict(domain, document);"),
        )
        self.assertIn("void ipc.editor(domain, document).then((descriptor) => {", raw_editor)
        self.assertIn('<View style={styles.rawNativeEditorFrame}>\n        <CodeEditorWebView', raw_editor)
        self.assertIn('documentKey={`${documentKey}:${editorRenderRevision}`}', raw_editor)
        self.assertIn("value={draft}", raw_editor)
        self.assertIn("baseline={baseline}", raw_editor)
        self.assertIn("showDiff", raw_editor)
        self.assertIn("const resetBaseline = !initializedRef.current", raw_editor)
        self.assertIn("if (resetBaseline || descriptor.baseline !== baselineRef.current) {", raw_editor)
        self.assertIn("baselineRef.current = descriptor.baseline;", raw_editor)
        self.assertIn("setBaseline(descriptor.baseline);", raw_editor)
        self.assertNotIn("baselineRef.current = descriptor.text;", raw_editor)
        self.assertIn('setDocumentKey([domain, document].join(":"));', raw_editor)
        self.assertNotIn('setDocumentKey("")', raw_editor)
        self.assertIn("draftRef.current = text;", raw_editor)
        self.assertIn("normalizeEditorText(text) === normalizeEditorText(draftRef.current)", raw_editor)
        self.assertIn("!initializedRef.current", raw_editor)
        self.assertNotIn("setDraft(text)", raw_editor)
        self.assertIn("onChange={(text) => {", raw_editor)
        self.assertIn("const RAW_EDITOR_SYNC_INTERVAL_MS = 120;", self.ui)
        self.assertIn("const flushAssistantEditorFields = async (): Promise<void> => {", self.ui)
        self.assertIn("field.flushBeforeAssistantEditor === true && field.isDirty?.() === true", self.ui)
        self.assertIn("flushBeforeAssistantEditor: true", self.ui)
        self.assertIn("const openAssistantFile = (target: AssistantFileTarget): void => {", self.ui)
        self.assertIn("void flushAssistantEditorFields()", self.ui)
        self.assertIn(".then(() => setActiveAssistantFile(target))", self.ui)
        self.assertIn("onOpenFile={openAssistantFile}", self.ui)
        self.assertLess(
            self.ui.index("void flushAssistantEditorFields()"),
            self.ui.index(".then(() => setActiveAssistantFile(target))"),
        )
        self.assertIn("void stageLatest(false).catch(() => undefined);", raw_editor)
        self.assertIn("}, RAW_EDITOR_SYNC_INTERVAL_MS);", raw_editor)
        self.assertNotIn("`+${diff.added}  ~${diff.changed}  -${diff.deleted}`", raw_editor)
        self.assertIn('style={styles.rawEditorOverlay}', raw_editor)
        self.assertIn('showLabel = true', raw_editor)
        self.assertIn('syncRevision', raw_editor)
        self.assertNotIn('showReload', raw_editor)
        self.assertNotIn('onPress={reloadEditor}', raw_editor)
        self.assertNotIn("NativeSecureTextEditor", raw_editor)

    def test_code_editor_preserves_language_diff_and_host_contract(self) -> None:
        for marker in (
            'import "ace-builds/src-noconflict/ace";',
            'import "ace-builds/src-noconflict/mode-json";',
            'import "ace-builds/src-noconflict/mode-toml";',
            'import "ace-builds/src-noconflict/ext-searchbox";',
            'type EditorLanguage = "json" | "toml" | "text";',
            "type ReplaceDocumentCommand = {",
            'type SetBaselineCommand = { type: "setBaseline"; baseline: string };',
            'type HostCommand = ReplaceDocumentCommand | SetBaselineCommand | { type: "focus" };',
            "type AceSession = {",
            "type AceEditor = {",
            "type AceApi = { edit: (element: HTMLElement, options?: Record<string, unknown>) => AceEditor };",
            "const CHANGE_SYNC_INTERVAL_MS = 16;",
            "if (changeTimer !== undefined) return;",
            "}, CHANGE_SYNC_INTERVAL_MS);",
            "const DIFF_LCS_CELL_LIMIT = 1_000_000;",
            "function diffHunks(before: string[], after: string[]): DiffHunk[]",
            "function computeDiff(): ComputedEditorDiff",
            "function renderDiffSidebar(): void",
            "function queueDiffSidebar(): void",
            'const diffSidebar = document.getElementById("diff-sidebar");',
            'const diffSidebarList = document.getElementById("diff-sidebar-list");',
            "const DIFF_SIDEBAR_ENTRY_LIMIT = 24;",
            'document.body.classList.toggle("diff-sidebar-enabled", showingDiff);',
            "sidebarList.replaceChildren();",
            'item.className = `diff-sidebar-item diff-sidebar-item-${entry.kind}`;',
            'appendDiffPreview(item, "−", entry.before, "diff-sidebar-code-before");',
            'appendDiffPreview(item, "+", entry.after, "diff-sidebar-code-after");',
            "function modeForLanguage(language: EditorLanguage): string",
            'if (language === "json") return "ace/mode/json";',
            'if (language === "toml") return "ace/mode/toml";',
            "function configureEditor(command: ReplaceDocumentCommand): void",
            "aceEditor.session.setUseWorker(false);",
            "function createEditor(command: ReplaceDocumentCommand): void",
            "aceEditor = aceApi.edit(",
            'aceEditor.session.on("change", reportChange);',
            "function replaceDocument(command: ReplaceDocumentCommand): void",
            "const summary = computeDiff();",
            'post({ type: "change", text: aceEditor.getValue(), added: summary.added, changed: summary.changed, deleted: summary.deleted });',
            'post({ type: "ready", documentKey: activeDocumentKey });',
            'const editorScrollbar = document.getElementById("editor-scrollbar");',
            'scrollTrack.addEventListener("pointerdown", (event) => {',
            "scrollEditorFromPointer(event.clientY, grabOffset);",
        ):
            self.assertIn(marker, self.code_editor_web)
        for marker in (
            "diffSummaryElement",
        ):
            self.assertNotIn(marker, self.code_editor_web)
        self.assertNotIn('fontSize: "13px"', self.code_editor_web)
        for marker in (
            "#code-editor-layout {",
            "body.diff-sidebar-enabled #code-editor-layout {",
            "#diff-sidebar {",
            "#diff-sidebar-list:empty::after {",
            ".diff-sidebar-item {",
            ".diff-sidebar-code {",
            '<aside id="diff-sidebar" aria-hidden="true">',
            "user-select: none;",
            "--editor-scrollbar-track:",
            "box-sizing: border-box;",
            "border: 1px solid var(--editor-border);",
            "#editor-scrollbar {",
            "#editor-scrollbar-thumb {",
            '<div id="editor-scrollbar" aria-hidden="true" hidden>',
        ):
            self.assertIn(marker, self.code_editor_wrapper)
        self.assertNotIn("#diff-summary", self.code_editor_wrapper)
        self.assertIn("window.webkit?.messageHandlers?.litellmCodeEditor", self.code_editor_web)
        self.assertIn("window.chrome?.webview?.postMessage", self.code_editor_web)
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["dependencies"].get("ace-builds"), "1.44.0")
        self.assertIn("<NativeCodeWebView", self.code_editor_wrapper)
        self.assertIn("window.LiteLLMCodeEditorInitialCommand", self.code_editor_wrapper)
        self.assertIn("const [initialHtml] = React.useState(() => codeEditorHtml({", self.code_editor_wrapper)
        self.assertIn("Rebuilding the HTML for every document-key change reloads", self.code_editor_wrapper)
        self.assertNotIn("const initialHtml = React.useMemo", self.code_editor_wrapper)
        self.assertIn("html={initialHtml}", self.code_editor_wrapper)
        bundler = (ROOT / "rn/scripts/build-code-editor.mjs").read_text(encoding="utf-8")
        self.assertIn("entryPoints: [source]", bundler)
        self.assertIn('format: "iife"', bundler)

    def test_assistant_setting_option_labels_cover_user_visible_non_brand_values(self) -> None:
        assistant_i18n = (ROOT / "rn/packages/shared/src/i18n/assistantSettingsI18n.ts").read_text(encoding="utf-8")
        for marker in ('"amazon-bedrock": "Amazon Bedrock"', 'lmstudio: "LM Studio"', 'vscode: "VS Code"', 'terminal: "终端"'):
            self.assertIn(marker, assistant_i18n)

    def test_logs_show_empty_state_after_a_loaded_but_missing_log_source(self) -> None:
        self.assertIn('active ? translate("logs.empty") : translate("logs.loading")', self.ui)

    def test_claude_settings_keep_desktop_basics_and_raw_code_sources_without_inventing_saved_defaults(self) -> None:
        for marker in (
            'translate("settings.claudeUnavailable")',
            "const desktop = asRecord(state.desktop);",
            'dispatch("desktop_patch", { inferenceProvider: inferenceProvider || null }, "claude")',
            'dispatch("desktop_patch", { inferenceGatewayBaseUrl }, "claude")',
            "const desktopModelNames = stringList(desktop.model_names);",
            'dispatch("desktop_models_patch", { model_names: splitLines(value) }, "claude")',
            'field="desktop_gateway_api_key"',
            'translate("claude.desktopSection")',
            'translate("claude.codeSection")',
            'document: "desktop"',
            'document: "developer"',
            'document: "settings"',
            "function AssistantFileEditorDialog",
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('translate("claude.deployment")', self.ui)
        self.assertNotIn('field="deployment_token"', self.ui)
        self.assertNotIn('patch_deployment', self.ui)
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
        claude = self.ui.split("function ClaudeScreen", 1)[1].split(
            "function AssistantSettingsWorkspace", 1
        )[0]
        for removed in (
            'translate("claude.memory")',
            'translate("claude.permissions")',
            'translate("claude.sandbox")',
            'translate("claude.capabilities")',
            'hasBooleanSetting',
        ):
            self.assertNotIn(removed, claude)
        self.assertIn('translate("claude.desktopSection")', claude)
        self.assertIn('translate("claude.codeSection")', claude)
        self.assertIn('translate("settings.claudeDesktopFilesHint")', self.ui)
        self.assertIn('translate("settings.claudeCodeFilesHint")', self.ui)

    def test_runtime_form_rows_keep_labels_and_controls_aligned_when_reflowed(self) -> None:
        for marker in (
            "runtimeInputRow:",
            "runtimeFieldLabel:",
            'runtimeFieldLabel: { width: 128, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "right" }',
            "runtimeValueSlot:",
            "runtimeMultilineField:",
            "runtimeMultilineHeader:",
            "runtimeMultilineEditor:",
            "runtimeMultilineHelpSlot:",
            'kind === "json"',
            "multiline plainText autoCommit",
            "nativeSecretTextArea:",
            "runtimeUnit:",
            "runtimeActionSlot:",
            "runtimeHelpSlot:",
            "runtimeTwoColumnForm:",
            "runtimeOneColumnForm:",
            "runtimeField: { minWidth:",
            "runtimeHelpText:",
            "runtimeJsonDefaultHint:",
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
            'function providerKeyChoices(provider: UnknownRecord, relaySources: RelaySourceOption[], baseURL?: string): ProviderKeyChoice[] {',
            '() => provider ? providerKeyChoices(provider, relaySources, providerBaseURL(provider)) : [],',
            'label: providerKeyChoiceLabel({ ...choice, name: providerKeyDisplayName(providerId, choice.id, choice.name) }, translate),',
            'const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyID(option.value);',
            'const providerKeyOptions = [...keyStates.map((key) => ({',
            'value={selectedProviderKey?.id ?? providerKeyOptions[0]?.value ?? ""}',
            'rows={keyRows}',
            'const pendingKeySelection = useRef<string | undefined>(undefined);',
            'pendingKeySelection.current = undefined;',
            'return dispatch("provider.key_delete", { provider_id: id, name: selectedKey });',
            'function apiKeyDisplayName(value: unknown, translate: Translate): string {',
            'if (!name) return translate("common.notAvailable");',
            'return name === "default" ? translate("providers.defaultKey") : name;',
            'const sourceName = choice.source',
            'join("/")',
            'const accountLabel = username ? username.split("@", 1)[0].trim() || username : stringValue(account.label, accountID).trim();',
            'return `${sourceName || name}${multiplier}`;',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('keys.length <= 1', self.ui)

    def test_provider_api_key_deletion_confirmation_explains_model_deletion(self) -> None:
        self.assert_ui_has('const affectedModelLines = asRecords(provider.models)')
        self.assert_ui_has('.filter((model) => stringValue(model.api_key_name).trim() === selectedKey)')
        self.assert_ui_has('const label = upstreamName && upstreamName !== publicName ? `${publicName} (${upstreamName})` : publicName;')
        self.assert_ui_has('title: translate("providers.deleteApiKey", { key: apiKeyDisplayName(selectedKey, translate) }),')
        self.assert_ui_has('? translate("providers.deleteApiKeyModelsMessage", { models: affectedModelLines.join("\\n") })')
        self.assert_ui_has(': translate("providers.deleteApiKeyNoModelsMessage"),')
        self.assertNotIn('message: `${apiKeyDisplayName(selectedKey, translate)} ->', self.ui)
        self.assertIn('"providers.deleteApiKey": "删除 API 密钥 {key}?"', self.zh)
        self.assertIn('"providers.deleteApiKeyModelsMessage": "将同时删除该密钥下模型:\\n{models}"', self.zh)
        self.assertIn('"providers.deleteApiKeyNoModelsMessage": "没有模型使用此密钥。"', self.zh)
        self.assertIn('"providers.deleteApiKey": "Delete API key {key}?"', self.en)
        self.assertIn('"providers.deleteApiKeyModelsMessage": "The following models will also be deleted:\\n{models}"', self.en)
        self.assertIn('"providers.deleteApiKeyNoModelsMessage": "No models use this key."', self.en)
        self.assertNotIn('点击“应用”后生效。', self.zh)

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
            'controller.focusSearchField()',
            'let searchField = NativeInstantFocusSearchField()',
            'searchField.focusRingType = .none',
            'showsInstantFocusBorder = true',
            'field.showsInstantFocusBorder = false',
            'private let focusBorderView = NativeInstantFocusBorderView(frame: .zero)',
            'layer?.borderWidth = borderVisible ? 3 : 0',
            'func updateVisibleRows()',
            'private var rowButtons: [Int: NSButton] = [:]',
            'foldedTitle: $0.folding(options:',
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
            'const dispatchWithOutcome = async (type: string, payload: UnknownRecord = {}, targetDomain = domain, keepControlsEnabled = false): Promise<CoreSnapshot | undefined>',
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

    def test_selecting_a_provider_does_not_stage_an_implicit_relay_conversion(self) -> None:
        workspace = self.ui.split("function ProviderWorkspace(", 1)[1].split(
            "function TablePane(",
            1,
        )[0]
        self.assertNotIn("autoRelaySelectionKeys", workspace)
        self.assertNotIn('dispatch("provider.select_relay_station"', workspace)
        self.assertIn(
            'onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }}',
            workspace,
        )

    def test_provider_inspector_keeps_the_compact_provider_form_and_return_link(self) -> None:
        """The provider editor uses compact, consistently aligned rows and a source-model return link."""
        for marker in (
            'const [providerSourceModel, setProviderSourceModel] = useState<string>();',
            'function ProviderSourceFields(',
            'label={translate("providers.endpointSource")} labelWidth={68}',
            'dispatch("provider.select_relay_station", { provider_id: providerID, station_id: nextStationID })',
            'disabled={busy || providerType === "relay"}',
            'label={translate("providers.providerName")} labelWidth={68}',
            'label={translate("providers.keyName")} labelWidth={68}',
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

    def test_service_provider_management_owns_login_and_provider_editor_is_api_key_only(self) -> None:
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")

        self.assertIn(
            'export type ProviderAuthKind = "api_key" | "openai_login" | "claude_login";',
            types,
        )
        self.assertIn("auth_status?: ProviderAuthStatus;", types)
        for marker in (
            "function ServiceProviderManager(",
            'dispatchWithOutcome("service_provider.add", { kind, name }, "providers_models")',
            'dispatchWithOutcome("service_provider.auth_start", { provider_id: targetProviderID }, "providers_models")',
            'dispatchWithOutcome("service_provider.delete", { provider_id: providerID }, "providers_models")',
            'void dispatchWithOutcome("service_provider.auth_status", { provider_id: accountFingerprint }, "providers_models", true)',
            '.then((next) => presentAuthChallenge(next, kind, label, accountFingerprint))',
            'native.showProviderAuth({',
            'field="provider_auth_token"',
            'function providerAuthKind(',
            'return candidates.filter((provider) => providerAuthKind(provider) === "api_key");',
            'const activeAuthKind: ProviderAuthKind = "api_key";',
            'auth_kind: "api_key"',
            'create_default_api_key: true',
        ):
            self.assert_ui_has(marker)
        self.assertIn('"providers.authTypeOpenAI": "通过 OpenAI 登录"', self.zh)
        self.assertIn('"providers.authTypeClaude": "通过 Claude 登录"', self.zh)
        self.assertIn('"providers.authTypeOpenAI": "Sign in with OpenAI"', self.en)
        self.assertIn('"providers.authTypeClaude": "Sign in with Claude"', self.en)
        self.assertIn('"providers.authStatusUnsupported": "暂不支持登录"', self.zh)
        self.assertIn('"providers.authStatusUnsupported": "Sign-in is unavailable"', self.en)
        self.assertIn("无需安装 Claude CLI", self.zh)
        self.assertIn("Claude CLI is not required", self.en)
        self.assert_ui_has("const callbackURL = stringValue(summary.redirect_uri);")
        self.assert_ui_has("...(callbackURL ? { callbackURL } : {})")
        self.assert_ui_has('selected && status === "error" ? <NativeSecretField')

        # Provider & Models deliberately has no login picker; its wizard is
        # API-key-only and the official account flow is in Service Provider
        # Management.
        self.assertIn('const addProvider = (): void => {\n    onOpenWizard();', self.ui)
        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertNotIn('providers.wizard.authentication', wizard)
        self.assertIn('label={translate("providers.wizard.baseUrl")}', wizard)
        self.assertIn('const activeAuthKind: ProviderAuthKind = "api_key";', wizard)

        editor = self.ui.split("function ProviderEditor(", 1)[1].split("function CodexWorkspace(", 1)[0]
        self.assertNotIn("<ProviderAuthFields", editor)
        self.assertIn("<ProviderSourceFields", editor)

    def test_service_provider_crud_controls_live_above_the_unified_list(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        route = self.ui.split(
            '{route === "relay-accounts" ? <View style={serviceProviderStyles.workspace}>',
            1,
        )[1].split('{route === "relay-add"', 1)[0]

        self.assertNotIn('translate("relay.officialAccountsHint")', route)
        self.assertIn('<View style={serviceProviderStyles.listToolbar}>', route)
        self.assertIn('symbol="plus"', route)
        self.assertIn('symbol="minus"', route)
        self.assertLess(route.index('symbol="plus"'), route.index('symbol="minus"'))
        self.assertLess(route.index('symbol="minus"'), route.index('<NativeTable'))
        self.assertIn('removeRequest={serviceProviderRemoveRequest}', route)
        self.assertIn('removeRequest: serviceProviderRemoveRequest', route)
        self.assertNotIn("embeddedAccountActions", relay)
        self.assertIn("const handledRemoveRequest = useRef(removeRequest ?? 0);", relay)

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
        self.assertIn('"logs.clearRecoveryCooldown": "重置恢复和冷却"', self.zh)
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
            'length: ROUTE_TRACE_REQUEST_ROW_HEIGHT',
            'offset: ROUTE_TRACE_REQUEST_ROW_HEIGHT * index',
            'removeClippedSubviews={false}',
            'const [visibleRequests, setVisibleRequests] = useState(requests);',
            'if (scrolling.current) {',
            'pendingRequests.current = requests;',
            'data={visibleRequests}',
            'onScroll={deferLiveRequestRefresh}',
            'scrollEventThrottle={16}',
            'routeTraceRequestRow: { height: ROUTE_TRACE_REQUEST_ROW_HEIGHT',
            'translate("logs.routeTrace.actualPath")',
            'selected.attempts.map((attempt, index)',
            'routeTraceWorkspace:',
            'routeTraceRequestPane:',
            'AppState.addEventListener("change", (state) => setAppActive(state === "active"))',
            'onFocus={() => onSelect(request.key)}',
            'accessibilityState={{ selected: isSelected }}',
            'selectedContentBackgroundColor',
            'unemphasizedSelectedContentBackgroundColor',
            'alternateSelectedControlTextColor',
            'routeTraceRequestRowSelected: { backgroundColor: systemColors.selectedContent',
            'routeTraceRequestTextSelected: { color: systemColors.selectedControlText }',
            'routeTraceTimeline:',
            'const ROUTE_TRACE_TIMELINE_MIN_WIDTH = 400;',
            'routeTraceTimeline: { flexGrow: 1, minWidth: ROUTE_TRACE_TIMELINE_MIN_WIDTH, paddingHorizontal: 14',
            'contentContainerStyle={[styles.routeTraceTimeline, hasTimelineHorizontalOverflow && styles.routeTraceTimelineWithHorizontalScrollbar]}',
            'onContentSizeChange={(width) => setTimelineContentWidth(width)}',
            'onResponderMove={({ nativeEvent }) => scrollTimelineToIndicatorPosition(nativeEvent.locationX)}',
            'routeTraceTimelineHorizontalScrollbarTrack: { position: "absolute", left: 14, right: 14, bottom: 4',
            'alwaysBounceHorizontal={false}\n        showsHorizontalScrollIndicator={false}\n        showsVerticalScrollIndicator',
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
        self.assertNotIn('hoveredKey', self.ui)
        self.assertNotIn('setHoveredKey', self.ui)
        self.assertNotIn('onHoverIn={() =>', self.ui)
        self.assertNotIn('onHoverOut={() =>', self.ui)
        self.assertNotIn('routeTraceRequestRowHovered:', self.ui)
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
        self.assertIn("const detected = chosenStation?.type || manualType ? undefined : await detectRelayType();", relay)
        self.assertIn('refreshResources: (accountId: string) => Promise<"ready" | "unavailable">;', relay)
        self.assertIn("resourceStatus = await refreshAccountResources(account, silent);", relay)
        self.assertIn("const setupControlsBusy = controlsBusy || loginBusy;", relay)
        self.assertIn("const [accountLoading, setAccountLoading]", relay)
        self.assertIn('translate("relay.status.loading")', relay)
        self.assertIn("selectedLoading.resources", relay)
        self.assertNotIn("controlsBusy && styles.loadingSurface]", relay)
        self.assertIn('title={translate("common.refresh")}', relay)
        self.assertNotIn('translate("relay.refreshResources")', relay)
        self.assertIn('account.loginStatus === "signed_in" ? translate("relay.resourcesNotLoaded")', relay)
        self.assertIn('case "login_expired": return translate("relay.resourcesLoginExpired")', relay)
        self.assertNotIn('title={translate("relay.importSelected")}', relay)
        self.assertIn("const accountType = chosenStation?.type ?? manualType ?? detected ?? detectedAddType;", relay)
        self.assertIn("account = await addAccount(accountType, candidate, rememberPassword, chosenStation ? {", relay)
        self.assertIn('translate("relay.rememberPassword")', relay)
        self.assertIn('translate("relay.back")', relay)
        self.assertIn("await deleteAccount(account);", relay)
        self.assertIn("const beforeRelayState = asRecord(beforeRelay.state);", self.ui)
        self.assertIn("const existingIDs = new Set(beforeRelayAccounts.map((item) => stringValue(item.id)).filter(Boolean));", self.ui)
        self.assertIn("const normalizedOrigin = normalizeRelayOrigin(origin);", self.ui)
        self.assertIn("origin: normalizedOrigin", self.ui)
        self.assertIn("stationOriginKey(stringValue(item.origin)) === originKey && item.type === type", self.ui)
        self.assertIn("NativePicker", relay)
        self.assertIn('title={translate("relay.next")}', relay)
        self.assertIn('disabled={setupControlsBusy || !origin.trim() || !addStationName.trim()}', relay)
        self.assertIn('disabled={setupControlsBusy || Boolean(selectedAddStation?.type)}', relay)
        self.assertIn('setOrigin("");\n      setAddStationName("");', relay)
        self.assertNotIn('title={translate("relay.importSelected")}', relay)
        self.assertIn("NativeSegmentedControl", relay)
        self.assertNotIn("const restorationAttempts", relay)
        self.assertIn("const openedAccountIDs = useRef(new Set<string>());", relay)
        self.assertIn('type SavedSessionRestore = "signed_in" | "expired" | "unavailable";', relay)
        self.assertIn("const refreshLoginState = async (account: RelayAccount, automatic = false): Promise<void> => {", relay)
        self.assertIn("const canAutoLogin = account.rememberPassword && account.passwordSaved && Boolean(account.username.trim());", relay)
        self.assertIn("void refreshLoginState(selected, true);", relay)
        self.assertNotIn('function statusKey(status: string): string', relay)
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
        )[1].split("const stageStationUpdate = async", 1)[0]

        self.assertIn("const [rememberPasswordDrafts, setRememberPasswordDrafts]", relay)
        self.assertIn("const selectedRememberPassword = selected ?", relay)
        self.assertIn('value={selectedRememberPassword}', relay)
        self.assertIn("setRememberPasswordDrafts", remember_password_update)
        self.assertNotIn("setCleanupBusy", remember_password_update)

    def test_relay_inline_edits_stage_without_local_save_buttons(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        self.assertNotIn('symbol="check"', relay)
        self.assertNotIn('title={translate("common.save")}', relay)
        self.assertIn('onBlur={() => { if (!disabled && !autoGrouping) void onNameCommit(); }}', relay)
        self.assertIn('onNameCommit={() => runApiKeyAction("update", selectedResource.id)}', relay)
        self.assertIn('onBlur={() => { if (!controlsBusy) void stageStationUpdate(selectedStation.id); }}', relay)
        self.assertIn('void stageStationUpdate(selectedStation.id, { type: nextType });', relay)
        self.assertIn('translate("relay.stationUpdateStaged")', relay)
        apply_body = self.ui.split('const apply = (): Promise<void> => {', 1)[1].split('const applyDataManagement', 1)[0]
        self.assertIn('await dispatchQueue.current;', apply_body)
        self.assertIn('title={translate("menu.apply")}', self.ui)

    def test_relay_model_names_are_explicit_and_expandable(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        self.assertIn('const INLINE_MODEL_LIMIT = 5;', relay)
        self.assertIn('function visibleResourceModels(resource: RelayResource, showAll: boolean): string[] {', relay)
        self.assertIn('models.join("\\n")', relay)
        self.assertIn('"relay.showAllModels"', relay)
        self.assertIn('"relay.showFewerModels"', relay)
        self.assertNotIn('+${resource.models.length - 2}', relay)

    def test_relay_account_header_is_a_single_compact_summary_row(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        header = relay.split('<View style={styles.accountHeader}>', 1)[1].split('<View style={[styles.resourcesSection]', 1)[0]

        self.assertIn('<View style={styles.accountBreadcrumb}>', header)
        self.assertIn('<NativeButton', header)
        self.assertIn('style={styles.accountBreadcrumbStation}', header)
        self.assertIn('<Text style={styles.accountBreadcrumbSeparator}>&gt;</Text>', header)
        self.assertIn('style={styles.accountBreadcrumbAccount}', header)
        self.assertIn('onPress={() => selectStation(selectedAccountStation.id)}', header)
        self.assertNotIn('relay.type', header)
        self.assertIn('<View style={styles.accountHeaderRight}>', header)
        self.assertIn('<View style={styles.accountSessionSummary}>', header)
        self.assertIn('styles.accountSessionValue', header)
        self.assertIn('selectedHeaderSignedIn', header)
        self.assertIn('selectedHeaderValue', header)
        self.assertIn('<NativeButton title={translate("common.refresh")}', header)
        self.assertNotIn('relay.balance', header)
        self.assertNotIn('common.status', header)
        self.assertLess(header.index('relay.rememberPassword'), header.index('accountSessionSummary'))
        self.assertNotIn('accountHeaderFields', header)
        self.assertNotIn('accountHeaderDivider', header)
        self.assertNotIn('accountStatusSlot', header)
        self.assertIn('accountHeader: { minWidth: 0, minHeight: 38', relay)
        self.assertIn('accountBreadcrumb: { flex: 1, minWidth: 0, minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }', relay)
        self.assertIn('accountHeaderRight: { marginLeft: "auto", marginRight: -4, flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 4 }', relay)
        self.assertIn('accountSessionSummary:', relay)
        self.assertNotIn('accountMetadata:', relay)
        self.assertNotIn('accountToolbar:', relay)

    def test_relay_manager_groups_accounts_by_station_and_uses_a_key_master_detail_workspace(self) -> None:
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
            'const rows: Array<{ key: string; cells: string[] }> = [{',
            'cells: [stationDisplay(station), ""]',
            'key: `account:${account.id}`',
            'cells: [`  ${accountDisplayName(account, translate)}`',
            "const stageStationUpdate = async",
            'const relayTableSelection = effectiveSelectedStationID ? "station:" + effectiveSelectedStationID : selected?.id ? "account:" + selected.id : "";',
            "sidebarAddButton:",
            'symbol="minus"',
            "NativeTable",
            'columns={[{ label: translate("common.name"), width: 118 }, { label: translate("relay.balance"), width: 78 }]}',
            "onSelectionChange={selectRelayTableRow}",
            "nativeRelayTable:",
            "nativeRelayTable:",
            "selectedStation",
            "selectedStationID",
            "stationHeader:",
            "stationHeaderMetrics:",
            'translate("relay.stationAccountCount", { count: selectedStationAccounts.length })',
            'translate("relay.stationKeyCount", { count: selectedStationResourceCount })',
            "stationSettingsForm:",
            "stationSettingsRow:",
            "stationSettingsFeedback:",
            'translate("relay.stationUpdateStaged")',
            'const INLINE_MODEL_LIMIT = 5;',
            'function visibleResourceModels(resource: RelayResource, showAll: boolean): string[] {',
            'models.join("\\n")',
            'relay.showAllModels',
            'relay.showFewerModels',
            'translate("relay.apiKeysTitle")',
            'translate("relay.apiKeyNamePlaceholder")',
            "resourceTableRows",
            'translate("relay.apiKeyDelete")',
            'translate("relay.apiKeyCreate")',
            "NativeSecureTextInput",
            'symbol="copy"',
            "resourceToolbarCrud",
            'translate("relay.apiKeyGroup")',
            'translate("relay.apiKeyMultiplier")',
            "function groupMultiplierLabel(multiplier: number | null, translate: Translate): string {",
            "function groupLabel(group: RelayGroup, translate: Translate): string {",
            "function resourceGroupName(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {",
            "function resourceGroupMultiplier(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {",
            "function resourceGroupLabel(resource: RelayResource, groups: RelayGroup[], translate: Translate): string {",
            "return `${group.name} / ${groupMultiplierLabel(group.multiplier, translate)}`;",
            "multiplier: groupMultiplier(entry.multiplier ?? entry.rate_multiplier ?? entry.ratio)",
            'domain="relay_accounts"',
            "apiKeyNameDrafts",
            "accountBreadcrumb",
            "accountBreadcrumbStation",
            "accountBreadcrumbSeparator",
            "accountBreadcrumbAccount",
            "accountHeaderRight",
            "accountSessionSummary",
            "accountSessionValue",
            "resourceEmptyText",
            'accountStationDisplay(selected)',
            'translate("relay.balance")',
            'title={translate("relay.addAccount")}',
            'disabled={setupControlsBusy || Boolean(selectedAddStation?.type)}',
            'scrollTrailingColumnOverflow={false}',
        ):
            self.assertIn(marker, relay)

        self.assertEqual(2, relay.count('scrollTrailingColumnOverflow={false}'))
        self.assertNotIn("resourceListActions", relay)
        self.assertNotIn('translate("relay.selectAllResources")', relay)
        self.assertNotIn("selectAllResources", relay)

        # The main workspace remains a single-selection native zebra table.
        for marker in (
            "const resourceTableRows = useMemo",
            "resourceSecondaryCellKeys",
            'rows={resourceTableRows}',
            'selectedKey={selectedResource?.id ?? ""}',
            "secondaryCellKeys={resourceSecondaryCellKeys}",
            "onSelectionChange={setSelectedResourceID}",
            "resourceNativeTable:",
            'columns={[{ label: translate("common.name"), width: 116 }, { label: translate("relay.apiKeyGroup"), width: 124 }, { label: translate("relay.apiKeyMultiplier"), width: 64 }]}',
            "resourceInspectorPane: { width: 266",
            "resourceInspectorLabel: { width: 54",
        ):
            self.assertIn(marker, relay)
        self.assertRegex(
            relay,
            r"const \[selectedResourceID, setSelectedResourceID\] = useState(?:<[^>]+>)?\(",
        )

        # Relay API keys are a live management list. There is no manual import
        # selection or import mode in this route.
        for marker in (
            "function ResourceImportDialog(",
            "selectedResources",
            "RelayImportMode",
            'translate("relay.importSelected")',
            'symbol="import"',
            "openResourceImport",
        ):
            self.assertNotIn(marker, relay)
        self.assertNotIn('leading={<ActionButton title={translate("relay.apiKeyImport")}', self.ui)
        self.assertNotIn("setRelayImportRequest((current) => current + 1)", self.ui)
        self.assertNotIn("importRequestKey", relay)
        resources_section = relay.split(
            '<View style={[styles.resourcesSection, compactStyles.resourcesSection]}>',
            1,
        )[1].split(
            '</View> : <View style={styles.blank}>',
            1,
        )[0]
        self.assertIn("<ResourceInspector", resources_section)
        self.assertIn("<NativeTable", resources_section)
        self.assertIn("          striped\n", resources_section)
        self.assertNotIn("ResourceImportDialog", resources_section)
        self.assertNotIn('translate("relay.importSelected")', resources_section)
        self.assertIn("NativeCheckbox", resources_section)
        self.assertIn('translate("relay.apiKeyAutoGrouping")', resources_section)
        for marker in (
            "resourceToolbarCrud",
            'symbol="plus"',
            'symbol="minus"',
            "setApiKeyCreateOpen(true)",
            "openRemoteKeyDelete(selected, selectedResource)",
        ):
            self.assertIn(marker, resources_section)
        self.assertIn("function ResourceInspector(", relay)
        resource_inspector = relay.split("function ResourceInspector(", 1)[1].split(
            "export function RelayAccountManager(",
            1,
        )[0]
        for marker in (
            "NativeTextField",
            "NativePicker",
            "NativeSecureTextInput",
            "NativeCheckbox",
            'symbol="copy"',
            'domain="relay_accounts"',
            "<Text selectable style={styles.resourceInspectorModels}",
        ):
            self.assertIn(marker, resource_inspector)
        self.assertNotIn("numberOfLines={3}", resource_inspector)
        for legacy_grid in (
            "function ResourceColumnHeader(",
            "function ResourceRow(",
            "<ResourceColumnHeader",
            "<ResourceRow",
        ):
            self.assertNotIn(legacy_grid, relay)

        # The redesign stays nested inside the account detail while preserving
        # the established station/account list.
        self.assertIn("rows={relayTableRows}", relay)
        self.assertIn("onSelectionChange={selectRelayTableRow}", relay)
        self.assertIn('if (kind === "station") selectStation(id);', relay)
        self.assertIn('const separator = key.indexOf(":");', relay)
        self.assertNotIn('key: `station:${station.id}`,\n      cells: [stationLabel, ""],\n      spanning: true,', relay)
        self.assertIn("style={styles.nativeRelayTable}", relay)
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
        self.assertIn('selected.resourceError === "no_api_keys" && !selectedLoading.resources && !selectedLoading.session', relay)
        self.assertIn('selectedLoading.resources || selectedLoading.session ? translate("relay.resourcesChecking") : resourceHint(selected, translate)', relay)
        self.assertNotIn('translate("relay.resourceCount"', relay)
        self.assertIn('"relay.apiKeyGroup": "分组"', self.zh)
        self.assertIn('"relay.apiKeyMultiplier": "倍率"', self.zh)
        self.assertIn('"relay.apiKeyGroup": "Group"', self.en)
        self.assertIn('"relay.apiKeyMultiplier": "Rate"', self.en)
        self.assertIn('columns={[{ label: translate("common.name"), width: 116 }, { label: translate("relay.apiKeyGroup"), width: 124 }, { label: translate("relay.apiKeyMultiplier"), width: 64 }]}', relay)
        self.assertIn('resourceGroupName(resource, selectedGroups, translate)', relay)
        self.assertIn('resourceGroupMultiplier(resource, selectedGroups, translate)', relay)
        self.assertNotIn('selectedResources.length > 0 ? <View style={[styles.bottomBar, compactStyles.bottomBar]}>', relay)
        self.assertNotIn("resourceSearch", relay)
        self.assertNotIn("syncTimeLabel", relay)
        self.assertNotIn("lastSyncedAt", relay)
        self.assertNotIn("UI_TIP_FONT_SIZE", relay)
        self.assertIn("resourceGroupLabel(selectedResource, selected?.groups ?? [], translate)", relay)
        self.assertIn("apiKeyActions", relay)
        self.assertIn('commitRelayMetadata("api_key.create"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.update"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.set_enabled"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.set_group"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.delete"', self.ui)
        self.assertIn('commitRelayMetadata("api_key.detach"', self.ui)
        self.assertIn('dependency_policy: dependencyPolicy', self.ui)
        self.assertNotIn('import_mode: importMode', self.ui)
        self.assertIn('const [localRemoval, setLocalRemoval] = useState<LocalRemovalIntent>();', relay)
        self.assertIn('const [remoteKeyDelete, setRemoteKeyDelete] = useState<RemoteKeyDeleteIntent>();', relay)
        self.assertNotIn('const [importMode, setImportMode]', relay)
        self.assertIn('? "relay.apiKeyCreateStaged"', relay)
        self.assertIn('? "relay.apiKeyDetachStaged" : "relay.apiKeyDeleteStaged"', relay)
        self.assertNotIn("await refreshAccountResources(selected);", relay)
        self.assertNotIn('resource_ids: [resourceId]', self.ui)
        self.assertIn('accountStationFor(selected)', relay)
        self.assertIn('symbol="refresh"', relay)
        self.assertIn("accountRememberPassword: { minWidth: 78, flexShrink: 0 }", relay)
        self.assertIn("resourcesSection: { flex: 1, minWidth: 0, minHeight: 0, borderTopWidth: 1", relay)

    def test_auto_grouping_hides_stale_resources_and_uses_the_global_status_bar(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        self.assertIn("const visibleResources = useMemo", relay)
        self.assertIn("!selected?.autoGrouping || !resourceGroupUnavailable(resource, selectedGroups)", relay)
        self.assertNotIn("autoGroupingTransitionAccountID", relay)
        self.assertNotIn("resourceListLoading", relay)
        self.assertNotIn('key: "resource-loading"', relay)
        self.assertIn("const selectedResource = visibleResources.find((resource) => resource.id === selectedResourceID) ?? visibleResources[0];", relay)
        self.assertIn("if (!selected.autoGrouping && selectedResource.groupID", relay)
        self.assertIn("selectedKey={selectedResource?.id ?? \"\"}", relay)
        self.assertIn("const result = await apiKeyActions.setAutoGrouping(accountID, enabled);", relay)
        self.assertIn('if (result.draftStaged) publishGlobalFeedback(translate("relay.apiKeyAutoGroupingStaged"));', relay)
        self.assertIn("else clearGlobalStatus();", relay)
        self.assertNotIn("await refreshAccounts();", relay.split("const updateAutoGrouping", 1)[1].split("useEffect", 1)[0])
        self.assertIn("disabled={disabled}\n            onPress={() => setShowAllModels", relay)
        self.assertIn("onStatus?: (status?: string) => void;", relay)
        self.assertIn("const publishGlobalFeedback = (message: string): void =>", relay)
        self.assertIn("onStatus?.(message);", relay)
        self.assertNotIn("resourcesFeedback", relay)
        self.assertNotIn("pendingOperationBar", relay)
        self.assertNotIn("accountPendingOperations", relay)
        self.assertNotIn('translate("relay.pendingOperationsCount", { count: resource.pendingOperationCount })', relay)
        self.assertIn("onStatus={setResult}", self.ui)
        self.assertIn("status={result}", self.ui)
        self.assertIn('return { draftStaged: next.drafts.relay_accounts?.dirty === true };', self.ui)

    def test_relay_dialogs_avoid_the_unregistered_macos_fabric_modal_host(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        # react-native-macos 0.85 can crash in RCTComponentViewFactory when
        # ModalHostView is first mounted on a secondary route surface.
        self.assertNotIn("<Modal", relay)
        self.assertNotIn("import { Modal,", relay)
        self.assertIn("function RelayDialogLayer(", relay)
        self.assertEqual(2, relay.count("<RelayDialogLayer visible={visible} onRequestClose={onClose}>"))
        self.assertIn(
            'relayDialogLayer: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, zIndex: 100 }',
            relay,
        )

    def test_relay_keys_are_discovered_by_base_url_without_manual_import_or_linking(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")

        for marker in (
            'return (["relay_accounts", "providers_models"] as const).filter',
            'function relaySourcesForBaseUrl(',
            'function providerKeyChoices(provider: UnknownRecord, relaySources: RelaySourceOption[], baseURL?: string): ProviderKeyChoice[] {',
            'function ProviderSourceFields(',
            'function relayStationsFromSnapshot(',
            'const providerType = stringValue(provider.provider_type, "custom") === "relay" ? "relay" : "custom";',
            'dispatch("provider.select_relay_station", { provider_id: providerID, station_id: nextStationID })',
            'const matchingRelaySources = relaySourcesForBaseUrl(',
            '() => provider ? providerKeyChoices(provider, relaySources, providerBaseURL(provider)) : [],',
            'const keyChoices = useMemo(() => providerKeyChoices(provider, relaySources, providerBaseUrl), [provider, providerBaseUrl, relaySources]);',
            'const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";',
            'dispatch("model.select_relay_resource"',
            'const providerKeyName = drafts?.providerKeyDisplayName(providerId, providerKey.id, providerKey.name) ?? providerKey.name;',
            'changes: { provider_key_id: providerKey.id, api_key_name: providerKeyName },',
            'providerKeyOptions.length > 0 ? <PickerField label={translate("providers.providerKey")}',
            'hint={translate("providers.relayKeyValueHint")}',
            'disabled domain="relay_accounts" field="api_key" target={relaySecretTarget}',
            '<TextField label={translate("providers.keyValue")} labelWidth={68} value={translate("providers.relayKeyValueHint")} disabled',
            'modelOrderMode(activeRoute.model) === "relay_multiplier"',
            'const canFollowMultiplier = usesRelayKey && relayMultiplier !== undefined;',
            'label={translate("providers.order")}',
            'label={translate("providers.followMultiplier")}',
            '{canFollowMultiplier ? <NativeCheckbox',
            'result.status === "partial"',
            'result.status === "failed"',
            'setIssues(applyIssuesForDisplay(value));',
            'if (!result.domains?.includes("relay_accounts") && result.status === "applied"',
            'if (!applied.domains?.includes("relay_accounts") && applied.status === "applied"',
        ):
            self.assertIn(marker, self.ui)

        for marker in (
            'dispatch("model.link_relay_key"',
            'dispatch("model.rebind_relay_key"',
            'dispatch("model.detach_relay_key"',
            'translate("providers.connectionSource")',
            'translate("providers.relayStation")',
            'translate("providers.relayAccount")',
            'translate("providers.relayApiKey")',
            'translate("providers.keyMode")',
            'translate("providers.sourceIndependent")',
            'translate("providers.sourceRelay")',
            'function relayRebindTargetsFromSnapshot(',
            'rebind: { provider_key_id:',
        ):
            self.assertNotIn(marker, self.ui)

        self.assertIn('"providers.providerKey": "密钥名"', self.zh)
        self.assertIn('"providers.apiKeys": "密钥列表"', self.zh)
        self.assertIn('"providers.relayKeyValueHint": "由服务商管理"', self.zh)
        self.assertNotIn('providers.bindingHealth', self.ui + self.zh + self.en)
        self.assertNotIn('providers.relayMultiplier', self.ui + self.zh + self.en)
        self.assertNotIn('providers.relayKeyBadge', self.ui + self.zh + self.en)

        for marker in (
            'commitRelayMetadata("resources.import"',
            'dispatch("provider.import_relay_key"',
            'translate("providers.relayKeySource")',
            "ResourceImportDialog",
            "RelayImportMode",
        ):
            self.assertNotIn(marker, self.ui + relay)

        for marker in (
            'value: "detach_disabled"',
            'value: "detach_only"',
            'commit("station.remove"',
            'linkedModelCount: count(entry.linked_model_count)',
            'pendingOperationCount: count(entry.pending_operation_count)',
        ):
            self.assertIn(marker, relay)

        for marker in (
            'translate("relay.bindingStatus")',
            'translate("relay.linkedModels")',
            'translate("relay.policyRebind")',
            'rebindTargetID',
        ):
            self.assertNotIn(marker, relay)

        for marker in (
            'catalog_mode?: "independent" | "relay_linked";',
            'order_mode?: "manual" | "relay_multiplier";',
            'effective_order?: number;',
            'status: "applied" | "partial" | "failed";',
            'completed_operations: number;',
            'pending_operations: number;',
        ):
            self.assertIn(marker, types)
        provider_key_contract = types.split("export interface ProviderKeySummary", 1)[1].split("}\n", 1)[0]
        for forbidden in ("value:", "password", "token", "secret"):
            self.assertNotIn(forbidden, provider_key_contract)

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
            "      native.window.close(canonicalWindowRoute(route));\n"
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
            'relayLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }',
            'sidebarIconButton: { width: 22, minWidth: 22, height: 22 }',
            'formRow: { width: "100%", minHeight: 34, flexDirection: "column", alignItems: "stretch", gap: 6 }',
            'bottomBar: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 6, flexDirection: "row", flexWrap: "wrap"',
            'resourcesSection: { flex: 1, minWidth: 0, minHeight: 0, borderTopWidth: 1, borderTopColor: colors.separator, paddingTop: 4 }',
            'accountDetailContent: { flex: 1, minWidth: 0, minHeight: 0 }',
            'accountHeader: { minWidth: 0, minHeight: 38, paddingHorizontal: 12, paddingVertical: 5, flexDirection: "row", alignItems: "center", columnGap: 6, backgroundColor: colors.window }',
            'accountBreadcrumb: { flex: 1, minWidth: 0, minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }',
            'accountHeaderRight: { marginLeft: "auto", marginRight: -4, flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 4 }',
            'resourcePane: { flex: 1, minWidth: 0, minHeight: 0, backgroundColor: colors.window }',
            'resourceToolbar: { minHeight: 32, paddingHorizontal: 12, paddingVertical: 3, flexDirection: "row", alignItems: "center", gap: 8 }',
            'resourceAutoGroupingCheckbox: { flexShrink: 0 }',
            'sidebarTableFrame: { flex: 1, minWidth: 0, minHeight: 0 }',
            'resourceListPane: { flex: 1, minWidth: 0, minHeight: 0 }',
            'resourceToolbarCrud: { marginLeft: "auto", flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 4 }',
            'resourceInspectorHeader: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6 }',
            '<Text numberOfLines={1} style={styles.resourceInspectorSubtitle}>{selectedResourceGroupLabel}</Text>',
            'pendingCleanupList: { maxHeight: 116',
            '<ScrollView style={styles.pendingCleanupList} contentContainerStyle={styles.pendingCleanupListContent}',
            'pendingCleanup: { minHeight: 30, flexDirection: "row", flexWrap: "wrap"',
        ):
            self.assertIn(marker, relay, marker)
        for web_card_marker in ("accountOverview:", "resourcesCard:", "borderRadius: 8"):
            self.assertNotIn(web_card_marker, relay)
        for redundant_separator in (
            'sidebarHeader: { height: 36, minHeight: 36, paddingHorizontal: 10, flexDirection: "row", alignItems: "center", gap: 8, borderBottomWidth:',
            'accountMetadata: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 5, flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 24, rowGap: 4, borderTopWidth:',
            'resourceToolbar: { minHeight: 36, paddingHorizontal: 12, flexDirection: "row", alignItems: "center", gap: 8, borderTopWidth:',
        ):
            self.assertNotIn(redundant_separator, relay)
        self.assertNotIn('accountHeader: { minWidth: 0, borderBottomWidth:', relay)
        self.assertIn('relayAccountsContent: { paddingBottom: 6, gap: 6 }', self.ui)

    def test_relay_add_setup_is_compact_and_starts_with_the_station_choice(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        setup = relay.split('<View style={[styles.formSection, setupOnly && styles.setupFormSection]}>', 1)[1].split('</View>', 1)[0]

        self.assertLess(setup.index('label={translate("relay.stationChoice")}'), setup.index('label={translate("relay.origin")}'))
        self.assertIn('<NativeSegmentedControl labels={addStationModeLabels}', setup)
        self.assertIn('translate("relay.stationExisting")', setup)
        self.assertIn('addStationMode === "existing"', setup)
        self.assertNotIn('<ScrollView', setup)
        self.assertLess(setup.index('label={translate("relay.origin")}'), setup.index('label={translate("relay.stationName")}'))
        self.assertIn('steps={[translate("relay.setupStepStation"), translate("relay.stepSignIn")]}', relay)
        self.assertIn('setupContent: { justifyContent: "flex-start", alignItems: "center", paddingHorizontal: 24, paddingTop: 18, paddingBottom: 12 }', relay)
        self.assertIn('setupSurface: { width: "100%", maxWidth: 520, minWidth: 0, gap: 12 }', relay)
        self.assertIn('setupFormSection: { maxWidth: 520, paddingVertical: 0, gap: 10 }', relay)
        self.assertIn('setupBottomBar: { minHeight: 46, paddingHorizontal: 20, paddingVertical: 8, borderTopWidth: 0, backgroundColor: colors.window }', relay)
        self.assertIn('const addStationModeLabels = stations.length > 0', relay)
        self.assertIn('stationModeSelector: { width: "100%", minWidth: 0, maxWidth: 520', relay)
        self.assertNotIn('setupProgressConnector', relay)
        form_section = relay.split('formSection: {', 1)[1].split('},', 1)[0]
        self.assertNotIn('borderTopWidth', form_section)

    def test_provider_table_columns_fit_the_fixed_provider_pane(self) -> None:
        self.assertIn('"providers.modelCount": "Count"', self.en)
        self.assertIn('"providers.modelCount": "模型数"', self.zh)
        self.assertNotIn('"providers.routeSource"', self.zh)
        self.assertIn('"providers.order": "顺序"', self.zh)
        self.assertIn('"providers.followMultiplier": "跟随倍率"', self.zh)
        self.assertNotIn('"providers.effectiveOrder"', self.zh)
        self.assertIn('"providers.keyName": "密钥名"', self.zh)
        self.assertIn('"providers.keyValue": "密钥值"', self.zh)
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 88 }, { label: translate("providers.modelCount"), width: 64 }]}')
        self.assert_ui_has('providerListPane: { width: 154, minWidth: 154, maxWidth: 154')
        self.assert_ui_has('columns={[{ label: translate("providers.model"), width: 96 }, { label: translate("providers.upstream"), width: 112 }, { label: translate("providers.providerKey"), width: 128 }]}')
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('providerKeysHeader: { minHeight: 24, flexDirection: "row"')
        self.assert_ui_has('<View style={styles.providerKeyActions}>')
        self.assert_ui_has('providerKeyTable: { width: "100%", height: 112, minHeight: 112')
        self.assert_ui_has('providerKeyFields: { minWidth: 0, gap: 4 }')
        self.assertNotIn('providerKeyGrid:', self.ui)

    def test_shared_native_controls_default_to_compact_density(self) -> None:
        native_controls = (ROOT / "rn/packages/shared/src/ui/NativeControls.tsx").read_text(encoding="utf-8")
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        for marker in (
            "const compact = props.compact ?? true;",
            "disabled: props.disabled === true,",
            "primary: props.primary === true,",
            "destructive: props.destructive === true,",
            "link: props.link === true,",
            "compact = true, onChange",
            "compact = true, followBottom",
            "button: { minWidth: 28, height: 24 }",
            "selectableRow: { minHeight: 28",
        ):
            self.assertIn(marker, native_controls)
        for marker in (
            "button: { minWidth: 28, height: 24 }",
            "segmented: { width: 224, height: 24 }",
            "picker: { minWidth: 160, height: 24 }",
            "textField: { minHeight: 24 }",
        ):
            self.assertIn(marker, appkit_controls)
        self.assert_ui_has("const compactStyles = StyleSheet.create({")
        self.assert_ui_has("formRow: { minHeight: 24, gap: 2 }")
        self.assert_ui_has("formRowControl: { gap: 1 }")
        self.assertIn("const compactStyles = StyleSheet.create({", relay)
        self.assertIn('resourceInspectorRow: { minHeight: 30, flexDirection: "row", alignItems: "center", gap: 6 }', relay)

    def test_relay_tables_use_compact_native_zebra_rows_and_shared_alignment(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        # Both account and API-key panes use the native compact zebra table;
        # explicit checkboxes are confined to the import dialog.
        self.assertEqual(2, relay.count("          striped\n"))
        self.assertNotIn("striped={false}", relay)
        for marker in (
            'tableTitleRow: { height: 38, minHeight: 38, paddingHorizontal: 10',
            'accountHeader: { minWidth: 0, minHeight: 38, paddingHorizontal: 12, paddingVertical: 5',
            'accountBreadcrumb: { flex: 1, minWidth: 0, minHeight: 24, flexDirection: "row", alignItems: "center", gap: 4 }',
            'accountHeaderRight: { marginLeft: "auto", marginRight: -4, flexShrink: 0, flexDirection: "row", alignItems: "center", justifyContent: "flex-end", gap: 4 }',
            'resourceToolbar: { minHeight: 32, paddingHorizontal: 12',
            'resourceNativeTable: { flex: 1, minWidth: 0, minHeight: 0 }',
            'resourceInspectorContent: { flexGrow: 1, minWidth: 0, paddingTop: 6, paddingLeft: 0, paddingRight: 12',
        ):
            self.assertIn(marker, relay)

    def test_webdav_form_is_integrated_into_the_unified_sync_tab(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            "function WebDavWorkspace(",
            '{tab === "webdav" ? <View style={[styles.dataManagementWebDavPane, styles.dataManagementWebDavContent, dataManagementPolishStyles.webDavContent]}>',
            '<WebDavWorkspace snapshot={snapshot} busy={busy || webDavOperationBusy} status={statuses.webdav} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onProbe={onProbeWebDav} hasChanges={hasWebDavChanges || hasPendingChanges} onApply={onApplyWebDav}>',
            '<NativeCheckbox label={translate("webdav.enabled")} value={booleanValue(state.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { enabled })} style={styles.webdavEnabledControl} />',
            "webdavStateRow:",
            "webdavEnabledControl: { flexGrow: 0, flexShrink: 0, alignSelf: \"flex-start\" }",
            "webdavStateStatus:",
            "webdavFormBody:",
            'webDavFormRows: { width: "100%", maxWidth: 560, gap: 7 }',
            "dataManagementWebDavPane:",
            "dataManagementWebDavContent:",
            'label={translate("webdav.url")}',
            'label={translate("webdav.remoteFile")}',
            'label={translate("webdav.syncEvery")}',
            'label={translate("webdav.httpTimeout")}',
            "function WebDavPasswordField(",
            'placeholder={configured ? translate("webdav.passwordHintConfigured") : translate("webdav.passwordHintOptional")}',
            'webdavPasswordInput: { width: "100%", minHeight: 26 }',
            "WEBDAV_FORM_LABEL_WIDTH",
            'labelWidth={WEBDAV_FORM_LABEL_WIDTH}',
            'labelAlign={labelAlign}',
            "dataManagementSyncContent:",
            "dataManagementToolbarButtons:",
            "webdavSyncArea:",
            "wideButton: { minWidth: 92 }",
        ):
            self.assert_ui_has(marker)
        self.assertNotIn(
            '<NativeSecretField label={translate("webdav.password")}',
            self.ui,
        )
        self.assertEqual(1, workspace.count("<WebDavWorkspace "))
        self.assertNotIn('title={translate("dataManagement.syncSettings")}', workspace)
        self.assertNotIn('dataManagementGroupDivider', workspace)
        self.assertIn('<View style={[styles.webdavSyncArea, dataManagementPolishStyles.webDavSyncArea]}>{children}</View>', self.ui)
        self.assertIn('webDavSyncArea: { borderTopWidth: 0, paddingTop: 4, marginTop: 2 }', self.ui)
        self.assertIn('webDavActionRow: { borderTopWidth: 0, paddingTop: 4, marginTop: 10 }', self.ui)
        self.assertIn('webdavActionSpacer: { flex: 1 }', self.ui)
        self.assertNotIn('webdavHeaderActions:', self.ui)
        webdav_component = self.ui.split("function WebDavWorkspace(", 1)[1].split("function WebDavPasswordField(", 1)[0]
        self.assertLess(webdav_component.index('title={translate("dataManagement.testConnection")}'), webdav_component.index('title={translate("common.saveAndApply")}'))
        self.assertGreater(webdav_component.index('title={translate("dataManagement.testConnection")}'), webdav_component.index('<View style={[styles.webdavSyncArea, dataManagementPolishStyles.webDavSyncArea]}>{children}</View>'))
        self.assertNotIn("dataManagementSectionGrid:", self.ui)
        self.assertIn("footerCompact:", self.ui)
        self.assertNotIn("dataManagementRelayRow:", self.ui)

    def test_webdav_sync_exposes_sync_push_pull_with_a_fixed_scope(self) -> None:
        for marker in (
            'type WebDavSyncAction = "sync" | "push" | "pull";',
            'const WEBDAV_SYNC_DOMAINS: readonly ConfigDomain[] = ["providers_models", "relay_accounts"];',
            '{ id: "sync", title: translate("dataManagement.syncSmart") }',
            '{ id: "push", title: translate("dataManagement.syncPush") }',
            '{ id: "pull", title: translate("dataManagement.syncPull") }',
            'onSyncWebDav: (action: WebDavSyncAction) => Promise<void>;',
            'const runWebDavOperation = async (operation: () => Promise<unknown>, message: string): Promise<void> => {',
            'if (webDavOperationInFlight.current) return;',
            'webDavOperationBusy={webDavOperationBusy}',
            'disabled={busy || webDavOperationBusy || snapshot?.webdav.enabled !== true}',
            'type: action, payload: { sections: [...WEBDAV_SYNC_DOMAINS] }',
            'onPress={() => { void onSyncWebDav(syncAction); }}',
        ):
            self.assert_ui_has(marker)

    def test_text_fields_keep_active_typing_local_and_project_dependent_views(self) -> None:
        for marker in (
            "function usePendingTextField(",
            "setDirty(next !== committedRef.current || commitInFlight.current !== undefined);",
            "void commit().catch(() => undefined);",
            "void field.commit().catch(() => undefined);",
            "hasPendingFieldEdits())",
            "registry?.setDirty(fieldId.current, true);",
            "const SECRET_INPUT_COMMIT_DEBOUNCE_MS = 150;",
            "const secretDebounceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);",
            "if (autoCommit) {",
            "}, SECRET_INPUT_COMMIT_DEBOUNCE_MS);",
            "state.status === \"saved\" || state.status === \"ready\" || state.status === \"error\"",
            "refreshAfter = true",
            "if (refreshAfter) await refresh();",
            "}, null, true);",
            "function WebDavPasswordField(",
            'isDirty: () => dirtyRef.current',
            "<NativeSecretField plainText autoCommit label={translate(\"common.apiKey\")}",
            'input: { width: "100%", minHeight: 26',
            'formRow: { width: "100%", minHeight: 26',
            'form: { gap: 6 }',
            'structuredForm: { gap: 6 }',
        ):
            self.assert_ui_has(marker)
        pending_hook = self.ui.split("function usePendingTextField(", 1)[1].split("function RuntimeValueField", 1)[0]
        self.assertIn("if (!dirtyRef.current) {\n      committedRef.current = value;", pending_hook)
        self.assertIn("onDraftChangeRef.current?.(next);", pending_hook)
        self.assertIn("if (dirtyRef.current) void commit().catch(() => undefined);", pending_hook)
        self.assertNotIn("debounceTimer", pending_hook)
        self.assertNotIn("setTimeout(", pending_hook)
        self.assertIn("onDraftChange={onNameDraftChange}", self.ui)
        self.assertIn("drafts?.providerDisplayName(provider)", self.ui)
        self.assertIn("setProviderNameDraft", self.ui)
        self.assertIn("ProviderWorkspaceDraftContext", self.ui)
        self.assertIn("const [providerNameDrafts, setProviderNameDrafts] = useState<Record<string, string>>({});", self.ui)
        self.assertIn("function providerModelDraftKey(providerID: string, modelID: string)", self.ui)
        self.assertIn("key={`model:${providerId}:${editorIdentifier(model)}`}", self.ui)
        self.assertIn('cells: [providerDisplayName(item), String(asRecords(item.models).length', self.ui)
        self.assertIn('cells: [modelDisplayName(providerId, item), modelUpstreamDisplay(providerId, item)', self.ui)
        self.assertIn(r'cells: [`\t${providerDisplayName(entry.provider)}`', self.ui)
        self.assertIn("modelUpstreamDisplay", self.ui)
        self.assertIn("providerKeyDisplayName", self.ui)
        self.assertIn('value={drafts?.providerKeyDisplayName(id, selectedChoice.id, selectedChoice.name)', self.ui)
        self.assertIn("const providerRows = asRecords(structured.providers).map(editableRecord);", self.ui)
        self.assertIn("const commitGateway = (base_url: string): Promise<void>", self.ui)
        self.assertIn("providers: providerRows.map((item) => identifier(item) === directProvider ? { ...item, base_url } : item)", self.ui)
        self.assertIn("onDraftChange={setModelDraft}", self.ui)
        self.assertIn('value={displayedModel}', self.ui)
        self.assertNotIn("const INPUT_SYNC_INTERVAL_MS", self.ui)
        self.assertNotIn("const INPUT_COMMIT_DEBOUNCE_MS", self.ui)
        self.assertNotIn("void commit(false).catch(() => undefined);", self.ui)
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        native_controls = (ROOT / "rn/packages/shared/src/ui/NativeControls.tsx").read_text(encoding="utf-8")
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        self.assertIn("apiKeyNameDrafts[resource.id] !== undefined", relay)
        self.assertIn("const apiKeyNameDraftsRef = useRef(apiKeyNameDrafts);", relay)
        self.assertIn("apiKeyNameDraftsRef.current[resourceID as string]", relay)
        self.assertIn("apiKeyNameDraftsRef.current = updated;", relay)
        self.assertIn("resourceDisplayName(resource)", relay)
        self.assertIn('const [stationDrafts, setStationDrafts] = useState<Record<string, StationDraft>>({});', relay)
        self.assertIn("const projectedStation = (station: RelayStation): RelayStation => {", relay)
        self.assertIn('cells: [stationDisplay(station), ""]', relay)
        self.assertIn("accountStationDisplay(selected)", relay)
        self.assertIn("onChangeText={(value) => setStationDraft(selectedStation.id, { name: value })}", relay)
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
            "showLabel={false} showDiff codexPane syncRevision={syncRevision} style={assistantFileSurfaceStyles.editorRaw}",
            "assistantFileSurfaceStyles.fileGroups",
            "assistantFileSurfaceStyles.editorDialog",
            "function AssistantFileEditorDialog",
            "const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);",
            "const [settingsRawBaselineToken, setSettingsRawBaselineToken] = useState(0);",
            'if (reloadDomain === "codex" || reloadDomain === "claude") setSettingsRawBaselineToken((current) => current + 1);',
            'if ((currentDisk[diskDomain]?.generation ?? 0) > priorGeneration && !currentDisk[diskDomain]?.changed && (diskDomain === "codex" || diskDomain === "claude"))',
            "await flushPendingFields();",
            "reloadToken={rawReloadToken}",
            "baselineToken={rawBaselineToken}",
            "baselineToken, document, domain, ipc, registry, reloadNonce, reloadToken, reset, translate",
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

    def test_codex_and_unified_webdav_actions_keep_visible_labels(self) -> None:
        self.assert_ui_has('>{translate("settings.structured")}</Text>')
        self.assert_ui_has('title={translate("dataManagement.testConnection")}')
        self.assert_ui_has('title={translate("dataManagement.syncNow")}')
        self.assert_ui_has('title={translate("menu.apply")}')
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
        self.assertNotIn('"dataManagement.staged":', chinese)
        for source in (english, chinese, translation_keys):
            self.assertNotIn("relay.resourcesImportedLinked", source)
            self.assertNotIn("relay.importLinked", source)

    def test_macos_leaf_localizes_window_titles_and_keeps_status_menu_order(self) -> None:
        for title in (
            'case "providers-models": return "LiteLLM " + localized("routeProvidersModels", fallback: "Providers & Models")',
            'case "codex-settings", "claude-settings": return localized("routeCodexSettings", fallback: "Codex / Claude Settings")',
            'case "data-management": return localized("routeDataManagement", fallback: "Data Management")',
            'case "logs": return "LiteLLM " + localized("routeLogs", fallback: "Logs")',
        ):
            self.assertIn(title, self.macos_leaf, title)
        self.assertIn("private static let statusMenuOrder", self.macos_leaf)
        for ordered_item in (
            '"toggle-autostart", "toggle-codex-model-catalog", "separator"',
            '"open-providers-models", "open-runtime-settings", "open-codex-settings", "open-relay-accounts", "separator"',
            '"webdav-status", "open-data-management", "separator"',
            '"open-logs", "separator"',
            '"show-version", "quit"',
        ):
            self.assertIn(ordered_item, self.macos_leaf, ordered_item)

    def test_data_management_route_replaces_standalone_webdav_route_everywhere(self) -> None:
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        macos_app = (ROOT / "rn/apps/macos/macos/LiteLLMMenu-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        windows_app = (ROOT / "rn/apps/windows/windows/LiteLLMMenu/LiteLLMMenu.cpp").read_text(encoding="utf-8")
        for source in (self.ui, routes, types, self.macos_leaf, self.platform_entry, self.windows_leaf, macos_app, windows_app):
            self.assertNotIn('"webdav-settings"', source)
            self.assertNotIn('"open-webdav-settings"', source)
            self.assertNotIn("routeWebdavSettings", source)
        self.assertIn('{ id: "data-management", titleKey: "menu.dataManagement" }', routes)
        self.assertIn('| "data-management"', types)
        self.assertIn("routeDataManagement: string;", types)
        for source in (self.ui, self.macos_leaf, self.windows_leaf):
            self.assertIn("open-data-management", source)
        for source in (self.macos_leaf, self.windows_leaf):
            self.assertIn("routeDataManagement", source)
            self.assertIn("data-management", source)

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
