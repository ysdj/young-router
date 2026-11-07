"""``view_image_by_id``: the fetch half of image context, over MCP stdio.

The proxy removes an image from a turn and leaves a placeholder naming its id.
This server is what makes that reversible: the model calls it with the ids a
placeholder named, and gets the originals back for that turn only.

It speaks MCP JSON-RPC over stdin/stdout, needs nothing but the standard library,
and never reaches the network: the bytes come from the replay the proxy stored
for the same task, and an id it cannot answer is reported as such instead of
being guessed at.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import image_context as _image_context_module
from . import image_context_store as _store_module

SERVER_NAME = "young-router-image-context"
SERVER_VERSION = "1"
TOOL_NAME = "view_image_by_id"

# The task this server answers for.  The proxy writes the same value into the
# replay cache, so both halves agree on which store to read.
THREAD_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_THREAD"

_INSTRUCTIONS_EN = (
    "Fetch the original of an omitted image by its id. The id is the one in the "
    "placeholder (\"Image IMG-004\"). A fetched image is provided for this call "
    "only; ask again when you need it again."
)


def _tool_spec() -> Dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "title": "View an omitted image by id",
        "description": (
            "Return the original bytes of images the user's image-context manager "
            "left out of the current turn, addressed by the id in their "
            "placeholder (for example IMG-004). Use it when the placeholder's "
            "description is not enough to answer. An image fetched here is "
            "provided for this call only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Image ids from the placeholders, e.g. [\"IMG-004\"].",
                }
            },
            "required": ["ids"],
        },
        "annotations": {"readOnlyHint": True},
    }


def _fetched_line(record: Dict[str, Any], image_id: str, lang: str) -> str:
    """The heading this copy carries, built by the pass's own builder."""

    return _image_context_module.fetched_label(
        str(record.get("id") or image_id),
        lang=lang,
        name=record.get("name"),
        kind=record.get("kind") or "tool",
        turn=record.get("turn"),
        width=record.get("width"),
        height=record.get("height"),
    )


def _thread_id(params: Dict[str, Any]) -> Optional[str]:
    configured = os.environ.get(THREAD_ENV, "").strip()
    if configured:
        return configured
    arguments = params.get("arguments")
    if isinstance(arguments, dict):
        value = arguments.get("threadId")
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = params.get("_meta")
    if isinstance(meta, dict):
        for key in ("threadId", "thread_id"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _wanted_ids(params: Dict[str, Any]) -> List[str]:
    arguments = params.get("arguments")
    values = arguments.get("ids") if isinstance(arguments, dict) else None
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    seen: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def fetch(params: Dict[str, Any]) -> Dict[str, Any]:
    """Answer one ``tools/call`` with MCP content blocks."""

    thread_id = _thread_id(params)
    wanted = _wanted_ids(params)
    if not wanted:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "No image ids were given. Pass the ids from the "
                        "placeholders, for example {\"ids\": [\"IMG-004\"]}."
                    ),
                }
            ],
            "isError": True,
        }
    blocks: List[Dict[str, Any]] = []
    missing: List[str] = []
    for image_id in wanted:
        answer = _store_module.fetched(thread_id, image_id)
        if answer is None:
            missing.append(image_id)
            continue
        record, lang, mime, data_url = answer
        # The line this copy carries is the pass's own heading for a fetched
        # image, in the task's language.  The pass reads it back on the next
        # request and keeps the id it names, so the picture the model just
        # asked for stays the id it asked for instead of taking a new number.
        blocks.append({"type": "text", "text": _fetched_line(record, image_id, lang)})
        blocks.append({"type": "image", "data": data_url.split(",", 1)[1], "mimeType": mime})
    if missing:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "Not available in this task any more: "
                    + ", ".join(missing)
                    + ". Those images are no longer part of the conversation; say "
                    "that plainly rather than describing them from memory."
                ),
            }
        )
    return {"content": blocks, "isError": False}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: Any, value: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Answer one JSON-RPC message; ``None`` for a notification."""

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if not isinstance(method, str):
        return None if request_id is None else _error(request_id, -32600, "invalid request")
    if method in {"initialize", "initialized", "notifications/initialized"}:
        if method != "initialize":
            return None
        return _result(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": _INSTRUCTIONS_EN,
            },
        )
    if method in {"tools/list", "tools/list_changed"}:
        return _result(request_id, {"tools": [_tool_spec()]})
    if method == "tools/call":
        name = params.get("name")
        if name != TOOL_NAME:
            return _result(request_id, {"content": [{"type": "text", "text": f"Unknown tool {name}."}], "isError": True})
        return _result(request_id, fetch(params))
    if method in {"ping"}:
        return _result(request_id, {})
    if method.startswith("notifications/"):
        return None
    return _error(request_id, -32601, f"unknown method {method}")


def main(argv: Optional[List[str]] = None) -> int:
    """Serve MCP over stdio until stdin closes."""

    del argv
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError:
            sys.stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(message, dict):
            sys.stdout.write(json.dumps(_error(None, -32600, "invalid request")) + "\n")
            sys.stdout.flush()
            continue
        answer = handle(message)
        if answer is None:
            continue
        sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
