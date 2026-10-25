from __future__ import annotations

import copy
import html
import importlib
import inspect
import json
import os
import re
import sys
import time

from . import request_context as _request_context_module
from . import responses_output as _responses_output_module
from . import trace as _trace_module

from .base import (
    _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE,
    _JSONStreamEvent,
    _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES,
    _UPSTREAM_URL_SURFACE_KEY,
    _WEB_SEARCH_NATIVE_EVENT_SEEN_METADATA_KEY,
    Any, AsyncIterator, Optional,
)


class _LateModule:
    """Resolve a sibling proxy module per use, not at import time.

    ``routing``, ``streaming`` and ``tools`` import each other and name this
    module, so binding them in this module's import block closes those cycles
    into an ImportError; the closure below only touches them while serving a
    request, which is always after the package finished importing.

    Each access reads ``sys.modules`` instead of caching the module object: the
    hook test harness reloads the whole proxy package per test, and a cached
    pre-reload module would answer from the discarded instance.
    """

    def __init__(self, name: str) -> None:
        object.__setattr__(self, '_name', name)

    def _resolve(self) -> Any:
        name = object.__getattribute__(self, '_name')
        module = sys.modules.get(f'{__package__}.{name}')
        if module is None:
            module = importlib.import_module(f'.{name}', __package__)
        return module

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self._resolve(), attribute)


_routing_module = _LateModule('routing')
_streaming_module = _LateModule('streaming')
_tools_module = _LateModule('tools')
_responses_execution_module = _LateModule('responses_execution')
"""Provider-native hosted web-search adaptation.

The proxy does not execute web search.  What it still owns is shape: an
upstream that returns a hosted ``web_search_call`` (or encodes its search as a
private ``tco_*`` reasoning item) has to leave the proxy in a form the client
recognizes and can display.  This module is that conversion and nothing else --
it holds no executor, no round loop, and no evidence injection.

These are the client contract: the shapes a client can render, not bridge
bookkeeping.  No part of this module fetches, plans, or injects evidence.
"""

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
    mistaken for a search-bridge function call during post-processing.
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
            metadata.get(_RESPONSES_CHAT_BRIDGE_METADATA_KEY) is True
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

_RAW_TOOL_CALL_START = "<tool_call>"

_RAW_TOOL_CALL_END = "</tool_call>"

_RAW_TOOL_CALL_BLOCK_RE = re.compile(r"(?ims)^[ \t]*<tool_call>.*?</tool_call>")

_RAW_TOOL_CALL_TAIL_RE = re.compile(r"(?ims)^[ \t]*<tool_call>.*$")

_DSML_TOOL_CALL_START = "<\uff5c\uff5cDSML\uff5c\uff5c calls>"

_DSML_TOOL_CALL_END = "</\uff5c\uff5cDSML\uff5c\uff5c calls>"

_DSML_TOOL_CALL_BLOCK_RE = re.compile(
    r"(?ims)^[ \t]*<\uff5c\uff5cDSML\uff5c\uff5c\s+calls>.*?</\uff5c\uff5cDSML\uff5c\uff5c\s+calls>"
)

_DSML_TOOL_CALL_TAIL_RE = re.compile(
    r"(?ims)^[ \t]*<\uff5c\uff5cDSML\uff5c\uff5c\s+calls>.*$"
)

def _raw_tool_call_pending_prefix_len(text: str, marker: str) -> int:
    lower_text = text.lower()
    lower_marker = marker.lower()
    max_len = min(len(lower_text), len(lower_marker) - 1)
    for length in range(max_len, 0, -1):
        if lower_marker.startswith(lower_text[-length:]):
            return length
    return 0

def _strip_raw_tool_call_blocks(text: str) -> str:
    if not isinstance(text, str):
        return text
    # Code examples are content, not wire tool calls.
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", text)
    for index in range(0, len(parts), 2):
        segment = parts[index]
        cleaned = _RAW_TOOL_CALL_BLOCK_RE.sub("", segment)
        cleaned = _RAW_TOOL_CALL_TAIL_RE.sub("", cleaned)
        cleaned = _DSML_TOOL_CALL_BLOCK_RE.sub("", cleaned)
        cleaned = _DSML_TOOL_CALL_TAIL_RE.sub("", cleaned)
        if cleaned != segment and cleaned.strip() in {".", "。"}:
            cleaned = ""
        parts[index] = cleaned
    cleaned = "".join(parts)
    if cleaned == text:
        return text
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned)

