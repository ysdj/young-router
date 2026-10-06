"""In-process adapter for the staged Veridrop quick-mode endpoint detector.

Veridrop (https://github.com/canarybyte/veridrop, AGPL-3.0-or-later) probes one
API endpoint and reports what actually answers behind a model name: a route that
serves the model it advertises passes, a route that answers with a cheaper
backend, a different brand, or a rewritten protocol fails.  Its ``quick`` mode
runs the cheap, structural half of that suite - identity, thinking signature,
consistency, protocol, and message id - so a check costs a handful of small
requests instead of a long-context scan.

The repository never carries a copy of that program: ``scripts/update_veridrop.py``
stages the upstream package on every artifact build (and re-checks the latest
upstream revision), the bundle installs the one or two upstream dependencies the
runtime does not already carry, and this adapter imports the staged package into
the Core's own interpreter and runs its quick suite directly.  The upstream
license text is staged beside it.

The staged directory contains::

    src/relay_detector/**     upstream package, verbatim
    LICENSE                   upstream AGPL-3.0 text
    pyproject.toml            upstream project metadata
    manifest.json             upstream revision, staging time, supported models

Which models the deep test may probe comes from the staged manifest, never from
a list kept here: upstream adds and drops models, and a build must not keep
offering a model the staged program no longer supports.

Every entry point tolerates an unstaged checkout: :func:`available` reports the
program state instead of raising, and :func:`run_quick` raises
:class:`VeridropUnavailable` so a caller can report a skipped probe instead of
failing the whole request.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence


_VERIDROP_DIR_ENV = "YOUNG_ROUTER_VERIDROP_DIR"
_VERIDROP_TIMEOUT_ENV = "YOUNG_ROUTER_VERIDROP_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 60.0
QUICK_MODE = "quick"
PROTOCOLS = ("anthropic", "openai", "gemini")
_SOURCE_DIR = "src"
_MAX_CONCURRENT_REQUESTS = 3
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class VeridropUnavailable(RuntimeError):
    """The staged Veridrop package is missing or cannot be imported."""


def staged_root() -> Path:
    """Directory holding the staged upstream package."""

    configured = os.environ.get(_VERIDROP_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("veridrop")


def source_root() -> Path:
    """The directory the upstream package is imported from."""

    return staged_root() / _SOURCE_DIR


def manifest() -> dict[str, Any]:
    """Read the staging manifest, or an empty mapping when unstaged."""

    path = staged_root() / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_model_name(value: object) -> str:
    """Normalize a public or upstream model name for a manifest lookup."""

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


def supported_models() -> dict[str, tuple[str, ...]]:
    """The upstream per-protocol model tables, from the staged manifest."""

    value = manifest().get("supported_models")
    if not isinstance(value, Mapping):
        return {}
    models: dict[str, tuple[str, ...]] = {}
    for protocol, names in value.items():
        if not isinstance(protocol, str) or not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            continue
        resolved = tuple(
            name
            for name in (normalize_model_name(item) for item in names)
            if name
        )
        if resolved:
            models[protocol] = resolved
    return models


def target(*names: object) -> dict[str, str] | None:
    """Return the supported model and its protocol for a model name, if any.

    The upstream name is consulted first and the public one after it, so a route
    that spells its own name differently still matches the model it calls.
    """

    models = supported_models()
    if not models:
        return None
    for name in names:
        normalized = normalize_model_name(name)
        if not normalized:
            continue
        for protocol, supported in models.items():
            if normalized in supported:
                return {"model": normalized, "protocol": protocol}
    return None


def _missing_staged_files() -> list[str]:
    root = staged_root()
    missing: list[str] = []
    if not (root / "manifest.json").is_file():
        missing.append("manifest.json")
    package = source_root() / "relay_detector"
    if not (package / "cli.py").is_file():
        missing.append(f"{_SOURCE_DIR}/relay_detector/")
    return missing


def available() -> bool:
    """True when the staged package can be imported from this interpreter."""

    if _missing_staged_files():
        return False
    try:
        _import_upstream()
    except VeridropUnavailable:
        return False
    return True


def engine() -> dict[str, Any]:
    """Describe the staged program for the UI and for probe results."""

    info = manifest()
    revision = info.get("revision")
    return {
        "name": "Veridrop",
        "source": str(info.get("source", "https://github.com/canarybyte/veridrop")),
        "license": str(info.get("license", "AGPL-3.0-or-later")),
        "revision": str(revision) if isinstance(revision, str) and revision else "",
        "staged_at": str(info.get("staged_at", "")),
        "mode": QUICK_MODE,
        "targets": sorted(
            f"{protocol}:{name}"
            for protocol, names in sorted(supported_models().items())
            for name in names
        ),
        "available": available(),
    }


def _import_upstream() -> tuple[Any, Any, Any, Any]:
    """Import the staged upstream entry points into this interpreter.

    The package directory is put on ``sys.path`` once; everything it imports
    from outside itself comes from the runtime's own site-packages, which the
    build completes with whatever the upstream project declares and the runtime
    does not already carry.
    """

    missing = _missing_staged_files()
    if missing:
        raise VeridropUnavailable(
            "Veridrop is not staged; run scripts/update_veridrop.py before building "
            f"(missing: {', '.join(missing)})"
        )
    source = str(source_root())
    if source not in sys.path:
        sys.path.insert(0, source)
    try:
        from relay_detector import cli as cli_module
        from relay_detector.models import ExecutionConfig, Mode, Protocol
    except Exception as exc:  # noqa: BLE001 - reported as an unavailable probe
        raise VeridropUnavailable(f"Veridrop could not be imported: {exc}") from exc
    run_detect = getattr(cli_module, "_run_detect", None)
    if run_detect is None:
        raise VeridropUnavailable("The staged Veridrop no longer publishes its quick-suite runner")
    return cli_module, run_detect, ExecutionConfig, Mode, Protocol


def _timeout_seconds(value: float | None) -> float:
    if value is not None:
        return max(1.0, float(value))
    raw = os.environ.get(_VERIDROP_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


def run_quick(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run Veridrop's quick suite against one endpoint and model.

    Returns the upstream verdict together with the detector tally::

        {"protocol": str, "model": str, "verdict": str, "score": float,
         "summary": str, "run_error": str | None, "identity": str | None,
         "brands": [str], "detectors": [{"name", "status", "score", "error"}],
         "skipped": [str], "failed": [str]}

    Raises :class:`VeridropUnavailable` when the staged package is missing, and
    ``RuntimeError`` when the suite itself fails.
    """

    normalized_protocol = str(protocol or "").strip().lower()
    if normalized_protocol not in PROTOCOLS:
        raise ValueError(f"Veridrop cannot probe the {protocol!r} protocol")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Veridrop needs the endpoint address")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Veridrop needs the route credential")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Veridrop needs the upstream model name")

    cli_module, run_detect, execution_config, mode, protocol_type = _import_upstream()
    try:
        selected_protocol = protocol_type(normalized_protocol)
    except ValueError as exc:
        raise ValueError(f"Veridrop cannot probe the {protocol!r} protocol") from exc
    config = execution_config.for_mode(mode.QUICK, max_concurrent=_MAX_CONCURRENT_REQUESTS)
    config.overall_timeout_s = _timeout_seconds(timeout_seconds)

    with tempfile.TemporaryDirectory(prefix="young-router-veridrop-") as directory:
        report_path = Path(directory) / "report.json"
        # The suite renders a terminal report through rich; the Core has no
        # terminal of its own, so its own noise is captured and dropped instead
        # of reaching the application log.
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                asyncio.run(
                    run_detect(
                        selected_protocol,
                        base_url.strip(),
                        api_key,
                        model,
                        config,
                        report_path,
                    )
                )
        except RuntimeError as exc:
            if "asyncio.run() cannot be called" in str(exc):
                raise RuntimeError(
                    "Veridrop needs a thread without a running event loop"
                ) from exc
            raise RuntimeError(f"Veridrop failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - reported as a failed finding
            raise RuntimeError(f"Veridrop failed: {exc}") from exc
        report = _read_report(report_path, captured.getvalue())
    return _normalize_report(report)


def _read_report(report_path: Path, output: str) -> dict[str, Any]:
    """The JSON report Veridrop wrote, or a failure naming what it printed."""

    try:
        payload = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        detail = output.strip()
        raise RuntimeError(
            "Veridrop wrote no report"
            + (f": {detail[-600:]}" if detail else f": {exc}")
        ) from exc
    if len(payload) > _MAX_OUTPUT_BYTES:
        raise RuntimeError("Veridrop returned an oversized report")
    try:
        decoded = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"Veridrop returned an unreadable report: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Veridrop returned an unexpected report shape")
    return decoded


