from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE = ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx"
NATIVE_CONTROLS = ROOT / "rn/packages/shared/src/ui/NativeControls.tsx"
MACOS_LEAF = ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift"
MACOS_PROJECT = ROOT / "rn/apps/macos/macos/YoungRouter.xcodeproj/project.pbxproj"
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

    def test_pane_status_bar_stays_mounted_and_falls_back_to_ready(self) -> None:
        # The pane status bar is a permanent strip: a pane window always shows
        # it, and an idle pane shows the localized Ready label instead of an
        # empty row that appears and disappears with each action.
        self.assert_ui_has('{shell ? <View style={styles.routeStatusBar}><Text numberOfLines={2} style={styles.routeStatusText}>{result ?? translate("common.ready")}</Text></View> : null}')
        self.assertNotIn("{shell && result ? <View style={styles.routeStatusBar}>", self.ui)
        self.assertIn('"common.ready": "Ready"', self.en)
        self.assertIn('"common.ready": "就绪"', self.zh)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        self.assertIn('| "common.ready"', translation_keys)
        # The strip is a tip surface: one type step below body text.
        self.assert_ui_has('routeStatusText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE }')

    def test_bootstrap_menu_uses_the_system_language_before_core_snapshot(self) -> None:
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        self.assertIn('const bootstrapTranslate = createTranslator("system", systemLocale);', self.platform_entry)
        self.assertIn('import { routeMenuActions } from "./routes";', self.platform_entry)
        self.assertIn('routeMenuActions(bootstrapTranslate)', self.platform_entry)
        self.assertIn('{ id: "logs", titleKey: "status.logs" }', routes)
        self.assertIn('id !== "claude-settings" && id !== "provider-wizard"', routes)

    def test_status_menu_uses_one_localized_recovery_logs_action(self) -> None:
        self.assertIn('function recoveryLogMenuTitle(', self.ui)
        self.assertIn('translate("status.logsSummary", { recovering, cooldown })', self.ui)
        self.assertIn('autoStart: translate("status.autoStart")', self.ui)
        self.assertIn('{ id: "open-logs", title: recoveryLogMenuTitle(snapshot.service, translate), enabled: true },', self.ui)
        self.assertNotIn('{ id: "open-claude-settings", title: translate("status.claude"), enabled: true },', self.ui)
        self.assertNotIn('{ id: "open-recovery", title: translate("status.recovery"), enabled: true },', self.ui)
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
            '"status.logsSummary": "日志 (路由恢复 {recovering}, 冷却 {cooldown})"',
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
            'type AssistantSettingsDomain = "codex" | "claude" | "clients";',
            'const settingsRoute = isAssistantSettingsRoute(route);',
            'const domain = settingsRoute ? undefined : domainForRoute(route);',
            '<AssistantSettingsWorkspace',
            'externalSettingsWorkspace',
            '<SettingsRail',
            'await flushPendingFields();',
            "assistantFileSurfaceStyles.fileRow",
            'const stagedDomainsForRoute = useCallback((currentSnapshot: CoreSnapshot | undefined): ConfigDomain[] => {',
            'if (settingsRoute) {',
            'return (["codex", "claude", "clients"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
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
        # holds immediate apply while open and saves (and closes) on its own
        # button.
        self.assert_ui_has('setAssistantEditorOpen(requested);')
        self.assert_ui_has('title={translate("status.saveAndClose")}')
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
            'title={probeTitle}',
            'function modelProbePresentation(',
            '? translate("providers.probeInlineReady")',
            '? translate("providers.probeInlinePartial")',
            '? translate("providers.probeInlineRefused")',
            '? translate("providers.probeInlineTimeout")',
            'const uniformStatus = surfaceStatuses.size === 1 ? [...surfaceStatuses][0] : "";',
            '? translate("providers.probeInlineAuthError")',
            '? translate("providers.probeInlineUnsupported")',
            'const compactReason = (deepTestIncluded ? degradationLine : "")',
            'const compact = [availabilityCount, compactReason].filter(Boolean).join("; ");',
            'translate("providers.probeResultPrefix")',
            'styles.inspectorProbeFinding}',
            'void native.showReadOnlyText({',
            'language: "text"',
            'translate("providers.probeOriginalRequest"',
            'const applyProbedSurface: ApplyProbedSurface = (providerId, modelId, nextSurface, options) => {',
            'const currentSurface = stringValue(currentModel.upstream_url_surface, "openai/responses");',
            'if (currentSurface === nextSurface) return;',
            'await enqueueDispatch("model.patch", {',
            'const applyStagedSurface = (nextRevision: number): Promise<IpcResults["apply"]> => (',
            'result = await applyStagedSurface(staged.revision);',
            'result = current.drafts.providers_models?.dirty === true',
            '? await applyStagedSurface(current.revision)',
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

    def test_host_bridges_keep_a_long_model_probe_alive(self) -> None:
        """A 30 s loopback timeout used to cut off a slow deep test."""

        macos_bridge = (ROOT / "rn/apps/macos/src/native/macos/CoreIPCBridge.swift").read_text(encoding="utf-8")
        windows_bridge = (ROOT / "rn/apps/windows/src/native/windows/CoreIPCBridge.cpp").read_text(encoding="utf-8")
        self.assertIn("private static func responseTimeoutInterval(for method: String) -> TimeInterval {", macos_bridge)
        self.assertIn("method == \"probe\" ? 900 : 30", macos_bridge)
        self.assertIn("timeoutInterval: Self.responseTimeoutInterval(for: metadata.method)", macos_bridge)
        self.assertIn('const int receive_timeout_ms = method == "probe" ? 900000 : 30000;', windows_bridge)
        self.assertIn('Request(endpoint, L"", L"POST", request_json, session, receive_timeout_ms);', windows_bridge)

    def test_model_detail_deep_test_names_the_degradation_probe_before_it_runs(self) -> None:
        # The button reports what the deep test will actually run: a Responses
        # route whose name the staged engine can attribute adds the
        # degradation fingerprint probe, every other route stays a plain probe.
        for marker in (
            'const degradationIncluded = booleanValue(asRecord(model.deep_probe).includes_degradation);',
            'const probeTitle = degradationIncluded',
            '? translate("providers.deepTest")',
            ': translate("providers.probe");',
            '<ActionButton title={probeTitle} titleWidth="tight" busy={probing} toolTip={(probePresentation.compact ? probePresentation.tooltip : "") || (degradationIncluded ? translate("providers.deepTestHint") : undefined)} disabled={(busy && !probing) || !probeReady} onPress={probe} />',
            'function probeDegradationLine(degradation: UnknownRecord, translate: Translate, detailed = false): string {',
            'function probeDegradationDetails(degradation: UnknownRecord, translate: Translate): string {',
            'const degradation = asRecord(result?.degradation ?? probe.degradation);',
            'case "matched":',
            'case "mismatch":',
            'case "unavailable":',
            'translate("providers.degradationEngine", { engine: stringValue(engine.name), revision: revision || "?" })',
            'titleWidth="tight"',
            'accessibilityRole="link" accessibilityLabel={probePresentation.compactSentence}',
            'const degradationSentence = probeDegradationLine(degradation, translate, true);',
            'const tooltip = joinProbeSummary([compactSentence, availabilitySentence, degradationSentence]);',
            '? translate("providers.probeInlineReady")',
            '? translate("providers.probeInlinePartial")',
            '? translate("providers.probeInlineRefused")',
            '? translate("providers.probeInlineTimeout")',
            'const uniformStatus = surfaceStatuses.size === 1 ? [...surfaceStatuses][0] : "";',
            '? translate("providers.probeInlineAuthError")',
            '? translate("providers.probeInlineUnsupported")',
            'const compactReason = (deepTestIncluded ? degradationLine : "")',
            'const compact = [availabilityCount, compactReason].filter(Boolean).join("; ");',
            'translate("providers.probeResultPrefix")',
            'styles.inspectorProbeFinding}',
        ):
            self.assert_ui_has(marker)
        # Only the availability finding may ever change stored routing; a
        # fingerprint mismatch is reported and nothing else.
        self.assertNotIn('degradation.status === "mismatch") ', self.ui)
        for marker in (
            'const unreachableCount = surfaces.filter((surface) => surface.status === "network_error").length;',
            '? translate("providers.probeSummaryUnreachable")',
            ': translate("providers.probeSummaryUnavailable")',
            '.filter((text, index, all) => all.indexOf(text) === index)',
            'case "unreachable":',
            'function probeFailureKey(degradation: UnknownRecord): TranslationKey {',
            'case "auth_error":',
            'return "providers.probeInlineAuthError";',
        ):
            self.assert_ui_has(marker)
        for marker in (
            '"providers.probeResultPrefix": "结果:"',
            '"providers.probeAvailabilityCount": "{available}/{total} 可用"',
            '"providers.probeSummaryUnavailable": "不可用"',
            '"providers.probeSummaryUnreachable": "上游未响应"',
            '"providers.degradationMatched": "未降智"',
            '"providers.degradationMatchedDetail": "未降智（指纹与 {target} 一致）"',
            '"providers.degradationMismatch": "疑似降智"',
            '"providers.degradationMismatchDetail": "降智：指纹更像 {label}（目标 {target}）"',
            '"providers.degradationUnknown": "指纹存疑"',
            '"providers.degradationUnreachable": "上游未响应"',
        ):
            self.assertIn(marker, self.zh)
        # The result line carries the finding itself: a "总结:" label would only
        # eat the width the pane needs for the whole short result.
        self.assertNotIn("总结", self.zh)
        self.assertIn('"providers.degradationMatched": "Not degraded"', self.en)
        self.assertIn('"providers.degradationMismatch": "Looks degraded"', self.en)
        self.assertIn('"providers.probeSummaryUnreachable": "Upstream did not answer"', self.en)
        self.assertIn('"providers.probeResultPrefix": "Result:"', self.en)
        self.assertIn('"providers.probeAvailabilityCount": "{available}/{total} available"', self.en)
        self.assertIn('"providers.probeInlineRefused": "Refused"', self.en)
        self.assertIn('"providers.probeInlineAuthError": "Key refused"', self.en)
        for marker in (
            'function nativeButtonTightWidth(title: string, plainLink = false): number {',
            'return Math.max(44, Math.ceil(nativeControlTextWidth(title) + 20));',
            'titleWidth?: "auto" | "tight" | "flex";',
            'props.busy === undefined ? reservation : nativeButtonBusyMinimumWidth(props.title, reservation, props.plainLink === true)',
            'function NativeButtonWithRef({ titleWidth: titleWidthRequest, ...props }, ref)',
            'const BUSY_SPINNER_SIZE = 12;',
        ):
            self.assertIn(marker, self.native_controls)
        self.assert_ui_has('titleWidth={titleWidth} toolTip={toolTip} onPress={onPress} style={style} />;')
        for marker in (
            '"providers.probe": "探测"',
            '"providers.deepTest": "深测"',
            '"providers.deepTestHint": "先验可用性，再比对降智指纹（TraceOne）"',
            '"providers.degradationUnavailable": "深测不可用"',
            '"providers.degradationError": "深测失败"',
        ):
            self.assertIn(marker, self.zh)
        for marker in (
            '"providers.probe": "Probe"',
            '"providers.deepTest": "Deep test"',
        ):
            self.assertIn(marker, self.en)

    def test_unprobed_models_do_not_render_a_status_placeholder(self) -> None:
        self.assert_ui_has('return { compact: "", compactSentence: "", tooltip: "", full: "" };')
        self.assertIn('export function nativeControlTextWidth(label: string): number {', self.native_controls)
        self.assertIn('if (plainLink) return Math.ceil(nativeControlTextWidth(title)) + 2;', self.native_controls)
        self.assert_ui_has('{probePresentation.compactSentence ? <Pressable style={styles.inspectorProbeFinding}')
        self.assert_ui_has('onPress={openProbeDetails} accessibilityRole="link" accessibilityLabel={probePresentation.compactSentence}')
        self.assert_ui_has('inspectorProbeFindingText: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, textDecorationLine: "underline" }')
        # A link also owns its cursor: the pointing hand, on both platforms.
        self.assert_ui_has('inspectorProbeFinding: { flexShrink: 1, minWidth: 0, cursor: "pointer" }')
        self.assert_ui_has('const compactSentence = `${translate("providers.probeResultPrefix")} ${compact}`;')
        self.assert_ui_has('ellipsizeMode="tail" tooltip={probePresentation.tooltip}')
        # The whole finding - label included - is one grey underlined link: the
        # underline is what says it can be clicked, so nothing is bolded.
        self.assertNotIn('inspectorProbeResultLabel', self.ui)

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
            '"providers.protocolModeHelp": "协议说明"',
        ):
            self.assertIn(marker, self.zh)
        self.assertNotIn('"providers.protocolModeFallback": "兜底协议"', self.zh)
        self.assertIn('"providers.protocolModeFallback": "Auto-adapt"', self.en)
        self.assertIn('"providers.fallbackProtocol": "Backup protocol"', self.en)
        self.assertIn('"providers.protocolModeHelp": "Protocol help"', self.en)
        self.assertIn('| "providers.protocolModeHelp"', (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8"))

    def test_protocol_mode_tip_opens_from_the_question_mark_beside_the_picker(self) -> None:
        """The mode's sentence is a tip on demand, opened beside the picker."""

        for marker in (
            'function HelpTip({ open, text, title, onToggle }: { open: boolean; text: string; title: string; onToggle: () => void }): React.JSX.Element {',
            '<NativeButton title="" symbol="help" link plainLink toolTip={title} accessibilityLabel={title} onPress={onToggle} style={styles.helpTipButton} />',
            '{open ? <View style={styles.helpTipPopup}><Text style={styles.helpTipText}>{text}</Text></View> : null}',
            'const protocolHint = translate(fixed ? "providers.protocolModeFixedHint" : "providers.protocolModeFallbackHint");',
            'accessory={<HelpTip open={tipOpen} text={protocolHint} title={translate("providers.protocolModeHelp")} onToggle={() => setTipOpen((current) => !current)} />}',
            'const [tipOpen, setTipOpen] = useState(false);',
            'const selectMode = (upstream_protocol_mode: string): void => {',
            'const selectProtocol = (upstream_url_surface: string): void => {',
            # The mark rides the picker's own row, after the control, and the
            # label column keeps its standard width.
            'accessory?: React.ReactNode',
            'labelWidth={60} allowShrink value={protocol}',
            'controlWidth === undefined ? null : { width: controlWidth, flex: 0 }]} />{accessory ?? null}</View>',
            # Anywhere else clicks the sheet drawn under the mark and its panel,
            # so the tip closes on the next click outside itself.
            '{open ? <Pressable accessible={false} onPress={onToggle} style={styles.helpTipDismiss} /> : null}',
            'helpTipDismiss: { position: "absolute", left: -2400, right: -2400, top: -2400, bottom: -2400 }',
            'helpTipButton: { width: 16, height: 16, minWidth: 16, minHeight: 16 }',
            'helpTipPopup: { position: "absolute", right: 0, bottom: 22, width: 208',
            'helpTipText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 15 }',
        ):
            self.assert_ui_has(marker)
        self.assert_ui_not_has('styles.protocolHint')
        self.assert_ui_not_has('protocolHint: { marginLeft: 62')
        # The mark is the platform's own help glyph on a quiet icon-only link,
        # never a bezelled button and never a drawn text mark, and it never
        # squeezes the label column into a wrap.
        self.assert_ui_not_has('helpTipGlyph')
        self.assert_ui_not_has('labelAccessory')
        self.assert_ui_not_has('formRowLabelGroup')
        self.assertIn('symbol == "help"', (ROOT / "rn/apps/windows/src/native/windows/WinUIControls.cpp").read_text(encoding="utf-8"))
        self.assertIn('symbol == "help"', (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(encoding="utf-8"))
        # The tip follows the live mode, and a chosen value answers it.
        self.assert_ui_has('setTipOpen(false);\n    void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_protocol_mode } });')
        self.assert_ui_has('setTipOpen(false);\n    void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_url_surface } });')

    def test_provider_probe_button_reports_progress_without_graying_out(self) -> None:
        self.assert_ui_has(
            '<ActionButton title={probeTitle} titleWidth="tight" busy={probing} toolTip={(probePresentation.compact ? probePresentation.tooltip : "") || (degradationIncluded ? translate("providers.deepTestHint") : undefined)} disabled={(busy && !probing) || !probeReady} onPress={probe} />'
        )
        # The probe keeps its own name while it runs: the button carries the
        # progress as a leading spinner instead of a swapped title.
        self.assert_ui_not_has('title={probing ? translate("providers.probing")')
        self.assert_ui_has(
            "onPress={probe} />{probePresentation.compactSentence ? <Pressable style={styles.inspectorProbeFinding}",
        )
        self.assert_ui_not_has("probeSummaryTrigger")
        self.assert_ui_not_has('styles.probeSummary}')
        self.assert_ui_has('const probeReady = Boolean(')
        self.assertNotIn('await flushPendingFields();\n      const before = await ipc.snapshot();', self.ui)

    def test_loading_buttons_keep_their_title_width_and_color_with_a_spinner(self) -> None:
        """A working button reports progress; it never disables, renames, or resizes itself."""

        # The shared contract: a busy button is handed a spinner the native
        # control draws as the button's leading image, so it centers the icon
        # with the title as one group and keeps the width its own reservation
        # already gave it. A caller that hugs its label adds that slot itself.
        for marker in (
            "busy?: boolean;",
            "busy: props.busy === true,",
            "function nativeButtonBusyMinimumWidth(",
            "const BUSY_SPINNER_SLOT = 2 * (BUSY_SPINNER_SIZE + BUSY_SPINNER_GAP + BUSY_SPINNER_BEZEL_ROOM);",
        ):
            self.assertIn(marker, self.native_controls)
        for component in ("macos", "windows"):
            spec = (ROOT / f"rn/packages/shared/src/ui/{component}/NativeButtonNativeComponent.ts").read_text(encoding="utf-8")
            self.assertIn("busy?: WithDefault<boolean, false>;", spec)
        # Every loading button reports its own progress and keeps its label;
        # the disabled state is left for controls that merely wait on a sibling.
        for marker in (
            'busy={processing} disabled={wizardBusy && !processing}',
            'busy={loginBusy} disabled={(wizardBusy && !loginBusy)',
            'busy={modelFetchState === "loading"}',
            'busy={fetchModelsBusy} disabled={(busy && !fetchModelsBusy)',
            'busy={probing}',
            'busy={saving} disabled={(busy && !saving)',
            'busy={designateBusy}',
            'busy={serviceBusy} disabled={snapshot === undefined || (busy && !serviceBusy)}',
            'busy={cooldownClearPending}',
            'busy={pendingAction === "import"}',
            'busy={pendingAction === "export"}',
            'busy={probeBusy} disabled={busy && !probeBusy}',
            'busy={pendingAction === "sync"}',
        ):
            self.assert_ui_has(marker)
        for marker in (
            'busy={pendingAction === "cleanup"}',
            'busy={pendingAction === "add"}',
            'busy={pendingAction === "login"}',
            'busy={pendingAction === "remove"}',
        ):
            self.assertIn(marker, self.relay)
        # A busy button stays pressable, so each converted action carries its
        # own re-entry guard instead of relying on `disabled`.
        for marker in (
            "if (loginBusy) return;",
            "if (processing) return;",
            "if (modelFetchState === \"loading\") return;",
            "if (fetchModelsBusy) return;",
            "if (cooldownClearPending) return;",
            "if (relayAddBusy) return;",
            "if (authPending !== undefined) return;",
        ):
            self.assert_ui_has(marker)
        # No loading button renames itself any more.
        for marker in (
            'title={processing ? translate("providers.wizard.creating")',
            'title={serviceBusy ? translate("service.starting")',
            'title={loginBusy ? translate("relay.stepSignIn")',
        ):
            self.assert_ui_not_has(marker)

    def test_provider_selection_and_new_models_keep_independent_stable_state(self) -> None:
        self.assert_ui_has('onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }}')
        self.assert_ui_has('const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);')
        self.assertNotIn('knownModelIdsByProvider', self.ui)
        self.assertNotIn('Promise.all(added.map(', self.ui)
        self.assert_ui_has('disabled={(busy && !probing) || !probeReady}')

    def test_new_models_use_zero_order_without_coercing_zero_to_one(self) -> None:
        # A model added from the pane is committed by the shell right away, so
        # it starts under the localized placeholder name (kept unique with a
        # numeric suffix) instead of an empty name that would block the commit
        # with a validation error nobody can act on.
        self.assert_ui_has('const base = translate("providers.newModel");')
        self.assert_ui_has('const name = uniquePlaceholderName(models.map((item) => stringValue(item.model_name ?? item.name)), base);')
        self.assert_ui_has('model: { name, upstream_model: name, enabled: true, order: 0 }')
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
            "providerModelColumns:",
            "providerListPane:",
            "modelListPane:",
            "providerInspector:",
            "tablePane:",
            "tableTitleRow:",
        ):
            self.assert_ui_has(marker)
        self.assert_ui_has('providerInspector: { width: 290, minWidth: 290, maxWidth: 290')
        self.assertNotIn("<ScrollView contentContainerStyle={styles.providerEditorScroll}><ProviderEditor", self.ui)
        self.assert_ui_has("providerEditorContent: { flex: 1, minHeight: 0 }")
        # The inspector pane's header row is the workspace's first row, so it
        # must not carry a top inset: 供应商: <name> lines up with the left
        # column's toolbar row (the 添加向导… button) instead of sitting below it.
        self.assert_ui_has("providerEditorScrollContent: { paddingLeft: 0, paddingRight: 16, paddingBottom: 12, gap: 6 }")
        self.assertNotIn("providerEditorScrollContent: { paddingTop", self.ui)
        self.assert_ui_has('persistentScrollIndicator: { position: "absolute", width: 0, height: 0 }')
        self.assert_ui_has('return <PersistentScrollView style={styles.providerEditorContent} contentContainerStyle={styles.providerEditorScrollContent} showsVerticalScrollIndicator nestedScrollEnabled>')
        self.assert_ui_has('{kind === "apiKey" || (kind === "relay" && !station) ? <ProviderSourceFields')
        self.assert_ui_has("<NativePersistentScrollIndicator style={styles.persistentScrollIndicator} />")
        self.assert_ui_has('showsHorizontalScrollIndicator={false}\n    onLayout=')
        self.assert_ui_has('<PersistentScrollView style={styles.providerWizardModelScroll} contentContainerStyle={styles.providerWizardModelScrollContent} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">')
        self.assert_ui_has('providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('inspectorContent: { paddingLeft: 0, paddingRight: 6')
        self.assertNotIn("inspectorContent: { paddingTop", self.ui)
        self.assert_ui_has("providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }")
        self.assert_ui_has("providerListPane: { width: 140, minWidth: 140, maxWidth: 140")
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 132 }]}')
        # The upstream-model, public-model, and order columns keep their combined
        # 280 pt: the scroller floats over the list (no gutter), the order column
        # fits its header and values, and a narrower pane still squeezes only the
        # trailing column.
        self.assert_ui_has('columns={[{ label: translate("providers.upstream"), width: 120 }, { label: translate("providers.publicModel"), width: 100 }, { label: translate("common.order"), width: 60 }]}')
        self.assert_ui_has('rows.push({ key: `key:${keyName}`, cells: [keyName], spanning: true });')
        self.assert_ui_has('rows.push({ key: editorIdentifier(item), cells: [`\\t${modelUpstreamDisplay(providerId, item)}`, modelDisplayName(providerId, item), modelOrderText(providerId, item)] });')
        self.assert_ui_has('columns={variant === "inline"')
        self.assert_ui_has('cellHorizontalPadding={6}')
        self.assert_ui_has('firstColumnHorizontalPadding={0}')
        self.assert_ui_has('label={translate("providers.keyName")}')
        self.assert_ui_has('NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}')
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
        self.assert_ui_has('rows={providerRows} disabledRowKeys={disabledProviderKeys} alertRowKeys={alertProviderKeys} selectedKey={providerId} compact firstColumnHorizontalPadding={0} onSelectionChange=')
        self.assert_ui_has('rows={modelRows} disabledRowKeys={disabledModelKeys} alertRowKeys={alertModelKeys} selectedKey={selectedModel ?? ""} compact firstColumnHorizontalPadding={0} onSelectionChange=')
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
            # A cleared click on the tab rail keeps the active tab: the pane
            # always shows one content surface.
            'onSelectionChange={(key) => { if (key) switchDataManagementTab(key as DataManagementTab); }}',
            'const switchDataManagementTab = (next: DataManagementTab): void => {',
            'const previous = tab;',
            'const pending = onFlushPendingFields();',
            'setTab(next);',
            'void pending.catch((reason: unknown) => {',
            'onTabSwitchError(previous, reason);',
            # The detail column is the one shared rail detail: its header band
            # names the active tab, so the pane body opens on its content
            # instead of repeating the tab name as a heading.
            '<View style={styles.settingsRailDetail}>',
            '<SettingsDetailHeader title={dataManagementTabRows.find((row) => row.key === tab)?.cells[0] ?? ""} />',
            '{tab === "import" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>',
            '{tab === "export" ? <View style={styles.dataManagementPane}>',
            '{tab === "webdav" ? <View style={[styles.dataManagementWebDavPane, styles.dataManagementWebDavContent, dataManagementPolishStyles.webDavContent]}>',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('<ScrollView style={styles.dataManagementWebDavPane}', workspace)
        self.assertNotIn("dataManagementPolishStyles.paneHeading", self.ui)
        self.assertNotIn("paneIntro", self.ui)
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
        # Every pane body starts from the one settings inset and every field
        # row from the one label column, so the three panes line up.
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: SETTINGS_PANE_INSET, gap: 14 }')
        self.assertNotIn('importLandingContent', self.ui)
        self.assert_ui_has('importIntro: { minHeight: 0, paddingHorizontal: 12, paddingVertical: 0, gap: 10 }')
        self.assert_ui_has('paneHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }')
        self.assert_ui_has('compactText: { fontSize: UI_FONT_SIZE, lineHeight: 16 }')
        self.assert_ui_has('dataManagementGroup: { gap: 6 }')
        self.assert_ui_has('dataManagementSectionPicker: { flexDirection: "row", flexWrap: "wrap"')
        self.assert_ui_has('const SETTINGS_FIELD_ROW_INDENTED: ViewStyle = { ...SETTINGS_FIELD_ROW, paddingLeft: SETTINGS_FIELD_LEAD };')
        # The runtime row carries the lead as padding like the others; its
        # marker bar is positioned in that lead instead of pushing the label.
        self.assert_ui_has('runtimeInputRow: { ...SETTINGS_FIELD_ROW_INDENTED }')
        self.assert_ui_has('runtimeModifiedBar: { position: "absolute", left: 0, top: 3, bottom: 3, width: 2, borderRadius: 1, backgroundColor: "transparent" }')
        self.assert_ui_has('externalSettingsInputRow: { ...SETTINGS_FIELD_ROW_INDENTED }')
        # The General pane is a settings pane too, so its labelled rows spread
        # the same grid instead of restating the label width, the tip indent,
        # and a pane inset of its own.
        general = self.ui.split("function GeneralWorkspace(", 1)[1].split("function RuntimeWorkspace(", 1)[0]
        for marker in (
            "generalRow: { ...SETTINGS_FIELD_ROW_INDENTED }",
            "generalRowLabel: { ...SETTINGS_FIELD_LABEL }",
            "generalToggle: { ...SETTINGS_FIELD_SWITCH_SLOT }",
            "generalHelpSlot: { ...SETTINGS_FIELD_HELP_SLOT }",
            "generalHelpText: { ...SETTINGS_FIELD_HELP_TEXT }",
        ):
            self.assert_ui_has(marker)
        self.assertIn('generalContent: { paddingTop: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET', self.ui)
        self.assertIn("<View style={styles.generalHelpSlot}>", general)
        for restated in ("generalRowLabel: { width: 128", "generalRowHint:", "generalToggle: { width: 44", 'textAlign: "right"'):
            self.assertNotIn(restated, general)
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
        self.assertIn('<PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>', export_pane)
        self.assertIn('<View style={[styles.dataManagementBottomActions, dataManagementPolishStyles.bottomActions]}>', export_pane)
        self.assertGreater(
            export_pane.index('<View style={[styles.dataManagementBottomActions, dataManagementPolishStyles.bottomActions]}>'),
            export_pane.index('</PersistentScrollView>'),
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
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: SETTINGS_PANE_INSET, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: SETTINGS_PANE_INSET, gap: 14 }')
        self.assert_ui_has('compactText: { fontSize: UI_FONT_SIZE, lineHeight: 16 }')

    def test_data_management_uses_native_preference_groups_not_web_cards(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            "webdavSyncArea:",
            "dataManagementToolbarButtons:",
            'const labelAlign = "left";',
            'dataManagementSyncScope: { ...SETTINGS_FIELD_ROW_INDENTED, minHeight: 24 }',
            'dataManagementSyncScopeLabel: { ...SETTINGS_FIELD_LABEL }',
            'dataManagementDirection: { ...SETTINGS_FIELD_ROW_INDENTED }',
            'dataManagementDirectionLabel: { ...SETTINGS_FIELD_LABEL }',
            'dataManagementImportFileRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: SETTINGS_FIELD_GAP, paddingLeft: SETTINGS_FIELD_LEAD }',
            'dataManagementImportFileLabel: { ...SETTINGS_FIELD_LABEL }',
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
        self.assertIn('dataManagementImportFileRow: { width: "100%", minHeight: 28, flexDirection: "row", alignItems: "center", gap: SETTINGS_FIELD_GAP, paddingLeft: SETTINGS_FIELD_LEAD }', self.ui)
        self.assertIn('dataManagementImportFileValue: { flex: 1, minWidth: 0, minHeight: 26, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: systemColors.separator, borderRadius: 4, backgroundColor: systemColors.textBackground }', self.ui)
        self.assertIn('<NativePicker labels={syncOptions.map(({ title }) => title)} selectedValue={selectedSyncLabel}', workspace)
        self.assertIn('dataManagementDirectionPicker: { width: SETTINGS_FIELD_ROUTE_WIDTH, height: 24', self.ui)
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
        self.assertNotIn('title={translate("status.close")}', workspace)
        self.assertNotIn('onClose:', workspace)
        # Immediate apply: neither the imported sections nor WebDAV keep an
        # in-pane Apply button.
        self.assertNotIn('title={translate("status.apply")}', workspace)
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
        self.assertIn('settingsSidebarTitle: { color: systemColors.label, fontSize: SOURCE_LIST_FONT_SIZE, fontWeight: "600" }', self.ui)
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
            self.assertTrue((ROOT / "rn/apps/macos/macos/YoungRouter-macOS/Assets.xcassets" / f"{image}.imageset" / "Contents.json").exists(), image)
            self.assertTrue((ROOT / "rn/apps/windows/windows/YoungRouter/Assets/Sidebar" / f"{image}.png").exists(), image)
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
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE", "SOURCE_LIST_FONT_SIZE", "10", "15"},
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
        self.assertIn("runtimeHelpText: { ...SETTINGS_FIELD_HELP_TEXT }", self.ui)
        self.assertIn("fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)

        self.assertIn("constexpr CGFloat LiteLLMUIFontSize = 13.0;", macos_controls)
        # The sidebar source list reads a half step above body text on both
        # hosts, and the React sidebar header keeps the same size.
        self.assertIn("export const SOURCE_LIST_FONT_SIZE = 13.5;", typography)
        self.assertIn("constexpr CGFloat LiteLLMSourceListFontSize = 13.5;", macos_controls)
        self.assertIn("constexpr double kSourceListFontSize = 13.5;", windows_controls)
        self.assertNotIn("systemFontSizeForControlSize", macos_controls)
        self.assertNotRegex(macos_controls, r"(?:systemFontOfSize|monospacedSystemFontOfSize):\d")
        self.assertIn("column.headerCell.attributedStringValue = TableHeaderTitle(columnTitle);", macos_controls)
        self.assertIn("NSFontAttributeName: [NSFont systemFontOfSize:LiteLLMUIFontSize weight:NSFontWeightMedium]", macos_controls)

        self.assertIn("private let nativeUIFontSize: CGFloat = 13", self.macos_leaf)
        self.assertIn("private final class NativeReadOnlyCodeController", self.macos_leaf)

        for source in (windows_controls, windows_leaf, windows_relay):
            self.assertIn("constexpr double kUIFontSize = 13.0;", source)
            # Every FontSize() call names one of the type steps: the rail's own
            # ternary picks the source-list step, never a literal.
            self.assertNotRegex(
                source,
                r"FontSize\((?!rail_row \? kSourceListFontSize : kUIFontSize\)|kUIFontSize\)|kSourceListFontSize\)|kSourceListGlyphFontSize\))",
            )

    def test_logs_default_to_requests_and_show_latest_first(self) -> None:
        self.assertNotIn("logInfoBar", self.ui)
        self.assertIn('onStatus={setResult} requestedTab=', self.ui)
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
        self.assert_ui_has('payload: { tab: "actions", menu_action: action }')
        self.assert_ui_has('runServiceOperation(serviceOperation).then(() => recordMenuAction(action))')

    def test_data_management_copy_names_unified_sections_and_actions(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for text in (english, chinese):
            for key in (
                "status.dataManagement",
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
            ):
                self.assertIn(f'"{key}":', text)
        for value in (
            '"status.dataManagement": "Backup & Sync"',
            '"dataManagement.tab.import": "Import"',
            '"dataManagement.tab.export": "Export"',
            '"dataManagement.tab.webdav": "WebDAV Sync"',
            '"dataManagement.section.providersModels": "Providers & Models"',
            '"dataManagement.section.relayAccounts": "Provider accounts"',
            '"dataManagement.importHint": "Choose a file to detect its importable configuration automatically."',
            '"dataManagement.importRecognizedHint": "These configuration areas were detected in the selected file and are selected by default."',
        ):
            self.assertIn(value, english)
        for value in (
            '"status.dataManagement": "备份与同步"',
            '"dataManagement.tab.import": "导入"',
            '"dataManagement.tab.export": "导出"',
            '"dataManagement.tab.webdav": "WebDAV 同步"',
            '"dataManagement.section.providersModels": "供应商与模型"',
            '"dataManagement.section.relayAccounts": "供应商账号"',
            '"dataManagement.importHint": "选择文件后会自动识别可导入的配置项。"',
            '"dataManagement.importRecognizedHint": "以下为文件中识别到的配置项，默认全选。"',
        ):
            self.assertIn(value, chinese)

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
        # Five data tables, the settings shell's pane source list, and the one
        # subordinate rail every split pane renders from the shared component.
        self.assertEqual(self.ui.count("<NativeTable"), 7)
        self.assertEqual(self.ui.count("alternatingRows"), 0)
        self.assertNotIn("selectedKey={selectedRoute ?? \"\"} alternatingRows", self.ui)
        self.assertEqual(self.ui.count("striped={false}"), 2)
        shell = self.ui.split("function SettingsShell(", 1)[1].split("function RouteSurface(", 1)[0]
        self.assertIn("striped={false}", shell)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", mac_native)
        self.assertIn("_tableView.style = NSTableViewStylePlain;", mac_native)
        self.assertIn("_tableView.style = sourceList ? NSTableViewStyleSourceList : NSTableViewStylePlain;", mac_native)
        self.assertIn("_tableView.selectionHighlightStyle = sourceList", mac_native)
        self.assertIn("NSTableViewSelectionHighlightStyleSourceList", mac_native)
        self.assertIn("_tableView.headerView = nil;", mac_native)
        self.assertIn("_scrollView.drawsBackground = NO;", mac_native)
        self.assertNotIn("_scrollView.drawsBackground = !sourceList;", mac_native)
        # The shell and the runtime rail coordinate their selection through the
        # JS selectedKey props, so the source-list chrome must keep empty
        # per-table selection representable.
        self.assertNotIn("_tableView.allowsEmptySelection = !sourceList;", mac_native)
        self.assertIn("NSImage *tileImage = imageName.length > 0 ? [NSImage imageNamed:imageName] : nil;", mac_native)
        self.assertIn("cell.badge.layer.backgroundColor = NSColor.clearColor.CGColor;", mac_native)
        # A sidebar pane title carries the sidebar's vibrancy ink (the primary
        # label color resolved for the vibrant sidebar appearance) so it reads
        # the same tone as the shell's app title; a raw catalog label color is
        # painted opaque black here, and only the selection swaps the ink for
        # the emphasized title color.
        source_list_cell = mac_native.split("@implementation LiteLLMSourceListCellView", 1)[1].split("@end", 1)[0]
        self.assertIn("NSColor *SourceListTitleColor(NSAppearance *appearance)", mac_native)
        self.assertIn("NSAppearanceNameVibrantLight", mac_native)
        self.assertIn("resolved = [NSColor.labelColor colorUsingColorSpace:NSColorSpace.sRGBColorSpace];", mac_native)
        self.assertIn(
            "self.label.textColor = emphasized ? NSColor.alternateSelectedControlTextColor : SourceListTitleColor(self.effectiveAppearance);",
            source_list_cell,
        )
        self.assertNotIn("label.textColor = NSColor.secondaryLabelColor;", source_list_cell)
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
        # A leading tab steps the row hierarchy in: one narrow stop, not
        # AppKit's 28 pt default that ate the first column's own text room.
        self.assertIn("constexpr CGFloat LiteLLMTableRowIndentWidth = 16.0;", mac_native)
        self.assertIn("paragraph.defaultTabInterval = LiteLLMTableRowIndentWidth;", mac_native)
        self.assertIn("location:LiteLLMTableRowIndentWidth", mac_native)
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

    def test_subordinate_rails_share_one_source_list_style(self) -> None:
        """Backup & sync, runtime settings, and external clients render one rail.

        The three panes that split their body into a list plus a detail used to
        carry three hand-built variants of the same source list (140 pt versus
        156 pt wide, 132 pt versus 148 pt columns), and their symbol-less rows
        fell through to the ordinary body cell: 13 pt regular ink in a row the
        sidebar one level up had already left behind. One shared rail keeps the
        second level on the sidebar's own source-list chrome, one density step
        below it — the way the platform's own in-pane rows sit under its sidebar
        rows (macOS measures 32 pt for a sidebar row and about 28 pt inside a
        pane).
        """
        mac_native = MACOS_CONTROLS.read_text(encoding="utf-8")
        windows_native = WINDOWS_CONTROLS.read_text(encoding="utf-8")
        self.assertIn("const SETTINGS_RAIL_WIDTH = 156;", self.ui)
        self.assertIn("const SETTINGS_RAIL_COLUMN_WIDTH = 148;", self.ui)
        self.assertIn(
            "settingsRail: { width: SETTINGS_RAIL_WIDTH, flexShrink: 0, minHeight: 0, paddingTop: 6, borderRightWidth: 1, borderRightColor: systemColors.separator }",
            self.ui,
        )
        self.assertIn("settingsRailList: { flex: 1, minHeight: 0 },", self.ui)
        # One rail: the sidebar's own source-list chrome, no header, no
        # striping, and one column a translated client name still fits in.
        rail = self.ui.split("function SettingsRail(", 1)[1].split("\nfunction ", 1)[0]
        for marker in (
            'columns={[{ label: "", width: SETTINGS_RAIL_COLUMN_WIDTH }]}',
            "sourceList",
            "cellHorizontalPadding={8}",
            "firstColumnHorizontalPadding={8}",
            "onSelectionChange={onSelectionChange}",
        ):
            self.assertIn(marker, rail)
        # The rail is the *compact* source list, and the shell's own sidebar is
        # the regular one: that one density step is what separates the two levels
        # of the same control.
        self.assertIn("compact", rail)
        shell = self.ui.split("function SettingsShell(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("compact={false}", shell)
        self.assertIn("sourceList", shell)
        self.assertEqual(self.ui.count("<SettingsRail"), 3)
        # Every split pane renders it; none of them keeps a rail of its own.
        for pane_start in (
            "function AssistantSettingsWorkspace(",
            "function RuntimeWorkspace(",
            "function DataManagementWorkspace(",
        ):
            self.assertIn("<SettingsRail", self.ui.split(pane_start, 1)[1].split("\nfunction ", 1)[0])
        for removed_rail in ("externalSettingsRail", "dataManagementRail", "runtimeToc:", "runtimeTocList"):
            self.assertNotIn(removed_rail, self.ui)
        # A source-list row without an icon tile is that same list one level
        # down, so each host gives it the sidebar's own type step and ink
        # instead of the ordinary body cell.
        rail_row = mac_native.split("const bool railRow = viewProps.sourceList;", 1)[1].split("label.toolTip = value;", 1)[0]
        self.assertIn('identifier = railRow ? @"LiteLLMAppKitSourceListTextCell" : @"LiteLLMAppKitTableCell";', rail_row)
        self.assertIn("Class cellClass = railRow ? [LiteLLMSourceListTextCellView class] : [NSTableCellView class];", rail_row)
        self.assertIn("label.stringValue = value;", rail_row)
        self.assertIn("label.font = SourceListFont();", rail_row)
        self.assertIn("label.textColor = SourceListTitleColor(self.effectiveAppearance);", rail_row)
        rail_cell = mac_native.split("@implementation LiteLLMSourceListTextCellView", 1)[1].split("@end", 1)[0]
        self.assertIn("self.textField.textColor = emphasized", rail_cell)
        self.assertIn("? NSColor.alternateSelectedControlTextColor", rail_cell)
        self.assertIn(": SourceListTitleColor(self.effectiveAppearance);", rail_cell)
        self.assertIn("cell.FontSize(rail_row ? kSourceListFontSize : kUIFontSize);", windows_native)
        self.assertIn("if (rail_row) cell.FontWeight(winrt::Windows::UI::Text::FontWeights::Medium());", windows_native)

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
        # The sidebar keeps the native source-list rhythm; the pane's own rail is
        # a compact source list one density step below it (the platform's own
        # in-pane rows sit under its sidebar rows).
        self.assertIn(
            "_tableView.rowHeight = newViewProps.sourceList ? (newViewProps.compact ? 26 : 30) : (newViewProps.compact ? 22 : 28);",
            re.sub(r"\s+", " ", mac_native),
        )
        self.assertIn(
            "row.MinHeight(source_list ? (compact_rows ? 26.0 : 30.0) : (compact_rows ? 22.0 : 28.0));",
            re.sub(r"\s+", " ", windows_native),
        )
        self.assertIn("const bool compact_rows = props.compact.value_or(false);", windows_native)
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
        # Short columns keep the requested width and ellipsize longer values;
        # only the trailing detail column grows to its measured content and the
        # table scrolls horizontally for it.
        self.assertNotIn("laidOutColumnWidths[index] = MAX(laidOutColumnWidths[index], _measuredColumnWidths[index]);", mac_native)
        self.assertIn("LiteLLMTableMeasuredRowLimit", mac_native)
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
        self.assertNotIn('route === "runtime-settings" ? translate("common.saveAndApply") : translate("status.apply")', self.ui)
        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('<NativeButton title={translate("status.close")} disabled={processing} onPress={onClose} />', wizard)

    def test_runtime_save_and_apply_reloads_the_running_proxy(self) -> None:
        # Both providers_models and runtime applies leave the seconds-long
        # proxy restart to Core's background reload instead of blocking the
        # pane on a synchronous lifecycle dispatch.
        self.assert_ui_has("// Core restarts the managed proxy for providers_models and runtime")
        self.assert_ui_not_has('await ipc.dispatch({ type: "service.reload" }, result.revision)')
        service = (ROOT / "young_router/core/service.py").read_text(encoding="utf-8")
        self.assertIn('if "providers_models" in applied or "runtime" in applied:', service)
        self.assertIn("self._schedule_service_reload_after_apply()", service)
        self.assertIn('name="litellm-core-service-reload"', service)

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
        # The compact AppKit switch is 44 x 20 pt, so the slot that centres it
        # has to reserve that width or the control overflows the row.
        self.assertIn('runtimeBooleanControl: { ...SETTINGS_FIELD_SWITCH_SLOT }', self.ui)
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
            'minWidth: (props.busy === undefined ? reservation : nativeButtonBusyMinimumWidth(props.title, reservation, props.plainLink === true)) + (props.symbol ? 22 : 0),',
            self.native_controls,
        )
        # A busy button keeps its title, width, and enabled color and shows a
        # leading spinner in the slack the reservation already leaves, so a
        # caller that hugs its label gets the slot added to its own width.
        self.assertIn("const BUSY_SPINNER_SIZE = 12;", self.native_controls)
        self.assertIn("return Math.max(reservation, Math.ceil(nativeControlTextWidth(title)) + BUSY_SPINNER_SLOT);", self.native_controls)
        self.assertIn("busy: props.busy === true,", self.native_controls)
        # The bezel-hugging width is opt-in for a caller that owns its own short
        # label, and the request never reaches the native component props.
        self.assertIn('titleWidth?: "auto" | "tight" | "flex";', self.native_controls)
        self.assertIn(
            "function NativeButtonWithRef({ titleWidth: titleWidthRequest, ...props }, ref)",
            self.native_controls,
        )
        self.assertIn(
            "const style = [props.link ? styles.linkButton : styles.button, props.style, titleWidth];",
            self.native_controls,
        )
        # ``flex`` hands the width to the caller's container: without it a long
        # title (a file path) reserves its full text width and widens the row.
        self.assertIn(
            'titleWidthRequest === "flex"',
            self.native_controls,
        )
        self.assertIn(
            "{ minWidth: 0, flexShrink: 1 }",
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
            "AssistantSettingsWorkspace": ("externalSettingsWorkspace:", "externalSettingsPane:"),
            "RuntimeWorkspace": ("runtimeWorkspace:", "runtimeScrollSurface:"),
            "DataManagementWorkspace": ("dataManagementWorkspace:", "dataManagementPane:"),
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

    def test_every_settings_pane_scrolls_through_the_shared_scroll_surface(self) -> None:
        # One scrolling surface per pane: the native indicator gives it the app's
        # persistent translucent scroller instead of AppKit's overlay bar, so a
        # pane never mixes scrollbar appearances with the tables beside it.
        for marker in (
            '<PersistentScrollView style={styles.generalScroll}',
            '<PersistentScrollView key={selectedCategory} style={styles.runtimeScrollSurface}',
            '<PersistentScrollView style={styles.externalSettingsPane}',
            '<PersistentScrollView style={styles.dataManagementPane}',
            '<PersistentScrollView\n            ref={timelineScrollRef}',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("<ScrollView style={styles.", self.ui)

    def test_assistant_settings_have_one_outer_scroll_surface_without_tabs(self) -> None:
        for marker in (
            '<PersistentScrollView style={styles.externalSettingsPane}',
            "externalSettingsPaneContent",
            "<SettingsDetailHeader",
            "<SettingsRail",
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("settingsTabBar", self.ui)
        self.assertNotIn("settingsTabs", self.ui)
        self.assertNotIn('<WindowTabs values={[{ id: "codex", title: "Codex" }, { id: "claude", title: "Claude" }]}', self.ui)

    def test_assistant_plaintext_credentials_have_no_set_or_clear_buttons(self) -> None:
        # The external settings pane edits files only; the remaining plaintext
        # credential fields (providers) still render one inline control with no
        # separate set/clear actions of their own.
        self.assertIn('<NativeSecretField labelVisible={false} plainText autoCommit', self.ui)
        for marker in (
            '<NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}',
            '<NativeSecretField\n          labelVisible={false}',
            '<NativeSecretField autoCommit label={translate("providers.authTypeClaude")}',
        ):
            self.assertIn(marker, self.ui)
        self.assertNotIn("clearTitle=", self.ui)
        self.assertNotIn("onClear=", self.ui)

    def test_client_actions_follow_the_selected_provider_row(self) -> None:
        # Codex names its gateway in ``model_provider``.  Both client actions
        # rewrite that row in place instead of inventing a provider named after
        # the route, and the pane resolves the route behind the proxy by the
        # public model name the proxy serves.
        core = (ROOT / "young_router/core/domains/codex.py").read_text(encoding="utf-8")
        self.assertIn("def _active_provider_patch(", core)
        self.assertIn('entry["base_url"] = base_url', core)
        self.assertIn('return {"api_key": key, "direct_connection": {"provider": "openai", "base_url": base_url}}', core)
        self.assertIn('patch["model"] = _direct_model_id(str(row.get("upstream_model") or model))', core)
        self.assertNotIn('"model_provider": provider,', core)
        self.assertIn('if (publicModel && publicModel === model) return true;', self.ui)
        self.assertIn("codexModels.find((row) => clientProvider !== \"\" && stringValue(row.provider).trim() === clientProvider && matches(row))", self.ui)

    def test_external_settings_list_only_registered_client_files_with_paths(self) -> None:
        pane = self.ui.split("function AssistantSettingsWorkspace", 1)[1].split("function GeneralWorkspace", 1)[0]
        self.assertIn(
            'const CLIENT_FILE_GROUPS: ReadonlyArray<{ client: ClientFile["client"]; titleKey: string; hintKey: string }> = [',
            self.ui,
        )
        for marker in (
            'accessibilityLabel={translate("settings.useLocalApi")}',
            'translate("settings.editFile")',
            # The designate control points the client straight at one saved
            # provider route and names the provider and model it points at;
            # its chevron rides the title's trailing edge.
            'title={designatedRoute}',
            # The control names the API the client uses and stays empty on
            # this app's proxy, where no specific API is selected.
            'const designatedRoute = localApiActive ? "" : clientRouteLabel;',
            'symbol="chevron-up-down"',
            "symbolTrailing",
            "void onUseSavedModel(chosen).finally(() => setDesignateBusy(false));",
            'void showGroupedActionMenu({ title: translate("settings.designateAs"), groups, anchor: { x, y, width, height } })',
            "{designateAction}",
            "void ipc.files()",
            ".then((result) => { if (active) setFiles(result.files); })",
            "{file.name}",
            "{file.display_path}",
            'translate("clients.fileMissing")',
            "onOpenFile(file)",
            "disabled={busy || !file.exists}",
            'toolTip={file.exists ? undefined : translate("clients.fileMissing")}',
            "assistantFileSurfaceStyles.fileRow",
            "assistantFileSurfaceStyles.filePathLink",
        ):
            self.assertIn(marker, pane)
        # The detail body draws no rule of its own: the switches and the file
        # rows are separated by the pane's row rhythm, so the only line in the
        # rightmost column is the shared header band's own divider. A rule per
        # switch and per file (plus a trailing one) turned the pane into a
        # stack of separators.
        self.assertIn('externalSettingsField: { ...SETTINGS_FIELD },', self.ui)
        self.assertIn('externalSettingsFieldList: { ...SETTINGS_FIELD_LIST },', self.ui)
        # The client files below the fields start on the fields' own label x.
        self.assertIn('fileRow: { minHeight: 36, flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 4, paddingLeft: SETTINGS_FIELD_LEAD },', self.ui)
        self.assertNotIn("borderBottomWidth", pane)
        self.assertNotIn("borderColor", pane)
        # Codex is the one client with a client-level action: adopt this app's
        # proxy as that client's backend.
        # The editor is a route the host presents as its own sub-sheet, not an
        # in-window React overlay.
        for marker in (
            "function FileEditorWorkspace(",
            "route === \"file-editor\"",
            "fileIdRequest={fileIdRequest}",
            "native.openFileEditor(JSON.stringify(editorTargetPayload(file, true)))",
            "native.prepareFileEditor?.();",
            "editorTargetFromPayload(native.pendingFileEditorTarget?.())",
            'native.window.open("codex-settings")',
            "setAssistantEditorOpen(requested);",
        ):
            self.assertIn(marker, self.ui)
        self.assertNotIn("AssistantFileEditorDialog", self.ui)
        self.assertNotIn("activeAssistantFile", self.ui)
        # Codex keeps its own model-list switch: the pane writes the same
        # managed catalog the status menu toggles, so checked replaces Codex's
        # model list with this app's public one and unchecked restores the
        # client's built-in list.
        for marker in (
            'accessibilityLabel={translate("codex.modelCatalog")}',
            'translate("codex.modelCatalogHint")',
            "const toggleCatalog = (enabled: boolean): void => {",
            "void onToggleCodexModelCatalog(enabled).finally(() => setCatalogBusy(false));",
            "disabled={(busy && !catalogBusy) || catalogBusy}",
            "styles.externalSettingsFieldLabel",
            "styles.externalSettingsValueSlot",
            "styles.externalSettingsHelpSlot",
            "styles.externalSettingsBooleanControl",
            'codexModelCatalogEnabled={codexModelCatalogEnabled}',
            'booleanValue(codexModelCatalogState(snapshot).enabled)',
            'onToggleCodexModelCatalog={(enabled) => dispatch("codex.model_catalog.set", { enabled }, "codex")}',
            'const CODEX_MODEL_CATALOG_FILE_ID = "codex_model_catalog";',
            "const openClientFile = (file: ClientFile): void => {",
            "if (file.id !== CODEX_MODEL_CATALOG_FILE_ID || !codexModelCatalogEnabled) {",
            'title: translate("codex.modelCatalogEditTitle")',
            'message: translate("codex.modelCatalogEditBody")',
            'confirmLabel: translate("codex.modelCatalogEditConfirm")',
            ".then((confirmed) => { if (confirmed) onOpenFile(file); });",
            "onPress={() => openClientFile(file)}",
        ):
            self.assertIn(marker, self.ui if marker not in pane else pane)
        for marker in (
            # The client backend is fields of the pane's own settings grid,
            # never a header control beside the detail title: the switch and
            # the control that names the designated provider and model share
            # one label column and one control column.
            '{selectedClient === "codex" ? <View style={styles.externalSettingsFieldList}>',
            "styles.externalSettingsInputRow",
            "styles.externalSettingsFieldLabel",
            "styles.externalSettingsValueSlot",
            "styles.externalSettingsHelpSlot",
            # One on/off setting is a switch, never a checkbox.
            "<NativeToggle",
            "disabled={(busy && !catalogBusy) || catalogBusy}",
            'void onUseLocalApi(offRoute ?? {}).finally(() => setClientActionBusy(false));',
            # On the proxy the model is only a public name, so the route the
            # user last chose decides where "off" goes instead of the first
            # matching row.
            "const offRoute = localApiActive ? lastRoute ?? clientRoute : clientRoute;",
            "const [lastRoute, setLastRoute] = useState<ClientRoute | undefined>(undefined);",
            "if (!offRoute) { settleClientSwitch(); return; }",
            "designateSavedModel(offRoute);",
            "value={localApiActive}",
            'accessibilityLabel={translate("settings.useLocalApi")}',
            'translate("settings.useLocalApiHint")',
            # The switch is a real two-way switch driven by the config: on
            # adopts this app's proxy, off returns the client to the saved
            # route the control names (resolved from the client's own provider
            # and model, so unchecking opens no menu).  A check that cannot
            # name a saved route changes nothing and re-mounts the box onto the
            # configured value.
            "disabled={(busy && !(clientActionBusy || designateBusy)) || clientActionBusy || designateBusy}",
            "onValueChange={toggleLocalApi}",
            "const toggleLocalApi = (enabled: boolean): void => {",
            "const [clientSwitchGeneration, setClientSwitchGeneration] = useState(0);",
            "const settleClientSwitch = (): void => setClientSwitchGeneration((current) => current + 1);",
            "const clientRoute = useMemo(() => {",
            "{designateAction}",
            # The control names the saved route's public provider and model
            # (the spelling the menu itself offers) and truncates inside its
            # own column instead of widening the row.
            'titleWidth="flex"',
            'const designatedRoute = localApiActive ? "" : clientRouteLabel;',
            'clientProvider={codexClientProvider}',
            'clientModel={codexClientModel}',
            'localApiActive={codexUsesLocalApi}',
            "const codexClientProvider = stringValue(codexStructured.model_provider).trim();",
            'onUseLocalApi={(selection) => dispatch("use_local_api", selection, "codex")}',
            'const codexUsesLocalApi = booleanValue(codexState.uses_local_api);',
        ):
            self.assertIn(marker, self.ui if marker not in pane else pane)
        # The path itself is the reveal control: a native link button whose
        # long title is bounded by its own column instead of the button's
        # reserved label width.  It shows the home-shortened spelling of the
        # file while the action still addresses the real path.
        for marker in (
            'titleWidth="flex"',
            'title={file.display_path}',
            'onPress={() => native.revealFile?.(file.path)}',
            'accessibilityLabel={`${revealFileLabel}: ${file.display_path}`}',
            'const revealFileLabel = translate(Platform.OS === "windows" ? "settings.revealInExplorer" : "settings.revealInFinder");',
            'filePathLink: { alignSelf: "stretch", minWidth: 0, flexGrow: 0, flexShrink: 1 },',
        ):
            self.assertIn(marker, self.ui)
        # Every level keeps the app's UI font and the standard sizes: the
        # heading is a section heading on its own band, and the file rows stay
        # ordinary 13pt list text.
        self.assertIn(
            'settingsRailDetailTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: "600" },',
            self.ui,
        )
        self.assertIn(
            "fileLabel: { color: systemColors.label, fontSize: UI_FONT_SIZE },",
            self.ui,
        )
        # The Core listing is the only path source, and the pane no longer
        # renders label/input structured editing for any client.
        for removed in ("TextField", "PickerField", "NativeSecretField", "settings.structured", "assistantQuickGrid"):
            self.assertNotIn(removed, pane)
        self.assertIn("externalSettingsWorkspace", self.ui)
        # The pane aligns its rail exactly like the backup & sync pane: the
        # route body contributes no horizontal padding of its own.
        self.assertIn("settingsRoute && styles.assistantSettingsContent", self.ui)
        self.assertIn("assistantSettingsContent: { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 }", self.ui)
        # The pane's rail is the settings window's shared subordinate rail, so it
        # needs no caption, width, or striping of its own.
        self.assertNotIn("externalSettingsRailTitle", self.ui)
        self.assertIn("settingsRailDetailHeader", self.ui)
        self.assertIn('horizontal={false}', self.ui)
        self.assertIn('showsHorizontalScrollIndicator={false}', self.ui)

    def test_external_file_editor_route_owns_save_and_close(self) -> None:
        editor = self.ui.split("function FileEditorWorkspace(", 1)[1].split("function AssistantSettingsWorkspace(", 1)[0]
        for marker in (
            "setAssistantEditorOpen(requested);",
            "return () => setAssistantEditorOpen(false);",
            'native.window.close("file-editor");',
            'native.window.open("codex-settings");',
            "await ipc.applyDomains([...domains], refreshed.revision);",
            'onStatus(translate("common.saving"));',
            'onStatus(translate("common.saved"));',
            'translate("status.close")',
            'translate("status.saveAndClose")',
            "assistantFileSurfaceStyles.editorFooterActions",
            "target?.display_path",
            # Close warns once, then discards the staged draft before the
            # sheet goes away; Save writes the file and closes it.
            "const closeWindow = (): void => {",
            "current.drafts[target.domain]?.dirty !== true",
            "native.showConfirmation(",
            'translate("settings.discardDraftTitle")',
            'translate("settings.discardDraftBody")',
            'translate("common.discard")',
            'type: "cancel"',
        ):
            self.assertIn(marker, editor)
        self.assertNotIn('translate("common.save")', editor)
        self.assertIn("fileEditorRouteContent:", self.ui)
        self.assertIn("menuLabels={editorMenuLabels(translate)}", self.ui)
        self.assertIn(
            'editorFooterActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 10 },',
            self.ui,
        )
        # The footer is the two actions alone: the old hint text is gone and
        # the actions keep the trailing edge of the row.
        self.assertNotIn("editorFooterHint", self.ui)
        self.assertIn('justifyContent: "flex-end", gap: 12', self.ui)
        # Copy and Cut work on the current line with no selection, like every
        # code editor: an unselected Cmd-X must never look dead.
        self.assertIn('aceEditor.setOption("copyWithEmptySelection", true);', self.code_editor_web)

    def test_runtime_uses_core_projection_kinds_and_adaptive_layout(self) -> None:
        for marker in (
            'kind === "toggle"',
            'kind === "choice"',
            'storageKind',
            'const [activeCategory, setActiveCategory] = useState("");',
            'const selectedCategory = categories.includes(activeCategory) ? activeCategory : (categories[0] ?? "");',
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

            'route === "file-editor" ? <FileEditorWorkspace',
            'translate("runtime.fixInvalidBeforeClose")',
            'invalidCloseNotice.current = true;',
            'invalidCloseNotice.current = false;',
            # One rail selection is one surface: the pane renders the selected
            # category alone, keyed by it so a switch starts at its own top.
            'const selectCategory = (name: string): void => {',
            'if (!name) return;',
            'setActiveCategory(name);',
            'key={selectedCategory}',
            'style={styles.runtimeScrollSurface}',
            'onSelectionChange={selectCategory}',
            'const tocRows = useMemo(() => categories.map((name) => ({ key: name, cells: [runtimeCategoryLabel(name, translate)] })), [categories, translate]);',
            # The detail column is the same rail detail external clients render:
            # one shared header band naming the selected category above the
            # keyed surface.
            '<View style={styles.settingsRailDetail}>',
            '<SettingsDetailHeader title={runtimeCategoryLabel(selectedCategory, translate)} />',
            '{(groups[selectedCategory] ?? []).map((item) => <RuntimeField',
            'translate("common.willClear")',
            'clearSecret({ domain: "runtime", field: "setting", target: key })',
            'runtimeCategoryLabel(name, translate)',
            'runtimeFieldLabel(key, stringValue(item.label, key), translate)',
            'runtimeFieldHelp(key, stringValue(item.help), translate)',
            'runtimeUnitLabel(stringValue(item.unit), translate)',
            'runtimeOptionLabel(key, option, translate)',
        ):
            self.assert_ui_has(marker)
        # The pane is a list plus a detail, not a continuous scroll: the rail
        # selects a category and the surface renders only that one, so nothing
        # tracks a scroll position back into the selection.
        runtime_workspace = self.ui.split("function RuntimeWorkspace(", 1)[1].split("\nfunction ", 1)[0]
        for removed in (
            "onScroll={trackScroll}",
            "runtimeTocScroll",
            "pendingJump",
            "sectionOffsets",
            "runtimeSectionTitle",
            "categories.map((name) => <View key={name}",
        ):
            self.assertNotIn(removed, runtime_workspace)
        # The band above the surface is the one shared detail header, so the
        # runtime, external, and backup panes never drift apart.
        self.assertEqual(self.ui.count("<SettingsDetailHeader"), 3)

    def test_dsh_router_keeps_advanced_json_below_native_quick_controls(self) -> None:
        schema = (ROOT / "young_router/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        quick_keys = (
            "YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED",
        )
        advanced = schema.index("YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON")
        for key in quick_keys:
            self.assertLess(schema.index(key), advanced)
        self.assertIn("<NativeCheckbox", self.ui)
        self.assertIn("<NativePicker", self.ui)
        self.assertIn("<RuntimeValueField", self.ui)
        self.assertIn("<NativeSecretInputControl", self.ui)

    def test_runtime_metadata_has_complete_chinese_projection(self) -> None:
        schema = (ROOT / "young_router/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        localized = (ROOT / "rn/packages/shared/src/i18n/runtimeSettingsI18n.ts").read_text(encoding="utf-8")
        keys = re.findall(r"'key': '([^']+)'", schema)
        self.assertEqual(65, len(keys))
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIn(f"  {key}: {{ label:", localized)
        # The Relay and Computer Facade categories disappeared with their only
        # settings; retired dead controls must not stay translated in the pane.
        for category in ("Timeouts", "Recovery", "Web Search", "Vision Router", "Model Context", "Fallback", "MCP", "Logs", "Network", "Service"):
            self.assertIn(f'  "{category}":', localized)
        self.assertNotIn('  "Relay":', localized)
        self.assertNotIn('  "Computer Facade":', localized)
        self.assertNotIn("Vision Bridge", localized)
        self.assertNotIn("YOUNG_ROUTER_VISION_BRIDGE_", schema)
        self.assertNotIn("YOUNG_ROUTER_COMPUTER_FACADE_", schema)
        for retired in (
            "YOUNG_ROUTER_RELAY_AUTO_GROUP_INTERVAL_MINUTES",
            "YOUNG_ROUTER_COMPUTER_FACADE_BACKEND",
            "YOUNG_ROUTER_COMPUTER_FACADE_MODEL",
            "YOUNG_ROUTER_COMPUTER_FACADE_MAX_STEPS",
            "YOUNG_ROUTER_COMPUTER_FACADE_TRACE",
            "YOUNG_ROUTER_COMPUTER_FACADE_TRACE_SCREENSHOTS",
            "YOUNG_ROUTER_COMPUTER_FACADE_ACTION_DENYLIST",
            "YOUNG_ROUTER_COMPUTER_FACADE_REQUIRE_OBSERVATION",
            "LITELLM_MAX_REQUESTS_BEFORE_RESTART",
            "LITELLM_STATE_TTL_SECONDS",
            "LITELLM_RUNTIME_VERIFY_WAIT_SECONDS",
            "LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS",
        ):
            self.assertNotIn(retired, schema)
            self.assertNotIn(f"  {retired}: ", localized)
        for added in (
            "YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS",
            "YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY",
            "YOUNG_ROUTER_LOG_BACKUP_SEGMENTS",
        ):
            self.assertIn(added, schema)
            self.assertIn(f"  {added}: {{ label:", localized)
        for key in (
            "YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED",
            "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED",
        ):
            self.assertIn(f"  {key}: {{ label:", localized)
        self.assertIn("const optionValues = stringList(item.options);", self.ui)
        self.assertIn("const next = optionValues[nativeEvent.index];", self.ui)
        self.assertNotIn('const label = stringValue(item.label, key);', self.ui)

    def test_assistant_settings_localize_display_values_without_changing_saved_values(self) -> None:
        self.assertNotIn("function FeatureToggles", self.ui)
        self.assertNotIn("codexFeatureLabel", self.ui)
        self.assertNotIn("label={key}", self.ui)
        self.assertIn("function PickerField", self.ui)
        self.assertIn("function ensureSelectedOption", self.ui)
        self.assertIn("A stale/unknown value must remain visible and selected", self.ui)
        self.assertIn("const selectedLabel = options.find((option) => option.value === value)?.label ?? value;", self.ui)
        self.assertIn("values: Array<string | AssistantSettingOption>", self.ui)
        self.assertIn("const option = options[nativeEvent.index];", self.ui)
        self.assertIn("if (option) onSelect(option.value);", self.ui)
        self.assertIn("assistantSettingOptions(values, optionTranslator)", self.ui)
        self.assertIn('titleKey: "clients.codex"', self.ui)
        self.assertIn('{ client: "claudeCode", titleKey: "claude.codeSection"', self.ui)
        self.assertIn('{ client: "claudeDesktop", titleKey: "claude.desktopSection"', self.ui)
        # The external settings pane lists client files instead of structured
        # Codex/Claude fields, so it localizes display metadata only.
        self.assertNotIn("localizeCodexValidationMessage(message, translate)", self.ui)

    def test_sensitive_settings_use_inline_native_password_controls(self) -> None:
        for marker in (
            "function NativeSecretInputControl(",
            "<NativeSecureTextInput domain={domain}",
            'domain="runtime" field="setting"',
            'domain="webdav" field="password"',
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
        self.assertIn("const openAssistantFile = (file: ClientFile): void => {", self.ui)
        self.assertIn("void flushAssistantEditorFields()", self.ui)
        self.assertIn("native.openFileEditor(JSON.stringify(editorTargetPayload(file, true)))", self.ui)
        # The sheet is warmed while the pane is open and switches documents
        # through the host's pending target, so opening never rebuilds it.
        self.assertIn("native.prepareFileEditor?.();", self.ui)
        self.assertIn("editorTargetFromPayload(native.pendingFileEditorTarget?.())", self.ui)
        self.assertIn("if (!target) return undefined;", self.ui)
        self.assertIn("onOpenFile={openAssistantFile}", self.ui)
        self.assertLess(
            self.ui.index("void flushAssistantEditorFields()"),
            self.ui.index("native.openFileEditor(JSON.stringify(editorTargetPayload(file, true)))"),
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
            'import "ace-builds/src-noconflict/mode-yaml";',
            'import "ace-builds/src-noconflict/ext-searchbox";',
            'type EditorLanguage = "json" | "toml" | "yaml" | "text";',
            "type ReplaceDocumentCommand = {",
            'type SetBaselineCommand = { type: "setBaseline"; baseline: string };',
            'type HostCommand = ReplaceDocumentCommand | SetBaselineCommand | InsertTextCommand | { type: "focus" };',
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
            'if (language === "yaml") return "ace/mode/yaml";',
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
            # Ace's content layer is one line taller than its scroller, so the
            # scroller's scrollHeight can never decide whether to show a bar.
            "function aceContentHeight(viewport: HTMLElement): number {",
            "Ace keeps its content layer one line taller than the scroller",
            "return Math.max(config?.maxHeight ?? 0, lineHeightTotal);",
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
            "#editor-menu {",
            ".editor-menu-item {",
            ".editor-menu-separator {",
            '<div id="editor-menu" role="menu" hidden></div>',
            "window.LiteLLMCodeEditorMenuLabels",
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

    def test_code_editor_cut_copy_and_context_menu_use_deterministic_actions(self) -> None:
        for marker in (
            "function writeClipboardText(text: string): boolean {",
            "copied = document.execCommand(\"copy\");",
            "function copySelection(editor: AceEditor): boolean {",
            "if (!copySelection(editor)) return;",
            "if (key === \"x\") editor.execCommand(\"cut\");",
            "function requestHostPaste(): void {",
            'post({ type: "paste" });',
            "function insertHostText(text: string): void {",
            'editor.execCommand("paste", text.slice(0, 1024 * 1024));',
            "function showEditorMenu(clientX: number, clientY: number): void {",
            "function runMenuAction(action: MenuAction): void {",
            'if (copySelection(editor)) editor.execCommand("cut");',
            'frame.addEventListener("contextmenu", (event) => {',
            "const MENU_ENTRIES: ReadonlyArray<MenuAction | \"separator\"> = [\"undo\", \"redo\", \"separator\", \"cut\", \"copy\", \"paste\", \"separator\", \"selectAll\"];",
        ):
            self.assertIn(marker, self.code_editor_web)
        # The async clipboard API is never the only route: its rejection used
        # to preventDefault away the browser's own copy/cut handling.
        self.assertNotIn("navigator.clipboard?.writeText", self.code_editor_web)
        self.assertNotIn("navigator.clipboard?.readText", self.code_editor_web)
        # Native hosts read the pasteboard for the context menu's Paste item.
        self.assertIn('if ([type isEqualToString:@"paste"])', MACOS_CONTROLS.read_text(encoding="utf-8"))
        self.assertIn('if (type == "paste")', WINDOWS_CONTROLS.read_text(encoding="utf-8"))

    def test_assistant_setting_option_labels_cover_user_visible_non_brand_values(self) -> None:
        assistant_i18n = (ROOT / "rn/packages/shared/src/i18n/assistantSettingsI18n.ts").read_text(encoding="utf-8")
        for marker in ('"amazon-bedrock": "Amazon Bedrock"', 'lmstudio: "LM Studio"', 'vscode: "VS Code"', 'terminal: "终端"'):
            self.assertIn(marker, assistant_i18n)

    def test_logs_show_empty_state_after_a_loaded_but_missing_log_source(self) -> None:
        self.assertIn('active ? translate("logs.empty") : translate("logs.loading")', self.ui)

    def test_external_settings_edit_claude_files_without_inventing_saved_defaults(self) -> None:
        # Claude documents are now file rows in the shared listing; the pane
        # never renders a structured Claude form or invents a saved default.
        for marker in (
            '{ client: "claudeCode", titleKey: "claude.codeSection"',
            '{ client: "claudeDesktop", titleKey: "claude.desktopSection"',
            'hintKey: "clients.claudeCodeFilesHint"',
            "function FileEditorWorkspace",
            'language={target.language === "text" ? "json" : target.language}',
        ):
            self.assert_ui_has(marker)
        for removed in (
            'translate("claude.deployment")',
            'field="deployment_token"',
            "const desktop = asRecord(state.desktop);",
            "const desktopModelNames = stringList(desktop.model_names);",
            'dispatch("desktop_patch", { inferenceProvider: inferenceProvider || null }, "claude")',
            'dispatch("desktop_models_patch", { model_names: splitLines(value) }, "claude")',
            'field="desktop_gateway_api_key"',
            'translate("settings.claudeUnavailable")',
            'patch_deployment',
            "booleanValue(settings.autoMemoryEnabled, true)",
            "booleanValue(sandbox.autoAllowBashIfSandboxed, true)",
            "booleanValue(sandbox.allowUnsandboxedCommands, true)",
            "booleanValue(settings.autoCompactEnabled, true)",
            'stringValue(settings.effortLevel, "medium")',
            '!booleanValue(filesystem.disabled)',
        ):
            self.assertNotIn(removed, self.ui)

    def test_runtime_form_rows_keep_labels_and_controls_aligned_when_reflowed(self) -> None:
        for marker in (
            "runtimeInputRow:",
            "runtimeFieldLabel:",
            # One shared grid with the external pane: the label column, the
            # control column, and the tip indent come from the same numbers.
            'runtimeFieldLabel: { ...SETTINGS_FIELD_LABEL }',
            'runtimeHelpSlot: { ...SETTINGS_FIELD_HELP_SLOT }',
            'const SETTINGS_FIELD_LABEL: TextStyle = { width: SETTINGS_FIELD_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" };',
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
            "<SettingsRail rows={tocRows}",
            "runtimeFieldList:",
            "runtimeModifiedBar:",
            "runtimeModifiedBarActive:",
            "runtimeFieldError:",
            "runtimeValueControlInvalid:",
            "runtimeResetButton:",
            "runtimeField: { ...SETTINGS_FIELD },",
            "runtimeHelpText:",
            "runtimeJsonDefaultHint:",
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
            'value={selectedProviderKey?.id ?? ""} values={[{ value: "", label: translate("providers.undefinedKey") }, ...providerKeyOptions]} disabled={busy} onSelect={selectProviderKey}',
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
            # The provider may be cleared (no list selection): fall back to no
            # identity so a fetch reply for another provider is still ignored.
            'const providerIdentity = provider ? identifier(provider) : "";',
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
            'NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}',
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
        self.assert_ui_has('helpTipAnchor: { position: "relative", zIndex: 2 }')
        self.assert_ui_not_has('protocolHint:')
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

    def test_editor_mutations_rebase_one_live_projection_revision_conflict(self) -> None:
        """Core checks the caller revision only after the store lock frees.

        A provider Apply rewrites the configuration and reloads the managed
        proxy while holding that lock, so a control that reads Core's shared
        revision before such an operation is accepted can never match it. The
        buffered dispatch rebases once on the authoritative snapshot instead of
        discarding the user's edit and reporting a conflict.
        """

        retryable = self.ui.split("function isRevisionRetryableAction(type: string): boolean", 1)[1].split(
            "\n}",
            1,
        )[0]
        self.assertIn('normalized.startsWith("model_") || normalized.startsWith("provider_")', retryable)
        # Wildcarding the editor families must not swallow the import actions:
        # their file capability is a one-time lease that a retry cannot reuse.
        self.assertIn('normalized.startsWith("model_")', retryable)
        self.assertNotIn('startsWith("providers_import_"', retryable)
        dispatch = self.ui.split("const enqueueDispatch =", 1)[1].split("const dispatch: Dispatch", 1)[0]
        self.assertIn("if (!isRevisionConflict(reason) || !isRevisionRetryableAction(type)) throw reason;", dispatch)

    def test_general_pane_service_actions_rebase_one_stale_revision(self) -> None:
        """The launch-at-login switch is an absolute, idempotent Core action.

        The shared Core revision also advances for work this window never saw
        (for example a clean-draft external settings reload that happens while
        the pane's snapshot is still catching up). The switch must take the
        buffered, rebasing service dispatch that keeps the live route revision
        instead of reporting "settings changed outside this window" for a
        change the user never made.
        """

        general = self.ui.split("function GeneralWorkspace", 1)[1].split("function RuntimeWorkspace", 1)[0]
        self.assertIn('await dispatchServiceAction(enabled ? "service.autostart_enable" : "service.autostart_disable");', general)
        self.assertNotIn("snapshot.revision", general)
        # Pane results stay in the one permanent bottom status strip: this pane
        # must not grow its own message row in the body.
        self.assertIn("onStatus: (message?: string) => void;", general)
        self.assertIn('onStatus(translate("common.saved"));', general)
        self.assertIn("onStatus(errorMessage(reason, translate));", general)
        self.assertNotIn("generalStatus", general)
        self.assertNotIn("generalStatus", self.ui)
        self.assert_ui_has('dispatchServiceAction={enqueueServiceDispatch} translate={translate} onStatus={setResult} onSnapshot={onSnapshot} /> : null}')
        # A rejected dispatch returns the native switch to the preference Core
        # actually kept instead of leaving it on the value the user clicked.
        self.assertIn("const [requestedAutoStart, setRequestedAutoStart] = useState<boolean>();", general)
        self.assertIn("const autoStartValue = requestedAutoStart ?? autoStartEnabled;", general)
        self.assertIn("value={autoStartValue}", general)
        self.assertIn("setRequestedAutoStart(undefined);", general)
        self.assertIn("dispatchServiceAction: (type: string) => Promise<unknown>;", self.ui)
        self.assertIn(
            'const enqueueServiceDispatch = (type: string, payload: UnknownRecord = {}): Promise<IpcResults["dispatch"]> => enqueueDispatch(type, payload, null);',
            self.ui,
        )
        self.assertIn("dispatchServiceAction={enqueueServiceDispatch}", self.ui)
        retryable = self.ui.split("function isRevisionRetryableAction(type: string): boolean", 1)[1].split("\n}", 1)[0]
        self.assertIn('normalized === "service_autostart_enable"', retryable)
        self.assertIn('normalized === "service_autostart_disable"', retryable)
        # A domain-less service action must not silently reuse the route domain,
        # and the route's own actions must still require one.
        dispatch = self.ui.split("const enqueueDispatch =", 1)[1].split("const dispatch: Dispatch", 1)[0]
        self.assertIn('if (!actionDomain && targetDomain !== null) return Promise.reject(new Error("A settings domain is required"));', dispatch)
        self.assertIn("const action = actionDomain === undefined ? { type, payload } : { domain: actionDomain, type, payload };", dispatch)

    def test_general_pane_offers_a_start_control_and_launch_start_retries(self) -> None:
        """A launch-time start that lost its race stays recoverable.

        The proxy follows the app, so a start that failed while spawning its
        workers at login (a login-time launch competes with every other
        start-up item) left 已停止 with no control in the pane. The launch start
        now retries on a bounded backoff, and the pane keeps one recovery button
        for a stopped or unhealthy service.
        """

        general = self.ui.split("function GeneralWorkspace", 1)[1].split("function RuntimeWorkspace", 1)[0]
        self.assertIn("const SERVICE_STARTUP_RETRY_DELAYS_MS = [0, 5_000, 20_000, 60_000];", self.ui)
        self.assertIn("startupAttempts.current = attempt + 1;", self.ui)
        self.assertIn('const timer = setTimeout(() => { void runServiceOperation("start"); }, delay);', self.ui)
        self.assertNotIn("startupAttempted", self.ui)
        self.assertIn('const serviceRestart = serviceState === "unhealthy";', general)
        self.assertIn('const serviceActionAvailable = serviceState === "stopped" || serviceRestart;', general)
        self.assertIn('await dispatchServiceAction(serviceRestart ? "service.restart" : "service.start");', general)
        self.assertIn('onStatus(translate("service.running"));', general)
        self.assertIn("onStatus(errorMessage(reason, translate));", general)
        self.assertIn('title={serviceRestart ? translate("service.restart") : translate("service.start")}', general)
        # The action reports its own progress instead of swapping its title or
        # graying out while the service restarts.
        self.assertIn("busy={serviceBusy} disabled={snapshot === undefined || (busy && !serviceBusy)}", general)
        self.assertIn("{serviceActionAvailable ? <NativeButton", general)
        self.assertIn("generalServiceAction: { minWidth: 96 }", self.ui)
        # The control is part of the 服务 row inside the pane, never of the
        # window's bottom action bar (which stays Close/Apply only).
        service_row = general.split('translate("general.serviceState")', 1)[1].split('translate("general.port")', 1)[0]
        self.assertIn("{serviceActionAvailable ? <NativeButton", service_row)

    def test_request_log_display_formats_duration_and_tokens_without_changing_records(self) -> None:
        """Units convert at display time only; recorded values stay raw."""

        for marker in (
            "function formatLogDuration(",
            "function formatLogTokens(",
            "Math.round(milliseconds / 100) / 10",
            "Math.round(tokens / 100) / 10",
            "const duration = formatLogDuration(compactLogValue(value.duration_ms));",
            "`${formatLogTokens(sentTokens)} / ${formatLogTokens(receivedTokens)}`",
            "{ label: translate(\"logs.duration\"), width: 62, value: (row) => row.duration }",
            "{ label: translate(\"logs.tokenCountK\"), width: 79, value: (row) => row.tokens }",
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
            "function requestStatusLabel(",
            'pending: "logs.sending"',
            'stream: "logs.streaming"',
            'success: "logs.success"',
            'failure: "logs.failed"',
            'stuck: "logs.stuck"',
            'aborted: "logs.aborted"',
            "function requestErrorReasonLabel(",
            "function logLevelLabel(",
            "function menuActionLabel(",
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
            # One permanent bottom status strip: the line count and the pause
            # state are its idle text, not a second bar under it.
            "const statusLine = [",
            "useEffect(() => { onStatus?.(statusLine); }, [onStatus, statusLine]);",
            'logsToolbar: { height: 28, minHeight: 28, flexShrink: 0, flexDirection: "row"',
            'logsTabs: { width: 640, maxWidth: "100%", minWidth: 0, height: 28, flexShrink: 0 },',
        ):
            self.assert_ui_has(marker)

    def test_log_vocabulary_is_localized_in_both_languages(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        # Every fixed log vocabulary the viewer renders must resolve through
        # i18n instead of printing the raw runtime token.
        for marker in (
            "function requestStatusLabel(",
            "function logLevelLabel(",
            "function menuActionLabel(",
            "function requestErrorReasonLabel(",
            "function routeTraceReasonLabel(",
            'const action = tab === "actions" ? menuActionLabel(',
            "status = logLevelLabel(servicePrefix[2], translate);",
            "status = logLevelLabel(proxyPrefix[2] || \"\", translate);",
            ": logErrorDetail(value.error, translate);",
        ):
            self.assert_ui_has(marker)
        for key, zh, en in (
            ("logs.success", "成功", "Success"),
            ("logs.stuck", "停滞", "Stalled"),
            ("logs.level.debug", "调试", "Debug"),
            ("logs.level.info", "信息", "Info"),
            ("logs.level.warning", "警告", "Warning"),
            ("logs.level.error", "错误", "Error"),
            ("logs.level.critical", "严重", "Critical"),
            ("logs.routeTrace.reasonStreamIncomplete", "上游流未完整结束", "Upstream stream ended before completion"),
            ("logs.routeTrace.reasonBodyCapacity", "上游请求体容量不足", "Upstream rejected the request body size"),
            ("logs.routeTrace.reasonImageUnsupported", "所有路由都不支持图像工具", "No route supports the image tool"),
            ("logs.routeTrace.reasonCompactionUnsupported", "上游不支持 Codex 压缩", "Upstream does not support Codex compaction"),
            ("logs.routeTrace.reasonImageFallback", "图像工具运行时回退", "Image tool runtime fallback"),
        ):
            self.assertIn(f'"{key}": "{en}",', english)
            self.assertIn(f'"{key}": "{zh}",', chinese)
            self.assertIn(f'| "{key}"', translation_keys)
        # The unmapped upstream reason stays readable in request details and
        # reusable menu actions keep their pane/service labels.
        for marker in (
            "return upstreamStatusReasonLabel(normalized, translate) ?? (key ? translate(key) : normalized);",
            '"open-providers-models": "status.providers",',
            '"service-restart": "service.restart",',
            '"set-language-zh-Hans": "language.simplified_chinese",',
            '"no-available-deployment": "logs.noAvailableRoute",',
            '"model-not-configured": "logs.modelNotConfigured",',
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
            'if (tab === "actions") return [\n'
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
        # Quietly restore sessions once per account when the panel mounts; a
        # restore that fails leaves the account signed out and never opens the
        # login page on its own (去登录 / + ask for it).  The quiet resource
        # refresh still runs: a locally known key is local data, and Core reads
        # the station through a remembered session when it has one.
        self.assertIn("const attemptedAccounts = useRef(new Set<string>());", relay)
        self.assertIn("attemptedAccounts.current.add(account.id);", relay)
        self.assertNotIn("if (!(await restoreSavedSession(account))) return;", relay)
        self.assertIn("await restoreSavedSession(account);", relay)
        self.assertIn("await refreshAccountResources(account, true);", relay)
        self.assertNotIn("const canAutoLogin =", relay)
        # Station connection details stay editable through staged updates.
        self.assertIn("const stageStationUpdate = async (overrides: StationDraft = {}): Promise<void> => {", relay)
        self.assertIn('await commit("station.update", { id: station.id, name, origin, type });', relay)
        self.assertIn("translate(\"relay.stationUpdateStaged\")", relay)
        # One login state drives the status column and both account actions:
        # 登录中 while the sign-in is in flight, otherwise 已登录 / 未登录 (the
        # expired and not-yet-probed cases read as 未登录 and keep the brown
        # alert row). 去登录 only appears while signed out and is never
        # disabled; 分组管理 shows the station's locally known keys, so it is
        # never gated on the login state that the keys outlive.
        self.assertIn('const relayLoginState = (account: RelayAccount): "signed_in" | "signing_in" | "signed_out" => {', relay)
        self.assertIn('if (loading[account.id]?.session) return "signing_in";', relay)
        self.assertIn('return effectiveLoginStatus(account) === "signed_in" ? "signed_in" : "signed_out";', relay)
        self.assertIn('translate(`relay.status.${relayLoginState(account)}`)', relay)
        self.assertIn('const selectedLoginState = selected ? relayLoginState(selected) : "signed_out";', relay)
        self.assertIn('{selectedLoginState === "signed_out" ? <NativeButton title={translate("relay.goLogin")} compact busy={pendingAction === "login"} disabled={controlsBusy && pendingAction !== "login"} onPress={() => { void loginSelected(); }} /> : null}', relay)
        # 去登录 opens the login page at once: the silent session probe must not
        # delay the sheet the user explicitly asked for.
        login_selected = relay.split("const loginSelected = async (): Promise<void> => {", 1)[1].split("const removeSelected", 1)[0]
        self.assertIn("await runPendingAction(\"login\", () => loginAccount(account));", login_selected)
        self.assertNotIn("restoreSavedSession(selected)", login_selected)
        self.assertIn('disabled={controlsBusy || !native.showGroupManager}', relay)
        self.assertNotIn('!native.showGroupManager || selectedLoginState !== "signed_in"', relay)
        self.assertNotIn('disabled={controlsBusy || isAccountLoading(selected.id)}', relay)
        self.assertIn('"relay.status.signing_in": "登录中"', self.zh)
        self.assertIn('"relay.status.signing_in": "Signing in"', self.en)
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
        self.assertIn("@objc private func applyPanel(_ sender: NSButton)", mac_leaf)
        self.assertIn("@objc private func closePanel(_ sender: NSButton)", mac_leaf)
        self.assertIn("@objc private func toggleSelectedEnabled(_ sender: NSButton)", mac_leaf)
        self.assertIn("func resultOnEnd() -> NativeGroupManagerResult?", mac_leaf)
        self.assertIn("let autoGrouping: Bool\n    let creates: [Create]\n    let updates: [Update]\n    let deletes: [String]", mac_leaf)
        self.assertIn("table.usesAlternatingRowBackgroundColors = true", mac_leaf)
        # The sheet list heads its columns with the same 名称 / 分组 / 倍率
        # labels the shared keys table used, on both hosts.
        self.assertIn('multiplierLabel: translate("relay.apiKeyMultiplier")', relay)
        self.assertIn("multiplierLabel: string;", types)
        self.assertIn("table.headerView = NSTableHeaderView(frame: NSRect(x: 0, y: 0, width: 0, height: headerHeight))", mac_leaf)
        self.assertIn('nameColumn.title = label("nameLabel")', mac_leaf)
        # 分组管理 names the key: 密钥名, not the generic 名称.
        self.assertIn('nameLabel: translate("providers.keyName"),', relay)
        self.assertIn('"providers.keyName": "密钥名"', self.zh)
        self.assertIn('"providers.keyName": "Key name"', self.en)
        self.assertIn('groupColumn.title = label("groupLabel")', mac_leaf)
        self.assertIn('multiplierColumn.title = label("multiplierLabel")', mac_leaf)
        self.assertIn("nameColumn.headerCell.attributedStringValue = groupManagerHeaderTitle(nameColumn.title)", mac_leaf)
        self.assertIn('labels.multiplier_label = read("multiplierLabel");', windows_module)
        self.assertIn("append_column_label(0, labels.name_label);", windows_leaf)
        self.assertIn("append_column_label(1, labels.group_label);", windows_leaf)
        self.assertIn("append_column_label(2, labels.multiplier_label);", windows_leaf)
        self.assertIn("list_frame.Child(list_body);", windows_leaf)
        # The footer saves and closes, and its button stays disabled until the
        # draft would change something: no no-op verification round-trip.
        self.assertIn('applyLabel: translate("status.saveAndClose")', relay)
        self.assertIn('"status.saveAndClose": "保存并关闭"', self.zh)
        self.assertIn('"status.saveAndClose": "Save and Close"', self.en)
        self.assertIn("applyButton.isEnabled = false", mac_leaf)
        self.assertIn("private func refreshApplyButton() {", mac_leaf)
        self.assertIn("guard applied, hasStagedChanges else { return nil }", mac_leaf)
        self.assertIn("guard hasStagedChanges else { return }", mac_leaf)
        self.assertIn("apply.IsEnabled(false);", windows_leaf)
        self.assertIn("auto refresh_apply = ", windows_leaf)
        self.assertIn("if (!*applied || !has_staged_changes()) return;", windows_leaf)
        # The list reports the store as it stands: the 分组 column names the
        # group and 倍率 carries the rate, on both hosts.  倍率 never carries
        # status text, so a row the draft will create or delete is dimmed.
        self.assertIn("private func presentation(for row: KeyRow) -> String {", mac_leaf)
        self.assertIn("private func presentation(for row: KeyRow) -> String {\n        multiplierText(for: row)\n    }", mac_leaf)
        self.assertIn("private func isStaged(_ row: KeyRow) -> Bool {", mac_leaf)
        self.assertIn("text?.textColor = isStaged(entry) ? .secondaryLabelColor : .labelColor", mac_leaf)
        self.assertIn("auto presentation = ", windows_leaf)
        self.assertIn("-> std::wstring {\n    return multiplier_text(row, labels);", windows_leaf)
        self.assertIn("auto is_staged = [](SheetRow const& row) { return row.deleted || row.draft; };", windows_leaf)
        # 分组 is a group name and 倍率 is a rate: neither field carries the
        # other, and the picker lists the name alone.
        self.assertIn('export function groupLabel(group: RelayGroup, translate: Translate): string {\n  return group.name || translate("relay.apiKeyUngrouped");\n}', relay)
        self.assertIn('name: group.name || translate("relay.apiKeyUngrouped"), rate: groupRateLabel(group)', relay)
        self.assertIn('"label": $0["label"] ?? "", "name": $0["name"] ?? ""', mac_module)
        self.assertIn("multiplier: groupRateLabel(resourceGroup(resource, current.groups)),", relay)
        self.assertIn("function groupRateLabel(group: RelayGroup | undefined): string {", relay)
        self.assertIn('{translate("relay.apiKeyMultiplier")}</Text><Text numberOfLines={1} style={styles.apiKeyPreviewValue}>{groupRateLabel(selectedGroup) || translate("common.none")}', relay)
        self.assertIn('label: `${accountKeyPrefix(account, translate)}/${resourceGroupName(resource, account.groups, translate)}`', relay)
        self.assertNotIn("resourceGroupLabel", relay)
        # The detail pane reports the selected key completely: its rate and the
        # station's key in plaintext with a native copy action; the models are
        # the selected key's whole station list, laid out by the sheet itself.
        self.assertIn("accountId: current.id,", relay)
        self.assertIn("hint: resource.keyHint,", relay)
        self.assertIn("models: resourceModelList(resource),", relay)
        self.assertIn("if (names.length < resource.models.length) names.push(\"…\");", relay)
        self.assertIn("const GROUP_MANAGER_MODEL_LIMIT = 256;", relay)
        self.assertIn("const GROUP_MANAGER_MODEL_LIST_CHARS = 12000;", relay)
        self.assertNotIn("models: String(resource.linkedModelCount),", relay)
        for label in (
            'valueLabel: translate("providers.keyValue")',
            'copyLabel: translate("relay.apiKeyCopy")',
            'copiedLabel: translate("relay.apiKeyCopied")',
            'failedLabel: translate("relay.operationFailed")',
            'modelsLabel: translate("relay.apiKeyModelList")',
            'emptyLabel: translate("common.none")',
        ):
            self.assertIn(label, relay)
        self.assertIn('"relay.apiKeyModelList": "模型列表"', self.zh)
        self.assertIn('"relay.apiKeyModelList": "Model list"', self.en)
        self.assertIn("export type RelayGroupManagerGroup = { id: string; label: string; name: string; rate: string };", types)
        self.assertIn('models: string[];', types)
        self.assertIn("let valueLabel = detailCaption(label(\"valueLabel\"))", mac_leaf)
        self.assertIn("let multiplierField = detailValue()", mac_leaf)
        self.assertIn('let copyButton = NSButton(title: "", target: self, action: #selector(copySelectedKey(_:)))', mac_leaf)
        # The copy is an icon button: the words ride it as its tooltip and its
        # accessibility name instead of a text button's width.
        self.assertIn('copyButton.image = NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: label("copyLabel"))', mac_leaf)
        self.assertIn('copyButton.toolTip = label("copyLabel")', mac_leaf)
        self.assertIn("copyButton.widthAnchor.constraint(equalToConstant: 22),", mac_leaf)
        self.assertIn('copy_glyph.Glyph(L"\\xE8C8");', windows_leaf)
        self.assertIn("private func copySelectedKey(_ sender: NSButton) {", mac_leaf)
        self.assertIn("CoreIPCBridge.shared.readPlainTextSecret(", mac_leaf)
        self.assertIn('domain: "relay_accounts",', mac_leaf)
        self.assertIn("private func showCopyStatus(_ message: String) {", mac_leaf)
        self.assertIn("copyButton?.isEnabled = canCopy(row)", mac_leaf)
        # 密钥值 is one line that shows the key's head, an ellipsis, and its
        # tail; the field keeps the whole value, so selecting it or pressing the
        # copy button hands over the key itself.  The reveal reads through
        # Core's lease and retries a lease that lost a revision race.
        self.assertIn("valueField.lineBreakMode = .byTruncatingMiddle", mac_leaf)
        self.assertIn("valueField.maximumNumberOfLines = 1", mac_leaf)
        self.assertIn("valueField.isSelectable = true", mac_leaf)
        self.assertIn("private func revealSelectedKey(_ row: KeyRow) {", mac_leaf)
        self.assertIn("private var revealedKeys: [String: String] = [:]", mac_leaf)
        # 密钥值 shows the key itself: a row whose station has no key says
        # 未提供, and the sheet never replaces the value with a presence label.
        self.assertIn('valueField?.stringValue = row.hint.isEmpty ? label("emptyLabel") : ""', mac_leaf)
        self.assertIn("valueField?.stringValue = \"\"", mac_leaf)
        self.assertIn("private static func readRevealedKey(target: String) -> String? {", mac_leaf)
        self.assertIn("if attempt < attempts { Thread.sleep(forTimeInterval: 0.25) }", mac_leaf)
        # The value row and the copy action read through that one retrying lease.
        self.assertEqual(2, mac_leaf.count("Self.readRevealedKey(target: target)"))
        # The sheet fills every row it can read, selected row first, so a row
        # carries its key when it is selected instead of staying empty.
        self.assertIn("private func fillRowKeys() {", mac_leaf)
        self.assertIn("private func drainRevealQueue() {", mac_leaf)
        self.assertIn("ids.remove(at: index)\n            ids.insert(selected, at: 0)", mac_leaf)
        # What this app already read is remembered for the next window: the
        # sheet opens on known keys instead of an empty value row.
        self.assertIn("private final class NativeRelayKeyMemo {", mac_leaf)
        self.assertIn("NativeRelayKeyMemo.shared.value(accountID: accountID, resourceID: row.id)", mac_leaf)
        self.assertIn("NativeRelayKeyMemo.shared.remember(accountID: self.accountID, resourceID: keyID, value: value)", mac_leaf)
        self.assertNotIn("savedLabel", mac_leaf)
        self.assertIn("self.valueField?.stringValue = value", mac_leaf)
        self.assertIn("private func fitPanelToContent() {", mac_leaf)
        # The sheet explains itself through its rows: the staged-changes tip
        # is gone, so the copy result is the detail column's last line.
        self.assertNotIn("hint: translate(\"relay.groupManagerHint\")", relay)
        self.assertIn("copyStatus.bottomAnchor.constraint(equalTo: toggle.topAnchor, constant: -14),", mac_leaf)
        # Close drops the draft, so a sheet that would lose edits confirms.
        self.assertIn('discardTitle: translate("relay.groupManagerDiscardTitle"),', relay)
        self.assertIn('discardBody: translate("relay.groupManagerDiscardBody"),', relay)
        self.assertIn('discardConfirm: translate("common.discard"),', relay)
        self.assertIn('"relay.groupManagerDiscardTitle": "放弃未保存的更改？"', self.zh)
        self.assertIn('"relay.groupManagerDiscardBody": "关闭分组管理会丢弃尚未保存的密钥更改。"', self.zh)
        self.assertIn("func confirmDiscardIfNeeded() -> Bool {", mac_leaf)
        self.assertIn("AppKitNativeLeaf.shared.confirm(", mac_leaf)
        # The title-bar close button asks the same question the footer Close
        # does, so the window never drops staged edits silently.
        self.assertIn("return groupManagerController?.confirmDiscardIfNeeded() ?? true", mac_leaf)
        self.assertIn("if (has_staged_changes() && !Confirm(labels.discard_title, labels.discard_body, labels.discard_confirm)) return;", windows_leaf)
        self.assertIn('labels.discard_title = read("discardTitle");', windows_module)
        self.assertNotIn("hint.Text(winrt::hstring(labels.hint));", windows_leaf)
        # The sheet opens on the account facts the provider window already
        # holds and loads the rest in place: it appears at once behind its
        # loading line, and the aligned draft arrives through the native update
        # instead of holding the window closed for the station round trip.  A
        # host without that update keeps the pre-load ordering.
        self.assertIn("updateGroupManager?(options: RelayGroupManagerSnapshot): Promise<boolean>;", types)
        self.assertIn("export type RelayGroupManagerSnapshot = {", types)
        self.assertIn("updateGroupManager?(options: RelayGroupManagerSnapshot): Promise<boolean>;", native_bridge)
        self.assertIn("updateGroupManager?: (options: RelayGroupManagerSnapshot) => Promise<boolean>;", platform_entry)
        self.assertIn("updateGroupManager: leaf.updateGroupManager", platform_entry)
        self.assertIn("loadingLabel: translate(\"relay.groupManagerLoading\"),", relay)
        self.assertIn('"relay.groupManagerLoading": "正在读取中转站…"', self.zh)
        self.assertIn('"relay.groupManagerLoading": "Reading the station…"', self.en)
        self.assertIn("const pending = update && groupManagerNeedsAlignment(account) ? loadGroupManagerAccount(account) : undefined;", relay)
        self.assertIn("const result = await native.showGroupManager({ ...groupManagerRequest(current), loading: Boolean(pending) });", relay)
        self.assertIn("if (!update) current = await loadGroupManagerAccount(account);", relay)
        # The update carries the sheet's content alone, which is exactly the
        # field set the native side accepts for it.
        self.assertIn("void push(groupManagerSnapshot(aligned)).catch(() => {", relay)
        self.assertIn("const groupManagerSnapshot = (current: RelayAccount) => {", relay)
        self.assertIn('guard Set(options.keys).isSubset(of: ["accountLabel", "groups", "keys", "autoGrouping"]),', mac_module)
        self.assertIn("guard let controller = groupManagerController, groupManagerPanel != nil else { return false }", mac_leaf)
        self.assertIn("@objc(updateGroupManager:resolver:rejecter:)", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(updateGroupManager:(NSDictionary *)options", mac_bridge)
        self.assertIn("func updateGroupManager(accountLabel: String, groups: [[String: String]], keys: [[String: String]], autoGrouping: Bool) -> Bool {", mac_leaf)
        self.assertIn("controller.applyData(", mac_leaf)
        self.assertIn("func applyData(accountLabel: String, groups: [GroupOption], rows: [KeyRow], autoGrouping: Bool) {", mac_leaf)
        self.assertIn("private func updateLoadingChrome() {", mac_leaf)
        self.assertIn("applyButton?.isEnabled = !loading && hasStagedChanges", mac_leaf)
        self.assertIn("!loading && toggle?.state == .off", mac_leaf)
        # The list header carries the load: the wheel turns beside 密钥 on the
        # key list the station answer is about to replace, and the words for
        # that wait ride the wheel as its tooltip.  It is the button's own wheel
        # - the shared pre-rendered frames, swapped in place - never a
        # progress-indicator view.
        self.assertIn("let loadingSpinner = NSImageView()", mac_leaf)
        self.assertIn('loadingSpinner.toolTip = label("loadingLabel")', mac_leaf)
        self.assertIn('loadingSpinner.setAccessibilityLabel(label("loadingLabel"))', mac_leaf)
        self.assertIn("loadingSpinner.leadingAnchor.constraint(equalTo: listTitle.trailingAnchor, constant: 6),", mac_leaf)
        self.assertIn("loadingSpinner?.image = AppKitBusySpinner.frames[loadingStep]", mac_leaf)
        self.assertIn("private func startLoadingWheel() {", mac_leaf)
        self.assertIn("private func stopLoadingWheel() {", mac_leaf)
        self.assertIn("RunLoop.main.add(timer, forMode: .common)", mac_leaf)
        # The footer keeps only the switch and the buttons.
        self.assertNotIn('let loadingLabel = NSTextField(labelWithString: label("loadingLabel"))', mac_leaf)
        self.assertNotIn("NSProgressIndicator", mac_leaf)
        # The list follows its key across the load while that key survives it.
        self.assertIn("let index = previousID.flatMap { id in rows.firstIndex { $0.id == id } } ?? (rows.isEmpty ? -1 : 0)", mac_leaf)
        self.assertIn("private static func groupNames(_ groups: [GroupOption]) -> [String: String] {", mac_leaf)
        self.assertIn("private static func modelGridRows(_ rows: [KeyRow]) -> Int {", mac_leaf)
        # The copy action keeps its own width, so the key wraps beside it.
        self.assertIn("copyButton.setContentCompressionResistancePriority(.required, for: .horizontal)", mac_leaf)
        # 模型列表 closes the sheet as an aligned, complete grid: it spans both
        # columns, lays every model out itself, and the sheet keeps its width.
        self.assertIn("private final class NativeModelsListView: NSScrollView {", mac_leaf)
        self.assertIn("func setModels(_ names: [String]) {", mac_leaf)
        self.assertIn("let modelsTitle = NSTextField(labelWithString: label(\"modelsLabel\"))", mac_leaf)
        self.assertIn("let modelsList = NativeModelsListView(font: detailFont, emptyText: label(\"emptyLabel\"))", mac_leaf)
        self.assertIn("modelsList?.setModels(row.modelNames)", mac_leaf)
        self.assertIn("let modelsHeight = modelsList.heightAnchor.constraint(equalToConstant: CGFloat(NativeGroupManagerController.modelGridRows(rows)) * 17)", mac_leaf)
        self.assertIn("modelsListHeight?.constant = CGFloat(NativeGroupManagerController.modelGridRows(rows)) * 17", mac_leaf)
        self.assertIn("listFrame.bottomAnchor.constraint(equalTo: toggle.topAnchor, constant: -14),", mac_leaf)
        self.assertIn("usePersistentScrollers(horizontal: false, vertical: true)", mac_leaf)
        self.assertIn("std::wstring EllipsizeMiddle(std::wstring const& value) {", windows_leaf)
        self.assertIn("kHead = 14;", windows_leaf)
        self.assertIn("kTail = 10;", windows_leaf)
        self.assertIn("value_text.Text(winrt::hstring(EllipsizeMiddle(*revealed_value)));", windows_leaf)
        self.assertNotIn("value_text.IsTextSelectionEnabled(true);", windows_leaf)
        self.assertIn("content.widthAnchor.constraint(equalToConstant: contentWidth),", mac_leaf)
        self.assertIn("let contentWidth = 20 + listWidth + 18 + detailWidth + 20", mac_leaf)
        self.assertIn("modelNames: (entry[\"models\"] ?? \"\").split(separator: \"\\n\").map(String.init),", mac_leaf)
        self.assertIn('"models": models.prefix(256).joined(separator: "\\n"),', mac_module)
        self.assertIn("auto reveal_value = ", windows_leaf)
        self.assertIn("value_text.Text(winrt::hstring(row.hint.empty() ? empty_label : std::wstring{}));", windows_leaf)
        self.assertIn("value_text.Text(L\"\");", windows_leaf)
        self.assertIn("if (!value && attempt < 2) std::this_thread::sleep_for(std::chrono::milliseconds(250));", windows_leaf)
        # The copy action and the pass that fills every row share that retry.
        self.assertEqual(2, windows_leaf.count("if (!value && attempt < 2) std::this_thread::sleep_for(std::chrono::milliseconds(250));"))
        self.assertIn("auto fill_row_keys = [rows, revealed_values, reveal_queue, reveal_in_flight, drain_reveal_queue,", windows_leaf)
        self.assertIn("pending.insert(pending.begin(), first);", windows_leaf)
        # What this app already read is remembered for the next sheet.
        self.assertIn("std::map<std::wstring, std::wstring> &RelayKeyMemo() {", windows_leaf)
        self.assertIn("RememberedRelayKey(account_id, row.id)", windows_leaf)
        self.assertIn("RememberRelayKey(account_id, key_id, *revealed_value);", windows_leaf)
        self.assertNotIn("saved_label", windows_leaf)
        self.assertIn("controls::StackPanel detail;", windows_leaf)
        # The Windows sheet lays the same models out in its own grid section.
        self.assertIn("controls::ScrollViewer models_scroll;", windows_leaf)
        self.assertIn("controls::ScrollViewer::SetVerticalScrollBarVisibility(models_scroll, controls::ScrollBarVisibility::Auto);", windows_leaf)
        self.assertIn("models_scroll.Height(models_height);", windows_leaf)
        self.assertIn("auto rebuild_models = ", windows_leaf)
        self.assertIn("rebuild_models(row.models);", windows_leaf)
        self.assertIn("std::vector<std::wstring> models;", windows_leaf)
        self.assertIn('labels.value_label = read("valueLabel");', windows_module)
        self.assertIn('labels.copy_label = read("copyLabel");', windows_module)
        self.assertIn('labels.models_label = read("modelsLabel");', windows_module)
        self.assertIn('labels.empty_label = read("emptyLabel");', windows_module)
        self.assertIn('account_id = Utf8ToWide(*account_id),', windows_module)
        # The model list travels as a list of names and is joined into the
        # newline-separated text the sheet carries on both hosts.
        self.assertIn("if (key == \"models\") {", windows_module)
        self.assertIn("(models && models->size() > 16384)", windows_module)
        self.assertIn("copy_button.Content(copy_glyph);", windows_leaf)
        self.assertIn("copy_button.Click(", windows_leaf)
        self.assertIn('CoreIPCBridge::Shared().ReadPlainTextSecret("relay_accounts", "api_key"', windows_leaf)
        self.assertIn("auto show_copy_status = [copy_status, status_timer]", windows_leaf)
        # 自动分组 owns one key per group: the sheet refreshes the station facts
        # and stages Core's alignment first, then lists only the keys still in a
        # group, so a checked switch never shows an ungrouped row or item.  That
        # load lands in the sheet that is already open, never in front of it.
        self.assertIn("await alignAutoGroupingAction(current.id);", relay)
        self.assertIn("pendingDelete: entry.pending_delete === true,", relay)
        self.assertIn("current.resources.filter((resource) => !resource.pendingDelete && resourceGroup(resource, current.groups) !== undefined)", relay)
        self.assertIn('if (current.type === "newapi" && !current.autoGrouping) {', relay)
        # The relay refresh reports its status through the dispatch action
        # summary; the top-level result carries the revision alone.
        self.assertIn('return asRecord(staged.action_summary).resource_status === "ready" ? "ready" : "unavailable";', (ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx").read_text(encoding="utf-8"))
        # The frame fits all three columns inside the pane, and both hosts floor
        # the trailing 未分组 text instead of clipping the multiplier column.
        # The sheet opens in the middle of the display like every other child
        # surface; a panel left at its creation origin sits in the corner.
        group_manager_panel = mac_leaf.split("func makePanel() -> NSPanel? {", 1)[1].split("func applyData(", 1)[0]
        self.assertIn("panel.center()", group_manager_panel)
        # The detail column states the key's facts at half the key list's
        # width, so the sheet never carries an empty right half.
        self.assertIn("let detailWidth: CGFloat = 189", mac_leaf)
        self.assertIn("detailGuide.widthAnchor.constraint(equalToConstant: detailWidth),", mac_leaf)
        self.assertIn("nameField.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),", mac_leaf)
        self.assertIn("modelsList.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),", mac_leaf)
        self.assertIn("right_column.Width(xaml::GridLengthHelper::FromPixels(190));", windows_leaf)
        self.assertIn("{590, window_height}", windows_leaf)
        self.assertIn("listFrame.widthAnchor.constraint(equalToConstant: listWidth),", mac_leaf)
        # 密钥's ＋ / － actions sit at the key list's top-right corner on both
        # hosts, not at the sheet's edge.
        self.assertIn("removeButton.trailingAnchor.constraint(equalTo: listFrame.trailingAnchor),", mac_leaf)
        # The ± pair stays compact like every other icon button: 22 pt each,
        # four points apart, on both hosts.
        self.assertIn("removeButton.widthAnchor.constraint(equalToConstant: 22),", mac_leaf)
        self.assertIn("addButton.widthAnchor.constraint(equalToConstant: 22),", mac_leaf)
        self.assertIn("addButton.trailingAnchor.constraint(equalTo: removeButton.leadingAnchor, constant: -4),", mac_leaf)
        self.assertIn("removeButton.controlSize = .small", mac_leaf)
        self.assertIn("add_button.Width(22);", windows_leaf)
        self.assertIn("remove_button.Width(22);", windows_leaf)
        self.assertIn("add_button.Margin(xaml::Thickness{4, 0, 0, 0});", windows_leaf)
        self.assertIn("append_column_label(0, labels.name_label);", windows_leaf)
        self.assertIn("list_header.Children().Append(add_button);", windows_leaf)
        self.assertIn("list_header.Children().Append(remove_button);", windows_leaf)
        self.assertIn("multiplierColumn.width = 74", mac_leaf)
        self.assertIn("constexpr double kMultiplierColumnWidth = 74;", windows_leaf)
        # 自动分组 follows the store: 未分组 for a stale or missing group in the
        # group column, while an ungrouped key leaves 倍率 empty rather than
        # repeating the label there.
        self.assertIn('ungroupedLabel: translate("relay.apiKeyUngrouped")', relay)
        self.assertIn("ungroupedLabel: string;", types)
        self.assertIn('labels.ungrouped_label = read("ungroupedLabel");', windows_module)
        self.assertIn("private func currentGroupText(for row: KeyRow) -> String {", mac_leaf)
        self.assertIn("private func multiplierText(for row: KeyRow) -> String {", mac_leaf)
        self.assertIn('guard !row.groupID.isEmpty, currentGroupNames[row.groupID] != nil else { return "" }', mac_leaf)
        # The names follow the sheet's data: the load in place replaces them
        # together with the rows that read them.
        self.assertIn("private var currentGroupNames: [String: String]", mac_leaf)
        self.assertIn("for group in groups where !group.id.isEmpty { names[group.id] = group.name }", mac_leaf)
        self.assertIn("self.currentGroupNames = NativeGroupManagerController.groupNames(groups)", mac_leaf)
        self.assertIn("auto current_group_text = ", windows_leaf)
        self.assertIn("auto multiplier_text = ", windows_leaf)
        self.assertIn("if (entry.id == row.group_id) return entry.name;", windows_leaf)
        windows_leaf_header = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.h").read_text(encoding="utf-8")
        self.assertIn("struct GroupManagerGroup {", windows_leaf_header)
        self.assertIn("std::vector<GroupManagerGroup> groups,", windows_leaf_header)
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
        self.assertIn("await runPendingAction(\"add\", startPendingLogin);", relay)
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
        self.assertNotIn('title={translate("status.apply")}', self.ui)

    def test_provided_keys_panel_groups_station_keys_with_staged_crud(self) -> None:
        ui = self.ui
        for marker in (
            '// Group headers only separate the two kinds; a single-kind list skips',
            '// them so custom-only vendors see a plain key table.',
            'if (showHeaders) rows.push({ key: "group:custom", cells: [`${translate("providers.keysCustom")} · ${customKeys.length}`], spanning: true });',
            'rows.push({ key: `custom:${key.id}`, cells: [showHeaders ? `\\t${key.name}` : key.name] });',
            '// The inline keys panel shows one list of a fixed height beside its editor',
            'const KEYS_INLINE_EDITOR_WIDTH = 124;',
            'const KEYS_INLINE_LIST_HEIGHT = 26 + 6 * 22;',
            'keysInlineBody: { minWidth: 0, height: KEYS_INLINE_LIST_HEIGHT, flexDirection: "row", alignItems: "flex-start", gap: 6 }',
            'keysTableInline: { flex: 1, minWidth: 0, height: KEYS_INLINE_LIST_HEIGHT, minHeight: KEYS_INLINE_LIST_HEIGHT }',
            'style={variant === "inline" ? styles.keysTableInline : styles.keysTable}',
            'keysEditorInline: { width: KEYS_INLINE_EDITOR_WIDTH, minWidth: 0, flexGrow: 0, flexShrink: 0, gap: 5 }',
            'keysEditorField: { minWidth: 0, gap: 2 }',
            '// Each inline editor row stacks its label above the control',
            'labelVisible={false}',
            'keysEditorFieldHeader: { height: 22, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 4 }',
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
            '// Core restarts the managed proxy for providers_models and runtime',
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
        # An empty key list keeps the area under it empty: the old hint read
        # like a value the user could edit.
        self.assertNotIn('translate("providers.keyListHint")', self.ui)
        self.assertNotIn('translate("providers.providedKeysNeedAccount")', self.ui)
        self.assertIn('} style={styles.panelActionButton} />)}\n      </> : null}\n    </View>;', self.ui)
        # The relay account section's + shares the key panel's right edge.
        self.assertIn(
            '<View style={styles.panelHeader}>\n        <Text style={styles.panelTitle}>{translate("providers.accounts")}</Text>\n        <View style={styles.panelActions}>',
            self.ui,
        )
        self.assertNotIn('<View style={styles.providerKeyActions}>\n          <NativeButton title="" symbol="plus"', self.ui)
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
            'officialActionsRow: { minHeight: 28, flexDirection: "row", flexWrap: "wrap"',
            'dataManagementSectionPicker: { flexDirection: "row", flexWrap: "wrap"',
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
            'keysEditorFieldHeader: { height: 22, flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 4 }',
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
        self.assert_ui_has('columns={[{ label: translate("providers.upstream"), width: 120 }, { label: translate("providers.publicModel"), width: 100 }, { label: translate("common.order"), width: 60 }]}')
        self.assertIn('"providers.upstream": "上游模型"', self.zh)
        self.assertIn('"providers.upstream": "Upstream"', self.en)
        self.assertIn('"providers.publicModel": "公开模型"', self.zh)
        self.assertIn('"providers.publicModel": "Public model"', self.en)
        self.assertNotIn('"providers.keyModelColumn"', self.zh)
        self.assertIn('"providers.keyOrderColumn": "密钥名 / 顺序"', self.zh)
        self.assertIn('"providers.keyOrderColumn": "Key / Order"', self.en)
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('providerMiddlePane: { flex: 1, minWidth: 0, gap: 6 }')
        self.assert_ui_has('keysPane: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('keysInline: { minWidth: 0, gap: 6, paddingTop: 6, borderTopWidth: 1, borderTopColor: systemColors.separator }')
        self.assert_ui_has('keysInlineBody: { minWidth: 0, height: KEYS_INLINE_LIST_HEIGHT, flexDirection: "row", alignItems: "flex-start", gap: 6 }')
        self.assert_ui_has('keysEditorFieldLabel: { flexShrink: 1, color: systemColors.label, fontSize: UI_FONT_SIZE }')
        # The custom key's 密钥值 field carries a copy icon at its trailing edge:
        # the icon rides the field's own row while the value keeps the rest of
        # it, and the copy reads the stored key through Core's capability.
        self.assert_ui_has('keysEditorValueRow: { minWidth: 0, flexDirection: "row", alignItems: "center", gap: 6 }')
        self.assert_ui_has('keysEditorValueField: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('<View style={styles.keysEditorValueRow}>')
        self.assert_ui_has('disabled={controlsBusy || !booleanValue(selectedCustom.configured)}')
        self.assert_ui_has('const copied = await native.copySecret({ domain: "providers_models", field: "api_key", target: `${providerId}\\u001f${selectedCustom.name}` });')
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
        self.assertIn('keysEditorField: { minWidth: 0, gap: 2 }', self.ui)
        # A plaintext field shows a value this app already read at once, without
        # another lease round trip, and keeps its loading hint out of the way.
        mac_controls = MACOS_CONTROLS.read_text(encoding="utf-8")
        self.assertIn("static NSMutableDictionary<NSString *, NSString *> *LiteLLMPlainTextSecretMemo(void)", mac_controls)
        self.assertIn("NSString *memoized = LiteLLMPlainTextSecretMemo()[LiteLLMPlainTextMemoKey(domain, field, target)];", mac_controls)
        self.assertIn("LiteLLMRememberPlainText(domain, field, target, value);", mac_controls)
        self.assertIn("LiteLLMRememberPlainText(domain, field, target, secret);", mac_controls)
        self.assertIn("LiteLLMForgetPlainText(_domain, _secretField, _target);", mac_controls)

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
            '<WebDavWorkspace snapshot={snapshot} busy={busy || webDavOperationBusy} probeBusy={pendingAction === "probe"} status={statuses.webdav} translate={translate} dispatch={dispatch} onSecretState={onSecretState} onProbe={() => runPendingAction("probe", onProbeWebDav)}>',
            '<NativeToggle accessibilityLabel={translate("webdav.enabled")} value={booleanValue(state.enabled)} disabled={busy} onValueChange={(enabled) => dispatch("patch", { enabled })} />',
            '<View style={[SETTINGS_FIELD_ROW_INDENTED, styles.webdavStateRow]}><Text numberOfLines={1} style={SETTINGS_FIELD_LABEL}>{translate("webdav.enabled")}</Text>',
            "webdavStateRow:",
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
            'webdavPasswordInput: { width: SETTINGS_FIELD_VALUE_WIDTH, minHeight: 26 }',
            'labelWidth={SETTINGS_FIELD_LABEL_WIDTH}',
            'controlWidth={SETTINGS_FIELD_VALUE_WIDTH}',
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
        # The tab's name rides the shared header band, so the retired 同步设置
        # heading left both the copy and the styles.
        self.assertNotIn("dataManagement.syncSettings", self.ui)
        self.assertNotIn("dataManagementPolishStyles.paneHeading", self.ui)
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
            'disabled={webDavBusy("sync") || snapshot?.webdav.enabled !== true}',
            'type: action, payload: { sections: [...WEBDAV_SYNC_DOMAINS] }',
            'onPress={() => { void runPendingAction("sync", () => onSyncWebDav(syncAction)); }}',
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
            '<NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}',

    
            'input: { width: "100%", minHeight: 26',
            'formRow: { width: "100%", minHeight: 26',
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

    def test_external_editor_is_a_native_sheet_not_an_overlay(self) -> None:
        # The editor is presented by the host (macOS sheet, Windows route), so
        # the shared UI keeps no dimmed overlay or in-window dialog for it.
        for removed in ("editorLayer", "editorDialog", "nativeSheetCornerRadius"):
            self.assertNotIn(removed, self.ui)
        self.assertIn("fileEditorRouteContent:", self.ui)
        self.assertIn("assistantFileSurfaceStyles.editorRoute", self.ui)

    def test_codex_raw_editors_share_height_and_follow_disk_generation(self) -> None:
        for marker in (
            "showLabel={false} showDiff codexPane syncRevision={syncRevision} style={assistantFileSurfaceStyles.editorRouteRaw}",
            "assistantFileSurfaceStyles.fileRow",
            "assistantFileSurfaceStyles.editorRoute",
            "function FileEditorWorkspace",
            "const [settingsRawReloadToken, setSettingsRawReloadToken] = useState(0);",
            "const [settingsRawBaselineToken, setSettingsRawBaselineToken] = useState(0);",
            'if (reloadDomain === "codex" || reloadDomain === "claude") setSettingsRawBaselineToken((current) => current + 1);',
            'if ((currentDisk[diskDomain]?.generation ?? 0) > priorGeneration && !currentDisk[diskDomain]?.changed && (diskDomain === "codex" || diskDomain === "claude"))',
            "await flushPendingFields();",
            "reloadToken={reloadToken}",
            "baselineToken={baselineToken}",
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

    def test_rejected_provider_apply_names_the_entries_that_block_it(self) -> None:
        # A rejected Apply used to leave only "fix the validation errors" with
        # nothing pointing at the offending row: an entry that was created but
        # never named rendered blank (empty string beat the placeholder), and
        # the domain answered with one opaque failure.  The pane now names the
        # provider and model position, and a rejected Apply shows those issues.
        self.assertIn(
            "function displayLabel(value: unknown, fallback: string): string {",
            self.ui,
        )
        self.assertIn('return stringValue(value).trim() || fallback;', self.ui)
        self.assertIn(
            'displayLabel(entry.display_name, displayLabel(entry.name, translate("providers.newProvider")))',
            self.ui,
        )
        self.assertIn(
            'displayLabel(entryModel.display_name, displayLabel(entryModel.model_name ?? entryModel.name, translate("providers.unnamedModel")))',
            self.ui,
        )
        self.assertIn(
            '      : upstreamModelLabel(entryModel);',
            self.ui,
        )
        self.assertIn('model_name_required: "validation.modelNameRequired"', self.ui)
        self.assertIn('model_upstream_required: "validation.modelUpstreamRequired"', self.ui)
        self.assertIn(
            'if (stringValue(asRecord(reason).code) === "validation_failed" && (domain === "providers_models" || route === "providers-models")) {',
            self.ui,
        )
        self.assertIn('const summary = await ipc.validate("providers_models");', self.ui)
        self.assertIn('setIssues(summary.issues);', self.ui)

        for locale in (self.zh, self.en):
            self.assertIn('"validation.modelNameRequired"', locale)
            self.assertIn('"validation.modelUpstreamRequired"', locale)

        # The row that needs the name is marked in place, so the pane points at
        # the entry instead of asking the user to hunt for it.
        self.assertIn("function modelNeedsAttention(model: UnknownRecord): boolean {", self.ui)
        self.assertIn(
            "const alertModelKeys = useMemo(() => models.filter(modelNeedsAttention).map(editorIdentifier), [models]);",
            self.ui,
        )
        self.assertIn("alertRowKeys={alertModelKeys}", self.ui)
        self.assertIn("alertRowKeys={alertProviderKeys}", self.ui)

        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn("def _entry_issues(providers: object) -> list[dict[str, Any]]:", domain)
        self.assertIn('"code": "model_name_required",', domain)
        self.assertIn('"code": "model_upstream_required",', domain)
        # The location has to survive the shared issue sanitizer, which drops
        # anything that is not a plain identifier.
        self.assertIn('location = "providers_models.{}.models[{}]".format(', domain)
        self.assertIn("def _provider_issue_label(provider: Mapping[str, Any]) -> str:", domain)
        self.assertIn('re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "provider"', domain)
        self.assertIn("api_key_value_required", domain)

    def test_plus_minus_buttons_hide_when_they_cannot_act(self) -> None:
        # A + / − that cannot act on the current state is hidden, never greyed
        # out: an empty list shows only the actions that apply to it.
        self.assertIn(
            '{provider ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteProvider} /> : null}',
            self.ui,
        )
        self.assertIn(
            '{provider && providerKindSelected !== "openai" && providerKindSelected !== "claude" ? <IconButton label="+" title={translate("providers.newModel")} disabled={busy} onPress={addModel} /> : null}',
            self.ui,
        )
        self.assertIn(
            '{model ? <IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteModel} /> : null}',
            self.ui,
        )
        self.assertIn(
            'const canAddKey = !(selectedGroup === "provided" && (autoGrouping || stationAccounts.length === 0 || !relay.apiKeyActions.create));',
            self.ui,
        )
        self.assertIn(
            'const canDeleteKey = Boolean(selectedCustom) || Boolean(selectedProvided && !autoGrouping);',
            self.ui,
        )
        self.assertIn(
            '{canAddKey ? <IconButton label="+" title={translate("providers.addKey")} disabled={controlsBusy} onPress={addKey} /> : null}',
            self.ui,
        )
        self.assertIn(
            '{canDeleteKey ? <IconButton label="−" title={translate("common.delete")} disabled={controlsBusy} onPress={deleteSelected} /> : null}',
            self.ui,
        )
        # No keys means no key table: the empty bordered box is not drawn, and
        # the inline panel keeps the one table beside its editor.
        self.assertEqual(1, self.ui.count("{tableRows.length > 0 ? keysTable : null}"))
        self.assertIn('{tableRows.length > 0 ? <View style={styles.keysInlineBody}>', self.ui)
        # The keys panel no longer measures the pane: its list height is fixed.
        self.assertNotIn("paneViewportHeight", self.ui)
        self.assertNotIn("paneContentHeight", self.ui)
        self.assertIn('const vendorBaseURL = (drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base))).trim();', self.ui)
        self.assertIn(
            '{vendorBaseURL ? <NativeButton title="" symbol="plus" compact toolTip={translate("providers.addRelayAccount")}',
            self.ui,
        )
        relay = self.relay
        self.assertIn(
            '{stationAccounts.length < 8 ? <NativeButton title="" symbol="plus" compact toolTip={translate("relay.addAccount")}',
            relay,
        )
        self.assertIn(
            '{selected ? <NativeButton title="" symbol="minus" compact destructive toolTip={translate("relay.removeLocal")}',
            relay,
        )

    def test_cleared_list_selection_drops_the_minus_and_editor(self) -> None:
        # A click on the empty space below a list clears its native selection.
        # The shared view has to follow that cleared state: the − and the editor
        # below it only ever act on the row the list still highlights.
        self.assertIn(
            '?? (selectedProvider === undefined ? providers[0] : undefined)',
            self.ui,
        )
        self.assertIn('if (selectedProvider === "") return;', self.ui)
        self.assertIn(
            'const [selectionCleared, setSelectionCleared] = useState(false);',
            self.ui,
        )
        self.assertIn('if (selectionCleared) return;', self.ui)
        self.assertIn('setSelectionCleared(key === "");', self.ui)
        self.assertIn(
            '?? (selectedID === undefined ? stationAccounts[0] : undefined)',
            self.relay,
        )
        self.assertIn('if (selectedID === "") return;', self.relay)
        # The route inspector empties on a cleared route selection instead of
        # keeping the last route active without a highlighted row.
        self.assertIn('if (!routeId) {', self.ui)
        self.assertIn('selectedRoute === ""', self.ui)
        # Navigation rails keep their current entry: the settings sidebar, the
        # runtime table of contents, and the data tabs always show one surface.
        self.assertIn('onSelectionChange={(key) => { if (key) switchDataManagementTab(key as DataManagementTab); }}', self.ui)
        self.assertIn('if (!name) return;', self.ui)

    def test_new_entries_fall_back_to_placeholder_names(self) -> None:
        # New entries are created under the localized placeholder name (with a
        # numeric suffix while it is taken); only the wizard's key-name field
        # seeds a random word, the key panel keeps key-1, key-2, … and the
        # native group-manager sheet keeps "New key".
        self.assertIn("function uniquePlaceholderName(existing: string[], base: string): string {", self.ui)
        self.assertIn("function uniqueKeyName(existing: string[]): string {", self.ui)
        self.assertIn("const RANDOM_KEY_NAME_WORDS = [", self.ui)
        self.assertIn("function randomKeyName(existing: string[]): string {", self.ui)
        self.assertIn("setKeyName(pendingNewKeyName ?? randomKeyName([]));", self.ui)
        self.assertIn("const initialKeyName = randomKeyName([]);", self.ui)
        self.assertIn("const name = uniqueKeyName(customKeyNames);", self.ui)
        self.assertNotIn("randomWordName", self.ui)
        self.assertNotIn("RANDOM_NAME_WORDS", self.ui)

        leaf = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        self.assertIn('let base = label("newKeyName", "New key")', leaf)
        self.assertIn('name = "\(base) \(suffix)"', leaf)
        self.assertNotIn("randomNameWords", leaf)
        self.assertIn('std::wstring base = labels.new_key_name.empty() ? std::wstring(L"New key") : labels.new_key_name;', windows)
        self.assertIn('name = base + L" " + std::to_wstring(suffix);', windows)
        self.assertNotIn("kRandomNameWords", windows)
        self.assertNotIn("#include <cstdlib>", windows)

    def test_provider_list_header_adds_a_provider_while_the_wizard_keeps_its_button(self) -> None:
        # The list header's + creates a provider directly; the toolbar keeps the
        # wizard as its own action.
        self.assertIn(
            '<TablePane style={styles.providerListPane} title={translate("providers.providers")} actions={<><IconButton label="+" title={translate("providers.newProvider")} disabled={busy} onPress={addProvider} />',
            self.ui,
        )
        self.assertIn('const addProvider = (): void => {', self.ui)
        self.assertIn('const base = translate("providers.newProvider");', self.ui)
        self.assertIn('const name = uniquePlaceholderName(providers.map((item) => providerDisplayName(item)), base);', self.ui)
        self.assertIn('void dispatch("provider.add", { provider: { name, models: [] } });', self.ui)
        self.assertIn('const pendingProviderIds = useRef<Set<string> | undefined>(undefined);', self.ui)
        self.assertIn(
            '<ActionButton title={translate("providers.addWizard")} disabled={busy} style={styles.providerWizardToolbarButton} onPress={onOpenWizard} />',
            self.ui,
        )

    def test_first_base_url_commit_names_a_custom_provider_from_the_host(self) -> None:
        # A provider created without a URL takes its name from the first
        # non-empty base URL, derived by the shared helper ("aaa.bb.cc" → "bb",
        # "aaa.cc" → "aaa", IP/localhost → "").  Later URL edits keep the name
        # the user chose, and the relay-station branch never carries a patched
        # name because the station manages it.
        self.assertIn('const suggested = suggestedProviderName(endpoint);', self.ui)
        self.assertIn('const previousBaseURL = stringValue(provider.endpoint, stringValue(provider.api_base));', self.ui)
        self.assertIn('if (!previousBaseURL.trim() && endpoint.trim()) {', self.ui)
        self.assertIn('suggested.toLocaleLowerCase() !== providerName.trim().toLocaleLowerCase()', self.ui)
        self.assertIn('const name = uniqueProviderName(drafts?.providers ?? [], suggested, providerID);', self.ui)
        self.assertIn('changes.name = name;', self.ui)
        self.assertIn('onNameDraftChange?.(name);', self.ui)
        self.assertIn('return dispatch("provider.patch", { provider_id: providerID, changes });', self.ui)
        # The name rides in the same patch as the endpoint, and a matched
        # station returns before that patch so it keeps managing its provider.
        self.assertLess(
            self.ui.index('return dispatch("provider.select_relay_station"'),
            self.ui.index('const suggested = suggestedProviderName(endpoint);'),
        )
        self.assertIn("function uniqueProviderName(providers: UnknownRecord[], name: string, excludeID = \"\"): string {", self.ui)
        self.assertIn("while (providerNameExists(providers, candidate, excludeID)) {", self.ui)

    def test_a_new_model_keeps_its_key_a_user_choice(self) -> None:
        # Adding a model used to bind it to whichever key came first, which the
        # pane showed as a fixed key the user never picked.  The model stays
        # unbound ("default") until the user chooses a key.
        self.assertIn(
            'return key ? keyName?.(key.id, key.name) ?? key.name : displayLabel(model.api_key_name, translate("providers.undefinedKey"));',
            self.ui,
        )
        # The list matches the key the way the editor does, so a moved slot id
        # cannot leave the list under "undefined key" while the editor shows it.
        self.assertIn("const keyStates = providerKeyStates(provider);", self.ui)
        self.assertIn(
            '?? keyStates.find((entry) => entry.name === stringValue(model.api_key_name));',
            self.ui,
        )
        self.assertIn(
            'value={selectedProviderKey?.id ?? ""} values={[{ value: "", label: translate("providers.undefinedKey") }, ...providerKeyOptions]} disabled={busy} onSelect={selectProviderKey}',
            self.ui,
        )
        # The editable field carries the model's real value: the placeholder
        # only labels the row and the breadcrumb, so a blank name cannot look
        # like it was set while validation still reports it missing.
        self.assertIn("const modelFieldName = stringValue(model.model_name, stringValue(model.name));", self.ui)
        self.assertIn("value={modelFieldName}", self.ui)
        self.assertIn('{displayLabel(modelName, translate("providers.unnamedModel"))}', self.ui)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.unnamedModel"', locale)
        self.assertIn('"providers.unnamedModel": "未命名模型"', self.zh)
        self.assertIn('"providers.unnamedModel": "Unnamed model"', self.en)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.undefinedKey"', locale)
        self.assertIn('"providers.undefinedKey": "未定义密钥"', self.zh)
        self.assertIn('"providers.undefinedKey": "Undefined key"', self.en)
        self.assertNotIn('label: translate("common.default") }, ...providerKeyOptions]', self.ui)
        self.assertIn("if (!providerKeyID) {", self.ui)
        self.assertIn(
            'void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { provider_key_id: "", api_key_name: "" } });',
            self.ui,
        )

        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn("if key is not None and (explicit_key_id or explicit_key_name):", domain)
        self.assertIn('model["api_key_name"] = key["name"]', domain)

        # Materialization still routes an unbound model through the provider's
        # default key, so the draft keeps working at runtime.
        dumper = (ROOT / "config_editor_core/dump.py").read_text(encoding="utf-8")
        self.assertIn("if api_key_item is None and not provider_key_id and not key_name:", dumper)
        self.assertIn("api_key_item = default_keys[0] if default_keys else None", dumper)

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

    def test_external_settings_and_webdav_actions_keep_visible_labels(self) -> None:
        self.assert_ui_has('title={translate("dataManagement.testConnection")}')
        self.assert_ui_has('title={translate("dataManagement.testConnection")}')
        self.assert_ui_has('title={translate("dataManagement.syncNow")}')
        # The pane edits files directly; the old structured heading is gone,
        # and the one remaining client-level shortcut lives in Codex's header.
        self.assertNotIn('translate("settings.structured")', self.ui)
        self.assertIn('dispatch("use_local_api", selection, "codex")', self.ui)
        self.assertNotIn('dispatch("use_local_api", selection, "claude")', self.ui)
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

    def test_settings_shell_copy_never_names_an_apply_action(self) -> None:
        """The shell applies edits immediately; no pane may describe an Apply step."""
        english = self.en
        chinese = self.zh
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        core_language = (ROOT / "young_router/core/domains/language.py").read_text(encoding="utf-8")
        # The route-level Apply vocabulary is gone from every copy source: the
        # shared locales, their key union, and Core's `language` mirror. A
        # string that names a control the shell does not render must never
        # come back as a tip.
        for removed in (
            "status.apply",
            "common.applied",
            "common.saveAndApply",
            "common.noChanges",
            "screen.dirty",
            "providers.apiKeyMissingHint",
            "providers.deleteKeyBody",
            "providers.clearKeyBody",
            "providers.wizard.summary",
            "settings.codexMissing",
            "dataManagement.applyImported",
            "relay.stationOverviewHint",
            "relay.stationEditHint",
            "relay.stepApply",
            "relay.stepApplyDetail",
            "relay.apiKeyEnableStaged",
            "relay.apiKeyDisableStaged",
            "relay.apiKeyAutoGroupingStaged",
            "relay.pendingOperationsCount",
        ):
            for source in (english, chinese, translation_keys, core_language):
                self.assertNotIn(f'"{removed}"', source)
        # The status strip reports the save (or the reload) that actually
        # happened; the shell's own commit stays the only write path.
        self.assertIn('message: string | null = "common.saved"', self.ui)
        self.assertIn('options?.message ?? "common.saved"', self.ui)
        self.assertIn('translate(appliedKey ?? "common.saved")', self.ui)
        self.assertIn('}, "common.reloaded");', self.ui)
        for text in (english, chinese):
            self.assertIn('"common.saved":', text)
            self.assertIn('"common.reloaded":', text)
        # A rejected live write names the fix, not an action that does not
        # exist, and Core reports the missing key value as a coded location so
        # the pane can point at the provider row it has to open.
        self.assertIn('"error.validationFailed": "Validation failed. Fix the problems listed above."', english)
        self.assertIn('"error.validationFailed": "校验失败，请修正列出的问题。"', chinese)
        self.assertIn('api_key_value_required: "validation.apiKeyValueRequired"', self.ui)
        self.assertIn('"code": "api_key_value_required"', (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8"))
        # Every literal key the two pane surfaces translate must resolve in both
        # locales to copy that never instructs an Apply press. The only writes
        # the shell performs are its own immediate applies, so a key that talks
        # about one belongs to a child surface with its own button instead.
        english_copy = dict(re.findall(r'^  "([^"]+)": "(.*)",$', english, re.M))
        chinese_copy = dict(re.findall(r'^  "([^"]+)": "(.*)",$', chinese, re.M))
        english_apply = re.compile(
            r"Apply to |on Apply|Pending Apply|before applying|after applying|during Apply|will run on Apply|select Apply|press Apply|Confirm and Apply|Apply failed"
        )
        chinese_apply = re.compile(r"再应用|点击“应用”|点击应用|待应用|应用后将|应用时|应用前|“应用”后")
        for path in (UI_SOURCE, RELAY_MANAGER):
            used = set(re.findall(r'translate\(\s*"([^"]+)"', path.read_text(encoding="utf-8")))
            self.assertTrue(used)
            for key in used:
                self.assertIn(key, english_copy, f"{path.name}: {key}")
                self.assertIn(key, chinese_copy, f"{path.name}: {key}")
                self.assertIsNone(english_apply.search(english_copy[key]), f"{path.name}: {key}")
                self.assertIsNone(chinese_apply.search(chinese_copy[key]), f"{path.name}: {key}")

    def test_macos_leaf_localizes_window_titles_and_keeps_status_menu_order(self) -> None:
        # Settings panes share one window, so its title is the app name and the
        # sidebar selection names the active pane.
        self.assertIn('if Self.settingsPaneRoutes.contains(canonicalRoute(route)) {', self.macos_leaf)
        self.assertIn('return localized("appTitle", fallback: "Young Router")', self.macos_leaf)
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
        macos_app = (ROOT / "rn/apps/macos/macos/YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        windows_app = (ROOT / "rn/apps/windows/windows/YoungRouter/YoungRouter.cpp").read_text(encoding="utf-8")
        for source in (self.ui, routes, types, self.macos_leaf, self.platform_entry, self.windows_leaf, macos_app, windows_app):
            self.assertNotIn('"webdav-settings"', source)
            self.assertNotIn('"open-webdav-settings"', source)
            self.assertNotIn("routeWebdavSettings", source)
        self.assertIn('{ id: "general-settings", titleKey: "status.general" }', routes)
        self.assertIn('{ id: "data-management", titleKey: "status.dataManagement" }', routes)
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
            '{ id: "language-picker", title: translate("status.language"), enabled: true }',
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
