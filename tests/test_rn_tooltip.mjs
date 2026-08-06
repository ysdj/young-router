#!/usr/bin/env node
import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const tooltipModule = path.resolve(import.meta.dirname, "../rn/packages/shared/src/ui/tooltip.ts");
const { screenBoundedTooltipText } = await import(pathToFileURL(tooltipModule));

const narrow = screenBoundedTooltipText("0123456789".repeat(20), { width: 248, height: 160 });
const narrowLines = narrow.split("\n");
assert.ok(narrowLines.length <= 7, "tooltip height must stay within the supplied screen bounds");
assert.ok(narrowLines.every((line) => Array.from(line).length <= 26), "tooltip lines must wrap within the supplied screen width");
assert.ok(narrow.endsWith("..."), "truncated tooltips must identify omitted content with ASCII punctuation");

const mixed = screenBoundedTooltipText("中文中文中文中文中文", { width: 108, height: 160 });
assert.deepEqual(mixed.split("\n"), ["中文中文", "中文中文", "中文"], "wide glyphs must consume two tooltip cells");

const short = "Short\ncontent";
assert.equal(screenBoundedTooltipText(short, { width: 1024, height: 768 }), short, "short tooltips must remain unchanged");

console.log("RN tooltip boundary regression tests OK");
