"""Rewrite a Messages SSE body on its way to the client.

Why this layer exists, measured on the deployed gateway 2026-11-16: the
post-call streaming hook sees a *partial* copy of a ``/v1/messages`` answer.
For one probe the hook yielded 45 frames while the client received 208, and
the markup DeepSeek writes instead of ``tool_calls`` reached the client as
``content_block_delta`` text with ``stop_reason: end_turn``.  A filter upstream
of that fork can hide frames only from itself.

``base_process_llm_request`` hands back the ``StreamingResponse`` whose
``body_iterator`` is the last thing between the route and the client, so the
rewrite happens there: the bytes are parsed back into SSE events, the text of
each ``text_delta`` is passed through ``anthropic_dsml``, and the calls it
parsed are appended as real ``tool_use`` blocks before the answer's terminal
frames.

Boundaries this keeps:

* a frame boundary may fall anywhere, including inside one event, so bytes are
  held until the delimiter that ends an event arrives;
* only ``content_block_delta``/``text_delta`` frames are rewritten -- pings,
  thinking, signatures and tool input are forwarded byte for byte;
* the client's ``stop_reason`` still describes what it is being given: an
  answer whose only output was a call reports ``tool_use``.
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

from . import anthropic_dsml as _anthropic_dsml_module

#: SSE events end with a blank line, in any of the three line endings a proxy
#: may pass through.
_EVENT_SEPARATOR = re.compile(r"\r\n\r\n|\n\n|\r\r")
_DATA_LINE = re.compile(r"^data:[ \t]?(.*)$", re.MULTILINE)
#: The indices the synthesized blocks use, so they cannot collide with a block
#: the route wrote itself.
_TOOL_BLOCK_INDEX_BASE = 900


def _event_json(block: str) -> Optional[Dict[str, Any]]:
    """The JSON payload of one SSE event, when it has exactly one data line."""

    payloads = _DATA_LINE.findall(block)
    if len(payloads) != 1:
        return None
    try:
        parsed = json.loads(payloads[0].strip())
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _sse_block(payload: Dict[str, Any]) -> bytes:
    return (
        "event: "
        + str(payload.get("type") or "message")
        + "\ndata: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


class MessagesSseRewriter:
    """Turn DeepSeek's call markup into ``tool_use`` blocks on the wire.

    ``lead_blocks`` are published before the upstream's own blocks: they are
    the search exchange a request already ran (``messages_web_search``), which
    the client reads back by block type.  Every upstream ``index`` is shifted
    past them, because one content block index names one block on the wire --
    leaving the first upstream block at 0 would overwrite the search call with
    the answer text.
    """

    def __init__(self, lead_blocks: Optional[List[Dict[str, Any]]] = None) -> None:
        self._filter = _anthropic_dsml_module.MessageStreamFilter()
        #: Blocks to publish right after ``message_start``, in order.
        self._lead_blocks: List[Dict[str, Any]] = [
            dict(block) for block in (lead_blocks or []) if isinstance(block, dict)
        ]
        #: Searches these blocks report.  The provider states them in
        #: ``usage.server_tool_use`` on the final ``message_delta``, and a
        #: client that reads that counter (Claude Code does) otherwise shows a
        #: search that answered as a turn that searched zero times.
        self._search_requests = sum(
            1
            for block in self._lead_blocks
            if block.get("type") == "web_search_tool_result"
        )
        #: How far every upstream block index moves to make room for them.
        self._index_offset = 0
        #: Content blocks the answer has open, in the order they opened.
        self._open_blocks: List[int] = []
        self._pending = ""
        self._flushed = False

    # -- events ---------------------------------------------------------------

    def feed(self, text: str) -> List[bytes]:
        """Consume one decoded piece of the body; return the bytes to send."""

        if not isinstance(text, str) or not text:
            return []
        self._pending += text
        out: List[bytes] = []
        while True:
            match = _EVENT_SEPARATOR.search(self._pending)
            if match is None:
                break
            block = self._pending[: match.start()]
            # The separator belongs to the event that preceded it: dropping it
            # concatenated every forwarded frame onto the next one's header.
            raw = self._pending[: match.end()]
            self._pending = self._pending[match.end() :]
            out.extend(self._handle_block(block, raw))
        return out

    def finish(self) -> List[bytes]:
        """Flush what the answer left behind, once the body has ended."""

        out: List[bytes] = []
        if self._pending.strip():
            out.extend(self._handle_block(self._pending))
            self._pending = ""
        if not self._flushed:
            self._flushed = True
            out.extend(self._closing_events())
        return out

    # -- one event ------------------------------------------------------------

    def _handle_block(self, block: str, raw: Optional[str] = None) -> List[bytes]:
        original = [(raw if raw is not None else block).encode("utf-8")]
        payload = _event_json(block)
        if payload is None:
            # Anything this module cannot read is forwarded as it arrived:
            # a keep-alive comment, a comment line, a body in another shape.
            return original
        event_type = payload.get("type")
        if event_type == "message_start":
            return original + self._lead_events(payload)
        shifted = self._with_index_offset(payload)
        if shifted is not None:
            payload = shifted
            original = [_sse_block(shifted)]
        if event_type == "content_block_start":
            index = payload.get("index")
            if isinstance(index, int) and index not in self._open_blocks:
                self._open_blocks.append(index)
            return original
        if event_type == "content_block_stop":
            index = payload.get("index")
            if index in self._open_blocks:
                self._open_blocks.remove(index)
            return original
        if event_type == "content_block_delta":
            return self._handle_delta(payload, original)
        if event_type == "message_delta":
            # The blocks the client is about to be told the answer ended on
            # have to exist first, and the reason has to name them.
            # ``_closing_events`` consumes the calls, so the count has to be
            # taken before it runs or the reason is left saying the answer
            # ended on its own.
            calls = self._filter.calls_parsed()
            out = self._closing_events()
            final = self._with_tool_use_stop_reason(payload) if calls else payload
            final = self._with_search_usage(final)
            if final is payload:
                return out + original
            return out + [_sse_block(final)]
        return original

    def _handle_delta(
        self, payload: Dict[str, Any], original: List[bytes]
    ) -> List[bytes]:
        delta = payload.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return original
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return original
        visible = self._filter.feed(text)
        if visible == text:
            return original
        if not visible:
            # The frame was entirely markup; the client has nothing to render.
            return []
        rewritten = dict(payload)
        rewritten_delta = dict(delta)
        rewritten_delta["text"] = visible
        rewritten["delta"] = rewritten_delta
        return [_sse_block(rewritten)]

    # -- synthesized frames ---------------------------------------------------

    def _lead_events(self, payload: Dict[str, Any]) -> List[bytes]:
        """Publish the search exchange before the answer's own blocks.

        A ``message_start`` that already carries content opened its blocks at
        index 0, so there is no room in front of them; the lead is dropped
        rather than written into an index the client already holds.  That shape
        is not what a streaming provider sends, and dropping it leaves the
        answer exactly as the route wrote it.
        """

        if not self._lead_blocks or self._index_offset:
            return []
        message = payload.get("message")
        initial = message.get("content") if isinstance(message, dict) else None
        if isinstance(initial, list) and initial:
            self._lead_blocks = []
            return []
        out: List[bytes] = []
        for index, block in enumerate(self._lead_blocks):
            out.extend(_lead_block_events(index, block))
        self._index_offset = len(self._lead_blocks)
        return out

    def _with_index_offset(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Move one upstream block's index past the lead blocks."""

        if not self._index_offset:
            return None
        index = payload.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        shifted = dict(payload)
        shifted["index"] = index + self._index_offset
        return shifted

    def _with_search_usage(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Report the searches these blocks stand for on the turn's usage."""

        if not self._search_requests:
            return payload
        usage = payload.get("usage")
        if not isinstance(usage, dict) or "server_tool_use" in usage:
            return payload
        updated = dict(payload)
        updated["usage"] = {
            **usage,
            "server_tool_use": {"web_search_requests": self._search_requests},
        }
        return updated

    def _with_tool_use_stop_reason(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return payload
        if delta.get("stop_reason") not in (None, "", "end_turn"):
            return payload
        rewritten = dict(payload)
        rewritten_delta = dict(delta)
        rewritten_delta["stop_reason"] = "tool_use"
        rewritten["delta"] = rewritten_delta
        return rewritten

    def _closing_events(self) -> List[bytes]:
        """Close what the answer left open and emit the calls it made."""

        out: List[bytes] = []
        calls = self._filter.take_calls()
        if not calls:
            return out
        for open_index in list(self._open_blocks):
            out.append(
                _sse_block({"type": "content_block_stop", "index": open_index})
            )
        self._open_blocks.clear()
        for offset, call in enumerate(calls):
            index = _TOOL_BLOCK_INDEX_BASE + offset
            out.append(
                _sse_block(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": {},
                        },
                    }
                )
            )
            out.append(
                _sse_block(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(
                                call["input"], ensure_ascii=False
                            ),
                        },
                    }
                )
            )
            out.append(_sse_block({"type": "content_block_stop", "index": index}))
        return out


