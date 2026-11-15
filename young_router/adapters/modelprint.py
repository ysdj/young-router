"""In-process port of the modelprint fingerprint probe set.

modelprint (https://github.com/unclecode/modelprint, MIT) asks one question:
*who is really behind that API?*  It answers with infrastructure and shape
probes rather than capability benchmarks - tokenizer counts on pinned texts, a
serving-template offset, the lab's own validation prose, the error-code family,
the finish vocabulary, and the top-logprob geometry that separates a quantized
deployment from full weights.  Those values are stable for a serving stack and
change when the stack does.

The upstream repository -- the daily tracker that records every value change
per model and host -- is staged into this package by
``scripts/update_modelprint.py`` on every artifact build::

    baselines/<model>__<host>.json   the latest tracked version of each entry
    manifest.json                    staging record: upstream revision, the
                                     pinned probe texts with their digests, and
                                     which baselines are official

Nothing here re-implements the tracker.  The probe *texts* come from the
manifest so this module can never drift from the baselines they produced: a
build whose staged texts do not hash to the recorded digests refuses to run
rather than comparing two different prompts.

Two things modelprint states plainly and this port keeps:

* It reads an endpoint, it does not prove one.  A route that serves the model it
  advertises produces the same values as the baseline; the finding is evidence,
  never a certificate.
* Some probes cannot answer from this Core.  ``net-headerdna`` needs response
  headers a browser may read, ``net-region`` needs a router's own metadata
  block, ``net-genrecord`` needs OpenRouter's generation ledger.  They are not
  faked here; they are absent from the table and their absence is reported.

Entry points tolerate an unstaged checkout: :func:`available` reports the
program state instead of raising, and :func:`run` raises
:class:`ModelPrintUnavailable` so a caller can report a skipped check.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from pathlib import Path
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Mapping, Sequence

_DIR_ENV = "YOUNG_ROUTER_MODELPRINT_DIR"
_CAPTURED_ENV = "YOUNG_ROUTER_MODELPRINT_CAPTURED_DIR"
_TIMEOUT_ENV = "YOUNG_ROUTER_MODELPRINT_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 300.0
PROBE_MODE = "quick"
REFERENCE_ENV_SUFFIX = "REFERENCE"

#: The protocols this port can speak.  modelprint's gemini row is left out on
#: purpose: the upstream registry carries no gemini probe set, and a table that
#: silently answered nothing for a gemini route would read as "nothing found".
PROTOCOLS = ("anthropic", "openai")

_MANIFEST = "manifest.json"

#: Values a probe reports when it measured nothing.  They are never compared:
#: "model busy" describes the provider's load, "unstable" says the router spread
#: the calls across hosts, and "logprobs-unsupported" says this endpoint cannot
#: answer that question at all.  Treated as a missing measurement, never as a
#: difference.
_UNMEASURED_PREFIXES = (
    "probe-failed",
    "model busy",
    "model overloaded",
    "model timed out",
    "model too slow",
    "network error",
    "unstable",
    "usage not reported",
    "headers-hidden",
    "harness-lacks",
    "router-spread-unstable",
    "no-upstream-reject-path",
    "baseline-not-local",
    "upstream-reject-unstable",
)
_UNMEASURED_VALUES = frozenset(
    {
        "logprobs-unsupported",
        "no-router-metadata",
        "record-not-found",
        "no-generation-id",
        "no-generation-ledger",
        "degenerate-distribution",
        "missing-probe-text",
    }
)

#: Two lanes are the same stack only when this share of comparable probe rows
#: match.  Calibrated against the upstream tracker's own snapshots: the same
#: model on the same host matches every row that is measurable, while two hosts
#: of the same model differ on the four tokenizer rows and the template offset.
MATCH_SHARE = 0.75
#: A row count below this says nothing either way.
MIN_COMPARABLE_ROWS = 4
#: Attribution needs the tokenizer rows: they are the hardest to fake, and one
#: matching text out of four is what two unrelated labs produce by accident.
MIN_TOKENIZER_ROWS = 2
#: Rows that decide a comparison *within* one vendor.  Every model of a family
#: shares the family's tokenizer, so all four tokenizer rows agree between a
#: flagship and its smaller sibling - measured on api.deepseek.com, flash and
#: v4-pro answer 49/53/62/83 identically.  What separates them is the serving
#: template's own overhead, so a same-vendor match that does not agree on these
#: rows names nothing: reporting the sibling would be a confident wrong answer.
SAME_VENDOR_DECISIVE_ROWS = ("template-offset",)

_TOKENIZER_PROBES = ("tok-english", "tok-chinese", "tok-code", "tok-emoji")
_TEMPLATE_PROBE = "template-offset"
_GEOMETRY_PROBE = "lp-geometry"

#: The probes this port runs, in table order.  ``net-*`` rows are absent by
#: design (see the module docstring) and ``cap-contextceiling`` is opt-in: the
#: upstream tracker runs it weekly because the ladder alone sends ~600k tokens.
_DEFAULT_PROBES = (
    "tok-english",
    "tok-chinese",
    "tok-code",
    "tok-emoji",
    _TEMPLATE_PROBE,
    "err-temperature",
    "err-maxtokens",
    "err-code-family",
    "finish-vocab",
    _GEOMETRY_PROBE,
)
#: Probes that cost real time or tokens on every run.  ``cap-contextceiling``
#: sends ~600k tokens, so it is opt-in.
_OPT_IN_PROBES = ("cap-contextceiling",)


class ModelPrintUnavailable(RuntimeError):
    """The staged modelprint tables are missing or cannot be used."""


class ProbeDeadline(RuntimeError):
    """The probe budget ran out before the next request could be issued."""


# --------------------------------------------------------------------------
# staged tables
# --------------------------------------------------------------------------


def staged_root() -> Path:
    """Directory holding the staged tables."""

    configured = os.environ.get(_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("modelprint")


def captured_root() -> Path:
    """Directory holding fingerprints captured from a vendor's own endpoint.

    These live in the repository rather than in the staged tree, because the
    staging step rewrites that tree from upstream on every build and upstream
    carries no entry for every vendor: the tracker pins each model to one
    provider and most of those providers are resellers.  A fingerprint taken
    from the vendor's own address is the one a route should be judged against,
    and it survives a rebuild because it is ours.
    """

    configured = os.environ.get(_CAPTURED_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("modelprint-official")


def manifest() -> dict[str, Any]:
    """Read the staging manifest, or an empty mapping when unstaged."""

    path = staged_root() / _MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def probe_texts() -> dict[str, str]:
    """The pinned probe texts, from the staged manifest.

    The texts live in the manifest rather than in this module so that a build
    whose upstream changed a text cannot silently compare new counts against
    baselines recorded from the old one: staging records what it shipped and
    this reads exactly that.
    """

    value = manifest().get("probe_texts")
    if not isinstance(value, Mapping):
        return {}
    texts: dict[str, str] = {}
    for name, text in value.items():
        if isinstance(name, str) and isinstance(text, str) and text:
            texts[name] = text
    return texts


def probe_text_digests() -> dict[str, str]:
    value = manifest().get("probe_text_digests")
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def texts_verified() -> bool:
    """Whether every staged text still hashes to the recorded digest."""

    texts = probe_texts()
    digests = probe_text_digests()
    if not texts or not digests:
        return False
    for name, text in texts.items():
        recorded = digests.get(name)
        if not recorded:
            return False
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != recorded:
            return False
    return True


def baselines() -> dict[str, dict[str, Any]]:
    """Every baseline this build can compare against, staged first.

    The staged tracker's entries come first and a captured one never replaces
    them: both describe a real endpoint, and which one applies is decided by
    :func:`_comparable_baselines` (an official entry wins).  A captured entry
    whose stem already exists keeps the staged document and adds itself under a
    ``-captured`` suffix, so nothing is silently dropped.
    """

    entries: dict[str, dict[str, Any]] = {}
    for root in (staged_root() / "baselines", captured_root()):
        try:
            paths = sorted(root.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict) or not isinstance(document.get("model"), str):
                continue
            stem = path.stem
            if stem in entries:
                stem = f"{stem}-captured"
            entries[stem] = document
    return entries


def captured_baselines() -> dict[str, dict[str, Any]]:
    """Only the entries captured from a vendor's own endpoint."""

    captured = captured_root()
    try:
        names = {path.stem for path in captured.glob("*.json")}
    except OSError:
        return {}
    return {
        stem: entry
        for stem, entry in baselines().items()
        if stem in names or stem.removesuffix("-captured") in names
    }


