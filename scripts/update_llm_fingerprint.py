#!/usr/bin/env python3
"""Stage the latest llm-fingerprint-detector for an artifact build.

The upstream project is https://github.com/ToseaAI/llm-fingerprint-detector
(MIT).  It is a TypeScript package with no npm release, so it is staged as
source and built with the project's own ``tsc`` — the same shape the other
bundled integrations take, and the reason this script runs ``npm`` rather than
copying files: the library's entry point is its build output.

**Why this one and not a smaller precision detector.**  The probe battery, the
reasoning-disable adapters and the verdict thresholds are the product, and they
come from a published study (arXiv:2607.10252): a same-model cross-provider
median distance of ~0.227 against a different-model median of ~0.463, cut at
0.25 and 0.35.  A detector with no calibration, or with one measured on a single
GPU by its author, cannot support the claim this app puts in a settings pane.

Staging verifies before it writes:

* the entry points the adapter imports are still exported,
* the threshold constants still carry the published values,
* the build produced ``dist/index.js``.

A release that renames an export or moves a threshold fails the build instead of
shipping a probe that would report something else.

``--check`` reports whether the staged copy is behind upstream without writing:
exit 0 current (or nothing staged yet), 3 stale, 4 upstream unreachable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

REPOSITORY = "https://github.com/ToseaAI/llm-fingerprint-detector"
API_BASE_URL = "https://api.github.com/repos/ToseaAI/llm-fingerprint-detector"
ARCHIVE_URL_ENV = "LLM_FINGERPRINT_ARCHIVE_URL"
REF_ENV = "LLM_FINGERPRINT_REF"
REVISION_ENV = "LLM_FINGERPRINT_REVISION"
TOKEN_ENV = "GITHUB_TOKEN"
DEFAULT_REF = "main"
DEFAULT_TIMEOUT_SECONDS = 300
USER_AGENT = "Young-Router/llm-fingerprint-build"

MANIFEST_TARGET = "manifest.json"
SOURCE_PREFIXES = ("src/",)
METADATA_FILES = (
    "LICENSE",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "README.md",
)
REVISION_PATTERN = re.compile(r"-([0-9a-f]{7,40})$")

#: The upstream objects the worker imports.  A release that renames one must
#: fail the build: the alternative is a shipped app whose precision probe throws
#: at probe time.
REQUIRED_EXPORTS: dict[str, tuple[str, ...]] = {
    "src/index.ts": ("fingerprint", "compare", "verify"),
}
#: The published thresholds.  They are the whole basis for the three-way verdict,
#: so a release that moves them changes what a finding means and must be noticed
#: by a human rather than absorbed silently.
REQUIRED_THRESHOLDS: dict[str, str] = {
    "JSD_MATCH_THRESHOLD": "0.25",
    "JSD_MISMATCH_THRESHOLD": "0.35",
}

DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
BUILD_ATTEMPTS = 2


class UpdateError(RuntimeError):
    """A staging step that must fail the build."""


def _log(message: str) -> None:
    print(f"[update_llm_fingerprint] {message}", flush=True)


def archive_url(ref: str) -> str:
    configured = os.environ.get(ARCHIVE_URL_ENV, "").strip()
    if configured:
        return configured
    return (
        f"https://codeload.github.com/ToseaAI/llm-fingerprint-detector"
        f"/tar.gz/refs/heads/{ref}"
    )


def _api_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _retryable(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return True


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
        payload = _download(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except Exception as error:  # noqa: BLE001 - reported as a build failure
        raise UpdateError(f"Could not download {url}: {error}") from error
    if not payload:
        raise UpdateError(f"Downloaded an empty archive from {url}")
    if payload[:2] != b"\x1f\x8b":
        raise UpdateError("The archive is not gzip data")
    return payload


def extract(archive: bytes) -> dict[str, bytes]:
    """The staged subset of the upstream tree, keyed by staged-relative path."""

    try:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as archive_file:
            files: dict[str, bytes] = {}
            for member in archive_file.getmembers():
                if not member.isfile():
                    continue
                parts = member.name.split("/", 1)
                if len(parts) != 2:
                    continue
                relative = parts[1]
                if not (relative.startswith(SOURCE_PREFIXES) or relative in METADATA_FILES):
                    continue
                handle = archive_file.extractfile(member)
                if handle is None:
                    raise UpdateError(f"The archive entry is unreadable: {member.name}")
                files[relative] = handle.read()
    except tarfile.TarError as error:
        raise UpdateError(f"The archive could not be read: {error}") from error
    if not any(name.endswith("index.ts") for name in files):
        raise UpdateError("The archive carries no src/ package")
    return files


def validate_sources(files: Mapping[str, bytes]) -> None:
    """Refuse a release whose exports or thresholds moved."""

    for path, names in REQUIRED_EXPORTS.items():
        payload = files.get(path)
        if payload is None:
            raise UpdateError(f"The archive is missing {path}")
        text = payload.decode("utf-8", errors="replace")
        for name in names:
            if not re.search(rf"\bexport\b[^\n]*\b{name}\b", text):
                raise UpdateError(f"{path} no longer exports {name}")
    constants = files.get("src/constants.ts")
    if constants is None:
        raise UpdateError("The archive is missing src/constants.ts")
    text = constants.decode("utf-8", errors="replace")
    for name, expected in REQUIRED_THRESHOLDS.items():
        match = re.search(rf"{name}\s*=\s*([0-9.]+)", text)
        if match is None:
            raise UpdateError(f"src/constants.ts no longer declares {name}")
        if match.group(1) != expected:
            raise UpdateError(
                f"src/constants.ts moved {name} to {match.group(1)} (the published value is "
                f"{expected}); the verdict boundaries changed, so the finding wording must be "
                "reviewed before this release ships"
            )


def thresholds(files: Mapping[str, bytes]) -> dict[str, float]:
    """The verdict boundaries this release states."""

    text = files["src/constants.ts"].decode("utf-8", errors="replace")
    resolved: dict[str, float] = {}
    for name in REQUIRED_THRESHOLDS:
        match = re.search(rf"{name}\s*=\s*([0-9.]+)", text)
        if match is not None:
            resolved[name] = float(match.group(1))
    return resolved


def build(output: Path, *, npm: str) -> None:
    """Build the staged package with its own toolchain.

    The library's entry point is its compiled output, so a staging that skipped
    this would ship source nothing can import.  ``npm ci`` is used when the
    archive carries a lockfile, because a staging step that resolved fresh
    versions would package a dependency set nobody reviewed.
    """

    install = [npm, "ci", "--no-audit", "--no-fund"] if (output / "package-lock.json").is_file() else [
        npm,
        "install",
        "--no-audit",
        "--no-fund",
    ]
    attempt = 0
    while True:
        attempt += 1
        completed = subprocess.run(install, cwd=output, capture_output=True, text=True, check=False)
        if completed.returncode == 0:
            break
        detail = (completed.stderr or completed.stdout).strip()
        if attempt >= BUILD_ATTEMPTS:
            raise UpdateError(
                "Could not install the staged package's dependencies"
                + (f": {detail[-800:]}" if detail else "")
            )
        _log(f"dependency install failed (attempt {attempt}/{BUILD_ATTEMPTS}); retrying")
        time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS * attempt)
    completed = subprocess.run(
        [npm, "run", "build"], cwd=output, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise UpdateError(
            "Could not build the staged package"
            + (f": {detail[-800:]}" if detail else "")
        )
    if not (output / "dist" / "index.js").is_file():
        raise UpdateError("The build produced no dist/index.js")


def prune(output: Path) -> None:
    """Drop what the runtime never reads, so the bundle stays small.

    ``node_modules`` is kept: the build's dependencies are dev-only, but a
    staged tree that carried an unbuilt source would be useless, and the compiled
    output is what ships.  What is dropped is everything the package does not
    need at runtime.
    """

    for name in ("node_modules", "test", "examples", "scripts", ".git", "coverage"):
        target = output / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    for pattern in ("*.tsbuildinfo", "*.map"):
        for path in output.rglob(pattern):
            path.unlink(missing_ok=True)


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
    version: str,
    boundaries: Mapping[str, float],
    archive_digest: str,
) -> dict[str, Any]:
    manifest = {
        "name": "llm-fingerprint-detector",
        "source": REPOSITORY,
        "ref": ref,
        "revision": revision,
        "archive_url": url,
        "archive_sha256": archive_digest,
        "staged_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "license": "MIT",
        "version": version,
        "thresholds": dict(boundaries),
        "paper": "arXiv:2607.10252",
        "files": {name: _digest(output / name) for name in staged_files(output)},
    }
    (output / MANIFEST_TARGET).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def stage(output: Path, files: Mapping[str, bytes]) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def package_version(files: Mapping[str, bytes]) -> str:
    payload = files.get("package.json")
    if payload is None:
        return ""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    return str(document.get("version") or "") if isinstance(document, dict) else ""


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
        return upstream_head(ref, timeout)
    except Exception:  # noqa: BLE001 - an offline or rate-limited lookup stays non-fatal
        pass
    return revision_from_archive(archive)


def check(arguments: argparse.Namespace) -> int:
    """Report whether the staged copy is behind upstream.

    Exit codes: 0 current or nothing staged, 3 stale, 4 upstream unreachable.
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
        _log(f"staged without a recorded revision; upstream is at {latest[:12]}")
        return 3
    if staged_revision == latest:
        _log(f"up to date at {latest[:12]}")
        return 0
    _log(f"STALE: staged {staged_revision[:12]}, upstream {latest[:12]}")
    return 3


