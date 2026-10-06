#!/usr/bin/env python3
"""Stage the latest Veridrop for an artifact build.

The upstream project is https://github.com/canarybyte/veridrop
(AGPL-3.0-or-later).  Its Python package is staged as source: the Core imports
it into its own interpreter and runs the quick suite in-process, so the staged
tree carries no private dependency directory and no launcher.  Whatever the
upstream project declares and the bundled runtime does not already carry is
added to that runtime by the build through ``--install-deps``, which never
overwrites a distribution the runtime already has.

Every artifact build performs a fresh lookup of the upstream default branch, so
the packaged deep test never lags the upstream project.  Nothing is committed as
a lock: the staged directory is written into the build's Core bundle, and
``manifest.json`` records which revision, which supported models, and which
runtime dependencies the revision declares.

The staged layout consumed by ``young_router/adapters/veridrop.py``::

    src/relay_detector/**     the upstream package, verbatim
    LICENSE                   upstream AGPL-3.0 text
    pyproject.toml            upstream project metadata
    manifest.json             staging record

The supported models are read from the upstream per-protocol tables instead of
being hard-coded here: a release that adds or drops a model changes what the
deep test offers, and a release that restructures those tables fails the build
rather than silently testing nothing.  The same holds for the entry points the
adapter calls: a release that renames one fails the build instead of failing a
user's probe.  ``VERIDROP_ARCHIVE_URL`` (or ``--archive-url``) points at an
explicit archive for offline and unit-test fixtures; the same environment
variable family is used by the other ``scripts/update_*.py`` staging helpers.
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
from typing import Any, Sequence

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the build runs on 3.11+
    tomllib = None  # type: ignore[assignment]


REPOSITORY = "https://github.com/canarybyte/veridrop"
API_BASE_URL = "https://api.github.com/repos/canarybyte/veridrop"
ARCHIVE_URL_ENV = "VERIDROP_ARCHIVE_URL"
REF_ENV = "VERIDROP_REF"
REVISION_ENV = "VERIDROP_REVISION"
DEFAULT_REF = "main"
DEFAULT_TIMEOUT_SECONDS = 180
USER_AGENT = "Young-Router/veridrop-build"

PACKAGE_PREFIX = "src/relay_detector/"
METADATA_FILES = ("LICENSE", "pyproject.toml")
MANIFEST_TARGET = "manifest.json"

# The upstream tables the deep test's supported models come from.  Each entry
# is (protocol, relative path, pattern): the anthropic table is a mapping whose
# keys are model ids, the other two are plain string lists.
MODEL_TABLE_PATTERNS: dict[str, tuple[str, re.Pattern[str]]] = {
    "anthropic": (
        "src/relay_detector/protocols/anthropic/config.py",
        re.compile(r'^\s{4}"([A-Za-z0-9._-]+)":\s*ModelInfo\(', re.M),
    ),
    "openai": (
        "src/relay_detector/protocols/openai/config.py",
        re.compile(r"OPENAI_MODEL_CHOICES\s*=\s*\[(.*?)\]", re.S),
    ),
    "gemini": (
        "src/relay_detector/protocols/gemini/config.py",
        re.compile(r"GEMINI_MODEL_CHOICES\s*=\s*\[(.*?)\]", re.S),
    ),
}
STRING_LITERAL_PATTERN = re.compile(r"[\"']([^\"']+)[\"']")
REVISION_PATTERN = re.compile(r"-([0-9a-f]{7,40})$")
REQUIRED_ENTRY_POINT = "relay_detector.cli:app"

# The adapter imports the staged package into the Core's interpreter and drives
# these upstream objects.  A release that renames one must fail the build: the
# alternative is a shipped app whose deep test raises at probe time.
REQUIRED_SOURCE_MARKERS: dict[str, tuple[str, ...]] = {
    "src/relay_detector/cli.py": ("async def _run_detect(",),
    # The adapter imports the compatibility module, so it has to keep
    # re-exporting the execution models the suite is configured with.
    "src/relay_detector/models.py": ("core.models",),
    "src/relay_detector/core/models.py": ("class ExecutionConfig", "class Mode", "class Protocol"),
    "src/relay_detector/report.py": ("def write_json(",),
}
PROTOCOL_MARKERS: dict[str, str] = {
    "anthropic": "src/relay_detector/protocols/anthropic/detectors/__init__.py",
    "openai": "src/relay_detector/protocols/openai/detectors/__init__.py",
    "gemini": "src/relay_detector/protocols/gemini/detectors/__init__.py",
}
BUILD_ALL_MARKER = "def build_all("

REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class UpdateError(RuntimeError):
    """A staging step that must fail the build."""


def _log(message: str) -> None:
    print(f"[update_veridrop] {message}", flush=True)


def archive_url(ref: str) -> str:
    configured = os.environ.get(ARCHIVE_URL_ENV, "").strip()
    if configured:
        return configured
    return f"https://codeload.github.com/canarybyte/veridrop/tar.gz/refs/heads/{ref}"


DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
DEPENDENCY_INSTALL_ATTEMPTS = 3


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
        payload = _download(
            url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/gzip"}
        )
    except Exception as exc:  # noqa: BLE001 - reported as a build failure
        raise UpdateError(f"Could not download {url}: {exc}") from exc
    if url.lower().endswith(".zip"):
        raise UpdateError("Only tar archives are supported for Veridrop staging")
    if not payload:
        raise UpdateError(f"Downloaded an empty Veridrop archive from {url}")
    return payload


def extract(archive: bytes) -> dict[str, bytes]:
    """Return the staged upstream files keyed by their staged-relative path."""

    if archive[:2] != b"\x1f\x8b":
        raise UpdateError("The Veridrop archive is not gzip data")
    try:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as archive_file:
            members = {
                member.name: member
                for member in archive_file.getmembers()
                if member.isfile()
            }
            files: dict[str, bytes] = {}
            for name, member in sorted(members.items()):
                relative = _staged_relative(name)
                if relative is None:
                    continue
                handle = archive_file.extractfile(member)
                if handle is None:
                    raise UpdateError(f"The Veridrop archive entry is unreadable: {name}")
                files[relative] = handle.read()
    except tarfile.TarError as exc:
        raise UpdateError(f"The Veridrop archive could not be read: {exc}") from exc
    if not any(name.endswith("cli.py") for name in files):
        raise UpdateError(f"The Veridrop archive carries no {PACKAGE_PREFIX} package")
    return files


def _staged_relative(member_name: str) -> str | None:
    """Map one archive member to its staged path, or None when it is skipped."""

    parts = member_name.split("/", 1)
    if len(parts) != 2:
        return None
    relative = parts[1]
    if relative.startswith(PACKAGE_PREFIX):
        return relative
    if relative in METADATA_FILES:
        return relative
    return None


def supported_models(files: dict[str, bytes]) -> dict[str, list[str]]:
    """Read the upstream per-protocol model tables.

    A release that renames or restructures a table fails the build: a deep test
    that silently offers nothing would read as "no model is supported".
    """

    models: dict[str, list[str]] = {}
    for protocol, (path, pattern) in MODEL_TABLE_PATTERNS.items():
        payload = files.get(path)
        if payload is None:
            raise UpdateError(f"The Veridrop archive is missing {path}")
        text = payload.decode("utf-8", errors="replace")
        if protocol == "anthropic":
            # The anthropic table is a mapping whose keys are the model ids, so
            # every entry is one supported model.
            names = [match.group(1) for match in pattern.finditer(text)]
            if not names:
                raise UpdateError(f"{path} does not declare its supported models any more")
        else:
            match = pattern.search(text)
            if not match:
                raise UpdateError(f"{path} does not declare its supported models any more")
            names = STRING_LITERAL_PATTERN.findall(match.group(1))
        resolved = [name.strip().lower() for name in names if name.strip()]
        if not resolved:
            raise UpdateError(f"{path} declares no supported model")
        models[protocol] = resolved
    return models


def validate_entry_points(files: dict[str, bytes]) -> None:
    """Refuse a release whose objects the adapter drives are gone."""

    for path, markers in REQUIRED_SOURCE_MARKERS.items():
        text = _source_text(files, path)
        for marker in markers:
            if marker not in text:
                raise UpdateError(f"{path} no longer declares {marker.strip()}")
    for protocol, path in PROTOCOL_MARKERS.items():
        if BUILD_ALL_MARKER not in _source_text(files, path):
            raise UpdateError(f"The {protocol} protocol no longer declares {BUILD_ALL_MARKER.strip()}")


def _source_text(files: dict[str, bytes], path: str) -> str:
    payload = files.get(path)
    if payload is None:
        raise UpdateError(f"The Veridrop archive is missing {path}")
    return payload.decode("utf-8", errors="replace")


def dependencies(files: dict[str, bytes]) -> list[str]:
    """The upstream runtime dependencies, from its own project metadata."""

    document = _project_document(files)
    project = document.get("project")
    requirements = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(requirements, list):
        raise UpdateError("The Veridrop pyproject.toml declares no runtime dependency list")
    return [str(requirement) for requirement in requirements]


def entry_point(files: dict[str, bytes]) -> str:
    """The upstream console script this staging corresponds to."""

    document = _project_document(files)
    project = document.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict) or REQUIRED_ENTRY_POINT not in set(map(str, scripts.values())):
        raise UpdateError(f"The Veridrop pyproject.toml no longer publishes {REQUIRED_ENTRY_POINT}")
    return REQUIRED_ENTRY_POINT


def _project_document(files: dict[str, bytes]) -> dict[str, Any]:
    payload = files.get("pyproject.toml")
    if payload is None or tomllib is None:
        raise UpdateError("The Veridrop archive carries no readable pyproject.toml")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError(f"The Veridrop pyproject.toml could not be read: {exc}") from exc
    return document if isinstance(document, dict) else {}


def requirement_name(requirement: str) -> str:
    """The distribution name one requirement line asks for."""

    match = REQUIREMENT_NAME_PATTERN.match(requirement)
    return match.group(1).lower().replace("_", "-") if match else ""


def missing_dependencies(requirements: Sequence[str], site_packages: Path) -> list[str]:
    """The requirements a site-packages directory does not already carry.

    Presence is read from the installed distributions on disk rather than by
    importing anything, so the caller can ask about an interpreter it is not
    running on.  A package that is present under any version satisfies the
    requirement: the bundle must not upgrade a dependency the runtime pins for
    its own use.
    """

    installed = _installed_distributions(site_packages)
    missing: list[str] = []
    for requirement in requirements:
        name = requirement_name(requirement)
        if not name:
            continue
        if name.replace("-", "_") in installed or name in installed:
            continue
        if name not in missing:
            missing.append(name)
    return missing


def _installed_distributions(site_packages: Path) -> set[str]:
    names: set[str] = set()
    try:
        entries = list(site_packages.iterdir())
    except OSError:
        return names
    for entry in entries:
        if entry.name.endswith(".dist-info"):
            names.add(_normalized_name(entry.name[: -len(".dist-info")].split("-", 1)[0]))
        elif entry.is_dir():
            names.add(_normalized_name(entry.name))
        elif entry.suffix in {".py", ".so", ".pyd"}:
            names.add(_normalized_name(entry.stem))
    return names


def _normalized_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def revision_from_archive(archive: bytes) -> str:
    """codeload archives use ``veridrop-<revision>`` as their root directory."""

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

    A codeload archive of a commit is named ``veridrop-<revision>``; a branch
    archive is named after the branch, so the API is consulted first and its
    failure is never fatal - the manifest also records the archive digest.
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


def digest(path: Path) -> str:
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
    models: dict[str, list[str]],
    requirements: list[str],
    entry: str,
    archive_digest: str,
) -> dict[str, Any]:
    manifest = {
        "name": "Veridrop",
        "source": REPOSITORY,
        "ref": ref,
        "revision": revision,
        "archive_url": url,
        "archive_sha256": archive_digest,
        "staged_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "license": "AGPL-3.0-or-later",
        "entry_point": entry,
        "source_path": "src",
        "supported_models": models,
        "dependencies": requirements,
        "files": {name: digest(output / name) for name in staged_files(output)},
    }
    (output / MANIFEST_TARGET).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def stage(output: Path, files: dict[str, bytes]) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage the latest Veridrop for an artifact build")
    parser.add_argument(
        "--output",
        required=True,
        help="directory to stage into (the Core's young_router/adapters/veridrop)",
    )
    parser.add_argument(
        "--ref", default=os.environ.get(REF_ENV, "").strip() or DEFAULT_REF, help="upstream git ref (default: main)"
    )
    parser.add_argument("--archive-url", default="", help=f"explicit archive URL or path (default: ${ARCHIVE_URL_ENV})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="download timeout in seconds")
    parser.add_argument(
        "--missing-deps",
        default="",
        help=(
            "print the staged revision's dependencies that this site-packages "
            "directory does not already carry, then exit"
        ),
    )
    parser.add_argument(
        "--install-deps",
        default="",
        help="complete this site-packages directory with the staged revision's dependencies, then exit",
    )
    parser.add_argument("--python", default="", help="interpreter the dependencies are resolved for")
    return parser.parse_args(argv)


def install_dependencies(output: Path, site_packages: Path, *, python: str, uv: str) -> list[str]:
    """Complete a runtime site-packages with what the staged revision imports.

    Only the distributions the runtime does not already carry are added, and the
    runtime's own copy always wins: a release that pins a dependency for its own
    use must not be upgraded behind its back.  A package added together with a
    dependency the runtime already has therefore runs against the runtime's
    version of that dependency, which the build's own import smoke test verifies
    before the app is packaged.
    """

    requirements = manifest_requirements(output)
    missing = missing_dependencies(requirements, site_packages)
    if not missing:
        return []
    _log(f"resolving {', '.join(missing)} for {site_packages}")
    with tempfile.TemporaryDirectory(prefix="veridrop-deps-") as directory:
        staging = Path(directory) / "site-packages"
        staging.mkdir(parents=True, exist_ok=True)
        _resolve(missing, staging, python=python, uv=uv)
        return copy_absent(staging, site_packages)


def _resolve(packages: list[str], target: Path, *, python: str, uv: str) -> None:
    """Resolve packages and their own dependencies into a fresh directory."""

    if uv:
        command = [uv, "pip", "install", "--python", python, "--target", str(target), *packages]
    else:
        command = [python, "-m", "pip", "install", "--target", str(target), *packages]
    attempt = 0
    while True:
        attempt += 1
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode == 0:
            return
        detail = (completed.stderr or completed.stdout).strip()
        if attempt >= DEPENDENCY_INSTALL_ATTEMPTS:
            raise UpdateError(
                f"Could not resolve {', '.join(packages)}"
                + (f": {detail[-800:]}" if detail else "")
            )
        _log(f"dependency resolution failed (attempt {attempt}/{DEPENDENCY_INSTALL_ATTEMPTS}); retrying")
        time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS * attempt)


def copy_absent(source: Path, destination: Path) -> list[str]:
    """Copy every top-level entry the destination does not already carry."""

    present = _installed_distributions(destination)
    added: list[str] = []
    for entry in sorted(source.iterdir()):
        if entry.name.startswith(".") or entry.name in {"bin", "__pycache__"}:
            continue
        target = destination / entry.name
        if target.exists():
            continue
        if entry.name.endswith(".dist-info"):
            # A distribution the runtime already carries keeps its own record:
            # copying a resolve-time metadata directory would only make
            # ``importlib.metadata`` report a version the runtime's own code
            # does not have.
            base = _normalized_name(entry.name[: -len(".dist-info")].split("-", 1)[0])
            if base in present:
                continue
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)
        added.append(entry.name)
    return added


def find_uv() -> str:
    configured = os.environ.get("UV_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("uv") or ""


def report_missing_dependencies(output: Path, site_packages: Path) -> int:
    """Print the absent dependency names on stdout, one line, for the build.

    The build captures that line; every diagnostic goes to stderr so the two
    never mix.
    """

    requirements = manifest_requirements(output)
    missing = missing_dependencies(requirements, site_packages)
    if missing:
        print(
            f"[update_veridrop] {site_packages} is missing: {' '.join(missing)}",
            file=sys.stderr,
            flush=True,
        )
        print(" ".join(missing))
    return 0


def manifest_requirements(output: Path) -> list[str]:
    try:
        document = json.loads((output / MANIFEST_TARGET).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"The staged Veridrop manifest is unreadable: {output / MANIFEST_TARGET}") from exc
    requirements = document.get("dependencies") if isinstance(document, dict) else None
    if not isinstance(requirements, list):
        raise UpdateError("The staged Veridrop manifest declares no dependencies")
    return [str(requirement) for requirement in requirements]


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    output = Path(arguments.output)
    if arguments.missing_deps:
        return report_missing_dependencies(output, Path(arguments.missing_deps))
    if arguments.install_deps:
        added = install_dependencies(
            output,
            Path(arguments.install_deps),
            python=arguments.python or sys.executable,
            uv=find_uv(),
        )
        if added:
            _log("added " + ", ".join(added))
        return 0
    url = arguments.archive_url or archive_url(arguments.ref)
    _log(f"staging Veridrop from {url}")
    archive = read_archive(url, arguments.timeout)
    files = extract(archive)
    models = supported_models(files)
    validate_entry_points(files)
    requirements = dependencies(files)
    entry = entry_point(files)
    revision = resolve_revision(
        arguments.ref,
        archive,
        arguments.timeout,
        allow_api=not arguments.archive_url and not os.environ.get(ARCHIVE_URL_ENV, "").strip(),
    )
    archive_digest = hashlib.sha256(archive).hexdigest()
    with tempfile.TemporaryDirectory(prefix="veridrop-stage-") as directory:
        staging = Path(directory) / "veridrop"
        stage(staging, files)
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
        requirements=requirements,
        entry=entry,
        archive_digest=archive_digest,
    )
    counts = ", ".join(f"{protocol} {len(names)}" for protocol, names in sorted(models.items()))
    _log(
        "staged Veridrop "
        + (manifest["revision"][:12] if manifest["revision"] else f"ref {arguments.ref}")
        + f" with supported models ({counts})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
