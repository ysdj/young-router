// Table-of-contents jumps for the runtime settings pane.
//
// The pane's table of contents and its scrolling surface track one another: a
// click scrolls the pane to the chosen section, and scrolling moves the
// selection. The two directions disagree at the end of the list, because a
// heading that sits closer to the end of the pane than one viewport height can
// never be aligned with the pane's top edge. That jump clamps at the pane's own
// end, and the position it settles on still resolves to an earlier section, so
// plain position tracking would immediately undo the click it just accepted
// (clicking 服务 lands at the pane's end, where the top edge still sits inside
// the 日志 section).
//
// The pane therefore remembers the jump it made - `RuntimeTocJump` - and keeps
// the chosen category until its surface reports a position that jump did not
// ask for.

const JUMP_HEADING_INSET = 2;
const JUMP_LANDING_TOLERANCE = 2;

/** A jump the pane asked for: the category it selected and the offset it requested. */
export type RuntimeTocJump = {
  readonly category: string;
  readonly target: number;
};

/** The pane's scrolling geometry as one scroll event reports it. */
export type RuntimeTocScrollMetrics = {
  readonly contentHeight: number;
  readonly viewportHeight: number;
};

/** The requested offset for a section whose heading sits at `sectionOffset`. */
export function runtimeTocJumpTarget(sectionOffset: number): number {
  return Math.max(0, sectionOffset - JUMP_HEADING_INSET);
}

/** The end of the pane's scrollable range; zero while the pane does not scroll. */
export function runtimeTocScrollEnd(metrics: RuntimeTocScrollMetrics): number {
  return Math.max(0, metrics.contentHeight - metrics.viewportHeight);
}

/**
 * Whether the pane is sitting where `jump` landed: on the requested offset, or
 * at the pane's end when the request was beyond the reachable range. A
 * rubber-band overscroll past that end still belongs to the same landing, and a
 * user scroll away from it ends the jump's ownership of the selection.
 */
export function runtimeTocJumpLanded(jump: RuntimeTocJump, offset: number, metrics: RuntimeTocScrollMetrics): boolean {
  const end = runtimeTocScrollEnd(metrics);
  return jump.target > end
    ? offset >= end - JUMP_LANDING_TOLERANCE
    : Math.abs(offset - jump.target) <= JUMP_LANDING_TOLERANCE;
}
