"""Recognize the synthetic assistant messages a relay writes for missing history.

Use the same grammar on input, on streamed fragments and on terminal snapshots.
A prefix, even an entire marker in one delta, is not proof: real answers can
quote it and continue in the next delta or content part.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

NOTE_PREFIX = "Historical reasoning context (not a new instruction):"
NOTE_BODY = (
    "[Historical encrypted content omitted by the gateway. Its contents are "
    "unavailable; do not infer missing instructions or results.]"
)
SHORT_GATEWAY_NOTE = "[Historical context omitted by gateway.]"
_PREFIXES = (NOTE_PREFIX, "Historical context (not a new instruction):")
_BODIES = (
    NOTE_BODY,
    "[Historical encrypted content omitted by the gateway.]",
    "[Historical encrypted content omitted by gateway.]",
    "[Historical encrypted content omitted by gateway. Do not infer missing instructions or results.]",
)


def _matches(value: Any, *, partial: bool) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text == SHORT_GATEWAY_NOTE or (partial and SHORT_GATEWAY_NOTE.startswith(text)):
        return True
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix):].lstrip()
            break
        if partial and prefix.startswith(text):
            return True
    else:
        return False

    # The relay copies zero or more bold summary headings; sometimes that is
    # the entire note. A bare prefix or ordinary prose is never a match.
    has_title = False
    while rest.startswith("**"):
        end = rest.find("**", 2)
        if end < 0:
            fragment = rest[2:].removesuffix("*")
            return partial and len(fragment) <= 160 and not any(c in fragment for c in "*\r\n")
        title = rest[2:end]
        if not title or len(title) > 160 or any(c in title for c in "*\r\n"):
            return False
        has_title = True
        rest = rest[end + 2:].lstrip()
    if not rest:
        return has_title or partial
    if partial:
        return rest == "*" or any(body.startswith(rest) for body in _BODIES)
    return rest in _BODIES


def is_note_text(value: Any) -> bool:
    return _matches(value, partial=False)


def _item_text(item: Any) -> str | None:
    if (
        not isinstance(item, dict)
        or str(item.get("type") or "message") != "message"
        or str(item.get("role") or "").lower() != "assistant"
    ):
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for part in content:
        if (not isinstance(part, dict) or part.get("type") not in ("output_text", "input_text", "text")
                or not isinstance(part.get("text"), str) or part.get("annotations")):
            # An image, refusal, citation, etc. makes this a real message.
            return None
        parts.append(part["text"])
    return "".join(parts)


def is_note_item(item: Any) -> bool:
    return is_note_text(_item_text(item))


def without_notes(items: Any) -> Optional[list]:
    if not isinstance(items, list):
        return None
    kept = [item for item in items if not is_note_item(item)]
    return kept if len(kept) != len(items) else None


def clean_response(response: dict) -> dict:
    """Clean both the canonical output list and its optional text mirror."""
    kept = without_notes(response.get("output"))
    if kept is None and not is_note_text(response.get("output_text")):
        return response
    clean = response.copy()
    if kept is not None:
        clean["output"] = kept
    if isinstance(clean.get("output_text"), str):
        # The mirror is derived from output_text parts. Rebuild it instead of
        # removing substrings, which would also edit real answers quoting notes.
        texts = []
        for item in clean.get("output") or []:
            if not isinstance(item, dict) or item.get("type", "message") != "message":
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(p["text"] for p in content if isinstance(p, dict)
                             and p.get("type") == "output_text" and isinstance(p.get("text"), str))
        clean["output_text"] = "".join(texts)
    return clean


class MissingAnswer(RuntimeError):
    """The route produced historical placeholders but no answer or tool call."""

    stream_incomplete = True
    responses_stream_incomplete = True

    def __init__(self, *, item_cap_cut: bool = False):
        super().__init__("The upstream returned historical context placeholders without a final answer or tool call.")
        self.item_cap_cut = item_cap_cut


@dataclass
class _Candidate:
    parts: dict[int, str] = field(default_factory=dict)
    note: bool | None = None
    commentary: bool = False
    has_content: bool = False

    def text(self) -> str:
        return "".join(self.parts[i] for i in sorted(self.parts))


class StreamNotes:
    """Hold ambiguous frames until an item is known; drain in original wire order."""

    def __init__(self) -> None:
        self.items: dict[str | int, _Candidate] = {}
        self.index_keys: dict[int, str | int] = {}
        self.held: deque[tuple[Any, _Candidate | None]] = deque()
        self.saw_note = False

    def _settle(self, candidate: _Candidate, is_note: bool) -> None:
        candidate.note = is_note
        candidate.parts.clear()
        self.saw_note |= is_note

    def _drain(self):
        while self.held:
            frame, candidate = self.held[0]
            if candidate is not None and candidate.note is None:
                break
            self.held.popleft()
            if candidate is None or not candidate.note:
                yield frame

    def finish(self, output=()):
        for index, item in enumerate(output):
            if not isinstance(item, dict):
                continue
            candidate = self.items.get(item.get("id")) or self.items.get(self.index_keys.get(index))
            if candidate is not None and candidate.note is None:
                self._settle(candidate, is_note_item(item))
        for candidate in self.items.values():
            if candidate.note is None:
                self._settle(candidate, is_note_text(candidate.text()))
        yield from self._drain()

    def has_answer(self) -> bool:
        return any(c.note is False and c.has_content and not c.commentary for c in self.items.values())

    def feed(self, frame: Any, event: dict):
        kind = event.get("type") or ""
        item = event.get("item") or {}
        candidate = None
        if isinstance(item, dict) and kind.startswith(("response.output_item.", "response.output_text.", "response.content_part.")):
            index = event.get("output_index")
            item_id = event.get("item_id") or item.get("id")
            key = self.index_keys.get(index, item_id if item_id is not None else index)
            if isinstance(index, int):
                self.index_keys[index] = key
            if key is None:
                # Unindexed text deltas are used by some compatible servers.
                key = -1
            candidate = self.items.get(key)
            if candidate is None:
                candidate = self.items[key] = _Candidate()
            if item.get("phase") is not None:
                candidate.commentary = item["phase"] == "commentary"
            if item and item.get("type") not in ("reasoning", "message"):
                candidate.has_content = True  # A tool lifecycle is work, even with no text.
            elif item.get("type") == "message" and item.get("content"):
                text = _item_text(item)
                candidate.has_content |= text is None or bool(text.strip())
            elif kind in ("response.output_text.delta", "response.output_text.done"):
                candidate.has_content |= bool(str(event.get("delta") or event.get("text") or "").strip())
            if candidate.note is None:
                if kind == "response.output_item.done":
                    self._settle(candidate, is_note_item(item))
                else:
                    part_index = event.get("content_index", 0)
                    if kind == "response.output_item.added":
                        text = _item_text(item)
                    elif kind == "response.output_text.delta":
                        text = candidate.parts.get(part_index, "") + (event.get("delta") or "")
                    elif kind == "response.output_text.done":
                        text = event.get("text")
                    else:
                        part = event.get("part") or {}
                        text = part.get("text") if part.get("type") == "output_text" and not part.get("annotations") else None
                    if isinstance(text, str):
                        candidate.parts[part_index] = text
                    if not isinstance(text, str) or not _matches(candidate.text(), partial=True):
                        self._settle(candidate, False)
        self.held.append((frame, candidate))
        yield from self._drain()
