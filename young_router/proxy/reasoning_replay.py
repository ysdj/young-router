"""A thinking-mode replay has to carry the thinking its own route gave it.

DeepSeek's ``/v1/responses`` keeps a turn's thinking in ``reasoning_text``
content parts and refuses a replayed history that comes back without them::

    HTTP 400 the `reasoning_text` in the thinking mode must be passed back to the API

Measured against ``api.deepseek.com`` -- one history, one tool call, otherwise
identical bodies:

===============================================  ======
reasoning item carrying only ``encrypted_content``   400
reasoning item carrying only ``summary``             400
``reasoning_text`` present but ``""``                400
``reasoning_text`` present as one space              200
two items, only one of them repaired                 200
===============================================  ======

So the requirement is per *item*, and presence is not the question -- an empty
part answers exactly as little as a missing one, while one character answers.
A client replays what its own transcript kept: Pi keeps the encrypted blob
beside the summary, Codex keeps the opaque id, and neither is readable text.
Every replayed turn with a tool call in it was therefore refused on the fastest
route of the group, before its answer could start, and the turn advanced to
whatever sibling answered the same history (measured: a 3 s relay taking the
place of a 160 ms route).

The repair writes a placeholder, never thinking.  The text this wire asks for
is the text it produced, which the client deliberately did not keep, and an
opaque blob states exactly as much as one space does -- the same reading the
chat bridge already applies when it echoes ``reasoning_content: ""``
(``patches._install_responses_chat_bridge_reasoning_replay_patch``).

The Chat wire of the same route has the same refusal under its own spelling
(``the `reasoning_content` in the thinking mode must be passed back``), and a
chat-shaped client replay is repaired the same way, with the field present and
empty: measured on the same upstream, ``""`` answers there while a missing field
does not.  One route, two wires, one rule about presence.

**Only after a refusal, and only once.**  The body changes only when the wire
itself said it wants the field, so a route whose validator would reject the
extra part never sees it, and the placeholder is proven by the retry that
follows it.  A route that refuses the repaired body too has answered about
something else; the marker keeps that second answer from being repaid a third
time with the same bytes.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

from . import request_context as _request_context_module
from . import trace as _trace_module

#: What a repaired item carries instead of the thinking it lost.  One space, not
#: ``""``: an empty part is refused exactly like a missing one (measured above),
#: so the placeholder has to be readable to satisfy the validator while saying
#: nothing a model could mistake for its own reasoning.
REASONING_TEXT_PLACEHOLDER = " "

#: What the same contract costs on the Chat wire, measured the same way: an
#: assistant message that carries tool calls and no ``reasoning_content`` at all
#: is refused, while the field being present with ``""`` answers.  One spelling
#: empties, the other needs a character -- the wires disagree about blankness,
#: not about presence.
REASONING_CONTENT_PLACEHOLDER = ""

#: Where a repaired attempt is marked.  It rides in ``litellm_metadata`` with
#: the other per-attempt markers, and it is what stops a second refusal from
#: buying a third identical replay.
RETRY_METADATA_KEY = "young_router_responses_reasoning_text_replay"

_READABLE_TEXT_FIELDS = ("reasoning_text", "text")


def _readable_reasoning_text(item: Dict[str, Any]) -> Optional[str]:
    """The text this item already passes back, if it passes any back.

    ``non-empty``, not ``non-blank``: the wire's own rule is that an empty
    string states nothing while any character answers, and this has to read the
    same rule -- read stricter and a repaired replay looks unrepaired to the
    next pass, which then writes a second placeholder into it.
    """

    content = item.get("content")
    if not isinstance(content, list):
        return None
    for part in content:
        if not isinstance(part, dict):
            continue
        for field in _READABLE_TEXT_FIELDS:
            text = part.get(field)
            if isinstance(text, str) and text:
                return text
    return None


def _is_blank_reasoning_text_part(part: Any) -> bool:
    """Whether this part is the field at all, holding nothing."""

    return (
        isinstance(part, dict)
        and part.get("type") == "reasoning_text"
        and not (isinstance(part.get("text"), str) and part["text"])
    )


def _summary_text(item: Dict[str, Any]) -> Optional[str]:
    """The thinking this item already publishes as a summary, if it keeps one.

    This router delivers a native route's thinking as ``summary`` and strips the
    raw ``content`` part on purpose, so a client replays a summary and no
    content.  Measured on ``api.deepseek.com``: that summary holds the model's
    actual words, and the wire refuses the replay anyway, because it reads
    ``content[].reasoning_text`` and nothing else.  The text is therefore
    already here -- moving it onto the field the wire reads is a translation,
    not an invention.
    """

    summary = item.get("summary")
    if not isinstance(summary, list):
        return None
    for part in summary:
        if not isinstance(part, dict):
            continue
        for field in ("text", "summary_text"):
            text = part.get(field)
            if isinstance(text, str) and text:
                return text
    return None


def _repaired_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The same item with the thinking text on the field the wire reads."""

    if _readable_reasoning_text(item) is not None:
        return None
    content = item.get("content")
    parts = list(content) if isinstance(content, list) else []
    # A part that is present but blank is what the wire refused, so it is
    # replaced rather than joined by a second part saying the same nothing.
    kept = [part for part in parts if not _is_blank_reasoning_text_part(part)]
    # The turn's own thinking first: a summary this router published is the
    # text that produced the turn, and replaying it verbatim keeps the history
    # honest.  The placeholder is only for a turn whose thinking is truly gone.
    text = _summary_text(item) or REASONING_TEXT_PLACEHOLDER
    kept.append({"type": "reasoning_text", "text": text})
    repaired = dict(item)
    repaired["content"] = kept
    return repaired


