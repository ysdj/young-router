from __future__ import annotations

from . import pi_web_access as _pi_web_access_module
from . import responses_output as _responses_output_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_tools as _responses_tools_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module
from .pi_web_access import (
    _external_web_search_float_env,
    _external_web_search_int_env,
)


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
    _CURRENT_SELECTED_DEPLOYMENT_BOX,
    _CURRENT_UPSTREAM_URL_SURFACE_KEY,
    _JSONStreamEvent,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _PI_WEB_ACCESS_TOOL_NAMES,
    _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
    _WEB_SEARCH_NATIVE_EVENT_SEEN_METADATA_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
    _WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY,
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


_WEB_SEARCH_FUNCTION_CALL_ITEM_TYPES = {"function_call", "custom_tool_call", "tool_call"}


def _function_call_name(call: Any) -> Optional[str]:
    if not isinstance(call, dict):
        return None
    name = call.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _is_web_search_function_call_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return False
    if (
        item_type in _WEB_SEARCH_FUNCTION_CALL_ITEM_TYPES
        and _function_call_name(item)
        in _PI_WEB_ACCESS_TOOL_NAMES
    ):
        return True
    function = item.get("function")
    return (
        item_type in _WEB_SEARCH_FUNCTION_CALL_ITEM_TYPES
        and isinstance(function, dict)
        and function.get("name")
        in _PI_WEB_ACCESS_TOOL_NAMES
    )


def _web_search_function_calls(response: Any) -> list[dict[str, Any]]:
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
        if _is_web_search_function_call_item(item):
            append_call(item)
            return
        function = item.get("function")
        if (
            item.get("type") == "function"
            and isinstance(function, dict)
            and function.get("name")
            in _PI_WEB_ACCESS_TOOL_NAMES
        ):
            append_call(item)
            return
        for value in item.values():
            if isinstance(value, (dict, list)):
                visit(value, depth + 1)

    visit(payload)
    return calls


def _web_search_arguments_from_call(call: dict[str, Any]) -> Any:
    arguments = call.get("arguments")
    function = call.get("function")
    if arguments is None and isinstance(function, dict):
        arguments = function.get("arguments")
    if arguments is None:
        arguments = call.get("input")
    return _parse_tool_search_arguments(arguments)


def _web_search_query_from_call(call: dict[str, Any]) -> Optional[str]:
    parsed = _web_search_arguments_from_call(call)
    if isinstance(parsed, dict):
        query = parsed.get("query")
        if isinstance(query, str) and query.strip():
            return query.strip()
    return None


def _external_web_search_clean_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip().rstrip(".,;:]}")
    # Provider text may append a Markdown citation immediately after the URL,
    # with or without a sentence-ending period before the marker, e.g.
    # ``https://example.test/page[[1]](https://example.test/page)``.
    # Keep only the actual URL so source lists never expose citation markup.
    citation_marker = re.search(r"\[\[\d+\]\]\(https?://", url)
    if citation_marker:
        url = url[: citation_marker.start()].rstrip(".,;:]}")
    # Preserve balanced parentheses in real URL paths, but remove a bare
    # closing parenthesis that came from surrounding Markdown punctuation.
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,;:]}")
    if not url.startswith(("http://", "https://")):
        return None
    return url


def _external_web_search_page_number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        page = int(value.strip())
        return page if page >= 1 else None
    return None


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

    # Some Chat-compatible providers describe a page lookup as a search query,
    # for example ``page: int in https://example.test/README.md``.  The URL at
    # the end makes this an unambiguous request to find text on the selected
    # page, rather than a new web search.  Preserve the model's chosen page and
    # pattern while translating its malformed function arguments.
    implicit_find_match = re.match(
        r"(?is)^(?:find\s+)?(.+?)\s+(?:in|on)\s+(https?://\S+)\s*$",
        stripped,
    )
    if implicit_find_match:
        pattern = implicit_find_match.group(1).strip()
        url = _external_web_search_clean_url(implicit_find_match.group(2))
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


def _web_search_action_from_call(call: dict[str, Any]) -> Optional[dict[str, str]]:
    tool_name = _function_call_name(call)
    parsed = _web_search_arguments_from_call(call)
    if isinstance(parsed, str) and parsed.strip():
        if _external_web_search_looks_like_tool_json_fragment(parsed):
            return None
        action = _external_web_search_action_from_text(parsed)
        return action or {"type": "search", "query": parsed.strip()}
    if not isinstance(parsed, dict):
        return None

    # Direct pi-web-access calls use the extension's native tool names.  Map
    # them to the existing bounded bridge actions without changing the model's
    # tool contract.
    if tool_name == "fetch_content":
        url = _external_web_search_clean_url(
            parsed.get("url") or parsed.get("href") or parsed.get("page_url")
        )
        if url is None and isinstance(parsed.get("urls"), list):
            for candidate in parsed["urls"]:
                url = _external_web_search_clean_url(candidate)
                if url:
                    break
        return {"type": "openPage", "url": url} if url else None

    if tool_name == "web_search":
        queries = parsed.get("queries")
        if isinstance(queries, list):
            for candidate in queries:
                if isinstance(candidate, str) and candidate.strip():
                    parsed = {**parsed, "query": candidate.strip()}
                    break

    action_type = _external_web_search_action_name(
        parsed.get("action") or parsed.get("type") or parsed.get("operation")
    )
    query = parsed.get("query") or parsed.get("q")
    page = parsed.get("page")
    if isinstance(query, str) and query.strip():
        action = _external_web_search_action_from_text(query)
        if action is not None:
            return action
    url = _external_web_search_clean_url(
        parsed.get("url") or parsed.get("href") or parsed.get("page_url")
    )
    pattern = parsed.get("pattern") or parsed.get("text") or parsed.get("needle")

    # Models occasionally put a result URL in ``query`` despite the tool
    # schema exposing ``url``. A bare URL is unambiguously a page action, not
    # a useful search query; normalize it so the model's choice opens the page.
    if url is None and isinstance(query, str):
        query_url = _external_web_search_clean_url(query)
        if query_url:
            url = query_url
            query = None

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
        action = {"type": "search", "query": query.strip()}
        if page is not None:
            action["page"] = str(page)
        return action
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
        if "page" in clean:
            page = _external_web_search_page_number(clean["page"])
            if page is None:
                return None
            clean["page"] = str(page)
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
    page = _external_web_search_page_number(action.get("page")) or 1
    return f"search:{action.get('query', '').strip().lower()}:page:{page}"


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
    query = action.get("query", "")
    page = _external_web_search_page_number(action.get("page")) or 1
    return f"{query} (page {page})" if query and page > 1 else query


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
        for key in ("query", "url", "pattern", "page"):
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
        action["sources"] = [{"type": "url", "url": url} for url in clean_urls[:20]]


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
    page = _external_web_search_page_number(
        lookup_action.get("page") or action.get("page")
    )

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
        label_action = {"type": "search", "query": unique_queries[0]}
        if page and page > 1:
            label_action["page"] = str(page)
        label = _external_web_search_action_label(label_action)
        if _external_web_search_looks_like_tool_json_fragment(label):
            return None
        clean_action["type"] = "search"
        clean_action["query"] = unique_queries[0]
        if page and page > 1:
            clean_action["page"] = str(page)
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
    *_PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
}


def _provider_native_web_search_event_value(value: Any, depth: int = 0) -> bool:
    """Recognize raw upstream search lifecycle evidence.

    This deliberately runs before the normal sanitizer. A normalized
    ``web_search_call`` is still useful to the client, but it must not be
    mistaken for a pi-web-access function call during post-processing.
    """

    if value is None or depth > 8:
        return False
    if isinstance(value, list):
        return any(_provider_native_web_search_event_value(child, depth + 1) for child in value)
    if not isinstance(value, dict):
        return False
    value_type = value.get("type")
    if isinstance(value_type, str):
        normalized_type = value_type.strip().lower()
        if normalized_type in _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES:
            return True
        if normalized_type == "reasoning":
            item_id = value.get("id")
            if isinstance(item_id, str) and item_id.startswith("tco_"):
                return True
    for child in value.values():
        if isinstance(child, (dict, list)) and _provider_native_web_search_event_value(child, depth + 1):
            return True
    return False


def _request_has_provider_native_web_search_event(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key) or {}
        if metadata.get(_WEB_SEARCH_NATIVE_EVENT_SEEN_METADATA_KEY) is True:
            return True
    return False


