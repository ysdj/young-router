"""DeepSeek's native tool-call markup, on the Anthropic Messages wire.

DeepSeek-V4 answers a tool request with its own markup instead of structured
``tool_calls``::

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="Bash">
    <｜｜DSML｜｜ parameter name="command" string="true">sed -n '1,80p' x</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

On the Responses wire this proxy already reads that markup and turns it into
``function_call`` items (``web_search_native._raw_dsml_tool_call_items`` and
``_RawToolCallTextFilter``).  The Anthropic Messages wire had no such path: the
markup arrived as ``content_block_delta`` text, so the client rendered a tool
call it could not execute and the turn stopped on prose that looked like XML.

Measured on the live gateway 2026-11-16, replaying the PIK3CA thread's own
body through ``POST /v1/messages``: the answer was

    \n\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="Bash">\n<｜｜DSML｜｜ parameter

-- a 200 with the call as text.  A client that reads text and not markup sees
an assistant that described a command instead of running one.

So this module gives the Messages wire the same two jobs the Responses wire
already does:

* **hold the markup back** from the reader while it streams, because a block
  arrives split across deltas and a half-written tag is not something a client
  can render or ignore;
* **hand it over as a real ``tool_use`` block** when the block closes, with the
  parameters parsed into the call's ``input``.

A fenced code example is content, not a call: the same rule the Responses
filter applies, for the same reason -- a thread discussing this format must not
have its example executed.
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any, Dict, List, Optional

#: The delimiters.  ``\uff5c`` is the fullwidth vertical bar DeepSeek uses.
_DSML = "\uff5c\uff5cDSML\uff5c\uff5c"
#: The trace a rewritten answer reports, so "no markup" and "nothing ran" are
#: distinguishable from the host's log.
TRACE_EVENT = "anthropic_dsml_rewritten"

CALLS_START = f"<{_DSML} calls>"
CALLS_END = f"</{_DSML} calls>"
INVOKE_START = f"<{_DSML} invoke"
_INVOKE_END = f"</{_DSML} invoke>"
_PARAMETER_END = f"</{_DSML} parameter>"

#: ``\s*`` and not ``\s+``: the live route writes ``<｜｜DSML｜｜ calls>`` with
#: a space, but the same block is also written without one, and a pattern that
#: insists on the space simply does not match the block it was written for.
_INVOKE_RE = re.compile(
    rf'(?is)<{_DSML}\s*invoke\s+name="([^"]+)"\s*>(.*?)</{_DSML}\s*invoke\s*>'
)
_PARAMETER_RE = re.compile(
    rf'(?is)<{_DSML}\s*parameter\s+name="([^"]+)"(\s+[^>]*)?>(.*?)</{_DSML}\s*parameter\s*>'
)
_CALLS_BLOCK_RE = re.compile(
    rf"(?is)<{_DSML}\s*calls>.*?</{_DSML}\s*calls>"
)
#: A block that never closed.  It still starts where the tag starts, wherever
#: in the text that is -- ``^`` with ``re.M`` would miss a block mid-paragraph.
_CALLS_TAIL_RE = re.compile(rf"(?is)<{_DSML}\s*calls>.*\Z")

#: A fenced span or an inline code span.  Markup inside one is prose.
_CODE_SPAN_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")

#: An opening fence: at least three backticks at the start of a line, which is
#: the only place Markdown opens one.  Anchoring matters -- without it the
#: closing run of a fence later in the same buffer reads as a new opening one.
_OPENING_FENCE_RE = re.compile(r"(?m)^(`{3,})")


def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Every DSML call in ``text`` as a Messages ``tool_use`` block.

    ``input`` is the parsed parameter map: ``string="false"`` marks a parameter
    whose body is a JSON value rather than the model's prose, and a body that
    is not JSON after all stays the string it is -- raising here would end a
    stream the route is still writing.
    """

    if not isinstance(text, str) or not text:
        return []
    calls: List[Dict[str, Any]] = []
    for segment in _CODE_SPAN_RE.split(text)[::2]:
        blocks = [match.group(0) for match in _CALLS_BLOCK_RE.finditer(segment)]
        if not blocks:
            # An unterminated block is still a call: the route stopped writing
            # it, so what arrived is all there is.  Parsing it is what keeps a
            # cut stream from ending in a half tag the client renders.
            tail = _CALLS_TAIL_RE.search(segment)
            if tail is not None:
                blocks = [tail.group(0)]
        for block in blocks:
            for invoke in _INVOKE_RE.finditer(block):
                name = html.unescape(invoke.group(1)).strip()
                if not name:
                    continue
                arguments: Dict[str, Any] = {}
                for parameter in _PARAMETER_RE.finditer(invoke.group(2)):
                    key = html.unescape(parameter.group(1)).strip()
                    value = html.unescape(parameter.group(3))
                    attributes = parameter.group(2) or ""
                    if re.search(r'\bstring="false"', attributes, re.IGNORECASE):
                        try:
                            value = json.loads(value)
                        except ValueError:
                            pass
                    arguments[key] = value
                calls.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_dsml_{time.time_ns()}_{len(calls)}",
                        "name": name,
                        "input": arguments,
                    }
                )
    return calls


