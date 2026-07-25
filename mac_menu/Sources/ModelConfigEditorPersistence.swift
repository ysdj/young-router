import Cocoa
import Darwin

private final class ProcessOutputCollector {
    private let maxBytes: Int
    private let lock = NSLock()
    private var data = Data()
    private var discardedBytes = 0

    init(maxBytes: Int = 1_048_576) {
        self.maxBytes = max(1, maxBytes)
    }

    func append(_ chunk: Data) {
        guard !chunk.isEmpty else { return }
        lock.lock()
        defer { lock.unlock() }

        if chunk.count >= maxBytes {
            discardedBytes += data.count + chunk.count - maxBytes
            data = Data(chunk.suffix(maxBytes))
            return
        }

        data.append(chunk)
        if data.count > maxBytes {
            let overflow = data.count - maxBytes
            discardedBytes += overflow
            data.removeFirst(overflow)
        }
    }

    func text(maxBytes: Int? = nil) -> String {
        lock.lock()
        let limit = maxBytes.map { max(1, $0) }
        let omitted = limit.map { max(0, data.count - $0) } ?? 0
        let snapshot = omitted > 0 ? Data(data.suffix(limit!)) : data
        let discarded = discardedBytes + omitted
        lock.unlock()

        var output = String(data: snapshot, encoding: .utf8) ?? String(decoding: snapshot, as: UTF8.self)
        if discarded > 0 {
            output = "[Earlier control output truncated: \(discarded) bytes]\n" + output
        }
        return output
    }

    func collectedData() -> Data {
        lock.lock()
        defer { lock.unlock() }
        return data
    }

    var didTruncate: Bool {
        lock.lock()
        defer { lock.unlock() }
        return discardedBytes > 0
    }
}

private let configEditorHelperTimeoutSeconds: TimeInterval = 30
private let configEditorHelperOutputLimitBytes = 32 * 1_024 * 1_024
private let configEditorHelperDiagnosticLimitBytes = 64 * 1_024
private let configEditorHelperDrainTimeoutSeconds: DispatchTimeInterval = .seconds(2)
private let externalImportOutputLimitBytes = 8 * 1_024 * 1_024

extension ModelConfigEditorController {
    func loadConfigPayload() throws -> ConfigEditorLoadPayload {
        if configEditorPythonPath() == nil {
            let bootstrap = runServiceControl(
                arguments: ["config-editor-bootstrap"],
                timeoutSeconds: 120
            )
            guard bootstrap.0 == 0 else {
                let detail = bootstrap.1.trimmingCharacters(in: .whitespacesAndNewlines)
                throw ConfigEditorError(
                    message: detail.isEmpty
                        ? "The config editor runtime could not be prepared."
                        : detail
                )
            }
        }
        let output = try runHelper(arguments: ["load"])
        return try JSONDecoder().decode(ConfigEditorLoadPayload.self, from: output)
    }

    func saveProviders(
        _ providers: [EditableProvider],
        expectedRevision: JSONValue? = nil,
        document: ConfigEditorDocument? = nil
    ) throws -> ConfigEditorSaveResult {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let input = try encoder.encode(
            ConfigEditorSavePayload(
                providers: providers,
                expectedRevision: expectedRevision ?? loadedConfigRevision,
                document: document ?? sourceDocument
            )
        )
        let output = try runHelper(arguments: ["save"], input: input)
        return try JSONDecoder().decode(ConfigEditorSaveResult.self, from: output)
    }

    func loadProviderBilling() throws -> ProviderBillingPayload {
        let result = runServiceControl(arguments: ["provider-billing"], timeoutSeconds: 15)
        guard result.0 == 0 else {
            throw ConfigEditorError(message: result.1)
        }
        guard let data = result.1.data(using: .utf8) else {
            throw ConfigEditorError(message: "Provider billing response was not valid UTF-8.")
        }
        return try JSONDecoder().decode(ProviderBillingPayload.self, from: data)
    }

