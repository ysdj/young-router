"""Python adapter for the bundled TraceOne degradation-fingerprint worker.

TraceOne (https://github.com/wangchao0708/TraceOne) attributes one model answer
to a Codex route by comparing the integers a model picks "from first instinct"
against a reference bank.  The repository never carries a copy of that
classifier: ``scripts/update_traceone.py`` stages the upstream ``dist`` module,
its artifacts, and the frozen web prompt on every artifact build (and re-checks
the latest upstream revision), and this adapter runs the staged JavaScript
through the bundled Node.js runtime - the same pattern as the
``pi-web-access`` bridge.

The staged directory contains::

    traceone.js                     upstream dist module (ESM)
    prompt.txt                      upstream frozen web prompt
    manifest.json                   upstream revision, staging time, target routes
    data/unified_bank.json          reference fingerprints
    data/codex_low_v4_adapter_415.json
    data/codex_low_v4_support_415.json

Every entry point tolerates an unstaged checkout: :func:`available` reports the
engine state instead of raising, and :func:`identify` raises
:class:`TraceOneUnavailable` so a caller can report a skipped probe instead of
failing the whole request.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
import uuid


_TRACEONE_DIR_ENV = "YOUNG_ROUTER_TRACEONE_DIR"
_TRACEONE_NODE_ENV = "YOUNG_ROUTER_TRACEONE_NODE"
_TRACEONE_WORKER_ENV = "YOUNG_ROUTER_TRACEONE_WORKER"
_TRACEONE_TIMEOUT_ENV = "YOUNG_ROUTER_TRACEONE_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 300.0

# The routes TraceOne can attribute.  The staged manifest is authoritative and
# is refreshed on every build; this tuple only covers a checkout whose staged
# files are not present yet, so the UI can still describe the deep test.
FALLBACK_TARGET_MODELS = (
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-astra",
)

_REQUIRED_FILES = (
    "traceone.js",
    "prompt.txt",
    "data/unified_bank.json",
    "data/codex_low_v4_adapter_415.json",
    "data/codex_low_v4_support_415.json",
)


class TraceOneUnavailable(RuntimeError):
    """The staged TraceOne engine or its Node.js runtime is missing."""


def _core_root() -> Path:
    return Path(__file__).resolve().parents[1]


def staged_root() -> Path:
    """Directory holding the staged upstream module and its artifacts."""

    configured = os.environ.get(_TRACEONE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("traceone")


def worker_path() -> Path:
    configured = os.environ.get(_TRACEONE_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("traceone_worker.mjs")


def node_command() -> str:
    """Resolve Node.js: an explicit override, the bundled runtime, then PATH."""

    configured = os.environ.get(_TRACEONE_NODE_ENV, "").strip()
    if configured:
        return configured
    bundled_name = "node.exe" if os.name == "nt" else "node"
    candidates = (
        _core_root() / "runtime" / "bin" / bundled_name,
        _core_root() / "runtime" / "bin" / "node",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    node = shutil.which("node")
    if node:
        return node
    raise TraceOneUnavailable(
        "TraceOne requires the bundled Node.js runtime; "
        f"set {_TRACEONE_NODE_ENV} to its executable path"
    )


def manifest() -> dict[str, Any]:
    """Read the staging manifest, or an empty mapping when unstaged."""

    path = staged_root() / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_route_name(value: object) -> str:
    """Normalize a public or upstream model name for TraceOne route lookup."""

    if not isinstance(value, str):
        return ""
    name = value.strip().lower()
    if not name:
        return ""
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("[") and "]" in name:
        name = name.split("]", 1)[1]
    return name.strip()


def target_models() -> tuple[str, ...]:
    """Route names the staged engine can attribute."""

    value = manifest().get("target_models")
    if isinstance(value, list):
        names = tuple(
            name
            for name in (normalize_route_name(item) for item in value)
            if name
        )
        if names:
            return names
    return FALLBACK_TARGET_MODELS


def route_target(*names: object) -> str | None:
    """Return the TraceOne route a model name belongs to, if any."""

    known = set(target_models())
    for name in names:
        normalized = normalize_route_name(name)
        if normalized and normalized in known:
            return normalized
    return None


def prompt_text() -> str:
    """The upstream frozen one-call identity prompt."""

    path = staged_root() / "prompt.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TraceOneUnavailable(f"TraceOne prompt is missing: {path}") from exc
    if not text:
        raise TraceOneUnavailable(f"TraceOne prompt is empty: {path}")
    return text


def engine() -> dict[str, Any]:
    """Describe the staged engine for the UI and for probe results."""

    root = staged_root()
    info = manifest()
    revision = info.get("revision")
    return {
        "name": "TraceOne",
        "source": str(info.get("source", "https://github.com/wangchao0708/TraceOne")),
        "revision": str(revision) if isinstance(revision, str) and revision else "",
        "staged_at": str(info.get("staged_at", "")),
        "available": available(),
    }


def available() -> bool:
    """True when the staged module, its artifacts, and Node.js all exist."""

    root = staged_root()
    if not worker_path().is_file():
        return False
    if any(not (root / name).is_file() for name in _REQUIRED_FILES):
        return False
    try:
        node_command()
    except TraceOneUnavailable:
        return False
    return True


def _timeout_seconds(value: float | None) -> float:
    if value is not None:
        return max(1.0, float(value))
    raw = os.environ.get(_TRACEONE_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


def identify(answer_text: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
    """Attribute one model answer to a TraceOne route.

    Returns the upstream decision together with the parsed-number diagnostics::

        {"status": "identified" | "unknown", "label": str | None,
         "support_p_value": float | None, "numbers": int, "errors": [str]}

    Raises :class:`TraceOneUnavailable` when the engine or Node.js is missing,
    and ``RuntimeError`` when the worker itself fails.
    """

    if not isinstance(answer_text, str) or not answer_text.strip():
        raise ValueError("TraceOne needs the model answer text")
    root = staged_root()
    missing = [name for name in _REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise TraceOneUnavailable(
            "TraceOne is not staged; run scripts/update_traceone.py before building "
            f"(missing: {', '.join(missing)})"
        )
    worker = worker_path()
    if not worker.is_file():
        raise TraceOneUnavailable(f"TraceOne worker is missing: {worker}")

    request_id = uuid.uuid4().hex
    timeout = _timeout_seconds(timeout_seconds)
    command = [node_command(), str(worker), "--dir", str(root)]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps({"id": request_id, "text": answer_text}, ensure_ascii=False) + "\n",
            text=True,
            capture_output=True,
            env=os.environ.copy(),
            timeout=timeout + 10.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"TraceOne worker timed out after {timeout:g} seconds") from exc
    except OSError as exc:
        raise TraceOneUnavailable(f"Could not start the TraceOne worker: {exc}") from exc

    # The worker answers with exactly one JSON object.  Match the request id
    # when it is echoed (a request whose stdin never parsed keeps ``id: null``)
    # and otherwise accept the single answer line so its error survives.
    response: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(candidate, dict):
            continue
        if candidate.get("id") == request_id or "ok" in candidate:
            response = candidate
            break
    if response is None:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"TraceOne worker exited with status {completed.returncode}"
            + (f": {detail[:500]}" if detail else "")
        )
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "TraceOne worker failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("TraceOne worker returned an invalid result")
    return _normalize_result(result)


def _normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        parsed = {}
    numbers = parsed.get("numbers")
    errors = parsed.get("errors")
    status = result.get("status")
    label = result.get("label")
    return {
        "status": status if status in {"identified", "unknown"} else "unknown",
        "label": label if isinstance(label, str) and label else None,
        "support_p_value": _optional_float(result.get("supportPValue")),
        "support_distance": _optional_float(result.get("supportDistance")),
        "support_path": str(result.get("supportPath")) if isinstance(result.get("supportPath"), str) else "",
        "numbers": len(numbers) if isinstance(numbers, list) else 0,
        "errors": [str(item) for item in errors][:8] if isinstance(errors, list) else [],
    }


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FALLBACK_TARGET_MODELS",
    "TraceOneUnavailable",
    "available",
    "engine",
    "identify",
    "manifest",
    "node_command",
    "normalize_route_name",
    "prompt_text",
    "route_target",
    "staged_root",
    "target_models",
    "worker_path",
]
