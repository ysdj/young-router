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

Observed live 2026-11-05: a 花体 document table on `gemini-3.8-flash` stopped at
row 24 of 400 and Cherry Studio reported the turn `success`, because that route
answers a long answer with its own 8192-token ceiling whatever `max_tokens` the
request stated (measured against the endpoint directly: `finish_reason:
length`, `completion_tokens: 8188`, with no ceiling in the body and with
`max_tokens: 32000` alike).  A ceiling the *route* imposes is not a ceiling the
user can fix in Cherry Studio -- that assistant had the per-assistant limit
off -- so it is decided here, by the numbers, rather than by an operator
toggle.

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
from . import anthropic_sse as _anthropic_sse_module
from . import routing as _routing_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    List,
    Optional,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    os,
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
    "Use the source below as context for continuing the answer you already "
    "started; it is not a new request."
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


def _chat_chunk_with(chunk: Any, **fields: Any) -> Any:
    if isinstance(chunk, dict):
        from . import streaming as _streaming_module

        return _streaming_module._json_stream_event({**chunk, **fields})
    return chunk.model_copy(update=fields)


def _split_chat_finish_chunk(chunk: Any) -> tuple[Optional[Any], Any]:
    """Keep a last text delta separate from the finish reason we may withhold."""
    from . import streaming as _streaming_module

    frame = _streaming_module._stream_chunk_dump(chunk)
    choices = frame.get("choices") or []
    if len(choices) != 1:
        return None, chunk
    choice = choices[0]
    delta = choice.get("delta")
    if not isinstance(delta, dict) or not any(delta.values()):
        return None, chunk
    visible = {key: value for key, value in frame.items() if key != "usage"}
    visible["choices"] = [{**choice, "finish_reason": None}]
    terminal = {**frame, "choices": [{**choice, "delta": {}, "logprobs": None}]}
    return _streaming_module._json_stream_event(visible), _streaming_module._json_stream_event(terminal)


def _sum_chat_token_usage(total: dict, usage: dict) -> None:
    """Combine the final counts from distinct upstream calls, including details."""
    for key, value in usage.items():
        if type(value) is int and key.endswith("tokens"):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, dict) and key.endswith("_tokens_details"):
            _sum_chat_token_usage(total.setdefault(key, {}), value)


# The request keys a continuation call carries.  A native request holds the
# router's own live dispatch objects (the original upstream callable, route
# bookkeeping), and a deep copy of those is not only wasteful but can fail
# outright, so the continuation is built from the chat payload's own fields.
_CONTINUATION_REQUEST_KEYS = (
    "temperature",
    "top_p",
    "reasoning_effort",
    "user",
    "service_tier",
    "seed",
    "stop",
    "response_format",
    "stream",
    "stream_timeout",
    "api_base",
    "api_key",
    "api_version",
    "custom_llm_provider",
    "extra_body",
    "extra_headers",
    "prompt_cache_key",
)


# A route that stops a whole ceiling's worth of tokens short of what the client
# asked for is reporting a limit of its own.  The gap has to be wide enough to
# tell that apart from a model that simply finished near the ceiling it was
# given, so the comparison allows one chunk of slack on either side.
_ROUTE_CEILING_SHORTFALL_TOKENS = 256


