#!/usr/bin/env python3
"""Download the latest pi-web-access package for an artifact build.

The upstream project is https://github.com/nicobailon/pi-web-access; its npm
distribution is used because it includes the published TypeScript extension
and the package metadata needed to install its Pi SDK peers.

The desktop Core is self-contained, so the build must not rely on a user's
global npm installation at runtime.  This helper resolves the npm ``latest``
dist-tag, installs the package and its Pi peer packages into a temporary npm
tree, then flattens the package into the requested Core directory.  It can
also copy an existing Node executable or download the newest Node 22 archive
for the current build host.

No repository lock is written: every artifact build performs a fresh metadata
lookup and stages a new package.  ``*_URL`` environment variables and the
matching command-line options exist for offline/unit-test fixtures.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tarfile
import tempfile
import zipfile
from typing import Any, Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import (
    UpdateError,
    download_bytes,
    find_package_manager,
    flatten_npm_package,
    package_metadata,
    record_staged_release,
    release_integrity,
    request_json,
    reused_staged_release,
    run_package_manager_install,
)


PACKAGE_NAME = "pi-web-access"
PACKAGE_REGISTRY_URL = os.environ.get(
    "PI_WEB_ACCESS_NPM_REGISTRY_URL",
    "https://registry.npmjs.org/pi-web-access",
)
NODE_INDEX_URL = os.environ.get(
    "PI_WEB_ACCESS_NODE_INDEX_URL",
    "https://nodejs.org/dist/index.json",
)
NODE_ARCHIVE_BASE_URL = os.environ.get(
    "PI_WEB_ACCESS_NODE_ARCHIVE_BASE_URL",
    "https://nodejs.org/dist",
)
DEFAULT_TIMEOUT_SECONDS = 120
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/pi-web-access-build"
# The shipped app never presents a User-Agent of its own, so the staged
# package's self-naming UA literals are rewritten to the same browser identity
# Core uses (`young_router/browser_identity.py`).
SELF_USER_AGENT_VALUE_RE = re.compile(
    r'''(?i)(["']user-agent["']\s*:\s*)(["'])([^"']*(?:pi-web-access|young[ _-]?router)[^"']*)\2'''
)
SELF_USER_AGENT_ASSIGNMENT_RE = re.compile(
    r'''(?i)(\bUSER_AGENT\s*=\s*)(["'])([^"']*(?:pi-web-access|young[ _-]?router)[^"']*)\2'''
)
STAGED_USER_AGENT_SUFFIXES = (".js", ".mjs", ".cjs", ".ts")
NODE_VERSION_PATTERN = re.compile(r"^v22\.[0-9]+\.[0-9]+$")
FALLBACK_PI_PEERS = (
    "@earendil-works/pi-ai",
    "@earendil-works/pi-coding-agent",
    "@earendil-works/pi-server",
    "@earendil-works/pi-tui",
)


def _request_bytes(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    """Download one staged file over the staging run's pooled connection.

    This script fetches two large files from two origins (the release index and
    the Node tarball) plus the package tarball, so the handshake it saves per
    request is several seconds of the build's wall clock.
    """

    return download_bytes(url, timeout=timeout, user_agent=USER_AGENT)


def _request_json(url: str) -> Any:
    return request_json(url, timeout=DEFAULT_TIMEOUT_SECONDS, user_agent=USER_AGENT)


def _package_metadata(registry_url: str) -> tuple[str, str, dict[str, Any]]:
    return package_metadata(
        registry_url,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        user_agent=USER_AGENT,
    )


def _peer_specs(version_payload: dict[str, Any]) -> list[str]:
    peers = version_payload.get("peerDependencies")
    names: list[str] = []
    if isinstance(peers, dict):
        for name in peers:
            if isinstance(name, str) and name.startswith("@earendil-works/pi-"):
                names.append(name)
    for name in FALLBACK_PI_PEERS:
        if name not in names:
            names.append(name)
    return names


def _find_executable(name: str) -> str:
    """The package manager this staging run installs with.

    pnpm is preferred for the same reason the vision router prefers it: it
    resolves the same registry graph an order of magnitude faster and its
    hoisted linker produces a smaller flat tree, which this staging step then
    copies and signs in every build.  npm stays the fallback so a machine
    without pnpm still builds.
    """

    return find_package_manager(
        name, purpose=f"install {PACKAGE_NAME} and its Pi peer packages"
    )


def _install_package(
    manager: str,
    manager_env: dict[str, str],
    npm_root: Path,
    package_tarball: Path,
    peer_names: Iterable[str],
) -> None:
    run_package_manager_install(
        manager,
        manager_env,
        npm_root,
        package_tarball,
        peer_names,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def _browser_user_agent() -> str:
    """Return the shared browser identity of the host this package is built for."""

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from young_router.browser_identity import browser_user_agent

    return browser_user_agent()


def _normalize_staged_user_agents(package_root: Path) -> int:
    """Rewrite the package's self-naming User-Agent literals.

    The shipped app must never expose a User-Agent of its own: its web search
    and fetch paths present the shared browser identity instead.  The staged
    package is patched here, in the one place that owns its adaptation, and the
    rewrite is verified — an upstream release that renames or drops these
    literals fails the build instead of silently shipping a self-identifying
    client.
    """

    user_agent = _browser_user_agent()
    rewritten = 0
    for path in sorted(package_root.rglob("*")):
        if path.suffix not in STAGED_USER_AGENT_SUFFIXES or not path.is_file():
            continue
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        updated, value_hits = SELF_USER_AGENT_VALUE_RE.subn(
            lambda match: f"{match.group(1)}{match.group(2)}{user_agent}{match.group(2)}",
            text,
        )
        updated, assignment_hits = SELF_USER_AGENT_ASSIGNMENT_RE.subn(
            lambda match: f"{match.group(1)}{match.group(2)}{user_agent}{match.group(2)}",
            updated,
        )
        if value_hits or assignment_hits:
            path.write_text(updated, encoding="utf-8")
            rewritten += value_hits + assignment_hits
    if rewritten < 3:
        raise UpdateError(
            "pi-web-access no longer exposes the expected self-naming User-Agent literals; "
            "re-check the package's request headers and update the staging adaptation"
        )
    return rewritten


def _post_stage_package(root: Path) -> None:
    _normalize_staged_user_agents(root)


def _carries_the_shared_user_agent(root: Path) -> bool:
    """Does this staged tree present exactly the shared browser identity?

    A reused tree is one ``_normalize_staged_user_agents`` already rewrote, so
    it has no self-naming literal left to replace and re-running that pass
    would fail its own "found too few" assertion rather than confirm anything.
    What a reuse has to prove instead is the property the rewrite exists to
    establish, in both directions: nothing self-naming remains, and the
    identity actually written into the tree is the one this build's host
    declares.  The second half is what lets a change to the shared identity
    invalidate a cached tree instead of shipping the previous one.

    Only the package's own sources are read, exactly as the rewrite reads them;
    ``node_modules`` is skipped so this stays a scan of a few hundred files.
    """

    if not root.is_dir():
        return False
    user_agent = _browser_user_agent()
    identity_written = False
    for path in sorted(root.rglob("*")):
        if path.suffix not in STAGED_USER_AGENT_SUFFIXES or not path.is_file():
            continue
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # One self-naming literal anywhere disqualifies the whole tree, so this
        # scans every file rather than stopping at the first good one.
        if SELF_USER_AGENT_VALUE_RE.search(text) or SELF_USER_AGENT_ASSIGNMENT_RE.search(text):
            return False
        if user_agent in text:
            identity_written = True
    return identity_written


def _flatten_package(npm_root: Path, destination: Path) -> str:
    return flatten_npm_package(
        npm_root,
        destination,
        package_name=PACKAGE_NAME,
        required_files=("package.json", "index.ts"),
        post_stage=_post_stage_package,
    )


def _node_target() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "osx-arm64-tar", "darwin-arm64", "node"
        if machine in {"x86_64", "amd64"}:
            return "osx-x64-tar", "darwin-x64", "node"
        raise UpdateError(f"Unsupported macOS Node architecture: {machine}")
    if system == "windows":
        if machine in {"amd64", "x86_64", "x64"}:
            return "win-x64-zip", "win-x64", "node.exe"
        raise UpdateError(f"Unsupported Windows Node architecture: {machine}")
    raise UpdateError("Node runtime packaging is supported only on macOS and Windows hosts")


def _node_release(index_url: str) -> tuple[str, str, str]:
    variant, archive_suffix, executable = _node_target()
    payload = _request_json(index_url)
    if not isinstance(payload, list):
        raise UpdateError("Node release index is invalid")
    for release in payload:
        if not isinstance(release, dict):
            continue
        version = release.get("version")
        files = release.get("files")
        if (
            isinstance(version, str)
            and NODE_VERSION_PATTERN.fullmatch(version)
            and isinstance(files, list)
            and variant in files
        ):
            filename = f"node-{version}-{archive_suffix}.tar.gz" if variant.endswith("-tar") else f"node-{version}-{archive_suffix}.zip"
            return version, filename, executable
    raise UpdateError("Node release index does not contain a Node 22 archive for this host")


def _extract_node_bytes(archive: bytes, filename: str, executable: str) -> bytes:
    wanted_suffix = "/bin/node" if executable == "node" else "/node.exe"
    if filename.endswith(".tar.gz"):
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as handle:
                for member in handle.getmembers():
                    if member.name.endswith(wanted_suffix) and member.isfile():
                        extracted = handle.extractfile(member)
                        if extracted is not None:
                            return extracted.read()
        except (OSError, tarfile.TarError) as exc:
            raise UpdateError("Downloaded Node archive is not a valid tarball") from exc
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as handle:
                for member in handle.infolist():
                    if member.filename.replace("\\", "/").endswith(wanted_suffix) and not member.is_dir():
                        return handle.read(member)
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError("Downloaded Node archive is not a valid zip file") from exc
    raise UpdateError("Downloaded Node archive does not contain its node executable")


def _copy_node_source(source: Path) -> bytes:
    candidates = [source]
    if source.is_dir():
        candidates = [
            source / "bin" / "node",
            source / "node",
            source / "bin" / "node.exe",
            source / "node.exe",
        ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                raise UpdateError(f"Could not read Node runtime source: {candidate}") from exc
            if data:
                return data
    raise UpdateError(f"Node runtime source does not contain node/node.exe: {source}")


def _install_node(node_output: Path, *, source: str | None, index_url: str, archive_url: str | None) -> str:
    _, _, executable = _node_target()
    if source:
        node_bytes = _copy_node_source(Path(source).expanduser())
        version = "copied"
    else:
        version, filename, executable = _node_release(index_url)
        if archive_url:
            url = archive_url
        else:
            url = f"{NODE_ARCHIVE_BASE_URL.rstrip('/')}/{version}/{filename}"
        node_bytes = _extract_node_bytes(_request_bytes(url, timeout=DEFAULT_TIMEOUT_SECONDS * 2), filename, executable)
    if len(node_bytes) < 1024 * 1024:
        raise UpdateError("Downloaded Node executable is unexpectedly small")
    node_output.mkdir(parents=True, exist_ok=True)
    target = node_output / executable
    temporary = target.with_name(f".{target.name}.staged")
    temporary.write_bytes(node_bytes)
    if executable == "node":
        temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if target.exists():
        target.unlink()
    temporary.replace(target)
    return version


def update(
    output: Path,
    *,
    node_output: Path | None,
    node_source: str | None,
    registry_url: str,
    node_index_url: str,
    node_archive_url: str | None,
) -> tuple[str, str | None]:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    latest, tarball_url, version_payload = _package_metadata(registry_url)
    integrity = release_integrity(version_payload)
    # The release was resolved above on this build; the branch below only skips
    # installing the identical one again.
    if reused_staged_release(
        output,
        package_name=PACKAGE_NAME,
        version=latest,
        integrity=integrity,
        required_files=("package.json", "index.ts"),
    ) and _carries_the_shared_user_agent(output):
        package_version = latest
    else:
        manager, manager_env = _find_executable("LITELLM_NPM_BIN")
        with tempfile.TemporaryDirectory(prefix="young-router-pi-web-access-") as directory:
            work = Path(directory)
            package_tarball = work / "pi-web-access.tgz"
            package_tarball.write_bytes(_request_bytes(tarball_url))
            npm_root = work / "npm"
            _install_package(
                manager, manager_env, npm_root, package_tarball, _peer_specs(version_payload)
            )
            package_version = _flatten_package(npm_root, output)
            if package_version != latest:
                raise UpdateError(
                    f"The package manager installed {PACKAGE_NAME} {package_version}, "
                    f"expected latest {latest}"
                )
            record_staged_release(
                output, package_name=PACKAGE_NAME, version=latest, integrity=integrity
            )

    node_version: str | None = None
    if node_output is not None:
        node_version = _install_node(
            node_output.expanduser().resolve(),
            source=node_source,
            index_url=node_index_url,
            archive_url=node_archive_url,
        )
    return package_version, node_version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Core/young_router/adapters/pi-web-access destination")
    parser.add_argument("--node-output", help="Directory receiving node or node.exe")
    parser.add_argument("--node-source", help="Existing Node 22 executable or distribution directory")
    parser.add_argument("--registry-url", default=PACKAGE_REGISTRY_URL)
    parser.add_argument("--node-index-url", default=NODE_INDEX_URL)
    parser.add_argument("--node-archive-url", default=os.environ.get("PI_WEB_ACCESS_NODE_ARCHIVE_URL"))
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    try:
        package_version, node_version = update(
            Path(options.output),
            node_output=Path(options.node_output) if options.node_output else None,
            node_source=options.node_source,
            registry_url=options.registry_url,
            node_index_url=options.node_index_url,
            node_archive_url=options.node_archive_url,
        )
    except (OSError, UpdateError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed {PACKAGE_NAME} {package_version} into {Path(options.output).expanduser()}")
    if node_version is not None:
        print(f"Installed Node 22 runtime ({node_version}) into {Path(options.node_output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
