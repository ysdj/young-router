"""Python adapter for the bundled llm-fingerprint-detector precision probe.

llm-fingerprint-detector (https://github.com/ToseaAI/llm-fingerprint-detector,
MIT) answers the question this app cannot answer from token counts alone: *is
this endpoint serving the weights it claims, or a quantized or substituted
deployment of them?*  It does so from single-token output distributions over a
fixed battery of trivial questions, which is the one signal that survives an API
that hides logits, and its decision thresholds come from a published study
(arXiv:2607.10252): a same-model cross-provider median distance of ~0.227
against a different-model median of ~0.463, cut at 0.25 and 0.35.

The package is TypeScript, so it runs on the Node runtime this app already
bundles rather than being re-implemented here.  ``scripts/update_llm_fingerprint.py``
stages it on every artifact build; this adapter starts
``llm_fingerprint_worker.mjs``, hands it one job, and reads one result back.

Two facts shape every finding this adapter returns:

* **The library's verdict is statistical, not an accusation.**  A ``mismatch``
  means the endpoint's one-token behaviour is as far from the reference as
  different models usually are; a ``match`` is strong evidence, not proof.
* **A reference is required to compare, and it must be a fingerprint.**  The
  library ships sample references for a handful of models, but a vendor's own
  address is the reference worth trusting, which is what
  :func:`capture_reference` records.

A run that measured nothing reports that; it never renders an empty comparison
as a match.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from ..node_runtime import node_command as node_runtime_command

_DIR_ENV = "YOUNG_ROUTER_LFD_DIR"
_WORKER_ENV = "YOUNG_ROUTER_LFD_WORKER"
_NODE_ENV = "YOUNG_ROUTER_LFD_NODE"
_TIMEOUT_ENV = "YOUNG_ROUTER_LFD_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 20.0

#: The run this app makes by default: five cells from the library's
#: discriminativeness-ordered battery, twelve samples each.
#:
#: Sized by a wall-clock budget, not by the library's own preset.  Its default
#: (8 cells x 25 samples) is the right shape for an audit someone sits through,
#: and it takes minutes on a real endpoint.  Measured on api.deepseek.com with
#: the pooled dispatcher this adapter installs, one call costs about 0.8 s at
#: concurrency 8, so wall time is calls x 0.1 s; 5 x 12 is 60 calls and came in
#: at 6.1 s over three runs.  That leaves room for an endpoint half again as
#: slow before the budget is missed.
#:
#: The size is above the library's floors (MIN_COMPARABLE_CELLS = 4,
#: MIN_VALID_SAMPLES_PER_CELL = 10), so a verdict is possible, and it is large
#: enough for the library's split-half readout (5 samples per half), which is
#: what decides whether the verdict means anything on this endpoint.
DEFAULT_CELLS = 4
DEFAULT_SAMPLES_PER_CELL = 10
#: One connection per in-flight request.  Measured on api.deepseek.com over 40
#: calls: concurrency 8 took 5.5 s and concurrency 12 took 8.3 s on the same
#: afternoon, and on an earlier one 10 took 4.0 s while 12 took 3.4 s.  The
#: endpoint's own capacity moves more than the difference between these
#: settings, so eight is the choice: it was never the slower one, and pushing
#: higher is what turns a probe into a load test.
DEFAULT_CONCURRENCY = 8
#: The wall-clock budget this probe is held to.  A caller that has its own gets
#: it passed through; the default is what keeps a probe from being something a
#: user starts and walks away from.
#:
#: Twenty seconds, not ten, because a measurement is a run of forty calls and
#: the endpoint's own pace moves: api.deepseek.com served 40 calls in 5.5 s on
#: one afternoon and 12.0 s on another at the same concurrency.  A ten-second
#: stop measured the endpoint's mood as much as its precision, aborting runs
#: that would have produced a verdict a moment later.
DEFAULT_BUDGET_SECONDS = 20.0

#: The library's own verdict levels.  Anything else is a value this adapter does
#: not understand and must not translate into a finding.
VERDICTS = ("match", "uncertain", "mismatch", "insufficient")


class FingerprintUnavailable(RuntimeError):
    """The staged package or its Node runtime is missing."""


class ProbeOverBudget(RuntimeError):
    """The endpoint could not answer the probe set inside the budget.

    A separate type because it is a fact about the endpoint rather than an
    error in this app: an endpoint too slow to answer 48 one-token questions in
    ten seconds cannot be audited this way at all, and a caller reports that
    instead of a failed check.
    """


def staged_root() -> Path:
    """Directory holding the staged upstream package."""

    configured = os.environ.get(_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("llm-fingerprint-detector")


def worker_path() -> Path:
    configured = os.environ.get(_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("llm_fingerprint_worker.mjs")


def node_command() -> str:
    return node_runtime_command(
        env_name=_NODE_ENV,
        label="The precision probe",
        unavailable=FingerprintUnavailable,
    )


def manifest() -> dict[str, Any]:
    """Read the staging manifest, or an empty mapping when unstaged."""

    try:
        value = json.loads((staged_root() / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _built_entry() -> Path | None:
    """The staged package's compiled entry point, when the build produced one."""

    for candidate in (staged_root() / "dist" / "index.js", staged_root() / "src" / "index.ts"):
        if candidate.is_file():
            return candidate
    return None


