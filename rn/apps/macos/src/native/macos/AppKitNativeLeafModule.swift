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
        leaf.fileCapabilityRegistrar = { url, purpose in
            CoreIPCBridge.shared.registerFileCapability(url, purpose: purpose)
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

    @objc(setWindowContentSize:height:resolver:rejecter:)
    func setWindowContentSize(
        _ width: NSNumber,
        height: NSNumber,
        resolver resolve: RCTPromiseResolveBlock,
        rejecter reject: RCTPromiseRejectBlock
    ) {
        resolve(leaf.setWindowContentSize(width: width.doubleValue, height: height.doubleValue))
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

    @objc func openFilePicker(_ purpose: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            let token = self.leaf.chooseImportFile(purpose: purpose)
            resolve(token)
        }
    }

    @objc func saveFilePicker(_ purpose: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            let token = self.leaf.chooseExportFile()
            resolve(token)
        }
    }

    @objc func showConfirmation(_ title: String, message: String, confirmLabel: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: RCTPromiseRejectBlock) {
        DispatchQueue.main.async {
            resolve(self.leaf.confirm(title: title, message: message, confirmTitle: confirmLabel))
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
            resolve(self.leaf.chooseModelsToAdd(models: models, providerName: providerName, keyName: keyName))
        }
    }

    @objc func setShortcuts(_ shortcuts: [String: String]) {
        leaf.setShortcuts(shortcuts)
    }

    @objc func setLocalization(_ strings: [String: String]) {
        leaf.setLocalization(strings)
    }

    @objc func editSecureDocument(_ editorToken: String, language: String, title: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        guard !editorToken.isEmpty, editorToken.utf8.count <= 256 else {
            reject("E_NATIVE_EDITOR_TOKEN", "The native editor token is invalid.", nil)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let content = try CoreIPCBridge.shared.readEditorDocument(editorToken)
                DispatchQueue.main.async {
                    guard let staged = self.leaf.editText(content: content, language: language, title: title) else {
                        resolve(nil)
                        return
                    }
                    DispatchQueue.global(qos: .userInitiated).async {
                        let revision: Int?
                        do {
                            revision = try CoreIPCBridge.shared.stageEditorDocument(editorToken, text: staged).revision
                        } catch {
                            DispatchQueue.main.async {
                                reject("E_NATIVE_EDITOR_STAGE", "The local Core could not stage the document.", nil)
                            }
                            return
                        }
                        DispatchQueue.main.async {
                            resolve(revision)
                        }
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    reject("E_NATIVE_EDITOR_READ", "The local Core could not read the document.", nil)
                }
            }
        }
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
                let alert = NSAlert()
                alert.alertStyle = .informational
                alert.messageText = title
                alert.addButton(withTitle: self.leaf.localizedText("set", fallback: "Set"))
                if allowClear && capability.present {
                    alert.addButton(withTitle: self.leaf.localizedText("clear", fallback: "Clear"))
                }
                alert.addButton(withTitle: self.leaf.localizedText("cancel", fallback: "Cancel"))
                let input = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
                input.maximumNumberOfLines = 1
                alert.accessoryView = input
                alert.window.initialFirstResponder = input
                let response = alert.runModal()
                let shouldSet = response == .alertFirstButtonReturn
                let shouldClear = allowClear && capability.present && response == .alertSecondButtonReturn
                guard shouldSet || shouldClear else {
                    input.stringValue = ""
                    resolve(nil)
                    return
                }
                let value = shouldSet ? input.stringValue : nil
                input.stringValue = ""
                if shouldSet && (value?.isEmpty ?? true) {
                    resolve(nil)
                    return
                }
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        let staged = try CoreIPCBridge.shared.stageSecret(
                            capability.token,
                            value: value,
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

    @objc func showVersion() {
        leaf.showVersion()
    }

    @objc func quit() {
        leaf.requestQuit()
    }
}
#endif
