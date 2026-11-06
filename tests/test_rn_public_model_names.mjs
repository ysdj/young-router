#!/usr/bin/env node
import assert from "node:assert/strict";
import {
  groupRoutesByPublicModel,
  preferredPublicSpelling,
  publicModelKey,
} from "../rn/packages/shared/src/ui/publicModelNames.ts";

// A public model is one name a client asks for, and the comparison folds case:
// one group, whichever case a route happens to state.
assert.equal(publicModelKey(" MiniMax-M3.1-Flash "), "minimax-m3.1-flash");
assert.equal(publicModelKey("MINIMAX-M3.1-FLASH"), publicModelKey("minimax-m3.1-flash"));

// The spelling a group publishes is its lower-case one, so the name a client is
// most likely to send is the name the pane edits and Core writes.
assert.equal(preferredPublicSpelling(["MiniMax-M3.1-Flash", "minimax-m3.1-flash"]), "minimax-m3.1-flash");
// With no lower-case spelling among them, the first one a row states stands.
assert.equal(preferredPublicSpelling(["MINIMAX-M3.1-FLASH", "MiniMax-M3.1-Flash"]), "MINIMAX-M3.1-FLASH");
assert.equal(preferredPublicSpelling([]), "");

// Two rows spelling one name in two cases are one public model with two routes,
// and the routes keep the order the list was sorted in.
const groups = groupRoutesByPublicModel([
  { publicModel: "MiniMax-M3.1-Flash", key: "tken:1" },
  { publicModel: "grok-4.7", key: "flux:2" },
  { publicModel: "minimax-m3.1-flash", key: "wb:3" },
  { publicModel: "MINIMAX-M3.1-FLASH", key: "sisct2:4" },
  { publicModel: "  ", key: "blank:5" },
]);
assert.deepEqual(
  groups.map((group) => [group.name, group.entries.map((entry) => entry.key)]),
  [
    ["minimax-m3.1-flash", ["tken:1", "wb:3", "sisct2:4"]],
    ["grok-4.7", ["flux:2"]],
  ],
);

// A name nothing else states keeps its own spelling: grouping never renames a
// model the user did not ask to rename.
assert.deepEqual(
  groupRoutesByPublicModel([{ publicModel: "GPT-6.1-Sol", key: "a:1" }]).map((group) => group.name),
  ["GPT-6.1-Sol"],
);

console.log("RN public model name regression tests OK");
