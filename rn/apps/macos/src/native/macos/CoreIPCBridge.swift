import Darwin
import Foundation

/// Private native client for the Python Core. Only versioned response/event
/// JSON crosses into React; endpoint descriptors, credentials, and selected
/// file URLs stay inside this host process.
@objc(CoreIPCBridge)
@objcMembers public final class CoreIPCBridge: NSObject {
    public static let shared = CoreIPCBridge()

    struct SecretCapability {
        let token: String
        let present: Bool
    }

    struct SecretStageResult {
        let revision: Int
        let present: Bool
    }

    struct SecretReadCapability {
        let token: String
    }

    struct EditorStageResult {
        let revision: Int
        let replacementToken: String
    }

    private struct EditorIdentity {
        let domain: String
        let document: String
    }

    private struct Endpoint {
        let address: String
        let port: Int
        let bootstrapToken: String

        var baseURL: URL? { URL(string: "http://\(address):\(port)/v1") }
    }

    private enum BridgeError: Error {
        case unavailable
        case authentication
        case invalidResponse
    }

    private let lock = NSLock()
    private let stoppingLock = NSLock()
    private static let relayLoginTimeout: TimeInterval = 60
    private static let coreShutdownTimeout: TimeInterval = 4
    // Core publishes IPC before starting the managed proxy, so host startup
    // only waits for the local control endpoint rather than proxy health.
    private static let coreStartupTimeout: TimeInterval = 10
    private var endpoint: Endpoint?
    private var sessionToken: String?
    private var sessionExpiresAt: Date?
    private var process: Process?
    private var coreDirectory: URL?
    private var eventHandler: ((String) -> Void)?
    private var subscriptionID: String?
    private var subscriptionRequest: String?
    private var recoveryScheduled = false
    private var pollCancelled = false
    private var stopping = false
    private var generation = 0
    private var editorIdentities: [String: EditorIdentity] = [:]
    private var editorIdentityOrder: [String] = []

    private override init() { super.init() }

    private func isStopping() -> Bool {
        stoppingLock.lock()
        defer { stoppingLock.unlock() }
        return stopping
    }

    private func beginStopping() -> Bool {
        stoppingLock.lock()
        defer { stoppingLock.unlock() }
        guard !stopping else { return false }
        stopping = true
        return true
    }

    /// Start the complete Core/proxy unit while the React bundle is loading.
    /// Failure remains visible to the menu so the user can inspect or retry.
    public func warm() {
        DispatchQueue.global(qos: .userInitiated).async {
            _ = try? self.ensureSession()
        }
    }

    func setEventHandler(_ handler: ((String) -> Void)?) {
        lock.lock()
        eventHandler = handler
        lock.unlock()
    }

