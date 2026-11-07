#!/usr/bin/env python3
"""Stage the latest ``@dmh2002/dsh-connect-qoder`` package for an artifact build.

Qoder model access is not reimplemented in this repository: the upstream
project (https://github.com/dingminhua/dsh-connect-qoder) owns the cosy signing
protocol, the PAT -> jobToken exchange, both model catalogs, the usage read, and
its own loopback shim.  Its npm distribution is the integration surface, so
every artifact build resolves the ``latest`` dist-tag, installs the package
together with the peers its bundled client imports, and flattens the result
into the Core bundle.  No repository lock is written: a stale adaptation is
never packaged.

The published entry point is a DeepSeek Harness plugin.  The worker this app
ships imports the package's own bundled upstream client, so only the two peer
packages that client imports at runtime are installed -- the Harness client
peers are not needed to drive a LiteLLM route.  The artifact build verifies the
staged surface with Node before packaging.

A deprecated release is a failed build, for the same reason it is in the
WorkBuddy staging script: npm keeps serving a withdrawn version and a build that
only checked the exit status would package it and report success.

``*_URL`` environment variables and the matching command-line options exist for
offline/unit-test fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
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


PACKAGE_NAME = "@dmh2002/dsh-connect-qoder"
PACKAGE_REGISTRY_URL = os.environ.get(
    "CONNECT_QODER_NPM_REGISTRY_URL",
    "https://registry.npmjs.org/@dmh2002%2Fdsh-connect-qoder",
)
DEFAULT_TIMEOUT_SECONDS = 180
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/connect-qoder-build"
# Entry points and peer packages the worker imports.  A staged package that is
# missing one of these cannot drive the upstream client at runtime, so the build
# fails here instead of shipping a Core that reports Qoder as unavailable.
REQUIRED_PACKAGE_FILES = ("package.json", "lib/index.js")
REQUIRED_PEER_DIRECTORIES = (
    "@deepseek-ai/dsh-atomic-write",
    "@deepseek-ai/dsh-home-paths",
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
    return find_npm(name, purpose="stage the Qoder integration")


def _peer_specs(version_payload: dict[str, Any]) -> list[str]:
    """Install the peers the bundled upstream client imports.

    The package declares every DeepSeek Harness surface its *plugin* binds to.
    A LiteLLM route needs none of those: the worker imports the published
    client, whose runtime closure is the two packages below.  Installing the
    whole peer set would put the Harness client UI and React into a Core bundle
    that never loads them, so the declared peers are read only to fill in a
    version when the publish omitted one of these two.
    """

    declared = version_payload.get("peerDependencies")
    declared = declared if isinstance(declared, dict) else {}
    specs: list[str] = []
    for name in REQUIRED_PEER_DIRECTORIES:
        spec = declared.get(name)
        specs.append(f"{name}@{spec}" if isinstance(spec, str) and spec.strip() else name)
    return specs


def _run_npm_install(npm: str, npm_root: Path, package_tarball: Path, peer_specs: list[str]) -> None:
    run_npm_install(
        npm,
        npm_root,
        package_tarball,
        peer_specs,
        package_name=PACKAGE_NAME,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        # The Qoder package's Harness peers are not this worker's runtime:
        # installing their closure would put the client UI in every Core just
        # to delete it below.
        legacy_peer_deps=True,
    )


#: Marks a staged tree that was reduced to the worker's runtime closure.
#: A tree staged by an older script (or a full npm closure) is re-staged once,
#: so a cache that predates this rule cannot keep shipping the whole Harness
#: client.
CLOSURE_MARKER = ".young-router-qoder-runtime-closure"


def _keep_runtime_closure(destination: Path) -> None:
    """Drop everything the Qoder worker never loads.

    The published package's npm closure is the DeepSeek Harness client: the
    plugin entry imports the whole DSH surface, and npm then installs its
    peers' peers.  The worker imports only the bundled cosy/upstream chunk,
    whose runtime closure is the two packages in ``REQUIRED_PEER_DIRECTORIES``
    (proved by the artifact build's Node check).  Staging the rest would put a
    hundred megabytes of a UI this app never renders into every Core.

    The closure is kept by *name*, not by walking imports: a release that adds
    a runtime peer fails the build's Node check rather than silently shipping a
    tree that cannot import, which is the same contract the staging script
    already applies to the package entry.
    """

    # ``node_modules`` holds a scope directory for a scoped peer and a plain
    # directory for an unscoped one.  The kept set is derived from the same
    # tuple that installed them, so a future peer is pruned by the rule that
    # staged it rather than by a second list here.
    keep: dict[str, set[str]] = {}
    for name in REQUIRED_PEER_DIRECTORIES:
        scope, _, package = name.partition("/")
        if package:
            keep.setdefault(scope, set()).add(package)
        else:
            keep.setdefault("", set()).add(scope)
    node_modules = destination / "node_modules"
    if node_modules.is_dir():
        for child in list(node_modules.iterdir()):
            wanted_scope = keep.get(child.name) if child.name.startswith("@") else None
            if wanted_scope is not None and child.is_dir():
                for scoped in list(child.iterdir()):
                    if scoped.name in wanted_scope:
                        continue
                    if scoped.is_dir():
                        shutil.rmtree(scoped)
                    else:
                        scoped.unlink()
                continue
            if not child.name.startswith("@") and child.name in keep.get("", set()):
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    for name in ("bin.js", "client.js"):
        (destination / "lib" / name).unlink(missing_ok=True)
    (destination / CLOSURE_MARKER).write_text("1\n", encoding="utf-8")


def _flatten_package(npm_root: Path, destination: Path) -> str:
    return flatten_npm_package(
        npm_root,
        destination,
        package_name=PACKAGE_NAME,
        required_files=REQUIRED_PACKAGE_FILES,
        required_peer_dirs=REQUIRED_PEER_DIRECTORIES,
        post_stage=_keep_runtime_closure,
    )


def update(output: Path, *, registry_url: str, version: str | None = None) -> str:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not (output / CLOSURE_MARKER).is_file():
        # A cache written before the closure rule (or by a full npm install)
        # must not be reused just because its version still matches.
        shutil.rmtree(output)
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
        required_peer_dirs=REQUIRED_PEER_DIRECTORIES,
    ):
        return latest
    npm = _find_executable("CONNECT_QODER_NPM_BIN")
    with tempfile.TemporaryDirectory(prefix="young-router-connect-qoder-") as directory:
        work = Path(directory)
        package_tarball = work / "connect-qoder.tgz"
        package_tarball.write_bytes(_request_bytes(tarball_url))
        npm_root = work / "npm"
        _run_npm_install(npm, npm_root, package_tarball, _peer_specs(version_payload))
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
    parser.add_argument("--output", required=True, help="Core/young_router/adapters/connect-qoder destination")
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
