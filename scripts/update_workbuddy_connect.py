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
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import (
    PACKAGE_VERSION_PATTERN,
    UpdateError,
    find_npm,
    flatten_npm_package,
    package_metadata,
    request_bytes,
    run_npm_install,
)


PACKAGE_NAME = "dsh-workbuddy-connect"
PACKAGE_REGISTRY_URL = os.environ.get(
    "WORKBUDDY_CONNECT_NPM_REGISTRY_URL",
    f"https://registry.npmjs.org/{PACKAGE_NAME}",
)
DEFAULT_TIMEOUT_SECONDS = 180
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/workbuddy-connect-build"
# Entry points and peer packages the worker imports.  A staged package that is
# missing one of these cannot drive the library at runtime, so the build fails
# here instead of shipping a Core that reports WorkBuddy as unavailable.
REQUIRED_PACKAGE_FILES = ("package.json", "lib/index.js")
REQUIRED_PEER_DIRECTORIES = (
    "@deepseek-ai/dsh-atomic-write",
    "@deepseek-ai/dsh-home-paths",
    "@earendil-works/pi-ai",
)


def _request_bytes(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    return request_bytes(url, timeout=timeout, user_agent=USER_AGENT)


def _package_metadata(registry_url: str) -> tuple[str, str, dict[str, Any]]:
    """Resolve the ``latest`` dist-tag and its tarball from the npm registry."""

    return package_metadata(
        registry_url,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        user_agent=USER_AGENT,
    )


def _find_executable(name: str) -> str:
    return find_npm(name, purpose="stage the WorkBuddy integration")


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
    run_npm_install(
        npm,
        npm_root,
        package_tarball,
        peer_specs,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def _flatten_package(npm_root: Path, destination: Path) -> str:
    return flatten_npm_package(
        npm_root,
        destination,
        package_name=PACKAGE_NAME,
        required_files=REQUIRED_PACKAGE_FILES,
        required_peer_dirs=REQUIRED_PEER_DIRECTORIES,
    )


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
    parser.add_argument("--output", required=True, help="Core/young_router/adapters/workbuddy-connect destination")
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
