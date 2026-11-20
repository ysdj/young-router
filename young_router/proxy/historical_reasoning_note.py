"""The note a Codex backend writes when it has no reasoning to replay.

A long Codex thread on a relay served by a Codex backend can come back with
assistant messages that are not answers at all::

    Historical reasoning context (not a new instruction):**Checking the proxy**
    [Historical encrypted content omitted by the gateway. Its contents are
     unavailable; do not infer missing instructions or results.]

The sentence names the gateway because the upstream observed that its
encrypted reasoning did not arrive.  Whatever omitted it, the note belongs to
the *context*, not to the answer: it states that nothing follows and tells the
reader not to infer anything from it.  Sent as an assistant message it is
recorded, replayed, and answered with another one -- live 2026-11-20 one thread
carried 95 of them, several per response, each rendered as a progress message
in the client.

Two things happen here, both narrow:

* a replayed note is removed from the request's ``input`` so the model stops
  reading its own noise and extending the pattern;
* a streamed note is dropped before delivery, so the client never records it;
  a response that is only the note still arrives, because a completion with no
  output is a failed turn the client reconnects over.

Only the exact shape is matched -- the fixed prefix, an optional bold progress
title, and the fixed bracketed body.  A message that merely *mentions* the
note (a bug report quoting it, a summary of this rule) does not match, and a
message with any real content after the body does not either.
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: The sentence the note is built from.  Matched whole, not as a substring.
NOTE_PREFIX = "Historical reasoning context (not a new instruction):"
NOTE_BODY = (
    "[Historical encrypted content omitted by the gateway. Its contents are "
    "unavailable; do not infer missing instructions or results.]"
)

_NOTE_PATTERN = re.compile(
    r"^Historical reasoning context \(not a new instruction\):"
    r"(?:\*\*[^*\n]{0,160}\*\*)?"
    r"\[Historical encrypted content omitted by the gateway\. "
    r"Its contents are unavailable; do not infer missing instructions or "
    r"results\.\]$"
)


def is_note_text(value: Any) -> bool:
    """Whether this text is exactly one of the synthetic context notes."""

    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or NOTE_PREFIX not in text:
        return False
    return bool(_NOTE_PATTERN.match(text))


def is_possible_note_prefix(value: Any) -> bool:
    """Whether text can still become a complete synthetic note."""

    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if len(text) <= len(NOTE_PREFIX):
        return NOTE_PREFIX.startswith(text)
    if not text.startswith(NOTE_PREFIX):
        return False
    rest = text[len(NOTE_PREFIX):]
    if NOTE_BODY.startswith(rest):
        return True
    if not rest.startswith("**"):
        return False
    closing = rest.find("**", 2)
    if closing < 0:
        title = rest[2:]
        return len(title) <= 160 and "\n" not in title and "*" not in title
    title = rest[2:closing]
    if len(title) > 160 or "\n" in title or "*" in title:
        return False
    return NOTE_BODY.startswith(rest[closing + 2:])


def _item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or part.get("input_text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def is_note_item(item: Any) -> bool:
    """Whether this Responses item is an assistant message carrying the note."""

    if not isinstance(item, dict):
        return False
    if str(item.get("type") or "message") != "message":
        return False
    if str(item.get("role") or "").lower() != "assistant":
        return False
    return is_note_text(_item_text(item))


def without_notes(items: Any) -> Optional[list]:
    """The same replay with the synthetic notes removed, or ``None``.

    ``None`` means there was nothing to remove, so the caller can tell the
    step lists an unchanged request without comparing the list itself.
    """

    if not isinstance(items, list):
        return None
    kept = [item for item in items if not is_note_item(item)]
    if len(kept) == len(items):
        return None
    return kept


__all__ = [
    "NOTE_BODY",
    "NOTE_PREFIX",
    "is_note_item",
    "is_note_text",
    "without_notes",
]
