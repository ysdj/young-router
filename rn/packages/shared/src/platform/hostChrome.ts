/**
 * The host's own surfaces — confirm/prompt dialogs, the record viewer, the
 * model picker, the anchored menu — as requests in a store rather than DOM.
 *
 * The native hosts express these as AppKit/WinUI surfaces. The browser host
 * used to build them with `document.createElement`, which meant a second,
 * hand-written stylesheet beside the app's own: the same question looked like a
 * different app, the read-only viewer's script never ran under the page's own
 * CSP, and each surface installed its own Escape listener so one Escape closed
 * every open layer at once.
 *
 * They are now drawn by `ui/HostChrome.tsx` with the same controls the panes
 * use, and Escape is answered once, by the topmost surface, through
 * `presentationStack`.
 *
 * This module imports no renderer: nothing under `packages/shared` may import
 * `react-dom`, or the two native bundles would try to resolve it.
 */

export type HostChromeMenuItem = { title: string; enabled?: boolean; checked?: boolean };

export type HostChromeAnchor = { x: number; y: number; width: number; height: number };

type Base = { id: number };

export type HostChromeRequest =
  | (Base & { kind: "confirm"; title: string; message: string; confirmLabel: string; cancelLabel: string; destructive: boolean; resolve: (accepted: boolean) => void })
  | (Base & { kind: "prompt"; title: string; value: string; confirmLabel: string; cancelLabel: string; resolve: (value: string | null) => void })
  | (Base & { kind: "viewer"; title: string; text: string; closeLabel: string; resolve: () => void })
  | (Base & { kind: "picker"; title: string; models: string[]; providerName: string; keyName: string; confirmLabel: string; cancelLabel: string; resolve: (models: string[] | undefined) => void })
  | (Base & { kind: "menu"; title: string; items: HostChromeMenuItem[]; anchor: HostChromeAnchor; resolve: (index: number | undefined) => void });

/** A request before the store assigns its id and settle callback. */
type RequestBody = Omit<Extract<HostChromeRequest, { kind: "confirm" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "prompt" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "viewer" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "picker" }>, "id" | "resolve">
  | Omit<Extract<HostChromeRequest, { kind: "menu" }>, "id" | "resolve">;

let requests: HostChromeRequest[] = [];
let serial = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function hostChromeSnapshot(): HostChromeRequest[] {
  return requests;
}

export function subscribeHostChrome(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** The surface a key press belongs to: the newest request still open. */
export function topHostChrome(): HostChromeRequest | undefined {
  return requests[requests.length - 1];
}

/** The answer a dismissing action gives, per surface kind. */
export function dismissalFor(request: HostChromeRequest): unknown {
  switch (request.kind) {
    case "confirm": return false;
    case "prompt": return null;
    case "viewer": return undefined;
    case "picker": return undefined;
    case "menu": return undefined;
  }
}

/**
 * Ask the page for one answer, drawn by `HostChrome`.
 *
 * The promise settles exactly once: by the surface's own answer, by Return or
 * Escape, or by `dismissHostChrome` when the layer underneath it went away.
 * The request leaves the stack before its answer is delivered, so a caller that
 * opens another surface from the answer starts from a clean stack.
 */
export function requestHostChrome<T>(body: RequestBody): Promise<T> {
  return new Promise<T>((resolve) => {
    const id = ++serial;
    const request = {
      ...body,
      id,
      resolve: (answer: unknown) => {
        if (!requests.some((entry) => entry.id === id)) return;
        requests = requests.filter((entry) => entry.id !== id);
        emit();
        resolve(answer as T);
      },
    } as HostChromeRequest;
    requests = [...requests, request];
    emit();
  });
}

/** Answer one open request. Answering the same request twice is a no-op. */
export function settleHostChrome(request: HostChromeRequest, answer: unknown): void {
  (request.resolve as (value: unknown) => void)(answer);
}

/** Close every open request with its dismissing answer, topmost first. */
export function dismissHostChrome(): void {
  for (const request of [...requests].reverse()) settleHostChrome(request, dismissalFor(request));
}
