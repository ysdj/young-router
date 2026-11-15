#!/usr/bin/env python3
"""Stage the latest modelprint tables for an artifact build.

The upstream project is https://github.com/unclecode/modelprint (MIT).  It is a
browser tool and a daily tracker, not a library, so nothing here imports it: the
*data* is what this app consumes.

    fingerprints/*.json     the tracker's per-model, per-host history
    probes/*.js             the probe sources, staged verbatim for audit
    VERSION                 the upstream probe-set version

The probe *texts* are lifted out of those sources and written into the manifest
with a digest each.  ``young_router/adapters/modelprint.py`` reads the texts from
there and refuses to run when a text does not hash to the recorded digest, so a
build can never compare new counts against baselines recorded from a different
prompt.

Each fingerprint file records every value change it saw.  The staged
``baselines/<model>__<host>.json`` keeps the newest version of each, because that
is the fingerprint a route is checked against today; the history stays upstream
where the commit log is the timeline.

``official_baselines`` records which entries were captured from the vendor's own
endpoint.  The tracker pins every model to one provider and most of those
providers are resellers; a reseller's serving template is not the vendor's, and
a route should be compared against the vendor's own entry when one exists.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

REPOSITORY = "https://github.com/unclecode/modelprint"
API_BASE_URL = "https://api.github.com/repos/unclecode/modelprint"
ARCHIVE_URL_ENV = "MODELPRINT_ARCHIVE_URL"
REF_ENV = "MODELPRINT_REF"
REVISION_ENV = "MODELPRINT_REVISION"
DEFAULT_REF = "main"
DEFAULT_TIMEOUT_SECONDS = 180
USER_AGENT = "Young-Router/modelprint-build"

MANIFEST_TARGET = "manifest.json"
BASELINE_DIR = "baselines"
UPSTREAM_DIR = "upstream"
METADATA_FILES = ("LICENSE", "VERSION")

#: The probe sources whose pinned text is compared across endpoints.  Each one
#: declares a ``const TEXT`` the probe sends verbatim; the name here is the probe
#: id, which is also the row name in a fingerprint.
PINNED_TEXT_PROBES = ("tok-english", "tok-chinese", "tok-code", "tok-emoji")

REVISION_PATTERN = re.compile(r"-([0-9a-f]{7,40})$")

#: Name prefixes that decide which wire a model is served on.  modelprint's own
#: registry treats everything that is not Anthropic or Gemini as an OpenAI chat
#: completion, and so does this.
ANTHROPIC_PREFIXES = ("claude",)
GEMINI_PREFIXES = ("gemini",)

#: Vendors whose own endpoint can be identified from the host name the tracker
#: records.  A host is "official" when its normalised form contains the vendor's
#: normalised form (``Z.AI`` for ``z-ai``, ``Anthropic`` for ``anthropic``).  The
#: overrides cover hosts whose name shares no spelling with the vendor, and an
#: entry named ``Stealth`` is never official: nobody's own endpoint is stealth.
OFFICIAL_HOST_OVERRIDES: dict[str, tuple[str, ...]] = {
    "z-ai": ("zai",),
    "x-ai": ("xai",),
    "google": ("googleaistudio", "google"),
    "meta-llama": ("meta",),
    "moonshotai": ("moonshot",),
    "nousresearch": ("nousresearch",),
    "deepseek": ("deepseek",),
    "openai": ("openai",),
    "anthropic": ("anthropic",),
    "qwen": ("alibaba", "qwen"),
    "minimax": ("minimax",),
}
NON_OFFICIAL_HOSTS = frozenset({"stealth", "anyscale", "openrouter"})

DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.0


class UpdateError(RuntimeError):
    """A staging step that must fail the build."""


def _log(message: str) -> None:
    print(f"[update_modelprint] {message}", flush=True)


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def archive_url(ref: str) -> str:
    configured = os.environ.get(ARCHIVE_URL_ENV, "").strip()
    if configured:
        return configured
    return f"https://codeload.github.com/unclecode/modelprint/tar.gz/refs/heads/{ref}"


def _retryable(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return True


TOKEN_ENV = "GITHUB_TOKEN"


def _api_headers() -> dict[str, str]:
    """Headers for one GitHub API call, authenticated when a token is present.

    An unauthenticated lookup is rate-limited per address, and a build machine
    that has already fetched anything today answers 403 to the version check -
    which would read as "upstream unreachable" on a machine that is perfectly
    online.  A token is not required, but it turns that into a real answer.
    """

    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _download(url: str, *, timeout: int, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:  # pragma: no cover - network path
            if attempt >= DOWNLOAD_ATTEMPTS or not _retryable(error):
                raise
            _log(f"{url} failed ({error}); retrying ({attempt + 1}/{DOWNLOAD_ATTEMPTS})")
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS * attempt)


def read_archive(url: str, timeout: int) -> bytes:
    if url.startswith("file://"):
        return Path(url[len("file://") :]).read_bytes()
    path = Path(url)
    if path.is_file():
        return path.read_bytes()
    try:
        payload = _download(
            url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/gzip"}
        )
    except Exception as error:  # noqa: BLE001 - reported as a build failure
        raise UpdateError(f"Could not download {url}: {error}") from error
    if not payload:
        raise UpdateError(f"Downloaded an empty modelprint archive from {url}")
    if payload[:2] != b"\x1f\x8b":
        raise UpdateError("The modelprint archive is not gzip data")
    return payload


def extract(archive: bytes) -> dict[str, bytes]:
    """Every file of the upstream tree, keyed by its path inside the archive."""

    try:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as archive_file:
            files: dict[str, bytes] = {}
            for member in archive_file.getmembers():
                if not member.isfile():
                    continue
                parts = member.name.split("/", 1)
                if len(parts) != 2:
                    continue
                handle = archive_file.extractfile(member)
                if handle is None:
                    continue
                files[parts[1]] = handle.read()
    except tarfile.TarError as error:
        raise UpdateError(f"The modelprint archive could not be read: {error}") from error
    if not any(name.startswith("probes/") for name in files):
        raise UpdateError("The modelprint archive carries no probes/ directory")
    if not any(name.startswith("fingerprints/") for name in files):
        raise UpdateError("The modelprint archive carries no fingerprints/ directory")
    return files


# --------------------------------------------------------------------------
# probe texts
# --------------------------------------------------------------------------


def probe_source_path(probe: str) -> str:
    return f"probes/{probe}.js"


def probe_text(files: Mapping[str, bytes], probe: str) -> str:
    """The pinned text one tokenizer probe sends, read out of its source.

    Upstream declares it as ``const TEXT = "…" + "…";``.  The literals are
    evaluated rather than copied so a multi-line concatenation cannot be
    transcribed wrong, and an unparsable declaration fails the build instead of
    shipping a fingerprint nobody can reproduce.
    """

    path = probe_source_path(probe)
    payload = files.get(path)
    if payload is None:
        raise UpdateError(f"The modelprint archive is missing {path}")
    source = payload.decode("utf-8", errors="replace")
    match = re.search(r"const TEXT = (.*?);\n", source, re.S)
    if not match:
        raise UpdateError(f"{path} no longer declares its pinned text")
    expression = match.group(1)
    try:
        value = _string_expression(ast.parse(_single_line(expression), mode="eval").body)
    except (ValueError, SyntaxError) as error:
        raise UpdateError(f"{path} declares a pinned text that cannot be read: {error}") from error
    if not isinstance(value, str) or not value.strip():
        raise UpdateError(f"{path} declares an empty pinned text")
    return value


def _single_line(expression: str) -> str:
    """Join a declaration broken across lines into one expression.

    Upstream writes ``"…"\n  + "…"``; a bare newline is only whitespace inside
    brackets, so the continuation is joined at the ``+`` it starts with.
    """

    return re.sub(r"\n\s*\+", " +", expression).strip()


def _string_expression(node: ast.AST) -> str:
    """Evaluate a concatenation of string literals, and nothing else.

    Upstream writes the text as ``"…"\n  + "…"``.  ``ast.literal_eval`` refuses
    the ``+`` form, and eval() on a fetched archive is not something a build
    should do, so exactly two node kinds are accepted: a string constant and the
    addition of two of these.
    """

    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        raise ValueError("the pinned text is not a string")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_expression(node.left) + _string_expression(node.right)
    raise ValueError("the pinned text is not a literal concatenation")


def pinned_texts(files: Mapping[str, bytes]) -> tuple[dict[str, str], dict[str, str]]:
    texts: dict[str, str] = {}
    digests: dict[str, str] = {}
    for probe in PINNED_TEXT_PROBES:
        text = probe_text(files, probe)
        texts[probe] = text
        digests[probe] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return texts, digests


# --------------------------------------------------------------------------
# baselines
# --------------------------------------------------------------------------


def latest_version(entry: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]] | None:
    """The newest recorded version of one fingerprint document."""

    versions = entry.get("versions")
    if not isinstance(versions, list):
        return None
    best: tuple[int, Mapping[str, Any]] | None = None
    for version in versions:
        if not isinstance(version, Mapping):
            continue
        values = version.get("values")
        if not isinstance(values, Mapping) or not values:
            continue
        number = version.get("v")
        order = number if isinstance(number, int) and not isinstance(number, bool) else 0
        if best is None or order >= best[0]:
            best = (order, version)
    return best


def baselines(
    files: Mapping[str, bytes],
) -> dict[str, dict[str, Any]]:
    """One staged fingerprint per tracked model+host, holding its newest values."""

    entries: dict[str, dict[str, Any]] = {}
    for name, payload in sorted(files.items()):
        if not name.startswith("fingerprints/") or not name.endswith(".json"):
            continue
        if name.endswith("index.json"):
            continue
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        model = document.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        newest = latest_version(document)
        if newest is None:
            continue
        _, version = newest
        stem = name[len("fingerprints/") : -len(".json")]
        entries[stem] = {
            "model": model.strip(),
            "host": str(document.get("host") or "").strip(),
            "first_tracked": str(document.get("first_tracked") or ""),
            "recorded_version": version.get("v"),
            "recorded_probe_set": str(version.get("probe_set") or ""),
            "recorded_first_seen": str(version.get("first_seen") or ""),
            "recorded_last_seen": str(version.get("last_seen") or ""),
            "values": {str(k): v for k, v in version["values"].items()},
        }
    if not entries:
        raise UpdateError("The modelprint archive carries no usable fingerprint")
    return entries


def model_vendor(model: str) -> str:
    value = model.strip().lower()
    if "/" in value:
        return value.split("/", 1)[0]
    for separator in ("-", "_", "."):
        if separator in value:
            return value.split(separator, 1)[0]
    return value


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def is_official(entry: Mapping[str, Any]) -> bool:
    """Whether this fingerprint was captured from the vendor's own endpoint."""

    host = str(entry.get("host") or "").strip()
    if not host:
        return False
    normalized_host = _normalized(host)
    if normalized_host in NON_OFFICIAL_HOSTS:
        return False
    vendor = model_vendor(str(entry.get("model") or ""))
    for candidate in OFFICIAL_HOST_OVERRIDES.get(vendor, (vendor,)):
        if _normalized(candidate) and _normalized(candidate) in normalized_host:
            return True
    return False


