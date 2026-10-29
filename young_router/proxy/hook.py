from __future__ import annotations

from . import image_generation as _image_generation_module
from . import image_inputs as _image_inputs_module
from . import codex_contract as _codex_contract_module
from . import responses_output as _responses_output_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import codex_fast_tier as _codex_fast_tier_module
from . import responses_execution as _responses_execution_module
from . import responses_surfaces as _responses_surfaces_module
from . import web_search_native as _web_search_native_module
from . import reasoning as _reasoning_module
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
    litellm,
)



class YoungRouterHook(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: Any,
    ) -> Optional[dict]:
        """Shrink a replayed Responses history before LiteLLM counts it.

        ``Router._pre_call_checks`` refuses a request whose token count exceeds
        a deployment's ``model_info.max_input_tokens``, and it runs at
        deployment selection -- before ``async_pre_call_deployment_hook``,
        where oversized replayed tool outputs and image histories are normally
        elided.  The limit it compares against is the same number the Core
        publishes to the client as that model's context budget, which the
        client has already applied to its own window: a conversation the client
        measured as fitting was refused locally, with no upstream contacted at
        all.

        Eliding here is the only ordering in which the count describes the
        request that is actually sent.
        """

        if not isinstance(data, dict):
            return None
        if not _responses_request_module._request_is_responses_api(data):
            return None
        # Record this before any bridge or surface adapter rewrites the input:
        # a Chat-shaped route replays this history as messages and the answer it
        # returns covers the whole restored context.
        _routing_module._remember_restored_reasoning_replay(
            data,
            _responses_execution_module._chat_bridge_reasoning_included(data),
        )
        # Image history is reduced before deployment selection as well as in the
        # deployment hook.  The second pass is intentionally retained because
        # LiteLLM may rebuild request kwargs between those two callback points.
        prepared = _responses_request_module._with_prefix_image_previews(data)
        if prepared is None:
            prepared = data
        elided = _responses_request_module._with_elided_oversized_tool_outputs(
            prepared
        )
        if elided is None:
            return prepared if prepared is not data else None
        # ``pre_call_hook`` replaces the request wholesale, so every field the
        # elision pass did not carry over has to travel with it.
        for key, value in data.items():
            if key not in elided:
                elided[key] = value
        return elided

    def _record_recent_request_started(self, kwargs: Dict[str, Any]) -> None:
        """Publish a bounded progress row before the upstream call begins."""
        record = _routing_module._request_log_record("pending", kwargs)
        # A request id is the join key used when later success/failure/stream
        # callbacks replace this progress row.  Do not emit an unjoinable
        # pending row that would remain as a duplicate legacy request entry.
        if record.get("request_id"):
            if _state_module._append_recent_request(record) is not None:
                # Pin this row on the selected route marker so the stream that
                # carries the attempt can refresh it, and close it if the
                # attempt ends without any terminal callback (client
                # disconnect, failover teardown, worker exit).
                _routing_module._remember_pending_request_log_id(
                    record.get("request_id")
                )
                _state_module._schedule_recent_request_settlement(record)

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        if _routing_module._is_codex_compaction_capability_probe(kwargs):
            return
        _trace_codex_fast_tier_result(kwargs, response_obj, outcome="success")
        streaming = kwargs.get("stream") is True
        completed_stream = (
            "async_complete_streaming_response" in kwargs
            or _streaming_module._responses_stream_chunk_is_completed(response_obj)
        )
        if not streaming:
            _routing_module._record_deployment_success_for_cooldown(kwargs)
        status = "stream" if streaming and not completed_stream else "success"
        _state_module._append_recent_request(
            _routing_module._request_log_record(status, kwargs, response_obj, start_time, end_time)
        )

    async def async_log_failure_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        if _routing_module._is_codex_compaction_capability_probe(kwargs):
            return
        _trace_codex_fast_tier_result(kwargs, response_obj, outcome="failure")
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
        if _routing_module._is_codex_compaction_capability_probe(kwargs):
            return
        failed = _codex_fast_tier_module._response_indicates_failure(response_obj)
        completed = _streaming_module._responses_stream_chunk_is_completed(response_obj)
        if failed:
            _trace_codex_fast_tier_result(kwargs, response_obj, outcome="stream_failure")
        elif completed:
            _trace_codex_fast_tier_result(kwargs, response_obj, outcome="stream_completed")
        if completed:
            _routing_module._record_deployment_success_for_cooldown(kwargs)
        status = "failure" if failed else "success" if completed else "stream"
        _state_module._append_recent_request(
            _routing_module._request_log_record(status, kwargs, response_obj, start_time, end_time)
        )

    async def async_pre_call_deployment_hook(
        self,
        kwargs: Dict[str, Any],
        call_type: Any,
    ) -> Optional[dict]:
        is_compaction_capability_probe = (
            _routing_module._is_codex_compaction_capability_probe(kwargs)
        )
        if not is_compaction_capability_probe:
            _routing_module._record_request_started_time(kwargs)
            self._record_recent_request_started(kwargs)
        modified_kwargs = kwargs
        changed = False
        for update_request in (
            _codex_fast_tier_module._with_codex_fast_default_service_tier,
            _reasoning_module._with_model_reasoning_mapping,
            _reasoning_module._with_model_reasoning_parameter_support,
            _responses_request_module._with_dsh_messages_plaintext_content_restored,
            _responses_request_module._with_plaintext_agent_message_content_restored,
            _responses_request_module._with_codex_function_call_arguments_repaired,
            _responses_request_module._with_neutral_attachment_paths,
            _responses_request_module._with_prefix_image_previews,
            _responses_request_module._with_codex_view_image_output_paths,
            _responses_request_module._with_codex_function_call_output_text,
            _responses_request_module._with_elided_oversized_tool_outputs,
            _image_inputs_module._with_bounded_image_inputs,
            _responses_request_module._with_internal_litellm_metadata,
            _responses_request_module._with_mcp_auto_approval,
            _responses_request_module._with_codex_openrouter_native_web_search_tool,
            *_codex_contract_module.CODEX_CONTRACT_REQUEST_STEPS,
            _responses_request_module._with_empty_tool_controls_removed,
            _responses_request_module._with_codex_compaction_controls,
            _responses_request_module._with_responses_native_extra_body,
            _responses_request_module._with_codex_compaction_headers,
            _responses_request_module._with_streaming_plaintext_headers,
            _responses_request_module._with_stream_request_timeout,
            _responses_request_module._with_incoming_user_agent_header,
            _responses_request_module._with_owned_user_agent_header,
            _responses_request_module._with_browser_compatible_headers,
        ):
            image_budget_before = None
            image_budget_event = None
            if update_request is _image_inputs_module._with_bounded_image_inputs:
                image_budget_before = _image_inputs_module._image_input_budget(modified_kwargs)
                image_budget_event = "image_input_budget"
            elif update_request is _responses_request_module._with_prefix_image_previews:
                image_budget_before = _image_inputs_module._image_input_budget(modified_kwargs)
                image_budget_event = "prefix_image_preview"
            updated_kwargs = update_request(modified_kwargs)
            if image_budget_before is not None:
                _trace_module._route_trace(
                    image_budget_event,
                    request_id=_routing_module._trace_request_id(modified_kwargs),
                    session=_routing_module._trace_session_context(modified_kwargs),
                    changed=updated_kwargs is not None,
                    before=image_budget_before,
                    after=_image_inputs_module._image_input_budget(
                        updated_kwargs if updated_kwargs is not None else modified_kwargs
                    ),
                )
            if updated_kwargs is None:
                continue
            modified_kwargs = updated_kwargs
            changed = True
            if update_request is _codex_fast_tier_module._with_codex_fast_default_service_tier:
                _trace_codex_fast_tier_injected(modified_kwargs)
        native_client_tool_kwargs = (
            _responses_surfaces_module._with_responses_native_client_tool_passthrough(
                modified_kwargs
            )
        )
        if native_client_tool_kwargs is not None:
            modified_kwargs = native_client_tool_kwargs
            changed = True
        if not is_compaction_capability_probe:
            await _routing_module._ensure_codex_compaction_capability(modified_kwargs)
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
            candidate_deployments = _responses_request_module._with_retry_target_constraints(
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
            image_tool_unsupported_deployments: list[dict[str, Any]] = []
            image_tool_unsupported_filtered = False

            if has_image_generation_tool:
                (
                    candidate_deployments,
                    image_tool_unsupported_deployments,
                    image_tool_unsupported_filtered,
                ) = _routing_module._with_active_image_generation_tool_unsupported(
                    candidate_deployments,
                    request_kwargs=request_kwargs,
                )

            if (
                not has_image_generation_tool
                and _image_inputs_module._request_has_image_input(request_kwargs)
                and _responses_request_module._request_is_responses_api(request_kwargs)
            ):
                responses_image_safe = [
                    deployment
                    for deployment in candidate_deployments
                    if _image_inputs_module._deployment_allows_responses_image_input(deployment)
                ]
                responses_image_input_filtered = bool(responses_image_safe)
                candidate_deployments = responses_image_safe or candidate_deployments

            (
                candidate_deployments,
                session_affinity_deployment_id,
                session_affinity_applied,
            ) = _routing_module._with_session_deployment_affinity(
                candidate_deployments,
                request_kwargs,
                model,
            )

            _trace_module._route_trace(
                "filter_deployments",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=model,
                target_order=_responses_request_module._request_target_order(request_kwargs),
                excluded_deployment_ids=sorted(_responses_request_module._request_excluded_deployment_ids(request_kwargs)),
                has_image_generation_tool=has_image_generation_tool,
                has_web_search_tool=_tools_module._request_has_web_search_tool(request_kwargs),
                has_image_input=_image_inputs_module._request_has_image_input(request_kwargs),
                deployment_cooldown_filtered=cooldown_filtered,
                deployment_cooldown_all_candidates=bool(
                    cooldown_deployments and not after_cooldown
                ),
                deployment_cooldown_deployments=cooldown_deployments,
                image_tool_unsupported_filtered=image_tool_unsupported_filtered,
                image_tool_unsupported_all_candidates=bool(
                    image_tool_unsupported_deployments and not candidate_deployments
                ),
                image_tool_unsupported_deployments=image_tool_unsupported_deployments,
                responses_surface_filtered=responses_surface_filtered,
                image_generation_filtered=image_generation_filtered,
                web_search_filtered=web_search_filtered,
                web_search_unsupported_bridge=web_search_unsupported_bridge,
                responses_image_input_filtered=responses_image_input_filtered,
                session_affinity_deployment_id=session_affinity_deployment_id,
                session_affinity_applied=session_affinity_applied,
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
        response = _image_inputs_module._sanitize_response_echoed_request_images_for_delivery(response, request_data)
        _web_search_native_module._mark_provider_native_web_search_event(
            request_data, response
        )
        response = _web_search_native_module._sanitize_response_stream_payload(
            response,
            request_data,
        )
        response = _responses_output_module._sanitize_reasoning_wrapper_response(
            response,
            request_data,
        )
        if (
            not _tools_module._request_has_image_generation_tool(request_data)
            or not _image_generation_module._response_should_trigger_image_generation_fallback(response)
        ):
            return response
        if _image_generation_module._response_is_image_generation_policy_refusal(
            response
        ):
            return response
        if not _image_generation_module._request_forces_image_generation_tool(
            request_data
        ):
            payload = _streaming_module._build_forced_image_generation_payload(request_data, stream=False)
            if payload is None:
                raise RuntimeError(
                    "image_generation forced probe payload could not be built"
                )
            fallback_response = await _streaming_module._call_forced_image_generation_payload(
                payload
            )
            if not _image_generation_module._response_should_trigger_image_generation_fallback(
                fallback_response
            ):
                return fallback_response
            if _image_generation_module._response_is_image_generation_policy_refusal(
                fallback_response
            ):
                return fallback_response
        raise _image_generation_module._image_generation_tool_runtime_fallback_exception(
            capability_unsupported=True,
        )

    async def async_post_call_response_headers_hook(
        self,
        data: dict,
        user_api_key_dict: Any,
        response: Any,
        request_headers: Any = None,
    ) -> dict:
        """Put a fact about the adapted stream on the real HTTP response.

        Codex reads ``x-reasoning-included`` from the streaming HTTP headers
        before the first SSE event and uses it to decide whether its own token
        estimate must add the earlier encrypted reasoning again.  No key inside
        the event body reaches that decision, so a marker written into
        ``response.created`` is invisible to it and a 185k-token context still
        looked like 400k.

        Only an adapted stream carries the fact: it replayed the client's
        context as ordinary messages, so the upstream counted every restored
        token.  A native Responses answer keeps whatever header its own
        upstream sent, and this hook adds nothing.
        """

        restored = _responses_execution_module._restored_reasoning_included(response)
        _trace_module._route_trace(
            "responses_reasoning_included_header",
            request_id=_routing_module._trace_request_id(data),
            session=_routing_module._trace_session_context(data),
            model_group=_responses_execution_module._request_model_group(data),
            applied=restored,
            response_type=type(response).__name__,
        )
        if not restored:
            return {}
        return {_responses_execution_module.REASONING_INCLUDED_HEADER: "true"}

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict,
    ) -> AsyncIterator[Any]:
        # The keepalive heartbeat wraps the complete delivery pipeline (start
        # buffering, guarded delivery, fallback and recovery rounds), so the
        # downstream client keeps receiving bytes while any internal wait is
        # in progress. Keepalive chunks intentionally bypass deliver_chunk():
        # they are already in final wire form and carry no output to adapt.
        async for chunk in _streaming_module._yield_downstream_keepalive_stream(
            self._post_call_streaming_iterator_pipeline(
                user_api_key_dict,
                response,
                request_data,
            ),
            request_data,
        ):
            # A dropped chunk is not a frame.  ``deliver_chunk`` answers
            # ``None`` for an event this client must not see (a reasoning
            # summary it never asked for), and a ``None`` that reaches
            # LiteLLM's serializer is written as the literal ``data: None`` --
            # a body every Responses client rejects as malformed JSON, which is
            # how a turn being recovered was cut by the frames meant to keep it
            # alive.  Every delivery path below funnels through here, so the
            # drop is applied once, at the wire boundary that would otherwise
            # invent a payload for it.
            if chunk is None:
                continue
            yield chunk

    async def _post_call_streaming_iterator_pipeline(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict,
    ) -> AsyncIterator[Any]:
        selected_marker = _routing_module._selected_deployment_marker_from_response(
            response
        )
        if selected_marker is not None:
            _routing_module._apply_selected_deployment_marker_to_request(
                request_data,
                selected_marker,
            )

        def deliver_chunk(chunk: Any) -> Any:
            return _streaming_module._responses_stream_chunk_for_delivery(
                chunk,
                request_data,
            )

        emit_image_generation_saved_paths = (
            _responses_request_module._request_has_codex_client_evidence(request_data)
            or (
                _responses_request_module._request_is_responses_api(request_data)
                and _image_generation_module._image_generation_session_component(request_data)
                != "unknown-session"
            )
        )
        image_generation_saved_paths: list[str] = []

        async def deliver_stream(stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
            async for stream_chunk in stream:
                for image_chunk in image_generation_chunks_for_delivery(stream_chunk):
                    yield deliver_chunk(image_chunk)

        def image_generation_chunks_for_delivery(stream_chunk: Any) -> list[Any]:
            if not emit_image_generation_saved_paths:
                return [stream_chunk]
            return _image_generation_module._image_generation_stream_chunks_for_delivery(
                stream_chunk,
                request_data,
                image_generation_saved_paths,
            )

        response = _image_inputs_module._sanitize_response_echoed_request_images_for_delivery(response, request_data)
        if not _responses_output_module._response_is_async_iterable(response):
            _web_search_native_module._mark_provider_native_web_search_event(
                request_data, response
            )
        if (
            _tools_module._request_has_web_search_tool(request_data)
            and _responses_output_module._response_is_async_iterable(response)
        ):
            response = _web_search_native_module._adapt_provider_hidden_web_search_stream(
                response,
                request_data,
            )
        if (
            not _tools_module._request_has_image_generation_tool(request_data)
            or (
                _image_generation_module._request_forces_image_generation_tool(
                    request_data
                )
                and _responses_request_module._request_already_attempted_streaming_fallback(
                    request_data
                )
            )
        ):
            async for chunk in deliver_stream(_streaming_module._yield_start_buffered_stream_with_error_fallback(
                response,
                request_data,
            )):
                yield chunk
            return

        buffer: List[Any] = []
        text = ""
        should_fallback = False
        should_passthrough = False

        if not _responses_output_module._response_is_async_iterable(response):
            async for chunk in deliver_stream(
                _streaming_module._non_streaming_response_as_stream(response, request_data)
            ):
                yield chunk
            return

        try:
            async for chunk in _streaming_module._stream_with_idle_timeout(response, request_data):
                _web_search_native_module._mark_provider_native_web_search_event(
                    request_data, chunk
                )
                sanitized_chunk = _web_search_native_module._sanitize_web_search_stream_chunk(
                    chunk,
                    request_data,
                )
                if sanitized_chunk is None:
                    continue
                chunk = sanitized_chunk
                chunk_exception = _streaming_module._stream_chunk_priority_error_exception(chunk)
                if chunk_exception is not None:
                    async for fallback_chunk in _streaming_module._yield_streaming_error_fallback_or_raise(
                        request_data,
                        chunk_exception,
                    ):
                        for delivered_chunk in image_generation_chunks_for_delivery(fallback_chunk):
                            yield deliver_chunk(delivered_chunk)
                    return
                buffer.append(chunk)
                if _image_generation_module._response_has_image_generation_activity(chunk):
                    should_passthrough = True
                    break

                chunk_text = _responses_output_module._response_text(chunk)
                if chunk_text:
                    text = f"{text}\n{chunk_text}" if text else chunk_text
                    if _image_generation_module._response_is_image_generation_policy_refusal(
                        {"output_text": text}
                    ):
                        should_passthrough = True
                        break
                    if _image_generation_module._response_is_image_generation_unavailable_refusal({"output_text": text}):
                        should_fallback = True
                        break
        except Exception as exc:
            async for fallback_chunk in _streaming_module._yield_streaming_error_fallback_or_raise(
                request_data,
                exc,
            ):
                for delivered_chunk in image_generation_chunks_for_delivery(fallback_chunk):
                    yield deliver_chunk(delivered_chunk)
            return

        if should_passthrough:
            async for chunk in deliver_stream(
                _streaming_module._yield_guarded_original_stream(buffer, response, request_data)
            ):
                yield chunk
            return

        if not should_fallback and _image_generation_module._response_should_trigger_image_generation_fallback({"output_text": text}):
            should_fallback = True

        if should_fallback:
            if _image_generation_module._request_forces_image_generation_tool(
                request_data
            ):
                fallback_exception = (
                    _image_generation_module._image_generation_tool_runtime_fallback_exception(
                        capability_unsupported=True,
                    )
                )
                async for fallback_chunk in _streaming_module._yield_streaming_error_fallback_or_raise(
                    request_data,
                    fallback_exception,
                ):
                    for delivered_chunk in image_generation_chunks_for_delivery(fallback_chunk):
                        yield deliver_chunk(delivered_chunk)
                return

            payload = _streaming_module._build_forced_image_generation_payload(
                request_data,
                stream=True,
            )
            if payload is None:
                raise RuntimeError(
                    "image_generation forced probe payload could not be built"
                )
            try:
                async for chunk in deliver_stream(
                    _streaming_module._stream_forced_image_generation_payload(payload)
                ):
                    yield chunk
                return
            except Exception as exc:
                async for fallback_chunk in _streaming_module._yield_streaming_error_fallback_or_raise(
                    request_data,
                    exc,
                ):
                    for delivered_chunk in image_generation_chunks_for_delivery(fallback_chunk):
                        yield deliver_chunk(delivered_chunk)
                return

        async for chunk in deliver_stream(
            _streaming_module._yield_guarded_original_stream(
                buffer, _streaming_module._empty_async_iterator(), request_data
            )
        ):
            yield chunk


def _trace_codex_fast_tier_result(
    request_kwargs: dict,
    response_obj: Any,
    *,
    outcome: str,
) -> None:
    """Record the requested/final tier without serializing request credentials."""

    injected_tier = _codex_fast_tier_module._codex_fast_default_service_tier(
        request_kwargs
    )
    if injected_tier is None:
        return
    upstream_tier = _codex_fast_tier_module._response_service_tier(response_obj)
    _trace_module._route_trace(
        "codex_fast_default_service_tier_result",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        outcome=outcome,
        codex_fast_default_injected=True,
        service_tier=injected_tier,
        requested_service_tier=injected_tier,
        upstream_service_tier=upstream_tier,
        final_service_tier=upstream_tier,
        final_request_service_tier=_codex_fast_tier_module._request_service_tier(
            request_kwargs
        ),
        final_tier_confirmed=upstream_tier is not None,
        upstream_tier_reported=upstream_tier is not None,
        upstream_priority_confirmed=upstream_tier == injected_tier,
        upstream_tier_differs=(
            upstream_tier is not None and upstream_tier != injected_tier
        ),
        priority_rejected_or_unconfirmed=(
            outcome in {"failure", "stream_failure"}
            or upstream_tier != injected_tier
        ),
    )


def _trace_codex_fast_tier_injected(request_kwargs: dict) -> None:
    _trace_module._route_trace(
        "codex_fast_default_service_tier_injected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        codex_fast_default_injected=True,
        service_tier=_codex_fast_tier_module._codex_fast_default_service_tier(
            request_kwargs
        ),
        requested_service_tier=_codex_fast_tier_module._codex_fast_default_service_tier(
            request_kwargs
        ),
        source="codex_config_fast_default",
    )
