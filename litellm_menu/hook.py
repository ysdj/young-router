from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_execution as _responses_execution_module
from . import responses_surfaces as _responses_surfaces_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import state as _state_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    CustomLogger,
    Dict,
    List,
    Optional,
    _STREAM_FALLBACK_TEXT_FLUSH_CHARS,
    litellm,
)



class LiteLLMMenuHook(CustomLogger):
    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        _routing_module._record_deployment_success_for_cooldown(kwargs)
        _state_module._append_recent_request(
            _routing_module._request_log_record("success", kwargs, response_obj, start_time, end_time)
        )

    async def async_log_failure_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        if _routing_module._should_suppress_recent_failure_log(kwargs, response_obj):
            return
        _state_module._append_recent_request(
            _routing_module._request_log_record("failure", kwargs, response_obj, start_time, end_time)
        )

    async def async_log_stream_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        _routing_module._record_deployment_success_for_cooldown(kwargs)
        _state_module._append_recent_request(
            _routing_module._request_log_record("stream", kwargs, response_obj, start_time, end_time)
        )

    async def async_pre_call_deployment_hook(
        self,
        kwargs: Dict[str, Any],
        call_type: Any,
    ) -> Optional[dict]:
        modified_kwargs = kwargs
        changed = False
        for update_request in (
            _image_generation_module._with_bounded_image_inputs,
            _image_generation_module._with_internal_litellm_metadata,
            _image_generation_module._with_empty_tool_controls_removed,
            _image_generation_module._with_codex_compaction_controls,
            _image_generation_module._with_responses_native_extra_body,
            _image_generation_module._with_codex_compaction_headers,
            _image_generation_module._with_stream_request_timeout,
            _image_generation_module._with_incoming_user_agent_header,
            _image_generation_module._with_browser_compatible_headers,
        ):
            updated_kwargs = update_request(modified_kwargs)
            if updated_kwargs is None:
                continue
            modified_kwargs = updated_kwargs
            changed = True
        return modified_kwargs if changed else None

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: List[dict],
        messages: Optional[List[Dict[str, Any]]],
        request_kwargs: Optional[dict] = None,
        parent_otel_span: Any = None,
    ) -> List[dict]:
        try:
            original_deployments = healthy_deployments
            candidate_deployments = _image_generation_module._with_retry_target_constraints(
                healthy_deployments,
                request_kwargs,
            )
            after_constraints = candidate_deployments
            candidate_deployments, cooldown_deployments, cooldown_filtered = (
                _routing_module._with_active_deployment_cooldowns(
                    candidate_deployments,
                    request_kwargs=request_kwargs,
                )
            )
            after_cooldown = candidate_deployments
            responses_surface_filtered = False
            image_generation_filtered = False
            web_search_filtered = False
            web_search_unsupported_bridge = False
            responses_image_input_filtered = False

            has_image_generation_tool = _tools_module._request_has_image_generation_tool(request_kwargs)

            if (
                not has_image_generation_tool
                and _image_generation_module._request_has_image_input(request_kwargs)
                and _image_generation_module._request_is_responses_api(request_kwargs)
            ):
                responses_image_safe = [
                    deployment
                    for deployment in candidate_deployments
                    if _image_generation_module._deployment_allows_responses_image_input(deployment)
                ]
                responses_image_input_filtered = bool(responses_image_safe)
                candidate_deployments = responses_image_safe or candidate_deployments

            _trace_module._route_trace(
                "filter_deployments",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=model,
                target_order=_image_generation_module._request_target_order(request_kwargs),
                excluded_deployment_ids=sorted(_image_generation_module._request_excluded_deployment_ids(request_kwargs)),
                has_image_generation_tool=has_image_generation_tool,
                has_web_search_tool=_tools_module._request_has_web_search_tool(request_kwargs),
                has_image_input=_image_generation_module._request_has_image_input(request_kwargs),
                deployment_cooldown_filtered=cooldown_filtered,
                deployment_cooldown_all_candidates=bool(
                    cooldown_deployments and not after_cooldown
                ),
                deployment_cooldown_deployments=cooldown_deployments,
                responses_surface_filtered=responses_surface_filtered,
                image_generation_filtered=image_generation_filtered,
                web_search_filtered=web_search_filtered,
                web_search_unsupported_bridge=web_search_unsupported_bridge,
                responses_image_input_filtered=responses_image_input_filtered,
                request_preview=_trace_module._trace_request_preview(request_kwargs, messages=messages),
                request=_trace_module._trace_request_summary(request_kwargs, messages=messages),
                healthy=_routing_module._trace_deployments(original_deployments),
                after_constraints=_routing_module._trace_deployments(after_constraints),
                after_cooldown=_routing_module._trace_deployments(after_cooldown),
                selected_candidates=_routing_module._trace_deployments(candidate_deployments),
            )
            return candidate_deployments
        except Exception as exc:
            _trace_module._route_trace(
                "filter_deployments_error",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=model,
                request_preview=_trace_module._trace_request_preview(request_kwargs, messages=messages),
                request=_trace_module._trace_request_summary(request_kwargs, messages=messages),
                exception=_routing_module._trace_exception(exc),
            )
            return healthy_deployments

    async def async_post_call_success_deployment_hook(
        self,
        request_data: dict,
        response: Any,
        call_type: Any,
    ) -> Optional[Any]:
        if request_data.get("stream") is not True:
            _routing_module._record_deployment_success_for_cooldown(request_data)
        response = _image_generation_module._sanitize_response_echoed_request_images_for_delivery(response, request_data)
        response = _responses_web_search_bridge_module._sanitize_response_stream_payload(response)
        if _tools_module._request_is_unmarked_internal_web_search_bridge_post_call(request_data):
            return response
        if (
            not _tools_module._request_suppresses_external_web_search_post_call(request_data)
            and _tools_module._request_should_consume_litellm_web_search_function_call(request_data)
            and _responses_web_search_bridge_module._has_litellm_web_search_actions_for_request(response, request_data)
        ):
            original_function = _responses_execution_module._responses_bridge_original_function(request_data)
            _trace_module._route_trace(
                "external_web_search_bridge_post_call_start",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(request_data),
                deployment_id=_routing_module._deployment_id_from_request(request_data),
                route_key=_routing_module._deployment_route_key_from_request(request_data),
                has_original_function=original_function is not None,
                request=_trace_module._trace_request_summary(request_data, call_type=call_type),
                response=_trace_module._trace_response_summary(response, request_data),
                actions=_responses_web_search_bridge_module._litellm_web_search_actions_for_request(response, request_data),
            )
            return await _responses_web_search_bridge_module._resolve_litellm_web_search_function_calls(
                response,
                request_data,
                original_function,
            )
        if (
            not _tools_module._request_has_image_generation_tool(request_data)
            or not _image_generation_module._response_should_trigger_image_generation_fallback(response)
        ):
            return response
        if (
            not _image_generation_module._request_forces_image_generation_tool(request_data)
            and not _image_generation_module._request_already_attempted_streaming_fallback(request_data)
        ):
            fallback_exception = _image_generation_module._image_generation_tool_runtime_fallback_exception()
            _routing_module._mark_exception_for_deployment_failover(fallback_exception, request_data)
            payload = _streaming_module._build_forced_image_generation_payload(request_data, stream=False)
            if payload is not None:
                try:
                    fallback_response = await _streaming_module._call_forced_image_generation_payload(payload)
                except Exception:
                    pass
                else:
                    if not _image_generation_module._response_should_trigger_image_generation_fallback(fallback_response):
                        return fallback_response
                    raise _image_generation_module._image_generation_tool_runtime_fallback_exception()
        raise litellm.InternalServerError(
            message="upstream returned no usable image_generation result while image_generation was available; trying fallback deployment",
            model=_image_generation_module._request_model_for_error(request_data),
            llm_provider="",
        )
        return response

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict,
    ) -> AsyncIterator[Any]:
        def deliver_chunk(chunk: Any) -> Any:
            return _streaming_module._responses_stream_chunk_for_delivery(chunk)

        response = _image_generation_module._sanitize_response_echoed_request_images_for_delivery(response, request_data)
        if (
            not _tools_module._request_has_image_generation_tool(request_data)
            or _image_generation_module._request_forces_image_generation_tool(request_data)
            or _image_generation_module._request_already_attempted_streaming_fallback(request_data)
        ):
            async for chunk in _streaming_module._yield_start_buffered_stream_with_error_fallback(
                response,
                request_data,
            ):
                yield deliver_chunk(chunk)
            return

        buffer: List[Any] = []
        text = ""
        should_fallback = False
        should_passthrough = False

        if not _image_generation_module._response_is_async_iterable(response):
            async for chunk in _streaming_module._non_streaming_response_as_stream(response, request_data):
                yield deliver_chunk(chunk)
            return

        try:
            async for chunk in _streaming_module._stream_with_idle_timeout(response, request_data):
                sanitized_chunk = _responses_web_search_bridge_module._sanitize_web_search_stream_chunk(chunk)
                if sanitized_chunk is None:
                    continue
                chunk = sanitized_chunk
                chunk_exception = _streaming_module._stream_chunk_priority_error_exception(chunk)
                if chunk_exception is not None:
                    async for fallback_chunk in _streaming_module._yield_streaming_error_fallback_or_raise(
                        request_data,
                        chunk_exception,
                    ):
                        yield deliver_chunk(fallback_chunk)
                    return
                buffer.append(chunk)
                if _image_generation_module._response_has_image_generation_activity(chunk):
                    should_passthrough = True
                    break

                chunk_text = _image_generation_module._response_text(chunk)
                if chunk_text:
                    text = f"{text}\n{chunk_text}" if text else chunk_text
                    if _image_generation_module._response_is_image_generation_unavailable_refusal({"output_text": text}):
                        should_fallback = True
                        break
                    if len(text) >= _STREAM_FALLBACK_TEXT_FLUSH_CHARS:
                        should_passthrough = True
                        break
        except Exception as exc:
            async for fallback_chunk in _streaming_module._yield_streaming_error_fallback_or_raise(
                request_data,
                exc,
            ):
                yield deliver_chunk(fallback_chunk)
            return

        if should_passthrough:
            async for chunk in _streaming_module._yield_guarded_original_stream(buffer, response, request_data):
                yield deliver_chunk(chunk)
            return

        if not should_fallback and _image_generation_module._response_should_trigger_image_generation_fallback({"output_text": text}):
            should_fallback = True

        if should_fallback:
            fallback_exception = _image_generation_module._image_generation_tool_runtime_fallback_exception()
            _routing_module._mark_exception_for_deployment_failover(fallback_exception, request_data)
            payload = _streaming_module._build_forced_image_generation_payload(request_data, stream=True)
            if payload is not None:
                yielded_fallback = False
                try:
                    async for chunk in _streaming_module._stream_forced_image_generation_payload(payload):
                        yielded_fallback = True
                        yield deliver_chunk(chunk)
                    if yielded_fallback:
                        return
                except Exception:
                    pass

        async for chunk in _streaming_module._yield_guarded_original_stream(buffer, _streaming_module._empty_async_iterator(), request_data):
            yield deliver_chunk(chunk)
