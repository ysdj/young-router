from __future__ import annotations

from . import computer_facade as _computer_facade_module
from . import responses_output as _responses_output_module
from . import image_inputs as _image_inputs_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_tools as _responses_tools_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    HostedToolPlan,
    List,
    Optional,
    _RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY,
    _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
    _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
    _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY,
    _SUPPORTS_RESPONSES_FUNCTION_TOOLS_KEY,
    _SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY,
    _SUPPORTS_RESPONSES_WEB_SEARCH_KEY,
    _SUPPORTS_WEB_SEARCH_KEY,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES,
    _UPSTREAM_URL_SURFACE_KEY,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
    copy,
    inspect,
    litellm,
)


def _sync_bridge_extra_body_tools(request_kwargs: dict, tools: list[dict]) -> None:
    existing_extra_body = request_kwargs.get("extra_body")
    if not isinstance(existing_extra_body, dict) or "tools" not in existing_extra_body:
        return
    updated_extra_body = existing_extra_body.copy()
    if tools:
        updated_extra_body["tools"] = copy.deepcopy(tools)
    else:
        updated_extra_body.pop("tools", None)
    request_kwargs["extra_body"] = updated_extra_body


def _with_responses_chat_bridge_compatible_tools(
    retry_kwargs: dict,
    retry_metadata: dict,
    *,
    bridge_provider_native_web_search: bool = False,
) -> None:
    if _routing_module._protocol_fallback_relax_tool_choice(retry_kwargs):
        retry_metadata[_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY] = True
    discovered_tools = _responses_tools_module._responses_input_tool_search_output_tools(retry_kwargs.get("input"))
    additional_tools = _responses_tools_module._responses_input_additional_tools(
        retry_kwargs.get("input")
    )
    if "tools" not in retry_kwargs and not discovered_tools and not additional_tools:
        return

    tools = retry_kwargs.get("tools")
    if not isinstance(tools, list):
        tools = []
    if additional_tools:
        tools = [*tools, *additional_tools]
    bridge_web_search = (
        "web_search_options" in retry_kwargs
        or any(
            isinstance(tool, dict)
            and tool.get("type") in {"web_search", "web_search_preview"}
            for tool in tools
        )
    )
    sanitized_tools, web_search_options, stats = _responses_tools_module._responses_chat_bridge_sanitize_tools(
        tools,
        input_value=retry_kwargs.get("input"),
        bridge_web_search=bridge_web_search,
        bridge_provider_native_web_search=bridge_provider_native_web_search,
    )
    if stats.get("changed"):
        retry_metadata["responses_chat_bridge_tool_sanitized"] = stats
    if additional_tools:
        retry_metadata["responses_chat_bridge_additional_tools"] = {
            "item_count": sum(
                1
                for item in retry_kwargs.get("input") or []
                if isinstance(item, dict) and item.get("type") == "additional_tools"
            ),
            "tool_count": len(additional_tools),
        }
    if stats.get("bridged_web_search_tools"):
        retry_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_KEY] = True
        retry_kwargs.pop("web_search_options", None)
        if retry_kwargs.get("stream") is True:
            retry_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY] = True
    _responses_tools_module._append_responses_chat_bridge_instruction(retry_kwargs, stats)
    if web_search_options is not None and not stats.get("bridged_web_search_tools"):
        retry_kwargs["web_search_options"] = web_search_options
    if sanitized_tools:
        retry_kwargs["tools"] = sanitized_tools
        _sync_bridge_extra_body_tools(retry_kwargs, sanitized_tools)
        kept_tool_names = {
            tool["name"]
            for tool in sanitized_tools
            if isinstance(tool.get("name"), str)
        }
        if "tool_choice" in retry_kwargs:
            if _routing_module._protocol_fallback_relax_tool_choice(retry_kwargs):
                retry_kwargs["tool_choice"] = "auto"
                retry_metadata[
                    "responses_chat_bridge_relaxed_forced_tool_choice"
                ] = True
            else:
                retry_kwargs["tool_choice"] = _responses_tools_module._responses_chat_bridge_sanitize_tool_choice(
                    retry_kwargs.get("tool_choice"),
                    kept_tool_names,
                )
        return

    retry_kwargs.pop("tools", None)
    retry_kwargs.pop("tool_choice", None)
    retry_kwargs.pop("parallel_tool_calls", None)
    _sync_bridge_extra_body_tools(retry_kwargs, [])


def _with_responses_function_tool_bridge_compatible_tools(
    bridge_kwargs: dict,
    bridge_metadata: dict,
    outer_request_kwargs: Optional[dict] = None,
) -> None:
    if _routing_module._protocol_fallback_relax_tool_choice(bridge_kwargs):
        bridge_metadata[_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY] = True
    discovered_tools = _responses_tools_module._responses_input_tool_search_output_tools(bridge_kwargs.get("input"))
    additional_tools = _responses_tools_module._responses_input_additional_tools(
        bridge_kwargs.get("input")
    )
    if "tools" not in bridge_kwargs and not discovered_tools and not additional_tools:
        return

    tools = bridge_kwargs.get("tools")
    if not isinstance(tools, list):
        tools = []
    if additional_tools:
        tools = [*tools, *additional_tools]
    plan = _responses_tools_module._responses_hosted_tool_plan(
        bridge_kwargs,
        outer_request_kwargs,
    )
    bridge_web_search = _responses_hosted_web_search_needs_external_bridge(
        bridge_kwargs,
        outer_request_kwargs,
        plan=plan,
    )
    sanitized_tools, web_search_options, stats = _responses_tools_module._responses_chat_bridge_sanitize_tools(
        tools,
        input_value=bridge_kwargs.get("input"),
        bridge_web_search=bridge_web_search,
    )
    if stats.get("changed"):
        bridge_metadata["responses_function_tool_bridge_tool_sanitized"] = stats
    if additional_tools:
        bridge_metadata["responses_function_tool_bridge_additional_tools"] = {
            "item_count": sum(
                1
                for item in bridge_kwargs.get("input") or []
                if isinstance(item, dict) and item.get("type") == "additional_tools"
            ),
            "tool_count": len(additional_tools),
        }
    if stats.get("bridged_web_search_tools"):
        bridge_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_KEY] = True
        bridge_kwargs.pop("web_search_options", None)
    _responses_tools_module._append_responses_chat_bridge_instruction(bridge_kwargs, stats)
    if web_search_options is not None and not stats.get("bridged_web_search_tools"):
        bridge_kwargs["web_search_options"] = web_search_options
    if sanitized_tools:
        bridge_kwargs["tools"] = sanitized_tools
        _sync_bridge_extra_body_tools(bridge_kwargs, sanitized_tools)
        if (
            not isinstance(bridge_kwargs.get("parallel_tool_calls"), bool)
            and (
                stats.get("bridged_namespace_tools")
                or stats.get("bridged_custom_tools")
            )
        ):
            bridge_kwargs["parallel_tool_calls"] = False
            bridge_metadata[
                "responses_function_tool_bridge_parallel_tool_calls_defaulted"
            ] = False
        kept_tool_names = {
            tool["name"]
            for tool in sanitized_tools
            if isinstance(tool.get("name"), str)
        }
        if "tool_choice" in bridge_kwargs:
            tool_choice = bridge_kwargs.get("tool_choice")
            if _routing_module._protocol_fallback_relax_tool_choice(bridge_kwargs):
                bridge_kwargs["tool_choice"] = "auto"
                bridge_metadata[
                    "responses_function_tool_bridge_relaxed_forced_tool_choice"
                ] = True
            elif not (
                not bridge_web_search
                and isinstance(tool_choice, dict)
                and tool_choice.get("type") in {"web_search", "web_search_preview"}
            ):
                bridge_kwargs["tool_choice"] = (
                    _responses_tools_module._responses_function_tool_bridge_sanitize_tool_choice(
                        tool_choice,
                        kept_tool_names,
                    )
                )
        return

    bridge_kwargs.pop("tools", None)
    bridge_kwargs.pop("tool_choice", None)
    bridge_kwargs.pop("parallel_tool_calls", None)
    _sync_bridge_extra_body_tools(bridge_kwargs, [])