def latest_values(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The recorded probe values of one baseline entry.

    Staging flattens the tracker's history to the newest version, so a staged
    entry carries ``values`` directly; an upstream document handed in unchanged
    still carries the ``versions`` list, and the newest of those answers.
    """

    values = entry.get("values")
    if isinstance(values, Mapping):
        return {str(k): v for k, v in values.items()}
    versions = entry.get("versions")
    if not isinstance(versions, Sequence) or isinstance(versions, (str, bytes)):
        return {}
    newest: Mapping[str, Any] | None = None
    for version in versions:
        if not isinstance(version, Mapping):
            continue
        recorded = version.get("values")
        if not isinstance(recorded, Mapping):
            continue
        if newest is None or _version_order(version) >= _version_order(newest):
            newest = version
    recorded = newest.get("values") if isinstance(newest, Mapping) else None
    return {str(k): v for k, v in recorded.items()} if isinstance(recorded, Mapping) else {}


def _version_order(version: Mapping[str, Any]) -> tuple[int, str]:
    number = version.get("v")
    return (
        number if isinstance(number, int) and not isinstance(number, bool) else 0,
        str(version.get("first_seen") or ""),
    )


def _model_family(model: str) -> str:
    """``deepseek/deepseek-v4-flash`` → ``deepseek``; the vendor a name states."""

    value = model.strip().lower()
    if "/" in value:
        return value.split("/", 1)[0]
    for separator in ("-", "_", "."):
        if separator in value:
            return value.split(separator, 1)[0]
    return value


def normalize_model_name(value: object) -> str:
    """A model name reduced to what two spellings of it share."""

    if not isinstance(value, str):
        return ""
    name = value.strip().lower()
    if not name:
        return ""
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("[") and "]" in name:
        name = name.split("]", 1)[1]
    return re.sub(r"[_\s]+", "-", name).strip()


def same_model(left: object, right: object) -> bool:
    """Whether two names state the same model, tolerating a vendor prefix.

    Dot, hyphen and underscore are interchangeable in model ids
    (``deepseek-v4.1-flash`` / ``deepseek-v4.1.f`` styles), and a dated pin
    (``…-0731``) is a snapshot of the same name rather than another model.
    """

    a = normalize_model_name(left)
    b = normalize_model_name(right)
    if not a or not b:
        return False
    a = a.replace(".", "-")
    b = b.replace(".", "-")
    if a == b:
        return True
    return a.startswith(b + "-") or b.startswith(a + "-")


def supported_models() -> dict[str, tuple[str, ...]]:
    """Which model names the staged baselines carry, per protocol."""

    value = manifest().get("supported_models")
    if not isinstance(value, Mapping):
        return {}
    models: dict[str, tuple[str, ...]] = {}
    for protocol, names in value.items():
        if not isinstance(protocol, str):
            continue
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            continue
        resolved = tuple(
            name for name in (normalize_model_name(item) for item in names) if name
        )
        if resolved:
            models[protocol] = resolved
    return models


def target(*names: object) -> dict[str, str] | None:
    """The staged baseline one model name corresponds to, when there is one."""

    models = supported_models()
    if not models:
        return None
    for name in names:
        normalized = normalize_model_name(name)
        if not normalized:
            continue
        for protocol, supported in models.items():
            for candidate in supported:
                if same_model(normalized, candidate):
                    return {"model": candidate, "protocol": protocol}
    return None


def official_baseline_names() -> frozenset[str]:
    """Baselines recorded from the vendor's own endpoint.

    Two sources count, and they answer the same question: the tracker's own
    list of entries it captured from a vendor's address, and the fingerprints
    this repository captured itself (``modelprint-official/``).  The tracker
    pins every model to one provider and most of those providers are resellers:
    a SiliconFlow DeepSeek is a DeepSeek, but its serving template is not
    ``api.deepseek.com``'s.  A route is compared against the official entry
    when there is one, and against every entry otherwise - the vendor is still
    readable from the tokens even when the template is not.
    """

    names: set[str] = set()
    value = manifest().get("official_baselines")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        names.update(str(item) for item in value if isinstance(item, str))
    names.update(captured_baselines())
    return frozenset(names)


def protocol_for(*names: object) -> str:
    """Which wire a model name is served on, from the name alone.

    modelprint's probe set is one OpenAI-shaped chat completion per row, so
    everything that is not a Claude name is probed over the chat wire exactly
    as the upstream page probes it through a router.  A Claude name is probed
    over the Anthropic wire when the route serves one, because that is the wire
    its validation prose and message ids come from.
    """

    for name in names:
        normalized = normalize_model_name(name)
        if not normalized:
            continue
        if normalized.startswith("claude") or "/claude" in normalized:
            return "anthropic"
        return "openai"
    return ""


def available() -> bool:
    """True when this build carries usable staged tables."""

    if not (staged_root() / _MANIFEST).is_file():
        return False
    return texts_verified()


def engine() -> dict[str, Any]:
    """Describe the staged tables for the UI and for a probe finding."""

    info = manifest()
    revision = info.get("revision")
    targets = sorted(
        f"{protocol}:{name}"
        for protocol, names in sorted(supported_models().items())
        for name in names
    )
    return {
        "name": "modelprint",
        "source": str(info.get("source", "https://github.com/unclecode/modelprint")),
        "license": str(info.get("license", "MIT")),
        "revision": str(revision) if isinstance(revision, str) and revision else "",
        "staged_at": str(info.get("staged_at", "")),
        "mode": PROBE_MODE,
        "targets": targets,
        "available": available(),
        "official": sorted(official_baseline_names()),
        "probes": list(_DEFAULT_PROBES),
    }


# --------------------------------------------------------------------------
# probe plan (what the pane promises before anything runs)
# --------------------------------------------------------------------------


def probe_names(
    *, include_context_ladder: bool = False
) -> tuple[str, ...]:
    """The rows a run measures, in table order.

    The default set is what a press costs without asking: eleven small calls
    plus the two that need logprobs.  The expensive rows are opt-in, and the
    caller states which of them it wants rather than the name of a switch.
    """

    names = list(_DEFAULT_PROBES)
    if include_context_ladder:
        names.append("cap-contextceiling")
    return tuple(names)


def plan(
    *,
    model: str,
    api_base: str | None,
    reference_configured: bool = False,
    include_context_ladder: bool = False,
) -> dict[str, Any]:
    """What a run against this route would measure, and why it might not.

    ``includes_degradation`` is false when there is nothing to compare
    against: no staged baseline for this model *and* no reference endpoint the
    user supplied.  A row that says "cannot attribute" before it runs is more
    useful than a button that promises a verdict and then reports a skip.
    """

    normalized = normalize_model_name(model)
    protocol = protocol_for(model)
    names = probe_names(
        include_context_ladder=include_context_ladder,
    )
    if not normalized or protocol not in PROTOCOLS:
        return {
            "includes_degradation": False,
            "target": None,
            "protocol": protocol,
            "surface": "",
            "probes": [],
            "reason": "protocol" if not protocol else "unsupported-protocol",
        }
    surface = {"anthropic": "anthropic", "openai": "openai/chat"}.get(protocol, "")
    comparable = _comparable_baselines(normalized)
    if not comparable and not reference_configured:
        return {
            "includes_degradation": False,
            "target": normalized,
            "protocol": protocol,
            "surface": surface,
            "probes": [],
            "reason": "no-baseline",
        }
    return {
        "includes_degradation": bool(surface),
        "target": normalized,
        "protocol": protocol,
        "surface": surface,
        "probes": list(names),
        "reason": "",
    }


def _comparable_baselines(model: str) -> list[tuple[str, dict[str, Any]]]:
    """Staged entries that could describe this model, best candidates first.

    An official entry for the same name wins outright; entries of the same
    vendor come next (the tokens are the vendor's even behind a reseller), and
    nothing else is offered - comparing an unrelated model's row would produce
    a confident "different" that means nothing.
    """

    wanted = normalize_model_name(model)
    vendor = _model_family(wanted)
    official = official_baseline_names()
    exact: list[tuple[str, dict[str, Any]]] = []
    same_vendor: list[tuple[str, dict[str, Any]]] = []
    for stem, entry in baselines().items():
        entry_model = str(entry.get("model") or "")
        entry_vendor = _model_family(entry_model)
        if same_model(wanted, entry_model):
            exact.append((stem, entry))
        elif entry_vendor == vendor:
            same_vendor.append((stem, entry))
    exact.sort(key=lambda item: (item[0] not in official, item[0]))
    same_vendor.sort(key=lambda item: (item[0] not in official, item[0]))
    return exact + same_vendor


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


class _Client:
    """One endpoint, one credential, one wire."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        protocol: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.protocol = protocol
        self.model = model
        self.deadline = time.monotonic() + max(1.0, timeout_seconds)
        self.request_timeout = max(5.0, min(60.0, timeout_seconds / 6.0))
        self.calls = 0

    # -- one request ------------------------------------------------------
    def chat(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 1.0:
            raise ProbeDeadline("the probe budget ran out")
        self.calls += 1
        body = dict(payload)
        body["model"] = self.model
        if self.protocol == "anthropic":
            return self._post(
                "/v1/messages",
                body,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                anthropic=True,
            )
        return self._post(
            "/chat/completions",
            body,
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            anthropic=False,
        )

    def _post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        anthropic: bool,
    ) -> dict[str, Any]:
        url = self.api_base + path
        request_body = dict(body)
        if anthropic:
            # The Anthropic wire calls the same thing output budget, and an
            # Anthropic-compatible relay rejects an OpenAI-shaped payload
            # outright rather than reading the part it knows.
            request_body.setdefault("max_tokens", 16)
        data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
        remaining = max(1.0, min(self.request_timeout, self.deadline - time.monotonic()))
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                raw = response.read(_MAX_RESPONSE_BYTES)
                status = int(getattr(response, "status", 200) or 200)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(_MAX_RESPONSE_BYTES)
            except OSError:
                raw = b""
            return _failure(status=int(error.code or 0), body=raw)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError) as error:
            return {"ok": False, "status": 0, "error": f"network error: {error}"}
        text = raw.decode("utf-8", errors="replace")
        try:
            document = json.loads(text)
        except ValueError:
            return _failure(status=status, body=raw)
        if not isinstance(document, dict):
            return _failure(status=status, body=raw)
        return _success(document, status=status, headers=response_headers, anthropic=anthropic)


_MAX_RESPONSE_BYTES = 1024 * 1024


def _success(
    document: Mapping[str, Any],
    *,
    status: int,
    headers: Mapping[str, str],
    anthropic: bool,
) -> dict[str, Any]:
    if anthropic:
        content = document.get("content")
        text = ""
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping)
            )
        usage = document.get("usage") if isinstance(document.get("usage"), Mapping) else {}
        return {
            "ok": True,
            "status": status,
            "usage": {
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
            },
            "finish": document.get("stop_reason"),
            "text": text,
            "id": document.get("id"),
            "reported_model": document.get("model"),
            "headers": dict(headers),
            "logprobs": None,
        }
    choices = document.get("choices")
    choice = choices[0] if isinstance(choices, Sequence) and choices and isinstance(choices[0], Mapping) else {}
    usage = document.get("usage") if isinstance(document.get("usage"), Mapping) else {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    logprobs = _openai_logprobs(choice)
    return {
        "ok": True,
        "status": status,
        # Routers normalize ``finish_reason``; the lab's own word survives in
        # ``native_finish_reason``.  Without it every routed model reads alike.
        "finish": choice.get("native_finish_reason") or choice.get("finish_reason"),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
        "text": str(message.get("content") or ""),
        "id": document.get("id"),
        "reported_model": document.get("model"),
        "system_fingerprint": document.get("system_fingerprint"),
        "headers": dict(headers),
        "logprobs": logprobs,
    }


def _openai_logprobs(choice: Mapping[str, Any]) -> Any:
    """The top-k distribution of the first generated token.

    A chat completion reports it under the channel the token was emitted on.  A
    reasoning model's first token is often its thinking rather than its answer,
    and that distribution lands under ``reasoning_content``; reading only
    ``content`` reported such an endpoint as unable to answer the geometry probe
    at all.  Both channels are the model's own distribution, so either answers.
    """

    block = choice.get("logprobs")
    if not isinstance(block, Mapping):
        return block
    for channel in ("content", "reasoning_content", "refusal"):
        entries = block.get(channel)
        if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)) and entries:
            first = entries[0]
            if isinstance(first, Mapping) and first.get("top_logprobs"):
                return first.get("top_logprobs")
    return block


