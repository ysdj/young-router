from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from pathlib import Path

from .. import api_base as _api_base_module
from . import codex_fast_tier as _codex_fast_tier_module
from . import context_window as _context_window_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_output as _responses_output_module
from . import web_search_native as _web_search_native_module
from . import reasoning as _reasoning_module
from . import reasoning_replay as _reasoning_replay_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import trace as _trace_module
from ..adapters import dsh_vision_router as _dsh_vision_router_module
from . import proxy_idle as _proxy_idle_module


from .base import (
    Any,
    List,
    Optional,
    _BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID,
    _CURRENT_SELECTED_DEPLOYMENT,
    _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS,
    _CURRENT_ROUTING_REQUEST_KWARGS,
    _GENERIC_HELPER_PATCH_ATTR,
    _ORDER_PEER_FAILOVER_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_REASONING_END_PATCH_ATTR,
    _RESPONSES_COMPLETION_STREAM_TRANSFORM_PATCH_ATTR,
    _RESPONSES_CHAT_BRIDGE_REASONING_PATCH_ATTR,
    _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
    _ROUTING_CONSTRAINT_PATCH_ATTR,
    _RESTORED_REASONING_REPLAY_KEY,
    _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _UPSTREAM_URL_SURFACE_KEY,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _install_websocket_frame_limit_patch,
    _install_uvicorn_shutdown_marker_patch,
    _normalize_response_completed_event_usage,
)


_ANTHROPIC_UNVERSIONED_ENDPOINT_PATCH_ATTR = (
    "_young_router_anthropic_unversioned_endpoint_patch"
)
# LiteLLM treats these top-level request keys as caller-supplied credentials and
# pins them on whatever deployment it selects next
# (``get_dynamic_litellm_params``); a fallback hop may only present the next
# deployment's own params.
_REQUEST_SCOPED_CREDENTIAL_KEYS = frozenset({"api_key"})
_LATIN1_RESPONSE_HEADERS_PATCH_ATTR = (
    "_young_router_latin1_response_headers_patch"
)
_RESPONSES_COMPLETION_STREAM_OUTPUT_ITEM_PATCH_ATTR = (
    "_young_router_responses_completion_stream_output_item_patch"
)
#: LiteLLM's Chat-to-Responses iterator builds its reasoning item from a delta
#: that carries only ``reasoning_content``, so the ``response.output_item.added``
#: frame it queues states ``id``/``type``/``status`` and no ``summary`` at all,
#: and it never queues the ``response.reasoning_summary_part.added`` frame that
#: opens summary index 0.  A client reads both as what it needs before it can
#: render the thinking: the Codex desktop app drops summary deltas whose item
#: announced no ``summary`` array, so the whole block vanished from a route that
#: had streamed it, and the same frames landed under the answer instead.
#: ``_open_reasoning_summary_items`` restates both beside the item the stock
#: iterator queues.
_RESPONSES_WEBSOCKET_HTTP_BRIDGE_PATCH_ATTR = (
    "_young_router_responses_websocket_http_bridge_patch"
)
_STREAM_CHUNK_RESTAMP_PATCH_ATTR = "_young_router_stream_chunk_restamp_patch"


def _install_stream_chunk_restamp_patch() -> None:
    """Stamp the client-facing model without flattening a chunk's wire form.

    ``async_data_generator`` writes a delivered chunk as ``f"data: {chunk}"``,
    so a plain dict reaches the client as a Python repr and no SSE JSON frame
    at all.  Young Router's Responses events are a dict subclass whose
    ``__str__`` is that JSON frame.  LiteLLM 1.106.0 stamps the requested
    model onto a dict chunk by rebuilding it as ``{**chunk, "model": ...}``,
    which drops the subclass: every event then arrived unparseable and Codex
    reported ``stream closed before response.completed``.  1.105.1 mutated the
    same object and kept the class.  Keep the class on the copy this version
    makes, so the frame stays parseable for every client of the Responses API.
    """

    try:
        from litellm.proxy import proxy_server as _litellm_proxy_server
    except Exception:
        return

    original_restamp = getattr(
        _litellm_proxy_server,
        "_restamp_streaming_chunk_model",
        None,
    )
    if not callable(original_restamp) or getattr(
        original_restamp,
        _STREAM_CHUNK_RESTAMP_PATCH_ATTR,
        False,
    ):
        return

    def patched_restamp_streaming_chunk_model(
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        chunk = kwargs.get("chunk", args[0] if args else None)
        stamped, model_mismatch_logged = original_restamp(*args, **kwargs)
        if (
            stamped is not chunk
            and isinstance(chunk, dict)
            and type(chunk) is not dict
            and type(stamped) is dict
        ):
            try:
                stamped = type(chunk)(stamped)
            except Exception:
                pass
        return stamped, model_mismatch_logged

    setattr(
        patched_restamp_streaming_chunk_model,
        _STREAM_CHUNK_RESTAMP_PATCH_ATTR,
        True,
    )
    setattr(
        patched_restamp_streaming_chunk_model,
        "_original_restamp_streaming_chunk_model",
        original_restamp,
    )
    _litellm_proxy_server._restamp_streaming_chunk_model = (
        patched_restamp_streaming_chunk_model
    )


_STREAM_CHUNK_BUILDER_DICT_FUNCTION_CALL_PATCH_ATTR = (
    "_young_router_stream_chunk_builder_dict_function_call_patch"
)


def _install_stream_chunk_builder_dict_function_call_patch() -> None:
    """Read a dict-shaped legacy ``function_call`` in the streaming usage builder.

    ``stream_chunk_builder`` assembles the final response from the chunks it
    saw so LiteLLM can price and log the turn.  Its
    ``get_combined_function_call_content`` reads ``function_call.name`` and
    ``.arguments`` as attributes, but the legacy ``function_call`` field
    arrives as a plain dict from OpenAI-compatible relays.  That
    ``AttributeError`` is re-raised as a 500 ("Error building chunks for
    logging/streaming usage calculation") *while the answer is streaming*, so
    a delivered answer is cut at whatever token the logging pass reached.
    Observed live 2026-11-02 on flux-code's Chat stream: ten cuts in an hour,
    every one after visible output.

    The replacement keeps LiteLLM's shape and only reads both spellings.
    """

    try:
        from litellm.litellm_core_utils import streaming_chunk_builder_utils
    except Exception:
        return
    processor = getattr(streaming_chunk_builder_utils, "ChunkProcessor", None)
    original = getattr(processor, "get_combined_function_call_content", None)
    if not callable(original) or getattr(
        original,
        _STREAM_CHUNK_BUILDER_DICT_FUNCTION_CALL_PATCH_ATTR,
        False,
    ):
        return

    def patched_get_combined_function_call_content(
        self: Any,
        function_call_chunks: Any,
    ) -> Any:
        argument_list: list[str] = []
        first = function_call_chunks[0]["choices"][0]["delta"].get("function_call", "")
        name = first.get("name") if isinstance(first, dict) else getattr(first, "name", "")
        for chunk in function_call_chunks:
            for choice in chunk.get("choices") or ():
                function_call = (choice.get("delta") or {}).get("function_call")
                if not function_call:
                    continue
                arguments = (
                    function_call.get("arguments")
                    if isinstance(function_call, dict)
                    else getattr(function_call, "arguments", "")
                )
                argument_list.append(arguments or "")
        return streaming_chunk_builder_utils.FunctionCall(
            name=name or "",
            arguments="".join(argument_list),
        )

    setattr(
        patched_get_combined_function_call_content,
        _STREAM_CHUNK_BUILDER_DICT_FUNCTION_CALL_PATCH_ATTR,
        True,
    )
    setattr(
        patched_get_combined_function_call_content,
        "_original_get_combined_function_call_content",
        original,
    )
    processor.get_combined_function_call_content = (
        patched_get_combined_function_call_content
    )


def _install_responses_websocket_http_bridge_patch() -> None:
    """Serve Responses WebSocket clients through the managed HTTP bridge.

    LiteLLM's native Responses WebSocket mode dials ``wss://<api_base>``
    directly for providers whose config reports native WebSocket support
    (OpenAI, Azure).  Relay deployments expose the OpenAI Responses HTTP
    endpoint on hosts that reject the WebSocket upgrade with 404, so the
    client's WebSocket is closed before ``response.completed`` and Codex
    enters its reconnect loop.  Reporting no native support routes the
    connection through ``ManagedResponsesWebSocketHandler``, which streams
    over the working HTTP endpoint instead.
    """

    try:
        from litellm.llms.base_llm.responses.transformation import (
            BaseResponsesAPIConfig,
        )
    except Exception:
        return

    def patched_supports_native_websocket(self: Any) -> bool:
        return False

    setattr(
        patched_supports_native_websocket,
        _RESPONSES_WEBSOCKET_HTTP_BRIDGE_PATCH_ATTR,
        True,
    )
    BaseResponsesAPIConfig.supports_native_websocket = patched_supports_native_websocket
    for module_name, class_name in (
        ("litellm.llms.openai.responses.transformation", "OpenAIResponsesAPIConfig"),
        ("litellm.llms.azure.responses.transformation", "AzureOpenAIResponsesAPIConfig"),
    ):
        try:
            import importlib

            module = importlib.import_module(module_name)
            provider_config = getattr(module, class_name, None)
        except Exception:
            provider_config = None
        if provider_config is not None:
            provider_config.supports_native_websocket = patched_supports_native_websocket


def _install_latin1_response_headers_patch() -> None:
    try:
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing,
        )
    except Exception:
        return

    original_get_custom_headers = getattr(
        ProxyBaseLLMRequestProcessing,
        "get_custom_headers",
        None,
    )
    if not callable(original_get_custom_headers) or getattr(
        original_get_custom_headers,
        _LATIN1_RESPONSE_HEADERS_PATCH_ATTR,
        False,
    ):
        return

    def patched_get_custom_headers(*args: Any, **kwargs: Any) -> Any:
        headers = original_get_custom_headers(*args, **kwargs)
        if not isinstance(headers, dict):
            return headers
        compatible_headers: dict[Any, Any] = {}
        for key, value in headers.items():
            try:
                str(key).encode("latin-1")
                str(value).encode("latin-1")
            except UnicodeEncodeError:
                continue
            compatible_headers[key] = value
        return compatible_headers

    setattr(
        patched_get_custom_headers,
        _LATIN1_RESPONSE_HEADERS_PATCH_ATTR,
        True,
    )
    setattr(
        patched_get_custom_headers,
        "_original_get_custom_headers",
        original_get_custom_headers,
    )
    ProxyBaseLLMRequestProcessing.get_custom_headers = staticmethod(
        patched_get_custom_headers
    )


