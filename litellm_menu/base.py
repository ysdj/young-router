from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import fcntl
import inspect
import io
import json
import logging
import os
import re
import threading
import time
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote, urlparse
import urllib.error
import urllib.request

import litellm
from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm.integrations.websearch_interception.handler import (
        WebSearchInterceptionLogger as _BaseWebSearchInterceptionLogger,
    )
    from litellm.integrations.websearch_interception.transformation import (
        WebSearchTransformation as _WebSearchTransformation,
    )
    from litellm.llms.base_llm.search.transformation import (
        SearchResponse as _SearchResponse,
        SearchResult as _SearchResult,
    )

    _WEB_SEARCH_INTERCEPTION_AVAILABLE = True
except Exception:
    _BaseWebSearchInterceptionLogger = CustomLogger
    _WebSearchTransformation = None
    _SearchResponse = None
    _SearchResult = None
    _WEB_SEARCH_INTERCEPTION_AVAILABLE = False


_STREAM_FALLBACK_METADATA_KEY = "litellm_image_generation_streaming_fallback_attempted"
_IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY = (
    "image_generation_tool_runtime_fallback_attempts"
)
_STREAM_ERROR_FALLBACK_METADATA_KEY = "streaming_error_fallback_attempted"
_ROUTE_RECOVERY_POLL_METADATA_KEY = "route_recovery_poll_attempt"
_STREAM_IDLE_TIMEOUT_METADATA_KEY = "stream_idle_timeout_triggered"
_STREAM_START_TIMEOUT_METADATA_KEY = "stream_start_timeout_triggered"
_RESPONSES_CHAT_BRIDGE_METADATA_KEY = "responses_chat_bridge_attempted"
_RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY = (
    "responses_chat_bridge_empty_retry_attempted"
)
_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY = (
    "responses_chat_bridge_original_model_group"
)
_RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY = (
    "responses_chat_bridge_fallback_reason"
)
_STREAM_FALLBACK_TEXT_FLUSH_CHARS = 160
_STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS = 20
_STALL_TIMEOUT_SECONDS_ENV = "LITELLM_MENU_STALL_TIMEOUT_SECONDS"
_STALL_TIMEOUT_DEFAULT_SECONDS = 120.0
_REQUEST_TIMEOUT_SECONDS_ENV = "LITELLM_MENU_REQUEST_TIMEOUT_SECONDS"
_REQUEST_TIMEOUT_DEFAULT_SECONDS = 7200.0
_IMAGE_GENERATION_TOOL_FALLBACK_MAX_ATTEMPTS_ENV = (
    "LITELLM_MENU_IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS"
)
_IMAGE_GENERATION_TOOL_FALLBACK_DEFAULT_MAX_ATTEMPTS = 3
_STREAM_ROUTE_EXHAUSTION_DEFAULT_RETRIES = 0
_STREAM_ROUTE_EXHAUSTION_RETRY_AFTER_MAX_SECONDS = 60.0
_DEPLOYMENT_COOLDOWN_FAILURES_ENV = "LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES"
_DEPLOYMENT_COOLDOWN_SECONDS_ENV = "LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS"
_DEPLOYMENT_COOLDOWN_FILE_ENV = "LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE"
_ROUTE_RECOVERY_STATE_FILE_ENV = "LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE"
_DEPLOYMENT_COOLDOWN_DEFAULT_FAILURES = 2
_DEPLOYMENT_COOLDOWN_DEFAULT_SECONDS = 300.0
_DEPLOYMENT_COOLDOWN_FAILURE_RECORDED_ATTR = (
    "_deployment_cooldown_failure_recorded"
)
_RECOVERY_MAX_SECONDS_ENV = "LITELLM_MENU_RECOVERY_MAX_SECONDS"
_RECOVERY_MAX_DEFAULT_SECONDS = 43200.0
_RECOVERY_INTERVAL_SECONDS_ENV = "LITELLM_MENU_RECOVERY_INTERVAL_SECONDS"
_RECOVERY_INTERVAL_DEFAULT_SECONDS = 5.0
_BROWSER_COMPATIBLE_HEADER_HOSTS = {"headers.example"}
_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY = (
    "_browser_compatible_headers_retry"
)
_RESPONSES_IMAGE_INPUT_SUPPORT_KEY = "supports_responses_image_input"
_UPSTREAM_URL_SURFACE_KEY = "upstream_url_surface"
_SUPPORTED_UPSTREAM_URL_SURFACES_KEY = "supported_upstream_url_surfaces"
_UPSTREAM_URL_SURFACE_OPENAI_RESPONSES = "openai/responses"
_UPSTREAM_URL_SURFACE_OPENAI_CHAT = "openai/chat"
_UPSTREAM_URL_SURFACE_ANTHROPIC = "anthropic"
_UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES = {
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
}
_UPSTREAM_URL_SURFACES = {
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
}
_CURRENT_UPSTREAM_URL_SURFACE_KEY = "_litellm_menu_upstream_url_surface"
_ATTEMPTED_UPSTREAM_URL_SURFACES_KEY = "_litellm_menu_attempted_upstream_url_surfaces"
_UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY = (
    "_litellm_menu_upstream_url_surface_deployment_id"
)
_SURFACE_TARGET_DEPLOYMENT_ID_KEY = "_litellm_menu_surface_target_deployment_id"
_RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY = (
    "responses_chat_bridge_preemptive"
)
_FALLBACK_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
_BROWSER_COMPATIBLE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
_UPSTREAM_METADATA_FORWARD_FLAGS = (
    "forward_metadata_to_upstream",
    "pass_through_metadata",
    "supports_request_metadata",
)
_SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY = "supports_responses_hosted_tools"
_SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY = "supports_responses_client_tools"
_SUPPORTS_RESPONSES_FUNCTION_TOOLS_KEY = "supports_responses_function_tools"
_SUPPORTS_RESPONSES_WEB_SEARCH_KEY = "supports_responses_web_search"
_SUPPORTS_WEB_SEARCH_KEY = "supports_web_search"
_XHIGH_REASONING_EFFORT = "xhigh"
_CHAT_COMPAT_REASONING_EFFORT = "high"
_MAX_COMPAT_REASONING_EFFORT = "max"
_XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY = (
    "xhigh_reasoning_compat_retry_attempted"
)
_INLINE_IMAGE_SINGLE_BUDGET_BYTES = 1_250_000
_INLINE_IMAGE_TOTAL_BUDGET_BYTES = 4_000_000
_INLINE_IMAGE_MANY_TARGET_BYTES = 320_000
_INLINE_IMAGE_SINGLE_TARGET_BYTES = 900_000
_INLINE_IMAGE_MANY_MAX_EDGE = 1400
_INLINE_IMAGE_SINGLE_MAX_EDGE = 2200
_GENERIC_HELPER_PATCH_ATTR = "_generic_deployment_failover_patch"
_ORDER_PEER_FAILOVER_PATCH_ATTR = "_order_peer_failover_patch"
_SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR = "_selected_deployment_marker_patch"
_SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR = "_sanitized_upstream_route_failure"
_SANITIZED_UPSTREAM_ROUTE_FAILURE_STATUS_CODE = 503
_ROUTING_CONSTRAINT_PATCH_ATTR = "_routing_constraint_patch"
_RESPONSES_COMPLETION_STREAM_PATCH_ATTR = "_responses_completion_stream_patch"
_RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR = (
    "_responses_completion_stream_default_done_patch"
)
_RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR = (
    "_responses_completion_stream_completed_patch"
)
_RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR = "_responses_tool_search_bridge_patch"
_RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY = (
    "responses_function_tool_bridge_attempted"
)
_RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY = (
    "responses_function_tool_bridge_preemptive"
)
_RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY = (
    "responses_function_tool_bridge_fallback_reason"
)
_RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY = (
    "responses_native_client_tool_passthrough"
)
_TOOL_SEARCH_BRIDGE_FUNCTION_NAME = "tool_search"
_WEB_SEARCH_BRIDGE_FUNCTION_NAME = "_litellm_web_search"
_RESPONSES_BRIDGE_NAMESPACE_KEY = "x-litellm-menu-responses-namespace"
_RESPONSES_BRIDGE_CUSTOM_TOOL_KEY = "x-litellm-menu-responses-custom-tool"
_WEB_SEARCH_EXTERNAL_BRIDGE_KEY = "external_web_search_bridge"
_WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY = "external_web_search_bridge_stream"
_WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY = "external_web_search_started"
_WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY = (
    "external_web_search_suppress_post_call"
)
_HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY = (
    "hosted_web_search_unsupported_bridge"
)
_HOSTED_TOOL_UNSUPPORTED_MESSAGE_KEY = "hosted_tool_unsupported_message"
_HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE = (
    "web_search is unavailable for this route."
)
_COMPUTER_FACADE_BACKEND_ENV = "LITELLM_MENU_COMPUTER_FACADE_BACKEND"
_COMPUTER_FACADE_MODEL_ENV = "LITELLM_MENU_COMPUTER_FACADE_MODEL"
_COMPUTER_FACADE_MAX_STEPS_ENV = "LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS"
_COMPUTER_FACADE_TRACE_ENV = "LITELLM_MENU_COMPUTER_FACADE_TRACE"
_COMPUTER_FACADE_TRACE_SCREENSHOTS_ENV = (
    "LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS"
)
_COMPUTER_FACADE_ACTION_DENYLIST_ENV = (
    "LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST"
)
_COMPUTER_FACADE_REQUIRE_OBSERVATION_ENV = (
    "LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION"
)
_COMPUTER_FACADE_PLANNER_METADATA_KEY = (
    "computer_facade_planner"
)
_COMPUTER_FACADE_EXECUTOR_METADATA_KEY = (
    "computer_facade_executor"
)
_COMPUTER_FACADE_AUTO_BACKEND = "auto"
_COMPUTER_FACADE_MCP_BACKEND = "mcp"
_COMPUTER_FACADE_BROWSER_BACKEND = "browser"
_COMPUTER_FACADE_CHROME_BACKEND = "chrome"
_COMPUTER_FACADE_PLAYWRIGHT_BACKEND = "playwright"
_COMPUTER_FACADE_CUA_BACKEND = "cua"
_COMPUTER_FACADE_MOCK_BACKEND = "mock"
_COMPUTER_FACADE_BACKENDS = {
    _COMPUTER_FACADE_AUTO_BACKEND,
    _COMPUTER_FACADE_MCP_BACKEND,
    _COMPUTER_FACADE_BROWSER_BACKEND,
    _COMPUTER_FACADE_CHROME_BACKEND,
    _COMPUTER_FACADE_PLAYWRIGHT_BACKEND,
    _COMPUTER_FACADE_CUA_BACKEND,
    _COMPUTER_FACADE_MOCK_BACKEND,
}
_COMPUTER_FACADE_DEFAULT_MAX_STEPS = 20
_COMPUTER_FACADE_SAFE_FAILURE_MESSAGE = (
    "computer-use backend is unavailable for this route."
)
_COMPUTER_FACADE_MOCK_DONE_MESSAGE = (
    "computer facade mock completed after screenshot observation."
)
_ROUTE_TRACE_ENV = "LITELLM_MENU_ROUTE_TRACE"
_ROUTE_TRACE_STATE_FILE_ENV = "LITELLM_ROUTE_TRACE_STATE_FILE"
_ROUTE_TRACE_PREVIEW_CHARS_ENV = "LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS"
_ROUTE_TRACE_PREVIEW_DEFAULT_CHARS = 2000
_ROUTE_TRACE_PREVIEW_MAX_CHARS = 2000
_ROUTE_TRACE_LIST_SCAN_ITEMS = 40
_RECENT_REQUESTS_LOG_ENV = "LITELLM_RECENT_REQUESTS_LOG"
_RECENT_REQUESTS_MAX_BYTES_ENV = "LITELLM_MENU_LOG_MAX_BYTES"
_EXTERNAL_WEB_SEARCH_MAX_RESULTS_ENV = "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS"
_EXTERNAL_WEB_SEARCH_READ_RESULTS_ENV = "LITELLM_MENU_WEB_SEARCH_READ_RESULTS"
_EXTERNAL_WEB_SEARCH_READ_CHARS_ENV = "LITELLM_MENU_WEB_SEARCH_READ_CHARS"
_EXTERNAL_WEB_FETCH_TIMEOUT_ENV = "LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS"
_EXTERNAL_WEB_SEARCH_MAX_ROUNDS_ENV = "LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS"
_EXTERNAL_WEB_SEARCH_MAX_QUERIES_ENV = "LITELLM_MENU_WEB_SEARCH_MAX_QUERIES"
_EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_ENV = "LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES"
_EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_ENV = "LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE"
_EXTERNAL_WEB_SEARCH_REGION_ENV = "LITELLM_MENU_WEB_SEARCH_REGION"
_EXTERNAL_WEB_SEARCH_BACKEND_ENV = "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND"
_VISION_BRIDGE_BACKEND_ENV = "LITELLM_MENU_VISION_BRIDGE_BACKEND"
_VISION_BRIDGE_MODE_ENV = "LITELLM_MENU_VISION_BRIDGE_MODE"
_VISION_BRIDGE_API_BASE_ENV = "LITELLM_MENU_VISION_BRIDGE_API_BASE"
_VISION_BRIDGE_API_KEY_ENV = "LITELLM_MENU_VISION_BRIDGE_API_KEY"
_VISION_BRIDGE_MODEL_ENV = "LITELLM_MENU_VISION_BRIDGE_MODEL"
_VISION_BRIDGE_TIMEOUT_ENV = "LITELLM_MENU_VISION_BRIDGE_TIMEOUT_SECONDS"
_VISION_BRIDGE_PROMPT_ENV = "LITELLM_MENU_VISION_BRIDGE_PROMPT"
_VISION_BRIDGE_LOCAL_FORMAT_ENV = "LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT"
_EXTERNAL_WEB_SEARCH_MAX_RESULTS_DEFAULT = 8
_EXTERNAL_WEB_SEARCH_READ_RESULTS_DEFAULT = 4
_EXTERNAL_WEB_SEARCH_READ_CHARS_DEFAULT = 1400
_EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT = 30.0
_EXTERNAL_WEB_SEARCH_MAX_ROUNDS_DEFAULT = 6
_EXTERNAL_WEB_SEARCH_MAX_QUERIES_DEFAULT = 16
_EXTERNAL_WEB_SEARCH_MAX_OPEN_PAGES_DEFAULT = 8
_EXTERNAL_WEB_SEARCH_MAX_FIND_IN_PAGE_DEFAULT = 12
_EXTERNAL_WEB_SEARCH_REGION_DEFAULT = "us-en"
_EXTERNAL_WEB_SEARCH_BACKEND_DEFAULT = "auto"
_VISION_BRIDGE_BACKEND_DEFAULT = "auto"
_VISION_BRIDGE_MODE_DEFAULT = "auto"
_VISION_BRIDGE_API_BASE_DEFAULT = "http://127.0.0.1:11434/v1"
_VISION_BRIDGE_MODEL_DEFAULT = "qwen2.5vl:3b"
_VISION_BRIDGE_TIMEOUT_DEFAULT = 45.0
_VISION_BRIDGE_PROMPT_DEFAULT = (
    "Describe the image accurately for a text-only language model. "
    "Include visible text, UI elements, layout, objects, and any important details."
)
_VISION_BRIDGE_LOCAL_FORMAT_DEFAULT = "compact"
_RECENT_REQUESTS_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_RECENT_REQUESTS_MIN_MAX_BYTES = 256 * 1024
_INTERNAL_CONTEXT_PREFIXES = (
    "another language model started to solve this problem",
    "<codex_internal_context",
    "<environment_context>",
    "<permissions instructions>",
    "<app-context>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<plugins_instructions>",
    "<skill>",
    "<subagent_notification>",
)
_SESSION_ID_KEY_FRAGMENTS = (
    "session_id",
    "thread_id",
    "conversation_id",
    "chat_id",
)
_SESSION_NAME_KEY_FRAGMENTS = (
    "session_name",
    "session_title",
    "thread_name",
    "thread_title",
    "conversation_name",
    "conversation_title",
    "chat_name",
    "chat_title",
)
_ROUTE_TRACE_LOGGER = logging.getLogger("litellm_menu.route_trace")
_CURRENT_SELECTED_DEPLOYMENT = contextvars.ContextVar(
    "current_selected_deployment",
    default=None,
)
_CURRENT_SELECTED_DEPLOYMENT_BOX = contextvars.ContextVar(
    "current_selected_deployment_box",
    default=None,
)
_CURRENT_EXCLUDED_DEPLOYMENT_IDS = contextvars.ContextVar(
    "current_excluded_deployment_ids",
    default=None,
)
_CURRENT_SURFACE_TARGET_DEPLOYMENT_ID = contextvars.ContextVar(
    "current_surface_target_deployment_id",
    default=None,
)
_CURRENT_DEPLOYMENT_COOLDOWN_SURFACE = contextvars.ContextVar(
    "current_deployment_cooldown_surface",
    default=None,
)
_OMIT_RESPONSE_VALUE = object()
_DEPLOYMENT_COOLDOWN_LOCK = threading.Lock()
_DEPLOYMENT_COOLDOWNS: dict[str, dict[str, Any]] = {}
_EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_LOCK = threading.Lock()
_EXTERNAL_WEB_SEARCH_STARTED_REQUESTS: dict[str, float] = {}
_EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_MAX = 512
_EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_TTL_SECONDS = 3600.0


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _int_or_zero(value: Any) -> int:
    converted = _int_or_none(value)
    return converted if converted is not None else 0


