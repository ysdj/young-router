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

The Messages wire pairs by **position**, not by id: a ``tool_result`` is read as
the answer of the ``tool_use`` in the message before it, so a result that sits
in a later turn answers nothing even though its call is in the body.  That is
the shape a compaction produces when it keeps the user's next words and drops
the results turn around them, and what a thread resumed from a summary looks
like -- refused as ``messages.0.content.3: unexpected `tool_use_id` found in
`tool_result` blocks`` on a 1328-turn replay carrying 656 results for 655 calls.
So a misplaced result is moved to the turn after its call, the results open the
turn they answer (a text block ahead of them is read as the user speaking), and
a call answered twice keeps its first answer -- one call has one result.

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
    """Pair ``tool_use`` blocks with ``tool_result`` blocks by position.

    Ids alone do not pair this wire: a ``tool_result`` is read as the answer of
    the ``tool_use`` in the message *before* it, so a replay whose ids all have
    their call is still refused when the result sits in a later turn --
    ``messages.0.content.3: unexpected `tool_use_id` found in `tool_result`
    blocks`` on a 1328-turn body carrying 656 results for 655 calls.  A
    compaction that keeps the user's next words and drops the results turn
    around them, and a thread resumed from a summary, both produce it.  So each
    result is placed in the turn directly after its call, the results open that
    turn, and a call answered twice keeps its first answer.
    """

    declared: Dict[str, List[int]] = {}
    for index, message in enumerate(messages):
        for block in _blocks(message) or ():
            if isinstance(block, Mapping) and block.get("type") == _BLOCK_CALL:
                identifier = block.get("id")
                if isinstance(identifier, str) and identifier:
                    declared.setdefault(identifier, []).append(index)

    stats = {
        "dropped_orphan_results": 0,
        "dropped_duplicate_results": 0,
        "moved_misplaced_results": 0,
        "reordered_results": 0,
        "answered_missing_results": 0,
    }

    #: Answers that have to be written into another turn, by the turn they
    #: answer.  ``answered`` records the turn each id ended up in, so a call is
    #: answered once and a duplicate is recognisable wherever it appears.
    moved: Dict[int, List[Any]] = {}
    answered: Dict[str, int] = {}
    kept: List[Optional[List[Any]]] = []
    for index, message in enumerate(messages):
        blocks = _blocks(message)
        if blocks is None:
            kept.append(None)
            continue
        blocks_original = blocks
        results: List[Any] = []
        others: List[Any] = []
        for block in blocks:
            identifier = (
                block.get("tool_use_id") if isinstance(block, Mapping) else None
            )
            if not (
                isinstance(block, Mapping)
                and block.get("type") == _BLOCK_RESULT
                and isinstance(identifier, str)
                and identifier
            ):
                others.append(block)
                continue
            # The nearest call before this turn owns the result.  A call after
            # it, or no call at all, means the result answers nothing the model
            # can read.
            owners = [turn for turn in declared.get(identifier, ()) if turn < index]
            if not owners:
                stats["dropped_orphan_results"] += 1
                continue
            legal = max(owners) + 1
            if identifier in answered:
                stats["dropped_duplicate_results"] += 1
                continue
            answered[identifier] = legal
            if legal == index:
                results.append(block)
            else:
                moved.setdefault(legal, []).append(block)
                stats["moved_misplaced_results"] += 1
        # Results open the turn they answer.  A text block ahead of them is read
        # as the user speaking before answering, which the wire refuses.
        blocks = results + others if results else others
        if blocks != blocks_original:
            stats["reordered_results"] += 1
        kept.append(blocks)

    out: List[Any] = []
    survived: List[int] = []
    for index, message in enumerate(messages):
        blocks = kept[index]
        if blocks is None:
            survived.append(index)
            out.append(message)
            continue
        if not blocks:
            # The turn carried only results that answered nothing.  Messages
            # alternates user/assistant, so an empty turn is itself a shape the
            # wire refuses: the message leaves together with them.
            continue
        survived.append(index)
        if blocks == message.get("content"):
            out.append(message)
        else:
            updated = dict(message)
            updated["content"] = blocks
            out.append(updated)

    def _position(turn: int) -> int:
        """Where a turn sits in the repaired list, counting the ones that left."""

        return sum(1 for survived_index in survived if survived_index < turn)

    # An unanswered call is answered in the turn that follows its own call turn,
    # which is where the wire reads it from.  Every answer for one turn goes in
    # at once, so the turn is rewritten once rather than shifted under the loop.
    for identifier in declared:
        if identifier in answered:
            continue
        legal = max(declared[identifier]) + 1
        moved.setdefault(legal, []).append(
            {
                "type": _BLOCK_RESULT,
                "tool_use_id": identifier,
                "content": MISSING_RESULT_TEXT,
                "is_error": True,
            }
        )
        stats["answered_missing_results"] += 1

    # Newest turn first: an insertion shifts every position after it.
    for legal in sorted(moved, reverse=True):
        position = _position(legal)
        following = out[position] if position < len(out) else None
        blocks = _blocks(following)
        if blocks is not None and following.get("role") == "user":
            updated = dict(following)
            # The answers join the run of results that already opens this turn,
            # so a parallel batch keeps its order and the user's own words stay
            # behind them.
            cut = 0
            for block in blocks:
                if isinstance(block, Mapping) and block.get("type") == _BLOCK_RESULT:
                    cut += 1
                    continue
                break
            updated["content"] = list(blocks[:cut]) + list(moved[legal]) + list(blocks[cut:])
            out[position] = updated
        else:
            out.insert(position, {"role": "user", "content": list(moved[legal])})

    if not any(stats.values()):
        return None, stats
    return out, stats


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