def _raw_dsml_tool_call_items(text: str) -> list[dict[str, Any]]:
    # DSML delimiters may be split across streamed deltas, so callers feed a
    # whole accumulated block here. Fenced examples remain prose and are
    # excluded by the segment split; an orphan period before a real block is
    # removed by the streaming filter rather than emitted as visible output.
    if not isinstance(text, str):
        return []
    invoke_pattern = re.compile(
        r'(?is)<\uff5c\uff5cDSML\uff5c\uff5c\s+invoke\s+name="([^"]+)"\s*>(.*?)'
        r'</\uff5c\uff5cDSML\uff5c\uff5c\s+invoke\s*>'
    )
    parameter_pattern = re.compile(
        r'(?is)<\uff5c\uff5cDSML\uff5c\uff5c\s+parameter\s+name="([^"]+)"'
        r'(\s+[^>]*)?>(.*?)</\uff5c\uff5cDSML\uff5c\uff5c\s+parameter\s*>'
    )
    items: list[dict[str, Any]] = []
    segments = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", text)
    for segment in segments[::2]:
        for block in re.finditer(r"(?is)<\uff5c\uff5cDSML\uff5c\uff5c\s+calls>.*?</\uff5c\uff5cDSML\uff5c\uff5c\s+calls>", segment):
            for invoke in invoke_pattern.finditer(block.group(0)):
                name = html.unescape(invoke.group(1)).strip()
                if not name:
                    continue
                arguments: dict[str, Any] = {}
                for parameter in parameter_pattern.finditer(invoke.group(2)):
                    key = html.unescape(parameter.group(1)).strip()
                    value = html.unescape(parameter.group(3))
                    attributes = parameter.group(2) or ""
                    if re.search(r'\bstring="false"', attributes, re.IGNORECASE):
                        value = json.loads(value)
                    arguments[key] = value
                call_id = f"dsml_call_{time.time_ns()}"
                items.append({
                    "id": call_id,
                    "call_id": call_id,
                    "type": "function_call",
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                    "status": "completed",
                })
    return items

