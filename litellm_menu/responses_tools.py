from __future__ import annotations

from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    HostedToolPlan,
    Optional,
    _COMPUTER_FACADE_BROWSER_BACKEND,
    _COMPUTER_FACADE_CHROME_BACKEND,
    _COMPUTER_FACADE_MCP_BACKEND,
    _HOSTED_GA_COMPUTER_TOOL_TYPES,
    _HOSTED_WEB_SEARCH_TOOL_TYPES,
    _RESPONSES_BRIDGE_CUSTOM_TOOL_KEY,
    _RESPONSES_BRIDGE_NAMESPACE_KEY,
    _TOOL_SEARCH_BRIDGE_FUNCTION_NAME,
    _WEB_SEARCH_BRIDGE_FUNCTION_NAME,
    copy,
    re,
)



def _responses_bridge_function_tool(tool: Any) -> Optional[dict]:
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return None

    function = tool.get("function")
    function_dict = function if isinstance(function, dict) else {}
    name = _routing_module._valid_chat_tool_name(function_dict.get("name") or tool.get("name"))
    if name is None:
        return None

    parameters = function_dict.get("parameters")
    if parameters is None:
        parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}

    converted: dict[str, Any] = {
        "type": "function",
        "name": name,
        "parameters": parameters.copy(),
    }
    description = _responses_bridge_tool_description(tool)
    if isinstance(description, str):
        converted["description"] = description

    strict = function_dict.get("strict")
    if strict is None:
        strict = tool.get("strict")
    if isinstance(strict, bool):
        converted["strict"] = strict

    # LiteLLM preserves a few Responses function-tool extensions during its
    # own Responses->Chat conversion. Keep only the safe, documented ones.
    for key in ("cache_control", "defer_loading", "allowed_callers", "input_examples"):
        if key in tool:
            converted[key] = tool[key]
    return converted


def _responses_bridge_custom_tool(tool: Any) -> Optional[dict]:
    if not isinstance(tool, dict) or tool.get("type") != "custom":
        return None

    name = _routing_module._valid_chat_tool_name(tool.get("name"))
    if name is None:
        return None

    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Raw custom tool input.",
                }
            },
            "required": ["input"],
            "additionalProperties": False,
        }

    converted: dict[str, Any] = {
        "type": "function",
        "name": name,
        "parameters": parameters.copy(),
        _RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True,
    }
    description = _responses_bridge_tool_description(tool)
    if isinstance(description, str):
        converted["description"] = description
    return converted


def _responses_bridge_tool_description(tool: dict, namespace: Optional[str] = None) -> Optional[str]:
    description = tool.get("description")
    if not isinstance(description, str):
        function = tool.get("function")
        function_dict = function if isinstance(function, dict) else {}
        description = function_dict.get("description")
    if not isinstance(description, str):
        description = ""
    description = " ".join(description.split())

    notes: list[str] = []
    function = tool.get("function")
    function_dict = function if isinstance(function, dict) else {}
    tool_name = _routing_module._valid_chat_tool_name(
        function_dict.get("name") or tool.get("name")
    )
    if tool_name in {"exec_command", "shell"}:
        notes.append(
            "Use this local shell tool to inspect repository files, list paths, "
            "search text, and run project commands."
        )
    elif tool_name == "write_stdin":
        notes.append("Use this only to continue a running exec_command session.")
    elif tool_name == "apply_patch":
        notes.append(
            "Use this for file edits; inspect files with local read/search commands first."
        )
    elif tool_name in {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}:
        notes.append("Use this for MCP resources, not normal workspace files.")

    if namespace:
        notes.append(
            f"This tool was originally exposed under the {namespace} namespace."
        )

    if not notes:
        return description or None
    note = " ".join(notes)
    if description:
        if note in description:
            return description
        return f"{description} {note}"
    return note


def _responses_bridge_tool_search_tool(tool: Any) -> Optional[dict]:
    if not isinstance(tool, dict) or tool.get("type") != "tool_search":
        return None

    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query for deferred tool discovery.",
                },
                "limit": {
                    "type": "number",
                    "description": "Maximum number of matching tools to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        description = (
            "Search the client-side deferred tool registry and return matching "
            "tool definitions, such as Codex sub-agent tools."
        )

    converted: dict[str, Any] = {
        "type": "function",
        "name": _TOOL_SEARCH_BRIDGE_FUNCTION_NAME,
        "description": description,
        "parameters": parameters.copy(),
    }
    if isinstance(tool.get("execution"), str):
        converted["x-litellm-menu-responses-tool-search-execution"] = tool["execution"]
    return converted


