"""The replay a fetch tool answers from, kept per task between turns.

``view_image_by_id`` has to hand a model the bytes of an image the proxy itself
removed from the turn it is answering.  Those bytes are in the request the proxy
already has, so the only thing that must survive between turns is the *last
replay*: a task's newest request is a superset of every earlier one -- the client
resends its whole transcript -- so a copy of it is enough to answer an id for the
rest of the session, including after the client compacts the image away.

Storage is deliberately small and self-limiting:

* one file per task, written atomically, replaced on every request that carries
  images;
* only the images are written, never the text, so the file is a picture store
  and not a transcript;
* a file older than a session is dropped by its own age, and the directory is
  swept on every write so nothing accumulates.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, List, Optional

REPLAY_CACHE_DIR_ENV = "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_CACHE_DIR"
# This app's own state, never the plugin's: a gateway deployed on a server has
# no attachment manager installed and no business writing into one's folders.
RUNTIME_ROOT_ENV = "LITELLM_RUNTIME_ROOT"
RUNTIME_ROOT_FALLBACK_ENV = "YOUNG_ROUTER_HOME"
# A task the user has not touched for a day cannot produce a turn that asks for
# one of its images, and Codex's own session list keeps nothing alive that long.
REPLAY_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
REPLAY_CACHE_MAX_TASKS = 32


def cache_dir(environ: Optional[Dict[str, str]] = None) -> str:
    """Where this app keeps the replays the fetch tool answers from.

    Its own directory, under this installation's runtime root: the attachment
    manager's data folder is a desktop plugin's, and a gateway on a server has
    neither the plugin nor a reason to write there.
    """

    env = os.environ if environ is None else environ
    configured = str(env.get(REPLAY_CACHE_DIR_ENV) or "").strip()
    if configured:
        return os.path.expanduser(configured)
    root = (
        str(env.get(RUNTIME_ROOT_ENV) or "").strip()
        or str(env.get(RUNTIME_ROOT_FALLBACK_ENV) or "").strip()
    )
    base = os.path.expanduser(root) if root else os.path.join(
        os.path.expanduser(str(env.get("HOME") or env.get("USERPROFILE") or "~")),
        ".young-router",
    )
    return os.path.join(base, "image-context", "replays")


def _path_for(thread_id: str, environ: Optional[Dict[str, str]] = None) -> str:
    return os.path.join(cache_dir(environ), f"{thread_id}.json")


def _safe_thread_id(thread_id: Any) -> Optional[str]:
    if not isinstance(thread_id, str):
        return None
    cleaned = thread_id.strip()
    if not cleaned or len(cleaned) > 120:
        return None
    if any(character in cleaned for character in ("/", "\\", "\0", "..")):
        return None
    return cleaned


def store(thread_id: Any, request_kwargs: Dict[str, Any]) -> int:
    """Write the images of one request and return how many were stored."""

    from . import image_context as _image_context_module

    safe = _safe_thread_id(thread_id)
    if not safe:
        return 0
    images: List[Dict[str, Any]] = []
    for image in _image_context_module.scan(request_kwargs.get("input")):
        if not isinstance(image.mime, str) or not image.raw:
            continue
        images.append(
            {
                "id": image.image_id,
                "mime": image.mime,
                "turn": image.turn,
                "width": image.width,
                "height": image.height,
                "name": image.name,
                "kind": image.kind,
                "base64": base64.b64encode(image.raw).decode("ascii"),
            }
        )
    if not images:
        return 0
    root = cache_dir()
    os.makedirs(root, exist_ok=True)
    target = _path_for(safe)
    temporary = f"{target}.tmp.{os.getpid()}"
    payload = {"threadId": safe, "at": time.time(), "images": images}
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, target)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return 0
    _sweep(root)
    return len(images)


def _sweep(root: str) -> None:
    now = time.time()
    entries: List[tuple] = []
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if now - modified > REPLAY_CACHE_MAX_AGE_SECONDS:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        entries.append((modified, path))
    for _, path in sorted(entries, reverse=True)[REPLAY_CACHE_MAX_TASKS:]:
        try:
            os.unlink(path)
        except OSError:
            pass


def load(thread_id: Any, environ: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Every image the last stored request of one task carried."""

    safe = _safe_thread_id(thread_id)
    if not safe:
        return []
    try:
        with open(_path_for(safe, environ), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    images = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(images, list):
        return []
    at = payload.get("at")
    if isinstance(at, (int, float)) and time.time() - at > REPLAY_CACHE_MAX_AGE_SECONDS:
        return []
    return [image for image in images if isinstance(image, dict)]


def image_data_url(
    thread_id: Any,
    image_id: str,
    environ: Optional[Dict[str, str]] = None,
) -> Optional[tuple]:
    """``(mime, data_url)`` for one stored id, or ``None`` when it is gone.

    The task is one lookup key, not a requirement: a client starts this tool
    without telling it which task the model is in (Codex passes a task id to its
    own tools, not to an MCP server's), so an id that the named task cannot
    answer is looked for in the most recent replays.  ``IMG-0004`` names one
    image of one task, and a hit in the newest replay that carries it is the
    task the model is asking about.
    """

    wanted = str(image_id or "").strip().upper()
    if not wanted:
        return None
    answer = _lookup(load(thread_id, environ), wanted)
    if answer is not None:
        return answer
    for candidate in _recent_threads(environ):
        if candidate == _safe_thread_id(thread_id):
            continue
        answer = _lookup(load(candidate, environ), wanted)
        if answer is not None:
            return answer
    return None


def _lookup(images: List[Dict[str, Any]], wanted: str) -> Optional[tuple]:
    for image in images:
        if str(image.get("id") or "").upper() != wanted:
            continue
        mime = image.get("mime")
        encoded = image.get("base64")
        if not isinstance(mime, str) or not isinstance(encoded, str) or not encoded:
            return None
        return mime, f"data:{mime};base64,{encoded}"
    return None


def _recent_threads(environ: Optional[Dict[str, str]] = None) -> List[str]:
    """Stored task ids, newest first."""

    root = cache_dir(environ)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    entries: List[tuple] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            entries.append((os.path.getmtime(os.path.join(root, name)), name[: -len(".json")]))
        except OSError:
            continue
    return [name for _, name in sorted(entries, reverse=True)[:REPLAY_CACHE_MAX_TASKS]]


def known_ids(thread_id: Any, environ: Optional[Dict[str, str]] = None) -> List[str]:
    return [
        str(image.get("id"))
        for image in load(thread_id, environ)
        if isinstance(image.get("id"), str)
    ]
