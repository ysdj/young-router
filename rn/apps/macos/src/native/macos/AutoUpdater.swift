import AppKit
import OSLog

private let updateLog = Logger(subsystem: "young.router.app", category: "auto-update")

struct YoungRouterReleaseVersion: Comparable, Sendable {
    let text: String
    private let parts: [Int]

    init?(_ value: String) {
        let components = value.split(separator: ".", omittingEmptySubsequences: false)
        guard components.count == 3 else { return nil }
        let parts = components.compactMap(Self.number)
        guard parts.count == 3 else { return nil }
        text = value
        self.parts = parts
    }

    static func number(_ value: Substring) -> Int? {
        guard !value.isEmpty, value.utf8.allSatisfy({ (48...57).contains($0) }),
              value.count == 1 || value.first != "0" else { return nil }
        return Int(value)
    }

    static func < (lhs: Self, rhs: Self) -> Bool {
        lhs.parts.lexicographicallyPrecedes(rhs.parts)
    }
}

struct YoungRouterRelease: Decodable {
    struct Asset: Decodable {
        let name: String
        let state: String
    }

    static let repository = "ysdj/young-router"
    let tagName: String
    let draft: Bool
    let prerelease: Bool
    let assets: [Asset]

    static func newest(
        in releases: [Self], after installed: (YoungRouterReleaseVersion, Int), architecture: String
    ) -> YoungRouterUpdate? {
        releases.lazy.flatMap { $0.updates(architecture: architecture) }
            .filter { $0.order > installed }
            .max { $0.order < $1.order }
    }

    private func updates(architecture: String) -> [YoungRouterUpdate] {
        let versionText = tagName.hasPrefix("v") ? String(tagName.dropFirst()) : tagName
        guard !draft, !prerelease, let version = YoungRouterReleaseVersion(versionText) else { return [] }
        let prefix = "young-router-\(version.text)-"
        let suffix = "-macos-\(architecture).zip"
        return assets.compactMap { asset in
            guard asset.state == "uploaded", asset.name.hasPrefix(prefix), asset.name.hasSuffix(suffix),
                  asset.name.count > prefix.count + suffix.count,
                  let build = YoungRouterReleaseVersion.number(asset.name.dropFirst(prefix.count).dropLast(suffix.count)),
                  build > 0,
                  let url = URL(string: "https://github.com/\(Self.repository)/releases/download/\(tagName)/\(asset.name)") else {
                return nil
            }
            return YoungRouterUpdate(version: version, build: build, url: url)
        }
    }
}

struct YoungRouterUpdate: Sendable {
    #if arch(arm64)
    static let architecture = "arm64"
    static let cpuType = CPU_TYPE_ARM64
    #else
    static let architecture = "x86_64"
    static let cpuType = CPU_TYPE_X86_64
    #endif

    let version: YoungRouterReleaseVersion
    let build: Int
    let url: URL

    var order: (YoungRouterReleaseVersion, Int) {
        (version, build)
    }

    static func isStableBundle(_ bundle: Bundle) -> Bool {
        bundle.object(forInfoDictionaryKey: "YoungRouterReleaseChannel") as? String == "stable"
            && bundle.object(forInfoDictionaryKey: "YoungRouterRouteScheme") as? String == "young-router"
            && bundle.object(forInfoDictionaryKey: "YoungRouterPreviewProfileRoot") == nil
            && bundle.object(forInfoDictionaryKey: "YoungRouterPreviewPort") == nil
    }
}

/// GitHub serves binaries through its release asset CDN. Keep the repository
/// and the TLS origins fixed; no URL comes from a setting or an IPC request.
private final class ReleaseAssetRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        let host = request.url?.host
        let allowed = task.originalRequest?.url?.host == "github.com"
            && request.url?.scheme == "https"
            && (host == "release-assets.githubusercontent.com" || host == "objects.githubusercontent.com")
        completionHandler(allowed ? request : nil)
    }
}

@MainActor
final class YoungRouterAutoUpdater {
    private static let interval = Duration.seconds(6 * 60 * 60)