def _responses_bridge_namespace_tools(tool: Any) -> list[dict]:
    if not isinstance(tool, dict) or tool.get("type") != "namespace":
        return []
    child_tools = tool.get("tools")
    if not isinstance(child_tools, list):
        return []
    namespace = tool.get("name")
    namespace_name = namespace.strip() if isinstance(namespace, str) else ""

    converted_tools: list[dict] = []
    seen_names: set[str] = set()
    for child_tool in child_tools:
        converted = _responses_bridge_function_tool(child_tool)
        if converted is None:
            continue
        name = converted.get("name")
        if not isinstance(name, str) or name in seen_names:
            continue
        if namespace_name:
            converted[_RESPONSES_BRIDGE_NAMESPACE_KEY] = namespace_name
            description = _responses_bridge_tool_description(child_tool, namespace_name)
            if description:
                converted["description"] = description
        seen_names.add(name)
        converted_tools.append(converted)
    return converted_tools


def _responses_bridge_web_search_options(tool: Any) -> Optional[dict]:
    if not isinstance(tool, dict) or tool.get("type") not in {
        "web_search",
        "web_search_preview",
    }:
        return None

    options: dict[str, Any] = {}
    search_context_size = tool.get("search_context_size")
    if isinstance(search_context_size, str) and search_context_size in {
        "low",
        "medium",
        "high",
    }:
        options["search_context_size"] = search_context_size

    user_location = tool.get("user_location")
    if isinstance(user_location, dict):
        options["user_location"] = user_location.copy()

    return options


def _responses_bridge_web_search_tool(tool: Any) -> Optional[dict]:
    if not isinstance(tool, dict) or tool.get("type") not in {
        "web_search",
        "web_search_preview",
    }:
        return None
    return {
        "type": "function",
        "name": _WEB_SEARCH_BRIDGE_FUNCTION_NAME,
        "description": (
            "Use this compatibility web search function for external or current "
            "information. Provide query for a new lookup. Provide url to read a "
            "known result page. Provide url plus pattern to find text within a "
            "known page. Cite source URLs from the returned results. Do not use "
            "this for local machine state such as this Mac's macOS version, local "
            "files, installed apps, shell output, or current environment; use "
            "local tools first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The focused web search query to execute.",
                },
                "url": {
                    "type": "string",
                    "description": "A known source URL to read.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Text to find within the page at url.",
                },
            },
            "required": [],
        },
    }


def _responses_external_web_search_bridge_tools(
    tools: Any,
) -> tuple[Optional[list[dict]], dict[str, Any]]:
    if not isinstance(tools, list):
        return None, {"changed": False}

    bridged_tools: list[dict] = []
    bridged_web_search_tools = 0
    has_bridge_tool = _tools_module._tools_include_litellm_web_search_bridge(tools)
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") in {"web_search", "web_search_preview"}:
            bridged_web_search_tools += 1
            converted = _responses_bridge_web_search_tool(tool)
            if converted is not None and not has_bridge_tool:
                bridged_tools.append(converted)
                has_bridge_tool = True
            continue
        bridged_tools.append(copy.deepcopy(tool))

    changed = bridged_web_search_tools > 0 and bridged_tools != tools
    stats = {
        "changed": changed,
        "original_count": len(tools),
        "kept_count": len(bridged_tools),
        "bridged_web_search_tools": bridged_web_search_tools,
        "kept_tool_types": _trace_module._trace_tool_types(bridged_tools),
        "kept_tool_names": _trace_module._trace_tool_names(bridged_tools),
    }
    return bridged_tools if changed else None, stats