def _object_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _responses_usage_for_codex(usage: Any) -> Optional[dict[str, Any]]:
    if usage is None:
        return None
    input_tokens = _int_or_none(_object_get(usage, "input_tokens"))
    if input_tokens is None:
        input_tokens = _int_or_zero(_object_get(usage, "prompt_tokens"))
    output_tokens = _int_or_none(_object_get(usage, "output_tokens"))
    if output_tokens is None:
        output_tokens = _int_or_zero(_object_get(usage, "completion_tokens"))
    total_tokens = _int_or_none(_object_get(usage, "total_tokens"))
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens

    input_details = _object_get(usage, "input_tokens_details")
    if input_details is None:
        input_details = _object_get(usage, "prompt_tokens_details")
    output_details = _object_get(usage, "output_tokens_details")
    if output_details is None:
        output_details = _object_get(usage, "completion_tokens_details")

    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": _int_or_zero(_object_get(input_details, "cached_tokens")),
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": _int_or_zero(_object_get(output_details, "reasoning_tokens")),
        },
        "total_tokens": total_tokens,
    }


def _normalize_response_completed_event_usage(event: Any) -> Any:
    response = _object_get(event, "response")
    usage = _object_get(response, "usage")
    normalized_usage = _responses_usage_for_codex(usage)
    if normalized_usage is None:
        return event
    if isinstance(response, dict):
        response["usage"] = normalized_usage
    elif response is not None:
        try:
            setattr(response, "usage", normalized_usage)
        except Exception:
            pass
    return event


