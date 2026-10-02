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

The staged layout consumed by ``young_router/adapters/traceone.py``::

    traceone.js                              dist/traceone.js
    prompt.txt                               prompts/identity-web-v1.txt
    manifest.json                            staging record
    data/<artifact>.json                     every dist/data/*.json artifact the
                                             classifier reads, by the role it
                                             declares (bank / adapter / support)

The artifact documents are discovered instead of pinned: upstream renames them
on classifier revisions (``unified_bank.json`` became ``unified_bank_v2_16.json``,
``codex_low_v4_adapter_415.json`` became ``codex_low_v7_adapter_791.json`` and
then ``codex_low_v8_optimized.json``), and a build that hard-codes the old
spelling would fail on a rename the engine itself handles.  A document that
declares its own ``schema`` names its role, and ````bank``/``adapter`` each have to
resolve to exactly one document — a genuinely dropped role still fails the
build.  A separate ``support`` document is optional: the 2026-10 release folded
those statistics into the adapter itself.  Whatever the staged module fetches
from ``./data/`` is staged with it, so a document the engine reads is never left
behind by a rename these rules do not know about.

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

# The module and the prompt are pinned by path; the classifier documents are
# resolved by the role they declare.  A release whose documents carry their own
# ``schema`` is named by it (``robust-number-fingerprint-bank``,
# ``traceone-sequence-adapter-v1``); an older release is resolved by the file
# name spelling it shipped.
REQUIRED_DIST_FILES = ("dist/traceone.js",)
PROMPT_SOURCE = "prompts/identity-web-v1.txt"
PROMPT_TARGET = "prompt.txt"
MANIFEST_TARGET = "manifest.json"
DATA_PREFIX = "dist/data/"

DATA_SCHEMA_ROLES = {
    "robust-number-fingerprint-bank": "bank",
    "traceone-sequence-adapter-v1": "adapter",
}

# ``bank`` carries the reference fingerprints and ``adapter`` the fitted
# document that shapes the comparison.  The separate ``support`` document is
# optional: the 2026-10 release folded those statistics into the adapter
# itself, so a release without one is complete, not broken.
DATA_ROLE_PATTERNS = {
    "bank": re.compile(r"unified_bank[^/]*\.json$"),
    "adapter": re.compile(r"_adapter_[^/]*\.json$"),
    "support": re.compile(r"_support_[^/]*\.json$"),
}
REQUIRED_DATA_ROLES = ("bank", "adapter")
# The staged module loads its artifacts itself, so the names it fetches are the
# staged set's authority: a document the engine reads must be staged whether or
# not its name still matches a role spelling.
DATA_REFERENCE_PATTERN = re.compile(r"[./]*data/([A-Za-z0-9._-]+\.json)")

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
            members_for_data = sorted(
                name for name in members if name.endswith(".json") and f"/{DATA_PREFIX}" in name
            )
            data_payloads: dict[str, bytes] = {}
            for name in members_for_data:
                handle = archive_file.extractfile(members[name])
                if handle is None:
                    raise SystemExit(f"The TraceOne archive entry is unreadable: {name}")
                data_payloads[name] = handle.read()
            module_text = files["dist/traceone.js"].decode("utf-8", errors="replace")
            for name in sorted(data_roles(data_payloads, module_text).values()):
                files[staged_data_name(name)] = data_payloads[name]
            return files
    except tarfile.TarError as exc:
        raise SystemExit(f"The TraceOne archive could not be read: {exc}") from exc


def artifact_role(name: str, payload: bytes) -> str:
    """The role one archive data document plays, or an empty string.

    A document that declares its own ``schema`` names its role, so a rename
    never moves the document out of its role; an older document is recognised
    by the file name spelling it shipped with.
    """

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        document = None
    if isinstance(document, dict):
        schema = document.get("schema")
        if isinstance(schema, str) and schema in DATA_SCHEMA_ROLES:
            return DATA_SCHEMA_ROLES[schema]
    for role, pattern in DATA_ROLE_PATTERNS.items():
        if pattern.search(name):
            return role
    return ""


def data_roles(data_payloads: dict[str, bytes], module_text: str = "") -> dict[str, str]:
    """Resolve each classifier document role to exactly one archive member."""

    roles: dict[str, str] = {}
    for name, payload in sorted(data_payloads.items()):
        role = artifact_role(name, payload)
        if not role:
            continue
        existing = roles.get(role)
        if existing is not None:
            raise SystemExit(
                f"The TraceOne archive declares more than one {role} artifact: "
                + ", ".join(sorted({existing, name}))
            )
        roles[role] = name
    for role in REQUIRED_DATA_ROLES:
        if role not in roles:
            raise SystemExit(f"The TraceOne archive has no {role} artifact under {DATA_PREFIX}")
    staged_names = {name.rsplit("/", 1)[-1]: name for name in data_payloads}
    for referenced in sorted(set(DATA_REFERENCE_PATTERN.findall(module_text))):
        name = staged_names.get(referenced)
        if name is None:
            raise SystemExit(
                f"The TraceOne module reads {referenced}, which the archive does not carry"
            )
        roles.setdefault(f"data:{referenced}", name)
    return roles


def staged_data_name(member_name: str) -> str:
    """The staged ``data/<basename>`` path for one archive data artifact."""

    return f"data/{member_name.rsplit('/', 1)[-1]}"


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
    for name in sorted(name for name in files if name.startswith("data/")):
        try:
            artifact = json.loads(files[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"The staged TraceOne artifact {name} is not valid JSON: {exc}") from exc
        if not isinstance(artifact, dict) or not artifact:
            raise SystemExit(f"The staged TraceOne artifact {name} is empty")
    if not files[PROMPT_SOURCE].decode("utf-8", errors="replace").strip():
        raise SystemExit("The staged TraceOne prompt is empty")
    return models


def staged_files(files: dict[str, bytes]) -> tuple[str, ...]:
    """The staged layout: module, prompt, and every discovered artifact."""

    return ("traceone.js", PROMPT_TARGET, *sorted(name for name in files if name.startswith("data/")))


def stage(output: Path, files: dict[str, bytes]) -> tuple[str, ...]:
    if output.exists():
        shutil.rmtree(output)
    (output / "data").mkdir(parents=True, exist_ok=True)
    (output / "traceone.js").write_bytes(files["dist/traceone.js"])
    (output / PROMPT_TARGET).write_bytes(files[PROMPT_SOURCE])
    names = sorted(name for name in files if name.startswith("data/"))
    for name in names:
        (output / name).write_bytes(files[name])
    return ("traceone.js", PROMPT_TARGET, *names)


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
    staged: tuple[str, ...],
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
        "files": {name: digest(output / name) for name in staged},
    }
    (output / MANIFEST_TARGET).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def smoke_test(output: Path, node: str | None = None) -> None:
    """Run the staged module once so a broken adaptation fails the build."""

    worker = Path(__file__).resolve().parents[1] / "young_router" / "adapters" / "traceone_worker.mjs"
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
    parser.add_argument("--output", required=True, help="directory to stage into (the Core's young_router/adapters/traceone)")
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
    staged: tuple[str, ...] = ()
    with tempfile.TemporaryDirectory(prefix="traceone-stage-") as directory:
        staging = Path(directory) / "traceone"
        staged = stage(staging, files)
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
        staged=staged,
    )
    _log(
        "staged TraceOne "
        + (manifest["revision"][:12] if manifest["revision"] else f"ref {arguments.ref}")
        + f" with {len(manifest['target_models'])} target routes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
