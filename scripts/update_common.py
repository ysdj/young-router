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
from contextlib import contextmanager, suppress
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Iterator

#: One npm ``latest`` lookup is retried a few times: the build machine's
#: network is the only thing between a release and its third-party package.
DOWNLOAD_ATTEMPTS = 3
PACKAGE_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")

#: Written into a staged npm package by the script that staged it: the release
#: it was installed from.  Reusing a staged tree is decided by this record, so a
#: directory that cannot prove which release it holds is reinstalled instead.
STAGED_RELEASE_MARKER = "young-router-staged-release.json"


class UpdateError(RuntimeError):
    """A build dependency could not be resolved or staged."""


#: One pooled HTTPS connection per thread, reused across a staging script's
#: downloads.  A run that fetches a release index and then a tarball pays one
#: DNS lookup and one TLS handshake instead of one of each per file, which on
#: this build machine is several seconds of wall clock per skipped handshake.
#: Thread-local keeps the parallel staging jobs from sharing one socket, and a
#: connection that a server retired is simply replaced on the next request.
_DOWNLOAD_CONNECTIONS: threading.local = threading.local()


def _open_connection(origin: str, timeout: int) -> http.client.HTTPSConnection:
    """The one place a staged download opens a connection.

    Kept as its own function so a test can substitute a local server without
    rebinding ``http.client.HTTPSConnection``: that name is resolved by CPython
    itself while connecting, so patching it would redirect the standard library
    rather than this module.
    """

    return http.client.HTTPSConnection(origin, timeout=timeout)


def _pooled_connection(origin: str, timeout: int) -> http.client.HTTPSConnection:
    connections = getattr(_DOWNLOAD_CONNECTIONS, "connections", None)
    if connections is None:
        connections = {}
        _DOWNLOAD_CONNECTIONS.connections = connections
    connection = connections.get(origin)
    if connection is None:
        connection = _open_connection(origin, timeout)
        connections[origin] = connection
    return connection


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


def download_bytes(url: str, *, timeout: int, user_agent: str) -> bytes:
    """Download ``url`` over the pooled connection, falling back to urllib.

    The connection is reused for the rest of the staging run, so a run that
    fetches an index and then a tarball does one TLS handshake rather than two.
    ``Connection: keep-alive`` is deliberately not sent: HTTP/1.1 defaults to
    persistent, which is what makes the reuse possible, and the header would
    only be a redundant thing for the origin to parse.

    Every connection is established with its own ``connect()`` before being
    cached, so a request that fails mid-flight leaves no half-open socket to
    reuse: the failure falls back to ``request_bytes``' own fresh-connection
    retry ladder below, which is also the path for a non-HTTPS URL.
    """

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return request_bytes(url, timeout=timeout, user_agent=user_agent)
    origin = parsed.netloc
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    headers = {"Accept": "*/*", "User-Agent": user_agent}
    try:
        connection = _pooled_connection(origin, timeout)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            # A 3xx must go back through urllib: the pooled connection does not
            # follow redirects, and silently returning an error body would be
            # worse than the second handshake.
            with suppress(Exception):
                connection.close()
            return request_bytes(url, timeout=timeout, user_agent=user_agent)
        return response.read()
    except (http.client.HTTPException, OSError, TimeoutError):
        # A pooled connection a server retired is replaced, not re-requested
        # on: the caller gets urllib's full retry ladder on a fresh socket.
        with suppress(Exception):
            connection.close()
        return request_bytes(url, timeout=timeout, user_agent=user_agent)


def request_json(url: str, *, timeout: int, user_agent: str) -> Any:
    raw = download_bytes(url, timeout=timeout, user_agent=user_agent)
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