#: Every DSML tag, opening or closing.  A lone closer is the shape the live
#: route leaked (2026-11-16: an assistant message whose whole text was
#: ``<thinking> </thinking>\n\n</｜｜DSML｜｜ calls>``, with no block around it),
#: and a dangling tag is no more renderable than a whole block.
_ANY_TAG_RE = re.compile(rf"</?{_DSML}\s*(?:calls|invoke|parameter)\b[^>]*>")


def strip_tool_calls(text: str) -> str:
    """``text`` with every DSML tag removed, fenced examples left alone."""

    if not isinstance(text, str) or not text:
        return text
    parts = _CODE_SPAN_RE.split(text)
    for index in range(0, len(parts), 2):
        segment = parts[index]
        cleaned = _CALLS_BLOCK_RE.sub("", segment)
        # A block that never closed is still a block: everything from its
        # opening tag to the end of the text is the call.
        cleaned = _CALLS_TAIL_RE.sub("", cleaned)
        # Whatever tag survived is dangling -- a lone closer, or an opener
        # whose block the route never finished writing.  Neither is text.
        cleaned = _ANY_TAG_RE.sub("", cleaned)
        if cleaned != segment and cleaned.strip() in {".", "。"}:
            cleaned = ""
        parts[index] = cleaned
    cleaned = "".join(parts)
    if cleaned == text:
        return text
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _trailing_run_length(text: str, character: str, limit: int) -> int:
    """How many of ``character`` sit at the end of ``text``, up to ``limit``."""

    count = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index] != character:
            break
        count += 1
        if count >= limit:
            break
    return count


def _pending_fence_length(text: str) -> int:
    """How much of a possible opening fence sits at the end of ``text``.

    A fence opens at a line start with three or more backticks.  Only a run
    that begins a line is ambiguous -- backticks after other text are inline
    spans, and holding those would delay ordinary prose.
    """

    count = _trailing_run_length(text, "`", 3)
    if count == 0:
        return 0
    before = text[: len(text) - count]
    if before and not before.endswith("\n"):
        return 0
    return count


class _PendingPrefix:
    """How much of a marker the buffer's tail might still be growing into."""

    @staticmethod
    def length(text: str, marker: str) -> int:
        lowered = text.lower()
        target = marker.lower()
        for length in range(min(len(lowered), len(target) - 1), 0, -1):
            if target.startswith(lowered[-length:]):
                return length
        return 0