def find_npm() -> str:
    configured = os.environ.get("NPM_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("npm") or ""


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage the latest llm-fingerprint-detector for an artifact build"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="directory to stage into (the Core's young_router/adapters/llm-fingerprint-detector)",
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
            "report whether the staged copy is behind upstream and exit: "
            "0 current or nothing staged, 3 stale, 4 upstream unreachable"
        ),
    )
    parser.add_argument("--no-build", action="store_true", help="stage the sources without building them")
    parser.add_argument("--missing-deps", default="", help=argparse.SUPPRESS)
    parser.add_argument("--install-deps", default="", help=argparse.SUPPRESS)
    parser.add_argument("--python", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    if arguments.check:
        return check(arguments)
    if arguments.missing_deps or arguments.install_deps:
        # Nothing to resolve into the Python runtime: this package runs on Node.
        return 0
    output = Path(arguments.output)
    url = arguments.archive_url or archive_url(arguments.ref)
    _log(f"staging llm-fingerprint-detector from {url}")
    archive = read_archive(url, arguments.timeout)
    files = extract(archive)
    validate_sources(files)
    boundaries = thresholds(files)
    version = package_version(files)
    revision = resolve_revision(
        arguments.ref,
        archive,
        arguments.timeout,
        allow_api=not arguments.archive_url and not os.environ.get(ARCHIVE_URL_ENV, "").strip(),
    )
    archive_digest = hashlib.sha256(archive).hexdigest()
    with tempfile.TemporaryDirectory(prefix="llm-fingerprint-stage-") as directory:
        staging = Path(directory) / "llm-fingerprint-detector"
        stage(staging, files)
        if not arguments.no_build:
            npm = find_npm()
            if not npm:
                raise UpdateError(
                    "npm is required to build the staged precision probe; "
                    "set NPM_BIN or install Node.js"
                )
            build(staging, npm=npm)
            prune(staging)
        if output.exists():
            shutil.rmtree(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, output)
    manifest = write_manifest(
        output,
        ref=arguments.ref,
        revision=revision,
        url=url,
        version=version,
        boundaries=boundaries,
        archive_digest=archive_digest,
    )
    _log(
        "staged llm-fingerprint-detector "
        + (manifest["revision"][:12] if manifest["revision"] else f"ref {arguments.ref}")
        + f" v{version or '?'} thresholds={boundaries}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
