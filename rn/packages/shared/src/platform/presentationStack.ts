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
    top.dismiss();
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

/** Whether `presentation` is the layer Escape and Tab belong to right now. */
export function isTopPresentation(presentation: Presentation): boolean {
  return stack[stack.length - 1] === presentation;
}
