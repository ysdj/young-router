#!/usr/bin/env python3
"""Stage the latest ``dsh-workbuddy-connect`` package for an artifact build.

WorkBuddy model access is not reimplemented in this repository: the upstream
project (https://github.com/corrinehu/dsh-workbuddy-connect) owns the desktop
app credential format, the token refresh, both model catalogs, and the
loopback shim that speaks the upstream's Chat Completions wire format.  Its
npm distribution is the integration surface, so every artifact build resolves
the ``latest`` dist-tag, installs the package together with its declared peer
dependencies, and flattens the result into the Core bundle.  No repository lock
is written: a stale adaptation is never packaged.

``*_URL`` environment variables and the matching command-line options exist for
offline/unit-test fixtures.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from typing import Any


PACKAGE_NAME = "dsh-workbuddy-connect"
PACKAGE_REGISTRY_URL = os.environ.get(
    "WORKBUDDY_CONNECT_NPM_REGISTRY_URL",
    f"https://registry.npmjs.org/{PACKAGE_NAME}",
)
DEFAULT_TIMEOUT_SECONDS = 180
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/workbuddy-connect-build"
PACKAGE_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
# Entry points and peer packages the worker imports.  A staged package that is
# missing one of these cannot drive the library at runtime, so the build fails
# here instead of shipping a Core that reports WorkBuddy as unavailable.
REQUIRED_PACKAGE_FILES = ("package.json", "lib/index.js")
REQUIRED_PEER_DIRECTORIES = (
    "@deepseek-ai/dsh-atomic-write",
    "@deepseek-ai/dsh-home-paths",
    "@earendil-works/pi-ai",
)


class UpdateError(RuntimeError):
    """The third-party package could not be resolved or staged."""


def _request_bytes(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 2:
                break
    raise UpdateError(f"Could not download {url}: {last_error}")


def _package_metadata(registry_url: str) -> tuple[str, str, dict[str, Any]]:
    """Resolve the ``latest`` dist-tag and its tarball from the npm registry."""

    payload = json.loads(_request_bytes(registry_url).decode("utf-8"))
    if not isinstance(payload, dict):
        raise UpdateError("The npm registry returned an invalid document")
    dist_tags = payload.get("dist-tags")
    versions = payload.get("versions")
    if not isinstance(dist_tags, dict) or not isinstance(versions, dict):
        raise UpdateError("The npm registry document has no dist-tags or versions")
    latest = dist_tags.get("latest")
    if not isinstance(latest, str) or not PACKAGE_VERSION_PATTERN.fullmatch(latest):
        raise UpdateError(f"The npm registry reported no usable latest version for {PACKAGE_NAME}")
    version_payload = versions.get(latest)
    if not isinstance(version_payload, dict):
        raise UpdateError(f"The npm registry has no metadata for {PACKAGE_NAME} {latest}")
    dist = version_payload.get("dist")
    tarball = dist.get("tarball") if isinstance(dist, dict) else None
    if not isinstance(tarball, str) or not tarball.startswith(("http://", "https://")):
        raise UpdateError(f"The npm registry reported no tarball for {PACKAGE_NAME} {latest}")
    return latest, tarball, version_payload


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
    raise UpdateError("npm is required to stage the WorkBuddy integration")


def _peer_specs(version_payload: dict[str, Any]) -> list[str]:
    """Install every declared peer; the package entry imports them directly."""

    peers = version_payload.get("peerDependencies")
    if not isinstance(peers, dict):
        return []
    names = [
        f"{name}@{spec}" if isinstance(spec, str) and spec.strip() else str(name)
        for name, spec in peers.items()
    ]
    for name in REQUIRED_PEER_DIRECTORIES:
        if name not in peers:
            names.append(name)
    return names


def _run_npm_install(npm: str, npm_root: Path, package_tarball: Path, peer_specs: list[str]) -> None:
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
        *peer_specs,
    ]
    env = os.environ.copy()
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
        raise UpdateError(f"npm could not install {PACKAGE_NAME}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 2000:
            details = details[-2000:]
        raise UpdateError(
            f"npm could not install {PACKAGE_NAME} and its peer packages"
            + (f": {details}" if details else "")
        )


def _flatten_package(npm_root: Path, destination: Path) -> str:
    package_root = npm_root / "node_modules" / PACKAGE_NAME
    dependencies_root = npm_root / "node_modules"
    package_json = package_root / "package.json"
    if not package_json.is_file():
        raise UpdateError(f"Installed {PACKAGE_NAME} package is missing package.json")
    for relative in REQUIRED_PACKAGE_FILES:
        if not (package_root / relative).is_file():
            raise UpdateError(f"Installed {PACKAGE_NAME} package is missing {relative}")
    for name in REQUIRED_PEER_DIRECTORIES:
        if not (dependencies_root / name).is_dir():
            raise UpdateError(f"Staged {PACKAGE_NAME} is missing the peer package {name}")

    package_payload = destination.parent / f".{destination.name}.staged"
    if package_payload.exists():
        shutil.rmtree(package_payload)
    shutil.copytree(package_root, package_payload, symlinks=True)

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
        raise UpdateError(f"Installed {PACKAGE_NAME} package has an invalid version")

    if destination.exists():
        if not destination.is_dir():
            raise UpdateError(f"{PACKAGE_NAME} output is not a directory: {destination}")
        shutil.rmtree(destination)
    package_payload.rename(destination)
    return version


def update(output: Path, *, registry_url: str, version: str | None = None) -> str:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    latest, tarball_url, version_payload = _package_metadata(registry_url)
    if version is not None:
        if not PACKAGE_VERSION_PATTERN.fullmatch(version):
            raise UpdateError(f"Invalid requested {PACKAGE_NAME} version: {version}")
        latest = version
    npm = _find_executable("WORKBUDDY_CONNECT_NPM_BIN")
    with tempfile.TemporaryDirectory(prefix="young-router-workbuddy-connect-") as directory:
        work = Path(directory)
        package_tarball = work / f"{PACKAGE_NAME}.tgz"
        package_tarball.write_bytes(_request_bytes(tarball_url))
        npm_root = work / "npm"
        _run_npm_install(npm, npm_root, package_tarball, _peer_specs(version_payload))
        package_version = _flatten_package(npm_root, output)
        if package_version != latest:
            raise UpdateError(
                f"npm installed {PACKAGE_NAME} {package_version}, expected {latest}"
            )
    return package_version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Core/young_router/workbuddy-connect destination")
    parser.add_argument("--registry-url", default=PACKAGE_REGISTRY_URL)
    parser.add_argument("--version", help="Stage this exact version instead of the latest dist-tag")
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    try:
        package_version = update(
            Path(options.output),
            registry_url=options.registry_url,
            version=options.version,
        )
    except (OSError, UpdateError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed {PACKAGE_NAME} {package_version} into {Path(options.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
