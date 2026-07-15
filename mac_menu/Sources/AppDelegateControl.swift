import Cocoa
import Darwin

extension AppDelegate {
    func control(_ action: String, logCommand: Bool = true) -> (Int32, String) {
        control(arguments: [action], logCommand: logCommand)
    }

    func control(_ action: String, logCommand: Bool = true, timeoutSeconds: TimeInterval?) -> (Int32, String) {
        control(arguments: [action], logCommand: logCommand, timeoutSeconds: timeoutSeconds)
    }

    func control(arguments: [String], input: String? = nil, logCommand: Bool = true, timeoutSeconds: TimeInterval? = nil) -> (Int32, String) {
        let process = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let stdinPipe = input == nil ? nil : Pipe()
        let outputLock = NSLock()
        let terminationGroup = DispatchGroup()
        var output = Data()
        let commandLabel = arguments.joined(separator: " ")

        func appendOutput(_ data: Data) {
            guard !data.isEmpty else { return }
            outputLock.lock()
            output.append(data)
            outputLock.unlock()
        }

        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [controlPath] + arguments
        process.currentDirectoryURL = URL(fileURLWithPath: bundleRoot)
        process.environment = controlEnvironment()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        if let stdinPipe {
            process.standardInput = stdinPipe
        }

        terminationGroup.enter()
        process.terminationHandler = { _ in
            terminationGroup.leave()
        }

        stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
            appendOutput(handle.availableData)
        }
        stderrPipe.fileHandleForReading.readabilityHandler = { handle in
            appendOutput(handle.availableData)
        }

