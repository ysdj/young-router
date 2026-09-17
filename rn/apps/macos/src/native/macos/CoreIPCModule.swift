import Foundation

#if canImport(React)
import React

@objc(LiteLLMCore)
final class LiteLLMCoreModule: RCTEventEmitter {
    private let core = CoreIPCBridge.shared
    private var observing = false
    private var eventHandlerToken: Int?

    override init() {
        super.init()
    }

    override static func requiresMainQueueSetup() -> Bool { true }
    override func supportedEvents() -> [String]! { ["coreEvent"] }
    override func startObserving() {
        observing = true
        guard eventHandlerToken == nil else { return }
        // One observer per React root: the primary host and every route window
        // each hold their own lifecycle, so the bridge fans Core events out
        // instead of delivering them to whichever root registered last.
        eventHandlerToken = core.addEventHandler { [weak self] event in
            DispatchQueue.main.async {
                guard let self, self.observing else { return }
                self.sendEvent(withName: "coreEvent", body: event)
            }
        }
    }
    override func stopObserving() {
        observing = false
        if let token = eventHandlerToken {
            core.removeEventHandler(token)
            eventHandlerToken = nil
        }
    }

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
