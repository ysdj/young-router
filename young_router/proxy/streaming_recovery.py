"""Route recovery, failover, and keepalive for the Responses stream.

The stream can outlive its first upstream attempt: a route falls over, the
recovery poll waits for a cooldown, an upstream stalls after a partial answer,
and the client keeps its connection open through keepalive frames.  That whole
conversation lives here; :mod:`young_router.proxy.streaming` owns the delivery of
the chunks themselves and calls back into these helpers.

Importing either module first is safe: the helpers below resolve
:mod:`young_router.proxy.streaming` lazily through :class:`_StreamingModuleProxy`.
"""

from __future__ import annotations


from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import state as _state_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    List,
    Optional,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_SELECTED_DEPLOYMENT_BOX,
    _JSONStreamEvent,
    _ROUTE_RECOVERY_POLL_METADATA_KEY,
    _RouteOrder,
    asyncio,
    time,
)


# The delivery helpers in young_router.proxy.streaming that these recovery paths call
# back into.  Resolved lazily so importing either module first is safe.
class _StreamingModuleProxy:
    def __getattr__(self, name: str) -> Any:
        from . import streaming

        return getattr(streaming, name)


_streaming_module = _StreamingModuleProxy()


def _reset_route_exhaustion_retry_state(
    request_data: dict,
    exception: Exception,
    *,
    preserve_failed_deployment: bool = False,
    preserve_existing_exclusions: bool = False,
    preserve_target_order: bool = False,
) -> None:
    target_order = _responses_request_module._request_target_order(request_data)
    if target_order is None:
        target_order = _responses_execution_module._failed_deployment_order(exception)
    failed_id = (
        _responses_execution_module._failed_deployment_id(exception)
        or _routing_module._deployment_id_from_request(request_data)
    )
    existing_excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_data)
    if preserve_failed_deployment and failed_id:
        excluded_ids = existing_excluded_ids
        excluded_ids.add(failed_id)
        request_data["_excluded_deployment_ids"] = sorted(excluded_ids)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids)
        try:
            exception.excluded_deployment_ids = sorted(excluded_ids)  # type: ignore[attr-defined]
        except Exception:
            pass
    elif preserve_existing_exclusions and existing_excluded_ids:
        request_data["_excluded_deployment_ids"] = sorted(existing_excluded_ids)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(existing_excluded_ids)
    else:
        request_data.pop("_excluded_deployment_ids", None)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(set())
        try:
            delattr(exception, "excluded_deployment_ids")
        except AttributeError:
            pass
        except Exception:
            pass
        if _routing_module._is_no_deployments_available_error(exception):
            try:
                delattr(exception, "failed_deployment_order")
            except AttributeError:
                pass
            except Exception:
                pass
    if preserve_target_order and target_order is not None:
        request_data["_target_order"] = target_order
    else:
        request_data.pop("_target_order", None)

def _configured_deployment_orders(router: Any, request_data: dict) -> list[_RouteOrder]:
    model_group = _responses_execution_module._request_model_group(request_data)
    if not isinstance(model_group, str) or not model_group.strip():
        return []
    try:
        metadata = _request_context_module._request_metadata_dict(request_data, "metadata") or {}
        team_id = metadata.get("user_api_key_team_id")
        deployments = _routing_module._router_configured_deployments(
            router,
            model_group,
            team_id=team_id,
        )
    except Exception:
        return []
    orders = {
        order
        for order in (
            _responses_request_module._deployment_order(deployment)
            for deployment in list(deployments or [])
        )
        if order is not None
    }
    return sorted(orders)