def _responses_chat_bridge_retry_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if request_kwargs.get("use_chat_completions_api") is True:
        return None
    if _responses_request_module._request_already_attempted_responses_chat_bridge(
        request_kwargs
    ) or _responses_request_module._request_already_attempted_responses_chat_bridge(outer_request_kwargs):
        return None
    if _responses_chat_bridge_retry_reason(
        exception, request_kwargs, outer_request_kwargs
    ) is None:
        return None
    current_surface = _routing_module._request_current_upstream_surface(
        request_kwargs
    )
    if current_surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return None
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_request_kwargs)
    if (
        _computer_facade_module._native_hosted_computer_unsupported_error(
            exception,
            request_kwargs,
            outer_request_kwargs,
        )
        and not _tools_module._request_has_browser_computer_client_tool(request_kwargs)
        and not _tools_module._request_has_browser_computer_client_tool(outer_request_kwargs)
    ):
        return None

    retry_kwargs = request_kwargs.copy()
    litellm_metadata = _request_context_module._request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_RESPONSES_CHAT_BRIDGE_METADATA_KEY] = True
    retry_metadata[_RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY] = (
        "responses_schema_unsupported"
    )
    _responses_execution_module._remember_responses_chat_bridge_model_group(
        retry_metadata,
        request_kwargs,
        outer_request_kwargs,
    )
    if _computer_facade_module._request_hosted_browser_computer_blocks_chat_bridge(request_kwargs, outer_request_kwargs):
        return None
    _with_responses_chat_bridge_compatible_tools(retry_kwargs, retry_metadata)
    bridge_input, input_stats = _responses_tools_module._responses_chat_bridge_input(
        retry_kwargs.get("input")
    )
    if input_stats.get("changed"):
        retry_kwargs["input"] = bridge_input
        retry_metadata["responses_chat_bridge_input_sanitized"] = input_stats
    retry_kwargs["litellm_metadata"] = retry_metadata
    retry_kwargs["use_chat_completions_api"] = True
    if current_surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        retry_kwargs["_litellm_menu_upstream_url_surface"] = current_surface
    return retry_kwargs