    func importExternalProviders(
        arguments: [String],
        standardInput: Data? = nil
    ) throws -> ExternalProviderImportPayload {
        let result = runServiceControl(
            arguments: ["external-provider-import"] + arguments,
            timeoutSeconds: 60,
            standardInput: standardInput,
            outputLimitBytes: externalImportOutputLimitBytes
        )
        guard result.0 == 0 else {
            throw ConfigEditorError(message: result.1)
        }
        guard let data = result.1.data(using: .utf8) else {
            throw ConfigEditorError(message: "External provider import response was not valid UTF-8.")
        }
        return try JSONDecoder().decode(ExternalProviderImportPayload.self, from: data)
    }

    private func drainPipe(_ pipe: Pipe, into collector: ProcessOutputCollector, group: DispatchGroup) {
        group.enter()
        DispatchQueue.global(qos: .utility).async {
            let handle = pipe.fileHandleForReading
            while true {
                let chunk = handle.readData(ofLength: 32 * 1024)
                if chunk.isEmpty {
                    break
                }
                collector.append(chunk)
            }
            group.leave()
        }
    }

    private func processIsAlive(_ pid: Int32) -> Bool {
        Darwin.kill(pid, 0) == 0 || errno == EPERM
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
            Darwin.kill(pid, SIGTERM)
        }
        if process.isRunning {
            Darwin.kill(rootPID, SIGTERM)
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
                Darwin.kill(pid, SIGKILL)
            }
        }
        if process.isRunning || processIsAlive(rootPID) {
            Darwin.kill(rootPID, SIGKILL)
        }
    }

    private func waitForProcess(_ process: Process, timeoutSeconds: TimeInterval?) -> Bool {
        guard let timeoutSeconds else {
            process.waitUntilExit()
            return true
        }

        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if !process.isRunning {
            return true
        }

        terminateProcessTree(process)
        return false
    }

    func summarizeControlOutput(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }

        let priorityNeedles = [
            "Timed out waiting for native LiteLLM",
            "Timed out waiting for LiteLLM",
            "Runtime route mismatch",
            "Failed to fetch LiteLLM model info",
        ]
        let lines = trimmed
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let prioritized = lines.filter { line in
            priorityNeedles.contains { line.localizedCaseInsensitiveContains($0) }
        }
        let selected = prioritized.isEmpty
            ? lines.filter { line in
                !line.hasPrefix("Step ")
                    && !line.hasPrefix("--->")
                    && !line.localizedCaseInsensitiveContains("Sending build context")
                    && !line.localizedCaseInsensitiveContains("Image litellm-menu")
                    && !line.localizedCaseInsensitiveContains("Successfully built")
                    && !line.localizedCaseInsensitiveContains("Successfully tagged")
            }
            : prioritized
        return Array(selected.prefix(12)).joined(separator: "\n")
    }

    func runControl(_ action: String, generation: Int? = nil) -> (Int32, String) {
        runServiceControl(arguments: [action], generation: generation)
    }

    func runServiceControl(
        arguments: [String],
        generation: Int? = nil,
        timeoutSeconds: TimeInterval? = nil,
        standardInput: Data? = nil,
        outputLimitBytes: Int = 1_048_576
    ) -> (Int32, String) {
        guard let action = arguments.first else {
            return (64, "A service action is required.")
        }
        let process = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let stdoutCollector = ProcessOutputCollector(maxBytes: outputLimitBytes)
        let stderrCollector = ProcessOutputCollector(maxBytes: outputLimitBytes)
        let drainGroup = DispatchGroup()
        let stdinPipe = standardInput == nil ? nil : Pipe()

        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["\(bundleRoot)/service.sh"] + arguments
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        process.environment = environment
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        process.standardInput = stdinPipe

        do {
            try process.run()
            drainPipe(stdoutPipe, into: stdoutCollector, group: drainGroup)
            drainPipe(stderrPipe, into: stderrCollector, group: drainGroup)
            if let standardInput, let stdinPipe {
                stdinPipe.fileHandleForWriting.write(standardInput)
                stdinPipe.fileHandleForWriting.closeFile()
            }
            if let generation {
                runtimeApplyLock.lock()
                if runtimeApplyGeneration == generation {
                    runtimeApplyProcess = process
                } else if process.isRunning {
                    terminateProcessTree(process)
                }
                runtimeApplyLock.unlock()
            }
            let effectiveTimeout = timeoutSeconds ?? (generation == nil ? nil : 95)
            let completed = waitForProcess(process, timeoutSeconds: effectiveTimeout)
            if !completed {
                if generation != nil {
                    runtimeApplyLock.lock()
                    if runtimeApplyProcess === process {
                        runtimeApplyProcess = nil
                    }
                    runtimeApplyLock.unlock()
                }
                _ = drainGroup.wait(timeout: .now() + 2)
                let output = stdoutCollector.text() + stderrCollector.text()
                let detail = output.trimmingCharacters(in: .whitespacesAndNewlines)
                let message = "Timed out running \(action). The runtime apply process was terminated."
                return (124, detail.isEmpty ? message : "\(message)\n\(detail)")
            }
        } catch {
            _ = drainGroup.wait(timeout: .now() + 2)
            return (1, String(describing: error))
        }

        _ = drainGroup.wait(timeout: .now() + 2)

        if stdoutCollector.didTruncate || stderrCollector.didTruncate {
            return (1, "Output from \(action) exceeded the supported size limit.")
        }

        if generation != nil {
            runtimeApplyLock.lock()
            if runtimeApplyProcess === process {
                runtimeApplyProcess = nil
            }
            runtimeApplyLock.unlock()
        }

        let text = stdoutCollector.text() + stderrCollector.text()
        return (process.terminationStatus, text)
    }

    func configSaveTooltip(_ result: ConfigEditorSaveResult, runtimeOutput: String? = nil) -> String {
        var tooltip = "Providers: \(result.providers)\nActive models: \(result.active)\nDisabled models: \(result.disabled)\nBackup: \(result.backup)"
        if !result.disabledPath.isEmpty {
            tooltip += "\nDisabled models file: \(result.disabledPath)"
        }
        if let runtimeOutput {
            let trimmed = runtimeOutput.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                tooltip += "\n\nRuntime apply:\n\(trimmed)"
            }
        }
        return tooltip
    }

    func setRuntimeApplyInFlight(_ applying: Bool) {
        runtimeApplyInFlight = applying
        applyButton.isEnabled = !applying && hasPendingChanges && !externalImportInFlight
        applyButton.title = applying ? "Applying…" : "Apply"
    }

    func cancelRuntimeApplyInFlight() {
        runtimeApplyLock.lock()
        runtimeApplyGeneration += 1
        let process = runtimeApplyProcess
        runtimeApplyProcess = nil
        runtimeApplyLock.unlock()

        if process?.isRunning == true {
            terminateProcessTree(process!)
        }
    }

    func beginLatestRuntimeApply() -> Int {
        cancelRuntimeApplyInFlight()
        return runtimeApplyGeneration
    }

    func applyRuntimeConfigAfterSave(_ result: ConfigEditorSaveResult, generation: Int) {
        setRuntimeApplyInFlight(true)
        setEditorStatus("Applying runtime routes...")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let applyResult = self.runControl("apply-config", generation: generation)
            DispatchQueue.main.async {
                guard self.runtimeApplyGeneration == generation else { return }
                self.setRuntimeApplyInFlight(false)
                if applyResult.0 == 0 {
                    if self.hasPendingChanges {
                        self.setEditorStatus(
                            "Saved previous config.",
                            tooltip: self.configSaveTooltip(result, runtimeOutput: applyResult.1)
                        )
                    } else {
                        self.setEditorStatus(
                            "Saved \(DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)) · runtime routes verified",
                            tooltip: self.configSaveTooltip(result, runtimeOutput: applyResult.1)
                        )
                    }
                } else {
                    let summary = self.summarizeControlOutput(applyResult.1)
                    self.setEditorStatus(
                        "Config saved, but runtime apply failed.",
                        tooltip: self.configSaveTooltip(result, runtimeOutput: summary.isEmpty ? applyResult.1 : summary)
                    )
                }
                self.onSaved(result)
            }
        }
    }

    private func helperFailureMessage(
        stdout: ProcessOutputCollector,
        stderr: ProcessOutputCollector,
        fallback: String
    ) -> String {
        let stderrText = stderr.text(maxBytes: configEditorHelperDiagnosticLimitBytes)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !stderrText.isEmpty {
            return stderrText
        }
        let stdoutText = stdout.text(maxBytes: configEditorHelperDiagnosticLimitBytes)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return stdoutText.isEmpty ? fallback : stdoutText
    }

    func configEditorPythonPath() -> String? {
        let bundledPython = "\(bundleRoot)/runtime/bin/python"
        let developmentPython = "\(root)/.venv/bin/python"
        if FileManager.default.isExecutableFile(atPath: bundledPython) {
            return bundledPython
        }
        if FileManager.default.isExecutableFile(atPath: developmentPython) {
            return developmentPython
        }
        return nil
    }

    func runHelper(
        arguments: [String],
        input: Data? = nil,
        timeoutSeconds: TimeInterval = configEditorHelperTimeoutSeconds
    ) throws -> Data {
        let process = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let stdinPipe = input == nil ? nil : Pipe()
        let stdoutCollector = ProcessOutputCollector(maxBytes: configEditorHelperOutputLimitBytes)
        let stderrCollector = ProcessOutputCollector(maxBytes: configEditorHelperDiagnosticLimitBytes)
        let drainGroup = DispatchGroup()
        let inputWriteGroup = DispatchGroup()

        guard let pythonPath = configEditorPythonPath() else {
            throw ConfigEditorError(
                message: "No bundled Python runtime is available for the config editor."
            )
        }

        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = ["\(bundleRoot)/config_editor.py", "--config", "\(root)/config.yaml"] + arguments
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        process.environment = environment
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        if let stdinPipe {
            process.standardInput = stdinPipe
        }

        do {
            try process.run()
            drainPipe(stdoutPipe, into: stdoutCollector, group: drainGroup)
            drainPipe(stderrPipe, into: stderrCollector, group: drainGroup)
            if let input, let stdinPipe {
                inputWriteGroup.enter()
                DispatchQueue.global(qos: .utility).async {
                    defer {
                        stdinPipe.fileHandleForWriting.closeFile()
                        inputWriteGroup.leave()
                    }
                    try? stdinPipe.fileHandleForWriting.write(contentsOf: input)
                }
            }
            let completed = waitForProcess(
                process,
                timeoutSeconds: max(1, timeoutSeconds)
            )
            if !completed {
                _ = inputWriteGroup.wait(timeout: .now() + configEditorHelperDrainTimeoutSeconds)
                _ = drainGroup.wait(timeout: .now() + configEditorHelperDrainTimeoutSeconds)
                let detail = helperFailureMessage(
                    stdout: stdoutCollector,
                    stderr: stderrCollector,
                    fallback: ""
                )
                let timeout = "Config editor timed out after \(Int(max(1, timeoutSeconds))) seconds and was terminated."
                throw ConfigEditorError(
                    message: detail.isEmpty ? timeout : "\(timeout)\n\(detail)"
                )
            }
        } catch let error as ConfigEditorError {
            throw error
        } catch {
            _ = inputWriteGroup.wait(timeout: .now() + configEditorHelperDrainTimeoutSeconds)
            _ = drainGroup.wait(timeout: .now() + configEditorHelperDrainTimeoutSeconds)
            throw ConfigEditorError(message: String(describing: error))
        }

        _ = inputWriteGroup.wait(timeout: .now() + configEditorHelperDrainTimeoutSeconds)
        let drained = drainGroup.wait(timeout: .now() + configEditorHelperDrainTimeoutSeconds) == .success
        guard drained else {
            stdoutPipe.fileHandleForReading.closeFile()
            stderrPipe.fileHandleForReading.closeFile()
            throw ConfigEditorError(
                message: "Config editor output did not finish draining after the process exited."
            )
        }

        if process.terminationStatus != 0 {
            throw ConfigEditorError(
                message: helperFailureMessage(
                    stdout: stdoutCollector,
                    stderr: stderrCollector,
                    fallback: "config_editor.py failed"
                )
            )
        }
        if stdoutCollector.didTruncate {
            throw ConfigEditorError(
                message: "Config editor output exceeded \(configEditorHelperOutputLimitBytes / (1_024 * 1_024)) MB. Reduce the configuration size before reopening the editor."
            )
        }
        return stdoutCollector.collectedData()
    }

}
