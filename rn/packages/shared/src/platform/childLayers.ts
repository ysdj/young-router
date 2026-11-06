/**
 * The browser host's child surfaces.
 *
 * The native hosts present the provider wizard, the raw document editor and
 * 分组管理 as their own movable windows over the settings window, and the
 * reasons are recorded in AGENTS.md: an overlay over a pane is clipped by that
 * pane, cannot be dragged, and leaves the window behind it alive. A browser has
 * no windows, so the same surface is drawn here as one layer over the page:
 * fixed to the viewport so no pane can clip it, carrying its own title bar so
 * it can be dragged, and locking the page behind it while it is open.
 *
 * It is not a route and it is not an address. Every route is one address in
 * this host, so a child surface that took one would add an entry to browser
 * history — Back would then step out of the dialog instead of out of the app.
 *
 * This module is DOM only. The React root inside each layer is mounted by the
 * caller (`platformEntry.web.ts`), which is where `react-dom` belongs: nothing
 * under `packages/shared` may import a web renderer, or the two native bundles
 * would try to resolve it.
 */

import type { AppRoute } from "../types";
import { pushPresentation } from "./presentationStack";

const LAYER_SIZE: Partial<Record<AppRoute, { width: number; height: number }>> = {
  // The native route layouts, so the same pane content fits before its first
  // scrollbar appears.
  "provider-wizard": { width: 620, height: 460 },
  "file-editor": { width: 900, height: 560 },
  "webdav-settings": { width: 600, height: 470 },
};
const DEFAULT_SIZE = { width: 720, height: 520 };
const MARGIN = 24;

type Layer = {
  scrim: HTMLElement;
  close: () => void;
};

const layers = new Map<AppRoute, Layer>();

export type ChildLayerOptions = {
  route: AppRoute;
  title: string;
  closeLabel: string;
  /**
   * Mount the route's own React root into `container`, and return the function
   * that unmounts it. The caller supplies this so the shared package never
   * imports a DOM renderer.
   */
  mount: (container: HTMLElement) => () => void;
  onDismiss: () => void;
};

/**
 * Present `route` over the page, or focus the layer that already shows it.
 *
 * `mount` is given the layer's body element, so the caller renders the very
 * component a native host would have mounted in a new window — same props,
 * same route, same entry point.
 */