def _normalize_report(report: Mapping[str, Any]) -> dict[str, Any]:
    detectors: list[dict[str, Any]] = []
    results = report.get("results")
    if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            detectors.append(
                {
                    "name": str(item.get("name", "")),
                    "status": str(item.get("status", "")),
                    "score": _optional_float(item.get("score")) or 0.0,
                    "error": str(item.get("error") or ""),
                }
            )
    outcome = str(report.get("verdict") or "marginal")
    if outcome not in {"passed", "marginal", "failed"}:
        outcome = "marginal"
    brands = report.get("detected_non_anthropic_brands")
    identity = report.get("self_reported_identity")
    return {
        "protocol": str(report.get("protocol") or ""),
        "model": str(report.get("target_model") or ""),
        "verdict": outcome,
        "score": _optional_float(report.get("total_score")) or 0.0,
        "summary": str(report.get("summary") or ""),
        "run_error": str(report.get("run_error") or "") or None,
        "identity": identity.strip() if isinstance(identity, str) and identity.strip() else None,
        "brands": [str(brand) for brand in brands][:8] if isinstance(brands, list) else [],
        "detectors": detectors,
        "skipped": [item["name"] for item in detectors if item["status"] == "skip"],
        "failed": [item["name"] for item in detectors if item["status"] in {"fail", "error"}],
    }


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PROTOCOLS",
    "QUICK_MODE",
    "VeridropUnavailable",
    "available",
    "engine",
    "manifest",
    "normalize_model_name",
    "run_quick",
    "source_root",
    "staged_root",
    "supported_models",
    "target",
]
