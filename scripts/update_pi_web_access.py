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
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Any, Iterable


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
USER_AGENT = "LiteLLM-Menu/pi-web-access-build"
PACKAGE_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
NODE_VERSION_PATTERN = re.compile(r"^v22\.[0-9]+\.[0-9]+$")
FALLBACK_PI_PEERS = (
    "@earendil-works/pi-ai",
    "@earendil-works/pi-coding-agent",
    "@earendil-works/pi-tui",
)


class UpdateError(RuntimeError):
    """A build dependency could not be resolved or staged."""


def _request_bytes(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise UpdateError(f"Could not download {url}: {exc}") from exc


def _request_json(url: str) -> Any:
    raw = _request_bytes(url)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Downloaded metadata from {url} is not valid JSON") from exc


def _package_metadata(registry_url: str) -> tuple[str, str, dict[str, Any]]:
    payload = _request_json(registry_url)
    if not isinstance(payload, dict):
        raise UpdateError("npm registry returned an invalid pi-web-access metadata object")
    dist_tags = payload.get("dist-tags")
    latest = dist_tags.get("latest") if isinstance(dist_tags, dict) else None
    if not isinstance(latest, str) or not PACKAGE_VERSION_PATTERN.fullmatch(latest):
        raise UpdateError("npm registry did not return a stable pi-web-access latest version")
    versions = payload.get("versions")
    version_payload = versions.get(latest) if isinstance(versions, dict) else None
    if not isinstance(version_payload, dict):
        raise UpdateError(f"npm registry metadata is missing pi-web-access {latest}")
    dist = version_payload.get("dist")
    tarball = dist.get("tarball") if isinstance(dist, dict) else None
    if not isinstance(tarball, str) or not tarball.strip():
        raise UpdateError(f"npm registry metadata is missing the pi-web-access {latest} tarball")
    return latest, tarball.strip(), version_payload


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
    configured = os.environ.get(name, "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        raise UpdateError(f"Configured {name} does not point to an executable: {configured}")
    candidates = ["npm.cmd", "npm"] if os.name == "nt" else ["npm"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise UpdateError("npm is required to install pi-web-access and its Pi peer packages")


def _run_npm_install(
    npm: str,
    npm_root: Path,
    package_tarball: Path,
    peer_names: Iterable[str],
) -> None:
    npm_root.mkdir(parents=True, exist_ok=True)
    command = [
        npm,
        "install",
        "--prefix",
        str(npm_root),
        "--no-save",
        "--no-package-lock",
        "--ignore-scripts",
        "--omit=dev",
        "--fund=false",
        "--audit=false",
        str(package_tarball),
        *peer_names,
    ]
    env = os.environ.copy()
    # npm's cache is still allowed, but metadata was resolved above and the
    # package tarball itself is always downloaded by this helper.
    env.setdefault("NPM_CONFIG_UPDATE_NOTIFIER", "false")
    use_shell = os.name == "nt" and npm.lower().endswith((".cmd", ".bat"))
    try:
        result = subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            timeout=DEFAULT_TIMEOUT_SECONDS * 2,
            check=False,
            shell=use_shell,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"npm could not install pi-web-access: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 2000:
            details = details[-2000:]
        raise UpdateError(
            "npm could not install pi-web-access and its Pi peers"
            + (f": {details}" if details else "")
        )


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise UpdateError(f"Expected package directory is missing: {source}")
    shutil.copytree(source, destination, symlinks=True)


def _flatten_package(npm_root: Path, destination: Path) -> str:
    package_root = npm_root / "node_modules" / PACKAGE_NAME
    dependencies_root = npm_root / "node_modules"
    package_json = package_root / "package.json"
    entry = package_root / "index.ts"
    if not package_json.is_file() or not entry.is_file():
        raise UpdateError("Installed pi-web-access package is missing package.json or index.ts")

    package_payload = destination.parent / f".{destination.name}.staged"
    if package_payload.exists():
        shutil.rmtree(package_payload)
    _copy_tree(package_root, package_payload)

    dependency_payload = package_payload / "node_modules"
    dependency_payload.mkdir()
    for child in dependencies_root.iterdir():
        if child.name in {PACKAGE_NAME, ".bin"}:
            continue
        target = dependency_payload / child.name
        if child.is_symlink():
            target.symlink_to(os.readlink(child))
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)

    version_data = json.loads(package_json.read_text(encoding="utf-8"))
    version = version_data.get("version") if isinstance(version_data, dict) else None
    if not isinstance(version, str) or not PACKAGE_VERSION_PATTERN.fullmatch(version):
        raise UpdateError("Installed pi-web-access package has an invalid version")

    if destination.exists():
        if not destination.is_dir():
            raise UpdateError(f"pi-web-access output is not a directory: {destination}")
        shutil.rmtree(destination)
    package_payload.rename(destination)
    return version


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
    npm = _find_executable("LITELLM_NPM_BIN")

    with tempfile.TemporaryDirectory(prefix="litellm-menu-pi-web-access-") as directory:
        work = Path(directory)
        package_tarball = work / "pi-web-access.tgz"
        package_tarball.write_bytes(_request_bytes(tarball_url))
        npm_root = work / "npm"
        _run_npm_install(npm, npm_root, package_tarball, _peer_specs(version_payload))
        package_version = _flatten_package(npm_root, output)
        if package_version != latest:
            raise UpdateError(f"npm installed pi-web-access {package_version}, expected latest {latest}")

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
    parser.add_argument("--output", required=True, help="Core/litellm_menu/pi-web-access destination")
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
