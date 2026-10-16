/**
 * OpenCode Zen worker shaping: the adapter emits a block-oriented harness
 * stream, and the worker re-encodes it as OpenAI Chat Completions frames.
 *
 * Two things have to hold for any client that assembles by concatenation:
 * a block's ``block-end`` snapshot must not duplicate what its own deltas
 * already streamed, and a terminal lane failure must surface as an error
 * frame rather than an empty answer a client would read as "said nothing".
 */

import assert from "node:assert/strict";

import {
  toOpenAiFrames,
  aggregate,
  toGenerateOptions,
  errorStatus,
  imageBlockFor,
} from "../young_router/adapters/opencode2dsh_worker.mjs";

/** Collect one harness chunk stream into the frames the worker would send. */
async function framesOf(chunks, model = "big-pickle") {
  const frames = [];
  for await (const frame of toOpenAiFrames(chunks, model)) frames.push(frame);
  return frames;
}

async function* textStream() {
  yield { type: "block-start", index: 0, blockType: "text" };
  yield { type: "text-delta", index: 0, text: "Hello" };
  yield { type: "text-delta", index: 0, text: " world" };
  yield { type: "block-end", index: 0, block: { type: "text", text: "Hello world" } };
  yield { type: "usage", usage: { inputTokens: 3, outputTokens: 2 } };
  yield { type: "finish", reason: { kind: "stop" } };
}

// A first frame announces the assistant role, and text arrives as its own deltas.
{
  const frames = await framesOf(textStream());
  assert.equal(frames[0].choices[0].delta.role, "assistant");
  const text = frames.map((f) => f.choices[0].delta.content ?? "").join("");
  assert.equal(text, "Hello world");
  const last = frames[frames.length - 1];
  assert.equal(last.choices[0].finish_reason, "stop");
  assert.deepEqual(last.usage, { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 });
}

// A tool call streams its arguments as fragments and its block-end carries the
// whole call: neither the name nor the arguments may be doubled.
{
  async function* toolStream() {
    yield { type: "block-start", index: 0, blockType: "tool-call" };
    yield { type: "tool-call-delta", index: 0, id: "call_1", name: "bash", argumentsDelta: '{"cmd":' };
    yield { type: "tool-call-delta", index: 0, argumentsDelta: '"ls"}' };
    yield { type: "block-end", index: 0, block: { type: "tool-call", id: "call_1", name: "bash", arguments: '{"cmd":"ls"}' } };
    yield { type: "finish", reason: { kind: "tool-calls" } };
  }
  const frames = await framesOf(toolStream());
  const calls = frames.flatMap((f) => f.choices[0].delta.tool_calls ?? []);
  const name = calls.map((c) => c.function.name ?? "").join("");
  const args = calls.map((c) => c.function.arguments ?? "").join("");
  assert.equal(name, "bash", "the tool name must not be repeated");
  assert.equal(args, '{"cmd":"ls"}', "the arguments must assemble to the call once");
  assert.equal(calls.find((c) => c.id)?.id, "call_1");
  assert.equal(frames[frames.length - 1].choices[0].finish_reason, "tool_calls");

  const answer = await aggregate(toOpenAiFrames(textStream(), "big-pickle"));
  assert.equal(answer.object, "chat.completion");
  assert.equal(answer.choices[0].message.content, "Hello world");
  assert.equal(answer.choices[0].finish_reason, "stop");
}

// A reasoning block becomes reasoning_content, kept apart from the answer.
{
  async function* reasoningStream() {
    yield { type: "block-start", index: 0, blockType: "reasoning" };
    yield { type: "reasoning-delta", index: 0, text: "think " };
    yield { type: "block-end", index: 0, block: { type: "reasoning", text: "think" } };
    yield { type: "block-start", index: 1, blockType: "text" };
    yield { type: "text-delta", index: 1, text: "answer" };
    yield { type: "block-end", index: 1, block: { type: "text", text: "answer" } };
    yield { type: "finish", reason: { kind: "stop" } };
  }
  const answer = await aggregate(toOpenAiFrames(reasoningStream(), "big-pickle"));
  assert.equal(answer.choices[0].message.content, "answer");
  assert.equal(answer.choices[0].message.reasoning_content, "think ");
}

// A terminal failure is an error frame, never an empty answer.
{
  async function* failingStream() {
    yield { type: "usage", usage: { inputTokens: 0, outputTokens: 0 } };
    yield { type: "finish", reason: { kind: "error", failure: { message: "403: FreeTierError", code: "FREE_TIER_RESTRICTED" } } };
  }
  await assert.rejects(async () => {
    for await (const _frame of toOpenAiFrames(failingStream(), "big-pickle")) { /* drain */ }
  }, /FreeTierError/);
}

// The status a lane failure maps to is the upstream's own, so a client can tell
// a restriction from a transport error.
assert.equal(errorStatus("403: FreeTierError free tier"), 403);
assert.equal(errorStatus("REGION_BLOCKED not available in your country"), 403);
assert.equal(errorStatus("429 rate limit"), 429);
assert.equal(errorStatus("401 AUTH API key invalid"), 401);
assert.equal(errorStatus("400 INVALID_REQUEST"), 400);
assert.equal(errorStatus("socket hang up"), 502);

// An OpenAI request becomes the adapter's harness GenerateOptions: roles,
// text/image order, tool calls, and the sampling fields all survive.
{
  const options = toGenerateOptions(
    {
      messages: [
        { role: "system", content: "sys" },
        { role: "user", content: "hi" },
        {
          role: "user",
          content: [
            { type: "text", text: "look" },
            { type: "image_url", image_url: { url: "data:image/png;base64,iVBORw0KGgo=" } },
          ],
        },
        { role: "assistant", content: "ok", tool_calls: [{ id: "c1", type: "function", function: { name: "bash", arguments: "{}" } }] },
        { role: "tool", tool_call_id: "c1", content: "out" },
      ],
      tools: [{ type: "function", function: { name: "bash", description: "d", parameters: { type: "object" } } }],
      max_tokens: 99,
      temperature: 0.5,
      reasoning_effort: "high",
    },
    "opencode2dsh",
    "big-pickle",
  );
  assert.equal(options.provider, "opencode2dsh");
  assert.equal(options.model, "big-pickle");
  assert.equal(options.maxTokens, 99);
  assert.equal(options.temperature, 0.5);
  assert.equal(options.reasoningEffort, "high");
  assert.deepEqual(options.tools.map((t) => t.name), ["bash"]);
  const roles = options.messages.map((m) => m.role);
  assert.deepEqual(roles, ["system", "user", "user", "assistant", "tool"]);
  // A system turn rides the conversation, where the adapter folds it into the
  // system prompt; the OpenAI wire shape never carries a separate top-level
  // system field, so nothing else has to invent one.
  assert.equal(options.messages[0].content[0].text, "sys");
  const toolResult = options.messages[4];
  assert.equal(toolResult.toolCallId, "c1");
  assert.equal(toolResult.content[0].text, "out");
  // An image part is materialized into the adapter's attachment store, so the
  // block the adapter reads is an attachment reference, not a remote URL.
  const imageBlock = options.messages[2].content.find((b) => b.type === "image");
  assert.match(imageBlock.attachment.attachmentId, /^sha256:[0-9a-f]{64}$/);
  assert.equal(imageBlock.attachment.mediaType, "image/png");
}

// A non-data image URL is left to the adapter's own text fallback.
assert.equal(imageBlockFor({ image_url: { url: "https://example.test/a.png" } }), undefined);
assert.equal(imageBlockFor({ image_url: "not-a-data-url" }), undefined);

console.log("opencode2dsh stream shaping ok");
