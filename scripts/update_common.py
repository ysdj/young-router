#!/usr/bin/env python3
"""Shared npm-registry staging helpers for the third-party update scripts.

``update_pi_web_access.py``, ``update_workbuddy_connect.py``, and
``update_dsh_vision_router.py`` resolve an npm ``latest`` dist-tag, download the
tarball, install it without touching a lockfile, and flatten the installed
package into a Core directory.  The mechanics live here once; each script keeps
only what is particular to its package (its peer rules and any staged rewrite).

This file is imported by sibling scripts, so it must stay dependency-free
beyond the standard library.
"""

from __future__ import annotations

import http.client
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Iterator

#: One npm ``latest`` lookup is retried a few times: the build machine's
#: network is the only thing between a release and its third-party package.
DOWNLOAD_ATTEMPTS = 3
PACKAGE_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class UpdateError(RuntimeError):
    """A build dependency could not be resolved or staged."""


def request_bytes(url: str, *, timeout: int, user_agent: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": "*/*", "User-Agent": user_agent}
    )
    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (http.client.IncompleteRead, OSError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == DOWNLOAD_ATTEMPTS - 1:
                break
            time.sleep(0.5 * (attempt + 1))
    raise UpdateError(f"Could not download {url}: {last_error}") from last_error


def request_json(url: str, *, timeout: int, user_agent: str) -> Any:
    raw = request_bytes(url, timeout=timeout, user_agent=user_agent)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Downloaded metadata from {url} is not valid JSON") from exc


def package_metadata(
    registry_url: str,
    *,
    package_name: str,
    timeout: int,
    user_agent: str,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve the ``latest`` dist-tag and its tarball from the npm registry."""

    payload = request_json(registry_url, timeout=timeout, user_agent=user_agent)
    if not isinstance(payload, dict):
        raise UpdateError(f"npm registry returned an invalid {package_name} metadata object")
    dist_tags = payload.get("dist-tags")
    latest = dist_tags.get("latest") if isinstance(dist_tags, dict) else None
    if not isinstance(latest, str) or not PACKAGE_VERSION_PATTERN.fullmatch(latest):
        raise UpdateError(f"npm registry did not return a stable {package_name} latest version")
    versions = payload.get("versions")
    version_payload = versions.get(latest) if isinstance(versions, dict) else None
    if not isinstance(version_payload, dict):
        raise UpdateError(f"npm registry metadata is missing {package_name} {latest}")
    dist = version_payload.get("dist")
    tarball = dist.get("tarball") if isinstance(dist, dict) else None
    if not isinstance(tarball, str) or not tarball.startswith(("http://", "https://")):
        raise UpdateError(f"npm registry metadata is missing the {package_name} {latest} tarball")
    return latest, tarball, version_payload


def find_package_manager(
    env_name: str, *, purpose: str
) -> tuple[str, dict[str, str]]:
    """Return the staged-install tool and the environment it runs with.

    pnpm is preferred: it resolves the same registry graph an order of magnitude
    faster, which is most of the artifact build's third-party staging time.  It
    defaults to the ``hoisted`` node linker so the install tree is a flat
    ``node_modules`` exactly as npm produces one — ``flatten_npm_package`` and
    every staged worker then resolve peers by plain directory lookup.  npm stays
    the fallback so a machine without pnpm still builds.
    """

    configured = os.environ.get(env_name, "").strip()
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise UpdateError(f"Configured {env_name} does not point to an executable: {configured}")
        manager, name = str(path), path.name.lower()
    else:
        manager, name = "", ""
        for candidate in _manager_candidates():
            resolved = shutil.which(candidate)
            if resolved:
                manager, name = resolved, candidate.lower()
                break
        if not manager:
            raise UpdateError(f"pnpm or npm is required to {purpose}")
    env = os.environ.copy()
    env.setdefault("NPM_CONFIG_UPDATE_NOTIFIER", "false")
    if _is_pnpm(name):
        env["npm_config_node_linker"] = "hoisted"
    return manager, env


def _manager_candidates() -> list[str]:
    windows = os.name == "nt"
    return (
        ["pnpm.cmd", "pnpm", "npm.cmd", "npm"]
        if windows
        else ["pnpm", "npm"]
    )


def _is_pnpm(name: str) -> bool:
    return "pnpm" in name.lower()


def run_package_manager_install(
    manager: str,
    manager_env: dict[str, str],
    npm_root: Path,
    package_tarball: Path,
    peer_specs: Iterable[str],
    *,
    package_name: str,
    timeout: int,
) -> None:
    """Install one published tarball and its peers into a throwaway tree.

    pnpm and npm are driven differently: pnpm needs a real project directory
    before it will install anything, and it auto-installs peers unless that is
    switched off.  Auto-install is disabled because a peer can pull an
    unpublished package into the graph, which then fails the whole staging step
    instead of being staged deliberately by the caller.
    """

    if _is_pnpm(manager):
        npm_root.mkdir(parents=True, exist_ok=True)
        manifest = npm_root / "package.json"
        if not manifest.exists():
            manifest.write_text(
                json.dumps({"name": "young-router-staging", "version": "0.0.0", "private": True})
                + "\n",
                encoding="utf-8",
            )
        command = [
            manager,
            "add",
            "--config.node-linker=hoisted",
            "--config.auto-install-peers=false",
            "--config.ignore-scripts=true",
            "--config.update-notifier=false",
            "--reporter=silent",
            str(package_tarball),
            *peer_specs,
        ]
        working = npm_root
        label = "pnpm"
    else:
        npm_root.mkdir(parents=True, exist_ok=True)
        command = [
            manager,
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
        working = npm_root
        label = "npm"
    use_shell = os.name == "nt" and manager.lower().endswith((".cmd", ".bat"))
    try:
        result = subprocess.run(
            command,
            cwd=str(working),
            env=manager_env,
            text=True,
            capture_output=True,
            timeout=timeout * 2,
            check=False,
            shell=use_shell,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"{label} could not install {package_name}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 2000:
            details = details[-2000:]
        raise UpdateError(
            f"{label} could not install {package_name} and its peer packages"
            + (f": {details}" if details else "")
        )


@contextmanager
def temporary_install_tree(prefix: str) -> Iterator[Path]:
    """A scratch directory for one staging run, removed however it ends."""

    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        yield Path(directory)


def find_npm(env_name: str, *, purpose: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        raise UpdateError(f"Configured {env_name} does not point to an executable: {configured}")
    candidates = ["npm.cmd", "npm"] if os.name == "nt" else ["npm"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise UpdateError(f"npm is required to {purpose}")


def run_npm_install(
    npm: str,
    npm_root: Path,
    package_tarball: Path,
    peer_specs: Iterable[str],
    *,
    package_name: str,
    timeout: int,
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
        *peer_specs,
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
            timeout=timeout * 2,
            check=False,
            shell=use_shell,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"npm could not install {package_name}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 2000:
            details = details[-2000:]
        raise UpdateError(
            f"npm could not install {package_name} and its peer packages"
            + (f": {details}" if details else "")
        )


def copy_tree(source: Path, destination: Path, *, package_name: str) -> None:
    if not source.is_dir():
        raise UpdateError(f"Expected {package_name} directory is missing: {source}")
    shutil.copytree(source, destination, symlinks=True)


def flatten_npm_package(
    npm_root: Path,
    destination: Path,
    *,
    package_name: str,
    required_files: Iterable[str],
    required_peer_dirs: Iterable[str] = (),
    post_stage: Callable[[Path], None] | None = None,
) -> str:
    """Move one installed npm package into ``destination`` with its peers.

    A published package may ship a nested ``node_modules`` of its own (npm
    installs a dependency there when the flat tree carries another version of
    it).  That copy is the one the package's own code resolved against, so it
    stays exactly as published and the peer closure the worker needs is merged
    beside it — never over it.
    """

    package_root = npm_root / "node_modules" / package_name
    dependencies_root = npm_root / "node_modules"
    package_json = package_root / "package.json"
    for relative in required_files:
        if not (package_root / relative).is_file():
            raise UpdateError(f"Installed {package_name} package is missing {relative}")
    for name in required_peer_dirs:
        if not (dependencies_root / name).is_dir():
            raise UpdateError(f"Staged {package_name} is missing the peer package {name}")

    package_payload = destination.parent / f".{destination.name}.staged"
    if package_payload.exists():
        shutil.rmtree(package_payload)
    copy_tree(package_root, package_payload, package_name=package_name)

    dependency_payload = package_payload / "node_modules"
    dependency_payload.mkdir(exist_ok=True)
    for child in dependencies_root.iterdir():
        if child.name in {package_name, ".bin"}:
            continue
        target = dependency_payload / child.name
        if target.exists() or target.is_symlink():
            continue
        if child.is_symlink():
            target.symlink_to(os.readlink(child))
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)

    version_data = json.loads(package_json.read_text(encoding="utf-8"))
    version = version_data.get("version") if isinstance(version_data, dict) else None
    if not isinstance(version, str) or not PACKAGE_VERSION_PATTERN.fullmatch(version):
        raise UpdateError(f"Installed {package_name} package has an invalid version")

    if post_stage is not None:
        post_stage(package_payload)

    if destination.exists():
        if not destination.is_dir():
            raise UpdateError(f"{package_name} output is not a directory: {destination}")
        shutil.rmtree(destination)
    package_payload.rename(destination)
    return version


__all__ = [
    "PACKAGE_VERSION_PATTERN",
    "UpdateError",
    "copy_tree",
    "find_npm",
    "find_package_manager",
    "flatten_npm_package",
    "package_metadata",
    "request_bytes",
    "request_json",
    "run_npm_install",
    "run_package_manager_install",
    "temporary_install_tree",
]
