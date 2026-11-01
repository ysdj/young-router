#!/usr/bin/env python3
"""Stage the Bun executable the Our Free Model free lane dials through.

The staged upstream moved its official OpenCode inference lane onto Bun: it
spawns one short-lived Bun child per request and refuses every row on that lane
-- the model listing included -- when it cannot find a native Bun executable.
That is not a package the app can leave to the user's ``PATH``: the installed
app is launched by Finder, which supplies the login shell's environment to
neither the app nor the Core it starts, so a host that installed Bun itself
still fails from the app window.

This stager downloads the platform's prebuilt release from the npm registry and
stages it **beside the bundled Node runtime** (``Core/runtime/bin``), which is
the one directory :func:`young_router.node_runtime.bundled_executable` looks in.
Nothing is installed into the user's home or onto ``PATH``, and no version is
pinned here: the registry's ``latest`` dist-tag is resolved on every artifact
build, exactly as the other staged integrations resolve theirs, so a fix
upstream reaches the next build instead of waiting for this file to move.

Bun is MIT licensed; its ``LICENSE`` is staged beside the executable so a
release bundle carries the notice of the binary it ships.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tarfile

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import (
    UpdateError,
    download_bytes,
    package_metadata,
    request_json,
)


PACKAGE_NAME = "@oven/bun-{platform}"
#: The registry document per platform package: the executable's tarball is
#: resolved from its own version, because the platform packages are released
#: under the same number as the umbrella ``bun`` package but are published
#: separately and can lag it.
REGISTRY_BASE = os.environ.get(
    "YOUNG_ROUTER_BUN_REGISTRY_BASE", "https://registry.npmjs.org"
)
DEFAULT_TIMEOUT_SECONDS = 300
USER_AGENT = "Young-Router/bun-runtime-build"
#: The distributed binary is ~90 MiB; anything far smaller is an error page or a
#: truncated download, and shipping it would fail on the first request instead
#: of here.
MINIMUM_BYTES = 16 * 1024 * 1024
LICENSE_URL = "https://raw.githubusercontent.com/oven-sh/bun/main/LICENSE.md"


def _platform_package() -> tuple[str, str]:
    """The registry package and the executable it ships for this host."""

    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = "aarch64" if machine in {"arm64", "aarch64"} else "x64"
    if system == "darwin" and machine in {"arm64", "aarch64", "x86_64", "amd64"}:
        return f"@oven/bun-darwin-{architecture}", "bun"
    if system == "windows" and machine in {"arm64", "aarch64", "amd64", "x86_64"}:
        return f"@oven/bun-windows-{architecture}", "bun.exe"
    raise UpdateError(f"The Bun runtime is not staged for {platform.system()} {machine}")


def _registry_url(package: str) -> str:
    return f"{REGISTRY_BASE.rstrip('/')}/{package.replace('/', '%2f')}"


def _extract_executable(archive: bytes, executable: str) -> bytes:
    """Read the one binary out of a Bun platform tarball."""

    wanted = f"/bin/{executable}"
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as handle:
            for member in handle.getmembers():
                if not member.isfile() or not member.name.endswith(wanted):
                    continue
                extracted = handle.extractfile(member)
                if extracted is not None:
                    return extracted.read()
    except (OSError, tarfile.TarError) as exc:
        raise UpdateError("Downloaded Bun archive is not a valid tarball") from exc
    raise UpdateError(f"Downloaded Bun archive does not contain {executable}")


def _license_bytes() -> bytes:
    """Bun's licence text, staged beside the binary it covers.

    A build with no network still stages the executable it resolved; the notice
    is the only piece that can be skipped, and skipping it silently would ship a
    binary without its licence, so the failure is reported instead.
    """

    try:
        text = download_bytes(
            LICENSE_URL, timeout=DEFAULT_TIMEOUT_SECONDS, user_agent=USER_AGENT
        )
    except UpdateError:
        raise
    if not text:
        raise UpdateError("Downloaded Bun licence is empty")
    return text


def _write(target: Path, payload: bytes, *, executable: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.staged")
    temporary.write_bytes(payload)
    if executable:
        temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    target.unlink(missing_ok=True)
    temporary.replace(target)


def update(output: Path, *, license_path: Path | None = None) -> str:
    """Stage the current Bun release for this host into ``output``.

    A repeat build compares the resolved release against what is already staged
    and writes nothing when they match, so an unchanged Bun never moves the
    runtime directory's mtime -- the same rule the Node stager follows: a build
    that packs the Core would otherwise see the runtime as changed every run.
    """

    output = output.expanduser().resolve()
    package, executable = _platform_package()
    version, tarball, _payload = package_metadata(
        _registry_url(package),
        package_name=package,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        user_agent=USER_AGENT,
    )
    target = output / executable
    marker = output / "BUN_VERSION"
    if (
        target.is_file()
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == version
    ):
        return version
    archive = download_bytes(
        tarball, timeout=DEFAULT_TIMEOUT_SECONDS, user_agent=USER_AGENT
    )
    binary = _extract_executable(archive, executable)
    if len(binary) < MINIMUM_BYTES:
        raise UpdateError(
            f"Downloaded Bun executable is unexpectedly small ({len(binary)} bytes)"
        )
    _write(target, binary, executable=True)
    _write(marker, f"{version}\n".encode("utf-8"), executable=False)
    licence = (
        Path(license_path).expanduser().read_bytes()
        if license_path is not None
        else _license_bytes()
    )
    _write(output / "BUN_LICENSE", licence, executable=False)
    return version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, help="Directory receiving bun/bun.exe and its licence"
    )
    parser.add_argument(
        "--license",
        default=None,
        help="An existing Bun licence to stage instead of downloading one",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str]) -> int:
    options = parse_arguments(arguments)
    try:
        version = update(
            Path(options.output),
            license_path=Path(options.license) if options.license else None,
        )
    except (OSError, UpdateError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed Bun {version} runtime into {Path(options.output).expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
