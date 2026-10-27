"""Output-limit continuation for the native (Chat/Anthropic) stream.

A route that reaches its own output ceiling ends the SSE stream *successfully*
with ``finish_reason: "length"``.  Nothing failed, so no recovery path runs and
the client renders a half-finished answer -- a full-document translation stops
mid-sentence -- as if the model had decided to stop there.

That is the one case the router can finish itself.  The ceiling is the route's,
not the user's: the same request that hit it can be asked to keep going from
where it stopped, and the continuation is spliced back into the stream the
client is already reading.  The client sees one answer that completes instead
of one that quietly ends.

The turn still ends on the route's terms.  Each continuation carries the text
already produced, so the model resumes rather than restarts, and the loop is
bounded by :data:`_MAX_CONTINUATION_ROUNDS` plus the request's own
``stream_idle_timeout_seconds`` budget on every upstream read.  A continuation
that fails, stalls, or repeats what was already delivered ends the turn with
the text that exists rather than discarding it.
"""

from __future__ import annotations


from . import responses_output as _responses_output_module
from . import responses_request as _responses_request_module
from . import routing as _routing_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    List,
    Optional,
    os,
    time,
)


# The route's ceiling is not the user's, so the router keeps asking for the
# rest -- but a route that answers every continuation with another ceiling is
# not making progress, and the turn must still end.  Each round is a full
# upstream call, so the bound is deliberately small.
_MAX_CONTINUATION_ROUNDS_ENV = "YOUNG_ROUTER_STREAM_CONTINUATION_MAX_ROUNDS"
_MAX_CONTINUATION_ROUNDS_DEFAULT = 8

# The assistant text carried into the next request is the continuation's own
# instruction.  Sending the whole answer back would grow the prompt by the
# output ceiling on every round, so the tail is enough to resume from: the
# model only has to know where it stopped and what it had already said.
_CONTINUATION_TAIL_CHARS = 6000

_CONTINUATION_INSTRUCTION = (
    "Your previous answer was cut off because it reached the output limit. "
    "Continue it from exactly where it stopped. Do not repeat any text that "
    "was already written, do not restate the question, and do not add a "
    "preamble. Resume in the middle of the sentence if that is where it stopped."
)

# The source the continuation no longer needs.  Re-sending the client's whole
# document every round re-asks the original question -- a document-length
# "translate all of this" -- so the model starts the answer again instead of
# resuming it, and it pays the full prompt cost on every round.  The quote is
# what the resume actually needs: enough of the source to name its place.
_CONTINUATION_SOURCE_QUOTE_CHARS = 1500

_CONTINUATION_PREAMBLE = (
    "You were translating a document. The document is quoted below, then the "
    "translation you had already produced before the output limit cut it off. "
    "Continue the translation from where it stopped; the document is here only "
    "so you can find your place, and it is not a new request."
)

_STREAM_CONTINUATION_ENV = "YOUNG_ROUTER_STREAM_CONTINUATION_ENABLED"


def _max_continuation_rounds() -> int:
    """How many times one answer may be extended after its route's ceiling.

    A ceiling means the route could not finish the answer, and a long document
    can legitimately take several rounds -- each is a full upstream call, so
    the bound trades a complete translation against spending calls on a route
    that answers every round with another ceiling.
    """

    value = os.getenv(_MAX_CONTINUATION_ROUNDS_ENV, "").strip()
    if not value:
        return _MAX_CONTINUATION_ROUNDS_DEFAULT
    try:
        parsed = int(value)
    except ValueError:
        return _MAX_CONTINUATION_ROUNDS_DEFAULT
    return max(0, parsed)


def _native_stream_output_limit_reason(chunk: Any) -> Optional[str]:
    """Return the output-ceiling reason a native terminal frame carries.

    Chat Completions states it in ``finish_reason`` and Anthropic Messages in
    ``stop_reason``/``delta.stop_reason``; both are the provider saying "the
    answer is not finished", which is exactly the case a continuation fixes.

    A transport read routinely carries the ceiling frame and the terminal frame
    that closes it together, so every frame in the read is asked: reading only
    the first one missed the ceiling reason beside a ``message_stop``, and the
    withheld frame was then never recognised as one to withhold.
    """

    from . import streaming as _streaming_module

    dumps = _streaming_module._stream_chunk_dumps(chunk)
    for dumped in dumps:
        candidates: List[Any] = []
        choices = dumped.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    candidates.append(choice.get("finish_reason"))
        delta = dumped.get("delta")
        if isinstance(delta, dict):
            candidates.append(delta.get("stop_reason"))
        candidates.append(dumped.get("stop_reason"))
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            reason = _streaming_module._output_limit_incomplete_reason_from_text(
                candidate
            )
            if reason is not None:
                return reason
    return None


