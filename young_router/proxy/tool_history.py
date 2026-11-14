"""Keep a replayed tool history paired before a route is asked to accept it.

Every wire here validates the same invariant: a tool result belongs to a tool
call that is still in the history, and a tool call is answered before the next
turn starts.  A client whose turn was cut -- an interrupted tool, a compaction
that kept one half, a thread resumed from a summary -- replays a history that
breaks it, and the route answers ``400 orphaned tool_result in conversation
history`` (or the same verdict in a station's own words: ``工具记录不完整``,
``No tool call found for function call output``, ``unexpected tool_use_id``).
That refusal is about the replay, not about the route, so resending the same
bytes meets it again and the turn ends on "start a new task".

The pairing is recoverable, and both halves are the client's own data:

* a **result with no call** answers nothing the model can read, and the route
  refuses the whole body because of it.  It leaves, and the trace says so.
* a **call with no result** is worse: the history is being replayed so the
  model can continue, and a wire that requires every call to be answered
  refuses the turn.  It is answered with the placeholder the clients' own
  history sanitizers write -- "no result was recorded", marked as an error --
  so the call stays in the transcript and the model reads what happened.

Nothing else is touched.  Ids, order, block types and every body stay exactly as
replayed; only the two halves of the invariant move.  A history that already
pairs is reported as ``None`` ("nothing to do"), so a caller never resends a
body it did not change.

Why here and not on a refusal: this runs in ``async_pre_call_hook``, before a
deployment is chosen, so every wire, every bridge and every hop sees the same
paired replay.  Repairing after a 400 instead would pay one refused round trip
per thread and could not help a client that keeps the broken history forever.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

TRACE_EVENT = "tool_history_replay_paired"

#: What an unanswered call is given instead of an output.  It states the fact
#: and nothing else: the model must not read an invented success, and the user
#: must not read a tool that ran.
MISSING_RESULT_TEXT = (
    "No result was recorded for this tool call: the turn that made it was "
    "interrupted before the tool answered."
)

#: Responses calls and their outputs.  ``custom_tool_call`` is the same record
#: under the wire's other spelling, and the Chat bridge converts one to the
#: other -- so a call and its result may be replayed under different types and
#: still pair by id.  ``local_shell_call``/``computer_call`` are hosted calls
#: this proxy never emits an output for (the wire answers them itself), so they
#: are deliberately absent: a placeholder there would answer a call the upstream
#: is still running.
_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})

#: The Messages spelling.  There the pairing is ``tool_use.id`` against
#: ``tool_result.tool_use_id``, inside turns that may carry several of either.
_BLOCK_CALL = "tool_use"
_BLOCK_RESULT = "tool_result"


def _item_type(item: Any) -> Optional[str]:
    if not isinstance(item, Mapping):
        return None
    value = item.get("type")
    return value if isinstance(value, str) else None


def _identifiers(item: Mapping[str, Any]) -> Tuple[str, ...]:
    """Every id this record can be paired by, ``call_id`` first.

    A Responses call carries ``call_id`` (the id the client uses) and ``id``
    (the item id).  Its output normally carries only ``call_id``; a client that
    resumed a thread has been seen to replay the item id instead, so both are
    accepted rather than one being preferred.
    """

    values: List[str] = []
    for key in ("call_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return tuple(values)


def _output_for(call: Mapping[str, Any], call_type: str) -> Dict[str, Any]:
    """The placeholder answer for one unanswered call.

    Only the fields the Responses wire declares for an output are written.  The
    error marker the Messages wire has (``is_error`` on a ``tool_result``) has no
    equivalent here, and a strict validator refuses a member it does not know;
    the sentence itself is what tells the model the call produced nothing.
    """

    output: Dict[str, Any] = {
        "type": (
            "custom_tool_call_output"
            if call_type == "custom_tool_call"
            else "function_call_output"
        ),
        "output": MISSING_RESULT_TEXT,
    }
    call_id = call.get("call_id")
    if not (isinstance(call_id, str) and call_id):
        call_id = call.get("id")
    if isinstance(call_id, str) and call_id:
        output["call_id"] = call_id
    name = call.get("name")
    if isinstance(name, str) and name:
        output["name"] = name
    return output


def _paired_responses_items(
    items: List[Any],
) -> Tuple[Optional[List[Any]], Dict[str, int]]:
    """Pair Responses calls and outputs.  ``None`` when they already pair."""

    declared: Dict[str, str] = {}
    answered: set = set()
    for item in items:
        kind = _item_type(item)
        if kind in _CALL_TYPES:
            for identifier in _identifiers(item):
                declared.setdefault(identifier, kind)
        elif kind in _OUTPUT_TYPES:
            for identifier in _identifiers(item):
                if identifier in declared:
                    answered.add(identifier)

    stats = {"dropped_orphan_outputs": 0, "answered_missing_outputs": 0}
    kept: List[Any] = []
    for item in items:
        if _item_type(item) in _OUTPUT_TYPES:
            identifiers = _identifiers(item)
            # A result whose call is not in this replay answers nothing.  Every
            # identifier has to be unknown for that: one live id is enough to
            # keep the result, because a client may replay either spelling.
            if identifiers and not any(
                identifier in declared for identifier in identifiers
            ):
                stats["dropped_orphan_outputs"] += 1
                continue
        kept.append(item)

    paired: List[Any] = []
    for item in kept:
        paired.append(item)
        kind = _item_type(item)
        if kind not in _CALL_TYPES:
            continue
        identifiers = _identifiers(item)
        if not identifiers or any(
            identifier in answered for identifier in identifiers
        ):
            continue
        # The placeholder goes immediately after the call: a Chat surface reads
        # the result of a call from the message that follows it, so anything
        # placed in between turns the answer into the answer of *that* record.
        paired.append(_output_for(item, kind))
        stats["answered_missing_outputs"] += 1
        answered.update(identifiers)

    if not any(stats.values()):
        return None, stats
    return paired, stats


def _blocks(item: Any) -> Optional[List[Any]]:
    if not isinstance(item, Mapping):
        return None
    content = item.get("content")
    return content if isinstance(content, list) else None


def _paired_messages(
    messages: List[Any],
) -> Tuple[Optional[List[Any]], Dict[str, int]]:
    """Pair ``tool_use`` blocks with ``tool_result`` blocks inside turns."""

    declared: Dict[str, int] = {}
    answered: set = set()
    for index, message in enumerate(messages):
        for block in _blocks(message) or ():
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == _BLOCK_CALL:
                identifier = block.get("id")
                if isinstance(identifier, str) and identifier:
                    declared.setdefault(identifier, index)
            elif kind == _BLOCK_RESULT:
                identifier = block.get("tool_use_id")
                if isinstance(identifier, str) and identifier:
                    answered.add(identifier)

    stats = {"dropped_orphan_results": 0, "answered_missing_results": 0}
    kept_messages: List[Any] = []
    # Where each original turn landed, because a turn that leaves shifts every
    # turn after it and the answers below are placed by the call turn's index.
    kept_index: Dict[int, int] = {}
    for index, message in enumerate(messages):
        blocks = _blocks(message)
        if blocks is None:
            kept_index[index] = len(kept_messages)
            kept_messages.append(message)
            continue
        kept: List[Any] = []
        for block in blocks:
            identifier = (
                block.get("tool_use_id") if isinstance(block, Mapping) else None
            )
            if (
                isinstance(block, Mapping)
                and block.get("type") == _BLOCK_RESULT
                and isinstance(identifier, str)
                and identifier
                and identifier not in declared
            ):
                stats["dropped_orphan_results"] += 1
                continue
            kept.append(block)
        if len(kept) == len(blocks):
            kept_index[index] = len(kept_messages)
            kept_messages.append(message)
            continue
        if not kept:
            # The turn carried only that orphan result.  Messages alternates
            # user/assistant, so an empty turn is itself a shape the wire
            # refuses: the message leaves together with its blocks.
            continue
        updated = dict(message)
        updated["content"] = kept
        kept_index[index] = len(kept_messages)
        kept_messages.append(updated)

    unanswered = [
        identifier for identifier in declared if identifier not in answered
    ]
    if not unanswered:
        if stats["dropped_orphan_results"]:
            return kept_messages, stats
        return None, stats

    # An unanswered call is answered in the turn that follows its own call
    # turn.  Messages requires the results to be in the *next* user message, and
    # a client that lost one usually still has that message (it holds the other
    # results of the same parallel batch).  Every answer goes in at once, so the
    # turn is rewritten once rather than shifted under the loop.
    missing_by_turn: Dict[int, List[str]] = {}
    for identifier in unanswered:
        turn = kept_index.get(declared[identifier])
        if turn is None:
            continue
        missing_by_turn.setdefault(turn, []).append(identifier)
    for index in sorted(missing_by_turn, reverse=True):
        results = [
            {
                "type": _BLOCK_RESULT,
                "tool_use_id": identifier,
                "content": MISSING_RESULT_TEXT,
                "is_error": True,
            }
            for identifier in missing_by_turn[index]
        ]
        target = index + 1
        following = kept_messages[target] if target < len(kept_messages) else None
        blocks = _blocks(following)
        if blocks is not None and following.get("role") == "user":
            updated = dict(following)
            # Results come first in the content array and stay together: a text
            # block after them is what the wire reads as the user speaking, and
            # a result placed after a text block is a shape it refuses.  So the
            # answer joins the run of results that already opens this turn, or
            # opens the turn itself when it has none.
            cut = 0
            for block in blocks:
                if isinstance(block, Mapping) and block.get("type") == _BLOCK_RESULT:
                    cut += 1
                    continue
                break
            updated["content"] = list(blocks[:cut]) + results + list(blocks[cut:])
            kept_messages[target] = updated
        else:
            kept_messages.insert(target, {"role": "user", "content": results})
        stats["answered_missing_results"] += len(results)

    return kept_messages, stats


def paired_request(data: Any) -> Optional[Dict[str, Any]]:
    """Return this replay with its tool history paired, or ``None``.

    ``None`` means the replay already pairs, carries no history this pass
    understands, or continues a thread the upstream holds itself: with
    ``previous_response_id`` the history is not in this body at all, so there is
    nothing here to pair and nothing to insert.
    """

    if not isinstance(data, Mapping):
        return None
    if data.get("previous_response_id"):
        return None
    items = data.get("input")
    if isinstance(items, list) and items:
        paired, stats = _paired_responses_items(items)
        if paired is None:
            return None
        result = dict(data)
        result["input"] = paired
        _trace(data, stats)
        return result
    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        paired, stats = _paired_messages(messages)
        if paired is None:
            return None
        result = dict(data)
        result["messages"] = paired
        _trace(data, stats)
        return result
    return None


def _trace(data: Mapping[str, Any], stats: Mapping[str, int]) -> None:
    from . import trace as _trace_module

    try:
        _trace_module._route_trace(
            TRACE_EVENT,
            model=data.get("model"),
            **{key: value for key, value in stats.items() if value},
        )
    except Exception:
        # A trace must never decide a request.
        return