def _responses_chat_bridge_retry_reason(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> Optional[str]:
    if _routing_module._is_responses_endpoint_not_found_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        return "responses_endpoint_not_found"
    if (
        (
            _responses_request_module._request_is_responses_api(request_kwargs)
            or _responses_request_module._request_is_responses_api(outer_request_kwargs)
        )
        and _routing_module._is_responses_schema_unsupported_error(exception)
    ):
        return "responses_schema_unsupported"
    return None


def _request_configured_responses_endpoint_unsupported(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    model_info = _request_context_module._request_model_info(request_kwargs)
    mode = _routing_module._request_current_upstream_surface(request_kwargs)
    if not mode:
        mode = _normalized_upstream_url_surface(
            model_info.get(_UPSTREAM_URL_SURFACE_KEY)
        )
    return mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES


def _request_has_explicit_surface_metadata(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    model_info = _request_context_module._request_model_info(request_kwargs)
    return _UPSTREAM_URL_SURFACE_KEY in model_info


def _current_route_responses_endpoint_unsupported(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if _request_has_explicit_surface_metadata(request_kwargs):
        return _request_configured_responses_endpoint_unsupported(request_kwargs)
    if _routing_module._request_current_upstream_surface(request_kwargs):
        return _request_configured_responses_endpoint_unsupported(request_kwargs)
    return _request_configured_responses_endpoint_unsupported(
        request_kwargs
    ) or _request_configured_responses_endpoint_unsupported(outer_request_kwargs)


def _model_info_has_chat_bridge_mode(model_info: dict) -> bool:
    mode = _normalized_upstream_url_surface(
        model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    )
    if mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True
    if mode == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return False
    return False


def _request_has_chat_bridge_mode(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    allow_selected_marker: bool = False,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        current_surface = _routing_module._request_current_upstream_surface(request)
        if current_surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
            return True
        if current_surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
            continue
        model_info = _request_context_module._request_model_info(request)
        if model_info and _model_info_has_chat_bridge_mode(model_info):
            return True
    if allow_selected_marker:
        # The generic router can rebuild callback kwargs before the native
        # provider error is handled.  In that narrow window the selected
        # deployment's surface is still available only in the request-scoped
        # marker.  Use that marker so a Hosted web-search schema rejection on a
        # Chat deployment enters the Chat bridge instead of the Responses bridge.
        marker = _routing_module._selected_deployment_marker_from_box()
        if isinstance(marker, dict):
            marker_surface = _normalized_upstream_url_surface(
                marker.get(_routing_module._CURRENT_UPSTREAM_URL_SURFACE_KEY)
            )
            if marker_surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
                return True
            marker_model_info = marker.get("model_info")
            if isinstance(marker_model_info, dict) and _model_info_has_chat_bridge_mode(
                marker_model_info
            ):
                return True
    return False


def _selected_marker_chat_fallback_surface_for_request(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    marker = _routing_module._selected_deployment_marker_from_box()
    if not isinstance(marker, dict):
        return None
    marker_model_info = marker.get("model_info")
    if not isinstance(marker_model_info, dict):
        return None
    marker_surface = _normalized_upstream_url_surface(
        marker_model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    )
    if marker_surface not in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return None
    request_model_info = _request_context_module._request_model_info(request_kwargs)
    request_id = request_model_info.get("id") if isinstance(request_model_info, dict) else None
    marker_id = marker_model_info.get("id")
    if isinstance(request_id, str) and request_id.strip() and isinstance(marker_id, str):
        if request_id.strip() != marker_id.strip():
            return None
    return marker_surface


def _request_is_direct_openai_route(request_kwargs: Optional[dict]) -> bool:
    host = _responses_request_module._api_base_host(_responses_request_module._request_api_base(request_kwargs))
    return host == "api.openai.com"


def _request_supports_native_responses_hosted_tools(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        model_info = _request_context_module._request_model_info(request)
        if model_info.get(_SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY) is True:
            return True
        if _request_is_direct_openai_route(request):
            return True
    return False


def _request_uses_responses_endpoint(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    model_info = _request_context_module._request_model_info(request_kwargs)
    mode = _routing_module._request_current_upstream_surface(request_kwargs)
    if not mode:
        mode = _normalized_upstream_url_surface(
            model_info.get(_UPSTREAM_URL_SURFACE_KEY)
        )
    if mode:
        if mode == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
            return True
        if mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
            return False
    return _responses_request_module._request_is_responses_api(request_kwargs)


def _request_supports_native_responses_web_search(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if _routing_module._request_current_upstream_surface(request_kwargs):
        support = _request_native_responses_web_search_support_decision(
            request_kwargs
        )
        return support is True
    for request in (request_kwargs, outer_request_kwargs):
        support = _request_native_responses_web_search_support_decision(request)
        if support is not None:
            return support
    return False


def _request_native_responses_web_search_support_decision(
    request_kwargs: Optional[dict],
) -> Optional[bool]:
    if not isinstance(request_kwargs, dict):
        return None
    model_info = _request_context_module._request_model_info(request_kwargs)
    if model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is False:
        return False
    if model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is False:
        return False
    # A deterministic native rejection is a route-local negative capability
    # signal. Keep the normal unknown/true native-first contract, but avoid
    # paying the same rejected tool call on every request until the short
    # probe memory expires.
    if _routing_module._web_search_tool_unsupported_cached(request_kwargs):
        return False
    if model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is True:
        return True
    if model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is True:
        return True
    if _request_is_direct_openai_route(request_kwargs):
        return True
    return None


def _request_native_responses_web_search_support_decision_for_requests(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[bool]:
    """Preserve unknown native capability separately from a definite false.

    The boolean support helper intentionally represents unknown providers as
    false for yes/no dispatch callers. The pre-dispatch provider-native path
    must not treat that representation as a rejection: only an explicit false
    flag or a cached deterministic rejection authorizes the local bridge.
    """
    if _routing_module._request_current_upstream_surface(request_kwargs):
        return _request_native_responses_web_search_support_decision(request_kwargs)
    for request in (request_kwargs, outer_request_kwargs):
        decision = _request_native_responses_web_search_support_decision(request)
        if decision is not None:
            return decision
    return None


def _request_web_search_support_is_unknown(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if _routing_module._web_search_tool_unsupported_cached(request_kwargs):
        return False
    model_info = _request_context_module._request_model_info(request_kwargs)
    return (
        model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is None
        and model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is None
    )


def _request_should_try_unknown_native_responses_web_search(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if _routing_module._request_current_upstream_surface(request_kwargs):
        return (
            _request_web_search_support_is_unknown(request_kwargs)
            and _request_uses_responses_endpoint(request_kwargs)
        )
    for request in (request_kwargs, outer_request_kwargs):
        if _request_web_search_support_is_unknown(
            request
        ) and _request_uses_responses_endpoint(request):
            return True
    return False


def _request_should_bridge_responses_web_search(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    plan: Optional[HostedToolPlan] = None,
) -> bool:
    plan = plan or _responses_tools_module._responses_hosted_tool_plan(
        request_kwargs,
        outer_request_kwargs,
    )
    cached_unsupported = any(
        _routing_module._web_search_tool_unsupported_cached(request)
        is not None
        for request in (request_kwargs, outer_request_kwargs)
    )
    if not plan.hosted_web_search:
        # Provider-native declarations (for example OpenRouter's
        # openrouter:web_search) remain pass-through while capability is
        # unknown. Once the route has explicitly rejected that declaration,
        # the same local function bridge is safe to use preemptively until
        # the probe memory expires.
        native_support_decision = (
            _request_native_responses_web_search_support_decision_for_requests(
                request_kwargs,
                outer_request_kwargs,
            )
        )
        return cached_unsupported or native_support_decision is False
    if _request_supports_native_responses_web_search(
        request_kwargs,
        outer_request_kwargs,
    ):
        return False
    if _request_should_try_unknown_native_responses_web_search(
        request_kwargs,
        outer_request_kwargs,
    ):
        return False
    return True


def _request_should_try_native_responses_client_tools(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    support = _request_responses_client_tool_support(
        request_kwargs,
        outer_request_kwargs,
    )
    if (
        support is None
        and _request_has_responses_client_tools_requiring_bridge(
            request_kwargs,
            outer_request_kwargs,
        )
        and any(
            _responses_request_module._request_has_codex_client_evidence(request)
            for request in (request_kwargs, outer_request_kwargs)
        )
    ):
        # Codex desktop requests carry namespace/custom client tools, but most
        # third-party Responses gateways do not advertise whether those native
        # tool types are accepted.  Trying the opaque namespace first adds a
        # guaranteed 400-and-retry cycle on such gateways; use the existing
        # same-surface function bridge immediately for Codex turns instead.
        return False
    return support is not False


def _request_responses_client_tool_support(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[bool]:
    current_host = _responses_request_module._api_base_host(
        _responses_request_module._request_api_base(request_kwargs)
    )
    if current_host and current_host != "api.openai.com":
        model_info = _request_context_module._request_model_info(request_kwargs)
        configured_support = model_info.get(_SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY)
        if isinstance(configured_support, bool):
            return configured_support
        return None
    for request in (request_kwargs, outer_request_kwargs):
        model_info = _request_context_module._request_model_info(request)
        configured_support = model_info.get(
            _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY
        )
        if isinstance(configured_support, bool):
            return configured_support
        if _request_is_direct_openai_route(request):
            return True
    return None


def _request_supports_responses_function_tools(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if isinstance(request_kwargs, dict):
        model_info = _request_context_module._request_model_info(request_kwargs)
        configured_support = model_info.get(
            _SUPPORTS_RESPONSES_FUNCTION_TOOLS_KEY
        )
        route_is_selected = bool(
            _routing_module._request_current_upstream_surface(request_kwargs)
            or _request_has_explicit_surface_metadata(request_kwargs)
        )
        if isinstance(configured_support, bool):
            return configured_support
        if route_is_selected and not _request_uses_responses_endpoint(request_kwargs):
            return False
        if _request_uses_responses_endpoint(request_kwargs):
            return True

    model_info = _request_context_module._request_model_info(outer_request_kwargs)
    configured_support = model_info.get(
        _SUPPORTS_RESPONSES_FUNCTION_TOOLS_KEY
    )
    if isinstance(configured_support, bool):
        return configured_support
    if _request_uses_responses_endpoint(outer_request_kwargs):
        return True
    return False


def _responses_hosted_web_search_needs_external_bridge(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    plan: Optional[HostedToolPlan] = None,
) -> bool:
    return _request_should_bridge_responses_web_search(
        request_kwargs,
        outer_request_kwargs,
        plan=plan,
    )


def _request_has_preemptive_responses_chat_bridge(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if (
            metadata is not None
                and metadata.get(_RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY) is True
        ):
            return True
    return False


def _request_has_preemptive_responses_function_tool_bridge(
    request_kwargs: Optional[dict],
) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if (
            metadata is not None
            and metadata.get(_RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY)
            is True
        ):
            return True
    return False


def _request_has_responses_function_tool_bridge_attempt(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        for key in ("litellm_metadata", "metadata"):
            metadata = _request_context_module._request_metadata_dict(request, key)
            if metadata is None:
                continue
            if metadata.get(_RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY) is True:
                return True
            if metadata.get(_RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY) is True:
                return True
    return False


def _with_responses_native_client_tool_passthrough(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    if request_kwargs.get("use_chat_completions_api") is True:
        return None
    if not _request_uses_responses_endpoint(request_kwargs):
        return None
    if _request_has_responses_function_tool_bridge_attempt(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None

    support = _request_responses_client_tool_support(
        request_kwargs,
        outer_request_kwargs,
    )
    if _request_is_direct_openai_route(request_kwargs):
        return None
    if support is False and not _responses_request_module._request_is_codex_compaction(
        request_kwargs
    ):
        return None
    if support is None and not any(
        isinstance(request, dict)
        and (
            bool(_request_context_module._request_model_info(request))
            or bool(_responses_request_module._request_api_base(request))
            or bool(_routing_module._request_current_upstream_surface(request))
        )
        for request in (request_kwargs, outer_request_kwargs)
    ):
        # A bare pre-call hook has not been associated with a deployment yet.
        # Do not guess that its Responses history belongs to a compatible
        # gateway and rewrite the client's input before routing metadata exists.
        return None

    input_value = request_kwargs.get("input")
    lifted_items = 0
    lifted_tools: list[dict] = []
    if (
        support is None
        or (
            support is False
            and _responses_request_module._request_is_codex_compaction(request_kwargs)
        )
    ) and isinstance(input_value, list):
        # Unknown compatible gateways, and explicitly unsupported compaction
        # routes, reject Codex additional_tools items. Relays that set
        # supports_responses_client_tools keep the native item.
        for item in input_value:
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                break
            item_tools = item.get("tools")
            if not isinstance(item_tools, list):
                break
            lifted_items += 1
            lifted_tools.extend(
                copy.deepcopy(tool)
                for tool in item_tools
                if isinstance(tool, dict)
            )
        if lifted_items >= len(input_value):
            lifted_items = 0
            lifted_tools = []

    request_tools = request_kwargs.get("tools")
    merged_tools = copy.deepcopy(request_tools) if isinstance(request_tools, list) else []
    for tool in lifted_tools:
        if tool not in merged_tools:
            merged_tools.append(tool)

    has_client_tool = bool(lifted_tools) or any(
        isinstance(tool, dict)
        and tool.get("type") in {"namespace", "custom", "tool_search"}
        for tool in merged_tools
    )
    if not has_client_tool or not merged_tools:
        return None

    modified_kwargs = request_kwargs.copy()
    if lifted_items:
        modified_kwargs["input"] = input_value[lifted_items:]
        modified_kwargs["tools"] = merged_tools

    existing_extra_body = request_kwargs.get("extra_body")
    extra_body = (
        existing_extra_body.copy()
        if isinstance(existing_extra_body, dict)
        else {}
    )
    if extra_body.get("tools") != merged_tools:
        extra_body["tools"] = copy.deepcopy(merged_tools)
    modified_kwargs["extra_body"] = extra_body

    litellm_metadata = (
        _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    passthrough_metadata = {
        "tool_count": len(merged_tools),
        "lifted_additional_tools_items": lifted_items,
        "lifted_tool_count": len(lifted_tools),
    }
    if (
        extra_body == existing_extra_body
        and not lifted_items
        and litellm_metadata.get(
            _RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY
        )
        == passthrough_metadata
    ):
        return None
    updated_metadata = litellm_metadata.copy()
    updated_metadata[
        _RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY
    ] = passthrough_metadata
    modified_kwargs["litellm_metadata"] = updated_metadata
    return modified_kwargs


def _responses_external_web_search_bridge_kwargs(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    plan: Optional[HostedToolPlan] = None,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    if _tools_module._request_suppresses_external_web_search_post_call(request_kwargs):
        return None
    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    if _current_route_responses_endpoint_unsupported(
        request_kwargs,
        outer_for_tool_plan,
    ):
        return None
    plan = plan or _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    cached_unsupported = any(
        _routing_module._web_search_tool_unsupported_cached(request)
        is not None
        for request in (request_kwargs, outer_for_tool_plan)
    )
    native_support_decision = (
        _request_native_responses_web_search_support_decision_for_requests(
            request_kwargs,
            outer_for_tool_plan,
        )
    )
    if (
        not plan.hosted_web_search
        and not cached_unsupported
        and native_support_decision is not False
    ):
        return None
    if plan.hosted_computer:
        return None
    if not _request_supports_responses_function_tools(
        request_kwargs,
        outer_for_tool_plan,
    ):
        # The fallback is an ordinary function-tool declaration. If the route
        # explicitly rejects function tools, do not advertise a capability it
        # cannot receive; leave the hosted request on the unsupported path.
        return None

    bridged_tools, stats = _responses_tools_module._responses_external_web_search_bridge_tools(
        request_kwargs.get("tools")
    )
    if bridged_tools is None:
        return None

    bridge_kwargs = request_kwargs.copy()
    bridge_kwargs["tools"] = bridged_tools
    bridge_kwargs.pop("web_search_options", None)
    if "tool_choice" in bridge_kwargs:
        bridge_kwargs["tool_choice"] = _responses_tools_module._responses_external_web_search_bridge_tool_choice(
            bridge_kwargs.get("tool_choice")
        )

    litellm_metadata = _request_context_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
    bridge_metadata = litellm_metadata.copy()
    bridge_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_KEY] = True
    bridge_metadata["external_web_search_native_bridge"] = True
    bridge_metadata["responses_external_web_search_tool_sanitized"] = stats
    _responses_execution_module._remember_responses_chat_bridge_model_group(
        bridge_metadata,
        request_kwargs,
        outer_request_kwargs,
    )
    bridge_kwargs["litellm_metadata"] = bridge_metadata
    return bridge_kwargs


def _with_responses_external_web_search_bridge(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    if not _responses_hosted_web_search_needs_external_bridge(
        request_kwargs,
        outer_for_tool_plan,
        plan=plan,
    ):
        return None
    # If the route has not declared support for opaque client tools, keep a
    # Hosted-search request containing a namespace/custom tool intact for the
    # native attempt.  Once that route explicitly rejects the Responses shape,
    # the error path can use the single Chat bridge to preserve both tool
    # families.  Ordinary function tools remain eligible for the direct
    # pi-web-access fallback.
    client_tool_support = _request_responses_client_tool_support(
        request_kwargs,
        outer_for_tool_plan,
    )
    if (
        client_tool_support is None
        and plan.client_namespaces
    ):
        return None
    return _responses_external_web_search_bridge_kwargs(
        request_kwargs,
        outer_request_kwargs,
        plan=plan,
    )


def _native_responses_web_search_unsupported_error(
    exception: Exception,
    request_kwargs: Optional[dict] = None,
) -> bool:
    return _routing_module._is_native_responses_web_search_unsupported_error(
        exception,
        request_kwargs,
    )


def _with_responses_external_web_search_bridge_after_native_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    if _responses_web_search_bridge_module._request_has_provider_native_web_search_event(
        request_kwargs
    ):
        return None
    if not _native_responses_web_search_unsupported_error(
        exception,
        request_kwargs,
    ):
        return None
    _routing_module._record_web_search_tool_unsupported(
        exception,
        request_kwargs,
        outer_request_kwargs,
    )
    if _tools_module._request_should_intercept_external_web_search(request_kwargs):
        return None
    # A bare Responses 404 means the selected gateway has no Responses
    # endpoint at all; it is not evidence that only Hosted web search is
    # unavailable. Use the one Chat bridge so client namespaces and the
    # Hosted declaration are converted together.
    if _routing_module._is_responses_endpoint_not_found_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        bridge_kwargs = _responses_chat_bridge_retry_kwargs(
            exception,
            request_kwargs,
            outer_request_kwargs,
        )
    else:
        bridge_kwargs = None
    # A Chat-surface deployment can accept ordinary function tools but cannot
    # accept the Responses Hosted `web_search` declaration.  Reuse the normal
    # preemptive Chat bridge here after the provider has explicitly rejected
    # the Hosted schema; otherwise the error would be mistaken for a stream
    # failure and enter route recovery.
    if bridge_kwargs is None:
        if (
            _request_has_chat_bridge_mode(
                request_kwargs,
                outer_request_kwargs,
                allow_selected_marker=True,
            )
            or _current_route_responses_endpoint_unsupported(
                request_kwargs,
                outer_request_kwargs,
            )
        ):
            bridge_kwargs = _responses_chat_bridge_preemptive_kwargs(
                request_kwargs,
                outer_request_kwargs,
                include_hosted_web_search_unsupported=True,
                include_client_tool_unsupported=True,
                allow_selected_marker=True,
            )
        else:
            bridge_kwargs = _responses_external_web_search_bridge_kwargs(
                request_kwargs,
                outer_request_kwargs,
            )
    if bridge_kwargs is None:
        return None
    bridge_metadata = _request_context_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    updated_metadata = bridge_metadata.copy()
    updated_metadata["external_web_search_native_error_fallback"] = True
    bridge_kwargs["litellm_metadata"] = updated_metadata
    return bridge_kwargs


def _responses_external_web_search_bridge_possible(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    plan: Optional[HostedToolPlan] = None,
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return False
    if _tools_module._request_suppresses_external_web_search_post_call(request_kwargs):
        return False
    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    if _current_route_responses_endpoint_unsupported(
        request_kwargs,
        outer_for_tool_plan,
    ):
        return False
    plan = plan or _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    if plan.hosted_computer:
        return False
    cached_unsupported = any(
        _routing_module._web_search_tool_unsupported_cached(request)
        is not None
        for request in (request_kwargs, outer_for_tool_plan)
    )
    native_support_decision = (
        _request_native_responses_web_search_support_decision_for_requests(
            request_kwargs,
            outer_for_tool_plan,
        )
    )
    if (
        not plan.hosted_web_search
        and not cached_unsupported
        and native_support_decision is not False
    ):
        return False
    bridged_tools, _stats = _responses_tools_module._responses_external_web_search_bridge_tools(
        request_kwargs.get("tools")
    )
    return bridged_tools is not None


def _responses_chat_bridge_preemptive_reason(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    include_hosted_web_search_unsupported: bool = False,
    include_client_tool_unsupported: bool = False,
    plan: Optional[HostedToolPlan] = None,
    allow_selected_marker: bool = False,
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    if _responses_request_module._request_is_codex_compaction(request_kwargs):
        return None
    outer_for_tool_reason = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    plan = plan or _responses_tools_module._responses_hosted_tool_plan(
        request_kwargs, outer_for_tool_reason
    )
    # A provider-native declaration is already valid on its own. Do not
    # mistake an OpenRouter Chat surface for a Hosted-search incompatibility
    # and replace it with pi-web-access while capability is unknown. If the
    # route has explicitly rejected that declaration, the error-path caller
    # opts into the local pair below.
    has_provider_native_search = any(
        isinstance(tool, dict)
        and tool.get("type") in _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES
        for tool in (request_kwargs.get("tools") or [])
    )
    if has_provider_native_search and not (
        plan.client_namespaces or plan.client_functions
    ):
        native_support_decision = (
            _request_native_responses_web_search_support_decision_for_requests(
                request_kwargs,
                outer_for_tool_reason,
            )
        )
        if (
            include_hosted_web_search_unsupported
            and native_support_decision is False
        ):
            current_surface = _routing_module._request_current_upstream_surface(
                request_kwargs
            )
            if (
                current_surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES
                or _request_has_chat_bridge_mode(
                    request_kwargs,
                    outer_for_tool_reason,
                    allow_selected_marker=allow_selected_marker,
                )
            ):
                return "provider_native_web_search_unsupported"
        return None
    if _current_route_responses_endpoint_unsupported(
        request_kwargs,
        outer_for_tool_reason,
    ):
        return "responses_endpoint_unsupported"
    current_surface = _routing_module._request_current_upstream_surface(request_kwargs)
    selected_chat_fallback_surface = (
        _selected_marker_chat_fallback_surface_for_request(request_kwargs)
        if allow_selected_marker
        else None
    )
    if (
        include_hosted_web_search_unsupported
        and plan is not None
        and plan.hosted_web_search
        and (
            current_surface != _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
            or selected_chat_fallback_surface is not None
        )
        and _request_has_chat_bridge_mode(
            request_kwargs,
            outer_for_tool_reason,
            allow_selected_marker=allow_selected_marker,
        )
    ):
        return "hosted_web_search_native_unsupported"
    return None


def _responses_chat_bridge_preemptive_kwargs(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    include_hosted_web_search_unsupported: bool = False,
    include_client_tool_unsupported: bool = False,
    allow_selected_marker: bool = False,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if _request_has_preemptive_responses_chat_bridge(request_kwargs):
        return request_kwargs
    if _responses_request_module._request_already_attempted_responses_chat_bridge(request_kwargs):
        return None

    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    reasoning = request_kwargs.get("reasoning")
    requested_xhigh = (
        isinstance(reasoning, dict) and reasoning.get("effort") == "xhigh"
    ) or request_kwargs.get("reasoning_effort") == "xhigh"
    if (
        request_kwargs.get("use_chat_completions_api") is True
        and requested_xhigh
        and not plan.hosted_web_search
    ):
        # An explicit Chat dispatch may reject xhigh independently of its
        # Responses-shaped tool payload. Preserve that payload for the
        # dedicated xhigh -> high retry below rather than changing two request
        # dimensions at once. Hosted web search is different: it must enter
        # the bridge before the Chat call because the upstream has no native
        # web-search tool.
        return None
    reason = _responses_chat_bridge_preemptive_reason(
        request_kwargs,
        outer_for_tool_plan,
        include_hosted_web_search_unsupported=include_hosted_web_search_unsupported,
        include_client_tool_unsupported=include_client_tool_unsupported,
        plan=plan,
        allow_selected_marker=allow_selected_marker,
    )
    if reason is None:
        return None
    bridge_kwargs = request_kwargs.copy()
    litellm_metadata = _request_context_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
    bridge_metadata = litellm_metadata.copy()
    bridge_metadata[_RESPONSES_CHAT_BRIDGE_METADATA_KEY] = True
    bridge_metadata[_RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY] = True
    bridge_metadata["responses_chat_bridge_preemptive_reason"] = reason
    _responses_execution_module._remember_responses_chat_bridge_model_group(
        bridge_metadata,
        request_kwargs,
        outer_request_kwargs,
    )
    if _computer_facade_module._request_hosted_browser_computer_blocks_chat_bridge(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    _with_responses_chat_bridge_compatible_tools(
        bridge_kwargs,
        bridge_metadata,
        bridge_provider_native_web_search=(
            reason == "provider_native_web_search_unsupported"
        ),
    )
    bridge_input, input_stats = _responses_tools_module._responses_chat_bridge_input(
        bridge_kwargs.get("input")
    )
    if input_stats.get("changed"):
        bridge_kwargs["input"] = bridge_input
        bridge_metadata["responses_chat_bridge_input_sanitized"] = input_stats
    bridge_kwargs["litellm_metadata"] = bridge_metadata
    bridge_kwargs["use_chat_completions_api"] = True
    current_surface = _routing_module._request_current_upstream_surface(
        request_kwargs
    )
    selected_chat_fallback_surface = (
        _selected_marker_chat_fallback_surface_for_request(request_kwargs)
        if allow_selected_marker
        else None
    )
    if current_surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        bridge_kwargs["_litellm_menu_upstream_url_surface"] = current_surface
    elif selected_chat_fallback_surface is not None:
        # Fallback-mode deployments start ordinary requests on the client
        # Responses surface. Hosted web_search is not valid on an Anthropic/
        # Chat fallback, so switch the bridge wire surface before dispatch.
        bridge_kwargs["_litellm_menu_upstream_url_surface"] = (
            selected_chat_fallback_surface
        )
    return bridge_kwargs


def _responses_function_tool_bridge_preemptive_reason(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    plan: Optional[HostedToolPlan] = None,
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    if _responses_request_module._request_is_codex_compaction(request_kwargs):
        return None
    outer_for_tool_reason = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    if _current_route_responses_endpoint_unsupported(
        request_kwargs,
        outer_for_tool_reason,
    ):
        return None
    if _computer_facade_module._request_hosted_browser_computer_blocks_chat_bridge(
        request_kwargs,
        outer_for_tool_reason,
    ):
        return None
    if _request_should_try_native_responses_client_tools(
        request_kwargs,
        outer_for_tool_reason,
    ):
        return None
    if not _request_supports_responses_function_tools(
        request_kwargs,
        outer_for_tool_reason,
    ):
        return None
    if plan.client_namespaces or plan.client_functions:
        return "client_tools_need_responses_function_bridge"
    return None

def _responses_function_tool_bridge_preemptive_kwargs(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if _responses_request_module._request_is_codex_compaction(request_kwargs):
        return None
    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    # Check the selected route before honoring an inherited preemptive marker.
    # LiteLLM may reuse the original Responses kwargs for a fallback callback;
    # that marker must not force a Responses function bridge onto a Chat route.
    if _current_route_responses_endpoint_unsupported(
        request_kwargs,
        outer_for_tool_plan,
    ):
        return None
    if _request_has_preemptive_responses_function_tool_bridge(request_kwargs):
        return request_kwargs

    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    reason = _responses_function_tool_bridge_preemptive_reason(
        request_kwargs,
        outer_for_tool_plan,
        plan=plan,
    )
    if reason is None:
        return None

    bridge_kwargs = request_kwargs.copy()
    litellm_metadata = _request_context_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
    bridge_metadata = litellm_metadata.copy()
    bridge_metadata[_RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY] = True
    bridge_metadata[_RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY] = True
    bridge_metadata["responses_function_tool_bridge_preemptive_reason"] = (
        reason
    )
    _responses_execution_module._remember_responses_chat_bridge_model_group(
        bridge_metadata,
        request_kwargs,
        outer_request_kwargs,
    )
    _with_responses_function_tool_bridge_compatible_tools(
        bridge_kwargs,
        bridge_metadata,
        outer_for_tool_plan,
    )
    bridge_input, input_stats = _responses_tools_module._responses_chat_bridge_input(
        bridge_kwargs.get("input")
    )
    if input_stats.get("changed"):
        bridge_kwargs["input"] = bridge_input
        bridge_metadata["responses_function_tool_bridge_input_sanitized"] = (
            input_stats
        )
    bridge_kwargs["litellm_metadata"] = bridge_metadata
    return bridge_kwargs


def _request_has_responses_client_tools_requiring_bridge(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        if not isinstance(request, dict):
            continue
        tools = request.get("tools")
        candidates = list(tools) if isinstance(tools, list) else []
        candidates.extend(
            _responses_tools_module._responses_input_tool_search_output_tools(
                request.get("input")
            )
        )
        candidates.extend(
            _responses_tools_module._responses_input_additional_tools(
                request.get("input")
            )
        )
        if any(
            isinstance(tool, dict)
            and tool.get("type") in {"namespace", "custom", "tool_search"}
            for tool in candidates
        ):
            return True
    return False


def _request_already_retried_forced_tool_choice_as_auto(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    """Whether this request already exhausted its narrow auto-choice retry."""

    for request in (request_kwargs, outer_request_kwargs):
        metadata = _request_context_module._request_metadata_dict(
            request,
            "litellm_metadata",
        ) or {}
        if metadata.get("responses_forced_tool_choice_auto_retry") is True:
            return True
    return False


def _native_responses_client_tools_unsupported_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if _routing_module._exception_status_code(exception) not in {400, 422}:
        return False
    if _routing_module._is_terminal_prompt_or_policy_error(exception):
        return False
    if not _request_has_responses_client_tools_requiring_bridge(
        request_kwargs,
        outer_request_kwargs,
    ):
        return False

    text = _routing_module._exception_text(exception)
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "authentication",
            "api key",
            "unauthorized",
            "permission denied",
            "insufficient_quota",
            "insufficient quota",
            "quota exceeded",
            "rate limit",
            "too many requests",
            "billing",
            "connection error",
            "connection refused",
            "network error",
            "timed out",
            "timeout",
        )
    ):
        return False

    # Some OpenAI-compatible gateways report an unsupported namespace/custom
    # tool only as a generic invalid-parameter-combination error.  Do not
    # reinterpret that broad 400 on the first attempt: first relax an
    # explicitly forced choice on the native protocol.  If that exact retry
    # has already failed, a same-protocol function-tool bridge is the next
    # compatible representation to try before changing protocol surfaces.
    if _request_already_retried_forced_tool_choice_as_auto(
        request_kwargs,
        outer_request_kwargs,
    ) and any(
        marker in text
        for marker in (
            "请求参数组合无效",
            "invalid parameter combination",
            "invalid parameters combination",
            "invalid combination of parameters",
            "invalid request parameter combination",
        )
    ):
        return True

    has_client_tool_marker = any(
        marker in text
        for marker in (
            "namespace",
            "tool_search",
            "tool search",
            "additional_tools",
            "additional tools",
            "custom tool",
            '"custom"',
            "'custom'",
            "`custom`",
            "type=custom",
            "type: custom",
        )
    )
    if not has_client_tool_marker:
        return False

    return any(
        marker in text
        for marker in (
            "no endpoints found that support",
            "unsupported tool",
            "unsupported tool type",
            "tool type is unsupported",
            "tool type is not supported",
            "tool type not supported",
            "tool is not supported",
            "tools are not supported",
            "does not support tool",
            "doesn't support tool",
            "unknown tool",
            "unrecognized tool",
            "invalid tool",
            "invalid tool type",
            "invalid_union",
            "invalid_type",
            "invalid value",
            "invalid_value",
            "literal_error",
            "expected one of",
            "must be one of",
            "should be one of",
            "supported values",
            "allowed values",
            "extra_forbidden",
            "extra inputs are not permitted",
        )
    )


def _responses_function_tool_bridge_retry_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    if _responses_request_module._request_is_codex_compaction(request_kwargs):
        return None
    if _request_has_responses_function_tool_bridge_attempt(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    if not _request_supports_responses_function_tools(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    if not _native_responses_client_tools_unsupported_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    if _computer_facade_module._request_hosted_browser_computer_blocks_chat_bridge(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None

    bridge_kwargs = request_kwargs.copy()
    litellm_metadata = (
        _request_context_module._request_metadata_dict(
            bridge_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    bridge_metadata = litellm_metadata.copy()
    bridge_metadata[_RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY] = True
    bridge_metadata[_RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY] = (
        "native_client_tools_unsupported"
    )
    bridge_metadata[
        "responses_function_tool_bridge_native_error_fallback"
    ] = True
    _responses_execution_module._remember_responses_chat_bridge_model_group(
        bridge_metadata,
        request_kwargs,
        outer_request_kwargs,
    )
    _with_responses_function_tool_bridge_compatible_tools(
        bridge_kwargs,
        bridge_metadata,
        outer_request_kwargs,
    )
    bridge_input, input_stats = (
        _responses_tools_module._responses_chat_bridge_input(
            bridge_kwargs.get("input")
        )
    )
    if input_stats.get("changed"):
        bridge_kwargs["input"] = bridge_input
        bridge_metadata[
            "responses_function_tool_bridge_input_sanitized"
        ] = input_stats
    bridge_kwargs["litellm_metadata"] = bridge_metadata
    return bridge_kwargs


def _normalized_upstream_url_surface(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    return text if text in {
        _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
        _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        _UPSTREAM_URL_SURFACE_ANTHROPIC,
    } else ""


def _with_preemptive_responses_chat_bridge(request_kwargs: dict) -> Optional[dict]:
    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    bridge_kwargs = _responses_chat_bridge_preemptive_kwargs(
        request_kwargs,
        include_hosted_web_search_unsupported=False,
        include_client_tool_unsupported=False,
    )
    if bridge_kwargs is None:
        return None
    bridge_metadata = _request_context_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
    _trace_module._route_trace(
        "responses_chat_bridge_preemptive",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        tool_types=_trace_module._trace_tool_types(bridge_kwargs.get("tools")),
        tool_names=_trace_module._trace_tool_names(bridge_kwargs.get("tools")),
        has_image_input=_image_inputs_module._request_has_image_input(request_kwargs),
        external_web_search_bridge=bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY),
        reason=bridge_metadata.get(
            "responses_chat_bridge_preemptive_reason"
        ),
    )
    return bridge_kwargs


def _request_already_attempted_responses_chat_bridge_empty_retry(
    request_kwargs: Optional[dict],
) -> bool:
    request_kwargs = request_kwargs or {}
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if (
            metadata is not None
            and metadata.get(_RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY) is True
        ):
            return True
    return False


def _append_responses_chat_bridge_empty_retry_instruction(retry_kwargs: dict) -> None:
    note = (
        "Responses compatibility note: the previous bridged chat response was empty. "
        "Return a non-empty assistant message for the user, or call an available tool "
        "if more work is required. Do not return an empty message."
    )
    existing = retry_kwargs.get("instructions")
    if isinstance(existing, str) and existing.strip():
        if note not in existing:
            retry_kwargs["instructions"] = f"{existing.rstrip()}\n\n{note}"
    else:
        retry_kwargs["instructions"] = note


def _responses_chat_bridge_empty_retry_kwargs(
    bridge_kwargs: dict,
) -> Optional[dict]:
    if _request_already_attempted_responses_chat_bridge_empty_retry(bridge_kwargs):
        return None
    retry_kwargs = bridge_kwargs.copy()
    litellm_metadata = _request_context_module._request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    retry_kwargs["use_chat_completions_api"] = True
    _append_responses_chat_bridge_empty_retry_instruction(retry_kwargs)
    return retry_kwargs


def _responses_chat_bridge_empty_success_exception(request_kwargs: dict) -> Exception:
    model_group = _responses_execution_module._request_model_group(request_kwargs) or _request_context_module._request_model_for_error(request_kwargs)
    message = (
        "Responses chat bridge returned an empty assistant response for "
        f"{model_group or 'the requested model'} after retry; treating it as an "
        "upstream failure instead of completing the Codex turn."
    )
    error_cls = getattr(
        litellm,
        "ServiceUnavailableError",
        getattr(litellm, "InternalServerError", RuntimeError),
    )
    try:
        exception = error_cls(
            message=message,
            model=model_group or "",
            llm_provider="litellm-menu",
        )
    except TypeError:
        exception = RuntimeError(message)
    try:
        exception.status_code = 503  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        exception.responses_chat_bridge_empty = True  # type: ignore[attr-defined]
    except Exception:
        pass
    _routing_module._mark_exception_for_deployment_failover(exception, request_kwargs)
    return exception


def _response_has_empty_check_shape(response: Any) -> bool:
    if isinstance(response, dict):
        return any(
            key in response
            for key in ("output", "output_text", "choices", "content", "message")
        )
    for key in ("output", "output_text", "choices", "content", "message"):
        if hasattr(response, key):
            return True
    if hasattr(response, "model_dump"):
        try:
            dumped = response.model_dump()
        except Exception:
            return False
        return isinstance(dumped, dict) and _response_has_empty_check_shape(dumped)
    return False


async def _ensure_responses_chat_bridge_non_empty_response(
    response: Any,
    bridge_kwargs: dict,
    bridge_metadata: dict,
    original_function: Any,
) -> Any:
    response = _image_inputs_module._sanitize_response_echoed_request_images(response, bridge_kwargs)
    if (
        _tools_module._request_has_image_generation_tool(bridge_kwargs)
        or not _response_has_empty_check_shape(response)
        or not _responses_output_module._response_is_effectively_empty(response)
    ):
        return response

    retry_kwargs = _responses_chat_bridge_empty_retry_kwargs(bridge_kwargs)
    if retry_kwargs is not None:
        _trace_module._route_trace(
            "responses_chat_bridge_empty_retry_start",
            request_id=_routing_module._trace_request_id(bridge_kwargs),
            session=_routing_module._trace_session_context(bridge_kwargs),
            model_group=_responses_execution_module._request_model_group(bridge_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(bridge_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(bridge_kwargs),
        )
        try:
            retry_response = original_function(**retry_kwargs)
            if inspect.isawaitable(retry_response):
                retry_response = await retry_response
            retry_response = _image_inputs_module._sanitize_response_echoed_request_images(
                retry_response,
                retry_kwargs,
            )
            if bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
                retry_response = await _responses_web_search_bridge_module._resolve_web_search_function_calls(
                    retry_response,
                    retry_kwargs,
                    original_function,
                )
        except Exception as exc:
            _trace_module._route_trace(
                "responses_chat_bridge_empty_retry_error",
                request_id=_routing_module._trace_request_id(bridge_kwargs),
                session=_routing_module._trace_session_context(bridge_kwargs),
                model_group=_responses_execution_module._request_model_group(bridge_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(bridge_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(bridge_kwargs),
                exception=_routing_module._trace_exception(exc),
            )
            raise
        if (
            not _response_has_empty_check_shape(retry_response)
            or not _responses_output_module._response_is_effectively_empty(retry_response)
        ):
            _trace_module._route_trace(
                "responses_chat_bridge_empty_retry_success",
                request_id=_routing_module._trace_request_id(bridge_kwargs),
                session=_routing_module._trace_session_context(bridge_kwargs),
                model_group=_responses_execution_module._request_model_group(bridge_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(bridge_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(bridge_kwargs),
                response_types=_responses_output_module._response_types(retry_response),
            )
            return retry_response
        response = retry_response

    _trace_module._route_trace(
        "responses_chat_bridge_empty_response",
        request_id=_routing_module._trace_request_id(bridge_kwargs),
        session=_routing_module._trace_session_context(bridge_kwargs),
        model_group=_responses_execution_module._request_model_group(bridge_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(bridge_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(bridge_kwargs),
        response_types=_responses_output_module._response_types(response),
    )
    raise _responses_chat_bridge_empty_success_exception(bridge_kwargs)