def _repaired_message(message: Any) -> Optional[Dict[str, Any]]:
    """The same assistant turn with the thinking field present, on the Chat wire."""

    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return None
    if "reasoning_content" in message:
        return None
    return dict(message, reasoning_content=REASONING_CONTENT_PLACEHOLDER)


def _synthesized_reasoning_item(seed: Any) -> Dict[str, Any]:
    """A thinking item for a turn whose own thinking the client never kept.

    ``id`` is derived from the tool call that turn ends in, so a replay repaired
    twice says the same thing both times -- two different ids for one turn read
    as two thinking steps that never happened.
    """

    text = seed if isinstance(seed, str) and seed.strip() else "replay"
    return {
        "type": "reasoning",
        "id": f"yr_reasoning_{text}",
        "summary": [],
        "content": [
            {"type": "reasoning_text", "text": REASONING_TEXT_PLACEHOLDER}
        ],
    }


def _is_segment_boundary(item: Any) -> bool:
    """Whether this item closes the assistant segment before it.

    A user turn and a tool result both open what the model answers next; what
    came before either of them belongs to a turn that has already been answered.
    """

    if not isinstance(item, dict):
        return False
    if item.get("type") == "function_call_output":
        return True
    return item.get("type") == "message" and item.get("role") == "user"


def _resign_tool_turns(items: List[Any]) -> Optional[List[Any]]:
    """Give every tool-calling turn the thinking item it is missing.

    A thinking-mode wire reads the whole history as one chain of turns, and each
    turn that ends in a tool call has to still carry the thinking that produced
    it.  An item the client dropped -- or never had, for a turn it restored from
    a summary -- leaves that turn empty, and the wire refuses the entire replay.

    The item goes at the **start** of the turn, not beside the tool call.  With
    a ``tools`` declaration in the request the upstream is strict about that:
    measured on ``api.deepseek.com``, one history with one tool call answered
    every time the placeholder opened the turn and was refused with HTTP 400
    every time it sat immediately before the ``function_call``.
    """

    repaired: List[Any] = []
    segment: List[Any] = []
    segment_has_call = False
    segment_has_text = False

    def flush() -> None:
        nonlocal segment, segment_has_call, segment_has_text
        if segment and segment_has_call and not segment_has_text:
            # Named after the call this turn ends in, not after whatever item
            # happened to open it.
            seed = next(
                (
                    item.get("call_id") or item.get("id")
                    for item in segment
                    if isinstance(item, dict) and item.get("type") == "function_call"
                ),
                None,
            )
            repaired.append(_synthesized_reasoning_item(seed))
        repaired.extend(segment)
        segment = []
        segment_has_call = False
        segment_has_text = False

    for item in items:
        if _is_segment_boundary(item):
            flush()
            repaired.append(item)
            continue
        if not isinstance(item, dict):
            segment.append(item)
            continue
        kind = item.get("type")
        if kind == "function_call":
            segment_has_call = True
        elif kind == "reasoning" and _readable_reasoning_text(item) is not None:
            segment_has_text = True
        segment.append(item)
    flush()
    return repaired if repaired != list(items) else None


