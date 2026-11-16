"""A picture inside a tool result is not a picture to the upstream.

The Messages wire lets a ``tool_result`` carry an ``image`` part -- that is what
``Read`` of a JPEG returns, and a Claude Code thread looking at figures produces
one every time.  An OpenAI-compatible upstream cannot read that shape: the
bridge turns the result into a Chat ``tool`` message and the picture inside it
is serialised as a data URL, which the upstream tokenizes as prose.

Measured against the live relay on 2026-11-16, one 473 KB JPEG:

===========================  ==============  ==============================
shape                        input_tokens    what the model said
===========================  ==============  ==============================
``image`` inside a result     327,716        "the image did not render"
``image`` as a sibling part     1,053        described the four panels
===========================  ==============  ==============================

300x the price, and the picture is invisible in the cheaper-to-produce but
expensive-to-send shape.  Five such figures put a 300k-token thread over a
1M-token window, so the context pass elided its history on every step and the
model answered by reading the same file again.

What this module does is move the picture, and nothing else.  It becomes a
sibling ``image`` block in the turn that already carries the result, behind a
caption; the result keeps its text and gains a note counting what left it.  The
pairing the wire validates is untouched -- the result stays first in its turn,
still answering the same ``tool_use_id``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

TRACE_EVENT = "tool_result_images_promoted"

#: What a result says in place of the pictures that left it.  It states the
#: fact and where they went; it is not an instruction, for the reason
#: ``context_window._marker`` documents at length.
_LIFTED_NOTE = "[{count} image{s} returned by this tool, shown below]"

#: What the promoted picture is introduced with.  Text ahead of an image in the
#: same turn is read as the user speaking, which is what this line is.
_CAPTION = "The image the tool returned:"


def _image_blocks(block: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The image parts inside one ``tool_result``, in order."""

    content = block.get("content")
    if not isinstance(content, list):
        return []
    return [
        part
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "image"
    ]


def _promote_turn(blocks: List[Any]) -> Optional[List[Any]]:
    """One turn with its results' pictures lifted, or ``None`` if it has none."""

    lifted: List[Any] = []
    rewritten: List[Any] = []
    for block in blocks:
        if not isinstance(block, Mapping) or block.get("type") != "tool_result":
            rewritten.append(block)
            continue
        pictures = _image_blocks(block)
        if not pictures:
            rewritten.append(block)
            continue
        kept = [
            part
            for part in block["content"]
            if not (isinstance(part, Mapping) and part.get("type") == "image")
        ]
        kept.append(
            {
                "type": "text",
                "text": _LIFTED_NOTE.format(
                    count=len(pictures), s="" if len(pictures) == 1 else "s"
                ),
            }
        )
        updated = dict(block)
        updated["content"] = kept
        rewritten.append(updated)
        lifted.extend(pictures)
    if not lifted:
        return None
    # The pictures go behind the turn's run of results.  A text block ahead of
    # a result is read as the user speaking before answering, which the wire
    # refuses; behind the results it is the caption for what follows.
    cut = 0
    for block in rewritten:
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            cut += 1
            continue
        break
    tail: List[Any] = [{"type": "text", "text": _CAPTION}, *lifted]
    return list(rewritten[:cut]) + tail + list(rewritten[cut:])


def promoted_tool_result_images(data: Any) -> Optional[Dict[str, Any]]:
    """This replay with tool-result pictures lifted, or ``None``.

    ``None`` means there was nothing to lift -- no ``messages``, no picture
    inside a result, or a result whose pictures are references rather than
    bytes.  A caller never resends a body this pass did not change.
    """

    if not isinstance(data, Mapping):
        return None
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    out: List[Any] = []
    results = 0
    pictures = 0
    for message in messages:
        blocks = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(blocks, list):
            out.append(message)
            continue
        rewritten = _promote_turn(blocks)
        if rewritten is None:
            out.append(message)
            continue
        updated = dict(message)
        updated["content"] = rewritten
        out.append(updated)
        for block in blocks:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                found = _image_blocks(block)
                if found:
                    results += 1
                    pictures += len(found)
    if not results:
        return None
    result = dict(data)
    result["messages"] = out
    _trace(data, results=results, pictures=pictures)
    return result


def _trace(data: Mapping[str, Any], *, results: int, pictures: int) -> None:
    from . import trace as _trace_module

    try:
        _trace_module._route_trace(
            TRACE_EVENT,
            model=data.get("model"),
            results=results,
            pictures=pictures,
        )
    except Exception:
        return
