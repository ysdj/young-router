"""Keep a replay inside the window its own deployment declares.

LiteLLM refuses a request *before* sending it when the prompt is larger than
every deployment's ``max_input_tokens``; Codex turns that refusal into
"Codex ran out of room in the model's context window. Start a new thread or
clear earlier history before retrying."  The verdict is honest but the turn is
still recoverable: the replay is a transcript, and the oldest, largest parts of
it are exactly the parts a model can ask for again.

So this pass runs in ``async_pre_call_hook`` -- the only place that runs before
deployment selection, and therefore before the refusal exists.  It shortens
nothing unless the request's content-aware estimate is over the window the
model group declares, and it never touches the current turn's own message or
the instructions.

What it elides, oldest first:

* inline images from earlier turns, replaced with the same placeholders the
  image-context setting writes (a file path the client's viewer opens),
* ``function_call_output`` bodies (file dumps, command output, tool results),
* text content of earlier ``message`` items.

Each elided part is replaced in place, so the transcript keeps its shape: an
elided tool result is still the result of the call that produced it.

Two rules this pass has to keep, both learned from a live Claude Code thread
that re-read the same paper fifty times instead of once:

* **A picture is priced as a picture on both wires.**  The image-context pass
  reads both spellings now (``input_image`` / ``image_url``, and Messages'
  ``{"type": "image", "source": {"type": "base64", …}}``); what this module
  still prices itself are the shapes that pass does not rewrite -- a picture
  still nested in a ``tool_result`` before the lift, and a Messages body from a
  client that is not Claude.  See ``_strings_joined``,
  ``_image_token_estimate`` and ``_messages_wire_image_tokens``.  Counting
  their base64 as prose declared a 300k-token replay to be 1.47M and elided
  history on every single step.
* **What the model just read stays readable.**  The turn being answered is
  protected, but on the Messages wire that turn is a *tool result*, and the
  result the step was actually made from sits one item before it -- so the read
  the model was mid-way through became a marker on the next request.  ``_marker``
  therefore never tells the model to re-read a file: that sentence was the
  instruction the thread followed.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Dict, List, Mapping, Optional, Tuple

TRACE_EVENT = "codex_context_window_elided"
RETRY_TRACE_EVENT = "codex_context_window_shrink_retry"

#: What one inline image byte costs when its own dimensions cannot be read.
#: A token counter prices an image from its size in pixels; a relay that
#: measures the prompt it receives prices the base64 it is actually sent.  Both
#: are real -- a thread with 93 images was reported as 298k of a 950k window by
#: the token counter and refused by the upstream for the 37.5 MB body.  This
#: rate is the fallback for the second case, and it is deliberately the
#: pessimist's: it is used to decide whether a *token* window could be exceeded,
#: so overestimating costs a rewrite that was not needed (see
#: ``_image_token_estimate``).
_IMAGE_BYTES_PER_TOKEN = 2.5

#: Pixels one token buys in a vision model's own accounting, and the per-image
#: overhead of the tiles an image is cut into.
_IMAGE_PIXELS_PER_TOKEN = 750
_IMAGE_TILE_OVERHEAD_TOKENS = 85

#: Base64 costs a third more than the bytes it encodes.
_BASE64_OVERHEAD = 4 / 3

#: Leave this much of the window free.  A turn that arrives *at* the boundary
#: would otherwise be one tool result away from the same refusal.
_TARGET_FRACTION = 0.9

#: Below this, an item is not worth rewriting: the marker costs as much as the
#: text it replaces.
_MIN_ELIDABLE_CHARS = 400

#: Share of the chars-to-tokens ratio an elision is credited with.
_ELIDE_SAFETY = 1.0

#: The turn being answered is normally kept verbatim, but a single tool output
#: can be larger than the whole window on its own (a build log, a dumped file).
#: Eliding history cannot fit that, and sending it anyway is a refusal the
#: client retries forever, so the overrun is cut out of the body instead: this
#: share of it stays, split unevenly between the head and the tail, and the
#: marker in the middle names exactly how many characters left.
_TRUNCATE_KEEP_SHARE = 0.7
_TRUNCATE_HEAD_SHARE = 0.7

#: A body smaller than this is not worth splitting: the marker would cost the
#: room the split was meant to win.
_MIN_TRUNCATABLE_CHARS = 8000

_TEXT_FIELDS = ("input_text", "output_text", "text", "summary_text")

#: Ratios tried, in order, when the *upstream* rejects a prompt the router was
#: willing to send.  A declared ``max_input_tokens`` is the route's promise, not
#: the model's, and a relay can hold the model to a smaller number without
#: saying so.  Halving twice reaches a quarter of the refused size, which is
#: past every limit such a relay has been seen to enforce; the ladder is short
#: on purpose, because each rung costs an upstream round trip.
_RETRY_FRACTIONS = (0.55, 0.3)

#: The largest request each model group is known to have accepted upstream.  A
#: relay that refused a prompt teaches the next turn where the real ceiling is,
#: so the same thread pays for the discovery once and not on every message.
_ACCEPTED_SIZE: Dict[str, int] = {}

#: The fewest bytes one token of a replay can occupy.  Base64 -- the encoded
#: reasoning a Responses client replays -- is 1.48 bytes per token measured
#: against ``o200k_base``; a bytes-per-token bound above that reads a replay as
#: smaller than it is and skips the count that would have caught it.
_DENSEST_BYTES_PER_TOKEN = 1.4

#: A shrink has to remove at least this share of the payload to be worth a
#: resend.  A rewrite that leaves the body within a rounding error of what was
#: just refused is the same bytes going back up, and the answer to those bytes
#: is already known.
_MIN_SHRINK_FRACTION = 0.9

#: The input size this task's guard measured.  The value is also used for
#: context reporting and remembering an upstream-accepted size.
_MEASURED_INPUT_TOKENS: ContextVar[Optional[int]] = ContextVar(
    "young_router_measured_input_tokens", default=None
)


def measured_input_tokens() -> Optional[int]:
    """The count for the request being served, for usage reporting."""

    return _MEASURED_INPUT_TOKENS.get()


#: Whether this request was already shortened after a size refusal.  Only then
#: is the answer the deployment gave evidence about where its real ceiling is.
_SHRUNK: ContextVar[bool] = ContextVar("young_router_context_shrunk", default=False)


def request_was_shrunk() -> bool:
    return _SHRUNK.get()


def _router() -> Any:
    try:
        from litellm.proxy import proxy_server as _proxy_server_module

        return getattr(_proxy_server_module, "llm_router", None)
    except Exception:
        return None


def _deployments(router: Any, model: str) -> List[Mapping[str, Any]]:
    try:
        entries = router.get_model_list(model_name=model) or []
    except Exception:
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


def window_for(model: str, router: Any = None) -> Optional[int]:
    """The smallest window every deployment of ``model`` declares.

    The router refuses the request when *no* deployment can hold it, so the
    largest declared window would be the wrong number to fit against: the
    request may still be routed to a smaller sibling.
    """

    router = router if router is not None else _router()
    if router is None or not isinstance(model, str) or not model:
        return None
    windows: List[int] = []
    for entry in _deployments(router, model):
        info = entry.get("model_info")
        info = info if isinstance(info, Mapping) else {}
        try:
            declared = int(info.get("max_input_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if declared > 0:
            windows.append(declared)
    if windows:
        return min(windows)
    return _catalog_window(model, router)


def _catalog_window(model: str, router: Any = None) -> Optional[int]:
    """The input cap the model behind this group states, for a route with none.

    A route is not required to carry ``model_info.max_input_tokens``, and none
    of the routed Claude groups did: the guard then had no number to compare
    against, so every oversized replay went out to meet the relay's own 1M cap
    and the thread died on a frame that only says "access forbidden".

    The cap is a property of the model the deployment names upstream, which is
    the map LiteLLM already ships; the app's own catalog is not used here.  It
    describes the *client's* window (a display number, adjusted per view), and
    on a route with no declared window it answered 272,000 for a group whose
    upstream accepts 1,000,000 -- a guess that would have elided four fifths of
    a replay that fit.
    """

    try:
        import litellm
    except Exception:
        return None
    router = router if router is not None else _router()
    windows: List[int] = []
    for entry in _deployments(router, model):
        params = entry.get("litellm_params")
        upstream = params.get("model") if isinstance(params, Mapping) else None
        if not isinstance(upstream, str) or not upstream:
            continue
        try:
            info = litellm.get_model_info(upstream)
        except Exception:
            continue
        try:
            declared = int((info or {}).get("max_input_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if declared > 0:
            windows.append(declared)
    if windows:
        return min(windows)
    # The cost map in a shipped bundle can carry the price tiers of a model
    # without its window (the dated map has no ``max_input_tokens`` for the
    # routed Claude groups).  The app's own catalog states the window it shows,
    # and that is the number the client compacts against.
    try:
        from . import reasoning as _reasoning_module

        record = _reasoning_module._reasoning_registry().record_for(model)
        catalog = getattr(record, "context_window", None)
    except Exception:
        return None
    if isinstance(catalog, int) and catalog > 0:
        return catalog
    return None


_ENCODER: Any = None


def wire_tokens(items: Any) -> Optional[int]:
    """Tokens the text of this replay costs, not the count LiteLLM keeps.

    ``_count_pre_call_check_tokens`` reads a Messages payload through the chat
    shape, and every block that shape has no room for -- a ``tool_result``'s
    body, a ``thinking`` block, a ``tool_use`` input -- is dropped on the way.
    On a 4.1 MB Claude Code replay it answered 578,261 for text the upstream
    counted at over a million, so a guard comparing that number with a 1M
    window saw room that was not there and sent the request to be refused.

    Readable content and the JSON structure are counted; opaque image bytes
    and thinking signatures are normalized before estimation.  Ciphertext is
    not prompt prose.  This remains a local estimate, not the upstream's own
    count, especially when a signature represents hidden thinking.
    """

    global _ENCODER
    if not isinstance(items, (list, Mapping)):
        return None
    try:
        import tiktoken

        if _ENCODER is None:
            _ENCODER = tiktoken.get_encoding("o200k_base")
    except Exception:
        return None
    text = _strings_joined(items)
    if not text:
        return None
    # Count the serialized replay, not just the strings inside it.  A
    # ``tool_use``/``tool_result`` block spends tokens on its ``type``, its id
    # and its field names before the body is counted, and a Claude Code
    # replay is thousands of those blocks: walking the strings alone answered
    # 10-15% below the same JSON, so a replay the pass believed it had fitted
    # to 918,680 tokens reached the upstream at 1,169,888 and was refused
    # again on every resend.
    #
    # The image bodies leave the counted text for the reason in
    # ``_strings_joined``: they are priced per picture below, not per byte.
    try:
        # Tokenization sees decoded JSON values, not their escape spelling.
        # Escaping CJK text here turns each character into several tokens.
        serialized = json.dumps(
            _without_inline_image_bytes(items), ensure_ascii=False
        )
    except Exception:
        serialized = text
    return len(_ENCODER.encode(serialized, disallowed_special=()))


def _without_inline_image_bytes(node: Any) -> Any:
    """A copy with opaque payloads removed from the local text estimate.

    Inline image bytes go: the part stays an image with its mime type, so the
    serialized size of the replay is what the wire pays for everything that is
    not a picture.  ``_image_token_estimate`` adds the pictures back.

    A thinking signature is an encrypted copy of the thinking, not readable
    prompt text.  Tokenizing its base64 spelling charges roughly one token per
    ciphertext character, which is not the cost of the thinking it represents.
    Visible thinking text is counted in its own field.  This is still a local
    estimate: omitted/summary thinking requires the upstream's count, not an
    invented ciphertext-to-token conversion.  Only the estimate uses this copy;
    the signed history itself must be sent unchanged.
    """

    if isinstance(node, list):
        return [_without_inline_image_bytes(part) for part in node]
    if isinstance(node, Mapping):
        if node.get("type") == "thinking":
            updated = dict(node)
            signature = updated.get("signature")
            if isinstance(signature, str) and signature:
                updated["signature"] = ""
            return {
                key: _without_inline_image_bytes(value)
                for key, value in updated.items()
            }
        if _is_inline_image(node):
            updated = dict(node)
            source = updated.get("source")
            if isinstance(source, Mapping) and isinstance(source.get("data"), str):
                source = dict(source)
                source["data"] = ""
                updated["source"] = source
                return updated
            value = updated.get("image_url")
            if isinstance(value, Mapping):
                value = dict(value)
                value["url"] = "data:image/;base64,"
                updated["image_url"] = value
                return updated
            if isinstance(value, str):
                updated["image_url"] = "data:image/;base64,"
                return updated
            return updated
        return {key: _without_inline_image_bytes(value) for key, value in node.items()}
    return node


def _is_inline_image(node: Mapping[str, Any]) -> bool:
    """An image part of either wire, with its bytes inline.

    Responses says ``{"type": "input_image", "image_url": "data:…"}``;
    Messages says ``{"type": "image", "source": {"type": "base64", "data":
    …}}``.

    This predicate exists for *measuring* the shapes the image-context scan
    does not own: an ordinary Messages picture is now rewritten by that pass,
    but one still nested in a ``tool_result`` before the lift, or in a body from
    a client that is not Claude, stays here.  Replacing those is the image
    pass's job when it applies; pricing them is always this module's, and the
    bug this walker fixed was arithmetic -- base64 counted as prose.
    """

    kind = node.get("type")
    if not isinstance(kind, str) or "image" not in kind:
        return False
    source = node.get("source")
    if isinstance(source, Mapping):
        data = source.get("data")
        if isinstance(data, str) and data:
            return True
        return isinstance(source.get("url"), str)
    value = node.get("image_url")
    if isinstance(value, Mapping):
        value = value.get("url")
    return isinstance(value, str) and bool(value)


def _inline_image_b64(node: Any, out: Optional[List[str]] = None) -> List[str]:
    """The base64 bodies of every inline image *the scanner cannot see*.

    ``image_context.scan`` is the Responses scanner and reads
    ``input_image`` / ``image_url``; its pictures are already priced from their
    headers there.  This walker reads the other spelling -- Messages'
    ``{"type": "image", "source": {"type": "base64", "data": …}}`` -- so the
    two prices never add up to the same picture twice.  The Messages pass may
    have already replaced some of these with text; only the ones still here are
    charged.

    A picture inside a ``tool_result`` is collected too, but it is priced by
    ``_tool_result_image_tokens`` instead: the upstream cannot read that shape
    and tokenizes its data URL as prose, so the honest price is its bytes and
    not its pixels.  ``tool_result_images`` lifts those pictures out before
    this pass runs; when one is still here the lift did not happen, and a
    pixel price would understate the body by two orders of magnitude.
    """

    found = out if out is not None else []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            source = item.get("source")
            if isinstance(source, Mapping):
                data = source.get("data")
                if isinstance(data, str) and data:
                    found.append(data)
                    continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _strings_joined(node: Any, out: Optional[List[str]] = None) -> str:
    """Every string inside ``node``, in order, as one blob.

    Opaque payloads are normalized by the same pass ``wire_tokens`` uses.
    Their absence from the token count also applies to the character ratio
    used to credit elisions: keeping a signature in that ratio made dropping
    useful tool history look too small and removed whole exchanges needlessly.

    A token counter prices a picture from its pixels; ``wire_tokens`` counts the
    serialized replay, where a 473 KB base64 of a 1999x1589 screenshot is
    322,000 tokens of prose.  Five ``Read`` results of one paper were enough to
    declare a 300k-token thread 1.47M and have this pass elide its history on
    every step -- the model then re-read what had just been taken away.  The
    pictures are priced by ``_image_token_estimate`` instead, in the window's
    own unit, and they are the image pass's to replace.
    """

    parts = out if out is not None else []
    stack = [_without_inline_image_bytes(node)]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            if _is_inline_image(item):
                continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return "".join(parts)


def count_tokens(data: Mapping[str, Any], router: Any = None) -> Optional[int]:
    """LiteLLM's own pre-call count, so this pass agrees with the refusal."""

    router = router if router is not None else _router()
    if router is None:
        return None
    try:
        return int(
            router._count_pre_call_check_tokens(
                messages=data.get("messages"),
                input=data.get("input"),
                request_kwargs=data,
            )
        )
    except Exception:
        return None