def _mark_provider_native_web_search_event(
    request_kwargs: Optional[dict],
    value: Any,
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    raw_native = _provider_native_web_search_event_value(value)
    if not raw_native:
        dumped = _streaming_module._stream_chunk_dump(value)
        chunk_type = dumped.get("type") if isinstance(dumped, dict) else None
        native_tool_declared = any(
            isinstance(tool, dict)
            and tool.get("type") in _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES
            for tool in (request_kwargs.get("tools") or [])
        )
        model_info = _request_context_module._request_model_info(request_kwargs)
        provider = model_info.get("provider") or request_kwargs.get("custom_llm_provider")
        api_base = request_kwargs.get("api_base")
        openrouter_route = (
            isinstance(provider, str)
            and provider.strip().lower() == "openrouter"
        )
        if isinstance(api_base, str):
            openrouter_route = openrouter_route or "openrouter.ai" in api_base.lower()
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs, "litellm_metadata"
        ) or {}
        local_bridge_active = bool(
            metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True
            or metadata.get(_RESPONSES_CHAT_BRIDGE_METADATA_KEY) is True
        )
        raw_native = (
            (native_tool_declared or (openrouter_route and not local_bridge_active))
            and isinstance(chunk_type, str)
            and (
                chunk_type.startswith("response.web_search_call.")
                or (
                    chunk_type in {
                        "response.output_item.added",
                        "response.output_item.done",
                    }
                    and isinstance(dumped.get("item"), dict)
                    and dumped["item"].get("type") == "web_search_call"
                )
            )
        )
    if not raw_native:
        return False
    if _request_has_provider_native_web_search_event(request_kwargs):
        return True
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    updated_metadata = metadata.copy()
    updated_metadata[_WEB_SEARCH_NATIVE_EVENT_SEEN_METADATA_KEY] = True
    request_kwargs["litellm_metadata"] = updated_metadata
    return True

_PROVIDER_HIDDEN_WEB_SEARCH_REASONING_ID_PREFIX = "tco_"
_PROVIDER_HIDDEN_WEB_SEARCH_DISPLAY_QUERY = "Web search"

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