def _install_anthropic_unversioned_endpoint_patch() -> None:
    try:
        from litellm import main as litellm_main
    except Exception:
        return

    original_complete = getattr(litellm_main, "_complete_anthropic", None)
    original_get_secret_bool = getattr(litellm_main, "get_secret_bool", None)
    if (
        not callable(original_complete)
        or not callable(original_get_secret_bool)
        or getattr(original_complete, _ANTHROPIC_UNVERSIONED_ENDPOINT_PATCH_ATTR, False)
    ):
        return

    skip_suffix = ContextVar("young_router_skip_anthropic_url_suffix", default=False)

    def patched_get_secret_bool(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "LITELLM_ANTHROPIC_DISABLE_URL_SUFFIX" and skip_suffix.get():
            return True
        return original_get_secret_bool(name, *args, **kwargs)

    def patched_complete(ctx: Any) -> Any:
        if not _api_base_module.is_unversioned_anthropic_messages_endpoint(
            getattr(ctx, "api_base", None)
        ):
            return original_complete(ctx)
        token = skip_suffix.set(True)
        try:
            return original_complete(ctx)
        finally:
            skip_suffix.reset(token)

    setattr(
        patched_complete,
        _ANTHROPIC_UNVERSIONED_ENDPOINT_PATCH_ATTR,
        True,
    )
    setattr(patched_complete, "_original_complete_anthropic", original_complete)
    litellm_main.get_secret_bool = patched_get_secret_bool
    litellm_main._complete_anthropic = patched_complete


def _enable_anthropic_prompt_caching() -> None:
    """Let LiteLLM mark the stable Claude prefix when the client cannot.

    Anthropic caches nothing unless the request carries ``cache_control``
    breakpoints, and only the native Messages surface can state them: a Codex
    ``/v1/responses`` turn carries no such field, so every turn of a thread
    reached a Claude route as fresh input.  Measured 2026-11-05 against this
    network's ``claude-opus-5-5`` route: the same ~7.4k-token prompt written
    through ``/v1/messages`` created a 7,360-token cache and read it back on
    the next turn, while three identical ``/v1/responses`` turns reported
    ``cache_write_tokens: 0`` and ``cached_tokens: 0`` each -- and the relay's
    own usage log billed all 42,658 input tokens of a Codex turn at full price.

    LiteLLM's switch adds the two breakpoints it documents (the system prompt
    and the trailing turn) to Claude models on an explicit-cache transport,
    and stands down when the request already carries client breakpoints, so a
    client that marks its own -- Pi's ``/v1/messages`` caching -- keeps its TTL
    and its four-block budget untouched.
    """

    try:
        import litellm
    except Exception:
        return
    litellm.enable_anthropic_prompt_caching = True


def _browser_compatible_headers_retry_kwargs(
    request_kwargs: dict,
) -> Optional[dict]:
    retry_kwargs = _responses_request_module._with_browser_compatible_headers_retry(request_kwargs)
    if retry_kwargs is None:
        return None
    header_kwargs = _responses_request_module._with_browser_compatible_headers(retry_kwargs)
    return header_kwargs or retry_kwargs


def _browser_compatible_headers_retry_entry(
    model_group: Optional[str],
    exception: Exception,
    request_kwargs: dict,
) -> Optional[dict]:
    model = _responses_execution_module._request_model_group(request_kwargs) or model_group
    if not isinstance(model, str) or not model.strip():
        return None
    entry: dict[str, Any] = {"model": model}
    target_order = request_kwargs.get("_target_order")
    if target_order is None:
        target_order = _responses_execution_module._failed_deployment_order(exception)
    if target_order is not None:
        entry["_target_order"] = target_order
    excluded_ids = sorted(_responses_request_module._request_excluded_deployment_ids(request_kwargs))
    if excluded_ids:
        entry["_excluded_deployment_ids"] = excluded_ids
    return entry


def _trace_codex_fast_tier_injected(request_kwargs: dict) -> None:
    requested_tier = _codex_fast_tier_module._codex_fast_default_service_tier(
        request_kwargs
    )
    if requested_tier is None:
        return
    _trace_module._route_trace(
        "codex_fast_default_service_tier_injected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        codex_fast_default_injected=True,
        service_tier=requested_tier,
        requested_service_tier=requested_tier,
        source="codex_config_fast_default",
    )



def _request_kwargs_from_positional_call(
    args: tuple,
    kwargs: dict,
    *,
    positional_index: int,
) -> Optional[dict]:
    request_kwargs = kwargs.get("request_kwargs")
    if isinstance(request_kwargs, dict):
        return request_kwargs
    if len(args) > positional_index and isinstance(args[positional_index], dict):
        return args[positional_index]
    return None


def _request_is_external_web_search_internal_call(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = request_kwargs.get(metadata_key)
        if not isinstance(metadata, dict):
            continue
        if (
            metadata.get("external_web_search_continuation") is True
            or metadata.get("external_web_search_synthesis") is True
        ):
            return True
    return False


def _request_local_verified_fallback_deployment(
    router: Any,
    model: Any,
    request_kwargs: Optional[dict],
    *,
    verified_ids: set[str],
    target_order: Any,
) -> Optional[dict]:
    if not verified_ids or not isinstance(model, str) or not model.strip():
        return None
    request_kwargs = request_kwargs if isinstance(request_kwargs, dict) else {}
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs,
        "metadata",
    ) or {}
    try:
        configured = _routing_module._router_configured_deployments(
            router,
            model,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return None

    constraints = request_kwargs.copy()
    if target_order is not None:
        constraints["_target_order"] = target_order
    constraints[_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY] = sorted(verified_ids)
    candidates = _responses_request_module._with_retry_target_constraints(
        list(configured or []),
        constraints,
    )
    candidates, cooldown_deployments, cooldown_filtered = (
        _routing_module._with_active_deployment_cooldowns(
            candidates,
            request_kwargs=constraints,
        )
    )
    if not candidates:
        return None
    deployment = candidates[0]
    _trace_module._route_trace(
        "request_local_verified_fallback_selected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=model,
        target_order=target_order,
        verified_deployment_ids=sorted(verified_ids),
        deployment=_routing_module._trace_deployment(deployment),
        cooldown_filtered=cooldown_filtered,
        cooldown_deployments=cooldown_deployments,
        request=_trace_module._trace_request_summary(request_kwargs),
    )
    return deployment


def _request_local_cooling_only_candidate_deployment(
    router: Any,
    model: Any,
    request_kwargs: Optional[dict],
    *,
    target_order: Any,
) -> Optional[dict]:
    """Hand a fresh request the least-cooling route when the pool is all cooling.

    A model group whose every candidate is quarantined has no route at all, so
    LiteLLM answers ``no deployments available`` for a group that is plainly
    configured -- the failure mode the cooldown must never produce.  The route
    whose cooldown ends first is the one that has waited longest, so it is the
    one handed back; the request then either succeeds or fails over through the
    ordinary peer machinery.  Only a request that is neither a recovery poll
    nor a fallback attempt reaches here, so the deliberate cooldown wait and
    the ordered peer chain keep their own semantics.
    """

    if not isinstance(model, str) or not model.strip():
        return None
    request_kwargs = request_kwargs if isinstance(request_kwargs, dict) else {}
    if _routing_module._is_route_recovery_poll_payload(request_kwargs):
        return None
    if _responses_request_module._request_is_fallback_attempt(request_kwargs):
        return None
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs,
        "metadata",
    ) or {}
    try:
        configured = _routing_module._router_configured_deployments(
            router,
            model,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return None

    constraints = request_kwargs.copy()
    if target_order is not None:
        constraints["_target_order"] = target_order
    candidates = _responses_request_module._with_retry_target_constraints(
        list(configured or []),
        constraints,
    )
    if not candidates:
        return None
    available, cooldown_deployments, cooldown_filtered = (
        _routing_module._with_active_deployment_cooldowns(
            candidates,
            request_kwargs=constraints,
        )
    )
    if available or not cooldown_filtered:
        return None

    deployment = min(
        candidates,
        key=lambda item: _cooldown_until_for_deployment(item, cooldown_deployments),
    )
    _trace_module._route_trace(
        "request_local_cooling_only_candidate_selected",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=model,
        target_order=target_order,
        deployment=_routing_module._trace_deployment(deployment),
        cooling_pool_size=len(candidates),
        cooldown_deployments=cooldown_deployments,
        request=_trace_module._trace_request_summary(request_kwargs),
    )
    return deployment


def _cooldown_until_for_deployment(
    deployment: dict,
    cooldown_deployments: list[dict[str, Any]],
) -> float:
    """When a deployment's own cooldown ends, for choosing the longest-waiting route.

    A deployment matches on more than one key (its id and its route), so the
    earliest end wins: that is the moment this route becomes eligible again.
    """

    until_by_key = {
        str(entry.get("cooldown_key", "")): float(entry.get("cooldown_until") or 0.0)
        for entry in cooldown_deployments
        if isinstance(entry, dict)
    }
    ends = [
        until_by_key[cooldown_key]
        for cooldown_key in _routing_module._deployment_cooldown_keys_from_deployment(deployment)
        if cooldown_key in until_by_key
    ]
    return min(ends) if ends else float("inf")


def _install_routing_constraint_patch() -> None:
    try:
        from litellm.router import Router
    except Exception:
        return

    original_get_all_deployments = getattr(Router, "_get_all_deployments", None)
    if original_get_all_deployments is not None and not getattr(
        original_get_all_deployments,
        _ROUTING_CONSTRAINT_PATCH_ATTR,
        False,
    ):

        def patched_get_all_deployments(self: Any, *args: Any, **kwargs: Any) -> Any:
            deployments = original_get_all_deployments(self, *args, **kwargs)
            if not isinstance(deployments, list):
                return deployments
            constrained = deployments
            excluded_ids = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.get()
            if excluded_ids:
                constrained = [
                    deployment
                    for deployment in constrained
                    if _responses_request_module._deployment_id(deployment) not in excluded_ids
                ]
            verified_ids = _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.get()
            if verified_ids:
                constrained = [
                    deployment
                    for deployment in constrained
                    if _responses_request_module._deployment_id(deployment)
                    in verified_ids
                ]
            target_deployment_id = _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.get()
            if target_deployment_id:
                constrained = [
                    deployment
                    for deployment in constrained
                    if _responses_request_module._deployment_id(deployment)
                    == target_deployment_id
                ]
            constrained, _cooldown_deployments, _cooldown_filtered = (
                _routing_module._with_active_deployment_cooldowns(
                    constrained,
                    request_kwargs=_CURRENT_ROUTING_REQUEST_KWARGS.get(),
                )
            )
            return constrained

        setattr(patched_get_all_deployments, _ROUTING_CONSTRAINT_PATCH_ATTR, True)
        setattr(
            patched_get_all_deployments,
            "_original_get_all_deployments",
            original_get_all_deployments,
        )
        Router._get_all_deployments = patched_get_all_deployments

    original_get_available_deployment = getattr(Router, "get_available_deployment", None)
    if original_get_available_deployment is not None and not getattr(
        original_get_available_deployment,
        _ROUTING_CONSTRAINT_PATCH_ATTR,
        False,
    ):

        def patched_get_available_deployment(self: Any, *args: Any, **kwargs: Any) -> Any:
            request_kwargs = _request_kwargs_from_positional_call(
                args,
                kwargs,
                positional_index=4,
            )
            excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_kwargs)
            verified_ids = _responses_request_module._request_verified_fallback_deployment_ids(
                request_kwargs
            )
            target_order = _responses_request_module._request_target_order(request_kwargs)
            excluded_token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids or None)
            verified_token = _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.set(
                verified_ids or None
            )
            target_token = _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.set(
                _routing_module._request_surface_target_deployment_id(request_kwargs)
            )
            request_token = _CURRENT_ROUTING_REQUEST_KWARGS.set(request_kwargs)
            try:
                try:
                    return original_get_available_deployment(self, *args, **kwargs)
                except Exception as exc:
                    if not _routing_module._is_no_deployments_available_error(exc):
                        raise
                    model = kwargs.get("model")
                    if model is None and args:
                        model = args[0]
                    deployment = _request_local_verified_fallback_deployment(
                        self,
                        model,
                        request_kwargs,
                        verified_ids=verified_ids,
                        target_order=target_order,
                    )
                    if deployment is None and not verified_ids:
                        deployment = _request_local_cooling_only_candidate_deployment(
                            self,
                            model,
                            request_kwargs,
                            target_order=target_order,
                        )
                    if deployment is None:
                        raise
                    return deployment
            finally:
                if isinstance(request_kwargs, dict):
                    request_kwargs.pop(_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY, None)
                _CURRENT_ROUTING_REQUEST_KWARGS.reset(request_token)
                _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.reset(target_token)
                _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.reset(verified_token)
                _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(excluded_token)

        setattr(patched_get_available_deployment, _ROUTING_CONSTRAINT_PATCH_ATTR, True)
        setattr(
            patched_get_available_deployment,
            "_original_get_available_deployment",
            original_get_available_deployment,
        )
        Router.get_available_deployment = patched_get_available_deployment

    original_async_get_available_deployment = getattr(
        Router,
        "async_get_available_deployment",
        None,
    )
    if original_async_get_available_deployment is None or getattr(
        original_async_get_available_deployment,
        _ROUTING_CONSTRAINT_PATCH_ATTR,
        False,
    ):
        return

    async def patched_async_get_available_deployment(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        request_kwargs = _request_kwargs_from_positional_call(
            args,
            kwargs,
            positional_index=1,
        )
        excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_kwargs)
        verified_ids = _responses_request_module._request_verified_fallback_deployment_ids(
            request_kwargs
        )
        target_order = _responses_request_module._request_target_order(request_kwargs)
        excluded_token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids or None)
        verified_token = _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.set(
            verified_ids or None
        )
        target_token = _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.set(
            _routing_module._request_surface_target_deployment_id(request_kwargs)
        )
        request_token = _CURRENT_ROUTING_REQUEST_KWARGS.set(request_kwargs)
        try:
            try:
                return await original_async_get_available_deployment(
                    self,
                    *args,
                    **kwargs,
                )
            except Exception as exc:
                if not _routing_module._is_no_deployments_available_error(exc):
                    raise
                model = kwargs.get("model")
                if model is None and args:
                    model = args[0]
                deployment = _request_local_verified_fallback_deployment(
                    self,
                    model,
                    request_kwargs,
                    verified_ids=verified_ids,
                    target_order=target_order,
                )
                if deployment is None and not verified_ids:
                    deployment = _request_local_cooling_only_candidate_deployment(
                        self,
                        model,
                        request_kwargs,
                        target_order=target_order,
                    )
                if deployment is None:
                    raise
                return deployment
        finally:
            if isinstance(request_kwargs, dict):
                request_kwargs.pop(_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY, None)
            _CURRENT_ROUTING_REQUEST_KWARGS.reset(request_token)
            _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID.reset(target_token)
            _CURRENT_VERIFIED_FALLBACK_DEPLOYMENT_IDS.reset(verified_token)
            _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(excluded_token)

    setattr(patched_async_get_available_deployment, _ROUTING_CONSTRAINT_PATCH_ATTR, True)
    setattr(
        patched_async_get_available_deployment,
        "_original_async_get_available_deployment",
        original_async_get_available_deployment,
    )
    Router.async_get_available_deployment = patched_async_get_available_deployment