def measure_tokens(data: Mapping[str, Any], router: Any = None) -> Optional[int]:
    """The largest honest count for this payload."""

    key, items = payload_items(data)
    if key == "messages":
        # LiteLLM's Messages counter prices inline images as base64 text.
        # Count decoded prompt text, system and tools; pessimistic_tokens prices
        # images separately in the same units as the window.
        prompt: Dict[str, Any] = {"messages": items}
        if data.get("system") is not None:
            prompt["system"] = data["system"]
        if isinstance(data.get("tools"), list):
            prompt["tools"] = data["tools"]
        wire = wire_tokens(prompt)
        if wire is not None:
            return wire
    return count_tokens(data, router)


def schema_tokens(data: Mapping[str, Any]) -> int:
    """Tokens the tool definitions add to this prompt, every request.

    A Claude Code turn carries one definition per tool the client exposes --
    192 on the thread that could not get through, 165 of them from the MCP
    servers it had connected.  They ride on every request and the model reads
    them, so they are part of the prompt the route prices.  The client's own
    "Context window 100.8k / 1M" counts the conversation and not the schema,
    which is why a turn that looked like 10% of the window was refused for
    size: the pass had fitted the messages and the wire still carried 169 KB
    of definitions on top.
    """

    tools = data.get("tools")
    if not tools:
        return 0
    global _ENCODER
    try:
        import tiktoken

        if _ENCODER is None:
            _ENCODER = tiktoken.get_encoding("o200k_base")
        return len(
            _ENCODER.encode(
                json.dumps(tools, ensure_ascii=False), disallowed_special=()
            )
        )
    except Exception:
        return 0


