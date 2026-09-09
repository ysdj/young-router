/**
 * The provider wizard is the one sub-sheet that keeps explicit Next/Finish
 * actions. Its steps stage provider, key, and model drafts, but the underlying
 * provider workspace must not auto-apply those partial edits while the sheet
 * is open: a provider is staged before its key and models exist, and an
 * immediate apply would reload the proxy on an incomplete configuration.
 *
 * Every React root shares one JS runtime, so a module-level gate is enough.
 * The wizard root opens the gate while it is mounted and closes it on unmount,
 * which lets the provider workspace apply the finished draft immediately.
 */
let providerWizardOpen = false;
let assistantEditorOpen = false;
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

export function subscribeProviderWizard(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
