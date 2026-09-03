from __future__ import annotations

import asyncio
import json
import os
from contextvars import ContextVar

from . import api_base as _api_base_module
from . import codex_fast_tier as _codex_fast_tier_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_output as _responses_output_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import trace as _trace_module
from . import dsh_vision_router as _dsh_vision_router_module


from .base import (
    Any,
    List,
    Optional,
    _BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID,
    _CURRENT_SELECTED_DEPLOYMENT,
    _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS,
    _CURRENT_ROUTING_REQUEST_KWARGS,
    _GENERIC_HELPER_PATCH_ATTR,
    _ORDER_PEER_FAILOVER_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_PATCH_ATTR,
    _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
    _ROUTING_CONSTRAINT_PATCH_ATTR,
    _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _install_websocket_frame_limit_patch,
    _normalize_response_completed_event_usage,
)


_ANTHROPIC_UNVERSIONED_ENDPOINT_PATCH_ATTR = (
    "_litellm_menu_anthropic_unversioned_endpoint_patch"
)
_LATIN1_RESPONSE_HEADERS_PATCH_ATTR = (
    "_litellm_menu_latin1_response_headers_patch"
)
_RESPONSES_COMPLETION_STREAM_OUTPUT_ITEM_PATCH_ATTR = (
    "_litellm_menu_responses_completion_stream_output_item_patch"
)
_RESPONSES_WEBSOCKET_HTTP_BRIDGE_PATCH_ATTR = (
    "_litellm_menu_responses_websocket_http_bridge_patch"
)


def _install_responses_websocket_http_bridge_patch() -> None:
    """Serve Responses WebSocket clients through the managed HTTP bridge.

    LiteLLM's native Responses WebSocket mode dials ``wss://<api_base>``
    directly for providers whose config reports native WebSocket support
    (OpenAI, Azure).  Relay deployments expose the OpenAI Responses HTTP
    endpoint on hosts that reject the WebSocket upgrade with 404, so the
    client's WebSocket is closed before ``response.completed`` and Codex
    enters its reconnect loop.  Reporting no native support routes the
    connection through ``ManagedResponsesWebSocketHandler``, which streams
    over the working HTTP endpoint instead.
    """

    try:
        from litellm.llms.base_llm.responses.transformation import (
            BaseResponsesAPIConfig,
        )
    except Exception:
        return

    def patched_supports_native_websocket(self: Any) -> bool:
        return False

    setattr(
        patched_supports_native_websocket,
        _RESPONSES_WEBSOCKET_HTTP_BRIDGE_PATCH_ATTR,
        True,
    )
    BaseResponsesAPIConfig.supports_native_websocket = patched_supports_native_websocket
    for module_name, class_name in (
        ("litellm.llms.openai.responses.transformation", "OpenAIResponsesAPIConfig"),
        ("litellm.llms.azure.responses.transformation", "AzureOpenAIResponsesAPIConfig"),
    ):
        try:
            import importlib

            module = importlib.import_module(module_name)
            provider_config = getattr(module, class_name, None)
        except Exception:
            provider_config = None
        if provider_config is not None:
            provider_config.supports_native_websocket = patched_supports_native_websocket


def _install_latin1_response_headers_patch() -> None:
    try:
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing,
        )
    except Exception:
        return

    original_get_custom_headers = getattr(
        ProxyBaseLLMRequestProcessing,
        "get_custom_headers",
        None,
    )
    if not callable(original_get_custom_headers) or getattr(
        original_get_custom_headers,
        _LATIN1_RESPONSE_HEADERS_PATCH_ATTR,
        False,
    ):
        return

    def patched_get_custom_headers(*args: Any, **kwargs: Any) -> Any:
        headers = original_get_custom_headers(*args, **kwargs)
        if not isinstance(headers, dict):
            return headers
        compatible_headers: dict[Any, Any] = {}
        for key, value in headers.items():
            try:
                str(key).encode("latin-1")
                str(value).encode("latin-1")
            except UnicodeEncodeError:
                continue
            compatible_headers[key] = value
        return compatible_headers

    setattr(
        patched_get_custom_headers,
        _LATIN1_RESPONSE_HEADERS_PATCH_ATTR,
        True,
    )
    setattr(
        patched_get_custom_headers,
        "_original_get_custom_headers",
        original_get_custom_headers,
    )
    ProxyBaseLLMRequestProcessing.get_custom_headers = staticmethod(
        patched_get_custom_headers
    )