def _provider_hidden_web_search_reasoning_item(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return False
    return _provider_hidden_web_search_reasoning_item_id(item.get("id"))


def _provider_hidden_web_search_reasoning_item_id(item_id: Any) -> bool:
    return (
        isinstance(item_id, str)
        and item_id.startswith(_PROVIDER_HIDDEN_WEB_SEARCH_REASONING_ID_PREFIX)
    )


def _provider_hidden_web_search_primary_reasoning_item(item: Any) -> bool:
    if not _provider_hidden_web_search_reasoning_item(item):
        return False
    item_id = str(item.get("id") or "")
    index_match = re.search(r"-(\d+)$", item_id)
    return index_match is None or index_match.group(1) == "0"


def _is_provider_hidden_web_search_call_item(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("type") != "web_search_call":
        return False
    item_id = item.get("id")
    return isinstance(item_id, str) and item_id.startswith(
        f"ws_{_PROVIDER_HIDDEN_WEB_SEARCH_REASONING_ID_PREFIX}"
    )


def _provider_hidden_web_search_query(request_kwargs: Optional[dict]) -> str:
    # The provider's encrypted ``tco_*`` item proves that native search ran,
    # but does not reveal its internal query planning.  Do not present the
    # user's whole prompt (or a guessed rewrite) as if it were the real query.
    return _PROVIDER_HIDDEN_WEB_SEARCH_DISPLAY_QUERY


def _provider_hidden_web_search_call_item(
    item: Any,
    request_kwargs: Optional[dict],
    source_urls: Optional[list[str]] = None,
    *,
    status: str = "completed",
) -> Optional[dict[str, Any]]:
    if (
        not _tools_module._request_has_web_search_tool(request_kwargs)
        or not _provider_hidden_web_search_primary_reasoning_item(item)
    ):
        return None
    item_id = item.get("id")
    search_item = _external_web_search_call_item_for_action(
        {
            "type": "search",
            "query": _provider_hidden_web_search_query(request_kwargs),
        },
        source_urls,
    )
    if search_item is None:
        return None
    search_item["id"] = f"ws_{item_id}"
    search_item["status"] = status
    return search_item


def _final_answer_message_item(item: Any) -> Any:
    if (
        not isinstance(item, dict)
        or item.get("type") != "message"
        or item.get("role") != "assistant"
        or item.get("phase") is not None
    ):
        return item
    clean_item = copy.deepcopy(item)
    clean_item["phase"] = "final_answer"
    return clean_item


class _ProviderHiddenWebSearchStreamAdapter:
    """Expose provider-private ``tco_*`` search reasoning as Responses events.

    Some OpenAI-compatible providers execute web search but encode the call as a
    private reasoning item instead of emitting ``web_search_call`` events.  Keep
    the conversion deliberately narrow: it is enabled only when the client sent
    a native ``web_search`` tool and only recognizes the provider's stable
    ``tco_`` item id prefix.  Private reasoning text is never forwarded.
    """

    def __init__(self, request_kwargs: Optional[dict]) -> None:
        self.request_kwargs = request_kwargs or {}
        self.enabled = _tools_module._request_has_web_search_tool(self.request_kwargs)
        self._seen_reasoning_ids: set[str] = set()
        self._search_items: dict[str, dict[str, Any]] = {}
        self._output_indexes: dict[str, int] = {}
        self._source_urls: list[str] = []
        self._next_output_index = 0
        self._sequence_number = 0
        self._completed_item_ids: set[str] = set()
        self._started_web_search_ids: set[str] = set()
        self._started_message_ids: set[str] = set()
        self._started_content_parts: set[tuple[str, int]] = set()

    @property
    def active(self) -> bool:
        return bool(self._search_items)

    def _encode(self, event: dict[str, Any]) -> Any:
        return _streaming_module._json_stream_event(event)

    def _remember_urls(self, value: Any) -> None:
        for url in _provider_hosted_web_search_source_urls(value):
            if url not in self._source_urls:
                self._source_urls.append(url)

    def _observe_sequence_number(self, value: Any) -> None:
        if isinstance(value, int) and value > self._sequence_number:
            self._sequence_number = value

    def _next_sequence_number(self) -> int:
        self._sequence_number += 1
        return self._sequence_number

    def _forward_event(
        self,
        chunk: Any,
        payload: Optional[dict[str, Any]] = None,
    ) -> Any:
        changed = payload is not None
        if payload is None:
            payload = _streaming_module._stream_chunk_dump(chunk)
        if not isinstance(payload, dict) or not payload:
            return chunk
        sequence_number = payload.get("sequence_number")
        if isinstance(sequence_number, int) and sequence_number < self._sequence_number:
            payload = copy.deepcopy(payload)
            payload["sequence_number"] = self._next_sequence_number()
            changed = True
        if not changed:
            return chunk
        if isinstance(chunk, _JSONStreamEvent):
            return _streaming_module._json_stream_event(payload)
        return payload

    def _item_events(self, item_id: str, output_index: int) -> list[Any]:
        item = self._search_items[item_id]
        action = copy.deepcopy(item.get("action", {}))
        added = copy.deepcopy(item)
        added["status"] = "in_progress"
        events: list[Any] = [
            self._encode(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": added,
                }
            )
        ]
        for event_type in (
            "response.web_search_call.in_progress",
            "response.web_search_call.searching",
        ):
            events.append(
                self._encode(
                    {
                        "type": event_type,
                        "item_id": item_id,
                        "output_index": output_index,
                        "sequence_number": self._next_sequence_number(),
                        "action": copy.deepcopy(action),
                    }
                )
            )
        return events

    def _start_for_item(self, item: Any, output_index: Any = None) -> list[Any]:
        if not self.enabled or not _provider_hidden_web_search_primary_reasoning_item(item):
            return []
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(item_id, str) or not item_id or item_id in self._seen_reasoning_ids:
            return []
        self._seen_reasoning_ids.add(item_id)
        if not isinstance(output_index, int) or output_index < 0:
            output_index = self._next_output_index
        self._next_output_index = max(self._next_output_index, output_index + 1)
        search_item = _provider_hidden_web_search_call_item(
            item,
            self.request_kwargs,
            self._source_urls,
            status="in_progress",
        )
        if search_item is None:
            return []
        search_item_id = str(search_item["id"])
        self._search_items[search_item_id] = search_item
        self._output_indexes[search_item_id] = output_index
        return self._item_events(search_item_id, output_index)

    def _completed_events(self) -> list[Any]:
        events: list[Any] = []
        for item_id, item in self._search_items.items():
            if item_id in self._completed_item_ids:
                continue
            output_index = self._output_indexes[item_id]
            completed = copy.deepcopy(item)
            completed["status"] = "completed"
            _external_web_search_apply_source_urls(completed["action"], self._source_urls)
            events.append(
                self._encode(
                    {
                        "type": "response.web_search_call.completed",
                        "item_id": item_id,
                        "output_index": output_index,
                        "sequence_number": self._next_sequence_number(),
                        "action": copy.deepcopy(completed.get("action", {})),
                    }
                )
            )
            events.append(
                self._encode(
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": completed,
                    }
                )
            )
            self._search_items[item_id] = completed
            self._completed_item_ids.add(item_id)
        return events

    def _message_start_events(
        self,
        chunk: dict[str, Any],
        *,
        include_content_part: bool = True,
    ) -> list[Any]:
        item_id = chunk.get("item_id")
        output_index = chunk.get("output_index")
        content_index = chunk.get("content_index")
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(output_index, int)
            or output_index < 0
        ):
            return []
        if not isinstance(content_index, int) or content_index < 0:
            content_index = 0
        self._next_output_index = max(self._next_output_index, output_index + 1)
        events: list[Any] = []
        if item_id not in self._started_message_ids:
            self._started_message_ids.add(item_id)
            events.append(
                self._encode(
                    {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": {
                            "id": item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [],
                        },
                    }
                )
            )
        content_part_key = (item_id, content_index)
        if (
            include_content_part
            and content_part_key not in self._started_content_parts
        ):
            self._started_content_parts.add(content_part_key)
            events.append(
                self._encode(
                    {
                        "type": "response.content_part.added",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "part": {
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                        },
                    }
                )
            )
        return events

    def _web_search_start_events(self, chunk: dict[str, Any]) -> list[Any]:
        item_id = chunk.get("item_id")
        output_index = chunk.get("output_index")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in self._started_web_search_ids
            or not isinstance(output_index, int)
            or output_index < 0
        ):
            return []
        action = chunk.get("action")
        if not isinstance(action, dict):
            return []
        clean_item = _sanitize_web_search_call_item(
            {
                "id": item_id,
                "type": "web_search_call",
                "status": "in_progress",
                "action": action,
            }
        )
        if clean_item is None:
            return []
        self._started_web_search_ids.add(item_id)
        self._next_output_index = max(self._next_output_index, output_index + 1)
        return [
            self._encode(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": clean_item,
                }
            )
        ]

    def _output_item_start_events(self, chunk: dict[str, Any]) -> list[Any]:
        item = chunk.get("item")
        item_type = _response_item_get(item, "type")
        item_id = _response_item_get(item, "id")
        output_index = chunk.get("output_index")
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(output_index, int)
            or output_index < 0
        ):
            return []
        if item_type == "web_search_call" and item_id not in self._started_web_search_ids:
            clean_item = _sanitize_web_search_call_item(item)
            if clean_item is None:
                return []
            clean_item["status"] = "in_progress"
            self._started_web_search_ids.add(item_id)
            return [
                self._encode(
                    {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": clean_item,
                    }
                )
            ]
        if item_type == "message" and item_id not in self._started_message_ids:
            return self._message_start_events(
                {
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                },
                include_content_part=False,
            )
        return []

    def _remember_message_start(self, chunk: dict[str, Any]) -> None:
        item = chunk.get("item")
        if _response_item_get(item, "type") != "message":
            return
        item_id = _response_item_get(item, "id")
        if isinstance(item_id, str) and item_id:
            self._started_message_ids.add(item_id)

    def _remember_web_search_start(self, chunk: dict[str, Any]) -> None:
        item = chunk.get("item")
        if _response_item_get(item, "type") != "web_search_call":
            return
        item_id = _response_item_get(item, "id")
        if isinstance(item_id, str) and item_id:
            self._started_web_search_ids.add(item_id)

    def _remember_content_part_start(self, chunk: dict[str, Any]) -> None:
        item_id = chunk.get("item_id")
        content_index = chunk.get("content_index")
        if (
            isinstance(item_id, str)
            and item_id
            and isinstance(content_index, int)
            and content_index >= 0
        ):
            self._started_content_parts.add((item_id, content_index))

    def _message_output_begins(
        self,
        chunk_type: str,
        chunk: dict[str, Any],
    ) -> bool:
        if chunk_type == "response.output_item.added":
            return _response_item_get(chunk.get("item"), "type") == "message"
        return chunk_type in {
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.annotation.added",
            "response.output_text.done",
            "response.content_part.done",
        }

    def _completion_response(self, response: Any) -> Any:
        payload = _streaming_module._jsonable(response)
        if not isinstance(payload, dict):
            return response
        payload = _sanitize_response_web_search_call_items(
            payload,
            self.request_kwargs,
        )
        return payload

    def consume(self, chunk: Any) -> list[Any]:
        _mark_provider_native_web_search_event(self.request_kwargs, chunk)
        if not self.enabled:
            return [chunk]
        dumped = _streaming_module._stream_chunk_dump(chunk)
        if not isinstance(dumped, dict) or not dumped:
            return [chunk]
        self._observe_sequence_number(dumped.get("sequence_number"))
        self._remember_urls(dumped)
        chunk_type = _streaming_module._stream_chunk_type(dumped)
        events: list[Any] = []
        if chunk_type in {
            "response.output_item.added",
            "response.output_item.done",
            "response.reasoning_item.added",
            "response.reasoning_item.done",
        }:
            item = dumped.get("item")
            if _provider_hidden_web_search_reasoning_item(item):
                events.extend(self._start_for_item(item, dumped.get("output_index")))
                if chunk_type == "response.output_item.done" and not self._search_items:
                    return events
                return events
        if _is_public_reasoning_summary_stream_event(chunk_type):
            if _provider_hidden_web_search_reasoning_item_id(
                dumped.get("item_id")
            ):
                return events
            events.append(self._forward_event(chunk))
            return events
        if chunk_type.startswith("response.reasoning"):
            # Raw reasoning remains private. Public summary events are handled
            # above, while the stable hidden-search item id is consumed only
            # to expose the standard web-search lifecycle.
            return events
        if self._message_output_begins(chunk_type, dumped):
            events.extend(self._completed_events())
        forwarded_payload: Optional[dict[str, Any]] = None
        if chunk_type in {
            "response.output_item.added",
            "response.output_item.done",
        } and self.active:
            item = dumped.get("item")
            clean_item = _final_answer_message_item(item)
            if clean_item is not item:
                forwarded_payload = copy.deepcopy(dumped)
                forwarded_payload["item"] = clean_item
        if chunk_type == "response.output_item.done":
            events.extend(self._output_item_start_events(forwarded_payload or dumped))
            if _response_item_get(dumped.get("item"), "type") == "message":
                events.extend(
                    self._message_start_events(
                        {
                            "item_id": _response_item_get(dumped.get("item"), "id"),
                            "output_index": dumped.get("output_index"),
                            "content_index": 0,
                        }
                    )
                )
        if chunk_type == "response.output_item.added":
            self._remember_message_start(forwarded_payload or dumped)
            self._remember_web_search_start(forwarded_payload or dumped)
        if chunk_type.startswith("response.web_search_call."):
            events.extend(self._web_search_start_events(dumped))
        if chunk_type == "response.content_part.added":
            events.extend(
                self._message_start_events(
                    dumped,
                    include_content_part=False,
                )
            )
            self._remember_content_part_start(dumped)
        if chunk_type in {
            "response.output_text.delta",
            "response.output_text.annotation.added",
            "response.output_text.done",
            "response.content_part.done",
        }:
            events.extend(self._message_start_events(dumped))
        if chunk_type == "response.completed":
            # Responses Lite/provider adapters sometimes omit the individual
            # output-item events and expose the private reasoning item only in
            # the terminal response.  Synthesize the same lifecycle in that
            # case so clients do not see a completed search with no progress.
            completed_response = dumped.get("response")
            completed_output = (
                completed_response.get("output")
                if isinstance(completed_response, dict)
                else None
            )
            if isinstance(completed_output, list):
                for output_index, item in enumerate(completed_output):
                    events.extend(self._start_for_item(item, output_index))
            events.extend(self._completed_events())
            clean = copy.deepcopy(dumped)
            clean["response"] = self._completion_response(dumped.get("response"))
            events.append(self._forward_event(chunk, clean))
            return events
        events.append(self._forward_event(chunk, forwarded_payload))
        return events

    def finalize(self) -> list[Any]:
        return self._completed_events()