def _configured_deployment_ids(router: Any, request_data: dict) -> set[str]:
    model_group = _responses_execution_module._request_model_group(request_data)
    if not isinstance(model_group, str) or not model_group.strip():
        return set()
    try:
        metadata = _request_context_module._request_metadata_dict(
            request_data,
            "metadata",
        ) or {}
        deployments = _routing_module._router_configured_deployments(
            router,
            model_group,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return set()
    return {
        deployment_id
        for deployment_id in (
            _responses_request_module._deployment_id(deployment)
            for deployment in list(deployments or [])
        )
        if deployment_id
    }

def _next_configured_order(
    orders: list[_RouteOrder],
    previous_order: Optional[_RouteOrder],
) -> Optional[_RouteOrder]:
    if not orders:
        return None
    if previous_order is None:
        return orders[0]
    for order in orders:
        if order > previous_order:
            return order
    return orders[0]

def _route_recovery_exhausted_order(
    request_data: dict,
    exception: Exception,
) -> Optional[_RouteOrder]:
    order = _responses_execution_module._failed_deployment_order(exception)
    if order is None:
        order = _responses_request_module._request_target_order(request_data)
    if order is None:
        order = _routing_module._deployment_order_from_request(request_data)
    return order

def _route_recovery_next_poll_order(
    router: Any,
    request_data: dict,
    exception: Exception,
) -> Optional[_RouteOrder]:
    orders = _streaming_module._configured_deployment_orders(router, request_data)
    return _streaming_module._next_configured_order(orders, _streaming_module._route_recovery_exhausted_order(request_data, exception))

async def _stream_streaming_error_fallback(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    # Streaming follows the same explicit same-route budget regardless of
    # client protocol. A failed fallback stream that has not delivered
    # visible output or a terminal event is a completed route attempt, not a
    # same-route retry; keep advancing while each failure adds a new excluded
    # deployment.  The configured deployment pool therefore bounds the loop.
    max_retries = _routing_module._same_deployment_retries()
    delay_seconds = _routing_module._stream_route_exhaustion_retry_delay_seconds()
    attempt = 0
    allow_repeated_attempt = False
    is_responses_stream = _streaming_module._request_is_responses_stream(request_data)
    is_structured_compaction = (
        _responses_request_module._request_has_structured_codex_compaction(
            request_data
        )
    )
    while True:
        buffered_chunks: List[Any] = []
        started_delivery = False
        excluded_before = (
            _responses_request_module._request_excluded_deployment_ids(
                request_data
            )
        )
        try:
            async for chunk in _streaming_module._stream_streaming_error_fallback_round(
                request_data,
                exception,
                allow_repeated_attempt=allow_repeated_attempt,
            ):
                if is_responses_stream and not started_delivery:
                    deliverable = bool(
                        _streaming_module._stream_chunk_has_visible_output(chunk)
                        or _streaming_module._responses_completed_chunk_has_usable_output(
                            chunk,
                            request_data,
                        )
                        or _streaming_module._responses_stream_chunk_is_incomplete_terminal(chunk)
                    )
                    if not deliverable:
                        buffered_chunks.append(chunk)
                        continue
                    started_delivery = True
                    for buffered_chunk in buffered_chunks:
                        yield buffered_chunk
                    buffered_chunks.clear()
                else:
                    started_delivery = True
                yield chunk
            return
        except Exception as exc:
            buffered_chunks.clear()
            if started_delivery:
                _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._sync_failed_deployment_exclusions(request_data, exc)
                raise
            if (
                not _routing_module._is_no_deployments_available_error(exc)
                and not _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    request_data,
                )
            ):
                raise
            structured_compaction_wait_failed = (
                is_structured_compaction
                and _routing_module._is_local_stream_timeout_error(exc)
            )
            if structured_compaction_wait_failed:
                # A structured compaction replays the complete signed history.
                # Once its replacement route has consumed a timed-out stream
                # attempt, another ordered replay only adds another full
                # stream wait and cannot make that failed attempt valid.
                # Keep the existing timeout values; bound only the route hop.
                _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._sync_failed_deployment_exclusions(
                    request_data,
                    exc,
                )
                _trace_module._request_route_trace(
                    "codex_compaction_streaming_fallback_stopped", request_data,
                    reason="stream_wait_timeout",
                    exception=_routing_module._trace_exception(exc),
                )
                raise
            excluded_after = (
                _responses_request_module._request_excluded_deployment_ids(
                    request_data
                )
            )
            newly_excluded = excluded_after - excluded_before
            from litellm.proxy.proxy_server import llm_router

            configured_deployment_ids = (
                _streaming_module._configured_deployment_ids(llm_router, request_data)
                if llm_router is not None
                else set()
            )
            remaining_deployment_ids = (
                configured_deployment_ids - excluded_after
            )
            route_attempt_consumed = bool(
                newly_excluded & configured_deployment_ids
            )
            if route_attempt_consumed:
                if not remaining_deployment_ids:
                    raise
                _trace_module._request_route_trace(
                    "streaming_error_fallback_route_hop", request_data,
                    excluded_deployment_ids=sorted(excluded_after),
                    newly_excluded_deployment_ids=sorted(newly_excluded),
                    remaining_deployment_ids=sorted(remaining_deployment_ids),
                    exception=_routing_module._trace_exception(exc),
                )
                exception = exc
                attempt = 0
                allow_repeated_attempt = True
                continue

            if attempt < max_retries:
                attempt += 1
                retry_delay_seconds = _routing_module._route_exhaustion_retry_delay_for_exception(
                    exc,
                    delay_seconds,
                )
                _trace_module._request_route_trace(
                    "route_exhaustion_retry", request_data,
                    retry_attempt=attempt,
                    max_retries=max_retries,
                    retry_delay_seconds=retry_delay_seconds,
                    configured_retry_delay_seconds=delay_seconds,
                    exception=_routing_module._trace_exception(exc),
                )
                _streaming_module._reset_route_exhaustion_retry_state(
                    request_data,
                    exc,
                    preserve_failed_deployment=False,
                    preserve_existing_exclusions=False,
                )
                exception = exc
                allow_repeated_attempt = True
                if retry_delay_seconds > 0:
                    await asyncio.sleep(retry_delay_seconds)
                continue

            _routing_module._mark_same_deployment_retry_exhausted(exc)
            _routing_module._sync_failed_deployment_exclusions(request_data, exc)
            excluded_after = (
                _responses_request_module._request_excluded_deployment_ids(
                    request_data
                )
            )
            newly_excluded = excluded_after - excluded_before
            remaining_deployment_ids = configured_deployment_ids - excluded_after
            if not (
                newly_excluded & configured_deployment_ids
                and remaining_deployment_ids
            ):
                raise
            _trace_module._request_route_trace(
                "streaming_error_fallback_route_hop", request_data,
                excluded_deployment_ids=sorted(excluded_after),
                newly_excluded_deployment_ids=sorted(newly_excluded),
                remaining_deployment_ids=sorted(remaining_deployment_ids),
                exception=_routing_module._trace_exception(exc),
            )
            exception = exc
            attempt = 0
            allow_repeated_attempt = True

async def _stream_route_recovery_poll_attempt(
    request_data: dict,
    exception: Exception,
    *,
    attempt: int,
    deadline: Optional[float] = None,
) -> AsyncIterator[Any]:
    selected_deployment_box: dict[str, Any] = {}
    fallback_iterator = _streaming_module._stream_streaming_error_fallback_round(
        request_data,
        exception,
        allow_repeated_attempt=True,
        route_recovery_poll=True,
    ).__aiter__()
    buffered_chunks: List[Any] = []
    completion_state = _streaming_module._ResponsesStreamCompletionState(request_data)
    started_delivery = False
    next_chunk_task = None
    configured_timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(request_data)
    request_timeout_seconds = configured_timeout_seconds
    if deadline is not None and attempt > 1:
        remaining_poll_seconds = max(0.0, deadline - time.monotonic())
        if remaining_poll_seconds <= 0:
            raise _streaming_module._stream_start_timeout_exception(
                request_data,
                start_seconds=configured_timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            )
        if request_timeout_seconds > 0:
            request_timeout_seconds = min(request_timeout_seconds, remaining_poll_seconds)
        else:
            request_timeout_seconds = remaining_poll_seconds
    attempt_deadline = (
        time.monotonic() + request_timeout_seconds
        if request_timeout_seconds > 0
        else None
    )

    async def _cancel_next_chunk_task() -> None:
        nonlocal next_chunk_task
        task = next_chunk_task
        next_chunk_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception:
            pass

    try:
        while True:
            if next_chunk_task is None:
                selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
                    selected_deployment_box
                )
                try:
                    next_chunk_task = asyncio.create_task(fallback_iterator.__anext__())
                finally:
                    _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(selected_deployment_box_token)
            try:
                wait_seconds = _streaming_module._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
                if not started_delivery and attempt_deadline is not None:
                    remaining_seconds = attempt_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        await _cancel_next_chunk_task()
                        completed_compat = (
                            _streaming_module._codex_compaction_done_item_completed_compat(
                                completion_state,
                                request_data,
                                reason="route_recovery_attempt_deadline",
                            )
                        )
                        if completed_compat is not None:
                            for buffered_chunk in buffered_chunks:
                                yield _streaming_module._responses_stream_chunk_for_delivery(
                                    buffered_chunk
                                )
                            yield completed_compat
                            return
                        raise _streaming_module._stream_route_recovery_wait_timeout_exception(
                            request_data,
                            buffered_chunks=len(buffered_chunks),
                            timeout_seconds=configured_timeout_seconds,
                            selected_deployment_box=selected_deployment_box,
                        ) from None
                    wait_seconds = min(wait_seconds, remaining_seconds)
                chunk = await asyncio.wait_for(
                    asyncio.shield(next_chunk_task),
                    timeout=wait_seconds,
                )
                next_chunk_task = None
            except StopAsyncIteration:
                next_chunk_task = None
                completed_compat = _streaming_module._codex_compaction_done_item_completed_compat(
                    completion_state,
                    request_data,
                    reason="route_recovery_clean_eof",
                )
                if completed_compat is not None:
                    for buffered_chunk in buffered_chunks:
                        yield _streaming_module._responses_stream_chunk_for_delivery(buffered_chunk)
                    yield completed_compat
                return
            except asyncio.TimeoutError:
                if next_chunk_task is not None and next_chunk_task.done():
                    completed_task = next_chunk_task
                    next_chunk_task = None
                    # The task may complete at the exact keepalive boundary.
                    # Consume its chunk below rather than dropping it and
                    # starting a second recovery attempt.
                    try:
                        chunk = completed_task.result()
                    except StopAsyncIteration:
                        completed_compat = _streaming_module._codex_compaction_done_item_completed_compat(
                            completion_state,
                            request_data,
                            reason="route_recovery_clean_eof",
                        )
                        if completed_compat is not None:
                            for buffered_chunk in buffered_chunks:
                                yield _streaming_module._responses_stream_chunk_for_delivery(
                                    buffered_chunk
                                )
                            yield completed_compat
                        return
                elif not started_delivery:
                    if (
                        attempt_deadline is not None
                        and (attempt_deadline - time.monotonic()) <= 0
                    ):
                        await _cancel_next_chunk_task()
                        completed_compat = (
                            _streaming_module._codex_compaction_done_item_completed_compat(
                                completion_state,
                                request_data,
                                reason="route_recovery_attempt_timeout",
                            )
                        )
                        if completed_compat is not None:
                            for buffered_chunk in buffered_chunks:
                                yield _streaming_module._responses_stream_chunk_for_delivery(
                                    buffered_chunk
                                )
                            yield completed_compat
                            return
                        raise _streaming_module._stream_route_recovery_wait_timeout_exception(
                            request_data,
                            buffered_chunks=len(buffered_chunks),
                            timeout_seconds=configured_timeout_seconds,
                            selected_deployment_box=selected_deployment_box,
                        ) from None
                    yield _streaming_module._route_recovery_sse_keepalive(
                        attempt,
                        request_data=request_data,
                        phase="attempt",
                    )
                    continue
                else:
                    raise

            if started_delivery:
                yield _streaming_module._responses_stream_chunk_for_delivery(chunk)
                if _streaming_module._responses_stream_chunk_is_completed(chunk):
                    return
                continue

            buffered_chunks.append(chunk)
            completion_state.remember(chunk)
            terminal_exception = _streaming_module._route_recovery_terminal_chunk_exception(
                chunk,
                request_data,
                selected_deployment_box,
                buffer=buffered_chunks,
            )
            if terminal_exception is not None:
                raise terminal_exception
            if not _streaming_module._stream_chunk_has_deliverable_route_recovery_output(
                chunk,
                request_data,
            ):
                continue

            completed_compat_chunk = _streaming_module._responses_output_limit_incomplete_as_completed_chunk(
                chunk,
                request_data,
            )
            if completed_compat_chunk is not None:
                chunk = completed_compat_chunk
                buffered_chunks[-1] = chunk

            if _streaming_module._responses_stream_chunk_is_incomplete_terminal(chunk):
                raise _streaming_module._responses_incomplete_stream_exception(
                    "route recovery attempt ended before response.completed",
                    buffer=buffered_chunks,
                    request_data=request_data,
                )

            started_delivery = True
            for buffered_chunk in buffered_chunks:
                yield _streaming_module._responses_stream_chunk_for_delivery(buffered_chunk)
            buffered_chunks.clear()
            if _streaming_module._responses_stream_chunk_is_completed(chunk):
                return
    except Exception as exc:
        completed_compat = _streaming_module._codex_compaction_done_item_completed_compat(
            completion_state,
            request_data,
            reason="route_recovery_stream_error_after_completed_compaction_item",
            exception=exc,
        )
        if completed_compat is not None:
            for buffered_chunk in buffered_chunks:
                yield _streaming_module._responses_stream_chunk_for_delivery(buffered_chunk)
            yield completed_compat
            return
        raise
    finally:
        await _cancel_next_chunk_task()
        try:
            await fallback_iterator.aclose()
        except Exception:
            pass

