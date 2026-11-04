from __future__ import annotations

from .. import claude_aliases as _claude_aliases_module
from . import image_generation as _image_generation_module
from . import image_context as _image_context_module
from . import image_inputs as _image_inputs_module
from . import codex_contract as _codex_contract_module
from . import context_window as _context_window_module
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
from . import patches as _patches_module
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
    _without_internal_request_keys,
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
        """Record what the replay carries before LiteLLM counts it.

        Nothing is rewritten here: the replay reaches the upstream exactly as
        the client sent it, and the image-context pass that decides which
        images go out runs in ``async_pre_call_deployment_hook`` together with
        every other request adaptation.
        """

        if not isinstance(data, dict):
            return None
        # Claude Desktop's picker can only offer ids that read as an Anthropic
        # model, so this app hands it a derived alias and keeps the real public
        # name behind it (``claude_aliases``).  This hook is the only place that
        # runs before deployment selection, so the alias has to become the
        # public name here or the router refuses it as an unknown model.
        _claude_aliases_module.rewrite_request_model(data)
        # This hook runs once per client turn, before any deployment is chosen,
        # so it is where the turn gets the identifier its hops share.  A model's
        # concurrency cap is counted per turn: the hop that advances to a peer
        # takes the peer's slot and gives the first route's back, never holding
        # two for one request.
        _routing_module._ensure_attempt_lease_id(data)
        if not _responses_request_module._request_is_responses_api(data):
            return None
        # Record this before any bridge or surface adapter rewrites the input:
        # a Chat-shaped route replays this history as messages and the answer it
        # returns covers the whole restored context.
        _routing_module._remember_restored_reasoning_replay(
            data,
            _responses_execution_module._chat_bridge_reasoning_included(data),
        )
        # This hook is the only place that runs before deployment selection, and
        # deployment selection is what refuses an oversized replay outright
        # ("no models have context window large enough for this call").
        # Shortening has to happen here or not at all: afterwards the refusal
        # exists and Codex has already told the user to start a new thread.
        fitted = _context_window_module.apply(data)
        if fitted is not None:
            return fitted
        return None

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
        # Every turn ends here, whichever shape carried it -- a streaming body
        # that finished, a non-streaming answer, a Responses call LiteLLM serves
        # as a fake stream.  The slot this turn claimed is not in flight any
        # more; without this it stayed counted until the lease aged out (two
        # hours), and enough of them parked the whole route.  Releasing is a
        # no-op for a turn that holds nothing (a listing, a health read).
        _routing_module._release_request_concurrency_slots(kwargs)
        # A model-list or health read is not a turn: it selects no route, so the
        # row it would add has no model, cost, or usage.  See
        # ``_is_local_control_plane_read``.
        if _routing_module._is_local_control_plane_read(kwargs):
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
        # A failed turn has no attempt in flight either; its slot goes back the
        # moment the failure is known, not when the lease expires.
        _routing_module._release_request_concurrency_slots(kwargs)
        # A listing or health read that failed never became a turn.  Left
        # logged, every ``/v1/models`` read a client or this app's own
        # client-model loop made while a worker was still loading its key became
        # a 本机网关拒绝密钥 row -- hundreds of them, all reading as failures of
        # requests nobody made.  A key a client cannot use still shows on the
        # turn it sends.
        if _routing_module._is_local_control_plane_read(kwargs):
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
        if _routing_module._is_local_control_plane_read(kwargs):
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
            _responses_request_module._with_codex_function_call_output_text,
            *(_image_context_module.CODEX_IMAGE_CONTEXT_REQUEST_STEPS),
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
            if update_request is _image_context_module._with_codex_image_context:
                image_budget_before = _image_inputs_module._image_input_budget(modified_kwargs)
                image_budget_event = "codex_image_context"
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
        # Last stop before the provider: this proxy's own routing keys must not
        # reach a payload validator.  The Anthropic adapter rejects unknown
        # fields outright, and these ride along on exactly the fallback path
        # that most needs to keep working.
        stripped_kwargs = _without_internal_request_keys(modified_kwargs)
        if stripped_kwargs is not None:
            return stripped_kwargs
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
            # A route at its concurrency cap can serve nothing, so it leaves the
            # candidate pool exactly as a cooled route does.  The cap is per
            # route, and the group is only narrowed while a free route remains:
            # a group whose every route is full keeps its pool, because a single
            # route's model group has nowhere else to go -- that attempt is told
            # its limit was reached after the request is routed, and another hop
            # is never invented for it.
            concurrency_saturated: list[dict[str, Any]] = []
            concurrency_filtered = False
            if candidate_deployments:
                (
                    concurrency_available,
                    concurrency_saturated,
                    concurrency_filtered,
                ) = _routing_module._with_active_model_concurrency(
                    candidate_deployments,
                    request_kwargs=request_kwargs,
                )
                if concurrency_available:
                    candidate_deployments = concurrency_available
                else:
                    concurrency_filtered = False

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
                concurrency_filtered=concurrency_filtered,
                concurrency_all_candidates=bool(
                    concurrency_saturated and not concurrency_filtered
                ),
                concurrency_saturated=concurrency_saturated,
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
            # The turn is over, so the slot it claimed is not in flight.  Without
            # this a successful turn held its route's slot until the lease aged
            # out -- two hours of "busy" per turn -- and eight of them parked the
            # whole route: observed live 2026-11-04, two probe turns left two
            # leases behind and every route of the group read as saturated.
            _routing_module._release_request_concurrency_slots(request_data)
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
        needs_terminal_delivery = (
            _streaming_module._responses_stream_requires_terminal_delivery(
                request_data
            )
        )
        terminal_delivered = False
        # The keepalive heartbeat wraps the complete delivery pipeline (start
        # buffering, guarded delivery, fallback and recovery rounds), so the
        # downstream client keeps receiving bytes while any internal wait is
        # in progress. Keepalive chunks intentionally bypass deliver_chunk():
        # they are already in final wire form and carry no output to adapt.
        try:
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
                if needs_terminal_delivery and (
                    _streaming_module._responses_stream_chunk_state_terminal(chunk)
                ):
                    terminal_delivered = True
                yield chunk
        except Exception as exc:
            # The reader has nothing to read the end of this body from: this
            # shape ends a turn at its terminal event, so a body that closes
            # without one is a turn left open -- the client waits until a person
            # presses stop, and the answer that never came is reported as a
            # stream that "ended before a terminal response event".  Every
            # internal path already tries to deliver a classified stop; this is
            # the wire boundary that guarantees it when one of them ends by
            # raising instead.
            if (
                not needs_terminal_delivery
                or terminal_delivered
                or _routing_module._is_local_shutdown_exception(exc)
                or not _streaming_module._stream_failure_ended_without_terminal(exc)
            ):
                raise
            terminal_delivered = True
            terminal_event = _streaming_module._synthesized_failed_response_event(
                request_data,
                exc,
            )
            _trace_module._request_route_trace(
                "responses_stream_terminal_event_synthesized",
                request_data,
                reason="pipeline_exception",
                exception=_routing_module._trace_exception(exc),
                request=_trace_module._trace_request_summary(request_data),
            )
            yield terminal_event
        finally:
            # This generator ending is the turn ending, whatever ended it: the
            # answer completed, a terminal error frame was delivered, the client
            # stopped reading, or this worker was torn down mid-answer.  A slot
            # whose turn is over is not a request in flight, and the lease only
            # ages out after the whole request budget -- two hours of a route
            # counted as busy with nobody behind it.  Enough of those park a
            # whole model group at its cap.
            _routing_module._release_request_concurrency_slots(request_data)

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
        # Which image calls this response already told the client it opened.  One
        # response's frames share it, so a route that streams a real opening
        # frame is left alone and only the done-only completion is repaired.
        image_generation_opened_item_ids: set[str] = set()

        reasoning_summary_texts: dict[str, str] = {}
        reasoning_summary_seen: set[str] = set()
        reasoning_summary_done: set[str] = set()

        async def deliver_stream(stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
            async for stream_chunk in stream:
                suppress_reasoning_text = (
                    _patches_module.reasoning_summary_suppresses_chunk(
                        stream_chunk,
                        request_data,
                    )
                )
                for summary_chunk in _patches_module.reasoning_summary_chunks_for_delivery(
                    stream_chunk,
                    reasoning_summary_texts,
                    reasoning_summary_seen,
                    reasoning_summary_done,
                    request_data,
                ):
                    yield deliver_chunk(summary_chunk)
                if suppress_reasoning_text:
                    continue
                for image_chunk in image_generation_chunks_for_delivery(stream_chunk):
                    yield deliver_chunk(image_chunk)

        def image_generation_chunks_for_delivery(stream_chunk: Any) -> list[Any]:
            return _image_generation_module._image_generation_stream_chunks_for_delivery(
                stream_chunk,
                request_data,
                image_generation_saved_paths,
                image_generation_opened_item_ids,
                attach_paths=emit_image_generation_saved_paths,
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