def _request_is_continuable_native_stream(request_data: Optional[dict]) -> bool:
    """Whether this request can be rebuilt as a single text continuation."""

    from . import streaming as _streaming_module

    if not isinstance(request_data, dict):
        return False
    if not _streaming_module._request_uses_native_event_stream(request_data):
        return False
    is_chat = not _streaming_module._request_is_anthropic_messages_stream(request_data)
    tools = request_data.get("tools")
    if request_data.get("functions"):
        return False
    if tools and (
        not is_chat
        or not isinstance(tools, list)
        or any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools)
    ):
        return False
    # Chat clients may attach optional function declarations to every request.
    # The stream checks whether the model actually started a call. A request
    # that requires one can't be resumed with the declarations removed.
    if is_chat and (
        request_data.get("tool_choice") not in (None, "auto", "none")
        or request_data.get("n") not in (None, 1)
        or request_data.get("response_format") not in (None, {"type": "text"})
        or request_data.get("modalities") not in (None, ["text"])
    ):
        return False
    messages = request_data.get("messages")
    if not isinstance(messages, list):
        return False

    user_message_count = 0
    for message in messages:
        if not isinstance(message, dict):
            return False
        role = message.get("role")
        if role == "user":
            user_message_count += 1
        elif role != "system":
            # The resume prompt intentionally rebuilds a single plain-text
            # request. Earlier assistant turns, tool results, and other roles
            # carry context it would otherwise discard.
            return False

        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or any(
            not isinstance(part, dict)
            or not (
                (part.get("type") == "text" and isinstance(part.get("text"), str))
                or (is_chat and role == "user" and part.get("type") == "image_url")
            )
            for part in content
        ):
            return False

    return user_message_count == 1


def _stopped_at_the_routes_own_ceiling(
    request_data: Optional[dict],
    output_tokens: Optional[int],
) -> bool:
    """True when the route's ceiling, not the client's, ended the answer.

    A route that resells a smaller model reports its own cap and ignores the
    budget in the request.  Measured 2026-11-05 on `gemini-3.8-flash` through a
    relay: `max_tokens: 32000` in the body, `completion_tokens: 31996` only when
    the relay honoured it, and 8188 -- its own ceiling -- on the route that
    ignores it.  The client asked for far more, so the stop is the route's
    truncation and this router is the only place that can continue it.

    If the client gave a budget, a missing usage count cannot show that the
    route stopped short of it. With no budget, the explicit length terminal is
    enough: the client did not ask for a limit.
    """

    from . import streaming as _streaming_module

    budget = _streaming_module._request_output_token_budget(request_data)
    if budget is None:
        return True
    return type(output_tokens) is int and 0 < output_tokens < budget - _ROUTE_CEILING_SHORTFALL_TOKENS


def _continuation_is_decided_by_the_engine(
    request_data: Optional[dict],
    ceiling_chunk: Any = None,
    *,
    answered_output_tokens: Optional[int] = None,
) -> bool:
    """Whether to continue, deciding a native stream the toggle leaves alone.

    ``YOUNG_ROUTER_STREAM_CONTINUATION_ENABLED`` still decides whether a client
    *with* a ceiling of its own is overrun.  A request that states no ceiling,
    or that was answered short of the one it stated, is decided by the numbers
    alone -- see :func:`_stopped_at_the_routes_own_ceiling`.

    The output count arrives either way: ``answered_output_tokens`` when the
    caller already collected the answer's accounting frames, else read from
    ``ceiling_chunk``.  A route states its usage in a frame of its own -- a
    trailing chunk beside the ``finish_reason`` one when ``stream_options``
    asks for it -- so the ceiling frame alone often carries no number, and a
    decision that could only read that frame would refuse every such route.
    """

    from . import streaming as _streaming_module

    if not _request_is_continuable_native_stream(request_data):
        return False
    if _routing_module._env_bool(_STREAM_CONTINUATION_ENV, False):
        return True
    output_tokens = answered_output_tokens
    if output_tokens is None:
        output_tokens = _streaming_module._responses_incomplete_output_tokens(
            ceiling_chunk
        )
    return _stopped_at_the_routes_own_ceiling(request_data, output_tokens)


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
    from . import streaming as _streaming_module

    messages = request_data.get("messages")
    if not isinstance(messages, list):
        return None
    tail = delivered_text[-_CONTINUATION_TAIL_CHARS:]
    if not tail.strip():
        return None
    payload = {key: request_data[key] for key in _CONTINUATION_REQUEST_KEYS if key in request_data}
    payload["model"] = _routing_module._request_public_model(request_data)
    payload["messages"] = _continuation_messages(messages, tail)
    if _streaming_module._request_is_anthropic_messages_stream(request_data):
        max_tokens = request_data.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            return None
        payload["call_type"] = "anthropic_messages"
        payload["max_tokens"] = max_tokens
        payload["messages"] = [
            message for message in payload["messages"] if message.get("role") != "system"
        ]
        for key in (
            "system",
            "stop_sequences",
            "thinking",
            "top_k",
            "container",
            "metadata",
        ):
            if key in request_data:
                payload[key] = request_data[key]
    else:
        # A continuation follows the deployment that actually answered, which
        # may be the last healthy sibling after several failed dispatches.
        if deployment_id := _routing_module._deployment_id_from_request(request_data):
            payload[_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY] = [deployment_id]
        stream_options = request_data.get("stream_options")
        if isinstance(stream_options, dict) and stream_options.get("include_usage") is True:
            payload["stream_options"] = {"include_usage": True}
    # The continuation is the router's own call, not the client's turn: it must
    # not inherit the fallback bookkeeping, or a ceiling would be recorded as a
    # route failure and quarantine a healthy deployment.
    payload["stream"] = True
    return payload