def protocol_of(model: str) -> str:
    name = model.strip().lower()
    bare = name.rsplit("/", 1)[-1]
    if bare.startswith(ANTHROPIC_PREFIXES):
        return "anthropic"
    if bare.startswith(GEMINI_PREFIXES):
        return "gemini"
    return "openai"


def supported_models(entries: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Which model names the staged baselines carry, grouped by wire."""

    grouped: dict[str, set[str]] = {}
    for entry in entries.values():
        model = str(entry.get("model") or "")
        bare = model.rsplit("/", 1)[-1]
        if not bare:
            continue
        grouped.setdefault(protocol_of(model), set()).add(bare)
    return {protocol: sorted(names) for protocol, names in sorted(grouped.items())}


# --------------------------------------------------------------------------
# manifest and staging
# --------------------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staged_files(output: Path) -> list[str]:
    return [
        str(path.relative_to(output))
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != MANIFEST_TARGET
    ]


def write_manifest(
    output: Path,
    *,
    ref: str,
    revision: str,
    url: str,
    probe_set: str,
    texts: Mapping[str, str],
    digests: Mapping[str, str],
    entries: Mapping[str, Mapping[str, Any]],
    archive_digest: str,
) -> dict[str, Any]:
    manifest = {
        "name": "modelprint",
        "source": REPOSITORY,
        "ref": ref,
        "revision": revision,
        "archive_url": url,
        "archive_sha256": archive_digest,
        "staged_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "license": "MIT",
        "probe_set": probe_set,
        "probe_texts": dict(texts),
        "probe_text_digests": dict(digests),
        "supported_models": supported_models(entries),
        "official_baselines": sorted(stem for stem, entry in entries.items() if is_official(entry)),
        "files": {name: _digest(output / name) for name in staged_files(output)},
    }
    (output / MANIFEST_TARGET).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def stage(output: Path, files: Mapping[str, bytes], entries: Mapping[str, Mapping[str, Any]]) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    baseline_dir = output / BASELINE_DIR
    baseline_dir.mkdir(parents=True, exist_ok=True)
    for stem, entry in entries.items():
        (baseline_dir / f"{stem}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    # The probe sources ride along for audit: a reader can see exactly which
    # declaration each pinned text and each row name came from without leaving
    # the installed app.
    upstream_dir = output / UPSTREAM_DIR / "probes"
    upstream_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        if not name.startswith("probes/") or not name.endswith(".js"):
            continue
        (upstream_dir / Path(name).name).write_bytes(payload)
    for name in METADATA_FILES:
        payload = files.get(name)
        if payload is None:
            if name == "VERSION":
                continue
            raise UpdateError(f"The modelprint archive is missing {name}")
        (output / name).write_bytes(payload)


def revision_from_archive(archive: bytes) -> str:
    try:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as archive_file:
            names = archive_file.getnames()
    except tarfile.TarError:
        return ""
    for name in names:
        root = name.split("/", 1)[0]
        match = REVISION_PATTERN.search(root)
        if match:
            return match.group(1)
    return ""


def upstream_head(ref: str, timeout: int) -> str:
    """The commit upstream's ref points at right now.

    ``resolve_revision`` falls back to reading a revision out of an archive's
    name, which a branch archive does not carry; a check has no archive to read,
    so it asks the API and reports an unreachable upstream instead of guessing.
    """

    body = _download(
        f"{API_BASE_URL}/commits/{ref}",
        timeout=min(timeout, 30),
        headers=_api_headers(),
    )
    payload = json.loads(body.decode("utf-8"))
    sha = payload.get("sha") if isinstance(payload, dict) else None
    return sha if isinstance(sha, str) else ""


def resolve_revision(ref: str, archive: bytes, timeout: int, *, allow_api: bool) -> str:
    configured = os.environ.get(REVISION_ENV, "").strip()
    if configured:
        return configured
    if not allow_api:
        return revision_from_archive(archive)
    try:
        body = _download(
            f"{API_BASE_URL}/commits/{ref}",
            timeout=min(timeout, 30),
            headers=_api_headers(),
        )
        payload = json.loads(body.decode("utf-8"))
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if isinstance(sha, str) and sha:
            return sha
    except Exception:  # noqa: BLE001 - an offline or rate-limited lookup stays non-fatal
        pass
    return revision_from_archive(archive)


def probe_set_version(files: Mapping[str, bytes]) -> str:
    payload = files.get("VERSION")
    if payload is None:
        return ""
    return payload.decode("utf-8", errors="replace").strip()


def check(arguments: argparse.Namespace) -> int:
    """Report whether the staged tables are behind upstream, and exit.

    A build must not package a fingerprint set that upstream has already
    replaced: the probe texts are versioned with the values they produced, and a
    release built from a stale pair reports differences that are not there.  The
    lookup is the same one staging does, so this is the check that runs first
    and staging is the same code path with a write at the end.

    Exit codes: 0 when the staged copy is current or absent (a first build has
    nothing to be behind), 3 when it is behind, 4 when upstream cannot be
    reached - a build must decide about that rather than assume "current".
    """

    output = Path(arguments.output)
    staged = output / MANIFEST_TARGET
    if not staged.is_file():
        _log("nothing staged yet; a build stages the current upstream")
        return 0
    try:
        document = json.loads(staged.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = {}
    staged_revision = str(document.get("revision") or "")
    try:
        latest = upstream_head(arguments.ref, arguments.timeout)
    except Exception as error:  # noqa: BLE001 - reported as an unreachable check
        _log(f"could not reach upstream: {error}")
        return 4
    if not latest:
        _log("upstream did not state a revision")
        return 4
    if not staged_revision:
        # Staged without a revision (an offline or rate-limited build).  It
        # cannot be shown to be current, so it is treated as behind and the
        # build restages: the manifest's archive digest is the only other
        # record, and comparing it would answer a different question.
        _log(f"staged without a recorded revision; upstream is at {latest[:12]}")
        return 3
    if staged_revision == latest:
        _log(f"up to date at {latest[:12]}")
        return 0
    _log(f"STALE: staged {staged_revision[:12]}, upstream {latest[:12]}")
    return 3


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage the latest modelprint tables for an artifact build")
    parser.add_argument(
        "--output",
        required=True,
        help="directory to stage into (the Core's young_router/adapters/modelprint)",
    )
    parser.add_argument(
        "--ref", default=os.environ.get(REF_ENV, "").strip() or DEFAULT_REF, help="upstream git ref (default: main)"
    )
    parser.add_argument("--archive-url", default="", help=f"explicit archive URL or path (default: ${ARCHIVE_URL_ENV})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="download timeout in seconds")
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "report whether the staged tables are behind upstream and exit: "
            "0 current or nothing staged, 3 stale, 4 upstream unreachable"
        ),
    )
    # Kept so a build script written against the Veridrop stager keeps working.
    # modelprint is data, so there is nothing to resolve into a runtime; both
    # flags answer "nothing missing" instead of failing an otherwise good build.
    parser.add_argument("--missing-deps", default="", help=argparse.SUPPRESS)
    parser.add_argument("--install-deps", default="", help=argparse.SUPPRESS)
    parser.add_argument("--python", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    if arguments.check:
        return check(arguments)
    if arguments.missing_deps or arguments.install_deps:
        # No runtime dependency: the tables are JSON and the probes are Python
        # in the Core itself.
        return 0
    output = Path(arguments.output)
    url = arguments.archive_url or archive_url(arguments.ref)
    _log(f"staging modelprint from {url}")
    archive = read_archive(url, arguments.timeout)
    files = extract(archive)
    texts, digests = pinned_texts(files)
    entries = baselines(files)
    probe_set = probe_set_version(files)
    revision = resolve_revision(
        arguments.ref,
        archive,
        arguments.timeout,
        allow_api=not arguments.archive_url and not os.environ.get(ARCHIVE_URL_ENV, "").strip(),
    )
    archive_digest = hashlib.sha256(archive).hexdigest()
    with tempfile.TemporaryDirectory(prefix="modelprint-stage-") as directory:
        staging = Path(directory) / "modelprint"
        stage(staging, files, entries)
        if output.exists():
            shutil.rmtree(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, output)
    manifest = write_manifest(
        output,
        ref=arguments.ref,
        revision=revision,
        url=url,
        probe_set=probe_set,
        texts=texts,
        digests=digests,
        entries=entries,
        archive_digest=archive_digest,
    )
    counts = ", ".join(
        f"{protocol} {len(names)}" for protocol, names in sorted(manifest["supported_models"].items())
    )
    _log(
        "staged modelprint "
        + (manifest["revision"][:12] if manifest["revision"] else f"ref {arguments.ref}")
        + f" probe set {probe_set or '?'}"
        + f" ({len(entries)} baselines, {len(manifest['official_baselines'])} official; {counts})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