def _chat_completions_stream_chunk_is_terminal(
    chunk: Any,
    finished_choice_indices: set[int],
    *,
    expected_choices: int,
) -> bool:
    dumped = _streaming_module._stream_chunk_dump(chunk)
    choices = dumped.get("choices")
    if not isinstance(choices, list):
        return False
    for offset, choice in enumerate(choices):
        if not isinstance(choice, dict) or choice.get("finish_reason") is None:
            continue
        index = choice.get("index")
        finished_choice_indices.add(index if isinstance(index, int) else offset)
    return len(finished_choice_indices) >= expected_choices

def _anthropic_messages_stream_chunk_is_terminal(chunk: Any) -> bool:
    return _streaming_module._stream_chunk_type(chunk) == "message_stop"

def _native_stream_chunk_is_terminal(
    chunk: Any,
    request_data: dict,
    finished_choice_indices: set[int],
) -> bool:
    if _streaming_module._request_is_anthropic_messages_stream(request_data):
        return _streaming_module._anthropic_messages_stream_chunk_is_terminal(chunk)
    requested_choices = request_data.get("n")
    expected_choices = (
        requested_choices
        if isinstance(requested_choices, int) and requested_choices > 0
        else 1
    )
    return _streaming_module._chat_completions_stream_chunk_is_terminal(
        chunk,
        finished_choice_indices,
        expected_choices=expected_choices,
    )

def _native_stream_incomplete_exception(
    request_data: dict,
    *,
    buffered_chunks: int,
    selected_deployment_box: Optional[dict[str, Any]] = None,
) -> Exception:
    is_anthropic = _streaming_module._request_is_anthropic_messages_stream(request_data)
    protocol_name = "Anthropic Messages" if is_anthropic else "Chat Completions"
    terminal_name = "message_stop" if is_anthropic else "finish_reason"
    reason = (
        "anthropic_messages_stream_incomplete"
        if is_anthropic
        else "chat_completions_stream_incomplete"
    )
    exception = RuntimeError(
        f"{protocol_name} stream ended before a terminal {terminal_name}"
    )
    exception.status_code = 503  # type: ignore[attr-defined]
    exception.stream_incomplete = True  # type: ignore[attr-defined]
    exception.body = {  # type: ignore[attr-defined]
        "reason": reason,
        "buffered_chunks": buffered_chunks,
    }
    failure_request = request_data.copy()
    _routing_module._apply_current_selected_deployment_to_request(
        failure_request,
        selected_box=selected_deployment_box,
    )
    _routing_module._mark_exception_for_deployment_failover(
        exception,
        failure_request,
    )
    return exception

async def _yield_native_stream_after_terminal(
    response: Any,
    request_data: dict,
    *,
    selected_deployment_box: Optional[dict[str, Any]] = None,
) -> AsyncIterator[Any]:
    """Validate a native SSE fallback before committing it to the client.

    Chat Completions and Anthropic Messages use different terminal frames, but
    both can be buffered until their own terminal frame. That preserves the
    same routing opportunity as Responses without exposing a partial replay.
    """

    buffered_chunks: List[Any] = []
    finished_choice_indices: set[int] = set()
    saw_terminal = False
    try:
        async for chunk in _streaming_module._stream_with_idle_timeout(response, request_data):
            chunk_exception = _streaming_module._stream_chunk_error_exception(chunk)
            if chunk_exception is not None:
                failure_request = request_data.copy()
                _routing_module._apply_current_selected_deployment_to_request(
                    failure_request,
                    selected_box=selected_deployment_box,
                )
                _routing_module._mark_exception_for_deployment_failover(
                    chunk_exception,
                    failure_request,
                )
                raise chunk_exception
            buffered_chunks.append(chunk)
            saw_terminal = saw_terminal or _streaming_module._native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            )
            if not saw_terminal:
                continue
            success_request = request_data.copy()
            _routing_module._apply_current_selected_deployment_to_request(
                success_request,
                selected_box=selected_deployment_box,
            )
            _routing_module._record_deployment_success_for_cooldown(success_request)
            for buffered_chunk in buffered_chunks:
                yield buffered_chunk
            return
    except Exception:
        raise

    raise _streaming_module._native_stream_incomplete_exception(
        request_data,
        buffered_chunks=len(buffered_chunks),
        selected_deployment_box=selected_deployment_box,
    )