        if logCommand {
            appendLog("control start: \(commandLabel)")
        }
        do {
            try process.run()
            if let stdinPipe {
                if let data = input?.data(using: .utf8) {
                    try? stdinPipe.fileHandleForWriting.write(contentsOf: data)
                }
                try? stdinPipe.fileHandleForWriting.close()
            }

            var completed = true
            if let timeoutSeconds {
                completed = terminationGroup.wait(timeout: .now() + timeoutSeconds) == .success
            } else {
                process.waitUntilExit()
            }

            if !completed {
                appendLog("control timeout: \(commandLabel), timeout=\(timeoutSeconds ?? 0)s")
                terminateProcessTree(process)
                _ = terminationGroup.wait(timeout: .now() + 2)
            }

            stdoutPipe.fileHandleForReading.readabilityHandler = nil
            stderrPipe.fileHandleForReading.readabilityHandler = nil
            appendOutput(stdoutPipe.fileHandleForReading.readDataToEndOfFile())
            appendOutput(stderrPipe.fileHandleForReading.readDataToEndOfFile())

            outputLock.lock()
            let data = output
            outputLock.unlock()

            let text = String(data: data, encoding: .utf8) ?? ""
            if !completed {
                let detail = text.trimmingCharacters(in: .whitespacesAndNewlines)
                let message = "Timed out running control command: \(commandLabel)"
                return (124, detail.isEmpty ? message : "\(message)\n\(detail)")
            }
            if logCommand {
                appendLog("control finish: \(commandLabel), exit=\(process.terminationStatus)")
            }
            return (process.terminationStatus, text)
        } catch {
            let message = String(describing: error)
            if logCommand {
                appendLog("control failed to launch: \(commandLabel), error=\(message)")
            }
            return (1, message)
        }
    }

    private func processIsAlive(_ pid: Int32) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }

    private func descendantProcessIDs(of rootPID: Int32) -> [Int32] {
        let process = Process()
        let outputPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-axo", "pid=,ppid="]
        process.standardOutput = outputPipe
        process.standardError = Pipe()

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return []
        }

        let output = outputPipe.fileHandleForReading.readDataToEndOfFile()
        let text = String(data: output, encoding: .utf8) ?? ""
        var childrenByParent: [Int32: [Int32]] = [:]
        for line in text.components(separatedBy: .newlines) {
            let parts = line.split(separator: " ").compactMap { Int32($0) }
            guard parts.count == 2 else { continue }
            childrenByParent[parts[1], default: []].append(parts[0])
        }

        var result: [Int32] = []
        var stack = childrenByParent[rootPID] ?? []
        while let pid = stack.popLast() {
            result.append(pid)
            stack.append(contentsOf: childrenByParent[pid] ?? [])
        }
        return result
    }

    private func terminateProcessTree(_ process: Process) {
        let rootPID = process.processIdentifier
        let descendants = descendantProcessIDs(of: rootPID).reversed()
        for pid in descendants {
            kill(pid, SIGTERM)
        }
        if process.isRunning {
            kill(rootPID, SIGTERM)
        }

        let deadline = Date().addingTimeInterval(2)
        while Date() < deadline {
            let rootAlive = process.isRunning || processIsAlive(rootPID)
            let childAlive = descendants.contains { processIsAlive($0) }
            if !rootAlive && !childAlive {
                return
            }
            Thread.sleep(forTimeInterval: 0.1)
        }

        for pid in descendants {
            if processIsAlive(pid) {
                kill(pid, SIGKILL)
            }
        }
        if process.isRunning || processIsAlive(rootPID) {
            kill(rootPID, SIGKILL)
        }
    }

    func controlEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let guiSafePath = [
            "\(bundleRoot)/runtime/bin",
            "\(bundleRoot)/bin",
            "\(root)/.venv/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ].joined(separator: ":")

        if let existingPath = environment["PATH"], !existingPath.isEmpty {
            environment["PATH"] = "\(guiSafePath):\(existingPath)"
        } else {
            environment["PATH"] = guiSafePath
        }
        environment["LITELLM_RUNTIME_ROOT"] = root
        environment["LITELLM_TEMPLATE_ROOT"] = bundleRoot
        environment["LITELLM_UV_BIN"] = "\(bundleRoot)/bin/uv"
        environment["LITELLM_MENU_LOG"] = "\(root)/menu-server.log"
        environment["LITELLM_MENU_ACTIONS_LOG"] = "\(root)/menu-actions.log"
        environment["LITELLM_MENU_RUNTIME_SETTINGS_FILE"] = "\(root)/runtime-settings.env"
        environment["LITELLM_RECENT_REQUESTS_LOG"] = "\(root)/recent-requests.jsonl"
        environment["LITELLM_CONFIG_WATCH_LOG"] = "\(root)/config-watch.log"
        environment["LITELLM_MENU_LOG_MAX_BYTES"] = "\(localLogMaxBytes())"
        environment["LITELLM_MENU_OWNER_PID"] = "\(ProcessInfo.processInfo.processIdentifier)"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment
    }

    func localLogMaxBytes() -> UInt64 {
        let defaultBytes: UInt64 = 10 * 1024 * 1024
        let settingsPath = "\(root)/runtime-settings.env"
        if let text = try? String(contentsOfFile: settingsPath, encoding: .utf8) {
            for rawLine in text.components(separatedBy: .newlines) {
                let line = rawLine.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)[0]
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard line.hasPrefix("LITELLM_MENU_LOG_MAX_BYTES=") else { continue }
                let value = line.dropFirst("LITELLM_MENU_LOG_MAX_BYTES=".count)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if let bytes = UInt64(value), bytes > 0 {
                    return bytes
                }
            }
        }

        let environment = ProcessInfo.processInfo.environment
        if let rawValue = environment["LITELLM_MENU_LOG_MAX_BYTES"],
           let bytes = UInt64(rawValue.trimmingCharacters(in: .whitespacesAndNewlines)),
           bytes > 0 {
            return bytes
        }
        return defaultBytes
    }

    func rotateLogIfNeeded(path: String) {
        let maxBytes = localLogMaxBytes()
        let fileManager = FileManager.default
        guard let attributes = try? fileManager.attributesOfItem(atPath: path),
              let fileSize = attributes[.size] as? NSNumber,
              fileSize.uint64Value > maxBytes else { return }

        let logURL = URL(fileURLWithPath: path)
        let backupURL = URL(fileURLWithPath: "\(path).1")
        guard let reader = try? FileHandle(forReadingFrom: logURL) else { return }
        defer { try? reader.close() }
        let offset = fileSize.uint64Value > maxBytes ? fileSize.uint64Value - maxBytes : 0
        try? reader.seek(toOffset: offset)
        guard let tailData = try? reader.readToEnd() else { return }

        let tempURL = logURL.deletingLastPathComponent()
            .appendingPathComponent(".\(logURL.lastPathComponent).rotate.\(UUID().uuidString)")
        do {
            try tailData.write(to: tempURL, options: .atomic)
            try? fileManager.removeItem(at: backupURL)
            try fileManager.moveItem(at: tempURL, to: backupURL)
            if let writer = try? FileHandle(forWritingTo: logURL) {
                try? writer.truncate(atOffset: 0)
                try? writer.write(contentsOf: tailData)
                try? writer.close()
            }
            try? fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
            try? fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: backupURL.path)
        } catch {
            try? fileManager.removeItem(at: tempURL)
        }
    }

    func appendLog(_ message: String) {
        let line = "[\(ISO8601DateFormatter().string(from: Date()))] \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        let url = URL(fileURLWithPath: menuLogPath)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        rotateLogIfNeeded(path: menuLogPath)

        if !FileManager.default.fileExists(atPath: menuLogPath) {
            FileManager.default.createFile(atPath: menuLogPath, contents: nil)
        }

        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        do {
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
            try handle.close()
        } catch {
            try? handle.close()
        }
    }

    func statusTimeout(deadline: Date? = nil) -> TimeInterval {
        guard let deadline else { return statusCommandTimeout }
        return max(0.1, min(statusCommandTimeout, deadline.timeIntervalSinceNow))
    }

    func readServiceState(deadline: Date? = nil) -> ServiceState {
        let result = control("status", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline))
        let output = result.1.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if result.0 == 124 {
            return .unhealthy
        }
        if result.0 == 0 || output.contains("running") {
            return .running
        }
        if output.contains("starting") {
            return .starting
        }
        if output.contains("unhealthy") {
            return .unhealthy
        }
        if output.contains("unmanaged") {
            return .unhealthy
        }
        if output.contains("stopped") {
            return .stopped
        }
        if result.0 != 0 {
            return .unhealthy
        }
        return .stopped
    }

    func displayedServiceState(_ serviceState: ServiceState) -> ServiceState {
        guard serviceState == .stopped, serviceShouldBeRunning else { return serviceState }
        scheduleUnexpectedStoppedRecovery()
        return serviceStartInFlight ? .starting : .unhealthy
    }

    func scheduleUnexpectedStoppedRecovery() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            guard self.serviceShouldBeRunning, !self.busy, !self.serviceStartInFlight else { return }
            let now = Date()
            if let previous = self.lastStoppedRecoveryAttempt,
               now.timeIntervalSince(previous) < self.stoppedRecoveryRetryInterval {
                return
            }
            self.beginServiceStart(
                logMessage: "LiteLLM service reported stopped while the menu app expects it running; starting LiteLLM service",
                failureTitle: "LiteLLM service restart failed",
                showFailureAlert: false
            )
        }
    }

    func isRunning() -> Bool {
        readServiceState().isRunning
    }

    func readAutoStartState(deadline: Date? = nil) -> AutoStartState {
        let result = control("autostart-status", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline))
        if result.0 == 0 {
            return .enabled
        }
        if result.1.localizedCaseInsensitiveContains("enabled but") {
            return .incomplete
        }
        return .disabled
    }

    func isRouteTraceEnabled(deadline: Date? = nil) -> Bool {
        control("route-trace-status", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline)).0 == 0
    }

    func readRouteRecoverySummary(deadline: Date? = nil) -> String {
        let result = control("route-recovery-summary", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline))
        if result.0 != 0 {
            return "0 recovering / 0 cooldown"
        }
        let output = result.1.trimmingCharacters(in: .whitespacesAndNewlines)
        return output.isEmpty ? "0 recovering / 0 cooldown" : output
    }

    func isWebDAVSyncEnabled(deadline: Date? = nil) -> Bool {
        control("webdav-enabled-status", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline)).0 == 0
    }

    func tomlStringValue(_ rawValue: String) -> String? {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let quote = trimmed.first else { return nil }
        if quote == "\"" || quote == "'" {
            var value = ""
            var escaping = false
            for character in trimmed.dropFirst() {
                if escaping {
                    value.append(character)
                    escaping = false
                    continue
                }
                if quote == "\"" && character == "\\" {
                    escaping = true
                    continue
                }
                if character == quote {
                    return value
                }
                value.append(character)
            }
            return value
        }

        let withoutComment = trimmed
            .split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)
            .first
            .map(String.init) ?? ""
        return withoutComment.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func tomlValue(for key: String, in text: String, topLevelOnly: Bool = false) -> String? {
        for line in text.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty || trimmed.hasPrefix("#") {
                continue
            }
            if trimmed.hasPrefix("[") {
                if topLevelOnly {
                    break
                }
                continue
            }
            let parts = trimmed.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let candidateKey = String(parts[0]).trimmingCharacters(in: .whitespacesAndNewlines)
            guard candidateKey == key else { continue }
            return tomlStringValue(String(parts[1]))
        }
        return nil
    }

    func tomlTableBody(_ table: String, in text: String) -> String? {
        var lines: [String] = []
        var collecting = false
        let wantedHeader = "[\(table)]"
        for line in text.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.hasPrefix("[") && !trimmed.hasPrefix("#") {
                let header = (trimmed.split(separator: "#", maxSplits: 1).first.map(String.init) ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if collecting {
                    break
                }
                collecting = header == wantedHeader
                continue
            }
            if collecting {
                lines.append(line)
            }
        }
        return collecting ? lines.joined(separator: "\n") : nil
    }

    func normalizedURL(_ value: String) -> String {
        var normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
        while normalized.hasSuffix("/") {
            normalized.removeLast()
        }
        return normalized
    }

    func codexConfigTextPointsToLiteLLM(_ text: String) -> Bool {
        guard let provider = tomlValue(for: "model_provider", in: text, topLevelOnly: true),
              !provider.isEmpty,
              let table = tomlTableBody("model_providers.\(provider)", in: text),
              let baseURL = tomlValue(for: "base_url", in: table) else {
            return false
        }

        let port = localServicePort(runtimeRoot: root, environment: ProcessInfo.processInfo.environment)
        return normalizedURL(baseURL) == normalizedURL("http://127.0.0.1:\(port)/v1")
    }

    func readCodexConfigState() -> CodexConfigState {
        let fileManager = FileManager.default
        let configPath = "\(codexHome)/config.toml"
        let statePath = "\(codexHome)/.litellm-menu-codex-local-config-state.json"
        let configText = try? String(contentsOfFile: configPath, encoding: .utf8)
        let configured = configText.map { codexConfigTextPointsToLiteLLM($0) } ?? false
        let preSwitchReapplyAvailable = codexPreSwitchStateIsActive(
            atPath: statePath,
            fileManager: fileManager
        )
        return CodexConfigState(
            configuredForLiteLLM: configured,
            preSwitchReapplyAvailable: preSwitchReapplyAvailable
        )
    }

    func codexPreSwitchStateIsActive(atPath statePath: String, fileManager: FileManager) -> Bool {
        guard fileManager.fileExists(atPath: statePath),
              let data = fileManager.contents(atPath: statePath),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["schema_version"] as? Int == 3,
              object["active"] as? Bool == true,
              object["config"] as? [String: Any] != nil,
              object["auth"] as? [String: Any] != nil,
              normalizedURL(object["target_base_url"] as? String ?? "") == normalizedURL("http://127.0.0.1:\(localServicePort(runtimeRoot: root, environment: ProcessInfo.processInfo.environment))/v1") else {
            return false
        }
        return true
    }

    func currentMenuState(timeoutSeconds: TimeInterval? = nil) -> MenuState {
        let deadline = timeoutSeconds.map { Date().addingTimeInterval($0) }
        return MenuState(
            serviceState: readServiceState(deadline: deadline),
            autoStartState: readAutoStartState(deadline: deadline),
            routeTraceEnabled: isRouteTraceEnabled(deadline: deadline),
            routeRecoverySummary: readRouteRecoverySummary(deadline: deadline),
            webdavSyncEnabled: isWebDAVSyncEnabled(deadline: deadline),
            webdavLastStatus: readWebDAVLastStatus(deadline: deadline),
            codexConfigState: readCodexConfigState()
        )
    }
}