def _install_anthropic_unversioned_endpoint_patch() -> None:
    try:
        from litellm import main as litellm_main
    except Exception:
        return

    original_complete = getattr(litellm_main, "_complete_anthropic", None)
    original_get_secret_bool = getattr(litellm_main, "get_secret_bool", None)
    if (
        not callable(original_complete)
        or not callable(original_get_secret_bool)
        or getattr(original_complete, _ANTHROPIC_UNVERSIONED_ENDPOINT_PATCH_ATTR, False)
    ):
        return

    skip_suffix = ContextVar("litellm_menu_skip_anthropic_url_suffix", default=False)

    def patched_get_secret_bool(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "LITELLM_ANTHROPIC_DISABLE_URL_SUFFIX" and skip_suffix.get():
            return True
        return original_get_secret_bool(name, *args, **kwargs)

    def patched_complete(ctx: Any) -> Any:
        if not _api_base_module.is_unversioned_anthropic_messages_endpoint(
            getattr(ctx, "api_base", None)
        ):
            return original_complete(ctx)
        token = skip_suffix.set(True)
        try:
            return original_complete(ctx)
        finally:
            skip_suffix.reset(token)

    setattr(
        patched_complete,
        _ANTHROPIC_UNVERSIONED_ENDPOINT_PATCH_ATTR,
        True,
    )
    setattr(patched_complete, "_original_complete_anthropic", original_complete)
    litellm_main.get_secret_bool = patched_get_secret_bool
    litellm_main._complete_anthropic = patched_complete


def _browser_compatible_headers_retry_kwargs(
    request_kwargs: dict,
) -> Optional[dict]:
    retry_kwargs = _responses_request_module._with_browser_compatible_headers_retry(request_kwargs)
    if retry_kwargs is None:
        return None
    header_kwargs = _responses_request_module._with_browser_compatible_headers(retry_kwargs)
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
    excluded_ids = sorted(_responses_request_module._request_excluded_deployment_ids(request_kwargs))
    if excluded_ids:
        entry["_excluded_deployment_ids"] = excluded_ids
    return entry


def _trace_codex_fast_tier_injected(request_kwargs: dict) -> None:
    requested_tier = _codex_fast_tier_module._codex_fast_default_service_tier(
        request_kwargs
    )
    if requested_tier is None:
        return
    _trace_module._route_trace(
        "codex_fast_default_service_tier_injected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        codex_fast_default_injected=True,
        service_tier=requested_tier,
        requested_service_tier=requested_tier,
        source="codex_config_fast_default",
    )



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


def _request_local_verified_fallback_deployment(
    router: Any,
    model: Any,
    request_kwargs: Optional[dict],
    *,
    verified_ids: set[str],
    target_order: Any,
) -> Optional[dict]:
    if not verified_ids or not isinstance(model, str) or not model.strip():
        return None
    request_kwargs = request_kwargs if isinstance(request_kwargs, dict) else {}
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs,
        "metadata",
    ) or {}
    try:
        configured = _routing_module._router_configured_deployments(
            router,
            model,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return None

    constraints = request_kwargs.copy()
    if target_order is not None:
        constraints["_target_order"] = target_order
    constraints[_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY] = sorted(verified_ids)
    candidates = _responses_request_module._with_retry_target_constraints(
        list(configured or []),
        constraints,
    )
    candidates, cooldown_deployments, cooldown_filtered = (
        _routing_module._with_active_deployment_cooldowns(
            candidates,
            request_kwargs=constraints,
        )
    )
    if not candidates:
        return None
    deployment = candidates[0]
    _trace_module._route_trace(
        "request_local_verified_fallback_selected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=model,
        target_order=target_order,
        verified_deployment_ids=sorted(verified_ids),
        deployment=_routing_module._trace_deployment(deployment),
        cooldown_filtered=cooldown_filtered,
        cooldown_deployments=cooldown_deployments,
        request=_trace_module._trace_request_summary(request_kwargs),
    )
    return deployment


def _request_local_cooling_only_candidate_deployment(
    router: Any,
    model: Any,
    request_kwargs: Optional[dict],
    *,
    target_order: Any,
) -> Optional[dict]:
    if not isinstance(model, str) or not model.strip():
        return None
    request_kwargs = request_kwargs if isinstance(request_kwargs, dict) else {}
    if _routing_module._is_route_recovery_poll_payload(request_kwargs):
        return None
    if _responses_request_module._request_is_fallback_attempt(request_kwargs):
        return None
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs,
        "metadata",
    ) or {}
    try:
        configured = _routing_module._router_configured_deployments(
            router,
            model,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return None

    constraints = request_kwargs.copy()
    if target_order is not None:
        constraints["_target_order"] = target_order
    candidates = _responses_request_module._with_retry_target_constraints(
        list(configured or []),
        constraints,
    )
    if len(candidates) != 1:
        return None
    available, cooldown_deployments, cooldown_filtered = (
        _routing_module._with_active_deployment_cooldowns(
            candidates,
            request_kwargs=constraints,
        )
    )
    if available or not cooldown_filtered:
        return None

    deployment = candidates[0]
    _trace_module._route_trace(
        "request_local_cooling_only_candidate_selected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=model,
        target_order=target_order,
        deployment=_routing_module._trace_deployment(deployment),
        cooldown_deployments=cooldown_deployments,
        request=_trace_module._trace_request_summary(request_kwargs),
    )
    return deployment


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
                    if _responses_request_module._deployment_id(deployment) not in excluded_ids
                ]
            verified_ids = _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.get()
            if verified_ids:
                constrained = [
                    deployment
                    for deployment in constrained
                    if _responses_request_module._deployment_id(deployment)
                    in verified_ids
                ]
            target_deployment_id = _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.get()
            if target_deployment_id:
                constrained = [
                    deployment
                    for deployment in constrained
                    if _responses_request_module._deployment_id(deployment)
                    == target_deployment_id
                ]
            constrained, _cooldown_deployments, _cooldown_filtered = (
                _routing_module._with_active_deployment_cooldowns(
                    constrained,
                    request_kwargs=_CURRENT_ROUTING_REQUEST_KWARGS.get(),
                )
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
            excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_kwargs)
            verified_ids = _responses_request_module._request_verified_fallback_deployment_ids(
                request_kwargs
            )
            target_order = _responses_request_module._request_target_order(request_kwargs)
            excluded_token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids or None)
            verified_token = _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.set(
                verified_ids or None
            )
            target_token = _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.set(
                _routing_module._request_surface_target_deployment_id(request_kwargs)
            )
            request_token = _CURRENT_ROUTING_REQUEST_KWARGS.set(request_kwargs)
            try:
                try:
                    return original_get_available_deployment(self, *args, **kwargs)
                except Exception as exc:
                    if not _routing_module._is_no_deployments_available_error(exc):
                        raise
                    model = kwargs.get("model")
                    if model is None and args:
                        model = args[0]
                    deployment = _request_local_verified_fallback_deployment(
                        self,
                        model,
                        request_kwargs,
                        verified_ids=verified_ids,
                        target_order=target_order,
                    )
                    if deployment is None and not verified_ids:
                        deployment = _request_local_cooling_only_candidate_deployment(
                            self,
                            model,
                            request_kwargs,
                            target_order=target_order,
                        )
                    if deployment is None:
                        raise
                    return deployment
            finally:
                if isinstance(request_kwargs, dict):
                    request_kwargs.pop(_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY, None)
                _CURRENT_ROUTING_REQUEST_KWARGS.reset(request_token)
                _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.reset(target_token)
                _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.reset(verified_token)
                _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(excluded_token)

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
        excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_kwargs)
        verified_ids = _responses_request_module._request_verified_fallback_deployment_ids(
            request_kwargs
        )
        target_order = _responses_request_module._request_target_order(request_kwargs)
        excluded_token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids or None)
        verified_token = _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.set(
            verified_ids or None
        )
        target_token = _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.set(
            _routing_module._request_surface_target_deployment_id(request_kwargs)
        )
        request_token = _CURRENT_ROUTING_REQUEST_KWARGS.set(request_kwargs)
        try:
            try:
                return await original_async_get_available_deployment(
                    self,
                    *args,
                    **kwargs,
                )
            except Exception as exc:
                if not _routing_module._is_no_deployments_available_error(exc):
                    raise
                model = kwargs.get("model")
                if model is None and args:
                    model = args[0]
                deployment = _request_local_verified_fallback_deployment(
                    self,
                    model,
                    request_kwargs,
                    verified_ids=verified_ids,
                    target_order=target_order,
                )
                if deployment is None and not verified_ids:
                    deployment = _request_local_cooling_only_candidate_deployment(
                        self,
                        model,
                        request_kwargs,
                        target_order=target_order,
                    )
                if deployment is None:
                    raise
                return deployment
        finally:
            if isinstance(request_kwargs, dict):
                request_kwargs.pop(_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY, None)
            _CURRENT_ROUTING_REQUEST_KWARGS.reset(request_token)
            _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.reset(target_token)
            _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.reset(verified_token)
            _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(excluded_token)

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
            surface = _routing_module._request_surface_for_deployment(kwargs, deployment)
            if not surface:
                _routing_module._remember_selected_deployment(deployment)
                force_browser_headers = _responses_request_module._request_forces_browser_compatible_headers(kwargs)
                result = original_update_kwargs_with_deployment(
                    self, deployment, kwargs, function_name=function_name
                )
                _routing_module._remember_selected_deployment_for_request(kwargs, deployment)
                if force_browser_headers:
                    kwargs[_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True
                browser_kwargs = _responses_request_module._with_browser_compatible_headers(kwargs)
                if browser_kwargs is not None:
                    kwargs.clear()
                    kwargs.update(browser_kwargs)
                return result
            deployment_id = _responses_request_module._deployment_id(deployment)
            attempted_surfaces = (
                _routing_module._request_attempted_upstream_surfaces(kwargs)
                if deployment_id
                == _routing_module._request_surface_deployment_id(kwargs)
                else []
            )
            if surface not in attempted_surfaces:
                attempted_surfaces.append(surface)
            _routing_module._set_request_surface_state(
                kwargs,
                surface=surface,
                attempted_surfaces=attempted_surfaces,
                deployment_id=deployment_id,
                target_deployment_id=_routing_module._request_surface_target_deployment_id(kwargs),
            )
            _routing_module._remember_selected_deployment(
                deployment,
                surface=surface,
            )
            force_browser_headers = _responses_request_module._request_forces_browser_compatible_headers(kwargs)
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
                target_order=_responses_request_module._request_target_order(kwargs),
                excluded_deployment_ids=sorted(_responses_request_module._request_excluded_deployment_ids(kwargs)),
            )
            result = original_update_kwargs_with_deployment(
                self,
                deployment,
                kwargs,
                function_name=function_name,
            )
            _routing_module._remember_selected_deployment_for_request(kwargs, deployment)
            _routing_module._apply_surface_adapter_to_request(
                kwargs,
                surface,
                deployment.get("litellm_params", {}).get("model"),
            )
            if force_browser_headers:
                kwargs[_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True

            from .responses_request import _with_browser_compatible_headers

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
            response = await original_make_call(self, original_function, *args, **kwargs)
            marker = _CURRENT_SELECTED_DEPLOYMENT.get()
            if _routing_module._is_failed_responses_stream_response(response):
                failed_request = getattr(response, "request_data", None)
                if not isinstance(failed_request, dict):
                    failed_request = kwargs
                if marker is not None:
                    _routing_module._apply_selected_deployment_marker_to_request(
                        failed_request,
                        marker,
                    )
                if _routing_module._protocol_fallback_attempt_active(
                    failed_request,
                ):
                    _routing_module._mark_exception_for_deployment_failover(
                        response.exception,
                        failed_request,
                    )
                    raise response.exception
                if marker is not None and _routing_module._is_current_upstream_surface_incompatible_error(
                    response.exception,
                    failed_request,
                ):
                    _routing_module._clear_protocol_fallback_cache_for_request(
                        failed_request,
                        preserve_relaxed_tool_choice=True,
                    )
                    _routing_module._mark_exception_for_upstream_surface_failover(
                        response.exception,
                        failed_request,
                    )
                    raise response.exception
                return response
            if marker is not None:
                # LiteLLM invokes the streaming-iterator hook after make_call
                # returns.  Persist the selected route on the request object
                # before recording fallback success and before the context
                # marker is reset, so an initial stream failure can exclude
                # the route that actually failed and the cache records the
                # protocol that was really selected.
                _routing_module._apply_selected_deployment_marker_to_request(
                    kwargs,
                    marker,
                )
                _routing_module._record_protocol_fallback_success(kwargs)
                response = _routing_module._wrap_response_with_selected_deployment_marker(
                    response,
                    marker,
                )
            else:
                _routing_module._record_protocol_fallback_success(kwargs)
            return response
        except Exception as exc:
            marker = _CURRENT_SELECTED_DEPLOYMENT.get()
            if marker is not None:
                _routing_module._apply_selected_deployment_marker_to_request(
                    kwargs,
                    marker,
                )
            if (
                marker is not None
                and _routing_module._protocol_fallback_attempt_active(kwargs)
            ):
                _routing_module._mark_exception_for_deployment_failover(exc, kwargs)
            elif marker is not None and _routing_module._is_current_upstream_surface_incompatible_error(
                exc,
                kwargs,
            ):
                _routing_module._clear_protocol_fallback_cache_for_request(
                    kwargs,
                    preserve_relaxed_tool_choice=True,
                )
                _routing_module._mark_exception_for_upstream_surface_failover(
                    exc,
                    kwargs,
                )
            elif (
                marker is not None
                and _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    kwargs,
                )
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
        include_fallback_errors: bool = False,
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
                if include_fallback_errors:
                    peer_kwargs["include_fallback_errors"] = True
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
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            e,
            kwargs,
        ):
            _routing_module._mark_same_deployment_retry_exhausted(e)
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
                if include_fallback_errors:
                    peer_kwargs["include_fallback_errors"] = True
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

        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            e,
            kwargs,
        ):
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
            decision_kwargs = _responses_execution_module._request_kwargs_with_model_group(
                model_group,
                kwargs,
            )
            if _routing_module._should_return_route_recovery_stream(
                e,
                decision_kwargs,
                self,
            ):
                if _routing_module._should_block_external_web_search_original_recovery(
                    decision_kwargs
                ):
                    _trace_module._route_trace(
                        "external_web_search_original_recovery_blocked",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=_responses_execution_module._request_model_group(kwargs)
                        or model_group,
                        target_order=kwargs.get("_target_order"),
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(e),
                    )
                    failed_stream_kwargs = decision_kwargs.copy()
                    return _routing_module._failed_responses_stream_response(
                        failed_stream_kwargs,
                        e,
                    )
                _trace_module._route_trace(
                    "route_recovery_stream_returned",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(kwargs)
                    or model_group,
                    target_order=kwargs.get("_target_order"),
                    excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                    exception=_routing_module._trace_exception(e),
                )
                recovery_stream_kwargs = decision_kwargs.copy()
                recovery_stream_kwargs["_route_recovery_ignore_local_constraints"] = True
                return _routing_module._route_recovery_stream_response(
                    recovery_stream_kwargs,
                    e,
                )
            if _routing_module._should_sanitize_final_upstream_route_error(e):
                _routing_module._raise_sanitized_upstream_route_failure(
                    _responses_execution_module._request_model_group(kwargs)
                    or model_group,
                    e,
                    kwargs,
                )
            raise e

        _trace_module._route_trace(
            "litellm_model_group_fallback_suppressed",
            request_id=_routing_module._trace_request_id(kwargs),
            session=_routing_module._trace_session_context(kwargs),
            model_group=_responses_execution_module._request_model_group(kwargs) or model_group,
            target_order=kwargs.get("_target_order"),
            excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
            request=_trace_module._trace_request_summary(kwargs),
            exception=_routing_module._trace_exception(e),
        )
        # Model identity is part of the request contract.  The Menu handles
        # same-model peer/order failover above; never hand LiteLLM a configured
        # model-group, client, context-window, or content-policy fallback that
        # could silently change the selected model.
        original_args = (
            self,
            e,
            disable_fallbacks,
            [],
            [],
            [],
            model_group,
            args,
            kwargs,
        )
        if include_fallback_errors:
            return await original_common_utils(
                *original_args,
                include_fallback_errors=True,
            )
        return await original_common_utils(*original_args)

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
            _codex_fast_tier_module._with_codex_fast_default_service_tier,
            _responses_request_module._with_codex_tool_registry_instruction,
            _responses_request_module._with_codex_descendant_cleanup_instruction,
            _responses_request_module._with_empty_tool_controls_removed,
            _responses_request_module._with_codex_compaction_controls,
            _responses_request_module._with_responses_native_extra_body,
            _responses_request_module._with_codex_compaction_headers,
        ):
            updated_kwargs = update_request(kwargs)
            if updated_kwargs is not None:
                kwargs = updated_kwargs
                if update_request is _codex_fast_tier_module._with_codex_fast_default_service_tier:
                    _trace_codex_fast_tier_injected(kwargs)
        target_order = kwargs.get("_target_order")
        excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
        external_web_search_internal = _request_is_external_web_search_internal_call(kwargs)
        max_retries = 0 if external_web_search_internal else _routing_module._same_deployment_retries()
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
        wrapped_original_function = (
            _responses_execution_module._wrap_generic_function_for_deployment_failover(
                original_generic_function,
                outer_request_kwargs=kwargs,
            )
        )
        while True:
            try:
                response = await original_helper(
                    self,
                    model,
                    wrapped_original_function,
                    **kwargs,
                )
                marker = _routing_module._selected_deployment_marker_from_response(
                    response
                )
                if marker is None:
                    marker = _routing_module._selected_deployment_marker_from_box()
                _routing_module._merge_request_routing_state_into_selected_deployment_marker(
                    marker,
                    kwargs,
                )
                return response
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
                if _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    kwargs,
                ):
                    _routing_module._mark_exception_for_deployment_failover(exc, kwargs)
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
                # The generic router callback may have rebuilt kwargs from the
                # raw deployment and dropped model_info.  Reapply the selected
                # deployment marker before deciding whether this was a
                # text-only model; otherwise a vision-capable deployment could
                # be rewritten solely because its callback omitted metadata.
                _routing_module._apply_current_selected_deployment_to_request(kwargs)
                if _dsh_vision_router_module.should_attempt_dsh_vision_router(exc, kwargs):
                    _trace_module._route_trace(
                        "dsh_vision_router_fallback_start",
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
                        routed_kwargs = await _dsh_vision_router_module.dsh_vision_router_request_kwargs(kwargs)
                        if routed_kwargs is None:
                            raise RuntimeError("dsh-vision-router could not extract image references")
                        routed_kwargs.pop("model", None)
                        kwargs = routed_kwargs
                        target_order = kwargs.get("_target_order")
                        excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                        retry_attempt = 0
                        _trace_module._route_trace(
                            "dsh_vision_router_fallback_retry_start",
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
                    except Exception as router_exc:
                        _trace_module._route_trace(
                            "dsh_vision_router_fallback_error",
                            request_id=_routing_module._trace_request_id(kwargs),
                            session=_routing_module._trace_session_context(kwargs),
                            model_group=model,
                            original_exception=_routing_module._trace_exception(exc),
                            exception=_routing_module._trace_exception(router_exc),
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
                # The explicit retry loop has now consumed the selected
                # route's budget (including the default zero extra attempts).
                # Do this before asking for an ordered peer/next-hop entry.
                _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._sync_failed_deployment_exclusions(
                    kwargs,
                    exc,
                    deployment_id=_responses_execution_module._failed_deployment_id(exc),
                )
                decision_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                order_fallback_entry = None
                if not external_web_search_internal:
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
                        failed_stream_kwargs["original_generic_function"] = wrapped_original_function
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
                    recovery_stream_kwargs = recovery_stream_kwargs.copy()
                    recovery_stream_kwargs[
                        "_route_recovery_ignore_local_constraints"
                    ] = True
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
                    failed_stream_kwargs["original_generic_function"] = wrapped_original_function
                    return _routing_module._failed_responses_stream_response(failed_stream_kwargs, exc)
                if (
                    kwargs.get("stream") is True
                    and "input" in kwargs
                    and _routing_module._recovery_max_seconds_for_request(kwargs) <= 0
                    and _routing_module._is_route_recovery_poll_error(exc)
                ):
                    failed_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                    failed_stream_kwargs["original_generic_function"] = wrapped_original_function
                    return _routing_module._failed_responses_stream_response(failed_stream_kwargs, exc)
                if _routing_module._should_sanitize_final_upstream_route_error(exc):
                    _routing_module._raise_sanitized_upstream_route_failure(model, exc, kwargs)
                raise

    setattr(patched_generic_helper, _GENERIC_HELPER_PATCH_ATTR, True)
    setattr(patched_generic_helper, "_original_helper", original_helper)
    Router._ageneric_api_call_with_fallbacks_helper = patched_generic_helper


def _install_same_deployment_retry_policy_patch() -> None:
    """Keep LiteLLM's own retry loop aligned with the Menu's next-hop budget."""
    try:
        from litellm.router import Router
    except Exception:
        return

    original_update_kwargs = getattr(Router, "_update_kwargs_before_fallbacks", None)
    if original_update_kwargs is not None and not getattr(
        original_update_kwargs,
        "_litellm_menu_same_deployment_retry_policy_patch",
        False,
    ):

        def patched_update_kwargs(
            self: Any,
            model: str,
            kwargs: dict,
            metadata_variable_name: Optional[str] = "metadata",
        ) -> Any:
            result = original_update_kwargs(
                self,
                model,
                kwargs,
                metadata_variable_name=metadata_variable_name,
            )
            # LiteLLM's per-router retry policy must not consume attempts
            # invisibly. The Menu's explicit loop owns this budget.
            kwargs["num_retries"] = 0
            return result

        setattr(
            patched_update_kwargs,
            "_litellm_menu_same_deployment_retry_policy_patch",
            True,
        )
        setattr(patched_update_kwargs, "_original_update_kwargs", original_update_kwargs)
        Router._update_kwargs_before_fallbacks = patched_update_kwargs

    original_retry_policy = getattr(Router, "get_num_retries_from_retry_policy", None)
    if original_retry_policy is not None and not getattr(
        original_retry_policy,
        "_litellm_menu_same_deployment_retry_policy_patch",
        False,
    ):

        def patched_retry_policy(
            self: Any,
            exception: Exception,
            model_group: Optional[str] = None,
        ) -> int:
            return 0

        setattr(
            patched_retry_policy,
            "_litellm_menu_same_deployment_retry_policy_patch",
            True,
        )
        setattr(patched_retry_policy, "_original_retry_policy", original_retry_policy)
        Router.get_num_retries_from_retry_policy = patched_retry_policy


_MANAGED_WS_KEEPALIVE_PATCH_ATTR = "_litellm_menu_managed_ws_keepalive_patch"


def _install_managed_responses_websocket_keepalive_patch() -> None:
    """Heartbeat the managed Responses WebSocket bridge during silent waits.

    ``ManagedResponsesWebSocketHandler`` serves providers without a native
    WebSocket endpoint by streaming ``litellm.aresponses`` over the client
    WebSocket.  That call bypasses the proxy streaming pipeline, so the
    downstream keepalive heartbeat never runs on this transport: while the
    upstream first event is delayed (a heavy replay prefix can take several
    minutes), nothing reaches the client and Codex fires its per-provider
    ``stream_idle_timeout_ms`` (default ~300s), reporting ``stream
    disconnected before completion: idle timeout waiting for websocket`` and
    replaying the whole turn.  Wrap the inner stream with the proxy heartbeat
    so every silent interval emits one ``response.metadata`` keepalive event
    per configured interval, exactly like the HTTP/SSE transport.
    """

    try:
        from litellm.responses.streaming_iterator import (
            ManagedResponsesWebSocketHandler,
        )
    except Exception:
        return

    original = ManagedResponsesWebSocketHandler._stream_and_forward
    if getattr(original, _MANAGED_WS_KEEPALIVE_PATCH_ATTR, False):
        return

    async def patched_stream_and_forward(self, model, call_kwargs):
        import litellm as litellm_module

        keepalive_request_data = {"model": model, "stream": True}
        for key in ("input", "instructions"):
            value = call_kwargs.get(key)
            if value is not None:
                keepalive_request_data[key] = value
        interval = _routing_module._stream_keepalive_interval_seconds_for_request(
            keepalive_request_data
        )
        stream_response = await litellm_module.aresponses(model=model, **call_kwargs)
        if interval <= 0:
            delivered = stream_response
        else:
            delivered = _streaming_module._yield_downstream_keepalive_stream(
                stream_response,
                keepalive_request_data,
            )
        completed_event = None
        async for chunk in delivered:
            if chunk is None:
                continue
            chunk_type = getattr(chunk, "type", None) or (
                chunk.get("type") if isinstance(chunk, dict) else None
            )
            serialized = self._serialize_chunk(chunk)
            if serialized is None:
                continue
            if chunk_type == "response.completed" and completed_event is None:
                try:
                    completed_event = json.loads(serialized)
                except Exception:
                    pass
            try:
                await self.websocket.send_text(serialized)
            except Exception:
                return completed_event
        return completed_event

    setattr(
        patched_stream_and_forward,
        _MANAGED_WS_KEEPALIVE_PATCH_ATTR,
        True,
    )
    setattr(patched_stream_and_forward, "_original_stream_and_forward", original)
    ManagedResponsesWebSocketHandler._stream_and_forward = patched_stream_and_forward


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

    original_ensure_output_item_for_chunk = getattr(
        LiteLLMCompletionStreamingIterator,
        "_ensure_output_item_for_chunk",
        None,
    )
    if callable(original_ensure_output_item_for_chunk) and not getattr(
        original_ensure_output_item_for_chunk,
        _RESPONSES_COMPLETION_STREAM_OUTPUT_ITEM_PATCH_ATTR,
        False,
    ):

        def patched_ensure_output_item_for_chunk(self: Any, chunk: Any) -> Any:
            # Do not let a role-only Chat chunk create an empty Responses
            # message.  Wait for text, reasoning, annotations, or a tool
            # payload; tool IDs and ordering remain owned by LiteLLM.
            if not _responses_output_module._chat_completion_chunk_has_output_payload(
                chunk
            ):
                return None
            return original_ensure_output_item_for_chunk(self, chunk)

        setattr(
            patched_ensure_output_item_for_chunk,
            _RESPONSES_COMPLETION_STREAM_OUTPUT_ITEM_PATCH_ATTR,
            True,
        )
        setattr(
            patched_ensure_output_item_for_chunk,
            "_original_ensure_output_item_for_chunk",
            original_ensure_output_item_for_chunk,
        )
        LiteLLMCompletionStreamingIterator._ensure_output_item_for_chunk = (
            patched_ensure_output_item_for_chunk
        )

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
    _install_latin1_response_headers_patch()
    _install_anthropic_unversioned_endpoint_patch()
    _install_routing_constraint_patch()
    _install_selected_deployment_marker_patch()
    _install_order_peer_failover_patch()
    _install_generic_deployment_failover_patch()
    _install_same_deployment_retry_policy_patch()
    _install_responses_websocket_http_bridge_patch()
    _install_websocket_frame_limit_patch()
    _install_managed_responses_websocket_keepalive_patch()
    _install_responses_completion_stream_patch()
    _install_responses_tool_search_bridge_patch()