def available() -> bool:
    """True when the staged package and its worker can run."""

    if _built_entry() is None or not worker_path().is_file():
        return False
    try:
        node_command()
    except FingerprintUnavailable:
        return False
    return True


def engine() -> dict[str, Any]:
    """Describe the staged program for the UI and for a probe finding."""

    info = manifest()
    return {
        "name": "llm-fingerprint-detector",
        "source": str(info.get("source", "https://github.com/ToseaAI/llm-fingerprint-detector")),
        "license": str(info.get("license", "MIT")),
        "revision": str(info.get("revision") or ""),
        "staged_at": str(info.get("staged_at") or ""),
        "version": str(info.get("version") or ""),
        "mode": "single-token distributions",
        "available": available(),
        "cells": DEFAULT_CELLS,
        "samples_per_cell": DEFAULT_SAMPLES_PER_CELL,
        "concurrency": DEFAULT_CONCURRENCY,
        "budget_seconds": DEFAULT_BUDGET_SECONDS,
        "thresholds": info.get("thresholds") if isinstance(info.get("thresholds"), Mapping) else {},
    }


def _timeout_seconds(value: float | None) -> float:
    if value is not None:
        return max(1.0, float(value))
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


def _run_job(job: Mapping[str, Any], *, timeout_seconds: float | None) -> dict[str, Any]:
    """Hand one job to the worker and read one result back."""

    if not available():
        raise FingerprintUnavailable(
            "llm-fingerprint-detector is not staged; run scripts/update_llm_fingerprint.py before building"
        )
    node = node_command()
    environment = dict(os.environ)
    environment[_DIR_ENV] = str(staged_root())
    environment.setdefault("NODE_NO_WARNINGS", "1")
    # The worker is killed at the budget rather than left to finish: a probe a
    # user starts must not be something they walk away from, and a run that
    # cannot finish in time is itself the finding about that endpoint.
    budget = _timeout_seconds(timeout_seconds)
    try:
        completed = subprocess.run(
            [node, str(worker_path())],
            input=json.dumps(dict(job), ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=budget,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeOverBudget(
            f"the endpoint did not finish {DEFAULT_CELLS} x {DEFAULT_SAMPLES_PER_CELL} "
            f"one-token probes within {budget:.0f}s"
        ) from error
    output = completed.stdout.decode("utf-8", errors="replace").strip()
    if not output:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "the precision probe produced no result"
            + (f": {detail[:400]}" if detail else "")
        )
    try:
        payload = json.loads(output)
    except ValueError as error:
        raise RuntimeError(f"the precision probe returned unreadable output: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("the precision probe returned an unexpected shape")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload


def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The library's result in the shape a caller renders.

    Everything measured stays; nothing is invented.  ``verdict`` is the
    library's own level, and a comparison that could not be made keeps its own
    name instead of borrowing one that sounds like a measurement.
    """

    comparison = payload.get("comparison")
    comparison = comparison if isinstance(comparison, Mapping) else None
    verdict = str(comparison.get("verdict") or "") if comparison else ""
    if verdict and verdict not in VERDICTS:
        verdict = "insufficient"
    thresholds = comparison.get("thresholds") if comparison else None
    baselines = comparison.get("baselines") if comparison else None
    mean_jsd = comparison.get("meanJsd") if comparison else None
    split_half = payload.get("split_half_jsd")
    # The library reports the distance between the two halves of one run, but
    # only once each half holds enough samples (upstream's MIN_SPLIT_HALF_SAMPLES
    # is 5 per half).  A run too short to produce it has NOT shown itself
    # consistent: it produced no readout at all, and calling that "consistent"
    # would be the exact mistake this field exists to prevent.
    if isinstance(split_half, (int, float)):
        self_consistent: bool | None = float(split_half) <= float(
            (thresholds or {}).get("match") or 0.25
        )
    else:
        self_consistent = None
    elapsed_ms = payload.get("elapsed_ms")
    budget_seconds = DEFAULT_BUDGET_SECONDS
    over_budget = (
        isinstance(elapsed_ms, (int, float)) and float(elapsed_ms) / 1000.0 > budget_seconds
    )
    return {
        "status": "measured" if comparison is not None else "captured",
        # What the run cost and whether that fits the budget this probe is held
        # to.  A slow endpoint is a finding about the endpoint, and naming it
        # keeps a user from reading a slow probe as a hung one.
        "elapsed_seconds": round(float(elapsed_ms) / 1000.0, 1) if isinstance(elapsed_ms, (int, float)) else None,
        "budget_seconds": budget_seconds,
        "over_budget": over_budget,
        "transport": str(payload.get("transport") or ""),
        "verdict": verdict,
        "mean_jsd": mean_jsd,
        "thresholds": dict(thresholds) if isinstance(thresholds, Mapping) else {},
        "baselines": dict(baselines) if isinstance(baselines, Mapping) else {},
        "comparable_cells": comparison.get("comparableCellCount") if comparison else None,
        "protocol_mismatch": bool(comparison.get("protocolMismatch")) if comparison else False,
        "split_half_jsd": split_half,
        # None means the run was too short to measure it, which is not the
        # same as "it disagreed with itself" and not the same as "it did not".
        "self_consistent": self_consistent,
        "post_reasoning": bool(payload.get("post_reasoning")),
        "cells": payload.get("cells"),
        "samples_per_cell": payload.get("samples_per_cell"),
        "warnings": [str(item) for item in (payload.get("warnings") or [])][:8],
        "fingerprint": payload.get("fingerprint"),
        "engine": engine(),
    }


def capture_reference(
    *,
    api_base: str,
    api_key: str,
    model: str,
    cells: int = DEFAULT_CELLS,
    samples_per_cell: int = DEFAULT_SAMPLES_PER_CELL,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Fingerprint one endpoint so it can serve as a reference later.

    This is how a caller records what the vendor's own address answers: point it
    at ``api.deepseek.com`` once, keep the returned fingerprint, and every route
    claiming that model can be compared against it.
    """

    payload = _run_job(
        {
            "mode": "fingerprint",
            "base_url": api_base,
            "api_key": api_key,
            "model": model,
            "cells": cells,
            "samples_per_cell": samples_per_cell,
            "concurrency": concurrency,
        },
        timeout_seconds=timeout_seconds,
    )
    return _normalize(payload)


def verify(
    *,
    api_base: str,
    api_key: str,
    model: str,
    reference: Mapping[str, Any],
    cells: int | None = None,
    samples_per_cell: int = DEFAULT_SAMPLES_PER_CELL,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Fingerprint one route and compare it against a recorded reference.

    ``reference`` is the ``fingerprint`` object :func:`capture_reference`
    returned.  The comparison, its thresholds and its verdict are the library's;
    this adapter only reports them together with the self-consistency readout
    that decides whether they mean anything on this endpoint.
    """

    job: dict[str, Any] = {
        "mode": "verify",
        "base_url": api_base,
        "api_key": api_key,
        "model": model,
        "reference": reference,
        "samples_per_cell": samples_per_cell,
        "concurrency": concurrency,
    }
    if isinstance(cells, int) and cells > 0:
        job["cells"] = cells
    payload = _run_job(job, timeout_seconds=timeout_seconds)
    return _normalize(payload)


__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_CELLS",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_SAMPLES_PER_CELL",
    "DEFAULT_TIMEOUT_SECONDS",
    "FingerprintUnavailable",
    "ProbeOverBudget",
    "VERDICTS",
    "available",
    "capture_reference",
    "engine",
    "manifest",
    "node_command",
    "staged_root",
    "verify",
    "worker_path",
]
