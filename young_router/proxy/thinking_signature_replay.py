"""A replayed thinking signature only verifies on the account that issued it.

Anthropic's Messages wire lets a client replay an assistant turn's ``thinking``
block together with the ``signature`` it was given, and it verifies that
signature when the block comes back.  A signature is worth exactly one issuer:
the account that produced it.  Measured against one station route
(``sisct2 / Claude Max``), one history, one signed block, otherwise identical
bodies:

===========================================================  ======
signature exactly as the client received it                   502
``signature`` present but ``""``                              200
whole ``thinking`` block removed                              200
``thinking`` text emptied, signature kept                     502
a signature this route issued itself, replayed to the route   200
===========================================================  ======

So the verdict is about the *signature*, not about the block, not about the
history, and not about the size of the request.  A thread whose turns were
answered by two different upstreams therefore carries a mixed history: the
blocks signed by the route that answered yesterday are refused by the route
that answers today, and since the replay is the same bytes, every attempt meets
the same refusal.

**The refusal never arrives as one.**  New API / sub2api answer every upstream
4xx with its own envelope -- ``502 {"type":"upstream_error","message":"Upstream
request failed after retries"}`` -- so this proxy sees a 5xx about the route and
parks it, while the bytes are the thing that was refused.  A client then retried
the identical history nine times (measured 2026-11-13, one Claude Code thread at
924,938 tokens) before it showed "Server error".  Blanking the signatures of
that same body answers 200, which is what makes a repair possible at all.

**Only after a refusal, and only for the session that needed it.**  Rewriting a
history changes the bytes an upstream caches, so this must not be applied
speculatively: a ``thinking`` signature that verifies is evidence the route is
the right one for that history, and blanking it would throw the proof away and
rebuild the prompt cache for nothing.  The requirement is therefore learned from
the retry that succeeded and remembered per *session and route*, never per route
alone: one session's foreign history says nothing about another's.

**The blanked form is canonical.**  Every replay of the learned session is
rewritten the same way -- ``signature`` becomes ``""``, the ``thinking`` text
stays -- so the bytes the upstream caches are stable from the first rewritten
turn on.  A route that answers the blanked body has proved this session's
history is servable there, and later turns are repaired before they are sent
instead of paying the same refused round trip again.
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any, Dict, List, Optional

from . import request_context as _request_context_module
from . import trace as _trace_module

#: The only canonical rewrite this module writes.  Empty, not absent: the field
#: is what the wire reads (measured above -- a kept signature is refused even
#: when the thinking text is empty), and an empty string is what the same wire
#: answers when it cannot verify anything.
BLANK_SIGNATURE = ""

#: Where a repaired attempt is marked.  It rides in ``litellm_metadata`` beside
#: the other per-attempt markers, and it is what keeps a second refusal from
#: buying a third identical replay.
RETRY_METADATA_KEY = "young_router_thinking_signature_replay"

#: How long a learned rewrite is trusted.  Expiring it costs one refused round
#: trip (no generation) and nothing else, so the window can cover a working
#: session while still letting a route that is re-pointed at another station
#: stop being sent the blanked form on its own.
REQUIREMENT_TTL_SECONDS = 6 * 3600.0

#: A route that refused the blanked body too answered about something else --
#: an outage, a body this proxy built wrongly, a station that is down.  That
#: answer is remembered briefly so an outage does not pay one extra attempt per
#: turn; long enough to cover an incident, short enough to re-learn after it.
DECLINED_TTL_SECONDS = 15 * 60.0

#: How stale the in-process copy may be before it is read again from the shared
#: file.  A lookup must not open the file per request; a newly learned entry is
#: published into memory by the worker that learned it, so this only bounds how
#: long another worker keeps answering from an older copy.
_REQUIREMENT_REFRESH_SECONDS = 5.0

_REQUIREMENTS: Dict[str, Dict[str, Any]] = {}
_REQUIREMENTS_LOCK = threading.Lock()
_REQUIREMENTS_LOADED_AT = 0.0

#: The shared document, beside the deployment cooldowns: the same kind of fact
#: (an answer a route gave about itself), kept in the file so sixteen workers do
#: not each pay the same refused round trip to learn it.
_REQUIREMENT_STATE_KEY = "thinking_signature_replay"


def _is_thinking_block(block: Any) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") == "thinking"
        and isinstance(block.get("signature"), str)
        and bool(block["signature"])
    )


def has_signed_thinking(messages: Any) -> bool:
    """Whether this body replays at least one signed thinking block."""

    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _is_thinking_block(block):
                return True
    return False


def with_blanked_thinking_signatures(
    request_kwargs: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return this replay with every replayed signature blanked, or ``None``.

    Only the signature changes: the ``thinking`` text, the block order, the tool
    pairing and every other byte of the body are left exactly as the client sent
    them, so an upstream that accepted the previous rewritten turn sees the same
    prefix again.  ``None`` means the body carried no signed thinking, so a
    caller never resends a body it did not change.
    """

    messages = request_kwargs.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    rewritten: List[Any] = []
    changed = False
    for message in messages:
        if not isinstance(message, dict):
            rewritten.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            rewritten.append(message)
            continue
        blocks: List[Any] = []
        message_changed = False
        for block in content:
            if _is_thinking_block(block):
                updated = dict(block)
                updated["signature"] = BLANK_SIGNATURE
                blocks.append(updated)
                message_changed = True
                continue
            blocks.append(block)
        if message_changed:
            rewritten.append(dict(message, content=blocks))
            changed = True
        else:
            rewritten.append(message)
    if not changed:
        return None
    return dict(request_kwargs, messages=rewritten)


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
    """State on the replay itself that it is the repaired one."""

    metadata = _request_context_module._request_metadata_dict(
        retry_kwargs, "litellm_metadata"
    )
    retry_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    retry_metadata[RETRY_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    return retry_kwargs


def _retry_is_worth_it(exception: Exception) -> bool:
    """Whether this failure can be a signature refusal at all.

    The station answers every upstream 4xx as its own 5xx envelope, so the
    signature verdict never arrives under its own status and a text gate would
    never fire -- which is exactly how the turn below kept looping.  What is
    excluded is the answers that cannot be about a signature: this proxy's own
    refusals, a body too large for the route's own count, and a rate limit (a
    throttle would only be repaid with a second attempt).
    """

    from . import routing as _routing_module

    if _routing_module._is_local_turn_refusal_error(exception):
        return False
    if _routing_module._is_local_gateway_auth_rejection_error(exception):
        return False
    if _routing_module._is_context_size_error(exception):
        return False
    status_code = _routing_module._exception_status_code(exception)
    if status_code in (413, 429):
        return False
    return True


def retry_request_kwargs(
    exception: Exception,
    request_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """The replay to send back to the same route, or ``None`` to move on.

    A route that cannot verify this history's signatures answers the same bytes
    the same way forever, so the only attempt worth making is one with the
    signatures blanked -- and only once per attempt, so a second refusal is
    read as something else.
    """

    if not isinstance(request_kwargs, dict):
        return None
    if _already_attempted(request_kwargs):
        return None
    if _record_declined_requirement(request_kwargs):
        # This session already asked this route to verify a rewritten history
        # and was refused: an outage, or a body the rewrite does not fix.  The
        # answer is remembered briefly so the extra attempt is not paid again
        # on every turn of the same incident.
        return None
    if not has_signed_thinking(request_kwargs.get("messages")):
        return None
    if not _retry_is_worth_it(exception):
        return None
    repaired = with_blanked_thinking_signatures(request_kwargs)
    if repaired is None:
        return None
    return _mark_attempted(repaired)


def _requirement_state_path() -> Optional[str]:
    from . import routing as _routing_module

    return _routing_module._deployment_cooldown_file_path()


def _requirement_cache_key(request_kwargs: Any) -> Optional[str]:
    """The *session* and *route* a learned rewrite belongs to.

    Session-scoped on purpose.  A signature is foreign relative to one history:
    the session that had to be rewritten says nothing about a session whose
    history this route signed itself, and rewriting that one would discard a
    working signature and rebuild its prompt cache for no reason.
    """

    if not isinstance(request_kwargs, dict):
        return None
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    from . import routing as _routing_module

    deployment = _routing_module._deployment_id_from_request(
        request_kwargs
    ) or _routing_module._deployment_route_key_from_request(request_kwargs)
    if not deployment:
        return None
    session = _routing_module._trace_session_context(request_kwargs).get("id")
    return f"session:{session or '-'}:route:{deployment}"


def _clean_entry(entry: Any, now: float) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    status = entry.get("status")
    if status not in ("unsigned", "declined"):
        return None
    try:
        expires_at = float(entry.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if expires_at <= now:
        return None
    cleaned = dict(entry)
    cleaned["expires_at"] = expires_at
    return cleaned


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
            payload.get(_REQUIREMENT_STATE_KEY) if isinstance(payload, dict) else None
        )
        if not isinstance(entries, dict):
            return
        fresh: Dict[str, Dict[str, Any]] = {}
        for cache_key, entry in entries.items():
            cleaned = _clean_entry(entry, now)
            if cleaned is not None:
                fresh[str(cache_key)] = cleaned
        _REQUIREMENTS.clear()
        _REQUIREMENTS.update(fresh)


def _recorded_entry(request_kwargs: Any) -> Optional[Dict[str, Any]]:
    cache_key = _requirement_cache_key(request_kwargs)
    if not cache_key:
        return None
    now = time.time()
    with _REQUIREMENTS_LOCK:
        entry = _REQUIREMENTS.get(cache_key)
    if entry is None:
        _refresh_requirements_from_shared(now)
        with _REQUIREMENTS_LOCK:
            entry = _REQUIREMENTS.get(cache_key)
    if entry is None:
        return None
    if entry["expires_at"] <= now:
        with _REQUIREMENTS_LOCK:
            _REQUIREMENTS.pop(cache_key, None)
        return None
    return entry


def _recorded_repair_requirement(request_kwargs: Any) -> bool:
    """Whether this session already had its signatures blanked on this route."""

    entry = _recorded_entry(request_kwargs)
    return bool(entry and entry.get("status") == "unsigned")


def _record_declined_requirement(request_kwargs: Any) -> bool:
    entry = _recorded_entry(request_kwargs)
    return bool(entry and entry.get("status") == "declined")


def _record_requirement(request_kwargs: Any, status: str, ttl: float) -> None:
    """Remember what this route answered for this session's history."""

    cache_key = _requirement_cache_key(request_kwargs)
    if not cache_key:
        return
    now = time.time()
    entry = {
        "status": status,
        "cache_key": cache_key,
        "detected_at": now,
        "expires_at": now + ttl,
    }
    with _REQUIREMENTS_LOCK:
        _REQUIREMENTS[cache_key] = entry

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
        "thinking_signature_replay_recorded",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_request_context_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        cache_key=cache_key,
        status=status,
        ttl_seconds=ttl,
        expires_at=entry["expires_at"],
    )


def _record_into_payload(payload: Any, cache_key: str, entry: Dict[str, Any]) -> None:
    """Write one entry into the shared document, dropping expired ones."""

    if not isinstance(payload, dict):
        return
    entries = payload.setdefault(_REQUIREMENT_STATE_KEY, {})
    if not isinstance(entries, dict):
        entries = {}
        payload[_REQUIREMENT_STATE_KEY] = entries
    now = time.time()
    for existing_key, existing in list(entries.items()):
        if _clean_entry(existing, now) is None:
            entries.pop(existing_key, None)
    entries[cache_key] = entry.copy()


async def call_with_signature_repair(
    call: Any,
    kwargs: dict,
) -> Any:
    """Call one deployment, replaying it without signatures it cannot verify.

    The refusal this answers is invisible: the station reports every upstream
    4xx as its own 5xx envelope, so there is no status and no text to gate on.
    The gate is therefore the body plus one attempt -- a session that already
    proved it needs the rewrite is rewritten before it is sent, and a session
    that has not is retried exactly once with the signatures blanked.  A route
    that answers the rewritten body has proved the rewrite is what it wanted,
    and that fact is remembered for this session on this route; a route that
    refuses it too is remembered as having declined, so an outage does not pay
    the extra attempt on every turn.
    """

    from . import routing as _routing_module

    prepared = _with_recorded_repair(kwargs)
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
            "thinking_signature_replay_start",
            request_id=_routing_module._trace_request_id(retry_kwargs),
            session=_routing_module._trace_session_context(retry_kwargs),
            model_group=_request_context_module._request_model_group(retry_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(retry_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(retry_kwargs),
            exception=_routing_module._trace_exception(exc),
        )
        kwargs.clear()
        kwargs.update(retry_kwargs)
        try:
            response = await call()
        except Exception as retry_exc:
            _record_requirement(kwargs, "declined", DECLINED_TTL_SECONDS)
            _trace_module._route_trace(
                "thinking_signature_replay_error",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_request_context_module._request_model_group(kwargs),
                deployment_id=_routing_module._deployment_id_from_request(kwargs),
                route_key=_routing_module._deployment_route_key_from_request(kwargs),
                exception=_routing_module._trace_exception(retry_exc),
            )
            raise
        _record_requirement(kwargs, "unsigned", REQUIREMENT_TTL_SECONDS)
        return response


def _with_recorded_repair(request_kwargs: Any) -> Optional[Dict[str, Any]]:
    """Repair the replay of a session already rewritten on this route.

    Runs before the body goes out so a session that proved it needs the rewrite
    is handed it on the first attempt instead of paying the same refusal every
    turn.  The bytes it writes are the ones the retry that proved it wrote, so
    the prompt prefix an upstream cached stays stable from turn to turn.
    """

    if not isinstance(request_kwargs, dict):
        return None
    if not _recorded_repair_requirement(request_kwargs):
        return None
    repaired = with_blanked_thinking_signatures(request_kwargs)
    if repaired is None:
        return None
    from . import routing as _routing_module

    _trace_module._route_trace(
        "thinking_signature_replay_applied",
        request_id=_routing_module._trace_request_id(repaired),
        session=_routing_module._trace_session_context(repaired),
        model_group=_request_context_module._request_model_group(repaired),
        deployment_id=_routing_module._deployment_id_from_request(repaired),
        route_key=_routing_module._deployment_route_key_from_request(repaired),
    )
    return repaired


_BRIDGE_ATTR = "_young_router_thinking_signature_repair"


def wrap_deployment_function(original: Any) -> Any:
    """Wrap one deployment's call with the signature repair.

    Sits outside the transport wrappers so a rewritten attempt re-enters
    whatever wire handling the deployment normally gets, and inside the route's
    failover so the attempt is still charged to the same deployment.
    """

    if getattr(original, _BRIDGE_ATTR, False):
        return original

    async def repaired(**call_kwargs: Any) -> Any:
        return await call_with_signature_repair(
            lambda: original(**call_kwargs), call_kwargs
        )

    functools.update_wrapper(repaired, original)
    setattr(repaired, _BRIDGE_ATTR, True)
    return repaired


__all__ = [
    "BLANK_SIGNATURE",
    "DECLINED_TTL_SECONDS",
    "REQUIREMENT_TTL_SECONDS",
    "RETRY_METADATA_KEY",
    "_record_declined_requirement",
    "_recorded_repair_requirement",
    "_with_recorded_repair",
    "call_with_signature_repair",
    "has_signed_thinking",
    "retry_request_kwargs",
    "with_blanked_thinking_signatures",
    "wrap_deployment_function",
]