class _ProviderHiddenWebSearchStream:
    """Async-iterator wrapper that also propagates early close to the source.

    The Responses delivery layer deliberately closes an upstream iterator as
    soon as it sees ``response.completed``.  A plain async-generator wrapper
    would swallow that close (and yielding from its ``finally`` block is not
    legal while ``aclose`` is running), so keep the adapter as an explicit
    iterator with a real ``aclose`` implementation.
    """

    def __init__(self, response: Any, request_kwargs: Optional[dict]) -> None:
        self._response = response
        self._iterator: Any = None
        self._adapter = _ProviderHiddenWebSearchStreamAdapter(request_kwargs)
        self._pending: list[Any] = []
        self._finished = False
        self._closed = False

    def __aiter__(self) -> "_ProviderHiddenWebSearchStream":
        return self

    async def __anext__(self) -> Any:
        if self._pending:
            return self._pending.pop(0)
        if self._closed or self._finished:
            raise StopAsyncIteration
        if self._iterator is None:
            self._iterator = self._response.__aiter__()
        while True:
            try:
                chunk = await self._iterator.__anext__()
            except StopAsyncIteration:
                self._finished = True
                self._pending.extend(self._adapter.finalize())
                if self._pending:
                    return self._pending.pop(0)
                raise
            self._pending.extend(self._adapter.consume(chunk))
            if self._pending:
                return self._pending.pop(0)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        iterator = self._iterator or self._response
        close = getattr(iterator, "aclose", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            return


def _adapt_provider_hidden_web_search_stream(
    response: Any,
    request_kwargs: Optional[dict],
) -> Any:
    """Return a provider stream with private search reasoning translated."""
    # A terminal Responses failure is a control object, not an upstream
    # provider stream.  Preserve its identity so the streaming error layer
    # can inspect the original exception and run the Hosted-to-Chat bridge.
    # Wrapping it here would turn it into a generic async generator and lose
    # the only opportunity to recover before route cooldown/retry handling.
    if _routing_module._is_failed_responses_stream_response(response):
        return response
    return _ProviderHiddenWebSearchStream(response, request_kwargs)


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


def _sanitize_response_web_search_call_items(
    response: Any,
    request_kwargs: Optional[dict] = None,
) -> Any:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        return response
    output = payload.get("output")
    if not isinstance(output, list):
        return response
    response_source_urls = _provider_hosted_web_search_source_urls(payload)
    clean_output: list[Any] = []
    changed = False
    hidden_items: list[dict[str, Any]] = []
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
        if (
            isinstance(request_kwargs, dict)
            and _tools_module._request_has_web_search_tool(request_kwargs)
            and _provider_hidden_web_search_primary_reasoning_item(item)
        ):
            hidden_item = _provider_hidden_web_search_call_item(
                item,
                request_kwargs,
                response_source_urls,
            )
            if hidden_item is not None:
                hidden_items.append(hidden_item)
                changed = True
                continue
        clean_output.append(item)
    if hidden_items:
        for index in range(len(clean_output) - 1, -1, -1):
            clean_item = _final_answer_message_item(clean_output[index])
            if clean_item is not clean_output[index]:
                clean_output[index] = clean_item
                break
        clean_output = hidden_items + clean_output
        changed = True
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


def _is_public_reasoning_summary_stream_event(chunk_type: str) -> bool:
    return chunk_type.startswith("response.reasoning_summary_")


def _sanitize_reasoning_output_item(item: Any) -> Optional[Any]:
    """Keep the public summary and opaque replay token, never raw reasoning."""
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return None
    if "content" not in item:
        return item
    clean_item = copy.deepcopy(item)
    clean_item.pop("content", None)
    return clean_item


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
            if _provider_hidden_web_search_reasoning_item(item):
                changed = True
                continue
            clean_reasoning_item = _sanitize_reasoning_output_item(item)
            if clean_reasoning_item is None:
                changed = True
                continue
            clean_output.append(clean_reasoning_item)
            changed = changed or clean_reasoning_item is not item
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


def _sanitize_response_stream_payload(
    response: Any,
    request_kwargs: Optional[dict] = None,
) -> Any:
    return _sanitize_response_reasoning_items(
        _sanitize_response_raw_tool_call_text(
            _sanitize_response_web_search_call_items(response, request_kwargs)
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
    if _is_public_reasoning_summary_stream_event(chunk_type):
        return chunk
    if chunk_type.startswith("response.reasoning"):
        return None

    if chunk_type in {"response.output_item.added", "response.output_item.done"}:
        item = dumped.get("item")
        if _is_reasoning_output_item(item):
            if _provider_hidden_web_search_reasoning_item(item):
                return None
            clean_item = _sanitize_reasoning_output_item(item)
            if clean_item is None:
                return None
            if clean_item is item:
                return chunk
            clean_chunk = copy.deepcopy(dumped)
            clean_chunk["item"] = clean_item
            return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk
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
        if value is None or depth > 6 or len(urls) >= 20:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in {"url", "href"}:
                    add_url(child)
                if len(urls) >= 20:
                    return
                if isinstance(child, (dict, list, tuple)):
                    visit(child, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth + 1)
                if len(urls) >= 20:
                    return

    visit(_streaming_module._jsonable(structured))
    for match in re.finditer(r"https?://[^\s<>\"]+", text or ""):
        add_url(match.group(0))
        if len(urls) >= 20:
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
    for index in range(len(output) - 1, -1, -1):
        clean_item = _final_answer_message_item(output[index])
        if clean_item is not output[index]:
            output[index] = clean_item
            break
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
    for index in range(len(output) - 1, -1, -1):
        clean_item = _final_answer_message_item(output[index])
        if clean_item is not output[index]:
            output[index] = clean_item
            break
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

def _web_search_queries_from_response(response: Any) -> list[str]:
    actions = _web_search_actions_from_response(response)
    queries: list[str] = []
    for action in actions:
        if action.get("type") != "search":
            continue
        query = action.get("query")
        if query and query not in queries:
            queries.append(query)
    return queries


def _web_search_actions_from_response(response: Any) -> list[dict[str, str]]:
    calls = _web_search_function_calls(response)
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for call in calls:
        if _function_call_name(call) == "web_search":
            parsed = _web_search_arguments_from_call(call)
            raw_queries = parsed.get("queries") if isinstance(parsed, dict) else None
            if isinstance(raw_queries, list):
                for raw_query in raw_queries:
                    if not isinstance(raw_query, str) or not raw_query.strip():
                        continue
                    action = {"type": "search", "query": raw_query.strip()}
                    key = _external_web_search_action_key(action)
                    if key not in seen:
                        seen.add(key)
                        actions.append(action)
                continue
        action = _web_search_action_from_call(call)
        action = _external_web_search_valid_action(action)
        if not action:
            continue
        key = _external_web_search_action_key(action)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    return actions


def _external_web_search_has_malformed_function_call(response: Any) -> bool:
    return bool(_web_search_function_calls(response)) and not bool(
        _web_search_actions_from_response(response)
    )


def _web_search_actions_for_request(
    response: Any,
    request_kwargs: Optional[dict],
) -> list[dict[str, str]]:
    _ = request_kwargs
    return _web_search_actions_from_response(response)


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
        r"读取|调用|执行|深挖|调查|研究|继续查|再查)"
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
        if _responses_output_module._response_text(item).strip():
            messages.append(item)
    return messages


def _external_web_search_has_completed_assistant_message(response: Any) -> bool:
    return bool(_external_web_search_completed_assistant_message_items(response))


def _has_web_search_actions_for_request(
    response: Any,
    request_kwargs: Optional[dict],
) -> bool:
    return bool(_web_search_actions_for_request(response, request_kwargs))


def _web_search_queries_for_request(
    response: Any,
    request_kwargs: Optional[dict],
) -> list[str]:
    queries: list[str] = []
    for action in _web_search_actions_for_request(response, request_kwargs):
        if action.get("type") != "search":
            continue
        query = action.get("query")
        if query and query not in queries:
            queries.append(query)
    return queries


async def _external_web_search_run_query(
    query: str,
    *,
    page: int = 1,
) -> tuple[str, list[str]]:
    try:
        text, structured = await asyncio.to_thread(
            _pi_web_access_module._pi_web_access_search_sync,
            query,
            page=page,
        )
        urls = _external_web_search_source_urls(structured, text)
    except Exception as exc:
        text = f"Search failed for query {query!r}: {exc}"
        urls = []
    page_line = f"\nResult page: {page}" if page > 1 else ""
    return f"Web search results for query: {query}{page_line}\n\n{text}", urls


def _external_web_search_page_read_chars() -> int:
    return _external_web_search_int_env(
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
    return await _external_web_search_fetch_page_text_with_limit(
        url,
        max_chars=_external_web_search_page_read_chars(),
    )


async def _external_web_search_fetch_page_text_with_limit(
    url: str,
    *,
    max_chars: int,
) -> str:
    try:
        text = await asyncio.to_thread(
            _pi_web_access_module._pi_web_access_page_excerpt,
            url,
            timeout=_external_web_search_page_timeout_seconds(),
            max_chars=max_chars,
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
    *,
    max_chars: Optional[int] = None,
) -> str:
    read_chars = max_chars or _external_web_search_page_read_chars()
    cache_key = f"{url}\x00{read_chars}"
    cached = page_cache.get(cache_key)
    if isinstance(cached, str) and cached.strip():
        return cached

    fetch_task = page_fetch_tasks.get(cache_key)
    if fetch_task is None:
        fetch_task = asyncio.create_task(
            _external_web_search_fetch_page_text_with_limit(
                url,
                max_chars=read_chars,
            )
        )
        page_fetch_tasks[cache_key] = fetch_task

    try:
        text = await fetch_task
    finally:
        if page_fetch_tasks.get(cache_key) is fetch_task and fetch_task.done():
            page_fetch_tasks.pop(cache_key, None)

    if not isinstance(text, str) or not text.strip():
        text = f"Page retrieval returned no readable text for URL: {url}"
    page_cache[cache_key] = text
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
    # A model-directed page find is an explicit request for evidence, so scan
    # the full bounded document rather than treating absence from the leading
    # display excerpt as a negative result. This matters for source files,
    # where definitions commonly appear after imports and class helpers.
    text = await _external_web_search_page_text(
        url,
        page_cache,
        page_fetch_tasks,
        max_chars=12000,
    )
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
    page = _external_web_search_page_number(action.get("page")) or 1
    section, urls = await _external_web_search_run_query(query, page=page)
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
    return _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_ROUNDS_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_ROUNDS_DEFAULT,
        1,
        8,
    )


def _external_web_search_max_queries() -> int:
    return _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_QUERIES_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_QUERIES_DEFAULT,
        1,
        64,
    )


def _external_web_search_max_open_pages() -> int:
    return _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_DEFAULT,
        0,
        32,
    )


def _external_web_search_max_find_in_page() -> int:
    return _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_DEFAULT,
        0,
        64,
    )


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