def _failure(*, status: int, body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    return {"ok": False, "status": status, "error": text[:4000]}


# --------------------------------------------------------------------------
# probes (ported one for one from probes/*.js)
# --------------------------------------------------------------------------


def _describe_failure(result: Mapping[str, Any]) -> str:
    """Turn a failed call into a sentence a reader can act on."""

    status = result.get("status")
    body = str(result.get("error") or "")
    if status == 429:
        return "model busy (rate limited)"
    if status in (502, 503, 504):
        return f"model overloaded ({status})"
    if status == 408:
        return "model timed out"
    if status in (0, None):
        if re.search(r"timeout|abort", body, re.I):
            return "model too slow (no answer in time)"
        return "network error (no answer)"
    if status in (401, 403):
        return f"key rejected ({status})"
    if re.search(r"overload|capacity|busy|try again", body, re.I):
        return "model overloaded"
    return f"probe-failed: {status}"


def _usage_missing(*results: Mapping[str, Any]) -> bool:
    for result in results:
        usage = result.get("usage")
        count = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            return True
    return False


_USAGE_NOT_REPORTED = "usage not reported"


def _tokenizer_probe(client: _Client, text: str) -> dict[str, Any]:
    """One pinned text, twice, minus a one-character baseline.

    The text is sent twice: a router spreading calls across hosts answers with
    two different counts, and then no fingerprint exists.  The baseline call
    cancels whatever hidden template the host wraps around the conversation,
    which is what lets two hosts running the same tokenizer match at all.
    """

    first = client.chat({"messages": [{"role": "user", "content": text}], "max_tokens": 1})
    second = client.chat({"messages": [{"role": "user", "content": text}], "max_tokens": 1})
    base = client.chat({"messages": [{"role": "user", "content": "a"}], "max_tokens": 1})
    for result in (first, second, base):
        if not result.get("ok"):
            return _row(_describe_failure(result))
    if _usage_missing(first, second, base):
        return _row(_USAGE_NOT_REPORTED)
    if first["usage"]["prompt_tokens"] != second["usage"]["prompt_tokens"]:
        return _row(
            "unstable (multi-host routing)",
            {"first": first["usage"]["prompt_tokens"], "second": second["usage"]["prompt_tokens"]},
        )
    normalized = int(first["usage"]["prompt_tokens"]) - int(base["usage"]["prompt_tokens"])
    return _row(
        str(normalized),
        {
            "raw_count": first["usage"]["prompt_tokens"],
            "baseline": base["usage"]["prompt_tokens"],
        },
    )


def _template_offset_probe(client: _Client) -> dict[str, Any]:
    """The serving template's own token overhead.

    Two prompts with a known difference in visible words: the part of
    ``prompt_tokens`` both share beyond the text is the template the host adds.
    """

    first = client.chat({"messages": [{"role": "user", "content": "a"}], "max_tokens": 1})
    second = client.chat({"messages": [{"role": "user", "content": "a"}], "max_tokens": 1})
    longer = client.chat(
        {"messages": [{"role": "user", "content": "a b c d e f g h"}], "max_tokens": 1}
    )
    for result in (first, second, longer):
        if not result.get("ok"):
            return _row(_describe_failure(result))
    if _usage_missing(first, second, longer):
        return _row(_USAGE_NOT_REPORTED)
    if first["usage"]["prompt_tokens"] != second["usage"]["prompt_tokens"]:
        return _row("unstable (multi-host routing)")
    overhead = int(first["usage"]["prompt_tokens"])
    per_word = int(longer["usage"]["prompt_tokens"]) - overhead
    return _row(f"+{overhead} (delta {per_word})", {"one_token_prompt": overhead})


_VOLATILE_PATTERNS = (
    (re.compile(r'"user_id"\s*:\s*"[^"]*"'), ""),
    (re.compile(r"user_[A-Za-z0-9]{10,}"), "…"),
    (re.compile(r'"(?:id|request_id|requestId)"\s*:\s*"[^"]*"'), '"id":"…"'),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "…"),
    (re.compile(r"\b\d{13,}\b"), "…"),
)


