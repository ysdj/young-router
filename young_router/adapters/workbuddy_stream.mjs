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
 * reasoning stream.  Token frames (``data: [DONE]``), comments, blank frames,
 * JSON bodies for other routes, and any unparseable frame are passed through
 * byte-for-byte.
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
      if (!isEmptyFieldValue(delta[field])) continue
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
