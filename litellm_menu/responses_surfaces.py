from __future__ import annotations

from . import computer_facade as _computer_facade_module
from . import image_generation as _image_generation_module
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
    _RESPONSES_ENDPOINT_SUPPORT_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _SUPPORTED_UPSTREAM_URL_SURFACES_KEY,
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
    _UPSTREAM_URL_SURFACE_PREFERENCE,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
    inspect,
    litellm,
    re,
)


def _with_responses_chat_bridge_compatible_tools(
    retry_kwargs: dict,
    retry_metadata: dict,
) -> None:
    discovered_tools = _responses_tools_module._responses_input_tool_search_output_tools(retry_kwargs.get("input"))
    if "tools" not in retry_kwargs and not discovered_tools:
        return

    tools = retry_kwargs.get("tools")
    if not isinstance(tools, list):
        tools = []
    sanitized_tools, web_search_options, stats = _responses_tools_module._responses_chat_bridge_sanitize_tools(
        tools,
        input_value=retry_kwargs.get("input"),
    )
    if stats.get("changed"):
        retry_metadata["responses_chat_bridge_tool_sanitized"] = stats
    if stats.get("bridged_web_search_tools"):
        retry_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_KEY] = True
        retry_kwargs.pop("web_search_options", None)
        if retry_kwargs.get("stream") is True:
            retry_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY] = True
            retry_kwargs["stream"] = False
    _responses_tools_module._append_responses_chat_bridge_instruction(retry_kwargs, stats)
    _responses_tools_module._append_external_web_search_bridge_instruction(retry_kwargs, stats)
    if web_search_options is not None and not stats.get("bridged_web_search_tools"):
        retry_kwargs["web_search_options"] = web_search_options
    if sanitized_tools:
        retry_kwargs["tools"] = sanitized_tools
        kept_tool_names = {
            tool["name"]
            for tool in sanitized_tools
            if isinstance(tool.get("name"), str)
        }
        if "tool_choice" in retry_kwargs:
            retry_kwargs["tool_choice"] = _responses_tools_module._responses_chat_bridge_sanitize_tool_choice(
                retry_kwargs.get("tool_choice"),
                kept_tool_names,
            )
        return

    retry_kwargs.pop("tools", None)
    retry_kwargs.pop("tool_choice", None)
    retry_kwargs.pop("parallel_tool_calls", None)


def _with_responses_function_tool_bridge_compatible_tools(
    bridge_kwargs: dict,
    bridge_metadata: dict,
    outer_request_kwargs: Optional[dict] = None,
) -> None:
    discovered_tools = _responses_tools_module._responses_input_tool_search_output_tools(bridge_kwargs.get("input"))
    if "tools" not in bridge_kwargs and not discovered_tools:
        return

    tools = bridge_kwargs.get("tools")
    if not isinstance(tools, list):
        tools = []
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
    if stats.get("bridged_web_search_tools"):
        bridge_metadata[_WEB_SEARCH_EXTERNAL_BRIDGE_KEY] = True
        bridge_kwargs.pop("web_search_options", None)
    _responses_tools_module._append_responses_chat_bridge_instruction(bridge_kwargs, stats)
    _responses_tools_module._append_external_web_search_bridge_instruction(bridge_kwargs, stats)
    if web_search_options is not None and not stats.get("bridged_web_search_tools"):
        bridge_kwargs["web_search_options"] = web_search_options
    if sanitized_tools:
        bridge_kwargs["tools"] = sanitized_tools
        kept_tool_names = {
            tool["name"]
            for tool in sanitized_tools
            if isinstance(tool.get("name"), str)
        }
        if "tool_choice" in bridge_kwargs:
            tool_choice = bridge_kwargs.get("tool_choice")
            if not (
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


def _responses_chat_bridge_retry_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if _image_generation_module._request_already_attempted_responses_chat_bridge(
        request_kwargs
    ) or _image_generation_module._request_already_attempted_responses_chat_bridge(outer_request_kwargs):
        return None
    retry_reason = _responses_chat_bridge_retry_reason(
        exception,
        request_kwargs,
        outer_request_kwargs,
    )
    if retry_reason is None:
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
    litellm_metadata = _image_generation_module._request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_RESPONSES_CHAT_BRIDGE_METADATA_KEY] = True
    if retry_reason != "responses_endpoint_not_found":
        retry_metadata[_RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY] = retry_reason
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
            _image_generation_module._request_is_responses_api(request_kwargs)
            or _image_generation_module._request_is_responses_api(outer_request_kwargs)
        )
        and _routing_module._is_responses_schema_unsupported_error(exception)
    ):
        return "responses_schema_unsupported"
    return None


