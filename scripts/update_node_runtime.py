#!/usr/bin/env python3
"""Stage the Node 22 runtime an artifact build ships.

Three bundled integrations run JavaScript workers, so the desktop Core needs a
Node of its own; a user's global installation is not a dependency it may rely
on.  This stager copies the Node the installed bundle already runs when that is
possible, and otherwise downloads the newest Node 22 archive for the build
host.

It is separate from the package stagers because it answers a different
question: the runtime is not a release to re-check against a registry, and a
build that skips it ships an app whose native workers cannot start.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tarfile
import zipfile
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import UpdateError, download_bytes, request_json


NODE_INDEX_URL = os.environ.get(
    "YOUNG_ROUTER_NODE_INDEX_URL",
    "https://nodejs.org/dist/index.json",
)
NODE_ARCHIVE_BASE_URL = os.environ.get(
    "YOUNG_ROUTER_NODE_ARCHIVE_BASE_URL",
    "https://nodejs.org/dist",
)
DEFAULT_TIMEOUT_SECONDS = 120
# Build-tool metadata lookup identity: this script runs on a maintainer's
# machine, not in the shipped app.
USER_AGENT = "Young-Router/node-runtime-build"
NODE_VERSION_PATTERN = re.compile(r"^v22\.[0-9]+\.[0-9]+$")


def _request_bytes(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    return download_bytes(url, timeout=timeout, user_agent=USER_AGENT)


def _request_json(url: str) -> Any:
    return request_json(url, timeout=DEFAULT_TIMEOUT_SECONDS, user_agent=USER_AGENT)


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
    target = node_output / executable
    if source:
        node_source = Path(source).expanduser()
        # A repeat build stages the same Node binary from the installed bundle
        # every run, and writing 90 MB only moves the output's mtime: whatever
        # packs the Core would then see the runtime as changed on every build.
        # Compare by size and content first, so an unchanged source leaves the
        # staged file exactly as it is.
        node_bytes = _copy_node_source(node_source)
        if target.is_file() and target.stat().st_size == len(node_bytes) \
           and target.read_bytes() == node_bytes:
            return "copied"
        version = "copied"
    else:
        version, filename, executable = _node_release(index_url)
        target = node_output / executable
        if archive_url:
            url = archive_url
        else:
            url = f"{NODE_ARCHIVE_BASE_URL.rstrip('/')}/{version}/{filename}"
        node_bytes = _extract_node_bytes(_request_bytes(url, timeout=DEFAULT_TIMEOUT_SECONDS * 2), filename, executable)
    if len(node_bytes) < 1024 * 1024:
        raise UpdateError("Downloaded Node executable is unexpectedly small")
    node_output.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.staged")
    temporary.write_bytes(node_bytes)
    if executable == "node":
        temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if target.exists():
        target.unlink()
    temporary.replace(target)
    return version



def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Directory receiving node or node.exe")
    parser.add_argument("--node-source", help="Existing Node 22 executable or distribution directory")
    parser.add_argument("--index-url", default=NODE_INDEX_URL)
    parser.add_argument("--archive-url", default=os.environ.get("YOUNG_ROUTER_NODE_ARCHIVE_URL"))
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    try:
        version = _install_node(
            Path(options.output).expanduser().resolve(),
            source=options.node_source,
            index_url=options.index_url,
            archive_url=options.archive_url,
        )
    except (OSError, UpdateError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed Node 22 runtime ({version}) into {Path(options.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
