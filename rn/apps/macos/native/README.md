# AppKit leaf

`apps/macos/src/native/macos/AppKitNativeLeaf.swift` owns the status item, menu, settings
window geometry, file panels, confirmation alerts, split view, segmented
control, and monospace editor. `AppKitNativeLeafModule.swift` exposes the
imperative surface to the shared React tree through the RN macOS bridge.

The file-panel methods return one-time selection tokens. `AppKitNativeLeafModule`
registers selected URLs directly with `CoreIPCBridge` before the opaque token
reaches RN, so a filesystem path never enters UI state, logs, or ordinary errors.
Sensitive editor text follows the same native-only read/edit/stage boundary;
React receives only the editor token and the staged revision.

The Objective-C bridges and Swift sources are registered in the checked-in
`LiteLLMMenu-macOS` Xcode target. Its release build embeds a relocatable Python
Core runtime under `Contents/Resources/Core`.