def reused_staged_release(
    destination: Path,
    *,
    package_name: str,
    version: str,
    integrity: str | None,
    required_files: Iterable[str] = (),
    required_peer_dirs: Iterable[str] = (),
) -> bool:
    """Is the tree at ``destination`` the release this build just resolved?

    The build resolves every staged integration against its upstream release
    before packaging it, and that resolution costs about two seconds while
    reinstalling an unchanged npm graph costs thirty.  Resolving a release is
    not the same work as installing one, so the release is still resolved on
    every build and only the redundant install is skipped.

    What makes the skip safe is provenance rather than trust: a tree staged by
    this script records the release it came from, and it is reused only when
    that record names the exact package, version, and registry digest the
    lookup above just resolved.  A changed release, a tree staged before this
    record existed, a partial tree from an interrupted run, or one whose entry
    points or peers have gone missing all fail this test and are staged from
    scratch.
    """

    try:
        document = json.loads((destination / STAGED_RELEASE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    if document.get("package") != package_name or document.get("version") != version:
        return False
    # A registry that published no digest for this release cannot be matched
    # against a recorded one, so the release is staged rather than assumed.
    if integrity is None or document.get("integrity") != integrity:
        return False
    for relative in required_files:
        if not (destination / relative).is_file():
            return False
    for name in required_peer_dirs:
        if not (destination / "node_modules" / name).is_dir():
            return False
    return True


def record_staged_release(
    destination: Path,
    *,
    package_name: str,
    version: str,
    integrity: str | None,
) -> None:
    """Record which published release the freshly staged tree came from.

    Written last, after the staging tree is in place, so an interrupted install
    leaves no record and cannot be mistaken for a complete one.
    """

    document = {
        "package": package_name,
        "version": version,
        "integrity": integrity,
    }
    (destination / STAGED_RELEASE_MARKER).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def release_integrity(version_payload: dict[str, Any]) -> str | None:
    """The registry's own digest for a resolved release, when it published one."""

    dist = version_payload.get("dist")
    integrity = dist.get("integrity") if isinstance(dist, dict) else None
    return integrity if isinstance(integrity, str) and integrity else None



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


def npm_dependency_manifest(npm_root: Path, peer_specs: Iterable[str]) -> None:
    """Write the peer specs into a manifest instead of onto the command line.

    A peer spec carries npm's own version range, so it contains ``^``
    (``@scope/name@^4.0.2``).  On Windows the resolved executable is
    ``npm.cmd``, which Python can only launch through the shell, and ``cmd.exe``
    reads a bare ``^`` as its escape character and drops it.  npm then receives
    the exact version ``4.0.2`` instead of the range and refuses the install
    because a sibling peer requires ``~4.0.4`` -- the whole staging step fails
    on one character that never reaches npm on macOS or Linux, where no shell
    is involved:

        npm error Found: @deepseek-ai/cordis@4.0.2
        npm error   @deepseek-ai/cordis@"4.0.2" from the root project
        npm error peer @deepseek-ai/cordis@"~4.0.4" from @deepseek-ai/dsh-llm@0.2.0-rc.2

    Naming the peers in ``package.json`` takes them off the command line, so no
    shell can rewrite them and the same call behaves identically everywhere.
    The tarball stays an argument: it is one path this script just created,
    with no range and no shell metacharacter in it.  The manifest is written
    rather than merged because this installs into a fresh scratch directory.
    """

    npm_root.mkdir(parents=True, exist_ok=True)
    dependencies: dict[str, str] = {}
    for spec in peer_specs:
        # ``@scope/name@range`` and ``name@range``; rpartition keeps the range
        # text, so npm resolves exactly what the registry declared.
        name, separator, version = spec.rpartition("@")
        if not separator or not name:
            name, version = spec, "*"
        dependencies[name] = version
    (npm_root / "package.json").write_text(
        json.dumps(
            {
                "name": "young-router-staging",
                "version": "0.0.0",
                "private": True,
                "dependencies": dependencies,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def run_npm_install(
    npm: str,
    npm_root: Path,
    package_tarball: Path,
    peer_specs: Iterable[str],
    *,
    package_name: str,
    timeout: int,
) -> None:
    npm_dependency_manifest(npm_root, peer_specs)
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
    "download_bytes",
    "find_npm",
    "find_package_manager",
    "flatten_npm_package",
    "package_metadata",
    "npm_dependency_manifest",
    "record_staged_release",
    "release_integrity",
    "request_bytes",
    "request_json",
    "reused_staged_release",
    "run_npm_install",
    "run_package_manager_install",
    "temporary_install_tree",
]