# The request keys a continuation call carries.  A native request holds the
# router's own live dispatch objects (the original upstream callable, route
# bookkeeping), and a deep copy of those is not only wasteful but can fail
# outright, so the continuation is built from the chat payload's own fields.
_CONTINUATION_REQUEST_KEYS = (
    "call_type",
    "model",
    "messages",
    "temperature",
    "top_p",
    "reasoning_effort",
    "user",
    "service_tier",
    "seed",
    "stop",
    "response_format",
    "stream",
    "stream_options",
    "stream_timeout",
    "api_base",
    "api_key",
    "api_version",
    "custom_llm_provider",
    "extra_body",
    "extra_headers",
    "client_metadata",
    "prompt_cache_key",
    "metadata",
    "parallel_tool_calls",
)


def _request_is_continuable_native_stream(request_data: Optional[dict]) -> bool:
    """Whether this request is one the router may extend on its own.

    Off unless ``YOUNG_ROUTER_STREAM_CONTINUATION_ENABLED`` is turned on: the
    ceiling belongs to the client's own configuration, and most of the time the
    right fix is there rather than here.

    Only a plain text answer is extended.  A turn that declares tools is
    deliberately excluded: a ceiling inside a tool-call turn means the call
    the model was writing is incomplete, and replaying that as more text would
    hand the client a half-written call it cannot execute.
    """

    from . import streaming as _streaming_module

    if not isinstance(request_data, dict):
        return False
    if not _routing_module._env_bool(_STREAM_CONTINUATION_ENV, False):
        return False
    if _responses_request_module._request_is_responses_api(request_data):
        return False
    if not _streaming_module._request_supports_streaming_error_fallback(request_data):
        return False
    if request_data.get("tools") or request_data.get("functions"):
        return False
    return True


def _continuation_request_payload(
    request_data: dict,
    delivered_text: str,
) -> Optional[dict]:
    """The request that asks the same route for the rest of the answer.

    The client's own ceiling is dropped rather than inherited.  A client that
    sends ``max_tokens: 4096`` by default -- Cherry Studio does, with the
    per-assistant control left off -- is not asking for a 4096-token answer; it
    is asking for an answer, and the ceiling is what cut that answer off.  A
    continuation that kept the ceiling would stop at the same place and never
    make progress.
    """
    messages = request_data.get("messages")
    if not isinstance(messages, list):
        return None
    tail = delivered_text[-_CONTINUATION_TAIL_CHARS:]
    if not tail.strip():
        return None
    payload: dict[str, Any] = {}
    for key in _CONTINUATION_REQUEST_KEYS:
        if key not in request_data:
            continue
        if key in ("messages", "client_metadata", "metadata", "stream_options"):
            continue
        payload[key] = request_data.get(key)
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        payload.pop(key, None)
    payload["messages"] = _continuation_messages(messages, tail)
    # The continuation is the router's own call, not the client's turn: it must
    # not inherit the fallback bookkeeping, or a ceiling would be recorded as a
    # route failure and quarantine a healthy deployment.
    payload["stream"] = True
    return payload


def _continuation_messages(messages: List[Any], tail: str) -> List[Any]:
    """Build a resume-shaped conversation from the client's own messages.

    The original user turn is replaced -- not repeated -- by a short quote of
    the source plus the answer so far.  Replaying the client's "translate all
    of this" instruction alongside a second copy of the answer is what made a
    route restart the translation, and it also re-sent the whole document on
    every round.
    """

    system_messages: List[Any] = []
    user_text = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            system_messages.append(_message_copy(message))
        elif role == "user" and not user_text:
            user_text = _message_text(message)
    source_quote = user_text[-_CONTINUATION_SOURCE_QUOTE_CHARS:]
    resume_turn = _CONTINUATION_PREAMBLE
    if source_quote.strip():
        resume_turn = f"{resume_turn}\n\n--- document (tail) ---\n{source_quote}"
    return [
        *system_messages,
        {"role": "user", "content": resume_turn},
        {"role": "assistant", "content": tail},
        {"role": "user", "content": _CONTINUATION_INSTRUCTION},
    ]


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def _message_copy(message: dict) -> dict:
    rebuilt: dict[str, Any] = {}
    for key, value in message.items():
        if key == "content" and isinstance(value, list):
            rebuilt[key] = [
                dict(part) if isinstance(part, dict) else part for part in value
            ]
        else:
            rebuilt[key] = value
    return rebuilt


