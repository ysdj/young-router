"""Per-task image context: what the panel shows, and what the user chooses.

The pass decides which images go out; this domain is how a person sees and
changes that decision.  It reads the same replay cache the fetch tool answers
from and writes the same selection file Context Attachment Manager's panel
writes, so a task curated here behaves identically to one curated there.

This app owns only the two image maps of that document.  A task's
``auto`` switch is Context Attachment Manager's own -- set in its panel -- and
is read, never written, here; every write keeps the rest of the file intact.

Nothing here decides a request: a change takes effect on that task's next turn,
because the pass reads this state fresh for every request.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._shared import DomainError, _action_name, _mapping
from ...proxy import image_context as _image_context
from ...proxy import image_context_store as _store

DOMAIN_NAME = "image_context"
# One message may carry 16 MiB.  An image at or below this travels as its own
# stored bytes, unchanged: re-encoding it would show a picture the model never
# received.
MAX_PREVIEW_BYTES = 6 * 1024 * 1024
# Anything larger is scaled to this long edge before it is sent.  The stored
# original stays what the pass and the fetch tool use; a window only needs a
# picture, and a full-size PNG can be 28 MB of base64 -- past the IPC frame
# budget, where the old answer was a refusal instead of an image.
PREVIEW_LONG_EDGE = 1600
PREVIEW_BUDGET_BYTES = 1_500_000
# A card in the grid is 148 pt wide.  Its picture is a thumbnail, not a preview:
# sending a 1600 px copy for every image of a 70-image task was megabytes of
# frames for a wall of postage stamps, and the cards that had not been asked for
# yet sat on "loading" while the queue drained.
THUMB_LONG_EDGE = 400
THUMB_BUDGET_BYTES = 160_000


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _codex_home(environ: Mapping[str, str]) -> Path:
    """Where Codex keeps its own files: ``CODEX_HOME``, else ``~/.codex``."""

    configured = str(environ.get("CODEX_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _read_thread_titles(environ: Mapping[str, str]) -> dict[str, str]:
    """Thread id -> the title Codex itself shows for that thread.

    A task id here is a Codex thread id, and Codex writes one line per thread
    to ``session_index.jsonl`` with the name the user sees in its own window.
    A panel that named its rows by timestamp alone made two threads of the same
    minute indistinguishable, so this is the name every other surface of the
    product already uses.  A missing or unreadable index is not an error: the
    titles are decoration, and the pane falls back to the clock.
    """

    try:
        with open(_codex_home(environ) / "session_index.jsonl", "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return {}
    titles: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        thread_id = str(entry.get("id") or "").strip()
        title = str(entry.get("thread_name") or "").strip()
        # Later lines win: the index is append-only and a thread that was
        # renamed appears again with its newest name.
        if thread_id and title:
            titles[thread_id] = title
    return titles


class ImageContextDomain:
    """Read a task's images, and write one task's selection."""

    name = DOMAIN_NAME

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environment = dict(environ or {})
        self.revision = 0
        # What the panel is looking at.  It travels in the snapshot because the
        # typed IPC contract carries state, not per-call reads: opening a task
        # and previewing an image are actions like any other.
        self._task: str = ""
        self._preview: dict[str, Any] | None = None
        # Per-replay summaries, keyed by the task and stamped with the file's
        # own identity.  A stored replay carries every image's bytes, so parsing
        # one costs seconds on a long task; reading all of them for a snapshot
        # (and again for every image a window asks for) is what made the window
        # sit on "loading" forever.
        self._summaries: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        # Codex's own title index, re-read only when that file changes.
        self._titles: dict[str, str] = {}
        self._titles_identity: tuple[int, int] | None = None

    # -- environment -------------------------------------------------------
    def _env(self) -> dict[str, str]:
        return {**os.environ, **self._environment}

    def cache_dir(self) -> Path:
        return Path(_store.cache_dir(self._env()))

    def selection_dir(self) -> Path:
        return Path(_image_context.data_dir(self._env())) / "selection"

    def thread_titles(self) -> dict[str, str]:
        path = _codex_home(self._env()) / "session_index.jsonl"
        try:
            details = path.stat()
        except OSError:
            self._titles, self._titles_identity = {}, None
            return {}
        identity = (details.st_mtime_ns, details.st_size)
        if identity != self._titles_identity:
            self._titles = _read_thread_titles(self._env())
            self._titles_identity = identity
        return self._titles

    def environment(self) -> dict[str, Any]:
        env = self._env()
        return {
            "mode": _image_context.mode(env),
            "fetch": _image_context.fetch_mode(env),
            # What the placeholders actually promise on this host: a file the
            # client opens, the fetch tool, or asking the user.  The panel says
            # this in words, so it never shows a raw enum to a person.
            "fetch_kind": _image_context.fetch_kind(env),
            "fetch_available": _image_context.fetch_available(env),
            "enabled": _image_context.enabled(env),
            "cache_dir": str(self.cache_dir()),
            "selection_dir": str(self.selection_dir()),
        }

    # -- reads -------------------------------------------------------------
    def tasks(self) -> list[dict[str, Any]]:
        root = self.cache_dir()
        try:
            names = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return []
        tasks: list[dict[str, Any]] = []
        titles = self.thread_titles()
        seen: set[str] = set()
        for path in names[:_store.REPLAY_CACHE_MAX_TASKS]:
            task_id = path.stem
            try:
                details = path.stat()
            except OSError:
                continue
            # A replay is immutable in meaning but rewritten in place, so its own
            # size and mtime are the identity of what was parsed from it.
            identity = (details.st_mtime_ns, details.st_size)
            cached = self._summaries.get(task_id)
            if cached is None or cached[0] != identity:
                images = _store.load(task_id, self._env())
                if not images:
                    self._summaries.pop(task_id, None)
                    continue
                cached = (
                    identity,
                    {
                        "images": len(images),
                        "bytes": sum(len(image.get("base64") or "") for image in images) * 3 // 4,
                    },
                )
                self._summaries[task_id] = cached
            seen.add(task_id)
            summary = cached[1]
            selection = _image_context.read_selection(task_id, self._env())
            unchecked = selection.get("unchecked") or set()
            pinned = selection.get("pinned") or set()
            auto = selection.get("auto")
            tasks.append(
                {
                    "id": task_id,
                    # The name Codex shows for this thread; empty when the
                    # index has no line for it, which the pane reads as
                    # "name unknown" rather than inventing one.
                    "title": titles.get(task_id, ""),
                    "images": summary["images"],
                    "unchecked": len(unchecked),
                    "pinned": len(pinned),
                    "auto": auto if isinstance(auto, bool) else _image_context.mode(self._env()) == "auto",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(details.st_mtime)),
                    "bytes": summary["bytes"],
                }
            )
        for stale in [key for key in self._summaries if key not in seen]:
            self._summaries.pop(stale, None)
        return tasks

    def state(self, task_id: str) -> dict[str, Any]:
        """One task's images and their state, newest first.

        The pass numbers images in wire order; the panel shows them newest
        first, because that is the one being worked on.  Both carry the id, so
        the two orders never have to agree.
        """

        task_id = self._require_task(task_id)
        task = next(
            (entry for entry in self.tasks() if entry["id"] == task_id),
            {"id": task_id},
        )
        images = _store.load(task_id, self._env())
        selection = _image_context.read_selection(task_id, self._env())
        unchecked = selection.get("unchecked") or set()
        pinned = selection.get("pinned") or set()
        auto = selection.get("auto")
        rows: list[dict[str, Any]] = []
        for image in images:
            image_id = str(image.get("id") or "")
            rows.append(
                {
                    "id": image_id,
                    "turn": image.get("turn"),
                    "kind": image.get("kind"),
                    "name": image.get("name"),
                    "width": image.get("width"),
                    "height": image.get("height"),
                    "bytes": len(image.get("base64") or "") * 3 // 4,
                    "pinned": image_id in pinned,
                    "unchecked": image_id in unchecked,
                }
            )
        rows.reverse()
        return {
            "task": task,
            "auto": auto if isinstance(auto, bool) else _image_context.mode(self._env()) == "auto",
            "images": rows,
            # What the next turn will actually do, stated as a number so the
            # panel never has to re-implement the pass's own rule.
            "next": (
                {
                    # Automatic selection omits every earlier turn: only a
                    # pinned image is guaranteed a place in the next request,
                    # and this turn's own images are not in the replay yet.
                    "sent": sum(1 for row in rows if row["pinned"]),
                    "omitted": sum(1 for row in rows if not row["pinned"]),
                }
                if (auto if isinstance(auto, bool) else _image_context.mode(self._env()) == "auto")
                else {
                    "sent": sum(1 for row in rows if not row["unchecked"]),
                    "omitted": sum(1 for row in rows if row["unchecked"]),
                }
            ),
            "environment": self.environment(),
        }

    def image(self, task_id: str, image_id: str, *, thumb: bool = False) -> dict[str, Any]:
        """One image's bytes, ready to be drawn.

        ``data_url`` is what a window shows: the stored bytes themselves while
        they fit, and otherwise a scaled re-encoding (see ``_scaled``), so a
        large original is never a refusal.  ``downscaled`` tells the caller the
        picture it receives is not the stored one, which is the difference
        between "this is the image" and "this is a smaller copy of it".
        """

        task_id = self._require_task(task_id)
        for image in _store.load(task_id, self._env()):
            if str(image.get("id") or "") != image_id:
                continue
            mime = image.get("mime")
            encoded = image.get("base64")
            if not isinstance(mime, str) or not isinstance(encoded, str) or not encoded:
                raise DomainError("That image has no stored bytes")
            size = len(encoded) * 3 // 4
            data_url: str | None = None
            width = image.get("width")
            height = image.get("height")
            downscaled = False
            # A thumbnail is scaled whenever it is bigger than a card, even
            # while the stored bytes would fit a message: what the caller asked
            # for is a small picture, not the stored one.
            if thumb and (size > THUMB_BUDGET_BYTES or max(int(width or 0), int(height or 0)) > THUMB_LONG_EDGE):
                scaled = _scaled(encoded, THUMB_LONG_EDGE, THUMB_BUDGET_BYTES)
                if scaled is not None:
                    data_url, width, height = scaled
                    downscaled = True
                else:
                    data_url = f"data:{mime};base64,{encoded}" if size <= MAX_PREVIEW_BYTES else None
            elif size <= MAX_PREVIEW_BYTES:
                data_url = f"data:{mime};base64,{encoded}"
            else:
                scaled = _scaled(encoded)
                if scaled is not None:
                    data_url, width, height = scaled
                    downscaled = True
            return {
                "task": task_id,
                "id": image_id,
                "mime": mime,
                "bytes": size,
                "width": width,
                "height": height,
                "turn": image.get("turn"),
                "name": image.get("name"),
                "data_url": data_url,
                "downscaled": downscaled,
            }
        raise DomainError(f"{image_id} is not an image of this task")

    # -- writes ------------------------------------------------------------
    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set", "select", "check"}:
            task_id = str(data.get("task") or "").strip()
            image_id = str(data.get("id") or "").strip()
            included = data.get("included")
            if not isinstance(included, bool):
                raise DomainError("An image selection must be on or off")
            self._write_selection(task_id, image_id, included=included)
        elif name in {"pin", "set_pinned"}:
            task_id = str(data.get("task") or "").strip()
            image_id = str(data.get("id") or "").strip()
            pinned = data.get("pinned")
            if not isinstance(pinned, bool):
                raise DomainError("An image pin must be on or off")
            self._write_pin(task_id, image_id, pinned=pinned)
        elif name in {"open", "select_task"}:
            task_id = str(data.get("task") or "").strip()
            self._require_task(task_id)
            self._task = task_id
            self._preview = None
        elif name in {"preview", "show"}:
            task_id = str(data.get("task") or "").strip() or self._task
            image_id = str(data.get("id") or "").strip()
            self._task = task_id
            # The snapshot says *which* image is open and how big it is; the
            # bytes are asked for by id on their own read.  A snapshot is
            # broadcast to every window and redacted field by field, so a
            # picture inside one would be re-encoded as a string on every
            # revision and every window would pay for it.
            entry = self.image(task_id, image_id)
            entry.pop("data_url", None)
            self._preview = entry
        elif name in {"close_preview", "hide"}:
            self._preview = None
        elif name in {"forget", "delete"}:
            task_id = str(data.get("task") or "").strip()
            self._require_task(task_id)
            # The file copies are part of the record: leaving them for the next
            # sweep kept a forgotten task's pictures readable for up to a day.
            if not _store.forget(task_id, self._env()):
                raise DomainError("That task's replay could not be removed")
        elif name in {"reload", "refresh"}:
            pass
        else:
            raise DomainError("The requested image context action is unavailable")
        self.revision += 1
        return self.snapshot()

    def _selection(self, task_id: str) -> dict[str, Any]:
        """The selection document to write back, with Context Attachment Manager's
        own keys left where they are.

        This app owns only the two image maps.  ``auto``, ``autoAt``,
        ``autoSince`` and anything a newer release adds belong to that panel,
        and a rewrite that rebuilt the file dropped them: the switch shown in
        its panel, the fork rule that reads ``autoAt``, and the copy rule that
        reads ``autoSince`` all changed under the user.  A mark that is already
        there keeps its own timestamp for the same reason -- a fork compares
        those stamps against the moment it was made.
        """

        document: dict[str, Any] = {}
        try:
            with open(
                _image_context._thread_state_path(task_id, self._env()),
                "r",
                encoding="utf-8",
            ) as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                document = dict(loaded)
        except (OSError, ValueError):
            document = {}

        def marks(key: str) -> dict[str, Any]:
            raw = document.get(key)
            if not isinstance(raw, dict):
                return {}
            found: dict[str, Any] = {}
            for image_id, entry in raw.items():
                if not isinstance(image_id, str):
                    continue
                found[image_id] = (
                    entry
                    if isinstance(entry, dict)
                    else {"id": image_id, "at": _now()}
                )
            return found

        document["threadId"] = task_id
        document["unchecked"] = marks("unchecked")
        document["pinned"] = marks("pinned")
        return document

    def _write(self, task_id: str, selection: dict[str, Any]) -> None:
        directory = self.selection_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{task_id}.json"
            temporary = target.with_suffix(f".tmp.{os.getpid()}")
            temporary.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        except OSError as error:
            raise DomainError("The image selection could not be saved") from error

    def _write_selection(self, task_id: str, image_id: str, *, included: bool) -> None:
        task = self._require_task(task_id)
        known = {str(image.get("id")) for image in _store.load(task_id, self._env())}
        if image_id not in known:
            raise DomainError(f"{image_id} is not an image of this task")
        selection = self._selection(task_id)
        # "Included" is expressed two ways, exactly as the attachment manager
        # does: pinned means every turn sends it, and not-unchecked means it is
        # sent like its turn's other images.  A panel checkbox is the second,
        # which is the one a person means by ticking a box.
        selection["unchecked"].pop(image_id, None)
        if not included:
            selection["unchecked"][image_id] = {"id": image_id, "at": _now()}
            selection["pinned"].pop(image_id, None)
        self._write(task_id, selection)

    def _write_pin(self, task_id: str, image_id: str, *, pinned: bool) -> None:
        """Pin an image: with automatic selection on, this is what "send" means.

        Automatic selection omits everything from earlier turns by itself, so a
        checkbox that only cleared an "unchecked" entry would have no visible
        effect.  Pinning is the same word the attachment manager's panel uses for
        "every turn", and it is the one the pass reads for that purpose.
        """

        task_id = self._require_task(task_id)
        known = {str(image.get("id")) for image in _store.load(task_id, self._env())}
        if image_id not in known:
            raise DomainError(f"{image_id} is not an image of this task")
        selection = self._selection(task_id)
        selection["pinned"].pop(image_id, None)
        if pinned:
            selection["pinned"][image_id] = {"id": image_id, "at": _now()}
            selection["unchecked"].pop(image_id, None)
        self._write(task_id, selection)

    # -- helpers -----------------------------------------------------------
    def _require_task(self, task_id: str) -> str:
        safe = _image_context._safe_thread_file_name(task_id)
        if not safe:
            raise DomainError("That task cannot be named")
        # The name is checked against the cache itself, not against a listing:
        # listing loads every replay, which is the whole cost this guard must
        # never pay on the path that serves one picture.
        if not (self.cache_dir() / f"{safe}.json").is_file():
            raise DomainError("That task has no stored images")
        return safe

    def snapshot(self) -> dict[str, Any]:
        tasks = self.tasks()
        current = self._task if any(entry["id"] == self._task for entry in tasks) else ""
        if not current and tasks:
            current = tasks[0]["id"]
        self._task = current
        return {
            "domain": DOMAIN_NAME,
            "revision": self.revision,
            "tasks": tasks,
            "task": self.state(current) if current else None,
            "preview": self._preview,
            "environment": self.environment(),
        }

    def draft_state(self) -> object:
        """The projection a staged edit compares against.

        This domain owns no document: a selection change is written at once,
        so its draft is its own state and it is never dirty.
        """

        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        self.revision += 1
        return self.snapshot()

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        del payload
        return {"valid": True, "issues": []}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        del payload
        return {}


def _scaled(
    encoded: str,
    long_edge: int = PREVIEW_LONG_EDGE,
    budget: int = PREVIEW_BUDGET_BYTES,
) -> tuple[str, int, int] | None:
    """A drawable copy of one oversized image, or None when it cannot be made.

    Pillow ships with the runtime the app installs (LiteLLM pulls it in), so a
    full-size picture is scaled here rather than refused.  A source checkout
    may not have it, and a decode can fail on bytes the pass stored anyway:
    both answer None, and the caller states plainly that the image is on disk
    instead of pretending a window failed.
    """

    try:
        from PIL import Image
    except Exception:
        return None
    try:
        raw = base64.b64decode(encoded, validate=False)
        with Image.open(io.BytesIO(raw)) as source:
            picture = source.convert("RGB") if source.mode not in {"RGB", "L"} else source
            picture.thumbnail((long_edge, long_edge))
            width, height = picture.size
            payload = b""
            for quality in (86, 78, 70):
                buffer = io.BytesIO()
                picture.save(buffer, format="JPEG", quality=quality, optimize=True)
                payload = buffer.getvalue()
                if len(payload) <= budget:
                    break
        return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"), width, height
    except Exception:
        return None


def build(environ: Mapping[str, str] | None = None) -> ImageContextDomain:
    return ImageContextDomain(environ)
