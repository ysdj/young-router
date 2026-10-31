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

from contextvars import ContextVar
from typing import Any, Dict, List, Mapping, Optional, Tuple

TRACE_EVENT = "codex_context_window_elided"
RETRY_TRACE_EVENT = "codex_context_window_shrink_retry"

#: What one inline image byte costs in the worst accounting a relay can use.
#: A token counter prices an image at a few tokens; a relay that measures the
#: prompt it receives prices the base64 it is actually sent.  Both are real --
#: a thread with 93 images was reported as 298k of a 950k window by the token
#: counter and refused by the upstream for the 37.5 MB body.  Charging images
#: at the pessimists' rate is what keeps the two verdicts from diverging.
_IMAGE_BYTES_PER_TOKEN = 2.5

#: Base64 costs a third more than the bytes it encodes.
_BASE64_OVERHEAD = 4 / 3

#: Leave this much of the window free.  A turn that arrives *at* the boundary
#: would otherwise be one tool result away from the same refusal.
_TARGET_FRACTION = 0.9

#: Below this, an item is not worth rewriting: the marker costs as much as the
#: text it replaces.
_MIN_ELIDABLE_CHARS = 400

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
    return min(windows) if windows else None


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
    return ""


def _marker(item: Mapping[str, Any], chars: int) -> str:
    kind = "tool output" if item.get("type") == "function_call_output" else "message"
    return (
        f"[{chars} characters of earlier {kind} elided to fit this model's "
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


def shorten(
    data: Mapping[str, Any],
    *,
    count: int,
    window: int,
    router: Any = None,
    target: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return a copy of ``data`` that fits, or ``None`` when it cannot."""

    items = data.get("input")
    if not isinstance(items, list) or not items:
        return None
    budget = target if target is not None else int(window * _TARGET_FRACTION)
    rewritten = _fitted_items(items, count=count, target=budget)
    if rewritten is None:
        return None
    result = dict(data)
    result["input"] = rewritten
    return result


def _fitted_items(items: List[Any], *, count: int, target: int) -> Optional[List[Any]]:
    """Drop or elide old bodies until the estimate reaches ``target`` tokens."""

    total_chars = sum(len(_item_text(item)) for item in items if isinstance(item, Mapping))
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
        replaced_chars = len(_item_text(item))
        freed += int(replaced_chars / chars_per_token)
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

    if not isinstance(data, dict) or not isinstance(data.get("input"), list):
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
    if not measured or measured <= 0:
        # The pre-call pass skips counting a request far below the declared
        # window, so there may be no measurement to scale from; the refusal is
        # the first evidence about the size that matters, and counting once
        # here is cheaper than ending the turn.
        measured = count_tokens(data, _router())
    if not measured or measured <= 0:
        return False
    # A refused prompt is usually refused for its bytes, not its tokens: the
    # images a thread keeps re-sending are the whole difference between what a
    # counter reports and what the relay receives.  Replacing them costs nothing
    # a token check would have saved, so it comes before eliding text.
    rewritten_images = _image_shrunk(data, 0)
    if rewritten_images is not None:
        data["input"] = rewritten_images["input"]
        _SHRUNK.set(True)
        _trace_retry(
            data,
            attempt=attempt,
            count=measured,
            target=measured,
            model=group,
            image_bytes=_inline_image_bytes(data.get("input")),
        )
        return True
    target = int(measured * _RETRY_FRACTIONS[attempt])
    rewritten = _fitted_items(data["input"], count=measured, target=target)
    if rewritten is None:
        return False
    data["input"] = rewritten
    _MEASURED_INPUT_TOKENS.set(target)
    _SHRUNK.set(True)
    _trace_retry(data, attempt=attempt, count=measured, target=target, model=group)
    return True


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
) -> None:
    from . import trace as _trace_module

    try:
        _trace_module._route_trace(
            RETRY_TRACE_EVENT,
            model=model or data.get("model"),
            attempt=attempt,
            before=count,
            target=target,
            image_bytes=image_bytes,
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


def pessimistic_tokens(data: Mapping[str, Any], count: int) -> int:
    """``count`` plus what the images would cost a byte-counting relay."""

    images = _inline_image_bytes(data.get("input"))
    if images <= 0:
        return count
    return count + int(images / _IMAGE_BYTES_PER_TOKEN)


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

    Three UTF-8 bytes per token is generous for CJK text, the densest case the
    counter sees, so a payload that even overestimates this way stays under
    the window cannot be refused -- skip the exact count that the router would
    otherwise pay twice for a healthy replay.
    """

    try:
        import json

        serialized = len(json.dumps(data.get("input") or []))
    except Exception:
        return True
    return serialized / 2.5 >= window


def apply(data: Any) -> Optional[Dict[str, Any]]:
    """The pre-call step: shorten only when the request cannot be sent."""

    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("input"), list):
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
    count = count_tokens(data, router)
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
    if rewritten_images is not None:
        data = rewritten_images
        next_count = count_tokens(data, router)
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
        shorter = shorten(current, count=current_count, window=window, router=router, target=target)
        if shorter is None:
            break
        result = shorter
        next_count = count_tokens(shorter, router)
        if next_count is None or next_count <= limit:
            current_count = next_count if next_count is not None else current_count
            break
        current, current_count = shorter, next_count
    final = result if result is not None else data
    if final is original:
        return None
    fitted_count = count_tokens(final, router)
    if fitted_count is not None:
        _MEASURED_INPUT_TOKENS.set(fitted_count)
    _trace(original, final, count=original_count, window=limit, router=router)
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
) -> None:
    from . import routing as _routing_module
    from . import trace as _trace_module

    try:
        after_count = count_tokens(after, router)
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
            image_bytes_before=_inline_image_bytes(before.get("input")),
            image_bytes_after=_inline_image_bytes(after.get("input")),
            image_pass=image_pass,
            before_facts=_turn_facts(before.get("input")),
            after_facts=_turn_facts(after.get("input")),
            items_before=len(before.get("input") or []),
            items_after=len(after.get("input") or []),
        )
    except Exception:
        # A trace must never decide a request.
        pass
