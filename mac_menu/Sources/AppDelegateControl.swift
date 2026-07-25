import Cocoa
import Darwin

extension AppDelegate {
    func control(_ action: String, logCommand: Bool = true) -> (Int32, String) {
        control(arguments: [action], logCommand: logCommand)
    }

    func control(_ action: String, logCommand: Bool = true, timeoutSeconds: TimeInterval?) -> (Int32, String) {
        control(arguments: [action], logCommand: logCommand, timeoutSeconds: timeoutSeconds)
    }

    func lifecycleControl(_ action: String) -> (Int32, String) {
        control(arguments: [action], trackLifecycleProcess: true)
    }

    func control(
        arguments: [String],
        input: String? = nil,
        logCommand: Bool = true,
        timeoutSeconds: TimeInterval? = nil,
        trackLifecycleProcess: Bool = false,
        outputLimitBytes: Int = 16 * 1_024 * 1_024
    ) -> (Int32, String) {
        let process = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let stdinPipe = input == nil ? nil : Pipe()
        let outputLock = NSLock()
        let terminationGroup = DispatchGroup()
        var output = Data()
        var outputWasTruncated = false
        let outputLimit = max(1, outputLimitBytes)
        let commandLabel = arguments.joined(separator: " ")

        func appendOutput(_ data: Data) {
            guard !data.isEmpty else { return }
            outputLock.lock()
            let remaining = max(0, outputLimit - output.count)
            if remaining > 0 {
                output.append(data.prefix(remaining))
            }
            if data.count > remaining {
                outputWasTruncated = true
            }
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
            if trackLifecycleProcess {
                lifecycleProcessLock.lock()
                let shouldCancel = lifecycleCancellationRequested
                if !shouldCancel {
                    lifecycleProcess = process
                }
                lifecycleProcessLock.unlock()
                if shouldCancel {
                    terminateProcessTree(process)
                }
            }
            defer {
                if trackLifecycleProcess {
                    lifecycleProcessLock.lock()
                    if lifecycleProcess === process {
                        lifecycleProcess = nil
                    }
                    lifecycleProcessLock.unlock()
                }
            }
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
            let truncated = outputWasTruncated
            outputLock.unlock()

            let text = String(data: data, encoding: .utf8) ?? ""
            if truncated {
                return (1, "Output from \(commandLabel) exceeded the supported size limit.")
            }
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

    func cancelLifecycleControl() {
        lifecycleProcessLock.lock()
        lifecycleCancellationRequested = true
        let process = lifecycleProcess
        lifecycleProcess = nil
        lifecycleProcessLock.unlock()
        if let process, process.isRunning {
            terminateProcessTree(process)
        }
    }

    func resumeLifecycleControl() {
        lifecycleProcessLock.lock()
        lifecycleCancellationRequested = false
        lifecycleProcessLock.unlock()
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
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        let line = "[\(formatter.string(from: Date()))] \(message)\n"
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
        guard serviceShouldBeRunning else { return serviceState }
        switch serviceState {
        case .running, .starting:
            return serviceState
        case .stopped, .unhealthy:
            scheduleUnexpectedServiceRecovery()
            return serviceStartInFlight ? .starting : .unhealthy
        }
    }

    func scheduleUnexpectedServiceRecovery() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            guard self.serviceShouldBeRunning, !self.busy, !self.serviceStartInFlight else { return }
            let now = Date()
            if let previous = self.lastServiceRecoveryAttempt,
               now.timeIntervalSince(previous) < self.serviceRecoveryRetryInterval {
                return
            }
            self.beginServiceStart(
                logMessage: "LiteLLM service is not healthy while the menu app is open; starting LiteLLM service",
                failureTitle: "LiteLLM service recovery failed",
                showFailureAlert: false
            )
        }
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

    func isWebDAVSyncEnabled(deadline: Date? = nil) -> Bool {
        control("webdav-enabled-status", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline)).0 == 0
    }

    func currentMenuState(timeoutSeconds: TimeInterval? = nil) -> MenuState {
        let result = control(
            "menu-status",
            logCommand: false,
            timeoutSeconds: timeoutSeconds ?? statusRefreshTimeout
        )
        guard result.0 == 0,
              let data = result.1.data(using: .utf8),
              let payload = try? JSONDecoder().decode(MenuStatusPayload.self, from: data) else {
            return initialMenuState(serviceState: readServiceState())
        }

        let serviceState: ServiceState
        switch payload.serviceState {
        case "running": serviceState = .running
        case "starting": serviceState = .starting
        case "unhealthy": serviceState = .unhealthy
        default: serviceState = .stopped
        }
        let autoStartState: AutoStartState
        switch payload.autoStartState {
        case "enabled": autoStartState = .enabled
        case "incomplete": autoStartState = .incomplete
        default: autoStartState = .disabled
        }
        return MenuState(
            serviceState: serviceState,
            autoStartState: autoStartState,
            routeRecoverySummary: payload.routeRecoverySummary,
            routeRecovery: payload.routeRecovery ?? .empty,
            webdavSyncEnabled: payload.webdavSyncEnabled,
            webdavLastStatus: payload.webdavLastStatus
        )
    }
}
