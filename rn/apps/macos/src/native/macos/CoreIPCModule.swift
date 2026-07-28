import Foundation

#if canImport(React)
import React

@objc(LiteLLMCore)
final class LiteLLMCoreModule: RCTEventEmitter {
    private let core = CoreIPCBridge.shared
    private var observing = false

    override init() {
        super.init()
        core.setEventHandler { [weak self] event in
            DispatchQueue.main.async {
                guard let self, self.observing else { return }
                self.sendEvent(withName: "coreEvent", body: event)
            }
        }
    }

    override static func requiresMainQueueSetup() -> Bool { true }
    override func supportedEvents() -> [String]! { ["coreEvent"] }
    override func startObserving() { observing = true }
    override func stopObserving() { observing = false }

    @objc func send(_ request: String, resolver resolve: @escaping RCTPromiseResolveBlock, rejecter reject: @escaping RCTPromiseRejectBlock) {
        core.send(request) { result in
            DispatchQueue.main.async {
                switch result {
                case .success(let response): resolve(response)
                case .failure: reject("core_unavailable", "The local Core is unavailable.", nil)
                }
            }
        }
    }

    @objc func shutdown() { core.stop() }
}
#endif
