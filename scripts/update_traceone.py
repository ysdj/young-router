#!/usr/bin/env python3
"""Stage the latest TraceOne for an artifact build.

The upstream project is https://github.com/wangchao0708/TraceOne.  Its ``dist``
build is used because it is the dependency-free ES module the project publishes
for local client-side attribution: the desktop Core runs it through the bundled
Node.js runtime instead of carrying a second copy of the classifier.

Every artifact build performs a fresh lookup of the upstream default branch, so
the packaged degradation deep test never lags the upstream project.  Nothing is
committed as a lock: the staged directory is written into the build's Core
bundle, and ``manifest.json`` records which revision and which target routes
were staged.

The staged layout consumed by ``young_router/traceone.py``::

    traceone.js                              dist/traceone.js
    prompt.txt                               prompts/identity-web-v1.txt
    manifest.json                            staging record
    data/unified_bank.json                   dist/data/unified_bank.json
    data/codex_low_v4_adapter_415.json       dist/data/...
    data/codex_low_v4_support_415.json       dist/data/...

``TRACEONE_ARCHIVE_URL`` (or ``--archive-url``) points at an explicit archive
for offline and unit-test fixtures; the same environment variable family is
used by ``scripts/update_pi_web_access.py``.
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
from typing import Any


REPOSITORY = "https://github.com/wangchao0708/TraceOne"
API_BASE_URL = "https://api.github.com/repos/wangchao0708/TraceOne"
ARCHIVE_URL_ENV = "TRACEONE_ARCHIVE_URL"
REF_ENV = "TRACEONE_REF"
REVISION_ENV = "TRACEONE_REVISION"
DEFAULT_REF = "main"
DEFAULT_TIMEOUT_SECONDS = 180
USER_AGENT = "Young-Router/traceone-build"

# Files copied out of the upstream archive.  Every path is required: a missing
# one means upstream moved or renamed it, and packaging a partial adaptation is
# worse than failing the build.
REQUIRED_DIST_FILES = (
    "dist/traceone.js",
    "dist/data/unified_bank.json",
    "dist/data/codex_low_v4_adapter_415.json",
    "dist/data/codex_low_v4_support_415.json",
)
PROMPT_SOURCE = "prompts/identity-web-v1.txt"
PROMPT_TARGET = "prompt.txt"
MANIFEST_TARGET = "manifest.json"

STAGED_FILES = (
    "traceone.js",
    PROMPT_TARGET,
    "data/unified_bank.json",
    "data/codex_low_v4_adapter_415.json",
    "data/codex_low_v4_support_415.json",
)

TARGET_MODELS_PATTERN = re.compile(r"export const TARGET_MODELS = \[(.*?)\]", re.S)
STRING_LITERAL_PATTERN = re.compile(r"[\"']([^\"']+)[\"']")
REVISION_PATTERN = re.compile(r"-([0-9a-f]{7,40})$")
REQUIRED_EXPORTS = ("identifyWithArtifacts", "parseGridResponse")


def _log(message: str) -> None:
    print(f"[update_traceone] {message}", flush=True)


def archive_url(ref: str) -> str:
    configured = os.environ.get(ARCHIVE_URL_ENV, "").strip()
    if configured:
        return configured
    return f"https://codeload.github.com/wangchao0708/TraceOne/tar.gz/refs/heads/{ref}"


DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.0


def _retryable_download_error(error: BaseException) -> bool:
    """A reset or a slow edge is worth another attempt; a 404 is not."""

    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    return True


def _download(url: str, *, timeout: int, headers: dict[str, str]) -> bytes:
    """Fetch one URL, retrying the transport the way the Core probe does."""

    request = urllib.request.Request(url, headers=headers)
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover - network path
            if attempt >= DOWNLOAD_ATTEMPTS or not _retryable_download_error(exc):
                raise
            _log(f"{url} failed ({exc}); retrying ({attempt + 1}/{DOWNLOAD_ATTEMPTS})")
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS * attempt)


def read_archive(url: str, timeout: int) -> bytes:
    if url.startswith("file://"):
        return Path(url[len("file://"):]).read_bytes()
    path = Path(url)
    if path.is_file():
        return path.read_bytes()
    try:
        payload = _download(url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/gzip"})
    except Exception as exc:  # noqa: BLE001 - reported as a build failure
        raise SystemExit(f"Could not download {url}: {exc}") from exc
    if url.lower().endswith(".zip"):
        raise SystemExit("Only tar archives are supported for TraceOne staging")
    if not payload:
        raise SystemExit(f"Downloaded an empty TraceOne archive from {url}")
    return payload


def extract(archive: bytes) -> dict[str, bytes]:
    """Return the required upstream files keyed by archive-relative path."""

    if archive[:2] != b"\x1f\x8b":
        raise SystemExit("The TraceOne archive is not gzip data")
    try:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as archive_file:
            members = {member.name: member for member in archive_file.getmembers() if member.isfile()}
            files: dict[str, bytes] = {}
            for wanted in (*REQUIRED_DIST_FILES, PROMPT_SOURCE):
                matches = [name for name in members if name.endswith(f"/{wanted}")]
                if not matches:
                    raise SystemExit(f"The TraceOne archive is missing {wanted}")
                handle = archive_file.extractfile(members[sorted(matches)[0]])
                if handle is None:
                    raise SystemExit(f"The TraceOne archive entry is unreadable: {wanted}")
                files[wanted] = handle.read()
            return files
    except tarfile.TarError as exc:
        raise SystemExit(f"The TraceOne archive could not be read: {exc}") from exc


def revision_from_archive(archive: bytes) -> str:
    """codeload archives use ``TraceOne-<revision>`` as their root directory."""

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


def resolve_revision(ref: str, archive: bytes, timeout: int, *, allow_api: bool) -> str:
    """Resolve the staged revision without requiring the GitHub API.

    A codeload archive of a commit is named ``TraceOne-<revision>``; a branch
    archive is named after the branch, so the API is consulted first and its
    failure is never fatal - the manifest also records the archive digest.
    An explicitly supplied archive URL skips the lookup, because that content
    is pinned by the caller rather than by the upstream default branch.
    """

    configured = os.environ.get(REVISION_ENV, "").strip()
    if configured:
        return configured
    if not allow_api:
        return revision_from_archive(archive)
    try:
        body = _download(
            f"{API_BASE_URL}/commits/{ref}",
            timeout=min(timeout, 30),
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        payload = json.loads(body.decode("utf-8"))
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if isinstance(sha, str) and sha:
            return sha
    except Exception:  # noqa: BLE001 - an offline or rate-limited lookup stays non-fatal
        pass
    return revision_from_archive(archive)


def target_models(module_text: str) -> list[str]:
    match = TARGET_MODELS_PATTERN.search(module_text)
    if not match:
        raise SystemExit("The staged TraceOne module does not declare TARGET_MODELS")
    return STRING_LITERAL_PATTERN.findall(match.group(1))


def validate(files: dict[str, bytes]) -> list[str]:
    module_text = files["dist/traceone.js"].decode("utf-8", errors="replace")
    for export in REQUIRED_EXPORTS:
        if f"function {export}(" not in module_text:
            raise SystemExit(f"The staged TraceOne module does not export {export}")
    models = target_models(module_text)
    if not models:
        raise SystemExit("The staged TraceOne module declares no target routes")
    try:
        bank = json.loads(files["dist/data/unified_bank.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"The staged TraceOne bank is not valid JSON: {exc}") from exc
    if not isinstance(bank, dict) or not bank:
        raise SystemExit("The staged TraceOne bank is empty")
    if not files[PROMPT_SOURCE].decode("utf-8", errors="replace").strip():
        raise SystemExit("The staged TraceOne prompt is empty")
    return models


def stage(output: Path, files: dict[str, bytes]) -> None:
    if output.exists():
        shutil.rmtree(output)
    (output / "data").mkdir(parents=True, exist_ok=True)
    (output / "traceone.js").write_bytes(files["dist/traceone.js"])
    (output / PROMPT_TARGET).write_bytes(files[PROMPT_SOURCE])
    for name in ("unified_bank.json", "codex_low_v4_adapter_415.json", "codex_low_v4_support_415.json"):
        (output / "data" / name).write_bytes(files[f"dist/data/{name}"])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(
    output: Path,
    *,
    ref: str,
    revision: str,
    url: str,
    models: list[str],
    archive_digest: str,
) -> dict[str, Any]:
    manifest = {
        "name": "TraceOne",
        "source": REPOSITORY,
        "ref": ref,
        "revision": revision,
        "archive_url": url,
        "archive_sha256": archive_digest,
        "staged_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "target_models": models,
        "files": {name: digest(output / name) for name in STAGED_FILES},
    }
    (output / MANIFEST_TARGET).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def smoke_test(output: Path, node: str | None = None) -> None:
    """Run the staged module once so a broken adaptation fails the build."""

    worker = Path(__file__).resolve().parents[1] / "young_router" / "traceone_worker.mjs"
    if not worker.is_file():
        raise SystemExit(f"The TraceOne worker is missing: {worker}")
    executable = node or os.environ.get("YOUNG_ROUTER_TRACEONE_NODE", "").strip() or shutil.which("node")
    if not executable:
        raise SystemExit(
            "Node.js is required to verify the staged TraceOne module; "
            "install Node.js 22+ or set YOUNG_ROUTER_TRACEONE_NODE"
        )
    answer = json.dumps(
        [[((index * 37 + row * 11) % 355) + 1 for index in range(35)] for row in range(9)],
        separators=(",", ":"),
    )
    completed = subprocess.run(
        [executable, str(worker), "--dir", str(output)],
        input=json.dumps({"id": "smoke", "text": answer}) + "\n",
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.strip().startswith("{")), "")
    if not line:
        raise SystemExit(
            "The staged TraceOne worker produced no result: "
            + (completed.stderr.strip()[:300] or f"exit status {completed.returncode}")
        )
    payload = json.loads(line)
    if payload.get("id") != "smoke" or not payload.get("ok"):
        raise SystemExit(f"The staged TraceOne worker failed: {payload.get('error') or payload}")
    result = payload.get("result") or {}
    if result.get("status") not in {"identified", "unknown"} or "parsed" not in result:
        raise SystemExit("The staged TraceOne worker returned an unexpected result shape")


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage the latest TraceOne for an artifact build")
    parser.add_argument("--output", required=True, help="directory to stage into (the Core's young_router/traceone)")
    parser.add_argument("--ref", default=os.environ.get(REF_ENV, "").strip() or DEFAULT_REF, help="upstream git ref (default: main)")
    parser.add_argument("--archive-url", default="", help=f"explicit archive URL or path (default: ${ARCHIVE_URL_ENV})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="download timeout in seconds")
    parser.add_argument("--node", default="", help="Node.js executable used for the staged smoke test")
    parser.add_argument("--no-smoke-test", action="store_true", help="skip the staged worker smoke test (offline fixtures only)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    url = arguments.archive_url or archive_url(arguments.ref)
    _log(f"staging TraceOne from {url}")
    archive = read_archive(url, arguments.timeout)
    files = extract(archive)
    models = validate(files)
    revision = resolve_revision(
        arguments.ref,
        archive,
        arguments.timeout,
        allow_api=not arguments.archive_url and not os.environ.get(ARCHIVE_URL_ENV, "").strip(),
    )
    archive_digest = hashlib.sha256(archive).hexdigest()
    output = Path(arguments.output)
    with tempfile.TemporaryDirectory(prefix="traceone-stage-") as directory:
        staging = Path(directory) / "traceone"
        stage(staging, files)
        if not arguments.no_smoke_test:
            smoke_test(staging, arguments.node or None)
        if output.exists():
            shutil.rmtree(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, output)
    manifest = write_manifest(
        output,
        ref=arguments.ref,
        revision=revision,
        url=url,
        models=models,
        archive_digest=archive_digest,
    )
    _log(
        "staged TraceOne "
        + (manifest["revision"][:12] if manifest["revision"] else f"ref {arguments.ref}")
        + f" with {len(manifest['target_models'])} target routes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