def _continuation_messages(messages: List[Any], tail: str) -> List[Any]:
    """Give the continuation the source and the text already delivered."""

    system_messages: List[Any] = []
    resume_turn: Any = _CONTINUATION_PREAMBLE
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            system_messages.append(_message_copy(message))
        elif role == "user":
            content = message.get("content")
            if isinstance(content, list) and any(part.get("type") == "image_url" for part in content):
                # For an image summary the unsummarized facts are in the
                # images. A text tail can't stand in for them. Keep the entire
                # source in order, with the partial answer as the next turn.
                resume_turn = [{"type": "text", "text": _CONTINUATION_PREAMBLE}, *content]
            else:
                source_quote = _message_text(message)[-_CONTINUATION_SOURCE_QUOTE_CHARS:]
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
    ceiling_chunk: Any = None,
    answered_output_tokens: Optional[int] = None,
    anthropic_index_offset: int = 0,
    chat_usage: Optional[dict] = None,
    continuation_error: Optional[dict[str, Exception]] = None,
) -> AsyncIterator[Any]:
    """Ask the same route for the rest of an answer it cut off at its ceiling.

    Yields only the continuation's own frames.  The caller owns the terminal
    frame: the route's first ``finish_reason`` belongs to the answer the client
    already read, so it is withheld here and re-emitted once the answer is
    actually complete.

    ``answered_output_tokens`` tells a route ceiling from a ceiling the client
    asked for when the request included a budget.
    """

    from . import streaming as _streaming_module

    if not _continuation_is_decided_by_the_engine(
        request_data,
        ceiling_chunk,
        answered_output_tokens=answered_output_tokens,
    ):
        return
    from litellm.proxy.proxy_server import llm_router

    is_anthropic = _streaming_module._request_is_anthropic_messages_stream(request_data)
    method_name = "anthropic_messages" if is_anthropic else "acompletion"
    router_method = getattr(llm_router, method_name, None) if llm_router else None
    if router_method is None:
        return

    seen_text = delivered_text
    chat_identity = {} if is_anthropic else {
        k: v for k, v in _streaming_module._stream_chunk_dump(ceiling_chunk).items()
        if k in ("id", "created", "model") and v is not None
    }
    next_anthropic_index = anthropic_index_offset
    # Anthropic reports ``usage.output_tokens`` for each Messages request.  A
    # continuation is a second request that belongs to the same client answer,
    # so the terminal usage we publish must include the initial route and every
    # continuation round.
    cumulative_anthropic_output_tokens = (
        answered_output_tokens
        if isinstance(answered_output_tokens, int)
        and not isinstance(answered_output_tokens, bool)
        and answered_output_tokens >= 0
        else None
    )

    async def consume_attempt(response: Any, payload: dict, state: dict) -> AsyncIterator[Any]:
        nonlocal next_anthropic_index

        round_text_parts: List[str] = []
        round_chat_usage = None
        anthropic_indices: dict[int, int] = {}
        open_anthropic_indices: set[int] = set()
        stream = (
            response
            if state.get("route_recovery_poll")
            else _streaming_module._stream_with_idle_timeout(response, payload)
        )
        chunks = _anthropic_sse_module.iter_messages_sse_frames(stream) if is_anthropic else stream
        try:
            async for chunk in chunks:
                if _streaming_module._is_route_recovery_sse_keepalive(chunk):
                    yield chunk
                    continue
                state["chunk_count"] += 1
                chunk_exception = _streaming_module._stream_chunk_error_exception(chunk)
                if chunk_exception is not None:
                    raise chunk_exception
                if is_anthropic:
                    for event in _streaming_module._stream_chunk_dumps(chunk):
                        event_type = event.get("type")
                        if event_type == "message_delta":
                            usage = event.get("usage")
                            if isinstance(usage, dict):
                                output_tokens = usage.get("output_tokens")
                                if (
                                    isinstance(output_tokens, int)
                                    and not isinstance(output_tokens, bool)
                                    and output_tokens >= 0
                                ):
                                    state["anthropic_output_tokens"] = output_tokens
                            if _native_stream_output_limit_reason(event) is not None:
                                state["round_ceiling_hit"] = True
                            else:
                                state["final_message_delta"] = event
                            continue
                        if event_type == "message_stop":
                            if not state["round_ceiling_hit"]:
                                state["final_terminal"] = event
                            continue
                        if event_type not in {
                            "content_block_start", "content_block_delta", "content_block_stop"
                        }:
                            continue
                        index = event.get("index")
                        if not isinstance(index, int):
                            continue
                        if event_type == "content_block_start":
                            anthropic_indices[index] = next_anthropic_index
                            next_anthropic_index += 1
                            open_anthropic_indices.add(anthropic_indices[index])
                        if index not in anthropic_indices:
                            continue
                        event = dict(event)
                        event["index"] = anthropic_indices[index]
                        if event_type == "content_block_stop":
                            open_anthropic_indices.discard(event["index"])
                        delta = event.get("delta")
                        if isinstance(delta, dict) and delta.get("type") == "text_delta":
                            round_text_parts.append(str(delta.get("text") or ""))
                        state["released"] = True
                        yield _anthropic_sse_module._sse_block(event)
                    continue

                frame = _streaming_module._stream_chunk_dump(chunk)
                if _streaming_module._response_has_tool_call_activity(frame):
                    # The continuation request declares no tools. Never splice
                    # an unsolicited call into the existing text answer.
                    state["unsupported_tool_activity"] = True
                    return
                updates = dict(chat_identity)
                if isinstance(frame.get("usage"), dict):
                    # The last usage on this call is cumulative, not a delta.
                    round_chat_usage = frame["usage"]
                    if not frame.get("choices"):
                        continue
                    updates["usage"] = None
                if updates:
                    chunk = _chat_chunk_with(chunk, **updates)
                reason = _native_stream_output_limit_reason(chunk)
                if reason is not None:
                    state["round_ceiling_hit"] = True
                if reason is not None or _streaming_module._native_stream_chunk_is_terminal(
                    chunk,
                    request_data,
                    set(),
                ):
                    visible, terminal = _split_chat_finish_chunk(chunk)
                    if visible is not None:
                        round_text_parts.append(
                            _responses_output_module._response_text(visible)
                        )
                        state["released"] = True
                        yield visible
                    if not state["round_ceiling_hit"]:
                        state["final_terminal"] = terminal
                    continue
                chunk_text = _responses_output_module._response_text(chunk)
                if chunk_text:
                    round_text_parts.append(chunk_text)
                state["released"] = True
                yield chunk
        except Exception as exception:
            state["exception"] = exception
            _trace_module._request_route_trace(
                "native_stream_continuation_stream_error",
                request_data,
                round=state["round_number"],
                exception=_routing_module._trace_exception(exception),
            )
        finally:
            if chunks is not stream:
                await chunks.aclose()
            await stream.aclose()
            await _streaming_module._close_async_iterator_safely(response)
            if chat_usage is not None and round_chat_usage is not None:
                _sum_chat_token_usage(chat_usage, round_chat_usage)

        state["round_text"] = "".join(round_text_parts)
        for index in sorted(open_anthropic_indices):
            yield _anthropic_sse_module._sse_block(
                {"type": "content_block_stop", "index": index}
            )

    def remember_error(exception: Exception) -> None:
        if continuation_error is not None and "exception" not in continuation_error:
            # This round exists only because the route ended its original
            # answer at an output ceiling. If the resume cannot finish, the
            # client's partial text is still an incomplete answer, regardless
            # of whether the resume failed before dispatch or mid-stream.
            exception.stream_incomplete = True
            continuation_error["exception"] = exception

    async def fallback_or_recover(
        payload: dict,
        exception: Exception,
        *,
        round_number: int,
    ) -> tuple[Any, dict, bool]:
        fallback_exception = exception
        try:
            fallback = await _streaming_module._streaming_error_fallback_response(
                payload,
                exception,
                allow_repeated_attempt=True,
            )
        except Exception as exc:
            fallback = None
            fallback_exception = exc

        if fallback is not None:
            response, fallback_payload = fallback
            _trace_module._request_route_trace(
                "native_stream_continuation_failover",
                request_data,
                round=round_number,
                exception=_routing_module._trace_exception(exception),
            )
            return response, fallback_payload, False

        if _routing_module._should_return_route_recovery_stream(fallback_exception, payload):
            _trace_module._request_route_trace(
                "native_stream_continuation_recovery_poll_start",
                request_data,
                round=round_number,
                exception=_routing_module._trace_exception(fallback_exception),
            )
            return (
                _streaming_module._stream_route_recovery_poll(
                    payload,
                    fallback_exception,
                ),
                payload,
                True,
            )

        _trace_module._request_route_trace(
            "native_stream_continuation_error",
            request_data,
            round=round_number,
            exception=_routing_module._trace_exception(fallback_exception),
        )
        remember_error(fallback_exception)
        return None, payload, False

    routing_request = request_data
    max_rounds = _max_continuation_rounds()
    for round_number in range(1, max_rounds + 1):
        payload = _continuation_request_payload(request_data, seen_text)
        if payload is None:
            return
        active_deployment_id = _routing_module._deployment_id_from_request(
            routing_request
        )
        if active_deployment_id:
            payload[_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY] = [active_deployment_id]
        excluded_ids = _responses_request_module._request_excluded_deployment_ids(
            routing_request
        )
        if excluded_ids:
            payload["_excluded_deployment_ids"] = sorted(excluded_ids)
        payload["stream_idle_timeout_seconds"] = stream_idle_timeout_seconds
        _trace_module._request_route_trace(
            "native_stream_continuation_start",
            request_data,
            round=round_number,
            delivered_chars=len(seen_text),
        )
        try:
            # A continuation round is a dispatch like any other: the route it
            # picks can be absent, and that phase has no clock of its own.
            response = await _streaming_module._await_upstream_dispatch(
                router_method(**_streaming_module._streaming_error_fallback_dispatch_kwargs(payload)),
                payload,
                fallback_budget_seconds=_routing_module._stream_start_timeout_seconds_for_request(
                    payload
                ),
            )
            response = await _responses_request_module._await_streaming_fallback_candidate_response(
                response,
                payload,
            )
            _routing_module._apply_current_selected_deployment_to_request(payload)
        except Exception as exception:
            # The first continuation is pinned to the deployment that produced
            # the ceiling so the answer does not restart on a random sibling.
            # That route can still go silent on the *new* request, though.  In
            # that case handing the original ``length`` frame to the client
            # makes a recoverable continuation look like a completed answer.
            # Reuse the normal streaming-fallback builder with this compact
            # continuation payload: it excludes the failed deployment and
            # selects the next healthy sibling while preserving the resume
            # prompt and the same client-facing answer.
            verified_ids = _responses_request_module._request_verified_fallback_deployment_ids(
                payload
            )
            if verified_ids:
                excluded_ids = _responses_request_module._request_excluded_deployment_ids(
                    payload
                )
                excluded_ids.update(verified_ids)
                payload["_excluded_deployment_ids"] = sorted(excluded_ids)
            response, payload, route_recovery_poll = await fallback_or_recover(
                payload,
                exception,
                round_number=round_number,
            )
            if response is None:
                return
            routing_request = payload
        else:
            route_recovery_poll = False

        round_text = ""
        released = False
        attempt_state = {}
        while True:
            attempt_state = {
                "round_number": round_number,
                "round_text": "",
                "round_ceiling_hit": False,
                "released": False,
                "final_terminal": None,
                "final_message_delta": None,
                "anthropic_output_tokens": None,
                "chunk_count": 0,
                "exception": None,
                "unsupported_tool_activity": False,
                "route_recovery_poll": route_recovery_poll,
            }
            async for chunk in consume_attempt(response, payload, attempt_state):
                yield chunk
            round_text += attempt_state["round_text"]
            released = released or attempt_state["released"]
            round_ceiling_hit = attempt_state["round_ceiling_hit"]
            final_terminal = attempt_state["final_terminal"]
            final_message_delta = attempt_state["final_message_delta"]
            if attempt_state["unsupported_tool_activity"]:
                remember_error(RuntimeError("The continuation emitted an unexpected tool call."))
                return
            if final_terminal is not None or round_ceiling_hit:
                _routing_module._apply_current_selected_deployment_to_request(payload)
                routing_request = payload
                break

            exception = attempt_state["exception"]
            if exception is None:
                exception = _streaming_module._native_stream_incomplete_exception(
                    payload,
                    buffered_chunks=attempt_state["chunk_count"],
                )
            if route_recovery_poll:
                _trace_module._request_route_trace(
                    "native_stream_continuation_recovery_poll_error",
                    request_data,
                    round=round_number,
                    exception=_routing_module._trace_exception(exception),
                )
                remember_error(exception)
                return
            _routing_module._mark_same_deployment_retry_exhausted(exception)
            _routing_module._apply_current_selected_deployment_to_request(payload)
            resume_payload = _continuation_request_payload(
                request_data,
                f"{seen_text}{round_text}",
            )
            if resume_payload is None:
                remember_error(exception)
                return
            fallback_source = payload.copy()
            fallback_source["messages"] = resume_payload["messages"]
            fallback_source["stream"] = True
            fallback_source["stream_idle_timeout_seconds"] = stream_idle_timeout_seconds
            response, payload, route_recovery_poll = await fallback_or_recover(
                fallback_source,
                exception,
                round_number=round_number,
            )
            if response is None:
                return
            routing_request = payload

        if attempt_state["anthropic_output_tokens"] is not None:
            if cumulative_anthropic_output_tokens is None:
                cumulative_anthropic_output_tokens = attempt_state[
                    "anthropic_output_tokens"
                ]
            else:
                cumulative_anthropic_output_tokens += attempt_state[
                    "anthropic_output_tokens"
                ]

        if is_anthropic and final_terminal is not None:
            if final_message_delta is not None:
                if cumulative_anthropic_output_tokens is not None:
                    final_message_delta = dict(final_message_delta)
                    usage = final_message_delta.get("usage")
                    updated_usage = dict(usage) if isinstance(usage, dict) else {}
                    updated_usage["output_tokens"] = cumulative_anthropic_output_tokens
                    final_message_delta["usage"] = updated_usage
                yield _anthropic_sse_module._sse_block(final_message_delta)
            final_terminal = _anthropic_sse_module._sse_block(final_terminal)

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
            remember_error(RuntimeError("The continuation reached the output limit without adding text."))
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

    if max_rounds > 0:
        remember_error(RuntimeError("Every continuation round reached the output limit."))


