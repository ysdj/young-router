import type { AppRoute, NativeLeafAdapter, NativeMenuAction, ServiceStatus } from "../types";

const noOpWindow = {
  open: (_route: AppRoute): void => undefined,
  close: (_route?: AppRoute): void => undefined,
  focus: (_route: AppRoute): void => undefined,
};

const noOpMenu = {
  setStatus: (_state: ServiceStatus): void => undefined,
  setActions: (_actions: NativeMenuAction[]): void => undefined,
};

export function createNativeLeafAdapter(overrides: Partial<NativeLeafAdapter> = {}): NativeLeafAdapter {
  return {
    window: overrides.window ?? noOpWindow,
    menuBar: overrides.menuBar ?? noOpMenu,
    tray: overrides.tray ?? noOpMenu,
    openFilePicker: overrides.openFilePicker ?? (async () => undefined),
    saveFilePicker: overrides.saveFilePicker ?? (async () => undefined),
    showConfirmation: overrides.showConfirmation ?? (async () => false),
    chooseModelsToAdd: overrides.chooseModelsToAdd ?? (async () => undefined),
    editSecureDocument: overrides.editSecureDocument ?? (async () => undefined),
    editSecret: overrides.editSecret ?? (async () => undefined),
    clearSecret: overrides.clearSecret ?? (async () => undefined),
    setLaunchAtLogin: overrides.setLaunchAtLogin ?? (async () => undefined),
    setLocalization: overrides.setLocalization ?? (() => undefined),
    setShortcuts: overrides.setShortcuts ?? (() => undefined),
  };
}
