import Foundation
import AppKit

#if canImport(React)
import React

@objc(LiteLLMNativeLeaf)
final class AppKitNativeLeafModule: RCTEventEmitter {
    private let leaf = AppKitNativeLeaf.shared
    private var observing = false
    private var pendingActions: [String] = []

    override init() {
        super.init()
        leaf.fileCapabilityRegistrar = { url, purpose, completion in
            CoreIPCBridge.shared.registerFileCapability(url, purpose: purpose, completion: completion)
        }
        leaf.menuActionHandler = { [weak self] action in
            DispatchQueue.main.async {
                self?.emit(action)
            }
        }
    }

    override static func requiresMainQueueSetup() -> Bool {
        true
    }

    @objc override var methodQueue: DispatchQueue! {
        DispatchQueue.main
    }

    override func supportedEvents() -> [String]! {
        ["menuAction"]
    }

    override func startObserving() {
        observing = true
        let queued = pendingActions
        pendingActions.removeAll()
        queued.forEach { sendEvent(withName: "menuAction", body: $0) }
    }

    override func stopObserving() {
        observing = false
    }

    private func emit(_ action: String) {
        if observing {
            sendEvent(withName: "menuAction", body: action)
        } else {
            pendingActions.append(action)
        }
    }

    @objc func openWindow(_ route: String) {
        leaf.open(route: route)
    }

    @objc func closeWindow(_ route: String?) {
        leaf.close(route: route)
    }

    @objc func focusWindow(_ route: String) {
        leaf.open(route: route)
    }

    @objc func cancelRelayLogin() {
        leaf.cancelRelayLogin()
    }

    @objc(setWindowContentSize:width:height:resolver:rejecter:)
    func setWindowContentSize(
        _ route: String,
        _ width: NSNumber,
        height: NSNumber,
        resolver resolve: RCTPromiseResolveBlock,
        rejecter reject: RCTPromiseRejectBlock
    ) {
        resolve(leaf.setWindowContentSize(route: route, width: width.doubleValue, height: height.doubleValue))
    }

    @objc func setMenuBarStatus(_ title: String, running: Bool) {
        leaf.setStatus(title: title, running: running)
    }

    @objc func setMenuBarActions(_ actions: [[String: Any]]) {
        leaf.setMenuActions(actions)
    }

    @objc func setTrayStatus(_ title: String, running: Bool) {
        leaf.setStatus(title: title, running: running)
    }

    @objc func setTrayActions(_ actions: [[String: Any]]) {
        leaf.setMenuActions(actions)
    }

