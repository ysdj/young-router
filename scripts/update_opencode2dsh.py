#!/usr/bin/env python3
"""Stage dsh-our-free-model without altering its OpenCode integration.

The existing Young Router provider reuses the upstream project's runtime and
OpenCode protocol. Its source is copied intact into the Core adapter tree; only
Young Router's process bridge lives outside that tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
import sys
import shutil
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import (
    PACKAGE_VERSION_PATTERN,
    UpdateError,
    STAGED_RELEASE_MARKER,
    copy_tree,
    find_npm,
    record_staged_release,
    reused_staged_release,
    npm_dependency_manifest,
)


PACKAGE_NAME = "dsh-our-free-model"
SOURCE_URL = os.environ.get(
    "OUR_FREE_MODEL_SOURCE_URL",
    "https://github.com/Ebony-Vinyl/dsh-our-free-model.git",
)
DEFAULT_TIMEOUT_SECONDS = 180
REQUIRED_FILES = (
    "package.json",
    "index.js",
    "src/adapter.js",
    "src/core/runtime.js",
    "src/forward.js",
    "src/providers/registry.json",
    "vendor/channel-pack/pack.js",
    "LICENSE",
)
REQUIRED_PEERS = (
    "@deepseek-ai/cordis",
    "@deepseek-ai/dsh-credentials",
    "@deepseek-ai/dsh-llm",
    "@deepseek-ai/schemastery",
)


def _install_peers(command: list[str], *, npm_root: Path) -> None:
    npm_dependency_manifest(
        npm_root,
        [
            f"{name}@{spec}"
            for name, spec in json.loads(
                (npm_root.parent / "payload" / "package.json").read_text(encoding="utf-8")
            )["peerDependencies"].items()
        ],
    )
    install = subprocess.run(
        [
            *command,
            "install",
            "--prefix",
            str(npm_root),
            "--no-package-lock",
            "--ignore-scripts",
            "--omit=dev",
            "--fund=false",
            "--audit=false",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SECONDS * 2,
        check=False,
    )
    if install.returncode != 0:
        details = (install.stderr or install.stdout).strip()[-2000:]
        raise UpdateError(
            f"npm could not install {PACKAGE_NAME} peers"
            + (f": {details}" if details else "")
        )


def _content_digest(root: Path, *, omit_nodes: bool = False) -> str:
    """Identify the upstream source payload before peers are installed."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.as_posix() == STAGED_RELEASE_MARKER:
            continue
        if omit_nodes and "node_modules" in relative.parts:
            continue
        if path.is_file() and not path.is_symlink():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return f"sha256-{digest.hexdigest()}"


def update(output: Path, *, source_url: str, ref: str = "main") -> str:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="young-router-our-free-model-") as directory:
        work = Path(directory)
        checkout = work / "source"
        if source_url.startswith("file://"):
            shutil.copytree(Path(source_url[7:]), checkout, symlinks=True)
        else:
            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    ref,
                    source_url,
                    str(checkout),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                raise UpdateError(detail[-1] if detail else f"Could not clone {PACKAGE_NAME}")
        package_file = checkout / "package.json"
        for relative in REQUIRED_FILES:
            if not (checkout / relative).is_file():
                raise UpdateError(f"{PACKAGE_NAME} source is missing {relative}")
        package = json.loads(package_file.read_text(encoding="utf-8"))
        peers = package.get("peerDependencies")
        if not isinstance(peers, dict):
            raise UpdateError(f"{PACKAGE_NAME} source declares no peers")
        for name in REQUIRED_PEERS:
            if name not in peers:
                raise UpdateError(f"{PACKAGE_NAME} source no longer declares the peer {name}")
        version = package.get("version")
        if not isinstance(version, str) or not PACKAGE_VERSION_PATTERN.fullmatch(version):
            raise UpdateError(f"{PACKAGE_NAME} source has an invalid version")

        payload = work / "payload"
        copy_tree(checkout, payload, package_name=PACKAGE_NAME)
        shutil.rmtree(payload / ".git", ignore_errors=True)
        # Install the declared peer closure beside the unchanged upstream files.
        npm_root = work / "npm"
        _install_peers(
            [find_npm("OUR_FREE_MODEL_NPM_BIN", purpose="stage the free-model integration")],
            npm_root=npm_root,
        )
        dependency_root = npm_root / "node_modules"
        for child in dependency_root.iterdir():
            if child.name.startswith("."):
                continue
            target = payload / "node_modules" / child.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(child, target, symlinks=True)
        for name in REQUIRED_PEERS:
            if not (payload / "node_modules" / name).is_dir():
                raise UpdateError(f"Staged {PACKAGE_NAME} is missing the peer package {name}")
        upstream_digest = _content_digest(payload, omit_nodes=True)
        reused = reused_staged_release(
            output,
            package_name=PACKAGE_NAME,
            version=version,
            integrity=upstream_digest,
            required_files=REQUIRED_FILES,
            required_peer_dirs=REQUIRED_PEERS,
        )
        if reused:
            return version
        staging = output.parent / f".{output.name}.staged"
        if staging.exists():
            shutil.rmtree(staging)
        payload.rename(staging)
        if output.exists():
            shutil.rmtree(output)
        staging.rename(output)
        record_staged_release(
            output,
            package_name=PACKAGE_NAME,
            version=version,
            integrity=upstream_digest,
        )
        return version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, help="Core/young_router/adapters/opencode2dsh destination"
    )
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--ref", default="main")
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    try:
        tag = update(
            Path(options.output),
            source_url=options.source_url,
            ref=options.ref,
        )
    except (OSError, UpdateError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed {PACKAGE_NAME} {tag} into {Path(options.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