def _external_web_search_synthesis_invalid_reason(response: Any) -> Optional[str]:
    if _web_search_function_calls(response):
        return "web_search_function_call"
    if _external_web_search_has_completed_assistant_message(response):
        return None
    text = _responses_output_module._response_text(response)
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
    if _external_web_search_has_malformed_function_call(response):
        return "malformed_web_search_function_call"
    if _web_search_function_calls(response):
        return None
    if _external_web_search_has_completed_assistant_message(response):
        return None
    text = _responses_output_module._response_text(response)
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
        response_preview=_trace_module._sanitize_trace_text(_responses_output_module._response_text(response)),
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

    def append_card(title: Any, url: Any, snippet: Any = "") -> None:
        cleaned_url = _external_web_search_clean_url(url)
        if not cleaned_url or cleaned_url in seen_urls:
            return
        seen_urls.add(cleaned_url)
        normalized_snippet = " ".join(str(snippet or "").split())
        if len(normalized_snippet) > 420:
            normalized_snippet = normalized_snippet[:420].rstrip() + "..."
        cards.append(
            {
                "title": " ".join(str(title or "").split()) or cleaned_url,
                "url": cleaned_url,
                "snippet": normalized_snippet,
            }
        )

    pattern = re.compile(
        r"(?ms)^Title:\s*(?P<title>.*?)\n"
        r"URL:\s*(?P<url>\S+)\n"
        r"Snippet:\s*(?P<snippet>.*?)(?=\n\nTitle:|\n\nWeb search results for query:|\n\nRetrieved page content for URL:|\n\nPage text matches for pattern:|\Z)"
    )
    for match in pattern.finditer(search_results or ""):
        snippet_source = re.split(
            r"\n\s*\n|Retrieved page content for URL:|Page text matches for pattern:",
            match.group("snippet"),
            maxsplit=1,
        )[0]
        append_card(match.group("title"), match.group("url"), snippet_source)

    # pi-web-access renders workflow:none results as numbered title/URL pairs.
    # Keep the bridge's card shape stable for the standalone search endpoint.
    numbered_pattern = re.compile(
        r"(?m)^\s*\d+\.\s+(?P<title>[^\n]+?)\s*\n"
        r"\s+(?P<url>https?://\S+)\s*(?=\n|\Z)"
    )
    for match in numbered_pattern.finditer(search_results or ""):
        append_card(match.group("title"), match.group("url"))

    full_result_pattern = re.compile(
        r"(?ms)^###\s+(?P<title>[^\n]+?)\s*\n"
        r"(?P<url>https?://\S+)\s*(?=\n|\Z)"
    )
    for match in full_result_pattern.finditer(search_results or ""):
        append_card(match.group("title"), match.group("url"))
    return cards


