"""Codex request/response contract held in the protocol, not in a prompt.

Three Codex behaviours used to be requested by appending text to the request's
``instructions``: which tool names exist, that a root must account for its
descendants before finishing, and that a progress message is not a turn
outcome.  Prose is advisory, so each one could be ignored -- and the descendant
rule could be over-applied, spending a fresh conversation's first answer on an
inventory of an empty tree.

Each rule has a determinate carrier in the wire protocol instead, and those are
what this module builds:

* The declared tool set is request data.  A call naming a tool outside it is
  caught on the response side, where the answer is a fact rather than a hope.
* The descendant barrier is a lifecycle state machine.  Its one enforceable
  half is ``tool_choice``, which the model cannot decline.
* A tool-free progress message is only a turn outcome because the client
  records it as one.  Where the replay already proves a turn ended that way,
  ``tool_choice`` holds the next response open.
* A completed tool call whose arguments and result repeat at the replay tail is
  a no-progress loop.  ``tool_choice: none`` gives the model one final text
  response instead of asking it to execute the same call again.

Request-side helpers return ``None`` when the request does not need the
adaptation, so the ordered step lists in :mod:`young_router.proxy.hook` and
:mod:`young_router.proxy.responses_execution` can run them unchanged.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import SplitResult, urlsplit

from . import request_context as _request_context_module
from .base import (
    Any,
    Optional,
    _CURRENT_UPSTREAM_URL_SURFACE_KEY,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
)

from . import routing as _routing_module


# ``responses_request`` imports this module's step list, so the request-side
# helpers are resolved lazily: importing either module first stays safe.
class _ResponsesRequestModuleProxy:
    def __getattr__(self, name: str) -> Any:
        from . import responses_request

        return getattr(responses_request, name)


_responses_request_module = _ResponsesRequestModuleProxy()


def _metadata_key() -> str:
    return _responses_request_module._CODEX_DESCENDANT_CLEANUP_METADATA_KEY


def _tool_definition_name(tool: Any) -> Optional[str]:
    return _responses_request_module._codex_tool_definition_name(tool)


_CODEX_DESCENDANT_CLEANUP_ENV = "YOUNG_ROUTER_CODEX_DESCENDANT_CLEANUP"
_CODEX_COMMENTARY_DISCIPLINE_ENV = "YOUNG_ROUTER_CODEX_COMMENTARY_DISCIPLINE"
_INJECTED_TOOL_CHOICE_KEY = "codex_contract_injected_tool_choice"
_REPEATED_TOOL_LOOP_METADATA_KEY = "codex_repeated_tool_loop_barrier"

#: How many trailing tool-free progress messages make a replay a loop rather
#: than a continuation.  One is the ordinary shape: the client recorded the
#: previous turn's progress sentence and asks the model to pick the work back
#: up.  The second one means the model answered that continuation with another
#: progress sentence, and the client will ask again forever (2026-11-19: one
#: Codex turn spent an hour and forty-two minutes emitting them).
_CODEX_PROGRESS_LOOP_MESSAGES = 2

# Three identical call/result pairs are enough to distinguish a deliberate
# retry from the loop seen in live Codex turns (the same command and the same
# 57-byte result were replayed until the input grew without bound).
_CODEX_TOOL_LOOP_PAIRS = 3

# A call whose arguments name a tool the request never declared is answered
# with this code so the turn ends as a stated protocol fact.
UNDECLARED_TOOL_CODE = "young_router_undeclared_tool"


def _is_codex_responses_request(request_kwargs: Optional[dict]) -> bool:
    """Whether this request is a Codex client's Responses turn."""

    if not _responses_request_module._request_has_responses_shape(request_kwargs):
        return False
    if _responses_request_module._request_is_codex_compaction(request_kwargs):
        return False
    if _responses_request_module._request_has_codex_client_evidence(request_kwargs):
        return True
    # The generic deployment helper can rebuild the request after the native
    # tool passthrough has removed all client-identifying metadata.  Three
    # consecutive identical custom-call/result pairs are a stronger signal
    # than that missing metadata and are exactly the loop this contract exists
    # to stop.  Keep ordinary Responses requests untouched until the replay
    # itself proves the no-progress pattern.
    return (
        _responses_request_module._codex_replay_tool_loop_pairs(request_kwargs)
        >= _CODEX_TOOL_LOOP_PAIRS
    )


