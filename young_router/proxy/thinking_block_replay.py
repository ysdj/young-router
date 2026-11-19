"""A route in thinking mode refuses a history whose tool turns carry no thinking.

DeepSeek's Anthropic-compatible endpoint states the rule in its own words::

    HTTP 400 {"type":"error","error":{"type":"invalid_request_error",
      "message":"The `content[].thinking` in the thinking mode must be passed
      back to the API."}}

Measured against ``api.deepseek.com/anthropic/v1/messages`` on 2026-11-19, one
body at a time, everything else equal:

==========================================================  ======
two assistant tool turns, both carrying ``thinking``          200
the same, second assistant turn without ``thinking``          400 (the message above)
the same, ``thinking`` present but empty (``""``)             200
a text-only assistant turn without ``thinking``               200
==========================================================  ======

So the requirement is about *tool turns*: an assistant message that contains a
``tool_use`` block must also carry the ``thinking`` block that produced it.
Nothing about the content of that block is verified -- an empty one is accepted
-- which is what makes a repair possible at all.

**Why a client's history lacks it.**  A thread served by more than one route
carries turns from each.  One route answers with ``thinking``, another answers
the same conversation through a wire that has no such block (this gateway's
Messages→Chat bridge, or an OpenAI-compatible upstream), and the client stores
what it was given.  The replay is then a mix, and the route that requires
thinking refuses the whole body because of the turns somebody else answered.
Live 2026-11-19: a Claude Code session (``d5d38978``) had 92 assistant turns,
85 of them with thinking -- every turn after the mixed history started was
refused in under two seconds, and the same route answered other sessions in
the same seconds without complaint.

**Repair only after a refusal, then remembered.**  The missing block is
inserted as ``{"type": "thinking", "thinking": "", "signature": ""}`` at the
head of that message's content, which is the position the wire reads blocks in.
It is added for a session and route that actually produced this refusal --
never speculatively, because a body that passes through unchanged is what the
upstream's prompt cache is keyed on -- and once the repaired body is accepted,
later turns of the same session on the same route are repaired before they are
sent instead of paying the refused round trip again.

**In-process only, on purpose.**  The learned repair is a fact about one
client's thread; a restarted worker re-learns it with one refused attempt (the
refusal arrives in under two seconds, see above), which is cheaper than making
every request read a shared document to find out that nothing is wrong.
"""

from __future__ import annotations

import functools
import json
import threading
import time
from typing import Any, Dict, List, Optional

from . import request_context as _request_context_module
from . import trace as _trace_module

#: The one rewrite this module writes.  Empty text and empty signature: the
#: endpoint verifies neither (measured above), and an empty block keeps the
#: body smaller than any invented text would.
PLACEHOLDER_THINKING_BLOCK: Dict[str, Any] = {
    "type": "thinking",
    "thinking": "",
    "signature": "",
}

#: Where a repaired attempt is marked, so a route that refuses it too does not
#: buy a third identical replay from the caller above.
RETRY_METADATA_KEY = "young_router_thinking_block_replay"

#: How long a learned repair is trusted: long enough for a working session, and
#: short enough that a changed history re-proves itself.
REQUIREMENT_TTL_SECONDS = 12 * 60 * 60.0
DECLINED_TTL_SECONDS = 10 * 60.0

_REQUIREMENTS: Dict[str, Dict[str, Any]] = {}
_REQUIREMENTS_LOCK = threading.Lock()

_TOOL_BLOCK_TYPES = frozenset({"tool_use", "tool_call", "function_call"})


