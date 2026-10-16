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

Request-side helpers return ``None`` when the request does not need the
adaptation, so the ordered step lists in :mod:`young_router.proxy.hook` and
:mod:`young_router.proxy.responses_execution` can run them unchanged.
"""

from __future__ import annotations

from . import request_context as _request_context_module
from .base import Any, Optional

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
_CODEX_TOOL_REGISTRY_ENV = "YOUNG_ROUTER_CODEX_TOOL_REGISTRY"

# A call whose arguments name a tool the request never declared is answered
# with this code so the turn ends as a stated protocol fact.
UNDECLARED_TOOL_CODE = "young_router_undeclared_tool"


def _is_codex_responses_request(request_kwargs: Optional[dict]) -> bool:
    """Whether this request is a Codex client's Responses turn."""

    if not _responses_request_module._request_has_responses_shape(request_kwargs):
        return False
    if _responses_request_module._request_is_codex_compaction(request_kwargs):
        return False
    return _responses_request_module._request_has_codex_client_evidence(request_kwargs)


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
        and _responses_request_module._codex_tool_choice_name(request_kwargs.get("tool_choice")) is None
    ):
        # Snapshot recovery has one valid action; a generic ``required`` lets
        # the model pick an unrelated tool and repeat the same turn.
        required_tool_choice: Any = "required"
        if runtime_state in {"snapshot_missing", "snapshot_invalidated"}:
            required_tool_choice = {"type": "function", "name": "list_agents"}
        if request_kwargs.get("tool_choice") != required_tool_choice:
            modified_kwargs["tool_choice"] = required_tool_choice
            changed = True

    return modified_kwargs if changed else None


def with_progress_message_contract(
    request_kwargs: dict,
) -> Optional[dict]:
    """Hold a turn open that a previous tool-free response would have ended.

    A progress message only ends the turn because the client records it as the
    turn's outcome.  Where the replay already proves that happened,
    ``tool_choice`` is the half that still holds: it makes the next response a
    call, which is a continuation by the client's own protocol.
    """

    if not _routing_module._env_bool(_CODEX_COMMENTARY_DISCIPLINE_ENV, True):
        return None
    if not _is_codex_responses_request(request_kwargs):
        return None
    # A request that declares no tool cannot make a call instead of talking.
    if not _responses_request_module._codex_declared_tool_names(request_kwargs):
        return None
    if not _responses_request_module._codex_replay_has_tool_free_progress_message(request_kwargs):
        return None
    if _routing_module._protocol_fallback_relax_tool_choice(request_kwargs):
        return None
    if request_kwargs.get("tool_choice") not in (None, "auto"):
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["tool_choice"] = "required"
    return modified_kwargs


# The protocol-layer replacements for the three former prompt steps, in the
# order the request passes through them.
CODEX_CONTRACT_REQUEST_STEPS = (
    with_tool_registry_contract,
    with_descendant_cleanup_barrier,
    with_progress_message_contract,
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
    "with_tool_registry_contract",
]
