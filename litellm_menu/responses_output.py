from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_tools as _responses_tools_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import streaming as _streaming_module


from .base import (
    Any,
    List,
    Optional,
    _RESPONSES_BRIDGE_CUSTOM_TOOL_KEY,
    _RESPONSES_BRIDGE_NAMESPACE_KEY,
    _TOOL_SEARCH_BRIDGE_FUNCTION_NAME,
    copy,
    json,
)


def _responses_tool_search_call_from_function_call(item: Any) -> Any:
    if _responses_web_search_bridge_module._response_item_get(item, "type") != "function_call":
        return item
    if _responses_web_search_bridge_module._response_item_get(item, "name") != _TOOL_SEARCH_BRIDGE_FUNCTION_NAME:
        return item

    call_id = _responses_web_search_bridge_module._response_item_get(item, "call_id") or _responses_web_search_bridge_module._response_item_get(item, "id") or ""
    item_id = _responses_web_search_bridge_module._response_item_get(item, "id") or call_id
    status = _responses_web_search_bridge_module._response_item_get(item, "status") or "completed"
    if status not in {"in_progress", "completed", "incomplete"}:
        status = "completed"

    payload: dict[str, Any] = {
        "id": str(item_id),
        "type": "tool_search_call",
        "call_id": str(call_id) if call_id is not None else None,
        "arguments": _responses_web_search_bridge_module._parse_tool_search_arguments(_responses_web_search_bridge_module._response_item_get(item, "arguments")),
        "execution": "client",
        "status": status,
    }
    created_by = _responses_web_search_bridge_module._response_item_get(item, "created_by")
    if created_by is not None:
        payload["created_by"] = created_by
    return payload


def _response_item_set(item: Any, key: str, value: Any) -> None:
    if isinstance(item, dict):
        item[key] = value
        return
    try:
        setattr(item, key, value)
    except Exception:
        try:
            item.__dict__[key] = value
        except Exception:
            pass


def _responses_namespace_tool_map_from_tools(value: Any) -> dict[str, str]:
    namespace_by_name: dict[str, str] = {}

    def add_tool_name(name: Any, namespace: Any) -> None:
        if not isinstance(name, str) or not isinstance(namespace, str):
            return
        tool_name = name.strip()
        namespace_name = namespace.strip()
        if not tool_name or not namespace_name:
            return
        namespace_by_name.setdefault(tool_name, namespace_name)

    def visit(tool: Any, inherited_namespace: Optional[str] = None) -> None:
        if not isinstance(tool, dict):
            return
        tool_type = tool.get("type")
        explicit_namespace = tool.get(_RESPONSES_BRIDGE_NAMESPACE_KEY)
        namespace = (
            explicit_namespace
            if isinstance(explicit_namespace, str) and explicit_namespace.strip()
            else inherited_namespace
        )
        if tool_type == "namespace":
            raw_namespace = tool.get("name")
            namespace = raw_namespace.strip() if isinstance(raw_namespace, str) else namespace
            child_tools = tool.get("tools")
            if isinstance(child_tools, list):
                for child_tool in child_tools:
                    visit(child_tool, namespace)
            return
        if tool_type == "function":
            function = tool.get("function")
            function_dict = function if isinstance(function, dict) else {}
            add_tool_name(function_dict.get("name") or tool.get("name"), namespace)

    if isinstance(value, list):
        for item in value:
            visit(item)
    return namespace_by_name


def _responses_custom_tool_names_from_tools(value: Any) -> set[str]:
    names: set[str] = set()

    def add_tool_name(name: Any) -> None:
        normalized = _routing_module._valid_chat_tool_name(name)
        if normalized is not None:
            names.add(normalized)

    def visit(tool: Any) -> None:
        if not isinstance(tool, dict):
            return
        tool_type = tool.get("type")
        if tool.get(_RESPONSES_BRIDGE_CUSTOM_TOOL_KEY) is True:
            add_tool_name(tool.get("name"))
        if tool_type == "custom":
            add_tool_name(tool.get("name"))
            return
        if tool_type == "namespace":
            child_tools = tool.get("tools")
            if isinstance(child_tools, list):
                for child_tool in child_tools:
                    visit(child_tool)

    if isinstance(value, list):
        for item in value:
            visit(item)
    return names


def _responses_custom_tool_names(
    request_input: Any = None,
    responses_api_request: Any = None,
) -> set[str]:
    names: set[str] = set()
    request_tools = _responses_web_search_bridge_module._response_item_get(responses_api_request, "tools")
    names.update(_responses_custom_tool_names_from_tools(request_tools))
    names.update(
        _responses_custom_tool_names_from_tools(
            _responses_tools_module._responses_input_tool_search_output_tools(request_input)
        )
    )
    return names


