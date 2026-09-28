/**
 * The provider wizard is the one sub-sheet that keeps explicit Next/Finish
 * actions. Its steps stage the provider, its key, and its models, and 完成 is
 * the one act that creates them (a provider the wizard is making does not
 * exist before that), so the underlying provider workspace must not auto-apply
 * those partial edits while the sheet is open: an immediate apply would reload
 * the proxy on an incomplete configuration.
 *
 * Every React root shares one JS runtime, so a module-level gate is enough.
 * The wizard root opens the gate while it is mounted and closes it on unmount,
 * which lets the provider workspace apply the finished draft immediately.
 */
let providerWizardOpen = false;
let assistantEditorOpen = false;
let groupManagerOpen = false;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export function setProviderWizardOpen(open: boolean): void {
  if (providerWizardOpen === open) return;
  providerWizardOpen = open;
  notify();
}

export function isProviderWizardOpen(): boolean {
  return providerWizardOpen;
}

/**
 * The raw file editor is a sub-sheet with an explicit Save action. While it is
 * open the main pane must not auto-apply: the editor's staged text should only
 * reach Core when the user presses Save (or when the sheet closes).
 */
export function setAssistantEditorOpen(open: boolean): void {
  if (assistantEditorOpen === open) return;
  assistantEditorOpen = open;
  notify();
}

export function isAssistantEditorOpen(): boolean {
  return assistantEditorOpen;
}

/**
 * 分组管理 is a native child sheet with its own 保存并关闭, and it carries its
 * own status bar. Its relay writes are applied by the sheet while it is still
 * on screen, so the pane must not auto-apply the same draft underneath it: the
 * child states the outcome, the window that opened it states nothing.
 */
export function setGroupManagerOpen(open: boolean): void {
  if (groupManagerOpen === open) return;
  groupManagerOpen = open;
  notify();
}

export function isGroupManagerOpen(): boolean {
  return groupManagerOpen;
}

export function subscribeProviderWizard(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