    @objc func openFilePicker(_ purpose: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            self.leaf.chooseImportFile(purpose: purpose) { token in resolve(token) }
        }
    }

    @objc func saveFilePicker(_ suggestedName: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            self.leaf.chooseExportFile(suggestedName: suggestedName) { token in resolve(token) }
        }
    }

    @objc func showConfirmation(_ title: String, message: String, confirmLabel: String, cancelLabel: String, destructive: Bool, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            // The app's own decision panel, never an alert's modal session: the
            // React host keeps running while the question is on screen, and the
            // answer that cannot be undone draws destructive.  The dismissing
            // answer keeps the caller's own words when the question means more
            // by 取消 than "no answer" (a move that would be cancelled, not a
            // question left unanswered), and falls back to the host's localized
            // 取消 when the caller names nothing.
            let cancelTitle = cancelLabel.trimmingCharacters(in: .whitespacesAndNewlines)
            self.leaf.presentDecisionPanel(
                title,
                message: message,
                answers: [
                    NativeDecisionAnswer(
                        id: "cancel",
                        title: cancelTitle.isEmpty ? self.leaf.localizedText("cancel", fallback: "Cancel") : cancelTitle,
                        isCancel: true
                    ),
                    NativeDecisionAnswer(
                        id: "confirm",
                        title: confirmLabel,
                        isDefault: true,
                        isDestructive: destructive
                    ),
                ]
            ) { answer in
                resolve(answer == "confirm")
            }
        }
    }

    @objc(showGroupManager:resolver:rejecter:)
    func showGroupManager(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: ["title", "accountLabel", "accountId", "groups", "keys", "labels", "autoGrouping", "loading"]),
              let title = options["title"] as? String,
              let accountLabel = options["accountLabel"] as? String,
              let accountID = options["accountId"] as? String,
              !accountID.isEmpty,
              accountID.count <= 256,
              let labels = options["labels"] as? [String: String],
              let autoGrouping = options["autoGrouping"] as? Bool,
              let groups = groupManagerGroups(options),
              let keys = groupManagerKeys(options) else {
            reject("E_NATIVE_GROUP_INPUT", "The group manager input is invalid.", nil)
            return
        }
        // The sheet appears before its account facts are loaded: while
        // `loading` is set it shows the rows it opened on behind its loading
        // line, and `updateGroupManager` hands it the aligned draft.
        let loading = (options["loading"] as? Bool) ?? false
        // The sheet is created in this same main-thread call, so an update the
        // shared UI pushes right after this request always finds it open.  The
        // leaf keeps its own main-thread guard for every other caller.
        self.leaf.showGroupManager(
            title: title,
            accountLabel: accountLabel,
            accountID: accountID,
            groups: groups,
            keys: keys,
            labels: labels,
            autoGrouping: autoGrouping,
            loading: loading
        ) { result in
            guard let result else {
                resolve(nil)
                return
            }
            resolve([
                "autoGrouping": result.autoGrouping,
                "creates": result.creates.map { ["name": $0.name, "groupID": $0.groupID] },
                "updates": result.updates.map {
                    ["keyID": $0.keyID, "name": $0.name, "groupID": $0.groupID, "enabled": $0.enabled]
                },
                "deletes": result.deletes,
            ])
        }
    }

    /// The sheet's group entries: the id, the picker label that carries the
    /// rate, the group name the list column shows, and the rate on its own.
    /// Every field is bounded like the rest of the native payloads.
    private func groupManagerGroups(_ options: [String: Any]) -> [[String: String]]? {
        guard let entries = options["groups"] as? [[String: String]],
              entries.count <= 512,
              entries.allSatisfy({
                  !($0["label"] ?? "").isEmpty && ($0["label"]?.count ?? 0) <= 256 && ($0["id"]?.count ?? 0) <= 256
                      && ($0["name"]?.count ?? 0) <= 256 && ($0["rate"]?.count ?? 0) <= 64
              }) else { return nil }
        return entries.map {
            ["id": $0["id"] ?? "", "label": $0["label"] ?? "", "name": $0["name"] ?? "", "rate": $0["rate"] ?? ""]
        }
    }

    /// The sheet's key entries: one row per station key with the group it
    /// belongs to, its rate, Core's credential-presence sentinel, and the
    /// models the station reports for it.  The same rows serve the sheet's
    /// opening request and its later update.
    private func groupManagerKeys(_ options: [String: Any]) -> [[String: String]]? {
        guard let entries = options["keys"] as? [[String: Any]],
              entries.count <= 512,
              entries.allSatisfy({
                  let name = ($0["name"] as? String) ?? ""
                  let id = ($0["id"] as? String) ?? ""
                  return !name.isEmpty && name.count <= 256 && id.count <= 256
              }) else { return nil }
        return entries.map {
            let models = ($0["models"] as? [String]) ?? []
            return [
                "id": ($0["id"] as? String) ?? "",
                "name": ($0["name"] as? String) ?? "",
                "groupID": ($0["groupID"] as? String) ?? "",
                "groupLabel": ($0["groupLabel"] as? String) ?? "",
                "multiplier": ($0["multiplier"] as? String) ?? "",
                "hint": ($0["hint"] as? String) ?? "",
                // The sheet lays the models out itself, so a long list travels
                // as one name per line and is bounded like every other field.
                "models": models.prefix(256).joined(separator: "\n"),
                "enabled": (($0["enabled"] as? Bool) ?? true) ? "1" : "0",
            ]
        }
    }

    /// Replace the open sheet's content with a later snapshot of the same
    /// account and end its loading state, so the sheet never holds the provider
    /// window closed for the station round trip its rows depend on.  Resolves
    /// false when no sheet is open: a load that lands after Close changes
    /// nothing.
    @objc(updateGroupManager:resolver:rejecter:)
    func updateGroupManager(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: ["accountLabel", "groups", "keys", "autoGrouping"]),
              let accountLabel = options["accountLabel"] as? String,
              let autoGrouping = options["autoGrouping"] as? Bool,
              let groups = groupManagerGroups(options),
              let keys = groupManagerKeys(options) else {
            reject("E_NATIVE_GROUP_INPUT", "The group manager input is invalid.", nil)
            return
        }
        resolve(self.leaf.updateGroupManager(
            accountLabel: accountLabel,
            groups: groups,
            keys: keys,
            autoGrouping: autoGrouping
        ))
    }

    /// The next 保存并关闭 request from the open sheet: the sheet stays up while
    /// the shared UI writes and applies the edits it handed over, and states the
    /// outcome in its own status strip.
    @objc(awaitGroupManagerApply:rejecter:)
    func awaitGroupManagerApply(
        _ resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        self.leaf.awaitGroupManagerApply { result in
            guard let result else {
                resolve(nil)
                return
            }
            resolve([
                "autoGrouping": result.autoGrouping,
                "creates": result.creates.map { ["name": $0.name, "groupID": $0.groupID] },
                "updates": result.updates.map {
                    ["keyID": $0.keyID, "name": $0.name, "groupID": $0.groupID, "enabled": $0.enabled]
                },
                "deletes": result.deletes,
            ])
        }
    }

    /// Answer a handed-over save: the sheet states the result in its own status
    /// strip and closes, or keeps its rows for another try.
    @objc(finishGroupManagerApply:resolver:rejecter:)
    func finishGroupManagerApply(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: ["status", "close"]),
              let status = options["status"] as? String,
              status.count <= 256,
              let close = options["close"] as? Bool else {
            reject("E_NATIVE_GROUP_INPUT", "The group manager answer is invalid.", nil)
            return
        }
        self.leaf.finishGroupManagerApply(status: status, close: close)
        resolve(nil)
    }

    @objc(showCodexRestartConfirmation:message:restartLabel:laterLabel:resolver:rejecter:)
    func showCodexRestartConfirmation(
        _ title: String,
        message: String,
        restartLabel: String,
        laterLabel: String,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: RCTPromiseRejectBlock
    ) {
        DispatchQueue.main.async {
            self.leaf.showCodexRestartConfirmation(
                title: title,
                message: message,
                restartLabel: restartLabel,
                laterLabel: laterLabel,
                completion: { choice in resolve(choice) }
            )
        }
    }

    @objc(showReadOnlyText:text:closeLabel:language:html:resolver:rejecter:)
    func showReadOnlyText(
        _ title: String,
        text: String,
        closeLabel: String,
        language: String,
        html: String,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        DispatchQueue.main.async {
            self.leaf.showReadOnlyText(
                title: title,
                text: text,
                closeTitle: closeLabel,
                language: language,
                html: html,
                completion: { resolve(nil) }
            )
        }
    }

    @objc(showProviderAuth:resolver:rejecter:)
    func showProviderAuth(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: ["provider", "fingerprint", "verificationURL", "userCode", "callbackURL", "title", "closeLabel"]),
              let provider = options["provider"] as? String,
              (options["fingerprint"] == nil || options["fingerprint"] is String),
              let verificationURL = options["verificationURL"] as? String,
              (options["userCode"] == nil || options["userCode"] is String),
              (options["callbackURL"] == nil || options["callbackURL"] is String),
              let title = options["title"] as? String,
              let closeLabel = options["closeLabel"] as? String,
              ["openai", "claude"].contains(provider),
              !verificationURL.isEmpty,
              verificationURL.utf8.count <= 2_048,
              (options["userCode"] == nil || (options["userCode"] as? String)?.isEmpty == false),
              ((options["userCode"] as? String)?.utf8.count ?? 0) <= 128,
              ((options["callbackURL"] as? String)?.utf8.count ?? 0) <= 512,
              !title.isEmpty,
              title.utf8.count <= 320,
              !closeLabel.isEmpty,
              closeLabel.utf8.count <= 160 else {
            reject("E_NATIVE_PROVIDER_AUTH_INPUT", "The provider authentication request is invalid.", nil)
            return
        }
        DispatchQueue.main.async {
            self.leaf.showProviderAuth(
                provider: provider,
                fingerprint: options["fingerprint"] as? String,
                verificationURL: verificationURL,
                userCode: options["userCode"] as? String,
                callbackURL: options["callbackURL"] as? String,
                title: title,
                closeTitle: closeLabel,
                completion: { resolve(nil) }
            )
        }
    }

    @objc(showActionMenu:items:anchor:resolver:rejecter:)
    func showActionMenu(_ title: String, items: [String], anchor: [String: NSNumber], resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            resolve(self.leaf.showActionMenu(title: title, items: items, anchor: anchor))
        }
    }

    @objc(showGroupedActionMenu:groups:anchor:resolver:rejecter:)
    func showGroupedActionMenu(_ title: String, groups: [NSDictionary], anchor: [String: NSNumber], resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            resolve(self.leaf.showGroupedActionMenu(title: title, groups: groups as? [[String: Any]] ?? [], anchor: anchor))
        }
    }

    @objc func chooseModelsToAdd(_ models: [String], providerName: String, keyName: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        guard models.count <= 10_000,
              models.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 256 && !$0.unicodeScalars.contains(where: { $0.value < 32 }) }),
              providerName.utf8.count <= 512,
              keyName.utf8.count <= 512
        else {
            reject("E_NATIVE_MODEL_CHOOSER_INPUT", "The native model chooser input is invalid.", nil)
            return
        }
        DispatchQueue.main.async {
            self.leaf.chooseModelsToAdd(models: models, providerName: providerName, keyName: keyName) { selection in
                resolve(selection)
            }
        }
    }

    @objc func setShortcuts(_ shortcuts: [String: String]) {
        leaf.setShortcuts(shortcuts)
    }

    @objc func setLocalization(_ strings: [String: String]) {
        leaf.setLocalization(strings)
    }

    @objc(editSecret:field:target:title:allowClear:resolver:rejecter:)
    func editSecret(
        _ domain: String,
        field: String,
        target: String?,
        title: String,
        allowClear: Bool,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            let capability: CoreIPCBridge.SecretCapability
            do {
                capability = try CoreIPCBridge.shared.createSecretCapability(
                    domain: domain,
                    field: field,
                    target: target,
                    purpose: "settings"
                )
            } catch {
                DispatchQueue.main.async {
                    reject("E_NATIVE_SECRET_CAPABILITY", "The local Core could not prepare this secret field.", nil)
                }
                return
            }
            DispatchQueue.main.async {
                // The app's own decision surface, not an alert: the field is a
                // child window over the pane that asked for it, and the React
                // host keeps running while it is up.
                self.leaf.presentSecretPrompt(
                    title: title,
                    clearLabel: allowClear && capability.present
                        ? self.leaf.localizedText("clear", fallback: "Clear")
                        : nil
                ) { answer, value in
                    let shouldSet = answer == "set"
                    let shouldClear = answer == "clear"
                    guard shouldSet || shouldClear, !(shouldSet && value.isEmpty) else {
                        resolve(nil)
                        return
                    }
                    DispatchQueue.global(qos: .userInitiated).async {
                        do {
                            let staged = try CoreIPCBridge.shared.stageSecret(
                                capability.token,
                                value: shouldSet ? value : nil,
                                clear: shouldClear
                            )
                            DispatchQueue.main.async {
                                resolve(["revision": staged.revision, "present": staged.present])
                            }
                        } catch {
                            DispatchQueue.main.async {
                                reject("E_NATIVE_SECRET_STAGE", "The local Core could not stage this secret.", nil)
                            }
                        }
                    }
                }
            }
        }
    }

    /// Clear a retained secret without exposing a value, an empty sentinel, or
    /// a second editor dialog to React Native.  The one-time capability stays
    /// entirely inside the native host and Core validates it before staging.
    @objc(clearSecret:field:target:resolver:rejecter:)
    func clearSecret(
        _ domain: String,
        field: String,
        target: String?,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard !domain.isEmpty,
              domain.utf8.count <= 64,
              !field.isEmpty,
              field.utf8.count <= 64,
              (target?.utf8.count ?? 0) <= 256 else {
            resolve(nil)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let capability: CoreIPCBridge.SecretCapability
            do {
                capability = try CoreIPCBridge.shared.createSecretCapability(
                    domain: domain,
                    field: field,
                    target: target,
                    purpose: "settings"
                )
            } catch {
                DispatchQueue.main.async {
                    reject("E_NATIVE_SECRET_CAPABILITY", "The local Core could not prepare this secret field.", nil)
                }
                return
            }
            guard capability.present else {
                DispatchQueue.main.async { resolve(nil) }
                return
            }
            do {
                let staged = try CoreIPCBridge.shared.stageSecret(
                    capability.token,
                    value: nil,
                    clear: true
                )
                DispatchQueue.main.async {
                    resolve(["revision": staged.revision, "present": staged.present])
                }
            } catch {
                DispatchQueue.main.async {
                    reject("E_NATIVE_SECRET_STAGE", "The local Core could not clear this secret.", nil)
                }
            }
        }
    }

    @objc(copySecret:field:target:resolver:rejecter:)
    func copySecret(
        _ domain: String,
        field: String,
        target: String,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard !domain.isEmpty,
              domain.utf8.count <= 64,
              !field.isEmpty,
              field.utf8.count <= 64,
              !target.isEmpty,
              target.utf8.count <= 256 else {
            resolve(false)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let value = try CoreIPCBridge.shared.readPlainTextSecret(
                    domain: domain,
                    field: field,
                    target: target
                )
                DispatchQueue.main.async {
                    let pasteboard = NSPasteboard.general
                    pasteboard.clearContents()
                    resolve(pasteboard.setString(value, forType: .string))
                }
            } catch {
                DispatchQueue.main.async { resolve(false) }
            }
        }
    }

    @objc(relayLogin:resolver:rejecter:)
    func relayLogin(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: ["accountId", "type", "label", "origin", "language", "username", "embedded", "pendingAccount", "stationId", "stationName", "stationType", "stationOrigin"]),
              let accountID = options["accountId"] as? String,
              let type = options["type"] as? String,
              let label = options["label"] as? String,
              let origin = options["origin"] as? String else {
            reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
            return
        }
        let language: String
        if let value = options["language"] {
            guard let suppliedLanguage = value as? String else {
                reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
                return
            }
            language = suppliedLanguage
        } else {
            language = "system"
        }
        guard ["system", "en", "zh-Hans"].contains(language) else {
            reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
            return
        }
        let embedded: Bool
        if let value = options["embedded"] {
            guard let suppliedEmbedded = value as? Bool else {
                reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
                return
            }
            embedded = suppliedEmbedded
        } else {
            embedded = false
        }
        let pendingAccount: Bool
        if let value = options["pendingAccount"] {
            guard let suppliedPending = value as? Bool else {
                reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
                return
            }
            pendingAccount = suppliedPending
        } else {
            pendingAccount = false
        }
        let stationType: String?
        if let value = options["stationType"] {
            guard let suppliedStationType = value as? String, ["newapi", "sub2api"].contains(suppliedStationType) else {
                reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
                return
            }
            stationType = suppliedStationType
        } else {
            stationType = nil
        }
        // WKWebView and its native host are created on AppKit's main thread, while
        // the React promise is completed asynchronously after the browser
        // flow finishes or is cancelled.
        DispatchQueue.main.async {
            self.leaf.relayLogin(
                accountID: accountID,
                type: type,
                label: label,
                origin: origin,
                language: language,
                username: options["username"] as? String,
                embedded: embedded,
                pendingAccount: pendingAccount,
                stationID: options["stationId"] as? String,
                stationName: options["stationName"] as? String,
                stationType: stationType,
                stationOrigin: options["stationOrigin"] as? String
            ) { result in
                DispatchQueue.main.async {
                    guard let result else {
                        resolve(nil)
                        return
                    }
                    resolve(["revision": result.revision, "loginStatus": "signed_in", "username": result.username])
                }
            }
        }
    }

    @objc(openRelayLogs:resolver:rejecter:)
    func openRelayLogs(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: ["accountId", "type", "label", "origin", "language"]),
              let accountID = options["accountId"] as? String,
              let type = options["type"] as? String,
              let label = options["label"] as? String,
              let origin = options["origin"] as? String else {
            reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
            return
        }
        let language = (options["language"] as? String) ?? "system"
        guard ["system", "en", "zh-Hans"].contains(language) else {
            reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
            return
        }
        DispatchQueue.main.async {
            self.leaf.openRelayLogs(
                accountID: accountID,
                type: type,
                label: label,
                origin: origin,
                language: language
            ) {
                DispatchQueue.main.async { resolve(nil) }
            }
        }
    }

    @objc(restoreRelaySession:resolver:rejecter:)
    func restoreRelaySession(
        _ options: [String: Any],
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard Set(options.keys).isSubset(of: Set(["accountId", "type", "label", "origin", "username"])),
              let accountID = options["accountId"] as? String,
              let type = options["type"] as? String,
              let label = options["label"] as? String,
              let origin = options["origin"] as? String,
              (options["username"] == nil || options["username"] is String),
              ((options["username"] as? String)?.utf8.count ?? 0) <= 320 else {
            reject("E_NATIVE_RELAY_INPUT", "The relay account is invalid.", nil)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let result = self.leaf.restoreRelaySession(
                accountID: accountID,
                type: type,
                label: label,
                origin: origin,
                username: options["username"] as? String
            )
            DispatchQueue.main.async {
                guard let result else {
                    resolve(nil)
                    return
                }
                resolve([
                    "revision": result.revision,
                    "loginStatus": result.loginStatus,
                    "username": result.username,
                ])
            }
        }
    }

    @objc(clearRelayCredentials:resolver:rejecter:)
    func clearRelayCredentials(
        _ accountID: String,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard leaf.clearRelayCredentials(accountID: accountID) else {
            reject("E_NATIVE_RELAY_CLEAR", "The relay credentials could not be removed.", nil)
            return
        }
        resolve(nil)
    }

    @objc(clearRelayPassword:resolver:rejecter:)
    func clearRelayPassword(
        _ accountID: String,
        resolver resolve: @escaping RCTPromiseResolveBlock,
        rejecter reject: @escaping RCTPromiseRejectBlock
    ) {
        guard leaf.clearRelayPassword(accountID: accountID) else {
            reject("E_NATIVE_RELAY_PASSWORD_CLEAR", "The relay password could not be removed.", nil)
            return
        }
        resolve(nil)
    }

    @objc func systemLocale() -> String {
        leaf.systemLocale()
    }

    @objc func setLaunchAtLogin(_ enabled: Bool, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            guard self.leaf.setLaunchAtLogin(enabled) else {
                reject("E_NATIVE_AUTOSTART", "The system could not update the login item.", nil)
                return
            }
            resolve(true)
        }
    }

    @objc func restartCodex(_ resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            resolve(self.leaf.restartCodex())
        }
    }

    @objc func showVersion() {
        leaf.showVersion()
    }

    @objc(versionInfo:rejecter:)
    func versionInfo(_ resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        resolve(leaf.versionInfo())
    }

    @objc func openExternalURL(_ url: String) {
        leaf.openExternalURL(url)
    }

    @objc func revealFile(_ path: String) {
        leaf.revealFile(path)
    }

    @objc func openFileEditor(_ payload: String) {
        leaf.openFileEditor(payload)
    }

    @objc func prepareFileEditor() {
        leaf.prepareFileEditor()
    }

    @objc func pendingFileEditorTarget() -> String {
        leaf.pendingFileEditorTarget()
    }

    @objc func quit() {
        leaf.requestQuit()
    }
}
#endif