def scrub(input_value: object) -> str:
    """A lab's validation prose with everything per-call removed.

    A router wraps the lab's own error inside its envelope; the lab's wording is
    what fingerprints, so ``error.metadata.raw`` is unwrapped first.  Volatile
    ids are then scrubbed so two identical refusals compare equal.
    """

    text = str(input_value or "")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        error = parsed.get("error") if isinstance(parsed.get("error"), Mapping) else None
        metadata = error.get("metadata") if isinstance(error, Mapping) else None
        raw = metadata.get("raw") if isinstance(metadata, Mapping) else None
        if isinstance(raw, str) and raw:
            text = raw
        elif isinstance(error, Mapping) and error.get("message"):
            text = str(error["message"])
            if error.get("code") is not None:
                text = f"{text} · code {error['code']}"
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:300]


def _upstream_error_body(result: Mapping[str, Any]) -> str | None:
    """The lab's own error envelope, or ``None`` when a router answered."""

    try:
        parsed = json.loads(str(result.get("error") or ""))
    except ValueError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    error = parsed.get("error") if isinstance(parsed.get("error"), Mapping) else None
    if not isinstance(error, Mapping):
        return None
    metadata = error.get("metadata") if isinstance(error.get("metadata"), Mapping) else None
    raw = metadata.get("raw") if isinstance(metadata, Mapping) else None
    if isinstance(raw, str) and raw:
        return raw
    if metadata is None:
        return json.dumps(error, ensure_ascii=False)
    return None