_EXTERNAL_WEB_SEARCH_SYNTHESIS_EVIDENCE_MAX_CHARS = 6000
_EXTERNAL_WEB_SEARCH_SYNTHESIS_SECTION_MAX_CHARS = 1400
_EXTERNAL_WEB_SEARCH_CONTINUATION_EVIDENCE_MAX_CHARS = 10000
_EXTERNAL_WEB_SEARCH_CONTINUATION_SECTION_MAX_CHARS = 9000
# The hidden bridge turns only need to emit a compact tool call or a short
# decision. Keeping these budgets bounded prevents a Kimi thinking route from
# spending a full answer budget on an internal routing turn.
_EXTERNAL_WEB_SEARCH_INITIAL_OUTPUT_TOKENS = 128
_EXTERNAL_WEB_SEARCH_CONTINUATION_OUTPUT_TOKENS = 512
_EXTERNAL_WEB_SEARCH_SYNTHESIS_OUTPUT_TOKENS = 1536
_EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_BY_EXCEPTION_ID: dict[int, dict[str, Any]] = {}
_EXTERNAL_WEB_SEARCH_RECOVERY_REQUESTS_MAX = 256
_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY = "external_web_search_original_user_text"
_EXTERNAL_WEB_SEARCH_CONVERSATION_CONTEXT_KEY = (
    "external_web_search_conversation_context"
)
_EXTERNAL_WEB_SEARCH_CONVERSATION_CONTEXT_MAX_CHARS = 8000
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
    _CURRENT_UPSTREAM_URL_SURFACE_KEY,
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
        metadata = _request_context_module._request_metadata_dict(
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
    for marker in ("\n\nRetrieved page content for URL:", "\n\nPage text matches for pattern:"):
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


def _external_web_search_evidence_section_priority(section: str) -> int:
    text = section.lstrip()
    if text.startswith("Page text matches for pattern:"):
        return 0
    if text.startswith("Retrieved page content for URL:"):
        return 1
    if text.startswith("Web search results for query:"):
        return 2
    return 3


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

    # The model explicitly chooses which sources to open or search within.
    # Those resulting excerpts are stronger evidence than the earlier result
    # snippets, especially when the synthesis budget cannot retain everything.
    # Within each class, prefer the most recent model-selected action.
    prioritized_sections = [
        section
        for _index, section in sorted(
            enumerate(sections),
            key=lambda item: (
                _external_web_search_evidence_section_priority(item[1]),
                -item[0],
            ),
        )
    ]
    compact_sections: list[str] = []
    total_chars = 0
    for section in prioritized_sections:
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
                "phase": "final_answer",
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
    model_info = _request_context_module._request_model_info(request_kwargs)
    surface = _routing_module._request_current_upstream_surface(request_kwargs)
    if not surface:
        surface = model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    if surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True
    return False


def _external_web_search_inherit_active_chat_surface(
    target_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
) -> None:
    surface = _routing_module._request_current_upstream_surface(request_kwargs)
    if surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        target_kwargs[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface


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


def _external_web_search_chat_tool_call_message(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    function = item.get("function")
    function_payload = function if isinstance(function, dict) else item
    name = _routing_module._valid_chat_tool_name(
        function_payload.get("name") or item.get("name")
    )
    if name is None:
        return None
    if item.get("type") == "custom_tool_call":
        arguments = {"input": item.get("input", "")}
    else:
        arguments = function_payload.get("arguments")
        if arguments is None:
            arguments = item.get("arguments")
    if not isinstance(arguments, str):
        try:
            arguments = json.dumps(arguments if arguments is not None else {})
        except (TypeError, ValueError):
            arguments = "{}"
    call_id = item.get("call_id") or item.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _external_web_search_chat_tool_output_content(item: dict[str, Any]) -> str:
    for key in ("output", "content", "text", "output_text"):
        value = item.get(key)
        text = _external_web_search_chat_message_content(value)
        if text:
            return text
        if value is not None:
            try:
                return json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(value)
    return ""


def _external_web_search_chat_tool_messages(
    call_kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    instructions = call_kwargs.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})

    input_value = call_kwargs.get("input")
    if isinstance(input_value, str):
        text = input_value.strip()
        if text:
            messages.append({"role": "user", "content": text})
    elif isinstance(input_value, list):
        index = 0
        while index < len(input_value):
            item = input_value[index]
            if not isinstance(item, dict):
                index += 1
                continue
            item_type = item.get("type")
            if item_type in {"function_call", "custom_tool_call", "tool_call"}:
                tool_calls: list[dict[str, Any]] = []
                while index < len(input_value):
                    call_item = input_value[index]
                    if not isinstance(call_item, dict) or call_item.get("type") not in {
                        "function_call",
                        "custom_tool_call",
                        "tool_call",
                    }:
                        break
                    chat_tool_call = _external_web_search_chat_tool_call_message(call_item)
                    if chat_tool_call is not None:
                        tool_calls.append(chat_tool_call)
                    index += 1
                if tool_calls:
                    messages.append({"role": "assistant", "tool_calls": tool_calls})
                continue
            if item_type in {
                "function_call_output",
                "custom_tool_call_output",
                "tool_call_output",
            }:
                call_id = item.get("call_id") or item.get("tool_call_id") or item.get("id")
                if isinstance(call_id, str) and call_id.strip():
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _external_web_search_chat_tool_output_content(item),
                        }
                    )
                index += 1
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
            if role == "tool":
                call_id = item.get("tool_call_id") or item.get("call_id")
                if isinstance(call_id, str) and call_id.strip():
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": text,
                        }
                    )
            elif text:
                messages.append({"role": role, "content": text})
            index += 1

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
    seen_direct_tool = False
    request_tools = request_kwargs.get("tools") if isinstance(request_kwargs, dict) else None
    # Codex's deferred tool surface contains a tool_search item plus a large
    # namespace of ordinary desktop tools. Those tools are not candidates for
    # this hidden web-search routing turn; sending all of them to the provider
    # needlessly inflates every continuation request. Keep the bridge itself
    # available so the model still controls query changes, result pages, and
    # source opening.
    if isinstance(request_tools, list) and any(
        isinstance(tool, dict)
        and (
            tool.get("type") == "tool_search"
            or (
                tool.get("type") == "function"
                and (
                    tool.get("name") == "tool_search"
                    or (
                        isinstance(tool.get("function"), dict)
                        and tool["function"].get("name") == "tool_search"
                    )
                )
            )
        )
        for tool in request_tools
    ):
        return _responses_tools_module._pi_web_access_tool_definitions()
    if isinstance(request_tools, list):
        direct_pi_tools: list[dict[str, Any]] = []
        for tool in request_tools:
            if not isinstance(tool, dict):
                continue
            function_name = _function_call_name(tool)
            if function_name in _PI_WEB_ACCESS_TOOL_NAMES:
                direct_pi_tools.append(copy.deepcopy(tool))
        if direct_pi_tools:
            return direct_pi_tools
        for tool in request_tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if tool_type in {"web_search", "web_search_preview"}:
                continue
            copied = copy.deepcopy(tool)
            tools.append(copied)
            if _function_call_name(copied) in _PI_WEB_ACCESS_TOOL_NAMES:
                seen_direct_tool = True
    if not seen_direct_tool:
        tools.extend(_responses_tools_module._pi_web_access_tool_definitions())
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


def _external_web_search_chat_route_model(
    request_kwargs: Optional[dict],
) -> str:
    model = _responses_execution_module._request_selected_route_upstream_model(
        request_kwargs
    )
    if isinstance(model, str) and model.strip():
        return model.strip().lower()
    model_group = _responses_execution_module._request_model_group(request_kwargs)
    return model_group.strip().lower() if isinstance(model_group, str) else ""


def _external_web_search_chat_route_is_non_gpt(
    request_kwargs: Optional[dict],
) -> bool:
    if not _external_web_search_chat_only_route(request_kwargs):
        return False
    model = _external_web_search_chat_route_model(request_kwargs)
    if not model:
        return False
    # Only recognized non-GPT chat families use the optional weak-search path.
    # Keep GPT-family and unknown aliases on the stricter current-fact path.
    return bool(
        re.search(
            r"(?:^|[/_-])(?:kimi|claude|gemini|qwen|deepseek|glm|ernie|"
            r"doubao|minimax|mistral|llama|moonshot|yi|internlm)(?:[-_/]|$)",
            model,
        )
    )


def _external_web_search_requires_initial_lookup(
    request_kwargs: Optional[dict],
) -> bool:
    text = _external_web_search_user_prompt_text(request_kwargs)
    if not text:
        return False
    lowered = text.lower()
    if re.search(
        r"\b(?:weather|forecast|news|score|scores|schedule|schedules|"
        r"stock(?:\s+price)?|exchange\s+rate|latest|current)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:search|look\s+up|find|verify|research)\s+(?:the\s+)?"
        r"(?:web|internet|online)\b",
        lowered,
    ):
        return True
    return bool(
        re.search(
            r"(?:天气|预报|新闻|比分|赛程|股价|汇率|实时|最新|当前)"
            r"|(?:搜索|检索|查询|查找)(?!桥|功能|接口|实现|代码|工具)",
            text,
        )
    )