async def _stream_native_continuation_rounds(
    request_data: dict,
    delivered_text: str,
    *,
    stream_idle_timeout_seconds: float,
) -> AsyncIterator[Any]:
    """Ask the same route for the rest of an answer it cut off at its ceiling.

    Yields only the continuation's own frames.  The caller owns the terminal
    frame: the route's first ``finish_reason`` belongs to the answer the client
    already read, so it is withheld here and re-emitted once the answer is
    actually complete.
    """

    from . import streaming as _streaming_module

    if not _request_is_continuable_native_stream(request_data):
        return
    from litellm.proxy.proxy_server import llm_router

    router_method = getattr(llm_router, "acompletion", None) if llm_router else None
    if router_method is None:
        return

    seen_text = delivered_text
    for round_number in range(1, _max_continuation_rounds() + 1):
        payload = _continuation_request_payload(request_data, seen_text)
        if payload is None:
            return
        payload["stream_idle_timeout_seconds"] = stream_idle_timeout_seconds
        _trace_module._request_route_trace(
            "native_stream_continuation_start",
            request_data,
            round=round_number,
            delivered_chars=len(seen_text),
            continuation_prompt_chars=len(str(payload.get("messages"))),
        )
        try:
            response = await router_method(**payload)
            response = await _responses_request_module._await_streaming_fallback_candidate_response(
                response,
                payload,
            )
        except Exception as exception:
            _trace_module._request_route_trace(
                "native_stream_continuation_error",
                request_data,
                round=round_number,
                exception=_routing_module._trace_exception(exception),
            )
            return

        round_text = ""
        round_ceiling_hit = False
        released = False
        final_terminal: Optional[Any] = None
        try:
            async for chunk in _streaming_module._stream_with_idle_timeout(
                response,
                payload,
            ):
                chunk_exception = _streaming_module._stream_chunk_error_exception(chunk)
                if chunk_exception is not None:
                    raise chunk_exception
                reason = _native_stream_output_limit_reason(chunk)
                if reason is not None:
                    round_ceiling_hit = True
                    continue
                if _streaming_module._native_stream_chunk_is_terminal(
                    chunk,
                    request_data,
                    set(),
                ):
                    # The route closed this continuation normally.  Its frame
                    # is the one the client must see: it states "stop", where
                    # the first route's withheld frame still says "length".
                    final_terminal = chunk
                    continue
                chunk_text = _responses_output_module._response_text(chunk)
                if chunk_text:
                    round_text += chunk_text
                released = True
                yield chunk
        except Exception as exception:
            _trace_module._request_route_trace(
                "native_stream_continuation_stream_error",
                request_data,
                round=round_number,
                exception=_routing_module._trace_exception(exception),
            )
            return
        finally:
            await _streaming_module._close_async_iterator_safely(response)

        if not released:
            if final_terminal is not None:
                # The route had nothing left to add and closed the answer:
                # that is the continuation finishing, not an empty call.  Its
                # own terminal frame is what marks the answer complete.
                _trace_module._request_route_trace(
                    "native_stream_continuation_complete",
                    request_data,
                    round=round_number,
                    delivered_chars=len(seen_text),
                    rounds=round_number,
                )
                yield final_terminal
                return
            # The route answered a continuation with nothing at all.  Asking
            # again would repeat the same silent call.
            _trace_module._request_route_trace(
                "native_stream_continuation_empty",
                request_data,
                round=round_number,
            )
            return
        seen_text = f"{seen_text}{round_text}"
        if not round_ceiling_hit:
            _trace_module._request_route_trace(
                "native_stream_continuation_done",
                request_data,
                round=round_number,
                delivered_chars=len(seen_text),
                rounds=round_number,
            )
            if final_terminal is not None:
                yield final_terminal
            return


