#!/usr/bin/env node
import assert from "node:assert/strict";
import { nextLayerOffset } from "../rn/packages/shared/src/platform/childLayers.ts";
import { canonicalPageRoute, canonicalWindowRoute } from "../rn/packages/shared/src/routes.ts";
import { finishModelPick, hostChromeSnapshot, requestModelPick } from "../rn/packages/shared/src/platform/hostChrome.ts";

// A second drag is relative to the layer's current position, not the centred
// position it had before the first drag.
assert.deepEqual(
  nextLayerOffset({ x: 0, y: 0 }, { x: 40, y: 20 }, { x: 100, y: 100 }),
  { x: 40, y: 20 },
);
assert.deepEqual(
  nextLayerOffset({ x: 40, y: 20 }, { x: 10, y: 5 }, { x: 100, y: 100 }),
  { x: 50, y: 25 },
);
// The accumulated position still respects the viewport clamp.
assert.deepEqual(
  nextLayerOffset({ x: 95, y: -95 }, { x: 20, y: -20 }, { x: 100, y: 100 }),
  { x: 100, y: -100 },
);

// A desktop window shares its assistant settings pane, but the page address
// must still select the assistant named by the deep link.
assert.equal(canonicalWindowRoute("claude-settings"), "codex-settings");
assert.equal(canonicalPageRoute("claude-settings"), "claude-settings");
assert.equal(canonicalPageRoute("relay-accounts"), "providers-models");

const pickerBody = {
  title: "Choose models",
  models: ["a", "b"],
  providerName: "Provider",
  keyName: "Credential",
  confirmLabel: "Add",
  cancelLabel: "Cancel",
  labels: {
    providerLabel: "Provider",
    keyLabel: "Key",
    search: "Search",
    selectAll: "All",
    selectAllToolTip: "All visible",
    invert: "Invert",
    invertToolTip: "Invert visible",
    count: "{count} models",
    countFiltered: "{visible} of {total} models",
    countSelected: "{count} selected",
    empty: "Empty",
    noMatches: "No matches",
  },
};
const initialPick = requestModelPick(pickerBody, "Saving…");
let picker = hostChromeSnapshot().at(-1);
assert.equal(picker?.kind, "picker");
picker.submit(["a"]);
assert.deepEqual(await initialPick, ["a"]);
picker = hostChromeSnapshot().at(-1);
assert.equal(picker?.saving, true);

const retryPick = finishModelPick("Could not save", false);
picker = hostChromeSnapshot().at(-1);
assert.equal(picker?.saving, false);
assert.equal(picker?.status, "Could not save");
picker.submit(["b"]);
assert.deepEqual(await retryPick, ["b"]);
await finishModelPick("Saved", true);
assert.equal(hostChromeSnapshot().some((request) => request.kind === "picker"), false);

console.log("RN child layer drag regression tests OK");
