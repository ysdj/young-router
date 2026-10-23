from __future__ import annotations

from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_surfaces as _responses_surfaces_module
from . import responses_tools as _responses_tools_module


from .base import (
    Any,
    List,
    Optional,
    os,
    _BROWSER_COMPUTER_CLIENT_FUNCTION_NAMES,
    _BROWSER_COMPUTER_CLIENT_NAMESPACE_NAMES,
    _HOSTED_COMPUTER_TOOL_TYPES,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
    _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
    _WEB_SEARCH_NATIVE_EVENT_SEEN_METADATA_KEY,
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
        and tool.get("type")
        in (
            {"web_search", "web_search_preview"}
            | _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES
        )
        for tool in tools
    )


def _request_has_web_search_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _tools_include_web_search(request_kwargs.get("tools"))


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
    """True when this request carries the client's own computer/browser tools.

    Codex Desktop declares its computer use inside leading ``additional_tools``
    input items (the ``mcp__cua_repl``/``mcp__node_repl`` repl namespaces), so
    the input items count exactly like top-level tools here: they are the
    client's executor and must keep working across a bridged request.
    """

    request_kwargs = request_kwargs or {}
    if _tools_include_browser_computer_client_tool(request_kwargs.get("tools")):
        return True
    if _tools_include_browser_computer_client_tool(
        _responses_tools_module._responses_input_additional_tools(
            request_kwargs.get("input")
        )
    ):
        return True
    for discovered_tool in _responses_tools_module._responses_input_tool_search_output_tools(
        request_kwargs.get("input")
    ):
        if _tools_include_browser_computer_client_tool([discovered_tool]):
            return True
    return False
