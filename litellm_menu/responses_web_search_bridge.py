from __future__ import annotations

from . import external_web_search as _external_web_search_module
from . import image_generation as _image_generation_module
from . import responses_execution as _responses_execution_module
from . import responses_tools as _responses_tools_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module
from .external_web_search import _external_web_search_float_env


from .base import (
    Any,
    Optional,
    _EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT,
    _EXTERNAL_WEB_FETCH_TIMEOUT_ENV,
    _EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_DEFAULT,
    _EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_ENV,
    _EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_DEFAULT,
    _EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_ENV,
    _EXTERNAL_WEB_SEARCH_MAX_QUERIES_DEFAULT,
    _EXTERNAL_WEB_SEARCH_MAX_QUERIES_ENV,
    _EXTERNAL_WEB_SEARCH_MAX_ROUNDS_DEFAULT,
    _EXTERNAL_WEB_SEARCH_MAX_ROUNDS_ENV,
    _EXTERNAL_WEB_SEARCH_READ_CHARS_DEFAULT,
    _EXTERNAL_WEB_SEARCH_READ_CHARS_ENV,
    _JSONStreamEvent,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RESPONSES_ENDPOINT_SUPPORT_KEY,
    _WEB_SEARCH_BRIDGE_FUNCTION_NAME,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
    _WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY,
    _SUPPORTED_UPSTREAM_URL_SURFACES_KEY,
    _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES,
    _UPSTREAM_URL_SURFACE_KEY,
    asyncio,
    copy,
    inspect,
    json,
    os,
    re,
    time,
)


def _response_item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _parse_tool_search_arguments(arguments: Any) -> Any:
    if arguments is None:
        return {}
    if not isinstance(arguments, str):
        return arguments
    text = arguments.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return arguments


_LITELLM_WEB_SEARCH_CALL_ITEM_TYPES = {"function_call", "custom_tool_call", "tool_call"}


def _is_litellm_web_search_call_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return False
    if (
        item_type in _LITELLM_WEB_SEARCH_CALL_ITEM_TYPES
        and item.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME
    ):
        return True
    function = item.get("function")
    return (
        item_type in _LITELLM_WEB_SEARCH_CALL_ITEM_TYPES
        and isinstance(function, dict)
        and function.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME
    )


def _litellm_web_search_function_calls(response: Any) -> list[dict[str, Any]]:
    payload = _streaming_module._jsonable(response)
    calls: list[dict[str, Any]] = []

    def append_call(item: Any) -> None:
        if isinstance(item, dict):
            calls.append(item)

    def visit(item: Any, depth: int = 0) -> None:
        if item is None or depth > 8:
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if not isinstance(item, dict):
            return
        if _is_litellm_web_search_call_item(item):
            append_call(item)
            return
        function = item.get("function")
        if (
            item.get("type") == "function"
            and isinstance(function, dict)
            and function.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME
        ):
            append_call(item)
            return
        for value in item.values():
            if isinstance(value, (dict, list)):
                visit(value, depth + 1)

    visit(payload)
    return calls


def _litellm_web_search_arguments_from_call(call: dict[str, Any]) -> Any:
    arguments = call.get("arguments")
    function = call.get("function")
    if arguments is None and isinstance(function, dict):
        arguments = function.get("arguments")
    if arguments is None:
        arguments = call.get("input")
    return _parse_tool_search_arguments(arguments)


def _litellm_web_search_query_from_call(call: dict[str, Any]) -> Optional[str]:
    parsed = _litellm_web_search_arguments_from_call(call)
    if isinstance(parsed, dict):
        query = parsed.get("query")
        if isinstance(query, str) and query.strip():
            return query.strip()
    return None


def _external_web_search_clean_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip().rstrip(").,;]")
    if not url.startswith(("http://", "https://")):
        return None
    return url


def _external_web_search_action_from_text(text: str) -> Optional[dict[str, str]]:
    stripped = text.strip()
    if _external_web_search_looks_like_tool_json_fragment(stripped):
        return None
    open_match = re.match(r"(?is)^open\s*page\s*:\s*(\S+)\s*$", stripped)
    if open_match:
        url = _external_web_search_clean_url(open_match.group(1))
        if url:
            return {"type": "openPage", "url": url}
    find_match = re.match(
        r"(?is)^find\s*in\s*page\s*:\s*(.*?)\s+(?:in|on)\s+(\S+)\s*$",
        stripped,
    )
    if find_match:
        pattern = find_match.group(1).strip()
        url = _external_web_search_clean_url(find_match.group(2))
        if pattern and url:
            return {"type": "findInPage", "url": url, "pattern": pattern}
    return None


