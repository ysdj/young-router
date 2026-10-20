#!/usr/bin/env python3
"""Stage the Our Free Model upstream without altering its own protocol.

The Our Free Model lane reuses the upstream project's runtime and protocol.
Its source is copied intact into the Core adapter tree; only Young Router's own
process bridge lives outside that tree.

Every artifact build resolves the upstream's **latest published release** and
stages that tag.  ``main`` is a development branch: it carries unreviewed work
and it moves without a version, so a build pointed at it packaged whatever the
branch held that morning and could not say which release it had shipped.  The
release tag is what the upstream publishes installable artifacts from, and its
version is the one its own release notes and staged payloads are cut against.
When the latest release cannot be resolved the build fails instead of falling
back to a branch or to the previously staged tree.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import re
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
    request_bytes,
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
#: The release lookup this build resolves against.  It reads the same repository
#: the clone below reads, so a mirror configured for the clone is also the one
#: whose releases are resolved.
RELEASES_URL = os.environ.get(
    "OUR_FREE_MODEL_RELEASES_URL",
    "https://api.github.com/repos/Ebony-Vinyl/dsh-our-free-model/releases?per_page=30",
)
#: The API is consulted only for a URL that really is GitHub's release list: a
#: mirror or a fixture gets the git tag list instead of a rewritten URL.
GITHUB_RELEASES_PREFIX = "https://api.github.com/"
#: Release tags are ``v2.5.5``; a tag that does not parse is not a release this
#: build can pin, so it is skipped rather than guessed at.
RELEASE_TAG_PATTERN = re.compile(r"^v?(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$")
USER_AGENT = "Young-Router/our-free-model-build"
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


@dataclass(frozen=True)
class Release:
    """One published upstream release and the identity that proves it.

    ``revision`` is the commit the tag points at.  It is the release's real
    identity: a tag can be moved, and the same tree can be reached from two tag
    spellings, while the commit cannot.  It is also far cheaper to learn than a
    clone - one ``ls-remote`` names it in about a second - so a build can decide
    whether it already holds the release without fetching its bytes.
    """

    tag: str
    version: str
    revision: str


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def _highest_release_candidate(tags: list[tuple[str, str, bool, bool]]) -> Release | None:
    """The highest stable ``vX.Y.Z`` among ``(tag, revision, draft, prerelease)``."""

    candidates: list[Release] = []
    for tag, revision, draft, prerelease in tags:
        if draft or prerelease:
            continue
        match = RELEASE_TAG_PATTERN.fullmatch(tag)
        if match is None:
            continue
        candidates.append(
            Release(tag=tag, version=match.group("version"), revision=revision)
        )
    if not candidates:
        return None
    return max(candidates, key=lambda release: _version_key(release.version))


def _releases_api(timeout: int) -> list[tuple[str, str, bool, bool]]:
    """The upstream's own release list, which states draft and prerelease.

    GitHub's API is asked first because only that answer distinguishes a stable
    release from one marked prerelease under a tag name that does not say so.
    Its ``target_commitish`` is not the revision - GitHub reports the branch a
    release was cut from there - so the answer carries no identity and the git
    tag list below does.
    """

    raw = request_bytes(RELEASES_URL, timeout=timeout, user_agent=USER_AGENT)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(
            f"The {PACKAGE_NAME} release list is not valid JSON (check {RELEASES_URL})"
        ) from exc
    if not isinstance(document, list):
        raise UpdateError(f"The {PACKAGE_NAME} release list is not a list of releases")
    return [
        (
            str(item.get("tag_name") or "").strip(),
            "",
            item.get("draft") is True,
            item.get("prerelease") is True,
        )
        for item in document
        if isinstance(item, dict)
    ]


def _remote_tags(source_url: str, timeout: int) -> list[tuple[str, str, bool, bool]]:
    """Every tag and the commit it points at, from the git protocol.

    The GitHub API allows sixty anonymous reads an hour per address, and a
    build that resolves a release on every run spends that budget across
    machines and CI runners until the lookup itself fails.  The git remote has
    no such counter and answers in about a second, so it is where the release
    identity comes from: a tag list says which releases exist and which commit
    each one is.
    """

    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", source_url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise UpdateError(
            f"Could not list {PACKAGE_NAME} release tags: "
            + (detail[-1] if detail else f"git exited {result.returncode}")
        )
    tags: list[tuple[str, str, bool, bool]] = []
    for line in result.stdout.splitlines():
        revision, _, reference = line.partition("\t")
        if reference.startswith("refs/tags/"):
            tags.append((reference[len("refs/tags/"):], revision.strip(), False, False))
    return tags


def latest_release(source_url: str, *, timeout: int = 30) -> Release:
    """Resolve the newest published release, with the commit that proves it.

    The tag list is the answer this build needs, because it is the only lookup
    that says both which releases exist and which commit each one is.  GitHub's
    release metadata is consulted first only for the property a tag name cannot
    express: a release marked prerelease.  When that call cannot answer - it is
    rate limited, offline, or a mirror - the build resolves from the tags, and a
    tag that spells its own prerelease in the name is not a bare version anyway.

    There is no further fallback: if no stable release can be read, the build
    fails rather than staging a branch or the previously staged tree.
    """

    tags = _remote_tags(source_url, timeout)
    resolved = _highest_release_candidate(tags)
    if resolved is None:
        raise UpdateError(
            f"{PACKAGE_NAME} has no published stable release to stage; "
            "every artifact build resolves one instead of a branch"
        )
    if GITHUB_RELEASES_PREFIX in RELEASES_URL:
        try:
            api = {tag: (draft, prerelease) for tag, _rev, draft, prerelease in _releases_api(timeout)}
        except (UpdateError, OSError):
            return resolved
        if api.get(resolved.tag) == (False, True):
            # The tag list cannot see this mark, so honour it: resolve again
            # with the API's verdict folded in.
            marked = [
                (tag, revision, *api.get(tag, (False, False)))
                for tag, revision, _draft, _prerelease in tags
            ]
            resolved = _highest_release_candidate(marked)
            if resolved is None:
                raise UpdateError(
                    f"{PACKAGE_NAME} has no published stable release to stage; "
                    "every artifact build resolves one instead of a branch"
                )
    return resolved


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


def _clone_tag(source_url: str, ref: str, destination: Path, timeout: int) -> None:
    """Check out exactly one release tag, shallow, with no branch tip.

    ``--branch`` accepts a tag, and a shallow clone of it is the release as
    published.  Kept as its own function so a fixture can supply the tree
    without a network round trip.
    """

    result = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            source_url,
            str(destination),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise UpdateError(
            detail[-1] if detail else f"Could not clone {PACKAGE_NAME} {ref}"
        )


def update(
    output: Path,
    *,
    source_url: str,
    ref: str | None = None,
) -> str:
    """Stage the newest published release, or the pinned ``ref`` when given.

    ``ref`` exists for a fixture and for a deliberate pin; the build passes
    nothing, so it resolves the release every time.  A ``file://`` source is a
    fixture and needs no release lookup: it already names the exact tree.
    """

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = source_url.startswith("file://")
    resolved_ref = ref
    release: Release | None = None
    if resolved_ref is None and not fixture:
        # One `ls-remote` answers both questions this build asks: which release
        # is newest, and whether the staged tree is already that release.  The
        # clone below is the only expensive step, and it happens only when the
        # answer is "not yet".
        release = latest_release(source_url)
        if reused_staged_release(
            output,
            package_name=PACKAGE_NAME,
            version=release.version,
            integrity=release.revision,
            required_files=REQUIRED_FILES,
            required_peer_dirs=REQUIRED_PEERS,
        ):
            return release.version
        resolved_ref = release.tag
    with tempfile.TemporaryDirectory(prefix="young-router-our-free-model-") as directory:
        work = Path(directory)
        checkout = work / "source"
        if fixture:
            shutil.copytree(Path(source_url[7:]), checkout, symlinks=True)
        else:
            assert resolved_ref is not None
            _clone_tag(source_url, resolved_ref, checkout, DEFAULT_TIMEOUT_SECONDS)
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
        # The version the release lookup resolved and the version inside the
        # checked-out tag have to be the same release.  A tag that points at a
        # tree still carrying the previous number is an upstream mistake, and
        # staging it would record a version this build never resolved.  This is
        # checked before the peer install, which is the expensive half.
        if release is not None and release.version != version:
            raise UpdateError(
                f"{PACKAGE_NAME} release {release.tag} declares version {version}, "
                f"not {release.version}; refusing to stage a mislabelled release"
            )

        payload = work / "payload"
        copy_tree(checkout, payload, package_name=PACKAGE_NAME)
        shutil.rmtree(payload / ".git", ignore_errors=True)
        # The reuse decision is made on the upstream payload alone, before the
        # peer closure is installed: an unchanged release reuses the staged
        # peers, and installing them first only to discard the result was the
        # larger half of a repeat build's staging time.
        # A resolved release is identified by its commit, which this build
        # already read from the remote; a pinned ref or a fixture has no such
        # identity, so its payload digest stands in.  The checkout is verified
        # against the tag either way by the version check above.
        integrity = release.revision if release is not None else _content_digest(
            payload, omit_nodes=True
        )
        if release is None and reused_staged_release(
            output,
            package_name=PACKAGE_NAME,
            version=version,
            integrity=integrity,
            required_files=REQUIRED_FILES,
            required_peer_dirs=REQUIRED_PEERS,
        ):
            return version
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
        # The peers are not part of the upstream release, so they cannot change
        # its digest; the recorded integrity is the payload this build resolved.
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
            integrity=integrity,
        )
        return version


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, help="Core/young_router/adapters/our-free-model destination"
    )
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument(
        "--ref",
        default=None,
        help="explicit upstream tag to pin (default: the newest published release)",
    )
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
