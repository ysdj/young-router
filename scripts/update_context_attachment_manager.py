#!/usr/bin/env python3
"""Pin the Context Attachment Manager release this build is written against.

Young Router speaks Context Attachment Manager's wire wording and reads its
selection file, so a release that reworded a placeholder or reshaped
``selection/<thread>.json`` would leave this app's image context subtly wrong in
a way no unit test can see: the model would read a sentence this pass never
writes, or a selection the user made would stop applying.

The upstream is a Codex plugin written in TypeScript, so nothing is bundled from
it and its own proxy is never used -- this check is a compatibility gate, not a
staging step.  Every artifact build resolves the current release and fails when
the part this app mirrors has moved:

* the ``IMG-xxx`` numbering its index assigns,
* the placeholder sentences and the heading both modes write,
* the developer note that explains the placeholders,
* the ``unchecked`` / ``pinned`` / ``auto`` shape of its selection state.

The release is resolved over the git protocol and its source archive is fetched
from codeload, not from the GitHub REST API: an anonymous API call is rate
limited to a few dozen requests an hour per address, and a build that fails
because of somebody else's quota is a build nobody can trust.

``*_URL`` / ``*_TAGS_URL`` environment variables and the matching command-line
options exist for offline and unit-test fixtures, mirroring the other update
scripts.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from update_common import (  # noqa: E402
    PACKAGE_VERSION_PATTERN,
    UpdateError,
    record_staged_release,
    request_bytes,
    reused_staged_release,
)


REPOSITORY = "chipfighter/context-attachment-manager"
TAGS_URL = os.environ.get(
    "CONTEXT_ATTACHMENT_MANAGER_TAGS_URL",
    f"https://github.com/{REPOSITORY}.git",
)
ARCHIVE_URL = os.environ.get(
    "CONTEXT_ATTACHMENT_MANAGER_ARCHIVE_URL",
    f"https://codeload.github.com/{REPOSITORY}/tar.gz/refs/tags",
)
# The release this checkout's placeholder wording and selection reader were
# written against.  A newer release is accepted only if every mirrored detail
# below still matches, which is what makes an upgrade safe to take.
PINNED_RELEASE = "0.9.1"
DEFAULT_TIMEOUT_SECONDS = 60
USER_AGENT = "Young-Router/context-attachment-manager-build"

# Files this app mirrors.  They are read from the release's own source archive;
# nothing is copied into the bundle.
REWRITE_SOURCE = "plugin/src/rewrite.ts"
SELECTION_SOURCE = "plugin/src/selection.ts"
THREAD_INDEX_SOURCE = "plugin/src/thread-index.ts"

# What the mirrored behaviour depends on.  Each entry is (file, pattern, why).
REQUIRED_MARKERS: tuple[tuple[str, str, str], ...] = (
    (
        THREAD_INDEX_SOURCE,
        r"IMG-\$\{String\(",
        "the IMG-xxx numbering a placeholder names changed",
    ),
    (
        REWRITE_SOURCE,
        r"export function rewriteItems",
        "the placeholder writer this pass mirrors is gone or renamed",
    ),
    (
        REWRITE_SOURCE,
        r"需要重看时，在回复里写",
        "the manual placeholder sentence changed",
    ),
    (
        REWRITE_SOURCE,
        r"需要原图时调用",
        "the automatic placeholder sentence changed",
    ),
    (
        REWRITE_SOURCE,
        r"已省略",
        "the placeholder heading state changed",
    ),
    (
        REWRITE_SOURCE,
        r"heading: \(id, label, state, fields\)",
        "the placeholder heading shape changed",
    ),
    (
        REWRITE_SOURCE,
        r"上下文管理说明",
        "the developer note this pass mirrors changed",
    ),
    (
        SELECTION_SOURCE,
        r"unchecked",
        "the unchecked-key selection field this pass reads is gone",
    ),
    (
        SELECTION_SOURCE,
        r"pinned",
        "the pinned-key selection field this pass reads is gone",
    ),
    (
        SELECTION_SOURCE,
        r"auto\??:",
        "the per-task automatic-selection flag this pass reads is gone",
    ),
)


def _request(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    return request_bytes(url, timeout=timeout, user_agent=USER_AGENT)


def _version_key(version: str) -> tuple:
    """Sort releases the way a maintainer reads them, not as strings."""

    parts: list[Any] = []
    for piece in re.split(r"[.\-+]", version):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def resolve_release(tags_url: str = TAGS_URL) -> dict[str, Any]:
    """The newest published tag, over the git protocol."""

    try:
        listing = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", tags_url],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(f"could not list {REPOSITORY} tags: {error}") from None
    if listing.returncode != 0:
        raise UpdateError(
            f"could not list {REPOSITORY} tags: {listing.stderr.strip()[:200]}"
        )
    versions: list[tuple[str, str, str]] = []
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith("refs/tags/"):
            continue
        commit, ref = fields
        tag = ref[len("refs/tags/") :]
        version = tag[1:] if tag.startswith("v") else tag
        if not re.fullmatch(PACKAGE_VERSION_PATTERN, version):
            continue
        versions.append((version, tag, commit))
    if not versions:
        raise UpdateError(f"{REPOSITORY} publishes no version tag")
    version, tag, commit = max(versions, key=lambda item: _version_key(item[0]))
    # The commit is the provenance a later build matches: a tag that was moved
    # onto different code is a different release even though its name is not.
    return {"version": version, "tag": tag, "commit": commit}


def _read_archive_member(archive: bytes, name: str) -> str:
    """One file out of a release tarball, without unpacking it anywhere."""

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            # The archive root is ``<repo>-<tag>/``.
            if member.name.split("/", 1)[-1] != name:
                continue
            extracted = bundle.extractfile(member)
            if extracted is None:
                break
            return extracted.read().decode("utf-8", errors="replace")
    raise UpdateError(f"{REPOSITORY} release carries no {name}")


def verify_mirrored_detail(archive: bytes) -> None:
    """Fail the build when something this app mirrors has moved upstream."""

    texts: dict[str, str] = {}
    for path, pattern, why in REQUIRED_MARKERS:
        if path not in texts:
            texts[path] = _read_archive_member(archive, path)
        if re.search(pattern, texts[path]) is None:
            raise UpdateError(
                f"{REPOSITORY} {path} no longer matches /{pattern}/: {why}. "
                "Re-read that file and update young_router/proxy/image_context.py "
                "before taking this release."
            )


def stage(
    output: Path,
    *,
    tags_url: str = TAGS_URL,
    archive_url: str = ARCHIVE_URL,
) -> dict[str, Any]:
    """Resolve, verify and record the release this build is checked against."""

    release = resolve_release(tags_url)
    version = release["version"]
    commit = release["commit"]
    if reused_staged_release(
        output,
        package_name=REPOSITORY,
        version=version,
        integrity=commit,
        required_files=("release.json",),
    ):
        return {"version": version, "reused": True}
    archive = _request(f"{archive_url}/{release['tag']}")
    verify_mirrored_detail(archive)
    if version != PINNED_RELEASE:
        # A newer release that carries every mirrored detail is fine; say so,
        # because `PINNED_RELEASE` is then the one line that moves.
        print(
            f"{REPOSITORY} {version} is newer than the pinned {PINNED_RELEASE}; "
            "every mirrored detail still matches.",
            file=sys.stderr,
        )
    output.mkdir(parents=True, exist_ok=True)
    (output / "release.json").write_text(
        json.dumps(
            {
                "repository": REPOSITORY,
                "version": version,
                "tag": release["tag"],
                "commit": commit,
                "pinned": PINNED_RELEASE,
                # Each file is read once per build; the marker list has several
                # entries per file, so the recorded list is deduplicated.
                "verified": sorted({path for path, _, _ in REQUIRED_MARKERS}),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    record_staged_release(
        output, package_name=REPOSITORY, version=version, integrity=commit
    )
    return {"version": version, "reused": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tags-url", default=TAGS_URL)
    parser.add_argument("--archive-url", default=ARCHIVE_URL)
    arguments = parser.parse_args(argv)
    try:
        result = stage(
            arguments.output,
            tags_url=arguments.tags_url,
            archive_url=arguments.archive_url,
        )
    except UpdateError as error:
        print(f"{REPOSITORY} check failed: {error}", file=sys.stderr)
        return 1
    print(
        f"{REPOSITORY} {result['version']}"
        f"{' (already verified)' if result['reused'] else ''}: mirrored detail intact"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
