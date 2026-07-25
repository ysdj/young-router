import Cocoa

/// A local-only window route. The app stays a menu-bar accessory; this gives
/// accessibility/Computer Use a deterministic way to surface a normal native
/// window without guessing a status-bar coordinate.
enum LiteLLMMenuWindowRoute: String {
    case codexSettings = "codex-settings"
    case providersModels = "providers-models"
    case runtimeSettings = "runtime-settings"
    case logs = "logs"
}

extension AppDelegate {
    static let localWindowURLScheme = "litellm-menu"

    /// Accept only fixed local action URLs:
    /// litellm-menu://open/codex-settings, providers-models,
    /// runtime-settings, and logs?tab=service.
    static func localWindowRoute(for url: URL) -> LiteLLMMenuWindowRoute? {
        guard url.scheme?.localizedCaseInsensitiveCompare(localWindowURLScheme) == .orderedSame,
              url.host?.localizedCaseInsensitiveCompare("open") == .orderedSame
        else {
            return nil
        }
        let path = url.path
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .lowercased()
        return LiteLLMMenuWindowRoute(rawValue: path)
    }

    static func logTab(forLocalWindowURL url: URL) -> LogWindowTab {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let value = components.queryItems?.first(where: {
                  $0.name.localizedCaseInsensitiveCompare("tab") == .orderedSame
              })?.value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty
        else {
            return .requests
        }
        return LogWindowTab.allCases.first {
            $0.rawValue.localizedCaseInsensitiveCompare(value) == .orderedSame
                || $0.title.localizedCaseInsensitiveCompare(value) == .orderedSame
        } ?? .requests
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls {
            guard let route = Self.localWindowRoute(for: url) else { continue }
            openLocalWindow(route, logTab: Self.logTab(forLocalWindowURL: url))
        }
    }

    func openLocalWindow(_ route: LiteLLMMenuWindowRoute, logTab: LogWindowTab = .requests) {
        guard !terminationCleanupInFlight else { return }
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.openLocalWindow(route, logTab: logTab)
            }
            return
        }

        NSApp.activate(ignoringOtherApps: true)
        switch route {
        case .codexSettings:
            configureCodexSettings()
        case .providersModels:
            editModelsConfig()
        case .runtimeSettings:
            configureRuntimeSettings()
        case .logs:
            showLogs(tab: logTab)
        }
    }
}