def _responses_namespace_tool_map(
    request_input: Any = None,
    responses_api_request: Any = None,
) -> dict[str, str]:
    namespace_by_name: dict[str, str] = {}
    request_tools = _responses_web_search_bridge_module._response_item_get(responses_api_request, "tools")
    namespace_by_name.update(_responses_namespace_tool_map_from_tools(request_tools))
    namespace_by_name.update(
        _responses_namespace_tool_map_from_tools(
            _responses_tools_module._responses_input_tool_search_output_tools(request_input)
        )
    )
    return namespace_by_name


def _restore_response_function_call_namespace(
    item: Any,
    namespace_by_name: Optional[dict[str, str]],
) -> Any:
    if not namespace_by_name:
        return item
    if _responses_web_search_bridge_module._response_item_get(item, "type") != "function_call":
        return item
    if _responses_web_search_bridge_module._response_item_get(item, "namespace") is not None:
        return item
    name = _responses_web_search_bridge_module._response_item_get(item, "name")
    namespace = namespace_by_name.get(name) if isinstance(name, str) else None
    if namespace:
        _response_item_set(item, "namespace", namespace)
    return item


def _restore_response_custom_tool_call(
    item: Any,
    custom_tool_names: Optional[set[str]],
) -> Any:
    if not custom_tool_names:
        return item
    if _responses_web_search_bridge_module._response_item_get(item, "type") != "function_call":
        return item
    name = _responses_web_search_bridge_module._response_item_get(item, "name")
    if not isinstance(name, str) or name not in custom_tool_names:
        return item

    converted = _streaming_module._jsonable(item)
    if not isinstance(converted, dict):
        return item
    converted = copy.deepcopy(converted)
    converted["type"] = "custom_tool_call"
    arguments = converted.pop("arguments", None)
    if "input" not in converted:
        converted["input"] = _custom_tool_input_from_chat_arguments(arguments)
    if converted.get("status") is None:
        converted["status"] = "completed"
    return converted


def _custom_tool_input_from_chat_arguments(arguments: Any) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        text = arguments
        try:
            parsed = json.loads(text)
        except Exception:
            return text
    else:
        parsed = arguments
    if isinstance(parsed, dict) and isinstance(parsed.get("input"), str):
        return parsed["input"]
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False)


def _custom_tool_input_prefix_from_chat_arguments(arguments: Any) -> str:
    if not isinstance(arguments, str):
        return _custom_tool_input_from_chat_arguments(arguments)
    parsed_input = _custom_tool_input_from_chat_arguments(arguments)
    if parsed_input != arguments:
        return parsed_input
    input_start = _json_string_property_value_start(arguments, "input")
    if input_start is None:
        return ""
    return _json_string_prefix(arguments[input_start:])


def _json_string_property_value_start(text: str, property_name: str) -> Optional[int]:
    decoder = json.JSONDecoder()
    index = 0
    while True:
        key_index = text.find('"', index)
        if key_index < 0:
            return None
        try:
            key, end_index = decoder.raw_decode(text[key_index:])
        except Exception:
            return None
        if isinstance(key, str) and key == property_name:
            colon_index = text.find(":", key_index + end_index)
            if colon_index < 0:
                return None
            value_index = colon_index + 1
            while value_index < len(text) and text[value_index].isspace():
                value_index += 1
            if value_index < len(text) and text[value_index] == '"':
                return value_index
            return None
        index = key_index + max(end_index, 1)


def _json_string_prefix(text: str) -> str:
    if not text.startswith('"'):
        return ""
    output: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == '"':
            break
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            break
        escape = text[index + 1]
        if escape in {'"', "\\", "/"}:
            output.append(escape)
            index += 2
            continue
        escape_map = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        if escape in escape_map:
            output.append(escape_map[escape])
            index += 2
            continue
        if escape == "u":
            digits = text[index + 2 : index + 6]
            if len(digits) < 4 or any(digit not in "0123456789abcdefABCDEF" for digit in digits):
                break
            output.append(chr(int(digits, 16)))
            index += 6
            continue
        break
    return "".join(output)


class _CustomToolInputDeltaTracker:
    def __init__(self) -> None:
        self.arguments_by_item_id: dict[str, str] = {}
        self.input_by_item_id: dict[str, str] = {}

    def append_arguments_delta(self, item_id: str, delta: str) -> str:
        arguments = self.arguments_by_item_id.get(item_id, "") + delta
        self.arguments_by_item_id[item_id] = arguments
        return self._new_input_delta(item_id, arguments)

    def complete_arguments(self, item_id: str, arguments: str) -> str:
        self.arguments_by_item_id[item_id] = arguments
        return self._new_input_delta(item_id, arguments)

    def _new_input_delta(self, item_id: str, arguments: str) -> str:
        input_text = _custom_tool_input_prefix_from_chat_arguments(arguments)
        previous = self.input_by_item_id.get(item_id, "")
        if not input_text.startswith(previous):
            previous = ""
        delta = input_text[len(previous) :]
        self.input_by_item_id[item_id] = input_text
        return delta