def _text_of(container: Any) -> str:
    if isinstance(container, str):
        return container
    if isinstance(container, Mapping):
        for field in _TEXT_FIELDS:
            value = container.get(field)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(container, list):
        return "".join(_text_of(part) for part in container)
    return ""


def _item_text(item: Mapping[str, Any]) -> str:
    if item.get("type") == "function_call_output":
        return _text_of(item.get("output"))
    if item.get("type") == "message":
        return _text_of(item.get("content"))
    if item.get("type") == "input_image":
        return str(item.get("image_url") or "")
    if _is_message_turn(item):
        return _messages_text(item.get("content"))
    return ""


def _marker(item: Mapping[str, Any], chars: int, kind: Optional[str] = None) -> str:
    label = kind or (
        "tool output" if item.get("type") == "function_call_output" else "message"
    )
    # No instruction to re-read.  The sentence this replaces ended "re-read the
    # file or ask the user if you need it", and a live Claude Code thread obeyed
    # it: every elided `sed -n 'A,Bp'` was answered by the same `sed` again, 100
    # offset reads for 24.9 KB of unique text.  The marker states what is gone;
    # what to do about it is the model's call, not this string's.
    return (
        f"[{chars} characters of earlier {label} elided to fit this model's "
        "context window.]"
    )


def _replace_text(container: Any, text: str) -> Tuple[Any, bool]:
    if isinstance(container, str):
        return text, True
    if isinstance(container, Mapping):
        updated = dict(container)
        for field in _TEXT_FIELDS:
            if isinstance(updated.get(field), str):
                updated[field] = text
                return updated, True
        return container, False
    if isinstance(container, list):
        replaced = False
        parts: List[Any] = []
        for part in container:
            if isinstance(part, Mapping) and part.get("type") in _TEXT_FIELDS:
                if not replaced:
                    updated = dict(part)
                    updated["text"] = text
                    parts.append(updated)
                    replaced = True
                continue
            parts.append(part)
        return (parts, True) if replaced else (container, False)
    return container, False


def _carries_image(item: Mapping[str, Any]) -> bool:
    for field in ("content", "output"):
        parts = item.get(field)
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, Mapping) and "image" in str(part.get("type") or ""):
                return True
    return False


