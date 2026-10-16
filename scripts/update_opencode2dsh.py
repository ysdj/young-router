#!/usr/bin/env python3
"""Stage the latest ``opencode2dsh`` plugin for an artifact build.

The upstream project (https://github.com/FishBottle7/opencode2dsh) owns the
OpenCode Zen anonymous lane: the free-model catalog and its fallback chain, the
CLI-identical request disguise, the session/request id derivation, and the
``pi-ai`` wire layer that streams from Zen.  Its npm distribution is the
integration surface, so every artifact build resolves the ``latest`` dist-tag,
installs the package together with the dependencies it declares, and flattens
the result into the Core bundle.  No repository lock is written: a stale
adaptation is never packaged.

The published artifact is the plugin package itself
(``@opencode2dsh/dsh-plugin``); the repository root is a private workspace whose
own ``dependencies`` pin it, and it publishes no ``lib/`` of its own.  This
script therefore resolves the plugin package directly rather than the
workspace root.

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
    record_staged_release,
    release_integrity,
    request_bytes,
    reused_staged_release,
    run_npm_install,
)


PACKAGE_NAME = "@opencode2dsh/dsh-plugin"
PACKAGE_REGISTRY_URL = os.environ.get(
    "OPENCODE2DSH_NPM_REGISTRY_URL",
    f"https://registry.npmjs.org/{PACKAGE_NAME}",
)
DEFAULT_TIMEOUT_SECONDS = 180
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/opencode2dsh-build"
# Entry point the worker imports, plus the module-level names it reads off that
# entry.  A staged package missing one of these cannot drive the library at
# runtime, so the build fails here instead of shipping a Core that reports the
# OpenCode Zen lane as unavailable.
REQUIRED_PACKAGE_FILES = ("package.json", "lib/index.js")
# The plugin's own runtime dependencies (``dependencies`` in its manifest), all
# published.  They are installed explicitly so the flatten step always lands
# them beside the package, whatever the package manager decides to hoist.
REQUIRED_DEPENDENCY_DIRECTORIES = (
    "@deepseek-ai/schemastery",
    "@earendil-works/pi-ai",
    "undici",
    "yaml",
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
    return find_npm(name, purpose="stage the opencode2dsh integration")


def _dependency_specs(version_payload: dict[str, Any]) -> list[str]:
    """Install the plugin's declared dependencies and the adapter's peers."""

    dependencies = version_payload.get("dependencies")
    if not isinstance(dependencies, dict):
        raise UpdateError(f"{PACKAGE_NAME} publishes no dependency list to stage")
    names = [
        f"{name}@{spec}" if isinstance(spec, str) and spec.strip() else str(name)
        for name, spec in dependencies.items()
    ]
    for name in REQUIRED_DEPENDENCY_DIRECTORIES:
        if name not in dependencies:
            names.append(name)
    return names


def _run_npm_install(
    npm: str, npm_root: Path, package_tarball: Path, dependency_specs: list[str]
) -> None:
    run_npm_install(
        npm,
        npm_root,
        package_tarball,
        dependency_specs,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def _flatten_package(npm_root: Path, destination: Path) -> str:
    return flatten_npm_package(
        npm_root,
        destination,
        package_name=PACKAGE_NAME,
        required_files=REQUIRED_PACKAGE_FILES,
        required_peer_dirs=REQUIRED_DEPENDENCY_DIRECTORIES,
    )


def update(output: Path, *, registry_url: str, version: str | None = None) -> str:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    latest, tarball_url, version_payload = _package_metadata(registry_url)
    if version is not None:
        if not PACKAGE_VERSION_PATTERN.fullmatch(version):
            raise UpdateError(f"Invalid requested {PACKAGE_NAME} version: {version}")
        latest = version
    integrity = release_integrity(version_payload)
    # The release was resolved above on this build; this only skips installing
    # the identical one again.  See ``reused_staged_release``.
    if reused_staged_release(
        output,
        package_name=PACKAGE_NAME,
        version=latest,
        integrity=integrity,
        required_files=REQUIRED_PACKAGE_FILES,
        required_peer_dirs=REQUIRED_DEPENDENCY_DIRECTORIES,
    ):
        return latest
    npm = _find_executable("OPENCODE2DSH_NPM_BIN")
    with tempfile.TemporaryDirectory(prefix="young-router-opencode2dsh-") as directory:
        work = Path(directory)
        package_tarball = work / "opencode2dsh-plugin.tgz"
        package_tarball.write_bytes(_request_bytes(tarball_url))
        npm_root = work / "npm"
        _run_npm_install(npm, npm_root, package_tarball, _dependency_specs(version_payload))
        package_version = _flatten_package(npm_root, output)
        if package_version != latest:
            raise UpdateError(
                f"npm installed {PACKAGE_NAME} {package_version}, expected {latest}"
            )
        record_staged_release(
            output, package_name=PACKAGE_NAME, version=latest, integrity=integrity
        )
    return package_version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, help="Core/young_router/adapters/opencode2dsh destination"
    )
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
