import Cocoa

private var presentedSettingsWindows: Set<ObjectIdentifier> = []

func beginSettingsWindowPresentation(_ window: NSWindow) {
    presentedSettingsWindows.insert(ObjectIdentifier(window))
    NSApp.setActivationPolicy(.regular)
    NSApp.activate(ignoringOtherApps: true)
    window.level = .normal
}

func endSettingsWindowPresentation(_ window: NSWindow) {
    presentedSettingsWindows.remove(ObjectIdentifier(window))
    if presentedSettingsWindows.isEmpty {
        NSApp.setActivationPolicy(.accessory)
    }
}

func performOnMainRunLoop(_ action: @escaping () -> Void) {
    RunLoop.main.perform(inModes: [.default, .modalPanel]) {
        action()
    }
}

func runSettingsModal(_ alert: NSAlert) -> NSApplication.ModalResponse {
    let window = alert.window
    beginSettingsWindowPresentation(window)
    let response = alert.runModal()
    endSettingsWindowPresentation(window)
    return response
}

func runSettingsModal(_ panel: NSSavePanel) -> NSApplication.ModalResponse {
    beginSettingsWindowPresentation(panel)
    let response = panel.runModal()
    endSettingsWindowPresentation(panel)
    return response
}