def _stream_function_arguments_event_item_id(event: Any) -> Optional[str]:
    for key in ("item_id", "call_id", "id"):
        value = _responses_web_search_bridge_module._response_item_get(event, key)
        if isinstance(value, str) and value:
            return value
    return None


def _response_event_set(event: Any, key: str, value: Any) -> None:
    if isinstance(event, dict):
        event[key] = value
        return
    try:
        setattr(event, key, value)
    except Exception:
        try:
            event.__dict__[key] = value
        except Exception:
            pass


def _normalize_custom_tool_input_event(
    event: Any,
    custom_tool_item_ids: set[str],
    input_delta_tracker: _CustomToolInputDeltaTracker,
) -> Optional[Any]:
    event_type = _responses_web_search_bridge_module._response_item_get(event, "type")
    if event_type not in {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }:
        return event
    item_id = _stream_function_arguments_event_item_id(event)
    if item_id not in custom_tool_item_ids:
        return event

    converted = copy.deepcopy(event)
    if event_type.endswith(".delta"):
        delta = _responses_web_search_bridge_module._response_item_get(event, "delta")
        if not isinstance(delta, str) or not item_id:
            return None
        input_delta = input_delta_tracker.append_arguments_delta(item_id, delta)
        if not input_delta:
            return None
        _response_event_set(converted, "type", "response.custom_tool_call_input.delta")
        _response_event_set(converted, "delta", input_delta)
        return converted

    arguments = _responses_web_search_bridge_module._response_item_get(event, "arguments")
    if not isinstance(arguments, str) or not item_id:
        return None
    input_text = _custom_tool_input_from_chat_arguments(arguments)
    input_delta_tracker.complete_arguments(item_id, arguments)
    _response_event_set(converted, "type", "response.custom_tool_call_input.done")
    _response_event_set(converted, "input", input_text)
    if isinstance(converted, dict):
        converted.pop("arguments", None)
    else:
        try:
            delattr(converted, "arguments")
        except Exception:
            pass
    return converted


def _normalize_response_tool_search_output(
    response: Any,
    namespace_by_name: Optional[dict[str, str]] = None,
    custom_tool_names: Optional[set[str]] = None,
) -> Any:
    output = _responses_web_search_bridge_module._response_item_get(response, "output")
    if not isinstance(output, list):
        return response
    for index, item in enumerate(output):
        converted = _responses_tool_search_call_from_function_call(item)
        converted = _restore_response_custom_tool_call(converted, custom_tool_names)
        output[index] = _restore_response_function_call_namespace(
            converted,
            namespace_by_name,
        )
    return response


def _responses_output_has_non_message_item(output: Any) -> bool:
    if not isinstance(output, list):
        return False
    return any(_responses_web_search_bridge_module._response_item_get(item, "type") != "message" for item in output)


def _strip_empty_message_items_when_structured_output_present(response: Any) -> Any:
    output = _responses_web_search_bridge_module._response_item_get(response, "output")
    if not isinstance(output, list) or not _responses_output_has_non_message_item(output):
        return response

    filtered_output: list[Any] = []
    changed = False
    for item in output:
        if (
            _responses_web_search_bridge_module._response_item_get(item, "type") == "message"
            and not _image_generation_module._payload_has_visible_text(item)
        ):
            changed = True
            continue
        filtered_output.append(item)

    if changed:
        _response_item_set(response, "output", filtered_output)
    return response


def _streaming_completion_message(response: Any) -> Any:
    try:
        choices = getattr(response, "choices", None)
        if isinstance(choices, list) and choices:
            return getattr(choices[0], "message", None)
    except Exception:
        return None
    return None


def _streaming_completion_should_skip_empty_message_events(response: Any) -> bool:
    message = _streaming_completion_message(response)
    if message is None or _image_generation_module._payload_has_visible_text(message):
        return False

    message_payload = _streaming_module._jsonable(message)
    return bool(
        _responses_web_search_bridge_module._response_item_get(message_payload, "tool_calls")
        or _responses_web_search_bridge_module._response_item_get(message_payload, "function_call")
        or _responses_web_search_bridge_module._response_item_get(message_payload, "reasoning_content")
    )


