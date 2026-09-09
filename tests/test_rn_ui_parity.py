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
        cls.relay = RELAY_MANAGER.read_text(encoding="utf-8")

    def assert_ui_has(self, marker: str) -> None:
        self.assertIn(marker, self.ui, marker)

    def assert_ui_not_has(self, marker: str) -> None:
        self.assertNotIn(marker, self.ui, marker)

    def test_menu_bar_home_is_not_a_dashboard_or_sidebar_shell(self) -> None:
        """The menu-bar host stays hidden; the status icon opens the settings shell."""
        self.assert_ui_has('route === "home" ? <View style={styles.menuBarHost} />')
        self.assert_ui_has("function SettingsShell(")
        self.assert_ui_has('isSettingsShellRoute(route)')
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
        self.assertIn('id !== "claude-settings" && id !== "provider-wizard"', routes)

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
            '"logs.duration": "耗时(s)"',
            '"logs.tokenCountK": "令牌数(k)"',
            '"service.status": "状态: {status}"',
            '"common.empty": "(空)"',
            '"providers.probeApplyTitle": "使用推荐协议?"',
            '"relay.typeDetected": "已识别: {type}"',
        ):
            self.assertIn(marker, chinese)
        self.assertIn('{translate("providers.provider")}: {providerName}', self.ui)
        self.assertIn('translate("common.default")}: ${defaultValue}', self.ui)
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
        self.assertIn("await dispatchQueue.current;", flush)
        self.assertNotIn("lastDispatchError", self.ui)

    def test_shared_assistant_apply_is_not_blocked_by_the_undefined_default_domain(self) -> None:
        apply_body = self.ui.split('const apply = (): Promise<void> => {', 1)[1].split(
            "const applyDataManagement",
            1,
        )[0]
        self.assertIn('if ((!settingsRoute && !domain) || domain === "logs")', apply_body)
        self.assertIn('settingsRoute || route === "providers-models"', apply_body)
        self.assertIn("stagedDomainsForRoute(refreshed)", apply_body)

    def test_shared_assistant_apply_is_not_blocked_by_the_undefined_default_domain(self) -> None:
        apply_body = self.ui.split('const apply = (options?: { silent?: boolean; message?: string; keepControlsEnabled?: boolean }): Promise<void> => {', 1)[1].split(
            "const autoAppliedKey",
            1,
        )[0]
        self.assertIn('if ((!settingsRoute && !domain) || domain === "logs")', apply_body)
        self.assertIn('settingsRoute || route === "providers-models" || route === "data-management"', apply_body)
        self.assertIn("stagedDomainsForRoute(refreshed)", apply_body)

    def test_settings_shell_applies_staged_domains_without_an_explicit_footer(self) -> None:
        for marker in (
            'const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {',
            'const actionSnapshot = latestSnapshot.current && latestSnapshot.current.revision >= (snapshot?.revision ?? -1)',
            'const routeHasStagedChanges = useCallback((currentSnapshot: CoreSnapshot | undefined): boolean => (',
            'return (["providers_models", "relay_accounts"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
            '|| hasPendingFieldEdits()',
            'const needsDiscardConfirmation = routeHasStagedChanges(current);',
            # Immediate apply: the shell watches the staged domains and applies
            # them after the debounce instead of rendering an Apply button.
            'const autoApplyKey = shell && actionSnapshot && !immediateApplyBlocked',
            'if (busy || hasPendingFieldEdits()) return;',
            'setResult(translate("common.saving"));',
            'void apply({ message: "common.saved", keepControlsEnabled: true });',
            '}, IMMEDIATE_APPLY_DEBOUNCE_MS);',
            # A single text edit must re-run the effect once its dirty flag
            # clears, so the pending-field counter is part of the deps.
            '}, [autoApplyKey, busy, hasPendingFieldEdits, pendingFieldRevision, shell, translate]);',
        ):
            self.assert_ui_has(marker)
        # The provider wizard is the exempted sub-sheet: its partial steps must
        # not auto-apply until the sheet closes.
        self.assert_ui_has('if (route !== "provider-wizard") return;')
        self.assert_ui_has('setProviderWizardOpen(true);')
        self.assert_ui_has('return () => setProviderWizardOpen(false);')
        # The raw file editor is the other explicit-action sub-sheet; it also
        # holds immediate apply while open and saves on its own button.
        self.assert_ui_has('setAssistantEditorOpen(activeAssistantFile !== undefined);')
        self.assert_ui_has('title={translate("common.save")}')
        gate = (ROOT / "rn/packages/shared/src/ui/providerWizardGate.ts").read_text(encoding="utf-8")
        self.assertIn("let providerWizardOpen = false;", gate)
        self.assertIn("export function setProviderWizardOpen(open: boolean): void {", gate)
        self.assertIn("export function subscribeProviderWizard(listener: () => void): () => void {", gate)
        # The route-level Apply/Close footer is gone; only the wizard sheet
        # keeps explicit actions.
        self.assertNotIn("function DialogFooter(", self.ui)
        self.assertNotIn('disabled={busy || !routeHasStagedChanges(actionSnapshot)}', self.ui)
        self.assertNotIn('disabled={busy || stagedDomainsForRoute(snapshot).length === 0}', self.ui)
        self.assertNotIn('snapshot?.drafts.codex?.dirty || snapshot?.drafts.claude?.dirty || hasClaudeDeploymentChanges(snapshot) || hasPendingFieldEdits()', self.ui)

    def test_close_reads_authoritative_snapshot_before_confirming(self) -> None:
        close_body = self.ui.split('const requestClose = async (): Promise<void> => {', 1)[1].split(
            '  useEffect(() => {',
            1,
        )[0]
        self.assertIn('const authoritative = await ipc.snapshot();', close_body)
        self.assertIn('authoritative.revision >= current.revision', close_body)
        self.assertIn('latestSnapshot.current = authoritative;', close_body)
        self.assertIn('const needsDiscardConfirmation = routeHasStagedChanges(current);', close_body)
        self.assertIn('if (!needsDiscardConfirmation) {', close_body)
        self.assertIn('closeRoute();', close_body)

    def test_apply_rebases_one_live_projection_revision_conflict(self) -> None:
        apply_body = self.ui.split('const apply = (options?: { silent?: boolean; message?: string; keepControlsEnabled?: boolean }): Promise<void> => {', 1)[1].split(
            'const activateProviderAndRestart',
            1,
        )[0]
        self.assertIn('const applyOnce = (nextRevision: number): Promise<IpcResults["apply"]>', apply_body)
        self.assertIn('if (!isRevisionConflict(reason)) throw reason;', apply_body)
        self.assertIn('const current = await ipc.snapshot();', apply_body)
        self.assertIn('const sameDomains = currentDomains.length === domains.length', apply_body)
        self.assertIn('const sameDiskState = domains.every((name) => (', apply_body)
        self.assertIn('result = await applyOnce(current.revision);', apply_body)

    def test_compatibility_claude_route_reuses_the_combined_native_window(self) -> None:
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        for marker in (
            'import { canonicalWindowRoute, isSettingsPaneRoute, isSettingsShellRoute, LOG_TABS, routeMenuActions, ROUTES, SETTINGS_PANES, SETTINGS_PANE_PRESENTATION } from "../routes";',
            'const windowRoute = canonicalWindowRoute(routeRequest);',
            'native.window.open(windowRoute);',
            'native.window.focus(windowRoute);',
            'native.window.close(canonicalWindowRoute(route))',
        ):
            self.assert_ui_has(marker)
        self.assertIn('if (route === "claude-settings") return "codex-settings";', routes)
        # Legacy Service Provider Management deep links land in the merged
        # provider workspace.
        self.assertIn('if (route === "relay-accounts") return "providers-models";', routes)
        self.assertIn('if (route === "relay-add") return "provider-wizard";', routes)
        # Every settings pane shares the one window; the native hosts map the
        # pane list to a single registry key.
        self.assertIn('export const SETTINGS_PANES: readonly RouteDefinition[] = MENU_ROUTES;', routes)
        self.assertIn('export function isSettingsShellRoute(route: AppRoute | undefined): boolean {', routes)
        macos_leaf = MACOS_LEAF.read_text(encoding="utf-8")
        windows_leaf = WINDOWS_LEAF.read_text(encoding="utf-8")
        self.assertIn('private static let settingsPaneRoutes: Set<String> = [', macos_leaf)
        self.assertIn('private func settingsWindowKey() -> String? {', macos_leaf)
        self.assertIn('if Self.settingsPaneRoutes.contains(windowRoute),', macos_leaf)
        self.assertIn('route == L"providers-models" || route == L"codex-settings"', windows_leaf)

    def test_desktop_route_actions_reopen_the_same_route_and_reset_bare_logs(self) -> None:
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn("const [routeRequestSequence, setRouteRequestSequence] = useState(0);", bootstrap)
        self.assertIn("setRouteRequestSequence((current) => current + 1);", bootstrap)
        self.assertIn('setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");', bootstrap)
        self.assertIn("routeRequestSequence?: number;", self.ui)
        self.assertIn("[isPrimaryHost, isWindowManagerHost, native, routeRequest, routeRequestSequence]", self.ui)

    def test_settings_pane_actions_do_not_reopen_the_shared_window_from_the_primary_host(self) -> None:
        # The native host focuses/creates the shared settings window before it
        # emits the pane action. Re-opening the pane from the hidden primary
        # host emitted the same action again and looped at ~8/s.
        self.assertIn("if (!isSettingsPaneRoute(windowRoute)) {", self.ui)
        route_effect = self.ui.split("if (!routeRequest) return;", 1)[1].split(
            "}, [isPrimaryHost, isWindowManagerHost, native, routeRequest, routeRequestSequence]);",
            1,
        )[0]
        window_manager = route_effect.split("if (isWindowManagerHost) {", 1)[1].split("return;", 1)[0]
        self.assertIn("if (!isSettingsPaneRoute(windowRoute)) {", window_manager)
        self.assertIn("native.window.open(windowRoute);", window_manager)

    def test_shared_snapshot_refresh_does_not_reset_native_window_geometry(self) -> None:
        self.assertNotIn("const windowSpecs = {", self.ui)
        # The settings shell owns one native window per platform, so no pane
        # may resize the window while the shared snapshot refreshes.
        self.assertNotIn("const resizeDataManagement = useCallback", self.ui)
        self.assertNotIn('setContentSize?.("data-management"', self.ui)
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
        self.assert_ui_has('providerInspector: { width: 290, minWidth: 290, maxWidth: 290')
        self.assertNotIn("<ScrollView contentContainerStyle={styles.providerEditorScroll}><ProviderEditor", self.ui)
        self.assert_ui_has("providerEditorContent: { flex: 1, minHeight: 0 }")
        self.assert_ui_has("providerEditorScrollContent: { paddingTop: 3, paddingLeft: 0, paddingRight: 16, paddingBottom: 12, gap: 6 }")
        self.assert_ui_has('persistentScrollIndicator: { position: "absolute", width: 0, height: 0 }')
        self.assert_ui_has('return <PersistentScrollView style={styles.providerEditorContent} contentContainerStyle={styles.providerEditorScrollContent} showsVerticalScrollIndicator nestedScrollEnabled onViewportHeightChange={setEditorViewportHeight} onContentHeightChange={setEditorContentHeight}>')
        self.assert_ui_has('{kind === "apiKey" || (kind === "relay" && !station) ? <ProviderSourceFields')
        self.assert_ui_has('{scrollable ? <NativePersistentScrollIndicator style={styles.persistentScrollIndicator} /> : null}')
        self.assert_ui_has('showsHorizontalScrollIndicator={false}\n    onLayout=')
        self.assert_ui_has('<PersistentScrollView style={styles.providerWizardModelScroll} contentContainerStyle={styles.providerWizardModelScrollContent} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">')
        self.assert_ui_has('providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('inspectorContent: { paddingTop: 3, paddingLeft: 0, paddingRight: 6')
        self.assert_ui_has("providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }")
        self.assert_ui_has("providerListPane: { width: 140, minWidth: 140, maxWidth: 140")
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 132 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.keyModelColumn"), width: 124 }, { label: translate("providers.model"), width: 108 }, { label: translate("common.order"), width: 48 }]}')
        self.assert_ui_has('rows.push({ key: `key:${keyName}`, cells: [keyName], spanning: true });')
        self.assert_ui_has('rows.push({ key: editorIdentifier(item), cells: [`\\t${modelUpstreamDisplay(providerId, item)}`, modelDisplayName(providerId, item), modelOrderText(providerId, item)] });')
        self.assert_ui_has('columns={variant === "inline"')
        self.assert_ui_has('cellHorizontalPadding={6}')
        self.assert_ui_has('firstColumnHorizontalPadding={0}')
        self.assert_ui_has('label={translate("providers.keyName")}')
        self.assert_ui_has('NativeSecretField plainText autoCommit label={translate("providers.keyValue")}')
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
        self.assert_ui_has('label: translate("providers.provider"), width: 96 }, { label: translate("providers.providerKey"), width: 130')
        self.assert_ui_has('label: translate("common.order"), width: 64 }, { label: translate("providers.upstream"), width: 120')
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
        self.assert_ui_has('rows={tableRows}')
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
        self.assertNotIn('serviceProviderRows', self.ui)
        self.assertNotIn('renderRelayManager', self.ui)
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
            'const dataManagementTabRows = useMemo(',
            '{ id: "import" as DataManagementTab, title: translate("dataManagement.tab.import") }',
            '{ id: "export" as DataManagementTab, title: translate("dataManagement.tab.export") }',
            '{ id: "webdav" as DataManagementTab, title: translate("dataManagement.tab.webdav") }',
            'selectedKey={tab}',
            'onSelectionChange={(key) => switchDataManagementTab(key as DataManagementTab)}',
            'const switchDataManagementTab = (next: DataManagementTab): void => {',
            'const previous = tab;',
            'const pending = onFlushPendingFields();',
            'setTab(next);',
            'void pending.catch((reason: unknown) => {',
            'onTabSwitchError(previous, reason);',
            '{tab === "import" ? <ScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>',
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

    def test_settings_window_uses_one_shared_native_geometry(self) -> None:
        # One window hosts every settings pane, so the shared UI no longer
        # resizes the window per data-management tab.
        self.assertNotIn('const windows = Platform.OS === "windows";', self.ui)
        self.assertNotIn('void onResize(size.width, size.height);', self.ui)
        self.assert_ui_has('const dataManagementPolishStyles = StyleSheet.create({')
        self.assert_ui_has('tabBar: { height: 42, minHeight: 42 }')
        self.assert_ui_has('tabs: { width: 272, height: 28 }')
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: 14, paddingHorizontal: 12, paddingBottom: 14, gap: 14 }')
        self.assertNotIn('importLandingContent', self.ui)
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

    def test_import_stages_sections_and_immediate_apply_writes_them(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'const [stagedSections, setStagedSections] = useState<ConfigDomain[]>([]);',
            'setStagedSections(DATA_PACKAGE_DOMAINS.filter((domain) => imported.draft_domains.includes(domain)));',
            '{sectionList(stagedSections, stagedSections, true, () => undefined)}',
        ):
            self.assertIn(marker, workspace)
        import_selected = workspace.split(
            'const importSelected = async (): Promise<void> => {',
            1,
        )[1].split(
            'const syncOptions:',
            1,
        )[0]
        self.assertNotIn('onApplyImported(', import_selected)
        # Immediate apply owns the commit: the imported draft domains are
        # written by the shell's auto-apply instead of a second Apply button.
        self.assertNotIn('onApplyImported', workspace)
        self.assertNotIn('{importedDirty ?', workspace)

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
        # Immediate apply: neither the imported sections nor WebDAV keep an
        # in-pane Apply button.
        self.assertNotIn('title={translate("menu.apply")}', workspace)
        self.assertNotIn('title={translate("common.saveAndApply")}', workspace)
        webdav = self.ui.split("function WebDavWorkspace(", 1)[1].split("function RuntimeField(", 1)[0]
        self.assertIn('title={translate("dataManagement.testConnection")}', webdav)
        self.assertIn('webdavActionRow', webdav)
        self.assertNotIn('title={translate("common.saveAndApply")}', webdav)

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

    def test_settings_shell_uses_a_native_source_list_sidebar(self) -> None:
        self.assertIn('settingsShell: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row" }', self.ui)
        self.assertIn('settingsSidebar: { width: 200', self.ui)
        self.assertIn('settingsSidebarHeader: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 16, paddingTop: SETTINGS_TITLEBAR_INSET + 10, paddingBottom: 8 }', self.ui)
        self.assertIn('const SETTINGS_TITLEBAR_INSET = Platform.OS === "macos" ? 32 : 0;', self.ui)
        self.assertIn('settingsDetail: { minWidth: 0, flex: 1, paddingTop: SETTINGS_TITLEBAR_INSET }', self.ui)
        self.assertIn('settingsDetailBody: { flex: 1, minHeight: 0, backgroundColor: systemColors.textBackground }', self.ui)
        self.assertIn('settingsSidebarTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" }', self.ui)
        self.assertIn('settingsSidebarDivider: { height: 1, flexShrink: 0, marginHorizontal: 12, backgroundColor: systemColors.separator }', self.ui)
        self.assertIn('settingsPaneTitle: { color: systemColors.label, fontSize: 15, fontWeight: "600" }', self.ui)
        self.assertIn('settingsSidebarList: { flex: 1, minHeight: 0 }', self.ui)
        shell = self.ui.split("function SettingsShell(", 1)[1].split("function RouteSurface(", 1)[0]
        self.assertIn('sourceList', shell)
        self.assertIn('selectedKey={pane}', shell)
        self.assertIn('rowSymbols={paneSymbols}', shell)
        self.assertIn('onSelectionChange={selectPane}', shell)
        self.assertIn('const next = SETTINGS_PANES.find(({ id }) => id === key)?.id;', shell)
        self.assertIn('native.window.open(next);', shell)
        self.assertIn('native.window.close(Platform.OS === "windows" ? pane : windowRoute);', shell)
        self.assertIn('<RouteSurface key={pane} route={pane} shell', shell)
        # Beta Display's native settings layout: an app identity header with a
        # hairline, a pane source list, and a flexible spacer. About lives in
        # the native application/tray menu instead of the sidebar.
        self.assertIn('<Text numberOfLines={1} style={styles.settingsSidebarTitle}>{translate("app.title")}</Text>', shell)
        self.assertIn('<View style={styles.settingsSidebarSpacer} />', shell)
        self.assertIn('<View style={styles.settingsPaneHeader}><Text numberOfLines={1} style={styles.settingsPaneTitle}>{translate(paneTitleKey)}</Text></View>', shell)
        self.assertIn('void native.versionInfo().then((next) => { if (active) setAppInfo(next); })', shell)
        self.assertIn('if (lastSelection.current.key === key && now - lastSelection.current.at < 250) return;', shell)
        self.assertNotIn('AboutPane', shell)
        self.assertNotIn('showAbout', shell)
        self.assertIn('versionInfo: bridge.versionInfo ? () => bridge.versionInfo!() : undefined,', (ROOT / "rn/packages/shared/src/platform/nativeBridge.ts").read_text(encoding="utf-8"))
        self.assertIn('openExternalURL: bridge.openExternalURL ? (url) => bridge.openExternalURL!(url) : undefined,', (ROOT / "rn/packages/shared/src/platform/nativeBridge.ts").read_text(encoding="utf-8"))
        self.assertIn('versionInfo: leaf.versionInfo ? () => leaf.versionInfo!() : undefined,', self.platform_entry)
        self.assertIn('openExternalURL: (url) => call("openExternalURL", url),', self.platform_entry)
        self.assertNotIn('settingsSidebarFooter', self.ui)
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        self.assertIn('export const SETTINGS_PANE_PRESENTATION: Partial<Record<AppRoute, { symbol: string; color: string; image: string }>> = {', routes)
        for symbol, color, image in (
            ('symbol: "slider.horizontal.3", color: "#34C759", image: "SidebarGeneral"', "general", "SidebarGeneral"),
            ('symbol: "square.stack.3d.up", color: "#0A84FF", image: "SidebarProviders"', "providers", "SidebarProviders"),
            ('symbol: "gearshape.2", color: "#8E8E93", image: "SidebarRuntime"', "runtime", "SidebarRuntime"),
            ('symbol: "terminal", color: "#5E5CE6", image: "SidebarCodex"', "codex", "SidebarCodex"),
            ('symbol: "arrow.up.arrow.down", color: "#30B0C7", image: "SidebarData"', "data", "SidebarData"),
            ('symbol: "list.bullet.rectangle", color: "#FF9F0A", image: "SidebarLogs"', "logs", "SidebarLogs"),
        ):
            self.assertIn(symbol, routes, color)
            self.assertTrue((ROOT / "rn/apps/macos/macos/LiteLLMMenu-macOS/Assets.xcassets" / f"{image}.imageset" / "Contents.json").exists(), image)
            self.assertTrue((ROOT / "rn/apps/windows/windows/LiteLLMMenu/Assets/Sidebar" / f"{image}.png").exists(), image)
        # The native About panel is already exported by both hosts.
        self.assertIn('showVersion: () => call("showVersion"),', self.platform_entry)
        self.assertIn('showVersion: () => bridge.showVersion?.(),', (ROOT / "rn/packages/shared/src/platform/nativeBridge.ts").read_text(encoding="utf-8"))
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
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE", "10", "15"},
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
        self.assertIn("panelFeedback: { color: colors.secondary, fontSize: UI_TIP_FONT_SIZE", relay)

        self.assertIn("constexpr CGFloat LiteLLMUIFontSize = 13.0;", macos_controls)
        self.assertNotIn("systemFontSizeForControlSize", macos_controls)
        self.assertNotRegex(macos_controls, r"(?:systemFontOfSize|monospacedSystemFontOfSize):\d")
        self.assertIn("column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);", macos_controls)
        self.assertIn("NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightMedium]", macos_controls)

        self.assertIn("private let nativeUIFontSize: CGFloat = 13", self.macos_leaf)
        self.assertIn("private final class NativeReadOnlyCodeController", self.macos_leaf)

        for source in (windows_controls, windows_leaf, windows_relay):
            self.assertIn("constexpr double kUIFontSize = 13.0;", source)
            self.assertNotRegex(source, r"FontSize\((?!kUIFontSize\)|kSourceListFontSize\)|kSourceListGlyphFontSize\))")

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
            '"menu.dataManagement": "Backup & Sync"',
            '"dataManagement.tab.import": "Import"',
            '"dataManagement.tab.export": "Export"',
            '"dataManagement.tab.webdav": "WebDAV Sync"',
            '"dataManagement.section.providersModels": "Providers & Models"',
            '"dataManagement.section.relayAccounts": "Provider accounts"',
            '"dataManagement.importHint": "Choose a file to detect its importable configuration automatically."',
            '"dataManagement.importRecognizedHint": "These configuration areas were detected in the selected file and are selected by default."',
            '"dataManagement.syncSettings": "Sync settings"',
        ):
            self.assertIn(value, english)
        for value in (
            '"menu.dataManagement": "备份与同步"',
            '"dataManagement.tab.import": "导入"',
            '"dataManagement.tab.export": "导出"',
            '"dataManagement.tab.webdav": "WebDAV 同步"',
            '"dataManagement.section.providersModels": "供应商与模型"',
            '"dataManagement.section.relayAccounts": "供应商账号"',
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
        relay = self.relay
        route = self.ui.split("const commitRelayMetadata", 1)[1].split("const flushPendingFields", 1)[0]
        removal = relay.split("const removeSelected = async (): Promise<void> => {", 1)[1].split("const setStationDraftValue", 1)[0]
        pending = relay.split("const startPendingLogin = async (): Promise<void> => {", 1)[1].split("const loginSelected", 1)[0]

        self.assertIn('commit("credential_cleanup_confirm"', relay)
        self.assertIn("commitRelayMetadata", self.ui)
        self.assertIn("await enqueueDispatch(type, payload, \"relay_accounts\");", route)
        self.assertIn("relayBridge", self.ui)
        # Deletion stages the Core metadata before the native erase.
        self.assertLess(
            removal.index('await commit("account.delete"'),
            removal.index("await native.clearRelayCredentials(removal.account.id);"),
        )
        self.assertIn("await native.clearRelayCredentials(removal.account.id);", removal)
        self.assertIn('kind: "credentials"', removal)
        # Pending logins reserve no slot; what the sign-in may keep is decided
        # by the post-login prompt inside the native browser flow.
        self.assertIn("pendingAccount: true,", pending)
        self.assertIn("post-login prompt", relay)

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
            self.assertIn("sourceList?: WithDefault<boolean, false>;", spec)
            self.assertIn("rowImageNames?: ReadonlyArray<string>;", spec)
            self.assertIn("secondaryCellKeys?: ReadonlyArray<string>;", spec)
            self.assertIn("spanningRowKeys?: ReadonlyArray<string>;", spec)
        self.assertIn("striped = true, alternatingRows = false", self.native_controls)
        self.assertIn("const stripedRows = striped && !sourceList && (alternatingRows || rows.length > 0);", self.native_controls)
        self.assertIn("alternatingRows: stripedRows,", self.native_controls)
        self.assertIn("framed = true, sourceList = false", self.native_controls)
        self.assertIn("borderless: !framed,", self.native_controls)
        self.assertIn("sourceList,", self.native_controls)
        self.assertIn("secondaryCellKeys = []", self.native_controls)
        self.assertIn("secondaryCellKeys,", self.native_controls)
        self.assertIn("const spanningRowKeys = rows.filter((row) => row.spanning).map((row) => row.key);", self.native_controls)
        self.assertIn("spanningRowKeys,", self.native_controls)
        # Five data tables plus the settings shell's pane source list, the
        # runtime table of contents, and the backup & sync rail.
        self.assertEqual(self.ui.count("<NativeTable"), 8)
        self.assertEqual(self.ui.count("alternatingRows"), 0)
        self.assertNotIn("selectedKey={selectedRoute ?? \"\"} alternatingRows", self.ui)
        self.assertEqual(self.ui.count("striped={false}"), 3)
        shell = self.ui.split("function SettingsShell(", 1)[1].split("function RouteSurface(", 1)[0]
        self.assertIn("striped={false}", shell)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", mac_native)
        self.assertIn("_tableView.style = NSTableViewStylePlain;", mac_native)
        self.assertIn("_tableView.style = sourceList ? NSTableViewStyleSourceList : NSTableViewStylePlain;", mac_native)
        self.assertIn("_tableView.selectionHighlightStyle = sourceList", mac_native)
        self.assertIn("NSTableViewSelectionHighlightStyleSourceList", mac_native)
        self.assertIn("_tableView.headerView = nil;", mac_native)
        self.assertIn("_scrollView.drawsBackground = !sourceList;", mac_native)
        # The shell and the runtime TOC coordinate their selection through the
        # JS selectedKey props, so the source-list chrome must keep empty
        # per-table selection representable.
        self.assertNotIn("_tableView.allowsEmptySelection = !sourceList;", mac_native)
        self.assertIn("NSImage *tileImage = imageName.length > 0 ? [NSImage imageNamed:imageName] : nil;", mac_native)
        self.assertIn("cell.badge.layer.backgroundColor = NSColor.clearColor.CGColor;", mac_native)
        # The sidebar material now lives on the window itself (AppDelegate),
        # so a source-list table must stay transparent instead of stacking a
        # second backdrop that would darken only the table's own area.
        self.assertNotIn("NSVisualEffectMaterialSidebar", mac_native)
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
        spanning_block = windows_native.split("if (spanning) {", 1)[1].split("if (source_list && spanning_text.empty()) {", 1)[1].split("Grid::SetColumnSpan(label", 1)[0]
        self.assertIn("label.FontSize(kUIFontSize);", spanning_block)
        self.assertIn("FontWeights::Normal()", spanning_block)
        self.assertNotIn("FontWeights::SemiBold()", spanning_block)
        self.assertNotIn("SecondaryTextBrush()", spanning_block)
        self.assertIn("style={[styles.selectableTitle, styles.tableFallbackGroupText]}", self.native_controls)
        self.assertIn('tableFallbackGroupText: { fontWeight: "400" },', self.native_controls)
        self.assertIn("spanningRowKeys", windows_native)
        self.assertIn("sourceList", windows_native)
        self.assertIn("header_frame_.Visibility(source_list ? winrt::Microsoft::UI::Xaml::Visibility::Collapsed", windows_native)
        self.assertIn('L"ms-appx:///Assets/Sidebar/"', windows_native)
        self.assertIn("Grid::SetColumnSpan(label", windows_native)
        self.assertIn("if (IsSpanningKey(Props()->rowKeys[index])) return;", windows_native)
        self.assertIn("RestoreControlledSelection();", windows_native)
        self.assertIn('rowKey + "\\x1f" + std::to_string(columnIndex)', mac_native)
        self.assertIn("static_cast<size_t>(row) >= viewProps.rowKeys.size()", mac_native)
        self.assertIn("static_cast<size_t>(columnIndex) >= columnCount", mac_native)
        self.assertIn("label.textColor = textColor;", mac_native)
        self.assertIn('props.rowKeys[row_index] + "\\x1f" + std::to_string(column_index)', windows_native)
        self.assertIn("if (alert) {", windows_native)
        self.assertIn('cell.Foreground(winrt::Microsoft::UI::Xaml::Media::SolidColorBrush{winrt::Windows::UI::Color{255, 0x6F, 0x55, 0x00}});', windows_native)
        self.assertIn("} else if (disabled || secondary) {", windows_native)
        self.assertIn("alertRowKeys?: ReadonlyArray<string>;", mac_table_spec + windows_table_spec)

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
        # Sidebar source lists use the taller settings density; data tables keep
        # the compact/normal log density.
        self.assertIn("_tableView.rowHeight = newViewProps.sourceList ? 26 : (newViewProps.compact ? 22 : 28);", mac_native)
        self.assertIn("row.MinHeight(source_list ? 26.0 : (props.compact.value_or(false) ? 22.0 : 28.0));", windows_native)
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

    def test_route_footer_is_replaced_by_immediate_apply(self) -> None:
        # The shared settings shell has no route-level Apply/Close footer; the
        # provider wizard sheet keeps its own explicit Close/Next actions.
        self.assertNotIn("function DialogFooter(", self.ui)
        self.assertNotIn('route === "runtime-settings" ? translate("common.saveAndApply") : translate("menu.apply")', self.ui)
        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('<NativeButton title={translate("menu.close")} disabled={processing} onPress={onClose} />', wizard)

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
        self.assertIn('<NativeToggle value={booleanValue(item.value)} disabled={busy} accessibilityLabel={label}', self.ui)
        self.assertIn('<TooltipText numberOfLines={1} tooltip={label} style={styles.runtimeFieldLabel}', self.ui)
        self.assertIn('styles.runtimeMultilineEditor', self.ui)
        self.assertIn('runtimeBooleanControl: { width: 40, minWidth: 40, height: 24', self.ui)
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
            "const showsTitle = props.symbolWithTitle === true || !props.symbol;",
            self.native_controls,
        )
        self.assertIn(
            "const titleWidth = showsTitle && !(compact && isCompactGlyphTitle(props.title))",
            self.native_controls,
        )
        self.assertIn(
            "{ minWidth: nativeButtonMinimumWidth(props.title, compact) + (props.symbol ? 22 : 0), flexShrink: 0 }",
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
            "const row = deployments.find((item) => stringValue(item.model) === model);",
            codex,
        )
        self.assertIn(
            "const selection: CodexModelSelection = {",
            codex,
        )
        self.assertIn(
            "supports_responses_compaction: typeof rawCompactionSupport === \"boolean\" ? rawCompactionSupport : null,",
            codex,
        )
        self.assertIn(
            "void dispatch(\"select_model\", { selection }, \"codex\");",
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
            'const [activeCategory, setActiveCategory] = useState("");',
            'const currentCategory = categories.includes(activeCategory) ? activeCategory : (categories[0] ?? "");',
            # Immediate-apply validation and modified-state affordances.
            'const validate = numericKind ? (next: string): string | undefined => {',
            'return translate("runtime.outOfRange", { min: String(minimum), max: String(maximum) });',
            'const modified = value !== "" && (isBoolean',
            'styles.runtimeModifiedBar, modified && styles.runtimeModifiedBarActive',
            'translate("runtime.resetToDefault")',
            # Reset also clears an uncommitted local draft.
            'const resetTo = useCallback((next: string): void => {',
            'useEffect(() => { if (resetToken > 0) field.resetTo(resetValue); }, [resetToken]);',
            # The assistant file editor is a window-level sheet.
            'onRegisterAssistantDialog={setAssistantDialog}',
            'onRegisterAssistantDialog(activeAssistantFile ? <AssistantFileEditorDialog {...assistantDialogProps.current} /> : null);',
            'translate("runtime.fixInvalidBeforeClose")',
            'invalidCloseNotice.current = true;',
            'invalidCloseNotice.current = false;',
            'const jumpToCategory = (name: string): void => {',
            'scrollRef.current?.scrollTo({ y: Math.max(0, offset - 2), animated: false });',
            'const trackScroll = ({ nativeEvent }: NativeSyntheticEvent<NativeScrollEvent>): void => {',
            'setSectionOffsets((current) => current[name] === nativeEvent.layout.y ? current : { ...current, [name]: nativeEvent.layout.y });',
            'onScroll={trackScroll} scrollEventThrottle={32}',
            'style={styles.runtimeScrollSurface}',
            'onSelectionChange={jumpToCategory}',
            'const tocRows = useMemo(() => categories.map((name) => ({ key: name, cells: [runtimeCategoryLabel(name, translate)] })), [categories, translate]);',
            'runtimeSectionTitle:',
            'translate("common.willClear")',
            'clearSecret({ domain: "runtime", field: "setting", target: key })',
            'runtimeCategoryLabel(name, translate)',
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
        self.assertEqual(71, len(keys))
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
        self.assertIn('!error && route !== "home" && snapshot ? (isSettingsShellRoute(route)', self.ui)

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
        self.assertIn("const REQUEST_LOG_POLL_MS = 1_000;", self.ui)
        self.assertIn("const RECOVERY_LOG_POLL_MS = 1_000;", self.ui)
        self.assertIn("const ONLINE_USAGE_POLL_MS = 15_000;", self.ui)
        self.assertIn('selected === "recovery"\n      ? RECOVERY_LOG_POLL_MS', self.ui)
        self.assertIn('selected === "requests" ? REQUEST_LOG_POLL_MS', self.ui)
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
            "runtimeWorkspaceBody:",
            "runtimeToc:",
            "runtimeSection:",
            "runtimeFieldList:",
            "runtimeModifiedBar:",
            "runtimeModifiedBarActive:",
            "runtimeFieldError:",
            "runtimeValueControlInvalid:",
            "runtimeResetButton:",
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
        # Restore defaults was removed entirely; per-setting reset in the
        # General/runtime rows is the only reset affordance.
        self.assertNotIn('runtimeRestoreButton', self.ui)
        self.assertNotIn('runtimeToolbar:', self.ui)
        self.assertNotIn('route === "runtime-settings" ? translate("common.saveAndApply")', self.ui)
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        self.assertIn('"common.restoreDefaults": "恢复默认"', chinese)

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
            '() => provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? providerKeyChoices(provider, relaySources, providerBaseURL(provider)) : [],',
            'label: providerKeyChoiceLabel({ ...choice, name: choice.name }, translate),',
            'const option = fetchKeyOptions[nativeEvent.index]; if (option) setFetchKeyID(option.value);',
            'const providerKeyOptions = [...keyStates.map((key) => ({',
            'value={selectedProviderKey?.id ?? providerKeyOptions[0]?.value ?? ""}',
            'rows.push({ key: `custom:${key.id}`, cells: [showHeaders ? `\\t${key.name}` : key.name] });',
            'const pendingCustomKeyName = useRef<string | undefined>(undefined);',
            'pendingCustomKeyName.current = undefined; // selection lands after the snapshot refresh' if False else 'setSelectedKey(`custom:${added.id}`);',
            'return dispatch("provider.key_delete", { provider_id: providerId, name: selectedKeyName });',
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
        self.assert_ui_has('const affectedModelLines = asRecords(provider?.models)')
        self.assert_ui_has('.filter((model) => stringValue(model.api_key_name).trim() === selectedKeyName)')
        self.assert_ui_has('const label = upstreamName && upstreamName !== publicName ? `${publicName} (${upstreamName})` : publicName;')
        self.assert_ui_has('title: translate("providers.deleteApiKey", { key: apiKeyDisplayName(selectedKeyName, translate) }),')
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
            'const actionSummary = staged.action_summary;',
            'operation_summary: actionSummary,',
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

    def test_provider_workspace_auto_binds_matching_stations_without_selecting(self) -> None:
        """A custom provider whose base URL targets a relay station is bound automatically.

        Selecting a provider row itself must not stage a conversion.  The
        auto-binding is URL-keyed, skips providers whose name already belongs
        to another provider, and Core keeps a rebind that changes nothing
        visible clean so no discard confirmation appears.
        """

        workspace = self.ui.split("function ProviderWorkspace(", 1)[1].split(
            "function TablePane(",
            1,
        )[0]
        self.assertIn("autoRelaySelectionKeys", workspace)
        self.assertIn("relayStationForBaseUrl(baseURL, relayStations)", workspace)
        self.assertIn("providerNameExists(providers, station.name, providerID)", workspace)
        self.assertIn('dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id })', workspace)
        self.assertIn(
            'onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }}',
            workspace,
        )

    def test_provider_source_picker_is_removed_with_automatic_station_binding(self) -> None:
        """There is no 供应商来源 switcher; base-URL matches bind automatically."""

        workspace = self.ui.split("function ProviderWorkspace(", 1)[1].split(
            "function TablePane(",
            1,
        )[0]
        # The explicit suppression plumbing is gone with the picker.
        self.assertNotIn("explicitCustomSourceProviders", workspace)
        self.assertNotIn("markExplicitCustomSource", self.ui)
        self.assertNotIn("onExplicitSourceSelected", self.ui)
        # The automatic rebind dispatch remains for base-URL matches.
        self.assertIn('dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id })', workspace)
        # The provider editor drops the source picker and the invite hint;
        # the relay-account association header is always present.
        self.assertNotIn('translate("providers.endpointSource")', self.ui)
        self.assertNotIn('translate("providers.addRelayAccountHint")', self.ui)
        editor = self.ui.split("function ProviderEditor(", 1)[1].split("function CodexWorkspace(", 1)[0]
        self.assertIn('<Text style={styles.panelTitle}>{translate("providers.accounts")}</Text>', editor)
        self.assertIn("providerAccountsHeader", editor)
        self.assertIn("addRelayAccountToVendor", editor)

    def test_provider_inspector_keeps_the_compact_provider_form_and_return_link(self) -> None:
        """The provider editor uses compact, consistently aligned rows and a source-model return link."""
        for marker in (
            'const [providerSourceModel, setProviderSourceModel] = useState<string>();',
            'function ProviderSourceFields(',
            'function providerNameExists(providers: UnknownRecord[], name: string, excludeID = ""): boolean {',
            'const [sourceResetToken, setSourceResetToken] = useState(0);',
            'providerNameExists(drafts?.providers ?? [], station.name, providerID)',
            'onBaseUrlDraftChange?.("");',
            'onNameDraftChange?.("");',
            'key={"provider-base-url:" + sourceResetToken}',
            'key={"provider-name:" + sourceResetToken}',
            'dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id })',
            'label={translate("providers.providerName")} labelWidth={88}',
            'label={translate("providers.keyName")}',
            'NativeSecretField plainText autoCommit label={translate("providers.keyValue")}',
            'title={translate("providers.backToModel", { model: sourceModelLabel })} link',
            'providerEditorHeader:',
            'providerEditorSection:',
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
        self.assertIn('"providers.wizard.duplicateName": "供应商名称已存在，请输入其他名称。"', self.zh)
        self.assertIn('"providers.wizard.duplicateName": "A provider with this name already exists. Enter a different name."', self.en)

    def test_unified_provider_workspace_lists_every_provider_kind(self) -> None:
        """服务商管理 is integrated: the provider table shows all kinds."""
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")

        self.assertIn(
            'export type ProviderAuthKind = "api_key" | "openai_login" | "claude_login";',
            types,
        )
        self.assertIn("auth_status?: ProviderAuthStatus;", types)
        self.assertNotIn("function ServiceProviderManager(", self.ui)
        self.assertNotIn('native.window.open("relay-accounts")', self.ui)
        self.assertNotIn('route === "relay-accounts"', self.ui)
        self.assertNotIn('route === "relay-add"', self.ui)
        for marker in (
            "function providerKind(provider: UnknownRecord): ProviderKind {",
            "function providerAuthKind(provider: UnknownRecord | undefined): ProviderAuthKind {",
            "function providerKindLabel(kind: ProviderKind, translate: Translate): string {",
            "const providers = useMemo(() => {",
            "cells: [providerDisplayName(item)]",
        ):
            self.assert_ui_has(marker)
        # Deleting routes through the kind-specific core action.
        self.assert_ui_has('const action = kind === "openai" || kind === "claude"')
        self.assert_ui_has('? "service_provider.delete"')
        self.assert_ui_has(': "provider.delete";')
        # Deleting the last provider of a station also removes the station
        # connection and its native sessions (old station.remove flow).
        self.assert_ui_has('await relay.commit("station.remove", { id: stationBeingRemoved.id, dependency_policy: "detach" });')
        self.assert_ui_has('translate("providers.deleteRelayProviderBody", {')
        self.assertIn('"providers.type.relay": "中转站"', self.zh)
        self.assertIn('"providers.type.openai": "GPT"', self.zh)
        self.assertIn('"providers.type.claude": "Claude"', self.zh)
        self.assertIn('"providers.type.apiKey": "API 密钥"', self.zh)
        self.assertIn('"providers.type.relay": "Relay station"', self.en)
        self.assertIn('"providers.type.apiKey": "API key"', self.en)

    def test_official_account_login_lives_in_the_provider_detail(self) -> None:
        editor = self.ui.split("function ProviderEditor(", 1)[1].split("function CodexWorkspace(", 1)[0]
        for marker in (
            'dispatchWithOutcome("service_provider.auth_start", { provider_id: id }, "providers_models")',
            'presentProviderAuthChallenge(native, translate, next, kind, providerName, id, shownChallenge.current)',
            'authAction === "service_provider.auth_start"',
            'if (authAction === "service_provider.auth_start") {',
            'await dispatch(authAction, { provider_id: id }, "providers_models");',
            '"service_provider.auth_activate"',
            'addOfficialAccount(kind === "claude" ? "claude_login" : "openai_login")',
            'field="provider_auth_token"',
            "officialStatusRow",
        ):
            self.assertIn(marker, editor)
        self.assert_ui_has("const callbackURL = stringValue(summary.redirect_uri);")
        self.assert_ui_has("...(callbackURL ? { callbackURL } : {})")
        self.assertIn("function presentProviderAuthChallenge(", self.ui)
        self.assertIn("native.showProviderAuth({", self.ui)
        # The workspace polls authorizing providers so logins started in the
        # wizard window still complete here.
        poll = self.ui.split("// Poll official-account authorizations", 1)[1].split("}, [dispatchWithOutcome, native, providers, translate]);", 1)[0]
        self.assertIn('providerAuthStatus(entry) === "authorizing"', poll)
        self.assertIn('"service_provider.auth_status"', poll)
        self.assertIn("setInterval", poll)
        self.assertIn('"providers.authStatusUnsupported": "暂不支持登录"', self.zh)
        self.assertIn("将打开 Claude 官方网页完成登录", self.zh)
        self.assertIn("official Claude sign-in page opens in your browser", self.en)

    def test_unified_workspace_stages_relay_and_provider_drafts_together(self) -> None:
        for marker in (
            'if (route === "providers-models") {',
            'return (["providers_models", "relay_accounts"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
            "const relayBridge = useMemo<RelayWorkspaceBridge>(() => ({",
            "commit: commitRelayMetadata,",
            "detectType: detectRelayType,",
            "refreshResources: refreshRelayResources,",
            "apiKeyActions: relayApiKeyActions,",
        ):
            self.assert_ui_has(marker)
        apply_body = self.ui.split("const apply = (options?: { silent?: boolean; message?: string; keepControlsEnabled?: boolean }): Promise<void> => {", 1)[1].split("const autoAppliedKey", 1)[0]
        self.assertIn('settingsRoute || route === "providers-models" || route === "data-management" || domain === undefined', apply_body)
        self.assertIn("await dispatchQueue.current;", apply_body)
        self.assertIn('return { draftStaged: next.drafts.relay_accounts?.dirty === true };', self.ui)

    def test_semantic_dispatch_rebases_one_stale_cross_window_revision(self) -> None:
        dispatch = self.ui.split(
            "const enqueueDispatch =",
            1,
        )[1].split("const dispatch: Dispatch", 1)[0]

        self.assertIn("if (!isRevisionConflict(reason) || !isRevisionRetryableAction(type)) throw reason;", dispatch)
        self.assertIn("const current = await ipc.snapshot();", dispatch)
        self.assertIn("revision.current = current.revision;", dispatch)
        self.assertIn("latestSnapshot.current = current;", dispatch)
        self.assertIn("onSnapshot(current);", dispatch)
        self.assertEqual(2, dispatch.count("await ipc.dispatch(action,"))
        self.assertNotIn("while", dispatch)
        self.assertIn("function isRevisionRetryableAction(type: string): boolean", self.ui)
        self.assertIn("normalized === \"service_provider_add\"", self.ui)
        self.assertIn("normalized === \"api_key_set_auto_grouping\"", self.ui)

    def test_request_log_display_formats_duration_and_tokens_without_changing_records(self) -> None:
        """Units convert at display time only; recorded values stay raw."""

        for marker in (
            "function formatLogDuration(",
            "function formatLogTokens(",
            "Math.round(milliseconds / 100) / 10",
            "Math.round(tokens / 100) / 10",
            "const duration = formatLogDuration(compactLogValue(value.duration_ms));",
            "`${formatLogTokens(sentTokens)} / ${formatLogTokens(receivedTokens)}`",
            "{ label: translate(\"logs.duration\"), width: 64, value: (row) => row.duration }",
            "{ label: translate(\"logs.tokenCountK\"), width: 96, value: (row) => row.tokens }",
        ):
            self.assertIn(marker, self.ui)

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
            'translate("logs.sending")',
            'translate("logs.streaming")',
            'const keyTime = tab === "requests" && requestId ? "" : time;',
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
            'logsTabs: { width: 640, maxWidth: "100%", minWidth: 0, height: 28, flexShrink: 0 },',
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

    def test_station_accounts_panel_manages_accounts_with_staged_metadata(self) -> None:
        """账号管理: add, re-login, rename, remember, and remove station accounts."""
        relay = self.relay
        for marker in (
            "export function StationAccountsPanel({",
            "const startPendingLogin = async (): Promise<void> => {",
            "pendingAccount: true,",
            "void beginAddLogin();",
            "const loginAccount = async (account: AddedRelayAccount): Promise<boolean> => {",
            "native.relayLogin({",
            "markLoginFailure(account.id, true);",
            "const restoreSavedSession = async (account: RelayAccount): Promise<boolean> => {",
            "native.restoreRelaySession({",
        ):
            self.assertIn(marker, relay)
        self.assertIn("const refreshAccountResources = async (target: ResourceRefreshTarget, silent = false)", relay)
        self.assertIn("await refreshResources(target.id);", relay)
        # Removal stages the dependency policy before the native erase and
        # persists a retry tombstone when the erase fails.
        removal = relay.split("const removeSelected = async (): Promise<void> => {", 1)[1].split("const setStationDraftValue", 1)[0]
        self.assertIn('await commit("account.delete", { id: removal.account.id, dependency_policy: removalPolicy });', removal)
        self.assertIn("await native.clearRelayCredentials(removal.account.id);", removal)
        self.assertIn('await commit("credential_cleanup_confirm", { id: removal.account.id, kind: "credentials" });', removal)
        # Quietly restore sessions once per account when the panel mounts.
        self.assertIn("const attemptedAccounts = useRef(new Set<string>());", relay)
        self.assertIn("attemptedAccounts.current.add(account.id);", relay)
        self.assertIn("const canAutoLogin = account.rememberPassword && account.passwordSaved && Boolean(account.username.trim());", relay)
        # Station connection details stay editable through staged updates.
        self.assertIn("const stageStationUpdate = async (overrides: StationDraft = {}): Promise<void> => {", relay)
        self.assertIn('await commit("station.update", { id: station.id, name, origin, type });', relay)
        self.assertIn("translate(\"relay.stationUpdateStaged\")", relay)
        # The relay family has no manual select: it comes from the station
        # type or auto-detection, and grouping lives in the manager dialog.
        self.assertNotIn('translate("relay.type")', relay)
        self.assertIn("detectType?: (origin: string) => Promise<RelayType | undefined>;", relay)
        self.assertIn("station.type ?? await detectType?.(station.origin)", relay)
        # 分组管理 stays the provider window's native subordinate sheet, but
        # it carries the pre-refactor keys list: every key is shown with its
        # group and the sheet drafts create / re-group / delete edits.
        self.assertIn("const openGroupManager = async (): Promise<void> => {", relay)
        self.assertIn("const result = await native.showGroupManager({", relay)
        self.assertIn("apiKeyActions?.create?.(account.id, { name: create.name, groupID: create.groupID, enabled: true });", relay)
        self.assertIn("apiKeyActions?.setGroup?.(account.id, update.keyID, update.groupID)", relay)
        self.assertIn("apiKeyActions?.setEnabled?.(account.id, update.keyID, update.enabled)", relay)
        self.assertIn("apiKeyActions?.update?.(account.id, update.keyID, update.name)", relay)
        self.assertIn('apiKeyActions?.remove?.(account.id, keyID, "detach_disabled");', relay)
        self.assertIn("await apiKeyActions?.setAutoGrouping?.(account.id, false);", relay)
        self.assertIn('onStatus?.(translate("relay.apiKeyGroupStaged"));', relay)
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        native_bridge = (ROOT / "rn/packages/shared/src/platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform_entry = (ROOT / "rn/packages/shared/src/platformEntry.ts").read_text(encoding="utf-8")
        mac_module = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        mac_leaf = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows_module = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_module_header = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_leaf = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        self.assertIn("showGroupManager(options: {", types)
        self.assertIn("export type RelayGroupManagerResult = {", types)
        self.assertIn("showGroupManager(options: {", native_bridge)
        self.assertIn("showGroupManager?: (options: {", platform_entry)
        self.assertIn("@objc(showGroupManager:resolver:rejecter:)", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(showGroupManager:(NSDictionary *)options", mac_bridge)
        self.assertIn("func showGroupManager(", mac_leaf)
        self.assertIn("REACT_METHOD(ShowGroupManager, L\"showGroupManager\")", windows_module_header)
        self.assertIn("ShowGroupManager(", windows_leaf)
        self.assertIn("WinUI3NativeLeafModule::ShowGroupManager(", windows_module)
        # The sheet is the pre-refactor master-detail editor: key list with
        # ＋ / －, the selected key's detail, and Close / Apply at the bottom.
        self.assertIn("private final class NativeGroupManagerController: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate", mac_leaf)
        self.assertIn("@objc private func addDraftKey(_ sender: NSButton)", mac_leaf)
        self.assertIn("@objc private func removeSelectedKey(_ sender: NSButton)", mac_leaf)
        self.assertIn("@objc private func applySheet(_ sender: NSButton)", mac_leaf)
        self.assertIn("@objc private func closeSheet(_ sender: NSButton)", mac_leaf)
        self.assertIn("@objc private func toggleSelectedEnabled(_ sender: NSButton)", mac_leaf)
        self.assertIn("func resultOnEnd() -> NativeGroupManagerResult?", mac_leaf)
        self.assertIn("let autoGrouping: Bool\n    let creates: [Create]\n    let updates: [Update]\n    let deletes: [String]", mac_leaf)
        self.assertIn("table.usesAlternatingRowBackgroundColors = true", mac_leaf)
        self.assertIn("private final class NativeListFrameView: NSView", mac_leaf)
        self.assertIn("struct GroupManagerResult {", (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.h").read_text(encoding="utf-8"))
        self.assertIn("grid.Background(index % 2 == 1", windows_leaf)
        self.assertNotIn("function RelayAccountManager(", relay)
        self.assertNotIn("relayNavigationItems", relay)

    def test_relay_add_login_asks_after_the_signin_succeeds(self) -> None:
        relay = self.relay
        ui = self.ui
        # No remember checkbox and no pre-login choice survive anywhere; the
        # native webview flow asks after the login completes. The account row
        # keeps its remember/password-saved flags only for auto-login.
        for marker in (
            'translate("relay.rememberPassword")',
            'translate("relay.addLoginPrompt")',
            'translate("relay.savePasswordAndSession")',
            'translate("relay.saveSessionOnly")',
            "showRelayLoginChoice",
            "const [addLogin, setAddLogin]",
            "providerWizardRememberRow",
        ):
            self.assertNotIn(marker, relay)
        for marker in (
            'translate("relay.rememberPassword")',
            "rememberPassword",
            "showRelayLoginChoice",
            "providerWizardRememberRow",
        ):
            self.assertNotIn(marker, ui)
        # Adding an account goes straight to the native sign-in.
        self.assertIn("await startPendingLogin();", relay)
        # The wizard's relay family is auto-detected inside the login flow:
        # no type picker, no station-details button, no type row style.
        for marker in (
            "providerWizardTypeRow",
            "manualType",
            "setupStepStation",
            'translate("relay.type")',
        ):
            self.assertNotIn(marker, ui)
        self.assertIn("const accountType = await resolveRelayType();", ui)
        self.assertIn("detectType={relay.detectType}", ui)

    def test_relay_inline_edits_stage_without_local_save_buttons(self) -> None:
        relay = self.relay
        self.assertNotIn('symbol="check"', relay)
        self.assertNotIn('title={translate("common.save")}', relay)
        self.assertIn("relay.apiKeyActions.update?.(selectedProvided.account.id, selectedProvided.resource.id, name)", self.ui)
        self.assertIn("onCommit={() => { void stageStationUpdate(); }}", self.ui)
        self.assertIn("await relay.commit(\"station.update\", { id: station.id, name, origin, type });", self.ui)
        self.assertIn("onStageStationUpdate=", self.ui)
        self.assertIn('translate("relay.stationUpdateStaged")', relay)
        apply_body = self.ui.split("const apply = (options?: { silent?: boolean; message?: string; keepControlsEnabled?: boolean }): Promise<void> => {", 1)[1].split("const autoAppliedKey", 1)[0]
        self.assertIn("await dispatchQueue.current;", apply_body)
        # Relay/provider drafts are committed by the shell's immediate apply;
        # the removed footer no longer renders a route-level Apply button.
        self.assertNotIn('title={translate("menu.apply")}', self.ui)

    def test_provided_keys_panel_groups_station_keys_with_staged_crud(self) -> None:
        ui = self.ui
        for marker in (
            '// Group headers only separate the two kinds; a single-kind list skips',
            '// them so custom-only vendors see a plain key table.',
            'if (showHeaders) rows.push({ key: "group:custom", cells: [`${translate("providers.keysCustom")} · ${customKeys.length}`], spanning: true });',
            'rows.push({ key: `custom:${key.id}`, cells: [showHeaders ? `\\t${key.name}` : key.name] });',
            '// it shrinks until the whole inspector fits, so the pane keeps no scrollbar',
            'const keysTableInlineRowHeights = tableRows.map((row) => (row.spanning ? 28 : 22));',
            'const keysTableInlineAvailableHeight = keysTableInlineSiblingHeight === undefined ? undefined : paneViewportHeight - keysTableInlineSiblingHeight - 1;',
            'keysTableInlineHeight = Math.min(keysTableInlineContentHeight, Math.max(fitted, Math.min(keysTableInlineMinHeight, keysTableInlineContentHeight)));',
            'React.useLayoutEffect(() => { keysTableInlineRenderedHeight.current = keysTableInlineHeight; }, [keysTableInlineHeight]);',
            "rows.push({ key: `account:${account.id}`, cells: [accountDisplayName(account, translate)], spanning: true });",
            "cells: [`\\t${providedNameDrafts[row.key] ?? row.label}`],",
            "providedRows",
            "ApiKeyCreateDialog",
            "DependencyPolicyDialog",
            "relay.apiKeyActions.update?.(selectedProvided.account.id, selectedProvided.resource.id, name)",
            "relay.apiKeyActions.create?.(account.id, options)",
            'relay.apiKeyActions.detach?.(selectedProvided.account.id' if False else 'relay.apiKeyActions.detach?.(account.id, resource.id)',
            "relay.apiKeyActions.remove?.(account.id, resource.id, remoteDeletePolicy)",
            'target={`${selectedProvided.account.id}:${selectedProvided.resource.id}`}',
            'domain="relay_accounts"',

        ):
            self.assertIn(marker, ui)
        relay = self.relay
        for marker in (
            "export function providedKeyRows(accounts: RelayAccount[], translate: Translate): ProvidedKeyRow[]",
            "export function ApiKeyCreateDialog(",
            "export function DependencyPolicyDialog<",
            "accountLabel: accountDisplayName(account, translate),",
            "resourceGroupUnavailable(resource, account.groups)",
        ):
            self.assertIn(marker, relay)

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
            'return (["providers_models", "relay_accounts"] as const).filter',
            'function relaySourcesForBaseUrl(',
            'function providerKeyChoices(provider: UnknownRecord, relaySources: RelaySourceOption[], baseURL?: string): ProviderKeyChoice[] {',
            'function ProviderSourceFields(',
            'function relayStationsFromSnapshot(',
            'const station = relayStationForBaseUrl(endpoint, relayStations);',
            'dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id })',
            'const matchingRelaySources = relaySourcesForBaseUrl(',
            '() => provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? providerKeyChoices(provider, relaySources, providerBaseURL(provider)) : [],',
            'const keyChoices = selectedProvider ? providerKeyChoices(selectedProvider, relaySources, activeProviderBaseURL) : [];',
            'const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";',
            'dispatch("model.select_relay_resource"',
            'const providerKeyName = drafts?.providerKeyDisplayName(providerId, providerKey.id, providerKey.name) ?? providerKey.name;',
            'changes: { provider_key_id: providerKey.id, api_key_name: providerKeyName },',
            'providerKeyOptions.length > 0 ? <PickerField label={translate("providers.providerKey")}',
            'modelOrderMode(activeRoute.model) === "relay_multiplier"',
            'const canFollowMultiplier = usesRelayKey && relayMultiplier !== undefined;',
            'label={translate("providers.order")}',
            'label={translate("providers.followMultiplier")}',
            '{canFollowMultiplier ? <NativeCheckbox',
            'result.status === "partial"',
            'result.status === "failed"',
            'setIssues(applyIssuesForDisplay(value));',
            'if (!result.domains?.includes("relay_accounts") && result.status === "applied"',
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
        self.assertIn('"providers.keysProvided": "供应商提供"', self.zh)
        # The station-managed key value stays behind the read-only native
        # secure-input capability inside the provided-keys panel.
        provided = self.ui.rsplit("function ProviderKeysPanel", 1)[1].split("function CodexWorkspace", 1)[0]
        self.assertIn('domain="relay_accounts"', provided)
        # The provided key's editor renders its value read-only through the
        # native secure-input capability, next to the copy action.
        provided_secret = [
            chunk.split("/>", 1)[0]
            for chunk in provided.split("<NativeSecretField")[1:]
            if "selectedProvided.account.id" in chunk.split("/>", 1)[0]
        ]
        self.assertTrue(provided_secret, "provided key secret field")
        self.assertIn("disabled", provided_secret[0])
        self.assertIn("plainText", provided_secret[0])
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
            'linkedModelCount: count(entry.linked_model_count)',
            'pendingOperationCount: count(entry.pending_operation_count)',
        ):
            self.assertIn(marker, relay)
        # Remote-delete policies moved with the keys panel into the ui.
        for marker in (
            'value: "detach_disabled"',
            'value: "detach_only"',
        ):
            self.assertIn(marker, self.ui)

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
        relay = self.relay

        # Keep this semantic: empty account and key lists explain the next
        # action instead of freezing a large placeholder height.
        self.assertIn('<View style={styles.accountsEmpty}><Text style={styles.accountsEmptyText}>{translate("relay.stationNoAccounts")}</Text></View>', relay)
        self.assertIn('isRelay && stationAccounts.length === 0 ? translate("providers.providedKeysNeedAccount") : translate("providers.keyListHint")', self.ui)
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

        relay = self.relay
        for marker in (
            'formRow: { width: "100%", minHeight: 30, flexDirection: "column", alignItems: "stretch", gap: 5 }',
            'accountActionsRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" }',
            'accountsEmpty: { minHeight: 40, alignItems: "center", justifyContent: "center", paddingHorizontal: 10, paddingVertical: 8, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel }',
        ):
            self.assertIn(marker, relay, marker)
        for marker in (
            'keysEditorRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" }',
            'keysGroupPicker: { flex: 1, minWidth: 120, height: 26 }',
            'panelActionButton: { width: 22, minWidth: 22, height: 22 }',
            'providerAccountsHeader: { minWidth: 0, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator },',
        ):
            self.assert_ui_has(marker)
        self.assert_ui_not_has('relayAccountInvite:')
        self.assert_ui_not_has('translate("providers.addRelayAccountHint")')
        for web_card_marker in ("accountOverview:", "resourcesCard:"):
            self.assertNotIn(web_card_marker, relay)
        for redundant_separator in (
            'sidebarHeader: { height: 36, minHeight: 36, paddingHorizontal: 10, flexDirection: "row", alignItems: "center", gap: 8, borderBottomWidth:',
            'accountMetadata: { minHeight: 38, paddingHorizontal: 12, paddingVertical: 5, flexDirection: "row", flexWrap: "wrap", alignItems: "center", columnGap: 24, rowGap: 4, borderTopWidth:',
        ):
            self.assertNotIn(redundant_separator, relay)
        # The unified provider workspace absorbs the old relay route content,
        # so the shared app no longer reserves a relay route style.
        self.assertNotIn('relayAccountsContent:', self.ui)

    def test_wizard_add_relay_account_embeds_the_station_sign_in(self) -> None:
        """添加向导: the relay login path opens the station sign-in inline."""
        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertLess(wizard.index('label={translate("providers.wizard.keyPath")}'), wizard.index('label={translate("providers.wizard.selectApiKey")}'))
        self.assertIn('<NativeSegmentedControl labels={[translate("providers.wizard.pathManual"), translate("providers.wizard.pathLogin")]}', wizard)
        # The vendor's base URL is the station origin; no separate station
        # picker and no vendor type split.
        self.assertNotIn('translate("relay.stationChoice")', wizard)
        self.assertNotIn("stationMode", wizard)
        self.assertIn("const beginRelayLogin = async (): Promise<void> => {", wizard)
        self.assertIn("provider.select_relay_station", wizard)
        # Pending login: the shell is created only after sign-in succeeds, so
        # a cancelled flow reserves nothing and cannot cascade a station away.
        self.assertIn("pendingAccount: true,", wizard)
        self.assertNotIn("await relay.addAccount(", wizard)
        self.assertNotIn('relay.commit("account.delete"', wizard)
        self.assertIn("embedded: true,", wizard)
        self.assertIn("native.cancelRelayLogin();", wizard)
        self.assertIn('provider.select_relay_station', wizard)
        # Login providers keep the official device-code flow on the keys step.
        self.assertIn("service_provider.auth_start", wizard)
        self.assertIn("const goBack = (): void => {", wizard)
        self.assertIn("cancelRelaySignIn();", wizard)
        self.assertNotIn("<ScrollView", wizard.split('loginPhase === "sign-in" ? <', 1)[0])

    def test_provider_table_columns_fit_the_fixed_provider_pane(self) -> None:
        self.assertIn('"providers.modelCount": "Count"', self.en)
        self.assertIn('"providers.modelCount": "模型数"', self.zh)
        self.assertNotIn('"providers.routeSource"', self.zh)
        self.assertIn('"providers.order": "顺序"', self.zh)
        self.assertIn('"providers.followMultiplier": "跟随倍率"', self.zh)
        self.assertNotIn('"providers.effectiveOrder"', self.zh)
        self.assertIn('"providers.keyName": "密钥名"', self.zh)
        self.assertIn('"providers.keyValue": "密钥值"', self.zh)
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 132 }]}')
        self.assert_ui_has('providerListPane: { width: 140, minWidth: 140, maxWidth: 140')
        self.assert_ui_has('columns={[{ label: translate("providers.keyModelColumn"), width: 124 }, { label: translate("providers.model"), width: 108 }, { label: translate("common.order"), width: 48 }]}')
        self.assertIn('"providers.keyOrderColumn": "密钥名 / 顺序"', self.zh)
        self.assertIn('"providers.keyOrderColumn": "Key / Order"', self.en)
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('keysSection: { flex: 3, minHeight: 170 }')
        self.assert_ui_has('modelPane: { flex: 1, minWidth: 0, minHeight: 130, paddingTop: 2, borderTopWidth: 1, borderTopColor: systemColors.separator }')
        self.assert_ui_has('keysPane: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('keysInline: { minWidth: 0, gap: 6, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator }')
        self.assert_ui_has('keysEditorRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" }')
        self.assert_ui_has('keysHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15, flexShrink: 1 }')
        self.assert_ui_has('<ProviderKeysPanel')
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
        self.assertIn('keysEditorRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" }', self.ui)

    def test_relay_tables_use_compact_native_zebra_rows_and_shared_alignment(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        # The accounts table uses the native compact zebra table; the keys
        # table inherits the striped default. Checkboxes stay in dialogs.
        self.assertEqual(1, relay.count("striped"))
        self.assertNotIn("striped={false}", relay)
        for marker in (
            'accountsTable: { flex: 0, height: 84, minHeight: 84, flexShrink: 0 }',
        ):
            self.assertIn(marker, relay)

    def test_relay_resource_columns_fit_the_initial_minimum_viewport(self) -> None:
        """The unified keys table is a single readable column in the detail pane."""
        match = re.search(
            r'columns=\{variant === "inline"\n?\s*\? \[\{ label: translate\("providers\.keys"\), width: (\d+) \}\]',
            self.ui,
        )
        self.assertIsNotNone(match, "inline keys table columns")
        self.assertEqual((264,), tuple(int(value) for value in match.groups()))

    def test_webdav_form_is_integrated_into_the_unified_sync_tab(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            "function WebDavWorkspace(",
            '{tab === "webdav" ? <View style={[styles.dataManagementWebDavPane, styles.dataManagementWebDavContent, dataManagementPolishStyles.webDavContent]}>',
            '<WebDavWorkspace snapshot={snapshot} busy={busy || webDavOperationBusy} status={statuses.webdav} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onProbe={onProbeWebDav}>',
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
        self.assertNotIn('webdavActionSpacer:', self.ui)
        self.assertNotIn('webdavHeaderActions:', self.ui)
        webdav_component = self.ui.split("function WebDavWorkspace(", 1)[1].split("function WebDavPasswordField(", 1)[0]
        # Immediate apply: the WebDAV form keeps only the connection probe; the
        # staged fields are written by the shell's auto-apply.
        self.assertIn('title={translate("dataManagement.testConnection")}', webdav_component)
        self.assertNotIn('title={translate("common.saveAndApply")}', webdav_component)
        self.assertGreater(webdav_component.index('title={translate("dataManagement.testConnection")}'), webdav_component.index('<View style={[styles.webdavSyncArea, dataManagementPolishStyles.webDavSyncArea]}>{children}</View>'))
        self.assertNotIn("dataManagementSectionGrid:", self.ui)
        self.assertNotIn("footerCompact:", self.ui)
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
        self.assertIn('cells: [providerDisplayName(item)]', self.ui)
        self.assertIn('const grouped = new Map<string, UnknownRecord[]>();', self.ui)
        self.assertIn(r'cells: [`\t${providerDisplayName(entry.provider)}`', self.ui)
        self.assertIn("modelUpstreamDisplay", self.ui)
        self.assertIn("providerKeyDisplayName", self.ui)
        self.assertIn('value={drafts?.providerKeyDisplayName(providerId, selectedCustom.id, selectedCustom.name) ?? selectedCustom.name}', self.ui)
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
        self.assertIn("const [providedNameDrafts, setProvidedNameDrafts] = useState<Record<string, string>>({});", self.ui)
        self.assertIn("providedNameDrafts[selectedProvided.key] ?? selectedProvided.keyName", self.ui)
        self.assertIn("const stationDraft = stationDraftProp ?? internalStationDraft;", relay)
        self.assertIn("stationDraft.name ?? stationDisplayName(station, translate)", self.ui)
        self.assertIn("value={stationDraft.origin ?? station.origin}", self.ui)
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
        self.assert_ui_has('dispatch("use_local_api", {}, "codex")')
        self.assert_ui_has('dispatch("use_local_api", {}, "claude")')
        self.assert_ui_has('title={translate("dataManagement.syncNow")}')
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
        # Settings panes share one window, so its title is the app name and the
        # sidebar selection names the active pane.
        self.assertIn('if Self.settingsPaneRoutes.contains(canonicalRoute(route)) {', self.macos_leaf)
        self.assertIn('return localized("appTitle", fallback: "LiteLLM Menu")', self.macos_leaf)
        self.assertIn('case "provider-wizard": return "LiteLLM " + localized("routeProviderWizard", fallback: "Add Provider")', self.macos_leaf)
        self.assertIn("private static let statusMenuOrder", self.macos_leaf)
        for ordered_item in (
            '"toggle-autostart", "toggle-codex-model-catalog", "separator"',
            '"open-providers-models", "open-runtime-settings", "open-codex-settings", "separator"',
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
        self.assertIn('{ id: "general-settings", titleKey: "menu.general" }', routes)
        self.assertIn('{ id: "data-management", titleKey: "menu.dataManagement" }', routes)
        self.assertIn('| "data-management"', types)
        self.assertIn("routeDataManagement: string;", types)
        for source in (self.ui, self.macos_leaf, self.windows_leaf):
            self.assertIn("open-data-management", source)
        # The settings window owns every pane, so the native hosts no longer
        # map a per-route window title for data management.
        for source in (self.macos_leaf, self.windows_leaf):
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