def _elide_item(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a copy of ``item`` with its body replaced, or ``None``."""

    if _is_message_turn(item):
        return _elided_message_turn(item)
    # A body that includes a picture cannot be replaced by a text marker: the
    # picture is not what "earlier characters elided" describes.  Images leave
    # the replay only through the placeholder pass, which can name their file.
    if _carries_image(item):
        return None
    chars = len(_item_text(item))
    if chars < _MIN_ELIDABLE_CHARS:
        return None
    marker = _marker(item, chars)
    if item.get("type") == "function_call_output":
        output, replaced = _replace_text(item.get("output"), marker)
        if not replaced:
            output = marker
        updated = dict(item)
        updated["output"] = output
        return updated
    content, replaced = _replace_text(item.get("content"), marker)
    if not replaced:
        return None
    updated = dict(item)
    updated["content"] = content
    return updated


# ---------------------------------------------------------------- Messages --
# "Messages" is the other wire whose payload this pass may shorten.  Its items
# are ``{"role": ..., "content": ...}`` turns, and the content that costs the
# window is the ``tool_result`` a turn carries: a single `cat` of a large file
# replays as hundreds of thousands of tokens on every step of the thread.  A
# turn is rewritten block by block -- the block, its ``tool_use_id`` and its
# position all stay in place, so the pairing the upstream validates and the
# shape the client replays are untouched -- and the body becomes a marker.
#
# Whole turns are never dropped here: Messages alternates user/assistant and
# pairs ``tool_use`` with ``tool_result``, so deleting either half is what the
# upstream refuses.  Eliding bodies alone is enough -- a refused 4.1 MB replay
# was 3.2 MB of tool output.


def _is_message_turn(item: Any) -> bool:
    return (
        isinstance(item, Mapping)
        and isinstance(item.get("role"), str)
        and item.get("type") is None
        and "content" in item
    )


def _messages_text(content: Any) -> str:
    """The characters a Messages block will cost, whatever shape it has."""

    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        # Every block names its body under a different key -- ``content`` for a
        # tool result, ``thinking`` for reasoning, ``input`` for a tool call --
        # so a named-field read misses two of the three.  Joining every string
        # in the block sizes all of them, which is what this measure is for.
        return _strings_joined(content)
    if isinstance(content, list):
        return "".join(_messages_text(part) for part in content)
    return ""


def _truncation_marker(removed: int, kind: str) -> str:
    """What the model is told about the middle that did not fit."""

    return (
        f"[young-router: {removed} characters of this {kind} were removed because "
        "the request exceeded the upstream input window; the beginning and the "
        "end of it are kept]"
    )


def _truncated_text(text: str, keep: int, kind: str) -> Optional[str]:
    """``text`` with ``keep`` characters of it kept, or None when it shrinks nothing."""

    if not isinstance(text, str) or keep < 0:
        return None
    if len(text) <= keep + _MIN_TRUNCATABLE_CHARS:
        return None
    head_chars = int(keep * _TRUNCATE_HEAD_SHARE)
    tail_chars = max(keep - head_chars, 0)
    head = text[:head_chars]
    tail = text[len(text) - tail_chars :] if tail_chars else ""
    marker = _truncation_marker(len(text) - len(head) - len(tail), kind)
    return f"{head}\n\n{marker}\n\n{tail}"


def _truncated_item(
    item: Mapping[str, Any], *, want_chars: int
) -> Tuple[Optional[Dict[str, Any]], int]:
    """Cut the largest bodies this item carries until ``want_chars`` is freed.

    Block types, ids and positions are untouched: a ``tool_result`` keeps its
    ``tool_use_id`` and a text block stays a text block, so the replay is still
    a valid turn.  Reasoning is skipped -- its signature covers the text, and a
    body the signature no longer matches is a request upstream refuses.
    """

    content = item.get("content")
    if not isinstance(content, list):
        return None, 0
    rewritten: List[Any] = []
    remaining = want_chars
    freed = 0
    changed = False
    for block in content:
        if not isinstance(block, Mapping) or remaining <= 0:
            rewritten.append(block)
            continue
        kind = block.get("type")
        if kind == "tool_result" and not _carries_image(block):
            text = _messages_text(block.get("content"))
            if len(text) >= _MIN_TRUNCATABLE_CHARS:
                keep = max(len(text) - remaining, _MIN_TRUNCATABLE_CHARS)
                cut = _truncated_text(text, keep, "tool output")
                if cut is not None:
                    updated = dict(block)
                    updated["content"] = cut
                    rewritten.append(updated)
                    gained = len(text) - len(cut)
                    freed += gained
                    remaining -= max(gained, 0)
                    changed = True
                    continue
        elif kind == "text":
            text = block.get("text")
            if isinstance(text, str) and len(text) >= _MIN_TRUNCATABLE_CHARS:
                keep = max(len(text) - remaining, _MIN_TRUNCATABLE_CHARS)
                cut = _truncated_text(text, keep, "message")
                if cut is not None:
                    updated = dict(block)
                    updated["text"] = cut
                    rewritten.append(updated)
                    gained = len(text) - len(cut)
                    freed += gained
                    remaining -= max(gained, 0)
                    changed = True
                    continue
        rewritten.append(block)
    if not changed:
        return None, 0
    result = dict(item)
    result["content"] = rewritten
    return result, freed


def _truncated_tail(items: List[Any], *, count: int, limit: int) -> Optional[Tuple[List[Any], int]]:
    """Fit a payload whose protected turn is itself over the window.

    ``_elidable`` never touches the turn being answered, which is right until
    that turn alone exceeds the window: the pass then reports a size it knows
    will be refused.  Measured on a live thread, the last turn carried
    1,690,305 tokens against a 1,000,000-token window after every older item
    had been elided -- the request was sent anyway, one route answered 502 and
    the turn only survived because a second route happened to take it.

    Only the overrun is cut, and only out of bodies that can lose their middle
    without changing what the replay *is*.
    """

    protected = _protected_index(items)
    if protected >= len(items) or count <= limit:
        return None
    total_chars = len(_strings_joined(items))
    if total_chars <= 0:
        return None
    chars_per_token = max(total_chars / max(count, 1), 0.25)
    # The bodies cut here tokenize denser than the payload average, so the
    # ratio alone under-credits the cut and the pass would stop one body short.
    want_chars = int((count - limit) * chars_per_token / _TRUNCATE_KEEP_SHARE) + 2048
    order = sorted(
        (index for index in range(protected, len(items)) if isinstance(items[index], Mapping)),
        key=lambda index: len(_item_text(items[index])),
        reverse=True,
    )
    rewritten = list(items)
    freed = 0
    for index in order:
        if freed >= want_chars:
            break
        updated, gained = _truncated_item(items[index], want_chars=want_chars - freed)
        if updated is None or gained <= 0:
            continue
        rewritten[index] = updated
        freed += gained
    if freed <= 0:
        return None
    return rewritten, freed


def _elided_message_turn(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Replace the bodies this turn carries that cost the window.

    A ``tool_result`` is the usual one, but a turn can also replay a large
    ``tool_use`` input (a written file, a command with a heredoc) or a long
    text block, and a thread whose tool outputs have already been elided still
    has to reach the target through those.  Every block keeps its type, its id
    and its position: only the body becomes a marker.
    """

    content = item.get("content")
    if not isinstance(content, list):
        return None
    rewritten: List[Any] = []
    changed = False
    for block in content:
        if isinstance(block, Mapping):
            kind = block.get("type")
            if kind == "tool_result":
                chars = len(_messages_text(block.get("content")))
                if chars >= _MIN_ELIDABLE_CHARS and not _carries_image(block):
                    updated = dict(block)
                    updated["content"] = _marker(block, chars, "tool output")
                    rewritten.append(updated)
                    changed = True
                    continue
            elif kind == "tool_use":
                raw = block.get("input")
                chars = len(_strings_joined(raw))
                if chars >= _MIN_ELIDABLE_CHARS:
                    updated = dict(block)
                    updated["input"] = {"elided": _marker(block, chars, "tool input")}
                    rewritten.append(updated)
                    changed = True
                    continue
            elif kind == "thinking":
                # Reasoning is the largest body a long Claude Code thread
                # replays (1.8 MB of a 4.1 MB history), and it cannot be cut
                # without its signature: the signature covers the text, so a
                # marker with the original signature is a replay the upstream
                # refuses.  It leaves as an unsigned marker instead -- the same
                # shape the signature repair sends when a route will not verify
                # a foreign signature, and one every route here serves.
                text = block.get("thinking")
                chars = len(text) if isinstance(text, str) else 0
                if chars >= _MIN_ELIDABLE_CHARS:
                    updated = dict(block)
                    updated["thinking"] = _marker(block, chars, "reasoning")
                    updated["signature"] = ""
                    rewritten.append(updated)
                    changed = True
                    continue
            elif kind == "text":
                text = block.get("text")
                chars = len(text) if isinstance(text, str) else 0
                if chars >= _MIN_ELIDABLE_CHARS:
                    updated = dict(block)
                    updated["text"] = _marker(block, chars, "message")
                    rewritten.append(updated)
                    changed = True
                    continue
        rewritten.append(block)
    if not changed:
        return None
    updated_turn = dict(item)
    updated_turn["content"] = rewritten
    return updated_turn


def _elidable(items: List[Any]) -> List[int]:
    """Indices worth shortening, largest first, never the step being answered.

    A picture is not in this ranking and must not be: ``_item_text`` measures
    bodies, and an image's body is the image pass's to replace with a
    placeholder that names the file the model can open.  A text marker in its
    place names nothing to view.
    """

    protected = _protected_index(items)
    ranked: List[Tuple[int, int]] = []
    for index, item in enumerate(items):
        if index >= protected or not isinstance(item, Mapping):
            continue
        chars = len(_item_text(item))
        if chars >= _MIN_ELIDABLE_CHARS:
            ranked.append((chars, index))
    ranked.sort(reverse=True)
    return [index for _, index in ranked]


def _protected_index(items: List[Any]) -> int:
    """Everything from the current turn's opening message on stays verbatim.

    The turn being answered is the unit that must arrive intact: its tool calls
    and their outputs are what the model is in the middle of using.  A thread
    with a single user message puts every ``view_image`` result of that turn
    here, and shortening one of them hands the model a placeholder for the
    picture it just asked to see.

    On the Messages wire the last ``user`` turn is *always* a ``tool_result``,
    so anchoring there protected one item out of hundreds: a Claude Code thread
    of one user request followed by 300 tool round trips had 341 of its 860
    items replaced by markers, and the answer it was composing had just lost
    the reads that answer was made from.  The step being answered is that
    ``tool_result`` *and the call that asked for it*, so the anchor is the last
    assistant turn -- the pair leaves or stays together, which is also the only
    shape the upstream accepts.

    This is a boundary, not the cure.  What made that thread unreadable was the
    count itself: base64 priced as prose (see ``_strings_joined``) put every
    request 500k tokens over a window it actually filled to 30%.  A pass that
    does not need to elide cannot lose what the model just read, and it stays a
    *group* rather than the whole history so a thread whose first message is its
    only user request is still shorten-able (see ``OneTurnThreadTests``).
    """

    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if (
            isinstance(item, Mapping)
            and item.get("type") == "message"
            and item.get("role") == "user"
        ):
            return index
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if isinstance(item, Mapping) and item.get("type") == "message":
            return index
    return _messages_step_anchor(items)


def _messages_step_anchor(items: List[Any]) -> int:
    """``_protected_index`` for a Messages replay (``{"role", "content"}``).

    The step being answered is the last ``tool_use`` turn *and* the
    ``tool_result`` that answers it and anything the assistant has begun to say
    since -- a call whose answer was elided is a step the model cannot take, and
    an answer elided this side of its own question is what the live thread kept
    re-deriving.  Anchoring on the last ``user`` turn instead protected the
    result alone and left its call elidable.
    """

    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not _is_message_turn(item) or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(block, Mapping) and block.get("type") == "tool_use"
            for block in content
        ):
            return index
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if _is_message_turn(item) and item.get("role") == "user":
            return index
    return len(items)