def _normalize_tool_search_output_item_event(
    event: Any,
    namespace_by_name: Optional[dict[str, str]] = None,
    custom_tool_names: Optional[set[str]] = None,
) -> Any:
    item = _responses_web_search_bridge_module._response_item_get(event, "item")
    converted = _responses_tool_search_call_from_function_call(item)
    converted = _restore_response_custom_tool_call(converted, custom_tool_names)
    converted = _restore_response_function_call_namespace(
        converted,
        namespace_by_name,
    )
    if converted is item:
        return event
    event_item = converted
    if isinstance(converted, dict) and item is not None and not isinstance(item, dict):
        try:
            event_item = item.__class__(**converted)
        except Exception:
            event_item = converted
    if isinstance(event, dict):
        event["item"] = event_item
        return event
    try:
        setattr(event, "item", event_item)
    except Exception:
        try:
            event.__dict__["item"] = event_item
        except Exception:
            pass
    return event


def _normalize_response_stream_tool_bridge_chunk(
    chunk: Any,
    namespace_by_name: Optional[dict[str, str]] = None,
    custom_tool_names: Optional[set[str]] = None,
    custom_tool_item_ids: Optional[set[str]] = None,
    input_delta_tracker: Optional[_CustomToolInputDeltaTracker] = None,
) -> Any:
    chunk_type = _streaming_module._stream_chunk_type(chunk)
    if chunk_type in {"response.output_item.added", "response.output_item.done"}:
        normalized = _normalize_tool_search_output_item_event(
            chunk,
            namespace_by_name,
            custom_tool_names,
        )
        item = _responses_web_search_bridge_module._response_item_get(normalized, "item")
        if (
            custom_tool_item_ids is not None
            and _responses_web_search_bridge_module._response_item_get(item, "type") == "custom_tool_call"
        ):
            item_id = _streaming_module._stream_output_item_key(item)
            if isinstance(item_id, str) and item_id:
                custom_tool_item_ids.add(item_id)
        return normalized

    if chunk_type in {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }:
        if custom_tool_item_ids is None or input_delta_tracker is None:
            return chunk
        return _normalize_custom_tool_input_event(
            chunk,
            custom_tool_item_ids,
            input_delta_tracker,
        )

    response = _responses_web_search_bridge_module._response_item_get(chunk, "response")
    if response is None:
        return chunk
    _normalize_response_tool_search_output(
        response,
        namespace_by_name,
        custom_tool_names,
    )
    return chunk


def _normalize_pending_tool_search_events(
    iterator: Any,
    namespace_by_name: Optional[dict[str, str]] = None,
    custom_tool_names: Optional[set[str]] = None,
) -> None:
    pending = getattr(iterator, "_pending_tool_events", None)
    if not isinstance(pending, list):
        return
    if namespace_by_name is None:
        namespace_by_name = _responses_namespace_tool_map(
            getattr(iterator, "request_input", None),
            getattr(iterator, "responses_api_request", None),
        )
    if custom_tool_names is None:
        custom_tool_names = _responses_custom_tool_names(
            getattr(iterator, "request_input", None),
            getattr(iterator, "responses_api_request", None),
        )
    custom_tool_item_ids: set[str] = set()
    input_delta_tracker = _CustomToolInputDeltaTracker()
    normalized_pending: List[Any] = []
    for event in pending:
        normalized = _normalize_response_stream_tool_bridge_chunk(
            event,
            namespace_by_name,
            custom_tool_names,
            custom_tool_item_ids,
            input_delta_tracker,
        )
        if normalized is not None:
            normalized_pending.append(normalized)
    pending[:] = normalized_pending
    _append_missing_pending_tool_search_done_events(pending)


def _append_missing_pending_tool_search_done_events(pending: list[Any]) -> None:
    added: dict[str, tuple[int, dict[str, Any]]] = {}
    done_ids: set[str] = set()
    for event in pending:
        event_type = _responses_web_search_bridge_module._response_item_get(event, "type")
        item = _responses_web_search_bridge_module._response_item_get(event, "item")
        item_type = _responses_web_search_bridge_module._response_item_get(item, "type")
        item_id = _streaming_module._stream_output_item_key(item)
        if not item_id or item_type != "tool_search_call":
            continue
        if event_type == "response.output_item.done":
            done_ids.update(_streaming_module._stream_output_item_identity_keys(item))
            continue
        if event_type != "response.output_item.added":
            continue
        output_index = _responses_web_search_bridge_module._response_item_get(event, "output_index", len(added) + 1)
        if not isinstance(output_index, int):
            output_index = len(added) + 1
        json_item = _streaming_module._jsonable(item)
        if isinstance(json_item, dict):
            added[item_id] = (output_index, json_item)

    for item_id, (output_index, item) in added.items():
        if any(identity_key in done_ids for identity_key in _streaming_module._stream_output_item_identity_keys(item)):
            continue
        done_item = copy.deepcopy(item)
        done_item["status"] = "completed"
        pending.append(
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": done_item,
            }
        )
