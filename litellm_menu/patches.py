from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_execution as _responses_execution_module
from . import responses_output as _responses_output_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import trace as _trace_module
from . import vision_bridge as _vision_bridge_module


from .base import (
    Any,
    List,
    Optional,
    _BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_SELECTED_DEPLOYMENT,
    _GENERIC_HELPER_PATCH_ATTR,
    _ORDER_PEER_FAILOVER_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_PATCH_ATTR,
    _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
    _ROUTING_CONSTRAINT_PATCH_ATTR,
    _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
    _normalize_response_completed_event_usage,
)


def _browser_compatible_headers_retry_kwargs(
    request_kwargs: dict,
) -> Optional[dict]:
    retry_kwargs = _image_generation_module._with_browser_compatible_headers_retry(request_kwargs)
    if retry_kwargs is None:
        return None
    header_kwargs = _image_generation_module._with_browser_compatible_headers(retry_kwargs)
    return header_kwargs or retry_kwargs


def _browser_compatible_headers_retry_entry(
    model_group: Optional[str],
    exception: Exception,
    request_kwargs: dict,
) -> Optional[dict]:
    model = _responses_execution_module._request_model_group(request_kwargs) or model_group
    if not isinstance(model, str) or not model.strip():
        return None
    entry: dict[str, Any] = {"model": model}
    target_order = request_kwargs.get("_target_order")
    if target_order is None:
        target_order = _responses_execution_module._failed_deployment_order(exception)
    if target_order is not None:
        entry["_target_order"] = target_order
    excluded_ids = sorted(_image_generation_module._request_excluded_deployment_ids(request_kwargs))
    if excluded_ids:
        entry["_excluded_deployment_ids"] = excluded_ids
    return entry



def _request_kwargs_from_positional_call(
    args: tuple,
    kwargs: dict,
    *,
    positional_index: int,
) -> Optional[dict]:
    request_kwargs = kwargs.get("request_kwargs")
    if isinstance(request_kwargs, dict):
        return request_kwargs
    if len(args) > positional_index and isinstance(args[positional_index], dict):
        return args[positional_index]
    return None


def _request_is_external_web_search_internal_call(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = request_kwargs.get(metadata_key)
        if not isinstance(metadata, dict):
            continue
        if (
            metadata.get("external_web_search_continuation") is True
            or metadata.get("external_web_search_synthesis") is True
        ):
            return True
    return False


def _install_routing_constraint_patch() -> None:
    try:
        from litellm.router import Router
    except Exception:
        return

    original_get_all_deployments = getattr(Router, "_get_all_deployments", None)
    if original_get_all_deployments is not None and not getattr(
        original_get_all_deployments,
        _ROUTING_CONSTRAINT_PATCH_ATTR,
        False,
    ):

        def patched_get_all_deployments(self: Any, *args: Any, **kwargs: Any) -> Any:
            deployments = original_get_all_deployments(self, *args, **kwargs)
            if not isinstance(deployments, list):
                return deployments
            constrained = deployments
            excluded_ids = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.get()
            if excluded_ids:
                constrained = [
                    deployment
                    for deployment in constrained
                    if _image_generation_module._deployment_id(deployment) not in excluded_ids
                ]
            constrained, _cooldown_deployments, _cooldown_filtered = (
                _routing_module._with_active_deployment_cooldowns(constrained)
            )
            return constrained

        setattr(patched_get_all_deployments, _ROUTING_CONSTRAINT_PATCH_ATTR, True)
        setattr(
            patched_get_all_deployments,
            "_original_get_all_deployments",
            original_get_all_deployments,
        )
        Router._get_all_deployments = patched_get_all_deployments

    original_get_available_deployment = getattr(Router, "get_available_deployment", None)
    if original_get_available_deployment is not None and not getattr(
        original_get_available_deployment,
        _ROUTING_CONSTRAINT_PATCH_ATTR,
        False,
    ):

        def patched_get_available_deployment(self: Any, *args: Any, **kwargs: Any) -> Any:
            request_kwargs = _request_kwargs_from_positional_call(
                args,
                kwargs,
                positional_index=4,
            )
            excluded_ids = _image_generation_module._request_excluded_deployment_ids(request_kwargs)
            token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids or None)
            try:
                return original_get_available_deployment(self, *args, **kwargs)
            finally:
                _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

        setattr(patched_get_available_deployment, _ROUTING_CONSTRAINT_PATCH_ATTR, True)
        setattr(
            patched_get_available_deployment,
            "_original_get_available_deployment",
            original_get_available_deployment,
        )
        Router.get_available_deployment = patched_get_available_deployment

    original_async_get_available_deployment = getattr(
        Router,
        "async_get_available_deployment",
        None,
    )
    if original_async_get_available_deployment is None or getattr(
        original_async_get_available_deployment,
        _ROUTING_CONSTRAINT_PATCH_ATTR,
        False,
    ):
        return

    async def patched_async_get_available_deployment(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        request_kwargs = _request_kwargs_from_positional_call(
            args,
            kwargs,
            positional_index=1,
        )
        excluded_ids = _image_generation_module._request_excluded_deployment_ids(request_kwargs)
        token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids or None)
        try:
            return await original_async_get_available_deployment(self, *args, **kwargs)
        finally:
            _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

    setattr(patched_async_get_available_deployment, _ROUTING_CONSTRAINT_PATCH_ATTR, True)
    setattr(
        patched_async_get_available_deployment,
        "_original_async_get_available_deployment",
        original_async_get_available_deployment,
    )
    Router.async_get_available_deployment = patched_async_get_available_deployment