    private let redirectDelegate = ReleaseAssetRedirects()
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 30 * 60
        configuration.httpAdditionalHeaders = ["User-Agent": "Young-Router-Auto-Updater"]
        return URLSession(configuration: configuration, delegate: redirectDelegate, delegateQueue: nil)
    }()
    private var checks: Task<Void, Never>?
    private var terminating = false

    func setEnabled(_ enabled: Bool) {
        guard enabled else {
            checks?.cancel()
            checks = nil
            return
        }
        guard checks == nil, !terminating, canUpdate else { return }
        checks = Task {
            while !Task.isCancelled {
                do {
                    try await check()
                } catch {
                    if !Task.isCancelled { updateLog.error("Update check failed: \(error.localizedDescription)") }
                }
                try? await Task.sleep(for: Self.interval)
            }
        }
    }

    func prepareForTermination() {
        terminating = true
        setEnabled(false)
    }

    private var canUpdate: Bool {
        let bundle = Bundle.main
        return YoungRouterUpdate.isStableBundle(bundle)
            && UserDefaults.standard.string(forKey: "MetroServerURL") == nil
            && FileManager.default.isWritableFile(atPath: bundle.bundleURL.deletingLastPathComponent().path)
            && (try? bundle.bundleURL.resourceValues(forKeys: [.volumeSupportsSwapRenamingKey]).volumeSupportsSwapRenaming) == true
    }

    private func check() async throws {
        let bundle = Bundle.main
        guard canUpdate,
              let installedID = bundle.bundleIdentifier,
              let executable = bundle.executableURL,
              let installedVersionText = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
              let installedVersion = YoungRouterReleaseVersion(installedVersionText),
              let installedBuildText = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String,
              let installedBuild = Int(installedBuildText),
              let update = try await newestUpdate(after: (installedVersion, installedBuild)) else { return }
        try Task.checkCancellation()

        let (archive, response) = try await session.download(from: update.url)
        defer { try? FileManager.default.removeItem(at: archive) }
        try Self.requireSuccess(response)
        try Task.checkCancellation()
        let staged = YoungRouterStagedUpdate(installed: bundle.bundleURL)
        try await staged.prepare(archive: archive, update: update, bundleID: installedID)
        do {
            try Task.checkCancellation()
            guard canUpdate else {
                await staged.discard()
                return
            }
            try staged.launchHelper(executable: executable)
        } catch {
            await staged.discard()
            throw error
        }
        NSApp.terminate(nil)
    }

    private func newestUpdate(after installed: (YoungRouterReleaseVersion, Int)) async throws -> YoungRouterUpdate? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let pageSize = 100
        var page = 1
        var latest: YoungRouterUpdate?
        while true {
            try Task.checkCancellation()
            let url = URL(string: "https://api.github.com/repos/\(YoungRouterRelease.repository)/releases?per_page=\(pageSize)&page=\(page)")!
            var request = URLRequest(url: url)
            request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
            let (data, response) = try await session.data(for: request)
            try Self.requireSuccess(response)
            let releases = try decoder.decode([YoungRouterRelease].self, from: data)
            if let newer = YoungRouterRelease.newest(
                in: releases, after: latest?.order ?? installed, architecture: YoungRouterUpdate.architecture
            ) {
                latest = newer
            }
            // GitHub orders this feed by publication data, not by version.
            if releases.count < pageSize { return latest }
            page += 1
        }
    }

    private static func requireSuccess(_ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
    }
}

/// A private sibling keeps the two app bundles on the same volume. The only
/// directory the updater removes is the one it creates for this download.
struct YoungRouterStagedUpdate: Sendable {
    private static let prefix = ".young-router-update-"
    let installed: URL
    let directory: URL
    var app: URL { directory.appendingPathComponent("Young Router.app", isDirectory: true) }

    init(installed: URL, id: UUID = UUID()) {
        self.installed = installed
        directory = installed.deletingLastPathComponent()
            .appendingPathComponent(Self.prefix + id.uuidString, isDirectory: true)
    }

