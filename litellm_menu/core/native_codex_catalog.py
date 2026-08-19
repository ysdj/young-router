"""Read the model catalog bundled with the installed native Codex binary."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any


NATIVE_CATALOG_TIMEOUT_SECONDS = 2.0
NATIVE_CATALOG_MAX_BYTES = 16 * 1024 * 1024
_NATIVE_CATALOG_COMMAND = ("debug", "models", "--bundled")

_CACHE_LOCK = threading.RLock()
_CATALOG_CACHE: dict[tuple[str, int, int], tuple[dict[str, Any], ...]] = {}


def _append_candidate(result: list[Path], seen: set[str], value: object) -> None:
    if not isinstance(value, (str, Path)):
        return
    raw = os.fspath(value).strip()
    if not raw:
        return
    path = Path(raw).expanduser()
    try:
        normalized = str(path.resolve(strict=False))
    except OSError:
        normalized = str(path.absolute())
    if normalized in seen:
        return
    seen.add(normalized)
    try:
        if not path.is_file():
            return
        if os.name != "nt" and not os.access(path, os.X_OK):
            return
    except OSError:
        return
    result.append(path)


def native_codex_executable_candidates() -> tuple[Path, ...]:
    """Return bounded, platform-aware candidates for the native Codex CLI."""

    result: list[Path] = []
    seen: set[str] = set()

    # The override is useful for portable installs and for hosts that do not
    # expose the native CLI in the desktop app's inherited PATH.
    _append_candidate(
        result,
        seen,
        os.environ.get("LITELLM_MENU_CODEX_EXECUTABLE", ""),
    )
    for command in ("codex", "codex-cli"):
        _append_candidate(result, seen, shutil.which(command))

    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            for app in ("ChatGPT.app", "Codex.app"):
                _append_candidate(result, seen, root / app / "Contents" / "Resources" / "codex")
    elif os.name == "nt":
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("APPDATA", ""),
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ]
        for raw_root in roots:
            if not raw_root:
                continue
            root = Path(raw_root)
            for relative in (
                Path("Programs") / "ChatGPT" / "resources" / "codex.exe",
                Path("ChatGPT") / "resources" / "codex.exe",
                Path("OpenAI") / "Codex" / "codex.exe",
                Path("Codex") / "codex.exe",
            ):
                _append_candidate(result, seen, root / relative)

    return tuple(result)


def _models_from_payload(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw_models = payload.get("models")
    if not isinstance(raw_models, Sequence) or isinstance(raw_models, (str, bytes, bytearray)):
        return []
    models: list[dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping):
            continue
        slug = raw_model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        models.append(copy.deepcopy(dict(raw_model)))
    return models


def read_native_catalog(
    executable: Path | str,
    *,
    runner: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Read one native catalog without invoking a shell or a refresh."""

    invoke = runner or subprocess.run
    try:
        completed = invoke(
            [os.fspath(executable), *_NATIVE_CATALOG_COMMAND],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=NATIVE_CATALOG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    if getattr(completed, "returncode", None) != 0:
        return []
    output = getattr(completed, "stdout", None)
    if isinstance(output, str):
        encoded = output.encode("utf-8")
    elif isinstance(output, bytes):
        encoded = output
    else:
        return []
    if len(encoded) > NATIVE_CATALOG_MAX_BYTES:
        return []
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    return _models_from_payload(payload)


def _cache_key(executable: Path) -> tuple[str, int, int] | None:
    try:
        details = executable.stat()
    except OSError:
        return None
    return (str(executable), int(details.st_mtime_ns), int(details.st_size))


def load_native_catalog() -> list[dict[str, Any]]:
    """Load and cache the native catalog, invalidating when the binary changes."""

    for executable in native_codex_executable_candidates():
        key = _cache_key(executable)
        if key is None:
            continue
        with _CACHE_LOCK:
            cached = _CATALOG_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(list(cached))
        models = read_native_catalog(executable)
        if not models:
            continue
        with _CACHE_LOCK:
            _CATALOG_CACHE[key] = tuple(copy.deepcopy(models))
        return models
    return []


__all__ = [
    "NATIVE_CATALOG_MAX_BYTES",
    "NATIVE_CATALOG_TIMEOUT_SECONDS",
    "load_native_catalog",
    "native_codex_executable_candidates",
    "read_native_catalog",
]