def _temperature_probe(client: _Client) -> dict[str, Any]:
    """``temperature: 2.0`` - gateways forward it, a lab with a lower ceiling refuses."""

    result = client.chat(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, "temperature": 2.0}
    )
    if result.get("ok"):
        return _row("accepts 2.0 (ceiling >= 2)")
    return _row(scrub(result.get("error")), {"error": str(result.get("error"))[:1200]})


def _maxtokens_probe(client: _Client) -> dict[str, Any]:
    """``max_tokens: 10^9`` - the refusal names the lab's real output limit."""

    result = client.chat(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1_000_000_000}
    )
    if result.get("ok"):
        return _row("accepted 10^9 (silently capped)")
    upstream = _upstream_error_body(result)
    if upstream is None:
        match = re.search(r"maximum context length is (\d+)", str(result.get("error") or ""))
        if match:
            return _row(f"router-reported limit: {match.group(1)} tokens")
        return _row("router-validated (no upstream signal)")
    return _row(scrub(upstream), {"error": str(result.get("error"))[:1200]})


def _code_family_probe(client: _Client) -> dict[str, Any]:
    """The shape of an error response: numeric code, string code, typed object."""

    result = client.chat(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, "temperature": 2.0}
    )
    if result.get("ok"):
        return _row("no upstream error (accepts 2.0)")
    body = _upstream_error_body(result)
    if body is None:
        return _row("router-validated (no upstream signal)")
    try:
        parsed = json.loads(body)
    except ValueError:
        text = body.strip()
        if re.fullmatch(r"[A-Z ]{3,20}", text):
            return _row("opaque (host hides errors)")
        return _row("opaque-text")
    if not isinstance(parsed, Mapping):
        return _row("opaque-text")
    error = parsed.get("error") if isinstance(parsed.get("error"), Mapping) else parsed
    parts: list[str] = []
    code = error.get("code")
    if isinstance(code, (int, float)) or (code is not None and str(code).isdigit()):
        parts.append("numeric-code")
    elif code:
        parts.append("string-code")
    if error.get("type"):
        parts.append(f"type:{error['type']}")
    if error.get("param") is not None:
        parts.append("param-field")
    return _row(" · ".join(parts) or "message-only")