    func prepare(archive: URL, update: YoungRouterUpdate, bundleID: String) async throws {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        do {
            try await Self.runTool("/usr/bin/ditto", ["-x", "-k", archive.path, directory.path])
            try Task.checkCancellation()
            guard let next = Bundle(url: app),
                  try app.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == false,
                  YoungRouterUpdate.isStableBundle(next), next.bundleIdentifier == bundleID,
                  next.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String == update.version.text,
                  next.object(forInfoDictionaryKey: "CFBundleVersion") as? String == String(update.build),
                  let executable = next.executableURL, fm.isExecutableFile(atPath: executable.path),
                  next.executableArchitectures?.contains(NSNumber(value: YoungRouterUpdate.cpuType)) == true else {
                throw CocoaError(.fileReadCorruptFile)
            }
            // Releases currently use ad hoc signing. HTTPS to the fixed GitHub
            // repository authenticates the publisher; codesign checks the seal.
            try await Self.runTool("/usr/bin/codesign", ["--verify", "--deep", "--strict", app.path])
        } catch {
            await discard()
            throw error
        }
    }

    func discard() async {
        try? FileManager.default.removeItem(at: directory)
    }

    func launchHelper(executable: URL) throws {
        let process = Process()
        process.executableURL = executable
        process.arguments = ["--install-stable-update", String(directory.lastPathComponent.dropFirst(Self.prefix.count)), String(getpid())]
        process.standardInput = FileHandle.nullDevice
        try process.run()
    }

    /// Exchange whole bundles atomically, so the installed path never goes
    /// missing. If Launch Services refuses to open the new app, restore the old
    /// one before the helper discards the staging directory.
    func replace(relaunch: (URL) throws -> Void) throws {
        var canDiscard = true
        defer { if canDiscard { try? FileManager.default.removeItem(at: directory) } }
        do {
            try exchange()
        } catch {
            try relaunch(installed)
            throw error
        }
        do {
            try relaunch(installed)
        } catch {
            canDiscard = false
            try exchange()
            canDiscard = true
            try relaunch(installed)
            throw error
        }
    }

    private func exchange() throws {
        let status = renamex_np(installed.path, app.path, UInt32(RENAME_SWAP))
        if status != 0 { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    }

    private static func runTool(_ path: String, _ arguments: [String]) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = arguments
            process.terminationHandler = { child in
                if child.terminationStatus == 0 {
                    continuation.resume()
                } else {
                    continuation.resume(throwing: CocoaError(.executableLoad))
                }
            }
            do { try process.run() } catch { continuation.resume(throwing: error) }
        }
    }
}

/// Started directly as a child of the running app, before the normal AppKit
/// startup and single-instance check. It never needs to load Core or React.
@objc(YoungRouterUpdateHelper)
public final class YoungRouterUpdateHelper: NSObject {
    @objc(installWithID:parentPID:)
    public static func install(id: String, parentPID: String) -> Int32 {
        guard let id = UUID(uuidString: id), let pid = Int32(parentPID), pid > 1,
              getppid() == pid || getppid() == 1 else { return EXIT_FAILURE }
        let staged = YoungRouterStagedUpdate(installed: Bundle.main.bundleURL, id: id)
        let deadline = ProcessInfo.processInfo.systemUptime + 5 * 60
        while getppid() == pid {
            guard ProcessInfo.processInfo.systemUptime < deadline else {
                try? FileManager.default.removeItem(at: staged.directory)
                updateLog.error("The running app did not exit in time to install the update")
                return EXIT_FAILURE
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        do {
            try staged.replace { app in
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
                process.arguments = ["-n", app.path]
                try process.run()
                process.waitUntilExit()
                guard process.terminationStatus == 0 else { throw CocoaError(.executableLoad) }
            }
            return EXIT_SUCCESS
        } catch {
            updateLog.error("Could not install the update: \(error.localizedDescription)")
            return EXIT_FAILURE
        }
    }
}