def _decoded(chunk: Any) -> str:
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk).decode("utf-8", "replace")
    return str(chunk)


def _lead_block_events(index: int, block: Dict[str, Any]) -> List[bytes]:
    """One whole content block, as the frames that publish it.

    A ``server_tool_use`` block states its input through ``input_json_delta``
    like any other tool call: the start frame carries an empty ``input`` and the
    call's fields follow, which is the shape Claude Code accumulates before it
    groups results by the id it saw there.
    """

    block_type = str(block.get("type") or "")
    if block_type == "server_tool_use":
        return [
            _sse_block(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": block_type,
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": {},
                    },
                }
            ),
            _sse_block(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            ),
            _sse_block({"type": "content_block_stop", "index": index}),
        ]
    return [
        _sse_block(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": block,
            }
        ),
        _sse_block({"type": "content_block_stop", "index": index}),
    ]


async def rewrite_messages_sse(
    body: AsyncIterator[Any],
    lead_blocks: Optional[List[Dict[str, Any]]] = None,
) -> AsyncIterator[bytes]:
    """Wrap a Messages body so its text never carries the route's markup."""

    rewriter = MessagesSseRewriter(lead_blocks=lead_blocks)
    async for chunk in body:
        for frame in rewriter.feed(_decoded(chunk)):
            yield frame
    for frame in rewriter.finish():
        yield frame