async def _stream_native_route_recovery_poll_attempt(
    request_data: dict,
    exception: Exception,
    *,
    attempt: int,
    deadline: Optional[float] = None,
) -> AsyncIterator[Any]:
    selected_deployment_box: dict[str, Any] = {}
    fallback_iterator = _streaming_module._stream_streaming_error_fallback_round(
        request_data,
        exception,
        allow_repeated_attempt=True,
        route_recovery_poll=True,
    ).__aiter__()
    buffered_chunks: List[Any] = []
    finished_choice_indices: set[int] = set()
    saw_terminal = False
    next_chunk_task = None
    configured_timeout_seconds = (
        _routing_module._stream_start_timeout_seconds_for_request(request_data)
    )
    request_timeout_seconds = configured_timeout_seconds
    if deadline is not None and attempt > 1:
        remaining_poll_seconds = max(0.0, deadline - time.monotonic())
        if remaining_poll_seconds <= 0:
            raise _streaming_module._stream_start_timeout_exception(
                request_data,
                start_seconds=configured_timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            )
        if request_timeout_seconds > 0:
            request_timeout_seconds = min(
                request_timeout_seconds,
                remaining_poll_seconds,
            )
        else:
            request_timeout_seconds = remaining_poll_seconds
    attempt_deadline = (
        time.monotonic() + request_timeout_seconds
        if request_timeout_seconds > 0
        else None
    )

    async def cancel_pending_read() -> None:
        nonlocal next_chunk_task
        task, next_chunk_task = next_chunk_task, None
        await _streaming_module._cancel_pending_read(task)

    def record_success() -> None:
        success_request = request_data.copy()
        _routing_module._apply_current_selected_deployment_to_request(
            success_request,
            selected_box=selected_deployment_box,
        )
        _routing_module._record_deployment_success_for_cooldown(success_request)

    async def yield_completed_attempt() -> AsyncIterator[Any]:
        record_success()
        for buffered_chunk in buffered_chunks:
            yield buffered_chunk

    try:
        while True:
            if next_chunk_task is None:
                selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
                    selected_deployment_box
                )
                try:
                    next_chunk_task = asyncio.create_task(
                        fallback_iterator.__anext__()
                    )
                finally:
                    _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(
                        selected_deployment_box_token
                    )
            try:
                wait_seconds = _streaming_module._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
                if attempt_deadline is not None:
                    remaining_seconds = attempt_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        await cancel_pending_read()
                        if saw_terminal:
                            async for chunk in yield_completed_attempt():
                                yield chunk
                            return
                        raise _streaming_module._stream_route_recovery_wait_timeout_exception(
                            request_data,
                            buffered_chunks=len(buffered_chunks),
                            timeout_seconds=configured_timeout_seconds,
                            selected_deployment_box=selected_deployment_box,
                        ) from None
                    wait_seconds = min(wait_seconds, remaining_seconds)
                chunk = await asyncio.wait_for(
                    asyncio.shield(next_chunk_task),
                    timeout=wait_seconds,
                )
                next_chunk_task = None
            except StopAsyncIteration:
                next_chunk_task = None
                if not saw_terminal:
                    raise _streaming_module._native_stream_incomplete_exception(
                        request_data,
                        buffered_chunks=len(buffered_chunks),
                        selected_deployment_box=selected_deployment_box,
                    )
                async for chunk in yield_completed_attempt():
                    yield chunk
                return
            except asyncio.TimeoutError:
                if next_chunk_task is not None and next_chunk_task.done():
                    completed_task = next_chunk_task
                    next_chunk_task = None
                    try:
                        chunk = completed_task.result()
                    except StopAsyncIteration:
                        if not saw_terminal:
                            raise _streaming_module._native_stream_incomplete_exception(
                                request_data,
                                buffered_chunks=len(buffered_chunks),
                                selected_deployment_box=selected_deployment_box,
                            )
                        async for completed_chunk in yield_completed_attempt():
                            yield completed_chunk
                        return
                    except Exception:
                        if saw_terminal:
                            async for completed_chunk in yield_completed_attempt():
                                yield completed_chunk
                            return
                        raise
                elif attempt_deadline is not None and time.monotonic() >= attempt_deadline:
                    await cancel_pending_read()
                    if saw_terminal:
                        async for completed_chunk in yield_completed_attempt():
                            yield completed_chunk
                        return
                    raise _streaming_module._stream_route_recovery_wait_timeout_exception(
                        request_data,
                        buffered_chunks=len(buffered_chunks),
                        timeout_seconds=configured_timeout_seconds,
                        selected_deployment_box=selected_deployment_box,
                    ) from None
                else:
                    yield _streaming_module._route_recovery_sse_keepalive(
                        attempt,
                        request_data=request_data,
                        phase="attempt",
                    )
                    continue
            except Exception:
                if saw_terminal:
                    async for completed_chunk in yield_completed_attempt():
                        yield completed_chunk
                    return
                raise

            chunk_exception = _streaming_module._stream_chunk_error_exception(chunk)
            if chunk_exception is not None:
                if saw_terminal:
                    async for completed_chunk in yield_completed_attempt():
                        yield completed_chunk
                    return
                failure_request = request_data.copy()
                _routing_module._apply_current_selected_deployment_to_request(
                    failure_request,
                    selected_box=selected_deployment_box,
                )
                _routing_module._mark_exception_for_deployment_failover(
                    chunk_exception,
                    failure_request,
                )
                raise chunk_exception

            buffered_chunks.append(chunk)
            saw_terminal = saw_terminal or _streaming_module._native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            )
    finally:
        await cancel_pending_read()
        try:
            await fallback_iterator.aclose()
        except Exception:
            pass

def _route_recovery_poll_keep_going(exception: Exception) -> bool:
    if _routing_module._is_upstream_model_not_found_error(exception):
        return False
    # A route that refuses this body by size will refuse it on every later
    # attempt too, so an explicit rejection ends the poll instead of replaying
    # identical bytes for the rest of the recovery window.
    if _routing_module._is_request_body_size_rejection_error(exception):
        return False
    if _streaming_module._external_web_search_exception_has_recovery_request(exception):
        return True
    return _routing_module._is_route_recovery_poll_error(exception)

def _external_web_search_exception_has_recovery_request(
    exception: Exception,
) -> bool:
    return (
        _responses_web_search_bridge_module._external_web_search_recovery_request_from_exception(
            exception,
        )
        is not None
    )

def _external_web_search_recovery_poll_error(exception: Exception) -> bool:
    return bool(
        _routing_module._is_route_recovery_poll_error(exception)
        or _streaming_module._external_web_search_exception_has_recovery_request(exception)
    )

def _external_web_search_recovery_payload_for_blocked_original(
    request_data: dict,
    exception: Exception,
) -> Optional[dict[str, Any]]:
    if not _routing_module._should_block_external_web_search_original_recovery(request_data):
        return None
    recovery_request = (
        _responses_web_search_bridge_module._external_web_search_recovery_request_from_exception(
            exception,
        )
    )
    if recovery_request is None:
        return None
    recovery_request["stream"] = True
    if not _responses_web_search_bridge_module._external_web_search_is_recovery_payload(
        recovery_request,
    ):
        return None
    return recovery_request