class _RawToolCallTextFilter:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._reset_segment()
        self.raw_tool_call_items: list[dict[str, Any]] = []

    def _reset_segment(self) -> None:
        self.buffer = ""
        self.end_marker: Optional[str] = None
        self.line_prefix = ""
        self.code_delimiter = ""
        self.hidden_block_parts: list[str] = []

    def finish_message(self, text: Optional[str] = None) -> None:
        # Some Responses-compatible providers emit the tool block in text
        # deltas but omit that text from response.completed. Keep the parsed
        # call until that terminal event so the tool protocol survives.
        if not self.raw_tool_call_items and isinstance(text, str):
            self.raw_tool_call_items.extend(_raw_dsml_tool_call_items(text))
        self._reset_segment()

    def take_raw_tool_call_items(self) -> list[dict[str, Any]]:
        items = self.raw_tool_call_items
        self.raw_tool_call_items = []
        self._reset_segment()
        return items

    def _visible(self, text: str) -> str:
        for match in re.finditer(r"`+", text):
            delimiter = match.group(0)
            if not self.code_delimiter:
                self.code_delimiter = delimiter
            elif delimiter == self.code_delimiter:
                self.code_delimiter = ""
        self.line_prefix = (self.line_prefix + text).rsplit("\n", 1)[-1]
        return text

    def _can_start(self, prefix: str) -> bool:
        delimiter = self.code_delimiter
        for match in re.finditer(r"`+", prefix):
            value = match.group(0)
            if not delimiter:
                delimiter = value
            elif delimiter == value:
                delimiter = ""
        return not delimiter and not (
            self.line_prefix + prefix
        ).rsplit("\n", 1)[-1].strip()

    def consume(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""
        self.buffer += text
        output: list[str] = []
        markers = (_RAW_TOOL_CALL_START, _DSML_TOOL_CALL_START)
        while self.buffer:
            lowered = self.buffer.lower()
            if self.end_marker:
                end_index = lowered.find(self.end_marker.lower())
                if end_index < 0:
                    keep = _raw_tool_call_pending_prefix_len(self.buffer, self.end_marker)
                    captured = self.buffer[:-keep] if keep else self.buffer
                    if captured:
                        self.hidden_block_parts.append(captured)
                    self.buffer = self.buffer[-keep:] if keep else ""
                    break
                block_text = (
                    "".join(self.hidden_block_parts)
                    + self.buffer[:end_index + len(self.end_marker)]
                )
                self.raw_tool_call_items.extend(
                    _raw_dsml_tool_call_items(block_text)
                )
                self.hidden_block_parts = []
                self.buffer = self.buffer[end_index + len(self.end_marker):]
                self.end_marker = None
                continue
            candidates = [
                (index, marker)
                for marker in markers
                if (index := lowered.find(marker.lower())) >= 0
                and self._can_start(self.buffer[:index])
            ]
            if candidates:
                index, marker = min(candidates, key=lambda value: value[0])
                prefix = self.buffer[:index]
                if prefix.strip() not in {".", "。"}:
                    output.append(self._visible(prefix))
                self.buffer = self.buffer[index:]
                self.hidden_block_parts = []
                self.end_marker = (
                    _DSML_TOOL_CALL_END if marker == _DSML_TOOL_CALL_START
                    else _RAW_TOOL_CALL_END
                )
                continue
            keep = max(
                (_raw_tool_call_pending_prefix_len(self.buffer, marker)
                 for marker in markers), default=0,
            )
            if keep and self._can_start(self.buffer[:-keep]):
                prefix = self.buffer[:-keep]
                if prefix.strip() not in {".", "。"}:
                    output.append(self._visible(prefix))
                    self.buffer = self.buffer[-keep:]
                break
            if not self.line_prefix.strip() and self.buffer.strip() in {".", "。"}:
                break
            output.append(self._visible(self.buffer))
            self.buffer = ""
        return "".join(output)

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

    raw_tool_calls: list[dict[str, Any]] = []
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        if not isinstance(payload.get("output"), list) or not payload["output"]:
            raw_tool_calls = _raw_dsml_tool_call_items(output_text)
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
            if isinstance(item, dict) and item.get("type") == "message":
                message_text = _responses_output_module._response_text(item)
                dsml_calls = _raw_dsml_tool_call_items(message_text)
                if dsml_calls:
                    clean_item, item_changed = _sanitize_message_raw_tool_calls(item)
                    if item_changed and _responses_output_module._response_text(clean_item).strip():
                        clean_output.append(clean_item)
                    raw_tool_calls.extend(dsml_calls)
                    changed = True
                    continue
            clean_item, item_changed = _sanitize_message_raw_tool_calls(item)
            clean_output.append(clean_item)
            changed = changed or item_changed
        if changed:
            payload = copy.deepcopy(payload)
            payload["output"] = clean_output
    if raw_tool_calls:
        payload = copy.deepcopy(payload)
        output = payload.setdefault("output", [])
        if not isinstance(output, list):
            output = []
            payload["output"] = output
        output.extend(raw_tool_calls)
        payload.pop("output_text", None)
        changed = True

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

# The summary values that state the opposite of a visibility request.  Every
# other named value is a client asking to see the thinking.
_REASONING_SUMMARY_OFF_VALUES = frozenset({"none", "off", "null", "false"})

def _requested_reasoning_summary(request_kwargs: Optional[dict]) -> Optional[str]:
    """The reasoning summary a request named, in whichever spelling carried it."""

    if not isinstance(request_kwargs, dict):
        return None
    reasoning = request_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        summary = reasoning.get("summary")
    else:
        # ``model_reasoning_summary`` is the Codex-shaped spelling of the same
        # request, and a ``reasoning`` string carries a level rather than one.
        summary = request_kwargs.get("model_reasoning_summary")
    if not isinstance(summary, str):
        return None
    normalized = summary.strip().casefold()
    return normalized or None

def _public_reasoning_is_allowed(request_kwargs: Optional[dict]) -> bool:
    """Whether this request asked to see the model's reasoning.

    ``reasoning.summary`` is that request, and every spelling of it counts:
    ``auto`` is what a client that wants thinking on screen sends by default
    (pi sends exactly that alongside ``effort``), ``concise``/``detailed`` ask
    for a shorter or longer one, and ``raw`` is this router's own name for the
    provider's unedited text.  All of them are visibility, so all of them
    deliver the reasoning summary frames and the item that owns them, whose
    text never enters the message item.

    Only an absent, ``null``, or off-valued summary means no reasoning at all.
    Reading the request as "raw only" hid every thinking block from a client
    that had asked for one: the frames were produced by the route and dropped
    here, which is silent rather than private.
    """

    summary = _requested_reasoning_summary(request_kwargs)
    return summary is not None and summary not in _REASONING_SUMMARY_OFF_VALUES

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

def _sanitize_response_reasoning_items(
    response: Any,
    request_kwargs: Optional[dict] = None,
) -> Any:
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
    allow_public_reasoning = _public_reasoning_is_allowed(request_kwargs)
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
            if (
                not allow_public_reasoning
                and isinstance(clean_reasoning_item, dict)
                and clean_reasoning_item.get("summary") not in (None, [], ())
            ):
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
        ),
        request_kwargs,
    )

