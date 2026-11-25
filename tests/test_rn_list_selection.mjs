#!/usr/bin/env node
import assert from "node:assert/strict";
import { listSelectionActionForKey, normalizeListSelection, reduceListSelection } from "../rn/packages/shared/src/ui/listSelection.ts";

const keyEvent = (key, modifiers = {}) => ({ key, preventDefault() {}, ...modifiers });
assert.deepEqual(listSelectionActionForKey(keyEvent("a", { metaKey: true })), { type: "all" }, "Command+A selects all");
assert.deepEqual(listSelectionActionForKey(keyEvent("ArrowDown", { shiftKey: true })), { type: "move", direction: "down", shiftKey: true }, "Shift+Down extends selection");
assert.deepEqual(listSelectionActionForKey(keyEvent("ArrowRight"), "horizontal"), { type: "move", direction: "down" }, "Right moves a horizontal list");
assert.equal(listSelectionActionForKey(keyEvent("ArrowDown", { altKey: true })), undefined, "Alt+Down is left to the host");

const order = ["a", "b", "c", "d", "e"];
const onlyRows = { isSelectable: (key) => key !== "c" };
const step = (state, action, options) => reduceListSelection(order, state, action, options);

let state = step(undefined, { type: "press", key: "b" });
assert.deepEqual(state, { selectedKeys: ["b"], activeKey: "b", anchorKey: "b" }, "plain click selects one row");
state = step(undefined, { type: "move", direction: "up" });
assert.deepEqual(state.selectedKeys, ["a"], "a list without an active row starts at the first row");
state = step(undefined, { type: "press", key: "b" });

state = step(state, { type: "press", key: "d", ctrlKey: true });
assert.deepEqual(state.selectedKeys, ["b", "d"], "Ctrl-click adds a row");
assert.equal(state.activeKey, "d");

state = step(state, { type: "press", key: "b", metaKey: true });
assert.deepEqual(state.selectedKeys, ["d"], "Command-click removes a selected row");

state = step(state, { type: "press", key: "e", shiftKey: true });
assert.deepEqual(state.selectedKeys, ["b", "c", "d", "e"], "Shift-click selects the anchor range");
assert.equal(state.anchorKey, "b");

state = step(state, { type: "move", direction: "up", shiftKey: true });
assert.deepEqual(state.selectedKeys, ["b", "c", "d"], "Shift+Up extends from the anchor");
assert.equal(state.activeKey, "d");

state = step(state, { type: "press", key: "a", shiftKey: true, ctrlKey: true });
assert.deepEqual(state.selectedKeys, ["a", "b", "c", "d"], "Ctrl+Shift-click adds a range");

state = step(state, { type: "move", direction: "first" });
assert.deepEqual(state.selectedKeys, ["a"], "Home selects the first row");
state = step(state, { type: "move", direction: "last" });
assert.deepEqual(state.selectedKeys, ["e"], "End selects the last row");

state = step(state, { type: "all" }, onlyRows);
assert.deepEqual(state.selectedKeys, ["a", "b", "d", "e"], "Select all excludes non-selectable rows");
assert.equal(state.activeKey, "e");

state = step(state, { type: "press", key: "c" }, onlyRows);
assert.deepEqual(state.selectedKeys, ["a", "b", "d", "e"], "A disabled/header row ignores clicks");
state = step(state, { type: "move", direction: "up" }, onlyRows);
assert.equal(state.activeKey, "d", "ArrowUp skips non-selectable rows");

state = reduceListSelection(["x", "x", "y"], { selectedKeys: ["x", "missing"] }, { type: "all" });
assert.deepEqual(state.selectedKeys, ["x", "y"], "Selection stays unique and follows visual order");

// Normalizing is what a table does on every render before it draws a selection.
// It must keep the rows the list still offers; `clear` is the user asking for
// no selection, and using it here emptied the highlight on the next render.
const normalized = normalizeListSelection(order, { selectedKeys: ["b", "c"], activeKey: "b", anchorKey: "b" }, onlyRows);
assert.deepEqual(normalized, { selectedKeys: ["b"], activeKey: "b", anchorKey: "b" }, "Normalize drops unselectable rows and keeps the rest");
const pressed = step(undefined, { type: "press", key: "d" });
assert.deepEqual(normalizeListSelection(order, pressed), pressed, "Normalize keeps a live selection unchanged");
assert.deepEqual(normalizeListSelection(order, { selectedKeys: ["missing", "d"], activeKey: "missing" }), { selectedKeys: ["d"], activeKey: "d", anchorKey: "d" }, "Normalize repairs an active row the list dropped");

console.log("RN list selection tests OK (click, toggle, range, keyboard, select-all, normalize)");
