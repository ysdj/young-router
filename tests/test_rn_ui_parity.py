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
# The account list both a relay station and an OpenAI row draw.
ACCOUNTS_TABLE = ROOT / "rn/packages/shared/src/ui/AccountsTable.tsx"
RELAY_ORIGIN = ROOT / "rn/packages/shared/src/ui/relayOrigin.ts"
TYPOGRAPHY = ROOT / "rn/packages/shared/src/ui/typography.ts"
MACOS_CONTROLS = ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
WINDOWS_CONTROLS = ROOT / "rn/apps/windows/src/native/windows/WinUIControls.cpp"
WINDOWS_LEAF = ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp"
WINDOWS_RELAY = ROOT / "rn/apps/windows/src/native/windows/WindowsRelayLogin.cpp"
CODE_EDITOR_WEB = ROOT / "rn/packages/shared/src/ui/code-editor/CodeEditorWeb.ts"
CODE_EDITOR_WRAPPER = ROOT / "rn/packages/shared/src/ui/code-editor/CodeEditorWebView.tsx"
CODE_EDITOR_PAGE = ROOT / "rn/packages/shared/src/ui/code-editor/codeEditorPage.ts"
ZH_HANS = ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts"
EN = ROOT / "rn/packages/shared/src/i18n/en.ts"


class ReactNativeUiParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = UI_SOURCE.read_text(encoding="utf-8")
        cls.native_controls = NATIVE_CONTROLS.read_text(encoding="utf-8")
        cls.appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        cls.macos_leaf = MACOS_LEAF.read_text(encoding="utf-8")
        cls.macos_project = MACOS_PROJECT.read_text(encoding="utf-8")
        cls.platform_entry = PLATFORM_ENTRY.read_text(encoding="utf-8")
        cls.windows_leaf = WINDOWS_LEAF.read_text(encoding="utf-8")
        cls.code_editor_web = CODE_EDITOR_WEB.read_text(encoding="utf-8")
        cls.code_editor_wrapper = CODE_EDITOR_WRAPPER.read_text(encoding="utf-8")
        cls.code_editor_page = CODE_EDITOR_PAGE.read_text(encoding="utf-8")
        cls.zh = ZH_HANS.read_text(encoding="utf-8")
        cls.en = EN.read_text(encoding="utf-8")
        cls.relay = RELAY_MANAGER.read_text(encoding="utf-8")

    def assert_ui_has(self, marker: str) -> None:
        self.assertIn(marker, self.ui, marker)

    def assert_ui_not_has(self, marker: str) -> None:
        self.assertNotIn(marker, self.ui, marker)

    @staticmethod
    def call_arguments(source: str, start: int) -> list[str]:
        """The top-level arguments of the call whose `(` is at `start`."""

        arguments: list[str] = []
        current: list[str] = []
        depth = 0
        for character in source[start + 1:]:
            if character in "([{":
                depth += 1
                current.append(character)
                continue
            if character in ")]}":
                if depth == 0:
                    arguments.append("".join(current).strip())
                    return arguments
                depth -= 1
                current.append(character)
                continue
            if character == "," and depth == 0:
                arguments.append("".join(current).strip())
                current = []
                continue
            current.append(character)
        return arguments

    @staticmethod
    def jsx_tags(source: str, name: str) -> list[str]:
        """Every `<Name …>` opening tag, arrow-function `>` included."""
        tags: list[str] = []
        start = source.find(f"<{name}")
        while start != -1:
            depth = 0
            index = start + len(name) + 1
            while index < len(source):
                character = source[index]
                if character in "({[":
                    depth += 1
                elif character in ")}]":
                    depth -= 1
                elif character == ">" and depth == 0:
                    break
                index += 1
            tags.append(source[start:index + 1])
            start = source.find(f"<{name}", index)
        return tags

    def test_a_render_fault_states_a_pane_instead_of_aborting_the_app(self) -> None:
        """An uncaught render error is a dead process, not a blank pane.

        A pane that read a `const` before its declaration threw a `TypeError`
        while rendering.  Nothing caught it: the throw left the window's root
        with no live child, travelled out of React's TurboModule call as a C++
        exception, and `std::terminate` aborted the app — the pane the user
        clicked took every other window with it.

        React only stops a render error in `componentDidCatch`, so every window's
        root is one boundary.  A THROW here is a bug in a pane, and the user's
        window survives it with the cause stated.
        """

        boundary = (ROOT / "rn/packages/shared/src/ui/renderFaultBoundary.tsx").read_text(encoding="utf-8")
        self.assertIn("export class RenderFaultBoundary extends React.Component<", boundary)
        # The fault screen must be escapable: a window that threw once used to
        # stay on it for the life of the process.  Recovery is a fresh mount,
        # not a reuse of the instance that holds the bad state, so the retry
        # clears the error and re-keys the subtree.
        self.assertIn("this.setState({ error: undefined });", boundary)
        self.assertIn("<React.Fragment key={this.attempt}>{this.props.children}</React.Fragment>", boundary)
        self.assertIn('translate("error.renderFaultRetry"', boundary)
        self.assertIn("static getDerivedStateFromError(error: unknown): RenderFaultState {", boundary)
        self.assertIn("componentDidCatch(error: Error): void {", boundary)
        # The handler of last resort may not throw again: a failure inside it
        # aborts the very process the boundary exists to protect.
        handler = boundary.split("componentDidCatch(error: Error): void {", 1)[1].split("render(): React.ReactNode {", 1)[0]
        self.assertIn("} catch {", handler)
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn('import { RenderFaultBoundary } from "./ui/renderFaultBoundary";', bootstrap)
        # One boundary per window root, wrapping the whole app.
        self.assertEqual(1, bootstrap.count("<RenderFaultBoundary"))
        self.assertIn("return <RenderFaultBoundary translate={dependencies.translate}><YoungRouterApp {...dependencies}", bootstrap)
        self.assertIn("nativeAction={nativeAction} /></RenderFaultBoundary>;", bootstrap)

    def test_menu_bar_home_is_not_a_dashboard_or_sidebar_shell(self) -> None:
        """The menu-bar host stays hidden; the status icon opens the settings shell."""
        self.assert_ui_has(': <View style={styles.menuBarHost} />) : null}')
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
        # A settings pane window keeps one permanent strip: the last result
        # replaces the idle Ready label, so it never appears or disappears
        # mid-action.  A child window keeps no strip of its own.
        # The strip keeps its permanent strip and, since the usage pager lives
        # at its right end, it may also carry that route's own controls.
        self.assert_ui_has('{shell ? <View style={[styles.routeStatusBar, isWindowsShell && styles.routeStatusBarWindows]}><View style={styles.routeStatusLead}>{busy ? <NativeBusySpinner label={translate("common.working")} /> : null}<Text numberOfLines={2} style={[styles.routeStatusText, isWindowsShell && styles.routeStatusTextWindows]}>{routeStatus ?? translate("common.ready")}</Text></View>{statusBarControls}</View> : null}')
        # The strip reads from the same Windows palette as the pane above it.
        self.assert_ui_has('routeStatusBarWindows: { backgroundColor: WINDOWS_CONTROL.fillSolid, borderTopColor: WINDOWS_CONTROL.strokeSubtle }')
        self.assertNotIn("{shell && routeStatus ? <View style={styles.routeStatusBar}>", self.ui)
        self.assertIn('"common.ready": "Ready"', self.en)
        # A counted run clears its result before it starts, so the strip has to
        # state the wait itself or a held window reads 就绪 while it answers
        # nothing.
        self.assert_ui_has('const routeStatus = result ?? (busy ? translate("common.working") : undefined);')
        self.assertIn('"common.working": "Working…"', self.en)
        self.assertIn('"common.working": "处理中…"', self.zh)
        self.assert_ui_has('{busy ? <NativeBusySpinner label={translate("common.working")} /> : null}')
        self.assertIn('"common.ready": "就绪"', self.zh)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        self.assertIn('| "common.ready"', translation_keys)
        # The strip is a tip surface: one type step below body text.
        self.assert_ui_has('routeStatusText: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE }')

    def test_child_surfaces_state_their_result_beside_their_own_buttons(self) -> None:
        # A child window (分组管理, the provider wizard, the raw file editor,
        # the WebDAV detail form) keeps no status bar of its own: it states its
        # result beside its own footer buttons, and the buttons keep their
        # position.  The window that opened it states nothing about work it did
        # not start.
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        self.assertIn("  childSaving: string;", types)
        self.assert_ui_has('childSaving: translate("common.saving"),')
        # The wizard's footer states its own line beside its buttons again.
        # Exactly one status line, and no step body repeats it.
        self.assert_ui_has('const wizardStatus = validation || loginFeedback.current || (loginPhase === "sign-in" ? translate("relay.loginWorking") : "");')
        self.assert_ui_has('const wizardFooterStatus = wizardStatus || status || "";')
        self.assert_ui_has('{wizardFooterStatus ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.childSurfaceFooterStatus}>{wizardFooterStatus}</Text> : <View style={styles.childSurfaceFooterSpacer} />}')
        # The WebDAV detail form is a child window too: its write is stated in
        # its footer, beside the three buttons that own it.
        self.assert_ui_has('const footerStatus = testing')
        self.assert_ui_has('? translate("webdav.testing")')
        self.assert_ui_has('{footerStatus ? <Text accessibilityLiveRegion="polite" numberOfLines={2} style={styles.childSurfaceFooterStatus}>{footerStatus}</Text> : <View style={styles.childSurfaceFooterSpacer} />}')
        self.assert_ui_has('<NativeButton primary title={translate("status.saveAndClose")} busy={saving} disabled={busy || saving || syncing || !pendingWrite} onPress={() => { void save(); }} />')
        self.assert_ui_not_has("providerWizardValidation")
        self.assert_ui_not_has('<Text style={styles.providerWizardHint}>{loginFeedback.current}')
        self.assert_ui_not_has("{loginFeedback.current ? <Text")
        self.assert_ui_not_has("loginFeedback.current ?? translate")
        # A required field wears its mark at its own label, so the message about
        # required fields points at something the user can see.
        self.assertIn("export function NativeFormRow({ label, required = false, children }", (ROOT / "rn/packages/shared/src/ui/RelayAccountManager.tsx").read_text(encoding="utf-8"))
        self.assertIn('{required ? <Text style={styles.formLabelRequired}>＊</Text> : null}', (ROOT / "rn/packages/shared/src/ui/RelayAccountManager.tsx").read_text(encoding="utf-8"))
        for marker in (
            '<NativeFormRow required label={translate("providers.wizard.selectProvider")}>',
            '<NativeFormRow required label={translate("providers.wizard.baseUrl")}>',
            '<NativeFormRow required label={translate("providers.wizard.providerName")}>',
            '<NativeFormRow required label={translate("providers.wizard.selectApiKey")}>',
            '<NativeFormRow required label={translate("providers.wizard.apiKeyName")}>',
            '<NativeFormRow required label={translate("providers.wizard.apiKeyValue")}>',
        ):
            self.assert_ui_has(marker)
        # The manual-model pair is one entry, and says so instead of a mark.
        self.assert_ui_has('setValidation(translate("providers.wizard.manualModelPairRequired"));')
        self.assertIn('"providers.wizard.manualModelPairRequired": "手动模型需要同时填写模型名与上游模型。"', self.zh)
        self.assertIn('"providers.wizard.manualModelPairRequired": "A manual model needs both its name and its upstream model."', self.en)
        # The raw file editor keeps its save result beside 关闭 / 保存并关闭.
        self.assert_ui_has('const [footerStatus, setFooterStatus] = useState<string>();')
        self.assert_ui_has('setFooterStatus(translate("common.saving"));')
        self.assert_ui_has('setFooterStatus(translate("common.saved"));')
        self.assert_ui_has('setFooterStatus(errorMessage(reason, translate));')
        # Windows asks "discard?" in this footer because a native confirmation
        # owned by the editor's own process window never appeared above it;
        # macOS keeps its sheet. The result still rides beside the buttons.
        self.assert_ui_has('<Text numberOfLines={1} style={assistantFileSurfaceStyles.editorFooterStatus}>{confirmingDiscard ? translate("settings.discardDraftTitle") : footerStatus ?? ""}</Text>')
        # The editor reports its own draft state through onDirtyChange, because
        # on Windows the draft never reaches Core until Save: a deferred stage
        # leaves the Core snapshot clean while the window holds unsaved text, so
        # Close used to throw that text away with no "discard?" asked at all.
        self.assert_ui_has('const localEditorDirtyRef = useRef(false);')
        self.assert_ui_has('const handleEditorDirtyChange = useCallback((dirty: boolean): void => {')
        self.assert_ui_has('const coreDraftDirty = current?.drafts[target.domain]?.dirty === true;')
        self.assert_ui_has('if (!coreDraftDirty && !localEditorDirtyRef.current) {')
        self.assert_ui_has('if (current !== undefined || localEditorDirtyRef.current) {')
        self.assert_ui_has('if (deferStage && draftRef.current !== stagedTextRef.current) return;')
        self.assert_ui_has('if (pending && !deferStage) scheduleStage();')
        self.assert_ui_has('deferStage={Platform.OS === "windows"}')
        # Both hosts now edit this document in the rich WebView: the browser was
        # under-scaled by the system DPI rather than unsupported in the child
        # window's XAML island, so the Windows multiline field (and the
        # line-break repair it needed) is gone rather than kept as a fallback.
        self.assert_ui_not_has('withDocumentEnding')
        self.assert_ui_not_has('styles.rawNativeTextField')
        self.assert_ui_has('setConfirmingDiscard(true);')
        self.assert_ui_has('<ActionButton primary danger title={translate("common.discard")} disabled={saving} onPress={discardConfirmed} />')
        # Its own actions are the only thing that may block this window: a stuck
        # host-wide busy flag used to make Close return immediately.
        self.assert_ui_has('    if (saving) return;')
        self.assert_ui_has('editorFooterStatus: { flex: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE },')
        # 分组管理 hands its staged edits over and stays up while they are
        # written and applied: a failed save keeps its rows and its
        # 保存并关闭 and states 更改未生效 beside those buttons.
        for marker in (
            "const answersApplies = Boolean(native.awaitGroupManagerApply && native.finishGroupManagerApply);",
            "const edits = await ask;",
            "await applyStagedQuietly();",
            'await native.finishGroupManagerApply?.({ status, close }).catch(() => undefined);',
            'status = translate("common.notApplied");',
            "if (!close) void answerOneApplyRequest();",
            "setGroupManagerOpen(true);",
            "setGroupManagerOpen(false);",
        ):
            self.assertIn(marker, self.relay)
        # The pane's own immediate apply stays away while the sheet is up, and
        # the apply the sheet asked for is quiet: the child states the outcome.
        gate = (ROOT / "rn/packages/shared/src/ui/providerWizardGate.ts").read_text(encoding="utf-8")
        self.assertIn("export function setGroupManagerOpen(open: boolean): void {", gate)
        self.assertIn("export function isGroupManagerOpen(): boolean {", gate)
        self.assert_ui_has("return isProviderWizardOpen() || isAssistantEditorOpen() || isGroupManagerOpen() || isWebDavSettingsOpen();")
        self.assert_ui_has("const applyStagedQuietly = (): Promise<void> => apply({ quiet: true });")
        # A host whose sheet can only save on close stages those same edits
        # after it is gone and states that host's outcome in the pane.
        self.assertIn('onStatus?.(translate("common.saved"));', self.relay)
        self.assert_ui_has("    quiet = false,\n  ): Promise<T | undefined> => {\n    const publishResult = (next: string | undefined): void => {\n      if (quiet) return;")
        self.assert_ui_has("true, undefined, options?.quiet === true);")

    def test_a_new_model_is_marked_until_finished_and_a_relay_block_is_not_the_edits_fault(self) -> None:
        """Standards for create-then-configure: a new row is a draft.

        Inline validation must not fire for a record the user has not finished
        (SAP draft handling; NN/g's error guidelines), and an error must name
        the thing that actually refused the write.  ＋ therefore creates a live
        row that carries the placeholder name — never a validation failure, and
        marked until it is finished — and a relay that is not ready states its
        own cause instead of blaming the edit.
        """
        # ＋ creates an enabled row that carries the placeholder name and
        # inherits the key the user is working in, so it lands in a real key
        # group instead of one the app invents — and the pane marks it until
        # the placeholder is replaced (`modelNeedsAttention`).
        self.assert_ui_has('const inheritedKey = modelProviderKeyState(model, provider)')
        self.assert_ui_has('?? providerKeyStates(provider).find((key) => key.id === selectedFetchKey)')
        self.assert_ui_has('...(inheritedKey ? { api_key_name: inheritedKey.name, provider_key_id: inheritedKey.id } : {})')
        self.assert_ui_has('model: {')
        self.assert_ui_has('enabled: true, order: 0,')
        self.assert_ui_has('const name = uniquePlaceholderName(models.map((item) => stringValue(item.model_name ?? item.name)), base);')
        # The list draws a group row only for a key the model actually belongs
        # to; a keyless draft is listed without an invented 未定义密钥 group.
        self.assert_ui_has("const key = modelProviderKeyState(item, provider ?? {});")
        self.assert_ui_has("if (!key) {")
        self.assert_ui_has("ungrouped.push(item);")
        self.assert_ui_has("for (const item of ungrouped) rows.push({ key: editorIdentifier(item), cells: modelCells(item) });")
        # One group row is one provider key, addressed by its slot id, so the
        # station's key and a custom key that read the same stay two groups and
        # a model is filed under the key it names.
        self.assert_ui_has('const grouped = new Map<string, { label: string; models: UnknownRecord[] }>();')
        self.assert_ui_has('const list = grouped.get(key.id);')
        self.assert_ui_has('rows.push({ key: `key:${keyID}`, cells: [group.label], spanning: true });')
        self.assert_ui_not_has('rows.push({ key: `key:${keyName}`, cells: [keyName], spanning: true });')
        self.assert_ui_has('const byName = keyStates.filter((entry) => entry.name === keyName && keyName !== "");')
        self.assert_ui_has('return byName.length === 1 ? byName[0] : undefined;')
        # The new row states what is left where its own fields are, and says
        # that it is already live (a create is enabled).
        self.assert_ui_has("function isDraftModel(model: UnknownRecord, translate: Translate): boolean {")
        # An account-backed route is just as finished as a key-backed one: the
        # hint asks whether the route names a credential of either kind.
        self.assert_ui_has('<Text style={styles.fieldHint}>{translate(selectedProviderKey || routeCredential ? "providers.draftModelHint" : "providers.draftModelKeylessHint")}</Text>')
        self.assertIn('"providers.draftModelKeylessHint": "新建的模型默认启用：请填写公开模型名与上游模型，并选择密钥。"', self.zh)
        self.assertIn('"providers.draftModelHint": "新建的模型默认启用：请填写公开模型名与上游模型，客户端即可使用它。"', self.zh)
        self.assertIn('"providers.draftModelHint": "A new model starts enabled: fill in the public name and the upstream model for clients to use it."', self.en)
        self.assertIn('"providers.draftModelKeylessHint": "A new model starts enabled: fill in the public name and the upstream model, and choose a key."', self.en)
        # Core names the domain that refused: a relay that is not ready is not
        # the user's edit failing validation.
        self.assertIn('provider_model_invalid: "error.validationFailed",', self.ui)
        self.assertIn('relay_preflight_failed: "error.relayNotReady",', self.ui)
        self.assertIn('relay_not_ready: "error.relayNotReady",', self.ui)
        self.assertIn('"error.relayNotReady": "中转站还有待处理的操作，本次更改未生效。请在“供应商与模型”中刷新资源或重新登录后重试。"', self.zh)
        self.assertIn('"error.relayNotReady": "The relay still has work pending, so the change was not applied. Refresh its resources or sign in again under Providers & Models, then try again."', self.en)
        # A linked key the relay could resolve but whose binding the station
        # cannot serve names that cause too, instead of a bare 更改未生效.
        self.assertIn('relay_binding_failed: "error.relayBindingFailed",', self.ui)
        self.assertIn('"error.relayBindingFailed": "已链接的中转密钥无法解析该路由（资源、域名或上游模型），本次更改未生效。请刷新资源并核对该路由的上游模型。"', self.zh)
        self.assertIn('"error.relayBindingFailed": "The linked relay key could not be resolved for this route (its resource, base URL, or upstream model), so the change was not applied. Refresh the relay resources and check this route\'s upstream model."', self.en)
        self.assertIn('| "error.relayBindingFailed"', (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8"))
        service = (ROOT / "young_router/core/service.py").read_text(encoding="utf-8")
        # Only a draft that binds relay material enters the relay transaction.
        self.assertIn("def _relay_binding_projection(state: object) -> list[tuple[str, ...]]:", service)
        self.assertIn("if baseline is None or _relay_binding_projection(", service)
        self.assertIn('raise CoreError("relay_not_ready", "Fix the relay connection or binding issues")', service)

    def test_one_window_states_each_result_once(self) -> None:
        """A window prints a status once: no message twice on one screen."""
        relay_manager = (ROOT / "rn/packages/shared/src/ui/RelayAccountManager.tsx").read_text(encoding="utf-8")
        mac_leaf = MACOS_LEAF.read_text(encoding="utf-8")
        windows_leaf = WINDOWS_LEAF.read_text(encoding="utf-8")

        # The provider wizard's own line is its footer's, and no step body
        # repeats it (the validation line and the two feedback hints are gone).
        self.assert_ui_not_has("providerWizardValidation")
        self.assert_ui_not_has("{loginFeedback.current ? <Text")
        self.assert_ui_not_has("loginFeedback.current ?? translate")
        self.assert_ui_has('{wizardFooterStatus ? <Text accessibilityLiveRegion="polite"')
        # Its required controls wear the mark the message talks about, and the
        # manual-model pair — one entry, not two required fields — says so.
        self.assertIn("{required ? <Text style={styles.formLabelRequired}>＊</Text> : null}", relay_manager)
        self.assertIn("<NativeFormRow required label={translate(\"providers.wizard.baseUrl\")}>", self.ui)
        self.assert_ui_has('setValidation(translate("providers.wizard.manualModelPairRequired"));')
        # 分组管理's copy result takes its one status line beside the buttons
        # instead of a second line under the detail rows.
        self.assertNotIn("copyStatusField", mac_leaf)
        self.assertIn("func showTransientStatus(_ message: String, clearAfter seconds: Double = 4) {", mac_leaf)
        self.assertNotIn("controls::TextBlock copy_status;", windows_leaf)
        self.assertIn("controls::TextBlock footer_status;", windows_leaf)
        # 常规 states the action that landed; the 服务 row already prints the
        # state, so the strip never repeats that word.
        self.assert_ui_has('onStatus(translate(serviceRestart ? "service.restarted" : "service.started"));')
        self.assert_ui_not_has('onStatus(translate("service.running"));')

    def test_a_refused_write_reports_not_applied_in_the_strip_only(self) -> None:
        # A settings pane never draws a validation card over its content: a
        # write Core refused, rolled back, or could not finish says so in the
        # pane's own permanent status strip, in the user's words.
        self.assert_ui_not_has("function IssueList(")
        self.assert_ui_not_has("styles.issueBox")
        self.assert_ui_not_has("styles.issue:")
        self.assert_ui_not_has("styles.validationText")
        self.assertNotIn("<WindowTitle title={windowTitle} validation=", self.ui)
        self.assert_ui_not_has("setIssues(")
        # A rejected draft names the entries that block it: the strip carries the
        # first issues instead of a sentence that says only that something is wrong.
        self.assertIn('publishResult(value.valid ? translate("common.saved") : (validationSummaryMessage(value, translate) ?? translate("error.validationFailed")));', self.ui)
        self.assertIn("function validationSummaryMessage(summary: unknown, translate: Translate): string | undefined {", self.ui)
        self.assertIn('publishResult(applyResultMessage(value, translate, typeof message === "string" ? message : null, latestSnapshot.current?.service.state));', self.ui)
        self.assertIn('if (result.status === "partial") return translate("common.notAppliedPartial");', self.ui)
        self.assertIn('if (result.status === "failed") return translate("common.notApplied");', self.ui)
        self.assertIn('apply_failed: "common.notApplied",', self.ui)
        self.assertIn('"common.notApplied": "Changes not applied"', self.en)
        self.assertIn('"common.notApplied": "更改未生效"', self.zh)
        self.assertIn('"common.notAppliedPartial": "Some changes not applied"', self.en)
        self.assertIn('"common.notAppliedPartial": "部分更改未生效"', self.zh)
        self.assertIn('"error.validationFailed": "Validation failed; changes not applied."', self.en)
        self.assertIn('"error.validationFailed": "校验未通过，更改未生效。"', self.zh)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        for key in ("common.notApplied", "common.notAppliedPartial", "common.notAppliedRetry"):
            self.assertIn(f'| "{key}"', translation_keys)
        self.assertIn('"common.notAppliedRetry": "This save did not go through; your draft is still here — save again."', self.en)
        self.assertIn('"common.notAppliedRetry": "这次没保存上，改动还在草稿里；再保存一次即可。"', self.zh)
        # A revision conflict is this app racing itself, not a cause the user
        # can act on: it must not name a second writer or announce what the
        # router did about it.  Every surface that can meet one already rebases
        # once on the authoritative revision (`applyOutcome`, the buffered
        # dispatch, the raw editor, the child surfaces), so one that still
        # arrives reports the outcome the pane can show, exactly like any other
        # rolled-back write.
        # A conflict that survives every rebase states the outcome *and* the
        # one thing that fixes it: the draft is still staged and saving again
        # is the remedy.  It still names no second writer and no router step —
        # a user with a staged edit and no way forward is what the bare
        # 更改未生效 left behind.
        self.assertIn('revision_conflict: "common.notAppliedRetry",', self.ui)
        self.assertIn('// A conflict is not a verdict on the change', self.ui)
        # A draft that Core refused during Apply is a validation failure too, and
        # it has to name the row that blocked it: a bare 更改未生效 left the row
        # the user had just touched (the ＋ that started the write) looking like
        # the one that failed, with nothing marked anywhere in the pane.
        self.assertIn('if (!(code === "provider_model_invalid" || code === "validation_failed" || code === "relay_preflight_failed" || code === "apply_failed")) return undefined;', self.ui)
        self.assertIn('code === "provider_model_invalid" || code === "apply_failed"', self.ui)
        self.assertNotIn('revision_conflict: "error.revisionConflict"', self.ui)
        for source in (self.en, self.zh, translation_keys):
            self.assertNotIn('"error.revisionConflict"', source)
        # A settled transition is the render that follows it, so the conflicts
        # the UI resolves itself never reach this stop either.
        for marker in (
            'if (!isRevisionConflict(reason)) throw reason;',
            'if (!isRevisionConflict(reason) || !isRevisionRetryableAction(type)) throw reason;',
        ):
            self.assert_ui_has(marker)
        # Core's own step names stay internal: a bare snake_case identifier is
        # not a cause the user can act on, so it never reaches the strip.
        self.assertIn("const INTERNAL_STEP_NAME = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/;", self.ui)
        self.assertIn("if (detail && detail.length <= 160 && !INTERNAL_STEP_NAME.test(detail)) return detail;", self.ui)
        # The vocabulary the card and the relay-sync sentences used is gone.
        for removed in (
            "common.validationIssues",
            "relay.applyIssue",
            "relay.applyPartial",
            "relay.applyFailed",
            "relay.apiKeyGroupStaged",
            # A copy that worked says nothing anywhere: no confirmation line.
            "relay.apiKeyCopied",
            "copiedLabel",
            "copied_label = read(",
        ):
            for source in (self.ui, self.relay, self.en, self.zh, translation_keys):
                self.assertNotIn(f'"{removed}"', source)

    def test_group_manager_status_stays_out_of_the_pane_status_strip(self) -> None:
        # 分组管理 is its own surface with its own status bar: its edits are
        # written and applied by its own 保存并关闭, and the sheet states the
        # outcome.  The pane never announces a group-shaped status, and the
        # group vocabulary that used to reach it is gone.
        self.assertNotIn('onStatus?.(translate("relay.apiKeyGroupStaged"));', self.relay)
        self.assertNotIn('onStatus?.(translate("relay.operationFailed"));', self.relay)
        self.assertNotIn('translate("relay.apiKeyGroupStaged")', self.relay)
        self.assertIn("await refreshAccounts();", self.relay)
        # The bridge hands the child the apply it asked for, without a word in
        # the pane's strip.
        relay_manager = (ROOT / "rn/packages/shared/src/ui/RelayAccountManager.tsx").read_text(encoding="utf-8")
        self.assertIn("  applyStagedQuietly: () => Promise<void>;", relay_manager)

    def test_a_child_save_is_disabled_until_it_has_something_to_write(self) -> None:
        """An enabled 保存并关闭 that writes nothing says "there was a change".

        Every child surface whose primary button commits a draft answers the
        same question first: does this window hold a difference from what is
        already saved?  The WebDAV form answers with the patch builder the write
        itself uses (plus the password Core staged), the raw file editor with
        the editor's own draft-versus-baseline report, and 分组管理 in both
        native hosts with its own staged-changes predicate.  One of them losing
        that gate is a live primary button over an untouched form.
        """

        form = self.ui.split('function WebDavSettingsPanel(', 1)[1].split('function WebDavPasswordField(', 1)[0]
        # The button reads the derived fact, and that fact is the write's own
        # comparison rather than a second opinion about what "changed" means.
        self.assertIn('|| !pendingWrite}', form)
        self.assertIn('const pendingWrite = stagedSecretPresent || Object.keys(webDavDetailPatch(values, saved)).length > 0;', form)
        # The secure field stages through Core, so its arrival is a change too —
        # and it has to be state, or the footer would never re-render.
        self.assertIn('setStagedSecretPresent(true);', form)
        self.assertIn('const [stagedSecretPresent, setStagedSecretPresent] = useState(false);', form)

        file_editor = self.ui.split('function FileEditorWorkspace(', 1)[1].split('const NATIVE_ACTION_MENU_ITEM_LIMIT', 1)[0]
        self.assertIn('|| !editorDirty}', file_editor)
        self.assertIn('setEditorDirty(dirty);', file_editor)

        # The browser host's own surfaces follow the same rule: the model
        # picker hands back a list only when something is picked (both native
        # hosts already disable theirs at zero), and a text prompt confirms a
        # value only when it differs from the one it opened on.
        chrome = (ROOT / "rn/packages/shared/src/ui/HostChrome.tsx").read_text(encoding="utf-8")
        self.assertIn('const pending = draft.trim() !== "" && draft !== request.value;', chrome)
        self.assertIn('disabled={!pending}', chrome)
        self.assertIn('compact primary disabled={selected.length === 0}', chrome)

        for source, marker in (
            ((ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift").read_text(encoding="utf-8"),
             "applyButton?.isEnabled = !loading && !saving && hasStagedChanges"),
            ((ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(encoding="utf-8"),
             "auto refresh_apply = [&apply, &has_staged_changes]() { apply.IsEnabled(has_staged_changes()); };"),
        ):
            self.assertIn(marker, source)

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
            '"providers.probeApplyTitle": "改用探测推荐的接口协议?"',
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
            'SETTINGS_RAIL_BODY',
            '<SettingsRail',
            'await flushPendingFields();',
            "<SettingsActionRow",
            'const stagedDomainsForRoute = useCallback((currentSnapshot: { drafts: CoreSnapshot["drafts"] } | undefined): ConfigDomain[] => {',
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
        apply_body = self.ui.split('const applyOutcome = (options?: { silent?: boolean; quiet?: boolean; message?: string }):', 1)[1].split(
            "const autoAppliedKey",
            1,
        )[0]
        self.assertIn('if ((!settingsRoute && !domain) || domain === "logs")', apply_body)
        self.assertIn('settingsRoute || route === "providers-models" || route === "data-management"', apply_body)
        # An Apply is planned from the commit screen, not from the projection:
        # the whole state on every edit is what made a published page wait
        # seconds for a control the native window applies at once.
        self.assertIn("const plan = await readCommitPlan();", apply_body)
        self.assertIn("stagedDomainsForRoute(plan)", apply_body)
        self.assertNotIn("const refreshed = await ipc.snapshot();", apply_body)

    def test_settings_shell_applies_staged_domains_without_an_explicit_footer(self) -> None:
        for marker in (
            'const stagedDomainsForRoute = useCallback((currentSnapshot: { drafts: CoreSnapshot["drafts"] } | undefined): ConfigDomain[] => {',
            'const actionSnapshot = latestSnapshot.current && latestSnapshot.current.revision >= (snapshot?.revision ?? -1)',
            'const routeHasStagedChanges = useCallback((currentSnapshot: CoreSnapshot | undefined): boolean => (',
            'return (["providers_models", "relay_accounts"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
            '|| hasPendingFieldEdits()',
            'const needsDiscardConfirmation = routeHasStagedChanges(current);',
            # Immediate apply: the shell watches the staged domains and applies
            # them after the debounce instead of rendering an Apply button.
            'const autoApplyKey = shell && actionSnapshot && !immediateApplyBlocked',
            # A revision is not an edit: only a domain Core still reports as
            # dirty earns the debounced Apply, so an external read that bumped
            # the revision cannot collect a second commit of the user's own.
            'const dirty = domains.filter((name) => actionSnapshot.drafts[name]?.dirty === true);',
            'return dirty.length === 0 ? "" : `${dirty.join(",")}@${actionSnapshot.revision}`;',
            'if (busy || hasPendingFieldEdits()) return;',
            'const savingMessage = translate("common.saving");',
            'setResult(savingMessage);',
            'void apply({ message: "common.saved" });',
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

    def test_apply_outcome_always_releases_its_busy_state(self) -> None:
        outcome = self.ui.split("const runOutcome = async <T,>(", 1)[1].split(
            "const run = async",
            1,
        )[0]
        self.assertIn("return undefined;\n    } finally {", outcome)
        self.assertIn("activeRuns.current -= 1;", outcome.split("} finally {", 1)[1])

    def test_an_apply_can_never_take_the_window_controls(self) -> None:
        """An Apply is background work; the signature has no lock to pass.

        The panes have no Apply button: the debounced auto-apply, the flush of
        the pane being left, and a child surface's quiet apply are the only
        callers.  Any of them holding the window-wide flag rendered the pane the
        user was looking at entirely disabled — most visibly the pane they had
        just navigated into — while the strip had already cleared to 就绪.  The
        lock is therefore not an option this API offers, so a future caller
        cannot reintroduce it by passing a flag.
        """

        signature = "const applyOutcome = (options?: { silent?: boolean; quiet?: boolean; message?: string }):"
        apply_body = self.ui.split(signature, 1)[1].split("const autoAppliedKey", 1)[0]
        self.assertNotIn("keepControlsEnabled", apply_body)
        # The third argument of `runOutcome` is the lock; an Apply always passes
        # true (kept controls) there, and the wait text is the strip's job.
        self.assertIn(
            'options?.silent ? null : (options?.message ?? "common.saved"), true, true, undefined, options?.quiet === true);',
            apply_body,
        )
        # Every call site states no lock either.
        for index, line in enumerate(self.ui.splitlines()):
            if "apply({" not in line and "apply()" not in line:
                continue
            self.assertNotIn(
                "keepControlsEnabled",
                line,
                f"line {index + 1} lets an Apply hold the pane-wide lock: {line.strip()}",
            )

    def test_provider_key_editor_uses_the_secret_native_control(self) -> None:
        editor = self.ui.split("const keysEditorView =", 1)[1].split(
            "</View>;",
            1,
        )[0]
        value_control = editor.split('keysEditorField("key-value"', 1)[1].split(
            "/>",
            1,
        )[0]
        self.assertIn("<NativeSecretField", value_control)
        self.assertIn("plainText autoCommit", value_control)
        native = (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(encoding="utf-8")
        model_table = self.ui.split('onLayout={({ nativeEvent }) => setModelTableWidth', 1)[1].split(
            "</View>",
            1,
        )[0]
        self.assertIn("modelTableColumns(modelTableWidth, translate)", model_table)
        self.assertIn("style={styles.modelTable}", model_table)

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
        apply_body = self.ui.split('const applyOutcome = (options?: { silent?: boolean; quiet?: boolean; message?: string }):', 1)[1].split(
            'const activateProviderAndRestart',
            1,
        )[0]
        self.assertIn('const applyOnce = (nextRevision: number): Promise<IpcResults["apply"]>', apply_body)
        self.assertIn('if (!isRevisionConflict(reason)) throw reason;', apply_body)
        self.assertIn('const current = await readCommitPlan();', apply_body)
        self.assertIn('const sameDomains = currentDomains.length === domains.length', apply_body)
        self.assertIn('const sameDiskState = domains.every((name) => (', apply_body)
        # One rebase was not enough while a proxy replacement was still
        # landing: the restart itself moves the revision again a moment
        # later, and the write was lost with it.  The retry is bounded, and
        # the disk/domain guards still refuse a draft somebody else changed.
        self.assertIn('const APPLY_ATTEMPTS = 3;', apply_body)
        self.assertIn('let nextRevision = plan.revision;', apply_body)
        self.assertIn('result = await applyOnce(nextRevision);', apply_body)
        self.assertIn('nextRevision = (await readCommitPlan()).revision;', apply_body)
        self.assertIn('if (!sameDomains || !sameDiskState) throw reason;', apply_body)
        # The retries are spent: ask once more whether the write landed before
        # the strip states a save that did not happen.
        self.assertIn('const settled = await readCommitPlan();', apply_body)
        self.assertIn('if (domains.every((name) => !currentDomains.includes(name))) {', apply_body)
        # A conflict that survives every rebase leaves a staged draft and
        # nothing that would try again — the debounce remembers the key it
        # already applied.  The Apply re-arms it once, so the retry the user
        # would have to discover happens by itself; a second failure for the
        # same draft stops there instead of looping.
        self.assertIn('const autoRetriedKey = useRef("");', self.ui)
        self.assertIn("if (autoRetriedKey.current !== autoApplyKey) {", apply_body)
        self.assertIn("autoRetriedKey.current = autoApplyKey;", apply_body)
        self.assertIn('autoAppliedKey.current = "";', apply_body)
        # A different section staying dirty is not evidence this write failed.
        self.assertIn("const currentDomains = stagedDomainsForRoute(current);", apply_body)
        self.assertIn('throw reason;', apply_body)
        # The replacement that moved the revision is still landing; the
        # retry waits for it and reads the revision again rather than
        # resending the one it already lost.
        self.assertIn('const settleBeforeRetry = (attempt: number): Promise<void> => new Promise<void>((resolve) => setTimeout(resolve, 40 * (attempt + 1)));', apply_body)
        self.assertIn('await settleBeforeRetry(attempt);', apply_body)
        # A conflict whose rebase finds this Apply's own domains no longer
        # staged is the pane's own background Apply having already committed
        # the edit. The user asked for a write that exists, so it is reported
        # applied instead of failing with a message about a change made outside
        # the window. That case is answered before the different-draft refusal;
        # it names this Apply's domains rather than `length === 0`, because a
        # different section (the relay draft, kept dirty by its own backlog)
        # staying staged is not evidence that this write failed.
        self.assertIn("if (domains.every((name) => !currentDomains.includes(name))) {", apply_body)
        self.assertIn("if (!sameDiskState || current.revision < plan.revision) throw reason;", apply_body)
        self.assertIn('status: "applied", domains: [...domains]', apply_body)
        # Two Applies for one edit move the revision under each other, and the
        # one that loses reports a save that happened as 更改未生效.  The
        # second caller joins the write already in flight instead.
        self.assertIn("const applyInFlight = useRef<Promise<IpcResults[\"apply\"] | { cancelled: true } | undefined> | undefined>(undefined);", self.ui)
        self.assertIn("if (running) return running;", apply_body)
        self.assertIn("applyInFlight.current = started;", apply_body)
        self.assertIn("if (applyInFlight.current === started) applyInFlight.current = undefined;", apply_body)
        self.assertNotIn('appliedRevision.current', self.ui)

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
            '? translate("providers.probeInlineEgressBlocked")',
            '? translate("providers.probeInlineAuthError")',
            '? translate("providers.probeInlineRateLimited")',
            '? translate("providers.probeInlineUnsupported")',
            'const compactReason = (deepTestIncluded ? degradationLine : "")',
            'const degradationMeasured = deepTestIncluded\n    && (availableCount > 0 || Boolean(stringValue(degradation.verdict)));',
            'const degradationLine = degradationMeasured ? probeDegradationLine(degradation, translate) : "";',
            'const degradationSentence = degradationMeasured ? probeDegradationLine(degradation, translate, true) : "";',
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
            'dispatchWithOutcome("model.add_many", {',
            'const apiKeyID = stringValue(summary.slot_id);',
            'models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, imported: true, imported_name_case: nameCase, api_key_name: apiKeyName, ...(apiKeyID ? { provider_key_id: apiKeyID } : {}), enabled: true, order: 0 }))',
            '<ProtocolPicker providerId={providerId}',
            'function ProtocolPicker(',
            'const mode = stringValue(model.upstream_protocol_mode, "fallback");',
            'const singleSurface = stringList(model.probe_surfaces);',
            'const onlySurface = singleSurface.length === 1 ? singleSurface[0] : "";',
            'if (onlySurface) {',
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
        self.assertIn("private static func responseTimeoutInterval(for method: String, request: Data) -> TimeInterval {", macos_bridge)
        self.assertIn("if method == \"probe\" { return 900 }", macos_bridge)
        # A WebDAV sync crosses the network like the probe, so it earns the same
        # wait instead of the 30 s cap that hid a sync Core was still running.
        self.assertIn('if method == "dispatch", webdavRemoteOperation(in: request) != nil { return 900 }', macos_bridge)
        # A lifecycle dispatch replaces the proxy, which the drain budget lets
        # run for minutes; the 30 s cap expired mid-restart and tore Core down.
        self.assertIn('if method == "dispatch", serviceLifecycleOperation(in: request) != nil { return 900 }', macos_bridge)
        # A usage read answers from a third-party relay, which can take over a
        # minute; the 30 s cap abandoned the read and the pane then reported a
        # relay the user had just chosen as if nothing were selected.
        self.assertIn('if method == "dispatch", onlineUsageReadOperation(in: request) != nil { return 900 }', macos_bridge)
        self.assertIn('private static func onlineUsageReadOperation(in data: Data) -> String? {', macos_bridge)
        self.assertIn('private static func serviceLifecycleOperation(in data: Data) -> String? {', macos_bridge)
        self.assertIn('guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],\n              let params = object["params"] as? [String: Any],\n              let action = params["action"] as? [String: Any],\n              action["domain"] == nil else { return nil }', macos_bridge)
        self.assertIn('return ["service.start", "service.stop", "service.restart", "service.reload"].contains(type) ? type : nil', macos_bridge)
        self.assertIn("timeoutInterval: Self.responseTimeoutInterval(for: metadata.method, request: data)", macos_bridge)
        self.assertIn('const int receive_timeout_ms = slow_remote_operation ? 900000 : 30000;', windows_bridge)
        self.assertIn('bool IsServiceLifecycleOperation(std::string const& request_json) {', windows_bridge)
        self.assertIn('bool IsOnlineUsageReadOperation(std::string const& request_json) {', windows_bridge)
        self.assertIn('method == "probe" || IsWebdavRemoteOperation(request_json) ||', windows_bridge)
        self.assertIn('IsServiceLifecycleOperation(request_json) ||', windows_bridge)
        self.assertIn('IsOnlineUsageReadOperation(request_json);', windows_bridge)
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
            '<ActionButton title={probeTitle} titleWidth="tight" busy={probing} toolTip={(probePresentation.compact ? probePresentation.tooltip : "") || (servesImages ? translate("providers.probeImagesHint") : degradationIncluded ? translate("providers.deepTestHint") : undefined)} disabled={busy && !probing} onPress={probe} />',
            'function probeDegradationLine(degradation: UnknownRecord, translate: Translate, detailed = false): string {',
            'function probeDegradationDetails(degradation: UnknownRecord, translate: Translate): string {',
            'const degradation = asRecord(result?.degradation ?? probe.degradation);',
            'case "matched":',
            'case "mismatch":',
            'case "unavailable":',
            'translate("providers.degradationEngine", { engine: stringValue(engine.name), revision: revision || "?" })',
            'titleWidth="tight"',
            'accessibilityRole="link" accessibilityLabel={probePresentation.compactSentence}',
            'const degradationSentence = degradationMeasured ? probeDegradationLine(degradation, translate, true) : "";',
            'const tooltip = joinProbeSummary([compactSentence, availabilitySentence, degradationSentence]);',
            '? translate("providers.probeInlineReady")',
            '? translate("providers.probeInlinePartial")',
            '? translate("providers.probeInlineRefused")',
            '? translate("providers.probeInlineTimeout")',
            'const uniformStatus = surfaceStatuses.size === 1 ? [...surfaceStatuses][0] : "";',
            '? translate("providers.probeInlineEgressBlocked")',
            '? translate("providers.probeInlineAuthError")',
            '? translate("providers.probeInlineRateLimited")',
            '? translate("providers.probeInlineUnsupported")',
            'const compactReason = (deepTestIncluded ? degradationLine : "")',
            'const degradationMeasured = deepTestIncluded\n    && (availableCount > 0 || Boolean(stringValue(degradation.verdict)));',
            'const degradationLine = degradationMeasured ? probeDegradationLine(degradation, translate) : "";',
            'const degradationSentence = degradationMeasured ? probeDegradationLine(degradation, translate, true) : "";',
            'const compact = [availabilityCount, compactReason].filter(Boolean).join("; ");',
            'translate("providers.probeResultPrefix")',
            'styles.inspectorProbeFinding}',
        ):
            self.assert_ui_has(marker)
        # Only the availability finding may ever change stored routing; a
        # failed check is reported and nothing else.
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
            'case "egress_blocked":',
            'return "providers.probeInlineEgressBlocked";',
            'case "rate_limited":',
            'return "providers.probeInlineRateLimited";',
        ):
            self.assert_ui_has(marker)
        for marker in (
            '"providers.probeResultPrefix": "结果:"',
            '"providers.probeAvailabilityCount": "{available}/{total} 可用"',
            '"providers.probeSummaryUnavailable": "不可用"',
            '"providers.probeSummaryUnreachable": "上游未响应"',
            '"providers.degradationMatched": "未注水"',
            '"providers.degradationMatchedDetail": "核验通过（{target} 的真伪指纹一致）"',
            '"providers.degradationMismatch": "疑似注水"',
            '"providers.degradationMismatchDetail": "核验未通过：{target} 的响应更像 {label}"',
            '"providers.degradationUnknown": "核验存疑"',
            '"providers.degradationUnreachable": "上游未响应"',
        ):
            self.assertIn(marker, self.zh)
        # The result line carries the finding itself: a "总结:" label would only
        # eat the width the pane needs for the whole short result.
        self.assertNotIn("总结", self.zh)
        self.assertIn('"providers.degradationMatched": "Not diluted"', self.en)
        self.assertIn('"providers.degradationMismatch": "Looks diluted"', self.en)
        self.assertIn('"providers.probeSummaryUnreachable": "Upstream did not answer"', self.en)
        self.assertIn('"providers.probeResultPrefix": "Result:"', self.en)
        self.assertIn('"providers.probeAvailabilityCount": "{available}/{total} available"', self.en)
        self.assertIn('"providers.probeInlineRefused": "Refused"', self.en)
        self.assertIn('"providers.probeInlineAuthError": "Key refused"', self.en)
        # A 403 that refused the network has its own word, so the pane never
        # sends a user to a key that the upstream never read.
        self.assertIn('"providers.probeInlineEgressBlocked": "Egress refused"', self.en)
        self.assertIn('"providers.probeInlineEgressBlocked": "出口被拒"', self.zh)
        # A 429 is an answer, not a refusal: it must never borrow the word for
        # "this protocol does not work here".
        self.assertIn('"providers.probeInlineRateLimited": "Rate limited"', self.en)
        self.assertIn('"providers.probeInlineRateLimited": "被限流"', self.zh)
        self.assertIn(
            '| "providers.probeInlineRateLimited"',
            (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8"),
        )
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
            '"providers.deepTest": "探测"',
            '"providers.deepTestHint": "先验可用性，再用 Veridrop 做快速真伪核验"',
            '"providers.degradationUnavailable": "核验不可用"',
            '"providers.degradationError": "核验失败"',
        ):
            self.assertIn(marker, self.zh)
        for marker in (
            '"providers.probe": "Probe"',
            '"providers.deepTest": "Probe"',
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
            # The popup grows away from the mark it belongs to: a tip that
            # states which way it opens is what keeps a right-hand pane's help
            # inside that pane instead of clipped by the column beside it.
            'function HelpTip({ open, text, title, onToggle, align = "right" }: { open: boolean; text: string; title: string; onToggle: () => void; align?: "left" | "right" }): React.JSX.Element {',
            '<NativeButton title="" symbol="help" link plainLink toolTip={title} accessibilityLabel={title} onPress={onToggle} style={styles.helpTipButton} />',
            # The panel is text a screen reader is told about: the mark's own
            # label names the tip, and the sentence it opens is announced when
            # it appears rather than being reachable only by reading the pane.
            '{open ? <View accessibilityRole="text" accessibilityLiveRegion="polite" style={align === "left" ? styles.helpTipPopupLeft : styles.helpTipPopup}><Text style={styles.helpTipText}>{text}</Text></View> : null}',
            # `accessibilityLiveRegion` alone does not reach a desktop screen
            # reader -- RN reads it on Android only, and Windows' leaf sets a UIA
            # LiveSetting without raising the change event.  The imperative
            # announcement is what macOS and Windows actually implement, so the
            # tip must carry it too, and must not announce on the browser host
            # where the live region already works.
            'AccessibilityInfo.announceForAccessibility(text);',
            'if (!open || Platform.OS === "web") return;',
            'helpTipPopupLeft: { position: "absolute", left: 0, bottom: 25, width: 208, zIndex: 4,',
            'const protocolHint = translate(fixed ? "providers.protocolModeFixedHint" : "providers.protocolModeFallbackHint");',
            'accessory={<HelpTip open={tipOpen} text={protocolHint} title={translate("providers.protocolModeHelp")} onToggle={() => setTipOpen((current) => !current)} />}',
            'const [tipOpen, setTipOpen] = useState(false);',
            'const selectMode = (upstream_protocol_mode: string): void => {',
            'const selectProtocol = (upstream_url_surface: string): void => {',
            # The mark rides the picker's own row, after the control, and the
            # label column is the model detail's own (MODEL_INSPECTOR_LABEL_WIDTH).
            'accessory?: React.ReactNode',
            'labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink value={protocol}',
            # The picker's cell drops the row's `flex` when a width is stated,
            # so that width is the size on every host (see
            # test_a_fixed_width_field_states_its_width_without_flex).
            'controlWidth === undefined ? styles.picker : styles.pickerFixed',
            'controlWidth === undefined ? null : { width: controlWidth }]} />{accessory ?? null}</View>',
            # Anywhere else clicks the sheet drawn under the mark and its panel,
            # so the tip closes on the next click outside itself -- and the
            # sheet says it is not a modal surface, so the macOS tab chain
            # keeps walking the controls behind it instead of trapping Tab on
            # the mark (a tip is transient; nothing behind it is unavailable).
            '{open ? <Pressable accessible={false} nativeID="young-router-tab-transparent" onPress={onToggle} style={styles.helpTipDismiss} /> : null}',
            'helpTipDismiss: { position: "absolute", left: -2400, right: -2400, top: -2400, bottom: -2400 }',
            # The mark keeps its 16 pt glyph but owns a 22 pt target: the
            # negative margins give back the extra points, so the glyph does
            # not move while the click area reaches the platform minimum.
            # The mark's own 22 pt target is the gap: the ring inside it is 13 pt
            # and the rest is air, which a negative horizontal margin took away —
            # landing the ring on the last character of 跟随全局.
            'helpTipButton: { width: 22, height: 22, minWidth: 22, minHeight: 22, marginVertical: -3 }',
            'helpTipPopup: { position: "absolute", right: 0, bottom: 25, width: 208',
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
        controls_source = (ROOT / "rn/packages/shared/src/ui/NativeControls.tsx").read_text(encoding="utf-8")
        # Windows draws Segoe's help glyph; the page has no such face, so the
        # ring macOS and Windows draw (`questionmark.circle`) is drawn here too
        # and a bare "?" — a character in the sentence — is not.
        self.assertIn('help: "\\uE897"', controls_source)
        self.assertIn('const WEB_RINGED_GLYPHS: Partial<Record<NonNullable<ButtonProps["symbol"]>, string>> = { help: "?", info: "i" };', controls_source)
        self.assertIn('windowsButtonRing: { width: 13, height: 13, borderRadius: 6.5, borderWidth: 1', controls_source)
        self.assertIn('symbol == "help"', (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(encoding="utf-8"))
        # The tip follows the live mode, and a chosen value answers it.
        self.assert_ui_has('setTipOpen(false);\n    void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_protocol_mode } });')
        self.assert_ui_has('setTipOpen(false);\n    void dispatch("model.patch", { provider_id: providerId, model_id: id, changes: { upstream_url_surface } });')
        # A route Core says answers on one surface only gets no pickers: both
        # would offer a choice the route cannot honour.  The fact stays readable
        # as one value row, with the sentence explaining why on its mark.  Two
        # sentences exist because there are two reasons: a local worker mounts
        # one wire, and a generator answers Images by definition.
        for marker in (
            'const singleSurface = stringList(model.probe_surfaces);',
            'const onlySurface = singleSurface.length === 1 ? singleSurface[0] : "";',
            'if (onlySurface) {',
            'const surfaceLabel = probeSurfaceLabel(onlySurface, translate);',
            'const fixedHint = translate(onlySurface === "openai/images" ? "providers.protocolSurfaceImagesHint" : "providers.protocolSurfaceFixedHint", { surface: surfaceLabel });',
            'translate("providers.protocolSurfaceFixed")',
            'accessibilityLabel={fixedHint}',
        ):
            self.assert_ui_has(marker)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.protocolSurfaceFixed":', locale)
            self.assertIn('"providers.protocolSurfaceFixedHint":', locale)
            self.assertIn('"providers.protocolSurfaceImagesHint":', locale)
        for key in ("providers.protocolSurfaceFixed", "providers.protocolSurfaceImagesHint"):
            self.assertIn(
                f'| "{key}"',
                (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8"),
            )

    def test_a_stopped_provider_greys_its_rows_and_holds_their_switches(self) -> None:
        """One fact, drawn once: the provider is off, so these routes are off.

        A provider that is off serves none of its routes, so its rows in the
        model list read as disabled and each row's own 启用 box keeps the state
        it stores while losing the press.  Rewriting that state instead would
        be the pane editing a configuration the user never touched — and a
        tick that clears itself on open is not a reading of the file.
        """

        # A provider that is off serves none of its routes, so its models read
        # as disabled in the list too — the same fact the inspector states.
        disabled = self.ui.split("const offModelKeys = useMemo(", 1)[1].split("// Rows that cannot materialize", 1)[0]
        self.assertIn("!booleanValue(provider?.enabled, true) || !booleanValue(item.model_enabled, booleanValue(item.enabled, true))", disabled)

        # The row's own 启用 box keeps drawing its stored state and only loses
        # its press: the value prop is untouched, `disabled` carries the
        # provider's state.  Rewriting the value would make the pane forget a
        # choice the user made, and a tick that changed itself on open is not a
        # reading of the configuration.
        self.assert_ui_has("const providerEnabled = booleanValue(provider.enabled, true);")
        self.assert_ui_has('value={booleanValue(model.model_enabled, booleanValue(model.enabled, true))} disabled={busy || !providerEnabled} onValueChange={(model_enabled) =>')
        # The sentence names the precondition, and stands under the switch it
        # explains rather than under the name field after it.
        self.assert_ui_has('{providerEnabled ? null : <Text style={styles.fieldHint}>{translate("providers.providerDisabledHint")}</Text>}')
        inspector = self.ui.split("function ModelInspector(", 1)[1].split("function ContextWindowHelp(", 1)[0]
        switch_row = inspector.index('label={translate("common.enable")} value={booleanValue(model.model_enabled')
        hint_row = inspector.index('translate("providers.providerDisabledHint")')
        name_row = inspector.index('label={translate("providers.publicModel")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} value={modelFieldName}')
        self.assertLess(switch_row, hint_row)
        self.assertLess(hint_row, name_row)
        # The sentence asks for the provider first; it is not a verdict about
        # the route, which the user cannot act on from this row.
        self.assertIn('"providers.providerDisabledHint": "供应商已停用，请先启用供应商，再设置本模型的启用状态。"', self.zh)
        self.assertIn('"providers.providerDisabledHint": "This provider is turned off. Turn the provider back on before setting this model', self.en)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.providerDisabledHint"', locale)

    def test_a_row_that_is_off_still_answers_the_click_that_turns_it_back_on(self) -> None:
        """A parked entry dims; it does not become a row nobody can open.

        Both hosts refuse a `disabledRowKeys` row the selection, which is right
        for a row with nothing to act on.  A provider, model or route that is
        merely switched off is the opposite: the click that selects it is the
        only door to the 启用 box (and, for a route, to the key or model its
        state is parked on).  Handing those to the disabled list left a window
        whose off providers answered nothing, so a provider turned off could
        never be turned back on from its own pane.
        """

        mac_native = MACOS_CONTROLS.read_text(encoding="utf-8")
        # The provider list is subscribed to its own domain, so it computes the
        # secondary rows where it draws them.  The set is the same switched-off
        # providers, handed to secondaryRowKeys as ink rather than to the
        # disabled list a click cannot select.
        provider_list = self.ui.split("const ProviderListTable = React.memo(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("providers.filter((item) => !booleanValue(item.enabled, true)).map(editorIdentifier)", provider_list)
        self.assertIn("secondaryRowKeys={secondaryRowKeys}", provider_list)
        self.assertNotIn("disabledRowKeys", provider_list)
        for state_keys in ("offModelKeys", "offRouteKeys"):
            self.assert_ui_has(f"secondaryRowKeys={{{state_keys}}}")
            self.assertNotIn(f"disabledRowKeys={{{state_keys}}}", self.ui)
        # Secondary ink is ink alone: the refusal still reads the disabled list.
        should_select = mac_native.split(
            "- (BOOL)tableView:(__unused NSTableView *)tableView shouldSelectRow:(NSInteger)row", 1)[1].split("\n}", 1)[0]
        self.assertIn("if ([self isDisabledRow:row]) {", should_select)
        self.assertNotIn("isSecondaryRow", should_select)
        self.assertIn("const bool secondaryRow = std::find(viewProps.secondaryRowKeys.begin(), viewProps.secondaryRowKeys.end(), rowKey)", mac_native)
        self.assertIn(
            "(disabled || secondaryRow || secondary ? NSColor.secondaryLabelColor : NSColor.labelColor)",
            mac_native,
        )
        # The React Windows table paints the same ink and keeps its press.
        self.assertIn(
            "const textColor = alert ? WINDOWS_CONTROL.warning : (disabled ? WINDOWS_CONTROL.textDisabled : (secondaryRow ? WINDOWS_CONTROL.textSecondary : WINDOWS_CONTROL.text));",
            self.native_controls,
        )
        self.assertIn("disabled={disabled || (spanning && !selectableSpanning)}", self.native_controls)

    def test_an_image_route_is_told_its_probe_generates_one_picture(self) -> None:
        """A generator's only honest availability test costs an image.

        The Images wire has no dry run, so the probe really generates one.  The
        button says so before it is pressed, because the user pays for it, and
        a generator carries no 真伪 line: the deep test is not one this route
        can run, and reporting it as "not applicable" beside a working probe
        read as one more thing that failed.
        """

        self.assert_ui_has('(servesImages ? translate("providers.probeImagesHint") : degradationIncluded ? translate("providers.deepTestHint") : undefined)')
        self.assert_ui_has('const degradationMeasured = deepTestIncluded\n    && (availableCount > 0 || Boolean(stringValue(degradation.verdict)));')
        for locale in (self.zh, self.en):
            self.assertIn('"providers.probeImagesHint":', locale)
        self.assertIn(
            '| "providers.probeImagesHint"',
            (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8"),
        )

    def test_provider_probe_button_reports_progress_without_graying_out(self) -> None:
        self.assert_ui_has(
            '<ActionButton title={probeTitle} titleWidth="tight" busy={probing} toolTip={(probePresentation.compact ? probePresentation.tooltip : "") || (servesImages ? translate("providers.probeImagesHint") : degradationIncluded ? translate("providers.deepTestHint") : undefined)} disabled={busy && !probing} onPress={probe} />'
        )
        # The probe keeps its own name while it runs: the button carries the
        # progress as a leading spinner instead of a swapped title.
        self.assert_ui_not_has('title={probing ? translate("providers.probing")')
        self.assert_ui_has(
            "onPress={probe} />{probePresentation.compactSentence ? <Pressable style={styles.inspectorProbeFinding}",
        )
        self.assert_ui_not_has("probeSummaryTrigger")
        self.assert_ui_not_has('styles.probeSummary}')
        # The credential is what a probe exists to test, so nothing the pane
        # believes about the route may grey the button: a route whose key Core
        # just materialized can be probed at once, and a route with nothing to
        # try answers with `providers.probeInlineInvalidConfig` — the sentence
        # that names the missing input — instead of a dead button whose reason
        # the reader has to guess.  Only a write in flight holds it.
        self.assert_ui_not_has("probeReady")
        self.assert_ui_not_has("authenticationReady")
        self.assertNotIn('await flushPendingFields();\n      const before = await ipc.snapshot();', self.ui)

    def test_a_changed_probe_input_clears_the_probe_result(self) -> None:
        """A finding is shown only while it describes the route on screen."""

        # The interface's own result is stored with the inputs it was measured
        # on, so an edited route never shows the verdict of the route it
        # replaced — and the press that asks the route again, and the edit that
        # changes it, both take the previous finding off screen first.
        self.assert_ui_has('const [probeResults, setProbeResults] = useState<Record<string, { inputs: string; result: IpcResults["probe"] }>>({});')
        self.assert_ui_has('const [droppedProbeResults, setDroppedProbeResults] = useState<Record<string, true>>({});')
        self.assert_ui_has('stringValue(model.provider_key_id, stringValue(model.api_key_name)),')
        self.assert_ui_has('stringValue(model.upstream_protocol_mode, "fallback"),')
        self.assert_ui_has('setProbeResults((current) => ({ ...current, [key]: { inputs, result } }));')
        # The probe's own recommended-surface write is not an edit by the user:
        # the finding is re-keyed on the route that write produced, so a newly
        # added model does not hide the verdict the press just measured.
        self.assert_ui_has('const applied = await applyProbedSurface(targetProviderId, targetModelId, nextSurface, options);')
        self.assert_ui_has('setProbeResults((current) => current[key] ? { ...current, [key]: { inputs: nextInputs, result } } : current);')
        self.assert_ui_has('const measuredHere = record !== undefined && record.inputs === inputs;')
        self.assert_ui_has('probeResult: droppedProbeResults[key] === true || (record !== undefined && !measuredHere)')
        self.assert_ui_has('const resultRecord = result === null ? undefined : result as UnknownRecord | undefined;')
        self.assert_ui_has('const probe = result === null ? ({} as UnknownRecord) : resultRecord ?? asRecord(model.probe);')
        self.assert_ui_has('probe: () => probeModel(targetProviderId, targetModelId, inputs),')
        # The button reports this question's progress only: the answer stands
        # the press down before the write it asks for begins, so a probe whose
        # finding has arrived is never left disabled by its own follow-up.
        self.assert_ui_has('setDroppedProbeResults((current) => ({ ...current, [key]: true }));')
        self.assertNotIn('} finally {\n      probingModelKeys.current.delete(key);', self.ui)
        self.assertIn('probingModelKeys.current.delete(key);\n    setProbeActivityRevision((value) => value + 1);\n    setProbeResults(', self.ui)
        # One expression decides both what the button probes and which result it
        # shows, so the two can never disagree about the route; the address and
        # the model name are the pane's own values, a pending edit included.
        self.assert_ui_has('function probeInputFingerprint(providerBaseUrl: string, upstreamModel: string, model: UnknownRecord): string {')
        self.assert_ui_has('{...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model), probeInputFingerprint(providerBaseURL(activeRoute.provider), modelUpstreamDisplay(editorIdentifier(activeRoute.provider), activeRoute.model), activeRoute.model))}')
        self.assert_ui_has('{...modelProbeProps(providerId, editorIdentifier(model), probeInputFingerprint(providerBaseURL(provider), modelUpstreamDisplay(providerId, model), model))}')
        self.assert_ui_not_has('{...modelProbeProps(providerId, editorIdentifier(model))}')
        self.assert_ui_has('{...modelProbeProps(editorIdentifier(activeRoute.provider), editorIdentifier(activeRoute.model), probeInputFingerprint(providerBaseURL(activeRoute.provider), modelUpstreamDisplay(editorIdentifier(activeRoute.provider), activeRoute.model), activeRoute.model))}')
        self.assert_ui_has('{...modelProbeProps(providerId, editorIdentifier(model), probeInputFingerprint(providerBaseURL(provider), modelUpstreamDisplay(providerId, model), model))}')
        self.assert_ui_not_has('{...modelProbeProps(providerId, editorIdentifier(model))}')

    def test_one_key_out_of_many_is_a_radio_list_that_scrolls(self) -> None:
        """Nineteen keys, one answer, and a window that cannot grow to fit them.

        The list was a plain View of checkboxes: an exact-count list read as
        "any number of these", and the rows past the fold could not be reached
        at all.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('key={value}', wizard)
        self.assertIn('onValueChange={() => setProvidedKeySelection(value)}', wizard)
        self.assertIn('<PersistentScrollView style={styles.providerWizardModelListScroll} contentContainerStyle={styles.providerWizardModelList} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">', wizard)
        # The control itself is a radio to a screen reader, and a radio is what
        # the drawn box says.
        self.assertIn('accessibilityRole="radio"', self.native_controls)
        self.assertIn("export function NativeRadio(", self.native_controls)

    def test_a_sign_in_reads_the_account_it_actually_landed_on(self) -> None:
        """Core never writes a second row for one site and address.

        The wizard asked with a pending id and then read that id's resources.
        When the login landed on the station's existing account — which is the
        normal case for signing in again, and the only case after 记住密码 — the
        read targeted a row that was never created, and a sign-in that had just
        succeeded reported 操作失败.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("const landed = relayLoginLandedAccount(accounts, pendingID, candidate, result.username);", wizard)
        self.assertIn("const landedID = landed?.id ?? pendingID;", wizard)
        self.assertIn("await relay.refreshResources(landedID, { force: true })", wizard)
        self.assertIn("setSignedInAccountID(landedID);", wizard)
        self.assertNotIn("relay.refreshResources(pendingID", wizard)

    def test_the_login_step_chooses_an_account_or_adds_one(self) -> None:
        """One account is selected, or one is added: never a wall of 未登录.

        The step read only the id *this run* had signed in, so opening it for a
        station whose accounts the pane behind it listed as 已登录 printed one
        address — and when a sign-in had just landed it listed only that
        account's keys.  Listing every account under that correction was the
        other failure: a signed-out row read as a key source.  The choices are
        the accounts that can serve keys; with none, the step offers the one
        action that changes that state.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("const stationAccounts = useMemo(() => {", wizard)
        self.assertIn("const priority = (account: RelayAccount): number =>", wizard)
        self.assertIn("relayAccountSignedIn(account) ? 1 : 2", wizard)
        # The choices are usable accounts, not every row the station has.
        self.assertIn("const selectableAccounts = useMemo(() => stationAccounts.filter(relayAccountSignedIn), [stationAccounts]);", wizard)
        self.assertIn("const selectedStationAccount = selectableAccounts.find((account) => account.id === selectedAccountID) ?? selectableAccounts[0];", wizard)
        self.assertIn("labels={accountPickerLabels}", wizard)
        self.assertIn("selectableAccounts.length > 1 ? <NativePicker", wizard)
        self.assertIn("const account = selectableAccounts[nativeEvent.index];", wizard)
        self.assertIn("{selectedStationAccount ? <View style={styles.providerWizardAccountRow}>", wizard)
        # With no usable account the card carries 登录 instead of a row of
        # names that cannot serve a key.
        self.assertIn('selectedStationAccount ? null : <NativeButton title={translate("relay.login")} primary compact', wizard)
        # The empty statement names the fact that explains it instead of
        # claiming the read failed.
        self.assertIn("const emptyKeysHint = ((): TranslationKey => {", wizard)
        self.assertIn('if (!selectedStationAccount) return "providers.wizard.loginFirstHint";', wizard)
        self.assertIn("{translate(emptyKeysHint)}", wizard)
        # The run's own id is a priority, not the scope of the list.
        self.assertNotIn("activeRelayAccount", wizard)
        self.assertNotIn("signedInAccountLabel", wizard)
        # And the reuse decision reads the account list it is deciding about,
        # because this window's snapshot can predate the pane behind it.
        self.assertIn("accountsFromSnapshot((await relay.refreshAccounts()) ?? undefined)", wizard)
        self.assertIn(
            'if (step !== "keys" || keyPath !== "login" || isOfficialLoginType) return;',
            wizard,
        )
        zh = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        en = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        for key in ("providers.wizard.loginNoKeysHint", "providers.wizard.loginNoModelsHint", "providers.wizard.loginExpiredHint"):
            self.assertIn(f'"{key}"', zh)
            self.assertIn(f'"{key}"', en)

    def test_the_login_step_can_sign_in_a_second_account(self) -> None:
        """A station with an account already signed in still offers another.

        The reuse shortcut answered "use the account you have" for every press,
        and the key list only appeared once a sign-in had produced keys, so
        after the first account the step had no way to add a second one.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assert_ui_has('const beginRelayLogin = async (signInAnother = false): Promise<void> => {')
        self.assert_ui_has('const reusable = signInAnother ? undefined : existingAccounts.find(relayAccountSignedIn);')
        self.assert_ui_has('onPress={() => { void beginRelayLogin(true); }}')
        # Adding one is the row's own action; the 登录 the empty state carries
        # is not shown beside an account that is already usable.
        self.assertIn('title={translate("relay.loginAnotherAccount")}', wizard)
        # Two accounts are a picker, not a wall of rows; the key list belongs
        # to the selected account.
        self.assert_ui_has('labels={accountPickerLabels}')
        self.assert_ui_has('source.accountID === selectedStationAccount.id')
        self.assert_ui_has('label={`${source.resourceLabel || relaySourceName(source)}${multiplier}`}')
        # A read or the post-login grouping states its wait in the key area;
        # the empty card is not drawn until both have finished.
        self.assert_ui_has('(loginBusy || resourcesBusy || autoGroupingBusy) ? <View style={styles.providerWizardKeyEmpty}>')
        self.assert_ui_has('translate(autoGroupingBusy ? "relay.autoGroupingInProgress" : "relay.keysLoading")')
        self.assert_ui_has('await relay.apiKeyActions?.alignAutoGrouping?.(landedID);')
        self.assert_ui_has('await relay.refreshAccounts();')
        zh = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        en = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        self.assertIn('"relay.loginAnotherAccount": "登录新账号",', zh)
        self.assertIn('"relay.loginAnotherAccount": "Sign In Another Account",', en)

    def test_the_wizard_can_finish_a_provider_without_a_key_or_model(self) -> None:
        """The provider is the row; its station is an association of that row.

        The key step required one provided key before 下一步, and the only
        finish without models still imported that key.  A station has no key
        of its own to import: its accounts and keys are the station's, and the
        provider row that owns the association is what 完成 creates.  Saving
        the provider without a key or model must still bind the station, and
        must never present the station as the thing being saved.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assert_ui_has('const finishProviderOnly = async (): Promise<void> => {')
        provider_only = wizard.split("const finishProviderOnly = async (): Promise<void> => {", 1)[1].split("const goBack", 1)[0]
        self.assertIn("const targetProviderID = providerID || await commitWizardProvider();", provider_only)
        self.assertNotIn("importProvidedKey", provider_only)
        self.assertNotIn("model.add_many", provider_only)
        self.assertIn('const bound = await dispatchWithOutcome("provider.select_relay_station", { provider_id: targetProviderID, station_id: station.id });', provider_only)
        self.assertIn('if (!bound) return;', provider_only)
        self.assertIn('focusProviderAfterWizard(targetProviderID);', provider_only)
        self.assertIn('setValidation(errorMessage(reason, translate));', provider_only)
        # The pane behind the wizard opens the row that was just created,
        # instead of leaving the user on the provider that was selected before.
        # The wizard is its own React root on the native hosts, so the row
        # travels through the shared gate rather than a parent callback.
        self.assert_ui_has('focusProviderAfterWizard(createdID);')
        self.assert_ui_has('const focusID = providerWizardFocusID();')
        self.assert_ui_has('if (!providers.some((item) => editorIdentifier(item) === focusID)) return;')
        self.assert_ui_has('const appliedProviderFocus = useRef("");')
        self.assert_ui_has('if (!focusID || appliedProviderFocus.current === focusID) return;')
        self.assert_ui_has('setSelectedProvider(focusID);')
        gate = (ROOT / "rn/packages/shared/src/ui/providerWizardGate.ts").read_text(encoding="utf-8")
        self.assertIn("let pendingProviderFocusID = \"\";", gate)
        self.assertIn("export function focusProviderAfterWizard(providerID: string): void {", gate)
        self.assertIn("export function providerWizardFocusID(): string {", gate)
        # It is offered only at the step that has a station to associate, and
        # only while no sign-in or read is holding the window.
        self.assert_ui_has('step === "keys" && creatingProvider && !isOfficialLoginType && keyPath === "login" && loginPhase !== "sign-in"')
        self.assert_ui_has('stationForProvider || selectedProvidedChoice || selectedStationAccount')
        self.assert_ui_has('title={translate("providers.wizard.saveProviderOnly")}')
        zh = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        en = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        self.assertIn('"providers.wizard.saveProviderOnly": "仅保存供应商"', zh)
        self.assertIn('"providers.wizard.saveProviderOnly": "Save Provider Only"', en)
        self.assertIn("中转站不会单独成行", zh)
        self.assertIn("station is never a row of its own", en)

    def test_save_provider_only_is_not_offered_for_an_existing_provider(self) -> None:
        """The action saves a new provider; an existing one saves nothing.

        Choosing 选择供应商 edits a row that already exists, so a footer action
        that creates a provider would close the window after writing nothing.
        Both create-only finishes therefore read the same predicate, and the
        save-provider action itself refuses the call rather than trusting the
        footer to be the only caller.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        provider_only = wizard.split("const finishProviderOnly = async (): Promise<void> => {", 1)[1].split("const goBack", 1)[0]
        self.assertIn("const creatingProvider = providerMode === \"new\" && !providerID;", wizard)
        self.assertIn("if (!creatingProvider) return;", provider_only)
        finish_without = wizard.split("const finishWithoutModels = async (): Promise<void> => {", 1)[1].split("const finishProviderOnly", 1)[0]
        self.assertIn("if (!creatingProvider) return;", finish_without)
        self.assert_ui_has('step === "keys" && creatingProvider && !isOfficialLoginType && keyPath === "login" && loginPhase !== "sign-in"')
        # The other create-only finish already had the same guard; the two
        # must not drift apart again.
        self.assert_ui_has('step === "model" && creatingProvider && genericModelStep')

    def test_the_wizard_has_no_relay_station_type(self) -> None:
        """A station is an association of an API provider, not a sibling type.

        The picker used to offer 中转站 beside API 供应商.  A station has no
        provider contract of its own: the provider is the row, and the station
        is reached through the provider's address and 网页登录获取.  Removing
        the sibling type is what keeps the two from being saved independently.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('type WizardType = "api" | "openai"', wizard)
        self.assertNotIn('| "relay"', wizard)
        self.assertNotIn('{ type: "relay"', self.ui)
        self.assertNotIn("providers.wizard.typeRelay", self.zh)
        self.assertNotIn("providers.wizard.typeRelay", self.en)
        # A known station is recognised by the provider's address, and that is
        # the only thing that switches a new provider onto the station login
        # path.  It is not a type the user picks.
        self.assert_ui_has('if (providerMode === "new" && providerType === "api" && !providerID && keyPath === "manual" && stationForProvider) {')
        self.assert_ui_has('const stationForProvider = useMemo(')
        # 选择供应商 resolves the picker option back to the provider record:
        # reading the option object as a provider gave every existing row the
        # default api_key kind, so the step always opened on 手动选择.
        self.assert_ui_has('const firstProvider = providers.find((entry) => editorIdentifier(entry) === first?.value);')
        self.assert_ui_has('existingKeyPath(firstProvider)')
        self.assert_ui_has('setKeyPath(existingKeyPath(existing));')

    def test_the_wizard_retargets_detection_and_model_state_with_the_address(self) -> None:
        """A value probed for one address must not answer for the next one.

        The live relay-family probe, the catalog rows it produced, and the
        picks made from them all belong to one provider address and account.
        Changing either leaves the old state on screen unless the step keys
        each answer by its own origin/account and clears the rest.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('useState<{ origin: string; state: "checking" | RelayType | "unknown" } | undefined>(undefined)', wizard)
        self.assertIn("setTypeDetection({ origin: candidate, state: detected ?? \"unknown\" });", wizard)
        self.assertIn("if (typeDetection?.origin === probeOrigin", wizard)
        self.assertIn("return typeDetection.state;", wizard)
        # A type switch is a new model step, not the previous one with a new
        # type label: its catalog, manual rows, and any fetch still arriving
        # must not cross over.
        type_block = wizard.split("const chooseProviderType = (value: WizardType): void => {", 1)[1].split("useEffect(() => {", 1)[0]
        for marker in (
            "modelFetchRequest.current += 1;",
            "modelFetchInFlight.current = false;",
            "setFetchedModelCandidates([]);",
            "setFetchedModelCapabilities({});",
            "setLaneRoutes({});",
            "setLaneChannels({});",
            "setManualModels([]);",
            "setSelectedManualModelIDs([]);",
        ):
            self.assertIn(marker, type_block)
        model_reset = wizard.split("// Everything the model step built belongs to this key selection", 1)[1].split("}, [activeKeySelection", 1)[0]
        self.assertIn("}, [activeKeySelection, keyPath, providedKeySelection, providerID, selectedStationAccountID]);", wizard)
        for marker in (
            "modelFetchRequest.current += 1;",
            "modelFetchInFlight.current = false;",
            "setFetchedModelCandidates([]);",
            "setLaneRoutes({});",
            "setSelectedModels([]);",
            "setManualModels([]);",
            "setModelFetchState(\"idle\");",
        ):
            self.assertIn(marker, model_reset)

    def test_editing_a_wizard_field_clears_its_previous_validation(self) -> None:
        """The error belongs to the value that failed, not to the field forever.

        A required-field or duplicate-name message used to stay on screen while
        the person typed the correction, and a successful sign-in's message
        followed the wizard back to the provider step.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        for name in ("updateProviderBaseURL", "updateProviderName"):
            block = wizard.split(f"const {name} = ", 1)[1].split("};", 1)[0]
            self.assertIn('setValidation("");', block)
        self.assertIn('onChangeText={(value) => { setKeyName(value); setValidation(""); }}', wizard)
        self.assertIn('onChangeText={(value) => { setModelName(value); setValidation(""); }}', wizard)
        self.assertIn('onChangeText={(value) => { setUpstreamModel(value); setValidation(""); }}', wizard)
        go_back = wizard.split("const goBack = (): void => {", 1)[1].split("};", 1)[0]
        self.assertIn("setLoginFeedbackMessage(undefined);", go_back)
        self.assertIn("setLoginFeedbackMessage(undefined);", wizard.split("const chooseProviderType = ", 1)[1].split("};", 1)[0])
        self.assertIn("setLoginFeedbackMessage(undefined);", wizard.split("const chooseExistingProvider = ", 1)[1].split("};", 1)[0])

    def test_a_failed_wizard_finish_reports_on_the_wizard(self) -> None:
        """A dispatch that throws is still a press the person has to see fail.

        下一步 and both create-only finishes used to unwind through `finally`
        alone.  An exception left the footer saying 就绪/previous status while
        nothing was written, indistinguishable from a press that did nothing.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        go_next = wizard.split("const goNext = async (): Promise<void> => {", 1)[1].split("const advanceWizard", 1)[0]
        self.assertIn("catch (reason) {", go_next)
        self.assertIn("setValidation(errorMessage(reason, translate));", go_next)
        finish_without = wizard.split("const finishWithoutModels = async (): Promise<void> => {", 1)[1].split("const finishProviderOnly", 1)[0]
        self.assertIn("catch (reason) {", finish_without)
        self.assertIn("setValidation(errorMessage(reason, translate));", finish_without)
        provider_only = wizard.split("const finishProviderOnly = async (): Promise<void> => {", 1)[1].split("const goBack", 1)[0]
        self.assertIn("setValidation(errorMessage(reason, translate));", provider_only)
        official = wizard.split("const startOfficialLogin = async (): Promise<void> => {", 1)[1].split("const cancelOfficialLogin", 1)[0]
        self.assertIn("catch (reason) {", official)
        self.assertIn("setValidation(errorMessage(reason, translate));", official)

    def test_an_existing_provider_does_not_report_a_model_save_it_did_not_write(self) -> None:
        """零个模型 for an existing row is a close, not 配置已保存.

        A catalog or official account already had its row before the wizard
        opened.  Finishing with no pick writes nothing there, so the window
        closes through the same path as 关闭 instead of claiming a save.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        service = wizard.split("if (isServiceCatalogType) {", 1)[1].split("const draftModelName", 1)[0]
        self.assertIn("if (picked.length === 0) {", service)
        self.assertIn("onClose();", service)
        official = wizard.split("if (isOfficialLoginType && keyPath === \"login\") {", 1)[1].split("// A keyless service", 1)[0]
        self.assertIn("const wrote = officialProviderCreated.current", official)
        self.assertIn('|| (officialLoginAttempted.current && officialStatus === "signed_in");', official)
        self.assertIn("if (!wrote) {", official)

    def test_a_dismissed_wizard_prunes_the_station_its_login_created(self) -> None:
        """Signing in is not the same as finishing the provider.

        A login stores its station before the wizard creates the provider row.
        Closing the window at that point used to leave a station and its
        account with no provider anywhere naming them.  The wizard records the
        newly created station id and hands it to Core's reachability check on
        unmount; Core removes only what no provider reaches.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("const pendingLoginStationIDs = useRef(new Set<string>());", wizard)
        self.assertIn("const knownStationIDs = new Set([", wizard)
        self.assertIn("if (landedStationID && !knownStationIDs.has(landedStationID)) {", wizard)
        self.assertIn("pendingLoginStationIDs.current.add(landedStationID);", wizard)
        self.assertIn("relay.commit(\"prune_orphans\", { station_ids: pending })", wizard)
        # The startup repair is the same reachability rule, in Core.
        service = (ROOT / "young_router/core/service.py").read_text(encoding="utf-8")
        self.assertIn("def prune_orphan_relay_stations(", service)
        self.assertIn("if station_ids is None:", service)
        self.assertIn("candidates -= reached", service)
        self.assertIn('"type": "station_remove"', service)
        self.assertIn("store.prune_orphan_relay_stations()", service)

    def test_the_keys_step_reads_a_chosen_accounts_resources_once(self) -> None:
        """A second signed-in account starts with an empty key list.

        Switching accounts must not leave the first account's keys on screen
        behind a 刷新 button; the chosen account's own resources are read once,
        and a failed read is stated by the account instead of retried forever.
        """

        self.assert_ui_has("const autoRefreshedAccountIDs = useRef(new Set<string>());")
        self.assert_ui_has('if (autoRefreshedAccountIDs.current.has(selectedStationAccount.id)) return;')
        self.assert_ui_has('autoRefreshedAccountIDs.current.add(selectedStationAccount.id);')
        self.assert_ui_has("void relay.refreshResources(selectedStationAccount.id)")

    def test_closing_the_wizard_cancels_its_sign_in_browser(self) -> None:
        """The wizard owns the native sign-in window it opened.

        A dismissed wizard used to leave that browser running with nothing
        left to accept its answer; the one surface that can cancel it is the
        window that started it.
        """

        self.assert_ui_has("const loginInFlight = useRef(false);")
        self.assert_ui_has("loginInFlight.current = true;")
        self.assert_ui_has("if (loginInFlight.current) {")
        self.assert_ui_has("setupLoginRequest.current += 1;")
        self.assert_ui_has("native.cancelRelayLogin();")
        self.assert_ui_has("relay.commit(\"prune_orphans\", { station_ids: pending })")

    def test_the_model_rosters_share_one_checkbox_rule(self) -> None:
        """Every roster toggles a pick the same way, so the lists cannot drift."""

        self.assert_ui_has("const setModelChecked = (name: string, checked: boolean): void => {")
        self.assert_ui_has("onValueChange={(checked) => setModelChecked(name, checked)}")
        self.assert_ui_has("const toggleAllModels = (names: readonly string[]): void => {")
        self.assertNotIn('setSelectedModels((current) => checked ? (current.includes(name) ? current : [...current, name]) : current.filter((item) => item !== name)); setValidation("");', self.ui)

    def test_the_station_picture_keeps_its_own_shape_inside_the_dialog(self) -> None:
        """A 560x820 display drawn at 100% of a wide card overflows the window.

        Its top and bottom were clipped away and the wizard behind showed
        through where the picture should have been.
        """

        chrome = (ROOT / "rn/packages/shared/src/ui/HostChrome.tsx").read_text(encoding="utf-8")
        self.assertIn('{ aspectRatio: mirror.width / mirror.height, width: undefined, alignSelf: "center" }', chrome)
        self.assertIn('browserFrame: { height: 520, maxHeight: "70vh" as never, overflow: "hidden", backgroundColor: "#000" },', chrome)

    def test_the_wizard_reads_the_detection_it_asked_for(self) -> None:
        """A detection's answer belongs to the dispatch that asked for it.

        The snapshot slot it also lands in is shared with every relay action,
        so the pane's own account refresh can replace it before the wizard reads
        it -- measured on lv as 无法识别中转站类别 for a station whose
        /api/status had just answered.
        """

        self.assert_ui_has('const direct = asRecord(staged.action_summary).detected_type;')
        self.assert_ui_has('if (direct === "newapi" || direct === "sub2api") return direct;')
        # The shared slot stays as the fallback for a Core that predates this.
        wizard = self.ui.split("const detectRelayType = useCallback(", 1)[1].split("}, [enqueueDispatch, refresh]);", 1)[0]
        self.assertIn("await refresh()", wizard)

    def test_the_wizard_creates_nothing_before_its_finish(self) -> None:
        """A wizard that has not been finished has no provider in the pane.

        A relay sign-in stores the station account before 完成, so the provider
        was raised as soon as 密钥 showed a station account and applied on the
        spot.  That row then answered the wizard's own name — the URL fills it in
        — as 供应商名称已存在, and the pane listed a provider for a wizard nobody
        finished.  The account does not need it: 密钥 matches accounts by origin,
        and 完成 binds the row it creates to the station in the same pass.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertNotIn("ensureRelayProviderForStation", wizard)
        self.assertNotIn("ensuredProvider", wizard)
        self.assertNotIn("applyProviders", self.ui)
        login = wizard.split("const beginRelayLogin = async (signInAnother = false): Promise<void> => {", 1)[1]
        login = login.split("const refreshStationResources", 1)[0]
        self.assertNotIn('dispatchWithOutcome("provider.add"', login)
        self.assertNotIn('dispatchWithOutcome("provider.select_relay_station"', login)
        # One create, on 完成, and nothing on the steps before it.
        commit = wizard.split("const commitWizardProvider = async (): Promise<string | undefined> => {", 1)[1]
        commit = commit.split("const createKey", 1)[0]
        self.assertEqual(wizard.count('dispatchWithOutcome("provider.add"'), 1)
        self.assertIn('const next = await dispatchWithOutcome("provider.add", {', commit)
        provider_step = wizard.split("if (step === \"provider\") {", 1)[1].split('if (step === "keys") {', 1)[0]
        self.assertNotIn("provider.add", provider_step)
        self.assertNotIn("provider.select_relay_station", provider_step)
        # A name and address the pane already lists are that provider, so the
        # URL-filled name cannot dead-end its own wizard.
        self.assertIn("const existingAtAddress = providerType === \"api\"", provider_step)
        self.assertIn("if (existingAtAddress && providerAuthKind(existingAtAddress) === \"api_key\") {", provider_step)
        self.assertIn("function providerAtAddress(providers: UnknownRecord[], baseURL: string, name: string): UnknownRecord | undefined {", self.ui)
        # An account with no provider row still answers the family question and
        # still lists its keys, because both read the account, not the row.
        self.assertIn("const accountType = stationAccounts.find((account) => account.type === \"newapi\" || account.type === \"sub2api\")?.type;", wizard)
        self.assertIn("const stationAccounts = useMemo(() => {", wizard)
        self.assertIn("await relay.refreshResources(landedID, { force: true });", wizard)

    def test_a_cleanup_tombstone_is_confirmed_by_the_host_not_by_a_pane(self) -> None:
        """Core can drop an account no pane is showing.

        A provider delete releases its station inside Core, so the account and
        its station row are gone before any surface could draw the per-station
        重试清理 row — the tombstone stayed, and the keychain entry survived every
        later delete of the same site.  The window that is open confirms every
        tombstone, whether or not a pane shows the station it names.
        """

        self.assert_ui_has("const pending = pendingCredentialCleanups(snapshot)")
        self.assert_ui_has(".filter((cleanup) => !cleanupConfirmedAccounts.current.has(cleanup.accountID));")
        self.assert_ui_has("if (cleanupConfirmedAccounts.current.has(cleanup.accountID)) continue;")
        self.assert_ui_has("await native.clearRelayCredentials(cleanup.accountID).catch(() => undefined);")
        self.assert_ui_has("""            "credential_cleanup_confirm",
            { id: cleanup.accountID, kind: cleanup.kind },
            "relay_accounts",""")
        # One attempt per window keeps a host that cannot erase from looping,
        # and the tombstone stays for the pane's own retry.
        self.assert_ui_has("cleanupConfirmedAccounts.current.add(cleanup.accountID);")
        # Serially: five tombstones fired at once each read the same revision,
        # and the ones that lost it left their credentials behind for good.
        self.assert_ui_has("for (const cleanup of pending) {")
        self.assert_ui_has("cleanupConfirmedAccounts.current.delete(cleanup.accountID);")

    def test_a_wizard_whose_provider_was_deleted_starts_creating_again(self) -> None:
        """The row can be deleted while the wizard window is open.

        The pane's − applies a delete behind 配置供应商, and a discarded draft
        takes its rows with it.  The wizard then held a selection that named
        nothing: 密钥 answered 请填写必填项后继续 over a finished form, with no
        field left to fill, and 完成 had no provider to write to.  Its own name
        and address are still in its state, so it becomes the create it would
        have been without that row — or 供应商, when those are gone as well.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('if (providerMode !== "existing" || selectedProvider) return;', wizard)
        fallback = wizard.split('if (providerMode !== "existing" || selectedProvider) return;', 1)[1].split("}, [providerMode", 1)[0]
        self.assertIn('setProviderMode("new");', fallback)
        self.assertIn("setProviderSelection(PROVIDER_WIZARD_NEW_PROVIDER);", fallback)
        self.assertIn("if (providerName.trim() && providerBaseURL.trim()) return;", fallback)
        self.assertIn('setStep("provider");', fallback)
        self.assertIn('stepRef.current = "provider";', fallback)
        # 完成 states why it cannot create; the empty name used to return before
        # the one check that says so.
        commit = wizard.split("const commitWizardProvider = async (): Promise<string | undefined> => {", 1)[1]
        commit = commit.split("const createKey", 1)[0]
        self.assertLess(
            commit.index("if (!providerStepValid()) return undefined;"),
            commit.index("const name = providerName.trim();"),
        )

    def test_a_provided_key_is_filed_on_the_slot_it_becomes(self) -> None:
        """A station resource's label is not a provider key.

        The wizard filed the provided-key routes on the resource's station
        label, which names no slot in the provider it just created: Core
        refused the whole draft at Apply (`model_provider_key_missing`) after
        the window had closed, and 完成 read as a press that did nothing.  The
        slot is staged first and the routes name its id and name; the fragile
        post-pass that patched each added row is gone with it.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('const next = await dispatchWithOutcome("provider.import_relay_key", {', wizard)
        self.assertIn("const providedKey = usingProvidedKey && selectedProvidedChoice", wizard)
        self.assertIn("const keyNameForModels = usingProvidedKey", wizard)
        # The reference is the slot, never the station's label.
        self.assertNotIn("api_key_name: usingProvidedKey ? selectedProvidedChoice?.resourceLabel", wizard)
        self.assertNotIn('dispatchWithOutcome("model.select_relay_resource"', wizard)
        # The reply, not a search through the snapshot, names what to file on:
        # the old pass guessed the new row and bailed silently when it missed.
        self.assertIn("const id = stringValue(summary.slot_id);", wizard)
        self.assertIn("const name = stringValue(summary.api_key_name);", wizard)
        # And a refusal names the key and the model it is about.
        self.assertIn('const values = { key: stringValue(issue.key), model: stringValue(issue.model) };', self.ui)
        self.assertIn('"validation.modelProviderKeyMissing": "该模型选择的供应商密钥「{key}」不可用。"', self.zh)
        self.assertIn(r'"validation.modelProviderKeyMissing": "The key \"{key}\" this model selects is unavailable."', self.en)

    def test_finishing_without_a_model_is_its_own_button(self) -> None:
        """A wizard may register a supplier without a single route.

        The generic model step's 完成 refused an empty pick with a sentence, so
        a provider whose roster is not worth adding yet could not be created at
        all.  Now 完成 writes the picks it shows (and is not lit when there are
        none), and 不添加模型 is the action that creates the provider, its key,
        and its station binding while leaving the routes for 获取模型 later.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        # The step whose 完成 writes the rows above it: not a catalog's create,
        # and not an official account's roster (that one may be skipped whole).
        self.assertIn('const genericModelStep = !isServiceCatalogType && !(isOfficialLoginType && keyPath === "login");', wizard)
        self.assertIn("const modelPicksEmpty = selectedModels.length === 0", wizard)
        self.assertIn("const finishWithoutModels = async (): Promise<void> => {", wizard)
        self.assertIn('{step === "model" && creatingProvider && genericModelStep', wizard)
        self.assertIn('title={translate("providers.wizard.finishWithoutModels")}', wizard)
        # It runs the same one create as 完成, and the key the user signed in
        # for is kept even when no route names it yet.
        finish = wizard.split("const finishWithoutModels = async (): Promise<void> => {", 1)[1].split("const goBack", 1)[0]
        self.assertIn("const targetProviderID = providerID || await commitWizardProvider();", finish)
        self.assertIn("await importProvidedKey(targetProviderID, selectedProvidedChoice)", finish)
        self.assertIn('onStatus(translate("providers.wizard.complete"));', finish)
        # The lit primary that would close over no write stays unlit.
        self.assertIn("(step === \"model\" && genericModelStep && modelPicksEmpty)", wizard)
        for locale, sentence in (
            (self.zh, '"providers.wizard.finishWithoutModels": "不添加模型"'),
            (self.en, '"providers.wizard.finishWithoutModels": "Finish Without Models"'),
        ):
            self.assertIn(sentence, locale)

    def test_the_model_pane_states_no_empty_roster_and_no_keyless_fetch(self) -> None:
        """A blank model table is the empty state; only its actions are gated.

        The pane drew 还没有模型 plus a hint for every provider, including a
        relay station whose account held no keys: the sentence described work
        the row could not do, and 获取模型 could only fail.  Neither sentence is
        drawn now.  The table's own headers and the ＋ stay, and the fetch row
        appears once a configured or relay key, or a signed-in login account,
        exists.
        """

        self.assertIn("const providerHasModelCredential = useMemo(() => {", self.ui)
        self.assertIn("choice.kind === \"account\" || choice.kind === \"relay\" || choice.state?.configured === true", self.ui)
        self.assertIn("provider && providerHasModelCredential ? <View style={styles.tableBottomRow}>", self.ui)
        self.assertNotIn("providers.modelsEmpty", self.ui)
        self.assertNotIn("providers.modelsEmptyHint", self.ui)

    def test_the_fetch_press_sends_only_the_listing(self) -> None:
        """获取模型 asks for one roster; the station refresh it ran first is gone.

        That pre-step read every key's own catalog before the selected key's
        listing — 24 requests and almost six seconds on a 19-key account, all
        to relearn what the document already carries.  Core resolves the
        credential from the slot (one station read of its own only when the
        slot carries none), so the press sends the listing and nothing else.
        """

        self.assertNotIn("await relay.refreshResources(relaySource.accountID)", self.ui)
        self.assertIn('const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";', self.ui)
        self.assertIn('const next = await dispatchWithOutcome(action, payload, "providers_models", true);', self.ui)

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
        mac_button_spec = (ROOT / "rn/packages/shared/src/ui/macos/NativeButtonNativeComponent.ts").read_text(encoding="utf-8")
        self.assertIn("busy?: WithDefault<boolean, false>;", mac_button_spec)
        self.assertIn("busy === true ? <ActivityIndicator", self.native_controls)
        # A symbol-only button has no title to keep beside a leading wheel, so
        # the wheel takes the glyph's place: drawing both put a refresh arrow
        # behind a spinner inside a 24 pt bezel, which is what a reader saw in
        # the Qoder account row while its worker was answering.
        self.assertIn("{symbol && !symbolTrailing && !(busy === true && !showsTitle) ? <ButtonGlyph symbol={symbol} ink={ink} /> : null}", self.native_controls)
        self.assertIn("{symbol && symbolTrailing && !(busy === true && !showsTitle) ? <ButtonGlyph symbol={symbol} ink={ink} /> : null}", self.native_controls)
        # The button asks one wheel for its slot instead of a host's progress
        # view, so each host draws the wait it has (`BusyWheel`).
        self.assertIn("{busy === true ? <BusyWheel ink={ink} /> : null}", self.native_controls)
        # Every loading button reports its own progress and keeps its label;
        # the disabled state is left for controls that merely wait on a sibling.
        for marker in (
            # The wizard's own progress stays pressable; the second clause is
            # content, not busy: 完成 has nothing to write with no model picked.
            'busy={processing} disabled={(wizardBusy && !processing) || (step === "model" && genericModelStep && modelPicksEmpty)}',
            'busy={loginBusy} disabled={(wizardBusy && !loginBusy)',
            # The toolbar row is the wheel alone while a listing runs, and the
            # control that started it returns with the answer: neither a second
            # wheel nor a button with nothing to offer appears.
            '{modelFetchState === "loading" ? <NativeBusySpinner label={translate("providers.wizard.fetchingModels")} /> : <NativeButton title={translate("providers.wizard.refreshModels")}',
            'busy={fetchModelsBusy} disabled={(busy && !fetchModelsBusy)',
            'busy={probing}',
            'busy={saving} disabled={saving || target === undefined || !editorDirty}',
            'busy={designateBusy}',
            'busy={serviceBusy} disabled={snapshot === undefined || (busy && !serviceBusy)}',
            'busy={cooldownClearPending}',
            'busy={pendingAction === "import"}',
            'busy={pendingAction === "export"}',
            'busy={testing} disabled={saving}',
            'busy={syncing} disabled={saving || !values.enabled}',
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
            # The wizard's own 下一步 guard also blocks the second delivery of one
            # press, because the button is the window's default button.
            "if (processing || advanceInFlight.current || stepRef.current !== step) return;",
            # The model listing's guard is a ref rather than the render's
            # `modelFetchState`, because a caller that just set that state
            # would read a stale closure and re-enter.
            "if (modelFetchInFlight.current) return;",
            "if (fetchModelsBusy) return;",
            "if (cooldownClearPending) return;",
            "if (relayAddBusy) return;",
            "if (authPending !== undefined) return;",
        ):
            self.assert_ui_has(marker)
        # A listing belongs to the credential it was asked for.  Selecting
        # another key hands the button back and drops the answer that is still
        # in flight, instead of holding 获取模型 until the old station round trip
        # ends -- the new key could not be listed before the old one finished.
        for marker in (
            "const fetchModelsGeneration = useRef(0);",
            "if (id === fetchKeyIDRef.current) return;",
            "if (request !== fetchModelsGeneration.current) return;",
            "if (request === fetchModelsGeneration.current) setFetchModelsBusy(false);",
        ):
            self.assert_ui_has(marker)
        # No loading button renames itself any more.
        for marker in (
            'title={processing ? translate("providers.wizard.creating")',
            'title={serviceBusy ? translate("service.starting")',
            'title={loginBusy ? translate("relay.stepSignIn")',
        ):
            self.assert_ui_not_has(marker)

    def test_the_page_draws_the_apps_own_wheel_in_the_slot_it_reserves(self) -> None:
        """A browser wait is this app's wheel, at 12 pt, not the system's 20 pt ring.

        React Native for Web's `ActivityIndicator` paints a 20 pt system ring
        inside the 12 pt slot a busy button reserves, so the ring spilled over
        the bezel: the 中转站关联 account row drew a 56 pt box with a ring in it
        while the same row read 24 pt at rest.  The page therefore draws
        `AppKitBusySpinner`'s own wheel (`WebBusyWheel`), and the geometry below
        is the Swift frame geometry -- a page wheel that drifted from it would
        be a third kind of wait in one application.
        """

        # One wait per branch: macOS mounts the native view, the page draws the
        # same wheel in CSS, Windows keeps its one busy affordance (AGENTS.md).
        self.assertIn('if (Platform.OS === "web") return <WebBusyWheel ink={ink} accessibilityLabel={accessibilityLabel} style={style} />;', self.native_controls)
        self.assertIn('return <ActivityIndicator accessibilityLabel={accessibilityLabel} size="small" color={ink} style={[styles.busySpinner, style]} />;', self.native_controls)
        self.assertIn('return <AppKitBusySpinner accessibilityLabel={label} running style={[styles.busySpinner, style]} />;', self.native_controls)
        # The wheel's own frame: twelve graded spokes, and its box is the slot
        # the caller reserved (`BUSY_SPINNER_SIZE`), never a size of its own.
        for marker in (
            "const BUSY_WHEEL_SPOKES = 12;",
            "const BUSY_WHEEL_RIM_INSET = 0.75;",
            "const BUSY_WHEEL_INNER_RATIO = 0.42;",
            "const BUSY_WHEEL_STROKE_RATIO = 0.3;",
            'style={[styles.webWheel, { width: size, height: size }, WEB_WHEEL_TURN, style]}',
            "animationTimingFunction: `steps(${BUSY_WHEEL_SPOKES}, end)`",
        ):
            self.assertIn(marker, self.native_controls)
        # The numbers are the Swift wheel's, so a change to either side has to
        # be a change to both.
        for marker in (
            "public static let stepCount = 12",
            "let outer = size / 2 - 0.75",
            "let inner = outer * 0.42",
            "path.lineWidth = max(1.1, outer * 0.3)",
        ):
            self.assertIn(marker, self.macos_leaf)

    def test_provider_selection_and_new_models_keep_independent_stable_state(self) -> None:
        self.assert_ui_has('onSelectionChange={(key) => { if (key) onSelect(key); }}')
        self.assert_ui_has('const selectProviderRow = useCallback((key: string): void => {\n    setSelectedProvider(key);\n    setSelectedModel(undefined);\n    setProviderSourceModel(undefined);\n  }, []);')
        self.assert_ui_has('selectedKey={selectedProviderId}')
        self.assert_ui_has('const pendingModelIds = useRef<{ providerId: string; ids: Set<string> } | undefined>(undefined);')
        self.assertNotIn('knownModelIdsByProvider', self.ui)
        self.assertNotIn('Promise.all(added.map(', self.ui)
        self.assert_ui_has('disabled={busy && !probing}')

    def test_new_models_use_zero_order_without_coercing_zero_to_one(self) -> None:
        # A model added from the pane is committed by the shell right away, so
        # it starts under the localized placeholder name (kept unique with a
        # numeric suffix) instead of an empty name that would block the commit
        # with a validation error nobody can act on.
        self.assert_ui_has('const base = translate("providers.newModel");')
        self.assert_ui_has('const name = uniquePlaceholderName(models.map((item) => stringValue(item.model_name ?? item.name)), base);')
        # ＋ creates the row enabled, so the user does not have to enable what
        # they just made; the placeholder name is what the pane marks.
        self.assert_ui_has('model: { name, upstream_model: name, enabled: true, order: 0, ...(inheritedKey ?')
        self.assert_ui_has('models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, imported: true, imported_name_case: nameCase, api_key_name: apiKeyName, ...(apiKeyID ? { provider_key_id: apiKeyID } : {}), enabled: true, order: 0 }))')
        self.assert_ui_has('value={String(displayedOrder)}')
        self.assert_ui_has('label={translate("providers.order")}')
        self.assert_ui_has('label={translate("providers.followMultiplier")}')
        # The switch is offered wherever a rate is known — a relay group's
        # multiplier or an account's own published catalog — so it asks about
        # the rate, not about which kind of provider the route uses.
        self.assert_ui_has('const followsServiceRate = usesRelayKey || providerService(provider) !== undefined;')
        self.assert_ui_has('const followedRate = usesRelayKey ? relayMultiplier : rateNumber(serviceRate);')
        self.assert_ui_has('const canFollowMultiplier = followsServiceRate && followedRate !== undefined;')
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
        self.assert_ui_has('providerInspector: { width: 320, minWidth: 260, maxWidth: 360')
        self.assertNotIn("<ScrollView contentContainerStyle={styles.providerEditorScroll}><ProviderEditor", self.ui)
        self.assert_ui_has("providerEditorContent: { flex: 1, minHeight: 0 }")
        # The inspector pane's header row is the workspace's first row, so it
        # must not carry a top inset: 供应商: <name> lines up with the left
        # column's toolbar row (the 添加向导… button) instead of sitting below it.
        self.assert_ui_has("providerEditorScrollContent: { paddingLeft: 0, paddingRight: 16, paddingBottom: 12, gap: 6 }")
        self.assertNotIn("providerEditorScrollContent: { paddingTop", self.ui)
        self.assert_ui_has('persistentScrollIndicator: { position: "absolute", width: 0, height: 0 }')
        self.assert_ui_has('return <PersistentScrollView style={styles.providerEditorContent} contentContainerStyle={styles.providerEditorScrollContent} showsVerticalScrollIndicator nestedScrollEnabled>')
        self.assert_ui_has('<ProviderIdentityFields')
        self.assert_ui_has("<NativePersistentScrollIndicator style={styles.persistentScrollIndicator} />")
        self.assert_ui_has('showsHorizontalScrollIndicator={false}')
        # Windows draws its own overlay thumb: the composition host leaves the
        # RNW platform theme empty, so the native WinUI scrollbar painted no
        # brushes at all.
        self.assert_ui_has('showsVerticalScrollIndicator={false}\n      scrollEventThrottle={scrollEventThrottle ?? 16}')
        self.assert_ui_has('persistentScrollThumb:')
        self.assert_ui_has('Animated.View')
        # The model step scrolls in exactly one place, and it is the roster box:
        # the step body is a plain column, so the pane cannot grow a second
        # scroller beside the list, and the box's own frame carries the border
        # (a border drawn by scrolled content is cut where that content is cut).
        self.assert_ui_has("providerWizardModelPane: { flex: 1, minHeight: 0, width: 100%".replace("100%", '"100%"') + ", justifyContent: \"flex-start\", paddingBottom: 8 }")
        self.assertNotIn("providerWizardModelScrollContent", self.ui)
        self.assert_ui_has('providerWizardModelListFrame: { flex: 1, minHeight: 120, width: "100%", borderWidth: 1, borderColor: systemColors.separator, backgroundColor: systemColors.textBackground, overflow: "hidden" }')
        self.assert_ui_has('providerWizardModelListScroll: { flex: 1, minHeight: 0, width: "100%" }')
        self.assert_ui_has('<View style={styles.providerWizardModelListFrame}><PersistentScrollView style={styles.providerWizardModelListScroll} contentContainerStyle={styles.providerWizardModelList} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">')
        self.assert_ui_has("providerWizardModelList: { paddingHorizontal: 8, paddingTop: 5, paddingBottom: 10, gap: 1 }")
        self.assert_ui_has('<PersistentScrollView style={styles.providerWizardModelListScroll} contentContainerStyle={styles.providerWizardModelList} showsVerticalScrollIndicator keyboardShouldPersistTaps="handled">')
        self.assert_ui_has('providersLayout: { flex: 1, minWidth: 0, minHeight: 0, flexDirection: "row", gap: 12 }')
        self.assert_ui_has('providerModelColumns: { flex: 1, minHeight: 0, flexDirection: "row", gap: COLUMN_GAP }')
        self.assert_ui_has('inspectorContent: { paddingLeft: 0, paddingRight: 6')
        self.assertNotIn("inspectorContent: { paddingTop", self.ui)
        self.assert_ui_has("providerLeftColumn: { flex: 1, minWidth: 0, minHeight: 0, gap: 6 }")
        self.assert_ui_has("providerListPane: { width: 120, minWidth: 120, maxWidth: 120")
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 112 }]}')
        # The model list is the upstream model, the public model, and the rate
        # the route follows.  The settings window's default width leaves that
        # pane about 296 pt, and each column's floor is what its own content
        # asks for: the upstream ids these routes serve read in full (the child
        # row's indent is part of the cell), the public name may be ellipsized,
        # and 倍率 holds a header and ``×0.25``.  The two name columns share
        # whatever a wider window adds, and a pane narrower than the floors
        # scales all three down together, because a column squeezed to a few
        # pixels is not a column.  A trailing column that does not fit is
        # reduced to nothing by the native layout, which would make 倍率 read as
        # a column that never existed.  顺序 stays out of it: a model's order orders the routes
        # that share its public name, and these rows are grouped by key, so the
        # column compared numbers from sequences that have nothing to do with
        # each other.  倍率 is the other way round — a rate is a property of the
        # row itself, so two rates read down this pane are two prices that can
        # be compared — and it sits last because it is read after the model it
        # prices.
        self.assert_ui_has('onLayout={({ nativeEvent }) => setModelTableWidth')
        self.assert_ui_has('modelTableColumns(modelTableWidth, translate)')
        self.assert_ui_has('const MODEL_TABLE_MINIMUMS = [124, 88, 54] as const;')
        self.assertLessEqual(124 + 88 + 54, 296)
        self.assert_ui_has('compact firstColumnHorizontalPadding={0} preserveColumnWidths')
        self.assertNotIn('label: translate("common.order"), width: 60', self.ui)
        self.assertNotIn("modelOrderText(providerId, item)", self.ui)
        self.assert_ui_has('rows.push({ key: `key:${keyID}`, cells: [group.label], spanning: true });')
        # One cell builder for both row kinds, so an ungrouped row and a grouped
        # one cannot disagree about the rate or the name they state.
        self.assert_ui_has('const modelCells = (item: UnknownRecord): string[] => {')
        self.assert_ui_has('const multiplier = relaySourceForKey(modelProviderKeyState(item, provider ?? {}), relaySources)?.multiplier;')
        self.assert_ui_has('modelRateText(workspaceService, stringValue(item.upstream_model).trim(), multiplier),')
        self.assert_ui_has('for (const item of ungrouped) rows.push({ key: editorIdentifier(item), cells: modelCells(item) });')
        self.assert_ui_has('for (const item of group.models) rows.push({ key: editorIdentifier(item), cells: modelCells(item) });')
        self.assert_ui_has('columns={variant === "inline"')
        self.assert_ui_has('cellHorizontalPadding={6}')
        self.assert_ui_has('firstColumnHorizontalPadding={0}')
        self.assert_ui_has('label={translate("providers.keyName")}')
        self.assert_ui_has('NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}')
        self.assert_ui_has('label={translate("providers.provider")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink')
        self.assert_ui_has('label={translate("providers.protocolMode")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink')
        self.assert_ui_has('pickerShrink: { minWidth: 0 }')
        self.assert_ui_has('allowShrink && styles.pickerShrink')
        self.assertNotIn('providerKeyGrid:', self.ui)
        self.assertNotIn('providerKeyList:', self.ui)
        self.assert_ui_has('modelListPane: { flex: 1, minWidth: 0 }')
        self.assert_ui_has('providerWorkspace: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('return <ProviderWorkspaceDraftContext.Provider value={providerDraftProjection}><View style={styles.providersLayout}>')
        self.assert_ui_has('<View style={styles.providerLeftColumn}>')
        self.assert_ui_has('<TablePane wide style={styles.routeTablePane}')
        self.assert_ui_has('<View style={styles.providerInspector}>')
        self.assertEqual(1, self.ui.count('<View style={styles.providerInspector}>'))
        self.assertNotIn('viewMode === "routes" ? <View style={styles.providerWorkspace}', self.ui)
        self.assertNotIn('routeWorkspaceWithInspector', self.ui)
        self.assert_ui_has('routeTablePane: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('onSelectionChange={(key) => selectRouteTableRow(key)}')
        # The routes table leads with the upstream model — the model a route
        # actually calls — then names its provider, key, and order, and its
        # columns come from the pane's own width: the two name columns were
        # fixed at 160 and 150 beside a trailing column that took the rest of
        # the window, so a wide pane was mostly blank while 密钥名 stayed cut
        # off.
        # The width comes from a node that only exists in this branch: the pane
        # itself is reused across tabs, and a reused node is observed once, when
        # it mounted, before it carried the handler — so the columns stayed at
        # their floors.
        self.assert_ui_has('<View style={styles.routeTableMeasure} onLayout={({ nativeEvent }) => setRouteTableWidth(nativeEvent.layout.width)}><NativeTable columns={routeTableColumns(routeTableWidth, translate)} rows={routeRows}')
        self.assert_ui_has('routeTableMeasure: { flex: 1, minWidth: 0, minHeight: 0 }')
        self.assert_ui_has('const ROUTE_TABLE_MINIMUMS = [150, 96, 176, 48] as const;')
        self.assert_ui_has('function routeTableColumns(width: number, translate: Translate): Array<{ label: string; width: number }> {')
        self.assert_ui_has('const labels = [translate("providers.upstream"), translate("providers.provider"), translate("providers.providerKey"), translate("common.order")];')
        self.assertIn('const keyWidth = minimums[2] + Math.ceil(slack * 0.68);', self.ui)
        self.assert_ui_has('const [routeTableWidth, setRouteTableWidth] = useState(0);')
        self.assertNotIn('ROUTE_TABLE_SPACER_COLUMN', self.ui)
        self.assertNotIn('{ label: translate("providers.provider"), width: 84 }', self.ui)
        self.assert_ui_has(r'cells: [`\t${modelUpstreamDisplay(editorIdentifier(entry.provider), entry.model) || translate("common.notAvailable")}`, providerDisplayName(entry.provider), modelProviderKeyLabel(entry.model, entry.provider, translate, undefined, relaySources, providerCredentials(entry.provider, translate)), order]')
        self.assertNotIn('providers.orderSource', self.ui)
        self.assertNotIn('providers.effectiveOrder', self.ui)
        self.assert_ui_has('const activeRouteGroupUsesMultiplier = activeRouteGroup.some((entry) => modelOrderMode(entry.model) === "relay_multiplier");')
        self.assertNotIn('const displayRoutes = useMemo', self.ui)
        self.assert_ui_has('key: routePublicModelRowKey(group.name)')
        self.assert_ui_has('spanning: true')
        self.assert_ui_has(r'cells: [`\t${modelUpstreamDisplay(editorIdentifier(entry.provider), entry.model)')
        self.assert_ui_has('rows={routeRows} secondaryRowKeys={offRouteKeys} alertRowKeys={alertRouteKeys} selectedKey={selectedPublicModel !== undefined ? routePublicModelRowKey(selectedPublicModel) : (selectedRoute ?? "")} compact preserveColumnWidths selectableSpanningRowKeys={selectableRouteGroupKeys} onSelectionChange={(key) => selectRouteTableRow(key)}')
        self.assert_ui_has('<ProviderListTable translate={translate} selectedProviderId={providerId} providerNameDrafts={providerNameDrafts} validationIssues={validationIssues} onSelect={selectProviderRow} />')
        provider_list = self.ui.split("const ProviderListTable = React.memo(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn('rows={rows} secondaryRowKeys={secondaryRowKeys} alertRowKeys={alertRowKeys} selectedKey={selectedProviderId} compact firstColumnHorizontalPadding={0} onSelectionChange=', provider_list)
        self.assert_ui_has('rows={modelRows} secondaryRowKeys={offModelKeys} alertRowKeys={alertModelKeys} selectedKey={selectedModelRowKey} compact firstColumnHorizontalPadding={0} preserveColumnWidths onSelectionChange=')
        self.assert_ui_has('columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths')
        self.assert_ui_has('rows={tableRows}')
        select_route = self.ui.split('const selectRoute = useCallback', 1)[1].split('const chooseViewMode', 1)[0]
        self.assertLess(select_route.index('if (!selected) return;'), select_route.index('setSelectedRoute(routeId);'))
        self.assert_ui_has('providerSourceModel ? <ProviderEditor key={`provider:${editorIdentifier(activeRoute.provider)}`} provider={activeRoute.provider}')
        self.assert_ui_has('model={activeRoute.model}')
        self.assert_ui_has('onProviderClick={() => setProviderSourceModel(editorIdentifier(activeRoute.model))}')
        self.assert_ui_has('setSelectedRoute(`${destinationProviderId}:${activeRoute.deploymentID}`)')
        self.assert_ui_has('secondaryRowKeys={offModelKeys}')
        self.assert_ui_has('secondaryRowKeys={offRouteKeys}')
        self.assertNotIn('secondaryCellKeys={routeSecondaryCellKeys}', self.ui)
        self.assertNotIn('<ScrollView contentContainerStyle={styles.inspectorContent}>', self.ui)
        self.assertNotIn('native.window.open("relay-accounts")', self.ui)
        self.assertNotIn('serviceProviderRows', self.ui)
        self.assertNotIn('renderRelayManager', self.ui)
        self.assertNotIn('serviceProviderTab', self.ui)

    def test_a_public_model_group_is_the_name_its_routes_state(self) -> None:
        """One name is one settings surface; another case is another model.

        `minimax-m3.1-flash` and `MiniMax-M3.1-Flash` are two deployment sets in
        the Router, so the pane draws the names the document states instead of
        folding a case onto another and showing two settings surfaces for one
        group.  The import is where a catalog label becomes the lower-case name
        a client types; after that a rename is the user's spelling.
        """

        self.assert_ui_has('import { groupRoutesByPublicModel, importNeedsNameCase, publicModelKey } from "./publicModelNames";')
        self.assert_ui_has('const routeGroups = useMemo(() => groupRoutesByPublicModel(routes), [routes]);')
        # The selection, the row key, and the group a route belongs to all read
        # the name itself, so every control in the pane addresses one group.
        self.assert_ui_has('return `${ROUTE_PUBLIC_MODEL_PREFIX}${publicModelKey(publicModel)}`;')
        self.assert_ui_has('const activeRouteGroup = activeRoute ? routes.filter((entry) => publicModelKey(entry.publicModel) === publicModelKey(activeRoute.publicModel)) : [];')
        self.assert_ui_has('const activePublicGroup = selectedPublicModel === undefined')
        self.assert_ui_has('? undefined\n    : routeGroups.find((group) => publicModelKey(group.name) === publicModelKey(selectedPublicModel));')
        self.assert_ui_has('if (publicModelKey(stringValue(model.model_name, stringValue(model.name))) !== key) continue;')

        # The grouping itself is executed by tests/test_rn_public_model_names.mjs;
        # that is where "one name, one group" is proved rather than spelled.
        self.assertIn("test_rn_public_model_names.mjs", (ROOT / "rn/package.json").read_text(encoding="utf-8"))
        names = (ROOT / "rn/packages/shared/src/ui/publicModelNames.ts").read_text(encoding="utf-8")
        self.assertIn("export function publicModelKey(name: string): string {", names)
        self.assertIn("return name.trim();", names)
        self.assertNotIn("toLocaleLowerCase", names)
        self.assertNotIn("preferredPublicSpelling", names)
        self.assertIn("const name = publicModelKey(entry.publicModel);", names)
        self.assertIn("groups.set(name, { name, entries: [entry] });", names)
        self.assertIn("export function importNeedsNameCase(names: readonly string[]): boolean {", names)

    def test_an_import_asks_which_spelling_it_files(self) -> None:
        """Upper case in a roster is a question, and its answer is one per import.

        A catalog label with upper case in it is a choice the user makes, not a
        rule this app applies: the pane asks once per import, and Core files
        every row of that import under the answer (``public_names.
        imported_public_name``).  Both answers are real — 改为小写 leaves clients
        one spelling, 保留原样 imports the catalog's own — and a roster that is
        already lower case is never asked about.
        """

        # One question, asked by the two paths that see the roster before it is
        # filed (获取模型 and a service catalog pick) and by the wizard's step.
        self.assert_ui_has('async function importNameCaseAnswer(native: NativeLeafAdapter, translate: Translate, names: readonly string[]): Promise<ImportNameCase> {')
        self.assert_ui_has('if (!importNeedsNameCase(names)) return "lower";')
        self.assert_ui_has('title: translate("providers.importNameCaseTitle"),')
        self.assert_ui_has('confirmLabel: translate("providers.importNameCaseLower"),')
        self.assert_ui_has('cancelLabel: translate("providers.importNameCaseOriginal"),')
        self.assert_ui_has('return lower ? "lower" : "original";')
        self.assert_ui_has('const nameCase = await importNameCaseAnswer(native, translate, selectedModels);')
        self.assert_ui_has('const nameCase = await importNameCaseAnswer(native, translate, chosen);')
        self.assert_ui_has('const importNameCase = await importNameCaseAnswer(native, translate, selectedImportNames);')
        # The wizard's 完成 creates the account-backed entries with the picks
        # its model step listed, so the catalog and the add are one act; the
        # pane's 获取模型 still asks the case question for its own import.
        self.assert_ui_has('const next = await dispatchWithOutcome("service_provider.add", {')
        self.assert_ui_has('models: kept,')
        self.assert_ui_not_has('createdCatalog')
        self.assert_ui_not_has('public_name_case: createdNameCase')
        # The answer travels with the pick that asked it.
        self.assert_ui_has('imported_name_case: nameCase,')
        self.assert_ui_has('models: chosen, public_name_case: nameCase })')
        self.assert_ui_has('imported_name_case: importNameCase })),')
        self.assert_ui_has('imported_name_case: importNameCase, ...modelRecordCapabilityChanges(fetchedModelCapabilities[name]) })),')
        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn('name_case=model.pop("imported_name_case", "lower"),', domain)
        self.assertIn('name_case = import_name_case(data.get("public_name_case"))', domain)
        # A create that omits the list still imports the account's whole
        # roster; the wizard's picks are resolved by the adapter's own entries,
        # and an explicit empty list still creates an entry with no routes.
        self.assertIn('raw_models = self._managed_catalog_models(kind)', domain)
        self.assertIn('self._managed_catalog_models(kind, selected=catalog_picks)', domain)
        public_names = (ROOT / "young_router/public_names.py").read_text(encoding="utf-8")
        self.assertIn('IMPORTED_NAME_CASES = ("lower", "original")', public_names)
        self.assertIn('return "original" if str(value or "").strip().lower() == "original" else "lower"', public_names)
        # Both answers are words the user reads, in both languages.
        for locale in (self.zh, self.en):
            self.assertIn('"providers.importNameCaseTitle"', locale)
            self.assertIn('"providers.importNameCaseMessage"', locale)
            self.assertIn('"providers.importNameCaseLower"', locale)
            self.assertIn('"providers.importNameCaseOriginal"', locale)

    def test_a_picked_catalog_row_is_an_import_and_a_typed_row_is_not(self) -> None:
        """The pane says where a name came from; Core settles the spelling.

        A pick from 获取模型 or the wizard's listing arrives with the label the
        chooser showed, so the row carries ``imported`` and Core files its public
        name under the answer the import asked for (lower case by default) while
        keeping the upstream id exactly as the station spells it.  A typed name
        (the wizard's manual pair, its draft row) is the user's own and carries
        nothing, so it stays as written.
        """

        self.assert_ui_has('.map((name) => ({ name, upstream_model: name, imported: true, imported_name_case: importNameCase })),')
        self.assert_ui_has('.map(({ name, upstream_model }) => ({ name, upstream_model })),')
        self.assert_ui_has('models: selectedModels.map((upstreamModel) => ({ name: upstreamModel, upstream_model: upstreamModel, imported: true, imported_name_case: nameCase,')
        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn('if explicit_bool(model.pop("imported", None)):', domain)
        self.assertIn('name_case=model.pop("imported_name_case", "lower"),', domain)
        # A relay import and a service-catalog pick build their own rows, so they
        # file the same spelling without crossing IPC.
        self.assertIn('name = imported_public_name(preferred or _public_name_for_lane_id(model_id), name_case=name_case)', domain)
        self.assertIn('public_model = imported_public_name(source_model)', domain)

    def test_routes_view_public_model_rows_open_a_settings_surface(self) -> None:
        """A public model is one clickable row with its own limits and order list."""
        self.assert_ui_has('const ROUTE_PUBLIC_MODEL_PREFIX = "route-public-model:";')
        self.assert_ui_has('function routePublicModelRowKey(publicModel: string): string')
        self.assert_ui_has('selectableSpanningRowKeys={selectableRouteGroupKeys}')
        self.assert_ui_has('onSelectionChange={(key) => selectRouteTableRow(key)}')
        # The click maps the row key back to the public model, not to a route.
        select_public = self.ui.split("const selectRouteTableRow = useCallback", 1)[1].split("const chooseViewMode", 1)[0]
        self.assertIn('rowKey.startsWith(ROUTE_PUBLIC_MODEL_PREFIX)', select_public)
        self.assertIn('rowKey.slice(ROUTE_PUBLIC_MODEL_PREFIX.length)', select_public)
        # The row key carries the name, and the group that answers it is the one
        # the pane edits -- a group is one name, and the import already stated
        # the spelling every route of it carries.
        self.assertIn('const group = routeGroups.find((entry) => publicModelKey(entry.name) === publicModelKey(publicModel));', select_public)
        self.assertIn('setSelectedPublicModel(group.name);', select_public)
        self.assertIn('setSelectedRoute("");', select_public)

        inspector = self.ui.split("function PublicModelInspector(", 1)[1].split("function ModelInspector(", 1)[0]
        # One surface owns the rename and the context window; the route order
        # list lives in the routes table, not here.
        self.assertIn('dispatchSnapshot("public.model_patch", { public_model: group.name, changes: { name: renamed } })', inspector)
        self.assertIn('translate("providers.contextWindow")', inspector)
        self.assertNotIn("routes.reorder_group", inspector)
        self.assertNotIn("publicModelRouteRow", inspector)
        self.assertNotIn("styles.publicModelRoutes", inspector)
        # The window is declared for the *group*, so this pane is where it is
        # written: one commit lands the number on every route of the public
        # model, and the field keeps the registry default as its placeholder
        # hint and a help mark that names both halves of the pair the model
        # detail shows.  A per-route editor is what this replaces — a single
        # route carrying its own window makes the group's number depend on
        # which route happened to answer.
        self.assertIn('dispatch("public.model_patch", { public_model: group.name, changes: { max_input_tokens: next.trim() } })', inspector)
        self.assertIn('placeholder={contextHint}', inspector)
        # The hint states only the number an empty field falls back to: the
        # recommendation is the mark's own line, and the tagged pair this
        # placeholder used to spell out needed ~241 pt where the control column
        # has ~175, so it truncated mid-number.
        self.assertIn('translate("providers.publicModelDefaultHint", { value: publicModelTokensText(customContext ?? defaultContext, translate) })', inspector)
        self.assertIn('publicModelDefault(modelContexts, savedPublicName)', inspector)
        self.assertNotIn("publicModelCeiling", inspector)
        self.assertIn('accessory={<ContextWindowHelp recommended={defaultContext} translate={translate} />}', inspector)
        self.assertNotIn("max_output_tokens", inspector)
        # Arriving from a model detail keeps a link back to that model, and the
        # header is the provider editor's own shape: the title takes the row and
        # the link sits on the trailing edge.  A route count beside the title is
        # gone for good — it restated the rows the table already lists and took
        # the width the title needs (the pane's own record rendered as its first
        # character) — and the link states its action in two words with the
        # model it returns to in its tooltip, because the full sentence as a
        # label took the same width.
        self.assertIn('backLabel && onBackToModel', inspector)
        self.assertIn('title={translate("providers.backToModelShort")}', inspector)
        self.assertIn('toolTip={translate("providers.backToModel", { model: backLabel })}', inspector)
        self.assertIn('<View style={styles.providerEditorHeader}>', inspector)
        self.assertIn('<Text numberOfLines={1} style={styles.providerEditorHeading}>{group.name}</Text>', inspector)
        self.assertLess(inspector.index('providerEditorHeading}>{group.name}'), inspector.index('providers.backToModelShort'))
        self.assertIn('style={styles.providerReturnToModel} /> : null}', inspector)
        self.assertNotIn('title={translate("providers.backToModel", { model: backLabel })}', inspector)
        self.assertNotIn("publicModelRouteCount", inspector)
        self.assertNotIn("publicModelRouteCount", self.ui)
        self.assertNotIn("publicModelRouteCount", self.zh)
        self.assertNotIn("publicModelRouteCount", self.en)
        self.assertNotIn("breadcrumbProvider}>{translate(\"providers.routes\")", inspector)

        # The model detail states the group's pair read-only, directly below
        # 公开模型 — the field the numbers belong to — and 设置 opens the
        # public-model pane that owns the write.  A field here would make one
        # route of a group disagree with the others.
        model_detail = self.ui.split("function ModelInspector(", 1)[1].split("// The protocol-mode sentence is a tip", 1)[0]
        self.assertIn('publicModelCustomLimit(providers, publicModelName, "max_input_tokens")', model_detail)
        # The row states one number — this public model's own window, falling
        # back to the recommendation while it declares nothing — and the value
        # pi.dev recommends rides the help mark beside it.
        self.assertIn('publicModelDefault(modelContexts ?? {}, publicModelName)', model_detail)
        self.assertIn('{publicModelTokensText(declaredWindow, translate)}', model_detail)
        self.assertIn('const declaredWindow = customWindow !== undefined ? customWindow : recommendedWindow;', model_detail)
        self.assertNotIn('contextWindowPairText', self.ui)
        self.assertIn('title={translate("providers.publicModelSettings")}', model_detail)
        self.assertIn('toolTip={translate("providers.publicModelSettingsHint")}', model_detail)
        self.assertIn('disabled={busy || !onOpenPublicModel} onPress={() => onOpenPublicModel?.()} style={styles.modelWindowLink}', model_detail)
        # The row's own mark carries the recommendation the line no longer
        # states; the pane that always edits the number is where the help
        # lives, so the read-only copy explains it with the same control.  It
        # rides the value row rather than a field's accessory slot: this row is
        # a read-only statement, not an input with a mark beside it.
        self.assertIn('<ContextWindowHelp recommended={recommendedWindow} translate={translate} />', model_detail)
        self.assertNotIn('accessory={<ContextWindowHelp', model_detail)
        self.assertIn('function ContextWindowHelp({ recommended, translate }', self.ui)
        # One row of the pane's own grid, like every neighbouring field: the
        # label, then the value as the control with 设置 and the mark beside it.
        # The value is short enough (one number, not a tagged pair) to share the
        # control column with those two, so splitting it onto a row of its own —
        # which left the number unlabelled under the label and pushed the two
        # controls to the pane's far edge — is the regression this guards.
        window_row = model_detail.split('{publicModelName && !servesImages ? ', 1)[1].split('{routeRate ? ', 1)[0]
        self.assertEqual(1, window_row.count('styles.formRow'))
        self.assertIn('<Text numberOfLines={1} style={styles.modelInspectorLabel}>{translate("providers.contextWindow")}</Text>', window_row)
        self.assertIn('<View style={styles.modelWindowValue}>', window_row)
        self.assertLess(window_row.index('modelInspectorLabel'), window_row.index('modelWindowValue'))
        # A route that only generates images gets no row at all: a generator
        # holds no conversation, so a window is not a fact about it.  Core
        # states the modality; the pane must not infer it from the name.
        self.assertIn('const servesImages = booleanValue(model.serves_images);', model_detail)
        self.assertIn('{publicModelName && !servesImages ? <View style={styles.formRow}>', self.ui)
        # 倍率 follows the public window, before the route's own fields: it is
        # the number the list's 倍率 column prices this row with and the one
        # 跟随倍率 sorts by, so it is stated through that column's own reader
        # rather than through a second reading of the catalog.  A rate no read
        # has produced keeps no row — the label alone would read as a rate of
        # nothing.
        rate_row = model_detail.split('{routeRate ? ', 1)[1].split('<PickerField label={translate("providers.provider")}', 1)[0]
        self.assertEqual(1, rate_row.count('styles.formRow'))
        self.assertIn('<Text numberOfLines={1} style={styles.modelInspectorLabel}>{translate("providers.modelRate")}</Text>', rate_row)
        self.assertIn('{routeRate}</Text>', rate_row)
        self.assertIn('const routeRate = modelRateText(service, upstreamModelID, relayMultiplier);', model_detail)
        for absent in ("modelWindowHeader", "modelWindowHeaderActions"):
            self.assertNotIn(absent, self.ui)
        # No commit and no input: the window is one number for the whole group.
        self.assertNotIn("max_input_tokens: next.trim()", model_detail)
        self.assertNotIn('placeholder={windowHint}', model_detail)
        self.assertNotIn("windowTipOpen", model_detail)
        self.assertNotIn("windowHint", model_detail)
        # The row sits directly under 公开模型 and above 供应商, and it is not
        # pushed below 协议方式: the number is a property of the public name,
        # not of how the route is called.
        self.assertLess(model_detail.index('label={translate("providers.publicModel")}'), model_detail.index('providers.publicModelSettingsHint'))
        self.assertLess(model_detail.index('providers.publicModelSettingsHint'), model_detail.index('<PickerField label={translate("providers.provider")}'))
        self.assertIn('onOpenPublicModel', model_detail)
        # The breadcrumb's second segment names the model this detail edits —
        # the upstream model the routes table's own row leads with — and the
        # 设置 button beside 公开上下文 is the pane's one way into the public
        # model: the segment is a reading, so it is plain text with no link and
        # no press, because a pressable segment read as a second entrance the
        # user can neither reach a destination from nor tell apart from one.
        self.assertIn('<Text numberOfLines={1} style={styles.inspectorHeading}>{displayLabel(upstreamName, translate("common.notAvailable"))}</Text>', model_detail)
        self.assertNotIn('displayLabel(modelName, translate("providers.unnamedModel"))', model_detail)
        self.assertNotIn('tooltip={upstreamName} style={styles.inspectorHeading}', model_detail)
        # The navigation is a real button (设置) that opens the very group the
        # routes table lists: the pane is keyed by the display name the drafts
        # project, not by the raw field value a rename left behind.  The window
        # itself is edited in the pane it opens, so the row states it read-only.
        self.assertIn('title={translate("providers.publicModelSettings")}', model_detail)
        self.assertIn('setViewMode("routes"); selectRouteTableRow(routePublicModelRowKey(publicModel));', self.ui)
        self.assertIn('const publicModel = modelDisplayName(providerId, model).trim();', self.ui)
        self.assertIn('const publicModel = activeRoute.publicModel.trim();', self.ui)
        self.assert_ui_has('dispatchSnapshot={dispatchWithOutcome} modelContexts={modelContexts} bindingIssue={bindingIssueFor(')
        # Both entrances to the public-model pane record their origin, and the
        # recorded route is what the breadcrumb returns to.
        self.assertIn('const [publicModelReturn, setPublicModelReturn] = useState<{ routeKey: string; label: string }>();', self.ui)
        self.assertIn('setPublicModelReturn({ routeKey: activeRoute.key,', self.ui)
        self.assertIn('setPublicModelReturn({ routeKey: originRouteKey,', self.ui)
        self.assertIn('selectRoute(publicModelReturn.routeKey);', self.ui)
        self.assert_ui_has('backLabel={publicModelReturn?.label} onBackToModel={returnToPublicModelOrigin}')

        # 上游上下文 states the window the route's own upstream publishes,
        # directly under the 上游模型 field it belongs to, and it is a reading:
        # the number is the upstream's, so there is no input and no commit —
        # only a mark that says where it came from.  A route whose upstream
        # published nothing keeps no row, so a blank never reads as a zero.
        upstream_row = model_detail.split('label={translate("providers.upstream")}', 1)[1].split('<View style={styles.orderEditorRow}>', 1)[0]
        self.assertIn('const upstreamContextWindow = numberValue(model.upstream_context_window, 0) || undefined;', model_detail)
        self.assertIn('{upstreamContextWindow !== undefined ? <View style={styles.formRow}>', upstream_row)
        self.assertIn('<Text numberOfLines={1} style={styles.modelInspectorLabel}>{translate("providers.upstreamContextWindow")}</Text>', upstream_row)
        self.assertIn('<Text numberOfLines={1} style={styles.modelWindowText}>{publicModelTokensText(upstreamContextWindow, translate)}</Text>', upstream_row)
        self.assertIn('translate("providers.upstreamContextWindowHelp")', upstream_row)
        # No field, no commit, and no edit affordance: the row is not a setting.
        self.assertNotIn('TextField label={translate("providers.upstreamContextWindow")}', model_detail)
        self.assertNotIn("upstream_context_window:", model_detail)
        self.assertIn('"providers.upstreamContextWindow": "上游上下文"', self.zh)
        self.assertIn('"providers.upstreamContextWindow": "Upstream context"', self.en)
        self.assertIn('| "providers.upstreamContextWindow"', (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8"))

        # The AppKit wrapper forwards the prop: a wrapper that lists its props
        # by hand is where a new native prop silently disappears otherwise.
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        table_wrapper = appkit_controls.split("export function AppKitTable(", 1)[1].split("export function AppKitTextEditor(", 1)[0]
        self.assertIn("selectableSpanningRowKeys", table_wrapper)

    def test_each_account_column_is_measured_inside_the_panes_budget(self) -> None:
        """The 账号 / 状态 / 余额 columns fit the inspector together.

        Measuring each column on its own is not enough: a 208 pt address beside
        an 81 pt `Signed out` and an 84 pt balance is 373 pt of cells in about
        304, and a table given more than its pane takes the deficit out of
        every column in proportion — which is how `$1234.56` came out as
        `$1234.…` and `Signed out` as `Signed…`.  The address is the one column
        whose value is repeated below the list, so it takes what the other two
        leave.
        """

        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        ui = self.ui
        # Both lists budget the account column against the same 304 pt.
        self.assertIn("const RELAY_ACCOUNT_COLUMNS_WIDTH_BUDGET = 304;", relay)
        self.assertIn("const AUTH_ACCOUNT_COLUMNS_WIDTH_BUDGET = 304;", ui)
        self.assertIn(
            "RELAY_ACCOUNT_COLUMNS_WIDTH_BUDGET - statusColumnWidth - balanceColumnWidth",
            relay,
        )
        self.assertIn(
            "AUTH_ACCOUNT_COLUMNS_WIDTH_BUDGET - authStatusColumnWidth",
            ui,
        )
        # The status column is measured, not a literal: the 74 pt cell left
        # 58 pt of text, which elided both English states to `Signed…`.
        self.assertIn("const RELAY_STATUS_COLUMN_MIN_WIDTH = 56;", relay)
        self.assertIn("const AUTH_STATUS_COLUMN_MIN_WIDTH = 56;", ui)
        self.assertNotIn('width: 74', relay)

    def test_the_account_lists_minus_is_the_sign_out(self) -> None:
        """One action, one control: − ends the account it is selected on.

        The list's − used to remove an account while a second 退出登录 button in
        the editor signed the same account out, so the same decision had two
        controls and the row's own removal read as something that happened to a
        different thing.  An account is one row of this list, so ending it is
        removing that row — the station pane's own rule, on the same table.
        """

        account_block = self.ui.split('{kind === "openai" ? <View style={styles.officialAccountSection}>', 1)[1].split('</View> : null}', 1)[0]
        # The list keeps its ＋ and its −, and − is the destructive one.
        self.assertIn('symbol="plus"', account_block)
        self.assertIn('symbol="minus" compact destructive', account_block)
        # − reads the list's own selection, which falls back to the first
        # account exactly as the pane below it does: a − that appeared only
        # after a click was a second rule for the same list.
        self.assertIn("void logoutAccount(stringValue(selectedAuthAccount.ref));", account_block)
        self.assertIn('selectedKey={stringValue(selectedAuthAccount?.ref)}', account_block)
        # The editor states who the account is and nothing else: no second
        # sign-out control under the row that already carries it, and no second
        # copy of a status the list above already draws.
        editor_row = account_block.split("<View style={styles.accountEditor}>", 1)[1]
        self.assertNotIn("runAuthAction", editor_row)
        self.assertNotIn("relay.officialProviderLogout", editor_row)
        self.assertIn('translate("relay.accountField")', editor_row)
        self.assertNotIn('translate("providers.authStatus")', editor_row)

    def test_every_sign_out_asks_before_it_ends_a_session(self) -> None:
        """Both sign-out controls ask first, and both name what they will end.

        The account list's - asks; the provider wizard's own 退出登录 did not,
        so the same irreversible act was guarded in one place and bare in the
        other.  A session that has to be signed in again is not something a
        single mis-press should spend.
        """

        signatures = {
            "logoutAccount": "const logoutAccount = async (accountRef: string): Promise<void> => {",
            "logoutOfficial": "const logoutOfficial = async (): Promise<void> => {",
        }
        for function, signature in signatures.items():
            block = self.ui.split(signature, 1)[1].split("\n  };", 1)[0]
            confirm_at = block.find("native.showConfirmation({")
            dispatch_at = block.find("auth_logout")
            self.assertGreater(confirm_at, -1, f"{function} asks before signing out")
            self.assertGreater(dispatch_at, confirm_at, f"{function} signs out only after the question")
            self.assertIn("if (!confirmed) return;", block)
            self.assertIn("destructive: true", block)
        # The two ask different questions because they do different things.
        # `logoutOfficial` sends no account_ref, and Core reads that as "end
        # every account this provider owns" — so the account-row sentence
        # ("the remaining accounts keep the traffic") would be false on it.
        account_block = self.ui.split(signatures["logoutAccount"], 1)[1].split("\n  };", 1)[0]
        provider_block = self.ui.split(signatures["logoutOfficial"], 1)[1].split("\n  };", 1)[0]
        self.assertIn('translate("relay.officialProviderLogoutBody", {', account_block)
        self.assertNotIn('translate("relay.officialProviderLogoutBody"', provider_block)
        self.assertIn('message: translate("relay.officialProviderSignOutAllBody", { name:', provider_block)
        self.assertIn('title: translate("relay.officialProviderSignOutAllTitle")', provider_block)

    def test_an_account_backed_route_is_filed_under_the_account_it_answers_with(
        self,
    ) -> None:
        """The model list groups by credential, and an account is one.

        A route of an account-backed row answers with exactly one credential:
        an account through the adapter, or a custom key at the service address.
        The list used to ask for a *key* first, so every account's route fell
        into the ungrouped bucket beside the group that existed to hold it —
        the pane showed a bare model row above the account heading, and Core's
        account projection was never read at all.
        """

        rows = self.ui.split("const modelRows = useMemo(", 1)[1].split("}, [modelCredentials", 1)[0]
        # The two cases are decided by whether the row has credentials to file
        # under, not by whether one of them happens to be a key.
        self.assertIn("if (modelCredentials.length === 0) {", rows)
        self.assertIn("const key = modelProviderKeyState(item, provider ?? {});", rows)
        self.assertIn("if (!modelCredential(item, provider ?? {}, modelCredentials)) {", rows)
        # A route the row cannot file is still listed, so it can be repaired or
        # deleted instead of silently disappearing from the pane.
        self.assertIn("ungrouped.push(item);", rows)
        # The group a route lands in is the credential it names.
        self.assertIn(
            "const own = models.filter((item) => modelCredential(item, provider ?? {}, modelCredentials)?.id === credential.id);",
            rows,
        )
        # The groups are every credential the row owns, an account included.
        self.assert_ui_has("function providerCredentials(provider: UnknownRecord | undefined, translate: Translate): ProviderCredential[] {")
        self.assertIn('id: `account:${stringValue(account.ref)}`', self.ui)
        self.assertIn("accountRef: stringValue(account.ref),", self.ui)
        self.assert_ui_has("function modelCredential(")
        self.assertIn('const accountRef = stringValue(model.account_ref);', self.ui)

    def test_model_list_groups_are_headings_and_model_rows_open_their_inspector(self) -> None:
        """A credential group is a heading; a model row is the entry it names.

        The model list draws one group per credential, and a group is not an
        entry: it has no model behind it, so a click on one must leave the
        selection where it was.  The row keys separate their three parts by a
        unit separator because a credential id is itself `account:<ref>` or
        `key:<slot>` — a colon-joined key put the credential's prefix in front
        of the model id, and every click on a model asked for a model named
        after its credential: the repair effect dropped the selection and the
        row flashed without opening the inspector.  The round trip itself is
        executed by tests/test_rn_model_row_keys.mjs; this pins the wiring.
        """

        row_keys = (ROOT / "rn/packages/shared/src/ui/modelRowKeys.ts").read_text(encoding="utf-8")
        self.assertIn('export const MODEL_ROW_KEY_SEPARATOR = "\\x1f";', row_keys)
        self.assertIn("export function modelGroupRowKey(credentialID: string): string {", row_keys)
        self.assertIn("export function modelRowKey(credentialID: string, modelID: string): string {", row_keys)
        self.assertIn("export function modelIDFromRowKey(key: string): string | undefined {", row_keys)
        self.assert_ui_has('import { modelGroupRowKey, modelIDFromRowKey, modelRowKey } from "./modelRowKeys";')
        # A group header is a spanning row, which the native table refuses to
        # select, so clicking 账号 or 密钥 cannot move the selection at all.
        self.assert_ui_has("rows.push({ key: modelGroupRowKey(credential.id), cells: [credential.label], spanning: true });")
        self.assert_ui_has("rows.push({ key: modelRowKey(credential.id, editorIdentifier(item)), cells: modelCells(item) });")
        # One route is one row.  A route that names no credential belongs to
        # the provider's own default, which is not one of these groups: listing
        # it under every group put the same route on screen once per credential,
        # and each copy carried the same 启用 box — so flipping one read as
        # flipping the others.
        self.assert_ui_has('for (const item of ungrouped) rows.push({ key: modelRowKey("", editorIdentifier(item)), cells: modelCells(item) });')
        grouped_rows = self.ui.split("if (modelCredentials.length > 0) {", 1)[1].split("return rows;", 1)[0]
        # A route answers with exactly one credential — an account or a key —
        # and Core projects the account it resolves to, so the group a route
        # lands in is decided by that one answer rather than by whichever
        # groups happen to end with its key slot's id.
        self.assertIn("const own = models.filter((item) => modelCredential(item, provider ?? {}, modelCredentials)?.id === credential.id);", grouped_rows)
        # A heading with nothing under it claims the credential serves routes it
        # does not serve, so an empty group draws no heading at all.
        self.assertIn("if (own.length === 0) continue;", grouped_rows)
        # The clicked row is what the table highlights: a model listed under
        # several credentials keeps the highlight on the copy that was pressed.
        self.assert_ui_has("const [selectedModelRow, setSelectedModelRow] = useState<string>();")
        self.assert_ui_has('if (selectedModelRow && modelRows.some((row) => !row.spanning && row.key === selectedModelRow && modelIDFromRowKey(row.key) === selectedModel)) {')
        # The list says which row a model sits in; the pane never invents a
        # group for it, because a route that names no credential has no group.
        self.assert_ui_has("const listed = modelRows.find((row) => !row.spanning && modelIDFromRowKey(row.key) === selectedModel);")
        model_table = self.ui.split("<NativeTable columns={modelTableColumns(", 1)[1].split("/></View>", 1)[0]
        self.assertIn("const modelID = modelIDFromRowKey(key);", model_table)
        self.assertIn("setSelectedModel(modelID); setSelectedModelRow(key);", model_table)

    def test_provider_empty_state_keeps_the_native_table_frames(self) -> None:
        provider_list = self.ui.split("const ProviderListTable = React.memo(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("rows={rows}", provider_list)
        self.assertNotIn("EmptyState", provider_list)
        for marker in (
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
            # The active tab belongs to the route surface, because it decides
            # whose result the window's one status strip reports.
            'onTabChange(next);',
            'void pending.catch((reason: unknown) => {',
            'onTabSwitchError(previous, reason);',
            # The detail column is the one shared rail detail: its header band
            # names the active tab and carries that pane's commands, so the pane
            # body opens on its content instead of repeating the tab name as a
            # heading and no pane draws an action bar of its own.
            '<View style={[styles.settingsRailDetail, isWindowsShell && styles.settingsRailDetailWindows]}>',
            '<SettingsDetailHeader title={dataManagementTabRows.find((row) => row.key === tab)?.cells[0] ?? ""} />',
            '{tab === "import" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>',
            '{tab === "export" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>',
            '{tab === "webdav" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>',
        ):
            self.assert_ui_has(marker)
        self.assert_ui_has('const [dataManagementTab, setDataManagementTab] = useState<DataManagementTab>("import");')
        self.assert_ui_has('tab={dataManagementTab} onTabChange={setDataManagementTab}')
        self.assertNotIn('const [tab, setTab] = useState<DataManagementTab>(', self.ui)
        # Every tab of this pane scrolls through the one shared scroll surface.
        self.assertEqual(3, workspace.count('<PersistentScrollView style={styles.dataManagementPane}'))
        self.assertNotIn("dataManagementPolishStyles.paneHeading", self.ui)
        self.assertNotIn("paneIntro", self.ui)
        self.assertEqual(3, workspace.count('{tab === "'))

    def test_a_pane_result_stands_beside_the_command_that_produced_it(self) -> None:
        # A backup & sync result belongs to the pane that produced it, on the
        # line of the command that produced it: the header band states it, and
        # it stays keyed per tab, so a message one tab produced never appears
        # while another tab is showing.  The window's strip keeps the window's
        # own state.
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        for marker in (
            'const runDataManagement = (tab: DataManagementTab, operation: () => Promise<unknown>, message: ResultMessage, keepControlsEnabled = false): Promise<void> => run(operation, message, keepControlsEnabled, true, tab);',
            'onTabSwitchError={(tab, reason) => setDataManagementStatuses((current) => ({ ...current, [tab]: errorMessage(reason, translate) }))}',
            'const [dataManagementStatuses, setDataManagementStatuses] = useState<Partial<Record<DataManagementTab, string>>>({});',
            'const publishResult = (next: string | undefined): void => {',
            'setDataManagementStatuses((current) => ({ ...current, [dataManagementTab]: next }))',
            'statuses: Partial<Record<DataManagementTab, string>>;',
            'statuses={dataManagementStatuses}',
            'const actionRow = (controls: React.ReactNode, result?: string, hint?: string): React.JSX.Element => <View style={SETTINGS_FIELD}>',
            '<Text numberOfLines={2} style={styles.dataManagementActionStatus}>{result ?? ""}</Text>',
            'const routeStatus = result ?? (busy ? translate("common.working") : undefined);',
            '{routeStatus ?? translate("common.ready")}',
        ):
            self.assert_ui_has(marker)
        # The command and its result share one field row: the row names the
        # action, the control column holds the button, and the result stands
        # immediately beside that button on the same line.
        self.assertLess(
            workspace.index('const actionRow = (controls: React.ReactNode, result?: string, hint?: string)'),
            workspace.index('{controls}'),
        )
        self.assertLess(workspace.index('{controls}'), workspace.index('<Text numberOfLines={2} style={styles.dataManagementActionStatus}>{result ?? ""}</Text>'))
        self.assert_ui_has('dataManagementActionStatus: { flexShrink: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14 }')
        # The sync category also states when the last run landed, so a manual
        # sync that fails says why the stamp stopped: the category names the
        # cause Core reported (an incompatible remote bundle, a conflict, or a
        # plain failure) instead of repeating one generic sentence.
        self.assert_ui_has('const lastSyncAt = stringValue(asRecord(asRecord(state.last_sync)).at);')
        self.assert_ui_has('translate("dataManagement.lastSync", { time: formatActionTimestamp(lastSyncAt) })')
        self.assert_ui_has('function formatActionTimestamp(value: string): string {')
        self.assert_ui_has('{translate("webdav.serverLabel")}{host === "" ? translate("webdav.notSet") : host}')
        self.assert_ui_has('style={[styles.settingsActionRowFact, lastFailed ? styles.settingsActionRowFactFailed : null]}')
        self.assert_ui_has('{lastFailed ? ` ${translate("webdav.failedSuffix")}` : ""}')
        self.assert_ui_has('function webdavHost(url: string): string {')
        self.assert_ui_has('const lastFailed = lastResult.ok === false && ["sync", "push", "pull"].includes(stringValue(lastResult.action));')
        self.assertIn('"dataManagement.lastSyncFailed": "上次同步失败（{time}），将在下个间隔重试"', self.zh)
        self.assert_ui_has("{hint === undefined ? null : <View style={SETTINGS_FIELD_HELP_SLOT}><Text style={SETTINGS_FIELD_HELP_TEXT}>{hint}</Text></View>}")
        for marker in (
            'webdav_sync_incompatible: "error.webdavSyncIncompatible"',
            'webdav_sync_conflict: "error.webdavSyncConflict"',
            'webdav_sync_unsaved: "error.webdavSyncUnsaved"',
            'webdav_sync_failed: "error.webdavSyncFailed"',
        ):
            self.assert_ui_has(marker)
        # The words themselves live in the localization tables.
        for marker in (
            '"error.webdavSyncIncompatible": "远端文件由旧版本创建。下次同步会先另存一份，再覆盖它。"',
            '"error.webdavSyncConflict": "本机与远端都有改动，同步已停止。请比较两端后选择以哪一侧为准。"',
            '"error.webdavSyncUnsaved": "本机有未保存的改动，同步已停止。请先保存或重新加载，再同步。"',
            '"error.webdavSyncFailed": "同步失败，请检查网络与 WebDAV 设置。"',
            '"dataManagement.lastSync": "上次同步：{time}"',
            '"webdav.serverLabel": "服务器："',
            '"webdav.failedSuffix": "失败"',
        ):
            self.assertIn(marker, self.zh)
        self.assertIn('"dataManagement.lastSync": "Last sync: {time}"', self.en)
        # A sync that had to set an unreadable remote file aside says so, using
        # the summary Core returns with the dispatch.
        self.assert_ui_has('if (stringValue(summary.archived_remote) !== "") return "dataManagement.syncedReplacingRemote";')
        self.assert_ui_has('if (stringValue(adopted.name) !== "" && adopted.removed !== true) return "dataManagement.syncedLeftoverRemote";')
        self.assertIn('"dataManagement.syncedLeftoverRemote": "同步完成；远端旧文件无法删除（服务器拒绝），请在 WebDAV 客户端里手动删除"', self.zh)
        self.assertIn('"dataManagement.syncedReplacingRemote": "同步完成；远端旧文件已另存为 .bak 备份"', self.zh)
        # The interval field is the automatic sync's cadence, so it says when
        # the loop runs and what 0 means, and the direction it follows is saved
        # rather than per-window: the interval loop reads the same setting.
        self.assert_ui_has('translate("webdav.syncEveryHint")')
        self.assertIn(
            '"webdav.syncEveryHint": "开启同步后按此间隔自动同步，填 0 表示只手动同步。"',
            self.zh,
        )
        # The pane's settings are what the interval loop reads, so changing one
        # stages the patch and applies it — the switch has to reach the flag
        # file, and a draft the route never applies would leave it meaningless.
        # The summary's switch commits at once; the detail window's 保存并关闭
        # sends the same staged patch through the same one write path.
        self.assert_ui_has("const writeWebDavSettings = (patch: UnknownRecord): Promise<IpcResults[\"apply\"] | undefined> => {")
        self.assert_ui_has('const staged = await enqueueDispatch("patch", patch, "webdav");')
        self.assert_ui_has('applied = await ipc.apply("webdav", revision.current, diskChanged ? ["overwrite_external_webdav"] : undefined);')
        # The switch lives in the form, so its enable commit is the form's
        # 保存并关闭; the category only states whether it is on.
        self.assert_ui_has('{webDavStateLabel(translate, enabled)}')
        self.assert_ui_has('patch.enabled = draft.enabled;')
        self.assertIn("const DATA_MANAGEMENT_DIRTY_DOMAINS: readonly ConfigDomain[] = DATA_PACKAGE_DOMAINS;", self.ui)
        # There is one sync and one direction to resolve a conflict in, and
        # neither is a saved setting: 触发一次同步 sends the comparing run, and
        # a diverged run is settled by reading the two sides and keeping one.
        self.assert_ui_has("const syncWebDav = (): Promise<void> => syncWebDavOperation(\"sync\");")
        self.assert_ui_has('const resolveWebDavConflict = (side: "local" | "remote"): Promise<void> => syncWebDavOperation(side === "local" ? "push" : "pull");')
        self.assert_ui_has('const dispatched = await ipc.dispatch({ domain: "webdav", type, payload: { sections: [...WEBDAV_SYNC_DOMAINS] } }, revision.current);')
        self.assertNotIn("const [syncAction, setSyncAction] = useState<WebDavSyncAction>", self.ui)
        self.assertNotIn("WEBDAV_SYNC_OPTIONS", self.ui)
        self.assertNotIn("sync_direction", self.ui)
        # Every secondary line of a data-management pane is one typography: the
        # result beside the buttons reads exactly like the tips under them.
        self.assertNotIn('dataManagementActionStatus: { flexShrink: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE', self.ui)
        self.assert_ui_has('dataManagementActionStatus: { flexShrink: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14 }')
        self.assertNotIn('statuses.import ? <Text', workspace)
        self.assertNotIn('statuses.export ? <Text', workspace)

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
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: 0, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: SETTINGS_PANE_INSET, gap: 14 }')
        self.assertNotIn('importLandingContent', self.ui)
        self.assert_ui_has('importIntro: { minHeight: 0, paddingHorizontal: 12, paddingVertical: 0, gap: 10 }')
        self.assert_ui_has('paneHint: { color: systemColors.secondaryLabel, fontSize: UI_FONT_SIZE, lineHeight: 16 }')
        # A sync category keeps the one pane inset and reports in captions over
        # values; the form it opens is fields of the same grid the panes draw.
        for marker in (
            'settingsActionRowFacts: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", gap: 12 }',
            'settingsActionRowFactFailed: { color: systemColors.red }',
            'webdavDialogBodyContent: { padding: SETTINGS_PANE_INSET }',
        ):
            self.assert_ui_has(marker)
        # The category draws no caption band of its own: it is a row of the grid,
        # so its name is a row label (regular weight) beside a value, an action,
        # and the facts under that same control column.
        for gone in ("webdavSection:", "webdavSectionHeader:", "webdavSectionTitle:", "webdavSectionState:", "webdavSectionSpacer:", "webdavSectionSummary:", "webdavSectionFact:", "webdavSectionStatus:"):
            self.assertNotIn(gone, self.ui)
        self.assertIn('webdavFieldList: { ...SETTINGS_FIELD_LIST }', self.ui)
        self.assertNotIn('compactText:', self.ui)
        self.assertNotIn('webDavFormRows:', self.ui)
        self.assert_ui_has('dataManagementSectionList: { gap: 4 }')
        self.assert_ui_has('dataManagementSectionItem: { minHeight: 22 }')
        self.assert_ui_has('const SETTINGS_FIELD_ROW_INDENTED: ViewStyle = { ...SETTINGS_FIELD_ROW, paddingLeft: SETTINGS_FIELD_LEAD };')
        # The runtime row carries the lead as padding like the others; its
        # marker bar is positioned in that lead instead of pushing the label.
        self.assert_ui_has('runtimeModifiedBar: { position: "absolute", left: 0, top: 3, bottom: 3, width: 2, borderRadius: 1, backgroundColor: "transparent" }')
        # One width scale for every control in the grid, chosen by what the
        # control holds: a switch takes its own slot, a number keeps only its
        # digits beside its unit, a short value takes the value column, and a
        # value that is text by nature takes the wide column.
        for marker in (
            "const SETTINGS_FIELD_VALUE_WIDTH = 160;",
            "const SETTINGS_FIELD_NUMBER_WIDTH = 84;",
            "const SETTINGS_FIELD_TEXT_WIDTH = 420;",
            "const SETTINGS_FIELD_CELL_WIDE: ViewStyle = { flex: 1, minWidth: 0, maxWidth: SETTINGS_FIELD_TEXT_WIDTH",
            "runtimeNumberControl: { width: SETTINGS_FIELD_NUMBER_WIDTH",
            "runtimeTextControl: { flexGrow: 1, flexShrink: 1, flexBasis: 0, minWidth: 0 }",
            'width="number" value={portValue}',
        ):
            self.assert_ui_has(marker)
        # No pane keeps a control column of its own beside the value column.
        self.assertNotIn("SETTINGS_FIELD_ROUTE", self.ui)
        # A settings pane uses the grid; it does not copy it.  Every pane-local
        # alias that only re-spread a shared constant is gone -- the extra layer
        # restated the same geometry under a second name, and the two panes had
        # already drifted apart on the one number nobody aliased (the pane inset).
        general = self.ui.split("function GeneralWorkspace(", 1)[1].split("function RuntimeWorkspace(", 1)[0]
        # Each name is checked as a style definition (`name: {`), so the i18n
        # helper `runtimeFieldLabel(...)` beside it is not mistaken for one.
        for alias in (
            "generalField", "generalFieldList", "generalRow", "generalRowLabel", "generalRowValue",
            "generalValueSlot", "generalPicker", "generalValueCellWide", "generalToggle", "generalHelpSlot",
            "generalHelpText", "generalSection", "generalSectionTitle", "generalContent",
            "runtimeField", "runtimeInputRow", "runtimeFieldLabel", "runtimeValueSlot", "runtimeValueCellWide",
            "runtimeBooleanControl", "runtimeUnit", "runtimeHelpSlot", "runtimeHelpText", "runtimeWorkspace",
        ):
            self.assertNotIn(f"{alias}: {{", self.ui, f"{alias} is a second name for a shared grid constant")
        # ... and the pane states the grid directly, section caption included.
        for marker in (
            "<View style={SETTINGS_SECTION}>",
            "<Text style={SETTINGS_SECTION_TITLE}>",
            "<View style={SETTINGS_FIELD_LIST}>",
            "<View style={SETTINGS_FIELD}>",
            "<View style={SETTINGS_FIELD_ROW_INDENTED}>",
            "<Text style={SETTINGS_FIELD_LABEL}>",
            "<View style={SETTINGS_FIELD_HELP_SLOT}>",
            "<Text style={SETTINGS_FIELD_HELP_TEXT}>",
            "contentContainerStyle={SETTINGS_SECTION_LIST}",
            # A tip belongs to the field above it, so the two are one block and
            # the blocks are a list -- the same 2-and-10 rhythm every other
            # settings pane has.
            "<View style={SETTINGS_FIELD_ROW_INDENTED}>",
        ):
            self.assertIn(marker, general)
        # One name for "this pane owns its inset", shared with the runtime and
        # backup panes instead of three identical style objects.
        self.assertIn("const SETTINGS_PANE_ROUTE: ViewStyle = { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 };", self.ui)
        self.assertIn('(settingsRoute || route === "runtime-settings" || route === "data-management" || route === "general-settings") && SETTINGS_PANE_ROUTE', self.ui)
        self.assertIn("const SETTINGS_PANE_CONTENT: ViewStyle = { width: \"100%\", padding: SETTINGS_PANE_INSET, gap: 14 };", self.ui)
        # A caption sits the same distance above the fields it names as the
        # runtime pane's detail header leaves to its first row, and further from
        # the block above it: a caption belongs to the fields below, not above.
        self.assertIn("const SETTINGS_SECTION: ViewStyle = { minWidth: 0, alignSelf: \"stretch\", gap: SETTINGS_HEADER_GAP };", self.ui)
        self.assertIn("const SETTINGS_SECTION_GAP = SETTINGS_PANE_INSET;", self.ui)
        self.assertIn("const SETTINGS_SECTION_LIST: ViewStyle = { ...SETTINGS_PANE_CONTENT, gap: SETTINGS_SECTION_GAP };", self.ui)
        # The body and the pane's own title start from the same x.  A body that
        # began left of its title read as part of the sidebar beside it, which is
        # exactly how the General pane looked: its caption at 14 while 常规 sat at 20.
        self.assertIn("const SETTINGS_PANE_INSET = 24;", self.ui)
        self.assertIn("paddingHorizontal: SETTINGS_PANE_INSET, paddingTop: 10, paddingBottom: 8", self.ui)
        self.assertNotIn("settingsPaneHeader: { flexShrink: 0, paddingHorizontal: 20", self.ui)
        self.assertNotIn("const SETTINGS_PANE_INSET = 14;", self.ui)
        self.assertNotIn("const SETTINGS_PANE_INSET = 20;", self.ui)
        # A section caption starts at the pane inset -- the same x as a detail
        # header's title -- and a labelled row starts one lead further in.  The
        # header therefore states the inset instead of restating its number.
        self.assertIn("const SETTINGS_SECTION_TITLE: TextStyle = { color: systemColors.label, fontSize: UI_FONT_SIZE, fontWeight: \"600\" };", self.ui)
        self.assertIn("paddingHorizontal: SETTINGS_PANE_INSET, paddingTop: SETTINGS_HEADER_LEAD, paddingBottom: SETTINGS_HEADER_GAP, backgroundColor: systemColors.window", self.ui)
        self.assertNotIn("paddingHorizontal: 14, paddingVertical: 8", self.ui)
        # One heading-to-form distance, stated once and read by both panes: a
        # section caption's gap and the body under a detail header (which adds
        # none of its own).  Adding a band's air to a full top inset is what made
        # a category title sit three times as far from its first row.
        self.assertIn("const SETTINGS_HEADER_GAP = 8;", self.ui)
        self.assertIn("const SETTINGS_HEADER_LEAD = 16;", self.ui)
        self.assertIn("const SETTINGS_PANE_BODY: ViewStyle = { ...SETTINGS_PANE_CONTENT, paddingTop: 0 };", self.ui)
        self.assertIn("contentContainerStyle={SETTINGS_PANE_BODY}", self.ui)
        self.assertNotIn("settingsRailDetailHeader: { minHeight: 50", self.ui)
        self.assertNotIn("paddingVertical: 8, backgroundColor: systemColors.window", self.ui)
        self.assertNotIn('dataManagementWarningPanel:', self.ui)
        self.assertNotIn('dataManagementImportEmpty:', self.ui)
        self.assertNotIn('setContentSize?.("data-management", Platform.OS === "windows" ? 740 : 720', self.ui)

    def test_export_pane_is_a_section_list_without_an_action_bar(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        export_pane = workspace.split('{tab === "export" ? <PersistentScrollView', 1)[1].split(
            '<WebDavSummary',
            1,
        )[0].rsplit('{tab === "webdav" ?', 1)[0]
        self.assertIn('{tab === "export" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>', workspace)
        # The pane is one field of the shared grid: the row names the list, the
        # field carries the list's own line (what is chosen, and the tool that
        # chooses it all) and then one row per section.  The command that
        # consumes the choice rides the header band, so nothing sits outside
        # the scroll surface and the strip stays the only bar at the bottom.
        for marker in (
            '<Text style={SETTINGS_FIELD_LABEL}>{translate("dataManagement.sections")}</Text>',
            '<View style={styles.dataManagementSectionsField}>',
            '{sectionListHeader(translate("dataManagement.selectedCount", { count: exportSections.length }), selectionTool(exportSections.length, DATA_PACKAGE_DOMAINS.length, () => setExportSections([...DATA_PACKAGE_DOMAINS]), () => setExportSections([])))}',
            '{sectionList(DATA_PACKAGE_DOMAINS, exportSections, busy, (domain, enabled) => toggleSection(setExportSections, domain, enabled))}',
            '<View style={SETTINGS_FIELD_HELP_SLOT}><Text numberOfLines={2} style={SETTINGS_FIELD_HELP_TEXT}>{translate("dataManagement.sensitiveHint")}</Text></View>',
        ):
            self.assertIn(marker, export_pane)
        # The tool that picks the whole list sits on the list, not beside the
        # command that consumes it, and the command itself is not here at all.
        self.assertLess(
            export_pane.index('<View style={styles.dataManagementSectionsField}>'),
            export_pane.index('selectionTool(exportSections.length, DATA_PACKAGE_DOMAINS.length'),
        )
        self.assertIn('{actionRow(<ActionButton primary title={translate("dataManagement.exportSelected")}', export_pane)
        self.assertIn('statuses.export)', export_pane)
        self.assertNotIn('dataManagementBottomActions', self.ui)
        self.assertNotIn('dataManagementBottomMessage', self.ui)
        self.assertNotIn('dataManagementSensitiveNote', self.ui)
        self.assertNotIn('bottomActions:', self.ui)
        self.assertNotIn('dataManagementStatus:', self.ui)
        self.assertNotIn('dataManagementGroup:', self.ui)
        self.assertNotIn('dataManagementSectionPicker:', self.ui)
        for header_tip in (
            'description={translate("dataManagement.importHint")}',
            'description={translate("dataManagement.importRecognizedHint")}',
            'description={translate("dataManagement.exportHint")}',
        ):
            self.assertNotIn(header_tip, workspace)
        self.assert_ui_has('dataManagementSectionList: { gap: 4 }')
        self.assert_ui_has('paneScrollContent: { flexGrow: 1, paddingTop: 0, paddingHorizontal: SETTINGS_PANE_INSET, paddingBottom: SETTINGS_PANE_INSET, gap: 14 }')

    def test_data_management_uses_native_preference_groups_not_web_cards(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        webdav = self.ui.split("function WebDavSyncSection(", 1)[1].split("function WebDavSettingsPanel(", 1)[0]
        webdav_form = self.ui.split("function WebDavSettingsPanel(", 1)[1].split("function WebDavPasswordField(", 1)[0]
        for marker in (
            "dataManagementToolbarButtons:",
            "dataManagementImportFileRow: { width: \"100%\", minHeight: 28, flexDirection: \"row\", alignItems: \"center\", gap: SETTINGS_FIELD_GAP, paddingLeft: SETTINGS_FIELD_LEAD }",
            "dataManagementImportFileLabel: { ...SETTINGS_FIELD_LABEL }",
        ):
            self.assert_ui_has(marker)
        # A sync category reports instead of holding a form: captions over
        # values, the one switch, and the command it exists for. The form it
        # opens draws the same grid every pane does, including the label column.
        for marker in (
            '<SettingsActionRow',
            '<View style={styles.settingsActionRowFacts}>',
            'style={styles.settingsActionRowFact}',
            '{translate("webdav.serverLabel")}{host === "" ? translate("webdav.notSet") : host}',
            '<ActionButton title={translate("webdav.details")}',
        ):
            self.assertIn(marker, webdav)
        # A row label is regular weight, like every other row's: a bold caption
        # over a value read as a section heading in a list of rows.
        self.assertNotIn("fontWeight", webdav)
        self.assertNotIn("<NativeToggle", webdav)
        for marker in (
            '<PersistentScrollView style={styles.webdavDialogBody} contentContainerStyle={styles.webdavDialogBodyContent}',
            '<WebDavField label=',
        ):
            self.assertIn(marker, webdav_form)
        # The field is one helper, so every row of the form reads the same.
        for marker in (
            'function WebDavField({ label, hint, children }',
            'style={styles.webdavFieldLabel}',
            'style={styles.webdavFieldHelpSlot}',
            'style={styles.webdavFieldHelpText}',
        ):
            self.assert_ui_has(marker)
        # The row label is the grid's own `SETTINGS_FIELD_LABEL` (regular weight,
        # label column), and the form's rows spread the same grid through one
        # alias -- the form's rows are a labelled field, the display is a row.
        self.assertIn('webdavFieldLabel: { ...SETTINGS_FIELD_LABEL }', self.ui)
        self.assertNotIn('<DataManagementGroup style={styles.webDavForm}>', self.ui)
        for legacy_form in (
            "webdavFormBody:",
            "webdavFormRows:",
            "webdavSyncArea:",
            "webdavActionRow:",
            "webdavActionStatus:",
            "webdavStateSpacer:",
            "webdavStateStatus:",
            "webdavStateRow:",
            "dataManagementSyncScope:",
            "dataManagementSyncScopeLabel:",
            "dataManagementDirection:",
            "dataManagementDirectionLabel:",
            "dataManagementSyncContent:",
        ):
            self.assertNotIn(legacy_form, self.ui)
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
        self.assertNotIn('dataManagementSelectionList:', self.ui)
        self.assertIn('dataManagementSectionItem: { minHeight: 22 }', self.ui)
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
        # A stopped sync offers the comparison as a field of this same grid:
        # the row names itself, the button opens it, and the diff takes the
        # window instead of a control column.
        self.assertIn('{conflictChanged ? <WebDavField label={translate("webdav.conflict.title")} hint={translate("webdav.conflict.hint")}>', webdav_form)
        # Every width here is the shared grid's scale, not a number this pane
        # chose: a short name takes the value column, the address and the
        # filename are text and take the wide column, and a number of seconds
        # keeps only its digits.
        self.assertIn('webdavConflictEditor: { flex: 1, minWidth: 0, minHeight: 200 }', self.ui)
        self.assertIn('webdavFieldText: { flex: 1, minWidth: 0, maxWidth: SETTINGS_FIELD_TEXT_WIDTH }', self.ui)
        self.assertIn('webdavFieldShort: { width: SETTINGS_FIELD_VALUE_WIDTH, flexGrow: 0, flexShrink: 0, minWidth: 0 }', self.ui)
        self.assertIn('webdavFieldNumber: { width: SETTINGS_FIELD_NUMBER_WIDTH, height: 26, flexGrow: 0, flexShrink: 0 }', self.ui)
        self.assertNotIn('<WindowTabs values={syncOptions}', workspace)
        self.assertNotIn('<ActionButton primary title={translate("dataManagement.chooseImportFile")}', workspace)
        self.assertIn('{actionRow(<ActionButton primary title={translate("dataManagement.importSelected")}', workspace)
        # One primary action per pane, and it is a field of that pane's grid:
        # export keeps its own, while the sync category's own command is an
        # ordinary button and its test lives in the form it opens.
        self.assertIn('<ActionButton primary title={translate("dataManagement.exportSelected")}', workspace)
        self.assertIn('<ActionButton title={translate("webdav.details")}', webdav)
        self.assertNotIn('primary title={translate("webdav.details")}', webdav)
        self.assertIn('title={translate("dataManagement.testConnection")}', webdav_form)
        self.assertNotIn('translate("dataManagement.testConnection")', webdav)

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
            'const selectionTool:',
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
        # No pane draws a bar under its scroll surface: each pane's command is
        # its own last field row, so the window's status strip is the only bar
        # left at the bottom.
        self.assertEqual(1, workspace.count('const actionRow = (controls: React.ReactNode, result?: string, hint?: string)'))
        for command in (
            '{actionRow(<ActionButton primary title={translate("dataManagement.importSelected")}',
            '{actionRow(<ActionButton primary title={translate("dataManagement.exportSelected")}',
        ):
            self.assertIn(command, self.ui)
        # The sync category has no command of its own: its run lives in the form.
        self.assertNotIn('dataManagement.syncNow', self.ui)
        # A command stands in the control column of an unlabelled action row:
        # the label column stays (so the buttons land on the pane's control
        # column) and stays empty, because each button names its own action.
        for marker in (
            '<Text style={SETTINGS_FIELD_LABEL}>{""}</Text>',
            'dataManagementActionStatus: { flexShrink: 1, minWidth: 0, color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE, lineHeight: 14 }',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('translate("dataManagement.actions")', self.ui)

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
        # The header band carries no rule of its own.  The sidebar and the
        # pane used to each draw a hairline under their title, so the window
        # showed two lines that read as artifacts rather than as structure;
        # the sidebar's chrome boundary is its own edge, so no column rule is
        # left over it either.
        self.assertNotIn('settingsSidebarDivider', self.ui)
        self.assertNotIn('settingsPaneDivider', self.ui)
        # The window's chrome is one L: the title bar and the sidebar draw the
        # border's own colour, so the two read as a single surface and no rule
        # cuts across it.  A transparent sidebar showed the desktop through the
        # window and left the header band as white as the content beside it.
        self.assertIn(
            'settingsShellHeader: { flexShrink: 0, flexDirection: "row", minWidth: 0, '
            'backgroundColor: systemColors.separator }, '
            'settingsShellHeaderWindows: { backgroundColor: systemColors.window, '
            'borderBottomWidth: 1, borderBottomColor: systemColors.separator }',
            self.ui,
        )
        self.assertIn(
            'settingsSidebar: { width: 200, minWidth: 0, flexShrink: 0, minHeight: 0, '
            'backgroundColor: systemColors.separator }',
            self.ui,
        )
        # The rail's column rule is a sibling divider, so it stays whole where
        # the list reaches past the list container's own edge.
        self.assertIn(
            'settingsRail: { width: SETTINGS_RAIL_WIDTH, flexShrink: 0, minHeight: 0, '
            'paddingTop: 6, overflow: "hidden" }',
            self.ui,
        )
        self.assertIn('<View style={styles.settingsRailDivider} />', self.ui)
        self.assertIn(
            "settingsRailDivider: { position: \"absolute\", top: 0, bottom: 0, right: 0, "
            "width: 1, backgroundColor: systemColors.separator }",
            self.ui,
        )
        # The rail draws that one divider and the list inside it draws no frame.
        # A box around a column that already owns a right edge put a hairline
        # under the pane title and another above the status strip, and both
        # belonged to no row: the rail is a column of the pane, not a content
        # list floating in it.  Counting the prop lines themselves keeps a
        # comment that names the prop out of the count.
        self.assertEqual(2, len([line for line in self.ui.splitlines() if line.strip() == "framed={false}"]))
        rail_source = self.ui.split("function SettingsRail(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("framed={false}", rail_source)
        self.assertIn('const SETTINGS_HEADER_CONTENT_HEIGHT = 20;', self.ui)
        # Every content list keeps its native frame: a bordered list is how the
        # user sees where a list ends, so dropping the 1 pt box left the
        # provider and model lists, the keys list and the log table unbounded
        # against the window background.  The two columns that are not content
        # lists opt out: the sidebar's own source list sits on the window's
        # chrome, and the rail is a column of its pane bounded by the one right
        # divider it draws itself.  A box around either one drew a top
        # and a bottom rule that belonged to no row plus a right edge doubling
        # an edge that already exists, so the opt-out exists exactly twice.
        self.assertEqual(7, self.ui.count("<NativeTable"))
        self.assertIn('settingsSidebarAppIcon: { width: SETTINGS_HEADER_CONTENT_HEIGHT, height: SETTINGS_HEADER_CONTENT_HEIGHT, borderRadius: 4 }', self.ui)
        self.assertIn('settingsPaneTitle: { color: systemColors.label, fontSize: 15, fontWeight: "600", lineHeight: SETTINGS_HEADER_CONTENT_HEIGHT }', self.ui)
        self.assertIn('settingsSidebarList: { flex: 1, minWidth: 0, minHeight: 0 }', self.ui)
        # A Windows NavigationView source list: no row rules, a rounded quiet
        # selection fill, a short accent pill, and a hover fill.  The old
        # bright-blue bar and the row separators were the macOS source list
        # drawn over a data-table frame.
        self.assertIn('settingsSidebarWindows: { backgroundColor: WINDOWS_CONTROL.fillSolid', self.ui)
        # One content surface on Windows: the panes used to paint their own
        # white card inside a grey body, so the toolbar row above it met the
        # content across a rule-less seam and the card's inset changed from pane
        # to pane.  The body, the rail's detail and that detail's header now draw
        # the same surface, and only the shell rules and list frames remain.
        self.assertIn('settingsDetailBodyWindows: { backgroundColor: WINDOWS_CONTROL.fill }', self.ui)
        self.assertIn('settingsRailDetailWindows: { backgroundColor: WINDOWS_CONTROL.fill }', self.ui)
        self.assertIn('settingsRailDetailHeaderWindows: { backgroundColor: WINDOWS_CONTROL.fill }', self.ui)
        self.assertIn('isWindowsShell && styles.settingsDetailBodyWindows', self.ui)
        # The rail column keeps the shell colour like the sidebar, and the pane
        # behind it is the content surface: RouteSurface used to paint the window
        # colour behind every pane, so the shell's surface showed only as a
        # gutter around a white card.
        self.assertIn('settingsRailWindows: { backgroundColor: WINDOWS_CONTROL.fillSolid }', self.ui)
        self.assertIn('windowSurfaceShellWindows: { backgroundColor: WINDOWS_CONTROL.fill }', self.ui)
        self.assertIn('shell && isWindowsShell && styles.windowSurfaceShellWindows', self.ui)
        self.assertIn('sourceRow: { minWidth: 0, marginHorizontal: 4, borderRadius: 4, borderBottomWidth: 0 }', self.native_controls)
        self.assertIn('sourceRowSelected: { backgroundColor: WINDOWS_CONTROL.fillSelected }', self.native_controls)
        self.assertIn('sourceSelectionIndicator:', self.native_controls)
        # The sidebar is exactly as wide as its rows: the row's first cell fills
        # the pane instead of taking a fixed column width, and the source list is
        # not wrapped in the data table's horizontal scroller.  Before that, a
        # row was 11 pt wider than the 199 pt sidebar and the column scrolled, so
        # every pane switch shifted it horizontally by a different amount.
        self.assertIn('sourceCellViewFlex: { flex: 1, minWidth: 0 }', self.native_controls)
        # A source-list row keeps its inset: this branch used to drop the
        # column padding along with the fixed width, so the sidebar and every
        # settings rail began their text on the row's own leading edge — 4 pt
        # from the pane's border, and on top of a selected row's accent pill.
        self.assertIn('const leadingInset = sourceImage !== undefined || rowSymbol ? TABLE_MIN_HORIZONTAL_PADDING : inset;', self.native_controls)
        # A list with more rows than it shows has to say so.  The fallback table
        # scrolls through the browser's own `overflow: auto`, whose bar is an
        # overlay that only appears mid-scroll: the keys list in the provider
        # inspector (nine keys, four visible) read as a list that ended.  It
        # draws the same 4 pt capsule `PersistentScrollView` draws.
        self.assertIn('tableScrollThumb: { position: "absolute", top: 2, right: 2, width: 4, borderRadius: 2', self.native_controls)
        self.assertIn('scrollMetrics.overflow && scrollMetrics.height > 0', self.native_controls)
        # The measurement bails out when the thumb did not move: every table a
        # pane mounts measures itself twice (mount effect + ResizeObserver), and
        # a fresh object each time re-rendered the whole mounted window for it.
        self.assertIn('current.overflow === overflow && current.height === height && Math.abs(current.offset - offset) < 0.5', self.native_controls)
        self.assertIn('.getScrollableNode?.();', self.native_controls)
        self.assertIn('windowsTableStyles.sourceCellViewFlex, { paddingLeft: leadingInset, paddingRight: TABLE_MIN_HORIZONTAL_PADDING }', self.native_controls)
        self.assertIn('{sourceList ? body : <ScrollView horizontal showsHorizontalScrollIndicator', self.native_controls)
        self.assertIn('onHoverIn={sourceList ? () => setHovered(true) : undefined}', self.native_controls)
        shell = self.ui.split("function SettingsShell(", 1)[1].split("function RouteSurface(", 1)[0]
        self.assertIn('sourceList', shell)
        self.assertIn('framed={false}', shell)
        self.assertIn('selectedKey={pane}', shell)
        self.assertIn('rowSymbols={paneSymbols}', shell)
        self.assertIn('onSelectionChange={selectPane}', shell)
        self.assertIn('const next = SETTINGS_PANES.find(({ id }) => id === key)?.id;', shell)
        # A switch made inside the shared window is not a request to the host
        # on macOS, where that window already draws every pane: `open` there
        # emitted an `open-<route>` action (a Core action-log write per click)
        # and re-fronted the window, ahead of the pane the user asked for.
        self.assertIn('if (Platform.OS !== "macos") native.window.open(next);', shell)
        self.assertIn('native.window.close(Platform.OS === "windows" ? pane : windowRoute);', shell)
        # The row's capsule is painted from the route the press set; the pane it
        # names is drawn from a deferred value, so a press never waits behind the
        # mount of the pane before it and presses faster than a mount coalesce
        # instead of queueing one mount each.
        self.assertIn('const drawnPane = useDeferredValue(pane);', shell)
        self.assertIn('<RouteSurface route={drawnPane} shell', shell)
        # Beta Display's native settings layout: an app identity header, a pane
        # source list, and a flexible spacer. About lives in the native
        # application/tray menu instead of the sidebar.
        self.assertIn('<Text numberOfLines={1} style={styles.settingsSidebarTitle}>{translate("app.title")}</Text>', shell)
        self.assertIn('<View style={styles.settingsSidebarSpacer} />', shell)
        self.assertIn('<View style={[styles.settingsPaneHeader, styles.settingsPaneHeaderIntegrated]}>', shell)
        self.assertIn('<View style={[styles.settingsShellHeader, isWindowsShell && styles.settingsShellHeaderWindows]}>', shell)
        self.assertIn('void native.versionInfo().then((next) => { if (active) setAppInfo(next); })', shell)
        self.assertNotIn("lastSelection", shell)
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

    def test_only_the_page_host_offers_a_sign_out(self) -> None:
        # 退出登录 belongs to the host that signed in with a key, which is the
        # browser page: its session is a 30-day cookie, so the way out is a
        # control rather than closing the tab.  The desktop hosts hold their
        # lease in the memory of a process a person quits, so they leave the
        # capability undefined and the shell draws nothing -- a switch nobody
        # can honour is worse than no switch (AGENTS.md).
        self.assertIn('if (!native.signOutWebSession) return null;', self.ui)
        self.assertIn('symbol="power-off"', self.ui)
        self.assertIn('translate("status.signOut")', self.ui)
        # The pane's staged edit is committed the same way leaving it commits
        # it, before the session ends: signing out is leaving this page.  The
        # commit is offered with a deadline, because a pane waiting on a Core
        # that is applying is exactly when this is pressed and a way out that
        # hangs on the way out is no button at all.
        self.assertIn('await Promise.race([\n          flushActivePane.current?.("navigate") ?? Promise.resolve(true),', self.ui)
        self.assertIn("setTimeout(() => resolve(false), SIGN_OUT_FLUSH_MS)", self.ui)
        self.assertIn("const SIGN_OUT_FLUSH_MS = 1_200;", self.ui)
        self.assertIn('await native.signOutWebSession?.();', self.ui)
        # Both layouts draw it, and both in the title band's trailing corner:
        # the wide one at the end of the pane header, the one-column one at the
        # end of the compact header.  The sidebar's bottom keeps no second copy.
        shell = self.ui.split("function SettingsShell(", 1)[1].split("function RouteSurface(", 1)[0]
        self.assertEqual(2, shell.count("{signOutControl}"))
        pane_header = shell.split('<View style={[styles.settingsPaneHeader, styles.settingsPaneHeaderIntegrated]}>', 1)[1].split("<View style={[styles.settingsSidebar, isWindowsShell && styles.settingsSidebarWindows]}>", 1)[0]
        self.assertIn("{signOutControl}", pane_header)
        sidebar = shell.split("<View style={[styles.settingsSidebar, isWindowsShell && styles.settingsSidebarWindows]}>", 1)[1].split("<View style={[styles.settingsDetail, styles.settingsDetailIntegrated]}>", 1)[0]
        self.assertNotIn("signOutControl", sidebar)
        self.assertIn('<Text numberOfLines={1} style={styles.settingsSidebarTitle}>{translate("app.title")}</Text>', shell)
        # One glyph, not a glyph plus a sentence: the label the band would
        # otherwise repeat is the button's own hover hint and accessible name.
        sign_out = self.ui.split("function SignOutControl(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('title=""', sign_out)
        self.assertIn('symbol="power-off"', sign_out)
        self.assertNotIn("symbolWithTitle", sign_out)
        self.assertIn("toolTip={title}", sign_out)
        self.assertIn("accessibilityLabel={title}", sign_out)
        # A browser draws a hover label from the host node's `title`, which
        # React Native Web does not forward; an icon-only page button would
        # otherwise have nothing to read under the pointer.
        self.assertIn('node?.setAttribute?.("title", hoverLabel);', self.native_controls)
        self.assertIn("props.toolTip ?? (showsTitle ? undefined : props.title)", self.native_controls)
        # The page is the one host that answers it, and Core's own route is what
        # it answers with.
        browser_leaf = (ROOT / "rn/packages/shared/src/platform/browserLeaf.ts").read_text(encoding="utf-8")
        self.assertIn('signOutWebSession: isOwnerPage() ? undefined : () => core.signOut()', browser_leaf)
        # The owner's own page is signed in by Core without a key, so a sign-out
        # there would reload straight back into a new session and read as a
        # button that did nothing.  The address rule is the gate's own.
        self.assertIn('function isOwnerPage(): boolean {', browser_leaf)
        self.assertIn('return name === "127.0.0.1" || name === "::1" || name === "localhost";', browser_leaf)
        self.assertIn('fetch(`${transport.baseURL}/ipc/web/logout`', browser_leaf)
        self.assertIn('signOutWebSession: bridge.signOutWebSession ? () => bridge.signOutWebSession!() : undefined,', (ROOT / "rn/packages/shared/src/platform/nativeBridge.ts").read_text(encoding="utf-8"))
        # The word is in both languages, and the Chinese one is a verb a person
        # recognises instead of a protocol value.
        self.assertIn('"status.signOut": "Sign Out",', (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8"))
        self.assertIn('"status.signOut": "退出登录",', (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8"))

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
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE", "SOURCE_LIST_FONT_SIZE", "10", "15", "17", "28"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", self.ui)},
        )
        self.assertEqual(
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", relay)},
        )
        self.assertEqual(
            # 12 is the tip step, 16 the sidebar's icon glyph: the glyph is
            # drawn from the icon font, so it is the one text run that does not
            # use a UI step.  9 is the character inside a drawn help ring, which
            # is 13 pt across and would otherwise carry 13 pt of ink.
            {"UI_FONT_SIZE", "UI_TIP_FONT_SIZE", "12", "16", "9"},
            {value.strip() for value in re.findall(r"fontSize:\s*([^,}\n]+)", self.native_controls)},
        )
        self.assertIn("const SETTINGS_FIELD_HELP_TEXT: TextStyle = { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)
        self.assertIn("fieldHint: { color: systemColors.secondaryLabel, fontSize: UI_TIP_FONT_SIZE", self.ui)

        self.assertIn("constexpr CGFloat LiteLLMUIFontSize = 13.0;", macos_controls)
        # The sidebar source list reads a half step above body text on both
        # hosts, and the React sidebar header keeps the same size.
        self.assertIn("export const SOURCE_LIST_FONT_SIZE = 13.5;", typography)
        self.assertIn("constexpr CGFloat LiteLLMSourceListFontSize = 13.5;", macos_controls)
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
                r"FontSize\((?!rail_row \? kSourceListFontSize : kUIFontSize\)|kUIFontSize\)|kSourceListFontSize\)|kSourceListGlyphFontSize\)|kTipFontSize\))",
            )
        # The hint step mirrors the shared UI's own tip size, so a native flyout
        # and a React help tip read the same.
        self.assertIn("export const UI_TIP_FONT_SIZE = 12;", typography)
        self.assertIn("constexpr double kTipFontSize = 12.0;", windows_leaf)

        # The native hint the group manager's question mark opens carries the
        # shared `HelpTip` popup's own metrics, so the one style every other
        # question mark in the app uses is the style this mark uses too.
        for constant in (
            "private let nativeTipFontSize: CGFloat = 12",
            "private let nativeTipLineHeight: CGFloat = 15",
            "private let nativeTipWidth: CGFloat = 208",
            "private let nativeTipInsetX: CGFloat = 8",
            "private let nativeTipInsetY: CGFloat = 6",
            "private let nativeTipCornerRadius: CGFloat = 6",
            "private let nativeTipAnchorGap: CGFloat = 22",
        ):
            self.assertIn(constant, self.macos_leaf)
        # Each constant is actually used to draw the popup rather than kept
        # beside a second literal.
        tip_body = self.macos_leaf.split("func presentTip(_ text: String, beside anchor: NSView?) -> NSPanel? {", 1)[1].split("func dismissTip() {", 1)[0]
        for used in (
            "nativeTipWidth",
            "nativeTipInsetX",
            "nativeTipInsetY",
            "nativeTipCornerRadius",
            "nativeTipAnchorGap",
            "nativeTipFontSize",
            "nativeTipLineHeight",
        ):
            self.assertIn(used, tip_body)
        self.assertRegex(tip_body, r"cornerRadius = nativeTipCornerRadius")
        self.assertRegex(tip_body, r"borderColor = NSColor\.separatorColor\.cgColor")
        self.assertRegex(tip_body, r"backgroundColor = NSColor\.controlBackgroundColor\.cgColor")

    def test_logs_default_to_requests_and_show_latest_first(self) -> None:
        self.assertNotIn("logInfoBar", self.ui)
        self.assertIn('onStatus={setResult} onStatusBar={setStatusBarControls} requestedTab=', self.ui)
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        self.assertIn('setLogTabRequest(tab && LOG_TABS.includes(tab) ? tab : "requests");', bootstrap)
        self.assert_ui_has('useState<typeof LOG_TABS[number]>(() => requestedTab ?? "requests")')
        self.assert_ui_has('return rightTime - leftTime || right.index - left.index;')
        self.assert_ui_has('compact preserveColumnWidths scrollTrailingColumnOverflow onSelectionChange=')
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
        self.assert_ui_has('IconButton label={Platform.OS === "web" ? translate("common.clear") : ""} symbol="trash" title={translate("common.clearView")} disabled={busy} onPress={clearLogs}')
        self.assertNotIn('dispatch("logs.refresh", { tab: selected }, "logs")', self.ui)
        self.assert_ui_has('translate("logs.apiKeyName")')
        self.assert_ui_has('value.api_key_name')
        # The stamp states a local clock without its year: the four digits it
        # saves are what pays for the row's two protocol columns, and the
        # newest-first sort reads the stamp back through the window's own year
        # so the order does not depend on a year that is no longer drawn.
        self.assert_ui_has('function logClockText(parsed: Date): string {')
        self.assert_ui_has('return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;')
        self.assert_ui_has('time = logClockText(epochDate);')
        self.assert_ui_has('const clock = value.trim().match(/^(\\d{2})-(\\d{2}) (\\d{2}):(\\d{2}):(\\d{2})$/);')

    def test_a_local_refusal_is_not_rendered_as_an_upstream_answer(self) -> None:
        """The Log tab has to say who refused the turn.

        Live 2026-11-04: a full concurrency pool and a dead relay answered the
        same public model in the same minute, and both rows read ``HTTP 503 |
        ServiceUnavailable``: the local cap refusal reaches the client as a
        sanitized 503 so the client resends, and the detail column copied it.
        The record now carries ``local`` plus its own reason, and the detail is
        rendered from that, never from a status this proxy invented for the
        wire.
        """

        self.assert_ui_has('const local = error.local === true;')
        self.assert_ui_has('translate("logs.localRefusal")')
        self.assert_ui_has('"model-concurrency-limit": "logs.routeTrace.reasonLocalConcurrency",')
        self.assert_ui_has('local_capacity: "logs.recoveryReason.localCapacity",')
        for source in (self.zh, self.en):
            for key in (
                "logs.localRefusal",
                "logs.routeTrace.reasonLocalConcurrency",
                "logs.recoveryReason.localCapacity",
                "logs.recoveryReason.localGatewayAuth",
            ):
                self.assertIn(f'"{key}":', source, key)

    def test_the_queue_and_its_wait_read_as_router_events(self) -> None:
        """A wait for a slot is this router's own event, worded as one.

        The queue happens inside the recovery round, so its rows sit among
        route-trace events and must not read as upstream traffic.  Each one
        carries how long the turn waited and whether a slot came of it, and the
        detail parts are the ones the Core already writes: ``waited`` and
        ``served``, never a raw boolean.
        """

        for event, label in (
            ("model_concurrency_queue_start", "logs.routeEvent.concurrencyQueueStart"),
            ("model_concurrency_queue_end", "logs.routeEvent.concurrencyQueueEnd"),
        ):
            self.assert_ui_has(f'{event}: "{label}",')
        self.assert_ui_has('const waited = value.match(/^waited=(.+)s$/);')
        self.assert_ui_has('if (value === "served=true") return translate("logs.routeTrace.queueServed");')
        self.assert_ui_has('if (value === "served=false") return translate("logs.routeTrace.queueWaiting");')
        for source in (self.zh, self.en):
            for key in (
                "logs.routeEvent.concurrencyQueueStart",
                "logs.routeEvent.concurrencyQueueEnd",
                "logs.routeTrace.concurrencyWaited",
                "logs.routeTrace.queueServed",
                "logs.routeTrace.queueWaiting",
            ):
                self.assertIn(f'"{key}":', source, key)

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
            '"dataManagement.tab.webdav": "Sync"',
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
            '"dataManagement.tab.webdav": "同步"',
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
            removal.index("await native.clearRelayCredentials(account.id);"),
        )
        self.assertIn("await native.clearRelayCredentials(account.id);", removal)
        self.assertIn('kind: "credentials"', removal)
        # Pending logins reserve no slot; what the sign-in may keep is decided
        # by the post-login prompt inside the native browser flow.
        self.assertIn("pendingAccount: true,", pending)
        self.assertIn("post-login prompt", relay)


    def test_native_tables_support_platform_list_chrome_and_grouping(self) -> None:
        mac_table_spec = (
            ROOT / "rn/packages/shared/src/ui/macos/NativeTableNativeComponent.ts"
        ).read_text(encoding="utf-8")
        mac_native = (
            ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
        ).read_text(encoding="utf-8")
        for marker in (
            "alternatingRows?: WithDefault<boolean, false>;",
            "borderless?: WithDefault<boolean, false>;",
            "sourceList?: WithDefault<boolean, false>;",
            "rowImageNames?: ReadonlyArray<string>;",
            "secondaryCellKeys?: ReadonlyArray<string>;",
            "secondaryRowKeys?: ReadonlyArray<string>;",
            "spanningRowKeys?: ReadonlyArray<string>;",
            "selectableSpanningRowKeys?: ReadonlyArray<string>;",
        ):
            self.assertIn(marker, mac_table_spec)
        self.assertIn("striped = true, compact = true", self.native_controls)
        self.assertIn("const stripedRows = striped && !sourceList;", self.native_controls)
        self.assertIn("alternatingRows: stripedRows,", self.native_controls)
        self.assertIn("const spanningRowKeys = React.useMemo(() => rows.filter((row) => row.spanning).map((row) => row.key), [rows]);", self.native_controls)
        self.assertIn("selectableSpanningRowKeys: Array.from(selectableSpanningKeys),", self.native_controls)
        # The React table forwards the same list chrome and grouping props.
        self.assertIn("secondaryCellKeys", self.native_controls)
        self.assertIn("alertRowKeys", self.native_controls)
        self.assertIn("function WindowsTable({", self.native_controls)
        self.assertEqual(self.ui.count("<NativeTable"), 7)
        self.assertIn("_tableView.usesAlternatingRowBackgroundColors = NO;", mac_native)
        self.assertIn("_tableView.style = sourceList ? NSTableViewStyleSourceList : NSTableViewStylePlain;", mac_native)
        self.assertIn("_tableView.headerView = nil;", mac_native)
        self.assertIn("constexpr CGFloat LiteLLMTableMinimumHorizontalPadding = 8.0;", mac_native)


    def test_subordinate_rails_share_one_source_list_style(self) -> None:
        mac_native = MACOS_CONTROLS.read_text(encoding="utf-8")
        self.assertIn("const SETTINGS_RAIL_WIDTH = 200;", self.ui)
        self.assertIn("const SETTINGS_RAIL_COLUMN_WIDTH = 192;", self.ui)
        # The column list is a module constant: an inline literal was a new
        # array on every settings render, which re-mounted each sidebar row.
        self.assertIn("const SETTINGS_RAIL_COLUMNS = [{ label: \"\", width: SETTINGS_RAIL_COLUMN_WIDTH }];", self.ui)
        self.assertIn('settingsRail: { width: SETTINGS_RAIL_WIDTH, flexShrink: 0, minHeight: 0, paddingTop: 6, overflow: "hidden" }', self.ui)
        self.assertIn("settingsRailList: { flex: 1, minHeight: 0 },", self.ui)
        rail = self.ui.split("function SettingsRail(", 1)[1].split("\nfunction ", 1)[0]
        for marker in (
            "columns={SETTINGS_RAIL_COLUMNS}",
            "sourceList",
            "cellHorizontalPadding={8}",
            "firstColumnHorizontalPadding={8}",
            "onSelectionChange={onSelectionChange}",
        ):
            self.assertIn(marker, rail)
        self.assertIn("compact", rail)
        shell = self.ui.split("function SettingsShell(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("compact={false}", shell)
        self.assertIn("sourceList", shell)
        # Runtime, external clients, and backup: every surface that splits into
        # a list and a detail renders the same pair.
        self.assertEqual(self.ui.count("<SettingsRail"), 3)
        rail_row = mac_native.split("const bool railRow = viewProps.sourceList;", 1)[1].split("label.toolTip = value;", 1)[0]
        # A rail row is the rail's own cell, and a row with a second line is
        # that same cell with the tip step under its name.
        self.assertIn('@"LiteLLMAppKitSourceListTextCell"', rail_row)
        self.assertIn('@"LiteLLMAppKitSourceListDetailCell"', rail_row)
        self.assertIn("[LiteLLMSourceListTextCellView class]", rail_row)
        self.assertIn("[LiteLLMSourceListDetailCellView class]", rail_row)
        self.assertIn("label.font = SourceListFont();", rail_row)
        self.assertIn("label.textColor = SourceListTitleColor(self.effectiveAppearance);", rail_row)
        # Windows draws the same rail with the shared React table.
        self.assertIn("rowSymbolColors", self.native_controls)
        self.assertIn("sourceCell", self.native_controls)

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
        self.assertIn('secondaryLabel: semanticColor("secondaryLabelColor", "TextFillColorSecondary", webInk.textSecondary)', self.ui)
        self.assertIn("routeTraceTimelineNodeStart: { backgroundColor: systemColors.secondaryLabel }", self.ui)
        # A picked route is a plan the request has not confirmed yet, so it
        # never wears the success ink; only a route that served the answer is
        # green.
        self.assertIn("routeTraceTimelineNodeSelected: { backgroundColor: systemColors.yellow }", self.ui)
        self.assertIn("routeTraceTimelineNodeSucceeded: { backgroundColor: systemColors.green }", self.ui)
        self.assertIn("routeTraceTimelineNodeFailed: { backgroundColor: systemColors.red }", self.ui)
        self.assertIn('routeTraceTimelineNode: { width: 18, height: 18, borderRadius: 9', self.ui)
        self.assertIn("routeTraceTimelineNodeAttempted: { borderWidth: 1, borderColor: systemColors.secondaryLabel", self.ui)
        self.assertIn('routeTraceTimelineNodeText: { color: systemColors.label, fontSize: 10, fontWeight: "400"', self.ui)
        self.assertIn("routeTraceStepCardSelected: { borderColor: systemColors.yellow, borderWidth: 2 }", self.ui)
        self.assertIn("routeTraceStepCardSucceeded: { borderColor: systemColors.green, borderWidth: 2 }", self.ui)
        self.assertIn("routeTraceStepStateSelected: { color: systemColors.yellow }", self.ui)
        self.assertIn("routeTraceStepStateSucceeded: { color: systemColors.green }", self.ui)


    def test_native_log_tables_support_compact_rows_on_both_hosts(self) -> None:
        mac_table_spec = (
            ROOT / "rn/packages/shared/src/ui/macos/NativeTableNativeComponent.ts"
        ).read_text(encoding="utf-8")
        mac_native = (
            ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
        ).read_text(encoding="utf-8")
        self.assertIn("compact?: WithDefault<boolean, false>;", mac_table_spec)
        # Two densities, and one step taller while a rail row carries a second
        # line: the row a list draws is the list's own decision, never a
        # per-row height that would desynchronise `scrollToRow`.
        self.assertIn(
            "_tableView.rowHeight = newViewProps.sourceList "
            "? (twoLine ? 40 : (newViewProps.compact ? 26 : 30)) "
            ": (newViewProps.compact ? 22 : 28);",
            re.sub(r"\s+", " ", mac_native),
        )
        self.assertIn(
            "const bool twoLine = newViewProps.sourceList && !newViewProps.rowDetails.empty();",
            re.sub(r"\s+", " ", mac_native),
        )
        # Windows is a React table with the same two densities.
        self.assertIn("rowCompact: { minHeight: 30 }", self.native_controls)
        self.assertIn("rowRelaxed: { minHeight: 36 }", self.native_controls)


    def test_native_tables_keep_short_log_columns_readable_and_scroll_overflow(self) -> None:
        mac_native = (
            ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm"
        ).read_text(encoding="utf-8")
        self.assertIn("column.minWidth = 1;", mac_native)
        self.assertIn("column.maxWidth = CGFLOAT_MAX;", mac_native)
        self.assertIn("_scrollView.hasHorizontalScroller = needsHorizontalScroller;", mac_native)
        self.assertIn("NSTableViewNoColumnAutoresizing", mac_native)
        self.assertIn("std::vector<CGFloat> _requestedColumnWidths;", mac_native)
        self.assertIn("NSTableViewColumnDidResizeNotification", mac_native)
        self.assertIn("- (void)updateColumnMinimumWidths", mac_native)
        self.assertIn("MIN(_requestedColumnWidths[index], minimumWidths[index])", mac_native)
        self.assertIn("_measuredColumnWidths.back() + trailingContentInset", mac_native)
        self.assertIn("LiteLLMTableMeasuredRowLimit", mac_native)
        self.assertIn("MAX(NSWidth(_scrollView.bounds), laidOutContentWidth)", mac_native)
        # The React table keeps short columns at their requested width and
        # scrolls its trailing overflow horizontally.  A cell's inset comes from
        # the table's own `cellHorizontalPadding` rather than from a fixed 8 in
        # the style, so a caller that asks for 6 or 0 gets it — the two numbers
        # used to stack (container 8 + cell 8) and put every header 8 pt right
        # of the text under it.
        self.assertIn("cellHorizontalPadding={cellHorizontalPadding} firstColumnHorizontalPadding={firstColumnHorizontalPadding}", self.native_controls)
        # The clamp and the leading mark's indent are one expression per cell:
        # the three hosts must put a column's text at the same distance from the
        # frame (AppKit has always clamped to 8), and a cell that starts with a
        # `\t` mark is a child of the row above it, so it reads further in than
        # the group heading — not at the frame's own edge.
        self.assertIn("const inset = tableColumnInset(columnIndex === 0 ? firstColumnPadding : cellPadding) + (marked ? TABLE_CELL_MARK_INDENT : 0);", self.native_controls)
        self.assertIn("return Math.max(TABLE_MIN_HORIZONTAL_PADDING, requested);", self.native_controls)
        self.assertIn("paddingHorizontal: inset", self.native_controls)
        self.assertIn("windowsTableStyles.horizontalContent", self.native_controls)
        self.assertIn("<ScrollView horizontal showsHorizontalScrollIndicator style={windowsTableStyles.horizontalScroll}", self.native_controls)
        # A source list lays its rows out at the pane's own width: wrapping the
        # sidebar in this scroller let a row that was a few points too wide
        # scroll, so the column slid sideways by a per-selection amount.
        self.assertIn("{sourceList ? body : <ScrollView horizontal showsHorizontalScrollIndicator", self.native_controls)

    def test_table_rows_state_their_selection_in_the_page_dom(self) -> None:
        """A row's selection and enablement reach the browser's own tree."""
        # React Native for Web drops `accessibilityState` entirely: it reads
        # `aria-selected` / `aria-disabled` off the props, so a row that only
        # stated its state the native way was announced as an unselected,
        # enabled button — the sidebar's current pane, the selected provider,
        # and a switched-off entry all read alike, and the row's `selected`
        # argument reached the DOM nowhere.
        self.assertIn("aria-selected={spanning ? undefined : selected}", self.native_controls)
        self.assertIn(
            "aria-disabled={spanning ? (disabled || !selectableSpanning) || undefined : disabled || undefined}",
            self.native_controls,
        )
        self.assertIn("sourceCellViewFlex: { flex: 1, minWidth: 0 }", self.native_controls)

    def test_a_browser_field_draws_its_hint_in_secondary_ink(self) -> None:
        """A placeholder is a hint, not a value, on the page too.

        React Native for Web draws `::placeholder` as
        `color: var(--placeholderTextColor)` and fills that variable from the
        field's own `placeholderTextColor`.  Neither browser leaf passed one, so
        the declaration was invalid at computed-value time and the hint
        inherited the field's `#1b1b1b` — an empty 配置供应商 read as one that
        already had `https://api.example.com/v1` typed into it.  The two native
        hosts ask AppKit/WinUI for the placeholder ink themselves; the page has
        to state it on the leaf.
        """

        self.assertIn("const webPlaceholderInk = (): string => WINDOWS_CONTROL.textSecondary;", self.native_controls)
        field = self.native_controls.split("export function NativeTextField(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("placeholderTextColor={props.placeholderTextColor ?? webPlaceholderInk()}", field)
        # The browser's password field is the page's second `<input>`: the WebDAV
        # and provider-key hints arrive through it, so it states the same ink.
        secret = self.native_controls.split("function WebSecureTextInput(", 1)[1]
        self.assertIn("placeholderTextColor={webPlaceholderInk()}", secret)

    def test_windows_codegen_declares_every_prop_its_spec_does(self) -> None:
        import re

        specs = ROOT / "rn/packages/shared/src/ui/windows"
        codegen = ROOT / "rn/apps/windows/windows/YoungRouter/codegen/react/components/YoungRouter"
        component_name = re.compile(r'codegenNativeComponent<[^>]*>\(\s*"([^"]+)"\s*,?\s*\)', re.S)
        checked = 0
        for spec in sorted(specs.glob("*NativeComponent.ts")):
            text = spec.read_text(encoding="utf-8")
            name = component_name.search(text)
            self.assertIsNotNone(name, f"{spec.name} declares no native component")
            header = codegen / f"{name.group(1)}.g.h"
            self.assertTrue(header.is_file(), f"{header.name} is missing from the codegen output")
            body = re.search(r"interface \w+Props extends ViewProps \{(.*?)\n\}", text, re.S)
            self.assertIsNotNone(body, f"{spec.name} declares no props interface")
            props = re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\??:", body.group(1), re.M)
            header_text = header.read_text(encoding="utf-8")
            for prop in props:
                self.assertIn(prop, header_text, f"{header.name} does not carry {prop}")
            checked += 1
        self.assertEqual(2, checked)

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
        # The reservation is the box and gap this host draws, then the label --
        # never a padded constant.  A flat 26 pt kept 4-7 pt of ink the control
        # never drew, and that padding only showed the moment something sat
        # beside a checkbox: the 并发上限 row's help mark read 10 pt farther from
        # 跟随全局 than the same mark reads from every other control's edge.
        self.assertIn("function nativeCheckboxLeadingWidth(): number {", self.native_controls)
        self.assertIn('return Platform.OS === "macos" ? 19 : 22;', self.native_controls)
        self.assertIn("return Math.ceil(nativeCheckboxLeadingWidth() + Math.max(24, textWidth));", self.native_controls)
        self.assertNotIn("Math.ceil(26 + Math.max(24, textWidth))", self.native_controls)
        self.assertIn('<NativeToggle value={booleanValue(item.value)} disabled={busy} accessibilityLabel={label}', self.ui)
        self.assertIn('<TooltipText numberOfLines={1} tooltip={label} style={SETTINGS_FIELD_LABEL}', self.ui)
        self.assertIn('styles.runtimeMultilineEditor', self.ui)
        # The compact AppKit switch is 44 x 20 pt, so the slot that centres it
        # has to reserve that width or the control overflows the row.
        self.assertIn('const SETTINGS_FIELD_SWITCH_SLOT: ViewStyle = { width: 44, minWidth: 44, height: 24, alignSelf: "flex-start" };', self.ui)
        self.assertNotIn("runtimeBooleanSlot", self.ui)
        self.assertNotIn("runtimeBooleanHelpSlot", self.ui)

    def test_a_number_and_the_switch_that_sources_it_are_one_grid_row(self) -> None:
        """顺序 and 并发上限 state a number, then where the number comes from.

        Both are one row of the pane's grid: the field *is* the row and its
        switch rides as the field's accessory.  Wrapping a full-width field in a
        row beside the switch made the field a flex item whose width was its
        content, and the fallback hosts resolved that to 2 pt -- a bezel with no
        number in it, with the switch drawn over it -- while the page resolved
        the same row by pushing 跟随倍率 out of the pane's right edge.  The grid
        states the row in one place, so one host cannot decide the field's width
        on its own.
        """

        self.assert_ui_has('accessory={canFollowMultiplier ? <NativeCheckbox')
        self.assert_ui_has('const accessory = <>')
        self.assert_ui_has('    accessory={accessory}')
        self.assert_ui_not_has('routeEditorRow')
        self.assert_ui_not_has('routeEditorField')
        # The switch and its mark are the row's own items, so the row's gap
        # separates them -- the same gap the protocol picker's mark keeps below.
        # A cluster of their own restated that gap (6 pt against the row's 2) and
        # the two marks stood off their control by different distances.
        self.assert_ui_not_has('routeFollowCluster')
        # The switch follows a number field rather than a label, so it keeps the
        # row's label-to-control gap *plus* the separation of a second control.
        self.assert_ui_has('routeFollowControl: { flexShrink: 0, marginLeft: 6 }')
        # An AppKit checkbox keeps 4.5 pt of its frame past its title, so the
        # same row gap drew its ring 3 pt farther from 跟随全局 than the protocol
        # picker's ring sits from its own control.  The correction is macOS-only:
        # the page's checkbox has no such chrome.
        self.assert_ui_has('routeFollowHelp: { flexShrink: 0, marginLeft: Platform.OS === "macos" ? -3 : 0 }')

    def test_a_fixed_width_field_states_its_width_without_flex(self) -> None:
        """A field that owns its width must not also inherit the row's `flex: 1`.

        `controlWidth` was applied by overriding that `flex: 1` with
        flexGrow/flexShrink/flexBasis, which does not override it: both
        spellings reach the host as separate properties, and the hosts answered
        differently.  Yoga kept the zero basis `flex: 1` wrote, so 顺序 and
        并发上限 drew a 2 pt bezel with the number invisible beside it, while the
        page took the stated width.  The cell carries no `flex` at all when a
        width is stated, so `width` is the size on every host.
        """

        self.assert_ui_has('controlWidth === undefined ? styles.formRowControl : styles.formRowControlFixed')
        self.assert_ui_has('controlWidth === undefined ? styles.picker : styles.pickerFixed')
        self.assert_ui_has('controlWidth === undefined ? null : { width: controlWidth }')
        self.assert_ui_has('formRowControlFixed: { minWidth: 0, gap: 3 }')
        self.assert_ui_has('pickerFixed: { minWidth: 0, height: 26 }')
        self.assert_ui_not_has('controlWidth, flexGrow: 0, flexShrink: 0, flexBasis: "auto"')

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
        self.assertIn('iconButton: { minWidth: 24, width: 24, minHeight: 24, height: 24', self.ui)
        self.assertIn('button: { minWidth: 28, height: 24 }', self.native_controls)
        appkit_controls = (ROOT / "rn/packages/shared/src/ui/AppKitControls.tsx").read_text(encoding="utf-8")
        self.assertIn('button: { minWidth: 28, height: 24 }', appkit_controls)

    def test_settings_workspaces_keep_their_legacy_layout_roots(self) -> None:
        expected_components = {
            "AssistantSettingsWorkspace": ("SETTINGS_RAIL_BODY", "externalSettingsPane:"),
            # The pane's body is the shared `SETTINGS_PANE_CONTENT`; what it keeps
            # of its own is the frame, the rail body and the scroll surface.
            "RuntimeWorkspace": ("runtimeWorkspaceFrame:", "SETTINGS_RAIL_BODY", "runtimeScrollSurface:"),
            "DataManagementWorkspace": ("dataManagementWorkspace:", "dataManagementPane:"),
            "WebDavSyncSection": ("settingsActionRowFacts:", "ActionButton title={translate(\"webdav.details\")"),
            "WebDavSettingsPanel": ("webdavDialogSurface:", "webdavDialogBody:", "webdavDialogBodyContent:"),
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
            "contentContainerStyle={SETTINGS_PANE_BODY}",
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
        self.assertIn(
            '<NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}',
            self.ui,
        )
        # Claude's paste-a-token field went with its OAuth: a retry of a login
        # this build cannot start is not a field the pane draws.
        self.assertNotIn('providers.authTypeClaude', self.ui)
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
        self.assertIn('patch["model"] = _direct_model_id(str(row.get("upstream_model") or row.get("model") or ""))', core)
        self.assertNotIn('"model_provider": provider,', core)
        # The saved-route resolution is one shared reader: the same rows answer
        # Codex and both Claude apps, so a route resolves identically in all
        # three and none of them can pick a route the others would not.
        shared = (ROOT / "young_router/core/domains/_shared_api.py").read_text(encoding="utf-8")
        self.assertIn("def one_saved_route(", shared)
        self.assertIn("def saved_routes(", shared)
        self.assertIn("if len(candidates) != 1:", shared)
        self.assertIn("function clientRouteLabel(provider: string, model: string): string {", self.ui)
        self.assertIn("function savedRouteMenuGroups(models: UnknownRecord[], translate: Translate)", self.ui)
        self.assertIn("function ClientBackendFields({ accessibilityPrefix, models, native, localApiActive, designatedRoute, designatedSelection, modelList, modelListHint, busy, onStatus, onUseLocalApi, onUseSavedModel, translate }", self.ui)

    def test_external_settings_list_only_registered_client_files_with_paths(self) -> None:
        # The client controls and the file rows are two components because one
        # rail entry can hold two apps; the pane composes them, so a marker
        # belongs to whichever of the three draws it.
        pane = self.ui.split("function ClientBackendFields(")[1].split("function GeneralWorkspace", 1)[0]
        self.assertIn(
            'const CLIENT_FILE_GROUPS: ReadonlyArray<{ client: ClientFile["client"]; titleKey: string; hintKey: string }> = [',
            self.ui,
        )
        for marker in (
            'accessibilityLabel={labelled("settings.useLocalApi")}',
            'translate("settings.editFile")',
            # The designate control points the client straight at one saved
            # provider route and names the provider and model it points at;
            # its chevron rides the title's trailing edge.
            'title={designatedRoute}',
            # The control names the API the client uses and stays empty on
            # this app's proxy, where no specific API is selected.
            'designatedRoute={localApiActive ? "" : codexRouteLabel}',
            'symbol="chevron-up-down"',
            "symbolTrailing",
            "void onUseSavedModel(selection).finally(() => setDesignateBusy(false));",
            'void showGroupedActionMenu({ title: translate("settings.designateAs"), groups, anchor: { x, y, width, height } })',
            "{designateButtonRef}",
            "void ipc.files()",
            ".then((result) => { if (active) setFiles(result.files); })",
            "{file.name}",
            "{file.display_path}",
            'translate("clients.fileMissing")',
            "onOpenFile(file)",
            "disabled={busy || !file.exists}",
            'toolTip={file.exists ? undefined : translate("clients.fileMissing")}',
            "<SettingsActionRow",
            "assistantFileSurfaceStyles.filePathLink",
        ):
            self.assertIn(marker, pane)
        # The detail body draws no rule of its own: the switches and the file
        # rows are separated by the pane's row rhythm, so the only line in the
        # rightmost column is the shared header band's own divider. A rule per
        # switch and per file (plus a trailing one) turned the pane into a
        # stack of separators.
        self.assertIn('const SETTINGS_FIELD: ViewStyle = { minWidth: 0, alignSelf: "stretch", gap: 2 };', self.ui)
        self.assertIn('const SETTINGS_FIELD_LIST: ViewStyle = { gap: 10 };', self.ui)
        # The client files below the fields start on the fields' own label x.
        self.assertIn('settingsActionRow: { minHeight: 36, flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 4, paddingLeft: SETTINGS_FIELD_LEAD },', self.ui)
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
        # client's built-in list.  The switch writes and reports, and nothing
        # asks the user to restart the client to pick the change up.
        for absent in ("CodexRestartNotice", "showCodexRestartConfirmation", "restartCodex"):
            self.assertNotIn(absent, self.ui)
        for marker in (
            'accessibilityLabel={labelled("settings.useRouterModelList")}',
            'translate("codex.modelCatalogHint")',
            "const toggleCatalog = (enabled: boolean): void => {",
            "void onToggleCodexModelCatalog(enabled).finally(() => setCatalogBusy(false));",
            # This app's model list names models only its proxy serves, so the
            # row is one of the pane's proxy-scoped switches: it is disabled,
            # and says why, while the client points somewhere else.
            "proxyOnly={localApiActive}",
            "disabled={busy || !proxyOnly}",
            'translate("settings.useLocalApiRequired")',
            "SETTINGS_FIELD_LABEL_UNAVAILABLE",
            "SETTINGS_FIELD_LABEL",
            "SETTINGS_FIELD_VALUE_SLOT",
            "SETTINGS_FIELD_HELP_SLOT",
            "SETTINGS_FIELD_SWITCH_SLOT",
            'codexModelCatalogEnabled={codexModelCatalogEnabled}',
            'booleanValue(codexModelCatalogState(snapshot).enabled)',
            'onToggleCodexModelCatalog={(enabled) => dispatch("codex.model_catalog.set", { enabled }, "codex")}',
            'const CODEX_MODEL_CATALOG_FILE_ID = "codex_model_catalog";',
            "const openClientFile = (file: ClientFile): void => {",
            "if (file.id === CODEX_MODEL_CATALOG_FILE_ID) { generatedFileOpen(file, codexModelCatalogEnabled); return; }",
            # Claude Code keeps its picker in its own settings document, so
            # that file is generated too and needs the same warning first.
            'const CLAUDE_CODE_SETTINGS_FILE_ID = "claude.settings";',
            'if (file.id === CLAUDE_CODE_SETTINGS_FILE_ID) { generatedFileOpen(file, booleanValue(claudeCode.model_list_enabled)); return; }',
            'title: translate("codex.modelCatalogEditTitle")',
            'message: translate("codex.modelCatalogEditBody")',
            'confirmLabel: translate("codex.modelCatalogEditConfirm")',
            ".then((confirmed) => { if (confirmed) onOpenFile(file); });",
            "onPress={() => onOpenFile(file)}",
        ):
            self.assertIn(marker, self.ui if marker not in pane else pane)
        for marker in (
            # The client backend is fields of the pane's own settings grid,
            # never a header control beside the detail title: the switch and
            # the control that names the designated provider and model share
            # one label column and one control column.
            '{selectedClient === "codex" ? <>',
            "SETTINGS_FIELD_ROW_INDENTED",
            "SETTINGS_FIELD_LABEL",
            "SETTINGS_FIELD_VALUE_SLOT",
            "SETTINGS_FIELD_HELP_SLOT",
            # One on/off setting is a switch, never a checkbox.
            "<NativeToggle",
            "void onUseLocalApi(designatedSelection ?? {}).finally(() => setClientActionBusy(false));",
            # On the proxy the model is only a public name, so the route the
            # user last chose decides where "off" goes instead of the first
            # matching row; Core remembers it because the client's own file no
            # longer carries it.
            "designatedSelection={codexRoute}",
            # A client that never named a route has no destination to return to,
            # so unchecking asks for one at the control that names a route
            # instead of bouncing the box back onto the proxy.
            "if (designatedSelection) { designateSavedModel(designatedSelection); return; }",
            "const savedModelCount = menuGroups.reduce((total, group) => total + group.labels.length, 0);",
            'onStatus?.(translate("settings.designateAsUnavailable"));',
            'onStatus?.(translate("settings.useLocalApiOffPrompt"));',
            "openSavedModelMenu(designateButtonRef.current);",
            "designateSavedModel(designatedSelection);",
            "value={localApiActive}",
            'accessibilityLabel={labelled("settings.useLocalApi")}',
            'translate("settings.useLocalApiHint")',
            # The switch is a real two-way switch driven by the config: on
            # adopts this app's proxy, off returns the client to the saved
            # route the control names, and a client that never named one is
            # asked for a route instead of having the box bounce back.  A check
            # nobody can answer re-mounts the box onto the configured value.
            "disabled={(busy && !(clientActionBusy || designateBusy)) || clientActionBusy || designateBusy}",
            "onValueChange={toggleLocalApi}",
            "const toggleLocalApi = (enabled: boolean): void => {",
            "const [switchGeneration, setSwitchGeneration] = useState(0);",
            "const assembleSwitch = (): void => setSwitchGeneration((current) => current + 1);",
            "const menuGroups = useMemo(() => savedRouteMenuGroups(models, translate), [models, translate]);",
            "{designateButtonRef}",
            # The control names the saved route's public provider and model
            # (the spelling the menu itself offers) and truncates inside its
            # own column instead of widening the row.
            'titleWidth="flex"',
            'designatedRoute={localApiActive ? "" : codexRouteLabel}',
            'clientProvider={codexClientProvider}',
            'clientModel={codexClientModel}',
            'localApiActive={codexUsesLocalApi}',
            "const codexClientProvider = stringValue(codexStructured.model_provider).trim();",
            'onUseLocalApi={(selection) => dispatch("use_local_api", selection, "codex")}',
            'const codexUsesLocalApi = booleanValue(codexState.uses_local_api);',
            # The same three fields answer for both Claude apps, each naming its
            # own client so Core writes the right document.
            'onClaudeUseLocalApi={(app, selection) => dispatch("use_local_api", { ...selection, client: app }, "claude")}',
            'onClaudeUseSavedModel={(app, selection) => dispatch("use_saved_model", { ...selection, client: app }, "claude")}',
            'onToggleClaudeModelList={(app, enabled) => dispatch("claude.models.set", { client: app, enabled }, "claude")}',
        ):
            self.assertIn(marker, self.ui if marker not in pane else pane)
        # Core -- not the pane -- remembers the route a client leaves: once the
        # client points at this proxy its own file names a public model that
        # several saved routes may share, so only Core can answer where "off"
        # goes back to.
        for path in (ROOT / "young_router/core/domains/codex.py", ROOT / "young_router/core/domains/claude.py"):
            domain = path.read_text(encoding="utf-8")
            self.assertIn('read_route_setting(self.app_state_root, "codex")', domain) if "codex" in path.name else self.assertIn('read_route_setting(self.app_state_root, client)', domain)
        # The path itself is the reveal control: a native link button whose
        # long title is bounded by its own column instead of the button's
        # reserved label width.  It shows the home-shortened spelling of the
        # file while the action still addresses the real path.
        for marker in (
            'titleWidth="flex"',
            'title={file.display_path}',
            "onPress={() => revealFile?.(file.path)}",
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
            "settingsActionRowTitle: { color: systemColors.label, fontSize: UI_FONT_SIZE },",
            self.ui,
        )
        # The Core listing is the only path source, and the pane no longer
        # renders label/input structured editing for any client.
        for removed in ("TextField", "PickerField", "NativeSecretField", "settings.structured", "assistantQuickGrid"):
            self.assertNotIn(removed, pane)
        self.assertIn("SETTINGS_RAIL_BODY", self.ui)
        # The pane aligns its rail exactly like the backup & sync pane: the
        # route body contributes no horizontal padding of its own.
        self.assertIn("(settingsRoute || route === \"runtime-settings\" || route === \"data-management\" || route === \"general-settings\") && SETTINGS_PANE_ROUTE", self.ui)
        self.assertIn("const SETTINGS_PANE_ROUTE: ViewStyle = { paddingHorizontal: 0, paddingTop: 0, paddingBottom: 0 };", self.ui)
        # The pane's rail is the settings window's shared subordinate rail, so it
        # needs no caption, width, or striping of its own.
        self.assertNotIn("externalSettingsRailTitle", self.ui)
        self.assertIn("settingsRailDetailHeader", self.ui)
        # The detail header band carries no rule of its own.  It is a title
        # over content, and the 1 pt rule it drew under itself ran the full
        # detail width like a section divider over nothing; the header keeps
        # its own height, ink and window background.
        self.assertIn(
            'settingsRailDetailHeader: { flexShrink: 0, flexDirection: "row", alignItems: "center", '
            'gap: 12, paddingHorizontal: SETTINGS_PANE_INSET, paddingTop: SETTINGS_HEADER_LEAD, '
            'paddingBottom: SETTINGS_HEADER_GAP, backgroundColor: systemColors.window }',
            self.ui,
        )
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
            'setFooterStatus(translate("common.saving"));',
            'setFooterStatus(translate("common.saved"));',
            'translate("status.close")',
            'translate("status.saveAndClose")',
            "assistantFileSurfaceStyles.editorFooterActions",
            "target?.display_path",
            # Close warns once, then discards the staged draft before the
            # sheet goes away; Save writes the file and closes it.
            "const closeWindow = (): void => {",
            "const coreDraftDirty = current?.drafts[target.domain]?.dirty === true;",
            "if (!coreDraftDirty && !localEditorDirtyRef.current) {",
            "native.showConfirmation(",
            'translate("settings.discardDraftTitle")',
            'translate("settings.discardDraftBody")',
            'translate("common.discard")',
            'type: "cancel"',
            # Its own save result stays beside the two actions, and the buttons
            # keep the trailing edge.
            'setFooterStatus(translate("common.saving"));',
            "assistantFileSurfaceStyles.editorFooterStatus",
        ):
            self.assertIn(marker, editor)
        self.assertNotIn('translate("common.save")', editor)
        self.assertIn("fileEditorRouteContent:", self.ui)
        self.assertIn("menuLabels={editorMenuLabels(translate)}", self.ui)
        self.assertIn(
            'editorFooterActions: { flexShrink: 0, flexDirection: "row", alignItems: "center", gap: 10 },',
            self.ui,
        )
        # The footer is the two actions plus this window's own result line:
        # the old hint text is gone and the actions keep the trailing edge.
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
            'setResult(translate(purpose === "navigate" ? "runtime.fixInvalidBeforeLeaving" : "runtime.fixInvalidBeforeClose"));',
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
            '<View style={[styles.settingsRailDetail, isWindowsShell && styles.settingsRailDetailWindows]}>',
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
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            if key == "YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR":
                continue
            self.assertIn(f"  {key}: {{ label:", localized)
        # A unit is a word the pane prints beside the number, so it has to be in
        # the same language as the label it sits under.  `minutes` was the one
        # the schema stated and this table did not have: 目录刷新间隔 read 分钟's
        # neighbours in Chinese and 15 minutes in English.
        units = sorted(set(re.findall(r"'unit': '([^']+)'", schema)))
        self.assertEqual(len(units), len(set(units)))
        for unit in units:
            self.assertIn(f"\n  {unit}: \"", localized)
        # The Relay and Computer Facade categories disappeared with their only
        # settings; retired dead controls must not stay translated in the pane.
        for category in ("Timeouts", "Recovery", "Web Search", "Vision Router", "Model Context", "Fallback", "MCP", "Logs", "Network", "Service", "Our Free Model"):
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
            # One account read answers the next entry for a window, so the
            # provider pane stops re-reading a WorkBuddy account per mount.
            "YOUNG_ROUTER_SERVICE_ACCOUNT_RECHECK_SECONDS",
            # A refused sign-in is not retried on the next entry, because every
            # attempt asks the station to mint another session.
            "YOUNG_ROUTER_RELAY_LOGIN_BACKOFF_SECONDS",
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
        self.assertIn('{ client: "claude", titleKey: "clients.claude"', self.ui)
        # One Claude section: the terminal and the desktop Code tab read one
        # settings document, so the pane does not draw the same client twice.
        self.assertNotIn('titleKey: "claude.codeSection"', self.ui)
        self.assertNotIn('titleKey: "claude.desktopSection"', self.ui)
        self.assertIn("const claudeApps:", self.ui)
        self.assertIn('models={claudeModels}', self.ui)
        # The external settings pane lists client files instead of structured
        # Codex/Claude fields, so it localizes display metadata only.
        self.assertNotIn("localizeCodexValidationMessage(message, translate)", self.ui)

    def test_sensitive_settings_use_inline_native_password_controls(self) -> None:
        for marker in (
            "function NativeSecretInputControl(",
            "<NativeSecureTextInput",
            "style={[styles.nativeSecretInput",
            "nativeSecretInput: { flex: 1, minWidth: 86, minHeight: 26 },",
            'domain="runtime" field="setting"',
            'domain="webdav"',
            'field="password"',
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
        # A snapshot states the write head and the revision its projection is
        # at; the root merges on both axes instead of taking whichever answer
        # arrived last.  Dropping by head alone let a read answered mid-write
        # overwrite a projection a patch had already advanced.
        self.assertIn("const next = mergeSnapshot(held, incoming);", self.ui)
        self.assertIn("if (next === held) return;", self.ui)
        self.assertIn("if (event.revision <= (snapshotProjectionRevision(current) ?? -1)) return;", self.ui)
        self.assertIn("import { applyPatch, mergeSnapshot, snapshotProjectionRevision } from \"../applyPatch\";", self.ui)
        self.assertNotIn("acceptedSnapshotFingerprint", self.ui)

    def test_route_windows_render_from_the_shared_snapshot_on_the_first_frame(self) -> None:
        ipc = (ROOT / "rn/packages/shared/src/ipc.ts").read_text(encoding="utf-8")
        bootstrap = (ROOT / "rn/packages/shared/src/bootstrap.tsx").read_text(encoding="utf-8")
        platform_entry = (ROOT / "rn/packages/shared/src/platformEntry.ts").read_text(encoding="utf-8")
        self.assertIn("latestSnapshot(): CoreSnapshot | undefined;", (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8"))
        self.assertIn("let initialSnapshotRequest: Promise<CoreSnapshot> | undefined;", ipc)
        self.assertIn('if (latestSnapshot) return readSnapshot();', ipc)
        self.assertIn('if (reason instanceof IpcProtocolError && reason.code === "invalid_response") return fetchSnapshot();', ipc)
        self.assertIn('const refreshed = await refresh().catch(() => latestSnapshot.current ?? snapshot);', self.ui)
        self.assertIn("if (!initialSnapshotRequest)", ipc)
        self.assertIn("rememberSnapshot(event.snapshot);", ipc)
        self.assertIn("const snapshotListeners = new Set<(event: IpcEvent) => void>();", ipc)
        self.assertIn("for (const snapshotListener of snapshotListeners) snapshotListener(event);", ipc)
        self.assertIn("if (!subscriptionStarted)", ipc)
        self.assertIn("const [initialSnapshot] = useState(() => dependencies.ipc.latestSnapshot());", bootstrap)
        self.assertIn("void ipc.snapshot().catch(() => undefined);", platform_entry)
        self.assertIn("const [snapshot, setSnapshot] = useState<CoreSnapshot | undefined>(initialSnapshot);", self.ui)
        self.assertIn('route !== "home" && snapshot ? (isSettingsShellRoute(route)', self.ui)

    def test_explicit_service_operations_republish_the_latest_snapshot(self) -> None:
        self.assertIn("const refreshSnapshot = useCallback(async (publishUnchanged = true)", self.ui)
        self.assertIn("if (publishUnchanged || next.revision !== acceptedSnapshotRevision.current) receiveSnapshot(next);", self.ui)
        self.assertIn("return await refreshSnapshot();", self.ui)
        self.assertNotIn("refreshSnapshot(!background)", self.ui)
        self.assertNotIn("runServiceOperation(\"health\", true)", self.ui)

    def test_image_context_task_list_is_gone(self) -> None:
        # The per-task list of a Codex thread's images (a browsable window, a
        # tick per image, a delete per task) duplicated Context Attachment
        # Manager's own panel and made this app a second owner of that state.
        # What remains is the pass and its runtime switches; no surface, route,
        # IPC method, or Core domain may bring the list back.
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        self.assertNotIn("image-context", routes)
        self.assertNotIn("image-context", self.ui)
        self.assertNotIn("ImageContextWorkspace", self.ui)
        self.assertNotIn("openImageContextWindow", self.ui)
        self.assertNotIn("imageContext", (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8"))
        self.assertNotIn("imageContextImage", (ROOT / "rn/packages/shared/src/ipc.ts").read_text(encoding="utf-8"))
        for path in (
            "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift",
            "rn/apps/macos/macos/YoungRouter-macOS/AppDelegate.mm",
            "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp",
            "rn/apps/windows/src/native/windows/WinUI3NativeLeafModule.cpp",
            "rn/apps/windows/windows/YoungRouter/YoungRouter.cpp",
        ):
            self.assertNotIn("image-context", (ROOT / path).read_text(encoding="utf-8"), path)
        # The remaining child surfaces are the wizard, the raw editor, and the
        # WebDAV detail form.
        self.assertIn('if windowRoute == "provider-wizard" || windowRoute == "file-editor" || windowRoute == "webdav-settings" {', MACOS_LEAF.read_text(encoding="utf-8"))
        self.assertIn(
            'if (!IsChildRoute(route)) return;',
            (ROOT / "rn/apps/windows/windows/YoungRouter/YoungRouter.cpp").read_text(encoding="utf-8"),
        )

    def test_online_usage_is_a_select_driven_log_view_not_a_child_browser(self) -> None:
        # The 在线用量 tab is a log view: a dropdown chooses which configured
        # relay to read, the read goes through Core's own log-format reader, and
        # the rows land in the table this window already draws. Opening the
        # station's dashboard in a native browser is deliberately not a path.
        logs = self.ui.split("function LogsWorkspace(", 1)[1]
        self.assertNotIn("openRelayUsageLogs", logs)
        self.assertNotIn("native.openRelayLogs", logs)
        self.assertNotIn("native.restoreRelaySession", logs)

        refresh = logs.split("const refreshOnlineUsage = useCallback", 1)[1].split("const refreshOnlineUsageRef", 1)[0]
        self.assertIn('type: "logs.refresh_online_usage"', refresh)
        self.assertIn('payload: { tab, source: sourceId, request: String(request) }', refresh)

        # The dropdown names the relays and nothing else: there is no aggregate
        # entry, because the tab reads exactly the station the user picks.
        self.assertIn("const onlineUsageSourceValues = useMemo(", logs)
        self.assertIn('() => ["", ...onlineUsageSources.map((source) => source.id)],', logs)
        self.assertIn('translate("logs.onlineUsageSelectSource")', logs)
        self.assertNotIn('translate("logs.onlineUsageAllSources")', logs)
        self.assertNotIn('"logs.onlineUsageAllSources"', self.en)
        self.assertNotIn('"logs.onlineUsageAllSources"', self.zh)
        # It is the shared native picker (the chevron dropdown), and its
        # selection is what scopes the read.
        self.assertIn('<NativePicker overlayLayer labels={onlineUsageSourceLabels}', logs)
        self.assertIn("refreshOnlineUsage(source, onlineUsageSourceLabels[nativeEvent.index] ?? \"\")", logs)
        # Entering the tab reads nothing until a relay is chosen, so the surface
        # can ask for one instead of reporting that no relay had rows.
        self.assertIn('if (selected === "online-usage" && onlineUsageSourceIdRef.current && !onlineUsageTimePendingRef.current) {', logs)
        self.assertIn('translate("logs.onlineUsageSelectSourceHint")', logs)
        # A remote read reports that it is working: a slow station must not look
        # like a frozen window.
        self.assertIn("const [onlineUsageLoadingTab, setOnlineUsageLoadingTab] =", logs)
        self.assertIn("setOnlineUsageLoadingTab(tab);", logs)
        self.assertIn("settleOnlineUsageLoading(tab);", logs)
        # The relay owns the history, so every pager control is a page request:
        # first, one back, a typed page, one forward, last, and the page size.
        # A view over locally held rows could never reach a page one refresh did
        # not happen to fetch, which is what stranded a 48 000-row account.
        pager = logs[logs.index("usageStatusControls") :]
        self.assertIn("firstOnlineUsagePage", pager)
        self.assertIn("previousOnlineUsagePage", pager)
        self.assertIn("loadMoreOnlineUsage", pager)
        self.assertIn("lastOnlineUsagePage", pager)
        self.assertIn("submitOnlineUsagePage", pager)
        self.assertIn("setOnlineUsagePageRows", pager)
        for action in (
            "logs.first_page",
            "logs.previous_page",
            "logs.load_more",
            "logs.last_page",
            "logs.goto_page",
            "logs.set_page_rows",
        ):
            self.assertIn(f'moveOnlineUsagePage("{action}"', logs)
        self.assertIn("disabled={onlineUsagePagerDisabled || onlineUsageLoadedPage <= 1}", pager)
        # The bar names the page once: the box states it, and a status line
        # repeating it pushed 末页 off the edge of a narrow window.
        self.assertNotIn("styles.logPagerStatus}>{onlineUsagePagerStatus}", pager)
        self.assertIn("disabled={onlineUsagePagerDisabled || onlineUsageRemaining <= 0}", pager)
        self.assertIn("disabled={onlineUsagePagerDisabled || onlineUsageLoadedPage >= onlineUsagePageCount}", pager)
        # The page is stated once, in the pager's own box; the strip beside it
        # states the row count and any read in flight instead of repeating it.
        self.assertIn('{`/ ${onlineUsagePageCount}`}', logs)
        # The window strip is this tab's only bar: the page controls sit at its
        # right end and the row count they page through sits on its left.
        self.assertIn("const statusLine = [", logs)
        self.assertIn('"logs.latestLinesAtLimit" : "common.lines", { count: lineCount }', logs)
        self.assertIn("const onlineUsageLoadedPage = Math.max(1, numberValue(active?.page));", logs)
        self.assertIn("const onlineUsagePageCount = Math.max(onlineUsageLoadedPage, numberValue(active?.page_count));", logs)
        self.assertIn("const onlineUsageNext = Math.min(onlineUsageRemaining, onlineUsagePageRows);", logs)
        self.assertIn("numberValue(active?.total) : 0", logs)
        self.assertIn("ONLINE_USAGE_PAGE_ROWS = 300", self.ui.split("function LogsWorkspace(", 1)[0])
        self.assertIn('scrollOnlineUsage(result.log.source_id ?? "", 0)', logs)
        self.assertIn('scrollRequest={selected === "online-usage" && onlineUsageScrollSourceRef.current === active?.source_id ? onlineUsageScrollRequest : undefined}', logs)
        self.assertIn("onlineUsagePagePending.current = false;", logs)
        self.assertIn("const onlineUsageRemaining = selected === \"online-usage\" ? numberValue(active?.remaining) : 0;", logs)
        self.assertIn("busy={onlineUsageLoadingMore}", logs)
        # One wait, one wheel.  The empty surface owns the first read, the
        # source switch, and the page move, and its own condition
        # (`onlineUsageLoading`) covers the narrower `onlineUsageLoadingSource`
        # the toolbar row used to key on, so a second wheel there spun the same
        # wait twice — one of them above the picker.
        self.assertEqual(1, logs.count("<NativeBusySpinner"))
        self.assertNotIn("{onlineUsageLoadingSource ? <NativeBusySpinner", logs)
        self.assertIn('translate("logs.onlineUsageFiltering")', logs)
        self.assertIn("rows.length > 0 && !onlineUsageWaitingForSource", logs)
        empty_branch = logs.split("styles.logEmptySurface", 1)[1]
        self.assertIn("<NativeBusySpinner", empty_branch)
        # The wheel rides the same row as the sentence it explains.
        self.assertIn("styles.logEmptyStatusRow", logs)
        self.assertIn(
            'logEmptyStatusRow: { flexDirection: "row", alignItems: "center", gap: 8 }',
            self.ui,
        )
        # It is the app's own wheel, not a second indicator drawn for this pane.
        self.assertIn("<NativeBusySpinner label={translate(\"logs.onlineUsageLoading\")} />", empty_branch)
        self.assertIn(
            '"logs.onlineUsageLoading": "正在读取中转站用量…"',
            (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8"),
        )
        # The wait belongs to the read, and only a current read ends it.
        self.assertIn("const onlineUsageLoading = onlineUsageLoadingTab === selected;", logs)
        self.assertIn("if (onlineUsageLoadingTabRef.current !== tab) return;", logs)
        # A pending relay read does not lock the picker. The latest choice can
        # replace the one still waiting.
        picker = logs.split('<NativePicker overlayLayer labels={onlineUsageSourceLabels}', 1)[1].split("/>", 1)[0]
        self.assertNotIn("onlineUsageLoading", picker)
        self.assertIn("disabled={onlineUsageSourceLabels.length === 0}", picker)
        self.assertIn('payload: { tab, source: sourceId, request: String(request) }', refresh)
        self.assertIn("onlineUsageRequest.current", refresh)
        self.assertIn("onlineUsagePendingSourceIdRef.current", refresh)
        self.assertIn("if (onlineUsageWaitingForSource || onlineUsageTimePending) return;", logs)
        self.assertIn("const onlineUsageWaitingForSource =", logs)
        self.assertIn("onlineUsageLoadedSourceRef.current !== onlineUsageSourceId", logs)
        self.assertIn("onlineUsageLoadedSourceRef.current = returnedSource;", logs)
        self.assertIn("settleOnlineUsageLoading(tab);", refresh)
        self.assertIn("const onlineUsageTotal =", logs)
        self.assertIn("ONLINE_USAGE_TIME_OPTIONS", logs)
        self.assertIn('ipc.dispatch({ domain: "logs", type: "logs.set_time_range"', logs)
        time_range = logs.split("const setOnlineUsageTimeRange =", 1)[1].split(
            "const commitOnlineUsageCustomRange",
            1,
        )[0]
        self.assertIn("const request = ++onlineUsageTimeRequest.current;", time_range)
        self.assertIn("setOnlineUsageTimePending(true);", time_range)
        self.assertIn("setActiveState({ tab, log: result.log });", logs)
        self.assertIn("setOnlineUsageTimeOption(option);", time_range)
        self.assertIn("request !== onlineUsageTimeRequest.current) return;", time_range)
        self.assertIn("setOnlineUsageTimePending(false);", time_range)
        self.assertIn("if (request === onlineUsageTimeRequest.current) {\n        setOnlineUsageTimeOption(\"all\");", time_range)
        self.assertIn("onlineUsageLoadedSourceRef.current = result.log.source_id ?? \"\";", logs)
        self.assertIn("const onlineUsagePagerDisabled = onlineUsageLoading || onlineUsageTimePending || onlineUsagePageBusy;", logs)
        self.assertIn("parseLocalDate", logs)
        self.assertIn("textPrompt", logs)
        self.assertIn('"logs.onlineUsageAllTime": "全部时间"', self.zh)
        self.assertIn('"logs.onlineUsageToday": "Today"', self.en)
        self.assertIn("count: onlineUsageNext", logs)
        # A relay that answered no rows says why, instead of the table looking
        # empty for no reason the user can act on.
        self.assertIn("const onlineUsageReason =", logs)
        self.assertIn("stringValue(active?.reason)", logs)
        self.assertIn("? onlineUsageReason", logs)

        self.assertIn('"logs.onlineUsageSelectSource": "Select a relay"', self.en)
        self.assertIn('"logs.onlineUsageSelectSourceHint": "Select a relay above to read its usage."', self.en)
        self.assertIn('"logs.onlineUsageLoading": "Reading relay usage..."', self.en)
        self.assertIn('"logs.onlineUsageSelectSource": "选择中转站"', self.zh)
        self.assertIn('"logs.onlineUsageLoading": "正在读取中转站用量…"', self.zh)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        for key in (
            "logs.onlineUsageSelectSource",
            "logs.onlineUsageSelectSourceHint",
            "logs.onlineUsageLoading",
            "logs.onlineUsageSource",
            "logs.onlineUsageDetail",
            "logs.promptTokens",
            "logs.completionTokens",
            "logs.cacheRead",
            "logs.cacheWrite",
            "logs.channel",
            "logs.group",
            "logs.cost",
            "logs.tokenPair",
            "logs.type",
            "logs.account",
            "logs.onlineUsageLoadMore",
            "logs.onlineUsageLoadingMore",
            "logs.onlineUsageFiltering",
            "logs.onlineUsagePage",
            "logs.onlineUsagePrevious",
            "logs.onlineUsageNext",
        ):
            self.assertIn(f'| "{key}"', translation_keys)
        self.assertIn('"logs.type": "TYPE"', self.en)
        self.assertIn('"logs.type": "类型"', self.zh)
        styles = UI_SOURCE.read_text(encoding="utf-8").split("const styles = StyleSheet.create({", 1)[1].split("\n});", 1)[0]
        for style_name in ("logsWindow", "logsToolbar", "logFilterRow", "logToolbarSpacer", "logActionsRow", "onlineUsageSourcePicker", "clearCooldownButton", "toolbarLabel", "logFilterInput", "logsTabs", "logRangeMessageSlot", "logTablesLayout", "logStrip", "logPagerStatus", "logTableFrame", "logTable", "logEmptySurface", "logEmptyText", "logLoadingText", "logEmptyStatusRow", "logLoadingOverlay", "logLoadingCard", "fieldLabel", "fieldHint", "fieldError", "input", "inputInvalid", "textArea", "compactTextArea", "secretFieldActions", "secretFieldButtons", "secretFieldButton", "nativeSecretControl", "nativeSecretInput", "rawEditor", "rawEditorHeader", "rawNativeEditorFrame", "rawNativeEditor", "rawEditorOverlay", "rawEditorLoading", "empty", "cardHint", "nativeSecretMultilineControl"):
            self.assertIn(f"{style_name}:", styles, style_name)
        native_table = (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(encoding="utf-8")
        logs_render = self.ui.split("function LogsWorkspace(", 1)[1].split("function EmptyState(", 1)[0]
        log_tabs = logs_render.split("<WindowTabs nativeRef={tabsRef}", 1)[1].split("style={styles.logsTabs}", 1)[0]
        log_toolbar = logs_render.split("<View style={styles.logsToolbar}>", 1)[1].split("</WindowTabs>", 1)[0]
        log_toolbar = log_toolbar.split('</View>} style={styles.logsTabs} />', 1)[0]
        self.assertNotIn("logsToolbar", log_tabs)
        self.assertNotIn("onlineUsageSourceLabels", log_tabs)
        self.assertIn("<Text style={styles.toolbarLabel}>{translate(\"common.filter\")}", log_toolbar)
        self.assertIn("labels={onlineUsageSourceLabels}", log_toolbar)
        self.assertNotIn("<View style={styles.onlineUsageControlRow}>", log_toolbar)
        self.assertIn("[self->_tableView scrollPoint:NSMakePoint(0, [self->_tableView rectOfRow:first].origin.y)];", native_table)

        # A row is rendered from the record's own fields, and the relay column
        # carries the station's full name rather than an alias.
        renderer = self.ui.split("function renderOnlineUsageRecord", 1)[1].split("function renderLogRecord", 1)[0]
        self.assertIn('compactLogValue(value.source_label)', renderer)
        self.assertIn("const account = compactLogValue(value.source_account);", renderer)
        self.assertIn('[translate("logs.account"), account],', renderer)
        self.assertIn("const family = compactLogValue(value.source_family);", renderer)
        self.assertIn("[translate(\"logs.type\"), family],", renderer)
        # The cached read and the cache write are two counts from one prompt,
        # under the same spellings the requests tab reads; the share the column
        # shows is drawn from the read against the whole prompt.
        self.assertIn("const cacheReadTokens = compactLogValue(value.cache_read_tokens ?? value.cache_tokens);", renderer)
        self.assertIn("const cacheWriteTokens = compactLogValue(value.cache_write_tokens);", renderer)
        self.assertIn('[translate("logs.cacheRead"), cacheReadTokens],', renderer)
        self.assertIn('[translate("logs.cacheWrite"), cacheWriteTokens],', renderer)
        self.assertIn("cacheHit: formatLogCacheHit(cacheReadTokens, compactLogValue(value.prompt_tokens)),", renderer)
        self.assertNotIn('value.source_label) || compactLogValue(value.source)', renderer)
        self.assertIn('if (tab === "online-usage") return renderOnlineUsageRecord(record, value, translate);', self.ui)
        columns = self.ui.split('if (tab === "online-usage") return [', 1)[1].split("];", 1)[0]
        # The relay column names the station, and the token pair owns the
        # leftover width so a large `in / out` pair is never clipped.
        # The relay's name is its own column, and the control plane that
        # answered is a separate narrow one: fusing them made the source column
        # too wide and named a type as if it were the relay.
        # The relay's name stops at its own ceiling, so a short name does not
        # stretch across the window; the model column takes the slack.
        # The relay and its login are chosen by a dropdown and repeat on every
        # row, so both columns are drawn only when a page actually mixes them:
        # two constant columns cost 214 pt that the duration column needed.
        self.assertIn(
            '...(hasSource ? [{ label: translate("logs.source"), width: 118, maxWidth: 168, flex: true, value: (row: RenderedLogRecord) => row.source }] : []),',
            columns,
        )
        # One relay can host several logins, and only the account column says
        # whose usage a row is.  It is declared the same way the status column
        # is: drawn only when a row actually carries one.
        self.assertIn('...(hasAccount ? [{ label: translate("logs.account"), width: 96, value: (row: RenderedLogRecord) => row.account }] : []),', columns)
        self.assertIn('selected === "online-usage" && hasDistinct(rows, (row) => row.account),', self.ui)
        # The control plane is the station's property, not the row's: it is
        # drawn only when a page actually mixes families, because the dropdown
        # already names it and 82 pt is what pushes the duration out of a narrow
        # window.
        self.assertIn(
            '...(hasFamily ? [{ label: translate("logs.type"), width: 82, value: (row: RenderedLogRecord) => row.family }] : []),',
            columns,
        )
        self.assertIn('{ label: translate("providers.publicModel"), width: 164, flex: true, value: (row) => row.model }', columns)
        # The token pair is as wide as the `34113.8 / 88.5` a busy relay
        # reports (85.7 pt at 13 pt) plus the cell's 8 pt padding on each side.
        # It used to be 128, which is narrower than the text: the cell painted
        # `32642.1 /` and cut the completion count off behind an ellipsis on
        # every row whose counts reached five digits.
        self.assertIn('{ label: translate("logs.tokenPair"), width: 140, value: (row) => row.tokens }', columns)
        # The relay's cached count sits beside the pair it belongs to, under
        # the same name the requests tab gives it.  The header `CACHE HIT` and
        # the widest value a station reports (`9999.9`) both fit 96 pt at the
        # table's 13 pt face, so the width is the header plus padding.
        self.assertIn(
            '...(hasCache ? [{ label: translate("logs.cacheHit"), width: LOG_CACHE_COLUMN_WIDTH, value: (row: RenderedLogRecord) => row.cacheHit }] : []),',
            columns,
        )
        # The money the row was charged is a column of its own, and it is drawn
        # under the same rule the share is: a station that stated no money
        # leaves it out rather than filling it with a credit count.
        self.assertIn(
            '...(hasCost ? [{ label: translate("logs.cost"), width: LOG_COST_COLUMN_WIDTH, value: (row: RenderedLogRecord) => row.cost }] : []),',
            columns,
        )
        # A page of one relay — the case this tab is read in — draws the
        # timestamp, the model, the grouping, the token pair, the cache share,
        # the price and the duration: 732 pt at their own ceilings, inside the
        # narrowest pane.  The relay and account columns belong to a page that
        # mixes stations or logins, and that page is the one the scroller is
        # for; no column is dropped or squeezed to avoid it.
        self.assertLess(120 + 164 + 96 + 140 + 68 + 78 + 66, 860)
        # Every declared width has to hold the longest value that column can
        # carry at the table's 13 pt face: a width under the measured text is
        # what put an ellipsis in the token pair.
        for declared, measured, value in (
            (168, 129.4, "2026-10-24 15:16:53"),
            (118, 58.0, "flux-code"),
            (96, 51.7, "river yang6"),
            (82, 51.7, "Sub2API"),
            (164, 127.5, "gemini-3.1-flash-lite"),
            (140, 111.3, "令牌 输入 / 输出(k)"),
            (78, 60.0, "$0.00439"),
            (62, 40.0, "1234.5"),
        ):
            self.assertGreaterEqual(declared, measured + 16, f"{value} needs {measured + 16} pt")
        # Several flexible columns share one pool of leftover width, so a data
        # column may absorb only up to its ceiling and the rest is the detail
        # column's: giving each flexible column `remaining` would hand the
        # table more width than it has and put the scroller back.
        self.assertIn("let pool = usableWidth - fixedWidth;", self.ui)
        self.assertIn("pool -= width;", self.ui)
        # A narrow window shrinks every column toward its own floor rather than
        # only the flexible one: shrinking the flexible column alone left the
        # fixed ones past the frame, so the token pair was ellipsized and the
        # duration column sat outside the window invisible.
        # A frame that cannot hold the set scrolls; nothing is squeezed or
        # dropped to make it fit.
        self.assertIn("if (pool >= 0) return widened;", self.ui)
        self.assertNotIn("function shrinkLogColumns(", self.ui)
        self.assertNotIn("function columnFloor(", self.ui)
        # The row keeps its timestamp, and the fields a summary endpoint leaves
        # empty share one detail column instead of standing blank beside it.
        self.assertIn("    time,", columns)
        # The detail column is declared at the shared flex floor, so the flex
        # rule gives it exactly that width and the table never scrolls sideways.
        # No detail column: the row's own double-click states every field, and
        # a second flexible column is what pushed the table past its frame.
        self.assertNotIn('{ ...detail, width: LOG_DETAIL_COLUMN_FLOOR }', columns)
        self.assertIn('{ label: translate("logs.duration"), width: 66, value: (row) => row.duration }', columns)
        # The detail column is the one flexible column, and a wide window's
        # spare width is what grows it.
        self.assertIn("const detail = { label: translate(\"logs.detail\"), width: 260, flex: true, detailFlex: true, value: (row: RenderedLogRecord) => row.detail };", self.ui)
        self.assertIn("flex: true, detailFlex: true, value: (row: RenderedLogRecord) => row.detail", self.ui)
        # The status column is drawn only when a row carries one: a summary
        # endpoint has no per-request outcome to report.
        self.assertIn('...(hasStatus ? [{ ...status, width: 64 }] : []),', columns)
        # The grouping is a per-row fact — it is what priced the request — so it
        # is drawn whenever the rows carry one, unlike the constant relay name.
        self.assertIn(
            "  hasGroup = false,\n  hasCache = true,\n  hasCost = true,\n): LogColumn[] {",
            self.ui,
        )
        self.assertIn(
            '...(hasGroup ? [{ label: translate("logs.group"), width: 96, value: (row: RenderedLogRecord) => row.group }] : []),',
            columns,
        )
        # 缓存命中 is a share of the input the token pair states, so it follows
        # that pair rather than standing before it.  It is drawn only when a
        # page has a count to show: a log tail written before the runtime
        # recorded one would otherwise spend 96 pt of the frame on empty cells.
        self.assertIn(
            '...(hasCache ? [{ label: translate("logs.cacheHit"), width: LOG_CACHE_COLUMN_WIDTH, value: (row: RenderedLogRecord) => row.cacheHit }] : []),',
            columns,
        )
        self.assertIn("rows.some((row) => Boolean(row.cacheHit)),", logs)
        # The cell is a rate and nothing else: `100%` is 34.1 pt in the table's
        # 13 pt face, so the header (缓存命中, 51.6 pt) is what sets the width at
        # 68 -- the cached token count that used to sit beside the rate cost 50 pt
        # to restate a share the rate already gives.
        self.assertIn("const LOG_CACHE_COLUMN_WIDTH = 68;", self.ui)
        self.assertGreaterEqual(68, 52 + 16)
        # The row's own fields travel in the detail, one per line, rather than
        # as a re-splitting of the joined string.
        self.assertIn("const details = detailFields.flatMap", renderer)
        self.assertIn("original: details.join(\"\\n\"),", renderer)
        # Double-click states the row's labelled fields, not the raw record.
        self.assertIn('const usageRow = selected === "online-usage";', logs)
        self.assertIn('title: usageRow ? translate("logs.onlineUsageDetail") : translate("logs.originalRecord"),', logs)
        self.assertIn('language: usageRow ? "text" : "json",', logs)

        # The view's own shape carries the selection, and the IPC schema beside
        # it declares the same two fields: the contract moves with the client.
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        self.assertIn("export interface LogSource {", types)
        self.assertIn("sources?: LogSource[];", types)
        self.assertIn("source_id?: string;", types)
        schema = (ROOT / "young_router/core/ipc-v1.schema.json").read_text(encoding="utf-8")
        self.assertIn('"logSource"', schema)
        self.assertIn('"sources": {"type": "array", "maxItems": 32', schema)
        self.assertIn('"source_id": {"type": "string", "maxLength": 64}', schema)
        self.assertIn('"reason": {"type": "string", "maxLength": 512}', schema)
        self.assertIn("reason?: string;", types)

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
        # A refused read names its own cause too: the old catch answered every
        # failure with the dead-Core string, so a document past the raw
        # editor's size budget read as 本地 Core 不可用。
        self.assertIn("setError(errorMessage(reason, translate));", raw_editor)
        self.assertNotIn('setError(translate("error.coreUnavailable"));', raw_editor)
        self.assertIn('core_unavailable: "error.coreUnavailable"', self.ui)
        self.assertIn('editor_too_large: "error.editorTooLarge"', self.ui)
        self.assertIn('"error.editorTooLarge": "文件过大，内置编辑器无法打开。"', (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8"))
        self.assertIn('"error.editorTooLarge": "This file is too large for the built-in editor, so it was not opened."', (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8"))
        self.assertNotIn("throw new IpcProtocolError", raw_editor)
        self.assertLess(
            raw_editor.index("if (descriptor.text === stagedTextRef.current)"),
            raw_editor.index("const resolution = await onConflict(domain, document);"),
        )
        self.assertIn("void ipc.editor(domain, document).then((descriptor) => {", raw_editor)
        self.assertIn("<View style={styles.rawNativeEditorFrame}>", raw_editor)
        # The raw editor shares Ace on both hosts. RNW lays out the child site
        # in DIPs but does not apply PointScaleFactor to its actual size; the
        # Windows native leaf applies that scale at the child-site boundary.
        self.assertNotIn("<NativeTextField", raw_editor)
        self.assertNotIn("styles.rawNativeTextField", raw_editor)
        self.assertIn("<CodeEditorWebView", raw_editor)
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
            'type HostCommand = ReplaceDocumentCommand | SetBaselineCommand | InsertTextCommand | { type: "scale"; scale: number } | { type: "focus" } | { type: "labels"; labels: unknown };',
            'window.addEventListener("message", (event) => {',
            'if (window.parent !== window) window.parent.postMessage(serialized, window.location.origin);',
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
        self.assertIn('return platform === "windows" ? "16px/1.45" : "12px/1.55";', self.code_editor_page)
        self.assertIn("font: ${aceEditorFont(platform)} ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;", self.code_editor_page)
        self.assertIn('${platform === "windows" ? `', self.code_editor_page)
        self.assertIn("zoom: var(--editor-scale);", self.code_editor_page)
        self.assertIn("window.LiteLLMCodeEditorPlatform", self.code_editor_page)
        self.assertIn("aceEditor.setFontSize(baseFontSize * effectiveScale);", self.code_editor_web)
        self.assertIn("applyEditorScale(message.scale);", self.code_editor_web)
        self.assertIn("calc(184px * var(--editor-scale))", self.code_editor_page)
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
            self.assertIn(marker, self.code_editor_page)
        self.assertNotIn("#diff-summary", self.code_editor_page)
        self.assertIn("window.webkit?.messageHandlers?.litellmCodeEditor", self.code_editor_web)
        self.assertIn("const webViewHandler = window.chrome?.webview;", self.code_editor_web)
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["dependencies"].get("ace-builds"), "1.44.0")
        self.assertIn("<NativeCodeWebView", self.code_editor_wrapper)
        self.assertIn("window.LiteLLMCodeEditorInitialCommand", self.code_editor_page)
        self.assertIn("const [initialHtml] = React.useState(() => codeEditorHtml({", self.code_editor_wrapper)
        self.assertIn("Rebuilding the HTML for every document-key change reloads", self.code_editor_wrapper)
        self.assertNotIn("const initialHtml = React.useMemo", self.code_editor_wrapper)
        self.assertIn("html={initialHtml}", self.code_editor_wrapper)
        # The browser host frames the same page as its own document: a
        # `srcdoc`/blob frame would inherit the shell's CSP and never run it.
        self.assertIn('src: "./editor.html"', self.code_editor_wrapper)
        self.assertIn("React.createElement(\"iframe\", {", self.code_editor_wrapper)
        self.assertIn("event.source !== frame.contentWindow", self.code_editor_wrapper)
        self.assertIn("event.origin !== window.location.origin", self.code_editor_wrapper)
        self.assertIn("contentWindow.postMessage(JSON.stringify(message), window.location.origin)", self.code_editor_wrapper)
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
        self.assertIn('translate("logs.emptyNoEntries")', self.ui)
        self.assertIn('translate("logs.loadFailed")', self.ui)
        self.assertIn('translate("logs.loading")', self.ui)
        self.assertIn('filterDraft.trim() ? translate("logs.empty")', self.ui)

    def test_external_settings_edit_claude_files_without_inventing_saved_defaults(self) -> None:
        # Claude documents are now file rows in the shared listing; the pane
        # never renders a structured Claude form or invents a saved default.
        for marker in (
            # One Claude entry in the rail and one set of controls in the
            # detail: the terminal and the desktop Code tab read the same
            # settings document, so a second block only restated the first.
            '{ client: "claude", titleKey: "clients.claude"',
            'hintKey: "clients.claudeFilesHint"',
            "const claudeApps:",
            "const claudeLocalApiActive = claudeApps.every(",
            # The fan-out is stated once for both controls: a switch that reads
            # "on" only when every app is on has to write to every app.
            "const forEveryClaudeApp = async (",
            # The remembered route has to be *absent*, not empty: the shared
            # switch reads absence as "ask for a route" and an empty record as
            # "return to a route", so an always-object answer made the Claude
            # box dispatch an unchosen route where Codex opens the list.
            "const claudeDesignated = (claudeCode.designated_route ?? claudeDesktop.designated_route) as UnknownRecord | undefined;",
            "const claudeRoute = claudeDesignated ? asRecord(claudeDesignated) : undefined;",
            "designatedSelection={claudeRoute}",
            'for (const { app } of claudeApps) await action(app, selection);',
            "onUseLocalApi={(selection) => forEveryClaudeApp(onClaudeUseLocalApi, selection)}",
            "onUseSavedModel={(selection) => forEveryClaudeApp(onClaudeUseSavedModel, selection)}",
            # Every Claude app's files under those controls.
            'files={claudeFiles}',
            "function FileEditorWorkspace",
            'language={target.language === "yaml" ? "yaml" : target.language === "toml" ? "toml" : "json"}',
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
            "SETTINGS_FIELD_ROW_INDENTED",
            "SETTINGS_FIELD_LABEL",
            # One shared grid with the external pane: the label column, the
            # control column, and the tip indent come from the same numbers.
            'const SETTINGS_FIELD_LABEL: TextStyle = { width: SETTINGS_FIELD_LABEL_WIDTH, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE, textAlign: "left" };',
            'const SETTINGS_FIELD_HELP_SLOT: ViewStyle = { paddingLeft: SETTINGS_FIELD_HELP_INDENT, paddingTop: 1, minWidth: 0, alignSelf: "stretch" };',
            "runtimeJsonField:",
            "runtimeMultilineField:",
            "runtimeMultilineHeader:",
            "runtimeMultilineEditor:",
            "runtimeMultilineHelpSlot:",
            'kind === "json"',
            # The JSON document is edited in the shared multiline field; the
            # native secret leaf's island could not be remounted when the pane
            # was reopened.
            "<RuntimeJsonField label={label}",
            "runtimeJsonField:",
            "SETTINGS_FIELD_ACTION_SLOT",
            # A number and its unit share the one value cell, so the unit and
            # the row's action land on the grid instead of drifting with the
            # old full-width field.
            '<View style={textValue ? SETTINGS_FIELD_CELL_WIDE : SETTINGS_FIELD_CELL}>',
            'width={numericKind ? "number" : "text"}',
            "SETTINGS_RAIL_BODY",
            "<SettingsRail rows={tocRows}",
            "runtimeModifiedBar:",
            "runtimeModifiedBarActive:",
            "runtimeFieldError:",
            "runtimeValueControlInvalid:",
            "runtimeResetButton:",
            "runtimeJsonDefaultHint:",
            "<NativePicker labels={optionLabels}",
            "<RuntimeValueField label={label}",
            "accessibilityLabel={label}",
        ):
            self.assert_ui_has(marker)

        self.assertNotIn('label={`${label}${stringValue(item.unit)', self.ui)
        # The multiline editor is a full-width block, so its tip may not keep
        # the value-column indent the shared help slot states as padding, and
        # the wrapper may not reserve a height the field itself overrides:
        # either mistake draws the tip over the editor or beside its left edge.
        self.assert_ui_has("runtimeMultilineEditor: { width: \"100%\", minWidth: 0, alignSelf: \"stretch\" }")
        self.assert_ui_has("runtimeMultilineHelpSlot: { paddingLeft: 0,")
        self.assert_ui_not_has("runtimeMultilineHelpSlot: { marginLeft: 0,")
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
        # becomes the persisted API-key identifier; the selection is handed
        # to the same helper that abandons a listing for the old key.
        for marker in (
            'function providerKeyChoices(provider: UnknownRecord, relaySources: RelaySourceOption[]): ProviderKeyChoice[] {',
            'if (choices.length > 0) return choices;',
            'const slot = providerKeyStates(provider).find((key) => key.managed && managedServiceForSlot(key.managed, providerAuthKind(provider)));',
            'return slot ? [{ id: slot.id, name: slot.name, kind: "independent" as const, state: slot }] : [];',
            'label: providerKeyChoiceLabel({ ...choice, name: choice.name }, translate),',
            'const option = fetchKeyOptions[nativeEvent.index]; if (option) chooseFetchKey(option.value);',
            'const providerKeyOptions = [...keyStates.map((key) => ({',
            'value={selectedProviderKey?.id ?? ""} values={[{ value: "", label: translate("providers.undefinedKey") }, ...providerKeyOptions]} disabled={busy} onSelect={selectProviderKey}',
            'cells: (credentials ?? []).length > 0 && variant !== "inline"',
            'const pendingCustomKeyName = useRef<string | undefined>(undefined);',
            'pendingCustomKeyName.current = undefined; // selection lands after the snapshot refresh' if False else 'setSelectedKey(`custom:${added.id}`);',
            'return dispatch("provider.key_delete", { provider_id: providerId, name: selectedKeyName });',
            'function apiKeyDisplayName(value: unknown, translate: Translate): string {',
            'if (!name) return translate("common.notAvailable");',
            'return name === "default" ? translate("providers.defaultKey") : name;',
            'const sourceName = relaySourceName(choice.source) || name;',
            'return source ? [source.accountLabel, source.resourceLabel].filter(Boolean).join("/") : "";',
            'const accountLabel = username ? username.split("@", 1)[0].trim() || username : stringValue(account.label, accountID).trim();',
            'return `${sourceName}${multiplier}`;',
        ):
            self.assert_ui_has(marker)
        self.assertNotIn('keys.length <= 1', self.ui)

    def test_provider_api_key_deletion_confirmation_explains_model_deletion(self) -> None:
        self.assert_ui_has('const affectedModelLines = asRecords(provider?.models)')
        self.assert_ui_has('.filter((model) => stringValue(model.api_key_name).trim() === selectedKeyName || stringValue(model.provider_key_id).trim() === localSlot.id)')
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
        self.assertIn("XamlDialog dialog;", windows_leaf)
        self.assertIn("RunOwnedModalWindow(dialog, window_handle_", windows_leaf)
        self.assertIn("list.MinHeight(220);", windows_leaf)
        self.assertIn("list.Height(420);", windows_leaf)

    def test_fetch_models_reports_empty_or_unavailable_results_and_does_not_open_stale_chooser(self) -> None:
        for marker in (
            'const dispatchWithOutcome = async (type: string, payload: UnknownRecord = {}, targetDomain = domain, keepControlsEnabled = false): Promise<CoreSnapshot | undefined>',
            'const actionSummary = staged.action_summary;',
            'operation_summary: actionSummary,',
            'const handleFetchedModels = (summary: UnknownRecord, requested?: ProviderKeyChoice): void => {',
            # The provider may be cleared (no list selection): fall back to no
            # identity so a fetch reply for another provider is still ignored.
            'const providerIdentity = provider ? identifier(provider) : "";',
            # The roster is the credential that was asked for, not the one the
            # picker holds when the answer lands: a fetch that lost that race
            # opened the old key's models under the new key's name.
            'const keyName = requested\n      ? providerKeyChoiceLabel({ name: requested.name, kind: requested.kind, source: requested.source }, translate)',
            'const requestedSource = requested?.kind === "relay" ? requested.source : undefined;',
            'stringValue(answeredSource.resource_id) !== requestedSource.resourceID',
            'handleFetchedModels(summary, choice);',
            # The picker follows the route the user selected, so 获取模型 asks
            # the key that route answers with instead of whichever key is first.
            'const selectedModelKeyID = model ? (modelProviderKeyState(model, provider ?? {})?.id ?? "") : "";',
            'if (followedModelKey.current === selectedModel) return;',
            'summaryProviderId !== providerId && summaryProviderId !== providerIdentity',
            'if (summary.available === false)',
            'translate("providers.fetchFailed"',
            'translate("providers.fetchEmpty")',
            # A fetch must speak in the user's terms when the key itself cannot
            # be resolved: 不适用 names no cause and no next step.
            'translate("providers.fetchKeyUnavailable")',
            'const summary = asRecord(asRecord(next.action_summaries?.providers_models).operation_summary);',
        ):
            self.assert_ui_has(marker)
        self.assertIn('"providers.fetchFailed": "获取模型失败：{detail}"', self.zh)
        self.assertIn('"providers.fetchKeyUnavailable": "无法解析该 API 密钥"', self.zh)
        self.assertIn('"providers.fetchEmpty": "供应商未返回模型。"', self.zh)
        self.assertIn('"providers.fetch": "获取模型"', self.zh)
        self.assertIn('"providers.fetchFailed": "Could not fetch models: {detail}"', self.en)
        self.assertIn('"providers.fetchKeyUnavailable": "the API key could not be resolved"', self.en)
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
            'const selectProviderRow = useCallback((key: string): void => {\n    setSelectedProvider(key);\n    setSelectedModel(undefined);\n    setProviderSourceModel(undefined);\n  }, []);',
            workspace,
        )
        self.assertIn(
            '<ProviderListTable translate={translate} selectedProviderId={providerId} providerNameDrafts={providerNameDrafts} validationIssues={validationIssues} onSelect={selectProviderRow} />',
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
        # A provider addressed at an official service links that service; every
        # other provider keeps the relay-station association header.
        self.assertIn('{translate(providerService(provider) ? "providers.serviceLinks" : "providers.accounts")}', editor)
        self.assertIn("providerAccountsHeader", editor)
        self.assertIn("addRelayAccountToVendor", editor)
        self.assertIn("{vendorBaseURL && !providerService(provider) ?", editor)

    def test_a_providers_identity_is_one_block_for_every_kind(self) -> None:
        """Every provider states the same identity rows, in the same order.

        The editor's one section rule sits above the identity block, and a group
        that is genuinely another surface — the keys, the account, the stations
        linked to the provider — opens its own rule.  The identity itself is one
        shared block (`ProviderIdentityFields`) whatever the kind is, so 启用,
        供应商名称, 供应商类型 and 基础 URL always appear in that order, and the
        type is *stated* rather than offered: nothing in the pane retargets a
        provider, so the row reads the type it is.
        """

        editor = self.ui.split("function ProviderEditor(", 1)[1].split("function CodexWorkspace(", 1)[0]
        # The shared block owns the whole identity, and the editor renders it
        # once, between its section rule and the surface groups.
        self.assertIn("<ProviderIdentityFields", editor)
        self.assertEqual(1, editor.count("<ProviderIdentityFields"))
        self.assert_ui_has("function ProviderIdentityFields(")
        self.assertLess(editor.index("<ProviderIdentityFields"), editor.index('{kind === "openai" ? <View'))
        self.assertLess(editor.index("<ProviderIdentityFields"), editor.index("<ProviderKeysPanel"))
        identity = self.ui.split("function ProviderIdentityFields(", 1)[1].split("function ProviderEditor(", 1)[0]
        # One container, one enable row, and the fields the block itself draws.
        self.assertIn("return <View style={styles.providerSourceFields}>", identity)
        self.assertIn('<View style={styles.providerEnabledRow}><NativeCheckbox label={translate("common.enable")}', identity)
        self.assertIn('label={translate("providers.providerName")}\n        labelWidth={88}', identity)
        self.assertIn('label={translate("providers.baseUrl")}\n        labelWidth={88}', identity)
        # The type row is a value, never a control: no picker, no switch
        # confirmation, and no map from a short type to a persisted login kind,
        # because nothing here retargets a provider or one of its routes.
        type_label = '<Text style={styles.providerAuthStatusLabel}>{translate("providers.providerType")}</Text>'
        # One row per branch — the service, the station-bound, and the custom
        # provider — and each of them a plain value beside its label.
        self.assertEqual(3, identity.count(type_label))
        for locale_label in ('label={translate("providers.providerType")}', 'label={translate("providers.wizard.providerType")}'):
            self.assertNotIn(locale_label, identity)
        self.assertNotIn("PROVIDER_TYPE_OPTIONS", self.ui)
        self.assertNotIn("ProviderTypeOption", self.ui)
        self.assertNotIn("selectType", identity)
        self.assertNotIn("typeBusy", identity)
        self.assertNotIn("serviceProviderKindFor", self.ui)
        self.assertNotIn("providers.switchTypeTitle", self.ui)
        self.assertNotIn("providers.switchTypeMessage", self.ui)
        self.assertNotIn("providers.switchTypeToApiKeyMessage", self.ui)
        self.assertNotIn("providers.switchTypeConfirm", self.ui)
        self.assertNotIn("providers.switchTypeTitle", self.zh)
        self.assertNotIn("providers.switchTypeTitle", self.en)
        # Each kind states its own type: a service provider names its service,
        # a relay-bound provider is still an API provider (the station is its
        # association, not its type), and a custom provider is a key provider.
        # A type label is longer than its column, so the value carries the whole
        # name as its hover hint rather than losing the tail to the ellipsis.
        self.assertIn('const providerTypeText = providerKindLabel(kind, translate);', identity)
        self.assertIn('tooltip={providerTypeText} accessibilityLabel={providerTypeText}', identity)
        self.assertIn('{translate("providers.type.apiProvider")}', identity)
        self.assert_ui_has('? translate("providers.type.apiProvider")')
        self.assertIn('{translate("providers.type.apiProvider")}', identity)
        # A login provider states the address its own service gives it, and
        # falls back to the address Core holds when the service table names
        # none — Our Free Model is served by a Core-owned worker on a
        # per-launch port, so its `os.environ/` reference is the only true
        # address and the pane must not print an upstream host for it.  That
        # reference is not an address to show either: it names a variable, it
        # cannot be edited here, and it is cut off in the column, so the row is
        # drawn only when the provider has a real address.
        self.assertIn('isLogin ? (SERVICE_BASE_URLS[kind as ServiceID] || stringValue(provider.endpoint, stringValue(provider.api_base)))', identity)
        self.assertIn('const providerAddressIsReference = providerBaseURL.trim().startsWith("os.environ/");', identity)
        self.assertIn('const showBaseURL = Boolean(providerBaseURL.trim()) && !providerAddressIsReference;', identity)
        self.assertIn('{showBaseURL ? <View style={styles.officialStatusRow}>', identity)
        self.assertNotIn('>{providerBaseURL}</Text>', identity)
        self.assertIn('tooltip={providerBaseURL} accessibilityLabel={providerBaseURL}', identity)
        # The account surface keeps its rule, as the keys and the stations do.
        self.assertIn('{kind === "openai" ? <View style={styles.officialAccountSection}>', editor)
        # Claude's single-account block is gone with its OAuth: no second
        # account section, and no paste-a-token field anywhere in the pane.
        self.assertEqual(1, editor.count("styles.officialAccountSection}"))
        self.assertNotIn('field="provider_auth_token"', editor)
        # Which account the row stands for, above the actions that act on it:
        # 退出登录 over a row that only says "OpenAI" names no session.
        self.assertIn('translate("providers.authAccount")', editor)
        self.assertIn("const authAccount = providerAuthAccount(provider);", editor)
        self.assertIn('"providers.authAccount": "账号"', self.zh)
        self.assertIn('"providers.authAccount": "Account"', self.en)
        self.assertIn("providerAccountsHeader", editor)
        self.assertIn("<ProviderKeysPanel", editor)
        # The section paints above the columns beside it, because its question
        # mark opens a popup that must not be clipped by them.
        self.assert_ui_has("officialAccountSection: { minWidth: 0, gap: 5, paddingTop: 6, zIndex: 3, borderTopWidth: 1, borderTopColor: systemColors.separator }")
        self.assert_ui_has("providerSourceFields: { minWidth: 0, gap: 4 }")
        for locale in (self.zh, self.en):
            self.assertIn('"providers.providerType":', locale)
        self.assertNotIn('"providers.providerUrl"', self.zh)
        self.assertNotIn('"providers.providerUrl"', self.en)

    def test_a_provider_rename_compares_the_submitted_name_to_the_stored_one(self) -> None:
        """A rename is due whenever the field differs from what Core holds.

        ``providerName`` is the draft projection: the field writes it on every
        keystroke through ``onDraftChange``, so by the time a blur commits it
        already equals what was typed.  A uniqueness/no-op guard read against
        that projection therefore answered “nothing changed” for *every* edit —
        the field displayed 新名字, Core kept the old one, and the next snapshot
        re-projected the stale stored name while the draft masked it, so
        retyping the same edit failed identically every time.

        The submitted value must be compared against the *stored* name, which is
        the only thing a write changes.  The draft stays what the field draws.
        """

        identity = self.ui.split("function ProviderIdentityFields(", 1)[1].split("function ProviderEditor(", 1)[0]
        # The stored name is a projection of the provider record alone — never of
        # the draft context — and the commit guard reads exactly that.
        self.assertIn(
            'const committedName = displayLabel(provider.display_name, displayLabel(provider.name, translate("providers.newProvider")));',
            identity,
        )
        self.assertIn("if (!next || next === committedName.trim()) return undefined;", identity)
        # The draft projection stays the field's value; only the guard moved.
        self.assertIn("value={providerName}", identity)
        self.assertIn("onDraftChange={onNameDraftChange}", identity)
        commit = identity.split("const commitName = (name: string)", 1)[1].split("};", 1)[0]
        self.assertNotIn("providerName.trim()", commit)
        # Both identity branches — a login provider's and a key provider's —
        # commit through that one guard, so the fix covers both surfaces.
        self.assertEqual(2, identity.count("onCommit={commitName}"))
        # A rename is only one of the writes a name field owns: the create path
        # derives a name from the first URL, and that suggestion still compares
        # against the displayed value, because there the field is what the user
        # sees and the stored name is still empty.
        self.assertIn(
            "if (suggested && suggested.toLocaleLowerCase() !== providerName.trim().toLocaleLowerCase()) {",
            identity,
        )

    def test_provider_inspector_keeps_the_compact_provider_form_and_return_link(self) -> None:
        """The provider editor uses compact, consistently aligned rows and a source-model return link."""
        for marker in (
            'const [providerSourceModel, setProviderSourceModel] = useState<string>();',
            'function ProviderIdentityFields(',
            'function providerNameExists(providers: UnknownRecord[], name: string, excludeID = ""): boolean {',
            'const [sourceResetToken, setSourceResetToken] = useState(0);',
            'providerNameExists(drafts?.providers ?? [], nextStation.name, providerID)',
            'onBaseUrlDraftChange?.("");',
            'onNameDraftChange?.("");',
            'key={`provider-base-url:${sourceResetToken}`}',
            'key={`provider-name:${sourceResetToken}`}',
            'dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id })',
            'label={translate("providers.providerName")}\n        labelWidth={88}',
            'label={translate("providers.keyName")}',
            'NativeSecretField labelVisible={false} plainText autoCommit label={translate("providers.keyValue")}',
            'title={translate("providers.backToModelShort")}',
            'toolTip={translate("providers.backToModel", { model: sourceModelLabel })}',
            'providerEditorHeader:',
            'providerEditorSection:',
            'formRow: { width: "100%", minHeight: 26',
            'formRowLabel: { width: 112, flexShrink: 0',
            'textAlign: "left"',
            'formRowControl: { flex: 1, minWidth: 0, gap: 3',
        ):
            self.assert_ui_has(marker)
        # Both panes of the provider workspace share one header: 290 pt wide,
        # the heading on a flexBasis-0 row that only takes what is left.  The
        # return link is the same two words here as in the public-model pane —
        # as a full sentence it measured 246 pt of the row and left the title
        # rendering as its own first character, which is the whole header's
        # reason for existing.  The model it returns to rides the tooltip.
        provider_header = self.ui.split("<View style={styles.providerEditorHeader}>", 2)
        self.assertEqual(2, len(provider_header) - 1)
        for header in (provider_header[1], provider_header[2].split("</View>", 1)[0]):
            self.assertIn('title={translate("providers.backToModelShort")}', header)
            self.assertIn('toolTip={translate("providers.backToModel"', header)
        self.assertNotIn('title={translate("providers.backToModel", { model: sourceModelLabel })}', self.ui)
        self.assertNotIn('title={translate("providers.backToModel", { model: backLabel })}', self.ui)
        self.assert_ui_has('label={translate("providers.publicModel")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} value={group.name} disabled={busy}')
        # The model detail draws every row's label from one column of its own,
        # sized by its longest rendered label (Codex 上下文, 80.9 pt at 13 pt)
        # plus COLUMN_GAP.  The old 60 pt column wrapped that label mid-word, and
        # the shared 112 pt column left the controls short of the room the fixed
        # 290 pt pane was built to give them.
        self.assert_ui_not_has('labelWidth={60}')
        self.assert_ui_not_has('modelStatusLabel')
        self.assert_ui_has('const MODEL_INSPECTOR_LABEL_WIDTH = 89;')
        self.assert_ui_has('modelInspectorLabel: { width: MODEL_INSPECTOR_LABEL_WIDTH, flexShrink: 0')
        # Every row in the pane joins that column — a row that falls back to
        # formRowLabel's 112 steps out of it and reads as broken alignment.
        for label in ("publicModel", "provider", "providerKey", "upstream", "order"):
            self.assert_ui_has(f'label={{translate("providers.{label}")}} labelWidth={{MODEL_INSPECTOR_LABEL_WIDTH}}')
        self.assert_ui_has('<TextField label={translate("providers.order")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} controlWidth={64}')
        self.assert_ui_has('<PickerField label={translate("providers.protocolMode")} labelWidth={MODEL_INSPECTOR_LABEL_WIDTH} allowShrink')
        for label in ("modelRate",):
            self.assert_ui_has(f'<Text numberOfLines={{1}} style={{styles.modelInspectorLabel}}>{{translate("providers.{label}")}}</Text>')
        # TextField grows the same accessory slot PickerField already had, so a
        # row with a help mark is not a row with a nested control column.
        self.assert_ui_has('accessory?: React.ReactNode')
        self.assert_ui_has('{accessory ?? null}{suffix ?')
        self.assert_ui_not_has('modelWindowLabel')
        self.assert_ui_has('inspectorBody: { gap: 4 }')
        self.assert_ui_has('protocolSettings: { gap: 4 }')
        self.assert_ui_has('helpTipAnchor: { position: "relative", zIndex: 2 }')
        self.assert_ui_not_has('protocolHint:')
        self.assertIn('"providers.wizard.duplicateName": "供应商名称已存在，请输入其他名称。"', self.zh)
        self.assertIn('"providers.wizard.duplicateName": "A provider with this name already exists. Enter a different name."', self.en)

    def test_a_live_read_never_holds_the_pane_wide_wait(self) -> None:
        """A read of external state must never disable the window it read from.

        The pane-wide wait is acquired *before* a `dispatch` enqueues, so a
        buffered queue headed by a slow live read produced a window that could
        neither use its controls nor name the wait that was disabling them.
        Live reads therefore run on their own lane and every one of them asks
        for the wait to stay off.
        """

        for marker in (
            "const TRANSIENT_READ_ACTIONS: ReadonlySet<string> = new Set([",
            '"workbuddy_status",',
            '"workbuddy_models",',
            '"workbuddy_login",',
            '"service_provider_auth_status",',
            '"provider_auth_status",',
            # Reads that stage nothing share one identity, so a slow relay
            # refresh or model fetch also stays off the queue of edits.
            "const NON_STAGING_ACTIONS: ReadonlySet<string> = new Set([",
            '"provider_fetch_models",',
            '"provider_fetch_relay_resource_models",',
            "function isNonStagingAction(type: string): boolean {",
            "const unbuffered = isNonStagingAction(type);",
            "? issue()\n      : dispatchQueue.current.catch(() => undefined).then(issue);",
            "if (!unbuffered) dispatchQueue.current = queued.then(() => undefined, () => undefined);",
        ):
            self.assert_ui_has(marker)

        # Every live read names the wait as somebody else's: `keepControlsEnabled`
        # is the fourth argument of both `dispatchWithOutcome` and the
        # `dispatchSnapshot` alias the model inspector takes.
        reads = (
            "workbuddy_status",
            "workbuddy_models",
            "workbuddy_login",
            "service_provider.auth_status",
            "provider_auth_status",
        )
        # The catalog reads are addressed through `serviceCatalog`, which takes
        # its action from `serviceCatalogRead`: the literal name lives in that
        # table, so a scan for the name alone would stop seeing the reads this
        # table exists to name.
        self.assert_ui_has('if (service === "workbuddy") return { action: "workbuddy_models", providerId: "workbuddy" };')
        # The channel is what Core filters the roster by, so the id travels with
        # the read instead of being fixed to the anonymous lane.
        self.assert_ui_has('if (service === "ourFreeModel") return { action: "our_free_model_models", providerId: channel ?? "our-free-model" };')
        live_reads = 0
        for index, line in enumerate(self.ui.splitlines()):
            for helper in ("dispatchWithOutcome(", "dispatchSnapshot("):
                from_table = f"{helper}action," in line
                if helper not in line or not (from_table or any(f'"{action}"' in line for action in reads)):
                    continue
                live_reads += 1
                arguments = self.call_arguments(line, line.index(helper) + len(helper) - 1)
                self.assertGreaterEqual(
                    len(arguments), 4,
                    f"line {index + 1} dispatches a live read without keepControlsEnabled: {line.strip()}",
                )
                self.assertEqual(
                    arguments[3], "true",
                    f"line {index + 1} lets a live read hold the pane-wide wait: {line.strip()}",
                )
        # The account read, the catalog read (twice: the pane's cache and the
        # inspector's rate), the desktop-app hand-off, and both authorization
        # polls are all covered.
        self.assertGreaterEqual(live_reads, 6)

    def test_a_snapshot_projects_the_account_without_reading_it(self) -> None:
        """Core answers the account question only where it was asked."""

        domain_source = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        for marker in (
            "def _managed_projection_status(",
            "def _managed_observed_account(",
            "def _managed_account_observe(",
            "self._managed_accounts_observed[provider_id] = {",
            '"auth_observed": self._managed_observed_account(provider_auth),',
            # The three live answers, and nothing else.
            "self._managed_account_remember(document)",
            'self._managed_account_remember({"providers": {provider_id: document.get("status")}})',
            "provider, self._provider_auth_state(provider), allow_live=True",
        ):
            self.assertIn(marker, domain_source, marker)
        # A projection reaches for the worker only when it is allowed to.
        projection = domain_source.split("def _managed_projection_status(", 1)[1].split("def _managed_account_auth_status(", 1)[0]
        self.assertEqual(projection.count("self._managed_account_login_status(auth)"), 1)
        self.assertIn("if allow_live:", projection)

    def test_workbuddy_provider_types_borrow_the_desktop_app_sign_in(self) -> None:
        """WorkBuddy keeps its sign-in and its model catalog in its own app."""
        for marker in (
            'type WizardType = "api" | "openai" | "workbuddy" | "workbuddyGlobal" | "qoder" | "qoderGlobal" | "ourFreeModel" | "ourFreeModelKilo";',
            'const isWorkBuddyType = providerType === "workbuddy" || providerType === "workbuddyGlobal";',
            'const workbuddyAuthKind: ServiceProviderKind = providerType === "workbuddyGlobal" ? "workbuddy_global_login" : "workbuddy_login";',
            'dispatchWithOutcome("workbuddy_status", { refresh }, "providers_models", true);',
            # A managed-account action carries no draft value to invalidate, and
            # staging the PAT in the same press advances the shared revision:
            # without the rebase the sign-in reported a conflict it never had.
            "const TRANSIENT_READ_ACTIONS: ReadonlySet<string> = new Set([",
            '  "qoder_status",',
            '  "qoder_models",',
            '  "qoder_login",',
            '  "qoder_logout",',
            '  "qoder_open_app",',
            '    || normalized.startsWith("qoder_")',
            # The wizard's model step states the account beside the roster it
            # makes readable, and 完成 creates the entry with the entries the
            # user picked — never a second visit to the pane.
            'kind: isQoderType ? qoderAuthKind : workbuddyAuthKind,',
            'models: kept,',
            'const kept = modelCandidates.filter((model) => selectedModels.includes(model));',
            'translate("providers.wizard.serviceUrlFixed")',
            '"providers.wizard.workbuddyAssociationHint"',
            'translate("providers.wizard.authPath")',
            'translate("providers.wizard.pathWebLogin")',
            'translate("providers.wizard.pathCustomKey")',
            'const refreshWorkBuddyAccount = async (): Promise<void> => {',
            'if (account && stringValue(account.state) !== "signed-in") void loadWorkBuddyAccount(true);',
            # The account re-read is an icon button, like the relay block's +.
            'symbol="refresh" compact toolTip={translate("providers.wizard.workbuddyRefresh")}',
            # The account links in the one association block; models keep the
            # provider's own key slot and the key-driven 获取模型 path.
            'const refreshWorkBuddyAccount = async (): Promise<void> => {',
            'const isOfficialAccount = kind === "openai";',
            'const isWorkBuddyAccount = kind === "workbuddy" || kind === "workbuddyGlobal";',
            'const service = providerService(provider);',
            'translate("providers.wizard.providerType")',
            'translate("providers.wizard.baseUrl")',
            # The model detail states the service's own rate.
            'translate("providers.modelRate")',
            'async function serviceCatalog(',
            'const SERVICE_RATE_TTL_MS = 5 * 60 * 1000;',
            # The rate is cached, so switching models never reflows the pane.
            'function peekServiceRate(service: ServiceID | undefined, modelID: string): string | undefined {',
            'const [serviceRate, setServiceRate] = useState<string>(() => peekServiceRate(service, upstreamModelID) ?? "");',
            # The same cached catalog is what Core learns each model's window
            # from, so the read is addressed through the one table that names
            # both a service's action and the id its worker document uses.
            'function serviceCatalogRead(service: ServiceID, channel?: string): { action: string; providerId: string } | undefined {',
            'if (service === "ourFreeModel") return { action: "our_free_model_models", providerId: channel ?? "our-free-model" };',
            # A service provider's type is an ordinary editable field: the
            # pane offers the same picker the wizard does, so WorkBuddy is
            # wired and retargeted like any other service.
            # The detail pane is the ordinary one: it states the name, the
            # bound address, and the account — never a type picker, because the
            # provider's service is what it is already bound to.
            'label={translate("providers.providerName")}',
            'isLogin ? (SERVICE_BASE_URLS[kind as ServiceID] || stringValue(provider.endpoint, stringValue(provider.api_base)))',
            # Every label column in the pane is the shared one, so a rate row
            # lines up with the fields beside it.  An unstated rate paints
            # blank, not 无: the upstream having published no rate and this
            # pane not having read one are different facts.
            'providerAuthStatusLabel: { width: 88, flexShrink: 0, color: systemColors.label, fontSize: UI_FONT_SIZE }',
            '{routeRate}</Text>',
            # One association block is labelled by the provider's service, its
            # right-hand control follows the sign-in state, and an account-backed
            # provider keeps its key slot and fetch path.
            '{translate(providerService(provider) ? "providers.serviceLinks" : "providers.accounts")}',
            '{(!isLogin || isWorkBuddyAccount || isQoderAccount) && !station ? <View style={styles.providerAccountsHeader}>',
            # An account-backed row is a real provider: it takes ＋ for a model
            # that names the account it answers with.
            'provider ? <IconButton label="+" title={translate("providers.newModel")}',
            # An unread account is handed to the desktop app, then confirmed.
            'const workbuddySignedIn = stringValue(workbuddyAccount?.state) === "signed-in"',
            '|| stringValue(asRecord(provider.auth_observed).state) === "signed-in";',
            'await dispatchWithOutcome("workbuddy_login", { provider: providerKey }, "providers_models", true);',
            'title: translate("providers.wizard.workbuddyLoginTitle", { name: workbuddyName }),',
            # Reading the account never disables the rest of the pane, and the
            # pane-wide wait is counted only by the runs that ask for it.
            'await dispatchWithOutcome("workbuddy_status", { refresh }, "providers_models", true);',
            'const busyRuns = useRef(0);',
            # The block that releases the wait, distinguished from the one that
            # acquires it by the decrement it performs.
            'busyRuns.current = Math.max(busyRuns.current - 1, 0);',
            'if (busyRuns.current === 0) setBusy(false);',
        ):
            self.assert_ui_has(marker)
        # The account is stated on the model step, beside the roster it makes
        # readable; the WorkBuddy branch asks for no WorkBuddy secret.
        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        workbuddy_model = wizard.split('</> : isWorkBuddyType ? <>', 1)[1].split('</> : null}', 1)[0]
        self.assertNotIn("NativeSecureTextInput", workbuddy_model)
        self.assertNotIn("NativePicker", workbuddy_model)
        # Both products and both service labels are named for the user.
        for text in (self.en, self.zh):
            self.assertIn('"providers.type.workbuddy": "WorkBuddy"', text)
            self.assertIn('"providers.type.workbuddyGlobal": "WorkBuddy Global"', text)
            self.assertIn('"providers.wizard.workbuddyCredits":', text)
            self.assertIn('"providers.serviceLinks":', text)
        self.assertIn('"providers.wizard.workbuddySignInRequired": "Sign in to the WorkBuddy desktop app first."', self.en)
        self.assertIn('"providers.wizard.workbuddySignInRequired": "请先在 WorkBuddy 桌面应用中登录。"', self.zh)
        self.assertIn('"providers.serviceLinks": "服务商关联"', self.zh)
        self.assertIn('"providers.wizard.pathCustomKey": "自定义 API 密钥"', self.zh)

    def test_a_catalog_service_walks_two_steps_and_creates_with_its_picks(self) -> None:
        """Qoder, WorkBuddy and the free lane share one model step.

        The three publish their own catalogs, so none of them has a 密钥 step:
        the rail is 供应商 → 模型, and the pick is part of the create instead of
        a second visit to 获取模型.  The step lists the public name the row will
        serve (`Qwen3.8-Max` as `qwen3.8-max`) while the pick itself stays the
        catalog id, because the adapter owns that mapping.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        for marker in (
            'const isServiceCatalogType = isOurFreeModelType || isQoderType || isWorkBuddyType;',
            # No 密钥 step for a keyless service: the walk is 供应商 → 模型.
            'const needsKeyStep = !isServiceCatalogType;',
            'const serviceCatalogRequest = serviceType === undefined || !isServiceCatalogType',
            # One branch draws the model step for all three types.
            'isServiceCatalogType ? <View style={styles.providerWizardModelPane}>',
            'const fetchServiceCatalogModels = async (): Promise<void> => {',
            'else if (isServiceCatalogType) void fetchServiceCatalogModels();',
            'onPress={() => { void fetchServiceCatalogModels(); }}',
            # The label is the spelling the created row will serve, in lower
            # case; the pick that travels back is the catalog id.
            'function serviceCatalogModelLabel(entry: UnknownRecord, lane: boolean): string {',
            'return (stringValue(entry.publicName).trim() || modelID).toLowerCase();',
            # 完成 creates the entry with the entries the user ticked.
            'kind: isQoderType ? qoderAuthKind : workbuddyAuthKind,',
            'models: kept,',
        ):
            self.assert_ui_has(marker)
        # The account strip has no refresh of its own: 重新获取模型 is the one
        # read that re-states the account and lists the roster again.
        self.assertNotIn("providers.wizard.qoderRefresh", wizard)
        self.assertNotIn("providers.wizard.workbuddyRefresh", wizard)
        # No token input exists anywhere: both accounts are the desktop apps'
        # own sign-ins, so the step states the account and offers the app —
        # never a field for a credential of this app's own.
        self.assertNotIn("qoderPat", wizard)
        self.assertNotIn("qoder_pat", wizard)
        self.assertNotIn("qoderSignIn\"", wizard)
        self.assertNotIn("qoderSignOut", wizard)
        self.assertNotIn("NativeSecureTextInput", wizard.split("isQoderType ? <>", 1)[1].split("</> : isWorkBuddyType ? <>", 1)[0])
        self.assertNotIn("NativeSecureTextInput", wizard.split("isWorkBuddyType ? <>", 1)[1].split("</> : null}", 1)[0])
        self.assert_ui_has('translate("providers.wizard.qoderOpenApp")')
        self.assert_ui_has('translate("providers.wizard.workbuddyOpenApp")')
        # ...and no product wording offers one, in either language.
        for text in (self.en, self.zh):
            for banned in ("Personal Access Token", "PAT", "个人访问令牌"):
                self.assertNotIn(banned, text)
        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        for marker in (
            "selected: Sequence[str] | None = None,",
            "provider_id, refresh=selected is None",
            "self._managed_catalog_models(kind, selected=catalog_picks)",
            "The service catalog no longer lists a selected model",
        ):
            self.assertIn(marker, domain, marker)

    def test_a_fetched_roster_checks_nothing_by_default(self) -> None:
        """A listing offers its models; it does not choose them.

        Pre-selecting every row handed the new entry a set nobody picked, and
        no other service does that — a WorkBuddy entry is created with no
        models at all.  A read keeps what the user already ticked and leaves
        the rest unchecked.
        """

        self.assertNotIn("current.length > 0 ? current : candidates", self.ui)
        self.assert_ui_has("setSelectedModels((current) => current.filter((name) => candidates.includes(name)));")

    def test_each_model_step_states_one_wait_with_one_wheel(self) -> None:
        """One read draws one spinner, whichever step is asking.

        The model listing used to answer a single fetch with three wheels — the
        toolbar's own, a second on the button that started it, and a block in
        the body repeating the same sentence — so the pane looked like three
        things were running.  A wait is stated once, in the row that names it.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        # One standalone wheel per branch (the official account's roster, the
        # lane's roster, the generic listing), the sign-in wait, and the key
        # read/grouping wait the signed-in key step states.  The buttons that
        # start those reads draw none of their own for the same wait.
        self.assertEqual(5, wizard.count("<NativeBusySpinner"))
        self.assertNotIn('busy={modelFetchState === "loading"}', wizard)
        self.assertNotIn("providerWizardModelLoading", wizard)
        self.assertNotIn("providerWizardModelLoading", self.ui)

    def test_the_opencode_free_lane_is_offered_without_an_account_step(self) -> None:
        """Our Free Model has no sign-in, so the wizard asks nothing.

        The lane authenticates itself, so the type appears in the picker and
        its walk is 供应商 → 模型: the model step lists the lane's own roster,
        and 完成 creates the entry and the picked rows together.  Its name
        carries the "Free" qualifier, because the same endpoint refuses a paid
        model under the anonymous credential — an unqualified name would read
        as an account for Zen's paid catalog.
        """

        for marker in (
            '{ type: "ourFreeModel", label: translate("providers.type.ourFreeModel") },',
            'const isOurFreeModelType = providerType === "ourFreeModel" || providerType === "ourFreeModelKilo";',
            'if (isOurFreeModelType) {',
            'kind: "our_free_model_login",',
            'translate("providers.wizard.ourFreeModelHint")',
            '"our_free_model_status",',
            '"our_free_model_models",',
            # The model step lists the lane's own free roster, so the user picks
            # what the entry carries instead of being handed the whole catalog.
            # The same read and the same step serve Qoder and WorkBuddy: their
            # own catalogs answer through `serviceCatalogRead`.
            'const isServiceCatalogType = isOurFreeModelType || isQoderType || isWorkBuddyType;',
            'const serviceCatalogRequest = serviceType === undefined || !isServiceCatalogType',
            'const fetchServiceCatalogModels = async (): Promise<void> => {',
            'else if (isServiceCatalogType) void fetchServiceCatalogModels();',
            'onPress={() => { void fetchServiceCatalogModels(); }}',
            # One entry per channel this bridge can fetch: adding a channel adds
            # that channel, so the picker offers each of them and Core reads the
            # stored tag to know which.  The upstream's co-paid channel has no
            # roster here (its seal never opens in this host), so it is not an
            # entry: a wizard option that could only fail its import is worse
            # than no option at all.
            '{ type: "ourFreeModelKilo", label: translate("providers.type.ourFreeModelKilo") },',
            'chooseProviderType(servicePickerItems[nativeEvent.index]?.type ?? "api");',
            'const ourFreeModelProviderID = ourFreeModelProviderIDFor(providerType);',
            # A channel's entry is named after the channel, because the name is
            # what Core reads to know which one it is.
            'const laneChannelName = OUR_FREE_MODEL_CHANNEL[providerType]?.name;',
            # The channel is stored identity, so a rename cannot retarget the row.
            'channel: OUR_FREE_MODEL_CHANNEL[providerType]?.tag ?? "",',
            'ourFreeModel: { providerId: "our-free-model", tag: "", name: "Our Free Model · OpenCode" },',
            ': channelName ? nextServiceProviderName(providers, "our_free_model_login", channelName)',
            # A keyless service has no 密钥 step to walk through: its next step
            # is 模型, where the catalog it publishes is listed.
            'const needsKeyStep = !isServiceCatalogType;',
            # 完成 creates with the models the user kept.
            'const kept = modelCandidates.filter((name) => selectedModels.includes(name));',
            # The list carries the same two tools the model chooser offers:
            # one press for everything, one for the complement.
            'translate(selectedModels.length === modelCandidates.length ? "dataManagement.deselectAll" : "dataManagement.selectAll")',
            'translate("modelChooser.invert")',
            # The picker groups by the producer the upstream catalog tagged, and
            # a producer heading is one level above the availability split.
            'const [laneChannels, setLaneChannels] = useState<Record<string, string>>({});',
            'function laneChannelOrder(channels: readonly string[]): string[] {',
            'function laneChannelLabel(channel: string, translate: Translate): string {',
            'const names = modelCandidates.filter((name) => (laneChannels[name] ?? LANE_CHANNEL_FREE) === channel);',
            'laneChannelLabel(channel, translate)} ({names.length})',
            'const showRouteHeadings = splitRoutes.length > 1',
            'setLaneChannels(channels);',
            # The header names the provider for what it is, so a bare name
            # beside 模型 is never read as a model id.
            'translate("providers.wizard.providerName")}: {selectedProviderName}',
            # The imported public name is the upstream's own id; renaming is
            # the user's decision, not the importer's, and the spelling is the
            # answer this step's case question got.
            'function publicModelNameForLane(modelID: string): string {',
            'const keptNameCase = await importNameCaseAnswer(native, translate, kept);',
            'upstream_model: model_id,',
            'imported_name_case: keptNameCase,',
            # Deleting the entry goes through the account-backed action, which
            # is the only one Core accepts for a login provider; routing it to
            # the ordinary provider action is an entry the user cannot remove.
            'const action = isLoginProviderKind(kind)',
        ):
            self.assert_ui_has(marker)
        # The one service-catalog branch is the model step for all three
        # keyless services, and it is the only branch that lists their rosters.
        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("isServiceCatalogType ? <View style={styles.providerWizardModelPane}>", wizard)
        self.assertIn("isOurFreeModelType ? <>", wizard)
        self.assertIn('translate(isOurFreeModelType ? "providers.wizard.ourFreeModelHint" : isQoderType ? "providers.wizard.qoderModelsHint" : "providers.wizard.workbuddyAssociationHint")', wizard)
        self.assertNotIn("fetchOurFreeModelModels", wizard)
        # Both languages name the type for the user, not for the integration:
        # the picker and the entry's own name are "Our Free Model" and the
        # adapter that serves it is `our_free_model` on disk.
        # Each channel is a parallel entry, and the entry states which one it is:
        # the name is what Core reads to know the channel.
        self.assertIn('"providers.type.ourFreeModel": "Our Free Model · OpenCode channel"', self.en)
        self.assertIn('"providers.type.ourFreeModelKilo": "Our Free Model · Kilo channel"', self.en)
        self.assertIn('"providers.type.ourFreeModel": "Our Free Model · OpenCode 通道"', self.zh)
        self.assertIn('"providers.type.ourFreeModelKilo": "Our Free Model · Kilo 通道"', self.zh)
        # No wording survives for a channel this build does not offer, in either
        # language or in the key union: a dead key is how the entry would creep
        # back into the picker.
        self.assertNotIn("ourFreeModelEAC", self.en)
        self.assertNotIn("ourFreeModelEAC", self.zh)
        self.assertNotIn("providers.laneChannel.eac", self.en)
        self.assertNotIn("providers.laneChannel.eac", self.zh)
        # Nothing in the shared UI still names the removed entry: a leftover
        # wizard type or channel id is how the picker would offer it again.
        self.assertNotIn("ourFreeModelEAC", self.ui)
        self.assertNotIn("our-free-model-eac", self.ui)
        # One roster, several upstreams: each producer heading is its own word
        # in both languages, because the ids cannot tell the lanes apart.
        for key, zh in (
            ("providers.laneChannel.free", "OpenCode 免费通道"),
            ("providers.laneChannel.kilo", "Kilo 通道"),
        ):
            self.assertIn(f'"{key}": "{zh}"', self.zh, key)
        for key, en in (
            ("providers.laneChannel.free", "OpenCode free lane"),
            ("providers.laneChannel.kilo", "Kilo channel"),
        ):
            self.assertIn(f'"{key}": "{en}"', self.en, key)

    def test_an_imported_lane_name_is_the_upstream_id_unchanged(self) -> None:
        """Importing a roster does not rename its models.

        A lane id is the upstream's public name.  Stripping a suffix from it
        invented a spelling no upstream document contains, and left the same
        model with two names depending on whether it arrived through the wizard
        or a fresh catalog read.  Renaming is the user's, in the pane.
        """

        from young_router.core.domains import providers_models as module

        for model_id in (
            "cat-jam-free",
            "big-pickle",
            "-free",
            "free",
            "openrouter/free",
            "kilo-auto/free",
        ):
            self.assertEqual(module._public_name_for_lane_id(model_id), model_id)
        # The UI states the identical rule, so neither side can drift alone.
        self.assert_ui_has("function publicModelNameForLane(modelID: string): string {")
        self.assert_ui_has("  return modelID;")
        self.assert_ui_not_has('const suffix = "-free";')

    def test_a_workbuddy_entry_states_the_account_and_its_credit(self) -> None:
        """Opening a WorkBuddy provider shows the积分, not just the account.

        The worker fills ``credits`` only on a read that asked for it, so the
        block's mount read has to be the credit-carrying one: a plain account
        read answers the sign-in question alone, and the pane then showed an
        account row and no credit row however many times it was opened.  The
        credit is a row of its own while signed in, an unstated credit is
        never rendered as a zero, and the record Core last observed supplies
        both facts until the block's own read lands.
        """

        for marker in (
            # The mount read asks for the credit document, and the retry after
            # a cold worker answers "not signed in" asks again.
            'void loadWorkBuddyAccount(true).then((account) => {',
            'if (account && stringValue(account.state) !== "signed-in") void loadWorkBuddyAccount(true);',
            # Both facts come from the live read with the observed record under
            # it, so the whole block is filled in from its first paint.
            'const workbuddyObserved = asRecord(provider.auth_observed);',
            'const workbuddyFacts = workbuddyAccountFacts(asRecord(workbuddyAccount), workbuddyObserved);',
            'function workbuddyAccountFacts(live: UnknownRecord, observed: UnknownRecord): UnknownRecord {',
            'if (!workbuddyCreditsStated(live) && workbuddyCreditsStated(observed)) merged.credits = observed.credits;',
            'function workbuddyCreditsStated(account: UnknownRecord | undefined): boolean {',
            # The credit row is unconditional for a signed-in account: the read
            # states a number, the wait, or that no read produced one.
            '{workbuddySignedIn ? <View style={styles.officialStatusRow}>',
            '<Text style={styles.providerAuthStatusLabel}>{translate("providers.wizard.workbuddyCredits")}</Text>',
            '<Text numberOfLines={1} style={styles.providerAuthStatusValue}>{workbuddyCreditsText(workbuddyFacts, translate) || progressText(workbuddyStatusBusy, translate) || translate("providers.workbuddyCreditUnavailable")}</Text>',
            'translate("providers.workbuddyCreditUnavailable")',
        ):
            self.assert_ui_has(marker)
        # An unstated credit is never a conditional row that simply vanishes,
        # and the credit helper never invents a zero for a missing record.
        self.assert_ui_not_has('{workbuddyCreditsText(workbuddyAccount, translate) ? <View style={styles.officialStatusRow}>')
        self.assert_ui_not_has('workbuddyCreditsRead')
        # A missing record states nothing, and a stated one reads in whole
        # credits: upstream sums its packages to a float, and printing that
        # tail claims a precision the balance does not have.
        self.assert_ui_has('if (typeof total !== "number" || !Number.isFinite(total)) return "";')
        self.assert_ui_has('const whole = Math.round(total);')
        self.assert_ui_has('if (whole === 0 && total > 0) return total.toFixed(1);')
        # Rounding a real balance to "0" would state an empty account.
        self.assert_ui_has('return String(whole);')
        for text in (self.en, self.zh):
            self.assertIn('"providers.workbuddyCreditUnavailable":', text)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        self.assertIn('| "providers.workbuddyCreditUnavailable"', translation_keys)

    def test_a_rate_row_only_remembers_a_catalog_it_actually_read(self) -> None:
        """A catalog read that named no model never becomes the cached answer.

        The model detail's 倍率 row is fed by one service catalog, and that
        read can come back without a catalog at all: a cold worker's first
        answer, a failed upstream read, or a summary another window's action
        already replaced in the shared slot.  Remembering that as this
        service's rates hides every 倍率 for the whole cache interval — the
        row cannot even ask again, because its own cache claims the question
        was answered.  Only a read that answers with this service's own
        catalog is stored, and the row retries once when it is told nothing.
        """

        for marker in (
            # The read's own summary is the only one this row accepts.
            'if (summary.operation !== request.action || summary.available !== true) return undefined;',
            '): Promise<UnknownRecord[] | undefined> {',
            # A read that named no catalog is not cached, and the row asks once
            # more instead of painting 无 until the interval expires.
            'if (!options?.force && cached && Date.now() - cached.at < SERVICE_RATE_TTL_MS) return cached.catalog;',
            'const readRates = (force: boolean): void => {',
            'if (catalog) { setServiceRate(peekServiceRate(service, upstreamModelID) ?? ""); return; }',
            'if (!force) readRates(true);',
            'readRates(false);',
        ):
            self.assert_ui_has(marker)
        # An empty read is never stored as an answer: the cache write sits
        # behind the gate that accepts only this service's own catalog.
        self.assertLess(
            self.ui.index('if (summary.operation !== request.action || summary.available !== true) return undefined;'),
            self.ui.index("serviceRates.set(service, { at: Date.now(), catalog: models, rates: serviceCatalogRates(models) });"),
        )
        self.assert_ui_has('if (catalog) { setServiceRate(peekServiceRate(service, upstreamModelID) ?? ""); return; }')

    def test_the_rate_column_reads_the_multiplier_the_catalog_publishes(self) -> None:
        """The staged library publishes ``creditMultiplier``; there is no ``billing``.

        ``serviceCatalogRates`` is the one place a service catalog's rate is
        turned into the string every pane prints, so the field it reads decides
        whether 倍率 exists at all.  Reading only ``billing`` emptied the column
        and left 跟随倍率 -- which asks whether a rate is known -- unreachable.
        ``0`` is a real answer here, so presence decides and not truthiness.
        """

        self.assert_ui_has('const statedRate = typeof entry.rate === "number" ? entry.rate : entry.creditMultiplier;')
        self.assert_ui_has('if (typeof statedRate === "number" && Number.isFinite(statedRate) && statedRate >= 0) {')
        self.assert_ui_has('rates[modelID] = rateSpelling(`x${statedRate}`);')
        # A stated `0` must reach the column: an entry the upstream prices at
        # nothing is a rate a route may follow, and `0` is falsy.
        self.assertNotIn('if (entry.creditMultiplier)', self.ui)
        # The older raw `billing` shape is still read, so a staged release that
        # trails this build keeps answering.
        self.assert_ui_has('rates[modelID] = rateSpelling(credits.replace(/\\s*credits?$/i, ""));')
        # The count of the field's readers is one: the pane and the list both go
        # through the cached catalog built here.
        self.assertEqual(1, self.ui.count('rates[modelID] = rateSpelling(`x${statedRate}`);'))

    def test_a_rate_is_printed_without_its_trailing_zeros(self) -> None:
        """`x0.00` reads as `x0`, and `x0.10` as `x0.1`, in both places a rate shows.
        """

        # The spelling lives in one module both panes import, so the model list
        # column and the relay group sheet cannot diverge.
        spelling = (ROOT / "rn/packages/shared/src/ui/rateSpelling.ts").read_text(encoding="utf-8")
        self.assertIn('/** One stated rate with the `credits` unit word dropped. */', spelling)
        self.assertIn('export function rateSpelling(', spelling)
        self.assertIn('export function multiplierSpelling(', spelling)
        self.assertIn('return prefix + String(number);', spelling)
        self.assert_ui_has('import { multiplierSpelling, rateNumber, rateSpelling } from "./rateSpelling";')
        self.assert_ui_has('rates[modelID] = rateSpelling(credits.replace(/\\s*credits?$/i, ""));')
        self.assert_ui_has('if (relayMultiplier !== undefined) return multiplierSpelling(relayMultiplier);')
        relay = (ROOT / "rn/packages/shared/src/ui/RelayAccountManager.tsx").read_text(encoding="utf-8")
        self.assertIn('import { multiplierSpelling } from "./rateSpelling";', relay)
        self.assertIn('return multiplier === null ? "" : multiplierSpelling(multiplier);', relay)
        # The old inline spellings are gone: a second copy is how the two panes
        # drifted apart in the first place.
        for inline in (
            'return `×${multiplier}`;',
            'return multiplier === null ? "" : `×${multiplier}`;',
        ):
            self.assertNotIn(inline, self.ui)
            self.assertNotIn(inline, relay)
        # A rate nobody stated stays blank rather than reading 无: the upstream
        # having published no number and this pane not having read one are
        # different facts, and only the first is a statement about the upstream.
        self.assert_ui_has('return rate === undefined ? "" : rateNumber(rate) !== undefined ? rate : "";')
        self.assert_ui_has('{routeRate}</Text>')
        self.assertNotIn('routeRate || translate("common.none")', self.ui)
        self.assertNotIn('rate === undefined ? "" : serviceRateNumber(rate) !== undefined ? rate : translate("common.none")', self.ui)

    def test_unified_provider_workspace_lists_every_provider_kind(self) -> None:
        """服务商管理 is integrated: the provider table shows all kinds."""
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")

        self.assertIn(
            'export type ProviderAuthKind =\n'
            '  | "api_key"\n'
            '  | "openai_login"\n'
            '  | "workbuddy_login"\n'
            '  | "workbuddy_global_login"\n'
            '  | "qoder_login"\n'
            '  | "qoder_global_login"\n'
            '  | "our_free_model_login";',
            types,
        )
        self.assertIn("auth_status?: ProviderAuthStatus;", types)
        self.assertNotIn("function ServiceProviderManager(", self.ui)
        self.assertNotIn('native.window.open("relay-accounts")', self.ui)
        self.assertNotIn('route === "relay-accounts"', self.ui)
        self.assertNotIn('route === "relay-add"', self.ui)
        for marker in (
            "function providerKind(provider: UnknownRecord | undefined): ProviderKind {",
            "function providerService(provider: UnknownRecord | undefined): ServiceID | undefined {",
            "function serviceFromURL(value: string): ServiceID | undefined {",
            "function providerAuthKind(provider: UnknownRecord | undefined): ProviderAuthKind {",
            "function providerKindLabel(kind: ProviderKind, translate: Translate): string {",
            "const providers = useMemo(() => snapshotProviderRecords(snapshot), [snapshot?.providers_models.providers, state.providers]);",
            "const providers = useMemo(() => asRecords(state?.providers), [state]);",
            "const name = providerNameDrafts[id] !== undefined",
            "return { key: id, cells: [name] };",
        ):
            self.assert_ui_has(marker)
        # Deleting routes through the kind-specific core action.
        self.assert_ui_has('const action = isLoginProviderKind(kind)')
        self.assert_ui_has('? "service_provider.delete"')
        self.assert_ui_has(': "provider.delete";')
        # Deleting the last row that reaches a station takes the station and its
        # accounts with it — in Core's own delete, not by a second write from
        # this pane: a delete that happens with this window closed must still
        # clear the sign-in, and the pane's version only knew bound providers.
        self.assert_ui_not_has('await relay.commit("station.remove", { id: stationBeingRemoved.id, dependency_policy: "detach" });')
        provider_workspace = self.ui.split("function ProviderWorkspace(", 1)[1].split("function ProviderEditor(", 1)[0]
        self.assertIn("&& !providers.some((entry) => entry !== provider && providerReachesStation(entry, station))", provider_workspace)
        self.assertIn("function providerReachesStation(provider: UnknownRecord, station: { id: string; origin: string }): boolean {", self.ui)
        self.assert_ui_has('translate("providers.deleteRelayProviderBody", {')
        self.assertIn('"providers.type.apiProvider": "API 供应商"', self.zh)
        # 供应商类型 names the service, not one of its model families: the
        # OpenAI entry reads OpenAI, the name the wizard's own provider takes.
        self.assertIn('"providers.type.openai": "OpenAI"', self.zh)
        self.assertIn('"providers.type.openai": "OpenAI"', self.en)
        self.assertNotIn('"providers.type.openai": "GPT"', self.zh)
        self.assertNotIn('"providers.type.openai": "GPT"', self.en)
        self.assertIn('"providers.type.apiKey": "API 密钥"', self.zh)
        self.assertIn('"providers.type.apiProvider": "API provider"', self.en)
        self.assertIn('"providers.type.apiKey": "API key"', self.en)

    def test_official_account_login_lives_in_the_provider_detail(self) -> None:
        editor = self.ui.split("function ProviderEditor(", 1)[1].split("function CodexWorkspace(", 1)[0]
        for marker in (
            'dispatchWithOutcome("service_provider.auth_start", { provider_id: id }, "providers_models")',
            'presentProviderAuthChallenge(native, translate, next, kind, providerName, id, shownChallenge.current)',
            'authAction === "service_provider.auth_start"',
            'if (authAction === "service_provider.auth_start") {',
            'await dispatch(authAction, { provider_id: id }, "providers_models");',
            'translate("relay.officialProviderAddLogin")',
            # A second account is a second account of this row, not a second
            # provider: the sign-in starts on the provider the pane shows.
            'dispatchWithOutcome("service_provider.auth_start", { provider_id: id }, "providers_models")',
            "officialStatusRow",
        ):
            self.assertIn(marker, editor)
        # The one flow left is a device code, so its challenge is a code and a
        # URL: a loopback redirect belonged to the retired Claude OAuth flow.
        self.assertNotIn("callbackURL", self.ui)
        self.assertNotIn('"service_provider.auth_activate"', editor)
        self.assert_ui_has('translate("relay.officialProviderMultiAccountHint")')
        self.assertIn("function presentProviderAuthChallenge(", self.ui)
        self.assertIn("native.showProviderAuth({", self.ui)
        # The workspace polls authorizing providers so logins started in the
        # wizard window still complete here.
        poll = self.ui.split("// Poll official-account authorizations", 1)[1].split("}, [dispatchWithOutcome, native, providers, translate]);", 1)[0]
        self.assertIn('providerAuthStatus(entry) === "authorizing"', poll)
        self.assertIn('"service_provider.auth_status"', poll)
        self.assertIn("setInterval", poll)
        self.assertIn('"providers.authStatusUnsupported": "暂不支持登录"', self.zh)
        # One official sign-in is left, and it is OpenAI's.  Claude's own OAuth
        # is gone, so neither its hint nor its paste-a-token field may return.
        self.assertNotIn('field="provider_auth_token"', editor)
        self.assertNotIn("将打开 Claude 官方网页完成登录", self.zh)
        self.assertNotIn("official Claude sign-in page opens in your browser", self.en)

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
        apply_body = self.ui.split("const applyOutcome = (options?: { silent?: boolean; quiet?: boolean; message?: string }):", 1)[1].split("const autoAppliedKey", 1)[0]
        self.assertIn('settingsRoute || route === "providers-models" || route === "data-management" || domain === undefined', apply_body)
        self.assertIn("await dispatchQueue.current;", apply_body)
        self.assertIn('return { draftStaged: next.drafts.relay_accounts?.dirty === true };', self.ui)

    def test_one_relay_read_serves_every_implicit_caller(self) -> None:
        """One station round trip per account; a sign-in is the only forced read."""

        refresh = self.ui.split("const refreshRelayResources = useCallback(async (", 1)[1].split("const relayApiKeyActions = useMemo", 1)[0]
        self.assertIn("const RELAY_RESOURCE_REUSE_MS = 15_000;", self.ui)
        self.assertIn("const relayRefreshInFlight = useRef(new Map<string, Promise<\"ready\" | \"unavailable\">>());", self.ui)
        self.assertIn("const relayRefreshFresh = useRef(new Map<string, { at: number; status: \"ready\" | \"unavailable\" }>());", self.ui)
        # An implicit caller joins the read in flight, or reuses the read that
        # just succeeded; a failed read is never reused.
        self.assertIn("if (!forced && inFlight) return inFlight;", refresh)
        self.assertIn("if (fresh && Date.now() - fresh.at < RELAY_RESOURCE_REUSE_MS) return fresh.status;", refresh)
        self.assertIn('if (status === "ready") relayRefreshFresh.current.set(accountId, { at: Date.now(), status });', refresh)
        self.assertIn("relayRefreshInFlight.current.delete(accountId);", refresh)
        # The station round trip after a sign-in is the one read that must not
        # come out of the reuse window, in the panel and in both host flows.
        forced = [line for line in self.ui.splitlines() if "refreshResources(landed?.id ?? pendingID" in line or "refreshResources(landedID" in line]
        self.assertEqual(2, len(forced), "the wizard and the relay pane both read the account the login landed on")
        for line in forced:
            self.assertIn("{ force: true }", line)

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
        # One rebase was not enough: the retry re-read the snapshot and met
        # another of this app's own writes already moving the revision.  The
        # rebase repeats a bounded number of times, and every attempt takes the
        # revision it just read rather than resending the one it lost.
        self.assertEqual(1, dispatch.count("await ipc.dispatch(action,"))
        self.assertIn("const DISPATCH_ATTEMPTS = 3;", dispatch)
        self.assertIn("if (attempt + 1 >= DISPATCH_ATTEMPTS) throw reason;", dispatch)
        self.assertIn("await new Promise<void>((resolve) => setTimeout(resolve, 40 * (attempt + 1)));", dispatch)
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
        # A service provider's own name and enabled switch are absolute values
        # addressed by a stable editor id, exactly like a custom provider's, so
        # they rebase too.  Without this a rename lost the race against the
        # pane's own debounced Apply, came back as a revision conflict, and the
        # field visibly reverted to the old name.
        self.assertIn('normalized === "service_provider_patch"', retryable)
        self.assertIn('normalized === "service_patch_provider"', retryable)
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
        self.assertIn('if (!actionDomain && targetDomain !== null) {', dispatch)
        self.assertIn('const missingDomain: Error & { code?: string } = new Error("A settings domain is required");', dispatch)
        # The refusal carries a code so `errorMessage` can state it in the UI's
        # own language: the sentence alone was an English string in a Chinese app.
        self.assertIn('missingDomain.code = "missing_domain";', dispatch)
        self.assertIn("return Promise.reject(missingDomain);", dispatch)
        self.assertIn("const action = actionDomain === undefined ? { type, payload } : { domain: actionDomain, type, payload };", dispatch)

    def test_background_launch_switch_is_one_label_and_states_no_consequence(self) -> None:
        """A two-state switch is the whole statement; the caption under it was not.

        The 启动 row used to carry a sentence explaining what each side of the
        switch does -- a second telling of the control beside it, which is the
        shape a settings field may not take. A switch whose label already says
        what it is on (`启动后在后台运行` / `Run in background at startup`) needs
        no caption, so the key is gone rather than emptied: a leftover key is a
        row that can come back.
        """

        types = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for name in ("general.runInBackground",):
            self.assertIn(f'| "{name}"', types)
            self.assertIn(f'"{name}":', english)
            self.assertIn(f'"{name}":', chinese)
        self.assertIn('"general.runInBackground": "启动后在后台运行"', chinese)
        self.assertIn('"general.runInBackground": "Run in background at startup"', english)
        for stale, table in (
            ("general.runInBackgroundHint", types),
            ("general.runInBackgroundHint", english),
            ("general.runInBackgroundHint", chinese),
            ('<Text style={SETTINGS_FIELD_HELP_TEXT}>{translate("general.runInBackgroundHint")}</Text>', self.ui),
        ):
            self.assertNotIn(stale, table)

    def test_language_row_states_auto_and_names_each_language_in_itself(self) -> None:
        """One choice, three values, and no second copy of the list.

        The preference is drawn in General beside the service it renames:
        自动 / English / 简体中文.  A language name is the same string in both
        tables -- a reader looking for 中文 should not have to recognise 英文 --
        and 自动 is a preference rather than the noun "system" it is stored as.
        """

        types = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")
        for name in ("general.interface", "general.language", "language.auto"):
            self.assertIn(f'| "{name}"', types)
            self.assertIn(f'"{name}":', english)
            self.assertIn(f'"{name}":', chinese)
        # The language's own name, identical on both sides.
        for table in (english, chinese):
            self.assertIn('"language.english": "English",', table)
            self.assertIn('"language.simplified_chinese": "简体中文",', table)
        self.assertIn('"language.auto": "自动"', chinese)
        self.assertIn('"language.auto": "Auto"', english)
        # The old menu wording is gone with the menu.
        for stale in ('"language.system"', '"status.language"', "language-picker", "set-language-"):
            self.assertNotIn(stale, self.ui)
            self.assertNotIn(stale, english)
            self.assertNotIn(stale, chinese)
        # No caption under the picker: the three values are the whole statement,
        # and 自动 is not the same platform on the two hosts, which a sentence
        # under the control could only try to restate.  The key is gone in both
        # tables for the same reason the background switch lost its own.
        for table in (types, english, chinese, self.ui):
            self.assertNotIn("general.languageHint", table)

    def test_no_user_facing_table_mixes_the_other_language_in(self) -> None:
        """A sentence is in one language, not one language with the other inside.

        Two shapes were shipped: an English sentence wearing Chinese corner
        brackets around an English phrase, and Chinese sentences keeping English
        nouns that have Chinese words in the very same table (桌面 App beside
        本应用, Web 页面, API Key).  A language name is the one deliberate
        exception -- it states itself -- and so are product names, config keys,
        placeholders and file extensions.
        """

        import re

        english = (ROOT / "rn/packages/shared/src/i18n/en.ts").read_text(encoding="utf-8")
        chinese = (ROOT / "rn/packages/shared/src/i18n/zh-Hans.ts").read_text(encoding="utf-8")

        def values(text: str) -> list[tuple[int, str, str]]:
            rows = []
            for index, line in enumerate(text.splitlines(), 1):
                match = re.match(r'\s*"([^"]+)": "(.*)",$', line)
                if match:
                    rows.append((index, match.group(1), match.group(2)))
            return rows

        cjk = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
        for index, key, value in values(english):
            # The language's own name is the one CJK string an English table
            # states; everything else is the other language leaking in.
            if key in {"language.english", "language.simplified_chinese"}:
                continue
            self.assertIsNone(cjk.search(value), f"en.ts:{index} {key} carries CJK: {value}")
        for index, key, value in values(chinese):
            for phrase in ("桌面 App", "App 未登录", "Web 页面", "Web 密钥", "API Key", "Young Router"):
                self.assertNotIn(phrase, value, f"zh-Hans.ts:{index} {key} keeps {phrase!r}: {value}")

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
        # The retry budget is spent by *attempts*, not by renders.  The effect
        # below keys on the service state and on this callback, so a callback
        # that took ``snapshot`` as a dependency changed identity on every
        # poll: each snapshot re-ran the effect, walked the whole backoff in a
        # second, and left a stopped router with no attempt left -- the window
        # then sat on 已停止 until the app was relaunched.  The optimistic
        # "starting" copy comes from a ref instead, so the callback is stable.
        self.assertIn("const current = latestSnapshot.current;", self.ui)
        self.assertIn(
            "receiveSnapshot({ ...current, service: { ...current.service, state: \"starting\" } });",
            self.ui,
        )
        self.assertNotIn("receiveSnapshot({ ...snapshot, service: { ...snapshot.service, state: \"starting\" } });", self.ui)
        self.assertIn(
            "}, [hostTranslate, ipc, native, receiveSnapshot, refreshSnapshot]);",
            self.ui,
        )
        self.assertIn('const serviceRestart = serviceState === "unhealthy" || serviceState === "starting";', general)
        self.assertIn('const serviceActionAvailable = serviceState === "stopped" || serviceRestart;', general)
        # A transitional state is a claim about Core, and Core is the authority
        # that clears it; a window re-reads while it draws one, so a replacement
        # that outlives the request budget cannot leave the pane stating 启动中
        # with no control and no correction.
        self.assertIn("const SERVICE_STATE_RECONCILE_MS = 1_000;", self.ui)
        self.assertIn('if (snapshot.service.state !== "starting") return;', self.ui)
        # The reconcile exists to correct one word; reading the whole projection
        # for it, once a second, is the wait it is meant to end.
        self.assertIn("const timer = setInterval(() => { void refreshServiceState().catch(() => undefined); }, SERVICE_STATE_RECONCILE_MS);", self.ui)
        self.assertIn("return () => clearInterval(timer);", self.ui)
        # Every window that draws the state re-reads it: the 服务 row is drawn by
        # the settings window, which is not the primary host, so gating the
        # reconcile on the primary host made the one surface that shows the word
        # the one surface that could never correct it.
        self.assertIn("  }, [refreshServiceState, snapshot?.revision, snapshot?.service.state]);", self.ui)
        # A settled failure is re-read as well.  Core repairs an unhealthy
        # proxy without this window, so a pane that only polled while running
        # kept stating 不健康 (with 重启服务 beside it) over a proxy that had
        # already answered for minutes -- the state that made a manual restart
        # look necessary.
        self.assertIn("const SERVICE_UNHEALTHY_RECONCILE_MS = 5_000;", self.ui)
        self.assertIn('if (snapshot?.service.state !== "unhealthy") return;', self.ui)
        self.assertIn("}, SERVICE_UNHEALTHY_RECONCILE_MS);", self.ui)
        self.assertNotIn('if (!isPrimaryHost || !snapshot) return;\n    // A transitional state is a claim about a proxy replacement', self.ui)
        # 运行中 is the other claim no window can verify: a Core replaced under
        # an idle app leaves the last snapshot standing, and the 服务 row then
        # states 运行中 (端口 12390) over a port nothing answers on.  未知 is
        # settled the same way -- a listener this installation does not own,
        # whose disappearance lets Core start the proxy it does own with no
        # event for this window.  Both re-read slowly, and they accept an
        # unchanged revision because a replacement restores that revision from
        # the same record.
        self.assertIn("const SERVICE_SETTLED_RECONCILE_MS = 15_000;", self.ui)
        self.assertIn('settled !== "running" && settled !== "unknown"', self.ui)
        # The settled and unhealthy intervals read the plan too: the row they
        # correct is one field of it, and a projection every 15 s is a page
        # downloading its own settings for a word that did not change.
        self.assertIn(
            "const timer = setInterval(() => { void refreshServiceState().catch(() => undefined); }, SERVICE_SETTLED_RECONCILE_MS);",
            self.ui,
        )
        self.assertIn('await dispatchServiceAction(serviceRestart ? "service.restart" : "service.start");', general)
        # The strip names the action that landed; the 服务 row already states the
        # state, so the same word is never printed twice on one screen.
        self.assertIn('onStatus(translate(serviceRestart ? "service.restarted" : "service.started"));', general)

    def test_the_web_page_is_one_key_and_no_second_port(self) -> None:
        """The page lives on the proxy's own port, so General states no port.

        A second port field is the failure this pins against: the page is
        answered at ``/ui`` on the local port, so a "web port" would be a knob
        that changes nothing.  The key is the one credential of this section and
        it is drawn in the clear, on the same plaintext lease as the API key.
        """

        general = self.ui.split("function GeneralWorkspace(", 1)[1].split("function RuntimeWorkspace(", 1)[0]
        for marker in (
            'translate("general.webKey")',
            'target="YOUNG_ROUTER_WEB_PASSWORD"',
            'translate("general.webAddress")',
            "webPageAddress",
            "const webPageAddress = (() => {",
            "`http://127.0.0.1:${port}/ui`",
        ):
            self.assertIn(marker, general)
        for gone in (
            "webHostRoute",
            "commitWebHost",
            "generateWebPassword",
            'translate("general.webPort")',
            'translate("general.webPassword")',
        ):
            self.assertNotIn(gone, general)
        # The address is the one thing in a pane that is meant to be pressed:
        # pressing it opens a page this window has already proved itself to, so
        # the key never has to be copied into another browser.
        for marker in (
            'accessibilityRole="link"',
            "void openWebPage()",
            "await ipc.webLaunchUrl()",
            "native.openExternalURL?.(address)",
        ):
            self.assertIn(marker, general)
        for locale in (self.zh, self.en):
            self.assertIn('"general.webKey"', locale)
            self.assertIn('"general.webAddress"', locale)
            for gone in ('"general.webPort"', '"general.webPassword"', '"general.webHost"'):
                self.assertNotIn(gone, locale)

    def test_background_launch_is_a_promise_the_general_pane_keeps_and_the_launch_honors(self) -> None:
        """启动后在后台运行 is off by default, so a launch presents its window.

        The app is menu-bar first, so a launch that shows nothing is silent
        about whether the router is even alive. The switch is therefore off by
        default (no marker file, the snapshot's answer is disabled) and a
        launch without the marker opens 供应商与模型; only an explicit promise
        keeps the launch behind the menu bar, on both hosts, through the shared
        "home" term that already means "hide the shell".
        """

        general = self.ui.split("function GeneralWorkspace", 1)[1].split("function RuntimeWorkspace", 1)[0]
        # The switch reads the same projected preference the launch reads, and
        # an absent answer is the default (a launch presents its window).
        self.assertIn('const backgroundEnabled = snapshot?.service.launch_background_state === "enabled";', general)
        self.assertIn("const backgroundValue = requestedBackground ?? backgroundEnabled;", general)
        self.assertIn("const [requestedBackground, setRequestedBackground] = useState<boolean>();", general)
        # A rejected dispatch hands the switch back to what Core kept, the same
        # contract the autostart switch keeps.
        self.assertIn("setRequestedBackground(undefined);", general)
        self.assertIn('await dispatchServiceAction(enabled ? "service.launch_background_enable" : "service.launch_background_disable");', general)
        self.assertIn('onStatus(translate("common.saved"));', general)
        self.assertIn("onStatus(errorMessage(reason, translate));", general)
        self.assertIn('<Text style={SETTINGS_FIELD_LABEL}>{translate("general.runInBackground")}</Text>', general)
        self.assertIn("<NativeToggle value={backgroundValue}", general)
        # The preference is stored before anything moves on screen: a promise
        # the host acts on must never outlive a failed Core write.
        self.assertIn("await dispatchServiceAction(", general)
        # Turning it on takes effect at once — the window the user is looking
        # at is the one a background launch would not have shown.
        self.assertIn('native.window.focus("home");', general)
        self.assertNotIn('native.window.open("providers-models")', general)
        # The launch itself is a one-shot decision, and a host that already has
        # a route to show has presented the launch on its own.
        self.assertIn("const launchPresented = useRef(false);", self.ui)
        self.assertIn("if (!isPrimaryHost || !snapshot || launchPresented.current) return;", self.ui)
        self.assertIn("if (routeRequest && routeRequest !== \"home\") return;", self.ui)
        self.assertIn('if (snapshot.service.launch_background_state === "enabled") {', self.ui)
        self.assertIn('native.window.open("providers-models");\n    native.window.focus("providers-models");', self.ui)
        # A service preference write is absolute Core state, never a staged
        # draft, so it rebases the shared revision like launch-at-login does.
        retryable = self.ui.split("function isRevisionRetryableAction(type: string): boolean", 1)[1].split("\n}", 1)[0]
        self.assertIn('normalized === "service_launch_background_enable"', retryable)
        self.assertIn('normalized === "service_launch_background_disable"', retryable)
        self.assertIn("onStatus(errorMessage(reason, translate));", general)
        for locale in (self.zh, self.en):
            self.assertIn('"service.started"', locale)
            self.assertIn('"service.restarted"', locale)
        self.assertIn('title={serviceRestart ? translate("service.restart") : translate("service.start")}', general)
        # The action reports its own progress instead of swapping its title or
        # graying out while the service restarts.
        self.assertIn("busy={serviceBusy} disabled={snapshot === undefined || (busy && !serviceBusy)}", general)
        self.assertIn("{serviceActionAvailable ? <NativeButton", general)
        # The row keeps the grid's action column, shared with the runtime pane,
        # so its button sits on the same x as every other row's action.
        self.assertIn("const SETTINGS_FIELD_ACTION_SLOT: ViewStyle = { width: 72, minHeight: 26, flexShrink: 0, justifyContent: \"center\" };", self.ui)
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
            "{ label: translate(\"logs.duration\"), width: 66, value: (row) => row.duration }",
            "{ label: translate(\"logs.tokenCountK\"), width: LOG_TOKEN_PAIR_COLUMN_WIDTH, value: (row) => row.tokens }",
            "const LOG_TOKEN_PAIR_COLUMN_WIDTH = 108;",
        ):
            self.assertIn(marker, self.ui)

    def test_every_log_column_is_narrowed_to_what_it_shows(self) -> None:
        """A declared width is a ceiling; the content decides what it spends.

        The request table declared 96 pt for the token pair, 78 for the relay,
        68 for the status and 62 for the duration, and spent the difference on
        nothing.  On a 1280 pt window that slack is what put the cache share and
        the detail column behind the frame edge, so a reader had to scroll
        sideways to reach a number the row was already reporting.

        The pass is pinned with the two things it must not do: it never widens
        a column past its ceiling, and it leaves the flex column alone, because
        the space it frees is what `fitLogColumns` grows the detail with.
        """

        self.assert_ui_has("function fitLogColumnsToContent(rows: RenderedLogRecord[], columns: LogColumn[]): LogColumn[] {")
        self.assert_ui_has("if (column.flex) return column;")
        self.assert_ui_has("const measured = nativeControlTextWidth(column.value(row));")
        self.assert_ui_has("return needed < column.width ? { ...column, width: needed } : column;")
        # The cell's own padding, plus the margin between an estimate and the
        # host's glyph run; the header is measured heavier because it is drawn
        # semibold and, in English, in upper case.
        self.assert_ui_has("const LOG_CELL_PADDING = 18;")
        self.assert_ui_has("const LOG_PROTOCOL_COLUMN_SIZE = 56;")
        self.assert_ui_has("const needed = Math.max(column.floor ?? 0, Math.ceil(widest + LOG_CELL_PADDING));")
        self.assert_ui_has("const LOG_HEADER_WEIGHT = 1.2;")
        self.assert_ui_has("Math.ceil(logHeaderTextWidth(column.label))")
        # Only an all-caps Latin header runs wider than its estimate; a CJK
        # header estimates level, so weighting it would be pure blank space.
        self.assert_ui_has("const latinOnly = !/[^\\u0000-\\u024f]/u.test(label);")
        self.assert_ui_has("return nativeControlTextWidth(label) * (latinOnly ? LOG_HEADER_WEIGHT : 1);")
        # The pass runs before the flex distribution, so the space it frees is
        # the detail column's.
        self.assert_ui_has("fitLogColumnsToContent(rows, logColumns(")
        self.assert_ui_has("      )),\n      tableWidth,")
        # The two columns that were being clipped by the slack now declare a
        # ceiling that holds their widest cell.
        self.assert_ui_has("const LOG_TOKEN_PAIR_COLUMN_WIDTH = 108;")
        self.assert_ui_has("const LOG_CACHE_COLUMN_WIDTH = 68;")
        # The detail column is declared once, and the flex rule grows it into
        # whatever the measured columns leave.
        self.assert_ui_has("const LOG_DETAIL_COLUMN_WIDTH = 240;")
        self.assert_ui_has("{ ...detail, width: LOG_DETAIL_COLUMN_WIDTH },")

    def test_a_narrow_frame_scrolls_instead_of_hiding_a_column(self) -> None:
        """Every column a row has stays; a frame too narrow scrolls sideways.

        Hiding the relay, the key, or the row's message when the window is
        narrow made the same log read differently at two window sizes, and a
        row that does not name the key it spent is not a row a reader can act
        on.  The columns stay, `scrollTrailingColumnOverflow` puts the scroller
        under them, and `fitLogColumns` returns the set untouched once the
        frame has run out of room.
        """

        # No column is ever dropped or squeezed to fit: the fit hands back the
        # set it was given and the native table scrolls.
        self.assert_ui_not_has("dropPriority")
        self.assert_ui_not_has("shrinkLogColumns")
        self.assert_ui_not_has("isRepeated(")
        self.assert_ui_has("if (pool >= 0) return widened;")
        self.assert_ui_has("// Step two: the frame is narrower than the set needs.")
        self.assert_ui_has("return widened;\n}")
        self.assertIn(
            "columns={nativeTableColumns} rows={nativeTableRows} selectedKey={selectedKey} compact preserveColumnWidths scrollTrailingColumnOverflow",
            self.ui,
        )
        # The relay and the key are unconditional columns of a request row.
        self.assert_ui_has('{ label: translate("common.provider"), width: 78, value: (row) => row.provider },')
        self.assert_ui_has('{ label: translate("logs.apiKeyName"), width: 64, value: (row) => row.apiKeyName },')

    def test_the_cache_column_states_a_rate_with_its_own_unit(self) -> None:
        """`100%` is the whole fact; the count beside it was a second spelling.

        The cell used to read `100% · 646.8` -- a rate and the cached token count
        it was computed from.  The count is a second number for the same fact,
        and under a header that says 缓存命中 a bare `100` would read as a rate
        missing its unit.  A row with no input count has no share to state, and
        states nothing rather than a count that looks like a rate.
        """

        self.assert_ui_has("function formatLogCacheHit(cached: string, input: string): string {")
        self.assert_ui_has("return `${Math.round(share * 100)}%`;")
        self.assert_ui_has("if (!input || !Number.isFinite(inputTokens) || inputTokens <= 0) {")
        self.assert_ui_not_has("`${rate} · ${count}`")
        # With the count gone the header is the widest thing the column holds.
        self.assert_ui_has("const LOG_CACHE_COLUMN_WIDTH = 68;")

    def test_the_relay_log_states_what_a_request_cost(self) -> None:
        """A relay bills a request in fractions of a cent.

        Two decimals — the unit the balance beside it is written in — would
        print every ordinary turn as `$0.00`, so the cell keeps three
        significant digits, states a bound rather than rounding a real charge
        down to nothing, and stays empty when the station stated no money at
        all.  The money is computed by Core (a Sub2API row states it, a New API
        row states credits) and the pane only decides how it reads.
        """

        self.assert_ui_has("function formatLogCost(value: string): string {")
        self.assert_ui_has('if (amount < 0.0001) return "<$0.0001";')
        self.assert_ui_has("const decimals = amount >= 100 ? 0")
        # The cell holds `$0.00439` and `<$0.0001` whole: 60 pt plus the cell's
        # 8 pt padding on each side, and the header is narrower than both.
        self.assert_ui_has("const LOG_COST_COLUMN_WIDTH = 78;")
        self.assertGreaterEqual(78, 60 + 16)
        # The row's own money, not a credit count re-formatted: the pane formats
        # the number of dollars Core stated, in the field the detail reuses.
        self.assert_ui_has("const cost = formatLogCost(compactLogValue(value.cost));")
        self.assert_ui_has('[translate("logs.cost"), cost],')
        self.assert_ui_has("rows.some((row) => Boolean(row.cost)),")
        # The money follows the share that decided it and precedes the duration,
        # so a row reads size, share, price, time.
        columns = self.ui.split('if (tab === "online-usage") return [', 1)[1].split("];", 1)[0]
        self.assertLess(
            columns.index("...(hasCache ? [{ label: translate(\"logs.cacheHit\")"),
            columns.index("...(hasCost ? [{ label: translate(\"logs.cost\")"),
        )
        self.assertLess(
            columns.index("...(hasCost ? [{ label: translate(\"logs.cost\")"),
            columns.index("{ label: translate(\"logs.duration\")"),
        )
        self.assertIn('"logs.cost": "消耗"', self.zh)
        self.assertIn('"logs.cost": "COST"', self.en)

    def test_the_model_columns_hold_a_real_model_name(self) -> None:
        """Both model columns hold `deepseek-v4.1-flash` whole.

        That id measures 123.2 pt in the table's 13 pt face, so 140 is it plus
        the cell's 8 pt padding on each side.  The width is a floor as well as a
        declaration: the estimator is not allowed to size the column below it,
        and a longer id is ellipsized rather than widening every row.
        """

        self.assert_ui_has("const LOG_MODEL_COLUMN_WIDTH = 140;")
        self.assert_ui_has('{ label: translate("logs.clientModel"), width: LOG_MODEL_COLUMN_WIDTH, floor: LOG_MODEL_COLUMN_WIDTH, value: (row) => row.model },')
        self.assert_ui_has('{ label: translate("logs.upstreamModel"), width: LOG_MODEL_COLUMN_WIDTH, floor: LOG_MODEL_COLUMN_WIDTH, value: (row) => row.upstreamModel },')
        # The floor bounds the content pass too, so an estimate that reads the
        # name short cannot narrow the column under it.
        self.assert_ui_has("const needed = Math.max(column.floor ?? 0, Math.ceil(widest + LOG_CELL_PADDING));")
        self.assertGreaterEqual(140, 124 + 16)

    def test_request_log_states_a_turns_two_wires_in_one_cell(self) -> None:
        """One protocol column, read the way the turn travelled.

        The row states which wire the client called in on and which wire the
        attempt left on.  Two columns spent 192 pt to repeat `Messages |
        Messages` in half the rows and left the reader to pair them up; the one
        column states the adaption once, and sits in the same left-to-right
        order as the model pair beside it.

        The cell is abbreviated to one letter per wire (`M→C`) because a
        routing log is scanned, not read: hovering the cell states the pair in
        full.  The header names the pair (协议 / PROTOCOL) rather than an end --
        入站/出站 named a station whose owner the reader had to guess.
        """

        self.assert_ui_has('if (tab === "requests") return [')
        requests = self.ui.split('if (tab === "requests") return [', 1)[1].split(
            'if (tab === "actions")', 1
        )[0]
        protocol = requests.index('translate("logs.protocol")')
        client_model = requests.index('translate("logs.clientModel")')
        upstream_model = requests.index('translate("logs.upstreamModel")')
        self.assertLess(protocol, client_model)
        self.assertLess(client_model, upstream_model)
        # One cell, carrying both ends -- not a column per end.
        self.assertEqual(1, requests.count("LOG_PROTOCOL_COLUMN_WIDTH"))
        self.assert_ui_has('value: (row) => row.protocol,')
        self.assert_ui_has('tooltip: (row) => row.protocolSpelled,')
        # The short form is one letter per wire, and keeps the ends' order.
        self.assert_ui_has('function logProtocolShortLabel(value: unknown): string {')
        self.assert_ui_has('if (normalized.includes("responses")) return "R";')
        self.assert_ui_has('if (normalized.includes("chat")) return "C";')
        self.assert_ui_has('if (normalized.includes("messages") || normalized.includes("anthropic")) return "M";')
        self.assert_ui_has('if (normalized.includes("image")) return "I";')
        self.assert_ui_has('function logProtocolShortPair(client: string, upstream: string): string {')
        self.assert_ui_has('[logProtocolShortLabel(client), logProtocolShortLabel(upstream)].filter(Boolean).join("→");')
        # The long form is what the cell keeps for its tooltip: an unknown end
        # is dropped rather than printed as a bare arrow, and a row with
        # neither end keeps an empty cell.
        self.assert_ui_has('function logProtocolPair(client: string, upstream: string): string {')
        self.assert_ui_has('return [client, upstream].filter(Boolean).join(" → ");')
        # In the Log window a turn's two ends are named once, in these words:
        # the request rows and the recovery rows both read 客户端模型 / 上游模型.
        self.assert_ui_has('{ label: translate("logs.clientModel"), width: 142, value: (row) => row.model },')
        self.assert_ui_has('{ label: translate("logs.upstreamModel"), width: 142, value: (row) => row.upstreamModel },')
        # A protocol is a wire name, never prose; the images wire has a name of
        # its own instead of the generic fallback.
        self.assert_ui_has('if (normalized.includes("image")) return translate("logs.routeTrace.protocolImages");')
        self.assertIn('"logs.protocol": "PROTOCOL"', self.en)
        self.assertIn('"logs.clientModel": "Client model"', self.en)
        self.assertIn('"logs.upstreamModel": "Upstream model"', self.en)
        self.assertIn('"logs.protocol": "协议"', self.zh)
        self.assertIn('"logs.clientModel": "客户端模型"', self.zh)
        self.assertIn('"logs.upstreamModel": "上游模型"', self.zh)
        # The per-end headers are gone, not merely unused: a translation key
        # that no column reads is a name waiting to come back.
        for gone in ("logs.clientProtocol", "logs.upstreamProtocol"):
            self.assertNotIn(gone, self.en)
            self.assertNotIn(gone, self.zh)
            self.assertNotIn(gone, self.ui)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        for key in (
            "logs.protocol",
            "logs.clientModel",
            "logs.upstreamModel",
            "logs.routeTrace.protocolImages",
        ):
            self.assertIn(f'| "{key}"', translation_keys)
        # Core records the two names; the pane only phrases them.
        self.assert_ui_has('logProtocolLabel(value.client_surface, translate),')
        self.assert_ui_has('logProtocolLabel(value.upstream_surface, translate),')

    def test_request_log_states_the_mode_beside_the_wire_pair(self) -> None:
        """A routing row says whether the client asked for a stream.

        A non-streaming request is the one shape a relay's silent-connection
        ceiling kills, and it is also what a client falls back to after a live
        stream fails: live 2026-11-11, Claude Code fell back to non-streaming
        after a mid-stream `upstream_error`, and the log had no column that
        could tell the retry apart from the turn it replaced.  The mode sits
        beside the wire pair and reads the client's own body, not the mode the
        route was dispatched with.
        """

        self.assert_ui_has('if (tab === "requests") return [')
        requests = self.ui.split('if (tab === "requests") return [', 1)[1].split(
            'if (tab === "actions")', 1
        )[0]
        protocol = requests.index('translate("logs.protocol")')
        mode = requests.index('translate("logs.mode")')
        client_model = requests.index('translate("logs.clientModel")')
        self.assertLess(protocol, mode)
        self.assertLess(mode, client_model)
        self.assert_ui_has('value: (row) => row.mode },')
        self.assert_ui_has('const LOG_MODE_COLUMN_WIDTH = 96;')
        self.assert_ui_has('const LOG_MODE_COLUMN_SIZE = 88;')
        # The cell's words are the pane's existing mode vocabulary, not a
        # second spelling of the same two states.
        self.assert_ui_has('translate(value.stream ? "logs.routeTrace.streaming" : "logs.routeTrace.nonStreaming")')
        self.assertIn('"logs.mode": "MODE"', self.en)
        self.assertIn('"logs.mode": "模式"', self.zh)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        self.assertIn('| "logs.mode"', translation_keys)
        # Core records the client's own body flag and keeps it out of the
        # wire-name vocabulary: a row that states nothing stays empty.
        routing_source = (ROOT / "young_router/proxy/routing.py").read_text(encoding="utf-8")
        self.assertIn("def _request_log_streamed(", routing_source)
        self.assertIn('"stream": _request_log_streamed(request_kwargs),', routing_source)

    def test_a_shortened_cell_states_its_long_form_on_hover(self) -> None:
        """A one-letter cell is only honest if hovering restores the wire names.

        The native table already sets every cell's tooltip to its own text, so
        an abbreviated cell would tooltip `M→C` -- the same riddle twice.  The
        tooltip therefore travels as its own per-cell list, alongside `cells`
        and by the same positional rule, and the AppKit side prefers it over
        the cell's text.  The pathway is pinned at every hop: a prop dropped in
        one file would still typecheck, and the cell would simply go quiet.
        """

        mac_native = MACOS_CONTROLS.read_text(encoding="utf-8")
        mac_spec = (ROOT / "rn/packages/shared/src/ui/macos/NativeTableNativeComponent.ts").read_text(encoding="utf-8")
        # One tooltip per cell, declared as the macOS-only prop it is: the
        # Windows table is a React list with no tooltip service to feed.
        self.assertIn("cellTooltips?: ReadonlyArray<string>;", mac_spec)
        spec_source = (ROOT / "rn/packages/shared/src/ui/nativeSpecs/NativeTable.spec.ts").read_text(encoding="utf-8")
        self.assertIn("  // @macos-only\n  cellTooltips?: ReadonlyArray<string>;", spec_source)
        self.assertIn("tooltips?: string[];", self.native_controls)
        self.assertIn("cellTooltips: rows.some((row) => (row.tooltips?.length ?? 0) > 0)", self.native_controls)
        self.assertIn("cellTooltips={cellTooltips}", self.appkit_controls)
        # The AppKit cell prefers the declared tooltip and falls back to its own
        # text, which is what every other column in the app relies on.
        native_table = re.sub(r"\s+", " ", mac_native)
        self.assertIn(
            "NSString *cellTooltip = cellIndex < viewProps.cellTooltips.size() "
            "? StringFromStdString(viewProps.cellTooltips[cellIndex]) : @\"\";",
            native_table,
        )
        self.assertIn("NSString *tooltip = cellTooltip.length > 0 ? cellTooltip : value;", native_table)
        # A tooltip-only change still has to re-tile the rows, or the cell keeps
        # the tooltip it was first drawn with.
        self.assertIn("oldViewProps.cellTooltips != newViewProps.cellTooltips;", native_table)

    def test_the_anthropic_protocol_option_names_its_wire(self) -> None:
        """A protocol option states a wire, and its name is ``Anthropic Messages``.

        ``Anthropic`` named the company; the other two options in the same
        picker name wires (``OpenAI Responses``, ``OpenAI Chat Completions``),
        and a reader choosing a fallback protocol is choosing a wire.
        """

        self.assertIn('"providers.anthropic": "Anthropic Messages"', self.en)
        self.assertIn('"providers.anthropic": "Anthropic Messages"', self.zh)
        self.assert_ui_has('{ value: "anthropic", label: translate("providers.anthropic") },')
        self.assert_ui_has('if (surface === "anthropic") return translate("providers.anthropic");')
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        self.assertIn('| "providers.anthropic"', translation_keys)

    def test_a_stopped_turn_says_who_stopped_it(self) -> None:
        """The reason a turn closed is part of the row, in both languages.

        ``aborted`` used to be written only by the runtime's own window or by a
        restart, so a row a person stopped read like a runtime failure.  The
        client-facing generator now closes its own row and states
        ``client_disconnect``; the pane has to name that case instead of
        falling through to the raw reason.
        """

        self.assert_ui_has('case "client_disconnect": return translate("logs.aborted.reasonClientDisconnect");')
        self.assertIn('"logs.aborted.reasonClientDisconnect": "Stopped in the client"', self.en)
        self.assertIn('"logs.aborted.reasonClientDisconnect": "客户端已停止"', self.zh)
        translation_keys = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        self.assertIn('| "logs.aborted.reasonClientDisconnect"', translation_keys)

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
            "const LOG_TIME_COLUMN_WIDTH = 120",
            "const time = { label: translate(\"logs.localTime\"), width: LOG_TIME_COLUMN_WIDTH,",
            "const LOG_PROTOCOL_COLUMN_WIDTH = 90",
            'value: (row) => row.protocol,',
            'tooltip: (row) => row.protocolSpelled,',
            "const LOG_PROTOCOL_COLUMN_WIDTH = 90",
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
            "const rows = useMemo(",
            "() => renderLogRecords(clearing ? [] : (active?.records ?? []), selected, translate)",
            "const active = activeState?.tab === selected ? activeState.log : undefined;",
            "const nativeTableColumns = useMemo(",
            "const nativeTableRows = useMemo(",
            '() => selected === "route-trace" ? [] : rows.map((row)',
            "selectedKey={selectedKey}",
            "logTableFrame:",
            "logFilterRow: { flex: 1, minWidth: Platform.OS === \"web\" ? 180 : 50",
            "logToolbarSpacer: { flex: 1, minWidth: 4",
            "logActionsRow: { height: 24, flexDirection: \"row\"",
            "onlineUsageSourcePicker: { width: 176, minWidth: 120",
            # The pager is a row of its own under the table, never an overlay
            # the native table paints over.
            "usageStatusBar: { minHeight: 24, flexShrink: 0",
            "logFilterRowOnlineUsage: { flex: 1, minWidth: 50",
            "logFilterInput: { flex: 1, minWidth:",
            "logTable: { flex: 1, minHeight: 0",
            # One permanent bottom status strip: the line count and the pause
            # state are its idle text, not a second bar under it.
            "const statusLine = [",
            "useEffect(() => { onStatus?.(statusLine); }, [onStatus, statusLine]);",
            "logsToolbar: { width: \"100%\", minWidth: 0, minHeight: 24, height: Platform.OS === \"web\" ? undefined : 24",
            "logsTabs: { width: undefined, minWidth: 389, height: 24 }",
            "logRangeMessageSlot: { width: \"100%\", minWidth: 0, height: 16",
        ):
            self.assert_ui_has(marker)
        self.assertLess(self.ui.index("<WindowTabs nativeRef={tabsRef}"), self.ui.index("<View style={styles.logsToolbar}>"))
        self.assertGreater(self.ui.index("<View style={styles.logsToolbar}>"), self.ui.index("style={styles.logsTabs}"))
        self.assertIn('logsWindowMacOS: { paddingTop: 0, paddingHorizontal: 10 }', self.ui)
        self.assertLess(self.ui.index("<View style={styles.logsToolbar}>"), self.ui.index("<View style={styles.logRangeMessageSlot}>"))
        self.assertLess(self.ui.index("<View style={styles.logRangeMessageSlot}>"), self.ui.index("style={styles.logTableFrame}"))
        self.assertNotIn("NativeLogFrame", self.ui)
        native_controls = (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(encoding="utf-8")
        self.assertIn("[_control setWidth:0 forSegment:index];", native_controls)
        controls = (ROOT / "rn/packages/shared/src/ui/NativeControls.tsx").read_text(encoding="utf-8")
        self.assertIn("compact && style === undefined ? { minWidth: pickerLabelWidth", controls)
        self.assertIn("const pickerLabelWidth =", controls)
        # The tab strip sits directly under the window title: a 36 pt top inset
        # here left a bare band above 请求 that no other pane has.
        self.assertIn('logsContent: { paddingHorizontal: 12, paddingTop: 12', self.ui)

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
            ("logs.routeTrace.reasonGatewayAuth", "本机网关拒绝了调用方的密钥", "The local gateway rejected the caller key"),
            ("logs.gatewayAuthRejected", "本机网关拒绝密钥", "Local gateway rejected the key"),
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
            '"no-available-deployment": "logs.noAvailableRoute",',
            '"model-not-configured": "logs.modelNotConfigured",',
            '"local-gateway-auth-rejected": "logs.routeTrace.reasonGatewayAuth",',
            'routingState === "gateway_auth_rejected"',
            'translate("logs.gatewayAuthRejected")',
        ):
            self.assert_ui_has(marker)

    def test_logs_always_show_client_and_upstream_model_columns(self) -> None:
        # The two model columns are never dropped when a page happens to hold
        # one model: they are the row's identity.  In this window they name the
        # turn's two ends -- 客户端模型 / 上游模型 -- so a reader never has to
        # translate the providers pane's configuration vocabulary into a
        # request's two ends, and 入站/出站 no longer asks whose station it was.
        for marker in (
            "const publicModel = compactLogValue(value.public_model ?? value.model_group ?? value.model);",
            "upstreamModel: compactUpstreamLogModel(upstreamModel)",
            '{ label: translate("logs.clientModel"), width: 142, value: (row) => row.model }',
            '{ label: translate("logs.upstreamModel"), width: 142, value: (row) => row.upstreamModel }',
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

    def test_route_trace_greens_only_the_route_that_actually_served(self) -> None:
        """A picked route is a plan; only a served route earns the success ink.

        ``selected_deployment`` is emitted when the router picks a route,
        before the request has run, so a selection that later failed and a
        selection that succeeded were drawn identically. The trace view has to
        learn the outcome from the success event, keep a later failure winning
        over an earlier success, and not let a bare re-selection withdraw an
        answer the request already gave.
        """

        for marker in (
            'state: "succeeded" | "selected" | "failed" | "attempted";',
            ': rawEvent === "deployment_success"',
            '? "succeeded"',
            "if (state === \"succeeded\") return translate(\"logs.success\");",
            'if (state === "succeeded") return "✓";',
            "const lastServed = attempts.map((attempt) => attempt.state).lastIndexOf(\"succeeded\");",
            "!(existing.state === \"succeeded\" && attempt.state === \"selected\")",
            "const ROUTE_TRACE_ATTEMPT_TONES: Record<",
            'deployment_success: "logs.routeEvent.deploymentSuccess",',
            'yellow: semanticColor("systemYellowColor", undefined, "#7a5c00"),',
        ):
            self.assert_ui_has(marker)
        # The four tones each answer to their own state.
        for marker in (
            "node: styles.routeTraceTimelineNodeSucceeded,",
            "node: styles.routeTraceTimelineNodeSelected,",
            "node: styles.routeTraceTimelineNodeFailed,",
            "node: styles.routeTraceTimelineNodeAttempted,",
            "text: styles.routeTraceStepStateSucceeded,",
            "text: styles.routeTraceStepStateSelected,",
        ):
            self.assert_ui_has(marker)
        self.assertIn('"logs.routeEvent.deploymentSuccess": "该路由应答成功"', self.zh)
        self.assertIn('"logs.routeEvent.deploymentSuccess": "Route served the request"', self.en)

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
        self.assertIn("const refreshAccountResources = async (target: ResourceRefreshTarget, options: { silent?: boolean; force?: boolean } = {})", relay)
        self.assertIn("const status = await refreshResources(target.id, { force });", relay)
        # A read is forced only where stale facts would be wrong (a sign-in that
        # just landed); the pane's mount read stays implicit so 分组管理 can join
        # it instead of paying for the same station round trip twice.
        self.assertIn("await refreshAccountResources({ id: account.id }, { silent: true, force: true });", relay)
        # A ready key read is the later proof of the same session the mount
        # probe may have failed to renew: it clears 未登录 and keeps 已登录.
        self.assertIn('if (status === "ready") {', relay)
        self.assertIn("markLoginFailure(target.id, false);", relay)
        self.assertIn("markLocalSignedIn(target.id, true);", relay)
        # Removal asks before it removes, on the host's own decision panel, and
        # stages the dependency policy before the native erase; a failed erase
        # persists a retry tombstone.
        removal = relay.split("const removeSelected = async (): Promise<void> => {", 1)[1].split("const setStationDraftValue", 1)[0]
        self.assertIn("await native.showConfirmation({", removal)
        self.assertLess(removal.index("await native.showConfirmation({"), removal.index('await commit("account.delete"'))
        self.assertIn('await commit("account.delete", { id: account.id, dependency_policy: "detach" });', removal)
        self.assertIn("await native.clearRelayCredentials(account.id);", removal)
        self.assertIn('await commit("credential_cleanup_confirm", { id: account.id, kind: "credentials" });', removal)
        # Quietly restore sessions once per account when the panel mounts; a
        # restore that fails leaves the account signed out and never opens the
        # login page on its own (去登录 / + ask for it).  The quiet resource
        # refresh still runs: a locally known key is local data, and Core reads
        # the station through a remembered session when it has one.  The probe
        # is a station round trip, so it reports in the resources lane: every
        # mount re-checks a saved session, and re-entering the pane must not
        # paint a verified account as 登录中 while that check runs.
        self.assertIn("const attemptedAccounts = useRef(new Set<string>());", relay)
        self.assertIn("attemptedAccounts.current.add(account.id);", relay)
        self.assertNotIn("if (!(await restoreSavedSession(account))) return;", relay)
        self.assertIn("await restoreSavedSession(account);", relay)
        self.assertIn("await refreshAccountResources(account, { silent: true });", relay)
        self.assertNotIn("const canAutoLogin =", relay)
        # ...and a login the runtime's re-check window still covers skips the
        # probe entirely: entering the pane twice within the window must not
        # re-authenticate or re-read a station whose observation is seconds old.
        # The short-circuit covers a determined 未登录 as well as 已登录 — a login
        # that has been answered is settled, not a question to ask again.  The
        # word alone is not the test, the observation is: an account created but
        # never checked carries 未登录 with no stamp, and that is exactly what a
        # mount must probe.  An expired login is left out on purpose, because
        # re-authenticating is how such an account recovers.
        self.assertIn("const observed = account.loginObservedSecondsAgo;", relay)
        self.assertIn('const settled = (account.loginStatus === "signed_in" || account.loginStatus === "signed_out")', relay)
        self.assertIn("&& observed !== null;", relay)
        self.assertIn("&& observed < sessionRecheckSeconds;", relay)
        self.assertNotIn('const fresh = account.loginStatus === "signed_in"', relay)
        # The probe runs for a session the window no longer covers, and its
        # answer is what decides whether the account is released back to
        # unattempted: a refused round trip observed nothing, so leaving the id
        # attempted suppressed the mount check for the life of the panel.
        self.assertIn("if (!fresh && active) answered = await restoreSavedSession(account);", relay)
        self.assertIn("if (!answered) attemptedAccounts.current.delete(account.id);", relay)
        self.assertIn("let active = true;", relay)
        self.assertIn("return () => { active = false; };", relay)
        self.assertIn("sessionRecheckSeconds?: number;", relay)
        self.assertIn("export function relaySessionRecheckSeconds(snapshot?: CoreSnapshot): number | undefined {", relay)
        self.assertIn("sessionRecheckSeconds={relaySessionRecheckSeconds(snapshotForCleanups)}", self.ui)
        runtime_schema = (ROOT / "young_router/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        self.assertIn("YOUNG_ROUTER_RELAY_SESSION_RECHECK_SECONDS", runtime_schema)
        self.assertIn("DEFAULT_SESSION_RECHECK_SECONDS = 60.0", (ROOT / "young_router/core/domains/relay_accounts.py").read_text(encoding="utf-8"))
        # Station connection details stay editable through staged updates.
        self.assertIn("const stageStationUpdate = async (overrides: StationDraft = {}): Promise<void> => {", relay)
        self.assertIn('await commit("station.update", { id: station.id, name, origin, type });', relay)
        self.assertIn("translate(\"relay.stationUpdateStaged\")", relay)
        # One login state drives the status column and the account actions:
        # 登录中 while a sign-in this pane started is still being settled,
        # 检测中 while the mount check is still fetching its answer, otherwise
        # 已登录 / 未登录 (the expired and answered-signed-out cases read as 未登录
        # and keep the brown alert row).  The two waits are not the same
        # statement: a sign-in asks the station to mint a session, while a check
        # only asks what the session on hand is worth. 去登录 only appears while
        # signed out and is never disabled; 刷新资源 and 分组管理 are the controls
        # that act on a session, so they appear only once the row reads 已登录.
        self.assertIn('const relayLoginState = (account: RelayAccount): "signed_in" | "checking_login" | "signing_in" | "signed_out" => {', relay)
        self.assertIn('if (loading[account.id]?.session) return "signing_in";', relay)
        self.assertIn('if (effectiveLoginStatus(account) === "signed_in") return "signed_in";', relay)
        self.assertIn('if (loading[account.id]?.checking) return "checking_login";', relay)
        self.assertIn('return "signed_out";', relay)
        # 未登录 is a conclusion, not a placeholder: the mount check is a station
        # round trip whose network timeout is long enough that a row painted
        # 未登录 while it ran read as "sign in again".  It therefore reports in
        # its own lane, which the login column reads, while the key-list read
        # stays in the resources lane — a wait that says nothing about the login
        # must not repaint it.  The check is deliberately not the session lane:
        # that lane is a sign-in window the host holds over this one, and 去登录
        # must be able to interrupt the check rather than sit it out.
        self.assertIn('updateLoading(account.id, "checking", true);', relay)
        self.assertIn('updateLoading(account.id, "checking", false);', relay)
        probe_lanes = relay.split("const restoreSavedSession = async (account: RelayAccount): Promise<boolean> => {", 1)[1].split("const loginAccount", 1)[0]
        self.assertNotIn('"session"', probe_lanes)
        self.assertNotIn('updateLoading(account.id, "resources"', probe_lanes)
        self.assertIn('type AccountLoading = { session: boolean; checking: boolean; resources: boolean };', relay)
        self.assertIn('const previous = current[accountID] ?? { session: false, checking: false, resources: false };', relay)
        self.assertIn('if (!merged.session && !merged.checking && !merged.resources) delete next[accountID];', relay)
        self.assertIn('return Boolean(state?.session || state?.checking || state?.resources);', relay)
        # The lane is opened on the way *in* to a sign-in and released only with
        # the refresh that follows the answer, never in a `finally` that a
        # verified sign-in could reach while its own repaint was still pending:
        # a lane cleared there left the row reading 未登录 with 去登录 beside it
        # for the whole of the commit.  The account that owns the wait is kept
        # by id so a failure or a cancellation releases it too.
        self.assertIn('const pendingLoginRef = useRef<string | undefined>(undefined);', relay)
        self.assertIn('const startSigningIn = (accountID: string): void => {', relay)
        self.assertIn('pendingLoginRef.current = accountID;', relay)
        self.assertIn('updateLoading(accountID, "session", true);', relay)
        self.assertIn('const clearPendingLogin = (): void => {', relay)
        self.assertIn('updateLoading(pending, "session", false);', relay)
        self.assertIn('if (pendingLoginRef.current === account.id) clearPendingLogin();', relay)
        self.assertIn('startSigningIn(account.id);', relay)
        self.assertNotIn('updateLoading(account.id, "session", false);', relay)
        self.assertIn('translate(`relay.status.${relayLoginState(account)}`)', relay)
        # 去登录 is the escape hatch from the wait itself: a row that states
        # 未登录 carries it, so the user can skip the station round trip the
        # mount check would otherwise make them sit through.  It is withheld
        # while the row already reads 登录中 — the pane's own sign-in, or the
        # mount check still fetching its answer — because a login already being
        # settled has no second sign-in to offer, and a button beside that word
        # read as "your attempt did not start".  A sign-in that lands while a
        # check was in flight stays authoritative: the check's answer is about
        # the session the sign-in replaced, so it is discarded by the pane's
        # sign-in count.
        self.assertIn('const signInCounts = useRef<Record<string, number>>({});', relay)
        self.assertIn('signInCounts.current[accountID] = (signInCounts.current[accountID] ?? 0) + 1;', relay)
        self.assertIn('const checkEpoch = signInCounts.current[account.id] ?? 0;', relay)
        self.assertIn('if ((signInCounts.current[account.id] ?? 0) !== checkEpoch) return false;', relay)
        self.assertIn('const selectedLoginState = selected ? relayLoginState(selected) : "signed_out";', relay)
        self.assertIn('{selectedLoginState === "signed_out" ? <NativeButton title={translate("relay.goLogin")} compact busy={pendingAction === "login"} disabled={controlsBusy && pendingAction !== "login"} onPress={() => { void loginSelected(); }} /> : null}', relay)
        # 登录中 owns no sign-in button at all, whether the wait is the pane's
        # own session lane or the mount check's.
        self.assertNotIn('selectedLoginState !== "signed_in" && !loading[selected.id]?.session ? <NativeButton title={translate("relay.goLogin")}', relay)
        # 刷新资源 and 分组管理 both act on a station session: 刷新资源 is a
        # forced station round trip and 分组管理 writes the station's keys, so
        # neither is offered while the account is not signed in — pressing either
        # one signed out only ever produced a login failure.  The comments the
        # relay messages carry ("请点击刷新资源") therefore point at a button the
        # row shows once there is a session to refresh.
        self.assertIn('{selectedLoginState === "signed_in" ? <NativeButton title={translate("relay.refreshResources")} symbol="refresh" compact busy={pendingAction === "refresh"} disabled={controlsBusy && pendingAction !== "refresh"} onPress={() => { void refreshSelected(); }} /> : null}', relay)
        self.assertIn('{selectedLoginState === "signed_in" ? <NativeButton title={translate("relay.groupManager")} titleWidth="tight" compact toolTip={native.showGroupManager ? undefined : translate("relay.groupManagerUnavailable")} disabled={controlsBusy || !native.showGroupManager} onPress={() => { void openGroupManager(); }} /> : null}', relay)
        self.assertIn('title={translate("relay.refreshResources")} symbol="refresh" compact busy={pendingAction === "refresh"}', relay)
        self.assertIn('const refreshSelected = async (): Promise<void> => {', relay)
        self.assertIn('await refreshAccountResources({ id: account.id }, { force: true });', relay)
        # 刷新 states the control that used to be 刷新资源; its own message
        # 登录成功，但无法加载 API 资源… still names it the same way.
        for locale, copy in ((self.zh, '刷新'), (self.en, 'Refresh Resources')):
            self.assertIn(f'"relay.refreshResources": "{copy}"', locale)
        # 去登录 opens the login page at once: the silent session probe must not
        # delay the sheet the user explicitly asked for.
        login_selected = relay.split("const loginSelected = async (): Promise<void> => {", 1)[1].split("const removeSelected", 1)[0]
        self.assertIn("await runPendingAction(\"login\", () => loginAccount(account));", login_selected)
        self.assertNotIn("restoreSavedSession(selected)", login_selected)
        self.assertIn('disabled={controlsBusy || !native.showGroupManager}', relay)
        self.assertNotIn('!native.showGroupManager || selectedLoginState !== "signed_in"', relay)
        self.assertNotIn('disabled={controlsBusy || isAccountLoading(selected.id)}', relay)
        # An unreachable station is not an answer about the login.  The host
        # answers a probe with nothing at all when it could not reach the
        # station, and the pane keeps what it already knew instead of painting
        # 未登录 and retiring a session the user still holds.
        probe = relay.split("const restoreSavedSession = async (account: RelayAccount): Promise<boolean> => {", 1)[1].split("const loginAccount", 1)[0]
        self.assertIn("if (!result) return false;", probe)
        self.assertIn('const signedIn = result.loginStatus === "signed_in";', probe)
        self.assertNotIn('const signedIn = result?.loginStatus === "signed_in";', relay)
        self.assertNotIn("markLocalSignedIn(account.id, false);\n      return false;", probe)
        self.assertIn('"relay.status.signing_in": "登录中"', self.zh)
        self.assertIn('"relay.status.signing_in": "Signing in"', self.en)
        # The mount check is not a sign-in, so the row says 检测中 while it runs
        # and 登录中 only while a sign-in this pane started is settling.
        self.assertIn('"relay.status.checking_login": "检测中"', self.zh)
        self.assertIn('"relay.status.checking_login": "Checking"', self.en)
        # The relay family has no manual select: it comes from the station
        # type or auto-detection, and grouping lives in the manager dialog.
        self.assertNotIn('translate("relay.type")', relay)
        self.assertIn("detectType?: (origin: string) => Promise<RelayType | undefined>;", relay)
        self.assertIn("station.type ?? await detectType?.(station.origin)", relay)
        # 分组管理 stays the provider window's native subordinate sheet, but
        # it carries the pre-refactor keys list: every key is shown with its
        # group and the sheet drafts create / re-group / delete edits.
        self.assertIn("const openGroupManager = async (): Promise<void> => {", relay)
        self.assertIn("const shown = native.showGroupManager({ ...groupManagerRequest(current), loading: Boolean(pending) });", relay)
        self.assertIn("apiKeyActions?.create?.(account.id, { name: create.name, groupID: create.groupID, enabled: true });", relay)
        self.assertIn("apiKeyActions?.setGroup?.(account.id, edit.keyID, edit.groupID)", relay)
        self.assertIn("apiKeyActions?.setEnabled?.(account.id, edit.keyID, edit.enabled)", relay)
        self.assertIn("apiKeyActions?.update?.(account.id, edit.keyID, edit.name)", relay)
        self.assertIn('apiKeyActions?.remove?.(account.id, keyID, "delete_models");', relay)
        self.assertIn("await apiKeyActions?.setAutoGrouping?.(account.id, false);", relay)
        # Turning 自动分组 off restores Core's pre-toggle draft, which can name
        # a different resource set than the rows on screen.  The pane reads the
        # account again before matching edits, and a row that is no longer a
        # key this account holds refuses the save instead of being dropped.
        self.assertIn("await refreshResources(current.id, { force: true });", relay)
        self.assertIn("const reloaded = refreshed\n            ? accountsFromSnapshot(refreshed).find((entry) => entry.id === current.id)", relay)
        self.assertIn("throw new Error(`The relay API key ${edit.keyID} is no longer available`);", relay)
        self.assertNotIn('onStatus?.(translate("relay.apiKeyGroupStaged"));', relay)
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        native_bridge = (ROOT / "rn/packages/shared/src/platform/nativeBridge.ts").read_text(encoding="utf-8")
        platform_entry = (ROOT / "rn/packages/shared/src/platformEntry.ts").read_text(encoding="utf-8")
        mac_module = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeafModule.swift").read_text(encoding="utf-8")
        mac_bridge = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeafBridge.m").read_text(encoding="utf-8")
        mac_leaf = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows_module = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeafModule.cpp").read_text(encoding="utf-8")
        windows_module_header = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeafModule.h").read_text(encoding="utf-8")
        windows_leaf = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        self.assertIn("showGroupManager?(options: {", types)
        self.assertIn("export type RelayGroupManagerResult = {", types)
        self.assertIn("showGroupManager?(options: {", native_bridge)
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
        # Neither host draws a status bar inside the sheet.
        self.assertNotIn("status_strip", windows_leaf)
        self.assertNotIn("status_row", windows_leaf)
        self.assertIn("list_frame.Child(list_body);", windows_leaf)
        # The footer saves and closes, and its button stays disabled until the
        # draft would change something: no no-op verification round-trip.
        self.assertIn('applyLabel: translate("status.saveAndClose")', relay)
        self.assertIn('"status.saveAndClose": "保存并关闭"', self.zh)
        self.assertIn('"status.saveAndClose": "Save and Close"', self.en)
        self.assertIn("applyButton.isEnabled = false", mac_leaf)
        self.assertIn("private func refreshApplyButton() {", mac_leaf)
        self.assertIn("guard applied else { return nil }", mac_leaf)
        self.assertIn("guard hasStagedChanges, !saving else { return }", mac_leaf)
        self.assertIn("apply.IsEnabled(false);", windows_leaf)
        self.assertIn("auto refresh_apply = ", windows_leaf)
        self.assertIn("if (!*applied || !has_staged_changes()) return;", windows_leaf)
        # The list reports the store as it stands: the 分组 column names the
        # group and 倍率 carries the rate, on both hosts.  倍率 never carries
        # status text, so a row the draft will create or delete is dimmed.
        self.assertIn("private func presentation(for row: KeyRow) -> String {", mac_leaf)
        self.assertIn("private func presentation(for row: KeyRow) -> String {\n        multiplierText(for: row)\n    }", mac_leaf)
        self.assertIn("private func isStaged(_ row: KeyRow) -> Bool {", mac_leaf)
        # 自动分组 checked is a preview, not a write: the list is Core's own
        # alignment applied to the rows on screen, the groups with no key become
        # draft rows, and a row the layout is replacing is dropped from the list
        # entirely.  Nothing it produces is handed back as a *deletion*, because
        # the user never saw those rows to agree to them — saving the switch is
        # what lets Core's own alignment retire the extra keys.
        self.assertIn("private func stageAutoGroupingLayout(_ enabled: Bool) {", mac_leaf)
        self.assertIn("private var autoGroupingBaselineKeys: Set<String> = []", mac_leaf)
        self.assertIn("guard !autoGrouping || !previewed.contains(row.id) else { return false }", mac_leaf)
        self.assertIn("return !autoGrouping || previewed.contains(row.id)", mac_leaf)
        self.assertIn("if autoGroupingOn && autoGroupingBaselineKeys.contains(row.id) {", mac_leaf)
        self.assertIn("auto stage_auto_grouping = [&rows, &groups, staged_deletes, auto_grouping_baseline](bool enabled) {", windows_leaf)
        self.assertIn("const bool previewing = *auto_grouping_baseline && result.auto_grouping;", windows_leaf)
        self.assertIn("if (!previewing) result.deletes.push_back(row.id);", windows_leaf)
        self.assertIn("if (*auto_grouping_baseline && toggle_on()) {", windows_leaf)
        self.assertIn("text?.textColor = isStaged(entry) ? .secondaryLabelColor : .labelColor", mac_leaf)
        self.assertIn("auto presentation = ", windows_leaf)
        self.assertIn("-> std::wstring {\n    return multiplier_text(row, labels);", windows_leaf)
        self.assertIn("auto is_staged = [](SheetRow const& row) { return row.deleted || (row.draft && !row.preview); };", windows_leaf)
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
        # A copy states its own result on the window's one status line.
        self.assertIn("func showTransientStatus(_ message: String, clearAfter seconds: Double = 4) {", mac_leaf)
        self.assertNotIn("copyStatusField", mac_leaf)
        self.assertNotIn("controls::TextBlock copy_status;", windows_leaf)
        self.assertIn("controls::TextBlock footer_status;", windows_leaf)
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
        self.assertIn("modelsList.bottomAnchor.constraint(lessThanOrEqualTo: toggle.topAnchor, constant: -14),", mac_leaf)
        # Close drops the draft, so a sheet that would lose edits confirms.
        self.assertIn('discardTitle: translate("relay.groupManagerDiscardTitle"),', relay)
        self.assertIn('discardBody: translate("relay.groupManagerDiscardBody"),', relay)
        self.assertIn('discardConfirm: translate("common.discard"),', relay)
        self.assertIn('"relay.groupManagerDiscardTitle": "放弃未保存的更改？"', self.zh)
        self.assertIn('"relay.groupManagerDiscardBody": "关闭分组管理会丢弃尚未保存的密钥更改。"', self.zh)
        # 分组 is the pane's own label; the sheet keeps its full name where the
        # sentence needs it.
        self.assertIn('"relay.groupManager": "分组"', self.zh)
        self.assertIn('"relay.groupManager": "Group Manager"', self.en)
        # Close drops the draft, so a sheet that would lose edits asks through the
        # app's own decision panel — one surface, one destructive answer — and the
        # answer decides whether the draft goes away.
        self.assertIn("func askToDiscardStagedChanges(completion: @escaping (Bool) -> Void) {", mac_leaf)
        self.assertIn("AppKitNativeLeaf.shared.presentDecisionPanel(", mac_leaf)
        self.assertIn("func closeGroupManager(applied: Bool) {", mac_leaf)
        # Every dismissal path ends the window through that one call: the footer's
        # Close, the title-bar close button, and the workspace window going away.
        # Save and Close hands the draft over instead (the child states the
        # outcome in its own status bar) and ends the window through the answer
        # that reports the write landed.
        self.assertIn("AppKitNativeLeaf.shared.closeGroupManager(applied: false)", mac_leaf)
        self.assertIn("AppKitNativeLeaf.shared.handGroupManagerApply()", mac_leaf)
        self.assertIn("controller.markApplied(true)\n        finishGroupManager()", mac_leaf)
        self.assertIn("closeGroupManager(applied: false)\n            return false", mac_leaf)
        self.assertNotIn("AppKitNativeLeaf.shared.endChildPanel(panel)", mac_leaf.split("@objc private func closePanel", 1)[1].split("func resultOnEnd()", 1)[0])
        self.assertIn("if (has_staged_changes() && !Confirm(labels.discard_title, labels.discard_body, labels.discard_confirm, {}, /*destructive=*/true)) return;", windows_leaf)
        self.assertIn('labels.discard_title = read("discardTitle");', windows_module)
        self.assertNotIn("hint.Text(winrt::hstring(labels.hint));", windows_leaf)
        # The sheet opens on the account facts the provider window already
        # holds and loads the rest in place: it appears at once behind its
        # loading line, and the aligned draft arrives through the native update
        # instead of holding the window closed for the station round trip.  A
        # host without that update keeps the pre-load ordering.  The read runs
        # whenever 自动分组 is on, not only when the pane holds no list: a
        # checked switch is Core keeping one key per group, and a station
        # rename is exactly the stale list a user opens this window to see.
        self.assertIn("updateGroupManager?(options: RelayGroupManagerSnapshot): Promise<boolean>;", types)
        self.assertIn("export type RelayGroupManagerSnapshot = {", types)
        self.assertIn("updateGroupManager?(options: RelayGroupManagerSnapshot): Promise<boolean>;", native_bridge)
        self.assertIn("updateGroupManager?: (options: RelayGroupManagerSnapshot) => Promise<boolean>;", platform_entry)
        self.assertIn("updateGroupManager: leaf.updateGroupManager", platform_entry)
        self.assertIn("loadingLabel: translate(\"relay.groupManagerLoading\"),", relay)
        self.assertIn('"relay.groupManagerLoading": "正在读取中转站…"', self.zh)
        self.assertIn('"relay.groupManagerLoading": "Reading the station…"', self.en)
        self.assertIn("const shouldRead = !groupManagerHasUsableFacts(account) || Boolean(account.autoGrouping && alignAutoGroupingAction);", relay)
        self.assertIn("const pending = update && shouldRead ? loadGroupManagerAccount(account) : undefined;", relay)
        # Opening the window is where a checked switch is made true: the read
        # asks the station for the account's keys and then asks Core to align
        # to them, so the rows and the switch can never disagree.
        self.assertIn("const align = current.autoGrouping ? alignAutoGroupingAction : undefined;", relay)
        self.assertIn("const readsStation = !groupManagerHasUsableFacts(current) || Boolean(align);", relay)
        self.assertIn("if (align) {", relay)
        self.assertIn("await align(current.id);", relay)
        self.assertIn("await applyStagedQuietly();", relay)
        self.assertIn("const shown = native.showGroupManager({ ...groupManagerRequest(current), loading: Boolean(pending) });", relay)
        self.assertIn("if (!update) current = await loadGroupManagerAccount(account);", relay)
        # The update carries the sheet's content alone, which is exactly the
        # field set the native side accepts for it.
        self.assertIn("void push(groupManagerSnapshot(loaded)).catch(() => {", relay)
        self.assertIn("const groupManagerSnapshot = (current: RelayAccount) => {", relay)
        self.assertIn('guard Set(options.keys).isSubset(of: ["accountLabel", "groups", "keys", "autoGrouping"]),', mac_module)
        self.assertIn("guard let controller = groupManagerController, groupManagerPanel != nil else { return false }", mac_leaf)
        self.assertIn("@objc(updateGroupManager:resolver:rejecter:)", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(updateGroupManager:(NSDictionary *)options", mac_bridge)
        self.assertIn("func updateGroupManager(accountLabel: String, groups: [[String: String]], keys: [[String: String]], autoGrouping: Bool) -> Bool {", mac_leaf)
        self.assertIn("controller.applyData(", mac_leaf)
        self.assertIn("func applyData(accountLabel: String, groups: [GroupOption], rows: [KeyRow], autoGrouping: Bool) {", mac_leaf)
        self.assertIn("private func updateLoadingChrome() {", mac_leaf)
        self.assertIn("applyButton?.isEnabled = !loading && !saving && hasStagedChanges", mac_leaf)
        self.assertIn("!loading && !saving && toggle?.state == .off", mac_leaf)
        # The sheet states its own result beside its footer buttons: one line
        # between the switch and the buttons, which keep their trailing edge,
        # so a message never moves them.
        self.assertIn("let footerStatus = NSTextField(labelWithString: \"\")", mac_leaf)
        # The 自动分组 switch carries a question mark that states what it does,
        # so the result line starts after that mark instead of at the switch.
        self.assertIn("let helpButton = NSButton(title: \"\", target: self, action: #selector(showAutoGroupingHelp(_:)))", mac_leaf)
        self.assertIn('helpButton.image = NSImage(systemSymbolName: "questionmark.circle", accessibilityDescription: label("autoGroupingLabel"))', mac_leaf)
        self.assertIn("helpButton.leadingAnchor.constraint(equalTo: toggle.trailingAnchor, constant: 4)", mac_leaf)
        self.assertIn("footerStatus.leadingAnchor.constraint(greaterThanOrEqualTo: helpButton.trailingAnchor, constant: 12)", mac_leaf)
        self.assertIn("@objc private func showAutoGroupingHelp(_ sender: NSButton) {", mac_leaf)
        # An explanation is a hint, not a question: the mark opens the app's own
        # anchored tip beside itself rather than an alert the user must answer.
        self.assertIn("func presentTip(_ text: String, beside anchor: NSView?) -> NSPanel? {", mac_leaf)
        self.assertIn("func dismissTip() {", mac_leaf)
        self.assertIn("AppKitNativeLeaf.shared.presentTip(text, beside: sender)", mac_leaf)
        # The mark opens a hint, never a question: the handler must not raise the
        # app's decision panel for a sentence the user only has to read.
        help_handler = mac_leaf.split("@objc private func showAutoGroupingHelp(_ sender: NSButton) {", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("presentDecisionPanel", help_handler)
        self.assertIn("presentTip", help_handler)
        self.assertIn("footerStatus.trailingAnchor.constraint(lessThanOrEqualTo: closeButton.leadingAnchor, constant: -8)", mac_leaf)
        self.assertIn("footerStatus.centerYAnchor.constraint(equalTo: toggle.centerYAnchor)", mac_leaf)
        self.assertIn("func setStatus(_ text: String) {", mac_leaf)
        self.assertIn("func setSaving(_ value: Bool) {", mac_leaf)
        self.assertIn('AppKitNativeLeaf.shared.localizedText("childSaving", fallback: "Saving…")', mac_leaf)
        # A copy that worked says nothing; only a failure takes that line.
        self.assertNotIn('showCopyStatus(copied ? label("copiedLabel")', mac_leaf)
        self.assertIn('if (!copied) { showTransientStatus(label("failedLabel")) }', mac_leaf)
        # The apply-answer pair is exported for the shared UI on both hosts.
        self.assertIn("@objc(awaitGroupManagerApply:rejecter:)", mac_module)
        self.assertIn("@objc(finishGroupManagerApply:resolver:rejecter:)", mac_module)
        self.assertIn("RCT_EXTERN_METHOD(awaitGroupManagerApply:(RCTPromiseResolveBlock)resolve", mac_bridge)
        self.assertIn("RCT_EXTERN_METHOD(finishGroupManagerApply:(NSDictionary *)options resolver:(RCTPromiseResolveBlock)resolve", mac_bridge)
        self.assertIn("func handGroupManagerApply() {", mac_leaf)
        self.assertIn("func awaitGroupManagerApply(completion: @escaping (NativeGroupManagerResult?) -> Void) {", mac_leaf)
        self.assertIn("func finishGroupManagerApply(status: String, close: Bool) {", mac_leaf)
        self.assertIn("func stagedResult() -> NativeGroupManagerResult? {", mac_leaf)
        self.assertIn("awaitGroupManagerApply?(): Promise<RelayGroupManagerResult | undefined>;", types)
        self.assertIn("finishGroupManagerApply?(options: { status: string; close: boolean }): Promise<void>;", types)
        self.assertIn("awaitGroupManagerApply?(): Promise<RelayGroupManagerResult | undefined>;", native_bridge)
        self.assertIn("finishGroupManagerApply?(options: { status: string; close: boolean }): Promise<void>;", native_bridge)
        self.assertIn("awaitGroupManagerApply?: () => Promise<RelayGroupManagerResult | undefined>;", platform_entry)
        self.assertIn("finishGroupManagerApply?: (options: { status: string; close: boolean }) => Promise<void>;", platform_entry)
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
        # 模型列表 keeps a visible scroller while its models overflow the box.
        # Overlay+autohide is right for a list that fits and wrong for one that
        # does not: the overflowing list looked complete with the rest of its
        # models below the fold.  The policy is the shared table's own — the
        # capsule exactly while the document is taller than the clip view.
        self.assertIn("private func applyOverflowScrollerPolicy() {", mac_leaf)
        self.assertIn("let overflows = textView.frame.height > contentView.bounds.height + 0.5", mac_leaf)
        self.assertIn("let style: NSScroller.Style = overflows ? .legacy : .overlay", mac_leaf)
        self.assertIn("autohidesScrollers = !overflows", mac_leaf)
        self.assertIn("applyOverflowScrollerPolicy()\n    }", mac_leaf)
        # A new key's models change the document but not this scroll view's own
        # frame; without an explicit layout pass the policy kept answering for the
        # model list the window opened on and the overflowing list showed no
        # capsule.
        set_models = mac_leaf.split("func setModels(_ names: [String]) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("refitDocument()", set_models)
        self.assertIn("needsLayout = true", set_models)
        # The models view must not pin the fitting-case chrome as its only state.
        models_view = mac_leaf.split("private final class NativeModelsListView: NSScrollView {", 1)[1].split("\n/// ", 1)[0]
        self.assertIn("hasVerticalScroller = true", models_view)
        self.assertIn("std::wstring EllipsizeMiddle(std::wstring const& value) {", windows_leaf)
        self.assertIn("kHead = 14;", windows_leaf)
        self.assertIn("kTail = 10;", windows_leaf)
        self.assertIn("value_text.Text(winrt::hstring(EllipsizeMiddle(*revealed_value)));", windows_leaf)
        self.assertNotIn("value_text.IsTextSelectionEnabled(true);", windows_leaf)
        self.assertIn("content.widthAnchor.constraint(equalToConstant: contentWidth),", mac_leaf)
        # The sheet keeps the one panel inset every child window keeps, so its
        # heading, its columns, and its footer start at the same edge as the
        # model chooser's and the file editor's.
        self.assertIn("let contentWidth = nativePanelInset + listWidth + 18 + detailWidth + nativePanelInset", mac_leaf)
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
        self.assertIn("controls::ScrollViewer::SetVerticalScrollBarVisibility(models_scroll, controls::ScrollBarVisibility::Visible);", windows_leaf)
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
        # The transient copy result takes the sheet's own status line in the
        # footer, beside its buttons.
        self.assertIn("auto show_copy_status = [footer_status, status_timer]", windows_leaf)
        self.assertIn("controls::Grid::SetColumn(footer_status, 1);", windows_leaf)
        # 自动分组 is a *preview* inside the sheet: the switch stages the layout
        # over the rows already on screen, and the groups with no key yet are
        # listed as draft rows.  The window reads the station whenever the
        # account's 自动分组 is on — a checked switch is Core keeping one key per
        # group, so this window opens on the station's own keys and aligns to
        # them; drawing the pane's last reading instead is the stale list a user
        # sees after the station renamed a group.  An account with the switch off
        # has no layout to be wrong about and keeps the cheap opening.
        self.assertIn("const groupManagerHasUsableFacts = (current: RelayAccount): boolean =>", relay)
        self.assertIn('current.resourceStatus === "ready" && current.resources.length > 0;', relay)
        self.assertIn('if (await refreshResources(current.id) !== "ready") return current;', relay)
        self.assertIn("if (!readsStation) return current;", relay)
        self.assertIn("if (align) {", relay)
        self.assertIn("await applyStagedQuietly();", relay)
        self.assertIn("pendingDelete: entry.pending_delete === true,", relay)
        # The list is the account's own keys and only those; 自动分组 checked is
        # a *preview* the window draws over them itself, and unchecking it puts
        # that preview away.  The request therefore never carries a synthetic
        # key list, and the window never shows a row the station does not report.
        self.assertIn("keys: current.resources.map((resource) => ({", relay)
        self.assertNotIn("claimedGroupIDs", relay)
        self.assertIn('if (current.type === "newapi" && !current.autoGrouping) {', relay)
        # A group the station no longer offers is not a choice the picker may
        # carry: Core refuses such a group, and the checked switch's own layout
        # cannot keep the key that names it.  Synthesizing a selectable item
        # for it made the window offer a group that does not exist.
        self.assertNotIn("groups.push({", relay)
        # The relay refresh reports its status through the dispatch action
        # summary; the top-level result carries the revision alone.  Only a
        # successful read is remembered for the reuse window, so the next
        # implicit caller never inherits a failure.
        self.assertIn('const status = asRecord(staged.action_summary).resource_status === "ready" ? "ready" : "unavailable";', (ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx").read_text(encoding="utf-8"))
        self.assertIn('if (status === "ready") relayRefreshFresh.current.set(accountId, { at: Date.now(), status });', (ROOT / "rn/packages/shared/src/ui/YoungRouterApp.tsx").read_text(encoding="utf-8"))
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
        # The height is rounded to the whole DIPs the window API takes, so the
        # measured layout decides the size instead of a truncated double.
        self.assertIn(
            "{590, static_cast<int32_t>(std::lround(window_height))}",
            windows_leaf,
        )
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

    def test_a_background_wait_never_locks_an_editable_field(self) -> None:
        """A wait started elsewhere does not disable the pane the user is editing.

        The provider detail used to carry one window-wide busy flag into every
        control, while the strip cleared to 就绪 at the start of each counted
        run.  Leaving a pane therefore disabled the pane the user opened (its
        Apply was still running), and the window said it was ready.
        """

        identity = self.ui.split("function ProviderIdentityFields(", 1)[1].split("function ProviderEditor(", 1)[0]
        # Identity fields are drafts staged on the shared queue, not commands:
        # an in-flight action never freezes the name, the address, or 启用.
        self.assertNotIn("disabled={busy}", identity)
        self.assertNotIn("busy: boolean;", identity)
        # A station account panel holds its rows for its own writes only; the
        # window flag describes work another surface started.
        self.assertIn("const controlsBusy = formBusy || stationBusy;", self.relay)
        self.assertNotIn("const controlsBusy = busy || formBusy || stationBusy;", self.relay)
        # The provided key's value is not being read: it is managed by the
        # station.  Placeholder text that claims a read is in progress is what
        # read as a stuck field in the provider inspector.
        provided = self.ui.split("function ProviderKeysPanel", 1)[1].split("\nfunction ", 1)[0]
        # The key editor's own two fields are drafts of this panel's writes:
        # they carry `formBusy`, never the window flag.
        self.assertNotIn("disabled={busy}", provided)
        self.assertIn("disabled={formBusy}", provided)
        self.assertIn('busy={formBusy} domain="providers_models"', provided)
        self.assertIn("disabled={formBusy || selectedProvided.account.autoGrouping}", provided)
        self.assertIn('hint={selectedProvided.resource.keyHint ? translate("providers.relayKeyValueHint") : translate("common.none")}', provided)
        self.assertNotIn('hint={selectedProvided.resource.keyHint ? translate("providers.apiKeySavedHint")', provided)
        # The one input auto grouping legitimately locks says why it is locked:
        # the switch that owns the name is in 分组管理, not on this pane.
        self.assertIn('{selectedProvided.account.autoGrouping ? <Text style={styles.fieldHint}>{translate("providers.keyNameAutoGroupingHint")}</Text> : null}', provided)
        self.assertIn('"providers.keyNameAutoGroupingHint": "自动分组已开启"', self.zh)
        self.assertIn('"providers.keyNameAutoGroupingHint": "Auto grouping is on"', self.en)

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
        apply_body = self.ui.split("const applyOutcome = (options?: { silent?: boolean; quiet?: boolean; message?: string }):", 1)[1].split("const autoAppliedKey", 1)[0]
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
            'cells: (credentials ?? []).length > 0 && variant !== "inline"',
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
            "relay.apiKeyActions.update?.(selectedProvided.account.id, selectedProvided.resource.id, name)",
            "relay.apiKeyActions.create?.(account.id, options)",
            'relay.apiKeyActions.remove?.(account.id, resource.id, "detach_disabled")',
            'target={`${selectedProvided.account.id}:${selectedProvided.resource.id}`}',
            'domain="relay_accounts"',

        ):
            self.assertIn(marker, ui)
        relay = self.relay
        for marker in (
            "export function providedKeyRows(accounts: RelayAccount[], translate: Translate): ProvidedKeyRow[]",
            "export function ApiKeyCreateDialog(",
            "accountLabel: accountDisplayName(account, translate),",
            "resourceGroupUnavailable(resource, account.groups)",
        ):
            self.assertIn(marker, relay)

    def test_no_key_editor_is_drawn_for_a_login_service(self) -> None:
        """A login service owns its key, so its literal value is nobody's to edit.

        The provider editor derived the predicate locally from the two kinds it
        also branches on, which left the Our Free Model lane out: the lane's
        one slot is minted by Core and holds an ``os.environ/`` reference to the
        loopback worker's bearer, and the 密钥 editor offered that reference as
        an editable, copyable API key.  One predicate answers this for both the
        identity block and the panel, so no kind can be added to one and missed
        by the other.
        """

        editor = self.ui.split("function ProviderEditor(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("const isLogin = isLoginProviderKind(kind);", editor)
        self.assertNotIn(
            "const isLogin = isOfficialAccount || isWorkBuddyAccount;", editor
        )
        # The keys panel, the station-accounts panel, and the accounts header
        # are the three surfaces that predicate gates.
        # The account section lists the row's accounts, and custom keys are its
        # peers: one OpenAI row serves both, so the keys panel is drawn for it.
        self.assertIn('{!isLogin || kind === "openai" ? <ProviderKeysPanel', editor)
        # An account is not a key slot, so it never enters the key editor: the
        # signed-in accounts get their own list, drawn with the station's own
        # shared table under the name that associations use everywhere.
        self.assertNotIn('credentials={kind === "openai" ? authAccounts', editor)
        self.assertIn('{kind === "openai" ? <View style={styles.officialAccountSection}>', editor)
        self.assertIn('<AccountsTable\n        title={translate("providers.accountLinks")}', editor)
        self.assertIn('dispatchWithOutcome("service_provider.auth_logout", { provider_id: id, account_ref: accountRef }, "providers_models")', editor)
        self.assertIn("{!isLogin && station ? <StationAccountsPanel", editor)
        identity = self.ui.split("function ProviderIdentityFields(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("const isLogin = isLoginProviderKind(kind);", identity)
        # The predicate itself names every login service, the lane included.
        self.assert_ui_has(
            'return kind === "openai" || kind === "workbuddy" || kind === "workbuddyGlobal" || kind === "qoder" || kind === "qoderGlobal" || kind === "ourFreeModel";'
        )
        # ...and the panel refuses Core's own slot itself, so the guarantee does
        # not rest on every caller naming the service correctly.  The panel no
        # longer filters the slots inline: it reads the same credential
        # derivation 获取模型's picker reads, and that derivation is where a
        # managed slot is dropped — for the table's rows and the picker's
        # choices alike, so neither surface can offer Core's reference as a key.
        panel = self.ui.split("function ProviderKeysPanel(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("providerCredentialKeys(keySlots, providedRows.map((row) => ({", panel)
        credentials = (ROOT / "rn/packages/shared/src/ui/providerCredentials.ts").read_text(encoding="utf-8")
        self.assertIn('if (slot.managed || slot.source.kind !== "independent") continue;', credentials)
        self.assertIn('if (slot.managed || slot.source.kind !== "relay") continue;', credentials)
        self.assertNotIn(
            'providerKeyStates(provider).filter((key) => key.source.kind === "independent") : []',
            self.ui,
        )
        self.assert_ui_has('managed: booleanValue(value.managed),')

    def test_a_relay_key_is_named_by_its_account_and_group_everywhere(self) -> None:
        """One name for one relay key: 账号/分组, never the group alone.

        The provider's own slot name is only the group name, so a surface
        without the account beside it (the model list's group rows, the routes
        table's key column, the wizard's provided-key list) read as a truncated
        key.  Every one of them composes the same `account/GroupName`
        label the key picker and the relay key table already use.
        """

        self.assertIn('return source ? [source.accountLabel, source.resourceLabel].filter(Boolean).join("/") : "";', self.ui)
        self.assertIn('return relaySourceName(relaySourceForKey(key, relaySources)) || name;', self.ui)
        # An account is not a key, so the same column names it before it ever
        # reaches a key lookup: an account-backed route reads as its account.
        self.assertIn('const credential = modelCredential(model, provider, credentials);', self.ui)
        self.assertIn('grouped.set(key.id, { label: modelProviderKeyLabel(item, provider ?? {}, translate, undefined, relaySources), models: [item] });', self.ui)
        self.assertIn('modelProviderKeyLabel(entry.model, entry.provider, translate, undefined, relaySources, providerCredentials(entry.provider, translate)), order],', self.ui)
        self.assertIn('const sourceName = relaySourceName(choice.source) || name;', self.ui)
        self.assertIn('label={`${source.resourceLabel || relaySourceName(source)}${multiplier}`}', self.ui)
        # The account is the selected row, so the key list is that account's.
        self.assertIn('source.accountID === selectedStationAccount.id', self.ui)
        self.assertIn('const { account, resource, label } = selectedProvided;', self.ui)
        # The relay key table already composes the same label from the account.
        self.assertIn('label: `${accountKeyPrefix(account, translate)}/${resourceGroupName(resource, account.groups, translate)}`', self.relay)

    def test_a_refused_relay_binding_marks_the_row_it_belongs_to(self) -> None:
        """A binding failure names its row: the strip keeps the outcome, rows get marked.

        Core publishes the materialization's secret-free issues as
        `binding_issues` (code + provider key + route), and the pane marks those
        rows and states the reason where the key is edited, in the same
        account/group form.  The routes table carries the marker too, because a
        route is what the user is looking at when the write is refused.
        """

        providers = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn("    def record_binding_issues(self, issues: object) -> None:", providers)
        self.assertIn('"binding_issues": [dict(item) for item in self._binding_issues],', providers)
        self.assertIn('row = {"code": code, "provider_key_id": target_key, "model_id": target}', providers)
        service = (ROOT / "young_router/core/service.py").read_text(encoding="utf-8")
        self.assertIn('recorder(materialized.get("issues") if isinstance(materialized, Mapping) else ())', service)
        self.assertIn("const bindingIssues = asRecords(state.binding_issues);", self.ui)
        # The lookup is shared by the provider list (subscribed to the domain)
        # and the inspector, so both mark the same row; the callback still
        # exists for the inspector's own consumers.
        self.assertIn("const bindingIssueFor = useCallback((model: UnknownRecord): UnknownRecord | undefined => bindingIssueRow(bindingIssues, model), [bindingIssues]);", self.ui)
        self.assertIn("function bindingIssueRow(bindingIssues: UnknownRecord[], model: UnknownRecord): UnknownRecord | undefined {", self.ui)
        self.assertIn("if (issueModel && issueModel === modelID) return true;", self.ui)
        self.assertIn("return Boolean(issueKey) && issueKey === keyID;", self.ui)
        self.assertIn("() => modelRowKeys(models.filter((item) => modelNeedsAttention(item, translate) || Boolean(bindingIssueFor(item))).map(editorIdentifier)),", self.ui)
        self.assertIn("alertRowKeys={alertRouteKeys}", self.ui)
        self.assertIn('<Text style={styles.fieldHint}>{bindingIssueText}</Text>', self.ui)
        # A relay key is a row in the provider's key table too, and the reason
        # stands beside the key it belongs to.
        self.assertIn("const bindingIssueForProvidedRow = useCallback((row: ProvidedKeyRow): UnknownRecord | undefined => {", self.ui)
        self.assertIn("alertRowKeys={[...providedRows.filter((row) => Boolean(bindingIssueForProvidedRow(row))).map((row) => `provided:${row.key}`), ...orphanSlots.filter((slot) => bindingIssues?.some((issue) => stringValue(issue.provider_key_id) === slot.id)).map((slot) => `orphan:${slot.id}`)]}", self.ui)
        self.assertIn("<Text style={styles.fieldHint}>{relayBindingIssueText(selectedProvidedIssue, selectedProvided.label, translate)}</Text>", self.ui)
        self.assertIn("|| bindingIssues.some((issue) => stringValue(issue.provider) === editorIdentifier(item))", self.ui)
        self.assertIn('account_unavailable: "providers.bindingIssueAccount",', self.ui)
        self.assertIn('resource_missing: "providers.bindingIssueResource",', self.ui)
        self.assertIn('catalog_model_missing: "providers.bindingIssueCatalog",', self.ui)
        # The strip names the row the station refused, not only the outcome:
        # the snapshot already says which key (and, for a catalog refusal, which
        # route) failed, so the sentence reads that one projection instead of a
        # second copy of the failure.
        self.assertIn('const bindingStatus = relayBindingFailureStatus(reason, failureSnapshot, translate);', self.ui)
        self.assertIn('if (stringValue(asRecord(reason).code) === "relay_binding_failed") {', self.ui)
        self.assertIn('failureSnapshot = await ipc.snapshot();', self.ui)
        self.assertIn('function relayBindingFailureStatus(reason: unknown, snapshot: unknown, translate: Translate): string | undefined {', self.ui)
        self.assertIn('const modelLabel = code === "catalog_model_missing" || code === "multiplier_missing"', self.ui)
        self.assertIn('translate("providers.bindingIssueCatalogModel", { key: keyLabel, model: modelLabel })', self.ui)
        self.assertIn('if (stringValue(asRecord(reason).code) !== "relay_binding_failed") return undefined;', self.ui)
        types = (ROOT / "rn/packages/shared/src/i18n/types.ts").read_text(encoding="utf-8")
        for key in (
            "providers.bindingIssueAccount",
            "providers.bindingIssueResource",
            "providers.bindingIssueCredential",
            "providers.bindingIssueCatalog",
            "providers.bindingIssueCatalogModel",
            "providers.bindingIssueBaseUrl",
            "providers.bindingIssueGeneric",
        ):
            self.assertIn(f'| "{key}"', types)
            self.assertIn(f'"{key}":', self.zh)
            self.assertIn(f'"{key}":', self.en)

    def test_relay_dialogs_avoid_the_unregistered_macos_fabric_modal_host(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")

        # react-native-macos 0.85 can crash in RCTComponentViewFactory when
        # ModalHostView is first mounted on a secondary route surface.
        self.assertNotIn("<Modal", relay)
        self.assertNotIn("import { Modal,", relay)
        self.assertIn("function RelayDialogLayer(", relay)
        # One form (新建 API 密钥) still needs a scrim of its own; no question
        # does — a confirm is the host's decision panel, presented by the host.
        self.assertEqual(1, relay.count("<RelayDialogLayer visible={visible} onRequestClose={onClose}>"))
        self.assertIn(
            'relayDialogLayer: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, zIndex: 100 }',
            relay,
        )

    def test_confirmations_state_their_destructive_answer_instead_of_inferring_it(self) -> None:
        """One confirmation surface: the host's own decision panel.

        A confirm is asked by the host — ``native.showConfirmation`` — and never
        drawn in the page.  The relay panels used to draw their two removals
        themselves, which put one decision on two surfaces: the app's dialog for
        a provider, a model, an account or a local key, and a hand-made one for
        these rows, with its own scrim, its own button row and a policy picker
        the dedicated panel has no room for.  The policy those removals applied
        is now stated in the question, so the answer needs nothing but the
        question and its one destructive button.
        """
        relay = self.relay
        ui = self.ui

        # No component in the shared UI presents a decision: that is not a
        # component's to draw, and a second one is a second answer to "what
        # does a confirm look like here".
        self.assertNotIn("DependencyPolicyDialog", relay)
        self.assertNotIn("DependencyPolicyDialog", ui)
        self.assertNotIn("relay.dependencyPolicy", relay)
        self.assertNotIn("relay.dependencyPolicy", ui)
        # Both removals ask the host, and both state that the answer is
        # destructive instead of leaving it to the translated label.
        self.assertIn('title: translate("relay.removeLocalTitle"),', relay)
        self.assertIn('      confirmLabel: translate("relay.removeLocal"),\n      destructive: true,\n', relay)
        self.assertIn('title: translate("relay.apiKeyDeleteTitle"),', ui)
        self.assertIn('        confirmLabel: translate("common.delete"),\n        destructive: true,\n', ui)
        # The policy the removal applies is part of the question, because the
        # dedicated panel answers with one button and holds no picker: models
        # that follow the keys stay (or are released) as independent models.
        self.assertIn('translate("relay.removeAccountModelsKept", { models })', relay)
        self.assertIn('dependency_policy: "detach"', relay)
        self.assertIn('relay.apiKeyActions.remove?.(account.id, resource.id, "detach_disabled")', ui)
        # The relay dialog surface still exists for the one form it carries; no
        # question is published on it.
        surface = (ROOT / "rn/packages/shared/src/ui/relayDialogSurface.tsx").read_text(encoding="utf-8")
        self.assertIn("export function useRelayDialogSurface(", surface)
        self.assertIn("export function RelayDialogHost(", surface)
        # The host is told after every render of a publishing panel, not only
        # when it mounts: the entry it holds is the panel's current render, and
        # a form opened later redrew nothing, so its ＋ did nothing until some
        # unrelated render of the route happened to draw the surface.
        self.assertIn("  useLayoutEffect(() => {\n    notify();\n  });", surface)
        self.assertIn("const dialogsHosted = useRelayDialogSurface(() => dialogs);", ui)
        self.assertEqual(2, ui.count("{dialogsHosted ? null : dialogs}"))
        self.assertNotIn("removalDialogHosted", relay)
        # The host is a sibling of the inspector, so no column clips it.
        self.assertIn("<RelayDialogHost />", ui)
        self.assertRegex(ui, r"\}\n    <RelayDialogHost />")
        # Every confirmation that deletes or discards says so: the provider,
        # model, and API key deletes, the route close that drops staged changes,
        # and the raw file editor's discarded draft.
        self.assertEqual(2, ui.count('confirmLabel: translate("common.delete"), destructive: true }).then('))
        self.assertIn('      confirmLabel: translate("common.delete"),\n      destructive: true,\n', ui)
        self.assertIn('        confirmLabel: translate("status.close"),\n        destructive: true,\n', ui)
        self.assertIn('          confirmLabel: translate("common.discard"),\n          destructive: true,\n', ui)

    def test_relay_keys_are_discovered_from_the_bound_station_without_manual_import_or_linking(self) -> None:
        relay = RELAY_MANAGER.read_text(encoding="utf-8")
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")

        for marker in (
            'return (["providers_models", "relay_accounts"] as const).filter',
            # A provider's station keys come from the station it is bound to.
            # Matching the Base URL alone offered the keys of any station that
            # happened to share an address, which the key table never listed —
            # so discovery is scoped by the binding, and a provider that binds
            # no station is offered none.
            'function relaySourcesForProvider(',
            'if (providerKind(provider) !== "relay") return [];',
            'return relaySources.filter((source) => source.stationID === stationID);',
            'function providerKeyChoices(provider: UnknownRecord, relaySources: RelaySourceOption[]): ProviderKeyChoice[] {',
            'function ProviderIdentityFields(',
            'function relayStationsFromSnapshot(',
            'const nextStation = relayStationForBaseUrl(endpoint, relayStations);',
            'dispatch("provider.select_relay_station", { provider_id: providerID, station_id: station.id })',
            'const matchingRelaySources = providerAuthKind(provider) === "api_key" ? relaySourcesForProvider(',
            'const keyChoices = selectedProvider ? providerKeyChoices(selectedProvider, relaySourcesForProvider(selectedProvider, relaySources, activeProviderBaseURL)) : [];',
            'const action = relaySource ? "provider.fetch_relay_resource_models" : "providers.fetch_models";',
            'dispatch("model.select_relay_resource"',
            'const providerKeyName = drafts?.providerKeyDisplayName(providerId, providerKey.id, providerKey.name) ?? providerKey.name;',
            'changes: { provider_key_id: providerKey.id, api_key_name: providerKeyName },',
            'providerKeyOptions.length > 0 ? <PickerField label={translate("providers.providerKey")}',
            'const activeRouteGroupUsesMultiplier = activeRouteGroup.some((entry) => modelOrderMode(entry.model) === "relay_multiplier");',
            'const canFollowMultiplier = followsServiceRate && followedRate !== undefined;',
            'label={translate("providers.order")}',
            'label={translate("providers.followMultiplier")}',
            'accessory={canFollowMultiplier ? <NativeCheckbox',
            'result.status === "partial"',
            'result.status === "failed"',
            'publishResult(applyResultMessage(value, translate, typeof message === "string" ? message : null, latestSnapshot.current?.service.state));',
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
        # A relay binding Core refused is stated on the row it belongs to: the
        # code Core reports resolves to the pane's own sentence for that kind of
        # failure, and the keys panel turns the row it resolves to brown.
        self.assertIn('resource_unavailable: "providers.bindingIssueResource",', self.ui)
        self.assertIn(
            'alertRowKeys={[...providedRows.filter((row) => Boolean(bindingIssueForProvidedRow(row))).map((row) => `provided:${row.key}`), ...orphanSlots.filter((slot) => bindingIssues?.some((issue) => stringValue(issue.provider_key_id) === slot.id)).map((slot) => `orphan:${slot.id}`)]}',
            self.ui,
        )
        # The remote delete applies one policy, and the question that asks for
        # it states that policy instead of offering a picker.
        self.assertIn('relay.apiKeyActions.remove?.(account.id, resource.id, "detach_disabled")', self.ui)
        self.assertNotIn('value: "detach_only"', self.ui)

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
        self.assertIn('emptyText={translate("relay.stationNoAccounts")}', relay)
        self.assertIn('styles.emptyText}>{emptyText}</Text>', ACCOUNTS_TABLE.read_text(encoding="utf-8"))
        # An empty key list keeps the area under it empty: the old hint read
        # like a value the user could edit.
        self.assertNotIn('translate("providers.keyListHint")', self.ui)
        self.assertNotIn('translate("providers.providedKeysNeedAccount")', self.ui)
        self.assertIn('} style={styles.panelActionButton} />)}\n        {selectedProvidedIssue ? <Text style={styles.fieldHint}>{relayBindingIssueText(selectedProvidedIssue, selectedProvided.label, translate)}</Text> : null}\n      </> : selectedOrphan ? <>', self.ui)
        self.assertIn('        <Text style={styles.fieldHint}>{translate("providers.orphanKeyHint")}</Text>\n      </> : null}\n    </View>;', self.ui)
        # The relay account section's + shares the key panel's right edge.
        self.assertIn(
            '<View style={styles.panelHeader}>\n        {/* The one association block: a station-backed provider links a relay\n            station, a provider addressed at an official service links that\n            service\'s own account.  The label follows the provider. */}\n        <Text style={styles.panelTitle}>{translate(providerService(provider) ? "providers.serviceLinks" : "providers.accounts")}</Text>\n        <View style={styles.panelActions}>',
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
            'dataManagementSectionItem: { minHeight: 22 }',
            'const promptedDiskGeneration = useRef<Partial<Record<EditableDiskDomain, number>>>({});',
            'void native.showConfirmation({',
            'message: translate("settings.diskChangedBody"),',
        ):
            self.assert_ui_has(marker)

        relay = self.relay
        for marker in (
            'formRow: { width: "100%", minHeight: 30, flexDirection: "column", alignItems: "stretch", gap: 5 }',
            # 刷新 and 分组 sit on the account row itself, so the relay pane has
            # no separate action row.
            'fieldRow: { minHeight: 26, flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap" }',
            'accountsEmpty: { minHeight: 40, alignItems: "center", justifyContent: "center", paddingHorizontal: 10, paddingVertical: 8, borderWidth: 1, borderColor: colors.separator, backgroundColor: colors.panel }',
        ):
            self.assertIn(marker, relay, marker)
        self.assertNotIn('accountActionsRow:', relay)
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
        self.assertIn("const beginRelayLogin = async (signInAnother = false): Promise<void> => {", wizard)
        self.assertIn("provider.select_relay_station", wizard)
        # Pending login: the shell is created only after sign-in succeeds, so
        # a cancelled flow reserves nothing and cannot cascade a station away.
        self.assertIn("pendingAccount: true,", wizard)
        self.assertNotIn("await relay.addAccount(", wizard)
        self.assertNotIn('relay.commit("account.delete"', wizard)
        # The station's page is its own window: embedding it made the wizard a
        # mostly blank window with a whole site loading inside its body.
        self.assertIn("embedded: false,", wizard)
        self.assertIn('translate("relay.signInWindowHint")', wizard)
        self.assertIn("native.cancelRelayLogin();", wizard)
        self.assertIn('provider.select_relay_station', wizard)
        # Login providers keep the official device-code flow on the keys step.
        self.assertIn("service_provider.auth_start", wizard)
        self.assertIn("const goBack = (): void => {", wizard)
        self.assertIn("cancelRelaySignIn();", wizard)
        self.assertNotIn("<ScrollView", wizard.split('loginPhase === "sign-in" ? <', 1)[0])

    def test_the_wizard_creates_nothing_before_it_finishes(self) -> None:
        """添加向导 stages every step; 完成 is the one act that creates.

        The provider (with its key and its models) may not appear because the
        user pressed 下一步, and a wizard that is closed without finishing
        reserves nothing — the key it staged is dropped with its token.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        core = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        # The staged key's target is the wizard's own token, and both sides
        # spell its prefix the same way.
        self.assertIn('const WIZARD_PENDING_KEY_PREFIX = "__wizard_provider__";', self.ui)
        self.assertIn('_WIZARD_KEY_TARGET_PREFIX = "__wizard_provider__"', core)
        self.assertIn("const [pendingKeyToken] = useState(() => `wizard-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`);", wizard)
        self.assertIn("const stagedKeyTarget = providerWizardKeyTarget(pendingKeyToken, stagedKeyName);", wizard)
        self.assertIn("target={stagedKeyTarget}", wizard)
        # 下一步 walks the steps; it never mints the provider.
        self.assertNotIn("createProvider", wizard)
        # The first exit is the same one both branches take: a type with a key
        # step lands on 密钥, the keyless ones go straight to 模型.
        self.assertIn('if (!providerStepValid()) return;\n        advanceFromProviderStep();\n        return;', wizard)
        self.assertIn('if (creatingProvider) {', wizard)
        self.assertIn("const targetProviderID = providerID || await commitWizardProvider();", wizard)
        self.assertIn('dispatchWithOutcome("provider.add", {\n      provider: {', wizard)
        # The token, never the secret target it was staged under: Core files
        # the value under the bare token and adopts it by that name, so a
        # payload that echoed the target would be refused as unknown.
        self.assertIn("{ pending_api_key: pendingKeyToken }", wizard)
        self.assertIn("pending_api_key: pendingKeyToken,", wizard)
        self.assertNotIn("pending_api_key: `${WIZARD_PENDING_KEY_PREFIX}", self.ui)
        self.assertIn("const fetchIdentity = creatingProvider ? pendingKeyToken : providerID;", wizard)
        # The model step lists a provider that does not exist yet through that
        # same token, so discovery leaves no record either.
        self.assertIn("pending_provider: {", wizard)
        # An official account is created by the press that signs in to it: the
        # sign-in needs the record it belongs to.
        self.assertIn('target = await addOfficialAccount(', wizard)
        self.assertIn('const startOfficialLogin = async (): Promise<void> => {', wizard)
        # The window that opened the wizard drops what it staged on every
        # dismissal path — quietly, and never into the pane that opened it.
        self.assertIn('if (route === "provider-wizard") dropProviderWizardStagedKey();', self.ui)
        self.assertIn("const registerProviderWizardKeyToken = (token: string): void => {", self.ui)
        self.assertIn('"provider.discard_pending_key",', self.ui)
        self.assertIn("{ pending_api_key: token },", self.ui)
        self.assertIn("onKeyToken(pendingKeyToken);", wizard)
        self.assertIn("onKeyToken={registerProviderWizardKeyToken}", self.ui)
        self.assertIn('if action == "provider_discard_pending_key":', core)
        # ...and Core files that value only for a target it can adopt: a
        # provider target still needs a provider.
        self.assertIn('if not provider_id.startswith(self._WIZARD_KEY_TARGET_PREFIX):', core)
        self.assertIn("pending_keys = self._pending_provider_keys.get(pending_token, {}) if pending_token else {}", core)
        self.assertIn('raise DomainError("The staged API key is unavailable")', core)

    def test_the_wizard_waits_for_the_staged_key_before_it_refuses(self) -> None:
        """A filled 密钥值 must not be reported as a missing required field.

        The key value is a native field that stages its value when it loses
        focus, and 下一步 is itself the press that blurs it: reading the
        readiness flag inside that same press saw the pre-blur state, so the
        first press always said 请填写必填项后继续 for a key the user had just
        typed, and only a second press — after Core's answer had arrived — went
        on.  下一步 and 完成 now wait for that answer instead.
        """

        wizard = self.ui.split("function ProviderSetupWizard(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        # The waiting itself: a dirty or saving field is asked to commit, and
        # the answer is what the step reads.
        self.assertIn("const flushStagedKey = (): Promise<boolean> => {", wizard)
        self.assertIn('if (stagedKeyField.current.status !== "dirty" && stagedKeyField.current.status !== "saving") {', wizard)
        self.assertIn("keyCommitRequest.current += 1;", wizard)
        self.assertIn("setKeyCommitToken(keyCommitRequest.current);", wizard)
        # The field reports its own state through one handler, and the commit
        # it is asked for rides the shared `commitRequest` prop.
        self.assertIn("const handleKeyFieldState = (state: SecretState): void => {", wizard)
        self.assertIn("onSecretState={handleKeyFieldState}", wizard)
        self.assertIn("commitRequest={keyCommitToken}", wizard)
        self.assertNotIn("setKeyReady(state.present); onSecretState(state);", wizard)
        # Every retarget of the field drops what the previous one said.
        self.assertIn("const resetStagedKeyField = (): void => {", wizard)
        # ...and the only place a bare flag is cleared is that helper itself.
        self.assertEqual(1, wizard.count("setKeyReady(false);"))
        self.assertIn("resetStagedKeyField();", wizard)
        # 下一步 reads the answered value, never the flag the same press would
        # have left stale.
        self.assertIn("const advanceToModels = async (keyFlush: Promise<boolean>): Promise<void> => {", wizard)
        # The wait settles *before* the step moves.  密钥值 is mounted by the
        # keys step alone and its native view is recycled on the way out, which
        # drops the staging answer; a wait that outlived its field never
        # resolved, so the model step spun on 正在自动获取可用模型 forever —
        # for every provider, including ones whose endpoint answers.
        self.assertNotIn("pendingKeyFlush", wizard)
        self.assertEqual(2, wizard.count("void advanceToModels(flushStagedKey())"))
        self.assertIn("void advanceToModels(flushStagedKey()).catch(() => {", wizard)
        # The step change is the last thing the settle does, after the key is
        # answered and its rename is staged.
        self.assertLess(
            wizard.index("const keyPresent = await keyFlush.catch(() => false);"),
            wizard.index("advanceStepToModel();"),
        )
        # The wait is stated on the step that owns the field, because it now
        # happens there instead of being hidden behind an instant step change.
        self.assertIn("setProcessing(true);", wizard)
        # One press must advance one step: 下一步 is the window's default button,
        # so its Return key equivalent can deliver the action a second time and
        # would otherwise walk 供应商 → 密钥 → 模型 and skip 密钥.
        self.assertIn("if (processing || advanceInFlight.current || stepRef.current !== step) return;", wizard)
        # A type that carries no secret of its own has no 密钥 step to walk
        # through: its next step after 供应商 is 模型.
        self.assertIn("const needsKeyStep = !isServiceCatalogType;", wizard)
        # The ref moves only inside the two helpers that actually change the
        # step — never in `goNext` before its validation guard.  Writing it
        # there left a rejected press pointing at the next step, so the guard
        # refused every later press for the rest of the wizard's life.
        self.assertNotIn('stepRef.current = step === "provider" ?', wizard)
        self.assertIn('stepRef.current = "keys";', wizard)
        self.assertIn('stepRef.current = "model";\n    setStep("model");', wizard)
        self.assertIn("const advanceFromProviderStep = (): void => {", wizard)
        self.assertIn('...(needsKeyStep ? [{ id: "keys" as WizardStep, title: translate("providers.wizard.stepKeys") }] : []),', wizard)
        self.assertIn("const advanceStepToModel = (): void => {", wizard)
        self.assertIn('stepRef.current = "model";\n    setStep("model");', wizard)
        # The guard's ref is declared with the step it mirrors.
        self.assertIn('const [step, setStep] = useState<WizardStep>("provider");', wizard)
        self.assertIn('const stepRef = useRef<WizardStep | undefined>("provider");', wizard)
        self.assertIn("const keyPresent = await keyFlush.catch(() => false);", wizard)
        self.assertIn('setValidation(translate("providers.wizard.required"));', wizard)
        # A refused key stops the model step, and the wheel reports the wait.
        self.assertIn('setModelFetchState("loading");', wizard)
        self.assertIn("<NativeBusySpinner label={translate(\"providers.wizard.fetchingModels\")} />", wizard)
        # The in-flight guard is a ref: a caller that just set `modelFetchState`
        # would otherwise read a stale closure and re-enter the listing.
        self.assertIn("if (modelFetchInFlight.current) return;", wizard)
        self.assertIn("if (request === modelFetchRequest.current) modelFetchInFlight.current = false;", wizard)
        # The keys step's own closure predates the model step and cannot see its
        # `serviceBaseURL`/`manualSelectedKeyName`, so the listing is triggered
        # by a ticket the model step's own render consumes.
        self.assertIn("const [modelFetchTicket, setModelFetchTicket] = useState(0);", wizard)
        self.assertIn("setModelFetchTicket((current) => current + 1);", wizard)
        self.assertIn('if (modelFetchTicket === 0 || step !== "model") return;', wizard)
        self.assertIn("void fetchWizardModels();", wizard)
        self.assertNotIn("await fetchWizardModels();", wizard)
        # The trigger sits after the listing's own declaration: an effect that
        # called it from above the `const` would be a temporal-dead-zone throw.
        self.assertLess(wizard.index("const fetchWizardModels = async"), wizard.index("if (modelFetchTicket === 0"))

    def test_a_staged_surface_keeps_a_way_back_and_reports_the_users_action(self) -> None:
        """The second half of a two-step surface must stay reachable.

        Four places used to end behind their own result: a staged import had no
        way to choose another file, a refused key rename selected the namesake,
        a snapshot refresh failing after a staged key edit reported the edit as
        failed, and the vendor sign-in claimed 登录成功 over an unread station.
        """

        # The staged-import result keeps the same choose-file control beside it.
        self.assert_ui_has('{sectionListHeader(statuses.import ?? translate("dataManagement.selectedCount", { count: stagedSections.length }), <ActionButton title={translate("dataManagement.changeImportFile")} busy={pendingAction === "inspect"} disabled={controlsBusy("inspect")} onPress={() => { void chooseImportFile(); }} />)}')
        # A rename answers the slot it edits, so a namesake is never selected.
        self.assert_ui_has('const pendingCustomKeyRename = useRef<{ id: string; name: string } | undefined>(undefined);')
        self.assert_ui_has('const renamed = customKeys.find((key) => key.id === rename.id);')
        self.assert_ui_has('pendingCustomKeyRename.current = { id: selectedCustom.id, name };')
        # A commit is not undone by the snapshot read that follows it.
        self.assert_ui_has('// The commit is the result the user asked for; the refresh that repaints')
        self.assert_ui_has('// The subscription, or the next pane action, repaints the staged row.')
        # The vendor sign-in binds the station it signed in to and states the
        # station read it actually got.
        self.assert_ui_has('const resourceStatus = await relay.refreshResources(landed?.id ?? pendingID, { force: true });')
        self.assert_ui_has('const bound = await dispatchWithOutcome("provider.select_relay_station", { provider_id: id, station_id: stationID });')
        self.assert_ui_has('const refreshedSnapshot = await relay.refreshAccounts().catch(() => undefined);')
        self.assert_ui_has('if (resourceError === "login_expired") onStatus?.(translate("relay.loginSessionExpired"));')
        self.assert_ui_has('else if (resourceStatus !== "ready" && resourceError === "unavailable") onStatus?.(translate("relay.loginResourcesUnavailable"));')
        self.assert_ui_has('else onStatus?.(translate("relay.loginComplete"));')
        # An address that cannot bind because its station name is taken says so
        # instead of looking like a lost keystroke.
        self.assert_ui_has('onStatus?.(translate("providers.relayNameTaken", { name: nextStation.name }));')
        # The 分组管理 sheet records each staged edit as it lands, so a retry
        # writes only the remainder instead of duplicating keys.
        self.assertIn('const opened = useRef(false);', self.relay)
        self.assertIn('handedOver = staged;', self.relay)
        self.assertIn('stagedEdit.name = edit.name;', self.relay)
        self.assertIn('staged.creates.push(create);', self.relay)
        self.assertIn('staged.deletes.push(keyID);', self.relay)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.relayNameTaken"', locale)
            self.assertIn('"providers.reorderSameOrder"', locale)
        # The one dialog's scrim is a dismiss target, not a click swallower:
        # 新建 API 密钥 closes on an outside press, the way the app's own
        # help tip does.
        self.assertEqual(1, self.relay.count('<Pressable accessible={false} onPress={onClose} style={StyleSheet.absoluteFill} />'))
        self.assertIn('import { Platform, PlatformColor, Pressable, StyleSheet, Text, View } from "react-native";', self.relay)
        # Leaving a pane commits what it staged and applies it, exactly as
        # closing the window does.
        shell = self.ui.split("function SettingsShell(", 1)[1].split("function SettingsRail(", 1)[0]
        self.assertIn('void flushActivePane.current?.("navigate").catch(() => undefined);', shell)
        # The commit of the pane being left is background work for the pane the
        # user just opened: it may not hold that pane's controls disabled while
        # Core spends a relay-bound Apply, with the strip already cleared to
        # 就绪 by the run itself.  Edits made meanwhile queue behind the Apply.
        self.assertIn('await apply({ silent: true });', self.ui)
        self.assertIn('const applyStagedQuietly = (): Promise<void> => apply({ quiet: true });', self.ui)
        self.assertIn('setResult(translate(purpose === "navigate" ? "runtime.fixInvalidBeforeLeaving" : "runtime.fixInvalidBeforeClose"));', self.ui)
        for locale in (self.zh, self.en):
            self.assertIn('"runtime.fixInvalidBeforeLeaving"', locale)
        self.assertIn('const flushActivePane = useRef<((purpose?: "close" | "navigate") => Promise<boolean>) | undefined>(undefined);', self.ui)

    def test_the_log_filter_owns_its_text_until_core_confirms_it(self) -> None:
        """A poll landing mid-typing must not delete the typed character.

        The field is fed from the Core projection while the edit is debounced
        for 250 ms, so a snapshot still carrying the previous filter used to
        overwrite what the user had just typed.
        """

        logs = self.ui.split("function LogsWorkspace(", 1)[1].split("\nfunction EmptyState(", 1)[0]
        self.assertIn("const filterDirty = useRef(false);", logs)
        self.assertIn("if (filterDirty.current && coreFilter !== filterDraftRef.current) return;", logs)
        self.assertIn("filterDirty.current = true; setFilterDraft(filter);", logs)
        # A tab switch starts from that tab's own filter, and the edit that was
        # still being debounced for the tab the reader left is not carried into
        # the field of the one they opened.
        self.assertIn("filterDirty.current = false;\n    // A debounced edit and an in-flight write both belong to the tab they were", logs)
        self.assertIn("    appliedFilter.current = \"\";\n  }, [selected]);", logs)
        # The text on screen is the text Core filters by: a view write that lost
        # a race is re-issued for the newer draft instead of leaving Core
        # filtering by a prefix of what the reader typed.
        self.assertIn("const flushFilter = useRef<() => void>(() => {});", logs)
        self.assertIn('void dispatch("logs.set_filter", { tab, filter: value }, "logs")', logs)
        self.assertIn("filterTimer.current = setTimeout(() => { flushFilter.current(); }, 250);", logs)
        # A log view action states an absolute value, so a lost revision race
        # rebases instead of being dropped on the floor.
        self.assertIn("if (LOGS_VIEW_ACTIONS.has(normalized)) return true;", self.ui)
        self.assertIn('  "logs_set_filter",\n', self.ui)

    def test_a_non_numeric_order_stays_in_the_field_instead_of_becoming_zero(self) -> None:
        """The model inspector's 顺序 field validates like every number field.

        A draft that was not a number used to be committed as 0 (first slot),
        silently reordering the group; the shared TextField now takes a
        commit-time validator and draws the message in its own row.
        """

        self.assertIn("validate?: (next: string) => string | undefined; /** A control that belongs to this row beside the field, such as its help mark. It rides the same grid level as PickerField's, so a field with an accessory and a picker with one draw the same column. */ accessory?: React.ReactNode; style?: StyleProp<ViewStyle> }): React.JSX.Element", self.ui)
        self.assertIn("const field = usePendingTextField(value, onCommit, label, onDraftChange, validate);", self.ui)
        self.assertIn('{field.error !== undefined ? <Text style={[styles.fieldError, hintStyle]} accessibilityLiveRegion="polite">{field.error}</Text> : null}', self.ui)
        self.assertIn("field.error !== undefined && styles.inputInvalid", self.ui)
        inspector = self.ui.split("function ModelInspector(", 1)[1].split("function HelpTip(", 1)[0]
        self.assertIn("validate={(nextOrder) => {", inspector)
        self.assertIn('const trimmed = nextOrder.trim();\n        return trimmed === "" || Number.isFinite(Number(trimmed)) ? undefined : translate("runtime.invalidNumber");', inspector)
        self.assertIn("const parsed = Number(nextOrder.trim());", inspector)

    def test_a_new_provider_and_model_start_enabled(self) -> None:
        """＋新建供应商 / ＋新建模型 create live rows, not parked drafts.

        The create payloads state `enabled: true` (a create with no flag is
        enabled in Core too), and the pane marks a row that still carries the
        placeholder name so a live placeholder never reads as a finished route.
        """

        workspace = self.ui.split("function ProviderWorkspace(", 1)[1].split("function ProviderKeysPanel(", 1)[0]
        self.assertIn('runMutationOnce("provider.add", () => dispatch("provider.add", { provider: { name, models: [], enabled: true } }));', workspace)
        self.assertIn('runMutationOnce(`model.add:${providerId}`, () => dispatch("model.add", {', workspace)
        self.assertIn('runMutationOnce(`model.duplicate:${providerId}:${editorIdentifier(model)}`, () => dispatch("model.duplicate", {', workspace)
        self.assertIn('model: { name, upstream_model: name, enabled: true, order: 0,', workspace)
        self.assertNotIn('enabled: false, order: 0', workspace)
        self.assertIn("if (isDraftModel(model, translate)) return true;", self.ui)
        self.assertIn("(item) => modelNeedsAttention(item, translate) || Boolean(bindingIssueFor(item))", self.ui)
        self.assertIn("asRecords(item.models).some((model) => modelNeedsAttention(model, translate) || bindingIssueRow(bindingIssues, model) !== undefined)", self.ui)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.draftModelHint"', locale)

    def test_a_login_item_the_host_refused_hands_cores_preference_back(self) -> None:
        """Launch-at-login is two-sided; a failed host side is rolled back.

        Core records the preference and the host registers the login item.  A
        host that refused the registration used to leave Core claiming an
        autostart the system would never perform; the pane now dispatches the
        opposite preference, exactly as the status menu's own toggle does.
        """

        general = self.ui.split("function GeneralWorkspace(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("await native.setLaunchAtLogin(enabled);", general)
        self.assertIn("await dispatchServiceAction(enabled ? \"service.autostart_disable\" : \"service.autostart_enable\");", general)
        self.assertIn("throw hostReason;", general)

    def test_provider_table_columns_fit_the_fixed_provider_pane(self) -> None:
        self.assertIn('"providers.modelCount": "Count"', self.en)
        self.assertIn('"providers.modelCount": "模型数"', self.zh)
        self.assertNotIn('"providers.routeSource"', self.zh)
        self.assertIn('"providers.order": "顺序"', self.zh)
        self.assertIn('"providers.followMultiplier": "跟随倍率"', self.zh)
        self.assertNotIn('"providers.effectiveOrder"', self.zh)
        self.assertIn('"providers.keyName": "密钥名"', self.zh)
        self.assertIn('"providers.keyValue": "密钥值"', self.zh)
        # The list is a column of provider names, and its widest row is
        # ``youyangai``: a 120 pt pane holds that beside the row's padding and
        # leaves the models their width.  The pane is fixed, so a long name
        # ellipsizes instead of pushing the two panes beside it.  Its own title
        # row is the floor: ``Providers`` measures 60 pt, and the ＋/− buttons
        # beside it are 50 more.
        self.assert_ui_has('columns={[{ label: translate("providers.provider"), width: 112 }]}')
        self.assert_ui_has('providerListPane: { width: 120, minWidth: 120, maxWidth: 120')
        # The providers side's model list is the upstream model, its public
        # model, and the rate the route follows: it carries no 顺序 column,
        # because that number orders one public model's routes while these rows
        # are grouped by provider key.
        self.assert_ui_has('onLayout={({ nativeEvent }) => setModelTableWidth')
        self.assert_ui_has('modelTableColumns(modelTableWidth, translate)')
        self.assertIn('"providers.upstream": "上游模型"', self.zh)
        self.assertIn('"providers.upstream": "Upstream"', self.en)
        self.assertIn('"providers.publicModel": "公开模型"', self.zh)
        self.assertIn('"providers.publicModel": "Public model"', self.en)
        self.assertIn('"providers.modelRate": "倍率"', self.zh)
        self.assertIn('"providers.modelRate": "Rate"', self.en)
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
        shared = ACCOUNTS_TABLE.read_text(encoding="utf-8")
        self.assertEqual(1, shared.count("striped"))
        self.assertNotIn("striped={false}", shared)
        self.assertNotIn("<NativeTable", relay)
        # Three rows plus the header, at each platform's own metrics: AppKit's
        # compact table draws a 24 pt header over 22 pt rows (24 + 66 + 2 = 92)
        # and the Windows/browser table a 30 pt header over 30 pt rows
        # (30 + 90 + 2 = 122).  One fixed 84 showed a single account on macOS,
        # and 116 clipped the third row on the 30 pt hosts.
        self.assertIn('tableHeight = Platform.OS === "macos" ? 92 : 122', shared)

    def test_relay_resource_columns_fit_the_initial_minimum_viewport(self) -> None:
        """The unified keys table is a single readable column in the detail pane."""
        # A credential list states what each row is worth; a keys-only list
        # stays one readable column.  The column is the width the frame has
        # left beside the editor rather than a fixed 264: a 264 pt column in a
        # 174 pt frame made the row itself the horizontal scroller, and the
        # table slid sideways under a heading that did not move.
        self.assertIn(
            '? [{ label: translate((credentials ?? []).length > 0 ? "providers.credentials" : "providers.keys"), width: inlineKeysWidth }]',
            self.ui,
        )
        self.assertIn("const inlineKeysWidth = Math.max(40, inlineKeysListWidth - 6);", self.ui)

    def test_webdav_form_is_integrated_into_the_unified_sync_tab(self) -> None:
        workspace = self.ui.split("function DataManagementWorkspace(", 1)[1].split(
            "function RuntimeField(",
            1,
        )[0]
        section = self.ui.split("function WebDavSyncSection(", 1)[1].split("function WebDavSettingsPanel(", 1)[0]
        form = self.ui.split("function WebDavSettingsPanel(", 1)[1].split("function WebDavPasswordField(", 1)[0]
        for marker in (
            "function WebDavSyncSection(",
            "function WebDavSettingsPanel(",
            '{tab === "webdav" ? <PersistentScrollView style={styles.dataManagementPane} contentContainerStyle={[styles.dataManagementPaneScrollContent, dataManagementPolishStyles.paneScrollContent]}>',
            '<WebDavSyncSection snapshot={snapshot} onOpenSettings={onOpenWebDavSettings} status={statuses.webdav} translate={translate} />',
            '{webDavStateLabel(translate, enabled)}',
            # The category names itself and carries its own 设置: that button
            # belongs to WebDAV 同步, not to the 同步 tab that lists categories.
            '<ActionButton title={translate("webdav.details")} onPress={onOpenSettings} />',
            # The category is two lines: the title with its state and 设置, then
            # the server it syncs to with the last run's outcome.
            # One grid row: row label, value cell, action column, and the facts
            # under the same control column as every other pane's tip.
            # Two columns, like the client file rows: the thing and its facts on
            # the left, the one action at the end.
            '<SettingsActionRow',
            'title={translate("webdav.sectionTitle")}',
            'detail={<View style={styles.settingsActionRowFacts}>',
            '<Text numberOfLines={1} style={styles.settingsActionRowFact}>{translate("webdav.serverLabel")}{host === "" ? translate("webdav.notSet") : host}</Text>',
            '<Text numberOfLines={1} style={[styles.settingsActionRowFact, lastFailed ? styles.settingsActionRowFactFailed : null]}>{translate("dataManagement.lastSync", { time: formatActionTimestamp(lastSyncAt) })}{lastFailed ? ` ${translate("webdav.failedSuffix")}` : ""}</Text>',
            # The form it opens is fields of the shared settings grid.
            '<View style={styles.webdavFieldList}>',
            '<WebDavField label={translate("webdav.url")}>',
            'label={translate("webdav.remoteFile")} hint={translate("webdav.remoteFileHint")}',
            'label={translate("webdav.syncEvery")} hint={translate("webdav.syncEveryHint")}',
            'label={translate("webdav.httpTimeout")}',
            'label={translate("webdav.conflict.title")} hint={translate("webdav.conflict.hint")}',
            "function WebDavPasswordField(",
            'placeholder={configured ? translate("webdav.passwordHintConfigured") : translate("webdav.passwordHintOptional")}',
            'webdavDialogBodyContent: { padding: SETTINGS_PANE_INSET }',
            "dataManagementToolbarButtons:",
        ):
            self.assert_ui_has(marker)
        self.assertNotIn(
            '<NativeSecretField label={translate("webdav.password")}',
            self.ui,
        )
        self.assertEqual(1, workspace.count("<WebDavSyncSection "))
        # ... and the sync list adds nothing above it: the tab's own header names
        # 同步, so a second bold caption over one row was a heading about a heading.
        self.assertNotIn("webdavSectionTitle", self.ui)
        # The category is a row of the grid (label, value, action, facts under the
        # control column), and the dialog's labelled rows are the same grid, so one
        # edit moves 运行时, 外部, 备份与同步 and this form together.  Neither
        # states a label width, a weight, or a help indent of its own.
        self.assertIn('title={translate("webdav.sectionTitle")}', section)
        self.assertNotIn("fontWeight", section)
        self.assertNotIn("webdavSection", section)
        # The action is the row's own third column's worth of width, not a value
        # cell: a middle column put 设置 a column away from the name it acts on.
        self.assertNotIn("SETTINGS_FIELD_CELL", section)
        self.assertIn("webdavFieldRow: { ...SETTINGS_FIELD_ROW_INDENTED }", self.ui)
        self.assertIn("webdavFieldLabel: { ...SETTINGS_FIELD_LABEL }", self.ui)
        # The display states the state; the switch itself is the form's.
        self.assertNotIn("<NativeToggle", section)
        self.assertIn('<NativeToggle accessibilityLabel={translate("webdav.enabled")} value={values.enabled}', form)
        # Nothing else may join the category: two rows is the whole display,
        # and everything that acts (the switch, the one-off run, the probe)
        # stays in the form.
        for removed in ("webdavEnableRow", "webdavSectionActions"):
            self.assertNotIn(removed, section)
        self.assertNotIn("dataManagement.webdavHint", self.ui)
        self.assertNotIn("dataManagement.syncNow", self.ui)
        self.assertNotIn("dataManagement.webdavScope", self.ui)
        # The probe sits beside the file it tests; the one-off run is its own row
        # under the switch it needs.
        self.assertNotIn('translate("dataManagement.testConnection")', section)
        self.assertIn('<NativeButton title={translate("dataManagement.testConnection")} busy={testing} disabled={saving}', form)
        self.assertIn('<NativeButton title={translate("webdav.syncNow")} busy={syncing} disabled={saving || !values.enabled}', form)
        # A 44 pt switch leaves no room for a button beside it: pushed together
        # the two read as one crowded control. The command therefore takes its
        # own row on the grid's control column, and no spacer moves it to the
        # row's far edge where it would read as the window's commit.
        self.assertIn('<WebDavField label={translate("webdav.enabled")}>\n          <NativeToggle', form)
        self.assertIn('<Text style={styles.webdavFieldLabel}>{""}</Text>\n            <NativeButton title={translate("webdav.syncNow")}', form)
        self.assertNotIn('webdavDialogSpacer', self.ui)
        self.assertNotIn('webdavFieldSpacer', self.ui)
        # A stopped run states the step that clears it, not just "failed": the
        # cause Core recorded picks the sentence, and the sentence names the
        # control (推送 / 拉取 / 测试连接) that clears it.
        self.assert_ui_has('const failureRemedy = webdavFailureRemedy(stringValue(lastResult.code));')
        self.assert_ui_has('function webdavFailureRemedy(code: string): TranslationKey {')
        self.assert_ui_has('{lastFailed && lastSyncAt !== "" ? <Text numberOfLines={1} style={styles.settingsActionRowFact}>{translate(failureRemedy)}</Text> : null}')
        self.assertIn('"webdav.remedy.conflict": "两端都有改动，请在设置里比较后保留其中一侧"', self.zh)
        self.assertIn('"webdav.remedy.incompatible": "远端文件由旧版本创建，下次同步会先另存一份再覆盖它"', self.zh)
        self.assertIn('"webdav.remedy.unsaved": "请先保存或重新加载本机的改动，再同步"', self.zh)
        self.assertIn('"webdav.remedy.disconnected": "连接失败，请在设置里测试连接"', self.zh)
        self.assertNotIn('translate("dataManagement.testConnection")} busy={testing} disabled={saving || busy} onPress={() => { void test(); }} />\n        <NativeButton title={translate("status.close")}', form)
        # The form is a child window: it is opened from the category, and every
        # control of it edits a draft that only 保存并关闭 writes.
        self.assert_ui_has('onOpenWebDavSettings={() => { native.window.open("webdav-settings"); native.window.focus("webdav-settings"); }}')
        self.assert_ui_has('{route === "webdav-settings" ? <WebDavSettingsPanel snapshot={snapshot} busy={busy} status={dataManagementStatuses.webdav} translate={translate} onSecretState={onSecretState} onFlushPendingFields={flushPendingFields} onProbe={probeWebDavDetails} onSave={saveWebDavDetails} onSync={syncWebDavAndReadState} onCompareConflict={compareWebDavConflict} onResolveConflict={resolveWebDavConflictAndReadState} onSetContentSize={(width, height) => { void native.window.setContentSize?.("webdav-settings", width, height); }} onClose={closeRoute} /> : null}')
        self.assert_ui_has('const saveWebDavDetails = (patch: UnknownRecord): Promise<boolean> => writeWebDavSettings(patch).then((applied) => applied !== undefined);')
        self.assert_ui_has('const patch = webDavDetailPatch(values, saved);')
        self.assert_ui_has('<NativeButton primary title={translate("status.saveAndClose")} busy={saving} disabled={busy || saving || syncing || !pendingWrite} onPress={() => { void save(); }} />')
        # The form holds the pane's auto-apply while it is open: its values are
        # one draft until 保存并关闭, so nothing of it may reach Core early.
        self.assert_ui_has('setWebDavSettingsOpen(true);')
        self.assert_ui_has('return isProviderWizardOpen() || isAssistantEditorOpen() || isGroupManagerOpen() || isWebDavSettingsOpen();')
        # 保存并关闭 flushes the field the user was typing — the native secure
        # field stages the password — and 关闭 drops whatever Core staged, so
        # neither button leaves half a draft behind.
        self.assert_ui_has('await onFlushPendingFields();')
        self.assert_ui_has('nothingToWrite = Object.keys(patch).length === 0 && !stagedSecret.current;')
        self.assert_ui_has('void enqueueDispatch("cancel", {}, "webdav").catch(() => undefined);')
        # 测试连接 tests the form's values, not the saved ones: the candidate
        # rides the probe request, so a test stages and saves nothing.
        # A probe is one action of this child surface: it keeps the window's
        # controls live, so the reader can close it or fix a field while the
        # server is being asked.
        self.assert_ui_has('const probeWebDavDetails = (values: UnknownRecord): Promise<void> => runDataManagement("webdav", () => ipc.probe(undefined, undefined, "webdav", values), (value) => {')
        # A reachable collection that takes no writes is not 连接正常: the folder
        # a reader picked decides whether a push can ever land there.
        self.assert_ui_has('return result.writable === false ? "webdav.probeReadOnly" : "webdav.probeOk";')
        self.assert_ui_has('const waiting = busy || saving;')
        self.assertNotIn('const waiting = busy || saving || testing || syncing;', self.ui)
        self.assert_ui_has('await onProbe(webDavDetailProbe(values));')
        self.assert_ui_has('function webDavDetailProbe(draft: WebDavDetailDraft): UnknownRecord {')
        # The tab's name rides the shared header band, so the retired 同步设置
        # heading left both the copy and the styles.
        self.assertNotIn("dataManagement.syncSettings", self.ui)
        self.assertNotIn("dataManagementPolishStyles.paneHeading", self.ui)
        self.assertNotIn('dataManagementGroupDivider', workspace)
        # The form was a stack of its own rows, its own two action lines, and
        # two style layers that cancelled each other's rules.
        for legacy in (
            "webdavFormBody:",
            "webdavFormRows:",
            "webdavStateRow:",
            "webdavStateStatus:",
            "webdavSyncArea:",
            "webdavActionRow:",
            "webdavActionStatus:",
            "dataManagementWebDavPane:",
            "dataManagementWebDavContent:",
            "dataManagementSyncContent:",
        ):
            self.assertNotIn(legacy, self.ui)
        self.assertNotIn('webdavActionSpacer:', self.ui)
        self.assertNotIn('webdavHeaderActions:', self.ui)
        # The enable row states the setting and nothing else: the connection
        # state is the category's own work, and the window's one strip reports it.
        self.assertNotIn("webdavMenuStatus(snapshot.service.webdav", form)
        # The form has one explicit write and no Apply button of its own.
        self.assertNotIn('title={translate("common.saveAndApply")}', form)
        self.assertNotIn("WEBDAV_SYNC_OPTIONS", form)
        self.assertNotIn("dataManagementSectionGrid:", self.ui)
        self.assertNotIn("footerCompact:", self.ui)
        self.assertNotIn("dataManagementRelayRow:", self.ui)

    def test_webdav_sync_is_one_operation_and_a_conflict_is_a_choice(self) -> None:
        """One sync, one scope, and a diverged run resolved by keeping a side."""

        for marker in (
            'const WEBDAV_SYNC_DOMAINS: readonly ConfigDomain[] = ["providers_models", "relay_accounts"];',
            'const runWebDavOperation = async (operation: () => Promise<unknown>, message: ResultMessage): Promise<void> => {',
            'if (webDavOperationInFlight.current) return;',
            'type, payload: { sections: [...WEBDAV_SYNC_DOMAINS] }',
            '<NativeToggle accessibilityLabel={translate("webdav.enabled")} value={values.enabled} disabled={waiting} onValueChange={(enabled) => update({ enabled })} style={styles.webdavFieldSwitch} />',
            '<NativeButton title={translate("webdav.syncNow")} busy={syncing} disabled={saving || !values.enabled} onPress={() => { void syncNow(); }} />',
            # The comparison reads the two sides through the versioned editor
            # lease: a document never travels in a snapshot.
            'const descriptor = await ipc.editor("webdav", document as "config" | "disabled_models" | "relay_accounts");',
            'return { local: descriptor.text, remote: descriptor.baseline };',
            # The diff is the editor's own, drawn read-only over the two texts.
            'const selectedText = selectedTexts?.local;',
            'const selectedBaseline = selectedTexts?.remote;',
            'value={selectedText}',
            'baseline={selectedBaseline}',
            'readOnly',
            'showDiff',
            # The choice is the footer's, and each button names its side.
            '<NativeButton primary title={translate("webdav.conflict.keepLocal")} busy={choosing === "local"}',
            '<NativeButton title={translate("webdav.conflict.keepRemote")} busy={choosing === "remote"}',
        ):
            self.assert_ui_has(marker)
        # The scope is every section the dispatch sends; it is not a field, so
        # no control can claim a narrower one.
        self.assertNotIn('dataManagement.webdavScope', self.ui)
        self.assertNotIn('syncScope', self.ui)
        # No saved direction survives anywhere in the shared UI or its copy.
        for source in (self.ui, self.en, self.zh):
            self.assertNotIn("sync_direction", source)
            self.assertNotIn("dataManagement.syncSmart", source)
            self.assertNotIn("dataManagement.syncPush", source)
            self.assertNotIn("dataManagement.syncPull", source)

    def test_native_toggles_size_themselves_instead_of_a_wrapper(self) -> None:
        """A native control owns its frame; a wrapper view cannot size it."""
        toggles = self.jsx_tags(self.ui, "NativeToggle")
        self.assertGreaterEqual(len(toggles), 5)
        for toggle in toggles:
            self.assertIn("style={", toggle)
        # Wrapping an unstyled native toggle in a sized view drew the switch a
        # row above its own label (the wrapper sized itself, the toggle kept
        # its natural frame), so the slot rides the control, never the view.
        self.assertNotIn("<View style={SETTINGS_FIELD_SWITCH_SLOT}><NativeToggle", self.ui)

    def test_text_fields_keep_active_typing_local_and_project_dependent_views(self) -> None:
        for marker in (
            "function usePendingTextField(",
            "setDirty(dirty);",
            "void commit().catch(() => undefined);",
            "void field.commit().catch(() => undefined);",
            "hasPendingFieldEdits())",
            "registry?.setDirty(fieldId.current, true);",
            "const SECRET_INPUT_COMMIT_DEBOUNCE_MS = 500;",
            "const TEXT_INPUT_COMMIT_DEBOUNCE_MS = 500;",
            "const secretDebounceTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);",
            "if (autoCommit) {",
            "}, SECRET_INPUT_COMMIT_DEBOUNCE_MS);",
            "state.status === \"saved\" || state.status === \"ready\" || state.status === \"error\"",
            "refreshAfter = true",
            "if (refreshAfter && !cancelled && postActionRefreshNeeded({ channel: eventChannel.current, revisionBefore, revisionAfter, localRevision: revision.current })) await refresh();",
            "}, null, true);",
            # Leaving a pane with nothing to write is not an Apply: the flush
            # used to cancel after a `staged` read and then read the whole
            # projection back for it, so every sidebar press paid two round trips
            # and a second render before the pane it asked for was drawn.
            "const paneHasNothingToWrite = (): boolean => dispatchGeneration.current === snapshotDispatchGeneration.current",
            "if (paneHasNothingToWrite()) return true;",
            "dispatchGeneration.current += 1;",
            "snapshotDispatchGeneration.current = dispatchGeneration.current;",
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
        self.assertIn("if (commitTimer.current !== undefined) clearTimeout(commitTimer.current);", pending_hook)
        self.assertIn("}, TEXT_INPUT_COMMIT_DEBOUNCE_MS);", pending_hook)
        self.assertIn("if (dirtyRef.current) void commit().catch(() => undefined);", pending_hook)
        self.assertIn("onDraftChange={onNameDraftChange}", self.ui)
        self.assertIn("drafts?.providerDisplayName(provider)", self.ui)
        self.assertIn("setProviderNameDraft", self.ui)
        self.assertIn("ProviderWorkspaceDraftContext", self.ui)
        self.assertIn("const [providerNameDrafts, setProviderNameDrafts] = useState<Record<string, string>>({});", self.ui)
        self.assertIn("function providerModelDraftKey(providerID: string, modelID: string)", self.ui)
        self.assertIn("key={`model:${providerId}:${editorIdentifier(model)}`}", self.ui)
        self.assertIn('const name = providerNameDrafts[id] !== undefined\n        ? providerNameDrafts[id]\n        : displayLabel(item.display_name, displayLabel(item.name, translate("providers.newProvider")));', self.ui)
        self.assertIn('return { key: id, cells: [name] };', self.ui)
        self.assertIn('const grouped = new Map<string, { label: string; models: UnknownRecord[] }>();', self.ui)
        self.assertIn(r'cells: [`\t${modelUpstreamDisplay(editorIdentifier(entry.provider), entry.model)', self.ui)
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
        self.assertIn("stationDraft?.name ?? stationDisplayName(station, translate)", self.ui)
        self.assertIn("value={stationDraft?.origin ?? station.origin}", self.ui)
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
        # The route trace names the same wires in both languages.  The word
        # around it may be translated; the protocol may not, or two readers of
        # one log disagree about which endpoint answered.
        for catalog in (english, chinese):
            self.assertIn('"logs.routeTrace.protocolResponses": "Responses"', catalog)
            self.assertIn('"logs.routeTrace.protocolChat": "Chat"', catalog)
            self.assertIn('"logs.routeTrace.protocolMessages": "Messages"', catalog)
        # ...and the event words that already carried the protocol in prose.
        self.assertNotIn('"logs.routeEvent.responses": "响应接口处理"', chinese)
        self.assertNotIn('"logs.routeEvent.chatBridge": "切换聊天协议"', chinese)

    def test_route_trace_merges_one_route_even_when_a_record_omits_the_wire(self) -> None:
        """A record without a wire must refine, never split, its route's step.

        The selection and the success of one attempt come from different hooks,
        and only some of them know which wire the attempt left on.  Merging on
        the rendered label turned that gap into a second step for the same
        route, and the second one carried the first's failure ink.
        """

        for marker in (
            "identity: string;",
            "const existing = attempts.find((candidate) => candidate.identity === attempt.identity);",
            "if (attempt.label !== attempt.identity && existing.label === existing.identity) {",
            "label: `${route}${routeSurfaceSuffix(value.route, translate)}`,",
            "label: `${failedRoute}${routeSurfaceSuffix(value.failed_route, translate)}`,",
        ):
            self.assert_ui_has(marker)
        # Only an attempt-written ``surface`` is evidence about the wire; the
        # route's policy and its declared backup protocol are not.
        self.assert_ui_has("const surface = compactLogValue(asRecord(value).surface);")
        self.assertNotIn("compactLogValue(route.surface) || compactLogValue(route.protocol_mode)", self.ui)

    def test_external_editor_is_a_native_sheet_not_an_overlay(self) -> None:
        # The editor is presented by the host (macOS sheet, Windows route), so
        # the shared UI keeps no dimmed overlay or in-window dialog for it.
        for removed in ("editorLayer", "editorDialog", "nativeSheetCornerRadius"):
            self.assertNotIn(removed, self.ui)
        self.assertIn("fileEditorRouteContent:", self.ui)
        self.assertIn("assistantFileSurfaceStyles.editorRoute", self.ui)

    def test_codex_raw_editors_share_height_and_follow_disk_generation(self) -> None:
        for marker in (
            "showLabel={false} showDiff codexPane deferStage={Platform.OS === \"windows\"} onDirtyChange={handleEditorDirtyChange} syncRevision={Platform.OS === \"windows\" ? undefined : syncRevision} style={assistantFileSurfaceStyles.editorRouteRaw}",
            "<SettingsActionRow",
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
        mac_button = (ROOT / "rn/packages/shared/src/ui/macos/NativeButtonNativeComponent.ts").read_text(encoding="utf-8")
        mac_native = (ROOT / "rn/apps/macos/src/native/macos/AppKitControlViews.mm").read_text(encoding="utf-8")
        self.assertIn("link?: WithDefault<boolean, false>;", mac_button)
        self.assertIn("NSBezelStyleInline", mac_native)
        self.assertIn("windowsButtonLinkTitle: { textDecorationLine: \"underline\" }", self.native_controls)

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
        # A refused write no longer draws an issue card over the pane: the
        # strip states the outcome and the offending row is marked in place.
        self.assert_ui_not_has("function IssueList(")
        self.assert_ui_not_has('setIssues(summary.issues);')
        # A rejected draft names the entries that block it: the strip carries the
        # first issues instead of a sentence that says only that something is wrong.
        self.assertIn('publishResult(value.valid ? translate("common.saved") : (validationSummaryMessage(value, translate) ?? translate("error.validationFailed")));', self.ui)
        self.assertIn("function validationSummaryMessage(summary: unknown, translate: Translate): string | undefined {", self.ui)

        for locale in (self.zh, self.en):
            self.assertIn('"validation.modelNameRequired"', locale)
            self.assertIn('"validation.modelUpstreamRequired"', locale)

        # The row that needs the name is marked in place, so the pane points at
        # the entry instead of asking the user to hunt for it.
        self.assertIn("function modelNeedsAttention(model: UnknownRecord, translate: Translate): boolean {", self.ui)
        self.assertIn(
            "() => modelRowKeys(models.filter((item) => modelNeedsAttention(item, translate) || Boolean(bindingIssueFor(item))).map(editorIdentifier)),",
            self.ui,
        )
        # A freshly created row is enabled and still carries the placeholder
        # name: the pane marks it until the user replaces the name.
        self.assertIn("if (isDraftModel(model, translate)) return true;", self.ui)
        self.assertIn("alertRowKeys={alertModelKeys}", self.ui)
        provider_list = self.ui.split("const ProviderListTable = React.memo(", 1)[1].split("function ProviderWorkspace(", 1)[0]
        self.assertIn("const alertRowKeys = useMemo(", provider_list)
        self.assertIn("|| asRecords(item.models).some((model) => modelNeedsAttention(model, translate) || bindingIssueRow(bindingIssues, model) !== undefined)).map(editorIdentifier),", provider_list)
        self.assertIn("alertRowKeys={alertRowKeys}", provider_list)

        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn("def _entry_issues(self, providers: object) -> list[dict[str, Any]]:", domain)
        self.assertIn('"code": "model_name_required",', domain)
        self.assertIn('"code": "model_upstream_required",', domain)
        self.assertIn('"code": "model_provider_key_missing",', domain)
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
        # An account-backed row takes ＋ too: a model added there names the
        # account it answers with, so the row is a first-class provider and not
        # a read-only projection of what the account happened to list.
        self.assertIn(
            '{provider ? <IconButton label="+" title={translate("providers.newModel")} disabled={busy} onPress={addModel} /> : null}',
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
            'const canDeleteKey = Boolean(selectedCustom) || Boolean(selectedOrphan) || Boolean(selectedProvided && !autoGrouping);',
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
        # The inline body measures itself once per layout: its list column is
        # the width the editor column beside it leaves, not a number the panel
        # invents (a 264 pt column in a 174 pt frame made the row itself the
        # horizontal scroller).
        self.assertIn("{tableRows.length > 0 ? <View style={styles.keysInlineBody} onLayout=", self.ui)
        self.assertIn("setInlineKeysListWidth(Math.max(1, nativeEvent.layout.width - KEYS_INLINE_EDITOR_WIDTH - 6))", self.ui)
        # The keys panel no longer measures the pane: its list height is fixed.
        self.assertNotIn("paneViewportHeight", self.ui)
        self.assertNotIn("paneContentHeight", self.ui)
        self.assertIn('const vendorBaseURL = (drafts?.providerBaseURL(provider) ?? stringValue(provider.endpoint, stringValue(provider.api_base))).trim();', self.ui)
        self.assertIn(
            '{vendorBaseURL && !providerService(provider) ? <NativeButton title="" symbol="plus" compact toolTip={translate("providers.addRelayAccount")}',
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

    def test_a_click_below_a_list_keeps_the_row_the_list_had_selected(self) -> None:
        # A click on the empty space below the rows names no row, so it is not a
        # selection: every list keeps the row it had.  Following the cleared
        # state emptied the pane beside the list instead — no highlighted row, no
        # editor, and nothing on screen saying which click would bring it back.
        self.assertIn(
            'const provider = useMemo(\n    () => providers.find((item) => editorIdentifier(item) === selectedProvider) ?? providers[0],\n    [providers, selectedProvider],\n  );',
            self.ui,
        )
        self.assertNotIn('if (selectedProvider === "") return;', self.ui)
        # The keys panel keeps its key: an empty key clears nothing, the repair
        # below puts a row back.
        self.assertNotIn('selectionCleared', self.ui)
        # The station's account list falls back to the first account instead of
        # keeping a cleared selection.
        self.assertIn(
            'const selected = stationAccounts.find((account) => account.id === selectedID)\n    ?? stationAccounts[0];',
            self.relay,
        )
        self.assertNotIn('if (selectedID === "") return;', self.relay)
        # The routes table is two levels deep, so a public-model row is a
        # selection of its own; a list that names no route takes the first row
        # rather than emptying the inspector beside it.
        self.assertIn('if (!routeId) return;', self.ui)
        self.assertNotIn('selectedRoute === ""', self.ui)
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
        self.assertIn("setKeyName(creatingProvider ? stagedKeyName : randomKeyName([]));", self.ui)
        self.assertIn("const [stagedKeyName] = useState(() => randomKeyName([]));", self.ui)
        self.assertIn("const name = uniqueKeyName(customKeyNames);", self.ui)
        self.assertNotIn("randomWordName", self.ui)
        self.assertNotIn("RANDOM_NAME_WORDS", self.ui)

        leaf = (ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift").read_text(encoding="utf-8")
        windows = (ROOT / "rn/apps/windows/src/native/windows/WinUI3NativeLeaf.cpp").read_text(encoding="utf-8")
        self.assertIn('let base = label("newKeyName", "New key")', leaf)
        self.assertIn(r'name = "\(base) \(suffix)"', leaf)
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
        self.assertIn('runMutationOnce("provider.add", () => dispatch("provider.add", { provider: { name, models: [], enabled: true } }));', self.ui)
        self.assertIn('const pendingProviderIds = useRef<Set<string> | undefined>(undefined);', self.ui)
        self.assertIn(
            '<ActionButton title={translate("providers.addWizard")} titleWidth="tight" disabled={busy} onPress={onOpenWizard} />',
            self.ui,
        )
        # The toolbar action carries no pane-specific width: NativeButton applies
        # its own title-width style after the caller's, so an override here only
        # read as a width that was never used.
        self.assertNotIn("providerWizardToolbarButton", self.ui)
        # A command that opens another surface ends in a real ellipsis, not three
        # full stops, so Windows draws one glyph instead of three spaced periods.
        self.assertIn('"providers.addWizard": "添加向导…"', self.zh)
        self.assertIn('"providers.addWizard": "Add with wizard…"', self.en)
        self.assertNotIn('"providers.addWizard": "添加向导..."', self.zh)

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
            'return relaySourceName(relaySourceForKey(key, relaySources)) || name;',
            self.ui,
        )
        self.assertIn('if (!key) return displayLabel(model.api_key_name, translate("providers.undefinedKey"));', self.ui)
        # The list matches the key the way the editor does, so a moved slot id
        # cannot leave the list under "undefined key" while the editor shows it
        # — and a name resolves the key only while one key carries it, so a
        # station key is never read as a custom key of the same name.
        self.assertIn("const keyStates = providerKeyStates(provider);", self.ui)
        self.assertIn('const keyName = stringValue(model.api_key_name);', self.ui)
        self.assertIn('const byID = keyStates.find((entry) => entry.id === keyID);', self.ui)
        self.assertIn('return byName.length === 1 ? byName[0] : undefined;', self.ui)
        self.assertIn(
            'value={selectedProviderKey?.id ?? ""} values={[{ value: "", label: translate("providers.undefinedKey") }, ...providerKeyOptions]} disabled={busy} onSelect={selectProviderKey}',
            self.ui,
        )
        # The editable field carries the model's real value, so a blank name
        # cannot look like it was set while validation still reports it
        # missing; the localized placeholder labels the rows and the panes that
        # name a model without editing it.
        self.assertIn("const modelFieldName = stringValue(model.model_name, stringValue(model.name));", self.ui)
        self.assertIn("value={modelFieldName}", self.ui)
        self.assertIn('displayLabel(model.model_name ?? model.name, translate("providers.unnamedModel"))', self.ui)
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

        # An unbound model still answers with the provider's default credential,
        # so the draft keeps working at runtime — and the write records the
        # route's own “no key” choice, so reading the file back cannot turn that
        # credential into a claim on the key that happens to carry it.
        dumper = (ROOT / "young_router/config/dump.py").read_text(encoding="utf-8")
        self.assertIn("elif not key_name and not provider_key_id:", dumper)
        self.assertIn('api_key = str(model.get("api_key", "")).strip() or (keys[0]["value"] if keys else "")', dumper)
        self.assertIn('model_info[MENU_KEY_BINDING_KEY] = "unbound"', dumper)
        loader = (ROOT / "young_router/config/load.py").read_text(encoding="utf-8")
        self.assertIn("def _is_unbound_entry(model_info: dict[str, Any]) -> bool:", loader)
        self.assertIn("if _is_unbound_entry(model_info):", loader)

    def test_route_actions_ask_before_renumbering_and_offer_add_and_delete(self) -> None:
        """The routes pane's own buttons: ＋/− beside ↑/↓, and a decimal question.

        A move keeps the numbers the group carries (they travel with the routes),
        so a group holding decimals is asked once whether it should become 1..n —
        the pane never renumbers a rate-ordered group behind the user's back.  The
        pane also offers the same ＋/− shape the providers pane uses.
        """

        self.assertIn("const routeOrderValues = activeRouteGroup.map((entry) => modelOrderValue(editorIdentifier(entry.provider), entry.model));", self.ui)
        # One multiplier route in the group locks the whole group's hand
        # ordering, exactly as Core refuses the rewrite: the pane reads the
        # group, not only the selected row, before it offers ↑/↓.
        self.assertIn("const activeRouteGroupUsesMultiplier = activeRouteGroup.some((entry) => modelOrderMode(entry.model) === \"relay_multiplier\");", self.ui)
        self.assertIn('const routeMoveTitleKey = activeRouteGroupUsesMultiplier ? "providers.reorderMultiplierLocked" : undefined;', self.ui)
        self.assertIn("const canMoveRouteUp = !activeRouteGroupUsesMultiplier && activeRouteIndex > 0;", self.ui)
        self.assertIn("if (!activeRoute || activeRouteIndex < 0 || activeRouteGroupUsesMultiplier) return;", self.ui)
        # A neighbour with the same number cannot trade it, so the press states
        # that instead of dispatching a permutation that changes nothing.
        self.assertIn('if (routeOrderValues[activeRouteIndex] === routeOrderValues[targetIndex]) {', self.ui)
        self.assertIn('onStatus(translate("providers.reorderSameOrder", { order: String(routeOrderValues[activeRouteIndex]) }));', self.ui)
        self.assertIn("const decimals = routeOrderValues.filter((value) => !Number.isInteger(value));", self.ui)
        self.assertIn('translate("providers.reorderIntegerMessage", { orders: decimals.join(translate("providers.orderListSeparator")) })', self.ui)
        # 取消移动 dismisses the move itself: only 改为整数 renumbers and moves,
        # so a decimal group keeps its order when the question is declined.
        self.assertIn('cancelLabel: translate("providers.reorderCancelMove"),', self.ui)
        self.assertIn("if (renumber) void reorder(true);", self.ui)
        self.assertIn("...(renumber ? { renumber: true } : {})", self.ui)
        # No question when every value is already an integer.
        self.assertIn("if (decimals.length === 0) {", self.ui)

        self.assertIn('<IconButton label="+" title={translate("providers.newRoute")} disabled={busy || providers.length === 0} onPress={addRoute} />', self.ui)
        self.assertIn('<IconButton label="−" title={translate("common.delete")} disabled={busy} onPress={confirmDeleteRoute} />', self.ui)
        self.assertIn("const addRoute = (): void => {", self.ui)
        self.assertIn('runMutationOnce(`route.add:${targetProviderID}:${publicModel}`, () => dispatchWithOutcome("model.add", {', self.ui)
        self.assertIn("const confirmDeleteRoute = (): void => {", self.ui)
        self.assertIn('runMutationOnce(`route.delete-group:${selectedGroup.name}`, () => native.showConfirmation({', self.ui)
        self.assertIn('runMutationOnce(`route.delete:${activeRoute.key}`, () => native.showConfirmation({', self.ui)
        self.assertIn('dispatch("model.delete", { provider_id: routeProviderID, model_id: routeModelID })', self.ui)
        # A selected public model deletes its whole group in one action.
        self.assertIn("const selectedGroup = selectedPublicModel !== undefined ? routeGroups.find((group) => publicModelKey(group.name) === publicModelKey(selectedPublicModel)) : undefined;", self.ui)
        self.assertIn('dispatch("public.model_delete", { public_model: selectedGroup.name })', self.ui)
        self.assertIn('translate("providers.deletePublicModelMessage", { routes: selectedGroup.entries.length })', self.ui)
        self.assertIn('{activeRoute || selectedPublicModel !== undefined ? <IconButton label="−"', self.ui)
        for locale in (self.zh, self.en):
            self.assertIn('"providers.deletePublicModel"', locale)
            self.assertIn('"providers.deletePublicModelMessage"', locale)
        domain = (ROOT / "young_router/core/domains/providers_models.py").read_text(encoding="utf-8")
        self.assertIn("def _delete_public_model(self, data: Mapping[str, Any]) -> None:", domain)
        self.assertIn('"public_model_delete", "public_models_delete", "delete_public_model"', domain)
        # The new row joins the group the user is looking at, as a disabled draft,
        # and states the group's own name rather than a second spelling of it.
        self.assertIn("const publicModel = (selectedPublicModel ?? activePublicGroupName).trim();", self.ui)
        self.assertIn("const knownModelIds = new Set(targetModels.map(editorIdentifier));", self.ui)
        # The new row is selected through the pending key, never by a key the
        # routes list does not carry yet (that would be repaired to row one).
        self.assertIn("const pendingRouteKey = useRef<string | undefined>(undefined);", self.ui)
        self.assertIn("pendingRouteKey.current = `${targetProviderID}:${stringValue(added.editor_id, stringValue(added.deployment_id, identifier(added))).trim()}`;", self.ui)
        self.assertIn("name,\n        upstream_model: publicModel || \"\",\n        enabled: false,\n        order: 0,", self.ui)

        for locale in (self.zh, self.en):
            for key in (
                '"providers.newRoute"',
                '"providers.deleteRoute"',
                '"providers.deleteRouteMessage"',
                '"providers.reorderIntegerTitle"',
                '"providers.reorderIntegerMessage"',
                '"providers.reorderIntegerConfirm"',
                '"providers.reorderCancelMove"',
                '"providers.orderListSeparator"',
                '"providers.reorderMultiplierLocked"',
                '"providers.reorderSameOrder"',
            ):
                self.assertIn(key, locale)

        # One route is one designate row, and an ambiguous name decides nothing:
        # two routes of one public name stay two menu entries (named by the key
        # they answer with), and a client whose provider this pane cannot match
        # is never moved onto another provider's route.
        self.assertIn(r"const identity = `${row.provider}\u001f${row.deploymentId}`;", self.ui)
        self.assertIn(r'`${row.model} · ${row.keyName || translate("providers.undefinedKey")}`', self.ui)
        # An ambiguous name resolves to nothing: Core answers a client's own
        # model back to one saved route only when exactly one route carries it,
        # and the pane's off direction uses that same answer.
        domain = (ROOT / "young_router/core/domains/_shared_api.py").read_text(encoding="utf-8")
        self.assertIn("    if len(candidates) != 1:", domain)
        self.assertIn("def _route_named(self, model: str) -> dict[str, str] | None:", (ROOT / "young_router/core/domains/claude.py").read_text(encoding="utf-8"))
        core = (ROOT / "young_router/core/codex_config.py").read_text(encoding="utf-8")
        self.assertIn('"api_key_name": str(info.get("api_key_name") or "").strip(),', core)
        self.assertIn('"order": params.get("order"),', core)

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
        self.assert_ui_has('title={translate("webdav.syncNow")}')
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
        self.assertIn('message: ResultMessage = "common.saved"', self.ui)
        self.assertIn('options?.message ?? "common.saved"', self.ui)
        self.assertIn('translate(appliedKey ?? "common.saved")', self.ui)
        self.assertIn('}, "common.reloaded");', self.ui)
        for text in (english, chinese):
            self.assertIn('"common.saved":', text)
            self.assertIn('"common.reloaded":', text)
        # A rejected live write names the fix, not an action that does not
        # exist, and Core reports the missing key value as a coded location so
        # the pane can point at the provider row it has to open.
        # A rejected draft names the entries that block it: the strip carries the
        # first issues instead of a sentence that says only that something is wrong.
        self.assertIn('publishResult(value.valid ? translate("common.saved") : (validationSummaryMessage(value, translate) ?? translate("error.validationFailed")));', self.ui)
        self.assertIn("function validationSummaryMessage(summary: unknown, translate: Translate): string | undefined {", self.ui)
        self.assertIn('"error.validationFailed": "Validation failed; changes not applied."', english)
        self.assertIn('"error.validationFailed": "校验未通过，更改未生效。"', chinese)
        # The code is carried to a translated sentence: a missing key value is
        # stated in the user's own words on the row that has to open.
        # The code reaches a translated sentence through the one mapper, which
        # builds the token from its parts so the literal never appears beside a
        # user-visible string: a missing key value is stated in the user's own
        # words on the row that has to open.
        self.assertIn('function validationIssueMessageKey(code: string): string | undefined {', self.ui)
        self.assertIn('case ["api", "key", "value", "required"].join("_"): return "validation.apiKeyValueRequired";', self.ui)
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

    def test_macos_leaf_localizes_window_titles_and_keeps_no_status_menu(self) -> None:
        # Settings panes share one window, so its title is the app name and the
        # sidebar selection names the active pane.
        self.assertIn('if Self.settingsPaneRoutes.contains(canonicalRoute(route)) {', self.macos_leaf)
        self.assertIn('return localized("appTitle", fallback: "Young Router")', self.macos_leaf)
        # The window title is the shared route word alone: the app's own name is
        # the branded surface, so the title bar does not carry a second brand
        # ahead of the provider wizard's name.
        self.assertIn('case "provider-wizard": return localized("routeProviderWizard", fallback: "Add Provider")', self.macos_leaf)
        # macOS no longer draws a status-item menu, so there is no order to
        # keep: either mouse button opens the shared settings window.
        self.assertNotIn("statusMenuOrder", self.macos_leaf)
        self.assertNotIn("makeMenu", self.macos_leaf)

    def test_webdav_detail_form_is_a_child_window_of_the_sync_tab(self) -> None:
        # WebDAV stays a tab of Backup & Sync, never a sidebar pane: the tab's
        # full form is the one child window it opens, so the route exists
        # without ever becoming a menu entry or a settings pane, and it keeps
        # no status strip of its own.
        routes = (ROOT / "rn/packages/shared/src/routes.ts").read_text(encoding="utf-8")
        types = (ROOT / "rn/packages/shared/src/types.ts").read_text(encoding="utf-8")
        macos_app = (ROOT / "rn/apps/macos/macos/YoungRouter-macOS/AppDelegate.mm").read_text(encoding="utf-8")
        windows_app = (ROOT / "rn/apps/windows/windows/YoungRouter/YoungRouter.cpp").read_text(encoding="utf-8")
        for source in (self.ui, routes, types, self.macos_leaf, self.platform_entry, self.windows_leaf, macos_app, windows_app):
            self.assertNotIn('"open-webdav-settings"', source)
            self.assertNotIn("routeWebdavSettings", source)
        self.assertIn('{ id: "webdav-settings", titleKey: "webdav.detailsTitle" }', routes)
        # A child route is no pane: it is filtered out of the sidebar rail, the
        # menu actions, and the shared settings shell.
        self.assertIn('id !== "provider-wizard" && id !== "webdav-settings"', routes)
        self.assertIn('route !== "file-editor" && route !== "webdav-settings"', routes)
        self.assertIn('| "webdav-settings"', types)
        self.assertIn("routeWebDavSettings: string;", types)
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
        # The child window is presented and titled by both hosts, and both
        # spawn it beside the workspace it belongs to.
        self.assertIn('case "webdav-settings": return localized("routeWebDavSettings", fallback: "WebDAV Sync Settings")', self.macos_leaf)
        self.assertIn('if (route == L"webdav-settings") return Localized("routeWebDavSettings", L"WebDAV Sync Settings");', self.windows_leaf)
        self.assertIn('@[@"home", @"provider-wizard", @"file-editor", @"webdav-settings"]', macos_app)
        self.assertIn('route == L"webdav-settings"', windows_app)
        # Both hosts present it the way every child surface is presented, so
        # the pane behind it is locked until the form closes.
        self.assertIn('windowRoute == "webdav-settings"', self.macos_leaf)
        self.assertIn('native.window.open("webdav-settings")', self.ui)

    def test_language_is_one_setting_in_the_general_pane(self) -> None:
        """One control for one preference, in the pane that owns preferences.

        The three choices used to exist twice: a menu-bar submenu on each host
        and (from this change) a settings row.  Two surfaces for one stored
        value is two places to disagree, and the menu copy also carried a second
        localization table of its own.  The row is the surface now; both hosts
        draw it because it is the shared pane.
        """
        for marker in (
            'const LANGUAGE_PREFERENCES: readonly LanguagePreference[] = ["system", "en", "zh-Hans"];',
            '<Text style={SETTINGS_FIELD_LABEL}>{translate("general.language")}</Text>',
            '{ domain: "language", type: "set_language", payload: { language: next } }',
            'await ipc.apply("language", staged.revision)',
            # The pane owns the domain it writes, so the shell's debounced Apply
            # flushes a staged language choice like every other field.
            'return (["runtime", "language"] as const).filter((name) => currentSnapshot?.drafts[name]?.dirty);',
        ):
            self.assert_ui_has(marker)
        for absent in (
            "language-picker",
            "set-language-system",
            "set-language-en",
            "set-language-zh-Hans",
            "installLanguageMenu",
            "language-settings",
            "function LanguageScreen",
        ):
            self.assertNotIn(absent, self.ui)
            self.assertNotIn(absent, self.macos_leaf)
        self.assertNotIn("language_menu", self.windows_leaf)

    def test_service_lifecycle_stays_in_the_shared_menu_surface(self) -> None:
        """Lifecycle labels and state-specific enablement are shared by AppKit and WinUI."""
        for marker in (
            # The callback reads the latest snapshot through a ref rather than a
            # closure dependency, so its identity stays stable across polls.
            'receiveSnapshot({ ...current, service: { ...current.service, state: "starting" } });',
            'const serviceState = snapshot.service.state;',
            'const serviceStartAvailable = !serviceOperationPending && serviceState === "stopped";',
            'const serviceRestartAvailable = !serviceOperationPending && serviceState !== "unknown";',
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