def _block_is_thinking(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "thinking"


def _block_is_tool_call(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return str(block.get("type") or "") in _TOOL_BLOCK_TYPES


def is_thinking_required_error(exception: Exception) -> bool:
    """Whether this refusal is the "thinking must be passed back" rule.

    Matched on the endpoint's own sentence, not on the status: a 400 from the
    same route can also be a body size refusal or an unsupported parameter, and
    rewriting the history for those would be a change nothing asked for.
    """

    try:
        from . import routing as _routing_module

        status_code = _routing_module._exception_status_code(exception)
    except Exception:
        status_code = None
    if status_code not in (None, 400):
        return False
    text = str(exception).lower()
    if "content[].thinking" in text:
        return True
    return "thinking" in text and "must be passed back" in text


def with_inserted_thinking_blocks(request_kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the body with a placeholder thinking block on each tool turn.

    ``None`` means nothing needed inserting -- there is no tool turn without a
    thinking block, or the body is not a Messages-shaped history at all.
    """

    if not isinstance(request_kwargs, dict):
        return None
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return None
    changed = False
    repaired_messages: List[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            repaired_messages.append(message)
            continue
        content = message.get("content")
        if (
            str(message.get("role") or "").lower() != "assistant"
            or not isinstance(content, list)
        ):
            repaired_messages.append(message)
            continue
        if any(_block_is_thinking(block) for block in content):
            repaired_messages.append(message)
            continue
        if not any(_block_is_tool_call(block) for block in content):
            # A text-only assistant turn is accepted without thinking (measured).
            repaired_messages.append(message)
            continue
        repaired_message = message.copy()
        repaired_message["content"] = [
            dict(PLACEHOLDER_THINKING_BLOCK),
            *content,
        ]
        repaired_messages.append(repaired_message)
        changed = True
    if not changed:
        return None
    repaired = request_kwargs.copy()
    repaired["messages"] = repaired_messages
    return repaired


def _body_thinking_summary(messages: Any) -> Dict[str, int]:
    """What the body holds, so the trace can say what the repair changed.

    The endpoint's rule is about tool turns, and a thread's history is written
    by several clients, so "how many tool turns and how many of them carry
    thinking" is the only useful account of a body that was refused for it.
    """

    summary = {
        "assistant_messages": 0,
        "tool_turns": 0,
        "tool_turns_without_thinking": 0,
    }
    if not isinstance(messages, list):
        return summary
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").lower() != "assistant":
            continue
        summary["assistant_messages"] += 1
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if not any(_block_is_tool_call(block) for block in content):
            continue
        summary["tool_turns"] += 1
        if not any(_block_is_thinking(block) for block in content):
            summary["tool_turns_without_thinking"] += 1
    return summary


def _body_shape_summary(messages: Any, *, sample_limit: int = 4) -> Dict[str, Any]:
    """The content shapes in a refused body, so the next attempt can name one.

    The endpoint's sentence says a tool turn carries no thinking, and a body
    where every tool turn visibly does carry one therefore says the sentence is
    about something this counter does not model -- a different block spelling, a
    string content, a role the wire reads differently.  Only the refusal path
    pays for this, and only the shapes (never the text) are recorded.
    """

    shapes: Dict[str, int] = {}
    positions: Dict[str, int] = {}
    samples: List[Dict[str, Any]] = []
    if not isinstance(messages, list):
        return {"shapes": shapes, "positions": positions, "samples": samples}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")
        if isinstance(content, str):
            key = f"{role}:str"
        elif isinstance(content, list):
            kinds = [
                str(block.get("type") or "?") if isinstance(block, dict) else "?"
                for block in content
            ]
            key = f"{role}:{'+'.join(kinds) or 'empty'}"
        else:
            key = f"{role}:{type(content).__name__}"
        shapes[key] = shapes.get(key, 0) + 1
        if role == "assistant" and key.startswith("assistant:") and "tool_use" in key:
            positions[str(index)] = positions.get(str(index), 0) + 1
        if role != "assistant" and isinstance(content, str):
            samples.append({"index": index, "role": role, "keys": sorted(message)})
        elif role == "assistant" and isinstance(content, list) and "thinking" not in key:
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "index": index,
                        "role": role,
                        "keys": sorted(message),
                        "block_keys": [
                            sorted(block) if isinstance(block, dict) else None
                            for block in content
                        ][:3],
                    }
                )
    return {
        "shapes": dict(sorted(shapes.items(), key=lambda item: -item[1])[:8]),
        "positions": dict(list(positions.items())[-3:]),
        "samples": samples[:sample_limit],
    }


#: Where a refused body may be written for offline analysis, when the operator
#: asks for it with ``YOUNG_ROUTER_CAPTURE_ANTHROPIC_BODY=/path.json``.  Off by
#: default: a body is the client's content.  One file per process, written at
#: the moment a repaired body is about to be resent, because that is the body an
#: endpoint keeps refusing and the only way to see what it is actually objecting
#: to when every shape the counters know is already correct.
CAPTURE_PATH_ENV = "YOUNG_ROUTER_CAPTURE_ANTHROPIC_BODY"
_CAPTURE_STATE: Dict[str, Any] = {"written": False}


def _capture_body(request_kwargs: Any, exception: Exception) -> None:
    import os

    path = os.getenv(CAPTURE_PATH_ENV, "").strip()
    if not path or _CAPTURE_STATE.get("written"):
        return
    try:
        payload = {
            "captured_at": time.time(),
            "refusal": str(exception)[:2000],
            "body": {
                key: value
                for key, value in (request_kwargs or {}).items()
                if key in ("model", "messages", "system", "max_tokens", "thinking",
                           "tools", "tool_choice", "temperature", "top_p", "stream")
            },
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        _CAPTURE_STATE["written"] = True
    except Exception:
        pass


def _requirement_cache_key(request_kwargs: Any) -> Optional[str]:
    """The session and route a learned repair belongs to."""

    if not isinstance(request_kwargs, dict):
        return None
    from . import routing as _routing_module

    deployment = _routing_module._deployment_id_from_request(
        request_kwargs
    ) or _routing_module._deployment_route_key_from_request(request_kwargs)
    if not deployment:
        return None
    session = _routing_module._trace_session_context(request_kwargs).get("id")
    return f"session:{session or '-'}:route:{deployment}"


def _record_requirement(request_kwargs: Any, status: str, ttl: float) -> None:
    cache_key = _requirement_cache_key(request_kwargs)
    if not cache_key:
        return
    now = time.time()
    with _REQUIREMENTS_LOCK:
        for existing_key, existing in list(_REQUIREMENTS.items()):
            try:
                expires_at = float(existing.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                expires_at = 0.0
            if expires_at <= now:
                _REQUIREMENTS.pop(existing_key, None)
        _REQUIREMENTS[cache_key] = {
            "status": status,
            "expires_at": now + ttl if ttl > 0 else 0.0,
        }
    from . import routing as _routing_module

    _trace_module._route_trace(
        "thinking_block_replay_recorded",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_request_context_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        cache_key=cache_key,
        status=status,
        ttl_seconds=ttl,
    )


def _recorded_requirement(request_kwargs: Any) -> Optional[str]:
    cache_key = _requirement_cache_key(request_kwargs)
    if not cache_key:
        return None
    now = time.time()
    with _REQUIREMENTS_LOCK:
        entry = _REQUIREMENTS.get(cache_key)
        if entry is None:
            return None
        try:
            expires_at = float(entry.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at <= now:
            _REQUIREMENTS.pop(cache_key, None)
            return None
        return str(entry.get("status") or "") or None


def _already_attempted(request_kwargs: Any) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    return metadata.get(RETRY_METADATA_KEY) is True


def _mark_attempted(request_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    updated_metadata = metadata.copy()
    updated_metadata[RETRY_METADATA_KEY] = True
    request_kwargs["litellm_metadata"] = updated_metadata
    return request_kwargs


def _with_recorded_repair(request_kwargs: Any) -> Optional[Dict[str, Any]]:
    """Repair a session that already proved this route needs it."""

    if not isinstance(request_kwargs, dict):
        return None
    if _recorded_requirement(request_kwargs) != "required":
        return None
    repaired = with_inserted_thinking_blocks(request_kwargs)
    if repaired is None:
        return None
    from . import routing as _routing_module

    _trace_module._route_trace(
        "thinking_block_replay_applied",
        request_id=_routing_module._trace_request_id(repaired),
        session=_routing_module._trace_session_context(repaired),
        model_group=_request_context_module._request_model_group(repaired),
        deployment_id=_routing_module._deployment_id_from_request(repaired),
        route_key=_routing_module._deployment_route_key_from_request(repaired),
    )
    return repaired


def retry_request_kwargs(
    exception: Exception,
    request_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """The repaired body to resend to the same route, or None to move on."""

    if not isinstance(request_kwargs, dict):
        return None
    if _already_attempted(request_kwargs):
        return None
    if _recorded_requirement(request_kwargs) == "declined":
        # This session already resent a repaired body to this route and was
        # refused again: the route is having an outage, and the extra attempt
        # must not be paid on every turn of it.
        return None
    if not is_thinking_required_error(exception):
        return None
    repaired = with_inserted_thinking_blocks(request_kwargs)
    if repaired is None:
        return None
    return _mark_attempted(repaired)


async def call_with_thinking_block_repair(call: Any, kwargs: dict) -> Any:
    """Call one deployment, replaying a refused history with thinking restored."""

    from . import routing as _routing_module

    prepared = _with_recorded_repair(kwargs)
    if prepared is not None:
        # In place: ``call`` closes over this dict.
        kwargs.clear()
        kwargs.update(prepared)

    try:
        return await call()
    except Exception as exc:
        retry_kwargs = retry_request_kwargs(exc, kwargs)
        if retry_kwargs is None:
            raise
        _trace_module._route_trace(
            "thinking_block_replay_start",
            request_id=_routing_module._trace_request_id(retry_kwargs),
            session=_routing_module._trace_session_context(retry_kwargs),
            model_group=_request_context_module._request_model_group(retry_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(retry_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(retry_kwargs),
            body=_body_thinking_summary(retry_kwargs.get("messages")),
            exception=_routing_module._trace_exception(exc),
        )
        kwargs.clear()
        kwargs.update(retry_kwargs)
        _capture_body(kwargs, exc)
        try:
            response = await call()
        except Exception as retry_exc:
            _record_requirement(kwargs, "declined", DECLINED_TTL_SECONDS)
            _trace_module._route_trace(
                "thinking_block_replay_error",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_request_context_module._request_model_group(kwargs),
                deployment_id=_routing_module._deployment_id_from_request(kwargs),
                route_key=_routing_module._deployment_route_key_from_request(kwargs),
                body=_body_thinking_summary(kwargs.get("messages")),
                body_shape=_body_shape_summary(kwargs.get("messages")),
                exception=_routing_module._trace_exception(retry_exc),
            )
            raise
        _record_requirement(kwargs, "required", REQUIREMENT_TTL_SECONDS)
        return response


_BRIDGE_ATTR = "_young_router_thinking_block_repair"


def wrap_deployment_function(original: Any) -> Any:
    """Wrap one deployment's call with the thinking-block repair."""

    if getattr(original, _BRIDGE_ATTR, False):
        return original

    async def repaired(**call_kwargs: Any) -> Any:
        return await call_with_thinking_block_repair(
            lambda: original(**call_kwargs), call_kwargs
        )

    functools.update_wrapper(repaired, original)
    setattr(repaired, _BRIDGE_ATTR, True)
    return repaired


__all__ = [
    "DECLINED_TTL_SECONDS",
    "PLACEHOLDER_THINKING_BLOCK",
    "REQUIREMENT_TTL_SECONDS",
    "RETRY_METADATA_KEY",
    "_recorded_requirement",
    "_with_recorded_repair",
    "call_with_thinking_block_repair",
    "is_thinking_required_error",
    "retry_request_kwargs",
    "with_inserted_thinking_blocks",
    "wrap_deployment_function",
]