class MessageStreamFilter:
    """Hold DSML markup back from a Messages stream and hand it over as calls.

    The stream is split into text the client should see and calls it should
    execute.  A block can arrive in any number of deltas, so nothing that might
    be the start of one is emitted until it is resolved: the tail of the buffer
    that could still grow into ``<｜｜DSML｜｜ calls>`` is kept, and everything
    before it goes out immediately.

    ``feed`` returns the visible text to deliver *now*; ``drain`` is what to
    deliver once the stream has ended; ``take_calls`` is what to turn into
    ``tool_use`` blocks.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self._hidden: List[str] = []
        self._calls: List[Dict[str, Any]] = []
        #: Backtick runs currently open, as a stack: ``` and ` nest in the
        #: Markdown sense, and markup inside either is an example the thread is
        #: discussing rather than a call it made.  Without this a replay of a
        #: conversation *about* DSML would execute the example it quotes.
        self._fences: List[str] = []
        #: Whether a stray closing tag is being held.  The live leak was
        #: exactly this: ``</｜｜DSML｜｜ calls>`` with no block around it, so a
        #: filter keyed only on the opener passed it straight through.
        self._in_orphan = False
        #: Whether a block opener was ever seen, and how many calls were
        #: parsed.  Both are what tells a route that wrote no markup apart
        #: from a filter that never looked at the answer.
        self.saw_marker = False

    def feed(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""
        self._buffer += text
        visible: List[str] = []
        while self._buffer:
            if self._inside:
                end = self._buffer.lower().find(CALLS_END.lower())
                if end < 0:
                    keep = _PendingPrefix.length(self._buffer, CALLS_END)
                    captured = self._buffer[:-keep] if keep else self._buffer
                    if captured:
                        self._hidden.append(captured)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    break
                block = "".join(self._hidden) + self._buffer[: end + len(CALLS_END)]
                self._calls.extend(parse_tool_calls(block))
                self._hidden = []
                self._buffer = self._buffer[end + len(CALLS_END) :]
                self._inside = False
                continue
            if self._fences:
                # An open example is checked first, and it wins over a marker
                # in the same buffer: the text inside a fence is what the
                # thread is quoting, not what it called.
                # Inside an example: deliver it verbatim, fence included, and
                # only watch for the run that closes the fence.
                closing = self._fences[-1]
                index = self._buffer.find(closing, len(closing))
                if index < 0:
                    # A closing run can arrive split across deltas, so a tail
                    # of backticks that could still grow into one is held.  The
                    # lookback has to be the run's own length, not a whole-run
                    # prefix match: the tail sits behind text this filter has
                    # already decided to deliver.
                    keep = _trailing_run_length(self._buffer, "`", len(closing))
                    if keep:
                        visible.append(self._buffer[:-keep])
                        self._buffer = self._buffer[-keep:]
                        break
                    visible.append(self._buffer)
                    self._buffer = ""
                    break
                visible.append(self._buffer[: index + len(closing)])
                self._buffer = self._buffer[index + len(closing) :]
                self._fences.pop()
                continue
            # A fence that opens before any marker in this buffer decides the
            # whole buffer: what follows belongs to the example.  Checking the
            # marker first read ``<`` inside a fenced block as a call.
            fence = _OPENING_FENCE_RE.search(self._buffer)
            start = self._buffer.lower().find(CALLS_START.lower())
            if fence is not None and (start < 0 or fence.start() < start):
                visible.append(self._buffer[: fence.end()])
                self._buffer = self._buffer[fence.end() :]
                self._fences.append(fence.group(1))
                continue
            if start >= 0:
                self.saw_marker = True
                visible.append(self._buffer[:start])
                self._buffer = self._buffer[start:]
                self._inside = True
                self._hidden = []
                continue
            orphan = self._orphan_tag()
            if orphan is not None:
                kind, index, end = orphan
                visible.append(self._buffer[:index])
                self._buffer = self._buffer[end:]
                continue
            keep = max(
                _PendingPrefix.length(self._buffer, CALLS_START),
                # A ``<`` may be the first character of any tag this filter
                # holds, so it is never delivered before the frame that
                # decides what it is.
                _PendingPrefix.length(self._buffer, "<"),
                # A closing tag arriving one character at a time must be held
                # from its first ``<``, or the client renders the half tag.
                _PendingPrefix.length(self._buffer, CALLS_END),
                _PendingPrefix.length(self._buffer, _INVOKE_END),
                _PendingPrefix.length(self._buffer, _PARAMETER_END),
                # A fence opened at the start of a line can also arrive one
                # backtick at a time, so a run of them at a line start is held
                # until the frame says whether it is a fence or prose.
                _pending_fence_length(self._buffer),
            )
            if keep:
                visible.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
                break
            visible.append(self._buffer)
            self._buffer = ""
        return "".join(visible)

    def _orphan_tag(self) -> Optional[tuple[str, int, int]]:
        """The first stray closing tag in the buffer: ``(kind, start, end)``.

        Only closers with no opener are considered here.  A closed block is
        consumed by the ``_inside`` branch above and never reaches this; what
        does reach it is the dangling tag a route writes after it has already
        finished its block -- the shape measured on the live gateway.
        """

        lowered = self._buffer.lower()
        best: Optional[tuple[str, int, int]] = None
        for kind, marker in (
            ("calls", CALLS_END),
            ("invoke", _INVOKE_END),
            ("parameter", _PARAMETER_END),
        ):
            index = lowered.find(marker.lower())
            if index < 0:
                continue
            if best is None or index < best[1]:
                best = (kind, index, index + len(marker))
        return best

    def drain(self) -> str:
        """The visible text left over once the stream has ended.

        An unterminated block is still a call: the route stopped writing it, so
        whatever arrived is all there will be.  Parsing it is what keeps a cut
        stream from ending in a half tag the client renders.
        """

        if self._inside:
            block = "".join(self._hidden) + self._buffer
            self._calls.extend(parse_tool_calls(block))
            self._hidden = []
            self._buffer = ""
            self._inside = False
            return ""
        text = self._buffer
        self._buffer = ""
        return text

    def calls_parsed(self) -> List[Dict[str, Any]]:
        """The calls parsed so far, without consuming them."""

        return list(self._calls)

    def take_calls(self) -> List[Dict[str, Any]]:
        calls = self._calls
        self._calls = []
        return calls


def has_tool_calls(text: str) -> bool:
    """Whether ``text`` carries a DSML call outside a fenced example."""

    if not isinstance(text, str) or CALLS_START not in text:
        return False
    return bool(_CALLS_BLOCK_RE.search(text) or re.search(
        rf"(?is)<{_DSML}\s*calls>", text
    ))
