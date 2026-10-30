"""Per-task image context: what the panel shows, and what the user chooses.

The pass decides which images go out; this domain is how a person sees and
changes that decision.  It reads the same replay cache the fetch tool answers
from and writes the same selection file Context Attachment Manager's panel
writes, so a task curated here behaves identically to one curated there.

Nothing here decides a request: a change takes effect on that task's next turn,
because the pass reads this state fresh for every request.
"""

from __future__ import annotations

import base64
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
# One message may carry 16 MiB; a preview that needs more than this is sent as
# metadata only, because a panel thumbnail is not worth a stalled window.
MAX_PREVIEW_BYTES = 6 * 1024 * 1024


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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

    # -- environment -------------------------------------------------------
    def _env(self) -> dict[str, str]:
        return {**os.environ, **self._environment}

    def cache_dir(self) -> Path:
        return Path(_store.cache_dir(self._env()))

    def selection_dir(self) -> Path:
        return Path(_image_context.data_dir(self._env())) / "selection"

    def environment(self) -> dict[str, Any]:
        env = self._env()
        return {
            "mode": _image_context.mode(env),
            "fetch": _image_context.fetch_mode(env),
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
        for path in names[:_store.REPLAY_CACHE_MAX_TASKS]:
            task_id = path.stem
            images = _store.load(task_id, self._env())
            if not images:
                continue
            selection = _image_context.read_selection(task_id, self._env())
            unchecked = selection.get("unchecked") or set()
            pinned = selection.get("pinned") or set()
            auto = selection.get("auto")
            try:
                updated = path.stat().st_mtime
            except OSError:
                updated = 0.0
            tasks.append(
                {
                    "id": task_id,
                    "images": len(images),
                    "unchecked": len(unchecked),
                    "pinned": len(pinned),
                    "auto": auto if isinstance(auto, bool) else _image_context.mode(self._env()) == "auto",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated)),
                    "bytes": sum(len(image.get("base64") or "") for image in images) * 3 // 4,
                }
            )
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

    def image(self, task_id: str, image_id: str) -> dict[str, Any]:
        """One image's bytes, or its metadata when it is too large to send."""

        task_id = self._require_task(task_id)
        for image in _store.load(task_id, self._env()):
            if str(image.get("id") or "") != image_id:
                continue
            mime = image.get("mime")
            encoded = image.get("base64")
            if not isinstance(mime, str) or not isinstance(encoded, str) or not encoded:
                raise DomainError("That image has no stored bytes")
            size = len(encoded) * 3 // 4
            entry = {
                "task": task_id,
                "id": image_id,
                "mime": mime,
                "bytes": size,
                "width": image.get("width"),
                "height": image.get("height"),
                "turn": image.get("turn"),
                "name": image.get("name"),
                "data_url": (
                    f"data:{mime};base64,{encoded}"
                    if size <= MAX_PREVIEW_BYTES
                    else None
                ),
            }
            return entry
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
        elif name in {"auto", "set_auto"}:
            task_id = str(data.get("task") or "").strip()
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise DomainError("Automatic selection must be on or off")
            self._write_auto(task_id, enabled)
        elif name in {"open", "select_task"}:
            task_id = str(data.get("task") or "").strip()
            self._require_task(task_id)
            self._task = task_id
            self._preview = None
        elif name in {"preview", "show"}:
            task_id = str(data.get("task") or "").strip() or self._task
            image_id = str(data.get("id") or "").strip()
            self._task = task_id
            self._preview = self.image(task_id, image_id)
        elif name in {"close_preview", "hide"}:
            self._preview = None
        elif name in {"forget", "delete"}:
            task_id = str(data.get("task") or "").strip()
            self._require_task(task_id)
            try:
                (self.cache_dir() / f"{task_id}.json").unlink()
            except OSError as error:
                raise DomainError("That task's replay could not be removed") from error
        elif name in {"reload", "refresh"}:
            pass
        else:
            raise DomainError("The requested image context action is unavailable")
        self.revision += 1
        return self.snapshot()

    def _selection(self, task_id: str) -> dict[str, Any]:
        selection = _image_context.read_selection(task_id, self._env())
        return {
            "threadId": task_id,
            "unchecked": {
                image_id: {"id": image_id, "at": _now()}
                for image_id in sorted(selection.get("unchecked") or set())
            },
            "pinned": {
                image_id: {"id": image_id, "at": _now()}
                for image_id in sorted(selection.get("pinned") or set())
            },
            **({"auto": True} if selection.get("auto") is True else {}),
            **({"auto": False} if selection.get("auto") is False else {}),
        }

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

    def _write_auto(self, task_id: str, enabled: bool) -> None:
        task = self._require_task(task_id)
        selection = self._selection(task_id)
        selection["auto"] = enabled
        selection["autoAt"] = _now()
        self._write(task_id, selection)

    # -- helpers -----------------------------------------------------------
    def _require_task(self, task_id: str) -> str:
        safe = _image_context._safe_thread_file_name(task_id)
        if not safe:
            raise DomainError("That task cannot be named")
        for entry in self.tasks():
            if entry["id"] == safe:
                return safe
        raise DomainError("That task has no stored images")

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


def build(environ: Mapping[str, str] | None = None) -> ImageContextDomain:
    return ImageContextDomain(environ)
