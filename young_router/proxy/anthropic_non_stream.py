"""Answer a non-streaming Messages turn from a streamed upstream call.

A relay's edge closes a request that produces no byte for roughly two minutes
(Cloudflare 524; New API answers its own 502 ``upstream_error`` afterwards).
A streaming turn is spared that ceiling because every event resets it, while a
whole-body turn is exactly the shape the edge kills.  The client still asked
for the whole message, so the deployment call is made with ``stream: true`` and
its frames are assembled back into the message the client asked for.

The route semantics are the streaming path's own: a frame that reports an
upstream failure raises here, after the route call returned, so the same turn
parks that route and asks a sibling.  The client never learns that the wire
under it was streamed, and it never enters its own non-streaming fallback --
the path that replayed this turn into the ceiling nine times on 2026-11-11.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from typing import Any, AsyncIterator, Optional

_BRIDGE_ATTR = "_young_router_anthropic_non_stream_bridge"
_EVENT_BOUNDARY = re.compile(r"\r\n\r\n|\n\n|\r\r")
_MESSAGE_TAIL_KEYS = ("stop_reason", "stop_sequence", "stop_details")
# The inner call is a transport detail, not a second turn: its callbacks must
# not write a second request-log row (or release the turn's slot early).  The
# assembled answer's own callbacks carry the only row the client sees.
_INNER_CALL_METADATA_KEY = "young_router_stream_bridge_inner"


def wrap_deployment_function(
    original: Any,
    *,
    method_name: Optional[str],
    client_streaming: bool,
) -> Any:
    """Wrap one deployment's generic call so a whole-body turn can be streamed.

    The wrapper sits outside the deployment failover wrapper, so an exception
    raised while assembling the frames is classified by the same handler that
    classifies an exception raised before the stream started: the route is
    parked and the next sibling answers in this turn.
    """

    if getattr(original, _BRIDGE_ATTR, False):
        return original

    async def bridged(**call_kwargs: Any) -> Any:
        if not _should_bridge(method_name, client_streaming, call_kwargs):
            return await original(**call_kwargs)
        stream = await original(**_inner_call_kwargs(call_kwargs))
        if not hasattr(stream, "__aiter__"):
            # The provider answered the whole message despite the flag (a
            # station that ignores ``stream``, a short-circuit for a tool it
            # serves itself).  There is nothing to assemble.
            return stream
        return await assemble_anthropic_message(stream)

    # Keep the deployment wrapper's own attributes (its failover marker is how
    # the router knows the function it received is already wrapped).
    functools.update_wrapper(bridged, original)
    setattr(bridged, _BRIDGE_ATTR, True)
    return bridged


def _inner_call_kwargs(call_kwargs: dict) -> dict:
    inner = {**call_kwargs, "stream": True}
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = inner.get(metadata_key)
        if isinstance(metadata, dict):
            inner[metadata_key] = {**metadata, _INNER_CALL_METADATA_KEY: True}
        else:
            inner[metadata_key] = {_INNER_CALL_METADATA_KEY: True}
    return inner


def _is_inner_bridge_call(request_kwargs: Optional[dict]) -> bool:
    """True for the deployment call a whole-body turn was bridged through.

    Only the request's own metadata is read.  LiteLLM copies the inner call's
    metadata into the shared ``litellm_params``, which is also carried by the
    client-facing attempt's logging kwargs; looking there would suppress the
    client's own success row (and its cooldown-clearing success) as if it were
    a second turn.
    """

    if not isinstance(request_kwargs, dict):
        return False
    for key in ("litellm_metadata", "metadata"):
        metadata = request_kwargs.get(key)
        if isinstance(metadata, dict) and metadata.get(_INNER_CALL_METADATA_KEY) is True:
            return True
    return False


def _should_bridge(
    method_name: Optional[str],
    client_streaming: bool,
    call_kwargs: dict,
) -> bool:
    # Only the Messages wire has frames this module knows how to assemble.
    # Chat completions and Responses have their own shapes and their own
    # streaming bridges upstream; nothing here may touch them.  The name is a
    # ``__qualname__`` when the callable is nested or a method, so compare the
    # tail, not the whole path.
    name = str(method_name or "").rsplit(".", 1)[-1]
    if name != "anthropic_messages":
        return False
    # A streaming client already receives the frames; assembling them would
    # replace its event stream with one buffered object.
    if client_streaming:
        return False
    if call_kwargs.get("stream") is True:
        return False
    surface = _selected_surface()
    if surface:
        # A route may name its own provider (``flux-code``) while its wire is
        # the Anthropic Messages surface; the surface is what decides whether
        # these frames exist at all.  Do not gate this on
        # ``use_chat_completions_api``: the surface adapter marks every
        # non-Responses wire with it (including ``anthropic``), and only the
        # Responses handler reads it.  Gating on it skipped the bridge for
        # exactly the routes the silent-connection ceiling breaks.
        return surface == "anthropic"
    # Without a recorded surface, a Chat bridge is the conservative read: it
    # speaks OpenAI chunks, not Anthropic frames.
    if call_kwargs.get("use_chat_completions_api") is True:
        return False
    return _call_provider(call_kwargs) == "anthropic"


def _selected_surface() -> str:
    """The wire the selected route declares, when the router has selected one."""

    from young_router.proxy import routing as _routing_module
    from young_router.proxy import base as _base_module

    marker = _routing_module._selected_deployment_marker_from_box()
    if not isinstance(marker, dict):
        return ""
    return _routing_module._normalized_request_surface(
        marker.get(_base_module._CURRENT_UPSTREAM_URL_SURFACE_KEY)
    )


def _call_provider(call_kwargs: dict) -> str:
    """The wire the deployment call will speak, from its own marker if needed.

    LiteLLM rebuilds the callback kwargs from the deployment, so the provider
    may be present as ``custom_llm_provider``, as the model's prefix, or only
    on the selected-deployment marker the routing patch kept for this call.
    """

    provider = str(call_kwargs.get("custom_llm_provider") or "").strip().lower()
    if provider:
        return provider
    model = str(call_kwargs.get("model") or "")
    if "/" in model:
        return model.split("/", 1)[0].strip().lower()
    from young_router.proxy import routing as _routing_module

    marker = _routing_module._selected_deployment_marker_from_box()
    params = marker.get("litellm_params") if isinstance(marker, dict) else None
    if not isinstance(params, dict):
        return ""
    provider = str(params.get("custom_llm_provider") or "").strip().lower()
    if provider:
        return provider
    model = str(params.get("model") or "")
    return model.split("/", 1)[0].strip().lower() if "/" in model else ""


async def assemble_anthropic_message(stream: AsyncIterator[Any]) -> dict:
    """Rebuild the whole ``/v1/messages`` answer from its SSE frames.

    A stream that ends before ``message_stop`` -- or whose tool input never
    parses -- is a route failure, not a shorter answer: raising here is what
    lets the caller park the route and let a sibling answer instead of handing
    the client a truncated message it would replay.
    """

    message: dict = {}
    blocks: dict[int, dict] = {}
    order: list[int] = []
    partial_json: dict[int, list[str]] = {}
    usage: dict = {}
    tail: dict = {}
    saw_stop = False

    async for event in _streamed_events(stream):
        event_type = event.get("type")
        if event_type == "message_start":
            start = event.get("message")
            if isinstance(start, dict):
                message = copy.deepcopy(start)
                if isinstance(start.get("usage"), dict):
                    usage.update(start["usage"])
                initial_content = start.get("content")
                if isinstance(initial_content, list):
                    for index, block in enumerate(initial_content):
                        if isinstance(block, dict):
                            blocks[index] = copy.deepcopy(block)
                            order.append(index)
        elif event_type == "message":
            # A provider that ignored ``stream`` and wrote the whole message
            # in one payload: there is nothing to rebuild.
            if isinstance(event.get("content"), list):
                return event
        elif event_type == "content_block_start":
            index = _event_index(event)
            block = event.get("content_block")
            if index is not None and index not in blocks and isinstance(block, dict):
                blocks[index] = copy.deepcopy(block)
                order.append(index)
                partial_json.setdefault(index, [])
        elif event_type == "content_block_delta":
            index = _event_index(event)
            delta = event.get("delta")
            block = blocks.get(index) if index is not None else None
            if isinstance(delta, dict) and isinstance(block, dict):
                _merge_delta(block, delta, partial_json.setdefault(index, []))
        elif event_type == "content_block_stop":
            index = _event_index(event)
            block = blocks.get(index) if index is not None else None
            if isinstance(block, dict):
                _finish_tool_input(block, partial_json.get(index) or [])
                partial_json.pop(index, None)
        elif event_type == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict):
                for key in _MESSAGE_TAIL_KEYS:
                    if key in delta:
                        tail[key] = delta[key]
            if isinstance(event.get("usage"), dict):
                usage.update(event["usage"])
        elif event_type == "message_stop":
            saw_stop = True
        elif event_type == "error":
            _raise_stream_error(event)
        # ``ping`` and any frame this module does not know carry no message
        # state; the event that does is the one that ends the turn.

    if not message:
        raise _incomplete("no message_start frame")
    if not saw_stop:
        raise _incomplete("the stream ended before message_stop")
    message["content"] = [blocks[index] for index in order]
    message.update(tail)
    if usage:
        message["usage"] = usage
    message.setdefault("type", "message")
    message.setdefault("role", "assistant")
    return message


async def _streamed_events(stream: AsyncIterator[Any]) -> AsyncIterator[dict]:
    """Decode the frames a transport read carries, including split reads.

    A read is whatever bytes one socket read returned: usually several whole
    frames, sometimes the first half of one.  The tail is kept until its
    boundary arrives instead of being dropped as an unparseable fragment.
    """

    buffer = ""
    async for chunk in stream:
        if isinstance(chunk, dict):
            yield chunk
            continue
        if isinstance(chunk, (bytes, bytearray)):
            try:
                text = bytes(chunk).decode("utf-8")
            except UnicodeDecodeError:
                continue
        elif isinstance(chunk, str):
            text = chunk
        else:
            dumped = _model_dump(chunk)
            if dumped is not None:
                yield dumped
            continue
        buffer += text
        while True:
            boundary = _EVENT_BOUNDARY.search(buffer)
            if boundary is None:
                break
            block = buffer[: boundary.start()]
            buffer = buffer[boundary.end() :]
            for payload in _frame_payloads(block):
                yield payload
    if buffer.strip():
        for payload in _frame_payloads(buffer):
            yield payload


def _frame_payloads(block: str) -> list[dict]:
    from young_router.proxy import streaming as _streaming_module

    return _streaming_module._sse_stream_frame_payloads(block)


def _merge_delta(block: dict, delta: dict, partial_json: list[str]) -> None:
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        block["text"] = _text(block.get("text")) + _text(delta.get("text"))
    elif delta_type == "thinking_delta":
        block["thinking"] = _text(block.get("thinking")) + _text(
            delta.get("thinking")
        )
    elif delta_type == "signature_delta":
        block["signature"] = _text(block.get("signature")) + _text(
            delta.get("signature")
        )
    elif delta_type == "input_json_delta":
        partial_json.append(_text(delta.get("partial_json")))
    elif delta_type == "citations_delta":
        citation = delta.get("citation")
        if isinstance(citation, dict):
            citations = block.get("citations")
            if not isinstance(citations, list):
                citations = []
                block["citations"] = citations
            citations.append(copy.deepcopy(citation))


def _finish_tool_input(block: dict, partial_json: list[str]) -> None:
    if not partial_json:
        return
    raw = "".join(partial_json).strip()
    if not raw:
        return
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise _incomplete("a tool call's input was not valid JSON") from None
    if isinstance(parsed, dict):
        block["input"] = parsed


def _event_index(event: dict) -> Optional[int]:
    index = event.get("index")
    if isinstance(index, bool):
        return None
    if isinstance(index, int):
        return index
    return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _model_dump(value: Any) -> Optional[dict]:
    if not hasattr(value, "model_dump"):
        return None
    try:
        dumped = value.model_dump()
    except Exception:
        return None
    return dumped if isinstance(dumped, dict) else None


def _raise_stream_error(event: dict) -> None:
    from young_router.proxy import streaming as _streaming_module

    exception = _streaming_module._stream_chunk_error_exception(event)
    if exception is None:
        exception = RuntimeError("streaming upstream error")
        exception.status_code = 502  # type: ignore[attr-defined]
    raise exception


def _incomplete(reason: str) -> Exception:
    exception = RuntimeError(f"Anthropic stream incomplete: {reason}")
    exception.stream_incomplete = True  # type: ignore[attr-defined]
    return exception