class _JSONStreamEvent(dict):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _normalize_response_completed_event_usage(self)

    def __str__(self) -> str:
        return json.dumps(self, ensure_ascii=False)


class _RouteRecoveryStreamResponse:
    def __init__(self, request_data: dict, exception: Exception) -> None:
        self.request_data = request_data
        self.exception = exception

    async def __aiter__(self) -> AsyncIterator[Any]:
        from . import streaming as streaming_module

        async for chunk in streaming_module._stream_route_recovery_poll(self.request_data, self.exception):
            yield chunk


class _TerminalFailedResponsesStreamResponse:
    def __init__(self, request_data: dict, exception: Exception) -> None:
        self.request_data = request_data
        self.exception = exception

    async def __aiter__(self) -> AsyncIterator[Any]:
        from . import streaming as streaming_module

        yield streaming_module._synthesized_failed_response_event(self.request_data, self.exception)


_UPSTREAM_BALANCE_ERROR_MARKERS = (
    "insufficient_balance",
    "insufficient account balance",
    "insufficient balance",
    "account balance is insufficient",
    "not enough credits",
    "insufficient credits",
    "credit balance",
    "out of credits",
    "quota exceeded",
    "insufficient_quota",
    "insufficient quota",
    "余额不足",
)
_UPSTREAM_TEMPORARY_ERROR_MARKERS = (
    "high demand",
    "temporarily unavailable",
    "temporary unavailable",
    "overloaded",
    "selected model is at capacity",
    "model is at capacity",
    "server is at capacity",
    "service is at capacity",
    "capacity reached",
    "concurrency limit",
    "rate limit",
    "retry later",
    "try again later",
    "too many requests",
)
_UPSTREAM_HTML_BAD_REQUEST_MARKERS = (
    "<html",
    "400 bad request",
    "nginx",
)
_LITELLM_MODEL_GROUP_FALLBACK_EXHAUSTED_MARKERS = (
    "received model group=",
    "available model group fallbacks=none",
)
_UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES = {
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
    "Timeout",
    "TimeoutError",
}
_CHAT_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HOSTED_BROWSER_COMPUTER_TOOL_TYPES = {"computer"}
_BROWSER_COMPUTER_CLIENT_NAMESPACE_NAMES = {
    "browser",
    "browser_use",
    "chrome",
    "chrome_browser",
    "mcp__browser",
    "mcp__browser_use",
    "mcp__chrome",
    "mcp__computer_use",
}
_BROWSER_COMPUTER_CLIENT_FUNCTION_NAMES = {
    "click",
    "drag",
    "get_app_state",
    "list_apps",
    "perform_secondary_action",
    "press_key",
    "scroll",
    "select_text",
    "set_value",
    "type_text",
}
_RESPONSES_STREAM_COMPLETED_TYPES = {"response.completed"}
_RESPONSES_STREAM_INCOMPLETE_TYPES = {
    "response.failed",
    "response.incomplete",
    "response.cancelled",
}
_RESPONSES_STREAM_INCOMPLETE_STATUSES = {"failed", "incomplete", "cancelled"}
_HOSTED_WEB_SEARCH_TOOL_TYPES = {"web_search", "web_search_preview"}
_HOSTED_GA_COMPUTER_TOOL_TYPES = {"computer"}


@dataclass(frozen=True)
class HostedToolPlan:
    hosted_web_search: bool = False
    hosted_web_search_preview: bool = False
    hosted_computer: bool = False
    client_namespaces: list[str] = field(default_factory=list)
    client_functions: list[str] = field(default_factory=list)
    passthrough_tools: list[dict] = field(default_factory=list)
    facade_required: bool = False
    unsupported_reason: Optional[str] = None
    hosted_computer_tools: list[dict] = field(default_factory=list)
    computer_environment: Optional[dict] = None
    available_executor_hints: list[str] = field(default_factory=list)


@dataclass
class ComputerObservation:
    type: str
    image_url: Optional[str] = None
    text: Optional[str] = None
    detail: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    backend: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComputerAction:
    type: str
    x: Optional[int] = None
    y: Optional[int] = None
    button: Optional[str] = None
    text: Optional[str] = None
    keys: Optional[list[str]] = None
    dx: Optional[int] = None
    dy: Optional[int] = None
    scroll_x: Optional[int] = None
    scroll_y: Optional[int] = None
    duration_ms: Optional[int] = None
    message: Optional[str] = None