def _install_selected_deployment_marker_patch() -> None:
    try:
        from litellm.router import Router
    except Exception:
        return

    original_update_kwargs_with_deployment = getattr(
        Router,
        "_update_kwargs_with_deployment",
        None,
    )
    if original_update_kwargs_with_deployment is not None and not getattr(
        original_update_kwargs_with_deployment,
        _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
        False,
    ):

        def patched_update_kwargs_with_deployment(
            self: Any,
            deployment: dict,
            kwargs: dict,
            function_name: Optional[str] = None,
        ) -> None:
            _responses_execution_module._remember_request_model_group_before_deployment_update(kwargs)
            _routing_module._remember_selected_deployment(deployment)
            force_browser_headers = _image_generation_module._request_forces_browser_compatible_headers(kwargs)
            _trace_module._route_trace(
                "selected_deployment",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_responses_execution_module._request_model_group(kwargs),
                function_name=function_name,
                deployment=_routing_module._trace_deployment(deployment),
                request=_trace_module._trace_request_summary(
                    kwargs,
                    call_type=function_name,
                    method_name=function_name,
                ),
                target_order=_image_generation_module._request_target_order(kwargs),
                excluded_deployment_ids=sorted(_image_generation_module._request_excluded_deployment_ids(kwargs)),
            )
            result = original_update_kwargs_with_deployment(
                self,
                deployment,
                kwargs,
                function_name=function_name,
            )
            _routing_module._remember_selected_deployment_for_request(kwargs, deployment)
            if force_browser_headers:
                kwargs[_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True

            from .image_generation import _with_browser_compatible_headers

            browser_kwargs = _with_browser_compatible_headers(kwargs)
            if browser_kwargs is not None:
                kwargs.clear()
                kwargs.update(browser_kwargs)
            return result

        setattr(
            patched_update_kwargs_with_deployment,
            _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
            True,
        )
        setattr(
            patched_update_kwargs_with_deployment,
            "_original_update_kwargs_with_deployment",
            original_update_kwargs_with_deployment,
        )
        Router._update_kwargs_with_deployment = patched_update_kwargs_with_deployment

    original_make_call = getattr(Router, "make_call", None)
    if original_make_call is None or getattr(
        original_make_call,
        _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
        False,
    ):
        return

    async def patched_make_call(self: Any, original_function: Any, *args: Any, **kwargs: Any) -> Any:
        token = _CURRENT_SELECTED_DEPLOYMENT.set(None)
        try:
            return await original_make_call(self, original_function, *args, **kwargs)
        except Exception as exc:
            marker = _CURRENT_SELECTED_DEPLOYMENT.get()
            if (
                marker is not None
                and _routing_module._is_priority_deployment_failover_error(exc)
                and not _routing_module._should_retry_with_browser_compatible_headers(exc, marker)
            ):
                _routing_module._mark_exception_for_deployment_failover(exc, marker)
            raise
        finally:
            _CURRENT_SELECTED_DEPLOYMENT.reset(token)

    setattr(patched_make_call, _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR, True)
    setattr(patched_make_call, "_original_make_call", original_make_call)
    Router.make_call = patched_make_call


def _install_order_peer_failover_patch() -> None:
    try:
        from litellm.router import Router
        from litellm.router_utils.fallback_event_handlers import run_async_fallback
    except Exception:
        return

    original_common_utils = getattr(Router, "async_function_with_fallbacks_common_utils", None)
    if original_common_utils is None or getattr(
        original_common_utils,
        _ORDER_PEER_FAILOVER_PATCH_ATTR,
        False,
    ):
        return

    async def patched_common_utils(
        self: Any,
        e: Exception,
        disable_fallbacks: Optional[bool],
        fallbacks: Optional[List],
        context_window_fallbacks: Optional[List],
        content_policy_fallbacks: Optional[List],
        model_group: Optional[str],
        args: tuple,
        kwargs: dict,
    ) -> Any:
        if _routing_module._is_terminal_prompt_or_policy_error(e):
            _trace_module._route_trace(
                "terminal_error_fallback_suppressed",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_responses_execution_module._request_model_group(kwargs)
                or model_group,
                request=_trace_module._trace_request_summary(kwargs),
                exception=_routing_module._trace_exception(e),
            )
            raise e
        browser_retry_kwargs = None
        if _routing_module._should_retry_with_browser_compatible_headers(e, kwargs):
            browser_retry_kwargs = _browser_compatible_headers_retry_kwargs(kwargs)
        if browser_retry_kwargs is not None and disable_fallbacks is not True:
            browser_retry_entry = _browser_compatible_headers_retry_entry(
                model_group,
                e,
                browser_retry_kwargs,
            )
            if browser_retry_entry is not None:
                peer_kwargs = {
                    "litellm_router": self,
                    "original_exception": e,
                    **browser_retry_kwargs,
                }
                peer_kwargs.setdefault("max_fallbacks", getattr(self, "max_fallbacks", 0))
                peer_kwargs.setdefault("fallback_depth", 0)
                peer_kwargs.update(
                    {
                        "fallback_model_group": [browser_retry_entry],
                        "original_model_group": _responses_execution_module._request_model_group(kwargs)
                        or model_group,
                    }
                )
                _trace_module._route_trace(
                    "browser_compatible_headers_retry_start",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(kwargs)
                    or model_group,
                    peer_entry=browser_retry_entry,
                    request=_trace_module._trace_request_summary(browser_retry_kwargs),
                    exception=_routing_module._trace_exception(e),
                )
                return await run_async_fallback(*args, **peer_kwargs)
        if (
            _routing_module._is_priority_deployment_failover_error(e)
            and not _routing_module._should_retry_same_deployment_before_fallback(e)
        ):
            _routing_module._mark_exception_for_deployment_failover(e, kwargs)
        _routing_module._sync_failed_deployment_exclusions(kwargs, e)
        if disable_fallbacks is not True:
            peer_entry = _responses_execution_module._ordered_deployment_fallback_entry(self, e, kwargs)
            if peer_entry is not None:
                peer_kwargs = {
                    "litellm_router": self,
                    "original_exception": e,
                    **kwargs,
                }
                peer_kwargs.setdefault("max_fallbacks", getattr(self, "max_fallbacks", 0))
                peer_kwargs.setdefault("fallback_depth", 0)
                peer_kwargs.update(
                    {
                        "fallback_model_group": [peer_entry],
                        "original_model_group": _responses_execution_module._request_model_group(kwargs) or model_group,
                    }
                )
                _trace_module._route_trace(
                    (
                        "same_order_peer_fallback_start"
                        if peer_entry.get("_target_order") == _responses_execution_module._failed_deployment_order(e)
                        else "next_order_fallback_start"
                    ),
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(kwargs) or model_group,
                    peer_entry=peer_entry,
                    request=_trace_module._trace_request_summary(kwargs),
                    exception=_routing_module._trace_exception(e),
                )
                return await run_async_fallback(*args, **peer_kwargs)

        if _routing_module._is_priority_deployment_failover_error(e):
            _trace_module._route_trace(
                "ordered_deployment_fallback_exhausted",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_responses_execution_module._request_model_group(kwargs)
                or model_group,
                target_order=kwargs.get("_target_order"),
                excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                request=_trace_module._trace_request_summary(kwargs),
                exception=_routing_module._trace_exception(e),
            )
            if _routing_module._should_sanitize_final_upstream_route_error(e):
                _routing_module._raise_sanitized_upstream_route_failure(
                    _responses_execution_module._request_model_group(kwargs) or model_group,
                    e,
                    kwargs,
                )
            raise e

        _trace_module._route_trace(
            "litellm_fallback_common_utils",
            request_id=_routing_module._trace_request_id(kwargs),
            session=_routing_module._trace_session_context(kwargs),
            model_group=_responses_execution_module._request_model_group(kwargs) or model_group,
            target_order=kwargs.get("_target_order"),
            excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
            request=_trace_module._trace_request_summary(kwargs),
            exception=_routing_module._trace_exception(e),
        )
        return await original_common_utils(
            self,
            e,
            disable_fallbacks,
            fallbacks,
            context_window_fallbacks,
            content_policy_fallbacks,
            model_group,
            args,
            kwargs,
        )

    setattr(patched_common_utils, _ORDER_PEER_FAILOVER_PATCH_ATTR, True)
    setattr(patched_common_utils, "_original_common_utils", original_common_utils)
    Router.async_function_with_fallbacks_common_utils = patched_common_utils


def _install_generic_deployment_failover_patch() -> None:
    try:
        from litellm.router import Router
    except Exception:
        return

    original_helper = getattr(Router, "_ageneric_api_call_with_fallbacks_helper", None)
    if original_helper is None or getattr(original_helper, _GENERIC_HELPER_PATCH_ATTR, False):
        return

    async def patched_generic_helper(
        self: Any,
        model: str,
        original_generic_function: Any,
        **kwargs: Any,
    ) -> Any:
        for update_request in (
            _image_generation_module._with_empty_tool_controls_removed,
            _image_generation_module._with_codex_compaction_controls,
            _image_generation_module._with_responses_native_extra_body,
            _image_generation_module._with_codex_compaction_headers,
        ):
            updated_kwargs = update_request(kwargs)
            if updated_kwargs is not None:
                kwargs = updated_kwargs
        target_order = kwargs.get("_target_order")
        excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
        external_web_search_internal = _request_is_external_web_search_internal_call(kwargs)
        max_retries = 0 if external_web_search_internal else _routing_module._stream_route_exhaustion_retries()
        retry_delay_seconds = _routing_module._stream_route_exhaustion_retry_delay_seconds()
        retry_attempt = 0
        _trace_module._route_trace(
            "generic_fallback_helper_start",
            request_id=_routing_module._trace_request_id(kwargs),
            session=_routing_module._trace_session_context(kwargs),
            model_group=model,
            target_order=target_order,
            excluded_deployment_ids=excluded_deployment_ids,
            request=_trace_module._trace_request_summary(
                kwargs,
                method_name=_trace_module._trace_function_name(original_generic_function),
            ),
        )
        while True:
            try:
                return await original_helper(
                    self,
                    model,
                    _responses_execution_module._wrap_generic_function_for_deployment_failover(
                        original_generic_function,
                        outer_request_kwargs=kwargs,
                    ),
                    **kwargs,
                )
            except Exception as exc:
                _routing_module._mark_no_deployments_for_order_exhaustion(exc, kwargs)
                browser_retry_kwargs = None
                if _routing_module._should_retry_with_browser_compatible_headers(exc, kwargs):
                    browser_retry_kwargs = _browser_compatible_headers_retry_kwargs(kwargs)
                if browser_retry_kwargs is not None:
                    kwargs = browser_retry_kwargs
                    target_order = kwargs.get("_target_order")
                    excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                    retry_attempt = 0
                    _trace_module._route_trace(
                        "browser_compatible_headers_retry_start",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=excluded_deployment_ids,
                        request=_trace_module._trace_request_summary(
                            kwargs,
                            method_name=_trace_module._trace_function_name(original_generic_function),
                        ),
                        exception=_routing_module._trace_exception(exc),
                    )
                    continue
                if _routing_module._is_priority_deployment_failover_error(exc):
                    _routing_module._mark_exception_for_deployment_failover(exc, kwargs)
                _routing_module._sync_failed_deployment_exclusions(
                    kwargs,
                    exc,
                    deployment_id=_responses_execution_module._failed_deployment_id(exc),
                )
                _trace_module._route_trace(
                    "generic_fallback_helper_error",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=model,
                    target_order=target_order,
                    excluded_deployment_ids=excluded_deployment_ids,
                    retry_attempt=retry_attempt,
                    request=_trace_module._trace_request_summary(
                        kwargs,
                        method_name=_trace_module._trace_function_name(original_generic_function),
                    ),
                    exception=_routing_module._trace_exception(exc),
                )
                if _vision_bridge_module.should_attempt_vision_bridge(exc, kwargs):
                    _trace_module._route_trace(
                        "vision_bridge_fallback_start",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        request=_trace_module._trace_request_summary(
                            kwargs,
                            method_name=_trace_module._trace_function_name(original_generic_function),
                        ),
                        exception=_routing_module._trace_exception(exc),
                    )
                    try:
                        bridged_kwargs = await _vision_bridge_module.bridged_request_kwargs(kwargs)
                        if bridged_kwargs is None:
                            raise RuntimeError("vision bridge could not extract image references")
                        bridged_kwargs.pop("model", None)
                        kwargs = bridged_kwargs
                        target_order = kwargs.get("_target_order")
                        excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                        retry_attempt = 0
                        _trace_module._route_trace(
                            "vision_bridge_fallback_retry_start",
                            request_id=_routing_module._trace_request_id(kwargs),
                            session=_routing_module._trace_session_context(kwargs),
                            model_group=model,
                            target_order=target_order,
                            excluded_deployment_ids=excluded_deployment_ids,
                            request=_trace_module._trace_request_summary(
                                kwargs,
                                method_name=_trace_module._trace_function_name(original_generic_function),
                            ),
                        )
                        continue
                    except Exception as bridge_exc:
                        _trace_module._route_trace(
                            "vision_bridge_fallback_error",
                            request_id=_routing_module._trace_request_id(kwargs),
                            session=_routing_module._trace_session_context(kwargs),
                            model_group=model,
                            original_exception=_routing_module._trace_exception(exc),
                            exception=_routing_module._trace_exception(bridge_exc),
                        )
                _responses_execution_module._restore_routing_constraints(
                    kwargs,
                    target_order=target_order,
                    excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                )
                if (
                    retry_attempt < max_retries
                    and _routing_module._should_retry_final_upstream_route_error(exc, kwargs)
                ):
                    retry_attempt += 1
                    await _routing_module._sleep_before_final_route_retry(
                        model,
                        exc,
                        kwargs,
                        attempt=retry_attempt,
                        max_retries=max_retries,
                        configured_delay_seconds=retry_delay_seconds,
                    )
                    _responses_execution_module._restore_routing_constraints(
                        kwargs,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids")
                        or excluded_deployment_ids,
                    )
                    continue
                decision_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                order_fallback_entry = None
                if (
                    not external_web_search_internal
                    and not _routing_module._is_sanitized_upstream_route_failure_error(exc)
                ):
                    order_fallback_entry = _responses_execution_module._ordered_deployment_fallback_entry(
                        self,
                        exc,
                        decision_kwargs,
                    )
                if order_fallback_entry is not None:
                    kwargs.update(
                        {
                            key: value
                            for key, value in order_fallback_entry.items()
                            if key != "model"
                        }
                    )
                    target_order = kwargs.get("_target_order")
                    excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                    _trace_module._route_trace(
                        "final_order_fallback_retry_start",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=excluded_deployment_ids,
                        peer_entry=order_fallback_entry,
                        request=_trace_module._trace_request_summary(
                            kwargs,
                            method_name=_trace_module._trace_function_name(original_generic_function),
                        ),
                        exception=_routing_module._trace_exception(exc),
                    )
                    continue
                if (
                    not external_web_search_internal
                    and _routing_module._should_return_route_recovery_stream(exc, decision_kwargs, self)
                ):
                    if _routing_module._should_block_external_web_search_original_recovery(decision_kwargs):
                        _trace_module._route_trace(
                            "external_web_search_original_recovery_blocked",
                            request_id=_routing_module._trace_request_id(kwargs),
                            session=_routing_module._trace_session_context(kwargs),
                            model_group=model,
                            target_order=target_order,
                            excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                            exception=_routing_module._trace_exception(exc),
                        )
                        failed_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                        return _routing_module._failed_responses_stream_response(failed_stream_kwargs, exc)
                    _trace_module._route_trace(
                        "route_recovery_stream_returned",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(exc),
                    )
                    recovery_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                    return _routing_module._route_recovery_stream_response(recovery_stream_kwargs, exc)
                if (
                    _routing_module._is_route_recovery_poll_payload(decision_kwargs)
                    and _routing_module._is_route_recovery_poll_error(exc)
                ):
                    _trace_module._route_trace(
                        "route_recovery_poll_error_propagated",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(exc),
                    )
                    raise
                if _routing_module._should_return_failed_responses_stream(exc, kwargs):
                    _trace_module._route_trace(
                        "responses_failed_stream_returned",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(exc),
                    )
                    failed_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                    return _routing_module._failed_responses_stream_response(failed_stream_kwargs, exc)
                if _routing_module._should_sanitize_final_upstream_route_error(exc):
                    _routing_module._raise_sanitized_upstream_route_failure(model, exc, kwargs)
                raise

    setattr(patched_generic_helper, _GENERIC_HELPER_PATCH_ATTR, True)
    setattr(patched_generic_helper, "_original_helper", original_helper)
    Router._ageneric_api_call_with_fallbacks_helper = patched_generic_helper


def _install_responses_completion_stream_patch() -> None:
    try:
        from litellm.responses.litellm_completion_transformation.streaming_iterator import (
            LiteLLMCompletionStreamingIterator,
        )
    except Exception:
        return

    original_init = getattr(LiteLLMCompletionStreamingIterator, "__init__", None)
    if original_init is None or getattr(
        original_init,
        _RESPONSES_COMPLETION_STREAM_PATCH_ATTR,
        False,
    ):
        return

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not hasattr(self, "completed_response"):
            self.completed_response = None

    setattr(patched_init, _RESPONSES_COMPLETION_STREAM_PATCH_ATTR, True)
    setattr(patched_init, "_original_init", original_init)
    LiteLLMCompletionStreamingIterator.__init__ = patched_init

    original_return_default_done_events = getattr(
        LiteLLMCompletionStreamingIterator,
        "return_default_done_events",
        None,
    )
    if original_return_default_done_events is not None and not getattr(
        original_return_default_done_events,
        _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
        False,
    ):

        def patched_return_default_done_events(
            self: Any,
            litellm_complete_object: Any,
        ) -> Any:
            if _responses_output_module._streaming_completion_should_skip_empty_message_events(
                litellm_complete_object
            ):
                self.sent_output_text_done_event = True
                self.sent_output_content_part_done_event = True
                self.sent_output_item_done_event = True
                return None
            return original_return_default_done_events(
                self,
                litellm_complete_object,
            )

        setattr(
            patched_return_default_done_events,
            _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_return_default_done_events,
            "_original_return_default_done_events",
            original_return_default_done_events,
        )
        LiteLLMCompletionStreamingIterator.return_default_done_events = (
            patched_return_default_done_events
        )

    original_emit_response_completed_event = getattr(
        LiteLLMCompletionStreamingIterator,
        "_emit_response_completed_event",
        None,
    )
    if original_emit_response_completed_event is not None and not getattr(
        original_emit_response_completed_event,
        _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
        False,
    ):

        def patched_emit_response_completed_event(
            self: Any,
            litellm_model_response: Any,
        ) -> Any:
            response_completed_event = original_emit_response_completed_event(
                self,
                litellm_model_response,
            )
            response = _responses_web_search_bridge_module._response_item_get(response_completed_event, "response")
            if response is not None:
                _responses_output_module._strip_empty_message_items_when_structured_output_present(response)
            _normalize_response_completed_event_usage(response_completed_event)
            return response_completed_event

        setattr(
            patched_emit_response_completed_event,
            _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
            True,
        )
        setattr(
            patched_emit_response_completed_event,
            "_original_emit_response_completed_event",
            original_emit_response_completed_event,
        )
        LiteLLMCompletionStreamingIterator._emit_response_completed_event = (
            patched_emit_response_completed_event
        )

def _install_responses_tool_search_bridge_patch() -> None:
    try:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )
    except Exception:
        LiteLLMCompletionResponsesConfig = None  # type: ignore

    if LiteLLMCompletionResponsesConfig is not None:
        original_transform_response = getattr(
            LiteLLMCompletionResponsesConfig,
            "transform_chat_completion_response_to_responses_api_response",
            None,
        )
        if original_transform_response is not None and not getattr(
            original_transform_response,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            False,
        ):

            def patched_transform_chat_completion_response_to_responses_api_response(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                response = original_transform_response(*args, **kwargs)
                request_input = args[0] if len(args) > 0 else kwargs.get("request_input")
                responses_api_request = (
                    args[1] if len(args) > 1 else kwargs.get("responses_api_request")
                )
                return _responses_output_module._normalize_response_tool_search_output(
                    response,
                    _responses_output_module._responses_namespace_tool_map(
                        request_input,
                        responses_api_request,
                    ),
                    _responses_output_module._responses_custom_tool_names(
                        request_input,
                        responses_api_request,
                    ),
                )

            setattr(
                patched_transform_chat_completion_response_to_responses_api_response,
                _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
                True,
            )
            setattr(
                patched_transform_chat_completion_response_to_responses_api_response,
                "_original_transform",
                original_transform_response,
            )
            LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response = staticmethod(
                patched_transform_chat_completion_response_to_responses_api_response
            )

    try:
        from litellm.responses.litellm_completion_transformation.streaming_iterator import (
            LiteLLMCompletionStreamingIterator,
        )
    except Exception:
        return

    original_queue_delta = getattr(
        LiteLLMCompletionStreamingIterator,
        "_queue_tool_call_delta_events",
        None,
    )
    if original_queue_delta is not None and not getattr(
        original_queue_delta,
        _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
        False,
    ):

        def patched_queue_tool_call_delta_events(
            self: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            result = original_queue_delta(self, *args, **kwargs)
            _responses_output_module._normalize_pending_tool_search_events(self)
            return result

        setattr(
            patched_queue_tool_call_delta_events,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_queue_tool_call_delta_events,
            "_original_queue_delta",
            original_queue_delta,
        )
        LiteLLMCompletionStreamingIterator._queue_tool_call_delta_events = (
            patched_queue_tool_call_delta_events
        )

    original_queue_final = getattr(
        LiteLLMCompletionStreamingIterator,
        "_queue_final_tool_call_done_events",
        None,
    )
    if original_queue_final is not None and not getattr(
        original_queue_final,
        _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
        False,
    ):

        def patched_queue_final_tool_call_done_events(
            self: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            result = original_queue_final(self, *args, **kwargs)
            _responses_output_module._normalize_pending_tool_search_events(self)
            return result

        setattr(
            patched_queue_final_tool_call_done_events,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_queue_final_tool_call_done_events,
            "_original_queue_final",
            original_queue_final,
        )
        LiteLLMCompletionStreamingIterator._queue_final_tool_call_done_events = (
            patched_queue_final_tool_call_done_events
        )


def install_all() -> None:
    _install_routing_constraint_patch()
    _install_selected_deployment_marker_patch()
    _install_order_peer_failover_patch()
    _install_generic_deployment_failover_patch()
    _install_responses_completion_stream_patch()
    _install_responses_tool_search_bridge_patch()
