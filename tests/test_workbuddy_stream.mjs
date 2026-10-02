/**
 * WorkBuddy stream shaping: the worker's streaming pass-through must drop the
 * empty delta fields the shim states on every frame, or a client reads
 * ``content: ""`` as "the answer started" and closes the thinking block it just
 * opened — one fragment per collapsed 深度思考 row.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import {
  normalizeChatCompletionChunk,
  normalizeChatCompletionStreamLine,
} from "../young_router/adapters/workbuddy_stream.mjs";

const here = dirname(fileURLToPath(import.meta.url));

// A reasoning frame drops every empty field and keeps what it carries.
const reasoning = {
  id: "cmb-1",
  object: "chat.completion.chunk",
  created: 1,
  model: "workbuddy-chat-1",
  choices: [{ index: 0, delta: { role: "assistant", reasoning_content: "We", content: "", refusal: "", tool_calls: [], extra_fields: null }, finish_reason: null }],
};
assert.equal(normalizeChatCompletionChunk(reasoning), true);
assert.deepEqual(Object.keys(reasoning.choices[0].delta).sort(), ["reasoning_content", "role"]);

// A frame that carries content keeps it, and its reasoning fragment too.
const mixed = { choices: [{ index: 0, delta: { content: "ok", reasoning_content: " exactly", tool_calls: [] } }] };
assert.equal(normalizeChatCompletionChunk(mixed), true);
assert.deepEqual(Object.keys(mixed.choices[0].delta).sort(), ["content", "reasoning_content"]);

// A frame with nothing to prune is reported unchanged.
const contentOnly = { choices: [{ index: 0, delta: { content: "ok" }, finish_reason: "stop" }] };
assert.equal(normalizeChatCompletionChunk(contentOnly), false);

// Non-empty tools and legacy function calls stay exactly as sent.
const toolFrame = { choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { name: "x", arguments: "{}" } }], function_call: { name: "y", arguments: "{}" } } }] };
assert.equal(normalizeChatCompletionChunk(toolFrame), false);

// A whole SSE line keeps its framing, and everything that is not a chat chunk
// passes through byte-for-byte.
const frame = 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"We","content":"","tool_calls":[]}}]}\n';
const normalized = normalizeChatCompletionStreamLine(frame);
assert.equal(normalized.endsWith("\n"), true);
assert.deepEqual(Object.keys(JSON.parse(normalized.slice("data: ".length)).choices[0].delta), ["reasoning_content"]);
for (const passthrough of [
  "\n",
  ": keep-alive\n",
  "data: [DONE]\n",
  "data: not json\n",
  "event: ping\n",
  'data: {"object":"chat.completion","choices":[{"index":0,"message":{"content":"ok"}}]}\n',
]) {
  assert.equal(normalizeChatCompletionStreamLine(passthrough), passthrough, JSON.stringify(passthrough));
}

// The worker routes its streaming pass-through through the shaper.
const worker = readFileSync(resolve(here, "../young_router/adapters/workbuddy_worker.mjs"), "utf8");
assert.match(worker, /import \{ normalizeChatCompletionStreamLine \} from ['"]\.\/workbuddy_stream\.mjs['"]/);
assert.match(worker, /res\.write\(normalizeChatCompletionStreamLine\(buffer\.slice\(0, newline \+ 1\)\)\)/);
assert.match(worker, /if \(buffer\) res\.write\(normalizeChatCompletionStreamLine\(buffer\)\)/);
// ...and does not pipe the raw stream to the client any more.
assert.doesNotMatch(worker, /readable\.pipe\(res\)/);

console.log("WorkBuddy stream shaping regression tests OK (empty delta fields dropped, other frames untouched)");