def _is_external_web_search_synthesis_recovery_payload(
    request_data: Optional[dict],
) -> bool:
    metadata = _request_context_module._request_metadata_dict(
        request_data,
        "litellm_metadata",
    ) or {}
    return metadata.get("external_web_search_synthesis") is True

def _external_web_search_non_stream_synthesis_payload(
    request_data: dict,
) -> Optional[dict[str, Any]]:
    if not _streaming_module._is_external_web_search_synthesis_recovery_payload(request_data):
        return None
    if not _streaming_module._request_is_responses_stream(request_data):
        return None
    method_name = _streaming_module._streaming_error_fallback_method_name(request_data)
    if method_name != "aresponses":
        return None
    payload = _streaming_module._build_streaming_error_fallback_payload(
        request_data,
        method_name=method_name,
        allow_repeated_attempt=True,
    )
    if payload is None:
        return None
    payload["stream"] = False
    payload.pop("stream_options", None)
    payload.pop("stream_timeout", None)
    litellm_metadata = _request_context_module._request_metadata_dict(
        payload,
        "litellm_metadata",
    ) or {}
    updated_metadata = litellm_metadata.copy()
    updated_metadata[_ROUTE_RECOVERY_POLL_METADATA_KEY] = True
    payload["litellm_metadata"] = updated_metadata
    target_order = _routing_module._coerce_order(
        request_data.get(_streaming_module._ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY)
    )
    if target_order is None:
        target_order = _responses_request_module._request_target_order(request_data)
    if target_order is None:
        for section_name in ("litellm_params", "model_info"):
            section = request_data.get(section_name)
            if not isinstance(section, dict):
                continue
            target_order = _routing_module._coerce_order(section.get("order"))
            if target_order is not None:
                break
            target_order = _routing_module._order_from_route_key(section.get("route_key"))
            if target_order is not None:
                break
    if target_order is not None:
        payload["_target_order"] = target_order
    payload.pop("_excluded_deployment_ids", None)
    return payload

async def _external_web_search_non_stream_synthesis_recovery(
    request_data: dict,
    exception: Exception,
) -> Optional[AsyncIterator[Any]]:
    if not _streaming_module._is_external_web_search_synthesis_recovery_payload(request_data):
        return None
    if not _routing_module._is_route_recovery_poll_error(exception):
        return None

    from litellm.proxy.proxy_server import llm_router

    if llm_router is None:
        raise RuntimeError("LiteLLM router is unavailable for external web_search synthesis recovery")
    router_method = getattr(llm_router, "aresponses", None)
    if router_method is None:
        raise RuntimeError("LiteLLM router does not support aresponses external web_search synthesis recovery")
    payload = _streaming_module._external_web_search_non_stream_synthesis_payload(
        request_data,
    )
    if payload is None:
        return None

    _trace_module._request_route_trace(
        "external_web_search_synthesis_non_stream_recovery_start", request_data,
        target_order=payload.get("_target_order"),
        excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
        request=_trace_module._trace_request_summary(request_data),
        retry_request=_trace_module._trace_request_summary(payload, method_name="aresponses"),
        exception=_routing_module._trace_exception(exception),
    )
    try:
        response = await router_method(**payload)
        _responses_web_search_bridge_module._external_web_search_raise_if_invalid_model_response(
            response,
            payload,
            phase="synthesis",
        )
        _trace_module._request_route_trace(
            "external_web_search_synthesis_non_stream_recovery_done", request_data,
            target_order=payload.get("_target_order"),
            excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
            response=_trace_module._trace_response_summary(response, payload),
        )
        return _streaming_module._non_streaming_response_as_stream(response, payload)
    except Exception as recovery_exception:
        _routing_module._mark_no_deployments_for_order_exhaustion(
            recovery_exception,
            payload,
        )
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            recovery_exception,
            payload,
        ):
            _routing_module._mark_exception_for_deployment_failover(
                recovery_exception,
                payload,
            )
        _trace_module._request_route_trace(
            "external_web_search_synthesis_non_stream_recovery_error", request_data,
            target_order=payload.get("_target_order"),
            excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
            request=_trace_module._trace_request_summary(request_data),
            retry_request=_trace_module._trace_request_summary(payload, method_name="aresponses"),
            original_exception=_routing_module._trace_exception(exception),
            exception=_routing_module._trace_exception(recovery_exception),
        )
        raise recovery_exception

def _route_recovery_sse_keepalive(
    attempt: int,
    *,
    request_data: Optional[dict] = None,
    phase: str = "poll",
) -> Any:
    if request_data is not None:
        _state_module._touch_route_recovery_state(
            _streaming_module._route_recovery_state_key(request_data)
        )
    if _streaming_module._request_is_responses_stream(request_data):
        return _JSONStreamEvent(
            {
                "type": "response.metadata",
                "metadata": {
                    "phase": phase,
                    "attempt": attempt,
                },
            }
        )
    if _streaming_module._request_uses_native_event_stream(request_data):
        return (
            f": young_router route_recovery phase={phase} attempt={attempt}\n\n"
        ).encode("utf-8")
    return _streaming_module._sse_comment_event(
        f"young_router route_recovery phase={phase} attempt={attempt}"
    )

def _is_route_recovery_sse_keepalive(chunk: Any) -> bool:
    if isinstance(chunk, bytes):
        return chunk.startswith(b": young_router route_recovery ")
    if isinstance(chunk, str):
        return chunk.startswith(": young_router route_recovery ")
    if isinstance(chunk, dict) and chunk.get("type") == "response.metadata":
        metadata = chunk.get("metadata")
        return isinstance(metadata, dict) and isinstance(metadata.get("phase"), str)
    return False

def _stream_keepalive_chunk(request_data: Optional[dict], sequence: int) -> Any:
    """A no-op downstream event emitted while the delivery stream is silent.

    The wire form follows ``_route_recovery_sse_keepalive``: Responses
    clients parse every ``data:`` payload as JSON, so they receive a parseable
    ``response.metadata`` event; native event-stream clients receive a bare
    SSE comment; other SSE clients receive a comment string. The event carries
    no output, so clients that ignore unknown types observe nothing.
    """

    if _streaming_module._request_is_responses_stream(request_data):
        return _JSONStreamEvent(
            {
                "type": "response.metadata",
                "metadata": {
                    "phase": "keepalive",
                    "sequence": sequence,
                },
            }
        )
    if _streaming_module._request_uses_native_event_stream(request_data):
        return (f": young_router keepalive sequence={sequence}\n\n").encode("utf-8")
    return _streaming_module._sse_comment_event(f"young_router keepalive sequence={sequence}")

def _is_stream_keepalive_chunk(chunk: Any) -> bool:
    if isinstance(chunk, bytes):
        return chunk.startswith(b": young_router keepalive ")
    if isinstance(chunk, str):
        return chunk.startswith(": young_router keepalive ")
    return (
        isinstance(chunk, dict)
        and chunk.get("type") == "response.metadata"
        and isinstance(chunk.get("metadata"), dict)
        and chunk["metadata"].get("phase") == "keepalive"
    )