def _sanitize_raw_tool_call_text_stream_chunk(
    chunk: Any,
    text_filter: Optional[_RawToolCallTextFilter] = None,
    request_data: Optional[dict] = None,
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
        text = dumped.get("text")
        if text_filter is not None:
            text_filter.finish_message(text if isinstance(text, str) else None)
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
        clean_response = _sanitize_response_stream_payload(
            response,
            request_data,
        )
        raw_tool_calls = (
            text_filter.take_raw_tool_call_items()
            if text_filter is not None
            else []
        )
        if raw_tool_calls:
            payload = _streaming_module._jsonable(clean_response)
            if isinstance(payload, dict):
                output = payload.get("output")
                output = list(output) if isinstance(output, list) else []
                existing_counts: dict[tuple[str, str], int] = {}
                for item in output:
                    if not isinstance(item, dict) or item.get("type") != "function_call":
                        continue
                    key = (str(item.get("name") or ""), str(item.get("arguments") or ""))
                    existing_counts[key] = existing_counts.get(key, 0) + 1
                for item in raw_tool_calls:
                    key = (str(item.get("name") or ""), str(item.get("arguments") or ""))
                    count = existing_counts.get(key, 0)
                    if count:
                        existing_counts[key] = count - 1
                    else:
                        output.append(item)
                payload["output"] = output
                if not _responses_output_module._response_text(payload).strip():
                    payload.pop("output_text", None)
                clean_response = payload
        if clean_response is response:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["response"] = clean_response
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    return chunk

def _sanitize_web_search_stream_chunk(
    chunk: Any,
    request_kwargs: Optional[dict] = None,
) -> Optional[Any]:
    dumped = _streaming_module._stream_chunk_dump(chunk)
    if not isinstance(dumped, dict) or not dumped:
        return chunk

    chunk_type = _streaming_module._stream_chunk_type(dumped)
    if _is_public_reasoning_summary_stream_event(chunk_type):
        return chunk if _public_reasoning_is_allowed(request_kwargs) else None
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
            if (
                not _public_reasoning_is_allowed(request_kwargs)
                and isinstance(clean_item, dict)
                and clean_item.get("summary") not in (None, [], ())
            ):
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
        clean_response = _sanitize_response_stream_payload(
            response,
            request_kwargs,
        )
        if clean_response is response:
            return chunk
        clean_chunk = copy.deepcopy(dumped)
        clean_chunk["response"] = clean_response
        return _streaming_module._json_stream_event(clean_chunk) if isinstance(chunk, _JSONStreamEvent) else clean_chunk

    return chunk

def _external_web_search_call_item_for_action(
    action: dict[str, Any],
    source_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    item_id = f"ws_bridge_{os.getpid()}_{time.time_ns()}"
    clean_action = copy.deepcopy(action)
    action_type = clean_action.get("type")
    if isinstance(action_type, str) and action_type in {"openPage", "findInPage"}:
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

_EXTERNAL_WEB_SEARCH_ORIGINAL_USER_TEXT_KEY = "external_web_search_original_user_text"

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

def _external_web_search_chat_only_route(request_kwargs: Optional[dict]) -> bool:
    model_info = _request_context_module._request_model_info(request_kwargs)
    surface = _routing_module._request_current_upstream_surface(request_kwargs)
    if not surface:
        surface = model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    if surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True
    return False

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
            if isinstance(item_type, str) and item_type in {"function_call", "custom_tool_call", "tool_call"}:
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
            if isinstance(value_type, str) and value_type in {"input_text", "output_text"}:
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

def _hosted_tool_unsupported_response(
    request_kwargs: Optional[dict],
    message: str,
) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    model = _routing_module._first_not_none(
        request_kwargs.get("model"),
        _responses_execution_module._request_model_group(request_kwargs),
        _routing_module._deployment_route_key_from_request(request_kwargs),
        "unknown",
    )
    response_id = f"resp_hosted_tool_unsupported_{os.getpid()}_{time.time_ns()}"
    message_id = f"msg_hosted_tool_unsupported_{time.time_ns()}"
    content = {
        "type": "output_text",
        "text": message,
        "annotations": [],
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output_text": message,
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [content],
            }
        ],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }

async def _hosted_tool_unsupported_stream(
    response: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    output_items = response.get("output")
    message = output_items[0] if isinstance(output_items, list) and output_items else {}
    content_items = message.get("content") if isinstance(message, dict) else None
    content = content_items[0] if isinstance(content_items, list) and content_items else {}
    message_id = message.get("id") if isinstance(message, dict) else None
    text = content.get("text") if isinstance(content, dict) else ""
    text = text if isinstance(text, str) else _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE
    if not text.strip():
        text = _responses_output_module._response_text(response)
    if not text.strip():
        text = _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE

    created_response = copy.deepcopy(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    yield encode({"type": "response.created", "response": created_response})
    yield encode({
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        },
    })
    yield encode({
        "type": "response.content_part.added",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })
    yield encode({
        "type": "response.output_text.delta",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    })
    yield encode({
        "type": "response.output_text.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "text": text,
    })
    yield encode({
        "type": "response.content_part.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": content,
    })
    yield encode({
        "type": "response.output_item.done",
        "output_index": 0,
        "item": message,
    })
    if isinstance(output_items, list):
        for output_index, output_item in enumerate(output_items[1:], start=1):
            item = _streaming_module._jsonable(output_item)
            if not isinstance(item, dict):
                continue
            added_item = copy.deepcopy(item)
            if added_item.get("status") == "completed":
                added_item["status"] = "in_progress"
            yield encode(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": added_item,
                }
            )
            done_item = copy.deepcopy(item)
            if done_item.get("status") == "in_progress":
                done_item["status"] = "completed"
            yield encode(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": done_item,
                }
            )
    yield encode({"type": "response.completed", "response": response})

async def _external_web_search_bridge_stream(
    response: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    response = _sanitize_response_web_search_call_items(response)
    output_items = response.get("output")
    output = output_items if isinstance(output_items, list) else []
    created_response = copy.deepcopy(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    yield encode({"type": "response.created", "response": created_response})
    sequence_number = 0
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            sanitized_item = _sanitize_web_search_call_item(item)
            if sanitized_item is None:
                continue
            item = sanitized_item
        if item.get("type") == "message":
            for event in _message_item_stream_events(item, index):
                yield encode(event)
            continue

        added_item = copy.deepcopy(item)
        if added_item.get("status") == "completed":
            added_item["status"] = "in_progress"
        yield encode({
            "type": "response.output_item.added",
            "output_index": index,
            "item": added_item,
        })
        if item.get("type") == "web_search_call":
            item_id = item.get("id")
            for event_type in (
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
                "response.web_search_call.completed",
            ):
                sequence_number += 1
                lifecycle_event = {
                    "type": event_type,
                    "item_id": item_id,
                    "output_index": index,
                    "sequence_number": sequence_number,
                }
                action = item.get("action")
                if isinstance(action, dict):
                    lifecycle_event["action"] = copy.deepcopy(action)
                yield encode(lifecycle_event)
        yield encode({
            "type": "response.output_item.done",
            "output_index": index,
            "item": item,
        })
    yield encode({"type": "response.completed", "response": response})

def _message_item_stream_events(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """Build the standard Responses events that stream one message item."""

    content_items = item.get("content")
    content = (
        content_items[0]
        if isinstance(content_items, list)
        and content_items
        and isinstance(content_items[0], dict)
        else {"type": "output_text", "text": "", "annotations": []}
    )
    text = content.get("text") if isinstance(content.get("text"), str) else ""
    added_message = copy.deepcopy(item)
    added_message["status"] = "in_progress"
    added_message["content"] = []
    events: list[dict[str, Any]] = [
        {
            "type": "response.output_item.added",
            "output_index": index,
            "item": added_message,
        },
        {
            "type": "response.content_part.added",
            "item_id": item.get("id"),
            "output_index": index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    ]
    if text:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "delta": text,
            }
        )
    events.extend(
        [
            {
                "type": "response.output_text.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "text": text,
            },
            {
                "type": "response.content_part.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "part": content,
            },
            {
                "type": "response.output_item.done",
                "output_index": index,
                "item": item,
            },
        ]
    )
    return events



def _trace_bridge_reasoning(
    request_kwargs: Optional[dict], *, saw: bool, delivered: bool, dropped: bool
) -> None:
    """Record whether a relayed Chat stream carried the thinking it received."""

    try:
        _trace_module._route_trace(
            "chat_bridge_reasoning",
            saw=saw,
            delivered=delivered,
            dropped=dropped,
            requested=_requested_reasoning_summary(request_kwargs),
            model_group=_request_context_module._request_model_group(request_kwargs),
        )
    except Exception:
        pass