def declared_tool_names(request_kwargs: Optional[dict]) -> set[str]:
    """The complete tool surface this request declares, namespaces included."""

    return _responses_request_module._codex_declared_tool_names(request_kwargs)


def is_undeclared_tool_call(request_kwargs: Optional[dict], tool_name: Any) -> bool:
    """Whether a returned call names a tool this request never declared.

    The declared set is the contract; a name outside it cannot be executed by
    the client, so the call is a protocol error rather than a capability gap.
    """

    if not isinstance(tool_name, str) or not tool_name.strip():
        return False
    if not _is_codex_responses_request(request_kwargs):
        return False
    declared = declared_tool_names(request_kwargs)
    if not declared:
        return False
    return tool_name.strip() not in declared


def with_tool_registry_contract(
    request_kwargs: dict,
) -> Optional[dict]:
    """State the declared tool surface without writing prose into ``instructions``.

    The registry is already carried by the request's ``additional_tools``
    items, which the client sends and the model reads.  Nothing has to be
    appended to make the set knowable; the enforcement lives in
    :func:`is_undeclared_tool_call` and the response-side answer below.
    """

    del request_kwargs
    return None


def _request_targets_official_deepseek(request_kwargs: dict) -> bool:
    params = request_kwargs.get("litellm_params")
    api_base = params.get("api_base") if isinstance(params, dict) else None
    if not isinstance(api_base, str):
        api_base = _responses_request_module._request_api_base(request_kwargs)
    if api_base:
        return _responses_request_module._api_base_host(api_base) == "api.deepseek.com"
    params = params if isinstance(params, dict) else {}
    model = params.get("model") or request_kwargs.get("model")
    provider = params.get("custom_llm_provider") or request_kwargs.get("custom_llm_provider")
    if not (provider == "deepseek" or isinstance(model, str) and model.startswith("deepseek/")):
        return False
    configured_base = (
        getattr(sys.modules.get("litellm"), "api_base", None)
        or os.getenv("DEEPSEEK_API_BASE", "")
    )
    if configured_base:
        return _responses_request_module._api_base_host(configured_base) == "api.deepseek.com"
    return True


def _tool_choice_upstream_surface(request_kwargs: dict, call_type: Any = None) -> str:
    surface = _routing_module._request_current_upstream_surface(request_kwargs)
    if surface == _UPSTREAM_URL_SURFACE_ANTHROPIC:
        return surface
    if request_kwargs.get("use_chat_completions_api") is True:
        return _UPSTREAM_URL_SURFACE_OPENAI_CHAT
    if surface:
        return surface
    if call_type is not None:
        request_kwargs = {
            **request_kwargs,
            "call_type": getattr(call_type, "value", call_type),
        }
    return _routing_module._request_client_surface(request_kwargs)


def _deepseek_thinking_rejects_forced_choice(request_kwargs: dict) -> bool:
    """Whether the selected official DeepSeek wire rejects a forced choice."""

    if not _request_targets_official_deepseek(request_kwargs):
        return False
    params = request_kwargs.get("litellm_params")
    for container in (request_kwargs.get("extra_body"), request_kwargs, params):
        if not isinstance(container, dict):
            continue
        thinking = container.get("thinking")
        if isinstance(thinking, dict):
            if thinking.get("type") == "enabled":
                return True
            if thinking.get("type") == "disabled":
                return False
    # Chat uses the native thinking switch. A Responses effort of none only
    # disables thinking when the selected call is actually sent as Responses.
    if _tool_choice_upstream_surface(request_kwargs) == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        reasoning = request_kwargs.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") == "none":
            return False
    return True


def _deepseek_forced_tool_choice(request_kwargs: dict) -> Any:
    # OpenAI's extra body is merged into the provider body last. It can also
    # supply the choice when the client SDK leaves the top-level field empty.
    extra = request_kwargs.get("extra_body")
    params = request_kwargs.get("litellm_params")
    for container in (extra, request_kwargs, params):
        if not isinstance(container, dict) or container.get("tool_choice") is None:
            continue
        choice = container["tool_choice"]
        kind = choice.get("type") if isinstance(choice, dict) else choice
        if kind in ("required", "any") or (
            kind in ("function", "tool", "custom")
            and _responses_request_module._codex_tool_choice_name(choice)
        ):
            return choice
        break
    return None