def _external_web_search_chat_tool_payload(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
    *,
    phase: str = "continuation",
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
    active_request_kwargs = request_kwargs or call_kwargs
    initial_lookup_required = phase == "initial" and _external_web_search_requires_initial_lookup(
        active_request_kwargs
    )
    optional_third_party_lookup = (
        initial_lookup_required
        and _external_web_search_chat_route_is_non_gpt(active_request_kwargs)
    )
    if optional_third_party_lookup:
        weak_search_note = (
            "A local evidence lookup function is available but is optional; "
            "its ranking and snippets can be noisy. "
            "Use your own knowledge and reasoning first; call it only when "
            "current or external evidence is genuinely needed. If you call "
            "it, use one focused query and stop when the evidence is enough."
        )
        time_note = _responses_tools_module._current_time_context_instruction(
            active_request_kwargs
        )
        if time_note:
            weak_search_note = f"{weak_search_note} {time_note}"
        payload["messages"] = [
            {
                "role": "system",
                "content": weak_search_note,
            },
            {
                "role": "user",
                "content": _external_web_search_user_prompt_text(active_request_kwargs),
            },
        ]
        payload["tool_choice"] = "auto"
    elif initial_lookup_required:
        # The local functions are ordinary optional tools. Do not force a
        # particular function call merely because the request looks current.
        payload["tool_choice"] = "auto"
    elif "tool_choice" in call_kwargs:
        payload["tool_choice"] = _external_web_search_chat_completion_tool_choice(
            call_kwargs.get("tool_choice")
        )
    if isinstance(call_kwargs.get("parallel_tool_calls"), bool):
        payload["parallel_tool_calls"] = call_kwargs["parallel_tool_calls"]

    max_completion_tokens = _request_context_module._positive_int_value(
        call_kwargs.get("max_completion_tokens")
    )
    if max_completion_tokens is None:
        max_completion_tokens = _request_context_module._positive_int_value(
            call_kwargs.get("max_output_tokens")
        )
    if max_completion_tokens is None and initial_lookup_required:
        if optional_third_party_lookup:
            max_completion_tokens = 512
        else:
            max_completion_tokens = _EXTERNAL_WEB_SEARCH_INITIAL_OUTPUT_TOKENS
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
        metadata = _request_context_module._request_metadata_dict(
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

    max_completion_tokens = _request_context_module._positive_int_value(
        call_kwargs.get("max_completion_tokens")
    )
    if max_completion_tokens is None:
        max_completion_tokens = _request_context_module._positive_int_value(
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
        metadata = _request_context_module._request_metadata_dict(
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


def _external_web_search_chat_completion_function_call_items(
    message: Any,
) -> list[dict[str, Any]]:
    message_payload = _streaming_module._jsonable(message)
    if not isinstance(message_payload, dict):
        return []
    raw_calls = message_payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        legacy_call = message_payload.get("function_call")
        raw_calls = [legacy_call] if isinstance(legacy_call, dict) else []

    items: list[dict[str, Any]] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        function_payload = function if isinstance(function, dict) else raw_call
        name = _routing_module._valid_chat_tool_name(
            function_payload.get("name") or raw_call.get("name")
        )
        if name is None:
            continue
        arguments = function_payload.get("arguments")
        if arguments is None:
            arguments = raw_call.get("arguments")
        if not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments if arguments is not None else {})
            except (TypeError, ValueError):
                arguments = "{}"
        call_id = raw_call.get("id") or raw_call.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_chat_{index}_{time.time_ns()}"
        items.append(
            {
                "id": call_id,
                "call_id": call_id,
                "type": "function_call",
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }
        )
    return items


def _external_web_search_chat_completion_message(chat_response: Any) -> Any:
    payload = _streaming_module._jsonable(chat_response)
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    return choice.get("message") if isinstance(choice, dict) else None


def _external_web_search_chat_progress_preamble_reason(chat_response: Any) -> Optional[str]:
    message = _external_web_search_chat_completion_message(chat_response)
    if _external_web_search_chat_completion_function_call_items(message):
        return None
    return _external_web_search_progress_preamble_reason(
        _external_web_search_chat_message_text(message)
    )


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
    tool_call_items = _external_web_search_chat_completion_function_call_items(
        message
    )
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
    if not tool_call_items:
        return response

    output = response.get("output")
    if not isinstance(output, list):
        output = []
        response["output"] = output
    if text.strip():
        if output and isinstance(output[0], dict):
            output[0]["phase"] = "commentary"
    else:
        response.pop("output_text", None)
        output.clear()
    output.extend(tool_call_items)
    return _responses_output_module._normalize_response_tool_search_output(
        response,
        _responses_output_module._responses_namespace_tool_map_from_tools(
            request_kwargs.get("tools") if isinstance(request_kwargs, dict) else None
        ),
        _responses_output_module._responses_custom_tool_names_from_tools(
            request_kwargs.get("tools") if isinstance(request_kwargs, dict) else None
        ),
    )


async def _external_web_search_chat_synthesis_response(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
) -> Optional[Any]:
    _external_web_search_inherit_active_chat_surface(call_kwargs, request_kwargs)
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
    _external_web_search_inherit_active_chat_surface(call_kwargs, request_kwargs)
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
        _external_web_search_chat_tool_payload(
            call_kwargs,
            request_kwargs,
            phase=phase,
        )
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
    selected_deployment_box: dict[str, Any] = {}
    selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
        selected_deployment_box
    )
    try:
        chat_response = await acompletion(**payload)
        if _external_web_search_has_malformed_function_call(chat_response):
            retry_payload = copy.deepcopy(payload)
            messages = retry_payload.get("messages")
            if not isinstance(messages, list):
                messages = []
                retry_payload["messages"] = messages
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "The previous web-search tool call had malformed or truncated "
                        "JSON arguments. Retry the turn once. You still decide whether to "
                        "search, change the query, request another result page, open a URL, "
                        "find text on a page, or answer directly; if you call a tool, emit "
                        "complete valid JSON arguments."
                    ),
                },
            )
            _trace_module._route_trace(
                "external_web_search_bridge_chat_tool_malformed_retry",
                request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
                session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
                model_group=_responses_execution_module._request_model_group(call_kwargs)
                or _responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(call_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(call_kwargs),
                phase=phase,
            )
            chat_response = await acompletion(**retry_payload)

        progress_reason = _external_web_search_chat_progress_preamble_reason(
            chat_response
        )
        if progress_reason is not None:
            retry_payload = copy.deepcopy(payload)
            messages = retry_payload.get("messages")
            if not isinstance(messages, list):
                messages = []
                retry_payload["messages"] = messages
            progress_text = _external_web_search_chat_message_text(
                _external_web_search_chat_completion_message(chat_response)
            )
            if progress_text:
                messages.append({"role": "assistant", "content": progress_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Continue the current task now. Do not return another status "
                        "update or plan. If the declared work requires a skill or another "
                        "tool, make the corresponding tool call in this turn; only give a "
                        "final answer after the task is actually complete."
                    ),
                }
            )
            _trace_module._route_trace(
                "external_web_search_bridge_chat_tool_progress_retry",
                request_id=_routing_module._trace_request_id(request_kwargs or call_kwargs),
                session=_routing_module._trace_session_context(request_kwargs or call_kwargs),
                model_group=_responses_execution_module._request_model_group(call_kwargs)
                or _responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(call_kwargs),
                route_key=_routing_module._deployment_route_key_from_request(call_kwargs),
                phase=phase,
                invalid_reason=progress_reason,
            )
            chat_response = await acompletion(**retry_payload)
    finally:
        _routing_module._apply_current_selected_deployment_to_request(
            call_kwargs,
            selected_box=selected_deployment_box,
        )
        if request_kwargs is not call_kwargs:
            _routing_module._apply_current_selected_deployment_to_request(
                request_kwargs,
                selected_box=selected_deployment_box,
            )
        _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(selected_deployment_box_token)
    if _external_web_search_has_malformed_function_call(chat_response):
        raise _external_web_search_invalid_synthesis_exception(
            request_kwargs or call_kwargs,
            reason="malformed_web_search_function_call",
            phase=phase,
        )
    progress_reason = _external_web_search_chat_progress_preamble_reason(chat_response)
    if progress_reason is not None:
        raise _external_web_search_invalid_synthesis_exception(
            request_kwargs or call_kwargs,
            reason=progress_reason,
            phase=phase,
        )
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
    metadata = _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    value = metadata.get("external_web_search_completed_actions")
    if not isinstance(value, list):
        return []
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        if any(key in item for key in ("arguments", "function", "input")):
            action = _web_search_action_from_call(item)
        else:
            action = _web_search_action_from_call({"arguments": item})
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
    metadata = _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    value = metadata.get("external_web_search_search_results")
    return value if isinstance(value, str) else ""


def _external_web_search_metadata(request_kwargs: Optional[dict]) -> dict[str, Any]:
    return _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}