def _finish_vocabulary_probe(client: _Client) -> dict[str, Any]:
    """Which ``finish_reason`` words a normal stop and a forced cut produce."""

    stop = client.chat({"messages": [{"role": "user", "content": "Say only: ok"}], "max_tokens": 400})
    cut = client.chat(
        {"messages": [{"role": "user", "content": "Count from 1 to 200."}], "max_tokens": 6}
    )
    if not stop.get("ok") and not cut.get("ok"):
        return _row(_describe_failure(stop))
    words = [str(item) for item in (stop.get("finish"), cut.get("finish")) if item]
    return _row(" · ".join(words))


def _logprob_geometry_probe(client: _Client) -> dict[str, Any]:
    """The normalized third top-logprob gap on pinned continuations.

    Extreme-value theory predicts δ≈0.32 for a Gumbel-class distribution; the
    residual spread is a signature of the weights and the precision they are
    served at, which is what separates a quantized clone from full weights.
    """

    deltas: list[float] = []
    minimum_k = None
    for prompt in _GEOMETRY_PROMPTS:
        result = client.chat(
            {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1,
                "temperature": 0,
                "logprobs": True,
                "top_logprobs": 10,
            }
        )
        if not result.get("ok"):
            message = str(result.get("error") or "")
            if re.search(r"logprob", message, re.I):
                return _row("logprobs-unsupported", {"error": message[:300]})
            return _row(f"probe-failed: {result.get('status')}")
        tops = result.get("logprobs")
        if not isinstance(tops, Sequence) or len(tops) < 5:
            return _row("logprobs-unsupported", {"got": "short" if tops else "null"})
        values: list[float] = []
        for item in tops:
            value = item.get("logprob") if isinstance(item, Mapping) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        if len(values) < 5:
            return _row("logprobs-unsupported", {"got": "unparsable"})
        minimum_k = len(values) if minimum_k is None else min(minimum_k, len(values))
        gaps = [values[i] - values[i + 1] for i in range(len(values) - 1)]
        if len(gaps) < 4:
            continue
        denominator = gaps[1] + gaps[2] + gaps[3]
        if denominator > 1e-9:
            deltas.append(gaps[2] / denominator)
    if not deltas:
        return _row("degenerate-distribution")
    average = sum(deltas) / len(deltas)
    spread = max(deltas) - min(deltas)
    return _row(
        f"δ={average:.2f} · span={spread:.2f} · k={minimum_k if minimum_k is not None else '?'}",
        {"deltas": deltas},
    )


_GEOMETRY_PROMPTS = (
    "The capital city of France is",
    "Water boils at one hundred degrees",
    "One two three four five",
)

_CONTEXT_LADDER = (8_000, 32_000, 64_000, 128_000, 200_000, 256_000)


def _top_logprobs(client: _Client, prompt: str) -> list[float] | None:
    """The sorted top-k distribution of one pinned call, or None.

    A distribution whose tail is floored is not a distribution: an endpoint that
    turns thinking off answers with one real logprob and a floor for the rest,
    and averaging a floor would invent a number.
    """

    result = client.chat(
        {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": 10,
        }
    )
    if not result.get("ok"):
        return None
    tops = result.get("logprobs")
    if not isinstance(tops, Sequence) or len(tops) < 3:
        return None
    values: list[float] = []
    for item in tops:
        value = item.get("logprob") if isinstance(item, Mapping) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    if len(values) < 3:
        return None
    values.sort(reverse=True)
    if values[1] <= _FLOORED_LOGPROB:
        return None
    return values


#: Any logprob at or below this is an endpoint's "not the chosen token" floor
#: rather than a probability (DeepSeek answers -9999.0 for every alternative
#: when thinking is off).
_FLOORED_LOGPROB = -9998.0


