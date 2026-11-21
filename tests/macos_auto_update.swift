import Foundation

@main
struct MacOSAutoUpdateTests {
    static func main() async throws {
        try selectNewestStableRelease()
        try await replaceSignedBundleAndRestoreOnFailure()
    }

    static func selectNewestStableRelease() throws {
        let data = Data("""
        [
          {"tag_name":"v5.0.0-dev.1","draft":false,"prerelease":false,"assets":[{"name":"young-router-5.0.0-9-macos-arm64.zip","state":"uploaded"}]},
          {"tag_name":"v4.0.0","draft":false,"prerelease":true,"assets":[{"name":"young-router-4.0.0-9-macos-arm64.zip","state":"uploaded"}]},
          {"tag_name":"v3.0.0","draft":true,"prerelease":false,"assets":[{"name":"young-router-3.0.0-9-macos-arm64.zip","state":"uploaded"}]},
          {"tag_name":"v2.0.0","draft":false,"prerelease":false,"assets":[{"name":"young-router-2.0.0-9-macos-x86_64.zip","state":"uploaded"}]},
          {"tag_name":"v1.9.1","draft":false,"prerelease":false,"assets":[{"name":"young-router-1.9.1-99-macos-arm64.zip","state":"uploaded"}]},
          {"tag_name":"v1.10.0","draft":false,"prerelease":false,"assets":[
            {"name":"young-router-1.10.0-20-macos-arm64.zip","state":"starter"},
            {"name":"young-router-1.10.0-17-macos-arm64.zip","state":"uploaded"},
            {"name":"young-router-1.10.0-18-macos-arm64.zip","state":"uploaded"}]}
        ]
        """.utf8)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let releases = try decoder.decode([YoungRouterRelease].self, from: data)
        let newest = YoungRouterRelease.newest(in: releases, after: (YoungRouterReleaseVersion("1.9.1")!, 99), architecture: "arm64")
        precondition(newest?.url.path == "/ysdj/young-router/releases/download/v1.10.0/young-router-1.10.0-18-macos-arm64.zip")
        let sameVersion = YoungRouterReleaseVersion("1.10.0")!
        precondition(YoungRouterRelease.newest(in: releases, after: (sameVersion, 17), architecture: "arm64")?.build == 18)
        precondition(YoungRouterRelease.newest(in: releases, after: (sameVersion, 18), architecture: "arm64") == nil)
    }

    static func replaceSignedBundleAndRestoreOnFailure() async throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fm.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? fm.removeItem(at: root) }

        let installed = root.appendingPathComponent("Installed app.app", isDirectory: true)
        let incoming = root.appendingPathComponent("Young Router.app", isDirectory: true)
        let archive = root.appendingPathComponent("release.zip")
        try makeBundle(installed, version: "1.9.1", channel: "stable")
        let update = YoungRouterUpdate(version: YoungRouterReleaseVersion("1.10.0")!, build: 18, url: archive)

        try makeBundle(incoming, version: update.version.text, channel: "dev")
        try tool("/usr/bin/ditto", ["-c", "-k", "--sequesterRsrc", "--keepParent", incoming.path, archive.path])
        let dev = YoungRouterStagedUpdate(installed: installed)
        do {
            try await dev.prepare(archive: archive, update: update, bundleID: bundleID)
            preconditionFailure("Dev bundle accepted as a stable update")
        } catch let error as CocoaError where error.code == .fileReadCorruptFile {
            precondition(!fm.fileExists(atPath: dev.directory.path))
        }

        try fm.removeItem(at: incoming)
        try fm.removeItem(at: archive)
        try makeBundle(incoming, version: update.version.text, channel: "stable")
        try tool("/usr/bin/ditto", ["-c", "-k", "--sequesterRsrc", "--keepParent", incoming.path, archive.path])
        let rejected = YoungRouterStagedUpdate(installed: installed)
        try await rejected.prepare(archive: archive, update: update, bundleID: bundleID)
        var opened: [String] = []
        do {
            try rejected.replace { app in
                let version = try readVersion(app)
                opened.append(version)
                if version == update.version.text { throw CocoaError(.executableLoad) }
            }
            preconditionFailure("An update that refused to launch was reported as installed")
        } catch let error as CocoaError where error.code == .executableLoad {
            precondition(opened == ["1.10.0", "1.9.1"])
            precondition(!fm.fileExists(atPath: rejected.directory.path))
        }

        let accepted = YoungRouterStagedUpdate(installed: installed)
        try await accepted.prepare(archive: archive, update: update, bundleID: bundleID)
        try accepted.replace { app in opened.append(try readVersion(app)) }
        precondition(opened.last == "1.10.0")
        precondition(!fm.fileExists(atPath: accepted.directory.path))
        precondition(fm.fileExists(atPath: archive.path))
    }

    static let bundleID = "test.young-router.auto-update"

    static func makeBundle(_ url: URL, version: String, channel: String) throws {
        let fm = FileManager.default
        let executableDirectory = url.appendingPathComponent("Contents/MacOS", isDirectory: true)
        try fm.createDirectory(at: executableDirectory, withIntermediateDirectories: true)
        try fm.copyItem(at: URL(fileURLWithPath: "/usr/bin/true"), to: executableDirectory.appendingPathComponent("Fixture"))
        let plist = [
            "CFBundleIdentifier": bundleID, "CFBundleExecutable": "Fixture", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version, "CFBundleVersion": "18",
            "YoungRouterReleaseChannel": channel, "YoungRouterRouteScheme": "young-router",
        ]
        try PropertyListEncoder().encode(plist).write(to: url.appendingPathComponent("Contents/Info.plist"))
        try tool("/usr/bin/codesign", ["--force", "--sign", "-", url.path])
    }

    static func readVersion(_ url: URL) throws -> String {
        let data = try Data(contentsOf: url.appendingPathComponent("Contents/Info.plist"))
        return try PropertyListDecoder().decode([String: String].self, from: data)["CFBundleShortVersionString"]!
    }

    static func tool(_ path: String, _ arguments: [String]) throws {
        let child = Process()
        child.executableURL = URL(fileURLWithPath: path)
        child.arguments = arguments
        try child.run()
        child.waitUntilExit()
        precondition(child.terminationStatus == 0, "\(path) failed")
    }
}