def _with_deepseek_thinking_passthrough(request_kwargs: dict, surface: str) -> dict:
    """Keep the native Chat thinking switch through LiteLLM's OpenAI adapter."""

    thinking = request_kwargs.get("thinking")
    if surface != _UPSTREAM_URL_SURFACE_OPENAI_CHAT or not isinstance(thinking, dict):
        return request_kwargs
    extra = request_kwargs.get("extra_body")
    if isinstance(extra, dict) and "thinking" in extra:
        return request_kwargs
    updated = request_kwargs.copy()
    updated["extra_body"] = {**(extra if isinstance(extra, dict) else {}), "thinking": thinking}
    return updated


def _with_deepseek_automatic_tool_choice(request_kwargs: dict, surface: str) -> dict:
    """Adapt a forced choice without altering the request's thinking settings."""

    updated = request_kwargs.copy()
    updated["tool_choice"] = (
        {"type": "auto"} if surface == _UPSTREAM_URL_SURFACE_ANTHROPIC else "auto"
    )
    updated.pop("function_call", None)
    for key in ("litellm_params", "extra_body"):
        nested = updated.get(key)
        if isinstance(nested, dict) and ("tool_choice" in nested or "function_call" in nested):
            updated[key] = {
                name: value
                for name, value in nested.items()
                if name not in ("tool_choice", "function_call")
            }
    return _with_deepseek_thinking_passthrough(updated, surface)


def _tool_choice_refusal_endpoint(exception: Exception) -> Optional[SplitResult]:
    # LiteLLM's Responses error wrapper uses an OpenAI base URL as a stub. The
    # original HTTP failure can still carry the endpoint in the exception chain.
    seen = set()
    error = exception
    while isinstance(error, Exception) and id(error) not in seen:
        seen.add(id(error))
        request = getattr(error, "request", None)
        url = getattr(request, "url", None)
        if url is not None and getattr(request, "method", None) == "POST":
            endpoint = urlsplit(str(url))
            stub = (endpoint.hostname, endpoint.path.rstrip("/"))
            if stub not in {("api.openai.com", "/v1"), ("docs.litellm.ai", "/docs")}:
                return endpoint
        error = error.__cause__ or error.__context__
    return None


