from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_surfaces as _responses_surfaces_module
from . import responses_tools as _responses_tools_module


from .base import (
    Any,
    List,
    Optional,
    _BROWSER_COMPUTER_CLIENT_FUNCTION_NAMES,
    _BROWSER_COMPUTER_CLIENT_NAMESPACE_NAMES,
    _HOSTED_BROWSER_COMPUTER_TOOL_TYPES,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
    _WEB_SEARCH_BRIDGE_FUNCTION_NAME,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY,
)



def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or part.get("input_text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def _tools_include_image_generation(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    return any(isinstance(tool, dict) and tool.get("type") == "image_generation" for tool in tools)


def _request_has_image_generation_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _tools_include_image_generation(request_kwargs.get("tools"))


def _tools_include_web_search(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and tool.get("type") in {"web_search", "web_search_preview"}
        for tool in tools
    )


def _tools_include_litellm_web_search_bridge(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME:
            return True
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME:
            return True
    return False


def _request_has_web_search_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _tools_include_web_search(request_kwargs.get("tools"))


def _request_has_litellm_web_search_bridge(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _tools_include_litellm_web_search_bridge(request_kwargs.get("tools"))


def _request_is_external_web_search_synthesis(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key) or {}
        if metadata.get("external_web_search_synthesis") is True:
            return True
    return False


def _request_suppresses_external_web_search_post_call(
    request_kwargs: Optional[dict],
) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key) or {}
        if metadata.get(_WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY) is True:
            return True
    return False


def _request_is_unmarked_internal_web_search_bridge_post_call(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if not _request_has_litellm_web_search_bridge(request_kwargs):
        return False
    if _request_has_web_search_tool(request_kwargs):
        return False
    if _request_should_intercept_external_web_search(request_kwargs):
        return False
    return (
        _image_generation_module._request_is_responses_api(request_kwargs)
        or _responses_surfaces_module._request_uses_responses_endpoint(request_kwargs)
    )


def _with_external_web_search_post_call_suppressed(
    request_kwargs: dict[str, Any],
) -> dict[str, Any]:
    suppressed_kwargs = request_kwargs.copy()
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key) or {}
        suppressed_metadata = metadata.copy()
        suppressed_metadata[_WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY] = True
        suppressed_kwargs[key] = suppressed_metadata
    return suppressed_kwargs


def _request_should_intercept_external_web_search(request_kwargs: Optional[dict]) -> bool:
    if not _request_has_litellm_web_search_bridge(request_kwargs):
        return False
    if not isinstance(request_kwargs, dict):
        return False
    metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    return bool(
        metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True
        or metadata.get(_RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY) is True
        or metadata.get(_RESPONSES_CHAT_BRIDGE_METADATA_KEY) is True
        or request_kwargs.get("use_chat_completions_api") is True
    )


def _request_should_consume_litellm_web_search_function_call(
    request_kwargs: Optional[dict],
) -> bool:
    if _request_should_intercept_external_web_search(request_kwargs):
        return True
    if not isinstance(request_kwargs, dict):
        return False
    if _request_suppresses_external_web_search_post_call(request_kwargs):
        return False
    if _request_is_external_web_search_synthesis(request_kwargs):
        return False
    if not (
        _request_has_litellm_web_search_bridge(request_kwargs)
        or _request_has_web_search_tool(request_kwargs)
    ):
        return False
    return (
        _image_generation_module._request_is_responses_api(request_kwargs)
        or _responses_surfaces_module._request_uses_responses_endpoint(request_kwargs)
    )


def _hosted_browser_computer_tool_types(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    types: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type in _HOSTED_BROWSER_COMPUTER_TOOL_TYPES and tool_type not in types:
            types.append(tool_type)
    return types


def _request_hosted_browser_computer_tool_types(
    request_kwargs: Optional[dict],
) -> list[str]:
    request_kwargs = request_kwargs or {}
    return _hosted_browser_computer_tool_types(request_kwargs.get("tools"))


def _tools_include_browser_computer_client_tool(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        name = tool.get("name")
        name_normalized = name.lower() if isinstance(name, str) else ""
        if tool_type == "namespace":
            if name_normalized in _BROWSER_COMPUTER_CLIENT_NAMESPACE_NAMES:
                return True
            child_tools = tool.get("tools")
            if _tools_include_browser_computer_client_tool(child_tools):
                return True
        elif tool_type == "function":
            function = tool.get("function")
            function_dict = function if isinstance(function, dict) else {}
            function_name = function_dict.get("name") or tool.get("name")
            if (
                isinstance(function_name, str)
                and function_name in _BROWSER_COMPUTER_CLIENT_FUNCTION_NAMES
            ):
                return True
    return False


def _request_has_browser_computer_client_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    if _tools_include_browser_computer_client_tool(request_kwargs.get("tools")):
        return True
    for discovered_tool in _responses_tools_module._responses_input_tool_search_output_tools(
        request_kwargs.get("input")
    ):
        if _tools_include_browser_computer_client_tool([discovered_tool]):
            return True
    return False