async def _call_deployment_with_reasoning_parameters_compat(
    original_make_call: Any,
    router: Any,
    original_function: Any,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Call one deployment, replaying it with a compatible reasoning shape.

    Some OpenAI-chat gateways convert a client reasoning request through their
    own per-model thinking table and refuse a model name they cannot map (a
    relay alias, for example a prefixed spelling of a Gemini model).  A
    Gemini-family upstream still honors the native Google thinking
    configuration, so that replay keeps the client's requested level; every
    other route falls back to the same completion without reasoning fields and
    keeps its provider default thinking behavior.  The successful shape is
    recorded so later requests are normalized before the upstream call.
    """

    try:
        return await original_make_call(router, original_function, *args, **kwargs)
    except Exception as exc:
        candidates = (
            _reasoning_module._reasoning_parameters_compat_retry_candidates(
                exc,
                kwargs,
            )
        )
        if not candidates:
            raise
        last_exc = exc
        for mode, retry_kwargs in candidates:
            _trace_module._route_trace(
                "reasoning_parameters_compat_retry_start",
                request_id=_routing_module._trace_request_id(retry_kwargs),
                session=_routing_module._trace_session_context(retry_kwargs),
                model_group=_responses_execution_module._request_model_group(
                    retry_kwargs
                ),
                deployment_id=_routing_module._deployment_id_from_request(
                    retry_kwargs
                ),
                route_key=_routing_module._deployment_route_key_from_request(
                    retry_kwargs
                ),
                mode=mode,
                exception=_routing_module._trace_exception(exc),
            )
            # The caller keeps one kwargs dict for the attempt, so each replay
            # must replace its reasoning fields in place rather than shadow
            # them; the candidate kwargs were built from the original request.
            kwargs.clear()
            kwargs.update(retry_kwargs)
            try:
                response = await original_make_call(
                    router,
                    original_function,
                    *args,
                    **kwargs,
                )
            except Exception as retry_exc:
                last_exc = retry_exc
                _trace_module._route_trace(
                    "reasoning_parameters_compat_retry_error",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(
                        kwargs
                    ),
                    deployment_id=_routing_module._deployment_id_from_request(
                        kwargs
                    ),
                    route_key=_routing_module._deployment_route_key_from_request(
                        kwargs
                    ),
                    mode=mode,
                    original_exception=_routing_module._trace_exception(exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                continue
            _routing_module._record_reasoning_parameters_compat(kwargs, mode)
            return response
        raise last_exc


def _install_selected_deployment_marker_patch() -> None:
    try:
        from litellm.router import Router
    except Exception:
        return

    original_update_kwargs_with_deployment = getattr(
        Router,
        "_update_kwargs_with_deployment",
        None,
    )
    if original_update_kwargs_with_deployment is not None and not getattr(
        original_update_kwargs_with_deployment,
        _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
        False,
    ):

        # LiteLLM keeps adding optional switches to this method -- 1.107 added
        # ``splits_stream_timeout`` for the Responses handlers. Whatever this
        # wrapper does not interpret must reach the original: a pinned
        # signature raises ``TypeError`` inside deployment selection, before
        # the route is called, so every request on that handler answers as a
        # terminal route failure with no upstream attempt behind it.
        def patched_update_kwargs_with_deployment(
            self: Any,
            deployment: dict,
            kwargs: dict,
            function_name: Optional[str] = None,
            **upstream_kwargs: Any,
        ) -> None:
            _responses_execution_module._remember_request_model_group_before_deployment_update(kwargs)
            # The route is about to be called, which is the moment its
            # concurrency cap applies: the candidate filter already skipped a
            # full route when a peer was free, so reaching here with a full cap
            # means every candidate of the group is full.  A turn that is turned
            # away here is refused by this proxy with a reason of its own -- it
            # is never retried against the same full pool by a caller that reads
            # the refusal as an upstream rate limit.
            concurrency_cap = _routing_module._model_concurrency_cap(deployment)
            if concurrency_cap > 0 and not _routing_module._claim_deployment_concurrency_slot(
                kwargs,
                deployment,
                concurrency_cap,
            ):
                raise _routing_module._model_concurrency_rejection_exception(
                    kwargs,
                    deployment,
                )
            surface = _routing_module._request_surface_for_deployment(kwargs, deployment)
            # Read the client's replay before the surface adapter rewrites it:
            # ``input`` becomes ``messages`` for a Chat-shaped route, and the
            # answer that comes back then covers the whole restored context.
            restored_reasoning_replay = surface in {
                _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
                _UPSTREAM_URL_SURFACE_ANTHROPIC,
            } and _responses_execution_module._chat_bridge_reasoning_included(kwargs)
            # ``make_call`` receives its kwargs by value, so the surface this
            # attempt was adapted to travels under the request id instead.
            _routing_module._remember_request_adapted_surface(kwargs, surface)
            if not surface:
                _routing_module._remember_selected_deployment(deployment)
                force_browser_headers = _responses_request_module._request_forces_browser_compatible_headers(kwargs)
                result = original_update_kwargs_with_deployment(
                    self,
                    deployment,
                    kwargs,
                    function_name=function_name,
                    **upstream_kwargs,
                )
                _routing_module._remember_selected_deployment_for_request(kwargs, deployment)
                if force_browser_headers:
                    kwargs[_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True
                browser_kwargs = _responses_request_module._with_browser_compatible_headers(kwargs)
                if browser_kwargs is not None:
                    kwargs.clear()
                    kwargs.update(browser_kwargs)
                _routing_module._strip_image_generation_internal_request_keys(
                    kwargs,
                    function_name,
                )
                return result
            deployment_id = _responses_request_module._deployment_id(deployment)
            attempted_surfaces = (
                _routing_module._request_attempted_upstream_surfaces(kwargs)
                if deployment_id
                == _routing_module._request_surface_deployment_id(kwargs)
                else []
            )
            if surface not in attempted_surfaces:
                attempted_surfaces.append(surface)
            _routing_module._set_request_surface_state(
                kwargs,
                surface=surface,
                attempted_surfaces=attempted_surfaces,
                deployment_id=deployment_id,
                target_deployment_id=_routing_module._request_surface_target_deployment_id(kwargs),
            )
            _routing_module._remember_selected_deployment(
                deployment,
                surface=surface,
            )
            force_browser_headers = _responses_request_module._request_forces_browser_compatible_headers(kwargs)
            image_request = _routing_module._request_is_standalone_image_generation(
                kwargs,
                function_name,
            )
            trace_deployment = deployment
            if image_request and isinstance(deployment.get("model_info"), dict):
                trace_deployment = deployment.copy()
                trace_deployment["model_info"] = {
                    key: value
                    for key, value in deployment["model_info"].items()
                    if key != _UPSTREAM_URL_SURFACE_KEY
                }
            request_summary = _trace_module._trace_request_summary(
                kwargs,
                call_type=function_name,
                method_name=function_name,
            )
            _trace_module._route_trace(
                "selected_deployment",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_responses_execution_module._request_model_group(kwargs),
                function_name=function_name,
                deployment=_routing_module._trace_deployment(
                    trace_deployment,
                    surface="" if image_request else surface,
                ),
                request=request_summary,
                target_order=_responses_request_module._request_target_order(kwargs),
                excluded_deployment_ids=sorted(_responses_request_module._request_excluded_deployment_ids(kwargs)),
            )
            result = original_update_kwargs_with_deployment(
                self,
                deployment,
                kwargs,
                function_name=function_name,
                **upstream_kwargs,
            )
            _routing_module._remember_selected_deployment_for_request(kwargs, deployment)
            _routing_module._apply_surface_adapter_to_request(
                kwargs,
                surface,
                deployment.get("litellm_params", {}).get("model"),
                function_name=function_name,
            )
            if restored_reasoning_replay:
                kwargs[_RESTORED_REASONING_REPLAY_KEY] = True
            if force_browser_headers:
                kwargs[_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True

            from .responses_request import _with_browser_compatible_headers

            browser_kwargs = _with_browser_compatible_headers(kwargs)
            if browser_kwargs is not None:
                kwargs.clear()
                kwargs.update(browser_kwargs)
            _routing_module._strip_image_generation_internal_request_keys(
                kwargs,
                function_name,
            )
            return result

        setattr(
            patched_update_kwargs_with_deployment,
            _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
            True,
        )
        setattr(
            patched_update_kwargs_with_deployment,
            "_original_update_kwargs_with_deployment",
            original_update_kwargs_with_deployment,
        )
        Router._update_kwargs_with_deployment = patched_update_kwargs_with_deployment

    original_make_call = getattr(Router, "make_call", None)
    if original_make_call is None or getattr(
        original_make_call,
        _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR,
        False,
    ):
        return

    async def patched_make_call(self: Any, original_function: Any, *args: Any, **kwargs: Any) -> Any:
        _routing_module._strip_image_generation_internal_request_keys(
            kwargs,
            getattr(original_function, "__name__", None),
        )
        token = _CURRENT_SELECTED_DEPLOYMENT.set(None)
        try:
            response = await _reasoning_replay_module.call_with_replay_repair(
                lambda: _call_deployment_with_reasoning_parameters_compat(
                    original_make_call,
                    self,
                    original_function,
                    args,
                    kwargs,
                ),
                kwargs,
            )
            marker = _CURRENT_SELECTED_DEPLOYMENT.get()
            if _routing_module._is_failed_responses_stream_response(response):
                failed_request = getattr(response, "request_data", None)
                if not isinstance(failed_request, dict):
                    failed_request = kwargs
                if marker is not None:
                    _routing_module._apply_selected_deployment_marker_to_request(
                        failed_request,
                        marker,
                    )
                if _routing_module._protocol_fallback_attempt_active(
                    failed_request,
                ):
                    _routing_module._mark_exception_for_deployment_failover(
                        response.exception,
                        failed_request,
                    )
                    raise response.exception
                if marker is not None and _routing_module._is_current_upstream_surface_incompatible_error(
                    response.exception,
                    failed_request,
                ):
                    _routing_module._clear_protocol_fallback_cache_for_request(
                        failed_request,
                        preserve_relaxed_tool_choice=True,
                    )
                    _routing_module._mark_exception_for_upstream_surface_failover(
                        response.exception,
                        failed_request,
                    )
                    raise response.exception
                return response
            if marker is not None:
                # LiteLLM invokes the streaming-iterator hook after make_call
                # returns.  Persist the selected route on the request object
                # before recording fallback success and before the context
                # marker is reset, so an initial stream failure can exclude
                # the route that actually failed and the cache records the
                # protocol that was really selected.
                _routing_module._apply_selected_deployment_marker_to_request(
                    kwargs,
                    marker,
                )
                _routing_module._record_protocol_fallback_success(kwargs)
                response = _routing_module._wrap_response_with_selected_deployment_marker(
                    response,
                    marker,
                )
            else:
                _routing_module._record_protocol_fallback_success(kwargs)
            # The answer a Chat-shaped deployment produces covers the client's
            # whole context, whatever route inside this call converted it: the
            # local bridge, or LiteLLM's own protocol fallback onto the
            # deployment's Chat surface.  The fact travels on the response
            # object because Codex reads it from the streaming HTTP header,
            # which LiteLLM assembles from this very object.
            response = _responses_execution_module._with_restored_reasoning_marker(
                response,
                kwargs,
            )
            return response
        except Exception as exc:
            marker = _CURRENT_SELECTED_DEPLOYMENT.get()
            if marker is not None:
                _routing_module._apply_selected_deployment_marker_to_request(
                    kwargs,
                    marker,
                )
            if (
                marker is not None
                and _routing_module._protocol_fallback_attempt_active(kwargs)
            ):
                _routing_module._mark_exception_for_deployment_failover(exc, kwargs)
            elif marker is not None and _routing_module._is_current_upstream_surface_incompatible_error(
                exc,
                kwargs,
            ):
                _routing_module._clear_protocol_fallback_cache_for_request(
                    kwargs,
                    preserve_relaxed_tool_choice=True,
                )
                _routing_module._mark_exception_for_upstream_surface_failover(
                    exc,
                    kwargs,
                )
            elif (
                marker is not None
                and _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    kwargs,
                )
                and not _routing_module._should_retry_with_browser_compatible_headers(exc, marker)
            ):
                _routing_module._mark_exception_for_deployment_failover(exc, marker)
            raise
        finally:
            _CURRENT_SELECTED_DEPLOYMENT.reset(token)

    setattr(patched_make_call, _SELECTED_DEPLOYMENT_MARKER_PATCH_ATTR, True)
    setattr(patched_make_call, "_original_make_call", original_make_call)
    Router.make_call = patched_make_call


def _next_deployment_kwargs(kwargs: dict) -> dict:
    """Hand the next deployment its own credentials, not the failed route's.

    LiteLLM reads a top-level ``api_key`` as a caller-supplied credential and
    pins it on every deployment it selects next (``get_dynamic_litellm_params``).
    The kwargs that reach a fallback still carry the *failed* deployment's
    request-scoped value, so the next hop authenticated with the previous
    route's key: observed as the local worker's token being presented to three
    unrelated stations, each answering HTTP 401 while its own configured key
    was valid, and the turn ending on "upstream auth or balance".  ``api_base``
    stays, because it is the surface adaptation this router applies per
    deployment rather than a credential.
    """

    return {
        key: value
        for key, value in kwargs.items()
        if key not in _REQUEST_SCOPED_CREDENTIAL_KEYS
    }


def _stream_for_a_streaming_client(response: Any, request_kwargs: dict) -> Any:
    """Never hand LiteLLM a buffered Responses object for a streaming request.

    Litellm's stream pipeline wraps whatever the route call returns and calls
    ``__aiter__`` on it; a bridge answer that stayed buffered (observed: a
    web-search round ending on the client's own tool call) crashed that wrapper
    with ``'dict' object has no attribute '__aiter__'``.  The client saw a
    broken stream and re-sent the identical turn, so the thread looked stuck.
    The payload leaves as ordinary Responses SSE instead.
    """

    if request_kwargs.get("stream") is not True:
        return response
    if _responses_output_module._response_is_async_iterable(response):
        return response
    if not isinstance(response, dict):
        return response
    if "output" not in response and "output_text" not in response:
        return response
    # Trace the conversion: without it the only evidence a buffered answer was
    # repaired is the absence of the crash it used to cause.
    _trace_module._route_trace(
        "responses_buffered_answer_streamed_for_client",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        response_type=type(response).__name__,
        output_items=len(response.get("output") or []),
    )
    return _streaming_module._non_streaming_response_as_stream(
        response,
        request_kwargs,
    )


def _install_order_peer_failover_patch() -> None:
    try:
        from litellm.router import Router
        from litellm.router_utils.fallback_event_handlers import run_async_fallback
    except Exception:
        return

    original_common_utils = getattr(Router, "async_function_with_fallbacks_common_utils", None)
    if original_common_utils is None or getattr(
        original_common_utils,
        _ORDER_PEER_FAILOVER_PATCH_ATTR,
        False,
    ):
        return

    async def patched_common_utils(
        self: Any,
        e: Exception,
        disable_fallbacks: Optional[bool],
        fallbacks: Optional[List],
        context_window_fallbacks: Optional[List],
        content_policy_fallbacks: Optional[List],
        model_group: Optional[str],
        args: tuple,
        kwargs: dict,
        include_fallback_errors: bool = False,
    ) -> Any:
        if _routing_module._is_terminal_prompt_or_policy_error(e):
            _trace_module._route_trace(
                "terminal_error_fallback_suppressed",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_responses_execution_module._request_model_group(kwargs)
                or model_group,
                request=_trace_module._trace_request_summary(kwargs),
                exception=_routing_module._trace_exception(e),
            )
            raise e
        browser_retry_kwargs = None
        if _routing_module._should_retry_with_browser_compatible_headers(e, kwargs):
            browser_retry_kwargs = _browser_compatible_headers_retry_kwargs(kwargs)
        if browser_retry_kwargs is not None and disable_fallbacks is not True:
            browser_retry_entry = _browser_compatible_headers_retry_entry(
                model_group,
                e,
                browser_retry_kwargs,
            )
            if browser_retry_entry is not None:
                peer_kwargs = {
                    "litellm_router": self,
                    "original_exception": e,
                    **_next_deployment_kwargs(browser_retry_kwargs),
                }
                peer_kwargs.setdefault("max_fallbacks", getattr(self, "max_fallbacks", 0))
                peer_kwargs.setdefault("fallback_depth", 0)
                if include_fallback_errors:
                    peer_kwargs["include_fallback_errors"] = True
                peer_kwargs.update(
                    {
                        "fallback_model_group": [browser_retry_entry],
                        "original_model_group": _responses_execution_module._request_model_group(kwargs)
                        or model_group,
                    }
                )
                _trace_module._route_trace(
                    "browser_compatible_headers_retry_start",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(kwargs)
                    or model_group,
                    peer_entry=browser_retry_entry,
                    request=_trace_module._trace_request_summary(browser_retry_kwargs),
                    exception=_routing_module._trace_exception(e),
                )
                return await run_async_fallback(*args, **peer_kwargs)
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            e,
            kwargs,
        ):
            # The attempt that held this route's slot is over.  A concurrency
            # refusal never took one, so releasing is a no-op for it; a real
            # failure frees the slot now instead of waiting for the lease to age
            # out, so the peer this attempt advances to starts from a true count.
            _routing_module._release_request_concurrency_slots(kwargs)
            _routing_module._mark_same_deployment_retry_exhausted(e)
            _routing_module._mark_exception_for_deployment_failover(e, kwargs)
        _routing_module._sync_failed_deployment_exclusions(kwargs, e)
        if disable_fallbacks is not True:
            peer_entry = _responses_execution_module._ordered_deployment_fallback_entry(self, e, kwargs)
            if peer_entry is not None:
                peer_kwargs = {
                    "litellm_router": self,
                    "original_exception": e,
                    **_next_deployment_kwargs(kwargs),
                }
                peer_kwargs.setdefault("max_fallbacks", getattr(self, "max_fallbacks", 0))
                peer_kwargs.setdefault("fallback_depth", 0)
                if include_fallback_errors:
                    peer_kwargs["include_fallback_errors"] = True
                peer_kwargs.update(
                    {
                        "fallback_model_group": [peer_entry],
                        "original_model_group": _responses_execution_module._request_model_group(kwargs) or model_group,
                    }
                )
                _trace_module._route_trace(
                    (
                        "same_order_peer_fallback_start"
                        if peer_entry.get("_target_order") == _responses_execution_module._failed_deployment_order(e)
                        else "next_order_fallback_start"
                    ),
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(kwargs) or model_group,
                    peer_entry=peer_entry,
                    request=_trace_module._trace_request_summary(kwargs),
                    exception=_routing_module._trace_exception(e),
                )
                return await run_async_fallback(*args, **peer_kwargs)

        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            e,
            kwargs,
        ):
            _trace_module._route_trace(
                "ordered_deployment_fallback_exhausted",
                request_id=_routing_module._trace_request_id(kwargs),
                session=_routing_module._trace_session_context(kwargs),
                model_group=_responses_execution_module._request_model_group(kwargs)
                or model_group,
                target_order=kwargs.get("_target_order"),
                excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                request=_trace_module._trace_request_summary(kwargs),
                exception=_routing_module._trace_exception(e),
            )
            decision_kwargs = _responses_execution_module._request_kwargs_with_model_group(
                model_group,
                kwargs,
            )
            if _routing_module._should_return_route_recovery_stream(
                e,
                decision_kwargs,
                self,
            ):
                _trace_module._route_trace(
                    "route_recovery_stream_returned",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=_responses_execution_module._request_model_group(kwargs)
                    or model_group,
                    target_order=kwargs.get("_target_order"),
                    excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                    exception=_routing_module._trace_exception(e),
                )
                recovery_stream_kwargs = decision_kwargs.copy()
                recovery_stream_kwargs["_route_recovery_ignore_local_constraints"] = True
                return _routing_module._route_recovery_stream_response(
                    recovery_stream_kwargs,
                    e,
                )
            if _routing_module._should_sanitize_final_upstream_route_error(e):
                _routing_module._raise_sanitized_upstream_route_failure(
                    _responses_execution_module._request_model_group(kwargs)
                    or model_group,
                    e,
                    kwargs,
                )
            raise e

        _trace_module._route_trace(
            "litellm_model_group_fallback_suppressed",
            request_id=_routing_module._trace_request_id(kwargs),
            session=_routing_module._trace_session_context(kwargs),
            model_group=_responses_execution_module._request_model_group(kwargs) or model_group,
            target_order=kwargs.get("_target_order"),
            excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
            request=_trace_module._trace_request_summary(kwargs),
            exception=_routing_module._trace_exception(e),
        )
        # Model identity is part of the request contract.  The Menu handles
        # same-model peer/order failover above; never hand LiteLLM a configured
        # model-group, client, context-window, or content-policy fallback that
        # could silently change the selected model.
        original_args = (
            self,
            e,
            disable_fallbacks,
            [],
            [],
            [],
            model_group,
            args,
            kwargs,
        )
        if include_fallback_errors:
            return await original_common_utils(
                *original_args,
                include_fallback_errors=True,
            )
        return await original_common_utils(*original_args)

    setattr(patched_common_utils, _ORDER_PEER_FAILOVER_PATCH_ATTR, True)
    setattr(patched_common_utils, "_original_common_utils", original_common_utils)
    Router.async_function_with_fallbacks_common_utils = patched_common_utils


def _install_generic_deployment_failover_patch() -> None:
    try:
        from litellm.router import Router
    except Exception:
        return

    original_helper = getattr(Router, "_ageneric_api_call_with_fallbacks_helper", None)
    if original_helper is None or getattr(original_helper, _GENERIC_HELPER_PATCH_ATTR, False):
        return

    async def patched_generic_helper(
        self: Any,
        model: str,
        original_generic_function: Any,
        **kwargs: Any,
    ) -> Any:
        for update_request in (
            _codex_fast_tier_module._with_codex_fast_default_service_tier,
            *_responses_request_module._CODEX_REQUEST_INSTRUCTION_STEPS,
        ):
            updated_kwargs = update_request(kwargs)
            if updated_kwargs is not None:
                kwargs = updated_kwargs
                if update_request is _codex_fast_tier_module._with_codex_fast_default_service_tier:
                    _trace_codex_fast_tier_injected(kwargs)
        target_order = kwargs.get("_target_order")
        excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
        external_web_search_internal = _request_is_external_web_search_internal_call(kwargs)
        max_retries = 0 if external_web_search_internal else _routing_module._same_deployment_retries()
        retry_delay_seconds = _routing_module._stream_route_exhaustion_retry_delay_seconds()
        retry_attempt = 0
        _trace_module._route_trace(
            "generic_fallback_helper_start",
            request_id=_routing_module._trace_request_id(kwargs),
            session=_routing_module._trace_session_context(kwargs),
            model_group=model,
            target_order=target_order,
            excluded_deployment_ids=excluded_deployment_ids,
            request=_trace_module._trace_request_summary(
                kwargs,
                method_name=_trace_module._trace_function_name(original_generic_function),
            ),
        )
        wrapped_original_function = (
            _responses_execution_module._wrap_generic_function_for_deployment_failover(
                original_generic_function,
                outer_request_kwargs=kwargs,
            )
        )
        while True:
            try:
                response = await original_helper(
                    self,
                    model,
                    wrapped_original_function,
                    **kwargs,
                )
                if _context_window_module.request_was_shrunk():
                    # This payload reached the deployment only because a size
                    # refusal was answered with a shorter one.  Remembering it
                    # lets the next turn start inside the real ceiling instead
                    # of paying for the same discovery again.
                    _context_window_module.record_accepted_size(kwargs, model=model)
                if getattr(
                    response,
                    "_young_router_terminal_failed_stream",
                    False,
                ) is True:
                    # A terminal response.failed stream is the answer for this
                    # request. Treating the async iterable as a successful
                    # answer sends its refusal again as a new prompt.
                    return response
                response = _stream_for_a_streaming_client(response, kwargs)
                marker = _routing_module._selected_deployment_marker_from_response(
                    response
                )
                if marker is None:
                    marker = _routing_module._selected_deployment_marker_from_box()
                _routing_module._merge_request_routing_state_into_selected_deployment_marker(
                    marker,
                    kwargs,
                )
                return response
            except Exception as exc:
                # LiteLLM's generic helper catches the upstream 400 after its
                # callback has already returned.  Classify the exact
                # structured-compaction capability failure before any retry,
                # cooldown, peer selection, or route-recovery decision.  The
                # selected-deployment marker may be the only source of the
                # route's api_base/model_info after the callback rebuilt its
                # kwargs, so restore it first.
                _routing_module._apply_current_selected_deployment_to_request(kwargs)
                _routing_module._mark_real_compaction_failure_capability_unsupported(
                    kwargs,
                    exc,
                )
                _routing_module._mark_no_deployments_for_order_exhaustion(exc, kwargs)
                browser_retry_kwargs = None
                if _routing_module._should_retry_with_browser_compatible_headers(exc, kwargs):
                    browser_retry_kwargs = _browser_compatible_headers_retry_kwargs(kwargs)
                if browser_retry_kwargs is not None:
                    kwargs = browser_retry_kwargs
                    target_order = kwargs.get("_target_order")
                    excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                    retry_attempt = 0
                    _trace_module._route_trace(
                        "browser_compatible_headers_retry_start",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=excluded_deployment_ids,
                        request=_trace_module._trace_request_summary(
                            kwargs,
                            method_name=_trace_module._trace_function_name(original_generic_function),
                        ),
                        exception=_routing_module._trace_exception(exc),
                    )
                    continue
                if _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    kwargs,
                ):
                    _routing_module._mark_exception_for_deployment_failover(exc, kwargs)
                _trace_module._route_trace(
                    "generic_fallback_helper_error",
                    request_id=_routing_module._trace_request_id(kwargs),
                    session=_routing_module._trace_session_context(kwargs),
                    model_group=model,
                    target_order=target_order,
                    excluded_deployment_ids=excluded_deployment_ids,
                    retry_attempt=retry_attempt,
                    request=_trace_module._trace_request_summary(
                        kwargs,
                        method_name=_trace_module._trace_function_name(original_generic_function),
                    ),
                    exception=_routing_module._trace_exception(exc),
                )
                # The generic router callback may have rebuilt kwargs from the
                # raw deployment and dropped model_info.  Reapply the selected
                # deployment marker before deciding whether this was a
                # text-only model; otherwise a vision-capable deployment could
                # be rewritten solely because its callback omitted metadata.
                _routing_module._apply_current_selected_deployment_to_request(kwargs)
                if _dsh_vision_router_module.should_attempt_dsh_vision_router(exc, kwargs):
                    _trace_module._route_trace(
                        "dsh_vision_router_fallback_start",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        request=_trace_module._trace_request_summary(
                            kwargs,
                            method_name=_trace_module._trace_function_name(original_generic_function),
                        ),
                        exception=_routing_module._trace_exception(exc),
                    )
                    try:
                        routed_kwargs = await _dsh_vision_router_module.dsh_vision_router_request_kwargs(kwargs)
                        if routed_kwargs is None:
                            raise RuntimeError("dsh-vision-router could not extract image references")
                        routed_kwargs.pop("model", None)
                        kwargs = routed_kwargs
                        target_order = kwargs.get("_target_order")
                        excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                        retry_attempt = 0
                        _trace_module._route_trace(
                            "dsh_vision_router_fallback_retry_start",
                            request_id=_routing_module._trace_request_id(kwargs),
                            session=_routing_module._trace_session_context(kwargs),
                            model_group=model,
                            target_order=target_order,
                            excluded_deployment_ids=excluded_deployment_ids,
                            request=_trace_module._trace_request_summary(
                                kwargs,
                                method_name=_trace_module._trace_function_name(original_generic_function),
                            ),
                        )
                        continue
                    except Exception as router_exc:
                        _trace_module._route_trace(
                            "dsh_vision_router_fallback_error",
                            request_id=_routing_module._trace_request_id(kwargs),
                            session=_routing_module._trace_session_context(kwargs),
                            model_group=model,
                            original_exception=_routing_module._trace_exception(exc),
                            exception=_routing_module._trace_exception(router_exc),
                        )
                _responses_execution_module._restore_routing_constraints(
                    kwargs,
                    target_order=target_order,
                    excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                )
                if (
                    retry_attempt < max_retries
                    and _routing_module._should_retry_final_upstream_route_error(exc, kwargs)
                ):
                    retry_attempt += 1
                    await _routing_module._sleep_before_final_route_retry(
                        model,
                        exc,
                        kwargs,
                        attempt=retry_attempt,
                        max_retries=max_retries,
                        configured_delay_seconds=retry_delay_seconds,
                    )
                    _responses_execution_module._restore_routing_constraints(
                        kwargs,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids")
                        or excluded_deployment_ids,
                    )
                    continue
                # The explicit retry loop has now consumed the selected
                # route's budget (including the default zero extra attempts).
                # Do this before asking for an ordered peer/next-hop entry.
                _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._sync_failed_deployment_exclusions(
                    kwargs,
                    exc,
                    deployment_id=_responses_execution_module._failed_deployment_id(exc),
                )
                decision_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                order_fallback_entry = None
                if not external_web_search_internal:
                    order_fallback_entry = _responses_execution_module._ordered_deployment_fallback_entry(
                        self,
                        exc,
                        decision_kwargs,
                    )
                if order_fallback_entry is not None:
                    for _credential_key in _REQUEST_SCOPED_CREDENTIAL_KEYS:
                        kwargs.pop(_credential_key, None)
                    kwargs.update(
                        {
                            key: value
                            for key, value in order_fallback_entry.items()
                            if key != "model"
                        }
                    )
                    target_order = kwargs.get("_target_order")
                    excluded_deployment_ids = kwargs.get("_excluded_deployment_ids")
                    _trace_module._route_trace(
                        "final_order_fallback_retry_start",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=excluded_deployment_ids,
                        peer_entry=order_fallback_entry,
                        request=_trace_module._trace_request_summary(
                            kwargs,
                            method_name=_trace_module._trace_function_name(original_generic_function),
                        ),
                        exception=_routing_module._trace_exception(exc),
                    )
                    continue
                if (
                    not external_web_search_internal
                    and _routing_module._should_return_route_recovery_stream(exc, decision_kwargs, self)
                ):
                    _trace_module._route_trace(
                        "route_recovery_stream_returned",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(exc),
                    )
                    recovery_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                    recovery_stream_kwargs = recovery_stream_kwargs.copy()
                    recovery_stream_kwargs[
                        "_route_recovery_ignore_local_constraints"
                    ] = True
                    return _routing_module._route_recovery_stream_response(recovery_stream_kwargs, exc)
                if (
                    _routing_module._is_route_recovery_poll_payload(decision_kwargs)
                    and _routing_module._is_route_recovery_poll_error(exc)
                ):
                    _trace_module._route_trace(
                        "route_recovery_poll_error_propagated",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(exc),
                    )
                    raise
                if _routing_module._should_return_failed_responses_stream(exc, kwargs):
                    _trace_module._route_trace(
                        "responses_failed_stream_returned",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=model,
                        target_order=target_order,
                        excluded_deployment_ids=kwargs.get("_excluded_deployment_ids"),
                        exception=_routing_module._trace_exception(exc),
                    )
                    failed_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                    failed_stream_kwargs["original_generic_function"] = wrapped_original_function
                    return _routing_module._failed_responses_stream_response(failed_stream_kwargs, exc)
                if (
                    kwargs.get("stream") is True
                    and "input" in kwargs
                    and _routing_module._recovery_max_seconds_for_request(kwargs) <= 0
                    and _routing_module._is_route_recovery_poll_error(exc)
                ):
                    failed_stream_kwargs = _responses_execution_module._request_kwargs_with_model_group(model, kwargs)
                    failed_stream_kwargs["original_generic_function"] = wrapped_original_function
                    return _routing_module._failed_responses_stream_response(failed_stream_kwargs, exc)
                if _routing_module._should_sanitize_final_upstream_route_error(exc):
                    _routing_module._raise_sanitized_upstream_route_failure(model, exc, kwargs)
                raise

    setattr(patched_generic_helper, _GENERIC_HELPER_PATCH_ATTR, True)
    setattr(patched_generic_helper, "_original_helper", original_helper)
    Router._ageneric_api_call_with_fallbacks_helper = patched_generic_helper


def _install_same_deployment_retry_policy_patch() -> None:
    """Keep LiteLLM's own retry loop aligned with the Menu's next-hop budget."""
    try:
        from litellm.router import Router
    except Exception:
        return

    original_update_kwargs = getattr(Router, "_update_kwargs_before_fallbacks", None)
    if original_update_kwargs is not None and not getattr(
        original_update_kwargs,
        "_young_router_same_deployment_retry_policy_patch",
        False,
    ):

        def patched_update_kwargs(
            self: Any,
            model: str,
            kwargs: dict,
            metadata_variable_name: Optional[str] = "metadata",
        ) -> Any:
            result = original_update_kwargs(
                self,
                model,
                kwargs,
                metadata_variable_name=metadata_variable_name,
            )
            # LiteLLM's per-router retry policy must not consume attempts
            # invisibly. The Menu's explicit loop owns this budget.
            kwargs["num_retries"] = 0
            return result

        setattr(
            patched_update_kwargs,
            "_young_router_same_deployment_retry_policy_patch",
            True,
        )
        setattr(patched_update_kwargs, "_original_update_kwargs", original_update_kwargs)
        Router._update_kwargs_before_fallbacks = patched_update_kwargs

    original_retry_policy = getattr(Router, "get_num_retries_from_retry_policy", None)
    if original_retry_policy is not None and not getattr(
        original_retry_policy,
        "_young_router_same_deployment_retry_policy_patch",
        False,
    ):

        def patched_retry_policy(
            self: Any,
            exception: Exception,
            model_group: Optional[str] = None,
        ) -> int:
            return 0

        setattr(
            patched_retry_policy,
            "_young_router_same_deployment_retry_policy_patch",
            True,
        )
        setattr(patched_retry_policy, "_original_retry_policy", original_retry_policy)
        Router.get_num_retries_from_retry_policy = patched_retry_policy


_MANAGED_WS_KEEPALIVE_PATCH_ATTR = "_young_router_managed_ws_keepalive_patch"


def _install_managed_responses_websocket_keepalive_patch() -> None:
    """Heartbeat the managed Responses WebSocket bridge during silent waits.

    ``ManagedResponsesWebSocketHandler`` serves providers without a native
    WebSocket endpoint by streaming ``litellm.aresponses`` over the client
    WebSocket.  That call bypasses the proxy streaming pipeline, so the
    downstream keepalive heartbeat never runs on this transport: while the
    upstream first event is delayed (a heavy replay prefix can take several
    minutes), nothing reaches the client and Codex fires its per-provider
    ``stream_idle_timeout_ms`` (default ~300s), reporting ``stream
    disconnected before completion: idle timeout waiting for websocket`` and
    replaying the whole turn.  Wrap the inner stream with the proxy heartbeat
    so every silent interval emits one ``response.metadata`` keepalive event
    per configured interval, exactly like the HTTP/SSE transport.
    """

    try:
        from litellm.responses.streaming_iterator import (
            ManagedResponsesWebSocketHandler,
        )
    except Exception:
        return

    original = ManagedResponsesWebSocketHandler._stream_and_forward
    if getattr(original, _MANAGED_WS_KEEPALIVE_PATCH_ATTR, False):
        return

    async def patched_stream_and_forward(self, model, call_kwargs):
        import litellm as litellm_module

        keepalive_request_data = {"model": model, "stream": True}
        for key in ("input", "instructions"):
            value = call_kwargs.get(key)
            if value is not None:
                keepalive_request_data[key] = value
        interval = _routing_module._stream_keepalive_interval_seconds_for_request(
            keepalive_request_data
        )
        stream_response = await litellm_module.aresponses(model=model, **call_kwargs)
        if interval <= 0:
            delivered = stream_response
        else:
            delivered = _streaming_module._yield_downstream_keepalive_stream(
                stream_response,
                keepalive_request_data,
            )
        completed_event = None
        async for chunk in delivered:
            if chunk is None:
                continue
            chunk_type = getattr(chunk, "type", None) or (
                chunk.get("type") if isinstance(chunk, dict) else None
            )
            serialized = self._serialize_chunk(chunk)
            if serialized is None:
                continue
            if chunk_type == "response.completed" and completed_event is None:
                try:
                    completed_event = json.loads(serialized)
                except Exception:
                    pass
            try:
                await self.websocket.send_text(serialized)
            except Exception:
                return completed_event
        return completed_event

    setattr(
        patched_stream_and_forward,
        _MANAGED_WS_KEEPALIVE_PATCH_ATTR,
        True,
    )
    setattr(patched_stream_and_forward, "_original_stream_and_forward", original)
    ManagedResponsesWebSocketHandler._stream_and_forward = patched_stream_and_forward


def _is_pending_reasoning_item_event(event: Any) -> bool:
    """Whether this queued iterator frame announces or closes a reasoning item."""

    if _web_search_native_module._response_item_get(event, "type") not in (
        "response.output_item.added",
        "response.output_item.done",
    ):
        return False
    item = _web_search_native_module._response_item_get(event, "item")
    return (
        _web_search_native_module._response_item_get(item, "type") == "reasoning"
    )


def _reasoning_summary_part_added_event(item_id: Any, output_index: Any) -> Any:
    """The frame that opens summary index 0 on a reasoning item."""

    event: dict[str, Any] = {
        "type": "response.reasoning_summary_part.added",
        "item_id": item_id,
        "output_index": output_index,
        "summary_index": 0,
        "part": {"type": "summary_text", "text": ""},
    }
    return _streaming_module._json_stream_event(event)


def _set_item_field(item: Any, key: str, value: Any) -> None:
    if isinstance(item, dict):
        item[key] = value
        return
    setattr(item, key, value)


def _chunk_reasoning_fragment(chunk: Any) -> str:
    choices = _web_search_native_module._response_item_get(chunk, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        return ""
    delta = _web_search_native_module._response_item_get(choices[0], "delta")
    fragment = _web_search_native_module._response_item_get(delta, "reasoning_content")
    return fragment if isinstance(fragment, str) else ""


def _chunk_closes_reasoning_without_an_answer(chunk: Any) -> bool:
    """Whether this Chat frame would end the thinking while saying nothing.

    LiteLLM ends a reasoning phase on the first frame that carries ``content``,
    ``function_call`` or ``tool_calls``, and it says so by queueing the
    summary's ``done`` frames and its item's ``done`` ahead of that frame.  Two
    shapes carry nothing the reader can see and still close the block:

    * a whitespace-only ``content`` delta -- the blank line a thinking model
      writes as it leaves its thinking (a DeepSeek route streams one before
      its answer);
    * the retired, content-free ``function_call`` mirror a Chat shim repeats
      on every frame of every turn.

    A model that goes back to thinking after one of those then has its
    remaining deltas arrive for an item the client already finished, and Codex
    renders a summary delta it has no open item for **inside the answer**:
    observed live 2026-11-04 on a ``deepseek-v4.1-flash`` turn, two paragraphs
    of English planning landed in the middle of a Chinese answer, and the
    client stored them as that message's text.  Nothing had been delivered yet
    at that point, so the frame is not the answer and may not end the phase.
    """

    get = _web_search_native_module._response_item_get
    choices = get(chunk, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        return False
    choice = choices[0]
    # A terminal frame ends the phase whatever it carries: the turn is over.
    if get(choice, "finish_reason") is not None:
        return False
    if get(chunk, "usage") is not None:
        return False
    delta = get(choice, "delta")
    if delta is None:
        return False
    if get(delta, "tool_calls"):
        return False
    content = get(delta, "content")
    if isinstance(content, str) and content.strip():
        return False
    legacy = get(delta, "function_call")
    if legacy is not None:
        name = get(legacy, "name")
        arguments = get(legacy, "arguments")
        if (isinstance(name, str) and name.strip()) or (
            isinstance(arguments, str) and arguments.strip()
        ):
            return False
    reasoning = get(delta, "reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        # Thinking is the phase itself, not what ends it.
        return False
    return True


def _pending_content_part_done_text(pending: list[Any], queued_from: int) -> Any:
    for event in pending[queued_from:]:
        if _web_search_native_module._response_item_get(event, "type") != "response.content_part.done":
            continue
        part = _web_search_native_module._response_item_get(event, "part")
        if _web_search_native_module._response_item_get(part, "type") != "reasoning_text":
            continue
        text = _web_search_native_module._response_item_get(part, "text")
        if isinstance(text, str) and text:
            return text
    return None


def _reasoning_summary_text_event(item_id: Any, output_index: Any, kind: str, text: str) -> Any:
    event: dict[str, Any] = {
        "type": f"response.reasoning_summary_text.{kind}",
        "item_id": item_id,
        "output_index": output_index,
        "summary_index": 0,
    }
    if kind == "delta":
        event["delta"] = text
    else:
        event["text"] = text
    return _streaming_module._json_stream_event(event)


def _reasoning_summary_part_done_event(item_id: Any, output_index: Any, text: str) -> Any:
    event: dict[str, Any] = {
        "type": "response.reasoning_summary_part.done",
        "item_id": item_id,
        "output_index": output_index,
        "summary_index": 0,
        "part": {"type": "summary_text", "text": text},
    }
    return _streaming_module._json_stream_event(event)


def _apply_reasoning_summary_to_output(output: Any, texts: Any) -> None:
    if not isinstance(output, (list, tuple)) or not isinstance(texts, dict):
        return
    for item in output:
        if _web_search_native_module._response_item_get(item, "type") != "reasoning":
            continue
        item_id = _web_search_native_module._response_item_get(item, "id")
        text = texts.get(item_id)
        if isinstance(text, str) and text:
            _set_item_field(item, "summary", [{"type": "summary_text", "text": text}])


def _apply_reasoning_summary_to_response(iterator: Any, response: Any) -> None:
    output = _web_search_native_module._response_item_get(response, "output")
    texts = getattr(iterator, "_young_router_reasoning_summary_text", None)
    _apply_reasoning_summary_to_output(output, texts)


def reasoning_summary_chunks_for_delivery(
    chunk: Any,
    texts: dict[str, str],
    seen: set[str],
    done: Optional[set[str]] = None,
    request_data: Optional[dict] = None,
) -> list[Any]:
    """Render streamed reasoning as the summary frames a Responses client reads.

    A Responses upstream may stream its thinking as raw
    ``response.reasoning_text.delta`` events rather than
    ``response.reasoning_summary_text.delta``.  A client that asked for a
    summary (``reasoning.summary``) renders the summary frames, so a raw delta
    is mirrored into one as it arrives, and the raw frame itself is suppressed
    by :func:`reasoning_summary_suppresses_chunk`.  A real summary delta from
    the upstream wins over the raw shape.  If the upstream sent no delta for an
    item, no summary is synthesized from its completed text: the reader sees
    the thinking only when the route actually streamed it.  ``done`` keeps the
    terminal frames from repeating.
    """

    get = _web_search_native_module._response_item_get
    chunk_type = get(chunk, "type")
    finished = done if isinstance(done, set) else set()
    if chunk_type == "response.reasoning_text.delta":
        if not _web_search_native_module._public_reasoning_is_allowed(request_data):
            return []
        text = get(chunk, "delta")
        item_id = get(chunk, "item_id")
        output_index = get(chunk, "output_index")
        if (
            not isinstance(text, str)
            or not text
            or item_id is None
            or item_id in seen
            or item_id in finished
        ):
            return []
        first = item_id not in texts
        texts[item_id] = (texts.get(item_id) or "") + text
        events = []
        if first:
            events.append(_reasoning_summary_part_added_event(item_id, output_index))
        events.append(_reasoning_summary_text_event(item_id, output_index, "delta", text))
        return events
    if chunk_type == "response.reasoning_summary_text.delta":
        item_id = get(chunk, "item_id")
        if item_id is not None:
            seen.add(item_id)
        return []
    if chunk_type == "response.output_item.added":
        item = get(chunk, "item")
        if get(item, "type") == "reasoning" and get(item, "summary") is None:
            _set_item_field(item, "summary", [])
        return []
    if chunk_type == "response.content_part.done":
        part = get(chunk, "part")
        if get(part, "type") != "reasoning_text":
            return []
        text = get(part, "text")
        item_id = get(chunk, "item_id")
        output_index = get(chunk, "output_index")
        if (
            not isinstance(text, str)
            or not text
            or item_id is None
            or item_id in finished
        ):
            return []
        if item_id not in texts:
            # Either the upstream sent no delta (do not invent a summary from
            # the completed text) or its own summary frames own the terminal
            # pair; only a raw-delta stream mirrored here needs this hook to
            # close it.
            return []
        texts[item_id] = text
        events = [
            _reasoning_summary_text_event(item_id, output_index, "done", text),
            _reasoning_summary_part_done_event(item_id, output_index, text),
        ]
        finished.add(item_id)
        return events
    if chunk_type == "response.output_item.done":
        item = get(chunk, "item")
        if get(item, "type") == "reasoning":
            text = texts.get(get(item, "id"))
            if isinstance(text, str) and text:
                _set_item_field(item, "summary", [{"type": "summary_text", "text": text}])
        return []
    if chunk_type == "response.completed":
        response = get(chunk, "response")
        _apply_reasoning_summary_to_output(get(response, "output"), texts)
        return []
    return []


def reasoning_summary_suppresses_chunk(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    """Whether a raw reasoning frame was replaced by a summary frame.

    The reader asked to see the thinking and the delivery loop converted this
    delta into a public summary delta; sending the raw frame too would show the
    same text twice in a client that renders both.
    """

    if _web_search_native_module._response_item_get(chunk, "type") != "response.reasoning_text.delta":
        return False
    return _web_search_native_module._public_reasoning_is_allowed(request_data)


def _open_reasoning_summary_items(
    iterator: Any,
    pending: list[Any],
    queued_from: int,
    chunk: Any,
) -> None:
    """Give a reasoning item both the summary frames and the summary text.

    LiteLLM's Chat-to-Responses iterator builds a reasoning item from a delta
    that carries only ``reasoning_content``: it queues the item and a
    ``reasoning_text`` content part, but no ``reasoning_summary_part`` and no
    summary deltas.  Its item ``summary`` is now an empty array rather than
    absent, so restating the array is not enough -- a client that renders
    thinking from ``reasoning_summary_text`` (the Pi does) still sees nothing.
    Emit the summary frames and mirror the text into the item's ``summary`` as
    well, so both the live block and the completed item carry it.  The raw
    ``reasoning_text`` content part stays where LiteLLM put it.
    """

    fragment = _chunk_reasoning_fragment(chunk)
    done_text = _pending_content_part_done_text(pending, queued_from)
    opening: list[Any] = []

    texts = getattr(iterator, "_young_router_reasoning_summary_text", None)
    if not isinstance(texts, dict):
        texts = {}
        setattr(iterator, "_young_router_reasoning_summary_text", texts)
    opened = getattr(iterator, "_young_router_reasoning_summary_open", None)
    if not isinstance(opened, set):
        opened = set()
        setattr(iterator, "_young_router_reasoning_summary_open", opened)
    finished = getattr(iterator, "_young_router_reasoning_summary_done", None)
    if not isinstance(finished, set):
        finished = set()
        setattr(iterator, "_young_router_reasoning_summary_done", finished)

    fragment_added = False
    for event in list(pending[queued_from:]):
        if not _is_pending_reasoning_item_event(event):
            continue
        item = _web_search_native_module._response_item_get(event, "item")
        item_id = _web_search_native_module._response_item_get(item, "id")
        output_index = _web_search_native_module._response_item_get(event, "output_index")
        if item_id is None:
            continue

        if _web_search_native_module._response_item_get(item, "summary") is None:
            _set_item_field(item, "summary", [])

        text = texts.get(item_id, "")
        if item_id not in opened:
            opening.append(_reasoning_summary_part_added_event(item_id, output_index))
            opened.add(item_id)
        if fragment and not fragment_added:
            # The stock iterator answers this same chunk with its own summary
            # delta; restating the fragment here delivered the block's first
            # fragment twice (live 2026-11-04, the thinking opened on "WeWe
            # need answer Chinese").  Only the accumulated text is kept, for
            # the item this block completes with.
            text += fragment
            texts[item_id] = text
            fragment_added = True
        if done_text is not None and item_id not in finished:
            text = done_text or text
            opening.append(
                _reasoning_summary_text_event(item_id, output_index, "done", text)
            )
            opening.append(
                _reasoning_summary_part_done_event(item_id, output_index, text)
            )
            _set_item_field(item, "summary", [{"type": "summary_text", "text": text}])
            texts[item_id] = text
            finished.add(item_id)
        elif _web_search_native_module._response_item_get(event, "type") == "response.output_item.done" and text:
            _set_item_field(item, "summary", [{"type": "summary_text", "text": text}])

    setattr(iterator, "_young_router_reasoning_summary_text", texts)
    setattr(iterator, "_young_router_reasoning_summary_open", opened)
    setattr(iterator, "_young_router_reasoning_summary_done", finished)
    if opening:
        pending.extend(opening)


def _install_responses_completion_stream_patch() -> None:
    try:
        from litellm.responses.litellm_completion_transformation.streaming_iterator import (
            LiteLLMCompletionStreamingIterator,
        )
    except Exception:
        return

    original_init = getattr(LiteLLMCompletionStreamingIterator, "__init__", None)
    if original_init is None or getattr(
        original_init,
        _RESPONSES_COMPLETION_STREAM_PATCH_ATTR,
        False,
    ):
        return

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not hasattr(self, "completed_response"):
            self.completed_response = None
        if not hasattr(self, "_young_router_reasoning_summary_text"):
            self._young_router_reasoning_summary_text = {}
        if not hasattr(self, "_young_router_reasoning_summary_open"):
            self._young_router_reasoning_summary_open = set()
        if not hasattr(self, "_young_router_reasoning_summary_done"):
            self._young_router_reasoning_summary_done = set()

    setattr(patched_init, _RESPONSES_COMPLETION_STREAM_PATCH_ATTR, True)
    setattr(patched_init, "_original_init", original_init)
    LiteLLMCompletionStreamingIterator.__init__ = patched_init

    original_ensure_output_item_for_chunk = getattr(
        LiteLLMCompletionStreamingIterator,
        "_ensure_output_item_for_chunk",
        None,
    )
    if callable(original_ensure_output_item_for_chunk) and not getattr(
        original_ensure_output_item_for_chunk,
        _RESPONSES_COMPLETION_STREAM_OUTPUT_ITEM_PATCH_ATTR,
        False,
    ):

        def patched_ensure_output_item_for_chunk(self: Any, chunk: Any) -> Any:
            # A completed provider search is released before this chunk is
            # allowed to start the answer: the text can only have been written
            # afterwards, and the client renders in emission order.
            _responses_output_module._release_completed_web_search_events(
                self
            )
            # Do not let a role-only Chat chunk create an empty Responses
            # message.  Wait for text, reasoning, annotations, or a tool
            # payload; tool IDs and ordering remain owned by LiteLLM.
            if not _responses_output_module._chat_completion_chunk_has_output_payload(
                chunk
            ):
                return None
            # A frame that would only close the thinking does not begin the
            # answer either: opening the message item on it announced an empty
            # answer ahead of the thinking that was still streaming.
            if (
                getattr(self, "_reasoning_active", False)
                and _chunk_closes_reasoning_without_an_answer(chunk)
            ):
                return None
            pending = getattr(self, "_pending_response_events", None)
            queued_before = len(pending) if isinstance(pending, list) else 0
            result = original_ensure_output_item_for_chunk(self, chunk)
            if isinstance(pending, list):
                _open_reasoning_summary_items(self, pending, queued_before, chunk)
            return result

        setattr(
            patched_ensure_output_item_for_chunk,
            _RESPONSES_COMPLETION_STREAM_OUTPUT_ITEM_PATCH_ATTR,
            True,
        )
        setattr(
            patched_ensure_output_item_for_chunk,
            "_original_ensure_output_item_for_chunk",
            original_ensure_output_item_for_chunk,
        )
        LiteLLMCompletionStreamingIterator._ensure_output_item_for_chunk = (
            patched_ensure_output_item_for_chunk
        )

    original_is_reasoning_end = getattr(
        LiteLLMCompletionStreamingIterator,
        "_is_reasoning_end",
        None,
    )
    if callable(original_is_reasoning_end) and not getattr(
        original_is_reasoning_end,
        _RESPONSES_COMPLETION_STREAM_REASONING_END_PATCH_ATTR,
        False,
    ):

        def patched_is_reasoning_end(self: Any, chunk: Any) -> Any:
            # LiteLLM closes the thinking on any frame that carries content, a
            # legacy ``function_call`` or a tool call.  A blank line and the
            # content-free mirror carry neither a call nor a word, and the
            # model that wrote them goes on thinking: closing the block there
            # strands the rest of its summary on a finished item, which the
            # client renders inside the answer (see
            # ``_chunk_closes_reasoning_without_an_answer``).
            if _chunk_closes_reasoning_without_an_answer(chunk):
                return False
            return original_is_reasoning_end(self, chunk)

        setattr(
            patched_is_reasoning_end,
            _RESPONSES_COMPLETION_STREAM_REASONING_END_PATCH_ATTR,
            True,
        )
        setattr(
            patched_is_reasoning_end,
            "_original_is_reasoning_end",
            original_is_reasoning_end,
        )
        LiteLLMCompletionStreamingIterator._is_reasoning_end = (
            patched_is_reasoning_end
        )

    original_transform_chunk = getattr(
        LiteLLMCompletionStreamingIterator,
        "_transform_chat_completion_chunk_to_response_api_chunk",
        None,
    )
    if callable(original_transform_chunk) and not getattr(
        original_transform_chunk,
        _RESPONSES_COMPLETION_STREAM_TRANSFORM_PATCH_ATTR,
        False,
    ):

        def patched_transform_chat_completion_chunk(
            self: Any,
            chunk: Any,
        ) -> Any:
            # The frame that only carries the answer's blank line is held back
            # with its phase: nothing was delivered before it, so its text
            # cannot be the start of a word, and delivering it would open the
            # answer over a thinking block that is still streaming.
            if (
                getattr(self, "_reasoning_active", False)
                and _chunk_closes_reasoning_without_an_answer(chunk)
            ):
                return None
            # Once the summary's terminal frames are out, the client's thinking
            # block is finished and it has no item to append a late delta to --
            # Codex renders one under the answer instead.  Thinking that comes
            # back after its block closed has no ordered place, so it is not
            # delivered rather than shown as prose.
            if (
                getattr(self, "_reasoning_done_emitted", False)
                and _chunk_reasoning_fragment(chunk)
            ):
                return None
            return original_transform_chunk(self, chunk)

        setattr(
            patched_transform_chat_completion_chunk,
            _RESPONSES_COMPLETION_STREAM_TRANSFORM_PATCH_ATTR,
            True,
        )
        setattr(
            patched_transform_chat_completion_chunk,
            "_original_transform_chat_completion_chunk_to_response_api_chunk",
            original_transform_chunk,
        )
        LiteLLMCompletionStreamingIterator._transform_chat_completion_chunk_to_response_api_chunk = (
            patched_transform_chat_completion_chunk
        )

    original_return_default_done_events = getattr(
        LiteLLMCompletionStreamingIterator,
        "return_default_done_events",
        None,
    )
    if original_return_default_done_events is not None and not getattr(
        original_return_default_done_events,
        _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
        False,
    ):

        def patched_return_default_done_events(
            self: Any,
            litellm_complete_object: Any,
        ) -> Any:
            if _responses_output_module._streaming_completion_should_skip_empty_message_events(
                litellm_complete_object
            ):
                self.sent_output_text_done_event = True
                self.sent_output_content_part_done_event = True
                self.sent_output_item_done_event = True
                return None
            return original_return_default_done_events(
                self,
                litellm_complete_object,
            )

        setattr(
            patched_return_default_done_events,
            _RESPONSES_COMPLETION_STREAM_DEFAULT_DONE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_return_default_done_events,
            "_original_return_default_done_events",
            original_return_default_done_events,
        )
        LiteLLMCompletionStreamingIterator.return_default_done_events = (
            patched_return_default_done_events
        )

    # LiteLLM renamed this emitter to ``_emit_terminal_response_event`` (it
    # can now answer an incomplete terminal event too): 1.104 still called the
    # old name, 1.106 only has the new one. Patch whichever name the installed
    # release carries: a patch that silently stops applying leaves the managed
    # WebSocket transport's completed event unnormalized, because that
    # transport bypasses the SSE pipeline that normalizes usage.
    def _patch_completed_event_emitter(attribute_name: str) -> None:
        original_emit_completed_event = getattr(
            LiteLLMCompletionStreamingIterator,
            attribute_name,
            None,
        )
        if original_emit_completed_event is None or getattr(
            original_emit_completed_event,
            _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
            False,
        ):
            return

        def patched_emit_completed_event(
            self: Any,
            litellm_model_response: Any,
        ) -> Any:
            response_completed_event = original_emit_completed_event(
                self,
                litellm_model_response,
            )
            response = _web_search_native_module._response_item_get(response_completed_event, "response")
            if response is not None:
                _apply_reasoning_summary_to_response(self, response)
                _responses_output_module._strip_empty_message_items_when_structured_output_present(response)
            _normalize_response_completed_event_usage(response_completed_event)
            return response_completed_event

        setattr(
            patched_emit_completed_event,
            _RESPONSES_COMPLETION_STREAM_COMPLETED_PATCH_ATTR,
            True,
        )
        setattr(
            patched_emit_completed_event,
            "_original_emit_completed_event",
            original_emit_completed_event,
        )
        setattr(
            LiteLLMCompletionStreamingIterator,
            attribute_name,
            patched_emit_completed_event,
        )

    _patch_completed_event_emitter("_emit_response_completed_event")
    _patch_completed_event_emitter("_emit_terminal_response_event")

def _chat_bridge_reasoning_replay_repaired_messages(
    litellm_completion_request: Any,
) -> list[str]:
    """Give a thinking upstream the replay contract LiteLLM left unsatisfied.

    A DeepSeek thinking upstream refuses the whole request with HTTP 400 ``the
    `reasoning_content` in the thinking mode must be passed back to the API``
    whenever an assistant message that carries ``tool_calls`` has no
    ``reasoning_content`` field at all.  The field being *empty* answers;
    being absent does not.  Measured side by side on api.deepseek.com, one
    history, one tool result: with the field the turn answers, without it the
    same bytes are refused.

    On the Responses surface the client's own replay satisfies that contract,
    because the ``reasoning`` item is forwarded as-is.  Across the chat bridge
    it cannot: ``transform_responses_api_input_to_messages`` turns a reasoning
    item into the assistant's ``reasoning_content`` only when it finds plaintext
    in ``content`` or ``summary``, and drops the item outright when the payload
    is only ``encrypted_content`` -- which is exactly the shape a Codex client
    sends.  So the bridged assistant message reaches the upstream with the
    field missing and a route that answers this conversation natively refuses
    it here.

    The repair adds the field and nothing else: an *empty* ``reasoning_content``
    states exactly as much as an opaque blob did to a provider that cannot
    decode it.  No thinking is invented, no content is moved, and a turn that
    already carries the field is left alone.  Only assistant messages with tool
    calls are touched, because that is the shape the contract names.

    The same transform has a second, broader hole that does not involve tool
    calls at all: a reasoning item *with* a visible summary becomes its own
    ``{"role": "assistant", "content": None, "reasoning_content": <summary>}``
    message, and DeepSeek refuses the whole request with HTTP 400 ``Invalid
    assistant message: content or tool_calls must be set``.  A Codex replay
    carries such an item after every thinking step, so this is the common case
    and not an edge one -- measured live 2026-11-04/05 on api.deepseek.com, 318
    refusals of exactly this shape, each one costing a full replay upload before
    the group's sibling route answered it.  Measured side by side on the same
    history: ``content: null`` is refused, ``content: ""`` is answered.  So the
    repair writes the empty string and nothing else -- an empty content states
    exactly as little as ``null`` did, no text moves between fields, and a
    message that already has content or tool calls is left alone.

    Returns the tool-call ids it repaired, for the trace.
    """

    messages = (
        litellm_completion_request.get("messages")
        if isinstance(litellm_completion_request, dict)
        else None
    )
    if not isinstance(messages, list):
        return []
    messages = _chat_bridge_empty_assistant_replayed_messages(messages)
    if isinstance(litellm_completion_request, dict):
        litellm_completion_request["messages"] = messages
    repaired: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        if "reasoning_content" in message:
            continue
        message["reasoning_content"] = ""
        for call in tool_calls:
            if isinstance(call, dict):
                call_id = call.get("id")
                if isinstance(call_id, str) and call_id.strip():
                    repaired.append(call_id.strip())
    return repaired


def _chat_bridge_empty_assistant_replayed_messages(
    messages: list[Any],
) -> list[Any]:
    """Keep a bridged assistant message from being rejected as empty.

    LiteLLM emits ``content: None`` for a reasoning item once it has moved that
    item's payload into ``reasoning_content``, and a thinking upstream refuses
    the message as having neither content nor tool calls.  The empty string is
    the same absence stated in the shape the validator accepts; this writes it
    only where the message would otherwise be refused.
    """

    repaired: list[Any] = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content")
            tool_calls = message.get("tool_calls")
            if content is None and not (
                isinstance(tool_calls, list) and tool_calls
            ):
                repaired.append(dict(message, content=""))
                continue
        repaired.append(message)
    return repaired


def _install_responses_chat_bridge_reasoning_replay_patch() -> None:
    """Repair the bridged replay before a thinking upstream judges it."""

    try:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )
    except Exception:
        return

    original_transform = getattr(
        LiteLLMCompletionResponsesConfig,
        "transform_responses_api_request_to_chat_completion_request",
        None,
    )
    if original_transform is None or getattr(
        original_transform,
        _RESPONSES_CHAT_BRIDGE_REASONING_PATCH_ATTR,
        False,
    ):
        return

    def patched_transform_responses_api_request_to_chat_completion_request(
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        litellm_completion_request = original_transform(*args, **kwargs)
        repaired = _chat_bridge_reasoning_replay_repaired_messages(
            litellm_completion_request
        )
        if repaired:
            _trace_module._route_trace(
                "responses_chat_bridge_reasoning_replay_repaired",
                model_group=kwargs.get("model"),
                repaired_tool_call_count=len(repaired),
                repaired_tool_call_ids=repaired[:20],
            )
        return litellm_completion_request

    setattr(
        patched_transform_responses_api_request_to_chat_completion_request,
        _RESPONSES_CHAT_BRIDGE_REASONING_PATCH_ATTR,
        True,
    )
    setattr(
        patched_transform_responses_api_request_to_chat_completion_request,
        "_original_transform_responses_api_request_to_chat_completion_request",
        original_transform,
    )
    LiteLLMCompletionResponsesConfig.transform_responses_api_request_to_chat_completion_request = staticmethod(
        patched_transform_responses_api_request_to_chat_completion_request
    )


_RESPONSES_BRIDGE_CACHE_USAGE_PATCH_ATTR = "_young_router_bridge_cache_usage_patch"


def _bridge_usage_with_provider_cache_hits(usage: Any) -> Any:
    """Carry a provider's own cache-hit count across the chat bridge.

    LiteLLM builds ``input_tokens_details.cached_tokens`` from
    ``prompt_tokens_details.cached_tokens``, and a DeepSeek-shaped response
    leaves that field at zero while reporting the same fact as
    ``prompt_cache_hit_tokens`` / ``cache_read_input_tokens``.  A bridged
    ``/v1/responses`` request therefore told Codex the cache was cold on every
    step of a thread whose prefix the upstream had in fact reused -- and Codex
    reads that number for both its turn meter and its compaction decision, so a
    false zero is not harmless.  Measured on a real replay: the upstream
    answered ``prompt_cache_hit_tokens: 16000`` of 16224 and the client was told
    ``cached_tokens: 0``.

    Only a missing number is filled in: an OpenAI-shaped usage is already
    correct and is passed through untouched.
    """

    details = getattr(usage, "prompt_tokens_details", None)
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    reported = 0
    if isinstance(details, dict):
        reported = int(details.get("cached_tokens") or 0)
    else:
        reported = int(getattr(details, "cached_tokens", 0) or 0)
    if reported:
        return usage

    hit = 0
    for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
        if isinstance(usage, dict):
            hit = int(usage.get(key) or 0)
        else:
            hit = int(getattr(usage, key, 0) or 0)
        if hit:
            break
    if not hit:
        return usage

    input_tokens = 0
    if isinstance(usage, dict):
        input_tokens = int(usage.get("prompt_tokens") or 0)
    else:
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    if input_tokens:
        hit = min(hit, input_tokens)

    try:
        from litellm.types.utils import PromptTokensDetailsWrapper

        details = PromptTokensDetailsWrapper(cached_tokens=hit)
        if isinstance(usage, dict):
            usage["prompt_tokens_details"] = details
        else:
            usage.prompt_tokens_details = details
    except Exception:
        return usage
    return usage


_USAGE_CACHE_READ_NO_OVERWRITE_ATTR = "_young_router_usage_cache_read_no_overwrite"


def _install_usage_cache_read_no_overwrite_patch() -> None:
    """Stop a relay's zero from erasing a real cache count.

    LiteLLM's ``Usage`` builds ``prompt_tokens_details.cached_tokens`` from the
    OpenAI-shaped field and then applies its Anthropic mapping, which assigns
    ``cache_read_input_tokens`` on top.  This app's DeepSeek route answers both
    keys on the same frame -- the real count in ``prompt_tokens_details`` and a
    literal ``0`` in ``cache_read_input_tokens`` -- so that assignment overwrote
    a correct 29,824 with a zero before anything downstream could read it.

    Measured, same body each time: straight to the upstream ``cached=29824``;
    through the proxy it reached Codex as ``cached_tokens: 0``; and
    ``_responses_usage_for_codex`` saw only zeroes, so no later repair could
    recover the count.  That zero is not harmless -- Codex's turn meter and its
    compaction both read this number.

    The two keys count the same quantity (input tokens served from cache), so a
    larger value can only be a more complete report.  This rewrites the last
    assignment to keep the larger one, and does nothing when the OpenAI-shaped
    field is absent or smaller.
    """

    try:
        from litellm.types.utils import Usage
    except Exception:
        return
    original_init = getattr(Usage, "__init__", None)
    if original_init is None or getattr(original_init, _USAGE_CACHE_READ_NO_OVERWRITE_ATTR, False):
        return

    # Read the OpenAI-shaped count the base model already validated, before the
    # Anthropic assignment in ``Usage.__init__`` runs.
    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        details = kwargs.get("prompt_tokens_details")
        read = kwargs.get("cache_read_input_tokens")
        keep = None
        if isinstance(details, dict):
            keep = details.get("cached_tokens")
        elif details is not None:
            keep = getattr(details, "cached_tokens", None)
        original_init(self, *args, **kwargs)
        if (
            isinstance(keep, int)
            and not isinstance(keep, bool)
            and isinstance(read, int)
            and not isinstance(read, bool)
            and keep > read
        ):
            current = getattr(self, "prompt_tokens_details", None)
            if current is not None:
                try:
                    current.cached_tokens = keep
                except Exception:
                    pass
            try:
                self.cache_read_input_tokens = keep
            except Exception:
                pass

    setattr(patched_init, _USAGE_CACHE_READ_NO_OVERWRITE_ATTR, True)
    setattr(patched_init, "_original_usage_init", original_init)
    Usage.__init__ = patched_init


def _install_responses_bridge_cache_usage_patch() -> None:
    """Give the bridged Responses shape the cache count the upstream sent.

    The repair runs on the chat response *before* LiteLLM reads its usage, so the
    number it copies into ``input_tokens_details`` is the one the provider
    reported.  Both the non-streaming transform and the streaming iterator reach
    LiteLLM's conversion through this one method.
    """

    try:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )
    except Exception:
        return

    original = getattr(
        LiteLLMCompletionResponsesConfig,
        "transform_chat_completion_response_to_responses_api_response",
        None,
    )
    if original is None or getattr(original, _RESPONSES_BRIDGE_CACHE_USAGE_PATCH_ATTR, False):
        return

    def patched_transform_chat_completion_response_to_responses_api_response(
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        chat_response = (
            args[0]
            if args
            else kwargs.get("chat_completion_response")
        )
        usage = getattr(chat_response, "usage", None) if chat_response is not None else None
        repaired = _bridge_usage_with_provider_cache_hits(usage)
        if repaired is not usage:
            try:
                chat_response.usage = repaired
            except Exception:
                pass
        try:
            details = getattr(usage, "prompt_tokens_details", None)
            _trace_module._route_trace(
                "responses_bridge_cache_usage",
                provider_cache_hit=getattr(usage, "prompt_cache_hit_tokens", None),
                cached_tokens_before=(getattr(details, "cached_tokens", None) if details else None),
                repaired=repaired is not usage,
            )
        except Exception:
            pass
        return original(*args, **kwargs)

    setattr(
        patched_transform_chat_completion_response_to_responses_api_response,
        _RESPONSES_BRIDGE_CACHE_USAGE_PATCH_ATTR,
        True,
    )
    setattr(
        patched_transform_chat_completion_response_to_responses_api_response,
        "_original_transform_response",
        original,
    )
    LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response = staticmethod(
        patched_transform_chat_completion_response_to_responses_api_response
    )


def _install_responses_tool_search_bridge_patch() -> None:
    try:
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )
    except Exception:
        LiteLLMCompletionResponsesConfig = None  # type: ignore

    if LiteLLMCompletionResponsesConfig is not None:
        original_transform_response = getattr(
            LiteLLMCompletionResponsesConfig,
            "transform_chat_completion_response_to_responses_api_response",
            None,
        )
        if original_transform_response is not None and not getattr(
            original_transform_response,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            False,
        ):

            def patched_transform_chat_completion_response_to_responses_api_response(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                response = original_transform_response(*args, **kwargs)
                request_input = args[0] if len(args) > 0 else kwargs.get("request_input")
                responses_api_request = (
                    args[1] if len(args) > 1 else kwargs.get("responses_api_request")
                )
                return _responses_output_module._normalize_response_tool_search_output(
                    response,
                    _responses_output_module._responses_namespace_tool_map(
                        request_input,
                        responses_api_request,
                    ),
                    _responses_output_module._responses_custom_tool_names(
                        request_input,
                        responses_api_request,
                    ),
                )

            setattr(
                patched_transform_chat_completion_response_to_responses_api_response,
                _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
                True,
            )
            setattr(
                patched_transform_chat_completion_response_to_responses_api_response,
                "_original_transform",
                original_transform_response,
            )
            LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response = staticmethod(
                patched_transform_chat_completion_response_to_responses_api_response
            )

    try:
        from litellm.responses.litellm_completion_transformation.streaming_iterator import (
            LiteLLMCompletionStreamingIterator,
        )
    except Exception:
        return

    original_queue_delta = getattr(
        LiteLLMCompletionStreamingIterator,
        "_queue_tool_call_delta_events",
        None,
    )
    if original_queue_delta is not None and not getattr(
        original_queue_delta,
        _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
        False,
    ):

        def patched_queue_tool_call_delta_events(
            self: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            result = original_queue_delta(self, *args, **kwargs)
            _responses_output_module._normalize_pending_tool_search_events(self)
            return result

        setattr(
            patched_queue_tool_call_delta_events,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_queue_tool_call_delta_events,
            "_original_queue_delta",
            original_queue_delta,
        )
        LiteLLMCompletionStreamingIterator._queue_tool_call_delta_events = (
            patched_queue_tool_call_delta_events
        )

    original_queue_final = getattr(
        LiteLLMCompletionStreamingIterator,
        "_queue_final_tool_call_done_events",
        None,
    )
    if original_queue_final is not None and not getattr(
        original_queue_final,
        _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
        False,
    ):

        def patched_queue_final_tool_call_done_events(
            self: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            result = original_queue_final(self, *args, **kwargs)
            _responses_output_module._normalize_pending_tool_search_events(self)
            return result

        setattr(
            patched_queue_final_tool_call_done_events,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_queue_final_tool_call_done_events,
            "_original_queue_final",
            original_queue_final,
        )
        LiteLLMCompletionStreamingIterator._queue_final_tool_call_done_events = (
            patched_queue_final_tool_call_done_events
        )

    # A native search is collected into provider fields and only emitted when
    # the stream ends, while the answer text streams as it arrives.  Arrival
    # order then reads answer-first, search-below-it.  The client renders in
    # emission order, so the completed search is released at the point the
    # answer starts.  See ``_release_completed_web_search_events``.
    original_queue_message = getattr(
        LiteLLMCompletionStreamingIterator,
        "_queue_message_item_added_events",
        None,
    )
    if original_queue_message is not None and not getattr(
        original_queue_message,
        _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
        False,
    ):

        def patched_queue_message_item_added_events(
            self: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            # Before the answer's own item is queued: the search has to reach
            # the client first, because it is what the answer was written from.
            _responses_output_module._release_completed_web_search_events(
                self
            )
            return original_queue_message(self, *args, **kwargs)

        setattr(
            patched_queue_message_item_added_events,
            _RESPONSES_TOOL_SEARCH_BRIDGE_PATCH_ATTR,
            True,
        )
        setattr(
            patched_queue_message_item_added_events,
            "_original_queue_message",
            original_queue_message,
        )
        LiteLLMCompletionStreamingIterator._queue_message_item_added_events = (
            patched_queue_message_item_added_events
        )


_MODEL_LISTING_EFFORTS_PATCH_ATTR = (
    "_young_router_model_listing_efforts_patch"
)
_PRE_CALL_TOKEN_COUNT_PATCH_ATTR = "_young_router_pre_call_token_count_patch"


def _install_pre_call_token_count_patch() -> None:
    """Count a replayed Responses body the way its upstream reads it.

    LiteLLM's ``enable_pre_call_checks`` refuses a request before any route is
    contacted when the token count it computes exceeds the route's
    ``max_input_tokens``.  On the Responses surface that count normalizes every
    input item into a chat message, and a ``reasoning`` item is normalized to
    one whose ``content`` is its ``summary`` text -- words the client never
    sends (it replays an opaque id) and the upstream never receives.  A thread
    whose real prompt was 400k tokens was charged 1,004,863 against a
    1,000,000 window for exactly that text, so a conversation with obvious
    headroom ended as ``context_length_exceeded`` before a route was tried.

    The estimate is the whole decision here, so it is taken over the same items
    the upstream will read: a summary-only reasoning item counts by identity, a
    signed one keeps everything it carries (a signed prefix is re-sent
    byte-for-byte, and pretending otherwise would undercount a real body), and
    one that carries visible ``content`` keeps that too.  ``input`` is not
    mutated; this wrapper only replaces the value it counts.
    """

    try:
        from litellm.router import Router
    except Exception:
        return

    original_count = getattr(Router, "_count_pre_call_check_tokens", None)
    if original_count is None or getattr(
        original_count, _PRE_CALL_TOKEN_COUNT_PATCH_ATTR, False
    ):
        return

    def patched_count_pre_call_check_tokens(
        self: Any,
        messages: Optional[Any],
        input: Optional[Any],
        request_kwargs: Optional[Any] = None,
    ) -> int:
        # ``messages`` is already a chat shape: nothing here needs reading.
        if messages is None and isinstance(input, list):
            input = _responses_request_module._without_replayed_reasoning_summary_text(
                input
            )
        return original_count(
            self,
            messages=messages,
            input=input,
            request_kwargs=request_kwargs,
        )

    setattr(
        patched_count_pre_call_check_tokens, _PRE_CALL_TOKEN_COUNT_PATCH_ATTR, True
    )
    setattr(
        patched_count_pre_call_check_tokens,
        "_original_count_pre_call_check_tokens",
        original_count,
    )
    Router._count_pre_call_check_tokens = patched_count_pre_call_check_tokens


def _install_model_listing_efforts_patch() -> None:
    """Publish each route's reasoning levels on the model listing.

    Pi's model discovery reads ``supported_efforts`` from an
    OpenAI-compatible ``/v1/models`` entry to build a model's
    ``thinkingLevelMap``: a level the array omits is recorded as an explicit
    ``null``, which is how Pi says "this model does not offer it".  A route
    that only reports ``reasoning: true`` therefore loses ``xhigh``/``max`` in
    Pi even when the connection in front of it accepts both, and the user has
    to hand-write the map.  Codex reads the same response but ignores this
    extra key, and its own catalog already carries the levels.

    ``create_model_info_response`` is the one builder behind the listing and
    the single-model lookup, and ``proxy_server`` imports it inside the
    endpoint that calls it rather than at module scope, so replacing the
    ``litellm.proxy.utils`` attribute reaches every listing caller without
    forking the endpoint.  The added key carries only levels the route's own
    deployments all accept -- the same common surface the request path clamps
    against -- so the listing cannot advertise a level this proxy would then
    refuse to map.
    """

    try:
        from litellm.proxy import utils as litellm_proxy_utils
    except Exception:
        return

    original = getattr(litellm_proxy_utils, "create_model_info_response", None)
    if not callable(original) or getattr(
        original, _MODEL_LISTING_EFFORTS_PATCH_ATTR, False
    ):
        return

    def patched_create_model_info_response(*args: Any, **kwargs: Any) -> Any:
        response = original(*args, **kwargs)
        if not isinstance(response, dict):
            return response
        efforts = _reasoning_module._model_listing_efforts(response.get("id"))
        if efforts is not None:
            response[_reasoning_module._MODEL_LISTING_EFFORTS_KEY] = efforts
        return response

    setattr(patched_create_model_info_response, _MODEL_LISTING_EFFORTS_PATCH_ATTR, True)
    setattr(
        patched_create_model_info_response,
        "_original_create_model_info_response",
        original,
    )
    litellm_proxy_utils.create_model_info_response = patched_create_model_info_response


#: A ``chatgpt/`` deployment names the account it answers with this scheme in
#: its ``api_key``.  LiteLLM's ChatGPT adapter resolves one token directory from
#: the process environment at construction, so a proxy process could serve
#: exactly one ChatGPT account: every other account was dropped as
#: unauthenticated ("ignoring and continuing with other deployments").  The
#: reference below is not a secret; it is the account's private directory, whose
#: ``auth.json`` the adapter already knows how to read and refresh.
_CHATGPT_AUTH_SCHEME = "young-router-auth:"
_CHATGPT_ACCOUNT_ENV = "YOUNG_ROUTER_PROVIDER_AUTH_ROOT"
_CHATGPT_ACCOUNT_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


def _chatgpt_account_authenticator(api_key: object):
    """The account an ``api_key`` reference names, or None for a plain key."""

    if not isinstance(api_key, str) or not api_key.startswith(_CHATGPT_AUTH_SCHEME):
        return None
    ref = api_key[len(_CHATGPT_AUTH_SCHEME) :].strip()
    if not re.fullmatch(_CHATGPT_ACCOUNT_REF_PATTERN, ref):
        return None
    root = str(os.environ.get(_CHATGPT_ACCOUNT_ENV, "") or "").strip()
    if not root:
        return None
    auth_file = Path(root) / "chatgpt" / ref / "auth.json"
    if not auth_file.is_file():
        return None
    from litellm.llms.chatgpt.authenticator import Authenticator

    # Configured directly, exactly as the Core's own login worker does: the
    # adapter's paths are per instance, so no process environment is involved.
    auth = Authenticator.__new__(Authenticator)
    auth.token_dir = str(auth_file.parent)
    auth.auth_file = str(auth_file)
    return auth


def _install_chatgpt_account_credential_patch() -> None:
    """Let each ``chatgpt/`` deployment resolve its own account.

    A route's ``api_key`` carries the account reference; the adapter reads that
    account's ``auth.json`` instead of one directory shared by the whole
    process.  Everything above stays LiteLLM's: the token exchange, the refresh
    on expiry, the request shape, and the streaming.  A deployment without the
    reference keeps the adapter's own behaviour.
    """

    try:
        from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig
        from litellm.llms.chatgpt.common_utils import ensure_chatgpt_session_id, get_chatgpt_default_headers
        from litellm.llms.openai.openai import OpenAIConfig
    except Exception:  # pragma: no cover - an adapter that moved is reported by the caller
        return
    if getattr(ChatGPTConfig, "_young_router_account_credentials", False):
        return

    original_info = ChatGPTConfig._get_openai_compatible_provider_info
    original_validate = ChatGPTConfig.validate_environment

    def account_credentials(self, model, api_base, api_key, custom_llm_provider):
        bound = _chatgpt_account_authenticator(api_key)
        if bound is None:
            return original_info(self, model, api_base, api_key, custom_llm_provider)
        return bound.get_api_base(), bound.get_access_token(), custom_llm_provider

    def account_headers(
        self,
        headers,
        model,
        messages,
        optional_params,
        litellm_params,
        api_key=None,
        api_base=None,
    ):
        bound = _chatgpt_account_authenticator(api_key)
        if bound is None:
            return original_validate(
                self,
                headers,
                model,
                messages,
                optional_params,
                litellm_params,
                api_key,
                api_base,
            )
        validated = OpenAIConfig.validate_environment(
            self,
            headers,
            model,
            messages,
            optional_params,
            litellm_params,
            api_key,
            api_base,
        )
        default_headers = get_chatgpt_default_headers(
            api_key or "",
            bound.get_account_id(),
            ensure_chatgpt_session_id(litellm_params),
        )
        return {**default_headers, **validated}

    setattr(account_credentials, "_original_get_openai_compatible_provider_info", original_info)
    setattr(account_headers, "_original_validate_environment", original_validate)
    ChatGPTConfig._get_openai_compatible_provider_info = account_credentials
    ChatGPTConfig.validate_environment = account_headers
    setattr(ChatGPTConfig, "_young_router_account_credentials", True)


_OPENAI_RESPONSES_FAKE_STREAM_PATCH_ATTR = (
    "_young_router_openai_responses_fake_stream_patch"
)


def _install_openai_responses_fake_stream_patch() -> None:
    """Keep a registered relay's own SSE instead of LiteLLM's fake stream.

    ``OpenAIResponsesAPIConfig.should_fake_stream`` asks
    ``supports_native_streaming``, which calls ``get_llm_provider`` and raises
    for a relay model whose bare id carries no provider.  The adapter passes no
    provider for such a call, so an unknown custom model is declared
    non-streaming and LiteLLM runs the request without ``stream`` and slices the
    completed body into events: the reader waits through the whole thinking
    phase and receives every summary delta at once.  A model this app already
    registered with ``supports_native_streaming`` is answered from that entry
    here, before the provider-inference failure can fake the stream.
    """

    try:
        import litellm

        from litellm.llms.openai.responses.transformation import (
            OpenAIResponsesAPIConfig,
        )
    except Exception:
        return

    original = getattr(OpenAIResponsesAPIConfig, "should_fake_stream", None)
    if not callable(original) or getattr(
        original, _OPENAI_RESPONSES_FAKE_STREAM_PATCH_ATTR, False
    ):
        return

    def patched_should_fake_stream(
        self: Any,
        model: Any = None,
        stream: Any = None,
        custom_llm_provider: Any = None,
    ) -> Any:
        if stream is True and isinstance(model, str):
            name = model.rsplit("/", 1)[-1]
            entry = getattr(litellm, "model_cost", {}).get(name)
            if (
                isinstance(entry, dict)
                and entry.get("supports_native_streaming") is True
            ):
                _trace_module._request_route_trace(
                    "openai_responses_fake_stream",
                    {},
                    model=model,
                    provider=custom_llm_provider,
                    fake=False,
                    source="registered-native-streaming",
                )
                return False
        fake = original(
            self,
            model=model,
            stream=stream,
            custom_llm_provider=custom_llm_provider,
        )
        _trace_module._request_route_trace(
            "openai_responses_fake_stream",
            {},
            model=model,
            provider=custom_llm_provider,
            fake=bool(fake),
            source="litellm",
        )
        return fake

    setattr(
        patched_should_fake_stream,
        _OPENAI_RESPONSES_FAKE_STREAM_PATCH_ATTR,
        True,
    )
    setattr(patched_should_fake_stream, "_young_router_original", original)
    OpenAIResponsesAPIConfig.should_fake_stream = patched_should_fake_stream


def install_all() -> None:
    _install_latin1_response_headers_patch()
    _install_anthropic_unversioned_endpoint_patch()
    # Anthropic caches only what the request marks, and a Responses turn has no
    # place to mark it: without this every Codex thread pays full input price.
    _enable_anthropic_prompt_caching()
    _install_model_listing_efforts_patch()
    # Claude Desktop's picker discovers from /v1/models; the view is installed
    # beside the listing patch so both listing branches publish it.
    from . import claude_desktop_listing as _claude_desktop_listing

    _claude_desktop_listing.install_patch()
    _install_chatgpt_account_credential_patch()
    _install_pre_call_token_count_patch()
    _install_routing_constraint_patch()
    _install_selected_deployment_marker_patch()
    _install_order_peer_failover_patch()
    _install_generic_deployment_failover_patch()
    _install_same_deployment_retry_policy_patch()
    _install_responses_websocket_http_bridge_patch()
    _install_stream_chunk_restamp_patch()
    _install_stream_chunk_builder_dict_function_call_patch()
    _install_websocket_frame_limit_patch()
    # A stream this process truncates while it is being replaced is not
    # upstream evidence, so the teardown is marked wherever uvicorn is run.
    _install_uvicorn_shutdown_marker_patch()
    _install_managed_responses_websocket_keepalive_patch()
    _install_responses_completion_stream_patch()
    _install_openai_responses_fake_stream_patch()
    _install_responses_tool_search_bridge_patch()
    # The bridged replay is repaired here, before a thinking upstream judges it:
    # the client's reasoning item does not survive the Responses-to-Chat
    # transformation when its payload is only ``encrypted_content``.
    _install_usage_cache_read_no_overwrite_patch()
    _install_responses_bridge_cache_usage_patch()
    _install_responses_chat_bridge_reasoning_replay_patch()
    # The managed proxy's idle loops are not part of request handling, but every
    # worker and the supervisor run them, so they are installed with the rest.
    _proxy_idle_module.install_proxy_idle_patches()
