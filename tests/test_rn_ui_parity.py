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
        self.assertIn('"service.runningOnPort": "运行中（端口 {port}）"', chinese)

    def test_assistant_settings_use_one_tabbed_surface_with_active_domain_actions(self) -> None:
        for marker in (
            'type AssistantSettingsDomain = "codex" | "claude";',
            'const settingsRoute = isAssistantSettingsRoute(route);',
            'const domain = settingsRoute ? settingsTab : domainForRoute(route);',
            'title: "Codex" }, { id: "claude", title: "Claude"',
            'selected={settingsTab} disabled={busy}',
            'await flushPendingFields();',
            'const dirtyDomains = settingsRoute',
            '? (["codex", "claude"] as const).filter((name) => current.drafts[name]?.dirty)',
            'for (const name of dirtyDomains) await enqueueDispatch("cancel", {}, name);',
            "const claudeDeploymentDraftRef = useRef<ClaudeDeploymentDraft | undefined>(undefined);",
            "const hasClaudeDeploymentChanges = (currentSnapshot: CoreSnapshot | undefined)",
            'if (dirtyDomains.length === 0 && (!settingsRoute || !hasClaudeDeploymentChanges(current)))',
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
            'tooltip={probePresentation.full}',
            'translate("providers.probeOriginalRequest"',
            'const selectedOrder = [nextOrder[0]];',
            'if (sameStringOrder(currentOrder, selectedOrder)) return;',
            'await enqueueDispatch("model.patch", {',
            'await ipc.apply("providers_models", staged.revision, confirmations);',
            'supported_upstream_url_surfaces: selectedOrder,',
            'label={translate("common.enable")}',
            'changes: { model_enabled }',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('await flushPendingFields();\n      const before = await ipc.snapshot();', self.ui)

    def test_provider_selection_and_new_models_keep_independent_stable_state(self) -> None:
        self.assert_ui_has('onSelectionChange={(key) => { setSelectedProvider(key); setSelectedModel(undefined); setProviderSourceModel(undefined); }}')
        self.assert_ui_has('const knownModelIdsByProvider = useRef<Map<string, Set<string>> | undefined>(undefined);')
        self.assert_ui_has('Promise.all(added.map(({ providerId: targetProviderId, modelId }) => probeModel(targetProviderId, modelId)))')
        self.assert_ui_has('const key = modelProbeKey(targetProviderId, targetModelId);')

    def test_log_view_identifies_when_the_recent_record_limit_is_reached(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        self.assert_ui_has('active && lineCount >= active.limit ? "logs.latestLinesAtLimit" : "common.lines"')
        self.assertIn('"logs.latestLinesAtLimit": "Latest {count} lines (view limit)"', english)
        self.assertIn('"logs.latestLinesAtLimit": "最近 {count} 行（视图上限）"', chinese)

    def test_providers_workspace_keeps_legacy_segmented_three_pane_structure(self) -> None:
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
        self.assert_ui_has('providerInspector: { width: 340, minWidth: 340, maxWidth: 340')
        self.assertNotIn("<ScrollView contentContainerStyle={styles.providerEditorScroll}><ProviderEditor", self.ui)
        self.assert_ui_has("providerEditorContent: { flex: 1, minHeight: 0, paddingTop: 4, paddingHorizontal: 14, paddingRight: 10, paddingBottom: 16")
        # At the provider window's fixed minimum width, the model table needs
        # all four native columns (118 + 130 + 112 + 104 = 464pt), plus the
        # 2pt native table bezel. Its left sibling is 190pt with 188pt of
        # columns (140 + 48), so the 668pt workspace leaves 466pt after the
        # 12pt inter-table gap. The inspector keeps its key list beside the
        # selected key editor; credentials remain native secure inputs without
        # extra Set/Clear controls beneath the form.
        self.assert_ui_has("providerLeftColumn: { flex: 1, minWidth: 0 }")
        self.assert_ui_has("providerListPane: { width: 190, minWidth: 190, maxWidth: 190")
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 140 }, { label: translate("providers.models"), width: 48 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.model"), width: 118 }, { label: translate("providers.upstream"), width: 130 }, { label: translate("providers.balance"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 104 }]}')
        self.assert_ui_has('columns={[{ label: translate("providers.key"), width: 138 }]}')
        self.assert_ui_has('label={translate("providers.keyName")} labelWidth={64} labelAlign="left"')
        self.assert_ui_has('NativeSecretField plainText autoCommit label={translate("common.apiKey")}')
        self.assert_ui_has('providerKeyGrid: { flex: 1, minHeight: 142, flexDirection: "row", alignItems: "flex-start", gap: 12 }')
        self.assert_ui_has('providerKeyTable: { width: 138, minWidth: 138, maxWidth: 138, height: 142, minHeight: 142, flexShrink: 0 }')
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 464')
        self.assert_ui_has('providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 12')
        self.assert_ui_has('{viewMode === "routes" ? <View style={[styles.routeWorkspace, styles.routeWorkspaceWithInspector]}')
        self.assert_ui_has('<TablePane wide style={styles.routeTablePane}')
        self.assert_ui_has('<View style={styles.providerInspector}>')
        self.assertNotIn('viewMode === "routes" ? <View style={styles.providerWorkspace}', self.ui)
        self.assert_ui_has('routeWorkspaceWithInspector: { flexDirection: "row", gap: 12 }')
        self.assert_ui_has('routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('onSelectionChange={selectRoute}')
        self.assert_ui_has('width: 170 }, { label: translate("common.order"), width: 56')
        self.assert_ui_has('width: 130 }, { label: translate("providers.upstream"), width: 164')
        self.assert_ui_has('providerSourceModel ? <ProviderEditor provider={activeRoute.provider}')
        self.assert_ui_has('model={activeRoute.model}')
        self.assert_ui_has('onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))}')
        self.assert_ui_has('setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`)')
        self.assert_ui_has('disabledRowKeys={disabledModelKeys}')
        self.assert_ui_has('disabledRowKeys={disabledRouteKeys}')
        self.assertNotIn('secondaryCellKeys={routeSecondaryCellKeys}', self.ui)
        self.assertNotIn('<ScrollView contentContainerStyle={styles.inspectorContent}>', self.ui)
        self.assert_ui_has('native.window.open("relay-accounts")')
        self.assert_ui_has('route === "relay-accounts" ? <RelayAccountManager visible')

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
        self.assertIn('footer: { height: 60, minHeight: 60, flexShrink: 0', self.ui)
        self.assertIn('paddingHorizontal: 20, paddingVertical: 12', self.ui)
        self.assertIn('footerButtons: { flexShrink: 0', self.ui)
        self.assertNotIn("footerExact", self.ui)
        self.assertNotIn('exact={route === "providers-models"}', self.ui)

    def test_logs_default_to_requests_and_follow_the_latest_row(self) -> None:
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn('setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");', bootstrap)
        self.assert_ui_has('useState<typeof LOG_TABS[number]>("requests")')
        self.assert_ui_has('compact followBottom onRowDoublePress=')
        self.assert_ui_has('translate("logs.apiKeyName")')
        self.assert_ui_has('value.api_key_name')
        self.assert_ui_has('`${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}')

    def test_health_poll_only_recovers_a_process_that_has_exited(self) -> None:
        self.assert_ui_has('if (current.service.state !== "stopped") return;')
        self.assertNotIn('current.service.state !== "stopped" && current.service.state !== "unhealthy"', self.ui)

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
            '"providers.configurationFile": "Import from File…"',
            '"providers.relay": "Import from Relay…"',
            '"providers.exportFile": "Export to File…"',
        ):
            self.assertIn(value, english)
        for value in (
            '"providers.currentCodex": "从当前 Codex 设置导入"',
            '"providers.currentClaude": "从当前 Claude 设置导入"',
            '"providers.configurationFile": "从文件导入…"',
            '"providers.relay": "从中转站导入…"',
            '"providers.exportFile": "导出到文件…"',
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
        self.assert_ui_has("structuredForm: { gap: 7 }")
        self.assertNotIn("twoColumnForm", self.ui)

    def test_claude_settings_expose_safe_capability_skill_worktree_and_advanced_controls(self) -> None:
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for marker in (
            'translate("claude.disableBypassPermissions")',
            'translate("claude.capabilities")',
            'translate("claude.disableBundledSkills")',
            'translate("claude.skillSettings")',
            'claudeSkillOverridesPatch(skillOverrides, value)',
            'translate("claude.worktree")',
            'translate("claude.worktreeBaseRef")',
            'translate("claude.advanced")',
            'translate("claude.feedbackSurveyRate")',
            'translate("claude.companyAnnouncements")',
            'translate("claude.diffTool")',
            'translate("claude.teammateDefaultModel")',
            'translate("claude.autoConnectIde")',
            'translate("claude.autoInstallIdeExtension")',
            'translate("claude.externalEditorContext")',
            'translate("claude.permissionExplainer")',
            'translate("claude.disableAgentView")',
            'translate("claude.disableArtifact")',
            'translate("claude.skipWebFetchPreflight")',
        ):
            self.assert_ui_has(marker)
        for text in (english, chinese):
            for key in (
                "claude.disableBypassPermissions",
                "claude.capabilities",
                "claude.skillSettings",
                "claude.worktree",
                "claude.advanced",
                "claude.feedbackSurveyRate",
                "claude.diffTool",
                "claude.teammateDefaultModel",
                "claude.autoConnectIde",
                "claude.autoInstallIdeExtension",
                "claude.externalEditorContext",
                "claude.permissionExplainer",
                "claude.disableAgentView",
                "claude.disableArtifact",
                "claude.skipWebFetchPreflight",
            ):
                self.assertIn(f'"{key}":', text)

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
        self.assertIn('item.kind === "password"', relay)
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
            password.index("await native.clearRelayPassword(selected.id)"),
        )
        self.assertIn('kind: "password"', password)
        self.assertIn('kind: "password" }, "relay_accounts")', password)
        self.assertIn('translate("relay.retryCleanup")', relay)

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
            self.assertIn("secondaryCellKeys?: ReadonlyArray<string>;", spec)
        self.assertIn("alternatingRows = false", self.native_controls)
        self.assertIn("secondaryCellKeys = []", self.native_controls)
        self.assertIn("secondaryCellKeys,", self.native_controls)
        # The fetched-model picker is now a native modal leaf, so its old
        # React table no longer belongs to the shared window tree.
        self.assertEqual(self.ui.count("<NativeTable"), 8)
        self.assertEqual(self.ui.count("alternatingRows"), 1)
        self.assertIn("selectedKey={selectedRoute ?? \"\"} alternatingRows", self.ui)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", mac_native)
        self.assertIn(
            "_tableView.usesAlternatingRowBackgroundColors = newViewProps.alternatingRows;",
            mac_native,
        )
        self.assertIn("props.alternatingRows.value_or(false)", windows_native)
        self.assertIn('rowKey + "\\x1f" + std::to_string(columnIndex)', mac_native)
        self.assertIn("static_cast<size_t>(row) >= viewProps.rowKeys.size()", mac_native)
        self.assertIn("static_cast<size_t>(columnIndex) >= columnCount", mac_native)
        self.assertIn("label.textColor = disabled || secondary", mac_native)
        self.assertIn('props.rowKeys[row_index] + "\\x1f" + std::to_string(column_index)', windows_native)
        self.assertIn("if (disabled || secondary) cell.Foreground(SecondaryTextBrush());", windows_native)

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
        self.assertIn("column.minWidth = 96;", mac_native)
        self.assertIn("column.maxWidth = CGFLOAT_MAX;", mac_native)
        self.assertIn("_scrollView.hasHorizontalScroller = needsHorizontalScroller;", mac_native)
        self.assertIn("NSTableViewNoColumnAutoresizing", mac_native)
        self.assertIn("std::vector<CGFloat> _requestedColumnWidths;", mac_native)
        self.assertIn("NSTableViewColumnDidResizeNotification", mac_native)
        self.assertIn("MAX(NSWidth(visibleBounds), contentWidth)", mac_native)
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
        self.assertIn("const sizedStyle = [{ minWidth: nativeCheckboxMinimumWidth(label) }, style];", self.native_controls)
        self.assertIn("style={sizedStyle}", self.native_controls)
        self.assertIn('runtimeBooleanSlot: { flex: 1, minWidth: 0, minHeight: 26', self.ui)
        self.assertIn('runtimeBooleanControl: { alignSelf: "flex-start", minHeight: 26 }', self.ui)
        self.assertNotIn("runtimeBooleanSlot: { width: 100", self.ui)

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
        self.assertEqual(54, len(keys))
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIn(f"  {key}: {{ label:", localized)
        for category in ("Timeouts", "Recovery", "Web Search", "Vision Bridge", "Fallback", "Computer Facade", "Logs", "Network", "Service"):
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
        self.assertIn("values: Array<string | AssistantSettingOption>", self.ui)
        self.assertIn("const option = options[nativeEvent.index];", self.ui)
        self.assertIn("if (option) onSelect(option.value);", self.ui)
        self.assertIn("function SegmentedField", self.ui)
        self.assertIn("assistantSettingOptions(values, translate)", self.ui)
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
        self.assertIn("const acceptedSnapshotRevision = useRef<number>(-1);", self.ui)
        self.assertIn("if (next.revision < acceptedSnapshotRevision.current) return;", self.ui)
        self.assertIn("Same-revision snapshots remain", self.ui)
        self.assertNotIn("acceptedSnapshotFingerprint", self.ui)

    def test_background_health_refresh_does_not_republish_an_unchanged_snapshot(self) -> None:
        self.assertIn("const refreshSnapshot = useCallback(async (publishUnchanged = true)", self.ui)
        self.assertIn("if (publishUnchanged || next.revision !== acceptedSnapshotRevision.current) receiveSnapshot(next);", self.ui)
        self.assertIn("return await refreshSnapshot(!background);", self.ui)

    def test_settings_disk_polling_ignores_unrelated_snapshot_revisions(self) -> None:
        self.assertIn("const latestSnapshot = useRef<CoreSnapshot | undefined>(snapshot);", self.ui)
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

    def test_claude_settings_hydrates_the_public_gateway_and_uses_a_localized_permission_picker(self) -> None:
        for marker in (
            "function claudeDeploymentFromSnapshot(snapshot: CoreSnapshot | undefined)",
            "deployment: ClaudeDeploymentDraft",
            "onDeploymentChange(key, value)",
            "const next = { ...(claudeDeploymentDraftRef.current ?? claudeDeploymentFromSnapshot(snapshot)), [key]: value };",
            'enqueueDispatch("patch_deployment", { [key]: value }, "claude")',
            'translate("settings.claudeUnavailable")',
            "function claudePermissionLabel(value: string, translate: Translate)",
            "function claudePermissionMode(label: string, translate: Translate)",
            '<PickerField label={translate("claude.permissions")}',
            'values={claudePermissionLabels(stringValue(permissions.defaultMode, "default"), translate)}',
            'const defaultMode = claudePermissionMode(permissionsLabel, translate); if (defaultMode) dispatch("patch", { permissions: { defaultMode } });',
            'const CLAUDE_PERMISSION_MODES = ["default", "manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "delegate"];',
            'translate("claude.permission.unknown", { value })',
            'translate("claude.sandboxFailIfUnavailable")',
            'translate("claude.allowMachLookup")',
            'translate("claude.askUserQuestionTimeout")',
            'translate("claude.workflowSize")',
            'stringValue(settings.workflowSizeGuideline, "unrestricted")',
            'containsPrivateMarker(sandbox.excludedCommands) ? <InfoPair',
            "const CLAUDE_BUILTIN_THEMES =",
            "function claudeThemeValues(value: unknown): string[]",
            "values={claudeThemeValues(settings.theme)}",
        ):
            self.assert_ui_has(marker)

    def test_claude_settings_expose_official_structured_memory_attribution_mode_vim_and_voice_controls(self) -> None:
        for marker in (
            'domain="claude" field="auto_memory_directory"',
            'settings.autoMemoryDirectoryConfigured',
            'attribution: { commit }',
            'attribution: { pr }',
            'attribution: { sessionUrl }',
            'autoMode: { classifyAllShell }',
            'autoMode: { environment: splitLines(environment) }',
            'autoMode: { allow: splitLines(allow) }',
            'autoMode: { soft_deny: splitLines(soft_deny) }',
            'autoMode: { hard_deny: splitLines(hard_deny) }',
            'translate("claude.autoModeDefaultsHint")',
            'autoUpdatesChannel',
            'vimInsertModeRemaps: vimInsertRemaps(remaps)',
            'voice: { enabled }',
            'voice: { mode:',
            'voice: { autoSubmit }',
            'function vimInsertRemapLines',
            'function vimInsertRemaps',
            'function claudeVoiceModeLabel',
            'function claudeVoiceMode',
        ):
            self.assert_ui_has(marker)
        self.assertIn('translate("codex.network")', self.ui)
        self.assertNotIn('translate("claude.network")', self.ui)

    def test_runtime_form_rows_keep_labels_and_controls_aligned_when_reflowed(self) -> None:
        for marker in (
            "runtimeInputRow:",
            "runtimeFieldLabel:",
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
        self.assertIn('route === "runtime-settings" ? translate("common.saveAndApply")', self.ui)

    def test_provider_workspace_fetches_multiplier_once_without_usage_polling(self) -> None:
        self.assertNotIn('providers.refresh_billing', self.ui)
        self.assertIn('providers.refresh_multiplier', self.ui)
        self.assertIn('multiplierRefreshStarted.current = true', self.ui)
        self.assertNotIn('billingRefreshMinutes', self.ui)
        self.assertNotIn('billingUsageValue(', self.ui)

    def test_model_inspector_matches_legacy_breadcrumb_and_compact_billing_surface(self) -> None:
        for marker in (
            'NativeButton title={providerLabel} link',
            'billingMultiplierValue(model.multiplier, translate)',
            '`${translate("providers.balance")}: ${translate("providers.billingUnavailable")}  ${translate("providers.multiplier")}: ${billingMultiplierValue(model.multiplier, translate)}`',
            '<Text numberOfLines={1} style={styles.billingSummaryText}>{billingSummary}</Text>',
        ):
            self.assert_ui_has(marker)

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
            'const fetchKeyOptions = apiKeyNames.map((name) => ({ value: name, label: apiKeyDisplayName(name, translate) }));',
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

    def test_provider_inspector_keeps_the_legacy_provider_form_and_return_link(self) -> None:
        """The AppKit editor uses a 96pt, left-aligned provider form and source-model return link."""
        for marker in (
            'const [providerSourceModel, setProviderSourceModel] = useState<string>();',
            'label={translate("providers.baseUrl")} labelWidth={96} labelAlign="left"',
            'label={translate("providers.providerName")} labelWidth={96} labelAlign="left"',
            'label={translate("providers.keyName")} labelWidth={64} labelAlign="left"',
            'NativeSecretField plainText autoCommit label={translate("common.apiKey")} hint={selectedKeyConfigured',
            'title={translate("providers.backToModel", { model: sourceModelLabel })} link',
            'providerEditorHeader:',
            'providerEditorSection:',
            'providerKeysHeading:',
            'NativeSecretField plainText autoCommit label={translate("common.apiKey")}',
        ):
            self.assert_ui_has(marker)

    def test_logs_keep_the_legacy_dense_toolbar_and_table_frame(self) -> None:
        for marker in (
            "function LogsWorkspace(",
            "<WindowTabs nativeRef={tabsRef} values={tabOptions} selected={selected}",
            "function renderLogRecord(",
            "function logColumns(",
            '<NativeTable columns={columns.map(({ label, width }) => ({ label, width }))}',
            "translate(\"logs.failed\")",
            "const proxyPrefix = detail.match",
            'translate("logs.duration")',
            'translate("logs.tokenCount")',
            "const columns = logColumns(selected, translate);",
            "rows={rows.map",
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
            '{ label: translate("providers.publicModel"), width: 126, value: (row) => row.model }',
            '{ label: translate("providers.upstream"), width: 126, value: (row) => row.upstreamModel }',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("function conditionalUpstreamLogModel(", self.ui)

    def test_logs_project_recovery_route_identity_and_localize_route_diagnostics(self) -> None:
        for marker in (
            "function recoveryStatusLabel(",
            "function recoveryDetailLabel(",
            "function routeTraceServiceTierLabel(",
            'return translate("logs.routeEvent.routeEvent");',
            "let source = logTitle(tab, translate);",
            '{ label: translate("common.provider"), width: 104, value: (row) => row.provider },',
            '{ label: translate("logs.apiKeyName"), width: 100, value: (row) => row.apiKeyName },',
            'model: model || recoveryFallback,',
            'upstreamModel: compactUpstreamLogModel(upstreamModel) || recoveryFallback,',
        ):
            self.assert_ui_has(marker)

    def test_menu_logs_do_not_repeat_actions_as_detail(self) -> None:
        self.assertIn(
            'if (tab === "menu") return [\n'
            '    time,\n'
            '    { label: translate("logs.action"), width: 112, value: (row) => row.action },\n'
            '    status,\n'
            '  ];',
            self.ui,
        )

    def test_route_trace_logs_use_diagnostic_columns_and_compact_detail(self) -> None:
        self.assertIn(
            'if (tab === "route-trace") return [\n'
            '    { ...time, width: 154 },\n'
            '    { label: translate("logs.event"), width: 142, value: (row) => row.event },\n'
            '    { label: translate("providers.publicModel"), width: 130, value: (row) => row.model },\n'
            '    { label: translate("providers.upstream"), width: 130, value: (row) => row.upstreamModel },\n'
            '    { label: translate("common.provider"), width: 104, value: (row) => row.provider },\n'
            '    { ...detail, width: 192 },\n'
            '  ];',
            self.ui,
        )
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
            'return details.join(" · ");',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn("logs.routeTrace.otherDetail", self.ui)

    def test_relay_manager_uses_url_wizard_and_checks_sessions_on_demand(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        self.assertIn("const detected = await detectRelayType();", relay)
        self.assertIn("refreshResources: (accountId: string) => Promise<void>;", relay)
        self.assertIn("await refreshResources(account.id);", relay)
        self.assertIn('title={translate("relay.importSelected")}', relay)
        self.assertIn("const account = await addAccount(detected, origin.trim(), rememberPasswordRef.current);", relay)
        self.assertIn('title={translate("relay.next")}', relay)
        self.assertIn('title={translate("relay.importSelected")}', relay)
        self.assertNotIn("NativePicker", relay)
        self.assertNotIn("const restorationAttempts", relay)
        self.assertNotIn("restoreSession(account, true)", relay)

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
            'header: { minHeight: 68, paddingHorizontal: 18, paddingVertical: 12, flexDirection: "row", flexWrap: "wrap"',
            'field: { minHeight: 32, flexDirection: "row", flexWrap: "wrap"',
            'detailActions: { minHeight: 36, marginTop: 4, flexDirection: "row", flexWrap: "wrap"',
            'pendingCleanupList: { maxHeight: 116',
            '<ScrollView style={styles.pendingCleanupList} contentContainerStyle={styles.pendingCleanupListContent}>',
            'pendingCleanup: { minHeight: 30, flexDirection: "row", flexWrap: "wrap"',
        ):
            self.assertIn(marker, relay, marker)

    def test_provider_table_columns_fit_the_fixed_provider_pane(self) -> None:
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 140 }, { label: translate("providers.models"), width: 48 }]}')
        self.assert_ui_has('providerListPane: { width: 190, minWidth: 190, maxWidth: 190')
        self.assert_ui_has('columns={[{ label: translate("providers.model"), width: 118 }, { label: translate("providers.upstream"), width: 130 }, { label: translate("providers.balance"), width: 112 }, { label: translate("providers.apiKeyOrder"), width: 104 }]}')
        self.assert_ui_has('providerKeysHeader: { minHeight: 26, flexDirection: "row"')
        self.assert_ui_has('providerKeyGrid: { flex: 1, minHeight: 142, flexDirection: "row", alignItems: "flex-start", gap: 12 }')
        self.assert_ui_has('providerKeyTable: { width: 138, minWidth: 138, maxWidth: 138, height: 142, minHeight: 142')

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
            'webdavPasswordInput: { width: "100%", minHeight: 30 }',
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
            'input: { width: "100%", minHeight: 30',
            'formRow: { width: "100%", minHeight: 30',
        ):
            self.assert_ui_has(marker)
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
        for marker in (
            'translate("settings.subtitle")',
            'translate("settings.rawDraftHint")',
            'translate("settings.synchronized")',
        ):
            self.assertNotIn(marker, self.ui)
        for marker in (
            'translate("settings.diskChangedTitle")',
            'message: translate("settings.diskChangedBody")',
            'confirmLabel: translate("settings.useDisk")',
        ):
            self.assert_ui_has(marker)

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
            '"toggle-autostart", "separator"',
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