def with_reasoning_text_replay(request_kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return this replay with its thinking carried, or ``None`` if it already is.

    Both wires of one thinking-mode route refuse the same absence under their
    own spelling, so both shapes are read here: a Responses replay through its
    ``reasoning`` items, a Chat replay through the ``reasoning_content`` of the
    assistant turn that made the tool calls.  A thinking item that is present but
    unreadable is rewritten, and a turn that carries a tool call with no thinking
    item at all gets one inserted -- both were measured against
    ``api.deepseek.com`` and both are answered while the original body is refused.

    Only what is missing is written; everything else is left byte-for-byte
    alone.  ``None`` means there was nothing to repair, so a caller never
    resends a body it did not change.
    """

    items = request_kwargs.get("input")
    if isinstance(items, list) and items:
        repaired_items: List[Any] = []
        changed = False
        for item in items:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                repaired = _repaired_item(item)
                if repaired is not None:
                    repaired_items.append(repaired)
                    changed = True
                    continue
            repaired_items.append(item)
        filled = _resign_tool_turns(repaired_items)
        if filled is not None:
            repaired_items = filled
            changed = True
        if changed:
            return dict(request_kwargs, input=repaired_items)
        return None
    messages = request_kwargs.get("messages")
    if isinstance(messages, list) and messages:
        repaired_messages: List[Any] = []
        changed = False
        for message in messages:
            repaired = _repaired_message(message)
            if repaired is not None:
                repaired_messages.append(repaired)
                changed = True
                continue
            repaired_messages.append(message)
        if changed:
            return dict(request_kwargs, messages=repaired_messages)
    return None


def _already_attempted(request_kwargs: Dict[str, Any]) -> bool:
    if request_kwargs.get(RETRY_METADATA_KEY) is True:
        return True
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs, metadata_key
        )
        if isinstance(metadata, dict) and metadata.get(RETRY_METADATA_KEY) is True:
            return True
    return False


def _mark_attempted(retry_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """State on the replay itself that it is the repaired one.

    The marker travels in ``litellm_metadata`` beside the other per-attempt
    facts, so the retry that carries it is recognisable even after LiteLLM has
    rebuilt the kwargs for another hop.
    """

    metadata = _request_context_module._request_metadata_dict(
        retry_kwargs, "litellm_metadata"
    )
    retry_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    retry_metadata[RETRY_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    return retry_kwargs


def retry_request_kwargs(
    exception: Exception,
    request_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """The replay to send back to the same route, or ``None`` to move on.

    The gate is the route's own refusal: this only answers a wire that said the
    thinking was not passed back.  Every other 400 -- an unknown field, a tool
    history that no longer pairs up -- is a verdict about something a
    placeholder cannot fix, and it keeps its ordinary reading.
    """

    from . import routing as _routing_module

    if not isinstance(request_kwargs, dict):
        return None
    if _already_attempted(request_kwargs):
        return None
    if not _routing_module._is_thinking_replay_contract_error(exception):
        return None
    repaired = with_reasoning_text_replay(request_kwargs)
    if repaired is None:
        return None
    return _mark_attempted(repaired)


#: What a route proved it wants, kept beside the deployment cooldowns because it
#: is the same kind of fact: a per-route answer learned from the wire itself.
#: Shared through the file rather than held per worker, so sixteen workers do
#: not each pay the same refused round trip to learn it.
_REQUIREMENT_STATE_KEY = "reasoning_text_replay"

#: How long a learned requirement is trusted.  Expiring it costs one refused
#: round trip (~150 ms, no generation) and nothing else, which is why the window
#: can be long enough to cover a working session yet short enough that a route
#: re-pointed at another station stops being sent the field on its own.
REQUIREMENT_TTL_SECONDS = 6 * 3600.0

#: How stale the in-process copy may be before it is read again from the shared
#: file.  A lookup must not open the file per request; a newly learned entry is
#: published into memory by the worker that learned it, so this only bounds how
#: long another worker keeps answering from an older copy.
_REQUIREMENT_REFRESH_SECONDS = 5.0

_REQUIREMENTS: Dict[str, float] = {}
_REQUIREMENTS_LOCK = threading.Lock()
_REQUIREMENTS_LOADED_AT = 0.0


def _replay_wire(request_kwargs: Any) -> Optional[str]:
    """Which wire this body is: its shape states it, nothing else has to."""

    if not isinstance(request_kwargs, dict):
        return None
    if isinstance(request_kwargs.get("input"), list):
        return "responses"
    if isinstance(request_kwargs.get("messages"), list):
        return "chat"
    return None


def _requirement_cache_key(request_kwargs: Any) -> Optional[str]:
    """The route *and wire* a requirement belongs to.

    Per wire, because the two wires of one route are told the same thing about
    the same history and are repaired in different fields: a fact learned on
    Chat must not rewrite a Responses replay that route never refused.
    """

    if not isinstance(request_kwargs, dict):
        return None
    wire = _replay_wire(request_kwargs)
    if not wire:
        return None
    from . import routing as _routing_module

    deployment_id = _routing_module._deployment_id_from_request(request_kwargs)
    return f"id:{deployment_id}:{wire}" if deployment_id else None


def _clean_requirement(entry: Any, now: float) -> Optional[float]:
    if not isinstance(entry, dict):
        return None
    if entry.get("status") != "required":
        return None
    try:
        expires_at = float(entry.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        return None
    return expires_at if expires_at > now else None


def _requirement_state_path() -> Optional[str]:
    from . import routing as _routing_module

    return _routing_module._deployment_cooldown_file_path()


def _refresh_requirements_from_shared(now: float, *, force: bool = False) -> None:
    """Replace the in-process copy with the file's, at most once per interval."""

    global _REQUIREMENTS_LOADED_AT
    with _REQUIREMENTS_LOCK:
        if not force and now - _REQUIREMENTS_LOADED_AT < _REQUIREMENT_REFRESH_SECONDS:
            return
        _REQUIREMENTS_LOADED_AT = now
        path = _requirement_state_path()
        if not path:
            return
        try:
            from .. import runtime_store

            payload = runtime_store.read_document(path)
        except (OSError, ValueError):
            return
        entries = (
            payload.get(_REQUIREMENT_STATE_KEY)
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(entries, dict):
            return
        fresh: Dict[str, float] = {}
        for cache_key, entry in entries.items():
            expires_at = _clean_requirement(entry, now)
            if expires_at is not None:
                fresh[str(cache_key)] = expires_at
        _REQUIREMENTS.clear()
        _REQUIREMENTS.update(fresh)


def _recorded_replay_requirement(request_kwargs: Any) -> bool:
    """Whether this route has already asked for its thinking to come back."""

    cache_key = _requirement_cache_key(request_kwargs)
    if not cache_key:
        return False
    now = time.time()
    with _REQUIREMENTS_LOCK:
        expires_at = _REQUIREMENTS.get(cache_key)
    if expires_at is not None:
        if expires_at > now:
            return True
        with _REQUIREMENTS_LOCK:
            _REQUIREMENTS.pop(cache_key, None)
        return False
    _refresh_requirements_from_shared(now)
    with _REQUIREMENTS_LOCK:
        expires_at = _REQUIREMENTS.get(cache_key)
    return expires_at is not None and expires_at > now


def _record_replay_requirement(request_kwargs: Any) -> None:
    """Remember that this route answered a replay once the field was there."""

    cache_key = _requirement_cache_key(request_kwargs)
    if not cache_key:
        return
    now = time.time()
    entry = {
        "status": "required",
        "cache_key": cache_key,
        "detected_at": now,
        "expires_at": now + REQUIREMENT_TTL_SECONDS,
    }
    with _REQUIREMENTS_LOCK:
        _REQUIREMENTS[cache_key] = entry["expires_at"]

    from . import routing as _routing_module
    from . import state as _state_module

    path = _requirement_state_path()
    if path:
        try:
            _state_module._locked_json_state_update(
                path,
                lambda payload: _record_into_payload(payload, cache_key, entry),
            )
        except OSError:
            pass
    _trace_module._route_trace(
        "responses_reasoning_text_replay_recorded",
        request_id=_routing_module._trace_request_id(request_kwargs),
        model_group=_request_context_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        cache_key=cache_key,
        ttl_seconds=REQUIREMENT_TTL_SECONDS,
        expires_at=entry["expires_at"],
    )


def _record_into_payload(payload: Any, cache_key: str, entry: Dict[str, Any]) -> None:
    """Write one requirement into the shared document, dropping expired ones."""

    if not isinstance(payload, dict):
        return
    entries = payload.setdefault(_REQUIREMENT_STATE_KEY, {})
    if not isinstance(entries, dict):
        entries = {}
        payload[_REQUIREMENT_STATE_KEY] = entries
    now = time.time()
    for existing_key, existing in list(entries.items()):
        if _clean_requirement(existing, now) is None:
            entries.pop(existing_key, None)
    entries[cache_key] = entry.copy()


async def call_with_replay_repair(
    call: Any,
    kwargs: dict,
) -> Any:
    """Call one deployment, replaying it with the thinking it asked to have back.

    A thinking-mode wire refuses a replayed history whose assistant turns lost
    their ``reasoning_text`` -- a verdict about this body, not about the route.
    The route that refused is usually the fastest one the group has, so the
    refusal is answered here, once, with the same route and the repaired
    replay; only a route that refuses *that* body too moves the turn on to a
    sibling.

    The route that answers the repaired body has proved it wants the field, and
    that fact is remembered so the next turn is repaired before it is sent
    instead of paying the same refused round trip again.  Remembering it here,
    and not before the refusal, is what keeps a validator that would reject the
    extra part from ever seeing it.

    This is the one place the repair is applied, for every dispatch that reaches
    a deployment: the per-deployment ``make_call`` and the Responses helper that
    builds the client-tool adapter and Chat bridge each funnel through it, so a
    route never depends on which of them its request took.
    """

    from . import routing as _routing_module

    prepared = _with_recorded_replay_repair(kwargs)
    if prepared is not None:
        # In place: ``call`` closes over this dict, and the retry below relies
        # on the same object carrying the repaired body.
        kwargs.clear()
        kwargs.update(prepared)

    try:
        return await call()
    except Exception as exc:
        retry_kwargs = retry_request_kwargs(exc, kwargs)
        if retry_kwargs is None:
            raise
        _trace_module._route_trace(
            "responses_reasoning_text_replay_start",
            request_id=_routing_module._trace_request_id(retry_kwargs),
            session=_routing_module._trace_session_context(retry_kwargs),
            model_group=_request_context_module._request_model_group(
                retry_kwargs
            ),
            deployment_id=_routing_module._deployment_id_from_request(
                retry_kwargs
            ),
            route_key=_routing_module._deployment_route_key_from_request(
                retry_kwargs
            ),
            wire=_replay_wire(retry_kwargs),
            exception=_routing_module._trace_exception(exc),
        )
        # The caller keeps one kwargs dict for the attempt, so the replay must
        # replace its body in place rather than shadow it: the repair is what
        # the same route is about to be handed.
        kwargs.clear()
        kwargs.update(retry_kwargs)
        try:
            response = await call()
        except Exception as retry_exc:
            _trace_module._route_trace(
                "responses_reasoning_text_replay_error",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_request_context_module._request_model_group(
                    kwargs
                ),
                deployment_id=_routing_module._deployment_id_from_request(
                    kwargs
                ),
                route_key=_routing_module._deployment_route_key_from_request(
                    kwargs
                ),
                exception=_routing_module._trace_exception(retry_exc),
            )
            raise
        # A stream this proxy already decided failed is not an answer about
        # the body: recording the requirement from it would send the field to a
        # route that never asked for it.
        if not _routing_module._is_failed_responses_stream_response(response):
            _record_replay_requirement(kwargs)
        return response


def _with_recorded_replay_repair(request_kwargs: Any) -> Optional[Dict[str, Any]]:
    """Repair the replay of a route already seen asking for its thinking.

    Runs before the body goes out -- after the deployment is known, since the
    requirement belongs to the route -- so a wire that proved it wants the field
    is handed it on the first attempt instead of paying the same refusal every
    turn.
    """

    if not isinstance(request_kwargs, dict):
        return None
    if not _recorded_replay_requirement(request_kwargs):
        return None
    repaired = with_reasoning_text_replay(request_kwargs)
    if repaired is None:
        return None
    from . import routing as _routing_module

    _trace_module._route_trace(
        "responses_reasoning_text_replay_applied",
        request_id=_routing_module._trace_request_id(repaired),
        session=_routing_module._trace_session_context(repaired),
        model_group=_request_context_module._request_model_group(repaired),
        deployment_id=_routing_module._deployment_id_from_request(repaired),
        route_key=_routing_module._deployment_route_key_from_request(repaired),
        wire=_replay_wire(repaired),
    )
    return repaired


__all__ = [
    "REASONING_CONTENT_PLACEHOLDER",
    "REASONING_TEXT_PLACEHOLDER",
    "REQUIREMENT_TTL_SECONDS",
    "RETRY_METADATA_KEY",
    "_recorded_replay_requirement",
    "_with_recorded_replay_repair",
    "call_with_replay_repair",
    "retry_request_kwargs",
    "with_reasoning_text_replay",
]