def _is_marker_item(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    if item.get("type") == "message":
        text = _item_text(item)
        return "elided" in text and text.startswith("[") and len(text) < 400
    return False


def _droppable(items: List[Any]) -> List[int]:
    """Indices that can go entirely, oldest first.

    Only two shapes are safe to delete outright: messages this pass already
    reduced to a marker (no information left), and a tool call together with
    its own output -- deleting one half of a pair is what upstream validators
    reject, so a pair only ever leaves as a unit.  Newest-message protection
    matches ``_elidable``.
    """

    protected = _protected_index(items)
    calls: Dict[str, int] = {}
    outputs: Dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or index >= protected:
            continue
        kind = item.get("type")
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if kind == "function_call":
            calls[call_id] = index
        elif kind == "function_call_output":
            outputs[call_id] = index
    grouped: List[Tuple[int, List[int]]] = []
    for call_id, call_index in calls.items():
        output_index = outputs.get(call_id)
        if output_index is None:
            continue
        grouped.append((min(call_index, output_index), sorted((call_index, output_index))))
    grouped.sort()
    dropped: List[int] = []
    dropped.extend(_droppable_tool_exchanges(items, protected))
    for index, item in enumerate(items):
        if index >= protected:
            continue
        if _is_marker_item(item):
            dropped.append(index)
    for _, pair in grouped:
        dropped.extend(pair)
    seen: set = set()
    unique = [index for index in sorted(dropped) if not (index in seen or seen.add(index))]
    return unique


def payload_items(data: Mapping[str, Any]) -> Tuple[Optional[str], Optional[List[Any]]]:
    """The list this request replays, and which field holds it.

    Responses calls it ``input``; Messages calls it ``messages``.  Everything
    else in this module works on the list, so the two wires share one pass.
    """

    for key in ("input", "messages"):
        items = data.get(key)
        if isinstance(items, list) and items:
            return key, items
    return None, None


def _exchange_partner(items: List[Any], index: int) -> Optional[int]:
    """The other half of the tool exchange ``index`` belongs to, if any.

    ``_droppable`` lists both halves of a pair, and the loop that consumes the
    list must not stop between them: a call without its answer (or the other
    way round) is a replay the wire refuses.  A turn that carries a call and
    the next turn carrying its answer are the two shapes here.
    """

    if not (0 <= index < len(items)):
        return None
    item = items[index]
    if not isinstance(item, Mapping):
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    calls = {
        block.get("id")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    }
    answers = {
        block.get("tool_use_id")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "tool_result"
    }
    if not calls and not answers:
        return None
    # The partner is the neighbouring turn, in whichever direction the pair
    # was written.
    for other in (index + 1, index - 1):
        if not (0 <= other < len(items)):
            continue
        candidate = items[other]
        if not isinstance(candidate, Mapping):
            continue
        other_content = candidate.get("content")
        if not isinstance(other_content, list):
            continue
        other_calls = {
            block.get("id")
            for block in other_content
            if isinstance(block, Mapping) and block.get("type") == "tool_use"
        }
        other_answers = {
            block.get("tool_use_id")
            for block in other_content
            if isinstance(block, Mapping) and block.get("type") == "tool_result"
        }
        if (calls & other_answers) or (answers & other_calls):
            return other
    return None


def _droppable_tool_exchanges(items: List[Any], protected: int) -> List[int]:
    """Oldest complete ``tool_use``/``tool_result`` exchanges, as units.

    A Messages replay cannot have half a pair removed: the next message must
    carry a ``tool_result`` for every ``tool_use`` it follows.  Dropping the
    assistant turn *and* the user turn that answers it keeps that rule, and it
    is the only way to remove the reasoning blocks that ride along with an old
    exchange (reasoning is the largest thing a long Claude Code thread keeps
    replaying).
    """

    dropped: List[int] = []
    index = 0
    while index + 1 < protected:
        turn = items[index]
        answer = items[index + 1]
        if not (_is_message_turn(turn) and _is_message_turn(answer)):
            index += 1
            continue
        if turn.get("role") != "assistant" or answer.get("role") != "user":
            index += 1
            continue
        content = turn.get("content")
        if not isinstance(content, list):
            index += 1
            continue
        wanted = {
            block.get("id")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "tool_use"
        }
        if not wanted:
            index += 1
            continue
        answered = {
            block.get("tool_use_id")
            for block in (answer.get("content") or [])
            if isinstance(block, Mapping) and block.get("type") == "tool_result"
        }
        if not wanted <= answered:
            index += 1
            continue
        dropped.extend((index, index + 1))
        index += 2
    return dropped


def shorten(
    data: Mapping[str, Any],
    *,
    count: int,
    window: int,
    target: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return a copy of ``data`` that fits, or ``None`` when it cannot."""

    key, items = payload_items(data)
    if key is None or items is None:
        return None
    budget = target if target is not None else int(window * _TARGET_FRACTION)
    rewritten = _fitted_items(items, count=count, target=budget)
    if rewritten is None:
        return None
    result = dict(data)
    result[key] = rewritten
    return result


def _fitted_items(items: List[Any], *, count: int, target: int) -> Optional[List[Any]]:
    """Drop or elide old bodies until the estimate reaches ``target`` tokens."""

    # The ratio has to be taken on the same text the count was taken on.  A
    # Messages turn names its body in ``_item_text`` but the wire also carries
    # keys, ids and tool inputs, so measuring the ratio from the block texts
    # alone made every elision look three times larger than it was and stopped
    # the pass one block in, still over the cap.
    total_chars = len(_strings_joined(items))
    if total_chars <= 0:
        return None
    chars_per_token = max(total_chars / max(count, 1), 0.25)
    rewritten = list(items)
    freed = 0
    changed = False
    # Phase one: keep every item, replace the body of the largest old ones
    # with a marker.  The transcript keeps its shape, so a replay stays valid.
    for index in _elidable(items):
        if count - freed <= target:
            break
        item = rewritten[index]
        elided = _elide_item(item)
        if elided is None:
            continue
        replaced_chars = len(_strings_joined(item))
        # The bodies this pass removes tokenize denser than the payload average
        # (tool output is code and paths, not prose), so the ratio alone
        # over-credits every elision; half of it keeps the pass going until the
        # measured count -- not the estimate -- is under the target.
        freed += int(replaced_chars / chars_per_token * _ELIDE_SAFETY)
        rewritten[index] = elided
        changed = True
    # Phase two: the markers themselves and the wrapper items now cost more
    # than the room that is left.  Dropping them is lossless -- the marker
    # says so -- and a tool call only ever leaves together with its output.
    if count - freed > target:
        # A tool exchange leaves as a unit, and this loop is where that can
        # stop being true: ``_droppable`` lists the call and its answer as two
        # adjacent indices, so a break between them drops the call and keeps
        # the answer -- the replay then goes out with ``unexpected
        # `tool_use_id` found in `tool_result` blocks``, which no resend can
        # satisfy.  Take the pair together: look ahead past the partner.
        order = _droppable(rewritten)
        position = 0
        while position < len(order):
            if count - freed <= target:
                break
            index = order[position]
            partner = _exchange_partner(rewritten, index)
            group = [index] if partner is None else [index, partner]
            for member in group:
                if rewritten[member] is not None:
                    freed += int(len(_item_text(rewritten[member])) / chars_per_token)
                    rewritten[member] = None
                    changed = True
            position += 1
            while position < len(order) and order[position] in group:
                position += 1
    if not changed:
        return None
    if any(item is None for item in rewritten):
        rewritten = [item for item in rewritten if item is not None]
    return rewritten


def retry_for_refusal(
    data: Any,
    exception: BaseException,
    *,
    attempt: int,
    model: Optional[str] = None,
) -> bool:
    """Shorten ``data`` in place after the provider refused it as too long.

    The router's pre-call verdict only knows the ``max_input_tokens`` a route
    declares.  A relay can enforce its own, smaller limit for the model behind
    it and answer with a plain 400 ("prompt is too long"), which is a size the
    router never sees.  Eliding one more time at ``_RETRY_FRACTIONS[attempt]``
    of what was actually sent turns that refusal back into a servable request
    instead of a dead turn.

    Returns True when the payload now differs, so the caller can resend it.
    """

    if not isinstance(data, dict):
        return False
    payload_key, payload = payload_items(data)
    if payload_key is None or payload is None:
        return False
    if attempt >= len(_RETRY_FRACTIONS):
        return False
    from . import routing as _routing_module

    try:
        if not _routing_module._is_context_size_error(exception):
            return False
    except Exception:
        return False
    group = model if isinstance(model, str) and model else data.get("model")
    measured = measured_input_tokens()
    # A refused prompt is usually refused for its bytes, not its tokens: the
    # images a thread keeps re-sending are the whole difference between what a
    # counter reports and what the relay receives.  Replacing them needs no
    # count at all -- the refusal itself proved there were too many -- so it runs
    # before the measurement gate.  Behind that gate a counter that could not
    # answer ended a turn whose cause was 50 MB of replayed pictures.
    before_bytes = _payload_bytes(data)
    rewritten_images = _image_shrunk(data, 0)
    if rewritten_images is not None and _shrank_enough(
        {"input": rewritten_images.get("input") or data.get("messages")}, before_bytes
    ):
        if isinstance(rewritten_images.get("input"), list):
            data["input"] = rewritten_images["input"]
        _SHRUNK.set(True)
        _trace_retry(
            data,
            attempt=attempt,
            count=measured or 0,
            target=measured or 0,
            model=group,
            image_bytes=_inline_image_bytes(data.get("input")),
            exception=exception,
        )
        return True
    if not measured or measured <= 0:
        # The pre-call pass skips counting a request far below the declared
        # window, so there may be no measurement to scale from; the refusal is
        # the first evidence about the size that matters, and counting once
        # here is cheaper than ending the turn.
        measured = measure_tokens(data, _router())
    if not measured or measured <= 0:
        return False
    target = int(measured * _RETRY_FRACTIONS[attempt])
    rewritten = _fitted_items(payload, count=measured, target=target)
    if rewritten is None:
        return False
    if not _shrank_enough({payload_key: rewritten}, before_bytes):
        # Eliding freed nothing a relay would notice (everything that fits this
        # target already sits inside the turn being answered).  Resending the
        # same bytes invites the same refusal, so the turn ends on the
        # classified error instead of on a second attempt that cannot succeed.
        return False
    data[payload_key] = rewritten
    # Measure what was built, not what was aimed at.  Recording ``target``
    # claimed a size the payload did not have -- a live thread rewrote to a
    # claimed 985,033 tokens and the upstream counted 1,243,305 on the same
    # bytes, so every resend was refused for a size the pass believed it had
    # already fixed, and the client compacted for 45 minutes against a number
    # that was never real.
    produced = measure_tokens(data, _router())
    _MEASURED_INPUT_TOKENS.set(produced if produced is not None else target)
    _SHRUNK.set(True)
    _trace_retry(
        data,
        attempt=attempt,
        count=measured,
        target=target,
        model=group,
        exception=exception,
    )
    return True

def _payload_bytes(data: Mapping[str, Any]) -> int:
    """The wire size of the replay, which is what a relay refuses."""

    key, items = payload_items(data)
    try:
        return len(json.dumps(items or [], ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0

def _shrank_enough(data: Mapping[str, Any], before_bytes: int) -> bool:
    """Whether this rewrite removed something worth resending the request for."""

    if before_bytes <= 0:
        return True
    return _payload_bytes(data) <= int(before_bytes * _MIN_SHRINK_FRACTION)


def record_accepted_size(data: Any, *, model: Optional[str] = None) -> None:
    """Remember that the payload this task sent was accepted upstream."""

    if not isinstance(data, dict):
        return
    group = model if isinstance(model, str) and model else data.get("model")
    measured = measured_input_tokens()
    if not isinstance(group, str) or not group or not measured or measured <= 0:
        return
    # The budget ``apply`` compares against is the pessimistic one -- the count
    # plus what the images cost a byte-counting relay -- so the size remembered
    # here has to be the same measure, or a large replay would be filed as small.
    accepted = pessimistic_tokens(data, measured)
    known = _ACCEPTED_SIZE.get(group)
    if known is None or accepted < known:
        _ACCEPTED_SIZE[group] = accepted


def _trace_retry(
    data: Mapping[str, Any],
    *,
    attempt: int,
    count: int,
    target: int,
    model: Optional[str] = None,
    image_bytes: Optional[int] = None,
    exception: Optional[BaseException] = None,
) -> None:
    from . import trace as _trace_module

    # The cause is part of the record: this pass only runs after a route refused
    # a request, and "which refusal" is the one fact a reader cannot recover
    # afterwards.  A live thread was shrinking a 32k-token request on every step
    # against a 1,000,000-token window, and nothing in the log said what the
    # route had objected to -- so the honest answer stayed unknown.
    refusal = None
    if exception is not None:
        refusal = str(exception)
        reason = getattr(exception, "reason", None)
        if reason:
            refusal = f"{reason}: {refusal}"
    try:
        _trace_module._route_trace(
            RETRY_TRACE_EVENT,
            model=model or data.get("model"),
            attempt=attempt,
            before=count,
            target=target,
            image_bytes=image_bytes,
            refusal=(refusal or "")[:400] or None,
            payload_bytes=_payload_bytes(data),
        )
    except Exception:
        return


def _inline_image_bytes(items: Any) -> int:
    """Wire size of every image the replay carries, after base64."""

    from . import image_context as _image_context_module

    try:
        images = _image_context_module.scan(items)
    except Exception:
        return 0
    raw = 0
    for image in images:
        data = getattr(image, "raw", None)
        if isinstance(data, (bytes, bytearray)):
            raw += len(data)
    return int(raw * _BASE64_OVERHEAD)


def _image_token_estimate(items: Any) -> int:
    """Tokens the replay's images could cost the *window*, overestimated.

    An image's price in a vision model is its pixels, not its bytes: a 1.06 MB
    screenshot is 2240x3040 and about 9,000 tokens, while charging its base64 at
    ``_IMAGE_BYTES_PER_TOKEN`` made it 423,000.  That factor of fifty is not
    conservative, it is a unit mismatch -- the number this feeds is compared
    against a *token* window, and on a live thread it declared a 32,000-token
    request with five screenshots to be 1,050,000 tokens of a 1,000,000-token
    window on every single step.  The pass then shortened a request that fit,
    removing the pictures the image pass had deliberately kept.

    Dimensions come from the image headers the scanner already reads, so this
    costs no decode.  An image whose size cannot be read keeps the byte rate,
    where the only thing known about it is how many bytes it occupies.

    ``image_context.scan`` is the Responses scanner, so it answers zero for a
    Claude Code replay that spells the same picture
    ``{"type": "image", "source": {"type": "base64", …}}``.  Those are priced
    here directly: the Messages pass rewrites the ordinary ones (an ask
    placeholder needs no tool), and this arithmetic is what prices the rest --
    a picture still nested in a ``tool_result``, or one in a body the pass
    declined.  A replay left unpriced is a replay whose size the pass gets
    wrong in the expensive direction.
    """

    from . import image_context as _image_context_module

    total = 0
    try:
        images = _image_context_module.scan(items)
    except Exception:
        images = []
    for image in images:
        width = getattr(image, "width", None)
        height = getattr(image, "height", None)
        raw = getattr(image, "raw", None)
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            pixels = width * height
            total += (
                -(-pixels // _IMAGE_PIXELS_PER_TOKEN) + _IMAGE_TILE_OVERHEAD_TOKENS
            )
        elif isinstance(raw, (bytes, bytearray)) and raw:
            total += int(len(raw) * _BASE64_OVERHEAD / _IMAGE_BYTES_PER_TOKEN)
    total += _messages_wire_image_tokens(items)
    return total


def _messages_wire_image_tokens(items: Any) -> int:
    """The pictures ``image_context.scan`` (the Responses scanner) cannot see.

    Only the head of each body is decoded -- an image header sits in the first
    few kilobytes of a JPEG, PNG, GIF and WEBP -- because this runs on every
    request and a replay can carry a hundred pictures.  A body whose header
    still is not in that window keeps the byte rate, which is the only thing
    known about it without decoding the whole thing.

    Pictures still inside a ``tool_result`` are the exception, and the reason
    this function is not simply a sum of pixel prices: the upstream cannot read
    that shape, so it tokenizes the data URL as prose.  Measured on the live
    relay, one 473 KB JPEG cost 327,716 prompt tokens there and 1,053 as an
    ordinary ``image`` part.  ``tool_result_images`` moves them out before this
    pass runs; a body that still has one is priced at what the upstream will
    actually charge for it.
    """

    import base64

    from . import image_context as _image_context_module

    nested = set()
    for body in _tool_result_image_b64(items):
        nested.add(body)
    total = _tool_result_image_tokens(items)
    for body in _inline_image_b64(items):
        if body in nested:
            continue
        head = b""
        if body:
            try:
                head = base64.b64decode(body[: 64 * 1024], validate=False)
            except (ValueError, TypeError):
                head = b""
        width, height = _image_context_module._image_size(head)
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            total += -(-(width * height) // _IMAGE_PIXELS_PER_TOKEN) + (
                _IMAGE_TILE_OVERHEAD_TOKENS
            )
        elif body:
            total += int(len(body) * 3 / 4 * _BASE64_OVERHEAD / _IMAGE_BYTES_PER_TOKEN)
    return total


def _tool_result_image_b64(node: Any, out: Optional[List[str]] = None) -> List[str]:
    """The base64 bodies of pictures sitting inside a ``tool_result``."""

    found = out if out is not None else []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            if item.get("type") == "tool_result":
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, Mapping):
                            continue
                        source = part.get("source")
                        if not isinstance(source, Mapping):
                            continue
                        data = source.get("data")
                        if isinstance(data, str) and data:
                            found.append(data)
                continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _tool_result_image_tokens(node: Any) -> int:
    """What a picture inside a ``tool_result`` costs on this wire.

    Not its pixels.  The bridge serialises the part as a data URL inside a Chat
    ``tool`` message, and the upstream tokenizes that string -- one 473 KB JPEG
    was billed at 327,716 prompt tokens against 1,053 for the same bytes as an
    ordinary image part.  Priced at the byte rate the upstream applies, this
    number is what makes the window guard see the body that will be refused.
    """

    total = 0
    for body in _tool_result_image_b64(node):
        # The data URL prefix (``data:image/jpeg;base64,``) is prose the
        # upstream also reads, and base64 costs roughly a token per character
        # on this relay: measured 327,716 tokens for 473,448 characters.
        total += len(body)
    return total


def pessimistic_tokens(data: Mapping[str, Any], count: int) -> int:
    """``count`` plus what the images could add in the window's own unit."""

    _key, items = payload_items(data)
    return count + _image_token_estimate(items)


def _image_shrunk(data: Any, keep: int) -> Optional[Dict[str, Any]]:
    """Drop earlier turns' images for placeholders, or ``None`` if it cannot."""

    from . import image_context as _image_context_module
    from . import responses_request as _responses_request_module

    try:
        if _responses_request_module._request_is_codex_compaction(data):
            return None
        return _image_context_module.rewrite_for_size(dict(data), keep=keep)
    except Exception:
        return None


def _count_is_worth_running(data: Mapping[str, Any], window: int) -> bool:
    """A bytes upper bound: no request that small can reach that window.

    2.5 UTF-8 bytes per token is deliberately more pessimistic than the
    three-byte bound for CJK text, the densest case the counter sees: only a
    payload this estimate already places under the window skips the exact
    count that the router would otherwise pay twice for a healthy replay.
    """

    key, items = payload_items(data)
    try:
        if key == "messages":
            measurable: Any = {"messages": items or []}
            for field in ("system", "tools"):
                if data.get(field) is not None:
                    measurable[field] = data[field]
        else:
            measurable = items or []
        serialized = len(json.dumps(measurable, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return True
    return serialized / _DENSEST_BYTES_PER_TOKEN >= window


def apply(data: Any) -> Optional[Dict[str, Any]]:
    """The pre-call step: shorten only when the request cannot be sent."""

    if not isinstance(data, dict):
        return None
    payload_key, payload = payload_items(data)
    if payload_key is None or payload is None:
        return None
    model = data.get("model")
    if not isinstance(model, str) or not model:
        return None
    # A compaction request asks the upstream to summarize exactly the history
    # it receives; eliding it would bake that loss into what the client keeps.
    # Signed replay items are already answered for elsewhere, and the client's
    # own compaction is the right place to shorten a history that big.
    from . import responses_request as _responses_request_module

    try:
        if _responses_request_module._request_is_codex_compaction(data):
            return None
    except Exception:
        pass
    router = _router()
    window = window_for(model, router)
    if not window:
        return None
    # A route's declared window is not always the model's: a relay can hold the
    # same model to a smaller limit and only say so by refusing a prompt.  Once
    # that refusal has been seen for this model group, the next turn starts
    # from the size that was actually accepted instead of paying for the same
    # discovery again.
    budget = _ACCEPTED_SIZE.get(model)
    limit = window if budget is None or budget >= window else min(window, budget)
    if not _count_is_worth_running(data, limit):
        return None
    count = measure_tokens(data, router)
    if count is None:
        return None
    # Messages measurements already include system and tool definitions.
    # Other surfaces keep LiteLLM's count and add their non-shrinking schema.
    schema = 0 if payload_key == "messages" else schema_tokens(data)
    count += schema
    _MEASURED_INPUT_TOKENS.set(count)
    if pessimistic_tokens(data, count) <= limit:
        return None
    original, original_count = data, count
    # Images first: they are the bytes a byte-counting relay refuses, and the
    # placeholder is the only rewrite that removes them.  Eliding text under
    # them would leave the largest cost in place.
    rewritten_images = _image_shrunk(data, 0)
    fitted_count: Optional[int] = count
    if rewritten_images is not None:
        data = rewritten_images
        next_count = measure_tokens(data, router)
        if next_count is not None and payload_key != "messages":
            next_count += schema
        fitted_count = next_count
        if next_count is not None:
            count = next_count
            _MEASURED_INPUT_TOKENS.set(count)
    if pessimistic_tokens(data, count) <= limit:
        _trace(
            original,
            data,
            count=original_count,
            window=limit,
            router=router,
            image_pass=True,
            after_count=fitted_count,
        )
        return dict(data)
    # The estimate the first pass uses is bytes-per-token, which is close but
    # not exact; the count after it is the router's own.  Repeat until the
    # request fits or a pass finds nothing left to elide -- the alternative is
    # a turn that fails one marker's worth of tokens below the limit.
    current = data
    current_count = count
    result: Optional[Dict[str, Any]] = None
    target = int(window * _TARGET_FRACTION)
    if budget is not None and budget < target:
        target = budget
    for _ in range(4):
        shorter = shorten(current, count=current_count, window=window, target=target)
        if shorter is None:
            break
        result = shorter
        next_count = measure_tokens(shorter, router)
        if next_count is not None and payload_key != "messages":
            next_count += schema
        fitted_count = next_count
        # Stop at the target, not at the last count that was still under the
        # limit.  A live thread was elided to 983,041 against a 1,000,000
        # window -- 1.7% of margin -- and the relay counted the same bytes 20%
        # higher, so the replay was refused and the next attempt started over.
        if next_count is None or next_count <= target:
            current_count = next_count if next_count is not None else current_count
            break
        current, current_count = shorter, next_count
    final = result if result is not None else data
    # The protected turn is the last thing elision refuses to touch.  When it
    # is on its own larger than the window, dropping history cannot fit the
    # request and the pass used to send a size it knew upstream would refuse:
    # a live thread shipped 1,690,305 tokens against a 1,000,000-token window,
    # got a 502 from one route and survived only because another took it.  Cut
    # the overrun out of the bodies instead, so the turn is servable wherever
    # it lands.
    truncated = False
    if fitted_count is not None and fitted_count > limit:
        payload_key, tail_items = payload_items(final)
        if payload_key is not None and tail_items is not None:
            fitted_tail = _truncated_tail(tail_items, count=fitted_count, limit=limit)
            if fitted_tail is not None:
                new_items, _freed_chars = fitted_tail
                candidate = dict(final)
                candidate[payload_key] = new_items
                recounted = measure_tokens(candidate, router)
                if recounted is not None and payload_key != "messages":
                    recounted += schema
                if recounted is not None and recounted < fitted_count:
                    final = candidate
                    fitted_count = recounted
                    truncated = True
    if final is original:
        return None
    if fitted_count is not None:
        _MEASURED_INPUT_TOKENS.set(fitted_count)
    _trace(
        original,
        final,
        count=original_count,
        window=limit,
        router=router,
        image_pass=rewritten_images is not None,
        after_count=fitted_count,
        truncated=truncated,
    )
    return dict(final)


def _turn_facts(items: Any, key: Optional[str] = None) -> Dict[str, Any]:
    """What the pass judged: which turns the images sit in, and what it protects.

    ``key`` is the field the replay lives in.  The two wires count a turn
    differently -- a Messages tool result is a user message, not a new request
    -- so a trace that always used the Responses counter would say ``turn 0``
    over the very replay the Messages image pass had just shortened.
    """

    from . import image_context as _image_context_module

    list_items = items if isinstance(items, list) else []
    try:
        if key == "messages":
            images = _image_context_module.scan_messages(list_items)
            current_turn = _image_context_module._messages_turn_count(list_items)
        else:
            images = _image_context_module.scan(list_items)
            current_turn = _image_context_module._turn_count(list_items)
    except Exception:
        return {}
    return {
        "current_turn": current_turn,
        "protected_index": _protected_index(list_items),
        "image_turns": sorted({image.turn for image in images})[:16],
        "image_count": len(images),
    }


def _trace(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    count: int,
    window: int,
    router: Any,
    image_pass: bool = False,
    after_count: Optional[int] = None,
    truncated: bool = False,
) -> None:
    from . import routing as _routing_module
    from . import trace as _trace_module

    try:
        if after_count is None:
            after_count = count_tokens(after, router)
        before_key, before_items = payload_items(before)
        after_key, after_items = payload_items(after)
        trace_fields = {
            "model": before.get("model"),
            "window": window,
            "before": count,
            "after": after_count,
            # The token count and the wire size are both answers to "will this
            # be accepted", and only the pair shows which one the elision moved.
            "image_bytes_before": _inline_image_bytes(before_items),
            "image_bytes_after": _inline_image_bytes(after_items),
            "image_pass": image_pass,
            "before_facts": _turn_facts(before_items, before_key),
            "after_facts": _turn_facts(after_items, after_key),
            "items_before": len(before_items or []),
            "items_after": len(after_items or []),
        }
        if truncated:
            trace_fields["truncated"] = True
        _trace_module._route_trace(
            TRACE_EVENT,
            request_id=_routing_module._trace_request_id(dict(before)),
            session=_routing_module._trace_session_context(dict(before)),
            **trace_fields,
        )
    except Exception:
        # A trace must never decide a request.
        pass
