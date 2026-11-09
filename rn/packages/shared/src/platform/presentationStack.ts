/**
 * The browser host's one modal stack.
 *
 * A page has no windows, so every child surface — the provider wizard, the raw
 * editor, the read-only record viewer, a decision dialog — is a layer in this
 * document.  Layers can stack (a record viewer opened over the wizard), and
 * Escape belongs to exactly one of them: the topmost.
 *
 * Each surface used to install its own capture-phase Escape listener, and
 * `stopPropagation()` does not stop a sibling listener on the same element, so
 * one Escape closed every open layer at once.  One stack, one listener, one
 * answer.
 */

type Presentation = {
  dismiss: () => void;
  /** Keep keyboard focus inside this layer while it is the topmost one. */
  trap?: (event: KeyboardEvent) => void;
  /** Keys the layer handles itself once Escape and Tab are accounted for. */
  key?: (event: KeyboardEvent) => void;
};

const stack: Presentation[] = [];
let listening = false;

const onKeyDown = (event: KeyboardEvent): void => {
  const top = stack[stack.length - 1];
  if (top === undefined) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopImmediatePropagation();
    dismissTopPresentation();
    return;
  }
  if (event.key === "Tab") {
    top.trap?.(event);
    return;
  }
  top.key?.(event);
};

/**
 * Put one layer on top of the stack.  The returned function removes it; calling
 * it twice is harmless, and removing a layer that is not on top (a dialog
 * dismissed by its own button while a layer above it is still open) leaves the
 * order of the rest intact.
 */
export function pushPresentation(presentation: Presentation): () => void {
  if (!listening) {
    // Capture, so a pane's own key handling never sees the key first.
    document.addEventListener("keydown", onKeyDown, true);
    listening = true;
  }
  stack.push(presentation);
  return () => {
    const index = stack.lastIndexOf(presentation);
    if (index >= 0) stack.splice(index, 1);
  };
}

/**
 * Answer Escape on the surface a person is looking at.
 *
 * Exported because a key pressed in another document never arrives here: the
 * raw editor is a frame with its own listener, and it asks for this answer
 * instead of deciding for itself which layer an Escape closes.  A surface
 * that states a "may I close?" runs that question, exactly as the key does.
 */
export function dismissTopPresentation(): void {
  stack[stack.length - 1]?.dismiss();
}

const FOCUSABLE = "button, input, select, textarea, [tabindex]:not([tabindex='-1'])";

/**
 * Keep Tab inside one element while it is the topmost layer.
 *
 * Three surfaces needed the same cycle — the child layers, the host's own
 * dialogs and the relay dialogs — and each used to answer keys itself, so one
 * surface's Tab could move focus out of the one a person was looking at.  The
 * wrap is expressed once here and handed to `pushPresentation`.
 *
 * A container with nothing focusable keeps the keys on its first control
 * (usually the close button) rather than letting focus escape to the page
 * behind the scrim.
 */
export function trapWithin(container: HTMLElement, fallback?: HTMLElement | null): (event: KeyboardEvent) => void {
  return (event: KeyboardEvent): void => {
    const controls = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((element) => !element.hasAttribute("disabled"));
    const first = controls[0] ?? fallback ?? null;
    const last = controls[controls.length - 1] ?? fallback ?? null;
    const active = document.activeElement;
    if (first === null || last === null) return;
    if (event.shiftKey && (active === first || !container.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !container.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };
}