def _responses_external_web_search_bridge_tool_choice(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice.get("type") not in {"web_search", "web_search_preview"}:
        return tool_choice
    return _WEB_SEARCH_BRIDGE_FUNCTION_NAME


def _responses_input_tool_search_output_tool_names(value: Any) -> list[str]:
    names: list[str] = []

    def add_name(name: Any) -> None:
        if not isinstance(name, str):
            return
        normalized = name.strip()
        if normalized and normalized not in names:
            names.append(normalized)

    def visit_tool(tool: Any) -> None:
        if not isinstance(tool, dict):
            return
        add_name(tool.get("name"))
        child_tools = tool.get("tools")
        if isinstance(child_tools, list):
            for child_tool in child_tools:
                visit_tool(child_tool)

    for tool in _responses_input_tool_search_output_tools(value):
        visit_tool(tool)
    return names


def _responses_input_tool_search_output_tools(value: Any) -> list[dict]:
    tools: list[dict] = []

    def append_tool(tool: Any) -> None:
        if isinstance(tool, dict) and len(tools) < 80:
            tools.append(tool)

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 8 or len(tools) >= 80:
            return
        if isinstance(item, dict):
            if item.get("type") == "tool_search_output":
                output_tools = item.get("tools")
                if isinstance(output_tools, list):
                    for tool in output_tools:
                        append_tool(tool)
                return
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    visit(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, depth + 1)

    visit(value)
    return tools


def _responses_input_additional_tools(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []

    tools: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            continue
        item_tools = item.get("tools")
        if not isinstance(item_tools, list):
            continue
        for tool in item_tools:
            if isinstance(tool, dict):
                tools.append(tool)
    return tools


def _responses_chat_bridge_input(value: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(value, list):
        return value, {"changed": False, "dropped_tool_search_items": 0}

    filtered: list[Any] = []
    dropped_tool_search = 0
    dropped_additional_tools = 0
    for item in value:
        if isinstance(item, dict) and item.get("type") in {
            "tool_search_call",
            "tool_search_output",
        }:
            dropped_tool_search += 1
            continue
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            dropped_additional_tools += 1
            continue
        filtered.append(copy.deepcopy(item))

    if dropped_tool_search == 0 and dropped_additional_tools == 0:
        return value, {"changed": False, "dropped_tool_search_items": 0}
    stats = {
        "changed": True,
        "dropped_tool_search_items": dropped_tool_search,
    }
    if dropped_additional_tools:
        stats["dropped_additional_tools_items"] = dropped_additional_tools
    return filtered, stats


def _append_unique_string(target: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    normalized = value.strip()
    if normalized and normalized not in target:
        target.append(normalized)


def _responses_hosted_tool_plan(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> HostedToolPlan:
    hosted_web_search = False
    hosted_web_search_preview = False
    hosted_computer = False
    client_namespaces: list[str] = []
    client_functions: list[str] = []
    passthrough_tools: list[dict] = []
    hosted_computer_tools: list[dict] = []
    computer_environment: Optional[dict] = None

    def visit_tool(tool: Any) -> None:
        nonlocal hosted_web_search
        nonlocal hosted_web_search_preview
        nonlocal hosted_computer
        nonlocal computer_environment
        if not isinstance(tool, dict):
            return
        tool_type = tool.get("type")
        if tool_type in _HOSTED_WEB_SEARCH_TOOL_TYPES:
            hosted_web_search = True
            if tool_type == "web_search_preview":
                hosted_web_search_preview = True
            return
        if tool_type in _HOSTED_GA_COMPUTER_TOOL_TYPES:
            hosted_computer = True
            hosted_computer_tools.append(copy.deepcopy(tool))
            environment = tool.get("environment")
            if isinstance(environment, dict):
                computer_environment = copy.deepcopy(environment)
            return
        if tool_type == "namespace":
            _append_unique_string(client_namespaces, tool.get("name"))
            child_tools = tool.get("tools")
            if isinstance(child_tools, list):
                for child_tool in child_tools:
                    if not isinstance(child_tool, dict):
                        continue
                    function = child_tool.get("function")
                    function_dict = function if isinstance(function, dict) else {}
                    _append_unique_string(
                        client_functions,
                        function_dict.get("name") or child_tool.get("name"),
                    )
            passthrough_tools.append(tool)
            return

        if tool_type == "function":
            function = tool.get("function")
            function_dict = function if isinstance(function, dict) else {}
            _append_unique_string(
                client_functions,
                function_dict.get("name") or tool.get("name"),
            )
            passthrough_tools.append(tool)
            return

        if tool_type == "tool_search":
            _append_unique_string(client_functions, _TOOL_SEARCH_BRIDGE_FUNCTION_NAME)
            passthrough_tools.append(tool)
            return

        if tool_type == "custom":
            _append_unique_string(client_functions, tool.get("name"))
            passthrough_tools.append(tool)
            return

        passthrough_tools.append(tool)

    for request in (request_kwargs, outer_request_kwargs):
        if not isinstance(request, dict):
            continue
        if "web_search_options" in request:
            hosted_web_search = True
        tools = request.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                visit_tool(tool)
        for discovered_tool in _responses_input_tool_search_output_tools(request.get("input")):
            visit_tool(discovered_tool)
        for additional_tool in _responses_input_additional_tools(request.get("input")):
            visit_tool(additional_tool)

    facade_required = hosted_computer
    unsupported_reason: Optional[str] = None
    available_executor_hints: list[str] = []
    for namespace in client_namespaces:
        lowered = namespace.lower()
        if lowered == "mcp__computer_use":
            _append_unique_string(available_executor_hints, _COMPUTER_FACADE_MCP_BACKEND)
        elif lowered in {"browser", "browser_use", "mcp__browser", "mcp__browser_use"}:
            _append_unique_string(
                available_executor_hints,
                _COMPUTER_FACADE_BROWSER_BACKEND,
            )
        elif lowered in {"chrome", "chrome_browser", "mcp__chrome"}:
            _append_unique_string(
                available_executor_hints,
                _COMPUTER_FACADE_CHROME_BACKEND,
            )

    return HostedToolPlan(
        hosted_web_search=hosted_web_search,
        hosted_web_search_preview=hosted_web_search_preview,
        hosted_computer=hosted_computer,
        client_namespaces=client_namespaces,
        client_functions=client_functions,
        passthrough_tools=passthrough_tools,
        facade_required=facade_required,
        unsupported_reason=unsupported_reason,
        hosted_computer_tools=hosted_computer_tools,
        computer_environment=computer_environment,
        available_executor_hints=available_executor_hints,
    )


def _append_unique_chat_tool(
    target: list[dict],
    tool: dict,
    seen_names: set[str],
) -> bool:
    name = tool.get("name")
    if not isinstance(name, str) or name in seen_names:
        return False
    seen_names.add(name)
    target.append(tool)
    return True


def _responses_chat_bridge_sanitize_tools(
    tools: Any,
    *,
    input_value: Any = None,
    bridge_web_search: bool = True,
) -> tuple[Optional[list[dict]], Optional[dict], dict[str, Any]]:
    if not isinstance(tools, list):
        return None, None, {"changed": False}

    tool_search_output_tool_names = _responses_input_tool_search_output_tool_names(
        input_value
    )
    suppress_tool_search_bridge = bool(tool_search_output_tool_names)
    sanitized: list[dict] = []
    seen_tool_names: set[str] = set()
    web_search_options: Optional[dict] = None
    dropped_types: list[str] = []
    invalid_function_tools = 0
    bridged_tool_search_tools = 0
    suppressed_tool_search_tools = 0
    bridged_namespace_tools = 0
    bridged_custom_tools = 0
    bridged_tool_search_output_tools = 0
    bridged_web_search_tools = 0
    for tool in tools:
        tool_type = tool.get("type") if isinstance(tool, dict) else None
        converted = _responses_bridge_function_tool(tool)
        if converted is not None:
            _append_unique_chat_tool(sanitized, converted, seen_tool_names)
            continue
        converted = _responses_bridge_custom_tool(tool)
        if converted is not None:
            if _append_unique_chat_tool(sanitized, converted, seen_tool_names):
                bridged_custom_tools += 1
            continue
        converted = _responses_bridge_tool_search_tool(tool)
        if converted is not None:
            if suppress_tool_search_bridge:
                suppressed_tool_search_tools += 1
                if tool_type not in dropped_types:
                    dropped_types.append(str(tool_type))
                continue
            if _append_unique_chat_tool(sanitized, converted, seen_tool_names):
                bridged_tool_search_tools += 1
            continue
        namespace_tools = _responses_bridge_namespace_tools(tool)
        if namespace_tools:
            for namespace_tool in namespace_tools:
                if _append_unique_chat_tool(
                    sanitized,
                    namespace_tool,
                    seen_tool_names,
                ):
                    bridged_namespace_tools += 1
            continue
        converted_web_search_options = _responses_bridge_web_search_options(tool)
        if converted_web_search_options is not None:
            if not bridge_web_search:
                sanitized.append(copy.deepcopy(tool))
                continue
            converted_web_search_tool = _responses_bridge_web_search_tool(tool)
            if converted_web_search_tool is not None and _append_unique_chat_tool(
                sanitized,
                converted_web_search_tool,
                seen_tool_names,
            ):
                bridged_web_search_tools += 1
            continue
        if tool_type == "function":
            invalid_function_tools += 1
        elif isinstance(tool_type, str) and tool_type not in dropped_types:
            dropped_types.append(tool_type)

    for discovered_tool in _responses_input_tool_search_output_tools(input_value):
        converted = _responses_bridge_function_tool(discovered_tool)
        discovered_tools = [converted] if converted is not None else []
        if converted is None:
            discovered_tools = _responses_bridge_namespace_tools(discovered_tool)
        for discovered_chat_tool in discovered_tools:
            if _append_unique_chat_tool(
                sanitized,
                discovered_chat_tool,
                seen_tool_names,
            ):
                bridged_tool_search_output_tools += 1

    changed = (
        sanitized != tools
        or bridged_web_search_tools > 0
        or suppressed_tool_search_tools > 0
        or bridged_tool_search_output_tools > 0
    )
    stats = {
        "changed": changed,
        "original_count": len(tools),
        "kept_count": len(sanitized),
        "dropped_types": dropped_types,
        "invalid_function_tools": invalid_function_tools,
        "bridged_tool_search_tools": bridged_tool_search_tools,
        "suppressed_tool_search_tools": suppressed_tool_search_tools,
        "bridged_namespace_tools": bridged_namespace_tools,
        "bridged_custom_tools": bridged_custom_tools,
        "bridged_tool_search_output_tools": bridged_tool_search_output_tools,
        "bridged_web_search_tools": bridged_web_search_tools,
        "tool_search_output_tool_names": tool_search_output_tool_names[:40],
        "kept_tool_names": _trace_module._trace_tool_names(sanitized),
    }
    return sanitized, web_search_options, stats


def _append_responses_chat_bridge_instruction(
    retry_kwargs: dict,
    stats: dict[str, Any],
) -> None:
    names = stats.get("tool_search_output_tool_names")
    if not isinstance(names, list) or not names:
        return
    visible_names = [name for name in names if isinstance(name, str) and name][:8]
    if not visible_names:
        return

    note = (
        "Responses compatibility note: the previous tool_search result is already "
        "available as callable tools in this request"
    )
    note = f"{note}: {', '.join(visible_names)}. "
    note += (
        "If one of these tools satisfies the user request, call it directly instead "
        "of calling tool_search again for the same tool."
    )
    existing = retry_kwargs.get("instructions")
    if isinstance(existing, str) and existing.strip():
        if note not in existing:
            retry_kwargs["instructions"] = f"{existing.rstrip()}\n\n{note}"
    else:
        retry_kwargs["instructions"] = note


def _request_current_time_context(
    request_kwargs: Optional[dict],
) -> Optional[dict[str, str]]:
    text = _responses_web_search_bridge_module._external_web_search_request_text(request_kwargs)
    if not text:
        return None

    date_match = re.search(
        r"<\s*current_date\s*>\s*(\d{4}-\d{2}-\d{2})\s*<\s*/\s*current_date\s*>",
        text,
        flags=re.IGNORECASE,
    )
    if date_match is None:
        date_match = re.search(
            r"\bcurrent[_ -]?date\b\s*[:=]\s*(\d{4}-\d{2}-\d{2})\b",
            text,
            flags=re.IGNORECASE,
        )
    if date_match is None:
        return None

    context = {"current_date": date_match.group(1)}
    timezone_match = re.search(
        r"<\s*timezone\s*>\s*([^<\n\r]+?)\s*<\s*/\s*timezone\s*>",
        text,
        flags=re.IGNORECASE,
    )
    if timezone_match is not None:
        timezone_text = " ".join(timezone_match.group(1).split())
        if timezone_text:
            context["timezone"] = timezone_text
    return context


def _current_time_context_instruction(request_kwargs: Optional[dict]) -> str:
    context = _request_current_time_context(request_kwargs)
    if not context:
        return ""
    current_date = context["current_date"]
    timezone_text = context.get("timezone")
    if timezone_text:
        date_text = f"{current_date} in timezone {timezone_text}"
    else:
        date_text = current_date
    return (
        "Authoritative request time context: current date is "
        f"{date_text}. For relative or current-time queries such as today, "
        "tomorrow, now, latest/current weather, news, schedules, or similar "
        "facts, ground search queries and final answers in this date context "
        "and do not substitute a different year."
    )

def _append_external_web_search_bridge_instruction(
    retry_kwargs: dict,
    stats: dict[str, Any],
) -> None:
    if not stats.get("bridged_web_search_tools"):
        return
    note = (
        "Native web_search compatibility note: when the user asks to search, "
        "look up, verify current/latest information, or answer real-time facts "
        "such as weather, prices, news, scores, schedules, or regulations, use "
        "the provided web search function tool instead of answering from memory. If a required "
        "prerequisite is missing, such as a location for weather, ask for that "
        "prerequisite instead of guessing. Do not use the web search function tool for "
        "local machine state; use local tools first. If you call local tools, "
        "treat their exact output as authoritative; for macOS, ProductVersion "
        "is the OS version and BuildVersion is not the macOS major version."
    )
    time_note = _current_time_context_instruction(retry_kwargs)
    if time_note:
        note = f"{note} {time_note}"
    existing = retry_kwargs.get("instructions")
    if isinstance(existing, str) and existing.strip():
        if note not in existing:
            retry_kwargs["instructions"] = f"{existing.rstrip()}\n\n{note}"
    else:
        retry_kwargs["instructions"] = note


def _responses_chat_bridge_sanitize_tool_choice(
    tool_choice: Any,
    kept_tool_names: set[str],
) -> Any:
    if not kept_tool_names:
        return None
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in {"auto", "none", "required"} else "auto"
    if not isinstance(tool_choice, dict):
        return "auto"

    tool_choice_type = tool_choice.get("type")
    if tool_choice_type in {"auto", "none", "required"}:
        return tool_choice_type

    if tool_choice_type == "function":
        function = tool_choice.get("function")
        function_dict = function if isinstance(function, dict) else {}
        name = _routing_module._valid_chat_tool_name(function_dict.get("name") or tool_choice.get("name"))
        if name in kept_tool_names:
            return {"type": "function", "function": {"name": name}}
        return "auto"

    if tool_choice_type == "custom":
        name = _routing_module._valid_chat_tool_name(tool_choice.get("name"))
        if name in kept_tool_names:
            return {"type": "function", "function": {"name": name}}
        return "auto"

    return "auto"


def _responses_function_tool_bridge_sanitize_tool_choice(
    tool_choice: Any,
    kept_tool_names: set[str],
) -> Any:
    if not kept_tool_names:
        return None
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in {"auto", "none", "required"} else "auto"
    if not isinstance(tool_choice, dict):
        return "auto"

    tool_choice_type = tool_choice.get("type")
    if tool_choice_type in {"auto", "none", "required"}:
        return tool_choice

    if tool_choice_type == "function":
        function = tool_choice.get("function")
        function_dict = function if isinstance(function, dict) else {}
        name = _routing_module._valid_chat_tool_name(function_dict.get("name") or tool_choice.get("name"))
        if name in kept_tool_names:
            return {"type": "function", "name": name}
        return "auto"

    if tool_choice_type == "custom":
        name = _routing_module._valid_chat_tool_name(tool_choice.get("name"))
        if name in kept_tool_names:
            return {"type": "function", "name": name}
        return "auto"

    if tool_choice_type in {"web_search", "web_search_preview"}:
        if _WEB_SEARCH_BRIDGE_FUNCTION_NAME in kept_tool_names:
            return {"type": "function", "name": _WEB_SEARCH_BRIDGE_FUNCTION_NAME}
        return "auto"

    return "auto"
