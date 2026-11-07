#!/usr/bin/env node
import assert from "node:assert/strict";
import {
  groupRoutesByPublicModel,
  importNeedsNameCase,
  publicModelKey,
} from "../rn/packages/shared/src/ui/publicModelNames.ts";

// A public model is the name a client asks for, and the name is the identity:
// the import settled the case once, so nothing here folds one spelling onto
// another.
assert.equal(publicModelKey(" MiniMax-M3.1-Flash "), "MiniMax-M3.1-Flash");
assert.notEqual(publicModelKey("MINIMAX-M3.1-FLASH"), publicModelKey("minimax-m3.1-flash"));

// Rows that state one name are one public model with several routes, and the
// routes keep the order the list was sorted in.
const groups = groupRoutesByPublicModel([
  { publicModel: "MiniMax-M3.1-Flash", key: "tken:1" },
  { publicModel: "grok-4.7", key: "flux:2" },
  { publicModel: "minimax-m3.1-flash", key: "wb:3" },
  { publicModel: " MiniMax-M3.1-Flash ", key: "sisct2:4" },
  { publicModel: "  ", key: "blank:5" },
]);
assert.deepEqual(
  groups.map((group) => [group.name, group.entries.map((entry) => entry.key)]),
  [
    ["MiniMax-M3.1-Flash", ["tken:1", "sisct2:4"]],
    ["grok-4.7", ["flux:2"]],
    ["minimax-m3.1-flash", ["wb:3"]],
  ],
);

// Another case is another name: the Router keeps one deployment set per
// spelling, so the table draws it as the separate group it serves.
assert.deepEqual(
  groupRoutesByPublicModel([
    { publicModel: "MINIMAX-M3.1-FLASH", key: "sisct2:4" },
    { publicModel: "minimax-m3.1-flash", key: "wb:3" },
  ]).map((group) => group.name),
  ["MINIMAX-M3.1-FLASH", "minimax-m3.1-flash"],
);

// A name nothing else states keeps its own spelling: grouping never renames a
// model the user did not ask to rename.
assert.deepEqual(
  groupRoutesByPublicModel([{ publicModel: "GPT-6.1-Sol", key: "a:1" }]).map((group) => group.name),
  ["GPT-6.1-Sol"],
);

// An import asks which spelling it files once, and only when there is something
// to decide: a roster that is already lower case has one answer, so it asks
// nothing.
assert.equal(importNeedsNameCase(["Qwen3.8-Max", "qwen3.8-flash"]), true);
assert.equal(importNeedsNameCase(["DeepSeek-V4-Pro"]), true);
assert.equal(importNeedsNameCase(["minimax-m3.1-flash", "kimi-k3"]), false);
assert.equal(importNeedsNameCase(["  ", ""]), false);
assert.equal(importNeedsNameCase([]), false);

console.log("RN public model name regression tests OK");
