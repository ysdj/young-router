export { YoungRouterApp } from "./ui/YoungRouterApp";
export type { YoungRouterAppProps } from "./ui/YoungRouterApp";
export { createIpcClient } from "./ipc";
export { createTranslator, resolveLanguage } from "./i18n";
export { registerYoungRouter } from "./bootstrap";
export { createNativeIpcTransport, createNativeLeafBridgeAdapter } from "./platform/nativeBridge";
export type {
  CoreSnapshot,
  IpcClient,
  IpcEndpoint,
  IpcTransport,
  NativeLeafAdapter,
  NativeLocalization,
} from "./types";
