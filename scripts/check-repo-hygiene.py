#!/usr/bin/env python3
"""Refuse to record private values in this public repository.

The gate exists because the leaks it catches are not reproducible after the
fact.  A real provider endpoint, a personal machine's path, or a developer's
own address in a commit object cannot be fixed by a later commit: the blob and
the commit metadata stay reachable by object id, so the only working remedy is
to notice before the object is created.  Everything here is therefore checked
against what a commit or push would actually record -- staged blobs, the
message, the identity, and the ignore rules -- and never against the working
tree, which is allowed to hold whatever a build or a run left behind.

Two kinds of finding, with deliberately different rules:

* Secrets are patterns a person cannot read a false positive out of (cloud key
  shapes, PEM headers, JSON Web Tokens).  They are refused everywhere, because
  no tracked file in this repository legitimately contains one.
* Local facts are the machine, account, and network a run happened on: home
  paths, private addresses, `.local` hostnames, and the addresses-in-use of a
  developer's own identity.  Those are refused in the files a reader studies,
  and allowed inside the test tree, which has to spell a path out to prove the
  router does not learn it.

Run with ``--staged`` (default) in the commit hook to read the index, or with
``--history <range>`` to re-audit commits that were already recorded.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


# A tracked file must never contain these, in any directory.
#
# Every pattern here has to be one a reader cannot dismiss as a fixture.  The
# fixtures this repository writes are readable words -- ``sk-replace-...``,
# ``Bearer replace-me-...`` -- while the values these patterns match carry the
# entropy of the real thing: a long unbroken run of key characters, or a
# three-part encoded token.  A looser pattern is worse than no pattern, because
# a gate that cries wolf is a gate people learn to pass with ``--no-verify``.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("a PEM private key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "an OpenAI-style API key",
        re.compile(rb"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9]{32,}\b"),
    ),
    (
        "an Anthropic API key",
        re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{40,}\b"),
    ),
    ("a GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    (
        "a GitHub fine-grained token",
        re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    ),
    ("an AWS access key id", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Slack token", re.compile(rb"\bxox[abposr]-[0-9A-Za-z-]{10,}\b")),
    (
        "a JSON Web Token",
        re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        # An opaque credential, not a phrase: 40+ characters of token alphabet
        # with no whitespace.  ``Bearer replace-me-secondary-secret`` is a
        # fixture and must not match.
        "a bearer credential",
        re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9/+]{40,}=*\b"),
    ),
)

# Machine, account, and network facts that identify whose checkout this is.
# ``example`` is the placeholder this repository already writes, so the tests
# that prove a path is stripped keep working unchanged.
LOCAL_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "a developer home directory",
        re.compile(rb"/(?:Users|home)/(?!example\b|Shared\b|runner\b)[A-Za-z0-9._-]+/"),
    ),
    (
        "a Windows user directory",
        re.compile(
            rb"(?i)\b[A-Z]:\\+Users\\+(?!example\b|runneradmin\b|Public\b)[A-Za-z0-9._-]+"
        ),
    ),
    (
        # A machine name, not an attribute or a filename extension: macOS names
        # the host after the user and appends ``.local``, and that label carries
        # an uppercase letter or a digit, while ``threading.local`` and
        # ``.xcode.env.local`` do not.
        "a macOS .local hostname",
        re.compile(rb"\b(?=[A-Za-z0-9-]*[A-Z0-9])[A-Za-z0-9-]{2,}\.local\b"),
    ),
    (
        # A private address is only a leak where it is dialled or browsed: an
        # endpoint carries a port or a path.  Windows SDK versions
        # (``10.0.22621.0``) are four dotted numbers too and carry neither.
        "a private network endpoint",
        re.compile(
            rb"(?:https?://)?(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            rb"|192\.168\.\d{1,3}\.\d{1,3}"
            rb"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
            rb"(?::\d{1,5}\b|/)"
        ),
    ),
)

# A personal mailbox is only a leak when it is a real one; the fixtures this
# repository uses all live under a reserved-for-documentation domain.
EMAIL_PATTERN = re.compile(
    rb"\b[A-Za-z0-9._%+-]+@"
    rb"(?!example\.(?:com|test|invalid|org)\b)"
    rb"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

# A lockfile records the maintainers of the packages it pins by design, and a
# reserved domain says nothing about the machine that ran the install.
EMAIL_ALLOWED_SUFFIXES = (
    b"users.noreply.github.com",
    b"noreply.github.com",
    b"example.com",
    b"example.test",
    b"example.invalid",
    b"example.org",
    b"izs.me",
    b"subtlelogic.com",
)

# ``icon@2x.png`` is an asset scale suffix, not a mailbox.
FILE_EXTENSION_TLDS = (
    b"png", b"jpg", b"jpeg", b"gif", b"svg", b"webp", b"ico", b"pdf",
    b"json", b"yaml", b"yml", b"toml", b"lock", b"py", b"sh", b"ps1",
    b"ts", b"tsx", b"js", b"mjs", b"md", b"txt", b"csv", b"plist",
    b"rb", b"c", b"h", b"m", b"mm", b"cpp", b"hpp",
)

# Where a local fact may legitimately appear.  ``tests/`` spells out the paths
# a hostile transcript would carry in order to prove the router never learns
# them, and the staged integration trees are generated from upstream releases
# this repository does not own.
LOCAL_FACT_EXEMPT_PREFIXES = (
    "tests/",
    "rn/vendor/",
    "rn/.staging-cache/",
)

# A lockfile pins maintainer addresses; nothing else about it is private.
LOCAL_FACT_EXEMPT_FILES = ("rn/pnpm-lock.yaml",)

SENSITIVE_FILE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ``config.example.yaml`` is the publishable template this repository
    # ships, so the placeholder name is what the pattern steps over.
    (
        "a runtime configuration",
        re.compile(r"(^|/)config\.yaml$"),
    ),
    (
        "a disabled-model configuration",
        re.compile(r"(^|/)config\.(?!example\.)[^/]*\.yaml$"),
    ),
    (
        "an environment file",
        re.compile(r"(^|/)\.env$"),
    ),
    ("a service log", re.compile(r"\.log$")),
    ("a local state database", re.compile(r"\.(?:db|sqlite3?|pid)$")),
    ("a launch agent plist", re.compile(r"young\.router\.[^/]*\.plist$")),
    ("a WebDAV settings document", re.compile(r"(^|/)webdav[^/]*\.(?:json|yaml)$")),
)


class Finding:
    __slots__ = ("location", "kind", "detail")

    def __init__(self, location: str, kind: str, detail: str) -> None:
        self.location = location
        self.kind = kind
        self.detail = detail

    def render(self) -> str:
        return f"{self.location}: {self.kind}: {self.detail}"


def _run(args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        args,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed: {completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout


def _git(*args: str) -> str:
    return _run(["git", *args]).decode("utf-8", "replace").strip()


def _repository_root() -> Path:
    return Path(_git("rev-parse", "--show-toplevel"))


def _index_blobs() -> dict[str, bytes]:
    """Read every staged blob, skipping binary content.

    Pattern matching runs over text because a match inside a compressed or
    compiled artifact is not a value anyone can read back out, and the
    repository ships several such artifacts on purpose.
    """

    listing = _run(["git", "ls-files", "--stage", "-z"])
    entries: list[tuple[str, str]] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) >= 3 and fields[2] != b"0":
            continue
        if len(fields) >= 2:
            entries.append((fields[1].decode(), path.decode("utf-8", "replace")))
    return _read_blobs(entries)


def _read_blobs(entries: list[tuple[str, str]]) -> dict[str, bytes]:
    """Read a list of ``(object id, path)`` pairs through one batch call."""

    blobs: dict[str, bytes] = {}
    if not entries:
        return blobs
    payload = b"\n".join(object_id.encode() for object_id, _ in entries) + b"\n"
    stream = _run(["git", "cat-file", "--batch"], input_bytes=payload)
    for (_, path), blob in zip(entries, _split_batch(stream)):
        if b"\0" in blob[:8192]:
            continue
        blobs[path] = blob
    return blobs


def _local_facts_allowed(name: str) -> bool:
    return name.startswith(LOCAL_FACT_EXEMPT_PREFIXES) or name in LOCAL_FACT_EXEMPT_FILES


def _scan_bytes(data: bytes, location: str, *, local_facts_allowed: bool) -> list[Finding]:
    findings: list[Finding] = []
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(data):
            findings.append(Finding(location, "secret", kind))
    # A mailbox is checked in every file, including the tests: a fixture belongs
    # under a reserved domain, so a real address is a leak wherever it appears.
    for match in EMAIL_PATTERN.finditer(data):
        address = match.group(0)
        if address.endswith(EMAIL_ALLOWED_SUFFIXES):
            continue
        if address.rsplit(b".", 1)[-1].lower() in FILE_EXTENSION_TLDS:
            continue
        findings.append(
            Finding(
                location,
                "local fact",
                f"a personal address ({address.decode('utf-8', 'replace')})",
            )
        )
    if local_facts_allowed:
        return findings
    for kind, pattern in LOCAL_PATTERNS:
        if pattern.search(data):
            findings.append(Finding(location, "local fact", kind))
    return findings


def scan_staged() -> list[Finding]:
    """Audit the index, the pending message, the identity, and the ignore rules."""

    findings: list[Finding] = []
    staged = [
        name
        for name in _run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        ).decode("utf-8", "replace").split("\0")
        if name
    ]

    for name in staged:
        for kind, pattern in SENSITIVE_FILE_PATTERNS:
            if pattern.search(name):
                findings.append(Finding(f"{name}", "tracked runtime file", kind))

    # Only the paths being committed are read: a file that is already tracked
    # was audited when it was committed, and re-reading all of them on every
    # commit would make the gate slow enough to be bypassed.
    index_blobs = _index_blobs()
    for name in staged:
        data = index_blobs.get(name)
        if data is None:
            continue
        findings.extend(
            _scan_bytes(data, name, local_facts_allowed=_local_facts_allowed(name))
        )

    name, email = _identity()
    findings.extend(scan_identity(name, email))

    findings.extend(scan_ignore_rules())
    return findings


def scan_message(path: str) -> list[Finding]:
    """Audit a commit message file, as the ``commit-msg`` hook receives it.

    The message is the half of a commit object a reader scans in a log view
    without ever opening a blob, and it carries its own hook because
    ``pre-commit`` cannot see it: git writes the file after that hook returns,
    so reading ``COMMIT_EDITMSG`` there would audit the previous attempt's
    message instead of the one being made.
    """

    message = Path(path)
    if not message.is_file():
        return []
    findings = _scan_bytes(
        message.read_bytes(),
        "commit message",
        local_facts_allowed=False,
    )
    # The identity is recorded by the same command, and this hook is the last
    # point before the object exists.
    findings.extend(scan_identity())
    return findings


def scan_identity(name: str = "", email: str = "") -> list[Finding]:
    """Refuse an identity that names this machine or a private mailbox.

    A commit's author is recorded forever and travels with every clone, so the
    one place a personal machine name must never reach is the object header.
    """

    if not name and not email:
        name, email = _identity()
    findings: list[Finding] = []
    for value, label in ((name, "user.name"), (email, "user.email")):
        if not value:
            continue
        findings.extend(
            _scan_bytes(
                value.encode(),
                f"git config {label}",
                local_facts_allowed=False,
            )
        )
    return findings


def scan_ignore_rules() -> list[Finding]:
    """A committed .gitignore must not carry the exceptions it was written for.

    A negation such as ``!config.yaml`` tracks the file it names, which is how
    a runtime document that was never meant to be public reaches a release.
    """

    findings: list[Finding] = []
    path = _repository_root() / ".gitignore"
    if not path.is_file():
        return findings
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        stripped = line.strip()
        if stripped.startswith("!") and stripped != "!":
            findings.append(
                Finding(
                    f".gitignore:{number}",
                    "ignore exception",
                    f"a negation tracks what it names ({stripped})",
                )
            )
    return findings


def _identity() -> tuple[str, str]:
    name = ""
    email = ""
    try:
        name = _git("config", "--get", "user.name")
        email = _git("config", "--get", "user.email")
    except RuntimeError:
        pass
    return name, email


def scan_history(revision_range: str) -> list[Finding]:
    """Re-audit commits that were already recorded.

    Every blob the range can reach is read, not just the tree at its tip: the
    leaks this mode exists to find were committed and later deleted, so their
    only remaining copy is in an interior commit that the tip no longer names.
    """

    findings: list[Finding] = []
    commits = [line for line in _git("rev-list", revision_range).splitlines() if line]
    for commit in commits:
        raw = _run(["git", "cat-file", "commit", commit]).decode("utf-8", "replace")
        header, _, body = raw.partition("\n\n")
        for line in header.splitlines():
            if not line.startswith(("author ", "committer ")):
                continue
            identity = line.split("<", 1)[-1].split(">", 1)[0]
            findings.extend(
                _scan_bytes(
                    identity.encode(),
                    f"commit {commit[:8]} identity",
                    local_facts_allowed=False,
                )
            )
        findings.extend(
            _scan_bytes(
                body.encode(),
                f"commit {commit[:8]} message",
                local_facts_allowed=False,
            )
        )
    findings.extend(_scan_reachable_blobs(revision_range))
    return findings


def _scan_reachable_blobs(revision_range: str) -> list[Finding]:
    """Read every text blob the range's history holds.

    A path is not deduplicated across commits: the leaks this mode exists to
    find were committed into ``AGENTS.md`` and removed again in a later one, so
    the newest revision of a path is exactly the revision that no longer
    carries them.  Objects are read by id and each distinct blob is scanned
    once, which is the whole of the deduplication that is safe here.
    """

    findings: list[Finding] = []
    listing = _run(["git", "rev-list", "--objects", revision_range]).decode(
        "utf-8", "replace"
    )
    scanned: set[str] = set()
    for line in listing.splitlines():
        object_id, _, path = line.partition(" ")
        if not path or object_id in scanned:
            continue
        scanned.add(object_id)
        try:
            data = _run(["git", "cat-file", "blob", object_id])
        except RuntimeError:
            continue
        if b"\0" in data[:8192]:
            continue
        findings.extend(
            _scan_bytes(
                data,
                f"{_short(object_id)} {path}",
                local_facts_allowed=_local_facts_allowed(path),
            )
        )
    return findings


def _short(object_id: str) -> str:
    return object_id[:8]


def scan_tree(revision: str = "HEAD") -> list[Finding]:
    """Audit the files a fresh checkout of ``revision`` would carry.

    This is the fast half of the history audit and the slow half of the commit
    gate: it reads one tree, not every blob the history reaches.
    """

    findings: list[Finding] = []
    listing = _run(["git", "ls-tree", "-r", "-z", revision])
    entries: list[tuple[str, str]] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) >= 3 and fields[1] == b"blob":
            entries.append((fields[2].decode(), path.decode("utf-8", "replace")))
    if not entries:
        return findings
    payload = b"\n".join(object_id.encode() for object_id, _ in entries)
    stream = _run(["git", "cat-file", "--batch"], input_bytes=payload + b"\n")
    for (object_id, path), chunk in zip(entries, _split_batch(stream)):
        del object_id
        if b"\0" in chunk[:8192]:
            continue
        findings.extend(
            _scan_bytes(
                chunk,
                path,
                local_facts_allowed=_local_facts_allowed(path),
            )
        )
    return findings


def _split_batch(stream: bytes) -> list[bytes]:
    """Split ``git cat-file --batch`` output into its blob payloads.

    Each record opens with ``<object> blob <size>`` followed by the payload and
    one trailing newline, so the payload length in the header is what finds the
    boundary -- payloads are binary-safe and may contain a newline.
    """

    payloads: list[bytes] = []
    cursor = 0
    while cursor < len(stream):
        newline = stream.find(b"\n", cursor)
        if newline < 0:
            break
        header = stream[cursor:newline]
        fields = header.split()
        if len(fields) < 3 or not fields[-1].isdigit():
            break
        size = int(fields[-1])
        start = newline + 1
        payloads.append(stream[start : start + size])
        cursor = start + size + 1
    return payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="audit the index, pending message, identity, and ignore rules (default)",
    )
    parser.add_argument(
        "--history",
        metavar="RANGE",
        help="re-audit recorded commits, e.g. 'origin/main..HEAD' or '--all'",
    )
    parser.add_argument(
        "--tree",
        metavar="REVISION",
        nargs="?",
        const="HEAD",
        help="audit one tree, e.g. 'HEAD' (files a fresh checkout carries)",
    )
    parser.add_argument(
        "--message",
        metavar="FILE",
        help="audit a commit message file (the commit-msg hook's argument)",
    )
    parser.add_argument(
        "--identity",
        action="store_true",
        help="audit the configured git identity alone",
    )
    args = parser.parse_args(argv)

    try:
        if args.message:
            findings = scan_message(args.message)
            scope = f"commit message {args.message}"
        elif args.history:
            findings = scan_history(args.history)
            scope = f"history {args.history}"
        elif args.tree:
            findings = scan_tree(args.tree)
            scope = f"tree {args.tree}"
        elif args.identity:
            findings = scan_identity()
            scope = "git identity"
        else:
            findings = scan_staged()
            scope = "staged commit"
    except RuntimeError as error:
        print(f"repo hygiene check could not run: {error}", file=sys.stderr)
        return 2

    if not findings:
        print(f"repo hygiene: {scope} is clean")
        return 0

    print(f"repo hygiene: {scope} would record private values\n", file=sys.stderr)
    seen: set[str] = set()
    for finding in findings:
        rendered = finding.render()
        if rendered in seen:
            continue
        seen.add(rendered)
        print(f"  {rendered}", file=sys.stderr)
    print(
        "\nReplace each value with a neutral fixture.  Tests may name a local "
        "path under tests/; committed sources may not.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
