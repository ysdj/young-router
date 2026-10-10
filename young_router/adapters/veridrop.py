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

The suite is driven through the per-protocol library entry points
``build_detectors`` / ``build_runner`` / ``make_client`` and its report is built
with upstream's own ``DetectionReport`` and scorer, which is the same path the
upstream project's own web application takes.  The ``detect`` CLI wrapper is
deliberately not the entry point: it renders a rich terminal report between the
run and the JSON write, so a route that fails a detector - the one case the deep
test exists to report - raises while formatting the failure and takes the whole
finding with it.  The suite's rendering is a terminal concern and this Core has
no terminal.

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
from datetime import datetime, timezone
import importlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, NamedTuple, Sequence


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


def protocol_for(*names: object) -> str:
    """Which staged suite can speak for a model name, from the name alone.

    The staged tables say which models upstream keeps a *reference* for; they
    do not say which names the suite can measure.  The quick suite measures any
    endpoint it can reach - its structural detectors compare a response against
    the request that asked for it, not against a stored fingerprint - so a name
    upstream has not listed yet is still probeable, and upstream's own entry
    points select the suite the same way this does.

    Empty means the name names no protocol, and the caller decides from what it
    knows about the route rather than from a guess made here.
    """

    for name in names:
        normalized = normalize_model_name(name)
        if not normalized:
            continue
        if normalized.startswith("claude") or "/claude" in normalized:
            return "anthropic"
        if normalized.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
            return "openai"
        if normalized.startswith("gemini") or "/gemini" in normalized:
            return "gemini"
    return ""


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


def _import_upstream() -> tuple[Any, Any, Any, Any, Any]:
    """Import the staged upstream entry points into this interpreter.

    The package directory is put on ``sys.path`` once; everything it imports
    from outside itself comes from the runtime's own site-packages, which the
    build completes with whatever the upstream project declares and the runtime
    does not already carry.

    Every object named here is one the adapter calls, so a release that renames
    one is caught by ``scripts/update_veridrop.py`` at build time instead of
    surfacing as a failed probe in someone's settings pane.
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
        from relay_detector.models import (
            DetectionReport,
            ExecutionConfig,
            Mode,
            Protocol,
            mask_api_key,
        )
        from relay_detector.scorer import (
            compute_total,
            effective_verdict,
            fatal_run_error,
            summary_text,
        )
    except Exception as exc:  # noqa: BLE001 - reported as an unavailable probe
        raise VeridropUnavailable(f"Veridrop could not be imported: {exc}") from exc
    return (
        ExecutionConfig,
        Mode,
        Protocol,
        _ReportSupport(
            report=DetectionReport,
            mask_api_key=mask_api_key,
            compute_total=compute_total,
            effective_verdict=effective_verdict,
            fatal_run_error=fatal_run_error,
            summary_text=summary_text,
        ),
        _protocol_module,
    )


class _ReportSupport(NamedTuple):
    """Upstream's report model and scorer, held together for one run."""

    report: Any
    mask_api_key: Any
    compute_total: Any
    effective_verdict: Any
    fatal_run_error: Any
    summary_text: Any


def _protocol_module(protocol: str) -> Any:
    """The staged module carrying one protocol's suite entry points."""

    try:
        return importlib.import_module(f"relay_detector.protocols.{protocol}")
    except Exception as exc:  # noqa: BLE001 - reported as an unavailable probe
        raise VeridropUnavailable(
            f"The staged Veridrop carries no {protocol} entry points: {exc}"
        ) from exc


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

    execution_config, mode, protocol_type, support, load_protocol = _import_upstream()
    try:
        selected_protocol = protocol_type(normalized_protocol)
    except ValueError as exc:
        raise ValueError(f"Veridrop cannot probe the {protocol!r} protocol") from exc
    # The suite's own module carries the entry points for this protocol, and
    # loading it through the staged package is what keeps a renamed one a
    # reported probe failure instead of an import error in the Core.
    protocol_module = load_protocol(normalized_protocol)
    config = execution_config.for_mode(mode.QUICK, max_concurrent=_MAX_CONCURRENT_REQUESTS)
    config.overall_timeout_s = _timeout_seconds(timeout_seconds)

    # The suite prints progress and its own terminal report; the Core has no
    # terminal, so that noise is captured and dropped instead of reaching the
    # application log.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            report = asyncio.run(
                _run_suite(
                    protocol=normalized_protocol,
                    protocol_module=protocol_module,
                    support=support,
                    base_url=base_url.strip(),
                    api_key=api_key,
                    model=model,
                    config=config,
                    selected_protocol=selected_protocol,
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
    return _normalize_report(report)


async def _run_suite(
    *,
    protocol: str,
    protocol_module: Any,
    support: _ReportSupport,
    base_url: str,
    api_key: str,
    model: str,
    config: Any,
    selected_protocol: Any,
) -> dict[str, Any]:
    """Run one quick suite and return its report as a plain mapping.

    The report is assembled exactly the way upstream's own web application
    assembles it - same runner, same detectors, same scorer - and serialized
    through upstream's report model, so the JSON shape is the one
    :func:`_normalize_report` already reads.
    """

    for name in ("build_detectors", "build_runner", "make_client"):
        if not hasattr(protocol_module, name):
            raise VeridropUnavailable(
                f"The staged Veridrop {protocol} module no longer publishes {name}"
            )

    async with protocol_module.make_client(
        base_url, api_key, timeout=config.request_timeout_s
    ) as client:
        runner = protocol_module.build_runner(
            client, protocol_module.build_detectors(config.mode), config
        )
        outcome = await runner.run(model)

    results = list(outcome.results)
    run_error = support.fatal_run_error(results)
    score = 0.0 if run_error else support.compute_total(results)
    verdict = support.effective_verdict(score, results)

    # Identity is populated only by the Anthropic identity detector; the other
    # protocols keep both fields empty, exactly as upstream reports them.
    identity: str | None = None
    brands: list[str] = []
    for result in results:
        if result.name != "identity" or not isinstance(result.details, dict):
            continue
        text = result.details.get("response_text")
        if isinstance(text, str) and text.strip():
            identity = text.strip()
        found = result.details.get("detected_non_anthropic_brands")
        if isinstance(found, list):
            brands = [item for item in found if isinstance(item, str)]
        break

    report = support.report(
        protocol=selected_protocol,
        base_url=base_url,
        api_key_masked=support.mask_api_key(api_key),
        target_model=model,
        mode=config.mode,
        timestamp=datetime.now(timezone.utc),
        total_score=score,
        verdict=verdict,
        results=results,
        performance=outcome.performance,
        summary=run_error or support.summary_text(score, verdict),
        run_error=run_error,
        self_reported_identity=identity,
        detected_non_anthropic_brands=brands,
    )
    payload = report.model_dump_json()
    if len(payload) > _MAX_OUTPUT_BYTES:
        raise RuntimeError("Veridrop returned an oversized report")
    decoded = json.loads(payload)
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
    "protocol_for",
    "run_quick",
    "source_root",
    "staged_root",
    "supported_models",
    "target",
]