async def _yield_downstream_keepalive_stream(
    delivery: AsyncIterator[Any],
    request_data: dict,
) -> AsyncIterator[Any]:
    """Heartbeat wrapper around the final downstream delivery stream.

    While the wrapped delivery generator has nothing to yield -- the buffered
    start phase, a silent upstream reasoning window, or route-recovery polls --
    emit one keepalive chunk per interval so client-side byte-idle timers do
    not fire before the proxy's own budgets do. The heartbeat adds no timeout
    of its own: every internal wait is already bounded by its existing
    start/idle/recovery deadline, and this wrapper only fills the silence.
    """

    interval = _routing_module._stream_keepalive_interval_seconds_for_request(
        request_data
    )
    if interval <= 0:
        async for chunk in delivery:
            yield chunk
        return

    iterator = delivery.__aiter__()
    pending: Optional[asyncio.Task[Any]] = asyncio.create_task(iterator.__anext__())
    sequence = 0

    async def cancel_pending_read() -> None:
        nonlocal pending
        task = pending
        pending = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception:
            pass

    try:
        while True:
            task = pending
            if task is None:
                return
            if task.done():
                try:
                    chunk = task.result()
                except StopAsyncIteration:
                    pending = None
                    return
                pending = asyncio.create_task(iterator.__anext__())
                yield chunk
                continue
            try:
                chunk = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                # The pending read may complete exactly at the timeout; resolve
                # it through the done-task branch instead of dropping a chunk.
                if task.done():
                    continue
                sequence += 1
                yield _streaming_module._stream_keepalive_chunk(request_data, sequence)
                continue
            except StopAsyncIteration:
                pending = None
                return
            pending = asyncio.create_task(iterator.__anext__())
            yield chunk
    finally:
        await cancel_pending_read()
        await _streaming_module._close_async_iterator_safely(delivery)

async def _sleep_route_recovery_poll_interval(
    delay_seconds: float,
    *,
    attempt: int,
    request_data: Optional[dict] = None,
    phase: str = "interval",
    emit_for_short_delay: bool = False,
) -> AsyncIterator[Any]:
    if delay_seconds <= 0:
        return
    if delay_seconds < _streaming_module._ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS:
        if emit_for_short_delay:
            yield _streaming_module._route_recovery_sse_keepalive(
                attempt,
                request_data=request_data,
                phase=phase,
            )
        await asyncio.sleep(delay_seconds)
        return

    remaining = delay_seconds
    while remaining > 0:
        yield _streaming_module._route_recovery_sse_keepalive(
            attempt,
            request_data=request_data,
            phase=phase,
        )
        sleep_seconds = min(_streaming_module._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS, remaining)
        await asyncio.sleep(sleep_seconds)
        remaining -= sleep_seconds