def _mark_external_web_search_started(request_kwargs: Optional[dict]) -> None:
    _routing_module._mark_external_web_search_started_for_request(request_kwargs)
    if not isinstance(request_kwargs, dict):
        return
    metadata = _request_context_module._request_metadata_dict(
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


def _external_web_search_conversation_context(
    request_kwargs: Optional[dict],
) -> str:
    """Keep recent plain conversation text when the bridge compacts input."""

    metadata = _external_web_search_metadata(request_kwargs)
    stored_context = metadata.get(_EXTERNAL_WEB_SEARCH_CONVERSATION_CONTEXT_KEY)
    if isinstance(stored_context, str) and stored_context.strip():
        return stored_context.strip()

    if not isinstance(request_kwargs, dict):
        return ""
    source = request_kwargs.get("input")
    if source is None:
        source = request_kwargs.get("messages")
    if isinstance(source, list):
        blocks: list[dict[str, Any]] = []
        for item in source:
            blocks.extend(_trace_module._trace_text_blocks(item))
    else:
        blocks = _trace_module._trace_text_blocks(source)
    context_blocks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("kind") == "internal_context":
            continue
        role = str(block.get("role") or "").strip().lower()
        if role not in {"user", "human", "assistant", "tool"}:
            continue
        text = str(block.get("text") or "").strip()
        if text:
            context_blocks.append(f"{role}: {text}")

    if not context_blocks:
        return ""

    retained: list[str] = []
    retained_chars = 0
    for block in reversed(context_blocks):
        projected = retained_chars + len(block) + (1 if retained else 0)
        if projected > _EXTERNAL_WEB_SEARCH_CONVERSATION_CONTEXT_MAX_CHARS:
            break
        retained.append(block)
        retained_chars = projected
    return "\n".join(reversed(retained))


def _external_web_search_conversation_context_prefix(
    request_kwargs: Optional[dict],
) -> str:
    context = _external_web_search_conversation_context(request_kwargs)
    if not context:
        return ""
    return (
        "Recent conversation context from the current thread. The latest user "
        "entry is the current request; use the surrounding assistant/user entries "
        "to resolve short follow-ups:\n"
        f"{context}\n\n"
    )

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
            response_preview=_trace_module._sanitize_trace_text(_responses_output_module._response_text(synthesized)),
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
    queries: list[str],
    completed_actions: Optional[list[dict[str, str]]] = None,
    round_number: int,
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

    continuation_kwargs = _external_web_search_low_reasoning_kwargs(
        continuation_kwargs
    )
    if _request_context_module._positive_int_value(
        continuation_kwargs.get("max_output_tokens")
    ) is None:
        continuation_kwargs["max_output_tokens"] = _EXTERNAL_WEB_SEARCH_CONTINUATION_OUTPUT_TOKENS

    metadata = _request_context_module._request_metadata_dict(continuation_kwargs, "litellm_metadata") or {}
    continuation_metadata = metadata.copy()
    original_request = _external_web_search_user_prompt_text(request_kwargs)
    conversation_context = _external_web_search_conversation_context(request_kwargs)
    if original_request:
        continuation_metadata[_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY] = original_request
    if conversation_context:
        continuation_metadata[_EXTERNAL_WEB_SEARCH_CONVERSATION_CONTEXT_KEY] = (
            conversation_context
        )
    continuation_metadata["external_web_search_continuation"] = True
    continuation_metadata["external_web_search_round"] = round_number
    continuation_metadata["external_web_search_completed_actions"] = copy.deepcopy(
        completed_actions or []
    )
    continuation_metadata["external_web_search_search_results"] = continuation_evidence
    continuation_kwargs["litellm_metadata"] = continuation_metadata

    continuation_has_pi_tools = any(
        isinstance(tool, dict)
        and _function_call_name(tool) in _PI_WEB_ACCESS_TOOL_NAMES
        for tool in continuation_kwargs.get("tools", [])
    )
    if continuation_has_pi_tools:
        note = (
            "Web-search evidence is attached below. Decide the next step now and "
            "answer the user's request directly. If the evidence is insufficient, "
            "you may use the "
            "available native pi-web-access functions according to their schemas; "
            "otherwise provide the final answer now. Do not emit tool-call markup "
            "as text."
        )
    else:
        note = (
            "Retrieved evidence is attached below. Decide the next step from the "
            "available tools and evidence: choose whether to search again (and which "
            "result page), open a listed source URL, find text within an opened page, or "
            "provide the final answer. If the search-result snippets directly answer the "
            "user's request with the needed facts, provide the final answer immediately; "
            "do not open a page merely to repeat them. Open a source, change the query, "
            "or request another result page only when evidence is missing, ambiguous, "
            "conflicting, or the user explicitly asks for deeper verification. Page-text "
            "matches and opened-page content are stronger evidence than search-result "
            "snippets; when sources conflict, do not infer a fact is absent merely because "
            "a wrapper or snippet omits it. For a narrow factual request, prefer one "
            "clearly specific source before opening additional sources. "
            "Do not emit tool-call markup as text."
        )
    time_note = _responses_tools_module._current_time_context_instruction(request_kwargs)
    if time_note:
        note = f"{note} {time_note}"
    continuation_kwargs["instructions"] = note

    time_context_lines = ""
    if time_note:
        time_context_lines = f"Authoritative time context:\n{time_note}\n\n"
    query_lines = "\n".join(f"- {query}" for query in queries) or "- (none)"
    next_step_text = (
        "If the evidence directly answers the request, provide the final answer. "
        "Otherwise you may use a native pi-web-access function according to its "
        "schema."
        if continuation_has_pi_tools
        else
        "If the evidence directly answers the request, provide the final answer. "
        "Otherwise choose whether to use an available lookup function with a focused "
        "query, a source URL to read, or a URL plus pattern to find text."
    )
    continuation_kwargs["input"] = (
        _external_web_search_conversation_context_prefix(request_kwargs)
        + "Original user request:\n"
        f"{original_request or '(no user text extracted)'}\n\n"
        f"{time_context_lines}"
        "Web actions completed so far:\n"
        f"{query_lines}\n\n"
        "Retrieved evidence observed so far:\n"
        f"{continuation_evidence}\n\n"
        "Decide the next step now. "
        f"{next_step_text}"
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
        _request_context_module._request_metadata_dict(
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
    queries: list[str],
    completed_actions: Optional[list[dict[str, str]]] = None,
    round_number: int,
) -> dict[str, Any]:
    continuation_kwargs = _external_web_search_continuation_kwargs(
        request_kwargs,
        search_results=search_results,
        queries=queries,
        completed_actions=completed_actions,
        round_number=round_number,
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

    continuation_kwargs = _external_web_search_prepare_continuation_recovery_request(
        request_kwargs=request_kwargs,
        search_results=search_results,
        queries=queries,
        completed_actions=completed_actions,
        round_number=round_number,
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
            response_preview=_trace_module._sanitize_trace_text(_responses_output_module._response_text(continued)),
            next_queries=_web_search_queries_for_request(continued, request_kwargs),
            next_actions=_web_search_actions_for_request(continued, request_kwargs),
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
        response_preview=_trace_module._sanitize_trace_text(_responses_output_module._response_text(response)),
    )
    return await _external_web_search_synthesize_or_fallback(
        request_kwargs=request_kwargs,
        search_results=search_results,
        queries=queries,
        source_urls=source_urls,
        original_function=original_function,
    )


async def _resolve_web_search_function_calls(
    response: Any,
    request_kwargs: Optional[dict],
    original_function: Optional[Any] = None,
) -> Any:
    initial_actions = _web_search_actions_for_request(response, request_kwargs)
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
            _web_search_actions_for_request(current_response, request_kwargs),
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

    metadata = _request_context_module._request_metadata_dict(synthesis_kwargs, "litellm_metadata") or {}
    synthesis_metadata = metadata.copy()
    original_request = _external_web_search_user_prompt_text(request_kwargs)
    conversation_context = _external_web_search_conversation_context(request_kwargs)
    if original_request:
        synthesis_metadata[_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY] = original_request
    if conversation_context:
        synthesis_metadata[_EXTERNAL_WEB_SEARCH_CONVERSATION_CONTEXT_KEY] = (
            conversation_context
        )
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
        "the evidence is insufficient. Page-text matches and opened-page content "
        "are stronger evidence than search-result snippets; if sources conflict, "
        "do not make an absence claim from an omitted field or wrapper alone. The "
        "local pi-web-access bridge may differ from hosted OpenAI web_search "
        "ranking and snippets."
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
        _external_web_search_conversation_context_prefix(request_kwargs)
        + "Original user request. Any instruction to call or use web_search has "
        "already been satisfied by the compatibility bridge:\n"
        f"{original_request or '(no user text extracted)'}\n\n"
        f"{time_context_lines}"
        "Retrieved evidence:\n"
        f"{synthesis_evidence}\n\n"
        "Now answer the original user request directly. Do not call tools."
    )
    synthesis_kwargs["input"] = synthesis_input
    synthesis_kwargs.pop("messages", None)
    requested_output_tokens = _request_context_module._positive_int_value(
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
        metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key) or {}
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
        if _external_web_search_has_malformed_function_call(response):
            return "malformed_web_search_function_call"
        if (
            not _web_search_function_calls(response)
            and _external_web_search_has_completed_assistant_message(response)
        ):
            return None
        text = _responses_output_module._response_text(response)
        if text.strip():
            progress_reason = _external_web_search_progress_preamble_reason(text)
            if progress_reason is not None:
                return progress_reason
            return None
        if _web_search_function_calls(response):
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
    metadata = _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
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