def _request_configured_responses_endpoint_unsupported(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    model_info = _image_generation_module._request_model_info(request_kwargs)
    mode = _normalized_upstream_url_surface(model_info.get(_UPSTREAM_URL_SURFACE_KEY))
    if mode == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return model_info.get(_RESPONSES_ENDPOINT_SUPPORT_KEY) is False
    if mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True
    return model_info.get(_RESPONSES_ENDPOINT_SUPPORT_KEY) is False


def _request_has_explicit_surface_metadata(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    model_info = _image_generation_module._request_model_info(request_kwargs)
    return any(
        key in model_info
        for key in (
            _UPSTREAM_URL_SURFACE_KEY,
            _SUPPORTED_UPSTREAM_URL_SURFACES_KEY,
            _RESPONSES_ENDPOINT_SUPPORT_KEY,
        )
    )


def _current_route_responses_endpoint_unsupported(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if _request_has_explicit_surface_metadata(request_kwargs):
        return _request_configured_responses_endpoint_unsupported(request_kwargs)
    return _request_configured_responses_endpoint_unsupported(
        request_kwargs
    ) or _request_configured_responses_endpoint_unsupported(outer_request_kwargs)


def _model_info_has_chat_bridge_mode(model_info: dict) -> bool:
    mode = _normalized_upstream_url_surface(model_info.get(_UPSTREAM_URL_SURFACE_KEY))
    if mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True
    if mode == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return False
    return model_info.get(_RESPONSES_ENDPOINT_SUPPORT_KEY) is False


def _request_has_chat_bridge_mode(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        model_info = _image_generation_module._request_model_info(request)
        if model_info and _model_info_has_chat_bridge_mode(model_info):
            return True
    return False


def _request_is_direct_openai_route(request_kwargs: Optional[dict]) -> bool:
    model_info = _image_generation_module._request_model_info(request_kwargs)
    provider = model_info.get("provider")
    host = _image_generation_module._api_base_host(_image_generation_module._request_api_base(request_kwargs))
    if host:
        return host == "api.openai.com"
    if isinstance(provider, str) and provider.strip().lower() == "openai":
        return True
    return False


def _request_supports_native_responses_hosted_tools(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        model_info = _image_generation_module._request_model_info(request)
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
    model_info = _image_generation_module._request_model_info(request_kwargs)
    mode = _normalized_upstream_url_surface(model_info.get(_UPSTREAM_URL_SURFACE_KEY))
    if mode:
        if mode == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
            return model_info.get(_RESPONSES_ENDPOINT_SUPPORT_KEY) is not False
        if mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
            return False
    if model_info.get(_RESPONSES_ENDPOINT_SUPPORT_KEY) is False:
        return False
    return _image_generation_module._request_is_responses_api(request_kwargs)


def _request_supports_native_responses_web_search(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
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
    model_info = _image_generation_module._request_model_info(request_kwargs)
    if model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is True:
        return True
    if model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is True:
        return True
    if model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is False:
        return False
    if model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is False:
        return False
    if _request_is_direct_openai_route(request_kwargs):
        return True
    return None


def _request_web_search_support_is_unknown(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    model_info = _image_generation_module._request_model_info(request_kwargs)
    return (
        model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is None
        and model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is None
    )


def _request_should_try_unknown_native_responses_web_search(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
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
    if not plan.hosted_web_search:
        return False
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


def _request_supports_native_responses_client_tools(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        model_info = _image_generation_module._request_model_info(request)
        if model_info.get(_SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY) is True:
            return True
        if _request_is_direct_openai_route(request):
            return True
    return False


def _request_supports_responses_function_tools(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    for request in (request_kwargs, outer_request_kwargs):
        model_info = _image_generation_module._request_model_info(request)
        if model_info.get(_SUPPORTS_RESPONSES_FUNCTION_TOOLS_KEY) is True:
            return True
        if _request_uses_responses_endpoint(request):
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
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
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
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
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
            metadata = _image_generation_module._request_metadata_dict(request, key)
            if metadata is None:
                continue
            if metadata.get(_RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY) is True:
                return True
            if metadata.get(_RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY) is True:
                return True
    return False


def _responses_external_web_search_bridge_kwargs(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    plan: Optional[HostedToolPlan] = None,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _image_generation_module._request_is_responses_api(request_kwargs):
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
    if not plan.hosted_web_search:
        return None
    if plan.hosted_computer:
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

    litellm_metadata = _image_generation_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
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
    _responses_tools_module._append_external_web_search_bridge_instruction(bridge_kwargs, stats)
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
    return _responses_external_web_search_bridge_kwargs(
        request_kwargs,
        outer_request_kwargs,
        plan=plan,
    )


def _native_responses_web_search_unsupported_error(exception: Exception) -> bool:
    return _routing_module._is_native_responses_web_search_unsupported_error(exception)


def _with_responses_external_web_search_bridge_after_native_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    if not _native_responses_web_search_unsupported_error(exception):
        return None
    if _tools_module._request_should_intercept_external_web_search(request_kwargs):
        return None
    bridge_kwargs = _responses_external_web_search_bridge_kwargs(
        request_kwargs,
        outer_request_kwargs,
    )
    if bridge_kwargs is None:
        return None
    bridge_metadata = _image_generation_module._request_metadata_dict(
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
    if not _image_generation_module._request_is_responses_api(request_kwargs):
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
    if not plan.hosted_web_search:
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
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
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
        return "responses_endpoint_unsupported"
    return None


def _responses_chat_bridge_preemptive_kwargs(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    include_hosted_web_search_unsupported: bool = False,
    include_client_tool_unsupported: bool = False,
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if _request_has_preemptive_responses_chat_bridge(request_kwargs):
        return request_kwargs
    if _image_generation_module._request_already_attempted_responses_chat_bridge(request_kwargs):
        return None

    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    reason = _responses_chat_bridge_preemptive_reason(
        request_kwargs,
        outer_for_tool_plan,
        include_hosted_web_search_unsupported=include_hosted_web_search_unsupported,
        include_client_tool_unsupported=include_client_tool_unsupported,
        plan=plan,
    )
    if reason is None:
        return None
    bridge_kwargs = request_kwargs.copy()
    litellm_metadata = _image_generation_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
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
    _with_responses_chat_bridge_compatible_tools(bridge_kwargs, bridge_metadata)
    bridge_kwargs["litellm_metadata"] = bridge_metadata
    bridge_kwargs["use_chat_completions_api"] = True
    return bridge_kwargs


def _responses_function_tool_bridge_preemptive_reason(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
    *,
    plan: Optional[HostedToolPlan] = None,
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    if not _image_generation_module._request_is_responses_api(request_kwargs):
        return None
    if _image_generation_module._request_is_codex_compaction(request_kwargs):
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
    plan = plan or _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_reason)
    if _computer_facade_module._request_hosted_browser_computer_blocks_chat_bridge(
        request_kwargs,
        outer_for_tool_reason,
    ):
        return None
    if _request_supports_native_responses_client_tools(
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
    if _image_generation_module._request_is_codex_compaction(request_kwargs):
        return None
    if _request_has_preemptive_responses_function_tool_bridge(request_kwargs):
        return request_kwargs

    outer_for_tool_plan = (
        None
        if _tools_module._request_is_external_web_search_synthesis(request_kwargs)
        else outer_request_kwargs
    )
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_for_tool_plan)
    reason = _responses_function_tool_bridge_preemptive_reason(
        request_kwargs,
        outer_for_tool_plan,
        plan=plan,
    )
    if reason is None:
        return None

    bridge_kwargs = request_kwargs.copy()
    litellm_metadata = _image_generation_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
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
    bridge_kwargs["litellm_metadata"] = bridge_metadata
    return bridge_kwargs


def _normalized_upstream_url_surfaces(value: Any) -> List[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[,;\s]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    modes: List[str] = []
    for item in raw_items:
        mode = _normalized_upstream_url_surface(item)
        if mode and mode not in modes:
            modes.append(mode)
    return modes


def _effective_upstream_url_surface(modes: List[str]) -> str:
    for preferred in _UPSTREAM_URL_SURFACE_PREFERENCE:
        if preferred in modes:
            return preferred
    return modes[0] if modes else ""


def _normalized_upstream_url_surface(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    aliases = {
        "openai_chat": _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        "openai_chat_completions": _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        "openai-chat": _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        "chat": _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        "chat_completions": _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        "openai_responses": _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
        "openai-responses": _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
        "responses": _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
        "anthropic_messages": _UPSTREAM_URL_SURFACE_ANTHROPIC,
        "anthropic/messages": _UPSTREAM_URL_SURFACE_ANTHROPIC,
        "claude": _UPSTREAM_URL_SURFACE_ANTHROPIC,
    }
    return aliases.get(text, text)


def _with_preemptive_responses_chat_bridge(request_kwargs: dict) -> Optional[dict]:
    if not _image_generation_module._request_is_responses_api(request_kwargs):
        return None
    bridge_kwargs = _responses_chat_bridge_preemptive_kwargs(
        request_kwargs,
        include_hosted_web_search_unsupported=False,
        include_client_tool_unsupported=False,
    )
    if bridge_kwargs is None:
        return None
    bridge_metadata = _image_generation_module._request_metadata_dict(bridge_kwargs, "litellm_metadata") or {}
    _trace_module._route_trace(
        "responses_chat_bridge_preemptive",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        tool_types=_trace_module._trace_tool_types(bridge_kwargs.get("tools")),
        tool_names=_trace_module._trace_tool_names(bridge_kwargs.get("tools")),
        has_image_input=_image_generation_module._request_has_image_input(request_kwargs),
        external_web_search_bridge=bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY),
        reason=bridge_metadata.get(
            "responses_chat_bridge_preemptive_reason"
        ),
    )
    return bridge_kwargs


def _with_preemptive_responses_chat_bridge_for_call_type(
    request_kwargs: dict,
    call_type: Any,
) -> Optional[dict]:
    if _image_generation_module._request_is_responses_api(request_kwargs):
        return _with_preemptive_responses_chat_bridge(request_kwargs)
    if not isinstance(call_type, str) or call_type.lower() not in {"responses", "aresponses"}:
        return None
    bridge_view = request_kwargs.copy()
    bridge_view["call_type"] = call_type
    return _with_preemptive_responses_chat_bridge(bridge_view)


def _request_already_attempted_responses_chat_bridge_empty_retry(
    request_kwargs: Optional[dict],
) -> bool:
    request_kwargs = request_kwargs or {}
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
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
    litellm_metadata = _image_generation_module._request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    retry_kwargs["use_chat_completions_api"] = True
    _append_responses_chat_bridge_empty_retry_instruction(retry_kwargs)
    return retry_kwargs


def _responses_chat_bridge_empty_success_exception(request_kwargs: dict) -> Exception:
    model_group = _responses_execution_module._request_model_group(request_kwargs) or _image_generation_module._request_model_for_error(request_kwargs)
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
    response = _image_generation_module._sanitize_response_echoed_request_images(response, bridge_kwargs)
    if (
        _tools_module._request_has_image_generation_tool(bridge_kwargs)
        or not _response_has_empty_check_shape(response)
        or not _image_generation_module._response_is_effectively_empty(response)
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
            retry_response = _image_generation_module._sanitize_response_echoed_request_images(
                retry_response,
                retry_kwargs,
            )
            if bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
                retry_response = await _responses_web_search_bridge_module._resolve_litellm_web_search_function_calls(
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
            or not _image_generation_module._response_is_effectively_empty(retry_response)
        ):
            _trace_module._route_trace(
                "responses_chat_bridge_empty_retry_success",
                request_id=_routing_module._trace_request_id(bridge_kwargs),
                session=_routing_module._trace_session_context(bridge_kwargs),
                model_group=_responses_execution_module._request_model_group(bridge_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(bridge_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(bridge_kwargs),
                response_types=_image_generation_module._response_types(retry_response),
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
        response_types=_image_generation_module._response_types(response),
    )
    raise _responses_chat_bridge_empty_success_exception(bridge_kwargs)
