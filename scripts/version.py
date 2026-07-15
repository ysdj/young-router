#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PLIST_BUDDY = Path("/usr/libexec/PlistBuddy")


@dataclass(frozen=True)
class VersionPaths:
    root: Path
    version_file: Path
    build_file: Path
    info_plist: Path
    cask_file: Path

    @classmethod
    def for_root(cls, root: Path) -> "VersionPaths":
        root = root.resolve()
        return cls(
            root=root,
            version_file=root / "VERSION",
            build_file=root / "BUILD_NUMBER",
            info_plist=root / "mac_menu" / "Info.plist",
            cask_file=root / "Casks" / "litellm-menu.rb",
        )


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(f"missing required version file: {path}") from exc


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("VERSION must use MAJOR.MINOR.PATCH, for example 1.0.0")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def read_version(paths: VersionPaths) -> tuple[int, int, int]:
    return parse_version(read_text(paths.version_file))


def read_build_number(paths: VersionPaths) -> int:
    raw = read_text(paths.build_file)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("BUILD_NUMBER must be a positive integer") from exc
    if value <= 0:
        raise ValueError("BUILD_NUMBER must be a positive integer")
    return value


def write_text_if_changed(path: Path, value: str) -> bool:
    old_value = path.read_text(encoding="utf-8") if path.exists() else None
    if old_value == value:
        return False
    path.write_text(value, encoding="utf-8")
    return True


def sync_info_plist(paths: VersionPaths, plist_path: Path | None = None) -> bool:
    plist_path = plist_path or paths.info_plist
    version = format_version(read_version(paths))
    build_number = str(read_build_number(paths))

    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)

    changed = False
    if info.get("CFBundleShortVersionString") != version:
        info["CFBundleShortVersionString"] = version
        changed = True
    if info.get("CFBundleVersion") != build_number:
        info["CFBundleVersion"] = build_number
        changed = True

    if changed:
        if PLIST_BUDDY.exists():
            subprocess.run(
                [str(PLIST_BUDDY), "-c", f"Set :CFBundleShortVersionString {version}", str(plist_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [str(PLIST_BUDDY), "-c", f"Set :CFBundleVersion {build_number}", str(plist_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            with plist_path.open("wb") as handle:
                plistlib.dump(info, handle, sort_keys=False)
    return changed


def sync_cask(paths: VersionPaths) -> bool:
    if not paths.cask_file.exists():
        return False
    version = format_version(read_version(paths))
    text = paths.cask_file.read_text(encoding="utf-8")
    updated = re.sub(
        r'(^\s*version\s+")([^"]+)(")',
        rf"\g<1>{version}\3",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        return False
    paths.cask_file.write_text(updated, encoding="utf-8")
    return True


def sync_metadata(paths: VersionPaths, plist_path: Path | None = None) -> bool:
    changed = sync_info_plist(paths, plist_path=plist_path)
    if plist_path is None or plist_path == paths.info_plist:
        changed = sync_cask(paths) or changed
    return changed


def bump_patch(paths: VersionPaths) -> tuple[str, int]:
    major, minor, patch = read_version(paths)
    build_number = read_build_number(paths)
    next_version = (major, minor, patch + 1)
    next_build = build_number + 1

    write_text_if_changed(paths.version_file, f"{format_version(next_version)}\n")
    write_text_if_changed(paths.build_file, f"{next_build}\n")
    sync_metadata(paths)
    return format_version(next_version), next_build


def stage_version_files(paths: VersionPaths) -> None:
    files = [
        paths.version_file.relative_to(paths.root),
        paths.build_file.relative_to(paths.root),
        paths.info_plist.relative_to(paths.root),
    ]
    if paths.cask_file.exists():
        files.append(paths.cask_file.relative_to(paths.root))
    subprocess.run(["git", "add", "--", *map(str, files)], cwd=paths.root, check=True)


def show(paths: VersionPaths, as_json: bool = False) -> str:
    version = format_version(read_version(paths))
    build_number = read_build_number(paths)
    if as_json:
        return json.dumps({"version": version, "build": build_number}, ensure_ascii=False)
    return f"{version} ({build_number})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage LiteLLM Menu app version metadata.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="print the current app version")
    show_parser.add_argument("--json", action="store_true", help="print JSON instead of text")

    bump_parser = subparsers.add_parser("bump", help="increment patch version and build number")
    bump_parser.add_argument("--stage", action="store_true", help="stage version files with git add")

    sync_parser = subparsers.add_parser("sync", help="sync Info.plist from VERSION and BUILD_NUMBER")
    sync_parser.add_argument("--stage", action="store_true", help="stage version files with git add")
    sync_parser.add_argument("--plist", type=Path, help="Info.plist path to update")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = VersionPaths.for_root(args.root)

    try:
        if args.command == "show":
            print(show(paths, as_json=args.json))
        elif args.command == "bump":
            version, build_number = bump_patch(paths)
            if args.stage:
                stage_version_files(paths)
            print(f"{version} ({build_number})")
        elif args.command == "sync":
            sync_metadata(paths, plist_path=args.plist)
            if args.stage:
                stage_version_files(paths)
            print(show(paths))
        else:
            parser.error(f"unknown command: {args.command}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"version: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