    func send(_ request: String, completion: @escaping (Result<String, Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                guard !self.isStopping() else { throw BridgeError.unavailable }
                guard let data = request.data(using: .utf8),
                      let metadata = self.requestMetadata(data) else { throw BridgeError.invalidResponse }
                let (body, _, requestGeneration, restarted) = try self.performCoreRequest(route: "", method: "POST", body: data)
                guard self.isValidResponseEnvelope(body, requestID: metadata.requestID),
                      let text = String(data: body, encoding: .utf8) else {
                    _ = self.resetCore(expectedGeneration: requestGeneration)
                    throw BridgeError.invalidResponse
                }
                if metadata.method == "editor" {
                    self.rememberEditorCapability(requestData: data, responseData: body)
                }
                self.startPollingIfSubscription(in: text, request: request, method: metadata.method, generation: requestGeneration)
                if restarted && metadata.method != "subscribe" { self.scheduleSubscriptionRecovery() }
                completion(.success(text))
            } catch {
                completion(.failure(BridgeError.unavailable))
            }
        }
    }

    /// Native AppKit windows use the same versioned IPC envelope as React.
    /// Keeping the helper here preserves one authenticated Core session and
    /// prevents settings logic from being duplicated in the presentation layer.
    @nonobjc func call(
        method: String,
        params: [String: Any],
        completion: @escaping (Result<[String: Any], Error>) -> Void
    ) {
        let requestID = "native-\(UUID().uuidString.lowercased())"
        let envelope: [String: Any] = [
            "protocol_version": 1,
            "request_id": requestID,
            "method": method,
            "params": params,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: envelope, options: []),
              let request = String(data: data, encoding: .utf8) else {
            completion(.failure(BridgeError.invalidResponse))
            return
        }
        send(request) { result in
            switch result {
            case .failure(let error):
                completion(.failure(error))
            case .success(let response):
                guard let data = response.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      object["protocol_version"] as? Int == 1,
                      object["request_id"] as? String == requestID,
                      object["ok"] as? Bool == true,
                      let payload = object["result"] as? [String: Any] else {
                    completion(.failure(BridgeError.invalidResponse))
                    return
                }
                completion(.success(payload))
            }
        }
    }

    /// Exchanges an AppKit panel URL for a one-time opaque Core token. The
    /// filesystem path never reaches React or a normal error string.
    func registerFileCapability(_ url: URL, purpose: String) -> String? {
        guard ["import", "export"].contains(purpose) else { return nil }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["purpose": purpose, "path": url.path], options: [])
            let (data, response, _, restarted) = try performCoreRequest(route: "host/file-capability", method: "POST", body: body)
            if restarted { scheduleSubscriptionRecovery() }
            guard response.statusCode == 200,
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  object["protocol_version"] as? Int == 1,
                  let token = object["token"] as? String,
                  !token.isEmpty else { return nil }
            return token
        } catch {
            return nil
        }
    }

    struct RelayLoginResult {
        let revision: Int
        let username: String
    }

    struct RelaySessionRestoreResult {
        let revision: Int
        let loginStatus: String
        let username: String
    }

    /// Sends browser credentials only over the authenticated native/Core host
    /// route. The returned value is deliberately limited to public metadata.
    func acceptRelayLogin(
        accountID: String,
        type: String,
        label: String,
        origin: String,
        username: String,
        cookie: String?,
        accessToken: String?,
        refreshToken: String?,
        password: String? = nil
    ) throws -> RelayLoginResult {
        var payload: [String: Any] = [
            "account_id": accountID,
            "type": type,
            "label": label,
            "origin": origin,
            "username": username,
        ]
        if let cookie, !cookie.isEmpty { payload["cookie"] = cookie }
        if let accessToken, !accessToken.isEmpty { payload["access_token"] = accessToken }
        if let refreshToken, !refreshToken.isEmpty { payload["refresh_token"] = refreshToken }
        if let password, !password.isEmpty { payload["password"] = password }
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []),
              body.count <= 96 * 1024 else { throw BridgeError.invalidResponse }
        let (data, response, _, restarted) = try performCoreRequest(
            route: "host/relay/login",
            method: "POST",
            body: body,
            timeoutInterval: Self.relayLoginTimeout
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "revision", "login_status", "username"]),
              object["protocol_version"] as? Int == 1,
              object["login_status"] as? String == "signed_in",
              let revision = object["revision"] as? NSNumber,
              revision.doubleValue.rounded(.towardZero) == revision.doubleValue,
              revision.intValue >= 0,
              let safeUsername = object["username"] as? String,
              !safeUsername.isEmpty,
              safeUsername.utf8.count <= 320 else { throw BridgeError.invalidResponse }
        return RelayLoginResult(revision: revision.intValue, username: safeUsername)
    }

    /// Reattaches an already verified native credential-store session after a
    /// Core restart. This route intentionally does not fetch relay API keys
    /// or touch the staged provider/model configuration.
    func restoreRelaySession(
        accountID: String,
        type: String,
        label: String,
        origin: String,
        loginStatus: String,
        username: String? = nil,
        cookie: String? = nil,
        accessToken: String? = nil,
        refreshToken: String? = nil
    ) throws -> RelaySessionRestoreResult {
        guard ["signed_in", "signed_out", "expired"].contains(loginStatus),
              accountID.utf8.count <= 96,
              type == "newapi" || type == "sub2api",
              !label.isEmpty, label.utf8.count <= 160,
              !origin.isEmpty, origin.utf8.count <= 2_048,
              (username?.utf8.count ?? 0) <= 320,
              (cookie?.utf8.count ?? 0) <= 32_768,
              (accessToken?.utf8.count ?? 0) <= 32_768,
              (refreshToken?.utf8.count ?? 0) <= 32_768,
              loginStatus != "signed_in" || (!(username?.isEmpty ?? true) && (!(cookie?.isEmpty ?? true) || !(accessToken?.isEmpty ?? true))),
              loginStatus == "signed_in" || ((cookie?.isEmpty ?? true) && (accessToken?.isEmpty ?? true) && (refreshToken?.isEmpty ?? true)) else {
            throw BridgeError.invalidResponse
        }
        var payload: [String: Any] = [
            "account_id": accountID,
            "type": type,
            "label": label,
            "origin": origin,
            "login_status": loginStatus,
        ]
        if let username, !username.isEmpty { payload["username"] = username }
        if let cookie, !cookie.isEmpty { payload["cookie"] = cookie }
        if let accessToken, !accessToken.isEmpty { payload["access_token"] = accessToken }
        if let refreshToken, !refreshToken.isEmpty { payload["refresh_token"] = refreshToken }
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []),
              body.count <= 96 * 1024 else { throw BridgeError.invalidResponse }
        let (data, response, _, restarted) = try performCoreRequest(
            route: "host/relay/restore",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "revision", "login_status", "username"]),
              object["protocol_version"] as? Int == 1,
              let restoredStatus = object["login_status"] as? String,
              ["signed_in", "signed_out", "expired"].contains(restoredStatus),
              let revision = object["revision"] as? NSNumber,
              revision.doubleValue.rounded(.towardZero) == revision.doubleValue,
              revision.intValue >= 0,
              let safeUsername = object["username"] as? String,
              safeUsername.utf8.count <= 320 else { throw BridgeError.invalidResponse }
        return RelaySessionRestoreResult(
            revision: revision.intValue,
            loginStatus: restoredStatus,
            username: safeUsername
        )
    }

    /// Reads an opaque editor capability and returns raw text only to the
    /// native host. This value must never cross the React Native bridge.
    func readEditorDocument(_ editorToken: String) throws -> String {
        guard !editorToken.isEmpty,
              editorToken.utf8.count <= 256,
              let body = try? JSONSerialization.data(
                  withJSONObject: ["editor_token": editorToken],
                  options: []
              ) else { throw BridgeError.invalidResponse }
        let (data, response, requestGeneration, restarted) = try performCoreRequest(
            route: "host/editor/read",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "text"]),
              object["protocol_version"] as? Int == 1,
              let text = object["text"] as? String,
              text.utf8.count <= 2 * 1024 * 1024 else {
            if response.statusCode >= 500 {
                _ = resetCore(expectedGeneration: requestGeneration)
            }
            throw BridgeError.invalidResponse
        }
        return text
    }

    /// Stages native editor text through the trusted host route. The
    /// replacement token is retained only by the native host, while React
    /// receives at most the revision through a native component event.
    @nonobjc func stageEditorDocument(_ editorToken: String, text: String) throws -> EditorStageResult {
        guard !editorToken.isEmpty,
              editorToken.utf8.count <= 256,
              text.utf8.count <= 2 * 1024 * 1024,
              let body = try? JSONSerialization.data(
                  withJSONObject: ["editor_token": editorToken, "text": text],
                  options: []
              ) else { throw BridgeError.invalidResponse }
        let (data, response, requestGeneration, restarted) = try performCoreRequest(
            route: "host/editor/stage",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "revision", "editor_token"]),
              object["protocol_version"] as? Int == 1,
              let revision = object["revision"] as? NSNumber,
              revision.doubleValue.rounded(.towardZero) == revision.doubleValue,
              revision.intValue >= 0,
              let replacementToken = object["editor_token"] as? String,
              !replacementToken.isEmpty,
              replacementToken.utf8.count <= 256 else {
            if response.statusCode >= 500 {
                _ = resetCore(expectedGeneration: requestGeneration)
            }
            throw BridgeError.invalidResponse
        }
        return EditorStageResult(revision: revision.intValue, replacementToken: replacementToken)
    }

    /// Reissues an expired editor capability without exposing its identity or
    /// document text to React. The trusted read is part of the handshake: Core
    /// requires it before the replacement capability may stage native text.
    @objc(refreshEditorDocument:completion:)
    public func refreshEditorDocumentAsync(
        _ editorToken: String,
        completion: @escaping (String?, String?, String?) -> Void
    ) {
        guard let identity = editorIdentity(for: editorToken) else {
            DispatchQueue.main.async { completion(nil, nil, "refresh_failed") }
            return
        }
        call(method: "editor", params: ["domain": identity.domain, "document": identity.document]) { result in
            switch result {
            case .failure:
                DispatchQueue.main.async { completion(nil, nil, "refresh_failed") }
            case .success(let payload):
                guard Set(payload.keys) == Set(["domain", "document", "editor_token", "revision"]),
                      payload["domain"] as? String == identity.domain,
                      payload["document"] as? String == identity.document,
                      let replacementToken = payload["editor_token"] as? String,
                      !replacementToken.isEmpty,
                      replacementToken.utf8.count <= 256 else {
                    DispatchQueue.main.async { completion(nil, nil, "refresh_failed") }
                    return
                }
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        let text = try self.readEditorDocument(replacementToken)
                        self.replaceEditorCapability(editorToken, with: replacementToken, identity: identity)
                        DispatchQueue.main.async { completion(replacementToken, text, nil) }
                    } catch {
                        DispatchQueue.main.async { completion(nil, nil, "refresh_failed") }
                    }
                }
            }
        }
    }

    /// Objective-C++ component views use these asynchronous wrappers so raw
    /// editor text stays inside the native host and never crosses the RN bridge.
    @objc(readEditorDocument:completion:)
    public func readEditorDocumentAsync(
        _ editorToken: String,
        completion: @escaping (String?, String?) -> Void
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let text = try self.readEditorDocument(editorToken)
                DispatchQueue.main.async { completion(text, nil) }
            } catch {
                DispatchQueue.main.async { completion(nil, "read_failed") }
            }
        }
    }

    @objc(stageEditorDocument:text:completion:)
    public func stageEditorDocumentAsync(
        _ editorToken: String,
        text: String,
        completion: @escaping (NSNumber?, String?, String?) -> Void
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let result = try self.stageEditorDocument(editorToken, text: text)
                self.rotateEditorCapability(editorToken, to: result.replacementToken)
                DispatchQueue.main.async {
                    completion(NSNumber(value: result.revision), result.replacementToken, nil)
                }
            } catch {
                DispatchQueue.main.async { completion(nil, nil, "stage_failed") }
            }
        }
    }

    @nonobjc func createSecretCapability(
        domain: String,
        field: String,
        target: String?,
        purpose: String
    ) throws -> SecretCapability {
        var payload: [String: Any] = ["domain": domain, "field": field, "purpose": purpose]
        if let target, !target.isEmpty { payload["target"] = target }
        guard domain.utf8.count <= 64,
              field.utf8.count <= 64,
              (target?.utf8.count ?? 0) <= 256,
              purpose.utf8.count <= 128,
              let body = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
            throw BridgeError.invalidResponse
        }
        let (data, response, requestGeneration, restarted) = try performCoreRequest(
            route: "host/secret/capability",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "secret_token", "revision", "present"]),
              object["protocol_version"] as? Int == 1,
              let token = object["secret_token"] as? String,
              !token.isEmpty,
              token.utf8.count <= 256,
              object["revision"] is NSNumber,
              let present = object["present"] as? Bool else {
            if response.statusCode >= 500 { _ = resetCore(expectedGeneration: requestGeneration) }
            throw BridgeError.invalidResponse
        }
        return SecretCapability(token: token, present: present)
    }

    @nonobjc func stageSecret(_ secretToken: String, value: String?, clear: Bool) throws -> SecretStageResult {
        guard !secretToken.isEmpty,
              secretToken.utf8.count <= 256,
              clear || value != nil,
              (value?.utf8.count ?? 0) <= 16_384 else { throw BridgeError.invalidResponse }
        let payload: [String: Any] = clear
            ? ["secret_token": secretToken, "clear": true]
            : ["secret_token": secretToken, "value": value ?? ""]
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
            throw BridgeError.invalidResponse
        }
        let (data, response, requestGeneration, restarted) = try performCoreRequest(
            route: "host/secret/stage",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "revision", "present"]),
              object["protocol_version"] as? Int == 1,
              let revision = object["revision"] as? NSNumber,
              revision.doubleValue.rounded(.towardZero) == revision.doubleValue,
              revision.intValue >= 0,
              let present = object["present"] as? Bool else {
            if response.statusCode >= 500 { _ = resetCore(expectedGeneration: requestGeneration) }
            throw BridgeError.invalidResponse
        }
        return SecretStageResult(revision: revision.intValue, present: present)
    }

    /// Plaintext credentials remain session-bound, expire quickly, and are
    /// consumed before the value is returned to this native host.
    @nonobjc func createSecretReadCapability(
        domain: String,
        field: String,
        target: String?
    ) throws -> SecretReadCapability {
        guard !domain.isEmpty,
              domain.utf8.count <= 64,
              !field.isEmpty,
              field.utf8.count <= 128,
              (target?.utf8.count ?? 0) <= 256 else {
            throw BridgeError.invalidResponse
        }
        // The Core route has a strict three-field envelope.  Keep the
        // target key present even for domain-level secrets; JSON null is the
        // explicit representation of a missing target.
        var payload: [String: Any] = ["domain": domain, "field": field, "target": NSNull()]
        if let target, !target.isEmpty { payload["target"] = target }
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
            throw BridgeError.invalidResponse
        }
        let (data, response, requestGeneration, restarted) = try performCoreRequest(
            route: "host/secret/read-capability",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "secret_read_token", "revision", "present"]),
              object["protocol_version"] as? Int == 1,
              let token = object["secret_read_token"] as? String,
              !token.isEmpty,
              token.utf8.count <= 256,
              let revision = object["revision"] as? NSNumber,
              revision.doubleValue.rounded(.towardZero) == revision.doubleValue,
              revision.intValue >= 0,
              object["present"] is Bool else {
            if response.statusCode >= 500 { _ = resetCore(expectedGeneration: requestGeneration) }
            throw BridgeError.invalidResponse
        }
        return SecretReadCapability(token: token)
    }

    @nonobjc func readSecret(_ secretReadToken: String) throws -> String {
        guard !secretReadToken.isEmpty,
              secretReadToken.utf8.count <= 256,
              let body = try? JSONSerialization.data(
                  withJSONObject: ["secret_read_token": secretReadToken],
                  options: []
              ) else { throw BridgeError.invalidResponse }
        let (data, response, requestGeneration, restarted) = try performCoreRequest(
            route: "host/secret/read",
            method: "POST",
            body: body
        )
        if restarted { scheduleSubscriptionRecovery() }
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == Set(["protocol_version", "value"]),
              object["protocol_version"] as? Int == 1,
              let value = object["value"] as? String,
              value.utf8.count <= 16_384,
              !value.contains("\u{0000}"),
              !value.contains("\r"),
              !value.contains("\n") else {
            if response.statusCode >= 500 { _ = resetCore(expectedGeneration: requestGeneration) }
            throw BridgeError.invalidResponse
        }
        return value
    }

    @nonobjc func readPlainTextSecret(domain: String, field: String, target: String?) throws -> String {
        try readSecret(
            createSecretReadCapability(domain: domain, field: field, target: target).token
        )
    }

    /// The completion is invoked only by the AppKit Fabric leaf; plaintext is
    /// never emitted through a React Native module or component event.
    @objc(readPlainTextSecretForDomain:field:target:completion:)
    public func readPlainTextSecretForDomain(
        _ domain: String,
        field: String,
        target: String?,
        completion: @escaping (NSString?, NSString?) -> Void
    ) {
        guard !domain.isEmpty, !field.isEmpty, target == nil || target!.utf8.count <= 256 else {
            DispatchQueue.main.async { completion(nil, "read_failed") }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let value = try self.readPlainTextSecret(domain: domain, field: field, target: target)
                DispatchQueue.main.async { completion(value as NSString, nil) }
            } catch {
                DispatchQueue.main.async { completion(nil, "read_failed") }
            }
        }
    }

    /// A password Fabric leaf invokes this directly.  The secret travels only
    /// from NSSecureTextField to the authenticated host/Core route; React sees
    /// the completion's presence/revision/error metadata and never the value.
    @objc(stageSecretForDomain:field:target:value:completion:)
    public func stageSecretForDomain(
        _ domain: String,
        field: String,
        target: String?,
        value: String,
        completion: @escaping (NSNumber?, NSNumber?, String?) -> Void
    ) {
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let capability = try self.createSecretCapability(
                    domain: domain,
                    field: field,
                    target: target,
                    purpose: "settings"
                )
                let staged = try self.stageSecret(capability.token, value: value, clear: false)
                DispatchQueue.main.async {
                    completion(NSNumber(value: staged.revision), NSNumber(value: staged.present), nil)
                }
            } catch {
                DispatchQueue.main.async { completion(nil, nil, "stage_failed") }
            }
        }
    }

    public func stop() {
        guard beginStopping() else { return }
        lock.lock()
        pollCancelled = true
        let shutdownEndpoint = endpoint
        let shutdownToken = sessionToken
        lock.unlock()

        // The authenticated host route owns the managed LiteLLM lifecycle.
        // Wait for it to finish stopping the proxy before terminating Core;
        // otherwise a forced Core exit can orphan the 4000 listener.
        if let shutdownEndpoint, let shutdownToken {
            _ = try? performRequest(
                endpoint: shutdownEndpoint,
                route: "host/shutdown",
                method: "POST",
                body: Data("{}".utf8),
                token: shutdownToken,
                timeoutInterval: Self.coreShutdownTimeout
            )
        }

        lock.lock()
        endpoint = nil
        sessionToken = nil
        sessionExpiresAt = nil
        subscriptionID = nil
        subscriptionRequest = nil
        recoveryScheduled = false
        generation += 1
        let process = self.process
        self.process = nil
        let directory = coreDirectory
        coreDirectory = nil
        lock.unlock()

        stopCoreProcess(process, directory: directory)
    }

    private func performCoreRequest(
        route: String,
        method: String,
        body: Data?,
        timeoutInterval: TimeInterval = 30
    ) throws -> (Data, HTTPURLResponse, Int, Bool) {
        var requestGeneration: Int?
        do {
            let (endpoint, token, generation, restarted) = try ensureSession()
            requestGeneration = generation
            let (data, response) = try performRequest(
                endpoint: endpoint,
                route: route,
                method: method,
                body: body,
                token: token,
                timeoutInterval: timeoutInterval
            )
            if response.statusCode == 401 || response.statusCode == 403 {
                throw BridgeError.authentication
            }
            guard response.statusCode == 200 || (400...499).contains(response.statusCode) else {
                throw BridgeError.unavailable
            }
            return (data, response, generation, restarted)
        } catch {
            if let requestGeneration, resetCore(expectedGeneration: requestGeneration) {
                scheduleSubscriptionRecovery()
            }
            throw error
        }
    }

    private func exchangeSession(endpoint: Endpoint, credential: String) throws -> (token: String, expiresAt: Date) {
        let (body, response) = try performRequest(
            endpoint: endpoint,
            route: "hello",
            method: "POST",
            body: Data(),
            token: credential
        )
        guard response.statusCode == 200,
              response.value(forHTTPHeaderField: "X-LiteLLM-Core-Session")?.isEmpty == false,
              let session = response.value(forHTTPHeaderField: "X-LiteLLM-Core-Session"),
              let envelope = try JSONSerialization.jsonObject(with: body) as? [String: Any],
              envelope["protocol_version"] as? Int == 1,
              envelope["ok"] as? Bool == true,
              let sessionObject = envelope["session"] as? [String: Any],
              let expiresIn = sessionObject["expires_in"] as? NSNumber,
              expiresIn.doubleValue > 0 else {
            throw BridgeError.authentication
        }
        return (session, Date().addingTimeInterval(expiresIn.doubleValue))
    }

    private func ensureSession() throws -> (Endpoint, String, Int, Bool) {
        lock.lock()
        defer { lock.unlock() }
        guard !isStopping() else { throw BridgeError.unavailable }
        if let endpoint, let sessionToken, let sessionExpiresAt,
           sessionExpiresAt.timeIntervalSinceNow > 15 {
            return (endpoint, sessionToken, generation, false)
        }
        if let endpoint, let sessionToken,
           let renewed = try? exchangeSession(endpoint: endpoint, credential: sessionToken) {
            self.sessionToken = renewed.token
            self.sessionExpiresAt = renewed.expiresAt
            return (endpoint, renewed.token, generation, false)
        }

        let shouldRecoverSubscription = subscriptionRequest != nil
        pollCancelled = true
        subscriptionID = nil
        let (staleProcess, staleDirectory) = discardCoreLocked()
        stopCoreProcess(staleProcess, directory: staleDirectory)
        generation += 1
        let sessionGeneration = generation
        do {
            let endpoint = try startCoreLocked()
            let session = try exchangeSession(endpoint: endpoint, credential: endpoint.bootstrapToken)
            self.endpoint = endpoint
            self.sessionToken = session.token
            self.sessionExpiresAt = session.expiresAt
            self.pollCancelled = false
            return (endpoint, session.token, sessionGeneration, shouldRecoverSubscription)
        } catch {
            let (failedProcess, failedDirectory) = discardCoreLocked()
            stopCoreProcess(failedProcess, directory: failedDirectory)
            throw error
        }
    }

    private func startCoreLocked() throws -> Endpoint {
        if let endpoint { return endpoint }
        let fileManager = FileManager.default
        let directory = fileManager.temporaryDirectory.appendingPathComponent("litellm-menu-core-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let endpointFile = directory.appendingPathComponent("endpoint.json")

        let environment = ProcessInfo.processInfo.environment
        let coreRoot = resolveCoreRoot(environment: environment)
        let process = Process()
        let bundledPython = coreRoot?.appendingPathComponent("runtime/bin/python").path
        let pythonCandidates = [
            environment["LITELLM_MENU_CORE_PYTHON"],
            bundledPython,
        ].compactMap { $0 }
        guard let python = pythonCandidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw BridgeError.unavailable
        }
        process.executableURL = URL(fileURLWithPath: python)
        let arguments = [
            "-m",
            "litellm_menu.core",
            "--endpoint-file",
            endpointFile.path,
            "--parent-pid",
            String(ProcessInfo.processInfo.processIdentifier),
        ]
        process.arguments = arguments

        var childEnvironment = previewProfileEnvironment(from: environment)
        // An explicitly isolated Preview must never fall back to the user's
        // production configuration, assistant state, or LiteLLM service. The
        // profile marker is independent of the app identity, while the native
        // instance lock still prevents the preview from running beside the
        // installed menu-bar app.
        if let coreRoot {
            let rootPath = coreRoot.path
            let current = childEnvironment["PYTHONPATH"]
            childEnvironment["PYTHONPATH"] = current.map { "\(rootPath):\($0)" } ?? rootPath
            childEnvironment["LITELLM_BIN"] = coreRoot.appendingPathComponent("runtime/bin/litellm").path
        }
        // Core imports from a signed application bundle.  Bytecode caches
        // must never be written beside those signed resources at runtime.
        childEnvironment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = childEnvironment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()

        let deadline = Date().addingTimeInterval(Self.coreStartupTimeout)
        while Date() < deadline {
            guard !isStopping() else { break }
            guard process.isRunning else { break }
            if let data = try? Data(contentsOf: endpointFile),
               let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               raw["kind"] as? String == "loopback",
               let address = raw["address"] as? String,
               ["127.0.0.1", "::1", "localhost"].contains(address),
               let port = raw["port"] as? Int,
               (1...65535).contains(port),
               let bootstrapToken = raw["bootstrap_token"] as? String,
               !bootstrapToken.isEmpty {
                self.process = process
                self.coreDirectory = directory
                return Endpoint(address: address, port: port, bootstrapToken: bootstrapToken)
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        stopCoreProcess(process, directory: directory)
        throw BridgeError.unavailable
    }

    private func previewProfileEnvironment(from inherited: [String: String]) -> [String: String] {
        guard let rawRoot = Bundle.main.object(forInfoDictionaryKey: "LiteLLMMenuPreviewProfileRoot") as? String,
              rawRoot.hasPrefix("/") else {
            return inherited
        }

        let root = URL(fileURLWithPath: rawRoot, isDirectory: true).standardizedFileURL
        let runtime = root.appendingPathComponent(".litellm-runtime", isDirectory: true)
        let home = root.appendingPathComponent("home", isDirectory: true)
        let codex = home.appendingPathComponent(".codex", isDirectory: true)
        let claude = home.appendingPathComponent(".claude", isDirectory: true)
        var environment = inherited
        environment["HOME"] = home.path
        environment["LITELLM_RUNTIME_ROOT"] = root.path
        environment["LITELLM_MENU_HOME"] = root.path
        environment["LITELLM_CONFIG_FILE"] = root.appendingPathComponent("config.yaml").path
        environment["LITELLM_RUNTIME_CONFIG"] = runtime.appendingPathComponent("config.yaml").path
        environment["LITELLM_MENU_RUNTIME_SETTINGS_FILE"] = root.appendingPathComponent("runtime-settings.env").path
        if let rawPort = Bundle.main.object(forInfoDictionaryKey: "LiteLLMMenuPreviewPort") as? String,
           let port = Int(rawPort),
           (1...65_535).contains(port) {
            environment["LITELLM_PORT"] = String(port)
        }
        environment["CODEX_HOME"] = codex.path
        environment["CLAUDE_CONFIG_DIR"] = claude.path
        environment["CLAUDE_SETTINGS_PATH"] = claude.appendingPathComponent("settings.json").path
        environment["LITELLM_MENU_LANGUAGE_FILE"] = root.appendingPathComponent("language.json").path
        environment["LITELLM_WEBDAV_SYNC_SETTINGS"] = root.appendingPathComponent("webdav-sync.json").path
        environment["LITELLM_WEBDAV_SYNC_ENABLED_FILE"] = runtime.appendingPathComponent("webdav-sync.enabled").path
        environment["LITELLM_WEBDAV_SYNC_STATUS_FILE"] = runtime.appendingPathComponent("webdav-sync-status.json").path
        environment["LITELLM_WEBDAV_SYNC_STATE"] = runtime.appendingPathComponent("webdav-sync-state.json").path
        environment["LITELLM_STATE_FILE"] = root.appendingPathComponent(".litellm-runtime-state").path
        environment["LITELLM_NATIVE_PID_FILE"] = runtime.appendingPathComponent("litellm.pid").path
        environment["LITELLM_NATIVE_OWNER_FILE"] = runtime.appendingPathComponent("litellm.owner").path
        environment["LITELLM_AUTOSTART_STATE_FILE"] = runtime.appendingPathComponent("autostart.enabled").path
        environment["LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE"] = runtime.appendingPathComponent("route-recovery-state.json").path
        environment["LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE"] = runtime.appendingPathComponent("deployment-cooldowns.json").path
        environment["LITELLM_MENU_SEARCH_STATE_FILE"] = runtime.appendingPathComponent("web-search-references.json").path
        environment["LITELLM_RUNTIME_DIR"] = runtime.path
        environment["LITELLM_RECENT_REQUESTS_LOG"] = root.appendingPathComponent("recent-requests.jsonl").path
        return environment
    }

    private func resolveCoreRoot(environment: [String: String]) -> URL? {
        let fileManager = FileManager.default
        let explicitRoots = [
            environment["LITELLM_MENU_CORE_ROOT"],
            Bundle.main.resourceURL?.appendingPathComponent("Core").path,
        ].compactMap { $0 }.map(URL.init(fileURLWithPath:))
        if let root = explicitRoots.first(where: {
            fileManager.fileExists(atPath: $0.appendingPathComponent("litellm_menu/core/__main__.py").path)
                && fileManager.fileExists(atPath: $0.appendingPathComponent("sitecustomize.py").path)
        }) {
            return root
        }
        return nil
    }

    private func performRequest(
        endpoint: Endpoint,
        route: String,
        method: String,
        body: Data?,
        token: String,
        timeoutInterval: TimeInterval = 30
    ) throws -> (Data, HTTPURLResponse) {
        guard let baseURL = endpoint.baseURL else { throw BridgeError.unavailable }
        let url: URL
        if route.contains("?") {
            url = URL(string: baseURL.absoluteString + "/" + route) ?? baseURL
        } else {
            url = route.isEmpty ? baseURL : baseURL.appendingPathComponent(route)
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        // Core event polling waits up to 20 seconds. Keep the transport above
        // that bound so an ordinary null heartbeat is not treated as failure.
        request.timeoutInterval = timeoutInterval
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body

        let semaphore = DispatchSemaphore(value: 0)
        var result: Result<(Data, HTTPURLResponse), Error> = .failure(BridgeError.unavailable)
        URLSession.shared.dataTask(with: request) { data, response, error in
            defer { semaphore.signal() }
            guard error == nil, let data, let response = response as? HTTPURLResponse else { return }
            result = .success((data, response))
        }.resume()
        guard semaphore.wait(timeout: .now() + timeoutInterval + 1) == .success else { throw BridgeError.unavailable }
        return try result.get()
    }

    private func startPollingIfSubscription(in response: String, request: String, method: String, generation: Int) {
        guard method == "subscribe" else { return }
        guard let data = response.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["ok"] as? Bool == true,
              let result = object["result"] as? [String: Any],
              let subscription = result["subscription_id"] as? String,
              !subscription.isEmpty else { return }

        lock.lock()
        guard self.generation == generation else { lock.unlock(); return }
        subscriptionRequest = request
        subscriptionID = subscription
        pollCancelled = false
        lock.unlock()
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.poll(subscription: subscription, generation: generation)
        }
    }

    private func poll(subscription: String, generation: Int) {
        while true {
            lock.lock()
            let cancelled = pollCancelled || subscriptionID != subscription || self.generation != generation
            let endpoint = self.endpoint
            let token = sessionToken
            let handler = eventHandler
            lock.unlock()
            guard !cancelled, let endpoint, let token else { return }
            do {
                let route = "events?subscription_id=\(subscription)&timeout=20"
                let (data, response) = try performRequest(endpoint: endpoint, route: route, method: "GET", body: nil, token: token)
                guard response.statusCode == 200,
                      let outer = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      outer["protocol_version"] as? Int == 1,
                      Set(outer.keys) == Set(["protocol_version", "event"]) else {
                    throw BridgeError.invalidResponse
                }
                // `event: null` is a normal heartbeat for a quiet Core.
                if outer["event"] is NSNull { continue }
                guard let event = outer["event"] as? [String: Any],
                      event["protocol_version"] as? Int == 1,
                      let eventData = try? JSONSerialization.data(withJSONObject: event),
                      let eventText = String(data: eventData, encoding: .utf8) else {
                    throw BridgeError.invalidResponse
                }
                handler?(eventText)
            } catch {
                lock.lock()
                let retry = !pollCancelled
                    && subscriptionID == subscription
                    && self.generation == generation
                lock.unlock()
                guard retry else { return }
                Thread.sleep(forTimeInterval: 1)
            }
        }
    }

    private func requestMetadata(_ data: Data) -> (requestID: String, method: String)? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["protocol_version"] as? Int == 1,
              let requestID = object["request_id"] as? String,
              !requestID.isEmpty,
              let method = object["method"] as? String,
              !method.isEmpty else { return nil }
        return (requestID, method)
    }

    private func rememberEditorCapability(requestData: Data, responseData: Data) {
        guard let request = try? JSONSerialization.jsonObject(with: requestData) as? [String: Any],
              let params = request["params"] as? [String: Any],
              let domain = params["domain"] as? String,
              let document = params["document"] as? String,
              (domain == "codex" && ["config", "auth"].contains(document)) ||
                  (domain == "claude" && ["settings", "desktop", "developer"].contains(document)),
              let response = try? JSONSerialization.jsonObject(with: responseData) as? [String: Any],
              response["ok"] as? Bool == true,
              let result = response["result"] as? [String: Any],
              result["domain"] as? String == domain,
              result["document"] as? String == document,
              let token = result["editor_token"] as? String,
              !token.isEmpty,
              token.utf8.count <= 256 else { return }
        replaceEditorCapability(nil, with: token, identity: EditorIdentity(domain: domain, document: document))
    }

    private func editorIdentity(for token: String) -> EditorIdentity? {
        lock.lock()
        defer { lock.unlock() }
        return editorIdentities[token]
    }

    private func rotateEditorCapability(_ oldToken: String, to newToken: String) {
        guard let identity = editorIdentity(for: oldToken) else { return }
        replaceEditorCapability(oldToken, with: newToken, identity: identity)
    }

    private func replaceEditorCapability(_ oldToken: String?, with newToken: String, identity: EditorIdentity) {
        lock.lock()
        defer { lock.unlock() }
        if let oldToken {
            editorIdentities.removeValue(forKey: oldToken)
            editorIdentityOrder.removeAll { $0 == oldToken }
        }
        editorIdentities[newToken] = identity
        editorIdentityOrder.removeAll { $0 == newToken }
        editorIdentityOrder.append(newToken)
        while editorIdentityOrder.count > 128 {
            let expired = editorIdentityOrder.removeFirst()
            editorIdentities.removeValue(forKey: expired)
        }
    }

    private func isValidResponseEnvelope(_ data: Data, requestID: String) -> Bool {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["protocol_version"] as? Int == 1,
              object["request_id"] as? String == requestID,
              let ok = object["ok"] as? Bool else { return false }
        if ok {
            return Set(object.keys) == Set(["protocol_version", "request_id", "ok", "result"])
        }
        guard Set(object.keys) == Set(["protocol_version", "request_id", "ok", "error"]),
              let error = object["error"] as? [String: Any],
              Set(error.keys) == Set(["code", "message", "retryable"]),
              let code = error["code"] as? String,
              !code.isEmpty,
              error["message"] is String,
              error["retryable"] is Bool else { return false }
        return true
    }

    private func scheduleSubscriptionRecovery(after delay: TimeInterval = 0) {
        lock.lock()
        guard subscriptionID == nil,
              subscriptionRequest != nil,
              !recoveryScheduled else {
            lock.unlock()
            return
        }
        recoveryScheduled = true
        lock.unlock()

        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard let request = self.subscriptionRequest, self.subscriptionID == nil else {
                self.recoveryScheduled = false
                self.lock.unlock()
                return
            }
            self.lock.unlock()
            self.send(request) { result in
                self.lock.lock()
                self.recoveryScheduled = false
                let retry = self.subscriptionRequest != nil && self.subscriptionID == nil
                self.lock.unlock()
                if case .failure = result, retry { self.scheduleSubscriptionRecovery(after: 1) }
            }
        }
    }

    @discardableResult
    private func resetCore(expectedGeneration: Int) -> Bool {
        lock.lock()
        guard generation == expectedGeneration else {
            lock.unlock()
            return false
        }
        pollCancelled = true
        subscriptionID = nil
        generation += 1
        let (process, directory) = discardCoreLocked()
        lock.unlock()
        stopCoreProcess(process, directory: directory)
        return true
    }

    private func stopCoreProcess(_ process: Process?, directory: URL?) {
        guard let process else {
            if let directory { try? FileManager.default.removeItem(at: directory) }
            return
        }
        if process.isRunning {
            process.terminate()
            let deadline = Date().addingTimeInterval(1)
            while process.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            if process.isRunning {
                _ = Darwin.kill(process.processIdentifier, SIGKILL)
            }
        }
        process.waitUntilExit()
        if let directory { try? FileManager.default.removeItem(at: directory) }
    }

    @discardableResult
    private func discardCoreLocked() -> (Process?, URL?) {
        endpoint = nil
        sessionToken = nil
        sessionExpiresAt = nil
        let process = self.process
        self.process = nil
        let directory = coreDirectory
        coreDirectory = nil
        return (process, directory)
    }
}
