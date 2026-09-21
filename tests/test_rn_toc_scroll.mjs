#!/usr/bin/env node
// The runtime pane's table of contents and its scrolling surface track each
// other. A section whose heading sits closer to the end of the pane than one
// viewport can never be aligned with the pane's top edge: that jump clamps at
// the pane's end, where the position still resolves to an earlier section, and
// the pane used to hand the selection back to it (clicking 服务 selected 日志).
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const scrollModule = path.resolve(import.meta.dirname, "../rn/packages/shared/src/ui/runtimeTocScroll.ts");
const { runtimeTocJumpLanded, runtimeTocJumpTarget, runtimeTocScrollEnd } = await import(pathToFileURL(scrollModule));

const pane = { contentHeight: 5000, viewportHeight: 500 };
assert.equal(runtimeTocScrollEnd(pane), 4500, "the pane's scrollable end must be its content minus one viewport");
assert.equal(runtimeTocScrollEnd({ contentHeight: 400, viewportHeight: 500 }), 0, "a pane that does not scroll has no reachable end");
assert.equal(runtimeTocJumpTarget(1000), 998, "a jump must land on the section heading, just under the pane's top edge");
assert.equal(runtimeTocJumpTarget(0), 0, "the first section must never ask for a negative offset");

// A section the pane can align: the jump owns the selection on its own offset
// alone, so the pane starts following the position again as soon as the user
// scrolls away from it.
const reachable = { category: "Recovery", target: runtimeTocJumpTarget(1000) };
assert.ok(runtimeTocJumpLanded(reachable, 998, pane), "a reachable jump must own the selection where it landed");
assert.ok(runtimeTocJumpLanded(reachable, 1000, pane), "a reachable landing tolerates sub-point rounding");
assert.ok(!runtimeTocJumpLanded(reachable, 1030, pane), "scrolling down past a reachable landing must return the selection to the position");
assert.ok(!runtimeTocJumpLanded(reachable, 900, pane), "scrolling up away from a reachable landing must return the selection to the position");

// The tail of the list (the reported bug): a heading below the reachable range
// lands at the pane's own end, where the tracker would otherwise select the
// section above it.
const sections = { Logs: 4200, Network: 4700, Service: 4800 };
const service = { category: "Service", target: runtimeTocJumpTarget(sections.Service) };
assert.ok(service.target > runtimeTocScrollEnd(pane), "the reported jump must be beyond the pane's reachable range");
assert.ok(runtimeTocJumpLanded(service, runtimeTocScrollEnd(pane), pane), "a clamped jump must own the selection at the pane's end it landed on");
assert.ok(runtimeTocJumpLanded(service, runtimeTocScrollEnd(pane) + 40, pane), "a rubber-band overscroll past the end still belongs to the same landing");
assert.ok(!runtimeTocJumpLanded(service, runtimeTocScrollEnd(pane) - 40, pane), "scrolling away from the clamped landing must return the selection to the position");
const logsOwnsEnd = sections.Logs <= runtimeTocScrollEnd(pane) + 12 && sections.Network > runtimeTocScrollEnd(pane) + 12;
assert.ok(logsOwnsEnd, "the clamped landing is exactly the position where plain tracking selects the section above the jump");

// A jump to a section the pane can align, from a pane that is already there.
const alreadyThere = { category: "Timeouts", target: runtimeTocJumpTarget(0) };
assert.ok(runtimeTocJumpLanded(alreadyThere, 0, { contentHeight: 300, viewportHeight: 500 }), "a jump inside a pane that does not scroll must keep its selection");

console.log("RN runtime table-of-contents scroll regression tests OK");