export function presentChildLayer(options: ChildLayerOptions): void {
  const existing = layers.get(options.route);
  if (existing) {
    // Asking again is the same window being focused, which is what
    // `focusWindow` means on the native hosts.
    existing.scrim.style.display = "";
    return;
  }

  const size = LAYER_SIZE[options.route] ?? DEFAULT_SIZE;

  // Fixed to the viewport, so the pane that opened it cannot clip it — the
  // failure an in-pane overlay has on both native hosts.
  const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const root = document.getElementById("root");
  if (root) root.inert = true;

  const scrim = document.createElement("div");
  // `!important`: React Native for Web injects its own base rules for `body`,
  // `div` and friends, and a plain inline `display` there loses the cascade to
  // a later sheet — which left this layer pinned to the viewport's top-left
  // corner instead of centred. An attribute sheet cannot outrank `!important`,
  // and this is the one element in the page that is not React's to style.
  scrim.style.cssText = [
    "position:fixed!important", "inset:0!important", "z-index:1000!important",
    "align-items:center!important", "justify-content:center!important",
    "margin:0!important", "padding:0!important",
    "background:rgba(0,0,0,0.28)",
  ].join(";");
  // Set through the property, not the `cssText` shorthand: a declaration list
  // that also carries `!important` elsewhere drops this one, and the layer then
  // renders as a block at the viewport's top-left corner.
  scrim.style.setProperty("display", "flex", "important");

  const frame = document.createElement("div");
  frame.setAttribute("role", "dialog");
  frame.setAttribute("aria-modal", "true");
  frame.setAttribute("aria-label", options.title);
  frame.tabIndex = -1;
  frame.style.cssText = [
    `width:min(${size.width}px, calc(100vw - ${MARGIN * 2}px))`,
    `height:min(${size.height}px, calc(100vh - ${MARGIN * 2}px))`,
    // Centred by geometry, not by the scrim's flex: a page's own stylesheet can
    // win the cascade over the scrim's `display`, and a layer that loses it then
    // sits in the corner. Absolute offsets do not depend on that.
    "position:absolute", "left:50%", "top:50%",
    "display:flex", "flex-direction:column", "overflow:hidden",
    "border-radius:8px", "border:1px solid var(--yr-separator, #d4d4d8)",
    "background:var(--yr-control, #ffffff)",
    "box-shadow:0 12px 32px rgba(0,0,0,0.28)",
    // The one placement that must survive any stylesheet: dead centre.
    "transform:translate(-50%,-50%)!important",
  ].join(";");

  // The title bar is the drag handle and carries the one control that dismisses
  // the layer — the same two things a native child panel offers.
  const titleBar = document.createElement("div");
  titleBar.style.cssText = [
    "flex:0 0 auto", "display:flex", "align-items:center", "gap:8px",
    "height:30px", "padding:0 6px 0 12px",
    "border-bottom:1px solid var(--yr-separator, #d4d4d8)",
    "background:var(--yr-window, #f3f3f3)", "cursor:grab", "user-select:none",
  ].join(";");
  const title = document.createElement("div");
  title.textContent = options.title;
  title.style.cssText = "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:600 13px/1.4 system-ui, sans-serif";
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "\u00d7";
  close.setAttribute("aria-label", options.closeLabel);
  close.style.cssText = "width:22px;height:22px;display:flex;align-items:center;justify-content:center;border:0;border-radius:4px;background:transparent;color:var(--yr-secondary-label, #5d5d5d);font:16px/18px system-ui, sans-serif;cursor:default";
  titleBar.append(title, close);

  const body = document.createElement("div");
  body.style.cssText = "flex:1;min-height:0;display:flex;overflow:hidden";
  frame.append(titleBar, body);
  scrim.append(frame);
  document.body.append(scrim);

  // The page behind must not scroll while the layer is up, exactly as
  // `NativeChildPanelShield` keeps the parent window from being used. Restoring
  // the previous value matters: layers can be dismissed out of order.
  const previousOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";

  const unmount = options.mount(body);
  close.focus();

  const dismiss = (): void => {
    if (!layers.delete(options.route)) return;
    releasePresentation();
    unmount();
    scrim.remove();
    if (layers.size === 0) {
      if (root) root.inert = false;
      previouslyFocused?.focus();
    }
    document.body.style.overflow = previousOverflow;
    options.onDismiss();
  };

  // Escape and Tab belong to the topmost layer only: the stack answers once,
  // for the surface a person is actually looking at.
  const trap = (event: KeyboardEvent): void => {
    if (event.key !== "Tab") return;
    const controls = Array.from(frame.querySelectorAll<HTMLElement>("button, input, select, textarea, [tabindex]:not([tabindex='-1'])")).filter((element) => !element.hasAttribute("disabled"));
    const first = controls[0] ?? close;
    const last = controls[controls.length - 1] ?? close;
    if (event.shiftKey && (document.activeElement === first || !frame.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !frame.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  };
  const releasePresentation = pushPresentation({ dismiss, trap });
  close.addEventListener("click", dismiss);

  // Dragging, the gesture a native child panel accepts on its title bar. The
  // layer is centred by layout, so the drag is clamped to the room the viewport
  // actually has around it rather than to the window's own edges.
  let drag: { pointer: number; x: number; y: number } | undefined;
  titleBar.addEventListener("pointerdown", (event) => {
    if (event.target === close) return;
    drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY };
    titleBar.setPointerCapture(event.pointerId);
    titleBar.style.cursor = "grabbing";
  });
  titleBar.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointer !== event.pointerId) return;
    // The layer is centred by the flex container, so its travel is the space
    // the viewport has on each side of that centred position — half the gap,
    // not the gap. Clamping to the unhalved value let the title bar be dragged
    // out through the window edge, which is how a panel got lost off-screen.
    const bounds = frame.getBoundingClientRect();
    const limitX = Math.max(0, (window.innerWidth - bounds.width) / 2);
    const limitY = Math.max(0, (window.innerHeight - bounds.height) / 2);
    const clamp = (value: number, limit: number): number => Math.max(-limit, Math.min(limit, value));
    // The frame is centred with `translate`, so a drag composes with that
    // instead of replacing it — otherwise the first pixel of movement snapped
    // the layer from the middle of the page to the corner.
    frame.style.transform = `translate(calc(-50% + ${clamp(event.clientX - drag.x, limitX)}px), calc(-50% + ${clamp(event.clientY - drag.y, limitY)}px))`;
  });
  const endDrag = (): void => { drag = undefined; titleBar.style.cursor = "grab"; };
  titleBar.addEventListener("pointerup", endDrag);
  titleBar.addEventListener("pointercancel", endDrag);

  layers.set(options.route, { scrim, close: dismiss });
}

/** End one child layer; returns false when that surface was not on screen. */
export function dismissChildLayer(route: AppRoute): boolean {
  const layer = layers.get(route);
  if (!layer) return false;
  layer.close();
  return true;
}

export function hasChildLayer(route: AppRoute): boolean {
  return layers.has(route);
}
