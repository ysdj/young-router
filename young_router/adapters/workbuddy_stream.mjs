/**
 * SSE shaping for the WorkBuddy worker's streaming pass-through.
 *
 * The library's shim states every delta field it knows on every frame, so a
 * reasoning-only frame arrives as
 * ``{"reasoning_content": "We", "content": "", "refusal": "", "tool_calls": []}``.
 * A client reads the presence of ``content`` as "the answer started" and closes
 * the thinking block it just opened: one fragment per collapsed 深度思考 block
 * (the reported ``We`` / ``need respond`` / ``to user.`` rows).
 *
 * The worker therefore drops the empty fields from each chat delta, so a frame
 * says exactly what it carries -- the shape every client already handles for a
 * reasoning stream.  The same rule covers the shim's content-free legacy
 * ``function_call`` mirror (``{name: "", arguments: ""}``): a present
 * ``function_call`` reads as a legacy call to LiteLLM's stream-chunk builder,
 * which fails building the logged response for a dict-shaped delta and reports
 * an upstream route failure on a stream the upstream actually answered.  Token
 * frames (``data: [DONE]``), comments, blank frames, JSON bodies for other
 * routes, and any unparseable frame are passed through byte-for-byte.
 *
 * @module young_router/workbuddy_stream
 */

/** Delta fields that mean nothing when empty, so they are dropped. */
const EMPTY_DELTA_FIELDS = Object.freeze([
  'content',
  'refusal',
  'reasoning',
  'reasoning_content',
  'tool_calls',
  'function_call',
  // The shim's own extension field; it is null on every frame that has nothing
  // to say through it, and a strict client rejects an unknown field either way.
  'extra_fields',
])

function isEmptyFieldValue(value) {
  if (value === '' || value === null || value === undefined) return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value).length === 0
  return false
}

/**
 * Whether a legacy ``function_call`` states nothing at all.
 *
 * The shim mirrors every tool call as the retired
 * ``function_call: {name: "", arguments: ""}`` shape, so the field is present
 * on frames that carry no function call — the tool call itself rides
 * ``tool_calls``.  A frame that names a function or carries arguments is kept
 * exactly as sent; only the content-free mirror is dropped, because LiteLLM's
 * stream-chunk builder treats any present ``function_call`` as a legacy call
 * and fails on the dict shape (`'dict' object has no attribute 'name'`), which
 * surfaces as an upstream route failure even though the upstream answered.
 */
function isEmptyLegacyFunctionCall(value) {
  if (value === null || value === undefined) return false
  if (typeof value !== 'object' || Array.isArray(value)) return false
  const name = value.name
  const args = value.arguments
  return isEmptyFieldValue(name) && (args === undefined || isEmptyFieldValue(args))
}

/**
 * Drop the empty delta fields from one Chat Completions chunk.
 *
 * Returns whether the payload changed; `role`, `finish_reason`, `usage`, ids,
 * and any non-empty value are left untouched.
 */
export function normalizeChatCompletionChunk(payload) {
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.choices)) return false
  let changed = false
  for (const choice of payload.choices) {
    const delta = choice?.delta
    if (!delta || typeof delta !== 'object') continue
    for (const field of EMPTY_DELTA_FIELDS) {
      if (!(field in delta)) continue
      const empty = field === 'function_call'
        ? isEmptyLegacyFunctionCall(delta[field])
        : isEmptyFieldValue(delta[field])
      if (!empty) continue
      delete delta[field]
      changed = true
    }
  }
  return changed
}

/**
 * Normalize one SSE line, preserving its own framing.
 *
 * A line that is not a chat-completions data frame is returned unchanged, so
 * the worker's pass-through cannot alter a route this shaper does not own.
 */
export function normalizeChatCompletionStreamLine(line) {
  const trimmed = line.trimEnd()
  if (!trimmed.startsWith('data:')) return line
  const data = trimmed.slice('data:'.length).trim()
  if (!data || data === '[DONE]') return line
  let payload
  try {
    payload = JSON.parse(data)
  } catch {
    return line
  }
  if (!normalizeChatCompletionChunk(payload)) return line
  return `data: ${JSON.stringify(payload)}\n`
}