async def _stream_route_recovery_poll(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    uses_native_event_stream = _streaming_module._request_uses_native_event_stream(request_data)
    recovery_request = _streaming_module._external_web_search_recovery_payload_for_blocked_original(
        request_data,
        exception,
    )
    if recovery_request is not None:
        _trace_module._request_route_trace(
            "external_web_search_original_route_recovery_resumed_with_payload", request_data,
            request=_trace_module._trace_request_summary(request_data),
            retry_request=_trace_module._trace_request_summary(recovery_request),
            exception=_routing_module._trace_exception(exception),
        )
        request_data = recovery_request
    elif _routing_module._should_block_external_web_search_original_recovery(request_data):
        _trace_module._request_route_trace(
            "external_web_search_original_route_recovery_blocked", request_data,
            request=_trace_module._trace_request_summary(request_data),
            exception=_routing_module._trace_exception(exception),
        )
        yield _streaming_module._external_web_search_missing_answer_failed_event(request_data, exception)
        return
    max_poll_seconds = _routing_module._recovery_max_seconds_for_request(request_data)
    if max_poll_seconds <= 0:
        return
    # The request body was already refused by size on this request: another
    # attempt forwards the same bytes, so the poll cannot make progress and
    # must not hold the client open until the recovery window expires.
    if _routing_module._request_body_size_rejected(request_data):
        _trace_module._request_route_trace(
            "route_recovery_poll_body_size_rejected", request_data,
            exception=_routing_module._trace_exception(exception),
        )
        return
    if not _streaming_module._external_web_search_recovery_poll_error(exception):
        return
    started_at_monotonic = time.monotonic()
    poll_interval_seconds = _routing_module._recovery_interval_seconds()
    # A network outage is an external connectivity condition, not a request
    # failure.  Keep the existing stream alive until the client cancels it or
    # an upstream route recovers; otherwise the timeout below becomes the
    # exact point at which Codex receives a connection failure and starts its
    # own reconnect loop.
    network_recovery = (
        _routing_module._is_network_recovery_exception(exception)
        and _routing_module._recovery_policy_for_exception(exception)
        != _routing_module._RECOVERY_POLICY_ERROR
    )
    if network_recovery:
        # Establish every supported stream immediately while the first
        # upstream probe is pending.  Without this event a quiet offline
        # interval is indistinguishable from a failed request to the client.
        yield _streaming_module._route_recovery_sse_keepalive(
            0,
            request_data=request_data,
            phase="network",
        )
    deadline: Optional[float] = (
        None if network_recovery else started_at_monotonic + max_poll_seconds
    )
    last_exception = exception
    attempt = 0
    waited_for_all_cooldown = False
    ignore_local_constraints = bool(
        request_data.pop("_route_recovery_ignore_local_constraints", False)
    )
    recovery_state_key = _streaming_module._route_recovery_state_upsert(
        request_data,
        exception,
        status="polling",
        attempt=attempt,
        started_at_monotonic=started_at_monotonic,
        max_poll_seconds=max_poll_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    _trace_module._request_route_trace(
        "route_recovery_poll_start", request_data,
        max_poll_seconds=max_poll_seconds,
        poll_interval_seconds=poll_interval_seconds,
        exception=_routing_module._trace_exception(exception),
    )

    async def yield_non_stream_synthesis_recovery(
        recovery_exception: Exception,
        *,
        reason: str,
    ) -> AsyncIterator[Any]:
        try:
            recovered_stream = await _streaming_module._external_web_search_non_stream_synthesis_recovery(
                request_data,
                recovery_exception,
            )
            if recovered_stream is None:
                return
            async for recovered_chunk in recovered_stream:
                yield recovered_chunk
        except Exception as non_stream_exception:
            nonlocal_last_exception[0] = non_stream_exception
            _trace_module._request_route_trace(
                "external_web_search_synthesis_non_stream_recovery_failed", request_data,
                poll_attempt=attempt,
                reason=reason,
                original_exception=_routing_module._trace_exception(recovery_exception),
                exception=_routing_module._trace_exception(non_stream_exception),
            )
            return

    nonlocal_last_exception: list[Exception] = [last_exception]

    try:
        while True:
            last_exception = nonlocal_last_exception[0]
            cooldown_wait = _streaming_module._route_recovery_poll_cooldown_wait(
                request_data,
                ignore_constraints=ignore_local_constraints,
            )
            if cooldown_wait is not None:
                waited_for_all_cooldown = True
                now = time.monotonic()
                if max_poll_seconds <= 0 or (
                    deadline is not None and now >= deadline
                ):
                    _trace_module._request_route_trace(
                        "route_recovery_poll_max_duration_reached", request_data,
                        poll_attempts=attempt,
                        elapsed_seconds=round(now - started_at_monotonic, 3),
                        max_poll_seconds=max_poll_seconds,
                        exception=_routing_module._trace_exception(last_exception),
                    )
                    break
                cooldown_until = cooldown_wait["cooldown_until"]
                delay_seconds = _streaming_module._route_recovery_cooldown_wait_delay(
                    cooldown_until,
                    poll_interval_seconds=poll_interval_seconds,
                    deadline=deadline,
                )
                _trace_module._request_route_trace(
                    "route_recovery_poll_waiting_for_cooldown", request_data,
                    poll_attempt=attempt,
                    cooldown_until=cooldown_until,
                    cooldown_remaining_seconds=round(
                        max(0.0, cooldown_until - time.time()), 3
                    ),
                    cooldown_deployments=cooldown_wait["cooldown_deployments"],
                    exception=_routing_module._trace_exception(last_exception),
                )
                new_recovery_state_key = _streaming_module._route_recovery_state_upsert(
                    request_data,
                    last_exception,
                    status="waiting",
                    attempt=attempt,
                    started_at_monotonic=started_at_monotonic,
                    max_poll_seconds=max_poll_seconds,
                    poll_interval_seconds=delay_seconds,
                    target_order=_responses_request_module._request_target_order(request_data),
                    cooldown_until=cooldown_until,
                    cooldown_deployments=cooldown_wait["cooldown_deployments"],
                )
                if new_recovery_state_key and new_recovery_state_key != recovery_state_key:
                    _streaming_module._route_recovery_state_remove(recovery_state_key)
                    recovery_state_key = new_recovery_state_key
                async for keepalive in _streaming_module._sleep_route_recovery_poll_interval(
                    delay_seconds,
                    attempt=attempt,
                    request_data=request_data,
                    phase="cooldown",
                    emit_for_short_delay=True,
                ):
                    yield keepalive
                continue
            recovery_request = _streaming_module._external_web_search_recovery_payload_for_blocked_original(
                request_data,
                last_exception,
            )
            if recovery_request is not None:
                _trace_module._request_route_trace(
                    "external_web_search_original_route_recovery_resumed_with_payload", request_data,
                    request=_trace_module._trace_request_summary(request_data),
                    retry_request=_trace_module._trace_request_summary(recovery_request),
                    exception=_routing_module._trace_exception(last_exception),
                )
                request_data = recovery_request
            elif _routing_module._should_block_external_web_search_original_recovery(request_data):
                _trace_module._request_route_trace(
                    "external_web_search_original_route_recovery_blocked", request_data,
                    request=_trace_module._trace_request_summary(request_data),
                    exception=_routing_module._trace_exception(last_exception),
                )
                break
            if waited_for_all_cooldown:
                # Only this path deliberately reopens the full route pool.
                # Normal recovery must retain its ordered fallback constraints.
                request_data.pop("_excluded_deployment_ids", None)
                request_data.pop("_target_order", None)
                request_data.pop(_streaming_module._ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY, None)
                _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(set())
                for attribute in (
                    "excluded_deployment_ids",
                    "failed_deployment_id",
                    "failed_deployment_route_key",
                    "failed_deployment_order",
                    "failed_deployment_surface",
                ):
                    try:
                        delattr(last_exception, attribute)
                    except AttributeError:
                        pass
                    except Exception:
                        pass
                waited_for_all_cooldown = False
                ignore_local_constraints = False
            now = time.monotonic()
            if (
                attempt > 0
                and max_poll_seconds > 0
                and deadline is not None
                and now >= deadline
            ):
                _trace_module._request_route_trace(
                    "route_recovery_poll_max_duration_reached", request_data,
                    poll_attempts=attempt,
                    elapsed_seconds=round(now - started_at_monotonic, 3),
                    max_poll_seconds=max_poll_seconds,
                    exception=_routing_module._trace_exception(last_exception),
                )
                break
            attempt += 1
            attempt_started_at = now
            if (
                _routing_module._is_request_scoped_priority_deployment_failover_error(
                    last_exception,
                    request_data,
                )
                and (
                    not _routing_module._should_retry_same_deployment_before_fallback(last_exception)
                    or _routing_module._is_local_stream_start_timeout_error(last_exception)
                )
            ):
                _routing_module._mark_exception_for_deployment_failover(last_exception, request_data)
                _routing_module._sync_failed_deployment_exclusions(request_data, last_exception)
            request_data.pop(_streaming_module._ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY, None)
            no_deployments_available = _routing_module._is_no_deployments_available_error(
                last_exception,
            )
            refresh_after_exhausted_poll = no_deployments_available and attempt > 1
            forced_target_order = None
            if refresh_after_exhausted_poll:
                previous_excluded_ids = sorted(
                    _responses_request_module._request_excluded_deployment_ids(request_data),
                )
                try:
                    from litellm.proxy.proxy_server import llm_router
                except Exception:
                    llm_router = None
                if llm_router is not None:
                    forced_target_order = _streaming_module._route_recovery_next_poll_order(
                        llm_router,
                        request_data,
                        last_exception,
                    )
                _trace_module._request_route_trace(
                    "route_recovery_poll_route_pool_reset", request_data,
                    poll_attempt=attempt,
                    next_target_order=forced_target_order,
                    excluded_deployment_ids=previous_excluded_ids,
                    exception=_routing_module._trace_exception(last_exception),
                )
            _streaming_module._reset_route_exhaustion_retry_state(
                request_data,
                last_exception,
                preserve_failed_deployment=(
                    _responses_execution_module._failed_deployment_id(last_exception) is not None
                    and not no_deployments_available
                ),
                preserve_existing_exclusions=(
                    no_deployments_available
                    and not refresh_after_exhausted_poll
                ),
                preserve_target_order=(
                    no_deployments_available
                    and not refresh_after_exhausted_poll
                ),
            )
            if forced_target_order is not None:
                request_data[_streaming_module._ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY] = forced_target_order

            target_order = forced_target_order
            if target_order is None:
                target_order = _responses_request_module._request_target_order(request_data)
            new_recovery_state_key = _streaming_module._route_recovery_state_upsert(
                request_data,
                last_exception,
                status="polling",
                attempt=attempt,
                started_at_monotonic=started_at_monotonic,
                max_poll_seconds=max_poll_seconds,
                poll_interval_seconds=poll_interval_seconds,
                target_order=target_order,
            )
            if new_recovery_state_key and new_recovery_state_key != recovery_state_key:
                _streaming_module._route_recovery_state_remove(recovery_state_key)
                recovery_state_key = new_recovery_state_key

            _trace_module._request_route_trace(
                "route_recovery_poll_attempt_start", request_data,
                poll_attempt=attempt,
                target_order=target_order,
                elapsed_seconds=round(attempt_started_at - started_at_monotonic, 3),
                remaining_poll_seconds=(
                    None
                    if deadline is None
                    else max(0.0, round(deadline - attempt_started_at, 3))
                ),
                exception=_routing_module._trace_exception(last_exception),
            )

            try:
                yielded = False
                poll_attempt = (
                    _streaming_module._stream_native_route_recovery_poll_attempt
                    if uses_native_event_stream
                    else _streaming_module._stream_route_recovery_poll_attempt
                )
                async for chunk in poll_attempt(
                    request_data,
                    last_exception,
                    attempt=attempt,
                    deadline=deadline,
                ):
                    if _streaming_module._is_route_recovery_sse_keepalive(chunk):
                        yield chunk
                        continue
                    yielded = True
                    if uses_native_event_stream:
                        yield chunk
                    else:
                        yield _streaming_module._responses_stream_chunk_for_delivery(chunk)
                if yielded:
                    _trace_module._request_route_trace(
                        "route_recovery_poll_success", request_data,
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                    )
                    return
                _trace_module._request_route_trace(
                    "route_recovery_poll_attempt_empty", request_data,
                    poll_attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                    exception=_routing_module._trace_exception(last_exception),
                )
                async for recovered_chunk in yield_non_stream_synthesis_recovery(
                    last_exception,
                    reason="empty_stream_attempt",
                ):
                    yield _streaming_module._responses_stream_chunk_for_delivery(recovered_chunk)
                    yielded = True
                if yielded:
                    _trace_module._request_route_trace(
                        "route_recovery_poll_success", request_data,
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        recovery_mode="external_web_search_synthesis_non_stream",
                    )
                    return
                last_exception = nonlocal_last_exception[0]
            except Exception as poll_exception:
                if _routing_module._is_context_size_error(poll_exception):
                    _trace_module._request_route_trace(
                        "route_recovery_poll_context_size_error", request_data,
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        exception=_routing_module._trace_exception(poll_exception),
                    )
                    raise
                if not _streaming_module._route_recovery_poll_keep_going(poll_exception):
                    last_exception = poll_exception
                    nonlocal_last_exception[0] = last_exception
                    _trace_module._request_route_trace(
                        "route_recovery_poll_terminal_error", request_data,
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        exception=_routing_module._trace_exception(last_exception),
                    )
                    break
                last_exception = poll_exception
                nonlocal_last_exception[0] = last_exception
                _trace_module._request_route_trace(
                    "route_recovery_poll_attempt_failed", request_data,
                    poll_attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                    exception=_routing_module._trace_exception(last_exception),
                )
                yielded_non_stream = False
                async for recovered_chunk in yield_non_stream_synthesis_recovery(
                    last_exception,
                    reason="stream_attempt_failed",
                ):
                    yielded_non_stream = True
                    yield _streaming_module._responses_stream_chunk_for_delivery(recovered_chunk)
                if yielded_non_stream:
                    _trace_module._request_route_trace(
                        "route_recovery_poll_success", request_data,
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        recovery_mode="external_web_search_synthesis_non_stream",
                    )
                    return
                last_exception = nonlocal_last_exception[0]

            now = time.monotonic()
            if max_poll_seconds <= 0 or (
                deadline is not None and now >= deadline
            ):
                _trace_module._request_route_trace(
                    "route_recovery_poll_max_duration_reached", request_data,
                    poll_attempts=attempt,
                    elapsed_seconds=round(now - started_at_monotonic, 3),
                    max_poll_seconds=max_poll_seconds,
                    exception=_routing_module._trace_exception(last_exception),
                )
                break

            delay_seconds = (
                poll_interval_seconds
                if deadline is None
                else min(poll_interval_seconds, max(0.0, deadline - now))
            )
            _trace_module._request_route_trace(
                "route_recovery_poll_next_attempt_scheduled", request_data,
                poll_attempt=attempt,
                poll_interval_seconds=delay_seconds,
                elapsed_seconds=round(now - started_at_monotonic, 3),
                exception=_routing_module._trace_exception(last_exception),
            )
            new_recovery_state_key = _streaming_module._route_recovery_state_upsert(
                request_data,
                last_exception,
                status="waiting",
                attempt=attempt,
                started_at_monotonic=started_at_monotonic,
                max_poll_seconds=max_poll_seconds,
                poll_interval_seconds=delay_seconds,
                target_order=_responses_request_module._request_target_order(request_data),
            )
            if new_recovery_state_key and new_recovery_state_key != recovery_state_key:
                _streaming_module._route_recovery_state_remove(recovery_state_key)
                recovery_state_key = new_recovery_state_key
            async for keepalive in _streaming_module._sleep_route_recovery_poll_interval(
                delay_seconds,
                attempt=attempt,
                request_data=request_data,
            ):
                yield keepalive

        if uses_native_event_stream:
            if (
                _routing_module._is_context_size_error(last_exception)
                or _routing_module._is_terminal_prompt_or_policy_error(last_exception)
            ):
                raise last_exception
            yield _streaming_module._synthesized_native_stream_error_chunk(request_data, last_exception)
            return
        yield _streaming_module._synthesized_failed_response_event(request_data, last_exception)
    finally:
        _streaming_module._route_recovery_state_remove(recovery_state_key)

def _route_recovery_poll_cooldown_wait(
    request_data: dict,
    *,
    ignore_constraints: bool = False,
) -> Optional[dict[str, Any]]:
    """Describe an all-cooled route pool without opening another upstream call."""

    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return None
    model_group = _responses_execution_module._request_model_group(request_data)
    if not isinstance(model_group, str) or not model_group.strip():
        return None
    try:
        metadata = _request_context_module._request_metadata_dict(
            request_data, "metadata"
        ) or {}
        deployments = _routing_module._router_configured_deployments(
            llm_router,
            model_group,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return None
    if not deployments:
        return None
    if ignore_constraints:
        # After a local fallback chain has exhausted its own route hints, the
        # next recovery stream must distinguish that local exhaustion from an
        # actual shared cooldown across the complete model group.
        constraints = request_data.copy()
        constraints.pop("_excluded_deployment_ids", None)
        constraints.pop("_target_order", None)
        constraints.pop(_streaming_module._ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY, None)
    else:
        # An active explicit target/order is a real request constraint. Do not
        # hold an unrelated group route open merely because it is cooling.
        constraints = request_data
    constrained = _responses_request_module._with_retry_target_constraints(
        deployments,
        constraints,
    )
    if not constrained:
        return None
    available, cooled, cooldown_filtered = _routing_module._with_active_deployment_cooldowns(
        constrained,
        request_kwargs=request_data,
    )
    if not (cooldown_filtered and cooled and not available):
        return None
    cooldown_until_values: list[float] = []
    for entry in cooled:
        try:
            cooldown_until = float(entry.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            continue
        if cooldown_until > time.time():
            cooldown_until_values.append(cooldown_until)
    if not cooldown_until_values:
        return None
    return {
        "cooldown_until": min(cooldown_until_values),
        "cooldown_deployments": cooled,
    }

def _route_recovery_poll_has_no_available_deployments(request_data: dict) -> bool:
    """Compatibility predicate for callers that only need the cooling state."""

    return _streaming_module._route_recovery_poll_cooldown_wait(request_data) is not None

def _route_recovery_cooldown_wait_delay(
    cooldown_until: float,
    *,
    poll_interval_seconds: float,
    deadline: Optional[float],
) -> float:
    remaining_cooldown = max(0.0, cooldown_until - time.time())
    remaining_poll = (
        float("inf")
        if deadline is None
        else max(0.0, deadline - time.monotonic())
    )
    return max(
        0.001,
        min(poll_interval_seconds, remaining_cooldown or 0.001, remaining_poll or 0.001),
    )