def _deepseek_tool_choice_retry_kwargs(
    exception: Exception, request_kwargs: dict, call_type: Any
) -> Optional[dict]:
    if not _routing_module._is_thinking_tool_choice_unsupported_error(exception):
        return None
    if _deepseek_forced_tool_choice(request_kwargs) is None:
        return None

    endpoint = _tool_choice_refusal_endpoint(exception)
    if endpoint and endpoint.hostname != "api.deepseek.com":
        return None
    if endpoint is None and not _request_targets_official_deepseek(request_kwargs):
        return None

    expected_surface = _tool_choice_upstream_surface(request_kwargs, call_type)
    surface = expected_surface
    path = endpoint.path.rstrip("/") if endpoint else ""
    if path.endswith("/responses"):
        surface = _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    elif path.endswith("/chat/completions"):
        surface = _UPSTREAM_URL_SURFACE_OPENAI_CHAT
    elif path.endswith("/messages"):
        surface = _UPSTREAM_URL_SURFACE_ANTHROPIC
    updated = _with_deepseek_automatic_tool_choice(request_kwargs, surface)
    if surface and surface != expected_surface:
        updated[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface
    return updated


async def _call_with_deepseek_tool_choice(
    call: Any, request_kwargs: dict, call_type: Any = None, *, prepare_retry: Any = None
) -> Any:
    """Repair a rejected combination before any upstream response is delivered."""

    if _request_targets_official_deepseek(request_kwargs):
        request_kwargs = _with_deepseek_thinking_passthrough(
            request_kwargs, _tool_choice_upstream_surface(request_kwargs, call_type)
        )
    if (
        _deepseek_forced_tool_choice(request_kwargs) is not None
        and _deepseek_thinking_rejects_forced_choice(request_kwargs)
    ):
        request_kwargs = _with_deepseek_automatic_tool_choice(
            request_kwargs, _tool_choice_upstream_surface(request_kwargs, call_type)
        )
        from . import trace as _trace_module

        _trace_module._request_route_trace(
            "deepseek_forced_tool_choice_adapted", request_kwargs, automatic=True
        )

    try:
        return await call(**request_kwargs)
    except Exception as exc:
        failed_request = prepare_retry(request_kwargs) if prepare_retry is not None else request_kwargs
        if failed_request is None:
            raise
        retry = _deepseek_tool_choice_retry_kwargs(exc, failed_request, call_type)
        if retry is None:
            raise
        from . import trace as _trace_module

        _trace_module._request_route_trace(
            "deepseek_forced_tool_choice_repaired", retry, automatic=True
        )
        return await call(**retry)


def _with_injected_tool_choice(request_kwargs: dict, choice: Any) -> dict:
    """Remember the caller's choice when a Codex contract replaces it."""

    modified = request_kwargs.copy()
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    metadata = metadata.copy()
    previous = metadata.get(_INJECTED_TOOL_CHOICE_KEY)
    if not isinstance(previous, dict):
        metadata[_INJECTED_TOOL_CHOICE_KEY] = {
            "present": "tool_choice" in request_kwargs,
            "original": request_kwargs.get("tool_choice"),
            "injected": choice,
        }
    else:
        metadata[_INJECTED_TOOL_CHOICE_KEY] = {**previous, "injected": choice}
    modified["litellm_metadata"] = metadata
    modified["tool_choice"] = choice
    return modified


def _has_injected_tool_choice(request_kwargs: dict) -> bool:
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    injected = metadata.get(_INJECTED_TOOL_CHOICE_KEY)
    if not isinstance(injected, dict) or injected.get("original") not in (None, "auto"):
        return False
    current = request_kwargs.get("tool_choice")
    original_injection = injected.get("injected")
    current_name = _responses_request_module._codex_tool_choice_name(current)
    injected_name = _responses_request_module._codex_tool_choice_name(original_injection)
    return current == original_injection or bool(
        current_name and current_name == injected_name
    )


def _restore_unsupported_injected_tool_choice(request_kwargs: dict) -> Optional[dict]:
    """Undo an earlier contract injection after selecting an incompatible wire."""

    if not _deepseek_thinking_rejects_forced_choice(request_kwargs):
        return None
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    if not _has_injected_tool_choice(request_kwargs):
        return None
    injected = metadata[_INJECTED_TOOL_CHOICE_KEY]
    modified = request_kwargs.copy()
    if injected.get("present"):
        modified["tool_choice"] = injected.get("original")
    else:
        modified.pop("tool_choice", None)
    metadata = metadata.copy()
    metadata.pop(_INJECTED_TOOL_CHOICE_KEY, None)
    modified["litellm_metadata"] = metadata
    return modified


def with_descendant_cleanup_barrier(
    request_kwargs: dict,
) -> Optional[dict]:
    """Hold a root turn open with ``tool_choice`` while its tree is unresolved.

    The barrier itself is the metadata state machine
    (``_codex_descendant_cleanup_runtime_state``): it answers whether this root
    still owes an account of its descendants.  When it does, the one
    enforceable half is the named ``tool_choice`` that makes the next response
    a call; the rule text was never what stopped a model from finishing early.
    """

    if not _routing_module._env_bool(_CODEX_DESCENDANT_CLEANUP_ENV, True):
        return None
    if not _is_codex_responses_request(request_kwargs):
        return None

    runtime_state = _responses_request_module._codex_descendant_cleanup_runtime_state(request_kwargs)
    if runtime_state is None:
        return None

    modified_kwargs = request_kwargs.copy()
    changed = False

    metadata = (
        _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    state_metadata = {"state": runtime_state, "tool_call_required": True}
    if metadata.get(_metadata_key()) != state_metadata:
        next_metadata = metadata.copy()
        next_metadata[_metadata_key()] = state_metadata
        modified_kwargs["litellm_metadata"] = next_metadata
        changed = True

    # A protocol fallback reaches this step again after the surface adapter
    # deliberately relaxed a named choice the upstream rejected, so the barrier
    # keeps its state but does not re-inject the refused choice.
    if (
        not _routing_module._protocol_fallback_relax_tool_choice(request_kwargs)
        and not _deepseek_thinking_rejects_forced_choice(request_kwargs)
        and request_kwargs.get("tool_choice") in (None, "auto")
    ):
        # Snapshot recovery has one valid action; a generic ``required`` lets
        # the model pick an unrelated tool and repeat the same turn.
        required_tool_choice: Any = "required"
        if runtime_state in {"snapshot_missing", "snapshot_invalidated"}:
            required_tool_choice = {"type": "function", "name": "list_agents"}
        if request_kwargs.get("tool_choice") != required_tool_choice:
            modified_kwargs = _with_injected_tool_choice(modified_kwargs, required_tool_choice)
            changed = True

    return modified_kwargs if changed else None


# The string ``reasoning`` spelling and the block's ``effort`` name the same
# ladder, so one set decides for both whether thinking was asked for: a split
# here flips the progress barrier with the spelling alone.
_THINKING_OFF_VALUES = frozenset({"", "none", "off", "null", "false", "minimal"})


def _request_thinks(request_kwargs: dict) -> bool:
    """Whether this turn asks the model to think before it answers.

    Any ``reasoning`` block with an effort or a summary is that request; an
    explicit off value is not.
    """

    reasoning = request_kwargs.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning.strip().casefold() not in _THINKING_OFF_VALUES
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort.strip().casefold() not in _THINKING_OFF_VALUES:
            return True
        summary = reasoning.get("summary")
        if isinstance(summary, str) and summary.strip().casefold() not in _THINKING_OFF_VALUES:
            return True
        return False
    summary = request_kwargs.get("model_reasoning_summary")
    if isinstance(summary, str):
        return summary.strip().casefold() not in _THINKING_OFF_VALUES
    return False


def with_progress_message_contract(
    request_kwargs: dict,
) -> Optional[dict]:
    """Hold a turn open that a previous tool-free response would have ended.

    A progress message only ends the turn because the client records it as the
    turn's outcome.  Where the replay already proves that happened,
    ``tool_choice`` is the half that still holds: it makes the next response a
    call, which is a continuation by the client's own protocol.

    The official DeepSeek thinking API accepts automatic choice but rejects
    required or named choices. On that route the model chooses whether to call
    a tool, so this request-side barrier cannot enforce a call.
    """

    if not _routing_module._env_bool(_CODEX_COMMENTARY_DISCIPLINE_ENV, True):
        return None
    if not _is_codex_responses_request(request_kwargs):
        return None
    # A request that declares no tool cannot make a call instead of talking.
    if not _responses_request_module._codex_declared_tool_names(request_kwargs):
        return None
    trailing_progress = (
        _responses_request_module._codex_replay_trailing_progress_messages(
            request_kwargs
        )
    )
    if trailing_progress < 1:
        return None
    if _routing_module._protocol_fallback_relax_tool_choice(request_kwargs):
        return None
    if _deepseek_thinking_rejects_forced_choice(request_kwargs):
        return None
    if request_kwargs.get("tool_choice") not in (None, "auto"):
        return None
    thinking = _request_thinks(request_kwargs)
    if thinking and trailing_progress < _CODEX_PROGRESS_LOOP_MESSAGES:
        return None

    modified_kwargs = _with_injected_tool_choice(request_kwargs, "required")
    # The request summary is taken before the deployment pre-call hook runs, so
    # a metadata flag written here would never appear in a trace.  The event is
    # what makes an injected ``required`` tell itself apart from a client's own
    # choice later.  Imported here because ``trace`` imports the callers of
    # this module at import time.
    from . import trace as _trace_module

    _trace_module._request_route_trace(
        "codex_progress_barrier",
        request_kwargs,
        trailing_progress_messages=trailing_progress,
        thinking=thinking,
        forced_tool_choice="required",
    )
    return modified_kwargs


def with_repeated_tool_loop_contract(
    request_kwargs: dict,
    *,
    allow_existing_tool_choice: bool = False,
) -> Optional[dict]:
    """End a no-progress tool loop with one ordinary text response.

    ``max_messages`` continuations are valid, but the client can replay a
    completed call and ask the model to continue indefinitely.  When the same
    call *and* result form the tail three times, forcing another tool call only
    repeats the work.  A caller-supplied choice, a descendant cleanup barrier,
    and a route that rejects forced choices all keep their existing contract.
    """

    if not _is_codex_responses_request(request_kwargs):
        return None
    repeated_pairs = _responses_request_module._codex_replay_tool_loop_pairs(
        request_kwargs
    )
    if repeated_pairs < _CODEX_TOOL_LOOP_PAIRS:
        return None

    current_choice = request_kwargs.get("tool_choice")
    metadata = (
        _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    injected = metadata.get(_INJECTED_TOOL_CHOICE_KEY)
    injected_choice = (
        isinstance(injected, dict)
        and injected.get("original") in (None, "auto")
        and current_choice == injected.get("injected")
    )
    # An active descendant still needs its required lifecycle call.  A missing
    # or invalidated snapshot, however, is itself part of the no-progress loop
    # once the same call/result has repeated three times; let the final text
    # response break that loop so the client can start a fresh action.
    cleanup_state = _responses_request_module._codex_descendant_cleanup_runtime_state(
        request_kwargs
    )
    cleanup_choice = cleanup_state is not None and (
        current_choice == "required"
        or (
            isinstance(current_choice, dict)
            and current_choice.get("type") == "function"
            and current_choice.get("name") == "list_agents"
        )
    )
    if (
        current_choice not in (None, "auto")
        and not injected_choice
        and not cleanup_choice
        and not allow_existing_tool_choice
    ):
        return None
    if cleanup_state == "active_descendants":
        # A stale active snapshot must not keep an ordinary command spinning
        # forever.  Lifecycle calls still retain the barrier: a repeated
        # ``spawn_agent``, ``followup_task``, ``interrupt_agent`` or
        # ``list_agents`` is itself the cleanup work that must finish.
        repeated_tool_name = (
            _responses_request_module._codex_replay_tool_loop_tail_name(
                request_kwargs
            )
        )
        if repeated_tool_name in {
            "followup_task",
            "interrupt_agent",
            "spawn_agent",
            "list_agents",
        }:
            return None
    if _deepseek_thinking_rejects_forced_choice(request_kwargs):
        return None

    modified_kwargs = _with_injected_tool_choice(request_kwargs, "none")
    metadata = (
        _request_context_module._request_metadata_dict(
            modified_kwargs,
            "litellm_metadata",
        )
        or {}
    ).copy()
    metadata[_REPEATED_TOOL_LOOP_METADATA_KEY] = {
        "repeated_tool_pairs": repeated_pairs,
        "terminal_response": True,
    }
    modified_kwargs["litellm_metadata"] = metadata
    from . import trace as _trace_module

    _trace_module._request_route_trace(
        "codex_repeated_tool_loop_barrier",
        request_kwargs,
        repeated_tool_pairs=repeated_pairs,
        forced_tool_choice="none",
    )
    return modified_kwargs


# The protocol-layer replacements for the three former prompt steps, in the
# order the request passes through them.
CODEX_CONTRACT_REQUEST_STEPS = (
    _restore_unsupported_injected_tool_choice,
    with_tool_registry_contract,
    with_descendant_cleanup_barrier,
    with_progress_message_contract,
    with_repeated_tool_loop_contract,
)


def undeclared_tool_response_event(
    tool_name: str,
    call_id: Any,
    request_data: Optional[dict] = None,
) -> dict[str, Any]:
    """The terminal answer for a call naming a tool this request never declared."""

    message = (
        f"The model called `{tool_name}`, which this request never declared. "
        "The declared tool set is fixed by the request itself, so the call "
        "cannot be executed."
    )
    return {
        "type": "response.failed",
        "response": {
            "id": f"resp_{UNDECLARED_TOOL_CODE}",
            "status": "failed",
            "error": {
                "code": UNDECLARED_TOOL_CODE,
                "message": message,
                "tool_name": tool_name,
                "call_id": call_id,
            },
            "output": [],
        },
    }


__all__ = [
    "CODEX_CONTRACT_REQUEST_STEPS",
    "UNDECLARED_TOOL_CODE",
    "declared_tool_names",
    "is_undeclared_tool_call",
    "undeclared_tool_response_event",
    "with_descendant_cleanup_barrier",
    "with_progress_message_contract",
    "with_repeated_tool_loop_contract",
    "with_tool_registry_contract",
]
