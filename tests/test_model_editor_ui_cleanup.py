from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "mac_menu" / "Sources"


class ModelEditorUICleanupTests(unittest.TestCase):
    def test_runtime_map_has_no_remaining_editor_code(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SOURCES.glob("ModelConfigEditor*.swift"))
        ).lower()
        self.assertNotIn("runtimemap", source)
        self.assertNotIn("runtime map", source)
        self.assertNotIn("runtimedeployment", source)
        self.assertNotIn("refreshruntimemap", source)

    def test_native_log_window_replaces_html_log_pages(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SOURCES.glob("*.swift"))
        )
        logs = (SOURCES / "LogWindowController.swift").read_text(encoding="utf-8")

        self.assertIn("NSTabView", logs)
        self.assertIn("case requests", logs)
        self.assertIn("case service", logs)
        self.assertIn("case menu", logs)
        self.assertIn("case configWatch", logs)
        self.assertIn("case routeTrace", logs)
        self.assertIn("case recovery", logs)
        self.assertIn("case remoteUsage", logs)
        self.assertIn('process.arguments = [service, "remote-usage-logs"]', logs)
        self.assertNotIn("visualLogHTML", source)
        self.assertNotIn("writeCommandToVisualFile", source)
        self.assertNotIn("NSWorkspace.shared.open", source)

    def test_native_controls_use_text_buttons_only(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SOURCES.glob("*.swift"))
        )

        self.assertNotIn("iconButton(", source)
        self.assertNotIn("imagePosition = .imageOnly", source)
        self.assertNotIn("NSButton(image:", source)
        self.assertNotIn("systemSymbolName", source)

    def test_editor_actions_use_short_text_or_textual_symbols(self) -> None:
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        layout = (SOURCES / "ModelConfigEditorLayout.swift").read_text(encoding="utf-8")
        routes = (SOURCES / "ModelConfigEditorRoutes.swift").read_text(encoding="utf-8")
        probes = (SOURCES / "ModelConfigEditorProbes.swift").read_text(encoding="utf-8")

        self.assertRegex(
            layout,
            r'title: "\+",\s+toolTip: "Add provider",\s+accessibilityLabel: "Add provider"',
        )
        for tooltip, accessibility_label in (
            ("Add API key", "Add API key"),
            ("Remove API key", "Remove API key"),
            ("Remove provider", "Remove provider"),
            ("Add model", "Add model"),
            ("Remove model", "Remove model"),
            ("Move route up", "Move route up"),
            ("Move route down", "Move route down"),
        ):
            self.assertIn(f'toolTip: "{tooltip}", accessibilityLabel: "{accessibility_label}"', core)

        self.assertIn('let up = NSButton(title: "↑"', routes)
        self.assertIn('let down = NSButton(title: "↓"', routes)
        self.assertIn('button.setAccessibilityLabel(accessibilityLabel)', routes)
        self.assertIn('textButton(title: "+", toolTip: "Add selected models", accessibilityLabel: "Add selected models")', probes)
        self.assertIn('popup.addItem(withTitle: "Import From")', core)
        self.assertNotIn('popup.lastItem?.isEnabled = false', core)
        self.assertIn('popup.addItem(withTitle: "Current Codex")', core)
        self.assertIn('popup.addItem(withTitle: "Configuration File…")', core)
        self.assertIn('popup.addItem(withTitle: "CC Switch / New API Link…")', core)
        self.assertNotIn("configureCodex", core)
        self.assertIn('title: "⧉", toolTip: "Duplicate model", accessibilityLabel: "Duplicate model"', core)
        self.assertNotIn('textButton(title: "Add")', core + layout)
        self.assertNotIn('textButton(title: "Delete")', core)
        self.assertNotIn('textButton(title: "Earlier")', core)
        self.assertNotIn('textButton(title: "Later")', core)

    def test_codex_settings_replaces_the_legacy_model_chooser(self) -> None:
        core = (SOURCES / "AppDelegateCore.swift").read_text(encoding="utf-8")
        actions = (SOURCES / "AppDelegateActions.swift").read_text(encoding="utf-8")
        editor = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SOURCES.glob("ModelConfigEditor*.swift"))
        )

        self.assertIn('menuItem("Codex Settings..."', core)
        self.assertIn("CodexConfigDialogController", actions)
        self.assertNotIn("CodexModelChooserDialogController", core + actions)
        self.assertNotIn("showCodexModelChooser", actions)
        self.assertNotIn("Choose Codex Model", core + actions)
        self.assertNotIn("Set Codex", editor)
        self.assertNotIn("configureCodex", editor)

    def test_runtime_settings_has_no_embedded_package_import_or_export(self) -> None:
        runtime = (SOURCES / "RuntimeSettingsDialog.swift").read_text(encoding="utf-8")
        actions = (SOURCES / "AppDelegateActions.swift").read_text(encoding="utf-8")

        self.assertNotIn("importHandler", runtime)
        self.assertNotIn("exportHandler", runtime)
        self.assertNotIn("importButton", runtime)
        self.assertNotIn("exportButton", runtime)
        self.assertNotIn("importRuntimeSettingsConfigurationPackage", actions)
        self.assertNotIn("exportRuntimeSettingsConfigurationPackageForDialog", actions)

    def test_routes_and_probe_state_share_the_editor_rows(self) -> None:
        tables = (SOURCES / "ModelConfigEditorTables.swift").read_text(encoding="utf-8")
        routes = (SOURCES / "ModelConfigEditorRoutes.swift").read_text(encoding="utf-8")
        networking = (SOURCES / "ModelConfigEditorNetworking.swift").read_text(encoding="utf-8")
        probes = (SOURCES / "ModelConfigEditorProbes.swift").read_text(encoding="utf-8")

        self.assertNotIn("modelProbePresentation(providerIndex: providerIndex, modelIndex: row)", tables)
        self.assertNotIn("compactRouteDisplayStatus", tables)
        self.assertIn("routeProbeTooltip(route)", tables)
        self.assertNotIn("routeStatusColumnIdentifier", tables + routes)
        self.assertIn("func setModelProbePresentation", networking)
        self.assertIn("func invalidateModelProbePresentation", networking)
        self.assertIn("func invalidateProviderProbePresentations", networking)
        self.assertIn("presentProbeStatus(", probes)
        self.assertIn("refreshProviderBilling()", probes.split("func probeModelAvailability", 1)[1].split("func recommendedUpstreamApiModes", 1)[0])
        self.assertNotIn("setEditorStatus(\"Full probe", probes)

    def test_billing_is_per_deployment_and_keeps_a_saved_snapshot_while_editing(self) -> None:
        models = (SOURCES / "CommonModels.swift").read_text(encoding="utf-8")
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        networking = (SOURCES / "ModelConfigEditorNetworking.swift").read_text(encoding="utf-8")
        persistence = (SOURCES / "ModelConfigEditorPersistence.swift").read_text(encoding="utf-8")
        tables = (SOURCES / "ModelConfigEditorTables.swift").read_text(encoding="utf-8")

        self.assertIn('case deploymentID = "deployment_id"', models)
        self.assertIn("struct ProviderBillingModelIdentity", models)
        self.assertIn("var models: [ProviderBillingModelIdentity]", models)
        self.assertIn("$0.deploymentID == deploymentID", networking)
        self.assertIn("func billingSnapshotNotice", networking)
        self.assertIn("Saved configuration snapshot", networking)
        self.assertIn("func compactBillingAmountText", networking)
        self.assertIn('return "N/A"', networking)
        self.assertNotIn("func compactMultiplierRange", networking)
        self.assertNotIn("func compactBalanceRange", networking)
        self.assertNotIn("let total = balances.reduce", networking)
        self.assertIn("Int(exactly: value).map(String.init)", networking)
        self.assertNotIn('guard !hasPendingChanges else { return "Apply changes" }', networking)
        self.assertNotIn('self.providerBilling = nil', networking)
        self.assertNotIn('"Apply to refresh billing"', tables)
        self.assertIn('timeoutSeconds: 15', persistence)
        refresh = networking.split("func refreshProviderBilling()", 1)[1].split("func billingProvider", 1)[0]
        self.assertIn("reloadModelBillingColumnPreservingViewport()", refresh)
        self.assertNotIn("modelTableView.reloadData()", refresh)
        self.assertIn("func configureProviderBillingRefreshTimer", networking)
        self.assertIn("LITELLM_MENU_BALANCE_REFRESH_MINUTES", networking)
        self.assertIn("RunLoop.main.add(timer, forMode: .common)", networking)
        self.assertIn("func refreshVisibleModelBillingDetail", networking)
        self.assertEqual(refresh.count("refreshVisibleModelBillingDetail()"), 2)
        self.assertIn("configureProviderBillingRefreshTimer(refreshImmediately: true)", core)
        self.assertNotIn("refreshProviderBilling()", persistence.split("func applyRuntimeConfigAfterSave", 1)[1].split("private func helperFailureMessage", 1)[0])

    def test_probe_results_never_change_model_enabled_state(self) -> None:
        probes = (SOURCES / "ModelConfigEditorProbes.swift").read_text(encoding="utf-8")
        outcome = probes.split("func applyModelAvailabilityProbeOutcome", 1)[1].split(
            "func showFetchedModelChooser", 1
        )[0]
        protocol_apply = probes.split("func applyRecommendedProtocolOrder", 1)[1].split(
            "func setModelCandidateFetchState", 1
        )[0]

        self.assertNotIn("model.modelEnabled =", outcome)
        self.assertNotIn("model.enabled =", outcome)
        self.assertNotIn("markPendingChanges", outcome)
        self.assertIn('presentProbeStatus("Available"', outcome)
        self.assertIn('"Unavailable"', outcome)
        self.assertNotIn("model.modelEnabled =", protocol_apply)
        self.assertNotIn("model.enabled =", protocol_apply)
        self.assertNotIn("func applyFullProbeSelection", probes)
        self.assertNotIn("func markModelAsImageGenerationEndpoint", probes)

    def test_probe_status_is_single_line_and_apply_stays_disabled_in_flight(self) -> None:
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        layout = (SOURCES / "ModelConfigEditorLayout.swift").read_text(encoding="utf-8")
        networking = (SOURCES / "ModelConfigEditorNetworking.swift").read_text(encoding="utf-8")
        persistence = (SOURCES / "ModelConfigEditorPersistence.swift").read_text(encoding="utf-8")

        probe_label = core.split("lazy var modelProbeStatusLabel", 1)[1].split("lazy var modelBillingStatusLabel", 1)[0]
        probe_row = layout.split("func modelEnabledRow()", 1)[1].split("func modelBreadcrumbView", 1)[0]
        apply_state = persistence.split("func setRuntimeApplyInFlight", 1)[1].split("func cancelRuntimeApplyInFlight", 1)[0]
        self.assertIn('NSTextField(labelWithString: "")', probe_label)
        self.assertIn("label.usesSingleLineMode = true", probe_label)
        self.assertIn("label.maximumNumberOfLines = 1", probe_label)
        self.assertIn("row.addArrangedSubview(modelProbeStatusLabel)", probe_row)
        self.assertNotIn("content.addArrangedSubview(modelProbeStatusLabel)", probe_row)
        self.assertIn('"\\(presentation.summary)\\n\\n\\(presentation.detail)"', networking)
        self.assertIn("!applying && hasPendingChanges", apply_state)

    def test_provider_fields_fill_available_width_and_dock_tracks_settings_windows(self) -> None:
        layout = (SOURCES / "ModelConfigEditorLayout.swift").read_text(encoding="utf-8")
        presentation = (SOURCES / "SettingsWindowPresentation.swift").read_text(encoding="utf-8")
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        crud = (SOURCES / "ModelConfigEditorCrud.swift").read_text(encoding="utf-8")

        make_field = layout.split("func makeTextField", 1)[1].split("func textButton", 1)[0]
        provider_keys = layout.split("func providerKeysEditor", 1)[1].split("func providerEnabledRow", 1)[0]
        self.assertNotIn("constraint(equalToConstant: preferredWidth)", make_field)
        self.assertIn("field.setContentHuggingPriority(.defaultLow", make_field)
        self.assertIn("keyFields.alignment = .width", provider_keys)
        self.assertIn("keyFields.trailingAnchor.constraint(equalTo: keyRow.trailingAnchor)", provider_keys)
        self.assertIn("NSApp.setActivationPolicy(.regular)", presentation)
        self.assertIn("presentedSettingsWindows.insert(ObjectIdentifier(window))", presentation)
        self.assertIn("presentedSettingsWindows.remove(ObjectIdentifier(window))", presentation)
        self.assertIn("if presentedSettingsWindows.isEmpty", presentation)
        self.assertNotIn("DispatchQueue.main.async", presentation)
        self.assertIn("beginSettingsWindowPresentation(window)", core)
        self.assertIn("endSettingsWindowPresentation(window)", crud)

    def test_fetched_models_infer_protocol_and_automatic_probe_saves_order(self) -> None:
        probes = (SOURCES / "ModelConfigEditorProbes.swift").read_text(encoding="utf-8")
        fetched_add = probes.split("func addFetchedModels", 1)[1].split(
            "func runAutomaticFullProbes", 1
        )[0]
        automatic_branch = probes.split("if automatic {", 1)[1].split(
            "} else {", 1
        )[0]

        self.assertIn("inferredPreferredUpstreamApiMode(", fetched_add)
        self.assertIn("upstreamApiMode: initialApiMode", fetched_add)
        self.assertIn("model.upstreamApiMode = initialApiMode", fetched_add)
        self.assertIn("model.supportedUpstreamApiModes = [initialApiMode]", fetched_add)
        self.assertIn("self.applyRecommendedProtocolOrder(", automatic_branch)

    def test_external_import_accepts_cc_switch_sql_and_keeps_links_out_of_arguments(self) -> None:
        actions = (SOURCES / "ModelConfigEditorActions.swift").read_text(encoding="utf-8")
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        persistence = (SOURCES / "ModelConfigEditorPersistence.swift").read_text(encoding="utf-8")

        self.assertIn("func importSourceSelected(_ sender: NSPopUpButton)", actions)
        self.assertNotIn("func importExternalConfiguration()", actions)
        self.assertIn("defer { sender.selectItem(at: 0) }", actions)
        self.assertIn("importSourcePopupButton.isEnabled = !inFlight", actions)
        self.assertIn('popup.addItem(withTitle: "Import From")', core)
        self.assertIn('.init(filenameExtension: "sql")!', actions)
        self.assertIn('NSSecureTextField', actions)
        self.assertIn('arguments: ["--link-stdin"], standardInput: input', actions)
        self.assertIn('standardInput: Data? = nil', persistence)
        self.assertIn('process.standardInput = stdinPipe', persistence)
        self.assertIn("timeoutSeconds: 60", persistence)
        self.assertIn("externalImportOutputLimitBytes", persistence)
        self.assertIn("DispatchQueue.global(qos: .userInitiated).async", actions)
        self.assertIn("setExternalImportInFlight(true)", actions)

    def test_routes_use_stable_configuration_columns_without_a_status_column(self) -> None:
        routes = (SOURCES / "ModelConfigEditorRoutes.swift").read_text(encoding="utf-8")
        tables = (SOURCES / "ModelConfigEditorTables.swift").read_text(encoding="utf-8")
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        layout = (SOURCES / "ModelConfigEditorLayout.swift").read_text(encoding="utf-8")

        self.assertNotIn("routeStatusColumnIdentifier", core + tables + layout)
        self.assertNotIn("RouteTableRow", core + tables + routes)
        self.assertNotIn("RouteModelGroupRow", core + tables + routes)
        self.assertIn("func routeTableRows()", tables + routes)
        self.assertIn("func routeStartsModelGroup(atTableRow row: Int)", routes)
        self.assertIn("return row == 0 || rows[row - 1].publicModel != rows[row].publicModel", routes)
        self.assertIn('text = routeStartsModelGroup(atTableRow: row) ? route.publicModel : ""', tables)
        self.assertNotIn('text = "Deployment"', tables)
        self.assertNotIn("leadingInset: routeDetailIndent", tables)
        self.assertNotIn("firstRouteDeploymentTableRowIndex", routes)
        self.assertIn("func tableView(_ tableView: NSTableView, isGroupRow row: Int) -> Bool {\n        false\n    }", tables)
        self.assertNotIn("enabledCount", routes + tables + core)
        self.assertNotIn("disabledCount", routes + tables + core)
        self.assertIn('modelColumn.title = "Model"', layout)
        self.assertIn('orderColumn.title = "Order"', layout)
        self.assertIn('providerKeyColumn.title = "Provider / Key"', layout)
        self.assertIn('upstreamColumn.title = "Upstream"', layout)
        self.assertIn('providersWorkspace?.isHidden = routesMode', routes)
        self.assertIn('routesWorkspace?.isHidden = !routesMode', routes)
        self.assertIn('let routesWorkspace = NSView()', layout)
        self.assertIn('routeScrollView.hasHorizontalScroller = false', layout)
        self.assertIn('routeTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle', layout)
        self.assertIn('routeTableView.usesAlternatingRowBackgroundColors = true', layout)
        self.assertNotIn('routeTableView.action = #selector(routeTableClicked(_:))', layout)
        self.assertIn('pendingRouteSelectionIdentity', core + tables + routes)
        self.assertIn('routeSelectionIdentity(atTableRow:', tables)
        self.assertIn('scrollSelectionIntoView: Bool = false', routes)
        self.assertIn('restoreRouteTableViewport', routes)
        self.assertIn('modelColumn.width = 170', layout)
        self.assertIn('orderColumn.width = 56', layout)
        self.assertIn('providerKeyColumn.width = 130', layout)
        self.assertIn('upstreamColumn.width = 164', layout)
        self.assertIn('routeStack.widthAnchor.constraint(greaterThanOrEqualToConstant: 560)', layout)
        self.assertIn('modelTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle', layout)
        self.assertIn('modelScrollView.hasHorizontalScroller = false', layout)
        self.assertIn('apiKeyOrderColumn.resizingMask = .autoresizingMask', layout)
        actions = (SOURCES / "ModelConfigEditorActions.swift").read_text(encoding="utf-8")
        self.assertNotIn("moveSharedDetailEditor", routes)
        self.assertIn('editorWorkspaceStack.addArrangedSubview(modeWorkspaceColumn)', layout)
        self.assertIn('modeWorkspaceColumn.addArrangedSubview(modeStack)', layout)
        self.assertIn('modeWorkspaceColumn.addArrangedSubview(modeWorkspaceHost)', layout)
        self.assertIn('editorWorkspaceStack.addArrangedSubview(detailScrollView)', layout)
        self.assertNotIn('editorWorkspaceStack.addArrangedSubview(modeWorkspaceHost)', layout)
        self.assertIn('modeWorkspaceHost.addSubview(providersWorkspace)', layout)
        self.assertIn('modeWorkspaceHost.addSubview(routesWorkspace)', layout)
        self.assertNotIn("routeInspectorHost", core + layout + routes + actions)
        self.assertNotIn("routeDeploymentInspector", core + layout + routes + actions)
        self.assertNotIn("routeProviderInspector", core + layout + routes + actions)
        self.assertIn('compactModelFormRow("Provider", modelProviderPopupButton', layout)
        self.assertIn("func modelBreadcrumbView()", layout)
        self.assertIn("modelBreadcrumbProviderButton", core + layout + tables)
        self.assertIn("modelBreadcrumbModelLabel", core + layout + tables)
        self.assertIn("@objc func modelBreadcrumbProviderClicked", actions)
        model_to_provider = actions.split("@objc func modelBreadcrumbProviderClicked", 1)[1].split("@objc func providerReturnToModelClicked", 1)[0]
        self.assertIn("providerEditorSourceModel = target", model_to_provider)
        self.assertIn("showProvider(at: current.provider, preservingModelSource: true)", model_to_provider)
        self.assertNotIn("viewMode = .providers", model_to_provider)
        self.assertIn("func providerEditorHeaderView()", layout)
        self.assertIn("providerReturnToModelButton", core + layout + tables + actions)
        self.assertIn("@objc func providerReturnToModelClicked", actions)
        self.assertIn("class NavigationLinkButton", core)
        self.assertIn(".mouseEnteredAndExited", core)
        self.assertIn(".pointingHand", core)
        self.assertIn(".underlineStyle", core)
        self.assertNotIn("providerBreadcrumb", core + layout + tables + actions)
        self.assertIn('providerEditorTitleLabel.stringValue = "Provider: \\(providers[providerIndex].displayName)"', tables)
        self.assertIn('providerReturnToModelButton.setNavigationTitle("Back to model \\(modelName)")', tables)
        self.assertIn("func modelProviderSelectionChanged", actions)
        self.assertIn('movedModel.provider = destinationProviderName', actions)
        self.assertIn('Move this deployment to another provider', core)
        self.assertIn('modelProviderPopupButton.lastItem?.representedObject = provider.editorID', routes)
        self.assertNotIn("routeDeploymentFormChanged", actions)
        self.assertNotIn("routeProviderFormChanged", actions)
        self.assertNotIn("renderRouteProviderInspector", routes)

    def test_editor_text_changes_update_dependent_controls_immediately(self) -> None:
        actions = (SOURCES / "ModelConfigEditorActions.swift").read_text(encoding="utf-8")
        layout = (SOURCES / "ModelConfigEditorLayout.swift").read_text(encoding="utf-8")
        probes = (SOURCES / "ModelConfigEditorProbes.swift").read_text(encoding="utf-8")

        live_sync = probes.split("func synchronizeLiveEditorDraft()", 1)[1].split("func controlTextDidEndEditing", 1)[0]
        self.assertIn("synchronizeLiveEditorDraft()", probes.split("func controlTextDidChange", 1)[1].split("func synchronizeLiveEditorDraft", 1)[0])
        self.assertIn("commitEditor()", live_sync)
        self.assertIn("isRenderingSelection = true", live_sync)
        self.assertIn("renderModelBreadcrumb(providerIndex: providerIndex, modelIndex: modelIndex)", actions)
        self.assertLess(layout.index('formRow("Base URL", providerApiBaseField)'), layout.index('formRow("Provider name", providerNameField)'))
        self.assertIn("autofillProviderNameFromBaseURL()", probes)
        self.assertIn("func suggestedProviderName(fromBaseURL", actions)
        self.assertIn('candidate = "\\(base) (\\(suffix))"', actions)
        autofill = actions.split("func autofillProviderNameFromBaseURL()", 1)[1].split("func reloadImportedProviderDraft", 1)[0]
        self.assertIn("providerNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty", autofill)
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        self.assertIn("configurationBaselineProviders", core)
        self.assertIn("captureConfigurationBaseline()", core)
        self.assertIn("func refreshPendingChanges", layout)
        self.assertIn("providers != configurationBaselineProviders", layout)
        self.assertIn("sourceDocument != configurationBaselineDocument", layout)
        self.assertIn("providerNameAutofillProviderID", core)
        self.assertIn("providerNameAutofillProviderID = nil", probes)

    def test_billing_is_shown_per_model_not_per_provider(self) -> None:
        """A provider may have unrelated accounts, so billing belongs to deployments."""
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        layout = (SOURCES / "ModelConfigEditorLayout.swift").read_text(encoding="utf-8")
        tables = (SOURCES / "ModelConfigEditorTables.swift").read_text(encoding="utf-8")
        networking = (SOURCES / "ModelConfigEditorNetworking.swift").read_text(encoding="utf-8")

        self.assertNotIn("providerBillingColumnIdentifier", core)
        self.assertNotIn("providerBillingColumnIdentifier", tables)
        self.assertIn("text = provider.displayName", tables)
        self.assertNotIn('let billingColumn = NSTableColumn(identifier: providerBillingColumnIdentifier)', layout)
        self.assertNotIn('providerBillingSummaryPanel()', layout)
        self.assertIn('let billingColumn = NSTableColumn(identifier: modelBillingColumnIdentifier)', layout)
        self.assertIn('billingColumn.title = "Balance"', layout)
        self.assertIn('let upstreamColumn = NSTableColumn(identifier: modelUpstreamColumnIdentifier)', layout)
        self.assertIn('width: 1052, height: 600', layout)
        self.assertIn('window.minSize = NSSize(width: 1052, height: 560)', layout)
        self.assertIn('window.level = .normal', layout)
        self.assertNotIn('window.level = .floating', layout)
        self.assertIn('providersContentStack.orientation = .horizontal', layout)
        self.assertIn('editorWorkspaceStack.orientation = .horizontal', layout)
        self.assertIn('modeWorkspaceColumn.orientation = .vertical', layout)
        self.assertIn('let modeWorkspaceHost = NSView()', layout)
        self.assertIn('let providerPane = NSView()', layout)
        self.assertIn('let modelsRoutesPane = NSView()', layout)
        self.assertIn('self.modelsRoutesPane = modelsRoutesPane', layout)
        self.assertIn('self.modelsView = modelStack.view', layout)
        self.assertIn('cascadeColumn(title: "Providers", actions: providerButtons)', layout)
        self.assertIn('cascadeColumn(title: "Models", actions: modelButtons)', layout)
        self.assertIn('providerPane.widthAnchor.constraint(equalToConstant: 196)', layout)
        self.assertIn('modelsRoutesPane.widthAnchor.constraint(greaterThanOrEqualToConstant: 460)', layout)
        self.assertIn('modeWorkspaceHost.widthAnchor.constraint(equalToConstant: 680)', layout)
        self.assertIn('modelsRoutesPane.widthAnchor.constraint(equalToConstant: 472)', layout)
        self.assertIn('detailScrollView.widthAnchor.constraint(greaterThanOrEqualToConstant: 340)', layout)
        self.assertNotIn('detailScrollView.widthAnchor.constraint(equalToConstant:', layout)
        self.assertIn('right: 6', layout)
        self.assertIn('editorWorkspaceStack.trailingAnchor.constraint(equalTo: contentGuide.trailingAnchor)', layout)
        self.assertIn('editorWorkspaceStack.topAnchor.constraint(equalTo: contentGuide.topAnchor)', layout)
        self.assertNotIn('editorWorkspaceStack.topAnchor.constraint(equalTo: modeStack.bottomAnchor', layout)
        self.assertIn('providersWorkspace.trailingAnchor.constraint(equalTo: modeWorkspaceHost.trailingAnchor)', layout)
        self.assertIn('routesWorkspace.trailingAnchor.constraint(equalTo: modeWorkspaceHost.trailingAnchor)', layout)
        self.assertIn('labelView.alignment = .left', layout)
        self.assertIn('label.alignment = .left', layout)
        self.assertIn('preferredWidth: 212, minWidth: 150', layout)
        self.assertIn('func modelDetailGridRow', layout)
        self.assertIn('control.widthAnchor.constraint(greaterThanOrEqualToConstant: minWidth)', layout)
        self.assertNotIn('cascadeStack.orientation = .vertical', layout)
        self.assertNotIn('let listPaneWidth', layout)
        self.assertNotIn('let listPaneMinWidth', layout)
        self.assertIn('routesWorkspace.isHidden = true', layout)
        self.assertIn('class TrailingSeparatorlessTableHeaderCell', layout)
        self.assertEqual(layout.count('suppressTrailingHeaderSeparator(in:'), 3)
        self.assertIn('tableView.allowsColumnReordering = false', layout)
        self.assertIn('modelBillingSummaryPanel()', layout)
        self.assertNotIn('providerBillingSummaryPanel()', layout)
        self.assertIn('countColumn.title = "Models"', layout)
        self.assertIn('tableView.usesAlternatingRowBackgroundColors = false', layout)
        self.assertIn('label.lineBreakMode = .byClipping', tables)
        self.assertIn('case "permission_required": return "Permission required"', networking)
        self.assertIn('func billingStatusText', networking)
        self.assertIn('case "unsupported": return "N/A"', networking)
        self.assertIn('func billingSummaryValue', networking)
        self.assertNotIn('guard provider.enabled, model.modelEnabled', networking)
        self.assertNotIn('func providerBillingSummary', networking)
        self.assertNotIn('func providerBillingTooltip', networking)
        self.assertNotIn('func modelBillingColor', networking)
        self.assertNotIn('func providerBillingColor', networking)
        self.assertNotIn('func probePresentationColor', networking)
        self.assertIn('label.textColor = enabled ? .labelColor : .secondaryLabelColor', tables)
        self.assertIn('refreshModelBillingDetail(provider: providers[providerIndex], model: model)', tables)
        self.assertIn('modelUsageStatusLabel.stringValue', networking)
        self.assertIn('modelMultiplierStatusLabel.stringValue', networking)
        self.assertIn('let apiKeyOrderColumn = NSTableColumn(identifier: modelApiKeyOrderColumnIdentifier)', layout)
        self.assertNotIn('modelProbeColumnIdentifier', core + layout + tables)
        self.assertNotIn('"Token"', core + layout + tables + networking)
        self.assertNotIn('no token', core + layout + tables + networking)

        # The provider list remains compact; a selected model owns the billing
        # state and needs practical fixed space in the model list.
        name_width = re.search(r"nameColumn\.width = (\d+)", layout)
        billing_width = re.search(r"billingColumn\.width = (\d+)", layout)
        count_width = re.search(r"countColumn\.width = (\d+)", layout)
        self.assertIsNotNone(name_width)
        self.assertIsNotNone(billing_width)
        self.assertIsNotNone(count_width)
        self.assertGreaterEqual(int(name_width.group(1)), 90)
        self.assertEqual(int(billing_width.group(1)), 112)
        self.assertGreaterEqual(int(count_width.group(1)), 44)
        self.assertIn('billingColumn.minWidth = 112', layout)
        self.assertIn('billingColumn.maxWidth = 112', layout)

    def test_selecting_a_model_does_not_create_a_draft(self) -> None:
        actions = (SOURCES / "ModelConfigEditorActions.swift").read_text(encoding="utf-8")
        routes = (SOURCES / "ModelConfigEditorRoutes.swift").read_text(encoding="utf-8")

        commit_model = actions.split("func commitModelEditor()", 1)[1].split("func modelUpstreamPart", 1)[0]
        self.assertNotIn("persistDisplayedUpstreamApiModeOrder", commit_model)
        self.assertIn("persistDisplayedUpstreamApiModeOrder(providerIndex: providerIndex, modelIndex: modelIndex)", routes)

        model_info_refresh = routes.split("func refreshSelectedModelInfoState", 1)[1].split(
            "func ensureProviderHasKey", 1
        )[0]
        self.assertNotIn("self.providers[", model_info_refresh)
        self.assertNotIn("supportsImageGeneration =", model_info_refresh)

        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        show_window = core.split("func showWindow()", 1)[1].split(
            "func prepareEditorSkeleton", 1
        )[0]
        self.assertIn("presentEditorWindow()", show_window)
        self.assertNotIn("try loadConfigPayload()", show_window)
        self.assertIn("DispatchQueue.global(qos: .userInitiated).async", core)
        self.assertIn("window.orderFrontRegardless()", core)
        self.assertIn("guard !configurationLoadInFlight, !hasPendingChanges else { return }", core)
        loaded_configuration = core.split("func applyLoadedConfiguration", 1)[1].split(
            "func windowShouldClose", 1
        )[0]
        self.assertIn("modelTableView.deselectAll(nil)", loaded_configuration)
        self.assertIn("showProvider(at: providerIndex)", loaded_configuration)
        self.assertNotIn("showModel(providerIndex: 0, modelIndex: 0)", loaded_configuration)

        app_actions = (SOURCES / "AppDelegateActions.swift").read_text(encoding="utf-8")
        present_editor = app_actions.split("func presentModelConfigEditor", 1)[1].split(
            "func stageImportedProvidersModels", 1
        )[0]
        self.assertIn("editor.showWindow()", present_editor)
        self.assertIn("editor.loadConfigurationInBackground()", present_editor)
        self.assertNotIn("config-editor-bootstrap", present_editor)
        self.assertLess(
            present_editor.index("editor.showWindow()"),
            present_editor.index("editor.loadConfigurationInBackground()"),
        )

        persistence = (SOURCES / "ModelConfigEditorPersistence.swift").read_text(encoding="utf-8")
        load_payload = persistence.split("func loadConfigPayload", 1)[1].split(
            "func saveProviders", 1
        )[0]
        self.assertIn("if configEditorPythonPath() == nil", load_payload)
        self.assertLess(
            load_payload.index("if configEditorPythonPath() == nil"),
            load_payload.index('arguments: ["config-editor-bootstrap"]'),
        )
        self.assertLess(
            load_payload.index('arguments: ["config-editor-bootstrap"]'),
            load_payload.index('runHelper(arguments: ["load"])'),
        )

        editor_actions = (SOURCES / "ModelConfigEditorActions.swift").read_text(encoding="utf-8")
        imported = editor_actions.split("func adoptImportedConfiguration", 1)[1].split(
            "func mergeImportedProviders", 1
        )[0]
        self.assertIn("configurationLoadGeneration += 1", imported)
        self.assertIn("configurationLoadInFlight = false", imported)

    def test_menu_control_requests_do_not_block_window_presentation(self) -> None:
        actions = (SOURCES / "AppDelegateActions.swift").read_text(encoding="utf-8")
        dialog = (SOURCES / "WebDAVSettingsDialog.swift").read_text(encoding="utf-8")

        webdav_dialog = actions.split("func showWebDAVConfigureDialog", 1)[1].split(
            "func webDAVRemoteNameForDialog", 1
        )[0]
        self.assertIn("dialog.setSavedSettingsLoading(true)", webdav_dialog)
        self.assertIn("DispatchQueue.global(qos: .utility).async", webdav_dialog)
        self.assertLess(
            webdav_dialog.index("DispatchQueue.global(qos: .utility).async"),
            webdav_dialog.index("dialog.runModal()"),
        )
        self.assertNotIn("let settings = readWebDAVSyncSettings()", webdav_dialog)

        runtime_settings = actions.split("func presentRuntimeSettings", 1)[1].split(
            "func runtimeSettingsInput", 1
        )[0]
        self.assertIn('arguments: ["runtime-settings-apply"]', actions)
        self.assertNotIn("restartService:", runtime_settings)
        self.assertIn("performOnMainRunLoop", runtime_settings)
        self.assertLess(
            runtime_settings.index("performOnMainRunLoop"),
            runtime_settings.index("dialog.runModal()"),
        )

        save_runtime_settings = actions.split("func saveRuntimeSettings", 1)[1].split(
            "@objc func configureWebDAVSync", 1
        )[0]
        self.assertIn('arguments: ["runtime-settings-apply"]', save_runtime_settings)
        self.assertIn("performOnMainRunLoop", save_runtime_settings)
        self.assertNotIn("DispatchQueue.main.async", save_runtime_settings)

        toggle = actions.split("@objc func toggleWebDAVSync()", 1)[1].split(
            "@objc func openLogs", 1
        )[0]
        self.assertIn("DispatchQueue.global(qos: .userInitiated).async", toggle)
        self.assertIn("let settings = enabled ? nil : self.readWebDAVSyncSettings()", toggle)
        self.assertIn("initialSettings: settings", toggle)

        self.assertIn("func applyLoadedSettings", dialog)
        self.assertIn("guard !didStopModal, !hasUserEdits else { return }", dialog)

    def test_configuration_packages_use_one_flat_menu_surface(self) -> None:
        core = (SOURCES / "AppDelegateCore.swift").read_text(encoding="utf-8")
        actions = (SOURCES / "AppDelegateActions.swift").read_text(encoding="utf-8")
        editor_actions = (SOURCES / "ModelConfigEditorActions.swift").read_text(encoding="utf-8")
        editor_core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")
        package_dialog = (SOURCES / "ConfigurationPackageDialog.swift").read_text(encoding="utf-8")
        dispatch = (ROOT / "service" / "dispatch.sh").read_text(encoding="utf-8")
        build = (ROOT / "mac_menu" / "build.sh").read_text(encoding="utf-8")

        self.assertIn('"Import / Export Config..."', core)
        self.assertIn('#selector(showConfigurationPackageDialog)', core)
        self.assertNotIn('"Import Configuration..."', core)
        self.assertNotIn('"Export Configuration..."', core)
        self.assertIn('func showConfigurationPackageDialog()', actions)
        self.assertIn("ConfigurationPackageDialogController", actions)
        self.assertNotIn("accessoryView", package_dialog)
        self.assertIn("importPanelHeight", package_dialog)
        self.assertIn("exportPanelHeight", package_dialog)
        self.assertIn("window.setContentSize", package_dialog)
        self.assertIn('ConfigurationPackageDialogController()', actions)
        self.assertIn('case .importPackage:', actions)
        self.assertIn('case .export(let sections):', actions)
        self.assertIn('func exportConfigurationPackageFromMenu(sections selected: [String])', actions)
        self.assertIn('checkboxWithTitle: "Runtime Settings"', package_dialog)
        self.assertIn('checkboxWithTitle: "Providers & Models"', package_dialog)
        self.assertIn('NSSegmentedControl(', package_dialog)
        self.assertIn('"configuration-package-import"', actions)
        self.assertIn('"configuration-package-export"', actions)
        self.assertIn('configuration-package-export)', dispatch)
        self.assertIn('configuration-package-import)', dispatch)
        self.assertIn("configuration_package.py", build)
        self.assertNotIn("runtime-settings-import", dispatch)
        self.assertNotIn("runtime-settings-export", dispatch)
        self.assertNotIn("importConfiguration()", editor_actions)
        self.assertNotIn("exportConfiguration()", editor_actions)
        self.assertNotIn("importConfigurationButton", editor_core)
        self.assertNotIn("exportConfigurationButton", editor_core)

        imported_package = actions.split("func presentImportedConfigurationPackage", 1)[1].split(
            "@objc func showRouteRecoveryDetails", 1
        )[0]
        self.assertIn("presentRuntimeSettings(stagedValues: runtimeValues) { accepted in", imported_package)
        self.assertIn("if accepted {", imported_package)
        self.assertLess(
            imported_package.index("presentRuntimeSettings(stagedValues: runtimeValues) { accepted in"),
            imported_package.index("} else {\n            presentProviders()"),
        )

    def test_editor_close_confirms_before_discarding_a_draft(self) -> None:
        crud = (SOURCES / "ModelConfigEditorCrud.swift").read_text(encoding="utf-8")
        core = (SOURCES / "ModelConfigEditorCore.swift").read_text(encoding="utf-8")

        self.assertIn("func requestEditorClose()", crud)
        self.assertIn("commitEditor()", crud.split("func requestEditorClose()", 1)[1])
        self.assertIn("guard hasPendingChanges else", crud)
        self.assertIn("Discard unsaved provider and route changes?", crud)
        self.assertIn("requestEditorClose()", core.split("func windowShouldClose", 1)[1])

    def test_status_menu_has_no_submenus_and_keeps_recovery_visible(self) -> None:
        core = (SOURCES / "AppDelegateCore.swift").read_text(encoding="utf-8")
        status = (SOURCES / "AppDelegateStatus.swift").read_text(encoding="utf-8")
        actions = (SOURCES / "AppDelegateActions.swift").read_text(encoding="utf-8")
        build_menu = core.split("func buildMenu()", 1)[1].split(
            "func appVersionMenuTitle", 1
        )[0]

        self.assertIn('logsMenuItem = menuItem("View Logs"', build_menu)
        self.assertIn('configurationPackageMenuItem = menuItem("Import / Export Config..."', build_menu)
        self.assertIn('codexConfigurationMenuItem = menuItem("Codex Settings..."', build_menu)
        self.assertIn('modelConfigEditorMenuItem = menuItem("Providers & Models..."', build_menu)
        self.assertIn("CodexConfigDialogController", actions)
        self.assertNotIn("showCodexModelChooser", actions)
        self.assertNotIn("CodexModelChooserDialogController", actions)
        self.assertIn("menu.addItem(modelConfigEditorMenuItem)", build_menu)
        self.assertIn("menu.addItem(configurationPackageMenuItem)", build_menu)
        self.assertIn("menu.addItem(webdavStatusMenuItem)", build_menu)
        self.assertIn("menu.addItem(webdavEnabledMenuItem)", build_menu)
        self.assertIn("menu.addItem(webdavConfigureMenuItem)", build_menu)
        self.assertNotIn("Diagnostics", build_menu)
        self.assertNotIn("routeTraceStartupMenuItem", build_menu)
        self.assertNotIn("Import Configuration...", build_menu)
        self.assertNotIn("Export Configuration...", build_menu)
        self.assertNotIn("serviceMenuItem.submenu", build_menu)
        self.assertNotIn("configurationMenuItem.submenu", build_menu)
        self.assertNotIn("configurationPackageMenuItem.submenu", build_menu)
        self.assertNotIn("Billing", build_menu)
        self.assertNotIn("refreshProviderBilling", core)
        self.assertNotIn("providerBillingMenuItem", core)
        self.assertEqual(build_menu.count(".submenu ="), 0)
        self.assertIn('routeRecoveryStatusMenuItem = menuItem("Recovery: 0 recovering / 0 cooldown"', build_menu)
        self.assertIn("routeRecoveryStatusMenuItem.isHidden = false", status)
        self.assertIn("routeRecoveryStatusMenuItem.isEnabled = true", status)
        self.assertIn("statusItem.length = 32", core)
        self.assertIn("let active = status.recovering > 0 || status.cooldown > 0", core)
        self.assertIn('button.title = "LL"', core)
        self.assertIn("button.toolTip = active", core)
        self.assertIn('button.setAccessibilityLabel("LiteLLM Menu")', core)
        self.assertIn("renderStatusButton(state.routeRecovery)", status)
        self.assertNotIn("routeTraceStartupMenuItem", status)
        self.assertNotIn("routeTraceStartupMenuItem", actions)
        self.assertNotIn("toggleRouteTrace", actions)
        self.assertIn("Next cooldown check:", status)
        self.assertNotIn('"LL !"', core)
        self.assertNotIn('"LL ?"', core)
        self.assertIn("Verdict: heartbeat is fresh; recovery is still working.", status)
        self.assertIn("Verdict: no fresh heartbeat; recovery may be stuck.", status)
        self.assertIn("Verdict: heartbeat unavailable; recovery progress is unknown.", status)
        self.assertIn("route deployment(s) are cooling down before they can be retried", status)
        self.assertIn('case "billing": return "Credit or quota"', status)
        self.assertIn("func routeRecoveryDisplayText", status)
        self.assertIn('of: "billing",', status)
        self.assertIn('with: "credit",', status)
        self.assertNotIn("providerBilling", status)
        self.assertNotIn("Refresh Billing", status)
        self.assertNotIn("provider-billing", core)
        self.assertNotIn("provider-billing", status)
        self.assertNotIn("provider-billing", actions)


if __name__ == "__main__":
    unittest.main()
