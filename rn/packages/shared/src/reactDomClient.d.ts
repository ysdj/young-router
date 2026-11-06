/**
 * The one DOM-renderer declaration the browser entry needs.
 *
 * `platformEntry.web.ts` is the only module under `packages/shared` that mounts
 * a React root of its own (the browser host's child layers). The native hosts
 * render through `RCTRootView`; a page renders through `react-dom`.
 *
 * The declaration lives here rather than in `@types/react-dom` because the
 * shared TypeScript project is the one the two native apps type-check, and
 * neither of them has `react-dom` installed: importing its types there would
 * add a browser dependency to a desktop build. This is the whole surface the
 * entry uses, so a mismatch is a compile error in the caller, not a silent
 * `undefined` at runtime.
 */
declare module "react-dom/client" {
  import type { ReactNode } from "react";

  export interface Root {
    render(children: ReactNode): void;
    unmount(): void;
  }

  export function createRoot(container: Element | DocumentFragment, options?: { onRecoverableError?: (error: unknown) => void }): Root;
}