def _context_ceiling_probe(client: _Client) -> dict[str, Any]:
    """The largest context bucket the endpoint accepts (expensive, opt-in)."""

    accepted = 0
    for size in _CONTEXT_LADDER:
        filler = ("the quick brown fox jumps over the lazy dog. " * (size // 9 + 1))[: size * 4]
        result = client.chat(
            {"messages": [{"role": "user", "content": filler}], "max_tokens": 1}
        )
        if result.get("ok"):
            accepted = size
            continue
        if result.get("status") in (429, 0, 500, 502, 503, 504):
            return _row(_describe_failure(result))
        break
    if not accepted:
        return _row("probe-failed: 0")
    if accepted >= _CONTEXT_LADDER[-1]:
        return _row("≥256k (ladder-top)")
    lower = [size for size in _CONTEXT_LADDER if size < accepted]
    return _row(f">{lower[-1] // 1000}k ≤{accepted // 1000}k" if lower else f"≤{accepted // 1000}k")


def _row(value: str, raw: Any = None) -> dict[str, Any]:
    row: dict[str, Any] = {"value": value}
    if raw is not None:
        row["raw"] = raw
    return row


def _run_probe(client: _Client, name: str, texts: Mapping[str, str]) -> dict[str, Any]:
    if name in _TOKENIZER_PROBES:
        text = texts.get(name)
        if not text:
            return _row("missing-probe-text")
        return _tokenizer_probe(client, text)
    if name == _TEMPLATE_PROBE:
        return _template_offset_probe(client)
    if name == "err-temperature":
        return _temperature_probe(client)
    if name == "err-maxtokens":
        return _maxtokens_probe(client)
    if name == "err-code-family":
        return _code_family_probe(client)
    if name == "finish-vocab":
        return _finish_vocabulary_probe(client)
    if name == _GEOMETRY_PROBE:
        return _logprob_geometry_probe(client)
    if name == "cap-contextceiling":
        return _context_ceiling_probe(client)
    return _row("probe-failed: unknown")


# --------------------------------------------------------------------------
# comparison and attribution
# --------------------------------------------------------------------------


def _comparable(value: object) -> bool:
    """Whether one probe value is a measurement rather than an excuse."""

    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text in _UNMEASURED_VALUES:
        return False
    lowered = text.lower()
    return not any(lowered.startswith(prefix) for prefix in _UNMEASURED_PREFIXES)


def compare(observed: Mapping[str, Any], recorded: Mapping[str, Any]) -> dict[str, Any]:
    """Row-by-row equality between one run and one recorded fingerprint."""

    matches: list[str] = []
    differences: list[dict[str, str]] = []
    tokenizer_hits = 0
    for name, value in recorded.items():
        current = observed.get(name)
        if not _comparable(value) or not _comparable(current):
            continue
        if str(value).strip() == str(current).strip():
            matches.append(name)
            if name in _TOKENIZER_PROBES:
                tokenizer_hits += 1
        else:
            differences.append({"probe": name, "expected": str(value), "observed": str(current)})
    total = len(matches) + len(differences)
    return {
        "matches": matches,
        "differences": differences,
        "total": total,
        "hits": len(matches),
        "tokenizer_hits": tokenizer_hits,
        "share": (len(matches) / total) if total else 0.0,
    }


def _recorded_for(stem: str, reference: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The recorded values behind one candidate stem."""

    if stem == "reference":
        values = reference.get("values") if isinstance(reference, Mapping) else None
        return values if isinstance(values, Mapping) else {}
    entry = baselines().get(stem)
    return latest_values(entry) if entry else {}


def _decisive_rows_agree(
    observed: Mapping[str, Any], recorded: Mapping[str, Any]
) -> bool:
    """Whether the rows that separate one model of a family from another agree.

    A row that was not measured on either side is not evidence either way, so it
    cannot make the match fail: the finding is then simply weaker, which the
    reported share already says.
    """

    compared = 0
    for name in SAME_VENDOR_DECISIVE_ROWS:
        current = observed.get(name)
        expected = recorded.get(name)
        if not _comparable(current) or not _comparable(expected):
            continue
        compared += 1
        if str(current).strip() != str(expected).strip():
            return False
    return compared > 0


def _attribute(
    observed: Mapping[str, Any],
    *,
    requested: str,
    reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Which model the observed values look like, and how firmly.

    Order of evidence, weakest first, because each one costs more than the one
    before it: the vendor stated by a matching tokenizer row, the name of the
    staged baseline that best matches, and finally a reference endpoint the
    caller supplied.  A vendor is still a useful answer when the exact name is
    not - a route serving ``deepseek-v4.1-flash`` where ``deepseek-v4-pro`` was
    bought is diluted either way.
    """

    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for stem, entry in _comparable_baselines(requested):
        recorded = latest_values(entry)
        if not recorded:
            continue
        result = compare(observed, recorded)
        candidates.append((stem, str(entry.get("model") or ""), result))
    if reference is not None:
        recorded = reference.get("values")
        model_name = str(reference.get("model") or "")
        if isinstance(recorded, Mapping) and model_name:
            candidates.append(("reference", model_name, compare(observed, recorded)))
    if not candidates:
        return {"label": None, "confidence": 0.0, "source": "", "candidates": []}

    def rank(item: tuple[str, str, dict[str, Any]]) -> tuple[int, float, int]:
        _, _, result = item
        attested = (
            result["total"] >= MIN_COMPARABLE_ROWS
            and result["tokenizer_hits"] >= MIN_TOKENIZER_ROWS
        )
        return (1 if attested else 0, result["share"], result["total"])

    candidates.sort(key=rank, reverse=True)
    best_stem, best_model, best = candidates[0]
    attested = (
        best["total"] >= MIN_COMPARABLE_ROWS
        and best["tokenizer_hits"] >= MIN_TOKENIZER_ROWS
        and best["share"] >= MATCH_SHARE
    )
    # Within one vendor the tokenizer rows cannot separate the models, so a
    # candidate of the same family must also agree on the rows that do.
    if attested and _model_family(best_model) == _model_family(requested):
        attested = _decisive_rows_agree(observed, _recorded_for(best_stem, reference))
    reported = [
        {
            "model": model_name,
            "baseline": stem,
            "share": round(result["share"], 3),
            "hits": result["hits"],
            "total": result["total"],
            "tokenizer_hits": result["tokenizer_hits"],
        }
        for stem, model_name, result in candidates[:4]
    ]
    if attested:
        return {
            "label": best_model,
            "confidence": round(best["share"], 3),
            "source": "reference" if best_stem == "reference" else "baseline",
            "candidates": reported,
        }
    # Nothing matched firmly.  The vendor is still readable when the tokenizer
    # rows agreed with one vendor's baseline and nothing else did - but only
    # across families, where those rows really do separate one lab from another.
    vendor_hit = next(
        (
            (model_name, result)
            for _, model_name, result in candidates
            if result["tokenizer_hits"] >= MIN_TOKENIZER_ROWS
            and result["share"] >= 0.5
            and _model_family(model_name) != _model_family(requested)
        ),
        None,
    )
    if vendor_hit is not None:
        return {
            "label": _model_family(vendor_hit[0]),
            "confidence": round(vendor_hit[1]["share"], 3),
            "source": "vendor",
            "candidates": reported,
        }
    return {"label": None, "confidence": 0.0, "source": "", "candidates": reported}


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def plan_target(model: str) -> str:
    """The name a run against this model is recorded under."""

    return normalize_model_name(model)


def run(
    *,
    api_base: str,
    api_key: str,
    model: str,
    protocol: str,
    reference: Mapping[str, Any] | None = None,
    include_context_ladder: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the probe table once against one endpoint and attribute the answer.

    ``reference`` is an optional ``{"model": str, "values": {probe: value}}``
    the caller collected from an endpoint it trusts (the vendor's own address,
    a route it verified before buying).  It is compared exactly like a staged
    baseline and wins on equal footing, which is what makes this usable for a
    model the tracker carries no entry for.

    Returns::

        {"protocol", "model", "verdict", "score", "summary", "run_error",
         "label", "label_confidence", "label_source", "candidates", "brands",
         "probes": [{"name", "value"}], "skipped", "failed", "calls"}

    Raises :class:`ModelPrintUnavailable` when this build carries no usable
    staged tables.
    """

    normalized_protocol = str(protocol or "").strip().lower()
    if normalized_protocol not in PROTOCOLS:
        raise ValueError(f"modelprint cannot probe the {protocol!r} protocol")
    if not isinstance(api_base, str) or not api_base.strip():
        raise ValueError("modelprint needs the endpoint address")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("modelprint needs the route credential")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("modelprint needs the upstream model name")
    if not available():
        raise ModelPrintUnavailable(
            "modelprint is not staged; run scripts/update_modelprint.py before building"
        )

    texts = probe_texts()
    names = probe_names(
        include_context_ladder=include_context_ladder,
    )
    client = _Client(
        api_base=api_base.strip(),
        api_key=api_key.strip(),
        protocol=normalized_protocol,
        model=model.strip(),
        timeout_seconds=_timeout_seconds(timeout_seconds),
    )
    observed: dict[str, Any] = {}
    probes: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed: list[str] = []
    budget_exhausted = False
    for name in names:
        if name == "cap-contextceiling" and not include_context_ladder:
            skipped.append(name)
            continue
        try:
            row = _run_probe(client, name, texts)
        except ProbeDeadline:
            budget_exhausted = True
            skipped.extend(names[names.index(name):])
            break
        except Exception as error:  # noqa: BLE001 - one probe cannot kill the run
            row = _row(f"probe-failed: {error}")
        value = str(row.get("value", ""))
        observed[name] = value
        probes.append({"name": name, "value": value})
        if not _comparable(value):
            failed.append(name)

    attribution = _attribute(observed, requested=model, reference=reference)
    label = attribution["label"]
    requested_name = normalize_model_name(model)
    if label and same_model(label, requested_name):
        verdict = "passed"
    elif label:
        verdict = "failed"
    else:
        verdict = "marginal"
    score = round(100 * (attribution["confidence"] if verdict != "marginal" else 0.0), 1)
    measured = [name for name, value in observed.items() if _comparable(value)]
    brands = sorted(
        {
            _model_family(str(candidate.get("model") or ""))
            for candidate in attribution["candidates"]
            if candidate.get("tokenizer_hits", 0) >= 1
        }
    )
    summary = {
        "passed": f"the tokenizer and shape rows match {label}",
        "failed": f"the rows match {label}, not {requested_name}",
        "marginal": "no staged baseline matched firmly enough to name a model",
    }[verdict]
    if budget_exhausted:
        summary += " (the probe budget ran out before every row was measured)"
    return {
        "protocol": normalized_protocol,
        "model": requested_name,
        "verdict": verdict,
        "score": score,
        "summary": summary,
        "run_error": None,
        "label": label,
        "label_confidence": attribution["confidence"],
        "label_source": attribution["source"],
        "candidates": attribution["candidates"],
        "brands": brands[:8],
        "probes": probes,
        "measured": measured,
        "skipped": skipped,
        "failed": failed,
        "calls": client.calls,
        "engine": engine(),
    }


def capture(
    *,
    api_base: str,
    api_key: str,
    model: str,
    protocol: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Collect one fingerprint to be used as a reference, in the staged shape.

    This is how a caller records what the vendor's own address answers: the
    result is ``{"model", "values"}`` and can be handed straight to
    :func:`run` as ``reference``.
    """

    outcome = run(
        api_base=api_base,
        api_key=api_key,
        model=model,
        protocol=protocol,
        timeout_seconds=timeout_seconds,
    )
    return {
        "model": normalize_model_name(model),
        "protocol": outcome["protocol"],
        "values": {row["name"]: row["value"] for row in outcome["probes"]},
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


def baseline_rows(*, official_only: bool = False) -> Iterable[tuple[str, str]]:
    """Every staged ``(baseline stem, model)`` pair, for a diagnostics view."""

    official = official_baseline_names()
    for stem, entry in sorted(baselines().items()):
        if official_only and stem not in official:
            continue
        yield stem, str(entry.get("model") or "")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MATCH_SHARE",
    "MIN_COMPARABLE_ROWS",
    "MIN_TOKENIZER_ROWS",
    "SAME_VENDOR_DECISIVE_ROWS",
    "ModelPrintUnavailable",
    "PROBE_MODE",
    "PROTOCOLS",
    "ProbeDeadline",
    "available",
    "baseline_rows",
    "baselines",
    "capture",
    "captured_baselines",
    "captured_root",
    "compare",
    "engine",
    "latest_values",
    "manifest",
    "normalize_model_name",
    "official_baseline_names",
    "plan",
    "plan_target",
    "probe_names",
    "probe_text_digests",
    "probe_texts",
    "protocol_for",
    "run",
    "same_model",
    "staged_root",
    "supported_models",
    "target",
    "texts_verified",
]
