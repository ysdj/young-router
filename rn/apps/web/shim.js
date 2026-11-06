/**
 * The bridge between `react-native` and `react-native-web`.
 *
 * Every module under `packages/shared` imports `"react-native"`; this file is
 * what that specifier resolves to in the browser bundle. It re-exports React
 * Native for Web unchanged and adds the three names the shared UI uses that it
 * does not publish:
 *
 *  - `codegenNativeComponent` — the native hosts register a Fabric component
 *    here. A browser has no Fabric, so the call returns a `View`: those
 *    wrappers (`AppKitControls.tsx`, `ui/macos/*`) are only ever imported by the
 *    native branches of `NativeControls`, and the browser never renders them.
 *    Answering with `View` rather than throwing keeps the module graph loadable,
 *    which matters because the imports are static.
 *  - `codegenNativeCommands` — same reasoning, for the command record.
 *  - `PlatformColor` — AppKit and WinUI semantic colors. The shared UI already
 *    passes a literal fallback for the third host (`semanticColor`), so this
 *    answers the literal it is given, which is what browsers can paint.
 *
 * Nothing else is shimmed: a missing component should fail loudly in the build
 * rather than render an empty box.
 */

export * from "react-native-web";

import { View } from "react-native-web";

export function codegenNativeComponent() {
  return View;
}

export function codegenNativeCommands() {
  return {};
}

export function PlatformColor(name) {
  return name;
}
