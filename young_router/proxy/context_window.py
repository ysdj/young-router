"""Keep a replay inside the window its own deployment declares.

LiteLLM refuses a request *before* sending it when the prompt is larger than
every deployment's ``max_input_tokens``; Codex turns that refusal into
"Codex ran out of room in the model's context window. Start a new thread or
clear earlier history before retrying."  The verdict is honest but the turn is
still recoverable: the replay is a transcript, and the oldest, largest parts of
it are exactly the parts a model can ask for again.

So this pass runs in ``async_pre_call_hook`` -- the only place that runs before
deployment selection, and therefore before the refusal exists.  It shortens
nothing unless the *exact* count LiteLLM itself computes is over the window the
model group declares, and it never touches the current turn's own message or
the instructions.

What it elides, oldest first:

* inline images from earlier turns, replaced with the same placeholders the
  image-context setting writes (a file path the client's viewer opens),
* ``function_call_output`` bodies (file dumps, command output, tool results),
* text content of earlier ``message`` items.

Each elided part is replaced in place, so the transcript keeps its shape: an
elided tool result is still the result of the call that produced it.
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

#: A shrink has to remove at least this share of the payload to be worth a
#: resend.  A rewrite that leaves the body within a rounding error of what was
#: just refused is the same bytes going back up, and the answer to those bytes
#: is already known.
_MIN_SHRINK_FRACTION = 0.9

#: The exact size of the request this task is serving, as the router counts it.
#: ``apply`` measures it anyway -- the count decides whether the request can be
#: sent at all -- and the answer the client gets back should be the same number:
#: Codex renders it as "context used" and compacts against it.  A relay that
#: reports a smaller prompt (they all count their own way) leaves that gauge
#: near zero while the proxy is already refusing the request, and the client
#: then has no reason to compact until it is far too late.
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

    Everything the wire will carry is a string somewhere in this list, so every
    string is counted.  The estimate is still not the upstream's own tokenizer,
    but it is the right order of magnitude, which the dropped-blocks count was
    not.
    """

    global _ENCODER
    if not isinstance(items, list):
        return None
    text = _strings_joined(items)
    if not text:
        return None
    try:
        import tiktoken

        if _ENCODER is None:
            _ENCODER = tiktoken.get_encoding("o200k_base")
        return len(_ENCODER.encode(text, disallowed_special=()))
    except Exception:
        return None


def _strings_joined(node: Any, out: Optional[List[str]] = None) -> str:
    """Every string inside ``node``, in order, as one blob."""

    parts = out if out is not None else []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
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

    count = count_tokens(data, router)
    key, items = payload_items(data)
    if key == "messages":
        wire = wire_tokens(items)
        if wire is not None and (count is None or wire > count):
            return wire
    return count


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
    return (
        f"[{chars} characters of earlier {label} elided to fit this model's "
        "context window; re-read the file or ask the user if you need it.]"
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
    """Indices worth shortening, largest first, never the last message."""

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
    for index in range(len(items) - 1, -1, -1):
        if _is_message_turn(items[index]) and items[index].get("role") == "user":
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
        for index in _droppable(rewritten):
            if count - freed <= target:
                break
            freed += int(len(_item_text(rewritten[index])) / chars_per_token)
            rewritten[index] = None
            changed = True
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
    _MEASURED_INPUT_TOKENS.set(target)
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
        return len(json.dumps(items or []))
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
    """

    from . import image_context as _image_context_module

    try:
        images = _image_context_module.scan(items)
    except Exception:
        return 0
    total = 0
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

    _key, items = payload_items(data)
    try:
        serialized = len(json.dumps(items or []))
    except Exception:
        return True
    return serialized / 2.5 >= window


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
    # A Messages replay is measured on the text it carries: the router's count
    # reads the chat shape and drops the blocks (tool results, thinking) that
    # make up most of a Claude Code history, so it under-reports by more than
    # the margin this pass needs.
    if payload_key == "messages":
        measured_wire = wire_tokens(payload)
        if measured_wire is not None and (count is None or measured_wire > count):
            count = measured_wire
    if count is None:
        return None
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
        fitted_count = next_count
        if next_count is None or next_count <= limit:
            current_count = next_count if next_count is not None else current_count
            break
        current, current_count = shorter, next_count
    final = result if result is not None else data
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
    )
    return dict(final)


def _turn_facts(items: Any) -> Dict[str, Any]:
    """What the pass judged: which turns the images sit in, and what it protects."""

    from . import image_context as _image_context_module

    try:
        images = _image_context_module.scan(items)
    except Exception:
        return {}
    list_items = items if isinstance(items, list) else []
    return {
        "current_turn": _image_context_module._turn_count(list_items),
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
) -> None:
    from . import routing as _routing_module
    from . import trace as _trace_module

    try:
        if after_count is None:
            after_count = count_tokens(after, router)
        _before_key, before_items = payload_items(before)
        _after_key, after_items = payload_items(after)
        _trace_module._route_trace(
            TRACE_EVENT,
            request_id=_routing_module._trace_request_id(dict(before)),
            session=_routing_module._trace_session_context(dict(before)),
            model=before.get("model"),
            window=window,
            before=count,
            after=after_count,
            # The token count and the wire size are both answers to "will this
            # be accepted", and only the pair shows which one the elision moved.
            image_bytes_before=_inline_image_bytes(before_items),
            image_bytes_after=_inline_image_bytes(after_items),
            image_pass=image_pass,
            before_facts=_turn_facts(before_items),
            after_facts=_turn_facts(after_items),
            items_before=len(before_items or []),
            items_after=len(after_items or []),
        )
    except Exception:
        # A trace must never decide a request.
        pass