def _external_web_search_looks_like_tool_json_fragment(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped.startswith(("{", "[")):
        return False
    lowered = stripped.lower()
    if not any(
        marker in lowered
        for marker in (
            '"url"',
            "'url'",
            '"query"',
            "'query'",
            '"pattern"',
            "'pattern'",
            '"action"',
            "'action'",
            "openpage",
            "findinpage",
        )
    ):
        return False
    try:
        json.loads(stripped)
        return False
    except Exception:
        return True


def _external_web_search_action_name(value: Any) -> str:
    if not isinstance(value, str):
        return "search"
    normalized = re.sub(r"[\s_-]+", "", value.strip().lower())
    if normalized in {"open", "openpage", "page", "openurl", "read", "readpage"}:
        return "openPage"
    if normalized in {"find", "findinpage", "findonpage", "searchinpage"}:
        return "findInPage"
    return "search"


def _litellm_web_search_action_from_call(call: dict[str, Any]) -> Optional[dict[str, str]]:
    parsed = _litellm_web_search_arguments_from_call(call)
    if isinstance(parsed, str) and parsed.strip():
        if _external_web_search_looks_like_tool_json_fragment(parsed):
            return None
        action = _external_web_search_action_from_text(parsed)
        return action or {"type": "search", "query": parsed.strip()}
    if not isinstance(parsed, dict):
        return None

    action_type = _external_web_search_action_name(
        parsed.get("action") or parsed.get("type") or parsed.get("operation")
    )
    query = parsed.get("query") or parsed.get("q")
    if isinstance(query, str) and query.strip():
        action = _external_web_search_action_from_text(query)
        if action is not None:
            return action
    url = _external_web_search_clean_url(
        parsed.get("url") or parsed.get("href") or parsed.get("page_url")
    )
    pattern = parsed.get("pattern") or parsed.get("text") or parsed.get("needle")

    if action_type == "search" and url and pattern:
        action_type = "findInPage"
    elif action_type == "search" and url and not query:
        action_type = "openPage"

    if action_type == "openPage":
        if not url:
            return None
        return {"type": "openPage", "url": url}

    if action_type == "findInPage":
        if not url:
            return None
        if not isinstance(pattern, str) or not pattern.strip():
            if isinstance(query, str) and query.strip():
                pattern = query
        if not isinstance(pattern, str) or not pattern.strip():
            return None
        return {"type": "findInPage", "url": url, "pattern": pattern.strip()}

    if isinstance(query, str) and query.strip():
        if _external_web_search_looks_like_tool_json_fragment(query):
            return None
        return {"type": "search", "query": query.strip()}
    return None


def _external_web_search_valid_action(action: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
    if not isinstance(action, dict):
        return None
    action_type = action.get("type")
    if action_type == "search":
        query = action.get("query")
        if not isinstance(query, str) or not query.strip():
            return None
        if _external_web_search_looks_like_tool_json_fragment(query):
            return None
        clean = copy.deepcopy(action)
        clean["query"] = query.strip()
        return clean
    if action_type == "openPage":
        url = _external_web_search_clean_url(action.get("url"))
        if not url:
            return None
        clean = copy.deepcopy(action)
        clean["url"] = url
        return clean
    if action_type == "findInPage":
        url = _external_web_search_clean_url(action.get("url"))
        pattern = action.get("pattern")
        if not url or not isinstance(pattern, str) or not pattern.strip():
            return None
        clean = copy.deepcopy(action)
        clean["url"] = url
        clean["pattern"] = pattern.strip()
        return clean
    return None


def _external_web_search_action_key(action: dict[str, str]) -> str:
    action_type = action.get("type")
    if action_type == "openPage":
        return f"openPage:{action.get('url', '').strip().lower()}"
    if action_type == "findInPage":
        return (
            f"findInPage:{action.get('url', '').strip().lower()}:"
            f"{action.get('pattern', '').strip().lower()}"
        )
    return f"search:{action.get('query', '').strip().lower()}"


def _external_web_search_action_label(action: dict[str, str]) -> str:
    action_type = action.get("type")
    if action_type == "openPage":
        return action.get("url", "")
    if action_type == "findInPage":
        pattern = action.get("pattern", "")
        url = action.get("url", "")
        if pattern and url:
            return f"{pattern} in {url}"
        return url or pattern
    return action.get("query", "")


def _external_web_search_action_labels(actions: list[dict[str, str]]) -> list[str]:
    return [
        label
        for label in (_external_web_search_action_label(action) for action in actions)
        if label
    ]


def _external_web_search_trace_actions(actions: list[dict[str, str]]) -> list[dict[str, str]]:
    traced: list[dict[str, str]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type") or "search"
        clean: dict[str, str] = {"type": str(action_type)}
        for key in ("query", "url", "pattern"):
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                clean[key] = value.strip()
        traced.append(clean)
    return traced


def _external_web_search_call_action_kind(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[\s_-]+", "", value.strip().lower())
    if normalized in {"search", "query", "websearch"}:
        return "search"
    if normalized in {"open", "openpage", "page", "openurl", "read", "readpage"}:
        return "openPage"
    if normalized in {"find", "findinpage", "findonpage", "searchinpage"}:
        return "findInPage"
    return None


def _external_web_search_nonempty_strings(value: Any) -> list[str]:
    strings: list[str] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip()
            if text not in strings:
                strings.append(text)

    if isinstance(value, str):
        add(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            add(item)
    return strings


def _external_web_search_source_urls_from_action(action: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    def add_url(value: Any) -> None:
        url = _external_web_search_clean_url(value)
        if url and url not in urls:
            urls.append(url)

    sources = action.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                add_url(source.get("url"))
            else:
                add_url(source)
    return urls


def _external_web_search_apply_source_urls(
    action: dict[str, Any],
    source_urls: Optional[list[str]] = None,
) -> None:
    clean_urls: list[str] = []
    for url in _external_web_search_source_urls_from_action(action) + list(source_urls or []):
        clean_url = _external_web_search_clean_url(url)
        if clean_url and clean_url not in clean_urls:
            clean_urls.append(clean_url)
    if clean_urls:
        action["sources"] = [{"type": "url", "url": url} for url in clean_urls[:10]]


def _sanitize_web_search_call_item(
    item: dict[str, Any],
    source_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    if item.get("type") != "web_search_call":
        return copy.deepcopy(item)

    raw_action = item.get("action")
    action = copy.deepcopy(raw_action) if isinstance(raw_action, dict) else {}
    bridge_action = action.get("bridge_action")
    lookup_action = bridge_action if isinstance(bridge_action, dict) else action
    top_level_queries = _external_web_search_nonempty_strings(item.get("query"))
    query_candidates = (
        _external_web_search_nonempty_strings(action.get("query"))
        + _external_web_search_nonempty_strings(action.get("queries"))
        + _external_web_search_nonempty_strings(lookup_action.get("query"))
        + _external_web_search_nonempty_strings(lookup_action.get("queries"))
        + top_level_queries
    )
    unique_queries: list[str] = []
    for query in query_candidates:
        if query not in unique_queries:
            unique_queries.append(query)

    action_kind = _external_web_search_call_action_kind(
        lookup_action.get("type") or action.get("type")
    )
    url = _external_web_search_clean_url(
        lookup_action.get("url")
        or lookup_action.get("href")
        or lookup_action.get("page_url")
        or action.get("url")
        or action.get("href")
        or action.get("page_url")
        or (unique_queries[0] if unique_queries else None)
    )
    pattern = (
        lookup_action.get("pattern")
        or lookup_action.get("text")
        or lookup_action.get("needle")
        or action.get("pattern")
        or action.get("text")
        or action.get("needle")
    )
    if not isinstance(pattern, str) or not pattern.strip():
        pattern = None

    if action_kind is None:
        if url and pattern:
            action_kind = "findInPage"
        elif url and not unique_queries:
            action_kind = "openPage"
        elif unique_queries:
            action_kind = "search"

    original_source_urls = _external_web_search_source_urls_from_action(action)
    clean_action: dict[str, Any] = {}
    label = ""
    if action_kind == "search":
        if not unique_queries:
            return None
        label = unique_queries[0]
        if _external_web_search_looks_like_tool_json_fragment(label):
            return None
        clean_action["type"] = "search"
        clean_action["query"] = label
        if len(unique_queries) > 1:
            clean_action["queries"] = unique_queries
    elif action_kind == "openPage":
        if not url:
            if not unique_queries:
                return None
            label = unique_queries[0]
            clean_action["type"] = "search"
            clean_action["query"] = label
        else:
            label_action = {"type": "openPage", "url": url}
            label = _external_web_search_action_label(label_action)
            clean_action["type"] = "search"
            clean_action["query"] = label
    elif action_kind == "findInPage":
        if not url:
            if not unique_queries:
                return None
            label = unique_queries[0]
            clean_action["type"] = "search"
            clean_action["query"] = label
        else:
            if pattern is None and unique_queries:
                pattern = unique_queries[0]
            if pattern is None:
                return None
            label_action = {"type": "findInPage", "url": url, "pattern": pattern.strip()}
            label = _external_web_search_action_label(label_action)
            clean_action["type"] = "search"
            clean_action["query"] = label
    else:
        return None

    if not label.strip() or not str(clean_action.get("query") or "").strip():
        return None

    merged_source_urls = original_source_urls + list(source_urls or [])
    _external_web_search_apply_source_urls(clean_action, merged_source_urls)
    clean_item = copy.deepcopy(item)
    clean_item["type"] = "web_search_call"
    clean_item["status"] = str(clean_item.get("status") or "completed")
    clean_item["query"] = label
    clean_item["action"] = clean_action
    if not isinstance(clean_item.get("id"), str) or not clean_item.get("id"):
        clean_item["id"] = f"ws_sanitized_{os.getpid()}_{time.time_ns()}"
    return clean_item


_PROVIDER_HOSTED_WEB_SEARCH_ITEM_TYPES = {
    "openrouter:web_search",
    "openrouter.web_search",
    "openrouter_web_search",
}

_RAW_TOOL_CALL_START = "<tool_call"
_RAW_TOOL_CALL_END = "</tool_call>"
_RAW_TOOL_CALL_BLOCK_RE = re.compile(r"(?is)<tool_call\b[^>]*>.*?</tool_call>")
_RAW_TOOL_CALL_TAIL_RE = re.compile(r"(?is)<tool_call\b[^>]*>.*$")


def _raw_tool_call_pending_prefix_len(text: str, marker: str) -> int:
    lower_text = text.lower()
    lower_marker = marker.lower()
    max_len = min(len(lower_text), len(lower_marker) - 1)
    for length in range(max_len, 0, -1):
        if lower_marker.startswith(lower_text[-length:]):
            return length
    return 0


def _strip_raw_tool_call_blocks(text: str) -> str:
    if not isinstance(text, str) or _RAW_TOOL_CALL_START not in text.lower():
        return text
    cleaned = _RAW_TOOL_CALL_BLOCK_RE.sub("", text)
    cleaned = _RAW_TOOL_CALL_TAIL_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


class _RawToolCallTextFilter:
    def __init__(self) -> None:
        self.buffer = ""
        self.dropping = False

    def consume(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""
        self.buffer += text
        output: list[str] = []

        while self.buffer:
            lowered = self.buffer.lower()
            if self.dropping:
                end_index = lowered.find(_RAW_TOOL_CALL_END)
                if end_index < 0:
                    keep = _raw_tool_call_pending_prefix_len(
                        self.buffer,
                        _RAW_TOOL_CALL_END,
                    )
                    self.buffer = self.buffer[-keep:] if keep else ""
                    break
                self.buffer = self.buffer[end_index + len(_RAW_TOOL_CALL_END) :]
                self.dropping = False
                continue

            start_index = lowered.find(_RAW_TOOL_CALL_START)
            if start_index >= 0:
                if start_index:
                    output.append(self.buffer[:start_index])
                self.buffer = self.buffer[start_index:]
                self.dropping = True
                continue

            keep = _raw_tool_call_pending_prefix_len(self.buffer, _RAW_TOOL_CALL_START)
            if keep:
                if len(self.buffer) > keep:
                    output.append(self.buffer[:-keep])
                self.buffer = self.buffer[-keep:]
                break

            output.append(self.buffer)
            self.buffer = ""
            break

        return "".join(output)

    def reset(self) -> None:
        self.buffer = ""
        self.dropping = False


def _provider_hosted_web_search_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in _PROVIDER_HOSTED_WEB_SEARCH_ITEM_TYPES:
        return normalized
    return None


def _is_provider_hosted_web_search_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return _provider_hosted_web_search_type(item.get("type")) is not None


def _provider_hosted_web_search_query_strings(item: Any) -> list[str]:
    queries: list[str] = []
    query_keys = {"query", "queries", "q", "search_query", "search_queries"}

    def add(value: Any) -> None:
        for query in _external_web_search_nonempty_strings(value):
            if _external_web_search_clean_url(query):
                continue
            if _external_web_search_looks_like_tool_json_fragment(query):
                continue
            if query not in queries:
                queries.append(query)

    def decoded(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        parsed = _parse_tool_search_arguments(value)
        return parsed if parsed is not value else value

    def visit(value: Any, depth: int = 0) -> None:
        if value is None or depth > 6 or len(queries) >= 10:
            return
        value = decoded(value)
        if isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
                if len(queries) >= 10:
                    return
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in query_keys:
                add(child)
        for child in value.values():
            if isinstance(child, (dict, list, str)):
                visit(child, depth + 1)
                if len(queries) >= 10:
                    return

    visit(item)
    return queries


def _provider_hosted_web_search_source_urls(value: Any) -> list[str]:
    urls: list[str] = []

    def add_url(candidate: Any) -> None:
        url = _external_web_search_clean_url(candidate)
        if url and url not in urls:
            urls.append(url)

    def visit(item: Any, depth: int = 0) -> None:
        if item is None or depth > 8 or len(urls) >= 20:
            return
        if isinstance(item, str):
            for match in re.finditer(r"https?://[^\s<>\"]+", item):
                add_url(match.group(0))
                if len(urls) >= 20:
                    return
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
                if len(urls) >= 20:
                    return
            return
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "url_citation":
            add_url(item.get("url"))
        for key, child in item.items():
            if str(key).strip().lower() in {"url", "href", "uri"}:
                add_url(child)
            if isinstance(child, (dict, list, str)):
                visit(child, depth + 1)
                if len(urls) >= 20:
                    return

    visit(_streaming_module._jsonable(value))
    return urls


def _provider_hosted_web_search_call_item(
    item: dict[str, Any],
    source_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    item_source_urls = _provider_hosted_web_search_source_urls(item)
    merged_source_urls: list[str] = []
    for url in item_source_urls + list(source_urls or []):
        clean_url = _external_web_search_clean_url(url)
        if clean_url and clean_url not in merged_source_urls:
            merged_source_urls.append(clean_url)

    queries = _provider_hosted_web_search_query_strings(item)
    action: dict[str, Any]
    if queries:
        action = {"type": "search", "query": queries[0]}
        if len(queries) > 1:
            action["queries"] = queries
    elif merged_source_urls:
        action = {"type": "openPage", "url": merged_source_urls[0]}
    else:
        return None

    item_id = item.get("id") or item.get("call_id") or f"ws_provider_{os.getpid()}_{time.time_ns()}"
    status = item.get("status") or "completed"
    return _sanitize_web_search_call_item(
        {
            "id": str(item_id),
            "type": "web_search_call",
            "status": str(status),
            "action": action,
        },
        merged_source_urls,
    )


def _sanitize_response_web_search_call_items(response: Any) -> Any:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return response
    output = payload.get("output")
    if not isinstance(output, list):
        return response
    response_source_urls = _provider_hosted_web_search_source_urls(payload)
    clean_output: list[Any] = []
    changed = False
    for item in output:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            clean_item = _sanitize_web_search_call_item(item)
            if clean_item is None:
                changed = True
                continue
            clean_output.append(clean_item)
            if clean_item != item:
                changed = True
            continue
        if _is_provider_hosted_web_search_item(item):
            clean_item = _provider_hosted_web_search_call_item(item, response_source_urls)
            if clean_item is not None:
                clean_output.append(clean_item)
                changed = True
                continue
        clean_output.append(item)
    if changed:
        payload["output"] = clean_output
        return payload
    return response


def _sanitize_output_text_part_raw_tool_calls(part: Any) -> tuple[Any, bool]:
    if not isinstance(part, dict):
        return part, False
    text = part.get("text")
    if not isinstance(text, str):
        return part, False
    cleaned = _strip_raw_tool_call_blocks(text)
    if cleaned == text:
        return part, False
    clean_part = copy.deepcopy(part)
    clean_part["text"] = cleaned
    return clean_part, True


def _sanitize_message_raw_tool_calls(item: Any) -> tuple[Any, bool]:
    if not isinstance(item, dict) or item.get("type") != "message":
        return item, False
    content = item.get("content")
    if not isinstance(content, list):
        return item, False
    clean_content: list[Any] = []
    changed = False
    for part in content:
        if isinstance(part, dict) and part.get("type") == "output_text":
            clean_part, part_changed = _sanitize_output_text_part_raw_tool_calls(part)
            clean_content.append(clean_part)
            changed = changed or part_changed
        else:
            clean_content.append(part)
    if not changed:
        return item, False
    clean_item = copy.deepcopy(item)
    clean_item["content"] = clean_content
    return clean_item, True


def _sanitize_response_raw_tool_call_text(response: Any) -> Any:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return response
    changed = False

    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        cleaned_output_text = _strip_raw_tool_call_blocks(output_text)
        if cleaned_output_text != output_text:
            payload = copy.deepcopy(payload)
            changed = True
            if cleaned_output_text.strip():
                payload["output_text"] = cleaned_output_text
            else:
                payload.pop("output_text", None)

    output = payload.get("output")
    if isinstance(output, list):
        clean_output: list[Any] = []
        for item in output:
            clean_item, item_changed = _sanitize_message_raw_tool_calls(item)
            clean_output.append(clean_item)
            changed = changed or item_changed
        if changed:
            payload = copy.deepcopy(payload)
            payload["output"] = clean_output

    return payload if changed else response


def _is_reasoning_output_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    if item_type == "summary_text":
        return True
    if isinstance(item_type, str) and item_type.startswith("reasoning"):
        return True
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "summary_text" or (
                isinstance(part_type, str) and part_type.startswith("reasoning")
            ):
                return True
    return False


def _reasoning_text_fragments(value: Any) -> set[str]:
    fragments: set[str] = set()

    def add_text(text: Any) -> None:
        if isinstance(text, str) and text.strip():
            fragments.add(text.strip())

    def walk(item: Any, *, in_reasoning: bool = False) -> None:
        if isinstance(item, list):
            for child in item:
                walk(child, in_reasoning=in_reasoning)
            return
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        item_is_reasoning = in_reasoning or item_type == "summary_text" or (
            isinstance(item_type, str) and item_type.startswith("reasoning")
        )
        if item_is_reasoning:
            for key in ("text", "summary_text", "content", "delta"):
                add_text(item.get(key))
        for key in ("summary", "content", "items", "output"):
            child = item.get(key)
            if isinstance(child, (dict, list)):
                walk(child, in_reasoning=item_is_reasoning)

    walk(value)
    return fragments


def _message_visible_text(item: Any) -> str:
    chunks: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        value_type = value.get("type")
        if value_type == "output_text":
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
            return
        if value_type in {"input_text", "summary_text"} or (
            isinstance(value_type, str) and value_type.startswith("reasoning")
        ):
            return
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
        content = value.get("content")
        if isinstance(content, (dict, list)):
            walk(content)

    if isinstance(item, dict):
        walk(item.get("content"))
    return "\n".join(chunks).strip()


def _has_structured_output_item(output: Any) -> bool:
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message" or _is_reasoning_output_item(item):
            continue
        return True
    return False


def _sanitize_response_reasoning_items(response: Any) -> Any:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return response
    output = payload.get("output")
    if not isinstance(output, list):
        return response
    reasoning_fragments: set[str] = set()
    for item in output:
        if _is_reasoning_output_item(item):
            reasoning_fragments.update(_reasoning_text_fragments(item))
    has_structured_output = _has_structured_output_item(output)
    clean_output: list[Any] = []
    changed = False
    for item in output:
        if _is_reasoning_output_item(item):
            changed = True
            continue
        if (
            has_structured_output
            and reasoning_fragments
            and isinstance(item, dict)
            and item.get("type") == "message"
            and _message_visible_text(item) in reasoning_fragments
        ):
            changed = True
            continue
        if (
            has_structured_output
            and isinstance(item, dict)
            and item.get("type") == "message"
            and not _message_visible_text(item)
        ):
            changed = True
            continue
        clean_output.append(item)
    if changed:
        payload["output"] = clean_output
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and (
            not output_text.strip() or output_text.strip() in reasoning_fragments
        ):
            payload.pop("output_text", None)
        return payload
    return response


def _sanitize_response_stream_payload(response: Any) -> Any:
    return _sanitize_response_reasoning_items(
        _sanitize_response_raw_tool_call_text(
            _sanitize_response_web_search_call_items(response)
        )
    )


def _sanitize_raw_tool_call_text_stream_chunk(
    chunk: Any,
    text_filter: Optional[_RawToolCallTextFilter] = None,
) -> Optional[Any]:
    dumped = _streaming_module._stream_chunk_dump(chunk)
    if not isinstance(dumped, dict) or not dumped:
        return chunk

    chunk_type = _streaming_module._stream_chunk_type(dumped)
    if chunk_type == "response.output_text.delta":
        delta = dumped.get("delta")
        if not isinstance(delta, str):
            return chunk
        cleaned_delta = (
            text_filter.consume(delta)
            if text_filter is not None
            else _strip_raw_tool_call_blocks(delta)
        )
        if cleaned_delta == delta:
            return chunk
        if not cleaned_delta:
            return None
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["delta"] = cleaned_delta
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    if chunk_type == "response.output_text.done":
        if text_filter is not None:
            text_filter.reset()
        text = dumped.get("text")
        if not isinstance(text, str):
            return chunk
        cleaned_text = _strip_raw_tool_call_blocks(text)
        if cleaned_text == text:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["text"] = cleaned_text
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    if chunk_type in {"response.content_part.added", "response.content_part.done"}:
        part = dumped.get("part")
        clean_part, changed = _sanitize_output_text_part_raw_tool_calls(part)
        if not changed:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["part"] = clean_part
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    if chunk_type in {"response.output_item.added", "response.output_item.done"}:
        item = dumped.get("item")
        clean_item, changed = _sanitize_message_raw_tool_calls(item)
        if not changed:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["item"] = clean_item
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    if chunk_type == "response.completed":
        response = dumped.get("response")
        clean_response = _sanitize_response_stream_payload(response)
        if clean_response is response:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["response"] = clean_response
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    return chunk


def _sanitize_web_search_stream_chunk(chunk: Any) -> Optional[Any]:
    dumped = _streaming_module._stream_chunk_dump(chunk)
    if not isinstance(dumped, dict) or not dumped:
        return chunk

    chunk_type = _streaming_module._stream_chunk_type(dumped)
    if chunk_type.startswith("response.reasoning"):
        return None

    if chunk_type in {"response.output_item.added", "response.output_item.done"}:
        item = dumped.get("item")
        if _is_reasoning_output_item(item):
            return None
        if not isinstance(item, dict):
            return chunk
        if item.get("type") == "web_search_call":
            clean_item = _sanitize_web_search_call_item(item)
        elif _is_provider_hosted_web_search_item(item):
            clean_item = _provider_hosted_web_search_call_item(item)
        else:
            return chunk
        if clean_item is None:
            return None
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["item"] = clean_item
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    if chunk_type.startswith("response.web_search_call."):
        action = dumped.get("action")
        if not isinstance(action, dict):
            return chunk
        clean_item = _sanitize_web_search_call_item(
            {
                "id": dumped.get("item_id"),
                "type": "web_search_call",
                "status": "completed",
                "query": dumped.get("query"),
                "action": action,
            }
        )
        if clean_item is None:
            return None
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["item_id"] = clean_item.get("id")
        clean_chunk["action"] = copy.deepcopy(clean_item.get("action", {}))
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    if chunk_type == "response.completed":
        response = dumped.get("response")
        clean_response = _sanitize_response_stream_payload(response)
        if clean_response is response:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["response"] = clean_response
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    return chunk


def _external_web_search_source_urls(structured: Any, text: str) -> list[str]:
    urls: list[str] = []

    def add_url(value: Any) -> None:
        if not isinstance(value, str):
            return
        candidate = value.strip().rstrip(").,;]")
        if candidate.startswith(("http://", "https://")) and candidate not in urls:
            urls.append(candidate)

    def visit(value: Any, depth: int = 0) -> None:
        if value is None or depth > 6 or len(urls) >= 10:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in {"url", "href"}:
                    add_url(child)
                if len(urls) >= 10:
                    return
                if isinstance(child, (dict, list, tuple)):
                    visit(child, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth + 1)
                if len(urls) >= 10:
                    return

    visit(_streaming_module._jsonable(structured))
    for match in re.finditer(r"https?://[^\s<>\"]+", text or ""):
        add_url(match.group(0))
        if len(urls) >= 10:
            break
    return urls


def _external_web_search_call_item(
    queries: list[str],
    source_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    clean_queries = [query for query in queries if isinstance(query, str) and query.strip()]
    if not clean_queries:
        return None
    primary_query = clean_queries[0] if clean_queries else ""
    action: dict[str, Any] = {
        "type": "search",
        "query": primary_query,
    }
    if clean_queries:
        action["queries"] = clean_queries
    return _external_web_search_call_item_for_action(action, source_urls)


def _external_web_search_call_item_for_action(
    action: dict[str, Any],
    source_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    item_id = f"ws_bridge_{os.getpid()}_{time.time_ns()}"
    clean_action = copy.deepcopy(action)
    action_type = clean_action.get("type")
    if action_type in {"openPage", "findInPage"}:
        label_action: dict[str, str] = {
            "type": str(action_type),
            "url": str(clean_action.get("url") or ""),
            "pattern": str(clean_action.get("pattern") or ""),
        }
        # Codex currently renders unknown action types as a blank "other" row.
        clean_action["query"] = _external_web_search_action_label(label_action)
        clean_action["type"] = "search"
    item = {
        "id": item_id,
        "type": "web_search_call",
        "status": "completed",
        "action": clean_action,
    }
    return _sanitize_web_search_call_item(item, source_urls)


def _external_web_search_call_items(
    queries: list[str],
    source_urls_by_query: Optional[list[list[str]]] = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, query in enumerate(queries):
        if not isinstance(query, str) or not query.strip():
            continue
        urls: list[str] = []
        if source_urls_by_query is not None and index < len(source_urls_by_query):
            urls = source_urls_by_query[index]
        item = _external_web_search_call_item([query.strip()], urls)
        if item is not None:
            items.append(item)
    return items


def _with_external_web_search_call_items(
    response: Any,
    queries: list[str],
    source_urls_by_query: Optional[list[list[str]]] = None,
) -> Any:
    if not queries:
        return response
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return response
    payload = _sanitize_response_web_search_call_items(payload)
    output = payload.get("output")
    if not isinstance(output, list):
        output = []
        payload["output"] = output
    if any(isinstance(item, dict) and item.get("type") == "web_search_call" for item in output):
        return payload
    for item in reversed(_external_web_search_call_items(queries, source_urls_by_query)):
        output.insert(0, item)
    return payload


def _with_external_web_search_call_action_items(
    response: Any,
    actions: list[dict[str, str]],
    source_urls_by_action: Optional[list[list[str]]] = None,
) -> Any:
    if not actions:
        return response
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return response
    payload = _sanitize_response_web_search_call_items(payload)
    output = payload.get("output")
    if not isinstance(output, list):
        output = []
        payload["output"] = output
    if any(isinstance(item, dict) and item.get("type") == "web_search_call" for item in output):
        return payload
    items: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        urls: list[str] = []
        if source_urls_by_action is not None and index < len(source_urls_by_action):
            urls = source_urls_by_action[index]
        item = _external_web_search_call_item_for_action(action, urls)
        if item is not None:
            items.append(item)
    for item in reversed(items):
        output.insert(0, item)
    return payload

def _litellm_web_search_queries_from_response(response: Any) -> list[str]:
    actions = _litellm_web_search_actions_from_response(response)
    queries: list[str] = []
    for action in actions:
        if action.get("type") != "search":
            continue
        query = action.get("query")
        if query and query not in queries:
            queries.append(query)
    return queries


def _litellm_web_search_actions_from_response(response: Any) -> list[dict[str, str]]:
    calls = _litellm_web_search_function_calls(response)
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for call in calls:
        action = _litellm_web_search_action_from_call(call)
        action = _external_web_search_valid_action(action)
        if not action:
            continue
        key = _external_web_search_action_key(action)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    return actions


def _litellm_web_search_actions_for_request(
    response: Any,
    request_kwargs: Optional[dict],
) -> list[dict[str, str]]:
    _ = request_kwargs
    return _litellm_web_search_actions_from_response(response)


def _external_web_search_request_needs_source_inspection(
    request_kwargs: Optional[dict],
) -> bool:
    user_text = _external_web_search_user_prompt_text(request_kwargs)
    request_text = (
        ""
        if _external_web_search_is_internal_prompt_request(request_kwargs)
        else _external_web_search_request_text(request_kwargs)
    )
    text = " ".join(part for part in (user_text, request_text) if part).strip()
    if not text:
        return False
    return bool(
        re.search(
            r"(?:"
            r"深挖|深入(?:调查|研究|分析)?|调查(?:一下|下)?|核验|核查|验证|求证|查证|考证|"
            r"证实|证伪|溯源|原文|原始(?:来源|出处)|一手(?:来源|资料)|可审计|"
            r"证据链|逐条(?:标注|引用)|是否(?:成立|可信|可靠|准确|为真|可以|能够|能|会)|"
            r"能不能|可不可以|是不是|真假|真伪|机制|因果|原因|影响|效果|风险|评估|评价|"
            r"\bdeep\s*dive\b|\binvestigat(?:e|ion|ing)\b|\bresearch\b|"
            r"\bverif(?:y|ication)\b|\bvalidat(?:e|ion)\b|\bfact[-\s]?check\b|"
            r"\baudit(?:able)?\b|\bcross[-\s]?check\b|\bprimary\s+source\b|"
            r"\boriginal\s+source\b|\bsource\s+page\b|\bevidence\s+(?:trail|chain)\b|"
            r"\bconfirm\s+whether\b|\bdetermine\s+whether\b|\bis\s+it\s+true\b|"
            r"\bclaim\b|\bcaus(?:e|al|ation)\b|\bmechanism\b|\bwhy\b|"
            r"\bimpact\b|\beffect\b|\brisk\b|\bassess\b|\bevaluate\b"
            r")",
            text,
            flags=re.IGNORECASE,
        )
    )


def _external_web_search_has_source_page_action(
    completed_actions: Optional[list[dict[str, str]]],
) -> bool:
    return any(
        isinstance(action, dict) and action.get("type") in {"openPage", "findInPage"}
        for action in completed_actions or []
    )


def _external_web_search_response_has_search_only_actions(
    response: Any,
    request_kwargs: Optional[dict],
) -> bool:
    actions = _litellm_web_search_actions_for_request(response, request_kwargs)
    if not actions:
        return False
    return not _external_web_search_has_source_page_action(actions)


def _external_web_search_source_read_required_for_continuation(
    request_kwargs: Optional[dict],
    completed_actions: Optional[list[dict[str, str]]],
    source_urls: list[str],
    search_results: str,
) -> bool:
    if not _external_web_search_request_needs_source_inspection(request_kwargs):
        return False
    if _external_web_search_has_source_page_action(completed_actions):
        return False
    if source_urls:
        return True
    return bool(_external_web_search_result_cards(search_results))


def _external_web_search_candidate_source_urls(
    source_urls: list[str],
    search_results: str,
    *,
    limit: int = 5,
) -> list[str]:
    urls: list[str] = []

    def add(value: Any) -> None:
        if len(urls) >= limit:
            return
        url = _external_web_search_clean_url(value)
        if url and url not in urls:
            urls.append(url)

    for url in source_urls:
        add(url)
    for card in _external_web_search_result_cards(search_results):
        add(card.get("url"))
    return urls


def _external_web_search_source_cards_for_planning(
    source_urls: list[str],
    search_results: str,
    *,
    limit: int = 5,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    def add_card(title: Any, url: Any, snippet: Any = "") -> None:
        if len(cards) >= limit:
            return
        clean_url = _external_web_search_clean_url(url)
        if not clean_url or clean_url in seen_urls:
            return
        seen_urls.add(clean_url)
        clean_title = " ".join(str(title or clean_url).split()) or clean_url
        clean_snippet = " ".join(str(snippet or "").split())
        if len(clean_snippet) > 260:
            clean_snippet = clean_snippet[:260].rstrip() + "..."
        cards.append(
            {
                "title": clean_title,
                "url": clean_url,
                "snippet": clean_snippet,
            }
        )

    for card in _external_web_search_result_cards(search_results):
        add_card(card.get("title"), card.get("url"), card.get("snippet"))
    for url in source_urls:
        add_card(url, url, "")
    return cards


def _external_web_search_source_planning_evidence(
    source_urls: list[str],
    search_results: str,
) -> str:
    cards = _external_web_search_source_cards_for_planning(source_urls, search_results)
    lines: list[str] = []
    for index, card in enumerate(cards, start=1):
        lines.append(f"{index}. {card['title']}")
        lines.append(f"   URL: {card['url']}")
        snippet = card.get("snippet") or ""
        if snippet:
            lines.append(f"   Snippet: {snippet}")
    return "\n".join(lines).strip()


def _external_web_search_find_patterns_for_source_inspection(
    request_kwargs: Optional[dict],
    completed_actions: list[dict[str, str]],
    *,
    max_patterns: int = 4,
) -> list[str]:
    stopwords = {
        "about",
        "against",
        "analysis",
        "and",
        "article",
        "assess",
        "audit",
        "cancer",
        "claim",
        "current",
        "deep",
        "determine",
        "effect",
        "evidence",
        "find",
        "for",
        "from",
        "inhibit",
        "inhibition",
        "inside",
        "investigate",
        "latest",
        "lookup",
        "mechanism",
        "page",
        "paper",
        "read",
        "research",
        "result",
        "results",
        "search",
        "source",
        "study",
        "the",
        "transport",
        "transporter",
        "verify",
        "whether",
        "with",
    }
    cjk_stopwords = {
        "一下",
        "不使用",
        "是否",
        "查证",
        "核验",
        "深挖",
        "调查",
        "证据",
    }
    patterns: list[str] = []

    def add(value: Any) -> None:
        if len(patterns) >= max_patterns:
            return
        if not isinstance(value, str):
            return
        pattern = value.strip().strip("'\"`()[]{}<>.,;:!?，。；：！？、")
        pattern = re.sub(
            r"^(?:深挖|深入|调查|查证|核验|核查|验证|求证|研究|分析)+",
            "",
            pattern,
        ).strip()
        pattern = re.sub(
            r"(?:是否|能否|能不能|可否|可不可以)$",
            "",
            pattern,
        ).strip()
        if len(pattern) < 2 or len(pattern) > 48:
            return
        lowered = pattern.lower()
        if lowered in stopwords or pattern in cjk_stopwords:
            return
        if re.fullmatch(r"(?:19|20)\d{2}", pattern):
            return
        if pattern.startswith(("http://", "https://")):
            return
        if lowered in {item.lower() for item in patterns}:
            return
        patterns.append(pattern)

    search_text_parts = [
        action.get("query", "")
        for action in completed_actions
        if isinstance(action, dict) and action.get("type") == "search"
    ]
    user_text = _external_web_search_user_prompt_text(request_kwargs)
    search_text = "\n".join(part for part in search_text_parts if part)

    for text in (search_text, user_text):
        if not isinstance(text, str) or not text.strip():
            continue
        for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9-]{1,31}\b", text):
            add(match.group(0))
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}", text):
            add(match.group(0))
        if len(patterns) >= max_patterns:
            break
    return patterns


def _external_web_search_auto_source_inspection_actions(
    request_kwargs: Optional[dict],
    *,
    completed_actions: list[dict[str, str]],
    source_urls: list[str],
    search_results: str,
) -> list[dict[str, str]]:
    if not _external_web_search_source_read_required_for_continuation(
        request_kwargs,
        completed_actions,
        source_urls,
        search_results,
    ):
        return []

    candidate_urls = _external_web_search_candidate_source_urls(
        source_urls,
        search_results,
    )
    if not candidate_urls:
        return []

    open_remaining = _external_web_search_max_open_pages() - sum(
        1 for action in completed_actions if action.get("type") == "openPage"
    )
    find_remaining = _external_web_search_max_find_in_page() - sum(
        1 for action in completed_actions if action.get("type") == "findInPage"
    )
    if open_remaining <= 0 and find_remaining <= 0:
        return []

    selected_urls = candidate_urls[:1]
    actions: list[dict[str, str]] = []
    if open_remaining > 0:
        actions.append({"type": "openPage", "url": selected_urls[0]})

    if find_remaining > 0:
        find_budget = min(3, find_remaining)
        patterns = _external_web_search_find_patterns_for_source_inspection(
            request_kwargs,
            completed_actions,
            max_patterns=find_budget,
        )
        for pattern in patterns[:find_budget]:
            actions.append(
                {"type": "findInPage", "url": selected_urls[0], "pattern": pattern}
            )

    return _external_web_search_budgeted_actions(actions, completed_actions)


def _external_web_search_force_low_reasoning(
    value: Any,
    *,
    in_reasoning: bool = False,
) -> tuple[Any, bool]:
    if not isinstance(value, dict):
        return value, False

    changed = False
    updated: dict[Any, Any] = {}
    for key, item in value.items():
        if key == "reasoning_effort" and isinstance(item, str) and item.strip():
            updated[key] = "low"
            changed = changed or item.strip().lower() != "low"
            continue
        if key == "reasoning" and isinstance(item, dict):
            mapped_item, item_changed = (
                _external_web_search_force_low_reasoning(
                    item,
                    in_reasoning=True,
                )
            )
            updated[key] = mapped_item
            changed = changed or item_changed
            continue
        if in_reasoning and key == "effort" and isinstance(item, str) and item.strip():
            updated[key] = "low"
            changed = changed or item.strip().lower() != "low"
            continue
        if key in {"extra_body", "litellm_params"} and isinstance(item, dict):
            mapped_item, item_changed = (
                _external_web_search_force_low_reasoning(item)
            )
            updated[key] = mapped_item
            changed = changed or item_changed
            continue
        updated[key] = item

    return (updated if changed else value), changed


def _external_web_search_low_reasoning_kwargs(
    request_kwargs: Optional[dict],
    *,
    force_top_level: bool = False,
) -> dict[str, Any]:
    low_kwargs = _external_web_search_safe_request_base(request_kwargs)
    mapped_kwargs, _ = _external_web_search_force_low_reasoning(
        low_kwargs
    )
    if isinstance(mapped_kwargs, dict):
        low_kwargs = mapped_kwargs.copy()
    if force_top_level or "reasoning" in low_kwargs:
        low_kwargs["reasoning"] = {"effort": "low"}
    if "reasoning_effort" in low_kwargs:
        low_kwargs["reasoning_effort"] = "low"
    return low_kwargs


def _external_web_search_progress_preamble_reason(text: str) -> Optional[str]:
    compact = " ".join(str(text or "").split()).strip()
    if not compact or len(compact) > 1200:
        return None
    lowered = compact.lower()
    if _RAW_TOOL_CALL_START in lowered:
        return None
    if "mcp__" in lowered and "web_search" in lowered:
        return None
    if re.search(r"\bweb[_ -]?search\s*\(", lowered):
        return None

    cjk_action = (
        r"(?:搜索|检索|查找|查证|核查|核实|核验|验证|确认|求证|获取|收集|阅读|打开|"
        r"调用|执行|深挖|调查|研究|继续查|再查)"
    )
    cjk_starter = r"(?:我(?:将|来|会|要|需要)|让我|现在我(?:来|将|会|要|需要)?|接下来我|下面我|先|继续|再)"
    if re.search(cjk_starter + r"[^。！？\n]{0,120}" + cjk_action, compact):
        return "web_search_progress_preamble"

    english_action = (
        r"(?:search|look\s+up|verify|check|confirm|fetch|collect|read|open|"
        r"investigate|research|continue|run\s+parallel\s+searches)"
    )
    english_starter = (
        r"(?:i\s*(?:will|'ll|’ll|am\s+going\s+to|need\s+to|can)|"
        r"let\s+me|now\s+i\s*(?:will|'ll|’ll|am\s+going\s+to)?|"
        r"next\s+i\s*(?:will|'ll|’ll|am\s+going\s+to)?|i'll\s+continue)"
    )
    if re.search(
        english_starter + r"[^.!?\n]{0,180}\b" + english_action + r"\b",
        lowered,
    ):
        return "web_search_progress_preamble"

    return None


def _external_web_search_final_answer_without_tool_call(response: Any) -> bool:
    if _litellm_web_search_function_calls(response):
        return False
    if _external_web_search_has_completed_assistant_message(response):
        return True
    text = _image_generation_module._response_text(response)
    return bool(text.strip()) and not _external_web_search_progress_preamble_reason(text)


def _external_web_search_completed_assistant_message_items(response: Any) -> list[dict[str, Any]]:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return []
    response_status = payload.get("status")
    if isinstance(response_status, str) and response_status not in {"completed", ""}:
        return []
    output = payload.get("output")
    if not isinstance(output, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role")
        if isinstance(role, str) and role != "assistant":
            continue
        status = item.get("status")
        if isinstance(status, str) and status not in {"completed", ""}:
            continue
        if _image_generation_module._response_text(item).strip():
            messages.append(item)
    return messages


def _external_web_search_has_completed_assistant_message(response: Any) -> bool:
    return bool(_external_web_search_completed_assistant_message_items(response))


def _has_litellm_web_search_actions_for_request(
    response: Any,
    request_kwargs: Optional[dict],
) -> bool:
    return bool(_litellm_web_search_actions_for_request(response, request_kwargs))


def _litellm_web_search_queries_for_request(
    response: Any,
    request_kwargs: Optional[dict],
) -> list[str]:
    queries: list[str] = []
    for action in _litellm_web_search_actions_for_request(response, request_kwargs):
        if action.get("type") != "search":
            continue
        query = action.get("query")
        if query and query not in queries:
            queries.append(query)
    return queries


async def _external_web_search_run_query(query: str) -> tuple[str, list[str]]:
    try:
        text, structured = await asyncio.to_thread(_external_web_search_module._ddgs_jina_web_search_sync, query)
        urls = _external_web_search_source_urls(structured, text)
    except Exception as exc:
        text = f"Search failed for query {query!r}: {exc}"
        urls = []
    return f"Web search results for query: {query}\n\n{text}", urls


async def _external_web_search_run_queries(
    queries: list[str],
) -> tuple[str, list[str], list[list[str]]]:
    query_results = await asyncio.gather(
        *(_external_web_search_run_query(query) for query in queries)
    )
    sections = [section for section, _urls in query_results]
    source_urls_by_query = [urls for _section, urls in query_results]
    source_urls: list[str] = []
    for _section, urls in query_results:
        for url in urls:
            if url not in source_urls:
                source_urls.append(url)
    message = "\n\n".join(section for section in sections if section.strip())
    return message, source_urls, source_urls_by_query


def _external_web_search_page_read_chars() -> int:
    return _external_web_search_module._external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_READ_CHARS_ENV,
        _EXTERNAL_WEB_SEARCH_READ_CHARS_DEFAULT * 3,
        500,
        12000,
    )


def _external_web_search_page_timeout_seconds() -> float:
    return _external_web_search_float_env(
        _EXTERNAL_WEB_FETCH_TIMEOUT_ENV,
        _EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT,
        3.0,
        60.0,
    )


async def _external_web_search_fetch_page_text(url: str) -> str:
    try:
        text = await asyncio.to_thread(
            _external_web_search_module._jina_reader_excerpt,
            url,
            timeout=_external_web_search_page_timeout_seconds(),
            max_chars=_external_web_search_page_read_chars(),
        )
    except Exception as exc:
        return f"Page retrieval failed for URL {url!r}: {exc}"
    if text.strip():
        return text
    return f"Page retrieval returned no readable text for URL: {url}"


async def _external_web_search_page_text(
    url: str,
    page_cache: dict[str, str],
    page_fetch_tasks: dict[str, asyncio.Task[str]],
) -> str:
    cached = page_cache.get(url)
    if isinstance(cached, str) and cached.strip():
        return cached

    fetch_task = page_fetch_tasks.get(url)
    if fetch_task is None:
        fetch_task = asyncio.create_task(_external_web_search_fetch_page_text(url))
        page_fetch_tasks[url] = fetch_task

    try:
        text = await fetch_task
    finally:
        if page_fetch_tasks.get(url) is fetch_task and fetch_task.done():
            page_fetch_tasks.pop(url, None)

    if not isinstance(text, str) or not text.strip():
        text = f"Page retrieval returned no readable text for URL: {url}"
    page_cache[url] = text
    return text


async def _external_web_search_open_page(
    url: str,
    page_cache: dict[str, str],
    page_fetch_tasks: dict[str, asyncio.Task[str]],
) -> tuple[str, list[str]]:
    text = await _external_web_search_page_text(url, page_cache, page_fetch_tasks)
    return f"Retrieved page content for URL: {url}\n\n{text}", [url]


def _external_web_search_find_matches(
    text: str,
    pattern: str,
    *,
    max_matches: int = 8,
    context_chars: int = 180,
) -> list[str]:
    if not text or not pattern:
        return []
    matches: list[str] = []
    lowered_text = text.lower()
    lowered_pattern = pattern.lower()
    start = 0
    while len(matches) < max_matches:
        index = lowered_text.find(lowered_pattern, start)
        if index < 0:
            break
        left = max(0, index - context_chars)
        right = min(len(text), index + len(pattern) + context_chars)
        snippet = " ".join(text[left:right].split())
        if left > 0:
            snippet = "..." + snippet
        if right < len(text):
            snippet = snippet + "..."
        matches.append(snippet)
        start = index + max(1, len(pattern))
    return matches


async def _external_web_search_find_in_page(
    url: str,
    pattern: str,
    page_cache: dict[str, str],
    page_fetch_tasks: dict[str, asyncio.Task[str]],
) -> tuple[str, list[str]]:
    text = await _external_web_search_page_text(url, page_cache, page_fetch_tasks)
    matches = _external_web_search_find_matches(text, pattern)
    if matches:
        body = "\n".join(f"- {match}" for match in matches)
    else:
        body = f"No readable matches for pattern {pattern!r}."
    return f"Page text matches for pattern: {pattern}\nURL: {url}\n\n{body}", [url]


async def _external_web_search_run_action(
    action: dict[str, str],
    page_cache: dict[str, str],
    page_fetch_tasks: dict[str, asyncio.Task[str]],
) -> tuple[str, list[str], dict[str, str]]:
    action_type = action.get("type")
    if action_type == "openPage":
        url = action.get("url", "")
        section, urls = await _external_web_search_open_page(
            url,
            page_cache,
            page_fetch_tasks,
        )
        return section, urls, action
    if action_type == "findInPage":
        url = action.get("url", "")
        pattern = action.get("pattern", "")
        section, urls = await _external_web_search_find_in_page(
            url,
            pattern,
            page_cache,
            page_fetch_tasks,
        )
        return section, urls, action
    query = action.get("query", "")
    section, urls = await _external_web_search_run_query(query)
    return section, urls, action


async def _external_web_search_run_actions(
    actions: list[dict[str, str]],
    page_cache: dict[str, str],
    page_fetch_tasks: dict[str, asyncio.Task[str]],
    request_kwargs: Optional[dict] = None,
) -> tuple[str, list[str], list[list[str]], list[dict[str, str]]]:
    _mark_external_web_search_started(request_kwargs)
    action_results = await asyncio.gather(
        *(
            _external_web_search_run_action(action, page_cache, page_fetch_tasks)
            for action in actions
        )
    )
    sections = [section for section, _urls, _action in action_results]
    source_urls_by_action = [urls for _section, urls, _action in action_results]
    completed_actions = [action for _section, _urls, action in action_results]
    source_urls: list[str] = []
    for _section, urls, _action in action_results:
        for url in urls:
            if url not in source_urls:
                source_urls.append(url)
    message = "\n\n".join(section for section in sections if section.strip())
    return message, source_urls, source_urls_by_action, completed_actions


def _external_web_search_max_rounds() -> int:
    return _external_web_search_module._external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_ROUNDS_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_ROUNDS_DEFAULT,
        1,
        8,
    )


def _external_web_search_max_queries() -> int:
    return _external_web_search_module._external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_QUERIES_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_QUERIES_DEFAULT,
        1,
        64,
    )


def _external_web_search_max_open_pages() -> int:
    return _external_web_search_module._external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_DEFAULT,
        0,
        32,
    )


def _external_web_search_max_find_in_page() -> int:
    return _external_web_search_module._external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_DEFAULT,
        0,
        64,
    )


def _external_web_search_new_queries(
    queries: list[str],
    completed_queries: list[str],
) -> list[str]:
    completed_normalized = {query.strip().lower() for query in completed_queries}
    new_queries: list[str] = []
    for query in queries:
        if not isinstance(query, str):
            continue
        clean_query = query.strip()
        if not clean_query:
            continue
        normalized = clean_query.lower()
        if normalized in completed_normalized:
            continue
        if normalized in {item.lower() for item in new_queries}:
            continue
        new_queries.append(clean_query)
    return new_queries


def _external_web_search_budgeted_queries(
    queries: list[str],
    completed_queries: list[str],
) -> list[str]:
    remaining = _external_web_search_max_queries() - len(completed_queries)
    if remaining <= 0:
        return []
    return _external_web_search_new_queries(queries, completed_queries)[:remaining]


def _external_web_search_budgeted_actions(
    actions: list[dict[str, str]],
    completed_actions: list[dict[str, str]],
) -> list[dict[str, str]]:
    completed_keys = {
        _external_web_search_action_key(action) for action in completed_actions
    }
    search_remaining = _external_web_search_max_queries() - sum(
        1 for action in completed_actions if action.get("type") == "search"
    )
    open_remaining = _external_web_search_max_open_pages() - sum(
        1 for action in completed_actions if action.get("type") == "openPage"
    )
    find_remaining = _external_web_search_max_find_in_page() - sum(
        1 for action in completed_actions if action.get("type") == "findInPage"
    )
    selected: list[dict[str, str]] = []
    selected_keys: set[str] = set()
    for action in actions:
        key = _external_web_search_action_key(action)
        if key in completed_keys or key in selected_keys:
            continue
        action_type = action.get("type")
        if action_type == "openPage":
            if open_remaining <= 0:
                continue
            open_remaining -= 1
        elif action_type == "findInPage":
            if find_remaining <= 0:
                continue
            find_remaining -= 1
        else:
            if search_remaining <= 0:
                continue
            search_remaining -= 1
        selected.append(action)
        selected_keys.add(key)
    return selected


def _external_web_search_bridge_chat_tool() -> dict[str, Any]:
    tool = _responses_tools_module._responses_bridge_web_search_tool({"type": "web_search"})
    if tool is not None:
        return tool
    return {
        "type": "function",
        "name": _WEB_SEARCH_BRIDGE_FUNCTION_NAME,
        "description": (
            "Search the web for external or current information. Provide query "
            "for a new lookup, url to read a known source page, or url plus "
            "pattern to find text within that page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "url": {"type": "string"},
                "pattern": {"type": "string"},
            },
            "required": [],
        },
    }


def _external_web_search_synthesis_invalid_reason(response: Any) -> Optional[str]:
    if _litellm_web_search_function_calls(response):
        return "web_search_function_call"
    if _external_web_search_has_completed_assistant_message(response):
        return None
    text = _image_generation_module._response_text(response)
    if not text.strip():
        return "empty_synthesis"
    progress_reason = _external_web_search_progress_preamble_reason(text)
    if progress_reason is not None:
        return progress_reason
    compact = " ".join(text.split())
    lowered = compact.lower()
    if re.search(r"<\s*/?\s*tool_call\b", lowered):
        return "tool_call_markup"
    if "mcp__" in lowered and "web_search" in lowered:
        return "mcp_web_search_placeholder"
    if re.search(r"\bweb[_ -]?search\s*\(", lowered):
        return "web_search_call_syntax"
    return None


def _external_web_search_initial_no_action_invalid_reason(response: Any) -> Optional[str]:
    if _litellm_web_search_function_calls(response):
        return None
    if _external_web_search_has_completed_assistant_message(response):
        return None
    text = _image_generation_module._response_text(response)
    if not text.strip():
        return None
    progress_reason = _external_web_search_progress_preamble_reason(text)
    if progress_reason is not None:
        return progress_reason
    compact = " ".join(text.split())
    lowered = compact.lower()
    if re.search(r"<\s*/?\s*tool_call\b", lowered):
        return "tool_call_markup"
    if "mcp__" in lowered and "web_search" in lowered:
        return "mcp_web_search_placeholder"
    if re.search(r"\bweb[_ -]?search\s*\(", lowered):
        return "web_search_call_syntax"
    return None


def _external_web_search_raise_if_invalid_initial_no_action_response(
    response: Any,
    request_kwargs: Optional[dict],
) -> None:
    reason = _external_web_search_initial_no_action_invalid_reason(response)
    if reason is None:
        return
    _trace_module._route_trace(
        "external_web_search_bridge_initial_no_action_invalid",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        invalid_reason=reason,
        response_preview=_trace_module._sanitize_trace_text(_image_generation_module._response_text(response)),
    )
    exception = _external_web_search_invalid_synthesis_exception(
        request_kwargs,
        reason=reason,
        phase="initial",
    )
    raise exception


def _external_web_search_result_cards(search_results: str) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    pattern = re.compile(
        r"(?ms)^Title:\s*(?P<title>.*?)\n"
        r"URL:\s*(?P<url>\S+)\n"
        r"Snippet:\s*(?P<snippet>.*?)(?=\n\nTitle:|\n\nWeb search results for query:|\n\nJina Reader excerpt:|\n\nMarkdown Content:|\Z)"
    )
    for match in pattern.finditer(search_results or ""):
        url = match.group("url").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        snippet_source = re.split(
            r"\n\s*\n|Jina Reader excerpt:|Markdown Content:",
            match.group("snippet"),
            maxsplit=1,
        )[0]
        snippet = " ".join(snippet_source.split())
        if len(snippet) > 420:
            snippet = snippet[:420].rstrip() + "..."
        cards.append(
            {
                "title": " ".join(match.group("title").split()) or url,
                "url": url,
                "snippet": snippet,
            }
        )
        if len(cards) >= 5:
            break
    return cards


_EXTERNAL_WEB_SEARCH_SYNTHESIS_EVIDENCE_MAX_CHARS = 6000
_EXTERNAL_WEB_SEARCH_SYNTHESIS_SECTION_MAX_CHARS = 1400
_EXTERNAL_WEB_SEARCH_CONTINUATION_EVIDENCE_MAX_CHARS = 2200
_EXTERNAL_WEB_SEARCH_CONTINUATION_SECTION_MAX_CHARS = 520
_EXTERNAL_WEB_SEARCH_CONTINUATION_OUTPUT_TOKENS = 512
_EXTERNAL_WEB_SEARCH_SYNTHESIS_OUTPUT_TOKENS = 1536
_EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID: dict[int, dict[str, Any]] = {}
_EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_MAX = 256
_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY = "external_web_search_original_user_text"
_EXTERNAL_WEB_SEARCH_PENDING_RECOVERY_REQUEST_KEY = (
    "external_web_search_pending_recovery_request"
)
_EXTERNAL_WEB_SEARCH_REQUEST_BASE_KEYS = (
    "call_type",
    "model",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "parallel_tool_calls",
    "reasoning",
    "reasoning_effort",
    "user",
    "service_tier",
    "seed",
    "stop",
    "response_format",
    "stream",
    "stream_options",
    "stream_timeout",
    "api_base",
    "api_key",
    "api_version",
    "custom_llm_provider",
    "extra_body",
    "extra_headers",
    "input",
    "instructions",
    "max_output_tokens",
    "max_completion_tokens",
    "truncation",
    "text",
    "include",
    "store",
    "previous_response_id",
    "client_metadata",
    "prompt_cache_key",
    "messages",
    "functions",
    "function_call",
    "modalities",
    "audio",
    "metadata",
    "litellm_metadata",
    "model_info",
    "litellm_params",
    "_target_order",
    "_excluded_deployment_ids",
)
_EXTERNAL_WEB_SEARCH_INTERNAL_REQUEST_KEYS = {
    _EXTERNAL_WEB_SEARCH_PENDING_RECOVERY_REQUEST_KEY,
    "proxy_server_request",
    "ssl_context",
    "sslcontext",
    "http_client",
    "async_client",
    "client",
    "session",
}


def _external_web_search_safe_json_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or depth > 8:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        cleaned_list = [
            item
            for item in (
                _external_web_search_safe_json_value(item, depth=depth + 1)
                for item in value
            )
            if item is not None
        ]
        return cleaned_list
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _EXTERNAL_WEB_SEARCH_INTERNAL_REQUEST_KEYS:
                continue
            cleaned_item = _external_web_search_safe_json_value(
                item,
                depth=depth + 1,
            )
            if cleaned_item is not None:
                cleaned[key_text] = cleaned_item
        return cleaned
    json_value = _streaming_module._jsonable(value)
    if json_value is None or json_value is value:
        return json_value
    return _external_web_search_safe_json_value(json_value, depth=depth + 1)


def _external_web_search_safe_payload_copy(
    request_kwargs: Optional[dict],
) -> dict[str, Any]:
    payload = _external_web_search_safe_json_value(request_kwargs or {})
    if isinstance(payload, dict):
        return copy.deepcopy(payload)
    return {}


def _external_web_search_safe_request_base(
    request_kwargs: Optional[dict],
) -> dict[str, Any]:
    if not isinstance(request_kwargs, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in _EXTERNAL_WEB_SEARCH_REQUEST_BASE_KEYS:
        if key not in request_kwargs:
            continue
        value = _external_web_search_safe_json_value(request_kwargs.get(key))
        if value is not None:
            payload[key] = value
    return payload


def _external_web_search_metadata_original_user_text(
    request_kwargs: Optional[dict],
) -> str:
    if not isinstance(request_kwargs, dict):
        return ""
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs,
            metadata_key,
        ) or {}
        text = metadata.get(_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _external_web_search_extract_internal_prompt_user_text(text: str) -> Optional[str]:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    patterns = (
        (
            r"(?is)^Original user request\. Any instruction to call or use web_search "
            r"has already been satisfied by the compatibility bridge:\s*(.*?)"
            r"(?:\n\s*\n(?:Authoritative time context:|Retrieved evidence:|Now answer)|\Z)"
        ),
        (
            r"(?is)^Original user request:\s*(.*?)"
            r"(?:\n\s*\n(?:Authoritative time context:|Web actions completed so far:|"
            r"Candidate source URLs from search results:|Retrieved evidence observed so far:|"
            r"Return a tool call now\.|Decide the next step now:)|\Z)"
        ),
    )
    for pattern in patterns:
        match = re.match(pattern, stripped)
        if match:
            extracted = match.group(1).strip()
            return extracted or None
    return None


def _external_web_search_normalize_user_prompt_text(text: str) -> str:
    current = str(text or "").strip()
    for _ in range(6):
        extracted = _external_web_search_extract_internal_prompt_user_text(current)
        if not extracted or extracted == current:
            break
        current = extracted.strip()
    if not current:
        return ""
    matches = re.findall(
        r"<input\b[^>]*>(.*?)</input>",
        current,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if matches:
        current = matches[-1]
    current = re.sub(r"<[^>]+>", " ", current)
    return " ".join(current.split()).strip()


def _external_web_search_is_internal_prompt_request(
    request_kwargs: Optional[dict],
) -> bool:
    metadata = _external_web_search_metadata(request_kwargs)
    if (
        metadata.get("external_web_search_continuation") is True
        or metadata.get("external_web_search_synthesis") is True
    ):
        return True
    text = _external_web_search_request_text(request_kwargs)
    return _external_web_search_extract_internal_prompt_user_text(text) is not None


def _external_web_search_trim_evidence_section(
    section: str,
    *,
    max_chars: int = _EXTERNAL_WEB_SEARCH_SYNTHESIS_SECTION_MAX_CHARS,
    label: str = "synthesis",
) -> str:
    text = section.strip()
    if len(text) <= max_chars:
        return text

    head_limit = max_chars
    for marker in ("\n\nJina Reader excerpt:", "\n\nMarkdown Content:"):
        marker_index = text.find(marker)
        if marker_index > 0:
            head_limit = min(head_limit, marker_index)
    trimmed = text[:head_limit].rstrip()
    if not trimmed:
        trimmed = text[:max_chars].rstrip()
    return f"{trimmed}\n[Evidence section trimmed for {label}.]"


def _external_web_search_evidence_sections(search_results: str) -> list[str]:
    sections = [
        section
        for section in re.split(
            r"\n\n(?=Web search results for query:|Retrieved page content for URL:|"
            r"Page text matches for pattern:|Search failed for query:|Title:)",
            search_results or "",
        )
        if section.strip()
    ]
    return sections


def _external_web_search_limited_evidence(
    search_results: str,
    *,
    max_chars: int,
    section_max_chars: int,
    label: str,
) -> str:
    sections = _external_web_search_evidence_sections(search_results)
    if not sections:
        text = (search_results or "").strip()
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars].rstrip()}\n[Evidence trimmed for {label}.]"

    compact_sections: list[str] = []
    total_chars = 0
    for section in sections:
        compact = _external_web_search_trim_evidence_section(
            section,
            max_chars=section_max_chars,
            label=label,
        )
        if not compact:
            continue
        projected = total_chars + len(compact) + (2 if compact_sections else 0)
        if projected > max_chars:
            remaining = max_chars - total_chars
            if remaining > 240:
                compact_sections.append(
                    compact[:remaining].rstrip()
                    + f"\n[Additional evidence trimmed for {label}.]"
                )
            break
        compact_sections.append(compact)
        total_chars = projected
    return "\n\n".join(compact_sections).strip()


def _external_web_search_synthesis_evidence(search_results: str) -> str:
    return _external_web_search_limited_evidence(
        search_results,
        max_chars=_EXTERNAL_WEB_SEARCH_SYNTHESIS_EVIDENCE_MAX_CHARS,
        section_max_chars=_EXTERNAL_WEB_SEARCH_SYNTHESIS_SECTION_MAX_CHARS,
        label="synthesis",
    )


def _external_web_search_continuation_evidence(search_results: str) -> str:
    return _external_web_search_limited_evidence(
        search_results,
        max_chars=_EXTERNAL_WEB_SEARCH_CONTINUATION_EVIDENCE_MAX_CHARS,
        section_max_chars=_EXTERNAL_WEB_SEARCH_CONTINUATION_SECTION_MAX_CHARS,
        label="continuation",
    )


def _external_web_search_fallback_answer(
    search_results: str,
    *,
    queries: Optional[list[str]] = None,
) -> str:
    query_text = ", ".join(query for query in (queries or []) if query)
    if query_text:
        return (
            "No usable source results were retrieved for: "
            f"{query_text}. The available evidence is insufficient "
            "to answer with source URLs."
        )
    return (
        "No usable source results were retrieved. The available evidence is "
        "insufficient to answer with source URLs."
    )


def _external_web_search_search_failed_without_sources(
    search_results: str,
    source_urls: list[str],
    completed_actions: list[dict[str, str]],
) -> bool:
    if source_urls:
        return False
    if not any(action.get("type") == "search" for action in completed_actions):
        return False
    text = search_results or ""
    if not text.strip():
        return True
    if "Title:" in text or re.search(r"(?im)^URL:\s*https?://", text):
        return False
    return "Search failed for query" in text


def _external_web_search_search_failed_without_sources_exception(
    request_kwargs: Optional[dict],
    *,
    search_results: str,
    queries: list[str],
    completed_actions: list[dict[str, str]],
    round_number: int,
) -> Exception:
    exception = _external_web_search_invalid_synthesis_exception(
        request_kwargs,
        reason="search_failed_without_sources",
        phase="search",
    )
    recovery_request = _external_web_search_prepare_continuation_recovery_request(
        request_kwargs=request_kwargs,
        search_results=search_results,
        source_urls=[],
        queries=queries,
        completed_actions=completed_actions,
        round_number=round_number,
    )
    _external_web_search_set_recovery_request(exception, recovery_request)
    return exception


def _external_web_search_message_response(
    request_kwargs: Optional[dict],
    message: str,
) -> dict[str, Any]:
    response_id = f"resp_external_web_search_{os.getpid()}_{time.time_ns()}"
    message_id = f"msg_external_web_search_{time.time_ns()}"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": str(
            _responses_execution_module._request_model_group(request_kwargs)
            or (request_kwargs or {}).get("model")
            or "unknown"
        ),
        "output_text": message,
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": message,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _external_web_search_chat_only_route(request_kwargs: Optional[dict]) -> bool:
    model_info = _image_generation_module._request_model_info(request_kwargs)
    surface = model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    if surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True

    supported_surfaces = model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
    if isinstance(supported_surfaces, list) and supported_surfaces:
        normalized = {
            surface
            for surface in supported_surfaces
            if isinstance(surface, str) and surface.strip()
        }
        if normalized and normalized.issubset(_UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES):
            return True

    return model_info.get(_RESPONSES_ENDPOINT_SUPPORT_KEY) is False


def _external_web_search_chat_synthesis_messages(
    call_kwargs: dict[str, Any],
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    instructions = call_kwargs.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})
    prompt = call_kwargs.get("input")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = _external_web_search_user_prompt_text(call_kwargs)
    messages.append({"role": "user", "content": str(prompt or "").strip()})
    return messages


def _external_web_search_chat_message_content(value: Any) -> str:
    chunks: list[str] = []

    def append_text(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            if item:
                chunks.append(item)
            return
        if isinstance(item, list):
            for child in item:
                append_text(child)
            return
        if not isinstance(item, dict):
            return
        for key in (
            "text",
            "content",
            "input_text",
            "output_text",
        ):
            append_text(item.get(key))

    append_text(value)
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _external_web_search_chat_tool_messages(
    call_kwargs: dict[str, Any],
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    instructions = call_kwargs.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})

    input_value = call_kwargs.get("input")
    if isinstance(input_value, str):
        text = input_value.strip()
        if text:
            messages.append({"role": "user", "content": text})
    elif isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            role = role if isinstance(role, str) and role.strip() else "user"
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            text = _external_web_search_chat_message_content(item.get("content"))
            if not text:
                text = _external_web_search_chat_message_content(item)
            if text:
                messages.append({"role": role, "content": text})

    if not messages or all(message.get("role") == "system" for message in messages):
        prompt = _external_web_search_user_prompt_text(call_kwargs)
        messages.append({"role": "user", "content": str(prompt or "").strip()})
    return messages


def _external_web_search_chat_completion_tools(tools: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(tools, list):
        return None
    chat_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            function_payload = copy.deepcopy(function)
        else:
            name = tool.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            function_payload = {"name": name.strip()}
            description = tool.get("description")
            if isinstance(description, str):
                function_payload["description"] = description
            parameters = tool.get("parameters")
            function_payload["parameters"] = (
                copy.deepcopy(parameters) if isinstance(parameters, dict) else {}
            )
            strict = tool.get("strict")
            if isinstance(strict, bool):
                function_payload["strict"] = strict
        chat_tools.append({"type": "function", "function": function_payload})
    return chat_tools or None


def _external_web_search_continuation_tools(
    request_kwargs: Optional[dict],
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    seen_bridge = False
    if isinstance(request_kwargs, dict) and isinstance(request_kwargs.get("tools"), list):
        for tool in request_kwargs.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if tool_type in {"web_search", "web_search_preview"}:
                continue
            copied = copy.deepcopy(tool)
            tools.append(copied)
            if _is_litellm_web_search_call_item(copied):
                seen_bridge = True
            if copied.get("type") == "function" and copied.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME:
                seen_bridge = True
            function = copied.get("function")
            if isinstance(function, dict) and function.get("name") == _WEB_SEARCH_BRIDGE_FUNCTION_NAME:
                seen_bridge = True
    if not seen_bridge:
        tools.append(_external_web_search_bridge_chat_tool())
    return tools


def _external_web_search_chat_compatible_tools(
    tools: Any,
    input_value: Any,
) -> Optional[list[dict[str, Any]]]:
    sanitized, _web_search_options, _stats = _responses_tools_module._responses_chat_bridge_sanitize_tools(
        tools,
        input_value=input_value,
        bridge_web_search=True,
    )
    if sanitized is not None:
        return _external_web_search_chat_completion_tools(sanitized)
    return _external_web_search_chat_completion_tools(tools)


def _external_web_search_chat_completion_tool_choice(value: Any) -> Any:
    if value in (None, "auto", "none", "required"):
        return value
    if not isinstance(value, dict):
        return value
    if value.get("type") != "function":
        return value
    function = value.get("function")
    if isinstance(function, dict):
        return copy.deepcopy(value)
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        return {"type": "function", "function": {"name": name.strip()}}
    return value


def _external_web_search_chat_tool_payload(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    model_group = _responses_execution_module._request_model_group(call_kwargs) or (
        call_kwargs.get("model") if isinstance(call_kwargs.get("model"), str) else None
    )
    if isinstance(model_group, str) and model_group.strip():
        payload["model"] = model_group

    payload["messages"] = _external_web_search_chat_tool_messages(call_kwargs)
    payload["stream"] = False

    chat_tools = _external_web_search_chat_compatible_tools(
        call_kwargs.get("tools"),
        call_kwargs.get("input"),
    )
    if chat_tools:
        payload["tools"] = chat_tools
    if "tool_choice" in call_kwargs:
        payload["tool_choice"] = _external_web_search_chat_completion_tool_choice(
            call_kwargs.get("tool_choice")
        )
    if isinstance(call_kwargs.get("parallel_tool_calls"), bool):
        payload["parallel_tool_calls"] = call_kwargs["parallel_tool_calls"]

    max_completion_tokens = _image_generation_module._positive_int_value(
        call_kwargs.get("max_completion_tokens")
    )
    if max_completion_tokens is None:
        max_completion_tokens = _image_generation_module._positive_int_value(
            call_kwargs.get("max_output_tokens")
        )
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens

    for key in (
        "temperature",
        "top_p",
        "reasoning",
        "user",
        "service_tier",
        "seed",
        "stop",
        "response_format",
        "metadata",
        "litellm_metadata",
        "api_base",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "extra_body",
        "extra_headers",
        "_target_order",
        "_excluded_deployment_ids",
    ):
        value = call_kwargs.get(key)
        if value is not None:
            payload[key] = copy.deepcopy(value)

    if "litellm_metadata" not in payload:
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        if metadata is not None:
            payload["litellm_metadata"] = copy.deepcopy(metadata)
    return payload


def _external_web_search_chat_synthesis_payload(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    model_group = _responses_execution_module._request_model_group(call_kwargs) or (
        call_kwargs.get("model") if isinstance(call_kwargs.get("model"), str) else None
    )
    if isinstance(model_group, str) and model_group.strip():
        payload["model"] = model_group

    payload["messages"] = _external_web_search_chat_synthesis_messages(call_kwargs)
    payload["stream"] = False

    max_completion_tokens = _image_generation_module._positive_int_value(
        call_kwargs.get("max_completion_tokens")
    )
    if max_completion_tokens is None:
        max_completion_tokens = _image_generation_module._positive_int_value(
            call_kwargs.get("max_output_tokens")
        )
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens

    for key in (
        "temperature",
        "top_p",
        "reasoning",
        "user",
        "service_tier",
        "seed",
        "stop",
        "response_format",
        "metadata",
        "litellm_metadata",
        "_target_order",
        "_excluded_deployment_ids",
    ):
        value = call_kwargs.get(key)
        if value is not None:
            payload[key] = copy.deepcopy(value)

    if "litellm_metadata" not in payload:
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        if metadata is not None:
            payload["litellm_metadata"] = copy.deepcopy(metadata)
    return payload


def _external_web_search_chat_message_text(message: Any) -> str:
    message_payload = _streaming_module._jsonable(message)
    if not isinstance(message_payload, dict):
        return ""
    chunks: list[str] = []

    def append_text(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if value:
                chunks.append(value)
            return
        if isinstance(value, list):
            for item in value:
                append_text(item)
            return
        if not isinstance(value, dict):
            return
        for key in (
            "text",
            "content",
            "output_text",
            "delta",
        ):
            append_text(value.get(key))

    for key in (
        "content",
        "output_text",
        "text",
    ):
        append_text(message_payload.get(key))
    return "".join(chunks)


def _external_web_search_chat_completion_to_response(
    chat_response: Any,
    request_kwargs: Optional[dict],
) -> dict[str, Any]:
    payload = _streaming_module._jsonable(chat_response)
    if not isinstance(payload, dict):
        payload = {}
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message")
    text = _external_web_search_chat_message_text(message)
    response = _external_web_search_message_response(request_kwargs, text)
    response["id"] = str(payload.get("id") or response["id"])
    response["model"] = str(
        payload.get("model")
        or _responses_execution_module._request_model_group(request_kwargs)
        or response.get("model")
        or "unknown"
    )
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        response["status"] = "incomplete"
        for item in response.get("output", []):
            if isinstance(item, dict):
                item["status"] = "incomplete"
    usage = payload.get("usage")
    if isinstance(usage, dict):
        response["usage"] = copy.deepcopy(usage)
    return response


async def _external_web_search_chat_synthesis_response(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
) -> Optional[Any]:
    if not _external_web_search_chat_only_route(call_kwargs):
        return None
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        llm_router = None
    acompletion = getattr(llm_router, "acompletion", None)
    if not callable(acompletion):
        return None

    payload = _tools_module._with_external_web_search_post_call_suppressed(
        _external_web_search_chat_synthesis_payload(call_kwargs, request_kwargs)
    )
    if not payload.get("model"):
        return None
    _trace_module._route_trace(
        "external_web_search_bridge_synthesis_chat_start",
        request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
        session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
        model_group=_responses_execution_module._request_model_group(call_kwargs)
        or _responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(call_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(call_kwargs),
        request=_trace_module._trace_request_summary(call_kwargs),
        retry_request=_trace_module._trace_request_summary(
            payload,
            method_name="acompletion",
        ),
    )
    chat_response = await acompletion(**payload)
    response = _external_web_search_chat_completion_to_response(
        chat_response,
        call_kwargs,
    )
    _trace_module._route_trace(
        "external_web_search_bridge_synthesis_chat_done",
        request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
        session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
        model_group=_responses_execution_module._request_model_group(call_kwargs)
        or _responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(call_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(call_kwargs),
        response=_trace_module._trace_response_summary(response, call_kwargs),
    )
    return response


async def _external_web_search_chat_tool_response(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
    *,
    phase: str,
) -> Optional[Any]:
    if not _external_web_search_chat_only_route(call_kwargs):
        return None
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        llm_router = None
    acompletion = getattr(llm_router, "acompletion", None)
    if not callable(acompletion):
        return None

    payload = _tools_module._with_external_web_search_post_call_suppressed(
        _external_web_search_chat_tool_payload(call_kwargs, request_kwargs)
    )
    if not payload.get("model"):
        return None
    _trace_module._route_trace(
        "external_web_search_bridge_chat_tool_start",
        request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
        session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
        model_group=_responses_execution_module._request_model_group(call_kwargs)
        or _responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(call_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(call_kwargs),
        phase=phase,
        request=_trace_module._trace_request_summary(call_kwargs),
        retry_request=_trace_module._trace_request_summary(
            payload,
            method_name="acompletion",
        ),
    )
    chat_response = await acompletion(**payload)
    if _litellm_web_search_function_calls(chat_response):
        response = chat_response
    else:
        response = _external_web_search_chat_completion_to_response(
            chat_response,
            call_kwargs,
        )
    _trace_module._route_trace(
        "external_web_search_bridge_chat_tool_done",
        request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
        session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
        model_group=_responses_execution_module._request_model_group(call_kwargs)
        or _responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(call_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(call_kwargs),
        phase=phase,
        response=_trace_module._trace_response_summary(response, call_kwargs),
    )
    return response


def _external_web_search_completed_actions_metadata(
    request_kwargs: Optional[dict],
) -> list[dict[str, str]]:
    metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    value = metadata.get("external_web_search_completed_actions")
    if not isinstance(value, list):
        return []
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        if any(key in item for key in ("arguments", "function", "input")):
            action = _litellm_web_search_action_from_call(item)
        else:
            action = _litellm_web_search_action_from_call({"arguments": item})
        if action is None:
            continue
        key = _external_web_search_action_key(action)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    return actions


def _external_web_search_search_results_metadata(
    request_kwargs: Optional[dict],
) -> str:
    metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    value = metadata.get("external_web_search_search_results")
    return value if isinstance(value, str) else ""


def _external_web_search_metadata(request_kwargs: Optional[dict]) -> dict[str, Any]:
    return _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}


def _mark_external_web_search_started(request_kwargs: Optional[dict]) -> None:
    _routing_module._mark_external_web_search_started_for_request(request_kwargs)
    if not isinstance(request_kwargs, dict):
        return
    metadata = _image_generation_module._request_metadata_dict(
        request_kwargs,
        "litellm_metadata",
    ) or {}
    if metadata.get(_WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY) is True:
        return
    updated_metadata = metadata.copy()
    updated_metadata[_WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY] = True
    request_kwargs["litellm_metadata"] = updated_metadata


def _external_web_search_is_recovery_payload(request_kwargs: Optional[dict]) -> bool:
    metadata = _external_web_search_metadata(request_kwargs)
    return bool(
        metadata.get("external_web_search_synthesis") is True
        or metadata.get("external_web_search_continuation") is True
    )


def _external_web_search_payload_has_embedded_evidence(
    request_kwargs: Optional[dict],
) -> bool:
    if not _external_web_search_is_recovery_payload(request_kwargs):
        return False
    text = _external_web_search_request_text(request_kwargs)
    return "Retrieved evidence" in text or "Retrieved evidence observed so far" in text


def _external_web_search_has_recovery_context(
    request_kwargs: Optional[dict],
    exception: Optional[Exception] = None,
) -> bool:
    if exception is not None and _external_web_search_recovery_request_from_exception(
        exception
    ) is not None:
        return True
    if _external_web_search_pending_recovery_request(request_kwargs) is not None:
        return True
    if _external_web_search_search_results_metadata(request_kwargs).strip():
        return True
    if _external_web_search_completed_actions_metadata(request_kwargs):
        return True
    if _external_web_search_payload_has_embedded_evidence(request_kwargs):
        return True
    return False


def _external_web_search_request_text(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    parts: list[str] = []

    def append_text(value: Any, depth: int = 0) -> None:
        if value is None or depth > 8 or len(parts) >= 80:
            return
        if isinstance(value, str):
            if value.strip():
                parts.append(value.strip())
            return
        if isinstance(value, list):
            for item in value:
                append_text(item, depth + 1)
            return
        if isinstance(value, dict):
            value_type = value.get("type")
            if value_type in {"input_text", "output_text"}:
                append_text(value.get("text"), depth + 1)
                return
            for key in ("content", "text", "input", "message"):
                if key in value:
                    append_text(value.get(key), depth + 1)

    append_text(request_kwargs.get("input"))
    append_text(request_kwargs.get("messages"))
    return "\n".join(parts[-20:]).strip()


def _external_web_search_latest_user_text(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    source_value: Any = None
    if request_kwargs.get("input") is not None:
        source_value = request_kwargs.get("input")
    elif request_kwargs.get("messages") is not None:
        source_value = request_kwargs.get("messages")

    blocks = _trace_module._trace_text_blocks(source_value)
    user_blocks = [
        block
        for block in blocks
        if block.get("role", "").lower() in {"user", "human"}
        and block.get("kind") == "user_request"
    ]
    if user_blocks:
        return str(user_blocks[-1].get("text") or "").strip()
    return ""


def _external_web_search_user_prompt_text(request_kwargs: Optional[dict]) -> str:
    metadata_text = _external_web_search_metadata_original_user_text(request_kwargs)
    if metadata_text:
        return _external_web_search_normalize_user_prompt_text(metadata_text)
    text = _external_web_search_latest_user_text(request_kwargs)
    if not text.strip():
        text = _external_web_search_request_text(request_kwargs)
    return _external_web_search_normalize_user_prompt_text(text)

async def _external_web_search_synthesize_or_fallback(
    *,
    request_kwargs: Optional[dict],
    search_results: str,
    queries: list[str],
    source_urls: list[str],
    original_function: Optional[Any],
) -> Any:
    if original_function is None:
        exception = _external_web_search_invalid_synthesis_exception(
            request_kwargs,
            reason="missing_original_function",
        )
        raise exception

    synthesis_kwargs = _external_web_search_synthesis_kwargs(
        request_kwargs,
        search_results,
    )
    _trace_module._route_trace(
        "external_web_search_bridge_synthesis_start",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        queries=queries,
    )
    try:
        synthesized = await _external_web_search_call_original(
            original_function,
            synthesis_kwargs,
            request_kwargs=request_kwargs,
            phase="synthesis",
        )
        reason = _external_web_search_synthesis_invalid_reason(synthesized)
        _trace_module._route_trace(
            "external_web_search_bridge_synthesis_done",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
            queries=queries,
            source_url_count=len(source_urls),
            invalid_reason=reason,
            response_preview=_trace_module._sanitize_trace_text(_image_generation_module._response_text(synthesized)),
        )
        if reason is None:
            return synthesized
        exception = _external_web_search_invalid_synthesis_exception(
            request_kwargs,
            reason=reason,
        )
        raise exception
    except Exception as exc:
        _trace_module._route_trace(
            "external_web_search_bridge_synthesis_error",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
            queries=queries,
            exception=_routing_module._trace_exception(exc),
        )
        _routing_module._mark_exception_for_deployment_failover(exc, request_kwargs)
        _external_web_search_set_recovery_request(exc, synthesis_kwargs)
        raise


def _external_web_search_continuation_kwargs(
    request_kwargs: Optional[dict],
    *,
    search_results: str,
    source_urls: Optional[list[str]] = None,
    queries: list[str],
    completed_actions: Optional[list[dict[str, str]]] = None,
    round_number: int,
    require_source_inspection: bool = False,
    source_inspection_retry: bool = False,
) -> dict[str, Any]:
    continuation_kwargs = _external_web_search_safe_request_base(request_kwargs)
    continuation_kwargs["tools"] = _external_web_search_continuation_tools(request_kwargs)
    continuation_evidence = _external_web_search_continuation_evidence(search_results)
    for key in (
        "allowed_tools",
        "custom_tools",
        "function_call",
        "functions",
        "mcp_servers",
        "tool_choice",
        "tool_resources",
        "use_chat_completions_api",
        "web_search_options",
    ):
        continuation_kwargs.pop(key, None)

    if require_source_inspection:
        continuation_kwargs = _external_web_search_low_reasoning_kwargs(
            continuation_kwargs,
            force_top_level=True,
        )
        continuation_kwargs["tool_choice"] = "required"
        continuation_kwargs["max_output_tokens"] = min(
            _EXTERNAL_WEB_SEARCH_CONTINUATION_OUTPUT_TOKENS,
            _image_generation_module._positive_int_value(
                continuation_kwargs.get("max_output_tokens")
            )
            or _EXTERNAL_WEB_SEARCH_CONTINUATION_OUTPUT_TOKENS,
        )
    else:
        continuation_kwargs = _external_web_search_low_reasoning_kwargs(
            continuation_kwargs
        )
        if (
            _image_generation_module._positive_int_value(
                continuation_kwargs.get("max_output_tokens")
            )
            is None
        ):
            continuation_kwargs["max_output_tokens"] = (
                _EXTERNAL_WEB_SEARCH_CONTINUATION_OUTPUT_TOKENS
            )

    metadata = _image_generation_module._request_metadata_dict(continuation_kwargs, "litellm_metadata") or {}
    continuation_metadata = metadata.copy()
    original_request = _external_web_search_user_prompt_text(request_kwargs)
    if original_request:
        continuation_metadata[_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY] = original_request
    continuation_metadata["external_web_search_continuation"] = True
    continuation_metadata["external_web_search_round"] = round_number
    continuation_metadata["external_web_search_completed_actions"] = copy.deepcopy(
        completed_actions or []
    )
    continuation_metadata["external_web_search_search_results"] = continuation_evidence
    continuation_kwargs["litellm_metadata"] = continuation_metadata

    if require_source_inspection:
        note = (
            "External web_search compatibility bridge source-inspection planning mode. "
            "You must call the provided web search function exactly now. Choose one "
            "listed source URL and call the function with url to read it, or with url "
            "plus pattern to find specific text inside that page. Do not answer the "
            "user. Do not call query while listed URLs are available. Do not emit "
            "tool-call markup as text."
        )
    else:
        note = (
            "External web_search compatibility bridge continuation mode. You have "
            "already observed the retrieved evidence below. Decide the next step from "
            "the available tools and evidence: call the provided web search bridge "
            "function if more focused lookup, source-page reading, or within-page text "
            "matching is needed, or provide the final answer. Do not emit tool-call "
            "markup as text."
        )
    if source_inspection_retry:
        note = f"{note} Your previous continuation did not read a source page."
    time_note = _responses_tools_module._current_time_context_instruction(request_kwargs)
    if time_note:
        note = f"{note} {time_note}"
    continuation_kwargs["instructions"] = note

    time_context_lines = ""
    if time_note:
        time_context_lines = f"Authoritative time context:\n{time_note}\n\n"
    query_lines = "\n".join(f"- {query}" for query in queries) or "- (none)"
    if require_source_inspection:
        planning_evidence = _external_web_search_source_planning_evidence(
            source_urls or [],
            search_results,
        )
        continuation_kwargs["input"] = (
            "Original user request:\n"
            f"{original_request or '(no user text extracted)'}\n\n"
            f"{time_context_lines}"
            "Web actions completed so far:\n"
            f"{query_lines}\n\n"
            "Candidate source URLs from search results:\n"
            f"{planning_evidence or '(no candidate URL extracted)'}\n\n"
            "Return a tool call now. Use url for the best source page, or url plus "
            "pattern for the most important entity/claim to check."
        )
    else:
        continuation_kwargs["input"] = (
            "Original user request:\n"
            f"{original_request or '(no user text extracted)'}\n\n"
            f"{time_context_lines}"
            "Web actions completed so far:\n"
            f"{query_lines}\n\n"
            "Retrieved evidence observed so far:\n"
            f"{continuation_evidence}\n\n"
            "Decide the next step now: call the web search bridge function for a "
            "focused follow-up lookup, source-page read, or within-page text match "
            "when needed; otherwise provide the final answer."
        )
    if source_inspection_retry:
        continuation_kwargs["input"] = (
            f"{continuation_kwargs['input']}\n\n"
            "The previous continuation was not sufficient because it did not perform "
            "source-page inspection. Return a tool call now; choose the source URL "
            "and optional pattern yourself."
        )
    continuation_kwargs.pop("messages", None)
    continuation_kwargs["stream"] = True
    return _responses_execution_module._normalize_external_web_search_router_kwargs(
        continuation_kwargs,
        request_kwargs,
    )


def _external_web_search_set_recovery_request(
    exception: Exception,
    request_kwargs: dict[str, Any],
) -> None:
    recovery_request = _external_web_search_safe_payload_copy(request_kwargs)
    try:
        exception.external_web_search_recovery_request = recovery_request  # type: ignore[attr-defined]
    except Exception:
        pass
    if len(_EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID) >= (
        _EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_MAX
    ):
        try:
            oldest_key = next(iter(_EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID))
            _EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID.pop(
                oldest_key,
                None,
            )
        except StopIteration:
            pass
    _EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID[id(exception)] = (
        _external_web_search_safe_payload_copy(recovery_request)
    )


def _external_web_search_recovery_request_from_exception(
    exception: Exception,
) -> Optional[dict[str, Any]]:
    request_kwargs = getattr(exception, "external_web_search_recovery_request", None)
    if isinstance(request_kwargs, dict):
        return _external_web_search_safe_payload_copy(request_kwargs)
    request_kwargs = _EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID.get(
        id(exception)
    )
    if isinstance(request_kwargs, dict):
        return _external_web_search_safe_payload_copy(request_kwargs)
    return None


def _external_web_search_set_pending_recovery_request(
    request_kwargs: Optional[dict],
    recovery_request: dict[str, Any],
) -> None:
    if not isinstance(request_kwargs, dict):
        return
    metadata = (
        _image_generation_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    updated_metadata = _external_web_search_safe_payload_copy(metadata)
    updated_metadata[_EXTERNAL_WEB_SEARCH_PENDING_RECOVERY_REQUEST_KEY] = (
        _external_web_search_safe_payload_copy(recovery_request)
    )
    request_kwargs["litellm_metadata"] = updated_metadata


def _external_web_search_pending_recovery_request(
    request_kwargs: Optional[dict],
) -> Optional[dict[str, Any]]:
    metadata = _external_web_search_metadata(request_kwargs)
    recovery_request = metadata.get(_EXTERNAL_WEB_SEARCH_PENDING_RECOVERY_REQUEST_KEY)
    if isinstance(recovery_request, dict):
        return _external_web_search_safe_payload_copy(recovery_request)
    return None


def _external_web_search_prepare_continuation_recovery_request(
    *,
    request_kwargs: Optional[dict],
    search_results: str,
    source_urls: list[str],
    queries: list[str],
    completed_actions: Optional[list[dict[str, str]]] = None,
    round_number: int,
    require_source_inspection: bool = False,
    source_inspection_retry: bool = False,
) -> dict[str, Any]:
    continuation_kwargs = _external_web_search_continuation_kwargs(
        request_kwargs,
        search_results=search_results,
        source_urls=source_urls,
        queries=queries,
        completed_actions=completed_actions,
        round_number=round_number,
        require_source_inspection=require_source_inspection,
        source_inspection_retry=source_inspection_retry,
    )
    _external_web_search_set_pending_recovery_request(
        request_kwargs,
        continuation_kwargs,
    )
    return continuation_kwargs


def _external_web_search_recovery_payload_phase(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    metadata = _external_web_search_metadata(request_kwargs)
    if metadata.get("external_web_search_continuation") is True:
        return "continuation"
    if metadata.get("external_web_search_synthesis") is True:
        return "synthesis"
    return None


async def _external_web_search_call_original(
    original_function: Any,
    call_kwargs: dict[str, Any],
    *,
    request_kwargs: Optional[dict] = None,
    phase: str = "continuation",
) -> Any:
    max_retries = _external_web_search_model_retry_count()
    delay_seconds = _external_web_search_model_retry_delay_seconds()
    attempt = 0
    while True:
        try:
            response = None
            if phase == "synthesis":
                response = await _external_web_search_chat_synthesis_response(
                    call_kwargs,
                    request_kwargs or call_kwargs,
                )
            else:
                response = await _external_web_search_chat_tool_response(
                    call_kwargs,
                    request_kwargs or call_kwargs,
                    phase=phase,
                )
            if response is None:
                response = original_function(
                    **_tools_module._with_external_web_search_post_call_suppressed(call_kwargs)
                )
                if inspect.isawaitable(response):
                    response = await response
            if _external_web_search_is_async_iterable(response):
                collected = await _external_web_search_collect_stream_response(
                    response,
                    call_kwargs,
                )
                _external_web_search_raise_if_invalid_model_response(
                    collected,
                    request_kwargs or call_kwargs,
                    phase=phase,
                )
                return collected
            _external_web_search_raise_if_invalid_model_response(
                response,
                request_kwargs or call_kwargs,
                phase=phase,
            )
            return response
        except Exception as exc:
            if (
                _external_web_search_origin_was_streaming(request_kwargs)
                and _routing_module._is_route_recovery_poll_error(exc)
            ):
                _routing_module._mark_exception_for_deployment_failover(exc, request_kwargs or call_kwargs)
                raise
            if (
                attempt >= max_retries
                or not _external_web_search_should_retry_model_exception(exc)
            ):
                raise
            attempt += 1
            _trace_module._route_trace(
                "external_web_search_bridge_model_retry",
                request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
                session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs or call_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(request_kwargs or call_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(request_kwargs or call_kwargs),
                phase=phase,
                retry_attempt=attempt,
                max_retries=max_retries,
                retry_delay_seconds=delay_seconds,
                exception=_routing_module._trace_exception(exc),
            )
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)


def _external_web_search_should_retry_model_exception(exception: Exception) -> bool:
    status_code = getattr(exception, "status_code", None)
    try:
        if int(status_code) == 429:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exception).lower()
    return "rate limit" in text or "too many requests" in text


def _external_web_search_is_async_iterable(response: Any) -> bool:
    return callable(getattr(response, "__aiter__", None))


async def _external_web_search_collect_stream_response(
    response: Any,
    call_kwargs: dict[str, Any],
) -> dict[str, Any]:
    from .streaming import _collect_responses_stream_completed_payload

    return await _collect_responses_stream_completed_payload(
        [],
        response,
        call_kwargs,
        stream_started_at=None,
        saw_visible_output=False,
    )


def _external_web_search_model_retry_count() -> int:
    value = os.getenv("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES", "").strip()
    if not value:
        return 2
    try:
        return max(0, min(5, int(value)))
    except ValueError:
        return 2


def _external_web_search_model_retry_delay_seconds() -> float:
    value = os.getenv("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS", "").strip()
    if not value:
        return 1.0
    try:
        return max(0.0, min(30.0, float(value)))
    except ValueError:
        return 1.0


async def _external_web_search_continue_or_synthesize(
    *,
    request_kwargs: Optional[dict],
    search_results: str,
    queries: list[str],
    completed_actions: Optional[list[dict[str, str]]] = None,
    source_urls: list[str],
    round_number: int,
    original_function: Optional[Any],
) -> Any:
    if original_function is None:
        return await _external_web_search_synthesize_or_fallback(
            request_kwargs=request_kwargs,
            search_results=search_results,
            queries=queries,
            source_urls=source_urls,
            original_function=original_function,
        )

    require_source_inspection = _external_web_search_source_read_required_for_continuation(
        request_kwargs,
        completed_actions,
        source_urls,
        search_results,
    )
    continuation_kwargs = _external_web_search_prepare_continuation_recovery_request(
        request_kwargs=request_kwargs,
        search_results=search_results,
        source_urls=source_urls,
        queries=queries,
        completed_actions=completed_actions,
        round_number=round_number,
        require_source_inspection=require_source_inspection,
    )
    _trace_module._route_trace(
        "external_web_search_bridge_continuation_start",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        round=round_number,
        queries=queries,
        evidence_chars=len(search_results or ""),
        continuation_evidence_chars=len(
            continuation_kwargs.get("litellm_metadata", {}).get(
                "external_web_search_search_results",
                "",
            )
        ),
        continuation_input_chars=len(str(continuation_kwargs.get("input") or "")),
        continuation_max_output_tokens=continuation_kwargs.get("max_output_tokens"),
    )
    try:
        continued = await _external_web_search_call_original(
            original_function,
            continuation_kwargs,
            request_kwargs=request_kwargs,
            phase="continuation",
        )
        if (
            require_source_inspection
            and _external_web_search_final_answer_without_tool_call(continued)
        ):
            _trace_module._route_trace(
                "external_web_search_bridge_source_inspection_retry",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                round=round_number,
                source_url_count=len(source_urls),
                response_preview=_trace_module._sanitize_trace_text(
                    _image_generation_module._response_text(continued)
                ),
            )
            retry_kwargs = _external_web_search_prepare_continuation_recovery_request(
                request_kwargs=request_kwargs,
                search_results=search_results,
                source_urls=source_urls,
                queries=queries,
                completed_actions=completed_actions,
                round_number=round_number,
                require_source_inspection=True,
                source_inspection_retry=True,
            )
            continued = await _external_web_search_call_original(
                original_function,
                retry_kwargs,
                request_kwargs=request_kwargs,
                phase="continuation",
            )
        elif (
            require_source_inspection
            and _external_web_search_response_has_search_only_actions(
                continued,
                request_kwargs,
            )
        ):
            _trace_module._route_trace(
                "external_web_search_bridge_source_inspection_query_only_retry",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                round=round_number,
                source_url_count=len(source_urls),
                next_actions=_litellm_web_search_actions_for_request(
                    continued,
                    request_kwargs,
                ),
            )
            retry_kwargs = _external_web_search_prepare_continuation_recovery_request(
                request_kwargs=request_kwargs,
                search_results=search_results,
                source_urls=source_urls,
                queries=queries,
                completed_actions=completed_actions,
                round_number=round_number,
                require_source_inspection=True,
                source_inspection_retry=True,
            )
            continued = await _external_web_search_call_original(
                original_function,
                retry_kwargs,
                request_kwargs=request_kwargs,
                phase="continuation",
            )
        if _external_web_search_is_empty_continuation_response(continued):
            _trace_module._route_trace(
                "external_web_search_bridge_empty_continuation_synthesis",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                round=round_number,
                queries=queries,
            )
            return await _external_web_search_synthesize_or_fallback(
                request_kwargs=request_kwargs,
                search_results=search_results,
                queries=queries,
                source_urls=source_urls,
                original_function=original_function,
            )
        _trace_module._route_trace(
            "external_web_search_bridge_continuation_done",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
            round=round_number,
            queries=queries,
            response_preview=_trace_module._sanitize_trace_text(_image_generation_module._response_text(continued)),
            next_queries=_litellm_web_search_queries_for_request(continued, request_kwargs),
            next_actions=_litellm_web_search_actions_for_request(continued, request_kwargs),
        )
        return continued
    except Exception as exc:
        should_raise_with_recovery = False
        if (
            _routing_module._is_route_recovery_poll_error(exc)
            and _external_web_search_invalid_response_phase(exc) != "continuation"
        ):
            should_raise_with_recovery = True
        if _external_web_search_is_timeout_exception(exc):
            should_raise_with_recovery = True
        if should_raise_with_recovery:
            _routing_module._mark_exception_for_deployment_failover(exc, request_kwargs)
            _external_web_search_set_recovery_request(exc, continuation_kwargs)
        recovery_request = _external_web_search_recovery_request_from_exception(exc)
        _trace_module._route_trace(
            "external_web_search_bridge_continuation_error",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
            round=round_number,
            queries=queries,
            exception=_routing_module._trace_exception(exc),
            recovery_payload_phase=_external_web_search_recovery_payload_phase(
                recovery_request
            ),
            recovery_payload_stream=(
                recovery_request.get("stream")
                if isinstance(recovery_request, dict)
                else None
            ),
        )
        if should_raise_with_recovery:
            raise
        return await _external_web_search_synthesize_or_fallback(
            request_kwargs=request_kwargs,
            search_results=search_results,
            queries=queries,
            source_urls=source_urls,
            original_function=original_function,
        )


async def _external_web_search_finalize_response(
    response: Any,
    *,
    request_kwargs: Optional[dict],
    search_results: str,
    queries: list[str],
    source_urls: list[str],
    original_function: Optional[Any],
) -> Any:
    reason = _external_web_search_synthesis_invalid_reason(response)
    if reason is None:
        return response
    _trace_module._route_trace(
        "external_web_search_bridge_final_invalid",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        queries=queries,
        invalid_reason=reason,
        response_preview=_trace_module._sanitize_trace_text(_image_generation_module._response_text(response)),
    )
    return await _external_web_search_synthesize_or_fallback(
        request_kwargs=request_kwargs,
        search_results=search_results,
        queries=queries,
        source_urls=source_urls,
        original_function=original_function,
    )


async def _resolve_litellm_web_search_function_calls(
    response: Any,
    request_kwargs: Optional[dict],
    original_function: Optional[Any] = None,
) -> Any:
    initial_actions = _litellm_web_search_actions_for_request(response, request_kwargs)
    if not initial_actions:
        _external_web_search_raise_if_invalid_initial_no_action_response(
            response,
            request_kwargs,
        )
        return response

    max_rounds = _external_web_search_max_rounds()
    current_response = response
    completed_actions: list[dict[str, str]] = _external_web_search_completed_actions_metadata(request_kwargs)
    existing_search_results = _external_web_search_search_results_metadata(request_kwargs)
    search_sections: list[str] = [existing_search_results] if existing_search_results.strip() else []
    source_urls: list[str] = []
    source_urls_by_action: list[list[str]] = []
    page_cache: dict[str, str] = {}
    page_fetch_tasks: dict[str, asyncio.Task[str]] = {}
    final_response: Any = response
    forced_synthesis = False

    for round_number in range(1, max_rounds + 1):
        round_actions = _external_web_search_budgeted_actions(
            _litellm_web_search_actions_for_request(current_response, request_kwargs),
            completed_actions,
        )
        if not round_actions:
            final_response = current_response
            break

        message, round_source_urls, round_source_urls_by_action, round_completed_actions = await _external_web_search_run_actions(
            round_actions,
            page_cache,
            page_fetch_tasks,
            request_kwargs,
        )
        _trace_module._route_trace(
            "external_web_search_bridge_actions_executed",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
            round=round_number,
            actions=_external_web_search_trace_actions(round_completed_actions),
            source_url_count=len(round_source_urls),
            evidence_chars=len(message or ""),
        )
        search_sections.append(message)
        completed_actions.extend(round_completed_actions)
        source_urls_by_action.extend(round_source_urls_by_action)
        for url in round_source_urls:
            if url not in source_urls:
                source_urls.append(url)

        search_results = "\n\n".join(section for section in search_sections if section.strip())
        completed_labels = _external_web_search_action_labels(completed_actions)

        if round_number >= max_rounds:
            final_response = await _external_web_search_synthesize_or_fallback(
                request_kwargs=request_kwargs,
                search_results=search_results,
                queries=completed_labels,
                source_urls=source_urls,
                original_function=original_function,
            )
            forced_synthesis = True
            break

        if _external_web_search_search_failed_without_sources(
            search_results,
            source_urls,
            completed_actions,
        ):
            raise _external_web_search_search_failed_without_sources_exception(
                request_kwargs,
                search_results=search_results,
                queries=completed_labels,
                completed_actions=completed_actions,
                round_number=round_number,
            )

        auto_source_actions = _external_web_search_auto_source_inspection_actions(
            request_kwargs,
            completed_actions=completed_actions,
            source_urls=source_urls,
            search_results=search_results,
        )
        if auto_source_actions:
            (
                auto_message,
                auto_source_urls,
                auto_source_urls_by_action,
                auto_completed_actions,
            ) = await _external_web_search_run_actions(
                auto_source_actions,
                page_cache,
                page_fetch_tasks,
                request_kwargs,
            )
            _trace_module._route_trace(
                "external_web_search_bridge_auto_source_actions_executed",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                round=round_number,
                actions=_external_web_search_trace_actions(auto_completed_actions),
                source_url_count=len(auto_source_urls),
                evidence_chars=len(auto_message or ""),
            )
            search_sections.append(auto_message)
            completed_actions.extend(auto_completed_actions)
            source_urls_by_action.extend(auto_source_urls_by_action)
            for url in auto_source_urls:
                if url not in source_urls:
                    source_urls.append(url)
            search_results = "\n\n".join(
                section for section in search_sections if section.strip()
            )
            final_response = await _external_web_search_synthesize_or_fallback(
                request_kwargs=request_kwargs,
                search_results=search_results,
                queries=_external_web_search_action_labels(completed_actions),
                source_urls=source_urls,
                original_function=original_function,
            )
            forced_synthesis = True
            break

        if (
            _external_web_search_request_needs_source_inspection(request_kwargs)
            and _external_web_search_has_source_page_action(completed_actions)
            and any(action.get("type") == "search" for action in completed_actions)
        ):
            final_response = await _external_web_search_synthesize_or_fallback(
                request_kwargs=request_kwargs,
                search_results=search_results,
                queries=completed_labels,
                source_urls=source_urls,
                original_function=original_function,
            )
            forced_synthesis = True
            break

        current_response = await _external_web_search_continue_or_synthesize(
            request_kwargs=request_kwargs,
            search_results=search_results,
            queries=completed_labels,
            completed_actions=completed_actions,
            source_urls=source_urls,
            round_number=round_number,
            original_function=original_function,
        )
        final_response = current_response

        if _external_web_search_response_has_search_only_actions(
            current_response,
            request_kwargs,
        ):
            auto_source_actions = _external_web_search_auto_source_inspection_actions(
                request_kwargs,
                completed_actions=completed_actions,
                source_urls=source_urls,
                search_results=search_results,
            )
            if auto_source_actions:
                (
                    auto_message,
                    auto_source_urls,
                    auto_source_urls_by_action,
                    auto_completed_actions,
                ) = await _external_web_search_run_actions(
                    auto_source_actions,
                    page_cache,
                    page_fetch_tasks,
                    request_kwargs,
                )
                _trace_module._route_trace(
                    "external_web_search_bridge_auto_source_actions_executed",
                    request_id=_routing_module._trace_request_id(request_kwargs),
                    session=_routing_module._trace_session_context(request_kwargs),
                    model_group=_responses_execution_module._request_model_group(request_kwargs),
                    deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                    route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                    round=round_number,
                    actions=_external_web_search_trace_actions(auto_completed_actions),
                    source_url_count=len(auto_source_urls),
                    evidence_chars=len(auto_message or ""),
                )
                search_sections.append(auto_message)
                completed_actions.extend(auto_completed_actions)
                source_urls_by_action.extend(auto_source_urls_by_action)
                for url in auto_source_urls:
                    if url not in source_urls:
                        source_urls.append(url)
                search_results = "\n\n".join(
                    section for section in search_sections if section.strip()
                )
                final_response = await _external_web_search_synthesize_or_fallback(
                    request_kwargs=request_kwargs,
                    search_results=search_results,
                    queries=_external_web_search_action_labels(completed_actions),
                    source_urls=source_urls,
                    original_function=original_function,
                )
                forced_synthesis = True
                break

    if not forced_synthesis:
        search_results = "\n\n".join(section for section in search_sections if section.strip())
        final_response = await _external_web_search_finalize_response(
            final_response,
            request_kwargs=request_kwargs,
            search_results=search_results,
            queries=_external_web_search_action_labels(completed_actions),
            source_urls=source_urls,
            original_function=original_function,
        )

    return _with_external_web_search_call_action_items(
        final_response,
        completed_actions,
        source_urls_by_action,
    )


def _external_web_search_synthesis_kwargs(
    request_kwargs: Optional[dict],
    search_results: str,
) -> dict[str, Any]:
    synthesis_evidence = _external_web_search_synthesis_evidence(search_results)
    synthesis_kwargs = _external_web_search_low_reasoning_kwargs(
        request_kwargs,
        force_top_level=True,
    )
    for key in (
        "allowed_tools",
        "custom_tools",
        "function_call",
        "functions",
        "mcp_servers",
        "parallel_tool_calls",
        "tool_choice",
        "tool_resources",
        "tools",
        "use_chat_completions_api",
        "web_search_options",
    ):
        synthesis_kwargs.pop(key, None)

    metadata = _image_generation_module._request_metadata_dict(synthesis_kwargs, "litellm_metadata") or {}
    synthesis_metadata = metadata.copy()
    original_request = _external_web_search_user_prompt_text(request_kwargs)
    if original_request:
        synthesis_metadata[_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY] = original_request
    synthesis_metadata.pop(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY, None)
    synthesis_metadata.pop(_WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY, None)
    synthesis_metadata.pop(_RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY, None)
    synthesis_metadata["external_web_search_synthesis"] = True
    synthesis_metadata["external_web_search_search_results"] = synthesis_evidence
    synthesis_metadata[_RESPONSES_CHAT_BRIDGE_METADATA_KEY] = True
    synthesis_kwargs["litellm_metadata"] = synthesis_metadata

    note = (
        "External web_search compatibility bridge synthesis mode. The search "
        "step is already complete. Do not call tools, do not emit tool-call "
        "markup, do not mention mcp tool names, and do not say that you will "
        "search. Write only the final answer to the user. Use only the provided "
        "retrieved evidence, cite source URLs from that evidence, and say when "
        "the evidence is insufficient. The local DDGS/Jina bridge may differ "
        "from hosted OpenAI web_search ranking and snippets."
    )
    time_note = _responses_tools_module._current_time_context_instruction(request_kwargs)
    if time_note:
        note = f"{note} {time_note}"
    existing = synthesis_kwargs.get("instructions")
    if isinstance(existing, str) and existing.strip():
        synthesis_kwargs["instructions"] = f"{existing.rstrip()}\n\n{note}"
    else:
        synthesis_kwargs["instructions"] = note
    time_context_lines = ""
    if time_note:
        time_context_lines = f"Authoritative time context:\n{time_note}\n\n"
    synthesis_input = (
        "Original user request. Any instruction to call or use web_search has "
        "already been satisfied by the compatibility bridge:\n"
        f"{original_request or '(no user text extracted)'}\n\n"
        f"{time_context_lines}"
        "Retrieved evidence:\n"
        f"{synthesis_evidence}\n\n"
        "Now answer the original user request directly. Do not call tools."
    )
    synthesis_kwargs["input"] = synthesis_input
    synthesis_kwargs.pop("messages", None)
    requested_output_tokens = _image_generation_module._positive_int_value(
        synthesis_kwargs.get("max_output_tokens")
    )
    synthesis_kwargs["max_output_tokens"] = max(
        requested_output_tokens or 0,
        _EXTERNAL_WEB_SEARCH_SYNTHESIS_OUTPUT_TOKENS,
    )
    synthesis_kwargs["stream"] = False
    return _responses_execution_module._normalize_external_web_search_router_kwargs(
        synthesis_kwargs,
        request_kwargs,
    )


def _external_web_search_invalid_synthesis_exception(
    request_kwargs: Optional[dict],
    *,
    reason: str,
    phase: str = "synthesis",
) -> Exception:
    model_group = _responses_execution_module._request_model_group(request_kwargs) or ""
    message = (
        f"LiteLLM Menu external web_search {phase} returned an invalid "
        f"response for {model_group or 'the route'}: {reason}"
    )
    exception = RuntimeError(message)
    try:
        exception.status_code = 503  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        exception.body = {  # type: ignore[attr-defined]
            "reason": "external_web_search_synthesis_invalid",
            "invalid_reason": reason,
            "phase": phase,
        }
    except Exception:
        pass
    try:
        exception.external_web_search_synthesis_invalid = True  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        exception.external_web_search_invalid_phase = phase  # type: ignore[attr-defined]
    except Exception:
        pass
    _routing_module._mark_exception_for_deployment_failover(exception, request_kwargs)
    return exception


def _external_web_search_invalid_response_phase(exception: Exception) -> Optional[str]:
    phase = getattr(exception, "external_web_search_invalid_phase", None)
    return phase if isinstance(phase, str) else None


def _external_web_search_final_answer_failure_text(
    request_kwargs: Optional[dict],
    exception: Exception,
) -> str:
    return _routing_module._sanitized_upstream_route_failure_message(
        _responses_execution_module._request_model_group(request_kwargs),
        exception,
        request_kwargs,
    )


def _external_web_search_original_model_group(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, metadata_key) or {}
        for model_key in (
            _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
            "original_model_group",
            "model_group",
        ):
            model_group = metadata.get(model_key)
            if isinstance(model_group, str) and model_group.strip():
                    return model_group
    return None


def _external_web_search_recovery_model_group(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    model_group = _responses_execution_module._request_selected_deployment_model_group(
        request_kwargs
    )
    if isinstance(model_group, str) and model_group.strip():
        return model_group
    model_group = _external_web_search_original_model_group(request_kwargs)
    if isinstance(model_group, str) and model_group.strip():
        return model_group
    model_group = _responses_execution_module._request_model_group(request_kwargs)
    if isinstance(model_group, str) and model_group.strip():
        return model_group
    return _responses_execution_module._external_web_search_router_model_group(
        request_kwargs,
    )


def _external_web_search_recovery_kwargs(
    request_kwargs: Optional[dict],
    search_results: str = "",
    exception: Optional[Exception] = None,
) -> dict[str, Any]:
    recovery_kwargs: dict[str, Any]
    if exception is not None:
        recovery_request = _external_web_search_recovery_request_from_exception(exception)
        if recovery_request is not None:
            model_group = _external_web_search_recovery_model_group(
                recovery_request,
            ) or _external_web_search_recovery_model_group(request_kwargs)
            if isinstance(model_group, str) and model_group.strip():
                recovery_request["model"] = model_group
            recovery_request["stream"] = True
            recovery_kwargs = recovery_request
            return recovery_kwargs

    recovery_request = _external_web_search_pending_recovery_request(request_kwargs)
    if recovery_request is not None:
        model_group = _external_web_search_recovery_model_group(
            recovery_request,
        ) or _external_web_search_recovery_model_group(request_kwargs)
        if isinstance(model_group, str) and model_group.strip():
            recovery_request["model"] = model_group
        recovery_request["stream"] = True
        return recovery_request

    if _external_web_search_payload_has_embedded_evidence(request_kwargs):
        recovery_kwargs = copy.deepcopy(request_kwargs or {})
        model_group = _external_web_search_recovery_model_group(recovery_kwargs)
        if isinstance(model_group, str) and model_group.strip():
            recovery_kwargs["model"] = model_group
        recovery_kwargs["stream"] = True
        return recovery_kwargs

    recovery_kwargs = _external_web_search_synthesis_kwargs(
        request_kwargs,
        search_results,
    )
    model_group = _external_web_search_recovery_model_group(recovery_kwargs)
    if isinstance(model_group, str) and model_group.strip():
        recovery_kwargs["model"] = model_group
    recovery_kwargs["stream"] = True
    return recovery_kwargs


def _external_web_search_model_response_invalid_reason(
    response: Any,
    *,
    phase: str,
) -> Optional[str]:
    if phase == "continuation":
        if (
            not _litellm_web_search_function_calls(response)
            and _external_web_search_has_completed_assistant_message(response)
        ):
            return None
        text = _image_generation_module._response_text(response)
        if text.strip():
            progress_reason = _external_web_search_progress_preamble_reason(text)
            if progress_reason is not None:
                return progress_reason
            return None
        if _litellm_web_search_function_calls(response):
            return None
        return "empty_continuation"
    return _external_web_search_synthesis_invalid_reason(response)


def _external_web_search_is_empty_continuation_response(response: Any) -> bool:
    return (
        _external_web_search_model_response_invalid_reason(
            response,
            phase="continuation",
        )
        == "empty_continuation"
    )


def _external_web_search_raise_if_invalid_model_response(
    response: Any,
    request_kwargs: Optional[dict],
    *,
    phase: str,
) -> None:
    reason = _external_web_search_model_response_invalid_reason(response, phase=phase)
    if reason is None:
        return
    if phase == "continuation" and reason == "empty_continuation":
        return
    exception = _external_web_search_invalid_synthesis_exception(
        request_kwargs,
        reason=reason,
    )
    try:
        exception.external_web_search_invalid_phase = phase  # type: ignore[attr-defined]
    except Exception:
        pass
    raise exception


def _external_web_search_origin_was_streaming(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    if request_kwargs.get("stream") is True:
        return True
    metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    return metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY) is True


def _external_web_search_is_timeout_exception(exception: Exception) -> bool:
    if isinstance(exception, (TimeoutError, asyncio.TimeoutError)):
        return True
    raw_status_code = getattr(exception, "status_code", None)
    if raw_status_code in (408, 504) or str(raw_status_code).strip() in {"408", "504"}:
        return True
    status_code = _routing_module._exception_status_code(exception)
    if status_code in (408, 504):
        return True
    body = getattr(exception, "body", None)
    if isinstance(body, dict) and body.get("reason") == "stream_idle_timeout":
        return True
    exception_class = type(exception).__name__.lower()
    text = _routing_module._exception_text(exception)
    if "timeout" in exception_class or "timeouterror" in exception_class:
        return True
    if any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "time out",
            "deadline exceeded",
            "deadline_exceeded",
            "upstream request timeout",
            "所有渠道",
            "均失败",
            "超时",
        )
    ):
        return True
    return False
